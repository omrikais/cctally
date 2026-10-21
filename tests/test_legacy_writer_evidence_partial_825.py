"""#825/#832 fix round A (review P3-13): durable legacy-writer evidence is
recorded only from a COMPLETE admission summary.

An admission summary first created mid-journal (`complete_through` NULL)
describes only the exclusions it saw. Recording legacy-writer evidence from it
would store a partial population; the next full rebuild then computes the
whole population, finds it "new", and restarts the seven-day warning age —
against case F9, under which only a genuinely new excluded write re-raises it.
"""
import datetime as dt
import json
import sqlite3

import _cctally_core
from conftest import load_script, redirect_paths

T0 = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def _sidecar():
    path = _cctally_core.LEGACY_WRITER_EVIDENCE_PATH
    return json.loads(path.read_text()) if path.exists() else None


def _raw_append(segment, records):
    import _lib_journal as J
    with open(_cctally_core.JOURNAL_DIR / segment, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _legacy_obs(J, at, pct):
    return J.unstamped_record(J.make_obs(
        at=at, src="statusline", provider="claude",
        payload={"weekly_percent": pct}))


def test_a_mid_journal_summary_records_no_evidence(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_journal as jr
    import _lib_journal as J
    clock = {"now": T0}
    monkeypatch.setattr(jr, "_evidence_now", lambda: clock["now"])
    jr.append_record(J.make_obs(at="2026-10-09T09:00:00Z", src="statusline",
                                provider="claude",
                                payload={"weekly_percent": 1.0}))
    jr.run_stats_ingest(mode="authoritative")
    seg = jr.journal_high_water()[0]
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T10:00:00Z", 70.0)])

    # The index forgets its summary: the next cycle starts a new one
    # mid-journal, which describes only what that cycle sees.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        conn.execute("DELETE FROM journal_admission_summary")
        conn.commit()
    finally:
        conn.close()
    _raw_append(seg, [_legacy_obs(J, "2026-10-09T11:00:00Z", 71.0)])
    jr.run_stats_ingest(mode="authoritative")
    assert _sidecar() is None, "evidence recorded from a partial population"

    # A full rebuild sees the whole population and records it once; the
    # unchanged population then keeps its age.
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    first = _sidecar()
    assert first["excludedCount"] == 2
    clock["now"] = T0 + dt.timedelta(days=3)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _sidecar() == first
