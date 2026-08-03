#!/usr/bin/env python3
"""Build the committed Codex reader-path wire fixture (#463 S1, finding F23).

Emits ``tests/fixtures/codex-reader/wire-detail.json`` — the exact JSON envelope
``get_codex_conversation`` produces for a synthetic Codex conversation. The
frontend harness ``bin/cctally-frontend-test`` feeds that envelope through
``adaptQualifiedDetail`` and pins the adapted neutral model as a second golden,
so the two sides drift independently: a server change to segmentation or block
building moves this file, a client change to adaptation moves the adapted one.

**The fixture must stay synthetic.** ``.mirror-allowlist`` publishes ``tests/**``
to the public repository, so the corpus is ``tests/fixtures/codex-parity/v1/
rollouts/`` and never the operator's real store. No production rollout UUIDs, no
``session_id``/``cell_id`` values, and no real filesystem paths may appear here.

Isolation is mandatory and is set up BEFORE any cctally module is imported,
because ``_cctally_core._init_paths_from_env()`` binds every path constant at
import time. Four variables, all four required:

  * ``CCTALLY_DATA_DIR``            — the scratch APP_DIR (cache.db, conversations.db)
  * ``CLAUDE_CONFIG_DIR``           — a scratch dir containing ``projects/``
  * ``CODEX_HOME``                  — the scratch rollout root
  * ``CCTALLY_DISABLE_DEV_AUTODETECT`` — keep the dev-checkout branch out of it

Omitting ``CODEX_HOME`` makes the ingest walk the operator's real
``~/.codex/sessions``, which both stalls and leaks production identifiers into a
publicly-mirrored fixture.

Idempotent AND machine-independent: rerunning anywhere overwrites the fixture
with the same bytes. That second property needs one deliberate step. Every
opaque identity the server emits — the ``v1.`` conversation key, ``civ1_`` item
keys, ``cbk1_`` block keys — is a hash over the provider root and the rollout's
absolute ``source_path``, so a scratch tree at a different absolute path yields
different identities for identical content. Emitting those verbatim would make
the file churn on every regeneration and would make a real server-side diff
unreviewable. ``_normalize_opaque_keys`` therefore rewrites each distinct opaque
value to a synthetic one derived from its family and its first-appearance
ordinal, consistently across the whole envelope so referential integrity holds
(an item's ``member_item_keys`` and a card's ``event_block_key`` still name the
same things they named before).

The tradeoff is stated rather than hidden: this fixture does NOT pin the
server's key *derivation*. That is proven directly in
``tests/test_codex_segments.py`` and ``tests/test_codex_conversation_*.py``.
What it pins is the envelope's shape and content — which is what the reader
renders and what the adapted golden is there to catch.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "codex-reader"

# TWO scenarios, because one is not enough to pin what F23 exists to pin.
#
#   * modern-full exercises every record family the reader renders — prompt,
#     assistant, reasoning (both extraction paths), function/custom/search/web
#     tool calls with their outputs, the patch and MCP completion events, and
#     the task lifecycle — in one small, reviewable conversation. It holds no
#     turn anywhere near the 40-block segment budget, so EVERY item in it
#     carries `segment_ordinal: 0`.
#   * segmented-turn holds one turn well past that budget, so the committed
#     fixture actually carries several segments of one turn. Without it the
#     two-sided pin would not move if segment boundaries or block accounting
#     changed, which is exactly the drift F23 is meant to catch.
#
# segmented-turn is GENERATED here rather than committed to
# tests/fixtures/codex-parity/v1/rollouts/. That corpus is enumerated by
# tests/test_codex_parity_contract.py, which requires every file in it to be a
# declared REQUIRED_SCENARIO and checksums the whole tree into a versioned
# manifest, so adding a rollout there would mean a corpus version bump for a
# fixture only this builder consumes.
SCENARIOS = (
    ("modern-full", "wire-detail.json"),
    ("segmented-turn", "wire-detail-segmented.json"),
)

# A single turn shaped to cross the segment budget SEVERAL times, with a
# reasoning title sitting inside each boundary window, so the committed fixture
# exercises the semantic-boundary rule and not only the budget.
#
# Sized deliberately. Two sweeps of thirty pairs produced three items and two
# segments, so the golden pinned exactly one boundary and one non-carrier
# segment: it exercised neither segment CONTIGUITY (which needs a follower
# followed by another follower) nor more than one follower at all. Four sweeps of
# forty-five pairs put roughly 190 blocks against the 40-block segment budget, so
# the turn splits into five or so segments and every one of those relations
# appears in the pin.
_SEGMENTED_PAIRS_PER_SWEEP = 45
_SEGMENTED_SWEEPS = 4

# A SECOND multi-segment turn in the same rollout, carrying no lifecycle event
# row at all (#463 S1 remediation). Both shapes belong in the fixture because the
# client treats them differently: `adaptQualifiedOutline` drops every
# event-bearing non-compaction turn from the navigation outline, so the first
# turn above is a DROPPED multi-segment turn and this one is a KEPT multi-segment
# turn. A sweep of all 730 conversations in a real store on 2026-08-02 found 589
# multi-segment turns, every one of them event-bearing and therefore dropped, and
# no multi-segment Claude turn at all — so the kept shape exists only in
# fixtures. Before the four-sweep enlargement the segmented fixture happened to
# be the kept shape; the `patch_apply_end` event per sweep converted it, and this
# turn puts the shape back rather than trading one for the other.
#
# Note what this turn does NOT do: it does not let the committed DETAIL fixture
# witness the kept/dropped distinction, because that decision reads the outline
# envelope's `kinds` counter and a folded `patch_apply_end` leaves no event-kind
# block in detail. Both turns look identical there. The witness for the kept
# path is `conversationAdapters.test.ts`; this turn pins boundary computation
# for a turn with no bracketed patch. The shape guard in main() reads the
# outline, which is the only place the distinction can be observed.
_SEGMENTED_PLAIN_PAIRS = 45

# Deliberately large: the fixture pins the whole conversation, so a later page
# bound has something to bound. Never 0 (that is the export's unbounded sentinel
# and would make the fixture insensitive to a paging regression).
LIMIT = 500


# Every opaque identity family the detail envelope can carry. The `v1.` form is
# the qualified conversation key; the two prefixed forms are the item and block
# anchors from _lib_codex_conversation_query.
_OPAQUE_RE = re.compile(r"\A(?:v1\.[A-Za-z0-9_-]{16,}|civ1_[0-9a-f]{40}|cbk1_[0-9a-f]{40})\Z")

# The one COMPOUND identity: a reasoning heading key is a block key and the
# heading's zero-based ordinal joined by `#` (#463 S2 §2.5). It is not opaque as
# a whole, so `_OPAQUE_RE` cannot match it, and without this the fixture would
# retain the real path-derived block hash and churn on every machine — which is
# exactly what `bin/cctally-frontend-test`'s byte comparison caught.
_COMPOUND_RE = re.compile(r"\A(cbk1_[0-9a-f]{40})#(\d+)\Z")


def _synthetic_key(real: str, ordinal: int) -> str:
    """A stable stand-in for one real opaque key, in the same family."""
    digest = hashlib.sha256(
        f"cctally-codex-reader-fixture\0{ordinal}".encode("utf-8")).hexdigest()
    if real.startswith("civ1_"):
        return "civ1_" + digest[:40]
    if real.startswith("cbk1_"):
        return "cbk1_" + digest[:40]
    return "v1.fixture" + digest[:32]


def _normalize_opaque_keys(value, mapping: dict[str, str]):
    """Replace every path-derived opaque key with a reproducible stand-in.

    Walks the whole envelope so nested occurrences (``member_item_keys``, a
    card's ``event_block_key``, a child link's ``conversation_key``) map to the
    same stand-in their top-level occurrence did. Order of first appearance
    drives the numbering, and dicts are walked in sorted key order so the
    numbering does not depend on Python's insertion order.
    """
    if isinstance(value, str):
        compound = _COMPOUND_RE.match(value)
        if compound is not None:
            base, ordinal = compound.group(1), compound.group(2)
            if base not in mapping:
                mapping[base] = _synthetic_key(base, len(mapping))
            return f"{mapping[base]}#{ordinal}"
        if not _OPAQUE_RE.match(value):
            return value
        if value not in mapping:
            mapping[value] = _synthetic_key(value, len(mapping))
        return mapping[value]
    if isinstance(value, list):
        return [_normalize_opaque_keys(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_opaque_keys(value[key], mapping)
                for key in sorted(value)}
    return value


def _segmented_rollout_records() -> list[dict]:
    """One synthetic rollout whose single turn crosses the segment budget.

    Wholly synthetic: no production rollout UUID, ``session_id``, ``cell_id``,
    account key, filesystem path or conversation content appears here, because
    ``.mirror-allowlist`` publishes ``tests/**`` to the public repository.

    TWO turns, because the client treats the two multi-segment shapes
    differently and only one of them occurs in real data.

    Turn one is EVENT-BEARING: an assistant opening message, then four sweeps,
    then a closing assistant message. Each sweep is a titled reasoning row,
    forty-five call-and-output pairs, and one ``apply_patch`` call bracketed by
    its ``patch_apply_end`` event. Each function call folds its output into one
    block, so the turn runs to roughly 190 blocks against the 40-block segment
    budget and splits into five or so segments — enough for the golden to pin
    segment contiguity and several follower segments, not just one boundary.
    Each sweep's reasoning title lands past the 75 percent lookback floor of the
    segment before it, so the boundary rule closes segments at the titles rather
    than at the raw budget, and each bracketed patch fold must survive whole.
    ``adaptQualifiedOutline`` drops this turn from the navigation outline.

    Turn two is EVENT-FREE: a prompt, an assistant opening message, one titled
    reasoning row, forty-five call-and-output pairs and a closing message, which
    crosses the budget once. The navigation outline keeps it, so it is the only
    KEPT multi-segment turn anywhere in the estate — the 2026-08-02 corpus sweep
    found no kept multi-segment turn in a real store.
    """
    records: list[dict] = []
    clock = [0]

    def add(record_type: str, payload: dict) -> None:
        tick = clock[0]
        records.append({
            "payload": payload,
            "timestamp": (f"2026-07-15T{12 + tick // 3600:02d}:"
                          f"{(tick // 60) % 60:02d}:{tick % 60:02d}Z"),
            "type": record_type,
        })
        clock[0] += 1

    add("session_meta", {
        "context_window": 272000,
        "cwd": "/synthetic/root-a/project-blue",
        "git": {"branch": "fixture-branch", "repository": "fixture-repository"},
        "id": "root-thread-segmented",
        "instructions": "synthetic instructions",
        "model": "gpt-synthetic-codex",
        "model_context_window": 272000,
        "model_provider": "fixture-provider",
        "session_id": "33333333-3333-4333-8333-333333333333",
        "source": "codex",
        "thread_source": "root-thread-segmented",
        "tools": [{"name": "fixture-tool"}],
        "user": "synthetic-user",
    })
    add("turn_context", {"model": "gpt-synthetic-codex",
                         "model_context_window": 272000, "turn_id": "turn-seg"})
    add("response_item", {
        "content": [{"text": "Synthetic segmented prompt", "type": "input_text"}],
        "phase": "input", "role": "user", "type": "message"})
    # A priced turn, so the committed golden pins the cost-carrier rule rather
    # than only the shape: segment 0 carries the turn's cost and every later
    # segment reports null, never zero.
    add("event_msg", {
        "info": {
            "last_token_usage": {"cached_input_tokens": 300, "input_tokens": 1200,
                                 "output_tokens": 400,
                                 "reasoning_output_tokens": 100,
                                 "total_tokens": 1600},
            "model_context_window": 272000,
            "total_token_usage": {"total_tokens": 1600},
        },
        "type": "token_count"})
    add("response_item", {
        "content": [{"text": "Synthetic segmented assistant opening",
                     "type": "output_text"}],
        "phase": "output", "role": "assistant", "type": "message"})

    def reasoning(title: str, body: str) -> None:
        add("response_item", {
            "content": [{"text": body, "type": "reasoning_text"}],
            "encrypted_content": "fixture-encrypted",
            "summary": [{"text": f"**{title}**", "type": "summary_text"}],
            "type": "reasoning"})

    def reasoning_multi(titles: list[str], body: str) -> None:
        """One aggregate holding SEVERAL authored headings (#463 S2 §2.4).

        The real shape, and the one the decomposition kernel exists for: Codex
        joins the headings with newlines inside ONE summary entry, so
        ``_REASONING_TITLE_RE`` cannot fullmatch it, the stored projection keeps
        it as a ``summary`` blob, and the read-time decomposition is what
        recovers the individual headings.

        Without one of these, every reasoning block in both committed wire
        fixtures carried exactly one heading, so the two-sided gate pinned
        ordinal ``0`` only: a regression that returned ``headings[:1]`` or
        dropped the ``enumerate`` ordinal would have left all four fixtures
        byte-identical, and ``_COMPOUND_RE``'s ordinal group above was
        unexercised end to end.
        """
        add("response_item", {
            "content": [{"text": body, "type": "reasoning_text"}],
            "encrypted_content": "fixture-encrypted",
            "summary": [{"text": "\n".join(f"**{title}**" for title in titles),
                         "type": "summary_text"}],
            "type": "reasoning"})

    for sweep in range(_SEGMENTED_SWEEPS):
        reasoning(f"Planning synthetic sweep {sweep}",
                  f"Synthetic reasoning body {sweep}")
        for index in range(_SEGMENTED_PAIRS_PER_SWEEP):
            call_id = f"seg-{sweep}-{index:02d}"
            add("response_item", {"arguments": json.dumps({"step": index}),
                                  "call_id": call_id,
                                  "name": "fixture_function", "type": "function_call"})
            add("response_item", {"call_id": call_id,
                                  "output": {"ok": True, "step": index},
                                  "type": "function_call_output"})
        # A BRACKETED patch fold, one per sweep so at least one lands close to a
        # segment boundary. The call and its `patch_apply_end` event fold into a
        # single group, and `plan_segments` treats a fold group as atomic — so a
        # boundary must never fall between them. Without a patch in the fixture
        # that guarantee had no golden behind it.
        patch_call_id = f"seg-{sweep}-patch"
        add("response_item", {
            "call_id": patch_call_id,
            "input": ("*** Begin Patch\n*** Update File: synthetic-fixture.txt\n"
                      f"@@\n-old {sweep}\n+new {sweep}\n*** End Patch"),
            "name": "apply_patch", "status": "completed", "type": "custom_tool_call"})
        add("event_msg", {
            "call_id": patch_call_id,
            "changes": [{"path": "synthetic-fixture.txt", "status": "modified",
                         "unified_diff": ("--- a/synthetic-fixture.txt\n"
                                          "+++ b/synthetic-fixture.txt\n"
                                          f"@@ -1 +1 @@\n-old {sweep}\n+new {sweep}\n")}],
            "status": "completed", "stderr": "", "stdout": "Done!", "success": True,
            "type": "patch_apply_end"})
    add("response_item", {
        "content": [{"text": "Synthetic segmented assistant closing",
                     "type": "output_text"}],
        "phase": "output", "role": "assistant", "type": "message"})

    # The second turn: multi-segment and EVENT-FREE, so the navigation outline
    # keeps it. `token_count` is deliberately included and is not a counter-
    # example — it is accounting, `_extract_event_row` returns no row for it, and
    # it exists here only so this turn has a cost carrier of its own.
    add("turn_context", {"model": "gpt-synthetic-codex",
                         "model_context_window": 272000, "turn_id": "turn-plain"})
    add("response_item", {
        "content": [{"text": "Synthetic plain prompt", "type": "input_text"}],
        "phase": "input", "role": "user", "type": "message"})
    add("event_msg", {
        "info": {
            "last_token_usage": {"cached_input_tokens": 100, "input_tokens": 600,
                                 "output_tokens": 200,
                                 "reasoning_output_tokens": 50,
                                 "total_tokens": 800},
            "model_context_window": 272000,
            "total_token_usage": {"total_tokens": 2400},
        },
        "type": "token_count"})
    add("response_item", {
        "content": [{"text": "Synthetic plain assistant opening",
                     "type": "output_text"}],
        "phase": "output", "role": "assistant", "type": "message"})
    reasoning("Planning the synthetic plain turn", "Synthetic plain reasoning body")
    # Placed in the EVENT-FREE turn, which `adaptQualifiedOutline` keeps, so the
    # multi-heading aggregate reaches the adapted goldens as well as the wire
    # fixture. It carries no stored `title`, so it is not a segmentation boundary
    # and the tuned boundary geometry of turn one is untouched.
    reasoning_multi(
        ["Checking the synthetic plain inputs",
         "Writing the synthetic plain result"],
        "Synthetic plain multi-heading body")
    for index in range(_SEGMENTED_PLAIN_PAIRS):
        call_id = f"plain-{index:02d}"
        add("response_item", {"arguments": json.dumps({"step": index}),
                              "call_id": call_id,
                              "name": "fixture_function", "type": "function_call"})
        add("response_item", {"call_id": call_id,
                              "output": {"ok": True, "step": index},
                              "type": "function_call_output"})
    add("response_item", {
        "content": [{"text": "Synthetic plain assistant closing",
                     "type": "output_text"}],
        "phase": "output", "role": "assistant", "type": "message"})
    return records


def _load_cctally():
    """Path-load the extensionless ``bin/cctally`` as module ``"cctally"``.

    A plain ``import cctally`` cannot find an extensionless file, and
    ``_cctally_cache._cctally()`` resolves ``sys.modules["cctally"]`` at call
    time, so the sibling modules are unusable until this registration happens.
    Same idiom as ``bin/build-bench-fixtures.py``. Must run AFTER the env pin,
    because the path globals are captured at import.
    """
    cached = sys.modules.get("cctally")
    if cached is not None:
        return cached
    path = BIN_DIR / "cctally"
    loader = importlib.machinery.SourceFileLoader("cctally", str(path))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _build(scratch: pathlib.Path) -> dict[str, dict]:
    codex_home = scratch / "codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True, exist_ok=True)
    for scenario, _name in SCENARIOS:
        rollout = sessions / f"{scenario}.jsonl"
        if scenario == "segmented-turn":
            rollout.write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n"
                        for record in _segmented_rollout_records()),
                encoding="utf-8")
        else:
            shutil.copyfile(CORPUS / f"{scenario}.jsonl", rollout)

    claude_config = scratch / "claude"
    (claude_config / "projects").mkdir(parents=True, exist_ok=True)

    os.environ["CCTALLY_DATA_DIR"] = str(scratch / "data")
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_config)
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_UPDATE_CHECK"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ["TZ"] = "Etc/UTC"

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))

    cctally = _load_cctally()
    # CCTALLY_DATA_DIR is captured at import; re-run so the path globals point
    # at THIS scratch tree even if a prior import already bound them.
    cctally._cctally_core._init_paths_from_env()

    from _cctally_cache import (  # noqa: E402
        open_cache_db, open_conversations_db, sync_codex_cache,
        sync_codex_conversations,
    )
    import _lib_codex_conversation_query as q  # noqa: E402

    conn = open_cache_db()
    keys: dict[str, str] = {}
    try:
        sync_codex_cache(conn, rebuild=True)
        for scenario, _name in SCENARIOS:
            row = conn.execute(
                "SELECT conversation_key FROM codex_conversation_threads "
                "WHERE source_path LIKE ?", (f"%/{scenario}.jsonl",)).fetchone()
            if row is None:
                raise SystemExit(
                    "build-codex-reader-fixtures: no conversation thread for "
                    f"{scenario}")
            keys[scenario] = row[0]
    finally:
        conn.close()

    conversations = open_conversations_db()
    try:
        sync_codex_conversations(conversations, rebuild=True)
        details = {
            scenario: q.get_codex_conversation(
                conversations, keys[scenario],
                effective_speed="standard", limit=LIMIT)
            for scenario, _name in SCENARIOS
        }
        # The detail envelope cannot answer whether the navigation outline
        # keeps or drops a turn: that decision reads the outline envelope's
        # `kinds` counter, and a folded lifecycle event leaves no event-kind
        # block behind in detail. The shape guard in main() needs the outline.
        outlines = {
            scenario: q.get_codex_conversation_outline(
                conversations, keys[scenario], effective_speed="standard")
            for scenario, _name in SCENARIOS
        }
        return details, outlines
    finally:
        conversations.close()


# Every opaque token shape, found ANYWHERE in the serialized fixture rather than
# only as a whole JSON string value. This is the guard, not `_normalize_opaque_keys`
# itself: a new envelope field that EMBEDS an identity inside a larger string is
# invisible to a whole-value matcher, and the fixture then silently retains a
# path-derived hash and churns on every machine. That is not hypothetical — the
# #463 S2 heading key `<block_key>#<ordinal>` did exactly this, and only
# `bin/cctally-frontend-test`'s byte comparison caught it. Extend
# `_normalize_opaque_keys` when this fires; do not relax the guard.
_IDENTITY_SCAN_RE = re.compile(
    r"(?:civ1_[0-9a-f]{40}|cbk1_[0-9a-f]{40}|v1\.[A-Za-z0-9_-]{16,})")


def _assert_no_unnormalized_identity(
    scenario: str, serialized: str, mapping: dict[str, str],
) -> None:
    synthetic = set(mapping.values())
    leaked = sorted({token for token in _IDENTITY_SCAN_RE.findall(serialized)
                     if token not in synthetic})
    if leaked:
        raise SystemExit(
            f"build-codex-reader-fixtures: {scenario} retains "
            f"{len(leaked)} path-derived identity token(s) that "
            "_normalize_opaque_keys did not rewrite, so the fixture would "
            "differ on every machine. Teach the normalizer the shape that "
            f"carries them. First: {leaked[:3]}")


def main(argv: list[str] | None = None) -> int:
    # ``--out-dir`` exists so ``bin/cctally-frontend-test`` can regenerate into a
    # scratch directory and byte-compare against the committed files (#463 S2
    # §6.4). Before that gate, the harness adapted the COMMITTED fixture and
    # never regenerated it, so a server-side wire change left the fixture stale
    # while the frontend golden still passed. A harness that regenerated in place
    # would hide the same drift by overwriting the evidence, so the comparison
    # target must be a directory the harness owns.
    parser = argparse.ArgumentParser(
        description="Build the committed Codex reader-path wire fixtures.")
    parser.add_argument(
        "--out-dir", default=None,
        help="write the fixtures here instead of tests/fixtures/codex-reader/")
    args = parser.parse_args(argv)
    out_dir = pathlib.Path(args.out_dir).resolve() if args.out_dir else OUT_DIR
    with tempfile.TemporaryDirectory(prefix="cctally-codex-reader-") as tmp:
        details, outlines = _build(pathlib.Path(tmp))
    out_dir.mkdir(parents=True, exist_ok=True)
    for scenario, out_name in SCENARIOS:
        out_file = out_dir / out_name
        detail = details[scenario]
        if detail.get("status") != "ok":
            raise SystemExit(
                f"build-codex-reader-fixtures: {scenario} detail status is "
                f"{detail.get('status')!r}, expected 'ok'")
        # A fresh key mapping per scenario, so one fixture's numbering never
        # depends on the other's content.
        mapping: dict[str, str] = {}
        detail = _normalize_opaque_keys(detail, mapping)
        serialized = json.dumps(
            detail, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _assert_no_unnormalized_identity(scenario, serialized, mapping)
        out_file.write_text(serialized, encoding="utf-8")
        items = detail.get("items") or []
        blocks = sum(len(item.get("blocks") or []) for item in items)
        ordinals = sorted({item.get("segment_ordinal") for item in items})
        print(f"wrote {out_file} "
              f"({len(items)} items, {blocks} blocks, "
              f"segment ordinals {ordinals})")
    segmented = details["segmented-turn"]
    if max((item.get("segment_ordinal") or 0)
           for item in segmented["items"]) < 1:
        raise SystemExit(
            "build-codex-reader-fixtures: the segmented-turn fixture carries no "
            "segment past ordinal 0 — it would pin nothing about segmentation")
    # Guard the two SHAPES, not the turn count. Counting multi-segment turns
    # cannot see the property that decides kept-versus-dropped, so adding a
    # lifecycle event row to the event-free turn would keep the count at two
    # and silently retire the kept shape — which is exactly how it was lost
    # once already. Read the decision from the outline, where it is made.
    split = [turn for turn in outlines["segmented-turn"]["turns"]
             if len(turn.get("segment_item_keys") or ()) > 1]
    dropped = [turn for turn in split
               if (turn.get("kinds") or {}).get("meta", 0) > 0
               or (turn.get("kinds") or {}).get("event", 0) > 0]
    kept = [turn for turn in split if turn not in dropped]
    if not dropped or not kept:
        raise SystemExit(
            "build-codex-reader-fixtures: the segmented-turn fixture must carry "
            "BOTH multi-segment shapes, and holds "
            f"{len(dropped)} the navigation outline drops and {len(kept)} it "
            "keeps. The kept shape exists only here and in "
            "conversationAdapters.test.ts; no production conversation has one "
            "(all 589 multi-segment Codex turns measured 2026-08-02 carry "
            "event rows). See docs/codex-gotchas.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
