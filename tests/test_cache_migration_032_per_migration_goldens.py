"""Per-migration goldens for cache migration ``032_codex_canonical_reset_anchor``
(#416 spec sections 4.1/4.2).

``quota_window_snapshots.canonical_resets_at_utc`` is a plain column addition, so
it lands through ``add_column_if_missing`` in ``_apply_cache_schema`` — the
idempotent-guard pattern, no marker and no version. Migration 032 exists for the
BACKFILL, which is a data-shape change and therefore does go through the
framework, and registering it bumps the head so a steady-state install re-runs
the version-gated schema apply and actually gains the column.

The ``pre.sqlite`` golden reproduces the real pre-slice-2 on-disk shape: an
install at the 031 head, four Codex quota observations, and the column absent.
Three of those observations are three spellings of one physical weekly reset
(including the stray ``10081`` window length, which shares the cluster) and must
collapse onto one anchor; the fourth is a genuinely later week and anchors
itself.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): this module exercises the handler's
# second-invocation idempotency.
IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "032_codex_canonical_reset_anchor"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "032_codex_canonical_reset_anchor"
_COLUMN = "canonical_resets_at_utc"
_CLUSTER_ANCHOR = "2026-08-01T19:19:03Z"
_LATER_WEEK_ANCHOR = "2026-08-08T19:19:00Z"


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _columns(conn, table: str) -> set:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _anchors(conn) -> set:
    return {
        row[0] for row in conn.execute(
            f"SELECT DISTINCT {_COLUMN} FROM quota_window_snapshots "
            "WHERE source='codex'")
    }


def test_pre_fixture_at_031_head_without_the_column(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='031_codex_file_account_map'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 0
        assert _COLUMN not in _columns(conn, "quota_window_snapshots"), (
            f"{_COLUMN} must be ABSENT in pre.sqlite — otherwise the golden "
            "does not exercise the version-gated column add at all")
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 4
    finally:
        conn.close()


def test_post_fixture_collapses_the_jitter_cluster(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _COLUMN in _columns(conn, "quota_window_snapshots")
        assert _anchors(conn) == {_CLUSTER_ANCHOR, _LATER_WEEK_ANCHOR}
        assert conn.execute(
            f"SELECT COUNT(*) FROM quota_window_snapshots WHERE {_COLUMN}=?",
            (_CLUSTER_ANCHOR,)).fetchone()[0] == 3, (
            "the three jittered spellings of one weekly reset — including the "
            "stray 10081 window length — must share ONE anchor")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_raw_reset_is_retained_verbatim_as_evidence(cctally_module):
    """Spec section 4.1: "Raw provider values are retained unchanged as
    evidence." Canonicalization adds a column; it never rewrites one."""
    conn = sqlite3.connect(POST_DB)
    try:
        raw = sorted(
            row[0] for row in conn.execute(
                "SELECT resets_at_utc FROM quota_window_snapshots "
                "WHERE source='codex'"))
        assert raw == [
            "2026-08-01T19:19:03Z", "2026-08-01T19:19:04Z",
            "2026-08-01T19:19:06Z", "2026-08-08T19:19:00Z",
        ]
    finally:
        conn.close()


def test_handler_backfills_from_the_pre_fixture(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        # The column add is `_apply_cache_schema`'s job (the dispatcher runs it
        # ahead of the handlers), so the golden path does the same.
        sys.modules['_cctally_db']._apply_cache_schema(conn)
        _handler(cctally_module)(conn)
        assert _anchors(conn) == {_CLUSTER_ANCHOR, _LATER_WEEK_ANCHOR}
    finally:
        conn.close()


def test_handler_declines_cleanly_without_the_column(cctally_module, tmp_path):
    """A legacy-shape cache whose FTS early-return fires before the column add
    must not raise: the reader's raw-reset fallback keeps behaviour identical."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _COLUMN not in _columns(conn, "quota_window_snapshots")
        _handler(cctally_module)(conn)  # must not raise
        assert _COLUMN not in _columns(conn, "quota_window_snapshots")
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        sys.modules['_cctally_db']._apply_cache_schema(conn)
        handler = _handler(cctally_module)
        handler(conn)
        first = sorted(conn.execute(
            f"SELECT id, {_COLUMN} FROM quota_window_snapshots"))
        handler(conn)
        assert sorted(conn.execute(
            f"SELECT id, {_COLUMN} FROM quota_window_snapshots")) == first
    finally:
        conn.close()
