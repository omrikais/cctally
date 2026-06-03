# bin/_cctally_reporting.py
"""Claude reporting command family.

Holds the four Claude reporting commands — `cmd_daily`, `cmd_monthly`,
`cmd_weekly`, `cmd_session` — and the daily-only render helper
`_emit_daily_view_table_or_json`.

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). This module
references the bin/cctally RE-EXPORTED names of every library kernel it
needs (`build_daily_view`, `_render_bucket_table`, `_compute_subscription_weeks`,
`_build_daily_snapshot`, …) — NOT the `_lib_*` module objects — so NO
qualified `_lib_*` import is required; every such name is reached via the
call-time `_cctally()` accessor so test monkeypatches through `cctally`'s
namespace are preserved (spec §3.2). The shared join/filter helpers
(`_usage_entry_from_joined`, `_project_filter_matches`, `_parse_project_aliases`,
`_alias_for`, `_resolve_session_id_for_filter`, …) STAY in bin/cctally; the
week-boundary infra (`get_recent_weeks`, `_apply_reset_events_to_weekrefs`,
`_get_canonical_boundary_for_date`) lives in `_cctally_weekrefs.py`
(re-exported on the cctally ns). Both groups are reached via `c.`.

bin/cctally re-exports EVERY moved symbol (eager): the parser resolves
`c.cmd_daily` / `c.cmd_monthly` / `c.cmd_weekly` / `c.cmd_session`; tests
retrieve `ns["cmd_session"]` etc. off the `cctally` namespace.

Spec: docs/superpowers/specs/2026-05-31-extract-codex-reporting-cmd-design.md
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from typing import Any

from _cctally_core import _command_as_of, eprint, open_db, parse_iso_datetime
from _lib_fmt import stable_sum


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.2)."""
    return sys.modules["cctally"]


# === moved verbatim from bin/cctally (Regions R1–R2) ===


def _emit_daily_view_table_or_json(view, args):
    """Order + emit a DailyView as the flat daily table or {daily} JSON.

    Shared by cmd_daily's default path and its -p-only (filter, no grouping)
    path so the two cannot drift. Body is exactly the default path's order +
    emit tail; callers keep their own --format share gate upstream of this.
    """
    c = _cctally()
    days = list(reversed(view.aggregated))
    if args.order == "desc":
        days = list(reversed(days))
    if args.json:
        print(c._bucket_to_json(days, list_key="daily", date_key="date"))
        return
    print(c._render_bucket_table(
        days,
        first_col_name="Date",
        title_suffix="Daily",
        compact_split_fn=c._daily_compact_split,
        breakdown=args.breakdown,
        compact=getattr(args, "compact", False),
    ))


def cmd_daily(args: argparse.Namespace) -> int:
    """Show usage report grouped by display-timezone date."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz so the
    # existing resolve_display_tz precedence absorbs the new alias. The
    # canonical --tz still wins (it's set on the namespace before this
    # bridge fires); when --tz is unset and -z is supplied, use -z.
    c._bridge_z_into_tz(args, config)
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz

    range = c._parse_cli_date_range(
        args,
        tz_name=(tz.key if tz is not None else None),
        now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    # ── Project-axis path (issue #86 Session E / T1.11) ────────────────────
    # Gated by -i/--instances or -p/--project; the default path below is
    # untouched/byte-stable. Mirrors cmd_project's I/O-layer git-root
    # resolution + substring-OR-path filter.
    aliases = c._parse_project_aliases(getattr(args, "project_aliases", None))
    project_patterns = [p.lower() for p in (getattr(args, "project", None) or [])]

    if getattr(args, "instances", False) or project_patterns:
        joined = list(c.get_claude_session_entries(range_start, range_end))
        resolver_cache: dict = {}
        keyed: list = []              # [(ProjectKey, UsageEntry)] — for -i grouping
        filtered_uentries: list = []  # UsageEntry — for -p-only / --format / debug
        for je in joined:
            if je.model == "<synthetic>":
                continue
            key = c._resolve_project_key(je.project_path, "git-root", resolver_cache)
            if project_patterns and not c._project_filter_matches(key, project_patterns):
                continue
            ue = c._usage_entry_from_joined(je)
            keyed.append((key, ue))
            filtered_uentries.append(ue)

        # Debug scope = the filtered entries (mirrors cmd_project).
        c._emit_debug_samples_if_set(args, filtered_uentries, command_label="daily")

        # --format share gate: -i is a no-op (no project-section share render),
        # but -p IS honored by building the snapshot from the filtered view.
        if getattr(args, "format", None):
            view = c.build_daily_view(filtered_uentries, now_utc=_command_as_of(),
                                    display_tz=tz, mode=args.mode)
            display_tz_str = c._share_display_tz_label(tz)
            snap = c._build_daily_snapshot(
                view, period_start=range_start, period_end=range_end,
                display_tz=display_tz_str, version=c._share_resolve_version(),
                theme=args.theme, reveal_projects=args.reveal_projects,
            )
            if args.order == "desc":
                snap = dataclasses.replace(snap, rows=tuple(reversed(snap.rows)))
            c._share_render_and_emit(snap, args)
            return 0

        if getattr(args, "instances", False):
            groups = c._aggregate_daily_by_project(keyed, tz=tz, mode=args.mode)
            aug = c._project_disambiguate_labels(
                [{"key": k, "cost_usd": stable_sum(b.cost_usd for b in bl)}
                 for k, bl in groups]
            )
            json_groups: list = []
            table_groups: list = []
            # `_project_disambiguate_labels` only suffixes the immediate
            # parent-dir basename, so two distinct git-roots like
            # `/a/x/app` + `/b/x/app` both resolve to `app (x)`. Guarantee
            # per-group JSON-key uniqueness with a counter suffix on any
            # residual collision — otherwise `_bucket_by_project_to_json`'s
            # `projects[label] = ...` silently overwrites the earlier group
            # (data loss in --json). The table_label derives from the now-
            # unique json_label, so section headers stay distinct too.
            # `json_label`s are unique by construction (the `(#N)` counter
            # above). Table labels, however, can re-collide: `_alias_for`
            # matches on `display_key` first, so a basename alias like
            # `--project-aliases app=Alias` maps BOTH same-basename git-roots
            # to "Alias" — re-merging the exact sections this feature
            # disambiguates. Apply the SAME `(#N)` counter to table labels so
            # the two distinct-total sections stay tellable apart (JSON keys
            # are untouched — they use the non-aliased `json_label`).
            seen_json_labels: dict[str, int] = {}
            seen_table_labels: dict[str, int] = {}
            for i, (k, bl) in enumerate(groups):
                ordered = list(reversed(bl)) if args.order == "desc" else bl
                base_json_label = aug.get(i, k.display_key)
                n = seen_json_labels.get(base_json_label, 0) + 1
                seen_json_labels[base_json_label] = n
                json_label = (
                    base_json_label if n == 1 else f"{base_json_label} (#{n})"
                )
                base_table_label = c._alias_for(k, aliases) or json_label
                nt = seen_table_labels.get(base_table_label, 0) + 1
                seen_table_labels[base_table_label] = nt
                table_label = (
                    base_table_label if nt == 1
                    else f"{base_table_label} (#{nt})"
                )
                json_groups.append((json_label, ordered))
                table_groups.append((table_label, ordered))
            if args.json:
                print(c._bucket_by_project_to_json(json_groups, date_key="date"))
                return 0
            print(c._render_bucket_table(
                [], first_col_name="Date", title_suffix="Daily",
                compact_split_fn=c._daily_compact_split,
                breakdown=args.breakdown,
                compact=getattr(args, "compact", False),
                project_groups=table_groups,
            ))
            return 0

        # -p only (no -i): filter-only → normal date-aggregated daily output.
        view = c.build_daily_view(filtered_uentries, now_utc=_command_as_of(),
                                display_tz=tz, mode=args.mode)
        _emit_daily_view_table_or_json(view, args)
        return 0

    # ── Default path (UNCHANGED) ───────────────────────────────────────────
    # Collect entries.
    all_entries = c.get_entries(range_start, range_end)

    c._emit_debug_samples_if_set(
        args, all_entries, command_label="daily",
    )

    # Build the unified daily view (spec §5.1: gap-free; the dashboard
    # heatmap's contiguous-window materialization stays at the dashboard
    # envelope adapter so CLI byte-stability is preserved). Consume
    # `view.aggregated` (BucketUsage tuple) for the CLI renderers — the
    # JSON shape's `bucket` / `model_breakdowns` / `models: list[str]`
    # fields live on BucketUsage, not on DailyPanelRow. The builder's
    # `_aggregate_daily` call is the same one we used inline.
    view = c.build_daily_view(all_entries, now_utc=_command_as_of(),
                            display_tz=tz, mode=args.mode)

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Gate runs BEFORE the `--order desc` reversal so the BarChart bars
    # render chronologically regardless of `--order`. Table rows in the
    # rendered artifact, however, must respect `--order desc` (parity
    # with terminal / JSON output) — handled by reversing snap.rows
    # post-build below; the chart points stay chronological because
    # they were built from ascending `days`.
    if getattr(args, "format", None):
        # Note: --breakdown is a no-op under --format (snapshot focuses on
        # the headline daily-cost trend; per-model sub-rows aren't in the
        # share spec scope). Same convention applies to other share-enabled
        # subcommands (cmd_report's --detail, etc.).
        display_tz_str = c._share_display_tz_label(tz)
        snap = c._build_daily_snapshot(
            view,
            period_start=range_start,
            period_end=range_end,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
        )
        if args.order == "desc":
            snap = dataclasses.replace(snap, rows=tuple(reversed(snap.rows)))
        c._share_render_and_emit(snap, args)
        return 0

    # Order + emit the flat daily table / {daily} JSON. Extracted into
    # `_emit_daily_view_table_or_json` so this default path and the
    # -p-only (filter, no grouping) path above stay byte-identical.
    _emit_daily_view_table_or_json(view, args)
    return 0


def cmd_monthly(args: argparse.Namespace) -> int:
    """Show usage report grouped by display-timezone calendar month."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    c._bridge_z_into_tz(args, config)
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz

    range = c._parse_cli_date_range(
        args,
        tz_name=(tz.key if tz is not None else None),
        now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    all_entries = c.get_entries(range_start, range_end)

    c._emit_debug_samples_if_set(
        args, all_entries, command_label="monthly",
    )

    # Build the unified monthly view (spec §5.2: drops boundary-spillover
    # bucket; computes delta_cost_pct internally). Consume
    # `view.aggregated` (BucketUsage tuple, newest-first) for CLI byte-
    # stability — `_bucket_to_json` reads BucketUsage fields not present
    # on MonthlyPeriodRow.
    #
    # Pass a large `n` so the CLI's `--since`/`--until` window controls
    # how many months render (the dashboard caps at n=12; CLI doesn't).
    view = c.build_monthly_view(all_entries, now_utc=_command_as_of(),
                              n=10**6, display_tz=tz, mode=args.mode)
    # The view stores `aggregated` newest-first; CLI default is asc.
    months = list(reversed(view.aggregated))

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Gate runs BEFORE the `--order desc` reversal so the BarChart bars
    # render chronologically regardless of `--order`. Table rows in the
    # rendered artifact respect `--order desc` (parity with terminal /
    # JSON) via post-build snap.rows reversal; chart stays chronological.
    if getattr(args, "format", None):
        # Note: --breakdown is a no-op under --format (snapshot focuses on
        # the headline monthly-cost trend; per-model sub-rows aren't in the
        # share spec scope). Same convention as cmd_daily / cmd_report.
        display_tz_str = c._share_display_tz_label(tz)
        snap = c._build_monthly_snapshot(
            view,
            period_start=range_start,
            period_end=range_end,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
        )
        if args.order == "desc":
            snap = dataclasses.replace(snap, rows=tuple(reversed(snap.rows)))
        c._share_render_and_emit(snap, args)
        return 0

    if args.order == "desc":
        months = list(reversed(months))

    if args.json:
        print(c._bucket_to_json(months, list_key="monthly", date_key="month"))
        return 0

    print(c._render_bucket_table(
        months,
        first_col_name="Month",
        title_suffix="Monthly",
        compact_split_fn=c._monthly_compact_split,
        breakdown=args.breakdown,
        compact=getattr(args, "compact", False),
    ))
    return 0


def cmd_weekly(args: argparse.Namespace) -> int:
    """Show Claude usage grouped by subscription week."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    c._bridge_z_into_tz(args, config)
    args._resolved_tz = c.resolve_display_tz(args, config)

    now_utc = _command_as_of()
    range = c._parse_cli_date_range(args, now_utc=now_utc)
    if isinstance(range, int):
        return range
    range_start, range_end = range

    conn = open_db()

    # Build the subscription-week list spanning the range. Boundaries are
    # anchored in `weekly_usage_snapshots` when available and otherwise
    # extrapolated (see `_compute_subscription_weeks`). Pass the
    # `--config`-honoring resolved config (issue #88) so the no-snapshot
    # calendar-week fallback uses the explicit override's `week_start`.
    weeks = c._compute_subscription_weeks(
        conn, range_start, range_end, config=config,
    )

    # Fetch entries and aggregate.
    # Cover each SubWeek's full [start_ts, end_ts) on the range_start side —
    # `_compute_subscription_weeks` can emit weeks whose start_ts precedes
    # range_start (any week overlapping the range). Without widening, boundary
    # weeks get tail-only cost divided by full-week usedPct → understated
    # totalCost and $/1%. range_end stays as the upper bound so historical
    # `--until <past>` queries still clip the tail week (paired with the
    # as_of_utc bound on get_latest_usage_for_week below).
    if weeks:
        fetch_start = min(
            range_start,
            parse_iso_datetime(weeks[0].start_ts, "week_start_at"),
        )
    else:
        fetch_start = range_start
    all_entries = c.get_entries(fetch_start, range_end)

    c._emit_debug_samples_if_set(
        args, all_entries, command_label="weekly",
    )

    # Bound the usage-snapshot lookup to `<= range_end` so historical
    # `--until <past date>` queries pick the usage% that was current at
    # the end of the requested window rather than the globally latest
    # snapshot for the week. Cost is already truncated to `range_end` by
    # `_aggregate_weekly`, so using a later usedPct would produce a
    # silently wrong $/1%. Match the stored `captured_at_utc` format
    # (see now_utc_iso): UTC, seconds precision, `Z` suffix — otherwise
    # lexicographic string compare inside SQLite would misorder `+00:00`
    # vs. `Z` at the same instant.
    as_of_utc = (
        range_end.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Build the unified weekly view (spec §5.3): runs _aggregate_weekly,
    # overlays weekly_usage_snapshots per WeekRef. view.aggregated is
    # the BucketUsage tuple newest-first; view.overlay is the parallel
    # (used_pct, dollar_per_pct) tuple. We reverse both for CLI's
    # default asc rendering so the existing renderer's len-equality
    # assertions stay aligned.
    view = c.build_weekly_view(
        conn, all_entries, weeks=weeks, now_utc=now_utc,
        display_tz=args._resolved_tz, as_of_utc=as_of_utc, mode=args.mode,
    )
    buckets = list(reversed(view.aggregated))
    overlay = list(reversed(view.overlay))

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Gate runs BEFORE the `--order desc` reversal so the BarChart bars
    # render chronologically regardless of `--order`. Table rows in the
    # rendered artifact respect `--order desc` (parity with terminal /
    # JSON) via post-build snap.rows reversal. `--breakdown` is honored:
    # when set, the snapshot adds per-model columns + stacked bar series
    # (vs. cmd_daily / cmd_monthly where --breakdown is a no-op under
    # --format).
    if getattr(args, "format", None):
        display_tz_str = c._share_display_tz_label(args._resolved_tz)
        snap = c._build_weekly_snapshot(
            view,
            period_start=range_start,
            period_end=range_end,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
            breakdown_model=bool(getattr(args, "breakdown", False)),
        )
        if args.order == "desc":
            snap = dataclasses.replace(snap, rows=tuple(reversed(snap.rows)))
        c._share_render_and_emit(snap, args)
        return 0

    # Apply sort order. Buckets and overlay must reverse together so their
    # indices stay aligned (both _render_weekly_table and _weekly_to_json
    # assert len equality).
    if args.order == "desc":
        buckets = list(reversed(buckets))
        overlay = list(reversed(overlay))

    if args.json:
        print(c._weekly_to_json(buckets, weeks, overlay))
        return 0

    if not buckets:
        print("No Claude usage found.")
        return 0

    print(c._render_weekly_table(
        buckets,
        overlay,
        weeks=weeks,
        compact_split_fn=c._daily_compact_split,
        breakdown=args.breakdown,
        compact=getattr(args, "compact", False),
    ))
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Show Claude usage grouped by sessionId (merges resumed-across-files sessions)."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    c._bridge_z_into_tz(args, config)
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz

    range = c._parse_cli_date_range(
        args,
        tz_name=(tz.key if tz is not None else None),
        now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    entries = c.get_claude_session_entries(range_start, range_end)

    # Issue #89: --debug report describes the joined-entry list filtered
    # by --id (post-fallback session_id resolution) when set, matching
    # the rendered scope of `sessions`.
    # `is not None`, not truthiness: an explicit empty `--id ''` must still
    # engage the filter (→ empty render), not silently fall through to
    # "describe/show all sessions" (code-review finding). Mirrored on the
    # post-aggregation filter below.
    if getattr(args, "id", None) is not None:
        joined_for_report = [
            je for je in entries
            if c._resolve_session_id_for_filter(je) == args.id
        ]
    else:
        joined_for_report = entries
    c._emit_debug_samples_if_set(
        args,
        [c._usage_entry_from_joined(je) for je in joined_for_report],
        command_label="session",
    )

    # Unified view-model kernel (spec §6.5). `limit=None` keeps the
    # full aggregator output — `cctally session` has no `--limit` flag
    # and emits every session in the requested range. `view.aggregated`
    # is the `list[ClaudeSessionUsage]` shape the legacy CLI / share
    # renderers consume (table, --json, share-snapshot); `view.rows`
    # is the typed `TuiSessionRow` tuple reserved for the TUI /
    # dashboard wiring in Task 15 / 16. Keeping both shapes parallel
    # at the builder preserves the resumed-session merge invariant
    # documented in CLAUDE.md (one sessionId across multiple JSONL
    # files collapses to ONE entry in BOTH tuples).
    view = c.build_sessions_view(
        entries, now_utc=_command_as_of(), limit=None, display_tz=tz,
        mode=args.mode,
    )
    sessions = list(view.aggregated)

    # Session A (spec §7.4): exact-string filter on sessionId. Applied
    # AFTER aggregation (so resume-merged sessions across multiple JSONL
    # files are matched against their post-merge id) and BEFORE the
    # `--order asc` reversal and the JSON / share / table render
    # branches. Unknown id → empty `sessions` list, which falls through
    # to the existing "no sessions" branch (table: "No Claude session
    # data found."; JSON: `{"sessions": []}`).
    if getattr(args, "id", None) is not None:  # explicit '' still filters
        sessions = [s for s in sessions if s.session_id == args.id]

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Privacy invariant (Section 8.4 / 5.3): the wrapper runs `_lib_share._scrub`
    # before rendering, so default output anonymizes project labels to
    # `project-1` / `project-2` / ...; `--reveal-projects` opts back in.
    # The builder populates `ProjectCell.label` / `ChartPoint.project_label`
    # / `ChartPoint.x_label` with REAL basenames; the wrapper-level scrubber
    # is the single chokepoint that rewrites them.
    if getattr(args, "format", None):
        # --top-n validation. Spec convention (Implementor 6 fix-loop):
        # invalid flag combinations exit 2; the soft-warn upper threshold
        # (>50) writes to stderr but proceeds.
        top_n = getattr(args, "top_n", None)
        if top_n is not None:
            if top_n < 1:
                print(
                    "cctally: --top-n must be >= 1",
                    file=sys.stderr,
                )
                return 2
            if top_n > 50:
                sys.stderr.write(
                    f"cctally: --top-n {top_n} will likely produce an "
                    "unreadable chart (consider 15 or fewer)\n"
                )
        # Note: --breakdown is a no-op under --format (snapshot focuses
        # on the headline per-session usage table + HBar chart; per-model
        # sub-rows aren't in the share spec scope). Same convention as
        # cmd_daily / cmd_project.
        display_tz_str = c._share_display_tz_label(tz)
        # Session A (spec §7.4): `_build_session_snapshot` reads
        # `view.aggregated`, so the `--id` filter applied to the local
        # `sessions` list above would otherwise be ignored for share
        # exports (HTML/Markdown/SVG). Hand the builder a view whose
        # `aggregated` is the filtered list so `--id` is honored across
        # every output path. The builder reads only `aggregated` (never
        # `rows` / `total_sessions`), so the parallel-tuple mismatch from
        # the replace is inert here.
        share_view = dataclasses.replace(view, aggregated=tuple(sessions))
        snap = c._build_session_snapshot(
            share_view,
            period_start=range_start,
            period_end=range_end,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
            top_n=top_n,
            tz=tz,
        )
        c._share_render_and_emit(snap, args)
        return 0

    # Aggregator returns descending by last_activity; --order asc reverses.
    if args.order == "asc":
        sessions = list(reversed(sessions))

    if args.json:
        print(c._claude_sessions_to_json(sessions))
        return 0

    if not sessions:
        print("No Claude session data found.")
        return 0

    # Session A (spec §7.6.1; Review-A P2-B): thread --compact through
    # so the renderer's scale-down branch fires regardless of terminal
    # width when the flag is set.
    print(c._render_claude_session_table(
        sessions,
        breakdown=args.breakdown,
        tz=tz,
        compact=getattr(args, "compact", False),
    ))
    return 0


def cmd_range_cost(args: argparse.Namespace) -> int:
    c = _cctally()
    # Session A (spec §7.2 / §7.6 row note): range-cost has no --tz of
    # its own — start/end carry their own zone via ISO 8601. Calling the
    # bridge keeps the alias-surface contract uniform across the 10
    # in-scope cmds: -z/--timezone lands on args.tz unchanged (no
    # downstream consumer), so this is a documented no-op for this
    # command. The bridge still runs _resolve_claude_tz_name so the §9.2a
    # production-path coverage is exercised here too.
    config = c._load_claude_config_for_args(args)
    c._bridge_z_into_tz(args, config)
    start_dt = parse_iso_datetime(args.start, "--start")
    if args.end:
        end_dt = parse_iso_datetime(args.end, "--end")
    else:
        # internal fallback: host-local intentional
        end_dt = dt.datetime.now().astimezone()
    if end_dt < start_dt:
        eprint("Error: --end must be after --start")
        return 1

    total_cost = 0.0
    matched_entries = 0
    first_match: dt.datetime | None = None
    last_match: dt.datetime | None = None
    model_buckets: dict[str, dict[str, Any]] = {}

    # Issue #89: keep the loaded list around so the --debug report can
    # describe the same dataset as the rendered output. Project filter is
    # applied at the loader (SELECT-time), so the scope is the same.
    # P2.2 (issue #89 review-loop): get_entries already returns
    # list[UsageEntry] per bin/_cctally_cache.py:1224 — no list() wrap.
    entries_list = c.get_entries(start_dt, end_dt, project=args.project)
    c._emit_debug_samples_if_set(
        args, entries_list, command_label="range-cost",
    )

    for entry in entries_list:
        cost = c._calculate_entry_cost(
            entry.model, entry.usage, mode=args.mode, cost_usd=entry.cost_usd,
        )
        total_cost += cost
        matched_entries += 1

        if first_match is None or entry.timestamp < first_match:
            first_match = entry.timestamp
        if last_match is None or entry.timestamp > last_match:
            last_match = entry.timestamp

        if entry.model not in model_buckets:
            model_buckets[entry.model] = {
                "entries": 0, "inputTokens": 0, "outputTokens": 0,
                "cacheCreationTokens": 0, "cacheReadTokens": 0, "costUSD": 0.0,
            }
        b = model_buckets[entry.model]
        b["entries"] += 1
        b["inputTokens"] += entry.usage.get("input_tokens", 0)
        b["outputTokens"] += entry.usage.get("output_tokens", 0)
        b["cacheCreationTokens"] += entry.usage.get("cache_creation_input_tokens", 0)
        b["cacheReadTokens"] += entry.usage.get("cache_read_input_tokens", 0)
        b["costUSD"] += cost

    if args.total_only:
        print(f"{total_cost:.9f}")
        return 0

    if args.json:
        breakdowns = []
        for model in sorted(model_buckets, key=lambda m: -model_buckets[m]["costUSD"]):
            b = model_buckets[model]
            total_tokens = (
                b["inputTokens"] + b["outputTokens"]
                + b["cacheCreationTokens"] + b["cacheReadTokens"]
            )
            breakdowns.append({
                "model": model,
                "entries": b["entries"],
                "inputTokens": b["inputTokens"],
                "outputTokens": b["outputTokens"],
                "cacheCreationTokens": b["cacheCreationTokens"],
                "cacheReadTokens": b["cacheReadTokens"],
                "totalTokens": total_tokens,
                "costUSD": round(b["costUSD"], 9),
            })
        output = {
            "start": start_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": args.mode,
            "project": args.project,
            "matchedEntries": matched_entries,
            "totalCostUSD": round(total_cost, 9),
            "firstMatchedEntry": (
                first_match.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                if first_match else None
            ),
            "lastMatchedEntry": (
                last_match.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                if last_match else None
            ),
            "modelBreakdowns": breakdowns,
        }
        print(json.dumps(output, indent=2))
        return 0

    if args.breakdown:
        headers = ["Model", "Entries", "Input", "Output", "Cache Create", "Cache Read", "Total Tokens", "Cost (USD)"]
        rows: list[list[str]] = []
        for model in sorted(model_buckets, key=lambda m: -model_buckets[m]["costUSD"]):
            b = model_buckets[model]
            total_tokens = (
                b["inputTokens"] + b["outputTokens"]
                + b["cacheCreationTokens"] + b["cacheReadTokens"]
            )
            rows.append([
                model,
                f"{b['entries']:,}",
                f"{b['inputTokens']:,}",
                f"{b['outputTokens']:,}",
                f"{b['cacheCreationTokens']:,}",
                f"{b['cacheReadTokens']:,}",
                f"{total_tokens:,}",
                f"${b['costUSD']:.9f}",
            ])
        # Total row
        tot_entries = matched_entries
        tot_inp = sum(b["inputTokens"] for b in model_buckets.values())
        tot_out = sum(b["outputTokens"] for b in model_buckets.values())
        tot_cc = sum(b["cacheCreationTokens"] for b in model_buckets.values())
        tot_cr = sum(b["cacheReadTokens"] for b in model_buckets.values())
        tot_tokens = tot_inp + tot_out + tot_cc + tot_cr
        rows.append([
            "Total",
            f"{tot_entries:,}",
            f"{tot_inp:,}",
            f"{tot_out:,}",
            f"{tot_cc:,}",
            f"{tot_cr:,}",
            f"{tot_tokens:,}",
            f"${total_cost:.9f}",
        ])

        aligns = ["left"] + ["right"] * (len(headers) - 1)
        print(c._boxed_table(headers, rows, aligns, compact=args.compact))
        return 0

    # Default: print cost
    print(f"${total_cost:.9f}")
    return 0
