"""Shared helpers for the i4i capacity-grab scripts.

Both grab_ondemand.py and grab_odcr.py import from here.
Region is configurable via --region (default: us-east-1).
"""
import os
import json
import time
import random
import logging
import datetime
from logging.handlers import RotatingFileHandler

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"

# All logs/ledgers live next to the scripts, regardless of cwd.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
GRAB_LEDGER = os.path.join(LOGS_DIR, "grabs.jsonl")

# vCPU per i4i size — used to count progress toward the core target.
VCPU = {
    "i4i.large": 2,
    "i4i.xlarge": 4,
    "i4i.2xlarge": 8,
    "i4i.4xlarge": 16,
    "i4i.8xlarge": 32,
    "i4i.12xlarge": 48,
    "i4i.16xlarge": 64,
    "i4i.24xlarge": 96,
    "i4i.32xlarge": 128,
    # i4g fallback fleet
    "i4g.large": 2,
    "i4g.xlarge": 4,
    "i4g.2xlarge": 8,
    "i4g.4xlarge": 16,
    "i4g.8xlarge": 32,
    "i4g.16xlarge": 64,
}

# Priority order: LARGE sizes first (grab big chunks to hit the core target
# with fewer instances/reservations and fewer API calls; fall back to smaller
# sizes when big blocks aren't available).
DEFAULT_PRIORITY = [
    "i4i.8xlarge",
    "i4i.4xlarge",
    "i4i.2xlarge",
    "i4i.xlarge",
    "i4i.large",
]

# Errors that just mean "no capacity here, move on" — NOT a script failure.
CAPACITY_ERRORS = {
    "InsufficientInstanceCapacity",
    "InsufficientCapacity",
    "Unsupported",  # type not offered in this AZ
    "InsufficientHostCapacity",
}
# Throttling → back off and retry the SAME target.
THROTTLE_ERRORS = {"RequestLimitExceeded", "Throttling", "ThrottlingException"}
# DryRun "success" sentinel.
DRYRUN_OK = "DryRunOperation"


def setup_logging(logfile=None):
    """Console + (optional) rotating file logger.

    logfile: base name like 'grab_odcr.log'. Written under logs/ with
             rotation (5 MB x 5 backups) so it never fills the disk.
    """
    logger = logging.getLogger("i4i-grab")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    # console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # rotating file
    if logfile:
        os.makedirs(LOGS_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(LOGS_DIR, logfile),
            maxBytes=5 * 1024 * 1024, backupCount=5,
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def record_grab(via, itype, az, vcpu, total, target, region, dry_run):
    """Append one JSON line to the ledger every time we secure capacity.

    Machine-readable feed for downstream tooling (parsers, dashboards, etc.).
    Skipped during dry-run so the ledger only ever holds real grabs.
    """
    if dry_run:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "via": via,                # "ondemand" | "odcr"
        "instance_type": itype,
        "az": az,
        "region": region,
        "vcpu": vcpu,
        "total_vcpu": total,
        "target_vcpu": target,
    }
    with open(GRAB_LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def ec2_client(region=DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


def list_azs(client):
    """All available AZs in the region."""
    resp = client.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    return sorted(z["ZoneName"] for z in resp["AvailabilityZones"])


def subnets_by_az(client):
    """Map AZ -> a usable subnet id (needed for RunInstances)."""
    resp = client.describe_subnets()
    out = {}
    for s in resp["Subnets"]:
        # prefer default-for-az, but accept any subnet as fallback
        az = s["AvailabilityZone"]
        if az not in out or s.get("DefaultForAz"):
            out[az] = s["SubnetId"]
    return out


def offered_types_by_az(client, types):
    """Which (type, az) combos are actually offered, so we skip impossible calls."""
    resp = client.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[{"Name": "instance-type", "Values": types}],
    )
    combos = set()
    for o in resp["InstanceTypeOfferings"]:
        combos.add((o["InstanceType"], o["Location"]))
    return combos


def backoff_sleep(attempt, base=1.0, cap=20.0):
    """Exponential backoff with full jitter."""
    delay = min(cap, base * (2 ** attempt))
    time.sleep(random.uniform(0, delay))


def classify(err: ClientError):
    """Return one of: 'dryrun_ok', 'capacity', 'throttle', 'fatal'."""
    code = err.response.get("Error", {}).get("Code", "")
    if code == DRYRUN_OK:
        return "dryrun_ok"
    if code in CAPACITY_ERRORS:
        return "capacity"
    if code in THROTTLE_ERRORS:
        return "throttle"
    return "fatal"
