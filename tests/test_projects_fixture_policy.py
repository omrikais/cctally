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


def _logical_snapshot(path: pathlib.Path) -> tuple:
    """Canonical schema + row content, independent of SQLite page layout."""
    with closing(sqlite3.connect(path)) as conn:
        schema = tuple(conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall())
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' ORDER BY name"
            )
        ]
        rows = []
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows.append((
                table,
                tuple(conn.execute(
                    f"SELECT * FROM {quoted} ORDER BY rowid"
                ).fetchall()),
            ))
    return schema, tuple(rows)


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


def test_projects_fixtures_rebuild_stably_without_planner_stats(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _build(first)
    _build(second)

    names = ("multi-week.db", "single-week.db", "edge-cases.db")
    first_hashes = {name: _sha256(first / name) for name in names}
    second_hashes = {name: _sha256(second / name) for name in names}
    assert first_hashes == second_hashes
    for name in names:
        assert _logical_snapshot(first / name) == _logical_snapshot(
            COMMITTED / name
        )

    with closing(sqlite3.connect(first / "multi-week.db")) as conn:
        stat_tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'sqlite_stat%' ORDER BY name"
        ).fetchall()
    assert stat_tables == []
