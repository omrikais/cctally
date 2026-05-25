"""Issue #101 — ``cache-report --since/--until`` restore the pre-Session-A
``parse_iso_datetime`` second-chance.

The dual-form refactor (#86 Session A) replaced cache-report's date-only
fallthrough with a ``_parse_dual_form_date`` call that rejects anything
other than ``YYYY-MM-DD`` / ``YYYYMMDD`` — silently dropping the
space-separated datetimes and ISO week-dates that ``datetime.fromisoformat``
(and thus the old code) accepted. Option 1 restores the second-chance
while keeping the clearer dual-form diagnostic for genuine garbage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import conftest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"

# A tz-aware UTC "now" so --days anchoring is deterministic. The forms
# under test all supply --since explicitly, so the value only matters for
# the trailing-window default (which these tests don't assert on).
PINNED_NOW = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _mod():
    conftest.load_script()
    return sys.modules["cctally"]


def _args(since=None, until=None, days=7):
    return argparse.Namespace(since=since, until=until, days=days)


# ─── second-chance accepted forms (the regression) ────────────────────


def test_space_separated_since_parses_verbatim_time():
    """``--since '2026-05-01 12:30:00'`` parses; the explicit wall-clock
    time is preserved verbatim (NOT collapsed to midnight)."""
    mod = _mod()
    since, _ = mod._resolve_cache_report_window(
        _args(since="2026-05-01 12:30:00"),
        now_utc=PINNED_NOW, tz_name="Etc/UTC",
    )
    assert (since.year, since.month, since.day) == (2026, 5, 1)
    assert (since.hour, since.minute, since.second) == (12, 30, 0)


def test_iso_week_date_since_parses():
    """``--since 2026-W18-1`` parses to the Monday of ISO week 18, 2026
    (2026-04-27)."""
    mod = _mod()
    since, _ = mod._resolve_cache_report_window(
        _args(since="2026-W18-1"),
        now_utc=PINNED_NOW, tz_name="Etc/UTC",
    )
    assert (since.year, since.month, since.day) == (2026, 4, 27)


def test_space_separated_until_keeps_its_time_no_end_of_day():
    """A full datetime ``--until`` carries its own time — the date-only
    23:59:59.999999 end-of-day expansion is NOT applied (faithful to the
    pre-Session-A fallthrough, which returned parse_iso_datetime verbatim)."""
    mod = _mod()
    _, until = mod._resolve_cache_report_window(
        _args(since="2026-04-01", until="2026-05-01 12:30:00"),
        now_utc=PINNED_NOW, tz_name="Etc/UTC",
    )
    assert (until.hour, until.minute, until.second) == (12, 30, 0)
    assert until.microsecond == 0


# ─── total failure still gets the dual-form diagnostic ────────────────


def test_garbage_since_still_raises_dual_form_error(capsys):
    """Input that is neither dual-form nor ISO (``26-01-01``) keeps the
    clear centralized ``YYYY-MM-DD or YYYYMMDD`` message — NOT
    parse_iso_datetime's generic 'must be ISO datetime'."""
    mod = _mod()
    with pytest.raises(ValueError):
        mod._resolve_cache_report_window(
            _args(since="26-01-01"), now_utc=PINNED_NOW, tz_name="Etc/UTC",
        )
    err = capsys.readouterr().err
    assert "must be YYYY-MM-DD or YYYYMMDD format" in err, err
    assert "26-01-01" in err, err


# ─── normal date-only forms unchanged ─────────────────────────────────


def test_date_only_since_still_midnight():
    """``--since 2026-05-01`` (dual-form) still expands to midnight — the
    second-chance must not intercept inputs the dual-form parser accepts."""
    mod = _mod()
    since, _ = mod._resolve_cache_report_window(
        _args(since="2026-05-01"), now_utc=PINNED_NOW, tz_name="Etc/UTC",
    )
    assert (since.hour, since.minute, since.second, since.microsecond) == (
        0, 0, 0, 0,
    )


def test_date_only_until_still_end_of_day():
    """``--until 2026-05-01`` (dual-form) still expands to 23:59:59.999999."""
    mod = _mod()
    _, until = mod._resolve_cache_report_window(
        _args(since="2026-04-01", until="2026-05-01"),
        now_utc=PINNED_NOW, tz_name="Etc/UTC",
    )
    assert (until.hour, until.minute, until.second, until.microsecond) == (
        23, 59, 59, 999999,
    )


# ─── no spurious stderr line on a successful second-chance parse ──────


def test_no_spurious_stderr_on_successful_space_form(tmp_path):
    """The dual-form parser eprints before it raises; the fallthrough must
    attempt it SILENTLY so a successful parse_iso_datetime second-chance
    does not leak a 'must be YYYY-MM-DD' line to stderr (the double-eprint
    trap)."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "TZ": "Etc/UTC"}
    env.pop("XDG_DATA_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    r = subprocess.run(
        [sys.executable, str(CCTALLY), "cache-report",
         "--since", "2026-05-01 12:30:00"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert "must be YYYY-MM-DD or YYYYMMDD" not in r.stderr, r.stderr
    assert r.returncode in (0, 2), (r.returncode, r.stderr)
