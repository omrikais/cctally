"""#946 / #825/#832 revision 2 (spec 4.7 "Safe traversal and completeness
proofs", Codex design-fix-round-3 Q5): live ingest and rebuild advance only
through complete lines actually traversed in the pinned input prefix, and
proofs produced under the old rule are not trusted.

The defect: `_run_cycle` advanced the ingest cursor to the RAW high water even
when its traversal stopped at a torn final line it could not decode. The next
appender's `_repair_torn_tail` truncated that segment back to the last complete
line, below the stored cursor, and wrote its record at the truncation point.
The next ingest resumed inside that record, skipped its suffix as malformed and
advanced to a valid endpoint, so the record was never folded — and the
derivation frontier and the Codex coverage certificate, both advanced from that
cursor, then certified the lost record as consumed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

import pytest

import _outcome_quarantine_trace as T

_BASE = 1_791_600_000            # 2026-10-10T02:40:00Z, hour-agnostic
_WEEK = _BASE + 3 * 86400 + 3600
_TORN = b'{"v":1,"t":"obs","provider":"claude","pay'


@pytest.fixture
def app(monkeypatch, tmp_path):
    app = T.make_app(monkeypatch, tmp_path)
    T.retain_session_input(app, "A")
    return app


def _current(app, monkeypatch, at, weekly):
    T.current(app, monkeypatch, at=T.iso(at), weekly=weekly,
              week_end=T.iso(_WEEK), account="A")


def _current_without_ingest(app, monkeypatch, at, weekly):
    """A current recorder's append through the supported path (torn-tail
    repair included), paused before its own ingest."""
    import _cctally_journal as jr
    import _cctally_statusline as sl
    T.set_clock(monkeypatch, T.iso(at))
    T.identity("A")
    args = argparse.Namespace(
        percent=weekly, resets_at=str(_WEEK), five_hour_percent=None,
        five_hour_resets_at=None, source="api")
    with monkeypatch.context() as patch:
        patch.setattr(jr, "run_stats_ingest", lambda **_kw: jr.IngestResult(
            ran=False, consumed=0, malformed=0, events_emitted=0, alerts=[]))
        assert app.record_usage_bound(
            args, binding=sl._capture_binding(), nudge_dashboard=False) == 0


def _newest_segment():
    import _cctally_core as core
    import _cctally_journal as jr
    return pathlib.Path(core.JOURNAL_DIR) / jr.list_segments()[-1]


def _tear(payload=_TORN):
    """A writer killed mid-line: bytes with no terminating newline."""
    with open(_newest_segment(), "ab") as handle:
        handle.write(payload)


def _cursor():
    import _cctally_core as core
    conn = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else (row[0], int(row[1]))


def _percents(app):
    return sorted(row["weekly_percent"] for row in T._rows(
        app, "SELECT weekly_percent FROM weekly_usage_snapshots"))


def _snapshot_decisions(weekly):
    """The admitted `snapshot_accept` decisions journaled for ``weekly``."""
    import _cctally_core as core
    import _lib_journal as J
    out = []
    for seg in sorted(pathlib.Path(core.JOURNAL_DIR).glob("*.jsonl")):
        for line in seg.read_bytes().splitlines():
            rec = J.decode_line(line)
            if (rec is not None and rec.get("t") == "evt"
                    and (rec.get("payload") or {}).get("kind")
                    == "snapshot_accept"
                    and rec["payload"].get("weekly_percent") == float(weekly)):
                out.append(rec["id"])
    return out


def _crossing_append(app, monkeypatch, at, weekly):
    """Tear the tail, let an ingest run over the torn bytes, then append
    ``weekly`` through the supported path. Returns the cursor the ingest
    stored and the appended record's span."""
    _tear()
    torn_end = _newest_segment().stat().st_size
    T.ingest(app)
    stored = _cursor()
    start = torn_end - len(_TORN)
    _current_without_ingest(app, monkeypatch, at, weekly)
    end = _newest_segment().stat().st_size
    return stored, start, end, torn_end


def test_a_record_appended_across_a_torn_tail_is_folded_exactly_once(
        app, monkeypatch):
    """The #946 regression: torn tail, ingest, a supported append, ingest
    again — the appended record is folded exactly once, and the stored
    cursor never passed the last complete line."""
    _current(app, monkeypatch, _BASE, 10)
    stored, start, end, torn_end = _crossing_append(
        app, monkeypatch, _BASE + 60, 20)
    assert stored is not None and stored[1] <= start, (
        "the ingest over a torn tail advanced the cursor past the last "
        f"complete line {start} (stored {stored}, torn end {torn_end})")
    assert start < torn_end < end, "non-vacuity: the append crosses the tear"
    T.ingest(app)
    assert _percents(app) == [10.0, 20.0]
    assert len(_snapshot_decisions(20)) == 1
    assert _cursor() == (_newest_segment().name, end)
    T.rebuild(app)
    assert _percents(app) == [10.0, 20.0]


def test_a_torn_suffix_counts_as_outstanding_lag_until_repaired(
        app, monkeypatch):
    """Doctor's lag is computed from the safe applied cursor: a torn suffix
    the ingest could not consume is still outstanding, never "caught up"."""
    import _cctally_doctor as D
    _current(app, monkeypatch, _BASE, 10)
    _tear()
    T.ingest(app)
    state = D.doctor_gather_state()
    assert state.journal_cursor_lag_bytes == len(_TORN), (
        state.journal_cursor_lag_bytes)


@pytest.mark.parametrize("where", ["past-the-segment", "inside-a-line"])
def test_an_invalid_cursor_never_renders_caught_up(app, monkeypatch, where):
    """A cursor past its segment's size, or inside a line, is not a safe
    applied endpoint, so doctor never reports the index caught up on it."""
    import _cctally_core as core
    import _cctally_doctor as D
    import _lib_doctor as L
    _current(app, monkeypatch, _BASE, 10)
    _current(app, monkeypatch, _BASE + 60, 20)
    seg = _newest_segment()
    size = seg.stat().st_size
    offset = size + 50 if where == "past-the-segment" else size - 7
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        conn.execute("UPDATE journal_cursor SET segment = ?, offset = ?, "
                     "applied_segment = ?, applied_offset = ?",
                     (seg.name, offset, seg.name, offset))
        conn.commit()
    finally:
        conn.close()
    result = L._check_journal_index_freshness(D.doctor_gather_state())
    assert result.summary != "index caught up", result
    assert result.severity != "ok", result


@pytest.mark.parametrize("later", ["no-later-reading", "later-valid-endpoint"])
def test_a_torn_tail_repair_across_an_advanced_cursor(app, monkeypatch, later):
    """Revision 2 decisive test 6: ingest and rebuild materialize the
    crossing reading exactly once, including after the cursor later reaches
    a valid endpoint (the state a pure endpoint check cannot detect), and a
    rebuild that trusts the live frontier keeps it too."""
    _current(app, monkeypatch, _BASE, 10)
    _crossing_append(app, monkeypatch, _BASE + 60, 20)
    if later == "later-valid-endpoint":
        _current(app, monkeypatch, _BASE + 120, 30)
        expected = [10.0, 20.0, 30.0]
    else:
        T.ingest(app)
        expected = [10.0, 20.0]
    assert _percents(app) == expected, "live ingest"
    T.rebuild(app)
    assert _percents(app) == expected, "a rebuild trusting the frontier"
    import _cctally_core as core
    (pathlib.Path(core.DB_PATH)).write_bytes(b"not a database" * 64)
    T.rebuild(app)
    assert _percents(app) == expected, "a whole-history reconstruction"
    T.rederive(app)
    assert _percents(app) == expected, "db rederive"
    assert len(_snapshot_decisions(20)) == 1


def test_an_old_generation_derivation_proof_is_rejected(app, monkeypatch):
    """A frontier row written under the old rule claims completeness through
    a valid endpoint past an input nothing derived. It must be untrusted
    even though its coordinate follows a newline, so the rebuild reconstructs
    and materializes the input."""
    import _cctally_core as core
    import _cctally_journal as jr
    _current(app, monkeypatch, _BASE, 10)
    _current_without_ingest(app, monkeypatch, _BASE + 60, 20)
    hw = jr.journal_high_water()
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        # The shape every build before revision 2 wrote: no proof generation.
        conn.execute("DELETE FROM journal_derivation_frontier")
        conn.execute(
            "INSERT INTO journal_derivation_frontier (id, segment, offset) "
            "VALUES (1, ?, ?)", (hw[0], hw[1]))
        conn.execute("UPDATE journal_cursor SET segment = ?, offset = ?, "
                     "applied_segment = ?, applied_offset = ?",
                     (hw[0], hw[1], hw[0], hw[1]))
        conn.commit()
    finally:
        conn.close()
    assert jr._sound_line_boundary(hw), "non-vacuity: a valid endpoint"
    assert jr.read_trusted_derivation_state(core.DB_PATH) is None
    T.rebuild(app)
    assert _percents(app) == [10.0, 20.0]
    assert len(_snapshot_decisions(20)) == 1


# --------------------------------------------------------------------------
# the Codex coverage certificate
# --------------------------------------------------------------------------

def _quota_obs(line_offset, used_percent):
    import _lib_journal as J
    return J.make_obs(at=T.iso(_BASE), src="codex-quota", provider="codex",
                      payload={
        "kind": "quota_window_snapshot",
        "source": "codex", "source_root_key": "root-a",
        "source_path": "/codex/root-a/rollout.jsonl",
        "line_offset": line_offset, "captured_at_utc": T.iso(_BASE),
        "observed_slot": "primary", "logical_limit_key": "limit-primary",
        "limit_id": "native-primary", "limit_name": "Primary",
        "window_minutes": 300, "used_percent": used_percent,
        "resets_at_utc": T.iso(_BASE + 3600), "plan_type": "pro",
        "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5.3-codex",
    })


def _quota_rows(app):
    conn = app.open_cache_db()
    try:
        return sorted(conn.execute(
            "SELECT line_offset, used_percent FROM quota_window_snapshots "
            "WHERE source = 'codex'").fetchall())
    finally:
        conn.close()


def _certificate(app):
    import _cctally_cache
    conn = app.open_cache_db()
    try:
        return _cctally_cache.load_codex_journal_coverage_certificate(conn)
    finally:
        conn.close()


def test_a_codex_quota_record_across_a_torn_tail_is_not_laundered(
        app, monkeypatch):
    """The same trace with a Codex quota observation crossing the tear: live
    ingest materializes it, the coverage certificate never claims coverage
    past what was applied, and a rebuild that trusts that certificate keeps
    the row."""
    import _cctally_journal as jr
    _current(app, monkeypatch, _BASE, 10)
    jr.append_record(_quota_obs(10, 11.0))
    T.ingest(app)
    _tear()
    T.ingest(app)
    jr.append_record(_quota_obs(20, 22.0))
    T.ingest(app)
    _current(app, monkeypatch, _BASE + 120, 30)
    assert _quota_rows(app) == [(10, 11.0), (20, 22.0)], "live ingest"
    cert = _certificate(app)
    if cert is not None:
        assert tuple(cert["appliedThrough"]) == _cursor(), cert
    T.rebuild(app)
    assert _quota_rows(app) == [(10, 11.0), (20, 22.0)], "after a rebuild"


def test_an_old_version_coverage_certificate_forces_the_quota_replay(
        app, monkeypatch):
    """A certificate minted under the old rule (coverage version 1) claims
    coverage of a quota observation the cache never received. It is rejected
    even though its covered boundary follows a newline, so the rebuild
    replays the journal into the cache rather than trusting it."""
    import _cctally_journal as jr
    import _lib_cache_coverage as C
    _current(app, monkeypatch, _BASE, 10)
    jr.append_record(_quota_obs(10, 11.0))
    hw = jr.journal_high_water()
    vector = jr.coverage_pinned_vector()
    conn = app.open_cache_db()
    try:
        row = conn.execute("SELECT value FROM cache_meta WHERE key = "
                           "'codex_physical_mutation_seq'").fetchone()
        seq = 0 if row is None else int(row[0])
        old = {
            "coverageVersion": 1,
            "interpretationVersion": C.INTERPRETATION_VERSION,
            "physicalMutationSeq": seq,
            "coveredHighWater": [hw[0], hw[1]],
            "appliedThrough": [hw[0], hw[1]],
            "identityRoot": C.identity_root(vector),
        }
        conn.execute(
            "INSERT INTO cache_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (C.CERTIFICATE_KEY, json.dumps(old, sort_keys=True,
                                           separators=(",", ":"))))
        conn.commit()
    finally:
        conn.close()
    assert jr._sound_line_boundary(hw), "non-vacuity: a valid endpoint"
    assert _quota_rows(app) == [], "non-vacuity: the cache lacks the row"
    T.rebuild(app)
    assert _quota_rows(app) == [(10, 11.0)]
    cert = _certificate(app)
    assert cert is not None and cert["coverageVersion"] == C.COVERAGE_VERSION
    assert C.COVERAGE_VERSION > 1
