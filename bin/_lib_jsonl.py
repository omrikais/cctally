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
import math
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from _lib_source_identity import canonical_identity_from_root_key


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

    Per-line gating is `parse_cost_entry`'s — the SINGLE implementation shared
    with the cache-ingest path (#279 S3 F2). This fallback (used when cache.db
    can't be opened) no longer re-implements the type -> timestamp -> usage ->
    model -> synthetic -> costUSD gating inline, so the two paths can never
    drift. The only fallback-specific step is the range filter, applied to the
    constructed `entry.timestamp` (inclusive bounds preserved).

    Dedup contract (matches ccusage's `push_deduped_entry`):
    - Entries with non-null (msg_id, req_id) go into `dedupe_map`; if a key
      already maps to an entry, replace iff `_should_replace(candidate, existing)`.
    - Entries with null msg_id or null req_id (rare in modern Claude Code,
      but possible on synthetic / legacy emissions) skip the dedup map and
      land in a separate list — partial UNIQUE index on the cache mirrors
      this behavior.
    - `<synthetic>` model rows are dropped entirely (matches ccusage's
      claude_loader.rs:454) — `parse_cost_entry` returns None for them.

    Caller is responsible for sorting the returned list by timestamp if
    needed; `_collect_entries_direct` does this once across all files
    after flattening `dedupe_map.values()`.
    """
    no_key_entries: list[UsageEntry] = []
    path_str = str(jsonl_path)
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
                parsed = parse_cost_entry(obj, path_str)
                if parsed is None:
                    continue
                entry, msg_id, req_id = parsed
                if entry.timestamp < range_start or entry.timestamp > range_end:
                    continue
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


_DELIBERATE_SKIP_REASONS = ("not-assistant", "synthetic")


def _classify_cost_entry(obj, path_str: str):
    """Reason-returning core of ``parse_cost_entry`` (#279 S2 F1). Returns
    ``(parsed, reason)`` where exactly one is non-None; ``parsed`` is the
    ``(UsageEntry, msg_id, req_id)`` tuple. Reasons: ``not-assistant`` /
    ``synthetic`` (deliberate skips) and the drift trio ``bad-timestamp`` /
    ``no-usage`` / ``no-model``. The gating ORDER is the contract — it must
    stay identical to the pre-#279 ``parse_cost_entry`` body (type ->
    raw-timestamp -> usage -> model -> synthetic -> timestamp-parse) so the
    public wrapper's behavior is unchanged.
    """
    if obj.get("type") != "assistant":
        return None, "not-assistant"

    ts_raw = obj.get("timestamp")
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None, "bad-timestamp"

    msg = obj.get("message")
    if not isinstance(msg, dict):
        msg = obj

    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None, "no-usage"

    model = msg.get("model") or obj.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "no-model"
    model = model.strip()
    if model == "<synthetic>":
        # Matches ccusage's claude_loader.rs:454. Filtered here so the cache
        # ingest path can't accidentally store these rows even if a downstream
        # loop forgets to double-check (see `sync_cache` in _cctally_cache.py).
        return None, "synthetic"

    try:
        ts = dt.datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None, "bad-timestamp"

    msg_id = msg.get("id")
    req_id = obj.get("requestId")
    cost_usd_raw = obj.get("costUSD")
    try:
        cost_usd = float(cost_usd_raw) if cost_usd_raw is not None else None
    except (TypeError, ValueError):
        # Drift-hardened (#279 S3, Codex gate F1): a malformed costUSD must
        # not abort a whole sync/read — degrade to "no raw cost"; the
        # token-derived cost still computes at query time.
        cost_usd = None

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
    ), None


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

    The body now delegates to ``_classify_cost_entry`` (#279 S2 F1) so the
    parse-health drift classifier and this cost parser can never disagree on
    the gating order; the public contract (returns the tuple or ``None``) is
    unchanged.
    """
    parsed, _reason = _classify_cost_entry(obj, path_str)
    return parsed


def assistant_skip_reason(obj) -> "str | None":
    """Drift classifier for parse-health counters (#279 S2 F1): the reason
    a line that LOOKS like an assistant cost entry was skipped, or None
    when the skip is deliberate (non-assistant, ``<synthetic>``) or the
    line parses fine. Callers invoke this only for lines where
    ``parse_cost_entry`` returned None, so the double-classify cost is
    bounded to skipped lines."""
    parsed, reason = _classify_cost_entry(obj, "")
    if parsed is not None or reason in _DELIBERATE_SKIP_REASONS:
        return None
    return reason


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
    # #279 S3 F1: the REAL dedup watermark. Seeded by the caller (non-zero
    # wins over the initial_total_tokens kwarg) and STAMPED by the iterator on
    # every yield to the cumulative `total_token_usage.total_tokens` the guard
    # admitted — so after the iterator drains this equals the guard's terminal
    # watermark by construction, and the caller persists exactly it (replacing
    # the old reconstructed initial+Σ(per-turn) sum, which could diverge).
    total_tokens: int = 0
    # #279 S2 F1 parse-health counters — per-iterator-call; sync_codex_cache
    # folds them into CodexIngestStats after each file drains. Reason
    # vocabulary: info-non-dict / no-last-token-usage / bad-timestamp /
    # no-session-id. Rate-limit-only events (info None) and cumulative-dedup
    # re-emissions are NORMAL and never counted.
    lines_seen: int = 0
    lines_malformed: int = 0
    token_events_skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)
    thread: "CodexThreadMetadata | None" = None


@dataclass(frozen=True)
class CodexThreadMetadata:
    """Source facts established by one ``session_meta`` physical record."""
    source_root_key: str | None
    source_path: str
    native_thread_id: str | None
    root_thread_id: str | None
    parent_thread_id: str | None
    conversation_key: str | None
    cwd: str | None
    git_json: str | None
    source_kind: str | None
    thread_source_json: str | None
    model_provider: str | None
    context_window: int | None


@dataclass(frozen=True)
class CodexQuotaObservation:
    """One validated native quota window from a physical rollout record."""
    source: str
    source_root_key: str | None
    source_path: str
    line_offset: int
    captured_at_utc: str
    observed_slot: str
    logical_limit_key: str
    limit_id: str | None
    limit_name: str | None
    window_minutes: int
    used_percent: float
    resets_at_utc: str
    plan_type: str | None
    individual_limit_json: str | None
    reached_type: str | None


@dataclass(frozen=True)
class CodexPhysicalEvent:
    """Complete canonical payload retained for one valid JSON object line."""
    source_path: str
    line_offset: int
    source_root_key: str | None
    conversation_key: str | None
    native_thread_id: str | None
    root_thread_id: str | None
    parent_thread_id: str | None
    timestamp_utc: str | None
    record_type: str | None
    event_type: str | None
    turn_id: str | None
    call_id: str | None
    payload_json: str


@dataclass(frozen=True)
class CodexFusedEmission:
    """All source-derived facts emitted after parsing one physical record once."""
    line_offset: int
    event: CodexPhysicalEvent
    accounting: CodexEntry | None
    quotas: tuple[CodexQuotaObservation, ...]
    thread: CodexThreadMetadata | None


def _codex_skip(state: "_CodexIterState", reason: str) -> None:
    state.token_events_skipped += 1
    state.skip_reasons[reason] = state.skip_reasons.get(reason, 0) + 1


_QUOTA_ENVELOPE_FIELDS = frozenset((
    "credits", "plan_type", "limit_id", "limit_name", "individual_limit",
    "rate_limit_reached_type",
))


def _codex_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _codex_canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _parse_codex_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _format_codex_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_number(value: object) -> float | None:
    # Quota JSON fields are typed numerics.  Do not coerce strings: a malformed
    # direct value must yield to a valid typed fallback in payload.info.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _json_value_is_finite(value: object) -> bool:
    """Whether a decoded JSON value can be serialized canonically.

    ``json.loads`` rejects the literal NaN/Infinity constants through
    ``parse_constant``, but a syntactically valid exponent such as ``1e400``
    is decoded as ``float('inf')``.  Reject it before a physical event reaches
    canonical serialization with ``allow_nan=False``.
    """
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_json_value_is_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_json_value_is_finite(item) for item in value)
    return True


def _positive_whole_number(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _quota_reset_at(value: object) -> str | None:
    numeric = _finite_number(value)
    if numeric is not None:
        try:
            return _format_codex_timestamp(
                dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
            )
        except (OverflowError, OSError, ValueError):
            return None
    parsed = _parse_codex_timestamp(value)
    return _format_codex_timestamp(parsed) if parsed is not None else None


def _first_valid_string(*values: object) -> str | None:
    for value in values:
        string = _codex_string(value)
        if string is not None:
            return string
    return None


def _canonical_container(value: object) -> str | None:
    if not isinstance(value, (dict, list)):
        return None
    return _codex_canonical_json(value)


def _thread_metadata_from_session_meta(
    payload: dict[str, Any], path_str: str, source_root_key: str | None,
) -> CodexThreadMetadata:
    accounting_id = _codex_string(payload.get("id"))
    native_thread_id = _codex_string(payload.get("session_id")) or accounting_id
    root_thread_id = _codex_string(payload.get("thread_source"))
    parent_thread_id = _codex_string(payload.get("forked_from_id"))
    conversation_key = None
    if native_thread_id is not None and root_thread_id is not None:
        conversation_key = canonical_identity_from_root_key(
            "codex", "conversation", source_root_key, native_thread_id, root_thread_id
        )
    return CodexThreadMetadata(
        source_root_key=source_root_key,
        source_path=path_str,
        native_thread_id=native_thread_id,
        root_thread_id=root_thread_id,
        parent_thread_id=parent_thread_id,
        conversation_key=conversation_key,
        cwd=_codex_string(payload.get("cwd")),
        git_json=_canonical_container(payload.get("git")),
        source_kind=_codex_string(payload.get("source")),
        thread_source_json=_canonical_container(payload.get("thread_source")),
        model_provider=_codex_string(payload.get("model_provider")),
        context_window=_positive_whole_number(payload.get("context_window")),
    )


def _first_valid_individual_limit_json(
    direct: dict[str, Any], fallback: dict[str, Any],
) -> str | None:
    for envelope in (direct, fallback):
        value = envelope.get("individual_limit")
        if isinstance(value, dict):
            return _codex_canonical_json(value)
        if _finite_number(value) is not None:
            return _codex_canonical_json(value)
    return None


def _first_valid_quota_number(
    direct: dict[str, Any], fallback: dict[str, Any], key: str,
    *, minimum: float | None = None, maximum: float | None = None,
) -> float | None:
    for window in (direct, fallback):
        number = _finite_number(window.get(key))
        if number is None:
            continue
        if minimum is not None and number < minimum:
            continue
        if maximum is not None and number > maximum:
            continue
        return number
    return None


def _first_valid_window_minutes(
    direct: dict[str, Any], fallback: dict[str, Any],
) -> int | None:
    for window in (direct, fallback):
        minutes = _positive_whole_number(window.get("window_minutes"))
        if minutes is not None:
            return minutes
    return None


def _first_valid_reset_at(
    direct: dict[str, Any], fallback: dict[str, Any],
) -> str | None:
    for window in (direct, fallback):
        reset_at = _quota_reset_at(window.get("resets_at"))
        if reset_at is not None:
            return reset_at
    return None


def _codex_logical_limit_key(
    source_root_key: str | None, limit_id: str | None, observed_slot: str,
    window_minutes: int, model: str | None = None,
) -> str:
    payload = {
        "limitId": limit_id,
        "observedSlot": observed_slot,
        "source": "codex",
        "sourceRootKey": source_root_key,
        "windowMinutes": window_minutes,
    }
    if (model_pool := codex_model_scoped_quota_pool(model)) is not None:
        payload["modelPool"] = model_pool
    return _codex_canonical_json(payload)


def codex_model_scoped_quota_pool(model: object) -> str | None:
    """Return the native model pool when Codex documents it as separate.

    GPT Codex Spark runs against its own allowance and does not consume the
    standard Codex quota.  Native payloads currently reuse ``limit_id=codex``
    and the same slot/duration as the standard pool, so the sticky rollout
    model is the only retained discriminator.
    """
    if not isinstance(model, str):
        return None
    normalized = model.strip().lower()
    return normalized if "-codex-spark" in normalized else None


def _codex_quota_observations(
    obj: dict[str, Any], payload: dict[str, Any], path_str: str, line_offset: int,
    source_root_key: str | None, model: str | None,
) -> tuple[CodexQuotaObservation, ...]:
    captured_at = _parse_codex_timestamp(obj.get("timestamp"))
    if captured_at is None:
        return ()
    direct = payload.get("rate_limits")
    info = payload.get("info")
    fallback = info.get("rate_limits") if isinstance(info, dict) else None
    direct = direct if isinstance(direct, dict) else {}
    fallback = fallback if isinstance(fallback, dict) else {}
    if not direct and not fallback:
        return ()

    slots = sorted({
        slot for envelope in (direct, fallback) for slot, value in envelope.items()
        if slot not in _QUOTA_ENVELOPE_FIELDS and isinstance(value, dict)
    })
    observations: list[CodexQuotaObservation] = []
    for slot in slots:
        direct_window = direct.get(slot)
        fallback_window = fallback.get(slot)
        direct_window = direct_window if isinstance(direct_window, dict) else {}
        fallback_window = fallback_window if isinstance(fallback_window, dict) else {}
        used_percent = _first_valid_quota_number(
            direct_window, fallback_window, "used_percent", minimum=0.0, maximum=100.0
        )
        window_minutes = _first_valid_window_minutes(direct_window, fallback_window)
        resets_at_utc = _first_valid_reset_at(direct_window, fallback_window)
        if used_percent is None or window_minutes is None or resets_at_utc is None:
            continue
        limit_id = _first_valid_string(
            direct.get("limit_id"), fallback.get("limit_id")
        )
        observations.append(CodexQuotaObservation(
            source="codex",
            source_root_key=source_root_key,
            source_path=path_str,
            line_offset=line_offset,
            captured_at_utc=_format_codex_timestamp(captured_at),
            observed_slot=slot,
            logical_limit_key=_codex_logical_limit_key(
                source_root_key, limit_id, slot, window_minutes, model
            ),
            limit_id=limit_id,
            limit_name=_first_valid_string(
                direct.get("limit_name"), fallback.get("limit_name")
            ),
            window_minutes=window_minutes,
            used_percent=used_percent,
            resets_at_utc=resets_at_utc,
            plan_type=_first_valid_string(
                direct.get("plan_type"), fallback.get("plan_type")
            ),
            individual_limit_json=_first_valid_individual_limit_json(direct, fallback),
            reached_type=_first_valid_string(
                direct.get("rate_limit_reached_type"),
                fallback.get("rate_limit_reached_type"),
            ),
        ))
    return tuple(observations)


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _seed_codex_iter_state(
    state: _CodexIterState, initial_session_id: str | None,
    initial_model: str | None, initial_total_tokens: int,
) -> None:
    if state.session_id is None and initial_session_id is not None:
        state.session_id = initial_session_id
    if state.model is None and initial_model is not None:
        state.model = initial_model
    if state.total_tokens == 0 and initial_total_tokens:
        state.total_tokens = int(initial_total_tokens)


def _event_from_record(
    obj: dict[str, Any], path_str: str, line_offset: int,
    source_root_key: str | None, thread: CodexThreadMetadata | None,
) -> CodexPhysicalEvent:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    record_type = _codex_string(obj.get("type")) or _codex_string(obj.get("record_type"))
    return CodexPhysicalEvent(
        source_path=path_str,
        line_offset=line_offset,
        source_root_key=source_root_key,
        conversation_key=thread.conversation_key if thread is not None else None,
        native_thread_id=thread.native_thread_id if thread is not None else None,
        root_thread_id=thread.root_thread_id if thread is not None else None,
        parent_thread_id=thread.parent_thread_id if thread is not None else None,
        timestamp_utc=(
            _format_codex_timestamp(timestamp)
            if (timestamp := _parse_codex_timestamp(obj.get("timestamp"))) is not None
            else None
        ),
        record_type=record_type,
        event_type=_codex_string(payload.get("type")),
        turn_id=_codex_string(payload.get("turn_id")),
        call_id=_codex_string(payload.get("call_id")),
        payload_json=_codex_canonical_json(obj),
    )


def _accounting_from_record(
    obj: dict[str, Any], payload: dict[str, Any], path_str: str,
    state: _CodexIterState, last_total_tokens: int,
    filename_uuid: str | None, filename_session_id_warned: bool,
) -> tuple[CodexEntry | None, int, bool]:
    if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None, last_total_tokens, filename_session_id_warned
    info = payload.get("info")
    if info is None:
        return None, last_total_tokens, filename_session_id_warned
    if not isinstance(info, dict):
        _codex_skip(state, "info-non-dict")
        return None, last_total_tokens, filename_session_id_warned
    last_token_usage = info.get("last_token_usage")
    if not isinstance(last_token_usage, dict):
        _codex_skip(state, "no-last-token-usage")
        return None, last_total_tokens, filename_session_id_warned

    # Forked/subagent rollouts can begin with a copied slice of their parent's
    # physical history before the child's first model-bearing turn_context.
    # Those records remain in the physical event stream, but projecting them
    # into accounting would duplicate the parent's usage under model="unknown".
    if (state.model is None and state.thread is not None
            and state.thread.parent_thread_id is not None):
        return None, last_total_tokens, filename_session_id_warned

    total_token_usage = info.get("total_token_usage")
    if isinstance(total_token_usage, dict):
        try:
            cumulative = int(total_token_usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            cumulative = 0
        if cumulative <= last_total_tokens:
            return None, last_total_tokens, filename_session_id_warned
    else:
        cumulative = None

    timestamp = _parse_codex_timestamp(obj.get("timestamp"))
    if timestamp is None:
        _codex_skip(state, "bad-timestamp")
        return None, last_total_tokens, filename_session_id_warned
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
            _codex_skip(state, "no-session-id")
            return None, last_total_tokens, filename_session_id_warned

    def token_count(key: str) -> int:
        try:
            return int(last_token_usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    entry = CodexEntry(
        timestamp=timestamp,
        session_id=session_id,
        model=state.model or "unknown",
        input_tokens=token_count("input_tokens"),
        cached_input_tokens=token_count("cached_input_tokens"),
        output_tokens=token_count("output_tokens"),
        reasoning_output_tokens=token_count("reasoning_output_tokens"),
        total_tokens=token_count("total_tokens"),
        source_path=path_str,
    )
    if cumulative is not None:
        state.total_tokens = cumulative
        last_total_tokens = cumulative
    return entry, last_total_tokens, filename_session_id_warned


def _iter_codex_fused_records_with_offsets(
    fh,
    path_str: str,
    *,
    initial_session_id: str | None = None,
    initial_model: str | None = None,
    initial_total_tokens: int = 0,
    source_root_key: str | None = None,
    state: _CodexIterState | None = None,
):
    """Yield typed facts for every complete, valid Codex JSONL object.

    Binary readers retain physical byte offsets and strictly reject invalid UTF-8,
    non-finite JSON constants, and non-object JSON.  The mutable state carries
    the shipped accounting resume contract alongside the latest thread facts.
    """
    if state is None:
        state = _CodexIterState()
    _seed_codex_iter_state(state, initial_session_id, initial_model, initial_total_tokens)
    last_total_tokens = state.total_tokens
    filename_session_id_warned = state.session_id is not None
    filename_uuid_match = _CODEX_FILENAME_UUID_RE.search(path_str)
    filename_uuid = filename_uuid_match.group(1) if filename_uuid_match else None

    while True:
        line_offset = fh.tell()
        line = fh.readline()
        if not line:
            return
        if isinstance(line, bytes):
            complete = line.endswith(b"\n")
            text: str | None
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        else:
            complete = line.endswith("\n")
            text = line
        if not complete:
            fh.seek(line_offset)
            return
        if text is None:
            state.lines_seen += 1
            state.lines_malformed += 1
            continue
        stripped = text.strip()
        if not stripped:
            continue
        state.lines_seen += 1
        try:
            obj = json.loads(stripped, parse_constant=_reject_nonfinite_json_constant)
        except (json.JSONDecodeError, ValueError, TypeError):
            state.lines_malformed += 1
            continue
        if not isinstance(obj, dict):
            state.lines_malformed += 1
            continue
        if not _json_value_is_finite(obj):
            state.lines_malformed += 1
            continue

        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        thread_update = None
        if obj.get("type") == "session_meta":
            session_id = _codex_string(payload.get("id"))
            if session_id is not None:
                state.session_id = session_id
            session_model = _codex_string(payload.get("model"))
            if session_model is not None:
                state.model = session_model.strip()
            thread_update = _thread_metadata_from_session_meta(
                payload, path_str, source_root_key
            )
            state.thread = thread_update
        elif obj.get("type") == "turn_context":
            model = _codex_string(payload.get("model"))
            if model is not None:
                state.model = model.strip()

        event = _event_from_record(
            obj, path_str, line_offset, source_root_key, state.thread
        )
        accounting, last_total_tokens, filename_session_id_warned = _accounting_from_record(
            obj, payload, path_str, state, last_total_tokens, filename_uuid,
            filename_session_id_warned,
        )
        quotas = _codex_quota_observations(
            obj, payload, path_str, line_offset, source_root_key, state.model
        )
        yield CodexFusedEmission(
            line_offset=line_offset,
            event=event,
            accounting=accounting,
            quotas=quotas,
            thread=thread_update,
        )


def _iter_codex_jsonl_entries_with_offsets(
    fh,
    path_str: str,
    *,
    initial_session_id: str | None = None,
    initial_model: str | None = None,
    initial_total_tokens: int = 0,
    state: _CodexIterState | None = None,
):
    """Compatibility wrapper exposing only existing accounting emissions."""
    binary_fh = fh
    text_fh = None
    try:
        if isinstance(fh.read(0), str) and hasattr(fh, "buffer"):
            text_fh = fh
            position = fh.tell()
            fh.seek(position)
            binary_fh = fh.buffer
            binary_fh.seek(position)
    except (AttributeError, OSError, TypeError):
        pass
    try:
        for emission in _iter_codex_fused_records_with_offsets(
            binary_fh,
            path_str,
            initial_session_id=initial_session_id,
            initial_model=initial_model,
            initial_total_tokens=initial_total_tokens,
            state=state,
        ):
            if emission.accounting is not None:
                yield emission.line_offset, emission.accounting
    finally:
        if text_fh is not None:
            text_fh.seek(binary_fh.tell())
