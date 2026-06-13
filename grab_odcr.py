#!/usr/bin/env python3
"""Grab i4i capacity via On-Demand Capacity Reservations.

Region is configurable via --region (default: us-east-1).

Strategy: sweep AZ x instance-type (large first), CreateCapacityReservation
count=1 each (all-or-nothing per call, so count=1 scavenges fragments), tag
each reservation, count vCPUs toward a target, stop at the cap.

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
Capacity is intermittent, so 24x7 watching is how you actually catch it.
Every real reservation is logged and appended to logs/grabs.jsonl.

Why ODCR over plain On-Demand: a reservation HOLDS the slot even when no
instance occupies it (and across stop/terminate/ASG-rollover). Trade-off:
an ACTIVE reservation bills at the On-Demand rate whether filled or not.

SAFETY: default is --dry-run (validates IAM + params, reserves nothing).
Use --live to actually reserve. Use --cancel-all to release everything.
Immediate-use reservations here have NO commitment and cancel anytime.

Examples:
  python3 grab_odcr.py --target-cores 8                        # dry-run plan
  python3 grab_odcr.py --target-cores 8 --live                 # really reserve
  python3 grab_odcr.py --target-cores 10000 --live --watch     # 24x7 hunt
  python3 grab_odcr.py --cancel-all --live                     # release all
  python3 grab_odcr.py --list                                  # show reservations
"""
import argparse
import sys
import time
import datetime

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, VCPU, DEFAULT_PRIORITY, ec2_client, list_azs,
    offered_types_by_az, backoff_sleep, classify, setup_logging,
    record_grab,
)

TAG_KEY = "purpose"
TAG_VAL = "primeday-i4i-grab"

# Logging is ALWAYS on (console + rotating file) — the fallback record of truth.
log = setup_logging("grab_odcr.log")


def reserve_one(client, itype, az, dry_run, end_hours=None):
    """Create a count=1 ODCR. open matching, no commitment.

    end_hours: if set, EndDateType=limited (auto-expires) as a billing guard.
               if None, EndDateType=unlimited (until you cancel).
    """
    kwargs = dict(
        InstanceType=itype,
        InstancePlatform="Linux/UNIX",
        AvailabilityZone=az,
        InstanceCount=1,
        InstanceMatchCriteria="open",
        Tenancy="default",
        DryRun=dry_run,
        TagSpecifications=[{
            "ResourceType": "capacity-reservation",
            "Tags": [{"Key": TAG_KEY, "Value": TAG_VAL}],
        }],
    )
    if end_hours:
        end = datetime.datetime.utcnow() + datetime.timedelta(hours=end_hours)
        kwargs["EndDateType"] = "limited"
        kwargs["EndDate"] = end
    else:
        kwargs["EndDateType"] = "unlimited"
    return client.create_capacity_reservation(**kwargs)


def list_reservations(client):
    resp = client.describe_capacity_reservations(Filters=[
        {"Name": "state", "Values": ["active", "pending", "assessing"]},
    ])
    rows = []
    for cr in resp["CapacityReservations"]:
        tags = {t["Key"]: t["Value"] for t in cr.get("Tags", [])}
        rows.append((
            cr["CapacityReservationId"], cr["InstanceType"],
            cr["AvailabilityZone"], cr["State"],
            cr.get("TotalInstanceCount"), tags.get(TAG_KEY, ""),
        ))
    return rows


def cancel_all(client, dry_run):
    rows = [r for r in list_reservations(client) if r[5] == TAG_VAL]
    if not rows:
        log.info("no tagged reservations to cancel")
        return
    for crid, itype, az, state, cnt, _ in rows:
        log.info("cancel %s (%s @ %s, %s)", crid, itype, az, state)
        if dry_run:
            log.info("  [dry-run] would cancel")
            continue
        try:
            client.cancel_capacity_reservation(CapacityReservationId=crid)
            log.info("  cancelled")
        except ClientError as e:
            log.error("  cancel failed: %s", e.response.get("Error"))


def _on_grab(args, crid, itype, az, state):
    """Bookkeeping for one secured reservation: count, log, ledger, push."""
    state["reserved"] += VCPU[itype]
    state["made"].append((crid, itype, az))
    log.info("RESERVED %s %s @ %s (+%d vCPU, total %d/%d)",
             crid, itype, az, VCPU[itype], state["reserved"], args.target_cores)
    record_grab("odcr", itype, az, VCPU[itype], state["reserved"],
                args.target_cores, args.region, not args.live)


def sweep_once(client, args, azs, offered, state):
    """One full AZ x type pass. Mutates state in place."""
    priority = args.types or DEFAULT_PRIORITY
    throttle_attempt = 0
    for itype in priority:
        if state["reserved"] >= args.target_cores:
            return
        for az in azs:
            if state["reserved"] >= args.target_cores:
                return
            if (itype, az) not in offered:
                continue
            try:
                resp = reserve_one(client, itype, az, not args.live, args.end_hours)
                crid = resp["CapacityReservation"]["CapacityReservationId"]
                _on_grab(args, crid, itype, az, state)
                throttle_attempt = 0
            except ClientError as e:
                kind = classify(e)
                if kind == "dryrun_ok":
                    log.info("[dry-run] would reserve %s @ %s (+%d vCPU)",
                             itype, az, VCPU[itype])
                    _on_grab(args, "(dry-run)", itype, az, state)
                elif kind == "capacity":
                    log.info("no capacity: %s @ %s — next", itype, az)
                elif kind == "throttle":
                    log.warning("throttled, backing off (attempt %d)", throttle_attempt)
                    backoff_sleep(throttle_attempt)
                    throttle_attempt += 1
                else:
                    log.error("FATAL on %s @ %s: %s", itype, az, e.response["Error"])
                    raise


def run(args):
    client = ec2_client(args.region)

    if args.list:
        rows = list_reservations(client)
        if not rows:
            log.info("no active/pending reservations")
        for crid, itype, az, state, cnt, tag in rows:
            log.info("%s  %-12s %-12s %-9s count=%s tag=%s",
                     crid, itype, az, state, cnt, tag)
        return

    if args.cancel_all:
        cancel_all(client, not args.live)
        return

    log.info("region=%s dry_run=%s target_cores=%d end_hours=%s watch=%s",
             args.region, not args.live, args.target_cores, args.end_hours, args.watch)

    priority = args.types or DEFAULT_PRIORITY
    azs = list_azs(client)
    offered = offered_types_by_az(client, priority)
    log.info("AZs: %s", azs)

    state = {"reserved": 0, "made": []}

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until %d vCPU reserved "
                 "(Ctrl-C to stop)", args.interval, args.target_cores)
        rounds = 0
        while state["reserved"] < args.target_cores:
            rounds += 1
            log.info("--- watch round %d (have %d/%d vCPU) ---",
                     rounds, state["reserved"], args.target_cores)
            sweep_once(client, args, azs, offered, state)
            if state["reserved"] >= args.target_cores:
                break
            time.sleep(args.interval)
        log.info("WATCH target reached after %d round(s)", rounds)
    else:
        sweep_once(client, args, azs, offered, state)

    log.info("=== DONE: reserved %d/%d vCPU across %d reservation(s) ===",
             state["reserved"], args.target_cores, len(state["made"]))
    for crid, t, a in state["made"]:
        log.info("  %s %s @ %s", crid, t, a)
    if not args.live:
        log.info("(dry-run — nothing was actually reserved, no billing)")
    else:
        log.warning("LIVE reservations are billing NOW at On-Demand rate.")
        log.warning("Run: python3 grab_odcr.py --cancel-all --live  to stop.")


def main():
    p = argparse.ArgumentParser(description="Grab i4i via On-Demand Capacity Reservations")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="AWS region to target (default %s)" % DEFAULT_REGION)
    p.add_argument("--target-cores", type=int, default=8,
                   help="stop once this many vCPU are reserved (default 8)")
    p.add_argument("--types", nargs="*", help="override instance-type priority list")
    p.add_argument("--end-hours", type=float, default=None,
                   help="auto-expire reservations after N hours (billing guard)")
    p.add_argument("--watch", action="store_true",
                   help="loop forever, re-sweeping until target reached (24x7 hunt)")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between sweeps in --watch mode (default 60)")
    p.add_argument("--live", action="store_true",
                   help="actually reserve (default is dry-run)")
    p.add_argument("--cancel-all", action="store_true",
                   help="cancel all reservations tagged %s=%s" % (TAG_KEY, TAG_VAL))
    p.add_argument("--list", action="store_true",
                   help="list current reservations and exit")
    args = p.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("interrupted — stopping watch")
    except ClientError as e:
        log.error("AWS error: %s", e.response.get("Error"))
        sys.exit(1)


if __name__ == "__main__":
    main()
