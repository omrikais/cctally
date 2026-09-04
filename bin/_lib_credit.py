"""Credit-plan decision kernel for cctally's record-credit path.

Pure-fn layer (no I/O at import time): holds the percent-ingress
sanitizer and the `record-credit` plan builder — values in, a frozen
`CreditPlan` (or a `ValueError`) out. Every DB read, file write, and
`eprint` for the in-place-credit machinery stays in the I/O sibling
`bin/_cctally_record.py`; this module is imported by that sibling (and
re-exported on the `cctally` namespace via `bin/cctally`), so the
existing `ns["_build_credit_plan"]` / `ns["_normalize_percent"]` /
`cctally.CreditPlan` read paths resolve unchanged.

`_normalize_percent` is the single chokepoint that flushes IEEE 754 ULP
noise out of ingress percents; `_build_credit_plan` validates the
record-credit inputs and floors the effective moment to the hour. Both
are pure and stdlib-only apart from two kernel imports
(`parse_iso_datetime`, `_canonicalize_optional_iso` from `_cctally_core`)
and the pure hour-floor from `_lib_blocks` — no cross-sibling I/O and no
`cctally` back-import.

Spec: docs/superpowers/specs/2026-07-09-279-s4-record-kernelization-design.md
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from _cctally_core import parse_iso_datetime, _canonicalize_optional_iso
from _lib_blocks import _floor_to_hour


_PERCENT_NORMALIZE_DECIMALS = 10


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


@dataclass(frozen=True)
class CreditPlan:
    week_start_date: str
    week_start_at: str
    week_end_at: str
    cur_end_canon: str
    from_pct: float
    from_source: str          # "hwm" | "explicit" | "prior_credit"
    to_pct: float
    effective_iso: str        # weekly_credit_floors.effective_at_utc (floored to hour)
    captured_iso: str         # synthetic snapshot captured_at_utc (un-floored), 'Z'


def _parse_credit_at(value, now):
    """Parse --at as an aware UTC datetime; default to `now`. Naive => UTC
    (mirrors bin/_cctally_five_hour.py:88), NOT parse_iso_datetime (host-local)."""
    if value is None:
        return now
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"--at: {e}") from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _build_credit_plan(*, week_start_date, week_start_at, week_end_at,
                       from_pct, from_source, to_pct, at_dt, now,
                       effective_override=None):
    """Validate inputs and build a CreditPlan. Pure (no DB/file I/O).
    Raises ValueError(msg) on any violation — caller maps to exit 2.

    `effective_override` (an ISO string) is the completion-path reuse of an
    EXISTING `weekly_credit_floors.effective_at_utc` (spec §4a): when present,
    the plan's effective is that value rather than `floor_to_hour(at)`, so a
    rerun of a half-applied credit at a later wall-clock keeps the original
    floor moment and never leaks a stale pre-credit replay into the floored
    MAX. The synthetic snapshot's captured timestamp stays the un-floored
    `at` either way."""
    to_pct = _normalize_percent(to_pct)
    from_pct = _normalize_percent(from_pct)
    # Defensive None-guard (issue #212 N3). `_normalize_percent` returns None
    # for a None input. The CLI never reaches here with None (`--to` is
    # required + type=float; `--from` always resolves to a float or the caller
    # errors out first), but this is a public pure helper called directly by
    # tests and any future non-CLI caller — surface a None as a clear ValueError
    # (caller -> exit 2) instead of a TypeError from the `0.0 <= None` compare
    # immediately below.
    if to_pct is None or from_pct is None:
        raise ValueError("--to/--from must be numeric")
    if not (0.0 <= to_pct <= 100.0) or not (0.0 <= from_pct <= 100.0):
        raise ValueError("--to/--from must be in [0, 100]")
    if to_pct >= from_pct:
        raise ValueError(f"not a credit: to >= from ({to_pct} >= {from_pct})")
    ws = parse_iso_datetime(week_start_at, "week_start_at")
    we = parse_iso_datetime(week_end_at, "week_end_at")
    if not (ws <= at_dt < we):
        raise ValueError(f"--at {at_dt.isoformat()} is outside the week window")
    if at_dt > now:
        raise ValueError("--at is in the future")
    if effective_override is not None:
        effective_iso = parse_iso_datetime(
            effective_override, "effective_override"
        ).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    else:
        effective_iso = _floor_to_hour(at_dt).isoformat(timespec="seconds")
    cur_end_canon = _canonicalize_optional_iso(week_end_at, "record-credit.week_end")
    return CreditPlan(
        week_start_date=week_start_date,
        week_start_at=week_start_at,
        week_end_at=week_end_at,
        cur_end_canon=cur_end_canon,
        from_pct=from_pct,
        from_source=from_source,
        to_pct=to_pct,
        effective_iso=effective_iso,
        captured_iso=at_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
