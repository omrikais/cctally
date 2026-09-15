#!/usr/bin/env python3
"""Build seeded SQLite fixtures and an INDEPENDENT oracle for `cctally explain`.

Writes one `.local/share/cctally/{stats,cache}.db` plus an `oracle.json` per
scenario under `tests/fixtures/explain/<scenario>/`.

**The one rule that makes this worth anything.** The oracle is computed
arithmetically from the seeded raw facts. This module must never import
`_lib_diagnosis`, `_cctally_diagnosis_sources` or `_cctally_diagnosis`, and
must never record diagnosis output as an expected value. A gate that clones
its result to build its baseline is an arithmetic identity: it would pass over
any diagnosis at all, including one that had stopped classifying.

The classification rule is therefore RESTATED here as arithmetic — the floor,
the support minima, the tie epsilon, the half-open window and the
pool-compatible block join — rather than imported. Restating it is the point.
If the kernel's rule drifts, the oracle keeps stating the rule these fixtures
were designed around and the harness fails.

`_lib_pricing` is imported, and that is not a diagnosis import: the diagnosis
reads the same production pricing table, so agreeing about the price of a
token is agreement about a fact rather than about the classification under
test. Two scenarios need a share of exactly 0.20 and six exactly equal
subjects, and they get that from `EQUAL_PRICED_MODELS` over identical token
counts — every entry reprices to the same float. `cost_usd_raw` cannot supply
it, because the diagnosis reprices at read time and never reads that column;
one scenario writes a deliberately wrong value there to prove it.

Run: `bin/build-explain-fixtures.py` (idempotent — overwrites existing DBs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_conversations_db,
    create_stats_db,
    fixture_timestamp_utc,
    seed_codex_conversation_thread,
    seed_codex_session_entry,
    seed_codex_session_file,
    seed_session_entry,
    seed_session_file,
)
from _lib_pricing import (  # noqa: E402
    CLAUDE_MODEL_PRICING, _calculate_codex_entry_cost, _calculate_entry_cost,
    claude_usage_dict,
)
# A fixture PRECONDITION, not an expectation. The header's rule forbids
# importing `_lib_diagnosis`, `_cctally_diagnosis_sources` and
# `_cctally_diagnosis`, because agreeing with the classifier under test would
# make the oracle an identity. This value is neither: it is the marker a real
# store carries, and a stale copy of it disarms nothing — the contract check
# clears every normalized Codex message, re-derives them from events that
# yield no prompts, and turns `codex-short-context` into a silent
# no-contributor scenario. The test helper already imports it, so restating it
# here also left the two disagreeing about one decision.
from _lib_codex_conversation import (  # noqa: E402
    CODEX_CONVERSATION_CONTRACT_VERSION,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/explain"

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)

OPUS = "claude-opus-4-20250514"
SONNET = "claude-sonnet-4-20250514"
HAIKU = "claude-haiku-4-5"
SPARE_MODELS = ("claude-opus-4-1", "claude-sonnet-4-5", "claude-3-5-haiku-latest")

# Six models that carry IDENTICAL rates in `CLAUDE_MODEL_PRICING`, asserted
# below. Two scenarios need every entry to cost the same so that "six equal
# subjects" and "a share of exactly one fifth" are exact rather than
# approximate. They used to get that by pinning `session_entries.cost_usd_raw`,
# which the diagnosis no longer reads: it reprices every entry from the
# embedded table (spec §3). Equal token counts under equally priced models is
# the only construction that survives repricing.
EQUAL_PRICED_MODELS = (
    "claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-latest", "claude-sonnet-4-6",
)
if len({tuple(sorted(CLAUDE_MODEL_PRICING[m].items()))
        for m in EQUAL_PRICED_MODELS}) != 1:
    raise SystemExit(
        "EQUAL_PRICED_MODELS are no longer equally priced; the "
        "control-no-contributor and boundary-on-floor scenarios rest on it"
    )

CODEX_STANDARD = "gpt-5.3-codex"
CODEX_SPARK = "gpt-5.3-codex-spark"
CODEX_UNKNOWN = "a-model-from-the-future"

COMPACTION_BODY = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The conversation is summarized below:"
)

ORACLE_SHARE_FLOOR = 0.20
ORACLE_TIE_EPSILON_USD = 1e-9
ORACLE_MIN_DISTINCT_SUBJECTS = 2
ORACLE_MIN_PRICED_ENTRIES = 20
# The kernel separates these two deliberately: the share-floor slack is part of
# the PUBLISHED rule (`DIAGNOSIS_SHARE_FLOOR_EPSILON`, on the wire beside the
# floor), and the coverage slack is an internal float guard. They hold the same
# value today, so restating the share floor with the coverage constant changes
# no number — but the oracle's whole worth is that it restates the rules
# independently, and restating them wrongly is exactly the drift it exists to
# catch.
ORACLE_SHARE_FLOOR_EPSILON = 1e-9
ORACLE_COVERAGE_EPSILON = 1e-9
ORACLE_WITHHOLD_MIN_COVERAGE = 0.50
ORACLE_HIGH_MIN_SUPPORT = 20
ORACLE_MEDIUM_MIN_SUPPORT = 5
ORACLE_MEDIUM_MIN_COVERAGE = 0.80

# Registry order, restated. It is the tie-break after observed USD in the
# cross-class ranking, so the oracle has to know it to state a rank.
ORACLE_REGISTRY_ORDER = ("model_mix", "project_concentration",
                         "session_concentration", "five_hour_bursts",
                         "cache_churn", "short_high_context",
                         "subagent_fanout")

ORACLE_WITHHELD_PRECEDENCE = (
    "provider_unavailable", "transcripts_not_visible",
    "retained_range_mismatch", "pricing_unavailable",
    "unattributed_evidence", "stale_evidence", "signal_unavailable",
    "insufficient_population", "calculation_failed",
)

# The three conversation-derived classes (#620 S3). Their evaluators land in
# S3 Task 2; until then the CLI resolves a plan that permits them, finds no
# loader behind it, and reports that the signal could not be established. The
# oracle RESTATES that disposition rather than importing it, which is the
# whole point of an independent oracle: if the adapter starts publishing a
# measured verdict here, this file is what notices.
ORACLE_S3_CLASSES = ("cache_churn", "short_high_context", "subagent_fanout")
ORACLE_S3_UNEVALUATED_CODE = "signal_unavailable"

# The S3 rules, RESTATED. Every constant below is written out here rather than
# imported, for the same reason the share floor and the support minima are: an
# oracle that read the kernel's own numbers would agree with a kernel that had
# changed them.
ORACLE_CACHE_FAILURE_CACHE_FLOOR = 20_000
ORACLE_CACHE_FAILURE_CREATE_FLOOR = 20_000
ORACLE_CACHE_FAILURE_COLLAPSE_FRACTION = 0.5
ORACLE_CACHE_FAILURE_RECREATE_FRACTION = 0.75
ORACLE_MAX_HUMAN_TURNS = 3
ORACLE_MIN_WINDOW_FRACTION = 0.80
ORACLE_MIN_SUBAGENT_BUCKETS = 2
ORACLE_MIN_DISTINCT_S3_SUBJECTS = 1

# The per-model context window, restated from the statusline's table. Only the
# ids these fixtures actually use appear here; an id absent from this map is
# an unknown capacity, which is the state `unknown_context_window` describes.
ORACLE_CONTEXT_WINDOWS = {
    OPUS: 200_000, SONNET: 200_000, HAIKU: 200_000,
}

# Restated, exactly like the floor and the support minima above: a conversation
# presenting more human-candidate rows than this cannot be decided, because
# `entry_type` is not decisive and deciding a candidate costs a normalization.
# Restating it rather than importing it is the point — if the kernel's budget
# drifts, the oracle keeps stating the bound these fixtures were designed
# around and the harness fails.
ORACLE_CONVERSATION_NORMALIZE_BUDGET_ROWS = 2_000

# A model that IS priced and publishes NO context window. Every Claude id
# carrying `sonnet`, `opus` or `haiku` resolves to 200,000 through the family
# default, so an unknown capacity needs an id outside those families.
CAPACITY_LESS_MODEL = "claude-fable-5"
if CAPACITY_LESS_MODEL not in CLAUDE_MODEL_PRICING:
    raise SystemExit(
        f"{CAPACITY_LESS_MODEL} is no longer priced; the unknown-context-window "
        "scenario needs a model that is priced and has no published window"
    )
if CAPACITY_LESS_MODEL in ORACLE_CONTEXT_WINDOWS:
    raise SystemExit(
        f"{CAPACITY_LESS_MODEL} now has a stated window; that scenario would "
        "assert the opposite of what it was built for"
    )

# The fixed aggregate subject each S3 class contributes, restated.
ORACLE_S3_SUBJECT_KEY = {
    "cache_churn": "qualifying-set/cache-churn",
    "short_high_context": "qualifying-set/short-high-context",
    "subagent_fanout": "qualifying-set/subagent-fanout",
}


def _s3_class_oracle(contributor_class: str, *, not_applicable: bool,
                     code: str) -> dict:
    """One S3 class's expected shape.

    A class that never shaped subjects publishes no figure it never measured,
    so every attributed dimension is ABSENT rather than zero — the same rule
    the provider-preempted branch of `_class_oracle` applies.
    """
    # `None`, not `"low"`. Confidence is a statement ABOUT a measurement and
    # this class made none, which is what the server publishes here too. The
    # field is read only inside the harness's per-row loop and this shape has
    # no rows, so the old `"low"` was inert — and an inert wrong value is the
    # one a later change silently starts trusting.
    base = {"contributorClass": contributor_class, "supportUnits": None,
            "usdCoverage": None, "confidence": None, "subjects": []}
    if not_applicable:
        # Codex retains a cached-input ratio and no loss predicate, so
        # prompt-cache churn is never measurable there. It is a capability
        # statement, so an unreadable accounting store does not overturn it.
        return {**base, "verdict": "not_applicable", "code": None}
    return {**base, "verdict": "withheld", "code": code}

BASELINE_START = WINDOW_START - (WINDOW_END - WINDOW_START)


def _iso(value: dt.datetime) -> str:
    return fixture_timestamp_utc(value)


def _iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _oracle_pool(name: str | None) -> str | None:
    normalized = (name or "").strip().lower()
    return normalized if "-codex-spark" in normalized else None


class Seed:
    """The raw facts of one scenario, and the arithmetic over them."""

    def __init__(self) -> None:
        self.claude: list[dict] = []
        self.codex: list[dict] = []
        self.claude_blocks: list[dict] = []
        self.codex_windows: list[dict] = []
        # The normalized transcript rows, and the Codex thread metadata the
        # fan-out predicate reads. A scenario that seeds NO conversation rows
        # writes no `conversations.db` at all, which is the shape every
        # accounting-only scenario keeps.
        self.conversation_rows: list[dict] = []
        # The Codex half of the transcript store. Kept apart from
        # `conversation_rows`, which is the CLAUDE stream a Codex oracle must
        # never see, so the `_build` guard below keeps meaning what it says.
        self.codex_conversation_rows: list[dict] = []
        self.codex_events: list[dict] = []
        self.codex_threads: dict[tuple, dict] = {}
        # A scenario-level fact about the STORE, not about the diagnosis: the
        # `provider-unavailable` scenario drops one provider's accounting
        # table, so that provider's read cannot succeed at all.
        self.drop_codex_tables = False
        self.forced_provider_cause: str | None = None
        # Which provider this oracle describes. A scenario may seed both
        # providers — `provider-unavailable` does — and a diagnosis never
        # merges them, so the oracle must not either.
        self.source = "claude"

    # --- seeding --------------------------------------------------------

    def add_claude(self, *, model: str, project: str | None, session: str | None,
                   at: dt.datetime, input_tokens: int = 1000,
                   output_tokens: int = 500, cache_create: int = 200,
                   cache_read: int = 100, cache_1h: int | None = 100,
                   cost_usd_raw: float | None = None,
                   account_key: str | None = None,
                   msg_id: str | None = None, req_id: str | None = None,
                   path: str | None = None, subagent: str | None = None) -> None:
        # `cost_usd_raw` is written to the row but is NOT the oracle's cost.
        # The diagnosis reprices every entry from the embedded table and never
        # reads that column, so a scenario passes a deliberately wrong value
        # here to prove it: if the adapter ever reads the column again, the
        # denominator this oracle states no longer matches what it publishes.
        usage = claude_usage_dict(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_creation_tokens=cache_create,
            cache_read_tokens=cache_read,
            cache_1h_tokens=cache_1h, speed=None,
        )
        # This is the SAME pricing function the adapter calls, and the
        # coupling is accepted rather than overlooked. A defect inside
        # `_calculate_entry_cost` would move the oracle and the report
        # together, so the oracle cannot catch one — but the construction it
        # replaced pinned `cost_usd_raw`, which the adapter read straight back,
        # so that coupling was equally direct and is now at least uniform.
        # The oracle's worth is elsewhere and is unaffected: it independently
        # restates share, verdict, rank, coverage and the support minimums
        # from the seeded facts, and none of those comes from this call.
        cost = _calculate_entry_cost(model, usage, mode="calculate")
        self.claude.append({
            "kind": "claude", "model": model, "project": project,
            "session": session, "at": at, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cache_create": cache_create,
            "cache_read": cache_read, "cache_1h": cache_1h,
            "cost_usd_raw": cost_usd_raw, "account_key": account_key,
            "cost": cost, "root": "claude", "pool": None,
            # The canonical turn key. `maxContextWindowFraction` and the
            # subagent bucket both join on it, so a scenario that wants a
            # conversation-derived class to see an accounting row has to give
            # the two sides the same key.
            "msg_id": msg_id, "req_id": req_id, "path": path,
            "subagent": subagent,
        })

    def add_codex(self, *, model: str, at: dt.datetime, root: str = "root-a",
                  conversation: str = "v1.root-a.0", cwd: str | None = None,
                  input_tokens: int = 1000, cached: int = 100,
                  output_tokens: int = 500, reasoning: int = 100,
                  account_key: str | None = None,
                  root_thread_id: str = "user",
                  parent_thread_id: str | None = None,
                  native_thread_id: str | None = None,
                  context_window: int | None = None,
                  path: str | None = None,
                  line_offset: int | None = None) -> None:
        cost = _calculate_codex_entry_cost(
            model, input_tokens, cached, output_tokens, reasoning,
            speed="standard",
        )
        if cwd is not None:
            # `root_thread_id` is the thread-ORIGIN CATEGORY, not a thread
            # identifier: `_inferred_codex_thread_source` returns an explicit
            # `thread_source` verbatim and falls back to the literal `user`.
            # Seeding a thread id there made every fixture thread ambiguous.
            self.codex_threads.setdefault((root, conversation), {
                "root": root, "conversation": conversation,
                "native": native_thread_id or conversation,
                "root_thread_id": root_thread_id,
                "parent_thread_id": parent_thread_id,
                # `codex_conversation_threads.context_window` comes from
                # `session_meta` alone and cannot describe a request whose
                # model changed mid-thread. It is the FALLBACK capacity, and
                # using it is a published qualification.
                "context_window": context_window,
            })
        self.codex.append({
            "kind": "codex", "model": model, "at": at, "root": root,
            "conversation": conversation, "cwd": cwd,
            "input_tokens": input_tokens, "cached": cached,
            "output_tokens": output_tokens, "reasoning": reasoning,
            "account_key": account_key, "cost": cost,
            "pool": _oracle_pool(model),
            # An explicit file and offset, because the Codex context-window
            # fraction joins an accounting row to its owning turn through the
            # physical `(source_path, line_offset)` of the `token_count`
            # event that produced it. A scenario that wants that join has to
            # give the two sides the same coordinates.
            "path": path, "line_offset": line_offset,
        })

    # --- transcript seeding --------------------------------------------

    def add_claude_turn(self, *, session: str, at: dt.datetime,
                        model: str = OPUS, project: str = "/repo/alpha",
                        subagent: str | None = None,
                        input_tokens: int = 1000, output_tokens: int = 500,
                        cache_create: int = 200, cache_read: int = 100,
                        priced: bool = True) -> None:
        """One assistant turn: a transcript row AND its priced accounting row.

        The two sides are joined on `(msg_id, req_id)`, which is a STORED key
        on both — that is what lets the context-window fraction be derived
        from the loaded accounting population instead of by walking retained
        history.
        """
        index = len(self.conversation_rows)
        msg_id, req_id = f"msg-{session}-{index}", f"req-{session}-{index}"
        path = (f"/fixtures/claude/agent-{subagent}.jsonl" if subagent
                else f"/fixtures/claude/{session}.jsonl")
        self.conversation_rows.append({
            "session": session, "at": at, "kind": "assistant",
            "text": "a reply", "model": model, "msg_id": msg_id,
            "req_id": req_id, "subagent": subagent, "path": path,
            "is_sidechain": 1 if subagent else 0, "project": project,
        })
        if priced:
            self.add_claude(model=model, project=project, session=session,
                            at=at, input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_create=cache_create, cache_read=cache_read,
                            msg_id=msg_id, req_id=req_id, path=path,
                            subagent=subagent)

    def add_claude_prompt(self, *, session: str, at: dt.datetime,
                          text: str = "please do the thing",
                          project: str = "/repo/alpha") -> None:
        """One human turn. Genuine prose, never a marker: the oracle counts
        these directly and does not restate the command/meta normalization."""
        self.conversation_rows.append({
            "session": session, "at": at, "kind": "human", "text": text,
            "model": None, "msg_id": None, "req_id": None, "subagent": None,
            "path": f"/fixtures/claude/{session}.jsonl", "is_sidechain": 0,
            "project": project,
        })

    def add_claude_meta(self, *, session: str, at: dt.datetime,
                        text: str = "tool result",
                        project: str = "/repo/alpha") -> None:
        """One ORDINARY meta row — a tool result, not a compaction.

        It is a human CANDIDATE, because `entry_type` is not decisive and
        command normalization can promote a meta row to a human turn, and it
        does not promote, because its body is ordinary prose rather than a
        slash-command invocation. That is exactly the population the
        per-conversation normalize budget exists to bound.
        """
        self.conversation_rows.append({
            "session": session, "at": at, "kind": "meta", "text": text,
            "model": None, "msg_id": None, "req_id": None, "subagent": None,
            "path": f"/fixtures/claude/{session}.jsonl", "is_sidechain": 0,
            "project": project,
        })

    def add_claude_compaction(self, *, session: str, at: dt.datetime,
                              project: str = "/repo/alpha") -> None:
        """A compaction row.

        `isCompactSummary` is consumed at ingest, sets `entry_type = META` and
        blanks `text`, so the seeded row carries the summary body ONLY inside
        `blocks_json` — which is the single retained signal the read path
        infers from.
        """
        self.conversation_rows.append({
            "session": session, "at": at, "kind": "meta", "text": "",
            "body": COMPACTION_BODY, "model": None, "msg_id": None,
            "req_id": None, "subagent": None,
            "path": f"/fixtures/claude/{session}.jsonl", "is_sidechain": 0,
            "project": project,
        })

    def add_codex_turn(self, *, conversation: str, path: str,
                       turn_id: str, at: dt.datetime, context_window: int,
                       entries: Sequence[int], model: str,
                       root: str = "root-a", cwd: str = "/repo/codex",
                       root_thread_id: str = "user",
                       session_context_window: int | None = None,
                       native_thread_id: str | None = None) -> None:
        """One Codex turn: a `turn_context` record, its priced `token_count`
        events, and the human prompt that opened it.

        `context_window` is the turn's OWN capacity, retained on the
        `turn_context` record. A later turn in the same conversation may carry
        a different one — that is the mid-thread model change spec 6.2 names,
        and it is why the fraction cannot come from the session-level value.
        """
        offset = len(self.codex_events)
        self.codex_events.append({
            "path": path, "offset": offset, "root": root,
            "conversation": conversation, "at": at,
            "record_type": "turn_context", "event_type": None,
            "turn_id": None,
            "payload": {"payload": {"type": "turn_context",
                                    "turn_id": turn_id,
                                    "model": model,
                                    "model_context_window": context_window}},
        })
        for index, input_tokens in enumerate(entries):
            offset = len(self.codex_events)
            self.codex_events.append({
                "path": path, "offset": offset, "root": root,
                "conversation": conversation,
                "at": at + dt.timedelta(minutes=index),
                "record_type": "event_msg", "event_type": "token_count",
                "turn_id": None,
                "payload": {"payload": {"type": "token_count"}},
            })
            self.add_codex(
                model=model, at=at + dt.timedelta(minutes=index), root=root,
                conversation=conversation, cwd=cwd,
                input_tokens=input_tokens, cached=0, output_tokens=500,
                reasoning=100, root_thread_id=root_thread_id,
                native_thread_id=native_thread_id,
                context_window=session_context_window,
                path=path, line_offset=offset)
        self.codex_conversation_rows.append({
            "conversation": conversation, "root": root, "path": path,
            "offset": 900 + len(self.codex_conversation_rows),
            "at": at - dt.timedelta(seconds=1), "turn_id": turn_id,
            "kind": "user", "record_family": "response_item",
            "text": "please do the thing",
        })

    def add_codex_session_start(self, *, conversation: str, path: str,
                                at: dt.datetime, root: str = "root-a") -> None:
        """The `session_meta` record every retained rollout opens with.

        `infer_codex_event_turns` treats it as a segment boundary, so a
        fixture that omitted it would let one conversation's turn leak
        forward into the next file's unanchored prefix.
        """
        self.codex_events.append({
            "path": path, "offset": len(self.codex_events), "root": root,
            "conversation": conversation, "at": at,
            "record_type": "session_meta", "event_type": None,
            "turn_id": None,
            "payload": {"payload": {"type": "session_meta"}},
        })

    def add_claude_block(self, *, start: dt.datetime, hours: int = 5,
                         account_key: str = "unattributed") -> None:
        self.claude_blocks.append({
            "start": start, "end": start + dt.timedelta(hours=hours),
            "account_key": account_key,
        })

    def add_codex_window(self, *, start: dt.datetime, end: dt.datetime,
                         root: str = "root-a",
                         limit_name: str = "codex_standard",
                         account_key: str = "unattributed") -> None:
        self.codex_windows.append({
            "start": start, "end": end, "root": root,
            "limit_name": limit_name, "account_key": account_key,
        })

    # --- the oracle -----------------------------------------------------

    def in_range(self, low: dt.datetime, high: dt.datetime) -> list[dict]:
        """Exactly the entries a half-open range admits.

        Restated rather than imported: an oracle that summed every seeded
        row would agree with a diagnosis that had lost its window bound.
        """
        rows = self.codex if self.source == "codex" else self.claude
        return [e for e in rows if low <= e["at"] < high]

    def in_window(self) -> list[dict]:
        return self.in_range(WINDOW_START, WINDOW_END)

    def in_baseline(self) -> list[dict]:
        return self.in_range(BASELINE_START, WINDOW_START)

    def total_usd(self) -> float:
        return math.fsum(e["cost"] for e in self.in_window())

    def _grouped(self, key, rows=None) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for entry in self.in_window() if rows is None else rows:
            out.setdefault(key(entry), []).append(entry)
        return out

    def _block_groups(self, rows=None,
                      window_low: dt.datetime = WINDOW_START,
                      window_high: dt.datetime = WINDOW_END):
        """Assign entries to native blocks, pool-compatibly and at most once.

        Returns `(groups, pool_of)`. A block is eligible when it OVERLAPS the
        requested range, which is what the adapter loads, so the pool map has
        to be keyed the same way.
        """
        groups: dict[str, list[dict]] = {}
        pool_of: dict[str, str] = {}
        claude_blocks = [b for b in self.claude_blocks
                         if b["end"] > window_low and b["start"] < window_high]
        codex_windows = [w for w in self.codex_windows
                         if w["end"] > window_low and w["start"] < window_high]
        for entry in (self.in_range(window_low, window_high)
                      if rows is None else rows):
            if entry["kind"] == "claude":
                for block in claude_blocks:
                    if block["start"] <= entry["at"] < block["end"]:
                        key = f"claude|standard|{_iso_z(block['start'])}"
                        groups.setdefault(key, []).append(entry)
                        pool_of[key] = "standard"
                        break
                continue
            for window in codex_windows:
                if window["root"] != entry["root"]:
                    continue
                if _oracle_pool(window["limit_name"]) != entry["pool"]:
                    continue
                if window["start"] <= entry["at"] < window["end"]:
                    pool = entry["pool"] or "standard"
                    key = f"{window['root']}|{pool}|{_iso_z(window['start'])}"
                    groups.setdefault(key, []).append(entry)
                    pool_of[key] = pool
                    break
        return groups, pool_of

    # --- baseline comparators, restated -------------------------------

    def _baseline_lookup(self, contributor_class: str):
        """`(lookup, code)` for one class over the preceding equal window.

        The comparators are the ones the spec names, and two of them are not
        per-key lookups: a 5-hour block key embeds its start instant and a
        session key is minted per session, so neither can appear in both
        windows. A subject the baseline does not contain, in a baseline that
        WAS established, compares to zero rather than being withheld.
        """
        rows = self.in_baseline()
        if not rows:
            return None, "baseline_insufficient"
        total = math.fsum(e["cost"] for e in rows)
        if total <= 0:
            return None, "baseline_insufficient"
        if contributor_class == "five_hour_bursts":
            groups, pool_of = self._block_groups(
                rows=rows, window_low=BASELINE_START, window_high=WINDOW_START
            )
            best: dict[str, float] = {}
            for key, block_rows in groups.items():
                pool = pool_of.get(key, "standard")
                share = math.fsum(r["cost"] for r in block_rows) / total
                if share > best.get(pool, 0.0):
                    best[pool] = share
            return (lambda _key, pool: best.get(pool or "standard", 0.0)), None
        if contributor_class == "session_concentration":
            grouped = self._grouped(_oracle_session_key, rows=rows)
            top = max((math.fsum(r["cost"] for r in group) / total
                       for group in grouped.values()), default=0.0)
            return (lambda _key, _pool: top), None
        key_fn = (_oracle_project_key if contributor_class
                  == "project_concentration" else (lambda e: e["model"]))
        grouped = self._grouped(key_fn, rows=rows)
        shares = {key: math.fsum(r["cost"] for r in group) / total
                  for key, group in grouped.items()}
        return (lambda key, _pool: shares.get(key, 0.0)), None

    # --- the per-class arithmetic -------------------------------------

    def _confidence(self, usd_coverage: float | None, support: int) -> str:
        if usd_coverage is None:
            return "low"
        if (usd_coverage >= 1.0 - ORACLE_COVERAGE_EPSILON
                and support >= ORACLE_HIGH_MIN_SUPPORT):
            return "high"
        if (usd_coverage >= ORACLE_MEDIUM_MIN_COVERAGE - ORACLE_COVERAGE_EPSILON
                and support >= ORACLE_MEDIUM_MIN_SUPPORT):
            return "medium"
        return "low"

    def _class_oracle(self, contributor_class: str,
                      groups: dict[str, list[dict]],
                      *, preempting_code: str | None = None,
                      provider_preempted: bool = False,
                      pool_of: dict[str, str] | None = None,
                      denominator_withheld: str | None = None) -> dict:
        total = self.total_usd()
        attributed = [r for rows in groups.values() for r in rows]
        support = len(attributed)
        attributed_usd = math.fsum(r["cost"] for r in attributed)
        usd_coverage = (attributed_usd / total) if total > 0 else None
        confidence = self._confidence(usd_coverage, support)
        base = {"contributorClass": contributor_class,
                "supportUnits": support,
                "usdCoverage": usd_coverage,
                "confidence": confidence}
        if provider_preempted:
            # A provider-wide cause is decided before any class shapes its
            # subjects, so the class attributed NOTHING — and the dimensions
            # that describe an attributed population are therefore absent
            # rather than zero. Zero would state that this class covered none
            # of the window's dollars, which is a measurement it never made.
            return {**base, "verdict": "withheld", "code": preempting_code,
                    "supportUnits": None, "usdCoverage": None,
                    "confidence": "low", "subjects": []}
        if preempting_code is not None:
            # A class-level cause is decided AFTER the class shaped its
            # subjects, so its real population still stands behind it.
            return {**base, "verdict": "withheld", "code": preempting_code,
                    "subjects": []}
        if (len(groups) < ORACLE_MIN_DISTINCT_SUBJECTS
                or support < ORACLE_MIN_PRICED_ENTRIES
                or (usd_coverage is not None
                    and usd_coverage < ORACLE_WITHHOLD_MIN_COVERAGE
                    - ORACLE_COVERAGE_EPSILON)):
            return {**base, "verdict": "withheld",
                    "code": "insufficient_population", "subjects": []}
        if denominator_withheld is not None:
            return {**base, "verdict": "withheld",
                    "code": denominator_withheld, "subjects": []}
        if total <= 0.0:
            return {**base, "verdict": "withheld",
                    "code": "insufficient_population", "subjects": []}
        ranked = sorted(
            ((key, math.fsum(r["cost"] for r in rows), len(rows))
             for key, rows in groups.items()),
            key=lambda triple: (-triple[1], triple[0]),
        )
        top_usd = ranked[0][1]
        if top_usd / total < ORACLE_SHARE_FLOOR - ORACLE_SHARE_FLOOR_EPSILON:
            return {**base, "verdict": "no_contributor", "code": None,
                    "subjects": []}
        tied = [row for row in ranked
                if abs(row[1] - top_usd) <= ORACLE_TIE_EPSILON_USD]
        tied.sort(key=lambda row: row[0])
        lookup, baseline_code = self._baseline_lookup(contributor_class)
        if confidence not in ("high", "medium"):
            lookup, baseline_code = None, "baseline_insufficient"
        subjects = []
        for key, usd, count in tied:
            pool = (pool_of or {}).get(key)
            subjects.append({
                "subjectKey": key,
                "observedUsd": usd,
                "share": usd / total,
                "pricedEntryCount": count,
                "baselineShare": None if lookup is None else lookup(key, pool),
                "baselineCode": baseline_code,
            })
        return {**base, "verdict": "contributor", "code": None,
                "subjects": subjects}

    # --- the S3 arithmetic, restated ----------------------------------

    def _conversation_sessions(self) -> list[str]:
        """Sessions holding a transcript row inside the window.

        This is the candidate population for every Claude conversation class:
        an accounting row whose session retains no transcript is not eligible
        for an attempted evaluation at all.
        """
        return sorted({
            row["session"] for row in self.conversation_rows
            if WINDOW_START <= row["at"] < WINDOW_END
        })

    def _session_rows(self, session: str) -> list[dict]:
        return sorted(
            (row for row in self.conversation_rows
             if row["session"] == session),
            key=lambda row: row["at"],
        )

    def _s3_verdict(self, contributor_class: str, *, candidates: list[dict],
                    evaluated: list[dict], qualifying: list[dict],
                    gap_codes: tuple = ()) -> dict:
        """One conversation-derived class's verdict, restated.

        Three populations: CANDIDATES were eligible for an attempted
        evaluation, EVALUATED are the ones whose predicate could be decided,
        QUALIFYING are the evaluated ones the predicate matched. Support and
        USD coverage are measured over the EVALUATED population, never over
        the qualifying set — counting qualifiers would make confidence rise
        precisely as the problem worsens.
        """
        if candidates and not evaluated:
            # Nothing at all could be decided. Spec 2.2, 2.3 and 2.4 state the
            # same rule for all three conversation classes: that is
            # `withheld / signal_unavailable`, not a class answered over an
            # empty evaluated population. Reporting the latter published
            # `insufficient_population (support 0 units)` over a fully
            # populated forty-entry window — a false sentence about the store.
            return _s3_class_oracle(contributor_class, not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        total = self.total_usd()
        support = len(evaluated)
        evaluated_usd = math.fsum(row["cost"] for row in evaluated)
        usd_coverage = (evaluated_usd / total) if total > 0 else None
        confidence = self._confidence(usd_coverage, support)
        base = {"contributorClass": contributor_class,
                "supportUnits": support, "usdCoverage": usd_coverage,
                "confidence": confidence}
        # The distinct-subject minimum does NOT apply to an evaluated
        # predicate that matched nothing: it has no subjects by RESULT rather
        # than by absence of data — which is why `ORACLE_MIN_DISTINCT_S3_SUBJECTS`
        # is restated above and never applied to an empty qualifying set.
        # Every other minimum still does apply.
        if (support < ORACLE_MIN_PRICED_ENTRIES
                or (usd_coverage is not None
                    and usd_coverage < ORACLE_WITHHOLD_MIN_COVERAGE
                    - ORACLE_COVERAGE_EPSILON)):
            return {**base, "verdict": "withheld",
                    "code": "insufficient_population", "subjects": []}
        if total <= 0.0:
            return {**base, "verdict": "withheld",
                    "code": "insufficient_population", "subjects": []}
        if not qualifying:
            # Evaluated, and it matched nothing: the healthy inverse.
            return {**base, "verdict": "no_contributor", "code": None,
                    "subjects": []}
        usd = math.fsum(row["cost"] for row in qualifying)
        if usd / total < ORACLE_SHARE_FLOOR - ORACLE_SHARE_FLOOR_EPSILON:
            return {**base, "verdict": "no_contributor", "code": None,
                    "subjects": []}
        return {**base, "verdict": "contributor", "code": None, "subjects": [{
            "subjectKey": ORACLE_S3_SUBJECT_KEY[contributor_class],
            "observedUsd": usd,
            "share": usd / total,
            "pricedEntryCount": len(qualifying),
            # Every new scenario seeds nothing in the preceding window, so the
            # comparator cannot be established and the field is withheld.
            "baselineShare": None,
            "baselineCode": "baseline_insufficient",
        }]}

    # Which classes read the transcript store, restated. Everything else an
    # S3 class needs lives in the accounting store, which every plan opens.
    _S3_NEEDS_TRANSCRIPTS = {
        "cache_churn": ("claude",),
        "short_high_context": ("claude", "codex"),
        "subagent_fanout": ("claude",),
    }

    def _s3_oracle(self, contributor_class: str, source: str,
                   provider_cause: str | None) -> dict:
        if contributor_class == "cache_churn" and source == "codex":
            # Capability, not availability: Codex retains a cached-input ratio
            # and no loss predicate, so an unreadable store cannot overturn it.
            return _s3_class_oracle(contributor_class, not_applicable=True,
                                    code=None)
        if provider_cause is not None:
            return _s3_class_oracle(contributor_class, not_applicable=False,
                                    code=provider_cause)
        transcript_rows = (self.codex_conversation_rows if source == "codex"
                           else self.conversation_rows)
        if (source in self._S3_NEEDS_TRANSCRIPTS[contributor_class]
                and not transcript_rows):
            # The scenario writes no `conversations.db` at all, so an
            # AUTHORIZED open fails and the signal cannot be established.
            return _s3_class_oracle(contributor_class, not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        if contributor_class == "cache_churn":
            return self._oracle_cache_churn()
        if contributor_class == "short_high_context":
            return self._oracle_short_high_context()
        return self._oracle_subagent_fanout()

    def _oracle_cache_churn(self) -> dict:
        """The churn predicate, restated over the seeded turns.

        Running maximum of `cache_read` keyed by `(subagent, model)`, cleared
        by a compaction. A turn is flagged when the prior maximum and this
        turn's creation both clear their floors, the read collapsed to at most
        half the maximum, and most of THIS turn's context was freshly created.
        The walk begins at the last compaction strictly before the window and
        publishes only flags that land inside it.
        """
        sessions = self._conversation_sessions()
        if not sessions:
            return _s3_class_oracle("cache_churn", not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        flagged: set[tuple] = set()
        for session in sessions:
            rows = self._session_rows(session)
            seed = 0
            for index, row in enumerate(rows):
                if row["kind"] == "meta" and row["at"] < WINDOW_START:
                    seed = index
            running: dict[tuple, int] = {}
            for row in rows[seed:]:
                if row["kind"] == "meta":
                    running.clear()
                    continue
                if row["kind"] != "assistant":
                    continue
                priced = self._priced_for(row)
                if priced is None:
                    continue
                key = (row["subagent"], row["model"])
                previous = running.get(key, 0)
                create = priced["cache_create"]
                read = priced["cache_read"]
                total = create + read
                if (previous >= ORACLE_CACHE_FAILURE_CACHE_FLOOR
                        and create >= ORACLE_CACHE_FAILURE_CREATE_FLOOR
                        and read <= (ORACLE_CACHE_FAILURE_COLLAPSE_FRACTION
                                     * previous)
                        and total > 0
                        and (create / total)
                        >= ORACLE_CACHE_FAILURE_RECREATE_FRACTION
                        and WINDOW_START <= row["at"] < WINDOW_END):
                    flagged.add((row["msg_id"], row["req_id"]))
                running[key] = max(previous, read)
        known = set(sessions)
        candidates = [entry for entry in self.in_window()
                      if entry["session"] in known]
        qualifying = [entry for entry in candidates
                      if (entry["msg_id"], entry["req_id"]) in flagged]
        return self._s3_verdict("cache_churn", candidates=candidates,
                                evaluated=candidates, qualifying=qualifying)

    def _priced_for(self, row: dict) -> dict | None:
        for entry in self.claude:
            if (entry["msg_id"] == row["msg_id"]
                    and entry["req_id"] == row["req_id"]
                    and entry["msg_id"] is not None):
                return entry
        return None

    def _oracle_short_high_context(self) -> dict:
        if self.source == "codex":
            return self._oracle_codex_short_high_context()
        return self._oracle_claude_short_high_context()

    def _oracle_codex_short_high_context(self) -> dict:
        """The Codex half, restated.

        A human turn is a NON-EMPTY prompt carrying a turn id: a turn-less
        user row is unturned and never a prompt. Capacity is the OWNING TURN's
        `model_context_window`, because the session-level value comes from
        `session_meta` alone and cannot describe a request whose model changed
        mid-thread; the session value is a fallback and using it is a
        published qualification. Only the exact literals `user` and `subagent`
        are origin categories, so anything else belongs to neither population
        and could not be decided.
        """
        candidates = self.in_window()
        if not candidates:
            return _s3_class_oracle("short_high_context",
                                    not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        # The turn each `token_count` event belongs to, and that turn's own
        # capacity — both read straight off the seeded records.
        turn_of: dict[tuple, str] = {}
        capacity_of: dict[str, int] = {}
        current: dict[str, str | None] = {}
        for event in sorted(self.codex_events,
                            key=lambda e: (e["path"], e["offset"])):
            path = event["path"]
            body = event["payload"]["payload"]
            if event["record_type"] == "session_meta":
                current[path] = None
                continue
            if event["record_type"] == "turn_context":
                current[path] = body["turn_id"]
                capacity_of[body["turn_id"]] = body["model_context_window"]
                continue
            turn = current.get(path)
            if turn is not None:
                turn_of[(path, event["offset"])] = turn
        prompts: dict[str, int] = {}
        for row in self.codex_conversation_rows:
            if (row["kind"] == "user" and row["turn_id"]
                    and row["text"].strip()):
                prompts[row["conversation"]] = prompts.get(
                    row["conversation"], 0) + 1
        # #834 S2 (#800), restated rather than imported: raw candidate evidence
        # is a property of the TRANSCRIPT store and the population is a property
        # of the ACCOUNTING rows, so a conversation the accounting names and the
        # transcript store does not retain has NO turn count to read. It is not
        # decided, it lowers coverage, and when no conversation in the window
        # retains a candidate row nothing about the class was established.
        # Presence is any retained candidate row for the key, not a prompt: a
        # retained conversation of zero human prompts IS decided.
        retained = {row["conversation"] for row in self.codex_conversation_rows}
        threads = {t["conversation"]: t for t in self.codex_threads.values()}
        keys = sorted({entry["conversation"] for entry in candidates})
        if not any(key in retained for key in keys):
            return _s3_class_oracle("short_high_context",
                                    not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        unevaluable: set[str] = set()
        qualifying_keys: list[str] = []
        for key in keys:
            if key not in retained:
                unevaluable.add(key)
                continue
            thread = threads.get(key)
            origin = thread["root_thread_id"] if thread else None
            if origin not in ("user", "subagent"):
                unevaluable.add(key)
                continue
            if origin != "user":
                continue                    # decided: a delegated thread
            count = prompts.get(key, 0)
            if count < 1 or count > ORACLE_MAX_HUMAN_TURNS:
                continue
            best = None
            saw_request = False
            for entry in candidates:
                if entry["conversation"] != key:
                    continue
                turn = turn_of.get((entry["path"], entry["line_offset"]))
                if turn is None:
                    continue                # orphaned: no owning turn
                saw_request = True
                capacity = capacity_of.get(turn) or (
                    thread["context_window"] if thread else None)
                if not capacity:
                    continue
                # Codex `input_tokens` is already cache-inclusive.
                fraction = entry["input_tokens"] / capacity
                best = fraction if best is None else max(best, fraction)
            if saw_request and best is None:
                unevaluable.add(key)
                continue
            if best is not None and best >= ORACLE_MIN_WINDOW_FRACTION:
                qualifying_keys.append(key)
        evaluated = [entry for entry in candidates
                     if entry["conversation"] not in unevaluable]
        qualifying = [entry for entry in evaluated
                      if entry["conversation"] in set(qualifying_keys)]
        return self._s3_verdict("short_high_context", candidates=candidates,
                                evaluated=evaluated, qualifying=qualifying)

    def _oracle_claude_short_high_context(self) -> dict:
        """One to three human turns over the WHOLE retained conversation, and
        at least one associated request at or above four fifths of its own
        model's context window. The largest single fraction, never their sum.
        """
        sessions = self._conversation_sessions()
        if not sessions:
            return _s3_class_oracle("short_high_context",
                                    not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        qualifying_sessions: list[str] = []
        unevaluable: set[str] = set()
        for session in sessions:
            rows = self._session_rows(session)
            humans = [row for row in rows
                      if row["kind"] == "human" and not row["subagent"]]
            # The per-conversation normalize budget, restated as production
            # applies it. Production ranks the non-sidechain `meta` and `human`
            # rows of each conversation by `(timestamp_utc, id)` and retains at
            # most `budget` of them, then counts the human turns among those.
            # A conversation whose TRUNCATED count already exceeds the maximum
            # is decidedly long — no further reading could bring it back under
            # three — so exhaustion only matters when the truncated count is
            # still small enough to qualify. Ordering the rule the other way
            # would call a two-thousand-turn conversation unevaluable when it
            # is plainly long.
            budget = ORACLE_CONVERSATION_NORMALIZE_BUDGET_ROWS
            candidate_rows = [row for row in rows
                              if row["kind"] in ("human", "meta")
                              and not row["subagent"]]
            truncated_humans = [row for row in candidate_rows[:budget]
                                if row["kind"] == "human"]
            if len(truncated_humans) > ORACLE_MAX_HUMAN_TURNS:
                continue                    # decided: the conversation is long
            if len(candidate_rows) > budget:
                unevaluable.add(session)
                continue
            if not humans or len(humans) > ORACLE_MAX_HUMAN_TURNS:
                continue
            first_human = humans[0]["at"]
            best = None
            saw_request = False
            for row in rows:
                if row["kind"] != "assistant" or row["subagent"]:
                    continue
                if not (WINDOW_START <= row["at"] < WINDOW_END):
                    continue
                if row["at"] < first_human:
                    continue                    # orphaned: no preceding human
                priced = self._priced_for(row)
                if priced is None:
                    continue
                saw_request = True
                capacity = ORACLE_CONTEXT_WINDOWS.get(row["model"])
                if not capacity:
                    continue
                used = (priced["input_tokens"] + priced["cache_read"]
                        + priced["cache_create"])
                fraction = used / capacity
                best = fraction if best is None else max(best, fraction)
            if saw_request and best is None:
                unevaluable.add(session)
                continue
            if best is not None and best >= ORACLE_MIN_WINDOW_FRACTION:
                qualifying_sessions.append(session)
        known = set(sessions)
        candidates = [entry for entry in self.in_window()
                      if entry["session"] in known]
        evaluated = [entry for entry in candidates
                     if entry["session"] not in unevaluable]
        qualifying = [entry for entry in evaluated
                      if entry["session"] in set(qualifying_sessions)]
        return self._s3_verdict("short_high_context", candidates=candidates,
                                evaluated=evaluated, qualifying=qualifying)

    def _oracle_subagent_fanout(self) -> dict:
        if self.source == "codex":
            return self._oracle_codex_fanout()
        return self._oracle_claude_fanout()

    def _oracle_claude_fanout(self) -> dict:
        """Cost bucketed on `(session, subagent)`, grouped by parent, with at
        least two distinct buckets before a parent fans out. A subagent-shaped
        accounting row with no normalized bucket behind it is UNALLOCATED: its
        dollars are published separately and it leaves the evaluated set."""
        sessions = self._conversation_sessions()
        if not sessions:
            return _s3_class_oracle("subagent_fanout", not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        buckets: dict[tuple, tuple] = {}
        by_parent: dict[str, set] = {}
        for row in self.conversation_rows:
            if row["kind"] != "assistant" or not row["subagent"]:
                continue
            if not (WINDOW_START <= row["at"] < WINDOW_END):
                continue
            buckets[(row["msg_id"], row["req_id"])] = (row["session"],
                                                       row["subagent"])
            by_parent.setdefault(row["session"], set()).add(row["subagent"])
        qualifying_parents = {
            session for session, names in by_parent.items()
            if len(names) >= ORACLE_MIN_SUBAGENT_BUCKETS
        }
        known = set(sessions)
        candidates = [entry for entry in self.in_window()
                      if entry["session"] in known]
        qualifying, unallocated = [], []
        for entry in candidates:
            bucket = buckets.get((entry["msg_id"], entry["req_id"]))
            if bucket is not None:
                if bucket[0] in qualifying_parents:
                    qualifying.append(entry)
                continue
            if entry["subagent"]:
                unallocated.append(entry)
        unallocated_ids = {id(entry) for entry in unallocated}
        evaluated = [entry for entry in candidates
                     if id(entry) not in unallocated_ids]
        return self._s3_verdict("subagent_fanout", candidates=candidates,
                                evaluated=evaluated, qualifying=qualifying)

    def _oracle_codex_fanout(self) -> dict:
        """The delegation ORIGIN CATEGORY, and a parent resolved on exactly
        one non-self match under the COMPLETE identity — the triple, not the
        pair. Only the two exact literals are recognisable; anything else
        belongs to neither population and could not be decided."""
        candidates = self.in_window()
        if not candidates:
            return _s3_class_oracle("subagent_fanout", not_applicable=False,
                                    code=ORACLE_S3_UNEVALUATED_CODE)
        by_native: dict[tuple, list] = {}
        for thread in self.codex_threads.values():
            by_native.setdefault((thread["root"], thread["native"]),
                                 []).append(thread)
        scoped = sorted({entry["conversation"] for entry in candidates})
        parents: dict[str, tuple] = {}
        failed: set[str] = set()
        for key in scoped:
            thread = next((t for t in self.codex_threads.values()
                           if t["conversation"] == key), None)
            if thread is None or thread["root_thread_id"] not in ("user",
                                                                  "subagent"):
                failed.add(key)
                continue
            if thread["root_thread_id"] != "subagent":
                continue                        # decided: a main thread
            parent = thread["parent_thread_id"]
            if parent is None:
                failed.add(key)
                continue
            matches = [t for t in by_native.get((thread["root"], parent), [])
                       if t["native"] != thread["native"]]
            if len(matches) != 1:
                failed.add(key)
                continue
            parents[key] = (thread["root"], parent)
        groups: dict[tuple, set] = {}
        for key, group in parents.items():
            groups.setdefault(group, set()).add(key)
        qualifying_groups = {group for group, children in groups.items()
                             if len(children) >= ORACLE_MIN_SUBAGENT_BUCKETS}
        qualifying, evaluated = [], []
        for entry in candidates:
            key = entry["conversation"]
            if key in failed:
                continue
            evaluated.append(entry)
            group = parents.get(key)
            if group is not None and group in qualifying_groups:
                qualifying.append(entry)
        return self._s3_verdict("subagent_fanout", candidates=candidates,
                                evaluated=evaluated, qualifying=qualifying)

    # --- provider-wide statements -------------------------------------

    def retained_bounds(self):
        rows = self.codex if self.source == "codex" else self.claude
        if not rows:
            return None, None
        return min(e["at"] for e in rows), max(e["at"] for e in rows)

    def store_horizon(self) -> dt.datetime | None:
        """The install's own observation horizon, restated.

        The earliest provider-native block the stats store holds, unbounded by
        the window. It is what separates a store pruned past the window start
        from an install that did not exist then.
        """
        starts = ([w["start"] for w in self.codex_windows]
                  if self.source == "codex"
                  else [b["start"] for b in self.claude_blocks])
        return min(starts) if starts else None

    def retention_coverage(self) -> float | None:
        low_retained, high_retained = self.retained_bounds()
        if low_retained is None:
            return None
        horizon = self.store_horizon()
        if horizon is None:
            # No horizon, no discriminator, so no figure. Falling back to the
            # window start would restore the exact rule the horizon replaced
            # and report a young store as a pruned one.
            return None
        answerable_start = WINDOW_START
        if horizon > WINDOW_START:
            answerable_start = min(horizon, WINDOW_END)
        span = (WINDOW_END - answerable_start).total_seconds()
        if span <= 0:
            return None
        low = max(answerable_start, low_retained)
        high = min(WINDOW_END, high_retained)
        if high <= low:
            return 0.0
        return min(1.0, (WINDOW_END - low).total_seconds() / span)

    def provider_cause(self) -> str | None:
        """The provider-wide withheld cause, restated as arithmetic."""
        if self.forced_provider_cause is not None:
            return self.forced_provider_cause
        rows = self.in_window()
        retention = self.retention_coverage()
        if not rows:
            if retention is not None and retention <= 0.0:
                return "retained_range_mismatch"
            return None
        if self.total_usd() <= 0.0:
            # A total of zero is `pricing_unavailable` only when nothing in
            # the population could be priced at all. A priceable population
            # that genuinely cost nothing is the answer zero, not a cause.
            if not any(_oracle_pricing_resolved(e) for e in rows):
                return "pricing_unavailable"
            return None
        # The kernel applies its coverage slack to retention too: it is a
        # ratio of two spans against the same threshold, so a genuinely
        # half-retained window computes just under it.
        if (retention is not None and retention
                < ORACLE_WITHHOLD_MIN_COVERAGE - ORACLE_COVERAGE_EPSILON):
            return "stale_evidence"
        return None

    def _project_groups_and_cause(self):
        groups = self._grouped(_oracle_project_key)
        rows = self.in_window()
        unresolved = sum(1 for e in rows if _oracle_project_key(e)
                         in ("(unknown)", "(unassigned)"))
        cause = ("unattributed_evidence"
                 if rows and unresolved == len(rows) else None)
        return groups, cause

    def oracle(self, name: str, source: str) -> dict:
        self.source = source
        project_groups, project_cause = self._project_groups_and_cause()
        provider_cause = self.provider_cause()
        if provider_cause is not None:
            project_cause = provider_cause
        block_groups, block_pools = self._block_groups()
        preempted = provider_cause is not None
        classes = [
            self._class_oracle("model_mix", self._grouped(lambda e: e["model"]),
                               preempting_code=provider_cause,
                               provider_preempted=preempted),
            self._class_oracle("project_concentration", project_groups,
                               preempting_code=project_cause,
                               provider_preempted=preempted),
            self._class_oracle("session_concentration",
                               self._grouped(_oracle_session_key),
                               preempting_code=provider_cause,
                               provider_preempted=preempted),
            self._class_oracle("five_hour_bursts", block_groups,
                               preempting_code=provider_cause,
                               provider_preempted=preempted,
                               pool_of=block_pools),
        ]
        # The provider overlay preempts every cell except `not_applicable`,
        # which is a capability statement rather than an availability one.
        classes += [self._s3_oracle(kind, source, provider_cause)
                    for kind in ORACLE_S3_CLASSES]
        _stamp_ranks(classes)
        if any(c["verdict"] == "contributor" for c in classes):
            overall, overall_code = "contributor_detected", None
        elif any(c["verdict"] == "withheld" for c in classes):
            withheld = [c["code"] for c in classes if c["verdict"] == "withheld"]
            overall = "withheld"
            overall_code = next(
                (code for code in ORACLE_WITHHELD_PRECEDENCE
                 if code in withheld),
                withheld[0],
            )
        else:
            overall, overall_code = "no_contributor_detected", None
        return {
            "scenario": name,
            "source": source,
            "window": {"startAt": _iso_z(WINDOW_START),
                       "endAt": _iso_z(WINDOW_END)},
            "denominatorUsd": (None if provider_cause is not None
                               else self.total_usd()),
            "denominatorState": ("withheld" if provider_cause is not None
                                 else "available"),
            "denominatorCode": provider_cause,
            "entryCount": len(self.in_window()),
            # This oracle describes ONE provider. Under `--source all` the
            # report-level verdict folds both providers, so the provider
            # verdict is what a single-provider oracle can bind.
            "providerVerdict": overall,
            "providerCode": overall_code,
            "overallVerdict": overall,
            "overallCode": overall_code,
            "withheldClassCount": sum(1 for c in classes
                                      if c["verdict"] == "withheld"),
            # `not_applicable` classes are excluded from the completeness
            # test: a provider with a permanently unsupported class could
            # otherwise never render as healthy.
            "applicableClassCount": sum(1 for c in classes
                                        if c["verdict"] != "not_applicable"),
            "classes": classes,
        }


def _stamp_ranks(classes: list[dict]) -> None:
    """The cross-class ranking, restated: `(-observedUsd, registryOrder, key)`.

    Rank is a report-level fact, so it cannot be derived inside one class.
    """
    rows = []
    for class_oracle in classes:
        order = ORACLE_REGISTRY_ORDER.index(class_oracle["contributorClass"])
        for subject in class_oracle["subjects"]:
            rows.append((-subject["observedUsd"], order,
                         subject["subjectKey"], subject))
    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    for rank, row in enumerate(rows, start=1):
        row[3]["rank"] = rank


def _oracle_pricing_resolved(entry: dict) -> bool:
    """Whether this entry's model has a pricing row of its own.

    A Claude model the embedded table does not know contributes zero cost,
    which is what `pricing_unavailable` is about. Every Codex model prices,
    through the legacy fallback when it is unknown.
    """
    if entry["kind"] != "claude":
        return True
    return entry["model"] in CLAUDE_MODEL_PRICING


def _oracle_project_key(entry: dict) -> str:
    if entry["kind"] == "claude":
        return entry["project"] or "(unknown)"
    return entry["cwd"] or "(unassigned)"


def _oracle_session_key(entry: dict) -> str:
    if entry["kind"] == "claude":
        return entry["session"] or "(unknown)"
    return entry["conversation"] or "(unknown)"


# --- writing the stores -------------------------------------------------

def _write(seed: Seed, app_dir: Path) -> None:
    stats_path = app_dir / "stats.db"
    cache_path = app_dir / "cache.db"
    create_stats_db(stats_path)
    create_cache_db(cache_path)
    # A scenario that seeds no transcript rows writes NO conversations.db at
    # all, which is the shape every accounting-only scenario keeps and the
    # reason its three conversation-derived classes stay
    # `withheld / signal_unavailable`.
    if (seed.conversation_rows or seed.codex_conversation_rows
            or seed.codex_events):
        _write_conversations(seed, app_dir / "conversations.db")

    stats = sqlite3.connect(stats_path)
    cache = sqlite3.connect(cache_path)
    try:
        seen_files: set[str] = set()
        for index, entry in enumerate(seed.claude):
            # One JSONL file carries one `cwd`, so the file identity is the
            # (session, project) PAIR. Keying it on the session alone made
            # every later entry inherit the first row's project through the
            # `session_files` join, collapsing every project fixture to one
            # bucket.
            project_slug = (entry["project"] or "unknown").strip("/").replace(
                "/", "-"
            )
            # A subagent invocation writes its OWN `agent-<hash>.jsonl`, and
            # `session_files` carries the PARENT's session id for it — which
            # is exactly why a physical row count cannot answer how many turns
            # a conversation has.
            path = entry.get("path") or (
                f"/fixtures/claude/"
                f"{entry['session'] or 'unknown'}--{project_slug}.jsonl")
            if path not in seen_files:
                seed_session_file(cache, path=path,
                                  session_id=entry["session"],
                                  project_path=entry["project"])
                seen_files.add(path)
            seed_session_entry(
                cache, source_path=path, line_offset=index,
                timestamp_utc=_iso(entry["at"]), model=entry["model"],
                input_tokens=entry["input_tokens"],
                output_tokens=entry["output_tokens"],
                cache_create=entry["cache_create"],
                cache_read=entry["cache_read"],
                cost_usd_raw=entry["cost_usd_raw"],
                account_key=entry["account_key"],
                msg_id=entry.get("msg_id"), req_id=entry.get("req_id"),
            )
            if entry["cache_1h"] is not None:
                cache.execute(
                    "UPDATE session_entries SET cache_create_1h_tokens = ? "
                    "WHERE source_path = ? AND line_offset = ?",
                    (entry["cache_1h"], path, index),
                )

        seen_codex_files: set[str] = set()
        seen_threads: set[tuple[str, str]] = set()
        for index, entry in enumerate(seed.codex):
            # An explicit path and offset when the scenario stated them, so
            # the accounting row and the `token_count` event that produced it
            # share the physical coordinates the turn join runs over.
            path = (entry.get("path")
                    or f"/fixtures/codex/{entry['root']}-{index // 20}.jsonl")
            line_offset = entry.get("line_offset")
            if line_offset is None:
                line_offset = index
            if path not in seen_codex_files:
                seed_codex_session_file(
                    cache, path=path, last_session_id=f"sess-{index // 20}",
                    last_model=entry["model"], source_root_key=entry["root"],
                )
                seen_codex_files.add(path)
            identity = (entry["root"], entry["conversation"])
            thread = seed.codex_threads.get(identity)
            if thread is not None and identity not in seen_threads:
                seed_codex_conversation_thread(
                    cache, conversation_key=entry["conversation"],
                    source_root_key=entry["root"],
                    native_thread_id=thread["native"],
                    root_thread_id=thread["root_thread_id"],
                    parent_thread_id=thread["parent_thread_id"],
                    source_path=path, cwd=entry["cwd"],
                    context_window=thread["context_window"],
                )
                seen_threads.add(identity)
            seed_codex_session_entry(
                cache, source_path=path, line_offset=line_offset,
                timestamp_utc=_iso(entry["at"]),
                session_id=f"sess-{index // 20}", model=entry["model"],
                input_tokens=entry["input_tokens"],
                cached_input_tokens=entry["cached"],
                output_tokens=entry["output_tokens"],
                reasoning_output_tokens=entry["reasoning"],
                total_tokens=entry["input_tokens"] + entry["output_tokens"],
                source_root_key=entry["root"],
                conversation_key=entry["conversation"],
                account_key=entry["account_key"],
            )

        for block in seed.claude_blocks:
            stats.execute(
                "INSERT OR IGNORE INTO five_hour_blocks "
                "(five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, "
                " last_updated_at_utc, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(block["start"].timestamp()), _iso(block["end"]),
                 _iso(block["start"]), _iso(block["start"]),
                 _iso(block["end"]), 12.5, _iso(block["start"]),
                 _iso(block["end"]), block["account_key"]),
            )
        for index, window in enumerate(seed.codex_windows):
            stats.execute(
                "INSERT INTO quota_window_blocks "
                "(source, source_root_key, logical_limit_key, observed_slot, "
                " window_minutes, limit_name, resets_at_utc, "
                " nominal_start_at_utc, first_observed_at_utc, "
                " last_observed_at_utc, first_percent, current_percent, "
                " last_source_path, last_line_offset, generation, account_key) "
                "VALUES ('codex',?,?,?,300,?,?,?,?,?,0.0,1.0,'',0,'fixture',?)",
                (window["root"], json.dumps({"limitId": f"w{index}"}),
                 str(index), window["limit_name"], _iso(window["end"]),
                 _iso(window["start"]), _iso(window["start"]),
                 _iso(window["end"]), window["account_key"]),
            )
        if seed.drop_codex_tables:
            # The shape an older cache.db has: Claude accounting is present
            # and readable, Codex accounting is not there at all.
            cache.execute("DROP TABLE IF EXISTS codex_session_entries")
        stats.commit()
        cache.commit()
    finally:
        stats.close()
        cache.close()


def _write_conversations(seed: Seed, path: Path) -> None:
    """The transcript store, seeded from the same rows the oracle reads.

    Compaction rows carry a BLANK `text` and the summary body only inside
    `blocks_json`: `isCompactSummary` is consumed at ingest and never
    persisted, so the normalized body is the single retained signal, and a
    fixture that put the sentinel in `text` would test a shape production
    never writes.
    """
    create_conversations_db(path)
    conn = sqlite3.connect(path)
    try:
        for offset, row in enumerate(seed.conversation_rows):
            body = row.get("body", row["text"])
            entry_type = row["kind"]
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id, uuid, parent_uuid, source_path, byte_offset, "
                " timestamp_utc, entry_type, text, blocks_json, model, "
                " msg_id, req_id, cwd, git_branch, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["session"], f"{row['session']}-{offset}", None,
                 row["path"], offset, _iso(row["at"]), entry_type,
                 row["text"],
                 json.dumps([{"kind": "text", "text": body}]),
                 row["model"], row["msg_id"], row["req_id"],
                 row["project"], "main", row["is_sidechain"]),
            )
        for row in seed.codex_conversation_rows:
            body = row["text"]
            conn.execute(
                "INSERT INTO codex_conversation_messages "
                "(conversation_key, source_root_key, source_path, "
                " line_offset, timestamp_utc, turn_id, kind, record_family, "
                " content_digest, content_len, text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["conversation"], row["root"], row["path"], row["offset"],
                 _iso(row["at"]), row["turn_id"], row["kind"],
                 row["record_family"],
                 f"{row['conversation']}:{row['offset']}", len(body), body),
            )
        for event in seed.codex_events:
            conn.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, "
                " conversation_key, native_thread_id, root_thread_id, "
                " parent_thread_id, timestamp_utc, record_type, event_type, "
                " turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["path"], event["offset"], event["root"],
                 event["conversation"], event["conversation"], "user", None,
                 _iso(event["at"]), event["record_type"], event["event_type"],
                 event["turn_id"], None, json.dumps(event["payload"])),
            )
        if seed.codex_events:
            # Disarm the byte-zero conversation replay. It fires on the first
            # writable conversations open that finds retained Codex events
            # with no matching contract-version marker, and it CLEARS every
            # normalized Codex message before re-deriving them from those
            # events — which would silently empty this fixture's prompts. A
            # real store carries the marker. The literal is restated rather
            # than imported, like every other expectation in this file.
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?,?)",
                ("codex_conversation_contract_version",
                 CODEX_CONVERSATION_CONTRACT_VERSION))
            conn.execute("DELETE FROM cache_meta "
                         "WHERE key='conversation_rebuild_codex_pending'")
        conn.commit()
    finally:
        conn.close()


# --- scenarios ----------------------------------------------------------

def _spread(index: int) -> dt.datetime:
    """Ten entries per 5-hour block, blocks back to back from the window start."""
    return (WINDOW_START + dt.timedelta(hours=5 * (index // 10))
            + dt.timedelta(minutes=25 * (index % 10)))


def _blocks_for(seed: Seed, count: int,
                origin: dt.datetime = WINDOW_START) -> None:
    for offset in range(count):
        seed.add_claude_block(start=origin + dt.timedelta(hours=5 * offset))


def _model_dominant() -> Seed:
    """One model clears the floor by a strict margin.

    The preceding equal-duration window is populated too, so the baseline
    field is AVAILABLE here — the pair with `baseline-insufficient` is what
    makes that scenario's withheld baseline mean something.
    """
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=OPUS, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    for index in range(40, 60):
        seed.add_claude(model=HAIKU, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    for index in range(30):
        seed.add_claude(model=OPUS, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}",
                        at=_spread(index) - dt.timedelta(days=7))
    _blocks_for(seed, 8)
    _blocks_for(seed, 8, WINDOW_START - dt.timedelta(days=7))
    return seed


def _project_dominant() -> Seed:
    seed = Seed()
    for index in range(48):
        seed.add_claude(model=(OPUS if index % 3 == 0 else HAIKU),
                        project="/repo/dominant",
                        session=f"sess-{index % 6}", at=_spread(index))
    for index in range(48, 60):
        seed.add_claude(model=HAIKU, project=f"/repo/small-{index % 4}",
                        session=f"sess-{index % 6}", at=_spread(index))
    _blocks_for(seed, 8)
    return seed


def _session_dominant() -> Seed:
    seed = Seed()
    for index in range(45):
        seed.add_claude(model=(OPUS if index % 2 == 0 else HAIKU),
                        project=f"/repo/p{index % 5}", session="sess-hot",
                        at=_spread(index))
    for index in range(45, 60):
        seed.add_claude(model=HAIKU, project=f"/repo/p{index % 5}",
                        session=f"sess-cool-{index % 4}", at=_spread(index))
    _blocks_for(seed, 8)
    return seed


def _burst_dominant() -> Seed:
    """Two blocks only, and the first carries two thirds of the entries.

    The first block starts two hours BEFORE the window, which is the ordinary
    production shape: a provider-native 5-hour block does not restart at a
    subscription-week boundary. Its dollars are still window-scoped, because
    only entries inside the window are ever assigned to it, so the row states
    the overlap through a `block_precedes_window` qualification rather than
    implying the block sits inside the window it is reported under.

    The four-minute spacing is load-bearing and follows from that move. The
    block now ends three hours into the window, so 40 entries at five-minute
    spacing would run past its end and scatter across no block at all; at four
    minutes the fortieth entry lands at 2h36m, inside it. The scenario is
    about one block carrying two thirds of the spend, so every one of those
    entries has to be inside it.
    """
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=(OPUS if index % 2 == 0 else HAIKU),
                        project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}",
                        at=WINDOW_START + dt.timedelta(minutes=4 * index))
    for index in range(20):
        seed.add_claude(model=(OPUS if index % 2 == 0 else HAIKU),
                        project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}",
                        at=WINDOW_START + dt.timedelta(days=3,
                                                       minutes=5 * index))
    seed.add_claude_block(start=WINDOW_START - dt.timedelta(hours=2))
    seed.add_claude_block(start=WINDOW_START + dt.timedelta(days=3))
    return seed


def _control_no_contributor() -> Seed:
    """Every class is measured and nothing reaches the floor.

    Six equal subjects per class means a top share of exactly one sixth,
    which is below the floor with room to spare. "Equal" is equal in binary
    floating point because the six models carry IDENTICAL rates and every
    entry carries identical token counts, so every entry reprices to the same
    float. Pinning `cost_usd_raw` no longer achieves that: the diagnosis
    reprices from the embedded table and never reads the column.
    """
    seed = Seed()
    models = EQUAL_PRICED_MODELS
    for index in range(60):
        seed.add_claude(
            model=models[index % 6],
            project=f"/repo/p{(index * 7) % 6}",
            session=f"sess-{(index * 5) % 6}",
            at=_spread(index),
        )
    _blocks_for(seed, 6)
    return seed


def _tie_exact() -> Seed:
    """Two projects carry identical cost, so both report.

    Identical entry sets under one model make the two sums equal in binary
    floating point rather than merely close, which is what the tie rule is
    written against.
    """
    seed = Seed()
    for index in range(30):
        seed.add_claude(model=OPUS, project="/repo/alpha",
                        session=f"sess-{index % 4}", at=_spread(index))
    for index in range(30):
        seed.add_claude(model=OPUS, project="/repo/zeta",
                        session=f"sess-{index % 4}", at=_spread(index + 30))
    _blocks_for(seed, 8)
    return seed


def _boundary_on_floor() -> Seed:
    """The top model's share is exactly 0.20, to pin inclusivity.

    Six equally priced models over identical token counts: one with ten
    entries and five with eight each, so fifty entries of one repriced float
    `c` give a top of `10c` over a total of `50c`. The share is one fifth to
    the last bit, which is what the inclusive `>=` floor is written against.

    Every row also carries a DELIBERATELY WRONG `cost_usd_raw`. The diagnosis
    reprices from `CLAUDE_MODEL_PRICING` and never reads that column, so the
    decoy is inert — and if a fold ever reads it again, this scenario's
    denominator jumps to $4,950.00 and the oracle says so.
    """
    seed = Seed()
    models = EQUAL_PRICED_MODELS
    index = 0
    for _ in range(10):
        seed.add_claude(model=models[0], project=f"/repo/p{index % 5}",
                        session=f"sess-{index % 6}", at=_spread(index),
                        cost_usd_raw=99.0)
        index += 1
    for model in models[1:6]:
        for _ in range(8):
            seed.add_claude(model=model, project=f"/repo/p{index % 5}",
                            session=f"sess-{index % 6}", at=_spread(index),
                            cost_usd_raw=99.0)
            index += 1
    _blocks_for(seed, 8)
    return seed


def _insufficient_population() -> Seed:
    """One model, one project, one session: support is not met anywhere."""
    seed = Seed()
    for index in range(5):
        seed.add_claude(model=OPUS, project="/repo/only",
                        session="sess-only", at=_spread(index))
    seed.add_claude_block(start=WINDOW_START)
    return seed


def _unattributed_codex() -> Seed:
    """Codex rows with no project metadata at all.

    Seeded here rather than taken from the shared bench corpora, because
    #618 item 5 records that the generated corpora contain zero unattributed
    Codex rows, so an acceptance leg written against them would pass without
    ever reaching this branch.
    """
    seed = Seed()
    for index in range(40):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation=f"v1.root-a.{index // 10}", cwd=None)
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    return seed


def _stale_evidence() -> Seed:
    """The store was observing before the window and retains only its last day.

    There IS a population and it IS priced, but six sevenths of the requested
    window predates what the store still holds. The block a month before the
    window is what makes that pruning rather than youth: this install was
    already recording then, so the missing accounting rows are missing rather
    than never written. `fresh-install` is the same shape without it.
    """
    seed = Seed()
    origin = WINDOW_END - dt.timedelta(hours=23)
    for index in range(40):
        seed.add_claude(model=(OPUS if index % 2 else HAIKU),
                        project=f"/repo/p{index % 3}",
                        session=f"sess-{index % 4}",
                        at=origin + dt.timedelta(minutes=20 * index))
    seed.add_claude_block(start=WINDOW_START - dt.timedelta(days=30))
    seed.add_claude_block(start=origin)
    seed.add_claude_block(start=origin + dt.timedelta(hours=5))
    return seed


def _fresh_install() -> Seed:
    """A brand-new install two days into the subscription week.

    The store holds nothing before the day it was installed, and it never
    claimed to: its observation horizon begins inside the window. That is not
    pruning, and reading it as pruning withheld every class of a new user's
    very first `cctally explain`.
    """
    seed = Seed()
    origin = WINDOW_END - dt.timedelta(days=2)
    for index in range(40):
        seed.add_claude(model=(OPUS if index % 2 else HAIKU),
                        project=f"/repo/p{index % 3}",
                        session=f"sess-{index % 4}",
                        at=origin + dt.timedelta(minutes=60 * index))
    for offset in range(9):
        seed.add_claude_block(start=origin + dt.timedelta(hours=5 * offset))
    return seed


def _provider_unavailable() -> Seed:
    """Claude answers while Codex cannot be read at all.

    The Codex accounting table is dropped from the store, which is the shape
    an older `cache.db` has. One unreadable provider must withhold itself as
    `provider_unavailable` rather than end a report the other provider can
    still answer.

    **This is the one oracle that ASSERTS its provider cause rather than
    deriving it** (`forced_provider_cause`). Every other cause is arithmetic
    over the seeded rows, but this one is a fact about the STORE — a table
    that does not exist — and the oracle deliberately holds no SQLite schema
    knowledge with which to derive "this read would raise". Restating the
    absence as a seeded fact keeps the oracle independent of the diagnosis
    code; it just makes this one scenario a weaker check than the others, and
    the pytest legs in `tests/test_620_s2_diagnosis_sources.py` carry the
    behaviour instead.
    """
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=OPUS, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    for index in range(40, 60):
        seed.add_claude(model=HAIKU, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    _blocks_for(seed, 8)
    seed.drop_codex_tables = True
    seed.forced_provider_cause = "provider_unavailable"
    return seed


def _codex_unmatched_window() -> Seed:
    """A third of the Codex spend falls outside every retained quota window.

    An entry matching no compatible window is a coverage gap: it is reported
    through `unmatched_pool_window` and appears in no block.
    """
    seed = Seed()
    for index in range(20):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation="v1.root-a.0", cwd="/repo/codex")
    for index in range(20):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(hours=6,
                                                      minutes=5 * index),
                       conversation="v1.root-a.1", cwd="/repo/codex")
    # Deliberately outside both windows below.
    for index in range(20):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(days=3,
                                                      minutes=5 * index),
                       conversation="v1.root-a.2", cwd="/repo/codex")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    seed.add_codex_window(start=WINDOW_START + dt.timedelta(hours=5),
                          end=WINDOW_START + dt.timedelta(hours=10))
    return seed


def _retained_range_mismatch() -> Seed:
    """Every entry predates the requested window, so nothing is in range."""
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=(OPUS if index % 2 else HAIKU),
                        project=f"/repo/p{index % 3}",
                        session=f"sess-{index % 4}",
                        at=WINDOW_START - dt.timedelta(days=30, minutes=index))
    seed.add_claude_block(start=WINDOW_START - dt.timedelta(days=30))
    return seed


def _pricing_unavailable() -> Seed:
    """Every row names a model the embedded table does not price.

    An unrecognized Claude model contributes zero cost, so the population
    exists and the denominator does not. That is a different statement from
    having no population, and it gets its own cause.
    """
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=f"claude-from-the-future-{index % 3}",
                        project=f"/repo/p{index % 3}",
                        session=f"sess-{index % 4}", at=_spread(index))
    _blocks_for(seed, 6)
    return seed


def _pricing_fallback() -> Seed:
    """The fallback-priced model is the TOP subject, not merely present.

    `classify_class` reports the top subject and its exact ties, so a scenario
    where the fallback-priced model is outspent proves nothing about
    `isFallbackPricing`: the qualification never reaches a reported row, and
    the string appeared in zero goldens. Forty unknown-model rows against
    twenty standard ones put it on top, so the qualification is proven all the
    way through the adapter, the wire and the terminal.
    """
    seed = Seed()
    for index in range(20):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation="v1.root-a.0", cwd="/repo/codex")
    for index in range(20, 60):
        seed.add_codex(model=CODEX_UNKNOWN,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation="v1.root-a.1", cwd="/repo/other")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=6))
    return seed


def _baseline_insufficient() -> Seed:
    """A measurable window whose preceding equal window holds nothing.

    Identical in-window shape to `model-dominant`, and different in exactly
    one respect: no preceding-week rows. The baseline field is therefore
    withheld while the observed-cost result still renders, which is the
    separation that keeps a thin baseline from suppressing a measured answer.
    """
    seed = Seed()
    for index in range(40):
        seed.add_claude(model=OPUS, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    for index in range(40, 60):
        seed.add_claude(model=HAIKU, project=f"/repo/p{index % 4}",
                        session=f"sess-{index % 5}", at=_spread(index))
    _blocks_for(seed, 8)
    return seed


def _codex_pool_split() -> Seed:
    """Spark and standard windows overlap on one root.

    A join matching on account and time alone would let each window claim the
    other's spend; the pool-compatible join keeps them apart.
    """
    seed = Seed()
    for index in range(30):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation="v1.root-a.0", cwd="/repo/codex")
    for index in range(30):
        seed.add_codex(model=CODEX_SPARK,
                       at=WINDOW_START + dt.timedelta(minutes=5 * index),
                       conversation="v1.root-a.1", cwd="/repo/codex")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5),
                          limit_name="codex_standard")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5),
                          limit_name=CODEX_SPARK)
    return seed


# --- the S3 dominant-signal corpora (#620 S3) ---------------------------
#
# Every one of these seeds a `conversations.db`, which is what separates them
# from the accounting-only scenarios above: without that store the three
# conversation-derived classes are withheld as `signal_unavailable` and the
# corpus would prove nothing about their predicates.

# A healthy read that establishes a high running maximum of cached prefix,
# and a re-create that collapses it. The floors are 20,000 tokens on both
# legs, so both profiles are well clear of the boundary.
PRIMED_TURN = {"cache_create": 5_000, "cache_read": 60_000}
REBUILT_TURN = {"cache_create": 40_000, "cache_read": 100}
# 170,000 of a 200,000-token window is 0.85 — comfortably over four fifths.
LARGE_REQUEST = {"input_tokens": 10_000, "cache_create": 10_000,
                 "cache_read": 150_000}


def _cache_churn_dominant() -> Seed:
    """Half the turns rebuild their cache, and they carry most of the cost.

    The two profiles alternate, so every re-create follows a read that has
    just set the running maximum. That is what makes twelve of twenty-four
    turns flag rather than one.
    """
    seed = Seed()
    # The timing is load-bearing and proves decision D-A. Seventeen turns land
    # in the first 5-hour block and seven in the second, which puts the first
    # block's observed cost ($8.55) BETWEEN this class's estimated waste
    # ($8.28) and its retained cost ($9.63). Ranking on retained cost puts
    # prompt-cache churn first; ranking on the counterfactual would put the
    # block first — so the corpus discriminates rather than merely agreeing.
    for index in range(17):
        seed.add_claude_turn(
            session="sess-churn",
            at=WINDOW_START + dt.timedelta(minutes=15 * index),
            model=OPUS, project="/repo/churn",
            **(PRIMED_TURN if index % 2 == 0 else REBUILT_TURN))
    for index in range(17, 24):
        seed.add_claude_turn(
            session="sess-churn",
            at=WINDOW_START + dt.timedelta(minutes=305 + 15 * (index - 17)),
            model=OPUS, project="/repo/churn",
            **(PRIMED_TURN if index % 2 == 0 else REBUILT_TURN))
    _blocks_for(seed, 3)
    return seed


def _cache_churn_seeded() -> Seed:
    """The same corpus with a compaction immediately before the window.

    A compaction legitimately invalidates the prefix, so the first in-window
    re-create is a re-prime rather than a failure. The pair proves the seed
    rule decides the verdict rather than merely being executed.
    """
    seed = Seed()
    seed.add_claude_turn(session="sess-churn",
                         at=WINDOW_START - dt.timedelta(hours=6),
                         model=OPUS, project="/repo/churn", **PRIMED_TURN)
    seed.add_claude_compaction(session="sess-churn",
                               at=WINDOW_START - dt.timedelta(hours=5),
                               project="/repo/churn")
    for index in range(24):
        seed.add_claude_turn(
            session="sess-churn",
            at=WINDOW_START + dt.timedelta(minutes=25 * index),
            model=OPUS, project="/repo/churn", **REBUILT_TURN)
    _blocks_for(seed, 3)
    return seed


def _short_context_dominant() -> Seed:
    """Six two-turn conversations each holding a request at 85% of its window,
    against six ordinary ones of the same shape."""
    seed = Seed()
    for index in range(12):
        session = f"sess-conv-{index}"
        project = f"/repo/c{index % 3}"
        start = WINDOW_START + dt.timedelta(hours=4 * index)
        for turn in range(2):
            seed.add_claude_prompt(session=session,
                                   at=start + dt.timedelta(minutes=turn),
                                   project=project)
        for turn in range(2):
            seed.add_claude_turn(
                session=session,
                at=start + dt.timedelta(minutes=10 + turn),
                model=OPUS, project=project,
                **(LARGE_REQUEST if index < 6 else {}))
    _blocks_for(seed, 10)
    return seed


def _fanout_dominant(unallocated: bool = False) -> Seed:
    """One parent delegating to three subagents, which carry three quarters of
    the window's cost."""
    seed = Seed()
    for index in range(8):
        seed.add_claude_turn(
            session="sess-parent",
            at=WINDOW_START + dt.timedelta(minutes=30 * index),
            model=OPUS, project="/repo/fanout")
    for slot, name in enumerate(("a1b2c3", "d4e5f6", "g7h8i9")):
        for index in range(8):
            seed.add_claude_turn(
                session="sess-parent",
                at=WINDOW_START + dt.timedelta(hours=5 + slot,
                                               minutes=15 * index),
                model=OPUS, project="/repo/fanout", subagent=name,
                input_tokens=4_000, output_tokens=2_000)
    if unallocated:
        # Subagent-shaped ACCOUNTING with no normalized bucket behind it. Its
        # dollars are neither folded into the row nor discarded.
        for index in range(4):
            seed.add_claude(
                model=OPUS, project="/repo/fanout", session="sess-parent",
                at=WINDOW_START + dt.timedelta(hours=9, minutes=10 * index),
                input_tokens=4_000, output_tokens=2_000,
                msg_id=f"orphan-{index}", req_id=f"orphan-req-{index}",
                path="/fixtures/claude/agent-zzzzzz.jsonl", subagent="zzzzzz")
    _blocks_for(seed, 12)
    return seed


def _fanout_unallocated() -> Seed:
    return _fanout_dominant(unallocated=True)


def _codex_fanout() -> Seed:
    """Two delegated Codex threads under one resolvable parent.

    Both tables live in `cache.db`, so this class needs no transcript
    authorization at all — and this scenario deliberately writes no
    `conversations.db`, which is what proves it.
    """
    seed = Seed()
    for index in range(12):
        seed.add_codex(model=CODEX_STANDARD,
                       at=WINDOW_START + dt.timedelta(minutes=10 * index),
                       conversation="v1.root-a.parent", cwd="/repo/codex",
                       native_thread_id="p1", root_thread_id="user")
    for slot, child in enumerate(("c1", "c2")):
        for index in range(12):
            seed.add_codex(
                model=CODEX_STANDARD,
                at=WINDOW_START + dt.timedelta(hours=3 + slot,
                                               minutes=10 * index),
                conversation=f"v1.root-a.{child}", cwd="/repo/codex",
                native_thread_id=child, root_thread_id="subagent",
                parent_thread_id="p1", input_tokens=4_000, output_tokens=2_000)
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    return seed


def _codex_short_context() -> Seed:
    """The Codex short-and-large-context winner, over a mid-thread model change.

    Spec 6.2 names this case by name, and the shape is the whole reason the
    fraction is per-turn. One two-prompt conversation runs two turns whose
    capacities differ, because the model changed between them:

      * turn one has a 400,000-token window and its requests sit at 100,000,
        which is a quarter of it;
      * turn two has a 200,000-token window and its requests sit at 180,000,
        which is nine tenths of it.

    `codex_conversation_threads.context_window` is 400,000 — the value
    `session_meta` recorded at the start — so dividing by the session-level
    capacity would put the largest request at 0.45 and the conversation would
    not qualify at all. Dividing by turn ONE's capacity would put it at 0.45
    as well. Only the OWNING turn's capacity produces 0.90, which is why this
    corpus discriminates turn-versus-turn rather than merely turn-versus-
    session.

    The second conversation runs five prompts, which is long by the published
    rule, and it supplies the population the class needs before it can be
    measured at all.
    """
    seed = Seed()
    short_path = "/fixtures/codex/root-a-short.jsonl"
    seed.add_codex_session_start(conversation="v1.root-a.short",
                                 path=short_path, at=WINDOW_START)
    seed.add_codex_turn(
        conversation="v1.root-a.short", path=short_path, turn_id="turn-one",
        at=WINDOW_START + dt.timedelta(minutes=5), context_window=400_000,
        entries=[100_000] * 6, model=CODEX_STANDARD,
        session_context_window=400_000, native_thread_id="short-1")
    seed.add_codex_turn(
        conversation="v1.root-a.short", path=short_path, turn_id="turn-two",
        at=WINDOW_START + dt.timedelta(hours=2), context_window=200_000,
        entries=[180_000] * 6, model=CODEX_STANDARD,
        session_context_window=400_000, native_thread_id="short-1")

    long_path = "/fixtures/codex/root-a-long.jsonl"
    seed.add_codex_session_start(conversation="v1.root-a.long",
                                 path=long_path,
                                 at=WINDOW_START + dt.timedelta(hours=6))
    for index in range(5):
        seed.add_codex_turn(
            conversation="v1.root-a.long", path=long_path,
            turn_id=f"long-{index}",
            at=WINDOW_START + dt.timedelta(hours=7 + index),
            context_window=400_000, entries=[20_000, 20_000],
            model=CODEX_STANDARD, session_context_window=400_000,
            native_thread_id="long-1")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    return seed


def _control_s3_codex_zero_match() -> Seed:
    """The CODEX zero-match control: evaluated, decided, matching nothing.

    #834 S2 (#800). `control-s3-zero-match` is registered as a CLAUDE scenario,
    so it cannot constrain the Codex evaluator at all — an implementation that
    withheld every Codex zero-match case would pass it unchanged. This is its
    Codex sibling, and it is a guard against OVER-withholding rather than a
    restatement of the fix: every conversation here retains prompt rows, so
    every one is evaluated and decided, and the class must still answer
    `no_contributor` rather than withdrawing behind an absence.

    Two conversations of five prompts each are long by the published rule, and
    every request sits at a twentieth of its turn's capacity, so nothing
    qualifies on either predicate half.
    """
    seed = Seed()
    for index, name in enumerate(("quiet-a", "quiet-b")):
        conversation = f"v1.root-a.{name}"
        path = f"/fixtures/codex/{name}.jsonl"
        start = WINDOW_START + dt.timedelta(hours=6 * index)
        seed.add_codex_session_start(conversation=conversation, path=path,
                                     at=start)
        for turn in range(5):
            seed.add_codex_turn(
                conversation=conversation, path=path,
                turn_id=f"{name}-{turn}",
                at=start + dt.timedelta(minutes=20 * (turn + 1)),
                context_window=400_000, entries=[20_000, 20_000, 20_000],
                model=CODEX_STANDARD, session_context_window=400_000,
                native_thread_id=f"{name}-1")
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    return seed


def _codex_mixed_transcript_coverage() -> Seed:
    """One conversation the transcript store retains, one it does not.

    #834 S2 (#800). A GLOBAL test of whether the candidate query returned any
    row would pass this scenario while still reporting `evaluabilityCoverage`
    of 1.0, because the retained conversation supplies the rows the global test
    looks for. Only a PER-KEY classification lowers the coverage here, which is
    what makes this the scenario that discriminates the two implementations.

    The retained conversation runs eight prompts and is decided long. The
    unretained one carries accounting entries and a readable `user` origin —
    so its origin is NOT what could not be read — and no transcript row at all.

    The retained conversation is sized so the class still clears
    `min_priced_entries` after the unretained one leaves the evaluated
    population. Without that the class would be withheld for want of support
    and the fixture would pin `insufficient_population` — a different sentence
    from the one it exists to pin, which is a MEASURED class publishing an
    `evaluabilityCoverage` below 1.0 with a `no_retained_transcript` gap.
    """
    seed = Seed()
    retained_path = "/fixtures/codex/retained.jsonl"
    seed.add_codex_session_start(conversation="v1.root-a.retained",
                                 path=retained_path, at=WINDOW_START)
    for turn in range(8):
        seed.add_codex_turn(
            conversation="v1.root-a.retained", path=retained_path,
            turn_id=f"retained-{turn}",
            at=WINDOW_START + dt.timedelta(minutes=20 * (turn + 1)),
            context_window=400_000, entries=[20_000, 20_000, 20_000],
            model=CODEX_STANDARD, session_context_window=400_000,
            native_thread_id="retained-1")
    for index in range(12):
        seed.add_codex(
            model=CODEX_STANDARD,
            at=WINDOW_START + dt.timedelta(hours=6, minutes=10 * index),
            conversation="v1.root-a.unretained", cwd="/repo/codex",
            native_thread_id="unretained-1", root_thread_id="user",
            input_tokens=20_000, output_tokens=500)
    seed.add_codex_window(start=WINDOW_START,
                          end=WINDOW_START + dt.timedelta(hours=5))
    return seed


def _control_s3_zero_match() -> Seed:
    """Every conversation-derived predicate is EVALUATED and matches nothing.

    Without adequate population the control would prove nothing: it would be
    withheld for want of support rather than answering `no_contributor`. Two
    conversations of five human turns each are long by the published rule, no
    turn rebuilds its cache, and nothing is delegated.
    """
    seed = Seed()
    for index, session in enumerate(("sess-quiet-a", "sess-quiet-b")):
        project = f"/repo/q{index}"
        start = WINDOW_START + dt.timedelta(days=index)
        for turn in range(5):
            seed.add_claude_prompt(session=session,
                                   at=start + dt.timedelta(minutes=turn),
                                   project=project)
        for turn in range(12):
            seed.add_claude_turn(
                session=session,
                at=start + dt.timedelta(hours=1, minutes=20 * turn),
                model=(OPUS if turn % 2 else HAIKU), project=project)
    _blocks_for(seed, 8)
    return seed


def _unknown_context_window() -> Seed:
    """One short conversation whose only request publishes no context window.

    The class still MEASURES, because two other conversations are evaluable —
    which is what makes this a rendered `unknown_context_window` gap and a
    fallen `evaluabilityCoverage` rather than a withheld class. A conversation
    with no known capacity for any reply is never guessed at and never
    silently treated as small.
    """
    seed = Seed()
    # Eight evaluable conversations, so the class clears `min_priced_entries`
    # and MEASURES. Without that the gap would be published on a class that
    # was withheld for want of support, and the fixture would be pinning the
    # wrong sentence.
    for index in range(8):
        session = f"sess-known-{index}"
        project = f"/repo/k{index % 2}"
        start = WINDOW_START + dt.timedelta(hours=6 * index)
        for turn in range(2):
            seed.add_claude_prompt(session=session,
                                   at=start + dt.timedelta(minutes=turn),
                                   project=project)
        for turn in range(3):
            seed.add_claude_turn(session=session,
                                 at=start + dt.timedelta(minutes=10 + turn),
                                 model=OPUS, project=project,
                                 **(LARGE_REQUEST if index < 4 else {}))
    for turn in range(2):
        seed.add_claude_prompt(session="sess-no-capacity",
                               at=WINDOW_START + dt.timedelta(days=3,
                                                              minutes=turn),
                               project="/repo/unknown")
    for turn in range(4):
        seed.add_claude_turn(
            session="sess-no-capacity",
            at=WINDOW_START + dt.timedelta(days=3, minutes=10 + turn),
            model=CAPACITY_LESS_MODEL, project="/repo/unknown",
            **LARGE_REQUEST)
    _blocks_for(seed, 10)
    return seed


def _budget_exhausted_conversation() -> Seed:
    """One conversation holding more candidate rows than the normalize budget.

    Spec 2.3: `entry_type` is not decisive, so deciding a human candidate costs
    a normalization, and a conversation with three human turns behind an
    arbitrarily long tool and meta history presents arbitrarily many rows. The
    budget bounds it, and a conversation that exhausts it is UNEVALUABLE — it
    lowers `evaluabilityCoverage` with a `scan_budget_exhausted` gap and is
    never classified short or long on a count nobody finished making.

    This is the rendered form of that state. The seeded-store unit legs prove
    the internal facts; only a golden proves what a reader is told.
    """
    seed = Seed()
    # Eight evaluable conversations, so the class clears `min_priced_entries`
    # and MEASURES with the exhausted one lowering `evaluabilityCoverage`,
    # rather than being withheld for want of support.
    for index in range(8):
        session = f"sess-short-{index}"
        project = f"/repo/b{index % 2}"
        start = WINDOW_START + dt.timedelta(hours=6 * index)
        for turn in range(2):
            seed.add_claude_prompt(session=session,
                                   at=start + dt.timedelta(minutes=turn),
                                   project=project)
        for turn in range(3):
            seed.add_claude_turn(session=session,
                                 at=start + dt.timedelta(minutes=10 + turn),
                                 model=OPUS, project=project,
                                 **(LARGE_REQUEST if index < 4 else {}))
    # Exactly three human turns behind a very long tool and meta history, and
    # one candidate row past the budget — which is spec 6.8's named case. The
    # three humans come FIRST, so the truncated count is three and the
    # conversation would qualify as short if the budget had not run out; that
    # is what makes it unevaluable rather than decidedly long.
    exhausted_start = WINDOW_START + dt.timedelta(days=4)
    for turn in range(3):
        seed.add_claude_prompt(
            session="sess-over-budget",
            at=exhausted_start + dt.timedelta(seconds=turn),
            project="/repo/over")
    for turn in range(ORACLE_CONVERSATION_NORMALIZE_BUDGET_ROWS - 2):
        seed.add_claude_meta(
            session="sess-over-budget",
            at=exhausted_start + dt.timedelta(seconds=10 + turn),
            project="/repo/over")
    for turn in range(4):
        seed.add_claude_turn(
            session="sess-over-budget",
            at=exhausted_start + dt.timedelta(hours=1, minutes=turn),
            model=OPUS, project="/repo/over", **LARGE_REQUEST)
    _blocks_for(seed, 10)
    return seed


def _s3_thin_population() -> Seed:
    """The conversation-derived classes are evaluated over fewer than twenty
    priced entries, while the provider window is fully populated.

    That is `withheld / insufficient_population` naming `min_priced_entries` —
    a class measured over too little, which is a different statement from a
    class whose signal could not be established at all.
    """
    seed = Seed()
    # Ten transcript-backed entries: enough to evaluate, not enough to report.
    for index in range(5):
        seed.add_claude_prompt(session="sess-thin",
                               at=WINDOW_START + dt.timedelta(minutes=index),
                               project="/repo/thin")
    for index in range(10):
        seed.add_claude_turn(
            session="sess-thin",
            at=WINDOW_START + dt.timedelta(hours=1, minutes=10 * index),
            model=OPUS, project="/repo/thin")
    # Forty more with no transcript rows at all, so the PROVIDER window is
    # populated and the shortfall is the class's own.
    for index in range(40):
        seed.add_claude(model=OPUS, project=f"/repo/p{index % 4}",
                        session=f"sess-plain-{index % 5}", at=_spread(index))
    _blocks_for(seed, 10)
    return seed


def _s3_thin_usd_coverage() -> Seed:
    """Enough evaluated entries, and too few of the window's dollars.

    The evaluated population clears `min_priced_entries` and carries under half
    the provider's cost, so the class is withheld naming `min_usd_coverage`
    rather than the entry minimum. The two shortfalls read identically as
    `insufficient_population` and mean different things, which is why the
    surface names which minimum was missed.
    """
    seed = Seed()
    for index in range(12):
        seed.add_claude_prompt(session="sess-covered",
                               at=WINDOW_START + dt.timedelta(minutes=index),
                               project="/repo/covered")
    # Twenty-four cheap transcript-backed turns.
    for index in range(24):
        seed.add_claude_turn(
            session="sess-covered",
            at=WINDOW_START + dt.timedelta(hours=1, minutes=5 * index),
            model=HAIKU, project="/repo/covered",
            input_tokens=100, output_tokens=50,
            cache_create=10, cache_read=10)
    # Twenty expensive entries with no transcript rows, which is most of the
    # window's money and none of the evaluated population.
    for index in range(20):
        seed.add_claude(model=OPUS, project="/repo/expensive",
                        session="sess-expensive", at=_spread(index + 40),
                        input_tokens=200_000, output_tokens=40_000,
                        cache_create=50_000, cache_read=50_000)
    _blocks_for(seed, 10)
    return seed


SCENARIOS = {
    "model-dominant": (_model_dominant, "claude"),
    "project-dominant": (_project_dominant, "claude"),
    "session-dominant": (_session_dominant, "claude"),
    "burst-dominant": (_burst_dominant, "claude"),
    "control-no-contributor": (_control_no_contributor, "claude"),
    "tie-exact": (_tie_exact, "claude"),
    "boundary-on-floor": (_boundary_on_floor, "claude"),
    "insufficient-population": (_insufficient_population, "claude"),
    "unattributed-codex": (_unattributed_codex, "codex"),
    "stale-evidence": (_stale_evidence, "claude"),
    "retained-range-mismatch": (_retained_range_mismatch, "claude"),
    "pricing-unavailable": (_pricing_unavailable, "claude"),
    "pricing-fallback": (_pricing_fallback, "codex"),
    "baseline-insufficient": (_baseline_insufficient, "claude"),
    "codex-pool-split": (_codex_pool_split, "codex"),
    "codex-unmatched-window": (_codex_unmatched_window, "codex"),
    "fresh-install": (_fresh_install, "claude"),
    "provider-unavailable": (_provider_unavailable, "codex"),
    "cache-churn-dominant": (_cache_churn_dominant, "claude"),
    "cache-churn-seeded": (_cache_churn_seeded, "claude"),
    "short-context-dominant": (_short_context_dominant, "claude"),
    "fanout-dominant": (_fanout_dominant, "claude"),
    "fanout-unallocated": (_fanout_unallocated, "claude"),
    "codex-fanout": (_codex_fanout, "codex"),
    "codex-short-context": (_codex_short_context, "codex"),
    "unknown-context-window": (_unknown_context_window, "claude"),
    "budget-exhausted-conversation": (_budget_exhausted_conversation, "claude"),
    "s3-thin-population": (_s3_thin_population, "claude"),
    "s3-thin-usd-coverage": (_s3_thin_usd_coverage, "claude"),
    # The one scenario measured and rendered in a real IANA zone rather than
    # in `Etc/UTC`. Every other fixture runs in the one zone whose rendering
    # looked correct while S2's zone defect was live, so this is the only
    # golden that can catch a zone printed as a bare numeric offset or a bound
    # converted into the wrong zone.
    #
    # It reuses `_short_context_dominant` rather than `_model_dominant`. The
    # `model-dominant` seed produces no S3 row at all, so the zone golden
    # differed from `model-dominant`'s by exactly one bracketed label and could
    # not fail for any reason that golden would not also fail for. This seed
    # renders an S3 contributor with its evidence line, three accounting
    # contributors, a 5-hour block subject whose label IS an instant, a
    # no-contributor section and an `insufficient_population` section — so the
    # whole terminal surface is covered under a non-UTC zone.
    "display-zone-non-utc": (_short_context_dominant, "claude"),
    "control-s3-zero-match": (_control_s3_zero_match, "claude"),
    # #834 S2 (#800). The Claude control above is registered as a CLAUDE
    # scenario and therefore constrains only the Claude evaluator; these two
    # are its Codex counterparts. The first proves the Codex class still
    # DECIDES and answers `no_contributor` where the evidence is present and
    # matches nothing; the second proves a per-key evidence classification,
    # because a global one would leave its coverage at 1.0.
    "control-s3-codex-zero-match": (_control_s3_codex_zero_match, "codex"),
    "codex-mixed-transcript-coverage": (
        _codex_mixed_transcript_coverage, "codex"),
}


def _build(name: str, factory, source: str, out_root: Path) -> None:
    out_dir = out_root / name
    app_dir = out_dir / ".local" / "share" / "cctally"
    app_dir.mkdir(parents=True, exist_ok=True)
    seed = factory()
    if source == "codex" and seed.conversation_rows:
        # A Codex oracle cannot read a Claude transcript store, so a Codex
        # scenario that seeded one would make the transcript-availability rule
        # above state something false about it.
        raise SystemExit(
            f"{name}: a codex scenario must seed no Claude transcript rows")
    _write(seed, app_dir)
    (out_dir / "oracle.json").write_text(
        json.dumps(seed.oracle(name, source), indent=2, sort_keys=True) + "\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Override output directory. Defaults to the in-tree path "
             "tests/fixtures/explain/. Used by cctally-explain-test to write "
             "into a per-run scratch dir so the in-tree fixtures stay "
             "byte-stable across harness runs.",
    )
    parser.add_argument("--scenario", action="append", default=None,
                        help="Build only the named scenario (repeatable).")
    args = parser.parse_args(argv)
    out_root = args.out if args.out is not None else FIXTURES_DIR
    for name in (args.scenario or list(SCENARIOS)):
        factory, source = SCENARIOS[name]
        _build(name, factory, source, out_root)
        print(f"built: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
