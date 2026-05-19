"""Perf regression tests for the per-click drill paths.

Neither assertion is wall-clock-bound (CI variability would flake it).
Both pin the OPTIMIZATION SHAPE: the project drill must filter at SQL
via the ``_drill_paths`` TEMP TABLE (not in Python after a full window
scan), and the session-detail endpoint must take the indexed
``session_files``-lookup fast path (not the 365-day full-aggregate
fallback).

These tests fail if a refactor accidentally reverts to the
pre-optimization shape (see commit history for context — the drill
used to walk every entry in the window and filter in Python; the
session detail used to aggregate every session in 365 days).
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script  # noqa: E402

_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402


FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "projects"
NOW_UTC = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _open(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


# --- Fix A regression: drill stages bucket paths into TEMP TABLE ---------


def test_drill_uses_drill_paths_temp_table():
    """The project drill must stage the bucket's source_paths into a
    TEMP TABLE and INNER JOIN the entries walk against it, so the
    engine only touches one project's rows.

    Regression: if a refactor reverts to the prior shape
    (``_projects_iter_session_entries`` walking the full window and
    Python-side ``if pkey.bucket_path != bucket_path: continue``),
    the entries SQL won't reference ``_drill_paths`` and this test
    catches it.
    """
    conn = _open(FIXTURE_DIR / "multi-week.db")
    captured: list[str] = []
    conn.set_trace_callback(lambda s: captured.append(s))
    try:
        detail = _cctally_dashboard._project_detail_for_window(
            conn,
            project_key="cctally-dev",
            weeks_back=4,
            now_utc=NOW_UTC,
            current_week=None,
        )
    finally:
        conn.set_trace_callback(None)
    assert detail is not None
    # The entries walk must INNER JOIN _drill_paths. Any other shape
    # (e.g. a bare full-window scan + Python filter) would not contain
    # the table name in any executed statement.
    joined = "\n".join(captured)
    assert "_drill_paths" in joined, (
        "Drill entries query must reference the _drill_paths TEMP "
        "TABLE — full-window Python-side filtering is a perf "
        "regression. Trace:\n" + joined
    )
    # And there must be exactly one INNER JOIN against it — guards
    # against an accidental duplicate scan that would pay the cost
    # twice.
    inner_join_count = sum(
        1 for s in captured if "INNER JOIN _drill_paths" in s
    )
    assert inner_join_count == 1, (
        f"Expected exactly one INNER JOIN _drill_paths in the drill "
        f"query, saw {inner_join_count}. Trace:\n{joined}"
    )


def test_drill_skips_envelope_when_passed_explicitly():
    """When ``projects_envelope`` is passed, ``_build_projects_envelope``
    must not be called again. The sync thread has already paid for it
    on this snapshot tick; rebuilding wastes ~1-2s on a real DB.
    """
    conn = _open(FIXTURE_DIR / "multi-week.db")
    # Build the envelope once via the legacy path.
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=4,
    )
    # Reset the per-process memo so a rebuild would be observable.
    _cctally_dashboard._projects_reset_memo()
    # Spy on the builder.
    original = _cctally_dashboard._build_projects_envelope
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    _cctally_dashboard._build_projects_envelope = _spy
    try:
        detail = _cctally_dashboard._project_detail_for_window(
            conn,
            project_key="cctally-dev",
            weeks_back=4,
            now_utc=NOW_UTC,
            current_week=None,
            projects_envelope=env,
        )
    finally:
        _cctally_dashboard._build_projects_envelope = original
    assert detail is not None
    assert call_count["n"] == 0, (
        "When projects_envelope is passed, _build_projects_envelope "
        "must NOT be rebuilt. The sync thread already paid for it "
        f"on this tick. Rebuild count: {call_count['n']}."
    )


def test_drill_rebuilds_envelope_when_not_passed():
    """Conversely, when ``projects_envelope`` is None (the test /
    reconcile-harness path), the builder must still run — backwards
    compatibility for callers that don't carry a snapshot."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    _cctally_dashboard._projects_reset_memo()
    original = _cctally_dashboard._build_projects_envelope
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    _cctally_dashboard._build_projects_envelope = _spy
    try:
        detail = _cctally_dashboard._project_detail_for_window(
            conn,
            project_key="cctally-dev",
            weeks_back=4,
            now_utc=NOW_UTC,
            current_week=None,
        )
    finally:
        _cctally_dashboard._build_projects_envelope = original
    assert detail is not None
    assert call_count["n"] == 1, (
        f"Legacy path (no projects_envelope kwarg) must rebuild the "
        f"envelope; saw {call_count['n']} rebuilds."
    )


# --- Fix B regression: session detail takes indexed fast path ------------


def test_session_detail_indexed_lookup_exists():
    """The session-detail builder must expose
    ``_tui_build_session_detail_indexed`` — the indexed fast path.

    If a refactor inlines it back into ``_tui_build_session_detail``
    without preserving the indexed-then-fallback split, this test
    breaks: the docstring's "indexed direct fetch" contract is the
    perf invariant.
    """
    # _NS is the globals dict from load_script(); the symbols are
    # re-exported into it via the `from _cctally_tui import …` block
    # near the top of bin/cctally.
    assert "_tui_build_session_detail_indexed" in _NS, (
        "_tui_build_session_detail_indexed must be a top-level symbol "
        "so it can be patched / probed separately from the bulk-scan "
        "fallback. Inlining it back is a perf regression."
    )
    assert "_tui_build_session_detail" in _NS, (
        "_tui_build_session_detail must remain exposed for the HTTP "
        "handler dispatch chain."
    )


def test_session_detail_fast_path_runs_before_fallback():
    """Patch the bulk-scan path so it raises if reached. A real
    session id in the cache must resolve via the fast path alone."""
    # Reach the real cctally module via sys.modules — _NS is just the
    # globals dict and rebinding entries there doesn't affect the
    # function closures inside _cctally_tui.
    cct = sys.modules.get("cctally")
    if cct is None:
        pytest.skip("cctally module not yet loaded; load_script() didn't register it")
    cache_db_path = pathlib.Path.home() / ".local/share/cctally/cache.db"
    if not cache_db_path.exists():
        pytest.skip("real cache.db not present — fast-path runtime check skipped")
    conn = sqlite3.connect(cache_db_path)
    row = conn.execute(
        "SELECT session_id FROM session_files "
        "WHERE session_id IS NOT NULL LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        pytest.skip("no session_files rows with session_id — skip")
    sid = row[0]
    # Tripwire the fallback path. The _cctally_tui shim resolves
    # `get_claude_session_entries` lazily via sys.modules["cctally"],
    # so monkeypatching on the cctally namespace propagates correctly.
    original_get = cct.get_claude_session_entries
    fallback_calls = {"n": 0}

    def _tripwire(*args, **kwargs):
        fallback_calls["n"] += 1
        return original_get(*args, **kwargs)

    cct.get_claude_session_entries = _tripwire
    try:
        detail = cct._tui_build_session_detail(sid, now_utc=NOW_UTC)
    finally:
        cct.get_claude_session_entries = original_get
    assert detail is not None, (
        "Known session_id must resolve via fast OR slow path."
    )
    assert fallback_calls["n"] == 0, (
        f"Fast path missed for a session_id present in session_files; "
        f"slow-path bulk fetch ran ({fallback_calls['n']} times). "
        f"This is the perf regression we're guarding against."
    )
