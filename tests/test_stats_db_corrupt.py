"""#279 S1 F4 / #314 — corruption yields staged guidance, not a traceback.

stats.db is the non-re-derivable DB (recorded usage history). Before this fix,
`open_db()` connected + ran PRAGMAs/DDL with no `sqlite3.DatabaseError`
handling, so a corrupt file surfaced as `Error: file is not a database` (rc 1)
or a raw traceback. Now an open-time probe raises a typed
`StatsDbCorruptError` → staged global exit 3 with a
one-line diagnosis + recovery guidance; command handlers that map DB errors to
other exit codes (record-credit → 3) re-raise so the exit-2 contract wins.
"""
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def _run(cmd_args, data_dir):
    env = dict(
        os.environ,
        CCTALLY_DATA_DIR=str(data_dir),
        CCTALLY_DISABLE_DEV_AUTODETECT="1",
        CCTALLY_DISABLE_UPDATE_CHECK="1",
        TZ="Etc/UTC",
    )
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "cctally"), *cmd_args],
        capture_output=True, text=True, env=env,
    )


def _corrupt_stats_db(data_dir: pathlib.Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stats.db").write_bytes(b"this is not a sqlite database at all\n" * 4)


def test_corrupt_stats_db_diagnosis_exit_2(tmp_path):
    _corrupt_stats_db(tmp_path)
    r = _run(["weekly"], tmp_path)
    assert r.returncode == 3, r.stderr
    assert "stats.db" in r.stderr and "corrupt" in r.stderr.lower(), r.stderr
    assert "cctally db repair --db stats --yes" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr


def test_corrupt_stats_db_record_credit_uses_staged_exit_3(tmp_path):
    _corrupt_stats_db(tmp_path)
    r = _run(["record-credit", "--to", "31", "--yes"], tmp_path)
    assert r.returncode == 3, r.stderr
    assert "Traceback" not in r.stderr, r.stderr
