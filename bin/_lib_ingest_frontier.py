"""Fail-safe dashboard ingest frontier (issue #680).

The provider hooks append the path named by their trusted event payload.  A
dashboard that has completed one ordinary full sync can then distinguish an
append target from a structurally unchanged tree without globbing or statting
every JSONL file.  Directory identity covers add/delete/rename; database and
schema identity cover replacement/migration.  Missing, malformed, truncated,
or otherwise ambiguous evidence always returns ``full``.

Every one of those guards needs a writer to move something.  A provider whose
hooks never run therefore holds its certificate while its sources grow, so a
certificate also expires purely on elapsed time after
``FRONTIER_CERTIFICATE_MAX_AGE_SECONDS``.

The activity journal is private runtime state.  Diagnostic surfaces publish
only the bounded ``mode``/``reason`` enums and counts, never its paths.
"""
from __future__ import annotations

import json
import fcntl
import os
import pathlib
import shlex
import time
from dataclasses import dataclass
from _lib_retained_size import retained_size_bytes

_MARKER_NAME = "dashboard-ingest-activity.jsonl"
_MARKER_LOCK_NAME = "dashboard-ingest-activity.lock"
_MARKER_ROTATE_BYTES = 4 * 1024 * 1024
_PROVIDERS = frozenset({"claude", "codex"})
FRONTIER_MAX_BYTES = 16 * 1024 * 1024
# Upper bound on how long one certificate may stand without an exhaustive
# walk.  Every other guard compares evidence some writer must produce -- a hook
# ticket, a directory mtime, a schema bump -- and appending to an
# already-tracked file moves none of them.  Elapsed time is the only evidence
# that accrues without anyone's cooperation, so this bound is what makes the
# worst-case staleness finite when a provider's hooks are absent, disabled or
# untrusted.  One walk per interval per provider is the whole cost.
#
# 120s measured against the operator's production store (2,854 Codex rollouts,
# 197k accounting entries), where one accounting-only walk costs 2.1-23.1s with
# a ~7s median.  That is roughly 6% of one core, against ~12% at 60s and the
# ~44% the unconditional per-tick walk this replaced was measured at.  A live
# tail advances its own conversation's accounting directly, so this bound is
# the safety net for surfaces nobody is watching, not the interactive path.
FRONTIER_CERTIFICATE_MAX_AGE_SECONDS = 120.0
CODEX_FULL_WALK_COMPLETE_KEY = "dashboard_codex_full_walk_complete"
_COMPLETE_KEYS = {
    "claude": "claude_ingest_walk_complete",
    "codex": CODEX_FULL_WALK_COMPLETE_KEY,
}
_PENDING_META_KEYS = (
    "conversation_backfill_pending",
    "conversation_sessions_backfill_pending",
    "codex_replay_from_zero_pending",
    "codex_replay_from_zero_blocked",
    "codex_replay_from_zero_deferred",
    "cache_creation_split_rewalk_pending",
)

# Transcript-store work which targeted ingest is not entitled to skip.  This
# is deliberately broader than the accounting frontier's pending set: these
# flags are consumed only by a full conversations pass, and a certificate made
# while one is present would strand that work forever.
_CONVERSATION_PENDING_META_KEYS = (
    "conversation_rebuild_claude_pending",
    "conversation_rebuild_codex_pending",
    "conversation_backfill_pending",
    "ai_titles_backfill_pending",
    "conversation_reingest_pending",
    "conversation_source_tool_use_reingest_pending",
    "conversation_reingest_enrichment_pending",
    "conversation_media_reingest_pending",
    "conversation_search_split_pending",
    "conversation_promote_command_args_pending",
    "conversation_sessions_backfill_pending",
    "conversation_queued_prompt_reingest_pending",
    "conversation_reingest_nested_agent_pending",
    "conversation_title_fts_backfill_pending",
    "conversation_reingest_file_touches_pending",
    "conversation_background_mcp_reingest_pending",
    "codex_conversation_replay_from_zero_pending",
    "codex_find_projection_backfill_pending",
)


def _now() -> float:
    """Monotonic seconds used to age certificates; never a wall clock.

    Certificates are process-local, so a monotonic source keeps the bound
    correct across system clock steps, suspend/resume and timezone changes.
    """
    return time.monotonic()


def activity_marker_path(app_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(app_dir) / _MARKER_NAME


def invalidate_activity_marker(app_dir: pathlib.Path) -> bool:
    """Remove the activity certificate after a writer cannot append a ticket.

    Absence is an intentionally fail-closed marker state: every live frontier
    reader falls back to its ordinary full provider pass.  Serialize removal
    with writers and cutoff capture so a later successful ticket cannot be
    unlinked by an earlier failed hook.
    """
    marker = activity_marker_path(pathlib.Path(app_dir))
    lock_path = marker.with_name(_MARKER_LOCK_NAME)
    lock_fd = None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        return True
    except OSError:
        # Even when the lock itself is unavailable, an atomic unlink is safe:
        # a concurrent writer either recreates the pathname with its ticket or
        # finishes on the unlinked inode and leaves the pathname absent.  Both
        # outcomes are conservative for the next reader.
        try:
            marker.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def record_activity(app_dir: pathlib.Path, provider: str, source_path: str) -> bool:
    """Append one hook-owned activity ticket before background work starts."""
    if provider not in _PROVIDERS:
        return False
    marker = activity_marker_path(pathlib.Path(app_dir))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"provider": provider, "path": str(source_path or "")},
            ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
        lock_path = marker.with_name(_MARKER_LOCK_NAME)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                rotate = marker.stat().st_size >= _MARKER_ROTATE_BYTES
            except OSError:
                rotate = False
            if rotate:
                replacement = marker.with_name(marker.name + ".next")
                fd = os.open(
                    replacement, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    _write_all(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(replacement, marker)
            else:
                fd = os.open(
                    marker, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    _write_all(fd, payload)
                finally:
                    os.close(fd)
            try:
                os.chmod(marker, 0o600)
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        return True
    except OSError:
        return False


def _write_all(fd: int, payload: bytes) -> None:
    """Write one complete marker record or fail closed.

    Regular-file ``os.write`` calls may legally report a short count. The
    hook's marker lock serializes the loop, so completing the remaining bytes
    preserves one newline-delimited ticket without weakening append ordering.
    """
    pending = memoryview(payload)
    while pending:
        try:
            written = os.write(fd, pending)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("activity marker write made no progress")
        pending = pending[written:]


@dataclass(frozen=True)
class FrontierPlan:
    provider: str
    mode: str
    paths: frozenset[str] = frozenset()
    marker_end: int = 0
    reason: str = "none"


@dataclass(frozen=True)
class MarkerCutoff:
    identity: tuple[int, int]
    end: int


@dataclass(frozen=True)
class _MarkerCutoffFailure:
    reason: str = "capture_failed"


_MARKER_CUTOFF_FAILURE = _MarkerCutoffFailure()


@dataclass
class _ProviderState:
    marker_identity: tuple[int, int]
    marker_offset: int
    db_identity: tuple[int, int]
    schema_identity: tuple[int, int]
    pending_identity: tuple[str, ...]
    directory_identity: dict[str, tuple[int, int, int, int]]
    guard_identity: dict[str, tuple[int, int, int, int]]
    # Monotonic timestamp of the exhaustive walk this certificate rests on.
    # The default is deliberately far in the past so a state built without one
    # is already expired rather than trusted forever.
    seeded_at: float = 0.0


def _frontier_memory_stats(owner):
    estimated = retained_size_bytes(
        owner._states, stop_after=FRONTIER_MAX_BYTES)
    return {
        "estimatedBytes": estimated,
        "maxBytes": FRONTIER_MAX_BYTES,
        "entryCount": len(owner._states),
        "maxEntries": len(_PROVIDERS),
        "fallbackCount": int(owner._fallback_count),
    }


def _admit_frontier_state(owner, provider: str, state: _ProviderState) -> bool:
    candidate = dict(owner._states)
    candidate[provider] = state
    if retained_size_bytes(candidate, stop_after=FRONTIER_MAX_BYTES) > (
            FRONTIER_MAX_BYTES):
        owner._states.pop(provider, None)
        owner._fallback_count += 1
        owner.last_seed_failure[provider] = "memory_budget"
        return False
    owner._states[provider] = state
    return True


def _stat_identity(path: pathlib.Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _stat_identity_or_missing(path: pathlib.Path) -> tuple[int, int, int, int]:
    """Represent an already-absent directory as a stable observed state."""
    try:
        return _stat_identity(path)
    except OSError:
        return (0, 0, 0, 0)


def _database_path(conn) -> pathlib.Path:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return pathlib.Path(path)
    raise OSError("database path unavailable")


def _database_identity(conn):
    st = _database_path(conn).stat()
    return st.st_dev, st.st_ino


def _schema_identity(conn) -> tuple[int, int]:
    schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    user = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return schema, user


def _pending_identity(conn) -> tuple[str, ...]:
    placeholders = ",".join("?" for _ in _PENDING_META_KEYS)
    rows = conn.execute(
        f"SELECT key FROM cache_meta WHERE key IN ({placeholders}) ORDER BY key",
        _PENDING_META_KEYS,
    )
    return tuple(str(row[0]) for row in rows)


def _provider_complete(conn, provider: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=? LIMIT 1",
        (_COMPLETE_KEYS[provider],),
    ).fetchone()
    return row is not None


def _source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    query = (
        "SELECT path FROM session_files"
        if provider == "claude"
        else "SELECT path FROM codex_session_files"
    )
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(query)
        if row[0] and os.path.isabs(str(row[0]))
    )


def _directory_paths(conn, provider: str, roots) -> tuple[pathlib.Path, ...]:
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


def _directory_identity(conn, provider: str, roots):
    return {
        str(path): _stat_identity_or_missing(path)
        for path in _directory_paths(conn, provider, roots)
    }


def _restat_directory_identity(saved):
    return {
        raw: _stat_identity_or_missing(pathlib.Path(raw))
        for raw in saved
    }


def _guard_identity(paths):
    result = {}
    for raw in paths:
        path = pathlib.Path(raw)
        try:
            result[str(path)] = _stat_identity(path)
        except OSError:
            result[str(path)] = (0, 0, 0, 0)
    return result


def _read_marker(marker: pathlib.Path, offset: int):
    with marker.open("rb") as fh:
        st = os.fstat(fh.fileno())
        identity = (st.st_dev, st.st_ino)
        if st.st_size < offset:
            raise ValueError("truncated")
        fh.seek(offset)
        raw = fh.read()
        end = fh.tell()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("partial")
    records = _decode_marker_records(raw)
    return identity, end, records


def _decode_marker_records(raw: bytes):
    records = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed") from None
        if not isinstance(value, dict) or value.get("provider") not in _PROVIDERS:
            raise ValueError("malformed")
        path = value.get("path")
        if not isinstance(path, str):
            raise ValueError("malformed")
        records.append((value["provider"], path))
    return records


def _validate_marker_prefix(marker: pathlib.Path, cutoff: "MarkerCutoff") -> None:
    """Validate all evidence that a successful full walk proposes to consume."""
    with marker.open("rb") as fh:
        st = os.fstat(fh.fileno())
        if (st.st_dev, st.st_ino) != cutoff.identity or st.st_size < cutoff.end:
            raise ValueError("activity_marker_changed")
        raw = fh.read(cutoff.end)
    if len(raw) != cutoff.end:
        raise ValueError("activity_marker_changed")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("partial")
    _decode_marker_records(raw)


def _target_has_cursor_gap(conn, provider: str, source_path: str) -> bool:
    table = "session_files" if provider == "claude" else "codex_session_files"
    row = conn.execute(
        f"SELECT size_bytes, last_byte_offset FROM {table} WHERE path=?",
        (source_path,),
    ).fetchone()
    if row is None:
        return False  # a genuinely new file is a valid targeted append
    try:
        actual_size = pathlib.Path(source_path).stat().st_size
    except OSError:
        return True
    return actual_size < max(int(row[0] or 0), int(row[1] or 0))


def _target_in_roots(source_path: str, roots) -> bool:
    path = pathlib.Path(source_path)
    if not path.is_absolute() or path.suffix != ".jsonl":
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_file():
        return False
    for root in roots:
        try:
            resolved.relative_to(pathlib.Path(root).resolve(strict=True))
        except (OSError, ValueError):
            continue
        return True
    return False


def is_dashboard_activity_claude_hook_handler(handler: object) -> bool:
    """Require the exact executable handler shape that writes tickets."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(
        len(tokens) == 2
        and pathlib.Path(tokens[0]).is_absolute()
        and pathlib.Path(tokens[0]).name in {"cctally", "cctally-npm-shim.js"}
        and tokens[1] == "hook-tick"
    )


def provider_sync_certifiable(mode: str, stats: object) -> bool:
    """Whether one provider result can advance or mint a certificate."""
    if mode == "caught_up":
        return True
    if stats is None:
        return False
    common_clean = not any((
        getattr(stats, "lock_contended", False),
        getattr(stats, "files_failed", 0),
        getattr(stats, "files_deferred_torn", 0),
        getattr(stats, "deferred_reason", None),
        getattr(stats, "prune_refused", False),
        getattr(stats, "budget_exhausted", False),
        getattr(stats, "maintenance_failed", False),
    ))
    if not common_clean:
        return False
    if mode == "targeted":
        return bool(getattr(stats, "targeted_clean", True))
    if mode == "full":
        return bool(getattr(stats, "full_walk_complete", False))
    return False


class DashboardIngestFrontier:
    """One process-local certificate per provider, rebuilt after full sync."""

    def __init__(self, app_dir: pathlib.Path):
        self.app_dir = pathlib.Path(app_dir)
        self._states: dict[str, _ProviderState] = {}
        self.last_seed_failure: dict[str, str] = {}
        self._fallback_count = 0

    def memory_stats(self):
        return _frontier_memory_stats(self)

    def capture_cutoff(self) -> "MarkerCutoff | _MarkerCutoffFailure":
        """Capture a writer-serialized boundary before a full walk starts.

        Materializing an absent marker under the same flock used by hook
        writers gives the first full walk a real zero-byte cutoff. A ticket
        appended after this method returns therefore remains pending instead
        of being swallowed by finalization.
        """
        marker = activity_marker_path(self.app_dir)
        lock_path = marker.with_name(_MARKER_LOCK_NAME)
        lock_fd = None
        marker_fd = None
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            marker_fd = os.open(marker, os.O_RDONLY | os.O_CREAT, 0o600)
            st = os.fstat(marker_fd)
        except OSError:
            return _MARKER_CUTOFF_FAILURE
        finally:
            if marker_fd is not None:
                os.close(marker_fd)
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return MarkerCutoff((st.st_dev, st.st_ino), st.st_size)

    def seed_provider(
        self, provider: str, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> bool:
        if provider not in _PROVIDERS:
            raise ValueError("unknown provider")
        if not trusted:
            self.last_seed_failure[provider] = "untrusted_hook_configuration"
            self._states.pop(provider, None)
            return False
        marker = activity_marker_path(self.app_dir)
        try:
            boundary = cutoff if cutoff is not None else self.capture_cutoff()
            if not isinstance(boundary, MarkerCutoff):
                self.last_seed_failure[provider] = "cutoff_capture_failed"
                self._states.pop(provider, None)
                return False
            st = marker.stat()
            marker_identity = (st.st_dev, st.st_ino)
            if marker_identity != boundary.identity or st.st_size < boundary.end:
                self.last_seed_failure[provider] = "activity_marker_changed"
                self._states.pop(provider, None)
                return False
            _validate_marker_prefix(marker, boundary)
            pending_identity = _pending_identity(conn)
            if pending_identity:
                self.last_seed_failure[provider] = "maintenance_pending"
                self._states.pop(provider, None)
                return False
            if not _provider_complete(conn, provider):
                self.last_seed_failure[provider] = "incomplete_store"
                self._states.pop(provider, None)
                return False
            candidate = _ProviderState(
                marker_identity=marker_identity,
                marker_offset=boundary.end,
                db_identity=_database_identity(conn),
                schema_identity=_schema_identity(conn),
                pending_identity=pending_identity,
                directory_identity=_directory_identity(conn, provider, roots),
                guard_identity=_guard_identity(guard_paths),
                seeded_at=_now(),
            )
            if not _admit_frontier_state(self, provider, candidate):
                return False
            self.last_seed_failure[provider] = ""
            return True
        except (OSError, ValueError) as exc:
            self.last_seed_failure[provider] = f"{type(exc).__name__}:{exc}"
            self._states.pop(provider, None)
            return False

    def plan_provider(self, provider: str, conn, *, roots, guard_paths=()) -> FrontierPlan:
        state = self._states.get(provider)
        if state is None:
            return FrontierPlan(provider, "full", reason="unseeded")
        # Checked before every other guard: an expired certificate needs a full
        # walk whatever the remaining evidence says, and answering here keeps
        # the expiry itself free of database and filesystem work.
        if _now() - state.seeded_at >= FRONTIER_CERTIFICATE_MAX_AGE_SECONDS:
            return FrontierPlan(provider, "full", reason="certificate_expired")
        marker = activity_marker_path(self.app_dir)
        try:
            if _database_identity(conn) != state.db_identity:
                raise ValueError("database_replaced")
            if _schema_identity(conn) != state.schema_identity:
                raise ValueError("schema_changed")
            if not _provider_complete(conn, provider):
                raise ValueError("incomplete_store")
            if _pending_identity(conn) != state.pending_identity:
                raise ValueError("maintenance_changed")
            if _guard_identity(guard_paths) != state.guard_identity:
                raise ValueError("hook_config_changed")
            if _restat_directory_identity(
                state.directory_identity
            ) != state.directory_identity:
                raise ValueError("filesystem_changed")
            marker_identity, marker_end, records = _read_marker(
                marker, state.marker_offset)
            if marker_identity != state.marker_identity:
                raise ValueError("marker_replaced")
        except (OSError, ValueError) as exc:
            reason = str(exc) if str(exc) else "ambiguous"
            return FrontierPlan(provider, "full", reason=reason)
        provider_paths = tuple(
            path for kind, path in records if kind == provider)
        if any(not path for path in provider_paths):
            return FrontierPlan(
                provider, "full", marker_end=marker_end,
                reason="ambiguous_activity",
            )
        paths = frozenset(provider_paths)
        if any(not _target_in_roots(path, roots) for path in paths):
            return FrontierPlan(
                provider, "full", marker_end=marker_end,
                reason="target_outside_scope",
            )
        if any(_target_has_cursor_gap(conn, provider, path) for path in paths):
            return FrontierPlan(provider, "full", reason="cursor_gap")
        return FrontierPlan(
            provider,
            "targeted" if paths else "caught_up",
            paths=paths,
            marker_end=marker_end,
            reason="activity" if paths else "unchanged",
        )

    def commit_provider(
        self, plan: FrontierPlan, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> None:
        """Advance a clean caught-up/targeted plan; full plans are reseeded."""
        if plan.mode == "full":
            self.seed_provider(
                plan.provider, conn, roots=roots, guard_paths=guard_paths,
                trusted=trusted, cutoff=cutoff,
            )
            return
        state = self._states.get(plan.provider)
        if state is None:
            return
        if plan.mode == "caught_up":
            # The plan already compared every guard. Advancing an empty marker
            # slice must remain O(1); recomputing the estate here would erase
            # the fast-negative immediately after taking it.
            state.marker_offset = plan.marker_end
            return
        # Targeted ingest may have changed schema-neutral cache guard values;
        # refresh every cheap guard from the committed connection.
        state.marker_offset = plan.marker_end
        state.db_identity = _database_identity(conn)
        state.schema_identity = _schema_identity(conn)
        state.pending_identity = _pending_identity(conn)
        state.directory_identity = _directory_identity(
            conn, plan.provider, roots)
        state.guard_identity = _guard_identity(guard_paths)
        _admit_frontier_state(self, plan.provider, state)


def _conversation_pending_identity(conn) -> tuple[str, ...]:
    placeholders = ",".join("?" for _ in _CONVERSATION_PENDING_META_KEYS)
    rows = conn.execute(
        f"SELECT key FROM cache_meta WHERE key IN ({placeholders}) ORDER BY key",
        _CONVERSATION_PENDING_META_KEYS,
    )
    return tuple(str(row[0]) for row in rows)


def _conversation_source_table(provider: str) -> str:
    if provider == "claude":
        return "conversation_source_files"
    if provider == "codex":
        return "codex_conversation_source_files"
    raise ValueError("unknown provider")


def _conversation_source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    table = _conversation_source_table(provider)
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(f"SELECT path FROM {table}")
        if row[0] and os.path.isabs(str(row[0]))
    )


def _conversation_directory_paths(conn, provider: str, roots):
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _conversation_source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


def _conversation_directory_identity(conn, provider: str, roots):
    return {
        str(path): _stat_identity_or_missing(path)
        for path in _conversation_directory_paths(conn, provider, roots)
    }


def _conversation_target_risk(conn, provider: str, source_path: str) -> str | None:
    """Classify a ticketed path that cannot use ordinary append ingest."""
    table = _conversation_source_table(provider)
    row = conn.execute(
        f"SELECT size_bytes,mtime_ns,last_byte_offset FROM {table} WHERE path=?",
        (source_path,),
    ).fetchone()
    if row is None:
        return None
    try:
        stat = pathlib.Path(source_path).stat()
    except OSError:
        return "cursor_gap"
    stored_size = int(row[0] or 0)
    if stat.st_size < max(stored_size, int(row[2] or 0)):
        return "cursor_gap"
    if stat.st_size == stored_size and stat.st_mtime_ns != int(row[1] or 0):
        return "source_replaced"
    return None


def conversation_sync_certifiable(
    mode: str, stats: object, *, expected_paths: int = 0,
) -> bool:
    """Whether one transcript result may advance its process certificate."""
    if mode == "caught_up":
        return True
    if stats is None:
        return False
    if any((
        getattr(stats, "lock_contended", False),
        getattr(stats, "files_failed", 0),
        getattr(stats, "files_deferred_torn", 0),
        getattr(stats, "deferred_reason", None),
        getattr(stats, "prune_refused", False),
        getattr(stats, "budget_exhausted", False),
        getattr(stats, "maintenance_failed", False),
    )):
        return False
    total = int(getattr(stats, "files_total", 0) or 0)
    completed = (
        int(getattr(stats, "files_processed", 0) or 0)
        + int(getattr(stats, "files_skipped_unchanged", 0) or 0)
    )
    if mode == "targeted":
        # A source deleted after planning is filtered before the sync sees it;
        # do not consume that ticket as an empty successful target set.
        return total == expected_paths and completed == total
    if mode == "full":
        # There is no durable conversation-walk sentinel.  The per-call census
        # is therefore the proof that the exhaustive discovery actually drained.
        return completed == total
    return False


class ConversationSyncFrontier:
    """Independent hook-journal certificates for `conversations.db`.

    The activity file is shared evidence, not a consumable queue.  This class
    owns its byte cursor independently of :class:`DashboardIngestFrontier` and
    guards the transcript store's own schema/cursor tables.
    """

    def __init__(self, app_dir: pathlib.Path):
        self.app_dir = pathlib.Path(app_dir)
        self._states: dict[str, _ProviderState] = {}
        self.last_seed_failure: dict[str, str] = {}
        self._fallback_count = 0

    def memory_stats(self):
        return _frontier_memory_stats(self)

    def capture_cutoff(self) -> "MarkerCutoff | _MarkerCutoffFailure":
        # Same writer-serialized boundary as the accounting frontier; keeping
        # the implementation single-sourced prevents the two readers assigning
        # different meaning to the first ticket in a newly-created marker.
        return DashboardIngestFrontier(self.app_dir).capture_cutoff()

    def seed_provider(
        self, provider: str, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> bool:
        if provider not in _PROVIDERS:
            raise ValueError("unknown provider")
        if not trusted:
            self.last_seed_failure[provider] = "untrusted_hook_configuration"
            self._states.pop(provider, None)
            return False
        marker = activity_marker_path(self.app_dir)
        try:
            boundary = cutoff if cutoff is not None else self.capture_cutoff()
            if not isinstance(boundary, MarkerCutoff):
                raise ValueError("cutoff_capture_failed")
            st = marker.stat()
            marker_identity = (st.st_dev, st.st_ino)
            if marker_identity != boundary.identity or st.st_size < boundary.end:
                raise ValueError("activity_marker_changed")
            _validate_marker_prefix(marker, boundary)
            pending = _conversation_pending_identity(conn)
            if pending:
                raise ValueError("maintenance_pending")
            candidate = _ProviderState(
                marker_identity=marker_identity,
                marker_offset=boundary.end,
                db_identity=_database_identity(conn),
                schema_identity=_schema_identity(conn),
                pending_identity=pending,
                directory_identity=_conversation_directory_identity(
                    conn, provider, roots),
                guard_identity=_guard_identity(guard_paths),
                seeded_at=_now(),
            )
            if not _admit_frontier_state(self, provider, candidate):
                return False
            self.last_seed_failure[provider] = ""
            return True
        except (OSError, ValueError) as exc:
            self.last_seed_failure[provider] = f"{type(exc).__name__}:{exc}"
            self._states.pop(provider, None)
            return False

    def plan_provider(self, provider: str, conn, *, roots, guard_paths=()):
        state = self._states.get(provider)
        if state is None:
            return FrontierPlan(provider, "full", reason="unseeded")
        # Checked before every other guard: an expired certificate needs a full
        # walk whatever the remaining evidence says, and answering here keeps
        # the expiry itself free of database and filesystem work.
        if _now() - state.seeded_at >= FRONTIER_CERTIFICATE_MAX_AGE_SECONDS:
            return FrontierPlan(provider, "full", reason="certificate_expired")
        marker = activity_marker_path(self.app_dir)
        try:
            if _database_identity(conn) != state.db_identity:
                raise ValueError("database_replaced")
            if _schema_identity(conn) != state.schema_identity:
                raise ValueError("schema_changed")
            if _conversation_pending_identity(conn) != state.pending_identity:
                raise ValueError("maintenance_changed")
            if _guard_identity(guard_paths) != state.guard_identity:
                raise ValueError("hook_config_changed")
            if _restat_directory_identity(
                state.directory_identity
            ) != state.directory_identity:
                raise ValueError("filesystem_changed")
            marker_identity, marker_end, records = _read_marker(
                marker, state.marker_offset)
            if marker_identity != state.marker_identity:
                raise ValueError("marker_replaced")
        except (OSError, ValueError) as exc:
            return FrontierPlan(
                provider, "full", reason=str(exc) or "ambiguous")
        provider_paths = tuple(
            path for kind, path in records if kind == provider)
        if any(not path for path in provider_paths):
            return FrontierPlan(
                provider, "full", marker_end=marker_end,
                reason="ambiguous_activity")
        paths = frozenset(provider_paths)
        if any(not _target_in_roots(path, roots) for path in paths):
            return FrontierPlan(
                provider, "full", marker_end=marker_end,
                reason="target_outside_scope")
        target_risks = {
            _conversation_target_risk(conn, provider, path)
            for path in paths
        }
        if "source_replaced" in target_risks:
            return FrontierPlan(provider, "full", reason="source_replaced")
        if "cursor_gap" in target_risks:
            return FrontierPlan(provider, "full", reason="cursor_gap")
        return FrontierPlan(
            provider,
            "targeted" if paths else "caught_up",
            paths=paths,
            marker_end=marker_end,
            reason="activity" if paths else "unchanged",
        )

    def commit_provider(
        self, plan: FrontierPlan, conn, *, roots, guard_paths=(), trusted=True,
        cutoff: "MarkerCutoff | _MarkerCutoffFailure | None" = None,
    ) -> None:
        if plan.mode == "full":
            self.seed_provider(
                plan.provider, conn, roots=roots, guard_paths=guard_paths,
                trusted=trusted, cutoff=cutoff)
            return
        state = self._states.get(plan.provider)
        if state is None:
            return
        state.marker_offset = plan.marker_end
        if plan.mode == "caught_up":
            return
        state.db_identity = _database_identity(conn)
        state.schema_identity = _schema_identity(conn)
        state.pending_identity = _conversation_pending_identity(conn)
        state.directory_identity = _conversation_directory_identity(
            conn, plan.provider, roots)
        state.guard_identity = _guard_identity(guard_paths)
        _admit_frontier_state(self, plan.provider, state)
