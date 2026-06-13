#!/usr/bin/env python3
"""Grab i4i capacity by launching plain On-Demand instances.

Region is configurable via --region (default: us-east-1).

Strategy: sweep AZ x instance-type (large first), RunInstances count=1 each,
keep what launches, count vCPUs toward a target, stop at the cap. Instances
that stay RUNNING hold their capacity — that is how plain On-Demand "holds".

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
Capacity is intermittent, so 24x7 watching is how you actually catch it.
Every real grab is logged and appended to logs/grabs.jsonl.

SAFETY: default is --dry-run (validates IAM + params, launches nothing).
Use --live to actually launch. Use --terminate-tagged to clean up afterward.

Examples:
  python3 grab_ondemand.py --target-cores 8                       # dry-run plan
  python3 grab_ondemand.py --target-cores 8 --live                # really launch
  python3 grab_ondemand.py --target-cores 10000 --live --watch    # 24x7 hunt
  python3 grab_ondemand.py --terminate-tagged --live              # tear down
"""
import argparse
import sys
import time

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, VCPU, DEFAULT_PRIORITY, ec2_client, list_azs, subnets_by_az,
    offered_types_by_az, backoff_sleep, classify, setup_logging,
    record_grab,
)

TAG_KEY = "purpose"
TAG_VAL = "primeday-i4i-grab"
AMI_SSM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

# Logging is ALWAYS on (console + rotating file) — the fallback record of truth.
log = setup_logging("grab_ondemand.log")


def resolve_ami(region):
    import boto3
    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=AMI_SSM)["Parameter"]["Value"]


def launch_one(client, itype, subnet_id, ami, dry_run):
    client.run_instances(
        ImageId=ami,
        InstanceType=itype,
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        DryRun=dry_run,
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": TAG_KEY, "Value": TAG_VAL},
                {"Key": "Name", "Value": f"i4i-grab-{itype}"},
            ],
        }],
    )


def terminate_tagged(client, dry_run):
    resp = client.describe_instances(Filters=[
        {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VAL]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]},
    ])
    ids = [i["InstanceId"] for r in resp["Reservations"] for i in r["Instances"]]
    if not ids:
        log.info("no tagged instances to terminate")
        return
    log.info("terminating %d instance(s): %s", len(ids), ids)
    try:
        client.terminate_instances(InstanceIds=ids, DryRun=dry_run)
        log.info("terminate requested")
    except ClientError as e:
        if classify(e) == "dryrun_ok":
            log.info("[dry-run] terminate would succeed for %s", ids)
        else:
            raise


def _on_grab(args, itype, az, state):
    """Bookkeeping for one secured instance: count, log, ledger, push."""
    state["grabbed"] += VCPU[itype]
    state["launched"].append((itype, az))
    log.info("LAUNCHED %s in %s (+%d vCPU, total %d/%d)",
             itype, az, VCPU[itype], state["grabbed"], args.target_cores)
    record_grab("ondemand", itype, az, VCPU[itype], state["grabbed"],
                args.target_cores, args.region, not args.live)


def sweep_once(client, args, ami, subs, offered, usable_azs, state):
    """One full AZ x type pass. Mutates state in place."""
    priority = args.types or DEFAULT_PRIORITY
    throttle_attempt = 0
    for itype in priority:
        if state["grabbed"] >= args.target_cores:
            return
        for az in usable_azs:
            if state["grabbed"] >= args.target_cores:
                return
            if (itype, az) not in offered:
                continue
            subnet_id = subs[az]
            try:
                launch_one(client, itype, subnet_id, ami, not args.live)
                _on_grab(args, itype, az, state)
                throttle_attempt = 0
            except ClientError as e:
                kind = classify(e)
                if kind == "dryrun_ok":
                    log.info("[dry-run] would launch %s in %s (+%d vCPU)",
                             itype, az, VCPU[itype])
                    _on_grab(args, itype, az, state)
                elif kind == "capacity":
                    log.info("no capacity: %s @ %s — next", itype, az)
                elif kind == "throttle":
                    log.warning("throttled, backing off (attempt %d)", throttle_attempt)
                    backoff_sleep(throttle_attempt)
                    throttle_attempt += 1
                    try:
                        launch_one(client, itype, subnet_id, ami, not args.live)
                        _on_grab(args, itype, az, state)
                    except ClientError as e2:
                        log.info("retry result: %s", classify(e2))
                else:
                    log.error("FATAL on %s @ %s: %s", itype, az, e.response["Error"])
                    raise


def run(args):
    client = ec2_client(args.region)

    if args.terminate_tagged:
        terminate_tagged(client, not args.live)
        return

    ami = resolve_ami(args.region)
    log.info("region=%s ami=%s dry_run=%s target_cores=%d watch=%s",
             args.region, ami, not args.live, args.target_cores, args.watch)

    priority = args.types or DEFAULT_PRIORITY
    azs = list_azs(client)
    subs = subnets_by_az(client)
    offered = offered_types_by_az(client, priority)

    usable_azs = [az for az in azs if az in subs]
    skipped = [az for az in azs if az not in subs]
    if skipped:
        log.warning("AZs WITHOUT a subnet (cannot RunInstances there): %s", skipped)
    log.info("usable AZs (have subnet): %s", usable_azs)

    state = {"grabbed": 0, "launched": []}

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until %d vCPU secured "
                 "(Ctrl-C to stop)", args.interval, args.target_cores)
        rounds = 0
        while state["grabbed"] < args.target_cores:
            rounds += 1
            log.info("--- watch round %d (have %d/%d vCPU) ---",
                     rounds, state["grabbed"], args.target_cores)
            sweep_once(client, args, ami, subs, offered, usable_azs, state)
            if state["grabbed"] >= args.target_cores:
                break
            time.sleep(args.interval)
        log.info("WATCH target reached after %d round(s)", rounds)
    else:
        sweep_once(client, args, ami, subs, offered, usable_azs, state)

    log.info("=== DONE: secured %d/%d vCPU across %d placement(s) ===",
             state["grabbed"], args.target_cores, len(state["launched"]))
    for t, a in state["launched"]:
        log.info("  %s @ %s", t, a)
    if not args.live:
        log.info("(dry-run — nothing was actually launched)")


def main():
    p = argparse.ArgumentParser(description="Grab i4i via plain On-Demand")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="AWS region to target (default %s)" % DEFAULT_REGION)
    p.add_argument("--target-cores", type=int, default=8,
                   help="stop once this many vCPU are secured (default 8)")
    p.add_argument("--types", nargs="*", help="override instance-type priority list")
    p.add_argument("--watch", action="store_true",
                   help="loop forever, re-sweeping until target reached (24x7 hunt)")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between sweeps in --watch mode (default 60)")
    p.add_argument("--live", action="store_true",
                   help="actually launch (default is dry-run)")
    p.add_argument("--terminate-tagged", action="store_true",
                   help="terminate all instances tagged %s=%s" % (TAG_KEY, TAG_VAL))
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
