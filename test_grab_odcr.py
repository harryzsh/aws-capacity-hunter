#!/usr/bin/env python3
"""Unit tests for grab_odcr.py — focused on the restart-idempotency logic.

These tests mock boto3 entirely (no AWS calls, no cost, runs in CI). They pin
the behavior that the real Smoke Tests in SMOKE_TEST.md do NOT cover: the
pure-Python core that makes a crash/restart safe —

  * held_cores_by_az(): count CORES (TotalInstanceCount x vCPU), not objects;
    only_azs= restricts the count to in-scope AZs.
  * _az_full(): per-AZ cap judged against cores actually held
  * sweep_once(): ONE grab per AZ per pass; the --watch loop repeats sweeps to
    fill up. After a restart, full AZs are skipped and only the short ones get
    topped up — never double-grab a full AZ, never go lopsided.
  * print_list(): --list prints a per-AZ + total CORE summary (optional target,
    auto-read from the ledger) + per-reservation USED/free + used/total tally.
  * reserve_one() / list_reservations() / cancel_all(): the 3 ODCR API wrappers.

Run:  python3 -m unittest test_grab_odcr -v
"""
import datetime
import logging
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from botocore.exceptions import ClientError

import grab_odcr
from grab_odcr import (
    held_cores_by_az, _az_full, sweep_once, print_list,
    reserve_one, list_reservations, cancel_all, TAG_KEY, TAG_VAL,
    parse_per_type, held_count_by_type, sweep_once_per_type,
)
from common import VCPU

# Quiet the script's INFO chatter during tests; we assert on state, not logs.
logging.getLogger("i4i-grab").setLevel(logging.CRITICAL)

V16 = VCPU["i4i.16xlarge"]  # 64


def _cap_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}},
                       "CreateCapacityReservation")


class FakeEC2:
    """Minimal stand-in for a boto3 EC2 client.

    create_capacity_reservation records (type, az) and returns a fake id,
    UNLESS that AZ is in `no_capacity` (raises InsufficientInstanceCapacity).
    DryRun=True always raises DryRunOperation (mirrors real EC2).
    """
    def __init__(self, reservations=None, no_capacity=()):
        self._reservations = reservations or []
        self._no_capacity = set(no_capacity)
        self.created = []          # list of (itype, az)
        self.create_kwargs = []    # full kwargs of each create call
        self.cancelled = []        # crids passed to cancel
        self._n = 0

    def describe_capacity_reservations(self, **kwargs):
        return {"CapacityReservations": self._reservations}

    def create_capacity_reservation(self, **kwargs):
        self.create_kwargs.append(kwargs)
        if kwargs.get("DryRun"):
            raise _cap_error("DryRunOperation")
        az = kwargs["AvailabilityZone"]
        if az in self._no_capacity:
            raise _cap_error("InsufficientInstanceCapacity")
        self._n += 1
        self.created.append((kwargs["InstanceType"], az))
        return {"CapacityReservation": {"CapacityReservationId": "cr-%04d" % self._n}}

    def cancel_capacity_reservation(self, CapacityReservationId=None):
        self.cancelled.append(CapacityReservationId)
        return {}


def _reservation(itype, az, count, tag=TAG_VAL, state="active", available=None):
    # available defaults to count (all free / unused). Pass available<count to
    # simulate a reservation that has instances in it (USED).
    r = {
        "CapacityReservationId": "cr-existing",
        "InstanceType": itype,
        "AvailabilityZone": az,
        "State": state,
        "TotalInstanceCount": count,
        "AvailableInstanceCount": count if available is None else available,
    }
    if tag is not None:
        r["Tags"] = [{"Key": TAG_KEY, "Value": tag}]
    return r


def _args(**over):
    base = dict(region="us-east-1", types=["i4i.16xlarge"], target_cores=10000,
                per_az_cores=5000, live=True, end_hours=None)
    base.update(over)
    return Namespace(**base)


def _drain(client, args, azs, offered, held, max_rounds=10000):
    """Drive sweep_once repeatedly the way the --watch loop does, until the
    target is reached or a full pass makes no progress (capacity exhausted).
    `held` accumulates in memory across rounds (mirrors run() re-reading it)."""
    made = []
    for _ in range(max_rounds):
        if sum(held.values()) >= args.target_cores:
            break
        before = dict(held)
        sweep_once(client, args, azs, offered, held, made)
        if held == before:
            break  # no progress this round → capacity exhausted
    return made


class HeldCoresByAz(unittest.TestCase):
    def test_counts_cores_not_reservation_objects(self):
        # ONE reservation holding 3 instances = 192 cores, NOT 1.
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 3)])
        self.assertEqual(held_cores_by_az(client), {"us-east-1b": 3 * V16})

    def test_sums_multiple_reservations_per_az(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 2),   # 128
            _reservation("i4i.16xlarge", "us-east-1b", 1),   #  64
            _reservation("i4i.16xlarge", "us-east-1d", 3),   # 192
        ])
        self.assertEqual(held_cores_by_az(client),
                         {"us-east-1b": 192, "us-east-1d": 192})

    def test_ignores_untagged_reservations(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3, tag=None),
            _reservation("i4i.16xlarge", "us-east-1b", 1),   # ours: 64
        ])
        self.assertEqual(held_cores_by_az(client), {"us-east-1b": 64})

    def test_ignores_other_tag_values(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3, tag="something-else"),
        ])
        self.assertEqual(held_cores_by_az(client), {})

    def test_skips_unknown_instance_type(self):
        client = FakeEC2([_reservation("c7gd.metal", "us-east-1b", 2)])
        self.assertEqual(held_cores_by_az(client), {})

    def test_only_azs_filters_out_of_scope_stock(self):
        # The --azs scope bug: targeting only 1d must NOT count 1b's stock,
        # else 1b inflates the total gate and stops the run before 1d fills.
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 4),   # 256 (out of scope)
            _reservation("i4i.16xlarge", "us-east-1d", 3),   # 192 (in scope)
        ])
        # no filter -> counts both
        self.assertEqual(held_cores_by_az(client),
                         {"us-east-1b": 256, "us-east-1d": 192})
        # only 1d -> 1b's 256 excluded, total reflects just 1d
        only = held_cores_by_az(client, only_azs={"us-east-1d"})
        self.assertEqual(only, {"us-east-1d": 192})
        self.assertEqual(sum(only.values()), 192)   # gate sees 192, not 448


class PrintListSummary(unittest.TestCase):
    """--list must print a per-AZ + total CORE summary (not just rows)."""

    def test_summary_logs_per_az_and_total_cores(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),   # 192
            _reservation("i4i.16xlarge", "us-east-1d", 2),   # 128
            _reservation("i4i.16xlarge", "us-east-1d", 1),   #  64 -> 1d=192
            _reservation("i4i.16xlarge", "us-east-1c", 5, tag=None),  # not ours
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        out = "\n".join(cm.output)
        self.assertIn("us-east-1b", out)
        self.assertIn("192 vCPU", out)              # per-AZ core count shown
        self.assertIn("TOTAL", out)
        self.assertIn("384 vCPU", out)              # 192+192, untagged excluded
        self.assertIn("across 2 AZ(s)", out)        # 1c (untagged) not counted

    def test_empty_says_none(self):
        client = FakeEC2([])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        self.assertIn("no active/pending reservations", "\n".join(cm.output))

    def test_summary_shows_target_and_flags_when_given(self):
        # held: 1b=192(short of 320), 1d=384(>=320 FULL); total 576 of 640 short
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),   # 192
            _reservation("i4i.16xlarge", "us-east-1d", 6),   # 384
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client, target_cores=640, per_az_cores=320)
        out = "\n".join(cm.output)
        self.assertIn("192 / 320 vCPU", out)   # 1b progress shown
        self.assertIn("[short]", out)          # 1b under cap
        self.assertIn("384 / 320 vCPU", out)   # 1d progress
        self.assertIn("[FULL]", out)           # 1d at/over cap
        self.assertIn("576 / 640 vCPU", out)   # total progress

    def test_summary_no_target_and_no_ledger_keeps_plain_format(self):
        # point ledger at an empty/missing file so no target is auto-read
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(grab_odcr, "GRAB_LEDGER",
                                   os.path.join(d, "nope.jsonl")):
                client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 1)])
                with self.assertLogs("i4i-grab", level="INFO") as cm:
                    print_list(client)                 # no targets, no ledger
        out = "\n".join(cm.output)
        # no "held / target vCPU" progress and no FULL/short flag
        self.assertNotIn("/ 1 vCPU", out)
        self.assertNotIn("vCPU [", out)
        self.assertNotIn("[FULL]", out)
        self.assertNotIn("[short]", out)

    def test_used_column_and_used_total_summary(self):
        # 3 ours: 2 occupied (available<count), 1 free; + 1 not-ours (ignored)
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 1, available=0),  # USED
            _reservation("i4i.16xlarge", "us-east-1b", 1, available=0),  # USED
            _reservation("i4i.16xlarge", "us-east-1d", 1, available=1),  # free
            _reservation("i4i.16xlarge", "us-east-1c", 1, available=0, tag="x"),  # not ours
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        out = "\n".join(cm.output)
        self.assertIn("USED", out)                       # per-row + summary label
        self.assertIn("free", out)                       # the unoccupied one
        # summary counts only OUR tagged: 2 used out of 3
        self.assertIn("2 / 3 reservations USED", out)

    def test_list_auto_reads_target_from_ledger(self):
        # plain print_list() with NO target args should read target from the
        # last grabs.jsonl line — so `--list` alone shows progress.
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "grabs.jsonl")
            with open(ledger, "w") as f:
                f.write('{"target_vcpu":10000,"per_az_cores":5000}\n')
            with mock.patch.object(grab_odcr, "GRAB_LEDGER", ledger):
                client = FakeEC2([
                    _reservation("i4i.16xlarge", "us-east-1b", 6),  # 384
                    _reservation("i4i.16xlarge", "us-east-1d", 6),  # 384
                ])
                with self.assertLogs("i4i-grab", level="INFO") as cm:
                    print_list(client)                 # NO args passed
        out = "\n".join(cm.output)
        self.assertIn("384 / 5000 vCPU", out)   # per-AZ target auto-read
        self.assertIn("768 / 10000 vCPU", out)  # total target auto-read


class AzFull(unittest.TestCase):
    def test_full_at_cap(self):
        self.assertTrue(_az_full(_args(per_az_cores=5000),
                                 {"us-east-1b": 5000}, "us-east-1b"))

    def test_not_full_below_cap(self):
        self.assertFalse(_az_full(_args(per_az_cores=5000),
                                  {"us-east-1b": 2000}, "us-east-1b"))

    def test_unset_per_az_never_full(self):
        self.assertFalse(_az_full(_args(per_az_cores=None),
                                  {"us-east-1b": 999999}, "us-east-1b"))


class SweepGranularity(unittest.TestCase):
    """Pin the real contract: one sweep grabs at most ONE per AZ per type."""
    def setUp(self):
        self._orig = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig

    def test_single_sweep_grabs_one_per_az(self):
        held = {}
        args = _args(per_az_cores=5000, target_cores=10000)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2()
        sweep_once(client, args, azs, offered, held, [])
        # exactly one grab in each AZ — the watch loop is what accumulates more
        self.assertEqual(sorted(client.created),
                         [("i4i.16xlarge", "us-east-1b"),
                          ("i4i.16xlarge", "us-east-1d")])
        self.assertEqual(held, {"us-east-1b": V16, "us-east-1d": V16})


class ResumeBehavior(unittest.TestCase):
    """The core of the change: a restart must resume per-AZ correctly.
    Driven through _drain (the watch loop) since one sweep only grabs 1/AZ."""

    def setUp(self):
        self._orig = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig

    def test_restart_skips_full_az_and_tops_up_short_one(self):
        cap = 5 * V16                                  # 320 (divisible by 64)
        held = {"us-east-1b": cap, "us-east-1d": 2 * V16}  # 1b full, 1d=128
        args = _args(per_az_cores=cap, target_cores=2 * cap)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2()

        _drain(client, args, azs, offered, held)

        # 1b was already at cap -> ZERO new reservations there.
        self.assertEqual([az for _t, az in client.created if az == "us-east-1b"], [])
        # 1d topped from 128 to 320 -> exactly 3 new (3*64=192).
        self.assertEqual(
            len([az for _t, az in client.created if az == "us-east-1d"]), 3)
        self.assertEqual(held, {"us-east-1b": cap, "us-east-1d": cap})

    def test_fresh_start_fills_both_azs_evenly(self):
        cap = 3 * V16                                  # 192
        held = {}
        args = _args(per_az_cores=cap, target_cores=2 * cap)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2()
        _drain(client, args, azs, offered, held)
        self.assertEqual(held, {"us-east-1b": cap, "us-east-1d": cap})
        self.assertEqual(len(client.created), 6)        # 3 per AZ

    def test_already_at_target_does_nothing(self):
        cap = 5 * V16
        held = {"us-east-1b": cap, "us-east-1d": cap}
        args = _args(per_az_cores=cap, target_cores=2 * cap)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2()
        _drain(client, args, azs, offered, held)
        self.assertEqual(client.created, [])            # not a single new grab

    def test_no_capacity_in_one_az_does_not_crash_or_overshoot(self):
        cap = 2 * V16                                  # 128
        held = {}
        args = _args(per_az_cores=cap, target_cores=2 * cap)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2(no_capacity={"us-east-1b"})
        _drain(client, args, azs, offered, held)
        self.assertNotIn("us-east-1b", held)            # never grabbed in 1b
        self.assertEqual(held["us-east-1d"], cap)       # 1d filled to cap

    def test_per_az_cap_is_hard_per_az_even_if_other_az_dry(self):
        # 1b dry, target=2*cap. Must NOT overflow 1d past its per-AZ cap to
        # make up the global target. per-AZ cap wins; we stay short overall.
        cap = 2 * V16
        held = {}
        args = _args(per_az_cores=cap, target_cores=2 * cap)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2(no_capacity={"us-east-1b"})
        _drain(client, args, azs, offered, held)
        self.assertEqual(held["us-east-1d"], cap)       # capped, not 2*cap
        self.assertLess(sum(held.values()), args.target_cores)  # stays short

    def test_non_divisible_cap_overshoots_by_at_most_one_instance(self):
        # Gate checks held>=cap BEFORE reserving, so the last grab can push a
        # bit over a cap not divisible by 64. Pin this so it's intentional.
        held = {}
        args = _args(per_az_cores=200, target_cores=200)
        azs = ["us-east-1b"]
        offered = {("i4i.16xlarge", "us-east-1b")}
        client = FakeEC2()
        _drain(client, args, azs, offered, held)
        # 64,128,192 (<200, grab) -> 256 (>=200, stop). ends at 256, 4 grabs.
        self.assertEqual(held["us-east-1b"], 256)
        self.assertEqual(len(client.created), 4)
        self.assertLess(held["us-east-1b"] - 200, V16)  # overshoot < 1 instance


class DryRunPlan(unittest.TestCase):
    def setUp(self):
        self._orig = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig

    def test_dry_run_simulates_plan_without_real_reservations(self):
        cap = 2 * V16
        held = {}
        args = _args(per_az_cores=cap, target_cores=2 * cap, live=False)
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        client = FakeEC2()
        _drain(client, args, azs, offered, held)
        # plan respects caps in-memory...
        self.assertEqual(held, {"us-east-1b": cap, "us-east-1d": cap})
        # ...but NOT one real reservation was created.
        self.assertEqual(client.created, [])


class ReserveOne(unittest.TestCase):
    """The single CreateCapacityReservation call — pin the exact params that
    make an OPEN, Linux/UNIX, default-tenancy, count=1 reservation (the 4
    attributes an ASG must match), plus dry-run and end-hours behavior."""

    def test_open_linux_default_count1_tagged(self):
        client = FakeEC2()
        reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=False)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["InstanceType"], "i4i.16xlarge")
        self.assertEqual(kw["InstancePlatform"], "Linux/UNIX")
        self.assertEqual(kw["AvailabilityZone"], "us-east-1b")
        self.assertEqual(kw["InstanceCount"], 1)
        self.assertEqual(kw["InstanceMatchCriteria"], "open")
        self.assertEqual(kw["Tenancy"], "default")
        self.assertEqual(kw["DryRun"], False)
        # i4i is Nitro (always EBS-optimized); reservation marked to match.
        self.assertEqual(kw["EbsOptimized"], True)
        # tagged so --list / --cancel-all / held_cores can find it
        tags = kw["TagSpecifications"][0]["Tags"]
        self.assertIn({"Key": TAG_KEY, "Value": TAG_VAL}, tags)

    def test_dry_run_flag_passes_through(self):
        client = FakeEC2()
        with self.assertRaises(ClientError):     # FakeEC2 raises DryRunOperation
            reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=True)
        self.assertTrue(client.create_kwargs[-1]["DryRun"])

    def test_no_end_hours_is_unlimited(self):
        client = FakeEC2()
        reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=False)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["EndDateType"], "unlimited")
        self.assertNotIn("EndDate", kw)

    def test_end_hours_sets_limited_with_future_enddate(self):
        client = FakeEC2()
        reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=False,
                    end_hours=6)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["EndDateType"], "limited")
        self.assertIn("EndDate", kw)
        self.assertGreater(kw["EndDate"], datetime.datetime.utcnow())


class ListReservations(unittest.TestCase):
    def test_parses_rows_and_tag(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),
            _reservation("i4i.8xlarge", "us-east-1d", 1, tag="other"),
        ])
        rows = list_reservations(client)
        self.assertEqual(len(rows), 2)
        crid, itype, az, state, cnt, tag, avail = rows[0]
        self.assertEqual(itype, "i4i.16xlarge")
        self.assertEqual(az, "us-east-1b")
        self.assertEqual(state, "active")
        self.assertEqual(cnt, 3)
        self.assertEqual(tag, TAG_VAL)
        self.assertEqual(avail, 3)                     # available slots (7th field)
        self.assertEqual(rows[1][5], "other")          # tag passthrough

    def test_untagged_reservation_yields_empty_tag(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 1, tag=None)])
        self.assertEqual(list_reservations(client)[0][5], "")


class CancelAll(unittest.TestCase):
    def test_only_cancels_our_tagged_reservations(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),              # ours
            _reservation("i4i.16xlarge", "us-east-1d", 2),              # ours
            _reservation("i4i.16xlarge", "us-east-1c", 9, tag="other"),  # NOT ours
            _reservation("i4i.16xlarge", "us-east-1a", 9, tag=None),     # NOT ours
        ])
        cancel_all(client, dry_run=False)
        # exactly the two tagged ones cancelled, others untouched
        self.assertEqual(len(client.cancelled), 2)

    def test_dry_run_cancels_nothing(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 3)])
        cancel_all(client, dry_run=True)
        self.assertEqual(client.cancelled, [])

    def test_nothing_tagged_is_noop(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 3, tag="other")])
        cancel_all(client, dry_run=False)
        self.assertEqual(client.cancelled, [])


class RunFake(FakeEC2):
    """FakeEC2 plus the describe_* calls run() needs end-to-end, so we can
    drive run() with an arbitrary (non-i4i) instance type and prove it is
    learned from AWS and then swept — the whole point of the feature."""

    def __init__(self, azs=("us-east-1b", "us-east-1d"), vcpus=None,
                 offered=None, **kw):
        super().__init__(**kw)
        self._azs = list(azs)
        self._vcpus = dict(vcpus or {})          # type -> DefaultVCpus
        # which (type, az) are offered; default: every requested type in every AZ
        self._offered = offered
        self.described_types = []                # types passed to DescribeInstanceTypes

    def describe_availability_zones(self, **kwargs):
        return {"AvailabilityZones": [{"ZoneName": z} for z in self._azs]}

    def describe_instance_types(self, **kwargs):
        req = kwargs.get("InstanceTypes", [])
        self.described_types.append(list(req))
        return {"InstanceTypes": [
            {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": self._vcpus[t]}}
            for t in req if t in self._vcpus
        ]}

    def describe_instance_type_offerings(self, **kwargs):
        types = kwargs["Filters"][0]["Values"]
        if self._offered is not None:
            offs = [{"InstanceType": t, "Location": az}
                    for (t, az) in self._offered if t in types]
        else:
            offs = [{"InstanceType": t, "Location": az}
                    for t in types for az in self._azs]
        return {"InstanceTypeOfferings": offs}


class RunLearnsCustomType(unittest.TestCase):
    """run() must accept a machine type that is NOT in the static VCPU table:
    look its vCPU up from AWS, then sweep/reserve it like any i4i size."""

    def setUp(self):
        self._orig_vcpu = dict(VCPU)
        self._orig_rec = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig_rec
        VCPU.clear()
        VCPU.update(self._orig_vcpu)

    def test_run_grabs_arbitrary_type_after_learning_vcpu(self):
        self.assertNotIn("r7i.48xlarge", VCPU)   # truly unknown up front
        client = RunFake(
            azs=["us-east-1b", "us-east-1d"],
            vcpus={"r7i.48xlarge": 192},
        )
        args = _args(types=["r7i.48xlarge"], per_az_cores=192,
                     target_cores=384, live=True, watch=False,
                     interval=0, list=False, cancel_all=False, azs=None)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        # learned the vCPU from AWS...
        self.assertEqual(VCPU["r7i.48xlarge"], 192)
        self.assertIn(["r7i.48xlarge"], client.described_types)
        # ...and actually reserved it in both AZs (1 per AZ in a non-watch run)
        self.assertEqual(sorted(client.created),
                         [("r7i.48xlarge", "us-east-1b"),
                          ("r7i.48xlarge", "us-east-1d")])

    def test_run_aborts_when_type_unresolvable(self):
        # AWS knows nothing about the type -> after dropping it there is
        # nothing to sweep, so we must NOT create any reservation.
        client = RunFake(azs=["us-east-1b"], vcpus={})  # describe returns []
        args = _args(types=["totally.bogus"], per_az_cores=None,
                     target_cores=8, live=True, watch=False,
                     interval=0, list=False, cancel_all=False, azs=None)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(client.created, [])


class ParsePerType(unittest.TestCase):
    def test_parses_type_count_pairs_in_order(self):
        targets, errors = parse_per_type(
            ["i4i.16xlarge:10", "i4i.8xlarge:5", "r7i.24xlarge:3"])
        self.assertEqual(errors, [])
        self.assertEqual(targets,
                         {"i4i.16xlarge": 10, "i4i.8xlarge": 5, "r7i.24xlarge": 3})
        self.assertEqual(list(targets), ["i4i.16xlarge", "i4i.8xlarge", "r7i.24xlarge"])

    def test_none_and_empty_yield_empty(self):
        self.assertEqual(parse_per_type(None), ({}, []))
        self.assertEqual(parse_per_type([]), ({}, []))

    def test_missing_colon_is_error(self):
        targets, errors = parse_per_type(["i4i.16xlarge"])
        self.assertEqual(targets, {})
        self.assertEqual(len(errors), 1)

    def test_non_integer_count_is_error(self):
        targets, errors = parse_per_type(["i4i.16xlarge:lots"])
        self.assertEqual(targets, {})
        self.assertEqual(len(errors), 1)

    def test_non_positive_count_is_error(self):
        _t0, e0 = parse_per_type(["i4i.16xlarge:0"])
        _t1, e1 = parse_per_type(["i4i.16xlarge:-3"])
        self.assertEqual(len(e0), 1)
        self.assertEqual(len(e1), 1)

    def test_empty_type_is_error(self):
        targets, errors = parse_per_type([":5"])
        self.assertEqual(targets, {})
        self.assertEqual(len(errors), 1)

    def test_duplicate_type_last_write_wins(self):
        targets, errors = parse_per_type(["i4i.16xlarge:2", "i4i.16xlarge:9"])
        self.assertEqual(errors, [])
        self.assertEqual(targets, {"i4i.16xlarge": 9})

    def test_size_with_colon_only_splits_on_last(self):
        # rpartition on the LAST colon, so a stray type is still parsed sanely
        targets, errors = parse_per_type(["i4i.16xlarge:10"])
        self.assertEqual(targets, {"i4i.16xlarge": 10})
        self.assertEqual(errors, [])


class HeldCountByType(unittest.TestCase):
    def test_counts_instances_not_cores_or_objects(self):
        # two reservations of the same type: 3 + 2 = 5 INSTANCES (not cores)
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),
            _reservation("i4i.16xlarge", "us-east-1d", 2),
            _reservation("i4i.8xlarge", "us-east-1b", 4),
        ])
        held = held_count_by_type(client, {"i4i.16xlarge": 99, "i4i.8xlarge": 99})
        self.assertEqual(held, {"i4i.16xlarge": 5, "i4i.8xlarge": 4})

    def test_ignores_types_not_requested(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),
            _reservation("i4i.8xlarge", "us-east-1b", 4),   # not in requested set
        ])
        self.assertEqual(held_count_by_type(client, {"i4i.16xlarge": 99}),
                         {"i4i.16xlarge": 3})

    def test_ignores_untagged_and_other_tags(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3, tag=None),
            _reservation("i4i.16xlarge", "us-east-1b", 2, tag="other"),
            _reservation("i4i.16xlarge", "us-east-1b", 1),   # ours
        ])
        self.assertEqual(held_count_by_type(client, {"i4i.16xlarge": 99}),
                         {"i4i.16xlarge": 1})


def _pt_args(per_type, **over):
    base = dict(region="us-east-1", per_type=dict(per_type), live=True,
                end_hours=None)
    base.update(over)
    return Namespace(**base)


class SweepOncePerType(unittest.TestCase):
    """Per-type sweep: each type reaches its OWN instance target; a dry type
    never blocks another; no shared gate, no per-AZ cap."""

    def setUp(self):
        self._orig = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig

    def test_each_type_hits_its_own_count(self):
        args = _pt_args({"i4i.16xlarge": 3, "i4i.8xlarge": 2})
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d"),
                   ("i4i.8xlarge", "us-east-1b"), ("i4i.8xlarge", "us-east-1d")}
        held = {}
        client = FakeEC2()
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held, {"i4i.16xlarge": 3, "i4i.8xlarge": 2})
        self.assertEqual(len([t for t, _a in client.created if t == "i4i.16xlarge"]), 3)
        self.assertEqual(len([t for t, _a in client.created if t == "i4i.8xlarge"]), 2)

    def test_dry_type_does_not_block_others(self):
        # i4i.16xlarge has NO capacity anywhere; i4i.8xlarge still fills fully.
        # Type-level dryness: FakeEC2.no_capacity is per-AZ, so use a subclass
        # that fails only for the one type.
        class TypeDryEC2(FakeEC2):
            def create_capacity_reservation(self, **kw):
                if kw["InstanceType"] == "i4i.16xlarge":
                    raise _cap_error("InsufficientInstanceCapacity")
                return super().create_capacity_reservation(**kw)

        args = _pt_args({"i4i.16xlarge": 5, "i4i.8xlarge": 4})
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d"),
                   ("i4i.8xlarge", "us-east-1b"), ("i4i.8xlarge", "us-east-1d")}
        held = {}
        client = TypeDryEC2()
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held.get("i4i.16xlarge", 0), 0)   # dry, stayed empty
        self.assertEqual(held.get("i4i.8xlarge"), 4)       # filled anyway

    def test_capacity_exhausted_does_not_infinite_loop(self):
        # target 5 but NO capacity at all -> must return, not spin forever.
        args = _pt_args({"i4i.16xlarge": 5})
        azs = ["us-east-1b"]
        offered = {("i4i.16xlarge", "us-east-1b")}
        held = {}
        client = FakeEC2(no_capacity={"us-east-1b"})
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held.get("i4i.16xlarge", 0), 0)
        self.assertEqual(client.created, [])

    def test_offered_filter_skips_unavailable_type_az(self):
        # 8xlarge only offered in 1d; 16xlarge only in 1b.
        args = _pt_args({"i4i.16xlarge": 2, "i4i.8xlarge": 2})
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.8xlarge", "us-east-1d")}
        held = {}
        client = FakeEC2()
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held, {"i4i.16xlarge": 2, "i4i.8xlarge": 2})
        # every 16xlarge grab landed in 1b, every 8xlarge in 1d
        self.assertTrue(all(a == "us-east-1b"
                            for t, a in client.created if t == "i4i.16xlarge"))
        self.assertTrue(all(a == "us-east-1d"
                            for t, a in client.created if t == "i4i.8xlarge"))

    def test_resume_tops_up_only_the_shortfall(self):
        # already hold 2 of a target-3 type -> exactly ONE new reservation.
        args = _pt_args({"i4i.16xlarge": 3})
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        held = {"i4i.16xlarge": 2}
        client = FakeEC2()
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held["i4i.16xlarge"], 3)
        self.assertEqual(len(client.created), 1)


class RunPerType(unittest.TestCase):
    """End-to-end run() in per-type mode, including learning a non-i4i type."""

    def setUp(self):
        self._orig_vcpu = dict(VCPU)
        self._orig_rec = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig_rec
        VCPU.clear()
        VCPU.update(self._orig_vcpu)

    def _args(self, per_type, **over):
        base = dict(region="us-east-1", per_type=dict(per_type), types=None,
                    target_cores=8, per_az_cores=None, azs=None, live=True,
                    watch=False, interval=0, end_hours=None, list=False,
                    cancel_all=False)
        base.update(over)
        return Namespace(**base)

    def test_run_per_type_reserves_each_type(self):
        client = RunFake(azs=["us-east-1b", "us-east-1d"], vcpus={})
        args = self._args({"i4i.16xlarge": 2, "i4i.8xlarge": 1})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(len([t for t, _a in client.created if t == "i4i.16xlarge"]), 2)
        self.assertEqual(len([t for t, _a in client.created if t == "i4i.8xlarge"]), 1)

    def test_run_per_type_learns_custom_type_vcpu(self):
        self.assertNotIn("r7i.24xlarge", VCPU)
        client = RunFake(azs=["us-east-1b"], vcpus={"r7i.24xlarge": 96})
        args = self._args({"r7i.24xlarge": 2})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(VCPU["r7i.24xlarge"], 96)
        self.assertEqual(len(client.created), 2)

    def test_run_per_type_drops_unresolvable_type(self):
        # AWS knows nothing -> type dropped, nothing reserved, no crash.
        client = RunFake(azs=["us-east-1b"], vcpus={})
        args = self._args({"totally.bogus": 3})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(client.created, [])

    def test_run_per_type_honors_azs_scope(self):
        # Region has 3 AZs but --azs locks the sweep to two of them: no grab
        # may land in the excluded AZ. Target is a per-type TOTAL, filled
        # across only the in-scope AZs (matches the chosen semantics).
        client = RunFake(azs=["us-east-1b", "us-east-1c", "us-east-1d"], vcpus={})
        args = self._args({"i4i.16xlarge": 3}, azs=["us-east-1b", "us-east-1d"])
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        grabbed_azs = {a for _t, a in client.created}
        self.assertNotIn("us-east-1c", grabbed_azs)          # excluded AZ untouched
        self.assertTrue(grabbed_azs <= {"us-east-1b", "us-east-1d"})
        self.assertEqual(len(client.created), 3)             # total target met

    def test_run_per_type_warns_and_ignores_missing_az(self):
        # An --azs entry not present in the region is ignored (warned), the
        # valid one still works, nothing crashes.
        client = RunFake(azs=["us-east-1b"], vcpus={})
        args = self._args({"i4i.16xlarge": 2},
                          azs=["us-east-1b", "us-west-2a"])   # 2nd doesn't exist
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual({a for _t, a in client.created}, {"us-east-1b"})
        self.assertEqual(len(client.created), 2)


if __name__ == "__main__":
    unittest.main()
