"""V1 regression: migration 008 uses a CLOSED interval (``<=``) on
``range_end_iso`` so it matches the production writer
(``iter_entries`` in bin/_cctally_cache.py: lex ``timestamp_utc >= ?
AND timestamp_utc <= ?``).

Pre-fix the migration used a half-open ``<`` end, so an entry whose
``timestamp_utc`` exactly equalled the snapshot's ``range_end_iso`` was
silently excluded from the recompute, even though every subsequent
``sync-week`` write would include it via the writer's closed predicate.
The asymmetry shows up as an off-by-one-entry cost drift on weeks
whose boundary lands on a real entry's timestamp.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
Reconcile invariant: R-DEDUP2 in ``bin/cctally-reconcile-test``.
"""
from __future__ import annotations

import datetime as dt
import importlib.util as ilu
import pathlib
import sqlite3
import sys

import pytest

# Test-local import of ``parse_iso_datetime`` from bin/_cctally_core for
# building the production-canonical UTC ISO shape of fixture entries.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
from _cctally_core import parse_iso_datetime  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_db():
    """Load bin/_cctally_db.py via SourceFileLoader.

    Matches the pattern in test_migration_008_scope.py so the production
    handler is exercised verbatim.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _pin_resolver_to_fake_home(core, tmp_path, monkeypatch):
    """Redirect HOME so the env-aware resolver doesn't pick up real
    ``~/.claude/projects`` on the developer's machine."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    core._init_paths_from_env()


def _stage_stats(stats_path: pathlib.Path, range_end_iso: str) -> int:
    """Stage stats.db with one auto/no-project snapshot whose
    ``range_end_iso`` is the caller-supplied boundary."""
    stats = sqlite3.connect(stats_path)
    try:
        stats.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE weekly_cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                range_start_iso TEXT,
                range_end_iso TEXT,
                cost_usd REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'cctally-range-cost',
                mode TEXT NOT NULL DEFAULT 'auto',
                project TEXT
            );
            """
        )
        cur = stats.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "2026-05-22T00:00:00Z", "2026-05-15", "2026-05-22",
                "2026-05-15T00:00:00Z", range_end_iso,
                999.0, "auto", None,
            ),
        )
        snap_id = cur.lastrowid
        stats.commit()
        return snap_id
    finally:
        stats.close()


def _stage_cache(
    cache_path: pathlib.Path,
    boundary_iso: str,
    applied_at_utc: str = "2026-05-22T00:00:00Z",
) -> None:
    """Stage cache.db with two ``session_entries`` rows:

      * One STRICTLY INSIDE the [range_start, range_end] window (always
        included regardless of the boundary predicate).
      * One whose ``timestamp_utc`` EXACTLY equals ``boundary_iso`` (the
        snapshot's ``range_end_iso``). Pre-fix the migration excluded
        this row (``<`` end); post-fix it's included (``<=`` end).
    """
    cache = sqlite3.connect(cache_path)
    try:
        cache.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE session_files (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER,
                mtime_ns INTEGER,
                last_byte_offset INTEGER,
                last_ingested_at TEXT,
                session_id TEXT,
                project_path TEXT
            );
            CREATE TABLE session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            CREATE TABLE cache_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        cache.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            ("001_dedup_highest_wins", applied_at_utc),
        )
        # cache_meta walk-complete marker: the gate's PROCEED signal now
        # (cctally-dev#93), paired with the non-empty session_entries
        # seeded below.
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('claude_ingest_walk_complete', '2026-05-22T02:00:00Z')"
        )
        # session_files row retained for parity with production seeding;
        # it no longer gates the walk (the marker does).
        cache.execute(
            "INSERT INTO session_files VALUES (?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 100, 0, 100,
                "2026-05-22T01:00:00Z", "s1", "/tmp/proj",
            ),
        )
        # Entry A: strictly inside the window. 1000 output tokens of
        # claude-opus-4-7 at $25/Mtok → $0.025.
        #
        # ``timestamp_utc`` uses the canonical UTC-offset form (``+00:00``)
        # that production's ``sync_cache`` always writes
        # (``entry.timestamp.astimezone(dt.timezone.utc).isoformat()``).
        # The fixture must match the on-disk invariant so the migration's
        # lex compare against canonicalized range bounds (perf P1) hits
        # both rows the same way it does for real users.
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 0, "2026-05-18T00:00:00+00:00",
                "claude-opus-4-7", 0, 1000, 0, 0, "{}",
            ),
        )
        # Entry B: timestamp_utc exactly equals the snapshot's
        # range_end_iso boundary (same physical instant, canonical
        # ``+00:00`` shape). Same model+tokens → another $0.025. Pre-fix
        # the migration's `< unixepoch(range_end_iso)` predicate excluded
        # this row; post-fix it's included (`<=`). Boundary is passed via
        # the migration's range_end_iso (any ISO offset accepted —
        # ``_canonical_utc_iso_for_index`` normalizes); the on-disk
        # entry stays in production-canonical form.
        boundary_canonical = parse_iso_datetime(
            boundary_iso, "boundary"
        ).astimezone(dt.timezone.utc).isoformat()
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, usage_extra_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "/tmp/session1.jsonl", 1, boundary_canonical,
                "claude-opus-4-7", 0, 1000, 0, 0, "{}",
            ),
        )
        cache.commit()
    finally:
        cache.close()


def test_008_boundary_inclusive_includes_entry_at_range_end(
    tmp_path, monkeypatch
):
    """Regression for V1: when a ``session_entries`` row's
    ``timestamp_utc`` exactly equals the snapshot's ``range_end_iso``,
    migration 008 INCLUDES it in the recompute (closed-interval
    semantics matching the production writer).

    Pre-fix:  cost_usd = $0.025 (boundary entry excluded by ``<`` end).
    Post-fix: cost_usd = $0.050 (boundary entry included by ``<=`` end).
    """
    db = _load_db()
    core = db._cctally_core

    boundary_iso = "2026-05-22T00:00:00Z"  # same as range_end_iso

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path, boundary_iso)
    _stage_cache(cache_path, boundary_iso)

    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots"
        ).fetchone()[0]
    finally:
        stats.close()

    # Both entries (the strictly-inside one AND the boundary one)
    # contribute $0.025 each → $0.05 total. The boundary inclusion is
    # the V1 fix; a regression to ``<`` would land $0.025.
    assert cost == pytest.approx(0.050, abs=1e-9), (
        f"migration 008 must include the boundary entry; got {cost!r}. "
        "Regression: half-open `<` end re-introduced."
    )


def test_008_matches_writer_predicate_byte_for_byte(tmp_path, monkeypatch):
    """The migration's recompute over ``[range_start_iso, range_end_iso]``
    matches the production writer's predicate
    (``iter_entries``: closed lex ``timestamp_utc >= ? AND <= ?``).

    Independent verification of the same invariant — derives the
    expected cost by walking ``session_entries`` with the writer's exact
    predicate and asserts equality (within 1e-9 USD) against the
    migration's output. A future drift on either side would break the
    R-DEDUP2 reconcile invariant; this test fails loudly before that.
    """
    db = _load_db()
    core = db._cctally_core

    boundary_iso = "2026-05-22T00:00:00Z"
    range_start_iso = "2026-05-15T00:00:00Z"

    stats_path = tmp_path / "stats.db"
    cache_path = tmp_path / "cache.db"
    _stage_stats(stats_path, boundary_iso)
    _stage_cache(cache_path, boundary_iso)

    _pin_resolver_to_fake_home(core, tmp_path, monkeypatch)
    fake_projects = tmp_path / "claude_projects"
    fake_projects.mkdir()
    (fake_projects / "session1.jsonl").write_text("{}\n")

    monkeypatch.setattr(core, "CACHE_DB_PATH", cache_path)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", fake_projects)

    # Reference cost from a separate walk using the writer's exact
    # closed-interval predicate. Independent of the migration handler.
    cache = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    try:
        writer_rows = cache.execute(
            "SELECT timestamp_utc FROM session_entries "
            "WHERE timestamp_utc >= ? AND timestamp_utc <= ? "
            "ORDER BY timestamp_utc",
            (range_start_iso, boundary_iso),
        ).fetchall()
    finally:
        cache.close()
    # Both entries should be in the writer's set (one strictly inside,
    # one at the boundary).
    assert len(writer_rows) == 2, (
        f"writer predicate found {len(writer_rows)} rows; expected 2 "
        f"(strictly-inside + boundary). Rows: {writer_rows!r}"
    )

    stats = sqlite3.connect(stats_path)
    try:
        db._008_recompute_weekly_cost_snapshots_dedup_fix(stats)
        cost = stats.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots"
        ).fetchone()[0]
    finally:
        stats.close()

    # Each entry: 1000 opus-4-7 output tokens at $25/Mtok = $0.025.
    # Migration must include both → $0.050 total.
    assert cost == pytest.approx(0.050, abs=1e-9)
