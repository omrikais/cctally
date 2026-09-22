"""Issue #345 — conditional account decoration on dashboard alert rows."""
from __future__ import annotations

import importlib
import threading

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import post_json, serve_dashboard, stop


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


def _wire_alert_test_handler(ns):
    handler = ns["DashboardHTTPHandler"]
    handler.hub = ns["SSEHub"]()
    handler.snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    handler.static_dir = ns["STATIC_DIR"]
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda: None)
    handler.run_sync_now_locked = staticmethod(lambda: None)
    handler.no_sync = False
    handler.display_tz_pref_override = None


@pytest.mark.parametrize("account_order", [
    (),
    ("unattributed",),
    ("unattributed", ACCOUNT_A),
    (ACCOUNT_A, "unattributed", ACCOUNT_B),
    (ACCOUNT_B, ACCOUNT_A, "unattributed"),
])
def test_alert_test_response_matches_real_envelope_account_decoration(
    ns, monkeypatch, account_order,
):
    """R8 is provider-local; the response must not expose the dispatch key."""
    conn = ns["open_db"]()
    try:
        for provider in ("claude", "codex"):
            for key in account_order:
                if provider == "codex" and key == "unattributed":
                    # The sentinel is a global registry key, not one row per
                    # provider; it must not count as a second real account.
                    continue
                provider_key = (
                    {ACCOUNT_A: "c" * 32, ACCOUNT_B: "d" * 32}.get(key, key)
                    if provider == "codex" else key
                )
                _seed_account(
                    conn, provider=provider, account_key=provider_key,
                    label="missing" if key == "unattributed" else key[:1],
                )
        _seed_projected_alert(
            conn, metric="weekly_pct", account_key="*",
            crossed_at="2026-07-15T13:00:00Z",
        )
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " percent_threshold, cumulative_cost_usd, usage_snapshot_id, "
            " cost_snapshot_id, alerted_at, account_key) "
            "VALUES ('2026-07-15T12:00:00Z', '2026-07-13', '2026-07-20', "
            "90, 45, 1, 1, '2026-07-15T12:00:00Z', 'unattributed')"
        )
        conn.execute(
            "INSERT INTO budget_milestones "
            "(vendor, period_start_at, period, threshold, budget_usd, "
            " spent_usd, consumption_pct, crossed_at_utc, alerted_at, "
            " account_key) VALUES ('codex', '2026-07-01T00:00:00Z', "
            " 'calendar-month', 90, 200, 180, 90, "
            " '2026-07-15T13:01:00Z', '2026-07-15T13:01:00Z', '*')"
        )
        conn.commit()
        real_rows = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
        row_by_axis = {row["axis"]: row for row in real_rows}
        before_rows = conn.execute(
            "SELECT (SELECT count(*) FROM projected_milestones), "
            "(SELECT count(*) FROM budget_milestones), "
            "(SELECT count(*) FROM percent_milestones)"
        ).fetchone()
    finally:
        conn.close()

    dispatched = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: (
            dispatched.append((mode, dict(payload))) or "queued"
        ),
    )
    _wire_alert_test_handler(ns)
    srv, thread, port = serve_dashboard(ns)
    try:
        responses = {}
        for axis, metric in (("weekly", None), ("projected", "weekly_pct"),
                             ("codex_budget", None)):
            request = {"axis": axis}
            if metric:
                request["metric"] = metric
            status, body = post_json(port, "/api/alerts/test", request)
            assert status == 200, body
            assert body["dispatch"] == "queued"
            responses[axis] = body["alert"]
    finally:
        stop(srv, thread)

    decorated = ACCOUNT_B in account_order
    assert [mode for mode, _ in dispatched] == ["test"] * 3
    for axis, raw_key in (("weekly", "unattributed"),
                          ("projected", "*"), ("codex_budget", "*")):
        payload = responses[axis]
        raw = next(item for _, item in dispatched if item["axis"] == axis)
        assert raw["account_key"] == raw_key
        assert "accountKey" not in raw and "accountLabel" not in raw
        assert "account_key" not in payload
        expected = {k: row_by_axis[axis][k] for k in
                    ("accountKey", "accountLabel") if k in row_by_axis[axis]}
        assert {k: payload[k] for k in ("accountKey", "accountLabel")
                if k in payload} == expected
        assert expected == (
            {"accountKey": raw_key,
             "accountLabel": ("Unattributed" if raw_key == "unattributed"
                              else "All accounts")}
            if decorated else {}
        )

    conn = ns["open_db"]()
    try:
        after_rows = conn.execute(
            "SELECT (SELECT count(*) FROM projected_milestones), "
            "(SELECT count(*) FROM budget_milestones), "
            "(SELECT count(*) FROM percent_milestones)"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(after_rows) == tuple(before_rows)


@pytest.mark.parametrize("decorated_provider", ["claude", "codex"])
def test_alert_test_resolves_projected_metric_and_all_axis_providers(
    ns, monkeypatch, decorated_provider,
):
    conn = ns["open_db"]()
    try:
        for provider, first, second in (
            ("claude", ACCOUNT_A, ACCOUNT_B),
            ("codex", "c" * 32, "d" * 32),
        ):
            _seed_account(conn, provider=provider, account_key=first,
                          label="first")
            if provider == decorated_provider:
                _seed_account(conn, provider=provider, account_key=second,
                              label="second")
        conn.commit()
    finally:
        conn.close()

    dispatched = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: (
            dispatched.append((mode, dict(payload))) or "queued"
        ),
    )
    _wire_alert_test_handler(ns)
    srv, thread, port = serve_dashboard(ns)
    cases = (
        ("weekly", None, "claude", "unattributed"),
        ("five_hour", None, "claude", "unattributed"),
        ("budget", None, "claude", "*"),
        ("project_budget", None, "claude", "*"),
        ("projected", "weekly_pct", "claude", "*"),
        ("projected", "budget_usd", "claude", "*"),
        ("projected", "codex_budget_usd", "codex", "*"),
        ("codex_budget", None, "codex", "*"),
    )
    try:
        for axis, metric, provider, raw_key in cases:
            request = {"axis": axis}
            if metric:
                request["metric"] = metric
            status, body = post_json(port, "/api/alerts/test", request)
            assert status == 200, body
            assert body["dispatch"] == "queued"
            response = body["alert"]
            dispatched_payload = dispatched[-1][1]
            assert dispatched[-1][0] == "test"
            assert dispatched_payload["account_key"] == raw_key
            assert "account_key" not in response
            if provider == decorated_provider:
                assert response["accountKey"] == raw_key
                assert response["accountLabel"] == (
                    "Unattributed" if raw_key == "unattributed"
                    else "All accounts"
                )
            else:
                assert "accountKey" not in response
                assert "accountLabel" not in response
    finally:
        stop(srv, thread)


def test_alert_test_without_stats_db_does_not_create_one(ns, monkeypatch):
    assert not ns["DB_PATH"].exists()
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: "queued",
    )
    _wire_alert_test_handler(ns)
    srv, thread, port = serve_dashboard(ns)
    try:
        status, body = post_json(port, "/api/alerts/test", {"axis": "weekly"})
    finally:
        stop(srv, thread)
    assert status == 200, body
    assert body["dispatch"] == "queued"
    assert "account_key" not in body["alert"]
    assert "accountKey" not in body["alert"]
    assert not ns["DB_PATH"].exists()
