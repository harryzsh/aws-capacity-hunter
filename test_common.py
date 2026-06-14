#!/usr/bin/env python3
"""Unit tests for common.py — every shared helper.

All mocked, no AWS, no real sleeping, no polluting the repo's logs/ dir.

Run:  python3 -m unittest test_common -v
"""
import io
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from botocore.exceptions import ClientError

import common
from common import (
    VCPU, DEFAULT_PRIORITY, DEFAULT_REGION,
    resolve_types, resolve_azs, list_azs, offered_types_by_az,
    classify, backoff_sleep, record_grab, setup_logging, ec2_client,
)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


class ResolveTypes(unittest.TestCase):
    def test_none_returns_default(self):
        self.assertEqual(resolve_types(None), (list(DEFAULT_PRIORITY), []))

    def test_empty_returns_default(self):
        self.assertEqual(resolve_types([]), (list(DEFAULT_PRIORITY), []))

    def test_sorts_large_first_by_vcpu(self):
        ordered, dropped = resolve_types(
            ["i4i.large", "i4i.16xlarge", "i4i.4xlarge"])
        self.assertEqual(ordered,
                         ["i4i.16xlarge", "i4i.4xlarge", "i4i.large"])
        self.assertEqual(dropped, [])

    def test_drops_unknown_types(self):
        ordered, dropped = resolve_types(["i4i.16xlarge", "bogus.type"])
        self.assertEqual(ordered, ["i4i.16xlarge"])
        self.assertEqual(dropped, ["bogus.type"])

    def test_all_unknown_yields_empty_ordered(self):
        ordered, dropped = resolve_types(["nope1", "nope2"])
        self.assertEqual(ordered, [])
        self.assertEqual(sorted(dropped), ["nope1", "nope2"])

    def test_i4g_fallback_known(self):
        # both are 64 vCPU -> a tie; sorted() is stable so it keeps input
        # order. Don't pin the tie order; just assert both survive, none drop.
        ordered, dropped = resolve_types(["i4g.16xlarge", "i4i.16xlarge"])
        self.assertEqual(dropped, [])
        self.assertEqual(set(ordered), {"i4g.16xlarge", "i4i.16xlarge"})

    def test_mixed_sizes_strictly_descending(self):
        ordered, _ = resolve_types(
            ["i4i.large", "i4i.32xlarge", "i4i.8xlarge", "i4i.2xlarge"])
        self.assertEqual(ordered,
                         ["i4i.32xlarge", "i4i.8xlarge", "i4i.2xlarge", "i4i.large"])


class ResolveAzs(unittest.TestCase):
    ALL = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]

    def test_none_returns_all(self):
        self.assertEqual(resolve_azs(self.ALL, None), (list(self.ALL), []))

    def test_empty_returns_all(self):
        self.assertEqual(resolve_azs(self.ALL, []), (list(self.ALL), []))

    def test_filters_to_requested(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1d"])
        self.assertEqual(sel, ["us-east-1b", "us-east-1d"])
        self.assertEqual(missing, [])

    def test_reports_missing(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1z"])
        self.assertEqual(sel, ["us-east-1b"])
        self.assertEqual(missing, ["us-east-1z"])

    def test_preserves_requested_order(self):
        sel, _ = resolve_azs(self.ALL, ["us-east-1d", "us-east-1b"])
        self.assertEqual(sel, ["us-east-1d", "us-east-1b"])  # not re-sorted


class ListAzs(unittest.TestCase):
    def test_returns_sorted_available_zone_names(self):
        client = mock.Mock()
        client.describe_availability_zones.return_value = {
            "AvailabilityZones": [
                {"ZoneName": "us-east-1d"},
                {"ZoneName": "us-east-1a"},
                {"ZoneName": "us-east-1c"},
            ]
        }
        self.assertEqual(list_azs(client),
                         ["us-east-1a", "us-east-1c", "us-east-1d"])
        # must filter on state=available
        _, kwargs = client.describe_availability_zones.call_args
        self.assertEqual(kwargs["Filters"],
                         [{"Name": "state", "Values": ["available"]}])


class OfferedTypesByAz(unittest.TestCase):
    def test_builds_type_az_combo_set(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [
                {"InstanceType": "i4i.16xlarge", "Location": "us-east-1b"},
                {"InstanceType": "i4i.16xlarge", "Location": "us-east-1d"},
            ]
        }
        combos = offered_types_by_az(client, ["i4i.16xlarge"])
        self.assertEqual(combos, {
            ("i4i.16xlarge", "us-east-1b"),
            ("i4i.16xlarge", "us-east-1d"),
        })

    def test_empty_offerings(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": []}
        self.assertEqual(offered_types_by_az(client, ["i4i.16xlarge"]), set())


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

    def test_missing_code_is_fatal(self):
        e = ClientError({"Error": {}}, "Op")
        self.assertEqual(classify(e), "fatal")


class BackoffSleep(unittest.TestCase):
    def test_delay_grows_then_caps(self):
        seen = []
        # random.uniform(0, d) -> return d so we can assert the upper bound
        with mock.patch.object(common.time, "sleep", lambda s: seen.append(s)), \
             mock.patch.object(common.random, "uniform", lambda a, b: b):
            for attempt in range(8):
                backoff_sleep(attempt, base=1.0, cap=20.0)
        # 1,2,4,8,16, then capped at 20,20,20
        self.assertEqual(seen[:5], [1, 2, 4, 8, 16])
        self.assertTrue(all(s <= 20.0 for s in seen))
        self.assertEqual(seen[5:], [20.0, 20.0, 20.0])

    def test_jitter_within_zero_and_delay(self):
        captured = {}
        with mock.patch.object(common.time, "sleep", lambda s: None), \
             mock.patch.object(common.random, "uniform",
                               lambda a, b: captured.update(lo=a, hi=b)):
            backoff_sleep(3, base=1.0, cap=20.0)  # delay = 8
        self.assertEqual(captured, {"lo": 0, "hi": 8})


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
        record_grab("odcr", "i4i.16xlarge", "us-east-1b", 64, 64, 10000,
                    "us-east-1", dry_run=True)
        self.assertFalse(os.path.exists(self.ledger))

    def test_live_appends_one_json_line_with_fields(self):
        record_grab("odcr", "i4i.16xlarge", "us-east-1b", 64, 128, 10000,
                    "us-east-1", dry_run=False,
                    per_az_cores=5000, per_az_total=64)
        with open(self.ledger) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["via"], "odcr")
        self.assertEqual(rec["instance_type"], "i4i.16xlarge")
        self.assertEqual(rec["az"], "us-east-1b")
        self.assertEqual(rec["vcpu"], 64)
        self.assertEqual(rec["total_vcpu"], 128)
        self.assertEqual(rec["target_vcpu"], 10000)
        self.assertEqual(rec["region"], "us-east-1")
        self.assertEqual(rec["per_az_cores"], 5000)   # balanced-mode cap recorded
        self.assertEqual(rec["per_az_total"], 64)     # cores held in this AZ
        self.assertIn("ts", rec)

    def test_per_az_fields_null_when_not_balanced(self):
        # no per-az args -> fields present but null (not missing)
        record_grab("odcr", "i4i.16xlarge", "us-east-1b", 64, 64, 1000,
                    "us-east-1", dry_run=False)
        with open(self.ledger) as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertIsNone(rec["per_az_cores"])
        self.assertIsNone(rec["per_az_total"])

    def test_appends_not_overwrites(self):
        for i in range(3):
            record_grab("odcr", "i4i.16xlarge", "us-east-1b", 64, 64 * (i + 1),
                        10000, "us-east-1", dry_run=False)
        with open(self.ledger) as f:
            self.assertEqual(len(f.read().splitlines()), 3)


class SetupLogging(unittest.TestCase):
    def test_console_only_when_no_file(self):
        logger = setup_logging(None)
        self.assertEqual(logger.name, "i4i-grab")
        # exactly one handler (console), no file handler
        from logging.handlers import RotatingFileHandler
        self.assertEqual(len(logger.handlers), 1)
        self.assertFalse(any(isinstance(h, RotatingFileHandler)
                             for h in logger.handlers))

    def test_adds_rotating_file_handler(self):
        tmp = tempfile.mkdtemp()
        from logging.handlers import RotatingFileHandler
        with mock.patch.object(common, "LOGS_DIR", tmp):
            logger = setup_logging("t.log")
        self.assertTrue(any(isinstance(h, RotatingFileHandler)
                            for h in logger.handlers))
        self.assertTrue(os.path.exists(os.path.join(tmp, "t.log")))

    def test_idempotent_no_handler_pileup(self):
        # calling twice must not stack duplicate handlers (handlers.clear())
        a = setup_logging(None)
        n1 = len(a.handlers)
        b = setup_logging(None)
        self.assertEqual(len(b.handlers), n1)
        self.assertIs(a, b)  # same named logger


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
