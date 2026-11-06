"""Shared store-shaping helpers for the #1005/#1018 Doctor maintenance tests.

`tests/test_cmd_doctor.py` (gather and CLI) and `tests/test_doctor_dashboard.py`
(`GET /api/doctor`) build the same stores. Every helper resolves the data dir
through `_cctally_core` at call time, so it must run after `load_script()` and
`redirect_paths(...)`.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import pathlib
import sys
import time


def seed_real_store():
    """One real `record-usage` reading: a current-epoch stats.db, a journal
    with retained bytes, and the maintenance lock file, all in the redirected
    data dir. The gather's two journal-leg stats reads then run as well as
    the six unconditional ones."""
    import _cctally_core
    now = int(time.time())
    args = argparse.Namespace(
        percent=7, resets_at=str(now + 3 * 86400),
        five_hour_percent=11, five_hour_resets_at=str(now + 3600),
        source="statusline")
    assert sys.modules["cctally"].cmd_record_usage(
        args, nudge_dashboard=False) == 0
    assert _cctally_core.DB_PATH.exists()
    assert any(_cctally_core.JOURNAL_DIR.glob("*.jsonl"))


def move_stats_into_completed_incident():
    """The physical-publication window (spec 3.1): the old family sits in a
    completed quarantine incident and one exact rebuild scratch base exists
    beside the absent `stats.db`, so `stats_interrupted_rebuild_evidence`
    reaches its journal high-water read. The maintenance lock file exists
    (unheld), as it does after any real rebuild."""
    import _cctally_core
    incident_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    incident = (_cctally_core.APP_DIR / "quarantine"
                / f"stats.db-{incident_at:%Y%m%dT%H%M%SZ}")
    incident.mkdir(parents=True)
    moved = []
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(f"{_cctally_core.DB_PATH}{suffix}")
        if member.exists():
            member.replace(incident / member.name)
            moved.append(member.name)
    (incident / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1,
        "quarantinedAtUtc": incident_at.isoformat().replace("+00:00", "Z"),
        "originalPath": str(_cctally_core.DB_PATH),
        "movedFiles": moved,
        "complete": True,
    }))
    scratch_at = incident_at + dt.timedelta(seconds=30)
    (_cctally_core.APP_DIR
     / f"stats.db.rebuilding-{scratch_at:%Y%m%dT%H%M%S}_000000").write_bytes(b"")
    _cctally_core.STATS_LOCK_MAINTENANCE_PATH.touch()
    assert not _cctally_core.DB_PATH.exists()


@contextlib.contextmanager
def held_exclusive(path):
    """Hold `path` exclusive on a SEPARATE open file description: flock
    conflicts are per description, so the gather's own opens (or the server
    thread's) contend with it exactly as with another process (cf.
    `test_gather_conversations_declines_during_recovery`)."""
    path.touch()
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fh
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
