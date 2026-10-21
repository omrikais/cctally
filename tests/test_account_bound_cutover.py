"""#825/#832 Task 1.3 — cutover establishment and the journal-anchored legacy
spool baseline (spec 5.5, 7, case F3).

The `account_bound_cutover` op is established once, under `journal.lock`,
together with the legacy spool baseline whose digest the op records. Every
crash window recovers from the journal without a second boundary; a losing or
stale initializer never installs or replaces the baseline; a baseline whose
digest is not the latest journal anchor is unavailable (a quarantine trigger
for the reducer, never silently re-initialized).
"""
import datetime as dt
import importlib
import json
import sqlite3
import threading

import pytest

import _cctally_core
import _lib_account_protocol as P
import _lib_journal as J
import _lib_legacy_spool as LS
import _cctally_journal as journal

FIXED = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
SEGMENT = "observations-2026-10.jsonl"


@pytest.fixture
def env(tmp_path, monkeypatch):
    global journal
    # Another module's `load_script` evicts the `_cctally_*` siblings from
    # sys.modules (tests/_script_loader.py), so the module imported above can
    # be stale under xdist. Bind the live one: a monkeypatch must reach the
    # module `_cctally_statusline` imports at call time.
    journal = importlib.import_module("_cctally_journal")
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    _cctally_core._init_paths_from_env()
    monkeypatch.setattr(journal, "_utc_now", lambda: FIXED)
    yield data_dir


def _records():
    out = []
    for name in journal.list_segments():
        raw = (_cctally_core.JOURNAL_DIR / name).read_bytes()
        out.extend((name, J.decode_line(line))
                   for line in raw.split(b"\n") if line)
    return out


def _cutover_ops():
    return [rec for _seg, rec in _records()
            if rec and (rec.get("payload") or {}).get("kind")
            == P.CUTOVER_OP_KIND]


def _seed_segment(name, records, torn=b""):
    jdir = _cctally_core.JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    data = b"".join(J.encode_line(J.unstamped_record(r)) for r in records)
    (jdir / name).write_bytes(data + torn)
    return len(data)


def _legacy_obs(i):
    return J.make_obs(at=f"2026-09-0{i}T00:00:00Z", src="statusline",
                      provider="claude", payload={"i": i})


def _write_legacy_candidate(name="cand-1.json", body=b'{"pct": 70}'):
    cand = _cctally_core.STATUSLINE_CANDIDATE_DIR
    cand.mkdir(parents=True, exist_ok=True)
    (cand / name).write_bytes(body)
    return f"statusline-candidates/{name}"


# --------------------------------------------------------------------------
# establishment
# --------------------------------------------------------------------------

def test_fresh_store_gets_one_cutover_with_empty_historical_boundary(env):
    boundary = journal.ensure_account_bound_cutover()
    assert boundary.frozen_extents == {}
    assert boundary.segment == SEGMENT
    assert boundary.end_offset == 0
    ops = _cutover_ops()
    assert len(ops) == 1
    assert ops[0]["id"] == P.CUTOVER_OP_ID
    assert P.is_stamped(ops[0])
    assert P.boundary_from_op(ops[0]) == boundary
    # Idempotent: a second call appends nothing and returns the same boundary.
    assert journal.ensure_account_bound_cutover() == boundary
    assert len(_cutover_ops()) == 1
    assert journal.find_account_bound_cutover() == boundary
    baseline = journal.read_anchored_legacy_spool_baseline()
    assert baseline is not None
    assert baseline.digest() == boundary.legacy_spool_baseline_digest
    assert baseline.entries == ()


def test_existing_store_freezes_complete_line_extents_of_every_segment(env):
    sep = _seed_segment("observations-2026-09.jsonl",
                        [_legacy_obs(1), _legacy_obs(2)],
                        torn=b'{"t":"obs","partial')
    oct_len = _seed_segment(SEGMENT, [_legacy_obs(3)])
    boot = _seed_segment("bootstrap-20260101T000000_000000.jsonl",
                         [_legacy_obs(4)])
    boundary = journal.ensure_account_bound_cutover()
    assert boundary.frozen_extents == {
        "bootstrap-20260101T000000_000000.jsonl": boot,
        "observations-2026-09.jsonl": sep,
        SEGMENT: oct_len,
    }
    assert boundary.segment == SEGMENT
    assert boundary.end_offset == oct_len
    # Every pre-existing complete line is historical; the op itself is not.
    assert P.is_historical(P.Coordinate("observations-2026-09.jsonl", 0), boundary)
    assert not P.is_historical(P.Coordinate(SEGMENT, oct_len), boundary)


def test_journal_cutover_pending_store_defers_establishment(env):
    _cctally_core.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_cctally_core.DB_PATH))
    conn.execute("CREATE TABLE t (x)")
    conn.execute(f"PRAGMA user_version = {_cctally_core.LEGACY_STATS_HEAD}")
    conn.commit()
    conn.close()
    with pytest.raises(journal.AccountBoundCutoverDeferred):
        journal.ensure_account_bound_cutover()
    assert _cutover_ops() == []


def test_baseline_captures_the_legacy_spool_paths(env):
    rel = _write_legacy_candidate()
    _cctally_core.STATUSLINE_SELECTED_PATH.write_text("{}")
    _cctally_core.STATUSLINE_OBSERVE_MARKER_PATH.write_text("1")
    state = journal.capture_legacy_spool_state()
    paths = [entry[0] for entry in state.entries]
    assert paths == sorted([rel, "statusline-observe.last",
                            "statusline-selected.json"])
    boundary = journal.ensure_account_bound_cutover()
    assert boundary.legacy_spool_baseline_digest == state.digest()
    assert journal.read_anchored_legacy_spool_baseline(boundary) == state


# --------------------------------------------------------------------------
# crash at each durability step (case F3)
# --------------------------------------------------------------------------

class _Crash(RuntimeError):
    pass


@pytest.mark.parametrize("step,baseline_survives", [
    ("after-scan", True),
    ("after-baseline-capture", True),
    ("after-append", False),
    ("after-fsync", False),
    ("after-baseline-install", True),
])
def test_crash_at_each_durability_step_recovers_one_boundary(
        env, monkeypatch, step, baseline_survives):
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    _write_legacy_candidate()

    def seam(name):
        if name == step:
            raise _Crash(name)

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    with pytest.raises(_Crash):
        journal.ensure_account_bound_cutover()
    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)

    boundary = journal.ensure_account_bound_cutover()
    again = journal.ensure_account_bound_cutover()
    assert again == boundary
    assert len(_cutover_ops()) == 1
    assert journal.find_account_bound_cutover() == boundary
    baseline = journal.read_anchored_legacy_spool_baseline()
    if baseline_survives:
        assert baseline is not None
        assert baseline.digest() == boundary.legacy_spool_baseline_digest
    else:
        # The install was lost with the crashed producer; the retry found the
        # op and installed nothing. The reducer reports baseline-unavailable.
        assert baseline is None


def test_corrupt_coordinate_cache_never_yields_a_second_boundary(env):
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    boundary = journal.ensure_account_bound_cutover()
    cache = _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH
    for garbage in (b"not json", json.dumps({
            "schemaVersion": 1,
            "cutover": {"segment": SEGMENT, "offset": 0}}).encode(),
            b""):
        cache.write_bytes(garbage)
        assert journal.find_account_bound_cutover() == boundary
        assert journal.ensure_account_bound_cutover() == boundary
        assert len(_cutover_ops()) == 1
    cache.unlink()
    assert journal.ensure_account_bound_cutover() == boundary
    assert len(_cutover_ops()) == 1


# --------------------------------------------------------------------------
# two-producer race (case F3, review round 7)
# --------------------------------------------------------------------------

def test_two_producer_race_loser_installs_nothing(env, monkeypatch):
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    both_scanned = threading.Barrier(2, timeout=10)
    legacy_written = threading.Event()
    results = {}
    errors = []

    def seam(name):
        if name != "after-scan":
            return
        both_scanned.wait()
        if threading.current_thread().name == "loser":
            assert legacy_written.wait(timeout=20)

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)

    def run():
        try:
            results[threading.current_thread().name] = (
                journal.ensure_account_bound_cutover())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    winner = threading.Thread(target=run, name="winner")
    loser = threading.Thread(target=run, name="loser")
    winner.start()
    loser.start()
    winner.join(timeout=30)
    assert not winner.is_alive()
    installed = _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    rel = _write_legacy_candidate()
    legacy_written.set()
    loser.join(timeout=30)
    assert not loser.is_alive()
    assert errors == []

    assert results["winner"] == results["loser"]
    assert len(_cutover_ops()) == 1
    # The loser neither appended an op nor wrote the baseline file.
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed
    baseline = journal.read_anchored_legacy_spool_baseline()
    assert baseline is not None
    assert LS.diff(baseline, journal.capture_legacy_spool_state()) == (rel,)


# --------------------------------------------------------------------------
# journal anchor
# --------------------------------------------------------------------------

def test_baseline_with_a_digest_other_than_the_latest_anchor_reads_none(env):
    journal.ensure_account_bound_cutover()
    _write_legacy_candidate()
    other = journal.capture_legacy_spool_state()
    _cctally_core.LEGACY_SPOOL_BASELINE_PATH.write_text(
        json.dumps(LS.to_document(other)))
    assert journal.read_anchored_legacy_spool_baseline() is None
    _cctally_core.LEGACY_SPOOL_BASELINE_PATH.write_text("{corrupt")
    assert journal.read_anchored_legacy_spool_baseline() is None
    _cctally_core.LEGACY_SPOOL_BASELINE_PATH.unlink()
    assert journal.read_anchored_legacy_spool_baseline() is None


def test_legacy_spool_activity_advances_the_anchor(env):
    boundary = journal.ensure_account_bound_cutover()
    assert journal.latest_legacy_spool_anchor_digest() == (
        boundary.legacy_spool_baseline_digest)
    rel = _write_legacy_candidate()
    new_state = journal.capture_legacy_spool_state()
    baseline = journal.read_anchored_legacy_spool_baseline()
    changed = LS.diff(baseline, new_state)
    assert changed == (rel,)
    op_id = journal.append_legacy_spool_activity(
        changed=changed, new_state=new_state, reason="changed")
    ops = [rec for _seg, rec in _records()
           if (rec.get("payload") or {}).get("kind")
           == P.LEGACY_SPOOL_ACTIVITY_KIND]
    assert [op["id"] for op in ops] == [op_id]
    assert P.is_stamped(ops[0])
    assert ops[0]["payload"]["reason"] == "changed"
    assert ops[0]["payload"]["changed"] == [rel]
    assert ops[0]["payload"]["digest"] == new_state.digest()
    assert journal.latest_legacy_spool_anchor_digest() == new_state.digest()
    assert journal.read_anchored_legacy_spool_baseline() == new_state
    # The cutover boundary itself never moves.
    assert journal.find_account_bound_cutover() == boundary


def test_baseline_file_is_written_only_after_its_anchoring_op(env, monkeypatch):
    journal.ensure_account_bound_cutover()
    before = _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    _write_legacy_candidate()
    new_state = journal.capture_legacy_spool_state()

    def seam(name):
        if name == "activity-after-append":
            raise _Crash(name)

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    with pytest.raises(_Crash):
        journal.append_legacy_spool_activity(
            changed=("x",), new_state=new_state, reason="changed")
    # The op landed; the file did not advance; the anchor and the file now
    # disagree, so the baseline is unavailable rather than silently trusted.
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == before
    assert journal.latest_legacy_spool_anchor_digest() == new_state.digest()
    assert journal.read_anchored_legacy_spool_baseline() is None


def test_baseline_unavailable_reinstalls_under_its_own_anchor(env):
    journal.ensure_account_bound_cutover()
    _cctally_core.LEGACY_SPOOL_BASELINE_PATH.unlink()
    assert journal.read_anchored_legacy_spool_baseline() is None
    state = journal.capture_legacy_spool_state()
    journal.append_legacy_spool_activity(
        changed=(), new_state=state, reason="baseline-unavailable")
    assert journal.read_anchored_legacy_spool_baseline() == state


def test_legacy_spool_activity_requires_a_cutover(env):
    with pytest.raises(journal.JournalError):
        journal.append_legacy_spool_activity(
            changed=(), new_state=journal.capture_legacy_spool_state(),
            reason="changed")


def test_anchor_lookup_survives_a_corrupt_cache_after_activity(env):
    journal.ensure_account_bound_cutover()
    _write_legacy_candidate()
    state = journal.capture_legacy_spool_state()
    journal.append_legacy_spool_activity(
        changed=("statusline-candidates/cand-1.json",), new_state=state,
        reason="changed")
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.write_bytes(b"garbage")
    assert journal.latest_legacy_spool_anchor_digest() == state.digest()
    assert journal.read_anchored_legacy_spool_baseline() == state


def test_unstamped_anchor_ops_never_advance_the_anchor(env):
    """An unstamped `legacy_spool_activity` (or cutover) op is excluded input:
    it can never move the anchor."""
    boundary = journal.ensure_account_bound_cutover()
    forged = J.unstamped_record(J.make_op(
        at="t", src="x", payload={"kind": P.LEGACY_SPOOL_ACTIVITY_KIND,
                                  "digest": "sha256:" + "f" * 64,
                                  "reason": "changed", "changed": []}))
    with open(_cctally_core.JOURNAL_DIR / SEGMENT, "ab") as fh:
        fh.write(J.encode_line(forged))
    assert journal.latest_legacy_spool_anchor_digest() == (
        boundary.legacy_spool_baseline_digest)


# --------------------------------------------------------------------------
# pure document codec
# --------------------------------------------------------------------------

def test_legacy_spool_state_digest_and_diff():
    a = LS.LegacySpoolState(entries=(("a", 1, 10, "sha256:" + "1" * 64),))
    b = LS.LegacySpoolState(entries=(("a", 1, 11, "sha256:" + "1" * 64),
                                     ("b", 2, 10, "sha256:" + "2" * 64)))
    assert a.digest().startswith("sha256:") and len(a.digest()) == 71
    assert a.digest() != b.digest()
    assert LS.diff(a, b) == ("a", "b")
    assert LS.diff(b, a) == ("a", "b")
    assert LS.diff(a, a) == ()
    assert LS.from_document(LS.to_document(b)) == b
    doc = LS.to_document(b)
    doc["digest"] = a.digest()
    with pytest.raises(ValueError):
        LS.from_document(doc)


# --------------------------------------------------------------------------
# torn-tail recheck and cache precedence (fix round J: review cycle 3
# 825-832-CD3-J02, Codex fix-round-4 Q5)
# --------------------------------------------------------------------------

def _raw_append(name, record):
    """Append ``record`` as the previous release writes it; returns the
    record actually written."""
    written = J.unstamped_record(record)
    with open(_cctally_core.JOURNAL_DIR / name, "ab") as fh:
        fh.write(J.encode_line(written))
    return written


def _coordinate_of(record_id):
    for name in journal.list_segments():
        offset = 0
        for line in (_cctally_core.JOURNAL_DIR / name).read_bytes().split(
                b"\n")[:-1]:
            rec = J.decode_line(line)
            if rec is not None and rec.get("id") == record_id:
                return P.Coordinate(name, offset)
            offset += len(line) + 1
    return None


def _admission_views():
    """Every boundary reader, cached and uncached."""
    return (journal.find_account_bound_cutover(),
            journal._whole_prefix_admission_boundary(),
            journal.ensure_account_bound_cutover())


def test_torn_tail_race_keeps_the_first_cutover_and_its_baseline(
        env, monkeypatch):
    """Two producers finish their absence scans over complete history plus a
    torn suffix. The winner repairs the tail and appends the cutover BELOW
    the scans' raw extent; a legacy observation and a legacy spool write
    follow. The loser's locked recheck must resume from the complete-line
    endpoint its scan actually traversed, find the winner's op and install
    nothing: one boundary, the winner's baseline, the spool write still
    detected, the legacy observation excluded, and every reader — with and
    without the coordinate cache — admitting identically."""
    torn = b'{"t":"obs","torn-suffix-written-by-a-crashed-writer'
    history = _seed_segment(SEGMENT, [_legacy_obs(1), _legacy_obs(2)],
                            torn=torn)
    both_scanned = threading.Barrier(2, timeout=10)
    winner_done = threading.Event()
    legacy_written = threading.Event()
    results = {}
    errors = []

    def seam(name):
        if name != "after-scan":
            return
        both_scanned.wait()
        if threading.current_thread().name == "loser":
            assert winner_done.wait(timeout=20)
            assert legacy_written.wait(timeout=20)

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)

    def run():
        try:
            results[threading.current_thread().name] = (
                journal.ensure_account_bound_cutover())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            if threading.current_thread().name == "winner":
                winner_done.set()

    winner = threading.Thread(target=run, name="winner", daemon=True)
    loser = threading.Thread(target=run, name="loser", daemon=True)
    try:
        winner.start()
        loser.start()
        winner.join(timeout=30)
        assert not winner.is_alive()
        assert errors == []
        boundary = results["winner"]
        # Non-vacuity: the winner's op starts below the raw extent the
        # scans pinned, inside what was the torn suffix.
        assert boundary.end_offset == history < history + len(torn)
        installed = _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
        legacy = J.make_obs(at="2026-10-09T12:30:00Z", src="statusline",
                            provider="claude", payload={"i": 9})
        legacy = _raw_append(SEGMENT, legacy)
        rel = _write_legacy_candidate()
        legacy_written.set()
        loser.join(timeout=30)
        assert not loser.is_alive()
    finally:
        legacy_written.set()
        winner_done.set()
        monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    assert errors == []

    assert len(_cutover_ops()) == 1
    assert results["loser"] == boundary
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed
    baseline = journal.read_anchored_legacy_spool_baseline()
    assert baseline is not None
    assert LS.diff(baseline, journal.capture_legacy_spool_state()) == (rel,)
    coordinate = _coordinate_of(legacy["id"])
    assert coordinate is not None
    assert not P.is_historical(coordinate, boundary)
    cached = _admission_views()
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    uncached = _admission_views()
    assert cached == uncached == (boundary, boundary, boundary)
    assert len(_cutover_ops()) == 1


def _second_cutover_line():
    """A duplicate stamped cutover op appended after the first, as the
    faulty recheck rule wrote one: its own coordinate, frozen extents and a
    replacement baseline digest."""
    segment = SEGMENT
    start = (_cctally_core.JOURNAL_DIR / segment).stat().st_size
    rel = _write_legacy_candidate("cand-later.json", b'{"pct": 99}')
    state = journal.capture_legacy_spool_state()
    extents = {name: (_cctally_core.JOURNAL_DIR / name).stat().st_size
               for name in journal.list_segments()}
    extents[segment] = start
    second = P.CutoverBoundary(
        segment=segment, end_offset=start, frozen_extents=extents,
        legacy_spool_baseline_digest=state.digest())
    record = J.make_op(at="2026-10-09T13:00:00Z", src="account-bound-cutover",
                       payload={"kind": P.CUTOVER_OP_KIND,
                                **P.boundary_payload(second)})
    record["id"] = P.CUTOVER_OP_ID
    with open(_cctally_core.JOURNAL_DIR / segment, "ab") as fh:
        fh.write(J.encode_line(J.prepare_record(record)))
    return second, start, state, rel


@pytest.mark.parametrize("cache_writer", ["faulty-rule", "current-writer"])
def test_a_cache_naming_a_later_cutover_is_never_authoritative(
        env, cache_writer):
    """The coordinate cache must establish that it names the FIRST cutover,
    not merely some valid cutover-shaped line. A cache the faulty recheck
    rule left pointing at a duplicate (its schema), or any cache whose
    precedence is not established, is reconstructed from the journal, so
    cached and uncached readers choose the same boundary."""
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    first = journal.ensure_account_bound_cutover()
    second, start, state, _rel = _second_cutover_line()
    assert second != first
    if cache_writer == "faulty-rule":
        doc = {"schemaVersion": 1,
               "cutover": {"segment": SEGMENT, "offset": start},
               "anchor": {"segment": SEGMENT, "offset": start,
                          "digest": state.digest()},
               "scanned": {"segment": SEGMENT, "offset": start}}
        _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.write_text(
            json.dumps(doc))
    else:
        journal._publish_account_bound_cache(
            (SEGMENT, start), (SEGMENT, start, state.digest()),
            (SEGMENT, start))
    assert _admission_views() == (first, first, first)
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    assert _admission_views() == (first, first, first)
    assert journal.read_anchored_legacy_spool_baseline(first) is not None


def test_a_faulty_rule_absence_cache_never_hides_the_cutover(env):
    """An absence prefix the faulty rule recorded (recomputed from file
    contents after a concurrent repair and append, so it can extend past the
    winner's op) is untrusted: every reader still finds the one cutover and
    no producer establishes a second."""
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    first = journal.ensure_account_bound_cutover()
    size = (_cctally_core.JOURNAL_DIR / SEGMENT).stat().st_size
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.write_text(json.dumps({
        "schemaVersion": 1, "cutover": None,
        "absentThrough": {SEGMENT: size}}))
    assert _admission_views() == (first, first, first)
    assert len(_cutover_ops()) == 1


# --------------------------------------------------------------------------
# review cycle 7: an incomplete lookup never proves absence
# --------------------------------------------------------------------------

def _inject_segment_failure(monkeypatch, segment, mode, active):
    """While ``active[0]``, every read (``mode == "read"``) or stat (``mode ==
    "stat"``) of journal ``segment`` fails with a transient EIO. The listing
    itself keeps working, so the segment is known to exist."""
    import errno
    import os
    import pathlib
    import sys

    def failure(path):
        return OSError(errno.EIO, "injected transient journal I/O failure",
                       str(path))

    if mode == "read":
        real_open = journal._open_segment_for_read

        def refusing_open(seg_path):
            if active[0] and pathlib.Path(seg_path).name == segment:
                raise failure(seg_path)
            return real_open(seg_path)

        monkeypatch.setattr(journal, "_open_segment_for_read", refusing_open)
        return
    real_stat = os.stat

    def refusing_stat(path, *args, **kwargs):
        # The stat sites that size the lookup's ranges (`getsize` is
        # `_segment_sizes`'s); the listing and the append are not this case.
        if (active[0] and not isinstance(path, int)
                and pathlib.Path(os.fspath(path)).name == segment
                and sys._getframe(1).f_code.co_name in (
                    "getsize", "_whole_prefix_admission_boundary")):
            raise failure(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refusing_stat)


@pytest.mark.parametrize("mode", ["read", "stat"])
def test_an_unreadable_segment_never_establishes_a_second_boundary(
        env, monkeypatch, mode):
    """Review cycle 7 825-832-CD3-J02. The coordinate cache is gone and the
    segment holding the cutover — the append target — cannot be read or
    stat'ed for a moment. Neither the unlocked scan nor the locked recheck
    proved absence, so the producer must not append a second cutover or
    replace the anchored baseline; once reads recover, every reader still
    finds the one original boundary."""
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    first = journal.ensure_account_bound_cutover()
    installed = _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    # A replacement baseline would differ from the anchored one.
    _write_legacy_candidate()
    active = [True]
    _inject_segment_failure(monkeypatch, SEGMENT, mode, active)

    def restore_before_append(step):
        # The failure is transient: reads recover before the append.
        if step == "after-baseline-capture":
            active[0] = False

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM",
                        restore_before_append)
    raised = None
    try:
        journal.ensure_account_bound_cutover()
    except OSError as exc:
        raised = exc
    active[0] = False
    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)

    assert len(_cutover_ops()) == 1, (
        "an incomplete scan was taken as absence: a second cutover")
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed
    assert journal.find_account_bound_cutover() == first
    assert journal.read_anchored_legacy_spool_baseline(first) is not None
    assert type(raised).__name__ == "AccountBoundCutoverUnavailable", raised

    # Every lookup fails closed while the read fails: none answers "none".
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink(missing_ok=True)
    active[0] = True
    for lookup in (journal.find_account_bound_cutover,
                   journal._whole_prefix_admission_boundary,
                   journal.ensure_account_bound_cutover):
        with pytest.raises(journal.AccountBoundCutoverUnavailable):
            lookup()
    active[0] = False
    assert len(_cutover_ops()) == 1
    assert _admission_views() == (first, first, first)
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed


def test_a_complete_scan_of_a_store_without_a_cutover_still_answers_none(env):
    """The pre-cutover case is unchanged: a readable journal with no cutover
    op answers None from every pure lookup."""
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    _seed_segment("bootstrap-20260101T000000_000000.jsonl", [_legacy_obs(2)])
    assert journal.find_account_bound_cutover() is None
    assert journal._whole_prefix_admission_boundary() is None
    assert _cutover_ops() == []


def test_a_segment_whose_listing_entry_cannot_be_typed_is_never_omitted(
        env, monkeypatch):
    """The listing is part of every lookup. Python 3.14's `Path.is_file`
    answers False for ANY stat error, so a segment whose entry could not be
    typed for a moment silently left the listing, and every scan built on it
    proved absence over a journal it never saw. Only a vanished entry is
    absent; any other failure makes the listing — and the lookup — fail."""
    import errno
    import os
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    journal.ensure_account_bound_cutover()
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    journal_dir = os.fspath(_cctally_core.JOURNAL_DIR)
    real_scandir = os.scandir

    class _Untypable:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_file(self, *args, **kwargs):
            raise OSError(errno.EIO, "injected entry stat failure", self.path)

        def __getattr__(self, name):
            return getattr(self._entry, name)

    class _Listing:
        def __init__(self, it):
            self._it = it

        def __iter__(self):
            for entry in self._it:
                yield _Untypable(entry) if entry.name == SEGMENT else entry

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._it.close()
            return False

        def close(self):
            self._it.close()

    def untypable_scandir(path=".", *args, **kwargs):
        it = real_scandir(path, *args, **kwargs)
        if not isinstance(path, int) and os.fspath(path) == journal_dir:
            return _Listing(it)
        return it

    import pathlib
    real_is_file = pathlib.Path.is_file
    monkeypatch.setattr(os, "scandir", untypable_scandir)
    # The previous listing typed entries through `Path.is_file`; under 3.14
    # that swallows the error, under earlier versions it raises. Either way
    # the entry's type is unknown here.
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self, *a, **k: (
        False if (self.name == SEGMENT
                  and os.fspath(self.parent) == journal_dir)
        else real_is_file(self, *a, **k)))
    with pytest.raises(OSError):
        journal.list_segments()
    with pytest.raises(OSError):
        journal.find_account_bound_cutover()
    with pytest.raises(OSError):
        journal.ensure_account_bound_cutover()
    monkeypatch.setattr(os, "scandir", real_scandir)
    monkeypatch.setattr(pathlib.Path, "is_file", real_is_file)
    assert len(_cutover_ops()) == 1
    assert journal.find_account_bound_cutover() is not None


def test_a_segment_removed_after_listing_is_absent(env, monkeypatch):
    """The only supported segment removal is an empty restored future
    segment (`_remove_empty_future_segments`), which holds no op. A segment
    that vanishes between the listing and its read is therefore absent, not
    an incomplete lookup."""
    import errno
    import pathlib
    _seed_segment(SEGMENT, [_legacy_obs(1)])
    real_open = journal._open_segment_for_read

    def vanished(seg_path):
        if pathlib.Path(seg_path).name == SEGMENT:
            raise FileNotFoundError(errno.ENOENT, "vanished", str(seg_path))
        return real_open(seg_path)

    monkeypatch.setattr(journal, "_open_segment_for_read", vanished)
    assert journal.find_account_bound_cutover() is None
    assert journal._whole_prefix_admission_boundary() is None


# --------------------------------------------------------------------------
# review cycle 7 / revision 5: an incomplete legacy spool inventory is never
# a baseline; before the initial cutover its uncertainty is recorded in the
# cutover op itself (spec 5.5, section 7, operator decision on dc9 Q4)
# --------------------------------------------------------------------------

def _inject_legacy_failure(monkeypatch, failure, active):
    """While ``active[0]``, the legacy candidate directory's file reads
    (``file-read``) or its listing (``directory-listing``) fail."""
    import builtins
    import errno
    import os
    candidate_dir = _cctally_core.STATUSLINE_CANDIDATE_DIR
    if failure == "file-read":
        def refusing_open(file, *args, **kwargs):
            if (active[0] and not isinstance(file, int)
                    and os.path.dirname(os.fspath(file))
                    == os.fspath(candidate_dir)):
                raise OSError(errno.EIO, "injected legacy read failure",
                              os.fspath(file))
            return builtins.open(file, *args, **kwargs)

        monkeypatch.setattr(journal, "open", refusing_open, raising=False)
        return
    real_scandir = os.scandir

    def refusing_scandir(path=".", *args, **kwargs):
        if (active[0] and not isinstance(path, int)
                and os.fspath(path) == os.fspath(candidate_dir)):
            raise PermissionError(errno.EACCES, "injected listing failure",
                                  os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", refusing_scandir)


_FAILED_OPERATION = {"file-read": "read", "directory-listing": "list"}
_FAILED_ERRNO = {"file-read": "EIO", "directory-listing": "EACCES"}


def _spool_activity_ops():
    return [rec for _seg, rec in _records()
            if rec and (rec.get("payload") or {}).get("kind")
            == P.LEGACY_SPOOL_ACTIVITY_KIND]


@pytest.mark.parametrize("failure", ["file-read", "directory-listing"])
def test_an_incomplete_spool_inventory_never_becomes_the_baseline(
        env, monkeypatch, failure):
    """Review cycle 7 825-832-CD7-J01, revision 5. A legacy candidate exists
    when the cutover is first established, but its read (or the listing of
    its directory) fails. Taking the incomplete inventory as the baseline
    would hide that candidate from every later rescan; deferring the
    establishment would stall every current producer for as long as the
    file stays unreadable. The cutover is established WITH its inventory
    uncertainty recorded in the op itself: no digest, no baseline file, and
    typed failure evidence. Once reads recover, the next complete inventory
    installs a baseline under its own anchoring op."""
    rel = _write_legacy_candidate()
    active = [True]
    _inject_legacy_failure(monkeypatch, failure, active)
    with pytest.raises(OSError):
        journal.capture_legacy_spool_state()

    boundary = journal.ensure_account_bound_cutover()
    [op] = _cutover_ops()
    assert boundary.legacy_spool_baseline_digest is None
    assert op["payload"]["legacySpoolBaselineDigest"] is None
    evidence = op["payload"]["legacySpoolInventoryFailure"]
    assert evidence["operation"] == _FAILED_OPERATION[failure], evidence
    assert evidence["error"] == _FAILED_ERRNO[failure], evidence
    assert evidence["path"].startswith("statusline-candidates"), evidence
    assert P.boundary_from_op(op) == boundary
    assert not _cctally_core.LEGACY_SPOOL_BASELINE_PATH.exists()
    assert journal.read_anchored_legacy_spool_baseline() is None
    # Idempotent: the uncertain boundary is THE boundary.
    assert journal.ensure_account_bound_cutover() == boundary
    assert journal.find_account_bound_cutover() == boundary
    assert len(_cutover_ops()) == 1
    # Within the same failure episode a rescan appends nothing more.
    assert journal.record_legacy_spool_inventory_incomplete(
        _capture_failure()) is None
    assert _spool_activity_ops() == []

    active[0] = False
    import _cctally_statusline as sl
    assert sl._rescan_legacy_spool() is True
    [activity] = _spool_activity_ops()
    # Fix round K12 (K11R-4): the uncertain cutover anchored no baseline, so
    # none is "unavailable"; the complete inventory anchors the episode's
    # first baseline under its own `inventory-complete` op.
    assert activity["payload"]["reason"] == "inventory-complete"
    baseline = journal.read_anchored_legacy_spool_baseline()
    assert baseline is not None
    assert [entry[0] for entry in baseline.entries] == [rel]
    assert baseline.digest() == activity["payload"]["digest"]
    assert len(_cutover_ops()) == 1


def _capture_failure():
    try:
        journal.capture_legacy_spool_state()
    except journal.LegacySpoolInventoryIncomplete as exc:
        return exc
    raise AssertionError("non-vacuity: the inventory did not fail")


@pytest.mark.parametrize("step", ["after-baseline-capture", "after-append",
                                  "after-fsync", "after-baseline-install"])
def test_a_crash_at_each_uncertain_establishment_step_never_drops_the_uncertainty(
        env, monkeypatch, step):
    """Before the initial cutover the inventory uncertainty is part of the
    cutover op, so no crash can leave a boundary without it: a crash before
    the op is complete leaves no boundary (the retry records the
    uncertainty again), and a crash after it leaves the uncertain op."""
    _write_legacy_candidate()
    active = [True]
    _inject_legacy_failure(monkeypatch, "file-read", active)

    class Crash(Exception):
        pass

    def crash(at):
        if at == step:
            raise Crash(at)

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", crash)
    try:
        journal.ensure_account_bound_cutover()
    except Crash:
        pass
    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    for op in _cutover_ops():
        assert op["payload"]["legacySpoolBaselineDigest"] is None, op
        assert op["payload"]["legacySpoolInventoryFailure"], op
    # Recovery: the cache is gone after a crash, and reads may have
    # recovered by now; the boundary is the uncertain one or a fresh one.
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink(missing_ok=True)
    boundary = journal.ensure_account_bound_cutover()
    [op] = _cutover_ops()
    assert P.boundary_from_op(op) == boundary
    assert boundary.legacy_spool_inventory_failure is not None
    assert not _cctally_core.LEGACY_SPOOL_BASELINE_PATH.exists()


@pytest.mark.parametrize("failure", ["file-read", "directory-listing"])
def test_one_inventory_incomplete_op_per_failure_episode(
        env, monkeypatch, failure):
    """After the cutover, the first failed inventory of a failure episode
    appends ONE current-stamped `legacy_spool_activity` op with reason
    `inventory-incomplete` and typed evidence; it claims no digest and moves
    no baseline. Later failures of the episode — a restarted process
    included — append nothing more. A successful inventory ends the episode
    without clearing anything, and the next failure starts a new one."""
    import _cctally_statusline as sl
    boundary = journal.ensure_account_bound_cutover()
    installed = _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes()
    _write_legacy_candidate()
    active = [True]
    _inject_legacy_failure(monkeypatch, failure, active)
    assert sl._rescan_legacy_spool() is True
    [op] = _spool_activity_ops()
    assert P.is_stamped(op)
    payload = op["payload"]
    assert payload["reason"] == "inventory-incomplete"
    assert "digest" not in payload
    assert payload["failure"]["operation"] == _FAILED_OPERATION[failure]
    assert payload["failure"]["error"] == _FAILED_ERRNO[failure]
    assert payload["failure"]["path"].startswith("statusline-candidates")
    # No baseline moved: the anchor is still the cutover's.
    assert _cctally_core.LEGACY_SPOOL_BASELINE_PATH.read_bytes() == installed
    assert (journal.latest_legacy_spool_anchor_digest()
            == boundary.legacy_spool_baseline_digest)
    # The same episode, in this process and after a restart (no cache).
    assert sl._rescan_legacy_spool() is False
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    assert sl._rescan_legacy_spool() is False
    assert len(_spool_activity_ops()) == 1

    # Reads recover: the complete inventory sees the candidate and anchors
    # a complete baseline under its own op; the episode is over.
    active[0] = False
    assert sl._rescan_legacy_spool() is True
    reasons = [op["payload"]["reason"] for op in _spool_activity_ops()]
    assert reasons == ["inventory-incomplete", "changed"]
    assert journal.read_anchored_legacy_spool_baseline() is not None
    assert sl._rescan_legacy_spool() is False

    # A new failure is a new episode, even when the inventory that ended
    # the last one appended nothing (an unchanged spool).
    active[0] = True
    assert sl._rescan_legacy_spool() is True
    active[0] = False
    assert sl._rescan_legacy_spool() is False
    active[0] = True
    assert sl._rescan_legacy_spool() is True
    reasons = [op["payload"]["reason"] for op in _spool_activity_ops()]
    assert reasons == ["inventory-incomplete", "changed",
                       "inventory-incomplete", "inventory-incomplete"]


def test_an_undurable_inventory_trigger_defers_with_an_os_error(
        env, monkeypatch):
    """If the `inventory-incomplete` op cannot be made durable, the rescan
    raises an `OSError` (the reducer defers as a waiting head and the OAuth
    writer publishes nothing) and no episode is recorded: the next rescan
    appends the op."""
    import errno
    import os
    import _cctally_statusline as sl
    journal.ensure_account_bound_cutover()
    _write_legacy_candidate()
    active = [True]
    _inject_legacy_failure(monkeypatch, "file-read", active)
    real_fsync = os.fsync
    failing = [True]

    def refusing_fsync(fd):
        if failing[0]:
            raise OSError(errno.EIO, "injected journal fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refusing_fsync)
    with pytest.raises(OSError) as raised:
        sl._rescan_legacy_spool()
    assert isinstance(raised.value, journal.LegacySpoolUncertaintyNotDurable)
    # Fix round K12 (K11R-7, spec section 7 *Durability*): the write landed
    # in the page cache before the failed fsync, so the op is visible, but a
    # crash can still lose it. While it cannot be made durable, a rescan
    # that sees it must not count the episode as recorded: it defers again.
    assert [op["payload"]["reason"] for op in _spool_activity_ops()] == [
        "inventory-incomplete"], "non-vacuity: the undurable op is visible"
    with pytest.raises(journal.LegacySpoolUncertaintyNotDurable):
        sl._rescan_legacy_spool()
    # Once the journal can be made durable again, the visible op is fsync'd
    # before it counts: one op, never a second for the same episode.
    failing[0] = False
    synced = []

    def recording_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    assert sl._rescan_legacy_spool() is False
    assert synced, "the visible uncertainty op was counted without an fsync"
    assert sl._rescan_legacy_spool() is False
    reasons = [op["payload"]["reason"] for op in _spool_activity_ops()]
    assert reasons == ["inventory-incomplete"]


# --------------------------------------------------------------------------
# K9K10-R5 / R10: a tail-read failure is never a zero extent
# --------------------------------------------------------------------------

def _refuse_tail_reads(monkeypatch, segment, active):
    """While ``active[0]``, `_complete_line_offset`'s tail read of
    ``segment`` fails with EIO (its stat keeps working)."""
    import builtins
    import errno
    import os
    import sys

    def refusing_open(file, *args, **kwargs):
        if (active[0] and not isinstance(file, int)
                and os.path.basename(os.fspath(file)) == segment
                and sys._getframe(1).f_code.co_name
                == "_complete_line_offset"):
            raise OSError(errno.EIO, "injected tail read failure",
                          os.fspath(file))
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(journal, "open", refusing_open, raising=False)


def test_a_tail_read_failure_never_freezes_a_zero_extent(env, monkeypatch):
    """K9K10-R5: the frozen extents of the immutable cutover op are read
    under `journal.lock` after the lookup. A tail read of an earlier segment
    failing there recorded that segment's extent as 0, making every
    historical record in it post-boundary forever. The establishment must
    fail closed: no cutover, no baseline; once reads recover, the real
    extent is frozen."""
    early = "observations-2026-09.jsonl"
    sep = _seed_segment(early, [_legacy_obs(1), _legacy_obs(2)])
    _seed_segment(SEGMENT, [_legacy_obs(3)])
    active = [False]
    _refuse_tail_reads(monkeypatch, early, active)

    def fail_after_capture(step):
        if step == "after-baseline-capture":
            active[0] = True

    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM",
                        fail_after_capture)
    with pytest.raises(journal.AccountBoundCutoverUnavailable):
        journal.ensure_account_bound_cutover()
    monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
    assert _cutover_ops() == [], "a zero extent was frozen into the cutover"
    assert not _cctally_core.LEGACY_SPOOL_BASELINE_PATH.exists()
    active[0] = False
    boundary = journal.ensure_account_bound_cutover()
    assert boundary.frozen_extents[early] == sep
    assert P.is_historical(P.Coordinate(early, 0), boundary)


def test_a_tail_read_failure_never_fakes_an_unavailable_baseline(
        env, monkeypatch):
    """K9K10-R10: the latest-anchor scan sized a segment with
    `_complete_line_offset`; a swallowed tail-read failure skipped the
    segment holding the latest anchoring op, so the reader saw the cutover's
    stale digest, rejected the valid baseline and journaled a spurious
    `baseline-unavailable` trigger. The lookup must raise instead (the
    reducer defers) and append nothing."""
    import _cctally_statusline as sl
    journal.ensure_account_bound_cutover()
    _write_legacy_candidate()
    assert sl._rescan_legacy_spool() is True        # one real "changed" op
    _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink()
    active = [True]
    _refuse_tail_reads(monkeypatch, SEGMENT, active)
    with pytest.raises(OSError):
        journal.latest_legacy_spool_anchor_digest()
    with pytest.raises(OSError):
        sl._rescan_legacy_spool()
    active[0] = False
    reasons = [op["payload"]["reason"] for op in _spool_activity_ops()]
    assert reasons == ["changed"]
    assert sl._rescan_legacy_spool() is False


# --------------------------------------------------------------------------
# Fix round K13 (review cycle 7 825-832-CD7-V01): a visible cutover is not an
# established boundary until it is durable (spec 5.5 step 4, section 7
# *Durability*, C6).
# --------------------------------------------------------------------------

NOVEMBER = dt.datetime(2026, 11, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
NEXT_SEGMENT = "observations-2026-11.jsonl"


class _HostDurability:
    """A host-loss model over the real filesystem: what a successful
    `os.fsync` made durable — a file's size at that fsync, and a directory's
    entries at its fsync. `host_loss` drops everything else from the
    journal, as a host crash after the page cache reached the disk only
    through those fsyncs would. ``fail`` (a path predicate) refuses the next
    matching fsync once with EIO; ``failing`` refuses every matching one."""

    def __init__(self, monkeypatch):
        import os
        self._os = os
        self.paths = {}
        self.sizes = {}
        self.entries = {}
        self.fail = None
        self.failing = None
        real_open, real_fsync = os.open, os.fsync

        def opener(path, flags, mode=0o777, *args, **kwargs):
            fd = real_open(path, flags, mode, *args, **kwargs)
            self.paths[fd] = os.path.abspath(os.fspath(path))
            return fd

        def fsync(fd):
            import errno
            path = self.paths.get(fd)
            refuse = self.failing or self.fail
            if path is not None and refuse is not None and refuse(path):
                if self.failing is None:
                    self.fail = None
                raise OSError(errno.EIO, "injected journal fsync failure")
            real_fsync(fd)
            if path is None:
                return
            if os.path.isdir(path):
                self.entries[path] = set(os.listdir(path))
            else:
                self.sizes[path] = os.fstat(fd).st_size

        monkeypatch.setattr(os, "open", opener)
        monkeypatch.setattr(os, "fsync", fsync)

    def durable(self, path) -> int:
        """The bytes of ``path`` a host crash keeps (0 when its directory
        entry, or its journal directory's, is not durable)."""
        os = self._os
        path = os.path.abspath(os.fspath(path))
        parent = os.path.dirname(path)
        if os.path.basename(path) not in self.entries.get(parent, ()):
            return 0
        grand = os.path.dirname(parent)
        if os.path.basename(parent) not in self.entries.get(grand, ()):
            return 0
        return self.sizes.get(path, 0)

    def host_loss(self):
        os = self._os
        jdir = _cctally_core.JOURNAL_DIR
        for name in os.listdir(jdir):
            keep = self.durable(jdir / name)
            if keep == 0:
                os.unlink(jdir / name)
                continue
            with open(jdir / name, "r+b") as handle:
                handle.truncate(keep)


def _durable_legacy_append(model, segment, record):
    """A previous-release writer appends an UNSTAMPED Claude observation to
    ``segment`` and makes it durable; returns its coordinate."""
    import os
    jdir = _cctally_core.JOURNAL_DIR
    path = jdir / segment
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        offset = os.fstat(fd).st_size
        os.write(fd, J.encode_line(J.unstamped_record(record)))
        os.fsync(fd)
    finally:
        os.close(fd)
    for directory in (jdir, jdir.parent):
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return P.Coordinate(segment, offset)


@pytest.mark.parametrize("path", ["found", "winner"])
def test_a_visible_cutover_whose_fsync_failed_is_established_only_once_durable(
        env, monkeypatch, path):
    """The first producer's cutover line reaches the page cache and its
    fsync fails. A later producer retries in the next month's segment and
    finds that visible op — through its unlocked scan (``found``) or its
    locked recheck (``winner``). It must make the original segment and the
    directory entries durable before it reports the boundary established,
    and defer (an `OSError`) while it cannot. A legacy writer then appends
    durably to the new segment and the host crashes: the boundary survives,
    and the legacy record stays excluded rather than become historical
    input of a re-established boundary (C6)."""
    import os
    model = _HostDurability(monkeypatch)
    cutover_segment = os.path.abspath(_cctally_core.JOURNAL_DIR / SEGMENT)

    def first_producer():
        with pytest.raises(OSError):
            journal.ensure_account_bound_cutover(now_utc=FIXED)

    if path == "found":
        model.fail = lambda p: p == cutover_segment
        first_producer()
    assert path == "winner" or _cutover_ops(), (
        "non-vacuity: the failed cutover line is visible")
    monkeypatch.setattr(journal, "_utc_now", lambda: NOVEMBER)
    if path == "winner":
        raced = []

        def seam(step):
            if step == "after-scan" and not raced:
                raced.append(step)
                monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", None)
                model.fail = lambda p: p == cutover_segment
                first_producer()
                assert _cutover_ops(), (
                    "non-vacuity: the failed cutover line is visible")

        monkeypatch.setattr(journal, "_ACCOUNT_BOUND_CUTOVER_SEAM", seam)
    else:
        # While the original segment still cannot be made durable, the
        # retry defers rather than accept the visible line.
        model.failing = lambda p: p == cutover_segment
        with pytest.raises(OSError):
            journal.ensure_account_bound_cutover()
        model.failing = None
    boundary = journal.ensure_account_bound_cutover()
    assert path == "found" or raced, "non-vacuity: the race ran"
    end = os.path.getsize(cutover_segment)
    assert len(_cutover_ops()) == 1
    assert boundary.segment == SEGMENT
    assert model.durable(cutover_segment) >= end, (
        "a visible cutover was accepted as established while its segment "
        "or directory entries were not durable")
    legacy = _durable_legacy_append(model, NEXT_SEGMENT, _legacy_obs(5))
    model.host_loss()
    survived = journal.find_account_bound_cutover()
    assert survived is not None and survived.segment == SEGMENT, (
        "a host crash lost the established boundary")
    admission = P.classify_admission(
        J.unstamped_record(_legacy_obs(5)), legacy,
        journal.ensure_account_bound_cutover())
    assert not admission.admitted, (
        "a post-cutover legacy record became historical input")
