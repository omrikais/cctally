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
  ``_get_canonical_boundary_for_date``,
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
  ``open_cache_db`` (→ ``_cctally_cache``), ``_resolve_display_tz_obj``
  (→ ``_lib_display_tz``),
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
from dataclasses import dataclass, field
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
    _latest_reset_event_for_end,
    _reset_aware_floor,
    make_week_ref,
    _get_alerts_config,
    _AlertsConfigError,
    _BudgetConfigError,
    _command_as_of,
    _as_of_or_command,
)
import _lib_accounts  # pure stdlib kernel; UNATTRIBUTED sentinel default (#341)
import _lib_blocks  # pure stdlib kernel; the #751a five-hour ownership rule
from _lib_five_hour import _canonical_5h_window_key, five_hour_milestone_range
from _lib_pricing import _calculate_entry_cost, claude_usage_dict
from _lib_codex_hooks import (
    CODEX_HOOK_THROTTLE_SECONDS,
    acquire_due_lifecycle_locks,
    codex_configuration_generation,
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
    plan_five_hour_source_local_credit,
    FIVE_HOUR_HOLD,
    FIVE_HOUR_SET_BASELINE,
    FIVE_HOUR_ARM,
    FIVE_HOUR_CONFIRM,
    FIVE_HOUR_CANCEL,
    hwm_clamp_applies,
    milestone_coverage_owes,
    hwm_file_next,
    projected_crossings,
    post_reset_seed_has_climb_evidence,
    FIRE_IMMEDIATE,
    CONFIRM_RESET,
    CLEAR_MARKER,
    ARM_MARKER,
    NO_ACTION,
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
        # Resolve the active segment for THIS captured moment, through the
        # one chokepoint (#750 S3, Unit B review). The segment is the
        # week_reset_events row keyed on week_end_at whose effective instant
        # is the LATEST at or before `captured_at`; 0 = pre-credit / no-event
        # sentinel. This site carried its own copy of the query ordered on
        # insertion `id`, which answers "the segment written last" — a
        # backfill row landing after a live-detected one makes that the older
        # reset. `cmd_percent_breakdown` filters milestones on the segment
        # `_latest_reset_event_for_end` returns, so the stamp and the filter
        # named different segments and the milestone rendered on neither.
        captured_at_iso = saved.get("capturedAt") or as_of or now_utc_iso()
        reset_event_id = 0
        reset_effective_iso = None
        if week_end_at:
            seg_row = _latest_reset_event_for_end(
                conn, week_end_at, account_key=account_key,
                as_of_utc=captured_at_iso,
            )
            if seg_row is not None:
                reset_event_id = int(seg_row["id"])
                reset_effective_iso = seg_row["effective_reset_at_utc"]

        max_existing = get_max_milestone_for_week(
            conn, week_start_date, reset_event_id=reset_event_id,
            account_key=account_key,
        )
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
        # The evidence is a stored observation inside this epoch whose floored
        # percent is strictly BELOW the threshold about to be recorded. It is
        # deliberately NOT a tolerance band around `observed_pre_credit_pct`:
        # that comparison is what let the stale replica survive in the first
        # place (issue #703), and repeating it here would inherit the same
        # failure mode. The epoch's lower bound is the governing event's own
        # `effective_reset_at_utc`, which is the same instant `reset_event_id`
        # was resolved against, so the window and the epoch cannot disagree.
        if reset_event_id != 0 and max_existing is None:
            # A bare aggregate SELECT always returns exactly one row, holding
            # NULL when nothing matched, so there is no empty-result case to
            # guard here — the kernel decides the NULL.
            # `weekly_observation_held = 0` (#769 S11, #824): a held row is not
            # an observation of the weekly axis. Its capture time is the tick's
            # while its weekly value is copied from a basis that may have been
            # captured BEFORE this epoch, so counting it would carry a
            # pre-reset reading into a post-reset window and answer "did the
            # counter climb?" with a number that observed nothing about this
            # epoch. That is the 2026-09-01 failure mode reached through the
            # new row shape.
            lowest_in_epoch = conn.execute(
                "SELECT MIN(weekly_percent) FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? AND account_key = ? "
                "  AND weekly_observation_held = 0 "
                "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
                "  AND unixepoch(captured_at_utc) <= unixepoch(?)",
                (week_start_date, account_key, reset_effective_iso,
                 captured_at_iso),
            ).fetchone()[0]
            if not post_reset_seed_has_climb_evidence(
                lowest_in_epoch, current_floor
            ):
                eprint(
                    "[milestone] skipping this crossing — the post-reset "
                    f"segment {reset_event_id} has no observation below "
                    f"{current_floor}%, so a {current_floor}% seed would come "
                    "from a stale pre-credit reading, not a climb"
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

        # For reset-affected weeks, the cached weekly_cost_snapshots row
        # covers the API-derived range (which for a post-reset week
        # backdates into the old window). Live-compute over the effective
        # range so the milestone captures cost from the reset moment
        # forward, not from the phantom backdated start.
        #
        # SCOPED to the account this milestone belongs to (#750 S3, Unit B
        # review). The cost seven lines below is computed for `account_key`
        # over `effective_ref`'s range, so a merged read here lets ANOTHER
        # account's cut shift that range and writes the result as this
        # account's `cumulative_cost`.
        effective_ref = week_ref
        adjusted = _apply_reset_events_to_weekrefs(
            conn, [week_ref], account_key=account_key)
        if adjusted:
            effective_ref = adjusted[0]

        if _week_ref_has_reset_event(
                conn, effective_ref, account_key=account_key):
            import _cctally_cache  # fail-closed attribution guard (#341)
            try:
                live_cost = _compute_cost_for_weekref(
                    effective_ref,
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
                            # The instant the crossing's own cycle began
                            # (#750 S3). `reset_effective_iso` is the
                            # governing event resolved for THIS captured
                            # moment above, so the cycle the alert names and
                            # the segment the milestone was stamped under
                            # cannot disagree. None on segment 0, where the
                            # builder falls back to the week's own start.
                            cycle_start_at=reset_effective_iso,
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


def _five_hour_ownership_windows(
    conn,
    *,
    account_key: str,
    target_key: int,
    target_start: dt.datetime,
    target_reset: dt.datetime,
    load_start: dt.datetime,
    load_end: dt.datetime,
) -> "list[Any]":
    """Every canonical window that can claim an entry in the load range.

    The target is always present, so a caller can price it even before its
    own row exists. The rest come from ``five_hour_blocks`` for the same
    account, selected by INTERVAL OVERLAP rather than by what the caller
    displays: a predecessor that starts before the range still owns the
    entries where the two windows overlap, and a consumer that loaded only
    its own range would reproduce the #751(a) double count on the first row.

    ``block_start_at`` is stored with the writing host's display offset
    while ``five_hour_resets_at`` is UTC, so both comparisons normalize
    through ``unixepoch()`` rather than comparing ISO bytes.
    """
    windows = [
        _lib_blocks.OwnedWindow(
            key=int(target_key), start=target_start, reset=target_reset,
        )
    ]
    try:
        rows = conn.execute(
            """
            SELECT five_hour_window_key, block_start_at, five_hour_resets_at
              FROM five_hour_blocks
             WHERE account_key = ?
               AND five_hour_window_key != ?
               AND unixepoch(five_hour_resets_at) > unixepoch(?)
               AND unixepoch(block_start_at)      <= unixepoch(?)
            """,
            (
                account_key,
                int(target_key),
                load_start.isoformat(),
                load_end.isoformat(),
            ),
        ).fetchall()
    except sqlite3.DatabaseError:
        return windows
    for row in rows:
        try:
            start = parse_iso_datetime(
                row["block_start_at"], "five_hour_blocks.block_start_at",
            ).astimezone(dt.timezone.utc)
            reset = parse_iso_datetime(
                row["five_hour_resets_at"],
                "five_hour_blocks.five_hour_resets_at",
            ).astimezone(dt.timezone.utc)
        except ValueError:
            continue
        windows.append(_lib_blocks.OwnedWindow(
            key=int(row["five_hour_window_key"]), start=start, reset=reset,
        ))
    return windows


def _compute_block_totals(
    block_start_at: dt.datetime,
    range_end: dt.datetime,
    *,
    owner_key: Any,
    windows: "Iterable[Any]",
    skip_sync: bool = False,
) -> dict[str, Any]:
    """Sum tokens + cost over the entries the target block OWNS within
    [block_start_at, range_end], plus per-model and per-project breakdowns
    in the same walk.

    ``windows`` is the full competing-window context — every canonical
    ``OwnedWindow`` whose interval can reach this range, INCLUDING the
    target itself — and ``owner_key`` says which of them this call prices.
    Entries are assigned by `_lib_blocks.resolve_owning_window` (issue
    #751a), so an entry that an adjacent window owns after a reset shift is
    priced once, by that window, instead of by both.

    The two bounds are different predicates and must stay different. The
    load range's upper bound is the observation cutoff and stays INCLUSIVE,
    because migration 009's contract requires an entry exactly at
    ``last_observed_at_utc`` to be counted
    (`tests/test_migration_009_boundary_inclusive.py`). Ownership is the
    half-open ``[start, reset)`` interval. An entry at the cutoff is loaded;
    whether it is priced is then ownership's decision.

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
    ownership_windows = list(windows)
    if not any(w.key == owner_key for w in ownership_windows):
        raise ValueError(
            f"_compute_block_totals: owner_key {owner_key!r} is absent from "
            f"the competing-window context"
        )

    def _priced():
        loaded = get_claude_session_entries(
            block_start_at, range_end, skip_sync=skip_sync,
        )
        owned, _unowned = _lib_blocks.partition_entries_by_owner(
            loaded, ownership_windows,
        )
        for entry in owned[owner_key]:
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
    # #769 S11 (#824). Two concepts, deliberately named apart: the weekly value
    # the BLOCK records at its start and end, and the weekly value a five-hour
    # milestone records as the crossing's observation metadata. They differ on a
    # tick whose weekly axis is held. `_five_hour_saved_from_fold` is the
    # builder that supplies both; a caller passing only the legacy
    # `weeklyPercent` is asserting the two are the same reading, which is true
    # for every tick whose weekly axis was genuinely observed.
    block_weekly_percent = saved.get("blockWeeklyPercent")
    crossing_weekly_percent = saved.get("sevenDayPercentAtCrossing")
    if block_weekly_percent is None:
        block_weekly_percent = saved.get("weeklyPercent")
    if crossing_weekly_percent is None:
        crossing_weekly_percent = saved.get("weeklyPercent")
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
                   five_hour_resets_at AS prior_resets_at,
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
            # The stored reset is the first writer's, and it is what the
            # rest of the estate treats as this block's interval. Ownership
            # reads it rather than this tick's `saved` value, which may
            # carry seconds of Anthropic capture jitter inside the same
            # canonical key.
            try:
                resets_dt = parse_iso_datetime(
                    prior["prior_resets_at"],
                    "five_hour_blocks.five_hour_resets_at",
                )
            except ValueError as exc:
                eprint(f"[5h-block] bad stored resets_at, skipping: {exc}")
                return
        current_is_frozen = (
            prior is not None
            and int(prior["is_closed"]) == 1
            and prior["journal_id"] is not None
        )

        # Step 6 (totals) — done outside the transaction so the
        # cache.db read doesn't hold the stats.db write lock open.
        captured_at_dt = parse_iso_datetime(captured_at, "capturedAt")
        ownership_windows = _five_hour_ownership_windows(
            conn,
            account_key=account_key,
            target_key=int(five_hour_window_key),
            target_start=block_start_dt.astimezone(dt.timezone.utc),
            target_reset=resets_dt.astimezone(dt.timezone.utc),
            load_start=block_start_dt,
            load_end=captured_at_dt,
        )
        totals = _compute_block_totals(
            block_start_dt, captured_at_dt,
            owner_key=int(five_hour_window_key),
            windows=ownership_windows,
        )

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
                    # seven_day_pct_at_block_start / _at_block_end
                    block_weekly_percent,
                    block_weekly_percent,
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
                                # seven_day_pct_at_crossing
                                crossing_weekly_percent,
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
                       SELECT 1 FROM week_reset_events e
                        WHERE e.account_key = five_hour_blocks.account_key
                          AND unixepoch(e.effective_reset_at_utc)
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
# debounce: the first ~0 ARMS this state (it does not fire); the next reading
# CONFIRMS (fires) only if usage stayed low, or CLEARS on recovery toward the
# baseline. The state is needed because the write-site clamp suppresses the
# deferred first zero, so it leaves no other DB trace. The original design is
# docs/superpowers/specs/2026-06-02-reset-zero-debounce-design.md, and it
# describes the FILESYSTEM marker this table replaced — read it for the
# arm/confirm/clear rules only; §1.3 below supersedes its storage and its
# crash-window discussion.
#
# #750 S3 §1.3: it is a stats.db ROW, not a file. The filesystem marker was
# written and unlinked outside the stats transaction, which left two crash
# windows. An arm-side crash rolled the cursor back but left the marker on
# disk, so the retry read the very observation that armed it as a
# confirmation. A confirm-side crash rolled the event back but had already
# unlinked the marker, so the retry saw an unarmed window, classified the low
# reading as NO_ACTION, and lost the reset with no visible symptom. A row
# mutated inside the cycle's own transaction rolls back with the cursor and
# with the event, which closes both. Moving the transition after the commit
# would only move the gap, because the ingest contract commits derived rows
# and cursor together.
#
# It is disposable operational state rather than journal truth, so an epoch
# rebuild legitimately loses it: a genuine reset re-arms and confirms one tick
# later. Losing it is always safe; acting on a stale copy of it is not.


def _read_reset_debounce_state(conn, account_key):
    """Return the armed debounce state for ``account_key``, or None.

    The tuple is ``(week_start_date, week_end_at, baseline_pct,
    first_zero_at_utc, first_zero_observation_id)``. A missing table is
    reported as "not armed" rather than raised: the detector must never crash
    a recording tick over its own debounce bookkeeping, and an index that
    predates epoch 1013 is about to be rebuilt anyway.
    """
    try:
        row = conn.execute(
            "SELECT week_start_date, week_end_at, baseline_pct, "
            "       first_zero_at_utc, first_zero_observation_id "
            "FROM weekly_reset_debounce_state WHERE account_key = ?",
            (account_key,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return (row[0], row[1], float(row[2]), row[3], row[4])


def _arm_reset_debounce_state(conn, account_key, *, week_start_date,
                              week_end_at, baseline_pct, first_zero_at_utc,
                              first_zero_observation_id):
    """Upsert the pending reset-to-zero candidate for one account.

    ``first_zero_at_utc`` MUST be the capture clock of the observation that
    armed it, because it becomes the reset's effective instant on confirm.
    ``first_zero_observation_id`` is that observation's raw journal id; the
    confirm leg stores it on the event, and the self-confirmation rule
    compares against it.
    """
    conn.execute(
        "INSERT INTO weekly_reset_debounce_state "
        "(account_key, week_start_date, week_end_at, baseline_pct, "
        " first_zero_at_utc, first_zero_observation_id) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_key) DO UPDATE SET "
        " week_start_date = excluded.week_start_date, "
        " week_end_at = excluded.week_end_at, "
        " baseline_pct = excluded.baseline_pct, "
        " first_zero_at_utc = excluded.first_zero_at_utc, "
        " first_zero_observation_id = excluded.first_zero_observation_id",
        (account_key, week_start_date, week_end_at, float(baseline_pct),
         first_zero_at_utc, first_zero_observation_id),
    )


def _clear_reset_debounce_state(conn, account_key):
    """Drop the pending candidate for one account."""
    conn.execute(
        "DELETE FROM weekly_reset_debounce_state WHERE account_key = ?",
        (account_key,),
    )


# ── Source-local 5h credit confirmation state (#769 S2 §3, issue #751) ─────
# Five-hour credit detection compared an incoming reading against the latest
# accepted snapshot whatever contributor produced it. On 2026-09-04 one stale
# `source=statusline` sample of 7% arrived between `source=api` readings of
# 12%, and a `five_hour_credit` was minted in the same instant with no
# confirmation from any source. The state below makes each contributor carry
# its own baseline and its own pending descent, so only a later, distinct
# observation from the SAME source can confirm that source's descent.

#: The bucket an observation with no `payload.source` is filed under. Legacy
#: and direct callers reach `detect_reset_and_credit` without one; filing them
#: together keeps the rule intact for them (a descent one such caller reported
#: still needs a second such observation) rather than exempting them from it.
FIVE_HOUR_UNKNOWN_SOURCE = "unknown"


def _read_five_hour_source_state(conn, account_key, five_hour_window_key,
                                 source):
    """Return one contributor's five-hour state, or None when it has none.

    The tuple is ``(baseline_pct, pending_low_pct, pending_at_utc,
    pending_observation_id)``; ``pending_low_pct is not None`` is the armed
    predicate. A missing table is reported as "no state" rather than raised,
    for the same reason the weekly debounce reader does it: the detector must
    never crash a recording tick over its own bookkeeping.

    COLD START — an admitted detection gap, stated here rather than left to be
    found later. This is disposable operational state: an epoch transition or
    any `db rebuild` publishes a fresh index that carries none of these rows,
    so the first observation from each source after a rebuild establishes a
    baseline and emits nothing. Two consequences follow, and neither is a
    fabrication:

      * A rebuild between a source's baseline and its descent loses the
        baseline, so `API 12 -> rebuild -> API 7 -> API 8` initializes the API
        baseline at 7 and never sees the descent. The credit is missed for that
        window.
      * A rebuild between arming and confirmation loses the pending descent, so
        the confirming observation arrives against a baseline re-established at
        the descent's own value and confirms nothing.

    The gap is admitted rather than closed because the evidence needed to close
    it is not reachable here. The only retained per-source five-hour percent
    inside this transaction is `weekly_usage_snapshots.five_hour_percent`,
    which `_usage_snapshot_fold_decision` MAX-clamps UP at write time — so the
    descent's raw low value is not in the index at all, and a baseline read
    back from it can exceed the raw one and manufacture a drop that never
    happened, which is the exact fabrication class this state exists to remove.
    The raw values do survive in the append-only journal, but the rebuild drops
    Claude observations once they have fed the account accumulator
    (`docs/journal-gotchas.md`), and reconstructing per-source state from them
    would require deriving the canonical window key per observation, which is a
    database-backed derivation the rebuild pass exists to avoid.

    The cost is bounded: only the window a rebuild lands inside is affected,
    and a later same-source descent inside a later window detects normally.
    """
    try:
        row = conn.execute(
            "SELECT baseline_pct, pending_low_pct, pending_at_utc, "
            "       pending_observation_id "
            "  FROM five_hour_credit_confirmation_state "
            " WHERE account_key = ? AND five_hour_window_key = ? "
            "   AND source = ?",
            (account_key, int(five_hour_window_key), source),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return (
        float(row[0]),
        None if row[1] is None else float(row[1]),
        row[2],
        row[3],
    )


def _write_five_hour_source_state(conn, account_key, five_hour_window_key,
                                  source, *, baseline_pct,
                                  pending_low_pct=None, pending_at_utc=None,
                                  pending_observation_id=None):
    """Upsert one contributor's state for one window, then retire that
    contributor's rows for earlier windows.

    The three ``pending_*`` values are written together on every call, so
    cancelling or confirming a descent is the same statement as arming one and
    a stale pending value can never survive a state change.

    ``pending_at_utc`` is the arming tick's DETECTION clock, which is what the
    five-hour path has always stamped into `effective_reset_at_utc`
    (`_floor_to_ten_minutes(now_utc)`); the weekly path anchors on the capture
    stamp instead (#750 S3 §1.2) and the two conventions are deliberately not
    merged here. In production they are the same instant and they differ only
    under `CCTALLY_AS_OF`.

    The trailing DELETE keeps the table proportional to live state rather than
    to history. Window keys advance monotonically through the journal, so a
    row for an earlier window of the same contributor can no longer be reached
    by detection; without this the table would grow by one row per contributor
    per five-hour window forever inside a live index.
    """
    conn.execute(
        "INSERT INTO five_hour_credit_confirmation_state "
        "(account_key, five_hour_window_key, source, baseline_pct, "
        " pending_low_pct, pending_at_utc, pending_observation_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_key, five_hour_window_key, source) DO UPDATE SET "
        " baseline_pct = excluded.baseline_pct, "
        " pending_low_pct = excluded.pending_low_pct, "
        " pending_at_utc = excluded.pending_at_utc, "
        " pending_observation_id = excluded.pending_observation_id",
        (account_key, int(five_hour_window_key), source, float(baseline_pct),
         None if pending_low_pct is None else float(pending_low_pct),
         pending_at_utc, pending_observation_id),
    )
    conn.execute(
        "DELETE FROM five_hour_credit_confirmation_state "
        " WHERE account_key = ? AND source = ? AND five_hour_window_key < ?",
        (account_key, source, int(five_hour_window_key)),
    )


def _step_five_hour_source_state(conn, account_key, five_hour_window_key,
                                 source, *, new_pct, observation_id,
                                 drop_threshold, window_live, now_iso):
    """Advance one contributor's five-hour state by one observation.

    Returns ``(decision, prior_state)``, where ``prior_state`` is the row as it
    stood BEFORE this observation — the confirming leg needs the arming
    instant it holds.

    Every action except FIVE_HOUR_CONFIRM is persisted here. The confirm write
    is deliberately left to the caller and made only after the credit's pivots
    complete, mirroring the weekly CONFIRM leg's P2a ordering: a mid-fire raise
    must leave the state armed so the next same-source reading below the
    baseline re-confirms and re-runs the idempotent pivots, rather than losing
    the credit outright.
    """
    prior_state = _read_five_hour_source_state(
        conn, account_key, five_hour_window_key, source)
    decision = plan_five_hour_source_local_credit(
        baseline_pct=None if prior_state is None else prior_state[0],
        pending_low_pct=None if prior_state is None else prior_state[1],
        pending_observation_id=None if prior_state is None else prior_state[3],
        new_pct=new_pct,
        observation_id=observation_id,
        drop_threshold=drop_threshold,
        window_live=window_live,
    )
    if decision.action == FIVE_HOUR_ARM:
        # The descent is recorded and nothing is emitted. The write clamp
        # raises this reading back to the baseline in
        # `weekly_usage_snapshots`, so this row is the only trace the descent
        # leaves — the same reason the weekly reset-to-zero debounce needs a
        # state row of its own.
        _write_five_hour_source_state(
            conn, account_key, five_hour_window_key, source,
            baseline_pct=decision.baseline_pct,
            pending_low_pct=new_pct,
            pending_at_utc=now_iso,
            pending_observation_id=observation_id,
        )
    elif decision.action in (FIVE_HOUR_SET_BASELINE, FIVE_HOUR_CANCEL):
        # Establish, raise, or cancel. All three write the baseline with the
        # three pending columns NULL, so a cancelled candidate cannot linger.
        _write_five_hour_source_state(
            conn, account_key, five_hour_window_key, source,
            baseline_pct=decision.baseline_pct,
        )
    # FIVE_HOUR_HOLD writes nothing: either the arming observation replayed
    # and must leave the state armed, or the reading neither raises the
    # baseline nor is eligible to arm.
    return decision, prior_state


# ``CreditPlan`` / ``_parse_credit_at`` / ``_build_credit_plan`` now live in
# ``bin/_lib_credit.py`` (#279 S4 F1); re-imported at module top so the
# ``bin/cctally`` re-exports and this module's own callers
# (``cmd_record_credit``) resolve them unchanged.


#: The stale-replica band, as one text. Five sites described the same rows —
#: `_fire_in_place_credit`'s winner capture, its refused-insert recovery
#: capture, the DELETE both of them describe, and `_apply_credit`'s manual
#: DELETE and ingest capture — and they drifted apart once already. Since
#: #834 S1 (#835) the only consumer is `_doomed_snapshot_rows`. The parameters
#: are, in order, `week_start_date`, `account_key`, the credit's effective
#: instant, and the pre-credit baseline.
#:
#: `{cmp}` is the band comparison, and the two spellings below are a deliberate
#: divergence rather than an oversight — `_doomed_snapshot_rows` documents why.
#:
#: `weekly_observation_held = 0` (#834 S1, #835): a held row is the ONLY carrier
#: of its tick's five-hour reading, which is why #824 writes it, so removing it
#: destroys that reading. The weekly value it carries IS retired by the credit,
#: and the read that consumes a weekly value is what became credit-aware —
#: `_latest_seven_day_and_window` in bin/_cctally_five_hour.py.
#:
#: The held predicate is NOT written in here. It is appended by
#: `_cctally_core.weekly_held_exclusion`, which omits it on a store that predates
#: the column — see `_stale_replica_band_sql`.
_STALE_REPLICA_BAND_TEMPLATE = (
    "WHERE week_start_date = ? AND account_key = ? "
    "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
    "  AND ABS(weekly_percent - ?) {cmp} 1.0"
    "{held}"
)


def _stale_replica_band_sql(conn, *, manual):
    """The stale-replica band for ``conn``, as one text.

    #834 S1 (#835) Gate A R4. This was two module-level constants, and both
    referenced `weekly_observation_held` unconditionally — so every site that used
    them raised ``sqlite3.OperationalError: no such column:
    weekly_observation_held`` on a store predating epoch 1015. Fifteen test modules
    and six fixture builders create `weekly_usage_snapshots` without the column,
    and `_cctally_core.weekly_held_exclusion` exists for exactly this predicate:
    omitting it where the column is absent is sound, because such a store cannot
    contain a held row, so the filtered and unfiltered populations are identical.

    A builder rather than a constant, because the predicate now depends on the
    connection. Both properties the constants were introduced for are kept: ONE
    template, so five sites cannot drift apart again, and the two comparisons stay
    distinct — ``manual=False`` is the automatic path's INCLUSIVE ``<= 1.0``,
    ``manual=True`` is `record-credit`'s STRICT ``< 1.0``, and
    `_doomed_snapshot_rows` states why that divergence is deliberate."""
    return _STALE_REPLICA_BAND_TEMPLATE.format(
        cmp="<" if manual else "<=",
        held=_cctally_core.weekly_held_exclusion(conn, prefix="   AND "),
    )


def _doomed_snapshot_rows(conn, *, week_start_date, account_key, effective_iso,
                          pre_credit, manual, id_base=None):
    """Classify the doomed stale-replica snapshots and return
    ``(delete_ids, suppression_journal_ids)``.

    #834 S1 (#835). THE JOURNAL SUPPRESSION LIST IS THE ONLY PRODUCTION OUTPUT.
    Every call site discards the first element (`_, doomed_supp = ...`, three
    sites in this module), because Gate A R7 replaced the classify-then-delete-by-
    id removal with the single-statement `_delete_doomed_snapshot_rows`.

    ``delete_ids`` — every row in the band, journalled or not, ordered by ``id``.
    It EXISTS FOR THE TESTS, not for a caller (Gate A S3): it is the band's
    selection made observable, which is what
    `test_835_r4_the_doomed_classifier_runs_on_a_pre_column_store` asserts so that
    a band silently matching nothing cannot pass. It is deliberately NOT what the
    set relation below is measured against — a test comparing two projections of
    this one function would stay green through a change that made the DELETE's
    band narrower than this one's, so
    `test_835_doomed_classifier_projections_agree_on_the_required_relation` diffs
    the table around a real `_delete_doomed_snapshot_rows` call instead.

    THE REQUIREMENT BETWEEN THE SUPPRESSION LIST AND THE LIVE DELETE IS A SET
    RELATION, not textual equality of two predicates. Revision 1 of this session's
    specification asked the normalized capture and DELETE SQL to compare equal;
    that is wrong in both directions, because the two deliberately select
    different populations, and normalizing the differences away would have proved
    only that shared text had been copied. An un-journalled poisoned row has no
    logical id for an applier to name, but it still holds the live seven-day
    surfaces at the pre-credit percentage, so the DELETE must remove it.

    ``suppression_journal_ids`` — only the rows a journal applier can name:
    non-NULL ``journal_id``, canonicalized with ``sorted(set(...))`` so one
    operation can never emit two payloads under one id (#761 residual 1), and
    with the current operation's own ``sa:<id_base>:syn:%`` family excluded when
    ``id_base`` is given. That exclusion is by deterministic id rather than by
    emission timing: a sub-1.0pp credit is legal and places the new synthetic
    (at ``to_pct``) inside a band centred on ``from_pct``, and under a crash
    between event fsync and COMMIT the next cycle replays
    ``sa:<id_base>:syn:0`` before the credit re-runs — so a timing-only
    exclusion would name the very row the operation must preserve. Excluding
    the whole prefix makes the list a PURE FUNCTION of the operation.

    ``manual`` picks the band comparison, and the divergence is intentional.
    The automatic path (``manual=False``) uses the INCLUSIVE ``<= 1.0`` because
    it compares an armed marker's baseline against whatever the status line last
    wrote — two different quantities that can sit exactly 1.0pp apart, which is
    how the 2026-09-01 replica survived a strict band. ``record-credit``
    (``manual=True``) keeps the strict ``< 1.0`` because it compares against the
    level the operator asserted, so the two quantities are one.

    Held rows are in NEITHER projection: see `_stale_replica_band_sql`, which is
    also where the held predicate's pre-column tolerance lives.

    ``account_key`` is mandatory. `_count_stale_replays` is therefore NOT routed
    through here — it previews the same DELETE but stays account-blind until
    #837 scopes the floor and high-water mark it is computed against."""
    band = _stale_replica_band_sql(conn, manual=manual)
    rows = conn.execute(
        "SELECT id, journal_id FROM weekly_usage_snapshots " + band
        + " ORDER BY id",
        (week_start_date, account_key, effective_iso, float(pre_credit)),
    ).fetchall()
    delete_ids = [int(r[0]) for r in rows]
    # The exclusion is CASE-INSENSITIVE, because the SQL it replaced was: SQLite's
    # `LIKE` folds ASCII case, and a bare `str.startswith` does not (Gate A R7
    # item 4). The two are NOT equivalent in general, and the divergence runs the
    # other way: `str.lower()` folds non-ASCII where `LIKE` folds ASCII only, so
    # this form excludes strictly more than the SQL did. That cannot matter here,
    # because a `journal_id` in this family is `sa:o:<hex>:syn:<n>` and holds no
    # character outside `[0-9a-f]` in the folded part. `id_base` likewise carries
    # no `LIKE` metacharacter (`%` or `_`), so the `startswith` form is strictly
    # safer than interpolating it into a pattern.
    #
    # Neither spelling is reachable today — the sole caller passes
    # `id_base=rec["id"]`, a content digest compared against a `journal_id` built
    # from that same digest in the same process — so this preserves a semantics
    # nothing currently depends on, rather than fixing an observed defect. It is
    # preserved anyway because a silent change inside a journal suppression list
    # can become reachable later without anyone noticing.
    #
    # `id_base=None` applies NO exclusion, which is also what the SQL it replaced
    # did at the site that ran with no `id_base`: the automatic path's old capture
    # carried no own-synthetic clause at all. The manual ingest capture, the only
    # site whose old SQL had the clause, always supplies one.
    own_synthetic_prefix = (
        None if id_base is None else f"sa:{id_base}:syn:".lower())
    suppression = {
        str(r[1]) for r in rows
        if r[1] is not None
        and (own_synthetic_prefix is None
             or not str(r[1]).lower().startswith(own_synthetic_prefix))
    }
    return delete_ids, sorted(suppression)


def _delete_doomed_snapshot_rows(conn, *, week_start_date, account_key,
                                 effective_iso, pre_credit, manual):
    """Remove every stale-replica snapshot in the band, in ONE statement.

    #834 S1 (#835) Gate A R7 item 1. The removal used to classify ids and then
    delete by id, which evaluated the band at CLASSIFICATION time rather than at
    deletion time: a row entering the band between the two escaped a deletion the
    single predicate statement it replaced would have performed, and on the
    automatic path a `conn.commit()` runs inside that region. The id list also
    bound the statement's parameter count to the size of the doomed set — the
    largest `(week, account, integer percent)` group on the live store holds 811
    rows, and an older SQLite caps a statement at 999 variables — while this form
    takes four parameters whatever the population.

    `_doomed_snapshot_rows` remains the source of the JOURNAL SUPPRESSION
    projection, and the set relation between that projection and the rows this
    removes is now true by construction rather than by mechanism: both apply the
    same band. The relation is still asserted as a test, which is where it
    belongs."""
    return conn.execute(
        "DELETE FROM weekly_usage_snapshots "
        + _stale_replica_band_sql(conn, manual=manual),
        (week_start_date, account_key, effective_iso, float(pre_credit)),
    ).rowcount


def _credit_retirement_bands(conn, *, week_start_date, week_start_at,
                             week_end_at, account_key):
    """Every weekly value one week's credits RETIRED, as read-side bands.

    #834 S1 (#835). `_doomed_snapshot_rows` names the rows a firing credit is
    about to remove; this names, for a week that has already been credited, the
    (floor, retired value) pairs a stored row can still match. It exists because
    #835 preserves held rows, so a row carrying a retired weekly value can now
    outlive the credit that retired it and a read must recognize it.

    The two legs are scoped exactly the way `_reset_aware_floor`
    (bin/_cctally_core.py) scopes them, and the two must stay in step: a
    `week_reset_events` row counts iff its `effective_reset_at_utc` falls in
    ``[week_start_at, week_end_at)``, a `weekly_credit_floors` row counts iff its
    `week_start_date` matches. `_reset_aware_floor` takes the LATEST of the two
    legs because it answers "from when may a capture be read"; this returns ALL
    of them, because every credit in the week retired its own value and a held
    row written after an early credit still carries that early credit's value.

    ``account_key`` is mandatory and is NOT defaulted: a credit under one account
    retires nothing under another. A merged read passes each row's OWN account.

    A row with a NULL `observed_pre_credit_pct` yields no band — there is no
    value to compare against, which is the state a legacy pre-#45 event is in.
    Absent `week_start_at` / `week_end_at` on the calling row yields no
    `week_reset_events` band for the same reason: the leg cannot be scoped
    without the week's bounds.

    A missing table yields no band from that leg, which is correct rather than a
    degradation: a store with no `weekly_credit_floors` cannot hold a manual
    credit, and a store with no `week_reset_events` cannot hold an automatic one.
    """
    bands: list = []
    if week_start_at is not None and week_end_at is not None:
        try:
            rows = conn.execute(
                "SELECT effective_reset_at_utc, observed_pre_credit_pct "
                "  FROM week_reset_events "
                " WHERE unixepoch(effective_reset_at_utc) >= unixepoch(?) "
                "   AND unixepoch(effective_reset_at_utc) <  unixepoch(?) "
                "   AND account_key = ? "
                "   AND observed_pre_credit_pct IS NOT NULL",
                (week_start_at, week_end_at, account_key),
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        for floor_at, retired in rows:
            # The automatic path's band is INCLUSIVE.
            bands.append((str(floor_at), float(retired), True))
    try:
        rows = conn.execute(
            "SELECT effective_at_utc, observed_pre_credit_pct "
            "  FROM weekly_credit_floors "
            " WHERE week_start_date = ? AND account_key = ? "
            "   AND observed_pre_credit_pct IS NOT NULL",
            (week_start_date, account_key),
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
    for floor_at, retired in rows:
        # `record-credit`'s band is STRICT.
        bands.append((str(floor_at), float(retired), False))
    return bands


def _latest_credit_floor_instant(bands):
    """The LATEST floor instant in ``bands``, as an epoch float, or ``None`` when
    ``bands`` is empty or no floor in it parses.

    #834 S1 (#835). `_credit_retirement_bands` returns every credit in the week
    because each retired its own value; a reader also needs the single boundary
    before which NO stored row can state the week's current effective weekly
    value, and that boundary is the latest of those floors. It is exposed here
    rather than re-derived at each call site so the floor and the bands cannot
    drift apart, and so a caller cannot accidentally take the FIRST floor, which
    would admit rows an intervening credit already invalidated.

    An unparseable floor contributes nothing, matching
    `_weekly_value_is_retired_replica`, which skips one for the same reason."""
    latest = None
    for floor_at, _retired, _inclusive in bands:
        try:
            moment = parse_iso_datetime(floor_at, "credit_floor").timestamp()
        except (ValueError, TypeError):
            continue
        if latest is None or moment > latest:
            latest = moment
    return latest


def _weekly_value_is_retired_replica(bands, *, captured_at_utc, weekly_percent):
    """True when a stored weekly value is one a credit in `bands` retired.

    #834 S1 (#835). The same band and floor predicate the stale-replica DELETE
    applies, applied as a READ filter instead of as a deletion: captured at or
    after the credit's floor, and within 1.0 point of the value that credit
    retired.

    Instants are compared as PARSED moments rather than as text, because the two
    sides carry mixed offset spellings (`Z` on a capture stamp, `+00:00` on a
    floor) — the same reason every SQL site wraps both sides in `unixepoch()`.

    AN UNPARSEABLE CAPTURE STAMP IS TREATED AS NOT RETIRED HERE, and what that
    means for the read depends on the CALLER. There are three, and the floor bound
    belongs to exactly one of them.

    `_latest_seven_day_and_window` (bin/_cctally_five_hour.py) carries a floor bound
    ahead of this predicate, added in Gate A R1. That bound treats an unparseable
    stamp inside a credited week as pre-floor and ends the walk with `None`, which
    is the right disposition: a row that cannot be placed relative to the floor
    cannot be certified as able to state the week's current effective value. So
    there, a malformed row blanks the read.

    `_load_five_hour_milestones` (bin/_cctally_five_hour.py, from tranche A) carries
    no such bound. An unparseable `snapshot_captured_at_utc` makes this function
    return `False` and the joined effective crossing value RENDERS. A correction of
    record: an earlier version of this docstring generalized R1's floor bound to
    every caller, and it holds for one.

    `_credit_aware_block_weekly_axes` (bin/_cctally_five_hour.py) carries no floor
    bound either, but since Gate A R8 an unparseable instant never reaches this
    predicate from it: `_block_weekly_axis_weeks` parses every axis instant first —
    including `block_start_at`, which the start axis falls back to — and returns an
    EMPTY TUPLE when it cannot, which resolves no bands. (It returned `None` when
    this paragraph was written; R8 made it return a tuple of candidate weeks and R9
    added a third resolution pass, and both forms are falsy, so the caller's `if not
    week_ids` guard is unaffected. The stated value was simply stale.) The value
    renders for that reason rather than this one. Same outcome, different mechanism,
    and the mechanism matters because only one of the two is a deliberate decision.

    What this function's own tolerance buys in every case is that a malformed stamp
    is never MISclassified as retired, which is what keeps the uncredited-week path
    byte-stable."""
    if weekly_percent is None or not bands:
        return False
    try:
        captured = parse_iso_datetime(captured_at_utc, "captured_at").timestamp()
    except (ValueError, TypeError):
        return False
    for floor_at, retired, inclusive in bands:
        try:
            floor = parse_iso_datetime(floor_at, "credit_floor").timestamp()
        except (ValueError, TypeError):
            continue
        if captured < floor:
            continue
        delta = abs(float(weekly_percent) - retired)
        # The band's comparison belongs to the credit that set it: the automatic
        # path is inclusive, `record-credit` is strict. See
        # `_doomed_snapshot_rows` for why those two differ.
        if (delta <= 1.0) if inclusive else (delta < 1.0):
            return True
    return False


def _fire_in_place_credit(conn, week_start_date, cur_end_canon, weekly_percent,
                          *, observed_pre_credit_pct, effective_dt,
                          as_of=None, commit=True, ctx=None,
                          account_key=_lib_accounts.UNATTRIBUTED,
                          origin_observation_id=None):
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
    passed AND this call is the genuine-new-reset winner (the
    ``week_reset_events`` INSERT rowcount == 1), the doomed stale-replica
    snapshots' ``journal_id``s are captured (SAME predicate as the pivot-2
    DELETE) into ``ctx.suppression_map`` keyed on the ``wr`` harvest natural
    key that ``week_reset_identity_parts`` builds, BEFORE the DELETE runs, so
    ``_build_harvest_evt`` attaches the list to the ``wr`` evt and the
    destructive effect replays. ``ctx=None`` (legacy) captures nothing.

    Side-effect ordering is load-bearing: the event-row INSERT is dedup-gated
    (by the epoch-1013 partial unique indexes), but the hwm force-write and
    stale-replica DELETE run UNCONDITIONALLY — a prior run may have committed
    the event then died before the pivots (memory:
    project_dedup_must_not_gate_side_effects). The pivots are individually
    idempotent (file overwrite + DELETE on a stable predicate).

    #750 S3 §1.5: when the INSERT is refused, no ``wr`` evt exists to carry
    the DELETE, so on the ingest path the removal is journalled as its own
    ``weekly_credit_effects`` event instead. Without it a rebuild re-folds the
    poisoned `snapshot_accept` and resurrects the row this pass removed.

    ``effective_dt`` is the (already-resolved) reset moment, recorded to the
    EXACT UTC second (#750 S3 §1.4). The immediate path passes the triggering
    observation's payload capture; the debounced path passes the first-zero
    capture instant the debounce state recorded. Hour flooring is gone: it
    back-dated the event before observations that were still legitimately
    pre-credit, which is how the 2026-09-01 stale replica survived the DELETE
    below and then seeded a fresh milestone epoch.

    ``origin_observation_id`` (#750 S3 §1.2) is the raw journal id of the
    observation this reset is derived from — the triggering observation on the
    immediate leg, the FIRST-ZERO observation on the confirm leg. It is the
    row's identity under the epoch-1013 partial unique index and, through
    ``week_reset_identity_parts``, the journal's identity for the event too.
    ``None`` keeps the legacy tuple identity, which is what a caller with no
    journal line gets."""
    effective_iso = effective_dt.isoformat(timespec="seconds")
    # #750 S3 §1.5/§1.8. Deduplication is EXACT-ORIGIN ONLY, and it is the
    # epoch-1013 partial unique indexes that perform it: a row naming an origin
    # is unique on `(account_key, origin_observation_id)`, an origin-null row
    # keeps the legacy `(account_key, old_week_end_at, new_week_end_at)` tuple.
    # `INSERT OR IGNORE` is therefore the whole mechanism, and there is no
    # pre-check. The old one refused ANY second event sharing a
    # `new_week_end_at`, which made a genuine second in-place credit in one
    # week unreachable (#732); the evidence-based echo guard that briefly
    # replaced it was withdrawn because its epoch maximum was computed over a
    # window excluding the predecessor reading, so it suppressed the ordinary
    # climb-back-and-credit-again sequence rather than only the stale echo.
    # A distinct observation reporting stale data is admitted, deliberately:
    # a phantom event is visible in the week's segmentation, whereas a
    # suppressed genuine reset does not self-heal.
    #
    # Row shape: old=effective_iso, new=cur_end_canon (DISTINCT) so only
    # post_map fires on the credited week in _apply_reset_events_to_weekrefs
    # (old==new collapses it to a zero-width window). observed_pre_credit_pct
    # stamps the pre-credit baseline (issue #45).
    ins_wr = conn.execute(
        "INSERT OR IGNORE INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " origin_observation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (as_of or now_utc_iso(), effective_iso, cur_end_canon,
         effective_iso, float(observed_pre_credit_pct), account_key,
         origin_observation_id),
    )
    # Design B (§5.3 event+effects): on the ingest path, capture the doomed
    # stale-replica snapshots' journal_ids BEFORE the pivot-2 DELETE (SAME
    # predicate), keyed on the wr harvest natural key. Gated on the
    # genuine-new-reset winner (rowcount == 1); a refused insert has no evt of
    # its own to attach them to and journals them through the recovery event
    # below instead. ctx=None (legacy) captures nothing.
    if ctx is not None and ins_wr.rowcount == 1:
        # Through the namespace (#834 S1 Gate A R7 item 2), because
        # `bin/cctally`'s re-export comment says both credit paths reach the
        # classifier that way for test monkeypatching. Before this it said so
        # while this function called the module-local symbol, so a test patching
        # the namespace silently failed to intercept the automatic path.
        _, doomed_supp = _cctally()._doomed_snapshot_rows(
            conn, week_start_date=week_start_date, account_key=account_key,
            effective_iso=effective_iso,
            pre_credit=float(observed_pre_credit_pct), manual=False)
        # #750 S3 §1.1: the map key comes from the SAME helper that builds
        # the harvest id, because the wr identity is dual-shaped and a key
        # computed here by hand would stop matching the moment a row
        # carries an origin. #341 put account_key first; the helper keeps
        # it there for both shapes.
        import _lib_journal as _lj
        ctx.suppression_map[_lj.week_reset_identity_parts(
            account_key, effective_iso, cur_end_canon,
            origin_observation_id,
        )] = doomed_supp
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
    # land captured_at >= effective with pct ~= baseline and dominate the
    # reset-aware clamp). 1.0pp tolerance band absorbs rounding drift; both
    # sides wrapped in unixepoch() for offset robustness.
    #
    # The bound is INCLUSIVE. On the debounced leg `observed_pre_credit_pct` is
    # the armed marker's baseline, while the rows being removed hold whatever
    # the status line last wrote — two different quantities, which is exactly
    # the drift this band exists to absorb. A strict `<` excludes the drift
    # bound itself, so on 2026-09-01 a baseline of 14.0 against stored replays
    # of 13.0 matched nothing, the stale rows survived, and they provoked a
    # second phantom credit. The manual path keeps `<`: it compares against the
    # level the operator asserted, so the two quantities are one.
    # #750 S3 §1.5: a DELETE that no `wr` evt carries has to be journalled on
    # its own, because the poisoned snapshot is itself a retained
    # `snapshot_accept` event and a rebuild or a `db rederive` would re-fold it
    # and resurrect exactly the row this pass removed. That case is the refused
    # insert: the winner's doomed ids ride its own harvested evt through
    # `ctx.suppression_map` above, and a refused insert has no evt to ride. The
    # recovery rides the existing effects-only `weekly_credit_effects` family —
    # the same applier, deleting by logical id and force-writing the same floor
    # — under an id keyed on the originating observation, which is the identity
    # the refused insert collided on and is therefore stable across replays of
    # that same observation.
    recovery_ctx = (
        ctx if (ctx is not None and ins_wr.rowcount != 1
                and origin_observation_id) else None
    )
    doomed_ids = []
    try:
        # One band, two consumers (#834 S1, #835). The recovery event takes the
        # SUPPRESSION projection, which can only name journalled rows; the removal
        # applies the BAND directly, so it also removes un-journalled poisoned
        # rows — such a row has no logical id for an applier to name, but it still
        # holds the live 7d surfaces at the pre-credit percentage. See
        # `_doomed_snapshot_rows` for why the two are a set relation rather than
        # two predicates required to compare equal, and
        # `_delete_doomed_snapshot_rows` for why the removal is one statement
        # rather than a captured id list.
        _c = _cctally()
        _, doomed_supp = _c._doomed_snapshot_rows(
            conn, week_start_date=week_start_date, account_key=account_key,
            effective_iso=effective_iso,
            pre_credit=float(observed_pre_credit_pct), manual=False)
        if recovery_ctx is not None:
            doomed_ids = doomed_supp
        _c._delete_doomed_snapshot_rows(
            conn, week_start_date=week_start_date, account_key=account_key,
            effective_iso=effective_iso,
            pre_credit=float(observed_pre_credit_pct), manual=False)
    except sqlite3.DatabaseError as exc:
        eprint(f"[record-usage] post-credit cleanup failed: {exc}")
        return
    # Deliberately OUTSIDE the handler above. Swallowing a failure here would
    # leave the DELETE standing as an inline-only effect, which is the exact
    # degradation this event exists to prevent, and the caller would be told
    # nothing. On the ingest path the raise aborts the cycle, so the DELETE
    # rolls back with it and the next pass retries both together.
    if recovery_ctx is not None:
        import _cctally_journal as _jr
        import _lib_journal as _lj
        recovery_payload = {
            "suppression": doomed_ids,
            "suppression_table": "weekly_usage_snapshots",
            "hwm_floor": {
                "week_start_date": week_start_date,
                "weekly_percent": weekly_percent,
            },
        }
        # The id names the originating observation AND digests the payload.
        # The origin alone is not enough: `doomed_ids` is a live query, so one
        # observation refused twice against different poisoned rows would emit
        # two different payloads under one id, and
        # `_classify_live_effective_event` withholds the second as a conflict —
        # leaving its DELETE as the inline-only effect this event exists to
        # replace, because `_converge_row_from_effective` drops an effects-only
        # family and `emit_model_a` then returns normally. With the digest each
        # distinct removal is its own event, while a genuine replay of one
        # observation against one poisoned set reproduces the line byte for
        # byte and collapses to the duplicate the journal already tolerates.
        #
        # `at` is the credit's effective instant, NOT the calling tick's clock,
        # and that is what makes "byte for byte" true rather than approximately
        # true: `at` is outside the id but inside the line's content hash, so a
        # wall clock there would make two replays of one observation a
        # same-revision CONFLICT even with identical payloads. On the debounce
        # confirm leg the two genuinely differ — the id names the first-zero
        # observation while `as_of` is the confirming tick — so the wall clock
        # was not merely imprecise there. `effective_iso` is a function of the
        # same observation the id names, on both legs.
        _jr.emit_model_a(
            recovery_ctx,
            kind="weekly_credit_effects",
            evt_id=_lj.evt_id(
                "wce", "replay", origin_observation_id,
                _lj.effects_payload_digest(recovery_payload)),
            table=None,
            columns=recovery_payload,
            at=effective_iso.replace("+00:00", "Z"),
        )
    if commit:
        try:
            conn.commit()
        except sqlite3.DatabaseError as exc:
            eprint(f"[record-usage] post-credit cleanup failed: {exc}")


def detect_reset_and_credit(conn, *, week_start_date, week_end_at,
                            weekly_percent, five_hour_window_key,
                            five_hour_percent, as_of=None, commit=True,
                            ctx=None, account_key=_lib_accounts.UNATTRIBUTED,
                            origin_observation_id=None, capture_at=None,
                            source=None, hold_weekly_axis=False):
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
    - ``origin_observation_id`` (#750 S3, default ``None``): the raw journal
      id of the observation this detection is derived from. The debounce ARM
      records it, and the self-confirmation rule compares an incoming
      observation against the recorded one so a byte-identical replay of the
      first zero cannot confirm itself. ``None`` (a caller with no journal
      line, such as a direct legacy invocation) disables that comparison and
      arms with no recorded origin, which is the pre-#750 behaviour.
    - ``capture_at`` (#750 S3 §1.2, default ``None``): the observation's
      payload CAPTURE stamp, which is what every ``effective_reset_at_utc``
      written below records. It is deliberately separate from ``as_of``: the
      journal distinguishes the detection clock from the capture stamp, they
      are equal in production and differ under ``CCTALLY_AS_OF``, and mixing
      them would make live detection and the backfill disagree about the same
      physical reset. ``None`` falls back to the detection clock.
    - ``source`` (#769 S2 §3, default ``None``): the observation's
      ``payload.source`` — ``api`` or ``statusline`` in production. It is the
      contributor discriminator for the five-hour confirmation state, and it is
      deliberately NOT the journal line's ``src``, which every rate-limit
      observation carries identically as ``record-usage``. ``None`` is filed
      under ``FIVE_HOUR_UNKNOWN_SOURCE``, which keeps the same-source rule in
      force for such a caller rather than exempting it. Only a DIRECT caller
      reaches that bucket: the production ingest caller
      (``_pipeline_claude_usage``) resolves a missing or non-string
      ``payload.source`` to ``statusline`` before calling, and passes the same
      resolved value to the snapshot row's ``source`` column, so the two record
      one value. The weekly branch does not read it: extending source-local
      confirmation to the weekly path would delay the immediate 63-to-0 reset
      that #755 exists to stop delaying.
    - ``hold_weekly_axis`` (default ``False``): scratch replay's explicit
      reviewed observation hold. It bypasses weekly reset/credit detection
      while still running the independent five-hour detector below. Live
      callers retain the default.
    """
    c = _cctally()
    now_utc = _as_of_or_command(as_of)
    # The CAPTURE clock, used only for the reset instant. `now_utc` stays the
    # causal detection clock (window comparisons, `detected_at_utc`).
    capture_dt = (
        parse_iso_datetime(capture_at, "record.capture_at")
        .astimezone(dt.timezone.utc)
        if capture_at else now_utc
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
        # `weekly_observation_held = 0` (#769 S11, #824): a weekly predecessor.
        # The pipeline copies a held row's boundary and weekly value from its
        # basis, so on a row this binary wrote the two agree and the predicate
        # changes no answer. That is exactly why it is here: it makes the
        # copy's correctness NOT load-bearing, so a row whose boundary diverged
        # — hand-written, or produced by an older shape — cannot reclassify a
        # later reset as an in-place credit (review finding 7).
        prior = conn.execute(
            "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots "
            "WHERE week_end_at IS NOT NULL AND account_key = ? "
            "  AND weekly_observation_held = 0 "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (account_key,),
        ).fetchone()
        if not hold_weekly_axis and prior and prior["week_end_at"] and cur_end_canon:
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
                    # #750 S3 §1.4: the EXACT capture second. Flooring to the
                    # hour was a display convenience that back-dated the
                    # event before observations still legitimately belonging
                    # to the old window. Provider-reported boundaries
                    # (`old_week_end_at`, `new_week_end_at`) keep their own
                    # hour normalization; only our own instant is unfloored.
                    effective_iso = capture_dt.isoformat(timespec="seconds")
                    conn.execute(
                        "INSERT OR IGNORE INTO week_reset_events "
                        "(detected_at_utc, old_week_end_at, new_week_end_at, "
                        " effective_reset_at_utc, account_key, "
                        " origin_observation_id) VALUES (?, ?, ?, ?, ?, ?)",
                        ((as_of or now_utc_iso()), prior_end_canon, cur_end_canon,
                         effective_iso, account_key, origin_observation_id),
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
                    # Read the pending reset-to-zero state up front and
                    # compute whether it is armed for THIS window; the
                    # debounce CLASSIFIER (pure) decides the action from
                    # those values + the c._RESET_* constants, then the glue
                    # below executes the decided I/O. The 5 branch outcomes
                    # (fire-immediate / confirm / clear / arm / none) map 1:1
                    # to the pre-extraction structure. #750 S3 §1.3: the
                    # state is a stats.db row, so every mutation below folds
                    # into the same transaction as the reset event and the
                    # journal cursor.
                    state = _read_reset_debounce_state(conn, account_key)
                    armed = (
                        state is not None
                        and state[0] == week_start_date
                        and state[1] == cur_end_canon
                    )
                    # #750 S3 §1.3: an observation can never confirm ITSELF.
                    # The armed state names the observation that armed it, so
                    # a byte-identical replay of that line — which crash
                    # replay produces by construction — must leave the state
                    # armed rather than reach the confirm branch and mint a
                    # reset nobody observed twice. Checked BEFORE the
                    # classifier is consulted, because the classifier sees
                    # only percentages and cannot tell the two readings
                    # apart.
                    self_replay = (
                        armed
                        and origin_observation_id is not None
                        and state[4] == origin_observation_id
                    )
                    if self_replay:
                        decision = NO_ACTION
                    else:
                        decision = plan_weekly_credit_debounce(
                            prior_pct, weekly_percent,
                            drop_threshold=c._RESET_PCT_DROP_THRESHOLD,
                            zero_floor_pct=c._RESET_ZERO_FLOOR_PCT,
                            zero_min_drop_pct=c._RESET_ZERO_MIN_DROP_PCT,
                            marker_armed=armed,
                            marker_baseline=(state[2] if armed else None),
                        ).action
                    if decision == FIRE_IMMEDIATE:
                        # >=25pp goodwill credit — fire immediately, never
                        # debounced. Clear any pending arm (now moot).
                        _clear_reset_debounce_state(conn, account_key)
                        _fire_in_place_credit(
                            conn, week_start_date, cur_end_canon, weekly_percent,
                            observed_pre_credit_pct=float(prior_pct),
                            effective_dt=capture_dt,
                            as_of=as_of, commit=commit, ctx=ctx,
                            account_key=account_key,
                            origin_observation_id=origin_observation_id,
                        )
                    elif decision == CONFIRM_RESET:
                        # Second reading stayed low → confirm. Anchor the
                        # reset at the FIRST-zero instant from the state
                        # (UTC-normalized like the backfill in-place path).
                        first_zero_dt = parse_iso_datetime(
                            state[3], "reset_debounce_state.first_zero"
                        ).astimezone(dt.timezone.utc)
                        _fire_in_place_credit(
                            conn, week_start_date, cur_end_canon,
                            weekly_percent,
                            observed_pre_credit_pct=state[2],
                            effective_dt=first_zero_dt,
                            as_of=as_of, commit=commit, ctx=ctx,
                            account_key=account_key,
                            # The FIRST-ZERO observation, not the confirming
                            # one: a replayed confirming observation must
                            # reproduce the same event id, and only the first
                            # zero is an instant the crashed cycle and its
                            # retry both agree on (#750 S3 §1.2).
                            origin_observation_id=state[4],
                        )
                        # Clear ONLY after the fire completes (P2a): a
                        # mid-fire raise leaves the state armed so the next
                        # zero re-confirms + re-runs the idempotent pivots.
                        _clear_reset_debounce_state(conn, account_key)
                    elif decision == CLEAR_MARKER:
                        # Recovered toward baseline → transient zero, not a
                        # reset. Clear, do not fire.
                        _clear_reset_debounce_state(conn, account_key)
                    elif decision == ARM_MARKER:
                        # First ~0 → arm; do NOT fire. The write clamp
                        # suppresses this 0 (no event row yet), so the prior
                        # snapshot stays at the baseline and this shape
                        # re-evaluates next tick. first_zero_at_utc is the
                        # observation's payload CAPTURE stamp (#750 S3 §1.2),
                        # not the detection clock and not wall-clock — it
                        # becomes the effective anchor on confirm, to the
                        # exact second.
                        _arm_reset_debounce_state(
                            conn, account_key,
                            week_start_date=week_start_date,
                            week_end_at=cur_end_canon,
                            baseline_pct=float(prior_pct),
                            first_zero_at_utc=capture_dt.isoformat(
                                timespec="seconds"),
                            first_zero_observation_id=origin_observation_id,
                        )
                    # else NO_ACTION: not a reset shape and not armed →
                    #     nothing. A non-matching stale state is inert
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
                # The prior snapshot answers two WINDOW-level questions, and
                # only those: whether the previous accepted reading belongs to
                # the same physical window, and whether that window has reset.
                # It is deliberately no longer the credit baseline — it is the
                # latest accepted row whatever contributor produced it, and its
                # five-hour percent is MAX-clamped at write time, so it sits
                # downstream of the defect this rule removes (#769 S2 §3).
                same_window = (
                    prior_5h_row is not None
                    and int(prior_5h_row["five_hour_window_key"])
                        == int(five_hour_window_key)
                    and prior_5h_row["five_hour_resets_at"] is not None
                )
                # ``now_utc`` was bound earlier in this same outer try block
                # from ``dt.datetime.now(dt.timezone.utc)``; reuse it so both
                # branches see the same instant.
                window_live = same_window and parse_iso_datetime(
                    prior_5h_row["five_hour_resets_at"],
                    "prior.five_hour_resets_at",
                ) > now_utc
                # This is False for two different reasons, and the kernel
                # CANCELs an armed descent in both: the window genuinely
                # elapsed, or the latest accepted snapshot belongs to some
                # other window — which is what a straggler observation arriving
                # after a later window's snapshot was accepted looks like from
                # here. Conflating them is deliberate rather than overlooked.
                # Both directions fail toward a MISSED credit and never toward
                # a fabricated one, which is the safety property this whole
                # rule exists to hold, and separating them would need the
                # straggler's own window state, which the latest-snapshot
                # lookup does not carry.
                # The state machine runs for EVERY observation that carries a
                # window and a percent, not only for one with a matching prior
                # snapshot. The first reading of a window has no prior row, and
                # if it did not establish this contributor's baseline the
                # SECOND reading would become the baseline and a descent
                # between them would be invisible.
                five_hour_source = source or FIVE_HOUR_UNKNOWN_SOURCE
                src_decision, src_state = _step_five_hour_source_state(
                    conn, account_key, five_hour_window_key, five_hour_source,
                    new_pct=float(five_hour_percent),
                    observation_id=origin_observation_id,
                    drop_threshold=c._FIVE_HOUR_RESET_PCT_DROP_THRESHOLD,
                    window_live=window_live,
                    now_iso=now_utc.isoformat(timespec="seconds"),
                )
                if src_decision.action == FIVE_HOUR_CONFIRM:
                    # The two ends of the credit, both taken from the ARMING
                    # observation: the pre-drop baseline this source
                    # established, and the low that dropped away from it. The
                    # confirming observation's own percent is neither of them
                    # — it only establishes that the drop was not a single
                    # stale reading — and binding `post_percent` to it records
                    # a drop smaller than the eligibility threshold the arming
                    # leg required, which `R-5HC1` forbids and which the three
                    # renderers of `post - prior` would display.
                    prior_5h_pct = float(src_decision.credit_prior_pct)
                    post_5h_pct = float(src_decision.credit_post_pct)
                    # The band centre for the stale-replica capture and DELETE
                    # below, which is a DIFFERENT question from the event's
                    # `prior_percent` (#769 S2 §3 F2). Those two describe
                    # different things: the event records what THIS contributor
                    # observed before its own drop, while the DELETE removes
                    # ACCEPTED snapshot rows, and
                    # `_usage_snapshot_fold_decision` MAX-clamps an accepted
                    # five-hour percent UP across every contributor. A row that
                    # holds the five-hour surfaces at the pre-credit level
                    # therefore carries that clamped maximum, which is at or
                    # above this contributor's own baseline whenever another
                    # contributor peaked higher inside the window. Banding on
                    # the baseline leaves those rows standing, and the
                    # post-credit clamp then raises every post-credit reading
                    # back to the peak.
                    #
                    # The latest accepted row IS that maximum, because the
                    # clamp makes accepted values non-decreasing inside a
                    # window. It is present here unconditionally: this leg is
                    # reached only when `window_live` is true, and that
                    # requires `prior_5h_row`.
                    stale_replica_pct = float(
                        prior_5h_row["five_hour_percent"])
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
                    #
                    # #769 S2 §3 F1: the pair compares the credit this tick is
                    # about to write against the credit already stored, so both
                    # sides must name the same two quantities. The stored
                    # `post_percent` is an armed low, so the incoming side is
                    # `post_5h_pct`, not the confirming tick's percent —
                    # comparing an armed low against a confirming percent
                    # answers no question at all.
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
                        and round(post_5h_pct, 1)
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
                    #
                    # #769 S2 §3: floored from the ARMING tick, not from
                    # this confirming one. The credit happened when the
                    # drop was observed, and the weekly CONFIRM leg anchors
                    # on its first-zero instant for the same reason. It is
                    # also what makes the stale-replica DELETE below reach
                    # the arming observation's own row: the write clamp
                    # raised that reading back to the pre-credit baseline,
                    # so it is a stale replica by that DELETE's own
                    # predicate, and both the DELETE and the reset-aware
                    # MAX bound on `>= effective_iso`. Falls back to the
                    # confirming tick when the armed row carries no instant
                    # (a state row written before this field existed).
                    armed_at_iso = (
                        src_state[2] if src_state is not None else None)
                    effective_dt = c._floor_to_ten_minutes(
                        parse_iso_datetime(
                            armed_at_iso, "five_hour_state.pending_at"
                        ).astimezone(dt.timezone.utc)
                        if armed_at_iso else now_utc
                    )
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
                                post_5h_pct,
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
                                    stale_replica_pct,
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
                    #
                    # #769 S2 §3 F1: this stays bound to the
                    # confirming tick's percent while the event
                    # row records the armed low, because the
                    # two answer different questions. The event
                    # is a historical record of a drop, so both
                    # its ends belong to the arming
                    # observation. This file is the statusline's
                    # no-regression floor for the CURRENT meter
                    # level, and the current level is the
                    # reading this tick just observed. Writing
                    # the armed low would publish a level the
                    # meter has already moved past.
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
                    # post-credit values are never caught. That
                    # margin holds against ``stale_replica_pct``
                    # and not only against the event's
                    # ``prior_percent``: arming requires the
                    # full 5.0pp drop from this source's own
                    # baseline, and the clamped maximum is at or
                    # above that baseline, so the credited low
                    # is at least 5.0pp below the band centre.
                    # ``unixepoch()`` on both sides for offset
                    # robustness (Z vs +00:00). Bind is
                    # ``stale_replica_pct`` — the clamped level
                    # these rows actually carry — NOT the
                    # event's ``prior_percent``; see the two
                    # values' definitions above.
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
                                stale_replica_pct,
                            ),
                        )
                        # (inline commit removed — end-of-function commit on
                        # legacy; the ingest cycle owns the commit.)
                    except sqlite3.DatabaseError as exc:
                        eprint(
                            "[record-usage] 5h post-credit "
                            f"cleanup failed: {exc}"
                        )
                    # Retire the pending descent and restart this source's
                    # baseline at the credited level. Written only AFTER
                    # the pivots complete, mirroring the weekly CONFIRM
                    # leg's P2a ordering: a mid-fire raise leaves the state
                    # armed so the next same-source reading below the
                    # baseline re-confirms and re-runs the idempotent
                    # pivots, rather than losing the credit outright.
                    _write_five_hour_source_state(
                        conn, account_key, five_hour_window_key,
                        five_hour_source,
                        baseline_pct=float(five_hour_percent),
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
    floor is the latest effective across BOTH `week_reset_events` and
    `weekly_credit_floors` (`_reset_aware_floor`) so a manual partial credit
    (record-credit M2, #209) lowers the resolved HWM without re-anchoring the
    week — used both as the `--from` default and as the assertion source of
    truth in the record-credit tests.

    ``account_key`` (#341): MANDATORY account context (no silent global
    fallback). A real key scopes the HWM to one account; ``None`` is the
    explicit merged read (byte-identical to today on a single-account install)."""
    floor_iso = _reset_aware_floor(conn, week_start_date, week_start_at,
                                   week_end_at, account_key=account_key)
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_param: tuple = () if account_key is None else (account_key,)
    # `weekly_observation_held = 0` (#769 S11, #824): a weekly sampling read,
    # and one a held row genuinely changes. The floored leg is the reason — a
    # held row written after a credit floor carries the PRE-credit value
    # forward, so counting it reports a high-water mark the credit retired. The
    # unfloored leg carries the predicate too, because the two must not
    # disagree about what a held row is.
    if floor_iso is not None:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            f" WHERE week_start_date = ?{acct_pred} "
            "   AND weekly_observation_held = 0 "
            "   AND unixepoch(captured_at_utc) >= unixepoch(?)",
            (week_start_date, *acct_param, floor_iso),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
            f" WHERE week_start_date = ?{acct_pred} "
            "   AND weekly_observation_held = 0",
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


def _resolve_prior_5h(conn, at_dt, *, account_key=None):
    """Return the most-recent snapshot's (five_hour_percent, five_hour_resets_at,
    five_hour_window_key) iff that 5h window is still active (resets_at > at_dt),
    else (None, None, None) — so the synthetic row doesn't blank the live 5h
    display, and never inflates the 5h HWM (copies an already-<=MAX value).

    ``account_key`` (#834 S2, #837): the five-hour evidence a credit carries
    forward must be the crediting account's own. This read takes the MOST
    RECENT row, so merged it took whichever account happened to tick last, and
    the synthetic then published another account's five-hour reading. ``None``
    is the explicit merged read, byte-identical on a single-account install."""
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_p = () if account_key is None else (account_key,)
    row = conn.execute(
        "SELECT five_hour_percent, five_hour_resets_at, five_hour_window_key "
        "FROM weekly_usage_snapshots "
        "WHERE five_hour_resets_at IS NOT NULL AND five_hour_window_key IS NOT NULL"
        + acct_pred +
        " ORDER BY unixepoch(captured_at_utc) DESC, id DESC LIMIT 1",
        acct_p).fetchone()
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
    spec §4). Unlike the >=25pp auto-credit path (`_fire_in_place_credit`), this
    writes NO `week_reset_events` row — the window-resolution code never sees a
    credit, so the week is NOT re-anchored. It only lowers the clamp floor.

    ``account_key`` (#341): stamps the ``weekly_credit_floors`` row + the
    synthetic snapshot and scopes the stale-replay DELETE to the account.

    Transaction-neutral / capture-time-pure seam (DB journal redesign §5.2.3):
    ``commit=False`` folds the floor INSERT + stale-replay DELETE + synthetic
    snapshot into the caller's transaction (the ingester's cycle) instead of
    committing inline; ``as_of`` (ISO-Z) stamps ``weekly_credit_floors``'
    ``applied_at_utc`` in place of wall clock (the reset moment stays
    ``plan.effective_iso``). Both defaults keep the legacy behavior.

    Design B/event+effects seam (§5.3): the same-window sub-25pp credit writes
    NO reset row, so on the ingest path (``ctx`` passed, ``id_base`` = the
    triggering ``record-credit`` op's journal id) its DESTRUCTIVE effects ride a
    ``weekly_credit_effects`` Model-A evt (``wce:<id_base>``) carrying the
    stale-replica suppression list (the doomed snapshots' ``journal_id``s,
    captured with the SAME predicate as the DELETE) + the forced ``hwm_floor``;
    the synthetic post-credit snapshot rides its own ``snapshot_accept`` evt
    (``sa:<id_base>:syn:0``) because ``weekly_usage_snapshots`` is written ONLY
    via ``snapshot_accept`` now (rev-3 emission rule). ``emit_model_a`` applies
    each evt through the same fold replay uses, so the inline hwm/DELETE/synthetic
    are SKIPPED on the ingest path (the evt appliers do them). ``ctx=None``
    (legacy) keeps the inline path verbatim.

    ``forced`` (ingest path only) journals the ``--force`` re-record's destructive
    clear that legacy ``_force_clear_credit`` did inline: the ``wce`` evt's
    ``suppression`` list widens to also delete this week's OLD command-owned
    synthetic snapshots and a ``floor_suppression`` list deletes its OLD
    ``weekly_credit_floors`` rows (both by logical ``journal_id``), so a
    ``--force`` re-record replays deterministically. The NEW floor (op fold,
    ``journal_id = id_base``) and NEW synthetic (``sa:<id_base>:syn:0``) are
    excluded from both lists, so the clear is order-independent and idempotent.

    Side-effect ordering mirrors `_fire_in_place_credit`'s discipline: the
    INSERT OR IGNORE of the floor row is dedup-gated by UNIQUE(week_start_date,
    effective_at_utc), but the hwm force-write, stale-replay DELETE, and
    synthetic-snapshot INSERT run UNCONDITIONALLY so a rerun finishes a crash-
    half-applied credit (memory: project_dedup_must_not_gate_side_effects). All
    are individually idempotent (file overwrite; DELETE on a stable predicate;
    the synthetic snapshot is re-INSERTed only after a ``--force`` re-record's
    destructive clear or on the completion path where none exists yet).

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
    # LEGACY-ONLY (Option (i), 6e): on the ingest path (`ctx` passed) the floor
    # row is written by the built-in op fold `_apply_op_weekly_credit_floor`,
    # which runs FIRST per record from the record-credit `op` line and stamps
    # `journal_id = record["id"]` — so the floor row is journal-identified and
    # rebuild-replayable, and the op fold is the SOLE `weekly_credit_floors`
    # writer on the ingest path. `_apply_credit`'s own INSERT here would race the
    # op fold for the UNIQUE(week_start_date, effective_at_utc) slot and leave a
    # NULL-`journal_id` row on the losing INSERT; gating it on `ctx is None`
    # makes the "op fold owns the floor" invariant explicit and keeps the floor's
    # journal_id unambiguous. On the legacy path (`ctx=None`) the INSERT is
    # idempotent via that same UNIQUE.
    if ctx is None:
        conn.execute(
            "INSERT OR IGNORE INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
            " applied_at_utc, account_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (plan.week_start_date, effective_iso, pre_credit,
             as_of or now_utc_iso(), account_key),
        )
        if commit:
            conn.commit()

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

        # 4c. Stale-replay DELETE: drop pre-credit-valued replays that land
        # at/after the floor (the gotcha_statusline_replay_race_after_credit
        # defense; same 1.0pp band as the auto path). unixepoch() on both sides
        # for offset safety.
        try:
            # #834 S1 (#835): ONE statement over the shared band. Held rows are
            # excluded by it; the strict comparison is selected by `manual=True`.
            # No `id_base` here — that argument belongs to the suppression
            # projection, and this leg runs the removal BEFORE step 4d inserts the
            # synthetic while a rerun re-inserts it, so the own-synthetic exclusion
            # the ingest capture needs has nothing to protect on this path.
            c._delete_doomed_snapshot_rows(
                conn, week_start_date=plan.week_start_date,
                account_key=account_key, effective_iso=effective_iso,
                pre_credit=pre_credit, manual=True)
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
        # #834 S1 (#835): the suppression projection of the ONE classifier.
        # `id_base` is what excludes THIS op's OWN synthetic ids
        # (`sa:<id_base>:syn:%`) by deterministic id rather than by emission
        # timing (6g P2 / Task 7 Item 0, the sibling of the `old_syn` forced-path
        # fix below), and `_doomed_snapshot_rows` states why: a sub-1.0pp credit
        # is legal and puts the synthetic (at `plan.to_pct`) INSIDE the band
        # centred on `from_pct`, and under a crash between evt fsync and COMMIT
        # the next cycle replays `sa:<id_base>:syn:0` at fold order 10 BEFORE
        # step 4b re-runs `_apply_credit`. The exclusion applies on the NON-force
        # path too (`supp = doomed_supp` here, before `if forced:`). Held rows are
        # in neither projection, so a suppression list can never name a row the
        # inline DELETE now keeps.
        _, doomed_supp = c._doomed_snapshot_rows(
            conn, week_start_date=plan.week_start_date,
            account_key=account_key, effective_iso=effective_iso,
            pre_credit=pre_credit, manual=True, id_base=id_base)
        supp = list(doomed_supp)
        floor_supp: list = []
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
            old_syn = conn.execute(
                "SELECT journal_id FROM weekly_usage_snapshots "
                "WHERE week_start_date = ? AND account_key = ? "
                "  AND source = 'record-credit' "
                "  AND journal_id IS NOT NULL "
                "  AND journal_id NOT LIKE 'sa:' || ? || ':syn:%'",
                (plan.week_start_date, account_key, id_base),
            ).fetchall()
            supp.extend(r[0] for r in old_syn if r[0] not in supp)
            old_floors = conn.execute(
                "SELECT journal_id FROM weekly_credit_floors "
                "WHERE week_start_date = ? AND account_key = ? "
                "  AND journal_id IS NOT NULL AND journal_id != ?",
                (plan.week_start_date, account_key, id_base),
            ).fetchall()
            floor_supp = [r[0] for r in old_floors]
        # #761 residual 1: canonicalize AFTER both the normal and the forced
        # captures. The two `--force` SELECTs below carry no `ORDER BY`, so
        # without this the payload depends on SQLite's row order — and the `wce`
        # id names the operation without digesting its payload, so one operation
        # emitting two orderings emits two payloads under one id. The second
        # classifies as a conflict and is never appended, which leaves its
        # DELETE standing as the inline-only effect this event exists to
        # replace. `_doomed_snapshot_rows` already returns its own projection
        # canonicalized, on both the automatic and the manual path; this line is
        # what keeps the widened list canonical too. The NULL and own-synthetic
        # exclusions stay inside the classifier and these queries.
        supp = sorted(set(supp))
        floor_supp = sorted(set(floor_supp))
        # wce evt (effects-only, table=None): snapshot suppression list + floor
        # suppression list (--force clear) + forced hwm floor. emit_model_a
        # appends+fsyncs the line then applies it via `_apply_weekly_credit_effects`
        # (DELETE by journal_id from both tables + hwm-7d write).
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
        # Synthetic post-credit snapshot as a snapshot_accept evt (the only
        # writer of weekly_usage_snapshots on the ingest path). Route it through
        # `_insert_credit_snapshot(journal=...)` — the SOLE synthetic writer on
        # both paths — so the non-vacuity stub of `_insert_credit_snapshot` stays
        # load-bearing (stubbing it disables the synthetic on the ingest path too).
        c._insert_credit_snapshot(
            conn, plan, five_hour=five_hour, commit=False,
            journal=(ctx, f"sa:{id_base}:syn:0"), account_key=account_key)

    # 4e. Clear a stale same-week reset-zero debounce state so the next
    # record-usage tick can't confirm a phantom reset-to-zero off it. The
    # DELETE folds into whatever transaction this op is running in, so it is
    # undone with the credit if the cycle rolls back.
    _clear_reset_debounce_state(conn, account_key)


def _stale_replay_candidates(conn, plan, *, account_key):
    """The exact `weekly_usage_snapshots` ids the `_apply_credit` stale-replay
    removal (step 4c) will touch, ordered by id. Read-only.

    #834 S2 (#837). This is the ENUMERATION the preview reports and the apply
    path re-derives under the writer lock, so the two describe one population
    rather than two numbers that happen to agree. Equal counts over different
    rows is the failure the identity comparison exists to catch, and it was
    reachable: the count below counted across every account while the removal
    has always been account-scoped.

    An eligible row is the intersection of the confirmed account, the stale
    credit band and the held-row exclusion — and all three come from
    `_stale_replica_band_sql`, the ONE template the removal itself uses, so the
    preview and the DELETE cannot drift apart again. `manual=True` selects
    `record-credit`'s STRICT `< 1.0` comparison; `_doomed_snapshot_rows` states
    why that divergence from the automatic path is deliberate.

    #834 S1 (#835): the held-row exclusion rides that template and goes through
    `_cctally_core.weekly_held_exclusion`, which omits the predicate on a store
    predating epoch 1015 — sound, because such a store cannot hold a held row.
    """
    band = _stale_replica_band_sql(conn, manual=True)
    rows = conn.execute(
        "SELECT id FROM weekly_usage_snapshots " + band + " ORDER BY id",
        (plan.week_start_date, account_key, plan.effective_iso,
         float(plan.from_pct)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _count_stale_replays(conn, plan, *, account_key):
    """The size of the previewed stale population, for the preview / --json
    `staleReplaysDeleted` field.

    #834 S2 (#837): the account predicate the DELETE carries is no longer
    absent. It used to be, because `plan.from_pct` and the floor the credit was
    computed from were themselves resolved account-blind, so scoping the count
    alone would have made the preview describe a different population from the
    credit it previewed. Every one of those reads is now account-scoped, so the
    count is too, and it is derived from the enumeration rather than from a
    second copy of the predicate."""
    return len(_stale_replay_candidates(conn, plan, account_key=account_key))


def _credit_account_disclosure(conn, account_key):
    """The R8-gated display label for the account a credit will be written
    under, or ``None`` when this provider renders no account decoration.

    #834 S2 (#837). R8 (`docs/accounts-gotchas.md`): decoration appears ONLY at
    more than one REAL account, and a lone `unattributed` bucket triggers
    nothing — so a single-account and a legacy install render byte-identically
    to pre-#837. The gate goes through `provider_is_decorated` rather than
    re-deriving a count, because that function is R8's single definition.

    The gate governs DISPLAY only. It has no bearing on the computational
    predicate: every planning read is scoped to `account_key` whether or not
    this returns a label.
    """
    import _cctally_account
    try:
        if not _cctally_account.provider_is_decorated(conn, "claude"):
            return None
        return _cctally_account.display_account_label(conn, account_key)
    except sqlite3.DatabaseError:
        # A registry read that fails must not take the whole preview with it:
        # the disclosure is decoration, and its absence is the byte-identical
        # single-account rendering.
        return None


def _credit_preview_text(plan, *, stale_replays, dry_run, account_label=None):
    """Human preview (spec §5). Shown before the confirm prompt and as the
    whole body under --dry-run.

    ``account_label`` (#834 S2, #837) is the R8-gated display label of the
    account this credit will be written under, or ``None`` at <=1 real account.
    A preview that does not say which account it will write is not a preview a
    person can check, and on a mixed store the target is not obvious."""
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
    ]
    if account_label is not None:
        lines.append(f"  account:       {account_label}")
    lines += [
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


def _credit_json(plan, *, applied, dry_run, forced, stale_replays, hwm_before,
                 account_key=None, account_label=None):
    """The --json envelope (schemaVersion 1, spec §5); all datetimes …Z.

    ``account_key`` / ``account_label`` (#834 S2, #837) disclose the account
    this credit will be written under. Both are ``None`` at <=1 real account and
    the keys are then OMITTED, so a single-account and a legacy install emit the
    byte-identical envelope they emitted before. The addition is optional and
    additive, so `docs/cli-contract.md` does not require a `schemaVersion`
    bump."""
    def _z(iso):
        return parse_iso_datetime(iso, "z").astimezone(
            dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
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
    if account_label is not None:
        payload["accountKey"] = account_key
        payload["accountLabel"] = account_label
    return payload


def _revalidate_credit_plan(conn, args, *, now, at_dt, expected_plan,
                            account_key=None):
    """Recompute the confirmed credit plan from locked, current DB truth.

    The caller has already completed every preview/refusal/confirmation path.
    Returning ``None`` is deliberately side-effect free: it means a concurrent
    writer changed the requested credit's basis and the user must retry rather
    than authorizing a different mutation than the preview showed.

    ``account_key`` (#834 S2, #837) is the account the preview was computed
    under, and EVERY read below carries it. An account-scoped preview validated
    against a merged reconstruction proves nothing: the two would describe
    different populations, so the comparison would pass or fail for reasons
    unrelated to concurrent writers.
    """
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_p = () if account_key is None else (account_key,)
    try:
        if getattr(args, "week", None):
            week_start_date = args.week
            ws_at, we_at = _get_canonical_boundary_for_date(
                conn, week_start_date, account_key=account_key)
            if not ws_at or not we_at:
                return None
        else:
            fetched = _fetch_current_week_snapshots(
                conn, at_dt, account_key=account_key)
            if fetched is None:
                return None
            ws_at, we_at, _samples = fetched
            ws_at = ws_at if isinstance(ws_at, str) else ws_at.isoformat(timespec="seconds")
            we_at = we_at if isinstance(we_at, str) else we_at.isoformat(timespec="seconds")
            week_start_date = parse_iso_datetime(ws_at, "ws_at").date().isoformat()
        existing = conn.execute(
            "SELECT id, effective_at_utc, observed_pre_credit_pct "
            "FROM weekly_credit_floors WHERE week_start_date=?" + acct_pred +
            " ORDER BY unixepoch(effective_at_utc) DESC, id DESC LIMIT 1",
            (week_start_date, *acct_p),
        ).fetchone()
        is_force = bool(getattr(args, "force", False))
        if getattr(args, "from_pct", None) is not None:
            from_pct, from_source = float(args.from_pct), "explicit"
        elif existing is not None and existing[2] is not None:
            from_pct, from_source = float(existing[2]), "prior_credit"
        else:
            from_pct = _resolve_reset_aware_hwm(
                conn, week_start_date, ws_at, we_at, account_key=account_key)
            if from_pct is None:
                return None
            from_source = "hwm"
        is_completion = False
        if existing is not None and not is_force:
            owned = conn.execute(
                "SELECT 1 FROM weekly_usage_snapshots "
                " WHERE week_start_date=? AND source='record-credit'" + acct_pred +
                "   AND unixepoch(captured_at_utc) >= unixepoch(?) LIMIT 1",
                (week_start_date, *acct_p, existing[1]),
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
        # 0. Resolve the ACTIVE Claude account BEFORE the first planning read
        #    (#834 S2, #837). It used to be resolved only under the writer lock,
        #    after the whole plan had been computed and confirmed, which made the
        #    stamped account a late label over a plan resolved across every
        #    account. The account is a decision input: it is the column the reset
        #    detector's predecessor query selects on, so a plan computed from one
        #    account's population and stamped to another changes which prior row a
        #    later credit is measured against.
        #
        #    The three-valued stable-read contract is unchanged (spec §1): a TORN
        #    read means the identity is genuinely unavailable -> exit 2 (retry); a
        #    STABLY-ABSENT read (no ~/.claude.json / api-key mode) is a RESOLVED
        #    `unattributed` outcome and proceeds. Only WHERE the call happens
        #    moved.
        identity = _cctally_core._resolve_active_claude_identity()
        if identity.get("status") == "torn":
            eprint("record-credit: active Claude account is unavailable "
                   "(torn read of ~/.claude.json); retry once it settles")
            return 2
        credit_account_key = identity["account_key"]
        # The resolved key travels ALONGSIDE the plan, never inside it:
        # `vars(plan)` is persisted into the credit op's payload, so a new
        # `CreditPlan` field would change what is written.
        credit_account_label = _credit_account_disclosure(
            conn, credit_account_key)

        # 1. Resolve the week — inside the resolved account (#834 S2, #837).
        #    Every read from here to the authoritative begin carries
        #    `credit_account_key`, because the plan is a statement about ONE
        #    account's population.
        _acct_pred = "" if credit_account_key is None else " AND account_key = ?"
        _acct_p = () if credit_account_key is None else (credit_account_key,)
        if getattr(args, "week", None):
            week_start_date = args.week
            ws_at, we_at = _get_canonical_boundary_for_date(
                conn, week_start_date, account_key=credit_account_key)
            if not ws_at or not we_at:
                # #834 S2 (#837). Scoping this read to the resolved account made
                # a bare absence ambiguous: the week may hold no rows at all, or
                # it may hold rows this account does not own. Those need
                # different actions from the reader, so the second one says so.
                # Not gated on R8's more-than-one-real-account rule, because the
                # reachable case is a single real identity over sentinel
                # history, and that is exactly the reader who would otherwise be
                # told the week is empty when it is not.
                other = None
                if credit_account_key is not None:
                    merged_ws, merged_we = _get_canonical_boundary_for_date(
                        conn, week_start_date, account_key=None)
                    if merged_ws and merged_we:
                        other = [
                            str(r[0]) for r in conn.execute(
                                "SELECT DISTINCT account_key FROM "
                                "weekly_usage_snapshots WHERE week_start_date = ?"
                                " AND account_key <> ? ORDER BY account_key",
                                (week_start_date, credit_account_key))]
                if other:
                    eprint(
                        f"record-credit: no snapshot for --week {week_start_date} "
                        f"under account {credit_account_key}; that week is held "
                        f"by {', '.join(other)}. A credit is never computed from "
                        f"another account's rows.")
                else:
                    eprint(
                        f"record-credit: no snapshot for --week {week_start_date}")
                return 2
        else:
            fetched = _fetch_current_week_snapshots(
                conn, at_dt, account_key=credit_account_key)
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
        # new effective leaves the old row only until the ingest-path wce evt's
        # floor_suppression deletes it; pick the newest defensively).
        existing = conn.execute(
            "SELECT id, effective_at_utc, observed_pre_credit_pct "
            "FROM weekly_credit_floors WHERE week_start_date=?" + _acct_pred +
            " ORDER BY unixepoch(effective_at_utc) DESC, id DESC LIMIT 1",
            (week_start_date, *_acct_p)).fetchone()

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
            hwm = _resolve_reset_aware_hwm(
                conn, week_start_date, ws_at, we_at,
                account_key=credit_account_key)
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
                " WHERE week_start_date=? AND source='record-credit'" + _acct_pred +
                "   AND unixepoch(captured_at_utc) >= unixepoch(?) LIMIT 1",
                (week_start_date, *_acct_p, existing[1])).fetchone()
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
        # The previewed population is ENUMERATED, not counted (#834 S2, #837).
        # The apply path re-derives the same list under the writer lock and
        # compares identifiers, so the number a person authorizes describes the
        # rows that are actually removed.
        stale_candidates = _stale_replay_candidates(
            conn, plan, account_key=credit_account_key)
        stale_replays = len(stale_candidates)
        hwm_before = _resolve_reset_aware_hwm(
            conn, week_start_date, ws_at, we_at,
            account_key=credit_account_key)
        if hwm_before is None:
            hwm_before = plan.from_pct

        # --dry-run: preview only, write nothing, exit 0 (TTY or not,
        # with/without --json).
        if is_dry:
            if is_json:
                print(json.dumps(_credit_json(
                    plan, applied=False, dry_run=True, forced=False,
                    stale_replays=stale_replays, hwm_before=hwm_before,
                    account_key=credit_account_key,
                    account_label=credit_account_label)))
            else:
                print(_credit_preview_text(plan, stale_replays=stale_replays,
                                           dry_run=True,
                                           account_label=credit_account_label))
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
                                       dry_run=False,
                                       account_label=credit_account_label))
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
                account_key=credit_account_key,
            )
            if revalidated is None:
                eprint("record-credit: plan changed while awaiting confirmation; retry")
                return 2
            plan, existing, is_completion = revalidated
            # Active-account gate (#341 P2-1, spec §3): record-credit is
            # active-account-only. The credit op is stamped with the active
            # Claude account so the floor lands under the SAME account
            # post-Step-9 usage carries — else the account-scoped
            # `_reset_aware_floor` clamp would never see a real credit floor.
            #
            # #834 S2 (#837): this is a RE-resolution, and it runs BEFORE
            # `_authoritative_begin`, before any journal append and before any
            # DELETE or INSERT — the whole plan above was computed from this
            # account's population, so a disagreement means the confirmed
            # preview described a mutation to a different account. A TORN read
            # keeps its own diagnostic, because the identity is unavailable
            # rather than different; a stably-absent read is a RESOLVED
            # `unattributed` outcome and compares equal on a legacy install. A
            # changed key — including present-then-absent, which resolves to the
            # sentinel — takes the established plan-drift diagnostic, because
            # the answer is a fresh preview rather than a redirected mutation.
            locked_identity = _cctally_core._resolve_active_claude_identity()
            if locked_identity.get("status") == "torn":
                eprint("record-credit: active Claude account is unavailable "
                       "(torn read of ~/.claude.json); retry once it settles")
                return 2
            if locked_identity["account_key"] != credit_account_key:
                eprint("record-credit: plan changed while awaiting "
                       "confirmation; retry")
                return 2
            # Re-enumerate under the lock and compare IDENTIFIERS against the
            # preview (#834 S2, #837). Equal counts over different rows would
            # authorize a removal the user never saw, so the comparison is on
            # the list. A changed population takes the established plan-drift
            # diagnostic and exits 2 here — before `_authoritative_begin`,
            # before any journal append, and before any DELETE or INSERT.
            locked_candidates = _stale_replay_candidates(
                conn, plan, account_key=credit_account_key)
            if locked_candidates != stale_candidates:
                eprint("record-credit: plan changed while awaiting "
                       "confirmation; retry")
                return 2
            stale_replays = len(locked_candidates)
            hwm_before = _resolve_reset_aware_hwm(
                conn, plan.week_start_date, plan.week_start_at, plan.week_end_at,
                account_key=credit_account_key
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
            # own write synchronously. The op fold writes the `weekly_credit_floors`
            # row (Option (i), journal_id = the op line id); `_pipeline_record_credit`
            # -> `_apply_credit(ctx=..., forced=...)` journals the `weekly_credit_effects`
            # evt (stale-replica suppression + the `--force` clear of OLD synthetics
            # + OLD floors) and the synthetic `snapshot_accept` evt. Preview/refusal
            # paths above appended NOTHING; the weekly tombstone is already
            # fail-closed immediately before this single append+ingest.
            forced = bool(existing is not None and is_force)
            # `five_hour` is the COMMAND-time prior-5h read (before the credit),
            # carried in the op so the ingest hook derives the synthetic against
            # the same 5h state the command saw.
            five_hour = _resolve_prior_5h(
                conn, at_dt, account_key=credit_account_key)
            capture_iso = now_utc_iso(now)
            effective_utc = parse_iso_datetime(
                plan.effective_iso, "op.effective"
            ).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
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
                stale_replays=stale_replays, hwm_before=hwm_before,
                account_key=credit_account_key,
                account_label=credit_account_label)))
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
    """Write the hook ticket or invalidate every caught-up certificate.

    A Codex ticket also carries the configuration generation it was written
    under (#769 S6). That digest is what turns "this hook ran" into "this hook
    ran under the configuration that is on disk now", which is the claim the
    frontier needs and the one `observed_enabled` cannot make. Recomputing it
    here rather than reading a cached value is deliberate: the hook is the
    only process that can honestly say the handler executed.
    """
    try:
        frontier = _cctally()._load_sibling("_lib_ingest_frontier")
        generation = None
        if provider == "codex":
            try:
                generation = codex_configuration_generation(
                    _codex_lifecycle_roots())
            except Exception:
                # An unreadable configuration leaves the ticket unstamped,
                # which reads as "no execution evidence" rather than as false
                # evidence. Never fail the hook over it.
                generation = None
        if not frontier.record_activity(
            _cctally_core.APP_DIR, provider, str(transcript_path or ""),
            configuration_generation=generation,
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
    "account_key", "weekly_observation_held",
)


def _usage_snapshot_columns(conn, payload, week_start_name, *, account_key=None):
    """Compute the ``weekly_usage_snapshots`` column map + output ``saved`` dict
    for a payload — the exact canonicalization ``insert_usage_snapshot`` does —
    WITHOUT inserting (DB journal redesign §5.3).

    Returns ``(cols, saved)`` where ``cols`` is the ordered column→value map
    (no ``id`` / ``journal_id``, key order == ``_USAGE_SNAPSHOT_COLUMNS``) and
    ``saved`` is the output dict minus ``id``. Shared by
    ``insert_usage_snapshot`` (bare INSERT, legacy) and the ingest obs pipeline
    hook (``snapshot_accept`` Model-A emit) so the two write paths never drift.
    ``conn`` is used only for the ``_get_canonical_boundary_for_date`` override.

    #834 S2 (#837): ``account_key`` is the account this observation belongs to,
    and it scopes the canonical-boundary read. The ingest pipeline resolves the
    account from the obs stamp and passes it here; it cannot travel in
    ``payload``, because ``payload`` is serialized verbatim into
    ``payload_json`` and adding a key there would change stored bytes. ``None``
    falls back to the payload's own ``account_key`` (the legacy bare-INSERT
    path), which is the value the row is stamped with either way — so the
    boundary a row inherits is always one its own account established.
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

    # The account this row belongs to, resolved ONCE: it scopes the canonical
    # boundary read below and it is the value stamped into `cols` (#834 S2,
    # #837). The caller-supplied key wins because the ingest pipeline carries
    # the obs stamp and the payload does not.
    row_account_key = (
        account_key if account_key is not None
        else (payload.get("account_key") or "unattributed"))

    # Use the canonical boundary already established for this week_start_date
    # BY THIS ACCOUNT. This prevents relative-reset drift from creating
    # duplicate weeks, and — since #837 — prevents one account inheriting the
    # boundary another account happened to establish first.
    date_str = week_window.week_start.isoformat()
    canon_start, canon_end = _get_canonical_boundary_for_date(
        conn, date_str, account_key=row_account_key)
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
        # Account dimension (#341): the caller-supplied key (the ingest
        # pipeline's obs stamp) when present, else the payload's own; defaults
        # to the reserved sentinel for the bare test-only insert path. Resolved
        # above as `row_account_key` so the boundary read and the stored stamp
        # cannot name different accounts (#834 S2, #837).
        "account_key": row_account_key,
        # Held provenance (#769 S11, #824). 1 ONLY when the caller says so —
        # the pipeline's held branch, which supplies the basis boundary above
        # and the raw reading in the payload. Every other caller writes 0,
        # which is the truthful value for a genuinely observed weekly reading.
        "weekly_observation_held": (
            1 if payload.get("weeklyObservationHeld") else 0),
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
              account_key,
              weekly_observation_held
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _write_hwm_weekly(week_start_date, weekly_percent):
    """Write the hwm-7d projection file (statusline no-regression), the
    monotonic-guarded weekly half of cmd_record_usage's accept path. A
    projection file, never journaled; re-materialized on rebuild. Best-effort
    (OSError-swallow), matching the legacy write sites.

    Split from the five-hour writer by #769 S11 (#824) so a tick whose weekly
    axis is HELD can advance the five-hour projection without touching this
    one. The split is STRUCTURAL, not defensive: `hwm_file_next` below happens
    to suppress a held weekly value in the common case because it is not above
    the stored one, but if `hwm-7d` ever trailed the stored value the held
    value — which is carried forward evidence, not an observation — would be
    written into the file the status line renders from. The held path simply
    does not call this function."""
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


def _write_hwm_five_hour(five_hour_window_key, five_hour_percent):
    """Write the hwm-5h projection file — the five-hour half of the same accept
    path, and the ONLY projection a held tick advances. Same monotonic guard,
    same best-effort posture, same no-op when the observation carries no
    five-hour anchor."""
    if five_hour_percent is None or five_hour_window_key is None:
        return
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


def _five_hour_saved_from_fold(fold, *, snapshot_id, capture_at,
                               five_hour_resets_at):
    """Build the derivation input ``maybe_update_five_hour_block`` consumes,
    from one :class:`_cctally_journal.UsageSnapshotFoldResult` (#769 S11, #824).

    Until this existed, one ``weeklyPercent`` key served three distinct
    concepts: the block's weekly START, the block's weekly END, and a five-hour
    milestone's crossing metadata. On a tick whose weekly axis is HELD those are
    not the same number, and binding all three to either one is wrong in a
    different way:

      - ``blockWeeklyPercent`` is the EFFECTIVE weekly value. A block whose
        weekly start and end straddle a raw reading and an effective one
        reports a weekly delta nobody observed (review finding 6). On the
        worked tick — effective 63, raw 60 — the block begins and ends at 63.
      - ``sevenDayPercentAtCrossing`` is the RAW incoming reading, because it is
        contemporaneous observation metadata: it records what the meter said
        when this five-hour threshold was crossed, not what the week's
        high-water mark was. The same tick records 60 there.

    The caller passes the fold result and does not choose the two values, which
    is what makes the distinction structural rather than a convention every
    future caller has to remember. On a tick whose weekly axis is OBSERVED the
    two collapse to one number, which is why every pre-#824 caller was correct.
    """
    return {
        "id": snapshot_id,
        "capturedAt": capture_at,
        "blockWeeklyPercent": fold.weekly.effective_pct,
        "sevenDayPercentAtCrossing": fold.weekly.raw_pct,
        "fiveHourPercent": fold.five_hour.effective_pct,
        "fiveHourResetsAt": five_hour_resets_at,
        "fiveHourWindowKey": fold.five_hour.window_key,
    }


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

    # The exact raw observation id is reviewed outside this pipeline. A hold
    # can only be supplied on scratch replay; live ingest has no event sink.
    hold_weekly_axis = (
        ctx.event_sink is not None
        and rec.get("id") in ctx.held_weekly_observation_ids
    )

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
    # One resolution, used twice: the five-hour confirmation state's source
    # bucket below, and `out_payload["source"]` -> the snapshot row's `source`
    # column. `_usage_snapshot_columns` applies the same `isinstance` test and
    # keeps a string verbatim, so resolving here is what makes the two agree.
    # Without the guard a present-but-non-string `source` reached the
    # confirmation state's PRIMARY KEY unchanged while the snapshot row
    # recorded `userscript` (#769 S2 §3 F8). The default is `statusline`
    # rather than `_usage_snapshot_columns`'s `userscript`, because this
    # pipeline's observations come from the status line, not the userscript.
    _raw_source = payload.get("source")
    source = _raw_source if isinstance(_raw_source, str) else "statusline"
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

    import _cctally_journal as jr
    reviewed_weekly_basis = None
    if hold_weekly_axis:
        reviewed_weekly_basis = (
            ctx.reviewed_weekly_basis_by_account.get(account_key)
            or jr._latest_weekly_basis(conn, account_key)
        )
        if reviewed_weekly_basis is None:
            import _lib_rederive
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly hold has no accepted account basis: "
                f"{rec['id']}"
            )

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
        # #750 S3: the raw journal id of the line this derivation came from.
        # The debounce ARM records it so a byte-identical replay of the first
        # zero cannot confirm itself.
        origin_observation_id=rec.get("id"),
        # The CAPTURE stamp, not `as_of`. It is what every
        # `effective_reset_at_utc` records, so live detection and the backfill
        # (which reads `weekly_usage_snapshots.captured_at_utc`) agree about
        # the same physical reset.
        capture_at=capture_at,
        # #769 S2 §3: the contributor discriminator for five-hour credit
        # confirmation. `payload.source`, never `rec["src"]`.
        source=source,
        hold_weekly_axis=hold_weekly_axis,
    )

    # 2. Accept/skip DECISION (clamp + dedup), made ONCE and journaled via the
    #    snapshot_accept evt (so replay never re-derives it — spec §5.3).
    fold = jr._usage_snapshot_fold_decision(conn, {
        "week_start_date": week_start_date,
        "week_start_at": week_start_at,
        "week_end_at": week_end_at,
        "weekly_percent": weekly_percent,
        "five_hour_percent": five_hour_percent,
        "five_hour_window_key": five_hour_window_key,
        "account_key": account_key,
    }, force_weekly_hold=hold_weekly_axis,
       reviewed_weekly_basis=reviewed_weekly_basis)
    # #769 S11 (#824). `weekly_held` says the weekly axis is held at the
    # accepted basis (an ordinary lower clamp or an explicit scratch hold);
    # `wrote_row` says whether this tick materialized a snapshot at
    # all. They are independent now: a held tick DOES write a row when its
    # five-hour axis carries evidence, and that row is the only place the
    # evidence survives, because `db rebuild` reconstructs open blocks from
    # snapshot history rather than by rerunning this pipeline.
    weekly_held = fold.weekly.disposition == jr.WEEKLY_HELD_CLAMP
    held_write = fold.snapshot_action == jr.SNAPSHOT_WRITE_HELD_5H
    wrote_row = fold.snapshot_action in (
        jr.SNAPSHOT_WRITE_OBSERVED, jr.SNAPSHOT_WRITE_HELD_5H)
    five_hour_percent = fold.five_hour.effective_pct

    # 3. Resolve the derivation target `saved`. The fold DECISION gates ONLY the
    #    snapshot INSERT (snapshot_accept); the derivations below run for EVERY
    #    line (spec §4.5: "the ingester's snapshot-insert dedup is the same rule
    #    as today's ... while derivations run for every line — preserving the
    #    'dedup must not gate side effects' invariant structurally"). A dedup-skip
    #    tick re-runs the (idempotent) chokepoints against the latest snapshot —
    #    which is exactly what subsumes today's kill-window self-heal probes.
    #    On ACCEPT `saved` is the freshly-journaled row; on SKIP it is the latest.
    if wrote_row:
        # A HELD row's weekly value and boundary are the basis's, and its
        # capture time, source and five-hour fields are the tick's own. The
        # boundary in particular must not be the tick's: `detect_reset_and_credit`
        # compares the incoming boundary against the latest stored one, so a
        # held row carrying the tick's boundary could reclassify a later reset
        # as an in-place credit (review finding 7). The fold guarantees a basis
        # whenever it selects SNAPSHOT_WRITE_HELD_5H.
        basis = fold.weekly.basis
        out_payload = {
            "source": source,
            "capturedAt": capture_at,
            "weeklyPercent": (
                fold.weekly.effective_pct if held_write else weekly_percent),
            "weekStartDate": (
                basis.week_start_date if held_write else week_start_date),
            "weekEndDate": (
                basis.week_end_date if held_write else week_end_date),
            "weekStartAt": (
                basis.week_start_at if held_write else week_start_at),
            "weekEndAt": (
                basis.week_end_at if held_write else week_end_at),
        }
        if held_write:
            # The stored weekly value is carried forward, so the tick's own
            # reading exists in no column of the row. It is retained here for
            # audit and for `db rederive`, which reruns the raw observation
            # through this pipeline and must reach the same decision.
            out_payload["weeklyObservationHeld"] = True
            out_payload["rawWeeklyPercent"] = weekly_percent
            out_payload["rawWeekStartDate"] = week_start_date
            out_payload["rawWeekEndDate"] = week_end_date
            out_payload["rawWeekStartAt"] = week_start_at
            out_payload["rawWeekEndAt"] = week_end_at
        if five_hour_percent is not None:
            out_payload["fiveHourPercent"] = five_hour_percent
        if five_hour_resets_at_str is not None:
            out_payload["fiveHourResetsAt"] = five_hour_resets_at_str
        if five_hour_window_key is not None:
            out_payload["fiveHourWindowKey"] = five_hour_window_key

        week_start_name = get_week_start_name(ctx.config or {}, None)
        # The account travels as an argument rather than inside `out_payload`,
        # because `out_payload` is serialized verbatim into `payload_json`
        # (#834 S2, #837). It stamps the snapshot_accept evt columns so the
        # journaled row — and every replay/rebuild fold of it — carries the
        # account (#341), AND it scopes the canonical week-boundary read the
        # canonicalization performs.
        cols, saved = _usage_snapshot_columns(
            conn, out_payload, week_start_name, account_key=account_key)
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
        if ctx.event_sink is not None and not weekly_held:
            # Keep the most recent accepted weekly source across scratch
            # records. A later five-hour credit may suppress its snapshot
            # row, but that independent effect must not erase the reviewed
            # weekly basis for another held observation.
            basis = jr._latest_weekly_basis(conn, account_key)
            if basis is not None:
                ctx.reviewed_weekly_basis_by_account[account_key] = basis
    else:
        saved_week_start_date = (
            fold.weekly.basis.week_start_date
            if weekly_held and fold.weekly.basis is not None
            else week_start_date
        )
        latest = conn.execute(
            "SELECT * FROM weekly_usage_snapshots WHERE week_start_date = ? "
            "  AND account_key = ? "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
            (saved_week_start_date, account_key),
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
    #    The one exception is a HELD weekly axis. A no-change tick AGREES with
    #    the stored row, so re-deriving against it is the self-heal; a held
    #    weekly axis means the observation's 7d value was not accepted,
    #    so it CONTRADICTS `saved` and a weekly milestone derived
    #    from `saved`'s carried percent can record a crossing the meter did not
    #    happen. That is how the 2026-09-01 incident fabricated a 13% milestone
    #    in a fresh post-credit epoch from a stale pre-credit replica, and
    #    because milestones are forward-only within an epoch that row forecloses
    #    every genuine crossing below it there. Only the WEEKLY milestone is
    #    gated: the 5h block derivation below (and the window-rollover heal at
    #    step 4') genuinely need to run, and on a held tick they are the whole
    #    point of the row that was written. Note the gate keys on the AXIS, not
    #    on whether a row was written — a held tick writes one and must still
    #    suppress the weekly milestone.
    if not weekly_held:
        c.maybe_record_milestone(
            saved, conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
            journal=(ctx, rec["id"]), account_key=account_key,
            retained_selection=c.WeekSelection(
                week_start=dt.date.fromisoformat(week_start_date),
                week_end=dt.date.fromisoformat(week_end_date),
                start_iso_override=week_start_at,
                end_iso_override=week_end_at,
            ))
    # #769 S11 (#824). On a WRITE the block derives from the fold, through the
    # one builder that names the block's weekly value and the crossing's weekly
    # value separately — they differ on a held tick. On a no-write tick the
    # derivation is the idempotent self-heal against the LATEST stored row, so
    # it keeps deriving from that row: its own weekly value is already the right
    # one for both concepts, and substituting the incoming tick's five-hour
    # identity here would silently do step 4''s job unconditionally.
    c.maybe_update_five_hour_block(
        _five_hour_saved_from_fold(
            fold, snapshot_id=saved["id"],
            capture_at=saved["capturedAt"],
            five_hour_resets_at=five_hour_resets_at_str,
        ) if wrote_row else saved,
        conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
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

    # 4'. Window-rollover 5h-block heal (no-row-written path only). When no row
    #     is written, `saved` (the LATEST stored row) still carries the PREVIOUS
    #     5h window; the derivations above only ever touched that old
    #     (still-fresh) window. When the INCOMING record observed a NEW canonical
    #     `five_hour_window_key` whose `five_hour_blocks` anchor doesn't exist
    #     yet, materialize it BLOCK-ONLY — no snapshot insert — against a saved
    #     dict carrying the INCOMING record's 5h identity, NOT `saved`'s. Without
    #     this the active window is left unanchored (blocks/dashboard fall back
    #     to the heuristic "~") until the percent next moves. Ported from the legacy
    #     cmd_record_usage dedup self-heal (spec §4.5 "dedup must not gate side
    #     effects"; regression: bin/cctally-record-usage-selfheal-test
    #     window-rollover scenario).
    #     #769 S11 (#824): the "no row written" predicate is `not wrote_row`
    #     rather than the old `skip`, because a HELD tick writes a row and
    #     therefore does not need this heal — its own snapshot carries the new
    #     window. The window-and-anchor half of the predicate is now resolved
    #     ONCE, by the fold, as `five_hour.rollover_heal`; deriving it in two
    #     places is how the two could disagree about the same tick.
    if (not wrote_row and fold.five_hour.rollover_heal
            and five_hour_resets_at_str is not None):
        #     #769 S11 (#824): through the same builder as the write path. The
        #     hand-built dict this replaces wired the RAW weekly reading into
        #     the new block's weekly start, so a held tick that rolled over
        #     without writing a row opened a block reporting a weekly delta
        #     nobody observed — review finding 6, on the path that writes no row.
        c.maybe_update_five_hour_block(
            _five_hour_saved_from_fold(
                fold, snapshot_id=saved.get("id"), capture_at=capture_at,
                five_hour_resets_at=five_hour_resets_at_str,
            ),
            conn=conn, as_of=capture_at, alert_sink=ctx.pending_alerts,
            account_key=account_key, journal_ctx=ctx)

    # 5. hwm-7d / hwm-5h projection files — WRITE paths only (the monotonic
    #    writer; a no-change tick's percent is already <= the stored HWM).
    #    In-place credit force-writes live in detect_reset_and_credit.
    #    #769 S11 (#824): the two axes have separate writers, and a HELD tick
    #    calls only the five-hour one. Its weekly value is carried-forward
    #    evidence, so it must never reach `hwm-7d` — and that is enforced by
    #    not calling the writer, not by trusting the monotonic guard to notice.
    if ctx.projection_writes:
        if wrote_row and not held_write:
            _write_hwm_weekly(week_start_date, weekly_percent)
        if wrote_row:
            _write_hwm_five_hour(five_hour_window_key, five_hour_percent)


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
