"""#402 Task A production-shaped CLI/dashboard acceptance."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time
import urllib.request

import _lib_journal as journal


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "cctally"
AT = "2026-07-25T12:00:00Z"
KINDS = (
    "marker_conflict",
    "commit_without_begin",
    "marker_manifest_mismatch",
    "record_order_violation",
    "manifest_action_sequence_mismatch",
    "manifest_actions_hash_mismatch",
    "action_sequence_conflict",
)


def _snapshot_payload(percent: float, *, week: int = 20) -> dict:
    return {
        "kind": "snapshot_accept",
        "captured_at_utc": AT,
        "week_start_date": f"2026-07-{week:02d}",
        "week_end_date": f"2026-07-{week + 7:02d}",
        "week_start_at": f"2026-07-{week:02d}T00:00:00+00:00",
        "week_end_at": f"2026-07-{week + 7:02d}T00:00:00+00:00",
        "weekly_percent": percent,
        "source": "acceptance",
        "payload_json": "{}",
        "account_key": "unattributed",
    }


def _replace(event_id: str, percent: float, revision: int) -> dict:
    return {
        "action": "replace",
        "id": event_id,
        "rev": revision,
        "at": AT,
        "payload": _snapshot_payload(percent),
    }


def _invalid_batch(kind: str, index: int) -> list[dict]:
    begin, first, second, commit = journal.make_correction_batch(
        batch_id=f"batch:invalid-{index}",
        family="claude-usage",
        at=AT,
        actions=[
            _replace("sa:overlap", 90.0 + index, 2),
            _replace(f"sa:invalid-only-{index}", 90.0 + index, 1),
        ],
    )
    if kind == "marker_conflict":
        divergent = copy.deepcopy(begin)
        divergent["protocol_extension"] = "divergent"
        return [begin, divergent, first, second, commit]
    if kind == "commit_without_begin":
        return [first, second, commit]
    if kind == "marker_manifest_mismatch":
        commit["family"] = "other-family"
        return [begin, first, second, commit]
    if kind == "record_order_violation":
        return [begin, commit, first, second]
    if kind == "manifest_action_sequence_mismatch":
        return [begin, first, commit]
    if kind == "manifest_actions_hash_mismatch":
        first["payload"]["weekly_percent"] = 100.0
        return [begin, first, second, commit]
    if kind == "action_sequence_conflict":
        divergent = copy.deepcopy(first)
        divergent["payload"]["weekly_percent"] = 100.0
        return [begin, first, divergent, second, commit]
    raise AssertionError(kind)


def _sha256(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doctor_protocol(payload: dict) -> dict:
    for category in payload["categories"]:
        for check in category["checks"]:
            if check["id"] == "journal.protocol":
                return check
    raise AssertionError("journal.protocol check missing")


def test_real_rebuild_doctor_and_dashboard_survive_all_structural_classes(
    tmp_path,
):
    app_dir = tmp_path / "data"
    journal_dir = app_dir / "journal"
    journal_dir.mkdir(parents=True)
    segment = journal_dir / "observations-2026-07.jsonl"
    alerts_path = app_dir / "alerts.log"

    base = journal.make_evt(
        kind="snapshot_accept",
        id="sa:overlap",
        at=AT,
        payload={
            key: value
            for key, value in _snapshot_payload(10.0).items()
            if key != "kind"
        },
    )
    before = journal.make_correction_batch(
        batch_id="batch:valid-before",
        family="claude-usage",
        at=AT,
        actions=[_replace("sa:overlap", 20.0, 1)],
    )
    after = journal.make_correction_batch(
        batch_id="batch:valid-after",
        family="claude-usage",
        at=AT,
        actions=[_replace("sa:overlap", 30.0, 3)],
    )
    later = journal.make_evt(
        kind="snapshot_accept",
        id="sa:later",
        at="2026-07-27T12:00:00Z",
        payload={
            key: value
            for key, value in _snapshot_payload(40.0, week=21).items()
            if key != "kind"
        },
    )
    records = [base, *before]
    for index, kind in enumerate(KINDS):
        records.extend(_invalid_batch(kind, index))
    records.extend([*after, later])
    segment.write_bytes(b"".join(journal.encode_line(record) for record in records))

    journal_hash = _sha256(segment)
    alerts_hash = _sha256(alerts_path)
    env = {
        **os.environ,
        "CCTALLY_DATA_DIR": str(app_dir),
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": "Etc/UTC",
    }

    rebuilt = subprocess.run(
        [str(BIN), "db", "rebuild", "--db", "stats", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    rebuild_payload = json.loads(rebuilt.stdout)
    violations = rebuild_payload["journalProtocolViolations"]
    assert [
        (item["batchId"], item["kind"])
        for item in violations
    ] == [
        (f"batch:invalid-{index}", kind)
        for index, kind in enumerate(KINDS)
    ]
    assert len({item["fingerprint"] for item in violations}) == len(KINDS)

    stats_path = app_dir / "stats.db"
    conn = sqlite3.connect(stats_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:overlap'"
        ).fetchone()[0] == 30.0
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:later'"
        ).fetchone()[0] == 40.0
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events "
            "WHERE event_id LIKE 'sa:invalid-only-%' "
            "OR batch_id LIKE 'batch:invalid-%'"
        ).fetchone()[0] == 0
        assert tuple(
            conn.execute(
                "SELECT segment, offset FROM journal_cursor WHERE id = 1"
            ).fetchone()
        ) == (segment.name, segment.stat().st_size)
    finally:
        conn.close()

    doctor = subprocess.run(
        [str(BIN), "doctor", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert doctor.returncode == 2
    protocol = _doctor_protocol(json.loads(doctor.stdout))
    assert protocol["severity"] == "fail"
    assert "tainted correction batches omitted" in protocol["summary"]
    assert protocol["details"]["violations"] == violations

    dashboard_log = tmp_path / "dashboard.log"
    with dashboard_log.open("w+") as log:
        process = subprocess.Popen(
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
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            port = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                log.flush()
                text = dashboard_log.read_text()
                match = re.search(r"http://(?:localhost|127\.0\.0\.1):(\d+)", text)
                if match:
                    port = int(match.group(1))
                    break
                if process.poll() is not None:
                    raise AssertionError(text)
                time.sleep(0.1)
            assert port is not None, dashboard_log.read_text()
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/data", timeout=10
            ) as response:
                assert response.status == 200
                envelope = json.load(response)
            assert "current_week" in envelope
            assert envelope["doctor"]["severity"] == "fail"
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/doctor", timeout=10
            ) as response:
                assert response.status == 200
                dashboard_doctor = json.load(response)
            dashboard_protocol = _doctor_protocol(dashboard_doctor)
            assert dashboard_protocol["severity"] == "fail"
            assert (
                dashboard_protocol["details"]["violations"] == violations
            )
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=10
            ) as response:
                assert response.status == 200
        finally:
            process.terminate()
            process.wait(timeout=20)

    assert _sha256(segment) == journal_hash
    assert _sha256(alerts_path) == alerts_hash
