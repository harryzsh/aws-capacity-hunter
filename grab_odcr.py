#!/usr/bin/env python3
"""Grab EC2 capacity via On-Demand Capacity Reservations, by INSTANCE COUNT.

Region is configurable via --region (default: us-east-1).

Strategy: you say how many instances of each type you want
(--per-type TYPE:COUNT ...); the script sweeps the target AZs and secures
them one instance at a time (all-or-nothing per call, so +1 scavenges
fragments), tags every reservation, and stops when every type hits its count.

CONSOLIDATION: the account holds ONE reservation object per (type, AZ). The
first grab of a (type, AZ) creates it (count=1); every later grab GROWS it by
+1 via ModifyCapacityReservation instead of creating another object. Modify
has the same capacity semantics as create (needs a free slot in the pool,
all-or-nothing), so the grab success rate is identical — only the object
count shrinks: e.g. 156 instances = 1-2 objects, not 156.

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
Capacity is intermittent, so 24x7 watching is how you actually catch it.
Every real grab is logged and appended to logs/grabs.jsonl.

RESUME / IDEMPOTENCY: the per-type stop-gates read what we ACTUALLY hold from
AWS each round (held_count_by_type), counting INSTANCES. So a crash/restart/
host-reboot picks up exactly where it left off: each type is judged against
its own real held count and the sweep only tops up the true shortfall — no
in-memory counter to lose, no double-grab. Safe under systemd Restart=always.

Why ODCR over plain On-Demand: a reservation HOLDS the slot even when no
instance occupies it (and across stop/terminate/ASG-rollover). Trade-off:
an ACTIVE reservation bills at the On-Demand rate whether filled or not.

SAFETY: default is --dry-run (validates IAM + params, reserves nothing).
Use --live to actually reserve. Use --cancel-all to release everything.
Immediate-use reservations here have NO commitment and cancel anytime.

Examples:
  python3 grab_odcr.py --per-type i4i.16xlarge:10                # dry-run plan
  python3 grab_odcr.py --per-type i4i.16xlarge:10 --live         # really reserve
  python3 grab_odcr.py --per-type i4i.16xlarge:100 i4i.8xlarge:50 \\
      --azs us-east-1b us-east-1d --live --watch --interval 30   # 24x7 hunt
  python3 grab_odcr.py --cancel-all --live                       # release all
  python3 grab_odcr.py --list                                    # show progress
"""
import argparse
import sys
import time
import json

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, GRAB_LEDGER, ec2_client, list_azs,
    offered_types_by_az, backoff_sleep, classify, setup_logging,
    record_grab, resolve_azs, ensure_vcpu,
)

TAG_KEY = "purpose"
TAG_VAL = "primeday-i4i-grab"

# Logging is ALWAYS on (console + rotating file) — the fallback record of truth.
log = setup_logging("grab_odcr.log")


def reserve_one(client, itype, az, dry_run):
    """Create a count=1 ODCR. open matching, no commitment, no end date
    (holds until you --cancel-all)."""
    kwargs = dict(
        InstanceType=itype,
        InstancePlatform="Linux/UNIX",
        AvailabilityZone=az,
        InstanceCount=1,
        InstanceMatchCriteria="open",
        Tenancy="default",
        # Nitro families have EBS optimization always-on. Mark the reservation
        # EBS-optimized so its attributes match the instances it will hold.
        # NOTE: EbsOptimized is NOT one of the `open` match criteria (those
        # are instance type / platform / AZ / tenancy only), so this neither
        # helps nor blocks matching — it's honest attribute alignment only.
        EbsOptimized=True,
        DryRun=dry_run,
        TagSpecifications=[{
            "ResourceType": "capacity-reservation",
            "Tags": [{"Key": TAG_KEY, "Value": TAG_VAL}],
        }],
        EndDateType="unlimited",
    )
    return client.create_capacity_reservation(**kwargs)


def growable_map(client):
    """{(type, az): [crid, count]} of OUR active reservations — grow targets.

    One entry per (type, az): the reservation a sweep should GROW (+1 via
    ModifyCapacityReservation) instead of creating another count=1 object.
    Only our tag and only 'active' state qualify (pending/assessing can't be
    modified). If legacy count=1 piles exist for the same (type, az), the last
    one listed wins — new capacity consolidates onto it and the others just
    stay as they are until --cancel-all.
    """
    m = {}
    for crid, itype, az, state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or state != "active":
            continue
        m[(itype, az)] = [crid, cnt or 0]
    return m


def secure_one(client, itype, az, dry_run, growable=None):
    """Secure ONE instance of (type, az): GROW our existing reservation by +1
    when we already hold one, CREATE a count=1 reservation only when we don't.
    Returns the reservation id that now holds the new slot.

    This is what keeps the account at one reservation OBJECT per (type, az)
    while the grab granularity stays one instance at a time (count=1-equivalent
    all-or-nothing: ModifyCapacityReservation to count+1 either fully succeeds
    or changes nothing).

    growable: the {(type, az): [crid, count]} map from growable_map(). Mutated
        in place on success (count += 1, or a fresh entry after a create) so
        the map stays accurate WITHIN a sweep between AWS re-reads — same
        contract as the `held` gates. ModifyCapacityReservation takes an
        ABSOLUTE count, so an accurate local count is what makes +1 mean +1.

    dry-run always takes the create path (raises DryRunOperation like before),
    never calls Modify — the dry-run plan and its classification are unchanged.

    Capacity/throttle errors from EITHER call propagate for classify(); the
    one error handled here is the growable entry having been cancelled behind
    our back (NotFound) — drop it and fall back to create.
    """
    if growable is None:
        growable = {}
    key = (itype, az)
    if not dry_run and key in growable:
        crid, cnt = growable[key]
        try:
            client.modify_capacity_reservation(
                CapacityReservationId=crid, InstanceCount=cnt + 1)
            growable[key][1] = cnt + 1
            return crid
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "InvalidCapacityReservationId.NotFound":
                raise
            growable.pop(key, None)
    resp = reserve_one(client, itype, az, dry_run)
    crid = resp["CapacityReservation"]["CapacityReservationId"]
    growable[key] = [crid, 1]
    return crid


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
            cr.get("AvailableInstanceCount"),   # 7th: free slots (Total - used)
        ))
    return rows


def held_count_by_type(client, types, only_azs=None):
    """Sum instances held per type across THIS script's tagged reservations,
    read LIVE from AWS. Counts INSTANCES (TotalInstanceCount) summed over ALL
    matching reservation objects — one reservation can hold many instances,
    and one type can have one object per AZ.

    types: the set/dict of types we care about; others are ignored so unrelated
        tagged reservations never inflate a per-type gate.

    only_azs: if given (a set/list of AZ names), count ONLY those AZs. This
        keeps the stop-gate in scope when you target a subset of AZs: e.g.
        holding 24 in us-east-1a, `--azs us-east-1b --per-type t:5` must grab
        5 in 1b — 1a's out-of-scope stock must not satisfy the target.

    This is the per-type stop-gate's source of truth. Re-reading it every round
    is what makes restarts safe: after a crash we see exactly how many of each
    type we already hold and only top up the real shortfall — never double-grab.
    """
    held = {}
    for _crid, itype, az, _state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or itype not in types:
            continue
        if only_azs is not None and az not in only_azs:
            continue
        held[itype] = held.get(itype, 0) + (cnt or 1)
    return held


def parse_per_type(pairs):
    """Parse --per-type 'TYPE:COUNT' tokens into an ordered {type: count} dict.

    Each entry is an INDEPENDENT instance-count target — grab exactly COUNT
    instances of TYPE, judged on its own. A dry type never blocks the others.

    Accepts 'i4i.16xlarge:10'. COUNT must be a positive integer. A later
    duplicate of the same type wins (last-write) so a typo can be re-stated.
    Returns (targets, errors): targets is an Ordered(dict), errors a list of
    human-readable strings for tokens we couldn't use (caller warns/aborts).
    """
    targets, errors = {}, []
    for tok in pairs or []:
        if ":" not in tok:
            errors.append("%r: expected TYPE:COUNT" % tok)
            continue
        itype, _, cnt = tok.rpartition(":")
        itype = itype.strip()
        if not itype:
            errors.append("%r: empty instance type" % tok)
            continue
        try:
            n = int(cnt)
        except ValueError:
            errors.append("%r: count %r is not an integer" % (tok, cnt))
            continue
        if n <= 0:
            errors.append("%r: count must be > 0" % tok)
            continue
        targets[itype] = n   # last-write-wins on duplicate type
    return targets, errors


def _targets_from_ledger():
    """Read the most recent per-type targets from grabs.jsonl.

    So `--list` ALONE can show progress — it remembers what count you were
    grabbing each type toward, no need to re-type --per-type. The latest
    line per type wins. Returns {type: target_count}, possibly empty.
    Old core-denominated ledgers (target_vcpu, no target_count) are ignored.
    """
    targets = {}
    try:
        with open(GRAB_LEDGER) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                itype = rec.get("instance_type")
                tgt = rec.get("target_count")
                if itype and isinstance(tgt, int):
                    targets[itype] = tgt
    except FileNotFoundError:
        pass
    return targets


def print_list(client, targets=None):
    """--list: show every tagged reservation, then a per-type + total summary.

    The summary answers the two questions you actually have during a grab:
    "how many instances of each type do I hold?" and "how many in total?"

    Each row also shows USED/free — whether an instance is actually occupying
    that reservation (Total - Available > 0) — and the summary tallies how many
    reservations are USED out of the total we hold.

    Progress (held/target + FULL/short) is shown automatically: if you don't
    pass targets, they're read from grabs.jsonl (latest line per type) — so
    plain `--list` already shows how close you are.
    """
    if targets is None:
        targets = _targets_from_ledger()
    rows = list_reservations(client)
    if not rows:
        log.info("no active/pending reservations")
        return
    used_n = 0   # reservations with an instance actually IN them (used > 0)
    ours_n = 0   # our tagged reservations (the denominator)
    held = {}    # {type: instances} across our tagged reservations
    for crid, itype, az, state, cnt, tag, avail in rows:
        # USED = is an instance occupying this reservation? (Total - Available)
        total = cnt or 0
        free = avail if avail is not None else total
        used = total - free
        used_str = "USED" if used > 0 else "free"
        log.info("%s  %-12s %-12s %-9s count=%s %-4s tag=%s",
                 crid, itype, az, state, cnt, used_str, tag)
        if tag == TAG_VAL:
            ours_n += 1
            held[itype] = held.get(itype, 0) + total
            if used > 0:
                used_n += 1
    if held:
        log.info("--- summary (tag=%s) ---", TAG_VAL)
        for itype in sorted(held):
            got = held[itype]
            tgt = targets.get(itype)
            if tgt:
                flag = "FULL" if got >= tgt else "short"
                log.info("  %-14s %4d / %d instances [%s]", itype, got, tgt, flag)
            else:
                log.info("  %-14s %4d instances", itype, got)
        log.info("  %-14s %4d instances  across %d type(s)",
                 "TOTAL", sum(held.values()), len(held))
        # How many reservations actually have an instance in them.
        log.info("  %-14s %d / %d reservations USED (have an instance running)",
                 "USED", used_n, ours_n)


def cancel_all(client, dry_run):
    rows = [r for r in list_reservations(client) if r[5] == TAG_VAL]
    if not rows:
        log.info("no tagged reservations to cancel")
        return
    for crid, itype, az, state, cnt, _tag, _avail in rows:
        log.info("cancel %s (%s @ %s, %s, count=%s)", crid, itype, az, state, cnt)
        if dry_run:
            log.info("  [dry-run] would cancel")
            continue
        try:
            client.cancel_capacity_reservation(CapacityReservationId=crid)
            log.info("  cancelled")
        except ClientError as e:
            log.error("  cancel failed: %s", e.response.get("Error"))


def _on_grab(args, crid, itype, az, held, made):
    """Bookkeeping for one secured instance: bump the held count for this
    type, log, and append to the ledger.

    held is {type: instances}, the per-type gate (seeded from AWS truth each
    round); we bump it locally so the gate stays accurate WITHIN a sweep,
    between AWS re-reads.
    """
    held[itype] = held.get(itype, 0) + 1
    made.append((crid, itype, az))
    tgt = args.per_type[itype]
    log.info("RESERVED %s %s @ %s (%s: %d/%d instances)",
             crid, itype, az, itype, held[itype], tgt)
    record_grab("odcr", itype, az, args.region, not args.live,
                held_count=held[itype], target_count=tgt)


def sweep_once_per_type(client, args, azs, offered, held, made):
    """One pass: for each type still short of its own instance-count target,
    grab one instance at a time across the AZs (cycling AZs) until that type
    is full or its AZs are exhausted this pass. Mutates held/made in place.

    Each type is judged on its OWN target from --per-type: a dry type is simply
    skipped, it never stops the others. No shared gate.

    Grabs go through secure_one (grow-or-create) — one reservation OBJECT per
    (type, az), not piles of count=1.
    """
    growable = growable_map(client) if args.live else {}
    throttle_attempt = 0
    for itype in args.per_type:
        target = args.per_type[itype]
        # Loop AZs repeatedly until this type hits its target or a full pass
        # over the AZs grabs nothing (capacity exhausted for it right now).
        while held.get(itype, 0) < target:
            progressed = False
            for az in azs:
                if held.get(itype, 0) >= target:
                    break
                if (itype, az) not in offered:
                    continue
                try:
                    crid = secure_one(client, itype, az, not args.live,
                                      growable)
                    _on_grab(args, crid, itype, az, held, made)
                    progressed = True
                    throttle_attempt = 0
                except ClientError as e:
                    kind = classify(e)
                    if kind == "dryrun_ok":
                        log.info("[dry-run] would reserve %s @ %s (1 instance)",
                                 itype, az)
                        _on_grab(args, "(dry-run)", itype, az, held, made)
                        progressed = True
                    elif kind == "capacity":
                        log.info("no capacity: %s @ %s — next", itype, az)
                    elif kind == "throttle":
                        log.warning("throttled, backing off (attempt %d)", throttle_attempt)
                        backoff_sleep(throttle_attempt)
                        throttle_attempt += 1
                        progressed = True  # retry same target, don't call it dry
                    else:
                        log.error("FATAL on %s @ %s: %s", itype, az, e.response["Error"])
                        raise
            if not progressed:
                # A full AZ pass grabbed nothing and wasn't throttled: this type
                # has no capacity anywhere right now. Stop hammering it — the
                # --watch loop will retry next round.
                log.info("%s: no capacity in any target AZ this pass "
                         "(%d/%d) — moving on", itype, held.get(itype, 0), target)
                break


def run(args):
    client = ec2_client(args.region)

    if args.list:
        print_list(client)     # targets auto-read from grabs.jsonl
        return

    if args.cancel_all:
        cancel_all(client, not args.live)
        return

    if not args.per_type:
        log.error("nothing to do: pass --per-type TYPE:COUNT ... "
                  "(or --list / --cancel-all)")
        return

    # --add is a one-shot manual top-up ("N MORE on top of what I hold").
    # It must never loop or restart-resume: under --watch (and thus under
    # systemd Restart=always) every re-entry would re-add N on top of the new
    # total — unbounded growth. And a dry-run "+N" has no stable base to add
    # onto. Refuse both combinations outright.
    if getattr(args, "add", False):
        if args.watch:
            log.error("--add cannot be combined with --watch: every restart "
                      "would add N more on top of the new total (unbounded "
                      "growth under systemd Restart=always). Run --add as a "
                      "one-shot, or set an absolute --per-type target for "
                      "the watcher.")
            return
        if not args.live:
            log.error("--add requires --live: '+N more' needs the real held "
                      "count from AWS as its base. Preview the plan with an "
                      "absolute --per-type target instead.")
            return

    log.info("region=%s dry_run=%s targets=%s%s watch=%s",
             args.region, not args.live, dict(args.per_type),
             " (ADD: +N on top of held)" if getattr(args, "add", False) else "",
             args.watch)

    # Validate every requested type against AWS (DescribeInstanceTypes) so a
    # typo is dropped up front instead of failing on every reserve call.
    # Known i4i/i4g sizes skip the API call entirely.
    types = list(args.per_type)
    added, unresolvable = ensure_vcpu(client, types)
    if added:
        log.info("validated new types with AWS: %s", sorted(added))
    if unresolvable:
        log.warning("AWS could not resolve these instance types (dropped): %s",
                    unresolvable)
        for t in unresolvable:
            args.per_type.pop(t, None)
        types = list(args.per_type)
    if not types:
        log.error("no usable instance types in --per-type — nothing to do")
        return

    all_azs = list_azs(client)
    azs, missing = resolve_azs(all_azs, args.azs)
    if missing:
        log.warning("requested AZs not present in %s (ignored): %s", args.region, missing)
    if not azs:
        log.error("no usable AZs after applying --azs %s — nothing to do", args.azs)
        return
    offered = offered_types_by_az(client, types)

    log.info("targets=%s  AZs=%s", {t: args.per_type[t] for t in types}, azs)

    def _done(held):
        return all(held.get(t, 0) >= args.per_type[t] for t in types)

    made = []
    # LIVE: seed from AWS so a restart resumes each type where it left off.
    # dry-run: start empty and simulate locally for a clean plan preview.
    #
    # IMPORTANT: count ONLY the AZs we're sweeping (only_azs=set(azs)). If you
    # target one AZ (--azs us-east-1b) but already hold stock in another
    # (us-east-1a), that out-of-scope stock would satisfy the gate and stop
    # the run before the targeted AZ gets anything.
    scope = set(azs)
    held = (held_count_by_type(client, args.per_type, only_azs=scope)
            if args.live else {})
    if args.live and held:
        log.info("resumed from AWS: already hold %s (in target AZs %s)",
                 held, azs)

    # --add: convert "+N more" into an absolute target of held + N, computed
    # ONCE from the AWS truth read above. From here on the normal absolute
    # machinery (gates, sweeps, resume within this process) works unchanged.
    if getattr(args, "add", False):
        args.per_type = {t: held.get(t, 0) + n for t, n in args.per_type.items()}
        log.info("ADD mode: effective targets = held + N -> %s",
                 dict(args.per_type))

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until every type hits its "
                 "target (Ctrl-C to stop)", args.interval)
        rounds = 0
        while not _done(held):
            rounds += 1
            # Re-read AWS truth each round (live), same AZ scope as the seed.
            if args.live:
                held = held_count_by_type(client, args.per_type, only_azs=scope)
            log.info("--- watch round %d (have %s) ---", rounds,
                     {t: held.get(t, 0) for t in types})
            sweep_once_per_type(client, args, azs, offered, held, made)
            if _done(held):
                break
            time.sleep(args.interval)
        log.info("WATCH: all targets reached after %d round(s)", rounds)
    else:
        sweep_once_per_type(client, args, azs, offered, held, made)

    log.info("=== DONE: %s (this run secured %d instance(s)) ===",
             {t: held.get(t, 0) for t in types}, len(made))
    for t in types:
        got = held.get(t, 0)
        flag = "FULL" if got >= args.per_type[t] else "short"
        log.info("  %-14s %d/%d instances [%s]", t, got, args.per_type[t], flag)
    for crid, t, a in made:
        log.info("  %s %s @ %s", crid, t, a)
    if not args.live:
        log.info("(dry-run — nothing was actually reserved, no billing)")
    else:
        log.warning("LIVE reservations are billing NOW at On-Demand rate.")
        log.warning("Run: python3 grab_odcr.py --cancel-all --live  to stop.")


def main():
    p = argparse.ArgumentParser(
        description="Grab EC2 capacity via ODCR, by instance count per type")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="AWS region to target (default %s)" % DEFAULT_REGION)
    p.add_argument("--per-type", nargs="*", metavar="TYPE:COUNT",
                   help="what to grab: an INDEPENDENT number of instances per "
                        "type, given as TYPE:COUNT tokens. Each target is "
                        "judged on its own — a type with no capacity never "
                        "blocks the others. Any EC2 instance type works "
                        "(validated against AWS at startup). Restart-safe: "
                        "held counts are re-read from AWS each round. "
                        "e.g. --per-type i4i.16xlarge:100 i4i.8xlarge:50")
    p.add_argument("--azs", nargs="*",
                   help="lock to these AZ names, e.g. --azs us-east-1c us-east-1d "
                        "(default: every AZ in the region). The per-type COUNT "
                        "is a TOTAL filled across these AZs, wherever there is "
                        "capacity — no per-AZ balancing.")
    p.add_argument("--add", action="store_true",
                   help="one-shot manual top-up: each --per-type COUNT means "
                        "'N MORE on top of what I already hold' instead of an "
                        "absolute total. Requires --live; refuses --watch "
                        "(a restarting watcher would re-add N forever). "
                        "e.g. holding 24, '--per-type i7i.8xlarge:1 --add "
                        "--live' grabs exactly 1 more -> 25.")
    p.add_argument("--watch", action="store_true",
                   help="loop forever, re-sweeping until every type hits its "
                        "target (24x7 hunt)")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between sweeps in --watch mode (default 60)")
    p.add_argument("--live", action="store_true",
                   help="actually reserve (default is dry-run)")
    p.add_argument("--cancel-all", action="store_true",
                   help="cancel all reservations tagged %s=%s" % (TAG_KEY, TAG_VAL))
    p.add_argument("--list", action="store_true",
                   help="list current reservations + per-type instance summary "
                        "(auto-reads targets from grabs.jsonl), then exit")
    args = p.parse_args()
    # Parse --per-type TYPE:COUNT tokens into an ordered {type: count} dict that
    # run() consumes directly. Abort on any malformed token rather than silently
    # grabbing the wrong thing.
    per_type, pt_errors = parse_per_type(args.per_type)
    if pt_errors:
        for e in pt_errors:
            log.error("--per-type %s", e)
        sys.exit(2)
    args.per_type = per_type
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("interrupted — stopping watch")
    except ClientError as e:
        log.error("AWS error: %s", e.response.get("Error"))
        sys.exit(1)


if __name__ == "__main__":
    main()
