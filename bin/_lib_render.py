"""Render kernel for cctally reporting subcommands.

Pure-fn layer (no I/O at import time): holds every ANSI-table renderer
and JSON shaper used by the ``daily`` / ``monthly`` / ``weekly`` /
``session`` / ``blocks`` / ``project`` / ``codex-{daily,monthly,session}`` /
``five-hour-blocks`` subcommands. Two contiguous source regions
collapse into one sibling here:

  * Region A (was bin/cctally L2175-L4661, ~2,486 LOC): block /
    bucket / weekly / codex-bucket / codex-session / claude-session /
    project renderers and their JSON-shape siblings, plus the
    ``_CODEX_MONTHS`` table and the project-row label disambiguator.
  * Region B (was bin/cctally L14350-L14507, ~158 LOC): the
    5h-blocks-table render pair (``_five_hour_blocks_to_json`` +
    ``_render_five_hour_blocks_table``).

Sibling dependencies (loaded at module-load time via ``_load_lib``):

  * ``_lib_blocks`` — ``Block`` (typing for ``_render_blocks_table``)
    and ``BLOCK_DURATION`` (block-duration fallback).
  * ``_lib_aggregators`` — the four bucket / session dataclasses
    consumed by the bucket / weekly / codex / claude-session renderers
    (``BucketUsage``, ``CodexBucketUsage``, ``CodexSessionUsage``,
    ``ClaudeSessionUsage``).
  * ``_lib_subscription_weeks`` — ``SubWeek`` (typing + runtime field
    access in ``_weekly_to_json`` / ``_render_weekly_table``).
  * ``_lib_pricing`` — ``_short_model_name`` (model-name shortener
    used across every breakdown-aware table).
  * ``_lib_display_tz`` — ``_resolve_tz`` (IANA tz resolution for the
    Codex session-table date columns).

``bin/cctally`` back-references via module-level callable shims
(spec §5.5; same precedent as ``bin/_cctally_record.py``'s 34 shims):

  * ``_supports_color_stdout`` / ``_supports_unicode_stdout`` /
    ``_style_ansi`` — ANSI capability + style primitives.
  * ``_fmt_num`` / ``_truncate_num`` — numeric formatting helpers
    used by every render path.
  * ``_boxed_table`` — generic boxed-table renderer reused by
    ``_render_five_hour_blocks_table``.
  * ``_format_block_start`` — 5h-block Block-Start cell formatter
    (consumed only by the 5h-blocks renderer).

Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not
bind time), so monkeypatches on cctally's namespace propagate into the
moved code unchanged.

``bin/cctally`` eager-re-exports every public symbol below so the ~25
internal call sites + SourceFileLoader-based tests resolve unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
from typing import Any, Callable


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_blocks = _load_lib("_lib_blocks")
Block = _lib_blocks.Block
BLOCK_DURATION = _lib_blocks.BLOCK_DURATION

_lib_aggregators = _load_lib("_lib_aggregators")
BucketUsage = _lib_aggregators.BucketUsage
CodexBucketUsage = _lib_aggregators.CodexBucketUsage
CodexSessionUsage = _lib_aggregators.CodexSessionUsage
ClaudeSessionUsage = _lib_aggregators.ClaudeSessionUsage

_lib_subscription_weeks = _load_lib("_lib_subscription_weeks")
SubWeek = _lib_subscription_weeks.SubWeek

_lib_pricing = _load_lib("_lib_pricing")
_short_model_name = _lib_pricing._short_model_name

_lib_display_tz = _load_lib("_lib_display_tz")
_resolve_tz = _lib_display_tz._resolve_tz


# Module-level back-ref shims. Each shim resolves
# ``sys.modules['cctally'].X`` at CALL TIME (not bind time), so
# monkeypatches on cctally's namespace propagate into the moved code
# unchanged. Mirrors the precedent established in
# ``bin/_cctally_record.py`` / ``bin/_cctally_cache.py``.
def _supports_color_stdout(*args, **kwargs):
    return sys.modules["cctally"]._supports_color_stdout(*args, **kwargs)


def _supports_unicode_stdout(*args, **kwargs):
    return sys.modules["cctally"]._supports_unicode_stdout(*args, **kwargs)


def _style_ansi(*args, **kwargs):
    return sys.modules["cctally"]._style_ansi(*args, **kwargs)


def _fmt_num(*args, **kwargs):
    return sys.modules["cctally"]._fmt_num(*args, **kwargs)


def _truncate_num(*args, **kwargs):
    return sys.modules["cctally"]._truncate_num(*args, **kwargs)


def _boxed_table(*args, **kwargs):
    return sys.modules["cctally"]._boxed_table(*args, **kwargs)


def _format_block_start(*args, **kwargs):
    return sys.modules["cctally"]._format_block_start(*args, **kwargs)


# Optional dependency: zoneinfo.ZoneInfo is referenced only as a string
# annotation in moved code; no runtime import needed.

def _render_blocks_table(
    blocks: list[Block],
    breakdown: bool = False,
    *,
    now: dt.datetime | None = None,
    tz: "ZoneInfo | None" = None,
) -> str:
    """Render blocks as a ccusage-style ANSI table with box-drawing borders.

    Uses a two-pass approach matching upstream ccusage's ResponsiveTable:
      Pass 1 - Build all cell content as plain strings (no ANSI, no padding).
      Pass 2 - Compute column widths from content, then render with borders,
               padding, and ANSI colors.

    ``now`` pins the current instant for ACTIVE-row elapsed/remaining
    calculations (typically via ``_command_as_of()``). Defaults to wall-
    clock UTC so production behavior is unchanged; fixture-based tests
    pass a pinned value so goldens stay byte-stable.

    ``tz`` is the resolved display zone (``None`` means host local).
    Block-start cells are rendered in this zone.
    """
    if not blocks:
        return "No session blocks found in the specified date range."
    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    # ── ANSI helpers ────────────────────────────────────────────────────

    def _dim(s: str) -> str:
        return _style_ansi(s, "90", color)

    def _cyan(s: str) -> str:
        return _style_ansi(s, "36", color)

    def _bold(s: str) -> str:
        return _style_ansi(s, "1", color)

    def _green(s: str) -> str:
        return _style_ansi(s, "32", color)

    def _blue(s: str) -> str:
        return _style_ansi(s, "34", color)

    def _yellow(s: str) -> str:
        return _style_ansi(s, "33", color)

    # ── time formatting ─────────────────────────────────────────────────

    def _fmt_time_local(ts: dt.datetime) -> str:
        local = ts.astimezone(tz)
        hour_12 = local.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        ampm = "a.m." if local.hour < 12 else "p.m."
        return (
            f"{local.year}-{local.month:02d}-{local.day:02d}, "
            f"{hour_12}:{local.minute:02d}:{local.second:02d} {ampm}"
        )

    def _fmt_duration_hm(total_seconds: float) -> str:
        total_minutes = int(total_seconds / 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h}h {m:02d}m"

    def _fmt_gap_duration(total_seconds: float) -> str:
        hours = round(total_seconds / 3600)
        if hours < 1:
            hours = 1
        return f"{hours}h gap"

    # ── determine if % column is needed ─────────────────────────────────
    max_completed_tokens = 0
    for b in blocks:
        if not b.is_gap and not b.is_active and b.total_tokens > 0:
            if b.total_tokens > max_completed_tokens:
                max_completed_tokens = b.total_tokens
    token_limit = 0
    active_block: Block | None = None
    for b in blocks:
        if b.is_active and not b.is_gap:
            active_block = b
    show_pct = max_completed_tokens > 0
    if show_pct:
        token_limit = max_completed_tokens

    # ── column layout ───────────────────────────────────────────────────
    headers = ["Block Start", "Duration/\u2026", "Models", "Tokens"]
    aligns = ["left", "left", "left", "right"]
    if show_pct:
        headers.append("%")
        aligns.append("right")
    headers.append("Cost")
    aligns.append("right")
    num_cols = len(headers)

    def _empty_cells() -> list[str]:
        return [""] * num_cols

    # ── Pass 1: build all row data as plain strings ─────────────────────
    # Each "row" is a list of display lines, each line is a list[str] of
    # cells.  We also track per-row metadata for colorizing in pass 2.

    ROW_NORMAL = "normal"
    ROW_GAP = "gap"
    ROW_ACTIVE = "active"
    ROW_REMAINING = "remaining"
    ROW_PROJECTED = "projected"

    # (lines, row_type)
    all_rows: list[tuple[list[list[str]], str]] = []

    for block in blocks:
        if block.is_gap:
            gap_seconds = (block.end_time - block.start_time).total_seconds()
            gap_dur = _fmt_gap_duration(gap_seconds)
            # Build gap text as single string; wrapping happens later if needed
            gap_text = (
                f"{_fmt_time_local(block.start_time)} - "
                f"{_fmt_time_local(block.end_time)} ({gap_dur})"
            )

            cells1 = _empty_cells()
            cells1[0] = gap_text
            cells1[1] = "(inactive)"
            cells1[2] = "-"
            cells1[3] = "-"
            if show_pct:
                cells1[4] = "-"
                cells1[-1] = "-"
            else:
                cells1[-1] = "-"

            all_rows.append(([cells1], ROW_GAP))

        else:
            if block.is_active:
                elapsed_secs = (now - block.start_time).total_seconds()
                remaining_secs = max(
                    (block.end_time - now).total_seconds(), 0
                )
                dur_str = (
                    f"{_fmt_duration_hm(elapsed_secs)} "
                    f"elapsed, {_fmt_duration_hm(remaining_secs)} remaining)"
                )
                duration_col = "ACTIVE"
                row_type = ROW_ACTIVE
            else:
                if block.actual_end_time:
                    elapsed_secs = (
                        block.actual_end_time - block.start_time
                    ).total_seconds()
                else:
                    elapsed_secs = BLOCK_DURATION.total_seconds()
                dur_str = f"{_fmt_duration_hm(elapsed_secs)})"
                duration_col = ""
                row_type = ROW_NORMAL

            time_str = _fmt_time_local(block.start_time)
            if not block.is_gap and block.anchor == "heuristic":
                time_str = f"~{time_str}"
            start_text = f"{time_str} ({dur_str}"

            short_models = [
                f"- {_short_model_name(m)}" for m in block.models
            ]
            if not short_models:
                short_models = [""]

            pct_str = ""
            if show_pct and token_limit > 0:
                pct_val = (block.total_tokens / token_limit) * 100.0
                pct_str = f"{pct_val:.1f}%"

            tokens_str = _fmt_num(block.total_tokens)
            cost_str = f"${block.cost_usd:.2f}"

            # First line
            cells1 = _empty_cells()
            cells1[0] = start_text  # may overflow; wrapping handled later
            cells1[1] = duration_col
            cells1[2] = short_models[0]
            cells1[3] = tokens_str
            ci = 4
            if show_pct:
                cells1[ci] = pct_str
                ci += 1
            cells1[ci] = cost_str

            display_lines: list[list[str]] = [cells1]

            # Continuation lines for remaining models
            for mi in range(1, len(short_models)):
                cont = _empty_cells()
                cont[2] = short_models[mi]
                display_lines.append(cont)

            all_rows.append((display_lines, row_type))

    # Footer rows (REMAINING, PROJECTED)
    footer_rows: list[tuple[list[list[str]], str]] = []
    if show_pct and token_limit > 0:
        active_tokens = active_block.total_tokens if active_block else 0
        remaining_tokens = max(token_limit - active_tokens, 0)
        remaining_pct = (remaining_tokens / token_limit) * 100.0
        rem_label = f"(assuming {_fmt_num(token_limit)} token limit)"

        rem_cells = _empty_cells()
        rem_cells[0] = rem_label
        rem_cells[1] = "REMAINING"
        rem_cells[3] = _fmt_num(remaining_tokens)
        ci = 4
        if show_pct:
            rem_cells[ci] = f"{remaining_pct:.1f}%"
            ci += 1
        footer_rows.append(([rem_cells], ROW_REMAINING))

        if active_block and active_block.projection:
            proj = active_block.projection
            proj_tokens = proj.get("totalTokens", 0)
            proj_pct = (
                (proj_tokens / token_limit) * 100.0 if token_limit > 0 else 0
            )
            proj_cost = proj.get("totalCost", 0.0)

            proj_cells = _empty_cells()
            proj_cells[0] = "(assuming current burn rate)"
            proj_cells[1] = "PROJECTED"
            proj_cells[3] = _fmt_num(proj_tokens)
            ci = 4
            if show_pct:
                proj_cells[ci] = f"{proj_pct:.1f}%"
                ci += 1
            proj_cells[ci] = f"${proj_cost:.2f}"
            footer_rows.append(([proj_cells], ROW_PROJECTED))

    # ── Pass 2: compute column widths from content ──────────────────────

    # Measure max content width per column from headers + all cell data.
    content_widths = [len(h) for h in headers]

    def _measure_rows(
        rows: list[tuple[list[list[str]], str]],
        skip_col0: bool = False,
    ) -> None:
        for display_lines, _ in rows:
            for line_cells in display_lines:
                for i, cell in enumerate(line_cells):
                    if skip_col0 and i == 0:
                        continue
                    content_widths[i] = max(content_widths[i], len(cell))

    _measure_rows(all_rows)
    # Footer labels (col 0) are right-justified into whatever width col 0
    # gets — they should not inflate it.
    _measure_rows(footer_rows, skip_col0=True)

    # Add padding matching upstream ccusage's ResponsiveTable.
    col_widths: list[int] = []
    for i, cw in enumerate(content_widths):
        if aligns[i] == "right":
            col_widths.append(max(cw + 3, 11))
        elif i == 1:  # Duration column
            col_widths.append(max(cw + 2, 15))
        else:
            col_widths.append(max(cw + 2, 10))

    # Get terminal width (matches ccusage's ResponsiveTable.toString()).
    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    # Scale down only when table exceeds terminal width.
    # ccusage does NOT expand columns when the table fits — it uses the
    # padded content widths as-is.
    table_overhead = 3 * num_cols + 1
    available_width = term_width - table_overhead
    if sum(col_widths) + table_overhead > term_width:
        scale_factor = available_width / sum(col_widths)
        col_widths = [
            max(
                math.floor(w * scale_factor),
                10 if aligns[i] == "right"
                else 10 if i == 0
                else 12 if i == 1
                else 8,
            )
            for i, w in enumerate(col_widths)
        ]

    # ── box-drawing characters ──────────────────────────────────────────
    if unicode_ok:
        ch = {
            "tl": "\u250c", "tm": "\u252c", "tr": "\u2510",
            "ml": "\u251c", "mm": "\u253c", "mr": "\u2524",
            "bl": "\u2514", "bm": "\u2534", "br": "\u2518",
            "h": "\u2500", "v": "\u2502",
        }
    else:
        ch = {k: c for k, c in zip(
            ["tl", "tm", "tr", "ml", "mm", "mr", "bl", "bm", "br", "h", "v"],
            "+++++++++-|",
        )}

    def hline(left: str, mid: str, right: str) -> str:
        segs = [ch["h"] * (col_widths[i] + 2) for i in range(num_cols)]
        return _dim(left + mid.join(segs) + right)

    def padcell(text: str, width: int, align: str) -> str:
        """Pad cell content to width. Text may contain ANSI codes, so we
        compute visible length by stripping escape sequences."""
        vis_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
        pad_needed = width - vis_len
        if pad_needed <= 0:
            return text
        if align == "right":
            return " " * pad_needed + text
        return text + " " * pad_needed

    def make_row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell_text in enumerate(cells):
            padded = padcell(cell_text, col_widths[i], aligns[i])
            parts.append(f" {padded} ")
        v = _dim(ch["v"])
        return v + v.join(parts) + v

    # ── Wrap + colorize helpers ─────────────────────────────────────────

    def _wrap_col0(text: str, width: int) -> list[str]:
        """Wrap column-0 text to fit *width*, breaking at word boundaries."""
        if len(text) <= width:
            return [text]
        # Try to break at a word boundary.
        split_at = width
        space_idx = text.rfind(" ", 0, width + 1)
        if space_idx > width // 2:
            split_at = space_idx + 1
        part1 = text[:split_at].rstrip()
        part2 = text[split_at:].lstrip()
        lines_out = [part1]
        if part2:
            lines_out.extend(_wrap_col0(part2, width))
        return lines_out

    def _colorize_cell(text: str, col_idx: int, row_type: str) -> str:
        """Apply ANSI color to a cell based on row type and column."""
        if row_type == ROW_GAP:
            return _dim(text) if text else text
        if col_idx == 1:
            if row_type == ROW_ACTIVE:
                return _green(text) if text == "ACTIVE" else text
            if row_type == ROW_REMAINING:
                return _blue(text) if text == "REMAINING" else text
            if row_type == ROW_PROJECTED:
                return _yellow(text) if text == "PROJECTED" else text
        return text

    # ── Render output ───────────────────────────────────────────────────

    # Title banner
    title = "Claude Code Token Usage Report - Session Blocks"
    title_padded = f"  {title}  "
    tw = len(title_padded)
    dash = "\u2500" if unicode_ok else "-"
    vb = "\u2502" if unicode_ok else "|"
    if unicode_ok:
        banner_top = f" \u256d{dash * tw}\u256e"
        banner_bot = f" \u2570{dash * tw}\u256f"
    else:
        banner_top = f" +{'-' * tw}+"
        banner_bot = f" +{'-' * tw}+"

    lines: list[str] = []
    lines.append(banner_top)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(f" {vb}" + _bold(title_padded) + vb)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(banner_bot)
    lines.append("")

    # Header
    lines.append(hline(ch["tl"], ch["tm"], ch["tr"]))
    header_cells = [_cyan(h) for h in headers]
    lines.append(make_row(header_cells))
    lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    # Data rows
    col0_w = col_widths[0]

    def _render_block_row(
        display_lines: list[list[str]], row_type: str
    ) -> None:
        """Render one block's display lines, wrapping col 0 and truncating
        the Tokens column as needed."""
        for li, line_cells in enumerate(display_lines):
            # Wrap column 0 if it overflows.
            col0_text = line_cells[0]
            col0_parts = _wrap_col0(col0_text, col0_w) if col0_text else [""]

            for wi, c0_part in enumerate(col0_parts):
                cells = _empty_cells()
                cells[0] = _colorize_cell(c0_part, 0, row_type)
                if wi == 0:
                    # First wrap-line carries the real cell data.
                    for ci in range(1, num_cols):
                        raw = line_cells[ci]
                        # Truncate tokens column if needed.
                        if ci == 3 and raw and raw != "-":
                            raw = _truncate_num(raw, col_widths[ci])
                        cells[ci] = _colorize_cell(raw, ci, row_type)
                lines.append(make_row(cells))

    for idx, (display_lines, row_type) in enumerate(all_rows):
        _render_block_row(display_lines, row_type)
        if idx < len(all_rows) - 1:
            lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    # Footer rows
    for fi, (display_lines, row_type) in enumerate(footer_rows):
        lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))
        # Right-align the label in column 0 for footer rows.
        for line_cells in display_lines:
            line_cells[0] = line_cells[0].rjust(col0_w)
        _render_block_row(display_lines, row_type)

    # Bottom border
    lines.append(hline(ch["bl"], ch["bm"], ch["br"]))

    rendered = "\n".join(lines)
    has_heuristic = any(
        (not b.is_gap) and b.anchor == "heuristic" for b in blocks
    )
    if has_heuristic:
        legend = _dim(
            "~ = approximate start "
            "(no recorded Anthropic reset for this window)"
        )
        rendered = f"{rendered}\n{legend}"
    return rendered


def _bucket_to_json(
    buckets: list[BucketUsage],
    *,
    list_key: str,
    date_key: str,
) -> str:
    """Serialize bucket aggregates to JSON matching upstream ccusage's shape.

    `list_key`  is the top-level array name ("daily" or "monthly").
    `date_key`  is the per-item bucket field name ("date" or "month").

    Key order inside each item matches ccusage:
      date_key, inputTokens, outputTokens, cacheCreationTokens,
      cacheReadTokens, totalTokens, totalCost, modelsUsed, modelBreakdowns.
    Totals key order (note: totalCost BEFORE totalTokens, per ccusage).
    """
    bucket_list: list[dict[str, Any]] = []
    tot_input = 0
    tot_output = 0
    tot_cc = 0
    tot_cr = 0
    tot_cost = 0.0
    tot_tokens = 0
    for d in buckets:
        bucket_list.append({
            date_key: d.bucket,
            "inputTokens": d.input_tokens,
            "outputTokens": d.output_tokens,
            "cacheCreationTokens": d.cache_creation_tokens,
            "cacheReadTokens": d.cache_read_tokens,
            "totalTokens": d.total_tokens,
            "totalCost": d.cost_usd,
            "modelsUsed": list(d.models),
            "modelBreakdowns": list(d.model_breakdowns),
        })
        tot_input += d.input_tokens
        tot_output += d.output_tokens
        tot_cc += d.cache_creation_tokens
        tot_cr += d.cache_read_tokens
        tot_cost += d.cost_usd
        tot_tokens += d.total_tokens

    totals = {
        "inputTokens": tot_input,
        "outputTokens": tot_output,
        "cacheCreationTokens": tot_cc,
        "cacheReadTokens": tot_cr,
        "totalCost": tot_cost,
        "totalTokens": tot_tokens,
    }
    return json.dumps({list_key: bucket_list, "totals": totals}, indent=2)


def _weekly_to_json(
    buckets: list[BucketUsage],
    weeks: list[SubWeek],
    week_pct_overlay: list[tuple[float | None, float | None]],
) -> str:
    """Serialize weekly rollup to JSON.

    Shape:
      {
        "weekly": [
          {
            "week": "YYYY-MM-DD",          # API-derived week_start_date (stable contract / lookup key)
            "displayWeek": "YYYY-MM-DD",   # effective post-reset start; equals `week` for non-reset weeks
            "weekStartAt": "...ISO...",
            "weekEndAt": "...ISO...",
            "weekSource": "snapshot" | "extrapolated",
            "inputTokens": int, "cacheCreationTokens": int, "cacheReadTokens": int,
            "outputTokens": int, "totalTokens": int, "totalCost": float,
            "usedPct": float | null, "dollarsPerPercent": float | null,
            "modelsUsed": [...], "modelBreakdowns": [...]
          }, ...
        ],
        "totals": { inputTokens, cacheCreationTokens, cacheReadTokens,
                    outputTokens, totalTokens, totalCost }
      }
    """
    assert len(week_pct_overlay) == len(buckets), (
        f"week_pct_overlay length {len(week_pct_overlay)} does not match "
        f"buckets length {len(buckets)} — caller contract violated"
    )
    # Build dict lookup from week-start-date ISO → SubWeek for metadata.
    week_by_key = {w.start_date.isoformat(): w for w in weeks}

    weekly_list: list[dict[str, Any]] = []
    tot_input = tot_cache_c = tot_cache_r = tot_output = tot_total = 0
    tot_cost = 0.0
    for i, bucket in enumerate(buckets):
        w = week_by_key.get(bucket.bucket)
        if w is None:
            # Defensive: bucket key should always match a SubWeek (_aggregate_weekly
            # only emits keys derived from the provided weeks list). Raise loud
            # rather than silently emit partial data.
            raise ValueError(
                f"bucket key {bucket.bucket!r} has no matching SubWeek in `weeks`"
            )
        pct, dpc = week_pct_overlay[i]
        weekly_list.append({
            "week": bucket.bucket,
            "displayWeek": w.display_start_date.isoformat(),
            "weekStartAt": w.start_ts,
            "weekEndAt": w.end_ts,
            "weekSource": w.source,
            "inputTokens": bucket.input_tokens,
            "cacheCreationTokens": bucket.cache_creation_tokens,
            "cacheReadTokens": bucket.cache_read_tokens,
            "outputTokens": bucket.output_tokens,
            "totalTokens": bucket.total_tokens,
            "totalCost": bucket.cost_usd,
            "usedPct": pct,
            "dollarsPerPercent": dpc,
            "modelsUsed": bucket.models,
            "modelBreakdowns": bucket.model_breakdowns,
        })
        tot_input += bucket.input_tokens
        tot_cache_c += bucket.cache_creation_tokens
        tot_cache_r += bucket.cache_read_tokens
        tot_output += bucket.output_tokens
        tot_total += bucket.total_tokens
        tot_cost += bucket.cost_usd

    payload = {
        "weekly": weekly_list,
        "totals": {
            "inputTokens": tot_input,
            "cacheCreationTokens": tot_cache_c,
            "cacheReadTokens": tot_cache_r,
            "outputTokens": tot_output,
            "totalTokens": tot_total,
            "totalCost": tot_cost,
        },
    }
    return json.dumps(payload, indent=2)


def _daily_compact_split(bucket: str) -> str:
    """YYYY-MM-DD → "YYYY\\nMM-DD" for compact-mode Date column."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", bucket)
    return f"{m.group(1)}\n{m.group(2)}-{m.group(3)}" if m else bucket


def _monthly_compact_split(bucket: str) -> str:
    """YYYY-MM → "YYYY\\nMM" for compact-mode Month column.

    Deliberate deviation from ccusage: upstream renders a synthetic "-01"
    day component in compact mode because its formatter is daily-oriented.
    We omit it — same information, less visual noise.
    """
    m = re.match(r"^(\d{4})-(\d{2})$", bucket)
    return f"{m.group(1)}\n{m.group(2)}" if m else bucket

_CODEX_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _codex_daily_bucket_display(bucket: str) -> str:
    """YYYY-MM-DD → "Mon DD, YYYY" (e.g. "Dec 25, 2025"). Upstream shape."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", bucket)
    if not m:
        return bucket
    return f"{_CODEX_MONTHS[int(m.group(2)) - 1]} {int(m.group(3)):02d}, {m.group(1)}"


def _codex_monthly_bucket_display(bucket: str) -> str:
    """YYYY-MM → "Mon YYYY" (e.g. "Dec 2025"). Upstream shape."""
    m = re.match(r"^(\d{4})-(\d{2})$", bucket)
    if not m:
        return bucket
    return f"{_CODEX_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"


def _codex_last_activity_iso(ts: dt.datetime) -> str:
    """ISO-8601 UTC with milliseconds and Z suffix (e.g. "2025-12-25T10:03:52.375Z").

    Matches upstream ccusage-codex's session `lastActivity` format byte-exactly.
    """
    utc = ts.astimezone(dt.timezone.utc)
    # Python's .isoformat() defaults to microseconds (6 digits); upstream uses
    # milliseconds (3 digits). Truncate and append Z.
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond // 1000:03d}Z"


def _emit_codex_no_data(args: argparse.Namespace, list_key: str) -> None:
    """Print upstream's empty-result sentinel for codex-{daily,monthly,session}.

    Matches ccusage-codex byte-exactly:
      - JSON: ``{"<list_key>":[],"totals":null}`` (compact separators, no
        whitespace — upstream uses ``JSON.stringify(...)`` with no indent
        argument for the empty case, even though the happy-path uses indent=2).
      - Text: ``"No Codex usage data found."`` when no filters are in effect,
        or ``"No Codex usage data found for provided filters."`` when --since
        or --until is set (matching upstream's filter-aware messaging).
    """
    filter_applied = bool(getattr(args, "since", None) or getattr(args, "until", None))
    if getattr(args, "json", False):
        # Compact separators to match Node's `JSON.stringify(obj)` output exactly.
        print(json.dumps({list_key: [], "totals": None}, separators=(",", ":")))
    else:
        if filter_applied:
            print("No Codex usage data found for provided filters.")
        else:
            print("No Codex usage data found.")


def _codex_models_dict(
    model_breakdowns: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Convert our internal list-of-breakdowns into upstream's models dict.

    Input: list of {modelName, inputTokens, cachedInputTokens, outputTokens,
                    reasoningOutputTokens, totalTokens, cost, isFallback}.
    Output: {<modelName>: {inputTokens, cachedInputTokens, outputTokens,
                            reasoningOutputTokens, totalTokens, isFallback}}
    Insertion order: whatever the caller passed (aggregators sort by cost desc).
    Note: the per-model `cost` / `modelName` keys from the list are dropped
    — upstream's dict doesn't include them at the per-model level.
    """
    out: dict[str, dict[str, Any]] = {}
    for mb in model_breakdowns:
        out[mb["modelName"]] = {
            "inputTokens": mb["inputTokens"],
            "cachedInputTokens": mb["cachedInputTokens"],
            "outputTokens": mb["outputTokens"],
            "reasoningOutputTokens": mb["reasoningOutputTokens"],
            "totalTokens": mb["totalTokens"],
            "isFallback": mb["isFallback"],
        }
    return out


def _codex_bucket_to_json(
    buckets: list[CodexBucketUsage],
    *,
    list_key: str,          # "daily" or "monthly"
    date_key: str,          # "date" or "month"
    display_fn: Callable[[str], str],  # maps bucket key → human display
) -> str:
    """Serialize Codex bucket aggregates to JSON matching upstream exactly.

    Per-entry shape:
      {<date_key>, inputTokens, cachedInputTokens, outputTokens,
       reasoningOutputTokens, totalTokens, costUSD, models}
    Totals:
      {inputTokens, cachedInputTokens, outputTokens,
       reasoningOutputTokens, totalTokens, costUSD}
    """
    bucket_list: list[dict[str, Any]] = []
    tot_input = tot_cached = tot_output = tot_reasoning = tot_tokens = 0
    tot_cost = 0.0
    for b in buckets:
        bucket_total = b.input_tokens + b.output_tokens
        bucket_list.append({
            date_key: display_fn(b.bucket),
            "inputTokens": b.input_tokens,
            "cachedInputTokens": b.cached_input_tokens,
            "outputTokens": b.output_tokens,
            "reasoningOutputTokens": b.reasoning_output_tokens,
            "totalTokens": bucket_total,
            "costUSD": b.cost_usd,
            "models": _codex_models_dict(b.model_breakdowns),
        })
        tot_input += b.input_tokens
        tot_cached += b.cached_input_tokens
        tot_output += b.output_tokens
        tot_reasoning += b.reasoning_output_tokens
        tot_tokens += bucket_total
        tot_cost += b.cost_usd

    totals = {
        "inputTokens": tot_input,
        "cachedInputTokens": tot_cached,
        "outputTokens": tot_output,
        "reasoningOutputTokens": tot_reasoning,
        "totalTokens": tot_tokens,
        "costUSD": tot_cost,
    }
    return json.dumps({list_key: bucket_list, "totals": totals}, indent=2)


def _codex_sessions_to_json(sessions: list[CodexSessionUsage]) -> str:
    """Serialize Codex session aggregates to JSON matching upstream exactly.

    Per-session shape:
      {sessionId, lastActivity, sessionFile, directory,
       inputTokens, cachedInputTokens, outputTokens,
       reasoningOutputTokens, totalTokens, costUSD, models}
    """
    session_list: list[dict[str, Any]] = []
    tot_input = tot_cached = tot_output = tot_reasoning = tot_tokens = 0
    tot_cost = 0.0
    for s in sessions:
        session_total = s.input_tokens + s.output_tokens
        session_list.append({
            "sessionId": s.session_id_path,
            "lastActivity": _codex_last_activity_iso(s.last_activity),
            "sessionFile": s.session_file,
            "directory": s.directory,
            "inputTokens": s.input_tokens,
            "cachedInputTokens": s.cached_input_tokens,
            "outputTokens": s.output_tokens,
            "reasoningOutputTokens": s.reasoning_output_tokens,
            "totalTokens": session_total,
            "costUSD": s.cost_usd,
            "models": _codex_models_dict(s.model_breakdowns),
        })
        tot_input += s.input_tokens
        tot_cached += s.cached_input_tokens
        tot_output += s.output_tokens
        tot_reasoning += s.reasoning_output_tokens
        tot_tokens += session_total
        tot_cost += s.cost_usd

    totals = {
        "inputTokens": tot_input,
        "cachedInputTokens": tot_cached,
        "outputTokens": tot_output,
        "reasoningOutputTokens": tot_reasoning,
        "totalTokens": tot_tokens,
        "costUSD": tot_cost,
    }
    return json.dumps({"sessions": session_list, "totals": totals}, indent=2)


def _claude_sessions_to_json(sessions: list[ClaudeSessionUsage]) -> str:
    """Serialize Claude sessions to JSON per spec A2.8.

    Per-session: sessionId, projectPath, sourcePaths (list), firstActivity
    / lastActivity ISO strings, modelsUsed, token counts
    (input/cacheCreation/cacheRead/output/total), totalCost, modelBreakdowns
    (camelCased token field names, cost).

    totals: same 6 numeric fields aggregated across sessions.
    """
    sess_list: list[dict[str, Any]] = []
    tot_i = tot_cc = tot_cr = tot_o = tot_t = 0
    tot_cost = 0.0

    for s in sessions:
        sess_list.append({
            "sessionId": s.session_id,
            "projectPath": s.project_path,
            "sourcePaths": list(s.source_paths),
            "firstActivity": s.first_activity.isoformat(),
            "lastActivity": s.last_activity.isoformat(),
            "modelsUsed": list(s.models),
            "inputTokens": s.input_tokens,
            "cacheCreationTokens": s.cache_creation_tokens,
            "cacheReadTokens": s.cache_read_tokens,
            "outputTokens": s.output_tokens,
            "totalTokens": s.total_tokens,
            "totalCost": s.cost_usd,
            "modelBreakdowns": [
                {
                    "model": mb["model"],
                    "inputTokens": mb["input"],
                    "cacheCreationTokens": mb["cache_create"],
                    "cacheReadTokens": mb["cache_read"],
                    "outputTokens": mb["output"],
                    "cost": mb["cost"],
                }
                for mb in s.model_breakdowns
            ],
        })
        tot_i += s.input_tokens
        tot_cc += s.cache_creation_tokens
        tot_cr += s.cache_read_tokens
        tot_o += s.output_tokens
        tot_t += s.total_tokens
        tot_cost += s.cost_usd

    payload = {
        "sessions": sess_list,
        "totals": {
            "inputTokens": tot_i,
            "cacheCreationTokens": tot_cc,
            "cacheReadTokens": tot_cr,
            "outputTokens": tot_o,
            "totalTokens": tot_t,
            "totalCost": tot_cost,
        },
    }
    return json.dumps(payload, indent=2)


def _render_bucket_table(
    buckets: list[BucketUsage],
    *,
    first_col_name: str,
    title_suffix: str,
    compact_split_fn: Callable[[str], str],
    breakdown: bool = False,
) -> str:
    """Render bucket aggregates as a ccusage-style ANSI table.

    Shared between `daily` and `monthly` subcommands.  Parameters:
      first_col_name  — header for the bucket column ("Date" or "Month").
      title_suffix    — banner text suffix ("Daily" or "Monthly").
      compact_split_fn — function that splits a bucket string into
                         "YYYY\\n..." for compact-mode two-line display.

    Mirrors ccusage's ResponsiveTable behavior: single-line headers and dates
    when content fits the terminal; falls back to two-line compact headers
    ("Cache"/"Create") and dates ("YYYY"/"MM-DD") with numeric truncation when
    scaling is needed. Breakdown rows are a single line ("  └─ model") in
    gray; the Total row is colored yellow.
    """
    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:
        return _style_ansi(s, "90", color)

    def _cyan(s: str) -> str:
        return _style_ansi(s, "36", color)

    def _bold(s: str) -> str:
        return _style_ansi(s, "1", color)

    def _yellow(s: str) -> str:
        return _style_ansi(s, "33", color)

    def _gray(s: str) -> str:
        return _style_ansi(s, "90", color)

    headers = [
        first_col_name, "Models", "Input", "Output",
        "Cache Create", "Cache Read", "Total Tokens", "Cost (USD)",
    ]
    aligns = ["left", "left", "right", "right", "right", "right", "right", "right"]
    num_cols = len(headers)

    arrow = "  \u2514\u2500" if unicode_ok else "  |_"

    # ── Build raw rows: each is (cells, row_type) where a cell is the
    #    tuple (text, color_fn_or_none). `text` may contain '\n' for
    #    multi-line cells (Models list, compact Date).
    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for d in buckets:
        # ccusage formatModelsDisplayMultiline: uniq → sort alphabetical
        short_models = sorted({_short_model_name(m) for m in d.models})
        models_text = "\n".join(f"- {m}" for m in short_models) if short_models else ""
        data_cells = [
            (d.bucket, None),
            (models_text, None),
            (_fmt_num(d.input_tokens), None),
            (_fmt_num(d.output_tokens), None),
            (_fmt_num(d.cache_creation_tokens), None),
            (_fmt_num(d.cache_read_tokens), None),
            (_fmt_num(d.total_tokens), None),
            (f"${d.cost_usd:.2f}", None),
        ]
        raw_rows.append((data_cells, ROW_DATA))

        if breakdown:
            for mb in d.model_breakdowns:
                short = _short_model_name(mb["modelName"])
                mb_input = int(mb["inputTokens"])
                mb_output = int(mb["outputTokens"])
                mb_cc = int(mb["cacheCreationTokens"])
                mb_cr = int(mb["cacheReadTokens"])
                mb_total = mb_input + mb_output + mb_cc + mb_cr
                mb_cost = float(mb["cost"])
                bd_cells = [
                    (f"{arrow} {short}", _gray),
                    ("", None),
                    (_fmt_num(mb_input), _gray),
                    (_fmt_num(mb_output), _gray),
                    (_fmt_num(mb_cc), _gray),
                    (_fmt_num(mb_cr), _gray),
                    (_fmt_num(mb_total), _gray),
                    (f"${mb_cost:.2f}", _gray),
                ]
                raw_rows.append((bd_cells, ROW_BREAKDOWN))

    # Total footer row — yellow on all populated cells.
    tot_input = sum(d.input_tokens for d in buckets)
    tot_output = sum(d.output_tokens for d in buckets)
    tot_cc = sum(d.cache_creation_tokens for d in buckets)
    tot_cr = sum(d.cache_read_tokens for d in buckets)
    tot_tokens = sum(d.total_tokens for d in buckets)
    tot_cost = sum(d.cost_usd for d in buckets)
    footer_cells = [
        ("Total", _yellow),
        ("", None),
        (_fmt_num(tot_input), _yellow),
        (_fmt_num(tot_output), _yellow),
        (_fmt_num(tot_cc), _yellow),
        (_fmt_num(tot_cr), _yellow),
        (_fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    # ── Compute content widths (single-line form: header as-is, dates
    #    single-line). Multi-line cell width = longest line.
    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    # ── Wide-mode column widths (ccusage formula) ───────────────────────
    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 1:           # Models
            return max(content + 2, 15)
        return max(content + 2, 10)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    compact_mode = sum(col_widths) + border_overhead > term_width

    if compact_mode:
        # Scale down proportionally with narrow minimums.
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0

        def _narrow_min(i: int) -> int:
            if aligns[i] == "right":
                return 10
            if i == 0:       # Date
                return 10
            if i == 1:       # Models
                return 12
            return 8

        col_widths = [
            max(int(w * scale), _narrow_min(i))
            for i, w in enumerate(col_widths)
        ]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[1] += remainder

    # ── Choose header presentation: single-line in wide mode;
    #    split multi-word headers to 2 lines when compact.
    if compact_mode:
        header_display = [h.replace(" ", "\n") for h in headers]
    else:
        header_display = headers[:]

    # ── Convert raw rows to multi-line display rows. In compact mode
    #    dates split to 2 lines ("YYYY" / "MM-DD").
    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _split_bucket_if_compact(text: str) -> str:
        if compact_mode:
            return compact_split_fn(text)
        return text

    display_rows: list[tuple[list[list[tuple[str, Any]]], str]] = []
    for cells, row_type in raw_rows:
        processed: list[tuple[str, Any]] = []
        for i, (text, cfn) in enumerate(cells):
            t = _split_bucket_if_compact(text) if i == 0 else text
            processed.append((t, cfn))
        line_counts = [len(_split_cell(t)) for t, _ in processed]
        n_lines = max(line_counts) if line_counts else 1
        row_lines: list[list[tuple[str, Any]]] = []
        for li in range(n_lines):
            row_cells: list[tuple[str, Any]] = []
            for (text, cfn) in processed:
                parts = _split_cell(text)
                row_cells.append((parts[li] if li < len(parts) else "", cfn))
            row_lines.append(row_cells)
        display_rows.append((row_lines, row_type))

    # Header display lines (multi-line in compact mode).
    header_line_counts = [len(_split_cell(h)) for h in header_display]
    header_n_lines = max(header_line_counts) if header_line_counts else 1
    header_lines: list[list[str]] = []
    for li in range(header_n_lines):
        line = []
        for h in header_display:
            parts = _split_cell(h)
            line.append(parts[li] if li < len(parts) else "")
        header_lines.append(line)

    # ── Box-drawing chars ───────────────────────────────────────────────
    if unicode_ok:
        ch = {
            "tl": "\u250c", "tm": "\u252c", "tr": "\u2510",
            "ml": "\u251c", "mm": "\u253c", "mr": "\u2524",
            "bl": "\u2514", "bm": "\u2534", "br": "\u2518",
            "h": "\u2500", "v": "\u2502",
        }
    else:
        ch = {k: c for k, c in zip(
            ["tl", "tm", "tr", "ml", "mm", "mr", "bl", "bm", "br", "h", "v"],
            "+++++++++-|",
        )}

    def hline(left: str, mid: str, right: str) -> str:
        segs = [ch["h"] * (col_widths[i] + 2) for i in range(num_cols)]
        return _dim(left + mid.join(segs) + right)

    def padcell(text: str, width: int, align: str) -> str:
        vis_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
        pad_needed = width - vis_len
        if pad_needed <= 0:
            return text
        if align == "right":
            return " " * pad_needed + text
        return text + " " * pad_needed

    def make_row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell_text in enumerate(cells):
            padded = padcell(cell_text, col_widths[i], aligns[i])
            parts.append(f" {padded} ")
        v = _dim(ch["v"])
        return v + v.join(parts) + v

    # ── Title banner ────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("")
    title = f"Claude Code Token Usage Report - {title_suffix}"
    title_padded = f"  {title}  "
    tw = len(title_padded)
    dash = "\u2500" if unicode_ok else "-"
    vb = "\u2502" if unicode_ok else "|"
    if unicode_ok:
        banner_top = f" \u256d{dash * tw}\u256e"
        banner_bot = f" \u2570{dash * tw}\u256f"
    else:
        banner_top = f" +{'-' * tw}+"
        banner_bot = f" +{'-' * tw}+"
    lines.append(banner_top)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(f" {vb}" + _bold(title_padded) + vb)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(banner_bot)
    lines.append("")

    # ── Header ──────────────────────────────────────────────────────────
    lines.append(hline(ch["tl"], ch["tm"], ch["tr"]))
    for line_cells in header_lines:
        lines.append(make_row([_cyan(c) for c in line_cells]))
    lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    # ── Data + footer rows, with separators between every row ──────────
    numeric_cols = (2, 3, 4, 5, 6, 7)  # Input, Output, CacheC, CacheR, Total, Cost

    def _render_display_row(row_lines: list[list[tuple[str, Any]]]) -> None:
        for line_cells in row_lines:
            rendered: list[str] = []
            for ci, (text, cfn) in enumerate(line_cells):
                out = text
                if compact_mode and ci in numeric_cols and out:
                    out = _truncate_num(out, col_widths[ci])
                if cfn is not None and out:
                    out = cfn(out)
                rendered.append(out)
            lines.append(make_row(rendered))

    for idx, (row_lines, _rt) in enumerate(display_rows):
        _render_display_row(row_lines)
        if idx < len(display_rows) - 1:
            lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    lines.append(hline(ch["bl"], ch["bm"], ch["br"]))
    return "\n".join(lines)


def _render_weekly_table(
    buckets: list[BucketUsage],
    week_pct_overlay: list[tuple[float | None, float | None]],
    *,
    weeks: list["SubWeek"],
    compact_split_fn: Callable[[str], str],
    breakdown: bool = False,
) -> str:
    """Render weekly bucket aggregates as a ccusage-style ANSI table.

    `weeks` is the parallel `SubWeek` metadata list \u2014 each `bucket.bucket`
    key (`start_date.isoformat()`) maps to one `SubWeek` via a local
    lookup. The Week column is rendered from `display_start_date` so that
    post-early-reset weeks show their effective start (e.g., 2026-04-13)
    rather than the API-derived backdated `start_date` (e.g., 2026-04-11);
    for non-reset weeks the two are equal and the rendering is unchanged.

    Near-clone of `_render_bucket_table` with two additional right-edge
    columns, `Used %` and `$/1%`, whose per-week values are supplied by
    the caller as a parallel list `week_pct_overlay[i] = (used_pct, dpc)`.
    Missing overlay values render as "\u2014" (em-dash). Breakdown sub-rows
    emit empty cells in the new columns (they are per-model, not per-week).
    The Total footer emits "\u2014" in both (summing percentages is not
    meaningful).

    `first_col_name` and `title_suffix` are hardcoded to "Week" and
    "Weekly" respectively.
    """
    assert len(week_pct_overlay) == len(buckets), (
        f"week_pct_overlay length {len(week_pct_overlay)} does not match "
        f"buckets length {len(buckets)} — caller contract violated"
    )
    # Lookup map for the Week-cell label: bucket key (= API-derived
    # start_date) → SubWeek, so we can read display_start_date without
    # changing the bucket aggregation key.
    week_by_key = {w.start_date.isoformat(): w for w in weeks}
    first_col_name = "Week"
    title_suffix = "Weekly"

    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:
        return _style_ansi(s, "90", color)

    def _cyan(s: str) -> str:
        return _style_ansi(s, "36", color)

    def _bold(s: str) -> str:
        return _style_ansi(s, "1", color)

    def _yellow(s: str) -> str:
        return _style_ansi(s, "33", color)

    def _gray(s: str) -> str:
        return _style_ansi(s, "90", color)

    headers = [
        first_col_name, "Models", "Input", "Output",
        "Cache Create", "Cache Read", "Total Tokens", "Cost (USD)",
        "Used %", "$/1%",
    ]
    aligns = [
        "left", "left", "right", "right", "right", "right", "right", "right",
        "right", "right",
    ]
    num_cols = len(headers)

    arrow = "  \u2514\u2500" if unicode_ok else "  |_"
    em_dash = "\u2014" if unicode_ok else "-"

    # ── Build raw rows: each is (cells, row_type) where a cell is the
    #    tuple (text, color_fn_or_none). `text` may contain '\n' for
    #    multi-line cells (Models list, compact Date).
    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for i, d in enumerate(buckets):
        # ccusage formatModelsDisplayMultiline: uniq → sort alphabetical
        short_models = sorted({_short_model_name(m) for m in d.models})
        models_text = "\n".join(f"- {m}" for m in short_models) if short_models else ""
        used_pct, dpc = week_pct_overlay[i]
        used_pct_text = f"{used_pct:.1f}%" if used_pct is not None else em_dash
        dpc_text = f"{dpc:.3f}" if dpc is not None else em_dash
        # Render the Week column from display_start_date — equals d.bucket
        # for non-reset weeks; shifted forward for post-early-reset weeks.
        # The bucket-aggregation contract guarantees a SubWeek for every
        # bucket key, so a missing key here is a contract violation; raise
        # KeyError loudly to mirror the dashboard's `next(...)`-raises-
        # StopIteration call site at _dashboard_build_weekly_periods.
        sw = week_by_key[d.bucket]
        display_label = sw.display_start_date.isoformat()
        data_cells = [
            (display_label, None),
            (models_text, None),
            (_fmt_num(d.input_tokens), None),
            (_fmt_num(d.output_tokens), None),
            (_fmt_num(d.cache_creation_tokens), None),
            (_fmt_num(d.cache_read_tokens), None),
            (_fmt_num(d.total_tokens), None),
            (f"${d.cost_usd:.2f}", None),
            (used_pct_text, None),
            (dpc_text, None),
        ]
        raw_rows.append((data_cells, ROW_DATA))

        if breakdown:
            for mb in d.model_breakdowns:
                short = _short_model_name(mb["modelName"])
                mb_input = int(mb["inputTokens"])
                mb_output = int(mb["outputTokens"])
                mb_cc = int(mb["cacheCreationTokens"])
                mb_cr = int(mb["cacheReadTokens"])
                mb_total = mb_input + mb_output + mb_cc + mb_cr
                mb_cost = float(mb["cost"])
                bd_cells = [
                    (f"{arrow} {short}", _gray),
                    ("", None),
                    (_fmt_num(mb_input), _gray),
                    (_fmt_num(mb_output), _gray),
                    (_fmt_num(mb_cc), _gray),
                    (_fmt_num(mb_cr), _gray),
                    (_fmt_num(mb_total), _gray),
                    (f"${mb_cost:.2f}", _gray),
                    ("", None),
                    ("", None),
                ]
                raw_rows.append((bd_cells, ROW_BREAKDOWN))

    # Total footer row — yellow on all populated cells.
    tot_input = sum(d.input_tokens for d in buckets)
    tot_output = sum(d.output_tokens for d in buckets)
    tot_cc = sum(d.cache_creation_tokens for d in buckets)
    tot_cr = sum(d.cache_read_tokens for d in buckets)
    tot_tokens = sum(d.total_tokens for d in buckets)
    tot_cost = sum(d.cost_usd for d in buckets)
    footer_cells = [
        ("Total", _yellow),
        ("", None),
        (_fmt_num(tot_input), _yellow),
        (_fmt_num(tot_output), _yellow),
        (_fmt_num(tot_cc), _yellow),
        (_fmt_num(tot_cr), _yellow),
        (_fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
        (em_dash, _yellow),
        (em_dash, _yellow),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    # ── Compute content widths (single-line form: header as-is, dates
    #    single-line). Multi-line cell width = longest line.
    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    # ── Wide-mode column widths (ccusage formula) ───────────────────────
    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 1:           # Models
            return max(content + 2, 15)
        return max(content + 2, 10)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    compact_mode = sum(col_widths) + border_overhead > term_width

    if compact_mode:
        # Scale down proportionally with narrow minimums.
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0

        def _narrow_min(i: int) -> int:
            if aligns[i] == "right":
                return 10
            if i == 0:       # Week
                return 10
            if i == 1:       # Models
                return 12
            return 8

        col_widths = [
            max(int(w * scale), _narrow_min(i))
            for i, w in enumerate(col_widths)
        ]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[1] += remainder

    # ── Choose header presentation: single-line in wide mode;
    #    split multi-word headers to 2 lines when compact.
    if compact_mode:
        header_display = [h.replace(" ", "\n") for h in headers]
    else:
        header_display = headers[:]

    # ── Convert raw rows to multi-line display rows. In compact mode
    #    dates split to 2 lines ("YYYY" / "MM-DD").
    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _split_bucket_if_compact(text: str) -> str:
        if compact_mode:
            return compact_split_fn(text)
        return text

    display_rows: list[tuple[list[list[tuple[str, Any]]], str]] = []
    for cells, row_type in raw_rows:
        processed: list[tuple[str, Any]] = []
        for i, (text, cfn) in enumerate(cells):
            t = _split_bucket_if_compact(text) if i == 0 else text
            processed.append((t, cfn))
        line_counts = [len(_split_cell(t)) for t, _ in processed]
        n_lines = max(line_counts) if line_counts else 1
        row_lines: list[list[tuple[str, Any]]] = []
        for li in range(n_lines):
            row_cells: list[tuple[str, Any]] = []
            for (text, cfn) in processed:
                parts = _split_cell(text)
                row_cells.append((parts[li] if li < len(parts) else "", cfn))
            row_lines.append(row_cells)
        display_rows.append((row_lines, row_type))

    # Header display lines (multi-line in compact mode).
    header_line_counts = [len(_split_cell(h)) for h in header_display]
    header_n_lines = max(header_line_counts) if header_line_counts else 1
    header_lines: list[list[str]] = []
    for li in range(header_n_lines):
        line = []
        for h in header_display:
            parts = _split_cell(h)
            line.append(parts[li] if li < len(parts) else "")
        header_lines.append(line)

    # ── Box-drawing chars ───────────────────────────────────────────────
    if unicode_ok:
        ch = {
            "tl": "\u250c", "tm": "\u252c", "tr": "\u2510",
            "ml": "\u251c", "mm": "\u253c", "mr": "\u2524",
            "bl": "\u2514", "bm": "\u2534", "br": "\u2518",
            "h": "\u2500", "v": "\u2502",
        }
    else:
        ch = {k: c for k, c in zip(
            ["tl", "tm", "tr", "ml", "mm", "mr", "bl", "bm", "br", "h", "v"],
            "+++++++++-|",
        )}

    def hline(left: str, mid: str, right: str) -> str:
        segs = [ch["h"] * (col_widths[i] + 2) for i in range(num_cols)]
        return _dim(left + mid.join(segs) + right)

    def padcell(text: str, width: int, align: str) -> str:
        vis_len = len(re.sub(r"\033\[[0-9;]*m", "", text))
        pad_needed = width - vis_len
        if pad_needed <= 0:
            return text
        if align == "right":
            return " " * pad_needed + text
        return text + " " * pad_needed

    def make_row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell_text in enumerate(cells):
            padded = padcell(cell_text, col_widths[i], aligns[i])
            parts.append(f" {padded} ")
        v = _dim(ch["v"])
        return v + v.join(parts) + v

    # ── Title banner ────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("")
    title = f"Claude Code Token Usage Report - {title_suffix}"
    title_padded = f"  {title}  "
    tw = len(title_padded)
    dash = "\u2500" if unicode_ok else "-"
    vb = "\u2502" if unicode_ok else "|"
    if unicode_ok:
        banner_top = f" \u256d{dash * tw}\u256e"
        banner_bot = f" \u2570{dash * tw}\u256f"
    else:
        banner_top = f" +{'-' * tw}+"
        banner_bot = f" +{'-' * tw}+"
    lines.append(banner_top)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(f" {vb}" + _bold(title_padded) + vb)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(banner_bot)
    lines.append("")

    # ── Header ──────────────────────────────────────────────────────────
    lines.append(hline(ch["tl"], ch["tm"], ch["tr"]))
    for line_cells in header_lines:
        lines.append(make_row([_cyan(c) for c in line_cells]))
    lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    # ── Data + footer rows, with separators between every row ──────────
    # Input, Output, CacheC, CacheR, Total, Cost, Used %, $/1%
    numeric_cols = (2, 3, 4, 5, 6, 7, 8, 9)

    def _render_display_row(row_lines: list[list[tuple[str, Any]]]) -> None:
        for line_cells in row_lines:
            rendered: list[str] = []
            for ci, (text, cfn) in enumerate(line_cells):
                out = text
                if compact_mode and ci in numeric_cols and out:
                    out = _truncate_num(out, col_widths[ci])
                if cfn is not None and out:
                    out = cfn(out)
                rendered.append(out)
            lines.append(make_row(rendered))

    for idx, (row_lines, _rt) in enumerate(display_rows):
        _render_display_row(row_lines)
        if idx < len(display_rows) - 1:
            lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    lines.append(hline(ch["bl"], ch["bm"], ch["br"]))
    return "\n".join(lines)


def _render_codex_bucket_table(
    buckets: list[CodexBucketUsage],
    *,
    first_col_name: str,             # "Date" or "Month"
    title: str,                      # banner title text
    compact_split_fn: Callable[[str], str],
    bucket_display_fn: Callable[[str], str],
    breakdown: bool = False,
    force_compact: bool = False,
) -> str:
    """Render Codex bucket aggregates matching upstream ccusage-codex daily/monthly tables.

    Byte-parity-targeted against upstream `ccusage-codex daily|monthly`:
      - banner indented by 1 space; 2-space padding around title text
      - inter-row separator (├┼...┤) between every data row AND between
        last data row and footer
      - 8 columns: <Date|Month> | Models | Input | Output | Reasoning |
                   Cache Read | Total Tokens | Cost (USD)
      - Input column = input_tokens - cached_input_tokens (non-cached)
      - Total Tokens column = input_tokens + output_tokens (derived)
    """
    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:  return _style_ansi(s, "90", color)
    def _cyan(s: str) -> str: return _style_ansi(s, "36", color)
    def _yellow(s: str) -> str: return _style_ansi(s, "33", color)
    def _gray(s: str) -> str: return _style_ansi(s, "90", color)

    headers = [
        first_col_name, "Models", "Input", "Output",
        "Reasoning", "Cache Read", "Total Tokens", "Cost (USD)",
    ]
    aligns = ["left", "left", "right", "right", "right", "right", "right", "right"]
    num_cols = len(headers)

    arrow = "  \u2514\u2500" if unicode_ok else "  |_"

    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for b in buckets:
        models_text = "\n".join(f"- {m}" for m in b.models) if b.models else ""
        non_cached = max(0, b.input_tokens - b.cached_input_tokens)
        bucket_total = b.input_tokens + b.output_tokens
        data_cells = [
            (bucket_display_fn(b.bucket), None),
            (models_text, None),
            (_fmt_num(non_cached), None),
            (_fmt_num(b.output_tokens), None),
            (_fmt_num(b.reasoning_output_tokens), None),
            (_fmt_num(b.cached_input_tokens), None),
            (_fmt_num(bucket_total), None),
            (f"${b.cost_usd:.2f}", None),
        ]
        raw_rows.append((data_cells, ROW_DATA))

        if breakdown:
            for mb in b.model_breakdowns:
                name = mb["modelName"]
                mb_input_inclusive = int(mb["inputTokens"])
                mb_cached = int(mb["cachedInputTokens"])
                mb_output = int(mb["outputTokens"])
                mb_reasoning = int(mb["reasoningOutputTokens"])
                mb_non_cached = max(0, mb_input_inclusive - mb_cached)
                mb_total = mb_input_inclusive + mb_output
                mb_cost = float(mb["cost"])
                bd_cells = [
                    (f"{arrow} {name}", _gray),
                    ("", None),
                    (_fmt_num(mb_non_cached), _gray),
                    (_fmt_num(mb_output), _gray),
                    (_fmt_num(mb_reasoning), _gray),
                    (_fmt_num(mb_cached), _gray),
                    (_fmt_num(mb_total), _gray),
                    (f"${mb_cost:.2f}", _gray),
                ]
                raw_rows.append((bd_cells, ROW_BREAKDOWN))

    tot_input_inclusive = sum(b.input_tokens for b in buckets)
    tot_cached = sum(b.cached_input_tokens for b in buckets)
    tot_output = sum(b.output_tokens for b in buckets)
    tot_reasoning = sum(b.reasoning_output_tokens for b in buckets)
    tot_non_cached = max(0, tot_input_inclusive - tot_cached)
    tot_tokens = tot_input_inclusive + tot_output
    tot_cost = sum(b.cost_usd for b in buckets)
    footer_cells = [
        ("Total", _yellow),
        ("", None),
        (_fmt_num(tot_non_cached), _yellow),
        (_fmt_num(tot_output), _yellow),
        (_fmt_num(tot_reasoning), _yellow),
        (_fmt_num(tot_cached), _yellow),
        (_fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 1:
            return max(content + 2, 15)
        return max(content + 2, 10)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    # `force_compact` (from --compact) short-circuits the width-based
    # auto-detect. Matches upstream's `--compact` behavior of always
    # rendering the narrow layout regardless of terminal width.
    compact_mode = force_compact or (sum(col_widths) + border_overhead > term_width)

    if compact_mode:
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0

        def _narrow_min(i: int) -> int:
            if aligns[i] == "right":
                return 10
            if i == 0:
                return 10
            if i == 1:
                return 12
            return 8

        col_widths = [
            max(int(w * scale), _narrow_min(i))
            for i, w in enumerate(col_widths)
        ]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[1] += remainder

    if compact_mode:
        header_display = [h.replace(" ", "\n") for h in headers]
    else:
        header_display = headers[:]

    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _split_bucket_if_compact(text: str) -> str:
        if compact_mode:
            return compact_split_fn(text)
        return text

    display_rows: list[tuple[list[list[str]], str, list[Any]]] = []
    for cells, rt in raw_rows:
        display_cells: list[list[str]] = []
        colors: list[Any] = []
        for i, (text, cfn) in enumerate(cells):
            if rt == ROW_DATA and i == 0:
                text = _split_bucket_if_compact(text)
            lines = _split_cell(text)
            w = col_widths[i]
            truncated: list[str] = []
            for ln in lines:
                if len(ln) <= w:
                    truncated.append(ln)
                else:
                    ell = "\u2026" if unicode_ok else "..."
                    truncated.append(ln[: max(0, w - len(ell))] + ell)
            display_cells.append(truncated)
            colors.append(cfn)
        display_rows.append((display_cells, rt, colors))

    # Box-drawing
    if unicode_ok:
        TL, TR, BL, BR = "\u250c", "\u2510", "\u2514", "\u2518"
        H, V = "\u2500", "\u2502"
        T_DOWN, T_UP, T_LEFT, T_RIGHT, CROSS = "\u252c", "\u2534", "\u2524", "\u251c", "\u253c"
        RTL, RTR, RBL, RBR = "\u256d", "\u256e", "\u2570", "\u256f"
    else:
        TL = TR = BL = BR = "+"
        H, V = "-", "|"
        T_DOWN = T_UP = T_LEFT = T_RIGHT = CROSS = "+"
        RTL = RTR = RBL = RBR = "+"

    def _border_row(left: str, mid: str, right: str) -> str:
        parts = [left]
        for i, w in enumerate(col_widths):
            parts.append(H * (w + 2))
            parts.append(mid if i < num_cols - 1 else right)
        return _dim("".join(parts))

    def _pad_cell(text: str, w: int, align: str) -> str:
        if align == "right":
            return text.rjust(w)
        return text.ljust(w)

    def _render_row(display_cells: list[list[str]], colors: list[Any]) -> list[str]:
        max_h = max(len(c) for c in display_cells) if display_cells else 1
        out_lines: list[str] = []
        for li in range(max_h):
            parts: list[str] = [_dim(V)]
            for i, cell in enumerate(display_cells):
                content = cell[li] if li < len(cell) else ""
                padded = _pad_cell(content, col_widths[i], aligns[i])
                if colors[i] is not None:
                    padded = colors[i](padded)
                parts.append(f" {padded} ")
                parts.append(_dim(V))
            out_lines.append("".join(parts))
        return out_lines

    # Banner — 1-space leading indent on each line, 2-space padding around title
    banner_inner_width = max(len(title) + 4, 60)
    left_pad = 2
    right_pad = banner_inner_width - len(title) - left_pad
    indent = " "  # upstream banner indents by 1 space
    top    = indent + RTL + H * banner_inner_width + RTR
    blank  = indent + V   + " " * banner_inner_width + V
    text_line = indent + V + " " * left_pad + title + " " * right_pad + V
    bottom = indent + RBL + H * banner_inner_width + RBR
    banner_lines = [_dim(top), _dim(blank), _dim(text_line), _dim(blank), _dim(bottom)]

    # Assemble
    out: list[str] = []
    out.extend(banner_lines)
    out.append("")  # blank line between banner and table (matches upstream)
    out.append(_border_row(TL, T_DOWN, TR))

    # Header row (cyan per cell)
    header_display_cells = [_split_cell(h) for h in header_display]
    max_h = max(len(c) for c in header_display_cells)
    for li in range(max_h):
        parts: list[str] = [_dim(V)]
        for i, cell in enumerate(header_display_cells):
            content = cell[li] if li < len(cell) else ""
            padded = _pad_cell(content, col_widths[i], aligns[i])
            parts.append(f" {_cyan(padded)} ")
            parts.append(_dim(V))
        out.append("".join(parts))
    out.append(_border_row(T_RIGHT, CROSS, T_LEFT))

    # Data + breakdown + footer, with inter-row separators
    sep = _border_row(T_RIGHT, CROSS, T_LEFT)
    for idx, (display_cells, rt, colors) in enumerate(display_rows):
        for ln in _render_row(display_cells, colors):
            out.append(ln)
        if idx < len(display_rows) - 1:
            # Separator between every row (data, breakdown, and between last
            # data row and footer) — matches upstream.
            out.append(sep)

    out.append(_border_row(BL, T_UP, BR))
    return "\n".join(out)


def _render_codex_session_table(
    sessions: list[CodexSessionUsage],
    *,
    title: str,
    force_compact: bool = False,
    tz_name: str | None = None,
) -> str:
    """Render Codex session aggregates matching upstream ccusage-codex session (11 cols).

    Columns:
      Date | Directory | Session | Models | Input | Output | Reasoning |
      Cache Read | Total Tokens | Cost (USD) | Last Activity

    Structural parity with Task 8's _render_codex_bucket_table:
      - banner with 1-space leading indent + 2-space title padding
      - inter-row separators (├┼...┤) between every row and before footer
      - Input column = non_cached_input (derived)
      - Total Tokens column = input + output (derived)

    ``force_compact`` honors upstream's ``--compact`` flag by always
    rendering the narrow layout. ``tz_name`` (from upstream's
    ``--timezone``) selects the IANA zone used to format Date /
    Last Activity cells; default falls back to local OS tz.
    """
    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:  return _style_ansi(s, "90", color)
    def _cyan(s: str) -> str: return _style_ansi(s, "36", color)
    def _yellow(s: str) -> str: return _style_ansi(s, "33", color)

    headers = [
        "Date", "Directory", "Session", "Models",
        "Input", "Output", "Reasoning", "Cache Read",
        "Total Tokens", "Cost (USD)", "Last Activity",
    ]
    aligns = [
        "left", "left", "left", "left",
        "right", "right", "right", "right",
        "right", "right", "left",
    ]
    num_cols = len(headers)

    _display_tz = _resolve_tz(tz_name)

    def _to_display_tz(ts: dt.datetime) -> dt.datetime:
        # internal fallback: host-local intentional (AM/PM render via attribute access)
        return ts.astimezone(_display_tz) if _display_tz is not None else ts.astimezone()

    def _date_cell(ts: dt.datetime) -> str:
        local = _to_display_tz(ts)
        return f"{_CODEX_MONTHS[local.month - 1]} {local.day:02d},\n{local.year}"

    def _last_activity_cell(ts: dt.datetime) -> str:
        local = _to_display_tz(ts)
        hour_12 = local.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        ampm = "a.m." if local.hour < 12 else "p.m."
        return f"{local.year}-{local.month:02d}-{local.day:02d}\n{hour_12}:{local.minute:02d}\n{ampm}"

    def _session_cell(session_id: str) -> str:
        if not session_id:
            return ""
        tail = session_id.split("-")[-1][-4:] if "-" in session_id else session_id[-4:]
        return f"\u2026{tail}\u2026" if unicode_ok else f"...{tail}..."

    ROW_DATA, ROW_FOOTER = "data", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for s in sessions:
        models_text = "\n".join(f"- {m}" for m in s.models) if s.models else ""
        non_cached = max(0, s.input_tokens - s.cached_input_tokens)
        session_total = s.input_tokens + s.output_tokens
        data_cells = [
            (_date_cell(s.last_activity), None),
            (s.directory, None),
            (_session_cell(s.session_id), None),
            (models_text, None),
            (_fmt_num(non_cached), None),
            (_fmt_num(s.output_tokens), None),
            (_fmt_num(s.reasoning_output_tokens), None),
            (_fmt_num(s.cached_input_tokens), None),
            (_fmt_num(session_total), None),
            (f"${s.cost_usd:.2f}", None),
            (_last_activity_cell(s.last_activity), None),
        ]
        raw_rows.append((data_cells, ROW_DATA))

    tot_input_inclusive = sum(s.input_tokens for s in sessions)
    tot_cached = sum(s.cached_input_tokens for s in sessions)
    tot_output = sum(s.output_tokens for s in sessions)
    tot_reasoning = sum(s.reasoning_output_tokens for s in sessions)
    tot_non_cached = max(0, tot_input_inclusive - tot_cached)
    tot_tokens = tot_input_inclusive + tot_output
    tot_cost = sum(s.cost_usd for s in sessions)
    footer_cells = [
        ("Total", _yellow),
        ("", None), ("", None), ("", None),
        (_fmt_num(tot_non_cached), _yellow),
        (_fmt_num(tot_output), _yellow),
        (_fmt_num(tot_reasoning), _yellow),
        (_fmt_num(tot_cached), _yellow),
        (_fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
        ("", None),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 0:
            return max(content + 2, 12)
        if i == 1:
            return max(content + 2, 15)
        return max(content + 2, 12)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    # `force_compact` (from --compact) short-circuits the width-based
    # auto-detect so the narrow layout renders regardless of terminal width.
    if force_compact or (sum(col_widths) + border_overhead > term_width):
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0
        col_widths = [max(int(w * scale), 8) for w in col_widths]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[3] += remainder  # grow Models column

    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _pad_cell(text: str, w: int, align: str) -> str:
        if align == "right":
            return text.rjust(w)
        return text.ljust(w)

    if unicode_ok:
        TL, TR, BL, BR = "\u250c", "\u2510", "\u2514", "\u2518"
        H, V = "\u2500", "\u2502"
        T_DOWN, T_UP, T_LEFT, T_RIGHT, CROSS = "\u252c", "\u2534", "\u2524", "\u251c", "\u253c"
        RTL, RTR, RBL, RBR = "\u256d", "\u256e", "\u2570", "\u256f"
    else:
        TL = TR = BL = BR = "+"
        H, V = "-", "|"
        T_DOWN = T_UP = T_LEFT = T_RIGHT = CROSS = "+"
        RTL = RTR = RBL = RBR = "+"

    def _border_row(left: str, mid: str, right: str) -> str:
        parts = [left]
        for i, w in enumerate(col_widths):
            parts.append(H * (w + 2))
            parts.append(mid if i < num_cols - 1 else right)
        return _dim("".join(parts))

    # Banner — 1-space leading indent + 2-space title padding
    banner_inner_width = max(len(title) + 4, 60)
    left_pad = 2
    right_pad = banner_inner_width - len(title) - left_pad
    indent = " "
    top    = indent + RTL + H * banner_inner_width + RTR
    blank  = indent + V   + " " * banner_inner_width + V
    text_line = indent + V + " " * left_pad + title + " " * right_pad + V
    bottom = indent + RBL + H * banner_inner_width + RBR
    out: list[str] = [_dim(top), _dim(blank), _dim(text_line), _dim(blank), _dim(bottom)]
    out.append("")  # blank line between banner and table

    out.append(_border_row(TL, T_DOWN, TR))

    # Header
    header_cells = [_split_cell(h) for h in headers]
    max_h = max(len(c) for c in header_cells)
    for li in range(max_h):
        parts = [_dim(V)]
        for i, cell in enumerate(header_cells):
            content = cell[li] if li < len(cell) else ""
            parts.append(f" {_cyan(_pad_cell(content, col_widths[i], aligns[i]))} ")
            parts.append(_dim(V))
        out.append("".join(parts))
    out.append(_border_row(T_RIGHT, CROSS, T_LEFT))

    # Data + footer with inter-row separators
    sep = _border_row(T_RIGHT, CROSS, T_LEFT)
    display_rows = list(raw_rows)
    for idx, (cells, rt) in enumerate(display_rows):
        split_cells = [_split_cell(t) for t, _c in cells]
        max_h = max(len(c) for c in split_cells) if split_cells else 1
        for li in range(max_h):
            parts = [_dim(V)]
            for i, (text, cfn) in enumerate(cells):
                content = split_cells[i][li] if li < len(split_cells[i]) else ""
                # Truncate with ellipsis if cell content exceeds column width
                w = col_widths[i]
                if len(content) > w:
                    ell = "\u2026" if unicode_ok else "..."
                    content = content[: max(0, w - len(ell))] + ell
                padded = _pad_cell(content, w, aligns[i])
                if cfn is not None:
                    padded = cfn(padded)
                parts.append(f" {padded} ")
                parts.append(_dim(V))
            out.append("".join(parts))
        if idx < len(display_rows) - 1:
            out.append(sep)

    out.append(_border_row(BL, T_UP, BR))
    return "\n".join(out)


def _render_claude_session_table(
    sessions: list[ClaudeSessionUsage],
    *,
    title: str = "Claude Token Usage Report - Sessions",
    breakdown: bool = False,
    tz: "ZoneInfo | None" = None,
) -> str:
    """Render Claude session aggregates matching upstream ccusage session view (11 cols).

    Columns:
      Date | Directory | Session | Models | Input | Cache Create |
      Cache Read | Output | Total Tokens | Cost (USD) | Last Activity

    Structural clone of `_render_codex_session_table` with:
      - ``Reasoning`` column replaced by ``Cache Create`` (sourced from
        ``cache_creation_tokens`` instead of ``reasoning_output_tokens``).
      - ``tz_name`` / ``force_compact`` parameters dropped — Claude-side
        commands don't expose ``--timezone`` / ``--compact`` today; dates
        render in local TZ via ``astimezone()`` and compact mode is
        triggered by terminal width alone.
      - ``Session`` cell shows first 8 chars of ``session_id`` (full UUID
        lives in --json).

    ``breakdown`` toggles per-model sub-rows beneath each session row.
    """
    color = _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:  return _style_ansi(s, "90", color)
    def _cyan(s: str) -> str: return _style_ansi(s, "36", color)
    def _yellow(s: str) -> str: return _style_ansi(s, "33", color)
    def _gray(s: str) -> str: return _style_ansi(s, "90", color)

    headers = [
        "Date", "Directory", "Session", "Models",
        "Input", "Cache Create", "Cache Read", "Output",
        "Total Tokens", "Cost (USD)", "Last Activity",
    ]
    aligns = [
        "left", "left", "left", "left",
        "right", "right", "right", "right",
        "right", "right", "left",
    ]
    num_cols = len(headers)

    def _to_display_tz(ts: dt.datetime) -> dt.datetime:
        return ts.astimezone(tz)

    def _date_cell(ts: dt.datetime) -> str:
        local = _to_display_tz(ts)
        return f"{_CODEX_MONTHS[local.month - 1]} {local.day:02d},\n{local.year}"

    def _last_activity_cell(ts: dt.datetime) -> str:
        local = _to_display_tz(ts)
        hour_12 = local.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        ampm = "a.m." if local.hour < 12 else "p.m."
        return f"{local.year}-{local.month:02d}-{local.day:02d}\n{hour_12}:{local.minute:02d}\n{ampm}"

    def _session_cell(session_id: str) -> str:
        if not session_id:
            return ""
        return session_id[:8]

    arrow = "  \u2514\u2500" if unicode_ok else "  |_"

    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for s in sessions:
        short_models = sorted({_short_model_name(m) for m in s.models})
        models_text = "\n".join(f"- {m}" for m in short_models) if short_models else ""
        # Spec A2.8: Total Tokens = input + output (cache shown separately,
        # not summed). Parallels `_render_codex_session_table` line ~4644.
        session_total = s.input_tokens + s.output_tokens
        data_cells = [
            (_date_cell(s.last_activity), None),
            (s.project_path, None),
            (_session_cell(s.session_id), None),
            (models_text, None),
            (_fmt_num(s.input_tokens), None),
            (_fmt_num(s.cache_creation_tokens), None),
            (_fmt_num(s.cache_read_tokens), None),
            (_fmt_num(s.output_tokens), None),
            (_fmt_num(session_total), None),
            (f"${s.cost_usd:.2f}", None),
            (_last_activity_cell(s.last_activity), None),
        ]
        raw_rows.append((data_cells, ROW_DATA))

        if breakdown:
            for mb in s.model_breakdowns:
                name = _short_model_name(mb["model"])
                mb_input = int(mb["input"])
                mb_cc = int(mb["cache_create"])
                mb_cr = int(mb["cache_read"])
                mb_output = int(mb["output"])
                # Spec A2.8: Total Tokens = input + output only.
                mb_total = mb_input + mb_output
                mb_cost = float(mb["cost"])
                bd_cells = [
                    (f"{arrow} {name}", _gray),
                    ("", None),
                    ("", None),
                    ("", None),
                    (_fmt_num(mb_input), _gray),
                    (_fmt_num(mb_cc), _gray),
                    (_fmt_num(mb_cr), _gray),
                    (_fmt_num(mb_output), _gray),
                    (_fmt_num(mb_total), _gray),
                    (f"${mb_cost:.2f}", _gray),
                    ("", None),
                ]
                raw_rows.append((bd_cells, ROW_BREAKDOWN))

    tot_input = sum(s.input_tokens for s in sessions)
    tot_cc = sum(s.cache_creation_tokens for s in sessions)
    tot_cr = sum(s.cache_read_tokens for s in sessions)
    tot_output = sum(s.output_tokens for s in sessions)
    # Spec A2.8: Total Tokens = input + output only.
    tot_tokens = tot_input + tot_output
    tot_cost = sum(s.cost_usd for s in sessions)
    footer_cells = [
        ("Total", _yellow),
        ("", None), ("", None), ("", None),
        (_fmt_num(tot_input), _yellow),
        (_fmt_num(tot_cc), _yellow),
        (_fmt_num(tot_cr), _yellow),
        (_fmt_num(tot_output), _yellow),
        (_fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
        ("", None),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 0:
            return max(content + 2, 12)
        if i == 1:
            return max(content + 2, 15)
        return max(content + 2, 12)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _pad_cell(text: str, w: int, align: str) -> str:
        if align == "right":
            return text.rjust(w)
        return text.ljust(w)

    if unicode_ok:
        TL, TR, BL, BR = "\u250c", "\u2510", "\u2514", "\u2518"
        H, V = "\u2500", "\u2502"
        T_DOWN, T_UP, T_LEFT, T_RIGHT, CROSS = "\u252c", "\u2534", "\u2524", "\u251c", "\u253c"
        RTL, RTR, RBL, RBR = "\u256d", "\u256e", "\u2570", "\u256f"
    else:
        TL = TR = BL = BR = "+"
        H, V = "-", "|"
        T_DOWN = T_UP = T_LEFT = T_RIGHT = CROSS = "+"
        RTL = RTR = RBL = RBR = "+"

    def _border_row(left: str, mid: str, right: str) -> str:
        parts = [left]
        for i, w in enumerate(col_widths):
            parts.append(H * (w + 2))
            parts.append(mid if i < num_cols - 1 else right)
        return _dim("".join(parts))

    # Banner — 1-space leading indent + 2-space title padding
    banner_inner_width = max(len(title) + 4, 60)
    left_pad = 2
    right_pad = banner_inner_width - len(title) - left_pad
    indent = " "
    top    = indent + RTL + H * banner_inner_width + RTR
    blank  = indent + V   + " " * banner_inner_width + V
    text_line = indent + V + " " * left_pad + title + " " * right_pad + V
    bottom = indent + RBL + H * banner_inner_width + RBR
    out: list[str] = [_dim(top), _dim(blank), _dim(text_line), _dim(blank), _dim(bottom)]
    out.append("")  # blank line between banner and table

    out.append(_border_row(TL, T_DOWN, TR))

    # Header
    header_cells = [_split_cell(h) for h in headers]
    max_h = max(len(c) for c in header_cells)
    for li in range(max_h):
        parts = [_dim(V)]
        for i, cell in enumerate(header_cells):
            content = cell[li] if li < len(cell) else ""
            parts.append(f" {_cyan(_pad_cell(content, col_widths[i], aligns[i]))} ")
            parts.append(_dim(V))
        out.append("".join(parts))
    out.append(_border_row(T_RIGHT, CROSS, T_LEFT))

    # Data + footer with inter-row separators
    sep = _border_row(T_RIGHT, CROSS, T_LEFT)
    display_rows = list(raw_rows)
    for idx, (cells, rt) in enumerate(display_rows):
        split_cells = [_split_cell(t) for t, _c in cells]
        max_h = max(len(c) for c in split_cells) if split_cells else 1
        for li in range(max_h):
            parts = [_dim(V)]
            for i, (text, cfn) in enumerate(cells):
                content = split_cells[i][li] if li < len(split_cells[i]) else ""
                padded = _pad_cell(content, col_widths[i], aligns[i])
                if cfn is not None:
                    padded = cfn(padded)
                parts.append(f" {padded} ")
                parts.append(_dim(V))
            out.append("".join(parts))
        if idx < len(display_rows) - 1:
            out.append(sep)

    out.append(_border_row(BL, T_UP, BR))
    return "\n".join(out)


def _project_disambiguate_labels(rows: list[dict]) -> dict[int, str]:
    """Return ``{row_index: disambiguated_label}`` for project rows whose
    bare ``display_key`` collides with another row's basename.

    When two projects share a basename (e.g., two ``app`` directories under
    different parents), suffix the colliding rows with the parent-directory
    segment ("(work)" / "(personal)") so they remain visually and
    semantically distinct. Prefer ``key.git_root`` as the disambiguation
    source when present; fall back to ``key.bucket_path`` for no-git rows.

    Used by:
      - ``_render_project_table`` (terminal table render).
      - ``_build_project_snapshot`` (share artifact table + chart) — without
        this, two same-basename projects collapse to a single anonymous
        ``project-N`` after scrub, losing rank meaning AND uniqueness.

    Rows that do not collide are absent from the returned dict; callers
    fall back to ``key.display_key`` for those.
    """
    display_counts: dict[str, int] = {}
    for r in rows:
        dk = r["key"].display_key
        display_counts[dk] = display_counts.get(dk, 0) + 1
    augmented: dict[int, str] = {}
    for idx, r in enumerate(rows):
        if display_counts[r["key"].display_key] > 1:
            source_path = r["key"].git_root or r["key"].bucket_path
            if source_path:
                parent = (
                    os.path.basename(os.path.dirname(source_path)) or "/"
                )
                augmented[idx] = f"{r['key'].display_key} ({parent})"
    return augmented


def _render_project_table(
    rows: list[dict],
    *,
    title: str,
    breakdown: bool = False,
    weeks_missing_snapshot: int = 0,
    weeks_in_range: int = 1,
    no_color: bool = False,
) -> str:
    """Render project rollup as a ccusage-style ANSI table.

    Columns: Project | Sessions | First Seen | Last Seen | Input |
             Cache Create | Cache Read | Output | Cost (USD) | Used % | $/1%

    Parent rows show all columns; breakdown child rows show per-model
    aggregates with blank Sessions/Used%/$/1% cells (those only make
    sense at the project level). Structural clone of
    `_render_claude_session_table` — same two-pass layout (plain cells
    first for width calc, ANSI applied at render time) and same banner /
    border / separator glyphs.
    """
    color = False if no_color else _supports_color_stdout()
    unicode_ok = _supports_unicode_stdout()

    def _dim(s: str) -> str:  return _style_ansi(s, "90", color)
    def _cyan(s: str) -> str: return _style_ansi(s, "36", color)
    def _gray(s: str) -> str: return _style_ansi(s, "90", color)
    def _green(s: str) -> str: return _style_ansi(s, "32", color)
    def _yellow(s: str) -> str: return _style_ansi(s, "33", color)
    def _red(s: str) -> str: return _style_ansi(s, "31", color)

    headers = [
        "Project", "Sessions", "First Seen", "Last Seen",
        "Input", "Cache Create", "Cache Read", "Output",
        "Cost (USD)", "Used %", "$/1%",
    ]
    aligns = [
        "left", "right", "left", "left",
        "right", "right", "right", "right",
        "right", "right", "right",
    ]
    num_cols = len(headers)

    if not rows:
        return ""

    def _to_display_tz(ts: dt.datetime) -> dt.datetime:
        # internal fallback: host-local intentional (AM/PM render via attribute access)
        return ts.astimezone()

    def _date_cell(ts: dt.datetime) -> str:
        local = _to_display_tz(ts)
        hour_12 = local.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        ampm = "a.m." if local.hour < 12 else "p.m."
        return f"{local.year}-{local.month:02d}-{local.day:02d}\n{hour_12}:{local.minute:02d}\n{ampm}"

    # Basename-collision disambiguation: hoisted to a module-level helper
    # so the share-snapshot builder can reuse the same logic (without it,
    # two same-basename projects collapse to a single anonymous `project-N`
    # after scrub, breaking both privacy uniqueness AND chart rank meaning).
    augmented = _project_disambiguate_labels(rows)

    def _project_cell(idx: int, r: dict) -> tuple[str, Any]:
        """Return (plain_text, color_fn_or_None) for the Project cell.

        `color_fn_or_None` is applied to the padded cell in Pass 2 so it
        doesn't perturb column-width math.
        """
        k = r["key"]
        if k.is_unknown:
            return ("(unknown)", _gray)
        base = augmented.get(idx, k.display_key)
        if k.is_no_git:
            # Append a dimmed `(no-git)` marker. The dim style is applied
            # to the whole cell at render time; keeping the plain text
            # unified here gives a clean width calc.
            return (f"{base} (no-git)", _gray)
        return (base, None)

    def _used_pct_color(pct: float) -> Any:
        if pct < 10:
            return _green
        if pct < 25:
            return _yellow
        return _red

    def _used_pct_cell(ap: float | None) -> tuple[str, Any]:
        if ap is None:
            return ("\u2014", _gray)  # em-dash for unknown
        base = f"{ap:.1f}%"
        if weeks_in_range > 1:
            # Count weeks the user asked about; surface via `(Nwk)` suffix
            # (spec §3). Keep the suffix short so column width stays sane.
            base = f"{base} ({weeks_in_range}wk)"
        return (base, _used_pct_color(ap))

    def _cost_per_pct_cell(cpp: float | None) -> tuple[str, Any]:
        if cpp is None or cpp <= 0:
            return ("\u2014", _gray)
        return (f"${cpp:.2f}", None)

    arrow = "  \u2514\u2500" if unicode_ok else "  |_"

    ROW_DATA, ROW_BREAKDOWN = "data", "breakdown"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for idx, r in enumerate(rows):
        proj_text, proj_cfn = _project_cell(idx, r)
        used_text, used_cfn = _used_pct_cell(r.get("attributed_pct"))
        cpp_text, cpp_cfn = _cost_per_pct_cell(r.get("cost_per_pct"))
        data_cells = [
            (proj_text, proj_cfn),
            (str(len(r["sessions"])), None),
            (_date_cell(r["first_seen"]), None),
            (_date_cell(r["last_seen"]), None),
            (_fmt_num(r["input"]), None),
            (_fmt_num(r["cache_write"]), None),
            (_fmt_num(r["cache_read"]), None),
            (_fmt_num(r["output"]), None),
            (f"${r['cost_usd']:.2f}", None),
            (used_text, used_cfn),
            (cpp_text, cpp_cfn),
        ]
        raw_rows.append((data_cells, ROW_DATA))

        if breakdown:
            for model_name, mb in sorted(r["models"].items()):
                short = _short_model_name(model_name)
                bd_cells = [
                    (f"{arrow} {short}", _gray),
                    ("", None),
                    (_date_cell(mb["first_seen"]), _gray),
                    (_date_cell(mb["last_seen"]), _gray),
                    (_fmt_num(mb["input"]), _gray),
                    (_fmt_num(mb["cache_write"]), _gray),
                    (_fmt_num(mb["cache_read"]), _gray),
                    (_fmt_num(mb["output"]), _gray),
                    (f"${mb['cost_usd']:.2f}", _gray),
                    ("", None),
                    ("", None),
                ]
                raw_rows.append((bd_cells, ROW_BREAKDOWN))

    def _max_line_width(s: str) -> int:
        if not s:
            return 0
        return max(len(line) for line in s.split("\n"))

    content_widths = [len(h) for h in headers]
    for cells, _rt in raw_rows:
        for i, (text, _c) in enumerate(cells):
            content_widths[i] = max(content_widths[i], _max_line_width(text))

    def _wide_width(i: int, content: int) -> int:
        if aligns[i] == "right":
            return max(content + 3, 11)
        if i == 0:
            return max(content + 2, 14)  # Project column
        # Date columns (First Seen / Last Seen)
        return max(content + 2, 12)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    if sum(col_widths) + border_overhead > term_width:
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0
        col_widths = [max(int(w * scale), 8) for w in col_widths]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[0] += remainder  # grow Project column

    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    def _pad_cell(text: str, w: int, align: str) -> str:
        if align == "right":
            return text.rjust(w)
        return text.ljust(w)

    if unicode_ok:
        TL, TR, BL, BR = "\u250c", "\u2510", "\u2514", "\u2518"
        H, V = "\u2500", "\u2502"
        T_DOWN, T_UP, T_LEFT, T_RIGHT, CROSS = "\u252c", "\u2534", "\u2524", "\u251c", "\u253c"
        RTL, RTR, RBL, RBR = "\u256d", "\u256e", "\u2570", "\u256f"
    else:
        TL = TR = BL = BR = "+"
        H, V = "-", "|"
        T_DOWN = T_UP = T_LEFT = T_RIGHT = CROSS = "+"
        RTL = RTR = RBL = RBR = "+"

    def _border_row(left: str, mid: str, right: str) -> str:
        parts = [left]
        for i, w in enumerate(col_widths):
            parts.append(H * (w + 2))
            parts.append(mid if i < num_cols - 1 else right)
        return _dim("".join(parts))

    banner_inner_width = max(len(title) + 4, 60)
    left_pad = 2
    right_pad = banner_inner_width - len(title) - left_pad
    indent = " "
    top    = indent + RTL + H * banner_inner_width + RTR
    blank  = indent + V   + " " * banner_inner_width + V
    text_line = indent + V + " " * left_pad + title + " " * right_pad + V
    bottom = indent + RBL + H * banner_inner_width + RBR
    out: list[str] = [_dim(top), _dim(blank), _dim(text_line), _dim(blank), _dim(bottom)]
    out.append("")

    out.append(_border_row(TL, T_DOWN, TR))

    header_cells = [_split_cell(h) for h in headers]
    max_h = max(len(c) for c in header_cells)
    for li in range(max_h):
        parts = [_dim(V)]
        for i, cell in enumerate(header_cells):
            content = cell[li] if li < len(cell) else ""
            parts.append(f" {_cyan(_pad_cell(content, col_widths[i], aligns[i]))} ")
            parts.append(_dim(V))
        out.append("".join(parts))
    out.append(_border_row(T_RIGHT, CROSS, T_LEFT))

    sep = _border_row(T_RIGHT, CROSS, T_LEFT)
    display_rows = list(raw_rows)
    for idx, (cells, rt) in enumerate(display_rows):
        split_cells = [_split_cell(t) for t, _c in cells]
        max_h = max(len(c) for c in split_cells) if split_cells else 1
        for li in range(max_h):
            parts = [_dim(V)]
            for i, (text, cfn) in enumerate(cells):
                content = split_cells[i][li] if li < len(split_cells[i]) else ""
                w = col_widths[i]
                if len(content) > w:
                    ell = "\u2026" if unicode_ok else "..."
                    content = content[: max(0, w - len(ell))] + ell
                padded = _pad_cell(content, w, aligns[i])
                if cfn is not None:
                    padded = cfn(padded)
                parts.append(f" {padded} ")
                parts.append(_dim(V))
            out.append("".join(parts))
        if idx < len(display_rows) - 1:
            out.append(sep)

    out.append(_border_row(BL, T_UP, BR))

    if weeks_missing_snapshot > 0:
        plural = "s" if weeks_missing_snapshot != 1 else ""
        out.append(
            _dim(
                f"Note: Used % unavailable for {weeks_missing_snapshot} "
                f"week{plural} \u2014 no usage snapshots recorded."
            )
        )

    return "\n".join(out)


def _five_hour_blocks_to_json(
    block_dicts: list[dict],
    since_iso: str | None,
    until_iso: str | None,
    cap: int | None,
    truncated: bool,
    breakdown_axis: str | None,
) -> dict:
    """Build the camelCase JSON envelope for ``cmd_five_hour_blocks``.

    Stable schema; the ``window`` object lets consumers detect default-cap
    truncation. Only one of ``modelBreakdowns`` / ``projectBreakdowns`` is
    present per block (per the requested ``--breakdown`` axis); both are
    omitted when ``--breakdown`` is unset.
    """
    blocks_out = []
    for d in block_dicts:
        crossed = bool(d.get("crossed_seven_day_reset"))
        p_start = d.get("seven_day_pct_at_block_start")
        p_end = d.get("seven_day_pct_at_block_end")
        delta = (
            None if (crossed or p_start is None or p_end is None)
            else round(p_end - p_start, 9)
        )
        pct = d["final_five_hour_percent"]
        cost = d["total_cost_usd"]
        dollar_per_pct = (
            round(cost / pct, 9) if pct >= 0.5 else None
        )
        out = {
            "blockStartAt":            d["block_start_at"],
            "fiveHourWindowKey":       d["five_hour_window_key"],
            "fiveHourResetsAt":        d["five_hour_resets_at"],
            "lastObservedAtUtc":       d["last_observed_at_utc"],
            "status":                  "active" if d["__is_active"] else "closed",
            "finalFiveHourPercent":    round(pct, 1),
            "totalCost":               round(cost, 9),
            "dollarsPerPercent":       dollar_per_pct,
            "inputTokens":             d["total_input_tokens"],
            "outputTokens":            d["total_output_tokens"],
            "cacheCreationTokens":     d["total_cache_create_tokens"],
            "cacheReadTokens":         d["total_cache_read_tokens"],
            "sevenDayPctAtBlockStart": p_start,
            "sevenDayPctAtBlockEnd":   p_end,
            "sevenDayPctDeltaPp":      delta,
            "crossedSevenDayReset":    crossed,
        }
        if breakdown_axis == "model":
            out["modelBreakdowns"] = [
                {
                    "modelName":           r["model"],
                    "inputTokens":         r["input_tokens"],
                    "outputTokens":        r["output_tokens"],
                    "cacheCreationTokens": r["cache_create_tokens"],
                    "cacheReadTokens":     r["cache_read_tokens"],
                    "cost":                round(r["cost_usd"], 9),
                    "entryCount":          r["entry_count"],
                }
                for r in d.get("__breakdown_rows", [])
            ]
        elif breakdown_axis == "project":
            out["projectBreakdowns"] = [
                {
                    "projectPath":         r["project_path"],
                    "inputTokens":         r["input_tokens"],
                    "outputTokens":        r["output_tokens"],
                    "cacheCreationTokens": r["cache_create_tokens"],
                    "cacheReadTokens":     r["cache_read_tokens"],
                    "cost":                round(r["cost_usd"], 9),
                    "entryCount":          r["entry_count"],
                }
                for r in d.get("__breakdown_rows", [])
            ]
        blocks_out.append(out)

    return {
        "schemaVersion": 1,
        "window": {
            "since":     since_iso,
            "until":     until_iso,
            "limit":     cap,
            "order":     "desc",
            "count":     len(blocks_out),
            "truncated": truncated,
        },
        "blocks": blocks_out,
    }


def _render_five_hour_blocks_table(
    block_dicts: list[dict], args: argparse.Namespace,
) -> None:
    """Render the human-readable boxed table for ``cmd_five_hour_blocks``.

    7-column layout: Block Start · Status · 5h % · Cost · $/1% · 7d % range
    · Δ7d. Crossed-reset rows are marked with a ``⚡ `` prefix on the Block
    Start cell (mirroring the ``~`` heuristic-anchor convention used by
    ``cctally blocks``). Footer summarizes block count + total cost; the
    ⚡ legend appears when at least one row crossed the weekly reset.
    """
    if not block_dicts:
        print("No 5h blocks recorded.")
        return
    headers = ["Block Start", "Status", "5h %", "Cost", "$/1%",
               "7d % range", "Δ7d"]
    aligns = ["left", "left", "right", "right", "right",
              "left", "right"]
    rows: list[list[str]] = []
    total_cost = 0.0
    has_crossed = False
    for d in block_dicts:
        crossed = bool(d.get("crossed_seven_day_reset"))
        has_crossed = has_crossed or crossed
        p_start = d.get("seven_day_pct_at_block_start")
        p_end = d.get("seven_day_pct_at_block_end")
        delta = (
            None if (crossed or p_start is None or p_end is None)
            else (p_end - p_start)
        )
        pct = d["final_five_hour_percent"]
        cost = d["total_cost_usd"]
        total_cost += cost
        dpp = (cost / pct) if pct >= 0.5 else None

        formatted_start = _format_block_start(d["block_start_at"], args._resolved_tz)
        if crossed:
            formatted_start = f"⚡ {formatted_start}"
        rows.append([
            formatted_start,
            "ACTIVE" if d["__is_active"] else "closed",
            f"{pct:.1f}%",
            f"${cost:.2f}",
            ("—" if dpp is None else f"${dpp:.2f}"),
            (
                f"{p_start:.1f}→{p_end:.1f}"
                if p_start is not None and p_end is not None
                else (f"—→{p_end:.1f}" if p_end is not None else "—")
            ),
            ("—" if delta is None else f"{delta:+.1f}"),
        ])
        # Breakdown child rows.
        for child in d.get("__breakdown_rows", []):
            label = (
                child.get("model") if args.breakdown == "model"
                else child.get("project_path")
            )
            rows.append([
                f"  └ {label}",
                "",
                "",
                f"${child['cost_usd']:.2f}",
                "", "", "",
            ])

    print(_boxed_table(headers, rows, aligns))
    glyph = " · ⚡ = block crossed weekly reset" if has_crossed else ""
    print(f"\n{len(block_dicts)} blocks · cost: ${total_cost:.2f}{glyph}")

