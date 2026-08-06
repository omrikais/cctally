"""#402 Task B — audited append-only structural protocol repair."""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import signal
import sqlite3
import subprocess
import time
import urllib.request
import weakref

import _lib_journal as journal
import journal_fixture_496_s5 as S5
import pytest

from conftest import load_script, redirect_paths


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "cctally"
AT = "2026-07-27T06:00:00Z"
STRUCTURAL_KINDS = (
    "marker_conflict",
    "commit_without_begin",
    "marker_manifest_mismatch",
    "record_order_violation",
    "manifest_action_sequence_mismatch",
    "manifest_actions_hash_mismatch",
    "action_sequence_conflict",
)


def _invalid_marker_conflict() -> list[dict]:
    begin, action, commit = journal.make_correction_batch(
        batch_id="batch:invalid-marker",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:missing",
                "rev": 1,
                "at": AT,
                "payload": {
                    "kind": "snapshot_accept",
                    "captured_at_utc": AT,
                    "week_start_date": "2026-07-27",
                    "week_start_at": "2026-07-27T00:00:00Z",
                    "weekly_percent": 50.0,
                    "source": "fixture",
                    "account_key": "unattributed",
                },
            }
        ],
    )
    divergent = copy.deepcopy(begin)
    divergent["protocol_extension"] = "divergent"
    return [begin, divergent, action, commit]


def _valid_base_event() -> dict:
    return journal.make_evt(
        kind="snapshot_accept",
        id="sa:valid-base",
        at=AT,
        payload={
            "captured_at_utc": AT,
            "week_start_date": "2026-07-27",
            "week_end_date": "2026-08-03",
            "week_start_at": "2026-07-27T00:00:00+00:00",
            "week_end_at": "2026-08-03T00:00:00+00:00",
            "weekly_percent": 25.0,
            "source": "fixture",
            "payload_json": "{}",
            "account_key": "unattributed",
        },
    )


def _valid_later_event() -> dict:
    return journal.make_evt(
        kind="snapshot_accept",
        id="sa:valid-later",
        at="2026-07-27T07:00:00Z",
        payload={
            "captured_at_utc": "2026-07-27T07:00:00Z",
            "week_start_date": "2026-07-27",
            "week_end_date": "2026-08-03",
            "week_start_at": "2026-07-27T00:00:00+00:00",
            "week_end_at": "2026-08-03T00:00:00+00:00",
            "weekly_percent": 26.0,
            "source": "fixture",
            "payload_json": "{}",
            "account_key": "unattributed",
        },
    )


def _invalid_batch(kind: str, *, index: int) -> list[dict]:
    batch_id = f"batch:{index}:{kind}"
    action = {
        "action": "replace",
        "id": f"sa:invalid:{index}",
        "rev": 1,
        "at": AT,
        "payload": {
            "kind": "snapshot_accept",
            "captured_at_utc": AT,
            "week_start_date": "2026-07-27",
            "week_start_at": "2026-07-27T00:00:00Z",
            "weekly_percent": 99.0,
            "source": "fixture",
            "account_key": "unattributed",
        },
    }
    begin, record, commit = journal.make_correction_batch(
        batch_id=batch_id,
        family="claude-usage",
        at=AT,
        actions=[action],
    )
    if kind == "marker_conflict":
        divergent = copy.deepcopy(begin)
        divergent["protocol_extension"] = "divergent"
        return [begin, divergent, record, commit]
    if kind == "commit_without_begin":
        return [record, commit]
    if kind == "marker_manifest_mismatch":
        commit["family"] = "other-family"
        return [begin, record, commit]
    if kind == "record_order_violation":
        return [begin, commit, record]
    if kind == "manifest_action_sequence_mismatch":
        return [begin, commit]
    if kind == "manifest_actions_hash_mismatch":
        record["payload"]["weekly_percent"] = 98.0
        return [begin, record, commit]
    if kind == "action_sequence_conflict":
        divergent = copy.deepcopy(record)
        divergent["payload"]["weekly_percent"] = 97.0
        return [begin, record, divergent, commit]
    raise AssertionError(f"unknown structural kind: {kind}")


def _tree(root: pathlib.Path) -> dict[str, tuple]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            "dir",
            path.stat().st_mode & 0o777,
        )
        if path.is_dir()
        else (
            "file",
            path.stat().st_mode & 0o777,
            path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    }


def _journal_records(journal_dir: pathlib.Path) -> list[tuple[pathlib.Path, dict]]:
    return [
        (segment, json.loads(line))
        for segment in sorted(journal_dir.glob("*.jsonl"))
        for line in segment.read_bytes().splitlines()
    ]


def _journal_repair_audits(
    journal_dir: pathlib.Path,
) -> list[tuple[pathlib.Path, dict]]:
    return [
        (segment, record)
        for segment, record in _journal_records(journal_dir)
        if record.get("src") == "journal-repair"
    ]


def _run(
    app_dir: pathlib.Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return _run_cli(
        app_dir,
        "db",
        "journal-repair",
        *args,
        extra_env=extra_env,
    )


def _run_cli(
    app_dir: pathlib.Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
        **(extra_env or {}),
    }
    return subprocess.run(
        [str(BIN), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _doctor_check(payload: dict, check_id: str) -> dict:
    for category in payload["categories"]:
        for check in category["checks"]:
            if check["id"] == check_id:
                return check
    raise AssertionError(f"missing doctor check {check_id}")


def _await_marker(
    marker: pathlib.Path,
    process: subprocess.Popen,
    *,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return
        assert process.poll() is None, (
            f"journal repair exited at rc={process.returncode} before pause"
        )
        time.sleep(0.01)
    raise AssertionError("journal repair did not reach the pause marker")


def test_preview_lists_exact_violation_without_persistent_writes(tmp_path):
    """Removing preview-only execution would create DB/lock/sidecar residue."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in _invalid_marker_conflict())
    )
    before = _tree(app_dir)

    result = _run(app_dir, "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "preview"
    assert payload["journalHighWater"] == {
        "segment": segment.name,
        "offset": segment.stat().st_size,
    }
    assert payload["journalPrefixHash"].startswith("sha256:")
    assert [
        (item["batchId"], item["kind"])
        for item in payload["unacknowledgedViolations"]
    ] == [("batch:invalid-marker", "marker_conflict")]
    violation = payload["unacknowledgedViolations"][0]
    assert violation["fingerprint"].startswith("sha256:")
    assert violation["evidence"]
    assert payload["acknowledgedViolations"] == []
    assert payload["selectedViolations"] == []
    assert payload["auditId"] is None
    assert payload["rebuild"] is None
    assert _tree(app_dir) == before


def test_exact_audit_acknowledges_violation_without_untainting_batch():
    """Treating acknowledgement as batch validity would partially apply bad data."""
    records = _invalid_marker_conflict()
    before = journal.resolve_effective_events(records)
    assert len(before.protocol_violations) == 1
    violation = before.protocol_violations[0]

    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("1" * 64),
    )
    after = journal.resolve_effective_events(
        [*records, audit],
        protocol_prefix_evidence=[
            (
                ("observations-2026-07.jsonl", 1234),
                "sha256:" + ("1" * 64),
            )
        ],
    )

    assert after.protocol_violations == ()
    assert [
        item.to_dict() for item in after.acknowledged_protocol_violations
    ] == [
        {
            **violation.to_dict(),
            "auditId": audit["id"],
            "journalHighWater": {
                "segment": "observations-2026-07.jsonl",
                "offset": 1234,
            },
            "journalPrefixHash": "sha256:" + ("1" * 64),
        }
    ]
    assert "batch:invalid-marker" not in after.completed_batches
    assert "sa:missing" not in after.by_id
    assert all(record.get("batch") != "batch:invalid-marker" for record in after.active)


def test_audit_must_follow_the_exact_violation_it_resolves():
    """Accepting a forged pre-violation audit would make the prefix binding false."""
    records = _invalid_marker_conflict()
    original = journal.resolve_effective_events(records).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[original],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("2" * 64),
    )

    with pytest.raises(
        journal.JournalProtocolError,
        match="precedes the violation",
    ):
        journal.resolve_effective_events(
            [audit, *records],
            protocol_prefix_evidence=[
                (
                    ("observations-2026-07.jsonl", 1234),
                    "sha256:" + ("2" * 64),
                )
            ],
        )


def test_later_identical_conflicting_marker_does_not_invalidate_audit():
    """Crash-replayed duplicate evidence must not turn an old audit into forgery."""
    records = _invalid_marker_conflict()
    violation = journal.resolve_effective_events(records).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("5" * 64),
    )

    selected = journal.resolve_effective_events(
        [*records, audit, copy.deepcopy(records[1])],
        protocol_prefix_evidence=[
            (
                ("observations-2026-07.jsonl", 1234),
                "sha256:" + ("5" * 64),
            )
        ],
    )

    assert selected.protocol_violations == ()
    assert [
        item.fingerprint
        for item in selected.acknowledged_protocol_violations
    ] == [violation.fingerprint]


def test_audit_without_verified_raw_prefix_evidence_fails_closed():
    records = _invalid_marker_conflict()
    violation = journal.resolve_effective_events(records).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("7" * 64),
    )

    with pytest.raises(
        journal.JournalProtocolError,
        match="requires verified raw-prefix evidence",
    ):
        journal.resolve_effective_events([*records, audit])


def test_apply_requires_explicit_violation_selection(tmp_path):
    """A bare --yes must never acknowledge current or future violations."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in _invalid_marker_conflict())
    )
    before = _tree(app_dir)

    result = _run(app_dir, "--yes", "--json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "conflict"
    assert payload["selectedViolations"] == []
    assert payload["errors"] == [
        "--yes requires at least one explicit --violation fingerprint"
    ]
    assert _tree(app_dir) == before


def test_apply_appends_one_exact_audit_and_rebuilds_usable_index(tmp_path):
    """Dropping the append/rebuild path would leave an acknowledged prefix unusable."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    original = segment.read_bytes()
    preview = _run(app_dir, "--json")
    assert preview.returncode == 0, preview.stderr
    violation = json.loads(preview.stdout)["unacknowledgedViolations"][0]

    applied = _run(
        app_dir,
        "--violation",
        violation["fingerprint"],
        "--yes",
        "--json",
    )

    assert applied.returncode == 0, applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "applied"
    assert payload["selectedViolations"] == [violation]
    assert payload["unacknowledgedViolations"] == []
    assert len(payload["acknowledgedViolations"]) == 1
    acknowledged = payload["acknowledgedViolations"][0]
    assert acknowledged["fingerprint"] == violation["fingerprint"]
    assert acknowledged["auditId"] == payload["auditId"]
    assert payload["rebuild"]["journalProtocolViolations"] == []
    assert [
        item["fingerprint"]
        for item in payload["rebuild"]["journalAcknowledgedProtocolViolations"]
    ] == [violation["fingerprint"]]

    assert segment.read_bytes().startswith(original)
    [(audit_segment, audit)] = _journal_repair_audits(journal_dir)
    assert payload["journalHighWater"] == {
        "segment": audit_segment.name,
        "offset": audit_segment.stat().st_size,
    }
    assert audit["id"] == payload["auditId"]
    assert audit["payload"]["journal_high_water"] == {
        "segment": segment.name,
        "offset": len(original),
    }

    conn = sqlite3.connect(app_dir / "stats.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:valid-base'"
        ).fetchone()[0] == 25.0
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events "
            "WHERE event_id = 'sa:missing'"
        ).fetchone()[0] == 0
        assert tuple(
            conn.execute(
                "SELECT segment, offset FROM journal_cursor WHERE id = 1"
            ).fetchone()
        ) == (
            payload["journalHighWater"]["segment"],
            payload["journalHighWater"]["offset"],
        )
    finally:
        conn.close()
    assert not (app_dir / "alerts.log").exists()


def _cutover_manifests(app_dir: pathlib.Path) -> list:
    """Every `preserve-then-atomic-replace-v1` manifest, oldest first."""
    root = pathlib.Path(app_dir) / "quarantine"
    if not root.is_dir():
        return []
    out = []
    for incident in sorted(root.iterdir()):
        path = incident / "manifest.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text())
        if payload.get("cutoverProtocol") == "preserve-then-atomic-replace-v1":
            out.append(payload)
    return out


def _force_physical_publication(db_path: pathlib.Path) -> None:
    """Make the destination one SQLite refuses to open.

    #496 S3 made in-place transactional publication the mechanism, and an
    in-place publish never preserves — preservation is a consequence of
    destroying a file. A test about the preservation manifest therefore has to
    reach the physical fallback, which only a structurally unopenable
    destination does. The magic string and the `user_version` at byte 60 are
    both left intact, because `_read_user_version_header` needs the first and
    the manifest records the second; the file-format version bytes at 18-19 are
    what SQLite rejects as NOTADB.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(db_path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    with db_path.open("r+b") as handle:
        handle.seek(18)
        handle.write(b"\xff\xff")


def test_acknowledge_apply_incident_records_its_trigger_identity(tmp_path):
    """#496 S1 F3, driven through the real `db journal-repair --yes` CLI.

    The AST sweep in tests/test_stats_incident_identity.py proves the call
    expression names a known trigger; only running the command proves that
    identity reaches the quarantine incident.
    """
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    # Materialize the live index first. Preview is write-free by design, so
    # without this the apply would publish into an ABSENT destination, preserve
    # nothing, and write no manifest — which is not the state a real install is
    # ever in when an operator runs this command.
    seeded = _run_cli(app_dir, "report", "--json")
    assert seeded.returncode == 0, seeded.stderr
    assert (app_dir / "stats.db").exists()

    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]
    before = len(_cutover_manifests(app_dir))
    # #496 S3: a readable destination is published in place and an in-place
    # publish never preserves, so the manifest under test is only written by
    # the structural fallback. Corrupting the destination after the preview is
    # safe because the apply path never opens stats.db before it rebuilds — it
    # goes straight from `_repair_locks` to `rebuild_stats_index`.
    _force_physical_publication(app_dir / "stats.db")

    applied = _run(app_dir, "--violation", fingerprint, "--yes", "--json")

    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "applied"
    manifests = _cutover_manifests(app_dir)
    assert len(manifests) == before + 1
    assert manifests[-1]["trigger"] == "journal-repair-acknowledge"
    assert manifests[-1]["schemaVersion"] == 2


def test_repeated_exact_apply_reports_already_resolved_without_append(tmp_path):
    """Appending a second audit for the same fingerprint would break idempotence."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]
    first = _run(
        app_dir, "--violation", fingerprint, "--yes", "--json"
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    after_first = _tree(journal_dir)

    repeated = _run(
        app_dir, "--violation", fingerprint, "--yes", "--json"
    )

    assert repeated.returncode == 0, repeated.stderr
    payload = json.loads(repeated.stdout)
    assert payload["status"] == "already-resolved"
    assert payload["auditId"] == first_payload["auditId"]
    assert payload["selectedViolations"] == [
        {
            key: value
            for key, value in payload["acknowledgedViolations"][0].items()
            if key
            not in {
                "auditId",
                "journalHighWater",
                "journalPrefixHash",
            }
        }
    ]
    assert payload["rebuild"] is None
    assert _tree(journal_dir) == after_first


def test_replay_rejects_changed_bytes_inside_the_audited_raw_prefix(tmp_path):
    """An unchanged violation fingerprint cannot excuse unrelated prefix drift."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    unknown = {"v": 1, "t": "future-record", "value": "a"}
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [
                unknown,
                _valid_base_event(),
                *_invalid_marker_conflict(),
            ]
        )
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]
    applied = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
    )
    assert applied.returncode == 0, applied.stderr
    assert len(_journal_repair_audits(journal_dir)) == 1
    audited_bytes = segment.read_bytes()
    assert b'"value":"a"' in audited_bytes
    segment.write_bytes(
        audited_bytes.replace(b'"value":"a"', b'"value":"b"', 1)
    )

    replay = _run(app_dir, "--json")

    assert replay.returncode == 2
    payload = json.loads(replay.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "failed"
    assert "raw-prefix binding does not match" in payload["errors"][0]
    assert len(_journal_repair_audits(journal_dir)) == 1


def test_all_seven_classes_share_one_exact_audit_and_new_divergence_reopens(
    tmp_path,
):
    """The decisive matrix binds all classes without masking later evidence."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    records = [_valid_base_event()]
    for index, kind in enumerate(STRUCTURAL_KINDS):
        records.extend(_invalid_batch(kind, index=index))
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in records)
    )
    original = segment.read_bytes()
    alerts_before = (
        (app_dir / "alerts.log").read_bytes()
        if (app_dir / "alerts.log").exists()
        else b""
    )
    tree_before = _tree(app_dir)

    preview_result = _run(app_dir, "--json")

    assert preview_result.returncode == 0, preview_result.stderr
    assert _tree(app_dir) == tree_before
    preview = json.loads(preview_result.stdout)
    assert {
        item["kind"] for item in preview["unacknowledgedViolations"]
    } == set(STRUCTURAL_KINDS)
    repeated_preview = json.loads(_run(app_dir, "--json").stdout)
    assert (
        repeated_preview["unacknowledgedViolations"]
        == preview["unacknowledgedViolations"]
    )
    fingerprints = [
        item["fingerprint"] for item in preview["unacknowledgedViolations"]
    ]

    apply_args = [
        token
        for fingerprint in fingerprints
        for token in ("--violation", fingerprint)
    ]
    applied = _run(app_dir, *apply_args, "--yes", "--json")

    assert applied.returncode == 0, applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "applied"
    assert payload["unacknowledgedViolations"] == []
    assert len(payload["acknowledgedViolations"]) == len(STRUCTURAL_KINDS)
    assert segment.read_bytes().startswith(original)
    [(audit_segment, audit)] = _journal_repair_audits(journal_dir)
    assert audit["id"] == payload["auditId"]
    assert audit["payload"]["journal_high_water"] == {
        "segment": segment.name,
        "offset": len(original),
    }
    assert [
        item["fingerprint"] for item in audit["payload"]["violations"]
    ] == sorted(fingerprints)

    later = _valid_later_event()
    with audit_segment.open("ab") as handle:
        handle.write(journal.encode_line(later))
    rebuilt = _run_cli(
        app_dir, "db", "rebuild", "--db", "stats", "--json"
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    rebuild_payload = json.loads(rebuilt.stdout)
    assert rebuild_payload["journalProtocolViolations"] == []
    assert len(
        rebuild_payload["journalAcknowledgedProtocolViolations"]
    ) == len(STRUCTURAL_KINDS)
    conn = sqlite3.connect(app_dir / "stats.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = ?",
            (later["id"],),
        ).fetchone()[0] == 26.0
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events "
            "WHERE event_id LIKE 'sa:invalid:%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert (
        (app_dir / "alerts.log").read_bytes()
        if (app_dir / "alerts.log").exists()
        else b""
    ) == alerts_before

    original_begin = records[1]
    newly_divergent = copy.deepcopy(original_begin)
    newly_divergent["protocol_extension"] = "new-after-audit"
    with audit_segment.open("ab") as handle:
        handle.write(journal.encode_line(newly_divergent))
    reopened = json.loads(_run(app_dir, "--json").stdout)
    assert len(reopened["acknowledgedViolations"]) == len(STRUCTURAL_KINDS)
    assert [
        item["kind"] for item in reopened["unacknowledgedViolations"]
    ] == ["marker_conflict"]
    assert reopened["unacknowledgedViolations"][0][
        "fingerprint"
    ] not in fingerprints


def test_unknown_explicit_selection_is_read_only(tmp_path):
    """A stale operator selection must fail before an audit or lock is created."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in _invalid_marker_conflict())
    )
    before = _tree(app_dir)

    result = _run(
        app_dir,
        "--violation",
        "sha256:" + ("f" * 64),
        "--yes",
        "--json",
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "conflict"
    assert payload["errors"] == [
        "unknown violation fingerprint(s): sha256:" + ("f" * 64)
    ]
    assert _tree(app_dir) == before


def test_apply_revalidates_the_preview_prefix_under_lock(tmp_path):
    """A concurrent append must invalidate the premise before any audit write."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in _invalid_marker_conflict())
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]
    marker = tmp_path / "before-lock.pid"
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
        "CCTALLY_JOURNAL_REPAIR_TEST_MODE": "1",
        "CCTALLY_JOURNAL_REPAIR_TEST_PAUSE_STAGE": "before-lock",
        "CCTALLY_JOURNAL_REPAIR_TEST_PAUSE_MARKER": str(marker),
    }
    process = subprocess.Popen(
        [
            str(BIN),
            "db",
            "journal-repair",
            "--violation",
            fingerprint,
            "--yes",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _await_marker(marker, process)
        later = _valid_later_event()
        with segment.open("ab") as handle:
            handle.write(journal.encode_line(later))
        process.send_signal(signal.SIGCONT)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=20)

    assert process.returncode == 2, stderr
    payload = json.loads(stdout)
    assert payload["status"] == "conflict"
    assert "fresh preview" in payload["errors"][0]
    assert not [
        json.loads(line)
        for line in segment.read_bytes().splitlines()
        if json.loads(line).get("src") == "journal-repair"
    ]
    assert not (app_dir / "stats.db").exists()
    assert not (app_dir / "alerts.log").exists()


def test_malformed_protocol_resolution_remains_fail_closed():
    """Audit-like records with invalid field shapes cannot suppress a violation."""
    records = _invalid_marker_conflict()
    violation = journal.resolve_effective_events(records).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("3" * 64),
    )
    audit["payload"]["violations"][0]["unexpected"] = True
    audit["id"] = journal.content_id(
        {
            "t": audit["t"],
            "at": audit["at"],
            "src": audit["src"],
            "payload": audit["payload"],
        }
    )

    with pytest.raises(
        journal.JournalProtocolError,
        match="violation shape is invalid",
    ):
        journal.resolve_effective_events([*records, audit])


def test_malformed_resolution_cli_json_is_fail_closed_and_read_only(tmp_path):
    """The real command preserves its envelope even when selection cannot finish."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    records = _invalid_marker_conflict()
    violation = journal.resolve_effective_events(records).protocol_violations[0]
    audit = journal.make_protocol_resolution(
        at=AT,
        violations=[violation],
        journal_high_water=("observations-2026-07.jsonl", 1234),
        journal_prefix_hash="sha256:" + ("4" * 64),
    )
    audit["payload"]["unexpected"] = True
    audit["id"] = journal.content_id(
        {
            "t": audit["t"],
            "at": audit["at"],
            "src": audit["src"],
            "payload": audit["payload"],
        }
    )
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(journal.encode_line(record) for record in [*records, audit])
    )
    before = _tree(app_dir)

    result = _run(app_dir, "--json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "failed"
    assert payload["unacknowledgedViolations"] is None
    assert "payload shape is invalid" in payload["errors"][0]
    assert _tree(app_dir) == before


def test_real_sigkill_after_audit_converges_to_one_audit_and_index(tmp_path):
    """Losing post-append progress must recover without a duplicate decision."""
    app_dir = tmp_path / "after-audit-append"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]

    killed = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
        extra_env={
            "CCTALLY_JOURNAL_REPAIR_TEST_MODE": "1",
            "CCTALLY_JOURNAL_REPAIR_TEST_CRASH_STAGE": "after-audit-append",
        },
    )
    assert killed.returncode == -signal.SIGKILL

    recovered = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
    )

    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert payload["status"] == "recovered"
    assert payload["rebuild"] is not None
    audits = [record for _segment, record in _journal_repair_audits(journal_dir)]
    assert len(audits) == 1
    assert audits[0]["id"] == payload["auditId"]
    conn = sqlite3.connect(app_dir / "stats.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        stored = json.loads(
            conn.execute(
                "SELECT violation_json FROM journal_protocol_violations"
            ).fetchone()[0]
        )
        assert stored["fingerprint"] == fingerprint
        assert stored["auditId"] == payload["auditId"]
    finally:
        conn.close()
    assert not (app_dir / "alerts.log").exists()


def test_real_sigkill_during_common_scratch_fold_recovers(tmp_path):
    """The common rebuild's in-progress scratch family must be disposable."""
    app_dir = tmp_path / "scratch-fold"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]
    marker = tmp_path / "rebuild-fold.pid"
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
        "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": "rebuild_fold_started",
        "CCTALLY_TEST_STATS_REBUILD_MARKER": str(marker),
    }
    process = subprocess.Popen(
        [
            str(BIN),
            "db",
            "journal-repair",
            "--violation",
            fingerprint,
            "--yes",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _await_marker(marker, process)
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=20)

    assert process.returncode == -signal.SIGKILL, stdout + stderr
    recovered = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
    )
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert payload["status"] == "recovered"
    audits = [record for _segment, record in _journal_repair_audits(journal_dir)]
    assert len(audits) == 1
    conn = sqlite3.connect(app_dir / "stats.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(
            conn.execute(
                "SELECT violation_json FROM journal_protocol_violations"
            ).fetchone()[0]
        )["auditId"] == payload["auditId"]
    finally:
        conn.close()
    assert not (app_dir / "alerts.log").exists()


def test_post_audit_sqlite_rebuild_error_keeps_exit3_json_and_recovers(tmp_path):
    """An uncommon scratch SQLite failure must not hide the durable audit."""
    app_dir = tmp_path / "sqlite-error"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    preview = json.loads(_run(app_dir, "--json").stdout)
    fingerprint = preview["unacknowledgedViolations"][0]["fingerprint"]

    failed = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
        extra_env={
            "CCTALLY_JOURNAL_REPAIR_TEST_MODE": "1",
            "CCTALLY_JOURNAL_REPAIR_TEST_REBUILD_ERROR": "sqlite",
        },
    )

    assert failed.returncode == 3, failed.stderr
    payload = json.loads(failed.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "failed"
    assert payload["unacknowledgedViolations"] == []
    assert payload["auditId"] == payload["acknowledgedViolations"][0][
        "auditId"
    ]
    assert "injected scratch sqlite failure" in payload["errors"][0]
    assert len(_journal_repair_audits(journal_dir)) == 1

    recovered = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
    )
    assert recovered.returncode == 0, recovered.stderr
    recovered_payload = json.loads(recovered.stdout)
    assert recovered_payload["status"] == "recovered"
    assert recovered_payload["auditId"] == payload["auditId"]
    assert len(_journal_repair_audits(journal_dir)) == 1


def test_doctor_moves_from_exact_fail_remedy_to_truthful_warn(tmp_path):
    """Reporting OK or retaining FAIL after audit would misstate omitted corrections."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    rebuilt = _run_cli(
        app_dir, "db", "rebuild", "--db", "stats", "--json"
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    violation = json.loads(rebuilt.stdout)["journalProtocolViolations"][0]

    before = _run_cli(app_dir, "doctor", "--json")
    assert before.returncode == 2
    before_leg = _doctor_check(json.loads(before.stdout), "journal.protocol")
    assert before_leg["severity"] == "fail"
    exact_apply = (
        "cctally db journal-repair --violation "
        f"{violation['fingerprint']} --yes"
    )
    assert exact_apply in before_leg["remediation"]
    assert before_leg["details"]["previewCommand"] == (
        "cctally db journal-repair"
    )
    assert before_leg["details"]["applyCommand"] == exact_apply

    applied = _run(
        app_dir,
        "--violation",
        violation["fingerprint"],
        "--yes",
        "--json",
    )
    assert applied.returncode == 0, applied.stderr
    acknowledged = json.loads(applied.stdout)["acknowledgedViolations"]

    after = _run_cli(app_dir, "doctor", "--json")
    after_leg = _doctor_check(json.loads(after.stdout), "journal.protocol")
    assert after_leg["severity"] == "warn"
    assert "acknowledged" in after_leg["summary"]
    assert "omitted" in after_leg["summary"]
    assert after_leg["details"]["violations"] == []
    assert after_leg["details"]["acknowledgedViolations"] == acknowledged


def test_dashboard_reports_durable_audit_after_next_rebuild(tmp_path):
    """A next-open rebuild must carry the post-crash audit into dashboard state."""
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    rebuilt = _run_cli(
        app_dir, "db", "rebuild", "--db", "stats", "--json"
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    fingerprint = json.loads(rebuilt.stdout)["journalProtocolViolations"][0][
        "fingerprint"
    ]
    killed = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
        extra_env={
            "CCTALLY_JOURNAL_REPAIR_TEST_MODE": "1",
            "CCTALLY_JOURNAL_REPAIR_TEST_CRASH_STAGE": "after-audit-append",
        },
    )
    assert killed.returncode == -signal.SIGKILL
    recovery = _run_cli(
        app_dir, "db", "rebuild", "--db", "stats", "--json"
    )
    assert recovery.returncode == 0, recovery.stderr
    assert [
        item["fingerprint"]
        for item in json.loads(recovery.stdout)[
            "journalAcknowledgedProtocolViolations"
        ]
    ] == [fingerprint]

    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-empty"),
        "CODEX_HOME": str(tmp_path / "codex-empty"),
        "TZ": "Etc/UTC",
    }
    dashboard = subprocess.Popen(
        [
            str(BIN),
            "dashboard",
            "--port",
            "0",
            "--host",
            "127.0.0.1",
            "--no-browser",
            "--no-sync",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert dashboard.stdout is not None
        port = None
        output = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = dashboard.stdout.readline()
            if line:
                output.append(line)
                match = re.search(r"http://(?:localhost|127\.0\.0\.1):(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
            elif dashboard.poll() is not None:
                break
        assert port is not None, "".join(output)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/doctor", timeout=10
        ) as response:
            doctor = json.load(response)
        protocol = _doctor_check(doctor, "journal.protocol")
        assert protocol["severity"] == "warn"
        assert protocol["details"]["violations"] == []
        assert [
            item["fingerprint"]
            for item in protocol["details"]["acknowledgedViolations"]
        ] == [fingerprint]
    finally:
        dashboard.terminate()
        dashboard.wait(timeout=20)
        assert dashboard.stdout is not None
        dashboard.stdout.close()


_PINNED_READER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('BEGIN')


def snapshot():
    row = conn.execute(
        "SELECT segment || '@' || offset FROM journal_cursor WHERE id = 1"
    ).fetchone()
    return str(row[0])


print(snapshot(), flush=True)
sys.stdin.readline()
print(snapshot(), flush=True)
sys.stdin.readline()
"""


def _live_cursor(db: pathlib.Path) -> str:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return str(
            conn.execute(
                "SELECT segment || '@' || offset FROM journal_cursor WHERE id = 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_live_dashboard_reader_keeps_its_snapshot_then_retry_converges(tmp_path):
    """Repair must never replace stats.db beneath the usual live reader.

    #496 S3 delivers that by publishing transactionally into the live file
    instead of refusing while a reader holds it open. The repair therefore
    completes with the dashboard running and a pinned read transaction open;
    the pinned reader keeps the generation it opened on, a fresh reader sees
    the repaired one, no scratch survives, and an identical retry is still
    idempotent against the single audit.
    """
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    segment.write_bytes(
        b"".join(
            journal.encode_line(record)
            for record in [_valid_base_event(), *_invalid_marker_conflict()]
        )
    )
    rebuilt = _run_cli(
        app_dir, "db", "rebuild", "--db", "stats", "--json"
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    fingerprint = json.loads(rebuilt.stdout)["journalProtocolViolations"][0][
        "fingerprint"
    ]
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-empty"),
        "CODEX_HOME": str(tmp_path / "codex-empty"),
        "TZ": "Etc/UTC",
    }
    dashboard = subprocess.Popen(
        [
            str(BIN),
            "dashboard",
            "--port",
            "0",
            "--host",
            "127.0.0.1",
            "--no-browser",
            "--no-sync",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    holder = None
    try:
        assert dashboard.stdout is not None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = dashboard.stdout.readline()
            if re.search(r"http://(?:localhost|127\.0\.0\.1):\d+", line):
                break
            assert dashboard.poll() is None, line
        else:
            raise AssertionError("dashboard did not start")

        holder = subprocess.Popen(
            [
                os.environ.get("PYTHON", "python3"),
                "-c",
                _PINNED_READER,
                str(app_dir / "stats.db"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout is not None
        assert holder.stdin is not None
        pinned = holder.stdout.readline().strip()
        assert pinned == _live_cursor(app_dir / "stats.db")

        applied = _run(
            app_dir,
            "--violation",
            fingerprint,
            "--yes",
            "--json",
        )
        assert applied.returncode == 0, applied.stdout + applied.stderr
        payload = json.loads(applied.stdout)
        assert payload["status"] == "applied"
        assert payload["unacknowledgedViolations"] == []
        assert [
            item["fingerprint"] for item in payload["acknowledgedViolations"]
        ] == [fingerprint]
        assert payload["auditId"] == payload["acknowledgedViolations"][0][
            "auditId"
        ]

        holder.stdin.write("go\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == pinned, (
            "the pinned reader must keep the generation it opened on"
        )
        assert _live_cursor(app_dir / "stats.db") != pinned, (
            "a fresh reader must see the repaired generation"
        )
        assert sorted(app_dir.glob("stats.db.rebuilding-*")) == []
        conn = sqlite3.connect(app_dir / "stats.db")
        try:
            assert conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0] == "ok"
        finally:
            conn.close()
    finally:
        if holder is not None:
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=20)
            if holder.stdin is not None:
                holder.stdin.close()
            assert holder.stdout is not None
            holder.stdout.close()
        dashboard.terminate()
        dashboard.wait(timeout=20)
        assert dashboard.stdout is not None
        dashboard.stdout.close()

    retried = _run(
        app_dir,
        "--violation",
        fingerprint,
        "--yes",
        "--json",
    )
    assert retried.returncode == 0, retried.stdout + retried.stderr
    retry_payload = json.loads(retried.stdout)
    assert retry_payload["status"] == "already-resolved"
    assert retry_payload["auditId"] == payload["auditId"]
    audits = [record for _segment, record in _journal_repair_audits(journal_dir)]
    assert len(audits) == 1


# ==========================================================================
# #496 S5 — the streaming prefix conversion
#
# `_read_prefix` materialized every raw line and then every decoded record,
# `_prefix_hash` traversed the prefix a second time, each resolution op made
# `journal_prefix_hash` traverse it again, and the already-resolved recovery
# branch traversed it once more through `_audit_high_water`. The conversion
# folds all of that into one pinned streaming pass, so nothing about the
# command's output may move.
# ==========================================================================

#: `db journal-repair --json` over `journal_fixture_496_s5.build_tainted`,
#: captured from the PRE-CHANGE implementation at a48ff6b3b. Written out as a
#: literal rather than recomputed, because a value recomputed by the code under
#: test proves only that the code agrees with itself. Every acknowledgement
#: fingerprint here hashes the selector's `enumerate` sequence numbers, and each
#: is named by name from a durable `journal_protocol_resolution` op, so a shift
#: in any of them would make an acknowledged violation unresolvable forever.
S5_BASELINE_PREVIEW = {
    "acknowledgedViolations": [
        {
            "auditId": "o:164552927507c250",
            "batchId": "batch:ack-one",
            "evidence": {
                "commitRecordHash": (
                    "sha256:35897a1a574e43647e417eed774635fd2ec96cb2840db35c"
                    "302e183cecc3164e"
                ),
                "commitSequence": 4,
            },
            "fingerprint": (
                "sha256:afcc566bae2a0a9493b27307684e1e2d455adb7c2aac2366d703"
                "a8e3175c12d2"
            ),
            "journalHighWater": {
                "offset": 492,
                "segment": "observations-2026-08.jsonl",
            },
            "journalPrefixHash": (
                "sha256:842c1a606f53b7cb758690f01cd0e316aae8fd59658d6453152d"
                "198bea754156"
            ),
            "kind": "commit_without_begin",
        },
        {
            "auditId": "o:688ae94a78b63582",
            "batchId": "batch:ack-two",
            "evidence": {
                "commitRecordHash": (
                    "sha256:e22dd608fa2bf093c30039c23c87827c4b983afc27e70596"
                    "6971dc7b57767078"
                ),
                "commitSequence": 6,
            },
            "fingerprint": (
                "sha256:d2a3c3a6d518a501257cf1b146766c57d7bdbcd15d7b76a47470"
                "1cbc1a5db504"
            ),
            "journalHighWater": {
                "offset": 968,
                "segment": "observations-2026-08.jsonl",
            },
            "journalPrefixHash": (
                "sha256:09e0adafcd83ed4e160d1bbbdcac6f97ad2832c289b0c0ab89fc"
                "32d985d9a94f"
            ),
            "kind": "commit_without_begin",
        },
    ],
    "auditId": None,
    "errors": [],
    "journalHighWater": {
        "offset": 1116,
        "segment": "observations-2026-09.jsonl",
    },
    "journalPrefixHash": (
        "sha256:092e517cd9560221590ff91264300a94f4895fe914ff8f55970cadc51b8e"
        "d472"
    ),
    "rebuild": None,
    "schemaVersion": 1,
    "selectedViolations": [],
    "status": "preview",
    "unacknowledgedViolations": [
        {
            "batchId": "batch:unack",
            "evidence": {
                "commitRecordHash": (
                    "sha256:4d75179a0565d5509b6389a013f525b5fa4e9f3c6df446a3"
                    "e9774378fdea2857"
                ),
                "commitSequence": 9,
            },
            "fingerprint": (
                "sha256:4b3bd585f4189e9ea6b79f33c3971953b526b9b7801f31ba0b9d"
                "9a8fdc40500a"
            ),
            "kind": "commit_without_begin",
        },
    ],
}

#: The requested order the baseline used: the unacknowledged fingerprint first,
#: then the two acknowledged ones. `selectedViolations` preserves request order,
#: so this pins the ordering too.
S5_REQUESTED_FINGERPRINTS = (
    S5_BASELINE_PREVIEW["unacknowledgedViolations"][0]["fingerprint"],
    S5_BASELINE_PREVIEW["acknowledgedViolations"][0]["fingerprint"],
    S5_BASELINE_PREVIEW["acknowledgedViolations"][1]["fingerprint"],
)

#: The pre-change `_audit_high_water` answer for both audit records, and for
#: each one alone. The conversion derives these from the streaming pass instead
#: of re-reading the prefix, so they are pinned separately from the payload.
S5_RECOVERY_HIGH_WATER = ("observations-2026-09.jsonl", 476)
S5_RECOVERY_HIGH_WATER_BY_AUDIT = {
    "o:164552927507c250": ("observations-2026-08.jsonl", 968),
    "o:688ae94a78b63582": ("observations-2026-09.jsonl", 476),
}


def _selected_violation(fingerprint):
    for item in (
        *S5_BASELINE_PREVIEW["unacknowledgedViolations"],
        *S5_BASELINE_PREVIEW["acknowledgedViolations"],
    ):
        if item["fingerprint"] == fingerprint:
            return {
                key: value
                for key, value in item.items()
                if key not in ("auditId", "journalHighWater",
                               "journalPrefixHash")
            }
    raise AssertionError(fingerprint)


def test_s5_repair_preview_is_byte_identical_to_the_pre_change_baseline(
    tmp_path,
):
    app_dir = tmp_path / "s5-equivalence"
    S5.build_tainted(app_dir, seed_cache=False)

    result = _run(app_dir, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == S5_BASELINE_PREVIEW

    selected = _run(
        app_dir,
        "--json",
        *(arg for fp in S5_REQUESTED_FINGERPRINTS
          for arg in ("--violation", fp)),
    )
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout) == {
        **S5_BASELINE_PREVIEW,
        "selectedViolations": [
            _selected_violation(fp) for fp in S5_REQUESTED_FINGERPRINTS
        ],
    }


@pytest.fixture
def s5_repair(tmp_path, monkeypatch):
    """The S5 fixture in-process, with every segment open recorded in order."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_journal_repair as repair

    built = S5.build_tainted(_cctally_core.APP_DIR, seed_cache=False)
    real = jr._open_segment_for_read
    opened: list[str] = []

    def record(seg_path):
        opened.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return {"built": built, "opened": opened, "repair": repair, "jr": jr}


def test_one_selection_snapshot_opens_each_segment_exactly_once(s5_repair):
    """Before the conversion this snapshot opened the three segments ten times:
    once each for the materialized read, twice more for each of the two
    resolution ops' `journal_prefix_hash` re-reads, and three more for the
    separate `_prefix_hash` pass."""
    repair, opened = s5_repair["repair"], s5_repair["opened"]
    opened.clear()
    snapshot = repair._selection_snapshot()
    selection = snapshot.selection

    assert opened == list(S5.SEGMENTS), (
        "a selection snapshot must open each segment through the seam exactly "
        "once, in canonical order"
    )
    assert snapshot.high_water == s5_repair["built"]["high_water"]
    assert snapshot.prefix_hash == S5_BASELINE_PREVIEW["journalPrefixHash"]
    assert [
        item.fingerprint for item in selection.protocol_violations
    ] == [
        item["fingerprint"]
        for item in S5_BASELINE_PREVIEW["unacknowledgedViolations"]
    ]
    assert [
        item.fingerprint for item in selection.acknowledged_protocol_violations
    ] == [
        item["fingerprint"]
        for item in S5_BASELINE_PREVIEW["acknowledgedViolations"]
    ]

    assert snapshot.audit_ends == {
        audit_id: coordinate
        for audit_id, coordinate in S5_RECOVERY_HIGH_WATER_BY_AUDIT.items()
    }


def test_the_prefix_hasher_is_released_before_the_normalization_loop(
    tmp_path, monkeypatch
):
    """`PrefixHashAccumulator` buffers the segment it is reading — 410 MB on the
    maintainer's journal — and `rebuild_stats_index` drops it the moment its pass
    ends, before it walks the decoded records. This pass must do the same, or
    that buffer stays resident across the whole normalization loop.

    Residency is not visible in any output, so this asserts it structurally: by
    the time the first record is normalized, the accumulator must already be
    unreachable.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_journal_repair as repair
    import _lib_journal_router as router

    S5.build_tainted(_cctally_core.APP_DIR, seed_cache=False)

    class _Tracked(router.PrefixHashAccumulator):
        """A weak-referenceable accumulator, otherwise the real one."""

    made: list = []

    def _factory(*args, **kwargs):
        accumulator = _Tracked(*args, **kwargs)
        made.append(weakref.ref(accumulator))
        return accumulator

    monkeypatch.setattr(router, "PrefixHashAccumulator", _factory)

    live: list = []
    real_normalize = jr._normalize_legacy_account_stamp

    def _normalize(record, account):
        live.append([ref() is not None for ref in made])
        return real_normalize(record, account)

    monkeypatch.setattr(jr, "_normalize_legacy_account_stamp", _normalize)

    records, _evidence, prefix_hash, _ends = repair._read_prefix(
        repair._read_only_high_water())
    assert records and prefix_hash, "the pass must have produced both"
    assert made, "the accumulator factory was never reached"
    assert live, "no record was normalized, so nothing was observed"
    assert all(alive == [False] * len(made) for alive in live), (
        "the prefix hasher was still reachable while records were normalized"
    )


def test_a_cutover_op_in_the_prefix_resolves_the_same_legacy_account(
    tmp_path, monkeypatch
):
    """The streaming pass captures the cutover op inline instead of calling
    `resolve_cutover_claude_account`, which scanned every segment again.

    COVERAGE BOUND: this exercises AGREEMENT over one pinned prefix — the op is
    appended before `_read_only_high_water` pins the high-water, so both readers
    see it. It cannot observe the one residual divergence, the TOCTOU window
    between pinning the high-water and the streaming loop, where an op appended
    inside that window used to be found by the whole-journal scan and now is
    not. That case is accepted behaviour (see `_read_prefix`), so this test is
    not evidence for the general equivalence of the two readers."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_journal_repair as repair

    S5.build_tainted(_cctally_core.APP_DIR, seed_cache=False)
    cutover = journal.make_op(
        at="2026-07-27T05:00:00Z",
        src="accounts-cutover",
        payload={"kind": "accounts_cutover",
                 "claude_legacy_account": "acct-legacy-claude"},
    )
    cutover["id"] = jr.CUTOVER_OP_ID
    legacy = journal.make_obs(
        at="2026-07-27T05:30:00Z", src="record-usage", provider="claude",
        payload={"captured_at": "2026-07-27T05:30:00Z", "source": "statusline",
                 "weekly_percent": 5.0},
    )
    with open(_cctally_core.JOURNAL_DIR / S5.SEG_C, "ab") as handle:
        handle.write(journal.encode_line(cutover))
        handle.write(journal.encode_line(legacy))

    assert jr.resolve_cutover_claude_account() == "acct-legacy-claude"
    records, _evidence, _hash, _ends = repair._read_prefix(
        repair._read_only_high_water())
    stamped = [
        record for record in records if record.get("id") == legacy["id"]
    ]
    assert len(stamped) == 1
    assert stamped[0]["account"] == "acct-legacy-claude"


def test_the_recovery_coordinate_costs_no_further_traversal(s5_repair):
    repair, opened = s5_repair["repair"], s5_repair["opened"]
    snapshot = repair._selection_snapshot()
    opened.clear()
    assert repair._recovery_high_water(
        set(S5_RECOVERY_HIGH_WATER_BY_AUDIT), snapshot.audit_ends
    ) == S5_RECOVERY_HIGH_WATER
    assert opened == [], (
        "the recovery coordinate must come from the pass that already ran"
    )


class _StubRebuildResult:
    """Just enough of a `RebuildResult` for `_rebuild_dict` to render."""

    segments_read = 0
    lines_folded = 0
    malformed = 0
    duration_s = 0.0
    rows_by_table: dict = {}
    conflicts: tuple = ()
    protocol_violations: tuple = ()
    acknowledged_protocol_violations: tuple = ()


def test_the_whole_recovery_command_takes_no_extra_prefix_traversal(
    s5_repair, monkeypatch, capsys
):
    """Criterion 6 counts EVERY traversal the command performs, not one per
    snapshot. A per-snapshot phrasing is exactly what would have let
    `_audit_high_water`'s fourth traversal survive unnoticed, so this drives the
    already-resolved recovery branch end to end and counts every open.

    The rebuild itself is stubbed: it is a separate concern with its own pinned
    read pass, and leaving it in would attribute its opens to this command's
    prefix handling.
    """
    repair, opened, jr = (
        s5_repair["repair"], s5_repair["opened"], s5_repair["jr"])
    rebuilt = []

    def _stub_rebuild(*, context, high_water, update_quota_cache,
                      before_swap=None):
        rebuilt.append(high_water)
        return _StubRebuildResult()

    monkeypatch.setattr(jr, "rebuild_stats_index", _stub_rebuild)

    fingerprint = S5_BASELINE_PREVIEW["acknowledgedViolations"][1][
        "fingerprint"]
    audit_id = S5_BASELINE_PREVIEW["acknowledgedViolations"][1]["auditId"]
    opened.clear()
    returncode = repair.cmd_db_journal_repair(
        argparse.Namespace(violation=[fingerprint], yes=True, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert returncode == 0, payload
    assert payload["status"] == "recovered"
    assert payload["auditId"] == audit_id
    assert rebuilt == [S5_RECOVERY_HIGH_WATER_BY_AUDIT[audit_id]]
    # Three legitimate selection snapshots: the unlocked preview, the revalidated
    # preview under the repair lock, and the final preview after the rebuild.
    assert opened == list(S5.SEGMENTS) * 3, opened
