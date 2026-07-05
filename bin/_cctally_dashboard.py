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
  ``--open`` is set; blocks on ``_dashboard_wait_for_signal`` for
  ``SIGINT`` / ``SIGTERM`` (lost-wakeup-proof per #154) then runs
  clean shutdown.
- ``_dashboard_wait_for_signal`` — main-thread shutdown wait built on
  ``signal.set_wakeup_fd`` + ``select`` so a single SIGINT/SIGTERM
  always tears the server down, immune to the ``threading.Event.wait()``
  lost-wakeup race (#154).
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
  ``format_display_dt``,
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
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` that swallows client-disconnect tracebacks.

    A backgrounded/closed/reloaded dashboard tab hangs up mid-response, so the
    per-request thread's socket write raises one of the "peer went away"
    exceptions, which `socketserver` routes through `handle_error`. On a local
    dashboard that is expected and benign — dumping a socket-write stack trace
    to the user's console for every such disconnect is pure noise (the original
    report was repeated `BrokenPipeError` spam after hours idle). Everything
    else still gets the full traceback via `super().handle_error`.

    `daemon_threads = True` is folded in here (was an inline `srv.*` assignment):
    SSE handler threads may block up to 15s on the keep-alive timeout — let them
    die with the process.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            # Client hung up mid-response; benign on a local dashboard.
            return
        super().handle_error(request, client_address)


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core; already-decentralized buckets (X = _lib_*,
# Y = _cctally_*) import from their natural home. These bypass the
# legacy shim pattern entirely.
import _cctally_core
from _cctally_core import (
    eprint,
    now_utc_iso,
    parse_iso_datetime,
    _now_utc,
    _command_as_of,
    open_db,
    get_latest_usage_for_week,
    make_week_ref,
    _get_alerts_config,
    _AlertsConfigError,
    _get_budget_config,
    _budget_alerts_active,
    _BudgetConfigError,
)
from _lib_display_tz import (
    format_display_dt,
    normalize_display_tz_value,
    _compute_display_block,
)
from _lib_aggregators import _aggregate_daily, _aggregate_monthly, _aggregate_weekly
from _lib_fmt import stable_sum
from _lib_pricing import _calculate_entry_cost, _chip_for_model, _short_model_name
from _lib_five_hour import _canonical_5h_window_key
from _lib_subscription_weeks import _compute_subscription_weeks
from _lib_blocks import _group_entries_into_blocks
from _cctally_config import save_config, _load_config_unlocked
from _cctally_db import _render_migration_error_banner
from _cctally_cache import (
    get_entries, iter_entries, iter_entries_with_id, open_cache_db, sync_cache,
    _prune_orphaned_cache_entries,
)
from _lib_snapshot_cache import (
    build_cached_group_a,
    bump_generation,
    cached_bugk_segment,
    reset_bugk_segment_state,
    reset_group_a_state,
    reset_projects_env_state,
    reset_session_cache_state,
    reset_weekref_cost_state,
    BugKSegment,
    _bugk_key,
    _max_id as _snapshot_max_id,
    _reset_sig as _snapshot_reset_sig,
)


# #268 Group A cached-bucket path master switch. Normally True: the
# calendar builders (daily / monthly / weekly) assemble their per-bucket
# aggregates through the module-level BucketCache and recompute only the
# current/dirty slice. Flip to False to force the from-scratch wide-fetch
# path (the pre-#268 behavior) — the parity tests toggle it to prove the
# cached and from-scratch rebuilds are byte-identical, and the builders
# fall back to it automatically if the cache DB can't be opened.
_GROUP_A_CACHE_ENABLED = True


# === Module-level back-ref shims for helpers that STAY in bin/cctally ======
# Each shim resolves ``sys.modules['cctally'].X`` at CALL TIME (not bind
# time), so monkeypatches on cctally's namespace propagate into the moved
# code unchanged. `load_config` and `get_claude_session_entries` STAY as
# shims even though their natural homes are decentralized (_cctally_config
# / _cctally_cache) — tests monkeypatch them via `ns["X"]` (21 sites total,
# audited 2026-05-17); direct imports would silently bypass the patches.
# See spec §3.5 (carve-out) and §3.7 (stays-on-shim allowlist).
def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def _warn_alerts_bad_config_once(*args, **kwargs):
    return sys.modules["cctally"]._warn_alerts_bad_config_once(*args, **kwargs)


def _load_recorded_five_hour_windows(*args, **kwargs):
    return sys.modules["cctally"]._load_recorded_five_hour_windows(*args, **kwargs)


def _make_run_sync_now(*args, **kwargs):
    return sys.modules["cctally"]._make_run_sync_now(*args, **kwargs)


def _make_run_sync_now_locked(*args, **kwargs):
    return sys.modules["cctally"]._make_run_sync_now_locked(*args, **kwargs)


def _register_faulthandler_sigusr1():
    """Register a SIGUSR1 handler that dumps all-thread tracebacks (#268 M6.1).

    So any future dashboard spin (a sync thread stuck in a rebuild) is
    self-diagnosing — ``kill -USR1 <pid>`` prints every thread's stack to stderr
    — without needing a root py-spy. Guarded for platforms lacking SIGUSR1
    (Windows), where it is a silent no-op. Idempotent; returns True when the
    handler was registered, False when SIGUSR1 is unavailable."""
    import faulthandler
    import signal
    if not hasattr(signal, "SIGUSR1"):
        return False
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    return True


def _dashboard_self_heal_orphans(*, skip_sync):
    """Prune removed-worktree orphans from the cache using a throwaway
    connection. No-op under frozen (--no-sync) mode. Non-blocking on the
    cache lock (retries on the next cadence if contended). Never raises.

    #268 M5.2 (spec §7 / Codex F4): a prune that actually deleted rows rewrites
    history in place — which `MAX(id)` alone can't detect (deleting a NON-max
    row leaves the max unchanged). So on any real deletion, bump the
    cache-generation counter (a composite-signature leg, so the next rebuild
    can't idle-short-circuit) AND clear the Group A / session caches, forcing a
    correct cold recompute on the next tick. The prune runs AFTER the tick's
    publish on the same sync thread, so the very next tick recomputes."""
    if skip_sync:
        return None
    try:
        conn = open_cache_db()
        try:
            result = _prune_orphaned_cache_entries(conn, lock_timeout=None)
        finally:
            conn.close()
    except Exception:
        return None
    if result is not None and (result.pruned_entries or result.pruned_files):
        try:
            bump_generation()
            reset_group_a_state()
            reset_session_cache_state()
            # #269 M3.2 (spec §6): the shared per-weekref immutable-cost cache
            # (B1 trend/weekly-history + B3 forecast) rides the SAME prune-site
            # clear — a non-max deletion the reconcile's max-id regression check
            # can't catch. (B2 cache-report cache was dropped at the M2.0 gate,
            # so there is no reset_cache_report_state() to call here.)
            reset_weekref_cost_state()
            # #269 M4.5 (spec §14 Win 2): the projects-envelope per-(project,
            # week) cache rides the same prune-site clear for the same reason.
            reset_projects_env_state()
            # #271 M3 (spec §18): the Bug-K pre-credit segment cache rides the
            # same prune-site clear — a non-max deletion inside a closed
            # pre-credit window the reconcile's max-id regression check can't
            # catch.
            reset_bugk_segment_state()
            # #269 (final reviewer): the per-(project, week) cache clear above is
            # NOT sufficient on its own. `_build_projects_envelope` consults the
            # whole-envelope memo `_PROJECTS_ENV_MEMO` FIRST — and its memo_key is
            # `(max_id, max_wus_id, cw_key, weeks_back)`, which carries NO
            # generation counter. A prune that deletes only NON-max
            # `session_entries` rows leaves `max_id` unchanged, so the memo_key
            # still matches and the memo would stale-serve the pre-prune envelope
            # (still showing the deleted project + its cost) before the fresh
            # per-week cache path is ever reached. Clear the memo too so a real
            # prune actually changes the envelope output.
            _projects_reset_memo()
        except Exception:
            # Invalidation must never turn a successful prune into a failure;
            # a stale-cache tick is self-corrected once the signature next moves.
            pass
    return result


def _build_forecast_json_payload(*args, **kwargs):
    return sys.modules["cctally"]._build_forecast_json_payload(*args, **kwargs)


def _build_alert_payload_weekly(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_weekly(*args, **kwargs)


def _build_alert_payload_five_hour(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_five_hour(*args, **kwargs)


def _build_alert_payload_budget(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_budget(*args, **kwargs)


def _build_alert_payload_projected(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_projected(*args, **kwargs)


def _build_alert_payload_project_budget(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_project_budget(
        *args, **kwargs
    )


def _build_alert_payload_codex_budget(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_codex_budget(
        *args, **kwargs
    )


def _dispatch_alert_notification(*args, **kwargs):
    return sys.modules["cctally"]._dispatch_alert_notification(*args, **kwargs)


def doctor_gather_state(*args, **kwargs):
    return sys.modules["cctally"].doctor_gather_state(*args, **kwargs)


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


# === Cache-report settings validator (spec 2026-05-21 §6) ================
# Validates the optional ``config.json:cache_report`` block. Strict in
# v1: only ``anomaly_threshold_pp`` is settable, must be a plain int in
# ``[1, 100]`` (bool / float / string rejected — bool because it's an
# int subclass in Python and quietly accepting ``true`` for a numeric
# field is exactly the trip-up ``_validate_update_check_ttl_hours_value``
# protects against). HTTP write path raises ``_CacheReportConfigError``
# → ``_handle_post_settings`` maps to HTTP 400 + ``{error, field}``
# (matches the existing handler convention at lines 4587-4602; spec
# explicitly says 400, NOT 422).

@dataclass(frozen=True)
class _CacheReportSettings:
    anomaly_threshold_pp: int


class _CacheReportConfigError(Exception):
    """Validation error for the ``cache_report`` config block.

    ``field`` carries the offending key path (``anomaly_threshold_pp`` or
    the unknown-key name) so the JSON 400 response can surface it.
    """
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


_CACHE_REPORT_ALLOWED_KEYS = frozenset({"anomaly_threshold_pp"})


def _validate_cache_report_settings(block: dict) -> dict:
    """Validate a ``cache_report`` config block.

    Pure function. Raises ``_CacheReportConfigError`` on invalid input;
    returns a dict containing ONLY the keys that were present in the
    input (validated). Callers merge the result into the existing
    persisted block instead of replacing it wholesale — this mirrors
    the ``update.check`` partial-PUT pattern at
    ``_handle_post_settings`` (~line 5277) and prevents a combined save
    that omits ``anomaly_threshold_pp`` from clobbering a previously
    persisted user value with the default.

    v1 only accepts ``anomaly_threshold_pp`` — ``anomaly_window_days``
    stays hardcoded at 14 (spec §6.1; F10 from spec §10 tracks adding
    a configurable baseline window along with the UI-copy work).
    """
    if not isinstance(block, dict):
        raise _CacheReportConfigError(
            "cache_report must be an object", field="cache_report",
        )
    for key in block:
        if key not in _CACHE_REPORT_ALLOWED_KEYS:
            raise _CacheReportConfigError(
                f"unknown key in cache_report block: {key!r}",
                field=key,
            )
    validated: dict = {}
    if "anomaly_threshold_pp" in block:
        threshold = block["anomaly_threshold_pp"]
        # bool is an int subclass — reject it explicitly (mirrors the
        # update.check.ttl_hours precedent).
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise _CacheReportConfigError(
                "anomaly_threshold_pp must be an integer",
                field="anomaly_threshold_pp",
            )
        if threshold < 1 or threshold > 100:
            raise _CacheReportConfigError(
                "anomaly_threshold_pp must be in [1, 100]",
                field="anomaly_threshold_pp",
            )
        validated["anomaly_threshold_pp"] = threshold
    return validated


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

# Conversation live-tail watch loop (spec §4.1). Single-file stat poll: cheap.
_LIVE_TAIL_POLL_INTERVAL = 1.0      # seconds between stat polls of the open file(s)
_LIVE_TAIL_DEBOUNCE = 0.25          # settle window after first detected growth
_LIVE_TAIL_KEEPALIVE = 15.0         # idle keep-alive cadence (proxy guard)
_LIVE_TAIL_FILE_RESET_EVERY = 10    # re-resolve the session file set every N cycles


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




# === Cache-report envelope dataclasses (spec 2026-05-21) =================
# Snake_case fields are emitted verbatim into the SSE envelope so the React
# store can read ``state.cache_report.<field>`` without a key-transform pass
# (the envelope is intentionally snake_case end-to-end; see
# ``dashboard/web/src/types/envelope.ts:189``). Built by
# ``build_cache_report_snapshot()`` and shipped on the existing 5-minute
# sync cadence — no separate ``/api/cache-report`` endpoint.

# Hardcoded for v1; F10 tracks lifting via cache_report.anomaly_window_days config.
CACHE_REPORT_WINDOW_DAYS = 14
# Two concepts that happen to share a value today: the data window the
# panel renders vs. the baseline window the anomaly classifier reads
# back over. Split so F10 can lift the latter without dragging the
# former along.
CACHE_REPORT_ANOMALY_WINDOW_DAYS = 14

@dataclass(frozen=True)
class CacheReportDailyRow:
    date: str  # YYYY-MM-DD in display tz
    cache_hit_percent: float
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    saved_usd: float
    wasted_usd: float
    net_usd: float
    anomaly_triggered: bool
    anomaly_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CacheReportBreakdownRow:
    """One row of the by-project / by-model breakdown sub-cards."""
    key: str
    cache_hit_percent: float
    net_usd: float


@dataclass(frozen=True)
class CacheReportTodaySpotlight:
    """Today's spotlight card: hit %, baseline-median, Δ vs baseline,
    cumulative net / saved / wasted, anomaly state, and the count of
    baseline daily rows so the React panel can gate the
    "Building baseline · N/5 days" insufficient-baseline state."""
    date: str
    cache_hit_percent: float
    baseline_median_percent: float | None
    delta_pp: float | None
    net_usd: float
    saved_usd: float
    wasted_usd: float
    anomaly_triggered: bool
    anomaly_reasons: tuple[str, ...]
    baseline_daily_row_count: int


def _cache_report_snapshot_to_dict(cr: "CacheReportSnapshot | None") -> "dict | None":
    """Serialize a ``CacheReportSnapshot`` to the SSE envelope dict.

    Returns ``None`` when the snapshot is ``None`` (first tick before
    sync, or sub-build failure recorded on ``last_sync_error``). Snake-
    case keys throughout — the envelope is intentionally snake_case end
    -to-end per ``envelope.ts:189`` (no ``to_camel`` pass). Tuples are
    flattened to lists for JSON palatability.
    """
    if cr is None:
        return None
    return {
        "window_days": cr.window_days,
        "anomaly_threshold_pp": cr.anomaly_threshold_pp,
        "anomaly_window_days": cr.anomaly_window_days,
        "today": {
            "date": cr.today.date,
            "cache_hit_percent": cr.today.cache_hit_percent,
            "baseline_median_percent": cr.today.baseline_median_percent,
            "delta_pp": cr.today.delta_pp,
            "net_usd": cr.today.net_usd,
            "saved_usd": cr.today.saved_usd,
            "wasted_usd": cr.today.wasted_usd,
            "anomaly_triggered": cr.today.anomaly_triggered,
            "anomaly_reasons": list(cr.today.anomaly_reasons),
            "baseline_daily_row_count": cr.today.baseline_daily_row_count,
        },
        "days": [
            {
                "date": d.date,
                "cache_hit_percent": d.cache_hit_percent,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "cache_creation_tokens": d.cache_creation_tokens,
                "cache_read_tokens": d.cache_read_tokens,
                "saved_usd": d.saved_usd,
                "wasted_usd": d.wasted_usd,
                "net_usd": d.net_usd,
                "anomaly_triggered": d.anomaly_triggered,
                "anomaly_reasons": list(d.anomaly_reasons),
            }
            for d in cr.days
        ],
        "by_project": [
            {
                "key": b.key,
                "cache_hit_percent": b.cache_hit_percent,
                "net_usd": b.net_usd,
            }
            for b in cr.by_project
        ],
        "by_model": [
            {
                "key": b.key,
                "cache_hit_percent": b.cache_hit_percent,
                "net_usd": b.net_usd,
            }
            for b in cr.by_model
        ],
        "seven_day_net_usd": cr.seven_day_net_usd,
        "seven_day_anomaly_count": cr.seven_day_anomaly_count,
        "fourteen_day_counterfactual_usd": cr.fourteen_day_counterfactual_usd,
        "fourteen_day_efficiency_ratio": cr.fourteen_day_efficiency_ratio,
        "is_empty": cr.is_empty,
    }


@dataclass(frozen=True)
class CacheReportSnapshot:
    """The complete cache-report envelope block.

    ``days`` is newest-first, length ``≤ window_days``. ``by_project`` /
    ``by_model`` are sorted by ``abs(net_usd)`` descending and capped at
    6 entries (top 5 + ``(other)``). ``window_days`` is hardcoded at 14
    in v1; ``anomaly_threshold_pp`` is read from
    ``config.json:cache_report.anomaly_threshold_pp`` (default 15) via
    the dashboard sync thread.
    """
    window_days: int
    anomaly_threshold_pp: int
    anomaly_window_days: int
    today: CacheReportTodaySpotlight
    days: tuple[CacheReportDailyRow, ...]
    by_project: tuple[CacheReportBreakdownRow, ...]
    by_model: tuple[CacheReportBreakdownRow, ...]
    seven_day_net_usd: float
    seven_day_anomaly_count: int
    fourteen_day_counterfactual_usd: float
    fourteen_day_efficiency_ratio: float
    is_empty: bool


# === Cache-report snapshot builder (spec 2026-05-21 §5.2) ================
# Adapter from the I/O layer (``get_claude_session_entries`` +
# ``CLAUDE_MODEL_PRICING`` + ``_calculate_entry_cost``) into the kernel's
# pure ``_build_cache_report`` orchestrator. By-project + by-model
# breakdowns dedup through the kernel's ``_aggregate_cache_breakdown``
# (one path, one ``<synthetic>`` filter rule) so the two axes can't
# silently disagree on token totals when a session has both real and
# synthetic entries on the same project.

def _cache_report_load_kernel():
    """Lazy-load ``_lib_cache_report`` via the cctally ``_load_sibling``
    bridge so monkeypatch-driven test reloads of cctally see the same
    kernel module instance (matches the late-load pattern used by share /
    doctor helpers in this file)."""
    return sys.modules["cctally"]._load_sibling("_lib_cache_report")


def build_cache_report_snapshot(
    *,
    now_utc: dt.datetime,
    anomaly_threshold_pp: int,
    anomaly_window_days: int,
    display_tz: "ZoneInfo | None",
    skip_sync: bool = False,
) -> CacheReportSnapshot:
    """Build the ``cache_report`` envelope field from the session-entry cache.

    Pulls entries via ``get_claude_session_entries`` (uses the cache when
    warm, falls back to direct-JSONL parse on cache miss / lock
    contention — same chain the CLI uses). Delegates aggregation +
    anomaly classification to ``_lib_cache_report._build_cache_report``;
    shapes the result into a frozen ``CacheReportSnapshot``.

    ``window_days`` is hardcoded at 14 in v1 (spec §6.1 hardcodes
    ``anomaly_window_days`` too; ``anomaly_threshold_pp`` is the only
    user-configurable knob). F10 from spec §10 tracks making the window
    configurable, plus the UI-copy work it'd require.
    """
    crk = _cache_report_load_kernel()
    cctally_ns = sys.modules["cctally"]

    window_days = CACHE_REPORT_WINDOW_DAYS  # v1: hardcoded per spec §6.1.
    since = now_utc - dt.timedelta(days=window_days)

    entries = list(
        get_claude_session_entries(since, now_utc, project=None, skip_sync=skip_sync)
    )

    today_iso = now_utc.astimezone(
        display_tz if display_tz is not None else dt.timezone.utc
    ).strftime("%Y-%m-%d")

    if not entries:
        empty_today = CacheReportTodaySpotlight(
            date=today_iso,
            cache_hit_percent=0.0,
            baseline_median_percent=None,
            delta_pp=None,
            net_usd=0.0, saved_usd=0.0, wasted_usd=0.0,
            anomaly_triggered=False,
            anomaly_reasons=(),
            baseline_daily_row_count=0,
        )
        return CacheReportSnapshot(
            window_days=window_days,
            anomaly_threshold_pp=anomaly_threshold_pp,
            anomaly_window_days=anomaly_window_days,
            today=empty_today,
            days=(), by_project=(), by_model=(),
            seven_day_net_usd=0.0,
            seven_day_anomaly_count=0,
            fourteen_day_counterfactual_usd=0.0,
            fourteen_day_efficiency_ratio=0.0,
            is_empty=True,
        )

    pricing = cctally_ns.CLAUDE_MODEL_PRICING

    # Day-mode kernel expects entries with a ``usage`` dict (matches
    # ``UsageEntry``). ``get_claude_session_entries`` returns flat
    # ``_JoinedClaudeEntry`` objects, so wrap each into the right shape
    # before passing to the kernel. SimpleNamespace keeps the wrapper
    # pure-Python and avoids a new dataclass type just for the bridge.
    from types import SimpleNamespace as _NS
    day_entries = [
        _NS(
            timestamp=e.timestamp,
            model=e.model,
            cost_usd=e.cost_usd,
            usage={
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "cache_creation_input_tokens": e.cache_creation_tokens,
                "cache_read_input_tokens": e.cache_read_tokens,
            },
        )
        for e in entries
    ]

    result = crk._build_cache_report(
        day_entries,
        now_utc=now_utc,
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        display_tz=display_tz,
        pricing=pricing,
        mode="day",
        cost_calculator=_calculate_entry_cost,
    )

    # Pick out today's row (if any) and the baseline-daily-row count for
    # the spotlight. The spotlight median is computed against ALL rows
    # except today (cross-row reference; mirrors what the panel's
    # "Δ vs 14d median" label means). The median itself rides back on
    # ``result.today_baseline_median`` (EFF-3 — kernel computes it once
    # alongside the anomaly classifier so we don't re-walk the same
    # row set here).
    today_row = next((r for r in result.rows if r.date == today_iso), None)
    other_rows = [r for r in result.rows if r.date != today_iso]
    baseline_median = result.today_baseline_median

    baseline_daily_row_count = len(other_rows)

    # ``delta_pp`` sign convention (spec §4.2): "signed; negative = today
    # below median" → ``delta = today − baseline``. The empty-day branch
    # uses today_hit_pct = 0.0 so the formula degenerates to
    # ``0.0 − baseline_median``, which IS what users expect (a flat-zero
    # today read against a healthy 60% baseline yields delta=-60pp).
    today_hit_pct = today_row.cache_hit_percent if today_row is not None else 0.0
    delta_pp = (
        None if baseline_median is None
        else today_hit_pct - baseline_median
    )

    if today_row is None:
        today_spotlight = CacheReportTodaySpotlight(
            date=today_iso,
            cache_hit_percent=0.0,
            baseline_median_percent=baseline_median,
            delta_pp=delta_pp,
            net_usd=0.0, saved_usd=0.0, wasted_usd=0.0,
            anomaly_triggered=False,
            anomaly_reasons=(),
            baseline_daily_row_count=baseline_daily_row_count,
        )
    else:
        today_spotlight = CacheReportTodaySpotlight(
            date=today_iso,
            cache_hit_percent=today_row.cache_hit_percent,
            baseline_median_percent=baseline_median,
            delta_pp=delta_pp,
            net_usd=today_row.net_usd,
            saved_usd=today_row.saved_usd,
            wasted_usd=today_row.wasted_usd,
            anomaly_triggered=today_row.anomaly_triggered,
            anomaly_reasons=tuple(today_row.anomaly_reasons),
            baseline_daily_row_count=baseline_daily_row_count,
        )

    # Daily rows — newest first, capped at ``window_days``.
    #
    # Slice cap (spec §4.2 — "length up to ``window_days``"): the kernel's
    # ``since = now_utc - timedelta(days=window_days)`` rolling window
    # straddles midnight in any non-UTC ``display_tz`` (and in fact even
    # in UTC, since ``now_utc - 14d`` and ``now_utc`` flank the same
    # wall-clock minute on different calendar dates), so the kernel can
    # emit ``window_days + 1`` distinct calendar-date buckets. Capping
    # here (and not in the kernel) keeps the kernel agnostic of the
    # envelope's hard ceiling while honoring the contract every TS /
    # React consumer relies on (the sparkline ladder is hard-sized to
    # ``window_days`` points). Regression:
    # ``test_build_cache_report_snapshot_days_bounded_by_window``.
    #
    # Synthetic-today insertion: if the trailing window has older activity
    # but no entries for the current display-tz day, the kernel emits a
    # rows[] list whose newest row is yesterday (or older). Both React
    # consumers (``CacheSparkline`` and ``CacheNetBars``) treat the
    # rightmost element of ``days`` as "Today" purely positionally
    # (``ordered.length - 1`` / ``isLast ? 'Today'``), so without an
    # explicit today bucket they would mis-label the older row as Today.
    # Insert a zero-valued CacheReportDailyRow at position 0 (newest)
    # whenever ``today_row is None``. The zero values mirror the
    # ``today_spotlight`` synthesized above (kept in lock-step), and
    # contribute 0 to ``seven_day_*`` / ``fourteen_day_*`` rollups so
    # the rollup math stays untouched.
    raw_days_newest_first = sorted(
        result.rows, key=lambda r: r.date or "", reverse=True,
    )
    days_newest_first: list = []
    if today_row is None:
        # Build a zero-valued synthetic today row mirroring today_spotlight.
        days_newest_first.append(
            CacheReportDailyRow(
                date=today_iso,
                cache_hit_percent=0.0,
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                saved_usd=0.0,
                wasted_usd=0.0,
                net_usd=0.0,
                anomaly_triggered=False,
                anomaly_reasons=(),
            )
        )
    days_newest_first.extend(
        CacheReportDailyRow(
            date=r.date or "",
            cache_hit_percent=r.cache_hit_percent,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            cache_read_tokens=r.cache_read_tokens,
            saved_usd=r.saved_usd,
            wasted_usd=r.wasted_usd,
            net_usd=r.net_usd,
            anomaly_triggered=r.anomaly_triggered,
            anomaly_reasons=tuple(r.anomaly_reasons),
        )
        for r in raw_days_newest_first
    )
    days = tuple(days_newest_first[:window_days])

    # By-project + by-model breakdowns are window-wide aggregates (not
    # today-only) so the panel can surface the project / model carrying
    # the bulk of net savings across the trailing 14d. by-project walks
    # raw entries (project_path is per-entry, not on the day-model
    # buckets); by-model folds the per-row ``model_breakdowns`` already
    # produced by day-mode, which avoids re-running the tiered-pricing
    # math per entry. Both paths apply the same ``<synthetic>`` filter so
    # the axes can't silently disagree on token totals.
    #
    # Constrain both axes to the SAME calendar dates as ``days``: the
    # kernel's rolling window can emit ``window_days + 1`` distinct
    # display-tz buckets (see the slice-cap comment above), and ``days``
    # drops the oldest. Without the same drop here the by-project /
    # by-model cards would silently include the clipped 15th day and
    # their net totals stop reconciling against the visible table /
    # CacheNetBars in the modal. The filter mirrors the kernel's
    # bucket-key derivation (``entry.timestamp.astimezone(tz)``) so a
    # cache entry and its corresponding day row always agree on which
    # bucket they belong to.
    kept_dates = frozenset(r.date for r in days if r.date)
    bucket_tz = display_tz if display_tz is not None else dt.timezone.utc
    entries_in_window = [
        e for e in entries
        if e.timestamp.astimezone(bucket_tz).strftime("%Y-%m-%d") in kept_dates
    ]
    rows_in_window = [r for r in result.rows if r.date in kept_dates]
    by_project_rows = crk._aggregate_cache_breakdown(
        entries_in_window,
        key_fn=lambda e: (getattr(e, "project_path", None) or "(unknown)"),
        pricing=pricing,
        skip_synthetic=True,
    )
    by_model_rows = crk._aggregate_cache_breakdown_from_rows(
        rows_in_window,
        skip_synthetic=True,
    )
    by_project = tuple(
        CacheReportBreakdownRow(
            key=r.key, cache_hit_percent=r.cache_hit_percent, net_usd=r.net_usd,
        )
        for r in by_project_rows
    )
    by_model = tuple(
        CacheReportBreakdownRow(
            key=r.key, cache_hit_percent=r.cache_hit_percent, net_usd=r.net_usd,
        )
        for r in by_model_rows
    )

    # 7-day rollup: today + 6 prior. Walk by string date; ``days_newest_first``
    # is already in the right order.
    seven_day_rows = days[:7]
    seven_day_net_usd = stable_sum(r.net_usd for r in seven_day_rows)
    seven_day_anomaly_count = sum(
        1 for r in seven_day_rows if r.anomaly_triggered
    )

    # 14-day counterfactual: sum(saved_usd) across the window.
    fourteen_day_counterfactual_usd = stable_sum(r.saved_usd for r in days)
    fourteen_day_wasted_usd = stable_sum(r.wasted_usd for r in days)
    denom = fourteen_day_counterfactual_usd + abs(fourteen_day_wasted_usd)
    fourteen_day_efficiency_ratio = (
        (fourteen_day_counterfactual_usd / denom) if denom > 1e-9 else 0.0
    )

    return CacheReportSnapshot(
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        today=today_spotlight,
        days=days,
        by_project=by_project,
        by_model=by_model,
        seven_day_net_usd=seven_day_net_usd,
        seven_day_anomaly_count=seven_day_anomaly_count,
        fourteen_day_counterfactual_usd=fourteen_day_counterfactual_usd,
        fourteen_day_efficiency_ratio=fourteen_day_efficiency_ratio,
        is_empty=False,
    )


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


def _group_a_monthly_buckets(now_utc, *, n, range_start, display_tz):
    """Assemble the monthly panel's per-month ``BucketUsage`` list via the
    #268 Group A cache, or ``None`` to fall back to the wide fetch (spec §5.1).

    ``all_bucket_labels`` is the contiguous set of display-tz ``"%Y-%m"``
    months spanned by the wide window ``[range_start, now_utc]`` — from
    ``range_start``'s display-tz month (the oldest an entry in the window can
    bucket into, including the west-of-UTC boundary-spillover month) up to
    the current display-tz month — in ascending order. ``build_monthly_view``
    then reverses + caps to ``n``, dropping any spillover month exactly as
    the from-scratch path does. Each recompute fetches a ±2-day-padded window
    (clamped to the wide window) and filters ``_aggregate_monthly`` to the
    target month, so the boundary follows ``display_tz`` and per-bucket
    first-seen model order matches the wide pass byte-for-byte.
    """
    if not _GROUP_A_CACHE_ENABLED:
        return None
    try:
        cache_conn = open_cache_db()
    except Exception:
        return None
    try:
        def _tz_month(instant):
            local = (
                instant.astimezone(display_tz) if display_tz is not None
                # internal fallback: host-local intentional
                else instant.astimezone()
            )
            return local.year, local.month

        oldest_y, oldest_m = _tz_month(range_start)
        newest_y, newest_m = _tz_month(now_utc)
        # Contiguous months oldest → newest (ascending).
        labels: list[str] = []
        y, m = oldest_y, oldest_m
        while (y, m) <= (newest_y, newest_m):
            labels.append(f"{y:04d}-{m:02d}")
            m += 1
            if m == 13:
                m = 1
                y += 1
        current_label = f"{newest_y:04d}-{newest_m:02d}"
        tz_sig = display_tz.key if display_tz is not None else "local"

        def _month_bounds(label):
            yy, mm = (int(x) for x in label.split("-"))
            start = dt.datetime(yy, mm, 1, tzinfo=dt.timezone.utc)
            ny, nm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
            nxt = dt.datetime(ny, nm, 1, tzinfo=dt.timezone.utc)
            return start, nxt

        def _fetch(label):
            start, nxt = _month_bounds(label)
            # ±2 days of slack covers the display-tz month boundary for any
            # offset; clamp to the wide window so nothing after now_utc /
            # before range_start leaks in.
            lo = max(start - dt.timedelta(days=2), range_start)
            hi = min(nxt + dt.timedelta(days=2), now_utc)
            if hi <= lo:
                return []
            return iter_entries(cache_conn, lo, hi)

        def _agg_one(label, entries):
            for b in _aggregate_monthly(entries, tz=display_tz):
                if b.bucket == label:
                    return b
            return None

        def _end_of(label):
            _start, nxt = _month_bounds(label)
            return nxt + dt.timedelta(days=2)

        # #271: current-bucket accumulator inputs. `_membership` IS
        # `_aggregate_monthly`'s own key (display-tz `%Y-%m`);
        # `_all_fetch`/`_delta_fetch` reuse `_fetch`'s ±2-day-padded month
        # window (clamped to now_utc) via the `(id, UsageEntry)` delta sibling.
        def _membership(e):
            local = (
                e.timestamp.astimezone(display_tz) if display_tz is not None
                # internal fallback: host-local intentional
                else e.timestamp.astimezone()
            )
            return local.strftime("%Y-%m") == current_label

        def _current_window(label):
            start, nxt = _month_bounds(label)
            lo = max(start - dt.timedelta(days=2), range_start)
            hi = min(nxt + dt.timedelta(days=2), now_utc)
            return lo, hi

        def _all_fetch(label):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(cache_conn, lo, hi)

        def _delta_fetch(label, after_id, after_ts):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(
                cache_conn, lo, hi, after_id=after_id, after_ts=after_ts)

        buckets = build_cached_group_a(
            "monthly",
            cache_conn=cache_conn,
            all_bucket_labels=labels,
            current_label=current_label,
            bucket_end_of=_end_of,
            fetch_bucket_entries=_fetch,
            aggregate_one=_agg_one,
            extra_signature=("monthly", tz_sig),
            use_current_accumulator=True,
            now_utc=now_utc,
            current_all_fetch=_all_fetch,
            current_delta_fetch=_delta_fetch,
            membership_of=_membership,
        )
        return tuple(buckets)
    finally:
        cache_conn.close()


def _dashboard_build_monthly_periods(conn: "sqlite3.Connection",
                                     now_utc: "dt.datetime",
                                     *, n: int = 12,
                                     skip_sync: bool = False,
                                     use_group_a_cache: bool = False,
                                     display_tz: "ZoneInfo | None" = None) -> "list[MonthlyPeriodRow]":
    """Latest n calendar months as MonthlyPeriodRow, newest-first.

    Thin wrapper around ``build_monthly_view`` (spec §6.2). The view
    owns the aggregator call, boundary-spillover drop, delta_cost_pct
    derivation, and ``is_current`` flagging — this function just
    fetches the trailing window of entries and returns ``view.rows``.

    The sync thread separately captures ``view.total_cost_usd`` /
    ``view.total_tokens`` for the envelope; see
    ``_tui_build_snapshot`` and ``DataSnapshot.monthly_total_*``.
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

    # #268 Group A cache: assemble the per-month BucketUsage list through the
    # module cache (recompute only the current + watermark-dirty months;
    # serve immutable past months from memory) and hand it to
    # build_monthly_view as aggregated_override. Gated on ``use_group_a_cache``
    # — set ONLY by the sync-thread rebuild, never by ``skip_sync`` (the
    # share-period-override path also runs ``skip_sync=True`` but on an HTTP
    # thread with a shifted PAST now, which would pollute the shared cache with
    # a partial past-month bucket — see ``_dashboard_build_daily_panel``). Every
    # non-sync-thread caller falls through to the from-scratch wide fetch. Also
    # falls back when disabled / cache DB unavailable.
    aggregated_override = (
        _group_a_monthly_buckets(
            now_utc, n=n, range_start=range_start, display_tz=display_tz,
        )
        if use_group_a_cache else None
    )
    c = _cctally()
    if aggregated_override is None:
        entries = get_entries(range_start, range_end, skip_sync=skip_sync)
        view = c.build_monthly_view(entries, now_utc=now_utc, n=n,
                                    display_tz=display_tz)
    else:
        view = c.build_monthly_view((), now_utc=now_utc, n=n,
                                    display_tz=display_tz,
                                    aggregated_override=aggregated_override)
    return list(view.rows)


def _group_a_weekly_buckets(stats_conn, now_utc, *, weeks):
    """Assemble the weekly panel's per-week ``BucketUsage`` list via the #268
    Group A cache, or ``None`` to fall back to the wide fetch (spec §5.1).

    ``all_bucket_labels`` = each SubWeek's ``start_date.isoformat()`` in
    ascending order (matching ``_aggregate_weekly``'s sorted-key output);
    ``current_label`` = the SubWeek containing ``now_utc`` (always recomputed
    as the open week). Each recompute fetches ``[week.start_ts, min(week.end_ts,
    now_utc)]`` (clamped to now, matching the wide fetch's ``range_end`` bound)
    and runs ``_aggregate_weekly`` over the FULL ``weeks`` list — extracting
    the target week's bucket — so overlapping SubWeeks (reset-day drift) keep
    their first-match-wins assignment byte-for-byte with the wide pass.

    Weekly is the multi-table case (spec §5.1): the SubWeek boundaries derive
    from ``weekly_usage_snapshots`` + the reset-event tables, so the cached raw
    aggregate could shift when any of those move. ``extra_signature`` folds in
    ``MAX(weekly_usage_snapshots.id)`` / ``MAX(weekly_cost_snapshots.id)`` /
    the reset-event change-signal — a change to any full-invalidates the weekly
    namespace (the scoped M2.4 fallback: recompute-all on a weekly-relevant
    change, cache-hit only when nothing weekly-relevant moved — still the idle
    win, and trivially byte-identical). Pure session-entry adds stay per-week
    via the watermark. The overlay / Bug-K presentation reruns fresh each tick,
    so a snapshot change is reflected even on a cache-served bucket.
    """
    if not _GROUP_A_CACHE_ENABLED:
        return None
    try:
        cache_conn = open_cache_db()
    except Exception:
        return None
    try:
        sw_by_label = {w.start_date.isoformat(): w for w in weeks}
        labels = [w.start_date.isoformat() for w in weeks]  # ascending
        # current_label = the SubWeek that contains now_utc (the open week);
        # fall back to the newest week if none contains it.
        current_label = None
        for w in weeks:
            try:
                s = parse_iso_datetime(w.start_ts, "week.start_ts")
                e = parse_iso_datetime(w.end_ts, "week.end_ts")
            except ValueError:
                continue
            if s <= now_utc < e:
                current_label = w.start_date.isoformat()
                break
        if current_label is None:
            current_label = max(
                weeks, key=lambda w: w.start_date
            ).start_date.isoformat()

        # Weekly-relevant signature legs: any change full-invalidates.
        extra_signature = (
            _snapshot_max_id(stats_conn, "weekly_usage_snapshots"),
            _snapshot_max_id(stats_conn, "weekly_cost_snapshots"),
            _snapshot_reset_sig(stats_conn),
        )

        def _fetch(label):
            w = sw_by_label[label]
            s = parse_iso_datetime(w.start_ts, "week.start_ts")
            e = parse_iso_datetime(w.end_ts, "week.end_ts")
            hi = min(e, now_utc)  # cap the open week at now, like the wide fetch
            if hi <= s:
                return []
            return iter_entries(cache_conn, s, hi)

        def _agg_one(label, entries):
            # Aggregate against the FULL weeks list so overlapping SubWeeks
            # resolve first-match-wins exactly as the wide pass does; extract
            # the target week's bucket.
            for b in _aggregate_weekly(entries, weeks):
                if b.bucket == label:
                    return b
            return None

        def _end_of(label):
            w = sw_by_label[label]
            try:
                return parse_iso_datetime(w.end_ts, "week.end_ts")
            except ValueError:
                return None

        # #271: current-bucket accumulator inputs. Membership REUSES
        # _aggregate_weekly's exact bisect + first-match-wins assignment
        # (bin/_lib_aggregators.py::_week_key_or_none) — the parsed-bounds list
        # is built identically (same order, same parse) so an entry in an
        # overlap region (reset-day drift) keys to the SAME week the full pass
        # assigns it. _all_fetch/_delta_fetch reuse _fetch's
        # [week.start, min(week.end, now_utc)] window via the (id, UsageEntry)
        # delta sibling.
        _parsed_bounds = [
            (parse_iso_datetime(w.start_ts, "week.start_ts"),
             parse_iso_datetime(w.end_ts, "week.end_ts"),
             w.start_date.isoformat())
            for w in weeks
        ]
        _bound_starts = [b[0] for b in _parsed_bounds]

        def _week_key(e):
            ts = e.timestamp
            idx = bisect.bisect_right(_bound_starts, ts) - 1
            if idx < 0:
                return None
            while idx > 0 and (
                _parsed_bounds[idx - 1][0] <= ts < _parsed_bounds[idx - 1][1]
            ):
                idx -= 1
            s_dt, e_dt, key = _parsed_bounds[idx]
            return key if s_dt <= ts < e_dt else None

        def _membership(e):
            return _week_key(e) == current_label

        def _current_window(label):
            w = sw_by_label[label]
            s = parse_iso_datetime(w.start_ts, "week.start_ts")
            e = parse_iso_datetime(w.end_ts, "week.end_ts")
            return s, min(e, now_utc)

        def _all_fetch(label):
            s, hi = _current_window(label)
            return [] if hi <= s else iter_entries_with_id(cache_conn, s, hi)

        def _delta_fetch(label, after_id, after_ts):
            s, hi = _current_window(label)
            return [] if hi <= s else iter_entries_with_id(
                cache_conn, s, hi, after_id=after_id, after_ts=after_ts)

        buckets = build_cached_group_a(
            "weekly",
            cache_conn=cache_conn,
            all_bucket_labels=labels,
            current_label=current_label,
            bucket_end_of=_end_of,
            fetch_bucket_entries=_fetch,
            aggregate_one=_agg_one,
            extra_signature=extra_signature,
            use_current_accumulator=True,
            now_utc=now_utc,
            current_all_fetch=_all_fetch,
            current_delta_fetch=_delta_fetch,
            membership_of=_membership,
        )
        return tuple(buckets)
    finally:
        cache_conn.close()


def _dashboard_build_weekly_periods(conn: "sqlite3.Connection",
                                    now_utc: "dt.datetime",
                                    *, n: int = 12,
                                    skip_sync: bool = False,
                                    use_group_a_cache: bool = False,
                                    use_bugk_segment_cache: bool = False) -> "list[WeeklyPeriodRow]":
    """Latest n subscription weeks as WeeklyPeriodRow, newest-first.

    Thin builder-using prelude + Bug-K pre-credit synthesis on top of
    ``view.rows``. ``build_weekly_view`` (in ``bin/_lib_view_models.py``)
    owns the bucket+overlay walk — this function calls it, swaps two
    presentation fields (``label`` ← ``display_start_date`` for post-
    early-reset weeks; ``is_current`` ← SubWeek-containing-now_utc with
    snapshot fallback so the "Now" pill tracks wall time), layers Bug-K
    pre-credit synthesized rows over the natural rows, then recomputes
    ``delta_cost_pct`` newest-first so the synthesized rows participate
    in the deltas.

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
    as_of_utc = (
        range_end.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    # #268 Group A cache: assemble the per-week BucketUsage list through the
    # module cache (recompute only the current + watermark-dirty weeks; serve
    # immutable past weeks from memory) and hand it to build_weekly_view as
    # aggregated_override. Gated on ``use_group_a_cache`` — set ONLY by the
    # sync-thread rebuild, never by ``skip_sync``. The share-period-override
    # path also runs ``skip_sync=True`` but on an HTTP thread with a shifted
    # PAST now; routing it through the shared cache would clamp the
    # now_override-current week to a partial aggregate and cache it under a real
    # PAST-week label (weekly is the worst case — its extra_signature is
    # stats-only, so nothing invalidates the polluted week until a data change
    # lands). Every non-sync-thread caller falls through to the from-scratch
    # wide fetch, as does the cache-disabled / cache-unavailable case. The
    # overlay + Bug-K + delta + is_current presentation always reruns fresh over
    # the assembled list.
    aggregated_override = (
        _group_a_weekly_buckets(conn, now_utc, weeks=weeks)
        if use_group_a_cache else None
    )
    c = _cctally()
    if aggregated_override is None:
        entries = get_entries(fetch_start, range_end, skip_sync=skip_sync)
        view = c.build_weekly_view(
            conn, entries, weeks=weeks, now_utc=now_utc,
            display_tz=None, as_of_utc=as_of_utc,
        )
    else:
        view = c.build_weekly_view(
            conn, (), weeks=weeks, now_utc=now_utc,
            display_tz=None, as_of_utc=as_of_utc,
            aggregated_override=aggregated_override,
        )
    if not view.rows:
        return []

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

    # SubWeek lookup by (start_ts, end_ts) — the builder identifies each row
    # by these ISO strings on ``WeeklyPeriodRow``, which match SubWeek 1:1
    # post _aggregate_weekly invariant.
    sw_by_window = {(w.start_ts, w.end_ts): w for w in weeks}

    # Convert builder rows (newest-first) → oldest-first so the existing
    # Bug-K insertion logic (oldest-first indices) stays unchanged. Override
    # the two presentation fields that diverge between CLI/share (which use
    # ``start_date`` + "now in window" semantics) and the dashboard panel
    # (which uses ``display_start_date`` + "current SubWeek" semantics).
    rows_oldest_first: list[WeeklyPeriodRow] = []
    for r in reversed(view.rows):
        sw = sw_by_window.get((r.week_start_at, r.week_end_at))
        if sw is not None:
            # Label = MM-DD of the week's display_start_date — for non-reset
            # weeks this equals start_date; for post-early-reset weeks the
            # post-processor shifts it forward to the effective reset moment
            # so the user sees the date the week actually began (04-23 vs the
            # API-derived backdated 04-18).
            r.label = sw.display_start_date.strftime("%m-%d")
            # is_current keys on start_date (the bucket / lookup key) on both
            # sides of the comparison; display_start_date may diverge for
            # reset-event weeks but that is intentional — display vs. lookup
            # are kept separate.
            r.is_current = (sw.start_date.isoformat() == cur_week_start)
        # delta_cost_pct: builder computed it in asc order; reset and
        # recompute newest-first below AFTER Bug-K rows merge in, so the
        # synthesized rows participate in the deltas.
        r.delta_cost_pct = None
        rows_oldest_first.append(r)

    # Bug K (v1.7.2 round-5): synthesize a pre-credit segment row for
    # each in-place credit event. Without this the credited week shows
    # ONLY the post-credit segment ($134 on live data) and the bulk of
    # the week's cost (~$372 in entries before the credit moment) is
    # invisible to the user.
    #
    # _apply_reset_events_to_subweeks shifts the credited SubWeek's
    # start_ts to ``effective_reset_at_utc``, so _aggregate_weekly's
    # bucket for that SubWeek already covers ONLY the post-credit
    # interval. We rebuild the pre-credit bucket here by fetching the
    # ``[original_start, effective)`` window directly (#268: no longer a
    # wide ``entries`` list on the cached path) and re-aggregating cost /
    # tokens / per-model.
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

            # Aggregate entries in [original_start, effective). Fetch this
            # window directly (skip_sync=True — the rebuild already ingested,
            # or the fallback's wide fetch above did) rather than filtering a
            # wide `entries` list, so Bug-K works identically on the cached
            # path (where no wide entry list exists) and the fallback path.
            #
            # #271 §18: the [original_start, effective) window is a CLOSED past
            # interval (effective is a historical credit moment), so this folded
            # aggregate is IMMUTABLE — cache it byte-identically (the same
            # "re-aggregate immutable history every tick" pattern the #269
            # weekref cost cache fixed). `_compute_pre_segment` is the exact
            # from-scratch fetch+fold closure; on the sync thread
            # (`use_bugk_segment_cache=True`) it routes through
            # `cached_bugk_segment` (get-or-compute over the canonical window
            # key), on EVERY other caller (CLI / share / tests / reconcile-
            # failed) it computes directly — byte-unchanged. The `used_pct`
            # snapshot query + WeeklyPeriodRow build BELOW always rerun fresh
            # from the segment (do NOT cache the row — Codex-BK-5).
            def _compute_pre_segment(_orig=original_start_dt, _eff=eff_dt):
                pi = po = pcc = pcr = 0
                pcost = 0.0
                pmodels: dict[str, float] = {}
                pcount = 0
                for e in get_entries(_orig, _eff, skip_sync=True):
                    if _orig <= e.timestamp < _eff:
                        usage = e.usage
                        pi  += usage.get("input_tokens", 0)
                        po  += usage.get("output_tokens", 0)
                        pcc += usage.get("cache_creation_input_tokens", 0)
                        pcr += usage.get("cache_read_input_tokens", 0)
                        c = _calc(
                            e.model, usage, mode="auto", cost_usd=e.cost_usd,
                        )
                        pcost += c
                        pmodels[e.model] = pmodels.get(e.model, 0.0) + c
                        pcount += 1
                # Codex-BK-4: freeze `models` as a tuple of (model, cost) in
                # first-seen (dict-insertion) order so the row's stable cost-desc
                # sort tie-order can't be mutated after caching.
                return BugKSegment(
                    input=pi, output=po, cache_create=pcc, cache_read=pcr,
                    cost=pcost, models=tuple(pmodels.items()), entry_count=pcount,
                )

            if use_bugk_segment_cache:
                seg = cached_bugk_segment(
                    key=_bugk_key(original_start_dt, eff_dt),
                    compute=_compute_pre_segment,
                )
            else:
                seg = _compute_pre_segment()
            pre_input = seg.input
            pre_output = seg.output
            pre_cc = seg.cache_create
            pre_cr = seg.cache_read
            pre_cost = seg.cost
            pre_models = seg.models  # ((model, cost), ...) in first-seen order
            pre_entry_count = seg.entry_count
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
            # `pre_models` is a first-seen-order tuple of (model, cost); the
            # stable cost-desc sort preserves that tie-order byte-for-byte,
            # identical to the pre-#271 `sorted(dict.items(), ...)`.
            pre_model_breakdowns = [
                {"modelName": m, "cost": c}
                for m, c in sorted(pre_models, key=lambda kv: -kv[1])
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


def _dashboard_build_blocks_view(conn: "sqlite3.Connection",
                                  now_utc: "dt.datetime",
                                  *,
                                  week_start_at: "dt.datetime",
                                  week_end_at: "dt.datetime",
                                  skip_sync: bool = False,
                                  display_tz: "ZoneInfo | None" = None):
    """Build a ``BlocksView`` for the dashboard Blocks panel window
    ``[week_start_at, week_end_at)`` (issue #56).

    Two-layer composition (mirrors `_dashboard_build_daily_panel`'s
    pattern):

    1. ``build_blocks_view`` (in ``bin/_lib_view_models.py``) is the
       data plane — `_group_entries_into_blocks` plus per-block model
       enrichment plus totals derivation.
    2. This function is the presentation adapter — owns the
       recorded-windows-widening trick (loads reset windows from
       ``[start - BLOCK_DURATION, end + BLOCK_DURATION]`` so a recorded
       reset just outside the visible window can still anchor blocks
       inside it) and the strict-window entry filter.

    Returning the full ``BlocksView`` (rows + totals) lets the sync
    thread populate ``DataSnapshot.blocks_total_cost_usd`` /
    ``blocks_total_tokens`` for the envelope without a second pass.
    """
    # Widen the entry window slightly so a recorded-reset window straddling
    # the boundary still picks up its entries.
    fetch_start = week_start_at - BLOCK_DURATION
    fetch_end = week_end_at + BLOCK_DURATION
    entries = get_entries(fetch_start, fetch_end, skip_sync=skip_sync)
    entries = [e for e in entries if week_start_at <= e.timestamp < week_end_at]

    recorded_windows, block_start_overrides, canonical_intervals = (
        _load_recorded_five_hour_windows(fetch_start, fetch_end)
    )
    c = _cctally()
    return c.build_blocks_view(
        entries,
        now_utc=now_utc,
        recorded_windows=recorded_windows,
        block_start_overrides=block_start_overrides,
        canonical_intervals=canonical_intervals,
        range_start=week_start_at,
        range_end=week_end_at,
        display_tz=display_tz,
        mode="auto",
    )


def _dashboard_build_blocks_panel(conn: "sqlite3.Connection",
                                   now_utc: "dt.datetime",
                                   *,
                                   week_start_at: "dt.datetime",
                                   week_end_at: "dt.datetime",
                                   skip_sync: bool = False,
                                   display_tz: "ZoneInfo | None" = None) -> "list[BlocksPanelRow]":
    """Activity blocks (`is_gap=False`) inside ``[week_start_at, week_end_at)``,
    newest-first.

    Thin presentation shim over ``_dashboard_build_blocks_view`` —
    returns just ``view.rows`` so existing call sites (sync thread,
    share-data override resolver, monkeypatch surfaces) keep their
    ``list[BlocksPanelRow]`` contract.
    """
    view = _dashboard_build_blocks_view(
        conn, now_utc,
        week_start_at=week_start_at,
        week_end_at=week_end_at,
        skip_sync=skip_sync,
        display_tz=display_tz,
    )
    return list(view.rows)


def _group_a_daily_buckets(now_utc, *, n, display_tz):
    """Assemble the daily panel's per-day ``BucketUsage`` list via the #268
    Group A cache, or ``None`` to signal the caller should take the
    from-scratch wide-fetch path (spec §5.1).

    Returns a tuple of ``BucketUsage`` in ascending date order (matching
    ``_aggregate_daily``'s sorted output), so ``build_daily_view`` consumes
    it as ``aggregated_override`` with no reorder. Each recompute fetches a
    ±1-day-padded window and filters ``_aggregate_daily`` to the target date,
    so the day boundary follows ``display_tz`` exactly (DST-safe) and the
    per-bucket first-seen model order reproduces the wide pass byte-for-byte.
    ``None`` is returned when the cache path is disabled or the cache DB is
    unavailable — the builder then does the original wide aggregation.
    """
    if not _GROUP_A_CACHE_ENABLED:
        return None
    try:
        cache_conn = open_cache_db()
    except Exception:
        return None
    try:
        # The from-scratch path fetches exactly [range_start, now_utc]; the
        # per-day fetches below clamp to the SAME window so the current day's
        # bucket never picks up an entry after now_utc (and nothing outside
        # the wide window leaks in) — byte-identical to the wide fetch.
        range_start = now_utc - dt.timedelta(days=n + 1)
        today_local = (
            now_utc.astimezone(display_tz) if display_tz is not None
            # internal fallback: host-local intentional
            else now_utc.astimezone()
        ).date()
        # Contiguous n-day window, oldest → newest (ascending, so the
        # assembled list matches _aggregate_daily's sorted-key order).
        labels = [
            (today_local - dt.timedelta(days=i)).isoformat()
            for i in range(n - 1, -1, -1)
        ]
        current_label = today_local.isoformat()
        tz_sig = display_tz.key if display_tz is not None else "local"

        def _fetch(label):
            d = dt.date.fromisoformat(label)
            base = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
            # ±1 day of slack fully covers the display-tz day for any tz
            # offset; the aggregate filter below selects exactly the label's
            # entries, so over-fetch is harmless and DST-robust. Clamp to the
            # wide [range_start, now_utc] window for exact parity.
            lo = max(base - dt.timedelta(days=1), range_start)
            hi = min(base + dt.timedelta(days=2), now_utc)
            if hi <= lo:
                return []
            return iter_entries(cache_conn, lo, hi)

        def _agg_one(label, entries):
            for b in _aggregate_daily(entries, tz=display_tz):
                if b.bucket == label:
                    return b
            return None

        def _end_of(label):
            d = dt.date.fromisoformat(label)
            # Over-estimate the day's UTC end (true end ≤ (d+1) 12:00 UTC for
            # any tz); over-marking dirty is safe, under-marking is not.
            return (
                dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
                + dt.timedelta(days=2)
            )

        # #271: current-bucket accumulator inputs. `_membership` IS
        # `_aggregate_daily`'s own key (display-tz `%Y-%m-%d`), so the id-bounded
        # fetch keeps exactly the entries the full pass assigns to the current
        # day, byte-identically. `_all_fetch`/`_delta_fetch` reuse `_fetch`'s
        # ±1-day-padded window (clamped to now_utc) via the `(id, UsageEntry)`
        # delta sibling.
        def _membership(e):
            local = (
                e.timestamp.astimezone(display_tz) if display_tz is not None
                # internal fallback: host-local intentional
                else e.timestamp.astimezone()
            )
            return local.strftime("%Y-%m-%d") == current_label

        def _current_window(label):
            d = dt.date.fromisoformat(label)
            base = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
            lo = max(base - dt.timedelta(days=1), range_start)
            hi = min(base + dt.timedelta(days=2), now_utc)
            return lo, hi

        def _all_fetch(label):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(cache_conn, lo, hi)

        def _delta_fetch(label, after_id, after_ts):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(
                cache_conn, lo, hi, after_id=after_id, after_ts=after_ts)

        buckets = build_cached_group_a(
            "daily",
            cache_conn=cache_conn,
            all_bucket_labels=labels,
            current_label=current_label,
            bucket_end_of=_end_of,
            fetch_bucket_entries=_fetch,
            aggregate_one=_agg_one,
            extra_signature=("daily", tz_sig),
            use_current_accumulator=True,
            now_utc=now_utc,
            current_all_fetch=_all_fetch,
            current_delta_fetch=_delta_fetch,
            membership_of=_membership,
        )
        return tuple(buckets)
    finally:
        cache_conn.close()


def _dashboard_build_daily_panel(conn: "sqlite3.Connection",
                                  now_utc: "dt.datetime",
                                  *,
                                  n: int = 30,
                                  skip_sync: bool = False,
                                  use_group_a_cache: bool = False,
                                  display_tz: "ZoneInfo | None" = None) -> "list[DailyPanelRow]":
    """Latest n display-tz dates as DailyPanelRow, newest-first.

    Two-layer composition (spec §4.4, §6.1):

    1. ``build_daily_view`` (in ``bin/_lib_view_models.py``) is the
       data plane — gap-free rows + totals derived from the
       aggregator output.
    2. This function is the presentation adapter — materializes the
       contiguous N-day calendar window (gap days as zero-cost rows
       so the heatmap shows them as faded cells), fills the
       presentation-only ``label`` (``MM-DD``) and
       ``intensity_bucket`` (quintile via
       ``_compute_intensity_buckets``) fields.

    Bucketing and the ``is_today`` reference both follow
    ``display_tz`` so users on a non-host display zone see days
    grouped consistently with the rest of the UI.

    Sync-thread totals: the caller sums over the materialized rows
    this function returns and stashes the result on
    ``DataSnapshot.daily_total_cost_usd`` / ``daily_total_tokens`` for
    the envelope adapter. Sum-over-visible-rows preserves the
    structural-equality invariant the dashboard footer reads
    (`total === rows.reduce(...)`); gap days carry ``cost_usd=0.0`` /
    ``total_tokens=0`` so the sum stays gap-free semantically. Doing
    it at the sync-thread site keeps this function's return type
    stable (preserving the dashboard / TUI / test fixture monkeypatch
    surface that consumes a plain ``list[DailyPanelRow]``).
    """
    # Wide trailing window — n days of slack on either side keeps it
    # forgiving of tz boundary issues.
    range_start = now_utc - dt.timedelta(days=n + 1)
    range_end = now_utc

    # #268 Group A cache: assemble the per-day BucketUsage list through the
    # module-level cache (recompute only the current + watermark-dirty days;
    # serve immutable past days from memory) and hand it to build_daily_view
    # as ``aggregated_override`` so every downstream derivation stays
    # single-sourced. Gated on ``use_group_a_cache`` — set ONLY by the
    # sync-thread rebuild (``_tui_build_snapshot``), which runs on a
    # process-consistent ``now``. NOT gated on ``skip_sync``: the
    # share-period-override path (``_share_apply_period_override``) also runs
    # ``skip_sync=True`` but on an HTTP handler thread with a shifted (PAST)
    # ``now_override`` — routing it through the shared module cache would clamp
    # the now_override-current bucket to a partial aggregate and cache it under
    # a real PAST-period label, truncating the live dashboard's past day (and
    # would mutate the module cache unlocked, off the sync thread). So every
    # non-sync-thread caller (default ``use_group_a_cache=False``) takes the
    # original from-scratch wide fetch. Also falls back when the cache is
    # disabled (parity tests) or the cache DB can't be opened.
    aggregated_override = (
        _group_a_daily_buckets(now_utc, n=n, display_tz=display_tz)
        if use_group_a_cache else None
    )

    c = _cctally()
    if aggregated_override is None:
        entries = get_entries(range_start, range_end, skip_sync=skip_sync)
        view = c.build_daily_view(entries, now_utc=now_utc,
                                  display_tz=display_tz)
    else:
        view = c.build_daily_view((), now_utc=now_utc,
                                  display_tz=display_tz,
                                  aggregated_override=aggregated_override)
    if not view.rows:
        return []

    # Materialize the contiguous N-day window. ``view.rows`` is gap-free
    # (newest-first) and carries the data-plane fields; the adapter
    # overlays it onto the calendar window and fills the presentation-
    # only ``label`` / ``intensity_bucket`` (which the builder left at
    # dataclass defaults per spec §4.4).
    rows_by_date = {r.date: r for r in view.rows}
    today_local = (
        now_utc.astimezone(display_tz) if display_tz is not None
        # internal fallback: host-local intentional
        else now_utc.astimezone()
    ).date()

    rows: list[DailyPanelRow] = []
    for i in range(n):
        d = today_local - dt.timedelta(days=i)
        date_str = d.isoformat()
        existing = rows_by_date.get(date_str)
        if existing is not None:
            # Use the view-model row but fill the presentation-only
            # ``label`` (intensity_bucket is set by
            # ``_compute_intensity_buckets`` below).
            rows.append(dataclasses.replace(existing, label=date_str[5:]))
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


# --- Projects panel / modal (spec 2026-05-19-projects-panel-design.md) ------
#
# Per-tick projects envelope builder. Runs on the sync thread that
# populates ``DataSnapshot``; the dashboard's pure ``snapshot_to_envelope``
# reads it back unchanged and assigns to ``envelope["projects"]`` so the
# serializer stays DB-free.
#
# See spec §5.2 (envelope shape), §6.2 (signatures), §6.4 (memoization),
# §9.2 (R-PROJ1..R-PROJ5 reconcile invariants).
#
# Identity:
#   - ``key``         = disambiguated ``ProjectKey.display_key`` via
#                       ``_lib_render._project_disambiguate_labels``.
#                       Stable within a single envelope (a `foo` collision
#                       resolves to `foo (parent_dir)`).
#   - ``bucket_path`` = canonical equality key (``ProjectKey.bucket_path``)
#                       — the absolute on-disk path. Privacy-sensitive;
#                       _lib_share._scrub strips it on the share path.

# Per-tick memo (spec §6.4 + memory: *Pre-probe before sync_cache*).
# Keyed on (max(session_entries.id), current_week.week_start_at,
# weeks_back); single entry, in-process only. Bounded per-tick cost on
# large caches: subsequent calls within the same sync tick hit the
# memo and skip the aggregation walk.
_PROJECTS_ENV_MEMO: dict = {"key": None, "value": None}


def _projects_reset_memo() -> None:
    """Clear the projects envelope memo. Used by unit tests that want to
    measure the inner aggregation cost in isolation."""
    _PROJECTS_ENV_MEMO["key"] = None
    _PROJECTS_ENV_MEMO["value"] = None


def _projects_week_start_monday_utc(ts: "dt.datetime") -> "dt.datetime":
    """Anchor ``ts`` to its containing ISO-Monday 00:00 UTC subscription
    week start. Fallback shape used when no snapshot anchor is available
    — mirrors ``cmd_project``'s Monday fallback (bin/cctally:4711)."""
    base = ts.astimezone(dt.timezone.utc)
    return (base - dt.timedelta(days=base.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


def _projects_week_label(week_start: "dt.datetime") -> str:
    """Render a `wk Mon DD` label for the trend chart x-axis.

    Per spec §5.2's `weeks[].week_label` example (`"wk Apr 22"`).
    UTC-anchored so JSON output is tz-agnostic.
    """
    return f"wk {week_start.strftime('%b %d')}"


def _projects_iter_session_entries(conn: "sqlite3.Connection",
                                   *,
                                   since: "dt.datetime",
                                   until: "dt.datetime",
                                   after_id: "int | None" = None):
    """Read ``session_entries`` joined with ``session_files`` over
    [since, until]. Yields rows directly off the passed conn — no
    cache.db monkeypatch, no production ``get_claude_session_entries``
    pipeline. The fixture DBs co-locate both schemas in one file; the
    production wiring opens both DBs and ATTACHes cache.db as a schema
    on the stats conn (see ``_run_dashboard_sync_tick``).

    ``after_id`` (Codex-P2b — #271 §20): when set, the current-week
    accumulator's warm delta seeks by the **rowid range** (``WHERE e.id > ?``)
    with the ``[since, until]`` timestamp bounds applied as a NON-indexed
    filter — the unary-plus no-op ``+e.timestamp_utc`` deprioritizes
    ``idx_entries_timestamp`` so the planner drives off the INTEGER PRIMARY
    KEY (``SEARCH e USING INTEGER PRIMARY KEY (rowid>?)``), touching only the
    small ingest delta instead of re-scanning the whole current-week timestamp
    range. Unary ``+`` is a TRUE no-op (it preserves the TEXT value AND the
    string comparison, unlike ``+ 0`` which would coerce to numeric), so the
    filtered row SET is byte-identical to the un-hinted form; the caller
    (``_fetch_delta_rows``) re-sorts the small result by
    ``(timestamp_utc, id)`` to reproduce the full pass's fold order. An
    ``EXPLAIN QUERY PLAN`` regression asserts the rowid seek
    (``tests/test_projects_envelope.py``).
    """
    since_iso = since.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    until_iso = until.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if after_id is not None:
        cur = conn.execute(
            "SELECT e.id, e.timestamp_utc, e.model, e.input_tokens, "
            "       e.output_tokens, e.cache_create_tokens, e.cache_read_tokens, "
            "       e.cost_usd_raw, e.source_path, "
            "       sf.session_id, sf.project_path "
            "FROM session_entries e "
            "LEFT JOIN session_files sf ON sf.path = e.source_path "
            "WHERE e.id > ? AND +e.timestamp_utc >= ? AND +e.timestamp_utc <= ? "
            "ORDER BY e.id ASC",
            (after_id, since_iso, until_iso),
        )
    else:
        cur = conn.execute(
            "SELECT e.id, e.timestamp_utc, e.model, e.input_tokens, "
            "       e.output_tokens, e.cache_create_tokens, e.cache_read_tokens, "
            "       e.cost_usd_raw, e.source_path, "
            "       sf.session_id, sf.project_path "
            "FROM session_entries e "
            "LEFT JOIN session_files sf ON sf.path = e.source_path "
            "WHERE e.timestamp_utc >= ? AND e.timestamp_utc <= ? "
            "ORDER BY e.timestamp_utc ASC, e.id ASC",
            (since_iso, until_iso),
        )
    for row in cur:
        yield row


class _ProjWeekBucket(NamedTuple):
    """One (bucket_path, week) immutable aggregate for the projects-envelope
    per-week cache (#269 §14 Win 2).

    All fields are entry-local — each entry belongs to exactly one bucket and
    one week — so a CLOSED week's values are stable and reproduce the
    full-window walk's contribution for that week byte-for-byte.

    ``first_seen`` / ``last_seen`` are the min / max PARSED event datetimes
    (emitted via ``_iso_z``). ``first_order`` / ``first_id`` / ``first_key`` are
    the SQL-FIRST entry for this bucket in this week (first-encountered under
    ``ORDER BY timestamp_utc ASC, id ASC``): ``first_order`` is the RAW
    ``timestamp_utc`` string, so the ``key_by_bucket`` reconstruction argmin
    reproduces the from-scratch global first-seen exactly (Codex-M4 P1 — the
    no-git ``display_key`` makes ``key_by_bucket[bp]`` non-deterministic per
    bucket_path; the envelope picks by global first-seen order).
    """
    cost_usd: float
    sessions_count: int
    first_seen: "dt.datetime"
    last_seen: "dt.datetime"
    first_order: str
    first_id: int
    first_key: "Any"


def _fold_projects_entry(
    mut: dict,
    row: tuple,
    *,
    resolver_cache: dict,
    week_start: "dt.datetime",
) -> "float | None":
    """Fold ONE ``_projects_iter_session_entries`` row onto ``mut`` (the shared
    per-row body, #271 §20 Codex-P1a).

    Used by BOTH the full-window cold fold (``_aggregate_projects_week_raw``)
    and the current-week accumulator's warm incremental append, so their
    per-bucket cost sums, session sets, and first/last-seen captures are
    byte-identical BY CONSTRUCTION. Returns the entry cost, or ``None`` when the
    row is filtered out (``<synthetic>`` model, or its Monday-anchored week ≠
    ``week_start``) — the caller then skips ``week_total`` / ``tail`` advance.

    ``mut[bp]`` is the running mutable dict ``{"cost_usd": float,
    "sessions": set, "first_seen": dt, "last_seen": dt, "first_order": ts_iso,
    "first_id": int, "first_key": ProjectKey}``. The first row seen for a
    ``bucket_path`` captures ``first_order`` / ``first_id`` / ``first_key`` —
    order-safe for the warm append because every delta row sorts strictly after
    ``tail`` (#271 §20), so a delta row is never the first-seen of a bucket that
    already exists in ``mut``.
    """
    c = _cctally()
    (entry_id, ts_iso, model, input_tok, output_tok,
     cache_create, cache_read, cost_raw, source_path,
     session_id, project_path) = row
    if model == "<synthetic>":
        return None
    ts = parse_iso_datetime(ts_iso, "session_entries.timestamp_utc")
    if _projects_week_start_monday_utc(ts) != week_start:
        return None
    entry_cost = _calculate_entry_cost(
        model,
        {
            "input_tokens": input_tok or 0,
            "output_tokens": output_tok or 0,
            "cache_creation_input_tokens": cache_create or 0,
            "cache_read_input_tokens": cache_read or 0,
        },
        mode="auto",
        cost_usd=cost_raw,
    )
    pkey = c._resolve_project_key(project_path, "git-root", resolver_cache)
    bp = pkey.bucket_path
    a = mut.get(bp)
    if a is None:
        a = {
            "cost_usd": 0.0,
            "sessions": set(),
            "first_seen": ts,
            "last_seen": ts,
            "first_order": ts_iso,
            "first_id": entry_id,
            "first_key": pkey,
        }
        mut[bp] = a
    a["cost_usd"] += entry_cost
    if session_id:
        a["sessions"].add(session_id)
    elif source_path:
        a["sessions"].add(source_path)
    if ts < a["first_seen"]:
        a["first_seen"] = ts
    if ts > a["last_seen"]:
        a["last_seen"] = ts
    return entry_cost


def _aggregate_projects_week_raw(
    conn: "sqlite3.Connection",
    *,
    week_start: "dt.datetime",
    week_end: "dt.datetime",
    resolver_cache: dict,
) -> "tuple[dict, float, tuple | None]":
    """Full-window fold for one Monday-anchored subscription week, returning the
    RAW mutable ``mut`` (sessions kept as SETS, so the current-week accumulator
    can dedup across ticks), the entry-order ``week_total`` float, and the
    ``tail`` ``(ts_iso, id)`` of the last folded CURRENT-WEEK entry (``None``
    when no row folded).

    Shared by the finalizing ``_aggregate_projects_week`` wrapper (closed-week
    cache path) and the current-week accumulator's cold seed (#271 §20
    Codex-P1a — the finalized public shape discards the sessions SET into a
    count, so cold seeding needs the raw ``mut``). Byte-identical to the
    original single-loop aggregate: same ``timestamp_utc ASC, id ASC`` order,
    same per-row ``_fold_projects_entry`` arithmetic, same entry-order
    ``week_total`` left-fold.
    """
    mut: "dict[str, dict]" = {}
    week_total = 0.0
    tail: "tuple | None" = None
    for row in _projects_iter_session_entries(
        conn, since=week_start, until=week_end,
    ):
        entry_cost = _fold_projects_entry(
            mut, row, resolver_cache=resolver_cache, week_start=week_start,
        )
        if entry_cost is None:
            continue
        week_total += entry_cost
        tail = (row[1], row[0])  # (ts_iso, id)
    return mut, week_total, tail


def _finalize_projects_mut(mut: dict) -> "dict[str, _ProjWeekBucket]":
    """Finalize a raw ``mut`` (sessions SETS) into ``{bucket_path:
    _ProjWeekBucket}`` — ``sessions_count = len(sessions)``, the rest copied
    through. The public shape both the closed-week cache and the current-week
    accumulator return (#271 §20).
    """
    return {
        bp: _ProjWeekBucket(
            cost_usd=a["cost_usd"],
            sessions_count=len(a["sessions"]),
            first_seen=a["first_seen"],
            last_seen=a["last_seen"],
            first_order=a["first_order"],
            first_id=a["first_id"],
            first_key=a["first_key"],
        )
        for bp, a in mut.items()
    }


def _aggregate_projects_week(
    conn: "sqlite3.Connection",
    *,
    week_start: "dt.datetime",
    week_end: "dt.datetime",
    resolver_cache: dict,
) -> "tuple[dict[str, _ProjWeekBucket], float]":
    """Aggregate one Monday-anchored subscription week's entry slice.

    Returns ``({bucket_path: _ProjWeekBucket}, week_total_cost)``. Byte-identical
    to the contribution the full-window walk in ``_build_projects_envelope``
    makes for this week: the per-week query returns entries in the same
    ``timestamp_utc ASC, id ASC`` order, filtered to those whose
    Monday-anchored week equals ``week_start``, so the per-bucket cost sum, the
    entry-order ``week_total`` sum, the session set, and the first/last-seen +
    SQL-first-entry captures all reproduce the full pass. ``week_end`` is the
    inclusive query bound (``<= ?``); the exact next-Monday boundary entry is
    fetched but filtered out (it belongs to the next week) — the next week's
    slice keeps it, so no entry is lost or double-counted.

    Thin wrapper over ``_aggregate_projects_week_raw`` + ``_finalize_projects_mut``
    (#271 §20 Codex-P1a); the public shape is unchanged for the closed-week
    cache path.
    """
    mut, week_total, _tail = _aggregate_projects_week_raw(
        conn, week_start=week_start, week_end=week_end,
        resolver_cache=resolver_cache,
    )
    return _finalize_projects_mut(mut), week_total


def _assemble_projects_via_cache(
    conn: "sqlite3.Connection",
    *,
    weeks_full: "list[dt.datetime]",
    cw_start: "dt.datetime",
    cw_end: "dt.datetime",
    cur_max_id: int,
) -> "tuple[dict, dict, dict]":
    """Flag-ON assembly (#269 §14 Win 2): recompute only the CURRENT week each
    warm tick; serve CLOSED weeks from the per-(project, week) cache on a hit and
    RECOMPUTE-AND-POPULATE on a miss (cold / Monday rollover / window slide,
    Codex-M4 P2). Returns ``(buckets, total_cost_by_week, key_by_bucket)`` in the
    exact shape the from-scratch walk produces, so the downstream disambiguation
    / attribution / trend assembly runs unchanged and byte-identically.

    #271 M4 (spec §20): the CURRENT week is no longer re-folded from scratch each
    warm tick — it goes through the single-slot ``accumulate_projects_current_week``
    accumulator, which folds only the ``id > reconciled_max_id`` delta (or
    finalizes the cached running ``mut`` unchanged on an empty-delta tick),
    byte-identically. ``cur_max_id`` is the tick's ``MAX(session_entries.id)``
    (the delta watermark). The accumulator is single-writer here (this runs only
    on the sync-thread ``use_projects_env_cache=True`` path).
    """
    c = _cctally()
    sc = c._load_sibling("_lib_snapshot_cache")
    resolver_cache: dict = {}
    buckets: dict = {}
    total_cost_by_week: dict = {}
    key_by_bucket: dict = {}
    # Reconstruct key_by_bucket by the GLOBAL first-seen SQL order
    # (timestamp_utc ASC, id ASC): per bucket_path, the argmin of
    # (first_order, first_id) across its assembled weeks (Codex-M4 P1).
    best_order: dict = {}

    def _merge_week(w, week_buckets, week_total):
        # `total_cost_by_week` mirrors the from-scratch dict, which is only
        # populated for weeks that had entries (an empty week stays absent and
        # falls back to `.get(w, 0.0)` downstream).
        if week_buckets:
            total_cost_by_week[w] = week_total
        for bp, wb in week_buckets.items():
            buckets[(bp, w)] = {
                "cost_usd": wb.cost_usd,
                # `sessions` is only ever `len()`-d downstream; a `range` of the
                # cached count reproduces that without storing the id set.
                "sessions": range(wb.sessions_count),
                "first_seen": wb.first_seen,
                "last_seen": wb.last_seen,
            }
            cand = (wb.first_order, wb.first_id)
            if bp not in best_order or cand < best_order[bp]:
                best_order[bp] = cand
                key_by_bucket[bp] = wb.first_key

    def _fetch_all_raw():
        return _aggregate_projects_week_raw(
            conn, week_start=cw_start, week_end=cw_end,
            resolver_cache=resolver_cache,
        )

    def _fetch_delta_rows(after_id):
        # Rowid-seek the ingest delta (id > after_id) over the current-week
        # window, then PRE-FILTER to genuine current-week non-synthetic rows
        # (mirrors _fold_projects_entry's membership filter) so the accumulator's
        # fold-order gate compares a REAL current-week entry as rows[0]. Sort by
        # (ts_iso, id) to reproduce the full pass's fold order exactly (SQLite
        # BINARY collation on the Z-normalized ISO string == Python str compare).
        out = []
        for r in _projects_iter_session_entries(
            conn, since=cw_start, until=cw_end, after_id=after_id,
        ):
            if r[2] == "<synthetic>":  # r[2] = model
                continue
            ts = parse_iso_datetime(r[1], "session_entries.timestamp_utc")
            if _projects_week_start_monday_utc(ts) != cw_start:
                continue
            out.append(r)
        out.sort(key=lambda r: (r[1], r[0]))  # (ts_iso, id)
        return out

    for w in weeks_full:
        if w == cw_start:
            week_buckets, week_total = sc.accumulate_projects_current_week(
                week_key=sc.projects_env_week_key(cw_start),
                cur_max_id=cur_max_id,
                fetch_all_raw=_fetch_all_raw,
                fetch_delta_rows=_fetch_delta_rows,
                finalize=_finalize_projects_mut,
                fold=lambda mut, row: _fold_projects_entry(
                    mut, row, resolver_cache=resolver_cache, week_start=cw_start,
                ),
            )
        else:
            week_iso = sc.projects_env_week_key(w)
            hit = sc.projects_env_week_get(week_iso)
            if hit is not None:
                week_buckets, week_total = hit
            else:
                week_buckets, week_total = _aggregate_projects_week(
                    conn, week_start=w, week_end=w + dt.timedelta(days=7),
                    resolver_cache=resolver_cache,
                )
                sc.projects_env_week_put(week_iso, week_buckets, week_total)
        _merge_week(w, week_buckets, week_total)
    return buckets, total_cost_by_week, key_by_bucket


def _build_projects_envelope(
    conn: "sqlite3.Connection",
    *,
    now_utc: "dt.datetime",
    current_week: "Any | None" = None,
    weeks_back: int = 12,
    use_projects_env_cache: bool = False,
) -> dict:
    """Build the ``projects.{current_week, trend}`` envelope block.

    Reuses ``cmd_project``'s identity model — per-(``ProjectKey``, week)
    rollup over ``session_entries`` with display-key disambiguation via
    ``_project_disambiguate_labels`` — but emits the simpler envelope
    shape from spec §5.2 (no per-model breakdowns, no first/last seen
    per session, no per-row $/1%; just cost / attributed_pct / sessions).

    Week boundaries follow ``cmd_project``'s Monday-anchored UTC
    fallback (``bin/cctally:4711``); ``weekly_usage_snapshots`` rows are
    matched by ``week_start_date`` (date-only) for ``attributed_pct``.

    ``current_week`` is passed through opaquely — if non-None and
    carrying a ``.week_start_at`` UTC datetime, that boundary supplants
    the Monday fallback for the current week's bucket. None (the
    default) preserves the fallback.

    Determinism: same conn + same ``now_utc`` ⇒ byte-identical JSON
    (R-PROJ5 invariant). Per-tick memoized on
    ``(max(session_entries.id), cw_week_start, weeks_back)``.
    """
    c = _cctally()

    # ---- Pre-probe gate / memoization (spec §6.4) -----------------------
    # `attributed_pct` and trend `total_pct` are functions of
    # `weekly_usage_snapshots.weekly_percent`, which the throttled OAuth
    # refresh path can advance between session_entries writes. Probe
    # `MAX(weekly_usage_snapshots.id)` so the memo invalidates on that
    # surface too (mirrors the operational-error guard the attribution
    # SELECT uses below).
    cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM session_entries")
    max_id = cur.fetchone()[0]
    try:
        cur = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM weekly_usage_snapshots"
        )
        max_wus_id = cur.fetchone()[0]
    except sqlite3.OperationalError:
        max_wus_id = 0
    cw_key: "dt.datetime | None" = None
    if current_week is not None:
        cw_key = getattr(current_week, "week_start_at", None)
    memo_key = (max_id, max_wus_id, cw_key, weeks_back)
    cached = _PROJECTS_ENV_MEMO.get("value")
    if cached is not None and _PROJECTS_ENV_MEMO.get("key") == memo_key:
        return cached

    # ---- Week-start anchor (current subscription week) ------------------
    # ``TuiCurrentWeek.week_start_at`` is NOT a valid Monday lookup key
    # after ``_apply_midweek_reset_override`` — it is shifted to the
    # in-week reset instant (e.g. Friday 13:00 UTC) while the bucket
    # aggregator below snaps every entry to its containing ISO-Monday
    # via ``_week_for``. Using ``cw_key`` directly as the bucket-lookup
    # key strands all current-week activity in an empty bucket and emits
    # ``rows: []`` with ``total_cost_usd: 0.0``. Snap to the canonical
    # Monday-UTC week anchor here so the lookup keys align — same
    # invariant the weekly handling notes call out for
    # ``weekly_usage_snapshots``/``percent_milestones`` cross-table
    # joins. Regression: ``tests/fixtures/dashboard/reset-week/`` +
    # ``test_current_week_rows_populated_after_midweek_reset``.
    if cw_key is not None:
        cw_start = _projects_week_start_monday_utc(cw_key)
    else:
        cw_start = _projects_week_start_monday_utc(now_utc)

    # Build a list of canonical Monday-anchored week starts ending with
    # cw_start, oldest → newest, of length ``weeks_back``. Clamping to
    # actual history happens after the entry walk reveals what weeks
    # have any activity.
    weeks_full = [
        cw_start - dt.timedelta(days=7 * (weeks_back - 1 - i))
        for i in range(weeks_back)
    ]
    cw_end = cw_start + dt.timedelta(days=7)
    since_dt = weeks_full[0]
    until_dt = cw_end  # exclusive end; SQL is `>= since AND <= until`

    # ---- Bucket entries per (ProjectKey, week_start) --------------------
    # The three structures the downstream disambiguation / attribution /
    # trend assembly consume:
    #   buckets[(bucket_path, week_start)] -> {cost_usd, sessions, first/last_seen}
    #   total_cost_by_week[week_start]     -> attribution denominator
    #   key_by_bucket[bucket_path]         -> global first-seen ProjectKey
    # Flag ON (#269 §14 Win 2, sync-thread only): the CURRENT week is recomputed
    # fresh each warm tick; CLOSED weeks are served from the per-(project, week)
    # cache (hit) or recomputed-and-populated (miss). Flag OFF (CLI / tests /
    # HTTP-drill): the original single full-window walk, byte-unchanged.
    if use_projects_env_cache:
        buckets, total_cost_by_week, key_by_bucket = _assemble_projects_via_cache(
            conn, weeks_full=weeks_full, cw_start=cw_start, cw_end=cw_end,
            cur_max_id=max_id,
        )
    else:
        # `_resolve_project_key` is the production resolver; we use git-root
        # mode (default for `cmd_project --group` absent) — matches the
        # CLI's default.
        _resolve_project_key = c._resolve_project_key
        resolver_cache: dict = {}

        buckets = {}
        total_cost_by_week = {}
        # First-seen ProjectKey per bucket_path (feeds `_project_disambiguate_labels`).
        key_by_bucket = {}

        def _week_for(ts: dt.datetime) -> "dt.datetime | None":
            wstart = _projects_week_start_monday_utc(ts)
            if wstart < weeks_full[0] or wstart > weeks_full[-1]:
                return None
            return wstart

        # Orphan handling: `_projects_iter_session_entries` LEFT JOINs
        # `session_files` so entries whose source_path has no
        # `session_files` row return `project_path = NULL`. Below,
        # `_resolve_project_key(None, ...)` maps that to the
        # `(unknown)` bucket — same identity the drill-down's explicit
        # orphan scan in `_project_detail_for_window` (see the
        # ``if unknown_bucket:`` branch around the
        # `orphan_cur` SELECT) collects via a NULL-side LEFT JOIN. The
        # two paths converge on the same `(unknown)` source_path set.
        for row in _projects_iter_session_entries(
            conn, since=since_dt, until=until_dt,
        ):
            (entry_id, ts_iso, model, input_tok, output_tok,
             cache_create, cache_read, cost_raw, source_path,
             session_id, project_path) = row
            if model == "<synthetic>":
                continue
            # Parse timestamp; assume Z / +00:00 — production iterators do
            # the same via `parse_iso_datetime`.
            ts = parse_iso_datetime(ts_iso, "session_entries.timestamp_utc")
            wstart = _week_for(ts)
            if wstart is None:
                continue

            # Entry cost via the shared pricing chokepoint.
            entry_cost = _calculate_entry_cost(
                model,
                {
                    "input_tokens": input_tok or 0,
                    "output_tokens": output_tok or 0,
                    "cache_creation_input_tokens": cache_create or 0,
                    "cache_read_input_tokens": cache_read or 0,
                },
                mode="auto",
                cost_usd=cost_raw,
            )

            # Project-key identity (`git_root` mode = production default).
            pkey = _resolve_project_key(project_path, "git-root", resolver_cache)
            bkey = (pkey.bucket_path, wstart)
            b = buckets.get(bkey)
            if b is None:
                b = {
                    "key": pkey,
                    "cost_usd": 0.0,
                    "sessions": set(),
                    "first_seen": ts,
                    "last_seen": ts,
                }
                buckets[bkey] = b
            b["cost_usd"] += entry_cost
            if session_id:
                b["sessions"].add(session_id)
            elif source_path:
                # Fallback: treat one source_path as one session when
                # session_files.session_id is NULL (lazy population).
                b["sessions"].add(source_path)
            if ts < b["first_seen"]:
                b["first_seen"] = ts
            if ts > b["last_seen"]:
                b["last_seen"] = ts
            total_cost_by_week[wstart] = (
                total_cost_by_week.get(wstart, 0.0) + entry_cost
            )
            # Remember first-seen ProjectKey for each bucket_path so the
            # disambiguator pass below sees consistent ProjectKey instances.
            if pkey.bucket_path not in key_by_bucket:
                key_by_bucket[pkey.bucket_path] = pkey

    # ---- Load weekly_usage_snapshots for attribution --------------------
    # weekly_percent keyed by week_start (UTC datetime, Monday). We
    # match on `week_start_date` (date-only) since that's the canonical
    # cross-table key per the CLAUDE.md weekly handling notes.
    #
    # We use the LATEST snapshot per week_start_date — NOT MAX — because
    # the "weekly_percent is monotonic within a week" invariant breaks
    # on weeks that receive an in-place credit (see CLAUDE.md "In-place
    # 5h credit" / `week_reset_events` notes). MAX would lock attribution
    # to the pre-credit high-water mark even after Anthropic credits the
    # week back down, overstating Used % on the Projects panel/modal
    # forever. The "latest row" pattern matches `_select_last_known_snapshot`
    # (bin/cctally:1162-1168) and the doctor credited-week check
    # (bin/cctally:8706-8714).
    #
    # Portable per-key-latest pattern: read rows ordered by capture-
    # ascending and let later rows overwrite. The final value per key
    # is the most-recent snapshot.
    weekly_pct_by_week: dict[dt.datetime, float] = {}
    try:
        cur = conn.execute(
            "SELECT week_start_date, weekly_percent "
            "FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc ASC, id ASC"
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        # No weekly_usage_snapshots table — leaves attributed_pct = None
        # throughout (acceptable per spec §2.7).
        rows = []
    for week_date_str, weekly_pct in rows:
        try:
            wd = dt.date.fromisoformat(week_date_str)
        except (TypeError, ValueError):
            continue
        # Snap the date to UTC Monday 00:00 (matches the bucketing key).
        wstart = dt.datetime.combine(
            wd, dt.time(0, 0, 0), tzinfo=dt.timezone.utc,
        )
        # Snap to Monday (snapshot rows that captured a non-Monday week
        # boundary still align to the same canonical bucket as the entry
        # walk, since the bucketing is Monday-anchored).
        wstart = _projects_week_start_monday_utc(wstart)
        if weekly_pct is not None:
            weekly_pct_by_week[wstart] = float(weekly_pct)

    # ---- Disambiguate display_keys across the union of projects --------
    # `_project_disambiguate_labels` expects a list of dicts each with a
    # `key` field that's a ProjectKey. Sort by bucket_path for stable
    # indexing.
    bucket_paths_sorted = sorted(key_by_bucket.keys())
    disambig_rows = [
        {"key": key_by_bucket[bp]} for bp in bucket_paths_sorted
    ]
    augmented_by_idx = c._project_disambiguate_labels(disambig_rows)
    display_key_by_bucket: dict[str, str] = {}
    for idx, bp in enumerate(bucket_paths_sorted):
        pkey = key_by_bucket[bp]
        display_key_by_bucket[bp] = augmented_by_idx.get(
            idx, pkey.display_key,
        )

    # ---- Determine actual weeks emitted (clamp to history) -------------
    weeks_with_activity = sorted(
        ws for ws in {ws for (_bp, ws) in buckets.keys()}
    )
    if weeks_with_activity:
        # Window = inclusive [oldest_active_week, cw_start]. Always emits
        # cw_start (panel + trend share the same current_week column).
        oldest = min(weeks_with_activity[0], cw_start)
        trend_weeks = []
        w = oldest
        while w <= cw_start:
            trend_weeks.append(w)
            w += dt.timedelta(days=7)
    else:
        trend_weeks = [cw_start]

    # ---- current_week.rows ---------------------------------------------
    cw_rows = []
    cw_pct = weekly_pct_by_week.get(cw_start)
    cw_total_cost = total_cost_by_week.get(cw_start, 0.0)
    for bp in bucket_paths_sorted:
        b = buckets.get((bp, cw_start))
        if b is None:
            continue
        if cw_pct is not None and cw_total_cost > 0:
            attributed = (b["cost_usd"] / cw_total_cost) * cw_pct
        else:
            attributed = None
        cw_rows.append({
            "key":              display_key_by_bucket[bp],
            "bucket_path":      bp,
            # NOTE: NOT rounded — `round(..., 6)` introduces ~1e-6 error
            # that breaks the R-PROJ1/R-PROJ2 1e-9 reconcile tolerance.
            # The JSON serializer emits up to 17 significant digits;
            # network bandwidth is negligible (KB-scale payloads).
            "cost_usd":         b["cost_usd"],
            "attributed_pct":   attributed,
            "sessions_count":   len(b["sessions"]),
        })
    # Desc by cost (ties broken by key for byte-stability across runs).
    cw_rows.sort(key=lambda r: (-r["cost_usd"], r["key"]))

    cw_block = {
        "week_label":      _projects_week_label(cw_start),
        "week_start_date": cw_start.date().isoformat(),
        "week_start_at":   _iso_z(cw_start),
        "total_cost_usd":  cw_total_cost,
        "rows":            cw_rows,
    }

    # ---- trend.weeks[] + trend.projects[] ------------------------------
    trend_weeks_blocks = []
    for w in trend_weeks:
        wpct = weekly_pct_by_week.get(w)
        trend_weeks_blocks.append({
            "week_start_date": w.date().isoformat(),
            "week_label":      _projects_week_label(w),
            "total_cost_usd":  total_cost_by_week.get(w, 0.0),
            "total_pct":       wpct,
        })

    trend_projects = []
    for bp in bucket_paths_sorted:
        weekly_cost: list[float] = []
        weekly_pct_arr: list[float | None] = []
        sessions_per_week: list[int] = []
        first_seen_per_week: list[str | None] = []
        last_seen_per_week: list[str | None] = []
        for w in trend_weeks:
            b = buckets.get((bp, w))
            if b is None:
                weekly_cost.append(0.0)
                weekly_pct_arr.append(None)
                sessions_per_week.append(0)
                first_seen_per_week.append(None)
                last_seen_per_week.append(None)
                continue
            week_total = total_cost_by_week.get(w, 0.0)
            week_pct = weekly_pct_by_week.get(w)
            if week_pct is not None and week_total > 0:
                attributed = (b["cost_usd"] / week_total) * week_pct
            else:
                attributed = None
            weekly_cost.append(b["cost_usd"])
            weekly_pct_arr.append(attributed)
            sessions_per_week.append(len(b["sessions"]))
            first_seen_per_week.append(_iso_z(b["first_seen"]))
            last_seen_per_week.append(_iso_z(b["last_seen"]))
        # Skip projects with zero total cost across the entire window
        # (the bucket-loop only enters projects that have at least one
        # entry, so this is mainly a safety check).
        if all(c == 0.0 for c in weekly_cost):
            continue
        trend_projects.append({
            "key":                 display_key_by_bucket[bp],
            "bucket_path":         bp,
            "weekly_cost":         weekly_cost,
            "weekly_pct":          weekly_pct_arr,
            "sessions_per_week":   sessions_per_week,
            "first_seen_per_week": first_seen_per_week,
            "last_seen_per_week":  last_seen_per_week,
        })
    # Stable sort: desc by total window cost, ties broken by key.
    trend_projects.sort(
        key=lambda p: (-stable_sum(p["weekly_cost"]), p["key"]),
    )

    trend_block = {
        "window_weeks": len(trend_weeks),
        "weeks":        trend_weeks_blocks,
        "projects":     trend_projects,
    }

    result = {
        "current_week": cw_block,
        "trend":        trend_block,
    }
    _PROJECTS_ENV_MEMO["key"] = memo_key
    _PROJECTS_ENV_MEMO["value"] = result
    return result


def _project_detail_for_window(
    conn: "sqlite3.Connection",
    *,
    project_key: str,
    weeks_back: int,
    now_utc: "dt.datetime",
    current_week: "Any | None" = None,
    projects_envelope: "dict | None" = None,
) -> "dict | None":
    """Build the drill payload for ``GET /api/project/<key>?weeks=N``
    (spec §5.3).

    Resolves ``project_key`` against the same disambiguated display
    keys the envelope emits. Returns ``None`` on miss → caller maps to
    HTTP 404. Top-N sessions by ``last_activity`` desc (cap=5 per spec
    §5.3); models list is desc by cost. ``window_attributed_pct`` is
    the across-window sum of ``(project_cost_in_week / week_total) *
    week_pct`` — ``None`` when no contributing week has a snapshot.

    Identity invariant (CLAUDE.md spec §9.2 R-PROJ3 + Codex F6):
    ``project_key`` is matched against the DISAMBIGUATED display_key
    (`foo (repos)` etc.), NOT a substring filter — the CLI
    ``--project <pattern>`` form does NOT reliably round-trip
    disambiguated keys. The reconcile harness asserts this by
    ``bucket_path`` identity, not pattern.

    Performance — two layered optimizations make this O(project-rows-
    in-window) instead of O(all-rows-in-window):

    1. ``projects_envelope`` (HTTP path passes ``snap.projects_envelope``):
       reuse the sync thread's already-built envelope for the
       ``project_key`` → ``bucket_path`` resolution and the trend-pct
       lookup, instead of rebuilding. On an active dashboard, the
       per-process memo on ``_build_projects_envelope`` invalidates
       every time ``session_entries.id`` advances — between the sync
       tick and the user's click, that's almost always — so the memo
       saves nothing on the HTTP path. Plumbing the snapshot's
       envelope through skips ~1-2s of redundant work per drill.

    2. SQL-side bucket filter: resolve ``bucket_path`` → set of
       ``session_files.path`` once (cheap — ``session_files`` is
       ~8k rows vs ``session_entries`` 150k+), stage into a TEMP TABLE,
       and INNER JOIN the entries walk so the engine only touches this
       project's rows. Eliminates the Python-side
       ``if pkey.bucket_path != bucket_path: continue`` filter that
       previously discarded ~99% of rows after paying for parse +
       resolve + cost-compute on each.
    """
    c = _cctally()

    # Resolve the projects envelope: prefer the snapshot-provided one
    # (sync thread already built it for this tick) over rebuilding
    # locally. ``None`` keeps the legacy behavior for callers that
    # don't have a snapshot (tests, the reconcile harness).
    if projects_envelope is not None:
        env = projects_envelope
    else:
        env = _build_projects_envelope(
            conn,
            now_utc=now_utc,
            current_week=current_week,
            weeks_back=weeks_back,
        )

    # Resolve project_key → bucket_path via the envelope's identity map.
    bucket_path: "str | None" = None
    matching_trend: "dict | None" = None
    for tp in env["trend"]["projects"]:
        if tp["key"] == project_key:
            bucket_path = tp["bucket_path"]
            matching_trend = tp
            break
    if bucket_path is None:
        # The project may have shown up in current_week (panel only)
        # but had 0 cost across the window — fall back to that.
        for r in env["current_week"]["rows"]:
            if r["key"] == project_key:
                bucket_path = r["bucket_path"]
                break
    if bucket_path is None:
        return None

    # ---- Window bounds (Monday-anchored UTC fallback, like the builder) -
    cw_start = parse_iso_datetime(
        env["current_week"]["week_start_at"],
        "projects.current_week.week_start_at",
    )
    since_dt = cw_start - dt.timedelta(days=7 * (weeks_back - 1))
    until_dt = cw_start + dt.timedelta(days=7)

    # ---- Build bucket → source_paths map for SQL-side scoping ----------
    # Walk session_files (~8k rows) once instead of session_entries
    # (~150k+ rows). _resolve_project_key gets called at most ~distinct-
    # project_paths times (~127 on real DBs) instead of once per entry,
    # and most of that is hashmap lookups via resolver_cache.
    resolver_cache: dict = {}
    _resolve_project_key_fn = c._resolve_project_key
    unknown_bucket = bucket_path == "(unknown)"
    bucket_source_paths: set[str] = set()
    sf_cur = conn.execute(
        "SELECT path, project_path FROM session_files"
    )
    for sf_path, sf_project_path in sf_cur:
        pkey = _resolve_project_key_fn(
            sf_project_path, "git-root", resolver_cache,
        )
        if pkey.bucket_path == bucket_path:
            bucket_source_paths.add(sf_path)
    if unknown_bucket:
        # session_entries rows whose source_path has no session_files
        # row at all LEFT-JOIN to NULL project_path → resolve to
        # ``(unknown)`` via _resolve_project_key. session_files-only
        # scan misses those.
        #
        # This explicit orphan scan is the drill-down's mirror of the
        # envelope's implicit orphan path: the envelope walk in
        # `_build_projects_envelope` (see the `_projects_iter_session_entries`
        # loop above) routes the same orphan source_paths through the
        # LEFT-JOIN→NULL→`_resolve_project_key(None, ...)` chain into the
        # ``(unknown)`` bucket. Both surfaces converge on the same
        # source_path set so envelope row counts/costs and drill row
        # counts/costs reconcile.
        orphan_cur = conn.execute(
            "SELECT DISTINCT e.source_path "
            "FROM session_entries e "
            "LEFT JOIN session_files sf ON sf.path = e.source_path "
            "WHERE sf.path IS NULL AND e.source_path IS NOT NULL"
        )
        for (sp,) in orphan_cur:
            bucket_source_paths.add(sp)

    if not bucket_source_paths:
        # Project visible in envelope but no contributing source_paths
        # for this conn (rare edge — e.g. an envelope built off a
        # different conn). Emit an empty drill so the 404 path stays
        # reserved for "key unknown."
        return {
            "key":                    project_key,
            "bucket_path":            bucket_path,
            "window_weeks":           weeks_back,
            "window_cost_usd":        0.0,
            "window_attributed_pct":  None,
            "models":                 [],
            "sessions":               [],
            "models_total":           0,
            "sessions_total":         0,
        }

    # Stage bucket source_paths into a TEMP TABLE so the entries walk
    # can INNER JOIN an indexed lookup. ``IN (?, ?, ?, ...)`` would
    # collide with SQLite's parameter cap (~999 bindings) on heavy
    # multi-cwd projects. DROP-then-CREATE makes the function
    # re-entrant on a reused conn (the HTTP handler closes the conn
    # per request, but tests share conns across calls).
    conn.execute("DROP TABLE IF EXISTS temp._drill_paths")
    conn.execute(
        "CREATE TEMP TABLE _drill_paths(path TEXT PRIMARY KEY)"
    )
    conn.executemany(
        "INSERT OR IGNORE INTO _drill_paths(path) VALUES (?)",
        [(p,) for p in bucket_source_paths],
    )

    since_iso = since_dt.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    until_iso = until_dt.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # ---- Walk session_entries (project-scoped) once -------------------
    # INNER JOIN to _drill_paths drops every row whose source_path
    # doesn't belong to this bucket. The Python-side filter that
    # previously discarded ~99% of rows post-resolve is gone.
    entries_cur = conn.execute(
        "SELECT e.id, e.timestamp_utc, e.model, e.input_tokens, "
        "       e.output_tokens, e.cache_create_tokens, "
        "       e.cache_read_tokens, e.cost_usd_raw, e.source_path, "
        "       sf.session_id, sf.project_path "
        "FROM session_entries e "
        "INNER JOIN _drill_paths dp ON dp.path = e.source_path "
        "LEFT JOIN session_files sf ON sf.path = e.source_path "
        "WHERE e.timestamp_utc >= ? AND e.timestamp_utc <= ? "
        "ORDER BY e.timestamp_utc ASC, e.id ASC",
        (since_iso, until_iso),
    )

    # Per-model rollup: {model -> {cost_usd, sessions, in, out, cache_*}}
    models: dict[str, dict] = {}
    # Per-session rollup: {session_id -> {cost_usd, last_activity,
    #                                     started_at, primary_model}}
    sessions: dict[str, dict] = {}
    # Aggregate scalars across the window.
    window_cost = 0.0
    window_input_t = 0
    window_output_t = 0

    for row in entries_cur:
        (entry_id, ts_iso, model, input_tok, output_tok,
         cache_create, cache_read, cost_raw, source_path,
         session_id, project_path) = row
        if model == "<synthetic>":
            continue
        # No need to call _resolve_project_key here — the INNER JOIN
        # on _drill_paths already restricted the result set to entries
        # whose source_path belongs to this bucket.
        ts = parse_iso_datetime(ts_iso, "session_entries.timestamp_utc")
        entry_cost = _calculate_entry_cost(
            model,
            {
                "input_tokens": input_tok or 0,
                "output_tokens": output_tok or 0,
                "cache_creation_input_tokens": cache_create or 0,
                "cache_read_input_tokens": cache_read or 0,
            },
            mode="auto",
            cost_usd=cost_raw,
        )
        window_cost += entry_cost
        window_input_t += int(input_tok or 0)
        window_output_t += int(output_tok or 0)

        # Per-model rollup.
        m = models.get(model)
        if m is None:
            m = {
                "model":          model,
                "cost_usd":       0.0,
                "sessions":       set(),
                "tokens_input":   0,
                "tokens_output":  0,
            }
            models[model] = m
        m["cost_usd"] += entry_cost
        m["tokens_input"] += int(input_tok or 0)
        m["tokens_output"] += int(output_tok or 0)
        if session_id:
            m["sessions"].add(session_id)
        elif source_path:
            m["sessions"].add(source_path)

        # Per-session rollup.
        sid = session_id or source_path
        if not sid:
            continue
        s = sessions.get(sid)
        if s is None:
            s = {
                "session_id":       sid,
                "started_at":       ts,
                "last_activity_at": ts,
                "primary_model":    model,
                "cost_usd":         0.0,
            }
            sessions[sid] = s
        else:
            if ts < s["started_at"]:
                s["started_at"] = ts
            if ts > s["last_activity_at"]:
                s["last_activity_at"] = ts
        s["cost_usd"] += entry_cost

    # Models desc by cost (ties broken by model name for stability).
    models_list = sorted(
        models.values(),
        key=lambda m: (-m["cost_usd"], m["model"]),
    )
    models_out = []
    for m in models_list:
        models_out.append({
            "model":          m["model"],
            "cost_usd":       m["cost_usd"],
            "sessions_count": len(m["sessions"]),
            "tokens_input":   m["tokens_input"],
            "tokens_output":  m["tokens_output"],
        })

    # Sessions: top-5 by last_activity_at desc (per spec §5.3).
    sessions_sorted = sorted(
        sessions.values(),
        key=lambda s: s["last_activity_at"],
        reverse=True,
    )
    sessions_out = []
    for s in sessions_sorted[:5]:
        sessions_out.append({
            "session_id":       s["session_id"],
            "started_at":       _iso_z(s["started_at"]),
            "last_activity_at": _iso_z(s["last_activity_at"]),
            "primary_model":    s["primary_model"],
            "cost_usd":         s["cost_usd"],
        })

    # window_attributed_pct: prefer the trend projection (already
    # computed correctly). Sum across weeks within the requested
    # ``weeks_back`` window; None when all contributing weeks lack
    # snapshots.
    #
    # IMPORTANT: when the HTTP path reuses ``snap.projects_envelope``
    # (built by the sync thread with ``weeks_back=12``),
    # ``matching_trend["weekly_pct"]`` is always a 12-element array even
    # when the drill is requested with ``?weeks=1|4|8``. Slice to the
    # trailing ``weeks_back`` entries — same pattern as
    # ``snapshot_to_share_envelope`` at line 1629 — so the answer doesn't
    # depend on whether the envelope was rebuilt or reused.
    win_pct: "float | None" = None
    if matching_trend is not None:
        weekly_pct_arr = matching_trend.get("weekly_pct") or []
        n = len(weekly_pct_arr)
        take = min(weeks_back, n) if n > 0 else 0
        sliced = weekly_pct_arr[-take:] if take > 0 else []
        wp = [p for p in sliced if p is not None]
        if wp:
            win_pct = stable_sum(wp)

    # Best-effort cleanup of the per-call TEMP TABLE so a reused conn
    # doesn't carry path state into the next drill (tests share conns;
    # production HTTP closes the conn so this is a no-op there).
    try:
        conn.execute("DROP TABLE IF EXISTS temp._drill_paths")
    except sqlite3.Error:
        pass

    return {
        "key":                    project_key,
        "bucket_path":            bucket_path,
        "window_weeks":           weeks_back,
        "window_cost_usd":        window_cost,
        "window_attributed_pct":  win_pct,
        "models":                 models_out,
        "sessions":               sessions_out,
        "models_total":           len(models_out),
        "sessions_total":         len(sessions),
    }


# Test-surface impl. The HTTP handler `_handle_get_project_detail`
# delegates here so unit tests can exercise the path parser + dispatch
# logic without spinning up a real server. ``handler`` is anything that
# exposes ``self.path`` (the URL path + query), ``send_response``,
# ``send_header``, ``end_headers``, ``send_error``, and ``wfile.write``
# — that's the BaseHTTPRequestHandler surface plus the
# ``test_dashboard_project_endpoint._FakeHandler`` stand-in.
def _handle_get_project_detail_impl(handler, *,
                                    conn: "sqlite3.Connection") -> None:
    """Shared impl for ``GET /api/project/<key>?weeks=N``.

    Parses the percent-decoded key + the ``weeks`` query param,
    validates ``weeks ∈ {1, 4, 8, 12}``, then delegates to
    ``_project_detail_for_window``. 400 on missing/invalid weeks,
    404 on unknown key, 200 with the detail JSON otherwise.
    """
    import urllib.parse as _urlparse
    raw_path = handler.path
    path_only, sep, query_str = raw_path.partition("?")
    # Strip the route prefix; what remains is the percent-encoded key.
    raw_key = path_only[len("/api/project/"):]
    project_key = _urlparse.unquote(raw_key)
    # Parse `weeks` from the query string.
    query = _urlparse.parse_qs(query_str)
    weeks_vals = query.get("weeks", [None])
    weeks_raw = weeks_vals[0] if weeks_vals else None
    try:
        weeks = int(weeks_raw) if weeks_raw is not None else None
    except (TypeError, ValueError):
        weeks = None
    if weeks not in {1, 4, 8, 12}:
        body = json.dumps({"error": "invalid weeks param"}).encode("utf-8")
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return

    # ``now_utc`` should mirror the current snapshot's generated_at so
    # the endpoint stays consistent with the panel rows the user just
    # clicked through. Pull it from the snapshot when available;
    # otherwise fall back to _command_as_of (CCTALLY_AS_OF honored).
    snap = None
    try:
        snap_ref = getattr(handler, "snapshot_ref", None)
        if snap_ref is not None:
            snap = snap_ref.get()
    except Exception:
        snap = None
    if snap is not None and getattr(snap, "generated_at", None) is not None:
        now_utc = snap.generated_at
    else:
        now_utc = _command_as_of()
    current_week = getattr(snap, "current_week", None) if snap else None
    # Reuse the sync-thread-built envelope when available. The per-process
    # memo invalidates on every session_entries.id advance, so on an
    # active dashboard the drill would otherwise rebuild the envelope
    # from scratch on each click (~1-2s wasted). Plumbing it through
    # skips that work; tests don't set ``projects_envelope`` on the
    # fake snapshot and fall back to the legacy build path.
    projects_envelope = getattr(snap, "projects_envelope", None) if snap else None

    try:
        detail = _project_detail_for_window(
            conn,
            project_key=project_key,
            weeks_back=weeks,
            now_utc=now_utc,
            current_week=current_week,
            projects_envelope=projects_envelope,
        )
    except Exception as exc:
        handler.log_error("/api/project failed: %r", exc)
        body = json.dumps(
            {"error": "project detail failed"}
        ).encode("utf-8")
        handler.send_response(500)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    if detail is None:
        body = json.dumps(
            {"error": "project not found", "key": project_key},
        ).encode("utf-8")
        handler.send_response(404)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    body = json.dumps(detail, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


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


# === Alerts-envelope per-axis row-mappers (Task F) =========================
# Each mapper turns one axis's ``alerted_at IS NOT NULL`` milestone rows into
# the shared envelope-item dicts. The SQL is genuinely heterogeneous per axis
# (Codex P0-3: distinct columns, JOINs, id shapes), so the registry unifies the
# *set* of axes + their ``milestone_table`` + the shared ``severity_for``
# authority, not the query itself. ``descriptor.milestone_table`` drives each
# ``FROM`` clause so the table name lives in the registry, not inlined here.


def _envelope_rows_weekly(conn, descriptor, limit, severity_for) -> list[dict]:
    # ``reset_event_id`` (v1.7.2) segments the same (week, threshold)
    # across pre-credit (0) and post-credit (event.id) cohorts, both
    # of which can be alerted. The envelope id must include the
    # segment so React's <li key={a.id}> / <tr key={a.id}> doesn't
    # collide on the duplicate (week, threshold) pair. Older clients
    # tolerate longer ids — the id is opaque to them; only the React
    # key uniqueness invariant matters.
    rows = conn.execute(
        f"""
        SELECT week_start_date, percent_threshold, captured_at_utc,
               alerted_at, cumulative_cost_usd, reset_event_id
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["percent_threshold"])
        cumulative = float(r["cumulative_cost_usd"])
        dpp = (cumulative / threshold) if threshold else None
        out.append({
            "id": f"weekly:{r['week_start_date']}:{threshold}:{r['reset_event_id']}",
            "axis": descriptor.id,
            "threshold": threshold,
            "severity": severity_for(threshold),
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
    return out


def _envelope_rows_five_hour(conn, descriptor, limit, severity_for) -> list[dict]:
    # Site F (spec §3.2 bucket C / §3.3): widen the row identity to
    # include ``reset_event_id`` so post-credit (seg=event.id) crossings
    # of the same (window_key, threshold) don't collide with pre-credit
    # (seg=0) crossings on the React row key. Older clients tolerate
    # longer ids — the id is opaque to them; only the React key
    # uniqueness invariant matters. Mirrors the weekly precedent.
    rows = conn.execute(
        f"""
        SELECT m.five_hour_window_key, m.percent_threshold, m.captured_at_utc,
               m.alerted_at, m.block_cost_usd, m.reset_event_id,
               b.block_start_at
        FROM {descriptor.milestone_table} m
        LEFT JOIN five_hour_blocks b ON b.five_hour_window_key = m.five_hour_window_key
        WHERE m.alerted_at IS NOT NULL
        ORDER BY m.alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["percent_threshold"])
        out.append({
            "id":          (
                f"five_hour:{int(r['five_hour_window_key'])}:"
                f"{threshold}:{int(r['reset_event_id'])}"
            ),
            "axis":        descriptor.id,
            "threshold":   threshold,
            "severity":    severity_for(threshold),
            "crossed_at":  r["captured_at_utc"],
            "alerted_at":  r["alerted_at"],
            "context": {
                "five_hour_window_key": int(r["five_hour_window_key"]),
                "block_start_at":       r["block_start_at"] or "",
                "block_cost_usd":       float(r["block_cost_usd"] or 0.0),
                "reset_event_id":       int(r["reset_event_id"]),
            },
        })
    return out


def _envelope_rows_budget_family(conn, descriptor, limit, severity_for) -> list[dict]:
    # Unified vendor-tagged budget axis (#143). ONE mapper backs BOTH the
    # ``budget`` (``vendor='claude'``, issue #19) and ``codex_budget``
    # (``vendor='codex'``, calendar-period-codex-budgets spec §6) axes —
    # ``descriptor.vendor`` drives the ``WHERE vendor=?`` row filter + the
    # ``COALESCE(period, <vendor-default-noun>)`` default, ``descriptor.id`` the
    # envelope id prefix, ``descriptor.milestone_table`` (now ``budget_milestones``
    # for both) the source table.
    #
    # Budget alerts are keyed by ``period_start_at`` (the resolved period-window
    # start instant — a subscription-week start for claude OR a calendar
    # period-start for codex) + the write-once ``period`` discriminator (#137) +
    # the integer threshold. No ``reset_event_id`` segment: a mid-week reset
    # (claude) or a period rollover (codex) re-anchors ``period_start_at`` so the
    # new window naturally gets fresh rows under
    # ``UNIQUE(vendor, period_start_at, period, threshold)``.
    #
    # All numbers + the ``period`` noun are read FROM THE ROW (snapshotted at
    # crossing), never live config that may have changed since (the Codex P0-4
    # lesson; Symptom 1 fix) — a user who fires alerts then switches
    # ``budget.period`` keeps the historical row's noun. ``COALESCE(period, …)``
    # renders a pre-011 NULL-sentinel row with the vendor-default noun and keeps
    # the ``id`` non-``None``; the id's ``period`` segment gives a calendar-week ↔
    # calendar-month coinciding-instant collision (now distinct coexisting rows)
    # distinct React keys.
    #
    # Byte-stable identity: the envelope id ==
    # ``f"{descriptor.id}:{period_start_at}:{period}:{threshold}"`` matches the
    # pre-#143 ``budget:…`` / ``codex_budget:…`` strings exactly (same instant
    # value, renamed key column). The per-vendor context dict KEY ORDER matches
    # the pre-#143 envelopes verbatim: the claude/``budget`` axis still emits BOTH
    # the legacy ``week_start_at`` AND ``period_start_at`` (same instant value) so
    # no existing TS consumer of ``context.week_start_at`` breaks; the
    # codex/``codex_budget`` axis emits ``period_start_at`` only.
    vendor = descriptor.vendor
    default_noun = "subscription-week" if vendor == "claude" else "calendar-month"
    rows = conn.execute(
        f"""
        SELECT period_start_at,
               COALESCE(period, ?) AS period,
               threshold, crossed_at_utc, alerted_at,
               budget_usd, spent_usd, consumption_pct
        FROM {descriptor.milestone_table}
        WHERE vendor = ? AND alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (default_noun, vendor, limit),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        if vendor == "claude":
            ctx = {  # key order byte-stable with the pre-#143 budget envelope
                "week_start_at":   r["period_start_at"],
                "period":          r["period"],
                "period_start_at": r["period_start_at"],
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            }
        else:
            ctx = {  # key order byte-stable with the pre-#143 codex_budget envelope
                "period":          r["period"],
                "period_start_at": r["period_start_at"],
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            }
        out.append({
            "id": (
                f"{descriptor.id}:{r['period_start_at']}:{r['period']}:{threshold}"
            ),
            "axis":       descriptor.id,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context":    ctx,
        })
    return out


def _envelope_rows_projected(conn, descriptor, limit, severity_for) -> list[dict]:
    # Fourth axis (issue #121): projected-pace threshold crossings. Like
    # budget, projected alerts re-anchor ``week_start_at`` on a mid-week
    # reset, so there is NO ``reset_event_id`` segment — the new window gets
    # fresh rows under ``UNIQUE(week_start_at, period, metric, threshold)``. The
    # ``metric`` discriminator (``weekly_pct`` | ``budget_usd`` |
    # ``codex_budget_usd``) drives the frontend's metric-aware context renderer;
    # ``denominator`` + ``projected_value`` are rendered FROM THE ROW (the values
    # snapshotted at crossing), never live config that may have changed since
    # (Codex P0-4). The envelope id mirrors the dispatch payload's
    # ``projected:<week_start_at>:<period>:<metric>:<threshold>`` shape. The
    # write-once ``period`` discriminator (#137) carries no symptom-1 label here
    # — projected's ``context`` is metric-driven, never a live-config period
    # noun — so ``COALESCE(period, 'subscription-week')`` is purely for a stable
    # non-``None`` id segment on a pre-011 NULL-sentinel row (the calendar-week ↔
    # calendar-month within-metric collision otherwise shares a React key).
    rows = conn.execute(
        f"""
        SELECT week_start_at,
               COALESCE(period, 'subscription-week') AS period,
               metric, threshold, projected_value,
               denominator, crossed_at_utc, alerted_at
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        metric = str(r["metric"])
        out.append({
            "id": (
                f"projected:{r['week_start_at']}:{r['period']}"
                f":{metric}:{threshold}"
            ),
            "axis":       descriptor.id,
            "metric":     metric,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_at":   r["week_start_at"],
                "metric":          metric,
                "projected_value": float(r["projected_value"]),
                "denominator":     float(r["denominator"]),
            },
        })
    return out


def _envelope_rows_project_budget(conn, descriptor, limit, severity_for) -> list[dict]:
    # Fifth axis (issue #19 / #121): PER-PROJECT equiv-$ budget threshold
    # crossings. Like the global budget axis, project-budget alerts re-anchor
    # ``week_start_at`` on a mid-week reset, so there is NO ``reset_event_id``
    # segment — the new window gets fresh rows under
    # ``UNIQUE(week_start_at, project_key, threshold)``. ``project_key`` is the
    # canonical git-root (``ProjectKey.bucket_path``); the human-readable chip
    # context carries the project BASENAME, resolved through the production
    # ``_resolve_project_key`` (git-root mode) so a moved/deleted repo still
    # renders its basename from the snapshotted path (no FS dependency on a
    # live ``.git``). ``budget_usd`` / ``spent_usd`` / ``consumption_pct`` are
    # rendered FROM THE ROW (snapshotted at crossing), never live config that
    # may have changed since (Codex P0-4). The envelope id mirrors the dispatch
    # payload's ``project_budget:<week_start_at>:<project_key>:<threshold>``
    # shape (``_build_alert_payload_project_budget``).
    rows = conn.execute(
        f"""
        SELECT week_start_at, project_key, threshold, budget_usd, spent_usd,
               consumption_pct, crossed_at_utc, alerted_at
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    c = _cctally()
    # Collision-aware labels via the shared primitive (#130), disambiguated
    # across the alerted ROWS (NOT live config) to preserve the
    # render-from-the-snapshotted-row invariant above — so a deleted/renamed
    # config key still renders. This is intentionally a different feed than the
    # table/notification (full config); see spec §1 Goals (Codex F1).
    label_by_key = c._project_budget_labels(
        sorted({r["project_key"] for r in rows})
    )
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        project_key = r["project_key"]
        out.append({
            "id": (
                f"project_budget:{r['week_start_at']}:{project_key}:{threshold}"
            ),
            "axis":       descriptor.id,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_at":   r["week_start_at"],
                "project":         label_by_key.get(project_key, project_key),
                "project_key":     project_key,
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            },
        })
    return out


# Keyed by ``AlertAxisDescriptor.id`` — the registry decides which axes run,
# in what order; this table supplies the bespoke heterogeneous row-mapper.
# ``budget`` (vendor='claude') and ``codex_budget`` (vendor='codex') share the
# one ``_envelope_rows_budget_family`` mapper (#143) — it reads
# ``descriptor.vendor`` for the row filter + default noun and ``descriptor.id``
# for the envelope id prefix, so the two axes stay distinct on the wire.
_ENVELOPE_AXIS_MAPPERS = {
    "weekly": _envelope_rows_weekly,
    "five_hour": _envelope_rows_five_hour,
    "budget": _envelope_rows_budget_family,
    "projected": _envelope_rows_projected,
    "project_budget": _envelope_rows_project_budget,
    "codex_budget": _envelope_rows_budget_family,
}


def _build_alerts_envelope_array(
    conn: sqlite3.Connection,
    limit: int = 100,
) -> list[dict]:
    """Return the ``alerts`` array for the SSE snapshot envelope.

    Union of ``percent_milestones``, ``five_hour_milestones``,
    ``budget_milestones`` (vendor-tagged — backs BOTH the ``budget`` and
    ``codex_budget`` axes since #143), ``projected_milestones``, and
    ``project_budget_milestones`` rows with
    ``alerted_at IS NOT NULL``, ordered newest-first by ``alerted_at``, capped at
    ``limit`` (default 100). Single source of truth for both the dashboard panel
    (slices to 10 client-side) and the modal (renders all 100). Forward-only
    semantics: only rows the alert-dispatch path stamped get included; pre-deploy
    crossings stay NULL and are intentionally invisible (spec §4.3).

    All six axes share the same envelope schema; the ``axis`` field
    (``weekly`` / ``five_hour`` / ``budget`` / ``projected`` /
    ``project_budget`` / ``codex_budget``) discriminates. The ``projected`` axis
    additionally carries a top-level ``metric`` (``weekly_pct`` | ``budget_usd``
    | ``codex_budget_usd``) so the frontend can pick its metric-aware context
    renderer; ``project_budget``
    carries the project basename + ``$spent of $budget`` in its context;
    ``budget`` + ``codex_budget`` carry a ``period`` discriminator
    (subscription-week / calendar-week / calendar-month) so the frontend renders
    a period-aware "Month" / "Calendar week" / "Week" label.

    Per-axis ``LIMIT`` is applied at the SQL level (each query may yield
    up to ``limit``) and the union is re-sorted + sliced — important for
    the boundary case where one axis has ``limit`` rows and the other
    has more recent ones that would otherwise be dropped before the
    final sort.

    **Registry-driven (Task F).** The *set* of axes, their union *order*,
    and each axis's ``milestone_table`` come from
    ``_lib_alert_axes.AXIS_REGISTRY`` — adding a future axis is "register a
    descriptor + add a row-mapper", not "hand-roll a parallel branch". The
    SQL stays genuinely heterogeneous per axis (Codex P0-3: different
    columns, JOINs, id shapes), so each descriptor pairs with a bespoke
    row-mapper keyed by ``descriptor.id`` in ``_ENVELOPE_AXIS_MAPPERS``. The
    shared ``severity_for`` kernel stamps the additive ``severity`` field on
    every item (single severity authority, consumed by the frontend too).
    """
    c = _cctally()
    registry = c.AXIS_REGISTRY
    severity_for = c.severity_for
    out: list[dict] = []
    for descriptor in registry:
        mapper = _ENVELOPE_AXIS_MAPPERS.get(descriptor.id)
        if mapper is None:  # pragma: no cover - registry/mapper drift guard
            continue
        out.extend(mapper(conn, descriptor, limit, severity_for))

    # Python's list.sort is stable. When two alerts share the same
    # `alerted_at` ISO string (rare; multiple axes firing within the same
    # millisecond), the union order (weekly, then 5h, then budget, then
    # projected) determines the tiebreaker — no extra deterministic key is
    # added because the spec doesn't require one.
    out.sort(key=lambda a: a["alerted_at"], reverse=True)
    return out[:limit]


def _resolve_dashboard_port(port_arg):
    """Effective dashboard port: an explicit --port always wins; otherwise
    8790 under the preview channel (CCTALLY_CHANNEL=preview), else 8789.
    Byte-stable when the marker is unset because any explicit port (incl. 0)
    short-circuits before the env check — the golden harness passes --port 0,
    so the port default is never exercised there."""
    if port_arg is not None:
        return port_arg
    if _cctally_core.is_preview_channel():
        return 8790
    return 8789


def _channel_env_fragment() -> dict:
    """`{'channel': 'preview'}` under the preview channel, else `{}` — so the
    envelope key is omitted when the marker is unset (additive-optional, keeps
    the dashboard goldens byte-identical). Spliced into the envelope via `**`."""
    if _cctally_core.is_preview_channel():
        return {"channel": "preview"}
    return {}


def snapshot_to_envelope(snap: "DataSnapshot", *,
                         now_utc: "dt.datetime",
                         monotonic_now: "float | None" = None,
                         oauth_usage_cfg: "dict | None" = None,
                         display_tz_pref_override: "str | None" = None,
                         runtime_bind: "str | None" = None,
                         transcripts_visible: bool = False) -> dict:
    """Serialize a DataSnapshot into the JSON envelope consumed by the
    browser (design spec §2.2).

    ``transcripts_visible`` gates the transcript-derived session ``title``
    (#264 S3): the key is emitted ONLY when the flag is True AND the row has
    a title, so False (the default) fails closed for any caller that forgets
    to pass it — no ``title`` key, no leaked prompt content. The two
    browser-serving emit sites (``GET /api/data`` + the SSE loop) pass the
    per-request ``_transcripts_visible_to_request()`` — the SAME predicate
    that drives ``transcriptsEnabled`` and the per-row "open conversation"
    button; every other caller (share builders, fixtures, tests) keeps the
    default. ``cache_hit_pct`` (sessions) and ``cost_usd`` (trend) are plain
    numbers and are never gated.

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
    # Issue #57 — prefer ``snap.forecast_view`` (precomputed by the
    # sync thread via ``build_forecast_view``) over re-deriving the
    # projection / verdict / header-routing / budget fields inline.
    # Falls back to the legacy inline routing below when
    # ``forecast_view`` is missing — fixture modules that construct
    # ``DataSnapshot`` positionally without the post-Bundle-1 fields
    # leave it at ``None``, and their goldens predate the View so
    # keeping the legacy path under that fallback preserves byte
    # stability.
    fc_view = getattr(snap, "forecast_view", None)

    # F1 fix: server-resolve the display tz to a CONCRETE IANA name and
    # surface it on the envelope so the browser never has to guess "local".
    # Reused below for week_lbl / blocks / monthly label rendering so the
    # whole envelope speaks one zone consistently.
    # F3 fix: when the dashboard was started with `--tz <X>`, the override
    # supersedes the persisted config.display.tz for the lifetime of the
    # process. The override flows in as a canonical tz token; we layer it
    # onto the config dict via _apply_display_tz_override before resolving
    # so every reader downstream sees one zone.
    # #268 M4: read the config + update-state + doctor from the sync-thread
    # precompute (spec §6) so this stays a PURE renderer — no config.json read,
    # no `security` fork, no update-state file read per SSE client per tick.
    # When the fields are absent (fixtures / the initial empty snapshot /
    # positionally-constructed DataSnapshots) fall back to the inline reads so
    # behavior is byte-identical for those callers.
    _precomp = getattr(snap, "envelope_precompute", None)
    _raw_config = _precomp["config"] if _precomp is not None else load_config()
    config = _apply_display_tz_override(
        _raw_config, display_tz_pref_override
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

    # Forecast field routing (issue #57). ``snap.forecast_view`` is the
    # single source of truth: ``build_forecast_view`` runs the per-
    # method projection / verdict / budget routing once and stashes
    # the surface fields on the View. The legacy inline derivation
    # remains as a fallback for fixture modules that construct
    # ``DataSnapshot`` positionally without populating ``forecast_view``
    # — their goldens predate the View, and the legacy block emits
    # the same numbers so byte stability is preserved.
    fcast_pct: "float | None" = None
    recent_24h_pct: "float | None" = None
    verdict: "str | None" = None
    confidence: "str | None" = None
    budget_100: "float | None" = None
    budget_90: "float | None" = None
    if fc_view is not None:
        fcast_pct = fc_view.week_avg_projection_pct
        recent_24h_pct = fc_view.recent_24h_projection_pct
        # ForecastView.dashboard_verdict / .confidence default to
        # ``"ok"`` / ``"unknown"`` even when ``output is None``; only
        # surface non-None envelope values when there's an actual
        # ForecastOutput backing them so the existing
        # ``verdict / confidence is None when fc is None`` shape stays.
        verdict = fc_view.dashboard_verdict if fc is not None else None
        confidence = fc_view.confidence if fc is not None else None
        budget_100 = fc_view.budget_100_per_day_usd
        budget_90 = fc_view.budget_90_per_day_usd
    elif fc is not None:
        # Legacy inline routing — kept verbatim for positionally-
        # constructed fixture snapshots that don't carry ``forecast_view``.
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
            if fcast_pct is None or p_final_recent != fcast_pct:
                recent_24h_pct = p_final_recent
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
    # verdict pill next to it. The View (issue #57) carries the
    # already-routed ``header_projection_pct``; the fallback path
    # replays the legacy routing inline for fixture snapshots that
    # don't populate ``forecast_view``. The Forecast panel still
    # exposes both ``week_avg_projection_pct`` and
    # ``recent_24h_projection_pct`` unchanged.
    if fc_view is not None and fc is not None:
        header_fcast_pct = fc_view.header_projection_pct
    else:
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
    weekly_env = {
        "rows": [_weekly_row_to_dict(r) for r in snap.weekly_periods],
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals. Sourced from sum-over-visible-rows in the sync thread
        # (``_tui_build_snapshot``) so the footer total is structurally
        # equal to the React panel's ``rows.reduce(...)``. The earlier
        # ``build_weekly_view``-sourced totals undercounted Bug-K
        # pre-credit synthesized rows on credit weeks; the regression is
        # captured by
        # ``test_weekly_envelope_total_matches_sum_of_visible_rows``.
        "total_cost_usd": snap.weekly_total_cost_usd,
        "total_tokens":   snap.weekly_total_tokens,
    }
    monthly_env = {
        "rows": [_monthly_row_to_dict(r) for r in snap.monthly_periods],
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals via sum-over-visible-rows so the React MonthlyPanel's
        # smoking-gun ``rows.reduce(...)`` collapses to a single envelope
        # read with the structural-equality invariant preserved.
        "total_cost_usd": snap.monthly_total_cost_usd,
        "total_tokens":   snap.monthly_total_tokens,
    }

    blocks_env = {
        "rows": [_blocks_row_to_dict(r) for r in snap.blocks_panel],
        # View-model unification follow-up (issue #56): additive scalars
        # so the React BlocksPanel can stop running `rows.reduce(...)`
        # in JS. Cost is summed-over-visible-rows in
        # `_dashboard_build_blocks_view` (same structural invariant as
        # daily/weekly/monthly footers); ``total_tokens`` is sourced
        # from the same view since ``BlocksPanelRow`` doesn't carry
        # token columns and we don't want to widen that shape.
        "total_cost_usd": snap.blocks_total_cost_usd,
        "total_tokens":   snap.blocks_total_tokens,
    }

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
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals via sum-over-visible-rows in the sync thread. Gap days
        # carry ``cost_usd=0.0`` and ``total_tokens=0`` so summing the
        # materialized panel rows preserves the gap-free semantics. The
        # React panel's `rows.reduce(...)` collapses to a single
        # envelope read with the structural-equality invariant
        # preserved.
        "total_cost_usd":      snap.daily_total_cost_usd,
        "total_tokens":        snap.daily_total_tokens,
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
    # #268 M4: reuse the precompute's config (or the fallback load_config()
    # resolved above) — the envelope used to call load_config() a SECOND time
    # here. Within one tick both reads returned the same file, so this is
    # byte-identical.
    _cfg_for_alerts = _raw_config
    try:
        _alerts_cfg = _get_alerts_config(_cfg_for_alerts)
    except _AlertsConfigError as exc:
        _warn_alerts_bad_config_once(exc)
        _alerts_cfg = {
            "enabled": False,
            "weekly_thresholds": [],
            "five_hour_thresholds": [],
            "projected_enabled": False,
            # Mirror the dispatch keys so the new alerts_settings lines
            # (`notifier` / `command_configured`) don't KeyError on a
            # corrupt config. Safe defaults: no notifier override, no
            # configured command.
            "notifier": "auto",
            "command_template": None,
        }
    # Budget is its OWN config block (issue #19) — source budget fields
    # from ``_get_budget_config`` (the validated ``budget`` block), NOT
    # the ``alerts`` block. Defensive: a corrupt budget block must not
    # 500 the whole snapshot — fall back to "no budget / disabled".
    try:
        _budget_cfg = _get_budget_config(_cfg_for_alerts)
    except _BudgetConfigError:
        _budget_cfg = {"weekly_usd": None, "alerts_enabled": True,
                       "alert_thresholds": [], "projected_enabled": False,
                       "projects": {}, "project_alerts_enabled": False}
    alerts_settings = {
        "enabled":              _alerts_cfg["enabled"],
        "weekly_thresholds":    list(_alerts_cfg["weekly_thresholds"]),
        "five_hour_thresholds": list(_alerts_cfg["five_hour_thresholds"]),
        "budget_thresholds":    list(_budget_cfg["alert_thresholds"]),
        "budget_enabled":       _budget_alerts_active(_budget_cfg),
        # Projected-pace opt-in mirrors (#121). Two flags, one per parent
        # axis — the frontend SettingsOverlay seeds two toggles. Sourced
        # from the validated getters' ``projected_enabled`` (default False).
        "projected_weekly_enabled": bool(_alerts_cfg.get("projected_enabled")),
        "projected_budget_enabled": bool(_budget_cfg.get("projected_enabled")),
        # Per-project budget alerts opt-in mirror (issue #19 / #121). Gates the
        # ``project_budget`` axis dispatch only (the display section always
        # renders configured projects). Sourced from the validated budget
        # getter's ``project_alerts_enabled`` (default False) — the frontend
        # SettingsOverlay seeds a single on/off toggle from it.
        "project_alerts_enabled": bool(_budget_cfg.get("project_alerts_enabled")),
        # Codex budget toggle mirrors (#134). The frontend SettingsOverlay
        # seeds two toggles (alerts + projected) + a disabled-with-hint empty
        # state from these three flags. ``_budget_cfg["codex"]`` is ``None`` by
        # default (no Codex budget) → all three default false/absent safely;
        # the ``_BudgetConfigError`` fallback dict above lacks a ``codex`` key,
        # so ``.get("codex")`` → ``None`` is likewise safe.
        "codex_budget_configured":     _budget_cfg.get("codex") is not None,
        "codex_budget_alerts_enabled": bool((_budget_cfg.get("codex") or {}).get("alerts_enabled")),
        "codex_projected_enabled":     bool((_budget_cfg.get("codex") or {}).get("projected_enabled")),
        # Alert-dispatch notifier mirror (Phase B). `notifier` is the
        # validated backend selector ("auto"/"command"/etc.). The raw
        # `command_template` is NEVER mirrored — it routinely holds secrets
        # (webhook URLs, bearer tokens) and the SSE snapshot is broadcast to
        # every connected client. We expose only a boolean: is a custom
        # command configured? (the CLI/config remains the sole writer of the
        # template itself).
        "notifier":           _alerts_cfg.get("notifier", "auto"),
        "command_configured": _alerts_cfg.get("command_template") is not None,
    }
    # Dashboard render-prefs mirror (cache-failure-markers opt-out, spec §5).
    # Reuses the single `_cfg_for_alerts = load_config()` read above (no extra
    # FS hit on the hot path). Default true — absence is treated as ON (opt-out,
    # not opt-in); a hand-edited non-bool surfaces the default. The on/off toggle
    # is honored entirely at client render time; the kernel + API always emit the
    # marker data.
    _dash_cfg = _cfg_for_alerts.get("dashboard") if isinstance(
        _cfg_for_alerts.get("dashboard"), dict) else {}
    _cfm = _dash_cfg.get("cache_failure_markers", True)
    _lt = _dash_cfg.get("live_tail", True)
    dashboard_prefs = {
        "cache_failure_markers": _cfm if isinstance(_cfm, bool) else True,
        "live_tail": _lt if isinstance(_lt, bool) else True,
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
    #
    # #268 M4: read from the sync-thread precompute (spec §6) when present so
    # this stays pure — no update-state file reads per SSE client. The inline
    # try/except is the fallback for fixtures / the initial snapshot, and keeps
    # the SAME error sentinels so the derived block is byte-identical.
    if _precomp is not None:
        _update_state_envelope = _precomp["update_state"]
        _update_suppress_envelope = _precomp["update_suppress"]
    else:
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
            _update_suppress_envelope = {
                "skipped_versions": [], "remind_after": None,
            }
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
    #
    # #268 M4: read the precomputed doctor block from the snapshot (spec §6)
    # so this render path never forks the `security` keychain subprocess. The
    # sync thread computes it ONCE per rebuild via `doctor_payload_memo`
    # (`_tui_precompute_doctor_payload`). The inline try/except below is the
    # fallback for fixtures / the initial snapshot / positionally-constructed
    # DataSnapshots (`doctor_payload=None`) and produces the SAME block, so
    # moving the computation is byte-identical. The lazy `GET /api/doctor`
    # endpoint keeps computing LIVE (an explicit user refresh must be fresh).
    _doctor_payload = getattr(snap, "doctor_payload", None)
    if _doctor_payload is not None:
        doctor_envelope: "dict" = _doctor_payload
    else:
        try:
            _ld = sys.modules["cctally"]._load_sibling("_lib_doctor")
            _doc_state = doctor_gather_state(now_utc=now_utc, runtime_bind=runtime_bind)
            _doc_report = _ld.run_checks(_doc_state)
            doctor_envelope = {
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

    # B1 (#207): the "vs last week" header delta reuses the is_current trend
    # row's delta_dpp ($/1% vs the previous trend row — normally the prior
    # subscription week). Select by the is_current FLAG, not snap.trend[-1]:
    # snap.trend is oldest-first by week_start_date and reset/credit handling
    # can synthesize an intervening row, so position -1 is not guaranteed to
    # be the current week (Codex P1). reversed() picks the latest if ever
    # multiple are flagged. Null-safe: None when no row is current, or when
    # the current row's delta_dpp is itself None — both hide the stat.
    _current_trend = next(
        (r for r in reversed(snap.trend) if r.is_current), None
    ) if snap.trend else None

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
            "vs_last_week_delta": (
                _current_trend.delta_dpp if _current_trend is not None else None
            ),
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
                        # #264 S3: additive weekly cost (already on the row via
                        # build_trend_view); rendered by the Trend modal's Cost
                        # column. ``getattr`` (like ``project_key`` /
                        # ``trend_history_median_dpp``) tolerates minimal trend
                        # shapes — SimpleNamespace test rows / older fixtures —
                        # that predate this nullable field. ``None`` when the
                        # week has no cost snapshot.
                        "cost_usd":       round(_wc, 4) if (_wc := getattr(w, "weekly_cost_usd", None)) is not None else None,
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
                        # #264 S3: additive weekly cost (see weeks[] above; same
                        # getattr tolerance for minimal/older trend shapes).
                        "cost_usd":       round(_wc, 4) if (_wc := getattr(w, "weekly_cost_usd", None)) is not None else None,
                    }
                    for w in (snap.weekly_history or [])
                ],
                # View-model unification (Bundle 1; spec §6.6): the
                # pre-computed 3-sample $/% mean. TrendPanel reads this
                # instead of re-deriving the panel-average.
                "avg_dollars_per_pct": snap.trend_avg_dollars_per_pct,
                # Issue #59 follow-up: pre-computed 4-week-median of
                # non-current ``dollars_per_percent`` over the 12-row
                # history. TrendModal.tsx's ``median4NonCurrent`` helper
                # used to compute this client-side; pre-computing on
                # ``build_trend_view`` keeps the rule
                # (``sort(last 4 non-current dpps)``, midpoint
                # ``(s[1]+s[2])/2``) in one place. ``None`` when fewer
                # than 4 valid non-current samples — modal's client-side
                # fallback handles the ``null`` case.
                "history_median_dpp":
                    getattr(snap, "trend_history_median_dpp", None),
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
                    # Projects-panel cross-nav identity (spec §4.1).
                    # Disambiguated display_key matching the projects
                    # envelope's `current_week.rows[].key`; ``None`` when
                    # the projects envelope sub-build failed, the row's
                    # project_path is missing, or the lookup didn't
                    # resolve (the React cell falls back to plain text).
                    "project_key":  getattr(s, "project_key", None),
                    "cost_usd":     round(s.cost_usd, 4) if s.cost_usd is not None else None,
                    # #264 S3: cache-hit % is a plain metric — always serialized
                    # (never gated). ``None`` when the row has no denominator.
                    "cache_hit_pct": round(s.cache_hit_pct, 1) if s.cache_hit_pct is not None else None,
                    # The session title is transcript-derived content: emit the
                    # `title` KEY only when the per-request transcript gate is
                    # open AND a title exists. Omitted (not null) otherwise, so
                    # a gated-off or untitled session renders the client's
                    # em-dash fallback and committed goldens (empty conversation
                    # fixtures -> no title) never carry a `title` key. Default
                    # transcripts_visible=False therefore fails fully closed.
                    **({"title": getattr(s, "title", None)}
                       if (transcripts_visible
                           and getattr(s, "title", None) is not None)
                       else {}),
                }
                for s in snap.sessions
            ],
        },

        # Projects panel + modal envelope block (spec §5.2).
        # Populated on the sync thread; the serializer reads it back
        # unchanged so it stays a pure renderer (no DB I/O). ``None``
        # on first tick before sync completes; the client renders the
        # panel-empty state in that case.
        "projects":         getattr(snap, "projects_envelope", None),

        # Cache-report panel + modal envelope block (spec
        # 2026-05-21-cache-report-panel-design.md §4.2). Snake_case
        # keys throughout — the envelope is intentionally snake_case
        # end-to-end (envelope.ts:189). ``None`` on first tick before
        # sync completes; the client renders the panel-empty state in
        # that case. envelope_version stays at 2 (additive optional
        # field, matches the update? / doctor? precedent).
        "cache_report":     _cache_report_snapshot_to_dict(
            getattr(snap, "cache_report", None)
        ),

        # threshold-actions T5: see prelude above for rationale.
        "alerts":           alerts_array,
        "alerts_settings":  alerts_settings,

        # Dashboard render-prefs mirror (cache-failure-markers opt-out, spec
        # §5). Additive optional, like alerts_settings — envelope_version
        # stays at 2. The client derives `markersEnabled` from this, defaulting
        # to true when the field is undefined (older server / first tick).
        "dashboard_prefs":  dashboard_prefs,

        # update-subcommand SSE mirror (see comment above the
        # `_load_update_state()` block). Shape matches GET
        # /api/update/status's payload (`{state, suppress}`) so the
        # dashboard client's existing coerceUpdateState/Suppress logic
        # consumes both surfaces uniformly.
        "update":           update_envelope,

        # Doctor aggregate-only block (spec §5.5). Full per-check
        # report fetched lazily via GET /api/doctor.
        "doctor":           doctor_envelope,

        # Preview channel (CCTALLY_CHANNEL=preview): additive-optional
        # `channel` key. Omitted when the marker is unset so prod payloads
        # (and the dashboard goldens) stay byte-identical; feeds the header
        # PREVIEW badge when set.
        **_channel_env_fragment(),
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


def _qs_int(q: dict, key: str, default: int) -> int:
    """Parse a single query-string int with a fallback.

    ``q`` is a ``urllib.parse.parse_qs`` mapping (list-valued). A missing key,
    an empty value, or a non-integer spelling all fall back to ``default`` —
    the kernels clamp bounds, so this only needs to be permissive, not strict.
    """
    vals = q.get(key, [None])
    raw = vals[0] if vals else None
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _qs_str(q: dict, key: str, default: str | None) -> str | None:
    """Parse a single query-string value with a fallback.

    ``q`` is a ``urllib.parse.parse_qs`` mapping (list-valued). A missing key
    or an empty list falls back to ``default``. The string sibling of
    ``_qs_int`` — collapses the ``(q.get(key, [default]) or [default])[0]``
    idiom the conversation handlers hand-respelled (a ``None`` default is fine,
    so the reader's ``after`` cursor routes through it too).
    """
    vals = q.get(key, [default])
    return vals[0] if vals else default


# #177 S6 / #217 S2: valid kind facets for the conversation routes. Kept in
# lockstep with the kernel (``_lib_conversation_query._SEARCH_KINDS`` /
# ``_FIND_KINDS``; the kernel re-raises ValueError on an unknown kind, and the
# handlers reject with a 400 BEFORE the call — ``_run_conversation_query``
# collapses every kernel exception to a 500, so a per-route 4xx must be decided
# in the handler, not via try/except around the kernel).
#
# P1-1 (load-bearing kind-validation SPLIT): the cross-session search route
# accepts ``title`` and ``files``; the in-conversation ``/find`` route does NOT —
# its kernel (``find_in_conversation``) indexes ``_FIND_KIND_COLUMNS[kind]``,
# which has no ``title``/``files`` entry, so accepting them there would be a 500
# KeyError. Two distinct tuples keep ``/find?kind=title`` and ``/find?kind=files``
# a clean 400.
_CONV_SEARCH_KINDS = (
    "all", "prompts", "assistant", "tools", "thinking", "title", "files")
_CONV_FIND_KINDS = ("all", "prompts", "assistant", "tools", "thinking")


class _BadConversationFilter(Exception):
    """Internal sentinel: a browse-filter query param failed validation. The
    parse helper has ALREADY sent the 400 response when this is raised, so the
    caller just unwinds and returns (the conversation routes all 400 on bad
    input, consistent with the search ``kind`` facet). Module-private."""


def _cached_file_sigs(conn, paths):
    """{path: size_bytes} from session_files for the given paths — the cache's
    own view of how far each file is ingested. Size-only by design, matching the
    watch kernel's size-only signature (`file_sig`) and sync_cache's size-only
    delta signal: mtime is NOT consulted, because a size-unchanged ingest does
    not refresh session_files.mtime_ns and a stale mtime would re-detect
    'changed' every cycle forever. Used to baseline the live-tail watch so a file
    the cache hasn't caught up on reads as 'changed' on cycle 1 (spec §2.4).
    Paths with no row are simply absent → treated as changed."""
    out = {}
    if not paths:
        return out
    placeholders = ",".join("?" for _ in paths)
    try:
        rows = conn.execute(
            f"SELECT path, size_bytes FROM session_files "
            f"WHERE path IN ({placeholders})", list(paths)).fetchall()
    except sqlite3.OperationalError:
        return out
    for p, size in rows:
        out[p] = size
    return out


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
    # Conversation viewer (Plan 2, spec §5): the resolved
    # `dashboard.expose_transcripts` opt-in. False = transcript endpoints
    # are served only over loopback; True = LAN devices reach them at the
    # bind's IP literal (anti-DNS-rebinding still rejects hostnames). Set by
    # cmd_dashboard before serve_forever; the per-request gate
    # (`_require_transcripts_allowed`) ANDs this with the request Host.
    cctally_expose_transcripts: bool = False

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
        elif path == "/favicon.ico":
            # #207 D11 — serve the SVG favicon for the browser's default
            # /favicon.ico request so it stops 404-ing even absent the
            # <link rel="icon"> in dashboard.html. Vite copies public/ verbatim
            # into the build output, so favicon.svg lands under static_dir.
            self._serve_static_file(self.static_dir / "favicon.svg",
                                    "image/svg+xml")
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
        elif path.startswith("/api/project/"):
            self._handle_get_project_detail()
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
        elif path == "/api/conversations/facets":
            self._handle_get_conversations_facets()
        elif path == "/api/conversations":
            self._handle_get_conversations()
        elif path == "/api/conversation/search":
            self._handle_get_conversation_search()
        elif path.startswith("/api/conversation/") and path.endswith("/payload"):
            # #178: on-demand load-full. Matched BEFORE the <id> reader
            # catch-all (same precedence as /api/conversation/search).
            self._handle_get_conversation_payload(path)
        elif path.startswith("/api/conversation/") and path.endswith("/media"):
            # #177 S4: on-demand media bytes. Matched BEFORE the <id> reader.
            self._handle_get_conversation_media(path)
        elif path.startswith("/api/conversation/") and path.endswith("/outline"):
            # #177 S5: full-session outline skeleton + stats. Matched BEFORE
            # the <id> reader catch-all (Codex F2 — same precedence as /payload).
            self._handle_get_conversation_outline(path)
        elif path.startswith("/api/conversation/") and path.endswith("/find"):
            # #177 S6: in-conversation find → rendered-turn anchors. Matched
            # BEFORE the <id> reader catch-all (same precedence as /outline).
            self._handle_get_conversation_find(path)
        elif path.startswith("/api/conversation/") and path.endswith("/events"):
            # Live-tail SSE for the open reader (spec §2). Matched BEFORE the
            # <id> reader catch-all.
            self._handle_get_conversation_events(path)
        elif path.startswith("/api/conversation/") and path.endswith("/export"):
            # #217 S5: whole-session Markdown export (F1/F5). Matched BEFORE the
            # <id> reader catch-all (same precedence as /outline).
            self._handle_get_conversation_export(path)
        elif path.startswith("/api/conversation/") and path.endswith("/prompts"):
            # #217 S7: ordered main-thread prompt spine for session comparison
            # (F10). Matched BEFORE the <id> reader catch-all (same precedence
            # as /outline).
            self._handle_get_conversation_prompts(path)
        elif path.startswith("/api/conversation/"):
            self._handle_get_conversation_detail(path)
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
            do_refresh = (
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                .get("refresh", ["1"])[0] != "0"
            )
            warnings: list = []
            if do_refresh and not type(self).no_sync:
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

    @staticmethod
    def _transcript_gate():
        """Lazy-load the pure transcript-access gate kernel (Plan 2, §5)."""
        return sys.modules["cctally"]._load_sibling("_lib_transcript_access")

    def _transcripts_visible_to_request(self) -> bool:
        """Single source of truth for "may transcripts be served to THIS
        request?" Composes the bind gate (`transcripts_allowed`) with the
        per-request Host allowlist (`host_allowed_for_transcripts`,
        anti-DNS-rebinding). Spec §5.

        `_require_transcripts_allowed` (the GET-route 403 gate) and the
        `transcriptsEnabled` client signal on BOTH the `/api/data` route
        (`_serve_api_data`) and the SSE stream (`_serve_api_events`) route
        through this predicate so they are contractually identical — a future
        one-line drift can never re-introduce the enabled-then-403 desync.
        """
        ta = self._transcript_gate()
        expose = bool(type(self).cctally_expose_transcripts)
        return (ta.transcripts_allowed(type(self).cctally_host, expose)
                and ta.host_allowed_for_transcripts(
                    self.headers.get("Host", ""), expose))

    def _require_transcripts_allowed(self) -> bool:
        """True if transcripts may be served to THIS request; else emit 403 and
        return False. Spec §5.
        """
        if not self._transcripts_visible_to_request():
            self._respond_403("transcripts not exposed")
            return False
        return True

    def _handle_post_settings(self) -> None:
        """Persist a settings update and trigger an immediate SSE broadcast.

        Body shape: ``{"display"?: {"tz": "..."}, "alerts"?: {...},
        "update"?: {"check"?: {"enabled"?: bool, "ttl_hours"?: int}},
        "cache_report"?: {"anomaly_threshold_pp"?: int},
        "budget"?: {"weekly_usd"?: number|null, "alerts_enabled"?: bool,
        "alert_thresholds"?: int[], "projected_enabled"?: bool,
        "codex"?: {"alerts_enabled"?: bool, "projected_enabled"?: bool}}}``
        — every top-level key is optional; any subset may be sent together
        (combined save). Unknown top-level keys are rejected with 400.

        Per-block validation:
          * ``display.tz`` — "local", "utc", or a valid IANA zone (via
            ``normalize_display_tz_value``); 400 on invalid.
          * ``alerts`` — must be a dict; ``alerts.enabled`` and
            ``alerts.projected_enabled`` must each be a JSON boolean
            (string "yes"/"true" rejected, per spec). Merged block is
            validated via ``_get_alerts_config(merged)``;
            ``_AlertsConfigError`` → 400.
          * ``update.check.enabled`` — JSON bool; 400 on type mismatch.
          * ``update.check.ttl_hours`` — JSON int (NOT string), in
            ``[1, 720]``; 400 on out-of-range or non-int. Bool is rejected
            (Python ``True`` is an int subclass, so a permissive check
            would silently accept ``true`` for a numeric field).
          * ``cache_report.anomaly_threshold_pp`` — JSON int (NOT bool /
            float / string), in ``[1, 100]``; 400 with
            ``{error, field: "anomaly_threshold_pp"}`` on out-of-range
            or non-int. Spec §6.1 hardcodes ``anomaly_window_days``;
            F10 tracks lifting that.
          * ``budget`` — must be a dict; the inbound leaves
            (``weekly_usd`` / ``alerts_enabled`` / ``alert_thresholds`` /
            ``projected_enabled``) are merged onto the persisted ``budget``
            block and validated via ``_get_budget_config(merged)`` (issue
            #19, projected toggle #121); ``_BudgetConfigError`` → 400.
            Budget is its OWN config block, distinct from ``alerts``.
            ``budget.codex`` (#134) is a nested partial-merge: only the two
            toggles (``alerts_enabled`` / ``projected_enabled``) are
            dashboard-writable — ``amount_usd`` / ``period`` /
            ``alert_thresholds`` stay CLI-only and are preserved from the
            persisted block. A non-dict ``budget.codex`` → 400; toggling
            ``budget.codex.*`` when no Codex budget is configured → 400
            (fail-closed; amounts are never invented from the dashboard).

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
        sent) is the full validated block from ``_get_alerts_config``,
        except the raw ``command_template`` is redacted to the boolean
        ``command_configured`` (it routinely holds secrets — webhook URLs
        / bearer tokens — and the echo is returned to the client; the
        SSE ``alerts_settings`` mirror redacts identically). Do NOT
        re-add the raw template to the echo.
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
        allowed_top_keys = {
            "display", "alerts", "update", "cache_report", "budget", "dashboard",
        }
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
            and "cache_report" not in payload
            and "budget" not in payload
            and "dashboard" not in payload
        ):
            self._respond_json(
                400,
                {"error": (
                    "body must contain at least one of: "
                    "display, alerts, update, cache_report, budget, dashboard"
                )},
            )
            return

        # Pre-validate cache_report block (spec 2026-05-21 §6.2). Outside
        # the config_writer_lock so a 400 short-circuit doesn't take the
        # lock unnecessarily. Returns HTTP 400 (NOT 422) on validation
        # error — matches the convention every other block here uses.
        #
        # Validator returns a dict of ONLY the keys present in the input
        # (partial-PUT semantics, mirroring the ``update.check`` block
        # at ~line 5277). The handler below merges this into the
        # existing persisted ``cache_report`` block so a combined save
        # that omits ``anomaly_threshold_pp`` does not clobber the
        # user's persisted threshold with the default.
        cache_report_validated: "dict | None" = None
        if "cache_report" in payload:
            cache_report_block = payload["cache_report"]
            if not isinstance(cache_report_block, dict):
                self._respond_json(
                    400,
                    {"error": "cache_report must be an object",
                     "field": "cache_report"},
                )
                return
            try:
                cache_report_validated = _validate_cache_report_settings(
                    cache_report_block
                )
            except _CacheReportConfigError as exc:
                self._respond_json(
                    400,
                    {"error": str(exc), "field": exc.field or "cache_report"},
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
            if "projected_enabled" in alerts_block and not isinstance(
                alerts_block["projected_enabled"], bool
            ):
                self._respond_json(
                    400,
                    {"error": "alerts.projected_enabled must be a JSON boolean"},
                )
                return
            # The dispatch command template is CLI/config-only — never
            # settable via the dashboard (it routinely holds secrets and the
            # dashboard echoes settings to the client). Reject it explicitly
            # rather than silently dropping it.
            if "command_template" in alerts_block:
                self._respond_json(
                    400,
                    {"error": "alerts.command_template is CLI/config-only "
                              "(not settable via the dashboard)"},
                )
                return
            # `notifier` is settable (the backend selector). Structural type
            # check only; the enum + cross-field rule (command needs a stored
            # template) is enforced free by `_get_alerts_config(merged)` below.
            if "notifier" in alerts_block and not isinstance(
                alerts_block["notifier"], str
            ):
                self._respond_json(
                    400, {"error": "alerts.notifier must be a string"}
                )
                return

        # Pre-validate budget shape (the structural type check; full
        # cross-key validation runs inside the lock via
        # ``_get_budget_config(merged)``). Budget is its OWN config block
        # (issue #19), not part of ``alerts``.
        if "budget" in payload:
            budget_block = payload["budget"]
            if not isinstance(budget_block, dict):
                self._respond_json(
                    400, {"error": "budget must be an object"}
                )
                return

        # Pre-validate the dashboard block (spec §5). Only
        # ``cache_failure_markers`` is dashboard-writable — a JSON boolean
        # (string/int rejected, mirroring the strict bool checks for
        # ``alerts.enabled``). ``dashboard.bind`` / ``dashboard.expose_transcripts``
        # are bind-time / privacy-gate settings, NOT live-mutable, so they are
        # rejected explicitly here (rather than silently dropped). Outside the
        # config_writer_lock so a 400 short-circuit doesn't take the lock.
        dashboard_validated: "dict | None" = None
        if "dashboard" in payload:
            dashboard_block = payload["dashboard"]
            if not isinstance(dashboard_block, dict):
                self._respond_json(
                    400,
                    {"error": "dashboard must be an object", "field": "dashboard"},
                )
                return
            for leaf in dashboard_block.keys():
                if leaf in ("bind", "expose_transcripts"):
                    self._respond_json(
                        400,
                        {"error": (f"dashboard.{leaf} is not settable via the "
                                   "dashboard (bind-time / privacy-gate setting)"),
                         "field": f"dashboard.{leaf}"},
                    )
                    return
                if leaf not in ("cache_failure_markers", "live_tail"):
                    self._respond_json(
                        400,
                        {"error": f"unknown dashboard settings key: {leaf}",
                         "field": f"dashboard.{leaf}"},
                    )
                    return
            dashboard_validated = {}
            for _leaf in ("cache_failure_markers", "live_tail"):
                if _leaf in dashboard_block:
                    if not isinstance(dashboard_block[_leaf], bool):
                        self._respond_json(
                            400,
                            {"error": f"dashboard.{_leaf} must be a JSON boolean",
                             "field": f"dashboard.{_leaf}"},
                        )
                        return
                    dashboard_validated[_leaf] = dashboard_block[_leaf]

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
                if "projected_enabled" in alerts_in:
                    merged_alerts["projected_enabled"] = (
                        alerts_in["projected_enabled"]
                    )
                if "notifier" in alerts_in:
                    merged_alerts["notifier"] = alerts_in["notifier"]
                merged["alerts"] = merged_alerts
                # Final cross-field validation against the merged block.
                # _AlertsConfigError → 400 (no partial write since
                # save_config has not yet been called).
                try:
                    _get_alerts_config(merged)
                except _AlertsConfigError as exc:
                    self._respond_json(400, {"error": str(exc)})
                    return

            if "budget" in payload:
                # Same hand-edited-junk guard as alerts: a non-dict stored
                # ``budget`` block in config.json should surface as a
                # recoverable 400, not a 500. Budget is its OWN config
                # block (issue #19); the inbound keys are
                # ``weekly_usd`` / ``alerts_enabled`` / ``alert_thresholds``.
                existing_budget = merged.get("budget")
                if existing_budget is not None and not isinstance(
                    existing_budget, dict
                ):
                    self._respond_json(
                        400, {"error": "budget must be an object"}
                    )
                    return
                merged_budget = dict(existing_budget or {})
                budget_in = payload["budget"]
                for leaf in (
                    "weekly_usd", "alerts_enabled", "alert_thresholds",
                    "projected_enabled", "project_alerts_enabled",
                ):
                    if leaf in budget_in:
                        merged_budget[leaf] = budget_in[leaf]
                # Nested partial-merge for the Codex sub-block (#134). Mirrors
                # the ``update.check`` nested-dict merge below: only the two
                # alert toggles (``alerts_enabled`` / ``projected_enabled``) are
                # dashboard-writable — ``amount_usd`` / ``period`` /
                # ``alert_thresholds`` stay CLI-only (ignored if sent), and the
                # merge preserves them from the persisted block rather than
                # replacing the whole ``codex`` dict (the clobber regression).
                if "codex" in budget_in:
                    incoming_codex = budget_in["codex"]
                    if not isinstance(incoming_codex, dict):
                        self._respond_json(
                            400, {"error": "budget.codex must be an object"}
                        )
                        return
                    existing_codex = merged_budget.get("codex")
                    if existing_codex is None:
                        # Fail closed: the dashboard only TOGGLES an existing
                        # Codex budget — amounts are CLI-only — so it must never
                        # invent one. The frontend disables the toggle, this
                        # backstops a direct POST.
                        self._respond_json(400, {"error": (
                            "no Codex budget configured — set one via the CLI "
                            "first (cctally budget set <amount> --vendor codex)"
                        )})
                        return
                    merged_codex = dict(existing_codex)
                    for sub in ("alerts_enabled", "projected_enabled"):
                        if sub in incoming_codex:
                            merged_codex[sub] = bool(incoming_codex[sub])
                    merged_budget["codex"] = merged_codex
                merged["budget"] = merged_budget
                # Final validation against the merged block.
                # _BudgetConfigError → 400 (no partial write — save_config
                # has not yet been called).
                try:
                    _get_budget_config(merged)
                except _BudgetConfigError as exc:
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

            if cache_report_validated is not None:
                # Same hand-edited-junk guard as alerts / update: a non
                # -dict ``cache_report`` block in config.json should
                # surface as a recoverable 400, not a 500.
                existing_cr = merged.get("cache_report")
                if existing_cr is not None and not isinstance(existing_cr, dict):
                    self._respond_json(
                        400, {"error": "cache_report must be an object",
                              "field": "cache_report"}
                    )
                    return
                # Partial-PUT merge: preserve keys the request didn't
                # touch (mirrors the update.check block at ~line 5371).
                # Becomes load-bearing once F10 lifts
                # ``anomaly_window_days`` to config — until then it
                # still defends a combined save (e.g. display + empty
                # cache_report) from clobbering ``anomaly_threshold_pp``
                # with the default.
                merged_cr = dict(existing_cr or {})
                merged_cr.update(cache_report_validated)
                merged["cache_report"] = merged_cr

            if dashboard_validated is not None:
                # Same hand-edited-junk guard as the other blocks: a non-dict
                # stored ``dashboard`` block should surface as a recoverable
                # 400, not a 500. Partial-merge so the (bind-time, CLI-only)
                # ``bind`` / ``expose_transcripts`` siblings are PRESERVED —
                # cache_failure_markers writes must never clobber them.
                existing_dash = merged.get("dashboard")
                if existing_dash is not None and not isinstance(existing_dash, dict):
                    self._respond_json(
                        400, {"error": "dashboard must be an object",
                              "field": "dashboard"}
                    )
                    return
                merged_dash = dict(existing_dash or {})
                merged_dash.update(dashboard_validated)
                merged["dashboard"] = merged_dash

            save_config(merged)

        # Build the response: subset of touched blocks.
        out: dict = {}
        if display_canonical is not None:
            out["display"] = _compute_display_block(
                merged, dt.datetime.now(dt.timezone.utc)
            )
        if "alerts" in payload:
            # Echo the full validated alerts block (defaults filled) so the
            # SettingsOverlay can repaint without a follow-up GET — but
            # redact the raw `command_template` (secrets) the same way the
            # SSE snapshot mirror does: replace it with a boolean
            # `command_configured`.
            _a = dict(_get_alerts_config(merged))
            _a["command_configured"] = _a.pop("command_template", None) is not None
            out["alerts"] = _a
        if "budget" in payload:
            # Echo the full validated budget block (defaults filled) so the
            # SettingsOverlay can repaint without a follow-up GET.
            validated_budget = _get_budget_config(merged)
            out["budget"] = validated_budget
            # Forward-only reconcile (mirrors `budget set` / `config set
            # budget.*`): enabling/raising a budget while already past a
            # threshold records the crossed thresholds as already-alerted so
            # the next record-usage tick does NOT dispatch retroactive alerts.
            # Runs AFTER save_config (config persisted first); best-effort —
            # never breaks the 200 response. Config write already left the
            # config_writer_lock, so the helper's open_db never nests.
            #
            # Gate each axis on the touched leaves (parity with `config set`):
            # running on an unrelated leaf would latch a currently-over-but-
            # not-yet-dispatched threshold, permanently suppressing the next
            # tick's dispatch. The dashboard accepts no `projects` leaf (the
            # map is CLI-only), so the per-project axis keys on
            # project_alerts_enabled/alert_thresholds.
            budget_in = payload["budget"]
            touched = (
                set(budget_in.keys()) if isinstance(budget_in, dict) else set()
            )
            # ``period`` included (#143 §5.4 fold-in): switching budget.period
            # via the dashboard while already over a threshold must reconcile
            # forward-only, exactly like the CLI `config set budget.period` path
            # (`_cctally_config.py`) — else the next record-usage tick would
            # instant-popup retroactive alerts under the new period window.
            if touched & {"weekly_usd", "alerts_enabled", "alert_thresholds", "period"}:
                _cctally()._reconcile_budget_on_config_write(validated_budget)
            if touched & {"project_alerts_enabled", "alert_thresholds"}:
                _cctally()._reconcile_project_budget_milestones_on_write(
                    validated_budget
                )
            # Codex actual-spend reconcile (#134). Key it on the ``alerts_enabled``
            # SUB-leaf specifically — NOT ``"codex" in touched``. The helper
            # (`_reconcile_codex_budget_on_config_write`) is itself gated on
            # alerts_enabled && amount && thresholds, so a coarse "codex touched"
            # check would run it whenever alerts is already True — meaning a
            # ``projected_enabled``-only toggle would latch (silently suppress) a
            # still-unfired actual-spend crossing. Keying on the sub-leaf means
            # flipping alerts ON latches already-crossed thresholds (intended),
            # while toggling projected reconciles nothing (projected stays
            # live-pace, Q4).
            codex_in = budget_in.get("codex")
            if isinstance(codex_in, dict) and "alerts_enabled" in codex_in:
                _cctally()._reconcile_codex_budget_on_config_write(
                    validated_budget
                )
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
        if cache_report_validated is not None:
            # Echo the full cooked block (resolved defaults included) so
            # the dashboard composer can repaint without a follow-up GET
            # — mirrors the update.check echo at ~line 5402. Read from
            # the merged cache_report we just wrote; fall back to the
            # documented default when neither the request nor the
            # persisted config carries an explicit value.
            persisted_cr = merged.get("cache_report") or {}
            stored_threshold = persisted_cr.get("anomaly_threshold_pp", 15)
            out["cache_report"] = {
                "anomaly_threshold_pp": stored_threshold,
            }
        if dashboard_validated is not None:
            # Echo the persisted dashboard-writable leaves (cache_failure_markers
            # + live_tail) so the SettingsOverlay can repaint without a
            # follow-up GET. Default true (opt-out) when nothing is persisted.
            persisted_dash = merged.get("dashboard") or {}
            out["dashboard"] = {
                "cache_failure_markers": bool(
                    persisted_dash.get("cache_failure_markers", True)
                ),
                "live_tail": bool(persisted_dash.get("live_tail", True)),
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

        Body (all fields optional): ``{"axis":
        "weekly"|"five_hour"|"budget"|"projected"|"project_budget",
        "threshold": 1..100, "metric": "weekly_pct"|"budget_usd"}``. Defaults:
        axis="weekly", threshold=90, metric="weekly_pct". ``metric`` is only
        consulted for the ``projected`` axis (mirrors the CLI ``alerts test
        --axis projected --metric`` surface); it is ignored for the other
        axes. The ``project_budget`` axis dispatches a synthetic example
        project ($26 of $25) — no real ``budget.projects`` entry required.

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
        if axis not in (
            "weekly", "five_hour", "budget", "projected", "project_budget",
            "codex_budget",
        ):
            self._respond_json(
                400,
                {"error": (
                    "axis must be 'weekly', 'five_hour', 'budget', "
                    "'projected', 'project_budget' or 'codex_budget', "
                    f"got {axis!r}"
                )},
            )
            return
        # ``metric`` discriminates the projected axis (weekly_pct vs
        # budget_usd vs codex_budget_usd); the other axes ignore it. Validate
        # only when it matters so a stray metric on a weekly/budget test isn't
        # a 400.
        metric = body.get("metric", "weekly_pct")
        if axis == "projected" and metric not in (
            "weekly_pct", "budget_usd", "codex_budget_usd",
        ):
            self._respond_json(
                400,
                {"error": (
                    "metric must be 'weekly_pct', 'budget_usd' or "
                    f"'codex_budget_usd', got {metric!r}"
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
        elif axis == "budget":
            # Synthetic budget payload — mirrors the CLI cmd_alerts_test
            # budget branch (NO DB writes, test/real divergence contract).
            # spent scaled to the threshold so the body reads plausibly
            # (e.g. 100% → $300 of $300).
            payload = _build_alert_payload_budget(
                threshold=threshold,
                crossed_at_utc=now_utc_iso(),
                week_start_at=dt.date.today().isoformat(),
                budget_usd=300.0,
                spent_usd=300.0 * threshold / 100.0,
                consumption_pct=float(threshold),
            )
        elif axis == "project_budget":
            # Synthetic per-project budget payload — mirrors the CLI
            # ``alerts test --axis project_budget`` branch (NO DB writes,
            # test/real divergence contract). Uses a fixed example project
            # ($26 of $25 = 104%) so no real ``budget.projects`` entry is
            # required; ``project_key`` is a placeholder canonical path.
            payload = _build_alert_payload_project_budget(
                threshold=threshold,
                crossed_at_utc=now_utc_iso(),
                week_start_at=dt.date.today().isoformat(),
                project="example-project",
                project_key="/example/repos/example-project",
                budget_usd=25.0,
                spent_usd=26.0,
                consumption_pct=104.0,
            )
        elif axis == "codex_budget":
            # Synthetic Codex budget payload — mirrors the CLI
            # ``alerts test --axis codex-budget`` branch (NO DB writes,
            # test/real divergence contract, NO real budget.codex entry
            # required). A $200 calendar-month budget reads plausibly; spent
            # scaled to the threshold so the body line reads as the
            # at-crossing snapshot the dashboard would render (R4).
            payload = _build_alert_payload_codex_budget(
                threshold=threshold,
                crossed_at_utc=now_utc_iso(),
                period_start_at=dt.date.today().replace(day=1).isoformat(),
                period="calendar-month",
                budget_usd=200.0,
                spent_usd=200.0 * threshold / 100.0,
                consumption_pct=float(threshold),
            )
        elif axis == "projected":
            # Synthetic projected-pace payload — mirrors the CLI
            # cmd_alerts_test projected branch (NO DB writes, test/real
            # divergence contract). The metric discriminator picks the
            # wiring; projected_value is the threshold's denominator-relative
            # value (so the body reads plausibly, e.g. weekly 100% → "~100% of
            # cap", budget 100% → "$300 of $300"). denominator is the
            # at-crossing target the row would carry (Codex P0-4): 100.0 for
            # weekly_pct, $300 for budget_usd, $200 for codex_budget_usd
            # (matching the codex_budget axis test-alert budget).
            if metric == "budget_usd":
                denominator = 300.0
                projected_value = 300.0 * threshold / 100.0
            elif metric == "codex_budget_usd":
                denominator = 200.0
                projected_value = 200.0 * threshold / 100.0
            else:  # weekly_pct
                denominator = 100.0
                projected_value = float(threshold)
            payload = _build_alert_payload_projected(
                metric=metric,
                threshold=threshold,
                projected_value=projected_value,
                denominator=denominator,
                week_start_at=dt.date.today().isoformat(),
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
        # Resolve the per-request transcript gate ONCE and use it for both the
        # in-envelope session `title` gate (#264 S3) and the `transcriptsEnabled`
        # client signal below — one predicate, two consumers, desync impossible.
        visible = self._transcripts_visible_to_request()
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
            transcripts_visible=visible,
        )
        # Conversation viewer (Plan 2, spec §5): inject the client signal
        # PER-REQUEST and Host-aware — NOT inside snapshot_to_envelope (the
        # request-independent SSE snapshot has no Host header). Routes through
        # the SAME predicate as the transcript GET-route gate so a LAN-hostname
        # request that the transcript GETs would 403 shows
        # transcriptsEnabled=false, never enabled-then-403 (the pass-2 P2
        # finding) — one predicate, two consumers, desync impossible.
        env["transcriptsEnabled"] = visible
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

    @staticmethod
    def _conversation_query():
        """Lazy-load the pure conversation query kernel (Plan 2, §3)."""
        return sys.modules["cctally"]._load_sibling("_lib_conversation_query")

    def _parse_search_kind(self, q, valid=_CONV_SEARCH_KINDS):
        """Read + validate the ``kind`` facet for a conversation route (#177 S6 /
        #217 S2). Returns the kind on success, or ``None`` after having ALREADY
        sent a 400 — callers just ``return`` on ``None``.

        ``valid`` is the per-route kind set (P1-1 split): the cross-session search
        route passes ``_CONV_SEARCH_KINDS`` (includes ``title``), the
        in-conversation ``/find`` route passes ``_CONV_FIND_KINDS`` (excludes
        ``title``/``files``), so ``/find?kind=title`` is a 400 here — never a 500
        KeyError downstream in ``find_in_conversation``. Kept in lockstep with the
        kernel's ``_SEARCH_KINDS`` / ``_FIND_KINDS`` (the kernel module is
        resolved lazily per-request, so the handler keeps literal tuples rather
        than reaching across that import edge for a nit)."""
        kind = _qs_str(q, "kind", "all")
        if kind not in valid:
            self._respond_json(400, {"error": f"unknown kind: {kind}"})
            return None
        return kind

    def _run_conversation_query(self, kernel_call, log_label):
        """Open cache.db, run ``kernel_call(conn)``, close — with the uniform
        500 envelopes the three conversation routes share (#151).

        Collapses the triplicated open-cache → try/except/finally → 500
        scaffold to one site. Returns ``(ok, body)``: ``ok=False`` means a 500
        has ALREADY been sent and the caller must just ``return``; ``ok=True``
        carries the kernel result (which may itself be ``None`` — the reader's
        404 sentinel — so the explicit flag, not ``body is None``, signals
        failure). An ``open_cache_db`` failure is a ``cache unavailable:`` 500;
        a kernel exception is logged as ``<log_label> failed: %r`` and returned
        as a ``{type}: {msg}`` 500 — byte-identical to the inlined handlers.
        """
        try:
            conn = open_cache_db()
        except (sqlite3.DatabaseError, OSError) as exc:
            self._respond_json(500, {"error": f"cache unavailable: {exc}"})
            return False, None
        try:
            body = kernel_call(conn)
        except Exception as exc:  # noqa: BLE001
            self.log_error("%s failed: %r", log_label, exc)
            self._respond_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return False, None
        finally:
            conn.close()
        return True, body

    def _parse_conversation_filters(self, q):
        """Parse the browse-list filter params (spec §2) from a ``parse_qs``
        mapping. On any malformed value this sends a **400** and returns
        ``None`` — the caller just ``return``s (the conversation routes all 400
        on bad input). On success returns a dict of ``list_conversations``
        kwargs: ``date_from``/``date_to`` (UTC-ISO bounds), ``projects``
        (list[str] | None), ``cost_min``/``cost_max`` (float | None),
        ``rebuild_min`` (int | None). Empty/blank params drop to ``None``.

        Numeric axes validate strictly (a non-numeric cost / non-integer
        rebuild threshold is a hard 400). Date bounds route through the pure
        ``_lib_dashboard_dates.parse_filter_date_range`` helper, which resolves
        naive date-only bounds in ``display.tz`` and raises ``ValueError`` (→
        400) on a malformed date. Projects accept BOTH repeated
        ``?projects=a&projects=b`` and a single comma-joined ``?projects=a,b``.
        """
        def _float(name):
            v = _qs_str(q, name, "")
            if v is None or v == "":
                return None
            try:
                return float(v)
            except ValueError:
                self._respond_json(400, {"error": f"bad {name}: {v}"})
                raise _BadConversationFilter

        def _int(name):
            v = _qs_str(q, name, "")
            if v is None or v == "":
                return None
            try:
                return int(v)
            except ValueError:
                self._respond_json(400, {"error": f"bad {name}: {v}"})
                raise _BadConversationFilter

        try:
            cost_min = _float("cost_min")
            cost_max = _float("cost_max")
            rebuild_min = _int("rebuild_min")
        except _BadConversationFilter:
            return None  # 400 already sent

        projects = [p for p in q.get("projects", []) if p] or None
        # Single comma-joined value -> split (the client may send either form).
        if projects and len(projects) == 1 and "," in projects[0]:
            projects = [s for s in projects[0].split(",") if s] or None

        date_from = _qs_str(q, "date_from", "") or None
        date_to = _qs_str(q, "date_to", "") or None
        if date_from or date_to:
            from importlib import import_module
            tz = _resolve_display_tz_obj(
                _apply_display_tz_override(
                    load_config(), type(self).display_tz_pref_override
                )
            ).key
            try:
                df, dtt = import_module(
                    "_lib_dashboard_dates"
                ).parse_filter_date_range(date_from, date_to, tz_name=tz)
            except ValueError as exc:
                self._respond_json(400, {"error": str(exc)})
                return None
        else:
            df = dtt = None

        return {
            "date_from": df,
            "date_to": dtt,
            "projects": projects,
            "cost_min": cost_min,
            "cost_max": cost_max,
            "rebuild_min": rebuild_min,
        }

    def _handle_get_conversations(self) -> None:
        """``GET /api/conversations`` — the browse rail (spec §3.1).

        Gated first (loopback / Host allowlist). ``sort``/``limit``/``offset``
        are read from the query string; the kernel clamps bounds. The browse
        filters (date/project/cost/rebuild — spec §2) are parsed/validated here
        (malformed → 400) and threaded into the kernel. Cache-open failures are
        500s, never 5xx-with-stacktrace.
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        q = _u.parse_qs(self.path.partition("?")[2])
        sort = _qs_str(q, "sort", "recent")
        limit = _qs_int(q, "limit", 50)
        offset = _qs_int(q, "offset", 0)
        filters = self._parse_conversation_filters(q)
        if filters is None:
            return  # a 400 has already been sent
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().list_conversations(
                conn, sort=sort, limit=limit, offset=offset, **filters),
            "/api/conversations")
        if not ok:
            return
        self._respond_json(200, body)

    def _handle_get_conversations_facets(self) -> None:
        """``GET /api/conversations/facets`` — distinct project labels + their
        conversation counts, for the browse filter's project multi-select (spec
        §2). Behind the SAME loopback/Host privacy gate as the list route; a
        cheap indexed GROUP BY over the rollup. The popover loads its options
        once from here (deriving from a paginated page would be incomplete).
        """
        if not self._require_transcripts_allowed():
            return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().list_conversation_facets(conn),
            "/api/conversations/facets")
        if not ok:
            return
        self._respond_json(200, body)

    def _handle_get_conversation_detail(self, path: str) -> None:
        """``GET /api/conversation/<session-id>`` — the reader (spec §3.2).

        The id is percent-decoded so clients that encode reserved chars
        round-trip. Unknown id → 404. ``after``/``before``/``tail``/``limit``
        page the items; ``after``/``before``/``tail`` are mutually exclusive
        (>1 supplied → 400). ``tail=1`` opens at the bottom; ``before=<id>``
        pages backward (#217 S2 / U4).
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        # ``path`` is already query-stripped by ``do_GET`` (``self.path.split("?")``),
        # so the cursor params (?after=/?before=/?tail=/?limit=) live ONLY on the
        # raw ``self.path``. Sibling handlers read ``self.path`` directly — the
        # detail route must too, or every request re-serves the head and
        # pagination is dead.
        query_str = self.path.partition("?")[2]
        session_id = _u.unquote(path[len("/api/conversation/"):])
        if not session_id:
            self.send_error(404, "conversation not found")
            return
        q = _u.parse_qs(query_str)
        after = _qs_str(q, "after", None)
        before = _qs_str(q, "before", None)
        tail = _qs_str(q, "tail", None) in ("1", "true", "yes")
        limit = _qs_int(q, "limit", 500)
        # Mutual-exclusion 400 (#217 S2 / U4). The kernel ALSO raises ValueError
        # on >1 cursor as its own invariant, but ``_run_conversation_query``
        # collapses every kernel exception to a 500, so the 400 must be decided
        # HERE, before the kernel call — this explicit pre-call check is the
        # authoritative backstop for the handler path.
        if sum(1 for x in (after is not None, before is not None, tail) if x) > 1:
            self.send_error(400, "after/before/tail are mutually exclusive")
            return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().get_conversation(
                conn, session_id, after=after, before=before, tail=tail,
                limit=limit),
            "/api/conversation")
        if not ok:
            return
        if body is None:
            self.send_error(404, "conversation not found")
            return
        self._respond_json(200, body)

    def _handle_get_conversation_events(self, path: str) -> None:
        """``GET /api/conversation/<id>/events`` — per-conversation live-tail
        SSE (spec §2). Fail-closed behind the same transcript privacy gate as
        the other conversation routes. Watches only this session's file(s);
        emits ``event: tail`` on growth, ``: keep-alive`` when idle. Passive
        (no ingest, no emit) under ``--no-sync``."""
        if not self._require_transcripts_allowed():
            return
        import time as _time
        import urllib.parse as _u
        watch = sys.modules["cctally"]._load_sibling("_lib_conversation_watch")
        cq = self._conversation_query()
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/events")])
        if not session_id:
            self.send_error(404, "conversation not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        passive = bool(type(self).no_sync)

        try:
            conn = open_cache_db()
        except (sqlite3.DatabaseError, OSError):
            # Cache unavailable — degrade to keep-alive only; client backstop
            # tick still surfaces turns. (Headers already sent; can't 500.)
            passive = True
            conn = None

        def _resolve():
            return cq.session_source_paths(conn, session_id) if conn else []

        def _ingest(changed):
            return sync_cache(conn, only_paths=set(changed))

        try:
            if passive:
                # Frozen-data contract: no ingest, no emit. Keep-alive only.
                while True:
                    _time.sleep(_LIVE_TAIL_KEEPALIVE)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()

            files = _resolve()
            # Best-effort connect ingest for immediacy, then baseline `seen`
            # from the cache's own offsets (session_files) so any pre-connect
            # growth the connect-ingest declined is still caught on cycle 1.
            try:
                if files:
                    sync_cache(conn, only_paths=set(files))
            except sqlite3.DatabaseError:
                pass
            seen = _cached_file_sigs(conn, files)

            idle = 0.0
            cycles = 0
            while True:
                _time.sleep(_LIVE_TAIL_POLL_INTERVAL)
                cycles += 1
                changed = watch.changed_paths(files, seen)
                if changed:
                    _time.sleep(_LIVE_TAIL_DEBOUNCE)
                    new_seen, emitted = watch.watch_step(
                        files, seen, ingest_fn=_ingest,
                        committed_sig_fn=lambda p: _cached_file_sigs(conn, [p]).get(p))
                    seen = new_seen
                    if emitted:
                        self.wfile.write(
                            ("event: tail\ndata: "
                             + json.dumps({"sessionId": session_id})
                             + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                        idle = 0.0
                        # §6 P2-H — a brand-new subagent file's FIRST content was
                        # just ingested by this emitting cycle, so the session's
                        # source-path set may have grown. Re-resolve it now (vs
                        # waiting up to _LIVE_TAIL_FILE_RESET_EVERY cycles) so the
                        # new thread (incl. a skill invoked inside it) live-tails
                        # promptly. A new path seeds seen=None (cur lacks a row),
                        # so changed_paths flags it next cycle → it ingests + emits.
                        # setdefault never disturbs an existing cursor.
                        new_files = _resolve()
                        if set(new_files) != set(files):
                            files = new_files
                            cur = _cached_file_sigs(conn, files)
                            for p in files:
                                seen.setdefault(p, cur.get(p))
                        continue
                idle += _LIVE_TAIL_POLL_INTERVAL
                if idle >= _LIVE_TAIL_KEEPALIVE:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    idle = 0.0
                if cycles % _LIVE_TAIL_FILE_RESET_EVERY == 0:
                    files = _resolve()
                    seen = {p: s for p, s in seen.items() if p in set(files)}
        except (BrokenPipeError, ConnectionResetError):
            pass            # client disconnect is normal
        finally:
            if conn is not None:
                conn.close()

    def _handle_get_conversation_search(self) -> None:
        """``GET /api/conversation/search?q=...&kind=...`` — cross-session
        FTS/LIKE search (spec §3.3). Matched BEFORE the ``<id>`` reader in
        ``do_GET``. ``kind`` (#177 S6) is validated to ``_CONV_SEARCH_KINDS``
        (else 400) before the kernel call.

        #217 S2 / Filtered-search: the browse filters (date/project/cost/rebuild)
        are parsed by the SAME ``_parse_conversation_filters`` the browse rail uses
        (malformed → 400 already sent) and threaded into the kernel, applied as a
        session-scope restriction across every kind. The 400s (bad kind, bad
        filter) are decided HERE, before the kernel call — ``_run_conversation_query``
        collapses kernel exceptions to a 500.
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        q = _u.parse_qs(self.path.partition("?")[2])
        query = _qs_str(q, "q", "")
        limit = _qs_int(q, "limit", 50)
        offset = _qs_int(q, "offset", 0)
        kind = self._parse_search_kind(q)
        if kind is None:
            return
        filters = self._parse_conversation_filters(q)
        if filters is None:
            return  # a 400 has already been sent
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().search_conversations(
                conn, query, limit=limit, offset=offset, kind=kind, **filters),
            "/api/conversation/search")
        if not ok:
            return
        self._respond_json(200, body)

    def _handle_get_conversation_payload(self, path: str) -> None:
        """``GET /api/conversation/<sid>/payload?tool_use_id=<id>&which=<result|input>``
        — the #178 on-demand load-full route. Re-reads the source JSONL line so
        a clipped result/input can be expanded without enlarging the cache.

        Gated FIRST by the same loopback/Host transcript privacy predicate the
        three other conversation routes use (fail-closed 403). ``locate_tool_payload``
        runs against cache.db (via the shared 500-envelope scaffold); the actual
        full body is re-read from disk by ``read_full_payload`` (no cache conn).
        ``which`` is validated to ``result``/``input`` (else 400); a missing
        tool_use_id is 400; an unknown id is 404; a gone/unparseable source line
        is 410 (the documented consequence of storing only capped text).
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        session_id = _u.unquote(
            path[len("/api/conversation/"):-len("/payload")])
        q = _u.parse_qs(self.path.partition("?")[2])
        tool_use_id = _qs_str(q, "tool_use_id", "")
        which = _qs_str(q, "which", "result")
        if not session_id or which not in ("result", "input") or not tool_use_id:
            self._respond_json(400, {"error": "bad request"})
            return
        cq = self._conversation_query()
        ok, loc = self._run_conversation_query(
            lambda conn: cq.locate_tool_payload(
                conn, session_id, tool_use_id, which),
            "/api/conversation/payload")
        if not ok:
            return
        if loc is None:
            self._respond_json(404, {"error": "not found"})
            return
        payload = cq.read_full_payload(loc[0], loc[1], tool_use_id, which)
        if payload is None:
            self._respond_json(410, {"error": "source no longer available"})
            return
        self._respond_json(200, payload)

    def _handle_get_conversation_outline(self, path: str) -> None:
        """``GET /api/conversation/<sid>/outline`` — full-session skeleton +
        session stats (#177 S5). Same fail-closed privacy gate; unknown id → 404.
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/outline")])
        if not session_id:
            self.send_error(404, "conversation not found")
            return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().get_conversation_outline(conn, session_id),
            "/api/conversation/outline")
        if not ok:
            return
        if body is None:
            self.send_error(404, "conversation not found")
            return
        self._respond_json(200, body)

    def _handle_get_conversation_prompts(self, path: str) -> None:
        """``GET /api/conversation/<sid>/prompts`` — ordered main-thread human
        prompts + full text (#217 S7 F10, the session-comparison spine). Same
        fail-closed transcript privacy gate as ``/outline`` —
        ``_require_transcripts_allowed()`` ONLY (no ``_check_origin_csrf``: the
        sibling transcript GETs gate on this predicate alone). Unknown id → 404.
        """
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/prompts")])
        if not session_id:
            self.send_error(404, "conversation not found")
            return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().get_conversation_prompts(conn, session_id),
            "/api/conversation/prompts")
        if not ok:
            return
        if body is None:
            self.send_error(404, "conversation not found")
            return
        self._respond_json(200, body)

    _CONV_EXPORT_SCOPES = ("all", "prompts", "chat", "recipe")

    def _handle_get_conversation_export(self, path: str) -> None:
        """``GET /api/conversation/<sid>/export?scope=<all|prompts|chat|recipe>``
        — whole-session Markdown (issue #217 S5 F1/F5).

        Same fail-closed transcript privacy gate as ``/outline`` / ``/payload``
        / ``/find`` — ``_require_transcripts_allowed()`` ONLY. **No
        ``_check_origin_csrf``** (Codex P0-1): the sibling transcript GETs gate
        on this predicate alone; ``_check_origin_csrf`` rejects a missing
        ``Origin`` and would make export STRICTER than its sibling reader routes.

        ``scope`` is validated HERE, BEFORE the kernel (the
        ``_run_conversation_query``-collapses-kernel-exceptions-to-500 gotcha —
        an invalid scope is a clean 400, never a 500). Unknown session → 404.
        Emits ``text/markdown; charset=utf-8`` (the client builds the download
        Blob/filename, so no ``Content-Disposition`` is needed)."""
        if not self._require_transcripts_allowed():
            return
        import urllib.parse as _u
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/export")])
        q = _u.parse_qs(self.path.partition("?")[2])
        scope = _qs_str(q, "scope", "all")
        if scope not in self._CONV_EXPORT_SCOPES:
            self._respond_json(400, {"error": f"unknown scope: {scope}"})
            return
        if not session_id:
            self.send_error(404, "conversation not found")
            return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().get_conversation_export(
                conn, session_id, scope),
            "/api/conversation/export")
        if not ok:
            return
        if body is None:
            self.send_error(404, "conversation not found")
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_get_conversation_find(self, path: str) -> None:
        """``GET /api/conversation/<sid>/find?q=...&kind=...`` — in-conversation
        find → document-ordered rendered-turn anchors (#177 S6). Same fail-closed
        privacy gate as the sibling routes; unknown id → 404; an invalid ``kind``
        → 400. Matched BEFORE the ``<id>`` reader catch-all in ``do_GET``.

        P1-1: validates against ``_CONV_FIND_KINDS`` (NOT the search set), so the
        cross-session-only ``kind=title``/``files`` return 400 here, never a 500.

        #217 S4 / I-1.2: ``regex``/``case`` are truthy params. An invalid regex
        is PRE-COMPILED here, BEFORE dispatching to the kernel — exactly as the
        detail route pre-validates ``after/before/tail`` — because
        ``_run_conversation_query`` collapses every kernel exception to a 500, so
        a ``re.error`` from the kernel's ``re.compile`` would otherwise leak as a
        500 instead of the actionable 400 the client maps to "invalid regex".
        """
        if not self._require_transcripts_allowed():
            return
        import re as _re
        import urllib.parse as _u
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/find")])
        if not session_id:
            self.send_error(404, "conversation not found")
            return
        q = _u.parse_qs(self.path.partition("?")[2])
        query = _qs_str(q, "q", "")
        kind = self._parse_search_kind(q, valid=_CONV_FIND_KINDS)
        if kind is None:
            return
        regex = _qs_str(q, "regex", None) in ("1", "true", "yes")
        case = _qs_str(q, "case", None) in ("1", "true", "yes")
        # Pre-validate the regex HERE (Codex P1): the kernel compiles the same
        # pattern, but its ``re.error`` would be swallowed into the generic 500
        # envelope below. Compiling first turns a bad pattern into a clean 400.
        if regex:
            try:
                _re.compile(query, 0 if case else _re.IGNORECASE)
            except _re.error as e:
                self._respond_json(400, {"error": f"invalid regex: {e}"})
                return
        ok, body = self._run_conversation_query(
            lambda conn: self._conversation_query().find_in_conversation(
                conn, session_id, query, kind=kind, regex=regex, case=case),
            "/api/conversation/find")
        if not ok:
            return
        if body is None:
            self.send_error(404, "conversation not found")
            return
        self._respond_json(200, body)

    _MEDIA_FETCH_SITE_ALLOWED = ("same-origin", "same-site", "none")

    def _handle_get_conversation_media(self, path: str) -> None:
        """``GET /api/conversation/<sid>/media?tool_use_id=<id>&index=N`` or
        ``?uuid=<uuid>&index=N`` (#177 S4) — serves decoded image/PDF bytes by
        re-reading the source JSONL line (the #178 mechanism). Nothing is ever
        written to cache.db or disk; no outbound requests.

        Gated FIRST by the transcript privacy predicate (fail-closed 403),
        then by Fetch-Metadata: unlike the JSON routes, images embed
        cross-origin (an <img src> on any website the user visits passes the
        Host/loopback gate and leaks existence + dimensions via
        onload/naturalWidth), so a PRESENT Sec-Fetch-Site header must be
        same-origin/same-site/none; an absent header (curl, older browsers)
        is allowed — defense-in-depth, not the primary gate (Codex F1).
        Exactly one addressing key (tool_use_id XOR uuid) + a non-negative
        integer index, else 400. Content-Type is the kernel's allowlist
        constant; images get CSP default-src 'none'; PDFs get inline
        Content-Disposition instead (a CSP sandbox would break native PDF
        viewers)."""
        if not self._require_transcripts_allowed():
            return
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs and sfs not in self._MEDIA_FETCH_SITE_ALLOWED:
            self._respond_json(403, {"error": "cross-site media fetch not allowed"})
            return
        import urllib.parse as _u
        session_id = _u.unquote(path[len("/api/conversation/"):-len("/media")])
        q = _u.parse_qs(self.path.partition("?")[2])
        tool_use_id = _qs_str(q, "tool_use_id", "")
        uuid = _qs_str(q, "uuid", "")
        index_raw = _qs_str(q, "index", "")
        if (not session_id or bool(tool_use_id) == bool(uuid)
                or not index_raw.isdigit()):
            self._respond_json(400, {"error": "bad request"})
            return
        index = int(index_raw)
        key = ({"tool_use_id": tool_use_id} if tool_use_id else {"uuid": uuid})
        cq = self._conversation_query()
        ok, loc = self._run_conversation_query(
            lambda conn: cq.locate_media(conn, session_id, index=index, **key),
            "/api/conversation/media")
        if not ok:
            return
        if loc is None:
            self._respond_json(404, {"error": "not found"})
            return
        # Defensive envelope parity with the sibling byte-serving handlers
        # (`_handle_get_doctor` / `_serve_static_file`): `locate_media` already
        # runs inside the `_run_conversation_query` 500-envelope, but the
        # `read_media_bytes` read + the byte emission did not. `read_media_bytes`
        # is internally defensive (OSError/ValueError → `gone`), so this guards
        # only an UNEXPECTED escape — but an unguarded one would kill the handler
        # thread with no logged 500. `response_started` tracks the commit point:
        # an exception BEFORE `send_response(200)` sends a clean logged 500; one
        # AFTER (mid-`wfile.write`, headers already out) can't re-send a status,
        # so it's logged only — never a silent thread death.
        response_started = False
        try:
            status, media_type, raw = cq.read_media_bytes(
                loc[0], loc[1], index=index, **key)
            if status == "unsupported":
                self._respond_json(404, {"error": "not found"})
                return
            if status == "too_large":
                self._respond_json(413, {"error": "media too large"})
                return
            if status != "ok":
                self._respond_json(410, {"error": "source no longer available"})
                return
            self.send_response(200)
            response_started = True
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=86400")
            if media_type == "application/pdf":
                self.send_header("Content-Disposition",
                                 f'inline; filename="attachment-{index}.pdf"')
            else:
                self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            self.wfile.write(raw)
        except Exception as exc:  # noqa: BLE001
            self.log_error("/api/conversation/media failed: %r", exc)
            if not response_started:
                self._respond_json(
                    500, {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_get_project_detail(self) -> None:
        """Return ProjectDetail JSON for ``GET /api/project/<key>?weeks=N``
        (spec §5.3 / §6.5).

        Opens the stats DB and ATTACHes cache.db so the shared builder
        sees both schemas off one conn (same contract the sync thread
        uses). Loopback bind + Origin parity is the entire auth
        surface — no CSRF needed for GETs.
        """
        try:
            conn = open_db()
        except Exception as exc:
            self.log_error("/api/project open_db failed: %r", exc)
            self.send_error(500, "project detail failed")
            return
        try:
            try:
                c = _cctally()
                conn.execute(
                    "ATTACH DATABASE ? AS cache_db",
                    (str(_cctally_core.CACHE_DB_PATH),),
                )
                conn.execute(
                    "CREATE TEMP VIEW IF NOT EXISTS session_entries AS "
                    "SELECT * FROM cache_db.session_entries"
                )
                conn.execute(
                    "CREATE TEMP VIEW IF NOT EXISTS session_files AS "
                    "SELECT * FROM cache_db.session_files"
                )
            except Exception as exc:
                # ATTACH/CREATE failure → fall back to the stats conn
                # alone; the builder will see no session_entries and
                # return None on key match. We still want to 500 here
                # because the dashboard contract is "should have both".
                self.log_error("/api/project ATTACH failed: %r", exc)
                self.send_error(500, "project detail failed")
                return
            _handle_get_project_detail_impl(self, conn=conn)
        finally:
            try:
                conn.execute("DROP VIEW IF EXISTS session_entries")
                conn.execute("DROP VIEW IF EXISTS session_files")
            except Exception:
                pass
            try:
                conn.execute("DETACH DATABASE cache_db")
            except Exception:
                pass
            conn.close()

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
            recorded_windows, block_start_overrides, canonical_intervals = (
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
                canonical_intervals=canonical_intervals,
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

        # Conversation viewer (Plan 2, spec §5): resolve the transcript gate
        # ONCE per SSE connection. `_transcripts_visible_to_request()` reads
        # this client's `Host` header + the class-level expose flag — both
        # constant for the connection's lifetime, so no per-tick FS/header
        # reads. The SSE `update` envelope MUST carry `transcriptsEnabled`:
        # the client replaces the whole snapshot on every tick, so without it
        # the steady-state UI loses the gate (the ViewSwitcher disappears
        # ~15s after bootstrap). Mirrors `/api/data`'s per-request injection.
        transcripts_enabled = self._transcripts_visible_to_request()

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
                    # #264 S3: gate the in-envelope session `title` on the same
                    # connection-scoped predicate that drives transcriptsEnabled.
                    transcripts_visible=transcripts_enabled,
                )
                env["transcriptsEnabled"] = transcripts_enabled
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

def _dashboard_wait_for_signal(
    signals,
    *,
    on_signal=None,
    timeout=None,
):
    """Block the calling (main) thread until one of ``signals`` is delivered.

    The wait is driven by a self-pipe wakeup fd (``signal.set_wakeup_fd``)
    rather than a ``threading.Event``: CPython's C-level signal trampoline
    writes the signum to the pipe on EVERY delivery, *before* (and
    independent of) the Python-level handler running, so the ``select``
    below unblocks on the very first signal. This eliminates the lost-wakeup
    that races ``threading.Event.wait()`` — a single SIGTERM that arrives as
    the main thread enters the wait is dropped ~0.04-0.07% of the time,
    never waking an Event-based loop, and recovery needs a *second* signal
    (issue #154; surfaced during #153 triage). A timed-poll
    (``while not stop.wait(0.5)``) does NOT fix it: on the miss the flag is
    never set, so it polls forever — the pipe buffer is the fix, not the
    timeout.

    ``on_signal`` (optional) is invoked from the Python-level handler on each
    delivery — a belt-and-suspenders secondary signal for callers that still
    want one; the wakeup does NOT depend on it running. ``timeout`` is
    ``None`` (block forever) in production; tests pass a finite bound so a
    regressed mechanism fails loudly instead of hanging.

    Returns ``True`` if woken by a signal, ``False`` if ``timeout`` elapsed.
    MUST be called from the main thread (``set_wakeup_fd`` / ``signal.signal``
    both require it). Restores the prior signal dispositions and wakeup fd on
    return, so it is safe to call repeatedly and inside a test process.
    """
    import os
    import selectors
    import signal

    prev_handlers = {sig: signal.getsignal(sig) for sig in signals}
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    # Arm the wakeup fd BEFORE installing the Python handlers: by the time a
    # handler can fire, the C trampoline already has a pipe to write to, so a
    # signal racing the setup can't slip through a gap and be lost. (A signal
    # before this point still hits the prior disposition — pre-existing, not
    # our shutdown wait.)
    prev_wakeup_fd = signal.set_wakeup_fd(write_fd)

    def _handler(signum, frame):
        if on_signal is not None:
            on_signal()

    sel = selectors.DefaultSelector()
    sel.register(read_fd, selectors.EVENT_READ)
    try:
        for sig in signals:
            signal.signal(sig, _handler)
        woke = bool(sel.select(timeout))
        if woke:
            # Drain the wakeup byte(s); the pipe is non-blocking so an empty
            # read raises BlockingIOError rather than blocking.
            try:
                os.read(read_fd, 4096)
            except BlockingIOError:
                pass
        return woke
    finally:
        # Order matters: restore the wakeup fd before closing write_fd so the
        # signal machinery never points at a closed descriptor.
        signal.set_wakeup_fd(prev_wakeup_fd)
        for sig, handler in prev_handlers.items():
            signal.signal(sig, handler)
        sel.close()
        os.close(read_fd)
        os.close(write_fd)


def _dashboard_initial_snapshot(args, *, pinned_now, display_tz_pref_override):
    """#179: build the dashboard's first snapshot WITHOUT the heavy sync so the
    HTTP port binds promptly. The background ``_DashboardSyncThread`` (started in
    cmd_dashboard before the ThreadingHTTPServer bind) runs the first full
    ``sync_cache`` — including any pending conversation reingest — and SSE-pushes
    the populated snapshot on completion. ``--no-sync`` already passed
    ``skip_sync=True`` here, so that path is byte-identical; only the normal launch
    changes (its previously-redundant foreground sync is removed, not duplicated —
    the background thread's first tick was always going to sync anyway)."""
    return sys.modules["cctally"]._tui_build_snapshot(
        now_utc=pinned_now, skip_sync=True,
        display_tz_pref_override=display_tz_pref_override,
        # #268 M4: precompute doctor / config / update-state on the initial
        # snapshot too, so the envelope is pure from the very first paint AND
        # under --no-sync (where no later sync tick would set them). One
        # `security` fork here is negligible next to the heavy sync #179 moved
        # to the background thread. ``getattr`` so minimal test ``args``
        # namespaces (no ``host``) still build an initial snapshot.
        precompute_envelope=True, runtime_bind=getattr(args, "host", None),
    )


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the live web dashboard."""
    import signal as _signal
    import threading
    import time as _time
    import webbrowser as _wb

    # #268 M6.1: arm the SIGUSR1 all-thread traceback dump so any future spin is
    # self-diagnosing (`kill -USR1 <pid>`) without root py-spy. No-op where
    # SIGUSR1 is unavailable (Windows).
    _register_faulthandler_sigusr1()

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

    # Load config for the bind-host + expose-transcripts resolution below. (#217
    # S1 / U7b: dropped the dead ``args._resolved_tz = resolve_display_tz(...)``
    # write — nothing in the dashboard ever read it back; the envelope display
    # block it was speculatively staged for was never wired. Each report
    # subcommand resolves + reads its own ``args._resolved_tz`` independently.)
    config = load_config()

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

    # #179: build the initial snapshot WITHOUT the heavy sync so the HTTP port
    # binds promptly even on a heavy-history instance. The background
    # _DashboardSyncThread (started below, before the bind) owns the first full
    # sync_cache — including any pending conversation-enrichment reingest — and
    # SSE-pushes the populated snapshot on completion.
    # Self-heal removed-worktree orphans BEFORE building the first snapshot so
    # the initial render already excludes stale sessions from a deleted
    # worktree (rather than showing them until the first periodic tick).
    # Gated off under --no-sync (a frozen dashboard mutates nothing).
    _heal = _dashboard_self_heal_orphans(skip_sync=bool(args.no_sync))
    if _heal is not None and _heal.pruned_files:
        print(f"dashboard: pruned {_heal.pruned_files} orphaned cache file(s) "
              f"from removed sessions on startup", flush=True)

    initial = _dashboard_initial_snapshot(
        args, pinned_now=pinned_now,
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
        # #268 M4: the dashboard's bound host, threaded into the sync-thread
        # doctor precompute so `safety.dashboard_bind` reflects the running
        # bind and the envelope reads the precomputed doctor block.
        runtime_bind=args.host,
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
    # Conversation viewer (Plan 2, spec §5): the resolved
    # `dashboard.expose_transcripts` opt-in. Read off the already-loaded
    # `config` the same way `dashboard.bind` is resolved above (the
    # `_config_known_value` shim surfaces the boolean default of False for
    # an absent or hand-edited-junk value).
    DashboardHTTPHandler.cctally_expose_transcripts = bool(
        _config_known_value(config, "dashboard.expose_transcripts")
    )
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
            last_heal = _time.monotonic()
            while not self._stop.is_set():
                _run_sync_now(skip_sync=self._skip_sync)
                # Self-heal removed-worktree orphans on a ~60s cadence (far
                # rarer than the sync tick — a deleted worktree is not urgent).
                # Non-blocking on the flock, so a contended tick just retries
                # next cadence; gated off under --no-sync.
                if (not self._skip_sync
                        and _time.monotonic() - last_heal >= 60.0):
                    last_heal = _time.monotonic()
                    _dashboard_self_heal_orphans(skip_sync=self._skip_sync)
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
    # `_QuietThreadingHTTPServer` folds in `daemon_threads = True` and silences
    # client-disconnect tracebacks (spec §5).
    resolved_port = _resolve_dashboard_port(args.port)
    srv = _QuietThreadingHTTPServer((args.host, resolved_port), DashboardHTTPHandler)

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

    # Block the main thread until SIGINT/SIGTERM. The wait is driven by a
    # self-pipe wakeup fd (signal.set_wakeup_fd) rather than a
    # threading.Event, so a single signal unblocks it unconditionally — the
    # C-level signal trampoline writes to the pipe before (and independent
    # of) any Python-level handler running, so the wakeup can't be lost to
    # the Event.wait() entry race (#154). timeout=None → block forever.
    try:
        _dashboard_wait_for_signal((_signal.SIGINT, _signal.SIGTERM))
    finally:
        if sync_thread is not None:
            sync_thread.stop()
        update_check_stop.set()
        srv.shutdown()
        http_thread.join(timeout=2)
        print("dashboard: stopped", flush=True)
    return 0

