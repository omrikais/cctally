# bin/_cctally_codex.py
"""Codex (OpenAI) parity command family.

Holds the four codex commands — `cmd_codex_daily`, `cmd_codex_monthly`,
`cmd_codex_weekly`, `cmd_codex_session` — their speed/tz resolvers
(`_detect_codex_fast_service_tier`, `_resolve_codex_speed`,
`_resolve_codex_tz_name`) and the cost-stats/debug cluster
(`_CodexCostSample`, `_CodexCostStats`, `_compute_codex_cost_stats`,
`_render_codex_cost_report`, `_emit_codex_debug_samples_if_set`).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). This module
references the bin/cctally RE-EXPORTED names of every library kernel it
needs (`build_codex_daily_view`, `_calculate_codex_entry_cost`,
`_render_codex_session_table`, …) — NOT the `_lib_*` module objects — so
NO qualified `_lib_*` import is required; every such name is reached via
the call-time `_cctally()` accessor so test monkeypatches through
`cctally`'s namespace are preserved (spec §3.1). The codex path-resolvers
`_codex_home_roots`/`_codex_session_roots` STAY in bin/cctally (shared
with cache/doctor/aggregators) and are reached via `c.`.

THE SHARED DEBUG GUARD: `_DEBUG_REPORT_EMITTED` STAYS in bin/cctally
(module-global); `_emit_codex_debug_samples_if_set` reaches it via
`c._DEBUG_REPORT_EMITTED` for BOTH read and write — there is NO `global`
declaration here (spec §3.3).

bin/cctally re-exports EVERY moved symbol (eager): the parser resolves
`c.cmd_codex_*`; tests reach `mod._compute_codex_cost_stats` /
`mod._render_codex_cost_report` / `cc._resolve_codex_speed` /
`cc._detect_codex_fast_service_tier` off the `cctally` namespace.

Spec: docs/superpowers/specs/2026-05-31-extract-codex-reporting-cmd-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import timezone

from _cctally_core import WEEKDAY_MAP, _command_as_of, eprint, get_week_start_name


UTC = timezone.utc


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


# === moved verbatim from bin/cctally (Regions A–C) ===


@dataclass
class _CodexCostSample:
    file: str
    timestamp: str
    model: str
    calculated_cost: float
    usage: dict
    is_fallback: bool


@dataclass
class _CodexCostStats:
    command_label: str | None = None
    total_entries: int = 0
    total_cost: float = 0.0
    model_counts: dict = field(default_factory=dict)
    fallback_models: set = field(default_factory=set)
    samples: list = field(default_factory=list)


def _compute_codex_cost_stats(entries, speed: str = "standard"):
    """Walk ``entries: Iterable[CodexEntry]`` and compute the totals +
    per-entry computed-cost samples that ``_render_codex_cost_report``
    consumes (issue #92).

    Unlike the Claude ``_compute_pricing_mismatch_stats`` there is no
    recorded cost to diff against, so every entry contributes a sample.
    Samples are collected for all entries and sorted descending by
    computed cost; the renderer slices to ``--debug-samples``. (Memory is
    O(entries); acceptable for typical codex histories and symmetric with
    the Claude helper, which retains its full discrepancy list.)

    Cost + fallback resolution mirror the live aggregation path:
    ``_calculate_codex_entry_cost`` (LiteLLM token semantics) and
    ``_resolve_codex_pricing`` (unknown model → ``gpt-5`` fallback).
    """
    c = _cctally()
    stats = _CodexCostStats()
    for entry in entries:
        stats.total_entries += 1
        stats.model_counts[entry.model] = (
            stats.model_counts.get(entry.model, 0) + 1
        )
        _, is_fallback = c._resolve_codex_pricing(entry.model)
        if is_fallback:
            stats.fallback_models.add(entry.model)
        cost = c._calculate_codex_entry_cost(
            entry.model,
            entry.input_tokens,
            entry.cached_input_tokens,
            entry.output_tokens,
            entry.reasoning_output_tokens,
            speed=speed,
        )
        stats.total_cost += cost
        stats.samples.append(_CodexCostSample(
            file=os.path.basename(entry.source_path),
            timestamp=entry.timestamp.isoformat(),
            model=entry.model,
            calculated_cost=cost,
            usage={
                "input_tokens": entry.input_tokens,
                "cached_input_tokens": entry.cached_input_tokens,
                "output_tokens": entry.output_tokens,
                "reasoning_output_tokens": entry.reasoning_output_tokens,
                "total_tokens": entry.total_tokens,
            },
            is_fallback=is_fallback,
        ))
    # Stable sort: equal-cost samples keep iteration order (mirrors the
    # Claude helper's iteration-order discrepancy list).
    stats.samples.sort(key=lambda s: -s.calculated_cost)
    return stats


def _render_codex_cost_report(stats, sample_limit):
    """Return the codex --debug report as a list of stderr lines (issue #92).

    Structurally parallel to ``_render_pricing_mismatch_report`` but with
    no match/mismatch framing (codex has no recorded cost):

      - Early-return ``"No Codex usage data found to analyze."`` when
        ``total_entries == 0``.
      - Totals header: entries processed, models seen (count desc, ties
        by name asc; fallback models tagged ``(N, fallback→gpt-5)``),
        total computed cost.
      - ``Command: cctally <label>`` self-identifier when set (parity
        with the Claude report's one non-upstream line).
      - Sample block omitted when ``sample_limit == 0`` or no samples;
        header prints the requested ``sample_limit`` (upstream parity).
        Each sample carries ``Recorded cost: (none)`` and a
        ``(fallback→gpt-5)`` model-line marker when applicable.
    """
    c = _cctally()
    out = []
    if stats.total_entries == 0:
        out.append("No Codex usage data found to analyze.")
        return out

    fallback = c.CODEX_LEGACY_FALLBACK_MODEL
    parts = []
    for model, count in sorted(
        stats.model_counts.items(), key=lambda kv: (-kv[1], kv[0]),
    ):
        if model in stats.fallback_models:
            parts.append(f"{model} ({count:,}, fallback→{fallback})")
        else:
            parts.append(f"{model} ({count:,})")

    out.append("")
    out.append("=== Codex Pricing Debug Report ===")
    if stats.command_label:
        out.append(f"Command: cctally {stats.command_label}")
    out.append(f"Total entries processed: {stats.total_entries:,}")
    out.append(f"Models seen: {', '.join(parts)}")
    out.append(f"Total computed cost: ${stats.total_cost:.6f}")

    if stats.samples and sample_limit > 0:
        out.append("")
        out.append(f"=== Sample Top Entries (first {sample_limit}) ===")
        for s in stats.samples[:sample_limit]:
            model_line = (
                f"{s.model} (fallback→{fallback})"
                if s.is_fallback else s.model
            )
            out.append(f"File: {s.file}")
            out.append(f"Timestamp: {s.timestamp}")
            out.append(f"Model: {model_line}")
            out.append("Recorded cost: (none)")
            out.append(f"Calculated cost: ${s.calculated_cost:.6f}")
            out.append(f"Tokens: {json.dumps(s.usage)}")
            out.append("---")
    return out


def _emit_codex_debug_samples_if_set(
    args,
    entries,
    *,
    command_label: str,
    speed: str = "standard",
) -> None:
    """Emit the codex --debug report once per process when ``args.debug``
    is True (issue #92).

    ``entries`` is an eager ``list[CodexEntry]`` — each ``cmd_codex_*`` body
    already loads them via ``get_codex_entries`` before this call, so unlike
    the Claude helper there is no deferred-loader variant. Shares the
    process-wide ``_DEBUG_REPORT_EMITTED`` guard with
    ``_emit_debug_samples_if_set`` so a single CLI invocation emits one
    report regardless of family.
    """
    c = _cctally()
    if c._DEBUG_REPORT_EMITTED:
        return
    if not getattr(args, "debug", False):
        return
    sample_limit = int(getattr(args, "debug_samples", 5))
    stats = _compute_codex_cost_stats(entries, speed=speed)
    stats.command_label = command_label
    for line in _render_codex_cost_report(stats, sample_limit):
        eprint(line)
    c._DEBUG_REPORT_EMITTED = True


def _resolve_codex_tz_name(args: argparse.Namespace,
                           config: "dict | None") -> "str | None":
    """Resolve the IANA tz NAME (or None for host-local) used by Codex
    aggregators (`codex-{daily,monthly,weekly,session}`).

    Precedence (F2 fix):
      1. Explicit `--tz <anything>` flag → use it (None on canonical "local").
      2. Explicit `display.tz` set in config → use it (None on "local").
      3. Else fall back to upstream's `--timezone` (drop-in parity).
      4. Else None (host local).

    Steps 1+2 funnel through `resolve_display_tz`; step 3+4 are the
    pre-existing fallback path. The bug `resolve_display_tz` could not
    fix on its own: it returns None for both "explicit local" AND
    "implicit local fallback when no config exists", which collapsed the
    two semantically distinct cases. We disambiguate by inspecting
    `args.tz` and `config["display"]["tz"]` directly.
    """
    c = _cctally()
    flag_set = (
        getattr(args, "tz", None) is not None
        and str(getattr(args, "tz")).strip() != ""
    )
    if flag_set or c._config_has_explicit_display_tz(config):
        tz_obj = c.resolve_display_tz(args, config)
        return tz_obj.key if tz_obj is not None else None
    # No explicit display tz pin → defer to upstream's --timezone, then
    # host-local as the final default.
    return getattr(args, "timezone", None)


def _detect_codex_fast_service_tier() -> bool:
    """True iff any $CODEX_HOME root's config.toml requests fast/priority tier.

    Reads <root>/config.toml for EVERY entry in _codex_home_roots() (comma-
    separated $CODEX_HOME, else ~/.codex) — including direct-JSONL entries,
    which usually have no config.toml (read → absent → skipped) but DO count
    if one is present. Returns on the first root that requests it (any-root
    semantics, matching upstream ccusage). Tolerates absent/unreadable config
    (→ that root contributes nothing).
    """
    c = _cctally()
    for root in c._codex_home_roots():
        cfg = root / "config.toml"
        try:
            content = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if c._codex_config_requests_fast_service_tier(content):
            return True
    return False


def _resolve_codex_speed(requested: str) -> str:
    """Resolve a ``--speed`` value to an effective tier.

    ``auto`` → ``fast`` iff any ``$CODEX_HOME`` root's ``config.toml``
    requests it, else ``standard``. ``fast``/``standard`` pass through
    unchanged.
    """
    if requested == "auto":
        return "fast" if _detect_codex_fast_service_tier() else "standard"
    return requested


def _build_codex_share_snapshot(command: str, view, rows):
    """Build the four established Codex report artifacts without path fields."""
    c = _cctally()
    lib = c._share_load_lib()
    end = getattr(view, "period_end", None) or _command_as_of()
    start = getattr(view, "period_start", None) or end
    if end < start:
        start = end
    display_tz = getattr(view, "display_tz_label", "UTC") or "UTC"
    period_label = f"{start.date().isoformat()} → {end.date().isoformat()} ({display_tz})"
    titles = {
        "codex-daily": "Codex Token Usage — Daily",
        "codex-monthly": "Codex Token Usage — Monthly",
        "codex-weekly": "Codex Token Usage — Weekly",
        "codex-session": "Codex Token Usage — Sessions",
    }
    if command not in titles:
        raise ValueError(f"unknown Codex share command: {command}")
    if command == "codex-session":
        columns = (
            lib.ColumnSpec(key="session", label="Session"),
            lib.ColumnSpec(key="last", label="Last Activity"),
            lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
            lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        )
        table_rows = tuple(
            lib.Row(cells={
                "session": lib.TextCell(f"Session {index + 1}"),
                "last": lib.TextCell(getattr(row, "last_activity").astimezone(UTC).date().isoformat()),
                "tokens": lib.TextCell(f"{int(getattr(row, 'total_tokens', 0)):,}"),
                "cost": lib.MoneyCell(float(getattr(row, "cost_usd", 0.0))),
            })
            for index, row in enumerate(rows)
        )
    else:
        first_label = {
            "codex-daily": "Date",
            "codex-monthly": "Month",
            "codex-weekly": "Week",
        }[command]
        columns = (
            lib.ColumnSpec(key="bucket", label=first_label),
            lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
            lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        )
        table_rows = tuple(
            lib.Row(cells={
                "bucket": lib.TextCell(str(getattr(row, "bucket", "—"))),
                "tokens": lib.TextCell(f"{int(getattr(row, 'total_tokens', 0)):,}"),
                "cost": lib.MoneyCell(float(getattr(row, "cost_usd", 0.0))),
            })
            for row in rows
        )
    return lib.ShareSnapshot(
        cmd=command,
        title=titles[command],
        subtitle=period_label,
        period=lib.PeriodSpec(start=start, end=end, display_tz=display_tz, label=period_label),
        columns=columns,
        rows=table_rows,
        chart=None,
        totals=(
            lib.Totalled(label="Total", value=f"${float(getattr(view, 'total_cost_usd', 0.0)):,.2f}"),
        ),
        notes=(),
        generated_at=end,
        version=c._share_resolve_version(),
        source="codex",
        source_label="Codex",
        availability="empty" if not rows else "ok",
    )


def cmd_codex_daily(args: argparse.Namespace) -> int:
    """Show Codex usage report grouped by date (display tz, --tz, or --timezone)."""
    c = _cctally()
    c._share_validate_args(args)
    config = c.load_config(getattr(args, "config", None))
    tz_obj = c.resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    # Codex aggregators take a tz_name string. F2 fix: precedence is
    # `--tz` flag > config.display.tz > `--timezone` > host-local. Without
    # this, an explicit "--tz local" silently falls through to --timezone
    # (because resolve_display_tz returns None for canonical "local").
    tz_name = _resolve_codex_tz_name(args, config)
    force_compact = bool(getattr(args, "compact", False))
    range = c._parse_cli_date_range(
        args, tz_name=tz_name, now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    entries = c.get_codex_entries(range_start, range_end)
    speed = _resolve_codex_speed(args.speed)
    _emit_codex_debug_samples_if_set(args, entries, command_label="codex-daily", speed=speed)
    # Route through ``build_codex_daily_view`` (issue #58). The View
    # wraps ``_aggregate_codex_daily`` without changing it — preserves
    # LiteLLM token semantics, intentional dedup vs upstream, and
    # ``CODEX_LEGACY_FALLBACK_MODEL`` warning end-to-end.
    view = c.build_codex_daily_view(
        entries, now_utc=_command_as_of(), tz_name=tz_name, speed=speed,
    )
    days = list(view.rows)                  # asc — matches aggregator default
    if args.order == "desc":
        days = list(reversed(days))

    if getattr(args, "format", None):
        c._share_render_and_emit(
            _build_codex_share_snapshot("codex-daily", view, days), args,
        )
        return 0

    if not days:
        # Match upstream's no-data sentinel (see _emit_codex_no_data docstring).
        c._emit_codex_no_data(args, "daily")
        return 0

    if args.json:
        # Upstream daily --json uses "Dec 25, 2025" style for the date key.
        print(c._codex_bucket_to_json(
            days, list_key="daily", date_key="date",
            display_fn=c._codex_daily_bucket_display,
        ))
        return 0

    # Wide-mode table Date cell: two-line "Dec 25,\n2025"
    def daily_table_display(bucket: str) -> str:
        y, m, d = bucket.split("-")
        return f"{c._CODEX_MONTHS[int(m) - 1]} {int(d):02d},\n{y}"

    tz_label = view.display_tz_label
    title = f"Codex Token Usage Report - Daily (Timezone: {tz_label})"
    print(c._render_codex_bucket_table(
        days,
        first_col_name="Date",
        title=title,
        compact_split_fn=c._daily_compact_split,  # reuse existing helper (YYYY-MM-DD split)
        bucket_display_fn=daily_table_display,
        breakdown=args.breakdown,
        force_compact=force_compact,
    ))
    return 0


def cmd_codex_monthly(args: argparse.Namespace) -> int:
    """Show Codex usage report grouped by calendar month (display tz, --tz, or --timezone)."""
    c = _cctally()
    c._share_validate_args(args)
    config = c.load_config(getattr(args, "config", None))
    tz_obj = c.resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    # F2 fix: see cmd_codex_daily.
    tz_name = _resolve_codex_tz_name(args, config)
    force_compact = bool(getattr(args, "compact", False))
    range = c._parse_cli_date_range(
        args, tz_name=tz_name, now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    entries = c.get_codex_entries(range_start, range_end)
    speed = _resolve_codex_speed(args.speed)
    _emit_codex_debug_samples_if_set(args, entries, command_label="codex-monthly", speed=speed)
    # Route through ``build_codex_monthly_view`` (issue #58).
    view = c.build_codex_monthly_view(
        entries, now_utc=_command_as_of(), tz_name=tz_name, speed=speed,
    )
    months = list(view.rows)
    if args.order == "desc":
        months = list(reversed(months))

    if getattr(args, "format", None):
        c._share_render_and_emit(
            _build_codex_share_snapshot("codex-monthly", view, months), args,
        )
        return 0

    if not months:
        # Match upstream's no-data sentinel (see _emit_codex_no_data docstring).
        c._emit_codex_no_data(args, "monthly")
        return 0

    if args.json:
        # Upstream monthly --json uses "Dec 2025" style for the month key.
        print(c._codex_bucket_to_json(
            months, list_key="monthly", date_key="month",
            display_fn=c._codex_monthly_bucket_display,
        ))
        return 0

    # Wide-mode table Month cell: two-line "Dec\n2025"
    def monthly_table_display(bucket: str) -> str:
        y, m = bucket.split("-")
        return f"{c._CODEX_MONTHS[int(m) - 1]}\n{y}"

    tz_label = view.display_tz_label
    title = f"Codex Token Usage Report - Monthly (Timezone: {tz_label})"
    print(c._render_codex_bucket_table(
        months,
        first_col_name="Month",
        title=title,
        compact_split_fn=c._monthly_compact_split,  # reuse existing Claude helper (YYYY-MM split)
        bucket_display_fn=monthly_table_display,
        breakdown=args.breakdown,
        force_compact=force_compact,
    ))
    return 0


def cmd_codex_weekly(args: argparse.Namespace) -> int:
    """Show Codex usage grouped by week (display tz, --tz, or --timezone)."""
    c = _cctally()
    now_utc = _command_as_of()
    c._share_validate_args(args)
    config = c.load_config(getattr(args, "config", None))
    tz_obj = c.resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    # F2 fix: see cmd_codex_daily.
    tz_name = _resolve_codex_tz_name(args, config)
    force_compact = bool(getattr(args, "compact", False))
    range = c._parse_cli_date_range(args, tz_name=tz_name, now_utc=now_utc)
    if isinstance(range, int):
        return range
    range_start, range_end = range

    # Resolve week-start from config (Monday default; reuse already-loaded config).
    week_start_name = get_week_start_name(config)
    week_start_idx = WEEKDAY_MAP[week_start_name]

    entries = c.get_codex_entries(range_start, range_end)
    speed = _resolve_codex_speed(args.speed)
    _emit_codex_debug_samples_if_set(args, entries, command_label="codex-weekly", speed=speed)
    # Route through ``build_codex_weekly_view`` (issue #58).
    view = c.build_codex_weekly_view(
        entries, now_utc=now_utc, tz_name=tz_name,
        week_start_idx=week_start_idx, speed=speed,
    )
    weeks = list(view.rows)
    if args.order == "desc":
        weeks = list(reversed(weeks))

    if getattr(args, "format", None):
        c._share_render_and_emit(
            _build_codex_share_snapshot("codex-weekly", view, weeks), args,
        )
        return 0

    if not weeks:
        # Match upstream's no-data sentinel (same string daily/monthly use).
        c._emit_codex_no_data(args, "weekly")
        return 0

    if args.json:
        # No upstream codex weekly JSON exists — use MMM DD, YYYY style matching codex-daily.
        def weekly_bucket_display(bucket: str) -> str:
            y, m, d = bucket.split("-")
            return f"{c._CODEX_MONTHS[int(m) - 1]} {int(d):02d}, {y}"
        print(c._codex_bucket_to_json(
            weeks, list_key="weekly", date_key="week",
            display_fn=weekly_bucket_display,
        ))
        return 0

    # Wide-mode table Week cell: two-line "Apr 13,\n2026"
    def weekly_table_display(bucket: str) -> str:
        y, m, d = bucket.split("-")
        return f"{c._CODEX_MONTHS[int(m) - 1]} {int(d):02d},\n{y}"

    tz_label = view.display_tz_label
    title = f"Codex Token Usage Report - Weekly (Timezone: {tz_label})"
    print(c._render_codex_bucket_table(
        weeks,
        first_col_name="Week",
        title=title,
        compact_split_fn=c._daily_compact_split,  # two-line split of "YYYY-MM-DD" — same shape as daily
        bucket_display_fn=weekly_table_display,
        breakdown=args.breakdown,
        force_compact=force_compact,
    ))
    return 0


def cmd_codex_session(args: argparse.Namespace) -> int:
    """Show Codex usage report grouped by session (sorted by last activity)."""
    c = _cctally()
    c._share_validate_args(args)
    config = c.load_config(getattr(args, "config", None))
    tz_obj = c.resolve_display_tz(args, config)
    args._resolved_tz = tz_obj
    # F2 fix: see cmd_codex_daily.
    tz_name = _resolve_codex_tz_name(args, config)
    force_compact = bool(getattr(args, "compact", False))
    range = c._parse_cli_date_range(
        args, tz_name=tz_name, now_utc=_command_as_of(),
    )
    if isinstance(range, int):
        return range
    range_start, range_end = range

    entries = c.get_codex_entries(range_start, range_end)
    speed = _resolve_codex_speed(args.speed)
    _emit_codex_debug_samples_if_set(args, entries, command_label="codex-session", speed=speed)
    # Route through ``build_codex_session_view`` (issue #58). View rows
    # come descending by last_activity (aggregator default + upstream
    # parity); --order asc reverses.
    view = c.build_codex_session_view(
        entries, now_utc=_command_as_of(), tz_name=tz_name, speed=speed,
    )
    sessions = list(view.rows)
    if args.order == "asc":
        sessions = list(reversed(sessions))

    if getattr(args, "format", None):
        c._share_render_and_emit(
            _build_codex_share_snapshot("codex-session", view, sessions), args,
        )
        return 0

    if not sessions:
        # Match upstream's no-data sentinel (plural "sessions" matches upstream
        # — confirmed in @ccusage/codex@18.0.8 dist/index.js around line 7962).
        c._emit_codex_no_data(args, "sessions")
        return 0

    if args.json:
        print(c._codex_sessions_to_json(sessions))
        return 0

    tz_label = view.display_tz_label
    # Upstream uses "Sessions" (plural) in the session banner title.
    title = f"Codex Token Usage Report - Sessions (Timezone: {tz_label})"
    print(c._render_codex_session_table(
        sessions, title=title,
        force_compact=force_compact, tz_name=tz_name,
    ))
    return 0
