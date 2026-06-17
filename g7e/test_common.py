#!/usr/bin/env python3
"""Unit tests for common.py (multi-type G-series helpers).

All mocked, no AWS. Run:  python3 -m unittest test_common -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from botocore.exceptions import ClientError

import common
from common import (
    DEFAULT_REGION, SUPPORTED_TYPES, VCPU, DEFAULT_TYPE, TAG_VAL,
    resolve_azs, list_azs, offered_by_az,
    classify, backoff_sleep, record_grab, setup_logging, ec2_client,
    save_plan, load_plan,
)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


class Constants(unittest.TestCase):
    def test_supported_types_are_the_two_48xl(self):
        self.assertEqual(SUPPORTED_TYPES, ["g6e.48xlarge", "g7e.48xlarge"])

    def test_vcpu_map_is_192_each(self):
        self.assertEqual(VCPU["g6e.48xlarge"], 192)
        self.assertEqual(VCPU["g7e.48xlarge"], 192)

    def test_default_type_is_g7e(self):
        self.assertEqual(DEFAULT_TYPE, "g7e.48xlarge")

    def test_tag_is_g_grab(self):
        self.assertEqual(TAG_VAL, "g-grab")


class ResolveAzs(unittest.TestCase):
    ALL = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]

    def test_none_returns_all(self):
        self.assertEqual(resolve_azs(self.ALL, None), (list(self.ALL), []))

    def test_filters_to_requested(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1d"])
        self.assertEqual(sel, ["us-east-1b", "us-east-1d"])
        self.assertEqual(missing, [])

    def test_reports_missing(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1z"])
        self.assertEqual(sel, ["us-east-1b"])
        self.assertEqual(missing, ["us-east-1z"])


class ListAzs(unittest.TestCase):
    def test_returns_sorted_available_zone_names(self):
        client = mock.Mock()
        client.describe_availability_zones.return_value = {
            "AvailabilityZones": [
                {"ZoneName": "us-east-1d"},
                {"ZoneName": "us-east-1a"},
            ]
        }
        self.assertEqual(list_azs(client), ["us-east-1a", "us-east-1d"])
        _, kwargs = client.describe_availability_zones.call_args
        self.assertEqual(kwargs["Filters"],
                         [{"Name": "state", "Values": ["available"]}])


class OfferedByAz(unittest.TestCase):
    def test_builds_type_az_combo_set(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [
                {"InstanceType": "g7e.48xlarge", "Location": "us-east-1b"},
                {"InstanceType": "g6e.48xlarge", "Location": "us-east-1d"},
            ]
        }
        combos = offered_by_az(client, SUPPORTED_TYPES)
        self.assertEqual(combos, {
            ("g7e.48xlarge", "us-east-1b"),
            ("g6e.48xlarge", "us-east-1d"),
        })
        _, kwargs = client.describe_instance_type_offerings.call_args
        self.assertEqual(kwargs["LocationType"], "availability-zone")
        self.assertEqual(kwargs["Filters"],
                         [{"Name": "instance-type", "Values": SUPPORTED_TYPES}])

    def test_empty_offerings(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": []}
        self.assertEqual(offered_by_az(client, SUPPORTED_TYPES), set())


class Classify(unittest.TestCase):
    def test_dryrun(self):
        self.assertEqual(classify(_err("DryRunOperation")), "dryrun_ok")

    def test_capacity_variants(self):
        for code in ("InsufficientInstanceCapacity", "InsufficientCapacity",
                     "Unsupported", "InsufficientHostCapacity"):
            self.assertEqual(classify(_err(code)), "capacity", code)

    def test_throttle_variants(self):
        for code in ("RequestLimitExceeded", "Throttling", "ThrottlingException"):
            self.assertEqual(classify(_err(code)), "throttle", code)

    def test_unknown_is_fatal(self):
        self.assertEqual(classify(_err("UnauthorizedOperation")), "fatal")


class BackoffSleep(unittest.TestCase):
    def test_delay_grows_then_caps(self):
        seen = []
        with mock.patch.object(common.time, "sleep", lambda s: seen.append(s)), \
             mock.patch.object(common.random, "uniform", lambda a, b: b):
            for attempt in range(8):
                backoff_sleep(attempt, base=1.0, cap=20.0)
        self.assertEqual(seen[:5], [1, 2, 4, 8, 16])
        self.assertTrue(all(s <= 20.0 for s in seen))


class RecordGrab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "grabs.jsonl")
        self._p1 = mock.patch.object(common, "LOGS_DIR", self.tmp)
        self._p2 = mock.patch.object(common, "GRAB_LEDGER", self.ledger)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_dry_run_writes_nothing(self):
        record_grab("odcr", "g7e.48xlarge", "us-east-1b", 1, 1, 4,
                    "us-east-1", dry_run=True)
        self.assertFalse(os.path.exists(self.ledger))

    def test_live_appends_one_json_line_with_fields(self):
        record_grab("odcr", "g6e.48xlarge", "us-east-1b", 1, 2, 10,
                    "us-east-1", dry_run=False, az_cap=5, az_total=2,
                    grand_total=7, grand_target=30)
        with open(self.ledger) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["instance_type"], "g6e.48xlarge")
        self.assertEqual(rec["az"], "us-east-1b")
        self.assertEqual(rec["count"], 1)
        self.assertEqual(rec["type_total"], 2)
        self.assertEqual(rec["type_target"], 10)
        self.assertEqual(rec["az_cap"], 5)
        self.assertEqual(rec["az_total"], 2)
        self.assertEqual(rec["grand_total"], 7)
        self.assertEqual(rec["grand_target"], 30)
        self.assertIn("ts", rec)

    def test_null_optional_fields(self):
        record_grab("odcr", "g7e.48xlarge", "us-east-1b", 1, 1, 1,
                    "us-east-1", dry_run=False)
        with open(self.ledger) as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertIsNone(rec["az_cap"])
        self.assertIsNone(rec["az_total"])


class SavePlanLoadPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.plan = os.path.join(self.tmp, "plan.json")
        self._p1 = mock.patch.object(common, "LOGS_DIR", self.tmp)
        self._p2 = mock.patch.object(common, "PLAN_FILE", self.plan)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_roundtrip_with_caps_and_none(self):
        cells = {
            ("g7e.48xlarge", "us-east-1b"): 5,
            ("g7e.48xlarge", "us-east-1d"): 3,
            ("g6e.48xlarge", "us-east-1b"): None,   # greedy cell
        }
        type_targets = {"g7e.48xlarge": 8, "g6e.48xlarge": 10}
        save_plan(cells, type_targets, "us-east-1")
        c2, t2 = load_plan()
        self.assertEqual(c2, cells)
        self.assertEqual(t2, type_targets)

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_plan(), ({}, {}))


class SetupLogging(unittest.TestCase):
    def test_logger_name_is_g_grab(self):
        logger = setup_logging(None)
        self.assertEqual(logger.name, "g-grab")

    def test_idempotent_no_handler_pileup(self):
        a = setup_logging(None)
        n1 = len(a.handlers)
        b = setup_logging(None)
        self.assertEqual(len(b.handlers), n1)
        self.assertIs(a, b)


class Ec2Client(unittest.TestCase):
    def test_passes_region(self):
        with mock.patch.object(common.boto3, "client") as mk:
            ec2_client("us-west-2")
            mk.assert_called_once_with("ec2", region_name="us-west-2")

    def test_default_region(self):
        with mock.patch.object(common.boto3, "client") as mk:
            ec2_client()
            mk.assert_called_once_with("ec2", region_name=DEFAULT_REGION)


if __name__ == "__main__":
    unittest.main()
