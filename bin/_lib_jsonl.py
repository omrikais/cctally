"""JSONL entry parsing for Claude + Codex session files.

Pure-fn layer (no I/O at import time): holds the two streaming readers
that delta-resume Claude `~/.claude/projects/**/*.jsonl` and Codex
`~/.codex/sessions/**/*.jsonl` files, the bulk parser that does
range-filtered + msg-id/req-id-dedup'd reads (the legacy entry point
preserved for paths that don't go through `cache.db`), and the
dataclasses they produce (`UsageEntry`, `CodexEntry`) + the mutable
cross-call tracker (`_CodexIterState`).

`bin/cctally` re-exports every public symbol below so the ~50 internal
call sites + SourceFileLoader-based tests
(`tests/test_dashboard_api_block`, `tests/test_blocks_recorded_anchor`,
`bin/build-codex-fixtures.py`) resolve unchanged. Zero call-time
back-references to `bin/cctally`: this module is a pure leaf in the
sibling graph. The only cross-module helper used (`eprint`) is
duplicated as a private `_eprint` per the split design's §5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


@dataclass
class UsageEntry:
    timestamp: dt.datetime
    model: str
    usage: dict[str, Any]
    cost_usd: float | None
    source_path: str   # REQUIRED — absolute JSONL path; basename used in
                       # --debug samples (issue #89). Always supply a
                       # non-empty path-like string; "" is invalid per
                       # spec R5 (no silent empty-string passthrough,
                       # crashes loudly at construction instead).


@dataclass
class CodexEntry:
    """One emitted Codex `token_count` event row.

    Mirrors the columns of codex_session_entries. `last_token_usage` fields
    are used (per-turn deltas), not the cumulative totals.
    """
    timestamp: dt.datetime
    session_id: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    source_path: str


def _entry_token_total(entry: "UsageEntry") -> int:
    """Sum of the four billed token fields. Mirrors ccusage's
    `usage_token_total` in rust/crates/ccusage/src/claude_loader.rs:516."""
    u = entry.usage
    return (
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("output_tokens", 0) or 0)
        + int(u.get("cache_creation_input_tokens", 0) or 0)
        + int(u.get("cache_read_input_tokens", 0) or 0)
    )


def _should_replace(
    candidate: "UsageEntry", existing: "UsageEntry"
) -> bool:
    """Port of ccusage's `should_replace_deduped_entry` in
    rust/crates/ccusage/src/claude_loader.rs:531. Higher token total wins;
    on equal totals, the row with `speed` set (non-null) wins (the post-stream
    finalization row carries `speed`; streaming intermediates don't).

    The `usage.get("speed") is not None` check matches the SQL UPDATE WHERE
    clause's `excluded.speed IS NOT NULL` in `sync_cache`'s INSERT … ON
    CONFLICT … DO UPDATE — `speed` is materialized into its own
    `session_entries.speed` column (#181), so the tiebreak no longer
    `json_extract`s the blob — keeping the direct-parse fallback and
    cache-ingest paths in lockstep on the rare-but-possible "explicit JSON
    null" payload.
    """
    c_total = _entry_token_total(candidate)
    e_total = _entry_token_total(existing)
    if c_total != e_total:
        return c_total > e_total
    return (candidate.usage.get("speed") is not None
            and existing.usage.get("speed") is None)


def _parse_usage_entries(
    jsonl_path: pathlib.Path,
    range_start: dt.datetime,
    range_end: dt.datetime,
    *,
    dedupe_map: "dict[str, UsageEntry]",
) -> list[UsageEntry]:
    """Parse one JSONL file's assistant entries within [range_start, range_end].

    Dedup contract (matches ccusage's `push_deduped_entry`):
    - Entries with non-null (msg_id, req_id) go into `dedupe_map`; if a key
      already maps to an entry, replace iff `_should_replace(candidate, existing)`.
    - Entries with null msg_id or null req_id (rare in modern Claude Code,
      but possible on synthetic / legacy emissions) skip the dedup map and
      land in a separate list — partial UNIQUE index on the cache mirrors
      this behavior.
    - `<synthetic>` model rows are dropped entirely (matches ccusage's
      claude_loader.rs:454).

    Caller is responsible for sorting the returned list by timestamp if
    needed; `_collect_entries_direct` does this once across all files
    after flattening `dedupe_map.values()`.
    """
    no_key_entries: list[UsageEntry] = []
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "assistant":
                    continue

                ts_raw = obj.get("timestamp")
                if not isinstance(ts_raw, str) or not ts_raw.strip():
                    continue

                msg = obj.get("message")
                if not isinstance(msg, dict):
                    msg = obj

                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                model = msg.get("model") or obj.get("model")
                if not isinstance(model, str) or not model.strip():
                    continue
                model = model.strip()
                if model == "<synthetic>":
                    # Matches ccusage's claude_loader.rs:454 — synthetic
                    # placeholder rows carry no billable usage.
                    continue

                try:
                    ts = dt.datetime.fromisoformat(
                        ts_raw.strip().replace("Z", "+00:00")
                    )
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    continue

                if ts < range_start or ts > range_end:
                    continue

                msg_id = msg.get("id")
                req_id = obj.get("requestId")
                cost_usd_raw = obj.get("costUSD")
                cost_usd = (
                    float(cost_usd_raw)
                    if cost_usd_raw is not None
                    else None
                )

                entry = UsageEntry(
                    timestamp=ts,
                    model=model,
                    usage=usage,
                    cost_usd=cost_usd,
                    source_path=str(jsonl_path),
                )

                if msg_id is None or req_id is None:
                    no_key_entries.append(entry)
                    continue
                key = f"{msg_id}:{req_id}"
                existing = dedupe_map.get(key)
                if existing is None or _should_replace(entry, existing):
                    dedupe_map[key] = entry
    except OSError as exc:
        _eprint(f"[cost] could not read {jsonl_path}: {exc}")

    # The function returns ONLY this file's no-key entries; the caller
    # flattens `dedupe_map.values()` once at the end across all files.
    return no_key_entries


def parse_cost_entry(obj, path_str: str):
    """Pure per-line cost parser: given a parsed JSONL object, return
    ``(UsageEntry, msg_id, req_id)`` when it is a billable assistant entry, or
    ``None`` otherwise (non-assistant, missing/invalid usage, model, or
    timestamp, or a ``<synthetic>`` placeholder). No I/O, no byte offset — the
    caller owns the readline()+tell() loop.

    Extracted (#138) so the streaming ``_iter_jsonl_entries_with_offsets`` reader
    and the fused single-pass sync walker (``_cctally_cache._iter_sync_entries``)
    share ONE gating implementation — each JSONL line is ``json.loads``-parsed
    once and classified once, never re-parsed for a separate second walk.
    """
    if obj.get("type") != "assistant":
        return None

    ts_raw = obj.get("timestamp")
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None

    msg = obj.get("message")
    if not isinstance(msg, dict):
        msg = obj

    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None

    model = msg.get("model") or obj.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    if model == "<synthetic>":
        # Matches ccusage's claude_loader.rs:454. Filtered here so the cache
        # ingest path can't accidentally store these rows even if a downstream
        # loop forgets to double-check (see `sync_cache` in _cctally_cache.py).
        return None

    try:
        ts = dt.datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None

    msg_id = msg.get("id")
    req_id = obj.get("requestId")
    cost_usd_raw = obj.get("costUSD")
    cost_usd = float(cost_usd_raw) if cost_usd_raw is not None else None

    return (
        UsageEntry(
            timestamp=ts,
            model=model,
            usage=usage,
            cost_usd=cost_usd,
            source_path=path_str,
        ),
        msg_id,
        req_id,
    )


def _iter_jsonl_entries_with_offsets(fh, path_str: str):
    """Yield (byte_offset, UsageEntry, msg_id, req_id) for each assistant
    entry starting from fh's current position.

    Uses readline()+tell() rather than `for line in fh` so byte offsets are
    accurate for resume-from-offset after partial ingests. Malformed JSON
    and non-assistant lines are skipped, but the offset still advances past
    them so they are never re-read. Range filtering is intentionally NOT
    done here — filters are applied at query time by iter_entries(). The
    per-line gating lives in ``parse_cost_entry`` (shared with the fused
    single-pass sync walker, #138).
    """
    while True:
        offset = fh.tell()
        line = fh.readline()
        if not line:
            return
        if not line.endswith("\n"):
            # Partial tail line — writer is mid-flight. Rewind so the
            # next sync re-reads this line once the newline is in place.
            # Without this, sync_cache would store fh.tell() (past the
            # partial) as last_byte_offset and permanently skip the entry.
            fh.seek(offset)
            return
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        parsed = parse_cost_entry(obj, path_str)
        if parsed is None:
            continue
        entry, msg_id, req_id = parsed
        yield (offset, entry, msg_id, req_id)


_CODEX_FILENAME_UUID_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-fA-F-]{36})\.jsonl$"
)


@dataclass
class _CodexIterState:
    """Mutable per-file tracker exposed to callers of
    `_iter_codex_jsonl_entries_with_offsets` so the iterator's terminal
    session_id/model are visible even when the delta window ends on a
    `session_meta` or `turn_context` event with no subsequent yielded
    `token_count`. Callers seed it with previously-persisted values and
    read it back after the iterator drains.
    """
    session_id: str | None = None
    model: str | None = None
    total_tokens: int = 0


def _iter_codex_jsonl_entries_with_offsets(
    fh,
    path_str: str,
    *,
    initial_session_id: str | None = None,
    initial_model: str | None = None,
    initial_total_tokens: int = 0,
    state: _CodexIterState | None = None,
):
    """Yield (line_offset, CodexEntry) for each billable `token_count` event.

    Maintains per-file state (session_id, model) as records are streamed.
    Callers performing a delta resume from non-zero byte offset should pass
    the previously-observed session_id/model as initial_session_id and
    initial_model so attribution stays correct even if the new byte range
    contains no fresh session_meta / turn_context record.

    If `state` is supplied it is updated in-place on every `session_meta`
    / `turn_context` record regardless of whether any subsequent
    `token_count` actually yields. This lets callers observe the iterator's
    terminal state even when the delta window ends on a metadata record —
    otherwise `last_model` would silently persist a stale value and the
    next resume would mis-attribute the first post-resume token_count.

    Skips token_count events with payload.info == None (rate-limit-only
    events). Falls back to filename-derived session_id with a one-shot warning
    if session_meta is never observed.

    Codex CLI emits multiple `token_count` events per completed turn (UI/
    turn_context updates re-emit the same `last_token_usage` while the
    cumulative `info.total_token_usage.total_tokens` stays flat). To avoid
    double-counting, we track the cumulative total across yields and skip
    any event whose cumulative total is not strictly greater than the
    previously-seen cumulative. Callers doing delta resumes should pass the
    last persisted cumulative as `initial_total_tokens`. If `total_token_usage`
    is missing or non-dict (older Codex builds), we fall back to yielding
    unconditionally — preserving legacy behavior on those rollouts.

    Readline()+tell() is used rather than `for line in fh` so byte offsets
    are accurate for resume-from-offset after partial ingests. Partial-tail
    lines (no trailing \\n) trigger a seek-back so the next sync re-reads
    the line once the newline is flushed.
    """
    if state is None:
        state = _CodexIterState()
    # Seed the tracker from the kwargs. Kwargs take priority only when the
    # caller-supplied state has no value yet — this preserves the existing
    # contract for callers that pass kwargs without a state object, while
    # letting callers who DO pass a pre-populated state see it honored.
    if state.session_id is None and initial_session_id is not None:
        state.session_id = initial_session_id
    if state.model is None and initial_model is not None:
        state.model = initial_model
    last_total_tokens: int = int(initial_total_tokens or 0)
    # Suppress the filename-UUID fallback warning when we already have a
    # seeded session_id (delta resume path). Without this, every resume
    # into a slice of the file that doesn't re-observe session_meta would
    # noisily warn even though attribution is correct.
    filename_session_id_warned = state.session_id is not None
    filename_uuid_match = _CODEX_FILENAME_UUID_RE.search(path_str)
    filename_uuid = filename_uuid_match.group(1) if filename_uuid_match else None

    while True:
        offset = fh.tell()
        line = fh.readline()
        if not line:
            return
        if not line.endswith("\n"):
            fh.seek(offset)
            return
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        rtype = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

        if rtype == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str) and sid:
                state.session_id = sid
            continue

        if rtype == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                state.model = m.strip()
            continue

        if rtype != "event_msg":
            continue

        if payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        ltu = info.get("last_token_usage")
        if not isinstance(ltu, dict):
            continue

        # Dedupe re-emitted token_count events. Codex re-emits `last_token_usage`
        # on UI/turn_context updates with a flat `total_token_usage.total_tokens`;
        # only yield once per actual turn by requiring the cumulative to strictly
        # advance. If `total_token_usage` is missing or non-dict (older Codex
        # builds), skip the guard and yield — preserving legacy behavior.
        ttu = info.get("total_token_usage")
        if isinstance(ttu, dict):
            try:
                cumulative = int(ttu.get("total_tokens") or 0)
            except (TypeError, ValueError):
                cumulative = 0
            if cumulative <= last_total_tokens:
                continue
        else:
            cumulative = None  # type: ignore[assignment]

        ts_raw = obj.get("timestamp")
        if not isinstance(ts_raw, str) or not ts_raw.strip():
            continue
        try:
            ts = dt.datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

        session_id = state.session_id
        if session_id is None:
            session_id = filename_uuid
            if session_id is not None and not filename_session_id_warned:
                _eprint(
                    f"[codex] session_meta not seen in {path_str}; "
                    f"falling back to filename UUID {session_id}"
                )
                filename_session_id_warned = True
            if session_id is None:
                # No session_meta and no parseable filename UUID — skip row.
                continue

        model = state.model or "unknown"

        def _int(key: str) -> int:
            v = ltu.get(key)
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        yield (
            offset,
            CodexEntry(
                timestamp=ts,
                session_id=session_id,
                model=model,
                input_tokens=_int("input_tokens"),
                cached_input_tokens=_int("cached_input_tokens"),
                output_tokens=_int("output_tokens"),
                reasoning_output_tokens=_int("reasoning_output_tokens"),
                total_tokens=_int("total_tokens"),
                source_path=path_str,
            ),
        )
        # Advance the cumulative watermark only after a successful yield so
        # resume-from-offset continues to dedupe against the last counted turn.
        if isinstance(ttu, dict) and cumulative is not None:
            last_total_tokens = cumulative
