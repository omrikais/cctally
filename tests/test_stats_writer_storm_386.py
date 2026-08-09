"""stats.db writer-storm durability regressions for issue #386.

Real ``subprocess`` children running the actual CLI, real ``signal.SIGKILL``,
real ``fcntl`` — mirroring ``tests/test_cache_writer_storm_344.py``, which is
the cache.db half of the same problem (#343/#344).

**Isolation is the precondition for everything else here.** A suite whose
purpose is to try to corrupt a ``stats.db`` must be structurally incapable of
reaching ``~/.local/share/cctally``. ``test_storm_suite_cannot_reach_prod``
runs first and asserts the resolved ``APP_DIR`` before any other test spawns a
writer.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import signal
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import types

import pytest

from conftest import load_script

ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"

# spec §4.3 gating. `pytest.ini` registers no custom markers, so the mechanism
# is `skipif` on an env var, following the `CCTALLY_RUN_BENCHMARK` precedent.
SOAK = os.environ.get("CCTALLY_RUN_STORM_SOAK") == "1"

#: Iteration / concurrency knobs. Default = one pass in the normal suite.
STORM_WRITERS = 24 if SOAK else 8
KILL_ROUNDS = 40 if SOAK else 6

_REQUIRED_SYNC_WEEK_CONTENTION = {
    "[cache] sync already in progress; using existing cache",
    (
        "cctally: account attribution unavailable (cache required): "
        "concurrent ingest"
    ),
}
_ALLOWED_SYNC_WEEK_CONTENTION = _REQUIRED_SYNC_WEEK_CONTENTION | {
    (
        "[cache] concurrent ingest in progress; falling back to direct JSONL "
        "parse for correctness"
    ),
}


def _is_expected_writer_cache_contention(detail: dict) -> bool:
    """Recognize only the account-safe refusal documented by #341.

    A scoped ``sync_week`` journal op cannot fall back to identity-less JSONL
    while another process is ingesting the cache. Any authoritative writer can
    pick up that pending op, so the exit may surface from ``record-usage`` as
    well as the process that appended it. That exit-3 refusal is a correctness
    result, not stats.db rollback-journal contention. Exact-line matching and
    the writer-only gate keep reader failures, every SQLite busy/locked error,
    and every other child failure load-bearing in the storm gate.
    """
    lines = {line.strip() for line in detail["stderr"].splitlines() if line.strip()}
    return (
        detail["surface"] == "writer"
        and detail["returncode"] == 3
        and _REQUIRED_SYNC_WEEK_CONTENTION <= lines
        and lines <= _ALLOWED_SYNC_WEEK_CONTENTION
    )


def _storm_env(data_dir: pathlib.Path) -> dict:
    """Environment for a storm child: pinned data dir, dev-autodetect off.

    Five pins, not two. ``CCTALLY_DATA_DIR`` alone moves ``APP_DIR`` but leaves
    the child reading the *host's* real ``~/.claude/projects`` corpus and
    ``$CODEX_HOME`` — which makes ``sync-week`` walk gigabytes of the
    maintainer's transcripts and makes every result non-deterministic. ``HOME``
    is pinned too so the guard's claim ("cannot reach the prod data dir") holds
    even if ``CCTALLY_DATA_DIR`` were ever dropped: with a fake ``HOME`` the
    fallback ``~/.local/share/cctally`` is itself inside ``tmp_path``.
    """
    home = data_dir.parent / "home"
    claude = data_dir.parent / "claude"
    codex = data_dir.parent / "codex-home"
    for path in (data_dir, home, codex, claude / "projects"):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "TZ": "Etc/UTC",
        }
    )
    return env


def _resolved_app_dir(env: dict) -> pathlib.Path:
    """Ask the real binary where it resolved ``APP_DIR`` to."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, %r); import _cctally_core as c; "
            "print(c.APP_DIR)" % str(ROOT / "bin"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return pathlib.Path(out.stdout.strip())


def _integrity_ok(db_path: pathlib.Path) -> "tuple[bool, str]":
    """Open READ-WRITE and run ``integrity_check``.

    NEVER probe with ``mode=ro``: a WAL-mode DB whose ``-shm`` is absent fails
    ``SQLITE_CANTOPEN`` on a read-only open, which is not corruption and has
    twice been misread as such on this project.
    """
    if not db_path.exists():
        return False, f"missing: {db_path}"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    text = "; ".join(str(r[0]) for r in rows)
    return text == "ok", text


def _cctally(env: dict, *args: str, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    return subprocess.run([sys.executable, str(CCTALLY), *args], env=env, **kw)


def _spawn_writer(env: dict, *args: str) -> subprocess.Popen:
    """A detached CLI child whose output is discarded (storm noise)."""
    return subprocess.Popen(
        [sys.executable, str(CCTALLY), *args],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


_UNSANCTIONED_GUARDED_WRITE_CHILD = """
import os
import pathlib
import sys

sys.path.insert(0, sys.argv[1])
import _cctally_core as core
import _cctally_store as store

print(
    "dev_autodetect_suppressed="
    + str(
        os.environ.get("CCTALLY_DISABLE_DEV_AUTODETECT") == "1"
        and not core._is_dev_checkout()
    ),
    flush=True,
)
print(
    "pytest_marker_present="
    + str(bool(os.environ.get("PYTEST_CURRENT_TEST"))),
    flush=True,
)
conn = store.stats_open_guarded(pathlib.Path(sys.argv[2]))
print("guarded_opener_reached=True", flush=True)
print("mutation_attempted=True", flush=True)
conn.execute("CREATE TABLE subprocess_guard_probe (value INTEGER)")
conn.commit()
print("mutation_persisted=True", flush=True)
conn.close()
"""


# ---------------------------------------------------------------------------
# Isolation guard — must pass before anything else spawns a process
# ---------------------------------------------------------------------------


def test_storm_suite_cannot_reach_prod(tmp_path):
    """The suite must be structurally incapable of touching the prod data dir."""
    data_dir = tmp_path / "data"
    env = _storm_env(data_dir)
    resolved = _resolved_app_dir(env)
    assert resolved == data_dir
    assert str(resolved).startswith(str(tmp_path))

    # The stronger claim: even WITHOUT the explicit override, the pinned HOME
    # keeps the fallback inside tmp_path. This is what makes the guard a real
    # structural property rather than a single-variable assertion.
    fallback_env = dict(env)
    fallback_env.pop("CCTALLY_DATA_DIR")
    fallback = _resolved_app_dir(fallback_env)
    assert str(fallback).startswith(str(tmp_path)), fallback
    assert fallback == pathlib.Path(env["HOME"]) / ".local" / "share" / "cctally"


def test_pytest_spawned_storm_child_keeps_stats_guard_fail_loud(tmp_path):
    """The pytest marker inherited through `_storm_env` must keep children strict.

    Dev autodetection is deliberately disabled in storm children, so
    ``PYTEST_CURRENT_TEST`` is the only fail-loud trigger. Exercise the real
    guarded opener and an actual main-schema mutation; a child import/setup
    failure cannot satisfy the precise denial or persistence postcondition.
    """
    data_dir = tmp_path / "data"
    env = _storm_env(data_dir)
    db = data_dir / "stats.db"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _UNSANCTIONED_GUARDED_WRITE_CHILD,
            str(ROOT / "bin"),
            str(db),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    conn = sqlite3.connect(str(db))
    try:
        persisted = conn.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'subprocess_guard_probe'"
        ).fetchone()[0]
    finally:
        conn.close()

    trace = "; ".join(child.stdout.splitlines())
    print(
        "subprocess_guard_acceptance: "
        f"{trace}; returncode={child.returncode}; "
        f"denied={'not authorized' in child.stderr.lower()}; persisted={persisted}"
    )
    assert child.returncode != 0, (
        "the unsanctioned subprocess write was not denied; "
        f"persisted={persisted}\nstdout:\n{child.stdout}\nstderr:\n{child.stderr}"
    )
    assert "not authorized" in child.stderr.lower(), child.stderr
    assert "dev_autodetect_suppressed=True" in child.stdout, child.stdout
    assert "pytest_marker_present=True" in child.stdout, child.stdout
    assert "guarded_opener_reached=True" in child.stdout, child.stdout
    assert "mutation_attempted=True" in child.stdout, child.stdout
    assert persisted == 0, (
        "the denied subprocess schema mutation persisted despite the guard"
    )


def test_helpers_seed_and_verify(tmp_path):
    """The seed + probe helpers work end to end against a real index.

    The assertion is UNCONDITIONAL. The Stage 1 shape guarded it behind
    ``if db.exists():`` and therefore passed vacuously on any run where the file
    was absent (Stage 1 review F7). It is also deliberately NOT written as
    "`doctor` materialises stats.db": Task 8 stops ``_db_status_for`` creating an
    absent stats.db, so an existence assertion keyed on ``doctor`` alone would
    have to be rewritten by that task. Seeding one real tick makes the
    precondition explicit and survives the fix.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 1)
    db = _resolved_app_dir(env) / "stats.db"
    assert db.exists(), f"one real record-usage tick did not create {db}"
    ok, text = _integrity_ok(db)
    assert ok, text
    res = _cctally(env, "doctor", "--json")
    assert res.returncode in (0, 2), res.stderr
    ok, text = _integrity_ok(db)
    assert ok, text


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
#
# `record-usage` has NO `--stdin-json` flag. The real surface is
# `--percent/--resets-at/--five-hour-percent/--five-hour-resets-at`, and BOTH
# epochs are plausibility-banded (`bin/_cctally_record.py:4079`, `:4112`):
# 7d in [now-30d, now+8d], 5h in [now-10m, now+6h]. A fixed historical epoch is
# accepted for 7d but SILENTLY DROPS the 5h fields, leaving
# `five_hour_window_key` NULL — which seeds ZERO `five_hour_blocks` and
# therefore never exercises the very table family the 2026-07-26T07:06:59Z
# field incident damaged. Timestamps here are runtime-relative for that reason.

_FIVE_HOUR_SLOTS = 11          # distinct 5h windows inside the +6h band


def _tick_args(i: int) -> list:
    """One monotonically-advancing tick. Monotonic 7d percent never trips
    reset/credit detection; each integer crossing writes a percent_milestone.

    ``now`` is read HERE, per call — not at module import (Stage 1 review F6).
    The 5h band is only ``[now-10m, now+6h]``: a module-level ``_NOW`` gives the
    whole session a single ~40-minute grace window, so a slow pytest phase (or a
    soak run) silently drops every 5h field, seeds zero ``five_hour_blocks`` and
    fails H1's non-vacuity gate for a reason that has nothing to do with storms.
    """
    now = int(time.time())
    return [
        "record-usage",
        "--percent", str(round(min(99.6, 0.4 * (i + 1)), 4)),
        "--resets-at", str(now + 3 * 86400),
        "--five-hour-percent", str(round((i * 7.3) % 99.0 + 0.1, 4)),
        "--five-hour-resets-at", str(now + 1800 + (i % _FIVE_HOUR_SLOTS) * 1800),
    ]


def _seed_journal(env: dict, ticks: int) -> None:
    """Drive real record-usage ticks so the journal and stats index have content."""
    for i in range(ticks):
        res = _cctally(env, *_tick_args(i))
        assert res.returncode == 0, f"seed tick {i} failed: {res.stderr}"


def _wal_path(db: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(str(db) + "-wal")


def _drain_wal(db: pathlib.Path) -> int:
    """Checkpoint ``stats.db`` and return the resulting ``-wal`` size (0 when the
    sidecar is drained or gone).

    Test scaffolding, deliberately not a production path. A SIGKILLed writer
    STRANDS its WAL on disk — measured: the WAL is not drained when its owner
    dies — so without this the next round starts against a corpse's sidecar and
    a "is the WAL live?" poll answers yes before the new child has even imported
    ``sqlite3``. See ``_kill_when_wal_active``.
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    wal = _wal_path(db)
    try:
        return wal.stat().st_size
    except OSError:
        return 0


def _kill_when_wal_active(
    proc: subprocess.Popen,
    wal: pathlib.Path,
    *,
    baseline: int = 0,
    budget_s: float = 20.0,
) -> "int | None":
    """SIGKILL ``proc`` the instant *its own* writes grow `-wal` past ``baseline``.

    Returns the WAL size observed at kill time, or ``None`` when the process
    finished without the poll ever seeing the WAL grow (i.e. the kill did NOT
    land in a write window). Reporting that distinction is the whole point: per
    #374 a "clean" verdict is only worth what the test could actually observe,
    and a fixed `time.sleep(0.05)` before the kill lands inside the write window
    a few percent of the time at best (measured: the WAL is non-empty for ~5% of
    a record-usage tick).

    **The trigger is ``size > baseline``, never ``size > 0`` (Stage 1 review
    P1).** A SIGKILLed writer leaves its WAL behind, so from round 1 onward a
    ``size > 0`` poll returns the PREVIOUS round's leftover within ~1 ms and
    kills the new child during interpreter startup, before it has opened the DB.
    That failure is invisible to an `assert hits` gate — it passes with zero
    genuine rounds — and its fingerprint is a constant `wal_at_kill_bytes` list
    plus `wal_after_kill == wal_before`. Callers pair this with ``_drain_wal``
    so ``baseline`` is 0 and the observed size is unambiguously the child's.
    """
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            size = wal.stat().st_size
        except OSError:
            size = 0
        if size > baseline:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=30)
            return size
        if proc.poll() is not None:
            return None
        time.sleep(0.0005)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    return None


# ---------------------------------------------------------------------------
# Meta-test for the kill primitive (Stage 1 review P1 regression)
# ---------------------------------------------------------------------------


def test_kill_primitive_observes_the_child_under_test(tmp_path):
    """The kill trigger must attribute the WAL to the CHILD, not to a corpse.

    Non-vacuous by construction: the child here provably never opens stats.db,
    so ANY reported in-window kill is a false positive. Against the pre-fix
    ``size > 0`` primitive this fails on the first poll.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    db = _resolved_app_dir(env) / "stats.db"
    wal = _wal_path(db)

    # A previous round's stranded WAL, reproduced exactly: grow it under a
    # pinning reader, then let the reader die. The sidecar survives.
    leftover = _grow_wal(db, 40)
    assert leftover > 0, "setup failed: no leftover WAL to be fooled by"
    assert wal.exists() and wal.stat().st_size == leftover

    idle = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observed = _kill_when_wal_active(idle, wal, baseline=leftover, budget_s=1.5)
    assert observed is None, (
        f"kill trigger fired on a stale WAL ({observed} bytes) for a child that "
        "never opened stats.db"
    )

    # And the drain the storm rounds rely on really does zero it.
    assert _drain_wal(db) == 0


# ---------------------------------------------------------------------------
# The opener half of the physical-replacement protocol (Task 8, spec 3.1/5.1)
# ---------------------------------------------------------------------------
#
# The pending record's whole claim is "no opener escapes into the live family
# while a replacement is in flight". Adding `strict=True` and an lsof preflight
# to the WRITER without this leaves a TOCTOU window: a new opener can arrive
# after the PID scan returns and before the first rename lands.


def _quarantine_pending_path(db: pathlib.Path) -> pathlib.Path:
    """Resolve the real pending-record path from production, never a literal.

    It is ``stats.db.quarantine-pending.json`` — the plan sketched it without
    the extension — and its CONTENT is schema-validated by
    ``_load_pending_quarantine``, so a hand-written stub is rejected as invalid
    rather than honoured.
    """
    sys.path.insert(0, str(ROOT / "bin"))
    import _cctally_db

    return _cctally_db._quarantine_pending_path(db)


def _publish_pending_quarantine(app: pathlib.Path, db: pathlib.Path) -> pathlib.Path:
    """Publish a VALID pending record naming an incident whose members have not
    moved yet — the exact state a strict quarantine is in between publishing its
    durable record and its first rename."""
    incident = app / "quarantine" / f"{db.name}-386-opener-test"
    incident.mkdir(parents=True, exist_ok=True)
    members = [
        p.name
        for p in (pathlib.Path(f"{db}-wal"), pathlib.Path(f"{db}-shm"), db)
        if p.exists()
    ]
    assert db.name in members, "setup failed: no live main file to quarantine"
    pending = _quarantine_pending_path(db)
    pending.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "originalPath": str(db),
                "incidentPath": str(incident),
                "members": members,
                "createdAtUtc": "2026-07-26T00:00:00Z",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return pending


def _hold_open_handle(db: pathlib.Path) -> subprocess.Popen:
    """A second process holding a real read transaction on the family."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys, time\n"
            "c = sqlite3.connect(sys.argv[1])\n"
            "c.execute('BEGIN')\n"
            "c.execute('SELECT count(*) FROM weekly_usage_snapshots').fetchone()\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    return holder


def _hold_raw_open_handle(db: pathlib.Path) -> subprocess.Popen:
    """A second process holding a plain file handle on the main file.

    Deliberately NOT a SQLite connection: the drain gate is a whole-system
    handle scan, and after #496 S3 the destinations that still reach physical
    replacement are exactly the ones SQLite refuses to open. A holder that
    could only hold a readable database could no longer exercise that gate.
    """
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "fh = open(sys.argv[1], 'rb')\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    return holder


#: Pins a read snapshot, re-reads it once a gate file appears, then ends the
#: transaction and reads again. Three numbers from ONE process are what make
#: cross-process snapshot isolation observable.
_SNAPSHOT_READER = """
import pathlib, sqlite3, sys, time
db, gate = sys.argv[1:3]
conn = sqlite3.connect(db)
conn.execute('BEGIN DEFERRED')
print('pinned %d' % conn.execute(
    'SELECT count(*) FROM weekly_usage_snapshots').fetchone()[0], flush=True)
deadline = time.monotonic() + 180
while time.monotonic() < deadline and not pathlib.Path(gate).exists():
    time.sleep(0.05)
print('during %d' % conn.execute(
    'SELECT count(*) FROM weekly_usage_snapshots').fetchone()[0], flush=True)
conn.rollback()
print('after %d' % conn.execute(
    'SELECT count(*) FROM weekly_usage_snapshots').fetchone()[0], flush=True)
"""


def _snapshot_reader(db: pathlib.Path, gate: pathlib.Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _SNAPSHOT_READER, str(db), str(gate)],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_stats_open_respects_quarantine_pending_record(tmp_path):
    """A pending quarantine record must block a new stats opener.

    Held open by another process, so the correct outcome is an explicit refusal
    — resuming the quarantine under a live mapping is the failure this whole
    session is about. Before the fix `open_db` never reads the record at all and
    `report` exits 0 straight into the family being replaced.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    assert db.exists()

    holder = _hold_open_handle(db)
    try:
        pending = _publish_pending_quarantine(app, db)
        assert pending.exists()
        res = _cctally(env, "report")
        assert res.returncode != 0, (
            "opener ignored the pending quarantine record: " + res.stdout
        )
        blob = (res.stderr + res.stdout).lower()
        assert "maintenance" in blob or "quarantine" in blob, blob
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    ok, text = _integrity_ok(db)
    assert ok, text


def _doctor_stats_file_refused(res: subprocess.CompletedProcess) -> bool:
    """`doctor --json` reports the stats.db file check as a failed open.

    Keyed on the exact check (`db.stats.file`) rather than on a substring of the
    whole payload: a healthy doctor report already contains the word
    "maintenance" elsewhere, so a blanket text match is a false positive — which
    is how the first draft of this helper failed its own control run.
    """
    try:
        payload = json.loads(res.stdout)
    except (ValueError, TypeError):
        return False
    for category in payload.get("categories", []):
        for check in category.get("checks", []):
            if check.get("id") == "db.stats.file":
                return (
                    check.get("severity") == "fail"
                    and "could not open" in (check.get("summary") or "")
                )
    return False


def _db_status_stats_refused(res: subprocess.CompletedProcess) -> bool:
    try:
        payload = json.loads(res.stdout)
    except (ValueError, TypeError):
        return False
    return bool(payload["databases"]["stats.db"].get("_open_error"))


def _db_backup_refused(res: subprocess.CompletedProcess) -> bool:
    return res.returncode == 3


@pytest.mark.parametrize(
    "argv,refused",
    [
        (("doctor", "--json"), _doctor_stats_file_refused),
        (("db", "status", "--json"), _db_status_stats_refused),
        (("db", "backup", "--db", "stats"), _db_backup_refused),
    ],
    ids=["doctor", "db-status", "db-backup"],
)
def test_raw_stats_openers_participate_in_the_protocol(tmp_path, argv, refused):
    """The three RAW stats openers must observe the pending record too.

    Routing only ``open_db`` is insufficient — spec 3.1's third clause says
    EVERY opener participates, and Stage 1's inventory found these bypassing it
    (`_cctally_doctor.py:507`/`:889`, `_cctally_db.py:7930`/`:6732`). A
    diagnostic that connects straight through defeats the guard.

    **The assertion is that each command visibly DECLINES**, not merely that it
    left the inode alone. A first draft asserted only "the live main file was
    not renamed away", which every one of these commands satisfies *by ignoring
    the record entirely* — it passed on the unfixed code and proved nothing. The
    inode check is retained as a second, weaker guard: no opener may resume a
    quarantine while a handle is live.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    before = db.stat().st_ino

    holder = _hold_open_handle(db)
    try:
        # Control: with no pending record the command must NOT be reporting a
        # refusal, so the positive assertion below cannot be satisfied by some
        # unrelated standing message. (`db backup` writes a real backup here;
        # that is fine, the destination is under tmp_path.)
        control = _cctally(env, *argv)
        assert not refused(control), (
            f"{argv} already reports the refusal with NO pending record; the "
            "assertion below would be vacuous: "
            + (control.stdout + control.stderr)[:400]
        )

        _publish_pending_quarantine(app, db)
        res = _cctally(env, *argv)
        assert refused(res), (
            f"{argv} opened the live stats family straight through a pending "
            "quarantine record: " + (res.stdout + res.stderr)[:600]
        )
        assert db.exists(), f"{argv} let the live main file be renamed away"
        assert db.stat().st_ino == before, (
            f"{argv} replaced the live stats.db inode while a handle was open"
        )
        assert "Traceback" not in res.stderr, res.stderr
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    ok, text = _integrity_ok(db)
    assert ok, text


# ---------------------------------------------------------------------------
# Strict quarantine + the handle-drain gate (Task 9, spec 5.1)
# ---------------------------------------------------------------------------


def test_quarantine_declines_when_family_has_live_handles(tmp_path):
    """Physical replacement must fail soft while a handle is open, not proceed.

    All three stats replacement paths (auto-heal, epoch resolver,
    ``db rebuild``) took the NON-strict ``quarantine_db_family`` default and no
    handle gate at all: three best-effort renames under a live mapping, which is
    exactly the point at which SQLite's crash guarantees stop applying
    (spec 1.2). ``db rebuild --db stats`` is the one of the three an operator can
    trigger deterministically, so it is the one under test.

    #496 S3 narrowed WHEN physical replacement runs, not what it must do when
    it does. A readable destination now publishes in place and correctly does
    NOT decline (see ``test_h4_live_handle_spans_rebuild_swap``), so the
    destination is corrupted first to reach the fallback the drain gate still
    guards. Without that the property would silently stop being tested.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    _drain_wal(db)
    _corrupt_header(db)
    before_ino = db.stat().st_ino
    before_bytes = db.read_bytes()

    holder = _hold_raw_open_handle(db)
    try:
        res = _cctally(env, "db", "rebuild", "--db", "stats")
        assert res.returncode != 0, (
            "rebuild proceeded with a live handle open: " + res.stdout
        )
        assert "declined" in (res.stderr + res.stdout).lower(), res.stderr
        assert db.exists(), "rebuild renamed the live main file away anyway"
        assert db.stat().st_ino == before_ino, (
            "rebuild replaced the live stats.db inode under an open handle"
        )
        assert db.read_bytes() == before_bytes, (
            "a declined replacement still mutated the family"
        )
        assert _quarantine_incidents(app) == [], (
            "a declined replacement still preserved: "
            + str(_quarantine_incidents(app))
        )
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    # Control: with the handle gone the SAME command must succeed, so the
    # assertion above is about the live handle and not about `db rebuild` being
    # broken outright. It also proves the publisher's own descriptor is absent
    # from the drain scan, because the run would otherwise decline against
    # itself.
    res = _cctally(env, "db", "rebuild", "--db", "stats")
    assert res.returncode == 0, res.stderr
    ok, text = _integrity_ok(_resolved_app_dir(env) / "stats.db")
    assert ok, text


# ---------------------------------------------------------------------------
# The four administrative lock corrections (Task 10, spec 1.1 Gap D / 5.3)
# ---------------------------------------------------------------------------
#
# `db rederive` is the reference implementation — it takes stats maintenance
# exclusive in lock order. These four did not:
#
#   db vacuum --db stats   serialized on CACHE_LOCK_MAINTENANCE_PATH, i.e. on
#                          the WRONG DATABASE'S lock, while rewriting the entire
#                          stats file under `locking_mode=EXCLUSIVE`. A
#                          standalone corruption vector and, on its own, a
#                          plausible cause of the field events.
#   db checkpoint --stats  no advisory lock at all (the flock branch is gated
#                          `if which == "cache"`).
#   db skip / db unskip    `_acquire_cache_admin_writer_flocks` returns [] for
#                          every non-cache DB, then they run raw DDL/DML and
#                          `PRAGMA user_version = 0`.
#   db repair              its marker + an lsof scan, but not the maintenance
#                          lock, so it can race a command that honours the lock
#                          and ignores the marker.


def _hold_stats_maintenance_lock(app: pathlib.Path) -> subprocess.Popen:
    """Another process holding ``stats.db.maintenance.lock`` EXCLUSIVE."""
    lock = app / "stats.db.maintenance.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            "fh = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(fh, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(120)\n",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    return holder


def _stats_migration_name() -> str:
    sys.path.insert(0, str(ROOT / "bin"))
    import _cctally_db

    return "stats.db:" + _cctally_db._STATS_MIGRATIONS[-1].name


@pytest.mark.parametrize(
    "argv_factory,busy_rc",
        [
            (lambda: ("db", "vacuum", "--db", "stats"), 3),
            (lambda: ("db", "skip", _stats_migration_name()), 3),
        (lambda: ("db", "unskip", _stats_migration_name()), 3),
        (lambda: ("db", "repair", "--db", "stats", "--yes"), 3),
    ],
        ids=["vacuum", "skip", "unskip", "repair"],
)
def test_admin_commands_serialize_on_the_stats_maintenance_lock(
    tmp_path, argv_factory, busy_rc
):
    """Every administrative stats mutator must honour stats.db.maintenance.lock.

    Non-vacuity: each case also runs the SAME command with the lock free and
    requires a DIFFERENT exit code, so "it exits 3 no matter what" cannot pass.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    argv = argv_factory()

    holder = _hold_stats_maintenance_lock(app)
    try:
        res = _cctally(env, *argv)
        assert res.returncode == busy_rc, (
            f"{argv} ran while stats.db.maintenance.lock was held EXCLUSIVE "
            f"(rc={res.returncode}) — it is serializing on the wrong lock, or "
            "on none at all: " + (res.stdout + res.stderr)[:500]
        )
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    ok, text = _integrity_ok(db)
    assert ok, text

    free = _cctally(env, *argv)
    assert free.returncode != busy_rc, (
        f"{argv} returns {busy_rc} with the lock FREE too, so the assertion "
        "above proves nothing: " + (free.stdout + free.stderr)[:500]
    )


# ---------------------------------------------------------------------------
# H1 — multi-writer baseline (the control case)
# ---------------------------------------------------------------------------


def test_h1_multiwriter_baseline_stays_intact(tmp_path):
    """Production-shaped rollback storm: live integrity, latency, no sidecars."""
    env = _storm_env(tmp_path / "data")
    env["CCTALLY_TEST_DISABLE_STATS_AUTO_HEAL"] = "1"
    _seed_journal(env, 3)
    db = _resolved_app_dir(env) / "stats.db"
    assert db.exists()

    stop_probe = threading.Event()
    probe = {"quick": 0, "full": 0, "errors": [], "sidecars": []}

    def integrity_probe() -> None:
        iteration = 0
        while not stop_probe.is_set():
            try:
                for suffix in ("-wal", "-shm"):
                    if pathlib.Path(f"{db}{suffix}").exists():
                        probe["sidecars"].append(suffix)
                conn = sqlite3.connect(str(db), timeout=15)
                try:
                    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    probe["quick"] += 1
                    if iteration % 4 == 0:
                        assert conn.execute(
                            "PRAGMA integrity_check"
                        ).fetchone()[0] == "ok"
                        probe["full"] += 1
                finally:
                    conn.close()
            except Exception as exc:  # pragma: no cover - asserted below
                probe["errors"].append(f"{type(exc).__name__}: {exc}")
            iteration += 1
            time.sleep(0.01)

    probe_thread = threading.Thread(
        target=integrity_probe, name="stats-integrity-probe", daemon=True
    )
    probe_thread.start()

    started = time.monotonic()
    process_details = {}
    writer_procs = []
    writer_specs = [tuple(_tick_args(3 + i)) for i in range(STORM_WRITERS)]
    writer_specs.extend([("sync-week",), ("sync-week",)])
    for argv in writer_specs:
        process = subprocess.Popen(
            [sys.executable, str(CCTALLY), *argv],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        writer_procs.append(process)
        process_details[process] = ("writer", argv)

    status_payload = json.dumps({
        "model": {"display_name": "Sonnet", "id": "claude-sonnet-4-5"},
        "cost": {"total_cost_usd": 0.0},
    })
    hook_payload = json.dumps({
        "hook_event_name": "PostToolBatch",
        "session_id": "538-storm",
        "transcript_path": "",
        "cwd": str(tmp_path),
    })
    reader_specs = (
        ("statusline", ("statusline",), status_payload),
        ("hook-tick", ("hook-tick", "--foreground", "--no-oauth"), hook_payload),
        ("report", ("report",), None),
    )
    reader_procs = []
    for surface, argv, payload in reader_specs:
        for _ in range(2):
            process = subprocess.Popen(
                [sys.executable, str(CCTALLY), *argv],
                env=env,
                stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            if payload is not None:
                assert process.stdin is not None
                process.stdin.write(payload)
                process.stdin.close()
            reader_procs.append((surface, process))
            process_details[process] = (surface, argv)

    active = {
        process: ("writer", time.monotonic()) for process in writer_procs
    }
    active.update({
        process: (surface, time.monotonic())
        for surface, process in reader_procs
    })
    latencies = {"writer": [], "statusline": [], "hook-tick": [], "report": []}
    deadline = time.monotonic() + 180
    while active and time.monotonic() < deadline:
        now = time.monotonic()
        for process, (surface, process_started) in list(active.items()):
            if process.poll() is not None:
                latencies[surface].append(now - process_started)
                del active[process]
        time.sleep(0.01)
    stop_probe.set()
    probe_thread.join(timeout=30)
    assert not probe_thread.is_alive()
    assert not active, f"storm processes exceeded 180 s: {list(active)}"
    all_procs = writer_procs + [process for _, process in reader_procs]
    returncodes = [process.returncode for process in all_procs]
    failures = []
    for process in all_procs:
        assert process.stderr is not None
        stderr = process.stderr.read()
        process.stderr.close()
        if process.returncode != 0:
            surface, argv = process_details[process]
            failures.append({
                "surface": surface,
                "argv": argv,
                "returncode": process.returncode,
                "stderr": stderr[-2000:],
            })
    expected_cache_contention = [
        detail for detail in failures
        if _is_expected_writer_cache_contention(detail)
    ]
    unexpected_failures = [
        detail for detail in failures
        if not _is_expected_writer_cache_contention(detail)
    ]
    successful_sync_weeks = sum(
        process.returncode == 0
        for process in writer_procs
        if process_details[process][1] == ("sync-week",)
    )
    latency_summary = {}
    for surface, values in latencies.items():
        ordered = sorted(values)
        latency_summary[surface] = {
            "p50": statistics.median(ordered),
            "p95": ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)],
            "max": max(ordered),
        }

    baseline_summary = {}
    for surface, argv, payload in reader_specs:
        samples = []
        for _ in range(2):
            baseline_started = time.monotonic()
            result = subprocess.run(
                [sys.executable, str(CCTALLY), *argv],
                env=env,
                input=payload,
                stdin=subprocess.DEVNULL if payload is None else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0
            samples.append(time.monotonic() - baseline_started)
        baseline_summary[surface] = {
            "p50": statistics.median(samples),
            "p95": max(samples),
            "max": max(samples),
        }
    print(
        "[538-storm] "
        f"processes={len(all_procs)} "
        f"expectedCacheContention={len(expected_cache_contention)} "
        f"unexpectedFailures={len(unexpected_failures)} "
        f"successfulSyncWeeks={successful_sync_weeks} "
        f"elapsed={time.monotonic() - started:.3f}s "
        f"quick={probe['quick']} integrity={probe['full']} "
        f"stormLatency={json.dumps(latency_summary, sort_keys=True)} "
        f"baselineLatency={json.dumps(baseline_summary, sort_keys=True)}"
    )
    if failures:
        print("[538-storm-failures] " + json.dumps(failures, sort_keys=True))
    assert unexpected_failures == [], unexpected_failures[0]
    assert successful_sync_weeks >= 1, failures or returncodes
    assert probe["quick"] >= 2
    assert probe["full"] >= 1
    assert probe["errors"] == []
    assert probe["sidecars"] == []
    # The concurrent writers are the scheduler-load control.  A fixed 2 s
    # comparison floor makes a healthy run fail on a constrained host even
    # when readers finish no slower than the writers creating the contention.
    # Keep the independent 5 s reader SLA, and compare relative slowdown
    # against both the uncontended reader and the concurrent writer cohort.
    scheduler_floor = latency_summary["writer"]["p95"] * 1.25
    for surface in ("statusline", "hook-tick", "report"):
        assert latency_summary[surface]["p95"] < 5.0, latency_summary
        assert latency_summary[surface]["p95"] <= max(
            2.0, baseline_summary[surface]["p95"] * 4, scheduler_floor
        ), (surface, latency_summary, baseline_summary)

    ok, text = _integrity_ok(db)
    assert ok, f"baseline storm corrupted stats.db: {text}"

    # Non-vacuity: the storm must actually have written. A suite that silently
    # no-ops every child would pass the integrity assertion trivially.
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0]
        blocks = conn.execute(
            "SELECT count(*) FROM five_hour_blocks").fetchone()[0]
    finally:
        conn.close()
    assert rows >= 4, f"storm wrote nothing: {rows} snapshots"
    assert blocks >= 1, f"storm never exercised five_hour_blocks: {blocks}"
    assert not pathlib.Path(str(db) + "-wal").exists()
    assert not pathlib.Path(str(db) + "-shm").exists()


# ---------------------------------------------------------------------------
# H2 — SIGKILL mid-transaction (characterisation, not an expected RED)
# ---------------------------------------------------------------------------


def test_h2_sigkill_mid_transaction_recovers_clean(tmp_path):
    """SIGKILL with a hot rollback journal preserves the committed generation."""
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    db = _resolved_app_dir(env) / "stats.db"
    before = db.read_bytes()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys, time\n"
            "c=sqlite3.connect(sys.argv[1], timeout=15)\n"
            "assert c.execute('PRAGMA journal_mode').fetchone()[0]=='delete'\n"
            "c.execute('BEGIN IMMEDIATE')\n"
            "c.execute('UPDATE weekly_usage_snapshots SET weekly_percent=99')\n"
            "print('ROLLBACK-HOT', flush=True)\n"
            "time.sleep(300)\n",
            str(db),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ROLLBACK-HOT"
        journal = pathlib.Path(str(db) + "-journal")
        assert journal.exists() and journal.stat().st_size > 0
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
        if child.stdout is not None:
            child.stdout.close()

    probe = sqlite3.connect(str(db))
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert probe.execute(
            "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots"
        ).fetchone()[0] != 99
    finally:
        probe.close()
    assert db.read_bytes() == before
    assert not pathlib.Path(str(db) + "-wal").exists()
    assert not pathlib.Path(str(db) + "-shm").exists()


# ---------------------------------------------------------------------------
# H3 — SIGKILL mid-checkpoint (characterisation; Stage 3's policy input)
# ---------------------------------------------------------------------------


def _grow_wal(db: pathlib.Path, commits: int, pad: int = 400) -> int:
    """Grow ``stats.db-wal`` past the 1000-page autocheckpoint threshold while a
    reader pins it, and return the resulting WAL size.

    Test-local scaffolding on a scratch DB under ``tmp_path`` — the only shape
    in which the stats WAL grows at all. Measured: with no holder, or with a
    merely-open autocommit connection, the WAL plateaus at ~4 MiB (~1000
    frames); only an OPEN READ TRANSACTION defeats the autocheckpoint.
    """
    transition = sqlite3.connect(str(db))
    try:
        assert transition.execute(
            "PRAGMA journal_mode=WAL"
        ).fetchone()[0] == "wal"
    finally:
        transition.close()

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sqlite3, sys, time\n"
         "c = sqlite3.connect(sys.argv[1])\n"
         "c.execute('BEGIN')\n"
         "c.execute('SELECT count(*) FROM weekly_usage_snapshots').fetchone()\n"
         "print('held', flush=True)\n"
         "time.sleep(300)\n",
         str(db)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            for i in range(commits):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO weekly_usage_snapshots "
                    "(captured_at_utc, week_start_date, week_end_date, "
                    " weekly_percent, source, payload_json, account_key) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("2026-07-26T00:00:%02dZ" % (i % 60), "2026-07-20",
                     "2026-07-26", float(i % 100), "storm-386",
                     '{"pad":"' + "x" * pad + '"}', "unattributed"),
                )
                conn.commit()
        finally:
            conn.close()
        return _wal_path(db).stat().st_size
    finally:
        holder.kill()
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()


#: A child that arms, announces, and then runs a TRUNCATE checkpoint. The
#: announcement is the LAST statement before the PRAGMA, so a kill shortly after
#: the parent reads it lands inside the checkpoint's page-copy loop.
_CHECKPOINTER = (
    "import sqlite3, sys\n"
    "c = sqlite3.connect(sys.argv[1])\n"
    "c.execute('PRAGMA busy_timeout=15000')\n"
    "print('CHECKPOINTING', flush=True)\n"
    "c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()\n"
    "c.close()\n"
)


def _wait_for_stats_maintenance_hold(
    app: pathlib.Path,
    child: subprocess.Popen,
    *,
    timeout_s: float = 10.0,
) -> bool:
    """Observe the checkpoint CLI's shared maintenance-lock acquisition."""
    lock_fd = os.open(
        str(app / "stats.db.maintenance.lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return False
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            time.sleep(0.005)
        return False
    finally:
        os.close(lock_fd)


@pytest.mark.skip(reason="#538 retired the live stats WAL checkpoint path")
def test_h3_sigkill_mid_checkpoint_recovers_clean(tmp_path):
    """SIGKILL a real `db checkpoint --db stats` mid-TRUNCATE on a grown WAL.

    EXPECTED CLEAN — SQLite checkpoints are crash-restartable. The value is
    characterisation: this is the only stats path that runs a large checkpoint,
    and it is the one Stage 3's policy has to reason about.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    db = app / "stats.db"

    interrupted = 0
    grown = 0
    for r in range(KILL_ROUNDS):
        assert _drain_wal(db) == 0
        reader = sqlite3.connect(str(db))
        reader.execute("BEGIN")
        reader.execute(
            "SELECT count(*) FROM weekly_usage_snapshots"
        ).fetchone()
        try:
            size = _grow_wal(db, 900)
            grown = max(grown, size)
            assert size > 4 * 1024 * 1024
            p = _spawn_writer(env, "db", "checkpoint", "--db", "stats")
            armed = _wait_for_stats_maintenance_hold(app, p)
            assert armed, (
                "checkpoint child exited before acquiring the stats maintenance "
                "lock, so the kill window was never reached"
            )
        finally:
            reader.close()

        # The read transaction pinned every grown WAL frame. Observing the
        # maintenance hold proves the real CLI reached its checkpoint path;
        # releasing the reader starts the drain without relying on scheduler
        # timing from Popen.
        time.sleep(0.0005 + (r % 6) * 0.002)
        if p.poll() is None:
            p.send_signal(signal.SIGKILL)
            interrupted += 1
        p.wait(timeout=60)
        ok, text = _integrity_ok(db)
        assert ok, f"H3 round {r} corrupted stats.db: {text}"

    res = _cctally(env, "doctor", "--json")
    assert res.returncode in (0, 2), res.stderr
    ok, text = _integrity_ok(db)
    assert ok, f"H3 produced corruption: {text}"
    print(f"[H3-leg1] rounds={KILL_ROUNDS} "
          f"cmd_interrupted={interrupted} grown_wal_bytes={grown}")
    assert interrupted, (
        "H3 leg 1 is vacuous: every `db checkpoint` child exited before its "
        "kill delay elapsed, so the command was never interrupted"
    )

    # ---- second, sharper leg -------------------------------------------
    # The rounds above interrupt the `db checkpoint` COMMAND, but ~300 ms of
    # CLI startup dwarfs a ~15 ms checkpoint, so a 1-15 ms delay after Popen
    # almost certainly lands during startup rather than inside the page-copy
    # loop. Claiming "mid-checkpoint" on that alone would be exactly the #374
    # failure. This leg announces itself from INSIDE the child, one statement
    # before the PRAGMA, so the kill provably lands in the checkpoint.
    inside = 0
    for r in range(KILL_ROUNDS):
        size = _wal_path(db).stat().st_size if _wal_path(db).exists() else 0
        if size < 4 * 1024 * 1024:
            size = _grow_wal(db, 900)
        child = subprocess.Popen(
            [sys.executable, "-c", _CHECKPOINTER, str(db)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            line = child.stdout.readline().strip()
            assert line == "CHECKPOINTING", f"child never armed: {line!r}"
            time.sleep(0.0005 + (r % 6) * 0.002)   # 0.5-10.5 ms into a ~15 ms op
            if child.poll() is None:
                child.send_signal(signal.SIGKILL)
                inside += 1
            child.wait(timeout=60)
        finally:
            if child.stdout is not None:
                child.stdout.close()
        ok, text = _integrity_ok(db)
        assert ok, f"H3 in-checkpoint round {r} corrupted stats.db: {text}"

    print(f"[H3] rounds={KILL_ROUNDS} cmd_interrupted={interrupted} "
          f"in_checkpoint_kills={inside} grown_wal_bytes={grown}")
    assert inside, (
        "H3 second leg is vacuous: every checkpoint completed before the kill, "
        "so the page-copy loop was never interrupted"
    )


# ---------------------------------------------------------------------------
# H4 — physical replacement racing live openers (spec 4.2, the PRIMARY hypothesis)
# ---------------------------------------------------------------------------
#
# H1-H3 are characterisation: SQLite's own crash guarantees hold, and there is
# no variant of them that SHOULD corrupt. H4 is the one that can, because it is
# the part cctally owns — renaming and swapping a live file family underneath
# processes that have it mapped. Once we do per-file surgery on a live family,
# SQLite's guarantees stop applying (spec 1.2).


def _no_orphan_sidecars(db: pathlib.Path) -> bool:
    """A surviving `-wal` must not predate the main file's CURRENT inode.

    The plan sketched this as ``wal.st_mtime >= db.st_mtime``, which is wrong and
    failed on its first real run: a perfectly healthy pinned WAL is older than a
    main file that ordinary reads have since touched, so the predicate reported
    an orphan where nothing had been replaced. What actually makes a sidecar an
    orphan is belonging to a PREVIOUS GENERATION of the main file — the
    cross-generation pairing spec 1.2 identifies — so the comparison is against
    the main inode's BIRTH time, not its mtime.

    Where the platform exposes no birth time the check degrades to "no claim"
    rather than to a guess; a test that silently swaps in a weaker predicate is
    worse than one that says it cannot tell.
    """
    wal = _wal_path(db)
    if not wal.exists():
        return True
    birth = getattr(db.stat(), "st_birthtime", None)
    if birth is None:
        return True
    return wal.stat().st_mtime >= birth


def _quarantine_incidents(app: pathlib.Path) -> list:
    root = app / "quarantine"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _corrupt_header(db: pathlib.Path) -> None:
    """Destroy the SQLite header magic in place.

    Deliberately NOT a truncate and NOT a delete: the classifier the auto-heal
    is gated on (`_is_sqlite_corruption_error`) must see a genuine
    corruption/NOTADB, never a missing file or a permission error.
    """
    with open(db, "r+b") as fh:
        fh.write(b"\x00" * 16)


def _storm_pause_env(env: dict, point: str, marker: pathlib.Path) -> dict:
    out = dict(env)
    out["CCTALLY_TEST_STATS_STORM_PAUSE_AT"] = point
    out["CCTALLY_TEST_STATS_STORM_MARKER"] = str(marker)
    return out


def _await_marker(marker: pathlib.Path, proc: subprocess.Popen,
                  budget_s: float = 30.0) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return
        assert proc.poll() is None, (
            f"child exited (rc={proc.returncode}) before reaching the pause point"
        )
        time.sleep(0.01)
    raise AssertionError(f"child never reached the pause point ({marker})")


def test_h4_new_opener_after_pid_scan(tmp_path):
    """A FRESH opener arriving after the handle scan and before the first rename.

    This is spec 1.1 Gap A's TOCTOU stated exactly: adding `strict=True` and an
    lsof preflight to the WRITER is not enough, because the scan's answer is
    stale the instant it returns. Only the opener half — maintenance-SHARED held
    across the marker/pending checks AND the connect — closes it.

    The replacement is stopped at `stats_replace_drained`, i.e. after the scan
    said "drained" and before any file moved. A new opener must NOT reach the
    live family while it is parked there.

    #496 S3: a readable destination publishes in place and never reaches that
    seam, so the destination is corrupted first to reach the physical fallback
    the seam belongs to. Without that the test would pass while exercising
    nothing.

    **The property is EXCLUSION, not blocking.** Stage 2 asserted the opener
    stayed at `poll() is None` for 3 s, which pinned the UNBOUNDED wait as
    intended behaviour — the very P1 Task 12.5 fixed. Post-fix the opener waits
    at most `_STATS_OPEN_MAINTENANCE_WAIT_S` and then DECLINES with the
    maintenance error, which satisfies the TOCTOU guarantee just as well: what
    must never happen is a successful open, not a fast failure.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    _drain_wal(db)
    _corrupt_header(db)
    marker = tmp_path / "paused.pid"

    rebuild = subprocess.Popen(
        [sys.executable, str(CCTALLY), "db", "rebuild", "--db", "stats"],
        env=_storm_pause_env(env, "stats_replace_drained", marker),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    opener = None
    opener_rc = None
    opener_err = ""
    try:
        _await_marker(marker, rebuild)
        # The replacement is parked mid-protocol. Race an opener into it.
        opener = subprocess.Popen(
            [sys.executable, str(CCTALLY), "report"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if opener.poll() is not None:
                break
            time.sleep(0.05)
        opener_rc = opener.poll()
        if opener_rc is not None:
            opener_out, opener_err = opener.communicate(timeout=30)
            assert opener_rc != 0, (
                "a NEW opener completed while a replacement was parked between "
                "its drain scan and its first rename — the TOCTOU window is "
                f"still open: {opener_out}"
            )
            # Attribute the refusal, so an unrelated crash cannot satisfy it.
            assert "maintenance" in opener_err.lower(), opener_err
    finally:
        rebuild.send_signal(signal.SIGCONT)
        rebuild.wait(timeout=120)
        if opener is not None and opener.poll() is None:
            opener.communicate(timeout=120)

    assert rebuild.returncode == 0
    # Control: with the replacement finished, the SAME command succeeds. Without
    # this, "the opener was excluded" could be satisfied by an opener that is
    # simply broken.
    res = _cctally(env, "report")
    assert res.returncode == 0, res.stderr

    ok, text = _integrity_ok(db)
    assert ok, text
    assert _no_orphan_sidecars(db)
    assert len(_quarantine_incidents(app)) == 1, _quarantine_incidents(app)


def test_h4_resume_of_partial_quarantine(tmp_path):
    """A half-completed strict quarantine must be COMPLETED, never recreated.

    Constructed by hand rather than by killing a real one: the state under test
    is "pending record published, some members moved, owner gone", and building
    it directly makes the assertion about the RESUME rather than about winning a
    kill race.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    app = _resolved_app_dir(env)
    db = app / "stats.db"

    # Drain first so -wal/-shm are not part of the family snapshot; then publish
    # the record and move exactly ONE member (the -wal if present, else main).
    _drain_wal(db)
    incident = app / "quarantine" / f"{db.name}-386-partial"
    incident.mkdir(parents=True, exist_ok=True)
    members = [
        p.name
        for p in (pathlib.Path(f"{db}-wal"), pathlib.Path(f"{db}-shm"), db)
        if p.exists()
    ]
    pending = _quarantine_pending_path(db)
    pending.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "originalPath": str(db),
                "incidentPath": str(incident),
                "members": members,
                "createdAtUtc": "2026-07-26T00:00:00Z",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if len(members) > 1:
        first = members[0]
        os.replace(str(app / first), str(incident / first))

    # A normal open must finish the SAME incident.
    res = _cctally(env, *_tick_args(900))
    assert res.returncode == 0, res.stderr

    assert not pending.exists(), "pending record survived the resume"
    incidents = _quarantine_incidents(app)
    assert incidents == [incident.name], (
        f"resume created a second incident instead of completing the first: "
        f"{incidents}"
    )
    assert (incident / db.name).exists(), "the main file never reached the incident"
    assert (incident / "manifest.json").exists()

    db = _resolved_app_dir(env) / "stats.db"
    ok, text = _integrity_ok(db)
    assert ok, text
    assert _no_orphan_sidecars(db)

    # Non-vacuity (Stage 2 review): a resume that quarantined the family and
    # then left an EMPTY index behind would satisfy every assertion above —
    # `integrity_check` is happy with an empty file. Pin that the post-resume
    # index actually carries the journal's rows.
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert rows >= 4, f"the resume left an empty index: {rows} snapshots"


def test_h4_live_handle_spans_rebuild_swap(tmp_path):
    """A rollback-journal reader makes writers fail soft without replacement."""
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    ino_before = db.stat().st_ino
    gate = tmp_path / "published.gate"

    reader = _snapshot_reader(db, gate)
    try:
        pinned = reader.stdout.readline().strip()
        assert pinned.startswith("pinned "), pinned
        pinned_rows = int(pinned.split()[1])
        assert pinned_rows > 0, "the reader pinned an empty index"

        res = _cctally(env, *_tick_args(4))
        assert res.returncode != 0
        assert "locked" in res.stderr.lower()
        assert db.stat().st_ino == ino_before
        gate.write_text("go\n")

        during = reader.stdout.readline().strip()
        assert during == f"during {pinned_rows}", (
            f"the pinned reader observed the swap: {during!r} "
            f"(pinned {pinned_rows})"
        )
        after = reader.stdout.readline().strip()
        assert after == f"after {pinned_rows}"
    finally:
        if reader.poll() is None:
            reader.send_signal(signal.SIGKILL)
        reader.wait(timeout=30)
        if reader.stdout is not None:
            reader.stdout.close()

    assert _quarantine_incidents(app) == [], (
        "an in-place publish never preserves: "
        + str(_quarantine_incidents(app))
    )
    for i in range(4, 6):
        res = _cctally(env, *_tick_args(i))
        assert res.returncode == 0, res.stderr
    res = _cctally(env, "db", "rebuild", "--db", "stats")
    assert res.returncode == 0, res.stderr
    db = _resolved_app_dir(env) / "stats.db"
    ok, text = _integrity_ok(db)
    assert ok, text
    assert _no_orphan_sidecars(db)


def test_h4_two_concurrent_heals(tmp_path):
    """Two processes discovering the same corruption; exactly one may replace.

    Corruption is induced by zeroing the header magic in place — not by
    truncating or deleting, because the auto-heal is classifier-gated and must
    see a genuine corruption rather than a missing file.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 6)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    _drain_wal(db)
    _corrupt_header(db)
    assert _quarantine_incidents(app) == []

    procs = [_spawn_writer(env, "report") for _ in range(2)]
    for p in procs:
        p.wait(timeout=180)

    # #496 S3: both detections DEFER, and admission admits exactly one of them.
    # The replacement itself happens in the detached worker that admission
    # spawned, so wait for it to converge before judging how many replacements
    # occurred. A poll rather than a fixed sleep: the property is "exactly one",
    # and reading it before the worker finishes would assert nothing.
    db = _resolved_app_dir(env) / "stats.db"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            ok, _text = _integrity_ok(db)
        except sqlite3.DatabaseError:
            ok = False  # still the corrupt original
        if ok and _quarantine_incidents(app):
            break
        time.sleep(0.5)

    incidents = _quarantine_incidents(app)
    assert len(incidents) == 1, (
        f"two heals both replaced the family: {incidents}"
    )
    ok, text = _integrity_ok(db)
    assert ok, text
    assert _no_orphan_sidecars(db)

    # Non-vacuity: the rebuilt index must actually carry the journal's rows, not
    # be an empty file that trivially passes integrity_check.
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert rows >= 5, f"heal rebuilt an empty index: {rows} snapshots"


@pytest.mark.skip(reason="#538 retired stats WAL checkpoint operation")
def test_h4_reader_pinned_wal(tmp_path):
    """A long-lived reader pinning the WAL across repeated checkpoint attempts.

    CHARACTERISATION, not a RED candidate. Stage 1 measured that a pinned reader
    defeats every checkpoint mode (PASSIVE returns in ~2 ms having changed
    nothing; TRUNCATE costs 148 ms at a 100 ms busy_timeout or 16.07 s at
    production's 15 s, and changes nothing either way) and that NEITHER raises —
    they return a busy row. What this asserts is that repeatedly failing to
    drain a pinned WAL is SAFE: no corruption, no orphan sidecar.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    grown = _grow_wal(db, 900)
    assert grown > 1024 * 1024, grown

    holder = _hold_open_handle(db)
    outcomes = []
    try:
        wal_before = _wal_path(db).stat().st_size
        for _ in range(4):
            res = _cctally(env, "db", "checkpoint", "--db", "stats",
                           "--busy-timeout-ms", "200")
            outcomes.append(res.returncode)
            ok, text = _integrity_ok(db)
            assert ok, text
        wal_after = _wal_path(db).stat().st_size
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    print(f"[H4-pinned] wal_before={wal_before} wal_after={wal_after} "
          f"checkpoint_rcs={outcomes}")
    # The pin is real: a pinned WAL does not shrink. If it did, this test is
    # characterising something other than a pinned reader.
    assert wal_after >= wal_before, (
        f"the WAL drained under a held read transaction ({wal_before} -> "
        f"{wal_after}); the reader was not actually pinning it"
    )
    assert _no_orphan_sidecars(db)

    # And once the reader is gone the same command drains it, so the failure
    # above is attributable to the pin and not to a broken checkpoint.
    res = _cctally(env, "db", "checkpoint", "--db", "stats")
    assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# Issue #393/#538 — dashboard reader releases rollback snapshots
# ---------------------------------------------------------------------------


_ROLLBACK_WRITER = """
import pathlib
import sys
import time

sys.path.insert(0, sys.argv[1])
import _cctally_store as store

db = pathlib.Path(sys.argv[2])
commits = int(sys.argv[3])
pad = "x" * 400
conn = store.stats_open_guarded(db)
try:
    started = time.monotonic()
    with store.stats_write_scope("issue-393-acceptance"):
        for i in range(commits):
            conn.execute(
                "INSERT INTO wal_probe(payload) VALUES (?)",
                (f"{i}:{pad}",),
            )
            conn.commit()
    print(
        f"commits={commits} elapsed={time.monotonic() - started:.6f} "
        f"mode={conn.execute('PRAGMA journal_mode').fetchone()[0]} "
        f"wal={int(pathlib.Path(str(db) + '-wal').exists())} "
        f"shm={int(pathlib.Path(str(db) + '-shm').exists())}",
        flush=True,
    )
finally:
    conn.close()
"""


def test_dashboard_source_reader_releases_rollback_snapshot(
    tmp_path, monkeypatch,
):
    """Sustained writes complete during a live dashboard source build.

    This is the direct #393 product gate, not a generic SQLite simulation:
    ``_tui_build_source_bundle`` is the dashboard sync thread's production
    read path, the stats handle comes from ``stats_open_guarded``, and the
    concurrent child writes through the same guarded opener plus an explicit
    sanctioned scope.  The provider builder is reduced to a slow operation
    only so the writer can finish while the dashboard-shaped reader is live.

    With the pre-#393 explicit ``BEGIN``, the first signature read held one
    snapshot for the entire build. In #538 rollback mode, autocommit statement
    snapshots must release after each SELECT so the writer can finish without
    a live WAL/SHM family.
    """
    data_dir = tmp_path / "data"
    env = _storm_env(data_dir)
    for key in (
        "CCTALLY_DATA_DIR",
        "CCTALLY_DISABLE_DEV_AUTODETECT",
        "CCTALLY_DISABLE_TELEMETRY",
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "TZ",
    ):
        monkeypatch.setenv(key, env[key])

    ns = load_script()
    tui = ns["_cctally_tui"]
    db = data_dir / "stats.db"
    cache = data_dir / "cache.db"
    data_dir.mkdir(parents=True, exist_ok=True)

    seed = sqlite3.connect(db)
    try:
        assert seed.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        seed.execute("PRAGMA synchronous=FULL")
        seed.execute(
            "CREATE TABLE wal_probe ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        seed.commit()
    finally:
        seed.close()
    sqlite3.connect(cache).close()

    signature = types.SimpleNamespace(
        max_entry_id=0,
        max_wus_id=0,
        max_wcs_id=0,
        reset_sig=(0, 0),
        max_codex_id=0,
        generation=0,
        entry_mutation_seq=0,
        codex_physical_mutation_seq=0,
    )
    monkeypatch.setitem(ns, "current_generation", lambda: 0)
    monkeypatch.setitem(ns, "compute_signature", lambda *a, **kw: signature)
    monkeypatch.setitem(ns, "open_cache_db", lambda: sqlite3.connect(cache))
    monkeypatch.setattr(
        tui,
        "resolve_dashboard_source_semantics",
        lambda *a, **kw: types.SimpleNamespace(
            display_tz_name="UTC",
            week_start_idx=0,
            week_start_name="monday",
            speed="auto",
            codex_budget=None,
            codex_quota_actual_thresholds=(),
            codex_quota_projected_thresholds=(),
            cache_report_anomaly_threshold_pp=15,
            codex_identity="test",
            claude_identity="test",
        ),
    )

    # The actual SELECT is load-bearing: under the pre-fix BEGIN it establishes
    # the snapshot whose SHARED lock blocks a rollback writer. Returning a
    # constant digest keeps the test focused on transaction lifetime.
    def stats_digest(conn):
        conn.execute("SELECT count(*) FROM wal_probe").fetchone()
        return "fixed"

    monkeypatch.setattr(tui, "codex_stats_digest", stats_digest)
    monkeypatch.setattr(tui, "accounts_identity_digest", lambda conn: "")
    monkeypatch.setattr(
        tui, "refresh_codex_source_clock", lambda state, *, now_utc: state
    )

    def compose_all_state(claude, codex):
        return tui.SourceDashboardState(
            source="all",
            availability="empty",
            freshness="fresh",
            warnings=(),
            data_version="all:test",
            last_success_at=codex.last_success_at,
            capabilities={},
            data={},
            domain_freshness={
                "hero": "fresh",
                "quota": "fresh",
                "sessions": "fresh",
            },
        )

    monkeypatch.setattr(tui, "compose_all_state", compose_all_state)

    observed: dict[str, int] = {}

    def build_codex_state(context, *, data_version):
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _ROLLBACK_WRITER,
                str(ROOT / "bin"),
                str(db),
                "2600",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert child.returncode == 0, child.stderr
        fields = dict(
            field.split("=", 1) for field in child.stdout.strip().split()
        )
        observed["commits"] = int(fields["commits"])
        observed["elapsed"] = float(fields["elapsed"])
        observed["mode"] = fields["mode"]
        observed["wal"] = int(fields["wal"])
        observed["shm"] = int(fields["shm"])
        return tui.SourceDashboardState(
            source="codex",
            availability="empty",
            freshness="fresh",
            warnings=(),
            data_version=data_version,
            last_success_at=context.now_utc,
            capabilities={},
            data={},
            domain_freshness={
                "hero": "fresh",
                "quota": "fresh",
                "sessions": "fresh",
            },
        )

    monkeypatch.setattr(tui, "build_codex_source_state", build_codex_state)

    import _cctally_store

    stats_conn = _cctally_store.stats_open_guarded(db)
    try:
        storm_started = time.monotonic()
        bundle = tui._tui_build_source_bundle(
            stats_conn=stats_conn,
            now_utc=ns["dt"].datetime.now(ns["dt"].timezone.utc),
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            raw_config={},
        )
        storm_elapsed = time.monotonic() - storm_started
        assert bundle.sources["codex"].source == "codex"
        assert observed["commits"] == 2600
        assert observed["mode"] == "delete"
        assert observed["wal"] == 0
        assert observed["shm"] == 0
        monkeypatch.setattr(
            tui,
            "build_codex_source_state",
            lambda *_args, **_kwargs: bundle.sources["codex"],
        )
        baseline_started = time.monotonic()
        baseline_bundle = tui._tui_build_source_bundle(
            stats_conn=stats_conn,
            now_utc=ns["dt"].datetime.now(ns["dt"].timezone.utc),
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
            raw_config={},
        )
        baseline_elapsed = time.monotonic() - baseline_started
        assert baseline_bundle.sources["codex"].source == "codex"
        wrapper_overhead = storm_elapsed - observed["elapsed"]
        print(
            "dashboard_rollback_acceptance: "
            f"commits={observed['commits']} writer={observed['elapsed']:.3f}s "
            f"storm={storm_elapsed:.3f}s overhead={wrapper_overhead:.3f}s "
            f"baseline={baseline_elapsed:.3f}s"
        )
        # DELETE/FULL commit speed depends heavily on the filesystem backing
        # the runner.  The regression signal is reader-added blocking, not the
        # child's own fsync time: a pinned snapshot makes the nested writer
        # fail its busy timeout, while a healthy reader adds only wrapper
        # overhead around the successful 2,600-commit child.
        assert wrapper_overhead >= 0.0
        assert wrapper_overhead <= max(2.0, baseline_elapsed * 4)
    finally:
        stats_conn.close()


def test_h4_open_time_cutover_races_storm(tmp_path):
    """A legacy-epoch stats.db cut over while storm writers run.

    Open-time cutover (spec 1.1 Gap C) exports every row to the journal, rewrites
    `journal_id` across the whole index and stamps `user_version`. Reachable from
    ANY `open_db()` caller, so under a hook storm several processes can enter it
    at once. Exactly one must do the work; the rest must wait for it.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 6)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    _drain_wal(db)

    conn = sqlite3.connect(str(db))
    try:
        epoch_before = conn.execute("PRAGMA user_version").fetchone()[0]
        before_rows = conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0]
        # Force the legacy branch of the epoch gate.
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()
    assert epoch_before != 13, "setup failed: DB was already at the legacy head"

    procs = [_spawn_writer(env, *_tick_args(1000 + i)) for i in range(6)]
    for p in procs:
        p.wait(timeout=180)

    db = _resolved_app_dir(env) / "stats.db"
    ok, text = _integrity_ok(db)
    assert ok, f"cutover under storm corrupted stats.db: {text}"
    assert _no_orphan_sidecars(db)

    conn = sqlite3.connect(str(db))
    try:
        epoch_after = conn.execute("PRAGMA user_version").fetchone()[0]
        after_rows = conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert epoch_after == epoch_before, (
        f"the storm left stats.db at epoch {epoch_after}, not {epoch_before}"
    )
    # Non-vacuity: the storm wrote through the cutover, it did not merely
    # bounce off it.
    assert after_rows >= before_rows, (after_rows, before_rows)


# ---------------------------------------------------------------------------
# Stage 2 review P1s (Task 12.5)
# ---------------------------------------------------------------------------


def test_stats_open_fails_fast_while_maintenance_is_held(tmp_path):
    """A long maintenance hold must not park every stats open forever (P1-1).

    ``stats_open_guarded`` sits on the path EVERY command takes — every
    ``statusline`` render, every detached ``hook-tick``. Stage 2 gave it an
    UNTIMED ``LOCK_SH``, and Stage 2 also made the exclusive holders long:
    ``db rebuild`` replays the whole journal, ``db vacuum`` rewrites the file,
    ``db rederive --yes`` runs a full scratch replay, ``db repair`` shells out to
    ``sqlite3 .recover``. During any of them every stats open blocked
    indefinitely — the #297-class stall spec section 5.2 ground 4 rejected the
    checkpoint policy over, reintroduced by the corruption fix itself.

    The wait is now bounded: on expiry the opener raises the ordinary
    "maintenance is in progress" error, which every caller already handles. That
    is strictly safe — the TOCTOU guarantee is "you cannot open WHILE exclusive
    is held", and failing after a timeout preserves it.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    lock = app / "stats.db.maintenance.lock"
    holder_code = (
        "import fcntl, sys, time; fh = open(sys.argv[1], 'a+'); "
        "fcntl.flock(fh, fcntl.LOCK_EX); print('held', flush=True); "
        "time.sleep(120)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        res = _cctally(env, "report", timeout=60)
        elapsed = time.monotonic() - started
        assert elapsed < 30, (
            f"stats open parked {elapsed:.1f}s behind a maintenance hold"
        )
        assert res.returncode != 0, (
            "the opener escaped into the live family while maintenance was "
            "held exclusive"
        )
        assert "maintenance" in (res.stderr + res.stdout).lower(), (
            res.stderr + res.stdout
        )
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()

    # Control: with the hold gone the very same command succeeds, so the
    # failure above is attributable to the lock and not to a broken `report`.
    res = _cctally(env, "report")
    assert res.returncode == 0, res.stderr


_NESTED_HEAL_CHILD = """
import importlib.machinery, importlib.util, sys
sys.path.insert(0, {bin!r})

# `bin/cctally` is EXTENSIONLESS, so a plain `import cctally` cannot find it —
# and `open_db` reaches the migration framework through the `_cctally()`
# accessor, which reads sys.modules['cctally']. Load it the way conftest does.
_loader = importlib.machinery.SourceFileLoader("cctally", {cli!r})
_spec = importlib.util.spec_from_loader("cctally", _loader)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cctally"] = _mod
_loader.exec_module(_mod)

import _cctally_core, _cctally_journal

fd = _cctally_journal._acquire_maintenance_shared("authoritative", 10.0)
assert fd is not None, "child could not take maintenance shared"
print("SHARED-HELD", flush=True)
try:
    assert _cctally_core.holds_stats_maintenance()
    try:
        conn = _cctally_core.open_db()
        conn.close()
        print("OPENED", flush=True)
    except BaseException as exc:
        # #496 S3: the heal DEFERS, and its signal derives from BaseException
        # so no broad reporting fallback can swallow it. Terminating with that
        # signal is the property under test; hanging is the failure.
        print("RAISED %s: %s" % (type(exc).__name__, exc), flush=True)
finally:
    _cctally_journal._release_maintenance_shared(fd)
print("DONE", flush=True)
"""


def test_nested_open_under_held_maintenance_does_not_self_deadlock(tmp_path):
    """A nested ``open_db()`` inside a maintenance-shared hold must complete (P1-2).

    ``_heal_flock_blocking`` took ``stats.db.maintenance.lock`` with a blocking
    ``LOCK_EX`` and never consulted ``holds_stats_maintenance()``. Both callers —
    the heal hook and the epoch resolver — are reachable from ``open_db``, and
    ``run_stats_ingest`` holds maintenance SHARED across its whole cycle. ``flock``
    conflicts are per open-file-DESCRIPTION and apply WITHIN a process, so SH on
    one fd plus EX on a second fd of the same file blocks the process against
    itself, forever.

    The child here reproduces exactly that shape: hold maintenance shared, then
    drive a nested ``open_db()`` against a classified corruption so the heal hook
    fires. Pre-fix it hangs; the assertion is that it TERMINATES.
    """
    env = _storm_env(tmp_path / "data")
    _seed_journal(env, 4)
    app = _resolved_app_dir(env)
    db = app / "stats.db"
    _drain_wal(db)
    _corrupt_header(db)

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _NESTED_HEAL_CHILD.format(bin=str(ROOT / "bin"), cli=str(CCTALLY)),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = child.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
        raise AssertionError(
            "a nested open_db() under a held maintenance-shared never returned "
            "— _heal_flock_blocking self-deadlocked on the maintenance lock"
        )

    assert "SHARED-HELD" in out, (out, err)
    # Non-vacuity: the heal path was genuinely entered, not skipped. A run that
    # never reached the blocking acquire would terminate for the wrong reason.
    assert "[heal]" in err, (
        "the corruption never reached the heal hook, so this run could not "
        f"have exercised the deadlock: out={out!r} err={err!r}"
    )
    assert "DONE" in out, (out, err)
    assert child.returncode == 0, (child.returncode, out, err)
