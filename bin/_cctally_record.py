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
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace as dc_replace
from typing import Any, Iterable


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
    _as_of_or_command,
)
import _lib_accounts  # pure stdlib kernel; UNATTRIBUTED sentinel default (#341)
import _lib_credit_identity  # pure stdlib kernel; credit_key / credit_order (#703)
import _lib_credit_selection  # pure stdlib kernel; the two replica selectors (#703)
import _lib_journal  # pure stdlib kernel; evt_id natural-key builder
from _lib_five_hour import _canonical_5h_window_key, five_hour_milestone_range
from _lib_pricing import _calculate_entry_cost, claude_usage_dict
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
    post_reset_seed_has_climb_evidence,
    FIRE_IMMEDIATE,
    CONFIRM_RESET,
    CLEAR_MARKER,
    ARM_MARKER,
    SNAPSHOT_SKIP_CLAMP,
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


def get_max_journaled_milestone_for_segment(*args, **kwargs):
    return sys.modules["cctally"].get_max_journaled_milestone_for_segment(
        *args, **kwargs)


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


_BUDGET_ANCHOR_WARNED = False


def _warn_budget_active_anchor_unavailable_once():
    """One-shot stderr WARN (#341 Step 4-eval, spec §6 `*`-anchor): the Claude
    vendor-wide subscription-week budget ladder was SKIPPED this tick because the
    active-account anchor is genuinely unavailable (a torn `~/.claude.json`
    read). Throttled to once per process so a persistent torn read never spams
    the hot path; the persistent health signal is the doctor `accounts.identity`
    WARN leg."""
    global _BUDGET_ANCHOR_WARNED
    if _BUDGET_ANCHOR_WARNED:
        return
    _BUDGET_ANCHOR_WARNED = True
    eprint(
        "warning: skipping vendor-wide Claude budget alert — the active account "
        "anchor is unavailable (torn ~/.claude.json read); see `cctally doctor`"
    )


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
    *,
    account_key: str = _lib_accounts.UNATTRIBUTED,
) -> int:
    """Return ``id`` of the most-recent ``five_hour_reset_events`` row for
    ``(account_key, five_hour_window_key)``, else 0 (pre-credit / no-event
    sentinel). ``account_key`` (#341) scopes the active-segment resolution to
    the account so a shared physical window keeps per-account segments.

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
            "WHERE five_hour_window_key = ? AND account_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (int(five_hour_window_key), account_key),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    return int(row["id"])


def maybe_record_milestone(
    saved: dict[str, Any],
    *,
    conn=None,
    as_of: "str | None" = None,
    alert_sink: "list | None" = None,
    journal: "tuple | None" = None,
    account_key: str = _lib_accounts.UNATTRIBUTED,
    retained_selection=None,
) -> None:
    """Check if a new integer percent threshold was crossed, and if so,
    fetch cost and record the milestone. Errors are logged, not raised.

    ``account_key`` (#341): the account the crossing belongs to. Scopes the
    per-account milestone ledger (max/marginal reads, INSERT, alerted_at
    UPDATE) so two accounts crossing the SAME threshold in the SAME week each
    record independently. Default ``"unattributed"`` is the rev-4.1 defensive
    backstop; the ingest pipeline hook passes the resolved account explicitly.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    when ``conn`` is passed the crossing folds into the caller's transaction —
    no internal ``open_db()``/``commit()``/``close()``, and alert dispatch is
    left to the caller (the ingester's post-commit ALERT_DISPATCHER). ``as_of``
    (ISO-Z) is stamped as the milestone ``captured_at_utc`` / ``alerted_at`` in
    place of wall clock. Both defaults keep the legacy own-connection,
    commit-and-dispatch behavior byte-identical.

    ``alert_sink`` (DB journal redesign Task 6): on the passed-conn path the new-
    crossing alert payloads are APPENDED to this list instead of being dropped,
    so the ingester dispatches them post-commit (spec §5.2 step 6). The
    ``alerted_at`` stamp already lands in-txn (before harvest), so it survives a
    rebuild. ``None`` keeps today's compute-and-drop behavior on the passed-conn
    path.

    ``retained_selection`` (#410 Task A) carries the triggering raw
    observation's canonical subscription window. It is deliberately separate
    from ``saved``: a dedup/self-heal fold may reuse a later physical usage row,
    but that must never retarget the triggering observation's ``wcs:`` event.

    ``journal`` (DB journal redesign Task 6, Design A): the ``(ctx, id_base)``
    tuple threaded into the milestone's pre-record cost sync so the
    ``weekly_cost_snapshots`` row it inserts rides a Model-A
    ``weekly_cost_snapshot`` evt (``wcs:<id_base>:<week>``) instead of a bare
    insert — the computed cost is journaled and replay reads it back verbatim
    (Claude Code prunes the source JSONL). Only threaded on the passed-conn
    (ingest) path; ``None`` / the legacy own-conn path keeps the bare insert."""
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

    own_conn = conn is None
    if own_conn:
        conn = open_db()
    try:
        # Resolve the credit epoch governing THIS captured moment, through the
        # ONE shared resolver (#703 + #707 §5.3). 0 stays the pre-credit /
        # no-credit sentinel.
        #
        # Two things changed here and both were defects. The lookup was keyed on
        # `new_week_end_at`, which a MANUAL credit leaves NULL — so a manual
        # credit opened no epoch at all, which is the missing reference
        # unification removes. And it ordered by `id DESC`, which a rebuild
        # reassigns and which the rebuild's fold-family sort can put in the
        # opposite order from the real chronology, so a reader could show a
        # different epoch from the one this writer stamped.
        captured_at_iso = saved.get("capturedAt") or as_of or now_utc_iso()
        reset_event_id = 0
        reset_effective_iso = None
        seg_row = _lib_credit_selection.resolve_weekly_credit_epoch(
            conn, week_start_date=week_start_date, account_key=account_key,
            captured_at=captured_at_iso, week_end_at=week_end_at)
        if seg_row is not None:
            reset_event_id = int(seg_row["id"])
            # The ACCOUNTING instant, not the hour-floored display one: the
            # seeding guard and the cost range both measure from where the
            # credit was observed.
            reset_effective_iso = seg_row["accounting_at"]

        max_existing = get_max_milestone_for_week(
            conn, week_start_date, reset_event_id=reset_event_id,
            account_key=account_key,
        )
        # A milestone row can leave the table without its event leaving the
        # journal: the `weekly_replica_suppression` applier removes the
        # dependents of a stale replica, and `--force` removes the dependents of
        # the occurrence it replaces. The segment's forward-only mark is
        # therefore the higher of the two, because re-deriving a threshold whose
        # identity is already journaled produces a same-revision divergence that
        # the emitter withholds and whose convergence cannot succeed — the
        # journaled event references the deleted snapshot. See
        # `get_max_journaled_milestone_for_segment` for the cost this pays.
        journaled_max = get_max_journaled_milestone_for_segment(
            conn, week_start_date, reset_event_id=reset_event_id,
            account_key=account_key,
        )
        if journaled_max is not None:
            max_existing = (journaled_max if max_existing is None
                            else max(max_existing, journaled_max))
        if max_existing is not None and current_floor <= max_existing:
            return

        # Seeding a POST-RESET epoch's ladder requires observed evidence of the
        # climb. A fresh install or a fresh week is genuinely missing history,
        # so seeding at `current_floor` is the only thing it can do — that is
        # the `reset_event_id == 0` path and it is unchanged. A post-reset epoch
        # is different: the reset event asserts the counter stood at the
        # credited level at a known instant, so a first in-epoch observation
        # high above it with no lower observation between the two is a stale
        # pre-credit replica, not a crossing. Seeding from one is permanent
        # damage, because milestones are forward-only within an epoch: on
        # 2026-09-01 a fresh epoch was seeded at 13% from a replica the
        # in-place-credit stale-replica DELETE had missed, so the epoch's
        # ladder started at a threshold the meter said had not been crossed
        # and every genuine crossing below 13 in that epoch was foreclosed.
        #
        # The evidence is a stored observation inside this epoch, compared
        # against what the credit RECORDED (#703 + #707 §5.2). When the row
        # carries `observed_post_credit_pct` the comparison is "at or below the
        # credited level", because the credit itself says where the counter
        # stood; when it does not, the comparison falls back to the #706 rule of
        # "strictly below the threshold being recorded". Neither is a tolerance
        # band around `observed_pre_credit_pct`: that comparison is what let the
        # stale replica survive in the first place (issue #703).
        #
        # The epoch's lower bound is the governing credit's ACCOUNTING instant —
        # `COALESCE(observed_at_utc, effective_reset_at_utc)`, the same value
        # `reset_event_id` was resolved against — so the window and the epoch
        # cannot disagree. Using the hour-floored display instant here is what
        # placed a genuine pre-credit reading inside the epoch on 2026-09-01.
        if reset_event_id != 0 and max_existing is None:
            # A bare aggregate SELECT always returns exactly one row, holding
            # NULL when nothing matched, so there is no empty-result case to
            # guard here — the kernel decides the NULL.
            lowest_in_epoch = conn.execute(
                "SELECT MIN(weekly_percent) FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? AND account_key = ? "
                "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
                "  AND unixepoch(captured_at_utc) <= unixepoch(?)",
                (week_start_date, account_key, reset_effective_iso,
                 captured_at_iso),
            ).fetchone()[0]
            landed = seg_row["observed_post_credit_pct"] if seg_row else None
            if not post_reset_seed_has_climb_evidence(
                lowest_in_epoch, current_floor, post_credit_pct=landed
            ):
                bound = (f"at or below the credited {landed}%"
                         if landed is not None else f"below {current_floor}%")
                eprint(
                    "[milestone] skipping this crossing — the post-reset "
                    f"segment {reset_event_id} has no observation {bound}, so "
                    f"a {current_floor}% seed would come from a stale "
                    "pre-credit reading, not a climb"
                )
                return

        # Threshold crossed — sync cost before recording so the milestone
        # captures up-to-date cumulative cost, not a stale snapshot.
        cost_synced = True
        try:
            if retained_selection is None:
                retained_selection = _cctally().WeekSelection(
                    week_start=dt.date.fromisoformat(week_start_date),
                    week_end=dt.date.fromisoformat(week_end_date),
                    start_iso_override=week_start_at,
                    end_iso_override=week_end_at,
                )
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
            # On the passed-conn path thread the same connection + capture time
            # so the cost snapshot folds into the caller's transaction; on the
            # legacy path cmd_sync_week opens its own connection as today. On the
            # ingest path also thread `journal=(ctx, id_base)` so the cost
            # snapshot rides a Model-A `weekly_cost_snapshot` evt (Design A) —
            # never on the legacy own-conn path.
            cmd_sync_week(
                sync_ns,
                conn=(None if own_conn else conn),
                as_of=as_of,
                journal=(None if own_conn else journal),
                # Materialize the cost snapshot under the crossing's account
                # (#341 P2-1) so the account-scoped read below finds it.
                account_key=account_key,
                # #410 Task A: the implicit milestone sync is a derivation of
                # THIS retained observation. Never ask `pick_week_selection`
                # for the latest stats row, which may carry a later anchor.
                retained_selection=retained_selection,
            )
        except Exception as exc:
            # The snapshot read below would now return a row from an EARLIER
            # crossing, so recording here stamps that older cumulative onto
            # this threshold — a write-once row with a $0.00 marginal and a
            # fabricated $/1%. Fall through only far enough to reach the
            # skip guard on the snapshot branch.
            cost_synced = False
            eprint(f"[milestone] cost sync failed: {exc}")

        week_start = dt.date.fromisoformat(week_start_date)
        week_end = dt.date.fromisoformat(week_end_date)
        week_ref = make_week_ref(
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=week_end_at,
        )

        # For a CREDITED week the cached weekly_cost_snapshots row covers the
        # whole week, so a crossing recorded after the credit would carry the
        # whole week's spend. Live-compute over the epoch's own range instead,
        # so the milestone captures cost from the credit forward.
        #
        # #703 + #707: the authoritative condition is `reset_event_id != 0` —
        # the epoch this crossing is actually being filed under — and the range
        # is built directly from that epoch's ACCOUNTING instant to the
        # unchanged week end. It used to be `_week_ref_has_reset_event` over a
        # reference `_apply_reset_events_to_weekrefs` had rewritten, which is
        # wrong twice over. That rewrite matches on the boundary columns, so it
        # never saw a MANUAL credit (both NULL, because a manual credit moved no
        # boundary) and every milestone after one carried the full week's spend.
        # And the display stops splitting a credited week, after which the
        # reference keeps its original start, nothing equals any
        # `effective_reset_at_utc`, and the same full-week fallback would take
        # over for automatic credits too. Segment 0 keeps the cached path.
        #
        # The range START is the accounting instant, not the hour-floored
        # display one: measuring from the floor would attribute up to an hour of
        # pre-credit spend to the new epoch. The range END is the week's own
        # end, because a credit is a counter discontinuity inside an UNCHANGED
        # window; `as_of` clamps it to the retained triggering observation clock
        # exactly as before (#410 Task A).
        if reset_event_id != 0 and reset_effective_iso:
            import _cctally_cache  # fail-closed attribution guard (#341)
            accounting_ref = dc_replace(
                week_ref, week_start_at=reset_effective_iso)
            try:
                live_cost = _compute_cost_for_weekref(
                    accounting_ref,
                    account_key=account_key,
                    as_of=as_of,
                )
            except _cctally_cache.AccountAttributionUnavailable as exc:
                # Same contract the budget ladder already holds (#341 Task 4):
                # an account-scoped read that fell into the fail-closed guard
                # SKIPS this tick and fires on the next healthy one. Never
                # re-raised — on the passed-conn (ingest) path a bare raise
                # would abort the whole cycle over a transient lock.
                eprint("[milestone] account attribution unavailable, "
                       f"skipping this crossing: {exc}")
                return
            if live_cost is None:
                eprint("[milestone] could not compute effective-range cost, skipping")
                return
            cumulative_cost = live_cost
            cost_snapshot_id = 0  # no snapshot row to anchor against
        else:
            if not cost_synced:
                # The latest snapshot predates this crossing. Milestones are
                # write-once, so a stale cumulative here is permanent; skip
                # instead. The next observation still sees current_floor >
                # max_existing and records the crossing with a real cost.
                eprint("[milestone] skipping this crossing — its cost would "
                       "come from a snapshot taken before the crossing")
                return
            # Account-scoped read (#341 P2-1): the cost snapshot was just
            # materialized under `account_key`, so scope the read to it — the
            # merged (account-blind) read would return another account's row on
            # a multi-account install. Byte-stable at single-account (the sole
            # row is the crossing account's).
            latest_cost = get_latest_cost_for_week(
                conn, week_ref, account_key=account_key)
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
                    account_key=account_key,
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
                as_of=as_of,
                account_key=account_key,
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
                    crossed_at = as_of or now_utc_iso()
                    # set-then-dispatch: alerted_at lands on the row BEFORE
                    # the osascript Popen, so a dismissed-after-spawn
                    # notification still surfaces in the dashboard alerts
                    # envelope (T5). UPDATE shares the transaction with
                    # the preceding INSERT (commit=False above) so a
                    # crash between them is impossible.
                    conn.execute(
                        "UPDATE percent_milestones SET alerted_at = ? "
                        "WHERE week_start_date = ? AND percent_threshold = ? "
                        "  AND reset_event_id = ? AND account_key = ? "
                        "  AND alerted_at IS NULL",
                        (crossed_at, week_start_date, pct, reset_event_id,
                         account_key),
                    )
                    # Cheap re-read for payload context (cumulative_cost_usd
                    # reflects the value persisted on insert, immune to any
                    # subsequent recompute drift). SELECT inside the open
                    # transaction is fine; values reflect post-INSERT state.
                    # Filter by reset_event_id so a credited week's
                    # alert payload reads the post-credit row, not a
                    # stale pre-credit row at the same (week, threshold).
                    # `week_start_at` comes from the row rather than the
                    # local variable for the same reason `cumulative_cost_usd`
                    # does: it is the value persisted on insert, so the alert
                    # states the week the milestone actually recorded.
                    row = conn.execute(
                        "SELECT cumulative_cost_usd, week_start_at "
                        "FROM percent_milestones "
                        "WHERE week_start_date = ? AND percent_threshold = ? "
                        "  AND reset_event_id = ? AND account_key = ?",
                        (week_start_date, pct, reset_event_id, account_key),
                    ).fetchone()
                    if row is not None:
                        cum = float(row["cumulative_cost_usd"])
                        # $/1% rough trend metric: cumulative / threshold.
                        dpp = (cum / pct) if pct else None
                        payload = _build_alert_payload_weekly(
                            threshold=pct,
                            crossed_at_utc=crossed_at,
                            week_start_date=week_start_date,
                            week_start_at=row["week_start_at"],
                            cumulative_cost_usd=cum,
                            dollars_per_percent=dpp,
                            account_key=account_key,
                        )
                        pending_alerts.append(payload)
        # Single commit after the loop durably writes every milestone row
        # AND its alerted_at marker together. On the passed-conn (ingest) path
        # the caller owns the commit and the post-commit alert dispatch (the
        # ingester's ALERT_DISPATCHER, spec §5.2.7), so both are skipped here.
        if own_conn:
            conn.commit()
            # Dispatch deferred to AFTER commit (matches 5h path). Per-payload
            # exception logged so a bad-payload alert can't suppress healthy
            # ones. Production caller ignores _dispatch_alert_notification's
            # return value (spec §6.4).
            for payload in pending_alerts:
                try:
                    _dispatch_alert_notification(payload, mode="real")
                except Exception as dispatch_exc:
                    eprint(f"[alerts] dispatch failed: {dispatch_exc}")
        elif alert_sink is not None:
            # Passed-conn (ingest) path: hand the new-crossing payloads to the
            # caller's sink so the ingester dispatches them post-commit
            # (spec §5.2 step 6). alerted_at is already stamped in the caller's
            # txn above, so a crash between commit and dispatch loses at most
            # one notification — the set-then-dispatch trade.
            alert_sink.extend(pending_alerts)
    except Exception as exc:
        # Exception discipline (6c-gate P1): on the passed-conn (ingest) path a
        # chokepoint failure must ABORT the cycle — re-raise so the ingester
        # rolls back and leaves the cursor unmoved (invariant ii). Legacy
        # own-connection ticks keep the log-and-swallow.
        if not own_conn:
            raise
        eprint(f"[milestone] error recording milestone: {exc}")
    finally:
        if own_conn:
            conn.close()


def _record_budget_milestone_for_vendor(
    *, vendor, target, thresholds, period, config, tz, build_payload,
    raise_errors: bool = False, conn=None, as_of=None, alert_sink=None,
    account_key: str = "*", window_account_key=None,
) -> int:
    """Shared budget-milestone firing core for both vendors (#143).

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    when ``conn`` is passed the crossings fold into the caller's transaction —
    no internal ``open_db()``/``commit()``/``close()``, and alert dispatch is
    left to the caller (the ingester's ALERT_DISPATCHER). ``as_of`` (ISO-Z) is
    the capture-time reference for window/spend + the crossing timestamps. Both
    defaults keep the legacy own-connection, commit-and-dispatch behavior.

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
    import _cctally_cache  # for the fail-closed AccountAttributionUnavailable skip (#341)
    now_utc = _as_of_or_command(as_of)
    pending_alerts: list[dict[str, Any]] = []
    own_conn = conn is None
    if own_conn:
        conn = open_db()
    try:
        start_at = _resolve_budget_window(
            conn, vendor=vendor, now_utc=now_utc, period=period,
            config=config, tz=tz, window_account_key=window_account_key,
        )
        if start_at is None:
            return 0  # no resolvable window yet (claude subscription-week pre-snapshot)
        period_key = start_at.isoformat(timespec="seconds")

        present = {
            int(r[0]) for r in conn.execute(
                "SELECT threshold FROM budget_milestones "
                "WHERE vendor = ? AND account_key = ? AND period_start_at = ? "
                "  AND (period = ? OR period IS NULL)",
                (vendor, account_key, period_key, period),
            )
        }
        pending = [t for t in sorted(thresholds) if t not in present]
        if not pending:
            return 0  # nothing left this window → skip the cost SUM

        spent = _budget_spend_for_vendor(
            conn, vendor=vendor, start_at=start_at, now_utc=now_utc,
            account_key=account_key,
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
            as_of=as_of,
            account_key=account_key,
        ):
            pending_alerts.append(build_payload(
                threshold=t,
                crossed_at_utc=crossed_at,
                period_key=period_key,
                period=period,
                budget_usd=tg,
                spent_usd=sp,
                consumption_pct=pct,
                account_key=account_key,
            ))
        # Single commit: every INSERT + its alerted_at marker durable together.
        # On the passed-conn (ingest) path the caller owns the commit + the
        # post-commit dispatch (the ingester's ALERT_DISPATCHER).
        if own_conn:
            conn.commit()
    except _cctally_cache.AccountAttributionUnavailable as exc:
        # #341 (Task 4): a per-account ladder whose account-scoped spend read
        # hit the fail-closed cache guard is SKIPPED this tick — never fired on
        # wrong/merged data (the mislabel the guard prevents) and, unlike a
        # generic failure, NEVER re-raised (a transient attribution gap must not
        # abort the whole ingest cycle). Fires next healthy tick. spec §6
        # "unresolvable keys ... skipped ... never a crash". `pending_alerts`
        # is empty at this point (the spend leg precedes any crossing), so no
        # partial arm leaks. Vendor-wide (`*`) reads pass `account_key=None`
        # downstream and never trip the guard, so this only guards real ladders.
        eprint(f"[budget-milestone:{vendor}] account attribution unavailable; "
               f"skipping the {account_key} ladder this tick ({exc})")
        return 0
    except Exception as exc:
        # Exception discipline (6c-gate P1): passed-conn (ingest) path re-raises
        # so a failure ABORTS the cycle (invariant ii); this covers both budget
        # axes (claude + codex share this core). Legacy own-connection swallows
        # (or re-raises when a caller explicitly requested raise_errors).
        if not own_conn:
            raise
        eprint(f"[budget-milestone:{vendor}] error recording budget milestone: {exc}")
        if raise_errors:
            raise
    finally:
        if own_conn:
            conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant — one queue attempt per crossing, deduped on
    # the alerted_at column). Skipped on the passed-conn path (caller dispatches).
    if own_conn:
        for payload in pending_alerts:
            try:
                _dispatch_alert_notification(payload, mode="real")
            except Exception as dispatch_exc:
                eprint(f"[budget-alerts:{vendor}] dispatch failed: {dispatch_exc}")
    elif alert_sink is not None:
        # Passed-conn (ingest) path: hand the budget crossings to the caller's
        # sink for the ingester's post-commit dispatch (spec §5.2 step 6).
        alert_sink.extend(pending_alerts)
    return len(pending_alerts)


def maybe_record_budget_milestone(
    saved: dict[str, Any], *, conn=None, as_of: "str | None" = None,
    alert_sink: "list | None" = None,
) -> None:
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
    thresholds = budget_cfg.get("alert_thresholds")
    if not thresholds or not budget_cfg.get("alerts_enabled"):
        return
    weekly_usd = budget_cfg.get("weekly_usd")
    # Per-account budget ladders (#341 Step 4-eval, spec §6): `budget.accounts` is
    # a {account_key: usd} map (refs normalised to immutable keys at write time).
    # Each real account fires its OWN ladder over its OWN stamped spend. A Claude
    # budget can be per-account-ONLY (no vendor-wide `weekly_usd`), so the gate
    # below no longer requires `weekly_usd`.
    accounts = budget_cfg.get("accounts") or {}
    if weekly_usd is None and not accounts:
        return  # neither a vendor-wide nor a per-account budget → nothing to do
    # Period generalization (spec §6): subscription-week resolves the snapshot-
    # anchored window; a calendar period (calendar-week / calendar-month)
    # resolves the window purely from `now` + the period. config/tz are
    # resolved once for the calendar branch.
    period = budget_cfg.get("period", "subscription-week")
    tz = resolve_display_tz(argparse.Namespace(tz=None), config)

    def _claude_budget_payload(**kw):
        # The Claude payload builder takes the legacy `week_start_at=` kwarg
        # (its value is the resolved period-start instant, == period_key), so
        # the at-fire dispatch id stays byte-stable `budget:<period_start_at>:<t>`.
        # `account_key` (#341) rides through to the alert's [label] prefix + the
        # `alerts.log` 8th field; `*` for the vendor-wide ladder.
        return _build_alert_payload_budget(
            threshold=kw["threshold"],
            crossed_at_utc=kw["crossed_at_utc"],
            week_start_at=kw["period_key"],
            budget_usd=kw["budget_usd"],
            spent_usd=kw["spent_usd"],
            consumption_pct=kw["consumption_pct"],
            period=kw["period"],
            account_key=kw["account_key"],
        )

    # Vendor-wide (`*`) ladder — today's semantics (sum across ALL accounts incl.
    # unattributed, the guaranteed-complete vendor total). Rev-3 `*`-anchor: a
    # subscription-week vendor budget anchors on the ACTIVE account's week; when
    # the active identity is genuinely UNAVAILABLE (a TORN `~/.claude.json` read —
    # NOT a resolved `unattributed` absence) we skip the eval + WARN rather than
    # guess a wrong anchor (the doctor `accounts.identity` leg surfaces the torn
    # read persistently). A <=1-real-account install resolves `unattributed`
    # (stably-absent) and behaves exactly as today (byte-stable).
    if weekly_usd is not None:
        skip_vendor_wide = False
        vendor_window_key = None
        if period == "subscription-week":
            ident = _cctally_core._resolve_active_claude_identity()
            if ident.get("status") == "torn":
                _warn_budget_active_anchor_unavailable_once()
                skip_vendor_wide = True
            else:
                # #341 (spec §6 rev-4): the vendor-wide (`*`) subscription-week
                # ladder anchors its PERIOD on the ACTIVE account's week (its
                # SPEND still sums every account). A <=1-real-account install
                # resolves `unattributed` → the same window as today (byte-stable).
                vendor_window_key = ident.get("account_key")
        if not skip_vendor_wide:
            _record_budget_milestone_for_vendor(
                vendor="claude", target=weekly_usd, thresholds=thresholds,
                period=period, config=config, tz=tz, conn=conn, as_of=as_of,
                alert_sink=alert_sink, account_key="*",
                window_account_key=vendor_window_key,
                build_payload=_claude_budget_payload,
            )

    # Per-account ladders — one per real account in `budget.accounts`. Each
    # anchors its period on its OWN account's subscription week (spec §6).
    for acct_key, acct_usd in accounts.items():
        _record_budget_milestone_for_vendor(
            vendor="claude", target=acct_usd, thresholds=thresholds,
            period=period, config=config, tz=tz, conn=conn, as_of=as_of,
            alert_sink=alert_sink, account_key=acct_key,
            window_account_key=acct_key,
            build_payload=_claude_budget_payload,
        )


def maybe_record_project_budget_milestone(
    saved: dict[str, Any], *, conn=None, as_of: "str | None" = None,
    alert_sink: "list | None" = None,
) -> None:
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

    now_utc = _as_of_or_command(as_of)
    pending_alerts: list[dict[str, Any]] = []
    own_conn = conn is None
    if own_conn:
        conn = open_db()
    try:
        # #341 (spec §6 rev-4): project_budget_milestones is a `*`-scoped
        # subscription-week writer, so it anchors "the current week" on the
        # ACTIVE account's week under the same skip+WARN rule as the vendor
        # budget ladder. TORN identity → skip + one-shot WARN (never guess an
        # anchor); identified/stably-absent → scope the window to that account
        # (<=1-real-account resolves `unattributed` = byte-stable).
        pb_window_key = None
        ident = _cctally_core._resolve_active_claude_identity()
        if ident.get("status") == "torn":
            _warn_budget_active_anchor_unavailable_once()
            return
        pb_window_key = ident.get("account_key")
        window = _resolve_current_budget_window(
            conn, now_utc, account_key=pb_window_key)
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
                as_of=as_of,
            )
            # Only the genuine-new-crossing winner (rowcount==1) dispatches; a
            # racing record-usage instance OR an already-recorded pair gets
            # rowcount==0 and skips.
            if inserted == 1:
                crossed_at = as_of or now_utc_iso()
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
        # On the passed-conn (ingest) path the caller owns commit + dispatch.
        if own_conn:
            conn.commit()
    except Exception as exc:
        # Exception discipline (6c-gate P1): passed-conn (ingest) path re-raises
        # so a failure ABORTS the cycle (invariant ii); legacy own-connection
        # swallows so a standalone record-usage tick never regresses.
        if not own_conn:
            raise
        eprint(
            f"[project-budget-milestone] error recording project budget "
            f"milestone: {exc}"
        )
    finally:
        if own_conn:
            conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant — one queue attempt per crossing, deduped on
    # the alerted_at column). Skipped on the passed-conn path (caller dispatches).
    if own_conn:
        for payload in pending_alerts:
            try:
                _dispatch_alert_notification(payload, mode="real")
            except Exception as dispatch_exc:
                eprint(f"[project-budget-alerts] dispatch failed: {dispatch_exc}")
    elif alert_sink is not None:
        alert_sink.extend(pending_alerts)


def maybe_record_codex_budget_milestone(
    saved: dict[str, Any], *, raise_errors: bool = False, conn=None, as_of=None,
    alert_sink: "list | None" = None,
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
    # Per-account Codex ladders (#341 Step 4-eval, spec §6): `budget.codex.accounts`
    # is a {account_key: usd} map. A Codex budget can be per-account-ONLY (no
    # vendor-wide `amount_usd`), so the gate accepts either a vendor-wide amount OR
    # a non-empty per-account map. Codex is calendar-anchored (no subscription
    # week -> no `*`-anchor rule).
    accounts = codex_cfg.get("accounts") or {}
    if not thresholds or (target is None and not accounts):
        return 0
    tz = resolve_display_tz(argparse.Namespace(tz=None), config)

    def _codex_budget_payload(**kw):
        # The Codex payload builder takes `period_start_at=` directly (== the
        # resolved period-start instant, == period_key), so the at-fire dispatch
        # id stays byte-stable `codex_budget:<period_start_at>:<threshold>`.
        # `account_key` (#341) rides through to the [label] prefix + log field.
        return _build_alert_payload_codex_budget(
            threshold=kw["threshold"],
            crossed_at_utc=kw["crossed_at_utc"],
            period_start_at=kw["period_key"],
            period=kw["period"],
            budget_usd=kw["budget_usd"],
            spent_usd=kw["spent_usd"],
            consumption_pct=kw["consumption_pct"],
            account_key=kw["account_key"],
        )

    fired = 0
    if target is not None:
        fired += _record_budget_milestone_for_vendor(
            vendor="codex", target=target, thresholds=thresholds,
            period=codex_cfg["period"], config=config, tz=tz,
            build_payload=_codex_budget_payload, account_key="*",
            raise_errors=raise_errors, conn=conn, as_of=as_of,
            alert_sink=alert_sink,
        ) or 0
    for acct_key, acct_usd in accounts.items():
        fired += _record_budget_milestone_for_vendor(
            vendor="codex", target=acct_usd, thresholds=thresholds,
            period=codex_cfg["period"], config=config, tz=tz,
            build_payload=_codex_budget_payload, account_key=acct_key,
            raise_errors=raise_errors, conn=conn, as_of=as_of,
            alert_sink=alert_sink,
        ) or 0
    return fired


def _forecast_calibrated_projection(
        now_utc, week_start_at, week_end_at, *, account_key=None):
    """Return the model-backed projection the forecast would publish here.

    `apply_regime` re-tests support against THIS week's population, so a
    calibration can validate while the forecast still falls back to the
    corrected meter. Retaining the numeric result instead of reducing it to a
    boolean lets the alert fire on exactly the calibrated value the forecast
    publishes, without a second week scan or a contradictory meter fallback.

    This calls the SAME helper the loader calls, with the SAME account key,
    so the two cannot answer differently. The earlier probe always read the
    merged bucket while the loader used its caller's key, which on a
    decorated multi-account install let the forecast publish a calibrated
    projection while the twin saw no merged regime and fired on the meter.

    Any failure to reach the helper returns ``None`` and leaves the alert on
    the corrected-meter fallback rather than making it go quiet.

    COST. This is NOT a cheap probe once a regime validates. `_calibrated_projection`
    reads the calibration file first — one small file read on every install
    that has never run `cctally quota`, and the common case — but when a
    regime does validate it then opens `cache.db` and runs a full current-week
    `session_entries` SELECT, on a path `record-usage` and `hook-tick` reach
    once per prompt. Measured over the twelve most recent completed
    subscription weeks of the maintainer's August 2026 store snapshot
    (476,216 `session_entries` rows), on an Apple M4 Max Mac Studio under
    CPython 3.14 with the file cache warm, a WHOLE week is a median 16,923
    rows and 28 ms and at worst 27,664 rows and 46 ms. This read covers the
    current week only as far as now, so it is a fraction of that early in
    the week and reaches it by the week's end. The durations are this
    machine's and another host will differ; what does not vary is that the
    read is one unbounded week-scan rather than a bounded probe. A cheap bound is available in principle — the same
    query with `COUNT(*)`, or a bounded `LIMIT 1` existence probe, would
    answer the empty-population half of `apply_regime`'s rejection without
    materializing the rows — but it would not answer the composition-support
    half, which needs the whole population. Not implemented here.
    """
    try:
        value, _code = _cctally()._load_sibling(
            "_cctally_forecast")._calibrated_projection(
                now_utc, week_start_at, week_end_at, account_key=account_key)
        return value
    except Exception:
        return None


def _weekly_pct_week_avg_projection(conn, now_utc, *, account_key=None):
    """Compute the week-AVERAGE weekly-% projection for the current
    subscription week on the same selected basis as ``forecast``.

    ``account_key`` is an AFFORDANCE, not a threaded production value. The one
    shipped caller (the projected-alert leg in ``maybe_record_projected_alert``)
    passes none, and the window it resolves just above comes from a merged
    ``_fetch_current_week_snapshots``, so the whole projected-alert path is
    account-blind today. Making it per-account is a #341 change to that leg
    rather than to this helper, and this session does not make it. The
    parameter exists so the calibration call below can be given the SAME key
    the forecast loader would use once the leg is threaded.

    Returns ``(projected_pct, low_conf)`` or ``None`` when no current-week
    snapshot resolves. The value follows the IDENTICAL basis selector and
    inputs that produce ``ForecastOutput.week_avg_projection_pct``
    (``_load_forecast_inputs`` → ``_compute_forecast``). A supported fitted
    regime supplies the numeric calibrated projection from the alert path's
    existing week scan. Otherwise ``p_now`` / elapsed / remaining come from
    ``_fetch_current_week_snapshots`` + ``_apply_midweek_reset_override`` and
    the corrected-meter fallback uses ``p_now + r_avg * remaining_hours``.
    The reconcile invariant binds the fired value to forecast's
    ``week_avg_projection_pct`` within 1e-9 on either basis.

    LOW CONF mirrors the displayed forecast confidence: ONE call to
    ``_assess_forecast_confidence(elapsed_hours, p_now, len(samples),
    has_sample_ge_24h=…)``, whose fourth trigger ``no_sample_ge_24h`` moved
    inside the predicate with #620 S2 E3 — so a thin early-week window that
    forecast renders ``LOW CONF`` never fires a projected alert, and glue no
    longer appends a reason of its own.

    Deliberately does NOT call ``_sum_cost_for_range`` (the weekly-% projection
    needs no spend; the forecast kernel's ``week_avg_projection_pct`` is also
    spend-free), and never calls ``sync_cache``.

    It is NOT, however, snapshot-only any more. The calibrated-basis selector
    below calls ``_forecast_calibrated_projection``, which reads the
    calibration file and — when a regime validates — opens ``cache.db`` for a
    full current-week ``session_entries`` SELECT. See that helper's COST note.
    """
    fetched = _fetch_current_week_snapshots(conn, now_utc,
                                            account_key=account_key)
    if fetched is None:
        return None
    week_start_at, week_end_at, samples = fetched
    week_start_at, samples = _apply_midweek_reset_override(
        conn, week_start_at, week_end_at, samples, now_utc=now_utc
    )
    if not samples:
        return None
    p_now = samples[-1][1]
    elapsed_hours = (now_utc - week_start_at).total_seconds() / 3600.0
    remaining_hours = max(0.0, (week_end_at - now_utc).total_seconds() / 3600.0)
    # #661 S2 spec section 3.1/3.3: the same CEILING-CORRECTED operand
    # `_compute_forecast` projects from. The reconcile invariant PROJECTED1
    # binds this value to `forecast --json`'s `week_avg_projection_pct`
    # within 1e-9, so the two must share the operand as well as the formula.
    # A right-censored reading has no corrected point and therefore no
    # corrected-meter projection at all: the fallback withholds rather than
    # firing on a number the observation cannot supply. A calibrated result
    # returned above remains usable at a censored meter.
    # Imported HERE rather than at module scope, and NOT for a measured
    # saving. An earlier revision of this comment claimed the module-scope
    # form cost `record-usage` and `hook-tick` 18.5 ms per run, about 11 ms of
    # it `_lib_quota_model` "which neither command previously loaded at all".
    # That is false: `bin/cctally` loads `_lib_quota_model` at line 574 and
    # `_cctally_forecast` at line 1667, both unconditionally, and
    # `_cctally_forecast` honest-imports `_lib_forecast` at its own module
    # top. By the time either command reaches this helper the module is
    # already in `sys.modules`, and the incremental import measures 0.000 ms.
    # 18.5 ms is the COLD import into a bare interpreter, which no shipped
    # path performs. What the local form does buy is correctness: it pairs
    # with `_ensure_sibling_loaded`, which the module-scope form omitted, so
    # it does not depend on `_load_sibling` having happened to insert `bin/`
    # into `sys.path` at `bin/cctally:156`.
    _ensure_sibling_loaded("_lib_forecast")
    from _lib_forecast import corrected_percent_point

    # Confidence comes from the predicate in one call, fourth trigger
    # included, so this LOW CONF gate == forecast's and glue no longer
    # downgrades a confidence the predicate returned (#620 S2 E3).
    target_24h = now_utc - dt.timedelta(hours=24)
    confidence, _reasons = _assess_forecast_confidence(
        elapsed_hours, p_now, len(samples),
        has_sample_ge_24h=any(s[0] <= target_24h for s in samples),
    )

    calibrated = _forecast_calibrated_projection(
        now_utc, week_start_at, week_end_at, account_key=account_key)
    if calibrated is not None:
        return (calibrated, confidence == "low")

    p_corrected = corrected_percent_point(p_now)
    if p_corrected is None:
        return None
    r_avg = p_corrected / elapsed_hours if elapsed_hours > 0 else 0.0
    projected_pct = p_corrected + r_avg * remaining_hours
    return (projected_pct, confidence == "low")


def maybe_record_projected_alert(
    saved: dict[str, Any], *, only_metrics=None, conn=None, as_of=None,
    alert_sink: "list | None" = None,
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

    now_utc = _as_of_or_command(as_of)
    pending: list[dict[str, Any]] = []
    own_conn = conn is None
    if own_conn:
        conn = open_db()
    try:
        # ── weekly_pct leg (snapshots + optional calibrated week scan) ─────
        if weekly_on:
            w_window = _fetch_current_week_snapshots(conn, now_utc)
            if w_window is not None:
                ws_at, we_at, samples = w_window
                ws_at, _ = _apply_midweek_reset_override(
                    conn, ws_at, we_at, samples, now_utc=now_utc
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
            # #341 (spec §6 rev-4): the vendor-budget projected metric is a
            # `*`-scoped subscription-week writer, so it anchors on the ACTIVE
            # account's week under the same skip+WARN rule. TORN identity → skip
            # ONLY this leg (the weekly_pct + codex legs still run); identified /
            # stably-absent → scope the window (byte-stable at <=1 real account).
            # Calendar periods ignore the key (pure calendar).
            bu_window_key = None
            bu_leg_ok = True
            if claude_period == "subscription-week":
                _bu_ident = _cctally_core._resolve_active_claude_identity()
                if _bu_ident.get("status") == "torn":
                    _warn_budget_active_anchor_unavailable_once()
                    bu_leg_ok = False
                else:
                    bu_window_key = _bu_ident.get("account_key")
            # Resolve the window key CHEAPLY first (SUM-free, same resolver the
            # actual-budget axis uses) so the pre-probe can short-circuit BEFORE
            # _build_vendor_budget_inputs runs any cost SUM / cache sync — the
            # pre-probe-runs-first contract (spec §3.4; mirrors the actual axis).
            window = _resolve_claude_budget_window(
                conn, now_utc, period=claude_period, config=cfg, tz=config_tz,
                window_account_key=bu_window_key,
            ) if bu_leg_ok else None
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
                        window_account_key=bu_window_key,
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
                as_of=as_of,
            )
            # Only the genuine-new-crossing winner (rowcount==1) arms+dispatches;
            # a racing record-usage instance gets rowcount==0 and skips. The
            # alerted_at UPDATE keys on the CONCRETE `period` (#137).
            if inserted == 1:
                conn.execute(
                    "UPDATE projected_milestones SET alerted_at = ? "
                    "WHERE week_start_at = ? AND period = ? AND metric = ? "
                    "  AND threshold = ? AND alerted_at IS NULL",
                    (as_of or now_utc_iso(), p["week_start_at"], p["period"],
                     p["metric"], p["threshold"]),
                )
                fired.append(p)
        # Single commit: every INSERT + its alerted_at marker durable together.
        # On the passed-conn (ingest) path the caller owns commit + dispatch.
        if own_conn:
            conn.commit()
    except Exception as exc:
        # Exception discipline (6c-gate P1): passed-conn (ingest) path re-raises
        # so a failure ABORTS the cycle (invariant ii); legacy own-connection
        # swallows so a standalone record-usage tick never regresses.
        if not own_conn:
            raise
        eprint(f"[projected-alert] error recording projected milestone: {exc}")
        fired = []
    finally:
        if own_conn:
            conn.close()

    # Dispatch AFTER commit; a dispatch failure NEVER rolls back the milestone
    # (set-then-dispatch invariant). Skipped on the passed-conn path (the
    # ingester's ALERT_DISPATCHER dispatches).
    if own_conn:
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
    elif alert_sink is not None:
        # Passed-conn (ingest) path: hand the projected crossings to the caller's
        # sink for the ingester's post-commit dispatch (spec §5.2 step 6). The
        # alerted_at stamp already landed in the caller's txn.
        for p in fired:
            try:
                alert_sink.append(_build_alert_payload_projected(
                    metric=p["metric"],
                    threshold=p["threshold"],
                    projected_value=p["projected_value"],
                    denominator=p["denominator"],
                    week_start_at=p["week_start_at"],
                ))
            except Exception as build_exc:
                eprint(f"[projected-alert] payload build failed: {build_exc}")


@dataclass
class PricedEntry:
    """One accounting entry after pricing, as the block fold consumes it.

    The canonical priced record: the fields `fold_block_totals` reads and
    nothing else. `_compute_block_totals` builds these from
    `_JoinedClaudeEntry` rows it has already priced; the diagnosis
    (#620 S2) builds them from its own half-open account-scoped window.
    """
    model: str
    project_path: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float


@dataclass
class BlockBucket:
    """One model or project bucket inside a block's totals."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_create_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    entry_count: int = 0

    def as_legacy_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_create_tokens": self.cache_create_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
            "entry_count": self.entry_count,
        }


@dataclass
class BlockTotals:
    """The summed result of `fold_block_totals`."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_create_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    entry_count: int = 0
    by_model: dict[str, BlockBucket] = field(default_factory=dict)
    by_project: dict[str, BlockBucket] = field(default_factory=dict)

    def as_legacy_dict(self) -> dict[str, Any]:
        """The exact dict `_compute_block_totals` has always returned.

        Key order is preserved because callers and fixture builders read
        this dict directly, and `entry_count` is deliberately absent at the
        top level: it exists on the dataclass for the diagnosis, and adding
        it here would change a shape every existing caller sees.
        """
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_create_tokens": self.cache_create_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
            "by_model": {k: v.as_legacy_dict() for k, v in self.by_model.items()},
            "by_project": {k: v.as_legacy_dict() for k, v in self.by_project.items()},
        }


def fold_block_totals(entries: "Iterable[PricedEntry]") -> BlockTotals:
    """Sum priced entries into block totals plus model and project buckets.

    Pure: it opens nothing, prices nothing, and reads no clock. Summation
    follows the iteration order of `entries`, and bucket insertion order
    follows first appearance, both of which the callers observe.

    A NULL `project_path` buckets under `(unknown)` so the reconcile
    invariant `SUM(child.cost) == parent.total` continues to hold. The
    JSONL-fallback loader always populates `project_path`, so `(unknown)`
    only appears on the cache-backed path during the brief `session_files`
    lazy-backfill window.
    """
    totals = BlockTotals()
    for entry in entries:
        totals.input_tokens += entry.input_tokens
        totals.output_tokens += entry.output_tokens
        totals.cache_create_tokens += entry.cache_creation_tokens
        totals.cache_read_tokens += entry.cache_read_tokens
        totals.cost_usd += entry.cost_usd
        totals.entry_count += 1

        for key, bucket_dict in (
            (entry.model, totals.by_model),
            (entry.project_path or "(unknown)", totals.by_project),
        ):
            b = bucket_dict.setdefault(key, BlockBucket())
            b.input_tokens += entry.input_tokens
            b.output_tokens += entry.output_tokens
            b.cache_create_tokens += entry.cache_creation_tokens
            b.cache_read_tokens += entry.cache_read_tokens
            b.cost_usd += entry.cost_usd
            b.entry_count += 1
    return totals


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
    def _priced():
        for entry in get_claude_session_entries(
            block_start_at, range_end, skip_sync=skip_sync,
        ):
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
            yield PricedEntry(
                model=entry.model,
                project_path=entry.project_path,
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cost_usd=cost,
            )

    return fold_block_totals(_priced()).as_legacy_dict()


def maybe_update_five_hour_block(
    saved: dict[str, Any], *, conn=None, as_of: "str | None" = None,
    alert_sink: "list | None" = None, account_key: str = _lib_accounts.UNATTRIBUTED,
    journal_ctx=None,
) -> None:
    """Upsert the current 5h block in five_hour_blocks; close strictly
    older open blocks; sweep naturally-expired blocks; flag blocks
    spanning a recorded mid-week 7d-reset.

    ``account_key`` (#341): the account the 5h block belongs to. Every
    block/child/milestone query below is scoped to it (composite
    ``(account_key, five_hour_window_key)`` identity — spec review finding 3) so
    two accounts observing the SAME physical 5h window each own their own block
    and children. Default ``"unattributed"`` is the rev-4.1 defensive backstop;
    the ingest pipeline hook passes the resolved account explicitly.

    Errors are logged and swallowed — record-usage must not regress
    because of this helper, same posture as maybe_record_milestone.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    when ``conn`` is passed the block upsert + milestone fold + cross-flag sweep
    run on the caller's transaction — no internal ``open_db()``, no
    ``BEGIN IMMEDIATE``/``commit()``/``close()``, and alert dispatch is left to
    the caller (the ingester's ALERT_DISPATCHER). ``as_of`` (ISO-Z) is stamped
    as ``last_updated_at_utc`` / ``alerted_at`` in place of wall clock. Both
    defaults keep the legacy own-connection, own-transaction behavior.

    ``journal_ctx`` makes a close durable at the transition itself. The complete
    parent + model/project child sets are frozen before the transaction can
    commit; an already-stamped closed block is immutable to later observations
    and mutable cache growth."""
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
    own_conn = conn is None
    if own_conn:
        conn = open_db()
    try:
        # Step 3 (per spec §3.2): read prior state including immutable
        # fields we'll re-use. Re-deriving block_start_at from saved.
        # fiveHourResetsAt would reintroduce the seconds-level Anthropic
        # ISO jitter that five_hour_window_key was designed to collapse.
        prior = conn.execute(
            """
            SELECT id              AS prior_block_id,
                   block_start_at  AS block_start_at,
                   is_closed       AS is_closed,
                   journal_id      AS journal_id,
                   last_updated_at_utc AS last_updated_at_utc
              FROM five_hour_blocks
             WHERE five_hour_window_key = ?
               AND account_key = ?
            """,
            (int(five_hour_window_key), account_key),
        ).fetchone()

        # Whole-table emptiness BEFORE this tick's upsert — the exact trigger of
        # the legacy `_backfill_five_hour_blocks` (open_db()-gated on "blocks
        # empty + snapshots present"). It's dead in the DB journal redesign
        # (snapshot + block share one cycle txn), so the post-insert close below
        # replicates only its is_closed = (resets < now) effect for the FIRST
        # block. Gating on this (not just `resets < now`) keeps a strictly-newer
        # historical block open — the backfill never touched a non-first insert.
        blocks_were_empty = conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks WHERE account_key = ?",
            (account_key,),
        ).fetchone()[0] == 0

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
        current_is_frozen = (
            prior is not None
            and int(prior["is_closed"]) == 1
            and prior["journal_id"] is not None
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
        now_iso = as_of or now_utc_iso()
        # BEGIN IMMEDIATE (not deferred): the first DML below is a write (the
        # close-older UPDATE), so this transaction already takes the write lock
        # up front today. Stating IMMEDIATE makes that the explicit contract —
        # a future edit that slips a SELECT before the first write here cannot
        # silently reintroduce a SQLITE_BUSY_SNAPSHOT crash (busy_timeout does
        # not absorb that). See cctally-dev#87. On the passed-conn (ingest) path
        # the caller already owns the transaction, so we do NOT open a nested
        # one — the DML runs directly in the caller's txn and the caller commits.
        if own_conn:
            conn.execute("BEGIN IMMEDIATE")
        try:
            # Capture the exact transition set before mutating it. A close is
            # triggered either by a retained successor-window observation or
            # by the first retained observation captured after natural expiry.
            # Both rules use ``now_iso`` (the observation's capture clock),
            # never ingest/retry wall time.
            closing_ids = {
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM five_hour_blocks "
                    "WHERE is_closed = 0 AND account_key = ? "
                    "AND (five_hour_window_key < ? "
                    "     OR unixepoch(five_hour_resets_at) < unixepoch(?)) "
                    "ORDER BY id",
                    (account_key, int(five_hour_window_key), now_iso),
                ).fetchall()
            }
            # A lost-commit retry replays the frozen event before reprocessing
            # its trigger observation. Re-emit that exact duplicate only when
            # this retained trigger clock matches the frozen closure clock;
            # later ordinary observations do not churn duplicate lines.
            retry_close_ids = {
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM five_hour_blocks "
                    "WHERE is_closed = 1 AND journal_id IS NOT NULL "
                    "AND account_key = ? AND last_updated_at_utc = ? "
                    "AND (five_hour_window_key < ? "
                    "     OR unixepoch(five_hour_resets_at) < unixepoch(?)) "
                    "ORDER BY id",
                    (
                        account_key,
                        now_iso,
                        int(five_hour_window_key),
                        now_iso,
                    ),
                ).fetchall()
            }

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
                   AND account_key = ?
                   AND five_hour_window_key < ?
                """,
                (now_iso, account_key, int(five_hour_window_key)),
            )

            # Step 5b: natural-expiration sweep. The close-older predicate
            # above only fires when a strictly-newer window arrives. A user
            # who lets a block expire without a successor (idle / shut down
            # past the 5h reset) would otherwise leave the row at
            # is_closed = 0 forever. Idempotent (only flips 0 → 1); safe to
            # re-run every tick. Normalize through unixepoch() because retained
            # reset/capture stamps may use different equivalent UTC suffixes.
            conn.execute(
                """
                UPDATE five_hour_blocks
                   SET is_closed = 1, last_updated_at_utc = ?
                 WHERE is_closed = 0
                   AND account_key = ?
                   AND unixepoch(five_hour_resets_at) < unixepoch(?)
                """,
                (now_iso, account_key, now_iso),
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
                  last_updated_at_utc,
                  account_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(account_key, five_hour_window_key) DO UPDATE SET
                  last_observed_at_utc       = excluded.last_observed_at_utc,
                  final_five_hour_percent    = excluded.final_five_hour_percent,
                  seven_day_pct_at_block_end = excluded.seven_day_pct_at_block_end,
                  total_input_tokens         = excluded.total_input_tokens,
                  total_output_tokens        = excluded.total_output_tokens,
                  total_cache_create_tokens  = excluded.total_cache_create_tokens,
                  total_cache_read_tokens    = excluded.total_cache_read_tokens,
                  total_cost_usd             = excluded.total_cost_usd,
                  last_updated_at_utc        = excluded.last_updated_at_utc
                WHERE five_hour_blocks.is_closed = 0
                   OR five_hour_blocks.journal_id IS NULL
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
                    account_key,
                ),
            )

            # ── Resolve current block_id once for reuse by the per-(block, model)
            # / per-(block, project) child writes below AND the existing milestone
            # detection (which previously did its own SELECT — drop that SELECT in
            # favor of this variable). Composite (account_key, five_hour_window_key)
            # so a shared physical window resolves THIS account's block (#341).
            block_id_row = conn.execute(
                "SELECT id FROM five_hour_blocks "
                "WHERE five_hour_window_key = ? AND account_key = ?",
                (int(five_hour_window_key), account_key),
            ).fetchone()
            block_id = int(block_id_row["id"])

            # Close the just-upserted FIRST block if its window already expired at
            # capture time — replicating the (journal-model-dead)
            # `_backfill_five_hour_blocks` is_closed = (resets < now) effect. Gated
            # on `blocks_were_empty` (the backfill's own "first block" trigger) so
            # a strictly-newer historical block stays open (scenario D), and on
            # `is_closed = 0` so it is idempotent + a no-op for a legacy own-conn
            # caller whose backfill already closed the historical block. `<`
            # instant-compares resets_at against now_iso, same as the sweep.
            if blocks_were_empty:
                conn.execute(
                    "UPDATE five_hour_blocks SET is_closed = 1, last_updated_at_utc = ? "
                    "WHERE five_hour_window_key = ? AND account_key = ? "
                    "  AND is_closed = 0 "
                    "  AND unixepoch(five_hour_resets_at) < unixepoch(?)",
                    (now_iso, int(five_hour_window_key), account_key, now_iso),
                )

            # ── Replace-all per-tick: per-(block, model) and per-(block, project_path)
            # rollup-children. DELETE keyed on five_hour_window_key (NOT block_id) so
            # orphan child rows from a prior parent rebuild are cleaned up automatically.
            # Same transaction as the parent upsert; if these raise, the whole tick
            # rolls back and the next tick recomputes from scratch.
            if not current_is_frozen:
                conn.execute(
                    "DELETE FROM five_hour_block_models "
                    "WHERE five_hour_window_key = ? AND account_key = ?",
                    (int(five_hour_window_key), account_key),
                )
                if totals.get("by_model"):
                    conn.executemany(
                        """
                        INSERT INTO five_hour_block_models (
                          block_id, five_hour_window_key, model,
                          input_tokens, output_tokens,
                          cache_create_tokens, cache_read_tokens,
                          cost_usd, entry_count, account_key
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                account_key,
                            )
                            for model, b in totals["by_model"].items()
                        ],
                    )

                conn.execute(
                    "DELETE FROM five_hour_block_projects "
                    "WHERE five_hour_window_key = ? AND account_key = ?",
                    (int(five_hour_window_key), account_key),
                )
                if totals.get("by_project"):
                    conn.executemany(
                        """
                        INSERT INTO five_hour_block_projects (
                          block_id, five_hour_window_key, project_path,
                          input_tokens, output_tokens,
                          cache_create_tokens, cache_read_tokens,
                          cost_usd, entry_count, account_key
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                account_key,
                            )
                            for project_path, b in totals["by_project"].items()
                        ],
                    )

            # The first historical block is inserted after the sweep above, so
            # include it explicitly when this observation closes it.
            if blocks_were_empty:
                closed_now = conn.execute(
                    "SELECT id FROM five_hour_blocks "
                    "WHERE id = ? AND is_closed = 1 AND journal_id IS NULL",
                    (block_id,),
                ).fetchone()
                if closed_now is not None:
                    closing_ids.add(int(closed_now["id"]))

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
                conn, int(five_hour_window_key), account_key=account_key
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
                    "WHERE five_hour_window_key = ? AND reset_event_id = ? "
                    "  AND account_key = ?",
                    (int(five_hour_window_key), active_reset_event_id, account_key),
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
                            "  AND reset_event_id = ? "
                            "  AND account_key = ?",
                            (int(five_hour_window_key), int(max_existing),
                             active_reset_event_id, account_key),
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
                              reset_event_id,
                              account_key
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                account_key,
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
                            crossed_at = as_of or now_utc_iso()
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
                                "  AND account_key = ? "
                                "  AND alerted_at IS NULL",
                                (crossed_at, int(five_hour_window_key),
                                 int(pct), active_reset_event_id, account_key),
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
                                "  AND reset_event_id = ? "
                                "  AND account_key = ?",
                                (int(five_hour_window_key), int(pct),
                                 active_reset_event_id, account_key),
                            ).fetchone()
                            block_row = conn.execute(
                                "SELECT block_start_at FROM five_hour_blocks "
                                "WHERE five_hour_window_key = ? AND account_key = ?",
                                (int(five_hour_window_key), account_key),
                            ).fetchone()
                            primary_model = _resolve_primary_model_for_block(
                                conn, int(five_hour_window_key),
                                account_key=account_key,
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
                                account_key=account_key,
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
                   AND account_key = ?
                   AND (is_closed = 0 OR journal_id IS NULL)
                   AND (
                     EXISTS (
                       -- #703 + #707: the ACCOUNTING instant, so a block is
                       -- flagged as crossing the moment the counter actually
                       -- moved rather than the hour it is displayed at. The
                       -- hour-floored instant can fall in the previous block.
                       SELECT 1 FROM week_reset_events e
                        WHERE e.account_key = five_hour_blocks.account_key
                          AND unixepoch(COALESCE(e.observed_at_utc,
                                                 e.effective_reset_at_utc))
                              BETWEEN unixepoch(five_hour_blocks.block_start_at)
                                  AND unixepoch(five_hour_blocks.last_observed_at_utc)
                     )
                     OR EXISTS (
                       SELECT 1 FROM weekly_usage_snapshots ws
                        WHERE ws.week_start_at IS NOT NULL
                          AND ws.account_key = five_hour_blocks.account_key
                          AND unixepoch(ws.week_start_at)
                              >  unixepoch(five_hour_blocks.block_start_at)
                          AND unixepoch(ws.week_start_at)
                              <= unixepoch(five_hour_blocks.last_observed_at_utc)
                     )
                   )
                """,
                (account_key,),
            )

            # Freeze only after every parent field and both rollup child sets
            # have reached their final close-state values for this observation.
            # The cross-reset sweep above is part of the parent fact, so moving
            # this earlier would journal ``crossed_seven_day_reset = 0`` and
            # then mutate the live closed row to 1.
            if journal_ctx is not None:
                import _cctally_journal as _jr

                for closing_id in sorted(closing_ids):
                    _jr.freeze_five_hour_block_close(journal_ctx, closing_id)
                for retry_id in sorted(retry_close_ids - closing_ids):
                    _jr.freeze_five_hour_block_close(journal_ctx, retry_id)

            if own_conn:
                conn.commit()
        except Exception:
            if own_conn:
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
        # On the passed-conn (ingest) path the caller owns the commit and the
        # post-commit alert dispatch (the ingester's ALERT_DISPATCHER), so both
        # are skipped here.
        if own_conn:
            for payload in pending_alerts:
                try:
                    _dispatch_alert_notification(
                        payload, mode="real", tz=display_tz_for_alerts
                    )
                except Exception as dispatch_exc:
                    eprint(f"[alerts] dispatch failed: {dispatch_exc}")
        elif alert_sink is not None:
            # Passed-conn (ingest) path: hand the 5h-milestone new-crossing
            # payloads to the caller's sink for post-commit dispatch (spec §5.2
            # step 6). alerted_at was stamped in the caller's txn above.
            alert_sink.extend(pending_alerts)
    except Exception as exc:
        # Exception discipline (6b-gate P2): on the passed-conn (ingest) path a
        # chokepoint exception must ABORT the cycle — re-raise so the ingester
        # rolls back the txn, leaves the cursor unmoved, and makes no partial
        # commit (invariant ii). Only the legacy own-connection path keeps the
        # log-and-swallow so a standalone record-usage tick never regresses on a
        # 5h-block hiccup.
        if not own_conn:
            raise
        eprint(f"[5h-block] error updating block: {exc}")
    finally:
        if own_conn:
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


def _reset_zero_marker_path(account_key=None):
    """The marker file for one account, or the legacy shared one.

    #703 + #707 §6.2a: the arm belongs to ONE account and there used to be a
    single file for all of them, so a second account's tick both confirmed off
    the first account's baseline and — once the confirm was scoped — OVERWROTE
    the first account's arm by arming its own. One file per account removes both
    without a read-modify-write, so two accounts arming concurrently cannot lose
    each other's arm.

    ``account_key`` ``None`` names the LEGACY shared file, which is what a
    binary this install is upgrading from wrote. It is read as a fallback and
    matches any account; nothing writes it any more.

    The suffix is sanitized because it becomes a filename. Account keys are hex
    digests or the ``unattributed`` sentinel today, so the substitution is a
    guard rather than a transformation that fires.
    """
    base = _cctally_core.APP_DIR / _RESET_ZERO_MARKER_NAME
    if not account_key:
        return base
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(account_key))
    return base.with_name(f"{_RESET_ZERO_MARKER_NAME}.{safe}")


_MARKER_ABSENT = "-"


def _arm_reset_zero_marker(week_start_date, cur_end_canon, *,
                           baseline_pct, first_zero_iso,
                           first_zero_capture_iso=None,
                           first_zero_identity=None,
                           account_key=None):
    """Persist the pending reset-to-zero candidate. ``first_zero_iso`` MUST be
    the ``_command_as_of()`` clock value (it becomes the effective anchor on
    confirm), NOT wall-clock.

    ``first_zero_capture_iso`` and ``first_zero_identity`` are the two facts
    #703 + #707 need from the FIRST zero and that the confirming tick can no
    longer see. The capture instant is the credit's `observed_at_utc`, which
    must be in the same clock domain as `weekly_usage_snapshots.captured_at_utc`
    because that is the column it filters; the identity is the observation's
    journal identity, from which the credit's `credit_key` is derived. Both were
    previously discarded on the confirm leg, which is why the epoch anchored on
    the hour floor and back-dated before observations that were still
    legitimately pre-credit.

    ``account_key`` (#703 + #707 §6.2a) is the account the arm belongs to. There
    is ONE marker file, so without it a zero arriving for a DIFFERENT account
    matched the arm and confirmed a credit built from the first account's
    baseline — and cleared the marker, leaving the account that genuinely lost
    its counter unrepaired on the very tick that should have repaired it.

    Every absent field is written as a literal ``-``, so the field count stays
    fixed and a whitespace split keeps working. Account keys are hex digests or
    the ``unattributed`` sentinel, so none contains whitespace."""
    try:
        _reset_zero_marker_path(account_key).write_text(
            f"{week_start_date} {cur_end_canon} "
            f"{float(baseline_pct)} {first_zero_iso} "
            f"{first_zero_capture_iso or _MARKER_ABSENT} "
            f"{first_zero_identity or _MARKER_ABSENT} "
            f"{account_key or _MARKER_ABSENT}\n"
        )
    except OSError:
        # The per-account arm was NOT written, so the legacy one is still the
        # only arm this install has. Retiring it here destroyed the arm the
        # failed write was meant to replace, and the swallowed OSError meant
        # nothing said so.
        return
    _retire_legacy_reset_zero_marker(account_key)


def _retire_legacy_reset_zero_marker(account_key):
    """Remove the pre-account shared marker once a per-account one exists.

    Left in place it would keep matching every account whose own file is absent,
    forever. It is only ever written by the binary an install is upgrading from,
    so it exists for at most the one tick that spans the upgrade — during which
    a legacy arm for a DIFFERENT account is lost. That is the bounded cost of
    not leaving a permanently account-blind marker behind.
    """
    if not account_key:
        return
    try:
        _reset_zero_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _clear_reset_zero_marker(account_key=None):
    try:
        _reset_zero_marker_path(account_key).unlink(missing_ok=True)
    except OSError:
        pass
    _retire_legacy_reset_zero_marker(account_key)


def _read_reset_zero_marker(account_key=None):
    """Return ``(week_start_date, cur_end_canon, baseline_pct, first_zero_iso,
    first_zero_capture_iso, first_zero_identity, account_key)`` or ``None`` when
    missing / empty / garbled. Validates ALL fields (arity, float baseline,
    parseable timestamps) so a malformed marker re-arms cleanly rather than
    wedging the confirm path.

    Three arities are accepted, and the shorter two are the shapes a binary an
    install is upgrading FROM wrote: four fields predate the exact-instant and
    identity facts, six predate the account. Each is read with the missing
    fields ``None`` rather than rejected, because rejecting one would drop a
    genuine armed reset on the single tick that spans the upgrade. The confirm
    leg falls back to the confirming observation for the two facts, and treats a
    marker with no recorded account as matching any account — which is exactly
    what it did before the field existed."""
    raw = ""
    for path in (_reset_zero_marker_path(account_key),
                 _reset_zero_marker_path()):
        try:
            raw = path.read_text().strip()
        except OSError:
            raw = ""
        if raw:
            break
    if not raw:
        return None
    parts = raw.split()
    if len(parts) not in (4, 6, 7):
        return None
    week_start_date, cur_end_canon, baseline_raw, first_zero_iso = parts[:4]
    capture_iso = identity = account_key = None
    if len(parts) >= 6:
        capture_iso, identity = parts[4], parts[5]
        capture_iso = None if capture_iso == _MARKER_ABSENT else capture_iso
        identity = None if identity == _MARKER_ABSENT else identity
    if len(parts) == 7:
        account_key = None if parts[6] == _MARKER_ABSENT else parts[6]
    try:
        baseline_pct = float(baseline_raw)
    except ValueError:
        return None
    try:
        parse_iso_datetime(first_zero_iso, "reset_zero_marker.first_zero")
        if capture_iso is not None:
            parse_iso_datetime(capture_iso, "reset_zero_marker.first_capture")
    except ValueError:
        return None
    return (week_start_date, cur_end_canon, baseline_pct, first_zero_iso,
            capture_iso, identity, account_key)


def _projection_read_reset_zero_marker(ctx, account_key=None):
    # Only the `("reset_zero_marker", account_key)` key exists. A bare
    # `"reset_zero_marker"` fallback was carried here for "a replay whose
    # earlier records armed before the account was part of the state key", but
    # projection state lives for one cycle and nothing has ever written the
    # plain key — so the fallback was unreachable, and had it ever returned a
    # value the confirm leg's `marker[6]` would have raised `IndexError` on the
    # six-tuple it described.
    if ctx is not None and not ctx.projection_writes:
        return ctx.projection_state.get(
            ("reset_zero_marker", account_key))
    return _read_reset_zero_marker(account_key)


def _projection_clear_reset_zero_marker(ctx, account_key=None):
    if ctx is not None and not ctx.projection_writes:
        ctx.projection_state.pop(("reset_zero_marker", account_key), None)
        return
    _clear_reset_zero_marker(account_key)


def _projection_arm_reset_zero_marker(
    ctx, week_start_date, cur_end_canon, *, baseline_pct, first_zero_iso,
    first_zero_capture_iso=None, first_zero_identity=None, account_key=None,
):
    if ctx is not None and not ctx.projection_writes:
        ctx.projection_state[("reset_zero_marker", account_key)] = (
            week_start_date,
            cur_end_canon,
            float(baseline_pct),
            first_zero_iso,
            first_zero_capture_iso,
            first_zero_identity,
            account_key,
        )
        return
    _arm_reset_zero_marker(
        week_start_date,
        cur_end_canon,
        baseline_pct=baseline_pct,
        first_zero_iso=first_zero_iso,
        first_zero_capture_iso=first_zero_capture_iso,
        first_zero_identity=first_zero_identity,
        account_key=account_key,
    )


# ``CreditPlan`` / ``_parse_credit_at`` / ``_build_credit_plan`` now live in
# ``bin/_lib_credit.py`` (#279 S4 F1); re-imported at module top so the
# ``bin/cctally`` re-exports and this module's own callers
# (``cmd_record_credit``) resolve them unchanged.


def _automatic_replicas(conn, *, week_start_date, account_key, observed_at,
                        confirming_capture_at, post_credit_pct):
    """The automatic bracket, with the one guard the bracket itself cannot make.

    Both instants are evidence. When either is absent there is no bracket, and
    the honest answer is to select nothing rather than to substitute a
    timestamp: the only substitute available is the hour-floored effective
    instant, and reaching back to it is precisely the back-dating §4 removed.
    A caller with no observation instants is one whose source record carried
    none, and every production leg supplies both.
    """
    if not observed_at or not confirming_capture_at:
        return []
    return _lib_credit_selection.select_automatic_replicas(
        conn, week_start_date=week_start_date, account_key=account_key,
        observed_at=observed_at, confirming_capture_at=confirming_capture_at,
        post_credit_pct=float(post_credit_pct))


def _sweep_late_replicas(conn, *, week_start_date, account_key, observed_pct,
                         capture_iso, as_of, ctx, source_identity):
    """Section 5.4's recurring pass: run the replica selectors on EVERY tick.

    A credit fires exactly once. The immediate leg needs a drop against
    ``prior_pct`` and the debounced leg clears its marker on confirmation, so
    ``_fire_in_place_credit`` — the only caller of the bracket selector before
    this function existed — runs once per credit and the bracket is evaluated
    once, one second wide on the immediate leg. A replay that arrives minutes
    later was therefore never selected at all, which is the second half of the
    2026-09-01 incident: every 7d surface reads the pre-credit value again, and
    on the next genuine tick that value is ``prior_pct``, so the drop back to
    the credited level fires a phantom second credit and restarts the milestone
    ladder.

    Two selections, and they are bounded differently because the evidence
    differs. Inside ``[observed_at, confirming_capture_at]`` the credit's own
    two bounding observations are the evidence, so that leg runs unconditionally
    and catches a row stamped inside the bracket but written after the fire.
    After the confirmation there is no bracket, and the evidence is THIS tick's
    reading: within one accounting epoch the weekly counter never falls, so a
    stored row at or above the pre-credit level followed by a lower in-epoch
    reading cannot also be true.

    Ordering is load-bearing. This runs BEFORE ``detect_reset_and_credit`` reads
    ``prior``, so the removed replica cannot be mistaken for the pre-credit
    baseline of a credit that never happened.

    A MANUAL credit reaches only the second of the two legs, and it must reach
    it. The manual fold writes ``confirming_capture_at_utc`` NULL by design,
    because a retroactive assertion has no confirming observation, and requiring
    that column returned this function early for every manual epoch. §5.4 does
    not exclude a manual epoch, and the failure it names is exactly what
    followed: after `record-credit`, a replayed high reading was admitted by the
    write clamp, stood, became ``prior_pct`` on the next genuine tick, and the
    drop back to the credited level fired a phantom automatic credit that opened
    a second epoch and restarted the milestone ladder. The bracket leg still
    needs both instants and is still skipped without them; the contradicted-level
    leg takes the asserted credit instant as its replay floor.

    Nothing is synthesized. A credit row that records no observation instant, or
    no level to compare against, selects nothing — exactly as
    ``_automatic_replicas`` refuses to substitute the hour-floored instant.
    """
    epoch = _lib_credit_selection.resolve_weekly_credit_epoch(
        conn, week_start_date=week_start_date, account_key=account_key,
        captured_at=capture_iso)
    if epoch is None:
        return
    observed_at = epoch["observed_at_utc"]
    confirming_at = epoch["confirming_capture_at_utc"]
    post_credit_pct = epoch["observed_post_credit_pct"]
    if not observed_at:
        return
    doomed: dict = {}
    if confirming_at and post_credit_pct is not None:
        doomed = {
            int(row["id"]): row
            for row in _lib_credit_selection.select_automatic_replicas(
                conn, week_start_date=week_start_date,
                account_key=account_key, observed_at=observed_at,
                confirming_capture_at=confirming_at,
                post_credit_pct=float(post_credit_pct))
        }
    level = _lib_credit_selection.resolve_replica_level(
        conn, week_start_date=week_start_date, account_key=account_key,
        observed_at=observed_at,
        observed_pre_credit_pct=epoch["observed_pre_credit_pct"])
    # The contradiction is tested at the granularity the store ADMITS at, not
    # exactly. `hwm_clamp_applies` compares `round(x, 1)` on both sides, so a
    # reading up to about 0.05pp below the recorded maximum is stored — the
    # weekly counter therefore CAN dip inside one epoch, which is a real
    # counterexample to the invariant this rule rests on. Compared exactly, such
    # a dip crossing `replica_level` supplied a false contradiction and deleted
    # the genuine row just above it. Reusing the clamp's own predicate means a
    # value the clamp let through can never trigger a removal.
    if level is not None and hwm_clamp_applies(float(observed_pct), level):
        for row in _lib_credit_selection.select_contradicted_replicas(
                conn, week_start_date=week_start_date,
                account_key=account_key,
                replay_floor_capture_at=(confirming_at or observed_at),
                replica_level=level, contradicting_capture_at=capture_iso):
            doomed.setdefault(int(row["id"]), row)
    if not doomed:
        return
    _suppress_replica_snapshots(
        conn, [doomed[key] for key in sorted(doomed)], ctx=ctx,
        account_key=account_key, credit_key=epoch["credit_key"],
        confirming_identity=source_identity, at=as_of)


def _suppress_replica_snapshots(conn, doomed_rows, *, ctx, account_key,
                                credit_key, confirming_identity, at):
    """Remove the selected stale-replica snapshots and their dependents.

    #703 + #707 §5.4. On the INGEST path the removal is journaled as one
    immutable, effects-only `weekly_replica_suppression` event, keyed on
    ``(account_key, credit_key, confirming_observation_id, target_set_digest)``.
    Its applier is what physically deletes, and it folds at order 70 — after the
    snapshot family at 10 and the milestone family at 60 — so a rebuild
    materializes both and then replays the removal on top of them. That is the
    whole point: the previous mechanism captured its target list ONLY when the
    credit's event row was first inserted, so every later detector pass deleted
    rows the next rebuild restored.

    Dependent milestones are named in the event alongside the snapshots. They
    have to be: a milestone whose `usage_snapshot_id` points at a removed
    snapshot dangles silently, because this codebase's foreign keys are
    documentation-only.

    BOTH milestone tables are dependents, not just `percent_milestones`.
    `five_hour_milestones.usage_snapshot_id` references the same snapshot table
    through a column of the same name, so a late replica that crossed a 5h
    threshold before it was removed would otherwise lose its weekly snapshot
    while its 5h milestone survived pointing at a gone row.

    Two rows are deleted inline rather than journaled, and both are rows a
    journal cannot describe. A row with a NULL `journal_id` has no logical id to
    name in an event; a rebuild does not re-materialize it either, so deleting
    it physically is already durable. And when there is no ``ctx`` there is no
    journal to append to at all — the legacy non-ingest path, which production
    does not take.
    """
    if not doomed_rows:
        return
    identified = sorted({r[1] for r in doomed_rows if r[1]})
    unidentified = [int(r[0]) for r in doomed_rows if not r[1]]
    journaled = bool(identified) and ctx is not None and bool(credit_key)

    if journaled:
        placeholders = ",".join("?" for _ in doomed_rows)
        doomed_ids = tuple(int(r[0]) for r in doomed_rows)

        def _dependents(table):
            return sorted({
                row[0]
                for row in conn.execute(
                    f"SELECT journal_id FROM {table} "
                    f"WHERE usage_snapshot_id IN ({placeholders}) "
                    "  AND journal_id IS NOT NULL",
                    doomed_ids,
                ).fetchall()
                if row[0]
            })

        milestones = _dependents("percent_milestones")
        five_hour = _dependents("five_hour_milestones")
        import _cctally_journal as _jr
        digest = _jr.replica_suppression_target_digest(
            identified, milestones, five_hour)
        columns = {
            "account_key": account_key,
            "credit_key": credit_key,
            "confirming_observation_id": confirming_identity,
            "snapshots": identified,
            "milestones": milestones,
        }
        # Omitted when empty, exactly as the digest omits it: a target set with
        # no 5h dependent produces the payload it produced before this leg
        # existed, so nothing already written is superseded by a shape change
        # alone.
        if five_hour:
            columns["five_hour_milestones"] = five_hour
        _jr.emit_model_a(
            ctx,
            kind="weekly_replica_suppression",
            evt_id=_lib_journal.evt_id(
                "wrs", account_key, credit_key,
                confirming_identity or "-", digest),
            table=None,
            columns=columns,
            at=at,
        )
    else:
        unidentified = [int(r[0]) for r in doomed_rows]

    for rowid in unidentified:
        conn.execute(
            "DELETE FROM weekly_usage_snapshots WHERE id = ?", (rowid,))



def _fire_in_place_credit(conn, week_start_date, cur_end_canon, weekly_percent,
                          *, observed_pre_credit_pct, effective_dt,
                          as_of=None, commit=True, ctx=None,
                          account_key=_lib_accounts.UNATTRIBUTED,
                          credit_source=None,
                          observed_at_utc=None,
                          confirming_capture_at_utc=None,
                          confirming_identity=None):
    """Emit/refresh the in-place weekly-credit artifacts (issue #19 + #128).
    Shared by the immediate >=25pp path and the debounced reset-to-zero
    confirmation path.

    ``account_key`` (#341): stamps the ``week_reset_events`` row and scopes the
    stale-replica capture/DELETE to the account.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``commit=False`` folds the event-row INSERT + stale-replica DELETE into the
    caller's transaction (the ingester's cycle) instead of committing inline;
    ``as_of`` (ISO-Z) stamps ``week_reset_events.detected_at_utc`` in place of
    wall clock (the reset moment itself stays ``effective_dt``, a parameter).
    Both defaults keep the legacy inline-commit, wall-clock behavior.

    Design B event+effects seam (§5.3): when an ``IngestContext`` ``ctx`` is
    passed AND this call is the genuine-new-reset winner (the ``already is None``
    pre-check AND the ``week_reset_events`` INSERT rowcount == 1), the doomed
    stale-replica snapshots' ``journal_id``s are captured (SAME predicate as the
    pivot-2 DELETE) into ``ctx.suppression_map`` keyed on the ``wr`` harvest
    natural key ``(old_week_end_at, new_week_end_at) = (effective_iso,
    cur_end_canon)`` BEFORE the DELETE runs, so ``_build_harvest_evt`` attaches
    the list to the ``wr`` evt and the destructive effect replays. ``ctx=None``
    (legacy) captures nothing.

    Side-effect ordering is load-bearing: the event-row INSERT is dedup-gated
    on a pre-check, but the hwm force-write and stale-replica DELETE run
    UNCONDITIONALLY — a prior run may have committed the event then died before
    the pivots (memory: project_dedup_must_not_gate_side_effects). The pivots
    are individually idempotent (file overwrite + DELETE on a stable predicate).

    ``effective_dt`` is the (already-resolved) reset moment; the immediate path
    passes ``_floor_to_hour(now_utc)``, the debounced path passes the floored
    first-zero instant from the marker.

    #703 + #707. ``credit_source`` is the journal record that caused this credit
    (see ``_lib_credit_identity``); it supplies ``credit_key`` and
    ``credit_order``, which replace the boundary pair as the row's identity.
    ``observed_at_utc`` is the exact instant the post-credit state was first
    observed and ``confirming_capture_at_utc`` the capture instant of the
    observation that confirmed it — BOTH in the capture clock domain, because
    they filter ``weekly_usage_snapshots.captured_at_utc``. ``effective_dt``
    stays hour-floored and becomes display-only: one timestamp cannot serve both
    human rounding and evidence membership, which is what back-dated the epoch
    in the 2026-09-01 incident. A caller that supplies no source leaves all
    three columns NULL, which is the honest value for a row whose source
    genuinely lacked the fact; nothing is synthesized."""
    effective_iso = effective_dt.isoformat(timespec="seconds")
    # A MANUAL credit is recognised everywhere by its row SHAPE: both boundary
    # columns NULL. `_manual_credits_at`, `_manual_credits_in_week`,
    # `_EXISTING_MANUAL_CREDIT_SQL` and `_week_segment_boundaries` all classify
    # on it, and the predicate is total only while no automatic leg writes NULL
    # there. That is an assumption about every future caller of this function,
    # so state it as a refusal rather than leaving it to convention: a single
    # automatic leg passing a missing boundary would silently reclassify its
    # rows as manual across all four helpers, with nothing raising.
    if not effective_iso or not cur_end_canon:
        raise ValueError(
            "an automatic weekly credit must carry both boundary columns; "
            f"got old={effective_iso!r} new={cur_end_canon!r}. Only a manual "
            "record-credit writes the NULL-boundary shape, and every helper "
            "that tells the two apart reads exactly that shape.")
    credit_key = credit_order = None
    if credit_source is not None:
        credit_key = _lib_credit_identity.derive_credit_key(credit_source)
        credit_order = _lib_credit_identity.derive_credit_order(credit_source)
    # Pre-check keyed on the row's IDENTITY. It used to key on
    # `new_week_end_at` alone, which suppressed every later credit in the same
    # week — the gate that made several credits per week unrepresentable.
    # UNIQUE(account_key, credit_key) also dedups, but the pre-check avoids a
    # useless write attempt and keeps logs clean.
    if credit_key is not None:
        already = conn.execute(
            "SELECT 1 FROM week_reset_events "
            "WHERE account_key = ? AND credit_key = ? LIMIT 1",
            (account_key, credit_key),
        ).fetchone()
    else:
        # No source record means no `credit_key`, and SQLite treats NULLs as
        # DISTINCT under UNIQUE — so `UNIQUE(account_key, credit_key)` stops
        # deduping here and every pass would append another row. Fall back to
        # the boundary pair, which is the only identity such a row has and is
        # exactly what the pre-change gate used. This does not reintroduce the
        # singleton gate: it is reachable only when nothing named a source, and
        # the keyed branch above never consults a boundary.
        already = conn.execute(
            "SELECT 1 FROM week_reset_events "
            "WHERE account_key = ? AND credit_key IS NULL "
            "  AND old_week_end_at = ? AND new_week_end_at = ? LIMIT 1",
            (account_key, effective_iso, cur_end_canon),
        ).fetchone()
    if already is None:
        # Row shape: old=effective_iso, new=cur_end_canon (DISTINCT) so only
        # post_map fires on the credited week in _apply_reset_events_to_weekrefs
        # (old==new collapses it to a zero-width window). observed_pre_credit_pct
        # stamps the pre-credit baseline (issue #45).
        ins_wr = conn.execute(
            "INSERT OR IGNORE INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, confirming_capture_at_utc, "
            " observed_post_credit_pct, credit_key, credit_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (as_of or now_utc_iso(), effective_iso, cur_end_canon,
             effective_iso, float(observed_pre_credit_pct), account_key,
             week_start_date, observed_at_utc, confirming_capture_at_utc,
             float(weekly_percent), credit_key, credit_order),
        )
        # Design B (§5.3 event+effects): on the ingest path, capture the doomed
        # stale-replica snapshots' journal_ids BEFORE the pivot-2 DELETE (SAME
        # predicate), keyed on the wr harvest natural key (old, new) =
        # (effective_iso, cur_end_canon). Gated on the genuine-new-reset winner
        # (rowcount == 1) so a crash-replayed reset never re-suppresses; ctx=None
        # (legacy) captures nothing.
        if ctx is not None and ins_wr.rowcount == 1:
            doomed = _automatic_replicas(
                conn, week_start_date=week_start_date, account_key=account_key,
                observed_at=observed_at_utc,
                confirming_capture_at=confirming_capture_at_utc,
                post_credit_pct=weekly_percent)
            # The suppression_map key is derived from the harvest `id_parts`
            # by `_build_harvest_evt`, so it follows them onto
            # (account_key, credit_key) (#703 + #707; #341 put account_key
            # first).
            ctx.suppression_map[(account_key, credit_key)] = [
                r["journal_id"] for r in doomed if r["journal_id"]
            ]
        if commit:
            conn.commit()
    # Unconditional pivot 1: force-write hwm-7d so the next status-line render
    # reflects the post-credit value (the monotonic guard at the normal write
    # site would refuse to decrease the file).
    if ctx is None or ctx.projection_writes:
        try:
            (_cctally_core.APP_DIR / "hwm-7d").write_text(
                f"{week_start_date} {weekly_percent}\n"
            )
        except OSError:
            pass
    # Unconditional pivot 2: race-defensive cleanup of stale pre-credit replays
    # (external claude-statusline can replay pre-credit --percent values that
    # land after the credit and dominate the reset-aware clamp).
    #
    # #703 + #707 §5.1: the SELECTION is the evidence bracket in
    # `_lib_credit_selection`, not the 1.0pp tolerance band that used to be
    # written out here and again at the capture above. The band compared the
    # stored percent against a REMEMBERED pre-credit level, and on the debounced
    # leg that level is the armed marker's baseline while the rows to remove
    # hold what was written — two different quantities. In the 2026-09-01
    # incident they differed by exactly 1.0, the strict band excluded every row,
    # and the DELETE did nothing.
    #
    # #703 + #707 §5.4: the removal is JOURNALED. It used to be a raw DELETE,
    # and the capture that made it replayable happened only when the credit's
    # own event row was first inserted — so every LATER pass deleted rows that
    # the next rebuild put straight back, permanently. The
    # `weekly_replica_suppression` event records the exact logical ids removed,
    # folds after the snapshot and milestone families, and is therefore the
    # thing a rebuild replays.
    try:
        doomed_rows = _automatic_replicas(
            conn, week_start_date=week_start_date, account_key=account_key,
            observed_at=observed_at_utc,
            confirming_capture_at=confirming_capture_at_utc,
            post_credit_pct=weekly_percent)
        _suppress_replica_snapshots(
            conn, doomed_rows, ctx=ctx, account_key=account_key,
            credit_key=credit_key,
            confirming_identity=(confirming_identity
                                 or (credit_source.identity
                                     if credit_source else None)),
            at=(as_of or now_utc_iso()),
        )
        if commit:
            conn.commit()
    except sqlite3.DatabaseError as exc:
        eprint(f"[record-usage] post-credit cleanup failed: {exc}")


def detect_reset_and_credit(conn, *, week_start_date, week_end_at,
                            weekly_percent, five_hour_window_key,
                            five_hour_percent, as_of=None, commit=True,
                            ctx=None, account_key=_lib_accounts.UNATTRIBUTED,
                            source_identity=None, capture_at=None):
    """Detect + record weekly and 5h reset/credit artifacts for one usage
    observation (extracted from ``cmd_record_usage``; DB journal redesign
    §5.2.3).

    ``account_key`` (#341): the account the observation belongs to. Every
    reset/credit row written here (``week_reset_events``,
    ``five_hour_reset_events``, ``weekly_credit_floors``, synthetic snapshots)
    is stamped with it so the reset-aware clamp legs stay account-scoped.
    Default ``"unattributed"`` is the rev-4.1 defensive backstop.

    Runs the mid-week ``week_reset_events`` detection, the reset-to-zero
    debounce + >=25pp in-place-credit fire, and the parallel 5h in-place-
    credit detection, all on the caller's ``conn``.

    Seam parameters keep legacy behavior at their defaults:

    - ``as_of`` (ISO-8601 ``Z``/offset, or ``None``): the capture-time
      predicate clock. ``None`` -> ``_command_as_of()`` wall clock
      (honoring ``CCTALLY_AS_OF``), byte-identical to the pre-extraction
      inline code; the ingester injects the observation's ``at`` so
      detection is replay-deterministic. It also anchors the
      ``detected_at_utc`` audit stamps (``as_of or now_utc_iso()``) and
      threads into ``_fire_in_place_credit``.
    - ``commit`` (default ``True``): the legacy own-transaction path
      commits once at the end; the ingest path passes ``commit=False`` so
      the cycle owns the single commit (invariant ii). ``commit=False``
      also flips the two outer handlers from log-and-swallow to re-raise,
      so a detection failure ABORTS the ingest cycle instead of silently
      dropping a reset.
    - ``ctx`` (default ``None``): the ingest cycle's ``IngestContext``.
      When present, the destructive 5h credit path (and, via the threaded
      ``ctx``, ``_fire_in_place_credit``) captures its stale-replica
      DELETE's doomed ``journal_id``s into ``ctx.suppression_map`` BEFORE
      deleting (Design B event+effects, §5.3), keyed on the reset row's
      harvest natural key. Legacy (``ctx=None``) captures nothing.
    - ``source_identity`` / ``capture_at`` (#703 + #707, default ``None``):
      the triggering observation's journal identity and its payload capture
      instant. The identity becomes the credit's ``credit_key`` and its journal
      instant becomes ``credit_order``; the capture instant becomes
      ``observed_at_utc`` / ``confirming_capture_at_utc``, which must be in the
      capture clock domain because they filter
      ``weekly_usage_snapshots.captured_at_utc``. A caller that supplies
      neither leaves those columns NULL rather than synthesizing them.
    """
    c = _cctally()
    now_utc = _as_of_or_command(as_of)
    # The observation's own capture instant. `as_of` is the DETECTION clock and
    # the two are deliberately separated on the ingest path, so falling back to
    # it here is a last resort for a caller that supplies no payload capture —
    # never the preferred value (spec §4).
    capture_iso = capture_at or now_utc.isoformat(timespec="seconds")
    immediate_source = (
        _lib_credit_identity.CreditSource(
            kind="immediate",
            identity=source_identity,
            order=_lib_credit_identity.credit_order_from_instant(
                as_of or now_utc.isoformat(timespec="seconds")),
        )
        if source_identity else None
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
        # #703 + #707 §5.4: the recurring replica pass, before `prior` is read.
        # A replay left standing becomes the pre-credit baseline of a credit
        # that never happened, so removing it has to precede the read that would
        # believe it.
        _sweep_late_replicas(
            conn, week_start_date=week_start_date, account_key=account_key,
            observed_pct=weekly_percent, capture_iso=capture_iso,
            as_of=(as_of or now_utc_iso()), ctx=ctx,
            source_identity=source_identity)
        prior = conn.execute(
            "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_end_at IS NOT NULL AND account_key = ? "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (account_key,),
        ).fetchone()
        if prior and prior["week_end_at"] and cur_end_canon:
            prior_end_canon = _canonicalize_optional_iso(
                prior["week_end_at"], "record.prior"
            )
            prior_pct = prior["weekly_percent"]
            # `now_utc` is the capture-time predicate clock, bound once at the
            # top of the function from `_as_of_or_command(as_of)` — legacy
            # `as_of=None` resolves it to `_command_as_of()` (CCTALLY_AS_OF /
            # wall clock, byte-identical to the pre-extraction inline binding
            # that lived here); the ingester injects the observation's `at` so
            # mid-week-reset detection replays deterministically.
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
                    # #703 + #707: this branch records a credit too, so it
                    # carries the same facts and the same identity as the
                    # in-place legs. The triggering observation is both the
                    # first post-credit observation and its confirmation, so
                    # the bracket is one capture instant wide. `credit_key` is
                    # NULL only when the caller supplied no source, and the
                    # boundary columns stay as written because they remain this
                    # row's provenance.
                    conn.execute(
                        "INSERT OR IGNORE INTO week_reset_events "
                        "(detected_at_utc, old_week_end_at, new_week_end_at, "
                        " effective_reset_at_utc, account_key, "
                        " week_start_date, observed_at_utc, "
                        " confirming_capture_at_utc, observed_post_credit_pct, "
                        " credit_key, credit_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ((as_of or now_utc_iso()), prior_end_canon, cur_end_canon,
                         effective_iso, account_key,
                         week_start_date, capture_iso, capture_iso,
                         float(weekly_percent),
                         (_lib_credit_identity.derive_credit_key(
                             immediate_source)
                          if immediate_source else None),
                         (_lib_credit_identity.derive_credit_order(
                             immediate_source)
                          if immediate_source else None)),
                    )
                    # (inline commit removed — the function commits once at the
                    # end on the legacy path; the ingest cycle owns the commit.)
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
                    marker = _projection_read_reset_zero_marker(ctx, account_key)
                    # #703 + #707 §6.2a: the arm belongs to ONE account. There
                    # is a single marker file, so without this comparison a zero
                    # arriving for a DIFFERENT account matched the arm and
                    # confirmed a credit built from the first account's
                    # baseline — and cleared the marker, leaving the account
                    # that genuinely lost its counter unrepaired.
                    #
                    # A marker with no recorded account was written by the
                    # binary this install is upgrading from. It matches any
                    # account, which is what it did before the field existed;
                    # rejecting it would drop a genuine armed reset on the one
                    # tick that spans the upgrade.
                    marker_account = marker[6] if marker is not None else None
                    armed = (
                        marker is not None
                        and marker[0] == week_start_date
                        and marker[1] == cur_end_canon
                        and (marker_account is None
                             or marker_account == account_key)
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
                        _projection_clear_reset_zero_marker(
                            ctx, account_key)
                        _fire_in_place_credit(
                            conn, week_start_date, cur_end_canon, weekly_percent,
                            observed_pre_credit_pct=float(prior_pct),
                            effective_dt=_floor_to_hour(now_utc),
                            as_of=as_of, commit=commit, ctx=ctx,
                            account_key=account_key,
                            credit_source=immediate_source,
                            observed_at_utc=capture_iso,
                            confirming_capture_at_utc=capture_iso,
                            confirming_identity=source_identity,
                        )
                    elif decision == CONFIRM_RESET:
                        # Second reading stayed low → confirm. Anchor the
                        # reset at the FIRST-zero instant from the marker
                        # (UTC-normalized like the backfill in-place path).
                        first_zero_dt = parse_iso_datetime(
                            marker[3], "reset_zero_marker.first_zero"
                        ).astimezone(dt.timezone.utc)
                        # #703 + #707: the credit is anchored on the FIRST
                        # zero, so both its identity and its exact capture
                        # instant come from that observation, retained in the
                        # marker. `effective_dt` stays the hour floor and is
                        # display-only. A pre-#703 four-field marker retained
                        # neither fact; it falls back to the CONFIRMING
                        # observation, which is the only source that tick has,
                        # and which is off by the debounce interval rather than
                        # by the hour floor.
                        first_zero_capture = marker[4] or capture_iso
                        first_zero_identity = marker[5] or source_identity
                        debounced_source = (
                            _lib_credit_identity.CreditSource(
                                kind="debounced",
                                identity=first_zero_identity,
                                order=(
                                    _lib_credit_identity
                                    .credit_order_from_instant(marker[3])),
                            )
                            if first_zero_identity else None
                        )
                        _fire_in_place_credit(
                            conn, week_start_date, cur_end_canon,
                            weekly_percent,
                            observed_pre_credit_pct=marker[2],
                            effective_dt=_floor_to_hour(first_zero_dt),
                            as_of=as_of, commit=commit, ctx=ctx,
                            account_key=account_key,
                            credit_source=debounced_source,
                            observed_at_utc=first_zero_capture,
                            confirming_capture_at_utc=capture_iso,
                            # The CONFIRMING observation, which is this tick's
                            # own record — not the first zero the credit is
                            # anchored on.
                            confirming_identity=source_identity,
                        )
                        # Clear ONLY after the fire completes (P2a): a
                        # mid-fire crash leaves the marker armed so the next
                        # zero re-confirms + re-runs the idempotent pivots.
                        _projection_clear_reset_zero_marker(
                            ctx, account_key)
                    elif decision == CLEAR_MARKER:
                        # Recovered toward baseline → transient zero, not a
                        # reset. Clear, do not fire.
                        _projection_clear_reset_zero_marker(
                            ctx, account_key)
                    elif decision == ARM_MARKER:
                        # First ~0 → arm; do NOT fire. The write clamp
                        # suppresses this 0 (no event row yet), so the prior
                        # snapshot stays at the baseline and this shape
                        # re-evaluates next tick. first_zero_iso is the
                        # _command_as_of() value (now_utc), NOT wall-clock —
                        # it becomes the effective anchor.
                        _projection_arm_reset_zero_marker(
                            ctx,
                            week_start_date, cur_end_canon,
                            baseline_pct=float(prior_pct),
                            first_zero_iso=now_utc.isoformat(timespec="seconds"),
                            # #703 + #707: retain the first zero's EXACT
                            # capture instant and its journal identity. The
                            # confirming tick cannot see either, and the hour
                            # floor it used instead back-dated the epoch before
                            # observations that were still pre-credit.
                            first_zero_capture_iso=capture_iso,
                            first_zero_identity=source_identity,
                            # The account the arm belongs to, so a zero for a
                            # different account cannot confirm it.
                            account_key=account_key,
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
                    "   AND account_key = ? "
                    " ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
                    (account_key,),
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
                            "   AND account_key = ? "
                            " ORDER BY id DESC LIMIT 1",
                            (int(five_hour_window_key), account_key),
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
                            ins_fhc = conn.execute(
                                "INSERT OR IGNORE INTO five_hour_reset_events "
                                "(detected_at_utc, five_hour_window_key, "
                                " prior_percent, post_percent, "
                                " effective_reset_at_utc, account_key) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    (as_of or now_utc_iso()),
                                    int(five_hour_window_key),
                                    prior_5h_pct,
                                    float(five_hour_percent),
                                    effective_iso,
                                    account_key,
                                ),
                            )
                            # Design B (§5.3 event+effects): on the ingest path,
                            # capture the doomed stale-replica snapshots'
                            # journal_ids BEFORE the unconditional DELETE below,
                            # using the SAME predicate as that DELETE, and stash
                            # them in ctx.suppression_map keyed on the fhc
                            # harvest natural key (window_key, effective_iso).
                            # _build_harvest_evt attaches them to the fhc evt so
                            # the destructive effect replays deterministically.
                            # Gated on the genuine-new-reset winner
                            # (rowcount == 1) so a crash-replayed reset never
                            # re-suppresses with a divergent list; ctx=None
                            # (legacy) captures nothing.
                            if ctx is not None and ins_fhc.rowcount == 1:
                                doomed = conn.execute(
                                    "SELECT journal_id "
                                    "FROM weekly_usage_snapshots "
                                    " WHERE five_hour_window_key = ? "
                                    "   AND account_key = ? "
                                    "   AND unixepoch(captured_at_utc) "
                                    "       >= unixepoch(?) "
                                    "   AND ABS(five_hour_percent - ?) "
                                    "       < 1.0",
                                    (
                                        int(five_hour_window_key),
                                        account_key,
                                        effective_iso,
                                        prior_5h_pct,
                                    ),
                                ).fetchall()
                                # #341: fhc harvest id_parts lead with
                                # account_key -> match the suppression_map key.
                                ctx.suppression_map[
                                    (account_key, int(five_hour_window_key),
                                     effective_iso)
                                ] = [r[0] for r in doomed]
                            # (inline commit removed — one end-of-function commit
                            # on legacy; the ingest cycle owns the commit.)
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
                        if ctx is None or ctx.projection_writes:
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
                                "   AND account_key = ? "
                                "   AND unixepoch(captured_at_utc) "
                                "       >= unixepoch(?) "
                                "   AND ABS(five_hour_percent - ?) "
                                "       < 1.0",
                                (
                                    int(five_hour_window_key),
                                    account_key,
                                    effective_iso,
                                    prior_5h_pct,
                                ),
                            )
                            # (inline commit removed — end-of-function commit on
                            # legacy; the ingest cycle owns the commit.)
                        except sqlite3.DatabaseError as exc:
                            eprint(
                                "[record-usage] 5h post-credit "
                                f"cleanup failed: {exc}"
                            )
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            # Exception discipline (6c-gate P1): on the ingest path
            # (commit=False, caller owns the txn) a 5h-detection failure must
            # ABORT the cycle — re-raise so the ingester rolls back and leaves
            # the cursor unmoved (invariant ii). Legacy (commit=True) keeps the
            # log-and-swallow so a standalone record-usage tick never regresses.
            if not commit:
                raise
            eprint(
                f"[record-usage] 5h in-place-credit detection "
                f"failed: {exc}"
            )
    except (sqlite3.DatabaseError, ValueError) as exc:
        # Same discipline as the inner 5h handler: propagate on the ingest path,
        # swallow-and-log on the legacy own-transaction path.
        if not commit:
            raise
        eprint(f"[record-usage] reset-event detection failed: {exc}")
    if commit:
        conn.commit()


def _resolve_reset_aware_hwm(conn, week_start_date, week_start_at, week_end_at,
                             *, account_key):
    """The floored MAX(weekly_percent) the statusline _hwm_clamp computes:
    MAX over snapshots captured at/after the latest in-week clamp floor. The
    floor is `_reset_aware_floor`, which reads the unified `week_reset_events`
    table: a manual credit is a row there now, so it lowers the resolved HWM
    without re-anchoring the week. (`weekly_credit_floors` survives inside that
    helper for one transitional state only — a cutover-exported store before its
    first rebuild — and is not a second materialization of a credit.) Used both
    as the `--from` default and as the assertion source of truth in the
    record-credit tests.

    ``account_key`` (#341): MANDATORY account context (no silent global
    fallback). A real key scopes the HWM to one account; ``None`` is the
    explicit merged read (byte-identical to today on a single-account install)."""
    floor_iso = _reset_aware_floor(conn, week_start_date, week_start_at,
                                   week_end_at, account_key=account_key)
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_param: tuple = () if account_key is None else (account_key,)
    if floor_iso is not None:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            f" WHERE week_start_date = ?{acct_pred} "
            "   AND unixepoch(captured_at_utc) >= unixepoch(?)",
            (week_start_date, *acct_param, floor_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            f" WHERE week_start_date = ?{acct_pred}",
            (week_start_date, *acct_param),
        ).fetchone()
    return None if not row or row[0] is None else float(row[0])


def _insert_credit_snapshot(conn, plan, *, five_hour=(None, None, None),
                            commit=True, journal=None,
                            account_key=_lib_accounts.UNATTRIBUTED):
    """Insert the post-credit synthetic snapshot at plan.to_pct, tagged
    source='record-credit'. The SOLE synthetic-snapshot writer on BOTH paths
    (legacy inline + ingest), so the non-vacuity stub stays load-bearing.

    ``account_key`` (#341) stamps the synthetic row so it joins the same
    account's clamp/HWM ledger.

    ``commit=False`` (DB journal redesign §5.2.3) folds the synthetic-snapshot
    INSERT into the caller's transaction instead of committing inline; the
    default keeps the legacy inline-commit behavior. Capture time comes from
    ``plan.captured_iso`` (already the record's moment), so no ``as_of`` is
    needed here.

    Design A (rev-3 emission rule): ``journal=(ctx, evt_id)`` routes the insert
    THROUGH ``emit_model_a`` as a ``snapshot_accept`` evt — the ONLY writer of
    ``weekly_usage_snapshots`` on the ingest path — so the synthetic is journaled
    (``evt_id`` = ``sa:<id_base>:syn:0``) and replay reads it back verbatim.
    Default ``None`` is the legacy direct INSERT. Returns the target rowid on the
    journal path, or ``conn.total_changes`` on the direct path."""
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
    columns = {
        "captured_at_utc": plan.captured_iso,
        "week_start_date": plan.week_start_date,
        "week_end_date": parse_iso_datetime(plan.week_end_at, "we").date().isoformat(),
        "week_start_at": plan.week_start_at,
        "week_end_at": plan.week_end_at,
        "weekly_percent": plan.to_pct,
        "page_url": None,
        "source": "record-credit",
        "payload_json": payload,
        "five_hour_percent": fhp,
        "five_hour_resets_at": fhr,
        "five_hour_window_key": fhk,
        "account_key": account_key,
    }
    if journal is not None:
        ctx, evt_id = journal
        import _cctally_journal as _jr
        return _jr.emit_model_a(
            ctx, kind="snapshot_accept", evt_id=evt_id,
            table="weekly_usage_snapshots", columns=columns,
            at=plan.captured_iso)
    colnames = ", ".join(columns.keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO weekly_usage_snapshots ({colnames}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    if commit:
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


def _apply_credit(conn, plan, *, five_hour=(None, None, None), as_of=None,
                  commit=True, ctx=None, id_base=None, forced=False,
                  account_key=_lib_accounts.UNATTRIBUTED):
    """Apply the M2 same-window partial-credit artifacts (record-credit, #209,
    spec §4).

    #703 + #707 unified the representation: a manual credit IS a
    ``week_reset_events`` row now, written by the op fold
    ``_apply_op_weekly_credit_floor`` and never by this function. What the row
    does changed with it — it defines an accounting epoch and never a display
    boundary — so the week is still not re-anchored, and unification is also
    what lets a manual credit restart the milestone ladder:
    ``percent_milestones.reset_event_id`` resolves only through
    ``week_reset_events``, and that missing reference is why a manual credit
    opened no epoch before. This function applies the credit's EFFECTS: the
    clamp floor, the stale-replay removal and the synthetic post-credit
    snapshot.

    ``account_key`` (#341): stamps the credit record + the
    synthetic snapshot and scopes the stale-replay DELETE to the account.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``commit=False`` folds the stale-replay DELETE + synthetic snapshot into the
    caller's transaction (the ingester's cycle) instead of committing inline;
    ``as_of`` (ISO-Z) stamps the credit record's ``applied_at_utc`` in place of
    wall clock (the reset moment stays ``plan.effective_iso``). Both defaults
    keep the legacy behavior.

    Design B/event+effects seam (§5.3): on the ingest path (``ctx`` passed,
    ``id_base`` = the triggering ``record-credit`` op's journal id) the
    DESTRUCTIVE effects ride a ``weekly_credit_effects`` Model-A evt
    (``wce:<id_base>``) carrying the stale-replica suppression list (the doomed
    snapshots' ``journal_id``s, captured with the SAME predicate as the DELETE)
    + the forced ``hwm_floor``; the synthetic post-credit snapshot rides its own
    ``snapshot_accept`` evt (``sa:<id_base>:syn:0``) because
    ``weekly_usage_snapshots`` is written ONLY via ``snapshot_accept`` now
    (rev-3 emission rule). ``emit_model_a`` applies each evt through the same
    fold replay uses, so the inline hwm/DELETE/synthetic are SKIPPED on the
    ingest path (the evt appliers do them). ``ctx=None`` (legacy) keeps the
    inline path verbatim.

    ``forced`` (ingest path only) journals the ``--force`` re-record's
    destructive clear that legacy ``_force_clear_credit`` did inline. It is
    scoped to the ONE occurrence ``plan.replaces_credit_key`` names, not to the
    week: the ``wce`` evt's ``suppression`` list widens to delete that
    occurrence's own synthetic snapshots and its ``floor_suppression`` list
    deletes that occurrence's ``week_reset_events`` row, both by logical
    ``journal_id``. The occurrence's DEPENDENT milestones ride a separate
    ``weekly_replica_suppression`` evt instead, because that family folds at
    order 70 and both milestone families fold at 60 — naming them on the
    order-50 ``wce`` would delete them before the fold that creates them. The
    NEW credit row (op fold, ``journal_id = id_base``) and NEW synthetic
    (``sa:<id_base>:syn:0``) are excluded from every list, so the clear is
    order-independent and idempotent.

    Side-effect ordering mirrors `_fire_in_place_credit`'s discipline: the
    credit record's own INSERT OR IGNORE (in the op fold) is dedup-gated by
    UNIQUE(account_key, credit_key), but the hwm force-write, stale-replay
    DELETE, and synthetic-snapshot INSERT run UNCONDITIONALLY so a rerun
    finishes a crash-half-applied credit (memory:
    project_dedup_must_not_gate_side_effects). All are individually idempotent
    (file overwrite; DELETE on a stable predicate; the synthetic snapshot is
    re-INSERTed only after a ``--force`` re-record's destructive clear or on the
    completion path where none exists yet).

    `plan.effective_iso` is `floor_to_hour(at)` and is DISPLAY-ONLY. The exact
    asserted instant is `plan.captured_iso`, which the fold stores as
    `observed_at_utc` and which names the occurrence: one timestamp cannot serve
    both human rounding and evidence membership, and asking it to is what
    back-dated the epoch in the 2026-09-01 incident. parse_iso_datetime returns
    a host-local-offset aware datetime; convert to UTC so the effective instant
    persists with a +00:00 spelling, not a host offset, in the `*_utc` column.
    On the completion / --force re-apply path the CALLER passes a `plan` whose
    `effective_iso` is the EXISTING credit row's effective instant (NOT a fresh
    floor_to_hour(now)) — spec §4a completion-effective reuse."""
    c = _cctally()
    effective_dt = parse_iso_datetime(plan.effective_iso, "effective").astimezone(dt.timezone.utc)
    effective_iso = effective_dt.isoformat(timespec="seconds")
    pre_credit = float(plan.from_pct)

    # 4a. The credit RECORD is written by the built-in op fold
    # `_apply_op_weekly_credit_floor`, which runs FIRST per record from the
    # record-credit `op` line and stamps `journal_id = record["id"]`. Since
    # #703 + #707 that fold inserts the unified `week_reset_events` row rather
    # than a `weekly_credit_floors` row, and it is the SOLE writer of the credit
    # record on either path. `_apply_credit` no longer inserts anything of its
    # own here: a second INSERT would be a second materialization of one credit,
    # which is exactly what unification removes.

    if ctx is None:
        # ── Legacy inline path (verbatim) ──────────────────────────────────
        # 4b. Force-write hwm-7d so the external statusline render reflects the
        # post-credit value (the normal write-site monotonic guard would refuse
        # to decrease the file).
        try:
            (_cctally_core.APP_DIR / "hwm-7d").write_text(
                f"{plan.week_start_date} {plan.to_pct}\n"
            )
        except OSError:
            pass

        # 4c. Stale-replay DELETE: drop replays still reading at the asserted
        # pre-credit level that land at or after the ASSERTED instant (the
        # gotcha_statusline_replay_race_after_credit defense).
        #
        # #703 + #707 §5.1: the manual rule, not the automatic bracket, and not
        # the 1.0pp band either path used to carry. A retroactive assertion has
        # no confirming observation to close an upper end, so a bracket here
        # would delete genuine climb between the asserted instant and the
        # command — 46 to 31 at 10:00, real usage through 32, 33 and 35,
        # recorded at 14:00. The LEVEL is what tells a replay from a climb.
        try:
            for row in _lib_credit_selection.select_manual_replicas(
                    conn, week_start_date=plan.week_start_date,
                    account_key=account_key, observed_at=plan.captured_iso,
                    from_pct=pre_credit):
                conn.execute(
                    "DELETE FROM weekly_usage_snapshots WHERE id = ?",
                    (int(row["id"]),))
            if commit:
                conn.commit()
        except sqlite3.DatabaseError as exc:
            eprint(f"[record-credit] post-credit cleanup failed: {exc}")

        # 4d. INSERT the synthetic post-credit snapshot at plan.to_pct.
        c._insert_credit_snapshot(conn, plan, five_hour=five_hour, commit=commit,
                                  account_key=account_key)
    else:
        # ── Ingest path (Design B, §5.3 event+effects) ─────────────────────
        # Journal the destructive effects + synthetic instead of running them
        # inline. Capture the doomed stale-replica journal_ids BEFORE the effect
        # applies its DELETE (SAME predicate as the legacy 4c DELETE).
        import _cctally_journal as _jr
        # The SAME manual rule the legacy 4c DELETE uses, from the one selector
        # (#703 + #707 §5.1) — the capture and the removal cannot drift when
        # both read one function.
        #
        # Exclude THIS op's OWN synthetic ids (`sa:<id_base>:syn:%`) from the
        # capture, by deterministic id — NOT merely by emission timing (6g P2 /
        # Task 7 Item 0, the sibling of the `old_syn` forced-path fix below). A
        # credit to a level at or above `from_pct` cannot happen, but the
        # exclusion is not about the level: under crash-replay (evts fsync'd,
        # COMMIT lost) the next cycle replays `sa:<id_base>:syn:0` at fold order
        # 10 BEFORE step 4b re-runs `_apply_credit`, so a timing-only capture
        # would re-fold the just-replayed NEW synthetic into a second `wce`
        # whose suppression deletes the very row it must preserve when a rebuild
        # folds all snapshot_accept before all wce. The prefix exclusion makes
        # `supp` a PURE FUNCTION of the op — identical whether or not the new
        # synthetic has been replayed — and applies on the NON-force path too
        # (`supp = doomed` here, before `if forced:`).
        own_synthetic_prefix = f"sa:{id_base}:syn:"
        doomed = [
            r for r in _lib_credit_selection.select_manual_replicas(
                conn, week_start_date=plan.week_start_date,
                account_key=account_key, observed_at=plan.captured_iso,
                from_pct=pre_credit)
            if r["journal_id"]
            and not r["journal_id"].startswith(own_synthetic_prefix)
        ]
        supp = [r["journal_id"] for r in doomed]
        floor_supp: list = []
        milestone_supp: list = []
        five_hour_milestone_supp: list = []
        replaces_key = None
        # `--force` re-record: journal the destructive clear that legacy
        # `_force_clear_credit` did inline (delete this week's command-owned
        # synthetic snapshots + its credit-floor rows). Ride the SAME wce
        # vehicle by widening its suppression lists (spec §5.3 event+effects) —
        # captured BEFORE the new synthetic is emitted, and EXCLUDING the new
        # op's own floor (journal_id = id_base; the op fold owns it), so replay
        # is order-independent and idempotent. On the non-force path both extra
        # lists are empty and the wce is exactly the same shape as before.
        if forced:
            # Exclude THIS op's own synthetic ids (`sa:<id_base>:syn:%`) from the
            # old-synthetic capture, by deterministic id — NOT merely by emission
            # timing (6f review P1). Under crash-replay (crash between evt fsync
            # and COMMIT) the next cycle replays `sa:<id_base>:syn:0` at fold
            # order 10 BEFORE step 4b re-runs `_apply_credit(forced=True)`; a
            # timing-only exclusion would then re-capture the just-replayed NEW
            # synthetic into a second `wce` whose suppression deletes the very row
            # it must preserve. Excluding the whole `sa:<id_base>:syn:%` prefix
            # makes the `wce` suppression a PURE FUNCTION of the op — identical
            # whether or not the new synthetic has been replayed — mirroring the
            # floor list's `journal_id != id_base` filter below. (`id_base` is a
            # content digest `o:<hex>`, no LIKE metacharacters.)
            # #703 + #707: the clear is scoped to the ONE occurrence
            # `plan.replaces_credit_key` names. It used to take the whole week —
            # every prior synthetic and every prior floor row — which cannot
            # coexist with several credits per week, and which silently removed
            # credits the user never named. The replaced occurrence's row is
            # found by its identity; its synthetic snapshot is the one keyed on
            # that row's own op id (`sa:<op>:syn:%`), never the whole week's
            # `source = 'record-credit'` set; and its DEPENDENT milestones go
            # with it, because this codebase's foreign keys are
            # documentation-only and a milestone pointing at a removed credit
            # would dangle silently.
            # `getattr`, not attribute access: an op line written before this
            # field existed replays through here, and its plan has no such
            # attribute. A legacy forced op then names no occurrence and clears
            # nothing, which is the conservative direction — it leaves a row
            # standing rather than removing one nobody named.
            replaces_key = getattr(plan, "replaces_credit_key", None)
            replaced = conn.execute(
                "SELECT id, journal_id FROM week_reset_events "
                "WHERE week_start_date = ? AND account_key = ? "
                "  AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
                "  AND credit_key = ? AND journal_id IS NOT NULL "
                "  AND journal_id != ?",
                (plan.week_start_date, account_key, replaces_key, id_base),
            ).fetchone() if replaces_key else None
            if replaced is not None:
                old_syn_rows = conn.execute(
                    "SELECT id, journal_id FROM weekly_usage_snapshots "
                    "WHERE week_start_date = ? AND account_key = ? "
                    "  AND source = 'record-credit' "
                    "  AND journal_id IS NOT NULL "
                    "  AND journal_id LIKE 'sa:' || ? || ':syn:%'",
                    (plan.week_start_date, account_key, replaced["journal_id"]),
                ).fetchall()
                supp.extend(r["journal_id"] for r in old_syn_rows
                            if r["journal_id"] not in supp)
                # Dependents of the replaced occurrence, from BOTH directions a
                # dependent can point: a weekly milestone whose epoch IS the
                # replaced credit, and a milestone of either kind whose
                # `usage_snapshot_id` names one of the synthetic snapshots being
                # removed with it. Every foreign key here is
                # documentation-only, so anything left behind dangles silently.
                syn_ids = tuple(int(r["id"]) for r in old_syn_rows)
                dependents = {
                    r["journal_id"]
                    for r in conn.execute(
                        "SELECT journal_id FROM percent_milestones "
                        "WHERE reset_event_id = ? AND journal_id IS NOT NULL",
                        (int(replaced["id"]),),
                    ).fetchall()
                }
                five_hour_dependents: set = set()
                if syn_ids:
                    holes = ",".join("?" for _ in syn_ids)
                    dependents |= {
                        r["journal_id"]
                        for r in conn.execute(
                            "SELECT journal_id FROM percent_milestones "
                            f"WHERE usage_snapshot_id IN ({holes}) "
                            "  AND journal_id IS NOT NULL", syn_ids).fetchall()
                    }
                    five_hour_dependents = {
                        r["journal_id"]
                        for r in conn.execute(
                            "SELECT journal_id FROM five_hour_milestones "
                            f"WHERE usage_snapshot_id IN ({holes}) "
                            "  AND journal_id IS NOT NULL", syn_ids).fetchall()
                    }
                milestone_supp = sorted(dependents)
                five_hour_milestone_supp = sorted(five_hour_dependents)
                floor_supp = [replaced["journal_id"]]
        # wce evt (effects-only, table=None): snapshot suppression list + floor
        # suppression list (--force clear) + forced hwm floor. emit_model_a
        # appends+fsyncs the line then applies it via `_apply_weekly_credit_effects`
        # (DELETE by journal_id from both tables + hwm-7d write).
        #
        # The dependent-milestone removal is NOT here. This family folds at
        # order 50 and both milestone families fold at 60, so a rebuild or a
        # rederive would run that DELETE against a table it had not filled yet —
        # the same live-only/rebuild-undone class §5.4 exists to remove. It
        # appeared to work only because the milestone's own `reset_event_ref`
        # named the record this event deletes at 50, so the reference failed to
        # resolve and the row was dropped against a NOT NULL column. That is a
        # coincidence of two unrelated mechanisms and it inverts the moment
        # either one changes. The removal rides the order-70
        # `weekly_replica_suppression` family below instead.
        _jr.emit_model_a(
            ctx,
            kind="weekly_credit_effects",
            evt_id=f"wce:{id_base}",
            table=None,
            columns={
                "suppression": supp,
                "suppression_table": "weekly_usage_snapshots",
                "floor_suppression": floor_supp,
                "hwm_floor": {
                    "week_start_date": plan.week_start_date,
                    "weekly_percent": plan.to_pct,
                },
            },
            at=(as_of or plan.captured_iso),
        )
        if milestone_supp or five_hour_milestone_supp:
            # The replaced occurrence's dependents, on the one family that folds
            # after the families it removes rows from. Keyed on the replaced
            # occurrence and on the op doing the replacing, so a crash-replayed
            # cycle converges on the same line rather than appending a second.
            digest = _jr.replica_suppression_target_digest(
                (), milestone_supp, five_hour_milestone_supp)
            columns = {
                "account_key": account_key,
                "credit_key": replaces_key,
                "confirming_observation_id": id_base,
                "snapshots": [],
                "milestones": milestone_supp,
            }
            if five_hour_milestone_supp:
                columns["five_hour_milestones"] = five_hour_milestone_supp
            _jr.emit_model_a(
                ctx,
                kind="weekly_replica_suppression",
                evt_id=_lib_journal.evt_id(
                    "wrs", account_key, replaces_key, id_base, digest),
                table=None,
                columns=columns,
                at=(as_of or plan.captured_iso),
            )
        # Synthetic post-credit snapshot as a snapshot_accept evt (the only
        # writer of weekly_usage_snapshots on the ingest path). Route it through
        # `_insert_credit_snapshot(journal=...)` — the SOLE synthetic writer on
        # both paths — so the non-vacuity stub of `_insert_credit_snapshot` stays
        # load-bearing (stubbing it disables the synthetic on the ingest path too).
        c._insert_credit_snapshot(
            conn, plan, five_hour=five_hour, commit=False,
            journal=(ctx, f"sa:{id_base}:syn:0"), account_key=account_key)

    # 4e. Clear a stale same-week reset-zero marker so the next record-usage
    # tick can't confirm a phantom reset-to-zero off it. Scoped to the credited
    # account (#703 + #707 §6.2a): a manual credit for one account must not
    # disarm another account's pending reset.
    _projection_clear_reset_zero_marker(ctx, account_key)


def _count_stale_replays(conn, plan):
    """Count the rows the credit's stale-replay removal will touch.

    For the preview and the `--json` `staleReplaysDeleted` field, so it MUST
    read the same selector the removal does (#703 + #707 §5.1): a preview
    computed from its own predicate is a preview of a different command.

    Account-blind (`account_key=None`), matching the preview's `hwm_before`
    read: the preview is shown before the account is resolved, and the merged
    count is byte-identical on a single-account install.
    """
    return len(_lib_credit_selection.select_manual_replicas(
        conn, week_start_date=plan.week_start_date, account_key=None,
        observed_at=plan.captured_iso, from_pct=plan.from_pct))


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
        f"    + credit record         (effective={plan.effective_iso}, "
        f"pre_credit={plan.from_pct:g})",
        f"    ~ hwm-7d                {plan.from_pct:g} -> {plan.to_pct:g}",
        f"    - stale replays         {stale_replays} rows",
        f"    + snapshot              captured={plan.captured_iso}, "
        f"weekly_percent={plan.to_pct:g}",
        # The old wording named the mechanism — "no week_reset_events row" —
        # and #703 + #707 made it false: a manual credit IS such a row now,
        # with both boundary columns NULL because it moved no boundary. The
        # replacement states the effect, which is what the note was for.
        "  note: same week — the window keeps its own boundaries",
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


def _credit_account_clause(account_key):
    """Scope a manual-credit query to one account, or explicitly to all.

    ``None`` is a deliberate merged read for a caller with no account context,
    never a silent global fallback (#703 + #707 §6.2a)."""
    if account_key is None:
        return "", ()
    return " AND account_key = ?", (account_key,)


_MANUAL_CREDIT_PROJECTION = (
    "SELECT id, credit_key, journal_id, "
    "       effective_reset_at_utc AS effective_at_utc, "
    "       observed_at_utc, observed_pre_credit_pct "
    "  FROM week_reset_events "
)


def _manual_credits_at(conn, week_start_date, at_iso, effective_iso, *,
                       account_key):
    """Every manual credit for this ACCOUNT'S week at the instant ``--at`` names.

    ``account_key`` is MANDATORY (#703 + #707 §6.2a). Unscoped, account B naming
    its own instant was refused by a message describing account A's credit, and
    the ambiguity refusal counted another account's rows.

    `--at` names ONE occurrence, and the instant that names it is the UNFLOORED
    one. `effective_reset_at_utc` stays hour-floored and display-only, so
    matching on it made every credit inside one hour the same occurrence: a
    second `record-credit` at 14:47 was read as a repeat of the one at 14:05 and
    refused, even though spec §3.1 admits two credits in the same hour whenever
    two distinct ops produced them. Only a genuinely identical instant is
    indistinguishable from a double-run, and that case keeps its refusal.

    Two arms, tried in order rather than unioned. A row written since
    `observed_at_utc` existed is named by that exact instant. A row that
    predates the column carries only the hour-floored effective instant, so an
    exact-only predicate would leave it unreachable by `--force`; the hour arm
    reaches it, and runs ONLY when no row matched exactly, so an exact match
    never becomes an ambiguity refusal merely because a legacy row shares its
    hour.

    `unixepoch()` on both sides because the producers spell the offset
    differently (`Z` against `+00:00`), which a textual comparison would
    silently disagree about on a non-UTC host.
    """
    acct_sql, acct_params = _credit_account_clause(account_key)
    exact = conn.execute(
        _MANUAL_CREDIT_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
        "   AND observed_at_utc IS NOT NULL "
        "   AND unixepoch(observed_at_utc) = unixepoch(?) "
        " ORDER BY id",
        (week_start_date,) + acct_params + (at_iso,),
    ).fetchall()
    if exact:
        return exact
    return conn.execute(
        _MANUAL_CREDIT_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
        "   AND observed_at_utc IS NULL "
        "   AND unixepoch(effective_reset_at_utc) = unixepoch(?) "
        " ORDER BY id",
        (week_start_date,) + acct_params + (effective_iso,),
    ).fetchall()


def _manual_credits_in_week(conn, week_start_date, *, account_key):
    """Every manual credit for this ACCOUNT'S week, oldest first.

    ``account_key`` is MANDATORY (#703 + #707 §6.2a). This is the destructive
    one: the half-applied scan built on it made a plain run COMPLETE another
    account's crashed credit, reusing that account's `credit_key` so the row it
    wrote carried the other account's identity."""
    acct_sql, acct_params = _credit_account_clause(account_key)
    return conn.execute(
        _MANUAL_CREDIT_PROJECTION +
        " WHERE week_start_date = ?" + acct_sql +
        "   AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
        " ORDER BY unixepoch("
        "     COALESCE(observed_at_utc, effective_reset_at_utc)), id",
        (week_start_date,) + acct_params,
    ).fetchall()


def _credit_has_its_synthetic(conn, week_start_date, occurrence, *,
                             account_key) -> bool:
    """Whether ``occurrence`` already has the synthetic post-credit snapshot
    `_apply_credit` writes for it.

    Matched on the snapshot's OWN logical id, `sa:<the credit's op id>:syn:%`,
    not on "any command-owned snapshot at or after this credit's effective
    instant". With several credits in one week the looser predicate answers for
    the wrong credit: a later occurrence's synthetic sits after an earlier one's
    effective instant and would make a genuinely half-applied earlier credit
    look finished. The looser predicate remains the fallback for a row with no
    journal id, which is a hand-built or pre-fold state with no own-synthetic to
    match.
    """
    acct_sql, acct_params = _credit_account_clause(account_key)
    if occurrence["journal_id"]:
        row = conn.execute(
            "SELECT 1 FROM weekly_usage_snapshots "
            " WHERE week_start_date = ?" + acct_sql +
            "   AND source = 'record-credit' "
            "   AND journal_id LIKE 'sa:' || ? || ':syn:%' LIMIT 1",
            (week_start_date,) + acct_params + (occurrence["journal_id"],),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT 1 FROM weekly_usage_snapshots "
        " WHERE week_start_date = ?" + acct_sql +
        "   AND source='record-credit' "
        "   AND unixepoch(captured_at_utc) >= unixepoch(?) LIMIT 1",
        (week_start_date,) + acct_params + (occurrence["effective_at_utc"],),
    ).fetchone()
    return row is not None


def _resolve_credit_occurrence(conn, *, week_start_date, at_dt, is_force,
                               account_key):
    """Classify the credit occurrence this invocation acts on.

    Returns ``(occurrence, is_completion, refusal)``; ``refusal`` is a message
    or None. ONE function, shared by `cmd_record_credit` and
    `_revalidate_credit_plan`, because the two must reach the same verdict — the
    revalidation exists to refuse a plan that drifted, and two copies of this
    rule would eventually make it refuse a plan that did not.

    #703 + #707 replaced "is there a credit for this WEEK" with two narrower
    questions, because the first could only be answered one way once several
    credits per week became representable, and it is the question that made
    `--force` replace the week.

      - ``--force`` acts on the one occurrence at the instant ``--at`` names.
        That instant is the UNFLOORED one: `effective_reset_at_utc` is
        hour-floored and display-only, so naming an occurrence by it made every
        credit inside one hour the same occurrence. None there is a refusal,
        because replacing the week instead is what this change exists to stop.
        Several is an ambiguity refusal: a keyless row predates credit identity
        and cannot be told apart from another keyless row beside it, and picking
        one silently would remove a credit nobody named.
      - A plain run first looks for a HALF-APPLIED credit anywhere in the week —
        a crash between the credit record and its synthetic snapshot — and
        COMPLETES it, reusing its effective instant and its identity. This
        lookup is deliberately not keyed on ``--at``: the completion rerun
        happens at a later wall clock than the credit it finishes, which is the
        whole reason section 4a reuses the recorded effective instead of
        flooring a fresh one.
      - Otherwise a plain run with a fully-applied occurrence at this instant is
        refused, as before: a second credit at the identical instant is
        indistinguishable from a double-run.
      - Otherwise it ADDS an occurrence and leaves every other credit untouched.
    """
    effective_iso = _floor_to_hour(at_dt).isoformat(timespec="seconds")
    at_iso = at_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    at_instant = _manual_credits_at(
        conn, week_start_date, at_iso, effective_iso,
        account_key=account_key)
    if is_force:
        if not at_instant:
            return None, False, (
                f"no credit recorded at {at_iso}; --force replaces one "
                "occurrence, named by --at")
        if len(at_instant) > 1:
            return None, False, (
                f"several credits are recorded at {at_iso}; --force "
                "cannot tell which one to replace")
        target = at_instant[0]
        if not target["credit_key"]:
            return None, False, (
                f"the credit at {at_iso} predates credit identity and "
                "cannot be named for replacement")
        return target, False, None

    half_applied = [
        row for row in _manual_credits_in_week(
            conn, week_start_date, account_key=account_key)
        if not _credit_has_its_synthetic(
            conn, week_start_date, row, account_key=account_key)
    ]
    if len(half_applied) > 1:
        return None, False, (
            "several half-applied credits are recorded for this week; pass "
            "--force with --at to replace one")
    if half_applied:
        return half_applied[0], True, None
    if len(at_instant) > 1:
        return None, False, (
            f"several credits are recorded at {at_iso}; pass --at to "
            "name one")
    if at_instant:
        return at_instant[0], False, None
    return None, False, None


# #703 + #707: `record-credit`'s existing-credit lookup. A manual credit is a
# `week_reset_events` row now, so the classification that decides between
# completion, refusal and `--force` reads that table. The projection is kept as
# `(id, effective_at_utc, observed_pre_credit_pct)` because both call sites and
# every downstream branch index it positionally, and the middle element is the
# effective instant either way.
#
# A MANUAL credit is recognised by its row SHAPE: both boundary columns NULL.
# Only the `weekly_credit_floor` op fold writes that shape, because a manual
# credit moves no boundary and has none to record; every automatic path writes
# both columns. `journal_id` is NOT the discriminator — an automatic credit's row
# is stamped by harvest too, so that column separates "already journaled" from
# "inserted this cycle", not manual from automatic. Scoping to manual rows keeps
# this classification about what `record-credit` itself wrote, which is what the
# completion and refusal branches are about.
#
# The ordering is §5.3's, not `unixepoch(effective_reset_at_utc) DESC, id DESC`.
# Two credits inside one hour are legal now and share the floored instant, so
# that form's real tiebreak was `id DESC` — a projection-local number a rebuild
# reassigns, and §5.3 states plainly that it is unusable rather than merely
# imprecise. The accounting instant orders by occurrence; `credit_order` records
# a fact of the source record; `credit_key` is the deterministic final fallback.
_EXISTING_MANUAL_CREDIT_SQL = (
    "SELECT id, effective_reset_at_utc AS effective_at_utc, "
    "       observed_pre_credit_pct, credit_key "
    "  FROM week_reset_events "
    " WHERE week_start_date = ?{acct} "
    "   AND old_week_end_at IS NULL AND new_week_end_at IS NULL "
    " ORDER BY unixepoch("
    "     COALESCE(observed_at_utc, effective_reset_at_utc)) DESC, "
    "          credit_order DESC, credit_key DESC LIMIT 1"
)


def _latest_manual_credit(conn, week_start_date, *, account_key):
    """The manual credit whose accounting instant is latest for this account.

    ``account_key`` is MANDATORY (#703 + #707 §6.2a); ``None`` is the explicit
    merged read, which only a caller that genuinely has no account context uses.
    """
    acct_sql, acct_params = _credit_account_clause(account_key)
    return conn.execute(
        _EXISTING_MANUAL_CREDIT_SQL.format(acct=acct_sql),
        (week_start_date,) + acct_params).fetchone()


def _credit_command_account_scope():
    """The account `record-credit` classifies its occurrences under.

    `record-credit` is active-account-only (#341 P2-1), and the op it appends is
    stamped with the active identity. The classification that decides between
    completion, refusal and `--force` has to read the SAME account, or account B
    resolves an occurrence belonging to account A (#703 + #707 §6.2a).

    A TORN read yields ``None`` — the explicit merged read — rather than an
    exit. The command already refuses a torn read before it writes anything, and
    failing here instead would move that refusal onto the preview, which writes
    nothing and is expected to work. So the transient degrades to exactly the
    unscoped behavior that preceded this function, for the one tick it lasts.
    """
    identity = _cctally_core._resolve_active_claude_identity()
    if identity.get("status") == "torn":
        return None
    return identity["account_key"]


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
        account_scope = _credit_command_account_scope()
        latest = _latest_manual_credit(
            conn, week_start_date, account_key=account_scope)
        is_force = bool(getattr(args, "force", False))
        if getattr(args, "from_pct", None) is not None:
            from_pct, from_source = float(args.from_pct), "explicit"
        elif latest is not None and latest[2] is not None:
            from_pct, from_source = float(latest[2]), "prior_credit"
        else:
            from_pct = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at, account_key=None)
            if from_pct is None:
                return None
            from_source = "hwm"
        existing, is_completion, refusal = _resolve_credit_occurrence(
            conn, week_start_date=week_start_date, at_dt=at_dt,
            is_force=is_force, account_key=account_scope)
        if refusal is not None:
            return None
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
            effective_override=(
                existing["effective_at_utc"] if is_completion else None),
            replaces_credit_key=(
                existing["credit_key"] if (is_force and existing is not None)
                else None),
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
        # the unified `week_reset_events` row the op fold writes, restricted to
        # the manual rows — #703 + #707 retired `weekly_credit_floors` as a
        # second materialization of one credit). Latest credit wins (a --force
        # re-apply at a new effective leaves the old row only until the
        # ingest-path wce evt's suppression deletes it; pick the newest
        # defensively).
        account_scope = _credit_command_account_scope()
        latest = _latest_manual_credit(
            conn, week_start_date, account_key=account_scope)

        # 2. Resolve --from default.
        if getattr(args, "from_pct", None) is not None:
            from_pct, from_source = float(args.from_pct), "explicit"
        elif latest is not None and latest[2] is not None:
            # A credit floor already exists for this week (completion or
            # --force re-record). Its recorded observed_pre_credit_pct is the
            # AUTHENTIC pre-credit baseline; prefer it over the reset-aware
            # HWM. The post-credit segment's MAX(weekly_percent) would
            # otherwise pick up the post-credit value (31) or a later real
            # status-line reading, mis-deriving the baseline and causing the
            # stale-replay DELETE to (wrongly) match real history. fromSource
            # is 'prior_credit' (spec §5).
            from_pct, from_source = float(latest[2]), "prior_credit"
        else:
            hwm = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at, account_key=None)
            if hwm is None:
                eprint("record-credit: no usage history for the week; pass --from")
                return 2
            from_pct, from_source = hwm, "hwm"

        is_force = getattr(args, "force", False)

        # 2a. Classify the existing-floor state (M2, spec §4/§5). A
        #     credit record may be:
        #       - half-applied (floor row present, NO command-owned snapshot
        #         at/after its effective): a crash between 4a and 4d. A plain
        #         rerun FINISHES it, reusing the existing effective_at_utc (NOT
        #         a fresh floor_to_hour(now)) so no stale [old,new) replay leaks
        #         into the floored MAX (spec §4a completion-effective reuse).
        #       - fully applied (floor row + command-owned snapshot): refuse by
        #         default; --force clears + re-records at a fresh effective.
        existing, is_completion, refusal = _resolve_credit_occurrence(
            conn, week_start_date=week_start_date, at_dt=at_dt,
            is_force=is_force, account_key=account_scope)
        if refusal is not None:
            eprint(f"record-credit: {refusal}")
            return 2

        # The effective the plan should carry: a half-applied completion reuses
        # the EXISTING record's effective; a first credit / --force replacement
        # uses floor_to_hour(at) (computed inside _build_credit_plan).
        reuse_effective = existing["effective_at_utc"] if is_completion else None

        # 3. Validate + build plan.
        try:
            plan = _build_credit_plan(
                week_start_date=week_start_date, week_start_at=ws_at,
                week_end_at=we_at, from_pct=from_pct, from_source=from_source,
                to_pct=args.to, at_dt=at_dt, now=now,
                effective_override=reuse_effective,
                replaces_credit_key=(
                    existing["credit_key"]
                    if (is_force and existing is not None) else None),
            )
        except ValueError as e:
            eprint(f"record-credit: {e}")
            return 2

        # 4. Output + confirm matrix (spec §5).
        is_json = getattr(args, "json", False)
        is_dry = getattr(args, "dry_run", False)
        is_yes = getattr(args, "yes", False)
        stale_replays = _count_stale_replays(conn, plan)
        hwm_before = _resolve_reset_aware_hwm(conn, week_start_date, ws_at, we_at, account_key=None)
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
            # Name the EXACT instant, not the hour-floored one. The refusal's
            # own "--at another instant" escape is only usable if the instant it
            # reports is the one an occurrence is named by, and a second credit
            # inside the same hour is now a legal second occurrence.
            eprint(f"record-credit: a credit is already recorded at "
                   f"{existing['observed_at_utc'] or existing['effective_at_utc']} "
                   f"(pre_credit={existing['observed_pre_credit_pct']}); "
                   f"pass --force to replace it, or --at another instant to "
                   f"record a second credit in this week")
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
                conn, plan.week_start_date, plan.week_start_at, plan.week_end_at,
                account_key=None
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

            # 6f writer reroute (Appendix A): the credit no longer writes stats.db
            # directly. Build the `record-credit` `op` line (floor columns + the
            # full 9-field plan + the command-time prior-5h read + the `forced`
            # flag) and run an AUTHORITATIVE ingest so the command observes its
            # own write synchronously. The op fold writes the unified
            # `week_reset_events` row (journal_id = the op line id);
            # `_pipeline_record_credit`
            # -> `_apply_credit(ctx=..., forced=...)` journals the `weekly_credit_effects`
            # evt (stale-replica suppression + the `--force` clear of OLD synthetics
            # + OLD floors) and the synthetic `snapshot_accept` evt. Preview/refusal
            # paths above appended NOTHING; the weekly tombstone is already
            # fail-closed immediately before this single append+ingest.
            forced = bool(existing is not None and is_force)
            # `five_hour` is the COMMAND-time prior-5h read (before the credit),
            # carried in the op so the ingest hook derives the synthetic against
            # the same 5h state the command saw.
            five_hour = _resolve_prior_5h(conn, at_dt)
            capture_iso = now_utc_iso(now)
            effective_utc = parse_iso_datetime(
                plan.effective_iso, "op.effective"
            ).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
            # Active-account gate (#341 P2-1, spec §3): record-credit is
            # active-account-only. Resolve the active Claude account and stamp
            # the credit op so the floor lands under the SAME account post-Step-9
            # usage carries — else the account-scoped `_reset_aware_floor` clamp
            # would never see a real credit floor. A TORN read (transient
            # mid-write ~/.claude.json) means the active identity is genuinely
            # unavailable -> exit 2 (retry). A stably-absent read (no
            # ~/.claude.json / api-key mode) is a RESOLVED `unattributed` outcome
            # (single-account / legacy install), NOT unavailable -> proceed
            # byte-identically to pre-#341.
            identity = _cctally_core._resolve_active_claude_identity()
            if identity.get("status") == "torn":
                eprint("record-credit: active Claude account is unavailable "
                       "(torn read of ~/.claude.json); retry once it settles")
                return 2
            credit_account_key = identity["account_key"]
            import _cctally_journal as _jr
            import _lib_journal as _lj
            op = _lj.make_op(
                at=capture_iso,
                src="record-credit",
                payload={
                    "kind": "weekly_credit_floor",
                    "week_start_date": plan.week_start_date,
                    "effective_at_utc": effective_utc,
                    "observed_pre_credit_pct": float(plan.from_pct),
                    "applied_at_utc": capture_iso,
                    "plan": dict(vars(plan)),
                    "five_hour": list(five_hour),
                    "forced": forced,
                    # #703 + #707: a COMPLETION finishes the credit a crash left
                    # half-applied, so it must take that credit's identity
                    # rather than mint a new one from its own content digest.
                    # Absent on every other path, where a distinct op is a
                    # distinct credit.
                    **({"completes_credit_key": existing["credit_key"]}
                       if (is_completion and existing is not None
                           and existing["credit_key"]) else {}),
                    # Two-shaped stamp (#341): evt/op carry account_key in the
                    # payload; the op fold + `_apply_credit` both read it.
                    "account_key": credit_account_key,
                },
            )
            # Close the command's read connection BEFORE the ingester (the sole
            # stats.db writer) opens its own — the reroute keeps a single live
            # stats.db writer. The authoritative cycle runs inline through this
            # appended op and commits; the stable-projection read below then
            # observes it.
            conn.close()
            conn = None
            _jr.append_record(op)
            _jr.run_stats_ingest(mode="authoritative")

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


def cmd_record_usage(
    args: argparse.Namespace, *,
    ingest_mode: str = "authoritative",
    writer: str = "record-usage",
    nudge_dashboard: bool = True,
    nudge_sink=None,
) -> int:
    """Record usage from the Claude Code status line rate_limits — DB journal
    redesign reroute (Appendix A).

    Validates the ingress (weekly plausibility -> exit 2; out-of-band 5h dropped
    non-fatally), builds the RAW obs line (raw capture only, NO derived week
    columns), appends it to the journal, and runs the single-flight ingest cycle
    that derives + journals every stats fact via ``_pipeline_claude_usage``
    (snapshot_accept / milestones / 5h block / reset-credit / cost snapshot /
    dollar axes). stats.db is written ONLY through ``run_stats_ingest`` now.

    ``ingest_mode`` -- "authoritative" (default: CLI + statusline publication)
    observes its own write synchronously; "opportunistic" (hook-tick OAuth /
    dedup ticks) skips a busy ingest lock and lets the winner consume the line.
    ``writer`` is the obs line ``src``. Returns 0 on a recorded/deduped tick, 2
    on an implausible weekly resets_at.

    ``nudge_dashboard`` (#583 S2 §5.3) enqueues a rebuild on a locally running
    dashboard when — and only when — the ingest emitted events. Pass False
    from `_refresh_usage_inproc`, which runs inside the dashboard's own
    ``sync_lock`` while servicing a ``refresh=1`` and whose caller
    ``cmd_refresh_usage`` already nudges once itself.

    ``nudge_sink`` is for callers that hold a cross-process lock across this
    call. When supplied it is invoked INSTEAD of the nudge, and that caller
    fires the real nudge once its critical section has ended. The nudge is a
    loopback POST with a multi-second timeout, so a stalled nudge inside such a
    section stalls every other process contending on the same lock. There are
    two such locks and both supply a sink: ``_selected_state_lock`` (the OAuth
    refresh and the authoritative statusline publication) and the statusline
    persist flock (``_statusline_reduce_and_publish``, reached from the forked
    persist child and from the ``sync_for_test`` foreground path)."""

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

    # Build the RAW observation (spec 4.2 / 5.3 -- raw capture only, NO derived
    # week columns; `_pipeline_claude_usage` canonicalizes the week boundaries +
    # the 5h window key at ingest). Append it, then run the single-flight cycle.
    # The capture clock intentionally stays separate from CCTALLY_AS_OF in
    # production. The harness-only pin lets deterministic CLI scenarios model a
    # genuinely open historical block without pretending a years-old reset is
    # current wall time.
    capture_now = now_dt if os.environ.get("CCTALLY_TEST_PIN_CAPTURE") else None
    raw: dict[str, Any] = {
        "weekly_percent": weekly_percent,
        "resets_at": resets_at,
        "source": getattr(args, "source", "statusline"),
        # The CAPTURE wall clock (now_utc_iso(), NOT _command_as_of()) — the
        # snapshot `captured_at_utc` + the milestone/5h-block/dollar-axis stamps
        # + the block cost-sum range end. Legacy used wall clock for these while
        # DETECTION (reset/credit) used `_command_as_of()`; the obs line `at`
        # carries `_command_as_of()` (below) so the hook preserves that split.
        # Captured ONCE here (append time) and journaled, so a delayed ingest is
        # deterministic (never the ingest wall clock).
        "captured_at": now_utc_iso(capture_now),
    }
    if five_hour_percent is not None:
        raw["five_hour_percent"] = five_hour_percent
    if five_hour_resets_at_str is not None:
        raw["five_hour_resets_at"] = five_hour_resets_at_str

    import _cctally_journal as _jr
    import _lib_journal as _lj
    import _lib_accounts
    # Observe-and-stamp (#341, spec §1): resolve the active Claude account from
    # ~/.claude.json (stable-read, mtime-cached) and stamp the usage obs with it.
    # Byte-safe: a real account is stamped explicitly; the reserved sentinel
    # (no ~/.claude.json / api-key / torn) OMITS the field, so a single-account /
    # legacy install's journal obs stays byte-identical to today (the pipeline's
    # `rec.get("account") or UNATTRIBUTED` fallback treats both the same).
    obs_at = now_utc_iso(now_dt)
    identity = _cctally_core._resolve_active_claude_identity()
    account_key = identity["account_key"]
    obs_account = None if account_key == _lib_accounts.UNATTRIBUTED else account_key
    if obs_account is not None:
        # Journal the account_observe DURABLY BEFORE the account-stamped usage obs
        # (spec §1: replay can never see a stamped row whose account was never
        # observed). First-sight/identity-change only — marker-deduped so it is
        # not appended every tick.
        _maybe_append_account_observe(identity, at=obs_at)
    _jr.append_record(_lj.make_obs(
        at=obs_at, src=writer, provider="claude", payload=raw,
        account=obs_account))
    # authoritative observes its own write synchronously; opportunistic skips a
    # busy ingest lock and lets the current holder consume the appended line.
    result = _jr.run_stats_ingest(mode=ingest_mode)
    # #583 S2 §5.3. Nudge only on a MATERIAL change. This runs at Claude
    # Code's status-line cadence, so an unconditional nudge would queue a
    # rebuild for work that changed nothing displayed. `consumed` is the wrong
    # signal — unchanged observations advance ingestion without changing
    # anything the dashboard shows — and `alerts` is the wrong signal, because
    # it covers only a subset of material events: a new 5-hour window changes
    # the dashboard without necessarily firing an alert. The nudge happens
    # after `run_stats_ingest` returns, which is already after post-commit
    # alert dispatch, so the alerts ordering is untouched.
    if (nudge_dashboard
            and getattr(result, "ran", False)
            and getattr(result, "error", None) is None
            and int(getattr(result, "events_emitted", 0)) > 0):
        # A caller holding the selected-state lock supplies a sink and fires
        # the real nudge after releasing it — a network call has no business
        # inside that critical section.
        (nudge_sink or _cctally()._nudge_dashboard_repaint)()
    return 0


_OBSERVED_ACCOUNT_MARKER = "observed-claude-account"


def _maybe_append_account_observe(identity: dict, *, at: str) -> None:
    """Append an ``account_observe`` op on first sight of a Claude account or an
    identity change (#341, spec §1) — NOT every tick. Deduped by a marker file in
    APP_DIR so a hot status-line loop journals at most one observe per account.
    Best-effort: a marker/journal hiccup never breaks record-usage."""
    import _lib_accounts
    account_key = identity.get("account_key")
    if not account_key or account_key == _lib_accounts.UNATTRIBUTED:
        return
    marker = _cctally_core.APP_DIR / _OBSERVED_ACCOUNT_MARKER
    try:
        last = marker.read_text().strip()
    except OSError:
        last = None
    if last == account_key:
        return
    try:
        import _cctally_journal as _jr
        import _lib_journal as _lj
        _jr.append_record(_lj.make_account_observe(
            at=at, account_key=account_key, provider="claude",
            natural_id=identity.get("natural_id"), email=identity.get("email"),
            plan_type=identity.get("plan_type"), label_source="auto"))
        marker.write_text(account_key + "\n")
    except OSError:
        pass


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


#: A token that is unmistakably a filesystem path: an absolute POSIX path, a
#: `~`-relative one, or a Windows drive path. The negative lookbehind is what
#: keeps `Input/output error` and `disk I/O error` intact — a separator with a
#: word character in front of it is prose, not a root.
#:
#: It deliberately does NOT match a RELATIVE path (`.codex/sessions/…`): every
#: separator in one is preceded by a word character, so widening the lookbehind
#: to reach it is the same edit that starts eating prose. That is acceptable
#: because of what the two rules are each for — a relative path carries no
#: username and no home directory, so the only identifier it can leak is the
#: conversation id, which the UUID rule below redacts wherever it appears.
_HOOK_LOG_PATHISH = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/]|~?/)[^\s'\"]*")

#: A conversation identifier, in or out of path form. Codex names its rollouts
#: `rollout-<timestamp>-<uuid>.jsonl`, and the `OSError` narrowing only drops
#: the one that arrives as `filename` — any OTHER exception type quoting a
#: rollout relatively, or a bare conversation key in one of our own
#: `ValueError(f"… {key}")` messages, escapes the path rule entirely. A
#: canonical UUID cannot occur in prose, so this one needs no lookbehind.
_HOOK_LOG_UUIDISH = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])")

#: `hook-tick.log` free text is read back by a LAST-WINS `k=v` comprehension,
#: so the value may not contain a separator of its own.
_HOOK_LOG_FIELD_SEPARATOR_SUBSTITUTE = ":"


def _hook_log_safe_free_text(value: str, *, limit: int = 200) -> str:
    """Collapse one free-text hook-tick log value and defuse it.

    Four transforms, in order: whitespace collapses so a multi-line message
    cannot split the record; path-shaped tokens are redacted; UUID-shaped ones
    are redacted separately, because a relative or bare conversation id never
    reaches the path rule; and ``=`` is substituted so the value cannot
    impersonate a field. See ``_hook_log_error_detail`` for why each is
    load-bearing.
    """
    collapsed = " ".join(str(value).split())
    collapsed = _HOOK_LOG_PATHISH.sub("<path>", collapsed)
    collapsed = _HOOK_LOG_UUIDISH.sub("<uuid>", collapsed)
    collapsed = collapsed.replace("=", _HOOK_LOG_FIELD_SEPARATOR_SUBSTITUTE)
    return collapsed[:limit]


def _hook_log_error_detail(exc: BaseException, *, limit: int = 200) -> str:
    """One bounded, privacy-safe ``<class>: <message>`` for a hook-tick line.

    Two things the plain ``f"{type(exc).__name__}: {exc}"`` emits that this
    durable, deliberately-bounded diagnostic must not.

    A FILESYSTEM PATH. The whole ``OSError`` family puts ``filename`` in its
    ``str()``, so one ``PermissionError: [Errno 13] Permission denied:
    '/Users/<name>/.codex/sessions/…/rollout-…-<uuid>.jsonl'`` writes a username
    AND a conversation identifier into the log — the same exposure for which a
    traceback was already rejected, without the traceback. The family is
    narrowed to ``errno``/``strerror``, which is the diagnostic half, and
    whatever remains is additionally scrubbed of path-shaped AND UUID-shaped
    tokens: the callers are blanket ``except`` blocks that can catch anything,
    including our own ``ValueError(f"… {path}")``. The two scrubs are separate
    rules because the path one recognises only an ABSOLUTE or ``~``-relative
    root — a relative ``.codex/sessions/…`` spelling escapes it, and so does a
    bare conversation key, neither of which the ``OSError`` narrowing can reach
    on a non-``OSError`` type. Between them: a username or home directory can
    only appear under an absolute or ``~`` root, and a conversation id is a UUID
    wherever it appears.

    An ``=``. The only real reader of these lines is a LAST-WINS
    ``{k: v for token in tokens if "=" in token}`` comprehension
    (``_codex_lifecycle_activity_24h``), so a message containing
    ``provider=claude`` or ``result=success`` overrides the true field — the
    record is dropped from doctor's view, or an errored tick is counted as a
    success. Appending the free text LAST does not protect the fixed columns
    from that parser; last-wins means it is precisely the position that loses.
    """
    detail = (
        f"[Errno {exc.errno}] {exc.strerror}"
        if isinstance(exc, OSError) and exc.strerror
        else str(exc)
    )
    return _hook_log_safe_free_text(
        f"{type(exc).__name__}: {detail}".strip(), limit=limit)


def _codex_lifecycle_log_line(
    *, source_root_key: str, event: str, sync: str, result: str,
    blocks: int, milestones: int, alert_eligible_roots: int,
    quota_alerts: int, budget_alerts: int, dur_ms: int, backlog: int = 0,
    error: str = "",
) -> str:
    """Render one privacy-safe root-qualified Codex lifecycle outcome.

    Native hook input can contain session paths and conversation identifiers;
    this durable diagnostic deliberately carries only the bounded event label,
    opaque source root key, aggregate reconciliation counts, duration, and — on
    an errored tick — a DEFUSED exception class and message.

    ``error`` is the one free-text field. It carries the class and message from
    the hook's blanket except, never a traceback and never anything derived from
    hook stdin, because a `result=error` tick with nothing else recorded is
    undiagnosable and has already cost one debugging round.

    Its position is last for readability, NOT for safety, and the comment that
    claimed otherwise had it backwards. The only real reader of this line is a
    LAST-WINS `k=v` comprehension (`_codex_lifecycle_activity_24h`), against
    which last position is the WINNING one — a message containing
    `provider=claude` dropped the whole record from doctor's view, and one
    containing `result=success` counted an errored tick as a success. Safety
    comes from `_hook_log_safe_free_text`, which is what keeps every fixed
    column authoritative and every path out of the file. A caller holding the
    exception should pass `_hook_log_error_detail(exc)`, so the `OSError`
    family's embedded `filename` is narrowed away at the source as well.
    """
    safe_event = "".join(
        char for char in str(event)[:40] if char.isalnum() or char in "-_"
    ) or "unknown"
    suffix = ""
    if error:
        collapsed = _hook_log_safe_free_text(error)
        if collapsed:
            suffix = f" error={collapsed}"
    return (
        f"{now_utc_iso()} provider=codex source_root_key={source_root_key} "
        f"event={safe_event} sync={sync} blocks={int(blocks)} "
        f"milestones={int(milestones)} "
        f"alert_eligible_roots={int(alert_eligible_roots)} "
        f"quota_alerts={int(quota_alerts)} budget_alerts={int(budget_alerts)} "
        f"backlog={max(0, int(backlog))} "
        f"dur_ms={max(0, int(dur_ms))} result={result}{suffix}"
    )


def _codex_lifecycle_roots():
    """Snapshot usable configured Codex homes in stable root-key order."""
    return codex_hook_roots(_cctally()._codex_home_roots())


def _stats_epoch_rebuild_pending() -> bool:
    """Delegate the side-effect-free epoch probe to the store boundary."""
    try:
        import _cctally_store
        return _cctally_store.stats_epoch_rebuild_pending()
    except Exception:
        return False


def _defer_stats_epoch_rebuild() -> str:
    """Hand a pending epoch rebuild to the dedicated store worker."""
    import _cctally_store
    return _cctally_store.defer_stats_epoch_rebuild()


def _cmd_hook_tick_codex(
    args: argparse.Namespace, *, event: str = "unknown",
    transcript_path: str = "",
) -> int:
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
        backlog: int = 0, error: str = "",
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
                backlog=backlog,
                dur_ms=dur_ms,
                error=error,
            ))
        _hook_tick_log_rotate_if_needed()

    # public #5: a pending stats.db epoch rebuild is the one operation on this
    # path that neither the ingest budget nor the projection's `defer` can
    # bound, because it happens inside `open_db()` before any of this code gets
    # a say. Measured on a real 211K-observation / 1,859-rollout store, the
    # first tick after the epoch bump cost 82.05s wall — 76.45s of it the
    # journal rebuild — against Codex's 30-second hook timeout. A killed rebuild
    # commits nothing, so the next tick repeats it: a non-converging
    # 30-second-per-turn loop, which is the reported defect delivered by the
    # fix. Hand it to the dedicated store-owned epoch worker and acknowledge
    # this tick as a no-op. No lifecycle marker is stamped, so the next Codex
    # turn re-checks immediately; store admission suppresses duplicate workers.
    if _stats_epoch_rebuild_pending():
        _defer_stats_epoch_rebuild()
        log_outcome(sync="deferred", result="noop")
        release_lifecycle_locks(locks)
        return 0

    try:
        # Hook stdout/stderr is contractually silent.  Cache migration and
        # ingest diagnostics remain available to explicit CLI operations.
        with open(os.devnull, "w", encoding="utf-8") as quiet, \
                contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            cache = c.open_cache_db()
            try:
                cache_mod = c._load_sibling("_cctally_cache")
                # public #5 spec §4: the hook's ingest leg is BUDGETED and
                # resumable, and it ingests the active rollout first so live
                # numbers stay correct while history lags. Only this caller
                # passes a budget — an explicit `cctally cache-sync` still runs
                # to completion.
                import _cctally_config as _cfg_codex
                budget_seconds = _cfg_codex.resolve_codex_hook_ingest_budget(
                    c.load_config())
                stats, cache = cache_mod._run_cache_operation_with_recovery(
                    cache,
                    lambda active_conn: c.sync_codex_cache(
                        active_conn, lock_timeout=0,
                        budget_seconds=budget_seconds,
                        active_transcript_path=transcript_path or None,
                        # public #5 spec §4: ONE reconcile per tick. The
                        # explicit alert-eligible reconcile below can never
                        # take the certificate short-circuit (it is guarded by
                        # `not alert_eligible_roots`), so the sync-internal one
                        # was pure duplicated cost.
                        quota_reconcile="defer",
                    ),
                    origin="hook.codex_quota.sync",
                )
            finally:
                cache.close()
            if stats.lock_contended:
                log_outcome(sync="contended", result="noop")
                return 0
            projection = c.reconcile_codex_quota_projection(
                source_root_keys=all_root_keys,
                alert_eligible_root_keys=due_root_keys,
                now=dt.datetime.now(dt.timezone.utc),
                # public #5 spec §2/§4: the hook path NEVER runs a
                # whole-history quota pass inline — not the once-a-day
                # verification, and not a rebuilt stats index, an interpretation
                # bump, a missing reverse map, a reset ledger or a dirty-unit
                # burst either. On a hook-only install no dashboard tick or
                # `codex quota` invocation reaches any of them first, so each
                # would land here as a ~14-30s reconcile on a blocking path
                # against Codex's 30-second timeout. `defer` hands every one of
                # them to the detached `_codex-quota-verify` worker; the bounded
                # ingest above stays foreground, so fresh observations still
                # precede the turn.
                full_pass="defer",
            )
            # Vendor-scoped spend is intentionally evaluated once per
            # successful due-set tick, not once per root. Task 7 Item 4: the
            # on-demand Codex budget firing routes through the single-flight
            # ingest cycle (the `codex_apply` seam) instead of opening its own
            # stats connection — so its `budget_milestones` (vendor=codex)
            # crossings are journaled by the budget harvest and its alerts
            # dispatch post-commit (set-then-dispatch). AUTHORITATIVE so a failure
            # propagates to this leg's try/except (raise_errors=True preserved).
            import _cctally_journal as _jr_codex
            _budget_holder = {"n": 0}

            def _codex_budget_leg(ictx):
                _budget_holder["n"] = int(c.maybe_record_codex_budget_milestone(
                    {}, conn=ictx.conn, alert_sink=ictx.pending_alerts,
                    raise_errors=True) or 0)

            budget_ingest = _jr_codex.run_stats_ingest(
                mode="authoritative", codex_apply=_codex_budget_leg)
            if not budget_ingest.ran:
                # A detached quota-verification worker can own the ingest lock
                # while applying its projection.  Authoritative ingest reports
                # that bounded timeout as `ran=False`; acknowledging the root
                # here would throttle away the skipped budget evaluation.  Keep
                # the lifecycle markers due so the first uncontended tick
                # retries the forward-only budget crossing.
                log_outcome(
                    sync="contended", result="noop", projection=projection,
                    backlog=int(getattr(stats, "backlog_files", 0) or 0),
                )
                return 0
            budget_alerts = _budget_holder["n"]
        if getattr(stats, "deferred_reason", None) == "replay_pending":
            # public #5: the budgeted tick declined a byte-zero replay, and on
            # a hook-only install nothing else would ever perform one — no
            # dashboard, no `codex quota`, no `cache-sync`. Every following tick
            # would return at the same decline and Codex ingest would freeze
            # permanently. Hand the unbudgeted drain to a detached worker.
            # Outside the cache flocks by construction (the sync released them
            # before returning) and after the connection closed, so the worker
            # is not racing this tick for the writer lock.
            cache_mod._defer_codex_replay_drain()
        mark_lifecycle_success(locks)
        log_outcome(
            # A byte-zero Codex replay is not sliceable, so a budgeted tick
            # declines it outright and hands the unbudgeted drain to a detached
            # worker (above). Say so in the lifecycle line rather than reporting
            # a sync that did not walk anything as "ok".
            sync="deferred" if getattr(stats, "deferred_reason", None) else "ok",
            result="success", projection=projection,
            budget_alerts=budget_alerts,
            backlog=int(getattr(stats, "backlog_files", 0) or 0),
        )
    except Exception as exc:
        # A failed sync, projection, or budget evaluation must acknowledge no
        # root.  Hooks are best-effort and remain a successful no-op to Codex.
        #
        # The class and message go into the lifecycle line. Discarding them
        # already cost a debugging round: a changed keyword signature raised
        # TypeError inside this block and presented as a silent `result=error`
        # tick indistinguishable from a database failure. No traceback (this is
        # a durable, privacy-bounded diagnostic and a traceback carries paths),
        # and nothing reaches stdout or stderr — those stay contractually
        # silent.
        #
        # `_hook_log_error_detail`, not a bare f-string: rejecting the traceback
        # for carrying paths and then interpolating `str(exc)` was the same leak
        # one layer down, because the whole `OSError` family embeds `filename`
        # — a rollout path is a username plus a conversation UUID.
        log_outcome(
            sync="error", result="error",
            error=_hook_log_error_detail(exc),
        )
        return 0
    finally:
        release_lifecycle_locks(locks)
    return 0


def _record_dashboard_activity(provider: str, transcript_path: str) -> None:
    """Write the hook ticket or invalidate every caught-up certificate."""
    try:
        frontier = _cctally()._load_sibling("_lib_ingest_frontier")
        if not frontier.record_activity(
            _cctally_core.APP_DIR, provider, str(transcript_path or ""),
        ):
            frontier.invalidate_activity_marker(_cctally_core.APP_DIR)
    except Exception:
        # Hook execution remains best-effort and always successful. A runtime
        # directory that cannot be mutated offers no additional durable signal
        # beyond this bounded attempt.
        pass


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
        transcript = (
            meta.get("transcript_path", "") if isinstance(meta, dict) else "")
        _record_dashboard_activity("codex", str(transcript or ""))
        return _cmd_hook_tick_codex(
            args, event=event, transcript_path=str(transcript or ""))
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
    _record_dashboard_activity(
        "claude", str(meta.get("transcript_path", "") or ""),
    )

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
                cache_mod = _cctally()._load_sibling("_cctally_cache")
                stats, cache_conn = (
                    cache_mod._run_cache_operation_with_recovery(
                        cache_conn,
                        lambda active_conn: sync_cache(active_conn),
                        origin="hook.claude.sync",
                    )
                )
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

    # #496 S6 §5.2, the hook-tick branch. It is admitted HERE rather than from
    # `main`'s post-command hook because the parent returns at the fork and
    # never reaches that hook, and because the INLINE FALLBACK — `os.fork()`
    # failed, so this body is running on Claude Code's blocking path — must not
    # gain a spawn. `forked` is exactly that discriminator.
    try:
        _cctally()._load_sibling(
            "_cctally_retention"
        ).maybe_defer_artifact_retention(
            command="hook-tick",
            exit_code=0,
            hook_forked=forked,
            hook_explain=explain,
            hook_foreground=foreground,
        )
    except Exception:
        pass

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


_USAGE_SNAPSHOT_COLUMNS = (
    "captured_at_utc", "week_start_date", "week_end_date", "week_start_at",
    "week_end_at", "weekly_percent", "page_url", "source", "payload_json",
    "five_hour_percent", "five_hour_resets_at", "five_hour_window_key",
    "account_key",
)


def _usage_snapshot_columns(conn, payload, week_start_name):
    """Compute the ``weekly_usage_snapshots`` column map + output ``saved`` dict
    for a payload — the exact canonicalization ``insert_usage_snapshot`` does —
    WITHOUT inserting (DB journal redesign §5.3).

    Returns ``(cols, saved)`` where ``cols`` is the ordered column→value map
    (no ``id`` / ``journal_id``, key order == ``_USAGE_SNAPSHOT_COLUMNS``) and
    ``saved`` is the output dict minus ``id``. Shared by
    ``insert_usage_snapshot`` (bare INSERT, legacy) and the ingest obs pipeline
    hook (``snapshot_accept`` Model-A emit) so the two write paths never drift.
    ``conn`` is used only for the ``_get_canonical_boundary_for_date`` override.
    """
    weekly_percent = _safe_float(payload.get("weeklyPercent"))
    captured_at, _captured_at_dt = _coerce_payload_captured_at(payload)

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
                eprint(
                    f"[record-usage] fiveHourWindowKey coerce failed "
                    f"(got {type(five_hour_window_key).__name__}: "
                    f"{five_hour_window_key!r}); "
                    f"5h DB clamp will be skipped for this row: {exc}"
                )
                _logged_window_key_coerce_failure = True
            five_hour_window_key = None

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

    cols = {
        "captured_at_utc": captured_at,
        "week_start_date": week_start.isoformat(),
        "week_end_date": week_end.isoformat(),
        "week_start_at": week_window.week_start_at,
        "week_end_at": week_window.week_end_at,
        "weekly_percent": weekly_percent,
        "page_url": page_url,
        "source": source,
        "payload_json": json.dumps(payload, separators=(",", ":")),
        "five_hour_percent": five_hour_percent,
        "five_hour_resets_at": five_hour_resets_at,
        "five_hour_window_key": five_hour_window_key,
        # Account dimension (#341): carried from the payload when present (the
        # snapshot_accept emit path threads the resolved account through here);
        # defaults to the reserved sentinel for the bare test-only insert path.
        "account_key": payload.get("account_key") or "unattributed",
    }

    saved = {
        "capturedAt": captured_at,
        "weekStartDate": week_start.isoformat(),
        "weekEndDate": week_end.isoformat(),
        "weeklyPercent": weekly_percent,
    }
    if week_window.week_start_at:
        saved["weekStartAt"] = week_window.week_start_at
    if week_window.week_end_at:
        saved["weekEndAt"] = week_window.week_end_at
    if isinstance(payload.get("resetText"), str):
        saved["resetText"] = payload["resetText"]
    if five_hour_percent is not None:
        saved["fiveHourPercent"] = five_hour_percent
    if five_hour_resets_at is not None:
        saved["fiveHourResetsAt"] = five_hour_resets_at
    if five_hour_window_key is not None:
        saved["fiveHourWindowKey"] = five_hour_window_key
    return cols, saved


def insert_usage_snapshot(payload: dict[str, Any], week_start_name: str) -> dict[str, Any]:
    conn = open_db()
    try:
        cols, saved = _usage_snapshot_columns(conn, payload, week_start_name)
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
              five_hour_window_key,
              account_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(cols[k] for k in _USAGE_SNAPSHOT_COLUMNS),
        )
        snapshot_id = int(cur.lastrowid)
        # DB journal redesign: every weekly_usage_snapshots row carries a
        # journal_id now — production writes go through the ``snapshot_accept``
        # evt (grep-proof: no production caller of this bare-insert helper
        # remains; it is test/fixture-only). Stamp a deterministic synthetic id
        # so a row inserted here is still reverse-referenceable by a milestone's
        # ``usage_snapshot_id`` at ingest harvest (a NULL journal_id would be a
        # harvest-order violation — the harvest cannot build a logical FK to it).
        conn.execute(
            "UPDATE weekly_usage_snapshots SET journal_id = ? WHERE id = ?",
            (f"sa:direct:{snapshot_id}", snapshot_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": snapshot_id, **saved}


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


# ==========================================================================
# DB journal redesign — ingest pipeline hooks (spec §5.2 step 4b / §5.3)
#
# These are the Task-6 (6e) COMPOSITION of the reviewed machinery: the obs
# derivation transplant (`_pipeline_claude_usage`), the record-credit op
# (`_pipeline_record_credit`), and the sync-week op (`_pipeline_sync_week`).
# Registered ONCE into `_cctally_journal.PIPELINE` at module wiring time (below).
# The CLI/hook call sites append `make_obs`/`make_op` lines and invoke
# `run_stats_ingest`; the cycle drives these hooks per record on ctx.conn with
# `as_of = record["at"]` and the alert sink `ctx.pending_alerts`.
# ==========================================================================

# Claude rate-limit obs writer identities (spec §4.2 `src`). The obs hook fires
# only for these + provider==claude + a payload that carries the raw capture
# (`resets_at` + `weekly_percent`); anything else (a codex quota obs, a minimal
# test obs) is skipped so a foreign line never mis-drives the Claude derivation.
_CLAUDE_OBS_SRCS = frozenset(
    {"statusline", "hook-tick", "refresh-usage", "record-usage"}
)


def _derive_5h_window_key(conn, five_hour_resets_at_epoch):
    """Resolve the canonical 5h window key for a raw `five_hour_resets_at` epoch,
    on `conn` (extracted from cmd_record_usage's Tier 1/2/3 prior-anchor logic).

    Tier 1: nearest canonical `five_hour_blocks` row within ±3×jitter-floor;
    Tier 2: latest snapshot anchor (legacy fallback); Tier 3 (implicit): the pure
    600s floor. Passing the prior anchor collapses boundary-straddling
    seconds-jitter to the first-seen key (spec 5h invariant #3). Runs at ingest
    (not capture) now, so the key is derived against the same DB state the fold
    decision + clamp read."""
    c = _cctally()
    prior_5h_epoch = None
    prior_5h_key = None
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
            prior_5h_epoch = int(parse_iso_datetime(
                prior_block_row["five_hour_resets_at"], "prior 5h block anchor"
            ).timestamp())
            prior_5h_key = int(prior_block_row["five_hour_window_key"])
    except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
        eprint(f"[ingest] prior 5h block-anchor lookup failed: {exc}")

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
                prior_5h_epoch = int(parse_iso_datetime(
                    prior_5h_row["five_hour_resets_at"], "prior 5h anchor"
                ).timestamp())
                prior_5h_key = int(prior_5h_row["five_hour_window_key"])
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            eprint(f"[ingest] prior 5h anchor lookup failed: {exc}")

    return _canonical_5h_window_key(
        five_hour_resets_at_epoch,
        prior_epoch=prior_5h_epoch,
        prior_key=prior_5h_key,
    )


def _run_dollar_axes(saved, *, conn, as_of, alert_sink, enabled=True):
    """The four dollar-decoupled alert axes in cmd_record_usage's legacy order
    (budget → project-budget → codex-budget → projected). Runs on BOTH the accept
    path AND every dedup-skip tick (spec §4.5: USD spend can cross a $ threshold
    while the weekly/5h percent is flat). Passed-conn → the chokepoints fold into
    the cycle txn and re-raise on failure (invariant ii); their crossings' alert
    payloads land in `alert_sink` for post-commit dispatch."""
    if not enabled:
        return
    c = _cctally()
    c.maybe_record_budget_milestone(
        saved, conn=conn, as_of=as_of, alert_sink=alert_sink)
    c.maybe_record_project_budget_milestone(
        saved, conn=conn, as_of=as_of, alert_sink=alert_sink)
    c.maybe_record_codex_budget_milestone(
        saved, conn=conn, as_of=as_of, alert_sink=alert_sink)
    c.maybe_record_projected_alert(
        saved, conn=conn, as_of=as_of, alert_sink=alert_sink)


def _write_hwm_files(week_start_date, weekly_percent,
                     five_hour_window_key, five_hour_percent):
    """Write the hwm-7d / hwm-5h projection files (statusline no-regression), the
    monotonic-guarded tail of cmd_record_usage's accept path. Projection files
    (never journaled); re-materialized on rebuild. Best-effort (OSError-swallow),
    matching the legacy write sites."""
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

    if five_hour_percent is not None and five_hour_window_key is not None:
        try:
            hwm5_path = _cctally_core.APP_DIR / "hwm-5h"
            existing_hwm5 = 0.0
            try:
                parts5 = hwm5_path.read_text().strip().split()
                if len(parts5) == 2 and parts5[0] == str(five_hour_window_key):
                    existing_hwm5 = float(parts5[1])
            except (FileNotFoundError, ValueError, OSError):
                pass
            if hwm_file_next(existing_hwm5, five_hour_percent) is not None:
                hwm5_path.write_text(
                    f"{five_hour_window_key} {five_hour_percent}\n")
        except OSError:
            pass


def _pipeline_claude_usage(ctx, rec):
    """Obs-derivation pipeline hook (spec §5.2 step 4b) — the ingest-cycle
    transplant of ``cmd_record_usage``'s derivation body.

    Fires per Claude rate-limit obs (src in the writer set, provider claude, a
    raw-capture payload). Canonicalizes the RAW capture on ``ctx.conn`` (week
    boundaries from ``resets_at`` + the Tier 1/2/3 5h window key), runs
    ``detect_reset_and_credit`` (transaction-neutral, capture-time, Design B
    ``ctx``), then makes the snapshot accept/skip decision ONCE via
    ``_usage_snapshot_fold_decision``. On ACCEPT it journals the row through a
    ``snapshot_accept`` Model-A evt (the sole ``weekly_usage_snapshots`` writer).
    The fold decision gates ONLY the snapshot insert — the derivation chokepoints
    run for EVERY line (spec §4.5, "dedup must not gate side effects"): milestone
    (with the cost-sync ``journal`` threaded), 5h block, then the four dollar
    axes, in legacy order. On ACCEPT they derive against the fresh row + the hwm
    projection files are written; on a dedup SKIP they re-run (idempotently)
    against the latest snapshot, which is what subsumes today's kill-window
    self-heal probes (a flat tick that owes a milestone or crosses a $ threshold
    still derives it)."""
    if rec.get("t") != "obs" or rec.get("provider") != "claude":
        return
    if rec.get("src") not in _CLAUDE_OBS_SRCS:
        return
    payload = rec.get("payload") or {}
    if "resets_at" not in payload or "weekly_percent" not in payload:
        return

    c = _cctally()
    conn = ctx.conn
    # `as_of` is the record's `at` (= `_command_as_of()` at capture, CCTALLY_AS_OF
    # in tests) — DETECTION timing (reset/credit) AND the dollar-axis budget/period
    # WINDOW clock (6g FIX 2b moved the four dollar axes off `capture_at` onto
    # `as_of`; see `_run_dollar_axes` below), matching legacy. `capture_at` is the
    # CAPTURE wall clock — the snapshot `captured_at_utc` + the milestone/5h-block
    # stamps + the block cost-sum range end, also matching legacy (which used
    # `now_utc_iso()` for those). In production the
    # two are the same instant; the split only shows under the CCTALLY_AS_OF test
    # hook. Falls back to `as_of` for a payload without `captured_at` (robustness).
    as_of = ctx.as_of_for(rec)
    capture_at = payload.get("captured_at") or as_of
    weekly_percent = float(payload["weekly_percent"])
    resets_at = int(payload["resets_at"])
    source = payload.get("source", "statusline")
    # Account dimension (#341): the obs carries the account stamp (record-usage /
    # statusline resolve it once per read, Step 9). Absent (pre-multi-account /
    # legacy obs) -> the reserved sentinel, which keeps every scoped query
    # byte-identical on a single-account install.
    import _lib_accounts
    account_key = rec.get("account") or _lib_accounts.UNATTRIBUTED

    # Week boundaries from resets_at (cmd_record_usage canonicalization).
    # Normalize before deriving the date keys as well as the ISO overrides:
    # rounding a 23:30–23:59 boundary crosses midnight, and letting
    # `_usage_snapshot_columns` normalize only the saved row would fork the
    # milestone's retained cost window onto the prior calendar date.
    week_end_at_dt = _cctally_core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(resets_at, tz=dt.timezone.utc)
    )
    week_start_at_dt = week_end_at_dt - dt.timedelta(days=7)
    week_start_date = week_start_at_dt.date().isoformat()
    week_end_date = week_end_at_dt.date().isoformat()
    week_start_at = week_start_at_dt.isoformat(timespec="seconds")
    week_end_at = week_end_at_dt.isoformat(timespec="seconds")

    # 5h fields (raw, post-ingress-drop) + canonical window key on ctx.conn.
    five_hour_percent = payload.get("five_hour_percent")
    if five_hour_percent is not None:
        five_hour_percent = float(five_hour_percent)
    five_hour_resets_at_str = payload.get("five_hour_resets_at")
    five_hour_window_key = None
    if five_hour_resets_at_str is not None:
        try:
            fh_epoch = int(parse_iso_datetime(
                five_hour_resets_at_str, "obs.five_hour_resets_at").timestamp())
            five_hour_window_key = _derive_5h_window_key(conn, fh_epoch)
        except (ValueError, TypeError) as exc:
            eprint(f"[ingest] 5h window-key derivation failed: {exc}")

    # 1. Reset/credit detection (fold into the cycle txn; Design B suppression).
    c.detect_reset_and_credit(
        conn,
        week_start_date=week_start_date,
        week_end_at=week_end_at,
        weekly_percent=weekly_percent,
        five_hour_window_key=five_hour_window_key,
        five_hour_percent=five_hour_percent,
        as_of=as_of,
        commit=False,
        ctx=ctx,
        account_key=account_key,
        # #703 + #707: a credit this observation causes is identified by that
        # observation. `sa:<obs id>` is the logical identity its snapshot_accept
        # evt carries, and it is derived from the obs record's content digest,
        # so it exists whether or not this particular tick goes on to emit one —
        # a credit tick is frequently a clamp skip. `capture_at` is the payload
        # capture instant, which is the domain the credit's observation columns
        # must share with `weekly_usage_snapshots.captured_at_utc`.
        source_identity=f"sa:{rec['id']}",
        capture_at=capture_at,
    )

    # 2. Accept/skip DECISION (clamp + dedup), made ONCE and journaled via the
    #    snapshot_accept evt (so replay never re-derives it — spec §5.3).
    import _cctally_journal as jr
    skip, adjusted_5h, skip_reason = jr._usage_snapshot_fold_decision(conn, {
        "week_start_date": week_start_date,
        "week_start_at": week_start_at,
        "week_end_at": week_end_at,
        "weekly_percent": weekly_percent,
        "five_hour_percent": five_hour_percent,
        "five_hour_window_key": five_hour_window_key,
        "account_key": account_key,
    })
    five_hour_percent = adjusted_5h

    # 3. Resolve the derivation target `saved`. The fold DECISION gates ONLY the
    #    snapshot INSERT (snapshot_accept); the derivations below run for EVERY
    #    line (spec §4.5: "the ingester's snapshot-insert dedup is the same rule
    #    as today's ... while derivations run for every line — preserving the
    #    'dedup must not gate side effects' invariant structurally"). A dedup-skip
    #    tick re-runs the (idempotent) chokepoints against the latest snapshot —
    #    which is exactly what subsumes today's kill-window self-heal probes.
    #    On ACCEPT `saved` is the freshly-journaled row; on SKIP it is the latest.
    if not skip:
        out_payload = {
            "source": source,
            "capturedAt": capture_at,
            "weeklyPercent": weekly_percent,
            "weekStartDate": week_start_date,
            "weekEndDate": week_end_date,
            "weekStartAt": week_start_at,
            "weekEndAt": week_end_at,
        }
        if five_hour_percent is not None:
            out_payload["fiveHourPercent"] = five_hour_percent
        if five_hour_resets_at_str is not None:
            out_payload["fiveHourResetsAt"] = five_hour_resets_at_str
        if five_hour_window_key is not None:
            out_payload["fiveHourWindowKey"] = five_hour_window_key

        week_start_name = get_week_start_name(ctx.config or {}, None)
        cols, saved = _usage_snapshot_columns(conn, out_payload, week_start_name)
        # Stamp the account onto the snapshot_accept evt columns so the journaled
        # row (and every replay/rebuild fold of it) carries the account (#341).
        cols["account_key"] = account_key
        rowid = jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id=f"sa:{rec['id']}",
            table="weekly_usage_snapshots",
            columns=cols,
            at=capture_at,
        )
        # An exact retry can meet effective event metadata whose physical row a
        # later suppression deliberately deleted.  emit_model_a must not
        # resurrect that row and therefore returns None; deriving milestones or
        # blocks from the absent target would either create dangling references
        # or attempt int(None).  This is distinct from an ordinary fold-decision
        # skip, which resolves a real latest snapshot below and still runs the
        # idempotent side-effect chokepoints.
        if rowid is None:
            return
        saved["id"] = rowid
    else:
        latest = conn.execute(
            "SELECT * FROM weekly_usage_snapshots WHERE week_start_date = ? "
            "  AND account_key = ? "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (week_start_date, account_key),
        ).fetchone()
        if latest is None:
            return  # nothing recorded yet -> nothing to derive against
        saved = _saved_dict_from_usage_row(latest)

    # 4. Derivations run for EVERY line, legacy order (milestone → 5h block → $
    #    axes). maybe_record_milestone threads journal=(ctx, rec id) for its cost
    #    sync; maybe_update_five_hour_block writes the block before its
    #    5h-milestone block_id read (P2-8). Idempotent under re-run on a dedup
    #    tick (INSERT OR IGNORE / upsert), so a flat tick that owes a milestone or
    #    crosses a $ threshold still derives it.
    #    The one exception is a CLAMP skip. A dedup skip AGREES with the stored
    #    row, so re-deriving against it is the self-heal; a clamp skip means the
    #    incoming 7d percent is strictly BELOW the reset-aware in-window maximum,
    #    so the observation CONTRADICTS `saved` and a weekly milestone derived
    #    from `saved`'s higher percent records a crossing the meter says did not
    #    happen. That is how the 2026-09-01 incident fabricated a 13% milestone
    #    in a fresh post-credit epoch from a stale pre-credit replica, and
    #    because milestones are forward-only within an epoch that row forecloses
    #    every genuine crossing below it there. Only the WEEKLY milestone is
    #    gated: the 5h block derivation below (and the window-rollover heal at
    #    step 4') genuinely need the skip path.
    if skip_reason != SNAPSHOT_SKIP_CLAMP:
        c.maybe_record_milestone(
            saved, conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
            journal=(ctx, rec["id"]), account_key=account_key,
            retained_selection=c.WeekSelection(
                week_start=dt.date.fromisoformat(week_start_date),
                week_end=dt.date.fromisoformat(week_end_date),
                start_iso_override=week_start_at,
                end_iso_override=week_end_at,
            ))
    c.maybe_update_five_hour_block(
        saved, conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
        account_key=account_key, journal_ctx=ctx)
    # The dollar-decoupled axes resolve a CURRENT budget/period WINDOW from
    # "now" and must see the record's DETECTION clock (`as_of` = rec["at"] =
    # `_command_as_of()`), NOT the wall-clock capture stamp: legacy
    # cmd_record_usage's dedup self-heal called these with as_of=None ->
    # `_command_as_of()`, so a CCTALLY_AS_OF-pinned tick resolved the pinned
    # week's window (capture_at is the raw wall clock, which lands outside a
    # pinned fixture window and silently drops the crossing). Both collapse to
    # real-now in production; the split only matters under a pinned as_of.
    _run_dollar_axes(
        saved,
        conn=conn,
        as_of=as_of,
        alert_sink=ctx.pending_alerts,
        # Budget/projected rows depend on historical config that was not retained
        # in the journal. Task B classifies them as re-materialized projections:
        # the planner retires stale latches and never fabricates old config.
        enabled=(ctx.event_sink is None),
    )

    # 4'. Window-rollover 5h-block heal (SKIP path only). A dedup skip swallows
    #     the snapshot insert, so `saved` (the dedup target — the LATEST stored
    #     row) still carries the PREVIOUS 5h window; the derivations above only
    #     ever touched that old (still-fresh) window. When the INCOMING record
    #     observed a NEW canonical `five_hour_window_key` whose `five_hour_blocks`
    #     anchor doesn't exist yet, materialize it BLOCK-ONLY — no snapshot
    #     insert, the tick stays deduped — against a saved dict carrying the
    #     INCOMING record's 5h identity, NOT `saved`'s. Without this the active
    #     window is left unanchored (blocks/dashboard fall back to the heuristic
    #     "~") until the percent next moves. Ported from the legacy
    #     cmd_record_usage dedup self-heal (spec §4.5 "dedup must not gate side
    #     effects"; regression: bin/cctally-record-usage-selfheal-test
    #     window-rollover scenario).
    if (skip and five_hour_window_key is not None
            and five_hour_percent is not None
            and five_hour_resets_at_str is not None):
        latest_wk = saved.get("fiveHourWindowKey")
        if latest_wk is None or int(latest_wk) != int(five_hour_window_key):
            if conn.execute(
                "SELECT 1 FROM five_hour_blocks WHERE five_hour_window_key = ? "
                "  AND account_key = ? LIMIT 1",
                (int(five_hour_window_key), account_key),
            ).fetchone() is None:
                c.maybe_update_five_hour_block(
                    {
                        "id": saved.get("id"),
                        "capturedAt": capture_at,
                        "weeklyPercent": weekly_percent,
                        "fiveHourPercent": five_hour_percent,
                        "fiveHourResetsAt": five_hour_resets_at_str,
                        "fiveHourWindowKey": int(five_hour_window_key),
                    },
                    conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
                    account_key=account_key, journal_ctx=ctx)

    # 5. hwm-7d / hwm-5h projection files — ACCEPT path only (the monotonic
    #    writer; a dedup tick's percent is already <= the stored HWM). In-place
    #    credit force-writes live in detect_reset_and_credit.
    if not skip and ctx.projection_writes:
        _write_hwm_files(week_start_date, weekly_percent,
                         five_hour_window_key, five_hour_percent)


def _pipeline_record_credit(ctx, rec):
    """Op-derivation hook for a ``record-credit`` op line (spec §5.3 event+effects).

    The built-in ``_pipeline_op_fold`` runs FIRST and writes the
    ``weekly_credit_floors`` row from this op's floor columns (Option (i): the
    op fold is the SOLE floor writer on the ingest path, stamping
    ``journal_id = rec['id']``). This hook then reconstructs the ``CreditPlan``
    from the op payload and applies the same-window credit's DESTRUCTIVE effects
    via ``_apply_credit(ctx=..., id_base=rec['id'])`` — which emits the
    ``weekly_credit_effects`` evt (suppression list + forced hwm floor) and the
    synthetic post-credit ``snapshot_accept`` evt, and (Option (i)) SKIPS its own
    floor INSERT.

    ``payload['forced']`` (a ``--force`` re-record) threads into ``_apply_credit``
    so the wce evt ALSO journals the destructive clear of this week's OLD
    synthetic snapshots + OLD credit-floor rows — the ingest-path replacement for
    legacy ``_force_clear_credit``'s inline DELETEs (spec §5.3)."""
    if rec.get("t") != "op":
        return
    payload = rec.get("payload") or {}
    if payload.get("kind") != "weekly_credit_floor":
        return
    plan_data = payload.get("plan")
    if not plan_data:
        return
    c = _cctally()
    plan = argparse.Namespace(**plan_data)
    five_hour = tuple(payload.get("five_hour") or (None, None, None))
    # Two-shaped stamp (#341): the record-credit op carries account_key in its
    # payload (Step 9 / Task 3 resolves the active account before appending it);
    # default to the sentinel for a legacy op written pre-#341.
    import _lib_accounts
    c._apply_credit(
        ctx.conn, plan, five_hour=five_hour, as_of=rec["at"],
        commit=False, ctx=ctx, id_base=rec["id"],
        forced=bool(payload.get("forced")),
        account_key=(payload.get("account_key") or _lib_accounts.UNATTRIBUTED))


def _pipeline_sync_week(ctx, rec):
    """Op-derivation hook for a ``sync_week`` op line (spec §5.3 / Appendix A):
    compute the week cost on ``ctx.conn`` and journal the ``weekly_cost_snapshots``
    row via a Model-A ``weekly_cost_snapshot`` evt (``journal=(ctx, rec['id'])``),
    so the authoritative CLI caller reads the row back for output and replay reads
    the cost verbatim (never recomputing from pruned provider JSONL)."""
    if rec.get("t") != "op":
        return
    payload = rec.get("payload") or {}
    if payload.get("kind") != "sync_week":
        return
    c = _cctally()
    args = argparse.Namespace(
        week_start=payload.get("week_start"),
        week_end=payload.get("week_end"),
        week_start_name=payload.get("week_start_name"),
        mode=payload.get("mode", "auto"),
        offline=payload.get("offline", False),
        project=payload.get("project"),
        json=False,
        quiet=True,
    )
    # Two-shaped stamp (#341 P2-1): the sync_week op carries the active account
    # in its payload; the fold stamps the cost snapshot under it (rebuild-
    # deterministic). Absent -> the sentinel (single-account / legacy op).
    import _lib_accounts
    c.cmd_sync_week(args, conn=ctx.conn, as_of=rec["at"],
                    journal=(ctx, rec["id"]),
                    account_key=(payload.get("account_key")
                                 or _lib_accounts.UNATTRIBUTED))


def _register_ingest_pipeline_hooks():
    """Register the 6e obs/op derivation hooks into ``_cctally_journal.PIPELINE``
    exactly once (spec §5.2 — ops fold first via the built-in
    ``_pipeline_op_fold``, then these). Idempotent: ``load_script()`` drops +
    reloads both siblings together (fresh ``PIPELINE`` == ``[_pipeline_op_fold]``),
    and the ``not in`` guard makes a bare re-import of this module without a
    journal reset a no-op rather than a double-registration."""
    import _cctally_journal as jr
    for hook in (_pipeline_claude_usage, _pipeline_record_credit,
                 _pipeline_sync_week):
        if hook not in jr.PIPELINE:
            jr.PIPELINE.append(hook)


_register_ingest_pipeline_hooks()
