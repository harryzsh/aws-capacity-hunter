#!/usr/bin/env python3
"""Unit tests for grab_g7e_odcr.py — multi-type (g6e+g7e) count-based engine.

Mocks boto3 entirely (no AWS, no cost). Pins: CLI target parsing, plan
building (explicit matrix / --counts greedy / --counts --balance / compat),
the per-(type,az) gates, restart-safe resume, and the API wrappers.

Run:  python3 -m unittest test_grab_g7e_odcr -v
"""
import datetime
import logging
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from botocore.exceptions import ClientError

import grab_g7e_odcr
from grab_g7e_odcr import (
    parse_counts, parse_az_counts, distribute, build_plan,
    held_by_type_az, type_held, grand_held,
    _cell_full, _type_done, _all_done, sweep_once, print_list,
    reserve_one, list_reservations, cancel_all, DEFAULT_TARGET,
)
from common import TAG_KEY, TAG_VAL

logging.getLogger("g-grab").setLevel(logging.CRITICAL)


def setUpModule():
    # test_common's SetupLogging tests call setup_logging(), which resets the
    # shared "g-grab" logger to INFO. Re-silence here so this module's sweep /
    # cancel tests (which log at INFO) don't spew to the console in CI.
    logging.getLogger("g-grab").setLevel(logging.CRITICAL)

G6 = "g6e.48xlarge"
G7 = "g7e.48xlarge"


def _cap_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}},
                       "CreateCapacityReservation")


class FakeEC2:
    def __init__(self, reservations=None, no_capacity=()):
        self._reservations = reservations or []
        self._no_capacity = set(no_capacity)   # AZ names OR (type,az) tuples
        self.created = []
        self.create_kwargs = []
        self.cancelled = []
        self._n = 0

    def describe_capacity_reservations(self, **kwargs):
        return {"CapacityReservations": self._reservations}

    def create_capacity_reservation(self, **kwargs):
        self.create_kwargs.append(kwargs)
        if kwargs.get("DryRun"):
            raise _cap_error("DryRunOperation")
        az = kwargs["AvailabilityZone"]
        itype = kwargs["InstanceType"]
        if az in self._no_capacity or (itype, az) in self._no_capacity:
            raise _cap_error("InsufficientInstanceCapacity")
        self._n += 1
        self.created.append((itype, az))
        return {"CapacityReservation": {"CapacityReservationId": "cr-%04d" % self._n}}

    def cancel_capacity_reservation(self, CapacityReservationId=None):
        self.cancelled.append(CapacityReservationId)
        return {}


def _reservation(itype, az, count, tag=TAG_VAL, state="active", available=None):
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
    base = dict(region="us-east-1", counts=None, az_counts=None, balance=False,
                azs=None, target_count=DEFAULT_TARGET, per_az_count=None,
                live=True, end_hours=None)
    base.update(over)
    return Namespace(**base)


def _drain(client, args, cells, type_targets, offered, held, max_rounds=10000):
    made = []
    for _ in range(max_rounds):
        if _all_done(type_targets, held):
            break
        before = dict(held)
        sweep_once(client, args, cells, type_targets, offered, held, made)
        if held == before:
            break
    return made


# --------------------------------------------------------------------------- #
# CLI parsing
# --------------------------------------------------------------------------- #
class ParseCounts(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_counts([f"{G6}=10", f"{G7}=20"]),
                         {G6: 10, G7: 20})

    def test_bad_syntax(self):
        with self.assertRaises(ValueError):
            parse_counts(["g7e.48xlarge"])

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            parse_counts(["p5.48xlarge=2"])

    def test_non_positive(self):
        with self.assertRaises(ValueError):
            parse_counts([f"{G7}=0"])


class ParseAzCounts(unittest.TestCase):
    def test_basic(self):
        got = parse_az_counts([f"{G7}@us-east-1b=5", f"{G6}@us-east-1d=10"])
        self.assertEqual(got, {(G7, "us-east-1b"): 5, (G6, "us-east-1d"): 10})

    def test_bad_syntax_missing_at(self):
        with self.assertRaises(ValueError):
            parse_az_counts([f"{G7}=5"])

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            parse_az_counts(["p5.48xlarge@us-east-1b=5"])


class Distribute(unittest.TestCase):
    def test_even(self):
        self.assertEqual(distribute(10, ["a", "b"]), [5, 5])

    def test_remainder_front_loaded(self):
        self.assertEqual(distribute(11, ["a", "b"]), [6, 5])

    def test_fewer_than_azs(self):
        self.assertEqual(distribute(1, ["a", "b"]), [1, 0])

    def test_empty_azs(self):
        self.assertEqual(distribute(5, []), [])


class BuildPlan(unittest.TestCase):
    ALL = ["us-east-1a", "us-east-1b", "us-east-1d"]

    def test_az_counts_explicit_matrix(self):
        args = _args(az_counts=[f"{G7}@us-east-1b=5", f"{G7}@us-east-1d=3",
                                f"{G6}@us-east-1b=2", f"{G6}@us-east-1d=10"])
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(cells, {
            (G7, "us-east-1b"): 5, (G7, "us-east-1d"): 3,
            (G6, "us-east-1b"): 2, (G6, "us-east-1d"): 10,
        })
        self.assertEqual(tt, {G7: 8, G6: 12})

    def test_counts_greedy_no_cap(self):
        args = _args(counts=[f"{G6}=4", f"{G7}=6"],
                     azs=["us-east-1b", "us-east-1d"])
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(tt, {G6: 4, G7: 6})
        # greedy -> every cell cap is None
        self.assertTrue(all(v is None for v in cells.values()))
        self.assertEqual(set(cells),
                         {(G6, "us-east-1b"), (G6, "us-east-1d"),
                          (G7, "us-east-1b"), (G7, "us-east-1d")})

    def test_counts_balanced_splits_evenly(self):
        args = _args(counts=[f"{G7}=5"], azs=["us-east-1b", "us-east-1d"],
                     balance=True)
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(tt, {G7: 5})
        self.assertEqual(cells[(G7, "us-east-1b")], 3)   # front-loaded
        self.assertEqual(cells[(G7, "us-east-1d")], 2)

    def test_counts_without_azs_uses_all_region_azs(self):
        args = _args(counts=[f"{G7}=3"])
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(set(a for (_t, a) in cells), set(self.ALL))

    def test_compat_per_az_auto_target(self):
        args = _args(per_az_count=2, azs=["us-east-1b", "us-east-1d"])
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(tt, {G7: 4})                    # 2 x 2 AZ
        self.assertEqual(cells[(G7, "us-east-1b")], 2)

    def test_compat_target_only_greedy(self):
        args = _args(target_count=3, azs=["us-east-1b"])
        cells, tt = build_plan(args, self.ALL)
        self.assertEqual(tt, {G7: 3})
        self.assertIsNone(cells[(G7, "us-east-1b")])


# --------------------------------------------------------------------------- #
# held / gates
# --------------------------------------------------------------------------- #
class HeldByTypeAz(unittest.TestCase):
    def test_counts_instances_per_type_az(self):
        client = FakeEC2([
            _reservation(G7, "us-east-1b", 3),
            _reservation(G7, "us-east-1b", 1),    # g7 1b = 4
            _reservation(G6, "us-east-1d", 2),
        ])
        self.assertEqual(held_by_type_az(client), {
            (G7, "us-east-1b"): 4, (G6, "us-east-1d"): 2})

    def test_ignores_untagged_and_other_types(self):
        client = FakeEC2([
            _reservation(G7, "us-east-1b", 3, tag=None),
            _reservation("p5.48xlarge", "us-east-1b", 9),
            _reservation(G6, "us-east-1b", 1),
        ])
        self.assertEqual(held_by_type_az(client), {(G6, "us-east-1b"): 1})

    def test_scope_filters_out_of_plan_cells(self):
        client = FakeEC2([
            _reservation(G7, "us-east-1b", 4),
            _reservation(G6, "us-east-1d", 3),
        ])
        scope = {(G6, "us-east-1d")}
        self.assertEqual(held_by_type_az(client, scope=scope),
                         {(G6, "us-east-1d"): 3})

    def test_type_held_and_grand_held(self):
        held = {(G7, "us-east-1b"): 4, (G7, "us-east-1d"): 2, (G6, "us-east-1b"): 5}
        self.assertEqual(type_held(held, G7), 6)
        self.assertEqual(type_held(held, G6), 5)
        self.assertEqual(grand_held(held), 11)


class Gates(unittest.TestCase):
    def test_cell_full_with_cap(self):
        cells = {(G7, "us-east-1b"): 2}
        self.assertTrue(_cell_full(cells, {(G7, "us-east-1b"): 2}, G7, "us-east-1b"))
        self.assertFalse(_cell_full(cells, {(G7, "us-east-1b"): 1}, G7, "us-east-1b"))

    def test_cell_none_cap_never_full(self):
        cells = {(G7, "us-east-1b"): None}
        self.assertFalse(_cell_full(cells, {(G7, "us-east-1b"): 999}, G7, "us-east-1b"))

    def test_type_done_and_all_done(self):
        tt = {G7: 4, G6: 2}
        held = {(G7, "us-east-1b"): 4, (G6, "us-east-1d"): 1}
        self.assertTrue(_type_done(tt, held, G7))
        self.assertFalse(_type_done(tt, held, G6))
        self.assertFalse(_all_done(tt, held))
        held[(G6, "us-east-1d")] = 2
        self.assertTrue(_all_done(tt, held))


# --------------------------------------------------------------------------- #
# sweep / resume
# --------------------------------------------------------------------------- #
class Sweep(unittest.TestCase):
    def setUp(self):
        self._orig = grab_g7e_odcr.record_grab
        grab_g7e_odcr.record_grab = lambda *a, **k: None

    def tearDown(self):
        grab_g7e_odcr.record_grab = self._orig

    def test_single_sweep_one_per_cell(self):
        cells = {(G7, "us-east-1b"): 5, (G6, "us-east-1d"): 5}
        tt = {G7: 5, G6: 5}
        offered = {(G7, "us-east-1b"), (G6, "us-east-1d")}
        held = {}
        client = FakeEC2()
        sweep_once(client, _args(), cells, tt, offered, held, [])
        self.assertEqual(sorted(client.created),
                         [(G6, "us-east-1d"), (G7, "us-east-1b")])
        self.assertEqual(held, {(G7, "us-east-1b"): 1, (G6, "us-east-1d"): 1})

    def test_explicit_matrix_fills_each_cell_to_its_cap(self):
        # the headline feature: different counts per (type,az)
        cells = {(G7, "us-east-1b"): 5, (G7, "us-east-1d"): 3,
                 (G6, "us-east-1b"): 2, (G6, "us-east-1d"): 10}
        tt = {G7: 8, G6: 12}
        offered = set(cells)
        held = {}
        client = FakeEC2()
        _drain(client, _args(), cells, tt, offered, held)
        self.assertEqual(held, {(G7, "us-east-1b"): 5, (G7, "us-east-1d"): 3,
                                (G6, "us-east-1b"): 2, (G6, "us-east-1d"): 10})

    def test_greedy_fills_type_total_without_per_cell_cap(self):
        cells = {(G7, "us-east-1b"): None, (G7, "us-east-1d"): None}
        tt = {G7: 5}
        offered = set(cells)
        held = {}
        client = FakeEC2()
        _drain(client, _args(), cells, tt, offered, held)
        self.assertEqual(type_held(held, G7), 5)        # exact, no overshoot

    def test_restart_skips_full_cell_tops_up_short(self):
        cells = {(G7, "us-east-1b"): 5, (G7, "us-east-1d"): 3}
        tt = {G7: 8}
        offered = set(cells)
        held = {(G7, "us-east-1b"): 5, (G7, "us-east-1d"): 1}   # 1b full, 1d short
        client = FakeEC2()
        _drain(client, _args(), cells, tt, offered, held)
        self.assertEqual([c for c in client.created if c[1] == "us-east-1b"], [])
        self.assertEqual(
            len([c for c in client.created if c[1] == "us-east-1d"]), 2)
        self.assertEqual(held[(G7, "us-east-1d")], 3)

    def test_not_offered_cell_skipped(self):
        cells = {(G7, "us-east-1a"): 2, (G7, "us-east-1d"): 2}
        tt = {G7: 4}
        offered = {(G7, "us-east-1d")}                  # 1a not offered
        held = {}
        client = FakeEC2()
        _drain(client, _args(), cells, tt, offered, held)
        self.assertNotIn((G7, "us-east-1a"), held)
        self.assertEqual(held[(G7, "us-east-1d")], 2)
        self.assertLess(grand_held(held), 4)            # stays short

    def test_no_capacity_in_one_cell(self):
        cells = {(G7, "us-east-1b"): 2, (G7, "us-east-1d"): 2}
        tt = {G7: 4}
        offered = set(cells)
        held = {}
        client = FakeEC2(no_capacity={(G7, "us-east-1b")})
        _drain(client, _args(), cells, tt, offered, held)
        self.assertNotIn((G7, "us-east-1b"), held)
        self.assertEqual(held[(G7, "us-east-1d")], 2)

    def test_dry_run_no_real_reservations(self):
        cells = {(G7, "us-east-1b"): 2}
        tt = {G7: 2}
        offered = set(cells)
        held = {}
        client = FakeEC2()
        _drain(client, _args(live=False), cells, tt, offered, held)
        self.assertEqual(held, {(G7, "us-east-1b"): 2})  # simulated
        self.assertEqual(client.created, [])             # nothing real


# --------------------------------------------------------------------------- #
# print_list
# --------------------------------------------------------------------------- #
class PrintList(unittest.TestCase):
    def test_summary_groups_by_type_and_az_with_progress(self):
        client = FakeEC2([
            _reservation(G7, "us-east-1b", 5),
            _reservation(G7, "us-east-1d", 1),
            _reservation(G6, "us-east-1d", 10),
        ])
        cells = {(G7, "us-east-1b"): 5, (G7, "us-east-1d"): 3,
                 (G6, "us-east-1d"): 10}
        tt = {G7: 8, G6: 10}
        with self.assertLogs("g-grab", level="INFO") as cm:
            print_list(client, cells, tt)
        out = "\n".join(cm.output)
        self.assertIn("5 / 5 [FULL]", out)          # g7 1b at cap
        self.assertIn("1 / 3 [short]", out)         # g7 1d short
        self.assertIn("10 / 10 [FULL]", out)        # g6 1d at cap
        self.assertIn("GRAND TOTAL", out)
        self.assertIn("16 / 18 instances [short]", out)

    def test_empty_says_none(self):
        client = FakeEC2([])
        with self.assertLogs("g-grab", level="INFO") as cm:
            print_list(client, {}, {})
        self.assertIn("no active/pending reservations", "\n".join(cm.output))

    def test_auto_reads_plan_when_not_passed(self):
        with tempfile.TemporaryDirectory() as d:
            planfile = os.path.join(d, "plan.json")
            with mock.patch.object(grab_g7e_odcr, "load_plan") as lp:
                lp.return_value = ({(G7, "us-east-1b"): 5}, {G7: 5})
                client = FakeEC2([_reservation(G7, "us-east-1b", 5)])
                with self.assertLogs("g-grab", level="INFO") as cm:
                    print_list(client)              # no plan args
        out = "\n".join(cm.output)
        self.assertIn("5 / 5 [FULL]", out)


# --------------------------------------------------------------------------- #
# API wrappers
# --------------------------------------------------------------------------- #
class ReserveOne(unittest.TestCase):
    def test_open_linux_default_count1_tagged_typed(self):
        client = FakeEC2()
        reserve_one(client, G6, "us-east-1b", dry_run=False)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["InstanceType"], G6)
        self.assertEqual(kw["InstancePlatform"], "Linux/UNIX")
        self.assertEqual(kw["AvailabilityZone"], "us-east-1b")
        self.assertEqual(kw["InstanceCount"], 1)
        self.assertEqual(kw["InstanceMatchCriteria"], "open")
        self.assertEqual(kw["Tenancy"], "default")
        self.assertEqual(kw["EbsOptimized"], True)
        tags = kw["TagSpecifications"][0]["Tags"]
        self.assertIn({"Key": TAG_KEY, "Value": TAG_VAL}, tags)

    def test_dry_run_flag_passes_through(self):
        client = FakeEC2()
        with self.assertRaises(ClientError):
            reserve_one(client, G7, "us-east-1b", dry_run=True)
        self.assertTrue(client.create_kwargs[-1]["DryRun"])

    def test_no_end_hours_is_unlimited(self):
        client = FakeEC2()
        reserve_one(client, G7, "us-east-1b", dry_run=False)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["EndDateType"], "unlimited")
        self.assertNotIn("EndDate", kw)

    def test_end_hours_limited(self):
        client = FakeEC2()
        reserve_one(client, G7, "us-east-1b", dry_run=False, end_hours=6)
        kw = client.create_kwargs[-1]
        self.assertEqual(kw["EndDateType"], "limited")
        self.assertIn("EndDate", kw)


class ListReservations(unittest.TestCase):
    def test_parses_rows_and_tag(self):
        client = FakeEC2([_reservation(G7, "us-east-1b", 3)])
        rows = list_reservations(client)
        crid, itype, az, state, cnt, tag, avail = rows[0]
        self.assertEqual(itype, G7)
        self.assertEqual(cnt, 3)
        self.assertEqual(tag, TAG_VAL)
        self.assertEqual(avail, 3)


class CancelAll(unittest.TestCase):
    def test_only_cancels_tagged(self):
        client = FakeEC2([
            _reservation(G7, "us-east-1b", 3),
            _reservation(G6, "us-east-1d", 2),
            _reservation(G7, "us-east-1c", 9, tag="other"),
        ])
        cancel_all(client, dry_run=False)
        self.assertEqual(len(client.cancelled), 2)

    def test_dry_run_cancels_nothing(self):
        client = FakeEC2([_reservation(G7, "us-east-1b", 3)])
        cancel_all(client, dry_run=True)
        self.assertEqual(client.cancelled, [])


if __name__ == "__main__":
    unittest.main()
