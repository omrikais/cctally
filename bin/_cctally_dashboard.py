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
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, socket.timeout)):
            # Client hung up mid-response (or stalled past the handler timeout);
            # benign on a local dashboard. #279 S1 F3 adds socket.timeout.
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
# #279 S2 F3: stdlib-logging chokepoint — server errors reach stderr via
# the real log_error override below (leaf import, no cycle).
import _lib_log
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
    reset_cache_report_state,
    reset_group_a_state,
    reset_projects_env_state,
    reset_session_cache_state,
    reset_weekref_cost_state,
    BugKSegment,
    _bugk_key,
    _max_id as _snapshot_max_id,
    _reset_sig as _snapshot_reset_sig,
)


# === #279 S5: consumer-only dashboard siblings ============================
# The share / envelope / cache-report / conversation seams live in four
# ``_cctally_dashboard_*`` siblings (spec §2). They are consumer-only —
# only THIS module imports them at body-time — so, like the S4 ``_lib_*``
# kernels, they were deliberately kept OUT of ``bin/cctally``'s eager
# ``_load_sibling`` block to keep ``bin/cctally`` byte-untouched (re-export
# continuity). Under the ``SourceFileLoader`` harness path (``bin/`` absent
# from ``sys.path``) a bare ``from _cctally_dashboard_X import`` would miss,
# so this pre-registers each sibling ``__file__``-relative first (mirrors
# ``bin/_cctally_record.py``'s ``_ensure_sibling_loaded``). The honest
# ``from _cctally_dashboard_X import (...)`` that follows re-binds every
# moved symbol as an attribute of THIS module, so ``bin/cctally``'s
# re-exports and the direct ``sys.modules["_cctally_dashboard"].X`` reaches
# (TUI, reconcile harness, pytest) keep resolving unchanged.
import importlib.util as _ilu


def _ensure_sibling_loaded(name: str) -> None:
    """Register a NON-eager-loaded ``_cctally_dashboard_*`` sibling in ``sys.modules``.

    The four S5 siblings — ``_cctally_dashboard_share``,
    ``_cctally_dashboard_envelope``, ``_cctally_dashboard_cache_report``,
    ``_cctally_dashboard_conversation`` — are consumer-only and NOT in
    ``bin/cctally``'s eager-load block, so a plain ``from … import`` would
    miss under the ``SourceFileLoader`` harness path (``bin/`` off
    ``sys.path``). Pre-register the sibling ``__file__``-relative first
    (mirrors ``bin/_cctally_record.py:_ensure_sibling_loaded`` and
    ``_cctally_cache._load_lib``); the honest ``from … import`` that
    follows is then a ``sys.modules`` hit in every load context (prod
    script, conftest, harness).
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


_ensure_sibling_loaded("_cctally_dashboard_cache_report")
from _cctally_dashboard_cache_report import (
    CACHE_REPORT_WINDOW_DAYS,
    CACHE_REPORT_ANOMALY_WINDOW_DAYS,
    CacheReportDailyRow,
    CacheReportBreakdownRow,
    CacheReportTodaySpotlight,
    _cache_report_snapshot_to_dict,
    CacheReportSnapshot,
    _cache_report_load_kernel,
    _cache_report_needed_closed_dates,
    _day_start,
    _cache_report_empty,
    build_cache_report_snapshot,
    _CacheReportSettings,
    _CacheReportConfigError,
    _CACHE_REPORT_ALLOWED_KEYS,
    _validate_cache_report_settings,
)

_ensure_sibling_loaded("_cctally_dashboard_envelope")
from _cctally_dashboard_envelope import (
    snapshot_to_envelope,
    _session_detail_to_envelope,
    _iso_z,
    _compute_intensity_buckets,
    _select_current_block_for_envelope,
    _envelope_rows_weekly,
    _envelope_rows_five_hour,
    _envelope_rows_budget_family,
    _envelope_rows_projected,
    _envelope_rows_project_budget,
    _ENVELOPE_AXIS_MAPPERS,
    _build_alerts_envelope_array,
    _model_breakdowns_to_models,
)

_ensure_sibling_loaded("_cctally_dashboard_share")
from _cctally_dashboard_share import (
    # accessor shims (still forward late-binding to sys.modules["cctally"])
    _share_load_lib,
    _share_now_utc,
    _share_now_utc_iso,
    _share_history_recipe_id,
    _share_iso,
    # constants
    _SHARE_POST_MAX_BYTES,
    _SHARE_PANELS_PERIOD_FIXED,
    _SHARE_PANELS_PERIOD_OVERRIDABLE,
    _SHARE_TOP_PROJECTS_BUILDER_CAP,
    # share-period pipeline + per-panel builders
    _share_resolve_period,
    _share_custom_window_n,
    _share_previous_period_delta,
    _share_apply_period_override,
    _share_apply_content_toggles,
    _share_top_projects_for_range,
    _share_all_projects_for_range,
    _share_per_day_per_project_for_range,
    _share_per_block_per_project,
    _build_share_panel_data,
    _share_empty_week_stub,
    _build_weekly_share_panel_data,
    _build_current_week_share_panel_data,
    _build_trend_share_panel_data,
    _build_daily_share_panel_data,
    _build_monthly_share_panel_data,
    _build_forecast_share_panel_data,
    _build_blocks_share_panel_data,
    _build_sessions_share_panel_data,
    _build_projects_share_panel_data,
    # handler impls (the ten thin class delegators call these)
    _share_load_templates_module_impl,
    _handle_share_templates_get_impl,
    _handle_share_render_post_impl,
    _handle_share_compose_post_impl,
    _handle_share_presets_get_impl,
    _handle_share_presets_post_impl,
    _handle_share_presets_delete_impl,
    _handle_share_history_get_impl,
    _handle_share_history_post_impl,
    _handle_share_history_delete_impl,
)

_ensure_sibling_loaded("_cctally_dashboard_conversation")
from _cctally_dashboard_conversation import (
    # constants / helpers
    _CONV_SEARCH_KINDS,
    _CONV_FIND_KINDS,
    _BadConversationFilter,
    _cached_file_sigs,
    # query plumbing + eleven handler impls (the class delegators call these)
    _conversation_query_impl,
    _parse_search_kind_impl,
    _run_conversation_query_impl,
    _parse_conversation_filters_impl,
    _handle_get_conversations_impl,
    _handle_get_conversations_facets_impl,
    _handle_get_conversation_detail_impl,
    _handle_get_conversation_events_impl,
    _handle_get_conversation_search_impl,
    _handle_get_conversation_payload_impl,
    _handle_get_conversation_outline_impl,
    _handle_get_conversation_prompts_impl,
    _handle_get_conversation_export_impl,
    _handle_get_conversation_find_impl,
    _handle_get_conversation_media_impl,
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
            # can't catch.
            reset_weekref_cost_state()
            # #272: the per-day cache-report cache rides the same prune-site
            # clear — a non-max deletion inside a closed day the reconcile's
            # max-id / mutation-seq regression check can't catch.
            reset_cache_report_state()
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
            # `(max_id, max_wus_id, entry_mutation_seq, cw_key, weeks_back)`, which
            # carries NO generation counter. A prune that deletes only NON-max
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


def _config_known_value(*args, **kwargs):
    return sys.modules["cctally"]._config_known_value(*args, **kwargs)


def config_writer_lock(*args, **kwargs):
    return sys.modules["cctally"].config_writer_lock(*args, **kwargs)


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
            # Latest-wins coalescing (#278 §2.6): every published snapshot is a
            # COMPLETE state replacement, so a client only ever needs the
            # newest. On a full queue drop the STALE queued frame and enqueue
            # the newest, so a slow subscriber (e.g. one filled by A2's rapid
            # partial republishes) still converges to the final hydrating=false
            # frame instead of dropping it — and a lagging client jumps to the
            # current state rather than replaying stale frames.
            #
            # Held under the hub lock so concurrent producers (the sync tick +
            # the update-check thread) can't interleave a get/put on the same
            # queue. All ops are non-blocking (put_nowait / get_nowait), so
            # holding the lock never back-pressures the producer, and the SSE
            # consumer only ever get()s (never puts), so after we make room the
            # re-put cannot lose to it.
            for q in self._queues:
                try:
                    q.put_nowait(snapshot)
                except _queue.Full:
                    try:
                        q.get_nowait()  # discard the oldest, stale frame
                    except _queue.Empty:
                        pass
                    try:
                        q.put_nowait(snapshot)
                    except _queue.Full:
                        # Defensive: a consumer racing between our get and put
                        # could only have removed items, so this is unreachable
                        # under the lock — but never raise out of publish().
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

        def _delta_fetch(label, after_seq, after_ts):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(
                cache_conn, lo, hi, after_seq=after_seq, after_ts=after_ts)

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

        def _delta_fetch(label, after_seq, after_ts):
            s, hi = _current_window(label)
            return [] if hi <= s else iter_entries_with_id(
                cache_conn, s, hi, after_seq=after_seq, after_ts=after_ts)

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

        def _delta_fetch(label, after_seq, after_ts):
            lo, hi = _current_window(label)
            return [] if hi <= lo else iter_entries_with_id(
                cache_conn, lo, hi, after_seq=after_seq, after_ts=after_ts)

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
                                   after_seq: "int | None" = None):
    """Read ``session_entries`` joined with ``session_files`` over
    [since, until]. Yields rows directly off the passed conn — no
    cache.db monkeypatch, no production ``get_claude_session_entries``
    pipeline. The fixture DBs co-locate both schemas in one file; the
    production wiring opens both DBs and ATTACHes cache.db as a schema
    on the stats conn (see ``_run_dashboard_sync_tick``).

    ``after_seq`` (#270 §8, re-key of Codex-P2b — #271 §20): when set, the
    current-week accumulator's warm delta seeks by the **mutation_seq range**
    (``WHERE e.mutation_seq > ?``) with the ``[since, until]`` timestamp bounds
    applied as a residual filter. Two hints keep the planner on the
    ``idx_entries_mutation_seq`` seek (``SEARCH e USING INDEX
    idx_entries_mutation_seq (mutation_seq>?)``) instead of a full ``SCAN`` /
    ``idx_entries_timestamp`` window scan (which would re-read the whole ~12K-entry
    current week and let the ~63ms floor creep back): (1) the unary-plus no-op
    ``+e.timestamp_utc`` deprioritizes ``idx_entries_timestamp`` (a TRUE no-op —
    it preserves the TEXT value AND the string comparison, unlike ``+ 0``); and
    (2) ``ORDER BY e.mutation_seq`` matches the index's leading column so the
    planner satisfies the order from the seek itself. ``INDEXED BY`` is NOT usable
    here — production runs this against a TEMP VIEW (``session_entries`` over the
    ATTACHed ``cache_db.session_entries``), and a view has no indexes; the hint
    pair drives the same seek on both the view and a base-table fixture. Unlike
    the old ``e.id > ?`` leg (a free INTEGER PRIMARY KEY rowid range), a bare
    ``e.mutation_seq > ?`` lets the planner fall back to a scan on a small table,
    so the hints are load-bearing. Re-keying from ``e.id`` to ``e.mutation_seq``
    catches an id-stable in-place finalization (it advances the seq while
    ``MAX(id)`` stays flat, so the pre-#270 ``e.id > ?`` leg missed it); on a
    pure-insert tick ``{mutation_seq > ?}`` == ``{id > ?}`` (seq monotone with
    id), so the delta row SET is byte-identical. The caller (``_fetch_delta_rows``)
    re-sorts the small result by ``(timestamp_utc, id)`` to reproduce the full
    pass's fold order, so the ``mutation_seq``-ASC SQL order is irrelevant
    downstream. An ``EXPLAIN QUERY PLAN`` regression asserts the mutation_seq
    index seek (``tests/test_projects_envelope.py``).
    """
    since_iso = since.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    until_iso = until.astimezone(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if after_seq is not None:
        cur = conn.execute(
            "SELECT e.id, e.timestamp_utc, e.model, e.input_tokens, "
            "       e.output_tokens, e.cache_create_tokens, e.cache_read_tokens, "
            "       e.cost_usd_raw, e.source_path, "
            "       sf.session_id, sf.project_path "
            "FROM session_entries e "
            "LEFT JOIN session_files sf ON sf.path = e.source_path "
            "WHERE e.mutation_seq > ? AND +e.timestamp_utc >= ? AND +e.timestamp_utc <= ? "
            "ORDER BY e.mutation_seq ASC",
            (after_seq, since_iso, until_iso),
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
    cur_max_seq: int,
) -> "tuple[dict, dict, dict]":
    """Flag-ON assembly (#269 §14 Win 2): recompute only the CURRENT week each
    warm tick; serve CLOSED weeks from the per-(project, week) cache on a hit and
    RECOMPUTE-AND-POPULATE on a miss (cold / Monday rollover / window slide,
    Codex-M4 P2). Returns ``(buckets, total_cost_by_week, key_by_bucket)`` in the
    exact shape the from-scratch walk produces, so the downstream disambiguation
    / attribution / trend assembly runs unchanged and byte-identically.

    #271 M4 (spec §20): the CURRENT week is no longer re-folded from scratch each
    warm tick — it goes through the single-slot ``accumulate_projects_current_week``
    accumulator, which folds only the changed-row delta (or finalizes the cached
    running ``mut`` unchanged on an empty-delta tick), byte-identically.
    ``cur_max_id`` is the tick's ``MAX(session_entries.id)`` (the regression
    backstop + pre-existing-row cold-refold trigger); ``cur_max_seq`` is the
    tick's ``MAX(mutation_seq)`` (the #270 §8 delta watermark, so an id-stable
    in-place finalization is caught). The accumulator is single-writer here (this
    runs only on the sync-thread ``use_projects_env_cache=True`` path).
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

    def _fetch_delta_rows(after_seq):
        # Index-seek the changed-row delta (mutation_seq > after_seq, #270 §8)
        # over the current-week window, then PRE-FILTER to genuine current-week
        # non-synthetic rows (mirrors _fold_projects_entry's membership filter) so
        # the accumulator's fold-order gate compares a REAL current-week entry as
        # rows[0]. Sort by (ts_iso, id) to reproduce the full pass's fold order
        # exactly (SQLite BINARY collation on the Z-normalized ISO string ==
        # Python str compare).
        out = []
        for r in _projects_iter_session_entries(
            conn, since=cw_start, until=cw_end, after_seq=after_seq,
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
                cur_max_seq=cur_max_seq,
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
    ``(max(session_entries.id), max_wus_id, max(mutation_seq), cw_week_start,
    weeks_back)`` — the ``mutation_seq`` leg (#270 §7d) busts the memo on an
    id-stable in-place finalization.
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
    # #270 (§7d, Codex-2b): this OUTER whole-envelope memo returns BEFORE the
    # inner per-(project, week) reconcile cache, so an id-stable in-place
    # finalization — which leaves MAX(id)/MAX(wus id) flat — would serve the
    # stale envelope regardless of the inner seq fix. Fold the mutation signal
    # into the memo key so it busts on any changed row. `MAX(mutation_seq)` is
    # read off the SAME `session_entries` surface as `max_id` (an id-stable
    # UPSERT advances it), index-backed by `idx_entries_mutation_seq`, and
    # degrades to 0 where the column is absent (old fixtures) — byte-identical.
    # Deliberately `MAX(mutation_seq)`, NOT the `cache_meta` counter the reconciles
    # read via `_entry_mutation_seq` (this conn is a project-detail view without a
    # `cache_meta` surface). The two are consistent — the counter is always
    # >= MAX(mutation_seq), and no row exists strictly between them at read time —
    # so do not "unify" them here (the divergence is intentional; see #270 §M3-1).
    try:
        cur = conn.execute(
            "SELECT COALESCE(MAX(mutation_seq), 0) FROM session_entries"
        )
        entry_mutation_seq = cur.fetchone()[0]
    except sqlite3.OperationalError:
        entry_mutation_seq = 0
    cw_key: "dt.datetime | None" = None
    if current_week is not None:
        cw_key = getattr(current_week, "week_start_at", None)
    memo_key = (max_id, max_wus_id, entry_mutation_seq, cw_key, weeks_back)
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
            cur_max_id=max_id, cur_max_seq=entry_mutation_seq,
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


# ── /api/debug/backend on-demand cache-state helpers (issue #276, Session A) ──
# All read-only, cheap, and privacy-safe: they leak ONLY row counts, signature
# legs (ints/tuples), pending-flag names, and the tool version — never prompt /
# prose / paths. Computed on demand so cache_state is available even with
# tracing off (the phase tree, by contrast, is present only when traced).

# Safe cache-table names surfaced as `dataset` row counts (no content read).
_DEBUG_CACHE_TABLES = (
    "session_entries",
    "session_files",
    "conversation_messages",
    "conversation_sessions",
    "conversation_ai_titles",
    "conversation_file_touches",
    "codex_session_entries",
    "codex_session_files",
)


def _debug_cache_table_counts(cache_conn) -> dict:
    """Row counts per known cache table. Absent tables (partially-migrated /
    fresh cache) are omitted rather than erroring."""
    counts: dict = {}
    for table in _DEBUG_CACHE_TABLES:
        try:
            row = cache_conn.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 — fixed allowlist
            ).fetchone()
            counts[table] = int(row[0])
        except sqlite3.Error:
            pass
    return counts


def _debug_cache_state(cache_conn) -> dict:
    """On-demand signature legs + pending-reingest flags + generation.

    The signature legs are the canonical ``compute_signature`` fields (ints /
    a small tuple). stats.db is opened READ-ONLY via a dispatcher-free URI
    connection so this diagnostic never forward-migrates or creates a DB; if it
    is absent the stats legs degrade (each ``_max_id`` / ``_reset_sig`` already
    returns 0 on a missing table)."""
    c = _cctally()
    sc = c._load_sibling("_lib_snapshot_cache")
    state: dict = {"generation": sc.current_generation()}
    stats_conn = None
    try:
        stats_conn = sqlite3.connect(
            f"{_cctally_core.DB_PATH.as_uri()}?mode=ro", uri=True
        )
    except sqlite3.Error:
        stats_conn = None
    try:
        if stats_conn is not None:
            sig = sc.compute_signature(
                cache_conn, stats_conn, generation=sc.current_generation()
            )
            state["signature"] = {
                "max_entry_id": sig.max_entry_id,
                "max_wus_id": sig.max_wus_id,
                "max_wcs_id": sig.max_wcs_id,
                "reset_sig": list(sig.reset_sig),
                "max_codex_id": sig.max_codex_id,
                "entry_mutation_seq": sig.entry_mutation_seq,
            }
        else:
            state["signature"] = {
                "max_entry_id": sc._max_id(cache_conn, "session_entries"),
                "entry_mutation_seq": sc._entry_mutation_seq(cache_conn),
            }
    except sqlite3.Error as exc:
        state["signature"] = {"_error": f"{type(exc).__name__}: {exc}"}
    finally:
        if stats_conn is not None:
            stats_conn.close()
    # Pending reingest / backfill flag NAMES (never values) set in cache_meta.
    try:
        rows = cache_conn.execute(
            "SELECT key FROM cache_meta "
            "WHERE key LIKE '%_pending' AND value IS NOT NULL"
        ).fetchall()
        state["pending_reingest"] = sorted(r[0] for r in rows)
    except sqlite3.Error:
        state["pending_reingest"] = []
    # Walk-complete sentinel presence (bool only — no timestamp leak).
    try:
        state["walk_complete"] = cache_conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
        ).fetchone() is not None
    except sqlite3.Error:
        state["walk_complete"] = False
    return state


def _debug_tool_version() -> str:
    """The running tool version (latest stamped CHANGELOG header), or
    ``"unknown"``. Safe to expose (already on the public update surface)."""
    try:
        v = _cctally()._load_sibling(
            "_lib_changelog"
        )._read_latest_changelog_version()
        return v[0] if v else "unknown"
    except Exception:  # noqa: BLE001 — diagnostic must never raise
        return "unknown"


# === Table-driven route dispatch (#279 S5 F5, spec §7) =====================
# Ordered, first-match-wins tables — evaluated top-to-bottom so semantics are
# if/elif-identical to the pre-S5 chains. Each entry is
#   (kind, pattern, handler_method_name, perf, wants_path)
# where:
#   kind        ∈ {"exact", "prefix", "prefix+suffix"} ("prefix+suffix" pattern
#               is a (prefix, suffix) pair, matched startswith AND endswith).
#   perf        None | ("scope", label) | ("phase", label) — the 11 per-route
#               conversation instrumentation wraps (8 _perf_scope + 3
#               _perf_gate().phase); their clean-exit stash_last feeds
#               /api/debug/backend's #276 trace, so this column is load-bearing
#               even though goldens run trace-off (gate P2-1).
#   wants_path  True → the handler is called with the query-stripped path
#               (the 11 path-taking handlers); False → called with no args.
# ``_dispatch`` resolves getattr(self, name) at request time (a monkeypatched
# handler method still intercepts). CSRF / privacy gates stay INSIDE the
# handlers — the table carries no security semantics. Order copies the former
# do_GET/do_POST/do_DELETE chains verbatim: the exact /api/conversations +
# /api/conversation/search entries and the seven /api/conversation/<id>/<suffix>
# prefix+suffix entries ALL precede the bare /api/conversation/ catch-all.
_GET_ROUTES = (
    ("exact", "/api/data", "_serve_api_data", None, False),
    ("exact", "/api/events", "_serve_api_events", None, False),
    ("prefix", "/api/session/", "_handle_get_session_detail", None, True),
    ("prefix", "/api/project/", "_handle_get_project_detail", None, False),
    ("prefix", "/api/block/", "_handle_get_block_detail", None, True),
    ("exact", "/api/update/status", "_handle_get_update_status", None, False),
    ("prefix", "/api/update/stream/", "_handle_get_update_stream", None, True),
    ("exact", "/api/share/templates", "_handle_share_templates_get", None, False),
    ("exact", "/api/share/presets", "_handle_share_presets_get", None, False),
    ("exact", "/api/share/history", "_handle_share_history_get", None, False),
    ("exact", "/api/doctor", "_handle_get_doctor", None, False),
    ("exact", "/api/debug/backend", "_handle_get_debug_backend", None, False),
    ("exact", "/api/conversations/facets", "_handle_get_conversations_facets",
     ("scope", "endpoint.conversations_facets"), False),
    ("exact", "/api/conversations", "_handle_get_conversations",
     ("scope", "endpoint.conversations"), False),
    ("exact", "/api/conversation/search", "_handle_get_conversation_search",
     ("scope", "endpoint.conversation_search"), False),
    ("prefix+suffix", ("/api/conversation/", "/payload"),
     "_handle_get_conversation_payload",
     ("phase", "endpoint.conversation_payload"), True),
    ("prefix+suffix", ("/api/conversation/", "/media"),
     "_handle_get_conversation_media",
     ("phase", "endpoint.conversation_media"), True),
    ("prefix+suffix", ("/api/conversation/", "/outline"),
     "_handle_get_conversation_outline",
     ("scope", "endpoint.conversation_outline"), True),
    ("prefix+suffix", ("/api/conversation/", "/find"),
     "_handle_get_conversation_find",
     ("scope", "endpoint.conversation_find"), True),
    ("prefix+suffix", ("/api/conversation/", "/events"),
     "_handle_get_conversation_events",
     ("phase", "endpoint.conversation_events"), True),
    ("prefix+suffix", ("/api/conversation/", "/export"),
     "_handle_get_conversation_export",
     ("scope", "endpoint.conversation_export"), True),
    ("prefix+suffix", ("/api/conversation/", "/prompts"),
     "_handle_get_conversation_prompts",
     ("scope", "endpoint.conversation_prompts"), True),
    ("prefix", "/api/conversation/", "_handle_get_conversation_detail",
     ("scope", "endpoint.conversation_detail"), True),
)

_POST_ROUTES = (
    ("exact", "/api/sync", "_handle_post_sync", None, False),
    ("exact", "/api/settings", "_handle_post_settings", None, False),
    ("exact", "/api/alerts/test", "_handle_post_alerts_test", None, False),
    ("exact", "/api/update", "_handle_post_update", None, False),
    ("exact", "/api/update/dismiss", "_handle_post_update_dismiss", None, False),
    ("exact", "/api/share/render", "_handle_share_render_post", None, False),
    ("exact", "/api/share/compose", "_handle_share_compose_post", None, False),
    ("exact", "/api/share/presets", "_handle_share_presets_post", None, False),
    ("exact", "/api/share/history", "_handle_share_history_post", None, False),
)

_DELETE_ROUTES = (
    ("prefix", "/api/share/presets/", "_handle_share_presets_delete", None, False),
    ("exact", "/api/share/history", "_handle_share_history_delete", None, False),
)


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

    # #279 S1 F3: bound request-parse reads and stalled sends. SSE streams
    # write keep-alives every <=15s, so a 60s stalled send == dead client;
    # http.server treats a socket timeout as close_connection. Slow-loris guard.
    timeout = 60

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

    # Access log stays silent (deliberate — noisy in the parent terminal),
    # but server errors are REAL as of #279 S2: log_error routes through
    # the _lib_log chokepoint. stdlib send_error() calls
    # log_error("code %d, message %s", code, message) for EVERY error
    # response — routine 400/403/404 rejections included — so the
    # stdlib-generated form is filtered: <500 drops, >=500 logs (covers
    # send_error(500) sites with no explicit log_error call).
    # Handler-authored log_error calls always log.
    def log_message(self, fmt: str, *args) -> None:
        pass

    def log_error(self, fmt: str, *args) -> None:
        if fmt.startswith("code ") and args and isinstance(args[0], int) \
                and args[0] < 500:
            return
        _lib_log.get_logger("dashboard").error(
            "%s - %s", self.address_string(),
            (fmt % args) if args else fmt,
        )

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

    def _dispatch(self, table) -> bool:
        """First-match-wins dispatch over a module-level route table (§7).

        Returns True when a route matched (and its handler ran), False when
        none matched — the caller then sends the method's exact 404/501
        fallback. Byte-identical to the former if/elif chains: same order,
        same match predicates, same per-route perf wrap. ``getattr`` resolves
        the handler at request time so a monkeypatched method still
        intercepts; ``wants_path`` threads the query-stripped path into the
        eleven path-taking handlers. (NOTE: returning a bool, not ``fn()`` —
        the void handlers all return None, so a None-return contract could not
        tell "handled" from "unmatched".)
        """
        path = self.path.split("?", 1)[0]
        for kind, pattern, name, perf, wants_path in table:
            if kind == "exact":
                hit = path == pattern
            elif kind == "prefix":
                hit = path.startswith(pattern)
            else:  # "prefix+suffix": pattern is (prefix, suffix)
                hit = path.startswith(pattern[0]) and path.endswith(pattern[1])
            if not hit:
                continue
            fn = getattr(self, name)
            args = (path,) if wants_path else ()
            if perf is None:
                fn(*args)
            elif perf[0] == "scope":
                with self._perf_scope(perf[1]):
                    fn(*args)
            else:  # "phase"
                with self._perf_gate().phase(perf[1]):
                    fn(*args)
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self._method_not_allowed_for_settings():
            return
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_static_file(self.static_dir / "dashboard.html",
                                    "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            # #207 D11 — serve the SVG favicon for the browser's default
            # /favicon.ico request so it stops 404-ing even absent the
            # <link rel="icon"> in dashboard.html. Vite copies public/ verbatim
            # into the build output, so favicon.svg lands under static_dir.
            self._serve_static_file(self.static_dir / "favicon.svg",
                                    "image/svg+xml")
            return
        if path.startswith("/static/"):
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
            return
        if not self._dispatch(_GET_ROUTES):
            self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 — stdlib API
        # No _method_not_allowed_for_settings() guard here (gate P1-3): POST
        # /api/settings must route to _handle_post_settings, not 405.
        if not self._dispatch(_POST_ROUTES):
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
        if not self._dispatch(_DELETE_ROUTES):
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

    @staticmethod
    def _perf_gate():
        """Lazy-load the opt-in backend phase-instrumentation collector (#276).
        Near-noop when tracing is off (``phase()`` returns a shared singleton),
        so the coarse ``endpoint.*`` wraps in ``do_GET`` cost nothing by
        default."""
        return sys.modules["cctally"]._load_sibling("_lib_perf")

    @contextlib.contextmanager
    def _perf_scope(self, name):
        """Like ``_perf_gate().phase(name)`` but ALSO stashes the completed root
        so the loopback ``/api/debug/backend`` surface can read a CONVERSATION
        trace, not just the ``/api/data`` snapshot tree (Session C / M5 — the
        Session A dead-end: no ``stash_last`` ran on a request thread). On enter
        it ``reset_thread()``s (each conversation handler runs on its own request
        thread, so this can't clobber the snapshot-build thread's tree) and opens
        ``phase(name)``; on clean exit it stashes ``current_root()``. Near-noop
        when ``CCTALLY_PERF_TRACE`` is off — ``phase()`` returns ``_NULL_PHASE``,
        no root is pushed, so ``current_root()`` is ``None`` and ``stash_last``
        early-returns. Used ONLY for the short-lived, assembly-relevant routes;
        the long-lived ``/events`` SSE keeps a plain, non-stashing wrap (Codex F3)
        so it can never overwrite the last useful assembly trace on disconnect."""
        perf = self._perf_gate()
        perf.reset_thread()
        with perf.phase(name):
            yield
        try:
            perf.stash_last(
                perf.current_root(),
                generated_at=dt.datetime.now(dt.timezone.utc).isoformat())
        except Exception:  # noqa: BLE001 — a diagnostic stash must never 500
            pass

    def _require_debug_backend_allowed(self) -> bool:
        """Gate for ``/api/debug/backend`` (issue #276) — STRICTER than the
        transcript gate.

        PRIMARY: the TCP peer (``client_address[0]``) must be loopback — the
        unspoofable signal (the dashboard can bind ``0.0.0.0``, so a
        ``Host``-only check is spoofable). DEFENSE-IN-DEPTH: the ``Host``
        authority must ALSO be an IP-literal loopback (anti-DNS-rebinding).
        ``expose_transcripts`` is NEVER consulted. 403 + ``False`` otherwise.
        """
        ta = self._transcript_gate()
        peer = self.client_address[0] if self.client_address else ""
        host = self.headers.get("Host")
        if not ta.debug_backend_allowed(peer, host):
            self._respond_403("forbidden")
            return False
        return True

    def _handle_get_debug_backend(self) -> None:
        """GET ``/api/debug/backend`` — loopback-only backend diagnostic (#276).

        Returns the last completed snapshot build's phase-timing tree (present
        only if the dashboard was started with ``CCTALLY_PERF_TRACE=1``; else
        ``null`` + a ``tracing_disabled`` note) plus on-demand cache-table row
        counts and signature legs. Leaks no prompt / prose / paths — timings,
        counts, flag names, and safe cache-table names only. ``schemaVersion``
        is 1 but the surface is documented UNSTABLE (a diagnostic, not a
        consumer contract): phase names / nesting / fields may change without a
        version bump.
        """
        if not self._require_debug_backend_allowed():
            return
        last = self._perf_gate().last_backend_perf()
        dataset: dict = {}
        cache_state: dict = {}
        try:
            conn = open_cache_db()
            try:
                dataset = _debug_cache_table_counts(conn)
                cache_state = _debug_cache_state(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — a diagnostic must not 500 loudly
            cache_state = {"_error": f"{type(exc).__name__}: {exc}"}
        body = {
            "schemaVersion": 1,
            "version": _debug_tool_version(),
            "generated_at": (last or {}).get("generated_at"),
            "dataset": dataset,
            "phases": (last or {}).get("phases"),
            "cache_state": cache_state,
        }
        if body["phases"] is None:
            body["note"] = "tracing_disabled"
        self._respond_json(200, body)

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


    # ---- share endpoints (spec §5.1) — thin delegators to the F1 sibling ----
    # Bodies live in bin/_cctally_dashboard_share.py as *_impl(handler, …) free
    # functions (#279 S5 F1); these keep the DashboardHTTPHandler method surface
    # + the privacy/CSRF gating identical (the getattr(self, name) route dispatch
    # + any monkeypatched-method interception still resolve on the class).
    def _share_load_templates_module(self):
        return _share_load_templates_module_impl(self)

    def _handle_share_templates_get(self) -> None:
        return _handle_share_templates_get_impl(self)

    def _handle_share_render_post(self) -> None:
        return _handle_share_render_post_impl(self)

    def _handle_share_compose_post(self) -> None:
        return _handle_share_compose_post_impl(self)

    def _handle_share_presets_get(self) -> None:
        return _handle_share_presets_get_impl(self)

    def _handle_share_presets_post(self) -> None:
        return _handle_share_presets_post_impl(self)

    def _handle_share_presets_delete(self) -> None:
        return _handle_share_presets_delete_impl(self)

    def _handle_share_history_get(self) -> None:
        return _handle_share_history_get_impl(self)

    def _handle_share_history_post(self) -> None:
        return _handle_share_history_post_impl(self)

    def _handle_share_history_delete(self) -> None:
        return _handle_share_history_delete_impl(self)

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


    # ---- conversation viewer (spec §6) — thin delegators to the F4 sibling ----
    # Bodies live in bin/_cctally_dashboard_conversation.py as *_impl(handler, …)
    # free functions (#279 S5 F4); the privacy gates + CSRF asymmetry stay on the
    # class and are reached via the handler parameter. getattr(self, name) route
    # dispatch + method monkeypatching still resolve on the class.
    @staticmethod
    def _conversation_query():
        return _conversation_query_impl()

    def _parse_search_kind(self, q, valid=_CONV_SEARCH_KINDS):
        return _parse_search_kind_impl(self, q, valid)

    def _run_conversation_query(self, kernel_call, log_label):
        return _run_conversation_query_impl(self, kernel_call, log_label)

    def _parse_conversation_filters(self, q):
        return _parse_conversation_filters_impl(self, q)

    def _handle_get_conversations(self) -> None:
        return _handle_get_conversations_impl(self)

    def _handle_get_conversations_facets(self) -> None:
        return _handle_get_conversations_facets_impl(self)

    def _handle_get_conversation_detail(self, path: str) -> None:
        return _handle_get_conversation_detail_impl(self, path)

    def _handle_get_conversation_events(self, path: str) -> None:
        return _handle_get_conversation_events_impl(self, path)

    def _handle_get_conversation_search(self) -> None:
        return _handle_get_conversation_search_impl(self)

    def _handle_get_conversation_payload(self, path: str) -> None:
        return _handle_get_conversation_payload_impl(self, path)

    def _handle_get_conversation_outline(self, path: str) -> None:
        return _handle_get_conversation_outline_impl(self, path)

    def _handle_get_conversation_prompts(self, path: str) -> None:
        return _handle_get_conversation_prompts_impl(self, path)

    def _handle_get_conversation_export(self, path: str) -> None:
        return _handle_get_conversation_export_impl(self, path)

    def _handle_get_conversation_find(self, path: str) -> None:
        return _handle_get_conversation_find_impl(self, path)

    def _handle_get_conversation_media(self, path: str) -> None:
        return _handle_get_conversation_media_impl(self, path)

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
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, socket.timeout):
            # #279 S1 F3: a stalled send past the handler timeout raises
            # socket.timeout inside the SSE loop — treat it as a client
            # disconnect (same as the other peer-gone classes), not an error.
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
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, socket.timeout):
            # #279 S1 F3: a stalled send past the handler timeout raises
            # socket.timeout inside the SSE loop — treat it as a client
            # disconnect (same as the other peer-gone classes), not an error.
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
    """#278 Theme A (A1): build the dashboard's first snapshot as a CHEAP
    partial on a normal launch so the HTTP port binds in ~110ms instead of
    waiting on the ~2.2s full aggregation. #179 already deferred the *ingest*
    half of cold start (the background ``_DashboardSyncThread`` owns
    ``sync_cache``); this defers the *aggregation* half too.

    Normal launch (``not args.no_sync``): populate only the two sub-ms headline
    panels — ``current_week`` + ``forecast`` — plus the real doctor +
    envelope-config precompute, and set ``hydrating=True``; every heavy panel
    stays at its empty default. The background thread's first tick runs the
    full cold build + SSE-publish, and the client hydrates the heavy panels
    from that frame. Built via the INDIVIDUAL builder helpers, NOT
    ``_tui_build_snapshot`` — so it never calls ``store_dispatch_state`` and
    never poisons the idle memo / accelerator caches (§1.1): the dispatch memo
    stays empty, so the first background tick sees ``prior_key=None`` →
    non-idle → a full cold build, and idle-reuse can never serve the partial.
    ``_tui_build_current_week`` / ``_tui_build_forecast_view`` touch no
    process-local cache state with their default args (Codex P3), so the seed
    leaves every accelerator pristine.

    ``--no-sync`` (§1.2): there is no background thread to fill the partial in,
    so the cheap seed would become the PERMANENT state (heavy panels never
    populate). Keep the full pre-bind build (``hydrating=False``) — a ~2s bind
    in niche frozen-data mode is acceptable since nothing hydrates later.

    §1.3: ``snapshot_to_envelope`` runs the REAL doctor inline PER CONNECTION
    when ``doctor_payload is None`` and KeyErrors on an ``envelope_precompute``
    missing ``"config"`` / the update fields, so a None-doctor seed would be
    WRONG (worse: every SSE client's first serialization would re-run doctor).
    The seed therefore runs BOTH precomputes for real (~110ms total). ``getattr``
    so minimal test ``args`` namespaces (no ``host`` / ``no_sync``) still build.
    """
    c = _cctally()
    tui = c._cctally_tui
    if getattr(args, "no_sync", False):
        # Route _tui_build_snapshot through the cctally module (its re-export)
        # so ``monkeypatch.setitem(ns, "_tui_build_snapshot", spy)`` in tests
        # propagates — identical to the pre-change call form.
        return c._tui_build_snapshot(
            now_utc=pinned_now, skip_sync=True,
            display_tz_pref_override=display_tz_pref_override,
            precompute_envelope=True, runtime_bind=getattr(args, "host", None),
        )

    import time as _time
    now_utc = pinned_now or dt.datetime.now(dt.timezone.utc)
    runtime_bind = getattr(args, "host", None)
    base = tui._tui_empty_snapshot(now_utc)
    errors: list[str] = []
    cw = None
    fc = None
    fc_view = None
    conn = open_db()
    try:
        try:
            cw = tui._tui_build_current_week(conn, now_utc, skip_sync=True)
        except Exception as exc:  # noqa: BLE001 — never block the bind
            errors.append(f"current-week: {exc}")
        try:
            fc_view = tui._tui_build_forecast_view(conn, now_utc, skip_sync=True)
            fc = fc_view.output if fc_view is not None else None
        except Exception as exc:  # noqa: BLE001
            errors.append(f"forecast: {exc}")
    finally:
        conn.close()
    # §1.3: run BOTH precomputes for real so the envelope serializes cleanly
    # without the per-connection inline-doctor fork or the config/update KeyErrors.
    doctor_payload = None
    envelope_precompute = None
    try:
        envelope_precompute = tui._tui_precompute_envelope_config(load_config())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"envelope-precompute: {exc}")
    try:
        doctor_payload = tui._tui_precompute_doctor_payload(now_utc, runtime_bind)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"doctor-precompute: {exc}")
    return dataclasses.replace(
        base,
        current_week=cw,
        forecast=fc,
        forecast_view=fc_view,
        last_sync_at=_time.monotonic(),
        last_sync_error=("; ".join(errors) if errors else None),
        doctor_payload=doctor_payload,
        envelope_precompute=envelope_precompute,
        hydrating=True,
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

