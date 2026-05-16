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
  subsystems, or constants that belong in bin/cctally. All accessed
  via the same shim/``c.X`` pattern.

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


# Module-level back-ref shims. Each shim resolves
# ``sys.modules['cctally'].X`` at CALL TIME (not bind time), so
# monkeypatches on cctally's namespace propagate into the moved code
# unchanged. Mirrors the precedent established in
# ``bin/_cctally_cache.py`` and ``bin/_cctally_db.py``.
def eprint(*args, **kwargs):
    return sys.modules["cctally"].eprint(*args, **kwargs)


def now_utc_iso(*args, **kwargs):
    return sys.modules["cctally"].now_utc_iso(*args, **kwargs)


def parse_iso_datetime(*args, **kwargs):
    return sys.modules["cctally"].parse_iso_datetime(*args, **kwargs)


def open_db(*args, **kwargs):
    return sys.modules["cctally"].open_db(*args, **kwargs)


def open_cache_db(*args, **kwargs):
    return sys.modules["cctally"].open_cache_db(*args, **kwargs)


def sync_cache(*args, **kwargs):
    return sys.modules["cctally"].sync_cache(*args, **kwargs)


def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


def get_week_start_name(*args, **kwargs):
    return sys.modules["cctally"].get_week_start_name(*args, **kwargs)


def compute_week_bounds(*args, **kwargs):
    return sys.modules["cctally"].compute_week_bounds(*args, **kwargs)


def parse_date_str(*args, **kwargs):
    return sys.modules["cctally"].parse_date_str(*args, **kwargs)


def _canonicalize_optional_iso(*args, **kwargs):
    return sys.modules["cctally"]._canonicalize_optional_iso(*args, **kwargs)


def _canonical_5h_window_key(*args, **kwargs):
    return sys.modules["cctally"]._canonical_5h_window_key(*args, **kwargs)


def _floor_to_hour(*args, **kwargs):
    return sys.modules["cctally"]._floor_to_hour(*args, **kwargs)


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


def make_week_ref(*args, **kwargs):
    return sys.modules["cctally"].make_week_ref(*args, **kwargs)


def cmd_sync_week(*args, **kwargs):
    return sys.modules["cctally"].cmd_sync_week(*args, **kwargs)


def _calculate_entry_cost(*args, **kwargs):
    return sys.modules["cctally"]._calculate_entry_cost(*args, **kwargs)


def get_claude_session_entries(*args, **kwargs):
    return sys.modules["cctally"].get_claude_session_entries(*args, **kwargs)


def _resolve_primary_model_for_block(*args, **kwargs):
    return sys.modules["cctally"]._resolve_primary_model_for_block(*args, **kwargs)


def _resolve_display_tz_obj(*args, **kwargs):
    return sys.modules["cctally"]._resolve_display_tz_obj(*args, **kwargs)


def _build_alert_payload_weekly(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_weekly(*args, **kwargs)


def _build_alert_payload_five_hour(*args, **kwargs):
    return sys.modules["cctally"]._build_alert_payload_five_hour(*args, **kwargs)


def _dispatch_alert_notification(*args, **kwargs):
    return sys.modules["cctally"]._dispatch_alert_notification(*args, **kwargs)


def _get_alerts_config(*args, **kwargs):
    return sys.modules["cctally"]._get_alerts_config(*args, **kwargs)


def _warn_alerts_bad_config_once(*args, **kwargs):
    return sys.modules["cctally"]._warn_alerts_bad_config_once(*args, **kwargs)


def _get_oauth_usage_config(*args, **kwargs):
    return sys.modules["cctally"]._get_oauth_usage_config(*args, **kwargs)


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


# Constants referenced by the moved bodies. Defined here (rather than
# `c.X`-routed) because they're pure literals — no monkeypatch surface
# and no dependency on cctally's module instance.
_PERCENT_NORMALIZE_DECIMALS = 10


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
# Path constants (APP_DIR, HOOK_TICK_*) are accessed via the
# `c = _cctally()` call-time accessor inside each function that needs
# them — so ``monkeypatch.setitem(ns, "APP_DIR", tmp)`` in tests
# resolves on every read (no stale module-level binding).
#
# Constants pulled from cctally at call time:
#   c._FIVE_HOUR_JITTER_FLOOR_SECONDS — _lib_five_hour.* re-export
#   c._RESET_PCT_DROP_THRESHOLD       — bin/cctally module-level constant
#   c.HOOK_TICK_LOG_DIR / _PATH / _ROTATED_PATH / _ROTATE_BYTES
#   c.HOOK_TICK_THROTTLE_PATH / _LOCK_PATH
#   c.HOOK_TICK_DEFAULT_THROTTLE_SECONDS
#   c.APP_DIR


def _normalize_percent(value: "float | int | None") -> "float | None":
    """Flush IEEE 754 ULP noise out of an ingress percent value.

    Single chokepoint applied at every site where a raw percent enters
    cctally's runtime path (OAuth fetch, hook-tick OAuth refresh, and
    the cmd_record_usage CLI ingress). Downstream consumers — HWM
    files, ``weekly_usage_snapshots.{weekly,five_hour}_percent`` REAL
    columns, ``five_hour_blocks.final_five_hour_percent``, milestone
    crossing values, and the SSE envelope's ``used_percent`` field —
    all read the cleaned value, so a single round here stops
    ``5h=7.000000000000001`` style strings from reaching any log or
    serialized surface.

    ``None`` is the canonical absent-percent sentinel; preserve it
    unchanged so the optional-5h branches stay simple.
    """
    if value is None:
        return None
    return round(float(value), _PERCENT_NORMALIZE_DECIMALS)


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
        except sys.modules["cctally"]._AlertsConfigError as exc:
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
        except sys.modules["cctally"]._AlertsConfigError as exc:
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
        conn.execute("BEGIN")
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

                if max_existing is None:
                    start_threshold = current_floor   # first observation: only current floor
                else:
                    start_threshold = int(max_existing) + 1

                if start_threshold <= current_floor:
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

                    for pct in range(start_threshold, current_floor + 1):
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

    five_hour_percent: float | None = None
    five_hour_resets_at_str: str | None = None
    five_hour_window_key: int | None = None
    five_hour_resets_at_epoch: int | None = None
    if args.five_hour_percent is not None:
        five_hour_percent = _normalize_percent(args.five_hour_percent)
    if args.five_hour_resets_at is not None:
        five_hour_resets_at_epoch = int(args.five_hour_resets_at)
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
                now_utc = dt.datetime.now(dt.timezone.utc)
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
                        and (float(prior_pct) - float(weekly_percent)) >= c._RESET_PCT_DROP_THRESHOLD
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
                    # In-place credit branch (v1.7.2). When `resets_at` stays
                    # unchanged but `weekly_percent` drops by RESET_PCT_DROP_THRESHOLD
                    # or more, Anthropic has issued a goodwill in-place weekly
                    # credit. Emit one week_reset_events row keyed on the
                    # current end_at (old == new) so the reset-aware clamp
                    # above and the milestone segment writer can pivot to
                    # the post-credit segment. The seed snapshot lands via
                    # the now-reset-aware clamp on this same call.
                    prior_end_dt = parse_iso_datetime(prior_end_canon, "prior.week_end_at")
                    if (
                        prior_end_dt > now_utc
                        and prior_pct is not None
                        and (float(prior_pct) - float(weekly_percent)) >= c._RESET_PCT_DROP_THRESHOLD
                    ):
                        # Pre-check (Q5 belt-and-suspenders): suppress duplicate
                        # event rows for the same new_week_end_at across
                        # consecutive ticks. UNIQUE(old, new) at the DDL
                        # also catches the duplicate in the (old == new) case,
                        # but the pre-check avoids a useless write attempt
                        # and keeps the log clean. After the seed lands at
                        # post-credit %, the next tick's `prior_pct` will be
                        # the post-credit value so the drop predicate alone
                        # also suffices — pre-check is belt-and-suspenders.
                        already = conn.execute(
                            "SELECT 1 FROM week_reset_events "
                            "WHERE new_week_end_at = ? LIMIT 1",
                            (cur_end_canon,),
                        ).fetchone()
                        effective_dt = _floor_to_hour(now_utc)
                        effective_iso = effective_dt.isoformat(timespec="seconds")
                        if already is None:
                            # Row shape: old=effective_iso, new=cur_end_canon
                            # (distinct values). The previous shape stored
                            # old==new==cur_end_canon, which let BOTH
                            # _apply_reset_events_to_weekrefs maps
                            # (pre_map[old] and post_map[new]) fire on the
                            # SAME WeekRef — pre_map rewrote week_end_at to
                            # effective, post_map rewrote week_start_at to
                            # effective, collapsing the credited week to a
                            # zero-width window in downstream renders. With
                            # old==effective and new==cur_end_canon, only
                            # post_map fires on the credited week (setting
                            # week_start_at = effective, the intended
                            # behavior); pre_map keys on effective_iso and
                            # finds no matching WeekRef in practice. The
                            # UNIQUE(old, new) constraint permits this
                            # row, and the pre-check above keys on
                            # new_week_end_at so dedup still works.
                            conn.execute(
                                "INSERT OR IGNORE INTO week_reset_events "
                                "(detected_at_utc, old_week_end_at, new_week_end_at, "
                                " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
                                (now_utc_iso(), effective_iso, cur_end_canon,
                                 effective_iso),
                            )
                            conn.commit()
                        # Pivots fire UNCONDITIONALLY whenever a credit
                        # is detected — they're NOT gated on
                        # ``already is None``. Memory
                        # ``project_dedup_must_not_gate_side_effects.md``:
                        # "Skipping a no-op INSERT must NOT skip
                        # milestones/rollups/alerts; prior run may have
                        # died mid-flight." Crash scenario: tick N
                        # committed the event row, then died before
                        # HWM + DELETE. Tick N+1's pre-check sees
                        # ``already`` non-None (the row IS in the
                        # table) and would skip the pivots, leaving
                        # the system wedged on pre-credit HWM + stale-
                        # replica rows. Pivots are individually
                        # idempotent (file overwrite + DELETE on stable
                        # predicate), so re-running them is safe.
                        # ``effective_iso`` is resolved above; on a
                        # recovery tick it lands on the SAME 10-min
                        # slot as the original (now_utc has drifted
                        # only seconds), so the DELETE predicate's
                        # ``unixepoch(captured_at_utc) >= unixepoch(?)``
                        # still matches every stale-replica row.
                        #
                        # Force-write hwm-7d so the next status-line
                        # render reflects the post-credit value. The
                        # monotonic guard at the normal write site
                        # (below) would refuse to decrease the file;
                        # this write is the credit-only escape hatch.
                        # Lands AFTER the conn.commit() so a concurrent
                        # record-usage reader doesn't see the new HWM
                        # before the event row is durable.
                        try:
                            (c.APP_DIR / "hwm-7d").write_text(
                                f"{week_start_date} {weekly_percent}\n"
                            )
                        except OSError:
                            pass

                        # Race-defensive cleanup. Between the moment
                        # Anthropic credited the user (effective_iso)
                        # and this code firing, the EXTERNAL
                        # claude-statusline tool can replay stale
                        # pre-credit `--percent` values (it has its
                        # own in-memory HWM cache and re-runs us once
                        # per status-line tick). Those replays land
                        # captured_at_utc >= effective_iso with
                        # weekly_percent == prior_pct (the pre-credit
                        # value), and they dominate the reset-aware
                        # clamp's MAX over the post-credit segment so
                        # legitimate fresh OAuth values are rejected.
                        # Strict equality (round(.,1)) keeps this
                        # narrow: we only delete rows whose percent
                        # exactly matches the pre-credit value we just
                        # observed — legitimate post-credit climbs
                        # past `prior_pct` (rare, but possible if the
                        # credit is small + activity is heavy) stay.
                        try:
                            conn.execute(
                                "DELETE FROM weekly_usage_snapshots "
                                "WHERE week_start_date = ? "
                                "  AND unixepoch(captured_at_utc) >= "
                                "      unixepoch(?) "
                                "  AND round(weekly_percent, 1) = "
                                "      round(?, 1)",
                                (week_start_date, effective_iso,
                                 float(prior_pct)),
                            )
                            conn.commit()
                        except sqlite3.DatabaseError as exc:
                            eprint(
                                "[record-usage] post-credit cleanup "
                                f"failed: {exc}"
                            )

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
                        if (
                            prior_5h_resets_dt > now_utc
                            and (prior_5h_pct - float(five_hour_percent))
                                >= c._FIVE_HOUR_RESET_PCT_DROP_THRESHOLD
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
                                (c.APP_DIR / "hwm-5h").write_text(
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
                            # HWM cache. Strict round-1 equality
                            # keeps the scope narrow — only rows
                            # whose five_hour_percent exactly matches
                            # the just-observed pre-credit value are
                            # removed. ``unixepoch()`` on both sides
                            # for offset robustness (Z vs +00:00).
                            try:
                                conn.execute(
                                    "DELETE FROM weekly_usage_snapshots "
                                    " WHERE five_hour_window_key = ? "
                                    "   AND unixepoch(captured_at_utc) "
                                    "       >= unixepoch(?) "
                                    "   AND round(five_hour_percent, 1) "
                                    "       = round(?, 1)",
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
            except (sqlite3.DatabaseError, ValueError) as exc:
                eprint(
                    f"[record-usage] 5h in-place-credit detection "
                    f"failed: {exc}"
                )
        except (sqlite3.DatabaseError, ValueError) as exc:
            eprint(f"[record-usage] reset-event detection failed: {exc}")

        # 7-day usage is monotonically non-decreasing within a billing week
        # — UNTIL Anthropic issues an in-place weekly credit. When a
        # week_reset_events row exists for THIS week_end_at, the MAX query
        # filters to samples captured at-or-after the segment's
        # effective_reset_at_utc so a fresh post-credit OAuth value (e.g.
        # 2%) lands instead of being held back by stale pre-credit history
        # (e.g. 67%). When no event row exists, COALESCE defaults to
        # epoch-zero so the filter is a no-op and legacy clamp behavior
        # is preserved byte-identically.
        # NB: comparison wrapped with ``unixepoch()`` on BOTH sides.
        # ``captured_at_utc`` is stored with `Z` suffix, but
        # ``effective_reset_at_utc`` may have a non-UTC offset on
        # historical backfill rows written before Bug 3 was fixed
        # (parse_iso_datetime returned host-local). Lex string compare
        # on mixed offsets silently mis-orders moments for non-UTC
        # hosts (CLAUDE.md gotcha: 5h-block cross-reset flag — "all
        # comparisons go through unixepoch(), NOT lex
        # BETWEEN/`<`/`>`"). Same rule applies here.
        max_row = conn.execute(
            """
            SELECT MAX(weekly_percent) AS v
              FROM weekly_usage_snapshots
             WHERE week_start_date = ?
               AND unixepoch(captured_at_utc) >= unixepoch(COALESCE(
                 (SELECT effective_reset_at_utc
                    FROM week_reset_events
                   WHERE new_week_end_at = ?
                   ORDER BY id DESC
                   LIMIT 1),
                 '1970-01-01T00:00:00Z'
               ))
            """,
            (week_start_date, week_end_at),
        ).fetchone()
        if max_row and max_row["v"] is not None and round(weekly_percent, 1) < round(float(max_row["v"]), 1):
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
                if (
                    max_5h_row
                    and max_5h_row["v"] is not None
                    and round(five_hour_percent, 1)
                        < round(float(max_5h_row["v"]), 1)
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
                    if max_existing is None or max_existing["m"] is None:
                        need_milestone_heal = True
                    elif int(max_existing["m"]) < latest_floor:
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
                                if (
                                    max_5h_existing is None
                                    or max_5h_existing["m"] is None
                                ):
                                    need_5h_heal = True
                                elif (
                                    int(max_5h_existing["m"])
                                    < latest_5h_floor
                                ):
                                    need_5h_heal = True
            finally:
                heal_conn.close()

            if need_milestone_heal or need_5h_heal:
                latest_saved = _saved_dict_from_usage_row(latest_row)
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
        except Exception as exc:
            eprint(f"[record-usage] self-heal lookup failed: {exc}")
        return 0

    payload = {
        "source": "statusline",
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

    # Write high-water mark so the status line never displays a regression.
    # The file contains "week_start_date weekly_percent" on one line.
    try:
        hwm_path = c.APP_DIR / "hwm-7d"
        existing_hwm = 0.0
        try:
            parts = hwm_path.read_text().strip().split()
            if len(parts) == 2 and parts[0] == week_start_date:
                existing_hwm = float(parts[1])
        except (FileNotFoundError, ValueError, OSError):
            pass
        if weekly_percent >= existing_hwm:
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
            hwm5_path = c.APP_DIR / "hwm-5h"
            existing_hwm5 = 0.0
            try:
                parts5 = hwm5_path.read_text().strip().split()
                if len(parts5) == 2 and parts5[0] == str(five_resets_key):
                    existing_hwm5 = float(parts5[1])
            except (FileNotFoundError, ValueError, OSError):
                pass
            if five_hour_percent >= existing_hwm5:
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
        c.HOOK_TICK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(c.HOOK_TICK_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
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
        size = c.HOOK_TICK_LOG_PATH.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if size <= c.HOOK_TICK_LOG_ROTATE_BYTES:
        return
    try:
        os.replace(c.HOOK_TICK_LOG_PATH, c.HOOK_TICK_LOG_ROTATED_PATH)
    except OSError:
        pass


def _hook_tick_throttle_age_seconds() -> float:
    """Return seconds since last successful OAuth fetch; +inf if never."""
    c = _cctally()
    try:
        mtime = c.HOOK_TICK_THROTTLE_PATH.stat().st_mtime
    except FileNotFoundError:
        return float("inf")
    except OSError:
        return float("inf")
    return max(0.0, time.time() - mtime)


def _hook_tick_throttle_touch() -> None:
    """Update mtime to now (creating the file if missing)."""
    c = _cctally()
    try:
        c.APP_DIR.mkdir(parents=True, exist_ok=True)
        c.HOOK_TICK_THROTTLE_PATH.touch(exist_ok=True)
        os.utime(c.HOOK_TICK_THROTTLE_PATH, None)
    except OSError:
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
    event: str, session: str, ingested: int, oauth_status: str, dur_ms: int
) -> str:
    ts = now_utc_iso()
    return (
        f"{ts} event={event:14s} session={session} "
        f"ingested={ingested} oauth={oauth_status} dur_ms={dur_ms}"
    )


def cmd_hook_tick(args: argparse.Namespace) -> int:
    """Per-fire hook runtime (Section 3 of onboarding spec).

    Normal mode: reads stdin, detaches stdout/stderr to log file, runs
    sync_cache + (throttled) OAuth refresh, writes one log line, returns 0
    UNCONDITIONALLY (even on internal failure — hook discipline).

    --explain mode: synchronous, prints to stdout, returns informative
    exit code.
    """
    c = _cctally()
    explain = bool(getattr(args, "explain", False))
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
    if not explain:
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
    if not explain:
        try:
            c.HOOK_TICK_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_fd = os.open(
                c.HOOK_TICK_LOG_PATH,
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
                ingested = int(stats.rows_inserted)
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
            c.APP_DIR.mkdir(parents=True, exist_ok=True)
            try:
                lock_fd = os.open(
                    c.HOOK_TICK_THROTTLE_LOCK_PATH,
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
    print(f"      → throttle file: {c.HOOK_TICK_THROTTLE_PATH}")
    if pre_age == float("inf"):
        print("      → mtime: (file absent)")
    else:
        print(f"      → mtime: {int(pre_age)}s ago")
    print(f"      → threshold: {int(throttle_seconds)}s → {decision}")
    print("[3/4] OAuth refresh")
    print(f"      → status: {oauth_status}")
    print(f"[4/4] Log written → {c.HOOK_TICK_LOG_PATH}")
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
