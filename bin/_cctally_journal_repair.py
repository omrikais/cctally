"""Preview-first audited repair for structural correction-batch violations."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import signal
import sqlite3
import sys

import _cctally_core
import _cctally_journal as _journal
import _lib_journal
from _lib_json_envelope import stamp_schema_version


def _read_only_high_water() -> "tuple[str, int] | None":
    """Capture the append-only prefix without creating a lock or sidecar."""
    segments = _journal.list_segments()
    if not segments:
        return None
    latest = segments[-1]
    return latest, os.path.getsize(_cctally_core.JOURNAL_DIR / latest)


def _read_prefix(high_water):
    if high_water is None:
        return [], ()
    records = []
    evidence = []
    malformed = 0
    prior_high_water = None
    for segment, offset, raw in _journal._read_range(None, high_water):
        record = _lib_journal.decode_line(raw)
        if record is None:
            malformed += 1
            prior_high_water = (segment, offset + len(raw) + 1)
            continue
        _journal._capture_protocol_prefix_evidence(
            record,
            prior_high_water,
            evidence,
        )
        records.append(record)
        prior_high_water = (segment, offset + len(raw) + 1)
    if malformed:
        raise _lib_journal.JournalProtocolError(
            f"journal prefix contains {malformed} malformed line(s)"
        )
    cutover_claude = _journal.resolve_cutover_claude_account()
    for record in records:
        _journal._normalize_legacy_account_stamp(record, cutover_claude)
    return records, tuple(evidence)


def _prefix_hash(high_water) -> "str | None":
    return _journal.journal_prefix_hash(high_water)


def _high_water_dict(high_water):
    if high_water is None:
        return None
    return {"segment": high_water[0], "offset": high_water[1]}


def _selection_snapshot():
    high_water = _read_only_high_water()
    records, evidence = _read_prefix(high_water)
    selection = _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=evidence,
    )
    return high_water, _prefix_hash(high_water), selection


def _preview_payload(requested=()):
    high_water, prefix_hash, selection = _selection_snapshot()
    unacknowledged = {
        violation.fingerprint: violation
        for violation in selection.protocol_violations
    }
    acknowledged = {
        violation.fingerprint: violation
        for violation in selection.acknowledged_protocol_violations
    }
    requested = list(requested)
    errors = []
    if len(set(requested)) != len(requested):
        errors.append("--violation fingerprints must not be repeated")
    unknown = sorted(set(requested) - set(unacknowledged) - set(acknowledged))
    if unknown:
        errors.append("unknown violation fingerprint(s): " + ", ".join(unknown))
    selected = []
    for fingerprint in requested:
        if fingerprint in unacknowledged:
            selected.append(unacknowledged[fingerprint].to_dict())
        elif fingerprint in acknowledged:
            selected.append(acknowledged[fingerprint].violation.to_dict())
    body = {
        "status": "preview",
        "journalHighWater": _high_water_dict(high_water),
        "journalPrefixHash": prefix_hash,
        "unacknowledgedViolations": [
            violation.to_dict() for violation in selection.protocol_violations
        ],
        "acknowledgedViolations": [
            violation.to_dict()
            for violation in selection.acknowledged_protocol_violations
        ],
        "selectedViolations": selected,
        "auditId": None,
        "rebuild": None,
        "errors": errors,
    }
    return stamp_schema_version(body, version=1), selection


def _rebuild_dict(result):
    if result is None:
        return None
    return {
        "segmentsRead": result.segments_read,
        "linesFolded": result.lines_folded,
        "malformed": result.malformed,
        "durationSeconds": round(result.duration_s, 3),
        "rowsByTable": result.rows_by_table,
        "journalConflicts": [
            conflict.to_dict() for conflict in result.conflicts
        ],
        "journalProtocolViolations": [
            violation.to_dict() for violation in result.protocol_violations
        ],
        "journalAcknowledgedProtocolViolations": [
            violation.to_dict()
            for violation in result.acknowledged_protocol_violations
        ],
    }


@contextlib.contextmanager
def _repair_locks(timeout: float = 5.0):
    """Take the repository's maintenance -> ingest lock prefix."""
    from _lib_cache_writer_lock import (
        acquire_ordered_flocks,
        release_cache_writer_flocks,
    )
    import _cctally_store

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    held = acquire_ordered_flocks(
        [
            (_cctally_core.STATS_LOCK_MAINTENANCE_PATH, fcntl.LOCK_EX),
            (_cctally_core.JOURNAL_INGEST_LOCK_PATH, fcntl.LOCK_EX),
        ],
        timeout=timeout,
    )
    if held is None:
        raise _journal.JournalError(
            "another database sync or maintenance operation holds the "
            "journal-repair lock set; retry shortly"
        )
    _cctally_core.note_stats_maintenance_acquired()
    try:
        with _cctally_store.stats_write_scope(
            "journal-repair", ingest_lock=True
        ):
            yield
    finally:
        _cctally_core.note_stats_maintenance_released()
        release_cache_writer_flocks(held)


def _iso_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _call_crash_hook(stage: str) -> None:
    if (
        os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_MODE") == "1"
        and os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_CRASH_STAGE") == stage
    ):
        os.kill(os.getpid(), signal.SIGKILL)


def _call_pause_hook(stage: str) -> None:
    """Private process-control seam for the #402 locked-revalidation test."""
    if (
        os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_MODE") != "1"
        or os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_PAUSE_STAGE") != stage
    ):
        return
    marker = os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_PAUSE_MARKER")
    if not marker:
        return
    pathlib.Path(marker).write_text(f"{os.getpid()}\n")
    os.kill(os.getpid(), signal.SIGSTOP)


def _call_rebuild_error_hook() -> None:
    if (
        os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_MODE") == "1"
        and os.environ.get("CCTALLY_JOURNAL_REPAIR_TEST_REBUILD_ERROR")
        == "sqlite"
    ):
        raise sqlite3.DatabaseError("injected scratch sqlite failure")


def _repair_failure_guidance(exc: Exception) -> str:
    detail = str(exc)
    lower = detail.lower()
    if (
        "open handle" in lower
        or "family is still open" in lower
        or "open in process" in lower
    ):
        return (
            f"{detail}; stop the dashboard or other process holding stats.db "
            "open, then rerun the identical db journal-repair command"
        )
    return detail


def _post_audit_failure(requested, audit_ids, exc):
    """Report durable acknowledgement truth when index publication declined."""
    payload, _selection = _preview_payload(requested)
    payload["status"] = "failed"
    payload["errors"] = [_repair_failure_guidance(exc)]
    if len(audit_ids) == 1:
        payload["auditId"] = next(iter(audit_ids))
    return payload, 3


def _stats_has_acknowledgements(fingerprints) -> bool:
    if not _cctally_core.DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(
            f"file:{_cctally_core.DB_PATH}?mode=ro", uri=True
        )
        try:
            rows = conn.execute(
                "SELECT violation_json FROM journal_protocol_violations"
            )
            stored = {
                item.get("fingerprint")
                for item in (json.loads(str(row[0])) for row in rows)
                if item.get("auditId")
            }
            return set(fingerprints) <= stored
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def _audit_high_water(audit_ids, high_water):
    found = {}
    for segment, offset, raw in _journal._read_range(None, high_water):
        record = _lib_journal.decode_line(raw)
        if record is not None and record.get("id") in audit_ids:
            found[record["id"]] = (segment, offset + len(raw) + 1)
    if set(found) != set(audit_ids):
        raise _journal.JournalError(
            "acknowledged protocol audit record is missing from the journal"
        )
    return max(found.values(), key=lambda item: _lib_journal.segment_sort_key(
        item[0]
    ) + (item[1],))


def _apply(requested, initial_preview):
    _call_pause_hook("before-lock")
    with _repair_locks():
        preview, selection = _preview_payload(requested)
        if preview["errors"]:
            preview["status"] = "conflict"
            return preview, 2
        if (
            preview["journalHighWater"] != initial_preview["journalHighWater"]
            or preview["journalPrefixHash"]
            != initial_preview["journalPrefixHash"]
            or preview["selectedViolations"]
            != initial_preview["selectedViolations"]
        ):
            preview["status"] = "conflict"
            preview["errors"] = [
                "journal changed before the repair lock was acquired; rerun "
                "`cctally db journal-repair` for a fresh preview"
            ]
            return preview, 2
        by_fingerprint = {
            violation.fingerprint: violation
            for violation in selection.protocol_violations
        }
        to_acknowledge = [
            by_fingerprint[fingerprint]
            for fingerprint in requested
            if fingerprint in by_fingerprint
        ]
        reviewed_high_water = (
            None
            if preview["journalHighWater"] is None
            else (
                preview["journalHighWater"]["segment"],
                preview["journalHighWater"]["offset"],
            )
        )
        if to_acknowledge:
            if reviewed_high_water is None:
                raise _journal.JournalError(
                    "journal disappeared before protocol resolution append"
                )
            audit = _lib_journal.make_protocol_resolution(
                at=_iso_now(),
                violations=to_acknowledge,
                journal_high_water=reviewed_high_water,
                journal_prefix_hash=preview["journalPrefixHash"],
            )
            audit_high_water = _journal.append_records(
                [audit],
                expected_high_water=reviewed_high_water,
            )
            _call_crash_hook("after-audit-append")
            try:
                _call_rebuild_error_hook()
                rebuild = _journal.rebuild_stats_index(
                    high_water=audit_high_water,
                    update_quota_cache=False,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
            except Exception as exc:
                return _post_audit_failure(
                    requested,
                    {audit["id"]},
                    exc,
                )
            final_payload, _final_selection = _preview_payload(requested)
            final_payload["status"] = "applied"
            final_payload["selectedViolations"] = [
                violation.to_dict() for violation in to_acknowledge
            ]
            final_payload["auditId"] = audit["id"]
            final_payload["reviewedJournalHighWater"] = (
                _high_water_dict(reviewed_high_water)
            )
            final_payload["reviewedJournalPrefixHash"] = (
                audit["payload"]["journal_prefix_hash"]
            )
            final_payload["rebuild"] = _rebuild_dict(rebuild)
            return final_payload, 0
        acknowledged = {
            violation.fingerprint: violation
            for violation in selection.acknowledged_protocol_violations
        }
        audit_ids = {
            acknowledged[fingerprint].audit_id
            for fingerprint in requested
            if fingerprint in acknowledged
        }
        preview["status"] = "already-resolved"
        if len(audit_ids) == 1:
            preview["auditId"] = next(iter(audit_ids))
        if not _stats_has_acknowledgements(requested):
            recovery_high_water = _audit_high_water(
                audit_ids, reviewed_high_water
            )
            try:
                _call_rebuild_error_hook()
                rebuild = _journal.rebuild_stats_index(
                    high_water=recovery_high_water,
                    update_quota_cache=False,
                    before_swap=lambda: _call_crash_hook(
                        "before-rebuild-swap"
                    ),
                )
            except Exception as exc:
                return _post_audit_failure(
                    requested,
                    audit_ids,
                    exc,
                )
            final_payload, _final_selection = _preview_payload(requested)
            final_payload["status"] = "recovered"
            if len(audit_ids) == 1:
                final_payload["auditId"] = next(iter(audit_ids))
            final_payload["rebuild"] = _rebuild_dict(rebuild)
            return final_payload, 0
        return preview, 0


def cmd_db_journal_repair(args) -> int:
    """Preview structural violations without mutating the journal or indexes."""
    try:
        requested = list(getattr(args, "violation", ()) or ())
        payload, _selection = _preview_payload(requested)
    except (OSError, _lib_journal.JournalProtocolError) as exc:
        if bool(getattr(args, "json", False)):
            try:
                high_water = _read_only_high_water()
            except OSError:
                high_water = None
            payload = stamp_schema_version(
                {
                    "status": "failed",
                    "journalHighWater": _high_water_dict(high_water),
                    "journalPrefixHash": None,
                    "unacknowledgedViolations": None,
                    "acknowledgedViolations": None,
                    "selectedViolations": [],
                    "auditId": None,
                    "rebuild": None,
                    "errors": [str(exc)],
                },
                version=1,
            )
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print(f"cctally: db journal-repair failed: {exc}", file=sys.stderr)
        return 2
    if bool(getattr(args, "yes", False)) and not list(
        getattr(args, "violation", ()) or ()
    ):
        payload["status"] = "conflict"
        payload["errors"] = [
            "--yes requires at least one explicit --violation fingerprint"
        ]
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, separators=(",", ":")))
        else:
            print(
                "cctally: db journal-repair: " + payload["errors"][0],
                file=sys.stderr,
            )
        return 2
    if payload["errors"]:
        payload["status"] = "conflict"
        if bool(getattr(args, "json", False)):
            print(json.dumps(payload, separators=(",", ":")))
        else:
            for error in payload["errors"]:
                print(f"cctally: db journal-repair: {error}", file=sys.stderr)
        return 2
    if bool(getattr(args, "yes", False)):
        import _cctally_db

        if _cctally_db._would_block_prod_stats(_cctally_core.DB_PATH):
            message = (
                "refusing to repair the prod stats.db "
                "(~/.local/share/cctally) from a dev checkout; run the "
                "installed binary or set CCTALLY_ALLOW_PROD_MIGRATION=1"
            )
            payload["status"] = "conflict"
            payload["errors"] = [message]
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, separators=(",", ":")))
            else:
                print(f"cctally: db journal-repair: {message}", file=sys.stderr)
            return 2
        try:
            payload, returncode = _apply(requested, payload)
        except (
            OSError,
            _journal.JournalError,
            _lib_journal.JournalProtocolError,
        ) as exc:
            payload["status"] = "failed"
            detail = _repair_failure_guidance(exc)
            payload["errors"] = [detail]
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, separators=(",", ":")))
            else:
                print(
                    f"cctally: db journal-repair failed: {detail}",
                    file=sys.stderr,
                )
            return 3
        if returncode != 0:
            if bool(getattr(args, "json", False)):
                print(json.dumps(payload, separators=(",", ":")))
            else:
                for error in payload["errors"]:
                    print(
                        f"cctally: db journal-repair: {error}",
                        file=sys.stderr,
                    )
            return returncode
        print(json.dumps(payload, separators=(",", ":"))) if bool(
            getattr(args, "json", False)
        ) else print(
            f"cctally: journal repair {payload['status']} — "
            f"audit {payload['auditId'] or 'already recorded'}."
        )
        return returncode
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, separators=(",", ":")))
    else:
        violations = payload["unacknowledgedViolations"]
        print(
            f"cctally: journal repair preview — {len(violations)} "
            "unacknowledged structural violation(s); no changes written."
        )
        for violation in violations[:10]:
            print(
                f"  {violation['batchId']}: {violation['kind']} "
                f"({violation['fingerprint']})"
            )
        if len(violations) > 10:
            print(f"  ... and {len(violations) - 10} more (see --json for all)")
    return 0
