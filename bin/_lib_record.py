"""Record write-path decision kernels for cctally.

Pure-fn leaf (stdlib only, no I/O at import time): values in, decisions
out. Every DB read, config load, accessor reach (``c.<constant>``,
``c._is_reset_drop``, ``c._floor_to_ten_minutes``), SQL statement, file
write, and ``eprint`` stays in the I/O glue in ``bin/_cctally_record.py``
— this module never touches the ``cctally`` namespace. That single rule
is what preserves the entire ns-patch surface of ``cmd_record_usage`` /
``maybe_record_projected_alert`` while their decision cores move here.

Each kernel mirrors the exact comparison operators of the fragment it was
lifted from (``round(x, 1)`` on the HWM clamp, ``+ 1e-9`` snap on every
percent/threshold crossing, inclusive band bounds) so behavior is
byte-identical. Glue call site is named in each kernel's docstring.

Spec: docs/superpowers/specs/2026-07-09-279-s4-record-kernelization-design.md
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# ── Fragment 1: --resets-at / --five-hour-resets-at plausibility band ──────
def check_resets_at_plausibility(
    epoch: int, now_epoch: int, *, past_slack_s: int, future_band_s: int
) -> bool:
    """Return True when ``epoch`` sits inside the inclusive plausibility band
    ``[now_epoch - past_slack_s, now_epoch + future_band_s]``.

    Glue call sites (``cmd_record_usage``): the 7d leg (day-scale slack;
    out-of-band → eprint + exit 2) and the 5h leg (10-min-past / 6h-future;
    out-of-band → drop the 5h fields and continue). The two leg-specific
    eprint literals and the differing consequences stay in glue; only the
    raw second-granularity band check moves here.
    """
    return now_epoch - past_slack_s <= epoch <= now_epoch + future_band_s


# ── Fragment 2: weekly in-place-credit / reset-to-zero debounce ────────────
FIRE_IMMEDIATE = "fire_immediate"
CONFIRM_RESET = "confirm_reset"
CLEAR_MARKER = "clear_marker"
ARM_MARKER = "arm_marker"
NO_ACTION = "none"


@dataclass(frozen=True)
class WeeklyDebounceDecision:
    """Which weekly credit/reset-to-zero action ``cmd_record_usage``'s
    same-week (``prior_end == cur_end``) branch should take. ``action`` is one
    of the module constants FIRE_IMMEDIATE / CONFIRM_RESET / CLEAR_MARKER /
    ARM_MARKER / NO_ACTION — mirroring the branch outcomes at the glue site."""
    action: str


def plan_weekly_credit_debounce(
    prev_pct, new_pct, *, drop_threshold, zero_floor_pct, zero_min_drop_pct,
    marker_armed, marker_baseline,
):
    """Classify the same-window weekly-credit debounce decision (glue call
    site: ``cmd_record_usage`` under ``prior_end_canon == cur_end_canon`` and
    ``prior_end_dt > now_utc and prior_pct is not None``).

    Mirrors the branch structure exactly:
      - ``big_drop`` (drop >= drop_threshold) → FIRE_IMMEDIATE (>=25pp goodwill
        credit; fires now, never debounced; glue also clears any pending arm).
      - else, marker armed for this window:
          - ``new_pct <= marker_baseline / 2.0`` → CONFIRM_RESET (stayed low).
          - else → CLEAR_MARKER (recovered toward baseline → transient zero).
      - else, ``zero_only`` (not big_drop AND new_pct <= zero_floor_pct AND
        drop >= zero_min_drop_pct) → ARM_MARKER (first ~0).
      - else → NO_ACTION.

    Glue reads the ``c._RESET_*`` constants + the marker file, computes
    ``marker_armed`` (window-key match) and passes ``marker_baseline``
    (marker[2] when armed), then executes the decided I/O.
    """
    drop = float(prev_pct) - float(new_pct)
    big_drop = drop >= drop_threshold
    zero_only = (
        (not big_drop)
        and float(new_pct) <= zero_floor_pct
        and drop >= zero_min_drop_pct
    )
    if big_drop:
        return WeeklyDebounceDecision(FIRE_IMMEDIATE)
    if marker_armed:
        if float(new_pct) <= marker_baseline / 2.0:
            return WeeklyDebounceDecision(CONFIRM_RESET)
        return WeeklyDebounceDecision(CLEAR_MARKER)
    if zero_only:
        return WeeklyDebounceDecision(ARM_MARKER)
    return WeeklyDebounceDecision(NO_ACTION)


# ── Fragment 3: 5h in-place-credit detection guard ─────────────────────────
def plan_five_hour_credit(
    prior_pct: float, new_pct: float, *, drop_threshold: float,
    prior_resets_in_future: bool,
) -> bool:
    """Return True when a 5h in-place credit is detected (glue call site:
    ``cmd_record_usage``'s 5h-detection block).

    Mirrors the one-line guard ``prior_5h_resets_dt > now_utc and
    (prior_5h_pct - five_hour_percent) >= threshold``. ``is_dup`` is NOT an
    input (gate P3-7): it gates only the glue INSERT, while the pivots
    (hwm-5h force-write, stale-replica DELETE) fire unconditionally once a
    credit is detected — all of that stays in glue.
    """
    return prior_resets_in_future and (prior_pct - new_pct) >= drop_threshold


# ── Fragment 3b: 5h source-local confirmation state machine (#769 S2 §3) ───
FIVE_HOUR_HOLD = "hold"
FIVE_HOUR_SET_BASELINE = "set_baseline"
FIVE_HOUR_ARM = "arm"
FIVE_HOUR_CONFIRM = "confirm"
FIVE_HOUR_CANCEL = "cancel"


@dataclass(frozen=True)
class FiveHourSourceDecision:
    """What one contributor's observation does to that contributor's own
    five-hour credit state.

    ``action`` is one of the FIVE_HOUR_* constants above. ``baseline_pct`` is
    the baseline to persist afterwards, and is ``None`` exactly when the action
    writes no baseline (FIVE_HOUR_HOLD).

    ``credit_prior_pct`` and ``credit_post_pct`` are set only on
    FIVE_HOUR_CONFIRM and are the two ends of the credit this decision found:
    the pre-drop baseline and the armed low that dropped away from it. They are
    what the credit event records as ``prior_percent`` and ``post_percent``.
    Both belong to the ARMING observation, because that is the observation that
    saw the drop; the confirming observation only establishes that the drop was
    not a single stale reading, and its own percent is not one end of the
    credit. Binding ``post_percent`` to the confirming percent instead records
    a drop that can be arbitrarily smaller than the eligibility threshold the
    arming leg required — down to nothing at all, since confirmation asks only
    for a reading below the baseline — which is what `R-5HC1` in
    ``bin/cctally-reconcile-test`` forbids.
    """
    action: str
    baseline_pct: float | None = None
    credit_prior_pct: float | None = None
    credit_post_pct: float | None = None


def plan_five_hour_source_local_credit(
    *, baseline_pct, pending_low_pct, pending_observation_id,
    new_pct, observation_id, drop_threshold, window_live,
):
    """Classify one observation against ONE contributor's own five-hour state
    (glue call site: ``detect_reset_and_credit``'s 5h branch).

    The rule this implements (#769 S2 §3, issue #751): a credit needs a
    same-source observed descent followed by a distinct same-source
    confirmation that is still below that source's own pre-drop baseline,
    inside one physical window. Detection used to compare an incoming reading
    against the latest accepted snapshot whatever contributor produced it, so a
    single stale ``source=statusline`` sample below an ``source=api`` baseline
    fabricated a credit with no confirmation from any source.

    All inputs are that ONE source's own state; the caller selects the row by
    ``(account_key, five_hour_window_key, source)`` and therefore cannot pass
    another contributor's evidence in. ``baseline_pct is None`` means the
    source has no state for this window — a fresh window, or state a rebuild
    dropped.

    ``drop_threshold`` stays ELIGIBILITY only. It decides whether a decrease is
    large enough to be worth arming; it is not evidence of freshness, and there
    is deliberately no maximum gap, no maximum climb and no confirmation-value
    band, because observations arrive at an unbounded interval and every finite
    threshold on the change between two of them has a legitimate crossing.
    """
    new_pct = float(new_pct)

    if baseline_pct is None:
        # No state for this source in this window. Establish the baseline and
        # emit nothing: a first reading is not a descent, whatever any other
        # contributor has already reported.
        return FiveHourSourceDecision(FIVE_HOUR_SET_BASELINE, new_pct)

    baseline_pct = float(baseline_pct)

    if pending_low_pct is not None:
        # An armed descent from this source.
        if (
            observation_id is not None
            and pending_observation_id is not None
            and observation_id == pending_observation_id
        ):
            # The arming observation replayed. It cannot confirm itself, so
            # leave the state armed and write nothing — the same rule the
            # weekly debounce applies at `first_zero_observation_id`.
            return FiveHourSourceDecision(FIVE_HOUR_HOLD)
        if window_live and new_pct < baseline_pct:
            # A distinct same-source observation, still below the pre-drop
            # baseline, inside the live window. This is the confirmation. The
            # baseline to persist afterwards is this observation's own percent
            # — the source's current level — while the credit itself is the
            # arming observation's descent from `baseline_pct` to
            # `pending_low_pct`.
            return FiveHourSourceDecision(
                FIVE_HOUR_CONFIRM, new_pct, baseline_pct,
                float(pending_low_pct))
        # Back at or above the baseline (or the window is no longer live):
        # the descent was not sustained, so the candidate is cancelled.
        return FiveHourSourceDecision(
            FIVE_HOUR_CANCEL, max(baseline_pct, new_pct))

    if plan_five_hour_credit(
        baseline_pct, new_pct,
        drop_threshold=drop_threshold, prior_resets_in_future=window_live,
    ):
        return FiveHourSourceDecision(FIVE_HOUR_ARM, baseline_pct)

    if new_pct > baseline_pct:
        return FiveHourSourceDecision(FIVE_HOUR_SET_BASELINE, new_pct)

    # Equal, or a decrease too small to be eligible. The baseline is this
    # source's running maximum inside the window, which is what the write-time
    # MAX clamp already makes the accepted snapshots behave like, so a
    # sub-threshold dip does not lower it and a later cumulative drop past the
    # threshold still arms.
    return FiveHourSourceDecision(FIVE_HOUR_HOLD)


# ── Fragment 4: reset-aware HWM clamp comparison ───────────────────────────
def hwm_clamp_applies(incoming_pct: float, recorded_max_pct) -> bool:
    """Return True when ``incoming_pct`` is below the reset-aware recorded MAX
    at tenths granularity (``round(x, 1)`` on both sides), i.e. the clamp fires.

    Glue call sites (``cmd_record_usage``), each with a DISTINCT consequence
    the glue keeps: the 7d leg sets ``should_insert = False`` (suppresses the
    row); the 5h leg — NESTED inside the 7d ``else:`` — clamps the value up
    (``five_hour_percent = float(max_5h_row["v"])``) and never touches
    ``should_insert``. ``recorded_max_pct`` is the MAX cell (may be ``None``
    when there is no in-window row); the SELECTs (including
    ``_reset_aware_floor``) stay in glue verbatim.
    """
    if recorded_max_pct is None:
        return False
    return round(incoming_pct, 1) < round(float(recorded_max_pct), 1)


# ── Fragment 5 (residue): self-heal milestone-coverage predicate ───────────
def milestone_coverage_owes(existing_max_threshold, floor: int) -> bool:
    """Return True when the milestone ledger for the ACTIVE segment owes a
    heal — no rows yet (``existing_max_threshold is None``) or the highest
    recorded threshold sits below the latest floor.

    The load-bearing decision repeated at BOTH self-heal milestone-coverage
    probes in ``cmd_record_usage``'s dedup self-heal block (weekly Probe 1 and
    the 5h probe's milestone-coverage else-leg). Glue runs the DB probes,
    reduces each to ``existing_max_threshold`` (int or None), and OR-s the
    result into ``need_milestone_heal`` / ``need_5h_heal``. The broader
    ``assess_self_heal`` aggregate stays glue-only (gate P3-5): its need-flags
    are thin residues interleaved with four nesting levels of DB probes and
    the staleness checks (``block_row is None`` / ``last_observed <
    captured``), which are not cleanly separable without moving I/O.
    """
    return existing_max_threshold is None or existing_max_threshold < floor


# ── Fragment 6: hwm-7d / hwm-5h monotonic file step ────────────────────────
def hwm_file_next(existing, incoming: float):
    """Return the value to write to the HWM file, or ``None`` when the write
    should be skipped (glue call sites: ``cmd_record_usage``'s hwm-7d and
    hwm-5h writers).

    Mirrors the real ``>=`` operator: write when ``incoming >= existing``
    (equality rewrites the same bytes — the code writes, so this returns the
    value, not None). ``existing is None`` (no prior value) always writes. The
    file read/parse and the actual ``write_text`` stay in glue.
    """
    if existing is None or incoming >= existing:
        return incoming
    return None


# ── Fragment 7: projected-pace alert threshold crossings ───────────────────
def projected_crossings(value: float, levels) -> list:
    """Return the threshold labels crossed by ``value`` at the ``+ 1e-9`` snap.

    ``levels`` is a list of ``(threshold_label, comparand)`` pairs — glue
    pre-scales each comparand per leg (weekly_pct: ``(t, float(t))``; the two
    budget legs: ``(t, (t / 100.0) * float(target))``), so this kernel never
    rescales. A label crosses when ``value + 1e-9 >= comparand``. Glue maps
    the returned labels back into the per-leg ``pending.append(dict(...))``
    bodies; the leg-level ``_projected_levels_already_latched`` pre-gate stays
    in glue (gate P2-2 — it is a per-leg gate BEFORE the loop, not a
    per-threshold filter).
    """
    return [t for (t, comparand) in levels if value + 1e-9 >= comparand]


# ── Fragment 8: usage-snapshot fold outcome classification ─────────────────
#: RETAINED LEGACY SPELLINGS with no live consumer (#769 S11, #824).
#:
#: These were the three outcomes of ``_usage_snapshot_fold_decision`` while it
#: returned a positional ``(skip, adjusted, reason)`` tuple. It now returns
#: ``UsageSnapshotFoldResult``, which states the weekly and five-hour axes
#: separately, and the weekly milestone gate reads that result's held flag
#: rather than comparing a reason string. Nothing outside this module and its
#: own test reads these names today.
#:
#: They are kept rather than deleted because an out-of-tree reader could still
#: import them, and because the distinction they encode is still true of the
#: system: a dedup outcome means the incoming observation AGREES with the stored
#: row, while a clamp outcome means it CONTRADICTS it, and only the second must
#: suppress a weekly milestone. Deleting them is a behaviour-neutral cleanup,
#: not part of this change.
SNAPSHOT_ACCEPT = "accept"
SNAPSHOT_SKIP_CLAMP = "clamp"
SNAPSHOT_SKIP_DEDUP = "dedup"


# ── Fragment 9: post-reset milestone-ladder seeding evidence ───────────────
def post_reset_seed_has_climb_evidence(lowest_in_epoch_pct, current_floor: int) -> bool:
    """Return True when a post-reset epoch may seed its milestone ladder at
    ``current_floor``.

    ``lowest_in_epoch_pct`` is the smallest ``weekly_percent`` stored for this
    week and account at-or-after the governing reset event's effective instant
    (``None`` when the epoch holds no observation at all). The seed is allowed
    only when some in-epoch observation floors STRICTLY below the threshold
    being recorded — the observable evidence that the counter climbed from the
    reset to here.

    Glue call site: ``maybe_record_milestone`` (bin/_cctally_record.py), on the
    ``reset_event_id != 0 and max_existing is None`` branch only. The ``+ 1e-9``
    snap matches the one the glue applies to ``current_floor`` itself, so a
    percent one ULP below an integer classifies the same on both sides.

    The ``None`` leg is the kernel's own contract for an empty epoch, not a
    frequently-taken branch: at that call site the triggering capture's own row
    usually sits inside the query window and makes the ``MIN`` non-NULL. It is
    still reachable, because the window's upper bound falls back to ``as_of``
    when ``saved`` carries no ``capturedAt``, and because the stale-replica
    DELETE can remove the row between the snapshot write and this read. An
    epoch holding no stored observation has observed no climb, so it refuses.
    """
    if lowest_in_epoch_pct is None:
        return False
    return math.floor(float(lowest_in_epoch_pct) + 1e-9) < current_floor
