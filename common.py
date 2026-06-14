"""Shared helpers for the i4i capacity-grab script.

grab_odcr.py imports from here.
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

# DEFAULT: only i4i.16xlarge. The requirement is "must be 16xl by default".
# To allow fallback to other sizes, pass --types explicitly; whatever you pass
# is auto-sorted LARGE-first (see resolve_types) so big blocks go first.
DEFAULT_PRIORITY = ["i4i.16xlarge"]

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


def record_grab(via, itype, az, vcpu, total, target, region, dry_run,
                per_az_cores=None, per_az_total=None):
    """Append one JSON line to the ledger every time we secure capacity.

    Machine-readable feed for downstream tooling (parsers, dashboards, etc.).
    Skipped during dry-run so the ledger only ever holds real grabs.

    per_az_cores:  the --per-az-cores cap in effect (None if not balanced mode).
    per_az_total:  cores held in THIS az after this grab (so the ledger shows
                   how the balanced run progresses per AZ, not just the total).
    """
    if dry_run:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "via": via,                # "odcr"
        "instance_type": itype,
        "az": az,
        "region": region,
        "vcpu": vcpu,
        "total_vcpu": total,
        "target_vcpu": target,
        "per_az_cores": per_az_cores,    # cap per AZ (null = not balanced mode)
        "per_az_total": per_az_total,    # cores held in this AZ after this grab
    }
    with open(GRAB_LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def ec2_client(region=DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


def resolve_types(types):
    """Normalize the instance-type priority list.

    - None / empty  -> DEFAULT_PRIORITY (just i4i.16xlarge).
    - Otherwise     -> the given list, auto-sorted LARGE-first by vCPU so the
                       caller never has to worry about ordering. Unknown types
                       (not in VCPU) are dropped with the list of drops returned
                       so the caller can warn.
    Returns (ordered_types, dropped_unknown).
    """
    if not types:
        return list(DEFAULT_PRIORITY), []
    known = [t for t in types if t in VCPU]
    dropped = [t for t in types if t not in VCPU]
    ordered = sorted(known, key=lambda t: VCPU[t], reverse=True)
    return ordered, dropped


def resolve_azs(all_azs, requested):
    """Lock the sweep to a caller-supplied AZ list.

    all_azs:   AZ names actually available in the region (from list_azs).
    requested: AZ names passed via --azs (e.g. ['us-east-1c','us-east-1d']);
               None/empty means "use every available AZ".
    Returns (selected_azs, missing) where missing are requested AZs that don't
    exist in the region (so the caller can warn instead of silently dropping).
    """
    if not requested:
        return list(all_azs), []
    avail = set(all_azs)
    selected = [az for az in requested if az in avail]
    missing = [az for az in requested if az not in avail]
    return selected, missing


def list_azs(client):
    """All available AZs in the region."""
    resp = client.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    return sorted(z["ZoneName"] for z in resp["AvailabilityZones"])


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
