"""#418: deterministic projects fixtures carry an explicit planner-stat policy."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDER = ROOT / "bin" / "build-projects-fixtures.py"
COMMITTED = ROOT / "tests" / "fixtures" / "projects"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_projects_fixtures", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(out_dir: pathlib.Path) -> None:
    env = os.environ.copy()
    env["TZ"] = "Etc/UTC"
    subprocess.run(
        [sys.executable, str(BUILDER), "--out-dir", str(out_dir)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_projects_fixture_declares_planner_stats_policy():
    builder = _load_builder()
    assert builder.PLANNER_STATS_POLICY == "absent"


def test_projects_fixtures_rebuild_byte_exact_without_planner_stats(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _build(first)
    _build(second)

    names = ("multi-week.db", "single-week.db", "edge-cases.db")
    first_hashes = {name: _sha256(first / name) for name in names}
    second_hashes = {name: _sha256(second / name) for name in names}
    committed_hashes = {name: _sha256(COMMITTED / name) for name in names}
    assert first_hashes == second_hashes == committed_hashes

    with closing(sqlite3.connect(first / "multi-week.db")) as conn:
        stat_tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'sqlite_stat%' ORDER BY name"
        ).fetchall()
    assert stat_tables == []
