#!/usr/bin/env python3
"""Unit tests for grab_odcr.py — focused on the restart-idempotency logic.

These tests mock boto3 entirely (no AWS calls, no cost, runs in CI). They pin
the behavior that the real Smoke Tests in SMOKE_TEST.md do NOT cover: the
pure-Python core that makes a crash/restart safe —

  * held_cores_by_az(): count CORES (TotalInstanceCount x vCPU), not objects
  * _az_full(): per-AZ cap judged against cores actually held
  * sweep_once(): ONE grab per AZ per pass; the --watch loop repeats sweeps to
    fill up. After a restart, full AZs are skipped and only the short ones get
    topped up — never double-grab a full AZ, never go lopsided.

Run:  python3 -m unittest test_grab_odcr -v
"""
import logging
import unittest
from argparse import Namespace

from botocore.exceptions import ClientError

import grab_odcr
from grab_odcr import held_cores_by_az, _az_full, sweep_once, TAG_KEY, TAG_VAL
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
        self._n = 0

    def describe_capacity_reservations(self, **kwargs):
        return {"CapacityReservations": self._reservations}

    def create_capacity_reservation(self, **kwargs):
        if kwargs.get("DryRun"):
            raise _cap_error("DryRunOperation")
        az = kwargs["AvailabilityZone"]
        if az in self._no_capacity:
            raise _cap_error("InsufficientInstanceCapacity")
        self._n += 1
        self.created.append((kwargs["InstanceType"], az))
        return {"CapacityReservation": {"CapacityReservationId": "cr-%04d" % self._n}}


def _reservation(itype, az, count, tag=TAG_VAL, state="active"):
    r = {
        "CapacityReservationId": "cr-existing",
        "InstanceType": itype,
        "AvailabilityZone": az,
        "State": state,
        "TotalInstanceCount": count,
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


if __name__ == "__main__":
    unittest.main()
