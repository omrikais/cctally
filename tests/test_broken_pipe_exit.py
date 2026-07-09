"""#279 S1 F5 — a dead stdout pipe is not an error.

`cctally daily | head` closes the read end early; the generic
`except Exception -> Error: [Errno 32] Broken pipe; rc=1` turned that into a
spurious failure. The dispatcher now catches BrokenPipeError, silences the
interpreter's shutdown-flush noise, and returns 0 immediately — skipping the
post-command update hooks (stdout is dead, banners are pointless), mirroring
the KeyboardInterrupt immediate-return precedent.

Subprocess-level so it exercises the real binary end-to-end. Determinism: the
child's stdout is a pipe with NO reader (the parent closes both ends and
close_fds hides them from the child), so the child's first stdout write raises
EPIPE regardless of output size or timing — no flaky reader-vs-writer race.
"""
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_broken_pipe_pipeline_exits_zero_quietly(tmp_path):
    # Full isolation from real Claude data (both env vars): an empty HOME with
    # an empty ~/.claude/projects so `daily` finds no sessions and returns fast
    # (still writing at least a header/no-data line to stdout → triggers EPIPE).
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    data_dir = tmp_path / ".local" / "share" / "cctally"
    data_dir.mkdir(parents=True)
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(
        HOME=str(tmp_path),
        CCTALLY_DATA_DIR=str(data_dir),
        CCTALLY_DISABLE_DEV_AUTODETECT="1",
        CCTALLY_DISABLE_UPDATE_CHECK="1",
        TZ="Etc/UTC",
    )
    r, w = os.pipe()
    try:
        p = subprocess.Popen(
            [sys.executable, str(REPO / "bin" / "cctally"), "daily"],
            stdout=w, stderr=subprocess.PIPE, env=env, close_fds=True,
        )
    finally:
        os.close(w)   # parent's copy of the write end
    os.close(r)       # close the read end → the pipe now has zero readers
    _out, err = p.communicate(timeout=60)
    err_text = err.decode(errors="replace")
    assert p.returncode == 0, err_text
    assert "Errno 32" not in err_text, err_text
    assert "Broken pipe" not in err_text, err_text
    assert "Traceback" not in err_text, err_text
