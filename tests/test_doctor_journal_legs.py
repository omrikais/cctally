"""doctor journal legs (DB journal redesign §9) — pure kernel, no DB/journal.

The four legs classify a constructed ``DoctorState`` over precomputed journal
evidence (the gather layer does the I/O). Covered states: no-journal (legacy
pre-cutover), healthy, un-writable dir, torn tail, mid-file malformed, stale
cursor, and a recent auto-heal incident.

`_lib_doctor` is imported off ``bin/`` — the same path mechanism the other
in-process doctor kernel tests use.
"""
import pathlib
import sys

from conftest import load_script


sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))


def _state(**overrides):
    import _lib_doctor
    from dataclasses import fields

    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(overrides)
    return _lib_doctor.DoctorState(**kwargs)


# ── presence ──────────────────────────────────────────────────────────────

def test_presence_no_journal_is_ok_not_fail():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(_state(journal_present=False))
    assert r.severity == "ok" and r.id == "journal.presence"
    assert "pre-cutover" in r.summary


def test_presence_writable_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(
        _state(journal_present=True, journal_appendable=True,
               journal_segment_count=3))
    assert r.severity == "ok"
    assert "3 segment" in r.summary


def test_presence_not_writable_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_presence(
        _state(journal_present=True, journal_appendable=False,
               journal_segment_count=2))
    assert r.severity == "warn"
    assert "not writable" in r.summary


# ── integrity ─────────────────────────────────────────────────────────────

def test_integrity_not_scanned_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=None))
    assert r.severity == "ok" and "not scanned" in r.summary


def test_integrity_malformed_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=2,
               journal_torn_tail_count=0))
    assert r.severity == "warn"
    assert "2 malformed" in r.summary
    assert r.details["malformed"] == 2


def test_integrity_torn_tail_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=0,
               journal_torn_tail_count=1))
    assert r.severity == "ok"
    assert "torn tail" in r.summary


def test_integrity_clean_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_integrity(
        _state(journal_present=True, journal_malformed_count=0,
               journal_torn_tail_count=0))
    assert r.severity == "ok" and "no malformed" in r.summary


# ── index freshness (cursor lag) ────────────────────────────────────────────

def test_index_freshness_caught_up_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=0))
    assert r.severity == "ok" and "caught up" in r.summary


def test_index_freshness_small_gap_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=1024))
    assert r.severity == "ok"
    assert "within threshold" in r.summary


def test_index_freshness_large_gap_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True,
               journal_cursor_lag_bytes=_lib_doctor._JOURNAL_CURSOR_LAG_WARN_BYTES + 1))
    assert r.severity == "warn"
    assert "behind journal" in r.summary
    assert "db rebuild --db stats" in (r.remediation or "")


def test_index_freshness_no_cursor_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_index_freshness(
        _state(journal_present=True, journal_cursor_lag_bytes=None))
    assert r.severity == "ok" and "no cursor" in r.summary


# ── auto-heal incidents ─────────────────────────────────────────────────────

def test_auto_heal_none_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(_state(journal_heal_incidents=[]))
    assert r.severity == "ok" and "no auto-heal" in r.summary


def test_auto_heal_recent_warns():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "quarantine", "name": "stats.db-20260720T120000Z",
             "age_s": 2 * 86400}]))
    assert r.severity == "warn"
    assert "fired recently" in r.summary


def test_auto_heal_old_is_ok():
    import _lib_doctor
    r = _lib_doctor._check_journal_auto_heal(
        _state(journal_heal_incidents=[
            {"kind": "forensics",
             "name": "stats.db-corruption-forensics-20260101T120000Z.json",
             "age_s": 30 * 86400}]))
    assert r.severity == "ok"
    assert "last incident" in r.summary
