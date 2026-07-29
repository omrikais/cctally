"""Scratch rederivation adapter for #372 Task B.

This eager sibling replays retained Claude observations and relevant operator
records through the current ingest hooks on a disposable stats index. Derived
events are captured in memory; the durable journal, source cache, config,
projection files, provider state, and alert dispatcher are never mutated.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import _cctally_core
import _cctally_journal as _journal
import _cctally_record as _record
import _lib_journal
import _lib_json_envelope
import _lib_rederive


class RederiveBusy(RuntimeError):
    """The stable-view/apply lock set could not be acquired in time."""


class RederiveApplyError(RuntimeError):
    """An operational apply stage failed after a concrete preview existed."""

    def __init__(self, stage, preview, batch_id, cause):
        super().__init__(f"{stage} failed: {cause}")
        self.stage = stage
        self.preview = preview
        self.batch_id = batch_id
        self.cause = cause


@dataclass(frozen=True)
class RederivePreview:
    plan: _lib_rederive.RederivePlan
    records: tuple[dict, ...]
    journal_high_water: "tuple[str, int] | None"
    record_ends: tuple[tuple[str, int], ...]
    generated_at: str
    batch_id: "str | None"
    incomplete_batch: bool
    latest_completed_batch: "str | None"
    latest_completed_high_water: "tuple[str, int] | None"
    recovery_required: bool
    # #374: the quarantined same-revision groups this plan will resolve by
    # forcing a revision advance. Additive; empty on a clean journal.
    journal_conflicts: tuple = ()


@dataclass(frozen=True)
class RederiveCommandResult:
    preview: RederivePreview
    status: str
    batch_id: "str | None"
    rebuild: "object | None"


_REDERIVE_LOCK_TIMEOUT_SECONDS = 5.0
_REDERIVE_CRASH_HOOK = None


def _canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _fingerprint(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _cache_contract(cache_conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = {
        row[0] for row in cache_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        name: {
            row[1] for row in cache_conn.execute(f"PRAGMA table_info({name})")
        }
        for name in names
    }


def _cache_fingerprint(cache_conn: sqlite3.Connection) -> str:
    """Hash every cost-bearing Claude cache row and its project metadata."""
    entry_columns = (
        "source_path", "line_offset", "timestamp_utc", "model", "input_tokens",
        "output_tokens", "cache_create_tokens", "cache_read_tokens",
        "cache_create_1h_tokens", "cost_usd_raw", "speed", "account_key",
    )
    file_columns = ("path", "session_id", "project_path")
    entries = [
        list(row) for row in cache_conn.execute(
            "SELECT " + ",".join(entry_columns)
            + " FROM session_entries ORDER BY source_path, line_offset")
    ]
    files = [
        list(row) for row in cache_conn.execute(
            "SELECT " + ",".join(file_columns)
            + " FROM session_files ORDER BY path")
    ]
    return _fingerprint({"sessionEntries": entries, "sessionFiles": files})


def _validate_cache_rows(cache_conn: sqlite3.Connection,
                         raw_records: list[dict]) -> None:
    missing_metadata = cache_conn.execute(
        "SELECT COUNT(*) FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE sf.path IS NULL"
    ).fetchone()[0]
    if missing_metadata:
        raise _lib_rederive.RederiveDataGap(
            "cache.db session_files metadata missing for "
            f"{missing_metadata} session_entries row(s)"
        )
    unknown_split = cache_conn.execute(
        "SELECT COUNT(*) FROM session_entries "
        "WHERE cache_create_tokens > 0 AND cache_create_1h_tokens IS NULL"
    ).fetchone()[0]
    if unknown_split:
        raise _lib_rederive.RederiveDataGap(
            "cache.db cache_create_1h_tokens missing for "
            f"{unknown_split} cache-write row(s)"
        )
    import _lib_accounts
    positive_accounts = {
        record.get("account") or _lib_accounts.UNATTRIBUTED
        for record in raw_records
        if record.get("t") == "obs"
        and record.get("provider") == "claude"
        and float((record.get("payload") or {}).get("weekly_percent") or 0) > 0
    }
    for account_key in sorted(positive_accounts):
        if account_key == _lib_accounts.UNATTRIBUTED:
            row = cache_conn.execute(
                "SELECT 1 FROM session_entries "
                "WHERE account_key IS NULL OR account_key = ? LIMIT 1",
                (_lib_accounts.UNATTRIBUTED,),
            ).fetchone()
        else:
            row = cache_conn.execute(
                "SELECT 1 FROM session_entries WHERE account_key = ? LIMIT 1",
                (account_key,),
            ).fetchone()
        if row is None:
            raise _lib_rederive.RederiveDataGap(
                "cache.db has no Claude session_entries for positive usage "
                f"account {account_key}"
            )


def _joined_entries(cache_conn, range_start, range_end, *,
                    project=None, account_key=None):
    cache = sys.modules["_cctally_cache"]
    start_iso = range_start.astimezone(dt.timezone.utc).isoformat()
    end_iso = range_end.astimezone(dt.timezone.utc).isoformat()
    sql = (
        "SELECT se.timestamp_utc, se.model, se.input_tokens, se.output_tokens, "
        "se.cache_create_tokens, se.cache_read_tokens, se.source_path, "
        "sf.session_id, sf.project_path, se.cost_usd_raw, se.speed, "
        "se.cache_create_1h_tokens "
        "FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path "
        "WHERE se.timestamp_utc >= ? AND se.timestamp_utc <= ?"
    )
    params = [start_iso, end_iso]
    if project is not None:
        escaped = (
            project.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        )
        sql += r" AND se.source_path LIKE ? ESCAPE '\'"
        params.append(f"%/projects/{escaped}/%")
    if account_key is not None:
        import _lib_accounts
        if account_key == _lib_accounts.UNATTRIBUTED:
            sql += " AND (se.account_key IS NULL OR se.account_key = ?)"
            params.append(_lib_accounts.UNATTRIBUTED)
        else:
            sql += " AND se.account_key = ?"
            params.append(account_key)
    sql += " ORDER BY se.timestamp_utc ASC, se.id ASC"
    out = []
    for row in cache_conn.execute(sql, params):
        out.append(cache._JoinedClaudeEntry(
            timestamp=dt.datetime.fromisoformat(row[0]),
            model=row[1],
            input_tokens=int(row[2] or 0),
            output_tokens=int(row[3] or 0),
            cache_creation_tokens=int(row[4] or 0),
            cache_read_tokens=int(row[5] or 0),
            source_path=row[6],
            session_id=row[7],
            project_path=row[8],
            cost_usd=row[9],
            usage_extra=({"speed": row[10]} if row[10] else None),
            cache_1h_tokens=(None if row[11] is None else int(row[11])),
        ))
    return out


@contextlib.contextmanager
def _scratch_read_adapters(cache_conn: sqlite3.Connection):
    """Temporarily route current cost/config readers to stable supplied inputs."""
    cctally = sys.modules["cctally"]
    cache = sys.modules["_cctally_cache"]
    replacements = {
        "get_entries": lambda start, end, *, project=None, skip_sync=False,
            account_key=None: cache.iter_entries(
                cache_conn, start, end, project=project, account_key=account_key),
        "get_claude_session_entries": (
            lambda start, end, *, project=None, skip_sync=False, account_key=None:
            _joined_entries(
                cache_conn, start, end, project=project, account_key=account_key)
        ),
        # No historical alert/budget config exists in the journal. The family
        # registry classifies those latches as re-materialized projections.
        "load_config": lambda *args, **kwargs: {},
    }
    prior = {name: getattr(cctally, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(cctally, name, value)
        yield
    finally:
        for name, value in prior.items():
            setattr(cctally, name, value)


def _rederivable_raw_records(records: list[dict]) -> list[dict]:
    out = []
    for record in records:
        if record.get("t") == "obs":
            if (
                record.get("provider") == "claude"
                and record.get("src") in _record._CLAUDE_OBS_SRCS
            ):
                out.append(record)
            continue
        if record.get("t") != "op":
            continue
        kind = (record.get("payload") or {}).get("kind")
        if kind in {"weekly_credit_floor", "account_observe",
                    "account_label", "accounts_cutover", "sync_week"}:
            out.append(record)
    return out


def _normalize_legacy_accounts(records: list[dict]) -> None:
    import _lib_accounts

    cutover_values = {
        (record.get("payload") or {}).get("claude_legacy_account")
        for record in records
        if record.get("t") == "op"
        and (record.get("payload") or {}).get("kind") == "accounts_cutover"
    }
    if None in cutover_values:
        raise _lib_rederive.RederiveConflict(
            "accounts_cutover is missing claude_legacy_account"
        )
    if len(cutover_values) > 1:
        raise _lib_rederive.RederiveConflict(
            "conflicting accounts_cutover Claude account values"
        )
    cutover_claude = (
        next(iter(cutover_values))
        if cutover_values
        else _lib_accounts.UNATTRIBUTED
    )
    for record in records:
        _journal._normalize_legacy_account_stamp(record, cutover_claude)


def _derive_desired_events(records: list[dict], cache_conn: sqlite3.Connection,
                           scratch_dir: Path) -> list[dict]:
    # #386: this replays the whole ingest pipeline into a PRIVATE scratch index
    # and is reached from `db rederive`'s PREVIEW, which takes no locks by
    # design (its contract is zero persistent writes to the live family). The
    # connection is authorizer-armed like every other `open_db` handle, so the
    # scratch replay has to declare itself sanctioned — otherwise a write-free
    # preview is denied for writing to its own temp file. The scope is entered
    # around the replay only; nothing here touches DB_PATH.
    import _cctally_store

    scratch_path = scratch_dir / "stats.rederive.db"
    with _cctally_store.stats_write_scope("rederive-derive"):
        return _derive_desired_events_into(
            records, cache_conn, str(scratch_path))


def _derive_desired_events_into(records, cache_conn, scratch_path) -> list[dict]:
    conn = _cctally_core.open_db(_target_path=str(scratch_path))
    events: list[dict] = []
    projection_state: dict = {}
    hooks = (
        _journal._pipeline_op_fold,
        _record._pipeline_claude_usage,
        _record._pipeline_record_credit,
        _record._pipeline_sync_week,
    )
    try:
        with _scratch_read_adapters(cache_conn):
            for record in _rederivable_raw_records(records):
                ctx = _journal.IngestContext(
                    conn=conn,
                    batch=[record],
                    config={},
                    event_sink=events,
                    projection_writes=False,
                    projection_state=projection_state,
                )
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for hook in hooks:
                        hook(ctx, record)
                    _journal._harvest(ctx)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        return events
    finally:
        conn.close()


def plan_claude_usage(
    records,
    *,
    cache_conn: sqlite3.Connection,
    journal_high_water: "tuple[str, int] | None",
    protocol_prefix_evidence=(),
):
    """Produce a deterministic Task-A-compatible plan without durable writes.

    ``records`` must be the canonical journal prefix ending at
    ``journal_high_water``. ``cache_conn`` must be a caller-held stable SQLite
    read view. Task C owns lock/snapshot orchestration; Task B owns the pure
    replay and comparison contract.
    """
    records = copy.deepcopy(list(records))
    _normalize_legacy_accounts(records)
    report = _lib_rederive.validate_family_registry(
        evt_kinds=set(_journal._EVT_SPECS),
        op_kinds=(
            set(_journal.FOLD_APPLIERS)
            | set(_journal._ACCOUNTS_MACHINERY_KINDS)
            | {"sync_week"}
        ),
    )
    if report.unclassified_evt_kinds or report.unclassified_op_kinds:
        raise _lib_rederive.RederiveConflict(
            "unclassified journal kind(s): evt="
            + ",".join(report.unclassified_evt_kinds)
            + " op=" + ",".join(report.unclassified_op_kinds)
        )
    _lib_rederive.validate_claude_cache_contract(_cache_contract(cache_conn))
    raw_records = _rederivable_raw_records(records)
    _validate_cache_rows(cache_conn, raw_records)
    cache_fingerprint = _cache_fingerprint(cache_conn)
    config_fingerprint = _fingerprint({
        "historicalConfig": "not-retained",
        "projectionPolicy": "retire-and-rematerialize",
    })
    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=protocol_prefix_evidence,
    )
    tainted = [
        *selection.protocol_violations,
        *selection.acknowledged_protocol_violations,
    ]
    if tainted:
        summary = ", ".join(
            f"{violation.batch_id}:{violation.kind}"
            for violation in tainted[:10]
        )
        raise _lib_rederive.RederiveConflict(
            "journal contains tainted correction batch(es): " + summary
        )
    with tempfile.TemporaryDirectory(prefix="cctally-rederive-") as tmp:
        desired = _derive_desired_events(records, cache_conn, Path(tmp))
    # #426: the scratch replay only sees RETAINED sources, so it can never
    # reproduce the pre-cutover rows the journal exported as `b:<table>:<rowid>`
    # evt lines. Hold them out of the diff instead of retiring them. Evidence is
    # counted in OBSERVATIONS alone — an operator record is replay input but
    # derives nothing on its own, and the cutover re-emits some of them
    # (`weekly_credit_floor`) carrying their original historical timestamp.
    preserved = _lib_rederive.preserved_history(
        records,
        evidence_retained=any(
            record.get("t") == "obs" for record in raw_records
        ),
    )
    return _lib_rederive.build_claude_usage_plan(
        selection=selection,
        desired_events=desired,
        journal_high_water=journal_high_water,
        cache_fingerprint=cache_fingerprint,
        config_fingerprint=config_fingerprint,
        preserved_events=preserved.values(),
        conflicted_event_ids=owned_conflicted_event_ids(selection),
    )


def owned_conflicted_event_ids(selection) -> frozenset:
    """The quarantined same-revision groups (#374) this family owns.

    Scoped two ways: the selector already filters `conflicts` to the WINNING
    revision (a group a completed rev-1 batch superseded is resolved, not
    outstanding), and this filters to events `claude-usage` re-derives — a
    conflict in a retained family (`quota_alert_arming`) or an unknown kind must
    force no action, because a correction this family cannot re-derive would be
    a fabrication."""
    owned = set()
    for conflict in getattr(selection, "conflicts", ()):
        selected = selection.by_id.get(conflict.event_id)
        if selected is None or selected.record is None:
            continue
        if _lib_rederive._is_owned_event(selected.record):
            owned.add(conflict.event_id)
    return frozenset(owned)


def read_rederive_journal_prefix(
    high_water: "tuple[str, int] | None" = None,
) -> tuple[list[dict], "tuple[str, int] | None", list[tuple[str, int]]]:
    """Read and strictly decode one canonical journal prefix."""
    if high_water is None:
        high_water = _journal.journal_high_water()
    if high_water is None:
        return [], None, []
    records: list[dict] = []
    record_ends: list[tuple[str, int]] = []
    malformed = 0
    for segment, offset, raw in _journal._read_range(None, high_water):
        record = _lib_journal.decode_line(raw)
        if record is None:
            malformed += 1
            continue
        records.append(record)
        record_ends.append((segment, offset + len(raw) + 1))
    if malformed:
        raise _lib_rederive.RederiveConflict(
            f"journal prefix contains {malformed} malformed line(s)"
        )
    return records, high_water, record_ends


def _protocol_prefix_evidence(records, record_ends):
    evidence = []
    prior_high_water = None
    for record, record_end in zip(records, record_ends):
        _journal._capture_protocol_prefix_evidence(
            record,
            prior_high_water,
            evidence,
        )
        prior_high_water = record_end
    return tuple(evidence)


def _read_only_journal_high_water() -> "tuple[str, int] | None":
    """Capture an append-only prefix without creating a coordination file."""
    segments = _journal.list_segments()
    if not segments:
        return None
    latest = segments[-1]
    return (latest, os.path.getsize(_cctally_core.JOURNAL_DIR / latest))


def _batch_id_for_plan(plan: _lib_rederive.RederivePlan) -> "str | None":
    if not plan.actions:
        return None
    body = {
        "family": plan.family,
        "actions": [
            action.to_correction_action() for action in plan.actions
        ],
    }
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return f"rederive:{plan.family}:{digest}"


def _iso_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@contextlib.contextmanager
def _open_sqlite_snapshot(path: Path, *, prefix: str):
    """Yield a query-only SQLite family copy without touching the source."""
    with tempfile.TemporaryDirectory(
        prefix=prefix
    ) as tmp:
        snapshot = Path(tmp) / path.name
        wal = path.with_name(path.name + "-wal")
        # Copy the append-only WAL prefix before the main file. If a checkpoint
        # races this read, a main-file identity change makes us retry rather
        # than combining generations. SQLite validates the copied WAL frames.
        for attempt in range(3):
            before = path.stat()
            snapshot_wal = snapshot.with_name(snapshot.name + "-wal")
            snapshot_wal.unlink(missing_ok=True)
            try:
                wal_size = wal.stat().st_size
                with wal.open("rb") as source, snapshot_wal.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.truncate(wal_size)
            except FileNotFoundError:
                snapshot_wal.unlink(missing_ok=True)
            shutil.copyfile(path, snapshot)
            after = path.stat()
            if (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) == (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                break
            if attempt == 2:
                raise RederiveBusy(
                    "cache.db changed during the read-only preview snapshot; "
                    "retry shortly"
                )
        conn = sqlite3.connect(snapshot)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()


@contextlib.contextmanager
def _open_cache_read_view():
    """Yield a stable cache snapshot without touching source WAL sidecars."""
    path = _cctally_core.CACHE_DB_PATH
    if not path.exists():
        raise _lib_rederive.RederiveDataGap(
            f"missing cache.db at {path}"
        )
    with _open_sqlite_snapshot(
        path,
        prefix="cctally-rederive-cache-",
    ) as conn:
        yield conn


def _latest_completed_family_batch(
    records: list[dict],
    record_ends: list[tuple[str, int]],
    completed: frozenset[str],
    family: str,
) -> tuple["str | None", "tuple[str, int] | None"]:
    latest_id = None
    latest_high_water = None
    for record, record_end in zip(records, record_ends):
        if (
            record.get("t") == "correction_batch"
            and record.get("phase") == "commit"
            and record.get("family") == family
            and record.get("id") in completed
        ):
            latest_id = record["id"]
            latest_high_water = record_end
    return latest_id, latest_high_water


def _preview_from_snapshot(
    family: str,
    *,
    journal_high_water: "tuple[str, int] | None" = None,
) -> RederivePreview:
    if family != _lib_rederive.FAMILY:
        raise _lib_rederive.RederiveConflict(
            f"unsupported rederive family: {family}"
        )
    if journal_high_water is None:
        journal_high_water = _read_only_journal_high_water()
    if journal_high_water is None:
        records, high_water, record_ends = [], None, []
    else:
        records, high_water, record_ends = read_rederive_journal_prefix(
            journal_high_water
        )
    protocol_evidence = _protocol_prefix_evidence(records, record_ends)
    with _open_cache_read_view() as cache:
        plan = plan_claude_usage(
            records,
            cache_conn=cache,
            journal_high_water=high_water,
            protocol_prefix_evidence=protocol_evidence,
        )

    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=protocol_evidence,
    )
    batch_id = _batch_id_for_plan(plan)
    generated_at = _iso_now()
    incomplete = False
    if batch_id is not None:
        begins = [
            record for record in records
            if record.get("t") == "correction_batch"
            and record.get("phase") == "begin"
            and record.get("id") == batch_id
        ]
        if begins:
            generated_at = str(begins[0]["at"])
            incomplete = batch_id not in selection.completed_batches
    latest_id, latest_high_water = _latest_completed_family_batch(
        records,
        record_ends,
        selection.completed_batches,
        family,
    )
    recovery_required = (
        latest_id is not None and not _stats_has_batch(latest_id)
    )
    owned_conflicts = owned_conflicted_event_ids(selection)
    journal_conflicts = tuple(
        conflict for conflict in selection.conflicts
        if conflict.event_id in owned_conflicts
    )
    return RederivePreview(
        journal_conflicts=journal_conflicts,
        plan=plan,
        records=tuple(records),
        journal_high_water=high_water,
        record_ends=tuple(record_ends),
        generated_at=generated_at,
        batch_id=batch_id,
        incomplete_batch=incomplete,
        latest_completed_batch=latest_id,
        latest_completed_high_water=latest_high_water,
        recovery_required=recovery_required,
    )


@contextlib.contextmanager
def _rederive_locks(*, apply: bool, timeout: float):
    """Acquire the stable-view locks in the repository's total order."""
    from _lib_cache_writer_lock import (
        acquire_ordered_flocks,
        release_cache_writer_flocks,
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    locks = [
        (
            _cctally_core.STATS_LOCK_MAINTENANCE_PATH,
            fcntl.LOCK_EX if apply else fcntl.LOCK_SH,
        ),
        (_cctally_core.CACHE_LOCK_MAINTENANCE_PATH, fcntl.LOCK_SH),
    ]
    if apply:
        locks.append((_cctally_core.JOURNAL_INGEST_LOCK_PATH, fcntl.LOCK_EX))
    locks.append((_cctally_core.CACHE_LOCK_PATH, fcntl.LOCK_EX))
    held = acquire_ordered_flocks(locks, timeout=timeout)
    if held is None:
        raise RederiveBusy(
            "another database sync or maintenance operation holds the "
            "rederive lock set; retry shortly"
        )
    # #386: record the stats maintenance hold so a nested live `open_db()` does
    # not request SHARED on a second fd of this same file and self-deadlock, and
    # (when applying) declare the sanctioned write regime + the ingest hold so a
    # heal reached from in here recognises itself as the serialized writer.
    import _cctally_store

    _cctally_core.note_stats_maintenance_acquired()
    try:
        with _cctally_store.stats_write_scope("rederive", ingest_lock=apply):
            yield
    finally:
        _cctally_core.note_stats_maintenance_released()
        release_cache_writer_flocks(held)


def preview_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
) -> RederivePreview:
    # Preview has a literal zero-persistent-write contract. A fixed append-only
    # journal prefix and a read-only SQLite transaction are stable inputs
    # without creating any coordination files.
    del lock_timeout
    return _preview_from_snapshot(family)


def _stats_has_batch(batch_id: str) -> bool:
    path = _cctally_core.DB_PATH
    if not path.exists():
        return False
    try:
        with _open_sqlite_snapshot(
            path,
            prefix="cctally-rederive-stats-",
        ) as conn:
            row = conn.execute(
                "SELECT 1 FROM journal_effective_events "
                "WHERE batch_id = ? LIMIT 1",
                (batch_id,),
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        return False


def _call_crash_hook(stage: str) -> None:
    if _REDERIVE_CRASH_HOOK is not None:
        _REDERIVE_CRASH_HOOK(stage)
    if (
        os.environ.get("CCTALLY_REDERIVE_TEST_MODE") == "1"
        and os.environ.get("CCTALLY_REDERIVE_TEST_CRASH_STAGE") == stage
    ):
        os.kill(os.getpid(), signal.SIGKILL)


def apply_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
) -> RederiveCommandResult:
    with _rederive_locks(apply=True, timeout=lock_timeout):
        preview = _preview_from_snapshot(family)
        plan = preview.plan
        recovering_prior = (
            preview.latest_completed_batch is not None
            and not _stats_has_batch(preview.latest_completed_batch)
        )
        if plan.actions:
            assert preview.batch_id is not None
            batch = _lib_journal.make_correction_batch(
                batch_id=preview.batch_id,
                family=family,
                at=preview.generated_at,
                actions=plan.to_correction_actions(),
            )
            try:
                batch_high_water = _journal.append_records(
                    batch,
                    expected_high_water=preview.journal_high_water,
                    line_hook=lambda index: _call_crash_hook(
                        f"after-batch-line-{index}"
                    ),
                )
            except Exception as exc:
                raise RederiveApplyError(
                    "correction append", preview, preview.batch_id, exc
                ) from exc
            try:
                _call_crash_hook("after-batch-commit")
                result = _journal.rebuild_stats_index(
                    high_water=batch_high_water,
                    update_quota_cache=False,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
                _call_crash_hook("after-rebuild")
            except Exception as exc:
                raise RederiveApplyError(
                    "stats rebuild", preview, preview.batch_id, exc
                ) from exc
            status = (
                "recovered"
                if preview.incomplete_batch or recovering_prior
                else "applied"
            )
            return RederiveCommandResult(
                preview=preview,
                status=status,
                batch_id=preview.batch_id,
                rebuild=result,
            )

        latest = preview.latest_completed_batch
        if latest is not None and not _stats_has_batch(latest):
            if preview.latest_completed_high_water is None:
                raise RederiveBusy(
                    f"completed correction batch {latest} has no commit high-water"
                )
            try:
                result = _journal.rebuild_stats_index(
                    high_water=preview.latest_completed_high_water,
                    update_quota_cache=False,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
                _call_crash_hook("after-rebuild")
            except Exception as exc:
                raise RederiveApplyError(
                    "stats recovery", preview, latest, exc
                ) from exc
            return RederiveCommandResult(
                preview=preview,
                status="recovered",
                batch_id=latest,
                rebuild=result,
            )
        return RederiveCommandResult(
            preview=preview,
            status="no-op",
            batch_id=latest,
            rebuild=None,
        )


def _high_water_dict(high_water):
    if high_water is None:
        return None
    return {"segment": high_water[0], "offset": high_water[1]}


def _rebuild_dict(result):
    if result is None:
        return None
    return {
        "segmentsRead": result.segments_read,
        "linesFolded": result.lines_folded,
        "malformed": result.malformed,
        "durationSeconds": round(result.duration_s, 3),
        "rowsByTable": result.rows_by_table,
    }


def _command_payload(
    *,
    status: str,
    preview: "RederivePreview | None" = None,
    batch_id: "str | None" = None,
    rebuild=None,
    conflicts=(),
    data_gaps=(),
    errors=(),
    family: str = _lib_rederive.FAMILY,
    journal_high_water=None,
):
    plan = None if preview is None else preview.plan
    counts = (
        {"retain": 0, "supersede": 0, "tombstone": 0, "add": 0}
        if plan is None else dict(plan.counts)
    )
    body = {
        "status": status,
        "family": family,
        "journalHighWater": (
            _high_water_dict(journal_high_water)
            if preview is None
            else _high_water_dict(preview.journal_high_water)
        ),
        "batchId": batch_id,
        "planHash": None if plan is None else plan.plan_hash,
        "actionCounts": counts,
        # #426: how many owned events the plan held OUT of the diff because no
        # retained source can re-derive them (pre-cutover exported history).
        "preservedEventCount": 0 if plan is None else plan.preserved_event_count,
        # `conflicts` is the LEGACY key and keeps its meaning: command-validation
        # failure messages (unsupported family, prod guard, structural journal
        # protocol errors). #374's quarantined same-revision GROUPS ride the new
        # `journalConflicts` key — never overload the old one.
        "conflicts": list(conflicts),
        "journalConflicts": [
            conflict.to_dict()
            for conflict in (() if preview is None else preview.journal_conflicts)
        ],
        "dataGaps": list(data_gaps),
        "errors": list(errors),
        "rebuild": _rebuild_dict(rebuild),
        "noOp": status == "no-op",
    }
    return _lib_json_envelope.stamp_schema_version(body, version=1)


def _emit_command_payload(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, separators=(",", ":")))
        return
    status = payload["status"]
    if status == "preview":
        actions = sum(
            payload["actionCounts"][name]
            for name in ("supersede", "tombstone", "add")
        )
        if actions == 0 and payload["batchId"] is not None:
            print(
                f"cctally: rederive preview for {payload['family']} — "
                f"completed batch {payload['batchId']} needs stats.db recovery; "
                "no changes written."
            )
        else:
            print(
                f"cctally: rederive preview for {payload['family']} — "
                f"{actions} correction action(s); no changes written."
            )
    elif status == "applied":
        print(
            f"cctally: applied {payload['family']} correction batch "
            f"{payload['batchId']} and rebuilt stats.db."
        )
    elif status == "recovered":
        print(
            f"cctally: recovered {payload['family']} correction batch "
            f"{payload['batchId']} and rebuilt stats.db."
        )
    elif status == "no-op":
        print(
            f"cctally: {payload['family']} is already current; "
            "no correction batch was appended."
        )


def cmd_db_rederive(args) -> int:
    """Preview or apply one audited Claude-usage correction plan."""
    family = str(getattr(args, "family", ""))
    as_json = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "yes", False))
    try:
        journal_high_water = _read_only_journal_high_water()
    except OSError as exc:
        payload = _command_payload(
            status="failed",
            family=family,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    if family != _lib_rederive.FAMILY:
        message = f"unsupported rederive family: {family}"
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=journal_high_water,
            conflicts=(message,),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive: {message}", file=sys.stderr)
        return 2
    if apply:
        import _cctally_db

        if _cctally_db._would_block_prod_stats(_cctally_core.DB_PATH):
            message = (
                "refusing to rederive the prod stats.db "
                "(~/.local/share/cctally) from a dev checkout; run the "
                "installed binary or set CCTALLY_ALLOW_PROD_MIGRATION=1"
            )
            payload = _command_payload(
                status="conflict",
                family=family,
                journal_high_water=journal_high_water,
                conflicts=(message,),
            )
            if as_json:
                _emit_command_payload(payload, as_json=True)
            else:
                print(f"cctally: db rederive: {message}", file=sys.stderr)
            return 2
    try:
        if apply:
            result = apply_db_rederive(family)
            payload = _command_payload(
                status=result.status,
                preview=result.preview,
                batch_id=result.batch_id,
                rebuild=result.rebuild,
            )
        else:
            preview = preview_db_rederive(family)
            status = (
                "preview"
                if preview.plan.actions or preview.recovery_required
                else "no-op"
            )
            payload = _command_payload(
                status=status,
                preview=preview,
                batch_id=(
                    preview.latest_completed_batch
                    if status == "no-op" or preview.recovery_required
                    else preview.batch_id
                ),
            )
    except _lib_rederive.RederiveDataGap as exc:
        payload = _command_payload(
            status="missing-source",
            family=family,
            journal_high_water=journal_high_water,
            data_gaps=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive missing source: {exc}", file=sys.stderr)
        return 2
    except (_lib_rederive.RederiveConflict,
            _lib_journal.JournalProtocolError) as exc:
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=journal_high_water,
            conflicts=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive conflict: {exc}", file=sys.stderr)
        return 2
    except RederiveApplyError as exc:
        payload = _command_payload(
            status="failed",
            preview=exc.preview,
            batch_id=exc.batch_id,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    except (RederiveBusy, _journal.JournalError, sqlite3.Error, OSError) as exc:
        payload = _command_payload(
            status="failed",
            family=family,
            journal_high_water=journal_high_water,
            errors=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive failed: {exc}", file=sys.stderr)
        return 3
    _emit_command_payload(payload, as_json=as_json)
    return 0
