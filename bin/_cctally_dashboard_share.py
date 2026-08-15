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

import dataclasses as _dataclasses
import datetime as dt
import json
import pathlib
import re
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

from _cctally_core import open_db, parse_iso_datetime
from _cctally_config import save_config, _load_config_unlocked
from _lib_fmt import stable_sum
from _lib_pricing import _calculate_entry_cost, claude_usage_dict
from _lib_five_hour import _canonical_5h_window_key
from _lib_display_tz import _resolve_tz, resolve_display_tz_name


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
# one panel; compose is up to the basket cap of 20 sections, each with a
# small options recipe).
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
    the builder, before `render()` / `compose()` prepare it.
    ShareSnapshot is frozen;
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
    happens later, in the preparation pass `render()` and `compose()` run
    over the raw snapshot; builders always emit real names per the
    kernel's privacy chokepoint contract. (It is NOT `_scrub()`: that
    function is retained for backward compatibility and no production
    path calls it.)
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
        usage = claude_usage_dict(   # #195 chokepoint
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
            cache_read_tokens=entry.cache_read_tokens,
            cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
            speed=getattr(entry, "speed", None),
        )
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
        usage = claude_usage_dict(   # #195 chokepoint
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
            cache_read_tokens=entry.cache_read_tokens,
            cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
            speed=getattr(entry, "speed", None),
        )
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
        usage = claude_usage_dict(   # #195 chokepoint
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_creation_tokens=entry.cache_creation_tokens,
            cache_read_tokens=entry.cache_read_tokens,
            cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
            speed=getattr(entry, "speed", None),
        )
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
    (`_lib_share.render()` / `compose()`), NOT here. This panel_data
    carries REAL display_keys + bucket_paths; the preparation pass those
    entry points run over the raw snapshot rewrites them when
    ``reveal_projects=false``. (It is NOT `_scrub()`: that function is
    retained for backward compatibility and no production path calls it.)
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


def _share_json_bool(payload: dict, key: str, *, default: bool = False) -> bool:
    """Read a strict JSON boolean instead of applying Python truthiness."""
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(key)
    return value


_ACCOUNT_KEY_RE = re.compile(r"[0-9a-f]{32}|unattributed|\*")


def _share_account_selection(req: dict) -> "str | None":
    """Resolve the optional captured account qualifier (#341 Task 4, spec §4).

    The composer captures ``(source, account)`` at OPEN_SHARE (client
    ``shareModal.account``) and posts the resolved account_key. A legacy request
    (no ``account`` field, or an explicit null) stays account-agnostic — today's
    byte-identical behavior. A malformed value is rejected so a bad qualifier
    can never reach the reader. The account carries into the data_digest (a focus
    change busts the composer drift check) and the response/history metadata.
    """
    if "account" not in req:
        return None
    account = req.get("account")
    if account is None:
        return None
    if not isinstance(account, str) or not _ACCOUNT_KEY_RE.fullmatch(account):
        raise ValueError("source capability unavailable")
    return account


def _share_account_display_label(source: str, account: "str | None",
                                 *, reveal: bool) -> "str | None":
    """The reveal-aware account label to stamp on a share artifact (#341 Task 4).

    Resolves the account's registry label + deterministic index for its provider
    (``all`` has no account selector) and routes both through the fail-closed
    kernel chokepoint ``anonymize_account_label`` — so anon-mode (the default)
    emits ``Account A/B/C`` and only an explicit reveal shows the real label.
    Returns ``None`` when no account is captured or it cannot be resolved (never
    guesses; never leaks). Emails are never consulted — only the label.
    """
    if account is None or source not in ("claude", "codex"):
        return None
    ls = _share_load_lib()
    c = sys.modules["cctally"]
    try:
        import _cctally_account
        conn = c._load_sibling("_cctally_core").open_db()
        try:
            reg = _cctally_account.load_accounts(conn, source)
            label = _cctally_account.display_account_label(conn, account)
        finally:
            conn.close()
    except Exception:
        return None
    index = next(
        (i for i, r in enumerate(reg) if r["account_key"] == account), -1,
    )
    return ls.anonymize_account_label(label, index, reveal=reveal)


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


def _share_resolved_display_tz(raw: object = None) -> str:
    """The concrete IANA zone a share artifact states its dates in.

    One resolution for the whole dashboard share surface (#503 S2 D7):
    the render handler injects it into `options`, the composite handler
    injects it into every composite section's options, and the Codex
    snapshot builders read it back instead of hardcoding `"UTC"` — so a
    composed document cannot carry one section labelled `(UTC)` beside
    another labelled `(Etc/UTC)`.

    `raw` is a configuration token (`local` / `utc` / IANA) or None; None
    means "read the server's config".
    """
    c = sys.modules["cctally"]
    token = raw if isinstance(raw, str) and raw else c.get_display_tz_pref(c.load_config())
    return resolve_display_tz_name(token)


def _share_parse_bucket_start(panel: str, label: object,
                              zone=None) -> "dt.datetime | None":
    """Lift a bucket label or a row timestamp into a period boundary.

    A NAIVE parse is a calendar label (`2026-05-04`, `2026-05`), so it is
    grounded at midnight in the zone the artifact is labelled with —
    grounding it in UTC and then converting it into that zone reports the
    previous day everywhere west of UTC (#503 S2 D7). An AWARE parse
    (`first_seen` / `last_activity`, which the envelope serializes with an
    explicit offset) is a real instant and keeps its own offset.
    """
    try:
        if panel == "monthly":
            parsed = dt.datetime.strptime(str(label), "%Y-%m")
        else:
            parsed = dt.datetime.fromisoformat(str(label))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone or dt.timezone.utc)
    return parsed


def _share_codex_period_bounds(*, state, panel: str, options: dict, rows,
                               display_tz: "str | None" = None) -> tuple:
    """The period bounds for a Codex source snapshot.

    `display_tz` is the zone the SNAPSHOT will be labelled with, and a
    bucket label is grounded at ITS midnight (#503 S2 D7). The period
    families resolve that from the panel's own value before calling
    here, and that can differ from the options-derived default —
    grounding in one zone while labelling with another states a date
    the artifact's own rows do not use.
    """
    now_override, start_override, err = _share_resolve_period(panel, options)
    if err is not None:
        raise ValueError("source capability unavailable")
    end = now_override or state.last_success_at or dt.datetime.now(dt.timezone.utc)
    if end.tzinfo is None or end.utcoffset() is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    if start_override is not None:
        return start_override.astimezone(dt.timezone.utc), end
    zone = _resolve_tz(
        display_tz or _share_resolved_display_tz(options.get("display_tz")),
        fallback=dt.timezone.utc)
    starts = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw = row.get("first_seen") or row.get("last_activity") or row.get("label")
        parsed = _share_parse_bucket_start(
            "monthly" if panel == "monthly" else "daily", raw, zone)
        if parsed is not None:
            starts.append(parsed)
    if panel == "current-week":
        starts = [
            parsed for row in rows if isinstance(row, Mapping)
            if (parsed := _share_parse_bucket_start(
                "weekly", row.get("label"), zone)) is not None
        ]
        return (max(starts) if starts else end - dt.timedelta(days=7)), end
    return (min(starts) if starts else end), end


# What the block column states when the row carries no parseable start.
_CODEX_BLOCK_LABEL_UNKNOWN = "(unknown)"


def _codex_block_label(ls, row) -> str:
    """The block-start text a Codex quota artifact states.

    `bin/_cctally_dashboard_sources.py` renders each block's `label` as
    `%H:%M %b %d` for the dashboard chip, where the compact form is the
    point and the year is context the surrounding page supplies. An
    artifact leaves cctally, so it names its year: every Claude blocks
    artifact states a full ISO instant, and this column stated the chip
    string instead (#503 S2 M5, the sixth site of the D4 class).

    The absent-`start_at` fallback does NOT reach for `label`. That field
    is only ever the yearless chip, so falling back to it re-introduced
    the exact string the fix removed — and neither D4 tripwire could see
    it, because one scans the six share-builder modules (which do not
    include `_cctally_dashboard_sources.py`, where the chip is formatted)
    and the other scans committed goldens (no golden covers this panel).
    A row with no parseable start falls back to its own `resets_at`,
    stated as a full ISO instant and labelled as a reset so the column is
    not read as a start. `(unknown)` discarded information the row still
    carried and left the block unidentifiable in the artifact; it remains
    only for a row that carries neither field (#503 S2 second review N4,
    third review).
    """
    for field, prefix in (("start_at", ""), ("resets_at", "resets ")):
        rendered = _codex_block_instant(ls, row, field)
        if rendered is not None:
            return prefix + rendered
    return _CODEX_BLOCK_LABEL_UNKNOWN


def _codex_block_instant(ls, row, field: str) -> "str | None":
    """One of the row's instants as `…Z`, or None when it has none."""
    raw = row.get(field)
    if raw:
        try:
            parsed = parse_iso_datetime(str(raw), f"codex.block.{field}")
        except ValueError:
            pass
        else:
            # NORMALIZED to UTC before formatting. `parse_iso_datetime`
            # ends with a bare `astimezone()`, so it hands back a
            # host-local datetime, and `_format_generated_at_iso` keeps
            # whatever offset it is given — the cell would otherwise read
            # `+03:00` on one machine and `Z` on another, where every
            # other blocks artifact states `…Z`.
            return ls._format_generated_at_iso(
                parsed.astimezone(dt.timezone.utc))
    return None


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
        # RESOLVED, never passed through (#503 S2 review F9). The panel
        # value is a display label, and `_lib_view_models._display_tz_label`
        # returns the literal `local` for a `None` zone — so a caller that
        # omits `options["display_tz"]` would put `(local)` back into an
        # artifact, which D7 exists to prevent.
        display_tz = _share_resolved_display_tz(
            weekly.get("display_tz") or options.get("display_tz"))
    elif panel in ("daily", "monthly", "weekly", "trend"):
        periods = data.get("periods")
        period_key = "weekly" if panel == "trend" else panel
        panel_data = periods.get(period_key) if isinstance(periods, Mapping) else {}
        if not isinstance(panel_data, Mapping):
            raise ValueError("source capability unavailable")
        source_rows = tuple(panel_data.get("rows", ()))
        command = f"codex-{period_key}"
        # Resolved, never passed through — see the `current-week` branch.
        display_tz = _share_resolved_display_tz(
            panel_data.get("display_tz") or options.get("display_tz"))
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
            # #350 spec §3.6 — "Projections blank. Actuals stay." BOTH the build
            # and the idle clock deliberately preserve `projected_percent`
            # alongside a non-ok `status`, and this site formatted it with no
            # status check at all. That was masked while a stale Codex source
            # collapsed to `unavailable` here; §3.4 keeps the source coherent, so
            # a shared forecast would otherwise publish a stale projection.
            projected = (
                forecast.get("projected_percent")
                if forecast.get("status") == "ok" else None
            )
            rows.append(ls.Row(cells={
                "limit": ls.TextCell(str(row.get("label") or "Codex quota")),
                "current": ls.TextCell("—" if current is None else f"{float(current):.1f}%"),
                "projected": ls.TextCell("—" if projected is None else f"{float(projected):.1f}%"),
            }))
        return ls.ShareSnapshot(
            cmd="codex-quota", title="Codex Quota Forecast", subtitle=None,
            period=ls.PeriodSpec(
                start=start, end=end, label=None,
                display_tz=_share_resolved_display_tz(options.get("display_tz")),
            ),
            columns=(
                ls.ColumnSpec(key="limit", label="Limit"),
                ls.ColumnSpec(key="current", label="Current", align="right"),
                ls.ColumnSpec(key="projected", label="Projected", align="right"),
            ),
            rows=tuple(rows), chart=None, totals=(),
            notes=_share_budget_notes(data), generated_at=end,
            version=sys.modules["cctally"]._share_resolve_version(),
            template_id=template_id, source="codex", source_label="Codex",
            availability=availability, availability_reason=reason,
        )
    elif panel == "sessions":
        panel_data = data.get(panel) if isinstance(data.get(panel), Mapping) else {}
        source_rows = tuple(panel_data.get("rows", ())) if isinstance(panel_data, Mapping) else ()
        command = "codex-session"
        display_tz = _share_resolved_display_tz(options.get("display_tz"))
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
            period=ls.PeriodSpec(
                start=start, end=end, label=None,
                display_tz=_share_resolved_display_tz(options.get("display_tz")),
            ),
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
    elif panel == "blocks":
        # NAMED rather than left as the chain's `else`. The three
        # `PeriodSpec` sites in this function are addressed BY ORDINAL
        # from the test suite, and an unnamed branch cannot be attributed
        # to a panel from the source, so the driver's ordinal-to-panel
        # mapping was unassertable (#503 S2 third review). Naming it also
        # turns an unrecognised panel into an error rather than silently
        # rendering it as a blocks artifact; `required_domain` above
        # already rejects every panel this chain does not list.
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
                "label": ls.TextCell(_codex_block_label(ls, row)),
                "usage": ls.TextCell(f"{float(percent or 0.0):.1f}%"),
                "resets": ls.TextCell(str(row.get("resets_at", "—"))),
            }
        rows = tuple(ls.Row(cells=cells(row)) for row in source_rows if isinstance(row, Mapping))
        return ls.ShareSnapshot(
            cmd="codex-quota", title="Codex Quota Windows", subtitle=None,
            period=ls.PeriodSpec(
                start=start, end=end, label=None,
                display_tz=_share_resolved_display_tz(options.get("display_tz")),
            ),
            columns=columns, rows=rows, chart=None,
            totals=(), notes=(), generated_at=end,
            version=sys.modules["cctally"]._share_resolve_version(),
            template_id=template_id, source="codex", source_label="Codex",
            availability=availability, availability_reason=reason,
        )
    else:
        raise ValueError(f"unsupported codex source panel: {panel}")

    start, end = _share_codex_period_bounds(
        state=state, panel=panel, options=options, rows=source_rows,
        display_tz=display_tz,
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
    # The canonical CLI adapter derives availability from its row count, but
    # this source-aware path has an authoritative provider state. Project the
    # normalized state after adapting the rows so all nine panels carry the
    # same source chrome and built-snapshot digest signal (#533).
    return replace(
        codex_module._build_codex_share_snapshot(command, view, normalized_rows),
        template_id=template_id,
        availability=availability,
        availability_reason=reason,
    )


def _share_current_week_evidence_is_stale(state) -> bool:
    """Whether ONE provider's own current-cycle evidence is stale (#556 §4.7).

    This used to read the shared ``domain_freshness.hero`` axis, which #556 S1
    repointed to accounting resolvability. The aggregate ``quota`` axis is not
    the substitute: a stale five-hour row stales it independently of the weekly
    cycle this note describes. So each provider is read on its own field —
    Claude's percent-observation label under ``hero.current_week.freshness``,
    Codex's additive ``hero.cycle_freshness``, which is omitted while fresh.
    """
    data = getattr(state, "data", None)
    hero = data.get("hero") if isinstance(data, Mapping) else None
    if not isinstance(hero, Mapping):
        return False
    if getattr(state, "source", None) == "claude":
        current_week = hero.get("current_week")
        freshness = (
            current_week.get("freshness")
            if isinstance(current_week, Mapping) else None
        )
        return isinstance(freshness, Mapping) and freshness.get("label") == "stale"
    return hero.get("cycle_freshness") == "stale"


def _share_apply_current_week_freshness(snapshot, state, panel: str):
    """Qualify retained current-week actuals with provider-local evidence age."""
    if panel != "current-week" or not _share_current_week_evidence_is_stale(state):
        return snapshot
    provider = "Claude" if state.source == "claude" else "Codex"
    note = (
        f"{provider} current-week spend is based on stale provider-cycle evidence."
    )
    return replace(snapshot, notes=tuple(snapshot.notes) + (note,))


# The meaning of `data_digest`, as a stored value (#503 S3 §4).
#
# `data_digest_at_add` lives in localStorage basket items and is replayed at
# compose time, so redefining what the digest hashes would mark every stored
# section outdated exactly once. Version 2 is that redefinition: two digests
# are compared ONLY when the stored version equals this one. A missing or
# older version is NOT COMPARABLE, which is not drifted — no badge, no third
# badge state. `KERNEL_VERSION` is untouched: it versions the RENDERER
# contract (`_lib_share.py`), which has not changed.
_SHARE_DATA_DIGEST_VERSION = 2


def _share_digest_value(ls, value):
    """Structurally project one value for `_data_digest` (#503 S3 §4).

    `_data_digest` serializes with `default=str`, and its own contract warns
    that arbitrary objects then hash as a per-process-unstable `repr`. The
    projected snapshot fields are nested frozen dataclasses — cells, `Row`,
    `ColumnSpec`, the chart union, chart points — so handing them over
    directly is exactly that hazard. This converts them instead:

    - a `PeriodSpec` becomes its CIVIL dates plus zone and label, never its
      raw `start`/`end` instants (`period_civil_dates` already honours S2's
      `civil_bucket` discriminator, so a `daily` bucket is not shifted a day
      west of UTC);
    - any other dataclass becomes a mapping of its declared fields, carrying
      `__type__` so the cell and chart unions stay discriminated (a
      `TextCell("5")` must not hash as a `DateCell("5")`);
    - mappings recurse structurally and tuples/lists preserve order;
    - a `datetime` outside a `PeriodSpec` becomes a normalized UTC ISO string;
    - anything else RAISES, so the callers' empty-digest fallback stays
      defensive rather than silently hashing a `repr`.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, ls.PeriodSpec):
        start_civil, end_civil = ls.period_civil_dates(value)
        return {
            "__type__": "PeriodSpec",
            "civil": [start_civil, end_civil],
            "display_tz": value.display_tz,
            "label": value.label,
        }
    if _dataclasses.is_dataclass(value) and not isinstance(value, type):
        projected = {"__type__": type(value).__name__}
        for field in _dataclasses.fields(value):
            projected[field.name] = _share_digest_value(
                ls, getattr(value, field.name))
        return projected
    if isinstance(value, Mapping):
        return {str(key): _share_digest_value(ls, item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_share_digest_value(ls, item) for item in value]
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    raise TypeError(
        f"share digest cannot serialize {type(value).__name__}")


def _share_snapshot_digest_projection(ls, snapshot):
    """The canonical projection of ONE built, pre-toggle `ShareSnapshot`.

    Two snapshot fields are excluded BY NAME. `generated_at` is a wall clock,
    and hashing a wall clock is what made every section drift with elapsed
    time. `version` is a renderer concern `KERNEL_VERSION` already covers.
    Nothing from the ambient process state enters — no `data_version`, no raw
    provider domain, no clock.
    """
    return {
        "title": snapshot.title,
        "subtitle": snapshot.subtitle,
        "period": _share_digest_value(ls, snapshot.period),
        "columns": _share_digest_value(ls, snapshot.columns),
        "rows": _share_digest_value(ls, snapshot.rows),
        "chart": _share_digest_value(ls, snapshot.chart),
        "totals": _share_digest_value(ls, snapshot.totals),
        "notes": _share_digest_value(ls, snapshot.notes),
        "template_id": snapshot.template_id,
        "source": snapshot.source,
        "source_label": snapshot.source_label,
        "availability": snapshot.availability,
        "availability_reason": snapshot.availability_reason,
    }


def _share_digest_input(*, panel: str, template_id: str, source: str,
                        snapshots, account: "str | None" = None, ls=None):
    """The digest payload: a projection of the built snapshots, and nothing else.

    `snapshots` MUST be the PRE-toggle tuple. `_share_apply_content_toggles`
    strips `chart` and `columns`/`rows` per `show_chart`/`show_table`, and a
    Codex `blocks` section carries its window identity ONLY in its rows — its
    title and period label are constants — so hashing the toggled tuple
    collapses two different five-hour windows on one civil day into one
    digest. Hashing pre-toggle also makes both toggles genuinely render-only,
    which is what the render handler's comment has always promised.

    One definition for every request: version 1 forked on `source_explicit`
    and kept a separate legacy payload, which left that branch carrying the
    defect this rewrite removes. `_SHARE_DATA_DIGEST_VERSION` is how a stored
    digest's meaning moves; see `docs/share-gotchas.md`.
    """
    ls = ls if ls is not None else _share_load_lib()
    # #341 Task 4: the captured account participates in the digest so switching
    # the focused account registers as data drift in the composer (spec §4).
    # It stays TOP-LEVEL rather than being read off the snapshots: an
    # account-focus change must change identity even when two scoped snapshots
    # happen to hold equal values. Absent → omitted.
    account_key = {"account": account} if account is not None else {}
    return {
        "panel": panel,
        "template_id": template_id,
        "source": source,
        "snapshots": [
            _share_snapshot_digest_projection(ls, snapshot)
            for snapshot in snapshots
        ],
        **account_key,
    }


_CODEX_ACCOUNT_SCOPED_DOMAINS = (
    "periods", "sessions", "projects", "cache_report", "budget", "quota",
    "alerts",
)


def _share_scope_codex_state(state, account: "str | None"):
    """Restrict a Codex source state to ONE account's child (#416 §5.6, F16).

    The share handler has always parsed `account` and stamped it on the digest,
    the response and the history metadata — but never passed it to the snapshot
    builder, so a focused user exported an ALL-ACCOUNT body LABELLED with the
    focused account. That is a disclosure defect, not a missing feature, and it
    is worse than an unlabelled export because the label asserts a scope the
    body does not have.

    Substitutes the per-account children the source already publishes, so the
    share reads exactly what the focused dashboard reads — one read model, not a
    second re-derivation that could drift from it.

    FAILS CLOSED. A decorated source that does not know this account raises
    rather than falling back to the merged body: falling back is precisely the
    leak. An UNDECORATED source (<=1 real account) publishes no children and
    needs none — its merged body IS that account's body.
    """
    if account is None:
        return state
    data = state.data if isinstance(state.data, Mapping) else {}
    scopes = data.get("account_scopes")
    if not isinstance(scopes, Mapping):
        return state
    scope = scopes.get(account)
    if not isinstance(scope, Mapping):
        raise ValueError("source capability unavailable")
    scoped = {
        key: scope[key] for key in _CODEX_ACCOUNT_SCOPED_DOMAINS if key in scope
    }
    return replace(state, data=MappingProxyType({**dict(data), **scoped}))


def _share_budget_notes(data) -> tuple[str, ...]:
    """The configured-budget status, as an artifact note (#556 S5 §5.12).

    The Forecast artifact carried quota projections and nothing about the
    CONFIGURED budget, so a shared Forecast said less than the panel it was
    taken from — and after S5 the panel renders both side by side. The note is
    ADDITIVE and omitted when no status is published, so an install with no
    budget produces a byte-identical artifact.

    ``data`` is already the ACCOUNT-SCOPED provider body when the request named
    an account (`_share_scope_codex_state` rewrites the scoped
    domains before this runs), so a focused share carries that account's own
    budget and never the vendor-wide one.
    """
    budget = data.get("budget") if isinstance(data, Mapping) else None
    status = budget.get("status") if isinstance(budget, Mapping) else None
    if not isinstance(status, Mapping):
        return ()
    try:
        spent = float(status["spent_usd"])
        target = float(status["budget_usd"])
        consumed = float(status["consumption_pct"])
        period = str(status["period"])
        verdict = str(status["verdict"])
    except (KeyError, TypeError, ValueError):
        return ()
    return (
        f"Budget ({period}): ${spent:,.2f} of ${target:,.2f} "
        f"({consumed:.1f}%) — {verdict}",
    )


def _share_build_source_snapshots(*, ls, template, template_id: str,
                                  panel: str, options: dict, source: str,
                                  source_explicit: bool, data_snap,
                                  account: "str | None" = None):
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
            claude_snapshot = _share_apply_current_week_freshness(
                claude_snapshot, claude_state, panel,
            )
            # #556 S5 §5.12 (Unit 2 review F7). The budget note was wired into
            # the Codex builder only, while §5.12 says "the configured-budget
            # sections" and the Claude Forecast panel renders one after S5 — so
            # a shared Claude Forecast said less than the panel it came from.
            # Gated exactly like the freshness stamp above: a source-less
            # request is the shipped legacy Claude contract and stays
            # byte-identical, because it has no source state to read at all.
            if panel == "forecast":
                notes = _share_budget_notes(
                    getattr(claude_state, "data", None) or {},
                )
                if notes:
                    claude_snapshot = replace(
                        claude_snapshot,
                        notes=tuple(claude_snapshot.notes) + notes,
                    )

    codex_snapshot = None
    codex_state = None
    if source in ("codex", "all"):
        codex_state = _share_scope_codex_state(
            _share_codex_state_for_period(
                data_snap, panel=panel, options=options,
            ),
            account,
        )
        codex_snapshot = _build_codex_source_share_snapshot(
            ls,
            state=codex_state,
            panel=panel,
            template_id=template_id,
            options=options,
        )
        codex_snapshot = _share_apply_current_week_freshness(
            codex_snapshot, codex_state, panel,
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
    # A privacy refusal is logged by CLASS ONLY (#503 S1 R10). Since the
    # refusal message widened to name the matched value, a `%r` of the
    # exception can put an absolute path, a UUID or an email address into the
    # dashboard log — and a log is a plausible thing to paste into a bug
    # report. The data is the user's own and the HTTP response below is
    # generic either way, so nothing reaches a remote client; this is about
    # what the log file accumulates. Every raise site in `_lib_share` sets
    # `classes`, and `SharePrivacyViolation` defaults it to a non-empty
    # sentinel, so this branch cannot fall through to the `%r` for a privacy
    # refusal even if a future raise site forgets the keyword. Any OTHER
    # exception is still logged in full, because its repr is the only
    # diagnostic there is.
    classes = getattr(exc, "classes", None)
    if classes:
        handler.log_error("/api/share/%s failed: %s: %s", phase,
                          type(exc).__name__, ", ".join(classes))
    else:
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
    template's builder, then renders via `_lib_share.render`, which
    prepares the RAW snapshot it is handed — anonymizing project labels
    when ``options.reveal_projects`` is False. Response:
    ``{body, content_type, snapshot}``
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
        account = _share_account_selection(req)
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
    #
    # RESOLVED here, once (#503 S2 D7). `get_display_tz_pref` returns a
    # configuration TOKEN whose default is the literal `local`, and that
    # token used to travel all the way into `PeriodSpec.display_tz`, so
    # the artifact stated a zone that names no zone. The resolution is
    # unconditional rather than gated on the key being absent, because a
    # caller-supplied token is a token too.
    options["display_tz"] = _share_resolved_display_tz(options.get("display_tz"))
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
    try:
        reveal = _share_json_bool(options, "reveal_projects")
    except TypeError:
        handler._respond_json(400, {
            "error": "reveal_projects must be a boolean",
            "field": "options.reveal_projects",
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
        source_snaps, _source_states, _panel_data = _share_build_source_snapshots(
            ls=ls,
            template=template,
            template_id=template_id,
            panel=panel,
            options=options,
            source=source,
            source_explicit=source_explicit,
            data_snap=data_snap,
            # #416 §5.6: the captured account has always reached the digest and
            # the response label; it must reach the BODY too.
            account=account,
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
    # TWO tuples, and the order is the whole point (#503 S3 §4). The digest
    # hashes what the BUILDERS produced; `render()` receives the toggled
    # versions. Reassigning `source_snaps` first — which is what this site
    # did — hands the digest a snapshot whose rows a render knob erased.
    digest_snaps = source_snaps
    source_snaps = tuple(
        _share_apply_content_toggles(item, options) for item in source_snaps
    )
    # FAIL CLOSED (#503 S1 F3). HTTP genuinely has an absent-field case, so
    # a default belongs here; it must resolve to anonymize, matching
    # /api/share/compose, which already defaulted closed. The kernel itself
    # has no default at all, so a fourth site cannot get this wrong.
    # No pre-scrub: the kernel's `render()` / `compose()` own the privacy
    # contract and require RAW snapshots (#503 S1). Pre-scrubbing here
    # renumbers aliases on the legacy path, and in the `source=all` branch it
    # merges two distinct projects that each mapped locally to `project-1`
    # into a single alias.
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
                reveal_projects=reveal,
            )
    except Exception as exc:
        _share_public_failure(handler, exc, phase="render kernel")
        return
    content_type = {
        "md":   "text/markdown",
        "html": "text/html",
        "svg":  "image/svg+xml",
    }[fmt]

    # data_digest hashes a canonical projection of the BUILT, PRE-TOGGLE
    # snapshots — what the artifact is made of — and nothing else. NOT the
    # rendering toggles (theme / branding / reveal_projects / format /
    # show_chart / show_table), and NOT the wall clock or the raw provider
    # state. Used by the composer to detect "section data has drifted since
    # add-time" (spec §5.2 / §7.1); flipping anon-on-export must not register
    # as drift, since the underlying data is identical.
    try:
        # The projection is INSIDE the guard, matching the compose site.
        # `_share_digest_value` raises on a value it cannot serialize, and
        # `do_POST` has no exception guard of its own, so building the input
        # outside this `try` turned a projection failure into a dropped
        # connection instead of the empty digest the fallback promises.
        data_digest = ls._data_digest(_share_digest_input(
            panel=panel,
            template_id=template_id,
            source=source,
            snapshots=digest_snaps,
            account=account,
            ls=ls,
        ))
    except Exception:
        # Defensive: digest is non-blocking for the response — fall
        # back to an empty string and let the composer treat it as
        # "always drifted" rather than failing the whole render.
        data_digest = ""

    # #341 Task 4: the captured account + its reveal-aware anonymized label
    # (fail-closed via the kernel chokepoint). Present only when an account was
    # captured, so a legacy/account-agnostic share's response is byte-stable.
    account_label = _share_account_display_label(source, account, reveal=reveal)
    account_meta = {}
    if account is not None:
        account_meta["account"] = account
        if account_label is not None:
            account_meta["account_label"] = account_label

    # #503 S1 B1 — does this export contain project names at all?
    #
    # The share modal's status line said "Export will show real project names"
    # on every panel. Some renders produce artifacts that are byte-identical in
    # both privacy modes apart from the `anonymized:` frontmatter line, so on
    # those the line was making a false statement — and a warning users learn to
    # disregard on Forecast is one they may disregard on Projects. The client
    # renders a third, neutral state from this flag.
    #
    # Which renders those are is derived per render from the snapshot in hand,
    # never counted or listed: it depends on the data the panel actually holds,
    # and it varies within a single panel. Do not state a number here.
    #
    # ADDITIVE per `docs/cli-contract.md`: an optional key does not bump a
    # schema version, and consumers must tolerate unknown keys. Derived from
    # the same RAW snapshots the renderer was just handed, through the kernel's
    # `_map_project_display` enumeration — never from a panel list, which would
    # be a second source of truth and wrong at template granularity.
    has_project_names = any(
        ls.has_project_identities(item) for item in source_snaps)

    handler._respond_json(200, {
        "body": body,
        "content_type": content_type,
        "has_project_names": has_project_names,
        "snapshot": {
            "kernel_version": ls.KERNEL_VERSION,
            "panel": panel,
            "template_id": template_id,
            "options": options,
            "generated_at": _share_now_utc_iso(),
            "data_digest": data_digest,
            "data_digest_version": _SHARE_DATA_DIGEST_VERSION,
            **({"source": source} if source_explicit else {}),
            **account_meta,
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
    try:
        reveal_projects = _share_json_bool(req, "reveal_projects")
    except TypeError:
        handler._respond_json(400, {
            "error": "reveal_projects must be a boolean",
            "field": "reveal_projects",
        })
        return
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
    # the user's display tz, not UTC. Resolved to a CONCRETE IANA zone
    # (#503 S2 D7) so no section states the token `local` as its zone.
    composite_display_tz = _share_resolved_display_tz()

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
            # #416 §5.6: a section that captured an account must be BODY-scoped
            # to it, exactly as the render path is. Absent (every section the
            # shipped composer posts today) → account-agnostic and byte-stable;
            # malformed → the same fail-closed rejection as `source`.
            section_account = _share_account_selection(snap_recipe)
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
        # Not `setdefault`: a section that arrived carrying its own
        # `display_tz` is carrying a TOKEN, which must be resolved too.
        composite_opts["display_tz"] = (
            _share_resolved_display_tz(sec_opts["display_tz"])
            if isinstance(sec_opts.get("display_tz"), str) and sec_opts["display_tz"]
            else composite_display_tz
        )
        try:
            source_snaps, _source_states, _panel_data = _share_build_source_snapshots(
                ls=ls,
                template=template,
                template_id=template_id,
                panel=panel,
                options=composite_opts,
                source=source,
                source_explicit=source_explicit,
                data_snap=data_snap,
                account=section_account,
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
        # Same content toggles as the single-section render path, and the
        # same two-tuple ordering (#503 S3 §4): `digest_snaps` is what the
        # builders produced, `source_snaps` is what `compose()` renders.
        # Per-section `show_chart`/`show_table` from the basket
        # recipe are applied here; the composite anon flag is
        # already merged into composite_opts upstream.
        digest_snaps = source_snaps
        source_snaps = tuple(
            _share_apply_content_toggles(item, composite_opts)
            for item in source_snaps
        )
        # No pre-scrub (#503 S1): `compose()` prepares every section itself,
        # under one merged alias namespace. Scrubbing per section here is what
        # made `project-1` denote a different project in each section.
        #
        # The digest below is unaffected by the removal, and the reason has
        # changed with version 2. `_share_digest_input` now reads `title`,
        # `subtitle`, `columns`, `rows`, `chart`, `totals` and `notes` off each
        # snapshot, labels included — so the claim can no longer rest on "it
        # never reads a label". It rests on ORDER instead: the digest hashes
        # `digest_snaps`, the snapshots the builders produced, and every
        # anonymization happens later inside `compose()`. Nothing scrubbed can
        # reach the digest, so no basket section spuriously reads "Outdated".

        # Defensive: digest is non-blocking metadata — fall back to
        # "" on failure rather than 500-ing the whole compose
        # (mirrors the render handler at bin/cctally:33402-33408).
        try:
            digest_now = ls._data_digest(_share_digest_input(
                panel=panel,
                template_id=template_id,
                source=source,
                snapshots=digest_snaps,
                ls=ls,
                # #341 Task 4: carry the section's captured account so a
                # focus-changed section re-digests as drift (matching render).
                account=section_account,
            ))
        except Exception:
            digest_now = ""
        # Two digests are comparable only when they mean the same thing
        # (#503 S3 §4). A section stored before `_SHARE_DATA_DIGEST_VERSION`
        # existed, or under an older one, is NOT COMPARABLE — and not
        # comparable is not drifted, so it carries no badge rather than a
        # spurious "Outdated" the user cannot clear. An absent field is the
        # legacy case and reads as not comparable, which is the fail-safe
        # direction: it under-reports drift once instead of over-reporting it
        # for every stored section.
        digest_comparable = (
            snap_recipe.get("data_digest_version_at_add")
            == _SHARE_DATA_DIGEST_VERSION
        )
        drift_detected = digest_comparable and digest_now != digest_at_add
        composed_sections.extend(
            ls.ComposedSection(snap=item, drift_detected=drift_detected)
            for item in source_snaps
        )
        section_results.append({
            "snapshot_id": f"{idx:02d}",
            "source": source,
            "drift_detected": drift_detected,
            "data_digest_at_add": digest_at_add,
            "data_digest_now": digest_now,
            # ADDITIVE (docs/cli-contract.md): a consumer that does not know
            # this key keeps reading `drift_detected`, which is already
            # false whenever this is false.
            "digest_comparable": digest_comparable,
            "data_digest_version": _SHARE_DATA_DIGEST_VERSION,
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

    # #503 S3 §1. Absent means false, which is the fail-safe direction and is
    # what makes this compatible with a caller written before the field
    # existed: an unwitting save can no longer destroy a stored recipe.
    try:
        overwrite = _share_json_bool(req, "overwrite")
    except TypeError:
        handler._respond_json(400, {
            "error": "overwrite must be a boolean", "field": "overwrite",
        })
        return

    saved_at = _share_now_utc_iso()
    record = {
        "template_id": template_id, "options": options,
        "source": source, "saved_at": saved_at,
    }

    # The OUTCOME is decided under the lock; the RESPONSE is written after it.
    # `config_writer_lock` is a cross-process `fcntl.flock`, and a client that
    # reads its socket slowly would otherwise hold it for the length of that
    # write, blocking every other config writer in every other process.
    conflict = False
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.setdefault("share", {})
        presets = share.setdefault("presets", {})
        panel_bucket = presets.setdefault(panel, {})
        # Decided INSIDE the writer lock (spec §1): a client-side name-list
        # preflight can go stale between its GET and this write, so the
        # preflight is an optimisation and this is the authority. Nothing is
        # persisted on this branch — `save_config` is the only writer.
        if name in panel_bucket and not overwrite:
            conflict = True
        else:
            panel_bucket[name] = record
            save_config(cfg)
    if conflict:
        handler._respond_json(409, _SHARE_PRESET_CONFLICT("name", name))
        return
    handler._respond_json(200, {"panel": panel, "name": name, **record})


# The one conflict body both preset mutations answer with (spec §1). Stable
# machine-readable `code`, the offending field, and a message that names only
# the preset the caller already sent.
def _SHARE_PRESET_CONFLICT(field: str, name: str) -> dict:
    return {
        "code": "preset_name_conflict",
        "error": f"a preset named {name!r} already exists",
        "field": field,
    }


def _share_preset_name_error(field: str) -> dict:
    return {
        "error": "name must be 1-64 chars and contain no '/'",
        "field": field,
    }


def _handle_share_presets_rename_post_impl(handler) -> None:
    """Rename a preset atomically, keeping its identity (#503 S3 §1).

    Body: ``{panel, from_name, to_name, overwrite}``. CSRF-gated.

    Rename was not an operation: the client issued ``savePreset`` then
    ``deletePreset``, rebuilding the record from the four fields it happened
    to hold. That dropped ``source`` (the server defaults it to ``claude``,
    so a renamed Codex preset started showing a Claude chip), reset
    ``saved_at`` even though the recipe had not changed, and silently
    overwrote any preset already holding the target name.

    So this MOVES THE STORED RECORD WHOLE — ``bucket[to] = bucket.pop(from)``
    — under ONE ``config_writer_lock`` with one ``save_config``. Nothing
    reconstructs the record, so every field it carries survives, including
    fields added later.

    A self-rename is rejected explicitly: a move-then-delete on one key
    deletes the record, and the client-side guard is not sufficient because
    this endpoint is independently reachable.

    Write discipline is the presets POST's: ``config_writer_lock`` +
    ``_load_config_unlocked`` + ``save_config``. Never ``load_config`` inside
    the lock — ``fcntl.flock`` is per-fd and self-deadlocks.
    """
    if not handler._check_origin_csrf():
        return
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length > _SHARE_POST_MAX_BYTES:
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
    from_name = req.get("from_name")
    to_name = req.get("to_name")
    try:
        overwrite = _share_json_bool(req, "overwrite")
    except TypeError:
        handler._respond_json(400, {
            "error": "overwrite must be a boolean", "field": "overwrite",
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
    for value, field in ((from_name, "from_name"), (to_name, "to_name")):
        if (not isinstance(value, str) or not value
                or "/" in value or len(value) > 64):
            handler._respond_json(400, _share_preset_name_error(field))
            return
    if from_name == to_name:
        handler._respond_json(400, {
            "error": "from_name and to_name are the same preset",
            "field": "to_name",
        })
        return

    # Outcome decided under the lock, response written after it — the same
    # rule the save handler above states, for the same reason.
    outcome = "ok"
    record = None
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.get("share") or {}
        presets = share.get("presets") or {}
        panel_bucket = presets.get(panel) or {}
        if from_name not in panel_bucket:
            outcome = "missing"
        # The target is checked under the SAME lock that performs the move,
        # so two renames racing onto one name cannot both win.
        elif to_name in panel_bucket and not overwrite:
            outcome = "conflict"
        else:
            record = panel_bucket.pop(from_name)
            panel_bucket[to_name] = record
            save_config(cfg)
    if outcome == "missing":
        handler._respond_json(404, {"error": "no such preset"})
        return
    if outcome == "conflict":
        handler._respond_json(409, _SHARE_PRESET_CONFLICT("to_name", to_name))
        return
    handler._respond_json(200, {
        "panel": panel, "name": to_name,
        **(record if isinstance(record, dict) else {"record": record}),
    })

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
    # Outcome decided under the lock, response written after it — the same
    # rule the save and rename handlers state, for the same reason.
    missing = False
    with sys.modules["cctally"].config_writer_lock():
        cfg = _load_config_unlocked()
        share = cfg.get("share") or {}
        presets = share.get("presets") or {}
        panel_bucket = presets.get(panel) or {}
        if name not in panel_bucket:
            missing = True
        else:
            del panel_bucket[name]
            # Tidy empty buckets so GET stays clean.
            if not panel_bucket:
                presets.pop(panel, None)
            save_config(cfg)
    if missing:
        handler._respond_json(404, {"error": "no such preset"})
        return
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

# #503 S3 §3 — a history row is a discriminated union on `kind`.
#
# `"panel"` is every field the row has always carried. `"composed"` is one
# multi-section export: `panel: null`, a bounded `sections[]`, and the
# composite knobs. There is NO migration and none is needed: a missing `kind`
# READS as `"panel"` (normalized in the GET response only, never written
# back), and a composed row's `panel: null` makes an older client's
# `h.panel === panel` filter false, so it hides the row rather than
# mis-rendering it. `docs/share-gotchas.md` records that `share.*` keys need
# no formal migration; this shape keeps that true.
_SHARE_HISTORY_KINDS = ("panel", "composed")

# Twenty is the composer basket cap, so a composed row is bounded by
# construction — this is the server saying so rather than trusting it.
_SHARE_HISTORY_COMPOSED_MAX_SECTIONS = 20

# The fields that belong to exactly one branch. A row carrying the other
# branch's fields is rejected rather than silently half-read.
_SHARE_HISTORY_PANEL_ONLY_FIELDS = ("template_id", "options", "source",
                                    "account")
_SHARE_HISTORY_COMPOSED_ONLY_FIELDS = ("sections", "composite")


class _ShareHistoryError(ValueError):
    """Carry a 400 envelope out of the history validators."""

    def __init__(self, payload: Mapping):
        super().__init__(str(payload.get("error", "invalid history row")))
        self.payload = dict(payload)


def _share_history_read_kind(record) -> str:
    """The branch a STORED row belongs to. Absent is the legacy panel row."""
    if not isinstance(record, Mapping):
        return "panel"
    kind = record.get("kind")
    return kind if kind in _SHARE_HISTORY_KINDS else "panel"


def _share_history_normalize_record(record):
    """The read-side shape of one stored row (response only, never written).

    A panel row keeps its S4 `source` default; a composed row has no
    top-level source to default, and inventing one would put a provider
    label on a document that has one per section.
    """
    if not isinstance(record, Mapping):
        return record
    kind = _share_history_read_kind(record)
    if kind == "composed":
        return {**record, "kind": "composed", "panel": record.get("panel")}
    return {**record, "kind": "panel",
            "source": record.get("source", "claude")}


def _share_history_validate_section(tpl_mod, sec, idx: int) -> dict:
    """One composed section, held to the same invariants a panel row is."""
    field = f"sections[{idx}]"
    if not isinstance(sec, Mapping):
        raise _ShareHistoryError({
            "error": f"{field} must be an object", "field": field})
    panel = sec.get("panel")
    template_id = sec.get("template_id")
    options = sec.get("options")
    if options is None:
        options = {}
    if not isinstance(panel, str) or panel not in tpl_mod.SHARE_CAPABLE_PANELS:
        raise _ShareHistoryError({
            "error": f"unknown share panel: {panel!r}",
            "field": f"{field}.panel"})
    if not isinstance(template_id, str) or not template_id:
        raise _ShareHistoryError({
            "error": "missing or non-string template_id",
            "field": f"{field}.template_id"})
    try:
        template = tpl_mod.get_template(template_id)
    except KeyError:
        raise _ShareHistoryError({
            "error": f"unknown template_id: {template_id!r}",
            "field": f"{field}.template_id"}) from None
    if template.panel != panel:
        raise _ShareHistoryError({
            "error": (f"template_id {template_id!r} belongs to panel "
                      f"{template.panel!r}, not {panel!r}"),
            "field": f"{field}.template_id"})
    if not isinstance(options, Mapping):
        raise _ShareHistoryError({
            "error": "options must be an object",
            "field": f"{field}.options"})
    try:
        source, _ = _share_source_selection(dict(sec))
    except ValueError:
        raise _ShareHistoryError({
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
            "field": f"{field}.source"}) from None
    try:
        account = _share_account_selection(dict(sec))
    except ValueError:
        raise _ShareHistoryError({
            "error": "malformed account key",
            "field": f"{field}.account"}) from None
    return {
        "panel": panel, "template_id": template_id,
        "options": dict(options), "source": source,
        **({"account": account} if account is not None else {}),
    }


def _share_history_validate_composite(composite) -> dict:
    """The composite knobs a composed row states about the document."""
    if composite is None:
        composite = {}
    if not isinstance(composite, Mapping):
        raise _ShareHistoryError({
            "error": "composite must be an object", "field": "composite"})
    title = composite.get("title")
    if not isinstance(title, str) or not title or len(title) > 200:
        raise _ShareHistoryError({
            "error": "composite.title must be 1-200 chars",
            "field": "composite.title"})
    theme = composite.get("theme", "light")
    if theme not in ("light", "dark"):
        raise _ShareHistoryError({
            "error": f"unknown theme: {theme!r}", "field": "composite.theme"})
    knobs = {}
    for key in ("reveal_projects", "no_branding"):
        value = composite.get(key, False)
        if not isinstance(value, bool):
            raise _ShareHistoryError({
                "error": f"composite.{key} must be a boolean",
                "field": f"composite.{key}"})
        knobs[key] = value
    return {"title": title, "theme": theme, **knobs}


def _handle_share_history_get_impl(handler) -> None:
    """Return the recent-shares ring buffer (newest last, spec §11.4)."""
    cfg = sys.modules["cctally"].load_config()
    history = (cfg.get("share") or {}).get("history") or []
    handler._respond_json(200, {"history": [
        _share_history_normalize_record(record) for record in history
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
    # #503 S3 §3. An absent `kind` is the legacy panel row and validates
    # exactly as it always has; an unknown value is refused rather than
    # silently filed as one of the two branches.
    kind = req.get("kind", "panel")
    if kind not in _SHARE_HISTORY_KINDS:
        handler._respond_json(400, {
            "error": f"unknown history kind: {kind!r}",
            "field": "kind",
        })
        return
    tpl_mod = handler._share_load_templates_module()
    if kind == "composed":
        try:
            record = _share_history_composed_record(tpl_mod, req)
        except _ShareHistoryError as exc:
            handler._respond_json(400, exc.payload)
            return
        _share_history_append(handler, record)
        return
    for field in _SHARE_HISTORY_COMPOSED_ONLY_FIELDS:
        if field in req:
            handler._respond_json(400, {
                "error": f"{field} belongs to a composed row",
                "field": field,
            })
            return
    panel = req.get("panel")
    template_id = req.get("template_id")
    options = req.get("options") or {}
    # `fmt` and `destination` are read below from
    # `_share_history_advisory_strings(req)`, which validates them.
    try:
        source, _ = _share_source_selection(req)
        account = _share_account_selection(req)
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
    try:
        fmt, destination = _share_history_advisory_strings(req)
    except _ShareHistoryError as exc:
        handler._respond_json(400, exc.payload)
        return

    record = {
        "recipe_id": _share_history_recipe_id(),
        "kind": "panel",
        "panel": panel,
        "template_id": template_id,
        "options": options,
        "source": source,
        **({"account": account} if account is not None else {}),
        "format": fmt,
        "destination": destination,
        "exported_at": _share_now_utc_iso(),
    }
    _share_history_append(handler, record)


def _share_history_advisory_strings(req: Mapping) -> tuple:
    """`format` and `destination` — display hints, not contracts.

    Any non-empty string is accepted; the frontend uses them only as row
    labels in the dropdown. None/missing is allowed (mirrors how the CLI
    doesn't always know which destination produced the export).
    """
    values = []
    for field in ("format", "destination"):
        value = req.get(field)
        if value is not None and not isinstance(value, str):
            raise _ShareHistoryError({
                "error": f"{field} must be a string if provided",
                "field": field,
            })
        values.append(value)
    return tuple(values)


def _share_history_composed_record(tpl_mod, req: Mapping) -> dict:
    """Validate and build ONE composed history row (#503 S3 §3).

    Every section is held to exactly the invariants a panel row is — panel
    membership, template ownership, options shape, source and account —
    applied per section, because a section that would 400 on replay is the
    same poisoned dropdown row a bad panel row would be.
    """
    for field in _SHARE_HISTORY_PANEL_ONLY_FIELDS:
        if field in req:
            raise _ShareHistoryError({
                "error": f"{field} belongs to a panel row",
                "field": field,
            })
    panel = req.get("panel")
    if panel is not None:
        raise _ShareHistoryError({
            "error": "a composed row carries no panel",
            "field": "panel",
        })
    sections_in = req.get("sections")
    if (not isinstance(sections_in, list) or not sections_in
            or len(sections_in) > _SHARE_HISTORY_COMPOSED_MAX_SECTIONS):
        raise _ShareHistoryError({
            "error": (
                "sections must hold 1-"
                f"{_SHARE_HISTORY_COMPOSED_MAX_SECTIONS} entries"
            ),
            "field": "sections",
        })
    sections = [
        _share_history_validate_section(tpl_mod, sec, idx)
        for idx, sec in enumerate(sections_in)
    ]
    composite = _share_history_validate_composite(req.get("composite"))
    fmt, destination = _share_history_advisory_strings(req)
    return {
        "recipe_id": _share_history_recipe_id(),
        "kind": "composed",
        # EXPLICIT null, not an absent key: it is what makes an older
        # client's `h.panel === panel` filter hide this row.
        "panel": None,
        "sections": sections,
        "composite": composite,
        "format": fmt,
        "destination": destination,
        "exported_at": _share_now_utc_iso(),
    }


def _share_history_append(handler, record: dict) -> None:
    """Append one row to the ring buffer and answer with it.

    Write discipline matches the presets handlers: `config_writer_lock` +
    `_load_config_unlocked` + `save_config`.
    """
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
