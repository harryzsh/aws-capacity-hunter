#!/usr/bin/env python3
"""Unit tests for grab_odcr.py — instance-count (per-type) grabbing only.

These tests mock boto3 entirely (no AWS calls, no cost, runs in CI). They pin
the pure-Python core that makes a crash/restart safe and the account tidy:

  * held_count_by_type(): count INSTANCES per type from AWS truth.
  * sweep_once_per_type(): each type reaches its OWN instance target; a dry
    type never blocks another; restart tops up only the real shortfall.
  * growable_map() / secure_one(): grow-or-create — ONE reservation object
    per (type, az), grown by +1, created only the first time.
  * print_list(): --list prints a per-type INSTANCE summary (optional targets,
    auto-read from the ledger) + per-reservation USED/free + used/total tally.
  * reserve_one() / list_reservations() / cancel_all(): the ODCR API wrappers.

Run:  python3 -m unittest test_grab_odcr -v
"""
import logging
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from botocore.exceptions import ClientError

import grab_odcr
from grab_odcr import (
    print_list, reserve_one, list_reservations, cancel_all, TAG_KEY, TAG_VAL,
    parse_per_type, held_count_by_type, sweep_once_per_type,
    growable_map, secure_one,
)
from common import VCPU

# Quiet the script's INFO chatter during tests; we assert on state, not logs.
logging.getLogger("i4i-grab").setLevel(logging.CRITICAL)


def _cap_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}},
                       "CreateCapacityReservation")


class FakeEC2:
    """Minimal stand-in for a boto3 EC2 client.

    STATEFUL like real EC2: create appends to the reservations that
    describe_capacity_reservations returns, and modify updates the count in
    place — so growable_map() sees what a sweep created, exactly as on AWS.
    Capacity errors: an AZ in `no_capacity` fails BOTH create and modify with
    InsufficientInstanceCapacity. DryRun=True always raises DryRunOperation.
    """
    def __init__(self, reservations=None, no_capacity=()):
        self._reservations = reservations or []
        self._no_capacity = set(no_capacity)
        self.created = []          # list of (itype, az)
        self.create_kwargs = []    # full kwargs of each create call
        self.modified = []         # list of (crid, new_count) — one per grow
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
        crid = "cr-%04d" % self._n
        self.created.append((kwargs["InstanceType"], az))
        tags = []
        for spec in kwargs.get("TagSpecifications", []):
            tags.extend(spec.get("Tags", []))
        count = kwargs.get("InstanceCount", 1)
        self._reservations.append({
            "CapacityReservationId": crid,
            "InstanceType": kwargs["InstanceType"],
            "AvailabilityZone": az,
            "State": "active",
            "TotalInstanceCount": count,
            "AvailableInstanceCount": count,
            "Tags": tags,
        })
        return {"CapacityReservation": {"CapacityReservationId": crid}}

    def modify_capacity_reservation(self, CapacityReservationId=None,
                                    InstanceCount=None, DryRun=False):
        if DryRun:
            raise _cap_error("DryRunOperation")
        for r in self._reservations:
            if r["CapacityReservationId"] == CapacityReservationId:
                if r["AvailabilityZone"] in self._no_capacity:
                    raise _cap_error("InsufficientInstanceCapacity")
                r["TotalInstanceCount"] = InstanceCount
                self.modified.append((CapacityReservationId, InstanceCount))
                return {"Return": True}
        raise _cap_error("InvalidCapacityReservationId.NotFound")

    def cancel_capacity_reservation(self, CapacityReservationId=None):
        self.cancelled.append(CapacityReservationId)
        return {}


def _added(client, itype=None, az=None):
    """Instances secured = creates + grows (each modify call is a +1 grow),
    optionally filtered by instance type and/or AZ."""
    def keep(t, a):
        return (itype is None or t == itype) and (az is None or a == az)
    creates = [1 for t, a in client.created if keep(t, a)]
    by_id = {r["CapacityReservationId"]: (r["InstanceType"], r["AvailabilityZone"])
             for r in client._reservations}
    grows = [1 for c, _n in client.modified
             if c in by_id and keep(*by_id[c])]
    return len(creates) + len(grows)


def _reservation(itype, az, count, tag=TAG_VAL, state="active", available=None,
                 crid="cr-existing"):
    # available defaults to count (all free / unused). Pass available<count to
    # simulate a reservation that has instances in it (USED).
    r = {
        "CapacityReservationId": crid,
        "InstanceType": itype,
        "AvailabilityZone": az,
        "State": state,
        "TotalInstanceCount": count,
        "AvailableInstanceCount": count if available is None else available,
    }
    if tag is not None:
        r["Tags"] = [{"Key": TAG_KEY, "Value": tag}]
    return r


def _pt_args(per_type, **over):
    base = dict(region="us-east-1", per_type=dict(per_type), live=True)
    base.update(over)
    return Namespace(**base)


class PrintListSummary(unittest.TestCase):
    """--list must print a per-type + total INSTANCE summary (not just rows)."""

    def setUp(self):
        # isolate from any real logs/grabs.jsonl on this machine
        self._tmp = tempfile.TemporaryDirectory()
        self._p = mock.patch.object(
            grab_odcr, "GRAB_LEDGER", os.path.join(self._tmp.name, "none.jsonl"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_summary_logs_per_type_instances_and_total(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),
            _reservation("i4i.16xlarge", "us-east-1d", 2),   # 16xl total 5
            _reservation("i4i.8xlarge", "us-east-1b", 4),
            _reservation("i4i.16xlarge", "us-east-1c", 9, tag=None),  # not ours
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        out = "\n".join(cm.output)
        self.assertIn("i4i.16xlarge", out)
        self.assertIn("5 instances", out)           # per-type count shown
        self.assertIn("4 instances", out)
        self.assertIn("TOTAL", out)
        self.assertIn("9 instances", out)           # 5+4, untagged excluded
        self.assertIn("across 2 type(s)", out)      # 1c (untagged) not counted

    def test_empty_says_none(self):
        client = FakeEC2([])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        self.assertIn("no active/pending reservations", "\n".join(cm.output))

    def test_summary_shows_target_and_flags_when_given(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 5),   # 5 of 10 -> short
            _reservation("i4i.8xlarge", "us-east-1d", 4),    # 4 of 4 -> FULL
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client, targets={"i4i.16xlarge": 10, "i4i.8xlarge": 4})
        out = "\n".join(cm.output)
        self.assertIn("5 / 10 instances", out)
        self.assertIn("[short]", out)
        self.assertIn("4 / 4 instances", out)
        self.assertIn("[FULL]", out)

    def test_no_target_and_no_ledger_keeps_plain_format(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 1)])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)                 # no targets, no ledger
        out = "\n".join(cm.output)
        self.assertNotIn("/ ", out.split("--- summary")[-1].split("USED")[0]
                         .replace("(", ""))    # no "held / target" progress
        self.assertNotIn("[FULL]", out)
        self.assertNotIn("[short]", out)

    def test_used_column_and_used_total_summary(self):
        # 3 ours: 2 occupied (available<count), 1 free; + 1 not-ours (ignored)
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 1, available=0),  # USED
            _reservation("i4i.16xlarge", "us-east-1b", 1, available=0),  # USED
            _reservation("i4i.16xlarge", "us-east-1d", 1, available=1),  # free
            _reservation("i4i.16xlarge", "us-east-1c", 1, available=0, tag="x"),
        ])
        with self.assertLogs("i4i-grab", level="INFO") as cm:
            print_list(client)
        out = "\n".join(cm.output)
        self.assertIn("USED", out)                       # per-row + summary label
        self.assertIn("free", out)                       # the unoccupied one
        # summary counts only OUR tagged: 2 used out of 3
        self.assertIn("2 / 3 reservations USED", out)

    def test_list_auto_reads_targets_from_ledger(self):
        # plain print_list() with NO targets should read per-type targets from
        # grabs.jsonl (latest line per type wins) — `--list` alone shows progress.
        ledger = os.path.join(self._tmp.name, "grabs.jsonl")
        with open(ledger, "w") as f:
            f.write('{"instance_type":"i4i.16xlarge","target_count":10}\n')
            f.write('{"instance_type":"i4i.8xlarge","target_count":9}\n')
            f.write('{"instance_type":"i4i.8xlarge","target_count":4}\n')  # latest wins
        with mock.patch.object(grab_odcr, "GRAB_LEDGER", ledger):
            client = FakeEC2([
                _reservation("i4i.16xlarge", "us-east-1b", 5),
                _reservation("i4i.8xlarge", "us-east-1d", 4),
            ])
            with self.assertLogs("i4i-grab", level="INFO") as cm:
                print_list(client)                 # NO targets passed
        out = "\n".join(cm.output)
        self.assertIn("5 / 10 instances", out)
        self.assertIn("4 / 4 instances", out)
        self.assertIn("[FULL]", out)

    def test_old_core_denominated_ledger_is_ignored(self):
        # a ledger written by the old core-count version has target_vcpu, not
        # target_count -> no progress shown, but no crash either.
        ledger = os.path.join(self._tmp.name, "grabs.jsonl")
        with open(ledger, "w") as f:
            f.write('{"instance_type":"i4i.16xlarge","target_vcpu":10000,'
                    '"per_az_cores":5000}\n')
        with mock.patch.object(grab_odcr, "GRAB_LEDGER", ledger):
            client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 5)])
            with self.assertLogs("i4i-grab", level="INFO") as cm:
                print_list(client)
        out = "\n".join(cm.output)
        self.assertIn("5 instances", out)
        self.assertNotIn("[short]", out)
        self.assertNotIn("[FULL]", out)


class ReserveOne(unittest.TestCase):
    """The single CreateCapacityReservation call — pin the exact params that
    make an OPEN, Linux/UNIX, default-tenancy, count=1 reservation (the 4
    attributes an ASG must match), plus dry-run behavior."""

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
        # tagged so --list / --cancel-all / held counts can find it
        tags = kw["TagSpecifications"][0]["Tags"]
        self.assertIn({"Key": TAG_KEY, "Value": TAG_VAL}, tags)

    def test_dry_run_flag_passes_through(self):
        client = FakeEC2()
        with self.assertRaises(ClientError):     # FakeEC2 raises DryRunOperation
            reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=True)
        self.assertTrue(client.create_kwargs[-1]["DryRun"])

    def test_always_unlimited_no_end_date(self):
        client = FakeEC2()
        reserve_one(client, "i4i.16xlarge", "us-east-1b", dry_run=False)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["EndDateType"], "unlimited")
        self.assertNotIn("EndDate", kw)


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
        # two reservations of the same type: 3 + 2 = 5 INSTANCES (not objects)
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

    def test_only_azs_filters_out_of_scope_stock(self):
        # The --azs scope bug: targeting only 1b must NOT count 1a's stock,
        # else 1a inflates the gate and stops the run before 1b gets any.
        client = FakeEC2([
            _reservation("i7i.8xlarge", "us-east-1a", 24),   # out of scope
            _reservation("i7i.8xlarge", "us-east-1b", 3),    # in scope
        ])
        # no filter -> counts both AZs
        self.assertEqual(held_count_by_type(client, {"i7i.8xlarge": 99}),
                         {"i7i.8xlarge": 27})
        # scoped to 1b -> 1a's 24 excluded
        self.assertEqual(
            held_count_by_type(client, {"i7i.8xlarge": 99},
                               only_azs={"us-east-1b"}),
            {"i7i.8xlarge": 3})


class GrowableMap(unittest.TestCase):
    """growable_map(): {(type, az): [crid, count]} of OUR active reservations —
    the grow targets that let a sweep consolidate instead of creating."""

    def test_maps_tagged_active_reservations(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 3),
            _reservation("i4i.8xlarge", "us-east-1d", 1),
        ])
        m = growable_map(client)
        self.assertEqual(m[("i4i.16xlarge", "us-east-1b")], ["cr-existing", 3])
        self.assertEqual(m[("i4i.8xlarge", "us-east-1d")], ["cr-existing", 1])

    def test_ignores_untagged_other_tag_and_non_active(self):
        client = FakeEC2([
            _reservation("i4i.16xlarge", "us-east-1b", 1, tag=None),
            _reservation("i4i.16xlarge", "us-east-1b", 1, tag="other"),
            _reservation("i4i.16xlarge", "us-east-1d", 1, state="pending"),
        ])
        self.assertEqual(growable_map(client), {})


class SecureOne(unittest.TestCase):
    """secure_one(): grow an existing (type, az) reservation by +1, create only
    when we hold none — the object-count consolidation at the heart of this."""

    def test_grows_existing_instead_of_creating(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 3)])
        growable = {("i4i.16xlarge", "us-east-1b"): ["cr-existing", 3]}
        crid = secure_one(client, "i4i.16xlarge", "us-east-1b",
                          dry_run=False, growable=growable)
        self.assertEqual(crid, "cr-existing")
        self.assertEqual(client.modified, [("cr-existing", 4)])  # absolute 3+1
        self.assertEqual(client.created, [])                     # no new object
        # local map bumped so the NEXT grab this sweep grows to 5, not 4 again
        self.assertEqual(growable[("i4i.16xlarge", "us-east-1b")][1], 4)

    def test_creates_and_registers_when_absent(self):
        client = FakeEC2()
        growable = {}
        crid = secure_one(client, "i4i.16xlarge", "us-east-1b",
                          dry_run=False, growable=growable)
        self.assertEqual(client.created, [("i4i.16xlarge", "us-east-1b")])
        self.assertEqual(client.modified, [])
        # registered: the next grab in this same sweep will GROW this crid
        self.assertEqual(growable[("i4i.16xlarge", "us-east-1b")], [crid, 1])

    def test_reservation_cancelled_elsewhere_falls_back_to_create(self):
        client = FakeEC2()                      # crid not in fake's store
        growable = {("i4i.16xlarge", "us-east-1b"): ["cr-gone", 2]}
        crid = secure_one(client, "i4i.16xlarge", "us-east-1b",
                          dry_run=False, growable=growable)
        self.assertEqual(len(client.created), 1)          # fell back to create
        self.assertNotEqual(crid, "cr-gone")
        # stale entry replaced by the fresh reservation
        self.assertEqual(growable[("i4i.16xlarge", "us-east-1b")], [crid, 1])

    def test_capacity_error_on_grow_propagates_for_classify(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 2)],
                         no_capacity={"us-east-1b"})
        growable = {("i4i.16xlarge", "us-east-1b"): ["cr-existing", 2]}
        with self.assertRaises(ClientError) as ctx:
            secure_one(client, "i4i.16xlarge", "us-east-1b",
                       dry_run=False, growable=growable)
        from common import classify
        self.assertEqual(classify(ctx.exception), "capacity")
        # count NOT bumped — the grow failed
        self.assertEqual(growable[("i4i.16xlarge", "us-east-1b")][1], 2)

    def test_dry_run_takes_create_path_untouched(self):
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1b", 3)])
        growable = {("i4i.16xlarge", "us-east-1b"): ["cr-existing", 3]}
        with self.assertRaises(ClientError):    # DryRunOperation, as before
            secure_one(client, "i4i.16xlarge", "us-east-1b",
                       dry_run=True, growable=growable)
        self.assertEqual(client.modified, [])   # dry-run never modifies
        self.assertEqual(growable[("i4i.16xlarge", "us-east-1b")][1], 3)


class SweepOncePerType(unittest.TestCase):
    """Per-type sweep: each type reaches its OWN instance target; a dry type
    never blocks another; no shared gate."""

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
        self.assertEqual(_added(client, itype="i4i.16xlarge"), 3)
        self.assertEqual(_added(client, itype="i4i.8xlarge"), 2)

    def test_dry_type_does_not_block_others(self):
        # i4i.16xlarge has NO capacity anywhere; i4i.8xlarge still fills fully.
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
        # already hold 2 of a target-3 type -> exactly ONE new instance.
        args = _pt_args({"i4i.16xlarge": 3})
        azs = ["us-east-1b", "us-east-1d"]
        offered = {("i4i.16xlarge", "us-east-1b"), ("i4i.16xlarge", "us-east-1d")}
        held = {"i4i.16xlarge": 2}
        client = FakeEC2()
        sweep_once_per_type(client, args, azs, offered, held, [])
        self.assertEqual(held["i4i.16xlarge"], 3)
        self.assertEqual(_added(client), 1)


class SweepConsolidates(unittest.TestCase):
    """The account-tidiness win: filling a target yields ONE reservation
    object per (type, az) that grows, not a pile of count=1 objects."""

    def setUp(self):
        self._orig = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig

    def test_fill_produces_one_object_per_type_az(self):
        args = _pt_args({"i4i.16xlarge": 3})
        offered = {("i4i.16xlarge", "us-east-1b")}
        held = {}
        client = FakeEC2()
        sweep_once_per_type(client, args, ["us-east-1b"], offered, held, [])
        self.assertEqual(held["i4i.16xlarge"], 3)
        self.assertEqual(len(client._reservations), 1)    # one object, count=3
        self.assertEqual(client._reservations[0]["TotalInstanceCount"], 3)

    def test_restart_grows_existing_object_from_aws(self):
        # after a restart, the sweep must find the PRIOR run's reservation via
        # AWS and grow IT — not start a second object beside it.
        client = FakeEC2([_reservation("i4i.16xlarge", "us-east-1d", 2)])
        args = _pt_args({"i4i.16xlarge": 4})
        held = {"i4i.16xlarge": 2}         # what run() re-read from AWS
        offered = {("i4i.16xlarge", "us-east-1d")}
        sweep_once_per_type(client, args, ["us-east-1d"], offered, held, [])
        self.assertEqual(held["i4i.16xlarge"], 4)
        self.assertEqual(client.created, [])              # zero new objects
        self.assertEqual(len(client._reservations), 1)    # still just one
        self.assertEqual(client._reservations[0]["TotalInstanceCount"], 4)

    def test_dry_run_plans_without_reserving(self):
        args = _pt_args({"i4i.16xlarge": 3}, live=False)
        offered = {("i4i.16xlarge", "us-east-1b")}
        held = {}
        client = FakeEC2()
        sweep_once_per_type(client, args, ["us-east-1b"], offered, held, [])
        # plan counted in-memory...
        self.assertEqual(held["i4i.16xlarge"], 3)
        # ...but NOT one real reservation was created or modified.
        self.assertEqual(client.created, [])
        self.assertEqual(client.modified, [])


class RunFake(FakeEC2):
    """FakeEC2 plus the describe_* calls run() needs end-to-end, so we can
    drive run() with an arbitrary (non-i4i) instance type and prove it is
    learned from AWS and then swept."""

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


class RunPerType(unittest.TestCase):
    """End-to-end run(), including learning a non-i4i type from AWS."""

    def setUp(self):
        self._orig_vcpu = dict(VCPU)
        self._orig_rec = grab_odcr.record_grab
        grab_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_odcr.record_grab = self._orig_rec
        VCPU.clear()
        VCPU.update(self._orig_vcpu)

    def _args(self, per_type, **over):
        base = dict(region="us-east-1", per_type=dict(per_type), azs=None,
                    live=True, watch=False, interval=0, add=False,
                    list=False, cancel_all=False)
        base.update(over)
        return Namespace(**base)

    def test_run_reserves_each_type(self):
        client = RunFake(azs=["us-east-1b", "us-east-1d"], vcpus={})
        args = self._args({"i4i.16xlarge": 2, "i4i.8xlarge": 1})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client, itype="i4i.16xlarge"), 2)
        self.assertEqual(_added(client, itype="i4i.8xlarge"), 1)

    def test_run_learns_custom_type_vcpu(self):
        self.assertNotIn("r7i.24xlarge", VCPU)
        client = RunFake(azs=["us-east-1b"], vcpus={"r7i.24xlarge": 96})
        args = self._args({"r7i.24xlarge": 2})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(VCPU["r7i.24xlarge"], 96)
        self.assertEqual(_added(client), 2)

    def test_run_drops_unresolvable_type(self):
        # AWS knows nothing -> type dropped, nothing reserved, no crash.
        client = RunFake(azs=["us-east-1b"], vcpus={})
        args = self._args({"totally.bogus": 3})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(client.created, [])

    def test_run_without_per_type_does_nothing(self):
        # no --per-type (and not --list / --cancel-all) -> error out, zero API
        # writes. There is no other grabbing mode any more.
        client = RunFake(azs=["us-east-1b"])
        args = self._args({})
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(client.created, [])
        self.assertEqual(client.modified, [])

    def test_run_honors_azs_scope(self):
        # Region has 3 AZs but --azs locks the sweep to two of them: no grab
        # may land in the excluded AZ. Target is a per-type TOTAL, filled
        # across only the in-scope AZs.
        client = RunFake(azs=["us-east-1b", "us-east-1c", "us-east-1d"], vcpus={})
        args = self._args({"i4i.16xlarge": 3}, azs=["us-east-1b", "us-east-1d"])
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client, az="us-east-1c"), 0)  # excluded AZ untouched
        grabbed_azs = {a for _t, a in client.created}
        self.assertTrue(grabbed_azs <= {"us-east-1b", "us-east-1d"})
        self.assertEqual(_added(client), 3)                  # total target met

    def test_run_warns_and_ignores_missing_az(self):
        # An --azs entry not present in the region is ignored (warned), the
        # valid one still works, nothing crashes.
        client = RunFake(azs=["us-east-1b"], vcpus={})
        args = self._args({"i4i.16xlarge": 2},
                          azs=["us-east-1b", "us-west-2a"])   # 2nd doesn't exist
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual({a for _t, a in client.created}, {"us-east-1b"})
        self.assertEqual(_added(client), 2)

    def test_out_of_scope_az_stock_does_not_satisfy_target(self):
        # Hold 24 in 1a, but sweep is locked to 1b with target 5: the 1a
        # stock must NOT satisfy the gate — exactly 5 land in 1b.
        client = RunFake(
            azs=["us-east-1a", "us-east-1b"],
            reservations=[_reservation("i4i.16xlarge", "us-east-1a", 24,
                                       crid="cr-1a")])
        args = self._args({"i4i.16xlarge": 5}, azs=["us-east-1b"])
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client, az="us-east-1b"), 5)
        self.assertEqual(_added(client, az="us-east-1a"), 0)  # 1a untouched
        # 1a's reservation object not modified either
        one_a = [r for r in client._reservations
                 if r["AvailabilityZone"] == "us-east-1a"][0]
        self.assertEqual(one_a["TotalInstanceCount"], 24)

    def test_multi_az_two_odcrs_different_counts_sum_and_grow_correctly(self):
        # The user's scenario: --azs 1a 1b, one ODCR in each with DIFFERENT
        # counts (3 in 1a, 1 in 1b), target 5 total. Gate must sum 3+1=4 and
        # grab exactly ONE more; the grow must go to the right object with
        # the right absolute count (its own count+1, not the other's).
        client = RunFake(
            azs=["us-east-1a", "us-east-1b"],
            reservations=[
                _reservation("i7i.8xlarge", "us-east-1a", 3, crid="cr-1a"),
                _reservation("i7i.8xlarge", "us-east-1b", 1, crid="cr-1b"),
            ],
            vcpus={"i7i.8xlarge": 32})
        args = self._args({"i7i.8xlarge": 5},
                          azs=["us-east-1a", "us-east-1b"])
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client), 1)               # 5 - (3+1) = 1
        self.assertEqual(client.created, [])              # grew, no new object
        # whichever object grew, it grew to ITS OWN count+1
        by_id = {r["CapacityReservationId"]: r["TotalInstanceCount"]
                 for r in client._reservations}
        self.assertIn(client.modified[0],
                      [("cr-1a", 4), ("cr-1b", 2)])
        self.assertEqual(sorted(by_id.values()), sorted([4, 1])
                         if client.modified[0][0] == "cr-1a" else [3, 2])

    def test_absolute_target_already_met_grabs_nothing(self):
        # default (absolute) semantics: hold 24, target 1 -> nothing to do.
        client = RunFake(
            azs=["us-east-1a"],
            reservations=[_reservation("i4i.16xlarge", "us-east-1a", 24)])
        args = self._args({"i4i.16xlarge": 1}, azs=["us-east-1a"])
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client), 0)

    def test_add_grabs_n_more_on_top_of_held(self):
        # --add: hold 24, ask +1 -> effective target 25, exactly ONE grab
        # (a grow of the existing reservation, no new object).
        client = RunFake(
            azs=["us-east-1a"],
            reservations=[_reservation("i4i.16xlarge", "us-east-1a", 24)])
        args = self._args({"i4i.16xlarge": 1}, azs=["us-east-1a"], add=True)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client), 1)
        self.assertEqual(client.created, [])              # grew, not created
        self.assertEqual(client._reservations[0]["TotalInstanceCount"], 25)

    def test_add_from_zero_equals_absolute(self):
        # --add with nothing held: +2 == grab 2, same as absolute.
        client = RunFake(azs=["us-east-1b"], vcpus={})
        args = self._args({"i4i.16xlarge": 2}, add=True)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client), 2)

    def test_add_requires_live(self):
        # --add is a manual top-up action; a dry-run "+N" has no stable base
        # to add onto, so it is rejected up front (no API writes).
        client = RunFake(azs=["us-east-1b"])
        args = self._args({"i4i.16xlarge": 1}, add=True, live=False)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(client.created, [])
        self.assertEqual(client.modified, [])

    def test_add_rejects_watch(self):
        # --add + --watch is the systemd footgun (every restart adds N more);
        # refuse the combination outright, no API writes.
        client = RunFake(
            azs=["us-east-1a"],
            reservations=[_reservation("i4i.16xlarge", "us-east-1a", 24)])
        args = self._args({"i4i.16xlarge": 1}, azs=["us-east-1a"],
                          add=True, watch=True)
        with mock.patch.object(grab_odcr, "ec2_client", return_value=client):
            grab_odcr.run(args)
        self.assertEqual(_added(client), 0)
        self.assertEqual(client.modified, [])


if __name__ == "__main__":
    unittest.main()
