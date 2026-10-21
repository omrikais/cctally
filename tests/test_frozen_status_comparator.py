"""The native preflight ignores only two valid status observation clocks."""
from contextlib import closing
import importlib.util
import json
import pathlib
import sqlite3
import subprocess

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "bench" / "write-attribution"


def _selftest():
    spec = importlib.util.spec_from_file_location("status_selftest", TOOLS / "frozen_selftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status():
    return {"version": 1, "observedAt": "2026-10-16T10:56:02Z",
            "decoderObservedAt": "2026-10-16T10:56:02Z", "decoder": "stdlib",
            "census": "complete", "readsPending": 0, "readsPendingSince": None,
            "verificationDebt": 0, "verificationPendingSince": None,
            "skipped": {"decode-error": 0, "decoder-unavailable": 0,
                        "verification-pending": 0, "verification-unverified": 0},
            "rebuildRefused": None, "barrier": {}}


def _rows(tmp_path, name, status, store="cache.db", *, raw=False, key="codex_container_status"):
    base = tmp_path / name
    base.mkdir()
    with closing(sqlite3.connect(base / store)) as conn:
        conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
        if status is not ...:
            conn.execute("INSERT INTO cache_meta VALUES (?, ?)",
                         (key, status if raw else json.dumps(status)))
        conn.commit()
    return _selftest().table_rows(base)


@pytest.mark.parametrize("store", ["cache.db", "conversations.db"])
@pytest.mark.parametrize("field", ["observedAt", "decoderObservedAt"])
def test_status_observation_clock_only_is_equal(tmp_path, store, field):
    left, right = _status(), _status()
    right[field] = "2026-10-16T10:55:59Z"
    assert _rows(tmp_path, "left", left, store) == _rows(tmp_path, "right", right, store)


@pytest.mark.parametrize("field,value", [
    ("readsPending", 1), ("verificationDebt", 1), ("decoder", "unavailable"),
    ("skipped", {"decode-error": 1}), ("barrier", {"pending": 1}),
    ("rebuildRefused", "decode-error"), ("census", "partial"),
    ("readsPendingSince", "2026-10-16T10:55:59Z"),
    ("verificationPendingSince", "2026-10-16T10:55:59Z"),
    ("observedAt", None), ("decoderObservedAt", None),
    ("otherObservedAt", "2026-10-16T10:55:59Z"),
])
def test_status_semantics_and_nullness_remain_unequal(tmp_path, field, value):
    left, right = _status(), _status()
    right[field] = value
    assert _rows(tmp_path, "left", left) != _rows(tmp_path, "right", right)


@pytest.mark.parametrize("field", ["observedAt", "decoderObservedAt", "decoder", "barrier"])
def test_missing_status_field_remains_unequal(tmp_path, field):
    left, right = _status(), _status()
    del right[field]
    assert _rows(tmp_path, "left", left) != _rows(tmp_path, "right", right)


def test_missing_status_row_remains_unequal(tmp_path):
    assert _rows(tmp_path, "left", _status()) != _rows(tmp_path, "right", ...)


@pytest.mark.parametrize("raw", ['{broken', '[]', 'null', '"text"'])
def test_malformed_status_fails_closed_even_on_identical_input(tmp_path, raw):
    with pytest.raises(ValueError, match="codex_container_status"):
        _rows(tmp_path, "bad", raw, raw=True)


@pytest.mark.parametrize("field", ["observedAt", "decoderObservedAt"])
@pytest.mark.parametrize("value", ["not-a-time", "", 123, {}, []])
def test_malformed_observation_clock_fails_closed(tmp_path, field, value):
    status = _status()
    status[field] = value
    with pytest.raises(ValueError, match="codex_container_status"):
        _rows(tmp_path, "bad", status)


def test_null_observation_clocks_preserve_equality(tmp_path):
    status = _status()
    status.update(observedAt=None, decoderObservedAt=None)
    assert _rows(tmp_path, "left", status) == _rows(tmp_path, "right", status)


def test_other_metadata_json_is_not_normalized(tmp_path):
    left, right = _status(), _status()
    right["observedAt"] = "2026-10-16T10:55:59Z"
    assert (_rows(tmp_path, "left", left, key="another_status")
            != _rows(tmp_path, "right", right, key="another_status"))


def test_prep_retains_synthetic_namespace_fixtures(tmp_path):
    # Stub the native command boundary: this exercises the sourced shell flow
    # without running macOS namespace tests on the remote test runner.
    root = tmp_path / "candidate"
    harness = root / "bench" / "write-attribution"
    harness.mkdir(parents=True)
    (root / "bin").mkdir()
    for name in ("_lib_write_budget.py", "_lib_reclaim_planner.py",
                 "_lib_conversation_retention.py"):
        (root / "bin" / name).touch()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "clang").write_text("#!/bin/sh\nexit 0\n")
    (shim / "clang").chmod(0o755)
    calls = tmp_path / "calls"
    python = shim / "python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    python.chmod(0o755)
    prep = (TOOLS / "_prep.sh").read_text().replace("/opt/homebrew/bin/python3", str(python))
    (harness / "_prep.sh").write_text(prep)
    out, scratch = tmp_path / "out", tmp_path / "scratch"
    out.mkdir()
    import os
    result = subprocess.run(["bash", "-c", '. "$P/_prep.sh"'],
                            env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
                                 "P": str(harness), "X": str(scratch), "OUT": str(out),
                                 "WA_MODE": "frozen", "WA_FREEZE": str(tmp_path / "freeze"),
                                 "CALLS": str(calls)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    frozen_call = next(line for line in calls.read_text().splitlines()
                       if "frozen_selftest.py" in line)
    assert "--keep" in frozen_call.split()
