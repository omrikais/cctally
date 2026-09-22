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
import _lib_journal_router
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
    reviewed_decision_id: "str | None" = None
    baseline_plan: "_lib_rederive.RederivePlan | None" = None
    baseline_plan_guard: "dict | None" = None


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
                    "account_label", "accounts_cutover", "sync_week",
                    "claude_weekly_observation_decision"}:
            out.append(record)
    return out


_REVIEWED_WEEKLY_KIND = "claude_weekly_observation_decision"
_REVIEWED_MANIFEST_KEYS_V1 = frozenset({
    "schemaVersion", "journalHighWater", "journalPrefixHash",
    "reviewedAt", "reason", "decisions",
})
_REVIEWED_MANIFEST_KEYS_V2 = frozenset({
    "schemaVersion", "journalHighWater", "journalPrefixHash",
    "reviewedAt", "reason", "weeklyAxisDecisions",
    "snapshotIdentityDecisions",
})
_REVIEWED_MANIFEST_EXPECTATION_KEYS = frozenset({
    "expectedBaselinePlanHash", "expectedDecisionPlanHash",
})
_REVIEWED_OP_EXPECTATION_KEYS = frozenset({
    "expected_baseline_plan_hash", "expected_decision_plan_hash",
})
_REVIEWED_OP_KEYS_V1 = frozenset({
    "kind", "schema_version", "journal_high_water",
    "journal_prefix_hash", "reason", "decisions",
}) | _REVIEWED_OP_EXPECTATION_KEYS
_REVIEWED_OP_KEYS_V2 = frozenset({
    "kind", "schema_version", "journal_high_water",
    "journal_prefix_hash", "reason", "weekly_axis_decisions",
    "snapshot_identity_decisions",
}) | _REVIEWED_OP_EXPECTATION_KEYS


def _reviewed_weekly_op_from_manifest(path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise _lib_rederive.RederiveConflict(
            f"invalid reviewed weekly decision manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has unexpected fields"
        )
    version = manifest.get("schemaVersion")
    expected_keys = (
        _REVIEWED_MANIFEST_KEYS_V1 if version == 1
        else _REVIEWED_MANIFEST_KEYS_V2 if version == 2
        else None
    )
    actual_keys = set(manifest)
    if (expected_keys is None
            or actual_keys not in (
                expected_keys,
                expected_keys | _REVIEWED_MANIFEST_EXPECTATION_KEYS,
            )):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest requires schemaVersion 1 or 2"
        )
    for key in _REVIEWED_MANIFEST_EXPECTATION_KEYS & actual_keys:
        value = manifest[key]
        if (not isinstance(value, str) or len(value) != 71
                or not value.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in value[7:])):
            raise _lib_rederive.RederiveConflict(
                f"reviewed weekly decision manifest has invalid {key}"
            )
    hw = manifest["journalHighWater"]
    if (not isinstance(hw, dict) or set(hw) != {"segment", "offset"}
            or not isinstance(hw["segment"], str) or not hw["segment"]
            or type(hw["offset"]) is not int or hw["offset"] < 0):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has invalid journalHighWater"
        )
    digest = manifest["journalPrefixHash"]
    if (not isinstance(digest, str) or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has invalid journalPrefixHash"
        )
    reviewed_at = manifest["reviewedAt"]
    if not isinstance(reviewed_at, str):
        raise _lib_rederive.RederiveConflict("reviewedAt must be UTC ISO-Z")
    try:
        parsed = dt.datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _lib_rederive.RederiveConflict(
            "reviewedAt must be UTC ISO-Z"
        ) from exc
    if (parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0)
            or parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
            != reviewed_at):
        raise _lib_rederive.RederiveConflict("reviewedAt must be UTC ISO-Z")
    reason = manifest["reason"]
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision reason must be nonempty"
        )
    decisions = (
        manifest["decisions"] if version == 1
        else manifest["weeklyAxisDecisions"]
    )
    identity_decisions = (
        [] if version == 1 else manifest["snapshotIdentityDecisions"]
    )
    if (not isinstance(decisions, list)
            or not isinstance(identity_decisions, list)
            or not decisions and not identity_decisions):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decisions must select at least one decision"
        )
    seen = set()
    normalized = []
    for item in decisions:
        if (not isinstance(item, dict)
                or set(item) != {"observationId", "disposition"}
                or not isinstance(item["observationId"], str)
                or not item["observationId"]
                or item["disposition"] not in ("hold", "accept")):
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision entry is malformed"
            )
        obs_id = item["observationId"]
        if obs_id in seen:
            raise _lib_rederive.RederiveConflict(
                f"duplicate reviewed weekly observation: {obs_id}"
            )
        seen.add(obs_id)
        normalized.append({
            "observationId": obs_id,
            "disposition": item["disposition"],
        })
    identity_ids = set()
    normalized_identity = []
    for item in identity_decisions:
        if (not isinstance(item, dict)
                or set(item) != {
                    "acceptedObservationId", "replayObservationId",
                    "disposition",
                }
                or not all(
                    isinstance(item[key], str) and item[key]
                    for key in (
                        "acceptedObservationId", "replayObservationId",
                    )
                )
                or item["disposition"] not in ("preserve", "rederive")
                or item["acceptedObservationId"] == item["replayObservationId"]
                or item["acceptedObservationId"] in identity_ids
                or item["replayObservationId"] in identity_ids):
            raise _lib_rederive.RederiveConflict(
                "snapshot identity decision is malformed, repeated, or ambiguous"
            )
        identity_ids.update((
            item["acceptedObservationId"], item["replayObservationId"],
        ))
        normalized_identity.append(dict(item))
    payload = {
        "kind": _REVIEWED_WEEKLY_KIND,
        "schema_version": version,
        "journal_high_water": dict(hw),
        "journal_prefix_hash": digest,
        "reason": reason,
    }
    if version == 1:
        payload["decisions"] = sorted(
            normalized, key=lambda item: item["observationId"])
    else:
        payload["weekly_axis_decisions"] = sorted(
            normalized, key=lambda item: item["observationId"])
        payload["snapshot_identity_decisions"] = sorted(
            normalized_identity,
            key=lambda item: (
                item["acceptedObservationId"], item["replayObservationId"],
            ),
        )
    if _REVIEWED_MANIFEST_EXPECTATION_KEYS <= actual_keys:
        payload.update({
            "expected_baseline_plan_hash": (
                manifest["expectedBaselinePlanHash"]
            ),
            "expected_decision_plan_hash": (
                manifest["expectedDecisionPlanHash"]
            ),
        })
    op = _lib_journal.make_op(
        at=reviewed_at,
        src="rederive",
        payload=payload,
    )
    if len(_lib_journal.encode_line(op)) > _journal._MAX_LINE_BYTES:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision exceeds journal line limit"
        )
    return op


def _reviewed_weekly_expected_hashes(path, *, required: bool):
    """Read plan pins that a successful apply also writes into its op."""
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise _lib_rederive.RederiveConflict(
            f"invalid reviewed weekly decision manifest: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision manifest has unexpected fields"
        )
    version = manifest.get("schemaVersion")
    if version not in (1, 2):
        return None
    present = _REVIEWED_MANIFEST_EXPECTATION_KEYS & set(manifest)
    if not present:
        if required:
            raise _lib_rederive.RederiveConflict(
                f"schemaVersion {version} apply requires "
                "expectedBaselinePlanHash and expectedDecisionPlanHash "
                "from a reviewed preview"
            )
        return None
    if present != _REVIEWED_MANIFEST_EXPECTATION_KEYS:
        raise _lib_rederive.RederiveConflict(
            f"schemaVersion {version} apply requires both reviewed plan hashes"
        )
    for key in present:
        value = manifest[key]
        if (not isinstance(value, str) or len(value) != 71
                or not value.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in value[7:])):
            raise _lib_rederive.RederiveConflict(
                f"reviewed weekly decision manifest has invalid {key}"
            )
    return (
        manifest["expectedBaselinePlanHash"],
        manifest["expectedDecisionPlanHash"],
    )


def _reviewed_op_expected_hashes(record, *, required: bool):
    payload = record.get("payload") or {}
    present = _REVIEWED_OP_EXPECTATION_KEYS & set(payload)
    if not present:
        if required:
            raise _lib_rederive.RederiveConflict(
                "retained reviewed weekly decision lacks durable plan hashes"
            )
        return None
    if present != _REVIEWED_OP_EXPECTATION_KEYS:
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision has incomplete plan hashes"
        )
    values = (
        payload["expected_baseline_plan_hash"],
        payload["expected_decision_plan_hash"],
    )
    if any(
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in value[7:])
        for value in values
    ):
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision has invalid plan hashes"
        )
    return values


def _reviewed_weekly_state(records):
    raw_ids = {
        record.get("id") for record in records
        if record.get("t") == "obs"
        and record.get("provider") == "claude"
        and record.get("src") in _record._CLAUDE_OBS_SRCS
        and "weekly_percent" in (record.get("payload") or {})
        and "resets_at" in (record.get("payload") or {})
    }
    effective = {}
    ops = []
    for record in records:
        if (record.get("t") != "op" or
                (record.get("payload") or {}).get("kind") != _REVIEWED_WEEKLY_KIND):
            continue
        payload = record["payload"]
        version = payload.get("schema_version")
        decisions = (
            payload.get("decisions") if version == 1
            else payload.get("weekly_axis_decisions") if version == 2
            else None
        )
        identity_decisions = (
            [] if version == 1 else payload.get("snapshot_identity_decisions")
        )
        if (not isinstance(decisions, list)
                or not isinstance(identity_decisions, list)
                or not decisions and not identity_decisions):
            raise _lib_rederive.RederiveConflict(
                "malformed retained reviewed weekly decision op"
            )
        seen = set()
        for item in decisions:
            if (not isinstance(item, dict)
                    or set(item) != {"observationId", "disposition"}
                    or not isinstance(item["observationId"], str)
                    or item["disposition"] not in ("hold", "accept")):
                raise _lib_rederive.RederiveConflict(
                    "malformed retained reviewed weekly decision entry"
                )
            obs_id = item["observationId"]
            if obs_id not in raw_ids or obs_id in seen:
                raise _lib_rederive.RederiveConflict(
                    f"unknown or duplicate reviewed weekly observation: {obs_id}"
                )
            seen.add(obs_id)
            effective[obs_id] = item["disposition"]
        identity_ids = set()
        for item in identity_decisions:
            if (not isinstance(item, dict)
                    or set(item) != {
                        "acceptedObservationId", "replayObservationId",
                        "disposition",
                    }
                    or not all(
                        isinstance(item.get(key), str) and item[key]
                        for key in (
                            "acceptedObservationId", "replayObservationId",
                        )
                    )
                    or item.get("disposition") not in (
                        "preserve", "rederive",
                    )
                    or item["acceptedObservationId"]
                    == item["replayObservationId"]
                    or item["acceptedObservationId"] in identity_ids
                    or item["replayObservationId"] in identity_ids):
                raise _lib_rederive.RederiveConflict(
                    "malformed retained snapshot identity decision"
                )
            identity_ids.update((
                item["acceptedObservationId"], item["replayObservationId"],
            ))
        ops.append(record)
    identity_effective = {}
    for op in ops:
        payload = op["payload"]
        for item in payload.get("snapshot_identity_decisions", []):
            identity_effective[
                (item["acceptedObservationId"], item["replayObservationId"])
            ] = item["disposition"]
    return (frozenset(
        obs_id for obs_id, disposition in effective.items()
        if disposition == "hold"
    ), frozenset(
        obs_id for obs_id, disposition in effective.items()
        if disposition == "accept"
    ), tuple(
        pair for pair, disposition in identity_effective.items()
        if disposition == "preserve"
    ), ops)


def _validate_retained_reviewed_op(record, prior_high_water, hasher) -> None:
    """Verify an operator decision against the bytes before its journal line."""
    payload = record.get("payload")
    if (not isinstance(payload, dict)
            or payload.get("kind") != _REVIEWED_WEEKLY_KIND):
        return
    bound = payload.get("journal_high_water")
    version = payload.get("schema_version")
    expected_keys = (
        _REVIEWED_OP_KEYS_V1 if version == 1
        else _REVIEWED_OP_KEYS_V2 if version == 2
        else None
    )
    if (expected_keys is None or set(payload) != expected_keys
            or not isinstance(bound, dict)
            or set(bound) != {"segment", "offset"}
            or type(bound.get("segment")) is not str
            or type(bound.get("offset")) is not int
            or not isinstance(payload.get("journal_prefix_hash"), str)
            or not isinstance(payload.get("reason"), str)
            or not payload["reason"].strip()
            or record != _lib_journal.make_op(
                at=record.get("at"), src=record.get("src"), payload=payload,
            )):
        raise _lib_rederive.RederiveConflict(
            "malformed retained reviewed weekly decision op"
        )
    _reviewed_op_expected_hashes(record, required=True)
    expected = (bound["segment"], bound["offset"])
    if prior_high_water != expected or hasher.digest_at(expected) != (
        payload["journal_prefix_hash"]
    ):
        raise _lib_rederive.RederiveConflict(
            "retained reviewed weekly decision prefix does not match its "
            "preceding journal bytes"
        )


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
                           scratch_dir: Path, *,
                           held_weekly_observation_ids=frozenset()) -> list[dict]:
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
            records, cache_conn, str(scratch_path),
            held_weekly_observation_ids=held_weekly_observation_ids)


def _derive_desired_events_into(
    records, cache_conn, scratch_path, *,
    held_weekly_observation_ids=frozenset(),
) -> list[dict]:
    raw_records = _rederivable_raw_records(records)
    held_ids = frozenset(held_weekly_observation_ids)
    valid_ids = {
        record.get("id") for record in raw_records
        if record.get("t") == "obs"
        and "weekly_percent" in (record.get("payload") or {})
        and "resets_at" in (record.get("payload") or {})
    }
    unknown_ids = held_ids - valid_ids
    if unknown_ids:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly hold names no Claude raw observation: "
            + ", ".join(sorted(unknown_ids))
        )
    conn = _cctally_core.open_db(_target_path=str(scratch_path))
    events: list[dict] = []
    reviewed_weekly_basis_by_account = {}
    hooks = (
        _journal._pipeline_op_fold,
        _record._pipeline_claude_usage,
        _record._pipeline_record_credit,
        _record._pipeline_sync_week,
    )
    try:
        with _scratch_read_adapters(cache_conn):
            for record in raw_records:
                ctx = _journal.IngestContext(
                    conn=conn,
                    batch=[record],
                    config={},
                    event_sink=events,
                    projection_writes=False,
                    held_weekly_observation_ids=held_ids,
                    reviewed_weekly_basis_by_account=(
                        reviewed_weekly_basis_by_account),
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


def _planner_op_kinds() -> "set[str]":
    """Every `op` kind the planner validates against `_OP_CLASSIFICATIONS`.

    One definition, so a test can assert what the planner ACTUALLY validates
    rather than re-deriving the same union beside it (review round 2, finding
    R2-6). A test that rebuilds the expression locally is a membership check
    against a set it constructed itself: it passes whatever this module does.

    `sync_week` is named literally because it is journaled as an `op` with no
    fold applier and no accounts machinery entry, so neither registry carries
    it, and an unclassified op kind raises `RederiveConflict`.
    """
    return (
        set(_journal.FOLD_APPLIERS)
        | set(_journal._ACCOUNTS_MACHINERY_KINDS)
        | {"sync_week"}
    )


def plan_claude_usage(
    records,
    *,
    cache_conn: sqlite3.Connection,
    journal_high_water: "tuple[str, int] | None",
    protocol_prefix_evidence=(),
    enforce_guard=True,
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
        op_kinds=_planner_op_kinds(),
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
    held_ids, _accepted_ids, identity_pairs, reviewed_ops = (
        _reviewed_weekly_state(records)
    )
    config_basis = {
        "historicalConfig": "not-retained",
        "projectionPolicy": "retire-and-rematerialize",
    }
    if reviewed_ops:
        config_basis["reviewedWeeklyDecisionOps"] = []
        for op in reviewed_ops:
            # Durable plan pins authenticate this derived plan. Feeding them
            # back into its own config fingerprint would make the plan hash
            # self-referential, so planning uses the decision's pin-free
            # logical identity while the complete journal record remains
            # content-addressed and validates the pins on recovery.
            payload = {
                key: value for key, value in op["payload"].items()
                if key not in _REVIEWED_OP_EXPECTATION_KEYS
            }
            logical = _lib_journal.make_op(
                at=op["at"], src=op["src"], payload=payload,
            )
            config_basis["reviewedWeeklyDecisionOps"].append({
                "id": logical["id"], "at": logical["at"],
                "payload": logical["payload"],
            })
    config_fingerprint = _fingerprint(config_basis)
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
        desired = _derive_desired_events(
            records, cache_conn, Path(tmp),
            held_weekly_observation_ids=held_ids,
        )
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
        raw_observations=raw_records,
        snapshot_identity_decisions=identity_pairs,
        reviewed_weekly_hold_ids=held_ids,
        enforce_guard=enforce_guard,
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
):
    """Stream and strictly decode one canonical journal prefix.

    Returns `(records, high_water, record_ends, protocol_prefix_evidence)`.
    The evidence digests come from a `PrefixHashAccumulator` fed by the bytes
    this pass is already reading. Before this, the prefix was materialized as
    raw lines, materialized again as decoded records while the first form was
    still referenced, walked a third time to produce the evidence, and re-read
    from byte zero by `journal_prefix_hash` once per
    `journal_protocol_resolution` op (#496 S5 §4).

    Retention is DELIBERATELY unfiltered, unlike the rebuild's. The rebuild
    keeps only the decision records and substitutes `None` placeholders for
    everything else; the planner here reads every observation for cache
    validation, desired-event derivation and preservation decisions, and walks
    `records` in parallel with `record_ends`. Both lists stay complete and
    aligned, so the win in this file is the removed double materialization and
    the removed hash traversals, not reduced retention.
    """
    if high_water is None:
        high_water = _journal.journal_high_water()
    if high_water is None:
        return [], None, [], ()
    segments = _journal.list_segments()
    if high_water[0] not in segments:
        raise OSError(
            f"journal high-water segment is unavailable: {high_water[0]}"
        )
    records: list[dict] = []
    record_ends: list[tuple[str, int]] = []
    evidence: list = []
    malformed = 0
    prior_high_water = None
    hasher = _lib_journal_router.PrefixHashAccumulator()
    for segment, offset, raw in _journal._iter_range_with_segments(
        None,
        high_water,
        segments,
        on_segment=lambda name: hasher.begin_segment(name, prior_high_water),
        on_bytes=hasher.extend,
    ):
        record = _lib_journal.decode_line(raw)
        record_end = (segment, offset + len(raw) + 1)
        if record is None:
            malformed += 1
            prior_high_water = record_end
            continue
        if record.get("t") == "op":
            _validate_retained_reviewed_op(
                record, prior_high_water, hasher,
            )
            _journal._capture_protocol_prefix_evidence(
                record,
                prior_high_water,
                evidence,
                hasher=hasher,
            )
        records.append(record)
        record_ends.append(record_end)
        prior_high_water = record_end
    # Released before the raise below, so a malformed prefix does not pin the
    # accumulator's buffered segment — 410 MB on the maintainer's journal — on
    # the traceback while the exception unwinds. On the success path the frame
    # dies two statements later, so this mirrors the repair reader, where the
    # release genuinely precedes a loop over every decoded record.
    hasher = None
    if malformed:
        raise _lib_rederive.RederiveConflict(
            f"journal prefix contains {malformed} malformed line(s)"
        )
    return records, high_water, record_ends, tuple(evidence)


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
    reviewed_op: "dict | None" = None,
) -> RederivePreview:
    if family != _lib_rederive.FAMILY:
        raise _lib_rederive.RederiveConflict(
            f"unsupported rederive family: {family}"
        )
    if journal_high_water is None:
        journal_high_water = _read_only_journal_high_water()
    if journal_high_water is None:
        records, high_water, record_ends, protocol_evidence = [], None, [], ()
    else:
        records, high_water, record_ends, protocol_evidence = (
            read_rederive_journal_prefix(journal_high_water)
        )
    planning_records = records
    baseline_records = None
    decision_op = reviewed_op
    decision_from_journal = False
    decision_batch_completed = False
    retained_decision = None
    retained_decision_index = None
    if reviewed_op is not None:
        bound = reviewed_op["payload"]["journal_high_water"]
        expected = (bound["segment"], bound["offset"])
        if high_water != expected:
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision journalHighWater drifted"
            )
        if _journal.journal_prefix_hash(high_water) != (
            reviewed_op["payload"]["journal_prefix_hash"]
        ):
            raise _lib_rederive.RederiveConflict(
                "reviewed weekly decision journalPrefixHash drifted"
            )
        planning_records = [*records, reviewed_op]
        baseline_records = records
    else:
        decision_indexes = [
            index for index, record in enumerate(records)
            if record.get("t") == "op"
            and (record.get("payload") or {}).get("kind")
            == _REVIEWED_WEEKLY_KIND
        ]
        if decision_indexes:
            index = decision_indexes[-1]
            retained_decision = records[index]
            retained_decision_index = index
    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=protocol_evidence,
    )
    if retained_decision is not None:
        suffix = records[retained_decision_index + 1:]
        first = suffix[0] if suffix else None
        completed_decision_batch = (
            first is not None
            and first.get("t") == "correction_batch"
            and first.get("phase") == "begin"
            and first.get("family") == family
            and first.get("id") in selection.completed_batches
        )
        correction_only_suffix = all(
            record.get("t") in {"correction", "correction_batch"}
            for record in suffix
        )
        if correction_only_suffix:
            decision_op = retained_decision
            decision_from_journal = not completed_decision_batch
            decision_batch_completed = completed_decision_batch
            baseline_records = [
                *records[:retained_decision_index],
                *records[retained_decision_index + 1:],
            ]
        elif not completed_decision_batch:
            raise _lib_rederive.RederiveConflict(
                "unfinished reviewed weekly decision has new journal records "
                "after it; review a fresh manifest"
            )
    current_events = {
        event_id: selected.record
        for event_id, selected in selection.by_id.items()
        if selected.status == "active" and selected.record is not None
    }
    plan_high_water = high_water
    if decision_op is not None and not decision_batch_completed:
        bound = decision_op["payload"]["journal_high_water"]
        plan_high_water = (bound["segment"], bound["offset"])
    baseline_plan = None
    baseline_plan_guard = None
    with _open_cache_read_view() as cache:
        if decision_op is None:
            plan = plan_claude_usage(
                planning_records,
                cache_conn=cache,
                journal_high_water=high_water,
                protocol_prefix_evidence=protocol_evidence,
            )
        else:
            baseline_plan = plan_claude_usage(
                baseline_records,
                cache_conn=cache,
                journal_high_water=plan_high_water,
                protocol_prefix_evidence=protocol_evidence,
                enforce_guard=False,
            )
            baseline_plan_guard = _lib_rederive.week_reset_add_burst_guard(
                baseline_plan)
            reviewed_plan = plan_claude_usage(
                planning_records,
                cache_conn=cache,
                journal_high_water=plan_high_water,
                protocol_prefix_evidence=protocol_evidence,
                enforce_guard=False,
            )
            _held_ids, accepted_ids, _identity_pairs, _reviewed_ops = (
                _reviewed_weekly_state(planning_records)
            )
            plan = _lib_rederive.causal_delta_plan(
                baseline_plan, reviewed_plan,
                current_events=current_events,
                authorized_week_reset_origins=accepted_ids,
            )
            _lib_rederive.enforce_week_reset_add_burst_guard(plan)

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
    preview = RederivePreview(
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
        reviewed_decision_id=(
            None if decision_op is None else decision_op["id"]),
        baseline_plan=baseline_plan,
        baseline_plan_guard=baseline_plan_guard,
    )
    if decision_op is not None and not decision_batch_completed:
        expected_hashes = _reviewed_op_expected_hashes(
            decision_op, required=decision_from_journal,
        )
        if expected_hashes is not None:
            _assert_reviewed_plan_hashes(preview, expected_hashes)
    return preview


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


# --------------------------------------------------------------------------
# #500 §8/§8.1 — the operator-attribution apply lock set
# --------------------------------------------------------------------------
#
# `_rederive_locks` above yields no handles and releases its whole set together,
# so there is no way to drop only the two cache flocks at the end of the cache
# transaction while retaining stats maintenance and the ingest lock for the
# stats transaction that follows. `account attribute` needs exactly that: its
# cache work must be committed and unlocked before the stats transaction begins,
# which is the repository law that all cache work precedes the stats transaction
# (`docs/journal-gotchas.md`), while the ingest lock has to be held ACROSS both
# so nothing can consume the appended prefix in between.
#
# ORDERED PARTIAL RELEASE is the whole mechanism: the owner below releases a
# SUFFIX of the acquired set in reverse acquisition order and nothing else. A
# release that skipped an inner lock, or that took them in acquisition order,
# would be the lock-order violation this exists to avoid rather than a
# convenience on top of it.


class _OrderedApplyLockOwner:
    """A held ordered flock set that can release a suffix of itself."""

    __slots__ = ("_paths", "_held", "_cache_flock_count", "_cache_flocks_noted")

    def __init__(self, paths, held, *, cache_flock_count: int) -> None:
        self._paths = tuple(paths)
        self._held = list(held)
        self._cache_flock_count = int(cache_flock_count)
        self._cache_flocks_noted = True
        _cctally_core.note_attribution_apply_cache_flocks_acquired()

    @property
    def held_paths(self) -> tuple:
        return tuple(self._paths[:len(self._held)])

    def _note_cache_flocks_released(self) -> None:
        if self._cache_flocks_noted:
            self._cache_flocks_noted = False
            _cctally_core.note_attribution_apply_cache_flocks_released()

    def release_cache_flocks(self) -> None:
        """Drop the global cache writer flock and the Codex provider flock.

        Idempotent, because the apply sequence releases them at the end of its
        cache transaction and the context manager's ``finally`` releases
        whatever is left — the second call must not close a descriptor twice.

        The release is RECORDED as well as performed, and that is what turns the
        repository lock-order law into something the code enforces:
        `_run_stats_ingest_once(locks_held=True)` refuses while this context
        still holds them, so a caller that forgets this call gets a loud refusal
        instead of a stats transaction opened underneath live cache flocks.
        """
        from _lib_cache_writer_lock import release_cache_writer_flocks

        if len(self._held) <= len(self._paths) - self._cache_flock_count:
            self._note_cache_flocks_released()
            return
        keep = len(self._paths) - self._cache_flock_count
        # `release_cache_writer_flocks` releases the list it is given in REVERSE
        # order, which is what makes this a suffix release rather than an
        # arbitrary one.
        release_cache_writer_flocks(self._held[keep:])
        del self._held[keep:]
        self._note_cache_flocks_released()

    def release_all(self) -> None:
        from _lib_cache_writer_lock import release_cache_writer_flocks

        release_cache_writer_flocks(self._held)
        self._held.clear()
        self._note_cache_flocks_released()


@contextlib.contextmanager
def codex_attribution_apply_locks(*, timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS):
    """Acquire the #500 §8 apply order and yield an ordered-partial-release owner.

    The order is ``_rederive_locks``' applying order extended by the Codex
    provider flock, exactly as the spec states:

    1. stats maintenance, exclusive
    2. cache maintenance, shared
    3. ``journal.ingest.lock``, exclusive
    4. global ``cache.db.lock``, exclusive
    5. the Codex provider flock, exclusive

    ``owner.release_cache_flocks()`` drops 5 then 4 and retains 1-3, so the
    stats transaction runs with the ingest lock still held while every cache
    writer is free again.

    Do not ``os.fork()`` and do not start a thread inside this block. The
    lock-order guard behind ``release_cache_flocks()`` is a ContextVar counter,
    so a child context starts from a COPY of this one and never observes the
    later release — a forked or spawned worker would either refuse a stats
    ingest that is legitimately unblocked, or, if it were created before the
    acquisition, run one while the cache flocks are still held. The flocks
    themselves are process-wide and unaffected; the guard is what is advisory
    across a context boundary.
    """
    from _lib_cache_writer_lock import acquire_ordered_flocks

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    locks = [
        (_cctally_core.STATS_LOCK_MAINTENANCE_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_MAINTENANCE_PATH, fcntl.LOCK_SH),
        (_cctally_core.JOURNAL_INGEST_LOCK_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_PATH, fcntl.LOCK_EX),
        (_cctally_core.CACHE_LOCK_CODEX_PATH, fcntl.LOCK_EX),
    ]
    held = acquire_ordered_flocks(locks, timeout=timeout)
    if held is None:
        raise RederiveBusy(
            "another database sync or maintenance operation holds the "
            "attribution apply lock set; retry shortly"
        )
    owner = _OrderedApplyLockOwner(
        [path for path, _mode in locks], held, cache_flock_count=2)
    # #386, the same declaration `_rederive_locks` makes: record the stats
    # maintenance hold so a nested live `open_db()` does not request SHARED on a
    # second fd of this same file and self-deadlock, and declare the sanctioned
    # write regime plus the ingest hold so a heal reached from in here
    # recognises itself as the serialized writer.
    import _cctally_store

    _cctally_core.note_stats_maintenance_acquired()
    try:
        with _cctally_store.stats_write_scope(
            "codex-window-attribution", ingest_lock=True,
        ):
            yield owner
    finally:
        _cctally_core.note_stats_maintenance_released()
        owner.release_all()


def preview_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
    reviewed_op: "dict | None" = None,
) -> RederivePreview:
    # Preview has a literal zero-persistent-write contract. A fixed append-only
    # journal prefix and a read-only SQLite transaction are stable inputs
    # without creating any coordination files.
    del lock_timeout
    return _preview_from_snapshot(family, reviewed_op=reviewed_op)


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


def _reviewed_op_is_exact_retry(preview: RederivePreview, op: dict) -> bool:
    bound = op["payload"]["journal_high_water"]
    expected = (bound["segment"], bound["offset"])
    if _journal.journal_prefix_hash(expected) != (
        op["payload"]["journal_prefix_hash"]
    ):
        return False
    matches = [
        index for index, record in enumerate(preview.records)
        if record.get("t") == "op" and record.get("id") == op["id"]
    ]
    if len(matches) != 1:
        return False
    index = matches[0]
    return preview.records[index] == op and (
        preview.record_ends[index - 1] if index else None
    ) == expected


def _reviewed_op_needs_materialization(preview: RederivePreview) -> bool:
    last_index = None
    for index, record in enumerate(preview.records):
        if (record.get("t") == "op" and
                (record.get("payload") or {}).get("kind") == _REVIEWED_WEEKLY_KIND):
            last_index = index
    if last_index is None:
        return False
    try:
        with _open_sqlite_snapshot(
            _cctally_core.DB_PATH, prefix="cctally-rederive-stats-",
        ) as conn:
            row = conn.execute(
                "SELECT segment, offset FROM journal_cursor WHERE id=1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        row = None
    if row is None:
        return True
    cursor = (str(row[0]), int(row[1]))
    if cursor == preview.journal_high_water:
        return False
    try:
        return preview.record_ends.index(cursor) < last_index
    except ValueError:
        return True


def _refuse_earlier_baseline_actions(
    family: str, preview: RederivePreview, reviewed_op: dict,
) -> None:
    """Keep pre-existing corrections outside the exact decision's history.

    A reviewed weekly decision can remove a reset-burst guard without causing
    older rederive drift. Such drift must be resolved separately before the
    reviewed operation is appended; it is not part of the operator's selected
    observations. The baseline plan may itself trip the burst guard, but that
    exception still carries its complete proposed action set.
    """
    selected_ids = {
        item["observationId"]
        for item in reviewed_op["payload"]["decisions"]
    }
    selected_at = [
        dt.datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
        for record in preview.records
        if record.get("t") == "obs" and record.get("id") in selected_ids
    ]
    if len(selected_at) != len(selected_ids):
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision has unknown selected observations"
        )
    first_selected_at = min(selected_at)
    try:
        baseline = _preview_from_snapshot(family)
        baseline_plan = baseline.plan
    except _lib_rederive.RederivePlanGuardConflict as exc:
        baseline_plan = exc.plan
    baseline_actions = {
        action.event_id: action.payload_hash
        for action in baseline_plan.actions
    }
    unchanged_earlier = [
        action for action in preview.plan.actions
        if action.disposition != "add"
        and baseline_actions.get(action.event_id) == action.payload_hash
        and dt.datetime.fromisoformat(action.at.replace("Z", "+00:00"))
        < first_selected_at
    ]
    if unchanged_earlier:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly correction contains pre-existing actions "
            "earlier than its selected observations; resolve baseline "
            "rederive drift first"
        )


def _assert_reviewed_plan_hashes(preview, expected_plan_hashes) -> None:
    if expected_plan_hashes is None:
        return
    if preview.baseline_plan is None:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision has no baseline plan to verify"
        )
    expected_baseline, expected_decision = expected_plan_hashes
    if preview.baseline_plan.plan_hash != expected_baseline:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly baseline plan hash drifted: expected "
            f"{expected_baseline}, got {preview.baseline_plan.plan_hash}"
        )
    if preview.plan.plan_hash != expected_decision:
        raise _lib_rederive.RederiveConflict(
            "reviewed weekly decision plan hash drifted: expected "
            f"{expected_decision}, got {preview.plan.plan_hash}"
        )


def apply_db_rederive(
    family: str,
    *,
    lock_timeout: float = _REDERIVE_LOCK_TIMEOUT_SECONDS,
    reviewed_op: "dict | None" = None,
    expected_plan_hashes: "tuple[str, str] | None" = None,
) -> RederiveCommandResult:
    with _rederive_locks(apply=True, timeout=lock_timeout):
        appended_reviewed_op = False
        if reviewed_op is not None:
            durable_hashes = _reviewed_op_expected_hashes(
                reviewed_op, required=True,
            )
            if expected_plan_hashes != durable_hashes:
                raise _lib_rederive.RederiveConflict(
                    "reviewed weekly apply hashes do not match its durable op"
                )
        if reviewed_op is None:
            preview = _preview_from_snapshot(family)
            report_preview = preview
        else:
            try:
                report_preview = _preview_from_snapshot(
                    family, reviewed_op=reviewed_op)
            except _lib_rederive.RederiveConflict as exc:
                if "journalHighWater drifted" not in str(exc):
                    raise
                preview = _preview_from_snapshot(family)
                if not _reviewed_op_is_exact_retry(preview, reviewed_op):
                    raise exc
                bound = reviewed_op["payload"]["journal_high_water"]
                original = _preview_from_snapshot(
                    family,
                    journal_high_water=(bound["segment"], bound["offset"]),
                    reviewed_op=reviewed_op,
                )
                op_index = next(
                    index for index, record in enumerate(preview.records)
                    if record.get("t") == "op"
                    and record.get("id") == reviewed_op["id"]
                )
                if any(
                    not (
                        (record.get("t") == "correction_batch"
                         and record.get("id") == original.batch_id)
                        or (record.get("t") == "correction"
                            and record.get("batch") == original.batch_id)
                    )
                    for record in preview.records[op_index + 1:]
                ):
                    raise _lib_rederive.RederiveConflict(
                        "reviewed weekly decision retry has new journal records "
                        "after its decision; review a fresh manifest"
                    )
                original_actions = original.plan.to_correction_actions()
                current_actions = preview.plan.to_correction_actions()
                if current_actions != original_actions and not (
                    not current_actions
                    and original.batch_id is not None
                    and preview.latest_completed_batch == original.batch_id
                ):
                    raise _lib_rederive.RederiveConflict(
                        "reviewed weekly decision retry changed correction "
                        "actions"
                    )
                report_preview = original
                _assert_reviewed_plan_hashes(
                    report_preview, expected_plan_hashes)
            else:
                _assert_reviewed_plan_hashes(
                    report_preview, expected_plan_hashes)
                try:
                    op_high_water = _journal.append_records(
                        [reviewed_op],
                        expected_high_water=report_preview.journal_high_water,
                    )
                except Exception as exc:
                    raise RederiveApplyError(
                        "reviewed decision append", report_preview,
                        report_preview.batch_id, exc,
                    ) from exc
                appended_reviewed_op = True
                _call_crash_hook("after-reviewed-decision-op")
                preview = _preview_from_snapshot(
                    family, journal_high_water=op_high_water)
                if (preview.plan.to_correction_actions()
                        != report_preview.plan.to_correction_actions()):
                    raise RederiveApplyError(
                        "reviewed decision revalidation", report_preview,
                        report_preview.batch_id,
                        "scratch actions changed after decision append",
                    )
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
                    context=_journal.RebuildContext(trigger="rederive-apply"),
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
                preview=report_preview,
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
                    context=_journal.RebuildContext(
                        trigger="rederive-recovery"
                    ),
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
                preview=report_preview,
                status="recovered",
                batch_id=latest,
                rebuild=result,
            )
        if appended_reviewed_op or _reviewed_op_needs_materialization(preview):
            try:
                result = _journal.rebuild_stats_index(
                    context=_journal.RebuildContext(trigger="rederive-recovery"),
                    high_water=preview.journal_high_water,
                    update_quota_cache=False,
                )
            except Exception as exc:
                raise RederiveApplyError(
                    "reviewed decision materialization", report_preview,
                    latest, exc,
                ) from exc
            return RederiveCommandResult(
                preview=report_preview,
                status="applied" if appended_reviewed_op else "recovered",
                batch_id=latest,
                rebuild=result,
            )
        return RederiveCommandResult(
            preview=report_preview,
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
    guarded_plan: "_lib_rederive.RederivePlan | None" = None,
    plan_guard=None,
    reviewed_decision_id=None,
):
    plan = guarded_plan if preview is None else preview.plan
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
        "actionCountsByEventKind": (
            {
                kind: {name: 0 for name in counts}
                for kind in sorted(_lib_rederive._EVT_CLASSIFICATIONS)
            }
            if plan is None else plan.action_counts_by_event_kind
        ),
        "planGuard": plan_guard,
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
    if reviewed_decision_id is not None:
        body["reviewedWeeklyDecisionId"] = reviewed_decision_id
    if preview is not None and preview.baseline_plan is not None:
        body["baselinePlanHash"] = preview.baseline_plan.plan_hash
        body["baselineActionCounts"] = dict(preview.baseline_plan.counts)
        body["baselinePlanGuard"] = preview.baseline_plan_guard
        body["decisionPlanHash"] = preview.plan.plan_hash
        body["decisionActionCounts"] = dict(preview.plan.counts)
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
        if payload["batchId"] is None:
            print(
                f"cctally: applied {payload['family']} reviewed decision "
                "and rebuilt stats.db."
            )
        else:
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
    if "baselinePlanHash" in payload:
        def _counts_text(counts):
            return ", ".join(
                f"{name}={counts[name]}"
                for name in ("retain", "supersede", "tombstone", "add")
            )
        print(f"baseline plan {payload['baselinePlanHash']}")
        print(
            "baseline action counts: "
            + _counts_text(payload["baselineActionCounts"])
        )
        if payload.get("baselinePlanGuard") is not None:
            print(
                "baseline plan guard: "
                + json.dumps(
                    payload["baselinePlanGuard"],
                    sort_keys=True, separators=(",", ":"),
                )
            )
        print(f"decision plan {payload['decisionPlanHash']}")
        print(
            "decision action counts: "
            + _counts_text(payload["decisionActionCounts"])
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
        reviewed_path = getattr(args, "reviewed_weekly_decisions", None)
        reviewed_op = (
            None if reviewed_path is None
            else _reviewed_weekly_op_from_manifest(reviewed_path)
        )
        expected_plan_hashes = (
            None if reviewed_path is None
            else _reviewed_weekly_expected_hashes(
                reviewed_path, required=apply,
            )
        )
        if apply:
            result = apply_db_rederive(
                family,
                reviewed_op=reviewed_op,
                expected_plan_hashes=expected_plan_hashes,
            )
            payload = _command_payload(
                status=result.status,
                preview=result.preview,
                batch_id=result.batch_id,
                rebuild=result.rebuild,
                reviewed_decision_id=(
                    None if reviewed_op is None else reviewed_op["id"]),
            )
        else:
            preview = preview_db_rederive(family, reviewed_op=reviewed_op)
            status = (
                "preview"
                if reviewed_op is not None or preview.plan.actions
                or preview.recovery_required
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
                reviewed_decision_id=preview.reviewed_decision_id,
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
    except _lib_rederive.RederivePlanGuardConflict as exc:
        payload = _command_payload(
            status="conflict",
            family=family,
            journal_high_water=exc.plan.journal_high_water,
            batch_id=_batch_id_for_plan(exc.plan),
            guarded_plan=exc.plan,
            plan_guard=exc.plan_guard,
            conflicts=(str(exc),),
        )
        if as_json:
            _emit_command_payload(payload, as_json=True)
        else:
            print(f"cctally: db rederive conflict: {exc}", file=sys.stderr)
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
