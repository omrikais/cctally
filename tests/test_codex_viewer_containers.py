"""#968 spec 5.10: the viewer's full-content re-read through containers."""
from __future__ import annotations

import builtins
import errno
import json
import os
import pathlib
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
    compress_in_place, conversation_records, write_rollout, zstd_bytes)

import _lib_codex_rollout_container as rc  # noqa: E402

SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0501"
NAME = f"rollout-2026-07-20T10-05-01-{SID}.jsonl"
DEEP_TEXT = "deep output " * 50


def _deep_rollout():
    records = conversation_records(4000, session_id=SID, label="d")
    ts = "2026-07-21T00:00:00.000Z"
    records.append({"timestamp": ts, "type": "response_item", "payload": {
        "type": "function_call", "name": "fixture_function",
        "arguments": json.dumps({"path": "deep.txt"}), "call_id": "call-deep"}})
    records.append({"timestamp": ts, "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "call-deep",
        "output": {"ok": True, "text": DEEP_TEXT}}})
    return records


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(
        ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    path = day / NAME
    offsets = write_rollout(path, _deep_rollout())
    assert offsets[-3] > 2 * rc.DECODE_CHUNK      # the call is deep in the stream
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()
    import _lib_codex_conversation_query as query
    conv = ns["open_conversations_db"]()
    try:
        key = conv.execute(
            "SELECT conversation_key FROM codex_conversation_rollups").fetchone()[0]
        rows = query._load_conversation_rows(conv, key)
        call_row = next(r for r in rows if r.kind == "tool_call")
        block = query._block_key_for_row(call_row)
        baseline = {which: query.read_codex_payload(conv, key, block, which)
                    for which in ("call", "output")}
    finally:
        conv.close()
    assert all(v["status"] == "ok" for v in baseline.values()), baseline
    return ns, query, path, key, block, baseline, tmp_path


def _read(ns, query, key, block, which):
    conv = ns["open_conversations_db"]()
    try:
        return query.read_codex_payload(conv, key, block, which)
    finally:
        conv.close()


def _read_or_error(ns, query, key, block, which):
    """``(result, None)`` or ``(None, exception)``: the error IS the outcome
    under test, and on 92e06df85 the read answers instead of raising."""
    try:
        return _read(ns, query, key, block, which), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def _assert_operational(result, error, category):
    # 92e06df85 RED: the read answers `{"status": "gone"}` instead.
    assert result is None, f"answered {result!r} instead of an operational error"
    assert type(error).__name__ == "CodexContainerReadError", repr(error)
    assert error.category == category


def test_a_deep_block_reads_identically_from_the_compressed_container(viewer):
    ns, query, path, key, block, baseline, _tmp = viewer
    compress_in_place(path)
    for which in ("call", "output"):
        assert _read(ns, query, key, block, which) == baseline[which]


def test_no_decoder_is_an_operational_error_not_gone(viewer, monkeypatch):
    ns, query, path, key, block, _baseline, _tmp = viewer
    compress_in_place(path)
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    _assert_operational(*_read_or_error(ns, query, key, block, "output"),
                        "decoder-unavailable")


def test_a_corrupt_stream_is_an_operational_error_not_gone(viewer):
    ns, query, path, key, block, _baseline, _tmp = viewer
    twin = compress_in_place(path)
    blob = twin.read_bytes()
    twin.write_bytes(blob[: len(blob) // 2])
    _assert_operational(*_read_or_error(ns, query, key, block, "output"),
                        "decode-error")


def test_a_plain_read_error_is_an_operational_error_not_gone(viewer, monkeypatch):
    """Spec 5.10 (review S-P3 item 9): a read failure of a PRESENT plain
    rollout (EIO here; EACCES alike) is operational, never `gone`."""
    ns, query, path, key, block, _baseline, _tmp = viewer
    real_open = builtins.open
    target = os.fspath(path)

    def failing_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) == target:
            raise OSError(errno.EIO, os.strerror(errno.EIO), target)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    _assert_operational(*_read_or_error(ns, query, key, block, "output"),
                        "read-error")


def test_a_vanished_container_is_still_gone(viewer):
    ns, query, path, key, block, _baseline, _tmp = viewer
    compress_in_place(path).unlink()
    assert _read(ns, query, key, block, "output")["status"] == "gone"


def test_a_missing_rollout_under_an_unknown_root_stays_not_found(viewer):
    """HEAD's 404 is kept (review S-P3 item 10): the LOGICAL path's
    containment is checked before a container is selected, so a missing
    rollout whose root is unknown answers `not_found`, never `gone`."""
    ns, query, path, key, block, _baseline, _tmp = viewer
    path.unlink()
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_source_roots")
        conn.commit()
    finally:
        conn.close()
    assert _read(ns, query, key, block, "output")["status"] == "not_found"


def test_a_container_escaping_its_root_is_never_read(viewer, monkeypatch):
    ns, query, path, key, block, _baseline, tmp_path = viewer
    twin = compress_in_place(path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / twin.name
    os.replace(twin, escaped)
    os.symlink(escaped, twin)

    def no_read(*_args, **_kwargs):
        raise AssertionError("a read was attempted outside the root")

    monkeypatch.setattr(rc, "open_decoded", no_read)
    assert _read(ns, query, key, block, "output")["status"] == "not_found"


@pytest.mark.parametrize("compressed", [False, True])
def test_stat_access_failure_is_operational_even_before_selection(
        viewer, monkeypatch, compressed):
    ns, query, path, key, block, _baseline, _tmp = viewer
    target = compress_in_place(path) if compressed else path
    real_stat = os.stat

    def failing_stat(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) == str(target):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
        return real_stat(file, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    _assert_operational(*_read_or_error(ns, query, key, block, "output"),
                        "read-error")


def test_plain_wins_over_a_corrupt_compressed_twin(viewer):
    ns, query, path, key, block, baseline, _tmp = viewer
    path.with_name(path.name + ".zst").write_bytes(b"broken")
    assert _read(ns, query, key, block, "output") == baseline["output"]


def test_compressed_record_mutation_keeps_the_structural_gone_check(viewer):
    ns, query, path, key, block, _baseline, _tmp = viewer
    data = path.read_bytes().replace(b"deep output", b"fake output")
    path.with_name(path.name + ".zst").write_bytes(zstd_bytes(data))
    path.unlink()
    assert _read(ns, query, key, block, "output")["status"] == "gone"


def test_a_request_reuses_one_decoded_stream_for_multiple_rows(viewer, monkeypatch):
    ns, query, path, key, block, _baseline, _tmp = viewer
    compress_in_place(path)
    real_open = rc.open_decoded
    opened = []

    def tracked_open(container, **kwargs):
        stream = real_open(container, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(rc, "open_decoded", tracked_open)
    conv = ns["open_conversations_db"]()
    streams = {}
    try:
        rows = query._locate_payload_block(conv, key, block)
        for which in ("call", "output", "call"):
            assert query._reread_codex_full_content(
                conv, rows[which], streams=streams) is not None
        assert len(opened) == 1
        assert len(streams) == 1
        # Keep the leaf's bounded-buffer contract while seeking back and forth.
        assert len(opened[0]._buf) <= rc.DECODE_CHUNK
    finally:
        for stream in streams.values():
            stream.close()
        conv.close()


@pytest.mark.parametrize("broken", [False, True])
def test_request_streams_close_on_success_and_decode_failure(viewer, monkeypatch, broken):
    ns, query, path, key, block, baseline, _tmp = viewer
    twin = compress_in_place(path)
    if broken:
        data = twin.read_bytes()
        twin.write_bytes(data[:len(data) // 2])
    real_open = rc.open_decoded
    opened = []

    def tracked_open(container, **kwargs):
        stream = real_open(container, **kwargs)
        opened.append(stream)
        return stream

    monkeypatch.setattr(rc, "open_decoded", tracked_open)
    result, error = _read_or_error(ns, query, key, block, "output")
    if broken:
        _assert_operational(result, error, "decode-error")
    else:
        assert result == baseline["output"] and error is None
    assert len(opened) == 1
    assert opened[0]._closed


def test_compression_between_selection_and_open_reselects_the_container(viewer, monkeypatch):
    ns, query, path, key, block, baseline, _tmp = viewer
    real_open = builtins.open
    first = True

    def compress_before_open(file, *args, **kwargs):
        nonlocal first
        if first and isinstance(file, (str, os.PathLike)) and os.fspath(file) == str(path):
            first = False
            compress_in_place(path)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", compress_before_open)
    assert _read(ns, query, key, block, "output") == baseline["output"]


def test_no_decoder_reaches_the_existing_dashboard_500_envelope(viewer, monkeypatch):
    ns, query, path, key, block, _baseline, _tmp = viewer
    compress_in_place(path)
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    import _cctally_dashboard_conversation as dashboard_query
    conn = ns["open_conversations_db"]()
    monkeypatch.setattr(dashboard_query, "_open_conversation_reader", lambda _label: conn)

    class Handler:
        path = "/api/conversation/payload"
        responses = []

        def log_error(self, *_args):
            pass

        def _respond_json(self, status, body):
            self.responses.append((status, body))

    handler = Handler()
    assert dashboard_query._run_conversation_query_impl(
        handler, lambda db: query.read_codex_payload(db, key, block, "output"),
        "payload") == (False, None)
    assert handler.responses == [(500, {"error":
        "CodexContainerReadError: Codex rollout could not be read (decoder-unavailable)"})]
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")
