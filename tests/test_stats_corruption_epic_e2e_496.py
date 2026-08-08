"""#496 — the whole epic exercised end to end on one tree (spec §10.2).

Three observations cannot be made in-process, which is why this is one
subprocess-backed pytest rather than a unit test:

1. the real statusline CLI and the real dashboard must not stall while a
   corrupt stats index is being healed;
2. flock admission is exactly-once ACROSS processes, including the re-entrant
   case where the detecting process already holds `stats.db.maintenance.lock`
   shared;
3. the published index must validate on bytes that are not reachable through an
   inherited handle.

**Anti-flake rules (§10.2, binding).** Readiness and release use explicit pipes,
files or the existing deterministic pause barriers — never a fixed `sleep`. The
dashboard's port is confirmed accepting connections before any request is
issued. Final validation runs in a fresh process. Every wait is bounded and
fails with the state it observed rather than timing out silently.

The retention assertion is discriminating by construction: the fixture plants an
**old classified control** beside an **unclassified sentinel** and injects a
policy that selects the control, then asserts the control is gone and the
sentinel is not. And doctor must fail for the REPEATED-SHAPE reason
specifically, not because three detections tripped the independent ring
threshold.

**Retention is exercised through BOTH of its routes**, on a control of its own
each, because they are different code paths and only one of them runs
unattended: `cctally db prune --yes` is the operator-invoked one, and the
detached `_artifact-retention` worker reached through the real admission
predicate is the one that runs against the maintainer's only forensic copies
with nobody watching. The unattended leg removes `artifact-retention.last-sweep`
first — the seed ticks already stamped the daily limit, and without that removal
no worker is ever spawned and the leg asserts over an artifact nothing looked
at.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
CCTALLY = BIN / "cctally"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from test_stats_writer_storm_386 import (  # noqa: E402
    _drain_wal,
    _resolved_app_dir,
    _seed_journal,
    _storm_env,
    _tick_args,
)


def _clobber_table_root_page(db: pathlib.Path, table: str) -> int:
    """Invalidate one named table's root page, leaving the schema readable.

    **This is the corruption the scenario uses, and the choice is load-bearing.**
    The obvious alternative — destroying the file's header magic — makes the raw
    damage scan raise `file does not carry the SQLite header magic`, so the
    incident records `shapeToken: "none"`, and `_NON_SHAPE_TOKEN` excludes that
    from the recurrence rule. The same header corruption twice therefore
    produces two incidents and NO repeated shape, and doctor stays at `warn`.
    Spec §10.2's step 3 requires the repeated-shape FAIL, so the corruption has
    to be one the scanner can characterize. Clobbering a table root page is also
    the production shape: the retained corpus implicates
    `quota_projection_state` and its automatic index almost every time.
    """
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        root = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = ?", (table,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    raw = int.from_bytes(db.read_bytes()[16:18], "big")
    page_size = 65536 if raw == 1 else raw
    with db.open("r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b"\x00" * page_size)
    return root

# §10.3: this runs in the ORDINARY suite — no environment flag, and no marker
# either, because `pytest.ini` registers none. #496 F19's benchmark sat behind
# `CCTALLY_RUN_BENCHMARK`, never ran, and "passed at 71 s for the wrong reason".

#: Every bounded wait in this file. Generous enough for a loaded CI runner,
#: small enough that a genuine hang fails the suite rather than stalling it.
_WAIT_S = 120.0
_CLI_BUDGET_S = 30.0

#: The flat `config.json` key `read_retention_policy` reads. Hardcoded rather
#: than imported: a fixture that took the constant from the module under test
#: would keep agreeing with it after a rename that broke every real install.
RETENTION_CONFIG_KEY = "storage.artifact_retention"

_CONTROL = "stats.db-20200101T000000Z"
_SENTINEL = "stats.db-20200102T000000Z"
#: A second classified control, planted only for the unattended leg, so the
#: operator-invoked `db prune --yes` leg keeps its own target and neither leg
#: can be credited with the other's deletion.
_WORKER_CONTROL = "stats.db-20200103T000000Z"


# --------------------------------------------------------------------------
# Bounded waits that report what they observed
# --------------------------------------------------------------------------


def _await(predicate, *, what, budget=_WAIT_S, describe=lambda: ""):
    """Poll `predicate` until true, then return. Never a fixed sleep.

    The poll interval is short and the failure carries the observed state,
    because a bare `TimeoutError` from a barrier tells the next reader nothing
    about which half of the handshake did not happen.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(
        f"timed out after {budget:g}s waiting for {what}; observed: "
        f"{describe()}"
    )


def _paused_pid(marker: pathlib.Path):
    """The pid a process wrote before SIGSTOPping itself at a pause barrier."""
    try:
        text = marker.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _process_stopped(pid: int) -> bool:
    out = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True, text=True,
    )
    return out.stdout.strip().startswith("T")


def _process_gone(pid: int) -> bool:
    return subprocess.run(
        ["ps", "-o", "pid=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip() == ""


# --------------------------------------------------------------------------
# The fixture
# --------------------------------------------------------------------------


def _cli(env, *args, timeout=_WAIT_S, stdin_text=""):
    return subprocess.run(
        [str(CCTALLY), *args], env=env, capture_output=True, text=True,
        timeout=timeout, input=stdin_text,
    )


#: The Claude Code status-line payload. `cctally statusline` reads it from
#: stdin, so an empty stdin is a usage error rather than the degrade path this
#: phase is about.
_STATUSLINE_STDIN = json.dumps({
    "session_id": "e2e-496-s6",
    "model": {"id": "claude-opus-4-1", "display_name": "Opus"},
    "workspace": {"current_dir": "/tmp"},
})


def _plant_incident(app: pathlib.Path, name: str, *, classified: bool):
    """One quarantine incident, valid on disk, classified or not.

    A classified incident carries a `schemaVersion: 2` manifest with a truthy
    `trigger`, which §3.3 accepts as `exact` without any correlation. An
    unclassified one carries a v1 manifest and nothing correlates to it, so the
    protection gate keeps it whatever the bounds say — which is the whole point
    of the sentinel.
    """
    incident = app / "quarantine" / name
    incident.mkdir(parents=True, exist_ok=True)
    (incident / "stats.db").write_bytes(b"preserved stats family bytes" * 64)
    manifest = {
        "schemaVersion": 2 if classified else 1,
        "quarantinedAtUtc": "2020-01-01T00:00:00Z",
        "originalPath": str(app / "stats.db"),
        "movedFiles": ["stats.db"],
        "complete": True,
        "cutoverProtocol": "preserve-then-atomic-replace-v1",
    }
    if classified:
        manifest["trigger"] = "corruption-heal"
        manifest["triggerError"] = "database disk image is malformed"
    (incident / "manifest.json").write_text(json.dumps(manifest))
    old = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    for path in (incident / "stats.db", incident / "manifest.json", incident):
        os.utime(path, (old, old))
    return incident


def _inject_policy(app: pathlib.Path):
    """A policy that selects the control and cannot reach the sentinel.

    Written into `config.json` directly rather than through `config set`,
    because the command opens the stats index and this runs against a
    deliberately damaged one.
    """
    config_path = app / "config.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        config = {}
    # A FLAT key. `read_retention_policy` does `loaded.get(RETENTION_CONFIG_KEY)`
    # on the top-level object, so a nested `{"storage": {"artifact_retention":
    # ...}}` is silently ignored and the DEFAULT policy is used — which this
    # fixture would then be testing instead of the injected one.
    config[RETENTION_CONFIG_KEY] = {
        "max_age_days": 1,
        "max_count_per_family": 1,
        "max_total_mib": None,
        "min_free_mib": 0,
        "max_shape_examples": 8,
    }
    config_path.write_text(json.dumps(config))


@pytest.fixture
def e2e(tmp_path):
    """An isolated data directory carrying the whole epic's inputs."""
    env = _storm_env(tmp_path / "data")
    # The sweep suppressor `bin/_lib-harness-env.sh` exports is inherited from
    # the harness environment. THIS test drives the worker deliberately, so it
    # is dropped here and the worker is invoked explicitly instead.
    env.pop("CCTALLY_DISABLE_RETENTION_SWEEP", None)
    _seed_journal(env, 3)
    app = _resolved_app_dir(env)
    _drain_wal(app / "stats.db")

    control = _plant_incident(app, _CONTROL, classified=True)
    sentinel = _plant_incident(app, _SENTINEL, classified=False)
    _inject_policy(app)
    return env, app, control, sentinel


# --------------------------------------------------------------------------
# The child that holds maintenance shared and drives the re-entrant open
# --------------------------------------------------------------------------

_HOLDER = """
import importlib.machinery, importlib.util, pathlib, sys, time

sys.path.insert(0, {bin!r})
# `_spawn_detached` launches `[sys.executable, realpath(sys.argv[0]), command]`.
# Under `python -c` that is the literal "-c", so the detached heal worker would
# be spawned against a path that does not exist and the whole handoff would
# vanish silently. Pinning argv[0] to the real entry point is what makes this
# child's spawn the production one.
sys.argv = [{cli!r}, "e2e-maintenance-holder"]
_loader = importlib.machinery.SourceFileLoader("cctally", {cli!r})
_spec = importlib.util.spec_from_loader("cctally", _loader)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cctally"] = _mod
_loader.exec_module(_mod)

import sqlite3
import _cctally_core, _cctally_journal, _cctally_tui

release = pathlib.Path({release!r})
fd = _cctally_journal._acquire_maintenance_shared("authoritative", 30.0)
assert fd is not None, "child could not take maintenance shared"
print("SHARED-HELD", flush=True)
try:
    # The re-entrant path: this process already owns the maintenance lock, and
    # the heal hook must reuse that hold rather than deadlock against itself on
    # a second descriptor. A clobbered table root page opens and serves rows, so
    # it is the POST-QUERY probe that classifies it — which is the detection
    # path 26 of the maintainer's 87 quarantined indexes took.
    try:
        _cctally_tui._tui_heal_post_query_stats(
            sqlite3.DatabaseError("database disk image is malformed")
        )
        print("HEAL-RETURNED", flush=True)
    except BaseException as exc:
        print("RAISED %s" % type(exc).__name__, flush=True)
    # Keep the shared hold until the parent says otherwise, so the statusline
    # and the dashboard meet a maintenance owner they cannot displace.
    deadline = time.monotonic() + 180
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
finally:
    _cctally_journal._release_maintenance_shared(fd)
print("DONE", flush=True)
"""


#: A detection with NO maintenance hold: the recurrence in phase 3. The same
#: post-query probe classifies the same clobbered root page, so the second
#: incident carries the same damage shape as the first — which is what makes
#: doctor's repeated-shape escalation reachable at all.
_DETECTOR = """
import importlib.machinery, importlib.util, sqlite3, sys

sys.path.insert(0, {bin!r})
sys.argv = [{cli!r}, "e2e-detector"]
_loader = importlib.machinery.SourceFileLoader("cctally", {cli!r})
_spec = importlib.util.spec_from_loader("cctally", _loader)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cctally"] = _mod
_loader.exec_module(_mod)

import _cctally_tui

try:
    _cctally_tui._tui_heal_post_query_stats(
        sqlite3.DatabaseError("database disk image is malformed")
    )
    print("HEAL-RETURNED", flush=True)
except BaseException as exc:
    print("RAISED %s" % type(exc).__name__, flush=True)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_accepting(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.25)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _worker_lock_held(app: pathlib.Path) -> bool:
    """Whether a detached heal worker currently owns its admission flock.

    `cmd_stats_corruption_heal_internal` takes `LOCK_EX | LOCK_NB` on
    `stats-corruption-heal.worker.lock` and returns immediately when it cannot,
    so a held lock IS the "exactly one admitted worker" property, observed the
    same way a second worker would observe it.
    """
    import fcntl

    path = app / "stats-corruption-heal.worker.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _heal_ring(app: pathlib.Path):
    """The durable heal-event ring: one entry per ADMITTED detection."""
    try:
        payload = json.loads(
            (app / "logs" / "stats-heal-events.json").read_text()
        )
    except (OSError, ValueError):
        return []
    events = payload.get("events") if isinstance(payload, dict) else payload
    return events if isinstance(events, list) else []


def _heal_log(app: pathlib.Path) -> str:
    try:
        return (app / "logs" / "stats-corruption-heal.log").read_text()
    except OSError:
        return ""


def _admitted_worker_runs(app: pathlib.Path) -> int:
    """How many detached heal workers have recorded an outcome.

    One line per worker that took the non-blocking worker flock and reported,
    which is what "exactly one admitted worker" means across processes.
    """
    return sum(1 for line in _heal_log(app).splitlines() if "result=" in line)


def _incident_names(app: pathlib.Path):
    quarantine = app / "quarantine"
    if not quarantine.exists():
        return []
    return sorted(
        p.name for p in quarantine.iterdir()
        if p.is_dir() and p.name not in (_CONTROL, _SENTINEL, _WORKER_CONTROL)
    )


def _retention_log_lines(app: pathlib.Path):
    """Every outcome line the detached retention worker has recorded.

    One line per worker that took the non-blocking worker flock and reported,
    which is the only channel a background sweep has: its streams are
    `/dev/null`.
    """
    try:
        text = (app / "logs" / "artifact-retention.log").read_text()
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _worker_deleted_count(line: str):
    """The `deleted=N` field of one worker outcome line, or None."""
    for token in line.split():
        if token.startswith("deleted="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def test_the_epic_scenario_end_to_end(e2e, tmp_path):
    env, app, control, sentinel = e2e
    release_marker = tmp_path / "release-maintenance"
    heal_request = app / "stats-corruption-heal.pending"
    stats_db = app / "stats.db"

    _clobber_table_root_page(stats_db, "quota_projection_state")

    # ---- Phase 1: the re-entrant detection, with the worker held off.
    #
    # The child holds `stats.db.maintenance.lock` SHARED and then drives a
    # nested `open_db()`. The heal hook reuses that hold (ownership-first),
    # files the durable request and spawns the detached worker; the worker,
    # a fresh process holding nothing, cannot take maintenance EXCLUSIVE while
    # the child still owns it and steps aside as `maintenance-busy`, leaving
    # the request marker for a later retry. That is the pause barrier, and it
    # is the production mechanism rather than an injected one.
    holder_err = tmp_path / "holder.err"
    holder_err_handle = holder_err.open("wb")
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            _HOLDER.format(
                bin=str(BIN), cli=str(CCTALLY), release=str(release_marker),
            ),
        ],
        env=env, stdout=subprocess.PIPE, stderr=holder_err_handle, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "SHARED-HELD"
        _await(
            lambda: _worker_lock_held(app),
            what="the detached heal worker to claim its admission flock",
            describe=lambda: (
                f"ring={_heal_ring(app)} log={_heal_log(app)!r} "
                f"holder_exit={holder.poll()} "
                f"holder_stderr={holder_err.read_text()[-1500:]!r}"
            ),
        )
        # Exactly one admitted worker. A second spawn would fail the same
        # non-blocking flock and return without recording anything, and the
        # ring carries one entry per admitted detection.
        assert len(_heal_ring(app)) == 1, _heal_ring(app)
        assert _admitted_worker_runs(app) == 0, (
            "no worker has finished yet, so the phase-1 assertions below are "
            f"about a live handoff: {_heal_log(app)!r}"
        )
        # The retry marker is durable while the worker is still working.
        request = json.loads(heal_request.read_text())
        assert request["healId"]
        assert (app / "stats-corruption-heal.admission.lock").exists()

        # Both real surfaces must return promptly against the damaged index
        # while the holder still owns maintenance.
        started = time.monotonic()
        statusline = _cli(
            env, "statusline", timeout=_CLI_BUDGET_S,
            stdin_text=_STATUSLINE_STDIN,
        )
        statusline_elapsed = time.monotonic() - started
        assert statusline_elapsed < _CLI_BUDGET_S, statusline_elapsed
        assert statusline.returncode in (0, 3), (
            statusline.returncode, statusline.stderr
        )

        # The dashboard, same phase, on an ephemeral loopback port. It must
        # bind and answer while the admitted heal worker is still blocked on
        # maintenance and the holder still owns it. The port is confirmed to be
        # accepting connections before any request is issued.
        port = _free_port()
        dash_out = tmp_path / "dashboard.out"
        dash_handle = dash_out.open("wb")
        started = time.monotonic()
        dashboard = subprocess.Popen(
            [str(CCTALLY), "dashboard", "--host", "127.0.0.1",
             "--port", str(port), "--no-browser"],
            env=env, stdout=dash_handle, stderr=subprocess.STDOUT,
        )
        try:
            _await(
                lambda: _port_accepting(port),
                what=f"the dashboard to accept connections on :{port}",
                budget=_CLI_BUDGET_S,
                describe=lambda: (
                    f"exit={dashboard.poll()} "
                    f"output={dash_out.read_text()[-2000:]!r}"
                ),
            )
            assert time.monotonic() - started < _CLI_BUDGET_S
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/data", timeout=_CLI_BUDGET_S,
            ) as response:
                assert response.status == 200
                envelope = json.loads(response.read().decode("utf-8"))
            assert envelope.get("envelope_version"), sorted(envelope)
        finally:
            dashboard.terminate()
            dashboard.wait(timeout=30)
            dash_handle.close()

        # Neither surface admitted a second worker.
        assert len(_heal_ring(app)) == 1, _heal_ring(app)
        assert _worker_lock_held(app)
        assert heal_request.exists(), "the retry marker must still be durable"
    finally:
        release_marker.write_text("go\n")
        try:
            holder.wait(timeout=60)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()
        holder_err_handle.close()

    # ---- Phase 2: maintenance is free, so the already-admitted worker — which
    # was blocked on `stats.db.maintenance.lock` exclusive, not on an injected
    # barrier — takes it and finishes. Nothing is started here.
    _await(
        lambda: not heal_request.exists(),
        what="the completed worker to clear its own request marker",
        describe=lambda: _heal_log(app),
    )
    assert "result=success" in _heal_log(app), _heal_log(app)

    # Final validation in a FRESH process, on bytes no inherited handle reaches.
    report = _cli(env, "report", "--json")
    assert report.returncode == 0, report.stderr
    status = _cli(env, "db", "status", "--json")
    assert status.returncode == 0, status.stderr

    healed = _incident_names(app)
    assert len(healed) == 1, f"one admitted heal, one incident: {healed}"
    manifest = json.loads(
        (app / "quarantine" / healed[0] / "manifest.json").read_text()
    )
    assert manifest["schemaVersion"] == 2
    assert manifest["trigger"] == "corruption-heal"
    assert manifest["forensicsPath"], "the incident must name its evidence"
    assert pathlib.Path(manifest["forensicsPath"]).exists()

    # ---- Phase 3: the damage recurs, and doctor escalates for the SHAPE.
    _drain_wal(stats_db)
    _clobber_table_root_page(stats_db, "quota_projection_state")
    detector = subprocess.run(
        [sys.executable, "-c", _DETECTOR.format(bin=str(BIN), cli=str(CCTALLY))],
        env=env, capture_output=True, text=True, timeout=_WAIT_S,
    )
    assert detector.returncode == 0, (detector.stdout, detector.stderr)
    _await(
        lambda: len(_incident_names(app)) == 2,
        what="the second heal to leave a second incident",
        describe=lambda: str(_incident_names(app)) + _heal_log(app),
    )
    _await(
        lambda: not heal_request.exists(),
        what="the second heal to clear its request marker",
        describe=lambda: _heal_log(app),
    )

    doctor = _cli(env, "doctor", "--json")
    payload = json.loads(doctor.stdout)
    legs = {
        check["id"]: check
        for category in payload["categories"]
        for check in category["checks"]
    }
    heal = legs["journal.auto_heal"]
    manifests = {
        name: json.loads(
            (app / "quarantine" / name / "manifest.json").read_text()
        ).get("damage")
        for name in _incident_names(app)
    }
    assert heal["severity"] == "fail", (heal, manifests)
    # The REPEATED-SHAPE reason specifically. Asserting only on the severity
    # would pass when three concurrent detections tripped the independent ring
    # threshold, which is a different finding about a different corpus.
    assert heal["details"]["repeatedShapes"], heal
    assert "shape" in heal["summary"].lower(), heal

    # ---- Phase 4: retention deletes the control and keeps the sentinel.
    assert control.exists() and sentinel.exists(), (
        "the fixture's own artifacts vanished before retention ran"
    )
    # `cctally db prune --yes` rather than the detached worker entry point:
    # the worker is rate-limited to one pass a day and `_seed_journal`'s
    # `record-usage` ticks already stamped `artifact-retention.last-sweep`, so
    # invoking it here is a no-op that would make the assertions below vacuous.
    # `db prune --yes` is the supported one-command reclaim and carries no such
    # stamp.
    # Exit 3 is the SANCTIONED outcome here, not a failure (§6.2): the
    # unclassified sentinel is old, the protection gate is absolute, and the age
    # bound therefore cannot be satisfied by any deletion the policy is allowed
    # to make. That is acceptance criterion 1 — protected evidence blocking a
    # bound — observed on the real command rather than asserted in a unit test.
    preview = _cli(env, "db", "prune", "--json")
    assert preview.returncode in (0, 3), preview.stderr
    plan = json.loads(preview.stdout)
    # The injected policy really is the one in force. Without this a fixture
    # whose config key stopped being read would silently fall back to the
    # 30-day default, and the control would still be selected — for a reason
    # this scenario never chose.
    assert plan["policy"]["maxAgeDays"] == 1, plan["policy"]
    assert plan["policy"]["maxCountPerFamily"] == 1, plan["policy"]
    sweep = _cli(env, "db", "prune", "--yes")
    assert sweep.returncode in (0, 3), sweep.stderr
    assert "max_age_seconds" in sweep.stdout, sweep.stdout

    assert not control.exists(), (
        "retention deleted nothing, so this assertion would be vacuous. "
        f"quarantine now holds "
        f"{sorted(p.name for p in (app / 'quarantine').iterdir())}; "
        f"the planned reclaim was {plan}"
    )
    assert sentinel.exists(), (
        "the unclassified sentinel was deleted; the protection gate is "
        "absolute and evidence cctally could not classify is never swept"
    )

    # ---- Phase 5: the same reclamation through the UNATTENDED route.
    #
    # `db prune --yes` above is the operator-invoked path. The detached worker
    # is the one that runs on the maintainer's only forensic copies with nobody
    # watching, and until this leg existed the scenario exercised it nowhere.
    #
    # The daily rate limit is what made a naive worker leg vacuous:
    # `_seed_journal`'s `record-usage` ticks already admitted a sweep and
    # stamped `artifact-retention.last-sweep`, so a second mutating command
    # schedules nothing and every assertion below would pass over an artifact
    # no worker ever looked at. Removing the stamp — rather than substituting a
    # different command — is what puts the real admission predicate back in the
    # path under test.
    worker_control = _plant_incident(app, _WORKER_CONTROL, classified=True)
    stamp = app / "artifact-retention.last-sweep"
    before_lines = _retention_log_lines(app)
    if stamp.exists():
        stamp.unlink()
    assert not stamp.exists()

    tick = _cli(env, *_tick_args(9))
    assert tick.returncode == 0, tick.stderr

    # The admission genuinely happened. `reserve_artifact_retention` writes
    # this stamp only after it has filed a durable request, so its reappearance
    # separates "the worker admitted and ran" from "the predicate declined and
    # nothing was ever spawned" — the two outcomes a surviving artifact alone
    # cannot tell apart.
    _await(
        lambda: stamp.exists(),
        what="the mutating command to admit a sweep and re-stamp the daily limit",
        describe=lambda: f"log={_retention_log_lines(app)}",
    )
    new_lines = _await(
        lambda: _retention_log_lines(app)[len(before_lines):] or None,
        what="the detached retention worker to record an outcome",
        describe=lambda: f"log={_retention_log_lines(app)}",
    )
    assert not any("no-request" in line for line in new_lines), (
        f"the worker started but found no request to act on: {new_lines}"
    )
    deleted = [_worker_deleted_count(line) for line in new_lines]
    assert any(count for count in deleted if count), (
        f"the worker ran and reclaimed nothing, so this leg is vacuous: "
        f"{new_lines}"
    )
    _await(
        lambda: not worker_control.exists(),
        what="the detached worker to reclaim the second control",
        describe=lambda: (
            f"quarantine now holds "
            f"{sorted(p.name for p in (app / 'quarantine').iterdir())}; "
            f"log={_retention_log_lines(app)}"
        ),
    )
    assert sentinel.exists(), (
        "the unattended route deleted the unclassified sentinel; the "
        "protection gate binds the detached worker exactly as it binds "
        "`db prune --yes`"
    )
