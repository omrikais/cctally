"""Dashboard share feature (#279 S5 F1): shims + builders + handler impls.

Consumer-only sibling of ``bin/_cctally_dashboard.py`` — it re-imports every
name below, so ``bin/cctally``'s re-exports and the share pytest files
(``tests/test_share_top_projects.py``, ``…_period_resolver.py``,
``…_v2_panel_ordering.py``) keep resolving unchanged (spec §2/§3).

What lives here (spec §3):
- the five share-CLI accessor shims (``_share_load_lib``, ``_share_now_utc``,
  ``_share_now_utc_iso``, ``_share_history_recipe_id``, ``_share_iso`` — each
  still forwards late-binding to ``sys.modules["cctally"]``, so moving the
  shim preserves the ``ns["X"]`` patch surface);
- ``_SHARE_POST_MAX_BYTES`` + the share-panel period constants
  (``_SHARE_PANELS_PERIOD_FIXED`` / ``_SHARE_PANELS_PERIOD_OVERRIDABLE``);
  the dashboard-bind validators stay in the dashboard;
- the share-period override pipeline + the per-panel share-data builders;
- the ten share handler methods as ``*_impl(handler, …)`` free functions
  (``self.`` → ``handler.`` throughout; ``type(self)`` → ``type(handler)``),
  the file's own ``_handle_get_project_detail_impl`` precedent. The
  dashboard keeps ten thin bound delegators on ``DashboardHTTPHandler``.

Cross-module reaches (spec §2.1 "fully-qualify cross-module refs"): the
cctally-forwarding accessor shims the moved code called by bare name
(``load_config``, ``get_display_tz_pref``, ``config_writer_lock``, and
``c = _cctally()``) are inlined to their ``sys.modules["cctally"].X``
call-time reach — identical behavior, ns["X"] patch surface preserved (none
is dashboard-object-patched; audited). ``get_claude_session_entries`` is
reached at call time via ``sys.modules["_cctally_dashboard"]`` (spec §3 gate
/ plan Step 2): the share tests patch the cctally namespace, but the
dashboard-object reach is strictly stronger (it ALSO honors a dashboard-
module-object patch, which the rebuild-parity cache-report tests use) and
cycle-free.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from _cctally_core import open_db, parse_iso_datetime
from _cctally_config import save_config, _load_config_unlocked
from _lib_fmt import stable_sum
from _lib_pricing import _calculate_entry_cost
from _lib_five_hour import _canonical_5h_window_key


# Share-CLI helpers consumed by the dashboard's share-data builders.
def _share_load_lib(*args, **kwargs):
    return sys.modules["cctally"]._share_load_lib(*args, **kwargs)


def _share_now_utc(*args, **kwargs):
    return sys.modules["cctally"]._share_now_utc(*args, **kwargs)


def _share_now_utc_iso(*args, **kwargs):
    return sys.modules["cctally"]._share_now_utc_iso(*args, **kwargs)


def _share_history_recipe_id(*args, **kwargs):
    return sys.modules["cctally"]._share_history_recipe_id(*args, **kwargs)


def _share_iso(*args, **kwargs):
    return sys.modules["cctally"]._share_iso(*args, **kwargs)


# #279 S1 F3: cap on /api/share/* POST bodies. The share composer sends bigger
# payloads than the 4 KB settings POSTs (a multi-panel compose recipe), but
# must still be bounded — 64 KiB comfortably exceeds any real payload (render is
# one panel; compose is ~8 panels each with a small options recipe).
_SHARE_POST_MAX_BYTES = 64 * 1024


# === Share-period override pipeline (dashboard-internal share helpers) =====
# Used by DashboardHTTPHandler's POST /api/share/render to rebuild a single
# panel's DataSnapshot against a shifted ``now_utc`` (kind=previous) or a
# custom date range (kind=custom). Pre-extract location: bin/cctally L13495.

_SHARE_PANELS_PERIOD_FIXED = ("forecast", "current-week", "sessions")
# Panels whose period is intrinsic to the panel's identity. We accept
# `kind="current"` (= no override) and reject anything else with 400.

_SHARE_PANELS_PERIOD_OVERRIDABLE = ("weekly", "daily", "monthly", "trend", "blocks")


def _share_resolve_period(panel: str, options: dict):
    """Return (now_utc_override, start_override, error_dict) for the period.

    - `(None, None, None)` — no override needed (period absent or
      `kind="current"`). Caller continues with the cached DataSnapshot.
    - `(datetime, None, None)` — `kind="previous"`. Caller rebuilds with
      this `now_utc`; window length stays at the panel default.
    - `(datetime, datetime, None)` — `kind="custom"`. Caller rebuilds
      with `now_utc = end_dt` AND a derived window length spanning
      `[start_dt, end_dt]` (computed by `_share_apply_period_override`
      per panel). Spec §6.3 advertises "Custom (start–end pickers)";
      honoring the start picker means the rendered window's left edge
      moves with it. The 2-tuple form silently ignored `start_dt`.
    - `(None, None, {...})` — validation failure; caller emits 400.

    `parse_iso_datetime` (the same parser used by every other share
    surface) accepts trailing `Z` / `+HH:MM` and naive forms. Naive
    inputs are treated as UTC by `parse_iso_datetime` and downstream
    UTC-fixup, so a date-only string like ``"2026-05-04"`` lands at
    midnight UTC.
    """
    period = options.get("period")
    if period is None or not isinstance(period, dict):
        # Absent → no override, defaults to current. (Permissive: the
        # UI always sends a period block, but older basket recipes /
        # CLI parity may omit it.)
        return (None, None, None)
    kind = period.get("kind", "current")
    if kind not in ("current", "previous", "custom"):
        return (None, None, {"error": f"unknown period kind: {kind!r}",
                              "field": "options.period.kind"})
    if panel in _SHARE_PANELS_PERIOD_FIXED:
        if kind != "current":
            return (None, None, {
                "error": (f"panel {panel!r} only supports period kind='current'; "
                          f"got {kind!r}"),
                "field": "options.period.kind",
            })
        return (None, None, None)
    # Overridable panels — handle each kind.
    if kind == "current":
        return (None, None, None)
    if kind == "previous":
        delta = _share_previous_period_delta(panel)
        return (_share_now_utc() - delta, None, None)
    # kind == "custom"
    start_str = period.get("start")
    end_str = period.get("end")
    if not isinstance(start_str, str) or not start_str \
            or not isinstance(end_str, str) or not end_str:
        return (None, None, {
            "error": "custom period requires non-empty start + end ISO dates",
            "field": "options.period",
        })
    try:
        start_dt = parse_iso_datetime(start_str, "options.period.start")
        end_dt = parse_iso_datetime(end_str, "options.period.end")
    except ValueError as exc:
        return (None, None, {"error": f"invalid period date: {exc}",
                              "field": "options.period"})
    if end_dt <= start_dt:
        return (None, None, {
            "error": ("custom period end must be strictly after start "
                      f"(got start={start_str!r}, end={end_str!r})"),
            "field": "options.period",
        })
    return (end_dt, start_dt, None)


def _share_custom_window_n(panel: str, start_dt: "dt.datetime",
                            end_dt: "dt.datetime") -> int:
    """Per-panel window length covering `[start_dt, end_dt]`, min 1.

    Each overridable panel exposes a different unit:
        - weekly / trend → weeks
        - daily          → days (inclusive)
        - monthly        → calendar months (inclusive)
    Blocks doesn't use this helper — its builder is window-anchored via
    `week_start_at`/`week_end_at`, not `n`, so we pass `start_dt`/`end_dt`
    directly to `_dashboard_build_blocks_panel`.

    Inputs are timezone-aware UTC datetimes (`parse_iso_datetime` UTCs
    naive inputs upstream). Math is purely on the timedelta + calendar
    diffs; `_dashboard_build_monthly_periods` does its own display-tz
    bucketing on the resulting window.
    """
    import math as _math
    delta_seconds = (end_dt - start_dt).total_seconds()
    delta_days = _math.ceil(delta_seconds / 86400.0)
    if panel in ("weekly", "trend"):
        return max(1, _math.ceil(delta_days / 7))
    if panel == "daily":
        return max(1, int(delta_days))
    if panel == "monthly":
        months = ((end_dt.year - start_dt.year) * 12
                  + (end_dt.month - start_dt.month) + 1)
        return max(1, months)
    # Shouldn't reach here — `_share_apply_period_override` handles
    # blocks separately. Defensive: return 1 rather than raising.
    return 1


def _share_previous_period_delta(panel: str) -> "dt.timedelta":
    """How far back `now_utc` shifts for `kind='previous'` on each panel.

    weekly/daily: 7 days. monthly: one whole month worth (we shift to
    the last day of the previous month at call time to handle variable
    month length, so this is unused — the caller routes through
    `_share_resolve_period` which special-cases monthly). trend: 8 weeks
    (one trend window). blocks: 5 hours (one block).
    """
    if panel == "weekly":
        return dt.timedelta(days=7)
    if panel == "daily":
        return dt.timedelta(days=7)
    if panel == "monthly":
        return dt.timedelta(days=30)  # close-enough for the resolver;
                                       # see _share_resolve_period_monthly
                                       # below for the calendar-aware
                                       # version when needed.
    if panel == "trend":
        return dt.timedelta(days=8 * 7)
    if panel == "blocks":
        return dt.timedelta(hours=5)
    raise ValueError(f"_share_previous_period_delta: no delta for panel {panel!r}")


def _share_apply_period_override(panel: str, options: dict,
                                  snap: "DataSnapshot | None"):
    """Return (snap_or_None, error_dict_or_None).

    Walks `_share_resolve_period`, then re-builds the panel's DataSnapshot
    field from DB when an override is requested. `dataclasses.replace`
    yields a shallow copy with one field swapped. Returns the original
    `snap` unchanged when no override applies.
    """
    if snap is None:
        # No cached snapshot to override against — return None unchanged
        # and let the panel_data builder's empty-snapshot path handle it.
        # Still validate the period option so the user gets a 400 on
        # malformed input even before the sync thread's first tick.
        _, _, err = _share_resolve_period(panel, options)
        return (snap, err)
    now_override, start_override, err = _share_resolve_period(panel, options)
    if err is not None:
        return (None, err)
    if now_override is None:
        return (snap, None)
    # For `kind="custom"`, derive a per-panel window length covering
    # `[start_override, now_override]` so the rendered window honors the
    # Start picker (spec §6.3). For `kind="previous"`, `start_override`
    # is None → window length stays at the panel's default.
    n_override = (
        _share_custom_window_n(panel, start_override, now_override)
        if start_override is not None else None
    )
    import dataclasses as _dc
    # Cross-module accessor — moved-function calls that are ALSO
    # monkeypatched in tests (``_dashboard_build_*``, ``_tui_build_trend``)
    # must resolve through cctally's namespace so ``monkeypatch.setitem(ns,
    # "_dashboard_build_weekly_periods", spy)`` propagates here per spec §5.6.
    c = sys.modules["cctally"]
    conn = open_db()
    try:
        if panel == "weekly":
            kwargs: dict = {"skip_sync": True}
            if n_override is not None:
                kwargs["n"] = n_override
            rows = c._dashboard_build_weekly_periods(conn, now_override, **kwargs)
            return (_dc.replace(snap, weekly_periods=rows), None)
        if panel == "daily":
            display_tz_name = options.get("display_tz", "Etc/UTC")
            try:
                display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
            except Exception:
                display_tz = None
            kwargs = {"skip_sync": True, "display_tz": display_tz}
            if n_override is not None:
                kwargs["n"] = n_override
            rows = c._dashboard_build_daily_panel(conn, now_override, **kwargs)
            return (_dc.replace(snap, daily_panel=rows), None)
        if panel == "monthly":
            kwargs = {"skip_sync": True}
            if n_override is not None:
                kwargs["n"] = n_override
            rows = c._dashboard_build_monthly_periods(conn, now_override, **kwargs)
            return (_dc.replace(snap, monthly_periods=rows), None)
        if panel == "trend":
            kwargs = {"skip_sync": True}
            if n_override is not None:
                kwargs["count"] = n_override
            rows = c._tui_build_trend(conn, now_override, **kwargs)
            return (_dc.replace(snap, trend=rows), None)
        if panel == "blocks":
            # `_dashboard_build_blocks_panel` is window-anchored via
            # `week_start_at`/`week_end_at`, not `n`. For `kind='custom'`,
            # use the user's [start_dt, end_dt] verbatim. For
            # `kind='previous'`, fall back to a 7-day window ending at
            # the override `now_utc` (the spec's prior-block semantics —
            # intentionally NOT aligned to subscription-week boundaries
            # since the share period override is wall-clock-aware, not
            # quota-aware).
            if start_override is not None:
                week_start_at = start_override
                week_end_at = now_override
            else:
                week_start_at = now_override - dt.timedelta(days=7)
                week_end_at = now_override
            rows = c._dashboard_build_blocks_panel(
                conn, now_override,
                week_start_at=week_start_at,
                week_end_at=week_end_at,
                skip_sync=True,
            )
            return (_dc.replace(snap, blocks_panel=rows), None)
        # forecast / current-week / sessions: resolver already gated; we
        # only reach here for `kind="current"`, which returns no
        # override.
        return (snap, None)
    finally:
        conn.close()


def _share_apply_content_toggles(snap_built, options: dict):
    """Strip chart / table from a built ShareSnapshot per render options.

    The render kernel consumes whatever the template builder emits, so
    chart/table on-off can't be expressed by the builder alone (every
    builder unconditionally emits both). Apply the toggle here, after
    the builder, before `_scrub` and `render`. ShareSnapshot is frozen;
    `dataclasses.replace` returns a new instance.

    Defaults preserve pre-toggle behavior: `show_chart` defaults to
    True, `show_table` defaults to True. Explicit False on either
    drops the corresponding payload.
    """
    import dataclasses as _dc
    show_chart = bool(options.get("show_chart", True))
    show_table = bool(options.get("show_table", True))
    changes: dict = {}
    if not show_chart:
        changes["chart"] = None
    if not show_table:
        changes["columns"] = ()
        changes["rows"] = ()
    if not changes:
        return snap_built
    return _dc.replace(snap_built, **changes)


# Cap on how many `(project, cost)` rows builders return for top_projects.
# Templates take `top_n` from options (default 5, see _lib_share_templates)
# and apply their own cap on top of this. The headroom matters because:
#   (a) the scrubber walks ProjectCells once per row, so unbounded length
#       balloons render-time anonymization cost;
#   (b) the live preview iframe streams the full table chrome;
#   (c) 20 covers any realistic `top_n` knob value (UI typically caps at 10).
_SHARE_TOP_PROJECTS_BUILDER_CAP = 20


def _share_top_projects_for_range(
    range_start: "dt.datetime",
    range_end: "dt.datetime",
    *,
    skip_sync: bool = True,
) -> list[tuple[str, float]]:
    """Aggregate session_entries in `[range_start, range_end]` by project_path.

    Returns `[(project_path_or_'(unknown)', cost_usd), ...]` sorted desc by
    cost and capped at `_SHARE_TOP_PROJECTS_BUILDER_CAP`. Templates apply
    a further `top_n` cap (default 5).

    Routes through `get_claude_session_entries` so we get `project_path`
    in the join — same cache-first/lock-contention/direct-JSONL fallback
    chain the rest of the share path relies on. `skip_sync=True` by
    default: the sync thread has already done its tick at snapshot-build
    time, and a per-request ingest would block the share render on
    `cache.db.lock`.

    Cost computation goes through `_calculate_entry_cost` — the
    single-source-of-truth pricing path. Mirrors `_compute_block_totals`'
    `by_project` bucketing exactly, so the reconcile invariant
    `SUM(top_projects) ≈ panel.cost_usd` is preserved within ULP drift
    when the panel's cost matches the same time range (e.g., current
    week, current 5h block).

    NULL `project_path` collapses to the `(unknown)` sentinel. Anon
    happens later in `_scrub()`; builders always emit real names per
    the kernel's privacy chokepoint contract.
    """
    bucket: dict[str, float] = {}
    try:
        # late-binding: reach the dashboard module object so both the ns-patch (share tests)
        # and a dashboard-object patch (rebuild-parity) are honored (#279 S5 F1 / spec §3).
        entries = sys.modules["_cctally_dashboard"].get_claude_session_entries(
            range_start, range_end, skip_sync=skip_sync,
        )
    except Exception:
        # `get_claude_session_entries` already has its own fallback chain,
        # but if even that fails (e.g., HOME unset in a fixture run with
        # no monkeypatch), don't break the whole share render — just emit
        # an empty top_projects.
        return []
    for entry in entries:
        usage = {
            "input_tokens":                entry.input_tokens,
            "output_tokens":               entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens":     entry.cache_read_tokens,
        }
        cost = _calculate_entry_cost(
            entry.model, usage, mode="auto", cost_usd=entry.cost_usd,
        )
        key = entry.project_path or "(unknown)"
        bucket[key] = bucket.get(key, 0.0) + cost
    ranked = sorted(bucket.items(), key=lambda kv: -kv[1])
    return [(path, cost) for path, cost in ranked[:_SHARE_TOP_PROJECTS_BUILDER_CAP]]


def _share_all_projects_for_range(
    range_start: "dt.datetime",
    range_end: "dt.datetime",
    *,
    skip_sync: bool = True,
) -> dict[str, float]:
    """Like `_share_top_projects_for_range` but uncapped and unsorted.

    Returns {project_path_or_'(unknown)': cost_usd} for every project
    active in the range. Caller orders or caps as needed. Used by
    `_share_per_block_per_project`'s fallback path so the fallback's
    accuracy matches the canonical rollup-table path (spec §7.2.1,
    issue #33).
    """
    bucket: dict[str, float] = {}
    try:
        # late-binding: reach the dashboard module object so both the ns-patch (share tests)
        # and a dashboard-object patch (rebuild-parity) are honored (#279 S5 F1 / spec §3).
        entries = sys.modules["_cctally_dashboard"].get_claude_session_entries(
            range_start, range_end, skip_sync=skip_sync,
        )
    except Exception:
        return bucket
    for entry in entries:
        usage = {
            "input_tokens":                entry.input_tokens,
            "output_tokens":               entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens":     entry.cache_read_tokens,
        }
        cost = _calculate_entry_cost(
            entry.model, usage, mode="auto", cost_usd=entry.cost_usd,
        )
        key = entry.project_path or "(unknown)"
        bucket[key] = bucket.get(key, 0.0) + cost
    return bucket


def _share_per_day_per_project_for_range(
    range_start: "dt.datetime",
    range_end: "dt.datetime",
    *,
    display_tz: str,
    skip_sync: bool = True,
) -> dict[str, dict[str, float]]:
    """Aggregate session_entries in [range_start, range_end] by
    (day-in-display_tz, project_path).

    Returns {date_str: {project_path_or_'(unknown)': cost_usd}}. Same
    cache-first/lock-contention/direct-JSONL fallback chain as
    `_share_top_projects_for_range`. Day bucket computed in display_tz
    so the rendered row label matches. Issue #33.
    """
    try:
        tz = ZoneInfo(display_tz) if display_tz else dt.timezone.utc
    except Exception:
        tz = dt.timezone.utc
    out: dict[str, dict[str, float]] = {}
    try:
        # late-binding: reach the dashboard module object so both the ns-patch (share tests)
        # and a dashboard-object patch (rebuild-parity) are honored (#279 S5 F1 / spec §3).
        entries = sys.modules["_cctally_dashboard"].get_claude_session_entries(
            range_start, range_end, skip_sync=skip_sync,
        )
    except Exception:
        return out
    for entry in entries:
        usage = {
            "input_tokens":                entry.input_tokens,
            "output_tokens":               entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens":     entry.cache_read_tokens,
        }
        cost = _calculate_entry_cost(
            entry.model, usage, mode="auto", cost_usd=entry.cost_usd,
        )
        day = entry.timestamp.astimezone(tz).strftime("%Y-%m-%d")
        proj = entry.project_path or "(unknown)"
        out.setdefault(day, {})
        out[day][proj] = out[day].get(proj, 0.0) + cost
    return out


def _share_per_block_per_project(
    recent_blocks: list[dict],
) -> dict[str, dict[str, float]]:
    """Aggregate per-block per-project costs from `five_hour_block_projects`.

    Returns {block_start_at_iso: {project_path_or_'(unknown)': cost_usd}}.
    Block.start_at → five_hour_window_key via `_canonical_5h_window_key`
    (10-min floor; same chokepoint as `maybe_update_five_hour_block`,
    per CLAUDE.md "5-hour windows" gotcha — never derive a third key shape).

    Fallback (rollup empty/unreadable): per-block sweep over
    `_share_all_projects_for_range` — uncapped, accuracy parity with the
    canonical path. Fires only during the first tick after fresh install
    or before stats-migration `002_five_hour_block_projects_backfill_v1`
    completes. Issue #33.
    """
    if not recent_blocks:
        return {}
    out: dict[str, dict[str, float]] = {}
    keys: list[int] = []
    iso_by_key: dict[int, str] = {}
    for b in recent_blocks:
        try:
            ts = parse_iso_datetime(b["start_at"], "share.block.start_at")
        except (ValueError, KeyError):
            continue
        wk = _canonical_5h_window_key(int(ts.timestamp()))
        keys.append(wk)
        iso_by_key[wk] = b["start_at"]
    if not keys:
        return out
    try:
        conn = open_db()
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT five_hour_window_key, project_path, cost_usd "
            f"FROM five_hour_block_projects "
            f"WHERE five_hour_window_key IN ({placeholders})",
            keys,
        ).fetchall()
        for wk, project_path, cost in rows:
            block_iso = iso_by_key.get(wk)
            if block_iso is None:
                continue
            proj = project_path or "(unknown)"
            out.setdefault(block_iso, {})
            out[block_iso][proj] = out[block_iso].get(proj, 0.0) + float(cost)
        if out:
            return out
    except (sqlite3.DatabaseError, OSError):
        pass
    # Fallback: per-block uncapped session_entries sweep.
    for b in recent_blocks:
        try:
            ts = parse_iso_datetime(b["start_at"], "share.block.start_at")
        except (ValueError, KeyError):
            continue
        end = ts + dt.timedelta(hours=5)
        out[b["start_at"]] = sys.modules["cctally"]._share_all_projects_for_range(ts, end)
    return out


def _build_share_panel_data(panel: str, options: dict,
                            snap: "DataSnapshot | None") -> dict:
    """Dispatch to the per-panel builder; reuses the dashboard DataSnapshot.

    Each per-panel builder reads from the already-built `DataSnapshot`
    rather than re-running CLI aggregation queries — keeps /api/share/render
    cheap and ensures the share artifact matches what the dashboard panel
    is currently showing.
    """
    if panel == "weekly":      return _build_weekly_share_panel_data(options, snap)
    if panel == "daily":       return _build_daily_share_panel_data(options, snap)
    if panel == "monthly":     return _build_monthly_share_panel_data(options, snap)
    if panel == "trend":       return _build_trend_share_panel_data(options, snap)
    if panel == "forecast":    return _build_forecast_share_panel_data(options, snap)
    if panel == "blocks":      return _build_blocks_share_panel_data(options, snap)
    if panel == "sessions":    return _build_sessions_share_panel_data(options, snap)
    if panel == "current-week": return _build_current_week_share_panel_data(options, snap)
    if panel == "projects":    return _build_projects_share_panel_data(options, snap)
    raise ValueError(f"unknown share panel: {panel!r}")


def _share_empty_week_stub() -> dict:
    """Minimal week shape so empty snapshots render as "no data" cleanly.

    Recap builders index `weeks[idx]` directly; supplying one zero-filled
    row keeps that access safe without leaking misleading numbers (the
    rendered artifact shows $0.00 / 0.0% — accurate for an empty install).
    """
    return {
        "start_date":     _share_now_utc().strftime("%Y-%m-%d"),
        "cost_usd":       0.0,
        "pct_used":       0.0,
        "dollar_per_pct": 0.0,
        "top_projects":   [],
    }


def _build_weekly_share_panel_data(options: dict,
                                    snap: "DataSnapshot | None") -> dict:
    """Weekly panel_data — last 8 subscription weeks + current-week index.

    Reuses `DataSnapshot.weekly_periods` (WeeklyPeriodRow list), already
    built by `_dashboard_build_weekly_periods` in the sync thread. Empty
    snapshots emit a one-week stub so the Recap builder's `weeks[idx]`
    access stays safe (renders as $0.00 / 0.0% — accurate "no data").
    """
    rows = list(getattr(snap, "weekly_periods", None) or []) if snap else []
    # weekly_periods is newest-first (see _dashboard_build_weekly_periods).
    # Take the newest 8 and reverse to oldest→newest — the Recap template
    # reads weeks[0] as the start anchor and weeks[-1] as the right-edge
    # (current-week) anchor, and current_week_index addresses that order.
    rows_8 = list(reversed(rows[:8]))
    weeks: list[dict] = []
    current_idx = 0
    for i, r in enumerate(rows_8):
        if getattr(r, "is_current", False):
            current_idx = i
        # WeeklyPeriodRow.week_start_at is an ISO datetime string; the
        # Recap shape wants a YYYY-MM-DD date label. Slice the leading
        # 10 chars (or fall back to parsing).
        wsa = getattr(r, "week_start_at", "") or ""
        start_date = wsa[:10] if isinstance(wsa, str) and len(wsa) >= 10 else wsa
        cost = float(getattr(r, "cost_usd", 0.0) or 0.0)
        used_pct_raw = getattr(r, "used_pct", None)
        used_pct = (float(used_pct_raw) / 100.0) if used_pct_raw is not None else 0.0
        dpp = float(getattr(r, "dollar_per_pct", 0.0) or 0.0)
        # Per-week top_projects: WeeklyPeriodRow doesn't carry a
        # per-project rollup, but `week_start_at` / `week_end_at` give us
        # an exact range — aggregate session_entries once per week so the
        # Recap template's `weeks[i].top_projects` table is meaningful.
        # 8 queries per share render is the perf trade; cached.
        week_end_at = getattr(r, "week_end_at", "") or ""
        top_projects: list[tuple[str, float]] = []
        try:
            ws_dt = parse_iso_datetime(wsa, "week_start_at") if isinstance(wsa, str) and wsa else None
            we_dt = parse_iso_datetime(week_end_at, "week_end_at") if isinstance(week_end_at, str) and week_end_at else None
        except ValueError:
            ws_dt = we_dt = None
        if ws_dt is not None and we_dt is not None:
            top_projects = sys.modules["cctally"]._share_top_projects_for_range(ws_dt, we_dt)
        # Per-week × per-model breakdown (issue #33 cross-tab Detail).
        models_list = getattr(r, "models", None) or []
        models = {
            (m.get("model") or "(unknown)"): float(m.get("cost_usd", 0.0) or 0.0)
            for m in models_list
        }
        weeks.append({
            "start_date":     start_date,
            "cost_usd":       cost,
            "pct_used":       used_pct,
            "dollar_per_pct": dpp,
            "top_projects":   top_projects,
            "models":         models,
        })
    if not weeks:
        weeks = [_share_empty_week_stub()]
    return {"weeks": weeks, "current_week_index": current_idx}


def _build_current_week_share_panel_data(options: dict,
                                          snap: "DataSnapshot | None") -> dict:
    """Current-week panel_data — KPI strip + daily progression + top projects.

    Synthesized from `DataSnapshot.current_week` + `daily_panel` (no 1:1
    CLI counterpart, per spec §9.5). `daily_progression` clips the daily
    panel to the current subscription week.
    """
    cw = getattr(snap, "current_week", None) if snap else None
    daily = list(getattr(snap, "daily_panel", None) or []) if snap else []
    if cw is None:
        # Empty-shape fallback — Recap builder renders "no data" gracefully.
        return {
            "kpi_cost_usd":       0.0,
            "kpi_pct_used":       0.0,
            "kpi_dollar_per_pct": 0.0,
            "kpi_days_remaining": 0.0,
            "daily_progression":  [],
            "top_projects":       [],
            "week_start_date":    _share_now_utc().strftime("%Y-%m-%d"),
            "display_tz":         options.get("display_tz", "Etc/UTC"),
        }
    week_start = getattr(cw, "week_start_at", None)
    week_end = getattr(cw, "week_end_at", None)
    week_start_date = (
        week_start.strftime("%Y-%m-%d") if isinstance(week_start, dt.datetime)
        else _share_now_utc().strftime("%Y-%m-%d")
    )
    # Days remaining = hours_to_reset / 24
    days_remaining = 0.0
    if isinstance(week_end, dt.datetime):
        remaining = (week_end - _share_now_utc()).total_seconds() / 86400.0
        days_remaining = max(0.0, remaining)
    used_pct = float(getattr(cw, "used_pct", 0.0) or 0.0) / 100.0
    progression: list[dict] = []
    if isinstance(week_start, dt.datetime):
        ws_date = week_start.date()
        # daily_panel is newest-first; iterate reversed so progression is
        # oldest→newest, matching the Recap template's progression[-1] =
        # today contract and the chart's left→right time axis.
        for r in reversed(daily):
            try:
                d = dt.date.fromisoformat(getattr(r, "date", "") or "")
            except ValueError:
                continue
            if d >= ws_date:
                progression.append({
                    "date":     d.isoformat(),
                    "cost_usd": float(getattr(r, "cost_usd", 0.0) or 0.0),
                })
    # Current-week top_projects: aggregate from `[week_start, now]`.
    # `cw.week_end_at` is the reset instant; using `now` keeps the rollup
    # symmetric with the panel's "spent this week" KPI (week-to-date).
    top_projects: list[tuple[str, float]] = []
    if isinstance(week_start, dt.datetime):
        top_projects = sys.modules["cctally"]._share_top_projects_for_range(
            week_start, _share_now_utc(),
        )
    return {
        "kpi_cost_usd":       float(getattr(cw, "spent_usd", 0.0) or 0.0),
        "kpi_pct_used":       used_pct,
        "kpi_dollar_per_pct": float(getattr(cw, "dollars_per_percent", 0.0) or 0.0),
        "kpi_days_remaining": days_remaining,
        "daily_progression":  progression,
        "top_projects":       top_projects,
        "week_start_date":    week_start_date,
        "display_tz":         options.get("display_tz", "Etc/UTC"),
    }


def _build_trend_share_panel_data(options: dict,
                                   snap: "DataSnapshot | None") -> dict:
    """Trend panel_data — 8 weeks of $/% + 3-week delta KPI.

    Reuses `DataSnapshot.trend` (TuiTrendRow list, already 8 rows).
    """
    trend = list(getattr(snap, "trend", None) or []) if snap else []
    weeks: list[dict] = []
    for r in trend:
        wsa = getattr(r, "week_start_at", None)
        start_date = (
            wsa.strftime("%Y-%m-%d") if isinstance(wsa, dt.datetime)
            else (str(wsa)[:10] if wsa else "")
        )
        used_pct_raw = getattr(r, "used_pct", None)
        used_pct = (float(used_pct_raw) / 100.0) if used_pct_raw is not None else 0.0
        dpp = float(getattr(r, "dollars_per_percent", 0.0) or 0.0)
        weeks.append({
            "start_date":     start_date,
            "cost_usd":       dpp * (used_pct * 100.0),  # ≈ row total
            "pct_used":       used_pct,
            "dollar_per_pct": dpp,
        })
    # Compute 3-week delta: compare last row vs row-4-from-end.
    delta = {"dpp_change_pct": 0.0, "cost_change_usd": 0.0}
    if len(weeks) >= 4:
        cur = weeks[-1]
        ref = weeks[-4]
        if ref["dollar_per_pct"]:
            delta["dpp_change_pct"] = (
                (cur["dollar_per_pct"] - ref["dollar_per_pct"]) / ref["dollar_per_pct"]
            )
        delta["cost_change_usd"] = cur["cost_usd"] - ref["cost_usd"]
    return {"weeks": weeks, "delta_3_weeks": delta}


def _build_daily_share_panel_data(options: dict,
                                   snap: "DataSnapshot | None") -> dict:
    """Daily panel_data — last 7 days with top model per day + top projects.

    Reuses `DataSnapshot.daily_panel` (DailyPanelRow list, 30 rows in
    full); clips to the most recent 7 for the Recap.
    """
    daily = list(getattr(snap, "daily_panel", None) or []) if snap else []
    # daily_panel is newest-first (today at index 0); take the most recent
    # 7 and reverse to oldest→newest so the Recap template's days[-1]
    # anchor lands on today.
    last_7 = list(reversed(daily[:7]))
    total = stable_sum(float(getattr(r, "cost_usd", 0.0) or 0.0) for r in last_7) or 1.0
    days: list[dict] = []
    for r in last_7:
        cost = float(getattr(r, "cost_usd", 0.0) or 0.0)
        models = getattr(r, "models", None) or []
        top_model = (models[0].get("model") if models else None) or "—"
        days.append({
            "date":          getattr(r, "date", "") or "",
            "cost_usd":      cost,
            "pct_of_period": cost / total,
            "top_model":     top_model,
        })
    # `days[*].date` is bucketed in display_tz by `_dashboard_build_daily_panel`,
    # so the query window must use display-tz midnights too — otherwise entries
    # near midnight (up to ±UTC-offset hours) get queried under the wrong UTC
    # day and either spill into Other or vanish from cross-tab cells while
    # still counted in the row total.
    display_tz_name = options.get("display_tz", "Etc/UTC")
    try:
        _range_tz = ZoneInfo(display_tz_name) if display_tz_name else dt.timezone.utc
    except Exception:
        _range_tz = dt.timezone.utc
    # Daily top_projects: aggregate over the 7-day window. Derive the
    # range from the dates rendered above so the rollup covers exactly
    # what the panel shows (rather than re-deriving "7 days ago" from
    # now and potentially clipping the oldest bucket).
    top_projects: list[tuple[str, float]] = []
    if days:
        try:
            first_date = dt.date.fromisoformat(days[0]["date"])
            last_date = dt.date.fromisoformat(days[-1]["date"])
            range_start = dt.datetime(
                first_date.year, first_date.month, first_date.day,
                tzinfo=_range_tz,
            )
            # Include the last day in full — end-exclusive boundary at
            # the start of the next display-tz day.
            range_end = dt.datetime(
                last_date.year, last_date.month, last_date.day,
                tzinfo=_range_tz,
            ) + dt.timedelta(days=1)
            top_projects = sys.modules["cctally"]._share_top_projects_for_range(range_start, range_end)
        except (ValueError, KeyError):
            top_projects = []
    # Per-day × per-project breakdown (issue #33 cross-tab Detail).
    per_day_per_project: dict[str, dict[str, float]] = {}
    if days:
        try:
            first_date = dt.date.fromisoformat(days[0]["date"])
            last_date = dt.date.fromisoformat(days[-1]["date"])
            pdpp_range_start = dt.datetime(
                first_date.year, first_date.month, first_date.day,
                tzinfo=_range_tz,
            )
            pdpp_range_end = dt.datetime(
                last_date.year, last_date.month, last_date.day,
                tzinfo=_range_tz,
            ) + dt.timedelta(days=1)
            per_day_per_project = sys.modules["cctally"]._share_per_day_per_project_for_range(
                pdpp_range_start, pdpp_range_end,
                display_tz=display_tz_name,
            )
        except (ValueError, KeyError):
            per_day_per_project = {}
    for d in days:
        d["projects"] = per_day_per_project.get(d["date"], {})
    return {"days": days, "top_projects": top_projects}


def _build_monthly_share_panel_data(options: dict,
                                     snap: "DataSnapshot | None") -> dict:
    """Monthly panel_data — last 12 months + top projects.

    Reuses `DataSnapshot.monthly_periods` (MonthlyPeriodRow list).
    `used_pct` isn't stored on MonthlyPeriodRow (monthly aggregates
    don't carry a subscription-quota %), so it surfaces as 0.0.
    """
    rows = list(getattr(snap, "monthly_periods", None) or []) if snap else []
    # monthly_periods is newest-first (see _dashboard_build_monthly_periods).
    # Reverse to oldest→newest — the Recap template reads months[0] as the
    # period-start anchor and months[-1] as the most recent month.
    rows = list(reversed(rows))
    months: list[dict] = []
    for r in rows:
        models_list = getattr(r, "models", None) or []
        top_model = (models_list[0].get("model") if models_list else None) or "—"
        # Per-month × per-model breakdown (issue #33 cross-tab Detail).
        models = {
            (m.get("model") or "(unknown)"): float(m.get("cost_usd", 0.0) or 0.0)
            for m in models_list
        }
        months.append({
            "month":     getattr(r, "label", "") or "",  # "YYYY-MM"
            "cost_usd":  float(getattr(r, "cost_usd", 0.0) or 0.0),
            "pct_used":  0.0,
            "top_model": top_model,
            "models":    models,
        })
    # Monthly top_projects: aggregate across the entire 12-month window.
    # Range = [first day of oldest month, last day of newest month + 1].
    top_projects: list[tuple[str, float]] = []
    if months:
        try:
            oldest_year, oldest_month = months[0]["month"].split("-")
            newest_year, newest_month = months[-1]["month"].split("-")
            range_start = dt.datetime(
                int(oldest_year), int(oldest_month), 1,
                tzinfo=dt.timezone.utc,
            )
            # End-exclusive: first day of the month AFTER the newest one.
            ny, nm = int(newest_year), int(newest_month) + 1
            if nm == 13:
                ny += 1
                nm = 1
            range_end = dt.datetime(ny, nm, 1, tzinfo=dt.timezone.utc)
            top_projects = sys.modules["cctally"]._share_top_projects_for_range(range_start, range_end)
        except (ValueError, KeyError):
            top_projects = []
    return {"months": months, "top_projects": top_projects}


def _build_forecast_share_panel_data(options: dict,
                                      snap: "DataSnapshot | None") -> dict:
    """Forecast panel_data — projection + per-day budgets + days-to-ceiling.

    Reuses ``DataSnapshot.forecast`` (ForecastOutput) and, when populated
    by the sync thread, ``DataSnapshot.forecast_view`` (the kernel
    wrapper from issue #57) for the (100, 90) budget pair.
    ``projection_curve`` is synthesized from ``r_avg`` / ``r_recent`` /
    ``inputs.p_now`` — the same arithmetic ``snapshot_to_envelope`` does
    for ``week_avg_projection_pct`` / ``recent_24h_projection_pct``,
    extended across the next 7 days.
    """
    fc = getattr(snap, "forecast", None) if snap else None
    fc_view = getattr(snap, "forecast_view", None) if snap else None
    if fc is None:
        return {
            "projected_end_pct":  0.0,
            "days_to_100pct":     0.0,
            "days_to_90pct":      0.0,
            "daily_budgets": {
                "avg": 0.0, "recent_24h": 0.0,
                "until_90pct": 0.0, "until_100pct": 0.0,
            },
            "projection_curve": [],
            "confidence":       "LOW CONF",
        }
    inputs = getattr(fc, "inputs", None)
    p_now = float(getattr(inputs, "p_now", 0.0) or 0.0) if inputs else 0.0
    remaining_hours = float(
        getattr(inputs, "remaining_hours", 0.0) or 0.0
    ) if inputs else 0.0
    confidence = getattr(inputs, "confidence", "ok") if inputs else "ok"
    r_avg = float(getattr(fc, "r_avg", 0.0) or 0.0)
    r_recent_raw = getattr(fc, "r_recent", None)
    r_recent = float(r_recent_raw) if r_recent_raw is not None else r_avg
    # End-of-week projected %
    projected_end_pct = (p_now + r_avg * remaining_hours) / 100.0
    # Days to ceilings (simple inverse: hours-to-target / 24)
    def _days_to_ceiling(target_pct: float) -> float:
        if r_avg <= 0 or p_now >= target_pct:
            return 0.0
        hours = (target_pct - p_now) / r_avg
        return max(0.0, hours / 24.0)
    days_to_100 = _days_to_ceiling(100.0)
    days_to_90 = _days_to_ceiling(90.0)
    # Daily budgets — prefer ForecastView's pre-routed pair (issue #57)
    # when available; otherwise replay the legacy ``fc.budgets`` scan
    # inline so positionally-constructed fixture snapshots still work.
    budgets: dict = {"avg": 0.0, "recent_24h": 0.0,
                     "until_90pct": 0.0, "until_100pct": 0.0}
    if fc_view is not None:
        budgets["until_100pct"] = float(
            fc_view.budget_100_per_day_usd or 0.0,
        )
        budgets["until_90pct"] = float(
            fc_view.budget_90_per_day_usd or 0.0,
        )
    else:
        for b in getattr(fc, "budgets", None) or []:
            tp = getattr(b, "target_percent", None)
            dpd = float(getattr(b, "dollars_per_day", 0.0) or 0.0)
            if tp == 100:
                budgets["until_100pct"] = dpd
            elif tp == 90:
                budgets["until_90pct"] = dpd
    # avg / recent_24h: derive from dollars-per-percent × r_avg/r_recent.
    dpp = float(getattr(inputs, "dollars_per_percent", 0.0) or 0.0) if inputs else 0.0
    budgets["avg"] = dpp * r_avg * 24.0
    budgets["recent_24h"] = dpp * r_recent * 24.0
    # Projection curve — 7-day forward, using r_avg
    today = _share_now_utc().date()
    projection_curve: list[dict] = []
    for i in range(7):
        d = today + dt.timedelta(days=i)
        pct = (p_now + r_avg * (i * 24.0)) / 100.0
        projection_curve.append({
            "date":               d.isoformat(),
            "projected_pct_used": pct,
        })
    return {
        "projected_end_pct":  projected_end_pct,
        "days_to_100pct":     days_to_100,
        "days_to_90pct":      days_to_90,
        "daily_budgets":      budgets,
        "projection_curve":   projection_curve,
        "confidence":         confidence,
    }


def _build_blocks_share_panel_data(options: dict,
                                    snap: "DataSnapshot | None") -> dict:
    """Blocks panel_data — current 5h block KPI + 8 recent blocks + top projects.

    Reuses `DataSnapshot.blocks_panel` (BlocksPanelRow list). Current
    block is the row with `is_active=True`; recent_blocks are the last 8.
    """
    rows = list(getattr(snap, "blocks_panel", None) or []) if snap else []
    current = next((r for r in rows if getattr(r, "is_active", False)), None)
    cb: dict = {}
    if current is not None:
        cb = {
            "start_at":     _share_iso(getattr(current, "start_at", None)) or "",
            "end_at":       _share_iso(getattr(current, "end_at", None)) or "",
            "cost_usd":     float(getattr(current, "cost_usd", 0.0) or 0.0),
            "pct_used":     0.0,  # BlocksPanelRow doesn't carry a %
            "tokens_total": 0,    # BlocksPanelRow drops token counts
        }
    # blocks_panel is newest-first (see _dashboard_build_blocks_panel:
    # `rows.sort(key=lambda r: r.start_at, reverse=True)`). Take the most
    # recent 8 blocks and reverse to oldest→newest so the template's chart
    # (uses enumerate(recent) for x-position) plots left→right time order.
    recent: list[dict] = []
    for r in list(reversed(rows[:8])):
        recent.append({
            "start_at": _share_iso(getattr(r, "start_at", None)) or "",
            "cost_usd": float(getattr(r, "cost_usd", 0.0) or 0.0),
        })
    # Blocks top_projects: aggregate across the window covered by
    # `recent_blocks` (the oldest block's start through the most recent
    # block's end — also the active block, if any). Mirrors what the
    # panel actually shows the user.
    top_projects: list[tuple[str, float]] = []
    if recent:
        try:
            range_start = parse_iso_datetime(
                recent[0]["start_at"], "blocks.recent_blocks[0].start_at",
            )
            # Pick the end of the latest block. `recent` is oldest→newest
            # after the slice/reverse, so `recent[-1]` is the most recent.
            # Each block is 5 hours long; if `current_block` has an
            # explicit `end_at`, prefer that since it may be the active
            # block whose end_at lives in the future.
            if cb.get("end_at"):
                range_end = parse_iso_datetime(
                    cb["end_at"], "blocks.current_block.end_at",
                )
            else:
                range_end = parse_iso_datetime(
                    recent[-1]["start_at"], "blocks.recent_blocks[-1].start_at",
                ) + dt.timedelta(hours=5)
            top_projects = sys.modules["cctally"]._share_top_projects_for_range(range_start, range_end)
        except (ValueError, KeyError):
            top_projects = []
    # Per-block × per-project breakdown (issue #33 cross-tab Detail).
    per_block_per_project = sys.modules["cctally"]._share_per_block_per_project(recent)
    for r in recent:
        r["projects"] = per_block_per_project.get(r["start_at"], {})
    return {
        "current_block": cb,
        "recent_blocks": recent,
        "top_projects":  top_projects,
    }


def _build_sessions_share_panel_data(options: dict,
                                      snap: "DataSnapshot | None") -> dict:
    """Sessions panel_data — top N sessions table.

    Reuses `DataSnapshot.sessions` (TuiSessionRow list). Truncated to
    `options.top_n` (default 15) by upstream cap before the Recap builder
    runs its own slice.
    """
    rows = list(getattr(snap, "sessions", None) or []) if snap else []
    top_n = options.get("top_n", 15)
    try:
        top_n_int = max(1, int(top_n))
    except (TypeError, ValueError):
        top_n_int = 15
    sessions: list[dict] = []
    for r in rows[:top_n_int]:
        sessions.append({
            "session_id":   getattr(r, "session_id", "") or "",
            "project_path": getattr(r, "project_label", "") or "",
            "cost_usd":     float(getattr(r, "cost_usd", 0.0) or 0.0),
            "started_at":   _share_iso(getattr(r, "started_at", None)) or "",
            "model":        getattr(r, "model_primary", "") or "",
        })
    return {"sessions": sessions}


def _build_projects_share_panel_data(options: dict,
                                      snap: "DataSnapshot | None") -> dict:
    """Projects panel_data — per-project rollup over a selectable window.

    Reuses ``DataSnapshot.projects_envelope`` already populated by the
    sync thread, so the share artifact matches what the dashboard panel
    is showing. ``options.windowWeeks`` (spec §5.4 + §7.3) selects the
    aggregation window:

      - ``windowWeeks=1`` (default): current_week only (PANEL share flow).
      - ``windowWeeks ∈ {4, 8, 12}``: sum across the trend window
        (MODAL share flow — supplies its active window pill).

    Output shape (consumed by `_build_projects_recap` / `_visual` /
    `_detail` builders below — see bin/_lib_share_templates.py):

      {
        "rows": [
          {
            "key":            "<disambiguated display_key>",
            "bucket_path":    "<absolute path>",
            "cost_usd":       <float>,
            "attributed_pct": <float | None>,
            "sessions_count": <int>,
          },
          ...                                       # desc by cost
        ],
        "total_cost_usd": <float>,
        "period_start":   <dt.datetime UTC>,
        "period_end":     <dt.datetime UTC>,
        "window_weeks":   <int>,
      }

    The Privacy invariant per spec §7.4 lives at the share-render gate
    (`_lib_share._scrub`), NOT here. This panel_data carries REAL
    display_keys + bucket_paths; downstream `_scrub` rewrites them
    when ``reveal_projects=false``.
    """
    env: dict = getattr(snap, "projects_envelope", None) or {} if snap else {}
    if not env:
        # First-tick / sub-build failure → render a minimal "no data"
        # shape. _build_project_snapshot already handles empty rows
        # downstream via "no data" title.
        now = _share_now_utc()
        return {
            "rows":           [],
            "total_cost_usd": 0.0,
            "period_start":   now - dt.timedelta(days=7),
            "period_end":     now,
            "window_weeks":   1,
        }
    weeks_back_raw = options.get("windowWeeks", 1)
    try:
        weeks_back = int(weeks_back_raw)
    except (TypeError, ValueError):
        weeks_back = 1
    if weeks_back not in {1, 4, 8, 12}:
        weeks_back = 1
    cw = env.get("current_week", {}) or {}
    trend = env.get("trend", {}) or {}

    # `effective_weeks` is the actual number of weeks of data the artifact
    # represents. For the 1-week (panel) path it's always 1. For multi-week
    # (modal) the trend envelope may carry fewer weeks than requested on
    # thin-history dashboards (fresh installs, post-rebuild), so clamp to
    # whatever history exists — otherwise the share artifact would label
    # itself "Last 12 weeks" and render a 12-week date range while only
    # (say) 3 weeks of rows were aggregated. The period bounds and the
    # `window_weeks` returned downstream both ride on `effective_weeks`.
    rows: list[dict]
    if weeks_back == 1:
        effective_weeks = 1
        rows = [
            {
                "key":            r["key"],
                "bucket_path":    r["bucket_path"],
                "cost_usd":       float(r["cost_usd"]),
                "attributed_pct": r.get("attributed_pct"),
                "sessions_count": int(r.get("sessions_count", 0) or 0),
            }
            for r in (cw.get("rows") or [])
        ]
        total_cost = float(cw.get("total_cost_usd", 0.0) or 0.0)
    else:
        # Multi-week: sum across the trailing `weeks_back` slices of
        # trend.projects[i].weekly_cost. attributed_pct sums each
        # project's weekly_pct (None when no week has a snapshot).
        n_weeks = len(trend.get("weeks") or [])
        # The trend window is already clamped to <= 12; we take the
        # trailing `weeks_back` slices.
        take = min(weeks_back, n_weeks)
        # On a brand-new dashboard with zero trend weeks, fall back to a
        # single-week (current_week) period so the artifact's labelling
        # still names a real range instead of "Last 0 weeks".
        effective_weeks = max(1, take)
        rows = []
        running_total = 0.0
        for tp in trend.get("projects") or []:
            wc = (tp.get("weekly_cost") or [])[-take:]
            wp = (tp.get("weekly_pct") or [])[-take:]
            ws = (tp.get("sessions_per_week") or [])[-take:]
            cost = float(stable_sum(wc))
            running_total += cost
            valid_pct = [float(p) for p in wp if p is not None]
            attributed = stable_sum(valid_pct) if valid_pct else None
            # Sum per-week distinct session counts. Slight over-count when a
            # single session spans a week boundary; the envelope's per-week
            # bucketing has no session-id sets to union, so this is the
            # cheapest reasonable approximation and matches the modal's
            # client-side derivation (envelope.ts → ProjectsModal.tsx).
            rows.append({
                "key":            tp["key"],
                "bucket_path":    tp["bucket_path"],
                "cost_usd":       cost,
                "attributed_pct": attributed,
                # Integer session counts — bare sum() is exact (NOT a
                # stable_sum float-output site; see test_stable_sum_chokepoint).
                "sessions_count": int(sum(ws)),
            })
        rows.sort(key=lambda r: (-r["cost_usd"], r["key"]))
        total_cost = running_total

    # Compute window bounds from the *effective* span — see the
    # `effective_weeks` note above. The rows in this panel_data are
    # week-to-date (current_week.rows are aggregated through "now"; the
    # multi-week branch sums weekly_cost slices, with the trailing slice
    # also week-to-date), so clip `period_end` to min(reset_at, now).
    # Without the clip a mid-week export advertises a future reset date
    # in the rendered period/frontmatter and disagrees with the live
    # dashboard's "spent this week" KPI, which is symmetrically clipped
    # by `_build_current_week_share_panel_data`'s use of `now`.
    cw_start_iso = cw.get("week_start_at") or _share_now_utc_iso()
    cw_start = parse_iso_datetime(cw_start_iso, "projects.cw_start")
    week_end = cw_start + dt.timedelta(days=7)
    now = _share_now_utc()
    period_end = week_end if week_end <= now else now
    period_start = cw_start - dt.timedelta(days=7 * (effective_weeks - 1))

    return {
        "rows":           rows,
        "total_cost_usd": total_cost,
        "period_start":   period_start,
        "period_end":     period_end,
        "window_weeks":   effective_weeks,
    }


# ---- share endpoints (spec §5.1) ----------------------------------
#
# GET  /api/share/templates?panel=<id> → list Recap/Visual/Detail
#      templates registered in _lib_share_templates for that panel.
# POST /api/share/render               → render one panel-section to
#      body via the kernel; returns {body, content_type, snapshot}
#      with kernel_version + data_digest for v2 composer drift checks.
#
# The template registry is late-imported per-request to keep dashboard
# startup cheap — matches cmd_tui's `rich` lazy-import pattern. Same
# late-load applies to the kernel (`_lib_share`) via `_share_load_lib`.
# GET is unauthenticated (idempotent read). POST gates on
# `_check_origin_csrf` (same convention as /api/sync, /api/settings).

def _share_load_templates_module_impl(handler):
    """Late-load the share-templates registry, cached in sys.modules.

    Keeps dashboard startup zero-cost — the registry only imports when
    the first share request arrives. Subsequent requests reuse the
    sys.modules entry; matches the `_share_load_lib` convention so
    ShareTemplate identity stays stable across calls.
    """
    cached = sys.modules.get("_lib_share_templates")
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / "_lib_share_templates.py"
    spec = _ilu.spec_from_file_location("_lib_share_templates", p)
    mod = _ilu.module_from_spec(spec)
    sys.modules["_lib_share_templates"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_lib_share_templates", None)
        raise
    return mod


def _share_source_selection(req: dict) -> tuple[str, bool]:
    """Resolve S4's optional source field without changing legacy requests."""
    explicit = "source" in req
    source = req.get("source", "claude")
    if source not in ("claude", "codex", "all"):
        raise ValueError("source capability unavailable")
    return source, explicit


def _source_state_for_share(data_snap, source: str):
    try:
        bundle = data_snap.source_bundle
        return bundle.sources[source]
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("source capability unavailable") from exc


def _share_codex_range_start(panel: str, now_utc: "dt.datetime",
                             custom_start: "dt.datetime | None") -> "dt.datetime":
    """Return the bounded cache range used for a non-current Codex share.

    The native source projection has no hidden live-current fallback: every
    range is derived from the requested panel period and the source builder is
    called with ``sync=False`` by construction.  Keep these spans aligned with
    the legacy dashboard builders' visible windows.
    """
    if custom_start is not None:
        return custom_start
    if panel == "daily":
        return now_utc - dt.timedelta(days=31)  # 30 rows plus boundary slack
    if panel == "monthly":
        year, month = now_utc.year, now_utc.month
        for _ in range(11):
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        return dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
    if panel == "weekly":
        return now_utc - dt.timedelta(days=7 * 13)
    if panel == "blocks":
        return now_utc - dt.timedelta(days=7)
    return now_utc - dt.timedelta(days=30)


def _share_codex_state_for_period(data_snap, *, panel: str, options: dict):
    """Return the selected Codex state, rebuilding non-current requests safely.

    Dashboard snapshots intentionally contain the live/current source bundle.
    Share period overrides rebuild their legacy Claude panel fields, but using
    that unchanged bundle for Codex would mislabel current provider data as a
    past/custom export.  Rebuild only the selected Codex read model over the
    requested bounded range; its source adapters are cache/stats readers and
    use ``sync=False`` internally.  The resulting state is request-local and
    never replaces the published snapshot.
    """
    now_override, start_override, err = _share_resolve_period(panel, options)
    if err is not None:
        raise ValueError("source capability unavailable")
    if now_override is None:
        return _source_state_for_share(data_snap, "codex")

    from _cctally_cache import open_cache_db
    from _cctally_dashboard_sources import (
        DashboardReadContext,
        build_codex_source_state,
        resolve_dashboard_source_semantics,
    )

    range_start = _share_codex_range_start(panel, now_override, start_override)
    config = sys.modules["cctally"].load_config()
    display_tz_name = options.get("display_tz")
    if display_tz_name == "utc":
        display_tz_name = "UTC"
    elif display_tz_name == "local" or not isinstance(display_tz_name, str):
        display_tz_name = None
    semantics = resolve_dashboard_source_semantics(
        config, display_tz_name=display_tz_name,
    )
    stats_conn = open_db()
    cache_conn = open_cache_db()
    try:
        return build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache_conn,
                stats_conn=stats_conn,
                range_start=range_start,
                now_utc=now_override,
                display_tz_name=semantics.display_tz_name,
                week_start_idx=semantics.week_start_idx,
                week_start_name=semantics.week_start_name,
                speed=semantics.speed,
                codex_budget=semantics.codex_budget,
            ),
            data_version=(
                f"share:codex:{panel}:{range_start.isoformat()}:"
                f"{now_override.isoformat()}:{semantics.identity}"
            ),
        )
    finally:
        cache_conn.close()
        stats_conn.close()


def _share_parse_bucket_start(panel: str, label: object) -> "dt.datetime | None":
    try:
        if panel == "monthly":
            return dt.datetime.strptime(str(label), "%Y-%m").replace(tzinfo=dt.timezone.utc)
        return dt.datetime.fromisoformat(str(label)).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _share_codex_period_bounds(*, state, panel: str, options: dict, rows) -> tuple:
    now_override, start_override, err = _share_resolve_period(panel, options)
    if err is not None:
        raise ValueError("source capability unavailable")
    end = now_override or state.last_success_at or dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None or end.utcoffset() is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    if start_override is not None:
        return start_override.astimezone(dt.timezone.utc), end
    starts = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw = row.get("first_seen") or row.get("last_activity") or row.get("label")
        parsed = _share_parse_bucket_start("monthly" if panel == "monthly" else "daily", raw)
        if parsed is not None:
            starts.append(parsed)
    if panel == "current-week":
        starts = [
            parsed for row in rows if isinstance(row, Mapping)
            if (parsed := _share_parse_bucket_start("weekly", row.get("label"))) is not None
        ]
        return (max(starts) if starts else end - dt.timedelta(days=7)), end
    return (min(starts) if starts else end), end


def _build_codex_source_share_snapshot(ls, *, state, panel: str,
                                       template_id: str, options: dict):
    """Adapt S4 normalized data through canonical Codex share kernels."""
    data = state.data
    if not isinstance(data, Mapping) or state.availability == "unavailable":
        raise ValueError("source capability unavailable")
    required_domain = {
        "current-week": "hero",
        "trend": "periods",
        "forecast": "quota",
        "daily": "periods",
        "monthly": "periods",
        "weekly": "periods",
        "blocks": "quota",
        "sessions": "sessions",
        "projects": "projects",
    }.get(panel)
    if required_domain is None or required_domain not in data:
        raise ValueError("source capability unavailable")
    availability = state.availability if state.availability in ("ok", "empty") else "unavailable"
    reason = "source data unavailable" if availability == "unavailable" else None
    hero = data.get("hero") if isinstance(data.get("hero"), Mapping) else {}
    if panel == "current-week":
        periods = data.get("periods")
        weekly = periods.get("weekly") if isinstance(periods, Mapping) else None
        if not isinstance(weekly, Mapping):
            raise ValueError("source capability unavailable")
        all_rows = tuple(weekly.get("rows", ()))
        source_rows = all_rows[-1:] if all_rows else ()
        command = "codex-weekly"
        display_tz = str(weekly.get("display_tz") or "UTC")
    elif panel in ("daily", "monthly", "weekly", "trend"):
        periods = data.get("periods")
        period_key = "weekly" if panel == "trend" else panel
        panel_data = periods.get(period_key) if isinstance(periods, Mapping) else {}
        if not isinstance(panel_data, Mapping):
            raise ValueError("source capability unavailable")
        source_rows = tuple(panel_data.get("rows", ()))
        command = f"codex-{period_key}"
        display_tz = str(panel_data.get("display_tz") or "UTC")
    elif panel == "forecast":
        quota = data.get("quota")
        panel_data = quota if isinstance(quota, Mapping) else {}
        source_rows = tuple(panel_data.get("histories", ())) if isinstance(panel_data, Mapping) else ()
        start, end = _share_codex_period_bounds(
            state=state, panel="weekly", options=options, rows=source_rows,
        )
        rows = []
        for row in source_rows:
            if not isinstance(row, Mapping):
                continue
            forecast = row.get("forecast") if isinstance(row.get("forecast"), Mapping) else {}
            current = row.get("current_percent")
            projected = forecast.get("projected_percent")
            rows.append(ls.Row(cells={
                "limit": ls.TextCell(str(row.get("label") or "Codex quota")),
                "current": ls.TextCell("—" if current is None else f"{float(current):.1f}%"),
                "projected": ls.TextCell("—" if projected is None else f"{float(projected):.1f}%"),
            }))
        return ls.ShareSnapshot(
            cmd="codex-quota", title="Codex Quota Forecast", subtitle=None,
            period=ls.PeriodSpec(start=start, end=end, display_tz="UTC", label=None),
            columns=(
                ls.ColumnSpec(key="limit", label="Limit"),
                ls.ColumnSpec(key="current", label="Current", align="right"),
                ls.ColumnSpec(key="projected", label="Projected", align="right"),
            ),
            rows=tuple(rows), chart=None, totals=(), notes=(), generated_at=end,
            version=sys.modules["cctally"]._share_resolve_version(),
            template_id=template_id, source="codex", source_label="Codex",
            availability=availability, availability_reason=reason,
        )
    elif panel == "sessions":
        panel_data = data.get(panel) if isinstance(data.get(panel), Mapping) else {}
        source_rows = tuple(panel_data.get("rows", ())) if isinstance(panel_data, Mapping) else ()
        command = "codex-session"
        display_tz = "UTC"
    elif panel == "projects":
        panel_data = data.get("projects") if isinstance(data.get("projects"), Mapping) else {}
        source_rows = tuple(panel_data.get("rows", ())) if isinstance(panel_data, Mapping) else ()
        start, end = _share_codex_period_bounds(
            state=state, panel=panel, options=options, rows=source_rows,
        )
        rows = tuple(ls.Row(cells={
            "project": ls.ProjectCell(
                str(row.get("label", "Project")),
                float(row.get("cost_usd", 0.0) or 0.0),
                identity=str(row.get("key")),
            ),
            "tokens": ls.TextCell(f"{int(row.get('total_tokens', 0) or 0):,}"),
            "cost": ls.MoneyCell(float(row.get("cost_usd", 0.0) or 0.0)),
        }) for row in source_rows if isinstance(row, Mapping))
        return ls.ShareSnapshot(
            cmd="project", title="Codex Project Usage", subtitle=None,
            period=ls.PeriodSpec(start=start, end=end, display_tz="UTC", label=None),
            columns=(
                ls.ColumnSpec(key="project", label="Project"),
                ls.ColumnSpec(key="tokens", label="Tokens", align="right"),
                ls.ColumnSpec(key="cost", label="$ Cost", align="right"),
            ),
            rows=rows, chart=None,
            totals=(ls.Totalled(label="Total", value=f"${float(panel_data.get('total_cost_usd', 0.0) or 0.0):,.2f}"),),
            notes=(), generated_at=end, version=sys.modules["cctally"]._share_resolve_version(),
            template_id=template_id, source="codex", source_label="Codex",
            availability=availability, availability_reason=reason,
        )
    else:  # blocks
        quota = data.get("quota")
        panel_data = quota if isinstance(quota, Mapping) else {}
        source_rows = tuple(panel_data.get("blocks", ())) if isinstance(panel_data, Mapping) else ()
        start, end = _share_codex_period_bounds(
            state=state, panel=panel, options=options, rows=source_rows,
        )
        columns = (
            ls.ColumnSpec(key="label", label="Quota", align="left"),
            ls.ColumnSpec(key="usage", label="Usage", align="right"),
            ls.ColumnSpec(key="resets", label="Resets", align="right"),
        )
        def cells(row):
            percent = row.get("current_percent", 0.0)
            return {
                "label": ls.TextCell(str(row.get("label", "Codex quota"))),
                "usage": ls.TextCell(f"{float(percent or 0.0):.1f}%"),
                "resets": ls.TextCell(str(row.get("resets_at", "—"))),
            }
        rows = tuple(ls.Row(cells=cells(row)) for row in source_rows if isinstance(row, Mapping))
        return ls.ShareSnapshot(
            cmd="codex-quota", title="Codex Quota Windows", subtitle=None,
            period=ls.PeriodSpec(start=start, end=end, display_tz="UTC", label=None),
            columns=columns, rows=rows, chart=None,
            totals=(), notes=(), generated_at=end,
            version=sys.modules["cctally"]._share_resolve_version(),
            template_id=template_id, source="codex", source_label="Codex",
            availability=availability, availability_reason=reason,
        )

    start, end = _share_codex_period_bounds(
        state=state, panel=panel, options=options, rows=source_rows,
    )
    normalized_rows = tuple(
        SimpleNamespace(
            bucket=str(row.get("label", "—")),
            total_tokens=int(row.get("total_tokens", 0) or 0),
            cost_usd=float(row.get("cost_usd", 0.0) or 0.0),
            last_activity=parse_iso_datetime(str(row.get("last_activity")), "codex.session.last_activity")
            if command == "codex-session" else None,
        )
        for row in source_rows if isinstance(row, Mapping)
    )
    view = SimpleNamespace(
        rows=normalized_rows,
        total_cost_usd=stable_sum(row.cost_usd for row in normalized_rows),
        total_tokens=sum(row.total_tokens for row in normalized_rows),
        period_start=start,
        period_end=end,
        display_tz_label=display_tz,
    )
    codex_module = sys.modules["cctally"]._load_sibling("_cctally_codex")
    return replace(
        codex_module._build_codex_share_snapshot(command, view, normalized_rows),
        template_id=template_id,
    )


def _share_plain_value(value):
    if isinstance(value, Mapping):
        return {str(key): _share_plain_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_share_plain_value(item) for item in value]
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    return value


def _share_state_domain(state, panel: str):
    data = state.data if isinstance(state.data, Mapping) else {}
    if panel in ("daily", "monthly", "weekly", "current-week"):
        periods = data.get("periods") if isinstance(data.get("periods"), Mapping) else {}
        key = "weekly" if panel == "current-week" else panel
        return periods.get(key)
    if panel == "blocks":
        return data.get("quota")
    return data.get(panel)


def _share_digest_input(*, panel: str, template_id: str, source: str,
                        source_explicit: bool, states, snapshots,
                        panel_data):
    if not source_explicit:
        return {
            "panel": panel,
            "template_id": template_id,
            "panel_data": panel_data,
        }
    providers = []
    for state, snapshot in zip(states, snapshots):
        providers.append({
            "source": state.source,
            "data_version": state.data_version,
            "availability": state.availability,
            "period": {
                "start": snapshot.period.start,
                "end": snapshot.period.end,
                "display_tz": snapshot.period.display_tz,
            },
            "data": _share_plain_value(_share_state_domain(state, panel)),
        })
    return {
        "panel": panel,
        "template_id": template_id,
        "source": source,
        "providers": providers,
        **(
            {"claude_panel_data": panel_data}
            if source in ("claude", "all") else {}
        ),
    }


def _share_build_source_snapshots(*, ls, template, template_id: str,
                                  panel: str, options: dict, source: str,
                                  source_explicit: bool, data_snap):
    """Branch by provider before invoking any provider-specific builder."""
    claude_snapshot = None
    claude_state = None
    panel_data = None
    if source in ("claude", "all"):
        claude_data_snap, period_err = _share_apply_period_override(
            panel, options, data_snap,
        )
        if period_err is not None:
            raise _SharePeriodError(period_err)
        panel_data = _build_share_panel_data(panel, options, claude_data_snap)
        claude_snapshot = replace(
            template.builder(panel_data=panel_data, options=options),
            template_id=template_id,
        )
        if source_explicit or source == "all":
            claude_snapshot = replace(
                claude_snapshot, source="claude", source_label="Claude",
            )
        # A source-less request is the shipped legacy Claude contract.  It
        # must remain usable by callers whose synthetic/older DataSnapshot
        # does not carry the additive S4 source bundle.
        if source_explicit or source == "all":
            claude_state = _source_state_for_share(data_snap, "claude")

    codex_snapshot = None
    codex_state = None
    if source in ("codex", "all"):
        codex_state = _share_codex_state_for_period(
            data_snap, panel=panel, options=options,
        )
        codex_snapshot = _build_codex_source_share_snapshot(
            ls,
            state=codex_state,
            panel=panel,
            template_id=template_id,
            options=options,
        )

    if source == "claude":
        return (claude_snapshot,), (claude_state,), panel_data
    if source == "codex":
        return (codex_snapshot,), (codex_state,), None
    return (
        (claude_snapshot, codex_snapshot),
        (claude_state, codex_state),
        panel_data,
    )


class _SharePeriodError(ValueError):
    """Carry the established period-validation envelope across dispatch."""

    def __init__(self, payload: Mapping):
        super().__init__(str(payload.get("error", "invalid period")))
        self.payload = dict(payload)


def _share_public_failure(handler, exc: Exception, *, phase: str,
                          capability: bool = False) -> None:
    handler.log_error("/api/share/%s failed: %r", phase, exc)
    if capability:
        handler._respond_json(400, {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        })
    else:
        handler._respond_json(500, {
            "code": "source_render_failed",
            "error": "source render failed",
        })

def _handle_share_templates_get_impl(handler) -> None:
    """List share templates registered for the requested panel.

    Query: ?panel=<id>. Rejects missing or non-share-capable panels
    (e.g., `alerts`) with 400 + {error, field} envelope (matches
    existing dashboard error shape; see spec §5.5).
    """
    import urllib.parse as _urlparse
    qs = _urlparse.urlparse(handler.path).query
    params = _urlparse.parse_qs(qs)
    panel = (params.get("panel", [""])[0] or "").strip()
    if not panel:
        handler._respond_json(400, {
            "error": "missing query param: panel",
            "field": "panel",
        })
        return
    tpl_mod = handler._share_load_templates_module()
    if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
        handler._respond_json(400, {
            "error": f"unknown share panel: {panel!r}",
            "field": "panel",
        })
        return
    templates = [
        {
            "id": t.id,
            "label": t.label,
            "description": t.description,
            "default_options": dict(t.default_options),
        }
        for t in tpl_mod.templates_for_panel(panel)
    ]
    handler._respond_json(200, {"panel": panel, "templates": templates})

def _handle_share_render_post_impl(handler) -> None:
    """Render a panel-section to body via the share kernel.

    Body shape: ``{panel, template_id, options}``. Validates panel +
    template_id against the registry, dispatches to the per-panel
    `_build_<panel>_share_panel_data` helper to assemble the
    builder-shaped dict from the current dashboard snapshot, runs the
    template's builder, applies `_scrub` when
    ``options.reveal_projects`` is False, then renders via
    `_lib_share.render`. Response: ``{body, content_type, snapshot}``
    where `snapshot` carries `kernel_version` + `data_digest` for the
    v2 composer's drift detection (spec §5.2).

    CSRF: Origin/Host parity via `_check_origin_csrf` — same gate as
    `/api/sync`, `/api/settings`, `/api/alerts/test`.
    """
    if not handler._check_origin_csrf():
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length > _SHARE_POST_MAX_BYTES:
        # #279 S1 F3: bound the body before reading it (memory/slow-loris).
        # length == 0 stays allowed below (empty body -> {}); cap only the top.
        handler._respond_json(400, {"error": "body too large (max 64 KiB)"})
        return
    try:
        raw = handler.rfile.read(length) if length > 0 else b""
        req = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        handler._respond_json(400, {"error": "malformed json"})
        return
    if not isinstance(req, dict):
        handler._respond_json(400, {"error": "expected JSON object"})
        return
    try:
        source, source_explicit = _share_source_selection(req)
    except ValueError:
        handler._respond_json(400, {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        })
        return
    panel = req.get("panel")
    template_id = req.get("template_id")
    options = req.get("options") or {}
    if not isinstance(options, dict):
        handler._respond_json(400, {
            "error": "options must be an object",
            "field": "options",
        })
        return
    # Client `ShareOptions` (dashboard/web/src/share/types.ts) does
    # not carry `display_tz`; server-side config is the source of
    # truth. Inject before `_share_apply_period_override` so the
    # daily panel rebuild and per-day cross-tab bucketing both see
    # the user's display tz instead of falling back to UTC.
    if "display_tz" not in options:
        options["display_tz"] = sys.modules["cctally"].get_display_tz_pref(sys.modules["cctally"].load_config())
    if not isinstance(panel, str) or not panel:
        handler._respond_json(400, {
            "error": "missing or non-string panel",
            "field": "panel",
        })
        return
    if not isinstance(template_id, str) or not template_id:
        handler._respond_json(400, {
            "error": "missing or non-string template_id",
            "field": "template_id",
        })
        return
    fmt = options.get("format", "html")
    if fmt not in ("md", "html", "svg"):
        handler._respond_json(400, {
            "error": f"unknown format: {fmt!r}",
            "field": "options.format",
        })
        return
    theme = options.get("theme", "light")
    if theme not in ("light", "dark"):
        handler._respond_json(400, {
            "error": f"unknown theme: {theme!r}",
            "field": "options.theme",
        })
        return
    # `top_n` may be explicit-null when the UI's Top-N input is
    # cleared (Knobs.tsx:43); treat null as "use template default"
    # rather than 400-ing every preview/export until the user types
    # a number.
    if options.get("top_n") is not None:
        top_n_raw = options["top_n"]
        if not isinstance(top_n_raw, int) or isinstance(top_n_raw, bool) or top_n_raw < 1:
            handler._respond_json(400, {
                "error": f"top_n must be a positive integer, got {top_n_raw!r}",
                "field": "options.top_n",
            })
            return

    tpl_mod = handler._share_load_templates_module()
    if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
        handler._respond_json(400, {
            "error": f"unknown share panel: {panel!r}",
            "field": "panel",
        })
        return
    try:
        template = tpl_mod.get_template(template_id)
    except KeyError:
        handler._respond_json(400, {
            "error": f"unknown template_id: {template_id!r}",
            "field": "template_id",
        })
        return
    if template.panel != panel:
        handler._respond_json(400, {
            "error": (
                f"template_id {template_id!r} belongs to panel "
                f"{template.panel!r}, not {panel!r}"
            ),
            "field": "template_id",
        })
        return

    snap_ref = type(handler).snapshot_ref
    data_snap = snap_ref.get() if snap_ref is not None else None
    ls = _share_load_lib()
    try:
        source_snaps, source_states, panel_data = _share_build_source_snapshots(
            ls=ls,
            template=template,
            template_id=template_id,
            panel=panel,
            options=options,
            source=source,
            source_explicit=source_explicit,
            data_snap=data_snap,
        )
    except _SharePeriodError as exc:
        handler._respond_json(400, exc.payload)
        return
    except ValueError as exc:
        _share_public_failure(handler, exc, phase="render provider", capability=True)
        return
    except Exception as exc:
        _share_public_failure(handler, exc, phase="render provider")
        return
    source_snaps = tuple(
        _share_apply_content_toggles(item, options) for item in source_snaps
    )
    reveal = bool(options.get("reveal_projects", True))
    if not reveal:
        source_snaps = tuple(
            ls._scrub(item, reveal_projects=False) for item in source_snaps
        )
    try:
        if source == "all":
            body = ls.compose(
                tuple(
                    ls.ComposedSection(snap=item, drift_detected=False)
                    for item in source_snaps
                ),
                opts=ls.ComposeOptions(
                    title=f"Claude + Codex {panel.replace('-', ' ').title()}",
                    theme=options.get("theme", "light"), format=fmt,
                    no_branding=bool(options.get("no_branding", False)),
                    reveal_projects=reveal,
                ),
            )
        else:
            body = ls.render(
                source_snaps[0],
                format=fmt,
                theme=options.get("theme", "light"),
                branding=not options.get("no_branding", False),
            )
    except Exception as exc:
        _share_public_failure(handler, exc, phase="render kernel")
        return
    content_type = {
        "md":   "text/markdown",
        "html": "text/html",
        "svg":  "image/svg+xml",
    }[fmt]

    # data_digest hashes the inputs that identify the underlying DATA
    # (panel + template + panel_data), NOT rendering toggles like theme
    # / branding / reveal_projects / format. Used by the composer to
    # detect "section data has drifted since add-time" (spec §5.2 /
    # §7.1) — flipping anon-on-export must not register as drift, since
    # the underlying data is identical.
    digest_input = _share_digest_input(
        panel=panel,
        template_id=template_id,
        source=source,
        source_explicit=source_explicit,
        states=source_states,
        snapshots=source_snaps,
        panel_data=panel_data,
    )
    try:
        data_digest = ls._data_digest(digest_input)
    except Exception:
        # Defensive: digest is non-blocking for the response — fall
        # back to an empty string and let the composer treat it as
        # "always drifted" rather than failing the whole render.
        data_digest = ""

    handler._respond_json(200, {
        "body": body,
        "content_type": content_type,
        "snapshot": {
            "kernel_version": ls.KERNEL_VERSION,
            "panel": panel,
            "template_id": template_id,
            "options": options,
            "generated_at": _share_now_utc_iso(),
            "data_digest": data_digest,
            **({"source": source} if source_explicit else {}),
        },
    })

# ---- /api/share/compose — stitch many basket sections (spec §5.3) ----

def _handle_share_compose_post_impl(handler) -> None:
    """Stitch multiple panel sections into one composed document.

    Recipe-only. The server re-renders every section from its
    ``(panel, template_id, options)`` recipe — never accepting a client-
    supplied ``body``. Per-section drift detection compares the fresh
    ``data_digest`` against the client's ``data_digest_at_add``;
    mismatches surface as ``section_results[i].drift_detected = true``
    for the composer's "Outdated" badge.

    Spec §5.3, §10.3. CSRF-gated.
    """
    if not handler._check_origin_csrf():
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length > _SHARE_POST_MAX_BYTES:
        # #279 S1 F3: bound the body before reading it (memory/slow-loris).
        # length == 0 stays allowed below (empty body -> {}); cap only the top.
        handler._respond_json(400, {"error": "body too large (max 64 KiB)"})
        return
    try:
        raw = handler.rfile.read(length) if length > 0 else b""
        req = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        handler._respond_json(400, {"error": "malformed json"})
        return
    if not isinstance(req, dict):
        handler._respond_json(400, {"error": "expected JSON object"})
        return

    title = req.get("title")
    theme = req.get("theme", "light")
    fmt = req.get("format", "html")
    no_branding = bool(req.get("no_branding", False))
    reveal_projects = bool(req.get("reveal_projects", False))
    sections_in = req.get("sections")
    if not isinstance(title, str) or not title:
        handler._respond_json(400, {"error": "missing title", "field": "title"})
        return
    if theme not in ("light", "dark"):
        handler._respond_json(400, {"error": f"unknown theme: {theme!r}",
                                  "field": "theme"})
        return
    if fmt not in ("md", "html", "svg"):
        handler._respond_json(400, {"error": f"unknown format: {fmt!r}",
                                  "field": "format"})
        return
    if not isinstance(sections_in, list) or not sections_in:
        handler._respond_json(400, {
            "error": "sections must be a non-empty array",
            "field": "sections",
        })
        return

    tpl_mod = handler._share_load_templates_module()
    ls = _share_load_lib()
    snap_ref = type(handler).snapshot_ref
    data_snap = snap_ref.get() if snap_ref is not None else None
    # Resolve display_tz from config once (client `ShareOptions`
    # does not carry it); applied to every section's options below
    # so daily panel rebuilds and per-day cross-tab cells bucket in
    # the user's display tz, not UTC.
    composite_display_tz = sys.modules["cctally"].get_display_tz_pref(sys.modules["cctally"].load_config())

    composed_sections: list = []
    section_results: list[dict] = []

    for idx, sec in enumerate(sections_in):
        if not isinstance(sec, dict):
            handler._respond_json(400, {
                "error": f"sections[{idx}] must be an object",
                "field": f"sections[{idx}]",
            })
            return
        # Explicit: client-supplied `body` and `content_type` are
        # silently IGNORED. This is the privacy chokepoint — the
        # regression test in tests/test_api_share.py guards it.
        snap_recipe = sec.get("snapshot") or {}
        panel = snap_recipe.get("panel")
        template_id = snap_recipe.get("template_id")
        sec_opts = snap_recipe.get("options") or {}
        digest_at_add = snap_recipe.get("data_digest_at_add") or ""
        try:
            source, source_explicit = _share_source_selection(
                {"source": snap_recipe["source"]}
                if "source" in snap_recipe else {}
            )
        except ValueError:
            handler._respond_json(400, {
                "code": "source_capability_unavailable",
                "error": "source capability unavailable",
            })
            return
        if (not isinstance(panel, str)
                or panel not in tpl_mod.SHARE_CAPABLE_PANELS):
            handler._respond_json(400, {
                "error": (
                    f"sections[{idx}].snapshot.panel invalid: {panel!r}"
                ),
                "field": f"sections[{idx}].snapshot.panel",
            })
            return
        try:
            template = tpl_mod.get_template(template_id)
        except KeyError:
            handler._respond_json(400, {
                "error": (
                    f"sections[{idx}].snapshot.template_id "
                    f"unknown: {template_id!r}"
                ),
                "field": f"sections[{idx}].snapshot.template_id",
            })
            return
        if template.panel != panel:
            handler._respond_json(400, {
                "error": (f"sections[{idx}].snapshot.template_id "
                          f"{template_id!r} belongs to panel "
                          f"{template.panel!r}, not {panel!r}"),
                "field": f"sections[{idx}].snapshot.template_id",
            })
            return

        # Force the composite reveal_projects across every section
        # (spec §8.5: per-section anon at add-time is ignored at compose).
        composite_opts = {**sec_opts, "reveal_projects": reveal_projects,
                          "theme": theme, "format": fmt,
                          "no_branding": no_branding}
        composite_opts.setdefault("display_tz", composite_display_tz)
        try:
            source_snaps, source_states, panel_data = _share_build_source_snapshots(
                ls=ls,
                template=template,
                template_id=template_id,
                panel=panel,
                options=composite_opts,
                source=source,
                source_explicit=source_explicit,
                data_snap=data_snap,
            )
        except _SharePeriodError as exc:
            handler._respond_json(400, {
                "error": f"sections[{idx}]: {exc.payload['error']}",
                "field": f"sections[{idx}].snapshot.{exc.payload['field']}",
            })
            return
        except ValueError as exc:
            _share_public_failure(
                handler, exc, phase=f"compose section {idx} provider", capability=True,
            )
            return
        except Exception as exc:
            _share_public_failure(
                handler, exc, phase=f"compose section {idx} provider",
            )
            return
        # Same content toggles as the single-section render path.
        # Per-section `show_chart`/`show_table` from the basket
        # recipe are applied here; the composite anon flag is
        # already merged into composite_opts upstream.
        source_snaps = tuple(
            _share_apply_content_toggles(item, composite_opts)
            for item in source_snaps
        )
        if not reveal_projects:
            source_snaps = tuple(
                ls._scrub(item, reveal_projects=False) for item in source_snaps
            )

        # Defensive: digest is non-blocking metadata — fall back to
        # "" on failure rather than 500-ing the whole compose
        # (mirrors the render handler at bin/cctally:33402-33408).
        try:
            digest_now = ls._data_digest(_share_digest_input(
                panel=panel,
                template_id=template_id,
                source=source,
                source_explicit=source_explicit,
                states=source_states,
                snapshots=source_snaps,
                panel_data=panel_data,
            ))
        except Exception:
            digest_now = ""
        composed_sections.extend(
            ls.ComposedSection(
                snap=item,
                drift_detected=(digest_now != digest_at_add),
            )
            for item in source_snaps
        )
        section_results.append({
            "snapshot_id": f"{idx:02d}",
            "source": source,
            "drift_detected": digest_now != digest_at_add,
            "data_digest_at_add": digest_at_add,
            "data_digest_now": digest_now,
        })

    compose_opts = ls.ComposeOptions(
        title=title, theme=theme, format=fmt,
        no_branding=no_branding, reveal_projects=reveal_projects,
    )
    try:
        body = ls.compose(tuple(composed_sections), opts=compose_opts)
    except Exception as exc:
        _share_public_failure(handler, exc, phase="compose kernel")
        return

    content_type = {
        "md":   "text/markdown",
        "html": "text/html",
        "svg":  "image/svg+xml",
    }[fmt]
    handler._respond_json(200, {
        "body": body,
        "content_type": content_type,
        "snapshot": {
            "kernel_version": ls.KERNEL_VERSION,
            "composed_at": _share_now_utc_iso(),
            "section_results": section_results,
        },
    })

# ---- /api/share/presets — saved-recipe CRUD (spec §5.1, §11.3) ----
#
# GET    /api/share/presets                       → list, grouped by panel
# POST   /api/share/presets                       → upsert (panel, name)
# DELETE /api/share/presets/{panel}/{name}        → remove one preset
#
# Persistence: `config.json` under `share.presets[<panel>][<name>]` so
# the CLI can read them later (CLI consumer is designed for, not
# shipped — out of scope per spec §15). GET is unauthenticated like
# `/api/share/templates`; POST + DELETE go through `_check_origin_csrf`
# (same gate as `/api/sync`, `/api/settings`, `/api/alerts/test`).
# Write discipline: `config_writer_lock` + `_load_config_unlocked` +
# `save_config` (atomic `os.replace`). Never call `load_config` from
# inside the writer lock — `fcntl.flock` is per-fd and would
# self-deadlock; see `_cmd_config_set` for the established pattern.

def _handle_share_presets_get_impl(handler) -> None:
    """List saved share presets, grouped by panel (spec §5.1, §11.3).

    Read-only — no CSRF gate. `config.json` may not contain the
    `share.presets` key on first run; returns `{"presets": {}}` then.
    """
    cfg = sys.modules["cctally"].load_config()
    presets = (cfg.get("share") or {}).get("presets") or {}
    # Old records predate S4. Resolve them as Claude on read without mutating
    # config (a GET must remain read-only).
    resolved = {
        panel: {
            name: ({**record, "source": record.get("source", "claude")}
                   if isinstance(record, dict) else record)
            for name, record in bucket.items()
        }
        for panel, bucket in presets.items() if isinstance(bucket, dict)
    }
    handler._respond_json(200, {"presets": resolved})

def _handle_share_presets_post_impl(handler) -> None:
    """Create or overwrite a preset (idempotent on `(panel, name)`).

    Body: ``{panel, name, template_id, options}``. CSRF-gated.

    Persistence is a read-modify-write under ``config_writer_lock`` +
    ``_load_config_unlocked``. The plain ``load_config`` would
    self-deadlock on the same fcntl.flock fd; see the CLAUDE.md
    config-write invariant and `_cmd_config_set` for the canonical
    pattern.
    """
    if not handler._check_origin_csrf():
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length > _SHARE_POST_MAX_BYTES:
        # #279 S1 F3: bound the body before reading it (memory/slow-loris).
        # length == 0 stays allowed below (empty body -> {}); cap only the top.
        handler._respond_json(400, {"error": "body too large (max 64 KiB)"})
        return
    try:
        raw = handler.rfile.read(length) if length > 0 else b""
        req = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        handler._respond_json(400, {"error": "malformed json"})
        return
    if not isinstance(req, dict):
        handler._respond_json(400, {"error": "expected JSON object"})
        return
    panel = req.get("panel")
    name = req.get("name")
    template_id = req.get("template_id")
    options = req.get("options")
    try:
        source, _ = _share_source_selection(req)
    except ValueError:
        handler._respond_json(400, {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        })
        return
    if not isinstance(panel, str) or not panel:
        handler._respond_json(400, {
            "error": "missing or non-string panel",
            "field": "panel",
        })
        return
    tpl_mod = handler._share_load_templates_module()
    if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
        handler._respond_json(400, {
            "error": f"unknown share panel: {panel!r}",
            "field": "panel",
        })
        return
    if not isinstance(name, str) or not name or "/" in name or len(name) > 64:
        handler._respond_json(400, {
            "error": "name must be 1-64 chars and contain no '/'",
            "field": "name",
        })
        return
    if not isinstance(template_id, str) or not template_id:
        handler._respond_json(400, {
            "error": "missing or non-string template_id",
            "field": "template_id",
        })
        return
    try:
        template = tpl_mod.get_template(template_id)
    except KeyError:
        handler._respond_json(400, {
            "error": f"unknown template_id: {template_id!r}",
            "field": "template_id",
        })
        return
    if template.panel != panel:
        handler._respond_json(400, {
            "error": (
                f"template_id {template_id!r} belongs to panel "
                f"{template.panel!r}, not {panel!r}"
            ),
            "field": "template_id",
        })
        return
    if not isinstance(options, dict):
        handler._respond_json(400, {
            "error": "options must be an object",
            "field": "options",
        })
        return

    saved_at = _share_now_utc_iso()
    record = {
        "template_id": template_id, "options": options,
        "source": source, "saved_at": saved_at,
    }

    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.setdefault("share", {})
        presets = share.setdefault("presets", {})
        panel_bucket = presets.setdefault(panel, {})
        panel_bucket[name] = record
        save_config(cfg)
    handler._respond_json(200, {"panel": panel, "name": name, **record})

def _handle_share_presets_delete_impl(handler) -> None:
    """Remove a preset by `(panel, name)`.

    Path: ``/api/share/presets/{panel}/{name}``. Missing → 404 so
    DELETE stays meaningful for idempotency-aware clients. CSRF-gated.
    """
    if not handler._check_origin_csrf():
        return
    import urllib.parse as _urlparse
    # Strip the query string defensively; the spec only uses path
    # segments but a stray "?" shouldn't poison the name token.
    path_only = handler.path.split("?", 1)[0]
    parts = path_only.split("/")
    # Expected: ["", "api", "share", "presets", "<panel>", "<name>"]
    if (
        len(parts) != 6
        or parts[1] != "api"
        or parts[2] != "share"
        or parts[3] != "presets"
        or not parts[4]
        or not parts[5]
    ):
        handler._respond_json(400, {"error": "malformed delete path"})
        return
    panel = _urlparse.unquote(parts[4])
    name = _urlparse.unquote(parts[5])
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.get("share") or {}
        presets = share.get("presets") or {}
        panel_bucket = presets.get(panel) or {}
        if name not in panel_bucket:
            handler._respond_json(404, {"error": "no such preset"})
            return
        del panel_bucket[name]
        # Tidy empty buckets so GET stays clean.
        if not panel_bucket:
            presets.pop(panel, None)
        save_config(cfg)
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler.end_headers()

# ---- /api/share/history — export-recipe ring buffer (spec §5.1, §11.4) ----
#
# GET    /api/share/history  → list (newest last) of last 20 export recipes
# POST   /api/share/history  → append; server-side FIFO trim to 20
# DELETE /api/share/history  → clear the entire buffer
#
# Persisted under `share.history` in `config.json`. Write discipline
# matches the presets handlers above: `config_writer_lock` +
# `_load_config_unlocked` + `save_config`. GET is unauthenticated
# like `/api/share/templates`; POST + DELETE go through
# `_check_origin_csrf`. The frontend posts fire-and-forget after
# every successful export — history failures are non-fatal.

def _handle_share_history_get_impl(handler) -> None:
    """Return the recent-shares ring buffer (newest last, spec §11.4)."""
    cfg = sys.modules["cctally"].load_config()
    history = (cfg.get("share") or {}).get("history") or []
    handler._respond_json(200, {"history": [
        ({**record, "source": record.get("source", "claude")}
         if isinstance(record, dict) else record)
        for record in history
    ]})

def _handle_share_history_post_impl(handler) -> None:
    """Append a recipe to the ring buffer; FIFO trim to 20.

    Body: ``{panel, template_id, options, format, destination}``. The
    server stamps ``recipe_id`` (random hex) and ``exported_at``
    (UTC ISO-8601) so the client doesn't need a clock or a UUID lib.
    CSRF-gated. Read-modify-write under ``config_writer_lock`` —
    same pattern as the presets POST.
    """
    if not handler._check_origin_csrf():
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length > _SHARE_POST_MAX_BYTES:
        # #279 S1 F3: bound the body before reading it (memory/slow-loris).
        # length == 0 stays allowed below (empty body -> {}); cap only the top.
        handler._respond_json(400, {"error": "body too large (max 64 KiB)"})
        return
    try:
        raw = handler.rfile.read(length) if length > 0 else b""
        req = json.loads(raw) if raw else {}
    except (ValueError, json.JSONDecodeError):
        handler._respond_json(400, {"error": "malformed json"})
        return
    if not isinstance(req, dict):
        handler._respond_json(400, {"error": "expected JSON object"})
        return
    panel = req.get("panel")
    template_id = req.get("template_id")
    options = req.get("options") or {}
    fmt = req.get("format")
    destination = req.get("destination")
    try:
        source, _ = _share_source_selection(req)
    except ValueError:
        handler._respond_json(400, {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        })
        return
    if not isinstance(panel, str) or not panel:
        handler._respond_json(400, {
            "error": "missing or non-string panel",
            "field": "panel",
        })
        return
    tpl_mod = handler._share_load_templates_module()
    if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
        handler._respond_json(400, {
            "error": f"unknown share panel: {panel!r}",
            "field": "panel",
        })
        return
    if not isinstance(template_id, str) or not template_id:
        handler._respond_json(400, {
            "error": "missing or non-string template_id",
            "field": "template_id",
        })
        return
    try:
        template = tpl_mod.get_template(template_id)
    except KeyError:
        handler._respond_json(400, {
            "error": f"unknown template_id: {template_id!r}",
            "field": "template_id",
        })
        return
    if template.panel != panel:
        handler._respond_json(400, {
            "error": (
                f"template_id {template_id!r} belongs to panel "
                f"{template.panel!r}, not {panel!r}"
            ),
            "field": "template_id",
        })
        return
    if not isinstance(options, dict):
        handler._respond_json(400, {
            "error": "options must be an object",
            "field": "options",
        })
        return
    # `format` and `destination` are advisory strings — accept any
    # non-empty string; the frontend uses them only as display hints
    # in the dropdown row. None/missing is allowed (mirrors how the
    # CLI doesn't always know which destination produced the export).
    if fmt is not None and not isinstance(fmt, str):
        handler._respond_json(400, {
            "error": "format must be a string if provided",
            "field": "format",
        })
        return
    if destination is not None and not isinstance(destination, str):
        handler._respond_json(400, {
            "error": "destination must be a string if provided",
            "field": "destination",
        })
        return

    record = {
        "recipe_id": _share_history_recipe_id(),
        "panel": panel,
        "template_id": template_id,
        "options": options,
        "source": source,
        "format": fmt,
        "destination": destination,
        "exported_at": _share_now_utc_iso(),
    }
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.setdefault("share", {})
        history = share.setdefault("history", [])
        history.append(record)
        # Ring buffer: trim from the front so the newest is always
        # last. `del history[:n]` keeps the same list instance, so
        # callers holding a reference (none in this scope, but a
        # safe invariant) see the same object mutated in place.
        _ring_cap = sys.modules["cctally"]._SHARE_HISTORY_RING_CAP
        if len(history) > _ring_cap:
            del history[: len(history) - _ring_cap]
        save_config(cfg)
    handler._respond_json(200, record)

def _handle_share_history_delete_impl(handler) -> None:
    """Empty the share-history ring buffer (spec §11.4)."""
    if not handler._check_origin_csrf():
        return
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.get("share")
        if isinstance(share, dict) and "history" in share:
            share["history"] = []
            save_config(cfg)
    handler.send_response(204)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
