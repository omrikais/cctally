"""#496 S1 — forensics-time WAL evidence, damage scans, and the publication
transaction.

These share one file because they share the kill/drive harness: proving that a
rebuild's published bytes are validated, and that a failed publication is
durable rather than merely raised, both require killing a real process at a
known seam and then observing what a LATER process finds on disk.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import load_script, redirect_paths

ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_live_index():
    """One journaled observation folded into a live stats.db."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    jr.run_stats_ingest(mode="authoritative")
    return jr


def _await_marker(
    marker: pathlib.Path, process: subprocess.Popen, budget_s: float = 30.0
) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return
        assert process.poll() is None, (
            f"process exited at rc={process.returncode} before {marker.name}"
        )
        time.sleep(0.01)
    raise AssertionError(f"process never reached pause marker {marker}")


def _strand_committed_wal_family(
    db: pathlib.Path, tmp_path: pathlib.Path
) -> tuple:
    """Leave a committed, non-empty WAL+SHM family as if its writer crashed.

    Modelled on tests/test_stats_rebuild_cutover_388.py, which uses the same
    disable-autocheckpoint-then-SIGKILL shape, with one deliberate difference.
    The schema change is committed and CHECKPOINTED first, and only a plain
    INSERT is left stranded. Measured against SQLite 3.53.4: a commit that
    changes the schema writes page 1 into the WAL, and a page-1 frame then
    MASKS a damaged main-file header completely -- `PRAGMA quick_check` even
    answers `ok`. An INSERT-only commit writes no page-1 frame, so the damage
    this test then applies is actually observable.
    """
    marker = tmp_path / "wal-writer.pid"
    script = """
import os, pathlib, signal, sqlite3, sys
db, marker = sys.argv[1:]
conn = sqlite3.connect(db, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE forensics_wal_probe (value TEXT NOT NULL)")
conn.commit()
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("INSERT INTO forensics_wal_probe VALUES ('committed')")
conn.commit()
pathlib.Path(marker).write_text(f"{os.getpid()}\\n")
os.kill(os.getpid(), signal.SIGSTOP)
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(marker, writer)
        os.kill(writer.pid, signal.SIGKILL)
        writer.communicate(timeout=30)
    finally:
        if writer.poll() is None:
            os.kill(writer.pid, signal.SIGKILL)
            writer.wait(timeout=30)
    assert writer.returncode == -signal.SIGKILL
    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    wal_bytes = wal.read_bytes()
    shm_bytes = shm.read_bytes()
    assert wal_bytes and shm_bytes
    return wal_bytes, shm_bytes


def _destroy_header_magic(db: pathlib.Path) -> None:
    """Make every open of the family fail with a CLASSIFIED corruption error."""
    with db.open("r+b") as handle:
        handle.write(b"not a database\x00")


def _page_size_of(db: pathlib.Path) -> int:
    raw = int.from_bytes(db.read_bytes()[16:18], "big")
    return 65536 if raw == 1 else raw


def _zero_main_data_pages(db: pathlib.Path) -> None:
    """Damage every page but page 1.

    The schema then still parses while every table b-tree read raises, which is
    the damage shape `PRAGMA quick_check` detects and the cheap open-time probe
    does not. Page 1 is left intact deliberately: the cutover still has to
    checkpoint the old family, and the file header is where
    `preservedUserVersion` lives.
    """
    page_size = _page_size_of(db)
    total = db.stat().st_size // page_size
    with db.open("r+b") as handle:
        for page in range(2, total + 1):
            handle.seek((page - 1) * page_size)
            handle.write(b"\x00" * page_size)


def _clobber_table_root_page(db: pathlib.Path, table: str) -> int:
    """Invalidate one named table's root page, leaving the schema readable.

    This is the production shape: the retained corpus implicates
    `quota_projection_state` and its automatic index almost every time, not the
    whole file.
    """
    conn = sqlite3.connect(str(db))
    try:
        root = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = ?", (table,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    page_size = _page_size_of(db)
    with db.open("r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b"\x00" * page_size)
    return root


def _forensics_bundles(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_file() and "corruption-forensics" in p.name
    )


def _evidence_dirs(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_dir() and "corruption-forensics" in p.name
    )


def _latest_bundle(log_dir: pathlib.Path) -> dict:
    bundles = _forensics_bundles(log_dir)
    assert bundles, "no forensics bundle was written"
    return json.loads(bundles[-1].read_text())


# ==========================================================================
# F2 — forensics-time WAL capture
# ==========================================================================

def test_forensics_captures_the_wal_bytes_present_when_the_damage_was_found(
    ns, tmp_path
):
    """The capture is driven through the dashboard/TUI post-query heal.

    That entry point is used rather than an ordinary `open_db` heal for a
    measured reason. `open_db` detects corruption on its own READ-WRITE
    connection and closes it before the heal hook runs; that close is the last
    close of the family, and SQLite removes the WAL as part of it, so nothing
    downstream can capture bytes that no longer exist. The post-query path
    reaches the hook with no read-write handle ever having been opened, and
    both the heal's locked re-check and the bundle's own integrity probe open
    read-only, which cannot checkpoint. The residual gap in the `open_db` path
    is recorded in the spec rather than closed here: closing it means capturing
    at the `open_db` corruption boundary, which is a later session's surface.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    wal_bytes, shm_bytes = _strand_committed_wal_family(db, tmp_path)
    _zero_main_data_pages(db)
    assert pathlib.Path(f"{db}-wal").stat().st_size == len(wal_bytes)

    healed = _cctally_tui._tui_heal_post_query_stats(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert healed is True

    conn = _cctally_core.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [7.0], "the heal must republish a usable index"

    evidence = _evidence_dirs(_cctally_core.LOG_DIR)
    assert len(evidence) == 1, f"expected one evidence directory, got {evidence!r}"
    assert (evidence[0] / "stats.db-wal").read_bytes() == wal_bytes
    assert len((evidence[0] / "stats.db-shm").read_bytes()) == len(shm_bytes)
    assert not (evidence[0] / "stats.db").exists(), (
        "the main file is deliberately not copied at forensics time"
    )

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "captured"
    assert bundle["walEvidence"]["path"] == str(evidence[0])
    assert bundle["walEvidence"]["bytes"]["stats.db-wal"] == len(wal_bytes)


def test_an_empty_wal_records_skipped_empty_and_writes_no_evidence_file(ns):
    import _cctally_core

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    # `run_stats_ingest` closed the family cleanly, so SQLite already folded
    # and removed the WAL — the routine corruption-heal shape.
    assert not pathlib.Path(f"{db}-wal").exists()
    _destroy_header_magic(db)

    healed = _cctally_core.open_db()
    healed.close()

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "skipped_empty"
    assert bundle["walEvidence"]["path"] is None
    assert _evidence_dirs(_cctally_core.LOG_DIR) == [], (
        "an empty WAL must not leave a zero-byte file pretending to be evidence"
    )


def test_operator_rebuild_on_a_healthy_index_records_skipped_not_corruption(ns):
    import _cctally_core

    _seed_live_index()
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False))
    assert rc == 0

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["walEvidence"]["disposition"] == "skipped_not_corruption"
    assert _evidence_dirs(_cctally_core.LOG_DIR) == []


# ==========================================================================
# F8 — the damage description, and its inability to break a heal
# ==========================================================================

def test_forensics_bundle_carries_a_structured_damage_description(ns):
    import _cctally_core

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _destroy_header_magic(db)

    healed = _cctally_core.open_db()
    healed.close()

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    damage = bundle["damage"]
    assert damage["schemaVersion"] == 1
    assert damage["method"] in ("integrity_rows", "raw_scan", "both", "unavailable")
    assert isinstance(damage["shapeToken"], str) and damage["shapeToken"]


def test_a_failing_damage_scan_never_breaks_the_heal(ns, monkeypatch):
    import _cctally_core
    import _lib_stats_damage

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _destroy_header_magic(db)

    def _boom(**_kwargs):
        raise RuntimeError("injected characterization failure")

    monkeypatch.setattr(_lib_stats_damage, "describe_damage", _boom)

    healed = _cctally_core.open_db()
    try:
        rows = healed.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        healed.close()
    # Assert on the heal's own success, not merely on the absence of an
    # exception: the rebuilt index must carry the journal-covered fact.
    assert [r[0] for r in rows] == [7.0]
    incidents = sorted((_cctally_core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    assert bundle["damage"]["method"] == "unavailable"
    assert "injected characterization failure" in bundle["damage"]["reason"]


def test_damage_description_names_the_implicated_object(ns, tmp_path):
    """F8's core claim, proven non-vacuously.

    The raw scan must name the object whose root page is damaged. In 54 of the
    74 retained production bundles `PRAGMA integrity_check` raised and produced
    no rows at all, so this finding cannot come from the pragma.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _clobber_table_root_page(db, "quota_projection_state")

    assert _cctally_tui._tui_heal_post_query_stats(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    bundle = _latest_bundle(_cctally_core.LOG_DIR)
    damage = bundle["damage"]
    assert damage["method"] in ("raw_scan", "both")
    scanned = [f for f in damage["findings"] if f["kind"] == "bad_root_page_type"]
    assert [f["table"] for f in scanned] == ["quota_projection_state"]
    assert damage["shapeToken"] != "none"


# ==========================================================================
# F8 — three scan points, placed so they bracket the explicit checkpoint
# ==========================================================================

def _incident_manifest(app_dir: pathlib.Path) -> dict:
    incidents = sorted((app_dir / "quarantine").iterdir())
    assert len(incidents) == 1, f"expected one incident, got {incidents!r}"
    return json.loads((incidents[0] / "manifest.json").read_text())


def test_corrupt_old_family_records_a_skipped_checkpoint(ns):
    import _cctally_core

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    healed = _cctally_core.open_db()
    healed.close()

    damage = _incident_manifest(_cctally_core.APP_DIR)["damage"]
    assert damage["checkpointOutcome"] == "skipped_corrupt"
    assert damage["preserved"]["schemaVersion"] == 1
    assert damage["postCheckpoint"]["schemaVersion"] == 1


def test_healthy_old_family_records_a_completed_checkpoint(ns):
    import _cctally_core

    _seed_live_index()
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False)) == 0

    damage = _incident_manifest(_cctally_core.APP_DIR)["damage"]
    assert damage["checkpointOutcome"] == "checkpointed"
    assert damage["preserved"]["method"] == "raw_scan"
    assert damage["preserved"]["findings"] == []
    assert damage["preserved"]["shapeToken"] == "none"
    assert damage["postCheckpoint"]["method"] == "raw_scan"
    assert damage["postCheckpoint"]["findings"] == []


def test_preserved_and_post_checkpoint_scans_name_the_damaged_object(ns):
    """The retained artifact is described, not just the live one.

    The preserved copy is taken BEFORE the explicit checkpoint and the
    post-checkpoint scan after it, so the pair shows what that checkpoint did.
    """
    import _cctally_core
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _clobber_table_root_page(db, "quota_projection_state")

    assert _cctally_tui._tui_heal_post_query_stats(
        sqlite3.DatabaseError("database disk image is malformed")
    ) is True

    damage = _incident_manifest(_cctally_core.APP_DIR)["damage"]
    assert damage["checkpointOutcome"] == "checkpointed"
    for scan in (damage["preserved"], damage["postCheckpoint"]):
        named = [f for f in scan["findings"] if f["kind"] == "bad_root_page_type"]
        assert [f["table"] for f in named] == ["quota_projection_state"]
    assert damage["preserved"]["shapeToken"] == damage["postCheckpoint"]["shapeToken"]


# ==========================================================================
# F1 — fresh-connection validation at both points
# ==========================================================================

def _main_file_of(conn) -> pathlib.Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return pathlib.Path(row[2])
    raise AssertionError("connection has no main database")


def _publication_marker(app_dir: pathlib.Path) -> pathlib.Path:
    return app_dir / "stats.db.publication"


def _rebuild_records(log_dir: pathlib.Path) -> list:
    if not log_dir.exists():
        return []
    return sorted(log_dir.glob("stats-rebuild-*.json"))


def _latest_record(log_dir: pathlib.Path) -> dict:
    records = _rebuild_records(log_dir)
    assert records, "no rebuild record was written"
    return json.loads(records[-1].read_text())


def test_a_scratch_damaged_after_the_build_is_refused_before_publication(ns):
    """The pre-publication check reads the bytes that will actually be
    published, on a connection that never saw them being written."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    before = pathlib.Path(_cctally_core.DB_PATH).read_bytes()

    real_remove = jr._remove_db_sidecars_strict

    def remove_then_damage(path):
        real_remove(path)
        candidate = pathlib.Path(path)
        if ".rebuilding-" in candidate.name:
            with candidate.open("r+b") as handle:
                handle.write(b"not a database\x00")

    jr._remove_db_sidecars_strict = remove_then_damage
    try:
        with pytest.raises(Exception):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="test-fixture")
            )
    finally:
        jr._remove_db_sidecars_strict = real_remove

    assert pathlib.Path(_cctally_core.DB_PATH).read_bytes() == before, (
        "a pre-publication refusal must leave the old family live"
    )
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == [], (
        "a pre-publication refusal must not create a quarantine incident"
    )
    assert not _publication_marker(_cctally_core.APP_DIR).exists()


def test_a_successful_rebuild_records_both_verdicts_and_leaves_no_marker(ns):
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    record = _latest_record(_cctally_core.LOG_DIR)
    assert record["schemaVersion"] == 1
    assert record["status"] == "ok"
    assert record["trigger"] == "db-rebuild"
    assert record["binaryEpoch"] == _cctally_core.STATS_INDEX_EPOCH
    assert record["prePublicationValidation"] == {"ok": True, "error": None}
    assert record["postPublicationValidation"] == {"ok": True, "error": None}
    assert record["incidentPath"]
    assert not _publication_marker(_cctally_core.APP_DIR).exists()

    manifest = _incident_manifest(_cctally_core.APP_DIR)
    assert manifest["rebuildRecordPath"] == str(
        _rebuild_records(_cctally_core.LOG_DIR)[-1]
    )


def test_a_published_family_ends_with_no_sidecars(ns):
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    db = pathlib.Path(_cctally_core.DB_PATH)
    assert db.exists()
    assert not pathlib.Path(f"{db}-wal").exists()
    assert not pathlib.Path(f"{db}-shm").exists()


def test_a_failed_post_publication_verdict_is_durable_and_refuses_the_next_open(
    ns,
):
    """The verdict has to outlive the process that reached it.

    After `os.replace` no scratch remains, so interrupted recovery cannot fire,
    and an epoch-current index returns from `open_db` without validation. A
    raise alone would therefore leave a known-bad index accepted by every later
    command.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    real_validate = jr._validate_rebuilt_stats_index

    def fail_on_the_destination(conn, high_water):
        real_validate(conn, high_water)
        if ".rebuilding-" not in _main_file_of(conn).name:
            raise jr.JournalError("injected post-publication validation failure")

    jr._validate_rebuilt_stats_index = fail_on_the_destination
    try:
        with pytest.raises(jr.JournalError, match="injected post-publication"):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._validate_rebuilt_stats_index = real_validate

    marker_path = _publication_marker(_cctally_core.APP_DIR)
    marker = json.loads(marker_path.read_text())
    assert marker["status"] == "failed"
    record_path = marker["recordPath"]
    record = json.loads(pathlib.Path(record_path).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False
    assert "injected post-publication" in record["postPublicationValidation"]["error"]

    # A REAL second open, not an inspection of the marker.
    import _cctally_db

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()
    assert record_path in str(caught.value)


# ==========================================================================
# F1 — the crash-point map, driven through real process kills
# ==========================================================================

def _isolated_env(tmp_path: pathlib.Path) -> dict:
    data = tmp_path / "data"
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    for path in (data, home, claude / "projects", codex):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "TZ": "Etc/UTC",
        }
    )
    return env


def _record_usage(env: dict) -> subprocess.CompletedProcess:
    now = int(time.time())
    return subprocess.run(
        [
            sys.executable, str(CCTALLY), "record-usage",
            "--percent", "7",
            "--resets-at", str(now + 3 * 86400),
            "--five-hour-percent", "11",
            "--five-hour-resets-at", str(now + 3600),
        ],
        env=env, capture_output=True, text=True, timeout=120,
    )


def _seed_cli(env: dict) -> pathlib.Path:
    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    db = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "stats.db"
    assert db.exists()
    return db


def _kill_rebuild_at(env: dict, tmp_path: pathlib.Path, point: str) -> None:
    marker = tmp_path / f"{point}.pid"
    child_env = dict(env)
    child_env.update(
        {
            "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": point,
            "CCTALLY_TEST_STATS_REBUILD_MARKER": str(marker),
        }
    )
    rebuild = subprocess.Popen(
        [sys.executable, str(CCTALLY), "db", "rebuild", "--db", "stats"],
        env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _await_marker(marker, rebuild)
        os.kill(rebuild.pid, signal.SIGKILL)
        rebuild.communicate(timeout=30)
    finally:
        if rebuild.poll() is None:
            os.kill(rebuild.pid, signal.SIGKILL)
            rebuild.wait(timeout=30)
    assert rebuild.returncode == -signal.SIGKILL


def test_a_kill_between_replace_and_the_verdict_is_resolved_by_the_next_open(
    tmp_path,
):
    """No scratch remains, so interrupted recovery structurally cannot fire.

    The pending marker is the only thing that tells a later process the
    publication outcome is unknown.
    """
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "rebuild_after_publication_replace")

    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    publication = db.parent / "stats.db.publication"
    state = json.loads(publication.read_text())
    assert state["status"] == "pending"
    record = json.loads(pathlib.Path(state["recordPath"]).read_text())
    assert record["status"] == "pending"

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert not publication.exists(), (
        "the next open must validate the destination and clear the marker"
    )
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        probe.close()


def test_a_kill_before_replace_lets_interrupted_recovery_win(tmp_path):
    """A scratch artifact still takes precedence, and clears the stale marker."""
    env = _isolated_env(tmp_path)
    db = _seed_cli(env)
    _kill_rebuild_at(env, tmp_path, "rebuild_before_cutover")

    assert sorted(db.parent.glob("stats.db.rebuilding-*")), (
        "the kill must leave the scratch artifact this branch is gated on"
    )
    publication = db.parent / "stats.db.publication"
    assert json.loads(publication.read_text())["status"] == "pending"

    result = _record_usage(env)
    assert result.returncode == 0, result.stderr
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == []
    assert not publication.exists()
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        probe.close()


# ==========================================================================
# F1 — a scratch from a LATER run must not discard an earlier run's verdict
# ==========================================================================

class _SimulatedCrash(Exception):
    """Stops a rebuild exactly where a SIGKILL would, in-process."""


def _die_at(jr, point: str):
    """Replace the rebuild's pause seam with one that raises at ``point``."""
    real_pause = jr._stats_rebuild_test_pause

    def pause(reached: str) -> None:
        if reached == point:
            raise _SimulatedCrash(reached)
        real_pause(reached)

    return real_pause, pause


def test_a_later_scratch_must_not_discard_an_earlier_pending_verdict(ns):
    """#496 S1 F1. A crashed rebuild releases its flock, so a scratch on disk
    can belong to a LATER run than the pending marker beside it.

    Run A dies between `os.replace` and the verdict, leaving a pending marker
    and no scratch. Run B then dies during its fold, leaving a scratch and no
    incident. Discarding A's marker because B's scratch exists accepts A's
    published index without ever validating it, which is exactly the
    silent-acceptance class the publication transaction exists to close.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)

    real_pause, dying_pause = _die_at(jr, "rebuild_after_publication_replace")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    marker_path = _publication_marker(_cctally_core.APP_DIR)
    assert json.loads(marker_path.read_text())["status"] == "pending"
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == [], (
        "os.replace consumed run A's scratch, so none can remain"
    )

    # What run A published is bad. Nothing on the ordinary open path notices:
    # the file carries the current epoch, so `open_db`'s zero-DDL fast path
    # returns it, and no scratch exists for interrupted recovery to find.
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("UPDATE journal_cursor SET offset = offset + 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()

    # Run B: started later, died during its fold, left only a scratch. The
    # stamp is well past run A's incident so it cannot be read as evidence of
    # the legacy quarantine-before-build shape.
    stamp = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    ).strftime("%Y%m%dT%H%M%S_%f")
    (db.parent / f"stats.db.rebuilding-{stamp}").write_bytes(b"")

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()

    resolved = json.loads(marker_path.read_text())
    assert resolved["status"] == "failed"
    assert resolved["recordPath"] in str(caught.value)
    record = json.loads(pathlib.Path(resolved["recordPath"]).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False
    assert sorted(db.parent.glob("stats.db.rebuilding-*")) == [], (
        "the stale scratch is still reclaimed"
    )


def _age_quarantine_incidents(app_dir: pathlib.Path, minutes: int) -> None:
    """Restamp every existing incident directory ``minutes`` into the past.

    `_has_completed_stats_quarantine_incident` matches a scratch to an incident
    only when the scratch was created within five minutes AFTER it, which is
    what separates the legacy quarantine-before-build interruption from a Task A
    orphan. Ageing the incidents states the ordinary case — the next rebuild
    starts well after the previous one finished — without which the recovery
    path would rebuild instead of reaching the marker branch under test.
    """
    stamp = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    ).strftime("%Y%m%dT%H%M%S_%f")
    for incident in sorted((app_dir / "quarantine").iterdir()):
        incident.rename(incident.with_name(f"stats.db-{stamp}"))


def test_a_later_publication_must_not_destroy_an_earlier_owed_verdict(ns):
    """#496 S1 F1. Two runs interleave, and the SECOND one destroys the first
    one's owed verdict by overwriting the single marker slot.

    Run A dies between `os.replace` and its verdict, so the marker beside the
    destination still owes a verdict on bytes that are already live. Run B then
    begins a fresh publication — `cmd_db_rebuild` takes maintenance EXCLUSIVE
    without opening the live database through `stats_open_guarded`, so nothing
    made it resolve A's marker first — and dies before its own `os.replace`.
    The next opener then sees a pending marker beside run B's own scratch,
    discards it as never-replaced, and accepts run A's never-validated index.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    marker_path = _publication_marker(_cctally_core.APP_DIR)

    real_pause, dying_pause = _die_at(jr, "rebuild_after_publication_replace")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    run_a = json.loads(marker_path.read_text())
    assert run_a["status"] == "pending"
    run_a_record = run_a["recordPath"]

    # What run A published is invalid. Nothing on the ordinary open path
    # notices: the file carries the current epoch, so `open_db`'s zero-DDL fast
    # path returns it, and `os.replace` consumed run A's scratch.
    raw = sqlite3.connect(str(db))
    try:
        raw.execute("UPDATE journal_cursor SET offset = offset + 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()
    _age_quarantine_incidents(_cctally_core.APP_DIR, 10)

    real_pause, dying_pause = _die_at(jr, "rebuild_before_cutover")
    jr._stats_rebuild_test_pause = dying_pause
    try:
        with pytest.raises(_SimulatedCrash):
            jr.rebuild_stats_index(
                context=jr.RebuildContext(trigger="db-rebuild")
            )
    finally:
        jr._stats_rebuild_test_pause = real_pause

    scratches = sorted(db.parent.glob("stats.db.rebuilding-*"))
    assert scratches, (
        "run B must leave its own scratch, which is what makes its marker look "
        "discardable to the next opener"
    )
    assert json.loads(marker_path.read_text())["scratchPath"] in [
        str(path) for path in scratches
    ], "run B's Phase 1 must have written a marker naming its own scratch"

    with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
        _cctally_core.open_db()
    assert run_a_record in str(caught.value), (
        "the verdict run A owed must survive run B, and must name run A's record"
    )
    record = json.loads(pathlib.Path(run_a_record).read_text())
    assert record["status"] == "failed"
    assert record["postPublicationValidation"]["ok"] is False


def test_a_marker_holding_valid_json_that_is_not_an_object_is_not_a_traceback(
    ns, monkeypatch
):
    """#496 S1 F1. `json.loads` returns whatever the bytes decode to.

    `_raise_settled_publication_failure` runs inside `_stats_heal_hook`'s
    `except Exception`, so an `AttributeError` from calling `.get(...)` on
    `null` escapes the hook entirely and surfaces as a raw traceback from
    `open_db` instead of the guided corruption error. That is the worst blast
    radius of this shape, because it replaces a diagnosis with a stack trace on
    a user whose stats.db is already corrupt.

    The marker is written from inside the failing rebuild rather than up front:
    `stats_open_guarded` resolves any marker that exists before it connects, so
    a marker present at open time never reaches the heal hook at all.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    def refuse(**_kwargs):
        _publication_marker(_cctally_core.APP_DIR).write_text("null")
        raise RuntimeError("injected rebuild failure")

    monkeypatch.setattr(jr, "rebuild_stats_index", refuse)

    with pytest.raises(_cctally_db.StatsDbCorruptError):
        _cctally_core.open_db()


# ==========================================================================
# F1 — the process that CAUSES the failure must not print the false message
# ==========================================================================

def test_the_failing_heal_reports_the_publication_guidance_not_db_repair(ns):
    """#496 S1 F1. Replacement has already occurred, so the pre-existing
    "Not auto-recreated … run `cctally db repair --db stats --yes`" text is
    false. Only the NEXT process used to see the correct guidance, because the
    heal hook's broad `except Exception` swallowed the publication failure.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    _destroy_header_magic(pathlib.Path(_cctally_core.DB_PATH))

    real_validate = jr._validate_rebuilt_stats_index

    def fail_on_the_destination(conn, high_water):
        real_validate(conn, high_water)
        if ".rebuilding-" not in _main_file_of(conn).name:
            raise jr.JournalError("injected post-publication validation failure")

    jr._validate_rebuilt_stats_index = fail_on_the_destination
    try:
        with pytest.raises(_cctally_db.StatsPublicationFailedError) as caught:
            _cctally_core.open_db()
    finally:
        jr._validate_rebuilt_stats_index = real_validate

    marker = json.loads(
        _publication_marker(_cctally_core.APP_DIR).read_text()
    )
    assert marker["status"] == "failed"
    assert marker["recordPath"] in str(caught.value)
    assert "db repair --db stats" not in str(caught.value), (
        "the causing process must not send the user down the pre-cutover path"
    )


# ==========================================================================
# F2 — the WAL capture is scoped to stats, and never breaks a heal
# ==========================================================================

def test_a_classified_cache_corruption_records_skipped_not_stats(ns):
    """#496 S1 F2. The evidence directory is a stats-only artifact.

    Retention for `logs/stats.db-corruption-forensics-<ts>/` is handed to S6;
    nobody owns retention for a cache or conversations family, whose WAL is
    not bounded by stats.db's 16 MiB `journal_size_limit` and has been observed
    above 256 MiB under a multi-agent hook storm (#297).
    """
    import _cctally_core
    import _cctally_db

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    cache = pathlib.Path(_cctally_core.APP_DIR) / "cache.db"
    cache.write_bytes(b"not a database\x00" + b"\x00" * 4080)
    pathlib.Path(f"{cache}-wal").write_bytes(b"\x00" * 4096)

    bundle_path = _cctally_db.write_corruption_forensics(
        cache,
        db_label="cache",
        trigger_origin="cache-open",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
    )
    bundle = json.loads(pathlib.Path(bundle_path).read_text())

    assert bundle["walEvidence"]["disposition"] == "skipped_not_stats"
    assert bundle["walEvidence"]["path"] is None
    assert _evidence_dirs(_cctally_core.LOG_DIR) == []


def test_a_non_oserror_from_the_wal_capture_never_breaks_the_heal(
    ns, tmp_path, monkeypatch
):
    """#496 S1 F2/F8. Enrichment can never break a heal, and the guard has to
    cover more than `OSError`: an escaping exception propagates out of
    `write_corruption_forensics`, and the cache caller's own `except Exception`
    then DECLINES destructive recovery.
    """
    import _cctally_core
    import _cctally_db
    import _cctally_tui

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    _strand_committed_wal_family(db, tmp_path)
    _zero_main_data_pages(db)

    real_copy = _cctally_db._copy_db_family

    def copy_or_boom(src, dst, **kwargs):
        # Scoped to the evidence copy: preservation uses the same helper, and
        # breaking that would test something else entirely.
        if "corruption-forensics" in str(dst):
            raise RuntimeError("injected WAL evidence copy failure")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(_cctally_db, "_copy_db_family", copy_or_boom)

    healed = _cctally_tui._tui_heal_post_query_stats(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    assert healed is True

    conn = _cctally_core.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [7.0]

    evidence = _latest_bundle(_cctally_core.LOG_DIR)["walEvidence"]
    assert evidence["disposition"] == "failed"
    assert "injected WAL evidence copy failure" in evidence["reason"]


def test_a_third_crashed_run_must_not_drop_a_carried_verdict(ns):
    """#496 S1 F1. The carry must be transitive.

    Run A publishes an invalid index and dies before validating it. Run B
    settles A to `failed`, carries that verdict forward in its own marker, and
    dies before its own `os.replace` — so B's scratch is still on disk. Run C
    then reaches `_settle_prior_publication_verdict` and finds a pending marker
    whose OWN scratch still exists, which means B replaced nothing and owes
    nothing about the live bytes.

    Returning `None` there is right about B and wrong about A: it drops the
    verdict B was carrying, and the next opener removes the marker and connects
    to A's never-validated index. The carried block must pass through.
    """
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)

    # Run B's scratch, still on disk because B died before its `os.replace`.
    live_scratch = db.with_name(f"{db.name}.rebuilding-20260805T120000_000000")
    live_scratch.write_bytes(b"")

    run_a_verdict = {
        "schemaVersion": 1,
        "status": "failed",
        "recordPath": "/nonexistent/stats-rebuild-run-a.json",
        "error": "integrity_check reported: database disk image is malformed",
    }
    jr._write_publication_marker(
        db,
        "/nonexistent/stats-rebuild-run-b.json",
        started_at="2026-08-05T12:00:00Z",
        scratch_path=str(live_scratch),
        prior=run_a_verdict,
    )

    carried = jr._settle_prior_publication_verdict(db)

    assert carried is not None, (
        "run A's verdict was dropped: a pending marker whose own scratch "
        "survives still carries an older run's failure"
    )
    assert carried["status"] == "failed"
    assert carried["error"] == run_a_verdict["error"]
    assert carried["recordPath"] == run_a_verdict["recordPath"]
