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
