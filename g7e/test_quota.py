#!/usr/bin/env python3
"""Unit tests for quota.py — shared G/VT vCPU quota preflight (multi-type).

All mocked, no AWS. Run:  python3 -m unittest test_quota -v
"""
import unittest
from unittest import mock

import quota
from quota import (
    SERVICE_CODE, G_VT_QUOTA_CODE, G_VT_QUOTA_NAME,
    service_quotas_client, vcpus_for_counts, get_g_vt_quota, check_quota,
)
from common import DEFAULT_REGION


def _fake_client(value):
    client = mock.Mock()
    client.get_service_quota.return_value = {"Quota": {"Value": value}}
    return client


class Constants(unittest.TestCase):
    def test_g_vt_quota_code_and_service(self):
        self.assertEqual(SERVICE_CODE, "ec2")
        self.assertEqual(G_VT_QUOTA_CODE, "L-DB2E81BA")
        self.assertEqual(G_VT_QUOTA_NAME, "Running On-Demand G and VT instances")


class VcpusForCounts(unittest.TestCase):
    def test_sums_across_types(self):
        # both 192 vCPU: 10 g6e + 20 g7e = 30 * 192 = 5760
        self.assertEqual(
            vcpus_for_counts({"g6e.48xlarge": 10, "g7e.48xlarge": 20}), 5760)

    def test_single_type(self):
        self.assertEqual(vcpus_for_counts({"g7e.48xlarge": 4}), 768)

    def test_empty(self):
        self.assertEqual(vcpus_for_counts({}), 0)

    def test_unknown_type_contributes_zero(self):
        self.assertEqual(vcpus_for_counts({"bogus": 5}), 0)


class GetGVtQuota(unittest.TestCase):
    def test_reads_value_with_correct_codes(self):
        client = _fake_client(768.0)
        self.assertEqual(get_g_vt_quota(client), 768.0)
        client.get_service_quota.assert_called_once_with(
            ServiceCode="ec2", QuotaCode="L-DB2E81BA")


class CheckQuota(unittest.TestCase):
    def test_sufficient(self):
        client = _fake_client(6000.0)
        r = check_quota(client, {"g6e.48xlarge": 10, "g7e.48xlarge": 20})
        self.assertTrue(r["sufficient"])
        self.assertEqual(r["needed_vcpu"], 5760)
        self.assertEqual(r["current_vcpu"], 6000.0)
        self.assertEqual(r["per_type_vcpu"],
                         {"g6e.48xlarge": 1920, "g7e.48xlarge": 3840})

    def test_insufficient(self):
        client = _fake_client(768.0)
        r = check_quota(client, {"g6e.48xlarge": 10, "g7e.48xlarge": 20})
        self.assertFalse(r["sufficient"])
        self.assertEqual(r["needed_vcpu"], 5760)

    def test_exact_boundary_is_sufficient(self):
        client = _fake_client(768.0)
        r = check_quota(client, {"g7e.48xlarge": 4})
        self.assertTrue(r["sufficient"])

    def test_zero_quota_insufficient(self):
        client = _fake_client(0.0)
        r = check_quota(client, {"g7e.48xlarge": 1})
        self.assertFalse(r["sufficient"])


class ServiceQuotasClient(unittest.TestCase):
    def test_passes_region(self):
        with mock.patch.object(quota.boto3, "client") as mk:
            service_quotas_client("us-west-2")
            mk.assert_called_once_with("service-quotas", region_name="us-west-2")

    def test_default_region(self):
        with mock.patch.object(quota.boto3, "client") as mk:
            service_quotas_client()
            mk.assert_called_once_with("service-quotas", region_name=DEFAULT_REGION)


if __name__ == "__main__":
    unittest.main()
