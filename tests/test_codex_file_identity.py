"""Per-file ``(device_id, inode)`` stamped with the Codex cursor (#769 S6, #716 A).

``codex_session_files`` and ``codex_conversation_source_files`` record a
size, an mtime and a byte offset, but no file identity.  A replacement that
lands at the same size and the same mtime therefore leaves the retained cursor
looking like a cursor into the NEW file, and the frontier has no evidence the
inode changed at all.  The Claude conversation cursor already stores device,
inode and prefix identity beside its source cursor; these two Codex tables are
the gap.

Identity is captured from the same pre-read stat that defines the scan target,
and it commits in the SAME transaction as the scan target, the final offset and
the derived rows.  That ordering is what makes a replacement unable to leave a
new ``(device, inode)`` paired with an old offset.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402


def _records(count: int, *, session_id: str = "sess-identity", start: int = 0):
    out = [
        {"timestamp": "2026-07-20T10:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id, "thread_source": "user"}},
        {"timestamp": "2026-07-20T10:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5"}},
    ]
    cumulative = start * 100
    for index in range(count):
        cumulative += 100
        out.append({
            "timestamp": f"2026-07-20T10:{start + index + 2:02d}:00.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": 60, "cached_input_tokens": 0,
                    "output_tokens": 40, "reasoning_output_tokens": 0,
                    "total_tokens": 100},
                "total_token_usage": {"total_tokens": cumulative}}},
        })
    return out


def _write_rollout(path: pathlib.Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")


@pytest.fixture
def root(tmp_path, monkeypatch):
    """One Codex provider root holding one rollout."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    provider_root.mkdir(parents=True, exist_ok=True)
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def _accounting_row(ns) -> dict:
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM codex_session_files").fetchone()
        return {} if row is None else dict(row)
    finally:
        conn.close()


def _conversation_row(ns) -> dict:
    conn = ns["open_conversations_db"]()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM codex_conversation_source_files").fetchone()
        return {} if row is None else dict(row)
    finally:
        conn.close()


def _sync_accounting(ns, **kwargs):
    conn = ns["open_cache_db"]()
    try:
        return ns["sync_codex_cache"](conn, **kwargs)
    finally:
        conn.close()


def _sync_conversations(ns, **kwargs):
    conn = ns["open_conversations_db"]()
    try:
        return ns["sync_codex_conversations"](conn, **kwargs)
    finally:
        conn.close()


def _replace_in_place(path: pathlib.Path, records) -> os.stat_result:
    """Atomically swap in a DIFFERENT file at the same size and mtime.

    ``os.replace`` gives the pathname a new inode while the caller restores the
    old size and mtime, which is precisely the state every existing guard reads
    as unchanged.
    """
    before = path.stat()
    replacement = path.with_name(path.name + ".replacement")
    with open(replacement, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    raw = replacement.read_bytes()
    if len(raw) < before.st_size:
        raw = raw + b" " * (before.st_size - len(raw))
    else:
        raw = raw[:before.st_size]
    replacement.write_bytes(raw)
    os.replace(replacement, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino != before.st_ino, (
        "the fixture did not actually replace the inode")
    return before


# ── the stored identity exists at all ──────────────────────────────────────

def test_accounting_cursor_stores_the_identity_of_the_file_it_read(root):
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    stat = rollout.stat()
    row = _accounting_row(ns)
    assert row["device_id"] == stat.st_dev
    assert row["inode"] == stat.st_ino


def test_conversation_cursor_stores_the_identity_of_the_file_it_read(root):
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    _sync_conversations(ns)
    stat = rollout.stat()
    row = _conversation_row(ns)
    assert row["device_id"] == stat.st_dev
    assert row["inode"] == stat.st_ino


# ── the defect the columns exist for ───────────────────────────────────────

def _accounting_session_ids(ns) -> set:
    conn = ns["open_cache_db"]()
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT DISTINCT session_id FROM codex_session_entries")
        }
    finally:
        conn.close()


def _conversation_session_ids(ns) -> set:
    conn = ns["open_conversations_db"]()
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT DISTINCT last_session_id "
                "FROM codex_conversation_source_files")
        }
    finally:
        conn.close()


@pytest.mark.parametrize("table_reader", ["accounting", "conversation"])
def test_replacement_at_same_size_and_mtime_is_detected(root, table_reader):
    """Neither size nor mtime moves, so identity is the only evidence.

    Detection is not the point on its own. The stored identity has to reach
    the decision that chooses between resuming a cursor and re-reading the
    file, so this asserts the ACTION as well: the next ordinary sync re-reads
    the pathname, corrects the rows it had attributed to the file that used to
    be there, and re-stamps the cursor with the identity it actually read.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    if table_reader == "conversation":
        _sync_conversations(ns)

    before = _replace_in_place(
        rollout, _records(3, session_id="sess-replaced"))
    restat = rollout.stat()

    row = (_accounting_row(ns) if table_reader == "accounting"
           else _conversation_row(ns))
    assert (row["device_id"], row["inode"]) == (before.st_dev, before.st_ino)
    assert (row["device_id"], row["inode"]) != (restat.st_dev, restat.st_ino), (
        "the retained cursor must be distinguishable from a cursor into the "
        "file that now occupies the pathname")

    if table_reader == "accounting":
        stats = _sync_accounting(ns)
        observed = _accounting_session_ids(ns)
        row = _accounting_row(ns)
    else:
        _sync_accounting(ns)
        stats = _sync_conversations(ns)
        observed = _conversation_session_ids(ns)
        row = _conversation_row(ns)

    assert stats.files_skipped_unchanged == 0, (
        "a replacement at the same size must not be skipped as unchanged")
    assert stats.files_processed == 1
    assert stats.files_reset_truncated == 1, (
        "a replacement reuses offsets from byte zero, so it is a reset")
    assert observed == {"sess-replaced"}, (
        "the rows the previous file left behind were never corrected")
    assert (row["device_id"], row["inode"]) == (restat.st_dev, restat.st_ino)


def test_a_replaced_rollout_is_re_read_even_when_the_frontier_escalated(root):
    """The frontier escalation is worth nothing if the walk then skips.

    `classify_recent_active_path` returns `replaced` from the stored identity
    and the planner answers it with a full walk. That walk is the thing that
    has to act: if its own resume decision reads only the size, it takes the
    `size == prev_size` branch, counts the file as unchanged, and leaves the
    old offset and the old identity standing — so every following tick inside
    the recency window escalates again and the whole-estate walk becomes a
    per-tick walk that fixes nothing.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    _replace_in_place(rollout, _records(3, session_id="sess-replaced"))

    conn = ns["open_cache_db"]()
    try:
        frontier = ns["_load_sibling"]("_lib_ingest_frontier")
        targets, escalation = frontier.plan_recent_active_restat(
            conn, extra_paths=(str(rollout),))
    finally:
        conn.close()
    assert (targets, escalation) == (frozenset(), "source_replaced"), (
        "the planner no longer classifies this as a replacement")

    stats = _sync_accounting(ns)
    assert stats.files_skipped_unchanged == 0
    assert _accounting_session_ids(ns) == {"sess-replaced"}

    conn = ns["open_cache_db"]()
    try:
        again = frontier.plan_recent_active_restat(
            conn, extra_paths=(str(rollout),))
    finally:
        conn.close()
    assert again == (frozenset(), None), (
        "the walk did not clear the escalation, so every later tick repeats it")


# ── the device number is corroboration, never the decision ─────────────────

def _restamp_identity(ns, *, store: str, device=None, inode=None) -> None:
    """Rewrite the stored identity, leaving size, mtime and offset alone.

    A remount is not reproducible inside a test, and it is the whole hazard:
    ``st_dev`` is assigned when the volume is mounted, so a ``$CODEX_HOME`` on
    an external or network volume presents every retained rollout at a new
    device number after a remount, with no file changed at all.  Rewriting the
    stored column is the same observation the walk would make.
    """
    opener, table = (
        ("open_cache_db", "codex_session_files") if store == "accounting"
        else ("open_conversations_db", "codex_conversation_source_files"))
    conn = ns[opener]()
    try:
        assignments, params = [], []
        if device is not None:
            assignments.append("device_id=?")
            params.append(device)
        if inode is not None:
            assignments.append("inode=?")
            params.append(inode)
        conn.execute(f"UPDATE {table} SET {','.join(assignments)}", params)
        conn.commit()
    finally:
        conn.close()


def _incarnation(ns, rollout: pathlib.Path) -> int:
    conn = ns["open_cache_db"]()
    try:
        row = conn.execute(
            "SELECT MAX(incarnation) FROM codex_file_incarnations").fetchone()
        return 1 if row is None or row[0] is None else int(row[0])
    finally:
        conn.close()


def test_a_device_only_difference_does_not_re_read_the_accounting_cursor(root):
    """A remount must not reset every cursor in the estate to byte zero.

    ``st_dev`` is a property of the mount, not of the file.  Treating a
    device-number difference as a replacement makes a single remount classify
    EVERY retained rollout as replaced: each one re-reads from offset 0, bumps
    its incarnation, finds no account range at the new incarnation, and takes
    a fresh decision from whichever account is logged in now.  That is the
    #416 failure class — re-deriving attribution per sync — reached by a
    filesystem event that changed no file.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    before_row = _accounting_row(ns)
    before_incarnation = _incarnation(ns, rollout)

    _restamp_identity(
        ns, store="accounting", device=int(before_row["device_id"]) + 1)

    stats = _sync_accounting(ns)
    assert stats.files_reset_truncated == 0, (
        "a device-number change alone re-read the whole file")
    assert stats.files_skipped_unchanged == 1, (
        "the file did not change, so the walk must skip it")
    assert _incarnation(ns, rollout) == before_incarnation, (
        "the incarnation bumped, which discards every stored account range")
    assert _accounting_session_ids(ns) == {"sess-identity"}


def test_a_device_only_difference_does_not_re_read_the_conversation_cursor(root):
    """The conversation consumer forms the same verdict and must narrow too."""
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    _sync_conversations(ns)
    before_row = _conversation_row(ns)

    _restamp_identity(
        ns, store="conversation", device=int(before_row["device_id"]) + 1)

    _sync_accounting(ns)
    stats = _sync_conversations(ns)
    assert stats.files_skipped_unchanged == 1, (
        "a device-number change alone re-read the whole transcript")
    assert _conversation_session_ids(ns) == {"sess-identity"}


def test_an_inode_difference_still_re_reads_the_accounting_cursor(root):
    """The narrowing is to the device alone. The inode still decides."""
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    before_row = _accounting_row(ns)
    before_incarnation = _incarnation(ns, rollout)

    _restamp_identity(
        ns, store="accounting", inode=int(before_row["inode"]) + 1)

    stats = _sync_accounting(ns)
    assert stats.files_skipped_unchanged == 0
    assert stats.files_reset_truncated == 1, (
        "an inode difference is a replacement and must reset the cursor")
    assert _incarnation(ns, rollout) == before_incarnation + 1


def test_an_inode_difference_still_re_reads_the_conversation_cursor(root):
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    _sync_conversations(ns)
    before_row = _conversation_row(ns)

    _restamp_identity(
        ns, store="conversation", inode=int(before_row["inode"]) + 1)

    _sync_accounting(ns)
    stats = _sync_conversations(ns)
    assert stats.files_skipped_unchanged == 0, (
        "an inode difference is a replacement and must not be skipped")
    assert stats.files_processed == 1


@pytest.mark.parametrize("store", ["accounting", "conversation"])
def test_a_non_integer_stored_identity_degrades_to_no_evidence(root, store):
    """A `ValueError` must not escape the per-file loop.

    The stored columns are integers by construction, but a store hand-edited,
    partially migrated or written by a future schema can hold anything.  A
    bare `int()` on that value raises out of the walk and takes every LATER
    file in the estate with it; the correct degradation is the one an unknown
    identity already gets, which is to fall through to the size comparison.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    if store == "conversation":
        _sync_conversations(ns)

    opener, table = (
        ("open_cache_db", "codex_session_files") if store == "accounting"
        else ("open_conversations_db", "codex_conversation_source_files"))
    conn = ns[opener]()
    try:
        conn.execute(f"UPDATE {table} SET inode='not-an-inode'")
        conn.commit()
    finally:
        conn.close()

    if store == "accounting":
        stats = _sync_accounting(ns)
    else:
        _sync_accounting(ns)
        stats = _sync_conversations(ns)
    assert stats.files_skipped_unchanged == 1, (
        "an unreadable stored identity must degrade to the size comparison")


def test_identity_and_cursor_commit_atomically(root):
    """A rolled-back file batch never leaves a new identity on an old offset.

    The identity, the scan target, the final offset and the derived rows are
    one physical unit.  Denying the derived-row insert rolls the whole file
    transaction back, so the retained row must still describe the file the last
    SUCCESSFUL pass read — old identity AND old offset together.
    """
    ns, _provider_root, rollout = root
    _write_rollout(rollout, _records(3))
    _sync_accounting(ns)
    first = _accounting_row(ns)
    original = rollout.stat()
    assert (first["device_id"], first["inode"]) == (
        original.st_dev, original.st_ino)

    # A genuinely different file, longer than the original, at a new inode.
    replacement = rollout.with_name(rollout.name + ".next")
    with open(replacement, "w", encoding="utf-8") as fh:
        for record in _records(9, session_id="sess-replaced"):
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    os.replace(replacement, rollout)
    grown = rollout.stat()
    assert grown.st_ino != original.st_ino

    conn = ns["open_cache_db"]()
    denied = {"count": 0}

    def deny_every_event_insert(action, arg1, _arg2, _db, _source):
        if action == sqlite3.SQLITE_INSERT and arg1 == "codex_session_entries":
            denied["count"] += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        conn.set_authorizer(deny_every_event_insert)
        stats = ns["sync_codex_cache"](conn)
        conn.set_authorizer(None)
    finally:
        conn.set_authorizer(None)
        conn.close()

    assert denied["count"] >= 1, "the fixture never reached the derived insert"
    assert stats.files_processed == 0

    after = _accounting_row(ns)
    assert (after["device_id"], after["inode"]) == (
        first["device_id"], first["inode"])
    assert after["last_byte_offset"] == first["last_byte_offset"]
    assert after["size_bytes"] == first["size_bytes"]
