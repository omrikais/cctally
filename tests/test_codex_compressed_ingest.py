"""#968 T2 + T7 ingest edges: compressed rollouts read in decoded space."""
from __future__ import annotations

import io
import json
import os
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


def test_archive_only_rollout_is_counted_once(tmp_path, monkeypatch):
    """#970 R2 (AC1/AC4): a rollout Codex already archived when cctally first
    looks was never discovered, so its usage, cost and quota history were
    silently missing. It must ingest exactly once, under its dated logical
    identity, and never under an archive-spelled path."""
    import _lib_jsonl
    ns, day = _store(tmp_path, monkeypatch, "archive")
    provider = day.parents[3]
    archived = provider / "archived_sessions" / NAME
    records = token_records(5, session_id="s-arch")
    records[-1]["payload"]["rate_limits"] = {
        "limit_id": "codex", "primary": {"used_percent": 12.0,
        "window_minutes": 10080, "resets_at": 1784966699}}
    write_rollout(archived, records)
    with open(archived, "rb") as fh:
        parsed = list(_lib_jsonl._iter_codex_jsonl_entries_with_offsets(
            fh, str(archived)))
    assert len(parsed) == 5            # the fixture really carries usage
    compress_in_place(archived)
    _sync(ns)
    logical = day / NAME
    conn = ns["open_cache_db"]()
    try:
        by_path = dict(conn.execute(
            "SELECT source_path, COUNT(*) FROM codex_session_entries "
            "GROUP BY source_path").fetchall())
        quota = dict(conn.execute(
            "SELECT source_path, COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex' GROUP BY source_path").fetchall())
        cursors = [r[0] for r in conn.execute(
            "SELECT path FROM codex_session_files")]
    finally:
        conn.close()
    # 16b055cae globs only `sessions/`, so nothing is ingested: 0 rows.
    assert by_path == {str(logical): len(parsed)}
    assert set(quota) == {str(logical)} and quota[str(logical)] >= 1
    assert cursors == [str(logical)]


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


# ── #970 T6: P2 — an archive first sight guesses no account ─────────────────

from test_codex_bounded_ingest import _auth_json  # noqa: E402

ARCH_UUID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0970"
ARCH_NAME = f"rollout-2026-07-19T10-00-00-{ARCH_UUID}.jsonl"
FRESH_NAME = "rollout-2026-07-20T12-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0971.jsonl"
ACCOUNTS = (("acct-a", "a@example.com"), ("acct-b", "b@example.com"))
#: An account-level weekly window, [2026-07-20T11:00Z, 2026-07-27T11:00Z).
WEEKLY = {"limit_id": "codex", "limit_name": "codex",
          "primary": {"used_percent": 30.0, "window_minutes": 10080,
                      "resets_at": 1785150000}}


def _p2_env(tmp_path, monkeypatch, login):
    ns, day = _store(tmp_path, monkeypatch, "p2")
    provider = day.parents[3]
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    (provider / "auth.json").write_text(_auth_json(*login))
    return ns, provider, day


def _login(provider, login):
    (provider / "auth.json").write_text(_auth_json(*login))


def _file_account_ops(ns) -> list:
    import json
    out = []
    for segment in sorted(ns["_cctally_core"].JOURNAL_DIR.glob("*.jsonl")):
        for line in segment.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if (record.get("payload") or {}).get("kind") == "codex_file_account":
                    out.append(record)
    return out


def _accounts(ns, path) -> dict:
    """``{line_offset: account_key}`` of one identity's accounting rows."""
    conn = ns["open_cache_db"]()
    try:
        return dict(conn.execute(
            "SELECT line_offset, account_key FROM codex_session_entries "
            "WHERE source_path=?", (str(path),)).fetchall())
    finally:
        conn.close()


def _archive_only(provider, records, *, compressed=True):
    archived = provider / "archived_sessions" / ARCH_NAME
    write_rollout(archived, records)
    if compressed:
        archived = compress_in_place(archived)
    return archived, provider / "sessions" / "2026" / "07" / "19" / ARCH_NAME


def _with_rate_limits(records):
    for record in records:
        payload = record.get("payload") or {}
        if payload.get("type") == "token_count":
            payload["info"]["rate_limits"] = WEEKLY
    return records


@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
def test_archive_first_sight_journals_no_decision(tmp_path, monkeypatch, order):
    """P2 (A6): usage cctally first reads in the archive was credited to
    whoever happened to be logged in — a durable, journaled misattribution
    that every rebuild replays. With two real accounts in either order, the
    archive first sight must journal no `codex_file_account` op and stay
    unattributed, while a sessions first sight still takes the live login."""
    first, second = ACCOUNTS if order == "a-then-b" else ACCOUNTS[::-1]
    ns, provider, day = _p2_env(tmp_path, monkeypatch, first)
    seeded = day / NAME
    write_rollout(seeded, token_records(3, session_id="seeded"))
    _sync(ns)
    seeded_accounts = set(_accounts(ns, seeded).values())
    assert len(seeded_accounts) == 1 and None not in seeded_accounts
    ops_before = len(_file_account_ops(ns))
    assert ops_before >= 1
    _login(provider, second)
    _archived, logical = _archive_only(
        provider, token_records(4, session_id="archived", start_minute=30))
    _sync(ns)
    assert len(_accounts(ns, logical)) == 4
    # BASE: the live login of the second account was decided and journaled.
    assert len(_file_account_ops(ns)) == ops_before
    assert set(_accounts(ns, logical).values()) == {None}
    fresh = day / FRESH_NAME
    write_rollout(fresh, token_records(2, session_id="fresh", start_minute=40))
    _sync(ns)
    fresh_accounts = set(_accounts(ns, fresh).values())
    assert len(fresh_accounts) == 1 and None not in fresh_accounts
    assert fresh_accounts != seeded_accounts       # two real accounts
    assert len(_file_account_ops(ns)) == ops_before + 1


def test_window_adoption_stamps_archived_spend(tmp_path, monkeypatch):
    """P2 leaves an archive first sight to the existing window adoption: when
    one account's weekly quota window claims its timestamps, its spend must
    be credited to that account (not left unattributed, not given to the
    current login) without any per-file decision."""
    ns, provider, day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[0])
    seeded = day / NAME
    write_rollout(seeded, _with_rate_limits(
        token_records(3, session_id="seeded", start_minute=70)))
    _sync(ns)
    claimed = set(_accounts(ns, seeded).values())
    assert len(claimed) == 1 and None not in claimed
    ops_before = len(_file_account_ops(ns))
    _login(provider, ACCOUNTS[1])
    _archived, logical = _archive_only(
        provider, token_records(4, session_id="archived", start_minute=90))
    _sync(ns)
    # BASE: the current login (B) was decided; P2 + adoption gives A.
    assert set(_accounts(ns, logical).values()) == claimed
    assert len(_file_account_ops(ns)) == ops_before


def test_delta_after_unarchive_takes_live_login_for_new_bytes_only(
        tmp_path, monkeypatch):
    """A resumed thread after an unarchive appends genuinely new bytes: those
    take the live login from the first offset beyond the cursor, while the
    archived history read first in the archive stays unattributed — never
    re-decided wholesale for the login of the day."""
    ns, provider, day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[1])
    archived, logical = _archive_only(
        provider, token_records(3, session_id="resumed"), compressed=False)
    _sync(ns)
    old_size = archived.stat().st_size
    assert set(_accounts(ns, logical).values()) == {None}
    ops_before = len(_file_account_ops(ns))
    logical.parent.mkdir(parents=True)
    os.rename(archived, logical)
    _sync(ns)                     # the unarchive: a location-only adoption
    assert set(_accounts(ns, logical).values()) == {None}
    assert len(_file_account_ops(ns)) == ops_before
    grown = token_records(5, session_id="resumed")
    with open(logical, "ab") as fh:
        for record in grown[len(token_records(3, session_id="resumed")):]:
            fh.write(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    _sync(ns)
    accounts = _accounts(ns, logical)
    assert len(accounts) == 5
    old = {k for off, k in accounts.items() if off < old_size}
    new = {k for off, k in accounts.items() if off >= old_size}
    # BASE: the first sight already took the live login for every byte.
    assert old == {None}
    assert len(new) == 1 and None not in new
    ops = _file_account_ops(ns)
    assert len(ops) == ops_before + 1


def test_rebuild_with_archived_identities_mints_no_decision(tmp_path, monkeypatch):
    """`cache-sync --source codex --rebuild` re-reads archived history; it
    must never mint a live-login decision for it (A6), or one rebuild would
    durably credit months of archived usage to the current login."""
    import argparse
    ns, provider, day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[0])
    seeded = day / NAME
    write_rollout(seeded, token_records(3, session_id="seeded"))
    _archived, logical = _archive_only(
        provider, token_records(4, session_id="archived", start_minute=30))
    _sync(ns)
    ops_before = len(_file_account_ops(ns))
    _login(provider, ACCOUNTS[1])
    cc = sys.modules["_cctally_cache"]
    assert cc.cmd_cache_sync(argparse.Namespace(
        source="codex", rebuild=True, prune_orphans=False,
        prune_conversations=False)) == 0
    assert len(_accounts(ns, logical)) == 4
    assert set(_accounts(ns, logical).values()) == {None}
    # BASE: the first sight journaled a decision before the rebuild.
    assert len(_file_account_ops(ns)) == ops_before == 1


def _quota_rows(ns, path) -> list:
    """One identity's quota observations, by their physical identity."""
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT line_offset, captured_at_utc, observed_slot, "
            "logical_limit_key, window_minutes, used_percent, resets_at_utc "
            "FROM quota_window_snapshots WHERE source='codex' "
            "AND source_path=? ORDER BY line_offset", (str(path),)).fetchall()
    finally:
        conn.close()


def _quota_obs_ops(ns, path) -> int:
    """Journal quota observation ops keyed by ``path`` (the cutover's
    bootstrap segment, which copies the first cache rows, excluded)."""
    count = 0
    for segment in sorted(ns["_cctally_core"].JOURNAL_DIR.glob("*.jsonl")):
        if segment.name.startswith("bootstrap-"):
            continue
        for line in segment.read_text().splitlines():
            if line.strip():
                payload = json.loads(line).get("payload") or {}
                if (payload.get("kind") == "quota_window_snapshot"
                        and payload.get("source_path") == str(path)):
                    count += 1
    return count


def test_a_rewritten_archive_adds_no_duplicate_of_a_retained_quota_reading(
        tmp_path, monkeypatch):
    """#970 F1 (AC5 A1): durable duplicate quota observations. A rollout read
    from `sessions/` and later pruned keeps its quota observations, which the
    journal retains. Codex rewrites the rollout when it archives it, so every
    line offset drifts, and the first sight of the archive re-emitted every
    retained reading under a new offset. The cache key and the journal natural
    key both carry the offset, so neither caught it: 15 semantic duplicates
    landed in cache.db and the journal on the operator's store, and the
    journal replays them on every rebuild. Only the genuinely new reading may
    be added; the accounting rows are the archived file's own entries."""
    ns, provider, day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[0])
    cc = sys.modules["_cctally_cache"]
    logical = provider / "sessions" / "2026" / "07" / "19" / ARCH_NAME
    original_ends = write_rollout(
        logical, _with_rate_limits(token_records(3, session_id="drifted")))
    keeper = day / NAME          # keeps the root recognizable once it goes
    write_rollout(keeper, token_records(1, session_id="keeper", start_minute=50))
    _sync(ns)
    assert len(_quota_rows(ns, logical)) == 3
    ops_before = _quota_obs_ops(ns, logical)
    assert ops_before == 3
    logical.unlink()
    _sync(ns)
    cc.run_codex_prune_drain(wait_for_requests=False)
    assert _accounts(ns, logical) == {}
    # The journal re-materializes the pruned readings: they are retained.
    sys.modules["_cctally_journal"].recover_quota_cache_from_journal(quiet=True)
    retained = _quota_rows(ns, logical)
    assert len(retained) == 3
    # The archive rewrite: the same records with one more field each, so
    # every offset drifts, and one genuinely new reading at the end.
    rewritten = _with_rate_limits(token_records(4, session_id="drifted"))
    for record in rewritten:
        record["archived"] = True
    archived = provider / "archived_sessions" / ARCH_NAME
    archived_ends = write_rollout(archived, rewritten)
    compress_in_place(archived)
    starts = [0] + archived_ends[:-1]
    token_starts = starts[2:]
    assert token_starts[:3] != ([0] + original_ends[:-1])[2:]   # drifted
    for _ in range(2):
        _sync(ns)
        cc.run_codex_history_provenance_drain(wait_for_requests=False)
    _sync(ns)
    after = _quota_rows(ns, logical)
    # BASE: the three retained readings came back under drifted offsets.
    assert len(after) == 4, after
    assert set(retained) <= set(after)
    (new,) = set(after) - set(retained)
    assert new[0] == token_starts[3]
    assert _quota_obs_ops(ns, logical) == ops_before + 1
    assert sorted(_accounts(ns, logical)) == token_starts


def _entry_rows(ns, path) -> list:
    """One identity's accounting rows, ids and attribution included."""
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT id, line_offset, timestamp_utc, total_tokens, account_key "
            "FROM codex_session_entries WHERE source_path=? ORDER BY line_offset",
            (str(path),)).fetchall()
    finally:
        conn.close()


def test_a_rewritten_archive_adds_no_duplicate_of_a_retained_accounting_entry(
        tmp_path, monkeypatch):
    """#970 N1: accounting double count. Accounting rows are keyed by
    (source_path, line_offset) only. A cursorless identity (its cursor row
    gone, its accounting entries retained) whose rollout Codex rewrote when
    it archived it is read from byte zero, every offset has drifted, and
    every retained entry was inserted a second time: the rollout's usage and
    cost counted twice. Only the genuinely new entry may be added, the
    retained rows keep their ids and attribution, and a later delta append
    to the now cursor-bearing identity never pays the retained-entry load."""
    ns, provider, _day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[0])
    logical = provider / "sessions" / "2026" / "07" / "19" / ARCH_NAME
    write_rollout(logical, token_records(3, session_id="drifted"))
    keeper = provider / "sessions" / "2026" / "07" / "20" / NAME
    write_rollout(keeper, token_records(1, session_id="keeper", start_minute=50))
    _sync(ns)
    retained = _entry_rows(ns, logical)
    assert len(retained) == 3 and None not in {r[4] for r in retained}
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("DELETE FROM codex_session_files WHERE path=?",
                            (str(logical),)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    logical.unlink()
    ops_before = len(_file_account_ops(ns))
    # The archive rewrite: one more field per line (every offset drifts) and
    # one genuinely new entry at the end.
    rewritten = token_records(4, session_id="drifted")
    for record in rewritten:
        record["archived"] = True
    archived = provider / "archived_sessions" / ARCH_NAME
    archived_ends = write_rollout(archived, rewritten)
    token_starts = ([0] + archived_ends[:-1])[2:]
    assert not {r[1] for r in retained} & set(token_starts)   # drifted
    cc = sys.modules["_cctally_cache"]
    for _ in range(2):
        _sync(ns)
        cc.run_codex_history_provenance_drain(wait_for_requests=False)
    _sync(ns)
    after = _entry_rows(ns, logical)
    # BASE: 7 rows, the three retained entries again at drifted offsets.
    assert len(after) == 4, after
    assert set(retained) <= set(after)
    (new,) = set(after) - set(retained)
    assert new[1] == token_starts[3]
    assert sum(r[3] for r in after) == 400
    assert len(_file_account_ops(ns)) == ops_before   # nothing journaled
    # A cursor-bearing delta append reads only its new bytes, never the load.
    loads = []
    real_open = ns["open_cache_db"]

    def opened():
        conn = real_open()
        conn.set_trace_callback(lambda sql: loads.append(sql) if (
            " ".join(sql.split()).lower().startswith("select timestamp_utc")
            and "from codex_session_entries" in sql.lower()) else None)
        return conn

    monkeypatch.setitem(ns, "open_cache_db", opened)
    monkeypatch.setattr(cc, "open_cache_db", opened)
    grown = token_records(5, session_id="drifted")
    grown[-1]["archived"] = True
    with open(archived, "ab") as fh:
        fh.write(json.dumps(grown[-1], separators=(",", ":")).encode() + b"\n")
    _sync(ns)
    assert loads == []
    assert len(_entry_rows(ns, logical)) == 5


def test_a_rewritten_archive_cut_short_on_its_first_read_adds_no_duplicate(
        tmp_path, monkeypatch):
    """#970 V1: the twin of F1 and N1 across a budget stop. A cursorless
    identity (cursor row gone, quota readings and accounting entries
    retained) whose rollout Codex rewrote when it archived it is read from
    byte zero, and the walk deadline stops that first read after one entry.
    The stop writes a cursor, so the continuation is cursor-bearing, and the
    retained-row filters covered only the byte-zero read: every retained
    reading and entry later in the file came back at its drifted offset, a
    duplicate quota observation journaled for good and the rollout's usage
    counted twice. Only the genuinely new reading and entry may be added,
    the retained rows keep their ids and attribution, and nothing is
    journaled as a file account decision."""
    ns, provider, _day = _p2_env(tmp_path, monkeypatch, ACCOUNTS[0])
    cc = sys.modules["_cctally_cache"]
    logical = provider / "sessions" / "2026" / "07" / "19" / ARCH_NAME
    write_rollout(logical, _with_rate_limits(token_records(3, session_id="drifted")))
    keeper = provider / "sessions" / "2026" / "07" / "20" / NAME
    write_rollout(keeper, token_records(1, session_id="keeper", start_minute=50))
    _sync(ns)
    retained_entries = _entry_rows(ns, logical)
    retained_quota = _quota_rows(ns, logical)
    assert len(retained_entries) == 3 and None not in {r[4] for r in retained_entries}
    assert len(retained_quota) == 3
    quota_ops_before = _quota_obs_ops(ns, logical)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("DELETE FROM codex_session_files WHERE path=?",
                            (str(logical),)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    logical.unlink()
    ops_before = len(_file_account_ops(ns))
    rewritten = _with_rate_limits(token_records(4, session_id="drifted"))
    for record in rewritten:
        record["archived"] = True
    archived = provider / "archived_sessions" / ARCH_NAME
    archived_ends = write_rollout(archived, rewritten)
    token_starts = ([0] + archived_ends[:-1])[2:]
    assert not {r[1] for r in retained_entries} & set(token_starts)   # drifted

    # The first read of the archive stops on the walk deadline right after
    # its first accounting entry; later reads run to the end.
    real_iter = cc._iter_codex_fused_records_with_offsets
    cut = {"armed": True, "walking": False, "now": 0.0, "fired": False}

    def iterate(fh, path, *args, **kwargs):
        for emission in real_iter(fh, path, *args, **kwargs):
            yield emission
            if (cut["armed"] and cut["walking"] and path == str(logical)
                    and emission.accounting is not None):
                cut.update(armed=False, now=10_000.0, fired=True)

    monkeypatch.setattr(cc, "_iter_codex_fused_records_with_offsets", iterate)
    monkeypatch.setattr(cc, "_walk_clock", lambda: cut["now"])
    partial_seen = []
    entry_loads = []      # per walk: retained-entry range loads

    def budgeted_sync():
        fired_before = cut["fired"]
        cut["walking"] = True
        conn = ns["open_cache_db"]()
        loads = []
        conn.set_trace_callback(lambda sql: loads.append(sql) if (
            " ".join(sql.split()).lower().startswith("select timestamp_utc")
            and "from codex_session_entries" in sql.lower()) else None)
        try:
            ns["sync_codex_cache"](conn, budget_seconds=60.0)
            entry_loads.append(len(loads))
            if cut["fired"] and not fired_before:
                partial_seen.append(conn.execute(
                    "SELECT ingest_complete, last_byte_offset "
                    "FROM codex_session_files WHERE path=?",
                    (str(logical),)).fetchone())
        finally:
            conn.close()
            cut.update(walking=False, now=0.0)

    for _ in range(2):
        budgeted_sync()
        cc.run_codex_history_provenance_drain(wait_for_requests=False)
    budgeted_sync()
    _sync(ns)
    # The fixture really stopped the cursorless first read mid-file.
    assert len(partial_seen) == 1, partial_seen
    complete, reached = partial_seen[0]
    assert complete == 0 and 0 < reached < archived_ends[-1], partial_seen
    # Every walk that reads the identity (its first read, then each attempt
    # at the continuation) loads the retained entries once, never once per
    # emitted entry.
    assert max(entry_loads) == 1, entry_loads
    after_entries = _entry_rows(ns, logical)
    after_quota = _quota_rows(ns, logical)
    # BASE: the continuation re-inserted the retained entries and readings
    # beyond the stop at their drifted offsets.
    assert len(after_entries) == 4, after_entries
    assert set(retained_entries) <= set(after_entries)
    (new_entry,) = set(after_entries) - set(retained_entries)
    assert new_entry[1] == token_starts[3]
    assert sum(r[3] for r in after_entries) == 400
    assert len(after_quota) == 4, after_quota
    assert set(retained_quota) <= set(after_quota)
    (new_reading,) = set(after_quota) - set(retained_quota)
    assert new_reading[0] == token_starts[3]
    assert _quota_obs_ops(ns, logical) == quota_ops_before + 1
    assert len(_file_account_ops(ns)) == ops_before   # nothing journaled
