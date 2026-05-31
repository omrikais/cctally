"""CLI glue for cctally cache-report: render + window resolution + IO
aggregation wrappers + the ``cache-report`` command. The pure data kernel
(bucketing, financial computation, anomaly classification) lives in
``bin/_lib_cache_report.py`` and is shared with the dashboard sync builder;
this module is CLI-only.

Accessor discipline (spec §2): ns-patchable / STAYS helpers are reached via
the call-time ``c = _cctally()`` accessor; ``_cctally_core`` kernel symbols are
honest-imported; the data kernel is reached as ``crk.<name>``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _cctally_core import eprint, now_utc_iso, parse_iso_datetime, _command_as_of
import _lib_cache_report as crk


def _cctally():
    """Call-time accessor to the cctally module namespace (ns-patchable)."""
    import sys
    return sys.modules["cctally"]


def _layout_cache_table(
    headers: list[str],
    aligns: list[str],
    raw_rows: list[tuple[list[tuple[str, Any]], str]],
    title: str,
    color: bool,
    unicode_ok: bool,
    *,
    expand_col_index: int,
    numeric_col_indices: tuple[int, ...],
    date_col_index: int | None,
    wide_text_min: int = 15,
    narrow_text_min: int = 12,
    droppable_col_index: int | None = None,
    compact: bool = False,
) -> str:
    """Shared responsive-width table layout for cache-report renderers.

    Takes fully-built raw_rows (list of (cells, row_type) where cells is a
    list of (text, color_fn|None)) plus column metadata and returns the
    rendered table as a newline-joined string. Applies wide/narrow
    responsive widths, multi-line cell splitting, cyan headers, row
    separators, and a banner title.

    - `expand_col_index`: the identity column that absorbs remainder width
      in compact mode (e.g., 1 = Models in daily, 2 = Project in session).
    - `numeric_col_indices`: column indices that get _truncate_num treatment
      in compact mode.
    - `date_col_index`: column index whose YYYY-MM-DD cells split across
      two lines in compact mode (None disables the split; used by daily's
      "Date" column).
    - `wide_text_min`/`narrow_text_min`: minimum widths for the expandable
      text column in wide/compact modes respectively.
    - `droppable_col_index`: column to drop entirely when the sum of
      narrow-mode minimums still exceeds the terminal width. Used as an
      escape hatch so 120-col terminals can render the full-dollar cache
      tables without overflow. Must not equal `expand_col_index`.
    """
    c = _cctally()
    num_cols = len(headers)

    # Ultra-compact fallback: if even the narrow-mode minimums won't fit
    # this terminal, drop the designated "low-value" column entirely. This
    # preserves table integrity instead of silently overflowing.
    def _narrow_min_floor(i: int) -> int:
        if aligns[i] == "right":
            return 7
        if i == expand_col_index:
            return narrow_text_min
        if date_col_index is not None and i == date_col_index:
            return 10
        return 8

    try:
        _term_width_probe = os.get_terminal_size().columns
    except (OSError, ValueError):
        _term_width_probe = int(os.environ.get("COLUMNS", "120"))
    _border_overhead_probe = 3 * num_cols + 1
    _min_total = sum(_narrow_min_floor(i) for i in range(num_cols)) + _border_overhead_probe
    if (
        droppable_col_index is not None
        and droppable_col_index != expand_col_index
        and _min_total > _term_width_probe
    ):
        d = droppable_col_index
        headers = headers[:d] + headers[d + 1:]
        aligns = aligns[:d] + aligns[d + 1:]
        raw_rows = [
            (cells[:d] + cells[d + 1:], rt)
            for cells, rt in raw_rows
        ]
        if expand_col_index > d:
            expand_col_index -= 1
        numeric_col_indices = tuple(
            (i - 1 if i > d else i)
            for i in numeric_col_indices
            if i != d
        )
        if date_col_index is not None:
            if date_col_index == d:
                date_col_index = None
            elif date_col_index > d:
                date_col_index -= 1
        num_cols = len(headers)

    def _dim(s: str) -> str:
        return c._style_ansi(s, "90", color)

    def _cyan(s: str) -> str:
        return c._style_ansi(s, "36", color)

    def _bold(s: str) -> str:
        return c._style_ansi(s, "1", color)

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
        if i == expand_col_index:
            return max(content + 2, wide_text_min)
        return max(content + 2, 10)

    col_widths = [_wide_width(i, content_widths[i]) for i in range(num_cols)]

    try:
        term_width = os.get_terminal_size().columns
    except (OSError, ValueError):
        term_width = int(os.environ.get("COLUMNS", "120"))

    border_overhead = 3 * num_cols + 1
    # Issue #91 (Shape A): the ``compact`` kwarg forces the responsive
    # scale-down path regardless of terminal width, mirroring
    # ``_render_project_table`` / ``_render_bucket_table``. Auto-detected
    # width-overflow continues to trigger the same path as before.
    compact_mode = compact or (sum(col_widths) + border_overhead > term_width)

    if compact_mode:
        available = term_width - border_overhead
        total_col = sum(col_widths)
        scale = available / total_col if total_col > 0 else 1.0

        def _narrow_min(i: int) -> int:
            if aligns[i] == "right":
                # 7 is enough for "100.0%", "$99.99", "+$9.99", and for
                # larger values _truncate_num (via numeric_col_indices)
                # adds an ellipsis tail instead of overflowing the cell.
                return 7
            if i == expand_col_index:
                return narrow_text_min
            if date_col_index is not None and i == date_col_index:
                return 10
            return 8

        col_widths = [
            max(int(w * scale), _narrow_min(i))
            for i, w in enumerate(col_widths)
        ]
        remainder = available - sum(col_widths)
        if remainder > 0:
            col_widths[expand_col_index] += remainder
        else:
            # Scaled widths still exceed available. Trim the widest
            # non-expand column by 1 each round; once those all hit their
            # floor, trim the expand col down to its floor too. Identity
            # cells that exceed their (shrunken) column get `_truncate_num`
            # ellipsis treatment via the render path.
            while remainder < 0:
                trim_i = -1
                trim_w = 0
                for i in range(num_cols):
                    if i == expand_col_index:
                        continue
                    if col_widths[i] <= _narrow_min(i):
                        continue
                    if col_widths[i] > trim_w:
                        trim_w = col_widths[i]
                        trim_i = i
                if trim_i < 0:
                    # No non-expand col can shrink further. Fall back to
                    # shrinking the expand col (down to its own floor).
                    if col_widths[expand_col_index] > _narrow_min(expand_col_index):
                        trim_i = expand_col_index
                    else:
                        break
                col_widths[trim_i] -= 1
                remainder += 1

    if compact_mode:
        header_display = [h.replace(" ", "\n") for h in headers]
    else:
        header_display = headers[:]

    def _split_cell(text: str) -> list[str]:
        return text.split("\n") if text else [""]

    # Detect glyph-prefixed dates (anomaly-flagged rows). `_split_date_if_compact`
    # uses a strict ^YYYY-MM-DD$ regex that won't match "⚠︎ YYYY-MM-DD" (or
    # the ASCII "! " fallback), so flagged rows would skip the split while
    # unflagged rows got it — producing inconsistent row heights in compact
    # mode. If ANY data row in this table is flagged, skip the date-split for
    # the whole table so every row renders on a single line with uniform height.
    any_anomaly_row = False
    if date_col_index is not None:
        for cells, row_type in raw_rows:
            if row_type != "data":
                continue
            cell_text = cells[date_col_index][0]
            stripped = re.sub(r"\033\[[0-9;]*m", "", cell_text)
            if stripped.startswith("⚠") or stripped.startswith("!"):
                any_anomaly_row = True
                break

    def _split_date_if_compact(text: str) -> str:
        if (
            compact_mode
            and date_col_index is not None
            and not any_anomaly_row
            and re.match(r"^\d{4}-\d{2}-\d{2}$", text)
        ):
            y, mm, dd = text.split("-")
            return f"{y}\n{mm}-{dd}"
        return text

    display_rows: list[tuple[list[list[tuple[str, Any]]], str]] = []
    for cells, row_type in raw_rows:
        processed: list[tuple[str, Any]] = []
        for i, (text, cfn) in enumerate(cells):
            t = (
                _split_date_if_compact(text)
                if date_col_index is not None and i == date_col_index
                else text
            )
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

    header_line_counts = [len(_split_cell(h)) for h in header_display]
    header_n_lines = max(header_line_counts) if header_line_counts else 1
    header_lines: list[list[str]] = []
    for li in range(header_n_lines):
        line = []
        for h in header_display:
            parts = _split_cell(h)
            line.append(parts[li] if li < len(parts) else "")
        header_lines.append(line)

    if unicode_ok:
        ch = {
            "tl": "┌", "tm": "┬", "tr": "┐",
            "ml": "├", "mm": "┼", "mr": "┤",
            "bl": "└", "bm": "┴", "br": "┘",
            "h": "─", "v": "│",
        }
    else:
        ch = {k: v for k, v in zip(
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

    lines: list[str] = []
    lines.append("")
    title_padded = f"  {title}  "
    tw = len(title_padded)
    dash = "─" if unicode_ok else "-"
    vb = "│" if unicode_ok else "|"
    if unicode_ok:
        banner_top = f" ╭{dash * tw}╮"
        banner_bot = f" ╰{dash * tw}╯"
    else:
        banner_top = f" +{'-' * tw}+"
        banner_bot = f" +{'-' * tw}+"
    lines.append(banner_top)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(f" {vb}" + _bold(title_padded) + vb)
    lines.append(f" {vb}" + " " * tw + vb)
    lines.append(banner_bot)
    lines.append("")

    lines.append(hline(ch["tl"], ch["tm"], ch["tr"]))
    for line_cells in header_lines:
        if compact_mode:
            line_cells = [
                c._truncate_num(cc, col_widths[i]) if len(cc) > col_widths[i] else cc
                for i, cc in enumerate(line_cells)
            ]
        lines.append(make_row([_cyan(cc) for cc in line_cells]))
    lines.append(hline(ch["ml"], ch["mm"], ch["mr"]))

    def _render_display_row(row_lines: list[list[tuple[str, Any]]]) -> None:
        for line_cells in row_lines:
            rendered: list[str] = []
            for ci, (text, cfn) in enumerate(line_cells):
                out = text
                if compact_mode and out:
                    if ci in numeric_col_indices:
                        out = c._truncate_num(out, col_widths[ci])
                    elif len(c._ANSI_ESC_RE.sub("", out)) > col_widths[ci]:
                        # Left-aligned content wider than its column — truncate
                        # with ellipsis. Uses `_truncate_display` so anomaly
                        # rows (which inject a red-styled glyph into cell 0)
                        # aren't sliced mid-escape-sequence, which would
                        # bleed color into adjacent cells.
                        out = c._truncate_display(out, col_widths[ci])
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


def _render_cache_day_rows(
    rows: list["crk.CacheRow"], title: str, *, compact: bool = False,
) -> str:
    """Render daily-mode cache report.

    Columns: Date, Models, Cache %, Input, Cache Create, Cache Read,
    Total Tokens, Cost (USD), $ Saved, $ Wasted, Net $.
    """
    c = _cctally()
    color = c._supports_color_stdout()
    unicode_ok = c._supports_unicode_stdout()

    def _yellow(s: str) -> str:
        return c._style_ansi(s, "33", color)

    def _gray(s: str) -> str:
        return c._style_ansi(s, "90", color)

    def _red(s: str) -> str:
        return c._style_ansi(s, "31", color)

    # U+FE0E (text-presentation variation selector) forces single-cell
    # text rendering of U+26A0; without it, emoji-capable terminals
    # (macOS Terminal, iTerm2) may render the glyph 2 cells wide and
    # shift subsequent cells 1 column to the right on flagged rows.
    anomaly_glyph = "⚠︎" if unicode_ok else "!"

    headers = [
        "Date", "Models", "Cache %", "Input",
        "Cache Create", "Cache Read", "Total Tokens", "Cost (USD)",
        "$ Saved", "$ Wasted", "Net $",
    ]
    aligns = [
        "left", "left", "right", "right", "right", "right", "right", "right",
        "right", "right", "right",
    ]

    arrow = "  └─" if unicode_ok else "  |_"

    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for row in rows:
        short_models = sorted({c._short_model_name(mb.model_name) for mb in row.model_breakdowns})
        models_text = "\n".join(f"- {m}" for m in short_models) if short_models else ""
        data_cells = [
            (row.date or "", None),
            (models_text, None),
            (f"{row.cache_hit_percent:.1f}%", None),
            (c._fmt_num(row.input_tokens), None),
            (c._fmt_num(row.cache_creation_tokens), None),
            (c._fmt_num(row.cache_read_tokens), None),
            (c._fmt_num(row.total_tokens), None),
            (f"${row.cost:.2f}", None),
            (f"${row.saved_usd:.2f}", None),
            (f"${row.wasted_usd:.2f}", None),
            (f"${row.net_usd:+.2f}", None),
        ]
        # Anomaly visual treatment (data rows only — never breakdown/footer).
        # Cell-index map (daily): 0=Date, 1=Models, 2=Cache%, 3=Input, 4=CC,
        # 5=CR, 6=Total, 7=Cost, 8=Saved, 9=Wasted, 10=Net $.
        if row.anomaly_triggered:
            first_text, first_style = data_cells[0]
            data_cells[0] = (
                f"{_red(anomaly_glyph)} {first_text}",
                first_style,
            )
            if "cache_drop" in row.anomaly_reasons:
                txt, _cfn = data_cells[2]
                data_cells[2] = (txt, _red)
            if "net_negative" in row.anomaly_reasons:
                txt, _cfn = data_cells[10]
                data_cells[10] = (txt, _red)
        raw_rows.append((data_cells, ROW_DATA))

        for mb in row.model_breakdowns:
            short = c._short_model_name(mb.model_name)
            mb_input = int(mb.input_tokens)
            mb_output = int(mb.output_tokens)
            mb_cc = int(mb.cache_creation_tokens)
            mb_cr = int(mb.cache_read_tokens)
            mb_total = mb_input + mb_output + mb_cc + mb_cr
            mb_cost = float(mb.cost)
            mb_hit = float(mb.cache_hit_percent)
            bd_cells = [
                (f"{arrow} {short}", _gray),
                ("", None),
                (f"{mb_hit:.1f}%", _gray),
                (c._fmt_num(mb_input), _gray),
                (c._fmt_num(mb_cc), _gray),
                (c._fmt_num(mb_cr), _gray),
                (c._fmt_num(mb_total), _gray),
                (f"${mb_cost:.2f}", _gray),
                (f"${mb.saved_usd:.2f}", _gray),
                (f"${mb.wasted_usd:.2f}", _gray),
                (f"${mb.net_usd:+.2f}", _gray),
            ]
            raw_rows.append((bd_cells, ROW_BREAKDOWN))

    tot_inp = sum(row.input_tokens for row in rows)
    tot_out = sum(row.output_tokens for row in rows)
    tot_cc = sum(row.cache_creation_tokens for row in rows)
    tot_cr = sum(row.cache_read_tokens for row in rows)
    tot_tokens = sum(row.total_tokens for row in rows)
    tot_cost = sum(row.cost for row in rows)
    tot_saved = sum(row.saved_usd for row in rows)
    tot_wasted = sum(row.wasted_usd for row in rows)
    tot_net = sum(row.net_usd for row in rows)
    tot_hit = crk._compute_cache_hit_percent(tot_inp, tot_cc, tot_cr)
    footer_cells = [
        ("Total", _yellow),
        ("", None),
        (f"{tot_hit:.1f}%", _yellow),
        (c._fmt_num(tot_inp), _yellow),
        (c._fmt_num(tot_cc), _yellow),
        (c._fmt_num(tot_cr), _yellow),
        (c._fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
        (f"${tot_saved:.2f}", _yellow),
        (f"${tot_wasted:.2f}", _yellow),
        (f"${tot_net:+.2f}", _yellow),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    return _layout_cache_table(
        headers, aligns, raw_rows, title, color, unicode_ok,
        expand_col_index=1,                            # Models column
        numeric_col_indices=(2, 3, 4, 5, 6, 7, 8, 9, 10),  # %, 5x tokens, 3x $
        date_col_index=0,                              # Date column
        wide_text_min=15,
        narrow_text_min=12,
        droppable_col_index=3,                         # Input column
        compact=compact,
    )


def _render_cache_session_rows(
    rows: list["crk.CacheRow"], title: str,
    *, tz: "ZoneInfo | None" = None, compact: bool = False,
) -> str:
    """Render session-mode cache report.

    Columns: SessionId, Last Activity, Project, Cache %, Input,
    Cache Create, Cache Read, Total Tokens, Cost (USD), $ Saved,
    $ Wasted, Net $.

    ``tz`` is the resolved display zone (None = host local). Last-Activity
    cells are rendered in this zone.
    """
    c = _cctally()
    color = c._supports_color_stdout()
    unicode_ok = c._supports_unicode_stdout()

    def _yellow(s: str) -> str:
        return c._style_ansi(s, "33", color)

    def _gray(s: str) -> str:
        return c._style_ansi(s, "90", color)

    def _red(s: str) -> str:
        return c._style_ansi(s, "31", color)

    # U+FE0E (text-presentation variation selector) forces single-cell
    # text rendering of U+26A0; without it, emoji-capable terminals
    # (macOS Terminal, iTerm2) may render the glyph 2 cells wide and
    # shift subsequent cells 1 column to the right on flagged rows.
    anomaly_glyph = "⚠︎" if unicode_ok else "!"

    headers = [
        "SessionId", "Last Activity", "Project",
        "Cache %", "Input", "Cache Create", "Cache Read",
        "Total Tokens", "Cost (USD)", "$ Saved", "$ Wasted", "Net $",
    ]
    aligns = [
        "left", "left", "left",
        "right", "right", "right", "right",
        "right", "right", "right", "right", "right",
    ]

    arrow = "  └─" if unicode_ok else "  |_"

    ROW_DATA, ROW_BREAKDOWN, ROW_FOOTER = "data", "breakdown", "footer"
    raw_rows: list[tuple[list[tuple[str, Any]], str]] = []

    for row in rows:
        sid_short = (row.session_id or "")[:8]
        last_act = (
            c.format_display_dt(row.last_activity, tz, fmt="%Y-%m-%d %H:%M", suffix=True)
            if row.last_activity else ""
        )
        project_short = ""
        if row.project_path:
            project_short = os.path.basename(row.project_path.rstrip("/"))

        data_cells = [
            (sid_short, None),
            (last_act, None),
            (project_short, None),
            (f"{row.cache_hit_percent:.1f}%", None),
            (c._fmt_num(row.input_tokens), None),
            (c._fmt_num(row.cache_creation_tokens), None),
            (c._fmt_num(row.cache_read_tokens), None),
            (c._fmt_num(row.total_tokens), None),
            (f"${row.cost:.2f}", None),
            (f"${row.saved_usd:.2f}", None),
            (f"${row.wasted_usd:.2f}", None),
            (f"${row.net_usd:+.2f}", None),
        ]
        # Anomaly visual treatment (data rows only — never breakdown/footer).
        # Cell-index map (session): 0=SessionId, 1=Last Activity, 2=Project,
        # 3=Cache%, 4=Input, 5=CC, 6=CR, 7=Total, 8=Cost, 9=Saved, 10=Wasted,
        # 11=Net $.
        if row.anomaly_triggered:
            first_text, first_style = data_cells[0]
            data_cells[0] = (
                f"{_red(anomaly_glyph)} {first_text}",
                first_style,
            )
            if "cache_drop" in row.anomaly_reasons:
                txt, _cfn = data_cells[3]
                data_cells[3] = (txt, _red)
            if "net_negative" in row.anomaly_reasons:
                txt, _cfn = data_cells[11]
                data_cells[11] = (txt, _red)
        raw_rows.append((data_cells, ROW_DATA))

        for mb in row.model_breakdowns:
            short = c._short_model_name(mb.model_name)
            mb_total = (
                mb.input_tokens + mb.output_tokens
                + mb.cache_creation_tokens + mb.cache_read_tokens
            )
            bd_cells = [
                (f"{arrow} {short}", _gray),
                ("", None),
                ("", None),
                (f"{mb.cache_hit_percent:.1f}%", _gray),
                (c._fmt_num(mb.input_tokens), _gray),
                (c._fmt_num(mb.cache_creation_tokens), _gray),
                (c._fmt_num(mb.cache_read_tokens), _gray),
                (c._fmt_num(mb_total), _gray),
                (f"${mb.cost:.2f}", _gray),
                (f"${mb.saved_usd:.2f}", _gray),
                (f"${mb.wasted_usd:.2f}", _gray),
                (f"${mb.net_usd:+.2f}", _gray),
            ]
            raw_rows.append((bd_cells, ROW_BREAKDOWN))

    tot_inp = sum(r.input_tokens for r in rows)
    tot_out = sum(r.output_tokens for r in rows)
    tot_cc = sum(r.cache_creation_tokens for r in rows)
    tot_cr = sum(r.cache_read_tokens for r in rows)
    tot_tokens = sum(r.total_tokens for r in rows)
    tot_cost = sum(r.cost for r in rows)
    tot_saved = sum(r.saved_usd for r in rows)
    tot_wasted = sum(r.wasted_usd for r in rows)
    tot_net = sum(r.net_usd for r in rows)
    tot_hit = crk._compute_cache_hit_percent(tot_inp, tot_cc, tot_cr)

    footer_cells = [
        ("Total", _yellow),
        (f"({len(rows)} sessions)", _yellow),
        ("", None),
        (f"{tot_hit:.1f}%", _yellow),
        (c._fmt_num(tot_inp), _yellow),
        (c._fmt_num(tot_cc), _yellow),
        (c._fmt_num(tot_cr), _yellow),
        (c._fmt_num(tot_tokens), _yellow),
        (f"${tot_cost:.2f}", _yellow),
        (f"${tot_saved:.2f}", _yellow),
        (f"${tot_wasted:.2f}", _yellow),
        (f"${tot_net:+.2f}", _yellow),
    ]
    raw_rows.append((footer_cells, ROW_FOOTER))

    return _layout_cache_table(
        headers, aligns, raw_rows, title, color, unicode_ok,
        expand_col_index=2,                                    # Project column
        numeric_col_indices=(3, 4, 5, 6, 7, 8, 9, 10, 11),    # %, 5x tokens, 3x $
        date_col_index=None,                                   # no date column
        wide_text_min=18,
        narrow_text_min=12,
        droppable_col_index=4,                                 # Input column
        compact=compact,
    )


def _render_cache_report_table(
    rows: list["crk.CacheRow"],
    title: str,
    *,
    mode: Literal["day", "session"] = "day",
    tz: "ZoneInfo | None" = None,
    compact: bool = False,
) -> str:
    """Dispatcher: routes to daily or session renderer based on mode.

    ``tz`` is the resolved display zone (None = host local). Day-mode
    rows have no clock-instant cells (date strings only) so the parameter
    is currently consumed only by the session-mode renderer.

    ``compact`` (issue #91, Shape A) forces ``_layout_cache_table``'s
    responsive scale-down branch regardless of terminal width.
    """
    if mode == "session":
        return _render_cache_session_rows(rows, title, tz=tz, compact=compact)
    return _render_cache_day_rows(rows, title, compact=compact)


def _compute_entry_cache_dollars(
    model: str,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> tuple[float, float, float]:
    """Compatibility wrapper — pre-extraction signature.

    The kernel function takes ``pricing`` explicitly so it stays pure;
    bin/cctally callers inject the embedded ``CLAUDE_MODEL_PRICING``.
    ``_lookup_pricing`` inside the kernel handles the ``anthropic/`` /
    ``anthropic.`` alias-stripping that the legacy ``_resolve_model_pricing``
    did, but without the stderr warning (the warning is the CLI's concern
    and already fires elsewhere via ``_calculate_entry_cost``).
    """
    c = _cctally()
    return crk._compute_entry_cache_dollars(
        model, cache_creation_tokens, cache_read_tokens,
        pricing=c.CLAUDE_MODEL_PRICING,
        tiered_threshold=c.TIERED_THRESHOLD,
    )


def _aggregate_cache_by_day(
    since: dt.datetime,
    until: dt.datetime,
    project: str | None = None,
    *,
    display_tz: "ZoneInfo | None" = None,
) -> list["crk.CacheRow"]:
    """CLI adapter: pulls entries from ``get_entries`` and delegates to the
    pure-fn kernel ``_lib_cache_report._aggregate_cache_by_day``.

    Adds an explicit ``display_tz`` kwarg (closes the pre-existing minor bug
    where ``--tz`` shifted the window edges but not the day-bucketing —
    spec §1.6, plan A3). Passes the embedded ``CLAUDE_MODEL_PRICING`` +
    ``_calculate_entry_cost`` into the kernel so the kernel itself stays
    free of pricing globals / cost-dispatch I/O.

    Direct callers that don't pass ``display_tz`` (legacy contract) fall
    back to host-local via the kernel's ``None``-tz handling, matching
    pre-extraction behavior byte-for-byte. ``since`` / ``until`` bound
    the I/O query here; the kernel itself trusts the caller's pre-filter.
    """
    c = _cctally()
    entries = list(c.get_entries(since, until, project=project))
    return crk._aggregate_cache_by_day(
        entries,
        display_tz=display_tz,
        pricing=c.CLAUDE_MODEL_PRICING,
        cost_calculator=c._calculate_entry_cost,
    )


def _aggregate_cache_by_session(
    since: dt.datetime,
    until: dt.datetime,
    project: str | None = None,
) -> list["crk.CacheRow"]:
    """CLI adapter: pulls Claude session entries from
    ``get_claude_session_entries`` and delegates to the pure-fn kernel
    ``_lib_cache_report._aggregate_cache_by_session``.

    Preserves the legacy one-shot ``Warning: N entries lacked
    session_files rows (cache may be catching up).`` stderr line by
    consuming the kernel's ``fallback_count`` and calling ``eprint``
    here (kept on the I/O side; kernel stays pure). Injects
    ``CLAUDE_MODEL_PRICING`` + ``_calculate_entry_cost`` +
    ``_decode_escaped_cwd`` so the kernel doesn't reach for cctally
    globals. ``since`` / ``until`` bound the I/O query; the kernel
    itself trusts the caller's pre-filter.
    """
    c = _cctally()
    entries = c.get_claude_session_entries(since, until, project=project)
    if not entries:
        return []

    def _project_decoder(source_path: str) -> str:
        return c._decode_escaped_cwd(
            os.path.basename(os.path.dirname(source_path))
        )

    agg = crk._aggregate_cache_by_session(
        entries,
        pricing=c.CLAUDE_MODEL_PRICING,
        cost_calculator=c._calculate_entry_cost,
        project_decoder=_project_decoder,
    )
    if agg.fallback_count:
        eprint(
            f"Warning: {agg.fallback_count} entries lacked session_files rows "
            "(cache may be catching up)."
        )
    return agg.rows


def _annotate_anomalies(
    rows: list["crk.CacheRow"],
    threshold_pp: int,
    window_days: int,
    *,
    enabled: bool = True,
) -> None:
    """CLI adapter: thin shim around the kernel's ``_classify_anomalies``.

    Kept under the original name so the existing call site in
    ``cmd_cache_report`` resolves unchanged. The kernel mutates each row
    in place (same contract as the pre-extraction implementation —
    ``anomaly_triggered`` / ``anomaly_reasons`` set on each ``CacheRow``).
    """
    crk._classify_anomalies(
        rows,
        threshold_pp=threshold_pp,
        window_days=window_days,
        enabled=enabled,
    )


def _resolve_cache_report_window(
    args: argparse.Namespace,
    *,
    now_utc: dt.datetime | None = None,
    tz_name: str | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve [since, until] from --since / --until / --days.

    Priority:
      - If both --since and --until present: use them verbatim.
      - If only --since: until = now.
      - If only --until: since = until - args.days.
      - If neither: since = today - (args.days - 1) midnight; until = now.

    Date-only args (YYYY-MM-DD or YYYYMMDD, no T/+/Z) are expanded to
    full-day bounds in the resolved display tz when ``tz_name`` is set
    (otherwise host-local tz, the legacy fallback). ``--since`` lands at
    00:00:00.000000; ``--until`` at 23:59:59.999999 — matching cmd_blocks
    and _parse_cli_date_range's inclusive-end-of-day convention. Full-ISO
    args carry their own offset/Z and are tz-independent.

    ``now_utc`` is an optional testing-hook override for "now"; when
    provided (via ``_command_as_of()``) it replaces the wall-clock default
    used to build ``until`` when ``--until`` is absent, and the
    ``--days N`` trailing-window anchor when neither ``--since`` nor
    ``--until`` is supplied. Must be a tz-aware UTC datetime. Omit to
    keep legacy wall-clock behavior.

    ``tz_name`` (typically derived from ``resolve_display_tz``) interprets
    naive date-only ``--since`` / ``--until`` in that IANA zone instead of
    host-local. Mirrors the contract documented in the CLAUDE.md gotcha
    "display.tz controls render; date-bucketing commands also parse
    --since/--until in display tz". Invalid zone raises ValueError (this
    arg is plumbed from a pre-validated ZoneInfo, so the validation is
    defensive — it should not trip in normal usage).
    """
    c = _cctally()
    tz: Any = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(
                f"--tz must be a valid IANA zone, got {tz_name!r}"
            ) from exc

    def _parse_window_arg(
        raw: str, flag: str, *, is_upper_bound: bool
    ) -> dt.datetime:
        # Full-ISO args carry an explicit time component or tz marker.
        if "T" in raw or "+" in raw or "Z" in raw:
            return parse_iso_datetime(raw, flag)
        # Date-only: route through the centralized dual-form helper
        # (spec §7.1.1) so YYYY-MM-DD / YYYYMMDD parsing and the error
        # message stay consistent with cmd_blocks / cmd_daily / etc.
        naive = c._try_dual_form_date(raw)
        if naive is None:
            # Second-chance (issue #101): the pre-Session-A code fell
            # through to parse_iso_datetime here, so space-separated
            # datetimes (`2026-05-01 12:30:00`) and ISO week-dates
            # (`2026-W18-1`) — both accepted by datetime.fromisoformat but
            # rejected by the dual-form parser — kept working. cache-report
            # is the only date-taking command with this fallthrough; the
            # other commands accept YYYY-MM-DD / YYYYMMDD only. Returned
            # verbatim: a full datetime carries its own time component, so
            # the is_upper_bound end-of-day expansion is NOT applied (it's
            # for bare date-only forms). On TOTAL failure (neither dual-form
            # nor ISO), fall back to the centralized dual-form diagnostic
            # rather than parse_iso_datetime's more generic message.
            try:
                return parse_iso_datetime(raw, flag)
            except ValueError:
                c._parse_dual_form_date(raw, flag)  # eprints + raises ValueError
                raise  # unreachable — the call above always raises here
        if is_upper_bound:
            naive = naive.replace(
                hour=23, minute=59, second=59, microsecond=999999,
            )
        # When tz is supplied, attach it directly so `--tz utc --since
        # 2026-05-01` lands at 2026-05-01T00:00Z regardless of host zone.
        # Otherwise legacy: astimezone() on a naive datetime consults the
        # OS tz database for the actual offset at that date (DST-safe).
        if tz is not None:
            return naive.replace(tzinfo=tz)
        # internal fallback: host-local intentional
        return naive.astimezone()

    # Testing hook: when a pinned "now" is supplied via _command_as_of(),
    # use it as the wall-clock anchor. When tz is supplied, project into
    # that zone for parity with the date-only branch (so `since = today -
    # (days-1) midnight` lands on the calendar boundary the user expects);
    # otherwise fall back to host-local.
    if now_utc is not None:
        # internal fallback: host-local intentional (else branch)
        now_local = (
            now_utc.astimezone(tz) if tz is not None else now_utc.astimezone()
        )
    else:
        now_local = (
            dt.datetime.now(tz=tz) if tz is not None
            # internal fallback: host-local intentional
            else dt.datetime.now().astimezone()
        )
    # Empty-interval contract (spec design.md:47): `--since == --until` →
    # empty window. Collapse before date-only expansion would push the
    # upper bound to 23:59:59.999999 and flip the contract. We compare
    # *parsed* lower-bound timestamps when both args are date-only so
    # `--since 20260418 --until 2026-04-18` (same day, different
    # accepted formats) also collapses. For full-ISO args, fall back to
    # raw-string equality — parsing both as lower bounds would drop an
    # explicit midnight upper bound silently.
    if args.since and args.until:
        since_is_iso = any(ch in args.since for ch in ("T", "+", "Z"))
        until_is_iso = any(ch in args.until for ch in ("T", "+", "Z"))
        if not since_is_iso and not until_is_iso:
            since_p = _parse_window_arg(
                args.since, "--since", is_upper_bound=False
            )
            until_p = _parse_window_arg(
                args.until, "--until", is_upper_bound=False
            )
            if since_p == until_p:
                return since_p, since_p
        elif args.since == args.until:
            since = _parse_window_arg(
                args.since, "--since", is_upper_bound=False
            )
            return since, since
    until = (
        _parse_window_arg(args.until, "--until", is_upper_bound=True)
        if args.until
        else now_local
    )
    if args.since:
        since = _parse_window_arg(args.since, "--since", is_upper_bound=False)
    else:
        days = args.days
        if args.until:
            # --until without --since: step back by args.days
            since = until - dt.timedelta(days=days)
        else:
            # neither: midnight-anchored behavior matches pre-refactor baseline
            since = dt.datetime.combine(
                (now_local - dt.timedelta(days=days - 1)).date(),
                dt.time(0, 0, 0),
                tzinfo=now_local.tzinfo,
            )
    return since, until


def _emit_cache_report_json(
    rows: list["crk.CacheRow"],
    mode: str,
    *,
    now_utc: dt.datetime | None = None,
) -> str:
    """Serialize rows + totals to JSON matching the spec schema.

    Daily mode keeps the top-level `days` key and per-row `totalCost` /
    totals.totalCost aliases for backward compat. Session mode uses
    `sessions` and adds sessionId / projectPath / lastActivity /
    sourcePaths per row. Anomaly object is always emitted with
    triggered=false / reasons=[] until Task 5 populates it.
    """
    top_key = "sessions" if mode == "session" else "days"

    def _row_to_dict(r: "crk.CacheRow") -> dict[str, Any]:
        d: dict[str, Any] = {
            "inputTokens": r.input_tokens,
            "outputTokens": r.output_tokens,
            "cacheCreationTokens": r.cache_creation_tokens,
            "cacheReadTokens": r.cache_read_tokens,
            "totalTokens": r.total_tokens,
            "cost": round(r.cost, 6),
            "cacheHitPercent": round(r.cache_hit_percent, 2),
            "savedUsd": round(r.saved_usd, 6),
            "wastedUsd": round(r.wasted_usd, 6),
            "netUsd": round(r.net_usd, 6),
            "anomaly": {
                "triggered": r.anomaly_triggered,
                "reasons": list(r.anomaly_reasons),
            },
            "modelBreakdowns": [
                {
                    "modelName": mb.model_name,
                    "inputTokens": mb.input_tokens,
                    "outputTokens": mb.output_tokens,
                    "cacheCreationTokens": mb.cache_creation_tokens,
                    "cacheReadTokens": mb.cache_read_tokens,
                    "cacheHitPercent": round(mb.cache_hit_percent, 2),
                    "cost": round(mb.cost, 6),
                    "savedUsd": round(mb.saved_usd, 6),
                    "wastedUsd": round(mb.wasted_usd, 6),
                    "netUsd": round(mb.net_usd, 6),
                }
                for mb in r.model_breakdowns
            ],
        }
        if mode == "session":
            d["sessionId"] = r.session_id
            d["projectPath"] = r.project_path
            d["lastActivity"] = (
                r.last_activity.astimezone(dt.timezone.utc).isoformat()
                if r.last_activity else None
            )
            d["sourcePaths"] = list(r.source_paths)
            d["models"] = [mb.model_name for mb in r.model_breakdowns]
        else:
            d["date"] = r.date
            d["models"] = [mb.model_name for mb in r.model_breakdowns]
        return d

    tot_inp = sum(r.input_tokens for r in rows)
    tot_cc = sum(r.cache_creation_tokens for r in rows)
    tot_cr = sum(r.cache_read_tokens for r in rows)

    output: dict[str, Any] = {
        top_key: [_row_to_dict(r) for r in rows],
        "totals": {
            "inputTokens": tot_inp,
            "outputTokens": sum(r.output_tokens for r in rows),
            "cacheCreationTokens": tot_cc,
            "cacheReadTokens": tot_cr,
            "totalTokens": sum(r.total_tokens for r in rows),
            "cost": round(sum(r.cost for r in rows), 6),
            "cacheHitPercent": round(
                crk._compute_cache_hit_percent(tot_inp, tot_cc, tot_cr), 2
            ),
            "savedUsd": round(sum(r.saved_usd for r in rows), 6),
            "wastedUsd": round(sum(r.wasted_usd for r in rows), 6),
            "netUsd": round(sum(r.net_usd for r in rows), 6),
        },
        "generatedAt": now_utc_iso(now_utc=now_utc),
    }

    # Backward compat: daily mode previously emitted "totalCost" at row +
    # totals level. Preserve alongside "cost" so downstream consumers keep
    # working.
    if mode == "day":
        for row_dict in output[top_key]:
            row_dict["totalCost"] = row_dict["cost"]
        output["totals"]["totalCost"] = output["totals"]["cost"]
    return json.dumps(output, indent=2)


def _build_cache_report_title(args: argparse.Namespace, mode: str) -> str:
    """Build the banner title for cache-report text output."""
    scope = "per session" if mode == "session" else "per model/day"
    if args.since or args.until:
        start = args.since or "auto"
        end = args.until or "now"
        return f"Cache Hit Report – [{start}, {end}] ({scope})"
    return (
        f"Cache Hit Report – Last {args.days} Day"
        f"{'s' if args.days != 1 else ''} ({scope})"
    )


def _sort_cache_rows(
    rows: list["crk.CacheRow"],
    sort_key: str,
    mode: str,
) -> None:
    """Sort rows in place per spec Sec. 8.

    Modes: 'session' or 'day'. Sort keys: date, net, cache, recent, cost,
    anomaly. Tiebreakers are hard-coded (not user-configurable):
      - net / cache / cost ties -> ascending (date | last_activity), then
        ascending session_id.
      - date / recent ties -> ascending session_id.

    For 'anomaly', delegates to the mode default first, then stable-sorts
    with anomaly_triggered rows first.
    """
    # Tiebreaker: ascending date/last_activity, then sessionId.
    def _time_tiebreaker(r: "crk.CacheRow") -> tuple[dt.datetime, str]:
        anchor = r.last_activity
        if anchor is None and r.date:
            # Use .astimezone() (OS tzdb) rather than .replace(tzinfo=...) so
            # DST-straddling dates resolve to the correct offset — same idiom
            # as _annotate_anomalies._row_anchor.
            # internal fallback: host-local intentional
            anchor = dt.datetime.strptime(r.date, "%Y-%m-%d").astimezone()
        # Use tz-aware sentinel to avoid naive-vs-aware comparison errors.
        fallback = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return (anchor or fallback, r.session_id or "")

    if sort_key == "date":
        if mode == "session":
            fallback = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            rows.sort(key=lambda r: (r.last_activity or fallback,
                                     r.session_id or ""))
        else:
            rows.sort(key=lambda r: (r.date or "", r.session_id or ""))
    elif sort_key == "net":
        rows.sort(key=lambda r: (r.net_usd, _time_tiebreaker(r)))
    elif sort_key == "cache":
        rows.sort(key=lambda r: (r.cache_hit_percent, _time_tiebreaker(r)))
    elif sort_key == "recent":
        if mode == "session":
            # Python sort is stable; negate timestamp so ascending sort yields
            # most-recent first. None -> epoch 0 (pushed to end when negated).
            rows.sort(
                key=lambda r: (
                    -(r.last_activity.timestamp() if r.last_activity else 0.0),
                    r.session_id or "",
                )
            )
        else:
            # Daily mode: descending by date. session_id tiebreaker is a
            # formality — daily rows never have session_id. Two-pass stable
            # sort: ascending session_id, then descending date preserves
            # primary desc + ascending tiebreaker.
            rows.sort(key=lambda r: r.session_id or "")
            rows.sort(key=lambda r: r.date or "", reverse=True)
    elif sort_key == "cost":
        rows.sort(key=lambda r: (-r.cost, _time_tiebreaker(r)))
    elif sort_key == "anomaly":
        # Anomalous rows first (stable), then mode default within each group.
        default_sub = "net" if mode == "session" else "date"
        _sort_cache_rows(rows, default_sub, mode)
        rows.sort(key=lambda r: 0 if r.anomaly_triggered else 1)


def cmd_cache_report(args: argparse.Namespace) -> int:
    c = _cctally()
    config = c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz so the
    # existing resolve_display_tz precedence absorbs the new alias. The
    # canonical --tz still wins (it's set on the namespace before this
    # bridge fires); when --tz is unset and -z is supplied, use -z.
    c._bridge_z_into_tz(args, config)
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz

    now_utc = _command_as_of()
    # Session A (spec §7.1.1): the dual-form helper eprints its own
    # diagnostic and raises a bare ValueError; catch it here so main()'s
    # generic ``Error: {exc}`` fallback doesn't double-print an empty
    # trailer. Mirrors the catch-and-return-1 shape in cmd_blocks.
    #
    # Session A note (Review-A P2-D): cache-report's argparse alias
    # surface lands in Implementor 2's scope (B-series). The try/except
    # here only routes _resolve_cache_report_window's bare ValueError
    # around main()'s generic Error: handler — no parser-level changes.
    try:
        since, until = _resolve_cache_report_window(
            args, now_utc=now_utc,
            tz_name=(tz.key if tz is not None else None),
        )
    except ValueError as exc:
        # Centralized helper already eprinted on the dual-form path; for
        # the legacy parse_iso_datetime path (full-ISO mis-format) the
        # ValueError carries the message, so eprint it.
        msg = str(exc)
        if msg:
            eprint(f"Error: {msg}")
        return 1

    # Issue #89 Pattern C: deferred loader scoped to the rendered window
    # (project filter mirrors what the cache-aggregator uses).
    c._emit_debug_samples_if_set(
        args,
        lambda: c.get_entries(since, until, project=args.project),
        command_label="cache-report",
    )

    mode = "session" if getattr(args, "by_session", False) else "day"
    top_key = "sessions" if mode == "session" else "days"

    if since == until:
        if args.json:
            print(json.dumps(
                {top_key: [], "totals": None,
                 "generatedAt": now_utc_iso(now_utc=now_utc)},
                indent=2,
            ))
        else:
            print("(no cache activity in window)")
        return 0

    if mode == "session":
        rows = _aggregate_cache_by_session(since, until, project=args.project)
    else:
        # Task A3: pass the resolved display_tz so day buckets match the
        # ``--tz`` flag (closes the pre-existing minor bug where the
        # window edges shifted but day buckets stayed on host-local —
        # spec §1.6 / plan A3).
        rows = _aggregate_cache_by_day(
            since, until, project=args.project, display_tz=tz,
        )

    if not rows:
        if args.json:
            print(json.dumps(
                {top_key: [], "totals": None,
                 "generatedAt": now_utc_iso(now_utc=now_utc)},
                indent=2,
            ))
        else:
            print("(no cache activity in window)")
        return 0

    _annotate_anomalies(
        rows,
        threshold_pp=args.anomaly_threshold_pp,
        window_days=args.anomaly_window_days,
        enabled=not args.no_anomaly,
    )

    resolved_sort = args.sort
    if resolved_sort is None:
        resolved_sort = "net" if mode == "session" else "date"
    _sort_cache_rows(rows, resolved_sort, mode)

    if args.json:
        print(_emit_cache_report_json(rows, mode, now_utc=now_utc))
        return 0

    title = _build_cache_report_title(args, mode)
    print(_render_cache_report_table(rows, title, mode=mode, tz=tz, compact=args.compact))
    return 0
