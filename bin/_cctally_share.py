"""cctally share-output construction (eager sibling).

Holds the share destination/emit path (_resolve_destination, _emit,
_share_open_file), the _build_*_snapshot builders, and the _share_* helpers.
Loaded eagerly by bin/cctally; every symbol is re-exported into the cctally
namespace (the dashboard thunks + staying budget-snapshot builders bare-call
these). Heavy share kernels are lazy-loaded inside _share_load_lib().

Spec: docs/superpowers/specs/2026-05-30-parser-share-extraction-design.md
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import subprocess
import sys

import _lib_changelog  # module-qualified: _lib_changelog._read_latest_changelog_version()
from _lib_display_tz import format_display_dt
from _lib_render import _project_disambiguate_labels


# ============================================================
# ==== Shareable reports: destination + emit ====            =
# ============================================================
# Translate parsed argparse args + a rendered string into actual delivery
# (stdout / file / clipboard / open). These helpers live here, NOT in
# `_lib_share.py`, so the kernel module stays I/O-pure (Section 5.8 of the
# shareable-reports spec).


# Module-level latch for the home-dir fallback hint. Spec Section 4.2 calls
# for a one-shot stderr suggestion when share output lands in $HOME because
# both XDG_DOWNLOAD_DIR and ~/Downloads were absent. Latched here (process
# scope) so a user running, e.g., a `cctally daily --format html` followed
# by `cctally weekly --format html` in the same shell sees the hint exactly
# once. Tests reset by reaching into the module globals if needed.
_DOWNLOADS_HOME_HINT_EMITTED = False


def _share_resolve_download_dir() -> pathlib.Path:
    """XDG -> ~/Downloads -> ~ fallback (Section 4.2)."""
    global _DOWNLOADS_HOME_HINT_EMITTED
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        p = pathlib.Path(xdg).expanduser()
        if p.exists():
            return p
    downloads = pathlib.Path.home() / "Downloads"
    if downloads.exists():
        return downloads
    if not _DOWNLOADS_HOME_HINT_EMITTED:
        sys.stderr.write(
            "cctally: writing share output to home dir; "
            "pass --output <path> to choose a destination\n"
        )
        _DOWNLOADS_HOME_HINT_EMITTED = True
    return pathlib.Path.home()


def _share_unique_path(base: pathlib.Path) -> pathlib.Path:
    """Auto-collision counter — base.html -> base-2.html -> base-3.html -> ... cap 99.

    Exhaustion (>99 same-day collisions) exits 3 per spec Section 4.4. Prior
    code raised ``SystemExit("…")`` which yields exit 1 — broke the spec's
    distinct-exit-code contract for collision exhaustion vs. generic errors.
    """
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    for n in range(2, 100):
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    print(
        f"cctally: too many same-day collisions in {parent}; use --output <path>",
        file=sys.stderr,
    )
    sys.exit(3)


def _resolve_destination(
    args, *, cmd: str, generated_at_utc_date: str
) -> tuple[str, pathlib.Path | None]:
    """Translate argparse args into (kind, value).

    kind: "stdout" | "file" | "clipboard"
    value: pathlib.Path for "file"; None for "stdout" / "clipboard".

    Exit-code contract (spec Section 4.4):
      - exit 2 on invalid flag combinations (--copy on non-md;
        --copy + --output; --copy with no clipboard tool; --open + md).
      - exit 3 on collision exhaustion (delegated to _share_unique_path).
    """
    fmt = args.format
    if getattr(args, "copy", False) and getattr(args, "output", None) is not None:
        # Mutex: a clipboard destination by definition has no path. Spec
        # Section 4.4 line 132 calls this out explicitly. Prior code silently
        # let --copy override --output, which surprised users who expected
        # the file to land alongside the clipboard write.
        print(
            "cctally: --copy is mutually exclusive with --output",
            file=sys.stderr,
        )
        sys.exit(2)
    if getattr(args, "copy", False):
        if fmt != "md":
            print("cctally: --copy is only valid with --format md", file=sys.stderr)
            sys.exit(2)
        return ("clipboard", None)

    output = getattr(args, "output", None)
    if output == "-":
        return ("stdout", None)
    if output:
        return ("file", pathlib.Path(output).expanduser())

    if fmt == "md":
        return ("stdout", None)
    # html/svg default -> ~/Downloads/cctally-<cmd>-<utcdate>.<ext>
    base = _share_resolve_download_dir() / f"cctally-{cmd}-{generated_at_utc_date}.{fmt}"
    return ("file", _share_unique_path(base))


def _emit(content: str, *, kind: str, value: pathlib.Path | str | None) -> None:
    """Deliver rendered content to stdout/file/clipboard."""
    if kind == "stdout":
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
        return

    if kind == "file":
        path = pathlib.Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        sys.stderr.write(f"Wrote {path}\n")
        return

    if kind == "clipboard":
        # Track tools that were found-but-failed separately from "no tool on
        # PATH" so the error message accurately describes what went wrong.
        # The prior shape ("requires pbcopy/xclip/clip on PATH") was
        # misleading when e.g. pbcopy was present but exited non-zero.
        tried = []
        for cmd_args in (
            ["pbcopy"],
            ["xclip", "-sel", "clip"],
            ["clip.exe"],
        ):
            tool = cmd_args[0]
            if shutil.which(tool):
                proc = subprocess.run(cmd_args, input=content, text=True, check=False)
                if proc.returncode == 0:
                    sys.stderr.write(f"Copied to clipboard via {tool}\n")
                    return
                tried.append(f"{tool} (exit {proc.returncode})")
        if tried:
            print(
                f"cctally: clipboard tool failed: {', '.join(tried)}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(
            "cctally: --copy requires pbcopy, xclip, or clip on PATH",
            file=sys.stderr,
        )
        sys.exit(2)

    raise ValueError(f"unknown destination kind: {kind!r}")


def _share_load_lib():
    """Lazy-load `_lib_share` with sys.modules caching.

    Single-load semantics keep ShareSnapshot / MoneyCell / etc. class
    identities stable across kernel imports: the test harness pre-registers
    `_lib_share` in `sys.modules`, the wrapper imports it via this helper,
    and snapshot builders import it via this helper — all paths must see
    the SAME module object so `isinstance` checks on snapshot cells compare
    across one class identity, not many. This is the chokepoint for the
    duplicate-class-identity bug surfaced under the test harness in
    Implementor 6's fix-loop.

    Registers in sys.modules BEFORE exec_module: Python 3.14's `dataclass`
    decorator looks up `cls.__module__` in `sys.modules` for `KW_ONLY` type
    checks, and an absent entry would re-trigger the dual-load path under
    some import orders.
    """
    cached = sys.modules.get("_lib_share")
    if cached is not None:
        return cached
    import importlib.util as _ilu
    _lib_share_path = pathlib.Path(__file__).resolve().parent / "_lib_share.py"
    _spec = _ilu.spec_from_file_location("_lib_share", _lib_share_path)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["_lib_share"] = _mod
    _spec.loader.exec_module(_mod)
    return _mod


def _share_now_utc() -> dt.datetime:
    """`generated_at` source — honors CCTALLY_AS_OF env hook for fixture stability.

    Mirrors the existing `CCTALLY_AS_OF` precedent used by `project` /
    `forecast` for deterministic fixture goldens. Format: ISO-8601 with `Z`
    or explicit offset (e.g. `2026-05-09T12:00:00Z` or
    `2026-05-09T12:00:00+00:00`); falls back to wall-clock UTC when unset.

    Raises ValueError on malformed `CCTALLY_AS_OF` input — deliberate
    fail-loud behavior for the dev hook so fixture authors notice typos
    immediately rather than silently falling back to wall-clock time.
    """
    override = os.environ.get("CCTALLY_AS_OF")
    if override:
        parsed = dt.datetime.fromisoformat(override.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _share_now_utc_iso() -> str:
    """`generated_at` ISO-8601 source for /api/share/render snapshot envelopes.

    Honors `CCTALLY_AS_OF` like `_share_now_utc` so fixture goldens stay
    deterministic across the CLI and HTTP paths. Format `YYYY-MM-DDTHH:MM:SSZ`.
    """
    return _share_now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


# Spec §11.4 — recent-shares ring buffer caps at 20. Server-side trim
# in `_handle_share_history_post` so the on-disk `config.json` can't
# grow unbounded even if a misbehaving client floods POSTs.
_SHARE_HISTORY_RING_CAP = 20


def _share_history_recipe_id() -> str:
    """Server-stamped opaque id for a history record.

    Random base16 (26 chars / 13 bytes) is sufficient: we order by
    insertion (ring buffer position), never by id, so we don't need
    ULID timestamp-prefix monotonicity. `secrets.token_hex` keeps us
    on stdlib and avoids the predictability of `random`.
    """
    import secrets
    return secrets.token_hex(13)


def _share_resolve_version() -> str:
    """Source from CHANGELOG via the public helper. Empty string if unset.

    `_lib_changelog._read_latest_changelog_version` returns
    `(version, date) | None`; the snapshot's `version` field carries
    the version string only.
    """
    info = _lib_changelog._read_latest_changelog_version()
    return info[0] if info else ""


def _share_period_label(
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz_label: str,
) -> str:
    """Render the canonical "<start> → <end> (<tz>)" period label.

    Used by both the report and daily snapshot builders so the period label
    format stays consistent across share-enabled subcommands.
    """
    return (
        f"{period_start.strftime('%b %d')} → "
        f"{period_end.strftime('%b %d')} ({display_tz_label})"
    )


def _share_parse_date_to_dt(value, tz: "ZoneInfo | None") -> dt.datetime:
    """Coerce a `YYYY-MM-DD` string or `dt.date` into a tz-aware datetime.

    Used by the share gate sites to lift week-boundary date strings
    (`weekStartDate`, `weekEndDate`) into the tz-aware datetimes that
    `PeriodSpec` expects. None / empty / unparseable -> current UTC; the
    caller already gated on a non-empty trend before reaching this path,
    so the fallback is purely defensive against missing-data corner cases.
    """
    if value is None:
        return _share_now_utc()
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz or dt.timezone.utc)
    if isinstance(value, dt.date):
        d = value
    else:
        try:
            d = dt.date.fromisoformat(str(value))
        except ValueError:
            return _share_now_utc()
    midnight = dt.datetime(d.year, d.month, d.day)
    return midnight.replace(tzinfo=tz or dt.timezone.utc)


def _share_display_tz_label(tz: "ZoneInfo | None") -> str:
    """Render a stable display-tz string for `PeriodSpec.display_tz`.

    `resolve_display_tz` returns `None` for "local" (caller does bare
    astimezone); the share snapshot needs a non-None string. Map None ->
    "local" and use ZoneInfo.key otherwise.
    """
    return tz.key if tz is not None else "local"


def _build_report_snapshot(
    view: "TrendView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally report`.

    Consumes the unified TrendView (spec §6.4). `view.rows` is the
    chronological (oldest-first) TuiTrendRow tuple — exactly the order
    the chart needs (BarChart polyline trends left→right with time);
    no reversal needed.

    The earlier camelCase-dict workaround (recorded in the commit body
    of Implementor 7 of the share-v2 work) is obsolete: `TuiTrendRow`
    now carries 10 nullable extended fields (spec §4.1) and is the
    single typed shape that flows through both CLI report and share
    builders. Cmd_report's JSON serialization happens at the gate site
    (camelCase mapping done in cmd_report); this function reads
    attributes directly from the typed row.

    `theme` and `reveal_projects` flow into the subtitle directly so
    the builder owns the canonical subtitle shape — no post-build
    re-stamp at the gate site. The forward-reference return type
    matches the kernel's lazy-import boundary.
    """
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="week", label="Week", align="left"),
        _lib_share.ColumnSpec(key="used", label="% Used", align="right"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right"),
        _lib_share.ColumnSpec(key="dpp", label="$ / %", align="right",
                              emphasis=True),
    )
    rows = view.rows  # oldest-first; matches chart's left→right walk.
    snap_rows: list = []
    chart_pts: list = []
    for i, r in enumerate(rows):
        wsd = r.week_start_date.isoformat() if r.week_start_date else None
        if isinstance(wsd, str) and wsd:
            try:
                week_label = dt.date.fromisoformat(wsd).strftime("%b %d")
            except ValueError:
                week_label = wsd
        else:
            week_label = "—"
        # Preserve None vs 0.0 distinction (parity with terminal/JSON).
        # Terminal _render_weekly_table renders missing values as "—";
        # share artifact follows the same convention. Coercing None to
        # 0.0 would render `$0.00` / `0.0%` — indistinguishable from a
        # genuine zero, and would skew the avg / chart.
        used_pct_raw = r.used_pct
        cost_raw = r.weekly_cost_usd
        dpp_raw = r.dollars_per_percent
        snap_rows.append(_lib_share.Row(cells={
            "week": _lib_share.TextCell(week_label),
            "used": (
                _lib_share.PercentCell(float(used_pct_raw))
                if used_pct_raw is not None else _lib_share.TextCell("—")
            ),
            "cost": (
                _lib_share.MoneyCell(float(cost_raw))
                if cost_raw is not None else _lib_share.TextCell("—")
            ),
            "dpp": (
                _lib_share.MoneyCell(float(dpp_raw))
                if dpp_raw is not None else _lib_share.TextCell("—")
            ),
        }))
        # Skip chart points for weeks with no $/% sample — the polyline
        # connects across the gap rather than dropping to 0, which would
        # misrepresent missing data as a crash to zero.
        if dpp_raw is not None:
            chart_pts.append(_lib_share.ChartPoint(
                x_label=week_label,
                x_value=float(i),
                y_value=float(dpp_raw),
            ))
    chart = (
        _lib_share.LineChart(points=tuple(chart_pts), y_label="$ / %")
        if len(chart_pts) >= 3 else None
    )
    # Source the avg from the view (3-sample rule). Falls back to a
    # length-based average over the chart points for the <3-sample case
    # so the Totalled cell always renders something concrete; preserves
    # the prior $0.00 sentinel on empty data.
    if view.avg_dollars_per_pct is not None:
        avg_dpp = view.avg_dollars_per_pct
    else:
        avg_dpp = (
            sum(p.y_value for p in chart_pts) / len(chart_pts)
            if chart_pts else 0.0
        )
    totals = (
        _lib_share.Totalled(label="Avg $/%", value=f"${avg_dpp:,.2f}"),
    )
    if rows:
        title = f"Weekly $ / % trend — last {len(rows)} weeks"
    else:
        title = "Weekly $ / % trend — no data"
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="report",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=(),
        generated_at=_share_now_utc(), version=version,
    )


def _build_daily_snapshot(
    view: "DailyView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally daily`.

    Consumes the unified DailyView (spec §6.1). `view.aggregated` is
    the gap-free BucketUsage tuple in newest-first order; we reverse
    here so BarChart bars render left-to-right chronologically.
    `view.total_cost_usd` is the pre-computed sum (replacing the
    prior inline re-totaling).

    Deviations from the plan sketch (which assumed dict rows with keys
    `date` / `cost_usd` / `pct_of_week` / `top_model`):

    - Rows are `BucketUsage` dataclasses; we read fields by attribute.
    - Daily has no native `% of week` column — daily is range-scoped, not
      week-scoped. We render `% of period` (this row's cost / total range
      cost) so the column carries meaningful info; the `pct_week` key
      survives in the column spec for plan-shape parity.
    - `top_model` is the first entry of `model_breakdowns` (sorted by cost
      desc per upstream ccusage parity); empty → "—".

    `period_start` / `period_end` / `display_tz` are passed by the
    caller (they reflect the CLI's `--since` / `--until` window which
    may extend past the data window). `theme` and `reveal_projects`
    flow into the subtitle directly so the builder owns the canonical
    subtitle shape — no post-build re-stamp at the gate site.
    """
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="date", label="Date", align="left"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="pct_week", label="% of Period",
                              align="right"),
        _lib_share.ColumnSpec(key="top_model", label="Top Model",
                              align="left"),
    )
    # Caller MUST pass rows in chronological order so the BarChart bars
    # line up left-to-right with time. view.aggregated is newest-first
    # (matches dashboard convention); reverse for chronological iteration.
    rows = list(reversed(view.aggregated))
    total_cost = view.total_cost_usd

    snap_rows: list = []
    chart_pts: list = []
    for i, r in enumerate(rows):
        # `BucketUsage.bucket` is typed `str` (YYYY-MM-DD); guard against
        # empty / unparseable but skip the dead `dt.date` branch.
        bucket = getattr(r, "bucket", None)
        if isinstance(bucket, str) and bucket:
            try:
                date_str = dt.date.fromisoformat(bucket).strftime("%b %d")
            except ValueError:
                date_str = bucket
        else:
            date_str = "—"
        cost_usd = float(getattr(r, "cost_usd", 0.0) or 0.0)
        breakdowns = getattr(r, "model_breakdowns", None) or []
        top_model = (breakdowns[0].get("modelName") if breakdowns else None) or "—"
        pct_of_period = (cost_usd / total_cost * 100.0) if total_cost > 0 else 0.0
        snap_rows.append(_lib_share.Row(cells={
            "date": _lib_share.TextCell(date_str),
            "cost": _lib_share.MoneyCell(cost_usd),
            "pct_week": _lib_share.PercentCell(pct_of_period),
            "top_model": _lib_share.TextCell(top_model),
        }))
        chart_pts.append(_lib_share.ChartPoint(
            x_label=date_str,
            x_value=float(i),
            y_value=cost_usd,
        ))
    chart = (
        _lib_share.BarChart(points=tuple(chart_pts), y_label="$")
        if chart_pts else None
    )
    avg_cost = (total_cost / len(chart_pts)) if chart_pts else 0.0
    totals = (
        _lib_share.Totalled(label="Sum", value=f"${total_cost:,.2f}"),
        _lib_share.Totalled(label="Days", value=str(len(chart_pts))),
        _lib_share.Totalled(label="Avg / day", value=f"${avg_cost:,.2f}"),
    )
    if rows:
        title = (
            f"Daily usage — {period_start.strftime('%b %d')} → "
            f"{period_end.strftime('%b %d')}"
        )
    else:
        title = "Daily usage — no data"
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="daily",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=(),
        generated_at=_share_now_utc(), version=version,
    )


def _build_monthly_snapshot(
    view: "MonthlyView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally monthly`.

    Consumes the unified MonthlyView (spec §6.2). `view.aggregated` is
    the gap-free BucketUsage tuple in newest-first order; we reverse
    so BarChart bars render left-to-right chronologically.

    Deviations from the plan sketch (which assumed dict rows with keys
    `month` / `cost_usd` / `sessions`):

    - Rows are `BucketUsage` dataclasses; we read fields by attribute.
    - The plan's `Sessions` column has no source in the underlying data
      (`BucketUsage` carries no session count and `_aggregate_monthly`
      never computes one). Substituted with a `Tokens` column carrying
      total tokens — meaningful info already on the dataclass.
    - `Δ vs prior` is computed on `cost_usd` between consecutive ASC-sorted
      months, matching the plan's intent.

    `period_start` / `period_end` / `display_tz` are passed by the
    caller (the CLI's `--since` / `--until` window may extend past
    the data window). `theme` / `reveal_projects` flow into the
    subtitle directly so the builder owns the canonical subtitle
    shape — no post-build re-stamp at the gate site.
    """
    # Caller MUST pass rows in chronological order so the BarChart bars
    # line up left-to-right with time. view.aggregated is newest-first.
    rows = list(reversed(view.aggregated))
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="month", label="Month", align="left"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="tokens", label="Tokens", align="right"),
        _lib_share.ColumnSpec(key="delta", label="Δ vs prior", align="right"),
    )
    snap_rows: list = []
    chart_pts: list = []
    prev_cost: float | None = None
    for i, r in enumerate(rows):
        # `BucketUsage.bucket` is typed `str` ("YYYY-MM"); guard against
        # empty / unparseable but skip the dead `dt.date` branch.
        bucket = getattr(r, "bucket", None)
        month_str = bucket if isinstance(bucket, str) and bucket else "—"
        cost_usd = float(getattr(r, "cost_usd", 0.0) or 0.0)
        total_tokens = int(getattr(r, "total_tokens", 0) or 0)
        if prev_cost is not None and prev_cost > 0:
            delta_pct = (cost_usd - prev_cost) / prev_cost * 100.0
            delta_cell = _lib_share.DeltaCell(value=delta_pct, unit="%")
        else:
            delta_cell = _lib_share.TextCell("—")
        snap_rows.append(_lib_share.Row(cells={
            "month": _lib_share.TextCell(month_str),
            "cost": _lib_share.MoneyCell(cost_usd),
            "tokens": _lib_share.TextCell(f"{total_tokens:,}"),
            "delta": delta_cell,
        }))
        chart_pts.append(_lib_share.ChartPoint(
            x_label=month_str,
            x_value=float(i),
            y_value=cost_usd,
        ))
        prev_cost = cost_usd
    chart = (
        _lib_share.BarChart(points=tuple(chart_pts), y_label="$")
        if chart_pts else None
    )
    sum_cost = sum(p.y_value for p in chart_pts)
    avg_cost = (sum_cost / len(chart_pts)) if chart_pts else 0.0
    totals = (
        _lib_share.Totalled(label="Sum", value=f"${sum_cost:,.2f}"),
        _lib_share.Totalled(label="Months", value=str(len(chart_pts))),
        _lib_share.Totalled(label="Avg / month", value=f"${avg_cost:,.2f}"),
    )
    if rows:
        title = (
            f"Monthly usage — {period_start.strftime('%Y-%m')} → "
            f"{period_end.strftime('%Y-%m')}"
        )
    else:
        title = "Monthly usage — no data"
    period_label = (
        f"{period_start.strftime('%Y-%m')} → "
        f"{period_end.strftime('%Y-%m')} ({display_tz})"
    )
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="monthly",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=(),
        generated_at=_share_now_utc(), version=version,
    )


def _build_weekly_snapshot(
    view: "WeeklyView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
    breakdown_model: bool,
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally weekly`.

    Consumes the unified WeeklyView (spec §6.3). `view.aggregated` is
    the gap-free BucketUsage tuple newest-first; `view.overlay` is the
    parallel `(used_pct, dollars_per_pct)` tuple. We reverse both for
    chronological iteration so BarChart bars render left-to-right
    with time.

    Each bucket carries `bucket` (week_start_date as "YYYY-MM-DD"),
    `cost_usd`, `total_tokens`, and `model_breakdowns` (list[dict]
    sorted by cost desc, each `{modelName, ..., cost}`). Either
    overlay component may be `None` for a week with no captured
    snapshot — surfaces in the snapshot row as a `0.0` PercentCell so
    the column stays aligned (matching the table renderer's "no data
    → 0%" behavior).

    Deviations from the plan sketch (which assumed dict rows with keys
    `week_start_date` / `used_pct` / `cost_usd` / `sessions` /
    `model_breakdown` and a `breakdown_model: bool` derived from
    `args.breakdown == "model"`):

    - Rows are `BucketUsage` dataclasses; per-week `used_pct` lives in the
      separate `overlay` list — neither shape matches the plan literal.
    - The plan's `Sessions` column has no source — `BucketUsage` carries
      no session count and `_aggregate_weekly` never computes one.
      Substituted with a `Tokens` column (`total_tokens` formatted with
      thousands separators).
    - `args.breakdown` for `cmd_weekly` is `action="store_true"` (not a
      `{model,project}` choice), so `breakdown_model` is just the boolean
      `args.breakdown` from the gate site.
    - `model_breakdowns` is a list-of-dicts (`modelName` / `cost`), not a
      `{model: cost}` mapping; we coerce to a dict before key lookup.

    Honors `breakdown_model` by appending one `m_<model>` column per
    distinct model and populating `BarChart.stacks` with per-model series.
    All model-axis iteration uses a single sorted list (`all_model_keys`)
    so column / stack ordering is deterministic across runs.

    `theme` and `reveal_projects` flow into the subtitle directly so the
    builder owns the canonical subtitle shape — no post-build re-stamp at
    the gate site.
    """
    # view.aggregated / view.overlay are newest-first; reverse for asc
    # so BarChart bars are chronological.
    rows = list(reversed(view.aggregated))
    overlay = list(reversed(view.overlay))
    _lib_share = _share_load_lib()
    columns_list: list = [
        _lib_share.ColumnSpec(key="week", label="Week Start", align="left"),
        _lib_share.ColumnSpec(key="used", label="% Used", align="right"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="tokens", label="Tokens", align="right"),
    ]
    # Per-row model→cost lookup (BucketUsage exposes a list-of-dicts;
    # collapse to dict here so per-row column population is O(1) per
    # model). All breakdown-aware iteration goes through `all_model_keys`
    # for deterministic ordering.
    per_row_model_costs: list[dict[str, float]] = []
    for r in rows:
        breakdowns = getattr(r, "model_breakdowns", None) or []
        per_row_model_costs.append({
            (b.get("modelName") or "—"): float(b.get("cost") or 0.0)
            for b in breakdowns
        })
    if breakdown_model:
        all_model_keys = sorted({m for d in per_row_model_costs for m in d})
        for m in all_model_keys:
            columns_list.append(_lib_share.ColumnSpec(
                key=f"m_{m}", label=m, align="right",
            ))
    else:
        all_model_keys = []

    snap_rows: list = []
    chart_pts: list = []
    stacks: dict[str, list] = {}
    for i, r in enumerate(rows):
        # `BucketUsage.bucket` is typed `str` ("YYYY-MM-DD"); guard against
        # empty / unparseable but skip the dead `dt.date` branch.
        bucket = getattr(r, "bucket", None)
        if isinstance(bucket, str) and bucket:
            try:
                week_label = dt.date.fromisoformat(bucket).strftime("%b %d")
            except ValueError:
                week_label = bucket
        else:
            week_label = "—"
        cost_usd = float(getattr(r, "cost_usd", 0.0) or 0.0)
        total_tokens = int(getattr(r, "total_tokens", 0) or 0)
        # `used_pct` is None when the week lacks a `weekly_usage_snapshots`
        # row — render as em-dash to match terminal `_render_weekly_table`.
        # Coercing to 0.0 would conflate "no snapshot recorded" with "0%
        # used," same divergence the report builder fixes. cost_usd from
        # session_entries is genuinely 0 when there are no entries (not
        # missing data) so that path keeps MoneyCell(0.0).
        used_pct_raw = (
            overlay[i][0] if i < len(overlay) else None
        )
        cells = {
            "week": _lib_share.TextCell(week_label),
            "used": (
                _lib_share.PercentCell(float(used_pct_raw))
                if used_pct_raw is not None else _lib_share.TextCell("—")
            ),
            "cost": _lib_share.MoneyCell(cost_usd),
            "tokens": _lib_share.TextCell(f"{total_tokens:,}"),
        }
        if breakdown_model:
            row_costs = per_row_model_costs[i]
            for m in all_model_keys:
                m_cost = float(row_costs.get(m) or 0.0)
                cells[f"m_{m}"] = _lib_share.MoneyCell(m_cost)
                stacks.setdefault(m, []).append(_lib_share.ChartPoint(
                    x_label=week_label,
                    x_value=float(i),
                    y_value=m_cost,
                    series_key=m,
                ))
        snap_rows.append(_lib_share.Row(cells=cells))
        chart_pts.append(_lib_share.ChartPoint(
            x_label=week_label,
            x_value=float(i),
            y_value=cost_usd,
        ))
    # `BarChart.stacks` is `Mapping[str, tuple[ChartPoint, ...]] | None`
    # (Implementor 1's tightening); convert dict-of-lists to dict-of-tuples.
    stacks_immut = (
        {k: tuple(v) for k, v in stacks.items()} if stacks else None
    )
    chart = (
        _lib_share.BarChart(
            points=tuple(chart_pts), y_label="$", stacks=stacks_immut,
        )
        if chart_pts else None
    )
    sum_cost = sum(p.y_value for p in chart_pts)
    pct_values = [
        float(o[0]) for o in overlay
        if o is not None and o[0] is not None
    ]
    avg_pct = (sum(pct_values) / len(pct_values)) if pct_values else 0.0
    peak_pct = max(pct_values, default=0.0)
    totals = (
        _lib_share.Totalled(label="Sum", value=f"${sum_cost:,.2f}"),
        _lib_share.Totalled(label="Avg %/wk", value=f"{avg_pct:.1f}%"),
        _lib_share.Totalled(label="Peak %", value=f"{peak_pct:.1f}%"),
    )
    title = (
        f"Weekly usage — last {len(rows)} weeks"
        if rows
        else "Weekly usage — no data"
    )
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="weekly",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=tuple(columns_list), rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=(),
        generated_at=_share_now_utc(), version=version,
    )


def _build_forecast_snapshot(
    *,
    week_start: dt.datetime,
    week_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
    actual_series: list[tuple[str, float, float]],
    projected_series: list[tuple[str, float, float]],
    current_pct: float,
    projected_low_pct: float,
    projected_high_pct: float,
    days_remaining: float,
    dollars_per_percent: float,
    dollars_per_percent_source: str,
    low_conf: bool,
    notes: tuple[str, ...] = (),
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally forecast`.

    `actual_series` is a list of `(x_label, x_value, y_value)` tuples drawn
    from `weekly_usage_snapshots` for the current week — each sample's
    `captured_at_utc` is the x_label (formatted compactly), `x_value` is
    elapsed-hours-since-week-start (a monotonic float so the LineChart
    renders left→right), and `y_value` is `weekly_percent` at that capture.

    `projected_series` is a parallel list of `(x_label, x_value, y_value)`
    tuples for the projection ray — the simplest form is a 2-point line
    from `(now, current_pct)` to `(week_end, projected_eow_pct)`. The
    renderer treats it as a `multi_series` overlay on top of the actual line.

    Deviations from the plan sketch (which assumed a single
    `_compute_forecast_data(args) -> dict` helper and `dpp_week_avg` /
    `dpp_24h` as separate columns):

    - `cmd_forecast` already exposes the data as `ForecastOutput` (which
      wraps `ForecastInputs`). No helper extraction was needed; we pass
      the actual scalars in directly.
    - `ForecastInputs` carries a single `dollars_per_percent` value plus
      a `dollars_per_percent_source` enum (`this_week` /
      `trailing_4wk_median` / `this_week_sparse`); there is no separate
      `dpp_week_avg` and `dpp_24h`. The table renders one $/1% row with
      the source as a paren suffix in the metric cell.
    - The plan's single `projected_eow_pct` is split into a low/high
      range (matching `--render-forecast-terminal`'s "Forecast 80–95%"
      band). The table shows both ends; the projected_series ray uses
      the high end so the overlay aligns with the conservative budget
      consumers expect from the chart.

    Reference lines at 90%/100% are LineChart-stable across all samples;
    severities `warn` (90%) and `alarm` (100%) drive the renderer's
    color mapping.

    `theme` and `reveal_projects` flow into the subtitle directly so the
    builder owns the canonical subtitle shape — no post-build re-stamp
    at the gate site.

    `notes`, when non-empty, overrides the auto-emitted "LOW CONF — data
    thin" note. The empty-data fast-path passes a clearer "no snapshots
    recorded" note so the artifact says what's actually wrong; the
    confidence-thin terminal path passes nothing and falls back to the
    auto LOW CONF banner.
    """
    _lib_share = _share_load_lib()
    actual_pts = tuple(
        _lib_share.ChartPoint(x_label=lbl, x_value=float(xv), y_value=float(yv))
        for lbl, xv, yv in actual_series
    )
    projected_pts = tuple(
        _lib_share.ChartPoint(x_label=lbl, x_value=float(xv), y_value=float(yv))
        for lbl, xv, yv in projected_series
    )
    chart = (
        _lib_share.LineChart(
            points=actual_pts,
            y_label="cumulative %",
            reference_lines=(
                (90.0, "90%", "warn"),
                (100.0, "100%", "alarm"),
            ),
            multi_series={"projected": projected_pts} if projected_pts else None,
        )
        if actual_pts else None
    )

    columns = (
        _lib_share.ColumnSpec(key="metric", label="Metric", align="left"),
        _lib_share.ColumnSpec(key="value", label="Value", align="right",
                              emphasis=True),
    )
    # Render the projected band as "low-high%" so a single PercentCell
    # carries the two-rate forecast spread. When the rates collapse to a
    # single value (no recent-24h sample), low == high.
    # 0.05 threshold: below .1f display precision — tighter spreads would
    # render as identical decimals, so collapse to a single value.
    if abs(projected_high_pct - projected_low_pct) < 0.05:
        projected_text = f"{projected_high_pct:.1f}%"
    else:
        projected_text = (
            f"{projected_low_pct:.1f}% — {projected_high_pct:.1f}%"
        )
    dpp_source_label = dollars_per_percent_source.replace("_", " ")
    snap_rows = (
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Current %"),
            "value": _lib_share.PercentCell(float(current_pct)),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Projected end-of-week %"),
            "value": _lib_share.TextCell(projected_text),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Days remaining"),
            "value": _lib_share.TextCell(f"{days_remaining:.1f}"),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell(f"$ / 1% ({dpp_source_label})"),
            "value": _lib_share.MoneyCell(float(dollars_per_percent)),
        }),
    )
    # Caller-provided `notes` (e.g., empty-data path's clearer message)
    # take precedence over the auto LOW CONF banner. Sibling builders
    # don't expose this knob; forecast does because its empty-state and
    # thin-confidence states need different copy.
    final_notes = notes if notes else (
        ("LOW CONF — data thin",) if low_conf else ()
    )
    if actual_pts:
        title = f"Forecast — week of {week_start.strftime('%b %d')}"
    else:
        title = "Forecast — no data"
    # Reuse the shared period-label helper so forecast's subtitle period
    # format matches sibling builders (cmd_daily / cmd_project / etc.).
    period_label = _share_period_label(week_start, week_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="forecast",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=week_start, end=week_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=snap_rows,
        chart=chart, totals=(), notes=final_notes,
        generated_at=_share_now_utc(), version=version,
    )


def _build_project_snapshot(
    rows: list[dict],
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally project`.

    `rows` is the in-memory per-project aggregate list produced inside
    `cmd_project` (`project_rows.values()` post-sort). Each row is a
    dict with: `key` (a `ProjectKey` carrying `display_key` /
    `bucket_path`), `cost_usd`, `attributed_pct` (`float | None`),
    `sessions` (a `set` of session-IDs).

    Privacy invariant (Section 8.4 / Section 5.3): the builder populates
    `ProjectCell.label` AND `ChartPoint.project_label` (and `x_label`,
    which is the project axis on a HorizontalBarChart) with the REAL
    `display_key`. The `_share_render_and_emit` wrapper then runs
    `_lib_share._scrub` BEFORE rendering — that's the single chokepoint
    that rewrites every project label to `project-1` / `project-2` /
    ... unless `--reveal-projects` is passed. The Section 8.4 canary
    test (`test_anonymized_output_contains_zero_original_tokens`) and
    the wrapper-level regression
    (`test_share_render_and_emit_scrubs_project_labels`) both anchor
    this contract.

    Deviations from the plan sketch (which assumed dict rows with keys
    `project` / `cost_usd` / `used_pct` / `sessions`):

    - Rows are dicts whose `key` field is a `ProjectKey` dataclass; the
      project label comes from `key.display_key`. The plan's `project`
      key does not exist on the actual `cmd_project` data shape.
    - `attributed_pct` may be `None` for projects whose contributing
      weeks all lacked a `weekly_usage_snapshots` row; the table renders
      that as em-dash (parity with terminal `_render_project_table`).
    - Sessions is a `set`; the cell carries its `len(...)` as text.

    `HorizontalBarChart.cap=12` matches the plan; when more than 12
    projects exist, a note clarifies that the table includes all rows
    while the chart shows only the top 12 by cost.

    Caller MUST pass `rows` already sorted in the desired order
    (cmd_project honors `--sort` / `--order` upstream). The builder
    preserves caller order for the table — terminal / JSON / share
    artifacts all show the same row ordering. Internally the builder
    ALSO computes a descending-cost copy that drives the HBar chart
    and the basename-disambiguation rank (both must match
    `_build_anon_mapping`'s descending-cost sort so `project-1` stays
    glued to the highest-cost bar regardless of `--sort`). Anonymization
    is row-identity based (`id(r)` → augmented label), not position
    based, so the table sees the same disambiguated label as the chart.

    `theme` and `reveal_projects` flow into the subtitle directly so the
    builder owns the canonical subtitle shape — no post-build re-stamp
    at the gate site.
    """
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="project", label="Project", align="left"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="used", label="% Used", align="right"),
        _lib_share.ColumnSpec(key="sessions", label="Sessions", align="right"),
    )
    # Two orderings — same rows, different consumers:
    #
    # * `rows` (caller order) drives the table. `cmd_project` upstream
    #   has already applied `--sort` / `--order`, so the share artifact's
    #   table matches terminal / JSON output for any of `--sort cost`,
    #   `--sort name`, `--sort sessions` × `--order asc|desc`.
    #
    # * `cost_sorted_rows` (descending cost) drives the HBar chart and
    #   the basename-disambiguation rank — both must align with
    #   `_build_anon_mapping`'s descending-cost sort so `project-1` stays
    #   glued to the highest-cost bar regardless of `--sort` choice.
    cost_sorted_rows = sorted(
        rows, key=lambda r: -float(r.get("cost_usd") or 0.0)
    )
    # Basename-collision disambiguation: mirrors `_render_project_table`'s
    # terminal logic. Computed on cost_sorted_rows; mapped back to row
    # identity so the caller-ordered table picks up the same augmented
    # label as the chart (anonymization is row-identity based, not
    # position based). Without disambiguation, two `app` projects under
    # different parent dirs collapse to ONE anonymous `project-N` after
    # scrub — losing both privacy uniqueness and chart rank meaning.
    augmented_by_index = _project_disambiguate_labels(cost_sorted_rows)
    augmented_by_row_id: dict[int, str] = {
        id(cost_sorted_rows[idx]): label
        for idx, label in augmented_by_index.items()
    }

    def _proj_label_for(r: dict) -> str:
        bare = getattr(r.get("key"), "display_key", None) or "(unknown)"
        return augmented_by_row_id.get(id(r), bare)

    # Table rows in CALLER order (--sort / --order parity).
    snap_rows: list = []
    for r in rows:
        proj_label = _proj_label_for(r)
        cost = float(r.get("cost_usd") or 0.0)
        attr_pct = r.get("attributed_pct")
        sessions = r.get("sessions")
        sessions_count = len(sessions) if sessions is not None else 0
        snap_rows.append(_lib_share.Row(cells={
            "project": _lib_share.ProjectCell(proj_label),
            "cost": _lib_share.MoneyCell(cost),
            # Preserve None vs 0.0 — terminal renders missing as em-dash.
            # Coercing None -> 0.0 would conflate "no usage snapshot for
            # any week this project touched" with "0% attributed."
            "used": (
                _lib_share.PercentCell(float(attr_pct))
                if attr_pct is not None else _lib_share.TextCell("—")
            ),
            "sessions": _lib_share.TextCell(str(sessions_count)),
        }))

    # Chart points in COST-SORTED order (HBar shows top-N by cost).
    chart_pts: list = []
    for r in cost_sorted_rows:
        proj_label = _proj_label_for(r)
        cost = float(r.get("cost_usd") or 0.0)
        chart_pts.append(_lib_share.ChartPoint(
            x_label=proj_label,
            x_value=cost,
            y_value=cost,
            project_label=proj_label,
        ))
    chart = (
        _lib_share.HorizontalBarChart(
            points=tuple(chart_pts), x_label="$", cap=12,
        )
        if chart_pts else None
    )
    notes: tuple[str, ...] = ()
    if chart is not None and len(chart_pts) > 12:
        notes = (
            f"Showing top 12 in chart; table includes all {len(chart_pts)}.",
        )
    sum_cost = sum(p.y_value for p in chart_pts)
    totals = (
        _lib_share.Totalled(label="Sum", value=f"${sum_cost:,.2f}"),
        _lib_share.Totalled(label="Projects", value=str(len(chart_pts))),
    )
    if rows:
        title = (
            f"Per-project usage — {period_start.strftime('%b %d')} → "
            f"{period_end.strftime('%b %d')}"
        )
    else:
        title = "Per-project usage — no data"
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="project",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=notes,
        generated_at=_share_now_utc(), version=version,
    )


def _build_five_hour_blocks_snapshot(
    view: "BlocksView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
    tz: "ZoneInfo | None",
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally five-hour-blocks`.

    `view` is the ``BlocksView`` produced by
    ``build_blocks_view_from_table_rows`` (issue #56). The
    API-anchored block dicts (sqlite Row → dict with the
    ``__is_active`` / ``__credits`` side-channels attached) live on
    ``view.aggregated``; reset-aware totals come from
    ``view.total_cost_usd`` so the share footer reads from the typed
    single source rather than re-summing inline. Schema fields used
    from each dict: ``block_start_at`` (ISO timestamp),
    ``total_cost_usd``, ``final_five_hour_percent``,
    ``crossed_seven_day_reset`` (0/1 int),
    ``seven_day_pct_at_block_start``, ``seven_day_pct_at_block_end``,
    plus the synthetic ``__is_active`` flag.

    Deviations from the plan sketch (which assumed dict rows with keys
    `block_start` / `cost_usd` / `used_pct_5h` / `top_model` /
    `cross_reset`):

    - Rows are sqlite-Row-derived dicts with snake_case schema column
      names — `block_start_at`, `total_cost_usd`,
      `final_five_hour_percent`, `crossed_seven_day_reset`. The plan
      keys `block_start` / `cost_usd` / `used_pct_5h` / `cross_reset`
      do not exist on the actual data shape.
    - `top_model` does not live on the `five_hour_blocks` row at all;
      `_load_breakdown` would have to be invoked per-block to derive
      it. Per share-spec convention (matches cmd_daily / cmd_monthly),
      the `--breakdown` flag is a no-op under `--format` and the
      headline snapshot omits the per-model "top model" column.
    - `crossed_seven_day_reset` is an INTEGER 0/1 (sqlite); coerce to
      `bool` for cell formatting.

    Cross-reset markers (spec §6.5):
      - `chart_pts` — `▲` (U+25B2) prefix in `x_label` so the SVG
        x-axis label visually flags the crossed-reset blocks.
      - `snap_rows` — `⚡` (U+26A1) glyph in the `cross_reset` cell
        text so the markdown / HTML table cell carries the same
        signal. The two glyphs are distinct (triangle for chart axis,
        bolt for table cell) so the legend reads correctly in either
        surface.

    `theme` and `reveal_projects` flow into the subtitle directly so
    the builder owns the canonical subtitle shape — no post-build
    re-stamp at the gate site.

    Caller MUST pass a view whose ``aggregated`` block dicts are
    already in the desired chronological order (cmd_five_hour_blocks
    pulls newest-first; we reverse here so the BarChart bars line up
    oldest→newest left-to-right). Tabular row order in the snapshot is
    irrelevant because the snapshot is what gets rendered (the gate
    site short-circuits the table renderer).
    """
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="block_start", label="Block Start",
                              align="left"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="used_pct", label="5h %",
                              align="right"),
        _lib_share.ColumnSpec(key="cross_reset", label="Reset",
                              align="left"),
    )
    # `view.aggregated` carries the newest-first DESC block dicts the
    # caller built from the SELECT. Reverse so BarChart x-axis runs
    # oldest→newest; table-row order tracks chart order so consumer
    # expectations align.
    rows = list(view.aggregated)
    chrono_rows = list(reversed(rows))
    snap_rows: list = []
    chart_pts: list = []
    for i, r in enumerate(chrono_rows):
        block_iso = r.get("block_start_at") or ""
        # Compact label respecting --tz; previously hard-coded to UTC
        # (parsed.strftime renders the wall-clock IN the parsed tz, and
        # `parsed` is tz-aware UTC after fromisoformat). UTC-vs-display_tz
        # is orthogonal to the SVG x-axis width budget — both render at
        # the same character count. Route through `format_display_dt`
        # with suffix=False to satisfy the chokepoint rule while keeping
        # the bar label compact (the subtitle's period_label already
        # carries the active tz).
        try:
            parsed = dt.datetime.fromisoformat(
                block_iso.replace("Z", "+00:00")
            )
            block_lbl = format_display_dt(
                parsed, tz, fmt="%b %d %H:%M", suffix=False,
            )
        except (ValueError, AttributeError):
            block_lbl = str(block_iso)
        cost_usd = float(r.get("total_cost_usd") or 0.0)
        used_pct = float(r.get("final_five_hour_percent") or 0.0)
        crossed = bool(r.get("crossed_seven_day_reset"))
        cell_text = "⚡" if crossed else "—"
        # Spec §5.1.1 (Codex r2 finding 3): consume the ``__credits``
        # side-channel set by ``cmd_five_hour_blocks`` and append a
        # ``⚡ -Xpp, -Ypp`` chip to the block_start cell. Pure-string
        # cell content flows uniformly through markdown / HTML table /
        # SVG text renderers without per-format additions. Symmetric to
        # the existing ⚡ glyph in the cross_reset cell — by position
        # (block_start suffix vs. dedicated column) the two annotations
        # remain visually distinguishable.
        credits = r.get("__credits") or []
        block_cell = block_lbl
        if credits:
            deltas = ", ".join(f"{c['deltaPp']:+.0f}pp" for c in credits)
            block_cell = f"{block_lbl} ⚡ {deltas}"
        snap_rows.append(_lib_share.Row(cells={
            "block_start": _lib_share.TextCell(block_cell),
            "cost": _lib_share.MoneyCell(cost_usd),
            "used_pct": _lib_share.PercentCell(used_pct),
            "cross_reset": _lib_share.TextCell(cell_text),
        }))
        x_label = f"▲ {block_lbl}" if crossed else block_lbl
        chart_pts.append(_lib_share.ChartPoint(
            x_label=x_label,
            x_value=float(i),
            y_value=cost_usd,
        ))
    chart = (
        _lib_share.BarChart(points=tuple(chart_pts), y_label="$")
        if chart_pts else None
    )
    # Reset-aware total comes from the BlocksView (issue #56); avg
    # divides by `chart_pts` count so the share footer "Sum" totalled
    # and the per-block `chart_pts` cost values share a single source-
    # of-truth at `view.total_cost_usd`.
    sum_cost = view.total_cost_usd
    avg_cost = (sum_cost / len(chart_pts)) if chart_pts else 0.0
    crossed_count = sum(
        1 for r in chrono_rows if bool(r.get("crossed_seven_day_reset"))
    )
    totals_list = [
        _lib_share.Totalled(label="Sum", value=f"${sum_cost:,.2f}"),
        _lib_share.Totalled(label="Blocks", value=str(len(chart_pts))),
        _lib_share.Totalled(label="Avg / block", value=f"${avg_cost:,.2f}"),
    ]
    if crossed_count:
        totals_list.append(_lib_share.Totalled(
            label="Crossed reset", value=str(crossed_count),
        ))
    totals = tuple(totals_list)
    notes: tuple[str, ...] = ()
    if crossed_count:
        notes = (
            "▲ / ⚡ marks blocks that crossed the weekly reset boundary.",
        )
    if rows:
        title = f"5-hour blocks — last {len(rows)} blocks"
    else:
        title = "5-hour blocks — no data"
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="five-hour-blocks",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=notes,
        generated_at=_share_now_utc(), version=version,
    )


def _session_disambiguate_labels(
    sessions: list["ClaudeSessionUsage"],
) -> dict[int, str]:
    """Return ``{session_index: disambiguated_label}`` for sessions whose
    bare ``project_path`` basename collides with another session's.

    Session-specific sibling of ``_project_disambiguate_labels`` (which
    operates over project rollup rows whose `key` is a ``ProjectKey``).
    Sessions carry only a `project_path` string — we derive the
    basename, count collisions, and append a parent-dir suffix
    ``" (parent)"`` to colliding rows so the post-scrub anonymization
    still produces unique anonymous labels (otherwise two `app/`
    sessions under different parents collapse to a single
    ``project-N``, breaking both privacy uniqueness and the chart's
    visual rank meaning).

    Sessions without collisions are absent from the returned dict;
    callers fall back to the bare basename.
    """
    basenames: list[str] = []
    for s in sessions:
        path = s.project_path or ""
        basenames.append(os.path.basename(path) or path or "(unknown)")
    counts: dict[str, int] = {}
    for bn in basenames:
        counts[bn] = counts.get(bn, 0) + 1
    augmented: dict[int, str] = {}
    for idx, s in enumerate(sessions):
        bn = basenames[idx]
        # Skip suffixing the literal "(unknown)" bare label even on
        # collision: `_build_anon_mapping` literal-passthrough-protects
        # exact "(unknown)" only — a suffixed form like "(unknown) (/)"
        # would be mapped to a regular `project-N` slot, losing the
        # (unknown) semantic in the anonymized output.
        if counts[bn] > 1 and bn != "(unknown)":
            path = s.project_path or ""
            parent = os.path.basename(os.path.dirname(path)) or "/"
            augmented[idx] = f"{bn} ({parent})"
    return augmented


def _build_session_snapshot(
    view: "SessionsView",
    *,
    period_start: dt.datetime,
    period_end: dt.datetime,
    display_tz: str,
    version: str,
    theme: str,
    reveal_projects: bool,
    top_n: int | None,
    tz: "ZoneInfo | None",
) -> "ShareSnapshot":
    """Build a ShareSnapshot for `cctally session`.

    Consumes the unified ``SessionsView`` (spec §6.5). ``view.aggregated``
    is the ``ClaudeSessionUsage`` tuple — the shape this builder needs
    for ``source_paths`` / ``model_breakdowns`` / ``last_activity``
    (fields ``view.rows`` / ``TuiSessionRow`` doesn't carry). The
    in-memory shape is unchanged at the read boundary — only the
    parameter container differs.

    Each ``ClaudeSessionUsage`` has: ``session_id`` (UUID),
    ``project_path`` (filesystem path), ``cost_usd``,
    ``last_activity`` (``dt.datetime``), ``models`` (first-seen-order
    ``list[str]``), and the token aggregates.

    Privacy invariant (Section 8.4 / Section 5.3): the builder populates
    `ProjectCell.label`, `ChartPoint.project_label`, and
    `ChartPoint.x_label` with the REAL `project_path` basename. The
    `_share_render_and_emit` wrapper runs `_lib_share._scrub` BEFORE
    rendering — that's the single chokepoint that rewrites every
    project label to `project-1` / `project-2` / ... unless
    `--reveal-projects` is passed.

    Deviations from the plan sketch (which assumed dict rows with keys
    `session_id` / `started_at` / `project_path` / `cost_usd` /
    `models`):

    - Sessions are `ClaudeSessionUsage` dataclasses; we read fields by
      attribute. `last_activity` is the canonical timestamp (no
      `started_at` field — sessions span a window via
      `first_activity` → `last_activity`).
    - The `project_path` column's basename can collide across two
      different parent dirs. We use the session-specific
      `_session_disambiguate_labels` helper (sibling of
      `_project_disambiguate_labels`, which expects `ProjectKey` rows
      not present on session data) to suffix `" (parent)"` on
      collisions before the scrubber runs.

    Caller MUST pass ``view`` whose ``aggregated`` tuple is already
    sorted in the desired order (``cmd_session`` keeps the
    aggregator's descending-by-last_activity sort); the builder
    re-sorts internally by descending cost so the chart's HBar bars
    rank consistently with the anonymization-mapping
    (``_build_anon_mapping`` also sorts by descending cost) — keeping
    ``project-1`` aligned with the highest-cost bar in the chart even
    when the user asked for ``--order asc``.

    `top_n`, when set (must be `>= 1`; caller validates), truncates
    BOTH the table rows and the chart points to the top-N by cost.
    The title shifts to `"Top N sessions"` whenever `top_n` actually
    truncated (so users know rows were dropped). When more rows exist
    than the chart cap (15) but `top_n` is None or `>= len(sessions)`,
    the table includes all rows while the chart shows the top 15 by
    cost (a note clarifies).

    `theme` and `reveal_projects` flow into the subtitle directly so
    the builder owns the canonical subtitle shape — no post-build
    re-stamp at the gate site.
    """
    _lib_share = _share_load_lib()
    columns = (
        _lib_share.ColumnSpec(key="session", label="Session", align="left"),
        _lib_share.ColumnSpec(key="project", label="Project", align="left"),
        _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right",
                              emphasis=True),
        _lib_share.ColumnSpec(key="last_activity", label="Last Activity",
                              align="left"),
        _lib_share.ColumnSpec(key="models", label="Models", align="left"),
    )
    # Sort by descending cost so the snapshot's chart-order matches the
    # `_build_anon_mapping` sort key (also descending cost).
    sorted_sessions = sorted(
        view.aggregated,
        key=lambda s: -float(getattr(s, "cost_usd", 0.0) or 0.0),
    )
    # Apply --top-n truncation (caller validated >= 1). Truncation status
    # gates the title shape below.
    truncated = (
        top_n is not None and top_n < len(sorted_sessions)
    )
    if top_n is not None:
        sorted_sessions = sorted_sessions[:top_n]
    # Basename-collision disambiguation: session-specific sibling of
    # `_project_disambiguate_labels`. Without this, two `app/` sessions
    # under different parents collapse to a single `project-N` after
    # scrub — losing both privacy uniqueness and chart rank meaning.
    augmented = _session_disambiguate_labels(sorted_sessions)
    snap_rows: list = []
    chart_pts: list = []
    for idx, s in enumerate(sorted_sessions):
        bare_label = (
            os.path.basename(s.project_path or "")
            or s.project_path
            or "(unknown)"
        )
        proj_label = augmented.get(idx, bare_label)
        cost_usd = float(getattr(s, "cost_usd", 0.0) or 0.0)
        sid_short = (s.session_id[:8] if s.session_id else "—") or "—"
        # Datetime chokepoint rule: route human-displayed timestamps
        # through `format_display_dt` so `--tz` is honored (was
        # `.astimezone()` which used host-local regardless of `--tz`).
        # `suffix=False` keeps the cell width tight — the subtitle's
        # period_label already carries the active tz.
        last_str = format_display_dt(
            s.last_activity, tz, fmt="%Y-%m-%d %H:%M", suffix=False,
        )
        models_text = ", ".join(s.models) if s.models else "—"
        snap_rows.append(_lib_share.Row(cells={
            "session": _lib_share.TextCell(sid_short),
            "project": _lib_share.ProjectCell(proj_label),
            "cost": _lib_share.MoneyCell(cost_usd),
            "last_activity": _lib_share.TextCell(last_str),
            "models": _lib_share.TextCell(models_text),
        }))
        chart_pts.append(_lib_share.ChartPoint(
            x_label=proj_label,
            x_value=cost_usd,
            y_value=cost_usd,
            project_label=proj_label,
        ))
    chart = (
        _lib_share.HorizontalBarChart(
            points=tuple(chart_pts), x_label="$", cap=15,
        )
        if chart_pts else None
    )
    notes: tuple[str, ...] = ()
    if chart is not None and len(chart_pts) > 15:
        notes = (
            f"Showing top 15 in chart; table includes all {len(chart_pts)}.",
        )
    sum_cost = sum(p.y_value for p in chart_pts)
    totals = (
        _lib_share.Totalled(label="Sum", value=f"${sum_cost:,.2f}"),
        _lib_share.Totalled(label="Sessions", value=str(len(chart_pts))),
    )
    if sorted_sessions:
        if truncated:
            title = f"Top {len(snap_rows)} sessions"
        else:
            title = (
                f"Sessions — {period_start.strftime('%b %d')} → "
                f"{period_end.strftime('%b %d')}"
            )
    else:
        title = "Sessions — no data"
    period_label = _share_period_label(period_start, period_end, display_tz)
    subtitle = " · ".join([
        period_label,
        theme,
        "real projects" if reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="session",
        title=title,
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=period_start, end=period_end,
            display_tz=display_tz, label=period_label,
        ),
        columns=columns, rows=tuple(snap_rows),
        chart=chart, totals=totals, notes=notes,
        generated_at=_share_now_utc(), version=version,
    )


# ---- v2 share panel_data builders (spec §5.2, plan M1.6) -------------
#
# These translate the live dashboard `DataSnapshot` into the dict shapes
# the M1.4 Recap builders (in `bin/_lib_share_templates.py`) consume.
# They're a thin extract step — the DataSnapshot was already built by
# the sync thread, so this path doesn't re-query the DB on the share
# hot path.
#
# Per-panel shape contracts live in each Recap builder's docstring in
# `bin/_lib_share_templates.py` (see `_build_<panel>_recap`); the keys
# below MUST stay in lockstep with those docstrings — the
# producer/consumer contract.
#
# When the snapshot has no data for a given panel (fresh install, no
# sync yet), the builder returns a minimal empty-shaped dict that the
# downstream Recap builder renders as a "no data" snapshot (kernel
# handles empty `weeks=[]` / `days=[]` / etc.).


def _share_iso(value) -> "str | None":
    """Coerce a datetime / ISO-string into an ISO-8601 string with `Z` suffix.

    DataSnapshot mixes attribute types (`week_start_at` is a
    `dt.datetime`; `WeeklyPeriodRow.week_start_at` is already a string).
    Recap builders' `_parse_iso_utc` accepts both shapes via fromisoformat
    + `Z`-swap, but normalizing here keeps the wire format consistent.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        v = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return v.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


# ---- Period override (spec §6.2 Q4 + Codex P2 on PR #35) ----
#
# The share modal's Period control offers three kinds — current, previous,
# custom — but the original render path consumed the dashboard's cached
# DataSnapshot directly, which only ever holds "current" data. Override
# semantics by panel:
#
#   panel        current             previous              custom (start/end)
#   --------     ------------------  --------------------  -------------------
#   weekly       this subscription   one week earlier      week containing end
#                week
#   daily        last 7 display-tz   7 days earlier        7 days ending at end
#                days ending today
#   monthly      last 12 months      12 months earlier     12 months ending at end
#                ending now
#   trend        last 8 weeks        8 weeks earlier       8 weeks ending at end
#                ending now
#   blocks       recent 5h blocks    blocks ending one     blocks ending at end
#                                     5h-window earlier
#   forecast     future projection   (rejected: previous
#                from now             forecast doesn't exist)
#   current-week this subscription   (rejected: panel IS current)
#                week
#   sessions     recent sessions     (deferred: ambiguous semantics — could
#                                     mean "older sessions" or "sessions in
#                                     date range"; revisit when use case clear)
#
# Override mechanics: derive a `now_utc` from the period option and
# re-build only the relevant DataSnapshot field by calling the same
# `_dashboard_build_*` function the sync thread uses, just with a
# shifted `now_utc`. `dataclasses.replace` returns a new DataSnapshot
# with that field swapped; everything downstream (panel_data builder,
# template builder, kernel render) consumes it unchanged.
#
# Validation failures land on the request as HTTP 400 with
# `field: "options.period.<key>"` so the UI can highlight the offending
# control.

def _share_render_and_emit(snap, args) -> None:
    """End-to-end: scrub -> render -> emit -> optional open.

    Lazy-imports `_lib_share` so non-share invocations don't pay the import
    cost. The kernel module stays I/O-pure; this wrapper does all the
    side-effecting glue (destination resolution, file writes, clipboard,
    post-write `--open` launch).

    Caller contract: ``args.format`` MUST be set ("md", "html", or "svg").
    The wrapper raises ValueError if called without it — surfaces the
    contract failure at the chokepoint instead of producing junk filenames
    like ``cctally-daily-<date>.None``.
    """
    if args.format is None:
        raise ValueError("_share_render_and_emit called without args.format")
    if args.open_after_write and args.format == "md":
        # Spec Section 4.4: --open is only meaningful for html/svg writes.
        # Reject explicitly with exit 2 instead of silently no-opping (which
        # the prior implementation did because the open-after-write branch
        # gates on ``kind == "file"``, and md routes to stdout by default).
        print(
            "cctally: --open is only valid with --format html or --format svg",
            file=sys.stderr,
        )
        sys.exit(2)
    # Routed through `_share_load_lib` so wrapper / builders / test harness
    # share one cached module object — see helper docstring for the
    # class-identity invariant this enforces.
    _lib_share = _share_load_lib()

    scrubbed = _lib_share._scrub(snap, reveal_projects=args.reveal_projects)
    rendered = _lib_share.render(
        scrubbed,
        format=args.format,
        theme=args.theme,
        branding=not args.no_branding,
    )

    utc_date = snap.generated_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    kind, value = _resolve_destination(args, cmd=snap.cmd, generated_at_utc_date=utc_date)
    _emit(rendered, kind=kind, value=value)

    if args.open_after_write and kind == "file":
        _share_open_file(pathlib.Path(value))


def _share_open_file(path: pathlib.Path) -> None:
    """Run `open` (macOS) / `xdg-open` (Linux). Silent fail if launcher missing."""
    for launcher in ("open", "xdg-open"):
        if shutil.which(launcher):
            subprocess.Popen(
                [launcher, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
    sys.stderr.write("cctally: --open requires `open` or `xdg-open` on PATH; skipped\n")
