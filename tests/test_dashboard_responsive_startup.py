"""A1: cheap-panels first-paint seed + bind-before-build (issue #278 §1.1-§1.5).

On a normal launch ``_dashboard_initial_snapshot`` builds a CHEAP partial
snapshot — only the two sub-ms headline panels (current_week + forecast) plus
the real doctor + envelope-config precompute, ``hydrating=True`` — so the HTTP
socket binds in ~110ms instead of waiting on the ~2.2s full aggregation. The
background ``_DashboardSyncThread`` owns the first full cold build + SSE-publish.
Under ``--no-sync`` (no background thread to fill the partial) it keeps the full
pre-bind build (``hydrating=False``).

The subprocess bind-timing check is gated on the ``large`` bench fixture; it
skips where that fixture is absent (fresh CI), and is proven non-vacuous
manually by stashing the cheap-seed impl → RED against the pre-change full-seed.
"""
import datetime as dt
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import types

from conftest import load_script, redirect_paths  # type: ignore

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"
OK_AS_OF = dt.datetime(2026, 4, 16, 14, 0, tzinfo=dt.timezone.utc)
LARGE_FIXTURE = (
    pathlib.Path(__import__("tempfile").gettempdir())
    / "cctally-bench" / "large-seed42"
)


def _dash_mod():
    import _cctally_dashboard  # re-imported against fresh cctally by load_script
    return _cctally_dashboard


def _seed_data_dir_from_fixture(tmp_path, scenario):
    """Copy a dashboard fixture's SQLite tree into a fresh tmp data dir and
    return ``(data_dir, claude_dir)`` for CCTALLY_DATA_DIR / CLAUDE_CONFIG_DIR
    (both pinned — see gotcha_isolate_from_real_claude_data_needs_both_env_vars)."""
    src = (REPO / "tests" / "fixtures" / "dashboard" / scenario
           / ".local" / "share" / "cctally")
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, data / f.name)
    claude = tmp_path / "claude"
    (claude / "projects").mkdir(parents=True, exist_ok=True)
    return data, claude


def _load_with_fixture(monkeypatch, tmp_path, scenario):
    data, claude = _seed_data_dir_from_fixture(tmp_path, scenario)
    # Pin BOTH env vars BEFORE load_script so _init_paths_from_env re-points
    # cache.db/stats.db at the copy and sync never touches the real data dir.
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    ns = load_script()
    return ns


def test_cheap_seed_normal_launch_shape(monkeypatch, tmp_path):
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    seed = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )
    # Hydrating partial: the two headline panels are filled, heavy panels empty.
    assert seed.hydrating is True
    assert seed.current_week is not None
    assert seed.forecast is not None
    assert seed.sessions == []
    assert seed.weekly_periods == []
    assert seed.monthly_periods == []
    assert seed.daily_panel == []
    assert seed.blocks_panel == []
    # Real doctor + well-formed envelope precompute so snapshot_to_envelope does
    # NOT hit the per-connection inline-doctor branch and does NOT KeyError.
    assert seed.doctor_payload is not None
    assert seed.envelope_precompute is not None
    assert {"config", "update_state", "update_suppress"} <= set(
        seed.envelope_precompute.keys()
    )
    env = ns["snapshot_to_envelope"](seed, now_utc=OK_AS_OF)
    assert env["hydrating"] is True
    assert env["current_week"] is not None


def test_cheap_seed_empty_data(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    seed = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=None, display_tz_pref_override=None,
    )
    assert seed.hydrating is True
    assert seed.sessions == []
    assert seed.weekly_periods == []
    # Even with no data, the real doctor + envelope precompute run so the
    # envelope serializes without the inline-doctor fork / KeyError.
    assert seed.doctor_payload is not None
    assert seed.envelope_precompute is not None
    assert {"config", "update_state", "update_suppress"} <= set(
        seed.envelope_precompute.keys()
    )
    env = ns["snapshot_to_envelope"](
        seed, now_utc=dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)
    )
    assert env["hydrating"] is True


def test_no_sync_keeps_full_build(monkeypatch, tmp_path):
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    args = types.SimpleNamespace(no_sync=True, host="127.0.0.1")
    full = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )
    # Frozen-data mode: the full pre-bind build, heavy panels populated,
    # hydrating cleared.
    assert full.hydrating is False
    assert full.current_week is not None
    assert len(full.sessions) > 0
    env = ns["snapshot_to_envelope"](full, now_utc=OK_AS_OF)
    assert env["hydrating"] is False


def _read_url_port(proc, deadline_s):
    """Block on the subprocess stdout until the 'serving …:PORT' line (printed
    right after the socket bind), returning (port, elapsed_since_start)."""
    import re
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("dashboard exited before binding")
            continue
        m = re.search(r"://[^ ]*?:(\d+)/", line)
        if m:
            return int(m.group(1)), time.monotonic() - t0
    raise RuntimeError("timed out waiting for the serving line")


def test_bind_before_build_timing(tmp_path):
    """Bind-before-build on the heavy fixture: the socket accepts a TCP
    connection WELL BEFORE the full-data SSE frame (hydrating=false, non-empty
    sessions) arrives — proving the bind no longer waits on the ~2.2s
    aggregation. Skips where the large bench fixture is absent.

    Non-vacuous by construction: under the pre-change full-seed the first SSE
    frame IS the full snapshot published at bind, so time-to-accept ≈
    time-to-full-data and the ``bind precedes full data by ≥1s`` assertion
    fails RED (measured: pre-change time-to-accept ~5.0s == time-to-full-data;
    post-change ~2.0s vs ~5.0s). The absolute bound is a loose secondary guard
    (fixed process overhead — module import + self-heal + the 720MB cache
    migration-open — dominates the residual, NOT aggregation)."""
    if not (LARGE_FIXTURE / "data" / "cache.db").exists():
        import pytest
        pytest.skip("large bench fixture absent (build via build-bench-fixtures.py)")
    # Fresh copy so the launch's background sync never dirties the shared
    # fixture. APFS clone (cp -c) is instant + space-free on darwin; fall back
    # to a byte copy elsewhere.
    data = tmp_path / "data"
    src = LARGE_FIXTURE / "data"
    cloned = False
    if sys.platform == "darwin":
        rc = subprocess.run(["cp", "-c", "-R", str(src), str(data)]).returncode
        cloned = rc == 0
    if not cloned:
        shutil.copytree(src, data)
    # Drop any stale lock so the fresh process can take the flock.
    for lk in data.glob("*.lock"):
        try:
            lk.unlink()
        except OSError:
            pass
    claude = tmp_path / "claude" / "projects"
    claude.mkdir(parents=True, exist_ok=True)
    env = dict(__import__("os").environ)
    env["CCTALLY_DATA_DIR"] = str(data)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(BIN), "dashboard", "--port", "0",
         "--no-browser", "--host", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        port, time_to_accept = _read_url_port(proc, deadline_s=30.0)
        # time-to-URL ≈ time-to-bind ≈ time-to-accept (the serving line prints
        # right after TCPServer.__init__ bound + listened).
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            pass
        # Full data arrives later over SSE: read /api/events until a
        # hydrating=false frame with non-empty sessions lands.
        import json as _json
        import urllib.request
        time_to_full = None
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events", timeout=20
        )
        end = time.monotonic() + 20.0
        data_lines: list[str] = []
        while time.monotonic() < end and time_to_full is None:
            raw = req.readline()
            if not raw:
                break
            s = raw.decode("utf-8", "replace").rstrip("\n")
            if s.startswith("data:"):
                data_lines.append(s[5:].strip())
            elif s == "" and data_lines:
                payload = "".join(data_lines)
                data_lines = []
                try:
                    frame = _json.loads(payload)
                except ValueError:
                    continue
                if frame.get("hydrating") is False and frame.get("sessions", {}).get("rows"):
                    time_to_full = time.monotonic() - t0
        req.close()
        assert time_to_full is not None, (
            "no full-data (hydrating=false, non-empty sessions) SSE frame"
        )
        # Core A1 property (robust to machine speed): the socket binds well
        # before the heavy build finishes.
        assert time_to_accept < time_to_full - 1.0, (
            f"bind did not precede full data by >=1s: "
            f"time_to_accept={time_to_accept:.3f}s time_to_full={time_to_full:.3f}s"
        )
        # Loose absolute guard — clearly below the pre-change ~5s full-pre-bind
        # time-to-accept while tolerating fixed process overhead.
        assert time_to_accept < 3.5, (
            f"time-to-accept {time_to_accept:.3f}s not under 3.5s"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
