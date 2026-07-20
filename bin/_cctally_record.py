"""Record-usage / hook-tick hot-path subsystem for cctally.

Eager I/O sibling: bin/cctally loads this at startup. Holds the
runtime path that every Claude Code statusline tick and every CC
hook fires:

- ``cmd_record_usage`` — the statusline-driven entry point. Parses
  ``--percent`` / ``--resets-at`` / ``--five-hour-*``, applies
  ULP-noise sanitization at the ingress (``_normalize_percent``),
  resolves the canonical 5h window key (Tier 1 blocks-table + Tier
  2 snapshots fallback), runs the mid-week reset-event detector,
  applies the per-window 7d/5h monotonicity clamps, dedup-skips
  no-op ticks (with a self-heal probe that re-fires the milestone
  + 5h-block helpers when a prior process was killed between
  ``insert_usage_snapshot`` and the helpers), inserts the snapshot
  row, queues the milestone + 5h-block updates, and writes the
  ``hwm-7d`` / ``hwm-5h`` files.
- ``cmd_hook_tick`` — the CC hook entry point. Reads CC's JSON
  payload from stdin BEFORE fork (POSIX §2.9.3 makes ``cmd &``
  blank stdin), forks to background so CC unblocks immediately,
  detaches stdio to ``hook-tick.log``, runs ``sync_cache`` + a
  throttled OAuth refresh under ``hook-tick.last-fetch.lock``, and
  writes one log line. Normal mode returns 0 unconditionally
  (hook discipline); ``--explain`` returns a decision-tree exit code.
- ``maybe_record_milestone`` — percent-crossing detector. Runs
  ``cmd_sync_week`` to refresh cost-on-disk, computes cumulative +
  marginal cost via ``_compute_cost_for_weekref`` for reset-affected
  weeks or ``get_latest_cost_for_week`` otherwise, inserts a
  ``percent_milestones`` row per crossed threshold inside a single
  transaction, and queues ``_dispatch_alert_notification`` jobs
  for thresholds configured in ``alerts.weekly_thresholds`` (set-
  then-dispatch invariant, spec §3.2).
- ``maybe_update_five_hour_block`` — 5h block upsert + rollup-children
  replace-all + 5h-% milestone detection. Resolves block_start_at
  from prior row (or computes from ``five_hour_resets_at - 5h`` on
  first observation), recomputes totals via ``_compute_block_totals``,
  upserts the parent row with ON CONFLICT DO UPDATE, replaces
  per-(block, model) and per-(block, project) children, fires the
  5h-% alert dispatch, and runs the cross-reset cross-flag JOIN
  sweep — all inside one BEGIN.
- ``_compute_block_totals`` — sums tokens + cost over
  [block_start_at, range_end] from ``session_entries``, with
  per-model and per-project breakdowns. Routes through
  ``get_claude_session_entries`` (cache-first / lock-contention
  fallback / direct-JSONL fallback) so the rollup children inherit
  the cache subsystem's correctness envelope.
- ``insert_usage_snapshot`` / ``_saved_dict_from_usage_row`` —
  ``weekly_usage_snapshots`` INSERT and its inverse (rebuild the
  ``saved`` dict from an existing row for the dedup self-heal
  path).
- ``DerivedWeekWindow`` + ``_derive_week_from_payload`` +
  ``_coerce_payload_captured_at`` — payload-to-week-bucket
  resolution shared by ``insert_usage_snapshot``. Anchors the
  bucket-key date on the canonical UTC ISO (regression: Israel host
  briefly running with TZ=America/Los_Angeles spawned ghost
  ``week_start_date`` rows; see ``tests/test_derive_week_utc_anchor.py``).
- ``_normalize_percent`` — single chokepoint that flushes IEEE 754
  ULP noise out of ingress percent floats. Applied at every
  cmd_record_usage ingress site (CLI args, hook-tick OAuth refresh,
  refresh-usage OAuth fetch). 10dp round is well below any
  meaningful consumer precision but above IEEE 754 ULP scale near
  100.
- ``_hook_tick_*`` helpers — log/throttle file primitives,
  stdin-read, session-id short, log-line formatter.
- ``_safe_float`` / ``_validate_date_optional`` — payload-validation
  helpers consumed only by ``insert_usage_snapshot``.
- ``_logged_window_key_coerce_failure`` — one-shot module-level
  guard so a misbehaving caller passing a non-int ``fiveHourWindowKey``
  doesn't spam stderr on every insert.

What stays in bin/cctally:
- Path constants ``APP_DIR``, ``HOOK_TICK_LOG_DIR``,
  ``HOOK_TICK_LOG_PATH``, ``HOOK_TICK_LOG_ROTATED_PATH``,
  ``HOOK_TICK_LOG_ROTATE_BYTES``, ``HOOK_TICK_THROTTLE_PATH``,
  ``HOOK_TICK_THROTTLE_LOCK_PATH``,
  ``HOOK_TICK_DEFAULT_THROTTLE_SECONDS`` — referenced from the
  moved bodies via the ``c = _cctally()`` call-time accessor pattern
  (spec §5.5, same as ``bin/_cctally_cache.py``). The accessor
  resolves ``sys.modules['cctally'].X`` on every call, so the
  conftest ``redirect_paths`` ``setitem(ns, "APP_DIR", tmp)``
  propagates transparently — no sibling-side patches needed.
- Alerts-config surface (``_AlertsConfigError``, ``_get_alerts_config``,
  ``_warn_alerts_bad_config_once``, ``_ALERTS_BAD_CONFIG_WARNED``)
  — stays in bin/cctally per task brief; consumed by dashboard
  and other surfaces beyond record/hook-tick. Routed through
  module-level shims here so the moved bodies keep bare-name
  call shape.
- ``_dispatch_alert_notification`` — already lives in
  ``bin/_cctally_alerts.py`` (Phase B). Accessed via shim that
  resolves through ``sys.modules['cctally']._dispatch_alert_notification``
  so the eager re-export in bin/cctally propagates the same
  function object both sides see.
- ``cmd_sync_week`` (Phase B sibling), ``cmd_refresh_usage`` /
  ``_hook_tick_oauth_refresh`` / ``_hook_tick_make_mock_refresh``
  / ``_get_oauth_usage_config`` / ``OauthUsageConfigError``
  (Phase C ``_cctally_refresh.py``) — consumed from this sibling
  via the same bare-name shim or ``c.X`` pattern.
- ``open_db``, ``open_cache_db``, ``sync_cache``, ``parse_iso_datetime``,
  ``now_utc_iso``, ``load_config``, ``get_week_start_name``,
  ``compute_week_bounds``, ``parse_date_str``,
  ``_canonicalize_optional_iso``, ``_canonical_5h_window_key``,
  ``_floor_to_hour``, ``_get_canonical_boundary_for_date``,
  ``_apply_reset_events_to_weekrefs``, ``_week_ref_has_reset_event``,
  ``_compute_cost_for_weekref``, ``get_latest_cost_for_week``,
  ``get_max_milestone_for_week``, ``get_milestone_cost_for_week``,
  ``insert_percent_milestone``, ``make_week_ref``,
  ``_calculate_entry_cost``, ``_resolve_primary_model_for_block``,
  ``_resolve_display_tz_obj``, ``_build_alert_payload_weekly``,
  ``_build_alert_payload_five_hour``, ``eprint``,
  ``get_claude_session_entries``, ``_FIVE_HOUR_JITTER_FLOOR_SECONDS``,
  ``_RESET_PCT_DROP_THRESHOLD`` — boundary helpers, already-extracted
  subsystems, or constants reached through the cctally namespace
  (``_RESET_PCT_DROP_THRESHOLD`` now lives in ``bin/_cctally_weekrefs.py``,
  re-exported on the cctally ns). Accessed via the shim/``c.X`` pattern
  EXCEPT the names honest-imported by the #279 S4 F5 collapse below.

  #279 S4 F5 (the #50 treatment): the forwarding shims for
  ``open_cache_db`` (→ ``_cctally_cache``), ``_floor_to_hour`` (→
  ``_lib_blocks``), ``_resolve_display_tz_obj`` (→ ``_lib_display_tz``),
  ``_build_alert_payload_{weekly,five_hour,budget,project_budget,
  codex_budget,projected}`` (→ ``_lib_alerts_payload``), and
  ``_get_oauth_usage_config`` (→ ``_cctally_refresh``) were replaced by
  honest top-level imports — each real def lives in a sibling
  bin/cctally eager-loads BEFORE _cctally_record, and none is
  monkeypatched through the cctally namespace / this module's route.
  The remaining 38 shims below STAY: patched surfaces (``load_config``,
  ``sync_cache``, ``_dispatch_alert_notification``, ``compute_budget_status``,
  ``_apply_reset_events_to_weekrefs``, ``resolve_display_tz``,
  ``_sum_cost_by_project``, ``_project_budget_labels``,
  ``get_claude_session_entries``, ``_compute_cost_for_weekref``,
  ``_get_canonical_boundary_for_date``, ``_hook_tick_oauth_refresh``),
  bin/cctally-homed residues (``_resolve_primary_model_for_block``,
  ``_warn_alerts_bad_config_once``, ``_warn_budget_bad_config_once``,
  ``_hook_tick_make_mock_refresh``, ``cmd_sync_week``), and names whose
  real home (``_cctally_milestones`` / ``_cctally_forecast`` /
  ``_cctally_weekrefs``) bin/cctally eager-loads AFTER _cctally_record —
  honest-importing those at module top would force an early sibling load.

§5.6 audit on this extraction's monkeypatch surface:
- ``cmd_record_usage`` — patched via ``monkeypatch.setitem(ns, …)``
  by 5 test files (``test_hook_tick_rate_limit.py``,
  ``test_refresh_usage_inproc.py``, ``test_refresh_usage_cmd.py``,
  callers via ``ns["cmd_record_usage"](...)``). Re-export in
  bin/cctally propagates patches; the moved body never reaches
  for itself.
- ``_hook_tick_oauth_refresh`` — patched via
  ``monkeypatch.setitem(ns, …)`` by ``test_hook_tick_rate_limit.py``.
  Moved ``cmd_hook_tick`` uses a module-level shim that resolves
  via ``sys.modules['cctally']`` at call time; the
  ``globals()["_hook_tick_oauth_refresh"] = …`` mock-injection
  branch is rewritten to mutate ``sys.modules['cctally']`` so
  ``--mock-oauth-response`` still propagates.
- ``_normalize_percent`` — test reads via
  ``ns["_normalize_percent"]`` (``test_record_usage_precision.py``).
  Re-export in bin/cctally propagates the same function object.
- ``_derive_week_from_payload`` — test reads via
  ``ns["_derive_week_from_payload"]``
  (``test_derive_week_utc_anchor.py``). Re-export in bin/cctally
  propagates.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3.
import _cctally_core
from _cctally_core import (
    eprint,
    now_utc_iso,
    parse_iso_datetime,
    open_db,
    get_week_start_name,
    compute_week_bounds,
    parse_date_str,
    _canonicalize_optional_iso,
    _reset_aware_floor,
    make_week_ref,
    _get_alerts_config,
    _AlertsConfigError,
    _BudgetConfigError,
    _command_as_of,
)
from _lib_five_hour import _canonical_5h_window_key, five_hour_milestone_range
from _lib_pricing import _calculate_entry_cost
from _lib_codex_hooks import (
    CODEX_HOOK_THROTTLE_SECONDS,
    acquire_due_lifecycle_locks,
    codex_hook_roots,
    mark_lifecycle_success,
    release_lifecycle_locks,
)


import importlib.util as _ilu


def _ensure_sibling_loaded(name: str) -> None:
    """Register a NON-eager-loaded ``_lib_*`` sibling in ``sys.modules``.

    Every ``_lib_*`` this module imports at body-time is eager-loaded by
    ``bin/cctally`` EXCEPT the #279 S4 kernels (``_lib_credit``,
    ``_lib_record``) — those are consumer-only and were deliberately kept
    out of ``bin/cctally``'s eager-load block so ``bin/cctally`` stays
    byte-untouched (spec §2 re-export continuity). Under the
    ``SourceFileLoader`` harness path (``bin/`` absent from ``sys.path``) a
    bare ``from _lib_X import`` would then miss, so this pre-registers the
    sibling ``__file__``-relative first — mirroring ``_cctally_cache.
    _load_lib`` and ``_lib_conversation_query``'s ``_lib_perf`` fallback.
    The honest ``from _lib_X import`` that follows is a ``sys.modules`` hit
    in every load context (prod script, conftest, harness).
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


_ensure_sibling_loaded("_lib_credit")
from _lib_credit import (
    _PERCENT_NORMALIZE_DECIMALS,
    _normalize_percent,
    CreditPlan,
    _parse_credit_at,
    _build_credit_plan,
)
_ensure_sibling_loaded("_lib_record")
from _lib_record import (
    check_resets_at_plausibility,
    plan_weekly_credit_debounce,
    plan_five_hour_credit,
    hwm_clamp_applies,
    milestone_coverage_owes,
    hwm_file_next,
    projected_crossings,
    FIRE_IMMEDIATE,
    CONFIRM_RESET,
    CLEAR_MARKER,
    ARM_MARKER,
)

# === #279 S4 F5: forwarding-shim wall collapse (the #50 treatment) =========
# Names whose real def lives in a sibling that bin/cctally eager-loads BEFORE
# _cctally_record (so these are sys.modules hits in every load context) AND
# that no test monkeypatches through the cctally namespace / _cctally_record
# route are honest-imported here instead of routed through a
# ``sys.modules["cctally"].X`` shim. The audit (full table in the commit body)
# used the suite as the authority; every name below survived the full
# bin/cctally-test-all. Patched / bin/cctally-homed / post-889-homed names
# keep their shims below.
from _cctally_cache import open_cache_db
from _lib_blocks import _floor_to_hour
from _lib_display_tz import _resolve_display_tz_obj
from _lib_alerts_payload import (
    _build_alert_payload_weekly,
    _build_alert_payload_five_hour,
    _build_alert_payload_budget,
    _build_alert_payload_project_budget,
    _build_alert_payload_codex_budget,
    _build_alert_payload_projected,
)
from _cctally_refresh import _get_oauth_usage_config


# Module-level back-ref shims (the REMAINING wall after the #279 S4 F5
# collapse above pulled 10 unpatched, pre-889-homed names up to honest
# imports). Each shim below resolves ``sys.modules['cctally'].X`` at CALL
# TIME (not bind time), so monkeypatches on cctally's namespace propagate
# into the moved code unchanged. `load_config` and
# `get_claude_session_entries` STAY as shims even though their natural
# homes are decentralized (_cctally_config / _cctally_cache) — tests
# monkeypatch them via `ns["X"]`; direct imports would silently bypass the
# patches. The rest stay because they are patched, bin/cctally-homed, or
# their real home is eager-loaded AFTER this module (see the F5 note in the
# module docstring for the full STAY/COLLAPSE audit).
# See spec §3.5 (carve-out) and §3.7 (stays-on-shim allowlist).
def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def sync_cache(*args, **kwargs):
    return sys.modules["cctally"].sync_cache(*args, **kwargs)


def _get_canonical_boundary_for_date(*args, **kwargs):
    return sys.modules["cctally"]._get_canonical_boundary_for_date(*args, **kwargs)


def _apply_reset_events_to_weekrefs(*args, **kwargs):
    return sys.modules["cctally"]._apply_reset_events_to_weekrefs(*args, **kwargs)


def _week_ref_has_reset_event(*args, **kwargs):
    return sys.modules["cctally"]._week_ref_has_reset_event(*args, **kwargs)


def _compute_cost_for_weekref(*args, **kwargs):
    return sys.modules["cctally"]._compute_cost_for_weekref(*args, **kwargs)


def get_latest_cost_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_latest_cost_for_week(*args, **kwargs)


def get_max_milestone_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_max_milestone_for_week(*args, **kwargs)


def get_milestone_cost_for_week(*args, **kwargs):
    return sys.modules["cctally"].get_milestone_cost_for_week(*args, **kwargs)


def insert_percent_milestone(*args, **kwargs):
    return sys.modules["cctally"].insert_percent_milestone(*args, **kwargs)


def cmd_sync_week(*args, **kwargs):
    return sys.modules["cctally"].cmd_sync_week(*args, **kwargs)


def _resolve_primary_model_for_block(*args, **kwargs):
    return sys.modules["cctally"]._resolve_primary_model_for_block(*args, **kwargs)


def _budget_crossings(*args, **kwargs):
    return sys.modules["cctally"]._budget_crossings(*args, **kwargs)


def _resolve_budget_window(*args, **kwargs):
    return sys.modules["cctally"]._resolve_budget_window(*args, **kwargs)


def _budget_spend_for_vendor(*args, **kwargs):
    return sys.modules["cctally"]._budget_spend_for_vendor(*args, **kwargs)


def _resolve_codex_budget_period_window(*args, **kwargs):
    return sys.modules["cctally"]._resolve_codex_budget_period_window(*args, **kwargs)


def resolve_display_tz(*args, **kwargs):
    return sys.modules["cctally"].resolve_display_tz(*args, **kwargs)


def _sum_cost_by_project(*args, **kwargs):
    return sys.modules["cctally"]._sum_cost_by_project(*args, **kwargs)


def insert_project_budget_milestone(*args, **kwargs):
    return sys.modules["cctally"].insert_project_budget_milestone(*args, **kwargs)


def _project_budget_labels(*args, **kwargs):
    return sys.modules["cctally"]._project_budget_labels(*args, **kwargs)


def _project_crossings(*args, **kwargs):
    return sys.modules["cctally"]._project_crossings(*args, **kwargs)


def _get_budget_config(*args, **kwargs):
    return sys.modules["cctally"]._get_budget_config(*args, **kwargs)


def _budget_alerts_active(*args, **kwargs):
    return sys.modules["cctally"]._budget_alerts_active(*args, **kwargs)


def _resolve_current_budget_window(*args, **kwargs):
    return sys.modules["cctally"]._resolve_current_budget_window(*args, **kwargs)


def _resolve_claude_budget_window(*args, **kwargs):
    return sys.modules["cctally"]._resolve_claude_budget_window(*args, **kwargs)


def insert_projected_milestone(*args, **kwargs):
    return sys.modules["cctally"].insert_projected_milestone(*args, **kwargs)


def _projected_levels_already_latched(*args, **kwargs):
    return sys.modules["cctally"]._projected_levels_already_latched(*args, **kwargs)


def _fetch_current_week_snapshots(*args, **kwargs):
    return sys.modules["cctally"]._fetch_current_week_snapshots(*args, **kwargs)


def _apply_midweek_reset_override(*args, **kwargs):
    return sys.modules["cctally"]._apply_midweek_reset_override(*args, **kwargs)


def _assess_forecast_confidence(*args, **kwargs):
    return sys.modules["cctally"]._assess_forecast_confidence(*args, **kwargs)


def _build_vendor_budget_inputs(*args, **kwargs):
    return sys.modules["cctally"]._build_vendor_budget_inputs(*args, **kwargs)


def compute_budget_status(*args, **kwargs):
    return sys.modules["cctally"].compute_budget_status(*args, **kwargs)


def _dispatch_alert_notification(*args, **kwargs):
    return sys.modules["cctally"]._dispatch_alert_notification(*args, **kwargs)


def _warn_alerts_bad_config_once(*args, **kwargs):
    return sys.modules["cctally"]._warn_alerts_bad_config_once(*args, **kwargs)


def _warn_budget_bad_config_once(*args, **kwargs):
    return sys.modules["cctally"]._warn_budget_bad_config_once(*args, **kwargs)


def _hook_tick_oauth_refresh(*args, **kwargs):
    """Shim for ``_hook_tick_oauth_refresh``.

    Resolves via ``sys.modules['cctally']`` at call time so
    ``monkeypatch.setitem(ns, "_hook_tick_oauth_refresh", boom)``
    propagates. The ``--mock-oauth-response`` flag below rewrites
    ``sys.modules['cctally']._hook_tick_oauth_refresh`` so this
    shim picks up the mock on the very next call.
    """
    return sys.modules["cctally"]._hook_tick_oauth_refresh(*args, **kwargs)


def _hook_tick_make_mock_refresh(*args, **kwargs):
    return sys.modules["cctally"]._hook_tick_make_mock_refresh(*args, **kwargs)


# Exception classes raised by callees that stay in bin/cctally
# (``_AlertsConfigError``) or in another sibling (``OauthUsageConfigError``
# in ``bin/_cctally_refresh.py``) are caught here via
# ``except sys.modules['cctally'].SomeError`` — Python evaluates the
# ``except`` expression at except-time, so each catch resolves to the
# live class object that the raiser also reaches. See call sites in
# ``maybe_record_milestone``, ``maybe_update_five_hour_block``, and
# ``cmd_hook_tick`` for the three rewrites.


# ``_PERCENT_NORMALIZE_DECIMALS`` + ``_normalize_percent`` now live in
# ``bin/_lib_credit.py`` (#279 S4 F1); re-imported at module top so
# ``bin/cctally``'s ``_cctally_record._PERCENT_NORMALIZE_DECIMALS`` /
# ``_normalize_percent`` re-exports keep resolving unchanged.

# Plausibility band for --resets-at / --five-hour-resets-at (issue #112).
# Out-of-band epochs are guarded at cmd_record_usage ingress before any
# datetime.fromtimestamp() call, so absurd values (ms-epochs, year-off
# bugs) can't crash the call or stamp phantom-week rows.
#
# The two bands are deliberately asymmetric and reject differently:
#
#   --resets-at: 30d past / 8d future. Wide past slack preserves the
#       documented "manually replay a missed snapshot" use case
#       (docs/commands/record-usage.md). Out-of-band → return 2
#       (entire call rejected, no weekly row written).
#
#   --five-hour-resets-at: 10m past / 6h future. Tight past slack is
#       intentional: maybe_update_five_hour_block computes
#       _compute_block_totals(block_start_at, captured_at_dt) where
#       captured_at_dt ≈ now and block_start_at = resets_at - 5h, so
#       accepting an already-expired 5h resets_at pollutes the prior
#       block with session_entries that belong to the NEXT block. 10m
#       matches _FIVE_HOUR_JITTER_FLOOR_SECONDS (the canonical-window-key
#       jitter floor) — enough for boundary jitter / clock skew, not
#       enough for cross-block pollution. Out-of-band → drop the 5h
#       fields and continue (the weekly snapshot still writes), so a
#       manual replay with stale 5h flags doesn't fail-close on a
#       documented recovery path.
_RECORD_USAGE_WEEK_PAST_SLACK_S = 30 * 86400
_RECORD_USAGE_WEEK_FUTURE_BAND_S = 8 * 86400
_RECORD_USAGE_5H_PAST_SLACK_S = 600  # 10 min; matches _FIVE_HOUR_JITTER_FLOOR_SECONDS
_RECORD_USAGE_5H_FUTURE_BAND_S = 6 * 3600


# One-shot guard so a misbehaving caller passing a non-int
# fiveHourWindowKey doesn't spam the log on every insert. Set on first
# loud-skip in insert_usage_snapshot. Moved into this sibling alongside
# insert_usage_snapshot — the `global` statement inside that function
# now binds to THIS module's namespace, which is correct (cctally re-
# exports the function via the eager-load block, but the `global` write
# stays in the sibling's __dict__ and the per-process one-shot semantics
# are preserved across both call routes).
_logged_window_key_coerce_failure = False


# === BEGIN MOVED REGIONS ===
# Path constants (APP_DIR, HOOK_TICK_*) moved to _cctally_core
# 2026-05-22 (#84). Reads use call-time ``_cctally_core.X``; tests
# patch via ``monkeypatch.setattr(_cctally_core, "X", v)``.
#
# Constants pulled at call time:
#   _cctally_core.APP_DIR
#   _cctally_core.HOOK_TICK_LOG_DIR / _PATH / _ROTATED_PATH / _ROTATE_BYTES
#   _cctally_core.HOOK_TICK_THROTTLE_PATH / _LOCK_PATH
#   c._FIVE_HOUR_JITTER_FLOOR_SECONDS — _lib_five_hour.* re-export
#   c._RESET_PCT_DROP_THRESHOLD       — bin/_cctally_weekrefs.py constant (re-exported on cctally ns)
#   c._is_reset_drop                  — bin/_cctally_weekrefs.py helper (re-exported on cctally ns)
#   c.HOOK_TICK_DEFAULT_THROTTLE_SECONDS
# (#279 S4 F5 collapsed only function-forwarding shims to honest imports;
#  these call-time constant/helper accessors are untouched.)


def _resolve_active_five_hour_reset_event_id(
    conn: "sqlite3.Connection",
    five_hour_window_key: int,
) -> int:
    """Return ``id`` of the most-recent ``five_hour_reset_events`` row for
    ``five_hour_window_key``, else 0 (pre-credit / no-event sentinel).

    Mirrors the weekly active-segment resolution pattern used by
    ``maybe_record_milestone`` for ``percent_milestones.reset_event_id``.
    Called once per ``maybe_update_five_hour_block`` invocation and the
    return value is threaded through every read/write site that keys on
    ``(five_hour_window_key, percent_threshold)`` so post-credit threshold
    crossings land as a distinct row from any pre-credit one at the same
    threshold. See spec
    docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md §3.3.

    Returns ``0`` when:
      - The window has no ``five_hour_reset_events`` row (most blocks).
      - The table doesn't exist yet (DB predates this feature).

    Returns the largest ``id`` matching the window otherwise; the
    ``ORDER BY id DESC LIMIT 1`` clause is what *defines* "active" in
    the stacked-credit case (spec §2.3 — multiple events across distinct
    10-min slots): pre-credit milestones key on ``seg=0``, milestones
    between credit 1 and credit 2 key on event-1's id, and milestones
    after credit 2 key on event-2's id.
    """
    try:
        row = conn.execute(
            "SELECT id FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(five_hour_window_key),),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    return int(row["id"])


def maybe_record_milestone(
    saved: dict[str, Any],
) -> None:
    """Check if a new integer percent threshold was crossed, and if so,
    fetch cost and record the milestone. Errors are logged, not raised."""
    weekly_percent = saved.get("weeklyPercent")
    if weekly_percent is None or weekly_percent < 1:
        return

    # Snap near-integer values up before flooring: the status-line API returns
    # N% as 0.N * 100, which in IEEE 754 can land one ULP below N.0 (e.g.
    # 0.58 * 100 == 57.99999999999999). A bare math.floor() then returns N-1
    # and the N-threshold milestone is never recorded.
    current_floor = math.floor(weekly_percent + 1e-9)
    if current_floor < 1:
        return

    week_start_date = saved["weekStartDate"]
    week_end_date = saved["weekEndDate"]
    week_start_at = saved.get("weekStartAt")
    week_end_at = saved.get("weekEndAt")
    usage_snapshot_id = saved["id"]
    five_hour_percent = saved.get("fiveHourPercent")

    conn = open_db()
    try:
        # Resolve the active segment for THIS captured moment. The segment
        # is the latest week_reset_events row keyed on week_end_at whose
        # effective_reset_at_utc <= captured_at; 0 = pre-credit / no-event
        # sentinel. ``unixepoch()`` normalizes the comparison across mixed
        # +00:00 / Z offsets (see precedent at bin/cctally:_compute_block_totals
        # cross-reset detection; also project gotcha
        # ``unixepoch_for_cross_offset_compare``).
        captured_at_iso = saved.get("capturedAt") or now_utc_iso()
        reset_event_id = 0
        if week_end_at:
            seg_row = conn.execute(
                """
                SELECT id FROM week_reset_events
                 WHERE new_week_end_at = ?
                   AND unixepoch(effective_reset_at_utc) <= unixepoch(?)
                 ORDER BY id DESC LIMIT 1
                """,
                (week_end_at, captured_at_iso),
            ).fetchone()
            if seg_row is not None:
                reset_event_id = int(seg_row["id"])

        max_existing = get_max_milestone_for_week(
            conn, week_start_date, reset_event_id=reset_event_id,
        )
        if max_existing is not None and current_floor <= max_existing:
            return

        # Threshold crossed — sync cost before recording so the milestone
        # captures up-to-date cumulative cost, not a stale snapshot.
        try:
            sync_ns = argparse.Namespace(
                week_start=None,
                week_end=None,
                week_start_name=None,
                mode="auto",
                offline=False,
                project=None,
                json=False,
                quiet=True,
            )
            cmd_sync_week(sync_ns)
        except Exception as exc:
            eprint(f"[milestone] cost sync failed, using latest available: {exc}")

        week_start = dt.date.fromisoformat(week_start_date)
        week_end = dt.date.fromisoformat(week_end_date)
        week_ref = make_week_ref(
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=week_end_at,
        )

        # For reset-affected weeks, the cached weekly_cost_snapshots row
        # covers the API-derived range (which for a post-reset week
        # backdates into the old window). Live-compute over the effective
        # range so the milestone captures cost from the reset moment
        # forward, not from the phantom backdated start.
        effective_ref = week_ref
        adjusted = _apply_reset_events_to_weekrefs(conn, [week_ref])
        if adjusted:
            effective_ref = adjusted[0]

        if _week_ref_has_reset_event(conn, effective_ref):
            live_cost = _compute_cost_for_weekref(effective_ref)
            if live_cost is None:
                eprint("[milestone] could not compute effective-range cost, skipping")
                return
            cumulative_cost = live_cost
            cost_snapshot_id = 0  # no snapshot row to anchor against
        else:
            latest_cost = get_latest_cost_for_week(conn, week_ref)
            if latest_cost is None:
                eprint("[milestone] no cost snapshot yet for this week, skipping")
                return
            cumulative_cost = float(latest_cost["cost_usd"])
            cost_snapshot_id = int(latest_cost["id"])

        # Determine which thresholds to record
        start_threshold = (max_existing + 1) if max_existing is not None else current_floor

        # Hoist `_get_alerts_config(load_config())` above the per-pct loop:
        # in the catch-up case (multi-percent jump on first observation) the
        # loop iterates N times and the config never changes mid-loop. One
        # read serves all iterations.
        # `load_config()` is safe outside the writer lock — atomic-rename
        # guarantees readers see whole bytes (CLAUDE.md gotcha).
        # `_ALERTS_BAD_CONFIG_WARNED` (module-level, M3) rate-limits the
        # warning to once per process; both axis paths share the flag since
        # the underlying problem is config-wide, not axis-specific.
        try:
            alerts_cfg: "dict | None" = _get_alerts_config(load_config())
        except _AlertsConfigError as exc:
            _warn_alerts_bad_config_once(exc)
            alerts_cfg = None

        # Collect dispatch jobs across the per-pct loop and fire AFTER the
        # single commit below. Mirrors the 5h path's pending_alerts pattern
        # (set-then-dispatch + atomic INSERT/UPDATE, spec §3.2). Without
        # this, `insert_percent_milestone`'s prior internal commit would
        # split INSERT and the alerted_at UPDATE across two transactions —
        # a crash in the gap left `alerted_at` NULL forever, since the
        # next call's INSERT OR IGNORE returns rowcount==0 and the
        # `if inserted == 1` dispatch guard skips re-firing.
        pending_alerts: list[dict[str, Any]] = []
        for pct in range(start_threshold, current_floor + 1):
            if pct == start_threshold and max_existing is not None:
                prev_cost = get_milestone_cost_for_week(
                    conn, week_start_date, max_existing,
                    reset_event_id=reset_event_id,
                )
                marginal = (cumulative_cost - prev_cost) if prev_cost is not None else None
            else:
                marginal = None
            inserted = insert_percent_milestone(
                conn,
                week_start_date=week_start_date,
                week_end_date=week_end_date,
                week_start_at=week_start_at,
                week_end_at=week_end_at,
                percent_threshold=pct,
                cumulative_cost_usd=cumulative_cost,
                marginal_cost_usd=marginal,
                usage_snapshot_id=usage_snapshot_id,
                cost_snapshot_id=cost_snapshot_id,
                five_hour_percent_at_crossing=five_hour_percent,
                commit=False,
                reset_event_id=reset_event_id,
            )
            # ── Threshold-actions dispatch (set-then-dispatch, spec §3.2) ──
            # Only the genuine-new-crossing winner (rowcount==1) reaches this
            # path; concurrent record-usage instances that race on the same
            # (week_start_date, percent_threshold) get rowcount==0 from the
            # INSERT OR IGNORE and skip dispatch entirely. The
            # `alerted_at IS NULL` guard on the UPDATE is defense-in-depth:
            # write-once even if two writers somehow both think they won.
            if inserted == 1:
                if (
                    alerts_cfg is not None
                    and alerts_cfg["enabled"]
                    and pct in alerts_cfg["weekly_thresholds"]
                ):
                    crossed_at = now_utc_iso()
                    # set-then-dispatch: alerted_at lands on the row BEFORE
                    # the osascript Popen, so a dismissed-after-spawn
                    # notification still surfaces in the dashboard alerts
                    # envelope (T5). UPDATE shares the transaction with
                    # the preceding INSERT (commit=False above) so a
                    # crash between them is impossible.
                    conn.execute(
                        "UPDATE percent_milestones SET alerted_at = ? "
                        "WHERE week_start_date = ? AND percent_threshold = ? "
                        "  AND reset_event_id = ? "
                        "  AND alerted_at IS NULL",
                        (crossed_at, week_start_date, pct, reset_event_id),
                    )
                    # Cheap re-read for payload context (cumulative_cost_usd
                    # reflects the value persisted on insert, immune to any
                    # subsequent recompute drift). SELECT inside the open
                    # transaction is fine; values reflect post-INSERT state.
                    # Filter by reset_event_id so a credited week's
                    # alert payload reads the post-credit row, not a
                    # stale pre-credit row at the same (week, threshold).
                    row = conn.execute(
                        "SELECT cumulative_cost_usd FROM percent_milestones "
                        "WHERE week_start_date = ? AND percent_threshold = ? "
                        "  AND reset_event_id = ?",
                        (week_start_date, pct, reset_event_id),
                    ).fetchone()
                    if row is not None:
                        cum = float(row["cumulative_cost_usd"])
                        # $/1% rough trend metric: cumulative / threshold.
                        dpp = (cum / pct) if pct else None
                        payload = _build_alert_payload_weekly(
                            threshold=pct,
                            crossed_at_utc=crossed_at,
                            week_start_date=week_start_date,
                            cumulative_cost_usd=cum,
                            dollars_per_percent=dpp,
                        )
                        pending_alerts.append(payload)
        # Single commit after the loop durably writes every milestone row
        # AND its alerted_at marker together.
        conn.commit()
        # Dispatch deferred to AFTER commit (matches 5h path). Per-payload
        # exception logged so a bad-payload alert can't suppress healthy ones.
        # Production caller ignores _dispatch_alert_notification's return
        # value (spec §6.4).
        for payload in pending_alerts:
            try:
                _dispatch_alert_notification(payload, mode="real")
            except Exception as dispatch_exc:
                eprint(f"[alerts] dispatch failed: {dispatch_exc}")
    except Exception as exc:
        eprint(f"[milestone] error recording milestone: {exc}")
    finally:
        conn.close()


def _record_budget_milestone_for_vendor(
    *, vendor, target, thresholds, period, config, tz, build_payload,
    raise_errors: bool = False,
) -> int:
    """Shared budget-milestone firing core for both vendors (#143).

    Hot-path ordering is preserved verbatim (spec §4.2 / [Pre-probe before
    sync_cache]): ``open_db`` → cheap ``_resolve_budget_window(vendor=…)`` →
    unified pre-probe (which configured thresholds are STILL un-recorded for this
    window/period) → **skip the cost SUM entirely when nothing is pending** →
    ``_budget_spend_for_vendor(vendor=…)`` (the costly leg) →
    ``_budget_crossings(vendor=…)`` (INSERT-and-arm, set-then-dispatch,
    fire-once via rowcount) → single durable commit → post-commit dispatch.

    The pre-probe's ``period = ? OR period IS NULL`` arm (#137) makes a pre-011
    NULL-period row for this window count as already-recorded (no spurious
    upgrade re-fire); a row under the SAME concrete ``period`` also counts
    (fire-once). The cost SUM is skipped ONLY when every threshold already has a
    row — a partial prior run still forces the SUM for the remaining thresholds
    ([Dedup mustn't gate side effects]).

    ``build_payload`` is the vendor's at-fire payload adapter (keeps the dispatch
    ``id`` byte-stable per vendor); it is invoked with
    ``threshold`` / ``crossed_at_utc`` / ``period_key`` / ``period`` /
    ``budget_usd`` / ``spent_usd`` / ``consumption_pct`` keyword args.
    """
    now_utc = _command_as_of()
    pending_alerts: list[dict[str, Any]] = []
    conn = open_db()
    try:
        start_at = _resolve_budget_window(
            conn, vendor=vendor, now_utc=now_utc, period=period,
            config=config, tz=tz,
        )
        if start_at is None:
            return 0  # no resolvable window yet (claude subscription-week pre-snapshot)
        period_key = start_at.isoformat(timespec="seconds")

        present = {
            int(r[0]) for r in conn.execute(
                "SELECT threshold FROM budget_milestones "
                "WHERE vendor = ? AND period_start_at = ? "
                "  AND (period = ? OR period IS NULL)",
                (vendor, period_key, period),
            )
        }
        pending = [t for t in sorted(thresholds) if t not in present]
        if not pending:
            return 0  # nothing left this window → skip the cost SUM

        spent = _budget_spend_for_vendor(
            conn, vendor=vendor, start_at=start_at, now_utc=now_utc
        )
        # Shared INSERT-and-arm core (set-then-dispatch, fire-once via rowcount);
        # commit=False inside, so this conn owns the single durable commit below.
        for t, crossed_at, sp, tg, pct in _budget_crossings(
            conn,
            vendor=vendor,
            period_key=period_key,
            period=period,
            thresholds=pending,
            target=target,
            spent=spent,
            now_utc=now_utc,
        ):
            pending_alerts.append(build_payload(
                threshold=t,
                crossed_at_utc=crossed_at,
                period_key=period_key,
                period=period,
                budget_usd=tg,
                spent_usd=sp,
                consumption_pct=pct,
            ))
        # Single commit: every INSERT + its alerted_at marker durable together.
        conn.commit()
    except Exception as exc:
        eprint(f"[budget-milestone:{vendor}] error recording budget milestone: {exc}")
        if raise_errors:
            raise
    finally:
        conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant — one queue attempt per crossing, deduped on
    # the alerted_at column).
    for payload in pending_alerts:
        try:
            _dispatch_alert_notification(payload, mode="real")
        except Exception as dispatch_exc:
            eprint(f"[budget-alerts:{vendor}] dispatch failed: {dispatch_exc}")
    return len(pending_alerts)


def maybe_record_budget_milestone(saved: dict[str, Any]) -> None:
    """Fire Claude equiv-$ budget alerts on ACTUAL-spend threshold crossings
    (axis ``budget`` — called from ``cmd_record_usage`` alongside the weekly-% /
    5h-% milestone helpers). Thin vendor adapter over
    :func:`_record_budget_milestone_for_vendor` (#143): reads the Claude budget
    config block, gates, resolves ``target`` / ``thresholds`` / ``period``, and
    passes the Claude payload builder. Gated, hot-path-cheap, set-then-dispatch,
    fire-once. Errors are logged, not raised (the caller also wraps).

    ``saved`` is accepted for call-site symmetry with
    ``maybe_record_milestone`` / ``maybe_update_five_hour_block`` but is
    unused: the budget window + live spend are resolved from the DB +
    ``session_entries`` independently (a budget crossing depends on
    cumulative equiv-$ spend, not on the just-recorded 7d-% snapshot).
    """
    # Gate FIRST (hot-path discipline): no budget or alerts off → zero
    # overhead for non-budget users. `load_config()` is safe outside any
    # writer lock — atomic-rename guarantees whole-byte reads. A malformed
    # budget block is a quiet warn-once no-op (mirrors weekly/5h), NOT an
    # unthrottled per-tick stderr via the caller's wrapper. One config read
    # services both the gate and the calendar-window tz resolution.
    config = load_config()
    try:
        budget_cfg = _get_budget_config(config)
    except _BudgetConfigError as exc:
        _warn_budget_bad_config_once(exc)
        return
    if not _budget_alerts_active(budget_cfg):
        return
    thresholds = budget_cfg["alert_thresholds"]
    if not thresholds:
        return
    # Period generalization (spec §6): subscription-week resolves the snapshot-
    # anchored window; a calendar period (calendar-week / calendar-month)
    # resolves the window purely from `now` + the period. config/tz are
    # resolved once for the calendar branch.
    period = budget_cfg.get("period", "subscription-week")
    tz = resolve_display_tz(argparse.Namespace(tz=None), config)
    _record_budget_milestone_for_vendor(
        vendor="claude",
        target=budget_cfg["weekly_usd"],
        thresholds=thresholds,
        period=period,
        config=config,
        tz=tz,
        # The Claude payload builder takes the legacy `week_start_at=` kwarg
        # (its value is the resolved period-start instant, == period_key), so
        # the at-fire dispatch id stays byte-stable `budget:<period_start_at>:<t>`.
        build_payload=lambda **kw: _build_alert_payload_budget(
            threshold=kw["threshold"],
            crossed_at_utc=kw["crossed_at_utc"],
            week_start_at=kw["period_key"],
            budget_usd=kw["budget_usd"],
            spent_usd=kw["spent_usd"],
            consumption_pct=kw["consumption_pct"],
            period=kw["period"],
        ),
    )


def maybe_record_project_budget_milestone(saved: dict[str, Any]) -> None:
    """Fire PER-PROJECT equiv-$ budget alerts on ACTUAL-spend threshold
    crossings (spec §6 — called from ``cmd_record_usage`` alongside the
    weekly-% / 5h-% / budget / projected milestone helpers). An independent
    helper (its own ``load_config()`` / ``open_db()``), matching the existing
    per-axis structure — NOT fused into ``maybe_record_budget_milestone``.

    Gated, hot-path-cheap, pre-probed, set-then-dispatch, fire-once. Errors are
    logged, not raised (the caller also wraps).

    ``saved`` is accepted for call-site symmetry with the sibling helpers but is
    unused: each project's live spend is resolved from ``session_entries`` via
    the shared ``_sum_cost_by_project`` scan, independent of the just-recorded
    7d-% snapshot.

    Invariants preserved byte-for-byte with the global budget path: gate-first,
    pre-probe-before-the-cost-scan, ``rowcount==1`` race guard, set-then-dispatch.
    The cost source is ``_sum_cost_by_project`` (NOT ``_sum_cost_for_range``): it
    skips ``<synthetic>`` entries + buckets by canonical git-root, matching
    ``cmd_project`` and the per-project DISPLAY — so the firing path reconciles
    exactly with the displayed ``consumption_pct``.
    """
    # Gate FIRST (hot-path discipline): no per-project budget OR per-project
    # alerts off → zero overhead for non-users. `load_config()` is safe outside
    # any writer lock (atomic-rename). A malformed budget block is a quiet
    # warn-once no-op (mirrors maybe_record_budget_milestone).
    try:
        budget_cfg = _get_budget_config(load_config())
    except _BudgetConfigError as exc:
        _warn_budget_bad_config_once(exc)
        return
    projects = budget_cfg.get("projects") or {}
    if not projects or not budget_cfg.get("project_alerts_enabled"):
        return
    thresholds = budget_cfg["alert_thresholds"]
    if not thresholds:
        return

    now_utc = _command_as_of()
    pending_alerts: list[dict[str, Any]] = []
    conn = open_db()
    try:
        window = _resolve_current_budget_window(conn, now_utc)
        if window is None:
            return  # no resolvable week window yet
        week_start_at, _week_end_at = window
        week_key = week_start_at.isoformat(timespec="seconds")

        # Pre-probe (hot-path discipline + [Dedup mustn't gate side effects]):
        # which configured (project, threshold) pairs are STILL un-recorded for
        # this week? The cost scan is skipped ONLY when EVERY pair already has a
        # row — so a partial prior run (some-but-not-all pairs) still scans for
        # the remainder. The skip never owes a crossing: an un-recorded pair
        # always forces the scan.
        recorded = {
            (str(r[0]), int(r[1]))
            for r in conn.execute(
                "SELECT project_key, threshold "
                "FROM project_budget_milestones WHERE week_start_at = ?",
                (week_key,),
            )
        }
        sorted_thresholds = sorted(thresholds)
        pending = [
            (p, t)
            for p in projects
            for t in sorted_thresholds
            if (p, t) not in recorded
        ]
        if not pending:
            return  # nothing left to cross this week → skip the cost scan

        # Collision-aware labels via the shared primitive (#130) — byte-matching
        # the display table + dashboard chip for the same key feed. A
        # uniquely-named project keeps its bare basename in the notification;
        # only same-basename roots (`/work/app` + `/personal/app`) get the
        # `(parent)` segment ("app (work)" / "app (personal)"). Resolved LAZILY
        # (just-in-time on the first genuine crossing below): the map does
        # per-key git-root resolution but is consumed ONLY when a new crossing
        # dispatches, and most pending ticks scan without crossing — so we skip
        # the resolution entirely on the common no-dispatch tick. Same map,
        # same labels; only the timing moves.
        label_by_key = None

        # ONE grouped scan over the week's session entries, bucketed by
        # canonical git-root. skip_sync=False (self-sufficient): the global
        # budget axis only warms the cache when `budget.weekly_usd` is set, so a
        # project-only user (no global budget) reaches here with a cold cache on
        # a no-5h-anchor tick — sync here or a crossing fires a tick late. The
        # pre-probe above already gated this scan to the rare pending-crossing
        # tick, so the self-sufficient sync is near-free.
        by_proj = _sum_cost_by_project(
            week_start_at, now_utc, mode="auto", skip_sync=False
        )
        # Crossing arithmetic via the shared generator (#130). Feed ALL
        # configured (project, threshold) pairs; dispatch is gated SOLELY by
        # INSERT-OR-IGNORE rowcount==1 (genuine new crossing). The pending
        # pre-probe above stays as the scan-skip optimization, NOT a write gate
        # — already-recorded pairs get rowcount==0 here and silently skip
        # ([Dedup mustn't gate side effects]).
        for project_key, t, spent, target, consumption_pct in _project_crossings(
            projects.items(), sorted_thresholds, by_proj
        ):
            inserted = insert_project_budget_milestone(
                conn,
                week_start_at=week_key,
                project_key=project_key,
                threshold=t,
                budget_usd=target,
                spent_usd=spent,
                consumption_pct=consumption_pct,
                commit=False,
            )
            # Only the genuine-new-crossing winner (rowcount==1) dispatches; a
            # racing record-usage instance OR an already-recorded pair gets
            # rowcount==0 and skips.
            if inserted == 1:
                crossed_at = now_utc_iso()
                # set-then-dispatch: alerted_at lands on the row BEFORE the
                # Popen, sharing this transaction with the INSERT (commit=False).
                # `alerted_at IS NULL` is write-once defense-in-depth.
                conn.execute(
                    "UPDATE project_budget_milestones SET alerted_at = ? "
                    "WHERE week_start_at = ? AND project_key = ? "
                    "  AND threshold = ? AND alerted_at IS NULL",
                    (crossed_at, week_key, project_key, t),
                )
                # Collision-aware label (shared primitive, #130); resolved once
                # on the first dispatch and reused for the rest of this tick.
                # Kept defensive fallback (F4).
                if label_by_key is None:
                    label_by_key = _project_budget_labels(sorted(projects))
                project_label = label_by_key.get(
                    project_key, os.path.basename(project_key) or project_key
                )
                pending_alerts.append(_build_alert_payload_project_budget(
                    threshold=t,
                    crossed_at_utc=crossed_at,
                    week_start_at=week_key,
                    project=project_label,
                    project_key=project_key,
                    budget_usd=target,
                    spent_usd=spent,
                    consumption_pct=consumption_pct,
                ))
        # Single commit: every INSERT + its alerted_at marker durable together.
        conn.commit()
    except Exception as exc:
        eprint(
            f"[project-budget-milestone] error recording project budget "
            f"milestone: {exc}"
        )
    finally:
        conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant — one queue attempt per crossing, deduped on
    # the alerted_at column).
    for payload in pending_alerts:
        try:
            _dispatch_alert_notification(payload, mode="real")
        except Exception as dispatch_exc:
            eprint(f"[project-budget-alerts] dispatch failed: {dispatch_exc}")


def maybe_record_codex_budget_milestone(
    saved: dict[str, Any], *, raise_errors: bool = False,
) -> int:
    """Fire Codex budget alerts on ACTUAL-Codex-spend threshold crossings (axis
    ``codex_budget``, calendar-period-codex-budgets spec §6 — the gap the Codex
    spec review flagged: Codex usage never flows through ``record-usage``, so the
    Claude budget axes can't catch it). Thin vendor adapter over
    :func:`_record_budget_milestone_for_vendor` (#143): reads the ``budget.codex``
    config block, gates, resolves ``target`` / ``thresholds`` / ``period``, and
    passes the Codex payload builder.

    Called from ``cmd_record_usage`` alongside the weekly-% / 5h-% / budget /
    project-budget milestone helpers AND opportunistically from ``cmd_budget``
    (the public name is kept so that call site is unchanged). Forward-only /
    fire-once, so the double-trigger never double-fires. Gated, hot-path-cheap,
    set-then-dispatch. Errors are logged, not raised (the caller also wraps).

    Unlike the Claude budget axis, Codex has NO subscription week: the period
    window is resolved purely from ``now`` + the configured calendar period
    (calendar-week / calendar-month) — it NEVER touches
    ``weekly_usage_snapshots`` (the shared core's ``_resolve_budget_window``
    dispatches to the pure calendar window for ``vendor='codex'``).

    ``saved`` is accepted for call-site symmetry with the sibling helpers but is
    unused: Codex spend is resolved from the cache DB independent of the
    just-recorded 7d-% snapshot.
    """
    # Gate FIRST (hot-path discipline): no Codex budget OR alerts off → zero
    # overhead for non-Codex-budget users. `load_config()` is safe outside any
    # writer lock (atomic-rename). A malformed budget block is a quiet warn-once
    # no-op (mirrors maybe_record_budget_milestone). One config read services
    # both the gate and the calendar-window tz resolution.
    config = load_config()
    try:
        budget_cfg = _get_budget_config(config)
    except _BudgetConfigError as exc:
        _warn_budget_bad_config_once(exc)
        return 0
    codex_cfg = budget_cfg.get("codex")
    if not codex_cfg or not codex_cfg.get("alerts_enabled"):
        return 0
    target = codex_cfg.get("amount_usd")
    thresholds = codex_cfg.get("alert_thresholds") or []
    if target is None or not thresholds:
        return 0
    tz = resolve_display_tz(argparse.Namespace(tz=None), config)
    return _record_budget_milestone_for_vendor(
        vendor="codex",
        target=target,
        thresholds=thresholds,
        period=codex_cfg["period"],
        config=config,
        tz=tz,
        # The Codex payload builder takes `period_start_at=` directly (== the
        # resolved period-start instant, == period_key), so the at-fire dispatch
        # id stays byte-stable `codex_budget:<period_start_at>:<threshold>`.
        build_payload=lambda **kw: _build_alert_payload_codex_budget(
            threshold=kw["threshold"],
            crossed_at_utc=kw["crossed_at_utc"],
            period_start_at=kw["period_key"],
            period=kw["period"],
            budget_usd=kw["budget_usd"],
            spent_usd=kw["spent_usd"],
            consumption_pct=kw["consumption_pct"],
        ),
        raise_errors=raise_errors,
    )


def _weekly_pct_week_avg_projection(conn, now_utc):
    """Compute the week-AVERAGE weekly-% projection for the current
    subscription week, snapshot-only (CHEAP — no cost SUM, no ``sync_cache``).

    Returns ``(projected_pct, low_conf)`` or ``None`` when no current-week
    snapshot resolves. The value is computed by the IDENTICAL formula +
    IDENTICAL inputs that produce ``ForecastOutput.week_avg_projection_pct``
    (``_load_forecast_inputs`` → ``_compute_forecast``): ``p_now`` / elapsed /
    remaining come from ``_fetch_current_week_snapshots`` +
    ``_apply_midweek_reset_override`` (the same reset-aware window resolution
    forecast uses), ``r_avg = p_now / elapsed_hours`` (``p_week_start`` treated
    as 0, matching the forecast kernel), and
    ``projected = p_now + r_avg * remaining_hours``. The reconcile invariant
    binds the fired value to forecast's ``week_avg_projection_pct`` within
    1e-9, so the two MUST share the formula by construction.

    LOW CONF mirrors the displayed forecast confidence: the binary
    ``_assess_forecast_confidence(elapsed_hours, p_now, len(samples))`` plus the
    ``no_sample_ge_24h`` clause ``_load_forecast_inputs`` appends — so a thin
    early-week window that forecast renders ``LOW CONF`` never fires a
    projected alert.

    Deliberately does NOT call ``_sum_cost_for_range`` (the weekly-% projection
    needs no spend; the forecast kernel's ``week_avg_projection_pct`` is also
    spend-free).
    """
    fetched = _fetch_current_week_snapshots(conn, now_utc)
    if fetched is None:
        return None
    week_start_at, week_end_at, samples = fetched
    week_start_at, samples = _apply_midweek_reset_override(
        conn, week_start_at, week_end_at, samples
    )
    if not samples:
        return None
    p_now = samples[-1][1]
    elapsed_hours = (now_utc - week_start_at).total_seconds() / 3600.0
    remaining_hours = max(0.0, (week_end_at - now_utc).total_seconds() / 3600.0)
    r_avg = p_now / elapsed_hours if elapsed_hours > 0 else 0.0
    projected_pct = p_now + r_avg * remaining_hours

    # Confidence: same predicate + the same no_sample_ge_24h augmentation that
    # _load_forecast_inputs applies, so this LOW CONF gate == forecast's.
    confidence, _reasons = _assess_forecast_confidence(
        elapsed_hours, p_now, len(samples)
    )
    target_24h = now_utc - dt.timedelta(hours=24)
    has_sample_ge_24h = any(s[0] <= target_24h for s in samples)
    if not has_sample_ge_24h:
        confidence = "low"
    return (projected_pct, confidence == "low")


def maybe_record_projected_alert(
    saved: dict[str, Any], *, only_metrics=None
) -> None:
    """Projected-pace detect-and-arm (axis ``projected``, #121 / #135).

    Fires on the WEEK-AVERAGE projection (never the displayed high-end verdict
    band) for ``weekly_pct``, ``budget_usd`` (any Claude period — #135) and/or
    ``codex_budget_usd`` (#135). Its OWN detect-and-arm — NOT folded into
    ``maybe_record_milestone`` (Section 1 / Codex P0-3) — called from
    ``cmd_record_usage`` in its own ``try`` after the weekly/5h/budget blocks.

    Master gates (Codex P1-2): ``weekly_pct`` fires only under
    ``alerts.enabled && alerts.projected_enabled``; ``budget_usd`` only under
    ``_budget_alerts_active(budget_cfg) && budget.projected_enabled`` (#135:
    ALL Claude periods, not just subscription-week); ``codex_budget_usd`` only
    under ``codex.alerts_enabled && codex.projected_enabled`` with a set
    ``amount_usd`` + ``alert_thresholds`` (mirrors
    ``maybe_record_codex_budget_milestone``'s gate — there is no
    ``_codex_budget_alerts_active`` helper). All toggles default OFF (no
    surprise notifications on upgrade). When NONE is on, returns after only a
    cheap config read — no projection math, no cost work.

    ``only_metrics`` (#135): when a set of metric names is passed (the
    opportunistic ``cctally budget`` fire passes ``{"codex_budget_usd"}``), only
    those legs run — so that interactive fire never pops a ``weekly_pct`` /
    Claude-``budget_usd`` notification. ``None`` (the record path) = every
    enabled leg.

    Pre-probe (Codex P1-1): a metric whose levels are ALL already latched is
    skipped BEFORE any projection / cost work.

    Snap-up (Codex P2-1): a level fires when ``projected + 1e-9 >= threshold``.
    Latch / fire-once: ``UNIQUE(week_start_at, period, metric, threshold)`` + the
    rowcount==1 predicate — a later recovery neither un-fires nor re-fires.
    Mid-week reset re-anchors ``week_start_at`` (budget pattern; no
    ``reset_event_id``).

    Set-then-dispatch: INSERT ``commit=False``, stamp ``alerted_at`` in the same
    txn, commit, THEN best-effort dispatch. A dispatch failure never rolls back
    the milestone.

    Both budget legs reuse the SAME ``_build_vendor_budget_inputs`` +
    ``compute_budget_status`` path that produces ``budget --json``'s
    ``week_avg_projection_usd`` (the reconcile-bound field) — value-exact by
    construction, keyed on the calendar/subscription period-start instant in the
    back-compat ``week_start_at`` column. The Claude leg passes ``skip_sync=True``
    (the cache is warmed by the actual-budget axis's spend SUM this same tick);
    the Codex leg passes ``skip_sync=False`` (R5: Codex has no other record-path
    warmer — ``maybe_record_codex_budget_milestone`` short-circuits before its SUM
    when all actual levels are latched, so a ``skip_sync=True`` Codex leg could
    read a cold cache and under-count; the delta-sync is a near-no-op when warm).
    The pre-probe skips each leg entirely when all its levels are already latched.
    """
    # The `projected_enabled` toggles are validated keys on the alerts/budget
    # blocks (bool-validated; default OFF), so read them straight off the
    # validated getter dicts — no raw-block fallback (which would re-emit the
    # "unknown alerts config key" warning every tick and bypass bool
    # validation). Master gates still compose with the parent-axis predicates.
    cfg = load_config()
    try:
        alerts_cfg = _get_alerts_config(cfg)
    except _AlertsConfigError as exc:
        _warn_alerts_bad_config_once(exc)
        alerts_cfg = {"enabled": False, "projected_enabled": False}
    try:
        budget_cfg = _get_budget_config(cfg)
    except _BudgetConfigError as exc:
        _warn_budget_bad_config_once(exc)
        budget_cfg = {}

    weekly_on = bool(alerts_cfg.get("enabled")) and bool(
        alerts_cfg.get("projected_enabled")
    )
    # #135: the Claude `budget_usd` leg now fires for ANY period (calendar-week /
    # calendar-month / subscription-week). `_build_vendor_budget_inputs` resolves
    # the correct window per period, and the milestone keys on that period-start
    # instant (in the back-compat `week_start_at` column) — the same key the
    # actual-budget axis uses — so there is no window/key mismatch any more.
    budget_on = _budget_alerts_active(budget_cfg) and bool(
        budget_cfg.get("projected_enabled")
    )
    # #135: the Codex `codex_budget_usd` leg. No `_codex_budget_alerts_active`
    # helper exists, so inline the gate mirroring
    # `maybe_record_codex_budget_milestone`: a Codex budget block with alerts +
    # projected on and a set amount/thresholds. (Projected requires
    # `alerts_enabled` too — same as the Claude leg, where `_budget_alerts_active`
    # requires it — documented in budget.md, not UI-enforced.)
    codex_cfg = budget_cfg.get("codex") or {}
    codex_on = (
        bool(codex_cfg)
        and bool(codex_cfg.get("alerts_enabled"))
        and bool(codex_cfg.get("projected_enabled"))
        and codex_cfg.get("amount_usd") is not None
        and bool(codex_cfg.get("alert_thresholds"))
    )
    # only_metrics scopes the opportunistic `cctally budget` fire to the Codex
    # leg so it never pops a weekly_pct / Claude budget_usd notification.
    if only_metrics is not None:
        weekly_on = weekly_on and "weekly_pct" in only_metrics
        budget_on = budget_on and "budget_usd" in only_metrics
        codex_on = codex_on and "codex_budget_usd" in only_metrics
    if not (weekly_on or budget_on or codex_on):
        return  # cheap config-only path — non-projected users pay nothing

    # Both budget legs resolve their window via _build_vendor_budget_inputs in
    # CONFIG tz (Namespace(tz=None)) — like maybe_record_codex_budget_milestone
    # — so a `cctally budget --tz X` opportunistic fire near a period boundary
    # resolves the SAME period_start_at dedup key as the record path and never
    # forks / double-fires.
    config_tz = resolve_display_tz(argparse.Namespace(tz=None), cfg)

    now_utc = _command_as_of()
    pending: list[dict[str, Any]] = []
    conn = open_db()
    try:
        # ── weekly_pct leg (snapshot-only, cheap) ───────────────────────────
        if weekly_on:
            w_window = _fetch_current_week_snapshots(conn, now_utc)
            if w_window is not None:
                ws_at, we_at, samples = w_window
                ws_at, _ = _apply_midweek_reset_override(
                    conn, ws_at, we_at, samples
                )
                week_key = ws_at.isoformat(timespec="seconds")
                levels = (90, 100)
                # weekly_pct is the Anthropic subscription week (#137).
                if not _projected_levels_already_latched(
                    conn, week_start_at=week_key, period="subscription-week",
                    metric="weekly_pct", levels=levels,
                ):
                    proj = _weekly_pct_week_avg_projection(conn, now_utc)
                    if proj is not None and not proj[1]:
                        value = proj[0]
                        # weekly_pct comparand == raw threshold (denominator 100).
                        for t in projected_crossings(
                            value, [(t, float(t)) for t in levels]
                        ):
                            pending.append(dict(
                                week_start_at=week_key,
                                period="subscription-week",
                                metric="weekly_pct",
                                threshold=t,
                                projected_value=value,
                                denominator=100.0,
                            ))

        # ── budget_usd leg (any Claude period — #135; shared factory) ────────
        if budget_on:
            target = budget_cfg["weekly_usd"]
            thresholds = tuple(
                sorted(set(int(t) for t in budget_cfg["alert_thresholds"]))
            )
            claude_period = budget_cfg.get("period", "subscription-week")
            # Resolve the window key CHEAPLY first (SUM-free, same resolver the
            # actual-budget axis uses) so the pre-probe can short-circuit BEFORE
            # _build_vendor_budget_inputs runs any cost SUM / cache sync — the
            # pre-probe-runs-first contract (spec §3.4; mirrors the actual axis).
            window = _resolve_claude_budget_window(
                conn, now_utc, period=claude_period, config=cfg, tz=config_tz
            )
            if window is not None and thresholds:
                b_ws_at, _b_we_at = window
                b_week_key = b_ws_at.isoformat(timespec="seconds")
                if not _projected_levels_already_latched(
                    conn, week_start_at=b_week_key, period=claude_period,
                    metric="budget_usd", levels=thresholds,
                ):
                    # skip_sync=True: the actual-budget axis already ran a
                    # _sum_cost_for_range this same tick, warming the cache.
                    inputs = _build_vendor_budget_inputs(
                        vendor="claude", period=claude_period, target_usd=target,
                        alert_thresholds=thresholds, now_utc=now_utc, config=cfg,
                        tz=config_tz, skip_sync=True,
                    )
                    if inputs is not None:
                        status = compute_budget_status(inputs)
                        if not status.low_confidence:
                            value = status.week_avg_projection_usd
                            # budget comparand == (t/100)*target (glue pre-scales).
                            for t in projected_crossings(
                                value,
                                [(t, (t / 100.0) * float(target)) for t in thresholds],
                            ):
                                pending.append(dict(
                                    week_start_at=b_week_key,
                                    period=claude_period,
                                    metric="budget_usd",
                                    threshold=t,
                                    projected_value=value,
                                    denominator=float(target),
                                ))

        # ── codex_budget_usd leg (#135; skip_sync=False — R5) ────────────────
        if codex_on:
            c_target = codex_cfg["amount_usd"]
            c_thresholds = tuple(
                sorted(set(int(t) for t in codex_cfg["alert_thresholds"]))
            )
            c_period = codex_cfg["period"]
            # Cheap, SUM-free window key first (pure calendar resolution), so the
            # pre-probe short-circuits BEFORE any Codex cache sync / cost SUM —
            # spec §3.4 (pre-probe runs FIRST).
            c_window = _resolve_codex_budget_period_window(
                c_period, now_utc, cfg, config_tz
            )
            if c_window is not None and c_thresholds:
                c_ws_at, _c_we_at = c_window
                c_week_key = c_ws_at.isoformat(timespec="seconds")
                if not _projected_levels_already_latched(
                    conn, week_start_at=c_week_key, period=c_period,
                    metric="codex_budget_usd", levels=c_thresholds,
                ):
                    # skip_sync=False (R5): Codex has no other record-path cache
                    # warmer (maybe_record_codex_budget_milestone short-circuits
                    # before its SUM when all actual levels are latched), so a
                    # skip_sync=True leg could read a cold cache and under-count.
                    # The pre-probe above already gated this, so a sync only runs
                    # when a cross is genuinely owed; it's a near-no-op when warm.
                    c_inputs = _build_vendor_budget_inputs(
                        vendor="codex", period=c_period, target_usd=c_target,
                        alert_thresholds=c_thresholds, now_utc=now_utc,
                        config=cfg, tz=config_tz, skip_sync=False,
                    )
                    if c_inputs is not None:
                        c_status = compute_budget_status(c_inputs)
                        if not c_status.low_confidence:
                            value = c_status.week_avg_projection_usd
                            # codex comparand == (t/100)*target (glue pre-scales).
                            for t in projected_crossings(
                                value,
                                [(t, (t / 100.0) * float(c_target)) for t in c_thresholds],
                            ):
                                pending.append(dict(
                                    week_start_at=c_week_key,
                                    period=c_period,
                                    metric="codex_budget_usd",
                                    threshold=t,
                                    projected_value=value,
                                    denominator=float(c_target),
                                ))

        # ── arm (set-then-dispatch): INSERT + stamp alerted_at in one txn ────
        fired: list[dict[str, Any]] = []
        for p in pending:
            inserted = insert_projected_milestone(
                conn,
                week_start_at=p["week_start_at"],
                period=p["period"],
                metric=p["metric"],
                threshold=p["threshold"],
                projected_value=p["projected_value"],
                denominator=p["denominator"],
                commit=False,
            )
            # Only the genuine-new-crossing winner (rowcount==1) arms+dispatches;
            # a racing record-usage instance gets rowcount==0 and skips. The
            # alerted_at UPDATE keys on the CONCRETE `period` (#137).
            if inserted == 1:
                conn.execute(
                    "UPDATE projected_milestones SET alerted_at = ? "
                    "WHERE week_start_at = ? AND period = ? AND metric = ? "
                    "  AND threshold = ? AND alerted_at IS NULL",
                    (now_utc_iso(), p["week_start_at"], p["period"],
                     p["metric"], p["threshold"]),
                )
                fired.append(p)
        # Single commit: every INSERT + its alerted_at marker durable together.
        conn.commit()
    except Exception as exc:
        eprint(f"[projected-alert] error recording projected milestone: {exc}")
        fired = []
    finally:
        conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant).
    for p in fired:
        try:
            payload = _build_alert_payload_projected(
                metric=p["metric"],
                threshold=p["threshold"],
                projected_value=p["projected_value"],
                denominator=p["denominator"],
                week_start_at=p["week_start_at"],
            )
            _dispatch_alert_notification(payload, mode="real")
        except Exception as dispatch_exc:
            eprint(f"[projected-alert] dispatch failed: {dispatch_exc}")


def _compute_block_totals(
    block_start_at: dt.datetime,
    range_end: dt.datetime,
    *,
    skip_sync: bool = False,
) -> dict[str, Any]:
    """Sum tokens + cost over [block_start_at, range_end] from session_entries,
    plus per-model and per-project breakdowns in the same walk.

    Used by the live write path (maybe_update_five_hour_block) and the
    historical backfill (_backfill_five_hour_blocks /
    _backfill_five_hour_block_models / _backfill_five_hour_block_projects).

    Routes through get_claude_session_entries (rather than the parent
    get_entries which returns UsageEntry without project_path) — same
    cache-first / lock-contention / direct-JSONL fallback chain.

    Returns a dict with:
      input_tokens, output_tokens, cache_create_tokens, cache_read_tokens (int)
      cost_usd (float)
      by_model: dict[model_name -> {input_tokens, output_tokens,
                                     cache_create_tokens, cache_read_tokens,
                                     cost_usd, entry_count}]
      by_project: dict[project_path_or_'(unknown)' -> same shape]
    """
    totals: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_create_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "by_model": {},
        "by_project": {},
    }
    for entry in get_claude_session_entries(
        block_start_at, range_end, skip_sync=skip_sync,
    ):
        usage = {
            "input_tokens":                entry.input_tokens,
            "output_tokens":               entry.output_tokens,
            "cache_creation_input_tokens": entry.cache_creation_tokens,
            "cache_read_input_tokens":     entry.cache_read_tokens,
        }
        cost = _calculate_entry_cost(
            entry.model, usage, mode="auto", cost_usd=entry.cost_usd,
        )

        totals["input_tokens"]        += entry.input_tokens
        totals["output_tokens"]       += entry.output_tokens
        totals["cache_create_tokens"] += entry.cache_creation_tokens
        totals["cache_read_tokens"]   += entry.cache_read_tokens
        totals["cost_usd"]            += cost

        # Bucket by model and by project_path. NULL project_path → sentinel
        # so reconcile invariant SUM(child.cost) == parent.total holds.
        # Note: the JSONL-fallback path (_direct_parse_claude_session_entries)
        # always populates project_path = cwd (never NULL); '(unknown)' only
        # appears on the cache-backed path during the brief session_files
        # lazy-backfill window.
        for key, bucket_dict in (
            (entry.model, totals["by_model"]),
            (entry.project_path or "(unknown)", totals["by_project"]),
        ):
            b = bucket_dict.setdefault(
                key,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_create_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0.0,
                    "entry_count": 0,
                },
            )
            b["input_tokens"]        += entry.input_tokens
            b["output_tokens"]       += entry.output_tokens
            b["cache_create_tokens"] += entry.cache_creation_tokens
            b["cache_read_tokens"]   += entry.cache_read_tokens
            b["cost_usd"]            += cost
            b["entry_count"]         += 1
    return totals


def maybe_update_five_hour_block(saved: dict[str, Any]) -> None:
    """Upsert the current 5h block in five_hour_blocks; close strictly
    older open blocks; sweep naturally-expired blocks; flag blocks
    spanning a recorded mid-week 7d-reset.

    Errors are logged and swallowed — record-usage must not regress
    because of this helper, same posture as maybe_record_milestone.
    """
    five_hour_percent = saved.get("fiveHourPercent")
    five_hour_resets_at = saved.get("fiveHourResetsAt")
    five_hour_window_key = saved.get("fiveHourWindowKey")
    if (
        five_hour_percent is None
        or five_hour_resets_at is None
        or five_hour_window_key is None
    ):
        return  # no canonical 5h anchor — nothing to record

    captured_at = saved["capturedAt"]
    weekly_percent = saved.get("weeklyPercent")
    snapshot_id = saved["id"]

    # Note: this is the 4th open_db() invocation per record-usage call
    # (after cmd_record_usage's prior-state read, insert_usage_snapshot,
    # and maybe_record_milestone). Each open re-runs the inline schema
    # migrations and the empty-table check that gates _backfill_five_hour_blocks.
    # The backfill itself only runs once per process (the gate fires only when
    # five_hour_blocks is empty), so the cost is benign — but the count is
    # surprising. If any future helper grows expensive open_db() side effects,
    # consolidate by passing the connection through rather than reopening.
    conn = open_db()
    try:
        # Step 3 (per spec §3.2): read prior state including immutable
        # fields we'll re-use. Re-deriving block_start_at from saved.
        # fiveHourResetsAt would reintroduce the seconds-level Anthropic
        # ISO jitter that five_hour_window_key was designed to collapse.
        prior = conn.execute(
            """
            SELECT id              AS prior_block_id,
                   block_start_at  AS block_start_at
              FROM five_hour_blocks
             WHERE five_hour_window_key = ?
            """,
            (int(five_hour_window_key),),
        ).fetchone()

        if prior is None:
            # First observation of this window. Compute block_start_at
            # from the canonical resets timestamp.
            try:
                resets_dt = parse_iso_datetime(
                    five_hour_resets_at, "five_hour_resets_at",
                )
            except ValueError as exc:
                eprint(f"[5h-block] bad resets_at, skipping: {exc}")
                return
            block_start_dt = resets_dt - dt.timedelta(hours=5)
            block_start_at = block_start_dt.isoformat(timespec="seconds")
        else:
            block_start_at = prior["block_start_at"]
            block_start_dt = parse_iso_datetime(
                block_start_at, "five_hour_blocks.block_start_at",
            )

        # Step 6 (totals) — done outside the transaction so the
        # cache.db read doesn't hold the stats.db write lock open.
        captured_at_dt = parse_iso_datetime(captured_at, "capturedAt")
        totals = _compute_block_totals(block_start_dt, captured_at_dt)

        # Hoist alerts config above BEGIN (M1 + M2): single read serves
        # all per-pct iterations in the catch-up case, AND keeps the
        # filesystem read out of the transaction window so the stats.db
        # write lock isn't held across config.json I/O.
        # `load_config()` is safe outside the writer lock — atomic-rename
        # guarantees readers see whole bytes (CLAUDE.md gotcha).
        # `_ALERTS_BAD_CONFIG_WARNED` (module-level, M3) rate-limits the
        # warning to once per process; both axis paths share the flag since
        # the underlying problem is config-wide, not axis-specific.
        cfg_for_alerts = load_config()
        try:
            alerts_cfg: "dict | None" = _get_alerts_config(cfg_for_alerts)
        except _AlertsConfigError as exc:
            _warn_alerts_bad_config_once(exc)
            alerts_cfg = None
        # Resolve display.tz once (shares the cfg load above). Threaded
        # into _dispatch_alert_notification so the macOS notification
        # subtitle (block-start time) matches the dashboard / TUI render
        # rather than falling back to host-local via tz=None.
        display_tz_for_alerts = _resolve_display_tz_obj(cfg_for_alerts)

        # Collect dispatch jobs while inside BEGIN (set-then-dispatch:
        # alerted_at UPDATE stays inside the transaction per spec §3.2)
        # but DEFER `_dispatch_alert_notification` until AFTER the outer
        # commit (I1: prevents the inner Popen-time conn.commit() from
        # ending the surrounding BEGIN mid-sequence and breaking the
        # close-older + upsert + cross-flag atomicity envelope).
        pending_alerts: list[dict[str, Any]] = []

        # Steps 4-5 + 7: transaction wraps close-older + upsert so a
        # mid-sequence failure doesn't leave the prior block closed
        # without the current block opened/updated.
        now_iso = now_utc_iso()
        # BEGIN IMMEDIATE (not deferred): the first DML below is a write (the
        # close-older UPDATE), so this transaction already takes the write lock
        # up front today. Stating IMMEDIATE makes that the explicit contract —
        # a future edit that slips a SELECT before the first write here cannot
        # silently reintroduce a SQLITE_BUSY_SNAPSHOT crash (busy_timeout does
        # not absorb that). See cctally-dev#87.
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Step 5: close any STRICTLY OLDER open block. `<` not `!=`
            # — record-usage runs in parallel via background hook-tick &
            # detach + status-line ticks; an older invocation completing
            # after a newer one would close the now-current block under
            # `!=`. With `<`, an older invocation only closes still-older
            # blocks. window_key is a 10-min-floored monotonic epoch.
            conn.execute(
                """
                UPDATE five_hour_blocks
                   SET is_closed = 1, last_updated_at_utc = ?
                 WHERE is_closed = 0
                   AND five_hour_window_key < ?
                """,
                (now_iso, int(five_hour_window_key)),
            )

            # Step 5b: natural-expiration sweep. The close-older predicate
            # above only fires when a strictly-newer window arrives. A user
            # who lets a block expire without a successor (idle / shut down
            # past the 5h reset) would otherwise leave the row at
            # is_closed = 0 forever. Idempotent (only flips 0 → 1); safe to
            # re-run every tick. ISO-string compare is monotonic so it
            # works directly on five_hour_resets_at.
            conn.execute(
                """
                UPDATE five_hour_blocks
                   SET is_closed = 1, last_updated_at_utc = ?
                 WHERE is_closed = 0
                   AND five_hour_resets_at < ?
                """,
                (now_iso, now_iso),
            )

            # Step 7: atomic upsert. Single statement collapses the
            # insert-vs-update branches and is race-safe: when two
            # record-usage invocations both observe `prior is None`
            # for a brand-new window (the SELECT at line 8636 happens
            # before BEGIN), the loser's INSERT lands as DO UPDATE
            # rather than raising IntegrityError on the
            # UNIQUE(five_hour_window_key) constraint and dropping the
            # tick. Immutable columns (block_start_at,
            # first_observed_at_utc, five_hour_resets_at,
            # seven_day_pct_at_block_start, created_at_utc) are
            # deliberately omitted from DO UPDATE — first writer
            # owns them.
            conn.execute(
                """
                INSERT INTO five_hour_blocks (
                  five_hour_window_key,
                  five_hour_resets_at,
                  block_start_at,
                  first_observed_at_utc,
                  last_observed_at_utc,
                  final_five_hour_percent,
                  seven_day_pct_at_block_start,
                  seven_day_pct_at_block_end,
                  crossed_seven_day_reset,
                  total_input_tokens,
                  total_output_tokens,
                  total_cache_create_tokens,
                  total_cache_read_tokens,
                  total_cost_usd,
                  is_closed,
                  created_at_utc,
                  last_updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(five_hour_window_key) DO UPDATE SET
                  last_observed_at_utc       = excluded.last_observed_at_utc,
                  final_five_hour_percent    = excluded.final_five_hour_percent,
                  seven_day_pct_at_block_end = excluded.seven_day_pct_at_block_end,
                  total_input_tokens         = excluded.total_input_tokens,
                  total_output_tokens        = excluded.total_output_tokens,
                  total_cache_create_tokens  = excluded.total_cache_create_tokens,
                  total_cache_read_tokens    = excluded.total_cache_read_tokens,
                  total_cost_usd             = excluded.total_cost_usd,
                  last_updated_at_utc        = excluded.last_updated_at_utc
                """,
                (
                    int(five_hour_window_key),
                    str(five_hour_resets_at),
                    block_start_at,
                    captured_at,
                    captured_at,
                    float(five_hour_percent),
                    weekly_percent,
                    weekly_percent,
                    totals["input_tokens"],
                    totals["output_tokens"],
                    totals["cache_create_tokens"],
                    totals["cache_read_tokens"],
                    totals["cost_usd"],
                    now_iso,
                    now_iso,
                ),
            )

            # ── Resolve current block_id once for reuse by the per-(block, model)
            # / per-(block, project) child writes below AND the existing milestone
            # detection (which previously did its own SELECT — drop that SELECT in
            # favor of this variable).
            block_id_row = conn.execute(
                "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?",
                (int(five_hour_window_key),),
            ).fetchone()
            block_id = int(block_id_row["id"])

            # ── Replace-all per-tick: per-(block, model) and per-(block, project_path)
            # rollup-children. DELETE keyed on five_hour_window_key (NOT block_id) so
            # orphan child rows from a prior parent rebuild are cleaned up automatically.
            # Same transaction as the parent upsert; if these raise, the whole tick
            # rolls back and the next tick recomputes from scratch.
            conn.execute(
                "DELETE FROM five_hour_block_models WHERE five_hour_window_key = ?",
                (int(five_hour_window_key),),
            )
            if totals.get("by_model"):
                conn.executemany(
                    """
                    INSERT INTO five_hour_block_models (
                      block_id, five_hour_window_key, model,
                      input_tokens, output_tokens,
                      cache_create_tokens, cache_read_tokens,
                      cost_usd, entry_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            block_id,
                            int(five_hour_window_key),
                            model,
                            b["input_tokens"],
                            b["output_tokens"],
                            b["cache_create_tokens"],
                            b["cache_read_tokens"],
                            b["cost_usd"],
                            b["entry_count"],
                        )
                        for model, b in totals["by_model"].items()
                    ],
                )

            conn.execute(
                "DELETE FROM five_hour_block_projects WHERE five_hour_window_key = ?",
                (int(five_hour_window_key),),
            )
            if totals.get("by_project"):
                conn.executemany(
                    """
                    INSERT INTO five_hour_block_projects (
                      block_id, five_hour_window_key, project_path,
                      input_tokens, output_tokens,
                      cache_create_tokens, cache_read_tokens,
                      cost_usd, entry_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            block_id,
                            int(five_hour_window_key),
                            project_path,
                            b["input_tokens"],
                            b["output_tokens"],
                            b["cache_create_tokens"],
                            b["cache_read_tokens"],
                            b["cost_usd"],
                            b["entry_count"],
                        )
                        for project_path, b in totals["by_project"].items()
                    ],
                )

            # ── 5h-% milestone detection (mirrors maybe_record_milestone) ──
            # Snap-up-by-1e-9 per the gotcha: 0.50 * 100 == 49.99...9 in
            # IEEE-754, so bare math.floor would miss the 50 threshold.
            current_floor = math.floor(float(five_hour_percent) + 1e-9)

            # Resolve active segment ONCE so every per-site read + write
            # below sees the same value within this transaction. Spec
            # §3.3 & §3.4 of
            # docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md:
            # the active segment is the latest five_hour_reset_events row
            # for this window_key, else sentinel 0 (pre-credit).
            active_reset_event_id = _resolve_active_five_hour_reset_event_id(
                conn, int(five_hour_window_key)
            )

            if current_floor >= 1:
                # Site A — MAX(percent_threshold) scoped to active segment.
                # Without the reset_event_id filter, MAX returns the
                # pre-credit max and post-credit milestones from 1..max
                # are silently never emitted.
                #
                # Use max(percent_threshold) directly (not prior block's
                # final_pct) so first-observation already-mid-stream doesn't
                # synthesize crossings 1..(current_floor - 1) we never had
                # authentic moment-of-detection data for. Same shape as
                # maybe_record_milestone's max_existing path.
                row = conn.execute(
                    "SELECT MAX(percent_threshold) AS m FROM five_hour_milestones "
                    "WHERE five_hour_window_key = ? AND reset_event_id = ?",
                    (int(five_hour_window_key), active_reset_event_id),
                ).fetchone()
                max_existing = row["m"] if row and row["m"] is not None else None

                # Which integer 5h-% thresholds to attempt: the pure fencing
                # decision (floor snap + first-obs / resume-above-max rule).
                # `milestone_range.start` is the start_threshold used for the
                # marginal-cost check below; a non-empty range is exactly the
                # old `start_threshold <= current_floor` guard.
                milestone_range = five_hour_milestone_range(
                    float(five_hour_percent), max_existing
                )
                start_threshold = milestone_range.start

                if milestone_range:
                    # block_id was resolved above (before the children writes) and
                    # is still in scope here.

                    # Site B — prior-cost lookup scoped to active segment.
                    # Marginal-cost lookup for the start_threshold milestone
                    # (only when there's a prior milestone in this block).
                    # Without the reset_event_id filter, marginal could be
                    # computed against a pre-credit row whose block_cost is
                    # unrelated to the post-credit segment's totals.
                    prior_cost: float | None = None
                    if max_existing is not None:
                        prev_row = conn.execute(
                            "SELECT block_cost_usd FROM five_hour_milestones "
                            "WHERE five_hour_window_key = ? "
                            "  AND percent_threshold = ? "
                            "  AND reset_event_id = ?",
                            (int(five_hour_window_key), int(max_existing),
                             active_reset_event_id),
                        ).fetchone()
                        if prev_row is not None:
                            prior_cost = float(prev_row["block_cost_usd"])

                    for pct in milestone_range:
                        if pct == start_threshold and prior_cost is not None:
                            marginal: float | None = totals["cost_usd"] - prior_cost
                        else:
                            marginal = None
                        # Site C — INSERT stamps the resolved
                        # ``active_reset_event_id`` (0 = pre-credit, else
                        # the latest five_hour_reset_events.id). UNIQUE
                        # is now (window_key, threshold, reset_event_id)
                        # so post-credit threshold crossings re-fire
                        # fresh — not absorbed into the pre-credit row.
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO five_hour_milestones (
                              block_id,
                              five_hour_window_key,
                              percent_threshold,
                              captured_at_utc,
                              usage_snapshot_id,
                              block_input_tokens,
                              block_output_tokens,
                              block_cache_create_tokens,
                              block_cache_read_tokens,
                              block_cost_usd,
                              marginal_cost_usd,
                              seven_day_pct_at_crossing,
                              reset_event_id
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                block_id,
                                int(five_hour_window_key),
                                int(pct),
                                captured_at,
                                int(snapshot_id),
                                totals["input_tokens"],
                                totals["output_tokens"],
                                totals["cache_create_tokens"],
                                totals["cache_read_tokens"],
                                totals["cost_usd"],
                                marginal,
                                weekly_percent,
                                active_reset_event_id,
                            ),
                        )
                        # ── Threshold-actions dispatch (set-then-dispatch, spec §3.2) ──
                        # Only the genuine-new-crossing winner (rowcount==1)
                        # reaches dispatch. Concurrent record-usage instances
                        # that race on the same (five_hour_window_key,
                        # percent_threshold) get rowcount==0 from the
                        # INSERT OR IGNORE and skip dispatch entirely.
                        # `alerted_at IS NULL` on the UPDATE preserves
                        # write-once even if two writers somehow both think
                        # they won.
                        #
                        # I1: alerted_at UPDATE stays inside BEGIN (set-then-
                        # dispatch invariant per spec §3.2 — the row carries
                        # alerted_at BEFORE any externally-observable side
                        # effect). The single outer commit at the bottom of
                        # this BEGIN durably writes the milestone row AND the
                        # alerted_at update together. Dispatch itself is
                        # collected into pending_alerts and fired AFTER the
                        # outer commit so the inner Popen-time bookkeeping
                        # never ends the surrounding BEGIN mid-sequence.
                        if (
                            cur.rowcount == 1
                            and alerts_cfg is not None
                            and alerts_cfg["enabled"]
                            and pct in alerts_cfg["five_hour_thresholds"]
                        ):
                            crossed_at = now_utc_iso()
                            # Site D — alerted_at UPDATE scoped to the
                            # active segment, so the post-credit row
                            # gets stamped without overwriting an
                            # already-alerted pre-credit row at the
                            # same threshold.
                            conn.execute(
                                "UPDATE five_hour_milestones SET alerted_at = ? "
                                "WHERE five_hour_window_key = ? "
                                "  AND percent_threshold = ? "
                                "  AND reset_event_id = ? "
                                "  AND alerted_at IS NULL",
                                (crossed_at, int(five_hour_window_key),
                                 int(pct), active_reset_event_id),
                            )
                            # Cheap re-reads inside BEGIN are SELECT-only and
                            # safe; values reflect post-INSERT state. We
                            # build the payload now (while block_id / totals
                            # are in scope) and defer ONLY the Popen-side
                            # _dispatch_alert_notification to after the outer
                            # commit.
                            # Site E — alert-payload reread scoped to
                            # the active segment so the dispatch shows
                            # post-credit cost, not the pre-credit
                            # row's stale value at the same threshold.
                            cost_row = conn.execute(
                                "SELECT block_cost_usd FROM five_hour_milestones "
                                "WHERE five_hour_window_key = ? "
                                "  AND percent_threshold = ? "
                                "  AND reset_event_id = ?",
                                (int(five_hour_window_key), int(pct),
                                 active_reset_event_id),
                            ).fetchone()
                            block_row = conn.execute(
                                "SELECT block_start_at FROM five_hour_blocks "
                                "WHERE five_hour_window_key = ?",
                                (int(five_hour_window_key),),
                            ).fetchone()
                            primary_model = _resolve_primary_model_for_block(
                                conn, int(five_hour_window_key)
                            )
                            payload = _build_alert_payload_five_hour(
                                threshold=int(pct),
                                crossed_at_utc=crossed_at,
                                five_hour_window_key=int(five_hour_window_key),
                                block_start_at=(
                                    block_row["block_start_at"] if block_row else ""
                                ),
                                block_cost_usd=(
                                    float(cost_row["block_cost_usd"])
                                    if cost_row
                                    else 0.0
                                ),
                                primary_model=primary_model,
                            )
                            pending_alerts.append(payload)

            # ── Reset-crossing cross-flag (opportunistic, JOIN-based) ──
            # Self-healing sweep: every tick, flag any open block whose
            # [block_start_at, last_observed_at_utc] interval crosses a
            # weekly reset, from either of two sources:
            #   (a) week_reset_events — Anthropic-shifted MID-week resets
            #       (prior week_end_at was still in the future at detect
            #       time; see cmd_record_usage's reset-event detection).
            #   (b) weekly_usage_snapshots.week_start_at — NATURAL weekly
            #       boundaries. These never get a week_reset_events row
            #       (mid-week detection requires the prior end to be in
            #       the future), so source (a) silently misses blocks
            #       that span a routine week reset. Without this clause
            #       the dashboard's "Δ pp this block" delta is computed
            #       against the pre-reset 7d% (~94%) versus post-reset
            #       (~0%) and renders as a misleading −94pp drop.
            # Predicate (b) uses strict ``>`` on the lower bound so a
            # block that starts EXACTLY at the boundary (post-reset) is
            # not flagged.  Symmetric with the historical-backfill
            # predicate (§4.2 step 5). Idempotent (only flips 0 → 1).
            #
            # Comparisons go through ``unixepoch()`` rather than a raw
            # lex BETWEEN: ``parse_iso_datetime`` returns host-local
            # tz-aware datetimes (line 9433: ``return parsed.astimezone()``),
            # so ``block_start_at`` is stored with the host's display
            # offset (e.g. ``+03:00``) while ``week_start_at`` is
            # ``+00:00`` and ``last_observed_at_utc`` is ``Z``. A lex
            # compare across mixed offsets silently mis-orders moments
            # for non-UTC hosts; ``unixepoch()`` normalizes all three
            # to seconds-since-epoch and is correct regardless of
            # offset suffix.
            #
            # Why the JOIN rather than a per-tick param: an earlier
            # design passed mid_week_reset_at only on the tick that
            # cmd_record_usage's INSERT OR IGNORE actually inserted
            # the event row. If the helper raised after the event
            # commit but before the flag UPDATE, the next tick's
            # INSERT OR IGNORE was a duplicate and the flag stayed 0
            # forever. The JOIN re-derives from durable state on
            # every tick and self-heals.
            conn.execute(
                """
                UPDATE five_hour_blocks
                   SET crossed_seven_day_reset = 1
                 WHERE crossed_seven_day_reset = 0
                   AND (
                     EXISTS (
                       SELECT 1 FROM week_reset_events e
                        WHERE unixepoch(e.effective_reset_at_utc)
                              BETWEEN unixepoch(five_hour_blocks.block_start_at)
                                  AND unixepoch(five_hour_blocks.last_observed_at_utc)
                     )
                     OR EXISTS (
                       SELECT 1 FROM weekly_usage_snapshots ws
                        WHERE ws.week_start_at IS NOT NULL
                          AND unixepoch(ws.week_start_at)
                              >  unixepoch(five_hour_blocks.block_start_at)
                          AND unixepoch(ws.week_start_at)
                              <= unixepoch(five_hour_blocks.last_observed_at_utc)
                     )
                   )
                """,
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # I1: dispatch deferred to AFTER the outer commit. The milestone
        # row + alerted_at update + close-older + parent upsert + child
        # rebuilds + cross-flag sweep are all durably written together
        # before any externally-observable osascript Popen fires. If the
        # inner BEGIN rolled back above, `pending_alerts` is unreachable
        # (the `raise` above bubbles out via the outer try). Production
        # caller ignores _dispatch_alert_notification's return value
        # (spec §6.4); a per-payload exception is logged and the loop
        # continues so a bad-payload alert can't suppress healthy ones.
        for payload in pending_alerts:
            try:
                _dispatch_alert_notification(
                    payload, mode="real", tz=display_tz_for_alerts
                )
            except Exception as dispatch_exc:
                eprint(f"[alerts] dispatch failed: {dispatch_exc}")
    except Exception as exc:
        eprint(f"[5h-block] error updating block: {exc}")
    finally:
        conn.close()


# ── Reset-to-zero debounce marker (issue #128) ─────────────────────────────
# A transient Anthropic OAuth zero (cold replica / outage) against non-trivial
# usage would otherwise mis-fire the live in-place reset-to-zero detector. We
# debounce: the first ~0 ARMS this marker (it does not fire); the next reading
# CONFIRMS (fires) only if usage stayed low, or CLEARS on recovery toward the
# baseline. The marker is needed because the write-site clamp suppresses the
# deferred first zero, so it leaves no DB trace. Losing the marker is always
# safe (a real reset re-arms and fires one tick later). Best-effort file I/O —
# the detector must never crash on a marker hiccup. See
# docs/superpowers/specs/2026-06-02-reset-zero-debounce-design.md.
_RESET_ZERO_MARKER_NAME = "pending-reset-zero-7d"


def _reset_zero_marker_path():
    return _cctally_core.APP_DIR / _RESET_ZERO_MARKER_NAME


def _arm_reset_zero_marker(week_start_date, cur_end_canon, *,
                           baseline_pct, first_zero_iso):
    """Persist the pending reset-to-zero candidate. ``first_zero_iso`` MUST be
    the ``_command_as_of()`` clock value (it becomes the effective anchor on
    confirm), NOT wall-clock."""
    try:
        _reset_zero_marker_path().write_text(
            f"{week_start_date} {cur_end_canon} "
            f"{float(baseline_pct)} {first_zero_iso}\n"
        )
    except OSError:
        pass


def _clear_reset_zero_marker():
    try:
        _reset_zero_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _read_reset_zero_marker():
    """Return ``(week_start_date, cur_end_canon, baseline_pct, first_zero_iso)``
    or ``None`` when missing / empty / garbled. Validates ALL fields (arity,
    float baseline, parseable timestamp) so a malformed marker re-arms cleanly
    rather than wedging the confirm path."""
    try:
        raw = _reset_zero_marker_path().read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split()
    if len(parts) != 4:
        return None
    week_start_date, cur_end_canon, baseline_raw, first_zero_iso = parts
    try:
        baseline_pct = float(baseline_raw)
    except ValueError:
        return None
    try:
        parse_iso_datetime(first_zero_iso, "reset_zero_marker.first_zero")
    except ValueError:
        return None
    return (week_start_date, cur_end_canon, baseline_pct, first_zero_iso)


# ``CreditPlan`` / ``_parse_credit_at`` / ``_build_credit_plan`` now live in
# ``bin/_lib_credit.py`` (#279 S4 F1); re-imported at module top so the
# ``bin/cctally`` re-exports and this module's own callers
# (``cmd_record_credit``) resolve them unchanged.


def _fire_in_place_credit(conn, week_start_date, cur_end_canon, weekly_percent,
                          *, observed_pre_credit_pct, effective_dt):
    """Emit/refresh the in-place weekly-credit artifacts (issue #19 + #128).
    Shared by the immediate >=25pp path and the debounced reset-to-zero
    confirmation path.

    Side-effect ordering is load-bearing: the event-row INSERT is dedup-gated
    on a pre-check, but the hwm force-write and stale-replica DELETE run
    UNCONDITIONALLY — a prior run may have committed the event then died before
    the pivots (memory: project_dedup_must_not_gate_side_effects). The pivots
    are individually idempotent (file overwrite + DELETE on a stable predicate).

    ``effective_dt`` is the (already-resolved) reset moment; the immediate path
    passes ``_floor_to_hour(now_utc)``, the debounced path passes the floored
    first-zero instant from the marker."""
    effective_iso = effective_dt.isoformat(timespec="seconds")
    # Pre-check keyed on new_week_end_at: suppress a duplicate event row across
    # ticks. UNIQUE(old, new) also dedups, but the pre-check avoids a useless
    # write attempt and keeps logs clean.
    already = conn.execute(
        "SELECT 1 FROM week_reset_events WHERE new_week_end_at = ? LIMIT 1",
        (cur_end_canon,),
    ).fetchone()
    if already is None:
        # Row shape: old=effective_iso, new=cur_end_canon (DISTINCT) so only
        # post_map fires on the credited week in _apply_reset_events_to_weekrefs
        # (old==new collapses it to a zero-width window). observed_pre_credit_pct
        # stamps the pre-credit baseline (issue #45).
        conn.execute(
            "INSERT OR IGNORE INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_utc_iso(), effective_iso, cur_end_canon,
             effective_iso, float(observed_pre_credit_pct)),
        )
        conn.commit()
    # Unconditional pivot 1: force-write hwm-7d so the next status-line render
    # reflects the post-credit value (the monotonic guard at the normal write
    # site would refuse to decrease the file).
    try:
        (_cctally_core.APP_DIR / "hwm-7d").write_text(
            f"{week_start_date} {weekly_percent}\n"
        )
    except OSError:
        pass
    # Unconditional pivot 2: race-defensive cleanup of stale pre-credit replays
    # (external claude-statusline can replay pre-credit --percent values that
    # land captured_at >= effective with pct ~= baseline and dominate the
    # reset-aware clamp). 1.0pp tolerance band absorbs rounding drift; both
    # sides wrapped in unixepoch() for offset robustness.
    try:
        conn.execute(
            "DELETE FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND ABS(weekly_percent - ?) < 1.0",
            (week_start_date, effective_iso, float(observed_pre_credit_pct)),
        )
        conn.commit()
    except sqlite3.DatabaseError as exc:
        eprint(f"[record-usage] post-credit cleanup failed: {exc}")


def _resolve_reset_aware_hwm(conn, week_start_date, week_start_at, week_end_at):
    """The floored MAX(weekly_percent) the statusline _hwm_clamp computes:
    MAX over snapshots captured at/after the latest in-week clamp floor. The
    floor is the latest effective across BOTH `week_reset_events` and
    `weekly_credit_floors` (`_reset_aware_floor`) so a manual partial credit
    (record-credit M2, #209) lowers the resolved HWM without re-anchoring the
    week — used both as the `--from` default and as the assertion source of
    truth in the record-credit tests."""
    floor_iso = _reset_aware_floor(conn, week_start_date, week_start_at, week_end_at)
    if floor_iso is not None:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            " WHERE week_start_date = ? AND unixepoch(captured_at_utc) >= unixepoch(?)",
            (week_start_date, floor_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            " WHERE week_start_date = ?",
            (week_start_date,),
        ).fetchone()
    return None if not row or row[0] is None else float(row[0])


def _insert_credit_snapshot(conn, plan, *, five_hour=(None, None, None)):
    """Insert the post-credit snapshot at plan.to_pct, tagged source='record-credit'."""
    fhp, fhr, fhk = five_hour
    # Normalize effective to a +00:00 UTC spelling in the payload (matches the
    # stored weekly_credit_floors.effective_at_utc on a non-UTC host).
    effective_utc = parse_iso_datetime(
        plan.effective_iso, "snapshot.effective"
    ).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(
        {"kind": "record-credit", "from": plan.from_pct, "to": plan.to_pct,
         "effective": effective_utc},
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " five_hour_percent, five_hour_resets_at, five_hour_window_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan.captured_iso, plan.week_start_date,
         parse_iso_datetime(plan.week_end_at, "we").date().isoformat(),
         plan.week_start_at, plan.week_end_at, plan.to_pct, None,
         "record-credit", payload, fhp, fhr, fhk),
    )
    conn.commit()
    return conn.total_changes


def _resolve_prior_5h(conn, at_dt):
    """Return the most-recent snapshot's (five_hour_percent, five_hour_resets_at,
    five_hour_window_key) iff that 5h window is still active (resets_at > at_dt),
    else (None, None, None) — so the synthetic row doesn't blank the live 5h
    display, and never inflates the 5h HWM (copies an already-<=MAX value)."""
    row = conn.execute(
        "SELECT five_hour_percent, five_hour_resets_at, five_hour_window_key "
        "FROM weekly_usage_snapshots "
        "WHERE five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NOT NULL "
        "ORDER BY unixepoch(captured_at_utc) DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return (None, None, None)
    try:
        if parse_iso_datetime(row[1], "prior.5h_resets") > at_dt:
            return (row[0], row[1], int(row[2]))
    except ValueError:
        pass
    return (None, None, None)


def _apply_credit(conn, plan, *, five_hour=(None, None, None)):
    """Apply the M2 same-window partial-credit artifacts (record-credit, #209,
    spec §4). Unlike the >=25pp auto-credit path (`_fire_in_place_credit`), this
    writes NO `week_reset_events` row — the window-resolution code never sees a
    credit, so the week is NOT re-anchored. It only lowers the clamp floor.

    Side-effect ordering mirrors `_fire_in_place_credit`'s discipline: the
    INSERT OR IGNORE of the floor row is dedup-gated by UNIQUE(week_start_date,
    effective_at_utc), but the hwm force-write, stale-replay DELETE, and
    synthetic-snapshot INSERT run UNCONDITIONALLY so a rerun finishes a crash-
    half-applied credit (memory: project_dedup_must_not_gate_side_effects). All
    are individually idempotent (file overwrite; DELETE on a stable predicate;
    the synthetic snapshot is re-INSERTed only after `_force_clear_credit` or on
    the completion path where none exists yet).

    `plan.effective_iso` is `floor_to_hour(at)`. parse_iso_datetime returns a
    host-local-offset aware datetime; convert to UTC so `effective_at_utc`
    persists with a +00:00 spelling, not a host offset, in the `*_utc` column.
    On the completion / --force re-apply path the CALLER passes a `plan` whose
    `effective_iso` is the EXISTING floor row's `effective_at_utc` (NOT a fresh
    floor_to_hour(now)) — spec §4a completion-effective reuse."""
    c = _cctally()
    effective_dt = parse_iso_datetime(plan.effective_iso, "effective").astimezone(dt.timezone.utc)
    effective_iso = effective_dt.isoformat(timespec="seconds")
    pre_credit = float(plan.from_pct)

    # 4a. INSERT the credit floor (no week_reset_events row — the whole point).
    conn.execute(
        "INSERT OR IGNORE INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, applied_at_utc) "
        "VALUES (?, ?, ?, ?)",
        (plan.week_start_date, effective_iso, pre_credit, now_utc_iso()),
    )
    conn.commit()

    # 4b. Force-write hwm-7d so the external statusline render reflects the
    # post-credit value (the normal write-site monotonic guard would refuse to
    # decrease the file).
    try:
        (_cctally_core.APP_DIR / "hwm-7d").write_text(
            f"{plan.week_start_date} {plan.to_pct}\n"
        )
    except OSError:
        pass

    # 4c. Stale-replay DELETE: drop pre-credit-valued replays that land at/after
    # the floor (the gotcha_statusline_replay_race_after_credit defense; same
    # 1.0pp band as the auto path). unixepoch() on both sides for offset safety.
    try:
        conn.execute(
            "DELETE FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND ABS(weekly_percent - ?) < 1.0",
            (plan.week_start_date, effective_iso, pre_credit),
        )
        conn.commit()
    except sqlite3.DatabaseError as exc:
        eprint(f"[record-credit] post-credit cleanup failed: {exc}")

    # 4d. INSERT the synthetic post-credit snapshot at plan.to_pct.
    c._insert_credit_snapshot(conn, plan, five_hour=five_hour)

    # 4e. Clear a stale same-week reset-zero marker so the next record-usage
    # tick can't confirm a phantom reset-to-zero off it.
    _clear_reset_zero_marker()


def _force_clear_credit(conn, week_start_date):
    """--force scope (M2, spec §4): delete ONLY this week's command-owned
    synthetic snapshots (`source='record-credit'`) + its `weekly_credit_floors`
    row(s). Never touches real status-line snapshots, and never
    `week_reset_events` / `percent_milestones` — a partial credit writes
    neither (no event row -> no segmentation; no milestones)."""
    conn.execute("DELETE FROM weekly_usage_snapshots "
                 " WHERE week_start_date=? AND source='record-credit'", (week_start_date,))
    conn.execute("DELETE FROM weekly_credit_floors "
                 " WHERE week_start_date=?", (week_start_date,))
    conn.commit()


def _count_stale_replays(conn, plan):
    """Count the pre-credit replay rows the _apply_credit stale-replay DELETE
    (step 4c, inline) will touch (captured at/after the credit moment within a
    1.0pp band of from), for the preview / --json `staleReplaysDeleted` field.
    Read-only."""
    row = conn.execute(
        "SELECT COUNT(*) FROM weekly_usage_snapshots "
        " WHERE week_start_date = ? "
        "   AND unixepoch(captured_at_utc) >= unixepoch(?) "
        "   AND ABS(weekly_percent - ?) < 1.0",
        (plan.week_start_date, plan.effective_iso, float(plan.from_pct)),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _credit_preview_text(plan, *, stale_replays, dry_run):
    """Human preview (spec §5). Shown before the confirm prompt and as the
    whole body under --dry-run."""
    eff_dt = parse_iso_datetime(plan.effective_iso, "effective").astimezone(dt.timezone.utc)
    cap_dt = parse_iso_datetime(plan.captured_iso, "captured").astimezone(dt.timezone.utc)
    we_dt = parse_iso_datetime(plan.week_end_at, "week_end").astimezone(dt.timezone.utc)
    src = {
        "hwm": "current HWM",
        "explicit": "explicit",
        "prior_credit": "prior credit",
    }.get(plan.from_source, plan.from_source)
    lines = [
        "record-credit — weekly in-place credit",
        f"  week:          {plan.week_start_date} -> "
        f"{we_dt.strftime('%Y-%m-%d %H:%M')} UTC",
        f"  from -> to:    {plan.from_pct:g}% -> {plan.to_pct:g}%   (from: {src})",
        f"  effective:     {eff_dt.strftime('%Y-%m-%d %H:%M')} UTC  "
        f"(floored from {cap_dt.strftime('%Y-%m-%d %H:%M')})",
        "  writes:",
        f"    + weekly_credit_floors  (effective={plan.effective_iso}, "
        f"pre_credit={plan.from_pct:g})",
        f"    ~ hwm-7d                {plan.from_pct:g} -> {plan.to_pct:g}",
        f"    - stale replays         {stale_replays} rows",
        f"    + snapshot              captured={plan.captured_iso}, "
        f"weekly_percent={plan.to_pct:g}",
        "  note: same week — no window re-anchor (no week_reset_events row)",
    ]
    if dry_run:
        lines.append("  (dry-run — nothing written)")
    return "\n".join(lines)


def _credit_json(plan, *, applied, dry_run, forced, stale_replays, hwm_before):
    """The --json envelope (schemaVersion 1, spec §5); all datetimes …Z."""
    def _z(iso):
        return parse_iso_datetime(iso, "z").astimezone(
            dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "schemaVersion": 1,
        "applied": applied,
        "dryRun": dry_run,
        "forced": forced,
        "week": {
            "weekStartDate": plan.week_start_date,
            "weekStartAt": _z(plan.week_start_at),
            "weekEndAt": _z(plan.week_end_at),
        },
        "credit": {
            "fromPct": plan.from_pct,
            "toPct": plan.to_pct,
            "fromSource": plan.from_source,
            "effectiveAtUtc": _z(plan.effective_iso),
        },
        "actions": {
            "creditFloorInserted": applied,
            "hwm7dBefore": hwm_before,
            "hwm7dAfter": plan.to_pct if applied else hwm_before,
            "staleReplaysDeleted": stale_replays,
            "postCreditSnapshotInserted": applied,
        },
    }


def _revalidate_credit_plan(conn, args, *, now, at_dt, expected_plan):
    """Recompute the confirmed credit plan from locked, current DB truth.

    The caller has already completed every preview/refusal/confirmation path.
    Returning ``None`` is deliberately side-effect free: it means a concurrent
    writer changed the requested credit's basis and the user must retry rather
    than authorizing a different mutation than the preview showed.
    """
    try:
        if getattr(args, "week", None):
            week_start_date = args.week
            ws_at, we_at = _get_canonical_boundary_for_date(conn, week_start_date)
            if not ws_at or not we_at:
                return None
        else:
            fetched = _fetch_current_week_snapshots(conn, at_dt)
            if fetched is None:
                return None
            ws_at, we_at, _samples = fetched
            ws_at = ws_at if isinstance(ws_at, str) else ws_at.isoformat(timespec="seconds")
            we_at = we_at if isinstance(we_at, str) else we_at.isoformat(timespec="seconds")
            week_start_date = parse_iso_datetime(ws_at, "ws_at").date().isoformat()
        existing = conn.execute(
            "SELECT id, effective_at_utc, observed_pre_credit_pct "
            "FROM weekly_credit_floors WHERE week_start_date=? "
            "ORDER BY unixepoch(effective_at_utc) DESC, id DESC LIMIT 1",
            (week_start_date,),
        ).fetchone()
        is_force = bool(getattr(args, "force", False))
        if getattr(args, "from_pct", None) is not None:
            from_pct, from_source = float(args.from_pct), "explicit"
        elif existing is not None and existing[2] is not None:
            from_pct, from_source = float(existing[2]), "prior_credit"
        else:
            from_pct = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at)
            if from_pct is None:
                return None
            from_source = "hwm"
        is_completion = False
        if existing is not None and not is_force:
            owned = conn.execute(
                "SELECT 1 FROM weekly_usage_snapshots "
                " WHERE week_start_date=? AND source='record-credit' "
                "   AND unixepoch(captured_at_utc) >= unixepoch(?) LIMIT 1",
                (week_start_date, existing[1]),
            ).fetchone()
            is_completion = owned is None
        if existing is not None and not is_force and not is_completion:
            return None
        plan = _build_credit_plan(
            week_start_date=week_start_date,
            week_start_at=ws_at,
            week_end_at=we_at,
            from_pct=from_pct,
            from_source=from_source,
            to_pct=args.to,
            at_dt=at_dt,
            now=now,
            effective_override=existing[1] if is_completion else None,
        )
    except (sqlite3.DatabaseError, ValueError, TypeError):
        return None
    if plan != expected_plan:
        return None
    return plan, existing, is_completion


def cmd_record_credit(args) -> int:
    now = _command_as_of()
    try:
        at_dt = _parse_credit_at(getattr(args, "at", None), now)
    except ValueError as e:
        eprint(f"record-credit: {e}")
        return 2
    conn = None
    try:
        conn = open_db()
        # 1. Resolve the week.
        if getattr(args, "week", None):
            week_start_date = args.week
            ws_at, we_at = _get_canonical_boundary_for_date(conn, week_start_date)
            if not ws_at or not we_at:
                eprint(f"record-credit: no snapshot for --week {week_start_date}")
                return 2
        else:
            fetched = _fetch_current_week_snapshots(conn, at_dt)
            if fetched is None:
                eprint("record-credit: no snapshot week contains --at; pass --week")
                return 2
            ws_at, we_at, _samples = fetched
            ws_at = ws_at if isinstance(ws_at, str) else ws_at.isoformat(timespec="seconds")
            we_at = we_at if isinstance(we_at, str) else we_at.isoformat(timespec="seconds")
            week_start_date = parse_iso_datetime(ws_at, "ws_at").date().isoformat()

        # Resolve any existing credit FLOOR for this week up front — needed
        # both for the --from default fallback (a half-applied credit empties
        # the post-credit segment, so the reset-aware HWM reads NULL) and the
        # apply-time completion/refuse/force branch (M2 keys on
        # weekly_credit_floors, NOT week_reset_events — a partial credit never
        # writes a reset-event row). Latest floor wins (a --force re-apply at a
        # new effective leaves the old row only until _force_clear_credit
        # deletes it; pick the newest defensively).
        existing = conn.execute(
            "SELECT id, effective_at_utc, observed_pre_credit_pct "
            "FROM weekly_credit_floors WHERE week_start_date=? "
            "ORDER BY unixepoch(effective_at_utc) DESC, id DESC LIMIT 1",
            (week_start_date,)).fetchone()

        # 2. Resolve --from default.
        if getattr(args, "from_pct", None) is not None:
            from_pct, from_source = float(args.from_pct), "explicit"
        elif existing is not None and existing[2] is not None:
            # A credit floor already exists for this week (completion or
            # --force re-record). Its recorded observed_pre_credit_pct is the
            # AUTHENTIC pre-credit baseline; prefer it over the reset-aware
            # HWM. The post-credit segment's MAX(weekly_percent) would
            # otherwise pick up the post-credit value (31) or a later real
            # status-line reading, mis-deriving the baseline and causing the
            # stale-replay DELETE to (wrongly) match real history. fromSource
            # is 'prior_credit' (spec §5).
            from_pct, from_source = float(existing[2]), "prior_credit"
        else:
            hwm = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at)
            if hwm is None:
                eprint("record-credit: no usage history for the week; pass --from")
                return 2
            from_pct, from_source = hwm, "hwm"

        is_force = getattr(args, "force", False)

        # 2a. Classify the existing-floor state (M2, spec §4/§5). A
        #     `weekly_credit_floors` row may be:
        #       - half-applied (floor row present, NO command-owned snapshot
        #         at/after its effective): a crash between 4a and 4d. A plain
        #         rerun FINISHES it, reusing the existing effective_at_utc (NOT
        #         a fresh floor_to_hour(now)) so no stale [old,new) replay leaks
        #         into the floored MAX (spec §4a completion-effective reuse).
        #       - fully applied (floor row + command-owned snapshot): refuse by
        #         default; --force clears + re-records at a fresh effective.
        is_completion = False
        if existing is not None and not is_force:
            owned = conn.execute(
                "SELECT 1 FROM weekly_usage_snapshots "
                " WHERE week_start_date=? AND source='record-credit' "
                "   AND unixepoch(captured_at_utc) >= unixepoch(?) LIMIT 1",
                (week_start_date, existing[1])).fetchone()
            is_completion = owned is None

        # The effective the plan should carry: a half-applied completion reuses
        # the EXISTING floor row's effective; a first credit / --force re-apply
        # uses floor_to_hour(at) (computed inside _build_credit_plan).
        reuse_effective = existing[1] if is_completion else None

        # 3. Validate + build plan.
        try:
            plan = _build_credit_plan(
                week_start_date=week_start_date, week_start_at=ws_at,
                week_end_at=we_at, from_pct=from_pct, from_source=from_source,
                to_pct=args.to, at_dt=at_dt, now=now,
                effective_override=reuse_effective,
            )
        except ValueError as e:
            eprint(f"record-credit: {e}")
            return 2

        # 4. Output + confirm matrix (spec §5).
        is_json = getattr(args, "json", False)
        is_dry = getattr(args, "dry_run", False)
        is_yes = getattr(args, "yes", False)
        stale_replays = _count_stale_replays(conn, plan)
        hwm_before = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at)
        if hwm_before is None:
            hwm_before = plan.from_pct

        # --dry-run: preview only, write nothing, exit 0 (TTY or not,
        # with/without --json).
        if is_dry:
            if is_json:
                print(json.dumps(_credit_json(
                    plan, applied=False, dry_run=True, forced=False,
                    stale_replays=stale_replays, hwm_before=hwm_before)))
            else:
                print(_credit_preview_text(plan, stale_replays=stale_replays,
                                           dry_run=True))
            return 0

        # --json (not dry-run) must be paired with --yes; never prompts.
        if is_json and not is_yes:
            eprint("record-credit: --json requires --yes or --dry-run")
            return 2

        # Fully-applied refuse (M2; state classified at step 2a). A credit
        # already fully recorded for this week (floor row + command-owned
        # snapshot) is refused by default. Hoisted ABOVE the interactive
        # confirm prompt so a TTY user is refused immediately, rather than
        # being shown the preview, prompted, answering y, and only THEN refused
        # (issue #212 N2). `--force` is intentionally NOT refused here — it
        # takes the clear + re-record path at step 4a; a half-applied credit
        # (is_completion) falls through to finish idempotently. This fires
        # regardless of --yes/TTY so the precondition failure is uniform; the
        # earlier --dry-run path still previews (writes nothing) and returns 0.
        if existing is not None and not is_force and not is_completion:
            eprint(f"record-credit: a credit is already recorded for this "
                   f"week (effective={existing[1]}, pre_credit={existing[2]}); "
                   f"pass --force to re-record")
            return 2

        # No --yes: prompt (TTY) or refuse (non-TTY).
        if not is_yes:
            if not sys.stdin.isatty():
                eprint("record-credit: stdin not a TTY: pass --yes to apply "
                       "or --dry-run to preview")
                return 2
            print(_credit_preview_text(plan, stale_replays=stale_replays,
                                       dry_run=False))
            try:
                reply = input("Proceed? [y/N] ")
            except EOFError:
                reply = ""
            if reply.strip().lower() not in ("y", "yes"):
                print("aborted — nothing written")
                return 0

        # Every non-mutating exit is above this point.  Re-open under the
        # selected-state writer lock, recompute from current DB truth, and
        # refuse plan drift before any durable pipeline artifact is visible.
        # This is intentionally after confirmation: preview/no/TTY refusal
        # must not create an inflight tombstone merely by inspecting a credit.
        conn.close()
        conn = None
        c = _cctally()
        with c._selected_state_lock():
            conn = open_db()
            revalidated = c._revalidate_credit_plan(
                conn,
                args,
                now=now,
                at_dt=at_dt,
                expected_plan=plan,
            )
            if revalidated is None:
                eprint("record-credit: plan changed while awaiting confirmation; retry")
                return 2
            plan, existing, is_completion = revalidated
            stale_replays = _count_stale_replays(conn, plan)
            hwm_before = _resolve_reset_aware_hwm(
                conn, plan.week_start_date, plan.week_start_at, plan.week_end_at
            )
            if hwm_before is None:
                hwm_before = plan.from_pct
            try:
                handles = c._authoritative_begin(
                    {"sevenDay"}, now_epoch=int(time.time())
                )
            except (OSError, ValueError) as exc:
                eprint(f"record-credit: could not prepare authoritative state: {exc}")
                return 3

            # Existing-floor handling remains exactly scoped to this week, but
            # the weekly tombstone is already fail-closed immediately before
            # either mutation kernel runs.
            forced = False
            if existing is not None and is_force:
                _force_clear_credit(conn, plan.week_start_date)
                forced = True
            five_hour = _resolve_prior_5h(conn, at_dt)
            _apply_credit(conn, plan, five_hour=five_hour)

            # A successful credit is authoritative only after the post-credit
            # DB state has a stable projection and all selected artifacts are
            # atomically committed.  Leave inflight on any failure so a later
            # authority repair remains fail-closed.
            try:
                projection = c._read_db_projection_stable()
                completion_epoch = int(time.time())
                c._authoritative_commit(
                    handles, completion_epoch=completion_epoch
                )
                c._reconcile_selected_control(
                    projection, now_epoch=completion_epoch, observed_axes={"sevenDay"}
                )
                c._statusline_observe_touch()
            except Exception as exc:
                eprint(f"record-credit: authoritative state incomplete: {exc}")
                return 3

        if is_json:
            print(json.dumps(_credit_json(
                plan, applied=True, dry_run=False, forced=forced,
                stale_replays=stale_replays, hwm_before=hwm_before)))
        else:
            print(f"record-credit: applied — week {plan.week_start_date} "
                  f"{plan.from_pct:g}% -> {plan.to_pct:g}% "
                  f"(effective {plan.effective_iso})")
        return 0
    except _cctally().StatsDbCorruptError:
        # #279 S1 F4: the global corrupt-DB contract (one-line diagnosis +
        # exit 2) wins over record-credit's documented exit-3 DB-error mapping.
        # StatsDbCorruptError subclasses sqlite3.DatabaseError, so without this
        # re-raise the handler below would swallow it and return 3.
        raise
    except sqlite3.DatabaseError as e:
        # Documented exit 3 (docs/commands/record-credit.md "3 — a database
        # error"). The inner ValueError->2 / EOFError paths return before
        # reaching here, so this only catches genuine DB failures from
        # open_db() through _apply_credit/output. Plain-text on stderr,
        # matching the record-credit: <msg> convention of the validation paths.
        eprint(f"record-credit: {e}")
        return 3
    finally:
        if conn is not None:
            conn.close()


def cmd_record_usage(args: argparse.Namespace) -> int:
    """Record usage data from Claude Code status line rate_limits."""
    c = _cctally()
    config = load_config()
    week_start_name = get_week_start_name(config, getattr(args, "week_start_name", None))

    # ULP-noise sanitization is applied at the cmd_record_usage ingress
    # boundary so every downstream consumer (HWM files, DB rows,
    # five_hour_blocks rollup, milestones) reads a stable value. See
    # `_normalize_percent` for the rationale.
    weekly_percent = _normalize_percent(args.percent)
    resets_at = int(args.resets_at)

    # Plausibility guard (issue #112). Band-check epochs BEFORE any
    # dt.datetime.fromtimestamp() call so absurd values (ms-epoch,
    # year-off bugs, negative) reject gracefully instead of raising
    # OverflowError. Reject path returns exit 2 so
    # _refresh_usage_inproc maps it to status="record_failed" instead
    # of silently reporting success on a dropped payload.
    now_dt = _command_as_of()
    now_epoch = int(now_dt.timestamp())
    if not check_resets_at_plausibility(
        resets_at, now_epoch,
        past_slack_s=_RECORD_USAGE_WEEK_PAST_SLACK_S,
        future_band_s=_RECORD_USAGE_WEEK_FUTURE_BAND_S,
    ):
        eprint(
            f"[record-usage] rejecting --resets-at={resets_at}: outside "
            f"plausibility band [now-30d, now+8d]; "
            f"now={now_epoch} ({now_dt.isoformat()}). No row written."
        )
        return 2

    five_hour_percent: float | None = None
    five_hour_resets_at_str: str | None = None
    five_hour_window_key: int | None = None
    five_hour_resets_at_epoch: int | None = None
    if args.five_hour_percent is not None:
        five_hour_percent = _normalize_percent(args.five_hour_percent)
    if args.five_hour_resets_at is not None:
        five_hour_resets_at_epoch = int(args.five_hour_resets_at)
        # Band-check BEFORE fromtimestamp (issue #112).
        #
        # Out-of-band 5h is non-fatal: drop the 5h fields and continue
        # so the weekly snapshot still writes. Two motivating cases:
        #   (a) docs' manual-replay path (record-usage.md) emits the
        #       original status-line args verbatim, including stale 5h
        #       flags — rejecting the whole call there contradicts the
        #       wider 30d weekly past slack.
        #   (b) An already-expired 5h resets_at would pollute the prior
        #       block's totals (block_start_at = resets_at - 5h →
        #       _compute_block_totals charges entries past the real
        #       reset to this block). Dropping the 5h portion here
        #       skips maybe_update_five_hour_block entirely.
        if not check_resets_at_plausibility(
            five_hour_resets_at_epoch, now_epoch,
            past_slack_s=_RECORD_USAGE_5H_PAST_SLACK_S,
            future_band_s=_RECORD_USAGE_5H_FUTURE_BAND_S,
        ):
            eprint(
                f"[record-usage] dropping --five-hour-resets-at="
                f"{five_hour_resets_at_epoch}: outside plausibility band "
                f"[now-10m, now+6h]; now={now_epoch} "
                f"({now_dt.isoformat()}). Weekly snapshot still written; "
                f"5h fields will be NULL."
            )
            five_hour_percent = None
            five_hour_resets_at_epoch = None
        else:
            five_hour_resets_at_str = dt.datetime.fromtimestamp(
                five_hour_resets_at_epoch, tz=dt.timezone.utc
            ).isoformat(timespec="seconds")
        # five_hour_window_key derivation is deferred until after open_db()
        # so we can pass the most-recent stored sample as the prior anchor.
        # See _canonical_5h_window_key docstring (spec invariant #3:
        # boundary-straddling jitter must collapse to the first-seen key).

    # Derive week boundaries from resets_at (exact UTC epoch)
    week_end_at_dt = dt.datetime.fromtimestamp(resets_at, tz=dt.timezone.utc)
    week_start_at_dt = week_end_at_dt - dt.timedelta(days=7)
    week_start_date = week_start_at_dt.date().isoformat()
    week_end_date = week_end_at_dt.date().isoformat()
    week_start_at = week_start_at_dt.isoformat(timespec="seconds")
    week_end_at = week_end_at_dt.isoformat(timespec="seconds")

    # Deduplication: skip if nothing changed since last snapshot
    should_insert = True
    conn = open_db()
    try:
        # Resolve the canonical 5h window key. Pass the most-recent stored
        # sample as the prior anchor so seconds-level jitter that straddles
        # a 600-second floor-bucket boundary (e.g. resets_at=1746014999 vs.
        # 1746015000) collapses to the first-seen key instead of forking
        # a new one. Without this, both the DB clamp below and the hwm-5h
        # file write further down would treat the same physical window as
        # distinct, regressing the monotonic 5h percent (spec invariant #3).
        if five_hour_resets_at_epoch is not None:
            prior_5h_epoch: int | None = None
            prior_5h_key: int | None = None
            # Tier 1: blocks-table lookup (steady state). Find the closest
            # canonical block whose five_hour_resets_at is within ±1800s of
            # the new resets_at. The blocks table has one canonical row per
            # physical window after the merge_5h_block_duplicates_v1
            # migration, so this is more reliable than scanning
            # weekly_usage_snapshots for an "anchor" row — snapshots can be
            # noisy when the status line returns out-of-order rate-limit
            # data from older windows (the F4 incident: snap N+1 carrying a
            # window-A boundary-jitter resets_at, but snap N reported an
            # OLDER window B; pre-fix the prior-anchor lookup picked B as
            # the anchor and the |epoch-prior| > 600 check then forked a
            # new key for what was actually still window A). 1800s is wide
            # enough to absorb known jitter, narrow enough that consecutive
            # 5h blocks (>4h apart in resets_at) cannot collide.
            try:
                prior_block_row = conn.execute(
                    """
                    SELECT five_hour_window_key, five_hour_resets_at
                      FROM five_hour_blocks
                     WHERE abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) <= ?
                     ORDER BY abs(? - CAST(strftime('%s', five_hour_resets_at) AS INTEGER)) ASC
                     LIMIT 1
                    """,
                    (
                        five_hour_resets_at_epoch,
                        c._FIVE_HOUR_JITTER_FLOOR_SECONDS * 3,
                        five_hour_resets_at_epoch,
                    ),
                ).fetchone()
                if prior_block_row is not None:
                    prior_iso = prior_block_row["five_hour_resets_at"]
                    prior_5h_epoch = int(parse_iso_datetime(
                        prior_iso, "prior 5h block anchor"
                    ).timestamp())
                    prior_5h_key = int(prior_block_row["five_hour_window_key"])
            except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
                eprint(f"[record-usage] prior 5h block-anchor lookup failed: {exc}")

            # Tier 2: snapshot lookup (legacy fallback). Only run when Tier
            # 1 missed (no canonical block row exists yet — the brand-new-
            # window case before any record-usage tick has materialized a
            # five_hour_blocks row). Tier 1's empty-result guard is the
            # `prior_block_row is not None` test above; replicating it
            # here keeps Tier 2 strictly secondary.
            if prior_5h_key is None:
                try:
                    prior_5h_row = conn.execute(
                        "SELECT five_hour_resets_at, five_hour_window_key "
                        "FROM weekly_usage_snapshots "
                        "WHERE five_hour_resets_at IS NOT NULL "
                        "  AND five_hour_window_key IS NOT NULL "
                        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
                    ).fetchone()
                    if prior_5h_row is not None:
                        prior_iso = prior_5h_row["five_hour_resets_at"]
                        prior_5h_epoch = int(parse_iso_datetime(
                            prior_iso, "prior 5h anchor"
                        ).timestamp())
                        prior_5h_key = int(prior_5h_row["five_hour_window_key"])
                except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
                    eprint(f"[record-usage] prior 5h anchor lookup failed: {exc}")

            # Tier 3 is implicit: with no anchor, _canonical_5h_window_key
            # falls back to the pure 600-second floor.
            five_hour_window_key = _canonical_5h_window_key(
                five_hour_resets_at_epoch,
                prior_epoch=prior_5h_epoch,
                prior_key=prior_5h_key,
            )

        # Mid-week reset detection. When `resets_at` advances before the
        # previously-declared reset actually fires (Anthropic-initiated
        # goodwill reset, or any API-side shift), record one week_reset_events
        # row so display + cost layers can treat the observed moment as the
        # old week's effective end AND the new week's effective start. The
        # monotonic check below stays keyed on week_start_date so it still
        # guards the new week against stale rate-limit data independently.
        # Both boundaries canonicalize to hour (same rule make_week_ref uses)
        # so minute/second-level Anthropic jitter doesn't masquerade as a
        # reset and the stored values match what WeekRef.week_end_at carries.
        # The 5h-block cross-flag is no longer threaded from here —
        # maybe_update_five_hour_block re-derives it every tick by JOINing
        # against week_reset_events (self-healing, see helper for rationale).
        try:
            cur_end_canon = _canonicalize_optional_iso(week_end_at, "record.cur")
            prior = conn.execute(
                "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots "
                "WHERE week_end_at IS NOT NULL "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
            ).fetchone()
            if prior and prior["week_end_at"] and cur_end_canon:
                prior_end_canon = _canonicalize_optional_iso(
                    prior["week_end_at"], "record.prior"
                )
                prior_pct = prior["weekly_percent"]
                # Use _command_as_of() so CCTALLY_AS_OF pins the predicate
                # for tests (no behavior change in production — falls back
                # to wall-clock when the env hook is unset). This makes
                # mid-week-reset detection deterministic against fixtures
                # whose `prior_end` is a fixed historical instant.
                now_utc = _command_as_of()
                if prior_end_canon and prior_end_canon != cur_end_canon:
                    prior_end_dt = parse_iso_datetime(prior_end_canon, "prior.week_end_at")
                    # Fire only when (a) prior window was still in the FUTURE
                    # (Anthropic shifted the boundary before natural expiration),
                    # AND (b) weekly_percent dropped by RESET_PCT_DROP_THRESHOLD
                    # or more (filters out API flaps / transient boundary
                    # jitter where usage stays roughly the same).
                    if (
                        prior_end_dt > now_utc
                        and prior_pct is not None
                        and c._is_reset_drop(prior_pct, weekly_percent)
                    ):
                        # See _backfill_week_reset_events for why we floor
                        # the reset moment to the hour (natural display
                        # boundary, aligned with Anthropic's hour-only
                        # resets_at values).
                        effective_iso = _floor_to_hour(now_utc).isoformat(timespec="seconds")
                        conn.execute(
                            "INSERT OR IGNORE INTO week_reset_events "
                            "(detected_at_utc, old_week_end_at, new_week_end_at, "
                            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
                            (now_utc_iso(), prior_end_canon, cur_end_canon,
                             effective_iso),
                        )
                        conn.commit()
                elif prior_end_canon and prior_end_canon == cur_end_canon:
                    # In-place credit branch (v1.7.2) + reset-to-zero debounce
                    # (issue #128). Same end_at across two captures. A >=25pp drop
                    # is a goodwill credit and fires immediately; a reset-to-zero
                    # (post <= floor, 3..25pp drop) is debounced against a
                    # transient API zero — armed on the first ~0, confirmed only
                    # if the next reading stays low (<= half the pre-zero
                    # baseline), cleared on recovery toward baseline. The gate
                    # drops the _is_reset_drop term so the recovery-clear path is
                    # reachable. See the spec for the midpoint rationale.
                    prior_end_dt = parse_iso_datetime(prior_end_canon, "prior.week_end_at")
                    if prior_end_dt > now_utc and prior_pct is not None:
                        # Read the pending reset-to-zero marker up front (pure
                        # file read) and compute whether it is armed for THIS
                        # window; the debounce CLASSIFIER (pure) decides the
                        # action from those values + the c._RESET_* constants,
                        # then the glue below executes the decided I/O. The 5
                        # branch outcomes (fire-immediate / confirm / clear /
                        # arm / none) map 1:1 to the pre-extraction structure.
                        marker = _read_reset_zero_marker()
                        armed = (
                            marker is not None
                            and marker[0] == week_start_date
                            and marker[1] == cur_end_canon
                        )
                        decision = plan_weekly_credit_debounce(
                            prior_pct, weekly_percent,
                            drop_threshold=c._RESET_PCT_DROP_THRESHOLD,
                            zero_floor_pct=c._RESET_ZERO_FLOOR_PCT,
                            zero_min_drop_pct=c._RESET_ZERO_MIN_DROP_PCT,
                            marker_armed=armed,
                            marker_baseline=(marker[2] if armed else None),
                        ).action
                        if decision == FIRE_IMMEDIATE:
                            # >=25pp goodwill credit — fire immediately, never
                            # debounced. Clear any pending arm (now moot).
                            _clear_reset_zero_marker()
                            _fire_in_place_credit(
                                conn, week_start_date, cur_end_canon, weekly_percent,
                                observed_pre_credit_pct=float(prior_pct),
                                effective_dt=_floor_to_hour(now_utc),
                            )
                        elif decision == CONFIRM_RESET:
                            # Second reading stayed low → confirm. Anchor the
                            # reset at the FIRST-zero instant from the marker
                            # (UTC-normalized like the backfill in-place path).
                            first_zero_dt = parse_iso_datetime(
                                marker[3], "reset_zero_marker.first_zero"
                            ).astimezone(dt.timezone.utc)
                            _fire_in_place_credit(
                                conn, week_start_date, cur_end_canon,
                                weekly_percent,
                                observed_pre_credit_pct=marker[2],
                                effective_dt=_floor_to_hour(first_zero_dt),
                            )
                            # Clear ONLY after the fire completes (P2a): a
                            # mid-fire crash leaves the marker armed so the next
                            # zero re-confirms + re-runs the idempotent pivots.
                            _clear_reset_zero_marker()
                        elif decision == CLEAR_MARKER:
                            # Recovered toward baseline → transient zero, not a
                            # reset. Clear, do not fire.
                            _clear_reset_zero_marker()
                        elif decision == ARM_MARKER:
                            # First ~0 → arm; do NOT fire. The write clamp
                            # suppresses this 0 (no event row yet), so the prior
                            # snapshot stays at the baseline and this shape
                            # re-evaluates next tick. first_zero_iso is the
                            # _command_as_of() value (now_utc), NOT wall-clock —
                            # it becomes the effective anchor.
                            _arm_reset_zero_marker(
                                week_start_date, cur_end_canon,
                                baseline_pct=float(prior_pct),
                                first_zero_iso=now_utc.isoformat(timespec="seconds"),
                            )
                        # else NO_ACTION: not a reset shape and not armed →
                        #     nothing. A non-matching stale marker is inert
                        #     (ignored on key mismatch, overwritten by next arm).

            # ── 5h in-place credit detection (parallel to weekly above) ──
            # Spec §2.2 of
            # docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md.
            # Slot SECOND so the weekly branch retains control-flow
            # priority — both branches are independent (they touch
            # different tables) and the order has no behavioral
            # interaction. Same outer try/except wraps both so a
            # 5h-detection failure logs but does not regress the rest
            # of cmd_record_usage.
            #
            # Diverges from weekly in three places:
            #   - Threshold: 5.0pp (constant on cctally module), not 25.0pp.
            #     The 5h envelope is smaller so a 5pp move is
            #     proportionally larger.
            #   - Effective-iso floor: 10-min (matches
            #     ``_canonical_5h_window_key``'s 600s floor), not hour.
            #     Up to ~30 distinct slots per 5h block; same-slot
            #     collisions absorbed by UNIQUE per spec §2.3.
            #   - Pre-check: pair-checks the latest event's
            #     ``(prior_percent, post_percent)`` against this tick's
            #     ``(prior_5h_pct, five_hour_percent)``, not
            #     ``new_week_end_at`` equality. A genuine replay matches
            #     BOTH fields; a NEW credit-with-idle (prior_pct equals
            #     the prior credit's post_pct because the user didn't
            #     move between credits) matches only one field and
            #     correctly proceeds to write a second event row.
            try:
                if (
                    five_hour_window_key is not None
                    and five_hour_percent is not None
                ):
                    prior_5h_row = conn.execute(
                        "SELECT five_hour_window_key, five_hour_percent, "
                        "       five_hour_resets_at "
                        "  FROM weekly_usage_snapshots "
                        " WHERE five_hour_window_key IS NOT NULL "
                        "   AND five_hour_percent IS NOT NULL "
                        " ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
                    ).fetchone()
                    if (
                        prior_5h_row is not None
                        and int(prior_5h_row["five_hour_window_key"])
                            == int(five_hour_window_key)
                        and prior_5h_row["five_hour_resets_at"] is not None
                    ):
                        prior_5h_pct = float(prior_5h_row["five_hour_percent"])
                        prior_5h_resets_dt = parse_iso_datetime(
                            prior_5h_row["five_hour_resets_at"],
                            "prior.five_hour_resets_at",
                        )
                        # ``now_utc`` was bound earlier in this same
                        # outer try block from
                        # ``dt.datetime.now(dt.timezone.utc)``; reuse it
                        # so both branches see the same instant.
                        if plan_five_hour_credit(
                            prior_5h_pct, float(five_hour_percent),
                            drop_threshold=c._FIVE_HOUR_RESET_PCT_DROP_THRESHOLD,
                            prior_resets_in_future=(prior_5h_resets_dt > now_utc),
                        ):
                            # Pair-check dedup pre-check (spec §2.2;
                            # refined by Codex r4 P1 finding). The
                            # round-1 predicate compared only the
                            # latest event's ``post_percent`` against
                            # this tick's ``prior_5h_pct``; that
                            # false-positived on a legitimate 2nd
                            # credit where the user was idle between
                            # credits (Credit 1 lands prior=20/post=5;
                            # user does nothing; Credit 2 arrives with
                            # CLI percent=0 so prior_5h_pct=5 reads
                            # equal to stored post_percent=5 →
                            # silently swallowed). Pair-checking
                            # against BOTH fields disambiguates: a
                            # genuine replay matches BOTH; a new
                            # credit-with-idle matches at most ONE
                            # (the prior side coincides but
                            # post_percent differs).
                            most_recent = conn.execute(
                                "SELECT prior_percent, post_percent "
                                "  FROM five_hour_reset_events "
                                " WHERE five_hour_window_key = ? "
                                " ORDER BY id DESC LIMIT 1",
                                (int(five_hour_window_key),),
                            ).fetchone()
                            is_dup = (
                                most_recent is not None
                                and round(prior_5h_pct, 1)
                                == round(float(most_recent["prior_percent"]), 1)
                                and round(float(five_hour_percent), 1)
                                == round(float(most_recent["post_percent"]), 1)
                            )
                            # 10-min floor (spec §2.3 — bounded
                            # stacked-credit resolution; one event per
                            # 10-min slot per block). Resolved BEFORE
                            # the ``if not is_dup`` branch so it's in
                            # scope for the pivots below (per memory
                            # ``project_dedup_must_not_gate_side_effects.md``:
                            # the recovery-tick path must still force
                            # HWM + DELETE even when the INSERT is
                            # absorbed by the pre-check or by
                            # UNIQUE — see comment below for the
                            # crash scenario). ``_floor_to_ten_minutes``
                            # is a cctally module attribute; the
                            # ``c.X`` accessor resolves at call time
                            # so test ``monkeypatch.setitem(ns,
                            # "_floor_to_ten_minutes", …)``
                            # propagates.
                            effective_dt = c._floor_to_ten_minutes(now_utc)
                            effective_iso = effective_dt.isoformat(
                                timespec="seconds"
                            )
                            if not is_dup:
                                conn.execute(
                                    "INSERT OR IGNORE INTO five_hour_reset_events "
                                    "(detected_at_utc, five_hour_window_key, "
                                    " prior_percent, post_percent, "
                                    " effective_reset_at_utc) "
                                    "VALUES (?, ?, ?, ?, ?)",
                                    (
                                        now_utc_iso(),
                                        int(five_hour_window_key),
                                        prior_5h_pct,
                                        float(five_hour_percent),
                                        effective_iso,
                                    ),
                                )
                                conn.commit()
                            # Pivots fire UNCONDITIONALLY whenever a
                            # credit is detected — NOT gated on
                            # ``not is_dup`` and NOT on
                            # ``rowcount == 1``. Memory
                            # ``project_dedup_must_not_gate_side_effects.md``:
                            # "Skipping a no-op INSERT must NOT skip
                            # milestones/rollups/alerts; prior run may
                            # have died mid-flight." Crash scenario A:
                            # tick N committed the event row, then died
                            # before HWM + DELETE. Tick N+1's
                            # INSERT OR IGNORE returns rowcount == 0
                            # (UNIQUE absorbs) but the system is still
                            # wedged on the pre-credit HWM + stale-
                            # replica rows. Crash scenario B (the
                            # Codex r4 finding): a recovery tick where
                            # ``(prior, post)`` pair-matches the
                            # already-stored event row also takes the
                            # ``is_dup`` branch; without the hoist the
                            # pivots would be skipped and the system
                            # would stay wedged. The pivots are
                            # individually idempotent (file overwrite
                            # + DELETE on a stable predicate), so
                            # re-running them on the recovery tick is
                            # always safe. Mirrors the weekly hoist at
                            # ``_cctally_record.py`` after the
                            # ``if already is None`` block (grep
                            # ``Force-write hwm-7d``).
                            #
                            # Force-write hwm-5h: bypasses the
                            # monotonic guard at the normal hwm-5h
                            # writer below. Lands AFTER
                            # ``conn.commit()`` so a concurrent reader
                            # doesn't see the new HWM before the
                            # event row is durable. File format
                            # matches the canonical writer:
                            # ``<key> <percent>\n``.
                            try:
                                (_cctally_core.APP_DIR / "hwm-5h").write_text(
                                    f"{int(five_hour_window_key)} "
                                    f"{float(five_hour_percent)}\n"
                                )
                            except OSError:
                                pass
                            # Stale-replica DELETE (spec §4.3).
                            # Defends against claude-statusline
                            # replaying the pre-credit
                            # ``--five-hour-percent`` value past the
                            # credit moment from its own in-memory
                            # HWM cache. 1.0pp tolerance band (issue
                            # #48 — symmetric follow-up to weekly #45)
                            # around the observed pre-credit baseline
                            # absorbs any rounding drift between
                            # cctally's OAuth read and statusline's
                            # ``--five-hour-percent`` payload (today
                            # they match byte-identically, but the
                            # band future-proofs against Anthropic or
                            # statusline changing 5h rounding). The
                            # band stays well below the 5.0pp 5h
                            # in-place credit detection threshold
                            # (``_FIVE_HOUR_RESET_PCT_DROP_THRESHOLD``)
                            # — 4pp safety margin — so legitimate
                            # post-credit values are never caught.
                            # ``unixepoch()`` on both sides for offset
                            # robustness (Z vs +00:00). Bind is the
                            # in-scope ``prior_5h_pct``, which equals
                            # the just-stamped
                            # ``five_hour_reset_events.prior_percent``
                            # on the event row.
                            try:
                                conn.execute(
                                    "DELETE FROM weekly_usage_snapshots "
                                    " WHERE five_hour_window_key = ? "
                                    "   AND unixepoch(captured_at_utc) "
                                    "       >= unixepoch(?) "
                                    "   AND ABS(five_hour_percent - ?) "
                                    "       < 1.0",
                                    (
                                        int(five_hour_window_key),
                                        effective_iso,
                                        prior_5h_pct,
                                    ),
                                )
                                conn.commit()
                            except sqlite3.DatabaseError as exc:
                                eprint(
                                    "[record-usage] 5h post-credit "
                                    f"cleanup failed: {exc}"
                                )
            except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
                eprint(
                    f"[record-usage] 5h in-place-credit detection "
                    f"failed: {exc}"
                )
        except (sqlite3.DatabaseError, ValueError) as exc:
            eprint(f"[record-usage] reset-event detection failed: {exc}")

        # 7-day usage is monotonically non-decreasing within a billing week
        # — UNTIL an in-place weekly credit lowers it. There are TWO credit
        # shapes and BOTH must floor this clamp, or a real post-credit tick
        # is suppressed and never stored:
        #   (a) an Anthropic mid-week reset / >=25pp auto-credit writes a
        #       `week_reset_events` row (it also re-anchors the window); and
        #   (b) a manual `record-credit` partial credit writes a
        #       `weekly_credit_floors` row WITHOUT re-anchoring the week
        #       (record-credit M2, #209).
        # `_reset_aware_floor` returns the LATEST in-week effective across
        # both legs. The MAX query then filters to samples captured at-or-
        # after that floor, so a fresh post-credit OAuth value (e.g. 37%
        # after a 46->31 credit) lands instead of being held back by stale
        # pre-credit history (46%). Without the credit-floor leg, the 37%
        # tick is `round(37,1) < round(46,1)` -> should_insert=False ->
        # never stored, cascading to every latest-snapshot surface
        # (the M2 linchpin; spec §4a, test S13).
        # When neither leg has a row, the floor is None -> '1970-...' epoch-
        # zero default -> the filter is a no-op and legacy clamp behavior is
        # preserved byte-identically.
        # NB: comparison wrapped with ``unixepoch()`` on BOTH sides.
        # ``captured_at_utc`` is stored with `Z` suffix, but the floor may
        # carry a non-UTC / +00:00 offset spelling. Lex string compare on
        # mixed offsets silently mis-orders moments for non-UTC hosts
        # (CLAUDE.md gotcha: 5h-block cross-reset flag — "all comparisons go
        # through unixepoch(), NOT lex BETWEEN/`<`/`>`"). Same rule here, and
        # inside `_reset_aware_floor`'s own ORDER BY.
        clamp_floor_iso = _reset_aware_floor(
            conn, week_start_date, week_start_at, week_end_at,
        ) or "1970-01-01T00:00:00Z"
        max_row = conn.execute(
            """
            SELECT MAX(weekly_percent) AS v
              FROM weekly_usage_snapshots
             WHERE week_start_date = ?
               AND unixepoch(captured_at_utc) >= unixepoch(?)
            """,
            (week_start_date, clamp_floor_iso),
        ).fetchone()
        if hwm_clamp_applies(weekly_percent, max_row["v"] if max_row else None):
            should_insert = False
        else:
            # 5-hour usage is monotonically non-decreasing within a window
            # — UNTIL an in-place 5h credit fires. When a
            # ``five_hour_reset_events`` row exists for THIS
            # ``five_hour_window_key``, the MAX query filters to samples
            # captured at-or-after the event's ``effective_reset_at_utc``
            # so a fresh post-credit OAuth value (e.g. 4%) lands instead
            # of being re-clamped to the pre-credit max (e.g. 28%). When
            # no event row exists, ``COALESCE`` defaults to epoch-zero so
            # the filter is a no-op and legacy clamp behavior is preserved
            # byte-identically.
            #
            # ``unixepoch()`` on BOTH sides for offset robustness — stored
            # ``captured_at_utc`` carries ``Z`` while
            # ``effective_reset_at_utc`` carries ``+00:00``. Lex compare
            # would silently mis-order moments for non-UTC hosts (same
            # gotcha as the weekly clamp / 5h-block cross-reset flag).
            #
            # Joining on ``five_hour_window_key`` (canonical 10-min-floored
            # epoch) absorbs Anthropic's seconds-level jitter on
            # ``resets_at``; an ISO-string equality at this site silently
            # skipped the clamp every time a jittered fetch landed in
            # the same physical 5h window (spec Bug B).
            #
            # Spec §4.1 of
            # docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md.
            if five_hour_percent is not None and five_hour_window_key is not None:
                max_5h_row = conn.execute(
                    """
                    SELECT MAX(five_hour_percent) AS v
                      FROM weekly_usage_snapshots
                     WHERE five_hour_window_key = ?
                       AND unixepoch(captured_at_utc) >= unixepoch(COALESCE(
                         (SELECT effective_reset_at_utc
                            FROM five_hour_reset_events
                           WHERE five_hour_window_key = ?
                           ORDER BY id DESC
                           LIMIT 1),
                         '1970-01-01T00:00:00Z'
                       ))
                    """,
                    (int(five_hour_window_key), int(five_hour_window_key)),
                ).fetchone()
                if hwm_clamp_applies(
                    five_hour_percent, max_5h_row["v"] if max_5h_row else None
                ):
                    five_hour_percent = float(max_5h_row["v"])

            # Dedup vs last snapshot: if BOTH weekly_percent and
            # five_hour_percent are unchanged from the most recent row in
            # this week, swallow the insert. Tests of the 5h clamp must
            # vary --percent (or --five-hour-percent) between calls, or
            # the second call is dropped here before the clamp even runs
            # — see bin/cctally-5h-canonical-test scenario B.
            last = conn.execute(
                """
                SELECT weekly_percent, five_hour_percent
                FROM weekly_usage_snapshots
                WHERE week_start_date = ?
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT 1
                """,
                (week_start_date,),
            ).fetchone()
            if last is not None:
                if float(last["weekly_percent"]) == weekly_percent:
                    last_5h = last["five_hour_percent"]
                    if five_hour_percent is None or (
                        last_5h is not None and float(last_5h) == five_hour_percent
                    ):
                        should_insert = False

        # No backfill of 5h data on existing milestones — we don't have
        # authentic crossing-time values for them.  New milestones created
        # by the status line path will have 5h data set at creation time
        # via maybe_record_milestone().
    finally:
        conn.close()

    if not should_insert:
        # Self-heal: a prior record-usage invocation may have inserted
        # the snapshot but been killed (CC self-update, machine sleep,
        # OOM) before maybe_record_milestone / maybe_update_five_hour_block
        # could run. Pre-probe both surfaces with cheap indexed SELECTs
        # and only invoke the helpers when a row is actually missing or
        # stale. Steady-state cost: 1-3 SELECTs (latest snapshot always;
        # +max_milestone if floor>=1; +block last_observed if window_key
        # is set); ZERO JSONL re-ingest on healthy ticks. The helpers themselves are idempotent under
        # concurrent record-usage instances (INSERT OR IGNORE for
        # percent_milestones; SQLite write-lock serialization for the
        # 5h upsert). Without the pre-probe, every dedup tick would
        # trigger sync_cache + a window walk + replace-all rollups via
        # maybe_update_five_hour_block's unconditional _compute_block_totals
        # call. Regression: bin/cctally-record-usage-selfheal-test.
        try:
            heal_conn = open_db()
            try:
                latest_row = heal_conn.execute(
                    "SELECT * FROM weekly_usage_snapshots "
                    "WHERE week_start_date = ? "
                    "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
                    (week_start_date,),
                ).fetchone()
                if latest_row is None:
                    return 0
                latest_saved = _saved_dict_from_usage_row(latest_row)

                # Probe 1: do we owe a percent milestone? Snap up before
                # floor (status-line API returns 0.N*100 which can fall
                # one ULP short of N — same convention as
                # maybe_record_milestone).
                latest_floor = math.floor(
                    float(latest_row["weekly_percent"]) + 1e-9
                )
                need_milestone_heal = False
                if latest_floor >= 1:
                    # v1.7.2: scope the heal probe to the ACTIVE segment.
                    # Without this, a credited week's MAX over the whole
                    # ledger would still read the pre-credit ceiling
                    # (e.g. 67%) and silently suppress the post-credit
                    # ledger's heal even though it has zero rows.
                    captured_at_for_probe = latest_row["captured_at_utc"]
                    week_end_at_for_probe = latest_row["week_end_at"]
                    heal_segment = 0
                    if week_end_at_for_probe and captured_at_for_probe:
                        seg = heal_conn.execute(
                            "SELECT id FROM week_reset_events "
                            "WHERE new_week_end_at = ? "
                            "  AND unixepoch(effective_reset_at_utc) <= unixepoch(?) "
                            "ORDER BY id DESC LIMIT 1",
                            (week_end_at_for_probe, captured_at_for_probe),
                        ).fetchone()
                        if seg is not None:
                            heal_segment = int(seg["id"])
                    max_existing = heal_conn.execute(
                        "SELECT MAX(percent_threshold) AS m "
                        "FROM percent_milestones "
                        "WHERE week_start_date = ? AND reset_event_id = ?",
                        (week_start_date, heal_segment),
                    ).fetchone()
                    existing_m = (
                        int(max_existing["m"])
                        if max_existing and max_existing["m"] is not None
                        else None
                    )
                    if milestone_coverage_owes(existing_m, latest_floor):
                        need_milestone_heal = True

                # Probe 2: do we owe a 5h-block update? Either no row
                # for this canonical window, or the existing row's
                # last_observed_at_utc is stale relative to the latest
                # snapshot's captured_at_utc (the kill landed between
                # insert_usage_snapshot and maybe_update_five_hour_block).
                #
                # Round-3: ALSO scope the milestone-coverage half of
                # this probe by ACTIVE 5h SEGMENT. Without this, a
                # credited block's MAX over the whole milestone ledger
                # would still read the pre-credit ceiling (e.g. 28%) and
                # silently suppress the post-credit ledger's heal even
                # though it has zero rows. Mirrors weekly Probe 1's
                # segment-aware fix above. Uses
                # ``_resolve_active_five_hour_reset_event_id`` to find
                # the active segment for the latest snapshot's window.
                need_5h_heal = False
                incoming_block_saved: dict[str, Any] | None = None
                window_key = latest_row["five_hour_window_key"]
                if window_key is not None:
                    block_row = heal_conn.execute(
                        "SELECT last_observed_at_utc "
                        "FROM five_hour_blocks "
                        "WHERE five_hour_window_key = ?",
                        (int(window_key),),
                    ).fetchone()
                    if block_row is None:
                        need_5h_heal = True
                    elif (
                        block_row["last_observed_at_utc"]
                        < latest_row["captured_at_utc"]
                    ):
                        need_5h_heal = True
                    else:
                        # Block row exists AND last_observed is fresh
                        # — but the post-credit milestone segment may
                        # still owe rows. Scope MAX(percent_threshold)
                        # by the active reset_event_id segment so
                        # post-credit climbs from threshold 1 trigger
                        # heal even when the pre-credit segment already
                        # crossed higher thresholds. Probe shape mirrors
                        # weekly Probe 1 (lines 1922-1956).
                        five_hour_percent_for_probe = latest_row[
                            "five_hour_percent"
                        ]
                        if five_hour_percent_for_probe is not None:
                            latest_5h_floor = math.floor(
                                float(five_hour_percent_for_probe) + 1e-9
                            )
                            if latest_5h_floor >= 1:
                                heal_5h_segment = (
                                    _resolve_active_five_hour_reset_event_id(
                                        heal_conn, int(window_key)
                                    )
                                )
                                max_5h_existing = heal_conn.execute(
                                    "SELECT MAX(percent_threshold) AS m "
                                    "FROM five_hour_milestones "
                                    "WHERE five_hour_window_key = ? "
                                    "  AND reset_event_id = ?",
                                    (int(window_key), heal_5h_segment),
                                ).fetchone()
                                existing_5h_m = (
                                    int(max_5h_existing["m"])
                                    if max_5h_existing
                                    and max_5h_existing["m"] is not None
                                    else None
                                )
                                if milestone_coverage_owes(
                                    existing_5h_m, latest_5h_floor
                                ):
                                    need_5h_heal = True
                # Window-rollover heal: this dedup tick observed a NEW 5h
                # window (its canonical ``five_hour_window_key`` differs
                # from the latest STORED snapshot's) whose
                # ``five_hour_blocks`` anchor does not exist yet. The dedup
                # above swallowed the snapshot insert because the weekly/5h
                # percents were flat, so ``latest_row`` still points at the
                # PREVIOUS window and the ``need_5h_heal`` probe only ever
                # checked that old (still-fresh) window — leaving the
                # current window unanchored. Materialize the missing block
                # now so ``blocks`` and the dashboard anchor the ACTIVE
                # block to its API-derived window instead of falling back to
                # the heuristic "~" until the percent next moves (the
                # statusline is unaffected — it renders the live
                # rate_limits, not the DB). Block-only: NO snapshot is
                # inserted (the tick stays deduped); the "latest snapshot"
                # weekly/5h surfaces and monotonicity clamps are untouched.
                # ``maybe_update_five_hour_block`` is an upsert keyed on
                # ``five_hour_window_key``, so later flat ticks in the same
                # window re-run it as an idempotent no-op.
                if (
                    five_hour_window_key is not None
                    and five_hour_percent is not None
                    and five_hour_resets_at_str is not None
                    and (
                        window_key is None
                        or int(window_key) != int(five_hour_window_key)
                    )
                ):
                    incoming_block_row = heal_conn.execute(
                        "SELECT 1 FROM five_hour_blocks "
                        "WHERE five_hour_window_key = ? LIMIT 1",
                        (int(five_hour_window_key),),
                    ).fetchone()
                    if incoming_block_row is None:
                        incoming_block_saved = {
                            # ``id`` is extracted-but-unused by
                            # maybe_update_five_hour_block; reuse latest_row's
                            # for output-dict shape parity with a real insert.
                            "id": int(latest_row["id"]),
                            "capturedAt": now_utc_iso(),
                            "weeklyPercent": weekly_percent,
                            "fiveHourPercent": five_hour_percent,
                            "fiveHourResetsAt": five_hour_resets_at_str,
                            "fiveHourWindowKey": int(five_hour_window_key),
                        }
            finally:
                heal_conn.close()

            if need_milestone_heal or need_5h_heal or incoming_block_saved:
                if need_milestone_heal:
                    try:
                        maybe_record_milestone(latest_saved)
                    except Exception as exc:
                        eprint(f"[milestone] self-heal error: {exc}")
                if need_5h_heal:
                    try:
                        maybe_update_five_hour_block(latest_saved)
                    except Exception as exc:
                        eprint(f"[5h-block] self-heal error: {exc}")
                if incoming_block_saved is not None:
                    try:
                        maybe_update_five_hour_block(incoming_block_saved)
                    except Exception as exc:
                        eprint(f"[5h-block] window-rollover heal error: {exc}")

            # Dollar-decoupled axes (budget / project-budget / projected) heal on
            # EVERY dedup tick — USD spend can cross a $ threshold while the
            # weekly/5h percent is flat (so should_insert is False), and a prior
            # run may have died after insert_usage_snapshot but before the
            # post-insert axis block. Each helper gates first on config (a cheap
            # read for non-users) and pre-probes its recorded set before any cost
            # scan, so non-budget users pay ~nothing here. Order matches the
            # post-insert block so the projected budget_usd leg's skip_sync=True
            # cache-warming dependency on the actual-budget axis still holds.
            # [Dedup mustn't gate side effects]
            for _heal_fn, _heal_tag in (
                (maybe_record_budget_milestone, "budget-milestone"),
                (maybe_record_project_budget_milestone,
                 "project-budget-milestone"),
                (maybe_record_codex_budget_milestone,
                 "codex-budget-milestone"),
                (maybe_record_projected_alert, "projected-alert"),
            ):
                try:
                    _heal_fn(latest_saved)
                except Exception as exc:
                    eprint(f"[{_heal_tag}] self-heal error: {exc}")
        except Exception as exc:
            eprint(f"[record-usage] self-heal lookup failed: {exc}")
        return 0

    payload = {
        # Record the true feeder. Defaults to "statusline" for the public
        # `record-usage` CLI (preserves prior behavior); the OAuth callers
        # (_hook_tick_oauth_refresh / _refresh_usage_inproc) pass "api", and
        # the statusline persist feeder passes "statusline". Previously this
        # was hard-coded "statusline" for EVERY caller, mislabeling OAuth
        # rows (spec §5). No migration — the column already exists.
        "source": getattr(args, "source", "statusline"),
        "capturedAt": now_utc_iso(),
        "weeklyPercent": weekly_percent,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": week_end_at,
    }
    if five_hour_percent is not None:
        payload["fiveHourPercent"] = five_hour_percent
    if five_hour_resets_at_str is not None:
        payload["fiveHourResetsAt"] = five_hour_resets_at_str
    if five_hour_window_key is not None:
        payload["fiveHourWindowKey"] = five_hour_window_key

    saved = insert_usage_snapshot(payload, week_start_name)
    try:
        maybe_record_milestone(saved)
    except Exception as exc:
        eprint(f"[milestone] unexpected error: {exc}")

    # NEW: 5h-block rollup (paired with maybe_record_milestone for 7d).
    # The helper performs an opportunistic JOIN against week_reset_events
    # every tick to flag any open block whose interval contains a recorded
    # reset; no per-call plumbing needed (self-healing).
    try:
        maybe_update_five_hour_block(saved)
    except Exception as exc:
        eprint(f"[5h-block] unexpected error: {exc}")

    # NEW: equiv-$ budget alert firing (Approach A, issue #19). Gated on a
    # set budget + alerts_enabled FIRST — non-budget users pay zero overhead.
    try:
        maybe_record_budget_milestone(saved)
    except Exception as exc:
        eprint(f"[budget-milestone] unexpected error: {exc}")

    # NEW: per-project equiv-$ budget alert firing (axis `project_budget`,
    # #19/#121). Runs AFTER the global budget axis, but the per-project scan is
    # self-sufficient — it passes skip_sync=False so a project-only user (no
    # global budget warming the cache) still resolves live spend on this tick.
    # Gated FIRST on a non-empty budget.projects + project_alerts_enabled — non-
    # users pay only one config read.
    try:
        maybe_record_project_budget_milestone(saved)
    except Exception as exc:
        eprint(f"[project-budget-milestone] unexpected error: {exc}")

    # NEW: Codex budget alert firing (axis `codex_budget`, calendar-period-codex-
    # budgets). Gated FIRST on a configured budget.codex + alerts_enabled — non-
    # Codex-budget users pay only one config read. Codex usage never flows through
    # record-usage, so this is one of the two firing triggers (the other is the
    # opportunistic fire on `cctally budget`); forward-only/fire-once means the
    # double-trigger never double-fires.
    try:
        maybe_record_codex_budget_milestone(saved)
    except Exception as exc:
        eprint(f"[codex-budget-milestone] unexpected error: {exc}")

    # NEW: projected-pace alert firing (axis `projected`, #121/#135). Runs in
    # its OWN detect-and-arm AFTER the weekly/5h/budget/codex blocks; gated up
    # front on (alerts.enabled && alerts.projected_enabled) ||
    # (_budget_alerts_active && budget.projected_enabled — ANY Claude period,
    # #135) || (codex.alerts_enabled && codex.projected_enabled, #135) — all
    # toggles default OFF, so non-projected users pay only a cheap config read.
    # No only_metrics on the record path: every enabled leg runs. The Codex leg
    # relies on the codex-budget block above (:3129) having warmed the cache,
    # but is robust even if it short-circuited (skip_sync=False self-syncs, R5).
    try:
        maybe_record_projected_alert(saved)
    except Exception as exc:
        eprint(f"[projected-alert] unexpected error: {exc}")

    # Write high-water mark so the status line never displays a regression.
    # The file contains "week_start_date weekly_percent" on one line.
    try:
        hwm_path = _cctally_core.APP_DIR / "hwm-7d"
        existing_hwm = 0.0
        try:
            parts = hwm_path.read_text().strip().split()
            if len(parts) == 2 and parts[0] == week_start_date:
                existing_hwm = float(parts[1])
        except (FileNotFoundError, ValueError, OSError):
            pass
        if hwm_file_next(existing_hwm, weekly_percent) is not None:
            hwm_path.write_text(f"{week_start_date} {weekly_percent}\n")
    except OSError:
        pass

    # Symmetric 5h HWM. Keyed by the canonical five_hour_window_key derived
    # under the prior-anchor logic above (NOT a fresh pure-floor recompute) so
    # boundary-straddling jitter writes to the same file key as the matching
    # DB row. File format: "<canonical_5h_window_key> <percent>".
    if (
        five_hour_percent is not None
        and five_hour_window_key is not None
    ):
        try:
            five_resets_key = five_hour_window_key
            hwm5_path = _cctally_core.APP_DIR / "hwm-5h"
            existing_hwm5 = 0.0
            try:
                parts5 = hwm5_path.read_text().strip().split()
                if len(parts5) == 2 and parts5[0] == str(five_resets_key):
                    existing_hwm5 = float(parts5[1])
            except (FileNotFoundError, ValueError, OSError):
                pass
            if hwm_file_next(existing_hwm5, five_hour_percent) is not None:
                hwm5_path.write_text(f"{five_resets_key} {five_hour_percent}\n")
        except OSError:
            pass

    return 0


def _hook_tick_log_line(line: str) -> None:
    """Append one line to hook-tick.log; create dir if missing.

    Uses O_APPEND so concurrent writers' sub-PIPE_BUF lines don't interleave.
    Best-effort: any IO error is silently swallowed (hook discipline).
    """
    c = _cctally()
    try:
        _cctally_core.HOOK_TICK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(_cctally_core.HOOK_TICK_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _hook_tick_log_rotate_if_needed() -> None:
    """If hook-tick.log exceeds the size cap, atomic-rename to .1 (overwriting)."""
    c = _cctally()
    try:
        size = _cctally_core.HOOK_TICK_LOG_PATH.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if size <= c.HOOK_TICK_LOG_ROTATE_BYTES:
        return
    try:
        os.replace(_cctally_core.HOOK_TICK_LOG_PATH, _cctally_core.HOOK_TICK_LOG_ROTATED_PATH)
    except OSError:
        pass


def _hook_tick_throttle_age_seconds() -> float:
    """Return seconds since last successful OAuth fetch; +inf if never."""
    c = _cctally()
    try:
        mtime = _cctally_core.HOOK_TICK_THROTTLE_PATH.stat().st_mtime
    except FileNotFoundError:
        return float("inf")
    except OSError:
        return float("inf")
    return max(0.0, time.time() - mtime)


def _hook_tick_throttle_touch() -> None:
    """Update mtime to now (creating the file if missing)."""
    c = _cctally()
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        _cctally_core.HOOK_TICK_THROTTLE_PATH.touch(exist_ok=True)
        os.utime(_cctally_core.HOOK_TICK_THROTTLE_PATH, None)
    except OSError:
        pass


# =========================================================================
# Statusline selected/transport markers + OAuth backoff deadline (#318)
# =========================================================================
#
# Two independent markers with two DIFFERENT time encodings:
#   - The selected-observation marker is MTIME-based and represents an actual
#     selected DB change or authoritative OAuth confirmation.
#   - The transport marker is also MTIME-based and represents an eligible
#     regular-pool candidate reaching the spool; it never throttles OAuth.
#   - The OAuth backoff marker is CONTENT-based: it stores a FUTURE absolute
#     epoch deadline as text. Its mtime is meaningless (~now). Encoding the
#     deadline as mtime would future-date the file and, if ever confused
#     with a throttle marker, corrupt an mtime-age reading — hence the
#     deliberate split (Codex P1-3).


def _marker_age(path) -> float:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return float("inf")
    except OSError:
        return float("inf")
    return max(0.0, time.time() - mtime)


def _touch_marker(path) -> None:
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        os.utime(path, None)
    except OSError:
        pass


def _statusline_observe_age_seconds() -> float:
    """Seconds since selected/authoritative usage changed; +inf if never."""
    return _marker_age(_cctally_core.STATUSLINE_OBSERVE_MARKER_PATH)


def _statusline_observe_touch() -> None:
    """Mark selected usage freshness after a proven selected transition.

    The historical public helper names remain aliases for selected freshness.
    """
    _touch_marker(_cctally_core.STATUSLINE_OBSERVE_MARKER_PATH)


def _statusline_transport_age_seconds() -> float:
    """Seconds since an eligible regular-pool candidate reached the spool."""
    return _marker_age(_cctally_core.STATUSLINE_TRANSPORT_MARKER_PATH)


def _statusline_transport_touch() -> None:
    """Mark regular-pool statusline transport after an atomic candidate write."""
    _touch_marker(_cctally_core.STATUSLINE_TRANSPORT_MARKER_PATH)


def _oauth_backoff_remaining_seconds() -> float:
    """Seconds until the shared OAuth 429 backoff deadline; ``0.0`` when the
    marker is absent, empty, malformed, or already elapsed.

    Reads the ABSOLUTE epoch deadline from the marker's text CONTENT (not
    its mtime) and returns ``max(0.0, deadline - now)``."""
    try:
        raw = _cctally_core.OAUTH_BACKOFF_MARKER_PATH.read_text()
    except FileNotFoundError:
        return 0.0
    except OSError:
        return 0.0
    try:
        deadline = float(raw.strip())
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, deadline - time.time())


def _oauth_backoff_set(deadline_epoch: float) -> None:
    """Persist the shared OAuth 429 backoff deadline (absolute epoch).

    Never SHORTENS an existing deadline: writes ``max(deadline_epoch,
    existing_deadline)`` so concurrent/repeated 429s keep the furthest-out
    cooldown. The write is atomic (tmp + ``os.replace``) so a reader never
    sees a half-written file. Best-effort — any OSError is swallowed."""
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Compare against the existing ABSOLUTE deadline, not the remaining
    # seconds, so "never shorten" holds regardless of when we read it.
    existing_abs = 0.0
    try:
        existing_raw = _cctally_core.OAUTH_BACKOFF_MARKER_PATH.read_text()
        existing_abs = float(existing_raw.strip())
    except (FileNotFoundError, OSError, TypeError, ValueError):
        existing_abs = 0.0
    target = max(float(deadline_epoch), existing_abs)
    path = _cctally_core.OAUTH_BACKOFF_MARKER_PATH
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(f"{target:.6f}")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _oauth_backoff_clear() -> None:
    """Remove the OAuth backoff deadline marker (ignore if absent)."""
    try:
        _cctally_core.OAUTH_BACKOFF_MARKER_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _oauth_backoff_count() -> int:
    """The consecutive-429 count (0 when absent/malformed)."""
    try:
        raw = _cctally_core.OAUTH_BACKOFF_COUNT_PATH.read_text()
    except (FileNotFoundError, OSError):
        return 0
    try:
        return max(0, int(raw.strip()))
    except (TypeError, ValueError):
        return 0


def _oauth_backoff_register_429(*, retry_after_deadline, now) -> float:
    """Record a 429: set/extend the shared backoff deadline and bump the
    consecutive-429 counter. Returns the effective deadline (absolute epoch).

    Policy (spec §4):
      - A valid ``Retry-After`` (``retry_after_deadline`` is not None) is used
        verbatim.
      - Otherwise conservative exponential backoff:
        ``now + min(CAP, BASE * 2**consecutive_429)``.
      - ``_oauth_backoff_set`` keeps the MAX, so concurrent/repeated 429s
        never shorten the cooldown."""
    count = _oauth_backoff_count()
    if retry_after_deadline is not None:
        deadline = float(retry_after_deadline)
    else:
        base = float(_cctally_core.OAUTH_BACKOFF_BASE_SECONDS)
        cap = float(_cctally_core.OAUTH_BACKOFF_CAP_SECONDS)
        # Clamp the exponent so a corrupt/huge counter can't overflow 2**n.
        exp = 2 ** min(count, 30)
        deadline = float(now) + min(cap, base * exp)
    _oauth_backoff_set(deadline)
    # Bump the counter atomically (best-effort).
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        path = _cctally_core.OAUTH_BACKOFF_COUNT_PATH
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(count + 1))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except (OSError, NameError, UnboundLocalError):
            pass
    return deadline


def _oauth_backoff_reset() -> None:
    """Clear the backoff deadline AND the consecutive-429 counter — called on
    any successful OAuth API response (spec §4)."""
    _oauth_backoff_clear()
    try:
        _cctally_core.OAUTH_BACKOFF_COUNT_PATH.unlink()
    except (FileNotFoundError, OSError):
        pass


def _hook_tick_read_stdin_event(stdin_max_bytes: int = 32 * 1024) -> dict:
    """Read CC's hook payload (JSON on stdin). Best-effort.

    Returns dict with keys event, session_id, transcript_path, cwd —
    every value is a string (or "unknown"). Never raises.
    """
    out = {"event": "unknown", "session_id": "unknown", "transcript_path": "", "cwd": ""}
    try:
        data = sys.stdin.buffer.read(stdin_max_bytes)
    except (OSError, ValueError):
        return out
    if not data:
        return out
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return out
    if not isinstance(payload, dict):
        return out
    out["event"] = str(payload.get("hook_event_name") or "unknown")
    sid = payload.get("session_id")
    out["session_id"] = str(sid) if isinstance(sid, str) else "unknown"
    tp = payload.get("transcript_path")
    out["transcript_path"] = str(tp) if isinstance(tp, str) else ""
    cwd = payload.get("cwd")
    out["cwd"] = str(cwd) if isinstance(cwd, str) else ""
    return out


def _hook_tick_session_short(sid: str) -> str:
    """First 8 chars of a session id, sanitized for log lines."""
    if not sid or sid == "unknown":
        return "unknown"
    return "".join(c for c in sid[:8] if c.isalnum() or c in "-_")


def _hook_tick_format_log_line(
    event: str, session: str, ingested: int, oauth_status: str, dur_ms: int,
    *, malformed: int = 0, skipped: int = 0,
) -> str:
    ts = now_utc_iso()
    return (
        f"{ts} event={event:14s} session={session} "
        f"ingested={ingested} malformed={malformed} skipped={skipped} "
        f"oauth={oauth_status} dur_ms={dur_ms}"
    )


def _codex_lifecycle_log_line(
    *, source_root_key: str, event: str, sync: str, result: str,
    blocks: int, milestones: int, alert_eligible_roots: int,
    quota_alerts: int, budget_alerts: int, dur_ms: int,
) -> str:
    """Render one privacy-safe root-qualified Codex lifecycle outcome.

    Native hook input can contain session paths and conversation identifiers;
    this durable diagnostic deliberately carries only the bounded event label,
    opaque source root key, aggregate reconciliation counts, and duration.
    """
    safe_event = "".join(
        char for char in str(event)[:40] if char.isalnum() or char in "-_"
    ) or "unknown"
    return (
        f"{now_utc_iso()} provider=codex source_root_key={source_root_key} "
        f"event={safe_event} sync={sync} blocks={int(blocks)} "
        f"milestones={int(milestones)} "
        f"alert_eligible_roots={int(alert_eligible_roots)} "
        f"quota_alerts={int(quota_alerts)} budget_alerts={int(budget_alerts)} "
        f"dur_ms={max(0, int(dur_ms))} result={result}"
    )


def _codex_lifecycle_roots():
    """Snapshot usable configured Codex homes in stable root-key order."""
    return codex_hook_roots(_cctally()._codex_home_roots())


def _cmd_hook_tick_codex(args: argparse.Namespace, *, event: str = "unknown") -> int:
    """Run one quiet, foreground Codex lifecycle tick.

    Native Codex Stop/SubagentStop hooks may fire concurrently.  Per-root
    lifecycle locks narrow alert eligibility while the one S1 cache sync and
    reporting reconciliation still observe the complete active root set.
    """
    c = _cctally()
    roots = _codex_lifecycle_roots()
    locks = acquire_due_lifecycle_locks(
        _cctally_core.APP_DIR,
        roots,
        now=time.time(),
        throttle_seconds=CODEX_HOOK_THROTTLE_SECONDS,
    )
    if not locks:
        return 0
    all_root_keys = tuple(root.source_root_key for root in roots)
    due_root_keys = tuple(lock.root.source_root_key for lock in locks)
    started_at = time.monotonic()

    def log_outcome(
        *, sync: str, result: str, projection=None, budget_alerts: int = 0,
    ) -> None:
        blocks = int(getattr(projection, "blocks_upserted", 0) or 0)
        milestones = int(getattr(projection, "milestones_upserted", 0) or 0)
        quota_alerts = int(getattr(projection, "alerts_dispatched", 0) or 0)
        dur_ms = int((time.monotonic() - started_at) * 1000)
        for lock in locks:
            _hook_tick_log_line(_codex_lifecycle_log_line(
                source_root_key=lock.root.source_root_key,
                event=event,
                sync=sync,
                result=result,
                blocks=blocks,
                milestones=milestones,
                alert_eligible_roots=len(due_root_keys),
                quota_alerts=quota_alerts,
                budget_alerts=budget_alerts,
                dur_ms=dur_ms,
            ))
        _hook_tick_log_rotate_if_needed()

    try:
        # Hook stdout/stderr is contractually silent.  Cache migration and
        # ingest diagnostics remain available to explicit CLI operations.
        with open(os.devnull, "w", encoding="utf-8") as quiet, \
                contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            cache = c.open_cache_db()
            try:
                stats = c.sync_codex_cache(cache, lock_timeout=0)
            finally:
                cache.close()
            if stats.lock_contended:
                log_outcome(sync="contended", result="noop")
                return 0
            projection = c.reconcile_codex_quota_projection(
                source_root_keys=all_root_keys,
                alert_eligible_root_keys=due_root_keys,
                now=dt.datetime.now(dt.timezone.utc),
            )
            # Vendor-scoped spend is intentionally evaluated once per
            # successful due-set tick, not once per root.
            budget_alerts = c.maybe_record_codex_budget_milestone(
                {}, raise_errors=True,
            )
        mark_lifecycle_success(locks)
        log_outcome(
            sync="ok", result="success", projection=projection,
            budget_alerts=budget_alerts,
        )
    except Exception:
        # A failed sync, projection, or budget evaluation must acknowledge no
        # root.  Hooks are best-effort and remain a successful no-op to Codex.
        log_outcome(sync="error", result="error")
        return 0
    finally:
        release_lifecycle_locks(locks)
    return 0


def cmd_hook_tick(args: argparse.Namespace) -> int:
    """Per-fire hook runtime (Section 3 of onboarding spec).

    Normal mode: reads stdin, detaches stdout/stderr to log file, runs
    sync_cache + (throttled) OAuth refresh, writes one log line, returns 0
    UNCONDITIONALLY (even on internal failure — hook discipline).

    --foreground mode: reads stdin and runs the normal best-effort body in the
    current process without detaching. --explain mode is synchronous, prints
    to stdout, and returns an informative exit code.
    """
    c = _cctally()
    source = getattr(args, "source", "claude")
    if source == "codex":
        # Codex's native handler always uses --foreground.  Drain stdin before
        # any further decision so its event payload is never lost to detaching
        # shell semantics; the lifecycle body itself is intentionally quiet.
        meta = _hook_tick_read_stdin_event()
        # The production reader always returns a mapping, but this boundary is
        # deliberately best-effort: hook callers and lightweight lifecycle
        # probes may only drain stdin.  Do not turn an absent payload into a
        # hook failure merely because event observability is unavailable.
        event = meta.get("event", "unknown") if isinstance(meta, dict) else "unknown"
        return _cmd_hook_tick_codex(args, event=event)
    explain = bool(getattr(args, "explain", False))
    foreground = bool(getattr(args, "foreground", False))
    no_oauth = bool(getattr(args, "no_oauth", False))
    # Use an explicit `is None` check so `--throttle-seconds 0` survives the
    # default-fallback (a `0 or DEFAULT` short-circuit would silently drop
    # the override and reapply the configured window — defeats the purpose
    # of the zero-second escape hatch).
    override = getattr(args, "throttle_seconds", None)
    if override is not None:
        throttle_seconds = float(override)
    else:
        try:
            _cfg = _get_oauth_usage_config(load_config())
            throttle_seconds = float(_cfg["throttle_seconds"])
        except sys.modules["cctally"].OauthUsageConfigError:
            throttle_seconds = float(c.HOOK_TICK_DEFAULT_THROTTLE_SECONDS)

    # --- Step 1: read stdin (before detach OR fork) ---
    # CRITICAL: stdin must be read BEFORE we fork. POSIX (XCU §2.9.3) says
    # async commands (`cmd &`) in non-interactive shells get stdin redirected
    # to /dev/null; we previously relied on shell `&` which blanked the
    # hook payload. Now the settings.json command is bare and we fork here
    # ourselves — but stdin still has to be drained first.
    forced_event = getattr(args, "event", None)
    if explain:
        meta = {"event": forced_event or "explain", "session_id": "explain",
                "transcript_path": "", "cwd": ""}
    else:
        meta = _hook_tick_read_stdin_event()
        if forced_event:
            meta["event"] = forced_event

    # --- Step 1b: fork to background so CC's hook returns immediately ---
    # Parent returns 0 right away; child carries on with sync_cache + OAuth.
    # If fork fails (rare: out of pids/memory), fall back to running the
    # body inline — the parent process must NOT be misclassified as a
    # forked child, otherwise os.setsid() would detach the parent's
    # controlling terminal and os._exit(0) at function end would kill it
    # mid-stack.
    forked = False
    pid = 0
    if not explain and not foreground:
        try:
            pid = os.fork()
            forked = True
        except OSError:
            pass
        if forked and pid > 0:
            # Parent of a successful fork: CC unblocks immediately.
            return 0
        # Either: child of successful fork, OR inline fallback after fork failure.
        if forked:
            # Detach from parent's session so SIGHUP from CC doesn't kill us.
            try:
                os.setsid()
            except OSError:
                pass

    # --- Step 2: detach stdio (forked child OR inline fallback after fork failure) ---
    # In the inline-fallback path the parent process re-routes its own stdout/
    # stderr to the log file for the rest of its short life. Function returns
    # immediately after Step 7, so the leak is bounded.
    if not explain and not foreground:
        try:
            _cctally_core.HOOK_TICK_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_fd = os.open(
                _cctally_core.HOOK_TICK_LOG_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644,
            )
            os.dup2(log_fd, 1)  # stdout
            os.dup2(log_fd, 2)  # stderr
            os.close(log_fd)
            try:
                devnull = os.open(os.devnull, os.O_RDONLY)
                os.dup2(devnull, 0)
                os.close(devnull)
            except OSError:
                pass
        except OSError:
            pass  # log redirect failed; carry on silently

    # --- Steps 3-7: wrap remainder in try/except (always exit 0 in normal mode) ---
    start = time.monotonic()
    ingested = 0
    # #279 S2 F1: parse-health counters from the local sync, surfaced on the
    # hook-tick log line (uniform fields — always emitted, defaulted to 0).
    parse_malformed = 0
    parse_skipped = 0
    oauth_status = "skipped-no-oauth" if no_oauth else "throttled(age=?s)"
    # Pre-fetch throttle state captured for --explain output. The OAuth
    # block re-touches the throttle marker after a successful fetch, so
    # re-reading age there would print `mtime: 0s ago → skip` even when
    # the call we just made was a fetch. Freeze the values at decision
    # time. `pre_age` is read once now (covers --no-oauth / lock-failure
    # paths); the throttle block below re-assigns it under flock for the
    # OAuth-active path so the explain output matches the actual decision.
    pre_age: float = _hook_tick_throttle_age_seconds()
    decision: str = "skip"

    try:
        # Local sync (always)
        try:
            cache_conn = open_cache_db()
            try:
                stats = sync_cache(cache_conn)
                ingested = int(stats.rows_changed)
                parse_malformed = int(stats.lines_malformed)
                parse_skipped = int(stats.assistant_lines_skipped)
            finally:
                try:
                    cache_conn.close()
                except Exception:
                    pass
        except Exception as exc:
            ingested = -1
            if explain:
                eprint(f"[hook-tick] sync_cache failed: {exc}")

        mock = getattr(args, "mock_oauth_response", None)
        if mock is not None:
            # Replace the throttle path's fetch fn for this process.
            sys.modules["cctally"]._hook_tick_oauth_refresh = _hook_tick_make_mock_refresh(mock)

        # Throttle check + OAuth (under flock)
        if not no_oauth:
            _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
            try:
                lock_fd = os.open(
                    _cctally_core.HOOK_TICK_THROTTLE_LOCK_PATH,
                    os.O_WRONLY | os.O_CREAT, 0o644,
                )
            except OSError:
                lock_fd = -1
            try:
                if lock_fd >= 0:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                pre_age = _hook_tick_throttle_age_seconds()
                if pre_age >= throttle_seconds:
                    decision = "fetch"
                    oauth_status, _ = _hook_tick_oauth_refresh(throttle_seconds=throttle_seconds)
                    if oauth_status.startswith("ok"):
                        _hook_tick_throttle_touch()
                else:
                    oauth_status = f"throttled(age={int(pre_age)}s)"
            finally:
                if lock_fd >= 0:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(lock_fd)
                    except OSError:
                        pass
    except Exception as exc:
        oauth_status = f"err(internal:{type(exc).__name__})"
        if explain:
            eprint(f"[hook-tick] internal error: {exc}")

    dur_ms = int((time.monotonic() - start) * 1000)

    # --- Step 7: log line ---
    line = _hook_tick_format_log_line(
        event=meta["event"],
        session=_hook_tick_session_short(meta["session_id"]),
        ingested=ingested,
        oauth_status=oauth_status,
        dur_ms=dur_ms,
        malformed=parse_malformed,
        skipped=parse_skipped,
    )
    _hook_tick_log_line(line)
    _hook_tick_log_rotate_if_needed()

    # --- Step 9: exit code ---
    if not explain:
        # Forked child: skip Python's atexit / argparse / cleanup paths
        # (they may try to flush already-redirected stdio handles).
        if forked:
            os._exit(0)
        return 0
    # --explain mapping (Section 3 of spec)
    if oauth_status == "skipped-no-token":
        rc = 2
    elif oauth_status.startswith("err(network") or oauth_status.startswith("err(parse"):
        rc = 3
    elif oauth_status.startswith("err(record-usage"):
        rc = 4
    elif ingested < 0:
        rc = 5
    else:
        rc = 0
    # Print --explain decision tree
    print("[1/4] Local sync (sync_cache)")
    print(f"      → ingested {max(0, ingested)} new entries")
    print("[2/4] Throttle check")
    print(f"      → throttle file: {_cctally_core.HOOK_TICK_THROTTLE_PATH}")
    if pre_age == float("inf"):
        print("      → mtime: (file absent)")
    else:
        print(f"      → mtime: {int(pre_age)}s ago")
    print(f"      → threshold: {int(throttle_seconds)}s → {decision}")
    print("[3/4] OAuth refresh")
    print(f"      → status: {oauth_status}")
    print(f"[4/4] Log written → {_cctally_core.HOOK_TICK_LOG_PATH}")
    print(f"\nDone in {dur_ms} ms.")
    return rc


def _safe_float(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("weeklyPercent must be numeric") from exc
    if num < 0:
        raise ValueError("weeklyPercent must be >= 0")
    if num > 1000:
        raise ValueError("weeklyPercent is unreasonably large")
    return num


def _validate_date_optional(value: Any, label: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string in YYYY-MM-DD")
    return parse_date_str(value, label)


@dataclass(frozen=True)
class DerivedWeekWindow:
    week_start: dt.date
    week_end: dt.date
    week_start_at: str | None = None
    week_end_at: str | None = None



def _coerce_payload_captured_at(payload: dict[str, Any]) -> tuple[str, dt.datetime]:
    captured_at_raw = payload.get("capturedAt")
    if isinstance(captured_at_raw, str) and captured_at_raw.strip():
        try:
            return captured_at_raw, parse_iso_datetime(captured_at_raw, "capturedAt")
        except ValueError:
            pass

    captured_at = now_utc_iso()
    return captured_at, parse_iso_datetime(captured_at, "capturedAt")



def _derive_week_from_payload(payload: dict[str, Any], week_start_name: str) -> DerivedWeekWindow:
    ws_at = payload.get("weekStartAt")
    we_at = payload.get("weekEndAt")
    if isinstance(ws_at, str) and ws_at.strip() and isinstance(we_at, str) and we_at.strip():
        start_iso = _canonicalize_optional_iso(ws_at, "weekStartAt")
        end_iso = _canonicalize_optional_iso(we_at, "weekEndAt")
        if not start_iso or not end_iso:
            raise ValueError("weekStartAt/weekEndAt must be non-empty ISO datetime strings")
        start_at = parse_iso_datetime(start_iso, "weekStartAt")
        end_at = parse_iso_datetime(end_iso, "weekEndAt")
        if end_at <= start_at:
            raise ValueError("weekEndAt must be after weekStartAt")
        # Anchor the bucket-key date on the canonical UTC ISO, not on
        # `.date()` of the parsed datetime — `parse_iso_datetime` ends
        # with `.astimezone()` which converts to host-local TZ. If the
        # cctally process inherits a TZ whose offset puts the UTC moment
        # on a different calendar date, `start_at.date()` silently
        # forks the `week_start_date` column for the SAME physical
        # subscription week, producing a ghost row that never gets
        # updated (regression: Israel host briefly running with
        # TZ=America/Los_Angeles for 7 minutes during refactor work
        # spawned 18 ghost usage rows + 2 ghost cost rows under
        # week_start_date='2026-05-08' while every other row sat at
        # '2026-05-09'). Re-canonicalize to UTC before `.date()` so the
        # bucket key matches what `cmd_record_usage` writes (it derives
        # `week_start_date` directly from `resets_at` in UTC).
        return DerivedWeekWindow(
            week_start=start_at.astimezone(dt.timezone.utc).date(),
            week_end=end_at.astimezone(dt.timezone.utc).date(),
            week_start_at=start_iso,
            week_end_at=end_iso,
        )

    ws = _validate_date_optional(payload.get("weekStartDate"), "weekStartDate")
    we = _validate_date_optional(payload.get("weekEndDate"), "weekEndDate")
    if ws and we:
        if we < ws:
            raise ValueError("weekEndDate must be on or after weekStartDate")
        return DerivedWeekWindow(week_start=ws, week_end=we)
    if ws and not we:
        return DerivedWeekWindow(week_start=ws, week_end=ws + dt.timedelta(days=6))

    captured_raw = payload.get("capturedAt")
    if isinstance(captured_raw, str) and captured_raw.strip():
        try:
            captured_dt = dt.datetime.fromisoformat(captured_raw.replace("Z", "+00:00"))
            if captured_dt.tzinfo is None:
                captured_dt = captured_dt.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            # internal fallback: host-local intentional
            captured_dt = dt.datetime.now().astimezone()
    else:
        # internal fallback: host-local intentional
        captured_dt = dt.datetime.now().astimezone()

    start, end = compute_week_bounds(captured_dt, week_start_name)
    return DerivedWeekWindow(week_start=start, week_end=end)


def insert_usage_snapshot(payload: dict[str, Any], week_start_name: str) -> dict[str, Any]:
    weekly_percent = _safe_float(payload.get("weeklyPercent"))
    captured_at, captured_at_dt = _coerce_payload_captured_at(payload)

    page_url = payload.get("pageUrl") if isinstance(payload.get("pageUrl"), str) else None
    source = payload.get("source") if isinstance(payload.get("source"), str) else "userscript"

    five_hour_percent = payload.get("fiveHourPercent")
    if five_hour_percent is not None:
        five_hour_percent = float(five_hour_percent)
    five_hour_resets_at = payload.get("fiveHourResetsAt")
    if five_hour_resets_at is not None:
        five_hour_resets_at = str(five_hour_resets_at)
    five_hour_window_key = payload.get("fiveHourWindowKey")
    if five_hour_window_key is not None:
        try:
            five_hour_window_key = int(five_hour_window_key)
        except (TypeError, ValueError) as exc:
            # Loud-skip on first failure only (module-level guard) so a
            # misbehaving caller doesn't spam the log on every insert.
            global _logged_window_key_coerce_failure
            if not _logged_window_key_coerce_failure:
                # Use the local (already extracted from payload at line
                # ~13858) instead of re-reading; payload is mutable and
                # could in principle change between extraction and the
                # except branch.
                eprint(
                    f"[record-usage] fiveHourWindowKey coerce failed "
                    f"(got {type(five_hour_window_key).__name__}: "
                    f"{five_hour_window_key!r}); "
                    f"5h DB clamp will be skipped for this row: {exc}"
                )
                _logged_window_key_coerce_failure = True
            five_hour_window_key = None

    conn = open_db()
    try:
        week_window = _derive_week_from_payload(payload, week_start_name)

        # Use the canonical boundary already established for this week_start_date.
        # This prevents relative-reset drift from creating duplicate weeks.
        date_str = week_window.week_start.isoformat()
        canon_start, canon_end = _get_canonical_boundary_for_date(conn, date_str)
        if canon_start and canon_end:
            week_window = DerivedWeekWindow(
                week_start=week_window.week_start,
                week_end=week_window.week_end,
                week_start_at=canon_start,
                week_end_at=canon_end,
            )

        week_start = week_window.week_start
        week_end = week_window.week_end

        cur = conn.execute(
            """
            INSERT INTO weekly_usage_snapshots
            (
              captured_at_utc,
              week_start_date,
              week_end_date,
              week_start_at,
              week_end_at,
              weekly_percent,
              page_url,
              source,
              payload_json,
              five_hour_percent,
              five_hour_resets_at,
              five_hour_window_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                week_start.isoformat(),
                week_end.isoformat(),
                week_window.week_start_at,
                week_window.week_end_at,
                weekly_percent,
                page_url,
                source,
                json.dumps(payload, separators=(",", ":")),
                five_hour_percent,
                five_hour_resets_at,
                five_hour_window_key,
            ),
        )
        conn.commit()
        snapshot_id = int(cur.lastrowid)
    finally:
        conn.close()

    out = {
        "id": snapshot_id,
        "capturedAt": captured_at,
        "weekStartDate": week_start.isoformat(),
        "weekEndDate": week_end.isoformat(),
        "weeklyPercent": weekly_percent,
    }
    if week_window.week_start_at:
        out["weekStartAt"] = week_window.week_start_at
    if week_window.week_end_at:
        out["weekEndAt"] = week_window.week_end_at
    if isinstance(payload.get("resetText"), str):
        out["resetText"] = payload["resetText"]
    if five_hour_percent is not None:
        out["fiveHourPercent"] = five_hour_percent
    if five_hour_resets_at is not None:
        out["fiveHourResetsAt"] = five_hour_resets_at
    if five_hour_window_key is not None:
        out["fiveHourWindowKey"] = five_hour_window_key
    return out


def _saved_dict_from_usage_row(row: sqlite3.Row) -> dict[str, Any]:
    """Mirror ``insert_usage_snapshot``'s output dict from an existing
    weekly_usage_snapshots row. Used by ``cmd_record_usage``'s dedup
    self-heal path so ``maybe_record_milestone`` and
    ``maybe_update_five_hour_block`` can re-run on the latest snapshot
    when an earlier invocation was killed between snapshot insert and
    milestone insert (e.g. CC self-update kill window, 2026-05-08).

    Field omissions match ``insert_usage_snapshot``: keys whose values
    would be ``None`` are not emitted, so downstream ``saved.get(...)``
    callers see the same shape they'd see on a fresh insert.

    Note: ``resetText`` (the only userscript-payload-only key
    ``insert_usage_snapshot`` re-emits in its output dict) is
    intentionally omitted — no downstream ``saved``-dict consumer in
    this codebase reads it. ``pageUrl`` is a column on
    ``weekly_usage_snapshots`` but is never propagated into the output
    dict either path.
    """
    out: dict[str, Any] = {
        "id": int(row["id"]),
        "capturedAt": row["captured_at_utc"],
        "weekStartDate": row["week_start_date"],
        "weekEndDate": row["week_end_date"],
        "weeklyPercent": float(row["weekly_percent"]),
    }
    if row["week_start_at"] is not None:
        out["weekStartAt"] = row["week_start_at"]
    if row["week_end_at"] is not None:
        out["weekEndAt"] = row["week_end_at"]
    if row["five_hour_percent"] is not None:
        out["fiveHourPercent"] = float(row["five_hour_percent"])
    if row["five_hour_resets_at"] is not None:
        out["fiveHourResetsAt"] = row["five_hour_resets_at"]
    if row["five_hour_window_key"] is not None:
        out["fiveHourWindowKey"] = int(row["five_hour_window_key"])
    return out
