"""#188 Bug 4 — slash-command-with-args promotion (kernel helper + migration).

A slash-command invocation that carries a real user prompt in <command-args>
IS a user turn. The kernel helper ``_extract_command_invocation`` decides
promotion (block-aware: all-text + non-empty args); ingest (``_normalize``) and
read-time assembly both consume it. Empty-args control commands (/clear, /exit,
/compact, /model) and stdout-only markers STAY hidden as system markers.

These cover Task A1 (the helper) + Task A4 (the migration consumer flips legacy
META rows to HUMAN and the split FTS re-indexes the promoted args). The ingest
(A2) and read-time (A3) classification tests live alongside the existing
command-marker cases in test_conversation_ingest.py / test_conversation_query.py.
"""
import json
import sqlite3
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_conversation as lc  # noqa: E402
import _cctally_db as db  # noqa: E402
import _cctally_cache as cache  # noqa: E402


def _text_blocks(s):
    # The normalized blocks shape the kernel works on: {"kind": "text", "text"}.
    return [{"kind": "text", "text": s}]


# ── Task A1: _extract_command_invocation (block-aware) ──────────────────────


def test_promotes_command_with_args():
    raw = ("<command-message>frontend-design:frontend-design</command-message>\n"
           "<command-name>/frontend-design:frontend-design</command-name>\n"
           "<command-args>Audit the reader UI and file issues.</command-args>")
    out = lc._extract_command_invocation(_text_blocks(raw), raw)
    assert out == {"name": "/frontend-design:frontend-design",
                   "args": "Audit the reader UI and file issues."}


def test_empty_args_not_promoted():
    raw = ("<command-name>/clear</command-name>\n"
           "<command-message>clear</command-message>\n<command-args></command-args>")
    assert lc._extract_command_invocation(_text_blocks(raw), raw) is None


def test_whitespace_only_args_not_promoted():
    raw = "<command-name>/compact</command-name><command-args>   \n  </command-args>"
    assert lc._extract_command_invocation(_text_blocks(raw), raw) is None


def test_no_args_tag_not_promoted():
    # /exit, /model and friends emit only a <command-name> (no <command-args>).
    raw = "<command-name>/exit</command-name><command-message>exit</command-message>"
    assert lc._extract_command_invocation(_text_blocks(raw), raw) is None


def test_stdout_only_not_promoted():
    raw = "<local-command-stdout>Set model to Fable 5</local-command-stdout>"
    assert lc._extract_command_invocation(_text_blocks(raw), raw) is None


def test_marker_plus_image_not_promoted():
    # all-text guard: a marker text block PLUS an attachment is never promoted.
    raw = "<command-name>/x</command-name><command-args>hi</command-args>"
    blocks = [{"kind": "text", "text": raw}, {"kind": "image", "source": {}}]
    assert lc._extract_command_invocation(blocks, raw) is None


def test_non_marker_prose_not_promoted():
    raw = "see <command-args>not a marker</command-args> mid sentence"
    assert lc._extract_command_invocation(_text_blocks(raw), raw) is None


def test_terse_args_promoted():
    raw = "<command-name>/effort</command-name><command-args>max</command-args>"
    out = lc._extract_command_invocation(_text_blocks(raw), raw)
    assert out == {"name": "/effort", "args": "max"}


def test_args_without_name_promoted_with_empty_name():
    raw = "<command-args>just the args</command-args>"
    out = lc._extract_command_invocation(_text_blocks(raw), raw)
    assert out == {"name": "", "args": "just the args"}


def test_empty_blocks_not_promoted():
    assert lc._extract_command_invocation([], "anything") is None


def test_reexported_from_query_kernel():
    import _lib_conversation_query as cq
    assert cq._extract_command_invocation is lc._extract_command_invocation


# ── Task A4: migration 011 consumer flips legacy META rows + reindexes FTS ───


def _legacy_command_cache():
    """A cache.db carrying a legacy META command-marker row whose <command-args>
    carry a real prompt — the pre-#188 ingest shape — plus a benign /clear META
    row (empty args, must STAY meta)."""
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    promotable = ("<command-message>review:review</command-message>"
                  "<command-name>/review</command-name>"
                  "<command-args>Review feat/x vs main carefully.</command-args>")
    clear = "<command-name>/clear</command-name><command-args></command-args>"
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain) VALUES "
        "('s','u1','f',0,'t','meta','',?,0)",
        (json.dumps([{"kind": "text", "text": promotable}]),))
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain) VALUES "
        "('s','u2','f',1,'t','meta','',?,0)",
        (json.dumps([{"kind": "text", "text": clear}]),))
    conn.commit()
    return conn


def test_migration_promotes_and_indexes_legacy_command():
    conn = _legacy_command_cache()
    db._set_cache_meta(conn, "conversation_promote_command_args_pending", "1")
    cache._consume_promote_command_args(conn)

    promoted = conn.execute(
        "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()
    assert promoted[0] == "human"
    assert promoted[1] == "Review feat/x vs main carefully."

    # /clear stays a hidden meta marker (empty args).
    clear = conn.execute(
        "SELECT entry_type, text FROM conversation_messages WHERE uuid='u2'"
    ).fetchone()
    assert clear[0] == "meta" and clear[1] == ""

    # flag cleared
    assert conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_promote_command_args_pending'").fetchone() is None

    if db._fts5_available(conn):
        rid = conn.execute(
            "SELECT id FROM conversation_messages WHERE uuid='u1'").fetchone()[0]
        hits = conn.execute(
            "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH 'feat'"
        ).fetchall()
        assert (rid,) in hits


def test_migration_consumer_noop_without_flag():
    conn = _legacy_command_cache()
    # No flag armed → the consumer must not touch anything.
    cache._consume_promote_command_args(conn)
    row = conn.execute(
        "SELECT entry_type FROM conversation_messages WHERE uuid='u1'").fetchone()
    assert row[0] == "meta"


def test_migration_consumer_resumable_cursor():
    # The consumer checkpoints a cursor; a re-run after partial progress is safe.
    conn = _legacy_command_cache()
    db._set_cache_meta(conn, "conversation_promote_command_args_pending", "1")
    cache._consume_promote_command_args(conn)
    # Re-arm + re-run: already-promoted rows are HUMAN (not META), so a second
    # pass is a clean no-op and the flag clears again.
    db._set_cache_meta(conn, "conversation_promote_command_args_pending", "1")
    cache._consume_promote_command_args(conn)
    row = conn.execute(
        "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()
    assert row[0] == "human" and row[1] == "Review feat/x vs main carefully."
