"""record-credit: pure helpers + cmd_record_credit integration."""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


NOW = dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc)
WS_AT = "2026-06-13T05:00:00+00:00"
WE_AT = "2026-06-20T05:00:00+00:00"


def _plan(ns, **over):
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
    return ns["_build_credit_plan"](**kw)


def test_parse_at_naive_is_utc(ns):
    got = ns["_parse_credit_at"]("2026-06-19T14:00", NOW)
    assert got == dt.datetime(2026, 6, 19, 14, 0, tzinfo=dt.timezone.utc)


def test_parse_at_default_is_now(ns):
    assert ns["_parse_credit_at"](None, NOW) == NOW


def test_build_plan_happy(ns):
    p = _plan(ns)
    assert p.to_pct == 31.0 and p.from_pct == 46.0
    assert p.effective_iso == "2026-06-19T14:00:00+00:00"   # floored to hour
    assert p.captured_iso == "2026-06-19T14:37:00Z"          # un-floored now, Z
    assert p.cur_end_canon == "2026-06-20T05:00:00+00:00"
    assert p.from_source == "hwm"


def test_build_plan_rejects_to_ge_from(ns):
    with pytest.raises(ValueError, match="not a credit"):
        _plan(ns, to_pct=46.0)


def test_build_plan_rejects_out_of_range(ns):
    with pytest.raises(ValueError):
        _plan(ns, to_pct=-1.0)
    with pytest.raises(ValueError):
        _plan(ns, from_pct=120.0)


def test_build_plan_rejects_future_at(ns):
    with pytest.raises(ValueError, match="future"):
        _plan(ns, at_dt=NOW + dt.timedelta(hours=1))


def test_build_plan_rejects_at_outside_window(ns):
    with pytest.raises(ValueError, match="window"):
        _plan(ns, at_dt=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc),
              now=dt.datetime(2026, 6, 12, 0, 0, tzinfo=dt.timezone.utc))
