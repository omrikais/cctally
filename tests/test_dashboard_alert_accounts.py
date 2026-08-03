"""Issue #345 — conditional account decoration on dashboard alert rows."""
from __future__ import annotations

import importlib

import pytest

from conftest import load_script, redirect_paths


ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _seed_account(conn, *, provider: str, account_key: str, label: str) -> None:
    conn.execute(
        "INSERT INTO accounts "
        "(account_key, provider, natural_id, email, label, plan_type, "
        " label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?, ?, 'pro', 'user', ?, ?)",
        (
            account_key,
            provider,
            f"natural-{account_key[0]}",
            f"{label}@example.com",
            label,
            "2026-07-01T00:00:00Z",
            "2026-07-15T00:00:00Z",
        ),
    )


def _seed_projected_alert(
    conn, *, metric: str, account_key: str, crossed_at: str,
) -> None:
    conn.execute(
        "INSERT INTO projected_milestones "
        "(week_start_at, period, metric, threshold, projected_value, "
        " denominator, crossed_at_utc, alerted_at, account_key) "
        "VALUES ('2026-07-13T00:00:00Z', 'subscription-week', ?, 100, "
        "        104.0, 100.0, ?, ?, ?)",
        (metric, crossed_at, crossed_at, account_key),
    )


def test_claude_alert_rows_gain_conditional_account_key_and_label(ns):
    conn = ns["open_db"]()
    try:
        _seed_account(conn, provider="claude", account_key=ACCOUNT_A, label="work")
        _seed_account(conn, provider="claude", account_key=ACCOUNT_B, label="personal")
        _seed_projected_alert(
            conn,
            metric="weekly_pct",
            account_key=ACCOUNT_A,
            crossed_at="2026-07-15T13:00:00Z",
        )
        _seed_projected_alert(
            conn,
            metric="weekly_pct",
            account_key=ACCOUNT_B,
            crossed_at="2026-07-15T13:01:00Z",
        )
        _seed_projected_alert(
            conn,
            metric="budget_usd",
            account_key="*",
            crossed_at="2026-07-15T13:02:00Z",
        )
        conn.commit()

        rows = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()

    by_key = {row["accountKey"]: row for row in rows}
    assert set(by_key) == {ACCOUNT_A, ACCOUNT_B, "*"}
    assert by_key[ACCOUNT_A]["accountLabel"] == "work"
    assert by_key[ACCOUNT_B]["accountLabel"] == "personal"
    assert by_key["*"]["accountLabel"] == "All accounts"
    # The legacy id remains stable; the account dimension belongs to toast
    # identity rather than mutating a long-standing public row id.
    assert by_key[ACCOUNT_A]["id"] == by_key[ACCOUNT_B]["id"]


def test_single_account_claude_alert_rows_remain_byte_shape_undecorated(ns):
    conn = ns["open_db"]()
    try:
        _seed_account(conn, provider="claude", account_key=ACCOUNT_A, label="work")
        _seed_projected_alert(
            conn,
            metric="weekly_pct",
            account_key=ACCOUNT_A,
            crossed_at="2026-07-15T13:00:00Z",
        )
        conn.commit()
        [row] = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()

    assert "accountKey" not in row
    assert "accountLabel" not in row


def test_every_claude_alert_mapper_threads_the_account_dimension(ns):
    conn = ns["open_db"]()
    try:
        _seed_account(conn, provider="claude", account_key=ACCOUNT_A, label="work")
        _seed_account(conn, provider="claude", account_key=ACCOUNT_B, label="personal")
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, percent_threshold, "
            " cumulative_cost_usd, usage_snapshot_id, cost_snapshot_id, alerted_at, account_key) "
            "VALUES ('2026-07-15T12:00:00Z', '2026-07-13', '2026-07-20', "
            "        90, 45.0, 1, 1, '2026-07-15T12:00:00Z', ?)",
            (ACCOUNT_A,),
        )
        block_id = conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
            " created_at_utc, last_updated_at_utc, account_key) "
            "VALUES (123456, '2026-07-15T15:00:00Z', '2026-07-15T10:00:00Z', "
            "        '2026-07-15T10:00:00Z', '2026-07-15T12:01:00Z', 90, "
            "        '2026-07-15T10:00:00Z', '2026-07-15T12:01:00Z', ?)",
            (ACCOUNT_A,),
        ).lastrowid
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, captured_at_utc, "
            " usage_snapshot_id, alerted_at, account_key) "
            "VALUES (?, 123456, 90, '2026-07-15T12:01:00Z', 1, "
            "        '2026-07-15T12:01:00Z', ?)",
            (block_id, ACCOUNT_A),
        )
        conn.execute(
            "INSERT INTO budget_milestones "
            "(vendor, period_start_at, period, threshold, budget_usd, spent_usd, "
            " consumption_pct, crossed_at_utc, alerted_at, account_key) "
            "VALUES ('claude', '2026-07-13T00:00:00Z', 'subscription-week', "
            "        90, 50, 45, 90, '2026-07-15T12:02:00Z', "
            "        '2026-07-15T12:02:00Z', ?)",
            (ACCOUNT_A,),
        )
        _seed_projected_alert(
            conn,
            metric="weekly_pct",
            account_key=ACCOUNT_A,
            crossed_at="2026-07-15T12:03:00Z",
        )
        conn.execute(
            "INSERT INTO project_budget_milestones "
            "(week_start_at, project_key, threshold, budget_usd, spent_usd, "
            " consumption_pct, crossed_at_utc, alerted_at, account_key) "
            "VALUES ('2026-07-13T00:00:00Z', '/tmp/project', 90, 50, 45, 90, "
            "        '2026-07-15T12:04:00Z', '2026-07-15T12:04:00Z', ?)",
            (ACCOUNT_A,),
        )
        conn.commit()
        rows = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()

    work_rows = [row for row in rows if row.get("accountKey") == ACCOUNT_A]
    assert {row["axis"] for row in work_rows} == {
        "weekly", "five_hour", "budget", "projected", "project_budget",
    }
    assert {row["accountLabel"] for row in work_rows} == {"work"}


def test_codex_alert_rows_keep_internal_key_and_gain_public_account_fields(ns):
    conn = ns["open_db"]()
    try:
        _seed_account(conn, provider="codex", account_key=ACCOUNT_A, label="work")
        _seed_account(conn, provider="codex", account_key=ACCOUNT_B, label="personal")
        _seed_projected_alert(
            conn,
            metric="codex_budget_usd",
            account_key=ACCOUNT_A,
            crossed_at="2026-07-15T13:00:00Z",
        )
        _seed_projected_alert(
            conn,
            metric="codex_budget_usd",
            account_key=ACCOUNT_B,
            crossed_at="2026-07-15T13:01:00Z",
        )
        conn.commit()
        dashboard_sources = importlib.import_module("_cctally_dashboard_sources")
        rows = dashboard_sources._alerts_wire(conn, decorated=True)
        undecorated = dashboard_sources._alerts_wire(
            conn, decorated=False,
        )
    finally:
        conn.close()

    by_key = {row["accountKey"]: row for row in rows}
    assert by_key[ACCOUNT_A]["account_key"] == ACCOUNT_A
    assert by_key[ACCOUNT_A]["accountLabel"] == "work"
    assert by_key[ACCOUNT_B]["accountLabel"] == "personal"
    assert all("accountKey" not in row for row in undecorated)
    assert all("accountLabel" not in row for row in undecorated)
    assert all("account_key" not in row for row in undecorated)
