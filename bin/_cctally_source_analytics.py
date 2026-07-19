"""SQLite adapter for S3's qualified Codex accounting foundation."""
from __future__ import annotations

import datetime as dt
import dataclasses
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
from copy import copy
from collections import defaultdict
from typing import Iterable

from _cctally_core import (
    _command_as_of,
    compute_week_bounds,
    get_week_start_name,
    parse_iso_datetime,
)
from _cctally_cache import _codex_provider_roots
from _lib_quota import build_blocks
from _lib_pricing import _calculate_codex_entry_cost
from _cctally_quota import load_codex_quota_observations
from _lib_source_analytics import (
    AnalyticsWindow,
    QUALIFIED_METADATA_WARNING,
    QualifiedCodexEntry,
    SourceResult,
    build_codex_diff_result,
    build_codex_project_result,
    build_codex_range_result,
    build_codex_report_result,
    build_codex_reuse_result,
    assign_collision_safe_project_labels,
    emitted_project_label,
    opaque_project_key,
    resolve_codex_diff_normalization,
    source_result_wire,
)


UTC = dt.timezone.utc


class QualifiedMetadataUnavailable(RuntimeError):
    """S1-qualified metadata is absent or cannot be truthfully read."""


class SourceUsageError(ValueError):
    """A provider-aware request is syntactically or semantically invalid."""


@dataclasses.dataclass(frozen=True)
class CodexProjectMetadataHealth:
    """A root-qualified partition of retained Codex accounting metadata."""

    total_rows: int
    qualified_rows: int
    missing_conversation_key_rows: int
    missing_thread_join_rows: int

    @property
    def incomplete_rows(self) -> int:
        return self.missing_conversation_key_rows + self.missing_thread_join_rows


@dataclasses.dataclass(frozen=True)
class RootedCodexAccountingEntry:
    """One cache-only Codex accounting row with authoritative root identity."""

    timestamp: dt.datetime
    session_id: str
    source_path: str
    source_root_key: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    cost_usd: float


_OPAQUE_PROJECT_KEY_RE = re.compile(r"^project:[0-9a-f]{24}$")


_QUALIFIED_CODEX_ENTRIES_SQL = """
    SELECT entries.timestamp_utc, entries.session_id, entries.source_path,
           entries.source_root_key,
           entries.conversation_key, entries.model,
           entries.input_tokens, entries.cached_input_tokens,
           entries.output_tokens, entries.reasoning_output_tokens,
           entries.total_tokens, threads.cwd, threads.git_json,
           threads.conversation_key AS joined_conversation_key,
           threads.source_root_key AS joined_source_root_key
      FROM codex_session_entries AS entries
           INDEXED BY idx_codex_entries_ts_root_conversation
      LEFT JOIN codex_conversation_threads AS threads
        ON threads.conversation_key = entries.conversation_key
       AND threads.source_root_key = entries.source_root_key
     WHERE entries.timestamp_utc >= ?
       AND entries.timestamp_utc < ?
     ORDER BY entries.timestamp_utc ASC, entries.source_root_key ASC,
              entries.conversation_key ASC, entries.id ASC
"""


_INHERITED_CODEX_PROJECT_METADATA_SQL = """
    SELECT files.source_root_key, files.path, inherited.cwd, inherited.git_json
      FROM codex_session_files AS files
      JOIN codex_conversation_threads AS inherited
        ON inherited.source_root_key = files.source_root_key
       AND inherited.native_thread_id = files.last_native_thread_id
     WHERE files.last_native_thread_id IS NOT NULL
       AND files.last_native_thread_id != ''
     ORDER BY inherited.last_seen_utc DESC, inherited.conversation_key DESC
"""


_CODEX_ACCOUNTING_ENTRIES_SQL = """
    SELECT timestamp_utc, source_root_key, conversation_key, model,
           input_tokens, cached_input_tokens, output_tokens,
           reasoning_output_tokens, total_tokens
      FROM codex_session_entries
     WHERE timestamp_utc >= ?
       AND timestamp_utc < ?
       AND source_root_key IS NOT NULL
     ORDER BY timestamp_utc ASC, source_root_key ASC, conversation_key ASC, id ASC
"""


_ROOTED_CODEX_ACCOUNTING_ENTRIES_SQL = """
    SELECT timestamp_utc, session_id, source_path, source_root_key, model,
           input_tokens, cached_input_tokens, output_tokens,
           reasoning_output_tokens, total_tokens
      FROM codex_session_entries INDEXED BY idx_codex_entries_ts_root_conversation
     WHERE timestamp_utc >= ?
       AND timestamp_utc < ?
     {root_predicate}
     ORDER BY timestamp_utc ASC, source_root_key ASC, conversation_key ASC, id ASC
"""


_CODEX_PROJECT_METADATA_HEALTH_SQL = """
    SELECT
      COUNT(*) AS total_rows,
      COALESCE(SUM(CASE
          WHEN entries.conversation_key IS NULL OR entries.conversation_key = ''
          THEN 1 ELSE 0 END), 0) AS missing_conversation_key_rows,
      COALESCE(SUM(CASE
           WHEN entries.conversation_key IS NOT NULL
           AND entries.conversation_key != ''
           AND threads.conversation_key IS NULL
           AND NOT EXISTS (
             SELECT 1 FROM codex_session_files AS files
             JOIN codex_conversation_threads AS inherited
               ON inherited.source_root_key = entries.source_root_key
              AND inherited.native_thread_id = files.last_native_thread_id
            WHERE files.path = entries.source_path
              AND files.source_root_key = entries.source_root_key
           )
          THEN 1 ELSE 0 END), 0) AS missing_thread_join_rows
      FROM codex_session_entries AS entries
      LEFT JOIN codex_conversation_threads AS threads
        ON threads.conversation_key = entries.conversation_key
       AND threads.source_root_key = entries.source_root_key
     WHERE (? IS NULL OR entries.timestamp_utc >= ?)
       AND (? IS NULL OR entries.timestamp_utc < ?)
"""


_CODEX_PROJECT_METADATA_HEALTH_LEGACY_SQL = """
    SELECT
      COUNT(*) AS total_rows,
      COALESCE(SUM(CASE
          WHEN entries.conversation_key IS NULL OR entries.conversation_key = ''
          THEN 1 ELSE 0 END), 0) AS missing_conversation_key_rows,
      COALESCE(SUM(CASE
          WHEN entries.conversation_key IS NOT NULL
           AND entries.conversation_key != ''
           AND threads.conversation_key IS NULL
          THEN 1 ELSE 0 END), 0) AS missing_thread_join_rows
      FROM codex_session_entries AS entries
      LEFT JOIN codex_conversation_threads AS threads
        ON threads.conversation_key = entries.conversation_key
       AND threads.source_root_key = entries.source_root_key
     WHERE (? IS NULL OR entries.timestamp_utc >= ?)
       AND (? IS NULL OR entries.timestamp_utc < ?)
"""


def _supports_native_file_aliases(cache_conn: sqlite3.Connection) -> bool:
    """Return whether this cache generation can link child files to a root task."""
    try:
        columns = cache_conn.execute("PRAGMA table_info(codex_session_files)").fetchall()
    except sqlite3.Error:
        return False
    return any(len(row) > 1 and row[1] == "last_native_thread_id" for row in columns)


def _cctally():
    return sys.modules["cctally"]


def _parse_timestamp(value: object) -> dt.datetime:
    try:
        timestamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise QualifiedMetadataUnavailable("Codex accounting metadata is unavailable") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise QualifiedMetadataUnavailable("Codex accounting metadata is unavailable")
    return timestamp.astimezone(UTC)


def _metadata_health_bound(value: dt.datetime | None, *, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    # Retain the canonical ``+00:00`` representation written by Codex ingest
    # and consumed by the accounting readers: SQLite TEXT bounds must sort in
    # the same encoding for the [start, end) window to remain exact.
    return value.astimezone(UTC).isoformat()


def load_codex_project_metadata_health(
    *,
    cache_conn: sqlite3.Connection,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> CodexProjectMetadataHealth:
    """Classify retained Codex accounting in one root-qualified SQL read.

    Bounds are both timezone-aware or both omitted; the omitted form serves the
    all-history doctor check.  The output is an exhaustive, mutually-exclusive
    count partition and contains no source or identity data.
    """
    if (start is None) != (end is None):
        raise ValueError("start and end must be supplied together or both omitted")
    bound_start = _metadata_health_bound(start, name="start")
    bound_end = _metadata_health_bound(end, name="end")
    if start is not None and end is not None and start > end:
        raise ValueError("start must not be after end")

    row = cache_conn.execute(
        (_CODEX_PROJECT_METADATA_HEALTH_SQL if _supports_native_file_aliases(cache_conn)
         else _CODEX_PROJECT_METADATA_HEALTH_LEGACY_SQL),
        (bound_start, bound_start, bound_end, bound_end),
    ).fetchone()
    if row is None or len(row) != 3:
        raise RuntimeError("Codex project metadata health query returned no partition")
    try:
        total_rows, missing_key_rows, missing_join_rows = (int(value) for value in row)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Codex project metadata health query returned invalid counts") from exc
    qualified_rows = total_rows - missing_key_rows - missing_join_rows
    values = (total_rows, qualified_rows, missing_key_rows, missing_join_rows)
    if any(value < 0 for value in values) or total_rows != sum(values[1:]):
        raise RuntimeError("Codex project metadata health partition is invalid")
    return CodexProjectMetadataHealth(
        total_rows=total_rows,
        qualified_rows=qualified_rows,
        missing_conversation_key_rows=missing_key_rows,
        missing_thread_join_rows=missing_join_rows,
    )


def load_cached_rooted_codex_accounting_entries(
    start: dt.datetime,
    end: dt.datetime,
    *,
    speed: str,
    cache_conn: sqlite3.Connection,
    source_root_keys: Iterable[str] | None = None,
) -> tuple[RootedCodexAccountingEntry, ...]:
    """Read bounded accounting from one caller-owned cache snapshot only.

    This reader intentionally never syncs, opens or closes a database, reads
    rollouts, resolves projects, or discovers configured Codex roots.  The
    cached root key plus source path are the dashboard's file identity.  A
    caller can additionally constrain the read to roots that established one
    native cycle; root identities remain internal to this adapter.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    root_keys = (
        tuple(sorted({key for key in source_root_keys if isinstance(key, str) and key}))
        if source_root_keys is not None else ()
    )
    if source_root_keys is not None and not root_keys:
        return ()
    root_predicate = (
        "AND source_root_key IN (" + ", ".join("?" for _ in root_keys) + ")"
        if source_root_keys is not None else ""
    )
    try:
        rows = tuple(cache_conn.execute(
            _ROOTED_CODEX_ACCOUNTING_ENTRIES_SQL.format(root_predicate=root_predicate),
            (
                start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(), *root_keys,
            ),
        ))
    except sqlite3.Error as exc:
        raise QualifiedMetadataUnavailable("Codex accounting metadata is unavailable") from exc

    result: list[RootedCodexAccountingEntry] = []
    for row in rows:
        try:
            (
                timestamp_raw, session_id_raw, source_path_raw, source_root_key_raw,
                model_raw, input_raw, cached_input_raw, output_raw, reasoning_raw,
                total_raw,
            ) = row
            source_path = source_path_raw if isinstance(source_path_raw, str) else ""
            source_root_key = source_root_key_raw if isinstance(source_root_key_raw, str) else ""
            if not source_path or not source_root_key:
                raise ValueError("rooted accounting identity is absent")
            model = str(model_raw)
            input_tokens = int(input_raw)
            cached_input_tokens = int(cached_input_raw)
            output_tokens = int(output_raw)
            reasoning_output_tokens = int(reasoning_raw)
            total_tokens = int(total_raw)
            cost_usd = _calculate_codex_entry_cost(
                model,
                input_tokens,
                cached_input_tokens,
                output_tokens,
                reasoning_output_tokens,
                speed=speed,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise QualifiedMetadataUnavailable("Codex accounting metadata is unavailable") from exc
        result.append(RootedCodexAccountingEntry(
            timestamp=_parse_timestamp(timestamp_raw),
            session_id=str(session_id_raw or ""),
            source_path=source_path,
            source_root_key=source_root_key,
            model=model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
        ))
    return tuple(result)


def has_cached_codex_accounting_entries(*, cache_conn: sqlite3.Connection) -> bool:
    """Return whether any retained Codex accounting exists without range filtering.

    This intentionally tiny caller-owned cache read lets a missing native cycle
    fail closed even when the dashboard's visible or budget range is empty.
    It neither resolves roots nor reads rollout files.
    """
    try:
        return cache_conn.execute(
            "SELECT 1 FROM codex_session_entries LIMIT 1"
        ).fetchone() is not None
    except sqlite3.Error as exc:
        raise QualifiedMetadataUnavailable("Codex accounting metadata is unavailable") from exc


def _project_label(value: object) -> str:
    """Render only a basename-like label; absolute paths remain internal."""
    raw = str(value).strip()
    normalized = raw.replace("\\", "/")
    if normalized in {"", "/"}:
        return "(root)" if normalized == "/" else "(unassigned)"
    parts = tuple(part for part in normalized.split("/") if part)
    # A cwd that is exactly a user home directory must never turn the local
    # account name into a public project label.  Keep the generic token too so
    # it remains safe for fixture homes and Windows rollouts.
    if (
        len(parts) == 2 and parts[0] in {"Users", "home"}
    ) or (
        len(parts) == 3 and parts[-2] in {"Users", "home"}
    ):
        return "(home)"
    return parts[-1] if parts else "(unassigned)"


def _git_resolved_key(value: object) -> str | None:
    """Return a non-renderable identity for valid S1 git metadata."""
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict) or not decoded:
        return None
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "git:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_joined_metadata(row: sqlite3.Row) -> tuple[str, str]:
    root_key = row["source_root_key"]
    conversation_key = row["conversation_key"]
    if (
        not isinstance(root_key, str) or not root_key
        or not isinstance(conversation_key, str) or not conversation_key
        or row["joined_conversation_key"] != conversation_key
        or row["joined_source_root_key"] != root_key
    ):
        raise QualifiedMetadataUnavailable("Codex qualified project metadata is unavailable")
    return root_key, conversation_key


def load_qualified_codex_entries(
    start: dt.datetime,
    end: dt.datetime,
    *,
    speed: str,
    sync: bool = True,
    group: str = "git-root",
    cache_conn: sqlite3.Connection | None = None,
) -> tuple[QualifiedCodexEntry, ...]:
    """Load exactly one bounded, root-qualified Codex accounting read.

    The S1 cache is the sole metadata source.  Unlike unqualified Codex
    accounting readers, this adapter deliberately has no direct-rollout fallback:
    without the relational conversation join it cannot safely attribute projects.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")

    if cache_conn is not None and sync:
        raise ValueError("cache_conn requires sync=False")

    c = _cctally()
    owns_conn = cache_conn is None
    if owns_conn:
        try:
            conn = c.open_cache_db()
        except (OSError, sqlite3.Error) as exc:
            raise QualifiedMetadataUnavailable("Codex qualified project metadata is unavailable") from exc
    else:
        conn = cache_conn
    previous_row_factory = conn.row_factory
    try:
        if sync:
            stats = c.sync_codex_cache(conn)
            if stats.lock_contended:
                raise QualifiedMetadataUnavailable("Codex qualified project metadata is unavailable")
        conn.row_factory = sqlite3.Row
        rows = tuple(conn.execute(
            _QUALIFIED_CODEX_ENTRIES_SQL,
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        ))
        inherited_metadata: dict[tuple[str, str], sqlite3.Row] = {}
        if _supports_native_file_aliases(conn):
            for inherited in conn.execute(_INHERITED_CODEX_PROJECT_METADATA_SQL):
                identity = (str(inherited["source_root_key"] or ""), str(inherited["path"] or ""))
                if all(identity):
                    inherited_metadata.setdefault(identity, inherited)
    except sqlite3.Error as exc:
        raise QualifiedMetadataUnavailable("Codex qualified project metadata is unavailable") from exc
    finally:
        if owns_conn:
            conn.close()
        else:
            conn.row_factory = previous_row_factory

    resolver_cache: dict[object, object] = {}
    resolved_by_cwd: dict[str, object] = {}
    resolved_by_git_json: dict[str, str | None] = {}
    result: list[QualifiedCodexEntry] = []
    for row in rows:
        root_key = row["source_root_key"]
        conversation_key = row["conversation_key"]
        inherited = inherited_metadata.get((str(root_key or ""), str(row["source_path"] or "")))
        try:
            root_key, conversation_key = _require_joined_metadata(row)
        except QualifiedMetadataUnavailable:
            if (
                inherited is None
                or not isinstance(root_key, str) or not root_key
                or not isinstance(conversation_key, str) or not conversation_key
            ):
                raise
        cwd = row["cwd"] or (inherited["cwd"] if inherited is not None else None)
        if isinstance(cwd, str) and cwd:
            project = resolved_by_cwd.get(cwd)
            if project is None:
                project = c._resolve_project_key(cwd, group, resolver_cache)
                resolved_by_cwd[cwd] = project
            resolved_key = project.bucket_path
            cwd_label = _project_label(cwd)
            project_label = (
                cwd_label if cwd_label in {"(home)", "(root)"}
                else _project_label(project.display_key)
            )
        else:
            git_json = row["git_json"] or (inherited["git_json"] if inherited is not None else None)
            if isinstance(git_json, str) and git_json not in resolved_by_git_json:
                resolved_by_git_json[git_json] = _git_resolved_key(git_json)
            git_key = resolved_by_git_json.get(git_json) if isinstance(git_json, str) else None
            if git_key is None:
                resolved_key = "(unassigned)"
                project_label = "(unassigned)"
            else:
                resolved_key = git_key
                project_label = "Git project"
        try:
            cost_usd = c._calculate_codex_entry_cost(
                str(row["model"]), int(row["input_tokens"]),
                int(row["cached_input_tokens"]), int(row["output_tokens"]),
                int(row["reasoning_output_tokens"]), speed=speed,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise QualifiedMetadataUnavailable("Codex qualified accounting is unavailable") from exc
        result.append(QualifiedCodexEntry(
            timestamp=_parse_timestamp(row["timestamp_utc"]),
            session_id=str(row["session_id"] or ""),
            source_path=str(row["source_path"] or ""),
            source_root_key=root_key,
            conversation_key=conversation_key,
            project_key=opaque_project_key("codex", root_key, resolved_key),
            project_label=project_label,
            model=str(row["model"]),
            input_tokens=int(row["input_tokens"]),
            cached_input_tokens=int(row["cached_input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            reasoning_output_tokens=int(row["reasoning_output_tokens"]),
            total_tokens=int(row["total_tokens"]),
            cost_usd=cost_usd,
        ))
    return tuple(result)


def load_codex_accounting_entries(
    start: dt.datetime, end: dt.datetime, *, speed: str, sync: bool = True,
    force_direct: bool = False,
) -> tuple[QualifiedCodexEntry, ...]:
    """Load accounting through the canonical cache-first Codex reader.

    Unlike qualified project attribution, accounting remains useful when the
    S1 cache is unavailable or its ingest lock is contended.  The established
    reader owns that cache/direct-JSONL fallback; this adapter only converts its
    native accounting objects to the provider result contract.  It never reads
    rollout metadata for a project identity.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    c = _cctally()
    try:
        entries = (
            c._collect_codex_entries_direct(start, end)
            if force_direct else c.get_codex_entries(start, end, skip_sync=not sync)
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise RuntimeError("Codex accounting is unavailable") from exc

    roots = tuple(_codex_provider_roots())

    def root_key_for_path(source_path: object) -> str:
        path = pathlib.Path(str(source_path)).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        for root in roots:
            try:
                resolved.relative_to(root.provider_root)
            except ValueError:
                continue
            return root.source_root_key
        return "direct:" + hashlib.sha256(str(resolved.parent).encode("utf-8")).hexdigest()[:24]

    result: list[QualifiedCodexEntry] = []
    for entry in entries:
        timestamp = getattr(entry, "timestamp", None)
        if not isinstance(timestamp, dt.datetime):
            continue
        timestamp = _parse_timestamp(timestamp.isoformat())
        if not start <= timestamp < end:
            continue
        source_path = str(getattr(entry, "source_path", ""))
        root_key = root_key_for_path(source_path)
        try:
            cost_usd = c._calculate_codex_entry_cost(
                str(entry.model), int(entry.input_tokens),
                int(entry.cached_input_tokens), int(entry.output_tokens),
                int(entry.reasoning_output_tokens), speed=speed,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("Codex accounting is unavailable") from exc
        native_session = str(getattr(entry, "session_id", "") or "accounting")
        conversation_key = "accounting:" + hashlib.sha256(
            f"{root_key}\0{native_session}\0{source_path}".encode("utf-8")
        ).hexdigest()[:24]
        result.append(QualifiedCodexEntry(
            timestamp=timestamp,
            session_id=native_session,
            source_path=source_path,
            source_root_key=root_key,
            conversation_key=conversation_key,
            project_key=opaque_project_key("codex", root_key, "(unassigned)"),
            project_label="(unassigned)",
            model=str(entry.model),
            input_tokens=int(entry.input_tokens),
            cached_input_tokens=int(entry.cached_input_tokens),
            output_tokens=int(entry.output_tokens),
            reasoning_output_tokens=int(entry.reasoning_output_tokens),
            total_tokens=int(entry.total_tokens),
            cost_usd=cost_usd,
        ))
    return tuple(result)


def _identity_sort_key(identity: object) -> tuple[str, str, str, str, int]:
    return (
        str(identity.source), str(identity.source_root_key),
        str(identity.logical_limit_key), str(identity.observed_slot),
        int(identity.window_minutes),
    )


def select_codex_report_blocks(blocks: Iterable[object], *, weeks: int) -> tuple[object, ...]:
    """Select newest blocks per full native identity, never by display slot."""
    if not isinstance(weeks, int) or isinstance(weeks, bool) or weeks <= 0:
        raise ValueError("weeks must be a positive integer")
    by_identity: dict[object, list[object]] = defaultdict(list)
    for block in blocks:
        by_identity[block.identity].append(block)
    selected: list[object] = []
    for identity in sorted(by_identity, key=_identity_sort_key):
        newest = sorted(
            by_identity[identity],
            key=lambda block: (block.resets_at, block.nominal_start_at),
            reverse=True,
        )[:weeks]
        selected.extend(sorted(newest, key=lambda block: (block.resets_at, block.nominal_start_at)))
    return tuple(selected)


def select_codex_project_blocks(
    blocks: Iterable[object], *, range_start: dt.datetime, range_end: dt.datetime,
    as_of: dt.datetime,
) -> tuple[object, ...]:
    """Keep only native blocks that overlap one requested project interval.

    Project attribution is root- and logical-limit-qualified.  A block is
    applicable only when its observed native interval overlaps the requested
    accounting interval; adjacent or future blocks must not create a fictional
    quota child on a project row.
    """
    start = range_start.astimezone(UTC)
    end = range_end.astimezone(UTC)
    cutoff = as_of.astimezone(UTC)
    selected: list[object] = []
    for block in blocks:
        identity = getattr(block, "identity", None)
        if getattr(identity, "source", None) != "codex":
            continue
        nominal_start = getattr(block, "nominal_start_at", None)
        resets_at = getattr(block, "resets_at", None)
        if not isinstance(nominal_start, dt.datetime) or not isinstance(resets_at, dt.datetime):
            continue
        if nominal_start.tzinfo is None or resets_at.tzinfo is None:
            continue
        block_start = nominal_start.astimezone(UTC)
        block_end = min(cutoff, resets_at.astimezone(UTC))
        if block_start < end and block_end > start:
            selected.append(block)
    return tuple(sorted(
        selected,
        key=lambda block: (
            _identity_sort_key(block.identity), block.resets_at, block.nominal_start_at,
        ),
    ))


def _arg_datetime(args: object, primary: str, fallback: str) -> dt.datetime:
    value = getattr(args, primary, getattr(args, fallback, None))
    if not isinstance(value, dt.datetime):
        raise ValueError(f"{primary} must be a resolved datetime")
    return value


def _source_config_and_tz(args: object) -> tuple[dict, object]:
    """Resolve the display configuration once for a source-aware command."""
    c = _cctally()
    config = c._load_claude_config_for_args(args)
    c._bridge_z_into_tz(args, config)
    tz = c.resolve_display_tz(args, config)
    args._resolved_tz = tz
    return config, tz


def _source_date_range(args: object, *, config: dict, tz: object) -> tuple[dt.datetime, dt.datetime]:
    """Resolve shared project date flags to the source path's half-open range."""
    c = _cctally()
    parsed = c._parse_cli_date_range(args, now_utc=_command_as_of())
    if isinstance(parsed, int):
        raise ValueError("invalid date range")
    start, end = parsed
    if getattr(args, "until", None) and not any(
        marker in args.until for marker in ("T", "+", "Z")
    ):
        # The historical helper supplies the final microsecond of a civil
        # date. Provider reads are half-open, so advance to the next midnight.
        end += dt.timedelta(microseconds=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def _resolve_source_project_range(args: object) -> tuple[dt.datetime, dt.datetime]:
    if isinstance(getattr(args, "range_start", None), dt.datetime):
        return _arg_datetime(args, "range_start", "start"), _arg_datetime(args, "range_end", "end")
    c = _cctally()
    config, tz = _source_config_and_tz(args)
    weeks = getattr(args, "weeks", None)
    if weeks is not None and weeks < 1:
        raise ValueError("--weeks must be >= 1")
    if weeks is not None and (getattr(args, "since", None) or getattr(args, "until", None)):
        raise ValueError("--weeks cannot be combined with --since/--until")
    if getattr(args, "since", None) or getattr(args, "until", None):
        start, end = _source_date_range(args, config=config, tz=tz)
    else:
        now = _command_as_of()
        local_now = now.astimezone(tz) if tz is not None else now.astimezone()
        week_start, _ = compute_week_bounds(
            local_now, get_week_start_name(config),
        )
        start = dt.datetime.combine(week_start, dt.time.min, tzinfo=local_now.tzinfo)
        if weeks is not None:
            start -= dt.timedelta(days=7 * (weeks - 1))
        end = now
    args.range_start, args.range_end = start.astimezone(UTC), end.astimezone(UTC)
    args.as_of = end.astimezone(UTC)
    return args.range_start, args.range_end


def _resolve_source_range_cost_range(args: object) -> tuple[dt.datetime, dt.datetime]:
    if isinstance(getattr(args, "start", None), dt.datetime):
        return _arg_datetime(args, "start", "range_start"), _arg_datetime(args, "end", "range_end")
    c = _cctally()
    start = parse_iso_datetime(args.start, "--start")
    end = parse_iso_datetime(args.end, "--end") if getattr(args, "end", None) else _command_as_of()
    if end < start:
        raise ValueError("--end must be after --start")
    args.start, args.end = start.astimezone(UTC), end.astimezone(UTC)
    return args.start, args.end


def _resolve_source_cache_range(args: object) -> tuple[dt.datetime, dt.datetime]:
    if isinstance(getattr(args, "start", None), dt.datetime):
        return _arg_datetime(args, "start", "range_start"), _arg_datetime(args, "end", "range_end")
    c = _cctally()
    _config, tz = _source_config_and_tz(args)
    start, end = c._resolve_cache_report_window(
        args, now_utc=_command_as_of(), tz_name=(tz.key if tz is not None else None),
    )
    if getattr(args, "until", None) and not any(
        marker in args.until for marker in ("T", "+", "Z")
    ):
        end += dt.timedelta(microseconds=1)
    args.start, args.end = start.astimezone(UTC), end.astimezone(UTC)
    return args.start, args.end


def _resolve_source_diff_windows(args: object) -> tuple[AnalyticsWindow, AnalyticsWindow]:
    if isinstance(getattr(args, "window_a", None), AnalyticsWindow):
        return args.window_a, args.window_b
    c = _cctally()
    config, tz = _source_config_and_tz(args)
    now = _command_as_of()
    tz_name = tz.key if tz is not None else c._local_tz_name()

    def resolve(token: str) -> AnalyticsWindow:
        match = c._DIFF_NW_AGO_RE.match(token)
        if token in {"this-week", "last-week"} or match:
            local_now = now.astimezone(tz) if tz is not None else now.astimezone()
            current_start, _ = compute_week_bounds(
                local_now, get_week_start_name(config),
            )
            start = dt.datetime.combine(current_start, dt.time.min, tzinfo=local_now.tzinfo)
            if token == "this-week":
                end = now
            else:
                weeks_back = 1 if token == "last-week" else int(match.group(1))
                start -= dt.timedelta(days=7 * weeks_back)
                end = start + dt.timedelta(days=7)
            return AnalyticsWindow(token, "week", start, end)
        parsed = c._parse_diff_window(
            token, now_utc=now, anchor_resets_at=None, anchor_week_start=None,
            tz_name=tz_name,
        )
        return AnalyticsWindow(parsed.label, parsed.kind, parsed.start_utc, parsed.end_utc)

    args.window_a, args.window_b = resolve(args.a), resolve(args.b)
    return args.window_a, args.window_b


def _resolve_source_report_as_of(args: object) -> dt.datetime:
    as_of = getattr(args, "as_of", None)
    if isinstance(as_of, dt.datetime):
        return as_of
    args.as_of = _command_as_of().astimezone(UTC)
    return args.as_of


def _source_entries(
    args: object, start: dt.datetime, end: dt.datetime, *, qualified: bool,
    inclusive_end: bool = False, group: str = "git-root", sync: bool | None = None,
    force_direct: bool = False,
) -> tuple[QualifiedCodexEntry, ...]:
    provided = getattr(args, "source_entries", None)
    if provided is not None:
        values = tuple(provided)
        return assign_collision_safe_project_labels(values) if qualified else values
    speed = _cctally()._resolve_codex_speed(str(getattr(args, "speed", "auto")))
    if sync is None:
        sync = bool(getattr(args, "_source_analytics_sync", not bool(getattr(args, "offline", False))))
    query_end = end + dt.timedelta(microseconds=1) if inclusive_end else end
    if qualified:
        values = load_qualified_codex_entries(
            start, query_end, speed=speed, sync=sync, group=group,
        )
        return assign_collision_safe_project_labels(values)
    return load_codex_accounting_entries(
        start, query_end, speed=speed, sync=sync, force_direct=force_direct,
    )


def _project_selectors(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw = value if isinstance(value, (list, tuple)) else (value,)
    selectors: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise SourceUsageError("--project must be a non-empty project key or display label")
        selectors.append(item)
    return tuple(selectors)


def validate_source_project_selectors(value: object) -> None:
    """Reject malformed opaque keys before a source cache read occurs."""
    for selector in _project_selectors(value):
        if selector.startswith("project:") and not _OPAQUE_PROJECT_KEY_RE.fullmatch(selector):
            raise SourceUsageError(
                "invalid opaque project key; expected project:<24 lowercase hex characters>"
            )


def _filter_project_entries(
    entries: Iterable[QualifiedCodexEntry], value: object,
) -> tuple[QualifiedCodexEntry, ...]:
    """Apply exact opaque-key or unique display-label project selectors."""
    selectors = _project_selectors(value)
    values = tuple(entries)
    if not selectors:
        return values
    validate_source_project_selectors(selectors)
    by_emitted_label: dict[str, set[str]] = defaultdict(set)
    by_raw_label: dict[str, set[str]] = defaultdict(set)
    keys = {entry.project_key for entry in values}
    for entry in values:
        by_emitted_label[emitted_project_label(entry)].add(entry.project_key)
        by_raw_label[entry.project_label].add(entry.project_key)
    selected: set[str] = set()
    for selector in selectors:
        if selector.startswith("project:"):
            if selector in keys:
                selected.add(selector)
            continue
        # Exact emitted collision-safe labels win over raw labels so each
        # label JSON, terminal, and share expose round-trips as a selector.
        matches = by_emitted_label.get(selector)
        if matches is None:
            matches = by_raw_label.get(selector, set())
        if len(matches) > 1:
            raise SourceUsageError(
                f"--project display label {selector!r} is ambiguous; use an exact projectKey"
            )
        selected.update(matches)
    return tuple(entry for entry in values if entry.project_key in selected)


def _filter_model_entries(
    entries: Iterable[QualifiedCodexEntry], value: object,
) -> tuple[QualifiedCodexEntry, ...]:
    patterns = value if isinstance(value, (list, tuple)) else (() if value is None else (value,))
    normalized = tuple(str(pattern).lower() for pattern in patterns if str(pattern))
    if not normalized:
        return tuple(entries)
    return tuple(
        entry for entry in entries
        if any(pattern in entry.model.lower() for pattern in normalized)
    )


_CLAUDE_DIFF_SECTIONS = frozenset({"overall", "models", "projects", "cache"})
_CODEX_DIFF_SECTIONS = frozenset({"overall", "models", "projects", "token-reuse"})


def _diff_only_for_provider(value: object, provider: str) -> str | None:
    """Keep each all-source diff leg inside its own section vocabulary."""
    if value is None:
        return None
    selected = [item.strip() for item in str(value).split(",") if item.strip()]
    supported = _CLAUDE_DIFF_SECTIONS if provider == "claude" else _CODEX_DIFF_SECTIONS
    return ",".join(item for item in selected if item in supported)


def _validate_source_diff_controls(
    args: object, window_a: AnalyticsWindow, window_b: AnalyticsWindow,
) -> str:
    """Reject invalid source-aware diff controls before either provider starts."""
    source = getattr(args, "source", "codex")
    selected = [item.strip() for item in str(getattr(args, "only", "")).split(",") if item.strip()]
    if getattr(args, "only", None) is not None:
        if not selected:
            raise SourceUsageError("diff: --only specified no sections")
        supported = _CODEX_DIFF_SECTIONS if source == "codex" else (_CLAUDE_DIFF_SECTIONS | _CODEX_DIFF_SECTIONS)
        unknown = [item for item in selected if item not in supported]
        if unknown:
            raise SourceUsageError(
                "diff: --only contains unknown section(s): " + ", ".join(unknown)
            )
    for extra in (item.strip() for item in str(getattr(args, "with_extra", "") or "").split(",")):
        if extra in {"trend", "time"}:
            raise RuntimeError(
                f"diff: --with {extra} is not yet implemented (deferred to v1.1)"
            )
    return resolve_codex_diff_normalization(
        window_a, window_b, allow_mismatch=bool(getattr(args, "allow_mismatch", False)),
    )


def _iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


_CLAUDE_COMMANDS = {
    "project": "_cmd_project_claude",
    "diff": "_cmd_diff_claude",
    "range-cost": "_cmd_range_cost_claude",
    "cache-report": "_cmd_cache_report_claude",
    "report": "_cmd_report_claude",
}


def _claude_result_status(command: str, payload: dict[str, object]) -> str:
    """Classify an established Claude JSON payload without reshaping it."""
    if command == "project":
        return "ok" if payload.get("projects") else "empty"
    if command == "range-cost":
        return "ok" if int(payload.get("matchedEntries", 0)) else "empty"
    if command == "cache-report":
        rows = payload.get("sessions", payload.get("days", ()))
        return "ok" if rows else "empty"
    if command == "report":
        return "ok" if payload.get("trend") else "empty"
    if command == "diff":
        for section in payload.get("sections", ()):
            if isinstance(section, dict) and section.get("name") == "overall":
                rows = section.get("rows", ())
                if not rows:
                    return "empty"
                row = rows[0] if isinstance(rows, list) else None
                if not isinstance(row, dict):
                    return "empty"
                for side in (row.get("a"), row.get("b")):
                    if isinstance(side, dict) and any(
                        side.get(name, 0) for name in (
                            "cost_usd", "tokens_input", "tokens_output",
                            "tokens_cache_read", "tokens_cache_write",
                        )
                    ):
                        return "ok"
                return "empty"
        return "empty"
    raise ValueError(f"unknown Claude analytics command: {command}")


def _run_claude_json_adapter(args: object, command: str) -> SourceResult[dict[str, object] | None]:
    """Invoke an established Claude handler through its structured result seam.

    The handlers retain their ordinary rendering paths.  This adapter uses the
    opt-in sink solely for the all-source composition path, so it never captures
    or reparses terminal output and the provider block remains exact legacy JSON.
    """
    c = _cctally()
    handler_name = _CLAUDE_COMMANDS[command]
    handler = getattr(c, handler_name)
    claude_args = copy(args)
    claude_args.source = "claude"
    claude_args.format = None
    claude_args.json = True
    # This is an internal structured-result invocation, not a destination
    # request.  Clear the public share flags before the legacy handler's
    # defense-in-depth validation so it observes a normal JSON invocation.
    claude_args.output = None
    claude_args.copy = False
    claude_args.open_after_write = False
    # The source-aware parser owns ``--format`` but not every legacy share
    # presentation default. Give the structured invocation the same default
    # namespace that an ordinary legacy JSON parse would have.
    for name, value in (
        ("theme", "light"),
        ("no_branding", False),
        ("reveal_projects", False),
    ):
        if not hasattr(claude_args, name):
            setattr(claude_args, name, value)
    if command == "range-cost":
        # The shared Codex resolver stores parsed bounds on the original
        # namespace; the established Claude handler intentionally owns its
        # own ISO parsing, so hand it back the exact wire spelling.
        if isinstance(getattr(claude_args, "start", None), dt.datetime):
            claude_args.start = _iso_z(claude_args.start)
        if isinstance(getattr(claude_args, "end", None), dt.datetime):
            claude_args.end = _iso_z(claude_args.end)
        # The all-source adapter needs the complete legacy JSON object to
        # compose compatible physical cost.  The public combined path renders
        # its total-only scalar after both provider legs have been read.
        claude_args.total_only = False
    if command == "diff":
        claude_args.emit_json = True
    captured: list[dict[str, object]] = []
    claude_args._source_result_sink = captured.append
    exit_code = handler(claude_args)
    if exit_code != 0 or len(captured) != 1:
        return SourceResult("claude", "unavailable", None)
    payload = captured[0]
    return SourceResult("claude", _claude_result_status(command, payload), payload)


def _source_block_wire(result: SourceResult, *, diff: bool) -> dict[str, object]:
    """Keep a provider block's native JSON exactly intact in an all result."""
    if result.source == "claude":
        return {
            "source": "claude",
            "status": result.status,
            "data": result.data,
            "warnings": [
                {"code": warning.code, "message": warning.message}
                for warning in result.warnings
            ],
        }
    direct = source_result_wire(result, diff=diff)
    return {
        "source": direct["source"],
        "status": direct["status"],
        "data": direct["data"],
        "warnings": direct["warnings"],
    }


def _legacy_claude_totals(payload: object) -> tuple[float, int]:
    """Read compatible accounting once from an exact legacy JSON payload."""
    if not isinstance(payload, dict):
        return 0.0, 0
    totals = payload.get("totals")
    if isinstance(totals, dict):
        cost = totals.get("costUsd", totals.get("cost", 0.0))
        tokens = totals.get("totalTokens", 0)
        if "totalTokens" not in totals and isinstance(payload.get("projects"), list):
            tokens = sum(
                int(row.get("inputTokens", 0)) + int(row.get("outputTokens", 0))
                + int(row.get("cacheWriteTokens", 0)) + int(row.get("cacheReadTokens", 0))
                for row in payload["projects"] if isinstance(row, dict)
            )
        return float(cost), int(tokens)
    if "totalCostUSD" in payload:
        return (
            float(payload.get("totalCostUSD", 0.0)),
            sum(int(row.get("totalTokens", 0)) for row in payload.get("modelBreakdowns", ()) if isinstance(row, dict)),
        )
    return 0.0, 0


def _codex_totals(result: SourceResult) -> tuple[float, int]:
    """Read one compatible physical Codex total from its native wire shape."""
    direct = source_result_wire(result)
    data = direct.get("data")
    if not isinstance(data, dict):
        return 0.0, 0
    totals = data.get("totals")
    if not isinstance(totals, dict):
        return 0.0, 0
    return float(totals.get("costUsd", 0.0)), int(totals.get("totalTokens", 0))


def _legacy_diff_totals(payload: object) -> tuple[tuple[float, int], tuple[float, int]]:
    """Extract the two physical Claude totals from the frozen diff payload."""
    if not isinstance(payload, dict):
        return (0.0, 0), (0.0, 0)
    for section in payload.get("sections", ()):
        if not isinstance(section, dict) or section.get("name") != "overall":
            continue
        rows = section.get("rows")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            break
        row = rows[0]

        def side(name: str) -> tuple[float, int]:
            values = row.get(name)
            if not isinstance(values, dict):
                return 0.0, 0
            return (
                float(values.get("cost_usd", 0.0)),
                sum(int(values.get(token, 0)) for token in (
                    "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
                )),
            )

        return side("a"), side("b")
    return (0.0, 0), (0.0, 0)


def _codex_diff_totals(result: SourceResult) -> tuple[tuple[float, int], tuple[float, int]]:
    direct = source_result_wire(result, diff=True)
    data = direct.get("data")
    combined = data.get("combined") if isinstance(data, dict) else None
    if not isinstance(combined, dict):
        return (0.0, 0), (0.0, 0)

    def side(name: str) -> tuple[float, int]:
        costs = combined.get("cost_usd")
        tokens = combined.get("total_tokens")
        if not isinstance(costs, dict) or not isinstance(tokens, dict):
            return 0.0, 0
        return float(costs.get(name, 0.0)), int(tokens.get(name, 0))

    return side("a"), side("b")


def _all_source_wire(
    claude: SourceResult, codex: SourceResult, *, diff: bool, report: bool,
) -> dict[str, object]:
    """Compose exact provider payloads and only compatible physical totals."""
    sources = [
        _source_block_wire(claude, diff=diff),
        _source_block_wire(codex, diff=diff),
    ]
    if diff:
        result: dict[str, object] = {"schema_version": 1, "source": "all"}
        if not report:
            (ca, ta), (cb, tb) = _legacy_diff_totals(claude.data)
            (xa, xa_tokens), (xb, xb_tokens) = _codex_diff_totals(codex)
            result["combined"] = {
                "cost_usd": {"a": ca + xa, "b": cb + xb, "delta": (cb + xb) - (ca + xa)},
                "total_tokens": {"a": ta + xa_tokens, "b": tb + xb_tokens, "delta": (tb + xb_tokens) - (ta + xa_tokens)},
            }
        result["sources"] = sources
        result["warnings"] = []
        return result
    result = {"schemaVersion": 1, "source": "all"}
    if not report:
        claude_cost, claude_tokens = _legacy_claude_totals(claude.data)
        codex_cost, codex_tokens = _codex_totals(codex)
        result["combined"] = {
            "costUsd": claude_cost + codex_cost,
            "totalTokens": claude_tokens + codex_tokens,
        }
    result["sources"] = sources
    result["warnings"] = []
    return result


_TERMINAL_TITLES = {
    "project": "Project Report",
    "diff": "Diff Report",
    "range-cost": "Range Cost Report",
    "cache-report": "Token Reuse Report",
    "report": "Dollars-per-Percent Report",
}


def _share_datetime(value: object, fallback: dt.datetime) -> dt.datetime:
    """Return an aware UTC instant without accepting untrusted display strings."""
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    return fallback


def _source_share_period(command: str, result: SourceResult) -> tuple[dt.datetime, dt.datetime]:
    """Derive a deterministic bounded share period from public result metadata."""
    fallback = _command_as_of().astimezone(UTC)
    data = result.data
    start = getattr(data, "range_start", getattr(data, "start", None))
    end = getattr(data, "range_end", getattr(data, "end", None))
    if command == "report":
        end = getattr(data, "as_of", end)
    if command == "diff":
        windows = getattr(data, "windows", ())
        if len(windows) == 2:
            start = min(window.window.start_at for window in windows)
            end = max(window.window.end_at for window in windows)
    if isinstance(data, dict):
        start = data.get("rangeStart", data.get("rangeStartIso", data.get("start", start)))
        end = data.get("rangeEnd", data.get("rangeEndIso", data.get("end", end)))
        if command == "diff":
            windows = data.get("windows")
            if isinstance(windows, dict):
                values = tuple(value for value in windows.values() if isinstance(value, dict))
                starts = tuple(value.get("start_at") for value in values)
                ends = tuple(value.get("end_at") for value in values)
                parsed_starts = tuple(_share_datetime(value, fallback) for value in starts)
                parsed_ends = tuple(_share_datetime(value, fallback) for value in ends)
                if parsed_starts:
                    start = min(parsed_starts)
                if parsed_ends:
                    end = max(parsed_ends)
        if command == "report" and not start:
            trend = data.get("trend")
            if isinstance(trend, list) and trend and isinstance(trend[0], dict):
                start = trend[0].get("weekStartDate", start)
                last = trend[-1] if isinstance(trend[-1], dict) else trend[0]
                end = last.get("weekStartDate", end)
    end_at = _share_datetime(end, fallback)
    start_at = _share_datetime(start, end_at)
    return (start_at, end_at if end_at >= start_at else start_at)


def _share_float(value: object, default: float = 0.0) -> float:
    """Read a legacy JSON number without letting malformed optional fields fail share."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _share_int(value: object, default: int = 0) -> int:
    """Read a legacy JSON integer without treating bools as token counts."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _claude_project_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt the established ``project`` JSON payload without Codex field guesses."""
    columns = (
        lib.ColumnSpec(key="project", label="Project", kind="project"),
        lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
    )
    rows = []
    for payload_row in data.get("projects", ()):
        if not isinstance(payload_row, dict):
            continue
        cost = _share_float(payload_row.get("costUsd"))
        token_count = sum(_share_int(payload_row.get(name)) for name in (
            "inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens",
        ))
        rows.append(lib.Row(cells={
            "project": lib.ProjectCell(
                str(payload_row.get("displayKey", "(unknown)")), rank_cost=cost,
            ),
            "cost": lib.MoneyCell(cost),
            "tokens": lib.TextCell(f"{token_count:,}"),
        }))
    total_cost, total_tokens = _legacy_claude_totals(data)
    totals = (
        lib.Totalled(label="Total", value=f"${total_cost:,.2f}"),
        lib.Totalled(label="Tokens", value=f"{total_tokens:,}"),
    )
    return columns, tuple(rows), totals


def _diff_metric_tokens(metric: object) -> int | None:
    """Read the source-native total-token field or its frozen Claude inputs."""
    if not isinstance(metric, dict):
        return None
    if "total_tokens" in metric:
        return _share_int(metric.get("total_tokens"))
    token_keys = (
        "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
    )
    if not any(key in metric for key in token_keys):
        return None
    return sum(_share_int(metric.get(key)) for key in token_keys)


def _diff_sections(data: dict[str, object]):
    """Yield legacy and Codex diff sections without flattening provider shape."""
    for section in data.get("sections", ()):
        if not isinstance(section, dict):
            continue
        key = str(section.get("key", section.get("name", "section")))
        label = str(section.get("label", key.replace("-", " ").title()))
        section_data = section.get("data")
        rows = (
            section_data.get("rows", ())
            if isinstance(section_data, dict) else section.get("rows", ())
        )
        yield key, label, str(section.get("status", "ok")), rows


def _diff_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt one native diff payload with row identities and full comparison metrics."""
    columns = (
        lib.ColumnSpec(key="section", label="Section"),
        lib.ColumnSpec(key="item", label="Item"),
        lib.ColumnSpec(key="status", label="Status"),
        lib.ColumnSpec(key="a_cost", label="A $ Cost", align="right"),
        lib.ColumnSpec(key="b_cost", label="B $ Cost", align="right"),
        lib.ColumnSpec(key="delta_cost", label="Δ $ Cost", align="right"),
        lib.ColumnSpec(key="a_tokens", label="A Tokens", align="right"),
        lib.ColumnSpec(key="b_tokens", label="B Tokens", align="right"),
        lib.ColumnSpec(key="delta_tokens", label="Δ Tokens", align="right"),
    )
    rows = []
    for section_key, section_label, section_status, payload_rows in _diff_sections(data):
        for payload_row in payload_rows:
            if not isinstance(payload_row, dict):
                continue
            side_a = payload_row.get("a") if isinstance(payload_row.get("a"), dict) else {}
            side_b = payload_row.get("b") if isinstance(payload_row.get("b"), dict) else {}
            delta = payload_row.get("delta") if isinstance(payload_row.get("delta"), dict) else {}
            label = str(payload_row.get("label", section_label))
            status = str(payload_row.get("status", section_status))
            if section_key == "projects":
                item = lib.ProjectCell(
                    label,
                    rank_cost=_share_float(side_b.get("cost_usd")),
                    identity=str(payload_row.get("key", payload_row.get("projectKey", label))),
                )
            else:
                item = lib.TextCell(label)
            a_tokens = _diff_metric_tokens(side_a)
            b_tokens = _diff_metric_tokens(side_b)
            delta_tokens = _diff_metric_tokens(delta)
            rows.append(lib.Row(cells={
                "section": lib.TextCell(section_label),
                "item": item,
                "status": lib.TextCell(status),
                "a_cost": lib.MoneyCell(_share_float(side_a.get("cost_usd"))),
                "b_cost": lib.MoneyCell(_share_float(side_b.get("cost_usd"))),
                "delta_cost": lib.DeltaCell(_share_float(delta.get("cost_usd")), "$"),
                "a_tokens": lib.TextCell("—" if a_tokens is None else f"{a_tokens:,}"),
                "b_tokens": lib.TextCell("—" if b_tokens is None else f"{b_tokens:,}"),
                "delta_tokens": lib.TextCell(
                    "—" if delta_tokens is None else f"{delta_tokens:+,}"
                ),
            }))
    if isinstance(data.get("combined"), dict):
        combined = data["combined"]
        costs = combined.get("cost_usd") if isinstance(combined.get("cost_usd"), dict) else {}
        tokens = combined.get("total_tokens") if isinstance(combined.get("total_tokens"), dict) else {}
        cost_a, cost_b = _share_float(costs.get("a")), _share_float(costs.get("b"))
        tokens_a, tokens_b = _share_int(tokens.get("a")), _share_int(tokens.get("b"))
    else:
        (cost_a, tokens_a), (cost_b, tokens_b) = _legacy_diff_totals(data)
    delta_cost = cost_b - cost_a
    totals = (
        lib.Totalled(label="A total", value=f"${cost_a:,.2f}"),
        lib.Totalled(label="B total", value=f"${cost_b:,.2f}"),
        lib.Totalled(
            label="Δ total",
            value=f"{'+' if delta_cost >= 0 else '-'}${abs(delta_cost):,.2f}",
        ),
        lib.Totalled(label="A tokens", value=f"{tokens_a:,}"),
        lib.Totalled(label="B tokens", value=f"{tokens_b:,}"),
        lib.Totalled(label="Δ tokens", value=f"{(tokens_b - tokens_a):+,}"),
    )
    return columns, tuple(rows), totals


def _claude_diff_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt frozen legacy diff sections with their A/B/delta quantities intact."""
    return _diff_share_rows(data, lib)


def _claude_range_cost_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt legacy ``modelBreakdowns`` and ``totalCostUSD`` exactly once."""
    columns = (
        lib.ColumnSpec(key="model", label="Model"),
        lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
        lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
    )
    rows = []
    for payload_row in data.get("modelBreakdowns", ()):
        if not isinstance(payload_row, dict):
            continue
        rows.append(lib.Row(cells={
            "model": lib.TextCell(str(payload_row.get("model", "(unknown)"))),
            "tokens": lib.TextCell(f"{_share_int(payload_row.get('totalTokens')):,}"),
            "cost": lib.MoneyCell(_share_float(payload_row.get("costUSD"))),
        }))
    if not rows and _share_int(data.get("matchedEntries")):
        cost, tokens = _legacy_claude_totals(data)
        rows.append(lib.Row(cells={
            "model": lib.TextCell("Total"),
            "tokens": lib.TextCell(f"{tokens:,}"),
            "cost": lib.MoneyCell(cost),
        }))
    cost, tokens = _legacy_claude_totals(data)
    totals = (
        lib.Totalled(label="Total", value=f"${cost:,.2f}"),
        lib.Totalled(label="Tokens", value=f"{tokens:,}"),
    )
    return columns, tuple(rows), totals


def _claude_cache_report_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt legacy cache-day/session rows without exposing session/path fields."""
    columns = (
        lib.ColumnSpec(key="label", label="Group"),
        lib.ColumnSpec(key="reuse", label="Cached Input", align="right"),
        lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
    )
    payload_rows = data.get("days")
    if not isinstance(payload_rows, (list, tuple)):
        payload_rows = data.get("sessions")
    rows = []
    for index, payload_row in enumerate(payload_rows if isinstance(payload_rows, (list, tuple)) else ()):
        if not isinstance(payload_row, dict):
            continue
        # Session ids and paths are intentionally never suitable share labels.
        label = payload_row.get("date") or f"Session {index + 1}"
        cached_percent = payload_row.get("cacheHitPercent")
        rows.append(lib.Row(cells={
            "label": lib.TextCell(str(label)),
            "reuse": lib.TextCell(
                "—" if cached_percent is None else f"{_share_float(cached_percent):.1f}%"
            ),
            "cost": lib.MoneyCell(_share_float(payload_row.get("cost"))),
        }))
    total_cost, total_tokens = _legacy_claude_totals(data)
    totals = (
        lib.Totalled(label="Total", value=f"${total_cost:,.2f}"),
        lib.Totalled(label="Tokens", value=f"{total_tokens:,}"),
    )
    return columns, tuple(rows), totals


def _claude_report_share_rows(data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Adapt the established weekly trend rather than inventing quota-series rows."""
    columns = (
        lib.ColumnSpec(key="week", label="Week"),
        lib.ColumnSpec(key="used", label="% Used", align="right"),
        lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        lib.ColumnSpec(key="per_percent", label="$/1%", align="right"),
    )
    rows = []
    for payload_row in data.get("trend", ()):
        if not isinstance(payload_row, dict):
            continue
        date_value = str(payload_row.get("weekStartDate", "—"))
        try:
            display_week = dt.date.fromisoformat(date_value).strftime("%b %d")
        except ValueError:
            display_week = date_value
        used = payload_row.get("weeklyPercent")
        per_percent = payload_row.get("dollarsPerPercent")
        rows.append(lib.Row(cells={
            "week": lib.TextCell(display_week),
            "used": lib.TextCell("—" if used is None else f"{_share_float(used):.1f}%"),
            "cost": lib.MoneyCell(_share_float(payload_row.get("weeklyCostUSD"))),
            "per_percent": lib.TextCell(
                "—" if per_percent is None else f"${_share_float(per_percent):.2f}"
            ),
        }))
    total_cost = sum(
        _share_float(row.get("weeklyCostUSD")) for row in data.get("trend", ())
        if isinstance(row, dict)
    )
    return columns, tuple(rows), (lib.Totalled(label="Trend total", value=f"${total_cost:,.2f}"),)


def _claude_legacy_share_rows(command: str, data: dict[str, object], lib) -> tuple[tuple, tuple, tuple]:
    """Route each legacy payload through its explicit, source-native adapter."""
    adapters = {
        "project": _claude_project_share_rows,
        "diff": _claude_diff_share_rows,
        "range-cost": _claude_range_cost_share_rows,
        "cache-report": _claude_cache_report_share_rows,
        "report": _claude_report_share_rows,
    }
    return adapters[command](data, lib)


def _source_share_rows(command: str, result: SourceResult, lib) -> tuple[tuple, tuple, tuple]:
    """Build a compact, source-safe table from the public provider result shape."""
    if result.status == "unavailable" or result.data is None:
        return (), (), ()
    if result.source == "codex":
        wire = source_result_wire(result, diff=(command == "diff"))
        data = wire.get("data")
    else:
        data = result.data
    if not isinstance(data, dict):
        return (), (), ()

    if command == "project":
        columns = (
            lib.ColumnSpec(key="project", label="Project", kind="project"),
            lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
            lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
        )
        rows = []
        for row in data.get("projects", ()):
            if not isinstance(row, dict):
                continue
            label = str(row.get("displayLabel", row.get("project", "(unknown)")))
            identity = row.get("projectKey") if result.source == "codex" else None
            tokens = row.get("tokens", {})
            token_count = tokens.get("totalTokens", 0) if isinstance(tokens, dict) else 0
            rows.append(lib.Row(cells={
                "project": lib.ProjectCell(label, rank_cost=float(row.get("costUsd", 0.0)), identity=identity),
                "cost": lib.MoneyCell(float(row.get("costUsd", 0.0))),
                "tokens": lib.TextCell(f"{int(token_count):,}"),
            }))
        return columns, tuple(rows), ()

    if command == "range-cost":
        columns = (
            lib.ColumnSpec(key="model", label="Model"),
            lib.ColumnSpec(key="tokens", label="Tokens", align="right"),
            lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        )
        model_rows = tuple(row for row in data.get("models", ()) if isinstance(row, dict))
        if model_rows:
            rows = tuple(
                lib.Row(cells={
                    "model": lib.TextCell(str(row.get("model", "(unknown)"))),
                    "tokens": lib.TextCell(f"{int(row.get('totalTokens', 0)):,}"),
                    "cost": lib.MoneyCell(float(row.get("costUsd", 0.0))),
                })
                for row in model_rows
            )
        else:
            totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
            rows = (lib.Row(cells={
                "model": lib.TextCell("Total"),
                "tokens": lib.TextCell(f"{int(totals.get('totalTokens', 0)):,}"),
                "cost": lib.MoneyCell(float(totals.get("costUsd", 0.0))),
            }),)
        return columns, rows, ()

    if command == "cache-report":
        columns = (
            lib.ColumnSpec(key="label", label="Group"),
            lib.ColumnSpec(key="reuse", label="Cached Input", align="right"),
            lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
        )
        rows = []
        for section in data.get("sections", ()):
            if not isinstance(section, dict):
                continue
            for row in (section.get("data") or {}).get("rows", ()):
                if not isinstance(row, dict):
                    continue
                rows.append(lib.Row(cells={
                    "label": lib.TextCell(str(row.get("label", "Codex"))),
                    "reuse": lib.TextCell(
                        "—" if row.get("cachedInputPercent") is None
                        else f"{float(row['cachedInputPercent']):.1f}%"
                    ),
                    "cost": lib.MoneyCell(float(row.get("costUsd", 0.0))),
                }))
        return columns, tuple(rows), ()

    if command == "diff":
        return _diff_share_rows(data, lib)

    columns = (
        lib.ColumnSpec(key="series", label="Quota Series"),
        lib.ColumnSpec(key="used", label="% Used", align="right"),
        lib.ColumnSpec(key="cost", label="$ Cost", align="right"),
    )
    rows = []
    for section in data.get("sections", ()):
        if not isinstance(section, dict):
            continue
        for series in (section.get("data") or {}).get("series", ()):
            if not isinstance(series, dict):
                continue
            for row in series.get("rows", ()):
                if not isinstance(row, dict):
                    continue
                slot = str(series.get("slot", "quota"))
                window_minutes = series.get("windowMinutes")
                if isinstance(window_minutes, int) and window_minutes > 0:
                    slot = f"{slot} · {window_minutes}m"
                rows.append(lib.Row(cells={
                    "series": lib.TextCell(slot),
                    "used": lib.TextCell("—" if row.get("usedPercent") is None else f"{float(row['usedPercent']):.1f}%"),
                    "cost": lib.MoneyCell(float(row.get("costUsd", 0.0))),
                }))
    return columns, tuple(rows), ()


def build_source_share_snapshot(
    command: str, source_result: SourceResult, *, reveal_projects: bool,
) -> "ShareSnapshot":
    """Return one source-bearing share snapshot without exposing local roots.

    Callers compose two returned snapshots for an all-source artifact instead
    of inventing a synthetic ``source='all'`` snapshot.
    """
    if command not in _TERMINAL_TITLES:
        raise ValueError(f"unknown source share command: {command}")
    if source_result.source not in {"claude", "codex"}:
        raise ValueError("source share snapshots require Claude or Codex")
    c = _cctally()
    lib = c._share_load_lib()
    start, end = _source_share_period(command, source_result)
    if source_result.source == "claude" and isinstance(source_result.data, dict):
        columns, rows, totals = _claude_legacy_share_rows(command, source_result.data, lib)
    else:
        columns, rows, totals = _source_share_rows(command, source_result, lib)
    reason = (
        source_result.warnings[0].message
        if source_result.status == "unavailable" and source_result.warnings
        else "Source analytics are unavailable."
    )
    availability = "unavailable" if source_result.status == "unavailable" else (
        "empty" if source_result.status == "empty" else "ok"
    )
    source_label = "Claude" if source_result.source == "claude" else "Codex"
    period_label = f"{start.date().isoformat()} → {end.date().isoformat()} (UTC)"
    notes = tuple(warning.message for warning in source_result.warnings if source_result.status != "unavailable")
    return lib.ShareSnapshot(
        cmd=command,
        title=_TERMINAL_TITLES[command],
        subtitle=period_label,
        period=lib.PeriodSpec(start=start, end=end, display_tz="UTC", label=period_label),
        columns=columns,
        rows=rows,
        chart=None,
        totals=totals,
        notes=notes,
        generated_at=end,
        version=c._share_resolve_version(),
        source=source_result.source,
        source_label=source_label,
        availability=availability,
        availability_reason=reason if availability == "unavailable" else None,
    )


def _terminal_amount(value: object) -> str:
    try:
        return f"${float(value):.6f}"
    except (TypeError, ValueError):
        return "$0.000000"


def _terminal_tokens(value: object) -> str:
    try:
        return f"{int(value):,} tokens"
    except (TypeError, ValueError):
        return "0 tokens"


def _terminal_delta_amount(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{'+' if amount >= 0 else '-'}${abs(amount):.6f}"


def _terminal_delta_tokens(value: object) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:+,} tokens"


def _append_diff_terminal(lines: list[str], data: dict[str, object]) -> None:
    """Append source-native diff rows with comparison metrics and statuses."""
    combined = data.get("combined") if isinstance(data.get("combined"), dict) else {}
    costs = combined.get("cost_usd") if isinstance(combined.get("cost_usd"), dict) else {}
    tokens = combined.get("total_tokens") if isinstance(combined.get("total_tokens"), dict) else {}
    if costs or tokens:
        lines.append(
            "Overall: "
            f"A {_terminal_amount(costs.get('a'))} / {_terminal_tokens(tokens.get('a'))}; "
            f"B {_terminal_amount(costs.get('b'))} / {_terminal_tokens(tokens.get('b'))}; "
            f"Δ {_terminal_delta_amount(costs.get('delta'))} / "
            f"{_terminal_delta_tokens(tokens.get('delta'))}"
        )
    else:
        (cost_a, tokens_a), (cost_b, tokens_b) = _legacy_diff_totals(data)
        lines.append(
            "Overall: "
            f"A {_terminal_amount(cost_a)} / {_terminal_tokens(tokens_a)}; "
            f"B {_terminal_amount(cost_b)} / {_terminal_tokens(tokens_b)}; "
            f"Δ {_terminal_delta_amount(cost_b - cost_a)} / "
            f"{_terminal_delta_tokens(tokens_b - tokens_a)}"
        )
    for section_key, section_label, section_status, payload_rows in _diff_sections(data):
        lines.append(f"- {section_label}: {section_status}")
        for payload_row in payload_rows:
            if not isinstance(payload_row, dict):
                continue
            side_a = payload_row.get("a") if isinstance(payload_row.get("a"), dict) else {}
            side_b = payload_row.get("b") if isinstance(payload_row.get("b"), dict) else {}
            delta = payload_row.get("delta") if isinstance(payload_row.get("delta"), dict) else {}
            label = str(payload_row.get("label", section_key))
            status = str(payload_row.get("status", section_status))
            a_tokens = _diff_metric_tokens(side_a)
            b_tokens = _diff_metric_tokens(side_b)
            delta_tokens = _diff_metric_tokens(delta)
            lines.append(
                f"  - {label} [{status}]: "
                f"A {_terminal_amount(side_a.get('cost_usd'))} / "
                f"{_terminal_tokens(a_tokens)}; "
                f"B {_terminal_amount(side_b.get('cost_usd'))} / "
                f"{_terminal_tokens(b_tokens)}; "
                f"Δ {_terminal_delta_amount(delta.get('cost_usd'))} / "
                f"{_terminal_delta_tokens(delta_tokens)}"
            )


def _render_codex_terminal(
    command: str, result: SourceResult, *, diff: bool,
    include_unavailable_diagnostic: bool = True,
) -> str:
    """Render a compact, provider-native terminal report without raw paths."""
    title = _TERMINAL_TITLES.get(command, "Analytics Report")
    lines = [f"Codex {title}"]
    if result.status == "unavailable":
        if not include_unavailable_diagnostic:
            return "\n".join([*lines, "Unavailable."])
        diagnostic = result.warnings[0].message if result.warnings else "Codex analytics are unavailable."
        return "\n".join([*lines, f"Unavailable: {diagnostic}"])
    if result.status == "empty":
        return "\n".join([*lines, "No data."])
    if result.status == "partial":
        diagnostic = result.warnings[0].message if result.warnings else "Some Codex detail is unavailable."
        lines.append(f"Partial: {diagnostic}")
    wire = source_result_wire(result, diff=diff)
    data = wire.get("data")
    if not isinstance(data, dict):
        return "\n".join(lines)
    if command == "project":
        totals = data.get("totals", {})
        lines.append(
            f"Total: {_terminal_amount(totals.get('costUsd'))} · "
            f"{_terminal_tokens(totals.get('totalTokens'))}"
        )
        for row in data.get("projects", ()):
            if isinstance(row, dict):
                lines.append(
                    f"- {row.get('displayLabel', '(unassigned)')}: "
                    f"{_terminal_amount(row.get('costUsd'))} · "
                    f"{_terminal_tokens(row.get('tokens', {}).get('totalTokens'))}"
                )
                # The pure project result omits this key unless --breakdown
                # was requested, so preserving it here makes the accepted
                # flag visible on both machine and terminal surfaces.
                for model in row.get("models", ()):
                    if isinstance(model, dict):
                        lines.append(
                            f"  - {model.get('model', '(unknown)')}: "
                            f"{_terminal_amount(model.get('costUsd'))} · "
                            f"{_terminal_tokens(model.get('totalTokens'))}"
                        )
    elif command == "range-cost":
        totals = data.get("totals", {})
        lines.append(
            f"Total: {_terminal_amount(totals.get('costUsd'))} · "
            f"{_terminal_tokens(totals.get('totalTokens'))}"
        )
        for row in data.get("models", ()):
            if isinstance(row, dict):
                lines.append(f"- {row.get('model', '(unknown)')}: {_terminal_amount(row.get('costUsd'))}")
    elif command == "cache-report":
        totals = data.get("totals", {})
        lines.append(
            f"Total: {_terminal_amount(totals.get('costUsd'))} · "
            f"{_terminal_tokens(totals.get('totalTokens'))}"
        )
        for section in data.get("sections", ()):
            if not isinstance(section, dict) or section.get("key") != "token-reuse":
                continue
            section_data = section.get("data") or {}
            for row in section_data.get("rows", ()):
                if isinstance(row, dict):
                    percent = row.get("cachedInputPercent")
                    rendered = "—" if percent is None else f"{float(percent):.1f}%"
                    lines.append(f"- {row.get('label', 'Codex')}: cached input {rendered}")
    elif command == "diff":
        _append_diff_terminal(lines, data)
    elif command == "report":
        sections = data.get("sections", ())
        series = 0
        for section in sections:
            if isinstance(section, dict):
                for source_series in (section.get("data") or {}).get("series", ()):
                    if not isinstance(source_series, dict):
                        continue
                    series += 1
                    slot = source_series.get("slot", "quota")
                    for row in source_series.get("rows", ()):
                        if not isinstance(row, dict):
                            continue
                        lines.append(
                            f"- {slot}: {_terminal_amount(row.get('costUsd'))} · "
                            f"{row.get('usedPercent', '—')}%"
                        )
                        for detail in row.get("detail", ()):
                            if isinstance(detail, dict):
                                lines.append(
                                    f"  - {detail.get('percentThreshold', '—')}%: "
                                    f"{_terminal_amount(detail.get('cumulativeCostUSD'))}"
                                )
        lines.append(f"Quota series: {series}")
    return "\n".join(lines)


def _render_claude_terminal(command: str, result: SourceResult) -> str:
    """Render an all-source Claude section without changing legacy CLI paths."""
    title = _TERMINAL_TITLES.get(command, "Analytics Report")
    lines = [f"Claude {title}"]
    if result.status == "unavailable":
        return "\n".join([*lines, "Unavailable: Claude analytics are unavailable."])
    if result.status == "empty":
        return "\n".join([*lines, "No data."])
    if command == "diff" and isinstance(result.data, dict):
        _append_diff_terminal(lines, result.data)
        return "\n".join(lines)
    cost, tokens = _legacy_claude_totals(result.data)
    if cost or tokens:
        lines.append(f"Total: {_terminal_amount(cost)} · {_terminal_tokens(tokens)}")
    else:
        lines.append("Data available.")
    return "\n".join(lines)


def _emit_source_share(
    args: object, result: SourceResult, *, command: str,
    claude: SourceResult | None,
) -> int:
    """Render one provider snapshot or a Claude-then-Codex composition."""
    c = _cctally()
    codex_snap = build_source_share_snapshot(
        command, result, reveal_projects=bool(getattr(args, "reveal_projects", False)),
    )
    if getattr(args, "source", "codex") != "all":
        c._share_render_and_emit(codex_snap, args)
    else:
        if claude is None:
            raise ValueError("all-source share rendering requires a Claude result")
        lib = c._share_load_lib()
        reveal_projects = bool(getattr(args, "reveal_projects", False))
        claude_snap = build_source_share_snapshot(
            command, claude, reveal_projects=reveal_projects,
        )
        sections = (
            lib.ComposedSection(
                snap=lib._scrub(claude_snap, reveal_projects=reveal_projects),
                drift_detected=False,
            ),
            lib.ComposedSection(
                snap=lib._scrub(codex_snap, reveal_projects=reveal_projects),
                drift_detected=False,
            ),
        )
        content = lib.compose(
            sections,
            opts=lib.ComposeOptions(
                title=f"{_TERMINAL_TITLES[command]} — Claude + Codex",
                theme=getattr(args, "theme", "light"),
                format=args.format,
                no_branding=bool(getattr(args, "no_branding", False)),
                reveal_projects=reveal_projects,
            ),
        )
        utc_date = codex_snap.generated_at.astimezone(UTC).strftime("%Y-%m-%d")
        kind, value = c._resolve_destination(args, cmd=command, generated_at_utc_date=utc_date)
        c._emit(content, kind=kind, value=value)
        if getattr(args, "open_after_write", False) and kind == "file":
            c._share_open_file(value)
    return 3 if (
        getattr(args, "project", None)
        and command in {"range-cost", "cache-report"}
        and result.status == "unavailable"
    ) else 0


def _emit_source_result(
    args: object, result: SourceResult, *, diff: bool = False, report: bool = False,
    claude: SourceResult | None = None,
) -> int:
    if getattr(args, "format", None):
        return _emit_source_share(
            args, result,
            command=str(getattr(args, "command", "analytics")),
            claude=claude,
        )
    if getattr(args, "source", "codex") == "all":
        if claude is None:
            raise ValueError("all-source rendering requires a Claude result")
        wire = _all_source_wire(claude, result, diff=diff, report=report)
        if bool(getattr(args, "json", False)):
            print(json.dumps(wire, separators=(",", ":")))
        else:
            command = str(getattr(args, "command", "analytics"))
            project_degradation = (
                getattr(args, "project", None)
                and command in {"range-cost", "cache-report"}
                and result.status == "unavailable"
            )
            sections = [
                _render_claude_terminal(command, claude),
                _render_codex_terminal(
                    command, result, diff=diff,
                    include_unavailable_diagnostic=not project_degradation,
                ),
            ]
            if not report:
                combined = wire.get("combined")
                if isinstance(combined, dict):
                    if diff:
                        costs = combined.get("cost_usd", {})
                        tokens = combined.get("total_tokens", {})
                        sections.append(
                            "Combined physical accounting: "
                            f"A {_terminal_amount(costs.get('a') if isinstance(costs, dict) else None)}; "
                            f"B {_terminal_amount(costs.get('b') if isinstance(costs, dict) else None)}; "
                            f"Δ {_terminal_delta_amount(costs.get('delta') if isinstance(costs, dict) else None)}; "
                            f"A {_terminal_tokens(tokens.get('a') if isinstance(tokens, dict) else None)}; "
                            f"B {_terminal_tokens(tokens.get('b') if isinstance(tokens, dict) else None)}; "
                            f"Δ {_terminal_delta_tokens(tokens.get('delta') if isinstance(tokens, dict) else None)}"
                        )
                    else:
                        sections.append(
                            "Combined physical accounting: "
                            f"{_terminal_amount(combined.get('costUsd'))} · "
                            f"{_terminal_tokens(combined.get('totalTokens'))}"
                        )
            print("\n\n".join(sections))
            if project_degradation:
                diagnostic = result.warnings[0].message if result.warnings else "Codex analytics are unavailable."
                print(f"cctally: {diagnostic}", file=sys.stderr)
        if (
            getattr(args, "project", None)
            and getattr(args, "command", None) in {"range-cost", "cache-report"}
            and result.status == "unavailable"
        ):
            return 3
        return 0
    wire = source_result_wire(result, diff=diff)
    if bool(getattr(args, "json", False)):
        print(json.dumps(wire, separators=(",", ":")))
    elif result.status == "unavailable":
        # Design 10.2 keeps requested-project range/cache direct stdout empty;
        # the other provider reports carry their stable unavailable diagnostic.
        if not (
            getattr(args, "project", None)
            and getattr(args, "command", None) in {"range-cost", "cache-report"}
        ):
            print(_render_codex_terminal(str(getattr(args, "command", "analytics")), result, diff=diff))
    else:
        print(_render_codex_terminal(str(getattr(args, "command", "analytics")), result, diff=diff))
    return 3 if result.status == "unavailable" else 0


def cmd_source_project(args: object) -> int:
    _cctally()._share_validate_args(args)
    if getattr(args, "source", "codex") == "claude" and getattr(args, "format", None):
        return _emit_source_result(args, _run_claude_json_adapter(args, "project"))
    start, end = _resolve_source_project_range(args)
    try:
        validate_source_project_selectors(getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    # Keep the Claude compatibility block on the single calendar interval
    # selected for the provider-aware command; without this, its legacy
    # default would silently choose a subscription-week boundary instead.
    # The provider reader is half-open; the legacy project command renders a
    # date-only --until as its inclusive final microsecond.  These describe
    # the identical instants, while retaining the frozen Claude JSON labels.
    claude_end = end
    raw_until = getattr(args, "until", None)
    if isinstance(raw_until, str) and not any(
        marker in raw_until for marker in ("T", "+", "Z")
    ):
        claude_end -= dt.timedelta(microseconds=1)
    args._source_analytics_range = (start, claude_end)
    claude = (
        _run_claude_json_adapter(args, "project")
        if getattr(args, "source", "codex") == "all" else None
    )
    try:
        population_entries = _source_entries(
            args, start, end, qualified=True, group=getattr(args, "group", "git-root"),
        )
    except QualifiedMetadataUnavailable:
        return _emit_source_result(
            args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
            claude=claude,
        )
    try:
        entries = _filter_project_entries(population_entries, getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    entries = _filter_model_entries(entries, getattr(args, "model", None))
    allocation_entries = population_entries
    quota_available = True
    supplied_blocks = getattr(args, "blocks", None)
    if supplied_blocks is None:
        try:
            blocks = select_codex_project_blocks(
                build_blocks(load_codex_quota_observations()),
                range_start=start, range_end=end, as_of=getattr(args, "as_of", end),
            )
        except (OSError, sqlite3.Error, ValueError):
            # Accounting stays useful when S2's operational state is absent;
            # omit attributions rather than manufacturing a blended quota.
            blocks = ()
            quota_available = False
    else:
        blocks = select_codex_project_blocks(
            tuple(supplied_blocks),
            range_start=start, range_end=end, as_of=getattr(args, "as_of", end),
        )
    if blocks:
        block_start = min(block.nominal_start_at for block in blocks)
        block_end = max(
            min(getattr(args, "as_of", end), block.resets_at) for block in blocks
        )
        if block_start < start or block_end > end:
            try:
                read_args = copy(args)
                read_args._source_analytics_sync = False
                population_entries = _source_entries(
                    read_args,
                    min(start, block_start), max(end, block_end),
                    qualified=True, group=getattr(args, "group", "git-root"),
                )
                entries = _filter_project_entries(population_entries, getattr(args, "project", None))
                entries = _filter_model_entries(entries, getattr(args, "model", None))
                allocation_entries = population_entries
            except QualifiedMetadataUnavailable:
                return _emit_source_result(
                    args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
                    claude=claude,
                )
    result = build_codex_project_result(
        entries, range_start=start, range_end=end,
        blocks=blocks,
        as_of=getattr(args, "as_of", end),
        sort=getattr(args, "sort", "cost"), order=getattr(args, "order", "desc"),
        include_breakdown=bool(getattr(args, "breakdown", False)),
        allocation_entries=allocation_entries,
        quota_available=quota_available,
    )
    return _emit_source_result(args, result, claude=claude)


def cmd_source_diff(args: object) -> int:
    _cctally()._share_validate_args(args)
    if getattr(args, "source", "codex") == "claude" and getattr(args, "format", None):
        return _emit_source_result(args, _run_claude_json_adapter(args, "diff"), diff=True)
    window_a, window_b = _resolve_source_diff_windows(args)
    if getattr(args, "a", None) is None and getattr(args, "b", None) is None:
        # Direct adapter callers supply resolved AnalyticsWindow instances;
        # their enclosing test/host owns validation, so preserve the pure
        # half-open seam without pretending it is a parsed CLI request.
        normalization = "none"
    else:
        try:
            normalization = _validate_source_diff_controls(args, window_a, window_b)
        except SourceUsageError as exc:
            print(f"cctally: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"diff: {exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    # The all-source calendar-window contract is resolved once here.  The
    # established Claude renderer consumes the same absolute intervals through
    # its structured seam instead of attempting subscription-week anchoring.
    args._source_analytics_windows = (window_a, window_b)
    source = getattr(args, "source", "codex")
    claude_only = _diff_only_for_provider(getattr(args, "only", None), "claude")
    codex_only = _diff_only_for_provider(getattr(args, "only", None), "codex")
    if source == "all":
        if claude_only == "":
            claude = SourceResult("claude", "empty", {"sections": []})
        else:
            claude_args = copy(args)
            claude_args.only = claude_only
            claude = _run_claude_json_adapter(claude_args, "diff")
    else:
        claude = None
    start, end = min(window_a.start_at, window_b.start_at), max(window_a.end_at, window_b.end_at)
    requires_project_metadata = (
        codex_only is None or "projects" in codex_only.split(",")
    )
    try:
        entry_args = copy(args)
        entry_args._source_analytics_sync = bool(getattr(args, "sync", False))
        entries = _source_entries(
            entry_args, start, end, qualified=requires_project_metadata,
        )
        metadata_available = True
    except QualifiedMetadataUnavailable:
        try:
            entries = _source_entries(entry_args, start, end, qualified=False)
        except RuntimeError:
            return _emit_source_result(
                args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
                diff=True, claude=claude,
            )
        metadata_available = False
    except RuntimeError:
        return _emit_source_result(
            args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
            diff=True, claude=claude,
        )
    return _emit_source_result(
        args,
        build_codex_diff_result(
            entries, window_a, window_b,
            project_metadata_available=metadata_available,
            only=(codex_only if source == "all" else getattr(args, "only", None)),
            allow_mismatch=bool(getattr(args, "allow_mismatch", False)),
            show_all=bool(getattr(args, "show_all", False)),
            min_delta_usd=(
                0.10 if getattr(args, "min_delta_usd", None) is None
                else float(args.min_delta_usd)
            ),
            min_delta_pct=(
                1.0 if getattr(args, "min_delta_pct", None) is None
                else float(args.min_delta_pct)
            ),
            sort=str(getattr(args, "sort", "delta")),
            top=getattr(args, "top", None),
            normalization=normalization,
            classify_presence=True,
        ),
        diff=True, claude=claude,
    )


def cmd_source_range_cost(args: object) -> int:
    _cctally()._share_validate_args(args)
    if getattr(args, "source", "codex") == "claude" and getattr(args, "format", None):
        return _emit_source_result(args, _run_claude_json_adapter(args, "range-cost"))
    start, end = _resolve_source_range_cost_range(args)
    try:
        validate_source_project_selectors(getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    claude = (
        _run_claude_json_adapter(args, "range-cost")
        if getattr(args, "source", "codex") == "all" else None
    )
    try:
        entries = _source_entries(
            args, start, end, qualified=bool(getattr(args, "project", None)),
            inclusive_end=True,
        )
    except (QualifiedMetadataUnavailable, RuntimeError):
        return _emit_source_result(
            args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
            claude=claude,
        )
    try:
        entries = _filter_project_entries(entries, getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    result = build_codex_range_result(
        entries, start, end, include_breakdown=bool(getattr(args, "breakdown", False)),
    )
    if getattr(args, "total_only", False) and not getattr(args, "format", None):
        codex_cost, _ = _codex_totals(result)
        if claude is not None:
            claude_cost, _ = _legacy_claude_totals(claude.data)
            codex_cost += claude_cost
        print(f"{codex_cost:.9f}")
        return 0
    return _emit_source_result(args, result, claude=claude)


def cmd_source_cache_report(args: object) -> int:
    _cctally()._share_validate_args(args)
    if getattr(args, "source", "codex") == "claude" and getattr(args, "format", None):
        return _emit_source_result(args, _run_claude_json_adapter(args, "cache-report"))
    start, end = _resolve_source_cache_range(args)
    try:
        validate_source_project_selectors(getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "source", "codex") == "all":
        # ``reuse`` is Codex-only.  The all-source Claude leg retains its
        # ordinary daily/session default rather than receiving an invalid
        # provider-specific sort; the Codex leg below still uses reuse-desc.
        claude_args = copy(args)
        if getattr(claude_args, "sort", None) == "reuse":
            claude_args.sort = None
        claude = _run_claude_json_adapter(claude_args, "cache-report")
    else:
        claude = None
    try:
        entry_args = copy(args)
        entry_args._source_analytics_sync = True
        entries = _source_entries(entry_args, start, end, qualified=True)
        metadata_available = True
    except QualifiedMetadataUnavailable:
        if getattr(args, "project", None):
            return _emit_source_result(
                args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
                claude=claude,
            )
        try:
            entries = _source_entries(entry_args, start, end, qualified=False)
        except RuntimeError:
            return _emit_source_result(
                args, SourceResult("codex", "unavailable", None, (QUALIFIED_METADATA_WARNING,)),
                claude=claude,
            )
        metadata_available = False
    try:
        entries = _filter_project_entries(entries, getattr(args, "project", None))
    except SourceUsageError as exc:
        print(f"cctally: {exc}", file=sys.stderr)
        return 2
    requested_sort = getattr(args, "sort", None)
    codex_sort = (
        requested_sort if requested_sort in {"date", "recent", "cost", "reuse"}
        else None
    )
    result = build_codex_reuse_result(
        entries,
        group_by=("session" if getattr(args, "by_session", False)
                  else getattr(args, "group_by", "date")),
        project_metadata_available=metadata_available,
        sort=codex_sort,
        range_start=start,
        range_end=end,
    )
    return _emit_source_result(args, result, claude=claude)


def cmd_source_report(args: object) -> int:
    _cctally()._share_validate_args(args)
    if getattr(args, "source", "codex") == "claude" and getattr(args, "format", None):
        return _emit_source_result(
            args, _run_claude_json_adapter(args, "report"), report=True,
        )
    as_of = _resolve_source_report_as_of(args)
    claude = (
        _run_claude_json_adapter(args, "report")
        if getattr(args, "source", "codex") == "all" else None
    )
    supplied_blocks = getattr(args, "blocks", None)
    already_synced = False
    force_direct_accounting = False
    if getattr(args, "sync_current", False) and supplied_blocks is None:
        c = _cctally()
        try:
            cache = c.open_cache_db()
            try:
                sync_stats = c.sync_codex_cache(cache)
            finally:
                cache.close()
            c.reconcile_codex_quota_projection(now=as_of)
            force_direct_accounting = bool(getattr(sync_stats, "lock_contended", False))
            already_synced = not force_direct_accounting
        except (OSError, sqlite3.Error, RuntimeError):
            return _emit_source_result(
                args, build_codex_report_result((), (), as_of=as_of, quota_available=False),
                report=True, claude=claude,
            )
    if supplied_blocks is None:
        try:
            blocks = select_codex_report_blocks(
                build_blocks(load_codex_quota_observations()),
                weeks=int(getattr(args, "weeks", 1)),
            )
        except (OSError, sqlite3.Error, ValueError):
            return _emit_source_result(
                args, build_codex_report_result((), (), as_of=as_of, quota_available=False),
                report=True, claude=claude,
            )
    else:
        blocks = tuple(supplied_blocks)
    if blocks:
        start = min(block.nominal_start_at for block in blocks)
        end = min(as_of, max(block.resets_at for block in blocks))
        try:
            entries = _source_entries(
                args, start, end, qualified=False, sync=not already_synced,
                force_direct=force_direct_accounting,
            )
        except RuntimeError:
            return _emit_source_result(
                args, build_codex_report_result((), (), as_of=as_of, quota_available=False),
                report=True, claude=claude,
            )
    else:
        entries = tuple(getattr(args, "source_entries", ()))
    return _emit_source_result(
        args,
        build_codex_report_result(
            entries, blocks, as_of=as_of,
            include_detail=bool(getattr(args, "detail", False)),
        ),
        report=True, claude=claude,
    )
