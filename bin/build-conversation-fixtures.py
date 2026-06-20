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
  * Session ``s5`` (#177) — the raw-cost-override guard: a turn whose single
    deduped ``session_entries`` row carries BOTH token columns AND a
    ``cost_usd_raw`` override, so the reader surfaces ``tokens`` from that row
    while ``cost_usd`` equals the override (token-derived math bypassed) — the
    "same source row, NOT same arithmetic" contract (no consumer may assert
    ``cost == f(tokens)``). The turn also pins ``input``/``input_truncated`` on
    a tool_call, ``stop_reason``/``attribution_*`` on the item, and
    ``result.full_length`` on a truncated tool result.
  * Session ``s7`` — the cache-failure-markers scenario: a first-prime turn
    (cr=0, never flagged) + a healthy turn (cr~rm, establishes the running-max)
    + one clear MAIN-thread failure (cache_read collapses to 0 while
    cache_creation balloons) + one SUBAGENT-thread failure (its own
    ``agent-*.jsonl`` file -> distinct ``subagent_key``, keyed independently so
    the main thread's high running-max never false-flags it). The reader carries
    ``cache_failure`` on exactly the two failing turns; the outline copies it +
    accumulates a session-level ``stats.cache_failures`` aggregate. Recomputed
    per read inside ``_assemble_session`` (no schema change, no migration).

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
# The browse-rail rollup is populated by sync_cache in production, but this
# builder direct-inserts conversation_messages and never calls sync_cache — so
# we recompute the rollup here (full; flag stays clear) after seeding. Without
# this the fixture's conversation_sessions table would be empty and the rail
# read would exercise only the live-GROUP-BY fallback, never the fast rollup
# path that the harness is meant to cover.
from _cctally_cache import _recompute_conversation_sessions  # noqa: E402

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
    stop_reason: str | None = None,
    attribution_skill: str | None = None,
    attribution_plugin: str | None = None,
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

    ``stop_reason`` / ``attribution_skill`` / ``attribution_plugin`` are the
    #177-enriched message-level columns (tail-appended, matching the production
    INSERT tuple). The reader surfaces stop_reason / attribution on assistant
    items. All NULL by default on rows that don't need them. (#217 S1 / U7a: the
    dead ``search_aux`` column was dropped from the live schema, so it is no
    longer inserted here.)
    """
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, parent_uuid, source_path, byte_offset, "
        " timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id, "
        " cwd, git_branch, is_sidechain, source_tool_use_id, "
        " stop_reason, attribution_skill, attribution_plugin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, uuid, parent_uuid, source_path, byte_offset,
            timestamp_utc, entry_type, text, blocks_json, model, msg_id, req_id,
            cwd, git_branch, is_sidechain, source_tool_use_id,
            stop_reason, attribution_skill, attribution_plugin,
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
    s5_file = "/fake/projects/proj/s5.jsonl"
    s5_cwd = "/home/u/proj"
    s6_file = "/fake/projects/proj/s6.jsonl"
    s6_cwd = "/home/u/proj"
    # s7 — the cache-failure-markers scenario: a first-prime + a healthy turn +
    # one clear MAIN-thread failure + one SUBAGENT-thread failure (its own file).
    s7_file = "/fake/projects/proj/s7.jsonl"
    s7_agent = "/fake/projects/proj/agent-dddd4444.jsonl"
    s7_cwd = "/home/u/proj"
    # s8 — the new subagent format (spec §4): a nested grandchild (string-content
    # result with agentId+<usage>), an async subagent (launch result + a
    # <task-notification>), and two truncated-result variants ((i) <usage>
    # clipped -> links + derived totals; (ii) agentId clipped -> flat card). The
    # main turn s8a1 holds FOUR spawns in one item (Codex P1-C two-spawn case).
    s8_main = "/fake/projects/proj/s8.jsonl"
    s8_agent_child = "/fake/projects/proj/agent-cc110001.jsonl"   # child subagent
    s8_agent_gchild = "/fake/projects/proj/agent-ace20002.jsonl"  # grandchild
    s8_agent_async = "/fake/projects/proj/agent-a55c0003.jsonl"   # background subagent
    s8_agent_trunc = "/fake/projects/proj/agent-b0c40004.jsonl"   # truncated-(i) child
    s8_cwd = "/home/u/proj"

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
        seed_session_file(cache_conn, path=s5_file, session_id="s5",
                          project_path=s5_cwd)
        seed_session_file(cache_conn, path=s6_file, session_id="s6",
                          project_path=s6_cwd)
        seed_session_file(cache_conn, path=s7_file, session_id="s7",
                          project_path=s7_cwd)
        seed_session_file(cache_conn, path=s7_agent, session_id="s7",
                          project_path=s7_cwd)
        for _p in (s8_main, s8_agent_child, s8_agent_gchild,
                   s8_agent_async, s8_agent_trunc):
            seed_session_file(cache_conn, path=_p, session_id="s8",
                              project_path=s8_cwd)

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
                '"input": {"file_path": "/home/u/proj/resets.py"}, '
                '"input_truncated": false, '
                '"id": "toolu_s1a", "preview": "/home/u/proj/resets.py"}, '
                '{"kind": "tool_use", "name": "Skill", "input_summary": '
                '"{\\"skill\\":\\"brainstorming\\"}", '
                '"input": {"skill": "brainstorming"}, "input_truncated": false, '
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
                '"truncated": false, "full_length": 16, "is_error": false, '
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
            # #177: the terminal prose fragment carries the turn's stop_reason +
            # skill/plugin attribution — the reader surfaces them on the turn item.
            stop_reason="end_turn",
            attribution_skill="superpowers:brainstorming",
            attribution_plugin="superpowers",
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
                 # #193: the bounded input carries the spawning description, which
                 # the query-time harvest copies into subagent_meta[aaaa1111].
                 "input": {"description": "Audit module A", "subagent_type": "Explore",
                           "prompt": "Audit module A thoroughly"},
                 "id": "toolu_a", "preview": "Audit module A", "subagent_type": "Explore"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Audit module B","subagent_type":"code-reviewer"}',
                 "input": {"description": "Audit module B", "subagent_type": "code-reviewer",
                           "prompt": "Audit module B thoroughly"},
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

        # --- session s5: the #177 raw-cost-override guard (Codex P1 "same
        # source row, NOT same arithmetic"). A human prompt + an assistant turn
        # whose single deduped session_entries row carries BOTH token columns
        # AND a vendor-provided ``cost_usd_raw`` override. The reader surfaces
        # per-turn ``tokens`` from that row, but ``cost_usd`` equals the raw
        # override (token-derived math bypassed) — so the two are deliberately
        # NOT equal, and no consumer may assert cost == f(tokens). The turn also
        # carries a truncated tool result (full_length >> capped text) and a
        # structured-but-truncated tool input, so the s5 reader golden pins
        # every #177 surface in one place. ----------------------------------
        _insert_message(
            cache_conn,
            session_id="s5", uuid="s5h1", parent_uuid=None,
            source_path=s5_file, byte_offset=0,
            timestamp_utc="2026-06-05T00:00:00Z",
            entry_type="human", text="apply the patch and report the cost",
            cwd=s5_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s5", uuid="s5a1", parent_uuid="s5h1",
            source_path=s5_file, byte_offset=1,
            timestamp_utc="2026-06-05T00:00:01Z",
            entry_type="assistant", text="patched the resolver",
            # #193: the Bash tool_use carries input.description; the server passes
            # it through on call.input (the client's BashCard renders it) and must
            # NOT harvest it as a subagent description (no subagent_type here) — so
            # s5's subagent_meta stays empty in the reader golden.
            blocks_json=(
                '[{"kind": "tool_use", "name": "Edit", "input_summary": '
                '"{\\"file_path\\":\\"/home/u/proj/resolve.py\\"}", '
                '"input": {"file_path": "/home/u/proj/resolve.py", '
                '"old_string": "clipped-leaf…"}, "input_truncated": true, '
                # #198: true {add, del} stamped at ingest from the FULL (un-clipped)
                # input. Carried on the truncated Edit so the reader contract proves
                # the field survives the tool_use -> tool_call fold to the client.
                '"edit_stat": {"add": 412, "del": 87}, '
                '"id": "toolu_s5", "preview": "/home/u/proj/resolve.py"}, '
                '{"kind": "tool_use", "name": "Bash", "input_summary": '
                '"{\\"command\\":\\"pytest -q\\"}", '
                '"input": {"command": "pytest -q", "description": "Run the test suite"}, '
                '"id": "toolu_s5bash", "preview": "pytest -q"}, '
                '{"kind": "text", "text": "patched the resolver"}]'
            ),
            model=MODEL, msg_id="m5", req_id="r5",
            cwd=s5_cwd, git_branch="main",
            stop_reason="tool_use",
            attribution_skill="superpowers:test-driven-development",
            attribution_plugin="superpowers",
        )
        # A truncated tool_result for toolu_s5: capped ``text`` (a short stub)
        # with ``full_length`` recording the true pre-clip size, ``truncated``
        # honest. The reader folds this into s5a1's tool_call.result, exercising
        # the result.full_length surface end-to-end.
        _insert_message(
            cache_conn,
            session_id="s5", uuid="s5tr", parent_uuid="s5a1",
            source_path=s5_file, byte_offset=2,
            timestamp_utc="2026-06-05T00:00:02Z",
            entry_type="tool_result", text="",
            blocks_json=(
                '[{"kind": "tool_result", "text": "applied 1 edit (truncated)", '
                '"truncated": true, "full_length": 48213, "is_error": false, '
                '"tool_use_id": "toolu_s5"}]'
            ),
            cwd=s5_cwd, git_branch="main",
        )
        # ONE session_entries row for (m5,r5) carrying BOTH tokens AND a raw
        # cost override. tokens surface on the turn; cost_usd == the override
        # ($0.99), which is intentionally NOT token-derived ($0.0175 for these
        # tokens) — the guard against a cost==f(tokens) assertion creeping in.
        seed_session_entry(
            cache_conn,
            source_path=s5_file, line_offset=1,
            timestamp_utc="2026-06-05T00:00:01Z",
            model=MODEL, msg_id="m5", req_id="r5",
            input_tokens=1000, output_tokens=500,
            cost_usd_raw=0.99,
        )

        # --- session s6: the #186 command-marker + ANSI end-to-end golden.
        # The FIRST user line is a slash-command stdout echo carrying terminal
        # SGR styling, stored entry_type='human' to model a PRE-FIX row (ingested
        # before the parser-level classification + ingest ANSI strip shipped). It
        # exercises BOTH read-time defenses in one payload: the read-time
        # _meta_classify reorder folds it to kind='meta'/meta_kind='command' (so
        # it renders as a System-marker pill, not a "YOU" turn) AND the read-time
        # ANSI strip removes the raw `\x1b[1m…\x1b[22m` from the folded command
        # <pre> body. The SECOND user line is the real prompt — it wins the title
        # (clean, no plumbing), proving the title de-poisoning end-to-end. ----
        _insert_message(
            cache_conn,
            session_id="s6", uuid="s6m", parent_uuid=None,
            source_path=s6_file, byte_offset=0,
            timestamp_utc="2026-06-06T00:00:00Z",
            entry_type="human",
            text=("<local-command-stdout>Set model to "
                  "\x1b[1mFable 5\x1b[22m</local-command-stdout>"),
            blocks_json=json.dumps([{"kind": "text", "text": (
                "<local-command-stdout>Set model to "
                "\x1b[1mFable 5\x1b[22m</local-command-stdout>")}]),
            cwd=s6_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s6", uuid="s6h", parent_uuid="s6m",
            source_path=s6_file, byte_offset=1,
            timestamp_utc="2026-06-06T00:00:02Z",
            entry_type="human",
            text="complete Session 6 — Search depth from issue #177",
            blocks_json=json.dumps([{"kind": "text", "text": (
                "complete Session 6 — Search depth from issue #177")}]),
            cwd=s6_cwd, git_branch="main",
        )

        # --- session s7: the cache-failure-markers scenario (spec §1/§2). A
        # first-prime turn + a healthy turn + one clear MAIN-thread failure +
        # one SUBAGENT-thread failure (its own agent file). The detector
        # recomputes per read inside _assemble_session; no schema change. Each
        # assistant turn has ONE session_entries row carrying the cache columns
        # that drive the running-max collapse rule. The big cache_create / read
        # token counts come from the embedded claude-opus-4-8 pricing, so the
        # est_wasted_usd estimate on each flagged turn is genuinely non-zero. ---
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7h1", parent_uuid=None,
            source_path=s7_file, byte_offset=0,
            timestamp_utc="2026-06-07T00:00:00Z",
            entry_type="human", text="trace the cache rebuild",
            cwd=s7_cwd, git_branch="main",
        )
        # Turn 1 — FIRST PRIME (cr=0, rm=0). The unavoidable initial cache build:
        # rm < CACHE_FLOOR -> NEVER flagged.
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7a1", parent_uuid="s7h1",
            source_path=s7_file, byte_offset=1,
            timestamp_utc="2026-06-07T00:00:01Z",
            entry_type="assistant", text="primed the context",
            blocks_json='[{"kind": "text", "text": "primed the context"}]',
            model=MODEL, msg_id="m7a", req_id="r7a",
            cwd=s7_cwd, git_branch="main",
        )
        # Turn 2 — HEALTHY (cr ~ rm): cache_read tracks the running-max, so the
        # running-max never collapses -> NOT flagged. Establishes rm=130000.
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7a2", parent_uuid="s7a1",
            source_path=s7_file, byte_offset=2,
            timestamp_utc="2026-06-07T00:00:02Z",
            entry_type="assistant", text="read from cache",
            blocks_json='[{"kind": "text", "text": "read from cache"}]',
            model=MODEL, msg_id="m7b", req_id="r7b",
            cwd=s7_cwd, git_branch="main",
        )
        # Turn 3 — MAIN-THREAD FAILURE: cache_read collapses to 0 while
        # cache_creation balloons (cc=134000 >= CREATE_FLOOR, cr=0 <= 0.5*rm,
        # frac=1.0 >= RECREATE_FRACTION) -> FLAGGED. lost = min(134000, 130000-0)
        # = 130000.
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7a3", parent_uuid="s7a2",
            source_path=s7_file, byte_offset=3,
            timestamp_utc="2026-06-07T00:00:03Z",
            entry_type="assistant", text="rebuilt the whole prefix",
            blocks_json='[{"kind": "text", "text": "rebuilt the whole prefix"}]',
            model=MODEL, msg_id="m7c", req_id="r7c",
            cwd=s7_cwd, git_branch="main",
        )
        # Subagent thread (its own agent file -> subagent_key dddd4444). Prime +
        # collapse, keyed independently of the main thread so the main thread's
        # high running-max never false-flags the subagent and the subagent's own
        # collapse flags on ITS key.
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7g1", parent_uuid="s7a1",
            source_path=s7_agent, byte_offset=0,
            timestamp_utc="2026-06-07T00:00:04Z",
            entry_type="assistant", text="subagent primed",
            blocks_json='[{"kind": "text", "text": "subagent primed"}]',
            model=MODEL, msg_id="m7d", req_id="r7d", is_sidechain=1,
            cwd=s7_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s7", uuid="s7g2", parent_uuid="s7g1",
            source_path=s7_agent, byte_offset=1,
            timestamp_utc="2026-06-07T00:00:05Z",
            entry_type="assistant", text="subagent rebuilt",
            blocks_json='[{"kind": "text", "text": "subagent rebuilt"}]',
            model=MODEL, msg_id="m7e", req_id="r7e", is_sidechain=1,
            cwd=s7_cwd, git_branch="main",
        )
        # session_entries carrying the cache columns that drive detection.
        seed_session_entry(cache_conn, source_path=s7_file, line_offset=1,
                           timestamp_utc="2026-06-07T00:00:01Z", model=MODEL,
                           msg_id="m7a", req_id="r7a", input_tokens=10,
                           output_tokens=20, cache_create=50000,
                           cache_read=0)               # first prime
        seed_session_entry(cache_conn, source_path=s7_file, line_offset=2,
                           timestamp_utc="2026-06-07T00:00:02Z", model=MODEL,
                           msg_id="m7b", req_id="r7b", input_tokens=10,
                           output_tokens=20, cache_create=1000,
                           cache_read=130000)          # healthy (rm=130000)
        seed_session_entry(cache_conn, source_path=s7_file, line_offset=3,
                           timestamp_utc="2026-06-07T00:00:03Z", model=MODEL,
                           msg_id="m7c", req_id="r7c", input_tokens=10,
                           output_tokens=20, cache_create=134000,
                           cache_read=0)               # MAIN FAILURE
        seed_session_entry(cache_conn, source_path=s7_agent, line_offset=0,
                           timestamp_utc="2026-06-07T00:00:04Z", model=MODEL,
                           msg_id="m7d", req_id="r7d", input_tokens=10,
                           output_tokens=20, cache_create=1000,
                           cache_read=80000)           # subagent prime (rm=80000)
        seed_session_entry(cache_conn, source_path=s7_agent, line_offset=1,
                           timestamp_utc="2026-06-07T00:00:05Z", model=MODEL,
                           msg_id="m7e", req_id="r7e", input_tokens=10,
                           output_tokens=20, cache_create=120000,
                           cache_read=5000)            # SUBAGENT FAILURE

        # --- session s8: the new subagent format (spec §4) -------------------
        # The MAIN turn s8a1 holds FOUR spawn tool_uses in ONE item (Codex P1-C):
        #   toolu_child  -> cc110001  (SYNC, structured result with full totals,
        #                               itself the PARENT of a nested grandchild)
        #   toolu_async  -> a55c0003  (ASYNC launch result, no totals; completes
        #                               via a separate <task-notification>)
        #   toolu_trunc1 -> b0c40004  (truncated-(i): agentId present, <usage>
        #                               clipped -> links + derived totals)
        #   toolu_trunc2 -> (clipped)  (truncated-(ii): agentId itself clipped ->
        #                               NO link, NO subagent_meta, flat card)
        _insert_message(
            cache_conn,
            session_id="s8", uuid="s8h1", parent_uuid=None,
            source_path=s8_main, byte_offset=0,
            timestamp_utc="2026-06-08T00:00:00Z",
            entry_type="human", text="run the nested + async + truncated audits",
            cwd=s8_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn,
            session_id="s8", uuid="s8a1", parent_uuid="s8h1",
            source_path=s8_main, byte_offset=1,
            timestamp_utc="2026-06-08T00:00:01Z",
            entry_type="assistant", text="spawning four subagents",
            blocks_json=json.dumps([
                {"kind": "text", "text": "spawning four subagents"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Sync audit","subagent_type":"code-reviewer"}',
                 "input": {"description": "Sync audit", "subagent_type": "code-reviewer",
                           "prompt": "Audit synchronously"},
                 "id": "toolu_child", "preview": "Sync audit",
                 "subagent_type": "code-reviewer"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Background audit","subagent_type":"Explore"}',
                 "input": {"description": "Background audit", "subagent_type": "Explore",
                           "prompt": "Audit in the background"},
                 "id": "toolu_async", "preview": "Background audit",
                 "subagent_type": "Explore"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Clipped-usage audit","subagent_type":"grounding"}',
                 "input": {"description": "Clipped-usage audit", "subagent_type": "grounding",
                           "prompt": "Audit with a clipped usage tail"},
                 "id": "toolu_trunc1", "preview": "Clipped-usage audit",
                 "subagent_type": "grounding"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Clipped-agentId audit","subagent_type":"grounding"}',
                 "input": {"description": "Clipped-agentId audit", "subagent_type": "grounding",
                           "prompt": "Audit clipped before its agentId line"},
                 "id": "toolu_trunc2", "preview": "Clipped-agentId audit",
                 "subagent_type": "grounding"},
                # #217 S1 / U6: a fifth spawn whose >16 KB result's agentId: trailer
                # landed PAST the 16 KB tool_result clip, but the INGEST stamp
                # recovered the id from the FULL raw (over the clip). Pre-#217 this
                # was the toolu_trunc2 flat-card case; with the ingest stamp it now
                # LINKS via the structured agent_id (the new additive golden delta).
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Stamped-over-cap audit","subagent_type":"grounding"}',
                 "input": {"description": "Stamped-over-cap audit", "subagent_type": "grounding",
                           "prompt": "Audit whose agentId landed past the 16 KB clip"},
                 "id": "toolu_trunc3", "preview": "Stamped-over-cap audit",
                 "subagent_type": "grounding"},
            ]),
            model=MODEL, msg_id="m8", req_id="r8",
            cwd=s8_cwd, git_branch="main",
        )
        # SYNC spawn result for toolu_child -> cc110001 (structured totals, the
        # existing #166 path; unchanged regression). cc110001 is ALSO the parent
        # of the nested grandchild below.
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_child", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=2,
            timestamp_utc="2026-06-08T00:00:02Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "text": "sync audit done",
                "truncated": False, "is_error": False, "tool_use_id": "toolu_child",
                "agent_id": "cc110001",
                "subagent_meta": {"total_tokens": 12000, "total_duration_ms": 7000,
                                  "total_tool_use_count": 3, "status": "completed"}}]),
            cwd=s8_cwd, git_branch="main",
        )
        # ASYNC launch result for toolu_async -> a55c0003: status:"async_launched"
        # and NO totals. Completion arrives below as a <task-notification>.
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_async", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=3,
            timestamp_utc="2026-06-08T00:00:03Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result",
                "text": "Launched background agent a55c0003",
                "truncated": False, "is_error": False, "tool_use_id": "toolu_async",
                "agent_id": "a55c0003",
                "subagent_meta": {"status": "async_launched"}}]),
            cwd=s8_cwd, git_branch="main",
        )
        # Truncated-(i) result for toolu_trunc1 -> b0c40004: STRING-content (no
        # structured agent_id/subagent_meta), agentId PRESENT but the <usage>
        # wrapper CLIPPED past the 16 KB cap -> 1a links the child, 1c derives
        # totals from the b0c40004 thread.
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_trunc1", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=4,
            timestamp_utc="2026-06-08T00:00:04Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "truncated": True,
                "is_error": False, "tool_use_id": "toolu_trunc1",
                "text": ("partial audit output before the usage tail was clipped\n"
                         "agentId: b0c40004 (use SendMessage with to: 'b0c40004' "
                         "to continue this agent)\n<usage>subagent_tokens: 740")}]),
            cwd=s8_cwd, git_branch="main",
        )
        # Truncated-(ii) result for toolu_trunc2: STRING-content clipped BEFORE the
        # agentId: line -> _parse_nested_agent_result returns None -> NO link, NO
        # subagent_meta entry, the spawn degrades to a flat title-only card (no
        # crash, no mis-link).
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_trunc2", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=5,
            timestamp_utc="2026-06-08T00:00:05Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "truncated": True,
                "is_error": False, "tool_use_id": "toolu_trunc2",
                "text": "partial audit output that was clipped mid-stream before"}]),
            cwd=s8_cwd, git_branch="main",
        )
        # #217 S1 / U6: result for toolu_trunc3 -> dd330005. The TEXT is clipped
        # past the 16 KB cap (truncated, NO agentId: trailer in the surviving text
        # — same surface shape as toolu_trunc2's flat-card case), BUT the INGEST
        # stamp recovered the id + usage from the FULL raw and persisted the
        # STRUCTURED agent_id/subagent_meta on the block. So the kernel's
        # b.pop("agent_id") consumer LINKS this grandchild (vs. toolu_trunc2's flat
        # card). This is the new additive golden delta proving the U6 fix: a >16 KB
        # grandchild whose agentId landed past the clip now links via the stamp.
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_trunc3", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=7,
            timestamp_utc="2026-06-08T00:00:07Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "truncated": True,
                "is_error": False, "tool_use_id": "toolu_trunc3",
                "text": "partial audit output clipped before the agentId tail",
                "agent_id": "dd330005",
                "subagent_meta": {"total_tokens": 555, "total_tool_use_count": 2,
                                  "total_duration_ms": 321, "status": "completed"}}]),
            cwd=s8_cwd, git_branch="main",
        )
        # The <task-notification> completing the async subagent. A user line whose
        # body is a <task-notification> wrapper -> Phase 4 classifies it
        # meta_kind="notification"; its <tool-use-id> = the spawn id toolu_async,
        # <status>completed</status>. 1c joins it back to a55c0003.
        _notif_body = (
            "<task-notification><task-id>a55c0003</task-id>"
            "<tool-use-id>toolu_async</tool-use-id>"
            "<status>completed</status>"
            "<summary>Background audit finished</summary>"
            "<result>module clean</result></task-notification>"
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="s8notif", parent_uuid="s8a1",
            source_path=s8_main, byte_offset=6,
            timestamp_utc="2026-06-08T00:00:18Z",
            entry_type="human", text=_notif_body,
            blocks_json=json.dumps([{"kind": "text", "text": _notif_body}]),
            cwd=s8_cwd, git_branch="main",
        )

        # --- child subagent thread (agent-cc110001.jsonl) -------------------
        # It itself SPAWNS a grandchild (toolu_gc). The grandchild's result is
        # STRING-content (no structured agent_id) with agentId + a full <usage>.
        _insert_message(
            cache_conn, session_id="s8", uuid="ch_h", parent_uuid=None,
            source_path=s8_agent_child, byte_offset=0,
            timestamp_utc="2026-06-08T00:00:06Z",
            entry_type="human", text="Sync audit", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="ch_a1", parent_uuid="ch_h",
            source_path=s8_agent_child, byte_offset=1,
            timestamp_utc="2026-06-08T00:00:07Z",
            entry_type="assistant", text="spawning a grounding grandchild",
            blocks_json=json.dumps([
                {"kind": "text", "text": "spawning a grounding grandchild"},
                {"kind": "tool_use", "name": "Agent",
                 "input_summary": '{"description":"Ground claims","subagent_type":"grounding"}',
                 "input": {"description": "Ground claims", "subagent_type": "grounding",
                           "prompt": "Ground the doc claims against code"},
                 "id": "toolu_gc", "preview": "Ground claims",
                 "subagent_type": "grounding"},
            ]),
            model=MODEL, msg_id="m8ch", req_id="r8ch", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )
        # The grandchild spawn result: STRING-content, no structured agent_id, a
        # trailing agentId: line + a FULL <usage> totals wrapper (1a parses
        # authoritative totals 8400/5/9100). Lives on the CHILD file, so its owner
        # (ch_a1) has subagent_key=cc110001 -> grandchild's parent_subagent_key.
        _gc_result = (
            "Grounded all six doc claims against the code; three needed edits.\n"
            "agentId: ace20002 (use SendMessage with to: 'ace20002' to "
            "continue this agent)\n"
            "<usage>subagent_tokens: 8400\ntool_uses: 5\nduration_ms: 9100</usage>"
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="tr_gc", parent_uuid="ch_a1",
            source_path=s8_agent_child, byte_offset=2,
            timestamp_utc="2026-06-08T00:00:08Z",
            entry_type="tool_result",
            blocks_json=json.dumps([{"kind": "tool_result", "text": _gc_result,
                "truncated": False, "is_error": False, "tool_use_id": "toolu_gc"}]),
            is_sidechain=1, cwd=s8_cwd, git_branch="main",
        )

        # --- grandchild thread (agent-ace20002.jsonl): >=1 assistant turn ---
        _insert_message(
            cache_conn, session_id="s8", uuid="gc_h", parent_uuid=None,
            source_path=s8_agent_gchild, byte_offset=0,
            timestamp_utc="2026-06-08T00:00:09Z",
            entry_type="human", text="Ground claims", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="gc_a1", parent_uuid="gc_h",
            source_path=s8_agent_gchild, byte_offset=1,
            timestamp_utc="2026-06-08T00:00:10Z",
            entry_type="assistant", text="three claims needed edits",
            blocks_json='[{"kind": "text", "text": "three claims needed edits"}]',
            model=MODEL, msg_id="m8gc", req_id="r8gc", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )

        # --- async subagent thread (agent-a55c0003.jsonl): 2 turns w/ tokens --
        # Provides the bucket 1c derives totals from (tokens stamped from
        # session_entries; one tool_use -> tool_call for the tool-count). Span
        # 11Z..15Z -> derived total_duration_ms covers the whole bucket (h@11Z).
        _insert_message(
            cache_conn, session_id="s8", uuid="as_h", parent_uuid=None,
            source_path=s8_agent_async, byte_offset=0,
            timestamp_utc="2026-06-08T00:00:11Z",
            entry_type="human", text="Background audit", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="as_a1", parent_uuid="as_h",
            source_path=s8_agent_async, byte_offset=1,
            timestamp_utc="2026-06-08T00:00:12Z",
            entry_type="assistant", text="reading the module",
            blocks_json=json.dumps([
                {"kind": "text", "text": "reading the module"},
                {"kind": "tool_use", "name": "Read",
                 "input_summary": '{"file_path":"/home/u/proj/mod.py"}',
                 "input": {"file_path": "/home/u/proj/mod.py"},
                 "id": "toolu_as_read", "preview": "/home/u/proj/mod.py"},
            ]),
            model=MODEL, msg_id="m8as1", req_id="r8as1", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="as_a2", parent_uuid="as_a1",
            source_path=s8_agent_async, byte_offset=2,
            timestamp_utc="2026-06-08T00:00:15Z",
            entry_type="assistant", text="background audit clean",
            blocks_json='[{"kind": "text", "text": "background audit clean"}]',
            model=MODEL, msg_id="m8as2", req_id="r8as2", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )
        seed_session_entry(cache_conn, source_path=s8_agent_async, line_offset=1,
                           timestamp_utc="2026-06-08T00:00:12Z", model=MODEL,
                           msg_id="m8as1", req_id="r8as1",
                           input_tokens=1000, output_tokens=300)
        seed_session_entry(cache_conn, source_path=s8_agent_async, line_offset=2,
                           timestamp_utc="2026-06-08T00:00:15Z", model=MODEL,
                           msg_id="m8as2", req_id="r8as2",
                           input_tokens=800, output_tokens=200)

        # --- truncated-(i) child thread (agent-b0c40004.jsonl): 2 turns -------
        # 1a links b0c40004 with NO totals (<usage> clipped); 1c derives them
        # here. Span 31Z..33Z; one tool_use -> tool_call for the tool-count.
        _insert_message(
            cache_conn, session_id="s8", uuid="tr4_h", parent_uuid=None,
            source_path=s8_agent_trunc, byte_offset=0,
            timestamp_utc="2026-06-08T00:00:30Z",
            entry_type="human", text="Clipped-usage audit", is_sidechain=1,
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="tr4_a1", parent_uuid="tr4_h",
            source_path=s8_agent_trunc, byte_offset=1,
            timestamp_utc="2026-06-08T00:00:31Z",
            entry_type="assistant", text="grepping the tree",
            blocks_json=json.dumps([
                {"kind": "text", "text": "grepping the tree"},
                {"kind": "tool_use", "name": "Grep",
                 "input_summary": '{"pattern":"reset"}',
                 "input": {"pattern": "reset"},
                 "id": "toolu_tr4_grep", "preview": "reset"},
            ]),
            model=MODEL, msg_id="m8t41", req_id="r8t41", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )
        _insert_message(
            cache_conn, session_id="s8", uuid="tr4_a2", parent_uuid="tr4_a1",
            source_path=s8_agent_trunc, byte_offset=2,
            timestamp_utc="2026-06-08T00:00:33Z",
            entry_type="assistant", text="clipped-usage audit clean",
            blocks_json='[{"kind": "text", "text": "clipped-usage audit clean"}]',
            model=MODEL, msg_id="m8t42", req_id="r8t42", is_sidechain=1,
            cwd=s8_cwd, git_branch="main",
        )
        seed_session_entry(cache_conn, source_path=s8_agent_trunc, line_offset=1,
                           timestamp_utc="2026-06-08T00:00:31Z", model=MODEL,
                           msg_id="m8t41", req_id="r8t41",
                           input_tokens=500, output_tokens=100)
        seed_session_entry(cache_conn, source_path=s8_agent_trunc, line_offset=2,
                           timestamp_utc="2026-06-08T00:00:33Z", model=MODEL,
                           msg_id="m8t42", req_id="r8t42",
                           input_tokens=200, output_tokens=50)
        # session_entries for the main turn + the child/grandchild turns so the
        # reader's header cost is non-zero (cost-once contract still holds).
        seed_session_entry(cache_conn, source_path=s8_main, line_offset=1,
                           timestamp_utc="2026-06-08T00:00:01Z", model=MODEL,
                           msg_id="m8", req_id="r8",
                           input_tokens=600, output_tokens=250)
        seed_session_entry(cache_conn, source_path=s8_agent_child, line_offset=1,
                           timestamp_utc="2026-06-08T00:00:07Z", model=MODEL,
                           msg_id="m8ch", req_id="r8ch",
                           input_tokens=400, output_tokens=120)
        seed_session_entry(cache_conn, source_path=s8_agent_gchild, line_offset=1,
                           timestamp_utc="2026-06-08T00:00:10Z", model=MODEL,
                           msg_id="m8gc", req_id="r8gc",
                           input_tokens=300, output_tokens=90)

        # --- #193: ai-title rows. s1 + s3 carry an AI-generated title (their
        # rail/reader title flips from the first human prompt to the ai-title);
        # s2/s4/s5/s6 deliberately carry NO ai-title, locking the first-prompt /
        # marker-skip / project-label fallback path in the goldens. The trailing
        # null + blank rewrites Claude Code emits are dropped at parse time
        # (covered by the parser/ingest pytest), so only the surviving non-null
        # title is stored here — last-non-null-write-wins.
        for _sid, _title, _src, _off in (
            ("s1", "Explain the weekly reset window", s1_file_a, 4),
            ("s3", "Audit modules A, B, and C", s3_main, 1),
        ):
            cache_conn.execute(
                "INSERT INTO conversation_ai_titles"
                "(session_id, ai_title, source_path, byte_offset) VALUES (?,?,?,?)",
                (_sid, _title, _src, _off),
            )

        # Populate the browse-rail rollup from the seeded conversation_messages
        # (full recompute; the backfill flag is NOT armed here, so the rail read
        # treats this rollup as authoritative and exercises the FAST path). Runs
        # BEFORE the commit/close so it persists; the create_cache_db atexit hook
        # zeros the SQLite writer-version bytes AFTER this, keeping the committed
        # fixture byte-stable.
        _recompute_conversation_sessions(cache_conn)

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
