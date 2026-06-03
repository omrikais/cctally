"""`cctally project` subcommand entry point.

Lazy I/O sibling: holds `cmd_project` + its 4 dedicated helpers
(`_load_week_snapshots`, `_accumulate_entry_into_bucket`,
`_project_json_output`, `_project_sort_key`). Aggregates session entries
by git-root project with per-project weekly usage attribution.

Honest imports are KERNEL-ONLY (`_cctally_core`). Every other symbol the
command calls is reached via the call-time `_cctally()` accessor so test
monkeypatches through `cctally`'s namespace are preserved — see the spec
§3.1 disposition table (the cache reads, the share builders/dispatch,
`_share_validate_args`, `_render_project_table`, `resolve_display_tz`,
and the `bin/cctally`-resident helpers all route through `c.`).

bin/cctally re-exports `cmd_project` (eager) so the parser's
`set_defaults(func=c.cmd_project)` resolves unchanged.

Spec: docs/superpowers/specs/2026-05-30-extract-project-cmd-design.md
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import sys

from _cctally_core import _command_as_of, eprint, open_db, parse_iso_datetime


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


def _load_week_snapshots(
    since: dt.datetime, until: dt.datetime
) -> dict[dt.datetime, float]:
    """Return {week_start_utc -> max(weekly_percent)} for weeks intersecting
    the [since, until] range.

    Reads the `weekly_percent` column of `weekly_usage_snapshots` (authoritative
    column name — NOT `used_7d_percent`). A week's "used %" is the maximum
    snapshot captured within that week (the monotonic-within-window invariant:
    weekly_percent only increases across the life of a week). Skips rows
    whose `week_start_at` or `week_end_at` are NULL (pre-migration legacy
    rows that only carried date granularity).

    MAX is computed in Python keyed on the parsed UTC datetime so that rows
    holding different string spellings of the same instant (e.g. `+00:00` vs
    `+03:00` from pre-UTC-cast canonicalizer history) coalesce into one
    bucket instead of splitting and silently dropping the higher value.

    Returns an empty dict if the stats DB has no relevant rows.
    """
    conn = open_db()
    try:
        cur = conn.execute(
            "SELECT week_start_at, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_start_at IS NOT NULL "
            "AND week_end_at IS NOT NULL "
            "AND datetime(week_start_at) < datetime(?) "
            "AND datetime(week_end_at) > datetime(?)",
            (until.isoformat(), since.isoformat()),
        )
        result: dict[dt.datetime, float] = {}
        for ws_iso, pct in cur.fetchall():
            if ws_iso is None or pct is None:
                continue
            ws = dt.datetime.fromisoformat(
                str(ws_iso).replace("Z", "+00:00")
            )
            key = ws.astimezone(dt.timezone.utc)
            pct_f = float(pct)
            prev = result.get(key)
            if prev is None or pct_f > prev:
                result[key] = pct_f
        return result
    finally:
        conn.close()


def _sum_cost_by_project(
    start: dt.datetime,
    now: dt.datetime,
    mode: str = "auto",
    skip_sync: bool = False,
) -> dict[str, float]:
    """Return ``{canonical_git_root: spent_usd}`` over ``[start, now]``.

    ONE scan over the joined session entries (the same iterator
    ``cmd_project`` walks), bucketed in Python by each entry's resolved
    git-root (``_resolve_project_key`` — a filesystem ``.git`` walk, NOT a
    SQL ``GROUP BY``), with per-entry cost computed via the same
    ``_calculate_entry_cost(model, usage, mode=...)`` path ``cmd_project``
    uses (so pricing edits flow through uniformly). Keys are the resolved
    ``ProjectKey.bucket_path`` (the canonical git-root when a ``.git`` is
    found, else the normalized path) — identical to how ``cmd_project``
    keys its rows, so configured ``budget.projects`` keys match by string
    equality.

    Synthetic entries (Claude Code internal markers) are skipped, mirroring
    ``cmd_project`` / the other ``_JoinedClaudeEntry`` aggregators. A
    configured project with no in-range entry simply never appears in the
    returned map (the caller renders it as a ``$0`` row — spec §7.2).

    Shared by the per-project budget display (§7.2, ``cmd_budget``) and the
    alert-firing path (§6.4); ``skip_sync`` threads through to
    ``get_claude_session_entries`` so the record-tick caller can reuse a
    cache already warmed earlier in the same tick.
    """
    c = _cctally()
    resolver_cache: dict[str, ProjectKey] = {}
    out: dict[str, float] = {}
    for entry in c.get_claude_session_entries(start, now, skip_sync=skip_sync):
        if entry.model == "<synthetic>":
            continue
        cost = c._calculate_entry_cost(
            entry.model,
            {
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "cache_creation_input_tokens": entry.cache_creation_tokens,
                "cache_read_input_tokens": entry.cache_read_tokens,
            },
            mode=mode,
            cost_usd=entry.cost_usd,
        )
        key = c._resolve_project_key(entry.project_path, "git-root", resolver_cache)
        out[key.bucket_path] = out.get(key.bucket_path, 0.0) + cost
    return out


def _project_budget_labels(keys):
    """Collision-aware ``{project_key: label}`` for a set of budget project
    keys. Single source of the resolve+disambiguate primitive used by the
    budget table (`_build_project_budget_rows`), the alert payload
    (`maybe_record_project_budget_milestone`), and the dashboard SSE envelope
    (`_envelope_rows_project_budget`) — issue #130. Each caller passes its own
    key feed; the label for a key is identical across callers only when they
    feed the same key set (the dashboard intentionally feeds its alerted-row
    subset). Output is order-independent (disambiguation keys off basename
    collisions, not position)."""
    c = _cctally()
    keys = list(keys)
    resolver_cache: dict = {}
    pkeys = [c._resolve_project_key(k, "git-root", resolver_cache) for k in keys]
    disambig = c._project_disambiguate_labels([{"key": pk} for pk in pkeys])
    return {
        keys[i]: disambig.get(i, pkeys[i].display_key)
        for i in range(len(keys))
    }


def _accumulate_entry_into_bucket(
    b: dict,
    entry: "_JoinedClaudeEntry",
    pre_computed_cost: float | None = None,
) -> None:
    """Add one joined-Claude entry's tokens, cost, session-id, and timestamps
    into a project×week bucket dict.

    Cost is computed via the same `_calculate_entry_cost(model, usage_dict,
    mode="auto", cost_usd=...)` path used by `_aggregate_cache_by_session`
    (the other `_JoinedClaudeEntry` consumer) so pricing updates flow through
    uniformly. Per-model sub-buckets mirror the parent bucket's shape.

    `pre_computed_cost`: if callers have already invoked `_calculate_entry_cost`
    for this entry (e.g. to also feed the attribution denominator in
    `cmd_project`), pass it in to avoid double work.
    """
    c = _cctally()
    # Mirror `_aggregate_claude_sessions`: NULL session_id falls back to the
    # source-file basename so distinct files don't collapse into one bucket.
    if entry.session_id:
        sid = entry.session_id
    else:
        sid = os.path.splitext(os.path.basename(entry.source_path))[0]
    b["sessions"].add(sid)
    if entry.timestamp < b["first_seen"]:
        b["first_seen"] = entry.timestamp
    if entry.timestamp > b["last_seen"]:
        b["last_seen"] = entry.timestamp
    b["input"] += entry.input_tokens
    b["output"] += entry.output_tokens
    b["cache_write"] += entry.cache_creation_tokens
    b["cache_read"] += entry.cache_read_tokens
    if pre_computed_cost is not None:
        cost = pre_computed_cost
    else:
        cost = c._calculate_entry_cost(
            entry.model,
            {
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "cache_creation_input_tokens": entry.cache_creation_tokens,
                "cache_read_input_tokens": entry.cache_read_tokens,
            },
            mode="auto",
            cost_usd=entry.cost_usd,
        )
    b["cost_usd"] += cost
    model = entry.model or "(unknown-model)"
    mb = b["models"].get(model)
    if mb is None:
        mb = {
            "cost_usd": 0.0,
            "input": 0, "output": 0,
            "cache_write": 0, "cache_read": 0,
            "first_seen": entry.timestamp, "last_seen": entry.timestamp,
        }
        b["models"][model] = mb
    if entry.timestamp < mb["first_seen"]:
        mb["first_seen"] = entry.timestamp
    if entry.timestamp > mb["last_seen"]:
        mb["last_seen"] = entry.timestamp
    mb["cost_usd"] += cost
    mb["input"] += entry.input_tokens
    mb["output"] += entry.output_tokens
    mb["cache_write"] += entry.cache_creation_tokens
    mb["cache_read"] += entry.cache_read_tokens


def _project_json_output(
    *,
    since: dt.datetime,
    until: dt.datetime,
    weeks_in_range: int,
    group_mode: str,
    rows: list[dict],
    weeks_missing_snapshot: set[dt.datetime],
    warnings: list[str],
    include_breakdown: bool,
    week_snapshots: dict[dt.datetime, float],
) -> str:
    """Render the project subcommand's --json payload per spec §4.

    Accepts rows already sorted by the caller (so ordering flags apply
    uniformly to both terminal and JSON modes). Aggregates `totals.costUsd`
    from `rows` and `totals.usedPercent` from `week_snapshots` (sum over
    all weeks with snapshots in the range — matches the conservation-law
    denominator used by per-project attribution). `models[]` is included
    per-project only when `--breakdown` is requested to avoid payload bloat.
    """
    total_cost = sum(r["cost_usd"] for r in rows)
    # Aggregate used % across all weeks with snapshots in the range.
    total_used_pct: float | None
    if week_snapshots:
        total_used_pct = sum(week_snapshots.values())
    else:
        total_used_pct = None

    def _fmt_dt(ts: dt.datetime) -> str:
        return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    projects_json = []
    for row in rows:  # rows come already sorted by caller
        p = {
            "displayKey": row["key"].display_key,
            "projectPath": row["key"].bucket_path,
            "gitRoot": row["key"].git_root,
            "sessions": len(row["sessions"]),
            "firstSeen": _fmt_dt(row["first_seen"]),
            "lastSeen": _fmt_dt(row["last_seen"]),
            "inputTokens": row["input"],
            "outputTokens": row["output"],
            "cacheWriteTokens": row["cache_write"],
            "cacheReadTokens": row["cache_read"],
            "costUsd": round(row["cost_usd"], 4),
            "attributedUsedPercent": (
                round(row["attributed_pct"], 4)
                if row["attributed_pct"] is not None else None
            ),
            "costPerPercent": (
                round(row["cost_per_pct"], 4)
                if row["cost_per_pct"] is not None else None
            ),
        }
        if include_breakdown:
            p["models"] = [
                {
                    "model": mname,
                    "firstSeen": _fmt_dt(mb["first_seen"]),
                    "lastSeen": _fmt_dt(mb["last_seen"]),
                    "inputTokens": mb["input"],
                    "outputTokens": mb["output"],
                    "cacheWriteTokens": mb["cache_write"],
                    "cacheReadTokens": mb["cache_read"],
                    "costUsd": round(mb["cost_usd"], 4),
                }
                for mname, mb in sorted(row["models"].items())
            ]
        projects_json.append(p)

    payload = {
        "rangeStart": since.date().isoformat(),
        "rangeEnd": until.date().isoformat(),
        "weeksInRange": weeks_in_range,
        "groupMode": group_mode,
        "totals": {
            "costUsd": round(total_cost, 4),
            "usedPercent": (
                round(total_used_pct, 4) if total_used_pct is not None else None
            ),
            "weeklyAttributionAvailable": len(weeks_missing_snapshot) == 0,
        },
        "projects": projects_json,
        "warnings": warnings,
    }
    return json.dumps(payload, indent=2)


def _project_sort_key(row: dict, sort_by: str, order: str):
    """Return (primary, dname) where the primary is flipped to match
    ``order``. Tie-break on dname ascending regardless of direction.

    ``sort_by`` values align with argparse choices: cost|used|name|last-seen.
    """
    dname = row["key"].display_key.lower()
    sign = -1 if order == "desc" else 1
    if sort_by == "cost":
        return (sign * row["cost_usd"], dname)
    if sort_by == "used":
        v = row["attributed_pct"] if row["attributed_pct"] is not None else -1
        return (sign * v, dname)
    if sort_by == "last-seen":
        return (sign * row["last_seen"].timestamp(), dname)
    if sort_by == "name":
        # name is asc-natural; caller uses sorted(reverse=order=='desc').
        return (dname,)
    # Unreachable given argparse choices, but safe default.
    return (sign * row["cost_usd"], dname)


def cmd_project(args: argparse.Namespace) -> int:
    """Roll entries up by project (git-root) with per-project usage attribution."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz so the
    # existing resolve_display_tz precedence absorbs the new alias.
    c._bridge_z_into_tz(args, config)
    args._resolved_tz = c.resolve_display_tz(args, config)

    # Flag-combination validation (must run before any expensive work).
    if args.weeks is not None and args.weeks < 1:
        eprint("Error: --weeks must be >= 1")
        return 1
    if args.weeks is not None and (args.since or args.until):
        eprint("Error: --weeks cannot be combined with --since/--until")
        return 1
    if args.since and args.until:
        # Parse both as dates using the same multi-format helper shape used
        # elsewhere in the codebase so YYYY-MM-DD and YYYYMMDD both compare
        # correctly (string compare alone breaks across mixed formats).
        def _parse(raw: str) -> dt.date | None:
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return dt.datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
            return None

        since_parsed = _parse(args.since)
        until_parsed = _parse(args.until)
        # Silent-skip if either date failed to parse: we only want to surface
        # a "range order" error here when both inputs are well-formed. Any
        # format error will be reported downstream by _parse_cli_date_range()
        # so the user sees the parse problem first (not a misleading order
        # complaint triggered by garbage input).
        if since_parsed is not None and until_parsed is not None and since_parsed > until_parsed:
            eprint("Error: --since must be <= --until")
            return 1

    now = _command_as_of()
    conn = open_db()

    # Resolve [since_dt, until_dt] in UTC.
    if args.since or args.until:
        parsed = c._parse_cli_date_range(args, now_utc=now)
        if isinstance(parsed, int):
            return parsed
        since_dt, until_dt = parsed
        since_dt = since_dt.astimezone(dt.timezone.utc)
        until_dt = until_dt.astimezone(dt.timezone.utc)
    else:
        # Default to the current subscription week; --weeks N extends backwards.
        # Widen by 1us so the emit loop fires when `now` is exactly at a reset
        # boundary (zero-width [now, now] makes Case A's `current < range_end`
        # false, which would otherwise wrongly fall through to the Monday
        # fallback for non-Monday-reset accounts).
        current_weeks = c._compute_subscription_weeks(
            conn, now, now + dt.timedelta(microseconds=1), config=config,
        )
        if current_weeks:
            cw_start = parse_iso_datetime(
                current_weeks[0].start_ts, "week.start_ts"
            ).astimezone(dt.timezone.utc)
        else:
            # No snapshots available: fall back to a Monday-anchored week.
            cw_start = (now - dt.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        if args.weeks is not None:
            since_dt = cw_start - dt.timedelta(days=7 * (args.weeks - 1))
        else:
            since_dt = cw_start
        until_dt = now

    # Pre-compute subscription-week bounds for the query window so each entry
    # can be bucketed onto a canonical subscription-week start_ts. Mirrors
    # `_aggregate_weekly`'s bisect pattern (first-match-wins on overlap).
    subweeks = c._compute_subscription_weeks(
        conn, since_dt, until_dt, config=config,
    )
    parsed_bounds: list[tuple[dt.datetime, dt.datetime]] = []
    for sw in subweeks:
        s_dt = parse_iso_datetime(sw.start_ts, "week.start_ts").astimezone(dt.timezone.utc)
        e_dt = parse_iso_datetime(sw.end_ts, "week.end_ts").astimezone(dt.timezone.utc)
        parsed_bounds.append((s_dt, e_dt))
    week_starts = [b[0] for b in parsed_bounds]

    def _week_start_for(ts: dt.datetime) -> dt.datetime | None:
        """Return the canonical subscription-week start_dt for `ts`, or None
        if `ts` falls outside every SubWeek interval (may happen near the
        boundaries of the requested [since_dt, until_dt] window)."""
        ts_utc = ts.astimezone(dt.timezone.utc)
        idx = bisect.bisect_right(week_starts, ts_utc) - 1
        if idx < 0:
            return None
        # First-match-wins on Anthropic reset-day-drift overlap (same
        # walk-back as `_aggregate_weekly`).
        while idx > 0:
            prev_start, prev_end = parsed_bounds[idx - 1]
            if prev_start <= ts_utc < prev_end:
                idx -= 1
            else:
                break
        s_dt, e_dt = parsed_bounds[idx]
        if s_dt <= ts_utc < e_dt:
            return s_dt
        return None

    # Pre-lower filter patterns (substring, OR semantics, repeatable).
    project_patterns = [p.lower() for p in (args.project or [])]
    model_patterns = [m.lower() for m in (args.model or [])]

    # Widen scan to full subscription-week bounds so the attribution
    # denominator includes ALL week cost, even entries outside the
    # user's [since_dt, until_dt] slice. Visible buckets are still
    # gated on the user slice below. Without this, a partial-week
    # --since/--until slice understates the denominator and inflates
    # every row's Used %.
    if parsed_bounds:
        scan_start = min(since_dt, parsed_bounds[0][0])
        scan_end = max(until_dt, parsed_bounds[-1][1])
    else:
        scan_start, scan_end = since_dt, until_dt

    resolver_cache: dict[str, ProjectKey] = {}
    buckets: dict[tuple[ProjectKey, dt.datetime], dict] = {}
    total_cost_by_week: dict[dt.datetime, float] = {}
    unknown_entry_count = 0
    missing_sid_count = 0

    # Issue #89: materialize the joined-entry iterator once so we can
    # (a) pre-compute the --debug report's scope (entries passing all
    # rendered-row filters — user slice + --model + --project) BEFORE
    # the aggregation loop runs and (b) preserve the existing
    # aggregation semantics (denominator widened to ALL entries; visible
    # rows only the post-filter subset). The list is small enough to
    # hold (entries already in memory via the cache row factory).
    joined_entries_all = list(c.get_claude_session_entries(scan_start, scan_end))

    # Build the --debug report dataset: skip synthetic + out-of-window
    # entries, then apply --model and --project filters (mirroring the
    # exact predicate at the aggregation loop below). This must match
    # the rendered scope, NOT the denominator scope.
    if getattr(args, "debug", False):
        filtered_for_report = []
        for je in joined_entries_all:
            if je.model == "<synthetic>":
                continue
            if _week_start_for(je.timestamp) is None:
                continue
            if je.timestamp < since_dt or je.timestamp > until_dt:
                continue
            if model_patterns:
                mname = (je.model or "").lower()
                if not any(p in mname for p in model_patterns):
                    continue
            key_for_filter = c._resolve_project_key(
                je.project_path, args.group, resolver_cache,
            )
            if not c._project_filter_matches(key_for_filter, project_patterns):
                continue
            filtered_for_report.append(je)
        c._emit_debug_samples_if_set(
            args,
            [c._usage_entry_from_joined(je) for je in filtered_for_report],
            command_label="project",
        )

    for entry in joined_entries_all:
        # Skip synthetic entries (Claude Code internal markers) to match
        # `_aggregate_cache_by_session` / `_aggregate_claude_sessions`.
        if entry.model == "<synthetic>":
            continue

        week_start = _week_start_for(entry.timestamp)
        if week_start is None:
            continue

        entry_cost = c._calculate_entry_cost(
            entry.model,
            {
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "cache_creation_input_tokens": entry.cache_creation_tokens,
                "cache_read_input_tokens": entry.cache_read_tokens,
            },
            mode="auto",
            cost_usd=entry.cost_usd,
        )

        # Denominator: always contribute (whole-week attribution) so
        # `--model`/`--project`/partial-slice do NOT rescale it.
        total_cost_by_week[week_start] = (
            total_cost_by_week.get(week_start, 0.0) + entry_cost
        )

        # User-slice gate: visible rows only include entries within
        # [since_dt, until_dt]. Entries outside the slice still
        # contributed to the denominator above.
        if entry.timestamp < since_dt or entry.timestamp > until_dt:
            continue

        if model_patterns:
            mname = (entry.model or "").lower()
            if not any(p in mname for p in model_patterns):
                continue

        key = c._resolve_project_key(entry.project_path, args.group, resolver_cache)
        if key.is_unknown:
            unknown_entry_count += 1

        # --project filter: match against display_key OR the underlying
        # path (git_root / bucket_path). Matching only display_key makes
        # basename-collision suffixes (e.g. `foo (repos)`) impossible to
        # select by their path segment.
        if project_patterns:
            dname = key.display_key.lower()
            pname = (key.git_root or key.bucket_path or "").lower()
            if not any((p in dname) or (p in pname) for p in project_patterns):
                continue

        if entry.session_id is None:
            missing_sid_count += 1

        bkey = (key, week_start)
        b = buckets.get(bkey)
        if b is None:
            b = {
                "key": key,
                "week_start": week_start,
                "sessions": set(),
                "first_seen": entry.timestamp,
                "last_seen": entry.timestamp,
                "input": 0, "output": 0,
                "cache_write": 0, "cache_read": 0,
                "cost_usd": 0.0,
                "models": {},
            }
            buckets[bkey] = b
        _accumulate_entry_into_bucket(b, entry, pre_computed_cost=entry_cost)

    if unknown_entry_count > 0:
        eprint(
            f"Warning: {unknown_entry_count} entries lacked project_path — "
            f"run `cache-sync` to backfill."
        )
    if missing_sid_count > 0:
        eprint(
            f"Warning: {missing_sid_count} entries lacked session_files "
            f"session_id — run `cache-sync` to backfill."
        )

    # --- Attribution math (Task 5) -----------------------------------------
    # Load per-week `weekly_percent` (max within window) for every week that
    # intersects [since_dt, until_dt]. Missing snapshots are tracked so we
    # can surface `weeksMissingSnapshot` in the output — those weeks can't
    # contribute to attributed %.
    week_snapshots: dict[dt.datetime, float] = _load_week_snapshots(
        since_dt, until_dt
    )

    # Set of every week the user asked about (from the computed SubWeek
    # bounds), used to report `weeksInRange` and `weeksMissingSnapshot`
    # independent of whether that week had any project activity.
    weeks_in_range: set[dt.datetime] = {ws for ws in week_starts}
    weeks_missing_snapshot: set[dt.datetime] = {
        ws for ws in weeks_in_range if ws not in week_snapshots
    }

    # Collapse (project_key, week) buckets into one row per project, summing
    # tokens / cost / sessions / first_seen / last_seen / models across the
    # weeks the project appears in.
    #
    # Attribution: for each (project P, week W) bucket,
    #     attributed_pct[P,W] = (cost[P,W] / total_cost[W]) * weekly_percent[W]
    # iff a snapshot exists for W. Weeks without a snapshot contribute None
    # (their weeks are already counted in `weeks_missing_snapshot`).
    project_rows: dict[str, dict] = {}
    for (key, wstart), b in buckets.items():
        row = project_rows.get(key.bucket_path)
        if row is None:
            row = {
                "key": key,
                "sessions": set(),
                "first_seen": b["first_seen"],
                "last_seen": b["last_seen"],
                "input": 0, "output": 0,
                "cache_write": 0, "cache_read": 0,
                "cost_usd": 0.0,
                # `None` until the first week with a snapshot contributes —
                # preserves the distinction between "every contributing week
                # lacked a snapshot" (→ None) and "genuine zero attribution"
                # (→ 0.0 after a real contribution). Spec §3.
                "attributed_pct": None,
                "models": {},
            }
            project_rows[key.bucket_path] = row
        row["sessions"] |= b["sessions"]
        if b["first_seen"] < row["first_seen"]:
            row["first_seen"] = b["first_seen"]
        if b["last_seen"] > row["last_seen"]:
            row["last_seen"] = b["last_seen"]
        row["input"] += b["input"]
        row["output"] += b["output"]
        row["cache_write"] += b["cache_write"]
        row["cache_read"] += b["cache_read"]
        row["cost_usd"] += b["cost_usd"]

        # Merge per-model sub-buckets.
        for model, mb in b["models"].items():
            rm = row["models"].get(model)
            if rm is None:
                rm = {
                    "cost_usd": 0.0,
                    "input": 0, "output": 0,
                    "cache_write": 0, "cache_read": 0,
                    "first_seen": mb["first_seen"],
                    "last_seen": mb["last_seen"],
                }
                row["models"][model] = rm
            if mb["first_seen"] < rm["first_seen"]:
                rm["first_seen"] = mb["first_seen"]
            if mb["last_seen"] > rm["last_seen"]:
                rm["last_seen"] = mb["last_seen"]
            rm["cost_usd"] += mb["cost_usd"]
            rm["input"] += mb["input"]
            rm["output"] += mb["output"]
            rm["cache_write"] += mb["cache_write"]
            rm["cache_read"] += mb["cache_read"]

        # Attribution contribution (only if this week has a snapshot and
        # the week has nonzero total cost — a zero denominator would make
        # the ratio meaningless). `attributed_pct` stays `None` until the
        # first real contribution; subsequent contributions accumulate.
        week_pct = week_snapshots.get(wstart)
        week_total = total_cost_by_week.get(wstart, 0.0)
        if week_pct is not None and week_total > 0:
            contribution = (b["cost_usd"] / week_total) * week_pct
            row["attributed_pct"] = (
                (row["attributed_pct"] or 0.0) + contribution
            )

    # Compute $/1% per project: `cost_per_pct = cost_usd / attributed_pct`
    # when attribution is positive; None otherwise (e.g. every contributing
    # week lacked a snapshot — `attributed_pct` still None — or attribution
    # came out to zero).
    for row in project_rows.values():
        ap = row["attributed_pct"]
        if ap is not None and ap > 0:
            row["cost_per_pct"] = row["cost_usd"] / ap
        else:
            row["cost_per_pct"] = None

    # Collect warnings to surface in the JSON payload (terminal path emits
    # them inline via eprint earlier, so this list stays JSON-specific).
    warnings: list[str] = []
    if unknown_entry_count > 0:
        warnings.append(
            f"{unknown_entry_count} entries lacked project_path — "
            f"run `cache-sync` to backfill."
        )
    if missing_sid_count > 0:
        warnings.append(
            f"{missing_sid_count} entries lacked session_files session_id — "
            f"run `cache-sync` to backfill."
        )

    # Honor --sort / --order. For numeric keys, `_project_sort_key` flips the
    # primary-key sign to match the requested direction so natural `sorted()`
    # ordering already produces the right answer; the dname tie-break stays
    # ascending in both directions (ties never invert alphabetically). For
    # `name`, the key is asc-natural (a-z) and `reverse=` is used for desc.
    if args.sort == "name":
        sorted_rows = sorted(
            project_rows.values(),
            key=lambda r: _project_sort_key(r, args.sort, args.order),
            reverse=(args.order == "desc"),
        )
    else:
        sorted_rows = sorted(
            project_rows.values(),
            key=lambda r: _project_sort_key(r, args.sort, args.order),
        )

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Privacy invariant (Section 8.4 / 5.3): the wrapper runs `_lib_share._scrub`
    # before rendering, so default output anonymizes project labels to
    # `project-1` / `project-2` / ...; `--reveal-projects` opts back in.
    # The builder populates `ProjectCell.label` / `ChartPoint.project_label`
    # / `ChartPoint.x_label` with REAL names; the wrapper-level scrubber is
    # the single chokepoint that rewrites them.
    if getattr(args, "format", None):
        # Note: --breakdown is a no-op under --format (snapshot focuses on
        # the headline per-project usage table + HBar chart; per-model
        # sub-rows aren't in the share spec scope). Same convention as
        # cmd_daily / cmd_weekly / cmd_report.
        display_tz_str = c._share_display_tz_label(args._resolved_tz)
        snap = c._build_project_snapshot(
            list(sorted_rows),
            period_start=since_dt,
            period_end=until_dt,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
        )
        c._share_render_and_emit(snap, args)
        return 0

    if args.json:
        print(_project_json_output(
            since=since_dt,
            until=until_dt,
            weeks_in_range=len(weeks_in_range),
            group_mode=args.group,
            rows=sorted_rows,
            weeks_missing_snapshot=weeks_missing_snapshot,
            warnings=warnings,
            include_breakdown=args.breakdown,
            week_snapshots=week_snapshots,
        ))
        return 0

    # Terminal path
    range_label = f"{since_dt.date().isoformat()} \u2014 {until_dt.date().isoformat()}"
    title = f"Claude Token Usage Report - Projects ({range_label})"

    if not sorted_rows:
        eprint("No project usage found in range.")
        return 0

    # Session A (spec §7.3): the new --color flag overrides NO_COLOR
    # env; --no-color overrides FORCE_COLOR env; deny-wins on the
    # --color + --no-color clash. _resolve_color_enabled returns the
    # effective bool; pass it as ``color=`` so the renderer skips its
    # internal _supports_color_stdout() auto-detect (which would
    # re-consult NO_COLOR and incorrectly disable color when the user
    # passed --color under NO_COLOR=1).
    print(c._render_project_table(
        sorted_rows,
        title=title,
        breakdown=args.breakdown,
        weeks_missing_snapshot=len(weeks_missing_snapshot),
        weeks_in_range=len(weeks_in_range),
        color=c._resolve_color_enabled(args),
        compact=args.compact,
    ))
    return 0
