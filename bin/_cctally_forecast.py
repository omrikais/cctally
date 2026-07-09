"""Forecast + Budget + Report command family.

Holds `cmd_report`, `cmd_forecast`, `cmd_budget`, the forecast core cluster
(window resolvers, ForecastInputs/ForecastOutput/BudgetRow, _compute_forecast,
the forecast render helpers, _iso_z), and the 10 budget render/snapshot helpers.

Honest *name* imports are KERNEL-ONLY (_cctally_core) + stdlib. Every library
kernel + sibling helper this module needs (build_forecast_view, build_trend_view,
compute_budget_status, project_linear, _build_{forecast,report,budget}_snapshot,
_share_*, format_display_dt, resolve_display_tz, get_recent_weeks,
_reconcile_budget_on_config_write, ...) is reached via the call-time _cctally()
accessor so test monkeypatches through cctally's namespace are preserved (§2).

The budget WRITE-PATH cluster (insert_budget_milestone,
_reconcile_budget_milestones_on_set, _reconcile_budget_on_config_write) lives in
_cctally_milestones.py (re-exported on the cctally ns), and the WeekRef JOIN
cluster (get_recent_weeks, _apply_reset_events_to_weekrefs,
_get_canonical_boundary_for_date) lives in _cctally_weekrefs.py (re-exported on
the cctally ns); both are reached via c.

_iso_z is defined HERE (intra-module) and re-exported in bin/cctally AFTER the
dashboard _iso_z binding so cctally._iso_z stays the forecast version
(the dt-only variant _lib_diff_kernel + _cctally_five_hour depend on).

Spec: docs/superpowers/specs/2026-05-31-extract-forecast-budget-cmd-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, replace

from _cctally_core import (
    WEEKDAY_MAP, _command_as_of, _normalize_week_boundary_dt,
    compute_week_bounds, eprint, get_week_start_name, make_week_ref,
    now_utc_iso, open_db, parse_iso_datetime,
)
# Non-kernel _cctally_core re-exports used by the moved code. Verified NOT
# ns-patched (§5.A gate); honest-import permitted (the §8.1b gate allows any
# _cctally_core import; neither is in KERNEL_SYMBOLS or PROMOTED_GLOBALS, so the
# kernel-invariant tests are unaffected). _BudgetConfigError is an exception
# class — honest-import avoids the except-over-accessor foot-gun
# (gotcha_except_over_callable_shim_typeerrors).
from _cctally_core import (
    _BudgetConfigError, _get_budget_config,
    BUDGET_PERIODS as _CCTALLY_BUDGET_PERIODS,
)

import importlib.util as _ilu


def _ensure_sibling_loaded(name: str) -> None:
    """Register a NON-eager-loaded ``_lib_*`` sibling in ``sys.modules``.

    ``_lib_forecast`` (#279 S4 F2) is a NEW consumer-only sibling —
    deliberately kept out of ``bin/cctally``'s eager-load block so
    ``bin/cctally`` stays byte-untouched (spec §2 re-export continuity).
    Under the ``SourceFileLoader`` harness path (``bin/`` absent from
    ``sys.path``) a bare ``from _lib_forecast import`` would miss, so this
    pre-registers the sibling ``__file__``-relative when it is not already
    importable (mirrors ``_cctally_cache._load_lib``). The honest import
    that follows is a ``sys.modules`` hit in every load context.
    """
    if name in sys.modules:
        return
    try:
        __import__(name)  # bin/ on sys.path: prod script / conftest / pytest
        return
    except ModuleNotFoundError:
        pass
    _p = os.path.join(os.path.dirname(__file__), f"{name}.py")
    _spec = _ilu.spec_from_file_location(name, _p)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules[name] = _mod
    _spec.loader.exec_module(_mod)


_ensure_sibling_loaded("_lib_forecast")
from _lib_forecast import ForecastInputs, BudgetRow, ForecastOutput, _compute_forecast


def _cctally():
    """Resolve the current `cctally` module at call-time (§2)."""
    return sys.modules["cctally"]


# ``ForecastInputs`` / ``BudgetRow`` / ``ForecastOutput`` / ``_compute_forecast``
# now live in ``bin/_lib_forecast.py`` (#279 S4 F2); honest-imported at module
# top so the ``bin/cctally`` re-exports and this module's own callers resolve
# them unchanged.


def _resolve_forecast_now(as_of: str | None) -> dt.datetime:
    """Return `now` as a UTC-aware datetime, honoring --as-of for tests.

    When ``--as-of`` is omitted, delegates to ``_command_as_of()`` so the
    CCTALLY_AS_OF env var (the canonical testing hook shared with weekly,
    cache-report, codex-weekly, and project) is also honored. Wall-clock
    behavior is preserved when neither mechanism is set.
    """
    if as_of:
        return parse_iso_datetime(as_of, "--as-of").astimezone(dt.timezone.utc)
    return _command_as_of()


def _fetch_current_week_snapshots(conn: sqlite3.Connection, now_utc: dt.datetime):
    """Return (week_start_at, week_end_at, list[(captured_at, percent, five_hr)])
    for the subscription week containing `now_utc`, or None if no snapshot
    exists for the current week.

    Selection: the snapshot whose [week_start_at, week_end_at) contains
    now_utc; ties (none expected) broken by max(captured_at_utc).

    Includes a date-only fallback when no boundary-aware row matches —
    synthesizes a UTC window from week_start_date/week_end_date at local
    midnight so installs that only have legacy date-based rows for the active
    week still get a forecast.

    Same-week NULL-timestamp rows (week_start_at IS NULL AND week_start_date
    matches chosen) are folded in as well — upgrade-window mid-migration rows
    stay visible.

    Samples are filtered to `captured_at <= now_utc` so that `--as-of <past>`
    is deterministic (no leak of future snapshots into samples[-1] / p_now /
    snapshot_count / latest_snapshot_at).
    """
    candidates = conn.execute(
        "SELECT week_start_at, week_end_at, week_start_date, MAX(captured_at_utc) AS latest_cap "
        "FROM weekly_usage_snapshots "
        "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL "
        "GROUP BY week_start_at, week_end_at, week_start_date"
    ).fetchall()
    chosen = None
    chosen_cap = None
    for r in candidates:
        try:
            ws = parse_iso_datetime(r[0], "week_start_at")
            we = parse_iso_datetime(r[1], "week_end_at")
        except ValueError:
            continue
        if ws <= now_utc < we:
            cap = r[3]
            if chosen is None or (cap is not None and (chosen_cap is None or cap > chosen_cap)):
                chosen = r
                chosen_cap = cap
    if chosen is None:
        # Date-only fallback: find a week whose [week_start_date, week_end_date]
        # (inclusive) contains today's local date. Synthesize a UTC window from
        # those dates at local midnight.
        # internal fallback: host-local intentional
        today_local_str = now_utc.astimezone().date().isoformat()
        drow = conn.execute(
            "SELECT week_start_date, week_end_date "
            "FROM weekly_usage_snapshots "
            "WHERE week_start_date <= ? AND week_end_date >= ? "
            "GROUP BY week_start_date, week_end_date "
            "ORDER BY MAX(captured_at_utc) DESC LIMIT 1",
            (today_local_str, today_local_str),
        ).fetchone()
        if drow is None:
            return None
        # internal fallback: host-local intentional
        local_tz = dt.datetime.now().astimezone().tzinfo
        ws_date = dt.date.fromisoformat(drow[0])
        we_date = dt.date.fromisoformat(drow[1])
        week_start_at = dt.datetime.combine(ws_date, dt.time(0, 0), local_tz).astimezone(dt.timezone.utc)
        week_end_at = dt.datetime.combine(we_date + dt.timedelta(days=1), dt.time(0, 0), local_tz).astimezone(dt.timezone.utc)
        rows = conn.execute(
            "SELECT captured_at_utc, weekly_percent, five_hour_percent "
            "FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "ORDER BY captured_at_utc ASC",
            (drow[0],),
        ).fetchall()
        samples = [
            (parse_iso_datetime(r[0], "captured_at_utc"), float(r[1]),
             float(r[2]) if r[2] is not None else None)
            for r in rows
        ]
        samples = [s for s in samples if s[0] <= now_utc]
        return week_start_at, week_end_at, samples
    row = chosen
    week_start_at = parse_iso_datetime(row[0], "week_start_at")
    week_end_at = parse_iso_datetime(row[1], "week_end_at")
    # Collect every textual variant of week_start_at that parses to the same
    # instant as the chosen row, so legacy local-offset rows and newly
    # UTC-canonicalized rows for the SAME week are loaded together during an
    # upgrade-in-progress window.
    matching_texts: list[str] = []
    for r in candidates:
        try:
            rws = parse_iso_datetime(r[0], "week_start_at")
        except ValueError:
            continue
        if rws == week_start_at:
            matching_texts.append(r[0])
    chosen_date = chosen[2]
    placeholders = ",".join("?" * len(matching_texts))
    rows = conn.execute(
        f"SELECT captured_at_utc, weekly_percent, five_hour_percent "
        f"FROM weekly_usage_snapshots "
        f"WHERE week_start_at IN ({placeholders}) "
        f"   OR (week_start_at IS NULL AND week_start_date = ?) "
        f"ORDER BY captured_at_utc ASC",
        tuple(matching_texts) + (chosen_date,),
    ).fetchall()
    samples = [
        (parse_iso_datetime(r[0], "captured_at_utc"), float(r[1]),
         float(r[2]) if r[2] is not None else None)
        for r in rows
    ]
    samples = [s for s in samples if s[0] <= now_utc]
    return week_start_at, week_end_at, samples


def _apply_midweek_reset_override(
    conn: sqlite3.Connection,
    week_start_at: dt.datetime,
    week_end_at: dt.datetime,
    samples: list,
) -> tuple[dt.datetime, list]:
    """If the current week's end_at matches a recorded reset event's
    ``new_week_end_at``, shift ``week_start_at`` to the effective reset
    moment and drop pre-reset samples.

    Keeps callers (``_load_forecast_inputs``, ``_tui_build_current_week``)
    from reporting spent_usd summed across the pre-reset window.

    Returns the (possibly-shifted) ``week_start_at`` and a
    (possibly-filtered) samples list. On any SQL or parse error, returns
    the inputs unchanged — the override is best-effort.
    """
    try:
        end_iso = _normalize_week_boundary_dt(
            week_end_at.astimezone(dt.timezone.utc)
        ).isoformat(timespec="seconds")
        event_row = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events "
            "WHERE new_week_end_at = ?",
            (end_iso,),
        ).fetchone()
        if event_row and event_row["effective_reset_at_utc"]:
            reset_dt = parse_iso_datetime(
                event_row["effective_reset_at_utc"], "reset_event.effective"
            )
            if reset_dt > week_start_at:
                week_start_at = reset_dt
                samples = [s for s in samples if s[0] >= reset_dt]
    except (sqlite3.DatabaseError, ValueError):
        pass
    return week_start_at, samples


def _resolve_current_budget_window(conn, now_utc):
    """Return ``(effective_week_start_dt, week_end_dt)`` for the subscription
    week containing ``now_utc``, honoring a mid-week reset re-anchor; or
    ``None`` if no snapshot exists yet.

    Reuses the SAME reset-aware resolution forecast/weekly use
    (``_fetch_current_week_snapshots`` + ``_apply_midweek_reset_override``)
    so the budget display window and the alert-firing window (Task 3) agree.
    Unlike forecast's ``_load_forecast_inputs``, this does NOT short-circuit
    on an empty samples list — budget computes live spend from
    ``session_entries`` regardless of whether a usage snapshot landed inside
    the window, so the worst case is ``spent_usd = 0`` (spec §6), not a
    no-window outcome.
    """
    fetched = _fetch_current_week_snapshots(conn, now_utc)
    if fetched is None:
        return None
    week_start_at, week_end_at, samples = fetched
    week_start_at, _samples = _apply_midweek_reset_override(
        conn, week_start_at, week_end_at, samples
    )
    return (week_start_at, week_end_at)


def _build_budget_status_inputs(
    conn, *, target_usd, now_utc, alert_thresholds, skip_sync=False
):
    """Gather live spend over the current subscription week and build a
    :class:`BudgetInputs`. Returns ``None`` when no week window resolves.

    Spend is recomputed live via ``_sum_cost_for_range(..., mode="auto")``
    (the same path ``weekly`` / ``forecast`` use — pricing edits take effect
    immediately; F3's reconcile invariant is pinned here, NOT to snapshot
    ``report``). ``recent_24h_usd`` is a second trailing-24h call that is NOT
    display-only: in ``_lib_budget.compute_budget_status`` it feeds
    ``rate_recent → rate_high → projected_high → projected``, which drives the
    ok/warn/over verdict. It is therefore clamped to the current budget week
    (``max(week_start_at, now - 24h)``) so a heavy spend just before reset
    can't leak last week's dollars into a fresh week's verdict (false
    WARN/OVER).

    ``skip_sync`` skips the JSONL ingest pass inside ``get_entries`` for BOTH
    cost SUMs — used by the projected-pace record path, where the actual-budget
    axis already ran ``_sum_cost_for_range`` (warming the cache) earlier in the
    same ``cmd_record_usage`` tick. The default ``False`` preserves the
    standalone ``budget`` command's sync-on-read behavior unchanged.
    """
    c = _cctally()
    window = _resolve_current_budget_window(conn, now_utc)
    if window is None:
        return None
    week_start_at, week_end_at = window
    spent = c._sum_cost_for_range(
        week_start_at, now_utc, mode="auto", skip_sync=skip_sync
    )
    # Clamp the recent-rate window at the week start: both bounds are tz-aware
    # so max() is well-defined. Without this, a brand-new week (now < week
    # start + 24h) would pull pre-reset spend into rate_recent and flip a
    # fresh verdict to warn/over.
    recent_start = max(week_start_at, now_utc - dt.timedelta(hours=24))
    recent_24h = c._sum_cost_for_range(
        recent_start, now_utc, mode="auto", skip_sync=skip_sync
    )
    return c.BudgetInputs(
        target_usd=float(target_usd),
        spent_usd=float(spent),
        recent_24h_usd=float(recent_24h),
        week_start_at=week_start_at,
        week_end_at=week_end_at,
        now=now_utc,
        alert_thresholds=tuple(alert_thresholds),
    )


def _select_dollars_per_percent(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    current_week_start: dt.datetime,
    p_now: float,
    spent_usd: float,
    *,
    skip_sync: bool = False,
    use_weekref_cost_cache: bool = False,
) -> tuple[float, str]:
    """Return (dollars_per_percent, source_label). See spec §1 selection rule.

    Eligible prior week: week_end_at < now_utc AND final_weekly_percent >= 1.
    Uses the existing `_sum_cost_for_range` helper (which opens the cache DB
    via `get_entries`); `conn` is only used for snapshot queries.
    """
    c = _cctally()
    # Path 1: current week, stable sample.
    if p_now >= 10.0 and p_now > 0:
        return spent_usd / p_now, "this_week"

    # Path 2: trailing 4-week median.
    # Fetch all eligible prior weeks then Python-filter (legacy rows may carry
    # non-UTC offsets that break lexical compare against an ISO-UTC bound).
    rows = conn.execute(
        "SELECT week_start_at, week_end_at, MAX(weekly_percent) AS final_pct, "
        "       MAX(captured_at_utc) AS latest_cap "
        "FROM weekly_usage_snapshots "
        "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL "
        "GROUP BY week_start_at "
        "HAVING MAX(weekly_percent) >= 1"
    ).fetchall()
    by_instant: dict[dt.datetime, dict] = {}
    for r in rows:
        try:
            ws = parse_iso_datetime(r[0], "week_start_at")
            we = parse_iso_datetime(r[1], "week_end_at")
        except ValueError:
            continue
        slot = by_instant.get(ws)
        final_pct = float(r[2])
        if slot is None:
            by_instant[ws] = {"we": we, "final_pct": final_pct}
        else:
            slot["final_pct"] = max(slot["final_pct"], final_pct)

    eligible: list[tuple[dt.datetime, dt.datetime, float]] = [
        (ws, v["we"], v["final_pct"])
        for ws, v in by_instant.items()
        if ws < current_week_start and v["we"] < now_utc and v["final_pct"] >= 1.0
    ]
    eligible.sort(key=lambda x: x[0], reverse=True)
    prior = eligible[:4]
    if len(prior) >= 4:
        import statistics
        values: list[float] = []
        for ws, we, final_pct in prior:
            if use_weekref_cost_cache:
                # #269 §4: every `prior` week satisfies `we < now_utc` (an
                # eligibility filter above), so all four are CLOSED and
                # cacheable — this is the SAME immutable per-weekref cost the
                # trend builder caches (B1↔B3 shared key). The `ws=ws, we=we`
                # default-arg capture pins the loop variables (the closure is
                # invoked lazily inside cached_weekref_cost). `skip_sync=True`
                # unconditionally (the dashboard synced once at the top of the
                # rebuild; the flag is only True there).
                _sc = c._load_sibling("_lib_snapshot_cache")
                week_cost = _sc.cached_weekref_cost(
                    week_start_at=ws, week_end_at=we, now_utc=now_utc,
                    compute=lambda ws=ws, we=we: c._sum_cost_for_range(
                        ws, we, mode="auto", skip_sync=True
                    ),
                )
            else:
                week_cost = c._sum_cost_for_range(
                    ws, we, mode="auto", skip_sync=skip_sync
                )
            values.append(week_cost / final_pct)
        return statistics.median(values), "trailing_4wk_median"

    # Path 3: fall back to current week even if sparse.
    if p_now > 0:
        return spent_usd / p_now, "this_week_sparse"
    # p_now == 0: no signal. Return 0; math layer guards against div-by-zero.
    return 0.0, "this_week_sparse"


def _assess_forecast_confidence(
    elapsed_hours: float, p_now: float, snapshot_count: int
) -> tuple[str, list[str]]:
    """Binary confidence (spec §2)."""
    reasons: list[str] = []
    if elapsed_hours < 24:
        reasons.append("elapsed_hours<24")
    if p_now < 2:
        reasons.append("percent<2")
    if snapshot_count < 3:
        reasons.append("snapshots<3")
    return ("low", reasons) if reasons else ("high", [])


def _pick_p_24h_ago(
    samples: list[tuple[dt.datetime, float, float | None]],
    now_utc: dt.datetime,
) -> tuple[float | None, float | None]:
    """Return (p_24h_ago, t_24h_actual_hours). Closest to (now_utc - 24h)
    by absolute time delta. t_24h_actual_hours can be <24h even when
    ≥24h samples exist — that is fine; the confidence logic (spec §2)
    keys on sample availability, not on the picked sample's age.
    Returns (None, None) if there are no samples."""
    if not samples:
        return None, None
    target = now_utc - dt.timedelta(hours=24)
    pick = min(samples, key=lambda s: abs((s[0] - target).total_seconds()))
    t_actual = (now_utc - pick[0]).total_seconds() / 3600.0
    return pick[1], t_actual


def _load_forecast_inputs(
    conn: sqlite3.Connection,
    now_utc: dt.datetime,
    *,
    skip_sync: bool = False,
    use_weekref_cost_cache: bool = False,
) -> ForecastInputs | None:
    """Gather everything from the DB. Returns None if no current-week snapshot.

    When `skip_sync=True`, all JSONL-backed cost lookups skip the ingest pass
    and serve whatever is already in the cache (honors `forecast --no-sync`).
    """
    c = _cctally()
    fetched = _fetch_current_week_snapshots(conn, now_utc)
    if fetched is None:
        return None
    week_start_at, week_end_at, samples = fetched
    if not samples:
        return None

    # Mid-week reset override: shift week_start_at to the effective
    # reset moment and drop pre-reset samples so elapsed/remaining math
    # and spent_usd reflect the post-reset window only.
    week_start_at, samples = _apply_midweek_reset_override(
        conn, week_start_at, week_end_at, samples
    )

    if not samples:
        return None
    latest = samples[-1]
    p_now = latest[1]
    five_hr = latest[2]

    elapsed_hours = (now_utc - week_start_at).total_seconds() / 3600.0
    total_hours = (week_end_at - week_start_at).total_seconds() / 3600.0
    elapsed_fraction = max(0.01, min(0.99, elapsed_hours / total_hours if total_hours else 0.5))
    remaining_hours = max(0.0, (week_end_at - now_utc).total_seconds() / 3600.0)
    remaining_days = remaining_hours / 24.0

    # Live compute current-week spend via the existing helper (opens cache.db
    # internally); mirrors `weekly`'s pattern of not trusting weekly_cost_snapshots.
    spent_usd = c._sum_cost_for_range(
        week_start_at, now_utc, mode="auto", skip_sync=skip_sync
    )
    p_24h_ago, t_24h = _pick_p_24h_ago(samples, now_utc)

    # Cache is warm for this invocation after the spent_usd lookup. Suppress
    # re-syncs in downstream cost lookups (trailing-4wk-median loop hits
    # _sum_cost_for_range once per historical week).
    dpp, dpp_source = _select_dollars_per_percent(
        conn, now_utc, week_start_at, p_now, spent_usd, skip_sync=True,
        use_weekref_cost_cache=use_weekref_cost_cache,
    )
    confidence, reasons = _assess_forecast_confidence(elapsed_hours, p_now, len(samples))
    target_24h = now_utc - dt.timedelta(hours=24)
    has_sample_ge_24h = any(s[0] <= target_24h for s in samples)
    if not has_sample_ge_24h:
        reasons = list(reasons) + ["no_sample_ge_24h"]
        confidence = "low"

    return ForecastInputs(
        now_utc=now_utc,
        week_start_at=week_start_at,
        week_end_at=week_end_at,
        elapsed_hours=elapsed_hours,
        elapsed_fraction=elapsed_fraction,
        remaining_hours=remaining_hours,
        remaining_days=remaining_days,
        p_now=p_now,
        five_hour_percent=five_hr,
        spent_usd=spent_usd,
        snapshot_count=len(samples),
        latest_snapshot_at=latest[0],
        p_24h_ago=p_24h_ago,
        t_24h_actual_hours=t_24h,
        dollars_per_percent=dpp,
        dollars_per_percent_source=dpp_source,
        confidence=confidence,
        low_confidence_reasons=reasons,
    )


# (moved to bin/_lib_forecast.py, #279 S4 F2 — honest-imported at module top)


def _parse_forecast_targets(raw: str) -> list[int]:
    """Parse --targets '100,90' → [100, 90]. Validate 0 < n <= 200."""
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError as exc:
            raise ValueError(f"invalid --targets token: {tok!r}") from exc
        if not (0 < n <= 200):
            raise ValueError(f"--targets value out of range: {n}")
        out.append(n)
    if not out:
        raise ValueError("--targets produced no valid values")
    return out


TOOL_VERSION = "forecast-v1"  # Bumped on material JSON-schema changes.


def _iso_z(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_forecast_json_payload(out: ForecastOutput) -> dict:
    """Dict shape for the forecast JSON endpoint and for the dashboard
    envelope's ``forecast.explain`` subtree (design spec §2.2).

    Pure function — no I/O, no clock reads."""
    i = out.inputs
    payload = {
        "week": {
            "start_at":         _iso_z(i.week_start_at),
            "end_at":           _iso_z(i.week_end_at),
            "elapsed_hours":    round(i.elapsed_hours, 3),
            "elapsed_fraction": round(i.elapsed_fraction, 4),
            "remaining_hours":  round(i.remaining_hours, 3),
            "remaining_days":   round(i.remaining_days, 3),
        },
        "current": {
            "weekly_percent":     round(i.p_now, 3),
            "five_hour_percent":  (None if i.five_hour_percent is None
                                   else round(i.five_hour_percent, 3)),
            "spent_usd":          round(i.spent_usd, 6),
            "snapshot_count":     i.snapshot_count,
            "latest_snapshot_at": _iso_z(i.latest_snapshot_at),
        },
        "rates": {
            "week_average_pct_per_hour":   round(out.r_avg, 6),
            "recent_24h_pct_per_hour":     (None if out.r_recent is None
                                            else round(out.r_recent, 6)),
            "dollars_per_percent":         round(i.dollars_per_percent, 6),
            "dollars_per_percent_source":  i.dollars_per_percent_source,
        },
        "forecast": {
            "final_percent_low":  round(out.final_percent_low, 3),
            "final_percent_high": round(out.final_percent_high, 3),
            "week_avg_projection_pct": round(out.week_avg_projection_pct, 3),
            "projected_cap":      out.projected_cap,
            "cap_at":             (None if out.cap_at is None else _iso_z(out.cap_at)),
            "already_capped":     out.already_capped,
            "confidence":         i.confidence,
            "low_confidence_reasons": list(i.low_confidence_reasons),
        },
        "budget": [
            {
                "target_percent": b.target_percent,
                "pct_headroom":   (None if b.pct_headroom is None
                                   else round(b.pct_headroom, 3)),
                "dollars_per_day":(None if b.dollars_per_day is None
                                   else round(b.dollars_per_day, 6)),
                "percent_per_day":(None if b.percent_per_day is None
                                   else round(b.percent_per_day, 3)),
            }
            for b in out.budgets
        ],
        "meta": {
            "generated_at": _iso_z(i.now_utc),
            "tool_version": TOOL_VERSION,
        },
    }
    return payload


def _emit_forecast_json(out: ForecastOutput) -> str:
    return json.dumps(_build_forecast_json_payload(out), indent=2)


def _render_forecast_status_line(out: ForecastOutput, color: bool) -> str:
    """Compact one-line status-line segment (spec §5)."""
    c = _cctally()
    def _c(s: str, code: str) -> str:
        return c._style_ansi(s, code, color)

    i = out.inputs
    if out.already_capped:
        return _c("\u26a0 CAPPED", "31")       # red
    if i.confidence == "low":
        return _c("tracking\u2026", "2")       # dim
    low = out.final_percent_low
    high = out.final_percent_high
    low_disp = round(low)
    high_disp = round(high)
    pct_range = f"{low_disp}\u2013{high_disp}%"
    if high_disp >= 100:
        # Conservative (to-90%) budget for actionability.
        budget = next((b for b in out.budgets if b.target_percent == 90), None)
        if budget is None or budget.dollars_per_day is None:
            budget_str = ""
        else:
            budget_str = f" ${budget.dollars_per_day:.2f}/d"
        return _c(f"\u26a0 proj {pct_range}{budget_str}", "31")  # red
    if high_disp >= 90:
        return _c(f"proj {pct_range}", "33")  # yellow
    return _c(f"proj {pct_range}", "36")      # cyan


def _forecast_color_enabled(mode: str, stream) -> bool:
    """Resolve --color {auto,always,never} + NO_COLOR. Returns bool."""
    if mode == "never":
        return False
    if "NO_COLOR" in os.environ:
        return False
    if mode == "always":
        return True
    # auto
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _render_forecast_progress_bar(
    used: float, low: float, high: float,
    width: int, unicode_ok: bool, color: bool,
) -> list[str]:
    """Return list of rendered lines: axis labels, ticks, bar, 100%-caption.

    `width` is the usable character width inside the box (not counting outer
    border + 2-col padding). scale = max(100, high) so >100% zone is visible.
    """
    c = _cctally()
    scale = max(100.0, high)

    def _pos(v: float) -> int:
        return max(0, min(width, int(round((v / scale) * width))))

    i_used = _pos(used)
    i_low = _pos(low)
    i_high = _pos(high)
    i_100 = _pos(100.0)
    # _pos() can return `width` exactly when v == scale (i.e., when high<=100
    # so scale==100 and _pos(100)==width). Clamp for anything that INDEXES
    # into a width-sized list (cap_row below). The `idx < i_X` comparisons
    # in the bar loop work fine with an unclamped value.
    i_100 = min(width - 1, i_100)

    if unicode_ok:
        glyph_used = "\u2588"   # █
        glyph_low = "\u2593"    # ▓
        glyph_gap = "\u2592"    # ▒
        glyph_over = "\u2588"   # █ (red)
    else:
        glyph_used = "#"
        glyph_low = "="
        glyph_gap = "-"
        glyph_over = "#"

    bar_chars: list[str] = []
    for idx in range(width):
        if idx < i_used:
            ch = c._style_ansi(glyph_used, "32", color)      # green
        elif idx < i_low:
            ch = c._style_ansi(glyph_low, "33", color)       # yellow
        elif idx < i_high and idx < i_100:
            ch = c._style_ansi(glyph_gap, "33", color)       # yellow
        elif idx < i_100:
            ch = " "
        elif idx < i_high:
            ch = c._style_ansi(glyph_over, "31", color)      # red: >100 zone
        else:
            ch = " "
        bar_chars.append(ch)
    bar_line = "".join(bar_chars)

    # Axis: 0  25  50  75  100  >100 (when scale>100)
    ticks = [0, 25, 50, 75, 100] + ([int(scale)] if scale > 100 else [])
    axis_line = ["\u2500"] * width if unicode_ok else ["-"] * width
    label_slots = [" "] * width
    for t in ticks:
        pos = _pos(t)
        pos = min(width - 1, pos)
        axis_line[pos] = ("\u253c" if unicode_ok else "+")
        lbl = f"{t}%" if t > 0 else "0%"
        # Left-justify the label starting at pos; if the label would run past
        # the right edge (e.g. the 100% tick at width-1 would clip "100%" to
        # "1"), left-shift the label so it fits within [0, width).
        start = pos
        if pos + len(lbl) > width:
            start = max(0, width - len(lbl))
        for j, c in enumerate(lbl):
            if start + j < width:
                label_slots[start + j] = c
    axis_str = "".join(axis_line)
    label_str = "".join(label_slots)

    # 100% caption row: a "│" at i_100.
    cap_row = [" "] * width
    if 0 <= i_100 < width:
        cap_row[i_100] = "\u2502" if unicode_ok else "|"
    cap_str = "".join(cap_row)

    return [label_str, axis_str, bar_line, cap_str]


def _render_forecast_terminal(out: "ForecastOutput", args, color: bool) -> str:
    """Full box-frame terminal render (spec §4)."""
    c = _cctally()
    i = out.inputs
    unicode_ok = c._supports_unicode_stdout()

    # Outer frame width: 60 cols inner by default; expand up to terminal width.
    try:
        term_w = os.get_terminal_size().columns
    except OSError:
        term_w = 80
    inner_w = max(56, min(78, term_w - 2))

    def _box_top() -> str:
        return ("\u256d" + "\u2500" * inner_w + "\u256e") if unicode_ok else ("+" + "-" * inner_w + "+")

    def _box_bot() -> str:
        return ("\u2570" + "\u2500" * inner_w + "\u256f") if unicode_ok else ("+" + "-" * inner_w + "+")

    def _box_mid() -> str:
        return ("\u251c" + "\u2500" * inner_w + "\u2524") if unicode_ok else ("+" + "-" * inner_w + "+")

    def _row(text: str) -> str:
        # Row width must match border width (inner_w + 2).
        # Layout: border + " " + text + pad + border → 3 + text + pad == inner_w + 2,
        # so pad = inner_w - 1 - len(text).
        border = "\u2502" if unicode_ok else "|"
        pad = inner_w - 1 - len(c._ANSI_ESC_RE.sub("", text))
        pad = max(0, pad)
        return border + " " + text + " " * pad + border

    # ── Panel 1: title
    # cmd_forecast attaches the resolved zone (ZoneInfo or None for "local")
    # via args._resolved_tz. None means "host local"; bare astimezone() honors it.
    # Times pass through format_display_dt so a zone-label suffix tells the user
    # whether "Mon 5PM" is local or UTC \u2014 without the suffix, --tz handling was
    # silent on the rendered terminal text.
    tz_render = getattr(args, "_resolved_tz", None)
    fmt_dt = lambda d: c.format_display_dt(  # noqa: E731
        d, tz_render, fmt="%a %-I%p", suffix=True
    )
    title = c._style_ansi("Subscription Forecast", "36", color)
    # %b %-d sites carry suffix for symmetry with fmt_dt \u2014 Option A from the
    # localize-datetime-display reviewer: "Replace each _localize().strftime()
    # with format_display_dt(...suffix=True)".
    subtitle = (f"Week {c.format_display_dt(i.week_start_at, tz_render, fmt='%b %-d', suffix=True)} "
                f"\u2192 {c.format_display_dt(i.week_end_at, tz_render, fmt='%b %-d', suffix=True)}  "
                f"(resets {fmt_dt(i.week_end_at)}, {i.remaining_days:.1f}d remaining)")

    # ── Panel 2: used / forecast / bar
    used_line = f"Used     {i.p_now:.1f}%   ${i.spent_usd:.2f}"
    if out.already_capped:
        forecast_line = c._style_ansi(
            f"\u26a0 CAPPED at {i.p_now:.1f}% \u2014 reset {fmt_dt(i.week_end_at)} "
            f"({i.remaining_days:.1f}d)", "31", color)
    elif i.confidence == "low":
        reasons = ", ".join(i.low_confidence_reasons)
        forecast_line = c._style_ansi(
            f"\u26a0 LOW CONF \u2014 insufficient data ({reasons})", "33", color)
    else:
        low, high = out.final_percent_low, out.final_percent_high
        low_rnd = round(low)
        high_rnd = round(high)
        high_disp = ">999%" if high > 999 else f"{high_rnd}%"
        glyph_color = "31" if high_rnd >= 100 else ("33" if high_rnd >= 90 else "32")
        warn = ""
        if high_rnd >= 100:
            warn = c._style_ansi(" \u26a0 may cap", "31", color)
        elif high_rnd >= 90:
            warn = c._style_ansi(" approaching 100%", "33", color)
        forecast_line = c._style_ansi(
            f"Forecast {low_rnd}%\u2013{high_disp}", glyph_color, color) + warn

    bar_lines = _render_forecast_progress_bar(
        used=i.p_now,
        low=out.final_percent_low,
        high=out.final_percent_high,
        width=inner_w - 2,
        unicode_ok=unicode_ok,
        color=color,
    )

    # ── Panel 3: budget
    budget_header = f"Daily budget \u2014 {i.remaining_days:.1f} days remaining"
    budget_rows = []
    for b in out.budgets:
        if b.dollars_per_day is None:
            budget_rows.append(f"  to {b.target_percent:>3}%    \u2014 past target")
        else:
            budget_rows.append(
                f"  to {b.target_percent:>3}%    ${b.dollars_per_day:>6.2f}/day"
                f"    {b.percent_per_day:>5.1f}%/day")

    # ── Footer
    footer_bits = []
    footer_bits.append(c._style_ansi(
        f"rate source: {i.dollars_per_percent_source.replace('_', ' ')}", "2", color))
    if out.cap_at is not None:
        # format_display_dt: zone-label suffix disambiguates --tz vs host-local
        # in the rendered footer (matches the reset-chip subtitle).
        cap_str = c.format_display_dt(
            out.cap_at, tz_render, fmt="%a %-I:%M%p", suffix=True,
        )
        footer_bits.append(c._style_ansi(f"\u26a0 projected cap: {cap_str}", "31", color))
    footer = "    ".join(footer_bits)

    lines: list[str] = []
    lines.append(_box_top())
    lines.append(_row(title))
    lines.append(_row(c._style_ansi(subtitle, "2", color)))
    lines.append(_box_mid())
    lines.append(_row(used_line))
    lines.append(_row(forecast_line))
    lines.append(_row(""))
    for bl in bar_lines:
        lines.append(_row(bl))
    if not out.already_capped and i.confidence != "low":
        lines.append(_box_mid())
        lines.append(_row(budget_header))
        lines.append(_row(""))
        for br in budget_rows:
            lines.append(_row(br))
    lines.append(_box_bot())
    lines.append("  " + footer)

    # --explain footer
    if getattr(args, "explain", False):
        r_rec = "\u2014" if out.r_recent is None else f"{out.r_recent:.3f}%/h"
        lines.append(c._style_ansi(
            f"  r_avg={out.r_avg:.3f}%/h \u00b7 r_recent={r_rec} \u00b7 "
            f"{i.snapshot_count} snapshots \u00b7 $/1% source={i.dollars_per_percent_source}",
            "2", color))

    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    c = _cctally()
    c._share_validate_args(args)
    if args.sync_current:
        sync_ns = argparse.Namespace(
            week_start=None,
            week_end=None,
            week_start_name=args.week_start_name,
            mode=args.mode,
            offline=args.offline,
            project=args.project,
            json=False,
            quiet=True,
        )
        c.cmd_sync_week(sync_ns)

    config = c.load_config()
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz
    week_start_name = get_week_start_name(config, args.week_start_name)

    conn = open_db()
    try:
        latest_usage = conn.execute(
            """
            SELECT *
            FROM weekly_usage_snapshots
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest_usage is not None:
            date_str = latest_usage["week_start_date"]
            canon_start, canon_end = c._get_canonical_boundary_for_date(conn, date_str)
            current_ref = make_week_ref(
                week_start_date=date_str,
                week_end_date=latest_usage["week_end_date"],
                week_start_at=canon_start,
                week_end_at=canon_end,
            )
        else:
            now_local = dt.datetime.now().astimezone(tz)
            current_start, current_end = compute_week_bounds(now_local, week_start_name)
            current_ref = make_week_ref(
                week_start_date=current_start.isoformat(),
                week_end_date=current_end.isoformat(),
            )

        # Bug D (v1.7.2 round-4): when an in-place credit event exists for
        # the current subscription week, `_apply_reset_events_to_weekrefs`
        # synthesizes a pre-credit ref alongside the post-credit one (both
        # share `WeekRef.key`). The live "current week" segment is the
        # POST-credit one (its `week_start_at` was shifted to the
        # effective reset moment). Route `current_ref` through the same
        # override so its `week_start_at` reflects the post-credit start;
        # this lets the per-row match below disambiguate the synthesized
        # pre-credit ref from the live post-credit ref via both
        # `key` AND `week_start_at`. Order contract from
        # `_apply_reset_events_to_weekrefs`: post-credit ref lands at
        # index 0, pre-credit at index 1. Non-credit weeks return the
        # single input ref unchanged, so this is a no-op on the common
        # path.
        _adjusted_current = c._apply_reset_events_to_weekrefs(conn, [current_ref])
        if _adjusted_current:
            current_ref = _adjusted_current[0]

        weeks = c.get_recent_weeks(conn, max(1, args.weeks))
        if not weeks:
            # Format-aware empty path mirrors cmd_forecast:18578-18629 — a
            # fresh install requesting `report --format html` should emit a
            # uniformly-shaped artifact, not a free-form "No data yet"
            # sentence the share consumer can't parse.
            if getattr(args, "format", None):
                display_tz_str = c._share_display_tz_label(tz)
                # Anchor the period_label on the current subscription
                # week so the artifact's subtitle is meaningful (the
                # week the report WOULD describe if data existed).
                # `_command_as_of()` honors CCTALLY_AS_OF — keeps the
                # period_label coherent with `generated_at` (which goes
                # through `_share_now_utc` from the same env hook) so
                # fixture goldens don't drift when the harness host's
                # wall-clock day rolls past CCTALLY_AS_OF.
                now_local = _command_as_of().astimezone(tz)
                local_tz = now_local.tzinfo
                ws_d, we_d = compute_week_bounds(now_local, week_start_name)
                ws_dt = dt.datetime.combine(
                    ws_d, dt.time.min, tzinfo=local_tz
                )
                we_dt = dt.datetime.combine(
                    we_d + dt.timedelta(days=1), dt.time.min, tzinfo=local_tz
                )
                snap = c._build_report_snapshot(
                    c.TrendView(),
                    period_start=ws_dt,
                    period_end=we_dt,
                    display_tz=display_tz_str,
                    version=c._share_resolve_version(),
                    theme=args.theme,
                    reveal_projects=args.reveal_projects,
                )
                c._share_render_and_emit(snap, args)
                return 0
            if args.json:
                print(json.dumps({"current": None, "trend": []}, indent=2))
            else:
                print("No data yet. Add record-usage to your status line script (see record-usage --help).")
            return 0

        # Build the unified trend view (spec §5.4). `build_trend_view`
        # owns the per-row construction, including:
        # - get_latest_usage_for_week with split-key as_of_utc pinning
        #   for credited weeks (Bug D / round-3 Bug B parity)
        # - _week_ref_has_reset_event → _compute_cost_for_weekref bypass
        #   for reset-affected weeks
        # - freshness sub-dict derivation
        # - 3-sample-rule average
        # Note: build_trend_view returns rows oldest-first (chronological);
        # cmd_report's JSON contract is newest-first to mirror
        # get_recent_weeks's order — we reverse below.
        view = c.build_trend_view(conn, now_utc=_command_as_of(), n=args.weeks,
                                 display_tz=tz)
        # Serialize TuiTrendRow → today's camelCase keys. Order:
        # newest-first (matches the prior cmd_report behavior).
        # Map week_start_date → original WeekRef ISO strings so the
        # JSON serialization preserves the snapshot-stored tz format
        # (`+00:00` for UTC-anchored weeks) — TuiTrendRow's datetime
        # form re-localizes via parse_iso_datetime, which would emit
        # `+03:00` on a UTC+3 host and break byte-stability.
        #
        # Index by ``(week_start_date_iso, week_start_at_utc_instant)``
        # so ``_row_to_dict`` resolves a row's original WeekRef ISO
        # strings in O(1) — credited weeks share ``week_start_date`` so
        # the UTC-instant disambiguates them. The lookup key matches the
        # row-side derivation in ``_row_to_dict`` (UTC instant from the
        # parsed datetime).
        week_iso_by_key: dict[tuple[str, dt.datetime],
                              tuple[str | None, str | None]] = {}
        for wr in weeks:
            if wr.week_start_at is None:
                continue
            try:
                wr_utc = parse_iso_datetime(
                    wr.week_start_at, "wr.week_start_at",
                ).astimezone(dt.timezone.utc)
            except ValueError:
                continue
            week_iso_by_key[(wr.week_start.isoformat(), wr_utc)] = (
                wr.week_start_at,
                wr.week_end_at,
            )

        def _row_to_dict(r):
            # Match this row's WeekRef by (week_start_date, UTC instant
            # of parsed week_start_at) — credited weeks share
            # week_start_date so we disambiguate via the UTC instant.
            wsd_str = (
                r.week_start_date.isoformat() if r.week_start_date else None
            )
            ws_at = ws_at_end = None
            if wsd_str is not None and r.week_start_at is not None:
                r_utc = r.week_start_at.astimezone(dt.timezone.utc)
                hit = week_iso_by_key.get((wsd_str, r_utc))
                if hit is not None:
                    ws_at, ws_at_end = hit

            d: dict[str, Any] = {
                "weekStartDate": wsd_str,
                "weekEndDate": (
                    r.week_end_date.isoformat() if r.week_end_date else None
                ),
                "weekStartAt": ws_at,
                "weekEndAt": ws_at_end,
                "weeklyPercent": r.used_pct,
                "weeklyCostUSD": (
                    round(r.weekly_cost_usd, 9)
                    if r.weekly_cost_usd is not None else None
                ),
                "dollarsPerPercent": (
                    round(r.dollars_per_percent, 9)
                    if r.dollars_per_percent is not None else None
                ),
                "usageCapturedAt": r.usage_captured_at,
                "costCapturedAt": r.cost_captured_at,
                "asOf": r.as_of,
                "rangeStartIso": r.range_start_iso,
                "rangeEndIso": r.range_end_iso,
            }
            if r.freshness:
                d["freshness"] = r.freshness
            return d

        # view.rows is oldest-first; reverse for cmd_report's newest-first
        # JSON contract. Also need WeekRef-based current_row matching —
        # use weekRef key + week_start_at to disambiguate credited weeks.
        # We re-walk the original `weeks` list to map (key, week_start_at)
        # → the corresponding dict row.
        trend: list[dict[str, Any]] = []
        current_row: dict[str, Any] | None = None
        # `view.rows` order = chrono asc (oldest first). Build trend in
        # the reverse order (newest first) to match the historical
        # cmd_report contract.
        # The view's TuiTrendRow doesn't carry WeekRef.key directly; we
        # use (week_start_date, week_start_at) for the match — week_start_at
        # in TuiTrendRow is a parsed datetime, and current_ref carries
        # ISO strings.
        for r in reversed(view.rows):
            row = _row_to_dict(r)
            trend.append(row)
            # Match against current_ref. Compare by week_start ISO date
            # AND week_start_at ISO string.
            week_start_at_iso = row["weekStartAt"]
            if (
                r.week_start_date is not None
                and r.week_start_date.isoformat() == current_ref.key
                and week_start_at_iso == current_ref.week_start_at
            ):
                current_row = row

        if current_row is None and trend:
            current_row = trend[0]

        output = {
            "current": current_row,
            "trend": trend,
            "weekStartRule": week_start_name,
            "generatedAt": now_utc_iso(),
            "currentWeek": {
                "weekStartDate": current_ref.week_start.isoformat(),
                "weekEndDate": current_ref.week_end.isoformat() if current_ref.week_end else None,
                "weekStartAt": current_ref.week_start_at,
                "weekEndAt": current_ref.week_end_at,
            },
        }

        if args.detail:
            milestone_rows = c.get_milestones_for_week(conn, current_ref.week_start.isoformat())
            output["milestones"] = [
                {
                    "percentThreshold": int(m["percent_threshold"]),
                    "cumulativeCostUSD": round(float(m["cumulative_cost_usd"]), 9),
                    "marginalCostUSD": round(float(m["marginal_cost_usd"]), 9) if m["marginal_cost_usd"] is not None else None,
                    "capturedAt": m["captured_at_utc"],
                }
                for m in milestone_rows
            ]

        # Shareable-reports gate: --format short-circuits the terminal/JSON
        # paths. The mutex in `_add_share_args` guarantees --format and
        # --json are not both set, so checking --format first is unambiguous.
        # Snapshot rows are reversed to ascending chronological order so
        # the line chart trends left->right with time (`get_recent_weeks`
        # returns newest-first; `trend` mirrors that order).
        if getattr(args, "format", None):
            # Note: --detail is a no-op under --format (snapshot focuses on
            # the headline weekly-trend table + chart; per-percent milestone
            # detail isn't in the share spec scope). Same convention applies
            # to other share-enabled subcommands (cmd_daily's --breakdown,
            # etc.).
            #
            # `view.rows` is already chronological (oldest-first), the
            # order the chart needs. period_start / period_end derived
            # from the view's oldest / newest rows.
            if view.rows:
                first_r = view.rows[0]
                last_r = view.rows[-1]
                first_wsd = (
                    first_r.week_start_date.isoformat()
                    if first_r.week_start_date else None
                )
                last_wed = (
                    last_r.week_end_date.isoformat()
                    if last_r.week_end_date else first_wsd
                )
                period_start = c._share_parse_date_to_dt(first_wsd, tz)
                period_end = c._share_parse_date_to_dt(last_wed, tz)
            else:
                period_start = period_end = c._share_now_utc()
            display_tz_str = c._share_display_tz_label(tz)
            snap = c._build_report_snapshot(
                view,
                period_start=period_start,
                period_end=period_end,
                display_tz=display_tz_str,
                version=c._share_resolve_version(),
                theme=args.theme,
                reveal_projects=args.reveal_projects,
            )
            c._share_render_and_emit(snap, args)
            return 0

        if args.json:
            print(json.dumps(output, indent=2))
            return 0

        if current_row is not None:
            week_window = c._format_week_window(
                current_row.get("weekStartDate"),
                current_row.get("weekEndDate"),
                current_row.get("weekStartAt"),
                current_row.get("weekEndAt"),
                tz=tz,
            )
            wp = current_row["weeklyPercent"]
            wc = current_row["weeklyCostUSD"]
            dpp = current_row["dollarsPerPercent"]
            print(
                c._boxed_table(
                    ["Week Window", "Usage %", "Cost USD", "$ / 1%"],
                    [[
                        week_window,
                        f"{wp:.2f}%" if wp is not None else "n/a",
                        f"${wc:.6f}" if wc is not None else "n/a",
                        f"${dpp:.6f}" if dpp is not None else "n/a",
                    ]],
                    ["left", "right", "right", "right"],
                )
            )
            print()

        print("Trend:")
        headers = [
            "#",
            "Week Window",
            "Usage %",
            "Cost USD",
            "$ / 1%",
            "As Of",
            "Usage Captured",
            "Cost Captured",
        ]
        display_trend = sorted(
            trend,
            key=c._trend_row_recency_seconds,
            reverse=True,
        )
        table_rows: list[list[str]] = []
        for idx, row in enumerate(display_trend, start=1):
            percent = "n/a" if row["weeklyPercent"] is None else f"{row['weeklyPercent']:.2f}%"
            cost = "n/a" if row["weeklyCostUSD"] is None else f"${row['weeklyCostUSD']:.6f}"
            dpp = "n/a" if row["dollarsPerPercent"] is None else f"${row['dollarsPerPercent']:.6f}"
            week_window = c._format_week_window(
                row.get("weekStartDate"),
                row.get("weekEndDate"),
                row.get("weekStartAt"),
                row.get("weekEndAt"),
                tz=tz,
            )
            table_rows.append(
                [
                    str(idx),
                    week_window,
                    percent,
                    cost,
                    dpp,
                    c._format_ts_compact(row.get("asOf"), tz=tz),
                    c._format_ts_compact(row.get("usageCapturedAt"), tz=tz),
                    c._format_ts_compact(row.get("costCapturedAt"), tz=tz),
                ]
            )

        print(
            c._boxed_table(
                headers,
                table_rows,
                aligns=[
                    "right",
                    "left",
                    "right",
                    "right",
                    "right",
                    "left",
                    "left",
                    "left",
                ],
                color_header=True,
            )
        )

        if args.detail:
            milestone_rows = c.get_milestones_for_week(conn, current_ref.week_start.isoformat())
            if milestone_rows:
                print()
                print("Percent breakdown (current week):\n")
                m_headers = ["#", "Threshold", "Cumulative Cost", "Marginal Cost"]
                m_rows: list[list[str]] = []
                for idx, m in enumerate(milestone_rows, start=1):
                    pct = f"{int(m['percent_threshold'])}%"
                    cum = f"${float(m['cumulative_cost_usd']):.6f}"
                    marg = f"${float(m['marginal_cost_usd']):.6f}" if m["marginal_cost_usd"] is not None else "n/a"
                    m_rows.append([str(idx), pct, cum, marg])
                print(c._boxed_table(m_headers, m_rows, ["right", "right", "right", "right"]))

        return 0
    finally:
        conn.close()


def cmd_forecast(args: argparse.Namespace) -> int:
    """Project current-week usage to reset boundary. Emit terminal report,
    JSON, or status-line one-liner. See docs/commands/forecast.md.
    """
    c = _cctally()
    c._share_validate_args(args)
    if args.json and args.status_line:
        print("forecast: --json and --status-line are mutually exclusive",
              file=sys.stderr)
        return 1

    # Resolve display tz via the unified --tz / config.display.tz pipeline.
    # The renderer reads it back from args._resolved_tz.
    config = c.load_config()
    args._resolved_tz = c.resolve_display_tz(args, config)

    try:
        targets = _parse_forecast_targets(args.targets)
    except ValueError as exc:
        print(f"forecast: {exc}", file=sys.stderr)
        return 1

    now_utc = _resolve_forecast_now(args.as_of)
    conn = open_db()
    # Cache sync is gated inside get_entries(..., skip_sync=args.no_sync); no
    # sync_cache(conn) here — that prior call ran on the stats DB connection
    # (wrong conn) and was a no-op for the real cache anyway.

    # Route through ``build_forecast_view`` (issue #57). The View is the
    # kernel-pattern wrapper; ``view.output`` carries the existing
    # ``ForecastOutput`` math result so every downstream renderer here
    # (text / JSON / status-line / share) reuses the same projection,
    # verdict, budgets, and per-method rate fields without recomputing.
    view = c.build_forecast_view(
        conn, now_utc=now_utc, targets=tuple(targets),
        skip_sync=args.no_sync, display_tz=args._resolved_tz,
    )
    inputs = view.output.inputs if view.output is not None else None
    if inputs is None:
        # No snapshot for the current week.
        if getattr(args, "format", None):
            # Shareable-reports empty-data path: emit a "no data" snapshot
            # rather than a free-form text message so consumers of the share
            # output (md / html / svg) get a uniformly-shaped artifact.
            #
            # Compute the real subscription-week boundaries from config
            # rather than collapsing to a 0-duration `now → now` window —
            # the period_label in the artifact's subtitle is meaningful
            # (the week the forecast WOULD describe if data existed).
            # Lift the `dt.date` boundaries from `compute_week_bounds`
            # to tz-aware datetimes anchored on local midnight so the
            # PeriodSpec stays consistent with sibling builders.
            tz = getattr(args, "_resolved_tz", None)
            display_tz_str = c._share_display_tz_label(tz)
            week_start_name = get_week_start_name(
                config, getattr(args, "week_start_name", None)
            )
            ws_date, we_date = compute_week_bounds(now_utc, week_start_name)
            # internal fallback: host-local intentional
            local_tz = dt.datetime.now().astimezone().tzinfo
            week_start_dt = dt.datetime.combine(
                ws_date, dt.time.min, tzinfo=local_tz
            )
            week_end_dt = dt.datetime.combine(
                we_date + dt.timedelta(days=1), dt.time.min, tzinfo=local_tz
            )
            # Pass `low_conf=False` + explicit notes: the issue is "no data
            # recorded yet," not "thin data." LOW CONF would mislead the
            # reader into thinking a projection ran with sparse samples.
            snap = c._build_forecast_snapshot(
                week_start=week_start_dt,
                week_end=week_end_dt,
                display_tz=display_tz_str,
                version=c._share_resolve_version(),
                theme=args.theme,
                reveal_projects=args.reveal_projects,
                actual_series=[],
                projected_series=[],
                current_pct=0.0,
                projected_low_pct=0.0,
                projected_high_pct=0.0,
                days_remaining=0.0,
                dollars_per_percent=0.0,
                dollars_per_percent_source="this_week",
                low_conf=False,
                notes=(
                    "No snapshots recorded for this week yet — run "
                    "cctally record-usage to populate.",
                ),
            )
            c._share_render_and_emit(snap, args)
            return 0
        if args.json:
            print(json.dumps({
                "error": "no_current_week_data",
                "meta": {"generated_at": _iso_z(now_utc), "tool_version": TOOL_VERSION},
            }, indent=2))
        elif args.status_line:
            pass  # silent segment
        else:
            print("forecast: no data for current week yet")
        return 0

    output = view.output

    # Shareable-reports gate: --format short-circuits the JSON / status-line /
    # terminal dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args(has_status_line=True)` keeps `--format`, `--json`, and
    # `--status-line` from coexisting. The gate fires AFTER ``build_forecast_view``
    # so the snapshot reuses the same projection math as the terminal/JSON
    # paths — no parallel computation.
    if getattr(args, "format", None):
        i = output.inputs
        # Re-fetch the samples for the LineChart's actual_series. The
        # `_load_forecast_inputs` path dropped them after deriving p_now /
        # p_24h_ago / snapshot_count; re-running `_fetch_current_week_snapshots`
        # is a single indexed query against `weekly_usage_snapshots` and only
        # fires when `--format` is requested, so the cost is bounded.
        # `_apply_midweek_reset_override` is replayed so the chart axis
        # matches the (possibly-shifted) week_start_at carried by `inputs`.
        fetched = _fetch_current_week_snapshots(conn, now_utc)
        actual_series: list[tuple[str, float, float]] = []
        if fetched is not None:
            _ws_at, _we_at, raw_samples = fetched
            _ws_at_shifted, samples = _apply_midweek_reset_override(
                conn, _ws_at, _we_at, raw_samples
            )
            tz_render = getattr(args, "_resolved_tz", None)
            for cap_at, pct, _five_hr in samples:
                elapsed_h = (
                    (cap_at - i.week_start_at).total_seconds() / 3600.0
                )
                lbl = c.format_display_dt(
                    cap_at, tz_render, fmt="%a %H:%M", suffix=False,
                )
                actual_series.append((lbl, elapsed_h, float(pct)))
        # Projected series: a 2-point ray from (now, p_now) to
        # (week_end, projected_high). The high-end matches the terminal
        # render's "may cap" warning so the chart and table tell the same
        # story. When `already_capped` is true the ray collapses to a flat
        # horizontal line at p_now from (now → week_end) — visually
        # signals "you are pinned at the cap; no further growth expected"
        # instead of the visually-empty (no-projection) chart that was
        # confusable with "no projection computed."
        projected_series: list[tuple[str, float, float]] = []
        if i.remaining_hours > 0:
            tz_render = getattr(args, "_resolved_tz", None)
            now_label = c.format_display_dt(
                i.now_utc, tz_render, fmt="%a %H:%M", suffix=False,
            )
            end_label = c.format_display_dt(
                i.week_end_at, tz_render, fmt="%a %H:%M", suffix=False,
            )
            now_x = (i.now_utc - i.week_start_at).total_seconds() / 3600.0
            end_x = (i.week_end_at - i.week_start_at).total_seconds() / 3600.0
            if output.already_capped:
                # Flat ray: y stays at p_now across the remaining window.
                projected_series.append(
                    (now_label, now_x, float(i.p_now))
                )
                projected_series.append(
                    (end_label, end_x, float(i.p_now))
                )
            else:
                projected_series.append(
                    (now_label, now_x, float(i.p_now))
                )
                projected_series.append(
                    (end_label, end_x, float(output.final_percent_high))
                )
        display_tz_str = c._share_display_tz_label(
            getattr(args, "_resolved_tz", None)
        )
        snap = c._build_forecast_snapshot(
            week_start=i.week_start_at,
            week_end=i.week_end_at,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
            theme=args.theme,
            reveal_projects=args.reveal_projects,
            actual_series=actual_series,
            projected_series=projected_series,
            current_pct=float(i.p_now),
            projected_low_pct=float(output.final_percent_low),
            projected_high_pct=float(output.final_percent_high),
            days_remaining=float(i.remaining_days),
            dollars_per_percent=float(i.dollars_per_percent),
            dollars_per_percent_source=i.dollars_per_percent_source,
            low_conf=(i.confidence == "low"),
        )
        c._share_render_and_emit(snap, args)
        return 0

    if args.json:
        print(_emit_forecast_json(output))
        return 0
    if args.status_line:
        # --status-line is invoked via $(cmd 2>/dev/null) by design — stdout is
        # a pipe and stderr is /dev/null, so auto-TTY detection always sees
        # non-interactive. Promote auto -> always here; NO_COLOR and explicit
        # `--color never` still disable (both handled inside _forecast_color_enabled).
        effective_mode = "always" if args.color == "auto" else args.color
        color = _forecast_color_enabled(effective_mode, sys.stdout)
        print(_render_forecast_status_line(output, color))
        return 0

    color = _forecast_color_enabled(args.color, sys.stdout)
    print(_render_forecast_terminal(output, args, color))
    return 0


# ── budget ──────────────────────────────────────────────────────────────
# `cctally budget` — weekly equivalent-$ budget + pace + spend alerts.
# cctally-original (NOT a ccusage drop-in) → flat surface only, no
# claude/codex subgroup. Status reads live spend; `set`/`unset` write the
# DEFAULT config (F4 — the path the alert firing in Task 3 reads). See
# docs/commands/budget.md + spec §4/§6.

_BUDGET_JSON_SCHEMA_VERSION = 1

# Calendar-period + Codex budgets (spec §2/§3): single-source the short→canonical
# `--period` spellings so the parser choices and the handler normalizer never
# drift. The SHORT aliases live here, keyed canonical → its short spelling;
# `_BUDGET_PERIOD_ALIASES` (the normalizer's lookup) and `_BUDGET_PERIOD_CHOICES`
# (the parser's `choices=`) are BOTH derived from this map + the canonical
# `BUDGET_PERIODS` tuple in _cctally_core, so adding a period (or renaming a
# spelling) touches exactly one place (code-review #5).
_BUDGET_PERIOD_SHORT = {
    "subscription-week": "sub-week",
    "calendar-week": "week",
    "calendar-month": "month",
}

# short → canonical (the handler normalizer's lookup), plus identity entries so
# canonical spellings pass through unchanged.
_BUDGET_PERIOD_ALIASES = {
    **{short: canonical for canonical, short in _BUDGET_PERIOD_SHORT.items()},
    **{canonical: canonical for canonical in _CCTALLY_BUDGET_PERIODS},
}

# The parser's `choices=` — canonical-first within each period group, matching
# the prior hardcoded order so `--help` stays byte-stable.
_BUDGET_PERIOD_CHOICES = [
    spelling
    for canonical in _CCTALLY_BUDGET_PERIODS
    for spelling in (canonical, _BUDGET_PERIOD_SHORT[canonical])
]


def _normalize_budget_period(raw):
    """Map a `--period` flag value (short or canonical) to the canonical enum.

    ``None`` (flag omitted) passes through so the set/unset semantics can
    distinguish "preserve stored / per-vendor default" from an explicit choice.
    An unknown value passes through unchanged so the per-vendor config validator
    raises the canonical _BudgetConfigError (single error surface).
    """
    if raw is None:
        return None
    return _BUDGET_PERIOD_ALIASES.get(raw, raw)


def _resolve_local_calendar_window(period, now_utc, week_start_idx=None):
    """DST-correct ``(start_utc, end_utc)`` for the ``display.tz=local`` case
    (issue #136).

    The explicit-tz path feeds a real DST-aware ``ZoneInfo`` to the pure
    kernels, which is correct. But ``display.tz=local`` has no clean stdlib
    IANA-name handle — ``datetime.now().astimezone().tzinfo`` is only a
    *fixed-offset* ``datetime.timezone`` snapped at one instant. Feeding that to
    the kernel converts the period-start local midnight to UTC at the wrong
    offset whenever the period straddles a DST transition, so the same civil
    period resolves to two different ``period_start_at`` instants before vs
    after the boundary — shifting the ``[start, now]`` spend window AND drifting
    the ``UNIQUE(period_start_at, threshold)`` milestone key into a re-fire.

    Instead, mirror ``_period_label_local``: build the NAIVE local civil
    boundaries, then convert each via a bare ``astimezone()`` so each boundary
    picks up the offset in effect at ITS OWN wall-clock instant — stable across
    an in-period transition and with NO dependency on the real wall clock.
    Period boundaries sit at 00:00 local, which is never inside a US/EU
    spring-forward gap (those land at 01:00–03:00), so the naive→aware
    conversion is unambiguous. Impure (it reads the process zone, like
    ``_period_label_local``); kept in the forecast layer so ``_lib_budget``
    stays a pure, dependency-injected kernel.
    """
    # internal fallback: host-local intentional (per-instant DST-correct)
    now_local = now_utc.astimezone()
    if period == "calendar-month":
        start_naive = now_local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        if start_naive.month == 12:
            end_naive = start_naive.replace(year=start_naive.year + 1, month=1)
        else:
            end_naive = start_naive.replace(month=start_naive.month + 1)
    else:  # calendar-week
        midnight_naive = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        diff = (midnight_naive.weekday() - week_start_idx) % 7
        start_naive = midnight_naive - dt.timedelta(days=diff)
        end_naive = start_naive + dt.timedelta(days=7)
    return (
        start_naive.astimezone(dt.timezone.utc),
        end_naive.astimezone(dt.timezone.utc),
    )


def _resolve_calendar_window(period, now_utc, config, tz):
    """Resolve a calendar period's ``(start_utc, end_utc)`` (spec §3). ``period``
    is canonical (calendar-week / calendar-month). Reuses the existing
    ``collector.week_start`` config for the week-start index — no new config key.

    Two paths (issue #136). An explicit ``display.tz`` (``utc`` / IANA) resolves
    to a real DST-aware ``ZoneInfo``, so it goes straight to the pure kernels —
    already DST-correct (proven by ``test_budget_periods.py``). ``display.tz=
    local`` (``tz is None``) has no DST-aware stdlib handle, so it takes
    ``_resolve_local_calendar_window``'s per-instant path instead of collapsing
    the zone to a single fixed offset."""
    c = _cctally()
    if period == "calendar-month":
        if tz is None:
            return _resolve_local_calendar_window("calendar-month", now_utc)
        return c.calendar_month_window(now_utc, tz)
    # calendar-week
    week_start_idx = WEEKDAY_MAP[get_week_start_name(config)]
    if tz is None:
        return _resolve_local_calendar_window(
            "calendar-week", now_utc, week_start_idx
        )
    return c.calendar_week_window(now_utc, tz, week_start_idx)


def _period_label_local(instant, tz):
    """Render ``instant`` (a UTC-aware datetime) into the DISPLAY-TZ civil
    wall-clock, PER INSTANT (code-review #4).

    For an explicit ``tz`` (utc / IANA) the offset is uniform, so this is just
    ``instant.astimezone(tz)``. For the ``display.tz=local`` case (``tz is
    None``) it does a BARE ``instant.astimezone()`` so EACH instant picks up its
    OWN host-local offset — matching the prior ``format_display_dt(..., tz=None)``
    behavior. Window RESOLUTION (``_resolve_local_calendar_window``) applies the
    same per-instant conversion for ``display.tz=local`` (issue #136); a single
    fixed offset captured at ``now()`` would shift a boundary that straddles a
    DST transition by an hour (and so a day, at midnight)."""
    if tz is not None:
        return instant.astimezone(tz)
    # internal fallback: host-local intentional
    return instant.astimezone()


def _civil_period_label(period, start_utc, end_utc, tz):
    """Build the terminal header period label from the DISPLAY-TZ civil
    boundary (NOT the UTC instant — spec §3). Returns e.g.
    ``subscription week 2026-05-26 → 2026-06-02`` /
    ``calendar month 2026-06`` / ``calendar week 2026-06-01 → 06-08``.

    The start/end are converted PER INSTANT (code-review #4) so a window
    straddling a DST transition renders each boundary at its own local offset."""
    s_local = _period_label_local(start_utc, tz)
    e_local = _period_label_local(end_utc, tz)
    if period == "calendar-month":
        return f"calendar month {s_local.strftime('%Y-%m')}"
    if period == "calendar-week":
        return (
            f"calendar week {s_local.strftime('%Y-%m-%d')} → "
            f"{e_local.strftime('%m-%d')}"
        )
    # subscription-week
    return (
        f"subscription week {s_local.strftime('%Y-%m-%d')} → "
        f"{e_local.strftime('%Y-%m-%d')}"
    )


def _build_vendor_budget_inputs(
    *, vendor, period, target_usd, alert_thresholds, now_utc, config, tz,
    skip_sync=False,
):
    """Resolve the window + live spend for one (vendor, period) budget and
    return a :class:`BudgetInputs` (or ``None`` only for the Claude /
    subscription-week case where no usage snapshot has landed yet).

    Decoupled from ``weekly_usage_snapshots`` for every calendar period (spec
    §4 review #5): the calendar/Codex path resolves the window from the pure
    ``calendar_*_window`` functions and renders ``$0`` / ``0%`` when entries are
    empty — it NEVER short-circuits to the no-data note. Only the legacy
    Claude + subscription-week path can return ``None`` (no resolvable week
    window yet), preserving the existing byte-identical no-data behavior.

    The stats DB is opened LAZILY and ONLY on the Claude + subscription-week
    branch (the sole reader of ``weekly_usage_snapshots`` via
    ``_resolve_current_budget_window``). Codex spend and Claude calendar-period
    spend come from the cache DB / pure window functions, so a Codex-only or
    calendar-period budget never opens a stats connection (code-review #2).

    Spend: Claude → ``_sum_cost_for_range``; Codex → ``_sum_codex_cost_for_range``
    (cache DB; spec §4). ``recent_24h_usd`` is the same helper over the CLAMPED
    trailing-24h window ``[max(start, now-24h), now]`` so a heavy spend just
    before the window start can't leak into a fresh window's verdict. The
    returned dataclass keeps the ``week_*`` field names (a deliberate
    back-compat misnomer — they back documented ``--json`` fields, spec §9).
    """
    c = _cctally()
    if vendor == "claude" and period == "subscription-week":
        conn = open_db()
        try:
            window = _resolve_current_budget_window(conn, now_utc)
        finally:
            conn.close()
        if window is None:
            return None
        start_at, end_at = window
    else:
        start_at, end_at = _resolve_calendar_window(period, now_utc, config, tz)

    recent_start = max(start_at, now_utc - dt.timedelta(hours=24))
    # The full-window sum (first call) honors the caller's ``skip_sync`` (render
    # callers default False; the Claude record/projected leg passes True because
    # other record-usage work already warmed the cache; the Codex projected leg
    # passes False — R5 — since Codex has no other record-path warmer). Either
    # way the recent-24h window is a SUBSET of the full window already fetched,
    # so ``skip_sync=True`` on the second call avoids a redundant JSONL walk.
    if vendor == "codex":
        spent = c._sum_codex_cost_for_range(start_at, now_utc, skip_sync=skip_sync)
        recent_24h = c._sum_codex_cost_for_range(
            recent_start, now_utc, skip_sync=True
        )
    else:
        spent = c._sum_cost_for_range(
            start_at, now_utc, mode="auto", skip_sync=skip_sync
        )
        recent_24h = c._sum_cost_for_range(
            recent_start, now_utc, mode="auto", skip_sync=True
        )
    return c.BudgetInputs(
        target_usd=float(target_usd),
        spent_usd=float(spent),
        recent_24h_usd=float(recent_24h),
        week_start_at=start_at,
        week_end_at=end_at,
        now=now_utc,
        alert_thresholds=tuple(alert_thresholds),
    )


def cmd_budget(args: argparse.Namespace) -> int:
    """Dispatch `cctally budget [set AMOUNT | unset]`. See docs/commands/budget.md."""
    c = _cctally()
    action = getattr(args, "action", None)

    # F4: mutations always target the DEFAULT config; --config is read-only.
    # --format is a status-only render surface — reject it on set/unset.
    if action in {"set", "unset"} and getattr(args, "config", None):
        eprint(
            "cctally budget: --config is read-only; "
            "set/unset always write the default config"
        )
        return 2
    if action in {"set", "unset"} and getattr(args, "format", None):
        eprint("cctally budget: --format is not valid with set/unset")
        return 2

    # Per-vendor calendar-period budgets (spec §2): `--vendor`/`--period`
    # normalize + validate in the handler so the error is a clean exit 2 with a
    # message (not an argparse usage error). `--project` is Claude/subscription-
    # week-only (spec Q5), so reject combining it with --vendor codex / --period.
    vendor = getattr(args, "vendor", "claude") or "claude"
    raw_period = getattr(args, "period", None)
    period = _normalize_budget_period(raw_period)
    if action in {"set", "unset"}:
        if getattr(args, "project", None) is not None and (
            vendor != "claude" or period is not None
        ):
            eprint(
                "cctally budget: --project budgets are Claude / subscription-week "
                "only; drop --vendor/--period"
            )
            return 2
        if vendor == "codex" and period in {"subscription-week"}:
            eprint(
                "cctally budget: Codex has no subscription week; use "
                "--period calendar-week or --period calendar-month"
            )
            return 2

    if action == "set":
        if getattr(args, "project", None) is not None:
            return _cmd_budget_set_project(args)
        if vendor == "codex":
            return _cmd_budget_set_codex(args, period)
        return _cmd_budget_set(args, period)
    if action == "unset":
        if getattr(args, "project", None) is not None:
            return _cmd_budget_unset_project(args)
        if vendor == "codex":
            return _cmd_budget_unset_codex(args)
        return _cmd_budget_unset(args)

    # ── bare status ──
    # Early reject of bad share-flag combos BEFORE any DB/sync work
    # (mirrors cmd_forecast; calls sys.exit(2) directly inside).
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)  # honors --config read-only
    args._resolved_tz = c.resolve_display_tz(args, config)
    tz = args._resolved_tz
    try:
        budget_cfg = _get_budget_config(config)
    except _BudgetConfigError as exc:
        eprint(f"cctally budget: {exc}")
        return 2

    # Per-project section is appended to WHICHEVER global path runs (unset,
    # no-data, full status) — gated on budget.projects being non-empty. When
    # empty, NOTHING is appended → the existing global render paths stay
    # byte-identical (spec §7.3a). It needs the budget window, so the work
    # happens after we have now_utc below (project rows open their own conn).
    has_projects = bool(budget_cfg["projects"])

    target = budget_cfg["weekly_usd"]
    claude_period = budget_cfg["period"]
    codex_cfg = budget_cfg["codex"]
    has_codex = codex_cfg is not None
    # Vendor labels / equivalent-$ vs actual-$ cues appear in the TERMINAL block
    # ONLY once a Codex budget exists OR the Claude period is non-default — so a
    # legacy Claude/subscription-week + no-Codex render stays byte-identical
    # (spec §5/§10.1). `coexists` gates the terminal header relabel; the share
    # artifact keys its period label off `claude_period` directly (code-review
    # #3), so it doesn't need `coexists`.
    coexists = has_codex or claude_period != "subscription-week"

    # Resolve the window-dependent per-project rows once (only when configured).
    # `project_rows` is None when no window resolves (no snapshot yet) — the
    # render paths degrade to the no-data note for the section. When projects
    # are unconfigured, we never open a connection just for them: the
    # individual global paths open their own.
    now_utc = _command_as_of()  # honors the CCTALLY_AS_OF testing hook
    project_rows = None
    project_window_resolved = False
    if has_projects:
        pj_conn = open_db()
        try:
            project_rows = _build_project_budget_rows(pj_conn, budget_cfg, now_utc)
        finally:
            pj_conn.close()
        project_window_resolved = project_rows is not None

    # Build the Codex sibling inputs/status once (when configured) so every
    # render path (terminal / --json / share) can reuse them. Decoupled from
    # weekly snapshots — always resolves a calendar window (spec §4 review #5).
    codex_inputs = None
    codex_status = None
    if has_codex:
        # Codex spend reads the cache DB — no stats connection needed here;
        # _build_vendor_budget_inputs opens one lazily only for the
        # Claude+subscription-week branch (code-review #2).
        codex_inputs = _build_vendor_budget_inputs(
            vendor="codex", period=codex_cfg["period"],
            target_usd=codex_cfg["amount_usd"],
            alert_thresholds=codex_cfg["alert_thresholds"],
            now_utc=now_utc, config=config, tz=tz,
        )
        codex_status = c.compute_budget_status(codex_inputs)
        # Opportunistic Codex-budget alert firing (spec §6 trigger 2) — the
        # INTERACTIVE backstop for spend that crosses a threshold BETWEEN
        # record-usage ticks. Gated to the plain terminal status render:
        #   * never under `--config PATH` (documented read-only — firing would
        #     write milestones into the DEFAULT stats.db + dispatch off an
        #     alternate config), and
        #   * never under the machine-readable `--json` / artifact `--format`
        #     surfaces (a scripted/automated read must not pop a desktop
        #     notification).
        # Routes through the SAME record-path helper as the automated trigger so
        # the dedup key is resolved in CONFIG tz (like record-usage), NOT the
        # display `--tz`: a `cctally budget --tz X` near a period boundary must
        # not fork `period_start_at` and double-fire. The helper pre-probes,
        # forward-only/fire-once via UNIQUE(period_start_at, threshold), and is
        # best-effort (it never raises into the status render).
        interactive_status = not (
            getattr(args, "config", None)
            or getattr(args, "json", False)
            or getattr(args, "format", None)
        )
        if interactive_status and codex_cfg.get("alerts_enabled"):
            c.maybe_record_codex_budget_milestone({})
            # #135: the opportunistic Codex PROJECTED-pace backstop, scoped to
            # the codex_budget_usd metric so it never pops a weekly_pct / Claude
            # budget_usd notification from a bare `cctally budget`. The projected
            # leg self-syncs (skip_sync=False); since codex_inputs was just
            # built above, that delta-sync is a no-op here — correct and cheap.
            if codex_cfg.get("projected_enabled"):
                c.maybe_record_projected_alert(
                    {}, only_metrics={"codex_budget_usd"}
                )

    if target is None:
        # Global Claude budget unset → friendly message, then (if configured)
        # the per-project section + the Codex sibling. --json carries
        # status:"unset" + projects[] + the gated codex block.
        return _budget_render_unset(
            args,
            claude_period=claude_period,
            project_rows=project_rows,
            has_projects=has_projects,
            project_window_resolved=project_window_resolved,
            codex_cfg=codex_cfg, codex_inputs=codex_inputs,
            codex_status=codex_status, tz=tz,
        )

    inputs = _build_vendor_budget_inputs(
        vendor="claude", period=claude_period,
        target_usd=target, alert_thresholds=budget_cfg["alert_thresholds"],
        now_utc=now_utc, config=config, tz=tz,
    )
    if inputs is None:
        # Claude/subscription-week with no usage snapshot yet → no resolvable
        # week window (spec §6 worst case). Calendar periods never reach here.
        if getattr(args, "format", None):
            snap = _build_budget_no_data_snapshot(args, budget_cfg, now_utc)
            snap = _append_project_share_rows(snap, project_rows, has_projects)
            snap = _append_codex_share_rows(
                snap, codex_cfg, codex_inputs, codex_status, tz
            )
            c._share_render_and_emit(snap, args)
            return 0
        if getattr(args, "json", False):
            payload = {
                "schemaVersion": _BUDGET_JSON_SCHEMA_VERSION,
                "status": "no_data",
                "weekly_usd": target,
                "period": claude_period,
            }
            if has_codex:
                _append_codex_json(payload, codex_cfg, codex_inputs, codex_status)
            if has_projects:
                _append_project_json(payload, project_rows)
            print(json.dumps(payload))
            return 0
        print(f"Weekly budget: ${target:,.2f} — no usage data yet this week.")
        _print_codex_section(codex_cfg, codex_inputs, codex_status, tz, args)
        _print_project_section_or_note(
            project_rows, has_projects, project_window_resolved, args
        )
        return 0

    status = c.compute_budget_status(inputs)
    if getattr(args, "format", None):
        snap = _build_budget_snapshot(
            args, budget_cfg, inputs, status,
            period=claude_period, tz=tz,
        )
        snap = _append_project_share_rows(snap, project_rows, has_projects)
        snap = _append_codex_share_rows(
            snap, codex_cfg, codex_inputs, codex_status, tz
        )
        c._share_render_and_emit(snap, args)
        return 0
    if getattr(args, "json", False):
        return _budget_emit_json(
            budget_cfg, inputs, status,
            project_rows=project_rows, has_projects=has_projects,
            period=claude_period,
            codex_cfg=codex_cfg, codex_inputs=codex_inputs,
            codex_status=codex_status,
        )
    rc = _budget_render_terminal(
        args, budget_cfg, inputs, status,
        period=claude_period, coexists=coexists, tz=tz,
    )
    _print_codex_section(codex_cfg, codex_inputs, codex_status, tz, args)
    _print_project_section_or_note(
        project_rows, has_projects, project_window_resolved, args
    )
    return rc


def _resolve_project_budget_target(raw: str):
    """Resolve the ``--project`` value to a canonical git-root path.

    BOTH branches route through ``_resolve_project_key(..., "git-root", {})``
    so the stored key is the SAME canonical bucket ``_sum_cost_by_project``
    buckets entries under — otherwise a sub-directory path (e.g.
    ``~/code/monorepo/packages/foo`` under a monorepo git-root) would store
    a key that never matches any entry, permanently rendering ``$0``.

    ``__CWD__`` (the bare-flag sentinel) → resolve ``os.getcwd()`` to its
    ``.git`` root; a result that is ``is_no_git``/``is_unknown`` (not inside
    a git repo) → return ``None`` (the caller emits the "not inside a git
    repository" error + exit 2).

    An explicit path → resolve the same way: take ``.git_root`` when a ``.git``
    is found (so a sub-dir path collapses onto its monorepo root), else the
    normalized ``bucket_path`` (a path that is itself a git-root resolves to
    itself; a genuinely non-git path keeps its normalized form). Explicit
    paths never return ``None`` — they always resolve to a usable key.
    """
    c = _cctally()
    if raw == "__CWD__":
        key = c._resolve_project_key(os.getcwd(), "git-root", {})
        if key.is_no_git or key.is_unknown or not key.git_root:
            return None
        return key.git_root
    key = c._resolve_project_key(raw, "git-root", {})
    return key.git_root or key.bucket_path


def _looks_numeric(s) -> bool:
    """True iff `s` parses as a positive finite number — used to detect the
    `budget set --project 25` footgun (#130)."""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def _cmd_budget_set_project(args: argparse.Namespace) -> int:
    """`cctally budget set AMOUNT --project[=PATH]` — write one entry into
    `budget.projects`, keyed by the resolved canonical git-root. Writes the
    DEFAULT config (F4); Task 3's forward-only reconcile runs after the write."""
    c = _cctally()
    raw_amount = getattr(args, "amount", None)
    if raw_amount is None:
        proj = getattr(args, "project", None)
        if proj and proj != "__CWD__" and _looks_numeric(proj) and not os.path.isdir(proj):
            # `budget set --project 25` → argparse bound 25 to --project,
            # leaving amount=None (#130). A bare numeric value is almost always
            # the amount in the wrong slot — but NOT when it names a real
            # directory (e.g. a repo literally called `./2025`), which the
            # `not os.path.isdir(proj)` guard excludes so a numeric-named path
            # falls through to the generic "requires an amount" message below
            # instead of being misread as a misplaced amount. Point at the
            # right ordering.
            eprint(
                f"cctally budget: '{proj}' looks like an amount, not a "
                f"project path. Did you mean: cctally budget set {proj} "
                f"--project"
            )
        else:
            eprint(
                "cctally budget: `set --project` requires an amount, e.g. "
                "cctally budget set 25 --project"
            )
        return 2
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        eprint(f"cctally budget: amount must be a positive number, got {raw_amount!r}")
        return 2
    if not math.isfinite(amount) or amount <= 0:
        eprint(
            f"cctally budget: amount must be a positive finite number, "
            f"got {raw_amount!r}"
        )
        return 2

    root = _resolve_project_budget_target(args.project)
    if root is None:
        eprint("cctally budget: not inside a git repository")
        return 2

    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        # Guard the merge-copy: a hand-edited non-dict `budget.projects`
        # (string / non-pair list) would traceback on `dict(...)` BEFORE
        # `_get_budget_config` can raise a controlled _BudgetConfigError.
        # Mirror the `existing` budget-block guard above → clean exit 2.
        existing_projects = block.get("projects")
        if existing_projects is not None and not isinstance(
            existing_projects, dict
        ):
            eprint("cctally budget: budget.projects must be an object")
            return 2
        projects = dict(existing_projects or {})
        projects[root] = amount
        block["projects"] = projects
        config["budget"] = block
        try:
            validated = _get_budget_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally budget: {exc}")
            return 2
        block["projects"] = dict(validated["projects"])
        config["budget"] = block
        c.save_config(config)

    # Forward-only reconcile (spec §6.8): record `root`'s already-crossed
    # (project, threshold) pairs alerted_at-set WITHOUT dispatch, so setting a
    # budget mid-week (already over) doesn't storm. Scoped to the TOUCHED
    # project so it never latches a sibling's already-crossed-but-not-yet-
    # dispatched threshold (which would permanently suppress that alert).
    c._reconcile_project_budget_milestones_on_write(
        validated, touched_projects={root}
    )

    basename = os.path.basename(root) or root
    if getattr(args, "json", False):
        print(json.dumps({
            "status": "set",
            "project_key": root,
            "budget_usd": amount,
        }))
        return 0
    print(f"Project budget set: {basename} ${amount:,.2f}")
    return 0


def _cmd_budget_unset_project(args: argparse.Namespace) -> int:
    """`cctally budget unset --project[=PATH]` — remove one `budget.projects`
    entry. Idempotent (message-only when absent)."""
    c = _cctally()
    root = _resolve_project_budget_target(args.project)
    if root is None:
        eprint("cctally budget: not inside a git repository")
        return 2

    removed = False
    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        # Guard the merge-copy: a hand-edited non-dict `budget.projects`
        # would traceback on `dict(...)` before the validator can report a
        # controlled error (mirrors `_cmd_budget_set_project`).
        existing_projects = block.get("projects")
        if existing_projects is not None and not isinstance(
            existing_projects, dict
        ):
            eprint("cctally budget: budget.projects must be an object")
            return 2
        projects = dict(existing_projects or {})
        if root in projects:
            projects.pop(root)
            removed = True
        block["projects"] = projects
        config["budget"] = block
        try:
            validated = _get_budget_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally budget: {exc}")
            return 2
        c.save_config(config)

    # Reconcile scoped to the TOUCHED project. The unset removed `root` from
    # the map, so this is a no-op for `root` — and it must NOT scan the
    # remaining projects: scanning them would latch a sibling's already-crossed-
    # but-not-yet-dispatched threshold, permanently suppressing its real alert.
    c._reconcile_project_budget_milestones_on_write(
        validated, touched_projects={root}
    )

    basename = os.path.basename(root) or root
    if getattr(args, "json", False):
        print(json.dumps({
            "status": "unset" if removed else "noop",
            "project_key": root,
        }))
        return 0
    if removed:
        print(f"Project budget cleared: {basename}")
    else:
        print(f"No project budget set for: {basename}")
    return 0


def _cmd_budget_set(args: argparse.Namespace, period=None) -> int:
    """`cctally budget set AMOUNT [--period P]` — write `budget.weekly_usd`,
    preserving the other budget keys. Writes the DEFAULT config (F4). Task 3
    appends the forward-only milestone reconcile here.

    ``period`` is the canonical-normalized ``--period`` (or ``None`` = omitted).
    When omitted, the stored period is preserved (a pre-existing budget keeps
    its chosen period; first-create defaults to ``subscription-week`` via the
    validator). When supplied, it's written as ``budget.period`` (spec §2)."""
    c = _cctally()
    raw = getattr(args, "amount", None)
    if raw is None:
        eprint("cctally budget: `set` requires an amount, e.g. cctally budget set 300")
        return 2
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        eprint(f"cctally budget: amount must be a positive number, got {raw!r}")
        return 2
    if not math.isfinite(amount) or amount <= 0:
        eprint(f"cctally budget: amount must be a positive finite number, got {raw!r}")
        return 2

    # Read-modify-write under config_writer_lock + _load_config_unlocked
    # (load_config inside the lock self-deadlocks — fcntl.flock is per-fd).
    # Re-validate the merged block via _get_budget_config before persisting.
    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        block["weekly_usd"] = amount
        if period is not None:
            block["period"] = period
        config["budget"] = block
        try:
            validated = _get_budget_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally budget: {exc}")
            return 2
        block["weekly_usd"] = validated["weekly_usd"]
        block["period"] = validated["period"]
        config["budget"] = block
        c.save_config(config)

    weekly_usd = validated["weekly_usd"]
    alerts_enabled = validated["alerts_enabled"]
    thresholds = validated["alert_thresholds"]
    stored_period = validated["period"]

    # Forward-only-from-set reconcile (Task 3, spec §5): record thresholds
    # ALREADY crossed with alerted_at set but WITHOUT dispatch, so setting a
    # budget mid-week doesn't instant-popup; only LATER crossings fire. Runs
    # OUTSIDE the config_writer_lock (open_db has its own locking; reusing the
    # config lock here would needlessly serialize a stats.db write behind it).
    # Shared with `config set budget.*` + dashboard POST /api/settings via
    # _reconcile_budget_on_config_write (gated on _budget_alerts_active — a
    # budget with alerts off or no thresholds records nothing).
    c._reconcile_budget_on_config_write(validated)
    if getattr(args, "json", False):
        print(json.dumps({
            "status": "set",
            "weekly_usd": weekly_usd,
            "period": stored_period,
            "alerts_enabled": alerts_enabled,
            "alert_thresholds": list(thresholds),
        }))
        return 0
    alerts_part = "alerts on" if alerts_enabled else "alerts off"
    if thresholds:
        thr_part = " · thresholds " + " ".join(f"{t}%" for t in thresholds)
    else:
        thr_part = " · no thresholds"
    # Back-compat: the subscription-week confirmation stays byte-identical; a
    # non-default period appends a ` · <period>` segment (spec §5).
    period_part = "" if stored_period == "subscription-week" else f" · {stored_period}"
    print(
        f"Weekly budget set to ${weekly_usd:,.2f}{period_part} · "
        f"{alerts_part}{thr_part}"
    )
    return 0


def _cmd_budget_unset(args: argparse.Namespace) -> int:
    """`cctally budget unset` — clear `budget.weekly_usd` (preserve
    alerts_enabled / alert_thresholds / period). Idempotent."""
    c = _cctally()
    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        block["weekly_usd"] = None
        config["budget"] = block
        c.save_config(config)

    if getattr(args, "json", False):
        print(json.dumps({"status": "unset", "weekly_usd": None}))
        return 0
    print("Weekly budget cleared")
    return 0


def _cmd_budget_set_codex(args: argparse.Namespace, period=None) -> int:
    """`cctally budget set AMOUNT --vendor codex [--period P]` — write the
    nested ``budget.codex`` block (spec §2). ``period`` is the canonical-
    normalized ``--period`` (or ``None`` = omitted → preserve the stored period
    on an existing Codex budget, else the per-vendor default calendar-month)."""
    c = _cctally()
    raw = getattr(args, "amount", None)
    if raw is None:
        eprint(
            "cctally budget: `set --vendor codex` requires an amount, e.g. "
            "cctally budget set 200 --vendor codex --period month"
        )
        return 2
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        eprint(f"cctally budget: amount must be a positive number, got {raw!r}")
        return 2
    if not math.isfinite(amount) or amount <= 0:
        eprint(f"cctally budget: amount must be a positive finite number, got {raw!r}")
        return 2

    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        existing_codex = block.get("codex")
        if existing_codex is not None and not isinstance(existing_codex, dict):
            eprint("cctally budget: budget.codex must be an object")
            return 2
        codex_block = dict(existing_codex or {})
        codex_block["amount_usd"] = amount
        if period is not None:
            codex_block["period"] = period
        # First create with no period → the validator fills calendar-month.
        block["codex"] = codex_block
        config["budget"] = block
        try:
            validated = _get_budget_config(config)
        except _BudgetConfigError as exc:
            eprint(f"cctally budget: {exc}")
            return 2
        block["codex"] = dict(validated["codex"])
        config["budget"] = block
        c.save_config(config)

    # Forward-only-from-set reconcile (spec §6): record Codex thresholds ALREADY
    # crossed this period with alerted_at set but WITHOUT dispatch, so setting a
    # Codex budget mid-period doesn't instant-popup; only LATER crossings fire.
    # Runs OUTSIDE the config_writer_lock (open_db has its own locking). Gated on
    # codex alerts_enabled + thresholds — a Codex budget with alerts off records
    # nothing.
    c._reconcile_codex_budget_on_config_write(validated)

    codex = validated["codex"]
    amount_usd = codex["amount_usd"]
    stored_period = codex["period"]
    alerts_enabled = codex["alerts_enabled"]
    thresholds = codex["alert_thresholds"]
    if getattr(args, "json", False):
        print(json.dumps({
            "status": "set",
            "vendor": "codex",
            "amount_usd": amount_usd,
            "period": stored_period,
            "alerts_enabled": alerts_enabled,
            "alert_thresholds": list(thresholds),
        }))
        return 0
    alerts_part = "alerts on" if alerts_enabled else "alerts off"
    if thresholds:
        thr_part = " · thresholds " + " ".join(f"{t}%" for t in thresholds)
    else:
        thr_part = " · no thresholds"
    print(
        f"Codex budget set to ${amount_usd:,.2f} · {stored_period} · "
        f"{alerts_part}{thr_part}"
    )
    return 0


def _cmd_budget_unset_codex(args: argparse.Namespace) -> int:
    """`cctally budget unset --vendor codex` — remove the ``budget.codex``
    block entirely (spec §2). Idempotent."""
    c = _cctally()
    removed = False
    with c.config_writer_lock():
        config = c._load_config_unlocked()
        existing = config.get("budget")
        if existing is not None and not isinstance(existing, dict):
            eprint("cctally budget: budget config must be an object")
            return 2
        block = dict(existing or {})
        if block.get("codex") is not None:
            removed = True
        block["codex"] = None
        config["budget"] = block
        c.save_config(config)

    if getattr(args, "json", False):
        print(json.dumps({
            "status": "unset" if removed else "noop", "vendor": "codex",
        }))
        return 0
    if removed:
        print("Codex budget cleared")
    else:
        print("No Codex budget set")
    return 0


def _budget_render_unset(
    args: argparse.Namespace,
    *,
    claude_period: str = "subscription-week",
    project_rows=None,
    has_projects: bool = False,
    project_window_resolved: bool = False,
    codex_cfg=None,
    codex_inputs=None,
    codex_status=None,
    tz=None,
) -> int:
    """No GLOBAL Claude budget set → friendly stdout message, exit 0 (NOT an
    error).

    When per-project budgets ARE configured (``has_projects``), the
    per-project section is STILL rendered below the unset message (spec
    §7.3a) — a project-only configuration is fully supported. A configured
    Codex budget likewise renders as a sibling section (a Codex-only
    configuration is supported). When neither is configured, every output mode
    is byte-identical to the pre-feature behavior (no ``projects``/``codex`` key
    in --json, no extra lines).
    """
    c = _cctally()
    has_codex = codex_cfg is not None
    if getattr(args, "format", None):
        snap = _build_budget_no_budget_snapshot(args)
        snap = _append_project_share_rows(snap, project_rows, has_projects)
        snap = _append_codex_share_rows(
            snap, codex_cfg, codex_inputs, codex_status, tz
        )
        c._share_render_and_emit(snap, args)
        return 0
    if getattr(args, "json", False):
        payload = {
            "schemaVersion": _BUDGET_JSON_SCHEMA_VERSION,
            "status": "unset",
            "weekly_usd": None,
            # `period` is ALWAYS present (spec §5/§10.8) — even with no global
            # Claude budget, the configured/default Claude period rides along so
            # consumers never have to special-case an absent key.
            "period": claude_period,
        }
        if has_codex:
            _append_codex_json(payload, codex_cfg, codex_inputs, codex_status)
        if has_projects:
            _append_project_json(payload, project_rows)
        print(json.dumps(payload))
        return 0
    print("No weekly budget set. Set one with: cctally budget set <amount>.")
    _print_codex_section(codex_cfg, codex_inputs, codex_status, tz, args)
    _print_project_section_or_note(
        project_rows, has_projects, project_window_resolved, args
    )
    return 0


def _budget_verdict_ansi_code(verdict: str) -> str:
    """ANSI color code for a budget verdict: ok→green, warn→amber, over→red."""
    return {"ok": "32", "warn": "33", "over": "31"}.get(verdict, "32")


def _budget_block_lines(
    inputs, status, *, header_label, alerts_line, color
) -> list:
    """Render one budget block (header + spent/remaining/pace/projected + the
    alerts footer) as a list of lines. Shared by the Claude top block and the
    Codex sibling so their layout is identical (spec §5). ``header_label`` is
    the fully-formed first line (already carries the period/equivalent-$ cue);
    ``alerts_line`` is the pre-rendered footer."""
    c = _cctally()
    total_seconds = (inputs.week_end_at - inputs.week_start_at).total_seconds()
    elapsed_days = status.elapsed_fraction * total_seconds / 86400.0
    remaining_days = max(
        0.0, total_seconds * (1.0 - status.elapsed_fraction) / 86400.0
    )
    lines = [header_label, ""]
    lines.append(
        f"  Spent so far    ${status.spent_usd:,.2f}    "
        f"{status.consumption_pct:.1f}% of budget"
    )
    lines.append(f"  Remaining       ${status.remaining_usd:,.2f}")
    lines.append(
        f"  Pace            ${status.daily_pace_usd:,.2f}/day  ·  "
        f"{elapsed_days:.1f} d elapsed"
    )
    lines.append(
        f"  Daily budget    ${status.daily_budget_remaining_usd:,.2f}/day for the "
        f"{remaining_days:.1f} d left to stay under"
    )
    verdict_label = {"ok": "OK", "warn": "WARN", "over": "OVER"}.get(
        status.verdict, status.verdict.upper()
    )
    verdict_glyph = {"ok": "✓", "warn": "⚠", "over": "✗"}.get(status.verdict, "")
    verdict_text = c._style_ansi(
        f"{verdict_glyph} {verdict_label}".strip(),
        _budget_verdict_ansi_code(status.verdict),
        color,
    )
    proj_line = (
        f"  Projected EOW   ${status.projected_eow_low_usd:,.0f}"
        f"–${status.projected_eow_high_usd:,.0f}   →   {verdict_text}"
    )
    if status.low_confidence:
        proj_line += "   (LOW CONF — early in week)"
    lines.append(proj_line)
    lines.append("")
    lines.append(alerts_line)
    return lines


def _claude_budget_header(inputs, period, coexists, tz):
    """The Claude block's header line. Byte-identical to the legacy
    ``Weekly budget: $X   (subscription week WS → WE)`` for the
    subscription-week + no-Codex case; switches to the civil-period label (and
    an `equivalent-$` cue) once a Codex budget coexists or the period is
    non-default (spec §5)."""
    period_label = _civil_period_label(
        period, inputs.week_start_at, inputs.week_end_at, tz
    )
    if not coexists:
        # Legacy byte-identical path (subscription-week, no Codex).
        return f"Weekly budget: ${inputs.target_usd:,.2f}   ({period_label})"
    return (
        f"Claude budget: ${inputs.target_usd:,.2f}   ({period_label})"
        f"   — equivalent-$"
    )


def _budget_render_terminal(
    args, budget_cfg, inputs, status, *,
    period="subscription-week", coexists=False, tz=None,
) -> int:
    """Render the §4 Claude status block to stdout. The period header is derived
    from the DISPLAY-TZ civil boundary (spec §3); ``coexists`` switches in the
    vendor label + equivalent-$ cue only when a Codex budget exists or the
    period is non-default (byte-identical legacy render otherwise)."""
    color = _cctally()._supports_color_stdout()
    header = _claude_budget_header(inputs, period, coexists, tz)
    lines = _budget_block_lines(
        inputs, status,
        header_label=header,
        alerts_line=_budget_alerts_line(budget_cfg, status),
        color=color,
    )
    print("\n".join(lines))
    return 0


def _print_codex_section(codex_cfg, codex_inputs, codex_status, tz, args) -> None:
    """Print the Codex budget sibling section below the Claude block (spec §5).
    No-op when no Codex budget is configured so the existing terminal output
    stays byte-identical. Layout mirrors the Claude block; the header carries an
    `actual API $` cue (vs Claude's equivalent-$)."""
    if codex_cfg is None or codex_inputs is None:
        return
    c = _cctally()
    color = c._supports_color_stdout()
    period_label = _civil_period_label(
        codex_cfg["period"], codex_inputs.week_start_at,
        codex_inputs.week_end_at, tz,
    )
    header = (
        f"Codex budget: ${codex_inputs.target_usd:,.2f}   ({period_label})"
        f"   — actual API $"
    )
    # The Codex alerts footer reads the codex block's own enabled/thresholds.
    alerts_line = _budget_alerts_line(
        {
            "alerts_enabled": codex_cfg["alerts_enabled"],
            "alert_thresholds": codex_cfg["alert_thresholds"],
        },
        codex_status,
    )
    lines = _budget_block_lines(
        codex_inputs, codex_status,
        header_label=header, alerts_line=alerts_line, color=color,
    )
    print("\n" + "\n".join(lines))


def _budget_alerts_line(budget_cfg, status) -> str:
    """Render the "Alerts: ..." footer line: on/off + thresholds + crossed."""
    enabled = budget_cfg["alerts_enabled"]
    thresholds = budget_cfg["alert_thresholds"]
    if not enabled:
        return "  Alerts: off"
    if not thresholds:
        return "  Alerts: on · no thresholds configured"
    thr_str = " · ".join(f"{t}%" for t in thresholds)
    crossed = status.crossed_thresholds
    if crossed:
        crossed_str = ", ".join(f"{t}%" for t in crossed)
        tail = f"({crossed_str} crossed)"
    else:
        tail = "(none crossed yet)"
    return f"  Alerts: on · thresholds {thr_str} · {tail}"


def _budget_emit_json(
    budget_cfg, inputs, status, *, project_rows=None, has_projects=False,
    period="subscription-week", codex_cfg=None, codex_inputs=None,
    codex_status=None,
) -> int:
    """Emit the full BudgetStatus + config echo + window as JSON (schemaVersion 1).
    Window timestamps are `…Z`, ignoring display.tz (every --json is UTC).

    Additive (no schemaVersion bump — spec §5/§10.8): a ``period`` string
    (ALWAYS present) + a gated ``codex`` sibling object (only when a Codex
    budget is configured, like ``projects``). When per-project budgets are
    configured (``has_projects``), an additive ``projects: [...]`` array is
    appended. The terminal output stays byte-identical for the legacy case;
    the --json golden is regenerated to carry ``period``."""
    payload = {
        "schemaVersion": _BUDGET_JSON_SCHEMA_VERSION,
        "status": "ok",
        "weekly_usd": inputs.target_usd,
        "period": period,
        "alerts_enabled": budget_cfg["alerts_enabled"],
        "alert_thresholds": list(budget_cfg["alert_thresholds"]),
        "week_start_at": _iso_z(inputs.week_start_at),
        "week_end_at": _iso_z(inputs.week_end_at),
        "as_of": _iso_z(inputs.now),
        "spent_usd": status.spent_usd,
        "remaining_usd": status.remaining_usd,
        "consumption_pct": status.consumption_pct,
        "elapsed_fraction": status.elapsed_fraction,
        "projected_eow_low_usd": status.projected_eow_low_usd,
        "projected_eow_high_usd": status.projected_eow_high_usd,
        "week_avg_projection_usd": status.week_avg_projection_usd,
        "daily_pace_usd": status.daily_pace_usd,
        "daily_budget_remaining_usd": status.daily_budget_remaining_usd,
        "verdict": status.verdict,
        "low_confidence": status.low_confidence,
        "crossed_thresholds": list(status.crossed_thresholds),
    }
    if codex_cfg is not None:
        _append_codex_json(payload, codex_cfg, codex_inputs, codex_status)
    if has_projects:
        _append_project_json(payload, project_rows)
    print(json.dumps(payload))
    return 0


def _append_codex_json(payload, codex_cfg, codex_inputs, codex_status) -> None:
    """Attach the additive, gated ``codex`` sibling object to a budget --json
    payload (spec §5). Gated exactly like ``projects`` — only emitted when a
    Codex budget is configured, so unconfigured users keep the smaller payload.
    The amount key is ``amount_usd`` (NOT ``weekly_usd`` — a misnomer inside a
    monthly Codex block); the status fields mirror the Claude top level."""
    payload["codex"] = {
        "amount_usd": codex_inputs.target_usd,
        "period": codex_cfg["period"],
        "alerts_enabled": codex_cfg["alerts_enabled"],
        "alert_thresholds": list(codex_cfg["alert_thresholds"]),
        "period_start_at": _iso_z(codex_inputs.week_start_at),
        "period_end_at": _iso_z(codex_inputs.week_end_at),
        "as_of": _iso_z(codex_inputs.now),
        "spent_usd": codex_status.spent_usd,
        "remaining_usd": codex_status.remaining_usd,
        "consumption_pct": codex_status.consumption_pct,
        "elapsed_fraction": codex_status.elapsed_fraction,
        "projected_eow_low_usd": codex_status.projected_eow_low_usd,
        "projected_eow_high_usd": codex_status.projected_eow_high_usd,
        "week_avg_projection_usd": codex_status.week_avg_projection_usd,
        "daily_pace_usd": codex_status.daily_pace_usd,
        "daily_budget_remaining_usd": codex_status.daily_budget_remaining_usd,
        "verdict": codex_status.verdict,
        "low_confidence": codex_status.low_confidence,
        "crossed_thresholds": list(codex_status.crossed_thresholds),
    }


def _build_project_budget_rows(conn, budget_cfg, now_utc):
    """Build per-project budget status dicts for the configured projects.

    Returns ``None`` when no budget week window resolves (no usage snapshot
    yet) — the caller renders the "no usage data yet this week" note instead
    of a table. Otherwise a list of dicts (one per configured project),
    SORTED by ``consumption_pct`` descending, each carrying the same verdict
    fields as the global status (from one ``compute_budget_status`` codepath).

    Spend is the shared ``_sum_cost_by_project`` scan over the current week
    ``[week_start_at, now]``; the recent-rate input is a SECOND scan over the
    CLAMPED trailing-24h window ``[max(week_start_at, now-24h), now]``. The
    clamp at ``week_start_at`` is MANDATORY (spec §7.1): without it a fresh
    week (``now < week_start + 24h``) pulls pre-reset spend into
    ``rate_recent`` and false-WARN/OVERs a project. Mirrors the global
    ``_build_budget_status_inputs`` (forecast.py) exactly.

    A configured ``project_key`` with no matching entry this week (deleted /
    moved / never-matched repo, spec §7.2) is absent from both scan maps →
    ``spent=0`` / ``recent_24h=0`` → a ``$0 / 0% / ok`` row, never an error.
    """
    c = _cctally()
    window = _resolve_current_budget_window(conn, now_utc)
    if window is None:
        return None
    week_start_at, week_end_at = window
    week = c._sum_cost_by_project(week_start_at, now_utc, mode="auto")
    recent_start = max(week_start_at, now_utc - dt.timedelta(hours=24))
    last24h = c._sum_cost_by_project(recent_start, now_utc, mode="auto")
    thresholds = tuple(budget_cfg["alert_thresholds"])

    # Collision-aware labels via the shared primitive (#130). Same-basename
    # roots (/work/app + /personal/app) get a parent segment ("app (work)");
    # uniquely-named roots keep their bare display_key.
    labels = c._project_budget_labels(budget_cfg["projects"])

    rows = []
    for key, target in budget_cfg["projects"].items():
        inputs = c.BudgetInputs(
            target_usd=float(target),
            spent_usd=float(week.get(key, 0.0)),
            recent_24h_usd=float(last24h.get(key, 0.0)),
            week_start_at=week_start_at,
            week_end_at=week_end_at,
            now=now_utc,
            alert_thresholds=thresholds,
        )
        status = c.compute_budget_status(inputs)
        label = labels[key]
        rows.append({
            "project": label,
            "project_key": key,
            "budget_usd": float(target),
            "spent_usd": status.spent_usd,
            "consumption_pct": status.consumption_pct,
            "verdict": status.verdict,
            "low_confidence": status.low_confidence,
        })
    rows.sort(key=lambda r: r["consumption_pct"], reverse=True)
    return rows


def _render_project_budget_section(rows, *, color: bool) -> str:
    """Render the per-project budget table as plain aligned text below the
    global status block (spec §7.3). Columns:
    ``Project · Budget · Spent · Used % · Verdict`` (LOW CONF cue), already
    sorted by Used % desc by ``_build_project_budget_rows``."""
    c = _cctally()
    headers = ["Project", "Budget", "Spent", "Used %", "Verdict"]
    body = []
    for r in rows:
        verdict_label = {"ok": "OK", "warn": "WARN", "over": "OVER"}.get(
            r["verdict"], r["verdict"].upper()
        )
        if r["low_confidence"]:
            verdict_label += " (LOW CONF)"
        body.append([
            r["project"],
            f"${r['budget_usd']:,.2f}",
            f"${r['spent_usd']:,.2f}",
            f"{r['consumption_pct']:.1f}%",
            verdict_label,
        ])
    # Column widths sized to content (header + every cell).
    widths = [len(h) for h in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    # Project + Verdict left-aligned; the three money/percent columns right.
    aligns = ["<", ">", ">", ">", "<"]

    def _fmt_row(cells):
        return "  ".join(
            f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(cells)
        )

    lines = ["", "Per-project budgets:", "", ("  " + _fmt_row(headers)).rstrip()]
    for cells, r in zip(body, rows):
        rendered = ("  " + _fmt_row(cells)).rstrip()
        if color:
            code = _budget_verdict_ansi_code(r["verdict"])
            rendered = c._style_ansi(rendered, code, color)
        lines.append(rendered)
    return "\n".join(lines)


def _print_project_section_or_note(rows, has_projects, window_resolved, args):
    """Terminal helper: when projects are configured, print either the
    per-project table (window resolved) or a brief no-data note (parallel to
    the global no-data text, spec §7.3a). No-op when projects are empty so the
    existing global terminal output stays byte-identical."""
    if not has_projects:
        return
    c = _cctally()
    if not window_resolved:
        print("\nPer-project budgets: no usage data yet this week.")
        return
    print(_render_project_budget_section(rows, color=c._supports_color_stdout()))


def _append_project_share_rows(snap, rows, has_projects):
    """Append per-project ProjectCell rows to a budget ShareSnapshot so the
    share-output anonymization chokepoint (``_lib_share._scrub``) rewrites the
    basenames under default output and reveals them under ``--reveal-projects``
    (spec §7.5). No-op when projects are empty → existing share goldens stay
    byte-identical. Project names go through ``ProjectCell`` (the single
    anonymization chokepoint); the [Anonymization fails closed] invariant
    applies."""
    if not has_projects or not rows:
        return snap
    c = _cctally()
    _lib_share = c._share_load_lib()
    # Reuse the snapshot's 2-col (Metric/Value) shape but render each project
    # as a ProjectCell in the metric column + its spend in the value.
    extra_rows = []
    # A header-ish separator row keeps the per-project block visually distinct
    # without changing the column schema.
    extra_rows.append(_lib_share.Row(cells={
        "metric": _lib_share.TextCell("— Per-project budgets —"),
        "value": _lib_share.TextCell(""),
    }))
    for r in rows:
        verdict = r["verdict"].upper()
        # Explicit rank via ProjectCell.rank_cost (#130) — spend-ranks the
        # anonymized labels (matching the `project` share convention) without a
        # hidden MoneyCell. Budget / consumption / verdict stay in the visible
        # `value` TextCell.
        extra_rows.append(_lib_share.Row(cells={
            "metric": _lib_share.ProjectCell(r["project"], rank_cost=r["spent_usd"]),
            "value": _lib_share.TextCell(
                f"${r['spent_usd']:,.2f} / ${r['budget_usd']:,.2f} "
                f"({r['consumption_pct']:.0f}%) {verdict}"
            ),
        }))
    return _replace_snapshot_rows(snap, tuple(snap.rows) + tuple(extra_rows))


def _replace_snapshot_rows(snap, rows):
    """Return a copy of ``snap`` with ``rows`` replaced (frozen dataclass)."""
    return replace(snap, rows=rows)


def _append_project_json(payload: dict, project_rows) -> None:
    """Attach the additive ``projects[]`` array to a budget ``--json`` payload
    (spec §7.4). Always present when this helper is called (the caller has
    already gated on ``has_projects``); empty ``project_rows`` → ``[]`` so a
    project-only configuration with no resolvable rows still emits the key.
    Single chokepoint for the three ``--json`` paths (full status / no-data /
    unset) so the append shape stays identical across them."""
    payload["projects"] = (
        _project_rows_json(project_rows) if project_rows else []
    )


def _project_rows_json(rows) -> list:
    """Project the per-project status dicts onto the additive ``projects[]``
    JSON shape (spec §7.4): real paths, no anonymization (every --json emits
    real values, like ``project --json``)."""
    return [
        {
            "project": r["project"],
            "project_key": r["project_key"],
            "budget_usd": r["budget_usd"],
            "spent_usd": r["spent_usd"],
            "consumption_pct": r["consumption_pct"],
            "verdict": r["verdict"],
            "low_confidence": r["low_confidence"],
        }
        for r in rows
    ]


def _append_codex_share_rows(snap, codex_cfg, codex_inputs, codex_status, tz):
    """Append the Codex budget section to a budget ShareSnapshot (spec §5). The
    Codex figures are vendor-level (no project names) → nothing new to
    anonymize. No-op when no Codex budget is configured so existing share
    goldens stay byte-identical."""
    if codex_cfg is None or codex_inputs is None:
        return snap
    c = _cctally()
    _lib_share = c._share_load_lib()
    extra_rows = [
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("— Codex budget (actual API $) —"),
            "value": _lib_share.TextCell(""),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Codex budget"),
            "value": _lib_share.MoneyCell(codex_inputs.target_usd),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Codex spent so far"),
            "value": _lib_share.MoneyCell(codex_status.spent_usd),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Codex consumption"),
            "value": _lib_share.PercentCell(codex_status.consumption_pct),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Codex remaining"),
            "value": _lib_share.MoneyCell(codex_status.remaining_usd),
        }),
        _lib_share.Row(cells={
            "metric": _lib_share.TextCell("Codex verdict"),
            "value": _lib_share.TextCell(codex_status.verdict.upper()),
        }),
    ]
    return _replace_snapshot_rows(snap, tuple(snap.rows) + tuple(extra_rows))


def _build_budget_snapshot(
    args, budget_cfg, inputs, status, *,
    period="subscription-week", tz=None,
):
    """Build a `_lib_share.ShareSnapshot` (cmd="budget") for `--format` output.

    This builds the GLOBAL budget rows only; when per-project budgets are
    configured, `_append_project_share_rows` appends ProjectCell rows so
    `--reveal-projects` reveals (or `_scrub` anonymizes) the per-project
    basenames via the share chokepoint. No parallel renderer; the gate calls
    `_share_render_and_emit(snap, args)`.

    ``period`` selects the artifact's period label + title (code-review #3):
    a Claude *calendar* period (calendar-week / calendar-month) renders the
    DISPLAY-TZ civil label (e.g. ``calendar month 2026-06``) — matching the
    terminal header — instead of the UTC-instant ``week of <month-1st>``
    date-range. The legacy subscription-week artifact stays byte-identical."""
    c = _cctally()
    _lib_share = c._share_load_lib()
    tz_label = c._share_display_tz_label(getattr(args, "_resolved_tz", None))
    if period in {"calendar-week", "calendar-month"}:
        # Civil period label off the display-tz boundary (NOT the UTC instant),
        # so a month/week artifact reads "calendar month 2026-06" / "calendar
        # week 2026-06-08 → 06-15" — the same label the terminal header uses.
        period_label = _civil_period_label(
            period, inputs.week_start_at, inputs.week_end_at, tz
        )
        title = f"Budget — {period_label}"
    else:
        # subscription-week: legacy date-range label + "week of …" title
        # (byte-identical to the pre-feature artifact).
        period_label = c._share_period_label(
            inputs.week_start_at, inputs.week_end_at, tz_label
        )
        title = f"Budget — week of {inputs.week_start_at.strftime('%b %d')}"
    period_spec = _lib_share.PeriodSpec(
        start=inputs.week_start_at, end=inputs.week_end_at,
        display_tz=tz_label, label=period_label,
    )
    columns = (
        _lib_share.ColumnSpec(key="metric", label="Metric", align="left"),
        _lib_share.ColumnSpec(key="value", label="Value", align="right",
                              emphasis=True),
    )
    rows = (
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Spent so far"),
                              "value": _lib_share.MoneyCell(status.spent_usd)}),
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Consumption"),
                              "value": _lib_share.PercentCell(status.consumption_pct)}),
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Remaining"),
                              "value": _lib_share.MoneyCell(status.remaining_usd)}),
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Daily pace"),
                              "value": _lib_share.MoneyCell(status.daily_pace_usd)}),
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Projected EOW (low)"),
                              "value": _lib_share.MoneyCell(status.projected_eow_low_usd)}),
        _lib_share.Row(cells={"metric": _lib_share.TextCell("Projected EOW (high)"),
                              "value": _lib_share.MoneyCell(status.projected_eow_high_usd)}),
    )
    notes = ("LOW CONF — early in week",) if status.low_confidence else ()
    subtitle = " · ".join([
        period_label,
        args.theme,
        "real projects" if args.reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="budget",
        title=title,
        subtitle=subtitle,
        period=period_spec,
        columns=columns,
        rows=rows,
        chart=None,
        totals=(
            _lib_share.Totalled(label="Verdict", value=status.verdict.upper()),
            _lib_share.Totalled(label="Budget", value=f"${inputs.target_usd:,.2f}"),
        ),
        notes=notes,
        generated_at=c._share_now_utc(),
        version=c._share_resolve_version(),
    )


def _build_budget_no_data_snapshot(args, budget_cfg, now_utc):
    """Share snapshot for "budget set but no usage data yet this week" — a
    uniformly-shaped artifact rather than free-form text. Computes the real
    subscription-week boundaries from config so the period label is meaningful."""
    c = _cctally()
    _lib_share = c._share_load_lib()
    config = c._load_claude_config_for_args(args)
    tz = getattr(args, "_resolved_tz", None)
    tz_label = c._share_display_tz_label(tz)
    week_start_name = get_week_start_name(
        config, getattr(args, "week_start_name", None)
    )
    ws_date, we_date = compute_week_bounds(now_utc, week_start_name)
    # internal fallback: host-local intentional
    local_tz = dt.datetime.now().astimezone().tzinfo
    week_start_dt = dt.datetime.combine(ws_date, dt.time.min, tzinfo=local_tz)
    week_end_dt = dt.datetime.combine(
        we_date + dt.timedelta(days=1), dt.time.min, tzinfo=local_tz
    )
    period_label = c._share_period_label(week_start_dt, week_end_dt, tz_label)
    target = budget_cfg["weekly_usd"]
    subtitle = " · ".join([
        period_label, args.theme,
        "real projects" if args.reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="budget",
        title=f"Budget — week of {week_start_dt.strftime('%b %d')}",
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=week_start_dt, end=week_end_dt,
            display_tz=tz_label, label=period_label,
        ),
        columns=(
            _lib_share.ColumnSpec(key="metric", label="Metric", align="left"),
            _lib_share.ColumnSpec(key="value", label="Value", align="right",
                                  emphasis=True),
        ),
        rows=(
            _lib_share.Row(cells={
                "metric": _lib_share.TextCell("Weekly budget"),
                "value": _lib_share.MoneyCell(float(target)),
            }),
        ),
        chart=None,
        totals=(),
        notes=("No usage data recorded for this week yet — run "
               "cctally record-usage to populate.",),
        generated_at=c._share_now_utc(),
        version=c._share_resolve_version(),
    )


def _build_budget_no_budget_snapshot(args):
    """Share snapshot for the "no budget set" status — a uniform artifact."""
    c = _cctally()
    _lib_share = c._share_load_lib()
    now_utc = _command_as_of()
    tz = getattr(args, "_resolved_tz", None)
    if tz is None:
        # Purely defensive: the sole caller (_budget_render_unset) already
        # resolves args._resolved_tz before dispatch, so this rarely fires —
        # resolve here anyway so the artifact always carries a tz label.
        config = c._load_claude_config_for_args(args)
        tz = c.resolve_display_tz(args, config)
    tz_label = c._share_display_tz_label(tz)
    period_label = c._share_period_label(now_utc, now_utc, tz_label)
    subtitle = " · ".join([
        period_label, args.theme,
        "real projects" if args.reveal_projects else "projects anonymized",
    ])
    return _lib_share.ShareSnapshot(
        cmd="budget",
        title="Budget — no budget set",
        subtitle=subtitle,
        period=_lib_share.PeriodSpec(
            start=now_utc, end=now_utc, display_tz=tz_label, label=period_label,
        ),
        columns=(
            _lib_share.ColumnSpec(key="metric", label="Metric", align="left"),
            _lib_share.ColumnSpec(key="value", label="Value", align="right",
                                  emphasis=True),
        ),
        rows=(),
        chart=None,
        totals=(),
        notes=("No weekly budget set. Set one with: cctally budget set <amount>.",),
        generated_at=c._share_now_utc(),
        version=c._share_resolve_version(),
    )
