#!/usr/bin/env python3
"""Deterministic synthetic transcript generator for the #281 S3 Playwright
reader smoke net.

Writes seeded synthetic ``*.jsonl`` session files under a scratch Claude root
plus a ``manifest.json`` the e2e suite reads (session ids, the jump-target /
sidechain-anchor uuids, the live-tail file path + append template). It does NOT
build ``cache.db`` — the launcher (``dashboard/web/e2e/serve.sh``) pre-primes the
cache via the production ``./bin/cctally cache-sync --source claude`` path under
the full isolation env, so the shape under test is the real ingest, not a
hand-forged cache.

Determinism: static past timestamps derived from a fixed reference epoch (never
wall-clock) + a fixed ``random.Random(281)`` seed, so every build is byte-stable.

The row shape mirrors exactly what ``bin/_lib_conversation.py::parse_message_row``
reads (top-level ``type``/``uuid``/``parentUuid``/``sessionId``/``timestamp``/
``cwd`` + nested ``message.id``/``requestId``/``message.model``/``message.usage``/
``message.content``) — the cost-only streaming-pair shape is insufficient for
``conversation_messages`` (memory-documented).

ISOLATION: this generator is pure file I/O — it never imports ``cctally``, never
reads/writes the operator's real ``~/.claude`` / ``~/.codex`` /
``~/.local/share/cctally``. It writes ONLY under the ``--out`` runtime dir.

Sizing constants are derived from HEAD product code, NOT guessed (memory-cited
line numbers drift — re-verified 2026-07-10):
  * PAGE = 500 — the reader page size. Client hook ``useConversation.ts`` fetches
    ``?limit=500``; the server default is ``get_conversation(..., limit=500)`` and
    the handler ``_qs_int(q, "limit", 500)``. So a "≥3 tail-pages" long
    conversation needs > 3 * 500 = 1500 rendered items.
  * SUBAGENT_WINDOW_CAP = 150 — ``dashboard/web/src/conversations/subagentWindow.ts``.
    A subagent thread renders every member when itemCount <= 150 (NO reveal bar);
    the "Show N earlier / Show all" reveal affordance only appears above 150. So
    the reveal-grow (scenario 7) sidechain needs > 150 members — the plan's "~40"
    predates the #239 windowing and would render no reveal control.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import random

# ── HEAD-derived sizing (see module docstring) ──────────────────────────────
PAGE = 500                 # useConversation.ts / get_conversation default
SUBAGENT_WINDOW_CAP = 150  # subagentWindow.ts

# The long conversation must exceed 3 tail-pages so a tail open lands
# multi-page (has_prev true) and reverse paging has room to prepend.
LONG_ITEMS = 1600                 # > 3 * PAGE
JUMP_TARGET_INDEX = 40            # an early USER turn, far above the tail window
# Giant rows live BELOW the first reverse-prepend page (which brings in items
# ~600..1099 before the 1100..1599 tail) so scenario 2's anchor pin sees a UNIFORM
# page (clean ±2px), while still sitting between the jump target (40) and the tail
# so scenario 4's walk-to-target traverses real height-estimation drift.
GIANT_START, GIANT_END = 60, 560
GIANT_COUNT = 20                  # varied multi-KB rows → real height-est. drift

SIDECHAIN_MEMBERS = 260           # > SUBAGENT_WINDOW_CAP so a reveal bar renders
SIDECHAIN_MAIN_TURNS = 12         # a few main turns before/after the sidechain
# Scenario 7's above-insertion reveal guard (#281 S5 B2). A find-jump to a late
# sidechain member centers the internal window on it: centeredWindow(260, 198, 150)
# clamps win.start to 110, so members 110..259 render and a "Show 100 earlier"
# control appears above them. Revealing it inserts members 10..109 (100 members)
# ABOVE the held anchor (member 110) — the exact above-insertion geometry the #239
# convergent reassert must pin. Member 198 is EVEN (a user turn → no rng call), so
# injecting the needle keeps every OTHER row byte-stable (the rng stream is
# untouched — only assistant rows consume it).
REVEAL_LATE_MEMBER_INDEX = 198
REVEAL_LATE_NEEDLE = "zzqrevlate198"
LIVE_ITEMS = 40                   # tall enough to scroll up, < PAGE (live-tail on)
# A single page (< PAGE, so has_prev=false → opens 'top') but tall enough to
# actually scroll — so the "parked at top" assertion is non-vacuous AND flips RED
# under the invert-has_prev lever (which would open it at the bottom instead).
SINGLE_ITEMS = 30

MODEL = "claude-opus-4-8"         # a real CLAUDE_MODEL_PRICING key (non-zero cost)
CWD = "/e2e/reader"
PROJECT_DIR = "-e2e-reader"       # cwd '/'→'-' encoding (matches bench builder)

# Unique needle stamped into the jump target's prose so the find bar (and the
# outline) can locate it deterministically (scenarios 4 + 8).
JUMP_TARGET_NEEDLE = "zzqjumptarget781"

# #287 B4 — a SECOND jump target at a PINNED even user index just after the giant
# band (60–560) but below 600 (Codex F11): far enough from the tail that the
# reverse-drain approach loads + MEASURES the giant rows above it (real
# height-estimation drift over the walk path), yet not a giant itself. A target
# near the tail window would load giant-free and prove nothing.
BELOW_GIANTS_JUMP_INDEX = 580
BELOW_GIANTS_NEEDLE = "zzqbelowgiants580"

_REF_EPOCH = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)


def _ts(step: int) -> str:
    """A static UTC timestamp `step` seconds past the reference epoch."""
    return (_REF_EPOCH + dt.timedelta(seconds=step)).isoformat().replace("+00:00", "Z")


def _uuid(prefix: str, i: int) -> str:
    """A deterministic UUID-shaped id (stable across builds)."""
    return f"e2e{prefix}-0000-0000-0000-{i:012d}"


def _usage(rng: random.Random) -> dict:
    return {
        "input_tokens": rng.randint(50, 800),
        "output_tokens": rng.randint(100, 3000),
        "cache_read_input_tokens": rng.randint(0, 20000),
        "cache_creation_input_tokens": rng.randint(0, 3000),
    }


def _user_row(uuid, parent, sid, step, text, *, is_sidechain=False):
    row = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": _ts(step),
        "cwd": CWD,
        "message": {"role": "user", "content": text},
    }
    if is_sidechain:
        row["isSidechain"] = True
    return row


def _asst_row(uuid, parent, sid, step, text, rng, *, is_sidechain=False, model=MODEL):
    row = {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": _ts(step),
        "cwd": CWD,
        "requestId": f"req_{uuid}",
        "message": {
            "id": f"msg_{uuid}",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": _usage(rng),
        },
    }
    if is_sidechain:
        row["isSidechain"] = True
    return row


def _giant_body(rng: random.Random, i: int) -> str:
    """A varied 2–20KB markdown/code body so react-virtuoso's size model has real
    high-variance rows to drift over (scenario 4's walk requirement)."""
    kb = rng.randint(2, 20)
    words = ["widget", "kernel", "cursor", "prepend", "anchor", "virtuoso",
             "reader", "scroll", "window", "ingest", "cache", "sidechain",
             "measure", "settle", "viewport", "estimate", "mount", "recycle"]
    lines = [f"## Giant assistant turn {i} (~{kb}KB)", ""]
    target = kb * 1024
    body_len = 0
    n = 0
    while body_len < target:
        if n % 9 == 0:
            block = "```python\n" + "\n".join(
                f"def step_{n}_{k}(x):  # {' '.join(rng.choice(words) for _ in range(6))}"
                for k in range(6)) + "\n```"
        else:
            block = " ".join(rng.choice(words) for _ in range(rng.randint(20, 40))) + "."
        lines.append(block)
        body_len += len(block) + 1
        n += 1
    return "\n".join(lines)


def _write(path: pathlib.Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def build_long(projects: pathlib.Path, rng: random.Random) -> dict:
    """A >3-page conversation with an early jump target and interleaved giant
    rows between the target and the tail window."""
    sid = "e2e-long"
    giant_indices = {
        GIANT_START + round(k * (GIANT_END - GIANT_START) / (GIANT_COUNT - 1))
        for k in range(GIANT_COUNT)
    }
    rows = []
    prev = None
    jump_target_uuid = None
    below_giants_uuid = None
    for i in range(LONG_ITEMS):
        is_user = (i % 2 == 0)
        uuid = _uuid(f"long{'u' if is_user else 'a'}", i)
        step = i * 10
        if is_user:
            if i == JUMP_TARGET_INDEX:
                text = (f"User prompt {i}: {JUMP_TARGET_NEEDLE} please review "
                        f"the reader anchor stability for turn {i}.")
                jump_target_uuid = uuid
            elif i == BELOW_GIANTS_JUMP_INDEX:
                text = (f"User prompt {i}: {BELOW_GIANTS_NEEDLE} verify the walk "
                        f"lands past the giant band for turn {i}.")
                below_giants_uuid = uuid
            else:
                text = f"User prompt {i}: continue the analysis of item {i}."
            rows.append(_user_row(uuid, prev, sid, step, text))
        else:
            if i in giant_indices:
                text = _giant_body(rng, i)
            else:
                text = f"Assistant reply {i}: acknowledged item {i}, proceeding."
            rows.append(_asst_row(uuid, prev, sid, step, text, rng))
        prev = uuid
    _write(projects / PROJECT_DIR / f"{sid}.jsonl", rows)
    return {
        "long_session_id": sid,
        "long_turn_count": LONG_ITEMS,
        "long_last_uuid": prev,
        "jump_target_uuid": jump_target_uuid,
        "jump_target_needle": JUMP_TARGET_NEEDLE,
        "below_giants_jump_target_uuid": below_giants_uuid,
        "below_giants_jump_target_index": BELOW_GIANTS_JUMP_INDEX,
        "below_giants_jump_target_needle": BELOW_GIANTS_NEEDLE,
    }


def build_single(projects: pathlib.Path, rng: random.Random) -> dict:
    sid = "e2e-single"
    rows = []
    prev = None
    first_uuid = None
    for i in range(SINGLE_ITEMS):
        is_user = (i % 2 == 0)
        uuid = _uuid(f"single{'u' if is_user else 'a'}", i)
        if first_uuid is None:
            first_uuid = uuid  # the opening (top) turn — scenario 1's top-open check
        step = i * 10
        # a few taller bodies so the single page overflows the viewport
        tall = "\n".join(f"single line {i}.{k} of a taller turn" for k in range(6)) \
            if (i % 4 == 0) else None
        if is_user:
            rows.append(_user_row(uuid, prev, sid, step,
                                  tall or f"Single-page prompt {i}."))
        else:
            rows.append(_asst_row(uuid, prev, sid, step,
                                  tall or f"Single-page reply {i}.", rng))
        prev = uuid
    _write(projects / PROJECT_DIR / f"{sid}.jsonl", rows)
    return {"single_page_session_id": sid, "single_first_uuid": first_uuid,
            "single_last_uuid": prev}


def build_sidechain(projects: pathlib.Path, rng: random.Random) -> dict:
    """A main file with a mid-conversation anchor turn, plus a SEPARATE
    ``agent-<key>.jsonl`` sidechain file (> SUBAGENT_WINDOW_CAP members) whose
    root row's parentUuid resolves cross-file to the main anchor uuid — so the
    reader nests it as a collapsible subagent thread with a reveal bar."""
    sid = "e2e-sidechain"
    subagent_key = "e2eside"
    # ── main thread ──────────────────────────────────────────────────────────
    main_rows = []
    prev = None
    anchor_uuid = None
    for i in range(SIDECHAIN_MAIN_TURNS):
        is_user = (i % 2 == 0)
        uuid = _uuid(f"scmain{'u' if is_user else 'a'}", i)
        step = i * 10
        if is_user:
            main_rows.append(_user_row(uuid, prev, sid, step,
                                       f"Main prompt {i} spawning a subagent."))
        else:
            main_rows.append(_asst_row(uuid, prev, sid, step,
                                       f"Main reply {i} (Task launched).", rng))
            if anchor_uuid is None and i == 5:
                anchor_uuid = uuid  # the assistant turn that spawned the subagent
        prev = uuid
    _write(projects / PROJECT_DIR / f"{sid}.jsonl", main_rows)
    # ── subagent thread (separate agent-*.jsonl, isSidechain) ────────────────
    agent_rows = []
    sc_prev = None
    base_step = 10_000
    reveal_late_uuid = None
    for j in range(SIDECHAIN_MEMBERS):
        is_user = (j % 2 == 0)
        uuid = _uuid(f"scagent{'u' if is_user else 'a'}", j)
        # The bucket ROOT (j == 0) parents cross-file to the main anchor so the
        # group nests under it; every later member parents within the thread.
        parent = anchor_uuid if j == 0 else sc_prev
        step = base_step + j * 5
        # Moderate multi-line bodies (NOT multi-KB giants): the reveal must grow
        # the card enough to force a Virtuoso re-anchor the reassert corrects, but
        # stay light enough that the ~150-member re-measure converges inside the
        # reveal reassert's 800ms budget (giants blow past it → a real fling).
        body = "\n".join(
            f"Subagent step {j} line {k}: exploring candidate {j} in detail."
            for k in range(4))
        # Scenario 7's above-insertion reveal target: a unique needle at the exact
        # late member the find bar jumps to (member 198), so the find count is 1/1
        # and the jump centers the window at start 110 (see REVEAL_LATE_* above).
        if j == REVEAL_LATE_MEMBER_INDEX:
            reveal_late_uuid = uuid
            body = f"{body}\nSubagent needle {REVEAL_LATE_NEEDLE} pinned at member {j}."
        if is_user:
            agent_rows.append(_user_row(uuid, parent, sid, step, body,
                                        is_sidechain=True))
        else:
            agent_rows.append(_asst_row(uuid, parent, sid, step, body, rng,
                                        is_sidechain=True))
        sc_prev = uuid
    _write(projects / PROJECT_DIR / f"agent-{subagent_key}.jsonl", agent_rows)
    return {
        "sidechain_session_id": sid,
        "sidechain_anchor_uuid": anchor_uuid,
        "sidechain_subagent_key": subagent_key,
        "sidechain_member_count": SIDECHAIN_MEMBERS,
        "reveal_late_member_uuid": reveal_late_uuid,
        "reveal_late_member_index": REVEAL_LATE_MEMBER_INDEX,
        "reveal_late_needle": REVEAL_LATE_NEEDLE,
    }


def build_live(projects: pathlib.Path, rng: random.Random) -> dict:
    """A single-page conversation (< PAGE items, so live-tail is active) tall
    enough to scroll up. The suite appends turns to this file at runtime."""
    sid = "e2e-live"
    rows = []
    prev = None
    last_uuid = None
    for i in range(LIVE_ITEMS):
        is_user = (i % 2 == 0)
        uuid = _uuid(f"live{'u' if is_user else 'a'}", i)
        step = i * 10
        # a few taller bodies so the conversation overflows the viewport
        tall = "\n".join(f"line {i}.{k} of a taller live turn" for k in range(6)) \
            if (i % 5 == 0) else None
        if is_user:
            rows.append(_user_row(uuid, prev, sid, step,
                                  tall or f"Live prompt {i}."))
        else:
            rows.append(_asst_row(uuid, prev, sid, step,
                                  tall or f"Live reply {i}.", rng))
        prev = uuid
        last_uuid = uuid
    live_path = projects / PROJECT_DIR / f"{sid}.jsonl"
    _write(live_path, rows)
    # An append template: a valid assistant turn with placeholders the suite's
    # appendLiveTurn() substitutes (__UUID__ / __TS__ / __PARENT__). Timestamps
    # past the seeded corpus keep document order stable.
    template = {
        "type": "assistant",
        "uuid": "__UUID__",
        "parentUuid": "__PARENT__",
        "sessionId": sid,
        "timestamp": "__TS__",
        "cwd": CWD,
        "requestId": "req___UUID__",
        "message": {
            "id": "msg___UUID__",
            "role": "assistant",
            "model": MODEL,
            "content": [{"type": "text", "text": "Appended live turn __UUID__."}],
            "usage": {"input_tokens": 120, "output_tokens": 400,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        },
    }
    return {
        "live_session_id": sid,
        "live_jsonl_path": str(live_path.resolve()),
        "live_last_uuid": last_uuid,
        "live_append_template": json.dumps(template),
    }


SECOND_MODEL = "claude-sonnet-4-5"   # a real CLAUDE_MODEL_PRICING key, DISTINCT
                                     # family from MODEL (opus) so the served
                                     # Sessions table is mixed-model.


def build_second_model(projects: pathlib.Path, rng: random.Random) -> dict:
    """A tiny SECOND-model session (claude-sonnet-4-5) so the served Sessions
    table renders mixed-model — i.e. the Model column is present, exercising
    #293 S2 SESS-1's 7-column tight case at span-6 (1440). Structurally
    minimal (4 turns); its only job is to make the client's
    singleModelLabel(rows) predicate return null (a second distinct real model
    across the session set). It is inert to the reader scenario specs (they
    open sessions by explicit manifest id); it only bumps the conversations-
    rail fixture count from 4 to 5 (asserted in smoke.spec.ts)."""
    sid = "e2e-sonnet"
    rows = []
    prev = None
    last_uuid = None
    for i in range(4):
        is_user = (i % 2 == 0)
        uuid = _uuid(f"sonnet{'u' if is_user else 'a'}", i)
        step = 800_000 + i * 10
        if is_user:
            rows.append(_user_row(uuid, prev, sid, step,
                                  f"Second-model prompt {i} for the mixed-model Sessions column."))
        else:
            rows.append(_asst_row(uuid, prev, sid, step,
                                  f"Second-model reply {i}.", rng, model=SECOND_MODEL))
        prev = uuid
        last_uuid = uuid
    _write(projects / PROJECT_DIR / f"{sid}.jsonl", rows)
    return {
        "second_model_session_id": sid,
        "second_model": SECOND_MODEL,
        "second_model_last_uuid": last_uuid,
    }


def build_codex_task_a(out: pathlib.Path) -> dict:
    """Build #329 Task A's native-cycle and partial-project browser inputs.

    The complete root fixtures keep their conversation content/identity but use
    the same basename under distinct synthetic parents. A token-only UUID file
    contributes a prior exact seven-day cycle and one metadata-incomplete
    accounting row without creating an extra conversation.
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    source_dir = repo / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
    target = out / "codex-task-a"
    target.mkdir(parents=True, exist_ok=True)

    def records(name: str) -> list[dict]:
        return [json.loads(line) for line in (source_dir / name).read_text().splitlines() if line]

    def normalize_native_limits(value):
        if isinstance(value, dict):
            for key, item in tuple(value.items()):
                if key == "window_minutes" and item == 10_020:
                    value[key] = 10_080
                elif key == "window_minutes" and item == 330:
                    value[key] = 300
                else:
                    normalize_native_limits(item)
        elif isinstance(value, list):
            for item in value:
                normalize_native_limits(item)

    current_files: dict[str, str] = {}
    for scenario, parent in (("root-a-collision", "workspace"), ("root-b-collision", "personal")):
        values = records(f"{scenario}.jsonl")
        for value in values:
            normalize_native_limits(value)
            if value.get("type") == "session_meta":
                value["payload"]["cwd"] = f"/synthetic/{parent}/repo"
        path = target / f"{scenario}.jsonl"
        path.write_text("".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in values))
        current_files[scenario] = str(path.resolve())

    modern = records("modern-full.jsonl")
    token = next(value for value in modern if value.get("payload", {}).get("type") == "token_count")
    token["timestamp"] = "2026-07-07T12:02:00Z"
    normalize_native_limits(token)
    limits = token["payload"]["info"]["rate_limits"]
    limits["secondary"]["resets_at"] = 1_784_030_400  # 2026-07-14T12:00:00Z
    prior = [
        {
            "timestamp": "2026-07-07T12:01:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-synthetic-codex", "turn_id": "issue-329-prior-cycle"},
        },
        token,
    ]
    prior_path = target / "rollout-2026-07-07T12-00-00-32900000-0000-4000-8000-000000000001.jsonl"
    prior_path.write_text("".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in prior))

    return {
        "codex_task_a_current_files": current_files,
        "codex_task_a_prior_file": str(prior_path.resolve()),
    }


def build_codex_task_b(out: pathlib.Path) -> dict:
    """Build #331 Task B's browser-only shell and patch-card corpus.

    Task A's checked-in wire fixture remains the canonical decoder contract.
    The browser copy adds only a deterministic long terminal result so the
    client's collapsed-output behavior is exercised without widening Task A's
    backend-owned fixture.
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    source = repo / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts" / "session-b-card-wire.jsonl"
    target = out / "codex-task-b"
    target.mkdir(parents=True, exist_ok=True)
    values = [json.loads(line) for line in source.read_text().splitlines() if line]
    long_output = "".join(f"synthetic long output line {line:02d}\n" for line in range(1, 26))
    values.extend([
        {
            "payload": {
                "call_id": "exec-long",
                "input": "const r = await tools.exec_command({\n  cmd: \"seq 1 25\",\n  workdir: \"/synthetic/root-a/project-red\"\n});\ntext(r.output);",
                "name": "exec",
                "status": "completed",
                "type": "custom_tool_call",
            },
            "timestamp": "2026-07-21T11:00:26Z",
            "type": "response_item",
        },
        {
            "payload": {
                "call_id": "exec-long",
                "output": [
                    {"text": "Script completed\nWall time 0.1 seconds\n\nOutput:", "type": "input_text"},
                    {"text": long_output, "type": "input_text"},
                ],
                "type": "custom_tool_call_output",
            },
            "timestamp": "2026-07-21T11:00:27Z",
            "type": "response_item",
        },
    ])
    path = target / "session-b-card-wire.jsonl"
    path.write_text("".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in values))
    claude_rows = [
        {
            "type": "user", "uuid": "331-ref-user", "parentUuid": None,
            "sessionId": "331-claude-reference", "timestamp": "2026-07-21T10:00:00Z",
            "cwd": "/synthetic/claude/reference",
            "message": {"role": "user", "content": "Synthetic Claude terminal and edit reference"},
        },
        {
            "type": "assistant", "uuid": "331-ref-bash", "parentUuid": "331-ref-user",
            "sessionId": "331-claude-reference", "timestamp": "2026-07-21T10:00:01Z",
            "cwd": "/synthetic/claude/reference", "requestId": "req-331-bash",
            "message": {
                "id": "msg-331-bash", "role": "assistant", "model": MODEL,
                "content": [{"type": "tool_use", "id": "toolu-331-bash", "name": "Bash", "input": {"command": "printf 'alpha\\n'", "description": "Print synthetic output"}}],
                "usage": {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            },
        },
        {
            "type": "user", "uuid": "331-ref-bash-result", "parentUuid": "331-ref-bash",
            "sessionId": "331-claude-reference", "timestamp": "2026-07-21T10:00:02Z",
            "cwd": "/synthetic/claude/reference", "toolUseResult": {"stdout": "alpha\n", "stderr": "", "interrupted": False},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu-331-bash", "content": [{"type": "text", "text": "alpha\n"}], "is_error": False}]},
        },
        {
            "type": "assistant", "uuid": "331-ref-edit", "parentUuid": "331-ref-bash-result",
            "sessionId": "331-claude-reference", "timestamp": "2026-07-21T10:00:03Z",
            "cwd": "/synthetic/claude/reference", "requestId": "req-331-edit",
            "message": {
                "id": "msg-331-edit", "role": "assistant", "model": MODEL,
                "content": [{"type": "tool_use", "id": "toolu-331-edit", "name": "Edit", "input": {"file_path": "synthetic-edit.txt", "old_string": "old\n", "new_string": "new\n"}}],
                "usage": {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            },
        },
        {
            "type": "user", "uuid": "331-ref-edit-result", "parentUuid": "331-ref-edit",
            "sessionId": "331-claude-reference", "timestamp": "2026-07-21T10:00:04Z",
            "cwd": "/synthetic/claude/reference",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu-331-edit", "content": "Updated synthetic-edit.txt", "is_error": False}]},
        },
    ]
    claude_path = target / "claude-card-reference.jsonl"
    claude_path.write_text("".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in claude_rows))
    return {
        "codex_task_b_file": str(path.resolve()),
        "claude_task_b_file": str(claude_path.resolve()),
    }


def build(out: pathlib.Path) -> dict:
    scratch = out / "scratch"
    data = scratch / "data"
    claude = scratch / "claude"
    codex = scratch / "codex"
    projects = claude / "projects"
    for d in (data, projects, codex):
        d.mkdir(parents=True, exist_ok=True)

    rng = random.Random(281)
    manifest: dict = {}
    manifest.update(build_long(projects, rng))
    manifest.update(build_single(projects, rng))
    manifest.update(build_sidechain(projects, rng))
    manifest.update(build_live(projects, rng))
    manifest.update(build_second_model(projects, rng))
    manifest.update(build_codex_task_a(out))
    manifest.update(build_codex_task_b(out))
    manifest["project_dir"] = PROJECT_DIR
    manifest["cwd"] = CWD
    manifest["page_size"] = PAGE

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True,
                    help="runtime dir; writes <out>/scratch/** + <out>/manifest.json")
    args = ap.parse_args()
    out = pathlib.Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = build(out)
    print(f"e2e fixtures written to {out}")
    print(f"  long={manifest['long_turn_count']} items, "
          f"jump_target={manifest['jump_target_uuid']}")
    print(f"  sidechain={manifest['sidechain_member_count']} members "
          f"(key={manifest['sidechain_subagent_key']})")
    print(f"  live={manifest['live_session_id']} at {manifest['live_jsonl_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
