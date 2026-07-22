#!/usr/bin/env python3
"""Generate the synthetic #294 S0 Codex-parity contract corpus."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from _lib_source_identity import (
    canonical_identity,
    canonical_identity_from_root_key,
    source_root_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
SCHEMA_VERSION = 1
CORPUS_ID = "cctally-codex-parity"
SOURCES = ("claude", "codex")
CAPABILITY_STATES = ("supported", "derived", "unavailable", "deferred", "not_applicable")
ADDITIVE_MEASURES = ("usdCost", "compatibleTokenTotals", "explicitCombinedUsdBudget")
NON_ADDITIVE_MEASURES = ("quotaUsedPercent", "quotaReset", "quotaWindow", "dollarsPerPercent", "percentMilestones")
REQUIRED_SCENARIOS = (
    "modern-full", "modern-quota-payload", "modern-no-quota", "modern-partial-quota",
    "modern-dual-location-conflict", "quota-kernel-history",
    "legacy-envelope", "unknown-records", "malformed-tail", "duplicate-token-count",
    "metadata-only-tail", "root-a-collision", "root-b-collision", "nested-parent",
    "nested-child", "claude-collision", "secret-canary", "empty-source", "stale-cache",
    "claude-only", "codex-only", "mixed-source",
    # #294 S6 conversation-normalization corpus extensions.
    "mirror-pairing", "unturned-event-prose", "title-wrapper-window",
    # #330 Task A canonical turn/injected-content contract.
    "session-a-turn-contract",
    # #331 Task A Codex exec/output/patch card-ready wire contract.
    "session-b-card-wire",
    # #332 Task A Codex secondary-tool/orchestration wire family.
    "session-c-secondary-tools", "session-c-child-proven",
    "session-c-child-ambiguous-a", "session-c-child-ambiguous-b",
    # #334 Task A Codex reasoning/lifecycle/harness-marker wire contract.
    "session-d-reasoning-lifecycle-markers",
    # #335 Session E retained native-family truth/privacy disposition.
    "session-e-native-families",
)

SHARED_ID = "11111111-1111-4111-8111-111111111111"
ROOT_A = "/synthetic/root-a/project-red"
ROOT_B = "/synthetic/root-b/project-blue"
MODEL = "gpt-synthetic-codex"

# #294 S6: distinct native thread/session ids for the conversation-normalization
# scenarios so each ingests as its own conversation (no accidental collision with
# the SHARED_ID identity scenarios). Kept UUID-shaped for realism.
MIRROR_SESSION = "22222222-2222-4222-8222-222222222222"
UNTURNED_SESSION = "33333333-3333-4333-8333-333333333333"
TITLE_SESSION = "44444444-4444-4444-8444-444444444444"
SESSION_A_THREAD = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_B_THREAD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_C_PARENT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SESSION_C_CHILD = "c1111111-1111-4111-8111-111111111111"
SESSION_C_AMBIG_A = "c2222222-2222-4222-8222-222222222222"
SESSION_C_AMBIG_B = "c3333333-3333-4333-8333-333333333333"
SESSION_D_THREAD = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SESSION_E_THREAD = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
# #294 S7 F1: the secret-canary scenario now carries a real turned conversation
# (its own thread/session id) so a route-level export can resolve a v1 key and
# prove the qualified anon plan scrubs the documented secret patterns end-to-end.
SECRET_SESSION = "55555555-5555-4555-8555-555555555555"
# The Claude-side seed reuses SHARED_ID as its sessionId so the collision proof
# can show Codex/Claude assemblies share ZERO rows on content, not just key
# inequality. A known-priced model keeps the sync-time cost pass warning-free.
CLAUDE_SEED_MODEL = "claude-opus-4-8"
CLAUDE_THINKING_SESSION = "334-claude-thinking-reference"
# Length of the shared prefix for the "distinct over-cap texts sharing a capped
# prefix" pairing case (spec §5.3). MUST be >= the kernel's display text cap
# (bin/_lib_codex_conversation.CODEX_TEXT_CAP) so the two rows' capped ``text``
# columns collide byte-for-byte while their full-text digests differ — proving
# pairing keys on the pre-cap digest, never on capped text. Equality of this
# bound with the kernel cap is pinned in tests/test_codex_conversation_normalization.py.
_OVERCAP_PREFIX_LEN = 16000


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict], malformed_tail: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(_canonical_json(record) + "\n" for record in records)
    path.write_text(text + (malformed_tail or ""), encoding="utf-8")


def _session_meta(*, root: str = ROOT_A, source: object = "codex", session_id: str = SHARED_ID,
                  record_id: str = "root-thread-a", thread_source: str = "root-thread-a",
                  forked_from_id: str = "root-thread-a") -> dict:
    return {"timestamp": "2026-07-14T12:00:00Z", "type": "session_meta", "payload": {
        "id": record_id, "session_id": session_id, "cwd": root,
        "git": {"branch": "fixture-branch", "repository": "fixture-repository"},
        "source": source, "thread_source": thread_source, "forked_from_id": forked_from_id,
        "model_provider": "fixture-provider", "model": MODEL, "context_window": 272000,
        "model_context_window": 272000, "user": "fixture-user", "instructions": "synthetic instructions",
        "tools": [{"name": "fixture-tool"}],
    }}


def _rate_limits(*, malformed: bool = False) -> dict:
    secondary: object = {"used_percent": 42.0, "window_minutes": 10020, "resets_at": 1784635200}
    if malformed:
        secondary = {"used_percent": "unknown", "window_minutes": 10020}
    return {"primary": {"used_percent": 12.5, "window_minutes": 330, "resets_at": 1784048400},
            "secondary": secondary, "credits": None, "plan_type": "synthetic-plan",
            "limit_id": "synthetic-limit", "limit_name": "Synthetic limit", "individual_limit": None,
            "rate_limit_reached_type": None}


def _token_event(*, timestamp: str = "2026-07-14T12:02:00Z", total: int = 1600,
                 include_quota: bool = True, quota_at_payload: bool = False, malformed: bool = False) -> dict:
    info = {"last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 300,
                                  "output_tokens": 400, "reasoning_output_tokens": 100, "total_tokens": total},
            "total_token_usage": {"total_tokens": total}, "model_context_window": 272000,
            "future_info": {"preserve_or_ignore": True}}
    payload = {"type": "token_count", "info": info}
    if include_quota:
        if quota_at_payload:
            payload["rate_limits"] = _rate_limits(malformed=malformed)
        else:
            info["rate_limits"] = _rate_limits(malformed=malformed)
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _dual_location_quota_event() -> dict:
    """Direct quota fields win independently over valid ``info`` fallbacks."""
    info_limits = _rate_limits()
    info_limits["limit_id"] = "conflict-info-limit"
    info_limits["limit_name"] = "Conflict info limit"
    info_limits["plan_type"] = "conflict-info-plan"
    direct_limits = {
        "primary": {"used_percent": 77.0, "window_minutes": 440, "resets_at": 1784048400},
        "secondary": {"used_percent": "invalid", "window_minutes": "invalid", "resets_at": None},
        "plan_type": None,
        "limit_id": 123,
        "limit_name": "",
        "individual_limit": None,
        "rate_limit_reached_type": None,
    }
    event = _token_event(include_quota=False)
    event["payload"]["info"]["rate_limits"] = info_limits
    event["payload"]["rate_limits"] = direct_limits
    return event


def _response_items() -> list[dict]:
    def item(timestamp: str, payload: dict) -> dict:
        return {"timestamp": timestamp, "type": "response_item", "payload": payload}
    return [
        item("2026-07-14T12:03:00Z", {"type": "message", "role": "user", "phase": "input", "content": [{"type": "input_text", "text": "Synthetic first meaningful user prompt"}]}),
        item("2026-07-14T12:03:10Z", {"type": "message", "role": "assistant", "phase": "output", "content": [{"type": "output_text", "text": "Synthetic assistant response"}]}),
        item("2026-07-14T12:03:20Z", {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Synthetic reasoning"}], "encrypted_content": "fixture-encrypted", "summary": [{"type": "summary_text", "text": "Synthetic summary"}]}),
        item("2026-07-14T12:03:30Z", {"type": "function_call", "call_id": "fn-1", "name": "fixture_function", "arguments": "{}", "namespace": "fixture"}),
        item("2026-07-14T12:03:40Z", {"type": "function_call_output", "call_id": "fn-1", "output": {"ok": True}}),
        item("2026-07-14T12:03:50Z", {"type": "custom_tool_call", "call_id": "custom-1", "name": "fixture_custom", "input": {"q": "synthetic"}, "status": "completed"}),
        item("2026-07-14T12:04:00Z", {"type": "custom_tool_call_output", "call_id": "custom-1", "output": {"answer": "synthetic"}}),
        item("2026-07-14T12:04:10Z", {"type": "tool_search_call", "call_id": "search-1", "arguments": {"query": "synthetic"}, "execution": {"id": "exec-1"}, "status": "completed"}),
        item("2026-07-14T12:04:20Z", {"type": "tool_search_output", "call_id": "search-1", "execution": {"id": "exec-1"}, "status": "completed", "tools": [{"name": "fixture-search"}]}),
        item("2026-07-14T12:04:30Z", {"type": "web_search_call", "action": "search", "status": "completed"}),
    ]


def _lifecycle_events() -> list[dict]:
    payloads = [
        {"type": "agent_message", "message": "Synthetic agent message", "phase": "final", "memory_citation": None},
        {"type": "agent_reasoning", "text": "Synthetic agent reasoning"},
        {"type": "task_started", "collaboration_mode_kind": "parallel", "model_context_window": 272000, "started_at": "2026-07-14T12:05:00Z", "turn_id": "turn-a"},
        {"type": "task_complete", "completed_at": "2026-07-14T12:05:10Z", "duration_ms": 10, "last_agent_message": "done", "turn_id": "turn-a"},
        {"type": "context_compacted"},
        {"type": "patch_apply_end", "call_id": "patch-1", "changes": [{"path": "synthetic.txt"}], "status": "completed", "stderr": "", "stdout": "ok", "success": True, "turn_id": "turn-a"},
        {"type": "mcp_tool_call_end", "call_id": "mcp-1", "duration": 1, "invocation": {"name": "fixture"}, "result": {"ok": True}},
        {"type": "web_search_end", "action": "search", "call_id": "search-1", "query": "synthetic"},
        {"type": "user_message", "images": [], "local_images": [], "message": "Synthetic user event", "text_elements": [{"text": "Synthetic user event"}]},
    ]
    return [{"timestamp": f"2026-07-14T12:{5 + index:02d}:00Z", "type": "event_msg", "payload": payload}
            for index, payload in enumerate(payloads)]


# ── #294 S6 conversation-normalization prose helpers ─────────────────────────
# Deterministic content-bearing records used by the collision/nested extensions
# and the three new conversation scenarios. Every text is synthetic and carries
# no maintainer data (enforced by test_fixture_corpus_contains_no_maintainer_data).


def _turn_context(timestamp: str, turn_id: str, model: str = MODEL) -> dict:
    return {"timestamp": timestamp, "type": "turn_context",
            "payload": {"turn_id": turn_id, "model": model, "model_context_window": 272000}}


def _response_message(timestamp: str, role: str, text: str) -> dict:
    """A ``response_item``/``message`` record (user or assistant prose)."""
    content_key = "input_text" if role == "user" else "output_text"
    return {"timestamp": timestamp, "type": "response_item",
            "payload": {"type": "message", "role": role,
                        "phase": "input" if role == "user" else "output",
                        "content": [{"type": content_key, "text": text}]}}


def _event_prose(timestamp: str, ptype: str, text: str) -> dict:
    """An ``event_msg``-family prose record (the mirror-pairing input family)."""
    if ptype == "agent_message":
        payload = {"type": "agent_message", "message": text, "phase": "final", "memory_citation": None}
    elif ptype == "agent_reasoning":
        payload = {"type": "agent_reasoning", "text": text}
    elif ptype == "user_message":
        payload = {"type": "user_message", "images": [], "local_images": [], "message": text,
                   "text_elements": [{"text": text}]}
    else:
        raise ValueError(f"unsupported event prose type: {ptype}")
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _mirror_pairing_records() -> list[dict]:
    """Modern turned conversation exercising every §5.3 pairing shape."""
    over_prefix = ("codex-mirror-overcap-prefix " * 640)[:_OVERCAP_PREFIX_LEN]
    long_a = over_prefix + "-distinct-tail-alpha"
    long_b = over_prefix + "-distinct-tail-beta"

    def ts(index: int) -> str:
        return f"2026-07-14T13:{index:02d}:00Z"

    return [
        _session_meta(session_id=MIRROR_SESSION, record_id="mirror-thread",
                      thread_source="mirror-thread", forked_from_id="mirror-thread"),
        _turn_context(ts(1), "turn-m"),
        # exact mirror pair (assistant): response_item canonical, event_msg suppressed.
        _response_message(ts(2), "assistant", "Mirror assistant reply"),
        _event_prose(ts(3), "agent_message", "Mirror assistant reply"),
        # non-mirror event prose survives (genuine unique event_msg content).
        _event_prose(ts(4), "agent_message", "Unique event-only note"),
        # distinct over-cap texts sharing a capped prefix must NOT pair.
        _response_message(ts(5), "assistant", long_a),
        _event_prose(ts(6), "agent_message", long_b),
        # whitespace-sensitive variants (two spaces vs one) must NOT pair.
        _response_message(ts(7), "assistant", "code x  y"),
        _event_prose(ts(8), "agent_message", "code x y"),
        # multiset one-to-one: one response, three identical events -> one pairs,
        # two survive (three copies never collapse into one response row).
        _response_message(ts(9), "assistant", "Triple echo"),
        _event_prose(ts(10), "agent_message", "Triple echo"),
        _event_prose(ts(11), "agent_message", "Triple echo"),
        _event_prose(ts(12), "agent_message", "Triple echo"),
        # distant identical cross-family rows in DIFFERENT turns must NOT pair.
        _event_prose(ts(13), "agent_message", "Distant cross echo"),
        # repeated identical prompts -> two distinct logical items (offsets differ).
        _response_message(ts(14), "user", "Repeat prompt"),
        _response_message(ts(15), "user", "Repeat prompt"),
        _turn_context(ts(16), "turn-n"),
        _response_message(ts(17), "assistant", "Distant cross echo"),
        _token_event(timestamp=ts(18)),
    ]


def _unturned_event_prose_records() -> list[dict]:
    """Identity established, event-family prose, NEVER any turn_context.

    Carries one adjacent mirror pair (retain canonical, drop event) and one
    uncorrelated duplicate separated by an intervening same-kind prose row
    (retain both) — the §5.3 unturned adjacency rule.
    """
    def ts(index: int) -> str:
        return f"2026-07-14T14:{index:02d}:00Z"

    return [
        _session_meta(session_id=UNTURNED_SESSION, record_id="unturned-thread",
                      thread_source="unturned-thread", forked_from_id="unturned-thread"),
        _response_message(ts(1), "assistant", "Unturned reply"),
        _event_prose(ts(2), "agent_message", "Unturned reply"),      # adjacent -> pairs
        _event_prose(ts(3), "agent_message", "Solo unturned note"),  # unique -> survives
        _response_message(ts(4), "assistant", "Coincidence"),
        _event_prose(ts(5), "agent_message", "Intervening other"),   # intervening same-kind
        _event_prose(ts(6), "agent_message", "Coincidence"),         # not adjacent -> retain both
        _token_event(timestamp=ts(7)),
    ]


def _title_wrapper_window_records() -> list[dict]:
    """Mirror-paired structural wrapper prompts push the first meaningful prompt
    past physical row 12 while keeping it inside logical prompt 12 (spec §4.3)."""
    def ts(index: int) -> str:
        return f"2026-07-14T15:{index:02d}:00Z"

    records: list[dict] = [
        _session_meta(session_id=TITLE_SESSION, record_id="title-thread",
                      thread_source="title-thread", forked_from_id="title-thread"),
    ]
    index = 1
    for wrapper in range(1, 8):  # 7 mirror-paired wrappers = 14 physical user rows
        if wrapper % 2:
            text = f"<environment_context>wrapper context {wrapper}</environment_context>"
        else:
            text = f"<user_instructions>wrapper instructions {wrapper}</user_instructions>"
        records.append(_response_message(ts(index), "user", text))
        index += 1
        records.append(_event_prose(ts(index), "user_message", text))
        index += 1
    records.append(_response_message(ts(index), "user", "First meaningful title prompt"))
    index += 1
    records.append(_token_event(timestamp=ts(index)))
    return records


def _content_bearing_root(root: str, record_id: str, subject: str) -> list[dict]:
    """A minimal turned conversation with prose distinct per collision root."""
    def ts(index: int) -> str:
        return f"2026-07-14T16:{index:02d}:00Z"

    return [
        _session_meta(root=root, record_id=record_id),
        _turn_context(ts(1), "turn-a"),
        _response_message(ts(2), "user", f"{subject} prompt"),
        _response_message(ts(3), "assistant", f"{subject} response"),
        _token_event(timestamp=ts(4)),
    ]


def _secret_canary_records() -> list[dict]:
    """A turned Codex conversation whose assistant reply embeds the documented
    secret patterns (spec §3.6). Normalizes to ONE thread (its own SECRET_SESSION
    identity) so the route-level export test can resolve a v1 conversation key,
    GET the export with anonymization on, and prove the qualified anon plan scrubs
    the canary tokens (secret shapes, provider root, project label) while the
    surrounding prose survives — and that the raw leg still carries them. The
    trailing structural ``message`` record keeps the raw file's secret/root/home
    strings so the kernel-level scrub test stays non-vacuous."""
    def ts(index: int) -> str:
        return f"2026-07-14T18:{index:02d}:00Z"

    return [
        _session_meta(root=ROOT_A, session_id=SECRET_SESSION,
                      record_id="secret-canary-thread", thread_source="secret-canary-thread",
                      forked_from_id="secret-canary-thread"),
        _turn_context(ts(1), "turn-canary"),
        _response_message(ts(2), "user", "Canary widget configuration prompt"),
        _response_message(
            ts(3), "assistant",
            "Configure the deployment with sk-fixture-not-a-secret and header "
            "Authorization: Bearer fixture-token targeting /synthetic/root-a/project-red now"),
        # Structural secret-bearing record retained verbatim: keeps the raw file's
        # api-key / bearer / home / root strings so kernel scrub tests keep proving
        # each redaction, and models a non-message record surviving normalization.
        {"timestamp": ts(4), "type": "message", "payload": {
            "api_key_shape": "sk-fixture-not-a-secret",
            "authorization_shape": "Authorization: Bearer fixture-token",
            "home_shape": "/home/fixture-user/project-green", "root_shape": ROOT_A}},
        _token_event(timestamp=ts(5)),
    ]


def _session_a_turn_contract_records() -> list[dict]:
    """Production-shaped fragmentation and injected-content contract (#330 A)."""
    def rec(timestamp: str, record_type: str, payload: dict) -> dict:
        return {"timestamp": timestamp, "type": record_type, "payload": payload}

    def message(timestamp: str, role: str, text: str, *, phase: str | None = None) -> dict:
        payload = {
            "type": "message",
            "role": role,
            "content": [{
                "type": "input_text" if role != "assistant" else "output_text",
                "text": text,
            }],
        }
        if phase is not None:
            payload["phase"] = phase
        return rec(timestamp, "response_item", payload)

    def token(timestamp: str, *, cached: int, inp: int, out: int, reasoning: int) -> dict:
        total = inp + out
        return rec(timestamp, "event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "cached_input_tokens": cached,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": total,
                },
                "model_context_window": 272000,
                "rate_limits": {},
                "total_token_usage": {"total_tokens": total},
            },
        })

    first_meta = _session_meta(
        session_id=SESSION_A_THREAD,
        record_id="session-a-thread",
        thread_source="session-a-thread",
        forked_from_id="session-a-thread",
    )
    first_meta["timestamp"] = "2026-07-21T10:00:00Z"
    resumed_meta = json.loads(json.dumps(first_meta))
    resumed_meta["timestamp"] = "2026-07-21T10:00:20Z"
    for field in ("instructions", "model_context_window", "model_provider", "tools", "user"):
        resumed_meta["payload"].pop(field, None)

    return [
        first_meta,
        rec("2026-07-21T10:00:01Z", "event_msg", {
            "collaboration_mode_kind": "default", "model_context_window": 272000,
            "started_at": "2026-07-21T10:00:01Z", "turn_id": "turn-a",
            "type": "task_started"}),
        message("2026-07-21T10:00:02Z", "developer",
                "<permissions instructions>synthetic permissions</permissions instructions>"),
        message("2026-07-21T10:00:03Z", "developer",
                "You are a synthetic harness role preamble."),
        message("2026-07-21T10:00:04Z", "developer",
                "<multi_agent_mode>synthetic mode</multi_agent_mode>"),
        message("2026-07-21T10:00:05Z", "user",
                "<recommended_plugins>synthetic plugin</recommended_plugins>"),
        message("2026-07-21T10:00:06Z", "user",
                "# AGENTS.md instructions for /synthetic/project\n\n"
                "<INSTRUCTIONS>Synthetic instructions only.</INSTRUCTIONS>"),
        message("2026-07-21T10:00:07Z", "user",
                "<skill>\n<name>synthetic-skill</name>\nSynthetic skill body.\n</skill>"),
        message("2026-07-21T10:00:07.500000Z", "user",
                "<recommended_plugins>synthetic bundled plugin</recommended_plugins>\n"
                "# AGENTS.md instructions for /synthetic/project\n\n"
                "<INSTRUCTIONS>synthetic bundled policy</INSTRUCTIONS>\n"
                "<environment_context>synthetic bundled environment</environment_context>"),
        _turn_context("2026-07-21T10:00:08Z", "turn-a"),
        message("2026-07-21T10:00:09Z", "user", "Build the synthetic widget", phase="input"),
        rec("2026-07-21T10:00:10Z", "event_msg", {
            "images": [], "local_images": [], "message": "Build the synthetic widget",
            "text_elements": [{"text": "Build the synthetic widget"}],
            "type": "user_message"}),
        rec("2026-07-21T10:00:11Z", "response_item", {
            "content": [{"text": "Plan the widget", "type": "reasoning_text"}],
            "summary": [], "type": "reasoning"}),
        rec("2026-07-21T10:00:12Z", "event_msg", {
            "text": "Plan the widget", "type": "agent_reasoning"}),
        rec("2026-07-21T10:00:13Z", "response_item", {
            "call_id": "call-a", "input": {"q": "widget"}, "name": "synthetic_tool",
            "status": "completed", "type": "custom_tool_call"}),
        rec("2026-07-21T10:00:14Z", "response_item", {
            "call_id": "call-a", "output": {"answer": "one"},
            "type": "custom_tool_call_output"}),
        message("2026-07-21T10:00:15Z", "assistant", "First distinct answer", phase="output"),
        rec("2026-07-21T10:00:16Z", "event_msg", {
            "memory_citation": None, "message": "First distinct answer",
            "phase": "commentary", "type": "agent_message"}),
        message("2026-07-21T10:00:17Z", "assistant", "Repeated legitimate note", phase="output"),
        message("2026-07-21T10:00:18Z", "assistant", "Repeated legitimate note", phase="output"),
        token("2026-07-21T10:00:19Z", cached=30, inp=120, out=40, reasoning=10),
        resumed_meta,
        message("2026-07-21T10:00:21Z", "developer",
                "<permissions instructions>synthetic resumed permissions</permissions instructions>"),
        rec("2026-07-21T10:00:22Z", "response_item", {
            "content": [{"text": "Continue widget reasoning", "type": "reasoning_text"}],
            "summary": [], "type": "reasoning"}),
        rec("2026-07-21T10:00:23Z", "response_item", {
            "arguments": "{\"path\":\"synthetic.txt\"}", "call_id": "call-b",
            "name": "fixture_function", "type": "function_call"}),
        rec("2026-07-21T10:00:24Z", "response_item", {
            "call_id": "call-b", "output": {"ok": True}, "type": "function_call_output"}),
        message("2026-07-21T10:00:25Z", "assistant", "Second distinct answer", phase="output"),
        rec("2026-07-21T10:00:26Z", "event_msg", {
            "memory_citation": None, "message": "Second distinct answer",
            "phase": "final", "type": "agent_message"}),
        token("2026-07-21T10:00:27Z", cached=60, inp=240, out=80, reasoning=20),
        rec("2026-07-21T10:00:28Z", "event_msg", {
            "call_id": "patch-a", "changes": [{"path": "synthetic.txt"}],
            "status": "completed", "turn_id": "turn-a", "type": "patch_apply_end"}),
        rec("2026-07-21T10:00:29Z", "event_msg", {
            "completed_at": "2026-07-21T10:00:29Z", "duration_ms": 28000,
            "last_agent_message": "Second distinct answer", "turn_id": "turn-a",
            "type": "task_complete"}),
        rec("2026-07-21T10:00:30Z", "event_msg", {
            "collaboration_mode_kind": "default", "model_context_window": 272000,
            "started_at": "2026-07-21T10:00:30Z", "turn_id": "turn-b",
            "type": "task_started"}),
        message("2026-07-21T10:00:31Z", "developer",
                "<model_switch>synthetic model switch</model_switch>"),
        _turn_context("2026-07-21T10:00:32Z", "turn-b"),
        message("2026-07-21T10:00:33Z", "user", "Build the synthetic widget", phase="input"),
        rec("2026-07-21T10:00:34Z", "event_msg", {
            "images": [], "local_images": [], "message": "Build the synthetic widget",
            "text_elements": [{"text": "Build the synthetic widget"}],
            "type": "user_message"}),
        message("2026-07-21T10:00:35Z", "assistant", "Third distinct answer", phase="output"),
        rec("2026-07-21T10:00:36Z", "event_msg", {
            "memory_citation": None, "message": "Third distinct answer",
            "phase": "final", "type": "agent_message"}),
        token("2026-07-21T10:00:37Z", cached=90, inp=360, out=120, reasoning=30),
        rec("2026-07-21T10:00:38Z", "event_msg", {
            "completed_at": "2026-07-21T10:00:38Z", "duration_ms": 8000,
            "last_agent_message": "Third distinct answer", "turn_id": "turn-b",
            "type": "task_complete"}),
    ]


def _session_b_card_wire_records() -> list[dict]:
    """Production-shaped shell/output/patch card contract (#331 Task A)."""
    def rec(timestamp: str, record_type: str, payload: dict) -> dict:
        return {"timestamp": timestamp, "type": record_type, "payload": payload}

    def call(timestamp: str, call_id: str, name: str, value: str,
             *, status: str = "completed") -> dict:
        return rec(timestamp, "response_item", {
            "call_id": call_id, "input": value, "name": name,
            "status": status, "type": "custom_tool_call",
        })

    def output(timestamp: str, call_id: str, value) -> dict:
        return rec(timestamp, "response_item", {
            "call_id": call_id, "output": value,
            "type": "custom_tool_call_output",
        })

    meta = _session_meta(
        session_id=SESSION_B_THREAD,
        record_id="session-b-thread",
        thread_source="session-b-thread",
        forked_from_id="session-b-thread",
    )
    meta["timestamp"] = "2026-07-21T11:00:00Z"
    exec_input = (
        'const r = await tools.exec_command({\n'
        '  cmd: "printf \'alpha\\\\n\'",\n'
        '  workdir: "/synthetic/root-a/project-red",\n'
        '  yield_time_ms: 10000,\n'
        '  max_output_tokens: 12000\n'
        '});\n'
        'text(r.output);'
    )
    direct_patch = (
        "*** Begin Patch\n"
        "*** Add File: synthetic-added.txt\n"
        "+alpha\n"
        "*** End Patch"
    )
    exec_patch = (
        'const patch = "*** Begin Patch\\n*** Update File: synthetic-edit.txt'
        '\\n@@\\n-old\\n+new\\n*** End Patch";\n'
        'text(await tools.apply_patch(patch));'
    )
    heredoc_patch = (
        "const r = await tools.exec_command({\n"
        "  cmd: \"apply_patch <<'PATCH'\\n*** Begin Patch\\n"
        "*** Delete File: synthetic-delete.txt\\n*** End Patch\\nPATCH\",\n"
        "  workdir: \"/synthetic/root-a/project-red\"\n"
        "});\n"
        "text(r.output);"
    )
    status_ok = [
        {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\n\nOutput:"},
        {"type": "input_text", "text": "alpha\n"},
    ]
    status_failed = [
        {"type": "input_text", "text": "Script failed\nWall time 0.2 seconds\n\nOutput:"},
        {"type": "input_text", "text": "synthetic stderr\n"},
    ]
    return [
        meta,
        _turn_context("2026-07-21T11:00:01Z", "turn-shell"),
        _response_message("2026-07-21T11:00:02Z", "user", "Run synthetic shell and patch fixtures"),
        call("2026-07-21T11:00:03Z", "exec-ok", "exec", exec_input),
        output("2026-07-21T11:00:04Z", "exec-ok", status_ok),
        call("2026-07-21T11:00:05Z", "exec-string", "exec", exec_input),
        output("2026-07-21T11:00:06Z", "exec-string", "plain string output\n"),
        call("2026-07-21T11:00:07Z", "exec-failed", "exec", exec_input,
             status="failed"),
        output("2026-07-21T11:00:08Z", "exec-failed", status_failed),
        call("2026-07-21T11:00:09Z", "exec-malformed", "exec",
             'text("tools.exec_command({cmd: \\\"do not decode\\\"})");'),
        output("2026-07-21T11:00:10Z", "exec-malformed", [{
            "type": "future_part", "value": {"inspectable": True}}]),
        call("2026-07-21T11:00:10.100000Z", "exec-blank", "exec", exec_input),
        output("2026-07-21T11:00:10.200000Z", "exec-blank", ""),
        call("2026-07-21T11:00:11Z", "direct-patch", "apply_patch", direct_patch),
        rec("2026-07-21T11:00:12Z", "event_msg", {
            "call_id": "direct-patch",
            "changes": [
                {"path": "synthetic-added.txt", "status": "added",
                 "unified_diff": "--- /dev/null\n+++ b/synthetic-added.txt\n@@ -0,0 +1 @@\n+alpha\n"},
                {"path": "synthetic-edit.txt", "status": "modified",
                 "unified_diff": "--- a/synthetic-edit.txt\n+++ b/synthetic-edit.txt\n@@ -1 +1 @@\n-old\n+new\n"},
                {"path": "synthetic-delete.txt", "status": "deleted",
                 "unified_diff": "--- a/synthetic-delete.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"},
                {"path": "synthetic-old.txt", "move_path": "synthetic-new.txt",
                 "status": "moved", "unified_diff": "--- a/synthetic-old.txt\n+++ b/synthetic-new.txt\n"},
            ],
            "status": "completed", "stderr": "", "stdout": "patch ok\n",
            "success": True, "turn_id": "turn-shell", "type": "patch_apply_end",
        }),
        output("2026-07-21T11:00:13Z", "direct-patch", "Done!"),
        call("2026-07-21T11:00:14Z", "exec-patch", "exec", exec_patch),
        rec("2026-07-21T11:00:15Z", "event_msg", {
            "call_id": "inner-exec-patch", "changes": [{
                "path": "synthetic-edit.txt", "status": "modified",
                "unified_diff": "--- a/synthetic-edit.txt\n+++ b/synthetic-edit.txt\n@@ -1 +1 @@\n-old\n+new\n",
            }], "status": "completed", "stderr": "", "stdout": "Done!",
            "success": True, "turn_id": "turn-shell", "type": "patch_apply_end",
        }),
        output("2026-07-21T11:00:16Z", "exec-patch", "Done!"),
        call("2026-07-21T11:00:17Z", "heredoc-patch", "exec", heredoc_patch),
        output("2026-07-21T11:00:18Z", "heredoc-patch", "Done!"),
        call("2026-07-21T11:00:19Z", "repeat-patch-1", "apply_patch", direct_patch),
        rec("2026-07-21T11:00:20Z", "event_msg", {
            "call_id": "repeat-patch-1", "changes": [{
                "path": "synthetic-repeat.txt", "status": "modified",
                "unified_diff": "--- a/synthetic-repeat.txt\n+++ b/synthetic-repeat.txt\n",
            }], "status": "completed", "stderr": "", "stdout": "Done!",
            "success": True, "turn_id": "turn-shell", "type": "patch_apply_end",
        }),
        output("2026-07-21T11:00:21Z", "repeat-patch-1", "Done!"),
        call("2026-07-21T11:00:22Z", "repeat-patch-2", "apply_patch", direct_patch),
        rec("2026-07-21T11:00:23Z", "event_msg", {
            "call_id": "repeat-patch-2", "changes": [{
                "path": "synthetic-repeat.txt", "status": "modified",
                "unified_diff": "--- a/synthetic-repeat.txt\n+++ b/synthetic-repeat.txt\n",
            }], "status": "completed", "stderr": "", "stdout": "Done!",
            "success": True, "turn_id": "turn-shell", "type": "patch_apply_end",
        }),
        output("2026-07-21T11:00:24Z", "repeat-patch-2", "Done!"),
        rec("2026-07-21T11:00:25Z", "event_msg", {
            "call_id": "diff-less", "changes": [{
                "path": "synthetic-summary.txt", "status": "modified",
            }], "status": "failed", "stderr": "synthetic failure", "stdout": "",
            "success": False, "turn_id": "turn-shell", "type": "patch_apply_end",
        }),
    ]


def _session_c_secondary_tool_records() -> list[dict]:
    """Production-shaped plan/web/MCP/agent wire contract (#332 Task A)."""
    records = [_session_meta(
        session_id=SESSION_C_PARENT, record_id="session-c-parent",
        thread_source=SESSION_C_PARENT, forked_from_id=SESSION_C_PARENT,
    )]
    records[0]["timestamp"] = "2026-07-22T08:00:00Z"
    tick = 1

    def add(record_type: str, payload: dict) -> None:
        nonlocal tick
        records.append({
            "timestamp": f"2026-07-22T08:{tick // 60:02d}:{tick % 60:02d}Z",
            "type": record_type, "payload": payload,
        })
        tick += 1

    def call(call_id: str, name: str, arguments: object, *, status=None) -> None:
        payload = {
            "type": "function_call", "call_id": call_id, "name": name,
            "arguments": (_canonical_json(arguments)
                          if not isinstance(arguments, str) else arguments),
        }
        if status is not None:
            payload["status"] = status
        add("response_item", payload)

    def output(call_id: str, value: object, *, status=None) -> None:
        payload = {"type": "function_call_output", "call_id": call_id,
                   "output": value}
        if status is not None:
            payload["status"] = status
        add("response_item", payload)

    add("turn_context", {"type": "turn_context", "turn_id": "turn-session-c",
                         "model": MODEL, "model_context_window": 272000})
    add("response_item", {
        "type": "message", "role": "user", "phase": "input",
        "content": [{"type": "input_text", "text": "Exercise synthetic secondary tools"}],
    })
    call("plan-ok", "update_plan", {
        "explanation": "Synthetic plan explanation",
        "plan": [
            {"step": "Pending synthetic step", "status": "pending"},
            {"step": "Active synthetic step", "status": "in_progress"},
            {"step": "Completed synthetic step", "status": "completed"},
        ],
    })
    output("plan-ok", "Plan updated")
    call("plan-malformed", "update_plan", {"plan": "not-a-list"})
    output("plan-malformed", "Malformed plan preserved")

    add("response_item", {
        "type": "web_search_call", "id": "web-ok", "status": "completed",
        "action": {"type": "search", "query": "synthetic web query",
                   "queries": ["synthetic web query"]},
    })
    add("event_msg", {
        "type": "web_search_end", "call_id": "web-ok",
        "query": "synthetic web query",
        "action": {"type": "search", "query": "synthetic web query"},
        "results": [{
            "type": "computer_initialize_state", "domain": "example.test",
            "ref_id": "turn0search0", "snippet": "Synthetic web snippet",
            "title": "Synthetic web result", "url": "https://example.test/result",
        }],
    })
    for suffix in ("a", "b"):
        add("response_item", {
            "type": "web_search_call", "id": f"web-repeat-{suffix}",
            "status": "completed",
            "action": {"type": "search", "query": "repeated synthetic query"},
        })
        add("event_msg", {
            "type": "web_search_end", "call_id": f"web-repeat-{suffix}",
            "query": "repeated synthetic query",
            "action": {"type": "search", "query": "repeated synthetic query"},
            "results": [],
        })
    add("response_item", {
        "type": "web_search_call", "id": "web-error", "status": "failed",
        "action": {"type": "search", "query": "synthetic failed query"},
    })
    add("event_msg", {
        "type": "web_search_end", "call_id": "web-error", "status": "error",
        "query": "synthetic failed query",
        "action": {"type": "search", "query": "synthetic failed query"},
        "results": [], "error": "synthetic search failure",
    })
    add("event_msg", {
        "type": "web_search_end", "call_id": "web-malformed",
        "query": ["not", "a", "string"], "action": None,
    })

    call("mcp-ok", "fixture_search_issues", {"state": "open"})
    add("event_msg", {
        "type": "mcp_tool_call_end", "call_id": "mcp-ok",
        "duration": {"secs": 1, "nanos": 250000000},
        "invocation": {"server": "fixture", "tool": "search_issues",
                       "arguments": {"state": "open"}},
        "result": {"Ok": {"content": [{"type": "text", "text": "synthetic"}]}},
    })
    output("mcp-ok", "synthetic MCP wrapper output")
    call("mcp-error", "fixture_get_issue", {"number": 999}, status="failed")
    add("event_msg", {
        "type": "mcp_tool_call_end", "call_id": "mcp-error",
        "duration": {"secs": 0, "nanos": 500000000},
        "invocation": {"server": "fixture", "tool": "get_issue",
                       "arguments": {"number": 999}},
        "result": {"Err": "synthetic MCP failure"},
    })
    output("mcp-error", "synthetic MCP failure", status="error")
    add("event_msg", {
        "type": "mcp_tool_call_end", "call_id": "mcp-malformed",
        "duration": "unknown", "invocation": "not-an-object",
        "result": {"future": True},
    })

    agent_calls = [
        ("spawn-proven", "spawn_agent", {
            "task_name": "session_c_child", "fork_turns": "none",
            "message": "Do synthetic child work", "agent_type": "cctally_reviewer",
        }, {"task_name": "/root/session_c_child"}),
        ("spawn-ambiguous", "spawn_agent", {
            "task_name": "ambiguous", "fork_turns": "none",
            "message": "Do ambiguous synthetic work", "agent_type": "cctally_reviewer",
        }, {"task_name": "/root/ambiguous"}),
        ("spawn-unmatched", "spawn_agent", {
            "task_name": "missing", "fork_turns": "none",
            "message": "Do unmatched synthetic work", "agent_type": "cctally_reviewer",
        }, {"task_name": "/root/missing"}),
        ("wait-a", "wait_agent", {"timeout_ms": 30000},
         {"message": "synthetic update", "timed_out": False}),
        ("wait-b", "wait_agent", {"timeout_ms": 30000},
         {"message": "synthetic timeout", "timed_out": True}),
        ("send", "send_message", {"target": "/root/session_c_child",
                                    "message": "Synthetic message"}, "Message sent"),
        ("list", "list_agents", {}, {"agents": [{"task_name": "/root/session_c_child",
                                                    "status": "completed"}]}),
        ("followup", "followup_task", {"target": "/root/session_c_child",
                                        "message": "Synthetic follow-up"}, "Task resumed"),
        ("interrupt", "interrupt_agent", {"target": "/root/session_c_child"},
         {"previous_status": "running"}),
    ]
    for call_id, name, arguments, result in agent_calls:
        call(call_id, name, arguments)
        output(call_id, _canonical_json(result) if not isinstance(result, str) else result)
    call("agent-malformed", "wait_agent", "not-json", status="failed")
    output("agent-malformed", "synthetic malformed fallback", status="error")
    add("response_item", {
        "type": "message", "role": "assistant", "phase": "output",
        "content": [{"type": "output_text", "text": (
            "Local image fixtures: ![private](/synthetic/private/screenshot.png) "
            "and ![placeholder](url:0)"
        )}],
    })
    return records


def _session_c_child_records(
    *, session_id: str, record_id: str, agent_path: str, nickname: str,
) -> list[dict]:
    meta = _session_meta(
        session_id=session_id, record_id=record_id,
        thread_source="subagent", forked_from_id=SESSION_C_PARENT,
    )
    meta["timestamp"] = "2026-07-22T09:00:00Z"
    meta["payload"].update({
        "parent_thread_id": SESSION_C_PARENT,
        "agent_path": agent_path,
        "agent_role": "cctally_reviewer",
        "agent_nickname": nickname,
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": SESSION_C_PARENT, "depth": 1,
            "agent_path": agent_path, "agent_role": "cctally_reviewer",
            "agent_nickname": nickname,
        }}},
    })
    return [
        meta,
        _turn_context("2026-07-22T09:00:01Z", f"turn-{record_id}"),
        _response_message("2026-07-22T09:00:02Z", "user", "Synthetic child prompt"),
        _response_message("2026-07-22T09:00:03Z", "assistant", "Synthetic child result"),
    ]


def _session_d_reasoning_lifecycle_marker_records() -> list[dict]:
    """Production-shaped Session D cases with only synthetic content."""
    records: list[dict] = [
        _session_meta(
            session_id=SESSION_D_THREAD,
            record_id="session-d-reasoning-lifecycle-markers",
            thread_source="session-d-reasoning-lifecycle-markers",
            forked_from_id="session-d-reasoning-lifecycle-markers",
        ),
    ]

    def turn(timestamp: str, turn_id: str) -> None:
        records.append(_turn_context(timestamp, turn_id))

    def response(timestamp: str, payload: dict) -> None:
        records.append({"timestamp": timestamp, "type": "response_item", "payload": payload})

    def event(timestamp: str, payload: dict) -> None:
        records.append({"timestamp": timestamp, "type": "event_msg", "payload": payload})

    # Exact response/event mirror for a structurally title-only reasoning row.
    turn("2026-07-22T06:00:00Z", "reason-title")
    response("2026-07-22T06:00:01Z", {
        "type": "reasoning", "summary": [
            {"type": "summary_text", "text": "**Inspecting synthetic state**"}],
        "content": [], "encrypted_content": "synthetic-encrypted-title",
    })
    event("2026-07-22T06:00:02Z", {
        "type": "agent_reasoning", "text": "**Inspecting synthetic state**"})

    # Retained provider summary and body stay distinct; body-only is explicit.
    turn("2026-07-22T06:01:00Z", "reason-summary-body")
    response("2026-07-22T06:01:01Z", {
        "type": "reasoning", "summary": [
            {"type": "summary_text", "text": "Synthetic provider summary."}],
        "content": [
            {"type": "reasoning_text", "text": "Detailed synthetic reasoning body."}],
        "encrypted_content": "synthetic-encrypted-summary-body",
    })
    turn("2026-07-22T06:02:00Z", "reason-body")
    response("2026-07-22T06:02:01Z", {
        "type": "reasoning", "summary": [], "content": [
            {"type": "reasoning_text", "text": "Body-only synthetic reasoning."}],
        "encrypted_content": "synthetic-encrypted-body",
    })

    # Repeated legitimate title in another physical turn must remain distinct.
    turn("2026-07-22T06:03:00Z", "reason-repeat")
    event("2026-07-22T06:03:01Z", {
        "type": "agent_reasoning", "text": "**Inspecting synthetic state**"})

    # Empty physical records remain retained but produce no presentation/search row.
    turn("2026-07-22T06:04:00Z", "reason-empty")
    response("2026-07-22T06:04:01Z", {
        "type": "reasoning", "summary": [], "content": [],
        "encrypted_content": "synthetic-encrypted-empty",
    })
    event("2026-07-22T06:04:02Z", {"type": "agent_reasoning", "text": "  \n\t"})

    # A fully proved lifecycle pair folds onto its one owning response.
    turn("2026-07-22T06:05:00Z", "lifecycle-folded")
    event("2026-07-22T06:05:01Z", {
        "type": "task_started", "collaboration_mode_kind": "default",
        "model_context_window": 272000, "started_at": 1784700301000,
        "turn_id": "lifecycle-folded",
    })
    response("2026-07-22T06:05:02Z", {
        "type": "message", "role": "assistant", "phase": "final",
        "content": [{"type": "output_text", "text": "Folded lifecycle answer."}],
    })
    event("2026-07-22T06:05:03Z", {
        "type": "task_complete", "completed_at": 1784700303000,
        "duration_ms": 2000, "last_agent_message": "Folded lifecycle answer.",
        "turn_id": "lifecycle-folded",
    })

    # Unmatched, ambiguous, unique-message, and error-bearing lifecycle stays visible.
    event("2026-07-22T06:06:00Z", {
        "type": "task_started", "collaboration_mode_kind": "default",
        "model_context_window": 272000, "started_at": "2026-07-22T06:06:00Z",
        "turn_id": "lifecycle-unmatched",
    })
    turn("2026-07-22T06:07:00Z", "lifecycle-ambiguous")
    for second in (1, 2):
        event(f"2026-07-22T06:07:0{second}Z", {
            "type": "task_started", "collaboration_mode_kind": "default",
            "model_context_window": 272000,
            "started_at": f"2026-07-22T06:07:0{second}Z",
            "turn_id": "lifecycle-ambiguous",
        })
    response("2026-07-22T06:07:03Z", {
        "type": "message", "role": "assistant", "phase": "final",
        "content": [{"type": "output_text", "text": "Ambiguous lifecycle answer."}],
    })
    event("2026-07-22T06:07:04Z", {
        "type": "task_complete", "completed_at": "2026-07-22T06:07:04Z",
        "duration_ms": 4000, "last_agent_message": "Unique completion message.",
        "turn_id": "lifecycle-ambiguous",
    })
    turn("2026-07-22T06:08:00Z", "lifecycle-error")
    response("2026-07-22T06:08:01Z", {
        "type": "message", "role": "assistant", "phase": "final",
        "content": [{"type": "output_text", "text": "Errored lifecycle answer."}],
    })
    event("2026-07-22T06:08:02Z", {
        "type": "task_complete", "completed_at": "2026-07-22T06:08:02Z",
        "duration_ms": 1000, "last_agent_message": "Errored lifecycle answer.",
        "turn_id": "lifecycle-error", "error": "Synthetic lifecycle failure",
    })

    marker_text = """Synthetic closeout prose remains visible.

::git-create-branch{cwd=\"/synthetic/project\" branch=\"feat/synthetic\"}
::git-stage{cwd=\"/synthetic/project\"}
::git-commit{cwd=\"/synthetic/project\"}
::git-push{cwd=\"/synthetic/project\" branch=\"feat/synthetic\"}
::git-create-pr{cwd=\"/synthetic/project\" branch=\"feat/synthetic\" url=\"https://example.test/pr/1\" isDraft=false}

<oai-mem-citation>
<citation_entries>
MEMORY.md:10-12|note=[synthetic fixture citation]
</citation_entries>
<rollout_ids>
11111111-2222-4333-8444-555555555555
</rollout_ids>
</oai-mem-citation>"""
    turn("2026-07-22T06:09:00Z", "markers")
    response("2026-07-22T06:09:01Z", {
        "type": "message", "role": "assistant", "phase": "final",
        "content": [{"type": "output_text", "text": marker_text}],
    })
    event("2026-07-22T06:09:02Z", {
        "type": "agent_message", "message": marker_text, "phase": "final"})

    # Authored lookalikes, code fences, malformed/unknown directives, and XML
    # near-misses are ordinary prose and must never be segmented away.
    turn("2026-07-22T06:10:00Z", "marker-lookalikes")
    response("2026-07-22T06:10:01Z", {
        "type": "message", "role": "user", "phase": "input", "content": [{
            "type": "input_text", "text":
            "User-authored ::git-stage{cwd=\"/synthetic/user\"} remains prose.",
        }],
    })
    response("2026-07-22T06:10:02Z", {
        "type": "message", "role": "assistant", "phase": "output", "content": [{
            "type": "output_text", "text":
            "```text\n::git-stage{cwd=\"/synthetic/fenced\"}\n```\n"
            "::git-unknown{cwd=\"/synthetic/unknown\"}\n"
            "::git-stage{cwd=\"/synthetic/malformed\" extra=\"nope\"}\n"
            "<oai-mem-citation><citation_entries>lookalike</citation_entries></oai-mem-citation>",
        }],
    })
    return records


def _session_e_native_family_records() -> list[dict]:
    """Retained control-plane families around two real logical turns."""
    full_state = {
        "agents_md": {"body": "SESSION_E_PRIVATE_INSTRUCTION_CANARY"},
        "environments": {"cwd": "/synthetic/private/session-e/workspace"},
        "skills": {"opaque_id": "native-secret-opaque-335"},
        "apps_instructions": True,
    }
    delta_state = {
        "environments": {"cwd": "/synthetic/private/session-e/workspace"},
        "host_skills": {"changed": True},
    }
    first_context = _turn_context(
        "2026-07-22T07:00:03Z", "session-e-turn-a")
    first_context["payload"].update({
        "cwd": "/synthetic/private/session-e/workspace",
        "workspace_roots": ["/synthetic/private/session-e/workspace"],
        "approval_policy": "never",
        "sandbox_policy": {"mode": "synthetic"},
        "summary": "SESSION_E_PRIVATE_INSTRUCTION_CANARY",
    })
    second_context = _turn_context(
        "2026-07-22T07:00:09Z", "session-e-turn-b",
        model="gpt-synthetic-codex-b")
    malformed_context = {
        "timestamp": "2026-07-22T07:00:15Z",
        "type": "turn_context",
        "payload": {
            "turn_id": {"future": "not-a-string"},
            "model": ["not", "a", "string"],
            "future_context_field": {"preserve": True},
        },
    }
    return [
        _session_meta(
            session_id=SESSION_E_THREAD,
            record_id="session-e-native-families",
            thread_source="session-e-native-families",
            forked_from_id="session-e-native-families",
        ),
        {"timestamp": "2026-07-22T07:00:01Z", "type": "world_state",
         "payload": {"full": True, "state": full_state}},
        {"timestamp": "2026-07-22T07:00:02Z",
         "type": "inter_agent_communication_metadata",
         "payload": {"trigger_turn": False}},
        first_context,
        _response_message(
            "2026-07-22T07:00:04Z", "user", "Session E visible prompt A"),
        _response_message(
            "2026-07-22T07:00:05Z", "assistant", "Session E visible answer A"),
        {"timestamp": "2026-07-22T07:00:06Z", "type": "world_state",
         "payload": {"full": False, "state": delta_state}},
        {"timestamp": "2026-07-22T07:00:07Z",
         "type": "inter_agent_communication_metadata",
         "payload": {"trigger_turn": True,
                     "future_metadata": {"preserve": True}}},
        {"timestamp": "2026-07-22T07:00:08Z", "type": "future_record_v100",
         "payload": {"opaque_id": "native-secret-opaque-335"}},
        second_context,
        _response_message(
            "2026-07-22T07:00:10Z", "user", "Session E visible prompt B"),
        _response_message(
            "2026-07-22T07:00:11Z", "assistant", "Session E visible answer B"),
        {"timestamp": "2026-07-22T07:00:12Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {
                 "input_tokens": 123456789,
                 "cached_input_tokens": 98765432,
                 "output_tokens": 76543210,
                 "reasoning_output_tokens": 54321098,
                 "total_tokens": 353086529,
             },
             "total_token_usage": {"total_tokens": 353086529},
             "model_context_window": 272000,
         }}},
        {"timestamp": "2026-07-22T07:00:13Z", "type": "world_state",
         "payload": {"full": False, "state": {
             "environments": {"cwd": "/synthetic/private/session-e/workspace"}}}},
        {"timestamp": "2026-07-22T07:00:14Z",
         "type": "inter_agent_communication_metadata",
         "payload": {"trigger_turn": False}},
        malformed_context,
    ]


def _claude_seed_records() -> list[dict]:
    """A genuine Claude-format conversation sharing SHARED_ID as its sessionId."""
    return [
        {"type": "user", "uuid": "claude-seed-uuid-1", "sessionId": SHARED_ID,
         "timestamp": "2026-07-14T12:00:00.000Z", "cwd": ROOT_A,
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "Claude seed user prompt distinct from codex"}]}},
        {"type": "assistant", "uuid": "claude-seed-uuid-2", "parentUuid": "claude-seed-uuid-1",
         "sessionId": SHARED_ID, "timestamp": "2026-07-14T12:00:05.000Z", "cwd": ROOT_A,
         "requestId": "claude-seed-req-1",
         "message": {"id": "claude-seed-msg-1", "model": CLAUDE_SEED_MODEL, "role": "assistant",
                     "content": [{"type": "text", "text": "Claude seed assistant reply distinct from codex"}],
                     "usage": {"input_tokens": 10, "output_tokens": 20,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    ]


def _claude_thinking_reference_records() -> list[dict]:
    """Claude Thinking reference for #334's same-pass provider comparison."""
    return [
        {"type": "user", "uuid": "334-ref-user", "parentUuid": None,
         "sessionId": CLAUDE_THINKING_SESSION, "timestamp": "2026-07-22T05:59:00Z",
         "cwd": "/synthetic/claude/session-d",
         "message": {"role": "user", "content": "Synthetic Claude thinking reference"}},
        {"type": "assistant", "uuid": "334-ref-assistant", "parentUuid": "334-ref-user",
         "sessionId": CLAUDE_THINKING_SESSION, "timestamp": "2026-07-22T05:59:01Z",
         "cwd": "/synthetic/claude/session-d", "requestId": "req-334-thinking",
         "message": {"id": "msg-334-thinking", "model": CLAUDE_SEED_MODEL, "role": "assistant",
                     "content": [
                         {"type": "thinking", "thinking":
                          "Claude keeps its established thinking body with **Markdown emphasis**."},
                         {"type": "text", "text": "Claude reference answer remains unchanged."},
                     ],
                     "usage": {"input_tokens": 10, "output_tokens": 20,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
    ]


def _scenarios() -> dict[str, tuple[list[dict], str | None]]:
    full = [_session_meta(), {"timestamp": "2026-07-14T12:01:00Z", "type": "turn_context", "payload": {"turn_id": "turn-a", "model": MODEL, "model_context_window": 272000}}, _token_event(), *_response_items(), *_lifecycle_events()]
    duplicate = [_session_meta()]
    for index, total in enumerate((1000, 1000, 3500, 3500, 7000)):
        duplicate.append(_token_event(timestamp=f"2026-07-14T12:{30 + index:02d}:00Z", total=total))
    parent = _session_meta(record_id="parent-thread-fixture", session_id="parent-thread-fixture", thread_source="parent-thread-fixture", forked_from_id="parent-thread-fixture")
    child = _session_meta(record_id="child-thread-fixture", thread_source="parent-thread-fixture", forked_from_id="parent-thread-fixture")
    quota_kernel_history = [
        {
            "timestamp": "2026-07-15T08:00:00Z",
            "type": "event_msg",
            "payload": {
                "rate_limits": {
                    "primary": {"used_percent": 10.0, "window_minutes": 120, "resets_at": 1784106000},
                    "secondary": {"used_percent": 30.0, "window_minutes": 60, "resets_at": 1784102400},
                },
                "limit_id": "quota-kernel-fixture",
                "limit_name": "Sanitized quota kernel fixture",
            },
        },
        {
            "timestamp": "2026-07-15T08:15:00Z",
            "type": "event_msg",
            "payload": {
                "rate_limits": {
                    "primary": {"used_percent": 30.0, "window_minutes": 120, "resets_at": 1784106000},
                },
                "limit_id": "quota-kernel-fixture",
                "limit_name": "Sanitized quota kernel fixture",
            },
        },
    ]
    return {
        "modern-full": (full, None),
        "modern-quota-payload": ([_session_meta(), _token_event(quota_at_payload=True)], None),
        "modern-no-quota": ([_session_meta(), _token_event(include_quota=False)], None),
        "modern-partial-quota": ([_session_meta(), _token_event(malformed=True)], None),
        "modern-dual-location-conflict": ([_session_meta(), _dual_location_quota_event()], None),
        "quota-kernel-history": (quota_kernel_history, None),
        "legacy-envelope": ([{"timestamp": "2026-07-14T12:03:00Z", "record_type": "token_count", "input_tokens": 700, "output_tokens": 200, "total_tokens": 900}], None),
        "unknown-records": ([{"timestamp": "2026-07-14T12:04:00Z", "type": "world_state", "payload": {"unknown": True}}, {"timestamp": "2026-07-14T12:05:00Z", "type": "future_record_v99", "payload": {"nested": {"future": 99}}}], None),
        "malformed-tail": ([_session_meta(), _token_event()], '{"timestamp":"2026-07-14T12:09:00Z"'),
        "duplicate-token-count": (duplicate, None),
        "metadata-only-tail": ([_token_event(), _session_meta()], None),
        "root-a-collision": (_content_bearing_root(ROOT_A, "codex-root-a", "Root A red"), None),
        "root-b-collision": (_content_bearing_root(ROOT_B, "codex-root-b", "Root B blue"), None),
        "nested-parent": ([
            parent, _turn_context("2026-07-14T17:01:00Z", "turn-p"),
            _response_message("2026-07-14T17:02:00Z", "user", "Parent thread question"),
            _response_message("2026-07-14T17:03:00Z", "assistant", "Parent thread answer"),
            _token_event(timestamp="2026-07-14T17:04:00Z"),
        ], None),
        "nested-child": ([
            child, _turn_context("2026-07-14T17:11:00Z", "turn-c"),
            _response_message("2026-07-14T17:12:00Z", "user", "Child thread question"),
            _response_message("2026-07-14T17:13:00Z", "assistant", "Child thread answer"),
            _token_event(timestamp="2026-07-14T17:14:00Z"),
        ], None),
        "mirror-pairing": (_mirror_pairing_records(), None),
        "unturned-event-prose": (_unturned_event_prose_records(), None),
        "title-wrapper-window": (_title_wrapper_window_records(), None),
        "session-a-turn-contract": (_session_a_turn_contract_records(), None),
        "session-b-card-wire": (_session_b_card_wire_records(), None),
        "session-c-secondary-tools": (_session_c_secondary_tool_records(), None),
        "session-c-child-proven": (_session_c_child_records(
            session_id=SESSION_C_CHILD, record_id="session-c-child-proven",
            agent_path="/root/session_c_child", nickname="Synthetic Child",
        ), None),
        "session-c-child-ambiguous-a": (_session_c_child_records(
            session_id=SESSION_C_AMBIG_A, record_id="session-c-child-ambiguous-a",
            agent_path="/root/ambiguous", nickname="Ambiguous A",
        ), None),
        "session-c-child-ambiguous-b": (_session_c_child_records(
            session_id=SESSION_C_AMBIG_B, record_id="session-c-child-ambiguous-b",
            agent_path="/root/ambiguous", nickname="Ambiguous B",
        ), None),
        "session-d-reasoning-lifecycle-markers": (
            _session_d_reasoning_lifecycle_marker_records(), None),
        "session-e-native-families": (
            _session_e_native_family_records(), None),
        "claude-collision": ([_session_meta(source="claude", record_id="claude-root", thread_source="claude-root", forked_from_id="claude-root"), {"timestamp": "2026-07-14T12:02:00Z", "type": "assistant", "payload": {"session_id": SHARED_ID}}], None),
        "secret-canary": (_secret_canary_records(), None),
        "empty-source": ([], None),
        "stale-cache": ([_session_meta(source={"kind": "codex"}), {"timestamp": "2025-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "stale_cache_marker", "age_seconds": 999999}}], None),
        "claude-only": ([_session_meta(source="claude", record_id="claude-only", thread_source="claude-only", forked_from_id="claude-only")], None),
        "codex-only": ([_session_meta(record_id="codex-only", thread_source="codex-only", forked_from_id="codex-only")], None),
        "mixed-source": ([_session_meta(source="claude", record_id="mixed-claude", thread_source="mixed-claude", forked_from_id="mixed-claude"), _session_meta(source="codex", record_id="mixed-codex", thread_source="mixed-codex", forked_from_id="mixed-codex")], None),
    }


def _row(requirement_id, owner, sources, capability, state, scenarios, target, requirement):
    targets = [target] if isinstance(target, str) else target
    return {"id": requirement_id, "ownerSession": owner, "sources": sources, "capability": capability,
            "contractState": state, "fixtureScenarios": scenarios, "futureTestTargets": targets, "requirement": requirement}


def _acceptance_matrix() -> dict:
    p = "tests/test_codex_parity_contract.py"
    rows = [
        _row("public-capability-matrix", "S0", ["all"], "public-contract", "supported", ["modern-full"], p, "Public matrix names every outcome, owner, state, reason, and evidence."),
        _row("s1-physical-quota-retention", "S1", ["codex"], "physical-quota-retention", "supported", ["modern-full", "modern-quota-payload", "modern-dual-location-conflict"], "tests/test_codex_fused_ingest.py", "Physically retain root-qualified native quota observations and complete rollout records; S2 owns history selection and user-facing interpretation."),
        _row("s1-db-provider-root-thread-collision-safety", "S1", ["all"], "physical-qualified-identity", "supported", ["root-a-collision", "root-b-collision", "claude-collision"], "tests/test_codex_fused_ingest.py", "Database source-root and thread identities keep colliding provider values distinct; routes, FTS, export, share, and browser storage remain deferred."),
        _row("s1-codex-file-lifecycle-atomicity", "S1", ["codex"], "physical-file-lifecycle", "supported", ["malformed-tail", "metadata-only-tail", "duplicate-token-count"], "tests/test_codex_fused_ingest.py", "Per-file fused ingest atomically retains accounting, physical events, quota facts, thread facts, and its resume watermark across append, requalification, prune, rebuild, and retry."),
        _row("four-codex-reports-reconcile", "S1", ["codex"], "accounting-ingest", "supported", ["duplicate-token-count", "legacy-envelope"], "tests/test_codex_fused_ingest.py", "Existing Codex reports reconcile and preserve their deliberate dedup divergence."),
        _row("s2-quota-interpretation-cli-kernel", "S2", ["codex"], "quota-interpretation", "supported", ["modern-full", "modern-quota-payload", "stale-cache"], ["tests/test_lib_quota.py", "tests/test_codex_quota_projection.py", "tests/test_codex_quota_cli.py", "bin/cctally-codex-quota-test"], "The pure quota kernel and native history, statusline, forecast, blocks, and breakdown CLI interpret root-qualified native windows without slot assumptions."),
        _row("s4-dashboard-quota-reconciliation", "S4", ["all"], "dashboard-quota-reconciliation", "supported", ["modern-full", "modern-quota-payload", "mixed-source", "stale-cache", "root-a-collision", "root-b-collision"], ["tests/test_dashboard_source_read_model.py", "tests/test_dashboard_source_invalidation.py"], "Dashboard quota blocks and alerts reconcile to the S2 native CLI kernels without blending independent windows."),
        _row("source-derived-project-attribution", "S3", ["all"], "project-attribution", "supported", ["root-a-collision", "root-b-collision"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Cwd, git, projects, diff, and range cost remain provider-qualified."),
        _row("provider-qualified-collision-safety", "S1", ["all"], "qualified-identity", "deferred", ["root-a-collision", "root-b-collision", "claude-collision"], "tests/test_codex_fused_ingest.py", "Qualified identities protect DB, routes, FTS, modals, exports, share, and browser storage."),
        _row("dashboard-source-switch-stale-safety", "S4", ["all"], "dashboard-source-state", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "malformed-tail"], ["tests/test_dashboard_source_read_model.py", "tests/test_dashboard_source_invalidation.py"], "Published source state fails closed or retains the complete prior provider generation; it never cross-falls back."),
        _row("provider-ingest-lifecycle", "S2", ["all"], "provider-ingest", "supported", ["malformed-tail", "metadata-only-tail", "stale-cache", "empty-source"], ["tests/test_codex_fused_ingest.py", "tests/test_codex_hook_lifecycle.py", "tests/test_codex_quota_cli.py"], "One S1 all-root sync per Codex lifecycle tick preserves no-sync, throttle, lock, truncation, rebuild, prune, and metadata-tail semantics."),
        _row("autonomous-codex-alerts", "S2", ["codex"], "lifecycle-alerts", "supported", ["modern-full"], ["tests/test_quota_alerts.py", "tests/test_codex_hook_lifecycle.py", "tests/test_codex_hooks_setup.py"], "Opt-in pure-Codex quota and budget alerts integrate additive setup-managed hooks, due-root evaluation, and owned-only uninstall."),
        _row("source-aware-cli-share-identity", "S3", ["all"], "share-identity", "supported", ["mixed-source", "root-a-collision"], ["tests/test_source_aware_share.py", "bin/cctally-source-aware-test"], "CLI share artifacts and the source-bearing share kernel preserve opaque source-qualified identity."),
        _row("source-aware-dashboard-share-identity", "S5", ["all"], "share-identity", "deferred", ["mixed-source", "root-a-collision"], "tests/test_source_aware_dashboard_share.py", "Dashboard share, composer, and history preserve source identity."),
        _row("s4-dashboard-share-backend-contract", "S4", ["all"], "dashboard-share-backend", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "root-a-collision", "root-b-collision", "malformed-tail"], ["tests/test_dashboard_source_share.py", "tests/test_dashboard_source_routes.py"], "Provider-qualified dashboard detail routes resolve bounded relational native data with collision safety, while canonical source-bearing share snapshots drive render, digest identity, composition, presets, and history before S5 adds source controls."),
        _row("native-codex-conversation-stack", "S8", ["codex"], "conversation-reader-ui", "supported", ["modern-full", "nested-child"], ["dashboard/web/src/conversations/ConversationRail.test.tsx", "dashboard/web/src/conversations/ComparisonView.test.tsx", "dashboard/web/src/hooks/useConversation.test.tsx", "dashboard/web/e2e/codex-reader.spec.ts"], "The React browse/reader/find/export/live-tail UI surfaces (plus reading position and cross-source combined presentation) consume the S7 backend's normalized qualified identities."),
        _row("codex-anonymization-privacy-gate", "S7", ["codex"], "conversation-privacy", "supported", ["secret-canary"], "tests/test_codex_conversation_api.py", "Roots, usernames, encoded paths, and secret patterns obey the same privacy gate."),
        _row("s7-conversation-route-exposure", "S7", ["all"], "conversation-routes", "supported", ["modern-full", "nested-parent", "claude-only"], ["tests/test_codex_conversation_api.py", "tests/test_conversation_endpoints.py"], "Dual-form entity routes (v1. lexical) and the strict ?source= collection routes (browse, facets, search) expose normalized conversations while the bare Claude surface stays byte-identical."),
        _row("s7-conversation-find", "S7", ["codex"], "conversation-find", "supported", ["modern-full", "mirror-pairing"], ["tests/test_codex_conversation_normalization.py", "tests/test_codex_conversation_api.py"], "In-conversation find anchors both providers with one item_key contract, honest FTS/LIKE selection, and mirror-pair collapse."),
        _row("s7-transcript-export-byte-parity", "S7", ["all"], "transcript-cli", "supported", ["modern-full", "root-a-collision"], ["tests/test_transcript_cli.py"], "The transcript CLI takes the dual-form export positional, --speed (Codex-only, resolved-source), and --source search, and its export byte-matches the HTTP export in both anonymize and raw modes."),
        _row("s7-conversation-payload-readback", "S7", ["codex"], "conversation-payload", "supported", ["modern-full"], ["tests/test_codex_conversation_normalization.py", "tests/test_codex_conversation_api.py"], "The Codex payload readback selects by opaque block_key + which={call,output}, serves beyond-cap content from the re-read record, validates gone against the stored full record, and guards containment."),
        _row("s7-targeted-ingest-live-tail", "S7", ["codex"], "conversation-live-tail", "supported", ["nested-parent", "nested-child", "modern-full"], ["tests/test_codex_conversation_live_tail.py", "tests/test_codex_conversation_frontier.py", "tests/test_codex_dashboard_conversation_events.py"], "Targeted Codex ingest (only_paths + targeted_clean, whole-tree-bypass) and the qualified live-tail SSE with the budgeted directory-frontier child discovery join new child threads mid-watch."),
        _row("s7-conversation-media-capability-gated", "S7", ["codex"], "conversation-media", "unavailable", ["modern-full"], ["tests/test_codex_conversation_api.py"], "Codex media returns an explicit capability_unsupported response (degrade explicitly, never zero-fill) until real renderable media is shown to exist in rollouts."),
        _row("synthetic-source-coverage", "S0", ["all"], "synthetic-corpus", "supported", ["claude-only", "codex-only", "mixed-source", "empty-source", "stale-cache", "malformed-tail", "duplicate-token-count", "root-a-collision", "nested-child"], p, "Corpus covers source-only, mixed, empty, stale, malformed, collision, and nested cases."),
        _row("existing-codex-compatibility", "S1", ["codex"], "compatibility", "supported", ["legacy-envelope", "duplicate-token-count"], "tests/test_codex_fused_ingest.py", "Existing accounting, budget, pricing, aliases, and deliberate divergences remain compatible."),
        _row("s5-s8-ui-qa-gates", "S9", ["all"], "ui-certification", "deferred", [], "dashboard/web/src/conversations/CodexConversationReader.test.tsx", "React/CSS tests, typecheck/build, dashboard goldens, and browser QA gate S5 and S8."),
        _row("root-docs-cover-both-sources", "S9", ["all"], "documentation", "deferred", [], "tests/test_codex_parity_certification.py", "Root help, commands, dashboard, transcript/share/privacy, config, setup, and doctor docs cover both sources."),
        _row("production-scale-final-certification", "S9", ["all"], "production-scale-certification", "deferred", [], "tests/test_codex_parity_certification.py", "S9 certifies production-scale source-qualified behavior."),
        _row("tui-freeze-explicit-disposition", "S9", ["all"], "tui-governance", "not_applicable", [], "tests/test_codex_parity_certification.py", "TUI presentation stays under the approved bugfix-only freeze."),
        _row("schema-drift-tolerance", "S0", ["codex"], "schema-tolerance", "supported", ["modern-full", "modern-quota-payload", "unknown-records", "legacy-envelope"], p, "Observed schema is tolerant and captures both quota locations."),
        _row("missing-rate-limits-degrades-quota-only", "S0", ["codex"], "quota-degradation", "supported", ["modern-no-quota", "modern-partial-quota", "malformed-tail"], p, "Missing or malformed quota preserves recoverable accounting."),
        _row("report-per-source-never-blended", "S3", ["all"], "report", "supported", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Report and dollars per percent use S2 quota kernels but remain per source."),
        _row("percent-breakdown-per-source", "S2", ["all"], "percent-breakdown", "supported", ["modern-full"], ["tests/test_codex_quota_projection.py", "tests/test_codex_quota_cli.py", "bin/cctally-codex-quota-test"], "Native Codex quota breakdown keeps percent milestones and query-time cost correlation per source root and logical limit."),
        _row("five-hour-breakdown-per-source", "S2", ["all"], "five-hour-breakdown", "deferred", ["modern-full"], "tests/test_codex_quota.py", "Breakdowns preserve native quota identity rather than slot aliases."),
        _row("codex-refresh-is-local-rollout-reread", "S2", ["codex"], "provider-live-refresh", "unavailable", ["modern-full"], "tests/test_codex_quota.py", "Codex has no provider-live or OAuth refresh analogue."),
        _row("codex-local-rollout-quota-freshness", "S2", ["codex"], "local-rollout-quota-freshness", "supported", ["modern-full", "stale-cache"], ["tests/test_lib_quota.py", "tests/test_codex_quota_cli.py", "tests/test_codex_quota_doctor.py"], "Local rollout captures surface native-window freshness only; this is not a provider-live refresh."),
        _row("codex-cache-hit-rate-not-applicable", "S3", ["codex"], "cache-hit-rate", "not_applicable", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py"], "Codex cached input is not relabeled as Claude cache hit rate."),
        _row("codex-token-reuse-forensics", "S3", ["codex"], "token-reuse", "supported", ["modern-full"], ["tests/test_source_aware_analytics.py", "tests/test_source_aware_cli.py", "bin/cctally-source-aware-test"], "Cached input is a truthful Codex token-reuse outcome."),
        _row("codex-pricing-coverage-supported", "S1", ["codex"], "pricing-coverage", "supported", ["modern-full"], "tests/test_codex_fused_ingest.py", "Existing Codex pricing coverage and drift semantics remain supported."),
        _row("codex-budget-existing-semantics", "S1", ["codex"], "budget", "supported", ["modern-full"], "tests/test_codex_fused_ingest.py", "Existing Codex budget calculation and actual/projected semantics remain supported."),
        _row("codex-title-first-prompt-fallback", "S6", ["codex"], "conversation-title", "supported", ["modern-full"], ["tests/test_codex_conversation_normalization.py"], "Initial title uses the first meaningful user prompt."),
        _row("codex-threading-uses-thread-metadata", "S6", ["codex"], "conversation-threading", "supported", ["nested-parent", "nested-child"], ["tests/test_codex_conversation_normalization.py"], "Nesting uses thread metadata, not filenames."),
        _row("codex-native-shell-patch-card-wire", "S6", ["codex"], "conversation-tool-cards", "supported", ["session-b-card-wire"], ["tests/test_codex_conversation_normalization.py", "tests/test_codex_conversation_api.py"], "Bounded non-executing harness decoding exposes provider-truthful terminal/output/patch cards, evidence-gated patch correlation, raw fallbacks, and full event payload readback."),
        _row("codex-secondary-tool-agent-link-wire", "S6", ["codex"], "conversation-secondary-tools", "supported", ["session-c-secondary-tools", "session-c-child-proven", "session-c-child-ambiguous-a", "session-c-child-ambiguous-b"], ["tests/test_codex_conversation_normalization.py", "tests/test_codex_conversation_api.py"], "Bounded plan, web, MCP, and agent cards preserve native vocabulary and results; exact same-root parent plus canonical task-name/agent-path proof exposes only an opaque child key, while ambiguous or unsupported shapes remain raw and unlinked."),
        _row("codex-reasoning-lifecycle-marker-wire", "S6", ["codex"], "conversation-reasoning-lifecycle-markers", "supported", ["session-d-reasoning-lifecycle-markers"], ["tests/test_codex_conversation_normalization.py", "tests/test_codex_conversation_api.py"], "Reasoning title, provider summary, body, and absence remain distinct; only uniquely owned redundant lifecycle rows fold; closed trailing Git and memory markers become privacy-safe metadata while authored lookalikes and raw payloads remain available."),
        _row("codex-native-family-disposition", "S6", ["codex"], "conversation-native-family-disposition", "supported", ["session-e-native-families"], ["tests/test_codex_conversation_normalization.py", "dashboard/web/e2e/session-e-polish.spec.ts"], "World state and inter-agent metadata remain replay-safe physical control-plane evidence; turn context supplies only sticky turn/model attribution; unknown native families remain physical-only without search, outline, export, or reader noise."),
        _row("codex-anon-plan-includes-roots", "S7", ["codex"], "anon-plan", "supported", ["root-a-collision"], "tests/test_codex_conversation_api.py", "The anonymization plan includes provider roots and labels."),
        _row("debug-backend-source-counts", "S4", ["all"], "debug-diagnostics", "supported", ["mixed-source", "stale-cache", "root-a-collision"], ["tests/test_dashboard_debug_backend.py", "tests/test_dashboard_source_invalidation.py"], "Loopback-only backend diagnostics expose source-aware aggregate counts and opaque versions without private identities."),
        _row("reading-position-qualified-key", "S8", ["all"], "reading-position", "supported", ["root-a-collision", "root-b-collision"], ["dashboard/web/src/store/readingPosition.test.ts", "dashboard/web/src/store/urlRouting.test.ts", "dashboard/web/e2e/codex-reader.spec.ts"], "Reading positions use opaque qualified conversation keys."),
        _row("dashboard-s5-after-293-s4", "S5", ["all"], "dashboard-sequencing", "deferred", [], "tests/test_source_aware_dashboard.py", "S5 waits for issue 293 S4 or an approved ownership split."),
        _row("conversation-phase-independently-deferrable", "S6", ["all"], "conversation-phase", "deferred", [], "tests/test_codex_conversation_normalization.py", "S6 through S8 may defer without falsely certifying the whole epic."),
    ]
    return {"schemaVersion": SCHEMA_VERSION, "requiredCapabilityFamilies": ["accounting", "quota", "analytics-share", "dashboard", "conversations", "lifecycle-governance"], "requirements": rows}


def _inventory_entry(variant, scenario, selector, path, required, *types):
    return {"variant": variant, "scenario": scenario, "recordSelector": selector, "path": path,
            "required": required, "types": list(types)}


def _field_inventory() -> list[dict]:
    e = _inventory_entry
    token = {"type": "event_msg", "payload.type": "token_count"}
    inventory = [
        e("modern-session-meta", "modern-full", {"type": "session_meta"}, "timestamp", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "type", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload", True, "object"),
        e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.id", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.session_id", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.cwd", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.git", True, "object"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.source", True, "string"), e("modern-session-meta", "stale-cache", {"type": "session_meta"}, "payload.source", False, "object"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.thread_source", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.forked_from_id", False, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.model_provider", True, "string"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.context_window", False, "number"), e("modern-session-meta", "modern-full", {"type": "session_meta"}, "payload.model_context_window", False, "number"),
        e("turn-context", "modern-full", {"type": "turn_context"}, "payload.turn_id", True, "string"), e("turn-context", "modern-full", {"type": "turn_context"}, "payload.model", False, "string"), e("turn-context", "modern-full", {"type": "turn_context"}, "payload.model_context_window", False, "number"),
        e("token-count-info-quota", "modern-full", token, "payload.type", True, "string"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage", True, "object"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.input_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.cached_input_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.output_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.last_token_usage.reasoning_output_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.total_token_usage.total_tokens", True, "number"), e("token-count-info-quota", "modern-full", token, "payload.info.rate_limits", True, "object"), e("token-count-payload-quota", "modern-quota-payload", token, "payload.rate_limits", True, "object"),
    ]
    for scenario, path in (("modern-full", "payload.info.rate_limits"), ("modern-quota-payload", "payload.rate_limits")):
        for key, types in (("primary", ("object",)), ("secondary", ("object", "null")), ("credits", ("object", "null")), ("plan_type", ("string", "null")), ("limit_id", ("string",)), ("limit_name", ("string", "null")), ("individual_limit", ("number", "object", "null")), ("rate_limit_reached_type", ("string", "null"))):
            inventory.append(e("quota-window", scenario, token, f"{path}.{key}", key == "primary", *types))
        for window in ("primary", "secondary"):
            for key in ("used_percent", "window_minutes", "resets_at"):
                inventory.append(e("quota-window", scenario, token, f"{path}.{window}.{key}", True, "number"))
    response_fields = {
        "message": (("role", "string"), ("content", "array")),
        "reasoning": (("content", "array"), ("encrypted_content", "string"), ("summary", "array")),
        "function_call": (("arguments", "string"), ("call_id", "string"), ("name", "string"), ("namespace", "string")),
        "function_call_output": (("call_id", "string"), ("output", "object")),
        "custom_tool_call": (("call_id", "string"), ("input", "object"), ("name", "string"), ("status", "string")),
        "custom_tool_call_output": (("call_id", "string"), ("output", "object")),
        "tool_search_call": (("arguments", "object"), ("call_id", "string"), ("execution", "object"), ("status", "string")),
        "tool_search_output": (("call_id", "string"), ("execution", "object"), ("status", "string"), ("tools", "array")),
        "web_search_call": (("action", "string"), ("status", "string")),
    }
    for record_type, fields in response_fields.items():
        for field, json_type in fields:
            inventory.append(e("response-item", "modern-full", {"type": "response_item", "payload.type": record_type}, f"payload.{field}", True, json_type))
    inventory += [
        e("response-item-exec", "session-b-card-wire",
          {"type": "response_item", "payload.type": "custom_tool_call",
           "payload.call_id": "exec-ok"}, "payload.input", True, "string"),
        e("response-item-exec-output-array", "session-b-card-wire",
          {"type": "response_item", "payload.type": "custom_tool_call_output",
           "payload.call_id": "exec-ok"}, "payload.output", True, "array"),
        e("response-item-exec-output-string", "session-b-card-wire",
          {"type": "response_item", "payload.type": "custom_tool_call_output",
           "payload.call_id": "exec-string"}, "payload.output", True, "string"),
        e("response-item-update-plan", "session-c-secondary-tools",
          {"type": "response_item", "payload.type": "function_call",
           "payload.call_id": "plan-ok"}, "payload.arguments", True, "string"),
        e("response-item-web-search", "session-c-secondary-tools",
          {"type": "response_item", "payload.type": "web_search_call",
           "payload.id": "web-ok"}, "payload.action", True, "object"),
        e("response-item-agent-op", "session-c-secondary-tools",
           {"type": "response_item", "payload.type": "function_call",
           "payload.call_id": "spawn-proven"}, "payload.arguments", True, "string"),
        e("response-item-reasoning-summary", "session-d-reasoning-lifecycle-markers",
          {"type": "response_item", "payload.type": "reasoning"},
          "payload.summary", True, "array"),
        e("response-item-marker-message", "session-d-reasoning-lifecycle-markers",
          {"type": "response_item", "payload.type": "message",
           "payload.role": "assistant"}, "payload.content", True, "array"),
        e("session-meta-agent-path", "session-c-child-proven",
          {"type": "session_meta"}, "payload.agent_path", True, "string"),
        e("session-meta-agent-parent", "session-c-child-proven",
          {"type": "session_meta"}, "payload.parent_thread_id", True, "string"),
        e("session-meta-agent-role", "session-c-child-proven",
          {"type": "session_meta"}, "payload.agent_role", True, "string"),
        e("session-meta-agent-nickname", "session-c-child-proven",
          {"type": "session_meta"}, "payload.agent_nickname", True, "string"),
        e("session-meta-agent-source", "session-c-child-proven",
          {"type": "session_meta"}, "payload.source", True, "object"),
        e("event-web-search-completion", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "web_search_end",
           "payload.call_id": "web-ok"}, "payload.results", True, "array"),
        e("event-web-search-action", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "web_search_end",
           "payload.call_id": "web-ok"}, "payload.action", True, "object"),
        e("event-web-search-error", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "web_search_end",
           "payload.call_id": "web-error"}, "payload.error", True, "string"),
        e("event-mcp-invocation", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "mcp_tool_call_end",
           "payload.call_id": "mcp-ok"}, "payload.invocation", True, "object"),
        e("event-mcp-result", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "mcp_tool_call_end",
           "payload.call_id": "mcp-ok"}, "payload.result", True, "object"),
        e("event-mcp-duration", "session-c-secondary-tools",
          {"type": "event_msg", "payload.type": "mcp_tool_call_end",
           "payload.call_id": "mcp-ok"}, "payload.duration", True, "object"),
    ]
    event_fields = {
        "agent_message": (("message", "string"), ("phase", "string")), "agent_reasoning": (("text", "string"),),
        "task_started": (("collaboration_mode_kind", "string"), ("model_context_window", "number"), ("turn_id", "string")),
        "task_complete": (("duration_ms", "number"), ("last_agent_message", "string"), ("turn_id", "string")),
        "context_compacted": (), "patch_apply_end": (("call_id", "string"), ("changes", "array"), ("success", "boolean"), ("turn_id", "string")),
        "mcp_tool_call_end": (("call_id", "string"), ("duration", "number"), ("invocation", "object"), ("result", "object")),
        "web_search_end": (("action", "string"), ("call_id", "string"), ("query", "string")),
        "user_message": (("images", "array"), ("local_images", "array"), ("message", "string"), ("text_elements", "array")),
    }
    for record_type, fields in event_fields.items():
        inventory.append(e("event-taxonomy", "modern-full", {"type": "event_msg", "payload.type": record_type}, "payload.type", True, "string"))
        for field, json_type in fields:
            inventory.append(e("event-taxonomy", "modern-full", {"type": "event_msg", "payload.type": record_type}, f"payload.{field}", True, json_type))
    for field, json_type in (
        ("status", "string"), ("stdout", "string"), ("stderr", "string"),
        ("success", "boolean"), ("changes", "array"),
    ):
        inventory.append(e(
            "event-patch-detail", "session-b-card-wire",
            {"type": "event_msg", "payload.type": "patch_apply_end",
             "payload.call_id": "direct-patch"},
            f"payload.{field}", True, json_type))
    inventory += [
        e("session-e-world-state-full", "session-e-native-families",
          {"type": "world_state", "payload.full": True},
          "payload.state", True, "object"),
        e("session-e-world-state-delta", "session-e-native-families",
          {"type": "world_state", "payload.full": False},
          "payload.state", True, "object"),
        e("session-e-inter-agent", "session-e-native-families",
          {"type": "inter_agent_communication_metadata"},
          "payload.trigger_turn", True, "boolean"),
        e("session-e-malformed-turn-context", "session-e-native-families",
          {"type": "turn_context", "payload.future_context_field": {"preserve": True}},
          "payload.future_context_field", True, "object"),
        e("session-e-unknown-future", "session-e-native-families",
          {"type": "future_record_v100"}, "payload.opaque_id", True, "string"),
        e("world-state", "unknown-records", {"type": "world_state"},
          "payload", True, "object"),
        e("legacy-envelope", "legacy-envelope", {"record_type": "token_count"},
          "record_type", True, "string"),
    ]
    return inventory


def _manifest() -> dict:
    return {"corpusId": CORPUS_ID, "schemaVersion": SCHEMA_VERSION, "observedAsOf": "2026-07-22",
            "sources": list(SOURCES), "capabilityStates": list(CAPABILITY_STATES),
            "schemaPosture": {"mode": "version-tolerant-feature-detection", "unknownRecords": "ignore-or-preserve-without-aborting-accounting", "missingRateLimits": "degrade-quota-only", "partialFinalLine": "retain-valid-prefix", "snapshotIsClosedSchema": False},
            "observedRecordTypes": ["session_meta", "turn_context", "event_msg", "response_item", "world_state", "inter_agent_communication_metadata", "future_record_v100", "record_type"],
            "quotaWindowFields": ["observedSlot", "limitId", "limitName", "windowMinutes", "usedPercent", "resetsAtUtc", "capturedAtUtc", "planType", "individualLimit", "reachedType"],
            "identityFields": ["version", "source", "resourceKind", "sourceRootKey", "nativeKey", "parentKey"],
            "combinedArithmetic": {"additive": list(ADDITIVE_MEASURES), "nonAdditive": list(NON_ADDITIVE_MEASURES)},
            "requiredCapabilityFamilies": ["accounting", "quota", "analytics-share", "dashboard", "conversations", "lifecycle-governance"],
            "scenarios": {name: f"rollouts/{name}.jsonl" for name in REQUIRED_SCENARIOS}, "fieldInventory": _field_inventory(),
            "history": [
                {"version": 1, "date": "2026-07-14", "change": "Initial synthetic snapshot from issue 294 audit"},
                {"version": 2, "date": "2026-07-17", "change": "#294 S6 conversation normalization: content-bearing collision roots and nested parent/child, new mirror-pairing / unturned-event-prose / title-wrapper-window scenarios, and a genuine Claude-format seed sharing SHARED_ID."},
                {"version": 3, "date": "2026-07-21", "change": "#330 Task A canonical Codex conversation-turn and injected-content contract scenario."},
                {"version": 4, "date": "2026-07-21", "change": "#331 Task A card-ready Codex conversation exec, output, and patch wire scenario."},
                {"version": 5, "date": "2026-07-22", "change": "#332 Task A conversation plan, web, MCP, agent-operation, exact child-link, ambiguous-link rejection, and local-image-reference fixture family."},
                {"version": 6, "date": "2026-07-22", "change": "#334 Task A conversation reasoning title/body/absence, lifecycle correlation/rejection, and closed trailing Git/memory marker fixture family."},
                {"version": 7, "date": "2026-07-22", "change": "#335 Session E conversation fixtures for retained world-state, inter-agent metadata, turn-context, unknown-family, and compact reader privacy/presentation coverage."},
            ]}


def _safe_output(out_dir: Path) -> Path:
    resolved = out_dir.resolve()
    if resolved in {REPO_ROOT.resolve(), (REPO_ROOT / "tests" / "fixtures").resolve()}:
        raise ValueError("refusing to replace repository root or fixture parent")
    if resolved.exists() and resolved != DEFAULT_OUT.resolve():
        manifest_path = resolved / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise ValueError("refusing to replace unowned output directory") from None
        if manifest.get("corpusId") != CORPUS_ID or manifest.get("schemaVersion") != SCHEMA_VERSION:
            raise ValueError("refusing to replace unowned output directory")
    return resolved


def build(out_dir: Path = DEFAULT_OUT) -> None:
    out_dir = _safe_output(Path(out_dir))
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for name, (records, malformed) in _scenarios().items():
        _write_jsonl(out_dir / "rollouts" / f"{name}.jsonl", records, malformed)
    # #294 S6: a genuine Claude-format seed (NOT a Codex rollout) sharing
    # SHARED_ID, placed under claude-seed/ so tests can ingest it through the
    # Claude sync_cache path and prove cross-provider assembly isolation.
    _write_jsonl(out_dir / "claude-seed" / f"{SHARED_ID}.jsonl", _claude_seed_records())
    # #334 Task B: a deterministic Claude Thinking baseline for the paired
    # real-browser comparison with native Codex Reasoning.
    _write_jsonl(out_dir / "claude-seed" / f"{CLAUDE_THINKING_SESSION}.jsonl",
                 _claude_thinking_reference_records())
    _write_json(out_dir / "manifest.json", _manifest())
    _write_json(out_dir / "acceptance-matrix.json", _acceptance_matrix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    build(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
