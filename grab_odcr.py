#!/usr/bin/env python3
"""Grab i4i capacity via On-Demand Capacity Reservations.

Region is configurable via --region (default: us-east-1).

Strategy: sweep AZ x instance-type (large first), CreateCapacityReservation
count=1 each (all-or-nothing per call, so count=1 scavenges fragments), tag
each reservation, count vCPUs toward a target, stop at the cap.

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
Capacity is intermittent, so 24x7 watching is how you actually catch it.
Every real reservation is logged and appended to logs/grabs.jsonl.

RESUME / IDEMPOTENCY: the per-AZ and total stop-gates read what we ACTUALLY
hold from AWS each round (held_cores_by_az), counting CORES not reservation
objects. So a crash/restart/host-reboot picks up exactly where it left off:
each AZ is judged against its own real held cores, and the sweep only tops up
the true shortfall per AZ. No in-memory counter to lose, no double-grab, no
lopsided distribution after a restart. Safe under systemd Restart=always.

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
import json
import os
import datetime

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, VCPU, DEFAULT_PRIORITY, GRAB_LEDGER, ec2_client, list_azs,
    offered_types_by_az, backoff_sleep, classify, setup_logging,
    record_grab, resolve_types, resolve_azs, ensure_vcpu,
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
        # i4i is a Nitro family — EBS optimization is always-on and can't be
        # disabled. Mark the reservation EBS-optimized so its attributes match
        # the instances it will hold. NOTE: EbsOptimized is NOT one of the
        # `open` match criteria (those are instance type / platform / AZ /
        # tenancy only), so this neither helps nor blocks matching — it's set
        # for honest attribute alignment, not for placement.
        EbsOptimized=True,
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
            cr.get("AvailableInstanceCount"),   # 7th: free slots (Total - used)
        ))
    return rows


def held_cores_by_az(client, only_azs=None):
    """Sum vCPU per AZ across THIS script's tagged reservations, read LIVE
    from AWS. Counts CORES (TotalInstanceCount x per-instance vCPU), NOT the
    number of reservation objects — one reservation can hold many instances,
    so counting objects would be meaningless.

    only_azs: if given (a set/list of AZ names), count ONLY those AZs. This
        keeps the stop-gate in scope when you target a subset of AZs: e.g.
        `--azs us-east-1d` must not let stock already held in us-east-1b
        inflate the total and stop the run before 1d is filled.

    This is the stop-gate's source of truth. Re-reading it every round is what
    makes restarts safe: after a crash we see exactly what we already hold, per
    AZ, and only top up the real shortfall — never double-grab, never lopsided.
    """
    held = {}
    for _crid, itype, az, _state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or itype not in VCPU:
            continue
        if only_azs is not None and az not in only_azs:
            continue
        held[az] = held.get(az, 0) + VCPU[itype] * (cnt or 1)
    return held


def parse_per_type(pairs):
    """Parse --per-type 'TYPE:COUNT' tokens into an ordered {type: count} dict.

    PER-TYPE mode: each entry is an INDEPENDENT instance-count target — grab
    exactly COUNT instances of TYPE, judged on its own. A dry type never blocks
    the others, and there is NO shared core target and NO per-AZ balancing (we
    grab wherever the type has capacity). This is separate from the core-target
    / --per-az-cores machinery and does not touch it.

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


def held_count_by_type(client, types):
    """Sum instances held per type across THIS script's tagged reservations,
    read LIVE from AWS. Counts INSTANCES (TotalInstanceCount), not cores and
    not reservation objects — per-type mode targets are in instance count.

    types: the set/dict of types we care about; others are ignored so unrelated
        tagged reservations never inflate a per-type gate.

    This is the per-type stop-gate's source of truth. Re-reading it every round
    is what makes restarts safe: after a crash we see exactly how many of each
    type we already hold and only top up the real shortfall — never double-grab.
    """
    held = {}
    for _crid, itype, az, _state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or itype not in types:
            continue
        held[itype] = held.get(itype, 0) + (cnt or 1)
    return held


def _targets_from_ledger():
    """Read the most recent (target_vcpu, per_az_cores) from grabs.jsonl.

    So `--list` ALONE can show progress — it remembers what target you were
    grabbing toward, no need to re-type --target-cores / --per-az-cores.
    Returns (target_cores, per_az_cores), each None if unavailable.
    """
    try:
        with open(GRAB_LEDGER) as f:
            lines = [ln for ln in f if ln.strip()]
        if not lines:
            return None, None
        last = json.loads(lines[-1])
        return last.get("target_vcpu"), last.get("per_az_cores")
    except (FileNotFoundError, ValueError, KeyError):
        return None, None


def print_list(client, target_cores=None, per_az_cores=None):
    """--list: show every tagged reservation, then a per-AZ + total summary.

    The summary answers the two questions you actually have during a grab:
    "how many cores do I hold total?" and "how is it split across AZs?"

    Each row also shows USED/free — whether an instance is actually occupying
    that reservation (Total - Available > 0) — and the summary tallies how many
    reservations are USED out of the total we hold.

    Progress (held/target + FULL/short) is shown automatically: if you don't
    pass --target-cores / --per-az-cores, they're read from the last grab in
    grabs.jsonl — so plain `--list` already shows how close you are.
    """
    # Fall back to the ledger's last-known targets when caller didn't pass any.
    if target_cores is None and per_az_cores is None:
        target_cores, per_az_cores = _targets_from_ledger()
    rows = list_reservations(client)
    if not rows:
        log.info("no active/pending reservations")
        return
    # Learn the vCPU of any tagged type we hold but don't have in the static
    # table (e.g. a custom type grabbed in a prior run), so held_cores_by_az
    # counts it instead of silently skipping it. No API call if all are known.
    ensure_vcpu(client, [itype for _c, itype, _a, _s, _n, tag, _av in rows
                         if tag == TAG_VAL])
    used_n = 0   # reservations with an instance actually IN them (used > 0)
    ours_n = 0   # our tagged reservations (the denominator)
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
            if used > 0:
                used_n += 1
    # Summary: only OUR tagged i4i reservations, counted in CORES.
    held = held_cores_by_az(client)
    if held:
        v16 = VCPU["i4i.16xlarge"]
        log.info("--- summary (tag=%s) ---", TAG_VAL)
        for az in sorted(held):
            got = held[az]
            if per_az_cores:
                flag = "FULL" if got >= per_az_cores else "short"
                log.info("  %-12s %5d / %d vCPU  (%d x i4i.16xlarge) [%s]",
                         az, got, per_az_cores, got // v16, flag)
            else:
                log.info("  %-12s %5d vCPU  (%d x i4i.16xlarge)",
                         az, got, got // v16)
        total = sum(held.values())
        if target_cores:
            flag = "FULL" if total >= target_cores else "short"
            log.info("  %-12s %5d / %d vCPU  across %d AZ(s) [%s]",
                     "TOTAL", total, target_cores, len(held), flag)
        else:
            log.info("  %-12s %5d vCPU  across %d AZ(s)",
                     "TOTAL", total, len(held))
        # How many reservations actually have an instance in them.
        log.info("  %-12s %d / %d reservations USED (have an instance running)",
                 "USED", used_n, ours_n)


def cancel_all(client, dry_run):
    rows = [r for r in list_reservations(client) if r[5] == TAG_VAL]
    if not rows:
        log.info("no tagged reservations to cancel")
        return
    for crid, itype, az, state, cnt, _tag, _avail in rows:
        log.info("cancel %s (%s @ %s, %s)", crid, itype, az, state)
        if dry_run:
            log.info("  [dry-run] would cancel")
            continue
        try:
            client.cancel_capacity_reservation(CapacityReservationId=crid)
            log.info("  cancelled")
        except ClientError as e:
            log.error("  cancel failed: %s", e.response.get("Error"))


def _on_grab(args, crid, itype, az, held, made):
    """Bookkeeping for one secured reservation: bump held cores, log, ledger.

    held is the per-AZ core gate (seeded from AWS truth each round); we bump it
    locally so the gate stays accurate WITHIN a sweep, between AWS re-reads.
    """
    held[az] = held.get(az, 0) + VCPU[itype]
    made.append((crid, itype, az))
    total = sum(held.values())
    if args.per_az_cores:
        log.info("RESERVED %s %s @ %s (+%d vCPU | %s: %d/%d | total %d/%d)",
                 crid, itype, az, VCPU[itype], az, held[az],
                 args.per_az_cores, total, args.target_cores)
    else:
        log.info("RESERVED %s %s @ %s (+%d vCPU, total %d/%d)",
                 crid, itype, az, VCPU[itype], total, args.target_cores)
    record_grab("odcr", itype, az, VCPU[itype], total,
                args.target_cores, args.region, not args.live,
                per_az_cores=args.per_az_cores, per_az_total=held[az])


def _az_full(args, held, az):
    """True if this AZ has hit its per-AZ core cap (balanced mode only).

    Judged against held cores READ FROM AWS this round — so a restart correctly
    skips an AZ that is already full and keeps topping up the ones that aren't.
    """
    if args.per_az_cores is None:
        return False
    return held.get(az, 0) >= args.per_az_cores


def sweep_once(client, args, azs, offered, held, made):
    """One full AZ x type pass. Mutates held/made in place."""
    priority = args.types or DEFAULT_PRIORITY
    throttle_attempt = 0
    for itype in priority:
        if sum(held.values()) >= args.target_cores:
            return
        for az in azs:
            if sum(held.values()) >= args.target_cores:
                return
            if _az_full(args, held, az):
                continue  # balanced mode: this AZ already at its per-AZ cap
            if (itype, az) not in offered:
                continue
            try:
                resp = reserve_one(client, itype, az, not args.live, args.end_hours)
                crid = resp["CapacityReservation"]["CapacityReservationId"]
                _on_grab(args, crid, itype, az, held, made)
                throttle_attempt = 0
            except ClientError as e:
                kind = classify(e)
                if kind == "dryrun_ok":
                    log.info("[dry-run] would reserve %s @ %s (+%d vCPU)",
                             itype, az, VCPU[itype])
                    _on_grab(args, "(dry-run)", itype, az, held, made)
                elif kind == "capacity":
                    log.info("no capacity: %s @ %s — next", itype, az)
                elif kind == "throttle":
                    log.warning("throttled, backing off (attempt %d)", throttle_attempt)
                    backoff_sleep(throttle_attempt)
                    throttle_attempt += 1
                else:
                    log.error("FATAL on %s @ %s: %s", itype, az, e.response["Error"])
                    raise


def _on_grab_type(args, crid, itype, az, held, made):
    """Bookkeeping for one secured reservation in PER-TYPE mode: bump the held
    instance count for this type, log, and append to the ledger.

    held is {type: instances}, the per-type gate (seeded from AWS truth each
    round); we bump it locally so the gate stays accurate WITHIN a sweep,
    between AWS re-reads — same idempotency contract as _on_grab, but the unit
    is instances of a type instead of cores in an AZ.
    """
    held[itype] = held.get(itype, 0) + 1
    made.append((crid, itype, az))
    tgt = args.per_type[itype]
    log.info("RESERVED %s %s @ %s (%s: %d/%d instances)",
             crid, itype, az, itype, held[itype], tgt)
    # Ledger stays core-denominated (vcpu/total_vcpu) so --list and downstream
    # tooling read it uniformly. total_vcpu here is this type's held cores.
    vcpu = VCPU.get(itype, 0)
    record_grab("odcr", itype, az, vcpu, held[itype] * vcpu,
                tgt * vcpu, args.region, not args.live)


def sweep_once_per_type(client, args, azs, offered, held, made):
    """One PER-TYPE pass: for each type still short of its own instance-count
    target, grab count=1 across the AZs (cycling AZs) until that type is full
    or its AZs are exhausted this pass. Mutates held/made in place.

    Each type is judged on its OWN target from --per-type: a dry type is simply
    skipped, it never stops the others. No shared core gate, no per-AZ cap.
    """
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
                    resp = reserve_one(client, itype, az, not args.live, args.end_hours)
                    crid = resp["CapacityReservation"]["CapacityReservationId"]
                    _on_grab_type(args, crid, itype, az, held, made)
                    progressed = True
                    throttle_attempt = 0
                except ClientError as e:
                    kind = classify(e)
                    if kind == "dryrun_ok":
                        log.info("[dry-run] would reserve %s @ %s (1 instance)",
                                 itype, az)
                        _on_grab_type(args, "(dry-run)", itype, az, held, made)
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


def _run_per_type(args, client):
    """PER-TYPE mode entry point, called from run() when --per-type is set.

    Independent instance-count target per type; --target-cores / --per-az-cores
    are ignored here (run() warns). Learns vCPU for any non-table type so the
    ledger/summary stay core-consistent, resolves offered (type, az) combos, and
    seeds held counts from AWS so a restart resumes per type.
    """
    types = list(args.per_type)
    added, unresolvable = ensure_vcpu(client, types)
    if added:
        log.info("learned vCPU from AWS for new types: %s", added)
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

    log.info("PER-TYPE mode: targets=%s  AZs=%s",
             {t: args.per_type[t] for t in types}, azs)

    def _done(held):
        return all(held.get(t, 0) >= args.per_type[t] for t in types)

    made = []
    # LIVE: seed from AWS so a restart resumes each type where it left off.
    # dry-run: start empty and simulate locally for a clean plan preview.
    held = held_count_by_type(client, args.per_type) if args.live else {}
    if args.live and held:
        log.info("resumed from AWS: already hold %s", held)

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until every type hits its "
                 "target (Ctrl-C to stop)", args.interval)
        rounds = 0
        while not _done(held):
            rounds += 1
            if args.live:
                held = held_count_by_type(client, args.per_type)
            log.info("--- watch round %d (have %s) ---", rounds,
                     {t: held.get(t, 0) for t in types})
            sweep_once_per_type(client, args, azs, offered, held, made)
            if _done(held):
                break
            time.sleep(args.interval)
        log.info("WATCH: all per-type targets reached after %d round(s)", rounds)
    else:
        sweep_once_per_type(client, args, azs, offered, held, made)

    log.info("=== DONE (per-type): %s (this run created %d reservation(s)) ===",
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


def run(args):
    client = ec2_client(args.region)

    if args.list:
        # Targets are read automatically from grabs.jsonl inside print_list,
        # so plain --list shows progress. If the caller DID pass them, prefer
        # those: target_cores defaults to 8 (placeholder) — treat that as unset;
        # if only --per-az-cores given, derive total = per_az x #--azs.
        tgt = None if args.target_cores == 8 else args.target_cores
        per_az = args.per_az_cores
        if per_az and tgt is None and args.azs:
            tgt = per_az * len(args.azs)
        if tgt is None and per_az is None:
            print_list(client)                       # auto-read from ledger
        else:
            print_list(client, target_cores=tgt, per_az_cores=per_az)
        return

    if args.cancel_all:
        cancel_all(client, not args.live)
        return

    # PER-TYPE mode: independent instance-count target per type. Separate path
    # from the core-target / per-AZ machinery below; those flags are ignored.
    if getattr(args, "per_type", None):
        if args.target_cores != 8 or args.per_az_cores is not None:
            log.warning("--per-type is set: ignoring --target-cores / "
                        "--per-az-cores (per-type has its own counts)")
        _run_per_type(args, client)
        return

    log.info("region=%s dry_run=%s target_cores=%d end_hours=%s watch=%s",
             args.region, not args.live, args.target_cores, args.end_hours, args.watch)

    # Learn the vCPU count for any requested type that isn't in the static
    # table (asks AWS once via DescribeInstanceTypes), so we can grab ANY
    # instance type — not just the i4i/i4g families baked into VCPU. No-op /
    # no API call when --types is unset (default i4i.16xlarge) or all-known.
    added, unresolvable = ensure_vcpu(client, args.types)
    if added:
        log.info("learned vCPU from AWS for new types: %s", added)
    if unresolvable:
        log.warning("AWS could not resolve these instance types (ignored): %s",
                    unresolvable)

    # Resolve & normalize the type priority (auto-sorted large-first) and
    # write it back so sweep_once() uses the exact same ordered list. After
    # ensure_vcpu above, any AWS-known type is in VCPU, so resolve_types only
    # drops truly bogus names.
    types, dropped = resolve_types(args.types)
    if dropped:
        log.warning("ignoring unknown instance types (not in VCPU table): %s", dropped)
    args.types = types
    if not types:
        log.error("no usable instance types after resolution — nothing to do")
        return
    log.info("instance-type priority (large-first): %s", types)

    all_azs = list_azs(client)
    offered = offered_types_by_az(client, types)

    # Lock the sweep to --azs if given, else use every AZ in the region.
    # ODCR needs NO subnet, so any available AZ works.
    azs, missing = resolve_azs(all_azs, args.azs)
    if missing:
        log.warning("requested AZs not present in %s (ignored): %s", args.region, missing)
    if not azs:
        log.error("no usable AZs after applying --azs %s — nothing to do", args.azs)
        return
    log.info("target AZs: %s", azs)

    # BALANCED mode: if --per-az-cores is set and --target-cores was left at the
    # default, auto-compute the total as per_az_cores * number-of-AZs so the
    # caller only has to supply ONE number.
    if args.per_az_cores is not None:
        auto_total = args.per_az_cores * len(azs)
        if args.target_cores == 8:  # untouched default
            args.target_cores = auto_total
            log.info("balanced mode: per-az cap %d vCPU x %d AZ -> target %d vCPU",
                     args.per_az_cores, len(azs), args.target_cores)
        elif args.target_cores != auto_total:
            log.warning("balanced mode: --target-cores %d != per-az %d x %d AZ (%d); "
                        "using --target-cores as the hard overall stop",
                        args.target_cores, args.per_az_cores, len(azs), auto_total)

    made = []  # reservations created in THIS process (for end-of-run listing)

    # held = per-AZ cores we ACTUALLY hold, the stop-gate's source of truth.
    # LIVE: seed from AWS so a restart resumes exactly where we left off.
    # dry-run: start empty and simulate locally so the plan preview is clean.
    #
    # IMPORTANT: count ONLY the AZs we're sweeping (only_azs=set(azs)). If you
    # target one AZ (--azs us-east-1d) but already hold stock in another
    # (us-east-1b), that out-of-scope stock would inflate the TOTAL gate and
    # stop the run before the targeted AZ is filled.
    held = held_cores_by_az(client, only_azs=set(azs)) if args.live else {}
    if args.live and held:
        log.info("resumed from AWS: %d vCPU already held in target AZs %s",
                 sum(held.values()), held)

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until %d vCPU reserved "
                 "(Ctrl-C to stop)", args.interval, args.target_cores)
        rounds = 0
        while sum(held.values()) < args.target_cores:
            rounds += 1
            # Re-read AWS truth each round (live): this is what makes the watch
            # loop self-correcting and restart-safe — per-AZ caps are always
            # judged against what we really hold right now. Same AZ-scope filter
            # as the seed above.
            if args.live:
                held = held_cores_by_az(client, only_azs=set(azs))
            log.info("--- watch round %d (have %d/%d vCPU | per-AZ %s) ---",
                     rounds, sum(held.values()), args.target_cores, held)
            sweep_once(client, args, azs, offered, held, made)
            if sum(held.values()) >= args.target_cores:
                break
            time.sleep(args.interval)
        log.info("WATCH target reached after %d round(s)", rounds)
    else:
        sweep_once(client, args, azs, offered, held, made)

    log.info("=== DONE: holding %d/%d vCPU (this run created %d reservation(s)) ===",
             sum(held.values()), args.target_cores, len(made))
    if args.per_az_cores is not None:
        for az in azs:
            got = held.get(az, 0)
            flag = "FULL" if got >= args.per_az_cores else "short"
            log.info("  per-AZ %s: %d/%d vCPU [%s]", az, got, args.per_az_cores, flag)
    for crid, t, a in made:
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
                   help="stop once this many vCPU are held (default 8). "
                        "If --per-az-cores is set and this is left at default, "
                        "the total is auto-computed as per-az-cores x number-of-AZs.")
    p.add_argument("--per-az-cores", type=int, default=None,
                   help="BALANCED mode: cap EACH AZ at this many vCPU. The cap is "
                        "checked against cores actually held in that AZ (read from "
                        "AWS each round), so the sweep skips any AZ already at its "
                        "cap and keeps hunting the rest — reservations stay even "
                        "across --azs (matches ASG's 50/50 balancing) AND a restart "
                        "resumes per-AZ correctly. e.g. --azs us-east-1b us-east-1d "
                        "--per-az-cores 5000 caps each AZ at 5000 vCPU, 10000 total.")
    p.add_argument("--types", nargs="*",
                   help="instance-type list; default is ONLY i4i.16xlarge. "
                        "Pass ANY EC2 instance type(s) — their vCPU count is "
                        "looked up from AWS automatically (DescribeInstanceTypes) "
                        "if not already in the built-in table, so you are not "
                        "limited to i4i/i4g. e.g. --types r7i.48xlarge m7i.24xlarge "
                        "(auto-sorted large-first; unknown-to-AWS names dropped)")
    p.add_argument("--per-type", nargs="*", metavar="TYPE:COUNT",
                   help="PER-TYPE mode: grab an INDEPENDENT number of instances "
                        "per type, given as TYPE:COUNT tokens. Each target is "
                        "judged on its own — a type with no capacity never blocks "
                        "the others, there is NO shared core target and NO per-AZ "
                        "balancing (grabs wherever the type has capacity). vCPU is "
                        "looked up from AWS for any non-i4i type. Restart-safe: "
                        "held counts are re-read from AWS each round. Overrides "
                        "--target-cores / --per-az-cores. e.g. --per-type "
                        "i4i.16xlarge:10 i4i.8xlarge:5 r7i.24xlarge:3")
    p.add_argument("--azs", nargs="*",
                   help="lock to these AZ names, e.g. --azs us-east-1c us-east-1d "
                        "(default: every AZ in the region)")
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
                   help="list current reservations + per-AZ/total core summary "
                        "(auto-reads target from grabs.jsonl), then exit")
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
