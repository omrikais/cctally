"""#797 T2: the provider-activity scanner, its scan cache and the compact
witness (spec section 4.1, 4.2 and 6 T2).

The scanner reads synthetic transcripts built in ``tmp_path`` only; nothing
here reads the operator's real transcripts or data directory. The last group
of tests drives the real render intake (`_candidate_from_input`,
`_capture_arrival`, `_statusline_persist`) to pin the carrier rules: the
witness is frozen at capture, an identical intake reuses the first committed
witness, and a missing or malformed witness makes the v2 document malformed.
"""
from __future__ import annotations

import dataclasses
import fcntl
import importlib
import json
import os
import pathlib
import sys
import time

import pytest

from conftest import load_script, redirect_paths


# --- transcript builders ---------------------------------------------------


def _assistant(ts, *, request_id="req_01", model="claude-opus-5-5", **extra):
    record = {
        "parentUuid": None, "isSidechain": False, "userType": "external",
        "cwd": "/Users/u/proj", "sessionId": "s1", "version": "2.1.310",
        "message": {"id": "msg_01", "model": model, "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
        "type": "assistant", "uuid": "u-1", "timestamp": ts,
    }
    if request_id is not None:
        record["requestId"] = request_id
    record.update(extra)
    return json.dumps(record)


def _user(ts, text="hello"):
    return json.dumps({"type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": text}})


def _filler(nbytes):
    """Non-qualifying transcript lines (attachments, progress) summing to at
    least ``nbytes`` bytes."""
    line = json.dumps({"type": "attachment", "timestamp": "2026-10-26T00:00:00Z",
                       "attachment": {"content": "x" * 900}})
    count = nbytes // (len(line) + 1) + 1
    return [line] * count


def _write(path, lines, *, newline=True):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(lines) + ("\n" if newline and lines else "")
    path.write_text(data)
    return path


def _append(path, lines):
    with open(path, "a") as fh:
        for line in lines:
            fh.write(line + "\n")


def _subagent(main, name):
    return pathlib.Path(str(main)[:-len(".jsonl")]) / "subagents" / name


@pytest.fixture
def pa():
    return importlib.import_module("_lib_provider_activity")


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "spool" / "activity-cache" / ("c" * 64 + ".json")


@pytest.fixture
def opens(pa, monkeypatch):
    """Every transcript file the scanner opens, by base name, per call."""
    seen = []
    real = pa._open_file

    def counting(path):
        seen.append(os.path.basename(path))
        return real(path)

    monkeypatch.setattr(pa, "_open_file", counting)
    return seen


def _capture(pa, main, cache):
    return pa.capture_activity(None if main is None else str(main), cache_path=cache)


# --- constants and the typed result ----------------------------------------


def test_constants_are_the_specified_values(pa):
    assert pa.EVIDENCE_REVISION == 1 and pa.PARSER_REVISION == 1
    assert pa.UNKNOWN_REASONS == (
        "no-transcript-path", "missing", "unreadable", "malformed",
        "scan-exhausted", "coverage-incomplete", "no-qualifying-record")
    assert (pa.MAX_FILE_BYTES, pa.CHUNK_BYTES) == (1 << 20, 64 << 10)
    assert (pa.MAX_INVENTORY, pa.ARRIVAL_FILE_BUDGET) == (4096, 8)


# --- qualifying records ----------------------------------------------------


def test_latest_instant_across_main_and_subagent_files(pa, tmp_path, cache):
    main = _write(tmp_path / "p" / "s1.jsonl", [
        _assistant("2026-10-26T10:00:00.000Z"), _user("2026-10-26T10:00:01Z")])
    _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:05:00.250Z")])
    _write(_subagent(main, "agent-b.jsonl"), [_assistant("2026-10-26T09:00:00Z")])
    _write(_subagent(main, "not-an-agent.jsonl"), [_assistant("2026-10-26T11:00:00Z")])
    w = _capture(pa, main, cache)
    assert (w.status, w.instant_utc, w.reason) == (
        "known", "2026-10-26T10:05:00.250Z", None)
    assert (w.files_total, w.files_examined) == (3, 3)
    assert (w.evidence_revision, w.parser_revision) == (1, 1)
    assert len(w.digest) == 64
    # The main transcript holds the latest when it is the latest.
    _append(main, [_assistant("2026-10-26T12:00:00Z")])
    assert _capture(pa, main, cache).instant_utc == "2026-10-26T12:00:00Z"


def test_error_response_counts_synthetic_does_not(pa, tmp_path, cache):
    main = _write(tmp_path / "s.jsonl", [
        _assistant("2026-10-26T10:00:00Z", request_id="req_old"),
        _assistant("2026-10-26T10:01:00Z", request_id="req_err",
                   isApiErrorMessage=True, error="rate_limit"),
        _assistant("2026-10-26T10:02:00Z", request_id="req_syn", model="<synthetic>"),
        _assistant("2026-10-26T10:03:00Z", request_id=None, model="<synthetic>"),
    ])
    w = _capture(pa, main, cache)
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:01:00Z")


def test_entry_without_request_id_does_not_count(pa, tmp_path, cache):
    main = _write(tmp_path / "s.jsonl", [
        _assistant("2026-10-26T10:00:00Z"),
        _assistant("2026-10-26T10:01:00Z", request_id=None),
        _assistant("2026-10-26T10:02:00Z", request_id=""),
        _user("2026-10-26T10:03:00Z"),
    ])
    assert _capture(pa, main, cache).instant_utc == "2026-10-26T10:00:00Z"
    only = _write(tmp_path / "t.jsonl", [
        _assistant("2026-10-26T10:01:00Z", request_id=None), _user("2026-10-26T10:03:00Z")])
    w = _capture(pa, only, tmp_path / "other.json")
    assert (w.status, w.instant_utc, w.reason) == ("unknown", None, "no-qualifying-record")


# --- missing, unreadable, malformed and bounds -------------------------------


def test_missing_and_unreadable_paths(pa, tmp_path, cache):
    for path in (None, ""):
        w = pa.capture_activity(path, cache_path=cache)
        assert (w.status, w.reason, w.files_total) == ("unknown", "no-transcript-path", 0)
    w = _capture(pa, tmp_path / "absent.jsonl", cache)
    assert (w.status, w.reason) == ("unknown", "missing")
    directory = tmp_path / "dir.jsonl"
    directory.mkdir()
    assert _capture(pa, directory, cache).reason == "unreadable"
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    assert _capture(pa, fifo, cache).reason == "unreadable"  # and never blocks
    # An unreadable subagent file makes the whole result unknown.
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    _subagent(main, "agent-x.jsonl").mkdir(parents=True)
    w = _capture(pa, main, tmp_path / "c2.json")
    assert (w.status, w.reason) == ("unknown", "unreadable")


def test_scan_exhausted_at_1_mib(pa, tmp_path, cache):
    record = _assistant("2026-10-26T10:00:00Z")
    over = _write(tmp_path / "over.jsonl", [record] + _filler(pa.MAX_FILE_BYTES + 1))
    w = _capture(pa, over, cache)
    assert (w.status, w.reason) == ("unknown", "scan-exhausted")
    # Just inside the bound: the record's line ends within the last 1 MiB.
    filler = _filler(pa.MAX_FILE_BYTES - 64 * 1024)
    inside = _write(tmp_path / "inside.jsonl", ["{}"] * 3 + [record] + filler)
    assert sum(len(f) + 1 for f in filler) + len(record) + 1 <= pa.MAX_FILE_BYTES
    w = _capture(pa, inside, tmp_path / "c2.json")
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:00:00Z")


def test_partial_final_line_and_oversized_line(pa, tmp_path, cache):
    full = _assistant("2026-10-26T10:05:00Z", request_id="req_new")
    partial = _write(tmp_path / "partial.jsonl", [
        _assistant("2026-10-26T10:00:00Z"), full[: len(full) // 2]], newline=False)
    w = _capture(pa, partial, cache)
    assert (w.status, w.reason) == ("unknown", "malformed")
    # A complete malformed line that could be an assistant record is unknown.
    broken = _write(tmp_path / "broken.jsonl", [
        _assistant("2026-10-26T10:00:00Z"), full[:-1]])
    assert _capture(pa, broken, tmp_path / "c2.json").reason == "malformed"
    # A qualifying record whose timestamp cannot be read is malformed too.
    bad_ts = _write(tmp_path / "badts.jsonl", [_assistant("yesterday")])
    assert _capture(pa, bad_ts, tmp_path / "c3.json").reason == "malformed"
    # A complete garbage line that cannot be an assistant record is ignored,
    # and so is trailing whitespace.
    noise = _write(tmp_path / "noise.jsonl", [
        _assistant("2026-10-26T10:00:00Z"), "{not json at all", "   "])
    assert _capture(pa, noise, tmp_path / "c4.json").instant_utc == "2026-10-26T10:00:00Z"
    # A single final line larger than the bound cannot be completed.
    huge = json.dumps({"type": "user", "message": {"content": "y" * (pa.MAX_FILE_BYTES + 10)}})
    oversized = _write(tmp_path / "huge.jsonl", [_assistant("2026-10-26T10:00:00Z"), huge])
    assert _capture(pa, oversized, tmp_path / "c5.json").reason == "scan-exhausted"


def test_fractional_timestamps_keep_precision(pa, tmp_path, cache):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00.5Z")])
    _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:00:00.123456789Z")])
    w = _capture(pa, main, cache)
    assert w.instant_utc == "2026-10-26T10:00:00.5Z"
    one = _write(tmp_path / "q" / "s.jsonl", [_assistant("2026-10-26T10:00:00.123456789Z")])
    assert _capture(pa, one, tmp_path / "c2.json").instant_utc == (
        "2026-10-26T10:00:00.123456789Z")
    whole = _write(tmp_path / "r" / "s.jsonl", [_assistant("2026-10-26T10:00:00+00:00")])
    assert _capture(pa, whole, tmp_path / "c3.json").instant_utc == "2026-10-26T10:00:00Z"
    assert pa.instant_key("2026-10-26T10:00:00.5Z") > pa.instant_key(
        "2026-10-26T10:00:00.123456789Z")
    assert pa.instant_precision("2026-10-26T10:00:00.120Z") == 3
    assert pa.instant_precision("2026-10-26T10:00:00Z") == 0


# --- inventory and the per-arrival budget -----------------------------------


def test_inventory_bound_4096_is_coverage_incomplete(pa, tmp_path, cache, monkeypatch):
    monkeypatch.setattr(pa, "ARRIVAL_FILE_BUDGET", 10_000)
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    sub = _subagent(main, "x").parent
    sub.mkdir(parents=True)
    for i in range(pa.MAX_INVENTORY):
        (sub / f"agent-{i:05d}.jsonl").touch()
    w = _capture(pa, main, cache)
    assert (w.status, w.files_total, w.files_examined) == ("known", 4097, 4097)
    (sub / "agent-99999.jsonl").touch()
    w = _capture(pa, main, tmp_path / "c2.json")
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")


def test_unresolved_membership_change_is_coverage_incomplete(pa, tmp_path, cache, monkeypatch):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    gone = _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:01:00Z")])
    assert _capture(pa, main, cache).status == "known"
    # A cached member that disappeared from the inventory is unresolved.
    gone.unlink()
    w = _capture(pa, main, cache)
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")
    # A listed member that vanished before it could be opened is unresolved.
    real = pa._list_subagents
    monkeypatch.setattr(pa, "_list_subagents", lambda d: (
        lambda r: (r[0] + ("agent-ghost.jsonl",), r[1]))(real(d)))
    w = _capture(pa, main, tmp_path / "c2.json")
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")
    # An unreadable inventory is incomplete coverage, not "no subagents".
    monkeypatch.setattr(pa, "_list_subagents", lambda d: ((), False))
    w = _capture(pa, main, tmp_path / "c3.json")
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")


def test_a_subagent_file_that_leaves_the_listing_keeps_its_instant(pa, tmp_path, cache):
    # F-I3-1: t_m is an upper bound on the input's sample instant, so a
    # response already observed in a subagent file must keep contributing
    # after the file leaves the listing; a deletion never lowers t_m.
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    gone = _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:05:00Z")])
    _write(_subagent(main, "agent-b.jsonl"), [_assistant("2026-10-26T10:01:00Z")])
    assert _capture(pa, main, cache).instant_utc == "2026-10-26T10:05:00Z"
    gone.unlink()
    # The membership change itself is one coverage-incomplete arrival.
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    for _ in range(2):
        w = _capture(pa, main, cache)
        assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:05:00Z")
    # A later response anywhere still moves t_m forward.
    _append(main, [_assistant("2026-10-26T10:09:00Z")])
    w = _capture(pa, main, cache)
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:09:00Z")


def test_a_replaced_subagent_incarnation_keeps_its_instant(pa, tmp_path, cache):
    # The same rule for a replacement: the old incarnation's observed
    # response keeps contributing once the new one is known.
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    sub = _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:05:00Z")])
    assert _capture(pa, main, cache).instant_utc == "2026-10-26T10:05:00Z"
    replacement = sub.with_name("r")
    replacement.write_text(_assistant("2026-10-26T10:01:00Z") + "\n")
    os.replace(replacement, sub)
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    _append(sub, [_user("2026-10-26T10:06:00Z")])
    w = _capture(pa, main, cache)
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:05:00Z")


def test_eight_file_budget_rotates_fairly_with_persisted_progress(
        pa, tmp_path, cache, opens):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    names = [f"agent-{i:02d}.jsonl" for i in range(20)]
    for i, name in enumerate(names):
        _write(_subagent(main, name), [_assistant(f"2026-10-26T10:{i + 1:02d}:00Z")])
    hot = names[:2]

    def arrival():
        for name in hot:  # two hot files grow before every arrival
            _append(_subagent(main, name), [_user("2026-10-26T11:00:00Z")])
        opens.clear()
        w = _capture(pa, main, cache)
        return w, [n for n in opens if n.startswith("agent-")]

    w1, first = arrival()
    assert first == names[:8]
    assert (w1.status, w1.reason, w1.files_total, w1.files_examined) == (
        "unknown", "coverage-incomplete", 21, 9)
    # Progress persists: the next arrival continues after the cursor, so the
    # hot files that changed cannot starve the never-examined ones.
    w2, second = arrival()
    assert second == names[8:16]
    # Examined: the main transcript, six unchanged cache hits and eight new
    # scans; the two hot files that changed again wait for their turn.
    assert (w2.status, w2.files_examined) == ("unknown", 15)
    w3, third = arrival()
    assert third == names[16:] + hot
    assert (w3.status, w3.instant_utc, w3.files_examined) == (
        "known", "2026-10-26T10:20:00Z", 21)


# --- the scan cache --------------------------------------------------------


def test_cache_hit_and_miss_by_device_inode_size_mtime_and_parser_revision(
        pa, tmp_path, cache, opens, monkeypatch):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    sub = _write(_subagent(main, "agent-a.jsonl"), [_assistant("2026-10-26T10:01:00Z")])
    first = _capture(pa, main, cache)
    assert sorted(opens) == ["agent-a.jsonl", "s.jsonl"]
    # Hit: nothing changed, nothing is opened, the same evidence comes back.
    opens.clear()
    assert _capture(pa, main, cache) == first and opens == []
    # mtime alone changes the key.
    st = sub.stat()
    os.utime(sub, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    opens.clear()
    assert _capture(pa, main, cache).status == "known" and opens == ["agent-a.jsonl"]
    # Size (an append) changes the key.
    _append(sub, [_assistant("2026-10-26T10:02:00Z")])
    opens.clear()
    w = _capture(pa, main, cache)
    assert opens == ["agent-a.jsonl"] and w.instant_utc == "2026-10-26T10:02:00Z"
    # Inode: a replacement is a different incarnation (and unknown coverage).
    replacement = sub.with_name("tmp")
    replacement.write_bytes(sub.read_bytes())
    os.replace(replacement, sub)
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    # Device: a record whose device differs is a different incarnation.
    _write(_subagent(main, "agent-b.jsonl"), [_assistant("2026-10-26T10:03:00Z")])
    other = tmp_path / "c2.json"
    _capture(pa, main, other)
    doc = json.loads(other.read_text())
    doc["subagents"]["agent-b.jsonl"]["device"] += 1
    other.write_text(json.dumps(doc))
    assert _capture(pa, main, other).reason == "coverage-incomplete"
    # Parser revision: a cache written under another revision is a miss.
    third = tmp_path / "c3.json"
    _capture(pa, main, third)
    monkeypatch.setattr(pa, "PARSER_REVISION", 2)
    opens.clear()
    w = _capture(pa, main, third)
    assert w.parser_revision == 2 and "s.jsonl" in opens


def test_replaced_or_shrunk_file_is_unknown(pa, tmp_path, cache):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    sub = _write(_subagent(main, "agent-a.jsonl"), [
        _assistant("2026-10-26T10:01:00Z"), _user("2026-10-26T10:02:00Z")])
    assert _capture(pa, main, cache).status == "known"
    with open(sub, "r+") as fh:
        fh.truncate(sub.stat().st_size - 10)
    w = _capture(pa, main, cache)
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")
    # A replaced main transcript is unknown too.
    main = _write(tmp_path / "q" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    other = tmp_path / "c2.json"
    assert _capture(pa, main, other).status == "known"
    replacement = main.with_name("tmp.jsonl")
    replacement.write_text(_assistant("2026-10-26T10:00:00Z") + "\n")
    os.replace(replacement, main)
    w = _capture(pa, main, other)
    assert (w.status, w.reason) == ("unknown", "coverage-incomplete")


def test_another_transcript_path_never_reuses_the_first_paths_records(pa, tmp_path, cache):
    first = _write(tmp_path / "a" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    _write(_subagent(first, "agent-a.jsonl"), [_assistant("2026-10-26T10:01:00Z")])
    assert _capture(pa, first, cache).instant_utc == "2026-10-26T10:01:00Z"
    second = _write(tmp_path / "b" / "s.jsonl", [_assistant("2026-10-26T11:00:00Z")])
    _write(_subagent(second, "agent-a.jsonl"), [_assistant("2026-10-26T11:01:00Z")])
    w = _capture(pa, second, cache)
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T11:01:00Z")


def test_lost_or_contended_cache_is_unknown(pa, tmp_path, cache):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    for i in range(10):
        _write(_subagent(main, f"agent-{i}.jsonl"), [_assistant("2026-10-26T10:01:00Z")])
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    # Lost: progress is gone, so the next arrival is still incomplete.
    cache.unlink()
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    # Contended: another arrival holds the cache, so this one cannot use it.
    lock = os.open(pa.cache_lock_path(cache), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        w = _capture(pa, main, cache)
        assert (w.status, w.reason) == ("unknown", "coverage-incomplete")
    finally:
        os.close(lock)
    assert _capture(pa, main, cache).status == "known"
    # Invalid cache data degrades to unknown, then recovers.
    cache.write_text("{not json")
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    assert _capture(pa, main, cache).status == "known"


def test_cached_unknown_never_becomes_known(pa, tmp_path, cache, opens):
    main = _write(tmp_path / "p" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    full = _assistant("2026-10-26T10:05:00Z")
    sub = _write(_subagent(main, "agent-a.jsonl"), [full[:40]], newline=False)
    assert _capture(pa, main, cache).reason == "malformed"
    opens.clear()
    # Unchanged key: the cached unknown is reused, never rescanned into known.
    assert _capture(pa, main, cache).reason == "malformed" and opens == []
    # A replaced incarnation keeps its cached unknown while it is unchanged.
    _write(_subagent(main, "agent-a.jsonl"), [full])
    replacement = sub.with_name("r")
    replacement.write_bytes(sub.read_bytes())
    os.replace(replacement, sub)
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    assert _capture(pa, main, cache).reason == "coverage-incomplete"
    # Only a changed key is examined again.
    _append(sub, [_user("2026-10-26T10:06:00Z")])
    w = _capture(pa, main, cache)
    assert (w.status, w.instant_utc) == ("known", "2026-10-26T10:05:00Z")


# --- the compact witness codec ---------------------------------------------


def test_witness_codec_round_trips_and_rejects_malformed(pa, tmp_path, cache):
    main = _write(tmp_path / "s.jsonl", [_assistant("2026-10-26T10:00:00.25Z")])
    for w in (_capture(pa, main, cache), _capture(pa, None, cache)):
        doc = pa.witness_to_json(w)
        assert json.loads(json.dumps(doc)) == doc
        assert pa.witness_from_json(doc) == w
    good = pa.witness_to_json(_capture(pa, main, cache))
    bad_docs = [None, [], {**good, "extra": 1}]
    for key in good:
        bad_docs.append({k: v for k, v in good.items() if k != key})
    bad_docs += [
        {**good, "status": "maybe"},
        {**good, "evidenceRevision": True},
        {**good, "parserRevision": "1"},
        {**good, "instant": None},
        {**good, "instant": "2026-10-26 10:00:00"},
        {**good, "reason": "missing"},
        {**good, "status": "unknown", "instant": None, "reason": "nope"},
        {**good, "filesTotal": -1},
        {**good, "filesExamined": good["filesTotal"] + 1},
        {**good, "digest": "g" * 64},
    ]
    for doc in bad_docs:
        with pytest.raises(pa.MalformedWitness):
            pa.witness_from_json(doc)


# --- the carrier: render intake ---------------------------------------------


def _status(app, *, transcript, percent=20, session="sess-797"):
    now = int(time.time())
    payload = {"session_id": session, "rate_limits": {"seven_day": {
        "used_percentage": percent,
        "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3 * 86400)),
    }}}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return app._lib_statusline.parse_statusline_stdin(json.dumps(payload).encode())


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    monkeypatch.setattr(app, "_nudge_dashboard_repaint", lambda: None)
    return app


def _slot(sl, candidate):
    return sl._slot_candidate(candidate.binding.account_key, candidate.token)


def test_render_capture_embeds_the_witness_in_the_v2_candidate(app, tmp_path):
    sl = sys.modules["_cctally_statusline"]
    main = _write(tmp_path / "proj" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    candidate = sl._candidate_from_input(_status(app, transcript=main),
                                         received_at=int(time.time()))
    assert candidate.activity.status == "known"
    assert candidate.activity.instant_utc == "2026-10-26T10:00:00Z"
    sl._capture_arrival(candidate)
    raw = sl._candidate_path(candidate.token, account_key=candidate.binding.account_key)
    doc = json.loads(raw.read_text())
    pa = sys.modules["_lib_provider_activity"]
    assert pa.witness_from_json(doc["activity"]) == candidate.activity
    assert str(main) not in raw.read_text() and "sess-797" not in raw.read_text()
    assert _slot(sl, candidate) == candidate
    # The scan cache lives in the v2 spool, one file per contributor.
    import _cctally_core as core
    cached = core.STATUSLINE_ACCOUNT_BOUND_DIR / "activity-cache" / f"{candidate.token}.json"
    assert cached.is_file() and str(main) not in cached.read_text()
    # A render without a transcript path carries an explicit unknown.
    blind = sl._candidate_from_input(_status(app, transcript=None, session="other"),
                                     received_at=int(time.time()))
    assert (blind.activity.status, blind.activity.reason) == (
        "unknown", "no-transcript-path")


def test_missing_witness_makes_v2_document_malformed(app, tmp_path):
    sl = sys.modules["_cctally_statusline"]
    lib = sys.modules["_lib_statusline_candidates"]
    main = _write(tmp_path / "proj" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    now = int(time.time())
    candidate = sl._candidate_from_input(_status(app, transcript=main), received_at=now)
    key = candidate.binding.account_key
    good = sl._candidate_document(candidate)
    assert sl._parse_candidate(json.dumps(good).encode(), account_key=key,
                               token=candidate.token, now_epoch=now) == candidate
    missing = {k: v for k, v in good.items() if k != "activity"}
    extra = {**good, "activity": {**good["activity"], "path": "/x"}}
    wrong = {**good, "activity": {**good["activity"], "filesTotal": "3"}}
    null = {**good, "activity": None}
    for doc in (missing, extra, wrong, null):
        with pytest.raises(lib.StateValidationError):
            sl._parse_candidate(json.dumps(doc).encode(), account_key=key,
                                token=candidate.token, now_epoch=now)
    # The intake evaluation line revalidates its members the same way.
    line = sl._evaluation_line(0, now, (candidate,))
    assert sl._parse_evaluation(line, account_key=key).members == (candidate,)
    doc = json.loads(line)
    del doc["members"][0]["candidate"]["activity"]
    with pytest.raises(lib.StateValidationError):
        sl._parse_evaluation(json.dumps(doc).encode(), account_key=key)
    # An in-memory candidate without a witness cannot be serialized.
    with pytest.raises(ValueError):
        sl._candidate_document(dataclasses.replace(candidate, activity=None))


def test_witness_fits_4kib_candidate(app, tmp_path):
    sl = sys.modules["_cctally_statusline"]
    lib = sys.modules["_lib_statusline_candidates"]
    pa = sys.modules["_lib_provider_activity"]
    import _lib_account_binding as binding_lib
    now = int(time.time())
    # The largest realistic candidate: both axes at full float precision, a
    # maximal email, a fractional instant at nanosecond precision and the
    # largest coverage counts.
    import _lib_accounts
    natural = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    binding = binding_lib.capture({
        "status": "identified", "natural_id": natural,
        "account_key": _lib_accounts.account_key("claude", natural),
        "email": ("e" * 64) + "@" + ("d" * 185) + ".com",
        "plan_type": "max_20x_enterprise_team_premium"},
        credential="c" * 200)
    assert binding is not None
    witness = pa.ActivityWitness(
        evidence_revision=pa.EVIDENCE_REVISION, parser_revision=pa.PARSER_REVISION,
        status="known", instant_utc="2026-10-26T10:00:00.123456789Z", reason=None,
        files_total=pa.MAX_INVENTORY + 1, files_examined=pa.MAX_INVENTORY + 1,
        digest="f" * 64)
    candidate = lib.Candidate(
        token="a" * 64, received_at=now,
        five_hour=lib.AxisValue(33.33333333333333, now + 5 * 3600),
        seven_day=lib.AxisValue(66.66666666666667, now + 7 * 86400),
        binding=binding, activity=witness)
    data = json.dumps(sl._candidate_document(candidate), allow_nan=False).encode()
    print(f"largest realistic v2 candidate: {len(data)} bytes; witness "
          f"{len(json.dumps(pa.witness_to_json(witness)).encode())} bytes")
    assert len(data) <= lib.CANDIDATE_DOCUMENT_MAX_BYTES // 2
    assert sl._parse_candidate(data, account_key=binding.account_key,
                               token=candidate.token, now_epoch=now) == candidate
    # At the limit: a document of exactly 4 KiB loads; one byte more does not.
    doc = sl._candidate_document(candidate)
    pad = lib.CANDIDATE_DOCUMENT_MAX_BYTES - len(data)
    exact = json.dumps(doc, allow_nan=False).encode()[:-1] + b" " * pad + b"}"
    assert len(exact) == lib.CANDIDATE_DOCUMENT_MAX_BYTES
    assert sl._parse_candidate(exact, account_key=binding.account_key,
                               token=candidate.token, now_epoch=now) == candidate
    with pytest.raises(lib.StateValidationError):
        sl._parse_candidate(exact[:-1] + b" }", account_key=binding.account_key,
                            token=candidate.token, now_epoch=now)


def test_duplicate_intake_reuses_first_evidence(app, tmp_path):
    sl = sys.modules["_cctally_statusline"]
    main = _write(tmp_path / "proj" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    now = int(time.time())
    first = sl._candidate_from_input(_status(app, transcript=main), received_at=now)
    assert sl._capture_arrival(first) is not None
    # Same contributor, same second, same reading: the transcript moved on,
    # so a fresh capture differs, but the arrival is the same immutable one.
    _append(main, [_assistant("2026-10-26T10:09:00Z")])
    again = sl._candidate_from_input(_status(app, transcript=main), received_at=now)
    assert again.activity != first.activity
    assert sl._capture_arrival(again) is None
    assert _slot(sl, first).activity == first.activity
    tail = sl._intake_tail_evaluation(first.binding.account_key)
    assert [m.activity for m in tail.members] == [first.activity]
    # A later second is a distinct arrival and carries its own evidence.
    later = sl._candidate_from_input(_status(app, transcript=main), received_at=now + 1)
    assert sl._capture_arrival(later) is not None
    assert _slot(sl, later).activity.instant_utc == "2026-10-26T10:09:00Z"


def test_append_and_path_replacement_after_capture_change_nothing(
        app, tmp_path, monkeypatch):
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    main = _write(tmp_path / "proj" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    parsed = _status(app, transcript=main)
    real_crash_point = sl._crash_point

    def after_capture(name):
        if name == "after-capture":
            _append(main, [_assistant("2026-10-26T10:30:00Z")])
            replacement = main.with_name("other.jsonl")
            replacement.write_text(_assistant("2026-10-26T11:00:00Z") + "\n")
            os.replace(replacement, main)
            # Nothing downstream of the capture may rescan.
            monkeypatch.setattr(pa, "capture_activity", lambda *a, **k: (
                _ for _ in ()).throw(AssertionError("rescanned after capture")))
        real_crash_point(name)

    monkeypatch.setattr(sl, "_crash_point", after_capture)
    sl._statusline_persist(parsed, sync_for_test=True)
    import _outcome_quarantine_trace as trace
    published = [r for r in trace.journal_records()
                 if r.get("t") == "obs" and "publication_id" in r.get("payload", {})]
    assert len(published) == 1
    provenance = published[0]["payload"]["publication"]["sevenDay"]
    assert provenance["providerActivityAt"] == "2026-10-26T10:00:00Z"
    assert provenance["providerActivityStatus"] == "known"
    assert provenance["providerActivityRevision"] == pa.EVIDENCE_REVISION
    blob = json.dumps(published[0])
    assert str(main) not in blob and "sess-797" not in blob


def test_frozen_evidence_never_changes(app, tmp_path, monkeypatch):
    sl = sys.modules["_cctally_statusline"]
    pa = sys.modules["_lib_provider_activity"]
    main = _write(tmp_path / "proj" / "s.jsonl", [_assistant("2026-10-26T10:00:00Z")])
    now = int(time.time())
    candidate = sl._candidate_from_input(_status(app, transcript=main), received_at=now)
    sl._capture_arrival(candidate)
    key = candidate.binding.account_key
    # Later scans, a scan-bound overflow and a lost cache change nothing the
    # intake or the spool already holds, and reading them never rescans.
    _append(main, _filler(pa.MAX_FILE_BYTES + 1))
    import _cctally_core as core
    for path in (core.STATUSLINE_ACCOUNT_BOUND_DIR / "activity-cache").iterdir():
        path.unlink()
    monkeypatch.setattr(pa, "capture_activity", lambda *a, **k: (
        _ for _ in ()).throw(AssertionError("frozen evidence was recomputed")))
    tail = sl._intake_tail_evaluation(key)
    assert [m.activity for m in tail.members] == [candidate.activity]
    assert _slot(sl, candidate).activity == candidate.activity
    assert sl._read_account_candidates(key, now_epoch=now, prune=False)[0].activity == (
        candidate.activity)
