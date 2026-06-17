"""Shared helpers for the G-series (g6e + g7e) capacity-grab script.

grab_g7e_odcr.py imports from here.
Region is configurable via --region (default: us-east-1).

DESIGN NOTE — this is the COUNT-BASED, MULTI-TYPE grabber for the EC2 G family.
You grab a specified number of EACH type, optionally with explicit per-AZ
counts that differ per type. Everything is counted in INSTANCES (台数), and the
target is a matrix:  (instance_type, az) -> desired instance count.

Both supported sizes are .48xlarge (192 vCPU, 8 GPU) and both live in the same
"Running On-Demand G and VT instances" quota — see quota.py / 配额.md.
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

# vCPU per supported instance type. Used for quota math and logging — the
# stop-gates count INSTANCES, not cores. Fixed to the two .48xlarge G-series
# sizes by requirement (no size fallback).
VCPU = {
    "g6e.48xlarge": 192,
    "g7e.48xlarge": 192,
}
SUPPORTED_TYPES = sorted(VCPU)          # ["g6e.48xlarge", "g7e.48xlarge"]
# The type used by the backward-compatible single-type flags
# (--target-count / --per-az-count) when no --counts/--az-counts is given.
DEFAULT_TYPE = "g7e.48xlarge"

# Tag stamped on every reservation we create, so --list / --cancel-all /
# held_count can find exactly ours. Covers both g6e and g7e.
TAG_KEY = "purpose"
TAG_VAL = "g-grab"

# All logs/ledgers/state live next to the scripts, regardless of cwd.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
GRAB_LEDGER = os.path.join(LOGS_DIR, "grabs.jsonl")
PLAN_FILE = os.path.join(LOGS_DIR, "plan.json")

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

    logfile: base name like 'grab_g7e_odcr.log'. Written under logs/ with
             rotation (5 MB x 5 backups) so it never fills the disk.
    """
    logger = logging.getLogger("g-grab")
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


def record_grab(via, itype, az, count, type_total, type_target, region,
                dry_run, az_cap=None, az_total=None, grand_total=None,
                grand_target=None):
    """Append one JSON line to the ledger every time we secure capacity.

    Machine-readable feed for downstream tooling (parsers, dashboards, etc.).
    Skipped during dry-run so the ledger only ever holds real grabs.

    itype:        the instance type secured (g6e.48xlarge / g7e.48xlarge).
    count:        instances secured by THIS grab (always 1 — one ODCR per call).
    type_total:   total instances of THIS type held after this grab.
    type_target:  overall instance target for this type.
    az_cap:       per-(type,az) cap in effect (None if no per-AZ cap).
    az_total:     instances of this type held in THIS az after this grab.
    grand_total:  total instances held across ALL types after this grab.
    grand_target: total instances targeted across ALL types.
    """
    if dry_run:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "via": via,                       # "odcr"
        "instance_type": itype,
        "az": az,
        "region": region,
        "count": count,                   # instances this grab (1)
        "type_total": type_total,         # this type held after this grab
        "type_target": type_target,       # this type's overall target
        "az_cap": az_cap,                 # per-(type,az) cap (null = none)
        "az_total": az_total,             # this type held in this az after grab
        "grand_total": grand_total,       # all types held after this grab
        "grand_target": grand_target,     # all types targeted
    }
    with open(GRAB_LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def save_plan(cells, type_targets, region):
    """Persist the resolved plan so `--list` can show progress without
    re-typing it. `cells` is {(type,az): cap_or_None}; `type_targets` is
    {type: total}. Keys are stored as 'type@az' strings (JSON has no tuple
    keys). Callers pass it only on live runs.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    data = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "region": region,
        "cells": {"%s@%s" % (t, az): cap for (t, az), cap in cells.items()},
        "type_targets": dict(type_targets),
    }
    with open(PLAN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_plan():
    """Read the last saved plan. Returns (cells, type_targets):
      cells        -> {(type,az): cap_or_None}
      type_targets -> {type: total}
    Both empty if no plan file. So plain `--list` can show progress.
    """
    try:
        with open(PLAN_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}, {}
    cells = {}
    for key, cap in data.get("cells", {}).items():
        if "@" in key:
            t, az = key.split("@", 1)
            cells[(t, az)] = cap
    return cells, dict(data.get("type_targets", {}))


def ec2_client(region=DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


def resolve_azs(all_azs, requested):
    """Lock the sweep to a caller-supplied AZ list.

    all_azs:   AZ names actually available in the region (from list_azs).
    requested: AZ names (e.g. ['us-east-1b','us-east-1d']);
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


def offered_by_az(client, types):
    """Set of (instance_type, az) combos actually offered, so we skip
    guaranteed-fail CreateCapacityReservation calls.

    types: iterable of instance types to query (our supported set).
    """
    resp = client.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[{"Name": "instance-type", "Values": list(types)}],
    )
    return {(o["InstanceType"], o["Location"])
            for o in resp["InstanceTypeOfferings"]}


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
