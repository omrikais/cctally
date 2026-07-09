"""Direct unit tests for bin/_lib_credit.py (#279 S4 F1).

The credit-plan kernel (`_normalize_percent`, `_parse_credit_at`,
`_build_credit_plan`, `CreditPlan`) moved verbatim out of
`bin/_cctally_record.py` into the pure `bin/_lib_credit.py` home. These
tests exercise the kernel by importing it DIRECTLY, duplicating and
extending the `ns["_build_credit_plan"]` coverage in
`tests/test_record_credit.py` (which stays as the namespace-continuity
witness). The final identity test proves the re-export continuity trick:
`cctally._build_credit_plan IS _lib_credit._build_credit_plan`.
"""
from __future__ import annotations

import datetime as dt

import pytest

# conftest puts bin/ on sys.path.
import _lib_credit
from _lib_credit import (
    CreditPlan,
    _build_credit_plan,
    _normalize_percent,
    _parse_credit_at,
)
from conftest import load_script


NOW = dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc)
WS_AT = "2026-06-13T05:00:00+00:00"
WE_AT = "2026-06-20T05:00:00+00:00"


def _plan(**over):
    kw = dict(
        week_start_date="2026-06-13",
        week_start_at=WS_AT,
        week_end_at=WE_AT,
        from_pct=46.0,
        from_source="hwm",
        to_pct=31.0,
        at_dt=NOW,
        now=NOW,
    )
    kw.update(over)
    return _build_credit_plan(**kw)


# ── _normalize_percent ─────────────────────────────────────────────────


def test_normalize_percent_flushes_ulp_noise():
    # 0.57 * 100 == 56.99999999999999; the 10dp round flushes the ULP noise.
    assert _normalize_percent(56.99999999999999) == 57.0


def test_normalize_percent_none_passthrough():
    assert _normalize_percent(None) is None


def test_normalize_percent_accepts_int():
    assert _normalize_percent(46) == 46.0


# ── _parse_credit_at ───────────────────────────────────────────────────


def test_parse_credit_at_naive_is_utc():
    d = _parse_credit_at("2026-07-01T10:00:00", NOW)
    assert d.tzinfo is not None and d.utcoffset().total_seconds() == 0
    assert d == dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc)


def test_parse_credit_at_default_is_now():
    assert _parse_credit_at(None, NOW) == NOW


def test_parse_credit_at_offset_normalized_to_utc():
    # +02:00 wall clock 12:00 == 10:00Z
    d = _parse_credit_at("2026-07-01T12:00:00+02:00", NOW)
    assert d == dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc)


# ── _build_credit_plan ─────────────────────────────────────────────────


def test_build_credit_plan_happy_path():
    p = _plan()
    assert isinstance(p, CreditPlan)
    assert p.to_pct == 31.0 and p.from_pct == 46.0
    assert p.effective_iso == "2026-06-19T14:00:00+00:00"   # floored to hour
    assert p.captured_iso == "2026-06-19T14:37:00Z"          # un-floored now, Z
    assert p.cur_end_canon == "2026-06-20T05:00:00+00:00"
    assert p.from_source == "hwm"


def test_build_credit_plan_rejects_to_ge_from():
    with pytest.raises(ValueError, match="not a credit"):
        _plan(to_pct=46.0)


def test_build_credit_plan_rejects_out_of_range():
    with pytest.raises(ValueError):
        _plan(to_pct=-1.0)
    with pytest.raises(ValueError):
        _plan(from_pct=120.0)


def test_build_credit_plan_rejects_none_pct():
    with pytest.raises(ValueError, match="numeric"):
        _plan(to_pct=None)
    with pytest.raises(ValueError, match="numeric"):
        _plan(from_pct=None)


def test_build_credit_plan_rejects_future_at():
    with pytest.raises(ValueError, match="future"):
        _plan(at_dt=NOW + dt.timedelta(hours=1))


def test_build_credit_plan_rejects_at_outside_window():
    with pytest.raises(ValueError, match="window"):
        _plan(at_dt=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc),
              now=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc))


def test_build_credit_plan_effective_override_reuses_existing_floor():
    p = _plan(effective_override="2026-06-19T11:00:00+00:00")
    assert p.effective_iso == "2026-06-19T11:00:00+00:00"
    # captured stays the un-floored `at` regardless of the override.
    assert p.captured_iso == "2026-06-19T14:37:00Z"


# ── continuity trick: the cctally namespace re-export IS the kernel fn ──


def test_credit_kernel_identity_via_namespace():
    ns = load_script()
    assert ns["_build_credit_plan"] is _lib_credit._build_credit_plan
    assert ns["_normalize_percent"] is _lib_credit._normalize_percent
    assert ns["CreditPlan"] is _lib_credit.CreditPlan
    assert ns["_parse_credit_at"] is _lib_credit._parse_credit_at
    assert ns["_PERCENT_NORMALIZE_DECIMALS"] == _lib_credit._PERCENT_NORMALIZE_DECIMALS
