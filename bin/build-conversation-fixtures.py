#!/usr/bin/env python3
"""Build the seeded SQLite fixture for the conversation-viewer endpoints
(Plan 2, spec §3 / §6.8).

Writes one ``cache.db`` (+ empty ``stats.db``) under
``tests/fixtures/conversation/all-history/.local/share/cctally/`` that the
``bin/cctally-conversation-test`` harness boots the dashboard against to
exercise the three conversation GET routes and the loopback/Host privacy gate
end-to-end.

The cache.db holds Plan 1's ``conversation_messages`` (+ FTS, created by
``_apply_cache_schema``) and ``session_entries`` rows shaped to stress the
load-bearing kernel invariants:

  * Session ``s1`` — a human prompt + a MULTI-FRAGMENT assistant turn
    ``(m1, r1)`` (a thinking-only fragment + a prose fragment) + ONE matching
    ``session_entries`` row for ``(m1, r1)`` so per-turn cost is non-zero. The
    model is ``claude-opus-4-8`` (a real id in ``CLAUDE_MODEL_PRICING`` — a
    bare ``"opus"`` prices to $0). The prose carries the distinctive search
    term "token limit window" for the search golden.
  * A REPLAY of the prose fragment in a SECOND ``source_path`` (same
    ``(session_id, uuid)``, different ``byte_offset``) — proves the reader
    dedup + cost-once join + search dedup all collapse it to one.
  * Session ``s2`` — human-only, so the rail has >=2 sessions and pagination
    is testable.

No ``seed_conversation_message`` helper exists in ``_fixture_builders`` — the
``conversation_messages`` rows are INSERTed directly here. The cache.db is
registered for cleanup via ``create_cache_db``'s internal ``register_fixture_db``
call (this builder never calls ``register_fixture_db`` directly — neither does
the sibling ``build-dashboard-fixtures.py``; the transitive registration is
sufficient), so the atexit hook gc-closes the connections and zeros the SQLite
writer-version bytes (96-99); without that the committed fixture re-dirties on
every harness run.

Migration posture mirrors ``build-dashboard-fixtures.py``: the empty stats.db
is stamped fully-migrated (``stamp_all_stats_migrations_applied``) so a read
command's ``sync_cache`` walk can't flip the upgrade-gate to PROCEED. The
conversation routes never read stats.db, but the dashboard server opens it at
boot, so it must exist and be migration-clean.

The cache.db half is NOT pre-stamped to head: ``create_cache_db`` stamps only
cache-001 (``001_dedup_highest_wins``, ``user_version = 1``), while the cache
migration registry also holds ``002_conversation_messages_backfill``. So on the
first ``cctally`` open the dispatcher sees ``user_version 1 != registry len 2``,
runs the ``002`` handler (advancing ``user_version`` -> 2 and setting
``cache_meta.conversation_backfill_pending = '1'``, which stays set under the
harness's ``--no-sync``). This is benign here: the conversation routes read the
``conversation_messages`` / ``session_entries`` tables directly and never gate
on that flag, and the gitignored ``.db`` doesn't re-dirty the tree.

Run: ``bin/build-conversation-fixtures.py`` (idempotent; overwrites).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
    stamp_all_stats_migrations_applied,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/conversation"

# Single scenario: the all-history rail + reader + search + gate, exercised by
# the same seeded cache.db. (One scenario is enough — the gate truth-table is
# driven by Host headers at the harness layer, not by separate fixtures.)
SCENARIO = "all-history"

# The model MUST be a real id in CLAUDE_MODEL_PRICING (a bare "opus" prices to
# $0 and would make the cost-once assertions vacuous — Implementer B hit this).
MODEL = "claude-opus-4-8"

# Distinctive search term embedded in the assistant prose (the search golden
# matches the single-token query "token" against it).
SEARCH_TERM = "token limit window"


def _insert_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    uuid: str,
    parent_uuid: str | None,
    source_path: str,
    byte_offset: int,
    timestamp_utc: str,
    entry_type: str,
    text: str = "",
    blocks_json: str = "[]",
    model: str | None = None,
    msg_id: str | None = None,
    req_id: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    is_sidechain: int = 0,
) -> None:
    """Insert one ``conversation_messages`` row (no shared helper exists).

    ``UNIQUE(source_path, byte_offset)`` mirrors production — a replay lands in
    a DIFFERENT ``source_path`` (the resume file) at its own ``byte_offset``,
    so it does not collide. The AFTER INSERT FTS trigger (created by
    ``_apply_cache_schema``) indexes ``text`` automatically when FTS5 is
    available.
    """
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, parent_uuid, source_path, byte_offset, "
        " timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id, "
        " cwd, git_branch, is_sidechain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, uuid, parent_uuid, source_path, byte_offset,
            timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id,
            cwd, git_branch, is_sidechain,
        ),
    )


def build(scenario: str) -> None:
    scenario_dir = FIXTURES_DIR / scenario
    app_dir = scenario_dir / ".local" / "share" / "cctally"
    app_dir.mkdir(parents=True, exist_ok=True)
    cache_path = app_dir / "cache.db"
    stats_path = app_dir / "stats.db"

    create_cache_db(cache_path)
    create_stats_db(stats_path)

    # Resume files for s1: the original session file (a.jsonl) and the resume
    # file (b.jsonl) where the prose fragment is REPLAYED.
    s1_file_a = "/fake/projects/proj/s1-a.jsonl"
    s1_file_b = "/fake/projects/proj/s1-b.jsonl"
    s2_file = "/fake/projects/other/s2.jsonl"
    s1_cwd = "/home/u/proj"
    s2_cwd = "/home/u/other"

    cache_conn = sqlite3.connect(cache_path)
    stats_conn = sqlite3.connect(stats_path)
    try:
        # --- session_files (powers resume-merge join on source_path) ---------
        seed_session_file(cache_conn, path=s1_file_a, session_id="s1",
                          project_path=s1_cwd)
        seed_session_file(cache_conn, path=s1_file_b, session_id="s1",
                          project_path=s1_cwd)
        seed_session_file(cache_conn, path=s2_file, session_id="s2",
                          project_path=s2_cwd)

        # --- session s1: human prompt + multi-fragment assistant turn --------
        # id=1: human prompt.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="h1", parent_uuid=None,
            source_path=s1_file_a, byte_offset=0,
            timestamp_utc="2026-06-01T00:00:00Z",
            entry_type="human", text="how does the reset work",
            cwd=s1_cwd, git_branch="main",
        )
        # id=2: assistant turn (m1,r1) — fragment 1, thinking-only (no prose).
        _insert_message(
            cache_conn,
            session_id="s1", uuid="a1a", parent_uuid="h1",
            source_path=s1_file_a, byte_offset=1,
            timestamp_utc="2026-06-01T00:00:04Z",
            entry_type="assistant", text="",
            blocks_json='[{"kind": "thinking", "text": "let me think"}]',
            model=MODEL, msg_id="m1", req_id="r1",
            cwd=s1_cwd, git_branch="main",
        )
        # id=3: assistant turn (m1,r1) — fragment 2, prose-bearing. Carries the
        # distinctive search term. This fragment is the turn's canonical anchor.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="a1b", parent_uuid="a1a",
            source_path=s1_file_a, byte_offset=2,
            timestamp_utc="2026-06-01T00:00:05Z",
            entry_type="assistant",
            text=f"the {SEARCH_TERM} resets every five hours",
            blocks_json=(
                '[{"kind": "text", "text": "the '
                + SEARCH_TERM
                + ' resets every five hours"}]'
            ),
            model=MODEL, msg_id="m1", req_id="r1",
            cwd=s1_cwd, git_branch="main",
        )
        # id=4: REPLAY of the prose fragment in the resume file (b.jsonl). Same
        # (session_id, uuid)=(s1, a1b) + same (msg_id, req_id)=(m1, r1) but a
        # distinct (source_path, byte_offset) so it does not collide. The
        # reader dedups it by uuid, the cost join counts it once, and search
        # dedups it to one hit.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="a1b", parent_uuid="a1a",
            source_path=s1_file_b, byte_offset=0,
            timestamp_utc="2026-06-01T00:00:05Z",
            entry_type="assistant",
            text=f"the {SEARCH_TERM} resets every five hours",
            blocks_json=(
                '[{"kind": "text", "text": "the '
                + SEARCH_TERM
                + ' resets every five hours"}]'
            ),
            model=MODEL, msg_id="m1", req_id="r1",
            cwd=s1_cwd, git_branch="main",
        )

        # ONE session_entries row for the turn (m1,r1) — cost joins to THIS
        # single deduped row (idx_entries_dedup is UNIQUE on (msg_id,req_id)),
        # so the replay can never double the cost. claude-opus-4-8: input
        # 1000 * $5e-6 + output 500 * $2.5e-5 = $0.0175 (non-zero).
        seed_session_entry(
            cache_conn,
            source_path=s1_file_a, line_offset=2,
            timestamp_utc="2026-06-01T00:00:05Z",
            model=MODEL, msg_id="m1", req_id="r1",
            input_tokens=1000, output_tokens=500,
        )

        # --- session s2: human-only (rail >=2 + pagination) ------------------
        # id=5.
        _insert_message(
            cache_conn,
            session_id="s2", uuid="h2", parent_uuid=None,
            source_path=s2_file, byte_offset=0,
            timestamp_utc="2026-06-02T00:00:00Z",
            entry_type="human", text="how do I set a weekly budget",
            cwd=s2_cwd, git_branch="dev",
        )

        # Empty stats.db stamped fully-migrated (dashboard-fixtures posture):
        # the dashboard server opens it at boot even though the conversation
        # routes never read it.
        stamp_all_stats_migrations_applied(stats_conn)

        cache_conn.commit()
        stats_conn.commit()
    finally:
        cache_conn.close()
        stats_conn.close()

    # input.env carries the loopback Host the harness pins the rail/reader/
    # search 200-path assertions to, and the LAN hostname Host that the gate
    # must reject with 403 (expose unset → default false).
    (scenario_dir / "input.env").write_text(
        "LOOPBACK_HOST=127.0.0.1\n"
        "LAN_HOSTNAME_HOST=lan-host.example.com\n"
        "SEARCH_QUERY=token\n"
    )


if __name__ == "__main__":
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build(SCENARIO)
    print(f"built: {SCENARIO}")
    print(f"Built fixtures under {FIXTURES_DIR}")
