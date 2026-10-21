"""O1 (#963 spec 9.1): bin/cctally-update-test removes its scratch through the
release kernel, keeps the status a signal gave it, and fails on a cleanup
failure."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "bin" / "cctally-update-test"


def _wait_for_scratch(tmpdir, proc, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        found = sorted(tmpdir.glob("cctally-update-test.*"))
        if found and any(found[0].iterdir()):
            return found[0]
        time.sleep(0.05)
    raise AssertionError("the harness never populated its scratch (status %r)" % proc.poll())


def test_a_terminated_update_test_keeps_its_signal_status_and_removes_its_scratch(tmp_path):
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = dict(os.environ, TMPDIR=str(tmpdir), CCTALLY_TEST_JOBS="2")
    proc = subprocess.Popen([str(HARNESS)], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        scratch = _wait_for_scratch(tmpdir, proc)
        os.killpg(proc.pid, signal.SIGTERM)
        _out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert proc.returncode == 143, (
        "the harness did not keep its SIGTERM status (got %r)\n%s" % (proc.returncode, err))
    assert not scratch.exists(), "the harness left its scratch behind"
