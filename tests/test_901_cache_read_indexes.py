"""#901 §5.1-§5.3: the cache.db read indexes cache migration 047 delivers.

The W2 lookups and the general quota loader prove their plans in their own
modules (Tasks A4/A5). This module pins the definitions, and the pricing scan,
whose SQL #901 does not change: the covering model-first indexes alone turn both
of its modes (doctor's trailing 30 days, pricing-check's all history) into a
streaming index scan; and the window-attribution read (Amendment 1 item 1,
``c901-design-2``), which streams in assertion order through
``idx_codex_window_attributions_read_order`` and returns exactly the tuple the
pre-#901 statement returned (``LEGACY_ATTRIBUTION_SQL``, frozen from
``56e66f07a``).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

NOW = dt.datetime(2026, 7, 20, 12, tzinfo=dt.timezone.utc)

EXPECTED_DEFINITIONS = {
    "idx_qws_codex_load_order": (
        "ON quota_window_snapshots(source_root_key, captured_at_utc, "
        "resets_at_utc, source_path, line_offset, canonical_resets_at_utc) "
        "WHERE source='codex'"),
    "idx_codex_entries_root_path_time": (
        "ON codex_session_entries(source_root_key, source_path, timestamp_utc)"),
    "idx_codex_files_alias_recent": (
        "ON codex_session_files(last_ingested_at, path) WHERE "
        "last_native_thread_id IS NOT NULL AND last_native_thread_id != ''"),
    "idx_codex_threads_recent": (
        "ON codex_conversation_threads(last_seen_utc, conversation_key)"),
    "idx_entries_model_time": (
        "ON session_entries(model, timestamp_utc, input_tokens, output_tokens, "
        "cache_create_tokens, cache_read_tokens)"),
    "idx_codex_entries_model_time": (
        "ON codex_session_entries(model, timestamp_utc, total_tokens)"),
    "idx_codex_window_attributions_read_order": (
        "ON codex_window_attributions(window_minutes ASC, asserted_at_utc ASC, "
        "op_id ASC)"),
}


@pytest.fixture
def store(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        conn.executemany(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, msg_id, req_id, input_tokens,"
            " output_tokens, cache_create_tokens, cache_read_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [("/p/a.jsonl", i, ts, model, f"m{i}", f"r{i}", 10, 5, 3, 2)
             for i, (ts, model) in enumerate((
                 ("2026-07-19T10:00:00Z", "claude-opus-4-7"),
                 ("2026-07-19T11:00:00Z", "claude-sonnet-4-6"),
                 ("2026-05-01T11:00:00Z", "claude-opus-4-7"),
             ))])
        conn.executemany(
            "INSERT INTO codex_session_entries (source_path, line_offset,"
            " timestamp_utc, session_id, model, total_tokens, source_root_key)"
            " VALUES (?,?,?,?,?,?,?)",
            [("/c/a.jsonl", i, ts, "s1", model, 100, "r" * 32)
             for i, (ts, model) in enumerate((
                 ("2026-07-19T10:00:00Z", "gpt-5"),
                 ("2026-05-01T10:00:00Z", "gpt-5.3-codex-spark"),
             ))])
        conn.commit()
    finally:
        conn.close()
    return ns


def _normalized(sql: str) -> str:
    return " ".join(str(sql).split())


def test_every_read_index_has_its_declared_definition(store):
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        for name, tail in EXPECTED_DEFINITIONS.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,)).fetchone()
            assert row is not None, f"{name} is missing from a fresh cache.db"
            assert _normalized(row[0]).endswith(_normalized(tail)), row[0]
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["trailing-window", "all-history"])
def test_pricing_scan_streams_through_the_covering_model_indexes(store, mode):
    import _cctally_pricing_check as pricing

    with guard.capture_sql_plans() as recorder:
        if mode == "trailing-window":
            pricing._pricing_observed_models(NOW)
        else:
            pricing._pricing_observed_models(NOW, since=None)
    scans = [statement for statement in recorder.statements
             if statement.sql.endswith("GROUP BY model")]
    assert len(scans) == 2, [statement.sql for statement in scans]
    for statement in scans:
        assert not any(marker in detail for detail in statement.plan
                       for marker in guard.TEMP_MARKERS), statement
        assert any("USING COVERING INDEX idx_entries_model_time" in detail
                   or "USING COVERING INDEX idx_codex_entries_model_time" in detail
                   for detail in statement.plan), statement


def test_pricing_scan_output_does_not_depend_on_the_indexes(store):
    import _cctally_core
    import _cctally_pricing_check as pricing

    with_indexes = (pricing._pricing_observed_models(NOW),
                    pricing._pricing_observed_models(NOW, since=None))
    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        conn.execute("DROP INDEX idx_entries_model_time")
        conn.execute("DROP INDEX idx_codex_entries_model_time")
        conn.commit()
    finally:
        conn.close()
    without_indexes = (pricing._pricing_observed_models(NOW),
                       pricing._pricing_observed_models(NOW, since=None))
    assert with_indexes == without_indexes
    assert len(with_indexes[1]) == 4, "non-vacuity: four observed models"


# ── Amendment 1 item 1: the window-attribution read order ────────────────────

LEGACY_ATTRIBUTION_SQL = (
    "SELECT op_id, account_key, source_root_key, logical_limit_key, "
    "       observed_slot, window_minutes, raw_resets_at_utc, "
    "       canonical_resets_at_utc, asserted_at_utc "
    "FROM codex_window_attributions "
    "WHERE retracted_by_op_id IS {retracted} "
    "  AND window_minutes = ? "
    "  AND account_key <> ?"
)
ROOT_1 = "1" * 32
ROOT_2 = "2" * 32
ROOT_3 = "3" * 32
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
WEEK = ["2026-07-21T00:00:00Z"]
#: (op_id, account, root, window, witnesses JSON, asserted, retracted_by).
#: Inserted in this order, which is deliberately NOT the assertion order.
ATTRIBUTIONS = (
    ("o:5", ACCOUNT_A, ROOT_1, 10080, json.dumps(WEEK),
     "2026-07-20T10:00:00.000000Z", None),
    # Equal assertion instant: op_id breaks the tie (o:3 before o:5).
    ("o:3", ACCOUNT_B, ROOT_2, 10080, json.dumps(WEEK),
     "2026-07-20T10:00:00.000000Z", None),
    ("o:1", ACCOUNT_A, ROOT_1, 10080, json.dumps(WEEK),
     "2026-07-20T11:00:00.000000Z", None),
    ("o:9", ACCOUNT_A, ROOT_3, 10080, json.dumps(WEEK),
     "2026-07-19T09:00:00.000000Z", None),
    # Excluded: the sentinel account, and a 5-hour window.
    ("o:2", "unattributed", ROOT_1, 10080, json.dumps(WEEK),
     "2026-07-18T00:00:00.000000Z", None),
    ("o:4", ACCOUNT_A, ROOT_1, 300, json.dumps(WEEK),
     "2026-07-18T00:00:00.000000Z", None),
    # Malformed witnesses: skipped after the read, wherever they sort.
    ("o:6", ACCOUNT_B, ROOT_2, 10080, "{not json",
     "2026-07-17T00:00:00.000000Z", None),
    ("o:7", ACCOUNT_B, ROOT_2, 10080, "[]", "2026-07-17T00:00:00.000000Z", None),
    ("o:11", ACCOUNT_A, ROOT_2, 10080, json.dumps(WEEK[0]),
     "2026-07-21T00:00:00.000000Z", None),
    # Retracted records (the retracted read returns these), one tie, one
    # malformed and one on the sentinel account.
    ("o:8", ACCOUNT_B, ROOT_1, 10080, json.dumps(WEEK),
     "2026-07-20T12:00:00.000000Z", "o:x1"),
    ("o:10", ACCOUNT_A, ROOT_3, 10080, json.dumps(WEEK),
     "2026-07-20T12:00:00.000000Z", "o:x2"),
    ("o:12", ACCOUNT_B, ROOT_3, 10080, "{not json",
     "2026-07-16T00:00:00.000000Z", "o:x3"),
    ("o:13", "unattributed", ROOT_2, 10080, json.dumps(WEEK),
     "2026-07-15T00:00:00.000000Z", "o:x4"),
    ("o:14", ACCOUNT_B, ROOT_2, 10080, json.dumps(WEEK),
     "2026-07-14T00:00:00.000000Z", "o:x5"),
)
ROOT_SELECTIONS = {
    "absent": None,
    "empty": set(),
    "single": {ROOT_1},
    "multiple": {ROOT_1, ROOT_2},
    "every": {ROOT_1, ROOT_2, ROOT_3},
    "unknown": {"z" * 32},
}


def _legacy_attributions(conn, *, source_root_keys=None, retracted_only=False):
    """``load_active_window_attributions`` as it was at ``56e66f07a``."""
    sql = LEGACY_ATTRIBUTION_SQL.format(
        retracted="NOT NULL" if retracted_only else "NULL")
    params: list = [10080, "unattributed"]
    if source_root_keys is not None:
        keys = sorted({str(k) for k in source_root_keys})
        if not keys:
            return ()
        sql += " AND source_root_key IN (%s)" % ",".join("?" * len(keys))
        params.extend(keys)
    sql += " ORDER BY asserted_at_utc ASC, op_id ASC"
    rows = []
    for row in conn.execute(sql, params):
        try:
            witnesses = json.loads(row[6])
        except (ValueError, TypeError):
            continue
        if not isinstance(witnesses, list) or not witnesses:
            continue
        rows.append({
            "op_id": row[0], "account_key": row[1], "source_root_key": row[2],
            "logical_limit_key": row[3], "observed_slot": row[4],
            "window_minutes": int(row[5]),
            "raw_resets_at_utc": tuple(str(w) for w in witnesses),
            "canonical_resets_at_utc": row[7], "asserted_at_utc": row[8],
        })
    return tuple(rows)


@pytest.fixture
def attributions(store):
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        conn.executemany(
            "INSERT INTO codex_window_attributions (op_id, account_key,"
            " source_root_key, logical_limit_key, observed_slot, window_minutes,"
            " raw_resets_at_utc, canonical_resets_at_utc, asserted_at_utc,"
            " retracted_by_op_id) VALUES (?,?,?,'weekly','primary',?,?,?,?,?)",
            [(op, account, root, window, witnesses, WEEK[0], asserted, retracted)
             for op, account, root, window, witnesses, asserted, retracted
             in ATTRIBUTIONS])
        conn.commit()
    finally:
        conn.close()
    return store


@pytest.mark.parametrize("retracted_only", [False, True])
@pytest.mark.parametrize("selection", sorted(ROOT_SELECTIONS))
def test_the_attribution_read_returns_the_legacy_tuple(
    attributions, selection, retracted_only,
):
    import _cctally_cache
    import _cctally_core

    roots = ROOT_SELECTIONS[selection]
    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        new = _cctally_cache.load_active_window_attributions(
            conn, source_root_keys=roots, retracted_only=retracted_only)
        old = _legacy_attributions(
            conn, source_root_keys=roots, retracted_only=retracted_only)
    finally:
        conn.close()
    assert new == old


def test_the_attribution_fixture_exercises_ties_and_exclusions(attributions):
    """Non-vacuity of the equivalence matrix above."""
    import _cctally_cache
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        active = _cctally_cache.load_active_window_attributions(conn)
        retracted = _cctally_cache.load_active_window_attributions(
            conn, retracted_only=True)
        multiple = _cctally_cache.load_active_window_attributions(
            conn, source_root_keys={ROOT_1, ROOT_2})
    finally:
        conn.close()
    assert [row["op_id"] for row in active] == ["o:9", "o:3", "o:5", "o:1"]
    assert [row["op_id"] for row in retracted] == ["o:14", "o:10", "o:8"]
    assert [row["op_id"] for row in multiple] == ["o:3", "o:5", "o:1"]


@pytest.mark.parametrize("retracted_only", [False, True])
@pytest.mark.parametrize("selection", ["absent", "single", "every"])
def test_the_attribution_read_streams_in_assertion_order(
    attributions, selection, retracted_only,
):
    import _cctally_cache
    import _cctally_core

    with guard.capture_sql_plans() as recorder:
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        try:
            _cctally_cache.load_active_window_attributions(
                conn, source_root_keys=ROOT_SELECTIONS[selection],
                retracted_only=retracted_only)
        finally:
            conn.close()
    reads = [statement for statement in recorder.statements
             if "FROM codex_window_attributions" in statement.sql]
    assert len(reads) == 1, [statement.sql for statement in reads]
    (read,) = reads
    assert not any(marker in detail for detail in read.plan
                   for marker in guard.TEMP_MARKERS), read.plan
    assert any("idx_codex_window_attributions_read_order" in detail
               for detail in read.plan), read.plan
