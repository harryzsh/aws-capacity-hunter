#!/usr/bin/env python3
"""Grab G-series (g6e + g7e) capacity via On-Demand Capacity Reservations.

COUNT-based, MULTI-TYPE. Region is configurable via --region (default us-east-1).

You say how many of EACH .48xlarge type to grab, optionally with explicit
per-AZ counts that can DIFFER per type. Internally everything compiles to a
target matrix:  (instance_type, az) -> desired instance count.

Three ways to specify the target (all compile to the same matrix):

  A) EXPLICIT per-(type,az) — counts may differ per AZ and per type:
       --az-counts g7e.48xlarge@us-east-1b=5 g7e.48xlarge@us-east-1d=3 \
                   g6e.48xlarge@us-east-1b=2 g6e.48xlarge@us-east-1d=10
  B) per-type TOTAL, evenly balanced across --azs:
       --counts g6e.48xlarge=10 g7e.48xlarge=20 --azs us-east-1b us-east-1d --balance
  C) per-type TOTAL, greedy fill (no per-AZ cap):
       --counts g6e.48xlarge=10 g7e.48xlarge=20 --azs us-east-1b us-east-1d

  (backward compat — single g7e via the old flags:)
       --target-count 4 --per-az-count 2 --azs us-east-1b us-east-1d

Strategy: sweep over the target cells, CreateCapacityReservation count=1 each
(all-or-nothing per call, so count=1 scavenges single-instance fragments), tag
each reservation, count INSTANCES per (type,az) toward the cell/type caps.

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
RESUME / IDEMPOTENCY: the gates read what we ACTUALLY hold from AWS each round
(held_by_type_az), per (type,az), counting instances. A crash/restart resumes
exactly where it left off, topping up only the real shortfall per cell.

Why ODCR: a reservation HOLDS the slot even when no instance occupies it.
Trade-off: an ACTIVE reservation bills at On-Demand rate whether filled or not.
(Capacity Blocks for ML do NOT cover the G family, so ODCR is the tool here.)

SAFETY: default is --dry-run. Use --live to actually reserve, --cancel-all to
release everything, --check-quota to preflight the shared G/VT vCPU quota.
"""
import argparse
import sys
import time

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, VCPU, SUPPORTED_TYPES, DEFAULT_TYPE, TAG_KEY, TAG_VAL,
    ec2_client, list_azs, offered_by_az, backoff_sleep, classify,
    setup_logging, record_grab, resolve_azs, save_plan, load_plan,
)

# --target-count placeholder: a tiny value treated as "unset" so the compat
# balanced mode (per-az-count x #AZ) can auto-fill the real total.
DEFAULT_TARGET = 1

# Logging is ALWAYS on (console + rotating file) — the fallback record of truth.
log = setup_logging("grab_g7e_odcr.log")


# --------------------------------------------------------------------------- #
# CLI target parsing (pure, unit-tested)
# --------------------------------------------------------------------------- #
def parse_counts(items):
    """['g6e.48xlarge=10', 'g7e.48xlarge=20'] -> {type: int}.

    Raises ValueError on bad syntax, unknown/unsupported type, or non-positive.
    """
    out = {}
    for item in items:
        if "=" not in item:
            raise ValueError("bad --counts entry %r (want TYPE=N)" % item)
        t, n = item.split("=", 1)
        t = t.strip()
        if t not in VCPU:
            raise ValueError("unsupported type %r (allowed: %s)"
                             % (t, ", ".join(SUPPORTED_TYPES)))
        try:
            n = int(n)
        except ValueError:
            raise ValueError("bad count in %r (want integer)" % item)
        if n <= 0:
            raise ValueError("count must be > 0 in %r" % item)
        out[t] = out.get(t, 0) + n
    return out


def parse_az_counts(items):
    """['g7e.48xlarge@us-east-1b=5', ...] -> {(type, az): int}.

    Raises ValueError on bad syntax, unknown/unsupported type, or non-positive.
    """
    out = {}
    for item in items:
        if "@" not in item or "=" not in item:
            raise ValueError("bad --az-counts entry %r (want TYPE@AZ=N)" % item)
        left, n = item.split("=", 1)
        t, az = left.split("@", 1)
        t, az = t.strip(), az.strip()
        if t not in VCPU:
            raise ValueError("unsupported type %r (allowed: %s)"
                             % (t, ", ".join(SUPPORTED_TYPES)))
        if not az:
            raise ValueError("missing AZ in %r" % item)
        try:
            n = int(n)
        except ValueError:
            raise ValueError("bad count in %r (want integer)" % item)
        if n <= 0:
            raise ValueError("count must be > 0 in %r" % item)
        out[(t, az)] = out.get((t, az), 0) + n
    return out


def distribute(total, azs):
    """Split `total` as evenly as possible across `azs` (front-loaded remainder).

    distribute(10, [a,b]) -> [5,5]; distribute(11,[a,b]) -> [6,5];
    distribute(1,[a,b])  -> [1,0].
    """
    k = len(azs)
    if k == 0:
        return []
    base, rem = divmod(total, k)
    return [base + (1 if i < rem else 0) for i in range(k)]


def build_plan(args, all_azs):
    """Resolve CLI args into (cells, type_targets).

    cells:        {(type,az): cap_or_None}  per-cell hard cap; None = no cap
                  (greedy). Insertion order is the sweep order.
    type_targets: {type: total}  hard overall target per type.

    `all_azs` is the region's AZ list, used only when AZs aren't otherwise
    determined (--counts/compat without --azs).
    """
    cells, type_targets = {}, {}

    if args.az_counts:
        matrix = parse_az_counts(args.az_counts)
        for (t, az), n in matrix.items():
            cells[(t, az)] = n
            type_targets[t] = type_targets.get(t, 0) + n
        return cells, type_targets

    if args.counts:
        per_type = parse_counts(args.counts)
        azs = args.azs if args.azs else list(all_azs)
        for t in sorted(per_type):
            total = per_type[t]
            type_targets[t] = total
            if args.balance:
                for az, cap in zip(azs, distribute(total, azs)):
                    cells[(t, az)] = cap
            else:
                for az in azs:
                    cells[(t, az)] = None      # greedy: no per-AZ cap
        return cells, type_targets

    # Backward-compatible single-type path (old --target-count/--per-az-count).
    t = DEFAULT_TYPE
    azs = args.azs if args.azs else list(all_azs)
    if args.per_az_count is not None:
        for az in azs:
            cells[(t, az)] = args.per_az_count
        if args.target_count == DEFAULT_TARGET:       # untouched default
            type_targets[t] = args.per_az_count * len(azs)
        else:
            type_targets[t] = args.target_count       # hard overall stop
    else:
        for az in azs:
            cells[(t, az)] = None
        type_targets[t] = args.target_count
    return cells, type_targets


# --------------------------------------------------------------------------- #
# AWS wrappers
# --------------------------------------------------------------------------- #
def reserve_one(client, itype, az, dry_run, end_hours=None):
    """Create a count=1 ODCR for `itype` in `az`. open matching, no commitment.

    end_hours: if set, EndDateType=limited (auto-expires) as a billing guard.
               if None, EndDateType=unlimited (until you cancel).
    """
    import datetime
    kwargs = dict(
        InstanceType=itype,
        InstancePlatform="Linux/UNIX",
        AvailabilityZone=az,
        InstanceCount=1,
        InstanceMatchCriteria="open",
        Tenancy="default",
        # g6e/g7e are Nitro families — EBS optimization is always-on. Mark the
        # reservation EBS-optimized so its attributes match the instances it
        # will hold. (Not an `open` match criterion, just honest alignment.)
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


def held_by_type_az(client, scope=None):
    """Instances held per (type, az) across THIS script's tagged reservations,
    read LIVE from AWS. Counts instances (TotalInstanceCount), not objects.

    scope: if given (a set of (type,az) tuples), count ONLY those cells — keeps
        the stop-gates in scope so out-of-plan stock can't inflate them and
        stop the run early. This is the gate's source of truth; re-reading it
        each round makes restarts safe.
    """
    held = {}
    for _crid, itype, az, _state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or itype not in VCPU:
            continue
        if scope is not None and (itype, az) not in scope:
            continue
        held[(itype, az)] = held.get((itype, az), 0) + (cnt or 0)
    return held


def type_held(held, itype):
    """Total instances of `itype` held across all AZs."""
    return sum(c for (t, _az), c in held.items() if t == itype)


def grand_held(held):
    return sum(held.values())


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def _cell_full(cells, held, t, az):
    """True if (t,az) has hit its per-cell cap. Cap None (greedy) = never full
    on the cell (the per-type total is what stops it)."""
    cap = cells.get((t, az))
    if cap is None:
        return False
    return held.get((t, az), 0) >= cap


def _type_done(type_targets, held, t):
    return type_held(held, t) >= type_targets.get(t, 0)


def _all_done(type_targets, held):
    return all(_type_done(type_targets, held, t) for t in type_targets)


def _azs_for_type(cells, t):
    """AZs targeted for type t, in plan (insertion) order."""
    return [az for (tt, az) in cells if tt == t]


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #
def _on_grab(args, cells, type_targets, crid, itype, az, held, made):
    """Bookkeeping for one secured reservation: bump held, log, ledger."""
    held[(itype, az)] = held.get((itype, az), 0) + 1
    made.append((crid, itype, az))
    tt = type_held(held, itype)
    gt = grand_held(held)
    cap = cells.get((itype, az))
    grand_target = sum(type_targets.values())
    cap_str = str(cap) if cap is not None else "-"
    log.info("RESERVED %s %s @ %s (+1 | %s@%s: %d/%s | %s: %d/%d | all: %d/%d)",
             crid, itype, az, itype, az, held[(itype, az)], cap_str,
             itype, tt, type_targets.get(itype, 0), gt, grand_target)
    record_grab("odcr", itype, az, 1, tt, type_targets.get(itype, 0),
                args.region, not args.live, az_cap=cap,
                az_total=held[(itype, az)], grand_total=gt,
                grand_target=grand_target)


def sweep_once(client, args, cells, type_targets, offered, held, made):
    """One full pass over the target cells. Mutates held/made in place.

    Grabs at most ONE instance per (type,az) per pass; the --watch loop repeats
    sweeps to accumulate toward the caps.
    """
    throttle_attempt = 0
    for t in sorted(type_targets):
        if _type_done(type_targets, held, t):
            continue
        for az in _azs_for_type(cells, t):
            if _type_done(type_targets, held, t):
                break
            if _cell_full(cells, held, t, az):
                continue
            if (t, az) not in offered:
                continue
            try:
                resp = reserve_one(client, t, az, not args.live, args.end_hours)
                crid = resp["CapacityReservation"]["CapacityReservationId"]
                _on_grab(args, cells, type_targets, crid, t, az, held, made)
                throttle_attempt = 0
            except ClientError as e:
                kind = classify(e)
                if kind == "dryrun_ok":
                    log.info("[dry-run] would reserve %s @ %s (+1)", t, az)
                    _on_grab(args, cells, type_targets, "(dry-run)", t, az,
                             held, made)
                elif kind == "capacity":
                    log.info("no capacity: %s @ %s — next", t, az)
                elif kind == "throttle":
                    log.warning("throttled, backing off (attempt %d)",
                                throttle_attempt)
                    backoff_sleep(throttle_attempt)
                    throttle_attempt += 1
                else:
                    log.error("FATAL on %s @ %s: %s", t, az, e.response["Error"])
                    raise


# --------------------------------------------------------------------------- #
# list / cancel
# --------------------------------------------------------------------------- #
def print_list(client, cells=None, type_targets=None):
    """--list: show every tagged reservation, then a per-(type,az) + per-type +
    grand-total summary. Progress (held/cap, held/target, FULL/short) is shown
    when a plan is available — passed in, or auto-read from logs/plan.json.
    """
    if cells is None and type_targets is None:
        cells, type_targets = load_plan()
    cells = cells or {}
    type_targets = type_targets or {}

    rows = list_reservations(client)
    if not rows:
        log.info("no active/pending reservations")
        return
    used_n = ours_n = 0
    for crid, itype, az, state, cnt, tag, avail in rows:
        total = cnt or 0
        free = avail if avail is not None else total
        used = total - free
        used_str = "USED" if used > 0 else "free"
        log.info("%s  %-14s %-12s %-9s count=%s %-4s tag=%s",
                 crid, itype, az, state, cnt, used_str, tag)
        if tag == TAG_VAL:
            ours_n += 1
            if used > 0:
                used_n += 1

    held = held_by_type_az(client)
    if not held and not type_targets:
        return
    log.info("--- summary (tag=%s) ---", TAG_VAL)
    types = sorted(set([t for (t, _a) in held] + list(type_targets)))
    grand = 0
    for t in types:
        # per-AZ rows for this type
        azs = sorted(set([a for (tt, a) in held if tt == t]
                         + [a for (tt, a) in cells if tt == t]))
        for az in azs:
            got = held.get((t, az), 0)
            cap = cells.get((t, az))
            if cap is not None:
                flag = "FULL" if got >= cap else "short"
                log.info("  %-13s %-12s %3d / %d [%s]", t, az, got, cap, flag)
            else:
                log.info("  %-13s %-12s %3d", t, az, got)
        tot = type_held(held, t)
        grand += tot
        tgt = type_targets.get(t)
        if tgt is not None:
            flag = "FULL" if tot >= tgt else "short"
            log.info("  %-13s %-12s %3d / %d instances [%s]",
                     t, "TYPE TOTAL", tot, tgt, flag)
        else:
            log.info("  %-13s %-12s %3d instances", t, "TYPE TOTAL", tot)
    grand_target = sum(type_targets.values()) if type_targets else None
    if grand_target:
        flag = "FULL" if grand >= grand_target else "short"
        log.info("  %-13s %-12s %3d / %d instances [%s]",
                 "GRAND TOTAL", "", grand, grand_target, flag)
    else:
        log.info("  %-13s %-12s %3d instances", "GRAND TOTAL", "", grand)
    log.info("  %d / %d reservations USED (have an instance running)",
             used_n, ours_n)


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


# --------------------------------------------------------------------------- #
# quota preflight
# --------------------------------------------------------------------------- #
def report_quota(args, type_targets):
    """--check-quota: print the shared G/VT vCPU quota vs the planned counts."""
    from quota import (
        service_quotas_client, get_g_vt_quota, vcpus_for_counts,
        G_VT_QUOTA_NAME, G_VT_QUOTA_CODE,
    )
    sq = service_quotas_client(args.region)
    current = get_g_vt_quota(sq)
    needed = vcpus_for_counts(type_targets)
    log.info("G/VT On-Demand quota (%s, %s) in %s: %g vCPU",
             G_VT_QUOTA_NAME, G_VT_QUOTA_CODE, args.region, current)
    for t in sorted(type_targets):
        log.info("  plan: %d x %s = %d vCPU",
                 type_targets[t], t, type_targets[t] * VCPU[t])
    ok = current >= needed
    log.info("  total needed %d vCPU vs current %g -> %s",
             needed, current, "OK" if ok else "INSUFFICIENT")
    if not ok:
        log.warning("quota too low — request an increase first. See 配额.md")


# --------------------------------------------------------------------------- #
# run / main
# --------------------------------------------------------------------------- #
def _validate_modes(args):
    """Reject conflicting target specs early (clear error over silent surprise)."""
    if args.az_counts and args.counts:
        raise ValueError("use either --az-counts OR --counts, not both")
    if args.balance and not args.counts:
        raise ValueError("--balance only applies with --counts")
    if args.az_counts and args.azs:
        log.warning("--azs is ignored when --az-counts is given "
                    "(AZs come from the TYPE@AZ keys)")


def _need_region_azs(args):
    """Do we have to ask AWS for the region's AZ list to resolve the plan?
    Only when AZs aren't given explicitly and we're not in --az-counts mode."""
    return not args.az_counts and not args.azs


def run(args):
    _validate_modes(args)

    if args.check_quota:
        all_azs = list_azs(ec2_client(args.region)) if _need_region_azs(args) else []
        _cells, type_targets = build_plan(args, all_azs)
        report_quota(args, type_targets)
        return

    if args.list:
        # Prefer an explicitly-passed plan; otherwise fall back to the saved
        # plan file (so plain --list shows progress).
        if args.az_counts or args.counts or args.per_az_count is not None \
                or args.target_count != DEFAULT_TARGET:
            all_azs = list_azs(ec2_client(args.region)) if _need_region_azs(args) else []
            cells, type_targets = build_plan(args, all_azs)
            print_list(ec2_client(args.region), cells, type_targets)
        else:
            print_list(ec2_client(args.region))
        return

    client = ec2_client(args.region)

    if args.cancel_all:
        cancel_all(client, not args.live)
        return

    all_azs = list_azs(client)
    offered = offered_by_az(client, SUPPORTED_TYPES)
    cells, type_targets = build_plan(args, all_azs)

    # Validate AZ existence + offering; warn (don't crash) on problems.
    plan_azs = sorted({az for (_t, az) in cells})
    _sel, missing = resolve_azs(all_azs, plan_azs)
    if missing:
        log.warning("requested AZs not present in %s (cells will be skipped): %s",
                    args.region, missing)
    not_offered = sorted({(t, az) for (t, az) in cells
                          if (t, az) not in offered})
    if not_offered:
        log.warning("these (type,az) cells are NOT offered (will skip): %s",
                    ["%s@%s" % (t, az) for (t, az) in not_offered])
    if not cells:
        log.error("empty plan — nothing to do")
        return

    log.info("region=%s dry_run=%s watch=%s", args.region, not args.live, args.watch)
    for t in sorted(type_targets):
        log.info("  plan %s: target %d, cells %s", t, type_targets[t],
                 {az: cells[(t, az)] for (tt, az) in cells if tt == t})
    grand_target = sum(type_targets.values())
    log.info("  GRAND target: %d instance(s)", grand_target)

    if args.live:
        save_plan(cells, type_targets, args.region)

    made = []
    scope = set(cells)
    # held = per-cell instances ACTUALLY held (gate's source of truth).
    # LIVE: seed from AWS so a restart resumes; dry-run: simulate locally.
    held = held_by_type_az(client, scope=scope) if args.live else {}
    if args.live and held:
        log.info("resumed from AWS: %d instance(s) already held %s",
                 grand_held(held), {("%s@%s" % k): v for k, v in held.items()})

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until %d instance(s) "
                 "reserved (Ctrl-C to stop)", args.interval, grand_target)
        rounds = 0
        while not _all_done(type_targets, held):
            rounds += 1
            if args.live:
                held = held_by_type_az(client, scope=scope)
            log.info("--- watch round %d (have %d/%d instances) ---",
                     rounds, grand_held(held), grand_target)
            sweep_once(client, args, cells, type_targets, offered, held, made)
            if _all_done(type_targets, held):
                break
            time.sleep(args.interval)
        log.info("WATCH target reached after %d round(s)", rounds)
    else:
        sweep_once(client, args, cells, type_targets, offered, held, made)

    log.info("=== DONE: holding %d/%d instance(s) (this run created %d) ===",
             grand_held(held), grand_target, len(made))
    for t in sorted(type_targets):
        tot = type_held(held, t)
        flag = "FULL" if tot >= type_targets[t] else "short"
        log.info("  %s: %d/%d [%s]", t, tot, type_targets[t], flag)
    for crid, ty, a in made:
        log.info("  %s %s @ %s", crid, ty, a)
    if not args.live:
        log.info("(dry-run — nothing was actually reserved, no billing)")
    else:
        log.warning("LIVE reservations are billing NOW at On-Demand rate.")
        log.warning("Run: python3 grab_g7e_odcr.py --cancel-all --live  to stop.")


def main():
    p = argparse.ArgumentParser(
        description="Grab G-series (g6e + g7e) via On-Demand Capacity "
                    "Reservations (count-based, multi-type)")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="AWS region to target (default %s)" % DEFAULT_REGION)
    p.add_argument("--counts", nargs="*", metavar="TYPE=N",
                   help="per-type TOTAL instance targets, e.g. "
                        "--counts g6e.48xlarge=10 g7e.48xlarge=20. "
                        "Combine with --azs (and optionally --balance).")
    p.add_argument("--az-counts", nargs="*", metavar="TYPE@AZ=N",
                   help="EXPLICIT per-(type,az) instance counts (may differ per "
                        "AZ/type), e.g. --az-counts g7e.48xlarge@us-east-1b=5 "
                        "g6e.48xlarge@us-east-1d=10. AZs are taken from the keys.")
    p.add_argument("--balance", action="store_true",
                   help="with --counts: spread each type's total EVENLY across "
                        "--azs (per-AZ cap = ceil/floor of total/#AZ)")
    p.add_argument("--azs", nargs="*",
                   help="AZ names for --counts/compat modes, e.g. --azs "
                        "us-east-1b us-east-1d (default: every AZ in the region)")
    # Backward-compatible single-type (g7e.48xlarge) flags:
    p.add_argument("--target-count", type=int, default=DEFAULT_TARGET,
                   help="[compat] single-type %s total target (default %d). "
                        "Used only when neither --counts nor --az-counts is given."
                        % (DEFAULT_TYPE, DEFAULT_TARGET))
    p.add_argument("--per-az-count", type=int, default=None,
                   help="[compat] single-type %s per-AZ cap; with default "
                        "--target-count, total auto = per-az x #AZ." % DEFAULT_TYPE)
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
                   help="list reservations + per-(type,az)/type/grand summary "
                        "(auto-reads plan from logs/plan.json), then exit")
    p.add_argument("--check-quota", action="store_true",
                   help="check the shared G/VT vCPU quota against the plan "
                        "(reads Service Quotas, reserves nothing), then exit")
    args = p.parse_args()
    try:
        run(args)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("interrupted — stopping watch")
    except ClientError as e:
        log.error("AWS error: %s", e.response.get("Error"))
        sys.exit(1)


if __name__ == "__main__":
    main()
