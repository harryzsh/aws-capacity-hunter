"""Shared helpers for the ODCR capacity-grab script.

grab_odcr.py imports from here.
Region is configurable via --region (default: us-east-1).
"""
import os
import re
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

# Known instance types (vCPU per size). Used as an "AWS knows this type"
# pre-validated set so the default i4i/i4g path never calls
# DescribeInstanceTypes; other types are validated via ensure_vcpu().
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
    for h in logger.handlers:
        h.close()
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


def record_grab(via, itype, az, region, dry_run,
                held_count=None, target_count=None):
    """Append one JSON line to the ledger every time we secure ONE instance.

    Machine-readable feed for downstream tooling (parsers, dashboards, etc.).
    Skipped during dry-run so the ledger only ever holds real grabs.

    held_count:   instances of this type held after this grab.
    target_count: this type's --per-type target (lets --list show progress
                  without re-typing the targets).
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
        "held_count": held_count,
        "target_count": target_count,
    }
    with open(GRAB_LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def ec2_client(region=DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


# AWS names the offending types in the InvalidInstanceType message, e.g.
# "The following supplied instance types do not exist: [g7.48xlarge, x.y]".
_BAD_TYPE_RE = re.compile(r"\[([^\]]*)\]")


def _bad_types_from_error(err):
    """Extract the instance-type names AWS flagged in an InvalidInstanceType
    error message. Returns a set (empty if the message can't be parsed)."""
    msg = err.response.get("Error", {}).get("Message", "")
    m = _BAD_TYPE_RE.search(msg)
    if not m:
        return set()
    return {t.strip() for t in m.group(1).split(",") if t.strip()}


def describe_vcpus(client, types):
    """Ask AWS the DefaultVCpus for each instance type in `types`.

    Returns {instance_type: vcpu} for the types AWS recognizes. Types AWS does
    not know are simply absent from the result (the caller decides what to do
    with the gap). Paginates via NextToken. No API call for an empty list.

    Nonexistent type names: real EC2 does NOT return them as "missing" — it
    fails the WHOLE DescribeInstanceTypes call with InvalidInstanceType,
    naming the bad types in the message. We parse those names out, drop them,
    and retry with the survivors, so one typo (`g7.48xlarge`) can't kill a run
    that also asked for valid types — the typo just ends up absent from the
    result (reported as unresolvable by ensure_vcpu). If every requested type
    is bad, the retry set is empty and we return {}.

    This is what lets the grabber target ANY instance type instead of only the
    families baked into the static VCPU table.
    """
    types = list(types)
    out = {}
    while types:
        token = None
        try:
            while True:
                kwargs = {"InstanceTypes": types}
                if token:
                    kwargs["NextToken"] = token
                resp = client.describe_instance_types(**kwargs)
                for it in resp.get("InstanceTypes", []):
                    vcpu = it.get("VCpuInfo", {}).get("DefaultVCpus")
                    if vcpu is not None:
                        out[it["InstanceType"]] = vcpu
                token = resp.get("NextToken")
                if not token:
                    break
            return out
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "InvalidInstanceType":
                raise
            bad = _bad_types_from_error(e)
            survivors = [t for t in types if t not in bad]
            # No parsable bad names, or nothing left to retry -> give up on
            # the rest (they're all unresolvable). Return whatever resolved.
            if not bad or not survivors or survivors == types:
                return out
            types = survivors
    return out


def ensure_vcpu(client, types):
    """Validate every type in `types` against AWS, learning unknown ones.

    For any requested type NOT already in the VCPU table, look it up from AWS
    (one DescribeInstanceTypes call) and insert it — this doubles as the
    "does AWS know this instance type at all?" validation for --per-type, so
    a typo is dropped up front instead of failing on every reserve call.

    Returns (added, unresolvable):
      added        -> {type: vcpu} newly learned and inserted this call
      unresolvable -> requested types AWS could not resolve (caller warns/drops)

    No-op (NO API call) when types is empty or every requested type is already
    known — so the all-i4i default path and `--list` never touch the EC2 API.
    """
    if not types:
        return {}, []
    unknown = []
    for t in types:
        if t not in VCPU and t not in unknown:
            unknown.append(t)
    if not unknown:
        return {}, []
    learned = describe_vcpus(client, unknown)
    VCPU.update(learned)
    added = dict(learned)
    unresolvable = [t for t in unknown if t not in learned]
    return added, unresolvable


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
