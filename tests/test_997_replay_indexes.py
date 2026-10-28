"""#997 T-A4 — the replay-scratch indexes serve the statements they were
built for, and only order-insensitive ones (spec D6, T6).

Before the indexes, `detect_reset_and_credit`'s two "latest prior row"
lookups and the reset-crossing sweep's second `EXISTS` each scanned
`weekly_usage_snapshots` per input (`SCAN … USE TEMP B-TREE FOR ORDER BY`),
three quarters of a replay's CPU. An index never changes a result set, but it
can change the visit order of a statement without a total `ORDER BY` and the
cost of other plans (a non-partial variant made the fold decision's `MAX`
70x slower). The audit of every statement is the plan's appendix; these tests
pin its decisive rows.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
W1 = int(dt.datetime(2026, 7, 27, tzinfo=UTC).timestamp())
W2 = W1 + 7 * 86400


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _scratch(tmp_path, name="scratch.db"):
    import _cctally_core
    import _cctally_store
    conn = _cctally_core.open_db(_target_path=str(tmp_path / name))
    _cctally_store.apply_disposable_scratch_policy(conn)
    return conn


def _plan(conn, sql, params=None) -> str:
    if params is None:
        params = [None] * sql.count("?")
    rows = sqlite3.Connection.execute(
        conn, "EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[3]) for row in rows)


#: `detect_reset_and_credit`'s prior weekly row (`_cctally_record`).
PRIOR_WEEKLY = (
    "SELECT week_end_at, weekly_percent FROM weekly_usage_snapshots "
    "WHERE week_end_at IS NOT NULL AND account_key = ? "
    "  AND weekly_observation_held = 0 "
    "ORDER BY captured_at_utc DESC, id DESC LIMIT 1")
#: Its prior five-hour row.
PRIOR_FIVE_HOUR = (
    "SELECT five_hour_window_key, five_hour_percent, five_hour_resets_at "
    "  FROM weekly_usage_snapshots "
    " WHERE five_hour_window_key IS NOT NULL "
    "   AND five_hour_percent IS NOT NULL "
    "   AND account_key = ? "
    " ORDER BY captured_at_utc DESC, id DESC LIMIT 1")
#: The reset-crossing sweep (`_flag_blocks_spanning_week_resets`).
SWEEP = (
    "UPDATE five_hour_blocks SET crossed_seven_day_reset = 1 "
    "WHERE crossed_seven_day_reset = 0 AND account_key = ? "
    "  AND (is_closed = 0 OR journal_id IS NULL) AND ( EXISTS ( "
    "    SELECT 1 FROM week_reset_events e "
    "     WHERE e.account_key = five_hour_blocks.account_key "
    "       AND unixepoch(e.effective_reset_at_utc) "
    "           BETWEEN unixepoch(five_hour_blocks.block_start_at) "
    "               AND unixepoch(five_hour_blocks.last_observed_at_utc) ) "
    "  OR EXISTS ( SELECT 1 FROM weekly_usage_snapshots ws "
    "     WHERE ws.week_start_at IS NOT NULL "
    "       AND ws.account_key = five_hour_blocks.account_key "
    "       AND unixepoch(ws.week_start_at) "
    "           > unixepoch(five_hour_blocks.block_start_at) "
    "       AND unixepoch(ws.week_start_at) "
    "           <= unixepoch(five_hour_blocks.last_observed_at_utc) ) )")
#: `_usage_snapshot_fold_decision`'s weekly maximum (`_cctally_journal`).
FOLD_MAX = (
    "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
    "WHERE week_start_date = ? AND account_key = ? "
    "  AND weekly_observation_held = 0 "
    "  AND unixepoch(captured_at_utc) >= unixepoch(?)")


def _source_has(module, fragment) -> bool:
    import importlib
    import inspect
    text = inspect.getsource(importlib.import_module(module))
    collapse = lambda s: re.sub(r'[\s"]+', " ", s)  # noqa: E731
    return collapse(fragment) in collapse(text)


def test_replay_index_plans(ns, tmp_path):
    """Spec T6: on a policy-applied scratch the two prior-row lookups search
    `replay_usage_latest_*` and the sweep's second `EXISTS` searches
    `replay_usage_week_start`. Without the policy each scans the table and
    sorts in a temp b-tree (the control below)."""
    import _cctally_core
    assert _source_has("_cctally_record", "WHERE week_end_at IS NOT NULL "
                       "AND account_key = ? AND weekly_observation_held = 0 "
                       "ORDER BY captured_at_utc DESC, id DESC LIMIT 1")
    assert _source_has("_cctally_record", "WHERE ws.week_start_at IS NOT NULL")
    conn = _scratch(tmp_path)
    try:
        weekly = _plan(conn, PRIOR_WEEKLY)
        five = _plan(conn, PRIOR_FIVE_HOUR)
        sweep = _plan(conn, SWEEP)
    finally:
        conn.close()
    assert "USING INDEX replay_usage_latest_weekly" in weekly, weekly
    assert "TEMP B-TREE" not in weekly, weekly
    assert "USING INDEX replay_usage_latest_five_hour" in five, five
    assert "TEMP B-TREE" not in five, five
    assert "USING INDEX replay_usage_week_start" in sweep, sweep

    control = _cctally_core.open_db(_target_path=str(tmp_path / "plain.db"))
    try:
        assert "replay_usage" not in _plan(control, PRIOR_WEEKLY)
        assert "replay_usage" not in _plan(control, SWEEP)
    finally:
        control.close()


def test_fold_decision_max_keeps_date_index(ns, tmp_path):
    """Spec T6: the partial predicates keep the fold decision's weekly `MAX`
    on the week-date index; a non-partial replay index won it and ran 70x
    slower."""
    assert _source_has("_cctally_journal",
                       "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots")
    conn = _scratch(tmp_path)
    try:
        plan = _plan(conn, FOLD_MAX)
    finally:
        conn.close()
    assert "idx_usage_week_date_group" in plan, plan
    assert "replay_usage" not in plan, plan


# --------------------------------------------------------------------------
# Every statement a replay actually runs: a replay index serves only
# order-insensitive ones.
# --------------------------------------------------------------------------

def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _readings(J):
    """Readings crossing a weekly reset, a five-hour block, then a descent of
    both axes on the same reset (the credit paths)."""
    base = W1 - 4 * 3600
    plan = [(0, 20.0, W1, 30.0), (1, 24.0, W1, 40.0), (2, 28.0, W1, 50.0),
            (3, 12.0, W1, 10.0), (4, 13.0, W1, 12.0), (9, 2.0, W2, 5.0),
            (10, 3.0, W2, 6.0), (11, 1.0, W2, 7.0)]
    out = []
    for step, weekly, resets, five in plan:
        captured = base + step * 1800
        out.append(J.make_obs(
            at=_iso(captured), src="record-usage", provider="claude",
            account="acct-a", payload={
                "captured_at": _iso(captured), "source": "statusline",
                "weekly_percent": weekly, "resets_at": resets,
                "five_hour_percent": five,
                "five_hour_resets_at": _iso(
                    W1 + 3600 if captured < W1 + 3600 else W1 + 6 * 3600)}))
    return out


#: A statement whose result cannot depend on visit order: a total order
#: ending in the unique `id` (with or without `LIMIT 1`), an aggregate, an
#: existence probe, or the sweep's per-row flag write.
_ORDER_INSENSITIVE = (
    re.compile(r"ORDER BY [^;]*\bid\s+(ASC|DESC)(\s+LIMIT\s+1)?\s*$",
               re.S | re.I),
    re.compile(r"^\s*SELECT\s+(MAX|MIN)\(", re.I),
    re.compile(r"^\s*SELECT\s+1\s+FROM\b.*\bLIMIT\s+1\s*$", re.S | re.I),
    re.compile(r"^\s*UPDATE\s+five_hour_blocks\s+SET\s+"
               r"crossed_seven_day_reset\b", re.I),
)


def test_replay_indexes_only_serve_order_insensitive_statements(ns, tmp_path):
    """Spec D6: every statement over `weekly_usage_snapshots` a replay runs
    (weekly reset, five-hour block, descents) is planned on the scratch; any
    that a replay index serves is order-insensitive."""
    import _cctally_completion
    import _cctally_quarantine
    import _cctally_rederive
    import _lib_journal as J

    conn = _scratch(tmp_path)
    statements: list = []

    def trace(sql):
        if "weekly_usage_snapshots" in sql and not sql.lstrip().startswith(
                "--"):
            statements.append(sql)

    try:
        conn.set_trace_callback(trace)
        _cctally_rederive.replay_scratch_records(
            conn, _readings(J), None,
            fold=_cctally_quarantine.QuarantineFold.empty(
                held_ids=frozenset()),
            read_adapters=_cctally_completion._no_cache_read_adapters())
        conn.set_trace_callback(None)
        served = {}
        for sql in dict.fromkeys(statements):
            plan = _plan(conn, sql)
            if "replay_usage_" in plan:
                served[sql] = plan
    finally:
        conn.close()
    assert len(set(statements)) >= 5, "the replay ran too few statements"
    assert served, "non-vacuity: a replay index served a statement"
    offending = {sql: plan for sql, plan in served.items()
                 if not any(p.search(sql) for p in _ORDER_INSENSITIVE)}
    assert not offending, offending
