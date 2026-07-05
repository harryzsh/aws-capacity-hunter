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
    VCPU, DEFAULT_REGION,
    resolve_azs, list_azs, offered_types_by_az,
    classify, backoff_sleep, record_grab, setup_logging, ec2_client,
    describe_vcpus, ensure_vcpu,
)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


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
        record_grab("odcr", "i4i.16xlarge", "us-east-1b", "us-east-1",
                    dry_run=True, held_count=1, target_count=10)
        self.assertFalse(os.path.exists(self.ledger))

    def test_live_appends_one_json_line_with_fields(self):
        record_grab("odcr", "i4i.16xlarge", "us-east-1b", "us-east-1",
                    dry_run=False, held_count=2, target_count=10)
        with open(self.ledger) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["via"], "odcr")
        self.assertEqual(rec["instance_type"], "i4i.16xlarge")
        self.assertEqual(rec["az"], "us-east-1b")
        self.assertEqual(rec["region"], "us-east-1")
        self.assertEqual(rec["held_count"], 2)     # instances held after grab
        self.assertEqual(rec["target_count"], 10)  # --per-type target
        self.assertIn("ts", rec)

    def test_appends_not_overwrites(self):
        for i in range(3):
            record_grab("odcr", "i4i.16xlarge", "us-east-1b", "us-east-1",
                        dry_run=False, held_count=i + 1, target_count=10)
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


class DescribeVcpus(unittest.TestCase):
    """describe_vcpus(): ask AWS the vCPU count for arbitrary instance types,
    so the grabber is not limited to the hand-maintained VCPU table."""

    def test_maps_type_to_default_vcpu_count(self):
        client = mock.Mock()
        client.describe_instance_types.return_value = {
            "InstanceTypes": [
                {"InstanceType": "r7i.48xlarge",
                 "VCpuInfo": {"DefaultVCpus": 192}},
                {"InstanceType": "m7i.large",
                 "VCpuInfo": {"DefaultVCpus": 2}},
            ]
        }
        self.assertEqual(
            describe_vcpus(client, ["r7i.48xlarge", "m7i.large"]),
            {"r7i.48xlarge": 192, "m7i.large": 2},
        )
        # must query EXACTLY the requested types (no wasted describe of all)
        _, kwargs = client.describe_instance_types.call_args
        self.assertEqual(sorted(kwargs["InstanceTypes"]),
                         ["m7i.large", "r7i.48xlarge"])

    def test_empty_input_makes_no_api_call(self):
        client = mock.Mock()
        self.assertEqual(describe_vcpus(client, []), {})
        client.describe_instance_types.assert_not_called()

    def test_paginates_with_next_token(self):
        client = mock.Mock()
        client.describe_instance_types.side_effect = [
            {"InstanceTypes": [{"InstanceType": "c7i.large",
                                "VCpuInfo": {"DefaultVCpus": 2}}],
             "NextToken": "tok"},
            {"InstanceTypes": [{"InstanceType": "c7i.2xlarge",
                                "VCpuInfo": {"DefaultVCpus": 8}}]},
        ]
        out = describe_vcpus(client, ["c7i.large", "c7i.2xlarge"])
        self.assertEqual(out, {"c7i.large": 2, "c7i.2xlarge": 8})
        self.assertEqual(client.describe_instance_types.call_count, 2)
        # 2nd call must forward the NextToken from the 1st
        _, kw2 = client.describe_instance_types.call_args_list[1]
        self.assertEqual(kw2.get("NextToken"), "tok")

    def test_unresolvable_type_simply_absent(self):
        # AWS returns nothing for a bogus type -> it's just not in the map.
        client = mock.Mock()
        client.describe_instance_types.return_value = {"InstanceTypes": []}
        self.assertEqual(describe_vcpus(client, ["nope.nope"]), {})


class EnsureVcpu(unittest.TestCase):
    """ensure_vcpu(): enrich the in-memory VCPU table for any requested types
    that aren't already known, by asking AWS. No-op (no API call) when every
    requested type is already in the table — this keeps --list and the
    all-i4i default path from ever touching DescribeInstanceTypes."""

    def setUp(self):
        # work on a copy so we never mutate the module's real table across tests
        self._orig = dict(common.VCPU)

    def tearDown(self):
        common.VCPU.clear()
        common.VCPU.update(self._orig)

    def test_known_types_make_no_api_call(self):
        client = mock.Mock()
        added, unresolved = ensure_vcpu(client, ["i4i.16xlarge", "i4g.large"])
        self.assertEqual(added, {})
        self.assertEqual(unresolved, [])
        client.describe_instance_types.assert_not_called()

    def test_none_or_empty_is_noop(self):
        client = mock.Mock()
        self.assertEqual(ensure_vcpu(client, None), ({}, []))
        self.assertEqual(ensure_vcpu(client, []), ({}, []))
        client.describe_instance_types.assert_not_called()

    def test_learns_unknown_type_and_inserts_into_table(self):
        client = mock.Mock()
        client.describe_instance_types.return_value = {
            "InstanceTypes": [
                {"InstanceType": "r7i.48xlarge",
                 "VCpuInfo": {"DefaultVCpus": 192}},
            ]
        }
        self.assertNotIn("r7i.48xlarge", common.VCPU)
        added, unresolved = ensure_vcpu(
            client, ["i4i.16xlarge", "r7i.48xlarge"])
        self.assertEqual(added, {"r7i.48xlarge": 192})
        self.assertEqual(unresolved, [])
        # the learned value is now usable by all the core counting logic
        self.assertEqual(common.VCPU["r7i.48xlarge"], 192)
        # only the UNKNOWN type was queried, not the already-known i4i
        _, kwargs = client.describe_instance_types.call_args
        self.assertEqual(kwargs["InstanceTypes"], ["r7i.48xlarge"])

    def test_reports_unresolvable_types(self):
        client = mock.Mock()
        client.describe_instance_types.return_value = {"InstanceTypes": []}
        added, unresolved = ensure_vcpu(client, ["totally.bogus"])
        self.assertEqual(added, {})
        self.assertEqual(unresolved, ["totally.bogus"])
        self.assertNotIn("totally.bogus", common.VCPU)

    def test_deduplicates_requested_unknowns(self):
        client = mock.Mock()
        client.describe_instance_types.return_value = {
            "InstanceTypes": [
                {"InstanceType": "m7i.large", "VCpuInfo": {"DefaultVCpus": 2}},
            ]
        }
        ensure_vcpu(client, ["m7i.large", "m7i.large"])
        _, kwargs = client.describe_instance_types.call_args
        self.assertEqual(kwargs["InstanceTypes"], ["m7i.large"])


if __name__ == "__main__":
    unittest.main()
