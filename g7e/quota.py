"""G/VT On-Demand vCPU quota preflight for the G-series (g6e + g7e) grabber.

g6e and g7e both belong to the EC2 **G** family, so they share ONE Service
Quota: "Running On-Demand G and VT instances" (quota code L-DB2E81BA), measured
in **vCPUs** — NOT instance count. Each .48xlarge is 192 vCPU, so the total
vCPU you need is the sum over every type of (count x 192).

This module is the small, testable core behind `grab_g7e_odcr.py --check-quota`.
The functions take an injected boto3 service-quotas client so they can be unit
tested with a mock (no AWS calls).

NOTE: a quota increase is approved ASYNCHRONOUSLY by AWS and a sufficient quota
does NOT guarantee capacity — you still have to grab an ODCR. See 配额.md.
"""
import boto3

from common import DEFAULT_REGION, VCPU

# Service Quotas identifiers for the G/VT On-Demand vCPU limit.
SERVICE_CODE = "ec2"
G_VT_QUOTA_CODE = "L-DB2E81BA"
G_VT_QUOTA_NAME = "Running On-Demand G and VT instances"


def service_quotas_client(region=DEFAULT_REGION):
    """A boto3 service-quotas client (separate API from EC2)."""
    return boto3.client("service-quotas", region_name=region)


def vcpus_for_counts(counts):
    """vCPUs required for a {instance_type: count} map (sum of count x vCPU).

    Unknown types contribute 0 (caller validates types elsewhere).
    """
    return sum(VCPU.get(t, 0) * n for t, n in counts.items())


def get_g_vt_quota(client):
    """Current APPLIED G/VT On-Demand vCPU quota value (float).

    Reads the live applied value via GetServiceQuota. Pending increase
    requests are not reflected here (use the Service Quotas console /
    request-history to see those).
    """
    resp = client.get_service_quota(
        ServiceCode=SERVICE_CODE, QuotaCode=G_VT_QUOTA_CODE)
    return resp["Quota"]["Value"]


def check_quota(client, counts):
    """Preflight the shared G/VT quota against a desired {type: count} map.

    Returns a dict:
      current_vcpu   - applied G/VT vCPU quota
      needed_vcpu    - sum over types of count x 192
      counts         - echoed input map
      per_type_vcpu  - {type: count x vCPU}
      sufficient     - True if current_vcpu >= needed_vcpu
    """
    current = get_g_vt_quota(client)
    needed = vcpus_for_counts(counts)
    return {
        "quota_code": G_VT_QUOTA_CODE,
        "quota_name": G_VT_QUOTA_NAME,
        "current_vcpu": current,
        "needed_vcpu": needed,
        "counts": dict(counts),
        "per_type_vcpu": {t: VCPU.get(t, 0) * n for t, n in counts.items()},
        "sufficient": current >= needed,
    }
