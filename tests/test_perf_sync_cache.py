"""cache-sync stdout byte-identity + stderr phase-trace guard (issue #276, M2).

Proves the opt-in backend perf trace is invisible on stdout (goes to stderr
only) and non-vacuous when enabled (the trace really emits). Runs the real
``bin/cctally`` binary as a subprocess against an isolated, empty HOME so no
real Claude history is ingested and the run is fast + deterministic.
"""
import os
import pathlib
import subprocess
import sys

CCTALLY = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


def _run(env_extra, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.pop("CCTALLY_PERF_TRACE", None)          # neutralize an inherited flag
    env["HOME"] = str(home)
    env["CCTALLY_DATA_DIR"] = str(data)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CCTALLY), "cache-sync", "--source", "claude"],
        capture_output=True, text=True, env=env,
    )


def test_cache_sync_stdout_byte_identical_and_stderr_trace(tmp_path):
    off = _run({}, tmp_path)
    on = _run({"CCTALLY_PERF_TRACE": "1"}, tmp_path)
    assert on.stdout == off.stdout                 # stdout unchanged by the flag
    assert "backend-perf:" in on.stderr            # non-vacuous: trace really emits
    assert "backend-perf:" not in off.stderr
    assert "sync_cache" in on.stderr
