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


def _parse_usage_entries(
    jsonl_path: pathlib.Path,
    range_start: dt.datetime,
    range_end: dt.datetime,
    seen_hashes: set[str] | None = None,
) -> list[UsageEntry]:
    """Parse assistant entries from a JSONL file within the given time range."""
    entries: list[UsageEntry] = []
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

                # Deduplicate by message.id + requestId (same as ccusage)
                msg_id = msg.get("id")
                req_id = obj.get("requestId")
                if msg_id is not None and req_id is not None:
                    entry_hash = f"{msg_id}:{req_id}"
                    if seen_hashes is not None:
                        if entry_hash in seen_hashes:
                            continue
                        seen_hashes.add(entry_hash)

                cost_usd_raw = obj.get("costUSD")
                cost_usd = (
                    float(cost_usd_raw)
                    if cost_usd_raw is not None
                    else None
                )

                entries.append(UsageEntry(
                    timestamp=ts,
                    model=model.strip(),
                    usage=usage,
                    cost_usd=cost_usd,
                ))
    except OSError as exc:
        _eprint(f"[cost] could not read {jsonl_path}: {exc}")

    return entries


def _iter_jsonl_entries_with_offsets(fh):
    """Yield (byte_offset, UsageEntry, msg_id, req_id) for each assistant
    entry starting from fh's current position.

    Uses readline()+tell() rather than `for line in fh` so byte offsets are
    accurate for resume-from-offset after partial ingests. Malformed JSON
    and non-assistant lines are skipped, but the offset still advances past
    them so they are never re-read. Range filtering is intentionally NOT
    done here — filters are applied at query time by iter_entries().
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

        try:
            ts = dt.datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue

        msg_id = msg.get("id")
        req_id = obj.get("requestId")
        cost_usd_raw = obj.get("costUSD")
        cost_usd = float(cost_usd_raw) if cost_usd_raw is not None else None

        yield (
            offset,
            UsageEntry(
                timestamp=ts,
                model=model.strip(),
                usage=usage,
                cost_usd=cost_usd,
            ),
            msg_id,
            req_id,
        )


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
