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
    ``(m1, r1)`` (a thinking + two id-bearing tool_use fragment + a prose
    fragment) + ONE matching ``session_entries`` row for ``(m1, r1)`` so
    per-turn cost is non-zero. The model is ``claude-opus-4-8`` (a real id in
    ``CLAUDE_MODEL_PRICING`` — a bare ``"opus"`` prices to $0). The prose
    carries the distinctive search term "token limit window" for the search
    golden. Two skill bodies exercise the skill-content nesting fold: a PAIRED
    body (a real Skill TRIPLE — a ``Skill`` tool_use ``toolu_FX``, a "Launching
    skill" tool_result for it, and an isMeta body carrying
    ``source_tool_use_id=toolu_FX``) that folds INTO the Skill tool chip, and a
    separate UNPAIRED skill body (no ``source_tool_use_id``, SessionStart-style)
    that survives as the standalone "Skill content" pill.
  * A REPLAY of the prose fragment in a SECOND ``source_path`` (same
    ``(session_id, uuid)``, different ``byte_offset``) — proves the reader
    dedup + cost-once join + search dedup all collapse it to one.
  * Session ``s2`` — human-only, so the rail has >=2 sessions and pagination
    is testable.
  * Session ``s3`` — sidechain grouping (#155): a main turn + two PARALLEL
    subagents whose rows interleave by timestamp (distinct ``agent-*.jsonl``
    files -> distinct ``subagent_key``) + a multi-fragment sidechain turn whose
    seed fragment parents to the main turn (cross-file nesting) while its prose
    fragment parents intra-turn.

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

import json
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
    source_tool_use_id: str | None = None,
) -> None:
    """Insert one ``conversation_messages`` row (no shared helper exists).

    ``UNIQUE(source_path, byte_offset)`` mirrors production — a replay lands in
    a DIFFERENT ``source_path`` (the resume file) at its own ``byte_offset``,
    so it does not collide. The AFTER INSERT FTS trigger (created by
    ``_apply_cache_schema``) indexes ``text`` automatically when FTS5 is
    available.

    ``source_tool_use_id`` is the message-level link an injected Skill body
    carries (the transcript's ``sourceToolUseID``); the reader uses it to fold
    the body into its owning Skill tool chip. NULL on every non-skill-body row.
    """
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, parent_uuid, source_path, byte_offset, "
        " timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id, "
        " cwd, git_branch, is_sidechain, source_tool_use_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, uuid, parent_uuid, source_path, byte_offset,
            timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id,
            cwd, git_branch, is_sidechain, source_tool_use_id,
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
    s3_main = "/fake/projects/proj/s3.jsonl"
    s3_agent_a = "/fake/projects/proj/agent-aaaa1111.jsonl"
    s3_agent_b = "/fake/projects/proj/agent-bbbb2222.jsonl"
    s3_agent_c = "/fake/projects/proj/agent-cccc3333.jsonl"
    s3_cwd = "/home/u/proj"
    s4_file = "/fake/projects/proj/s4.jsonl"
    s4_cwd = "/home/u/proj"

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
        seed_session_file(cache_conn, path=s3_main, session_id="s3",
                          project_path=s3_cwd)
        seed_session_file(cache_conn, path=s3_agent_a, session_id="s3",
                          project_path=s3_cwd)
        seed_session_file(cache_conn, path=s3_agent_b, session_id="s3",
                          project_path=s3_cwd)
        seed_session_file(cache_conn, path=s3_agent_c, session_id="s3",
                          project_path=s3_cwd)
        seed_session_file(cache_conn, path=s4_file, session_id="s4",
                          project_path=s4_cwd)

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
        # id=2: assistant turn (m1,r1) — fragment 1, thinking + an id-bearing
        # tool_use (no prose). The tool_use carries an id (toolu_s1a) + a
        # parse-time preview so the reader pairs it with the matching
        # tool_result row below (#164) — exercising the kernel's two-phase fold
        # + the member_uuids growth. The SECOND tool_use is a Skill invocation
        # (id=toolu_FX, name="Skill") — the head of the Skill TRIPLE (tool_use +
        # "Launching skill" tool_result + an isMeta body carrying
        # source_tool_use_id=toolu_FX). The reader FOLDS that body into THIS
        # Skill tool_call (skill_body/skill_name set, result cleared, body uuid
        # joined to member_uuids), so the standalone skill pill disappears for
        # the paired case.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="a1a", parent_uuid="h1",
            source_path=s1_file_a, byte_offset=1,
            timestamp_utc="2026-06-01T00:00:04Z",
            entry_type="assistant", text="",
            blocks_json=(
                '[{"kind": "thinking", "text": "let me think"}, '
                '{"kind": "tool_use", "name": "Read", "input_summary": '
                '"{\\"file_path\\":\\"/home/u/proj/resets.py\\"}", '
                '"id": "toolu_s1a", "preview": "/home/u/proj/resets.py"}, '
                '{"kind": "tool_use", "name": "Skill", "input_summary": '
                '"{\\"skill\\":\\"brainstorming\\"}", '
                '"id": "toolu_FX", "preview": "brainstorming"}]'
            ),
            model=MODEL, msg_id="m1", req_id="r1",
            cwd=s1_cwd, git_branch="main",
        )
        # id=3 (NEW): the user tool_result row for toolu_s1a. The kernel folds
        # this into the (m1,r1) turn's tool_call.result and joins uuid 'tr1'
        # into that turn's member_uuids — so the golden exercises pairing +
        # folding + the #160 anchor. tool_result rows are not indexed as prose
        # (text=""), so the search golden is unaffected.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="tr1", parent_uuid="a1a",
            source_path=s1_file_a, byte_offset=2,
            timestamp_utc="2026-06-01T00:00:04Z",
            entry_type="tool_result", text="",
            blocks_json=(
                '[{"kind": "tool_result", "text": "def reset(): ...", '
                '"truncated": false, "is_error": false, '
                '"tool_use_id": "toolu_s1a"}]'
            ),
            cwd=s1_cwd, git_branch="main",
        )
        # The trivial "Launching skill" tool_result for toolu_FX (Skill triple,
        # message 2 of 3). The Phase 4b skill-body fold REPLACES this result with
        # the rich body and sets the chip's result to null, so this row's only
        # job is to be the pre-fold pairing target the body supersedes.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="tr_fx", parent_uuid="a1a",
            source_path=s1_file_a, byte_offset=5,
            timestamp_utc="2026-06-01T00:00:04Z",
            entry_type="tool_result", text="",
            blocks_json=(
                '[{"kind": "tool_result", "text": "Launching skill: brainstorming", '
                '"truncated": false, "is_error": false, '
                '"tool_use_id": "toolu_FX"}]'
            ),
            cwd=s1_cwd, git_branch="main",
        )
        # id=5: assistant turn (m1,r1) — fragment 2, prose-bearing. Carries the
        # distinctive search term. This fragment is the turn's canonical anchor.
        # (byte_offset is arbitrary-but-unique per file; the reader orders by
        # timestamp_utc, id — both tool_result rows tr1/tr_fx precede this row.)
        _insert_message(
            cache_conn,
            session_id="s1", uuid="a1b", parent_uuid="a1a",
            source_path=s1_file_a, byte_offset=3,
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
        # id=6: REPLAY of the prose fragment in the resume file (b.jsonl). Same
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

        # PAIRED skill body (Skill triple, message 3 of 3): an injected isMeta
        # skill body carrying source_tool_use_id=toolu_FX — the explicit link
        # back to the Skill tool_use in a1a. entry_type='meta', text='' (not
        # FTS-indexed, not a title candidate); the body lives in a text block.
        # The reader FOLDS this into the Skill tool_call (skill_body/skill_name
        # set, result cleared, uuid sk1 joined to member_uuids) and DROPS the
        # standalone item — so s1 has NO standalone skill pill. No msg_id/req_id
        # -> no cost join.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="sk1", parent_uuid="tr_fx",
            source_path=s1_file_a, byte_offset=4,
            timestamp_utc="2026-06-01T00:00:06Z",
            entry_type="meta", text="",
            blocks_json=(
                '[{"kind": "text", "text": "Base directory for this skill: '
                '/home/u/.claude/skills/brainstorming\\n\\n# Brainstorming Ideas"}]'
            ),
            cwd=s1_cwd, git_branch="main",
            source_tool_use_id="toolu_FX",
        )

        # UNPAIRED skill body: an isMeta skill body with NO source_tool_use_id
        # (a SessionStart-injected skill, e.g. using-superpowers — no Skill
        # tool_use, no link). The reader CANNOT fold it, so it survives as the
        # standalone "Skill content · using-superpowers" pill — the permanent
        # fallback path. This is the SINGLE remaining meta item on s1 after the
        # paired sk1 folds into its Skill chip.
        _insert_message(
            cache_conn,
            session_id="s1", uuid="sk2", parent_uuid="a1b",
            source_path=s1_file_a, byte_offset=6,
            timestamp_utc="2026-06-01T00:00:07Z",
            entry_type="meta", text="",
            blocks_json=(
                '[{"kind": "text", "text": "Base directory for this skill: '
                '/home/u/.claude/skills/using-superpowers\\n\\n# Using Superpowers"}]'
            ),
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
        _insert_message(
            cache_conn,
            session_id="s2", uuid="h2", parent_uuid=None,
            source_path=s2_file, byte_offset=0,
            timestamp_utc="2026-06-02T00:00:00Z",
            entry_type="human", text="how do I set a weekly budget",
            cwd=s2_cwd, git_branch="dev",
        )

        # --- session s3: sidechain grouping (#155) ---------------------------
        # Main turn that "spawns" the subagents (cross-file nesting target).
        _insert_message(
            cache_conn,
            session_id="s3", uuid="s3h1", parent_uuid=None,
            source_path=s3_main, byte_offset=0,
            timestamp_utc="2026-06-03T00:00:00Z",
            entry_type="human", text="run the audits",
            cwd=s3_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s3", uuid="s3a1", parent_uuid="s3h1",
            source_path=s3_main, byte_offset=1,
            timestamp_utc="2026-06-03T00:00:01Z",
            entry_type="assistant", text="spawning two audits in parallel",
            blocks_json=json.dumps([
                {"kind": "text", "text": "spawning two audits in parallel"},
                {"kind": "tool_use", "name": "Task",
                 "input_summary": '{"description":"Audit module A","subagent_type":"Explore"}',
                 "id": "toolu_a", "preview": "Audit module A", "subagent_type": "Explore"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Audit module B","subagent_type":"code-reviewer"}',
                 "id": "toolu_b", "preview": "Audit module B", "subagent_type": "code-reviewer"},
            ]),
            model=MODEL, msg_id="m3", req_id="r3",
            cwd=s3_cwd, git_branch="main",
        )
        # Two PARALLEL subagents whose rows INTERLEAVE by timestamp (A,B,A,B).
        # Old contiguous-run grouping would fuse them into ONE group; the new
        # subagent_key grouping must split them into TWO. Both roots have a null
        # parent (matches real data: 61/64 subagent roots are null) -> document
        # order, NOT nested.
        _insert_message(
            cache_conn, session_id="s3", uuid="a1", parent_uuid=None,
            source_path=s3_agent_a, byte_offset=0,
            timestamp_utc="2026-06-03T00:00:02Z",
            entry_type="human", text="Audit module A", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s3", uuid="b1", parent_uuid=None,
            source_path=s3_agent_b, byte_offset=0,
            timestamp_utc="2026-06-03T00:00:03Z",
            entry_type="human", text="Audit module B", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s3", uuid="a2", parent_uuid="a1",
            source_path=s3_agent_a, byte_offset=1,
            timestamp_utc="2026-06-03T00:00:04Z",
            entry_type="assistant", text="module A clean",
            blocks_json='[{"kind": "text", "text": "module A clean"}]',
            model=MODEL, msg_id="ma", req_id="ra", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s3", uuid="b2", parent_uuid="b1",
            source_path=s3_agent_b, byte_offset=1,
            timestamp_utc="2026-06-03T00:00:05Z",
            entry_type="assistant", text="module B clean",
            blocks_json='[{"kind": "text", "text": "module B clean"}]',
            model=MODEL, msg_id="mb", req_id="rb", is_sidechain=1,
        )
        # Subagent C: a MULTI-FRAGMENT sidechain turn whose SEED fragment parents
        # to the MAIN turn s3a1 (cross-file entry point) and whose prose fragment
        # parents intra-turn (c1 -> c2). The reader turn item must carry the SEED
        # parent (s3a1), so the frontend can nest this group under the main turn.
        _insert_message(
            cache_conn, session_id="s3", uuid="c1", parent_uuid="s3a1",
            source_path=s3_agent_c, byte_offset=0,
            timestamp_utc="2026-06-03T00:00:06Z",
            entry_type="assistant", text="",
            blocks_json='[{"kind": "thinking", "text": "planning"}]',
            model=MODEL, msg_id="mc", req_id="rc", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s3", uuid="c2", parent_uuid="c1",
            source_path=s3_agent_c, byte_offset=1,
            timestamp_utc="2026-06-03T00:00:07Z",
            entry_type="assistant", text="module C needs a follow-up",
            blocks_json='[{"kind": "text", "text": "module C needs a follow-up"}]',
            model=MODEL, msg_id="mc", req_id="rc", is_sidechain=1,
        )
        # Two spawn-result tool_result rows on the MAIN file carrying the #166
        # record-level toolUseResult linkage: tool_use_id matches s3a1's spawn
        # ids; agent_id matches subagent_keys aaaa1111 / bbbb2222 (A completed,
        # B error). Subagent C (cccc3333) gets NO linkage -> title-only fallback.
        _insert_message(
            cache_conn, session_id="s3", uuid="tr_a", parent_uuid="s3a1",
            source_path=s3_main, byte_offset=2,
            timestamp_utc="2026-06-03T00:00:08Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "text": "module A audited",
                "truncated": False, "is_error": False, "tool_use_id": "toolu_a",
                "agent_id": "aaaa1111",
                "subagent_meta": {"total_tokens": 23285, "total_duration_ms": 10668,
                                  "total_tool_use_count": 1, "status": "completed"}}]),
            cwd=s3_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn, session_id="s3", uuid="tr_b", parent_uuid="s3a1",
            source_path=s3_main, byte_offset=3,
            timestamp_utc="2026-06-03T00:00:09Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "text": "module B FAILED",
                "truncated": False, "is_error": True, "tool_use_id": "toolu_b",
                "agent_id": "bbbb2222",
                "subagent_meta": {"total_tokens": 5120, "total_duration_ms": 4200,
                                  "total_tool_use_count": 0, "status": "error"}}]),
            cwd=s3_cwd, git_branch="main",
        )
        # session_entries so the parallel + nested turns have non-zero cost.
        seed_session_entry(cache_conn, source_path=s3_main, line_offset=1,
                           timestamp_utc="2026-06-03T00:00:01Z", model=MODEL,
                           msg_id="m3", req_id="r3", input_tokens=500, output_tokens=200)
        seed_session_entry(cache_conn, source_path=s3_agent_a, line_offset=1,
                           timestamp_utc="2026-06-03T00:00:04Z", model=MODEL,
                           msg_id="ma", req_id="ra", input_tokens=400, output_tokens=100)
        seed_session_entry(cache_conn, source_path=s3_agent_b, line_offset=1,
                           timestamp_utc="2026-06-03T00:00:05Z", model=MODEL,
                           msg_id="mb", req_id="rb", input_tokens=400, output_tokens=100)
        seed_session_entry(cache_conn, source_path=s3_agent_c, line_offset=1,
                           timestamp_utc="2026-06-03T00:00:07Z", model=MODEL,
                           msg_id="mc", req_id="rc", input_tokens=300, output_tokens=150)

        # --- session s4: marker-first → title derivation must SKIP the
        # /clear plumbing and pick the SECOND human (#165 Q2 end-to-end). ----
        _insert_message(
            cache_conn,
            session_id="s4", uuid="h4m", parent_uuid=None,
            source_path=s4_file, byte_offset=0,
            timestamp_utc="2026-06-04T00:00:00Z",
            entry_type="human",
            text=("<command-name>clear</command-name>"
                  "<command-message>clear</command-message>"
                  "<command-args></command-args>"),
            cwd=s4_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s4", uuid="h4", parent_uuid="h4m",
            source_path=s4_file, byte_offset=1,
            timestamp_utc="2026-06-04T00:00:02Z",
            entry_type="human", text="set up the marker-skip scenario",
            cwd=s4_cwd, git_branch="main",
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
