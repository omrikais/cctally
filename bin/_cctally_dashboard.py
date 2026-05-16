"""Dashboard subsystem for cctally (live web server + share endpoints).

Eager I/O sibling: bin/cctally loads this at startup. Owns the
``cctally dashboard`` user-facing surface, the stdlib HTTP +
Server-Sent-Events server, the per-panel DataSnapshot assemblers, the
share-period override pipeline + per-panel share-data builders that
power the dashboard share GUI, the JSON-envelope shape consumed by
the React client, and the bind-config validators that gate LAN
exposure:

- ``cmd_dashboard`` — ``cctally dashboard`` entry point. Resolves
  ``--host`` / ``dashboard.bind`` config / ``--tz`` overrides; binds
  the ``ThreadingHTTPServer``; spins up the snapshot sync thread, the
  update-check thread, and the SSE hub; opens a browser when
  ``--open`` is set; handles ``SIGINT`` / ``SIGTERM`` for clean
  shutdown.
- ``DashboardHTTPHandler`` — the stdlib ``BaseHTTPRequestHandler``
  subclass that serves the static React bundle plus the entire
  ``/api/*`` surface (``data``, ``events``, ``sync``, ``refresh``,
  ``settings``, ``doctor``, ``alerts/test``, ``update`` family,
  ``share/*`` family, ``block/<window>``, ``session/<id>``). CSRF +
  Origin-header gating per ``docs/dashboard-gotchas.md``.
- ``SSEHub`` — thread-safe fan-out hub for SSE subscribers. Producers
  call ``publish(snapshot)`` (non-blocking, drops on full client
  queues so a slow browser cannot back-pressure the sync thread);
  consumers ``subscribe()`` for a ``queue.Queue`` and read with
  timeout.
- ``_SnapshotRef`` — thread-safe holder for the current
  ``DataSnapshot`` (read by handlers + SSE publisher; written by the
  sync thread; test-and-clear ``request_sync()`` flag avoids the
  ``threading.Event`` is_set/clear race).
- ``snapshot_to_envelope`` — serializes a ``DataSnapshot`` into the
  JSON envelope the React client consumes. Single source of truth
  for the wire shape; tests assert against it directly.
- ``_session_detail_to_envelope`` — sibling serializer for
  ``GET /api/session/<id>``; rebuilds ``TuiSessionDetail`` for one
  session id at request time.
- ``_dashboard_build_monthly_periods`` / ``_dashboard_build_weekly_periods``
  / ``_dashboard_build_daily_panel`` / ``_dashboard_build_blocks_panel``
  / ``_build_block_detail`` — panel data assemblers consumed by the
  sync thread (bulk rebuild) AND the share-period override pipeline
  (rebuild a single panel against a shifted ``now_utc``).
- ``_empty_dashboard_snapshot`` — minimal ``DataSnapshot`` used at
  startup before the first sync lands; gates the first-paint chrome
  while async DB fan-out is in flight.
- ``_select_current_block_for_envelope`` — picks the right
  ``five_hour_blocks`` row to embed in the envelope's
  ``current_week.five_hour_block`` field (latest open, fall through
  to latest closed if none).
- ``_build_alerts_envelope_array`` — folds percent + 5h alert
  fires (``alerts_settings.weekly_thresholds`` /
  ``five_hour_thresholds``) into the envelope's flat
  ``alerts_settings`` array consumed by the alerts panel.
- ``_format_url`` / ``_discover_lan_ip`` — opener helpers for the
  banner printed at startup; ``_format_url`` quotes IPv6 brackets
  for browser display while ``_resolve_dashboard_bind_for_runtime``
  unwraps them for ``socket.bind``.
- ``_model_breakdowns_to_models`` / ``_compute_intensity_buckets`` —
  envelope transforms (model-breakdown reshape + daily-cell color
  bucketing).
- ``_iso_z`` — formats a datetime as a Zulu ISO string with ``Z``
  suffix; used to keep wire timestamps tz-independent.
- ``_validate_dashboard_bind_value`` / ``_resolve_dashboard_bind_for_runtime``
  / ``_DASHBOARD_BIND_SEMANTIC_ALIASES`` — config validators for
  ``dashboard.bind``. Consumed by ``cmd_dashboard`` at boot and by
  ``_cctally_config.cmd_config`` (CLI + ``POST /api/settings``).
- Share-period override pipeline: ``_SHARE_PANELS_PERIOD_FIXED`` /
  ``_SHARE_PANELS_PERIOD_OVERRIDABLE`` plus
  ``_share_resolve_period`` (kind=current/previous/custom →
  now_override+start_override), ``_share_custom_window_n``
  (derives ``--last`` count from a custom date range),
  ``_share_previous_period_delta`` (per-panel "what does kind=previous
  shift mean?"), ``_share_apply_period_override`` (rebuilds the
  relevant panel field on a new ``DataSnapshot`` via
  ``dataclasses.replace``), and ``_share_apply_content_toggles``
  (post-override mutator for ``reveal-projects`` / hidden-row
  filters).
- Per-panel share-data builders: ``_build_share_panel_data`` is the
  dispatcher; ``_build_weekly_share_panel_data`` /
  ``_build_current_week_share_panel_data`` /
  ``_build_trend_share_panel_data`` /
  ``_build_daily_share_panel_data`` /
  ``_build_monthly_share_panel_data`` /
  ``_build_forecast_share_panel_data`` /
  ``_build_blocks_share_panel_data`` /
  ``_build_sessions_share_panel_data`` produce the
  panel-specific dict the share kernel renders.
- Project-side share helpers: ``_share_top_projects_for_range`` /
  ``_share_all_projects_for_range`` /
  ``_share_per_day_per_project_for_range`` /
  ``_share_per_block_per_project`` aggregate ``session_entries``
  joined to ``session_files`` for the per-project rows; cap at
  ``_SHARE_TOP_PROJECTS_BUILDER_CAP`` per spec.
- ``_share_empty_week_stub`` — empty-current-week placeholder
  consumed by the ``current-week`` and ``weekly`` builders when the
  selected period has no data.

What stays in bin/cctally:
- ``DataSnapshot``, ``RuntimeState``, and every ``Tui*`` dataclass
  (``TuiSessionRow``, ``TuiSessionDetail``, ``TuiPercentMilestone``,
  ``TuiTrendRow``, ``WeeklyPeriodRow``, ``MonthlyPeriodRow``,
  ``BlocksPanelRow``, ``DailyPanelRow``, ``TuiCurrentWeek``,
  ``Block``, ``ForecastOutput``) — SHARED with the TUI vertical
  (Phase F #23 has not landed yet). Both consumers reference these
  types; moving them now would break the upcoming TUI extraction.
  Moved bodies access them via ``c.X`` at call time (Python doesn't
  care whether the callable is a class or a function as long as it
  returns the expected object) — see the ``c = _cctally()``
  accessor pattern below.
- ``_TuiSyncThread`` — shared base class subclassed inline by
  ``_DashboardSyncThread`` inside ``cmd_dashboard`` (L21613 in
  pre-extract bin/cctally) AND used directly by ``cmd_tui`` for its
  own live-rebuild loop. Subclassing requires a real class reference
  at the call site; the moved ``cmd_dashboard`` body resolves it via
  ``c._TuiSyncThread`` so the base class binds at call time.
- All share-CLI helpers (``_share_load_lib``, ``_share_now_utc``,
  ``_share_now_utc_iso``, ``_share_history_recipe_id``,
  ``_share_resolve_version``, ``_share_period_label``,
  ``_share_parse_date_to_dt``, ``_share_display_tz_label``,
  ``_share_iso``, ``_share_validate_args``,
  ``_share_resolve_download_dir``, ``_share_unique_path``,
  ``_resolve_destination``, ``_emit``, ``_share_render_and_emit``,
  ``_share_open_file``) — consumed by every CLI subcommand
  (``cmd_daily``, ``cmd_monthly``, ``cmd_weekly``, ``cmd_report``,
  ``cmd_forecast``, ``cmd_project``, ``cmd_session``,
  ``cmd_five_hour_blocks``) so they must stay alongside those
  ``cmd_*`` functions. The dashboard's per-panel share-data builders
  in this sibling consume them via the module-level shims below.
- All ``_build_<panel>_snapshot`` builders (``_build_report_snapshot``,
  ``_build_daily_snapshot``, ``_build_monthly_snapshot``,
  ``_build_weekly_snapshot``, ``_build_forecast_snapshot``,
  ``_build_project_snapshot``, ``_build_five_hour_blocks_snapshot``,
  ``_build_session_snapshot``) — same reason; consumed by the CLI
  subcommands AND by the share render pipeline.
- ``ORIGINAL_SYS_ARGV`` / ``ORIGINAL_ENTRYPOINT`` /
  ``_UPDATE_WORKER`` — module-level globals that ``cmd_dashboard``
  writes via ``global`` at startup; consumed by the moved
  ``UpdateWorker`` / ``_DashboardUpdateCheckThread`` via
  ``cctally.X`` reads. The ``global`` statement only works in the
  module where the names are declared, so ``cmd_dashboard`` rebinds
  them via the cctally module proxy: ``c = _cctally();
  c.ORIGINAL_SYS_ARGV = list(sys.argv)``.
- ``eprint``, ``now_utc_iso``, ``parse_iso_datetime``, ``open_db``,
  ``load_config``, ``save_config``, ``_now_utc``, ``_command_as_of``,
  ``format_display_dt``, ``resolve_display_tz``,
  ``normalize_display_tz_value``, ``_render_migration_error_banner``,
  ``_aggregate_daily``, ``_aggregate_monthly``, ``_aggregate_weekly``,
  ``_calculate_entry_cost``, ``_canonical_5h_window_key``,
  ``_chip_for_model``, ``_short_model_name``,
  ``_compute_subscription_weeks``, ``_group_entries_into_blocks``,
  ``get_entries``, ``get_claude_session_entries``,
  ``get_latest_usage_for_week``, ``make_week_ref``,
  ``_get_alerts_config``, ``_AlertsConfigError``,
  ``_warn_alerts_bad_config_once``,
  ``_OAUTH_USAGE_DEFAULTS``, ``_load_recorded_five_hour_windows``,
  ``_make_run_sync_now``, ``_make_run_sync_now_locked``,
  ``_build_forecast_json_payload``, ``_build_alert_payload_weekly``,
  ``_build_alert_payload_five_hour``, ``_dispatch_alert_notification``,
  ``doctor_gather_state``, ``sync_cache`` — accessed via the
  ``c = _cctally(); c.X`` accessor inside each moved function or via
  the module-level callable shims below (the shim pattern matches
  ``bin/_cctally_update.py`` precedent — Python sees a bare-name
  call ``eprint(...)``, the call resolves to the shim, the shim
  delegates to ``sys.modules['cctally'].eprint(...)`` at runtime).
- ``UpdateError``, ``UpdateWorker``, ``_DashboardUpdateCheckThread``
  (live in ``_cctally_update`` since Phase F #21; re-exported through
  ``bin/cctally`` so moved dashboard code resolves them via ``c.X``
  at call time).

§5.6 audit on this extraction's monkeypatch surface
(``tests/test_dashboard_*.py`` + ``tests/test_share_*.py``: 19+ distinct
``ns["X"]`` direct-dict reads on moved symbols including
``ns["_SnapshotRef"]``, ``ns["SSEHub"]``, ``ns["DashboardHTTPHandler"]``,
``ns["_format_url"]``, ``ns["_discover_lan_ip"]``,
``ns["_compute_intensity_buckets"]``,
``ns["_dashboard_build_monthly_periods"]``,
``ns["_dashboard_build_weekly_periods"]``,
``ns["_dashboard_build_blocks_panel"]``,
``ns["_dashboard_build_daily_panel"]``,
``ns["_empty_dashboard_snapshot"]``, ``ns["_build_block_detail"]``,
``ns["_select_current_block_for_envelope"]``,
``ns["snapshot_to_envelope"]``, ``ns["_share_resolve_period"]``,
``ns["_share_custom_window_n"]``, ``ns["_share_apply_period_override"]``,
``ns["_share_top_projects_for_range"]``,
``ns["_build_weekly_share_panel_data"]``,
``ns["_build_current_week_share_panel_data"]``,
``ns["_build_daily_share_panel_data"]``,
``ns["_build_monthly_share_panel_data"]``,
``ns["_build_blocks_share_panel_data"]``, ``ns["STATIC_DIR"]``,
``ns["_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS"]``, plus
``monkeypatch.setitem`` mutations on
``_dashboard_build_weekly_periods``, ``_dashboard_build_blocks_panel``,
and ``_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS``). Forces the **eager
re-export** carve-out per spec §4.8 (same precedent as Phase E
#19/#20 + Phase F #21):

- ``ns["X"]`` reads on dataclass / function / class objects propagate
  via eager re-export; PEP 562 ``__getattr__`` does NOT fire on
  ``ns["X"]`` dict-key access because ``ns`` is the module's
  ``__dict__``, not the module proxy. Re-export at module-load time
  means cctally's ``__dict__`` carries the same object the sibling
  defines.
- ``monkeypatch.setitem(ns, "X", mock)`` mutates cctally's namespace.
  For a moved symbol that is ALSO called bare-name by another moved
  body (e.g. ``DashboardHTTPHandler._serve_api_data`` calls
  ``_dashboard_build_weekly_periods`` via the module-level shim;
  ``cmd_dashboard`` references ``STATIC_DIR`` at call time), the
  internal bare-name lookup resolves in this sibling's ``__dict__``,
  NOT cctally's — so the mock would not propagate. Pattern matches
  Phase D #17/#18 + F #21: every cross-call from one moved function
  to another moved function that's also a monkeypatch target routes
  through ``c.X`` (alias for ``sys.modules['cctally'].X``) at call
  time. The accessor resolves at every call so the latest binding
  wins; mocks propagate without sibling-side patches.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §7.2
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import dataclasses
import datetime as dt
import io
import json
import math
import os
import pathlib
import queue
import re
import shutil
import signal as _signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser as _wb
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. Mirrors the precedent established in
# ``bin/_cctally_record.py`` (34 shims), ``bin/_cctally_cache.py``
# (4 shims), ``bin/_cctally_db.py`` (4 shims), and
# ``bin/_cctally_update.py`` (8 shims).
def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


def now_utc_iso(*args, **kwargs):
    return sys.modules["cctally"].now_utc_iso(*args, **kwargs)


def parse_iso_datetime(*args, **kwargs):
    return sys.modules["cctally"].parse_iso_datetime(*args, **kwargs)


def _now_utc(*args, **kwargs):
    return sys.modules["cctally"]._now_utc(*args, **kwargs)


def _command_as_of(*args, **kwargs):
    return sys.modules["cctally"]._command_as_of(*args, **kwargs)


def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def save_config(*args, **kwargs):
    return sys.modules["cctally"].save_config(*args, **kwargs)


def open_db(*args, **kwargs):
    return sys.modules["cctally"].open_db(*args, **kwargs)


def get_entries(*args, **kwargs):
    return sys.modules["cctally"].get_entries(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def get_latest_usage_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_latest_usage_for_week(*args, **kwargs)


def make_week_ref(*args, **kwargs):
    return sys.modules["cctally"].make_week_ref(*args, **kwargs)


def format_display_dt(*args, **kwargs):
    return sys.modules["cctally"].format_display_dt(*args, **kwargs)


def resolve_display_tz(*args, **kwargs):
    return sys.modules["cctally"].resolve_display_tz(*args, **kwargs)


def normalize_display_tz_value(*args, **kwargs):
    return sys.modules["cctally"].normalize_display_tz_value(*args, **kwargs)


def _compute_display_block(*args, **kwargs):
    return sys.modules["cctally"]._compute_display_block(*args, **kwargs)


def _render_migration_error_banner(*args, **kwargs):
    return sys.modules["cctally"]._render_migration_error_banner(*args, **kwargs)


def _aggregate_daily(*args, **kwargs):
    return sys.modules["cctally"]._aggregate_daily(*args, **kwargs)


def _aggregate_monthly(*args, **kwargs):
    return sys.modules["cctally"]._aggregate_monthly(*args, **kwargs)


def _aggregate_weekly(*args, **kwargs):
    return sys.modules["cctally"]._aggregate_weekly(*args, **kwargs)


def _calculate_entry_cost(*args, **kwargs):
    return sys.modules["cctally"]._calculate_entry_cost(*args, **kwargs)


def _canonical_5h_window_key(*args, **kwargs):
    return sys.modules["cctally"]._canonical_5h_window_key(*args, **kwargs)


def _chip_for_model(*args, **kwargs):
    return sys.modules["cctally"]._chip_for_model(*args, **kwargs)


def _short_model_name(*args, **kwargs):
    return sys.modules["cctally"]._short_model_name(*args, **kwargs)


def _compute_subscription_weeks(*args, **kwargs):
    return sys.modules["cctally"]._compute_subscription_weeks(*args, **kwargs)


def _group_entries_into_blocks(*args, **kwargs):
    return sys.modules["cctally"]._group_entries_into_blocks(*args, **kwargs)


def _get_alerts_config(*args, **kwargs):
    return sys.modules["cctally"]._get_alerts_config(*args, **kwargs)


def _warn_alerts_bad_config_once(*args, **kwargs):
    return sys.modules["cctally"]._warn_alerts_bad_config_once(*args, **kwargs)


def _load_recorded_five_hour_windows(*args, **kwargs):
    return sys.modules["cctally"]._load_recorded_five_hour_windows(*args, **kwargs)


def _make_run_sync_now(*args, **kwargs):
    return sys.modules["cctally"]._make_run_sync_now(*args, **kwargs)


def _make_run_sync_now_locked(*args, **kwargs):
    return sys.modules["cctally"]._make_run_sync_now_locked(*args, **kwargs)


def _build_forecast_json_payload(*args, **kwargs):
    return sys.modules["cctally"]._build_forecast_json_payload(*args, **kwargs)


def _build_alert_payload_weekly(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_weekly(*args, **kwargs)


def _build_alert_payload_five_hour(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_five_hour(*args, **kwargs)


def _dispatch_alert_notification(*args, **kwargs):
    return sys.modules["cctally"]._dispatch_alert_notification(*args, **kwargs)


def doctor_gather_state(*args, **kwargs):
    return sys.modules["cctally"].doctor_gather_state(*args, **kwargs)


def _load_config_unlocked(*args, **kwargs):
    return sys.modules["cctally"]._load_config_unlocked(*args, **kwargs)


def _apply_display_tz_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_display_tz_override(*args, **kwargs)


def _refresh_usage_inproc(*args, **kwargs):
    return sys.modules["cctally"]._refresh_usage_inproc(*args, **kwargs)


def _get_oauth_usage_config(*args, **kwargs):
    return sys.modules["cctally"]._get_oauth_usage_config(*args, **kwargs)


def _freshness_label(*args, **kwargs):
    return sys.modules["cctally"]._freshness_label(*args, **kwargs)


def _resolve_display_tz_obj(*args, **kwargs):
    return sys.modules["cctally"]._resolve_display_tz_obj(*args, **kwargs)


def get_display_tz_pref(*args, **kwargs):
    return sys.modules["cctally"].get_display_tz_pref(*args, **kwargs)


def _load_update_state(*args, **kwargs):
    return sys.modules["cctally"]._load_update_state(*args, **kwargs)


def _load_update_suppress(*args, **kwargs):
    return sys.modules["cctally"]._load_update_suppress(*args, **kwargs)


def _do_update_skip(*args, **kwargs):
    return sys.modules["cctally"]._do_update_skip(*args, **kwargs)


def _do_update_remind_later(*args, **kwargs):
    return sys.modules["cctally"]._do_update_remind_later(*args, **kwargs)


def _validate_update_check_ttl_hours_value(*args, **kwargs):
    return sys.modules["cctally"]._validate_update_check_ttl_hours_value(*args, **kwargs)


def _config_known_value(*args, **kwargs):
    return sys.modules["cctally"]._config_known_value(*args, **kwargs)


def config_writer_lock(*args, **kwargs):
    return sys.modules["cctally"].config_writer_lock(*args, **kwargs)


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


# Dataclass shims — Python doesn't care whether the callable is a class
# or a function as long as ``isinstance`` is not used downstream; these
# wrappers delegate construction to the real classes in cctally so the
# moved bodies' bare-name constructor calls keep working unchanged. The
# shared dataclasses STAY in cctally (Phase F #23 / TUI vertical).
def DataSnapshot(*args, **kwargs):
    return sys.modules["cctally"].DataSnapshot(*args, **kwargs)


def DailyPanelRow(*args, **kwargs):
    return sys.modules["cctally"].DailyPanelRow(*args, **kwargs)


def WeeklyPeriodRow(*args, **kwargs):
    return sys.modules["cctally"].WeeklyPeriodRow(*args, **kwargs)


def MonthlyPeriodRow(*args, **kwargs):
    return sys.modules["cctally"].MonthlyPeriodRow(*args, **kwargs)


def BlocksPanelRow(*args, **kwargs):
    return sys.modules["cctally"].BlocksPanelRow(*args, **kwargs)


# Update-vertical types consumed at call time by the dashboard handlers
# + cmd_dashboard. UpdateError / UpdateWorker / _DashboardUpdateCheckThread
# live in _cctally_update (Phase F #21); the names below resolve through
# cctally's re-exported namespace so monkeypatches on cctally propagate.
#
# UpdateError is NOT a callable shim — exception classes used in `except`
# clauses must be the class object itself, not a function that returns
# one (Python rejects non-class objects in `except` with TypeError).
# All `except UpdateError:` sites in this file use the explicit
# `except sys.modules["cctally"].UpdateError:` form (3 sites; same
# pattern as `_AlertsConfigError` per Phase D #18 precedent).
def UpdateWorker(*args, **kwargs):
    return sys.modules["cctally"].UpdateWorker(*args, **kwargs)


def _DashboardUpdateCheckThread(*args, **kwargs):
    return sys.modules["cctally"]._DashboardUpdateCheckThread(*args, **kwargs)


# Module-level __getattr__ — lazy-resolves a handful of cctally globals at
# attribute-access time (used for the ``except cctally.SomeError`` sites
# that need real class identity, and for non-callable constants that the
# bare-name shim pattern can't wrap). PEP 562 fires on
# ``module.X``-shaped access from outside this module; bare-name lookups
# in function bodies bypass it. The rare bare-name read sites for
# _OAUTH_USAGE_DEFAULTS / _SHARE_HISTORY_RING_CAP / SKIP_USE_STATE_LATEST
# / config_writer_lock are rewritten to ``sys.modules["cctally"].X`` at
# their call sites; this __getattr__ handles the test-harness
# ``ns["_AlertsConfigError"]`` etc. access pattern.
_LAZY_ATTRS = (
    "_AlertsConfigError",
    "_OAUTH_USAGE_DEFAULTS",
    "_SHARE_HISTORY_RING_CAP",
    "SKIP_USE_STATE_LATEST",
    "config_writer_lock",
)


def __getattr__(name):  # pylint: disable=invalid-name
    if name in _LAZY_ATTRS:
        return getattr(sys.modules["cctally"], name)
    raise AttributeError(name)


# Module-level bindings pulled from cctally at sibling-load time. These
# are pure constants / read-only objects whose identity is stable across
# the process lifetime; binding them once at load time keeps bare-name
# reads in moved bodies working without per-call attribute lookups.
# Path constants and tunables that tests monkeypatch (STATIC_DIR,
# _DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS) are eager-re-exported FROM the
# sibling at bin/cctally so monkeypatches propagate; this block carries
# things that are NEVER patched at runtime.
BLOCK_DURATION = sys.modules["cctally"].BLOCK_DURATION


# === STATIC_DIR — dashboard static-asset root ==============================
STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "static"


# === Dashboard bind validators (config + cmd_dashboard) ====================

_DASHBOARD_BIND_SEMANTIC_ALIASES = {
    "lan": "0.0.0.0",
    "loopback": "127.0.0.1",
}




def _validate_dashboard_bind_value(raw) -> str:
    """Validate and canonicalize a dashboard.bind config value.

    Accepts: 'lan' | 'loopback' | any non-empty whitespace-free host string
    (IPv4, IPv6, hostname). Returns the canonicalized stored form.
    Raises ValueError on unknown shape.
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"dashboard.bind must be a string, got {type(raw).__name__}")
    s = raw.strip()
    if not s:
        raise ValueError("dashboard.bind must be a non-empty string")
    if s != raw or any(ch.isspace() for ch in s):
        # Reject any embedded whitespace (spaces, tabs, newlines) — IP/host
        # literals don't contain whitespace; users hand-typing 'not a host'
        # should fail loud.
        raise ValueError(f"dashboard.bind has invalid host shape: {raw!r}")
    if s.startswith("[") or s.endswith("]"):
        # Bracketed IPv6 form is for URL display only; socket.bind() needs the
        # bare address. Storing '[::1]' would surface as a confusing
        # gaierror at server-start.
        raise ValueError(
            f"dashboard.bind: do not use bracketed IPv6 form for bind "
            f"(use '::1' not '[::1]'): {raw!r}")
    return s


def _resolve_dashboard_bind_for_runtime(stored: str) -> str:
    """Map stored dashboard.bind value to a literal host for socket bind.

    'lan' -> '0.0.0.0', 'loopback' -> '127.0.0.1', everything else passes through.
    """
    return _DASHBOARD_BIND_SEMANTIC_ALIASES.get(stored, stored)


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
    c = _cctally()
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
        entries = get_claude_session_entries(
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
        entries = get_claude_session_entries(
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
        entries = get_claude_session_entries(
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
    total = sum(float(getattr(r, "cost_usd", 0.0) or 0.0) for r in last_7) or 1.0
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

    Reuses `DataSnapshot.forecast` (ForecastOutput).
    `projection_curve` is synthesized from `r_avg` / `r_recent` /
    `inputs.p_now` — the same arithmetic `snapshot_to_envelope` does for
    `week_avg_projection_pct` / `recent_24h_projection_pct`, extended
    across the next 7 days.
    """
    fc = getattr(snap, "forecast", None) if snap else None
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
    # Daily budgets — pull from fc.budgets[] when present
    budgets: dict = {"avg": 0.0, "recent_24h": 0.0,
                     "until_90pct": 0.0, "until_100pct": 0.0}
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




# === Dashboard server core: _SnapshotRef + SSEHub + envelope builders =====
# Pre-extract location: bin/cctally L16265.

class _SnapshotRef:
    """Thread-safe holder for the current DataSnapshot."""

    def __init__(self, initial: DataSnapshot) -> None:
        import threading
        self._lock = threading.Lock()
        self._snap = initial
        self._sync_requested = False

    def get(self) -> DataSnapshot:
        with self._lock:
            return self._snap

    def set(self, snap: DataSnapshot) -> None:
        with self._lock:
            self._snap = snap

    def request_sync(self) -> None:
        with self._lock:
            self._sync_requested = True

    def take_sync_request(self) -> bool:
        # Atomic test-and-clear — threading.Event's is_set()/clear() pair
        # could drop a request_sync() call racing between them.
        with self._lock:
            taken, self._sync_requested = self._sync_requested, False
            return taken


class SSEHub:
    """Thread-safe fan-out hub for SSE clients.

    Producers call `publish(snapshot)` — non-blocking, drops on full
    client queues so a slow browser cannot back-pressure the sync thread.
    Consumers call `subscribe()` to obtain a `queue.Queue`, then read
    with a timeout; call `unsubscribe()` on disconnect or at teardown.
    """

    def __init__(self, maxsize: int = 4) -> None:
        import threading
        import queue as _queue
        self._lock = threading.Lock()
        self._queues: list[_queue.Queue] = []
        self._maxsize = maxsize
        # Held so we can send the current state to a newly-subscribed
        # client without waiting for the next sync tick.
        self._last: object | None = None

    def subscribe(self):
        import queue as _queue
        q = _queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._queues.append(q)
            if self._last is not None:
                # Seed the new subscriber so it renders immediately.
                try:
                    q.put_nowait(self._last)
                except _queue.Full:
                    pass
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def publish(self, snapshot) -> None:
        import queue as _queue
        with self._lock:
            self._last = snapshot
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(snapshot)
            except _queue.Full:
                # Slow client — drop this frame; the next tick re-sends.
                pass


STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "static"


def _format_url(host: str, port: int) -> str:
    """Build a browser-ready URL, bracketing IPv6 hosts per RFC 3986.

    `http://::1:8789/` is malformed (the second `:` is ambiguous);
    `http://[::1]:8789/` is the correct form. Already-bracketed hosts pass
    through unchanged.
    """
    if host.startswith("["):
        return f"http://{host}:{port}/"
    if ":" in host:
        return f"http://[{host}]:{port}/"
    return f"http://{host}:{port}/"


def _discover_lan_ip() -> "str | None":
    """Return the kernel's chosen IPv4 source address for off-host traffic.

    Uses the UDP-no-send trick: connect() on an unconnected UDP socket
    populates getsockname() with the right interface IP without sending a
    packet. Returns None on any OSError (offline, IPv6-only, restrictive
    firewall) so callers can fall back to localhost-only banner output.

    Honors CCTALLY_TEST_LAN_IP for fixture-stable banner goldens:
      - any non-empty value other than '__SUPPRESS__' is returned verbatim
      - '__SUPPRESS__' returns None unconditionally
    """
    forced = os.environ.get("CCTALLY_TEST_LAN_IP")
    if forced == "__SUPPRESS__":
        return None
    if forced:
        return forced
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))   # TEST-NET-1; not routed
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _model_breakdowns_to_models(model_breakdowns: list[dict[str, Any]],
                                period_cost: float) -> list[dict[str, Any]]:
    """Reshape a BucketUsage.model_breakdowns list (sorted desc by cost)
    into the dashboard envelope's `models` shape: short display name,
    chip key, and cost percentage of the period."""
    out: list[dict[str, Any]] = []
    for mb in model_breakdowns:
        canonical = mb["modelName"]
        cost = float(mb["cost"])
        pct = (cost / period_cost * 100.0) if period_cost > 0 else 0.0
        # Display: reuse the canonical short-name helper (strips "claude-"
        # prefix and any trailing "-YYYYMMDD" date suffix via regex).
        display = _short_model_name(canonical)
        out.append({
            "model": canonical,
            "display": display,
            "chip": _chip_for_model(canonical),
            "cost_usd": cost,
            "cost_pct": pct,
        })
    return out


def _compute_intensity_buckets(rows: "list[DailyPanelRow]") -> list[float]:
    """Mutates rows in place to set `intensity_bucket`. Returns the
    threshold cut points (length 5 when any non-zero days exist, [] otherwise).

    Differs from spec §5.1 pseudocode: the spec saturates the largest of
    5 distinct values at bucket 4; this variant correctly reaches bucket
    5. See `test_compute_intensity_buckets_quintile_distribution`.

    Bucket 0 → cost == 0 (always).
    Buckets 1..5 → quintile over non-zero days only, so a string of
    zero-cost days doesn't flatten the scale.

    The five raw cut points are taken from the sorted non-zero costs at
    relative positions 0/5, 1/5, 2/5, 3/5, 4/5 (treated as "lower bound
    of bucket 1..5"). When fewer than five distinct non-zero values
    exist (e.g. one non-zero day), duplicate cut points are kept in the
    returned threshold list for telemetry, but bucket assignment uses a
    deduplicated copy so a single distinct value collapses to bucket 1
    rather than saturating at bucket 5.
    """
    nonzero = sorted(r.cost_usd for r in rows if r.cost_usd > 0)
    if not nonzero:
        for r in rows:
            r.intensity_bucket = 0
        return []
    # Five cut points at relative quintiles of the sorted non-zero days.
    # Using i in 0..4 (rather than 1..5) makes each threshold the LOWER
    # bound of its bucket — so a value v lands in bucket `count of
    # thresholds <= v`, which is exactly `bisect_right(thresholds, v)`.
    thresholds = [
        nonzero[min(int(i / 5 * len(nonzero)), len(nonzero) - 1)]
        for i in (0, 1, 2, 3, 4)
    ]
    # Dedup for the bucket-assignment lookup: with one distinct non-zero
    # value the raw list is [v]*5, and bisect_right would return 5 →
    # bucket 5. Dedup → [v], bisect_right → 1 → bucket 1, matching the
    # "degenerate data lands at the lowest active bucket" intent.
    distinct = sorted(set(thresholds))
    for r in rows:
        if r.cost_usd == 0:
            r.intensity_bucket = 0
        else:
            r.intensity_bucket = min(bisect.bisect_right(distinct, r.cost_usd), 5)
    return thresholds


def _dashboard_build_monthly_periods(conn: "sqlite3.Connection",
                                     now_utc: "dt.datetime",
                                     *, n: int = 12,
                                     skip_sync: bool = False,
                                     display_tz: "ZoneInfo | None" = None) -> "list[MonthlyPeriodRow]":
    """Latest n calendar months as MonthlyPeriodRow, newest-first.

    Builds via `_aggregate_monthly` over the trailing window
    [now_utc - n calendar months, now_utc]. Bucketing and `is_current`
    label both follow `display_tz` so users on a non-host display zone
    see months grouped consistently with the rest of the UI.
    """
    # Compute window start = first day of the month that is (n - 1) months
    # before the current month. _aggregate_monthly walks all entries and
    # groups by local month; passing a wide window keeps it simple.
    cur_year = now_utc.year
    cur_month = now_utc.month
    months_back = n - 1
    # Roll back month-by-month so calendar arithmetic stays clear.
    sy, sm = cur_year, cur_month
    for _ in range(months_back):
        sm -= 1
        if sm == 0:
            sm = 12
            sy -= 1
    range_start = dt.datetime(sy, sm, 1, tzinfo=dt.timezone.utc)
    range_end = now_utc

    entries = get_entries(range_start, range_end, skip_sync=skip_sync)
    buckets = _aggregate_monthly(entries, mode="auto", tz=display_tz)
    # Reverse for newest-first AND cap to n BEFORE the delta loop. In tzs
    # west of UTC, `range_start` (UTC midnight on the 1st) lands in the
    # PREVIOUS local month, so entries in the boundary window get bucketed
    # as a (n+1)th `'YYYY-MM'` row. Slicing here drops that partial bucket,
    # which (a) keeps the visible history at the requested length and
    # (b) makes the oldest visible row's delta `None` (prev = None) rather
    # than a wildly wrong delta vs. a few-hour spillover bucket.
    buckets = list(reversed(buckets))[:n]
    rows: list[MonthlyPeriodRow] = []
    # `_aggregate_monthly` keys buckets by `display_tz` (or local-tz when
    # unset) month. Mirror that here so `is_current` matches even when
    # now_utc straddles a tz month boundary (e.g. 23:30 UTC on the last
    # of the month in a UTC+1 zone).
    cur_label = (
        now_utc.astimezone(display_tz) if display_tz is not None
        # internal fallback: host-local intentional
        else now_utc.astimezone()
    ).strftime("%Y-%m")
    for i, b in enumerate(buckets):
        # b.bucket is the YYYY-MM string for monthly aggregation.
        prev = buckets[i + 1] if i + 1 < len(buckets) else None
        delta = None
        if prev is not None and prev.cost_usd > 0:
            delta = (b.cost_usd - prev.cost_usd) / prev.cost_usd
        rows.append(MonthlyPeriodRow(
            label=b.bucket,
            cost_usd=b.cost_usd,
            total_tokens=b.total_tokens,
            input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
            delta_cost_pct=delta,
            is_current=(b.bucket == cur_label),
            models=_model_breakdowns_to_models(b.model_breakdowns, b.cost_usd),
        ))
    return rows


def _dashboard_build_weekly_periods(conn: "sqlite3.Connection",
                                    now_utc: "dt.datetime",
                                    *, n: int = 12,
                                    skip_sync: bool = False) -> "list[WeeklyPeriodRow]":
    """Latest n subscription weeks as WeeklyPeriodRow, newest-first.

    Mirrors the bucket+overlay path used by `cmd_weekly`, scoped to a
    trailing 84-day window (12 weeks plus slack). Boundaries come from
    `_compute_subscription_weeks`; usage % comes from
    `weekly_usage_snapshots` via `get_latest_usage_for_week`.

    Note: weekly bucketing intentionally does NOT take ``display_tz`` —
    SubWeek bucket keys come from server-anchored stored anchors and the
    panel labels are date-only (``%m-%d``), so the output is tz-independent
    by construction. The asymmetry vs. the daily/monthly/sessions panel
    builders (which DO localize) is deliberate; do not "fix" it.
    """
    range_end = now_utc
    range_start = now_utc - dt.timedelta(days=7 * (n + 1))
    weeks = _compute_subscription_weeks(conn, range_start, range_end)
    if not weeks:
        return []
    fetch_start = min(
        range_start,
        parse_iso_datetime(weeks[0].start_ts, "week_start_at"),
    )
    entries = get_entries(fetch_start, range_end, skip_sync=skip_sync)
    buckets = _aggregate_weekly(entries, weeks)
    if not buckets:
        return []

    as_of_utc = (
        range_end.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Prefer the SubWeek that actually contains `now_utc` so the "Now" pill
    # tracks wall time even when `weekly_usage_snapshots` is stale (e.g.,
    # status line hasn't fired yet this week but cost entries already exist).
    # Mid-week resets are still handled correctly: the post-reset window IS
    # a SubWeek, and `now_utc` lands inside it. Fall back to the latest
    # snapshot's week_start_date for boundary edge cases, then to the newest
    # computed week as last resort.
    cur_week_start: str | None = None
    for w in weeks:
        start_dt = parse_iso_datetime(w.start_ts, "week_start_at")
        end_dt = parse_iso_datetime(w.end_ts, "week_end_at")
        if start_dt <= now_utc < end_dt:
            cur_week_start = w.start_date.isoformat()
            break
    if cur_week_start is None:
        latest_usage = conn.execute(
            "SELECT week_start_date FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        if latest_usage is not None and latest_usage["week_start_date"] is not None:
            cur_week_start = latest_usage["week_start_date"]
        else:
            cur_week_start = max(weeks, key=lambda w: w.start_date).start_date.isoformat()

    rows_oldest_first: list[WeeklyPeriodRow] = []
    for bucket in buckets:
        sw = next(w for w in weeks if w.start_date.isoformat() == bucket.bucket)
        # Label = MM-DD of the week's display_start_date — for non-reset
        # weeks this equals start_date; for post-early-reset weeks the
        # post-processor shifts it forward to the effective reset moment
        # so the user sees the date the week actually began (04-23 vs the
        # API-derived backdated 04-18).
        label = sw.display_start_date.strftime("%m-%d")
        ref = make_week_ref(
            week_start_date=sw.start_date.isoformat(),
            week_end_date=sw.end_date.isoformat(),
            week_start_at=sw.start_ts,
            week_end_at=sw.end_ts,
        )
        usage_row = get_latest_usage_for_week(conn, ref, as_of_utc=as_of_utc)
        used_pct = None
        dpp = None
        if usage_row is not None and usage_row["weekly_percent"] is not None:
            used_pct = float(usage_row["weekly_percent"])
            dpp = (bucket.cost_usd / used_pct) if used_pct > 0 else None
        rows_oldest_first.append(WeeklyPeriodRow(
            label=label,
            cost_usd=bucket.cost_usd,
            total_tokens=bucket.total_tokens,
            input_tokens=bucket.input_tokens,
            output_tokens=bucket.output_tokens,
            cache_creation_tokens=bucket.cache_creation_tokens,
            cache_read_tokens=bucket.cache_read_tokens,
            used_pct=used_pct,
            dollar_per_pct=dpp,
            delta_cost_pct=None,  # filled below in newest-first pass
            # is_current keys on start_date (the bucket / lookup key) on both
            # sides of the comparison; display_start_date may diverge for
            # reset-event weeks but that is intentional — display vs. lookup
            # are kept separate.
            is_current=(sw.start_date.isoformat() == cur_week_start),
            models=_model_breakdowns_to_models(bucket.model_breakdowns, bucket.cost_usd),
            week_start_at=sw.start_ts,
            week_end_at=sw.end_ts,
        ))

    # Bug K (v1.7.2 round-5): synthesize a pre-credit segment row for
    # each in-place credit event. Without this the credited week shows
    # ONLY the post-credit segment ($134 on live data) and the bulk of
    # the week's cost (~$372 in entries before the credit moment) is
    # invisible to the user.
    #
    # _apply_reset_events_to_subweeks shifts the credited SubWeek's
    # start_ts to ``effective_reset_at_utc``, so _aggregate_weekly's
    # bucket for that SubWeek already covers ONLY the post-credit
    # interval. We rebuild the pre-credit bucket here by filtering the
    # same ``entries`` list to ``[original_start, effective)`` and
    # re-aggregating cost / tokens / per-model.
    #
    # The pre-credit row's ``used_pct`` comes from the
    # weekly_usage_snapshots row captured at-or-before the credit
    # moment (the pre-credit peak the user reached); fall back to None
    # if no snapshot was recorded before the credit fired.
    in_place_credits = conn.execute(
        "SELECT new_week_end_at, effective_reset_at_utc "
        "FROM week_reset_events "
        "WHERE old_week_end_at = effective_reset_at_utc"
    ).fetchall()
    if in_place_credits:
        _lib_pricing = sys.modules.get("_lib_pricing")
        if _lib_pricing is None:
            import importlib.util as _ilu, pathlib as _pl
            _p = _pl.Path(__file__).resolve().parent / "_lib_pricing.py"
            _spec = _ilu.spec_from_file_location("_lib_pricing", _p)
            _lib_pricing = _ilu.module_from_spec(_spec)
            sys.modules["_lib_pricing"] = _lib_pricing
            _spec.loader.exec_module(_lib_pricing)
        _calc = _lib_pricing._calculate_entry_cost

        insertions: list[tuple[int, WeeklyPeriodRow]] = []
        for ev in in_place_credits:
            try:
                eff_dt = parse_iso_datetime(
                    ev["effective_reset_at_utc"], "credit.eff"
                )
                new_end_dt = parse_iso_datetime(
                    ev["new_week_end_at"], "credit.new_end"
                )
            except ValueError:
                continue
            # Find the SubWeek whose end_ts equals new_week_end_at (the
            # post-credit segment); its start_ts has already been
            # shifted to ``effective`` by _apply_reset_events_to_subweeks.
            post_sw = None
            for w in weeks:
                try:
                    w_end = parse_iso_datetime(w.end_ts, "sw.end")
                except ValueError:
                    continue
                if w_end == new_end_dt:
                    post_sw = w
                    break
            if post_sw is None:
                continue

            # Original start instant: take the EARLIEST recorded
            # week_start_at for this week_start_date. The post-credit
            # SubWeek's start_ts is the shifted value (= effective); the
            # MIN over weekly_usage_snapshots gives us the original
            # API-derived start before the override fired.
            orig_row = conn.execute(
                "SELECT MIN(week_start_at) AS ws "
                "FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? AND week_start_at IS NOT NULL",
                (post_sw.start_date.isoformat(),),
            ).fetchone()
            if orig_row is None or orig_row["ws"] is None:
                continue
            try:
                original_start_iso = str(orig_row["ws"])
                original_start_dt = parse_iso_datetime(
                    original_start_iso, "credit.original_start"
                )
            except ValueError:
                continue
            if original_start_dt >= eff_dt:
                # No pre-credit interval to aggregate.
                continue

            # Aggregate entries in [original_start, effective).
            pre_input = pre_output = pre_cc = pre_cr = 0
            pre_cost = 0.0
            pre_models: dict[str, float] = {}
            pre_entry_count = 0
            for e in entries:
                if original_start_dt <= e.timestamp < eff_dt:
                    usage = e.usage
                    pre_input  += usage.get("input_tokens", 0)
                    pre_output += usage.get("output_tokens", 0)
                    pre_cc     += usage.get("cache_creation_input_tokens", 0)
                    pre_cr     += usage.get("cache_read_input_tokens", 0)
                    c = _calc(
                        e.model, usage, mode="auto", cost_usd=e.cost_usd,
                    )
                    pre_cost += c
                    pre_models[e.model] = pre_models.get(e.model, 0.0) + c
                    pre_entry_count += 1
            if pre_entry_count == 0 and pre_cost <= 0:
                # No measurable pre-credit activity — skip insertion.
                continue

            # Pre-credit used_pct: latest snapshot at-or-before the
            # credit moment for this week_start_date.
            pre_usage = conn.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? "
                "  AND unixepoch(captured_at_utc) <= unixepoch(?) "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
                (post_sw.start_date.isoformat(), ev["effective_reset_at_utc"]),
            ).fetchone()
            pre_used_pct: float | None = None
            if pre_usage is not None and pre_usage["weekly_percent"] is not None:
                pre_used_pct = float(pre_usage["weekly_percent"])
            pre_dpp = (
                pre_cost / pre_used_pct
                if pre_used_pct and pre_used_pct > 0 else None
            )

            pre_total = pre_input + pre_output + pre_cc + pre_cr
            pre_model_breakdowns = [
                {"modelName": m, "cost": c}
                for m, c in sorted(pre_models.items(), key=lambda kv: -kv[1])
            ]
            pre_label = original_start_dt.strftime("%m-%d")
            pre_row = WeeklyPeriodRow(
                label=pre_label,
                cost_usd=pre_cost,
                total_tokens=pre_total,
                input_tokens=pre_input,
                output_tokens=pre_output,
                cache_creation_tokens=pre_cc,
                cache_read_tokens=pre_cr,
                used_pct=pre_used_pct,
                dollar_per_pct=pre_dpp,
                delta_cost_pct=None,
                # Pre-credit segment is historical even though it
                # shares the bucket date with the live week.
                is_current=False,
                models=_model_breakdowns_to_models(
                    pre_model_breakdowns, pre_cost
                ),
                week_start_at=original_start_iso,
                week_end_at=ev["effective_reset_at_utc"],
            )

            # Find post-credit row's index and insert pre-credit BEFORE
            # it (chronological order: pre then post in oldest-first).
            post_idx = None
            for i, r in enumerate(rows_oldest_first):
                if r.week_start_at == post_sw.start_ts and r.week_end_at == post_sw.end_ts:
                    post_idx = i
                    break
            if post_idx is None:
                # The post-credit row may have been dropped by
                # _aggregate_weekly (no entries in the post-credit
                # interval) — append at the most-recent slot so the
                # pre-credit segment still surfaces.
                insertions.append((len(rows_oldest_first), pre_row))
            else:
                insertions.append((post_idx, pre_row))

        # Apply insertions in REVERSE index order so prior insertions
        # don't shift the indices of later ones.
        for idx, pre_row in sorted(insertions, key=lambda t: -t[0]):
            rows_oldest_first.insert(idx, pre_row)

    # Reverse so caller gets newest-first; compute delta_cost_pct vs the
    # immediately older row in that orientation.
    rows = list(reversed(rows_oldest_first))
    for i, r in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        if prev is not None and prev.cost_usd > 0:
            r.delta_cost_pct = (r.cost_usd - prev.cost_usd) / prev.cost_usd
    # Cap to n.
    return rows[:n]


def _build_block_detail(block: "Block",
                        entries: "list[UsageEntry]",
                        *,
                        display_tz: "ZoneInfo | None" = None) -> "dict[str, Any]":
    """Build the JSON payload for ``GET /api/block/:start_at``.

    Pure function over a single Block plus its constituent entries (already
    filtered to the block's window by the caller). All time-relative state
    (``is_active``, ``burn_rate``, ``projection``) is already encoded on
    the ``Block`` by ``_group_entries_into_blocks``, so this helper takes
    no ``now`` argument.

    Per-entry cumulative samples are computed using ``_calculate_entry_cost``
    so the chart line is the literal cost trajectory the panel sums up.
    The final cumulative MUST equal ``block.cost_usd`` within 1e-9 USD —
    this is the project's standard reconcile tolerance (see CLAUDE.md
    "Reconcile invariants"). An assert enforces it so a future pricing
    drift fails loud rather than silently misaligning the chart.
    """
    # Per-entry cumulative samples (sorted by timestamp).
    sorted_entries = sorted(entries, key=lambda e: e.timestamp)
    samples: list[dict[str, Any]] = []
    running = 0.0
    per_model_cost: dict[str, float] = {}
    for e in sorted_entries:
        c = _calculate_entry_cost(
            e.model, e.usage, mode="auto", cost_usd=e.cost_usd,
        )
        running += c
        per_model_cost[e.model] = per_model_cost.get(e.model, 0.0) + c
        samples.append({
            "t":   e.timestamp.astimezone(dt.timezone.utc).isoformat(),
            "cum": running,
        })

    # Reconcile invariant: per-entry sum equals Block.cost_usd within 1e-9.
    # Off only if pricing-dict drifted between block aggregation and now.
    assert abs(running - block.cost_usd) < 1e-9, (
        f"_build_block_detail reconcile mismatch: per-entry sum "
        f"{running!r} vs block.cost_usd {block.cost_usd!r}"
    )

    # Per-model breakdown — same shape `_dashboard_build_blocks_panel` uses.
    model_breakdowns = [
        {"modelName": name, "cost": cost}
        for name, cost in sorted(per_model_cost.items(), key=lambda kv: -kv[1])
    ]
    models = _model_breakdowns_to_models(model_breakdowns, block.cost_usd)

    # Cache-hit %: cache_read / (input + cache_creation + cache_read) * 100.
    # Same denominator as the canonical _compute_cache_hit_percent helper —
    # cache_creation contributes to the input-side token count and must be
    # in the divisor or the ratio inflates toward 100% on heavy-cache blocks.
    denom = (block.input_tokens
             + block.cache_creation_tokens
             + block.cache_read_tokens)
    cache_hit_pct = (
        (block.cache_read_tokens / denom) * 100.0 if denom > 0 else None
    )

    # Active blocks expose burn_rate / projection in snake_case (the Block
    # dataclass uses camelCase for legacy parity with ccusage CLI JSON).
    burn_rate_out: dict[str, float] | None = None
    projection_out: dict[str, Any] | None = None
    if block.burn_rate is not None:
        burn_rate_out = {
            "tokens_per_minute": block.burn_rate["tokensPerMinute"],
            "cost_per_hour":     block.burn_rate["costPerHour"],
        }
    if block.projection is not None:
        projection_out = {
            "total_tokens":      block.projection["totalTokens"],
            "total_cost_usd":    block.projection["totalCost"],
            "remaining_minutes": block.projection["remainingMinutes"],
        }

    return {
        "start_at":      block.start_time.astimezone(dt.timezone.utc).isoformat(),
        "end_at":        block.end_time.astimezone(dt.timezone.utc).isoformat(),
        "actual_end_at": (block.actual_end_time.astimezone(dt.timezone.utc).isoformat()
                          if block.actual_end_time else None),
        "anchor":   block.anchor,
        "is_active": bool(block.is_active and block.entries_count > 0),
        "label":    format_display_dt(
            block.start_time, display_tz, fmt="%H:%M %b %d", suffix=True,
        ),

        "entries_count": block.entries_count,
        "cost_usd":      block.cost_usd,
        "total_tokens":  block.total_tokens,
        "input_tokens":  block.input_tokens,
        "output_tokens": block.output_tokens,
        "cache_creation_tokens": block.cache_creation_tokens,
        "cache_read_tokens":     block.cache_read_tokens,
        "cache_hit_pct":         cache_hit_pct,

        "models":     models,
        "burn_rate":  burn_rate_out,
        "projection": projection_out,
        "samples":    samples,
    }


def _dashboard_build_blocks_panel(conn: "sqlite3.Connection",
                                   now_utc: "dt.datetime",
                                   *,
                                   week_start_at: "dt.datetime",
                                   week_end_at: "dt.datetime",
                                   skip_sync: bool = False,
                                   display_tz: "ZoneInfo | None" = None) -> "list[BlocksPanelRow]":
    """Activity blocks (`is_gap=False`) inside ``[week_start_at, week_end_at)``,
    newest-first.

    Mirrors the recorded-windows-widening trick used by ``cmd_blocks``: loads
    recorded reset windows from ``[start - BLOCK_DURATION, end + BLOCK_DURATION]``
    so a recorded reset just outside the visible window can still anchor
    blocks inside it.
    """
    # Widen the entry window slightly so a recorded-reset window straddling
    # the boundary still picks up its entries.
    fetch_start = week_start_at - BLOCK_DURATION
    fetch_end = week_end_at + BLOCK_DURATION
    entries = get_entries(fetch_start, fetch_end, skip_sync=skip_sync)
    entries = [e for e in entries if week_start_at <= e.timestamp < week_end_at]

    recorded_windows, block_start_overrides = _load_recorded_five_hour_windows(
        fetch_start, fetch_end,
    )
    blocks = _group_entries_into_blocks(
        entries, mode="auto",
        recorded_windows=recorded_windows,
        block_start_overrides=block_start_overrides,
        now=now_utc,
    )
    blocks = [b for b in blocks if not b.is_gap]
    if not blocks:
        return []

    # Build per-block model-cost breakdown (matches _model_breakdowns_to_models
    # input shape: dicts with `modelName` / `cost` keys, sorted desc by cost).
    rows: list[BlocksPanelRow] = []
    for b in blocks:
        # Reaggregate the entries inside [b.start_time, b.end_time) for
        # the model-split. _group_entries_into_blocks gives us total
        # cost_usd per block but not per-model breakdown. Use
        # `_calculate_entry_cost` (the single source-of-truth pricing
        # path) so block per-model costs reconcile exactly with the
        # block's own cost_usd.
        per_model: dict[str, float] = {}
        for e in entries:
            if b.start_time <= e.timestamp < b.end_time:
                cost = _calculate_entry_cost(
                    e.model, e.usage, mode="auto", cost_usd=e.cost_usd,
                )
                per_model[e.model] = per_model.get(e.model, 0.0) + cost
        model_breakdowns = [
            {"modelName": name, "cost": cost}
            for name, cost in sorted(per_model.items(), key=lambda kv: -kv[1])
        ]
        local_label = format_display_dt(
            b.start_time, display_tz, fmt="%H:%M %b %d", suffix=True,
        )
        rows.append(BlocksPanelRow(
            start_at=b.start_time.astimezone(dt.timezone.utc).isoformat(),
            end_at=b.end_time.astimezone(dt.timezone.utc).isoformat(),
            anchor=b.anchor,
            is_active=bool(b.is_active and b.entries_count > 0),
            cost_usd=b.cost_usd,
            models=_model_breakdowns_to_models(model_breakdowns, b.cost_usd),
            label=local_label,
        ))

    rows.sort(key=lambda r: r.start_at, reverse=True)
    return rows


def _dashboard_build_daily_panel(conn: "sqlite3.Connection",
                                  now_utc: "dt.datetime",
                                  *,
                                  n: int = 30,
                                  skip_sync: bool = False,
                                  display_tz: "ZoneInfo | None" = None) -> "list[DailyPanelRow]":
    """Latest n display-tz dates as DailyPanelRow, newest-first.

    Mirrors `_dashboard_build_monthly_periods`: walks a wide trailing
    window, runs `_aggregate_daily`, reverses to newest-first, caps to
    `n`, then computes intensity buckets in-place. Bucketing and the
    `is_today` reference both follow `display_tz` so users on a
    non-host display zone see days grouped consistently with the rest
    of the UI.
    """
    # Wide trailing window — n days of slack on either side keeps it
    # forgiving of tz boundary issues.
    range_start = now_utc - dt.timedelta(days=n + 1)
    range_end = now_utc
    entries = get_entries(range_start, range_end, skip_sync=skip_sync)
    buckets = _aggregate_daily(entries, mode="auto", tz=display_tz)
    if not buckets:
        return []

    # Materialize the full n-day calendar window so gap days render as
    # zero-cost h0 cells (and today always appears, even on idle days).
    # _aggregate_daily only emits buckets for dates with entries, so we
    # overlay them onto a contiguous newest-first range.
    buckets_by_date = {b.bucket: b for b in buckets}
    today_local = (
        now_utc.astimezone(display_tz) if display_tz is not None
        # internal fallback: host-local intentional
        else now_utc.astimezone()
    ).date()

    rows: list[DailyPanelRow] = []
    for i in range(n):
        d = today_local - dt.timedelta(days=i)
        date_str = d.isoformat()
        b = buckets_by_date.get(date_str)
        if b is not None:
            # cache_read / (input + cache_creation + cache_read) — same ratio
            # used by Block / Session / TuiSession dashboard surfaces. Do NOT
            # switch to the diff-metrics formula (cache_read / (cache_read +
            # input)) without aligning all dashboard surfaces.
            denom = b.input_tokens + b.cache_creation_tokens + b.cache_read_tokens
            cache_hit = (b.cache_read_tokens / denom * 100.0) if denom > 0 else None
            rows.append(DailyPanelRow(
                date=date_str,
                label=date_str[5:],  # YYYY-MM-DD → MM-DD
                cost_usd=b.cost_usd,
                is_today=(d == today_local),
                intensity_bucket=0,  # set by _compute_intensity_buckets below
                models=_model_breakdowns_to_models(b.model_breakdowns, b.cost_usd),
                input_tokens=b.input_tokens,
                output_tokens=b.output_tokens,
                cache_creation_tokens=b.cache_creation_tokens,
                cache_read_tokens=b.cache_read_tokens,
                total_tokens=b.total_tokens,
                cache_hit_pct=cache_hit,
            ))
        else:
            # Zero-cost gap day: tokens default to 0, cache_hit_pct to None
            # (avoids /0 and signals 'no data' cleanly to the modal tile).
            rows.append(DailyPanelRow(
                date=date_str,
                label=date_str[5:],
                cost_usd=0.0,
                is_today=(d == today_local),
                intensity_bucket=0,
                models=[],
            ))

    _compute_intensity_buckets(rows)
    return rows


def _empty_dashboard_snapshot() -> "DataSnapshot":
    """A minimal DataSnapshot used at startup before the first sync
    completes. All panels render placeholders; the sync thread replaces
    this with real data within one tick."""
    now = dt.datetime.now(dt.timezone.utc)
    return DataSnapshot(
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=now,
        percent_milestones=[],
        weekly_history=[],
        weekly_periods=[],
        monthly_periods=[],
        blocks_panel=[],
        daily_panel=[],
    )


def _iso_z(d: "dt.datetime | None") -> "str | None":
    """Serialize a UTC-aware datetime as ISO-8601 with a ``Z`` suffix.
    Returns None for None so it can be dropped straight into the JSON
    envelope."""
    if d is None:
        return None
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_current_block_for_envelope(
    conn: sqlite3.Connection,
    *,
    current_used_pct: "float | None",
    now_utc: "dt.datetime",
) -> "dict | None":
    """Select the current 5h block for the dashboard's current_week envelope.

    Selection rule (spec §4.1): pick the row from ``five_hour_blocks`` whose
    ``five_hour_window_key`` matches the latest ``weekly_usage_snapshots``
    row's ``five_hour_window_key``. Returns None when no row matches —
    binding to the latest snapshot's key (rather than highest
    ``block_start_at``) prevents the panel's "current 5h session" copy from
    surfacing stale closed blocks.

    The latest-snapshot lookup is filtered to ``captured_at_utc <=
    now_utc`` so callers that pin the clock (CCTALLY_AS_OF / --as-of) get
    a block consistent with the same pinned moment used for ``used_pct``.
    Without this filter, a past ``now_utc`` would still pick the
    absolute-newest snapshot's window and surface a future block's deltas
    against past usage — mirrors ``_handle_get_session_detail``'s
    ``snap.generated_at`` plumbing for the same reason.
    ``captured_at_utc`` is stored canonical UTC-Z (see ``now_utc_iso``),
    so a lexicographic ``<=`` compare is chronological.

    Stale-block suppression: the block lookup additionally filters on
    ``is_closed = 0`` AND ``five_hour_resets_at > now_utc``. The two
    clauses are belt-and-suspenders — ``maybe_update_five_hour_block``'s
    natural-expiration sweep (``UPDATE … SET is_closed = 1``) only fires
    on a ``record-usage`` tick, but the dashboard read can race ahead of
    the next tick when the user is idle past the reset. A block that
    matches the latest snapshot's key but whose ``five_hour_resets_at``
    has already passed is no longer current — surfacing it would render
    a stale "current 5h session" delta against post-reset weekly usage.
    Returns ``None`` in that case; the React panel falls back to the
    legacy single-big-number layout (CLAUDE.md gotcha:
    ``Dashboard current_week.five_hour_block …``).

    Returned dict has snake_case keys to match the existing dashboard
    envelope convention (``current_week`` is snake_case; CLI ``--json`` is
    camelCase — separate conventions, see CLAUDE.md).

    Delta semantics:
      - Non-crossed block: ``current_used_pct - seven_day_pct_at_block_start``
        (the natural "how much 7d% has changed during this 5h block" read).
      - Crossed block (``crossed_seven_day_reset == 1``): the block straddles
        a weekly reset, so the natural delta would be dominated by the
        reset itself (e.g. −94pp) rather than the user's actual burn-rate.
        Compute the POST-RESET delta instead — ``current_used_pct -
        weekly_percent_at_first_post_reset_snapshot_in_block``. The React
        panel prefixes this delta with ``⚡`` to show the reset crossing
        without hiding the informative number.
      - ``None`` only when ``current_used_pct`` is unknown OR the
        block-start anchor is missing AND no post-reset anchor was found.
    """
    snap = conn.execute(
        """
        SELECT five_hour_window_key, week_start_at
          FROM weekly_usage_snapshots
         WHERE captured_at_utc <= ?
         ORDER BY captured_at_utc DESC, id DESC
         LIMIT 1
        """,
        (_iso_z(now_utc),),
    ).fetchone()
    if snap is None or snap["five_hour_window_key"] is None:
        return None

    block = conn.execute(
        """
        SELECT five_hour_window_key, block_start_at, last_observed_at_utc,
               seven_day_pct_at_block_start,
               crossed_seven_day_reset
          FROM five_hour_blocks
         WHERE five_hour_window_key = ?
           AND is_closed = 0
           AND five_hour_resets_at > ?
        """,
        (snap["five_hour_window_key"], _iso_z(now_utc)),
    ).fetchone()
    if block is None:
        return None

    crossed = bool(block["crossed_seven_day_reset"])
    p_start = block["seven_day_pct_at_block_start"]

    # When the block crossed a weekly reset, recompute the delta against
    # the first post-reset snapshot inside the block instead of the
    # pre-reset block-start anchor. Use ``unixepoch()`` because
    # ``block_start_at`` is host-local-tz (``+03:00``) while
    # ``captured_at_utc`` is canonical UTC-Z; lex compares mis-order
    # mixed-offset moments. ``snap.week_start_at`` is the latest
    # (post-reset) week's anchor, so equality on that column scopes
    # the lookup to the current weekly window.
    p_anchor = p_start
    if crossed and snap["week_start_at"] is not None:
        post = conn.execute(
            """
            SELECT weekly_percent
              FROM weekly_usage_snapshots
             WHERE week_start_at = ?
               AND unixepoch(captured_at_utc) >= unixepoch(?)
               AND unixepoch(captured_at_utc) <= unixepoch(?)
             ORDER BY captured_at_utc ASC, id ASC
             LIMIT 1
            """,
            (
                snap["week_start_at"],
                block["block_start_at"],
                _iso_z(now_utc),
            ),
        ).fetchone()
        if post is not None and post["weekly_percent"] is not None:
            p_anchor = float(post["weekly_percent"])

    delta = (
        None if (p_anchor is None or current_used_pct is None)
        else round(current_used_pct - p_anchor, 9)
    )

    # Spec §5.3 — in-place credit events for this 5h block's window,
    # ascending by ``effective_reset_at_utc``. Drives the
    # ``CurrentWeekPanel.tsx`` ``⚡ credited -Xpp`` chip and the
    # ``CurrentWeekModal.tsx`` merged-stream 5h milestones section.
    # Snake_case keys to match the envelope convention (see CLAUDE.md;
    # CLI ``--json`` uses camelCase, dashboard envelope is snake_case).
    cred_rows = conn.execute(
        """
        SELECT effective_reset_at_utc, prior_percent, post_percent
          FROM five_hour_reset_events
         WHERE five_hour_window_key = ?
         ORDER BY effective_reset_at_utc ASC
        """,
        (int(block["five_hour_window_key"]),),
    ).fetchall()
    credits = [
        {
            "effective_reset_at_utc": c["effective_reset_at_utc"],
            "prior_percent": float(c["prior_percent"]),
            "post_percent": float(c["post_percent"]),
            "delta_pp": round(
                float(c["post_percent"]) - float(c["prior_percent"]), 1
            ),
        }
        for c in cred_rows
    ]

    return {
        "block_start_at":               block["block_start_at"],
        "five_hour_window_key":         int(block["five_hour_window_key"]),
        "seven_day_pct_at_block_start": p_start,
        "seven_day_pct_delta_pp":       delta,
        "crossed_seven_day_reset":      crossed,
        "credits":                      credits,
    }


def _build_alerts_envelope_array(
    conn: sqlite3.Connection,
    limit: int = 100,
) -> list[dict]:
    """Return the ``alerts`` array for the SSE snapshot envelope.

    Union of ``percent_milestones`` and ``five_hour_milestones`` rows
    with ``alerted_at IS NOT NULL``, ordered newest-first by
    ``alerted_at``, capped at ``limit`` (default 100). Single source of
    truth for both the dashboard panel (slices to 10 client-side) and
    the modal (renders all 100). Forward-only semantics: only rows the
    alert-dispatch path stamped get included; pre-deploy crossings stay
    NULL and are intentionally invisible (spec §4.3).

    Both axes share the same envelope schema; the ``axis`` field
    discriminates.

    Per-axis ``LIMIT`` is applied at the SQL level (each query may yield
    up to ``limit``) and the union is re-sorted + sliced — important for
    the boundary case where one axis has ``limit`` rows and the other
    has more recent ones that would otherwise be dropped before the
    final sort.
    """
    out: list[dict] = []
    # ``reset_event_id`` (v1.7.2) segments the same (week, threshold)
    # across pre-credit (0) and post-credit (event.id) cohorts, both
    # of which can be alerted. The envelope id must include the
    # segment so React's <li key={a.id}> / <tr key={a.id}> doesn't
    # collide on the duplicate (week, threshold) pair. Older clients
    # tolerate longer ids — the id is opaque to them; only the React
    # key uniqueness invariant matters.
    weekly_rows = conn.execute(
        """
        SELECT week_start_date, percent_threshold, captured_at_utc,
               alerted_at, cumulative_cost_usd, reset_event_id
        FROM percent_milestones
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for r in weekly_rows:
        threshold = int(r["percent_threshold"])
        cumulative = float(r["cumulative_cost_usd"])
        dpp = (cumulative / threshold) if threshold else None
        out.append({
            "id": f"weekly:{r['week_start_date']}:{threshold}:{r['reset_event_id']}",
            "axis": "weekly",
            "threshold": threshold,
            "crossed_at": r["captured_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_date":     r["week_start_date"],
                "cumulative_cost_usd": cumulative,
                "dollars_per_percent": dpp,
                # Round-3: parallel to the 5h context block below — both
                # axes now expose ``reset_event_id`` so downstream
                # clients (panel, modal, third-party consumers) can
                # discriminate pre- vs post-credit crossings of the
                # same (week, threshold) without scraping the
                # envelope ``id`` string. 0 = pre-credit / no-event;
                # event.id = post-credit segment.
                "reset_event_id":      int(r["reset_event_id"]),
            },
        })

    # Site F (spec §3.2 bucket C / §3.3): widen the row identity to
    # include ``reset_event_id`` so post-credit (seg=event.id) crossings
    # of the same (window_key, threshold) don't collide with pre-credit
    # (seg=0) crossings on the React row key. Older clients tolerate
    # longer ids — the id is opaque to them; only the React key
    # uniqueness invariant matters. Mirrors the weekly precedent at
    # line ~2597.
    fh_rows = conn.execute(
        """
        SELECT m.five_hour_window_key, m.percent_threshold, m.captured_at_utc,
               m.alerted_at, m.block_cost_usd, m.reset_event_id,
               b.block_start_at
        FROM five_hour_milestones m
        LEFT JOIN five_hour_blocks b ON b.five_hour_window_key = m.five_hour_window_key
        WHERE m.alerted_at IS NOT NULL
        ORDER BY m.alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for r in fh_rows:
        threshold = int(r["percent_threshold"])
        out.append({
            "id":          (
                f"five_hour:{int(r['five_hour_window_key'])}:"
                f"{threshold}:{int(r['reset_event_id'])}"
            ),
            "axis":        "five_hour",
            "threshold":   threshold,
            "crossed_at":  r["captured_at_utc"],
            "alerted_at":  r["alerted_at"],
            "context": {
                "five_hour_window_key": int(r["five_hour_window_key"]),
                "block_start_at":       r["block_start_at"] or "",
                "block_cost_usd":       float(r["block_cost_usd"] or 0.0),
                "reset_event_id":       int(r["reset_event_id"]),
            },
        })

    # Python's list.sort is stable. When two alerts share the same
    # `alerted_at` ISO string (rare; both axes firing within the same
    # millisecond), the union order (weekly first, then 5h) determines
    # the tiebreaker — no extra deterministic key is added because the
    # spec doesn't require one.
    out.sort(key=lambda a: a["alerted_at"], reverse=True)
    return out[:limit]


def snapshot_to_envelope(snap: "DataSnapshot", *,
                         now_utc: "dt.datetime",
                         monotonic_now: "float | None" = None,
                         oauth_usage_cfg: "dict | None" = None,
                         display_tz_pref_override: "str | None" = None,
                         runtime_bind: "str | None" = None) -> dict:
    """Serialize a DataSnapshot into the JSON envelope consumed by the
    browser (design spec §2.2).

    Pure function — no I/O on the snapshot data path. Reads
    ``config.json`` once for ``display.tz`` and once for the
    ``alerts_settings`` mirror (cheap atomic-rename reads); the
    actual ``alerts`` array comes from the precomputed
    ``snap.alerts`` already populated by the sync thread, so
    rendering never touches the DB.

    ``now_utc`` is used for wall-clock age computations (``reset_in_sec``,
    ``last_snapshot_age_sec``, etc.). ``monotonic_now`` is the caller's
    ``time.monotonic()`` snapshot, used only to compute ``sync_age_s``
    against ``snap.last_sync_at`` (a monotonic-clock reading on the real
    DataSnapshot). Pass ``None`` when no sync has happened yet.

    ``oauth_usage_cfg`` is the resolved oauth_usage block (from
    ``_get_oauth_usage_config(load_config())``) used for freshness-label
    thresholds. Callers MUST pass a pre-resolved cfg to keep this
    function pure (no per-tick FS reads on the dashboard hot path);
    when ``None``, falls back to ``_OAUTH_USAGE_DEFAULTS`` directly so
    pure-construction tests don't need to plumb config.

    Panels that have no data serialize as None so the JS client can
    render placeholders without special-casing.

    Field mapping (envelope key → DataSnapshot attribute):
      * ``current_week.*``  ← ``TuiCurrentWeek`` (dollars_per_percent,
        five_hour_resets_at, latest_snapshot_at, spent_usd, used_pct,
        five_hour_pct). ``week_label`` and ``reset_at_utc`` are
        synthesized from ``week_start_at`` / ``week_end_at``.
      * ``forecast.*``      ← ``ForecastOutput`` (inputs.confidence,
        inputs.dollars_per_percent, r_avg / r_recent, inputs.p_now /
        remaining_hours, budgets[]). ``week_avg_projection_pct`` and
        ``recent_24h_projection_pct`` are computed per-method from their
        source rates so labels stay correct on decelerating weeks
        (r_recent < r_avg); ``recent_24h`` is omitted when ``r_recent``
        is None or the two projections match. ``budget_100_per_day_usd``
        / ``budget_90_per_day_usd`` pick the matching
        ``BudgetRow.dollars_per_day``.
      * ``trend.weeks[]``   ← ``snap.trend`` (the 8-week panel dataset,
        ``TuiTrendRow``: week_label, used_pct, dollars_per_percent,
        delta_dpp, is_current, spark_height). Preserves the envelope key
        names (``label``, ``dollar_per_pct``, ``delta``) that the JS
        consumes. Do NOT swap this for ``snap.weekly_history`` — that
        field holds up to 12 rows for the modal (``trend.history[]``
        below) and would overflow the panel's hard-coded "(8 weeks)"
        header if used for ``weeks[]``.
      * ``trend.history[]`` ← ``snap.weekly_history`` (up to 12 rows,
        same TuiTrendRow shape as ``weeks[]``). v2-only: consumed by
        the Trend detail modal. Sibling to ``weeks[]``, not a
        replacement.
      * ``sessions.rows[]`` ← ``TuiSessionRow`` (session_id, started_at,
        duration_minutes, model_primary, project_label, cost_usd).
    """
    cw = snap.current_week
    fc = snap.forecast

    # F1 fix: server-resolve the display tz to a CONCRETE IANA name and
    # surface it on the envelope so the browser never has to guess "local".
    # Reused below for week_lbl / blocks / monthly label rendering so the
    # whole envelope speaks one zone consistently.
    # F3 fix: when the dashboard was started with `--tz <X>`, the override
    # supersedes the persisted config.display.tz for the lifetime of the
    # process. The override flows in as a canonical tz token; we layer it
    # onto the config dict via _apply_display_tz_override before resolving
    # so every reader downstream sees one zone.
    config = _apply_display_tz_override(
        load_config(), display_tz_pref_override
    )
    display_block = _compute_display_block(config, snap.generated_at)
    resolved_tz_obj = ZoneInfo(display_block["resolved_tz"])

    if snap.last_sync_at is not None and monotonic_now is not None:
        sync_age_s = max(0, int(monotonic_now - snap.last_sync_at))
    else:
        sync_age_s = None

    # Header is a thin projection of the other panels — the JS can read
    # from current_week / forecast directly, but pre-composing it lets
    # tools like `curl` read a self-contained envelope.
    used_pct = getattr(cw, "used_pct", None) if cw is not None else None
    five_hr  = getattr(cw, "five_hour_pct", None) if cw is not None else None
    dollar_pp = getattr(cw, "dollars_per_percent", None) if cw is not None else None

    # Forecast fields: route each projection by method identity from
    # r_avg / r_recent and inputs.{p_now, remaining_hours}. Don't use
    # final_percent_{low,high} directly — those are numerical min/max of
    # the two methods, which swaps the labels on decelerating weeks
    # (r_recent < r_avg). Map defensively via getattr — the JS only
    # needs the values, not the internal structure.
    fcast_pct: "float | None" = None
    recent_24h_pct: "float | None" = None
    verdict: "str | None" = None
    confidence: "str | None" = None
    budget_100: "float | None" = None
    budget_90: "float | None" = None
    if fc is not None:
        inputs = getattr(fc, "inputs", None)
        if inputs is not None:
            confidence = getattr(inputs, "confidence", None)
        r_avg = getattr(fc, "r_avg", None)
        r_recent = getattr(fc, "r_recent", None)
        p_now = getattr(inputs, "p_now", None) if inputs is not None else None
        rem_hrs = getattr(inputs, "remaining_hours", None) if inputs is not None else None
        if p_now is not None and rem_hrs is not None and r_avg is not None:
            fcast_pct = p_now + r_avg * rem_hrs
        if (p_now is not None and rem_hrs is not None
                and r_recent is not None):
            p_final_recent = p_now + r_recent * rem_hrs
            # Only emit recent_24h when the two projections diverge — if
            # r_recent equals r_avg the second method added no info.
            if fcast_pct is None or p_final_recent != fcast_pct:
                recent_24h_pct = p_final_recent
        # Verdict — simple mapping: "cap" if projected_cap, else "ok".
        if getattr(fc, "already_capped", False):
            verdict = "capped"
        elif getattr(fc, "projected_cap", False):
            verdict = "cap"
        else:
            verdict = "ok"
        for b in getattr(fc, "budgets", []) or []:
            tp = getattr(b, "target_percent", None)
            dpd = getattr(b, "dollars_per_day", None)
            if tp == 100:
                budget_100 = dpd
            elif tp == 90:
                budget_90 = dpd

    # Freshness envelope for current_week — derived from
    # cw.latest_snapshot_at (a datetime, not an ISO string). None when
    # cw is absent or has no snapshot timestamp; the React client renders
    # a placeholder in that case. Refs spec §3.4.
    cw_freshness: "dict | None" = None
    if cw is not None and cw.latest_snapshot_at is not None:
        captured = cw.latest_snapshot_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=dt.timezone.utc)
        age_s = max(0.0, (now_utc - captured).total_seconds())
        _fresh_cfg = (
            oauth_usage_cfg if oauth_usage_cfg is not None
            else sys.modules["cctally"]._OAUTH_USAGE_DEFAULTS
        )
        cw_freshness = {
            "label":        _freshness_label(age_s, _fresh_cfg),
            "captured_at":  _iso_z(captured),
            "age_seconds":  int(age_s),
        }

    week_lbl: "str | None" = None
    reset_at_utc: "dt.datetime | None" = None
    if cw is not None:
        ws = getattr(cw, "week_start_at", None)
        we = getattr(cw, "week_end_at", None)
        # Full window "Apr 13–20" so the dashboard matches the TUI/report
        # headers and doesn't hide month crossings (e.g. "Apr 27–May 03").
        # En-dash U+2013 matches the TUI format at cmd_tui.
        if ws is not None and we is not None:
            week_lbl = (
                f"{format_display_dt(ws, resolved_tz_obj, fmt='%b %d', suffix=False)}"
                f"–"
                f"{format_display_dt(we, resolved_tz_obj, fmt='%b %d', suffix=False)}"
            )
        elif ws is not None:
            week_lbl = format_display_dt(ws, resolved_tz_obj, fmt='%b %d', suffix=False)
        reset_at_utc = we

    # Header forecast_pct should match the projection that drove the
    # verdict pill next to it. When the verdict warns (projected to
    # cap or already capped) and the recent-24h projection is higher
    # than the week-average path, surface the pessimistic value so
    # the number and the pill tell the same story. The Forecast panel
    # still exposes both `week_avg_projection_pct` and
    # `recent_24h_projection_pct` unchanged.
    header_fcast_pct = fcast_pct
    if (
        verdict in ("cap", "capped")
        and recent_24h_pct is not None
        and fcast_pct is not None
        and recent_24h_pct > fcast_pct
    ):
        header_fcast_pct = recent_24h_pct

    # ---- weekly / monthly periods ---------------------------------
    def _weekly_row_to_dict(r: "WeeklyPeriodRow") -> dict:
        return {
            "label":                  r.label,
            "cost_usd":               r.cost_usd,
            "total_tokens":           r.total_tokens,
            "input_tokens":           r.input_tokens,
            "output_tokens":          r.output_tokens,
            "cache_creation_tokens":  r.cache_creation_tokens,
            "cache_read_tokens":      r.cache_read_tokens,
            "used_pct":               r.used_pct,
            "dollar_per_pct":         r.dollar_per_pct,
            "delta_cost_pct":         r.delta_cost_pct,
            "is_current":             r.is_current,
            "models":                 list(r.models),
            "week_start_at":          r.week_start_at,
            "week_end_at":            r.week_end_at,
        }

    def _monthly_row_to_dict(r: "MonthlyPeriodRow") -> dict:
        return {
            "label":                  r.label,
            "cost_usd":               r.cost_usd,
            "total_tokens":           r.total_tokens,
            "input_tokens":           r.input_tokens,
            "output_tokens":          r.output_tokens,
            "cache_creation_tokens":  r.cache_creation_tokens,
            "cache_read_tokens":      r.cache_read_tokens,
            "delta_cost_pct":         r.delta_cost_pct,
            "is_current":             r.is_current,
            "models":                 list(r.models),
        }

    def _blocks_row_to_dict(r: "BlocksPanelRow") -> dict:
        return {
            "start_at":  r.start_at,
            "end_at":    r.end_at,
            "anchor":    r.anchor,
            "is_active": r.is_active,
            "cost_usd":  r.cost_usd,
            "models":    list(r.models),
            "label":     r.label,
        }

    def _daily_row_to_dict(r: "DailyPanelRow") -> dict:
        return {
            "date":             r.date,
            "label":            r.label,
            "cost_usd":         r.cost_usd,
            "is_today":         r.is_today,
            "intensity_bucket": r.intensity_bucket,
            "models":           list(r.models),
            # ---- v2.3 additions ----
            "input_tokens":          r.input_tokens,
            "output_tokens":         r.output_tokens,
            "cache_creation_tokens": r.cache_creation_tokens,
            "cache_read_tokens":     r.cache_read_tokens,
            "total_tokens":          r.total_tokens,
            "cache_hit_pct":         r.cache_hit_pct,
        }

    # Spec §2.7: empty state is `weekly.rows === []`, not `weekly === null`.
    # Always emit a `{rows: [...]}` envelope (possibly empty) so the panel
    # can distinguish "loading / not synced yet" (null parent) from
    # "synced + no data" (empty rows). For Weekly/Monthly, sync always
    # builds the rows list (even if empty), so we always emit the object.
    weekly_env = {"rows": [_weekly_row_to_dict(r) for r in snap.weekly_periods]}
    monthly_env = {"rows": [_monthly_row_to_dict(r) for r in snap.monthly_periods]}

    blocks_env = {"rows": [_blocks_row_to_dict(r) for r in snap.blocks_panel]}

    # Re-run helper to derive thresholds; mutates rows[*].intensity_bucket
    # (no-op for builder-constructed rows since values match cost_usd).
    # Single-source-of-truth with bucket assignment — do NOT re-derive
    # thresholds with an independent formula.
    daily_rows = list(snap.daily_panel)
    daily_thresholds = _compute_intensity_buckets(daily_rows)
    daily_peak = None
    if daily_rows:
        nonzero_rows = [r for r in daily_rows if r.cost_usd > 0]
        if nonzero_rows:
            peak_row = max(nonzero_rows, key=lambda r: r.cost_usd)
            daily_peak = {"date": peak_row.date, "cost_usd": peak_row.cost_usd}
    daily_env = {
        "rows":                [_daily_row_to_dict(r) for r in daily_rows],
        "quantile_thresholds": daily_thresholds,
        "peak":                daily_peak,
    }

    # ---- threshold-actions T5: alerts envelope + settings mirror ----
    # `alerts` is precomputed at sync-thread snapshot-build time (see
    # `_tui_build_snapshot`'s `_build_alerts_envelope_array(conn)` call)
    # so this remains a pure render path (no DB I/O on dashboard hot
    # path). Single source of truth for both the dashboard panel
    # (slices to 10 client-side) and the modal (renders all 100).
    #
    # `alerts_settings` mirrors the validated alerts config block into
    # the envelope so the dashboard's SettingsOverlay can seed its
    # fieldset without a separate GET /api/settings round-trip
    # (parallels how `display.tz` lives at envelope["display"]["tz"]).
    # Defensive: a corrupt alerts block in `config.json` must not 500
    # the entire snapshot — fall back to safe defaults and rely on
    # `_warn_alerts_bad_config_once` for the user-visible signal.
    alerts_array = list(getattr(snap, "alerts", []) or [])
    try:
        _alerts_cfg = _get_alerts_config(load_config())
    except sys.modules["cctally"]._AlertsConfigError as exc:
        _warn_alerts_bad_config_once(exc)
        _alerts_cfg = {
            "enabled": False,
            "weekly_thresholds": [],
            "five_hour_thresholds": [],
        }
    alerts_settings = {
        "enabled":              _alerts_cfg["enabled"],
        "weekly_thresholds":    list(_alerts_cfg["weekly_thresholds"]),
        "five_hour_thresholds": list(_alerts_cfg["five_hour_thresholds"]),
    }

    # Mirror update-state.json + update-suppress.json into the envelope
    # so the dashboard's amber "Update available" badge repaints from
    # the SSE channel rather than requiring a /api/update/status fetch
    # per check. Without this mirror, a long-open dashboard tab never
    # learns that the dashboard's `_DashboardUpdateCheckThread` wrote
    # a fresher latest_version (24h-TTL by default) — the badge stayed
    # hidden until manual reload. Same precedent as `alerts_settings`:
    # cheap atomic-rename reads on the snapshot hot path. Failures
    # produce a `null` block so a missing/corrupt state file doesn't
    # 500 the entire envelope; the client falls back to the defensive
    # null-state shape `coerceUpdateState` already understands.
    try:
        _update_state_envelope = _load_update_state()
    except sys.modules["cctally"].UpdateError:
        # _load_update_state() raises on truly malformed JSON. Surface
        # an _error sentinel so the client renders "no update info" the
        # same way it does for unreachable /api/update/status.
        _update_state_envelope = {"_error": "update-state.json invalid"}
    except Exception:
        _update_state_envelope = {"_error": "update-state.json read failed"}
    try:
        _update_suppress_envelope = _load_update_suppress()
    except Exception:
        _update_suppress_envelope = {"skipped_versions": [], "remind_after": None}
    update_envelope = {
        "state":    _update_state_envelope,
        "suppress": _update_suppress_envelope,
    }

    # Doctor aggregate block (spec §5.5). Only the small severity tree
    # rides on every SSE tick (~120 bytes); the full per-check payload
    # is fetched lazily via `GET /api/doctor`. `runtime_bind` is the
    # actual host the dashboard process is bound to (Codex H4) so
    # `safety.dashboard_bind` reflects the running state, not just
    # `config.json`. Defensive: never crash the snapshot pipeline on
    # a doctor failure — surface a synthetic FAIL block with `_error`.
    try:
        _ld = sys.modules["cctally"]._load_sibling("_lib_doctor")
        _doc_state = doctor_gather_state(now_utc=now_utc, runtime_bind=runtime_bind)
        _doc_report = _ld.run_checks(_doc_state)
        doctor_envelope: "dict" = {
            "severity":     _doc_report.overall_severity,
            "counts":       dict(_doc_report.counts),
            "generated_at": _ld._iso_z(_doc_report.generated_at),
            "fingerprint":  _ld.fingerprint(_doc_report),
        }
    except Exception as exc:  # noqa: BLE001 — never crash SSE on doctor failure
        doctor_envelope = {
            "severity":     "fail",
            "counts":       {"ok": 0, "warn": 0, "fail": 1},
            "generated_at": _iso_z(now_utc),
            "fingerprint":  "sha1:" + ("0" * 40),
            "_error":       f"{type(exc).__name__}: {exc}",
        }

    return {
        "envelope_version": 2,
        "generated_at":     _iso_z(snap.generated_at),
        # last_sync_at in DataSnapshot is a monotonic float, not a wall
        # clock — the envelope's wall-clock moment is unknowable from
        # here, so expose it as None and rely on sync_age_s for the UI.
        "last_sync_at":    None,
        "sync_age_s":      sync_age_s,
        "last_sync_error": snap.last_sync_error,

        # F1 (server-resolves "local" → IANA): the browser never has to
        # guess. {tz, resolved_tz, offset_label, offset_seconds} computed
        # at the snapshot's `generated_at`. Always present, even when
        # cw / fc are None.
        "display":         display_block,

        "header": {
            "week_label":         week_lbl,
            "used_pct":           used_pct,
            "five_hour_pct":      five_hr,
            "dollar_per_pct":     dollar_pp,
            "forecast_pct":       header_fcast_pct,
            "forecast_verdict":   verdict,
            "vs_last_week_delta": None,   # populated if/when trend comparison lands
        },

        "current_week":
            None if cw is None else {
                "used_pct":                 cw.used_pct,
                "five_hour_pct":            cw.five_hour_pct,
                "five_hour_resets_in_sec":
                    None if cw.five_hour_resets_at is None
                    else max(0, int((cw.five_hour_resets_at - now_utc).total_seconds())),
                "spent_usd":                cw.spent_usd,
                "dollar_per_pct":           cw.dollars_per_percent,
                "reset_at_utc":             _iso_z(reset_at_utc),
                "reset_in_sec":
                    None if reset_at_utc is None
                    else max(0, int((reset_at_utc - now_utc).total_seconds())),
                "last_snapshot_age_sec":
                    None if cw.latest_snapshot_at is None
                    else max(0, int((now_utc - cw.latest_snapshot_at).total_seconds())),
                "freshness":                cw_freshness,
                # Current 5h block (spec §4.1) — populated upstream by
                # `_tui_build_current_week`. `getattr` with default keeps
                # legacy fixture modules that construct TuiCurrentWeek
                # directly (without the new field) compatible.
                "five_hour_block":          getattr(cw, "five_hour_block", None),
                "milestones": [
                    {
                        "percent":                m.percent,
                        "crossed_at_utc":         _iso_z(m.crossed_at),
                        "cumulative_usd":         round(m.cumulative_cost_usd, 4),
                        "marginal_usd":
                            None if m.marginal_cost_usd is None
                            else round(m.marginal_cost_usd, 4),
                        "five_hour_pct_at_cross": m.five_hour_pct_at_crossing,
                    }
                    for m in (snap.percent_milestones or [])
                ],
                # Spec §5.3 (Codex r1 finding 3) — NEW envelope key
                # parallel to ``milestones`` (which carries the WEEKLY
                # timeline). 5h-block milestones for the active block,
                # in capture-time order, both pre- and post-credit
                # segments included (bucket B per §3.2 — no
                # ``reset_event_id`` filter; the React layer renders
                # repeated thresholds as distinct rows keyed on
                # ``reset_event_id``). Empty list when no 5h block is
                # bound or the data source crashed during sync
                # (recorded on ``last_sync_error``).
                "five_hour_milestones":     getattr(snap, "five_hour_milestones", []) or [],
            },

        "forecast":
            None if fc is None else {
                "verdict":                     verdict,
                "week_avg_projection_pct":     fcast_pct,
                "recent_24h_projection_pct":   recent_24h_pct,
                "budget_100_per_day_usd":      budget_100,
                "budget_90_per_day_usd":       budget_90,
                "confidence":                  confidence or "unknown",
                # Map the binary ForecastOutput.confidence ("high" / "low") to
                # a 7-dot fill scale for the dashboard footer. ForecastOutput
                # has no numeric grade, so we pick a visually meaningful floor
                # for "low" (≈30% filled) and a full bar for "high".
                "confidence_score":            (7 if confidence == "high"
                                                else 2 if confidence == "low"
                                                else 0),
                "explain":                     _build_forecast_json_payload(fc),
            },

        "trend":
            None if not snap.trend else {
                "weeks": [
                    {
                        "label":          w.week_label,
                        "used_pct":       w.used_pct,
                        "dollar_per_pct": w.dollars_per_percent,
                        "delta":          w.delta_dpp,
                        "is_current":     bool(w.is_current),
                    }
                    for w in snap.trend
                ],
                "spark_heights": [w.spark_height for w in snap.trend],
                "history": [
                    {
                        "label":          w.week_label,
                        "used_pct":       w.used_pct,
                        "dollar_per_pct": w.dollars_per_percent,
                        "delta":          w.delta_dpp,
                        "is_current":     bool(w.is_current),
                    }
                    for w in (snap.weekly_history or [])
                ],
            },

        "weekly":  weekly_env,
        "monthly": monthly_env,
        "blocks":  blocks_env,
        "daily":   daily_env,

        "sessions": {
            "total":    len(snap.sessions),
            "sort_key": "started_desc",
            "rows": [
                {
                    "session_id":   s.session_id,
                    "started_utc":  _iso_z(s.started_at),
                    "duration_min": int(round(s.duration_minutes)) if s.duration_minutes else 0,
                    "model":        s.model_primary,
                    "project":      s.project_label,
                    "cost_usd":     round(s.cost_usd, 4) if s.cost_usd is not None else None,
                }
                for s in snap.sessions
            ],
        },

        # threshold-actions T5: see prelude above for rationale.
        "alerts":           alerts_array,
        "alerts_settings":  alerts_settings,

        # update-subcommand SSE mirror (see comment above the
        # `_load_update_state()` block). Shape matches GET
        # /api/update/status's payload (`{state, suppress}`) so the
        # dashboard client's existing coerceUpdateState/Suppress logic
        # consumes both surfaces uniformly.
        "update":           update_envelope,

        # Doctor aggregate-only block (spec §5.5). Full per-check
        # report fetched lazily via GET /api/doctor.
        "doctor":           doctor_envelope,
    }


def _session_detail_to_envelope(detail: "TuiSessionDetail") -> dict:
    """Serialize TuiSessionDetail for GET /api/session/:id (spec §3.2, §4.6.4).

    Field names stay parallel to the live ``sessions.rows[]`` envelope
    (``session_id``, ``started_utc``, ``cost_usd``) so the JS client can
    treat this as an enrichment of a session row. ``models`` is flattened
    from ``list[tuple[str, str]]`` to ``list[{name, role}]`` for JSON
    palatability. ``cost_per_model`` is similarly flattened.

    Pure function — no I/O, no clocks. Dataclass fields mirror exactly
    (``started_at`` → ``started_utc``, ``last_activity_at`` →
    ``last_activity_utc``, ``duration_minutes`` → ``duration_min`` as an
    int for parity with sessions.rows[]). There is no per-entry array —
    the underlying aggregator is session-level (spec §4.6.4).
    """
    return {
        "session_id":            detail.session_id,
        "started_utc":           _iso_z(detail.started_at),
        "last_activity_utc":     _iso_z(detail.last_activity_at),
        "duration_min":          int(round(detail.duration_minutes)) if detail.duration_minutes else 0,
        "project_label":         detail.project_label,
        "project_path":          detail.project_path,
        "source_paths":          list(detail.source_paths),
        "models": [
            {"name": name, "role": role}
            for (name, role) in detail.models
        ],
        "input_tokens":          detail.input_tokens,
        "cache_creation_tokens": detail.cache_creation_tokens,
        "cache_read_tokens":     detail.cache_read_tokens,
        "output_tokens":         detail.output_tokens,
        "cache_hit_pct":         detail.cache_hit_pct,
        "cost_per_model": [
            {"model": name, "cost_usd": round(cost, 4)}
            for (name, cost) in detail.cost_per_model
        ],
        "cost_total_usd":        round(detail.cost_total_usd, 4),
    }


# Bounded wait for /api/sync's lock acquisition. The periodic background
# sync thread holds sync_lock during sync_cache + snapshot build (often
# 100-1500ms under active CC sessions); a non-blocking try_acquire would
# 503 the user's click whenever it lands inside that window, silently
# dropping their refresh-usage intent. 2s is generous enough to span a
# normal periodic tick yet short enough to surface a stuck rebuild as
# 503 instead of hanging the request indefinitely.
_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS = 2.0


# === DashboardHTTPHandler (the /api/* + static surface) ===================
# Pre-extract location: bin/cctally L17694.


class DashboardHTTPHandler(BaseHTTPRequestHandler):
    """Routes:
        GET /                       → dashboard.html
        GET /static/<path>          → file in STATIC_DIR
        GET /api/data               → one-shot JSON snapshot   (Task 4)
        GET /api/events             → SSE stream                (Task 5)
        GET /api/session/:id        → per-session detail JSON   (v2)
        GET /api/block/:start_at    → per-block detail JSON     (v3)
        GET /api/doctor             → full doctor report JSON   (spec §5.6)
        POST /api/sync              → trigger a sync tick       (v2)
    Anything else → 404.

    Class attributes set by cmd_dashboard before serve_forever():
        hub            : SSEHub
        snapshot_ref   : _SnapshotRef
        static_dir     : pathlib.Path
        sync_lock      : threading.Lock  (v2)
        run_sync_now   : () -> None      (v2, staticmethod)
    """

    hub: "SSEHub" = None                     # type: ignore[assignment]
    snapshot_ref: "_SnapshotRef" = None       # type: ignore[assignment]
    static_dir: pathlib.Path = STATIC_DIR
    sync_lock: "threading.Lock" = None        # type: ignore[assignment]
    run_sync_now: "staticmethod" = None        # type: ignore[assignment]
    # Mirrors cmd_dashboard's --no-sync arg so request handlers respect
    # the same intent as the panel build path. Default False keeps
    # detail-handler behavior symmetric with the panel under both modes.
    no_sync: bool = False
    # F3: canonical tz token from `--tz` (or None). When non-None, every
    # request handler that builds an envelope or resolves the display zone
    # routes through `_apply_display_tz_override` so the override wins
    # over `config.display.tz`. Set by cmd_dashboard before serve_forever.
    display_tz_pref_override: "str | None" = None
    # Doctor (spec §5.5, §7.4): the host the dashboard process is
    # ACTUALLY bound to (`args.host`). Threaded into `doctor_gather_state`
    # so `safety.dashboard_bind` reflects the running state — the CLI
    # path leaves this None and only sees the `config.json` view, which
    # is the Codex H4 finding. Set by cmd_dashboard before serve_forever.
    cctally_host: "str | None" = None

    # Silence the default per-request access log — noisy in the parent
    # terminal, and we pipe it through our own logger in cmd_dashboard.
    def log_message(self, fmt: str, *args) -> None:
        pass

    def _method_not_allowed_for_settings(self) -> bool:
        """If the request targets /api/settings, send 405 and return True.

        Per spec §error-matrix and RFC 9110 §15.5.6: non-POST requests to
        /api/settings must return 405 Method Not Allowed with an Allow
        header listing the supported methods. Using send_error would
        lose the Allow header, so we hand-roll the response via
        _respond_json plus an explicit Allow header.
        """
        if self.path.split("?", 1)[0] == "/api/settings":
            encoded = json.dumps({"error": "method not allowed"}).encode("utf-8")
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self._method_not_allowed_for_settings():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_static_file(self.static_dir / "dashboard.html",
                                    "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            # Defense in depth: a lexical ".." pre-check is bypassable via
            # percent-encoding (e.g., %2e%2e), so the authoritative guard is the
            # resolve() + relative_to() containment check below. The pre-check
            # stays as a fast rejection for plain-text ".." and absolute paths.
            rel = path[len("/static/"):]
            if ".." in rel.split("/") or rel.startswith("/"):
                self.send_error(400, "bad path")
                return
            candidate = (self.static_dir / rel).resolve()
            try:
                candidate.relative_to(self.static_dir.resolve())
            except ValueError:
                self.send_error(403, "forbidden")
                return
            ctype = self._content_type_for(candidate)
            self._serve_static_file(candidate, ctype)
        elif path == "/api/data":
            self._serve_api_data()
        elif path == "/api/events":
            self._serve_api_events()
        elif path.startswith("/api/session/"):
            self._handle_get_session_detail(path)
        elif path.startswith("/api/block/"):
            self._handle_get_block_detail(path)
        elif path == "/api/update/status":
            self._handle_get_update_status()
        elif path.startswith("/api/update/stream/"):
            self._handle_get_update_stream(path)
        elif path == "/api/share/templates":
            self._handle_share_templates_get()
        elif path == "/api/share/presets":
            self._handle_share_presets_get()
        elif path == "/api/share/history":
            self._handle_share_history_get()
        elif path == "/api/doctor":
            self._handle_get_doctor()
        else:
            self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 — stdlib API
        path = self.path.split("?", 1)[0]
        if path == "/api/sync":
            self._handle_post_sync()
        elif path == "/api/settings":
            self._handle_post_settings()
        elif path == "/api/alerts/test":
            self._handle_post_alerts_test()
        elif path == "/api/update":
            self._handle_post_update()
        elif path == "/api/update/dismiss":
            self._handle_post_update_dismiss()
        elif path == "/api/share/render":
            self._handle_share_render_post()
        elif path == "/api/share/compose":
            self._handle_share_compose_post()
        elif path == "/api/share/presets":
            self._handle_share_presets_post()
        elif path == "/api/share/history":
            self._handle_share_history_post()
        else:
            self.send_error(404, "not found")

    # --- 405 overrides for /api/settings -------------------------------
    # BaseHTTPRequestHandler answers unknown methods with 501 Not
    # Implemented. Spec §error-matrix mandates 405 Method Not Allowed
    # (with Allow: POST) for non-POST requests against /api/settings.
    # For other paths we fall through to send_error(501) so the rest of
    # the surface keeps stdlib semantics.
    def do_PUT(self) -> None:  # noqa: N802 — stdlib API
        if self._method_not_allowed_for_settings():
            return
        self.send_error(501, "Unsupported method ('PUT')")

    def do_DELETE(self) -> None:  # noqa: N802 — stdlib API
        if self._method_not_allowed_for_settings():
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/share/presets/"):
            self._handle_share_presets_delete()
            return
        if path == "/api/share/history":
            self._handle_share_history_delete()
            return
        self.send_error(501, "Unsupported method ('DELETE')")

    def do_PATCH(self) -> None:  # noqa: N802 — stdlib API
        if self._method_not_allowed_for_settings():
            return
        self.send_error(501, "Unsupported method ('PATCH')")

    def _handle_post_sync(self) -> None:
        """Trigger refresh-usage + snapshot rebuild on user demand.

        Flow:
          1. Origin/Host CSRF check.
          2. acquire(timeout=_DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS) -> 503
             only on truly degenerate contention beyond the timeout.
          3. With lock held: under --no-sync skip refresh; otherwise call
             _refresh_usage_inproc(); always call run_sync_now_locked.
          4. Return 204 on clean success, 200 + JSON warnings on
             non-ok refresh status, 500 on unexpected exception.

        Lock scope spans both refresh + rebuild so the periodic background
        thread cannot fire a redundant rebuild between the two steps.
        run_sync_now_locked assumes the caller holds sync_lock - that's the
        whole point of the Task 0 lock split.

        Bounded wait (vs. earlier non-blocking try_acquire): the periodic
        background thread holds the lock for hundreds of ms each tick, and
        a non-blocking acquire would 503 any click that landed inside that
        window — silently dropping the user's force-refresh intent (the
        periodic thread doesn't run refresh-usage). Waiting up to ~2s lets
        the click span a normal periodic tick while still 503-ing on
        truly stuck contention.

        --no-sync mode: refresh skipped (frozen mode preserves "no network
        calls"), rebuild still runs with skip_sync=True (the wired
        staticmethod closes over args.no_sync, so the no-arg call DTRT).

        Refresh failures DO NOT cause 500 - they surface as warnings in the
        200 envelope and the rebuild still runs so the snapshot stays
        fresh. Only an unexpected rebuild crash produces 500.
        """
        if not self._check_origin_csrf():
            return
        sync_lock = type(self).sync_lock
        if not sync_lock.acquire(
                timeout=sys.modules["cctally"]._DASHBOARD_SYNC_LOCK_TIMEOUT_SECONDS):
            self.send_error(503, "sync in progress")
            return
        try:
            warnings: list = []
            if not type(self).no_sync:
                result = _refresh_usage_inproc()
                if result.status != "ok":
                    warnings.append({"code": result.status})
            try:
                type(self).run_sync_now_locked()
            except Exception as exc:
                self.log_error("/api/sync rebuild failed: %r", exc)
                self.send_error(500, "sync failed")
                return
        finally:
            sync_lock.release()

        if warnings:
            self._respond_json(200, {"status": "ok", "warnings": warnings})
        else:
            self.send_response(204)
            self.end_headers()

    def _check_origin_csrf(self) -> bool:
        """Return True if Origin matches the request's Host header; else 403.

        Host-header parity: the browser's ``Origin`` header (host:port) must
        equal its ``Host`` header (host:port), case-insensitive. This blocks
        cross-origin browser attacks regardless of what the server bound to
        (no need to know own LAN IP at startup) AND preserves loopback
        aliasing for free (localhost ↔ 127.0.0.1 ↔ ::1 all flow through
        naturally because Host and Origin both come from the URL bar).
        Missing Origin or Host → 403. Shared across /api/sync,
        /api/settings, and /api/alerts/test.
        """
        import urllib.parse as _urlparse  # stdlib — local import (matches existing pattern in this module)
        origin = self.headers.get("Origin", "")
        host_header = self.headers.get("Host", "")
        if not origin or not host_header:
            self._respond_403("origin missing")
            return False
        try:
            origin_authority = _urlparse.urlsplit(origin).netloc
        except ValueError:
            self._respond_403("origin malformed")
            return False
        if not origin_authority:
            self._respond_403("origin malformed")
            return False
        if origin_authority.lower() != host_header.lower():
            self._respond_403("origin mismatch")
            return False
        return True

    def _respond_403(self, reason: str) -> None:
        """Send 403 in the right body shape for the calling endpoint.

        /api/sync uses send_error (text body) for backwards compat with
        existing fixture goldens; /api/settings + /api/alerts/test use
        the JSON shape every other 4xx response on those paths uses.
        """
        path = self.path.split("?", 1)[0]
        if path == "/api/sync":
            self.send_error(403, reason.title())
        else:
            self._respond_json(403, {"error": reason})

    def _handle_post_settings(self) -> None:
        """Persist a settings update and trigger an immediate SSE broadcast.

        Body shape: ``{"display"?: {"tz": "..."}, "alerts"?: {...},
        "update"?: {"check"?: {"enabled"?: bool, "ttl_hours"?: int}}}``
        — every top-level key is optional; any subset may be sent
        together (combined save). Unknown top-level keys are rejected
        with 400.

        Per-block validation:
          * ``display.tz`` — "local", "utc", or a valid IANA zone (via
            ``normalize_display_tz_value``); 400 on invalid.
          * ``alerts`` — must be a dict; ``alerts.enabled`` must be a
            JSON boolean (string "yes"/"true" rejected, per spec). Merged
            block is validated via ``_get_alerts_config(merged)``;
            ``_AlertsConfigError`` → 400.
          * ``update.check.enabled`` — JSON bool; 400 on type mismatch.
          * ``update.check.ttl_hours`` — JSON int (NOT string), in
            ``[1, 720]``; 400 on out-of-range or non-int. Bool is rejected
            (Python ``True`` is an int subclass, so a permissive check
            would silently accept ``true`` for a numeric field).

        Atomic merged write: if all touched blocks validate, the merged
        config is persisted in a single ``save_config`` call inside the
        ``config_writer_lock``. If any block fails validation, nothing
        is persisted — no partial commits.

        Security: Origin must match the request's Host header (Host-header
        parity; see ``_check_origin_csrf``). Missing Origin → 403.

        F3 ``--tz`` pin: when the operator launched the dashboard with
        ``--tz``, attempts to change ``display.tz`` are refused with 409.
        The pin protection scopes to the display block only — alerts
        writes proceed normally even under ``--tz``.

        F2 fix per spec: after `save_config` succeeds, call
        ``run_sync_now()`` synchronously so a fresh DataSnapshot lands on
        the SSE hub before this response returns. Each subscribed client's
        next read rebuilds the envelope through `snapshot_to_envelope`,
        which re-reads `load_config()` and therefore picks up the new
        config values. Critical under ``--no-sync`` where no periodic
        tick would deliver the new envelope on its own. Broadcast
        failures are logged and swallowed so the user-visible 200 still
        reflects the persisted config.

        Response 200 body: subset of touched blocks. ``display`` (when
        sent) is the full computed block from ``_compute_display_block``
        (preserves ``tz`` / ``resolved_tz`` / ``offset_label`` /
        ``offset_seconds`` shape consumers rely on). ``alerts`` (when
        sent) is the full validated block from ``_get_alerts_config``.
        ``saved_at`` is included for backward compat.
        """
        if not self._check_origin_csrf():
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._respond_json(400, {"error": "body required (<=4 KB)"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return
        if not isinstance(payload, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return

        # Reject unknown top-level keys (forward-compat hygiene).
        allowed_top_keys = {"display", "alerts", "update"}
        for k in payload.keys():
            if k not in allowed_top_keys:
                self._respond_json(
                    400, {"error": f"unknown settings key: {k}"}
                )
                return

        # Body must touch at least one known block.
        if (
            "display" not in payload
            and "alerts" not in payload
            and "update" not in payload
        ):
            self._respond_json(
                400,
                {"error": (
                    "body must contain at least one of: "
                    "display, alerts, update"
                )},
            )
            return

        # Pre-validate display block (outside the config_writer_lock so a
        # 400 short-circuit doesn't take the lock unnecessarily).
        display_canonical: "str | None" = None
        if "display" in payload:
            display_block = payload["display"]
            if not isinstance(display_block, dict) or "tz" not in display_block:
                self._respond_json(
                    400, {"error": "missing display.tz", "field": "display.tz"}
                )
                return
            try:
                display_canonical = normalize_display_tz_value(display_block["tz"])
            except ValueError:
                self._respond_json(
                    400,
                    {"error": f"invalid IANA zone: {display_block['tz']!r}",
                     "field": "display.tz"},
                )
                return
            # F3: refuse to overwrite the persisted preference while
            # `--tz` pins the runtime tz. Pin scope: display only —
            # alerts writes proceed even under --tz pin.
            if type(self).display_tz_pref_override is not None:
                self._respond_json(
                    409,
                    {"error": ("display.tz is pinned by --tz; restart "
                               "without --tz to change persisted preference"),
                     "field": "display.tz"},
                )
                return

        # Pre-validate alerts shape (the structural type checks; the full
        # cross-key validation runs inside the lock against the merged
        # config so future cross-field rules see the actually-persisted
        # state).
        if "alerts" in payload:
            alerts_block = payload["alerts"]
            if not isinstance(alerts_block, dict):
                self._respond_json(
                    400, {"error": "alerts must be an object"}
                )
                return
            if "enabled" in alerts_block and not isinstance(
                alerts_block["enabled"], bool
            ):
                self._respond_json(
                    400,
                    {"error": "alerts.enabled must be a JSON boolean"},
                )
                return

        # Pre-validate update shape. Only `update.check.{enabled,ttl_hours}`
        # is settable today; any other key under `update` or `update.check`
        # is rejected so adding e.g. `update.banner.*` later is forward
        # compatible. `enabled` must be a JSON bool; `ttl_hours` an int
        # (bools rejected — see _validate_update_check_ttl_hours_value).
        update_check_validated: "dict | None" = None
        if "update" in payload:
            update_in = payload["update"]
            if not isinstance(update_in, dict):
                self._respond_json(
                    400, {"error": "update must be an object"}
                )
                return
            for inner in update_in.keys():
                if inner != "check":
                    self._respond_json(
                        400,
                        {"error": f"unknown update settings key: {inner}",
                         "field": f"update.{inner}"},
                    )
                    return
            check_in = update_in.get("check", {})
            if not isinstance(check_in, dict):
                self._respond_json(
                    400,
                    {"error": "update.check must be an object",
                     "field": "update.check"},
                )
                return
            for leaf in check_in.keys():
                if leaf not in ("enabled", "ttl_hours"):
                    self._respond_json(
                        400,
                        {"error": f"unknown update.check key: {leaf}",
                         "field": f"update.check.{leaf}"},
                    )
                    return
            update_check_validated = {}
            if "enabled" in check_in:
                if not isinstance(check_in["enabled"], bool):
                    self._respond_json(
                        400,
                        {"error": "update.check.enabled must be a JSON boolean",
                         "field": "update.check.enabled"},
                    )
                    return
                update_check_validated["enabled"] = check_in["enabled"]
            if "ttl_hours" in check_in:
                try:
                    update_check_validated["ttl_hours"] = (
                        _validate_update_check_ttl_hours_value(check_in["ttl_hours"])
                    )
                except ValueError as exc:
                    self._respond_json(
                        400,
                        {"error": str(exc),
                         "field": "update.check.ttl_hours"},
                    )
                    return

        # Acquire config_writer_lock so a concurrent `cctally config set`
        # in another shell can't interleave its write between our load
        # and save (issue #17). Lock is process-cross via fcntl.flock,
        # complementing the in-process serialization done by the
        # dashboard's single-threaded settings handler.
        #
        # MUST use _load_config_unlocked() inside the lock — fcntl.flock
        # is per-fd not per-process, so calling load_config() (which
        # acquires the lock) would self-deadlock.
        with config_writer_lock():
            config = _load_config_unlocked()
            merged = dict(config)

            if display_canonical is not None:
                merged_display = dict(merged.get("display") or {})
                merged_display["tz"] = display_canonical
                merged["display"] = merged_display

            if "alerts" in payload:
                # Pre-merge type guard: a hand-edited config with a
                # non-dict stored alerts block (e.g. ``"alerts": "bad"``)
                # would make the dict() copy below raise inside the
                # request handler, bypassing the _AlertsConfigError → 400
                # mapping and surfacing as a 500 to the Settings UI.
                # Return the documented 400 + actionable message so the
                # user can recover (e.g. by hand-editing config.json).
                existing_alerts = merged.get("alerts")
                if existing_alerts is not None and not isinstance(
                    existing_alerts, dict
                ):
                    self._respond_json(
                        400, {"error": "alerts must be an object"}
                    )
                    return
                merged_alerts = dict(existing_alerts or {})
                alerts_in = payload["alerts"]
                if "enabled" in alerts_in:
                    merged_alerts["enabled"] = alerts_in["enabled"]
                merged["alerts"] = merged_alerts
                # Final cross-field validation against the merged block.
                # _AlertsConfigError → 400 (no partial write since
                # save_config has not yet been called).
                try:
                    _get_alerts_config(merged)
                except sys.modules["cctally"]._AlertsConfigError as exc:
                    self._respond_json(400, {"error": str(exc)})
                    return

            if update_check_validated is not None:
                # Same hand-edited-junk guard as alerts: a non-dict
                # `update` or `update.check` block in config.json should
                # surface as a recoverable 400, not a 500.
                existing_update = merged.get("update")
                if existing_update is not None and not isinstance(
                    existing_update, dict
                ):
                    self._respond_json(
                        400, {"error": "update must be an object",
                              "field": "update"}
                    )
                    return
                merged_update = dict(existing_update or {})
                existing_check = merged_update.get("check")
                if existing_check is not None and not isinstance(
                    existing_check, dict
                ):
                    self._respond_json(
                        400, {"error": "update.check must be an object",
                              "field": "update.check"}
                    )
                    return
                merged_check = dict(existing_check or {})
                merged_check.update(update_check_validated)
                merged_update["check"] = merged_check
                merged["update"] = merged_update

            save_config(merged)

        # Build the response: subset of touched blocks.
        out: dict = {}
        if display_canonical is not None:
            out["display"] = _compute_display_block(
                merged, dt.datetime.now(dt.timezone.utc)
            )
        if "alerts" in payload:
            out["alerts"] = _get_alerts_config(merged)
        if update_check_validated is not None:
            # Echo the full merged check block (cooked defaults included)
            # so the SettingsOverlay can repaint without a follow-up GET.
            out["update"] = {
                "check": {
                    "enabled": _config_known_value(
                        merged, "update.check.enabled"
                    ),
                    "ttl_hours": _config_known_value(
                        merged, "update.check.ttl_hours"
                    ),
                }
            }
        out["saved_at"] = (
            dt.datetime.now(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        # F2: synchronous SSE broadcast so --no-sync mode propagates the
        # new config before the next periodic tick (which never fires
        # under --no-sync). run_sync_now is the same path POST /api/sync
        # uses; under skip_sync=True it still rebuilds + publishes via
        # hub.publish, which is what each SSE listener pulls.
        try:
            type(self).run_sync_now()
        except Exception as exc:
            eprint(f"warning: settings broadcast failed: {exc!r}")

        self._respond_json(200, out)

    def _handle_post_alerts_test(self) -> None:
        """Send a synthetic alert through the dispatch pipeline (T7).

        Mirrors the CLI ``cctally alerts test`` path: builds a synthetic
        payload via the same ``_build_alert_payload_*`` helpers, calls
        ``_dispatch_alert_notification(..., mode="test")``, returns the
        dispatch status string in the JSON response.

        Body (all fields optional): ``{"axis": "weekly"|"five_hour",
        "threshold": 1..100}``. Defaults: axis="weekly", threshold=90.

        IMPORTANT: ``axis`` uses the underscore form (``"five_hour"``)
        in the JSON API to match the dispatch payload's internal axis
        discriminator. The CLI uses ``five-hour`` for argparse — that's
        a CLI-only convention.

        Always returns 200 when the request is well-formed (the dispatch
        helper is the source of "did it work"; the HTTP layer is just a
        transport). Body: ``{"alert": <payload>, "dispatch":
        "queued"|"spawn_error: ..."}``.

        Test alerts intentionally bypass ``alerts.enabled`` and the full
        config validation — users can verify the dispatch pipeline
        BEFORE turning on alerts (spec §5.3 Q&A).
        """
        if not self._check_origin_csrf():
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > 4096:
            self._respond_json(400, {"error": "body too large (<=4 KB)"})
            return
        if length == 0:
            body: dict = {}
        else:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._respond_json(400, {"error": "malformed json"})
                return
            if not isinstance(body, dict):
                self._respond_json(400, {"error": "expected JSON object"})
                return

        axis = body.get("axis", "weekly")
        if axis not in ("weekly", "five_hour"):
            self._respond_json(
                400,
                {"error": (
                    f"axis must be 'weekly' or 'five_hour', got {axis!r}"
                )},
            )
            return
        try:
            threshold = int(body.get("threshold", 90))
        except (TypeError, ValueError):
            self._respond_json(
                400, {"error": "threshold must be an integer in [1, 100]"}
            )
            return
        if not (1 <= threshold <= 100):
            self._respond_json(
                400, {"error": "threshold must be in [1, 100]"}
            )
            return

        if axis == "weekly":
            payload = _build_alert_payload_weekly(
                threshold=threshold,
                crossed_at_utc=now_utc_iso(),
                week_start_date=dt.date.today().isoformat(),
                cumulative_cost_usd=1.23,
                dollars_per_percent=0.01,
            )
        else:
            payload = _build_alert_payload_five_hour(
                threshold=threshold,
                crossed_at_utc=now_utc_iso(),
                five_hour_window_key=int(
                    dt.datetime.now(dt.timezone.utc).timestamp()
                ),
                block_start_at=now_utc_iso(),
                block_cost_usd=1.23,
                primary_model="claude-sonnet-4-6",
            )
        status = _dispatch_alert_notification(payload, mode="test")
        self._respond_json(200, {"alert": payload, "dispatch": status})

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

    def _share_load_templates_module(self):
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

    def _handle_share_templates_get(self) -> None:
        """List share templates registered for the requested panel.

        Query: ?panel=<id>. Rejects missing or non-share-capable panels
        (e.g., `alerts`) with 400 + {error, field} envelope (matches
        existing dashboard error shape; see spec §5.5).
        """
        import urllib.parse as _urlparse
        qs = _urlparse.urlparse(self.path).query
        params = _urlparse.parse_qs(qs)
        panel = (params.get("panel", [""])[0] or "").strip()
        if not panel:
            self._respond_json(400, {
                "error": "missing query param: panel",
                "field": "panel",
            })
            return
        tpl_mod = self._share_load_templates_module()
        if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
            self._respond_json(400, {
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
        self._respond_json(200, {"panel": panel, "templates": templates})

    def _handle_share_render_post(self) -> None:
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
        if not self._check_origin_csrf():
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            req = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return
        if not isinstance(req, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return
        panel = req.get("panel")
        template_id = req.get("template_id")
        options = req.get("options") or {}
        if not isinstance(options, dict):
            self._respond_json(400, {
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
            options["display_tz"] = get_display_tz_pref(load_config())
        if not isinstance(panel, str) or not panel:
            self._respond_json(400, {
                "error": "missing or non-string panel",
                "field": "panel",
            })
            return
        if not isinstance(template_id, str) or not template_id:
            self._respond_json(400, {
                "error": "missing or non-string template_id",
                "field": "template_id",
            })
            return
        fmt = options.get("format", "html")
        if fmt not in ("md", "html", "svg"):
            self._respond_json(400, {
                "error": f"unknown format: {fmt!r}",
                "field": "options.format",
            })
            return
        theme = options.get("theme", "light")
        if theme not in ("light", "dark"):
            self._respond_json(400, {
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
                self._respond_json(400, {
                    "error": f"top_n must be a positive integer, got {top_n_raw!r}",
                    "field": "options.top_n",
                })
                return

        tpl_mod = self._share_load_templates_module()
        if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
            self._respond_json(400, {
                "error": f"unknown share panel: {panel!r}",
                "field": "panel",
            })
            return
        try:
            template = tpl_mod.get_template(template_id)
        except KeyError:
            self._respond_json(400, {
                "error": f"unknown template_id: {template_id!r}",
                "field": "template_id",
            })
            return
        if template.panel != panel:
            self._respond_json(400, {
                "error": (
                    f"template_id {template_id!r} belongs to panel "
                    f"{template.panel!r}, not {panel!r}"
                ),
                "field": "template_id",
            })
            return

        # Build panel_data from the live dashboard snapshot — reuses the
        # already-built `DataSnapshot` so we don't re-query the DB on the
        # share hot path. `_build_share_panel_data` dispatches per panel.
        snap_ref = type(self).snapshot_ref
        data_snap = snap_ref.get() if snap_ref is not None else None
        # Period override (current / previous / custom). For
        # `kind='current'` (the default) this is a no-op; otherwise we
        # re-build the relevant panel's DataSnapshot field from DB with
        # a shifted `now_utc` before slicing.
        data_snap, period_err = _share_apply_period_override(panel, options,
                                                              data_snap)
        if period_err is not None:
            self._respond_json(400, period_err)
            return
        try:
            panel_data = _build_share_panel_data(panel, options, data_snap)
        except Exception as exc:
            self._respond_json(500, {"error": f"panel_data build failed: {exc}"})
            return

        # Run template builder → kernel render. Builder produces a
        # ShareSnapshot; `_scrub` anonymizes project labels when the
        # client opted in to anon-on-export (`reveal_projects=False`).
        ls = _share_load_lib()
        try:
            snap_built = template.builder(panel_data=panel_data, options=options)
        except Exception as exc:
            self._respond_json(500, {"error": f"builder failed: {exc}"})
            return
        snap_built = replace(snap_built, template_id=template_id)
        # Content toggles (spec §Q4). Defaults match the existing
        # behavior (chart on, table on); explicit False strips the
        # corresponding section from the ShareSnapshot. ShareSnapshot
        # is frozen so we use dataclasses.replace.
        snap_built = _share_apply_content_toggles(snap_built, options)
        reveal = bool(options.get("reveal_projects", True))
        if not reveal:
            snap_built = ls._scrub(snap_built, reveal_projects=False)
        try:
            body = ls.render(
                snap_built,
                format=fmt,
                theme=options.get("theme", "light"),
                branding=not options.get("no_branding", False),
            )
        except Exception as exc:
            self._respond_json(500, {"error": f"render failed: {exc}"})
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
        digest_input = {
            "panel": panel,
            "template_id": template_id,
            "panel_data": panel_data,
        }
        try:
            data_digest = ls._data_digest(digest_input)
        except Exception:
            # Defensive: digest is non-blocking for the response — fall
            # back to an empty string and let the composer treat it as
            # "always drifted" rather than failing the whole render.
            data_digest = ""

        self._respond_json(200, {
            "body": body,
            "content_type": content_type,
            "snapshot": {
                "kernel_version": ls.KERNEL_VERSION,
                "panel": panel,
                "template_id": template_id,
                "options": options,
                "generated_at": _share_now_utc_iso(),
                "data_digest": data_digest,
            },
        })

    # ---- /api/share/compose — stitch many basket sections (spec §5.3) ----

    def _handle_share_compose_post(self) -> None:
        """Stitch multiple panel sections into one composed document.

        Recipe-only. The server re-renders every section from its
        ``(panel, template_id, options)`` recipe — never accepting a client-
        supplied ``body``. Per-section drift detection compares the fresh
        ``data_digest`` against the client's ``data_digest_at_add``;
        mismatches surface as ``section_results[i].drift_detected = true``
        for the composer's "Outdated" badge.

        Spec §5.3, §10.3. CSRF-gated.
        """
        if not self._check_origin_csrf():
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            req = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return
        if not isinstance(req, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return

        title = req.get("title")
        theme = req.get("theme", "light")
        fmt = req.get("format", "html")
        no_branding = bool(req.get("no_branding", False))
        reveal_projects = bool(req.get("reveal_projects", False))
        sections_in = req.get("sections")
        if not isinstance(title, str) or not title:
            self._respond_json(400, {"error": "missing title", "field": "title"})
            return
        if theme not in ("light", "dark"):
            self._respond_json(400, {"error": f"unknown theme: {theme!r}",
                                      "field": "theme"})
            return
        if fmt not in ("md", "html", "svg"):
            self._respond_json(400, {"error": f"unknown format: {fmt!r}",
                                      "field": "format"})
            return
        if not isinstance(sections_in, list) or not sections_in:
            self._respond_json(400, {
                "error": "sections must be a non-empty array",
                "field": "sections",
            })
            return

        tpl_mod = self._share_load_templates_module()
        ls = _share_load_lib()
        snap_ref = type(self).snapshot_ref
        data_snap = snap_ref.get() if snap_ref is not None else None
        # Resolve display_tz from config once (client `ShareOptions`
        # does not carry it); applied to every section's options below
        # so daily panel rebuilds and per-day cross-tab cells bucket in
        # the user's display tz, not UTC.
        composite_display_tz = get_display_tz_pref(load_config())

        composed_sections: list = []
        section_results: list[dict] = []

        for idx, sec in enumerate(sections_in):
            if not isinstance(sec, dict):
                self._respond_json(400, {
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
            if (not isinstance(panel, str)
                    or panel not in tpl_mod.SHARE_CAPABLE_PANELS):
                self._respond_json(400, {
                    "error": (
                        f"sections[{idx}].snapshot.panel invalid: {panel!r}"
                    ),
                    "field": f"sections[{idx}].snapshot.panel",
                })
                return
            try:
                template = tpl_mod.get_template(template_id)
            except KeyError:
                self._respond_json(400, {
                    "error": (
                        f"sections[{idx}].snapshot.template_id "
                        f"unknown: {template_id!r}"
                    ),
                    "field": f"sections[{idx}].snapshot.template_id",
                })
                return
            if template.panel != panel:
                self._respond_json(400, {
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
            # Per-section period override — each basket item carries its
            # own period recipe, independent of the composite anon flag.
            sec_snap, period_err = _share_apply_period_override(
                panel, composite_opts, data_snap,
            )
            if period_err is not None:
                self._respond_json(400, {
                    "error": f"sections[{idx}]: {period_err['error']}",
                    "field": f"sections[{idx}].snapshot.{period_err['field']}",
                })
                return
            try:
                panel_data = _build_share_panel_data(panel, composite_opts,
                                                     sec_snap)
            except Exception as exc:
                self._respond_json(500, {
                    "error": f"sections[{idx}] panel_data build failed: {exc}",
                })
                return
            try:
                snap_built = template.builder(panel_data=panel_data,
                                              options=composite_opts)
            except Exception as exc:
                self._respond_json(500, {
                    "error": f"sections[{idx}] builder failed: {exc}",
                })
                return
            snap_built = replace(snap_built, template_id=template_id)
            # Same content toggles as the single-section render path.
            # Per-section `show_chart`/`show_table` from the basket
            # recipe are applied here; the composite anon flag is
            # already merged into composite_opts upstream.
            snap_built = _share_apply_content_toggles(snap_built, composite_opts)
            if not reveal_projects:
                snap_built = ls._scrub(snap_built, reveal_projects=False)

            # Defensive: digest is non-blocking metadata — fall back to
            # "" on failure rather than 500-ing the whole compose
            # (mirrors the render handler at bin/cctally:33402-33408).
            try:
                digest_now = ls._data_digest({
                    "panel": panel,
                    "template_id": template_id,
                    "panel_data": panel_data,
                })
            except Exception:
                digest_now = ""
            composed_sections.append(ls.ComposedSection(
                snap=snap_built,
                drift_detected=(digest_now != digest_at_add),
            ))
            section_results.append({
                "snapshot_id": f"{idx:02d}",
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
            self._respond_json(500, {"error": f"compose failed: {exc}"})
            return

        content_type = {
            "md":   "text/markdown",
            "html": "text/html",
            "svg":  "image/svg+xml",
        }[fmt]
        self._respond_json(200, {
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

    def _handle_share_presets_get(self) -> None:
        """List saved share presets, grouped by panel (spec §5.1, §11.3).

        Read-only — no CSRF gate. `config.json` may not contain the
        `share.presets` key on first run; returns `{"presets": {}}` then.
        """
        cfg = load_config()
        presets = (cfg.get("share") or {}).get("presets") or {}
        self._respond_json(200, {"presets": presets})

    def _handle_share_presets_post(self) -> None:
        """Create or overwrite a preset (idempotent on `(panel, name)`).

        Body: ``{panel, name, template_id, options}``. CSRF-gated.

        Persistence is a read-modify-write under ``config_writer_lock`` +
        ``_load_config_unlocked``. The plain ``load_config`` would
        self-deadlock on the same fcntl.flock fd; see the CLAUDE.md
        config-write invariant and `_cmd_config_set` for the canonical
        pattern.
        """
        if not self._check_origin_csrf():
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            req = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return
        if not isinstance(req, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return
        panel = req.get("panel")
        name = req.get("name")
        template_id = req.get("template_id")
        options = req.get("options")
        if not isinstance(panel, str) or not panel:
            self._respond_json(400, {
                "error": "missing or non-string panel",
                "field": "panel",
            })
            return
        tpl_mod = self._share_load_templates_module()
        if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
            self._respond_json(400, {
                "error": f"unknown share panel: {panel!r}",
                "field": "panel",
            })
            return
        if not isinstance(name, str) or not name or "/" in name or len(name) > 64:
            self._respond_json(400, {
                "error": "name must be 1-64 chars and contain no '/'",
                "field": "name",
            })
            return
        if not isinstance(template_id, str) or not template_id:
            self._respond_json(400, {
                "error": "missing or non-string template_id",
                "field": "template_id",
            })
            return
        try:
            template = tpl_mod.get_template(template_id)
        except KeyError:
            self._respond_json(400, {
                "error": f"unknown template_id: {template_id!r}",
                "field": "template_id",
            })
            return
        if template.panel != panel:
            self._respond_json(400, {
                "error": (
                    f"template_id {template_id!r} belongs to panel "
                    f"{template.panel!r}, not {panel!r}"
                ),
                "field": "template_id",
            })
            return
        if not isinstance(options, dict):
            self._respond_json(400, {
                "error": "options must be an object",
                "field": "options",
            })
            return

        saved_at = _share_now_utc_iso()
        record = {"template_id": template_id, "options": options, "saved_at": saved_at}

        with config_writer_lock():
            cfg = _load_config_unlocked()
            share = cfg.setdefault("share", {})
            presets = share.setdefault("presets", {})
            panel_bucket = presets.setdefault(panel, {})
            panel_bucket[name] = record
            save_config(cfg)
        self._respond_json(200, {"panel": panel, "name": name, **record})

    def _handle_share_presets_delete(self) -> None:
        """Remove a preset by `(panel, name)`.

        Path: ``/api/share/presets/{panel}/{name}``. Missing → 404 so
        DELETE stays meaningful for idempotency-aware clients. CSRF-gated.
        """
        if not self._check_origin_csrf():
            return
        import urllib.parse as _urlparse
        # Strip the query string defensively; the spec only uses path
        # segments but a stray "?" shouldn't poison the name token.
        path_only = self.path.split("?", 1)[0]
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
            self._respond_json(400, {"error": "malformed delete path"})
            return
        panel = _urlparse.unquote(parts[4])
        name = _urlparse.unquote(parts[5])
        with config_writer_lock():
            cfg = _load_config_unlocked()
            share = cfg.get("share") or {}
            presets = share.get("presets") or {}
            panel_bucket = presets.get(panel) or {}
            if name not in panel_bucket:
                self._respond_json(404, {"error": "no such preset"})
                return
            del panel_bucket[name]
            # Tidy empty buckets so GET stays clean.
            if not panel_bucket:
                presets.pop(panel, None)
            save_config(cfg)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def _handle_share_history_get(self) -> None:
        """Return the recent-shares ring buffer (newest last, spec §11.4)."""
        cfg = load_config()
        history = (cfg.get("share") or {}).get("history") or []
        self._respond_json(200, {"history": history})

    def _handle_share_history_post(self) -> None:
        """Append a recipe to the ring buffer; FIFO trim to 20.

        Body: ``{panel, template_id, options, format, destination}``. The
        server stamps ``recipe_id`` (random hex) and ``exported_at``
        (UTC ISO-8601) so the client doesn't need a clock or a UUID lib.
        CSRF-gated. Read-modify-write under ``config_writer_lock`` —
        same pattern as the presets POST.
        """
        if not self._check_origin_csrf():
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            req = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return
        if not isinstance(req, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return
        panel = req.get("panel")
        template_id = req.get("template_id")
        options = req.get("options") or {}
        fmt = req.get("format")
        destination = req.get("destination")
        if not isinstance(panel, str) or not panel:
            self._respond_json(400, {
                "error": "missing or non-string panel",
                "field": "panel",
            })
            return
        tpl_mod = self._share_load_templates_module()
        if panel not in tpl_mod.SHARE_CAPABLE_PANELS:
            self._respond_json(400, {
                "error": f"unknown share panel: {panel!r}",
                "field": "panel",
            })
            return
        if not isinstance(template_id, str) or not template_id:
            self._respond_json(400, {
                "error": "missing or non-string template_id",
                "field": "template_id",
            })
            return
        try:
            template = tpl_mod.get_template(template_id)
        except KeyError:
            self._respond_json(400, {
                "error": f"unknown template_id: {template_id!r}",
                "field": "template_id",
            })
            return
        if template.panel != panel:
            self._respond_json(400, {
                "error": (
                    f"template_id {template_id!r} belongs to panel "
                    f"{template.panel!r}, not {panel!r}"
                ),
                "field": "template_id",
            })
            return
        if not isinstance(options, dict):
            self._respond_json(400, {
                "error": "options must be an object",
                "field": "options",
            })
            return
        # `format` and `destination` are advisory strings — accept any
        # non-empty string; the frontend uses them only as display hints
        # in the dropdown row. None/missing is allowed (mirrors how the
        # CLI doesn't always know which destination produced the export).
        if fmt is not None and not isinstance(fmt, str):
            self._respond_json(400, {
                "error": "format must be a string if provided",
                "field": "format",
            })
            return
        if destination is not None and not isinstance(destination, str):
            self._respond_json(400, {
                "error": "destination must be a string if provided",
                "field": "destination",
            })
            return

        record = {
            "recipe_id": _share_history_recipe_id(),
            "panel": panel,
            "template_id": template_id,
            "options": options,
            "format": fmt,
            "destination": destination,
            "exported_at": _share_now_utc_iso(),
        }
        with config_writer_lock():
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
        self._respond_json(200, record)

    def _handle_share_history_delete(self) -> None:
        """Empty the share-history ring buffer (spec §11.4)."""
        if not self._check_origin_csrf():
            return
        with config_writer_lock():
            cfg = _load_config_unlocked()
            share = cfg.get("share")
            if isinstance(share, dict) and "history" in share:
                share["history"] = []
                save_config(cfg)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- helpers ----

    def _respond_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def _content_type_for(p: pathlib.Path) -> str:
        return {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".svg":  "image/svg+xml",
            ".txt":  "text/plain; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(p.suffix.lower(), "application/octet-stream")

    def _serve_static_file(self, path: pathlib.Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "not found")
            return
        except IsADirectoryError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_data(self) -> None:
        snap = self.snapshot_ref.get()
        # Resolve oauth_usage cfg out here so snapshot_to_envelope stays
        # pure (no per-request FS read on the dashboard hot path).
        # Tolerate user config typos -- fall back to defaults rather than
        # 500 the dashboard endpoint.
        try:
            cfg_oauth = _get_oauth_usage_config(load_config())
        except OauthUsageConfigError:
            cfg_oauth = dict(sys.modules["cctally"]._OAUTH_USAGE_DEFAULTS)
        env = snapshot_to_envelope(
            snap,
            # `_now_utc()` honors CCTALLY_AS_OF for harness determinism;
            # zero production impact (the env var is never set outside
            # fixture tests). Without this, the doctor block's now_utc
            # flows from wall clock and severity flips on borderline-age
            # checks between parallel test runs, churning goldens.
            now_utc=_now_utc(),
            monotonic_now=time.monotonic(),
            oauth_usage_cfg=cfg_oauth,
            display_tz_pref_override=type(self).display_tz_pref_override,
            runtime_bind=type(self).cctally_host,
        )
        body = json.dumps(env, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_get_doctor(self) -> None:
        """`GET /api/doctor` — full kernel-serialized doctor report (spec §5.6).

        Lazy companion to the aggregate-only `doctor` block on the SSE
        envelope. Re-runs the gather + checks fresh on every call (cheap;
        no per-tick cache needed — the kernel's identity-slice fingerprint
        carries the dedup story for the SSE channel, not this endpoint).
        Passes `runtime_bind = cctally_host` so `safety.dashboard_bind`
        sees the bind the dashboard is ACTUALLY serving (Codex H4 — CLI
        path stays config-only).

        GET endpoints are read-only and loopback-protected by the
        dashboard's default bind; no CSRF gating here (mirrors
        `/api/data`, `/api/session/:id`, `/api/block/:start_at`).
        """
        try:
            _ld = sys.modules["cctally"]._load_sibling("_lib_doctor")
            state = doctor_gather_state(
                runtime_bind=type(self).cctally_host,
            )
            report = _ld.run_checks(state)
            body = json.dumps(
                _ld.serialize_json(report), ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            self.log_error("/api/doctor failed: %r", exc)
            self._respond_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_get_session_detail(self, path: str) -> None:
        """Return TuiSessionDetail JSON for the given session id (spec §3.2).

        Path form: ``/api/session/<url-encoded-session-id>``. The id is
        percent-decoded before lookup so clients that encode ``/`` or
        other reserved chars round-trip correctly. Unknown ids return
        404. ``_tui_build_session_detail`` exceptions become a 500 via
        ``self.log_error`` so the browser sees a clean status line rather
        than a stack trace.

        ``now_utc`` is sourced from the current snapshot's
        ``generated_at`` (mirroring the TUI v2 session-detail cache)
        rather than the wall clock, so fixture runs with
        ``CCTALLY_AS_OF`` pinned far from today stay consistent between
        the panel row list and the modal fetch. Otherwise a session
        visible in the rows could 404 on modal open because the build
        helper's freshness window was anchored to real-now.
        """
        import urllib.parse as _urlparse
        raw = path[len("/api/session/"):]
        session_id = _urlparse.unquote(raw)
        if not session_id:
            self.send_error(404, "session not found")
            return
        try:
            snap = self.snapshot_ref.get()
            detail = sys.modules["cctally"]._tui_build_session_detail(
                session_id,
                now_utc=snap.generated_at,
            )
        except Exception as exc:
            self.log_error("/api/session failed: %r", exc)
            self.send_error(500, "session detail failed")
            return
        if detail is None:
            self.send_error(404, "session not found")
            return
        body = json.dumps(
            _session_detail_to_envelope(detail), ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _handle_get_block_detail(self, path: str) -> None:
        """Return BlockDetail JSON for the block whose start_time equals
        the URL-encoded ISO-8601 UTC datetime in the path tail.

        Bad-datetime → 400. Unknown block → 404. Builder exceptions → 500.
        ``now`` flows through ``_command_as_of()`` (not
        ``snap.generated_at`` like ``_handle_get_session_detail``) so the
        CCTALLY_AS_OF env override pins this endpoint deterministically
        for fixture-driven tests, independent of any active snapshot tick.
        """
        import urllib.parse as _urlparse
        raw = path[len("/api/block/"):]
        decoded = _urlparse.unquote(raw)
        try:
            start_at = parse_iso_datetime(decoded, "start_at")
        except ValueError:
            body = json.dumps({"error": "invalid start_at"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=dt.timezone.utc)
        else:
            start_at = start_at.astimezone(dt.timezone.utc)
        end_at = start_at + BLOCK_DURATION
        try:
            now_utc = _command_as_of()
            # Recorded-windows lookup widens by one block on each side so
            # a recorded reset just outside the bounds can still anchor.
            recorded_windows, block_start_overrides = (
                _load_recorded_five_hour_windows(
                    start_at - BLOCK_DURATION, end_at + BLOCK_DURATION,
                )
            )
            # Entries: only the window we care about. Mirrors the panel's
            # discipline of pre-filtering before grouping (cf.
            # _dashboard_build_blocks_panel) so a cross-week-boundary
            # entry just outside the requested block can't shift a
            # heuristic anchor and turn the requested block into a 404.
            # skip_sync mirrors cmd_dashboard's --no-sync flag (set as a
            # class attr by cmd_dashboard). Default False keeps
            # panel<->detail symmetric: if the panel's get_entries() fell
            # back to direct JSONL parse on cache contention, the detail
            # call will too -- no 404 on click during a cache rebuild.
            # Under --no-sync, both paths stay cache-only.
            entries_in_window = list(get_entries(
                start_at, end_at, skip_sync=self.no_sync,
            ))
            blocks = _group_entries_into_blocks(
                entries_in_window, mode="auto",
                recorded_windows=recorded_windows,
                block_start_overrides=block_start_overrides,
                now=now_utc,
            )
            target = next(
                (b for b in blocks
                 if (not b.is_gap) and b.start_time == start_at),
                None,
            )
            if target is None:
                body = json.dumps({"error": "block not found"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            block_entries = [
                e for e in entries_in_window
                if target.start_time <= e.timestamp < target.end_time
            ]
            # Resolve display tz once per request so the block detail's
            # `label` matches the snapshot envelope's blocks panel.
            # Shared resolver -- same warn-once semantics as
            # `_compute_display_block` and `_tui_build_snapshot`. F3:
            # honor the dashboard's `--tz` override (set as a class attr
            # by cmd_dashboard) so the block-detail label speaks the
            # same zone the rest of the envelope speaks.
            _detail_tz = _resolve_display_tz_obj(
                _apply_display_tz_override(
                    load_config(), type(self).display_tz_pref_override
                )
            )
            detail = _build_block_detail(
                target, block_entries, display_tz=_detail_tz,
            )
        except Exception as exc:
            self.log_error("/api/block failed: %r", exc)
            self.send_error(500, "block detail failed")
            return
        body = json.dumps(detail, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_api_events(self) -> None:
        import queue as _queue
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        # Nginx/proxies: disable buffering so events flow immediately.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Resolve oauth_usage cfg once per SSE connection so the per-tick
        # envelope build stays free of FS reads. A config edit during the
        # connection's lifetime won't take effect until the client
        # reconnects; that's an acceptable trade-off for the hot path.
        try:
            cfg_oauth = _get_oauth_usage_config(load_config())
        except OauthUsageConfigError:
            cfg_oauth = dict(sys.modules["cctally"]._OAUTH_USAGE_DEFAULTS)

        q = self.hub.subscribe()
        try:
            while True:
                try:
                    snap = q.get(timeout=15)
                except _queue.Empty:
                    # Keep-alive. Comment lines are ignored by EventSource
                    # but stop idle-proxy timeouts.
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                env = snapshot_to_envelope(
                    snap,
                    now_utc=dt.datetime.now(dt.timezone.utc),
                    monotonic_now=time.monotonic(),
                    oauth_usage_cfg=cfg_oauth,
                    display_tz_pref_override=type(self).display_tz_pref_override,
                    runtime_bind=type(self).cctally_host,
                )
                msg = (
                    "event: update\n"
                    + "data: " + json.dumps(env, ensure_ascii=False) + "\n\n"
                )
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnect is normal. No-op.
            pass
        finally:
            self.hub.unsubscribe(q)

    # --- /api/update* (spec §5.6, §5.6.1, §5.7) ----------------------
    # The four endpoints share the module-level singleton
    # ``_UPDATE_WORKER`` which is created in ``cmd_dashboard``. POST
    # routes pass through ``_check_origin_csrf`` (host-header parity);
    # GET routes are read-only and skip CSRF intentionally so polling
    # fallbacks survive odd Origin shapes (e.g. native browser tools).

    def _read_update_post_body(self) -> "dict | None":
        """Read + parse a small JSON body for /api/update* POSTs.

        Empty body is allowed (returns ``{}``). Non-dict / malformed →
        respond 400 and return ``None`` so the caller short-circuits.
        Body is capped at 4 KB — same envelope as /api/settings.
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > 4096:
            self._respond_json(400, {"error": "body too large (<=4 KB)"})
            return None
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond_json(400, {"error": "malformed json"})
            return None
        if not isinstance(payload, dict):
            self._respond_json(400, {"error": "expected JSON object"})
            return None
        return payload

    def _handle_post_update(self) -> None:
        """POST /api/update — kick off an in-process update.

        Body: ``{"version"?: "X.Y.Z"}``. CSRF-gated. Returns
        202 + ``{"run_id": ...}`` on accept; 409 + ``{"run_id_in_progress": ...}``
        when another run is already in progress.
        """
        if not self._check_origin_csrf():
            return
        payload = self._read_update_post_body()
        if payload is None:
            return
        worker = sys.modules["cctally"]._UPDATE_WORKER
        if worker is None:
            self._respond_json(
                500, {"error": "update worker not initialized"}
            )
            return
        version = payload.get("version") if isinstance(payload, dict) else None
        if version is not None and not isinstance(version, str):
            self._respond_json(
                400, {"error": "version must be a string"}
            )
            return
        accepted, run_id = worker.start(version)
        if accepted:
            self._respond_json(202, {"run_id": run_id})
        else:
            self._respond_json(409, {"run_id_in_progress": run_id})

    def _handle_post_update_dismiss(self) -> None:
        """POST /api/update/dismiss — record a skip / remind-later.

        Body: ``{"action": "skip"|"remind", "version"?: "X.Y.Z", "days"?: int}``.
        CSRF-gated. Mutates ``update-suppress.json`` via the same
        ``_do_update_skip`` / ``_do_update_remind_later`` helpers the
        CLI uses, so the on-disk shape stays single-source-of-truth.
        Returns 204 on success, 400 on invalid action / shape.
        """
        if not self._check_origin_csrf():
            return
        payload = self._read_update_post_body()
        if payload is None:
            return
        action = payload.get("action")
        # Suppress stdout/stderr from _do_update_skip /
        # _do_update_remind_later so their CLI-style "Skipped …" /
        # "Will remind …" prints don't pollute the dashboard server's
        # log stream. The HTTP response is the user-facing surface.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            if action == "skip":
                ver = payload.get("version")
                if ver is None or ver == "":
                    rc = _do_update_skip(sys.modules["cctally"].SKIP_USE_STATE_LATEST)
                elif not isinstance(ver, str):
                    self._respond_json(
                        400, {"error": "version must be a string"}
                    )
                    return
                else:
                    rc = _do_update_skip(ver)
            elif action == "remind":
                days_raw = payload.get("days", 7)
                try:
                    days = int(days_raw)
                except (TypeError, ValueError):
                    self._respond_json(
                        400, {"error": "days must be an integer"}
                    )
                    return
                rc = _do_update_remind_later(days)
            else:
                self._respond_json(
                    400,
                    {
                        "error": (
                            "action must be 'skip' or 'remind'"
                        )
                    },
                )
                return
        if rc != 0:
            # Helper printed an explanation to stderr (now swallowed).
            # Map to a generic 400; UI's polling status will show the
            # latest state. Future surface improvement: have the
            # helpers return a structured error so we can echo the
            # specific reason ("no version in cache to skip") here.
            self._respond_json(400, {"error": "dismiss failed"})
            return
        self.send_response(204)
        self.end_headers()

    def _handle_get_update_status(self) -> None:
        """GET /api/update/status — return state + suppress + worker status.

        Polling-fallback friendly so a browser that missed the SSE
        execvp event can detect "no run in progress" and re-render the
        idle modal state. ``state`` and ``suppress`` come from the
        canonical helpers so the on-disk shape is single-source-of-truth.
        """
        try:
            state = _load_update_state()
        except sys.modules["cctally"].UpdateError as e:
            state = {"_error": str(e)[:200]}
        try:
            suppress = _load_update_suppress()
        except sys.modules["cctally"].UpdateError as e:
            suppress = {"_error": str(e)[:200]}
        _w = sys.modules["cctally"]._UPDATE_WORKER
        worker_status = (
            _w.status() if _w is not None
            else {"current_run_id": None}
        )
        body = {
            "state": state,
            "suppress": suppress,
            **worker_status,
        }
        self._respond_json(200, body)

    def _handle_get_update_stream(self, path: str) -> None:
        """GET /api/update/stream/<run_id> — SSE event stream.

        Yields events from ``UpdateWorker.stream(run_id)``. Closes on
        the worker's terminal events (``execvp`` / ``error_event`` /
        ``done``). 404 when the worker is uninitialized; the worker's
        own generator returns immediately on unknown run_id, so the
        connection closes cleanly.
        """
        worker = sys.modules["cctally"]._UPDATE_WORKER
        if worker is None:
            self.send_error(404, "update worker not initialized")
            return
        run_id = path.rsplit("/", 1)[-1]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for ev in worker.stream(run_id):
                ev_type = ev.get("type", "message")
                ev_data = json.dumps(
                    {k: v for k, v in ev.items() if k != "type"},
                    ensure_ascii=False,
                )
                msg = f"event: {ev_type}\ndata: {ev_data}\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Browser disconnected mid-stream. The worker keeps
            # running; subsequent reconnects can poll /api/update/status.
            pass




# === cmd_dashboard (dashboard subcommand entry point) =====================
# Pre-extract location: bin/cctally L21478.

def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the live web dashboard."""
    import signal as _signal
    import threading
    import time as _time
    import webbrowser as _wb

    # Spec §5.7: capture the un-mutated argv + PATH-resolved entrypoint
    # at boot so the in-place ``execvp`` after a successful update
    # re-enters the user-facing wrapper (npm Node shim → CCTALLY_PYTHON
    # honoured; brew symlink → post-upgrade Python script). Module-level
    # globals live in cctally (declared at L23205 pre-extract); we write
    # them via the module-proxy so ``UpdateWorker`` (in _cctally_update)
    # reads the right values via ``cctally.X`` at call time.
    _c_boot = _cctally()
    _c_boot.ORIGINAL_SYS_ARGV = list(sys.argv)
    _c_boot.ORIGINAL_ENTRYPOINT = shutil.which("cctally")
    _c_boot._UPDATE_WORKER = UpdateWorker()

    # Resolve display tz (--tz overrides config.display.tz). The dashboard's
    # envelope-emitted display block (Tasks 11-12) reads this; for now,
    # stash on args so downstream selectors can pick it up uniformly.
    config = load_config()
    args._resolved_tz = resolve_display_tz(args, config)

    # Resolve bind host: --host flag > config.dashboard.bind > argparse default.
    # `--host` defaults to None (Task 2) so we can distinguish "user explicitly
    # passed --host" from "user did not pass --host".
    if args.host is not None:
        resolved_bind_stored = args.host  # explicit flag — pass through verbatim
    else:
        dashboard_block = config.get("dashboard")
        if not isinstance(dashboard_block, dict):
            dashboard_block = {}
        stored = dashboard_block.get("bind") or "loopback"
        try:
            stored = _validate_dashboard_bind_value(stored)
        except ValueError:
            # Hand-edited junk: warn + fall back to loopback default. Same
            # posture as load_config()'s warn-once-and-fall-back on
            # JSONDecodeError. Loopback is the safe default — opt in to LAN
            # via `dashboard.bind = lan` or `--host 0.0.0.0`.
            eprint("warning: invalid dashboard.bind in config; using 'loopback'")
            stored = "loopback"
        resolved_bind_stored = stored
    args.host = _resolve_dashboard_bind_for_runtime(resolved_bind_stored)

    # F3: capture the canonical tz token from `--tz` (NOT the ZoneInfo
    # — the override needs to flow through `load_config()`-style readers
    # as if it were `config["display"]["tz"]`). When `--tz` is unset, no
    # override applies and persisted config wins. Canonicalized via
    # normalize_display_tz_value so all readers see the same shape.
    raw_flag = getattr(args, "tz", None)
    if raw_flag is not None and str(raw_flag).strip() != "":
        try:
            display_tz_pref_override = normalize_display_tz_value(raw_flag)
        except ValueError:
            # argparse already validates via _argparse_tz, but defend in
            # depth in case a future caller bypasses argparse.
            display_tz_pref_override = None
    else:
        display_tz_pref_override = None

    # Fail fast if static assets are missing — symlinked-from-~/.local/bin
    # installs require the checked-out repo to stay in place.
    if not (STATIC_DIR / "dashboard.html").exists():
        print(
            f"dashboard: static assets not found at {STATIC_DIR}. "
            "Ensure the repo is checked out and symlinks point at it.",
            file=sys.stderr,
        )
        return 1

    # Honor CCTALLY_AS_OF for fixture-driven testing — same hook the TUI,
    # weekly, project, forecast subcommands already respect.
    pinned_now = _command_as_of() if os.environ.get("CCTALLY_AS_OF") else None

    # Build initial snapshot — blocking, serves immediately.
    initial = sys.modules["cctally"]._tui_build_snapshot(
        now_utc=pinned_now, skip_sync=args.no_sync,
        display_tz_pref_override=display_tz_pref_override,
    )
    if args.no_sync:
        # No background refresher will run, so surfacing a ticking
        # "synced Ns ago" chip would be misleading. Clear the monotonic
        # stamp so the envelope emits sync_age_s=None and the JS client
        # renders the documented `sync paused` state.
        initial = dataclasses.replace(initial, last_sync_at=None)

    ref = _SnapshotRef(initial)
    hub = SSEHub()
    hub.publish(initial)  # seed for early subscribers

    # sync_lock serializes sync-work between the periodic sync thread and
    # the POST /api/sync handler. Held only around _tui_build_snapshot +
    # ref.set + hub.publish — NOT around the handler's response path.
    # The handler uses acquire(timeout=…) so a click that lands inside
    # the periodic thread's lock-hold waits briefly rather than 503-ing
    # and silently dropping the user's force-refresh intent; only stuck
    # contention beyond the timeout produces 503. The lock inside
    # _run_sync_now is what actually prevents overlap.
    sync_lock = threading.Lock()

    # Build the two variants up front. The locked variant is exposed on the
    # handler so /api/sync paths that already hold sync_lock (e.g. for
    # multi-step refresh-then-rebuild) can reuse the snapshot-publish body
    # without recursive-acquire (threading.Lock is non-reentrant).
    _run_sync_now_locked = _make_run_sync_now_locked(
        ref=ref, hub=hub, pinned_now=pinned_now,
        display_tz_pref_override=display_tz_pref_override,
    )
    _run_sync_now = _make_run_sync_now(
        sync_lock=sync_lock, ref=ref, hub=hub, pinned_now=pinned_now,
        display_tz_pref_override=display_tz_pref_override,
    )

    # Wire the class-level handles for the handler.
    DashboardHTTPHandler.hub = hub
    DashboardHTTPHandler.snapshot_ref = ref
    DashboardHTTPHandler.static_dir = STATIC_DIR
    DashboardHTTPHandler.sync_lock = sync_lock
    DashboardHTTPHandler.no_sync = bool(args.no_sync)
    DashboardHTTPHandler.display_tz_pref_override = display_tz_pref_override
    # Doctor (spec §7.4 / Codex H4): runtime bind, so safety.dashboard_bind
    # in the doctor SSE block + /api/doctor reflects the actual --host
    # the process is serving, not just the config-only view the CLI sees.
    DashboardHTTPHandler.cctally_host = args.host
    DashboardHTTPHandler.run_sync_now = staticmethod(
        lambda: _run_sync_now(skip_sync=args.no_sync)
    )
    DashboardHTTPHandler.run_sync_now_locked = staticmethod(
        lambda: _run_sync_now_locked(skip_sync=args.no_sync)
    )

    # Background rebuilder — reuses the TUI's proven sync thread with a
    # small shim that delegates the sync body to _run_sync_now (so POST
    # /api/sync and the periodic tick share one code path). The base class
    # is resolved via ``c._TuiSyncThread`` at call time (subclassing needs
    # a real class object) since ``_TuiSyncThread`` STAYS in bin/cctally
    # for the upcoming Phase F #23 TUI extraction.
    _c_for_subclass = _cctally()

    class _DashboardSyncThread(_c_for_subclass._TuiSyncThread):
        def _run(self) -> None:
            while not self._stop.is_set():
                _run_sync_now(skip_sync=self._skip_sync)
                for _ in range(int(max(1, self._interval * 10))):
                    if self._stop.is_set():
                        return
                    if self._ref.take_sync_request():
                        break
                    _time.sleep(0.1)

    sync_thread = (
        None if args.no_sync
        else _DashboardSyncThread(
            ref, float(args.sync_interval), skip_sync=False, now_utc=pinned_now,
        )
    )
    if sync_thread is not None:
        sync_thread.start()

    # Spec §3.5 (codex review fix #5): update-check thread runs even
    # under --no-sync (frozen-data sessions still surface new versions).
    # Dedicated stop event so we can join cleanly on shutdown without
    # relying on the data-sync thread's lifecycle.
    update_check_stop = threading.Event()
    update_check_thread = _DashboardUpdateCheckThread(
        update_check_stop, hub=hub, snapshot_ref=ref,
    )
    update_check_thread.start()

    # HTTP server on its own thread so the main thread can block on signal.
    srv = ThreadingHTTPServer((args.host, args.port), DashboardHTTPHandler)
    srv.daemon_threads = True  # SSE handler threads may block up to 15s on keep-alive timeout — let them die with the process.

    bind_host = args.host
    bind_port = srv.server_address[1]
    is_all_interfaces = bind_host in ("0.0.0.0", "::")
    is_loopback = bind_host in ("127.0.0.1", "localhost", "::1")

    if is_all_interfaces:
        local_url = _format_url("localhost", bind_port)
        lan_ip = _discover_lan_ip()
        print("dashboard: serving on all interfaces:", flush=True)
        print(f"  - {local_url}      (this machine)", flush=True)
        if lan_ip:
            lan_url = _format_url(lan_ip, bind_port)
            print(f"  - {lan_url}   (LAN)", flush=True)
        print("Ctrl-C to stop", flush=True)
    elif is_loopback:
        url = _format_url("localhost", bind_port)
        print(f"dashboard: serving {url} — Ctrl-C to stop", flush=True)
    else:
        url = _format_url(bind_host, bind_port)
        print(f"dashboard: serving {url} — Ctrl-C to stop", flush=True)

    http_thread = threading.Thread(target=srv.serve_forever, daemon=True,
                                   name="dashboard-http")
    http_thread.start()

    # Surface any pending migration errors at server startup. Browser is a
    # separate UI surface (out of scope for this fix); this prints once to
    # stdout for the operator who launched the server. We bypass the
    # main()-level banner via the dashboard suppression in
    # _print_migration_error_banner_if_needed and re-render here so the
    # banner appears AFTER the "serving …" line for readability.
    _dashboard_banner_msg = _render_migration_error_banner()
    if _dashboard_banner_msg is not None:
        print(_dashboard_banner_msg, flush=True)

    if not args.no_browser:
        # Mirror the banner: localhost works for all-interfaces and loopback
        # binds, but a specific non-loopback --host (e.g. a LAN or Tailscale
        # IP) means the server isn't listening on localhost — open the bound
        # host instead so the auto-opened URL matches what the banner prints.
        if is_all_interfaces or is_loopback:
            browser_url = _format_url("localhost", bind_port)
        else:
            browser_url = _format_url(bind_host, bind_port)
        try:
            _wb.open(browser_url)
        except Exception as exc:
            # Headless environments (no DISPLAY, missing xdg-open) are not
            # fatal — user can visit the URL manually.
            print(
                f"dashboard: could not open browser ({exc}); "
                f"visit {browser_url} manually",
                file=sys.stderr,
            )

    stop = threading.Event()
    def _handler(signum, frame):
        stop.set()
    _signal.signal(_signal.SIGINT, _handler)
    _signal.signal(_signal.SIGTERM, _handler)

    try:
        stop.wait()
    finally:
        if sync_thread is not None:
            sync_thread.stop()
        update_check_stop.set()
        srv.shutdown()
        http_thread.join(timeout=2)
        print("dashboard: stopped", flush=True)
    return 0

