"""#968 T2 + T7 ingest edges: compressed rollouts read in decoded space."""
from __future__ import annotations

import io
import pathlib
import shutil
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    compress_in_place, snapshot_tables, token_records, write_rollout,
    zstd_bytes)

from test_codex_prune_drain import _no_detached_drains  # noqa: F401,E402

import _lib_codex_rollout_container as rc  # noqa: E402

UUID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0003"
NAME = f"rollout-2026-07-20T10-00-00-{UUID}.jsonl"
ACCOUNTING = ("codex_session_entries", "quota_window_snapshots")
EXCLUDED = ("id", "codex_session_entries.id", "quota_window_snapshots.id")


def _store(tmp_path, monkeypatch, name):
    ns = load_script()
    # This module runs a conversations pass over July-2026 fixtures, so it
    # keeps the retention prune off (Global Constraints).
    redirect_paths(
        ns, monkeypatch, tmp_path / name / "data")
    provider = tmp_path / "codex-provider"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    return ns, day


def _sync(ns):
    conn = ns["open_cache_db"]()
    try:
        return ns["sync_codex_cache"](conn)
    finally:
        conn.close()


def _snapshot(ns, rename_from=None, rename_to=None):
    conn = ns["open_cache_db"]()
    try:
        snap = snapshot_tables(conn, ACCOUNTING, exclude_columns=EXCLUDED)
    finally:
        conn.close()
    if rename_from is None:
        return snap
    return {t: [tuple(rename_to if v == rename_from else v for v in row)
                for row in rows] for t, rows in snap.items()}


def _cursor(ns):
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM codex_session_files").fetchone())
    finally:
        conn.close()


def test_compressed_only_rollout_ingests_exactly_like_its_plain_twin(
        tmp_path, monkeypatch):
    """T2: same rows, decoded offsets, logical source path, anchored cursor."""
    ns_p, day_p = _store(tmp_path, monkeypatch, "plain")
    plain = day_p / NAME
    records = token_records(6, session_id="s1")
    records[-1]["payload"]["rate_limits"] = {
        "limit_id": "codex", "primary": {"used_percent": 31.0,
        "window_minutes": 10080, "resets_at": 1784966699}}
    offsets = write_rollout(plain, records)
    _sync(ns_p)
    reference = _snapshot(ns_p, str(plain), "<logical>")
    assert reference["quota_window_snapshots"]

    ns_z, day_z = _store(tmp_path, monkeypatch, "zstd")
    logical = day_z / NAME
    write_rollout(logical, records)
    decoded = logical.read_bytes()
    compress_in_place(logical)
    _sync(ns_z)
    # 92e06df85 never discovers the compressed rollout: zero rows.
    assert _snapshot(ns_z, str(logical), "<logical>") == reference
    cursor = _cursor(ns_z)
    assert cursor["path"] == str(logical)
    assert cursor["last_byte_offset"] == offsets[-1] == len(decoded)
    assert cursor.get("container_kind") == rc.ZSTD
    assert cursor.get("decoded_size_bytes") == len(decoded)
    with io.BytesIO(decoded) as fh:
        anchor, _ = rc.anchor_over(fh, start_offset=0,
                                   start_anchor=rc.anchor_seed(),
                                   end_offset=len(decoded))
    assert bytes(cursor.get("content_anchor") or b"") == anchor
    assert cursor.get("content_anchor_offset") == len(decoded)


def test_compressed_conversation_events_match_the_plain_twin(
        tmp_path, monkeypatch):
    """T2, transcript side."""
    def events(ns, path):
        conv = ns["open_conversations_db"]()
        try:
            ns["sync_codex_conversations"](conv)
            raw = [tuple("<logical>" if v == str(path) else v for v in row)
                    for row in conv.execute(
                        "SELECT source_path, line_offset, conversation_key, "
                        "record_type, event_type, payload_json "
                        "FROM codex_conversation_events ORDER BY line_offset")]
            normalized = [tuple(row) for row in conv.execute(
                "SELECT line_offset,kind,model,text,content_digest,detail_json "
                "FROM codex_conversation_messages ORDER BY line_offset")]
            assert normalized
            return raw, normalized
        finally:
            conv.close()

    records = token_records(4, session_id="s1") + [
        {"timestamp": "2026-07-20T10:08:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "read this rollout"}]}},
        {"timestamp": "2026-07-20T10:09:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [
             {"type": "output_text", "text": "rollout read"}]}},
    ]
    ns_p, day_p = _store(tmp_path, monkeypatch, "plain")
    write_rollout(day_p / NAME, records)
    _sync(ns_p)
    reference = events(ns_p, day_p / NAME)
    ns_z, day_z = _store(tmp_path, monkeypatch, "zstd")
    write_rollout(day_z / NAME, records)
    compress_in_place(day_z / NAME)
    _sync(ns_z)
    assert reference and events(ns_z, day_z / NAME) == reference


def test_compressed_rollout_without_session_meta_keeps_the_filename_uuid(
        tmp_path, monkeypatch):
    """T2: with no session_meta, both forms fall back to the logical filename's
    UUID, so `codex-session` groups the compressed rollout under the same id."""
    records = token_records(3, session_id="unused")[1:]   # no session_meta

    def session_ids(ns):
        conn = ns["open_cache_db"]()
        try:
            return {r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM codex_session_entries")}
        finally:
            conn.close()

    ns_p, day_p = _store(tmp_path, monkeypatch, "plain")
    write_rollout(day_p / NAME, records)
    _sync(ns_p)
    reference = _snapshot(ns_p, str(day_p / NAME), "<logical>")
    assert reference["codex_session_entries"] and session_ids(ns_p) == {UUID}

    ns_z, day_z = _store(tmp_path, monkeypatch, "zstd")
    write_rollout(day_z / NAME, records)
    compress_in_place(day_z / NAME)
    _sync(ns_z)
    assert _snapshot(ns_z, str(day_z / NAME), "<logical>") == reference
    assert session_ids(ns_z) == {UUID}


def test_partial_decoded_resume_continues_at_the_decoded_offset(
        tmp_path, monkeypatch):
    """T7: a budget-stopped compressed ingest resumes without re-reading."""
    ns, day = _store(tmp_path, monkeypatch, "resume")
    logical = day / NAME
    offsets = write_rollout(logical, token_records(8, session_id="s1"))
    full = logical.read_bytes()
    compress_in_place(logical)
    _sync(ns)
    complete = _snapshot(ns)
    cut = offsets[4]
    with open(day / "prefix.bin", "wb") as fh:
        fh.write(full[:cut])
    with open(day / "prefix.bin", "rb") as fh:
        prefix_anchor, _ = rc.anchor_over(
            fh, start_offset=0, start_anchor=rc.anchor_seed(), end_offset=cut)
    (day / "prefix.bin").unlink()
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_session_entries WHERE line_offset>=?",
                     (cut,))
        conn.execute("DELETE FROM quota_window_snapshots WHERE line_offset>=?",
                     (cut,))
        conn.execute(
            "UPDATE codex_session_files SET last_byte_offset=?, "
            "ingest_complete=0, last_total_tokens=300, content_anchor=?, content_anchor_offset=?",
            (cut, prefix_anchor, cut))
        conn.commit()
    finally:
        conn.close()
    _sync(ns)
    assert _snapshot(ns) == complete
    cursor = _cursor(ns)
    assert cursor["ingest_complete"] == 1
    assert cursor["last_byte_offset"] == len(full)
    with open(day / "x.tmp", "wb") as fh:
        fh.write(full)
    with open(day / "x.tmp", "rb") as fh:
        expected, _ = rc.anchor_over(fh, start_offset=0,
                                     start_anchor=rc.anchor_seed(),
                                     end_offset=len(full))
    (day / "x.tmp").unlink()
    assert bytes(cursor["content_anchor"]) == expected


def test_torn_final_line_stops_at_the_complete_line_end(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "torn")
    logical = day / NAME
    offsets = write_rollout(logical, token_records(3, session_id="s1"))
    with open(logical, "ab") as fh:
        fh.write(b'{"timestamp":"2026-07-20T10:09:00.000Z","type":"event_')
    compress_in_place(logical)
    _sync(ns)
    cursor = _cursor(ns)
    assert cursor.get("container_kind") == rc.ZSTD
    assert cursor["last_byte_offset"] == offsets[-1]
    assert cursor["ingest_complete"] == 1
    assert cursor.get("content_anchor_offset") == offsets[-1]


def test_truncated_frame_changes_nothing_and_is_retried(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "trunc")
    logical = day / NAME
    write_rollout(logical, token_records(3, session_id="s1"))
    data = logical.read_bytes()
    zst = compress_in_place(logical)
    good = zst.read_bytes()
    zst.write_bytes(good[: len(good) // 2])
    stats = _sync(ns)
    assert _snapshot(ns) == {t: [] for t in ACCOUNTING}
    assert getattr(stats, "container_skipped", {}) == {"decode-error": 1}
    zst.write_bytes(good)
    _sync(ns)
    assert _cursor(ns)["last_byte_offset"] == len(data)


def test_a_device_change_sends_an_accepted_container_to_verification(
        tmp_path, monkeypatch):
    """Review P3-2: an accepted compressed container is unchanged only when
    its device AND inode match (5.1, 5.11). A renumbered device is a held
    transition: nothing is re-ingested, reset or deleted on the walk."""
    ns, day = _store(tmp_path, monkeypatch, "device")
    logical = day / NAME
    write_rollout(logical, token_records(3, session_id="s1"))
    compress_in_place(logical)
    _sync(ns)
    before = _snapshot(ns)
    assert len(before["codex_session_entries"]) == 3
    conn = ns["open_cache_db"]()
    try:
        with conn:
            conn.execute("UPDATE codex_session_files SET device_id=device_id+1")
    finally:
        conn.close()
    stats = _sync(ns)
    assert _snapshot(ns) == before
    assert getattr(stats, "container_pending", 0) == 1
    assert _cursor(ns)["container_state"] == "pending"


def test_both_forms_ingest_the_plain_content(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "both")
    logical = day / NAME
    write_rollout(logical, token_records(5, session_id="s1"))
    other = day / "other.jsonl"
    write_rollout(other, token_records(2, session_id="s-other"))
    (day / (NAME + ".zst")).write_bytes(zstd_bytes(other.read_bytes()))
    other.unlink()
    _sync(ns)
    conn = ns["open_cache_db"]()
    try:
        sessions = {r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM codex_session_entries")}
    finally:
        conn.close()
    assert sessions == {"s1"}


def test_alias_roots_ingest_one_compressed_identity_once(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "alias")
    provider = day.parents[3]
    alias = tmp_path / "alias-home"
    alias.symlink_to(provider)
    monkeypatch.setenv("CODEX_HOME", f"{provider},{alias}")
    write_rollout(day / NAME, token_records(3, session_id="s1"))
    compress_in_place(day / NAME)
    _sync(ns)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_path), COUNT(*) "
            "FROM codex_session_entries").fetchone() == (1, 3)
    finally:
        conn.close()


def test_transition_holds_both_cursors_and_their_retained_facts(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "transition")
    logical = day / NAME
    write_rollout(logical, token_records(3, session_id="s1"))
    _sync(ns)
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        conv_before = snapshot_tables(conv, (
            "codex_conversation_events", "codex_conversation_messages"))
        cursor_before = tuple(conv.execute(
            "SELECT size_bytes,last_byte_offset,last_ingested_at,device_id,inode "
            "FROM codex_conversation_source_files").fetchone())
        accounting_before = _snapshot(ns)
        cache_before = _cursor(ns)
        compress_in_place(logical)
        cache_stats = _sync(ns)
        conv_stats = ns["sync_codex_conversations"](conv)
        assert _snapshot(ns) == accounting_before
        assert snapshot_tables(conv, tuple(conv_before)) == conv_before
        assert tuple(conv.execute(
            "SELECT size_bytes,last_byte_offset,last_ingested_at,device_id,inode "
            "FROM codex_conversation_source_files").fetchone()) == cursor_before
        assert _cursor(ns)["last_byte_offset"] == cache_before["last_byte_offset"]
        assert _cursor(ns)["last_ingested_at"] == cache_before["last_ingested_at"]
        assert cache_stats.container_pending == conv_stats.container_pending == 1
        assert not cache_stats.full_walk_complete
        assert conv.execute("SELECT container_state FROM codex_conversation_source_files").fetchone() == ("pending",)
        again = _sync(ns)
        assert again.container_pending == 0
        assert again.container_skipped == {"verification-pending": 1}
    finally:
        conv.close()


def test_unchanged_accepted_compressed_file_never_decodes(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "unchanged")
    logical = day / NAME
    write_rollout(logical, token_records(3, session_id="s1"))
    compress_in_place(logical)
    _sync(ns)
    # Recognition is a separate bounded safety probe, not ingestion.
    monkeypatch.setattr(sys.modules["_cctally_cache"], "_codex_file_is_recognizable", lambda path: True)
    monkeypatch.setattr(rc, "open_decoded", lambda *args, **kwargs: pytest.fail("unchanged ingest decoded"))
    assert _sync(ns).files_skipped_unchanged == 1


def test_unstable_container_after_validation_commits_no_cursor(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "unstable")
    logical = day / NAME
    write_rollout(logical, token_records(3, session_id="s1"))
    zst = compress_in_place(logical)
    original = rc.open_decoded
    calls = 0

    def mutate_on_ingest(container, **kwargs):
        nonlocal calls
        calls += 1
        # recognition, validation, then the actual parse pass.
        if calls == 3:
            import os
            stat = zst.stat()
            os.utime(zst, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        return original(container, **kwargs)

    monkeypatch.setattr(rc, "open_decoded", mutate_on_ingest)
    stats = _sync(ns)
    assert _snapshot(ns) == {t: [] for t in ACCOUNTING}
    assert stats.container_skipped == {"decode-error": 1}
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM codex_session_files").fetchone() == (0,)
    finally:
        conn.close()


def test_interrupted_resume_is_bounded_and_handed_off_after_two_strikes(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "interrupted")
    logical = day / NAME
    offsets = write_rollout(logical, token_records(5, session_id="s1"))
    data = logical.read_bytes()
    compress_in_place(logical)
    _sync(ns)
    cut = offsets[3]
    with io.BytesIO(data[:cut]) as fh:
        anchor, _ = rc.anchor_over(fh, start_offset=0,
                                   start_anchor=rc.anchor_seed(), end_offset=cut)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_session_entries WHERE line_offset>=?", (cut,))
        conn.execute("UPDATE codex_session_files SET last_byte_offset=?,ingest_complete=0,last_total_tokens=200,content_anchor=?,content_anchor_offset=?", (cut, anchor, cut))
        conn.commit()
    finally:
        conn.close()
    before = _snapshot(ns)
    monkeypatch.setattr(sys.modules["_cctally_cache"], "_codex_file_is_recognizable", lambda path: True)

    def interrupt_resume(container, *, should_stop=None):
        assert callable(should_stop), "decoded gap seeks must also carry the budget predicate"
        raise rc.ContainerReadInterrupted()

    monkeypatch.setattr(rc, "open_decoded", interrupt_resume)
    for strike in (1, 2):
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn, budget_seconds=10)
        finally:
            conn.close()
        assert stats.budget_exhausted
        assert stats.backlog_files == 1
        assert stats.backlog_bytes == len(data) - cut
        assert _snapshot(ns) == before
        assert _cursor(ns)["last_byte_offset"] == cut
        if strike == 1:
            assert _cursor(ns)["container_resume_strikes"] == 1
            assert _cursor(ns)["container_state"] == "accepted"
        else:
            assert _cursor(ns)["container_state"] == "pending"
            assert _cursor(ns)["container_state_reason"] == "resume-budget"
            assert stats.drain_requested == ("_codex-replay-drain",)


def test_normally_returning_resume_seek_exhausts_budget_and_hands_off(
        tmp_path, monkeypatch):
    """R01: the last prefix-decode chunk can finish after the deadline.

    No decoder exception is injected: the real seek returns its decoded
    offset, then the iterator's ordinary stop predicate withholds the suffix.
    Two such attempts must hand off without rewriting retained ingest facts.
    """
    ns, day = _store(tmp_path, monkeypatch, "normal-seek-budget")
    logical = day / NAME
    records = token_records(5, session_id="s1")
    records[2]["payload"]["rate_limits"] = {
        "limit_id": "codex", "primary": {"used_percent": 31.0,
        "window_minutes": 10080, "resets_at": 1784966699}}
    offsets = write_rollout(logical, records)
    data = logical.read_bytes()
    compress_in_place(logical)
    _sync(ns)
    cut = offsets[3]
    with io.BytesIO(data[:cut]) as fh:
        anchor, _ = rc.anchor_over(fh, start_offset=0,
            start_anchor=rc.anchor_seed(), end_offset=cut)
    conn = ns["open_cache_db"]()
    try:
        with conn:
            conn.execute("DELETE FROM codex_session_entries WHERE line_offset>=?",
                         (cut,))
            conn.execute(
                "UPDATE codex_session_files SET last_byte_offset=?, "
                "ingest_complete=0,last_total_tokens=200,content_anchor=?, "
                "content_anchor_offset=?", (cut, anchor, cut))
        facts = snapshot_tables(conn, ACCOUNTING + (
            "codex_conversation_threads", "codex_file_accounts",
            "codex_file_incarnations"))
    finally:
        conn.close()
    assert facts["codex_session_entries"] and facts["quota_window_snapshots"]
    cursor = _cursor(ns)
    debt_fields = {"container_resume_strikes", "container_state",
                   "container_state_reason", "pending_container_fingerprint"}
    retained_cursor = {k: v for k, v in cursor.items() if k not in debt_fields}
    journal_dir = ns["_cctally_core"].JOURNAL_DIR

    def journal_bytes():
        return {p.name: p.read_bytes() for p in journal_dir.glob("*.jsonl")}

    journal_before = journal_bytes()
    clock = {"now": 0.0}
    returned_seeks = []
    original_seek = rc.DecodedZstdStream.seek
    original_readline = rc.DecodedZstdStream.readline

    def seek_past_deadline(stream, offset, whence=0):
        prefix_resume = offset == cut and whence == 0 and stream.tell() == 0
        result = original_seek(stream, offset, whence)
        if prefix_resume:
            assert result == cut
            returned_seeks.append(result)
            clock["now"] = 11.0
        return result

    def no_suffix_read(stream, *args, **kwargs):
        assert not (clock["now"] >= 10.0 and stream.tell() >= cut), (
            "the expired budget must stop before reading a suffix line")
        return original_readline(stream, *args, **kwargs)

    monkeypatch.setattr(sys.modules["_cctally_cache"], "_walk_clock",
                        lambda: clock["now"])
    monkeypatch.setattr(sys.modules["_cctally_cache"],
                        "_codex_file_is_recognizable", lambda path: True)
    monkeypatch.setattr(rc.DecodedZstdStream, "seek", seek_past_deadline)
    monkeypatch.setattr(rc.DecodedZstdStream, "readline", no_suffix_read)
    for attempt in (1, 2):
        clock["now"] = 0.0
        committed = []
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](
                conn, budget_seconds=10, quota_reconcile="defer",
                _on_file_committed=committed.append)
            assert returned_seeks == [cut] * attempt
            assert stats.budget_exhausted and stats.backlog_files == 1
            assert stats.backlog_bytes == len(data) - cut
            assert _cursor(ns)["container_resume_strikes"] == 1
            assert snapshot_tables(conn, tuple(facts)) == facts
        finally:
            conn.close()
        after = _cursor(ns)
        assert {k: v for k, v in after.items() if k not in debt_fields} == retained_cursor
        assert not committed and stats.files_processed == stats.rows_changed == 0
        assert journal_bytes() == journal_before
        if attempt == 1:
            assert after["container_state"] == "accepted"
            assert stats.drain_requested == ()
        else:
            assert after["container_state"] == "pending"
            assert after["container_state_reason"] == "resume-budget"
            assert stats.drain_requested == ("_codex-replay-drain",)


def test_targeted_reverse_transition_is_held_before_plain_shrink_preflight(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "reverse")
    logical = day / NAME
    write_rollout(logical, token_records(5, session_id="old"))
    compress_in_place(logical)
    _sync(ns)
    before = _snapshot(ns)
    write_rollout(logical, token_records(1, session_id="new"))
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, only_paths={str(logical)})
    finally:
        conn.close()
    assert stats.deferred_reason is None
    assert stats.container_pending == 1
    assert _snapshot(ns) == before
    assert _cursor(ns)["container_state"] == "pending"


@pytest.mark.parametrize("compressed", [False, True])
def test_anchor_tracks_every_complete_line_and_terminal_parser_state(tmp_path, monkeypatch, compressed):
    ns, day = _store(tmp_path, monkeypatch, "all-lines")
    logical = day / NAME
    write_rollout(logical, token_records(2, session_id="s1"))
    with logical.open("ab") as fh:
        fh.write(b'not-json\n\n{"type":"turn_context","payload":{"model":"gpt-5-mini"}}\n')
    data = logical.read_bytes()
    if compressed:
        compress_in_place(logical)
    _sync(ns)
    cursor = _cursor(ns)
    with io.BytesIO(data) as fh:
        expected, complete = rc.anchor_over(fh, start_offset=0,
            start_anchor=rc.anchor_seed(), end_offset=len(data))
    assert cursor["last_byte_offset"] == cursor["content_anchor_offset"] == complete == len(data)
    assert bytes(cursor["content_anchor"]) == expected
    assert cursor["last_model"] == "gpt-5-mini"
    assert cursor["last_total_tokens"] == 200


def test_held_transition_keeps_the_final_changed_file_in_its_w11_transaction(tmp_path, monkeypatch):
    ns, day = _store(tmp_path, monkeypatch, "mixed-w11")
    logical = day / NAME
    write_rollout(logical, token_records(2, session_id="old"))
    _sync(ns)
    compress_in_place(logical)
    new_plain = day / NAME.replace("10-00-00", "12-00-00")
    write_rollout(new_plain, token_records(3, session_id="new"))
    committed = []
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn, _on_file_committed=committed.append)
        assert stats.files_failed == 0
        assert stats.container_pending == 1
        assert stats.files_processed == 1
        assert stats.rows_changed == 3
        assert committed == [str(new_plain)]
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?",
            (str(new_plain),)).fetchone() == (3,)
        assert conn.execute(
            "SELECT container_state FROM codex_session_files WHERE path=?",
            (str(logical),)).fetchone() == ("pending",)
    finally:
        conn.close()
