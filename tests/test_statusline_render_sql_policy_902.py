"""#902: eligible statusline projection reads never sort retained history.

Plans are judged under MEMORY and a FILE control. Output selection is checked
against explicit observations with opposite SQLite scan order; MEMORY alone
does not discharge a history-sized SQL sorter.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import io
import json
import sys
import time

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    app = sys.modules["cctally"]
    # Schema work is outside recurring-path capture (its FILE policy is covered
    # by test_901_connection_policy.py).
    app.open_db().close()
    app.open_cache_db().close()
    app.open_conversations_db(attach_cache=False).close()
    return app


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _insert(conn, app, *, row_id, captured, weekly_reset, five_reset,
            weekly=20, five=30, account="unattributed", held=0):
    start, end = _iso(weekly_reset - 7 * 86400), _iso(weekly_reset)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(id, captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "week_end_at, weekly_percent, five_hour_percent, five_hour_resets_at, "
        "five_hour_window_key, source, payload_json, account_key, "
        "weekly_observation_held) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'statusline', '{}', ?, ?)",
        (row_id, _iso(captured), start[:10], end[:10], start, end, weekly, five,
         _iso(five_reset), app._canonical_5h_window_key(five_reset), account, held),
    )


def _projection_statements(recorder):
    return [s for s in recorder.statements
            if "_cctally_statusline._read_db_projection_once" in s.call_sites
            and s.sql.startswith("SELECT id,")]


def _assert_recurring_plans(recorder):
    # Call site AND literal SQL LIMIT prove these sorter bounds. No exception
    # allows a whole-history projection sort, even if its store is MEMORY.
    bounded = (
        guard.TempStructureRule(
            "statusline-latest-axis", "_cctally_statusline._build_statusline_injections.<locals>._db_latest_rate_limits",
            r"FROM weekly_usage_snapshots\b.* ORDER BY .* LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        # #825/#832 K16c: the one shared latest-five-hour-reading selection
        # (forecast and the current-week card reach it outside the
        # latest-axis reader above).
        guard.TempStructureRule(
            "latest-five-hour-reading", "_lib_five_hour.latest_five_hour_reading",
            r"FROM weekly_usage_snapshots\b.* ORDER BY captured_at_utc DESC, id DESC LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "weekly-credit-floor", "_cctally_core._reset_aware_floor",
            r"ORDER BY unixepoch\(floor_at\) DESC LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "forecast-reset-event", "_cctally_forecast._apply_midweek_reset_override",
            r"ORDER BY .* LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "record-five-hour-anchor", "_cctally_record._derive_5h_window_key",
            r"FROM (five_hour_blocks|weekly_usage_snapshots)\b.* ORDER BY .* LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "record-latest-axis", "_cctally_record.detect_reset_and_credit",
            r"FROM weekly_usage_snapshots\b.* ORDER BY captured_at_utc DESC, id DESC LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "usage-fold-reset", "_cctally_journal._usage_snapshot_fold_decision",
            r"SELECT MAX\(five_hour_percent\).* FROM five_hour_reset_events\b.* ORDER BY id DESC LIMIT 1\)",
            r"USE TEMP B-TREE FOR ORDER BY", "nested top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "latest-cost-snapshot", "_cctally_core._get_latest_row_for_week",
            r"FROM weekly_cost_snapshots\b.* ORDER BY .* LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
        guard.TempStructureRule(
            "active-five-hour-reset", "_cctally_record._resolve_active_five_hour_reset_event_id",
            r"FROM five_hour_reset_events\b.* ORDER BY id DESC LIMIT 1$",
            r"USE TEMP B-TREE FOR ORDER BY", "top-1: literal LIMIT 1"),
    )
    findings, _ = guard.classify(
        recorder.statements, allowlist=guard.ALLOWLIST + bounded, pending=())
    assert not findings, guard.format_findings(findings)
    # Judge the SQL owner, rather than an ancestor that also calls schema and
    # epoch probes. Those approved default-policy opens have separate coverage
    # in test_901_connection_policy.py and must not become ordinary writers.
    ordinary_owners = {
        "_cctally_statusline._build_statusline_injections.<locals>._db_latest_rate_limits",
        # #825/#832 K16c: the latest-rate-limits reader's five-hour leg runs
        # in the shared selection, which is therefore that SQL's owner.
        "_lib_five_hour.latest_five_hour_reading",
        "_cctally_statusline._read_db_projection_once",
        "_cctally_forecast._fetch_current_week_snapshots",
        "_cctally_cache.iter_entries",
        "_cctally_cache.sync_cache",
        "_cctally_record.maybe_update_five_hour_block",
        "_cctally_record.detect_reset_and_credit",
        "_cctally_record._derive_5h_window_key",
        "_cctally_journal._usage_snapshot_fold_decision",
    }
    ordinary = [s for s in recorder.statements
                if s.call_sites and s.call_sites[0] in ordinary_owners]
    assert ordinary, "non-vacuity: ordinary render/ingest SQL executed"
    assert {s.temp_store for s in ordinary} == {2}, "\n".join(
        f"temp_store={s.temp_store} owner={s.call_sites[0]} sql={s.sql}"
        for s in ordinary if s.temp_store != 2)


@pytest.mark.parametrize("temp_store", ["MEMORY", "FILE"])
def test_projection_select_plans_never_build_history_sized_temp_structures(
    app, monkeypatch, temp_store,
):
    now = int(time.time())
    conn = app.open_db()
    try:
        for row_id in range(1, 65):
            _insert(conn, app, row_id=row_id, captured=now - row_id,
                    weekly_reset=now + 3 * 86400, five_reset=now + 7200)
        conn.commit()
    finally:
        conn.close()
    store = importlib.import_module("_cctally_store")
    monkeypatch.setattr(store, "WRITER_TEMP_STORE", temp_store)
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        result = app._read_db_projection_once(account_key="unattributed")
    statements = _projection_statements(recorder)
    assert len(statements) == 2, "both quota-axis projection SELECTs executed"
    assert {s.temp_store for s in statements} == {
        2 if temp_store == "MEMORY" else 1}
    assert result.seven_day.percent == 20 and result.five_hour.percent == 30
    findings = list(guard.temp_findings(statements))
    assert not findings, guard.format_findings(findings)


def test_forecast_status_line_no_sync_keeps_ordinary_memory_policy(
    app, monkeypatch,
):
    now = int(time.time())
    conn = app.open_db()
    try:
        for row_id, hours_ago, weekly in ((1, 48, 5), (2, 24, 10), (3, 1, 20)):
            _insert(conn, app, row_id=row_id, captured=now - hours_ago * 3600,
                    weekly_reset=now + 3 * 86400, five_reset=now + 7200,
                    weekly=weekly)
        conn.commit()
    finally:
        conn.close()
    with guard.capture_sql_plans() as recorder:
        first = _command(app, monkeypatch,
                         ["forecast", "--status-line", "--no-sync"], {})
        second = _command(app, monkeypatch,
                          ["forecast", "--status-line", "--no-sync"], {})
    assert first == second and first.endswith("\n")
    assert "tracking" not in first.lower()
    assert not any("_cctally_cache.sync_cache" in s.call_sites
                   for s in recorder.statements), "--no-sync must skip ingestion"
    _assert_recurring_plans(recorder)


def test_caught_up_foreground_claude_hook_keeps_memory_policy(
    app, monkeypatch,
):
    monkeypatch.setattr(app, "_hook_tick_read_stdin_event", lambda: {
        "event": "Stop", "session_id": "caught-up-902", "cwd": "",
        "transcript_path": ""})
    argv = ["hook-tick", "--foreground", "--no-oauth", "--explain"]
    _command(app, monkeypatch, argv, {})
    with guard.capture_sql_plans() as recorder:
        output = _command(app, monkeypatch, argv, {})
    assert "ingested 0 new entries" in output
    assert any("_cctally_cache.sync_cache" in s.call_sites
               for s in recorder.statements), "caught-up Claude still visits cache"
    _assert_recurring_plans(recorder)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("active", ["account-a", "account-b"])
def test_projection_is_independent_of_insertion_and_scan_order(
    app, monkeypatch, reverse, active,
):
    accounts = importlib.import_module("_lib_accounts")
    other = accounts.account_key(
        "claude", "account-b" if active == "account-a" else "account-a")
    active = accounts.account_key("claude", active)
    sl = importlib.import_module("_cctally_statusline")
    monkeypatch.setattr(sl, "_statusline_active_account", lambda: active)
    now = int(time.time())
    reset, five_reset = now + 3 * 86400, now + 7200
    rows = [
        # Newer canonical window wins even over a later-captured older window.
        dict(row_id=1, captured=now - 1, weekly_reset=reset - 86400,
             five_reset=five_reset - 600, weekly=99, five=99),
        # Larger id cannot override a genuinely later capture.
        dict(row_id=20, captured=now - 30, weekly_reset=reset,
             five_reset=five_reset, weekly=88, five=88),
        dict(row_id=2, captured=now - 10, weekly_reset=reset,
             five_reset=five_reset, weekly=21, five=31),
        # Equal capture: explicit id wins, independent of physical scan order.
        dict(row_id=3, captured=now - 10, weekly_reset=reset,
             five_reset=five_reset, weekly=22, five=32),
        # Held weekly is excluded while its independently observed 5h survives.
        dict(row_id=4, captured=now - 5, weekly_reset=reset,
             five_reset=five_reset, weekly=98, five=33, held=1),
    ]
    conn = app.open_db()
    try:
        for row in reversed(rows) if reverse else rows:
            _insert(conn, app, account=active, **row)
        _insert(conn, app, row_id=100, captured=now, weekly_reset=reset + 86400,
                five_reset=five_reset + 600, weekly=97, five=97,
                account=other)
        conn.commit()
    finally:
        conn.close()
    real_open = sl.open_db

    def open_in_scan_order():
        conn = real_open()
        conn.execute(f"PRAGMA reverse_unordered_selects = {int(reverse)}")
        return conn

    monkeypatch.setattr(sl, "open_db", open_in_scan_order)
    projection = app._read_db_projection_once(account_key=active)
    assert projection.seven_day.percent == 22
    assert projection.seven_day.captured_at == now - 10
    assert projection.five_hour.percent == 33
    assert projection.five_hour.captured_at == now - 5


@pytest.mark.parametrize("reverse", [False, True])
def test_projection_preserves_weekly_credit_and_five_hour_reset_floors(
    app, monkeypatch, reverse,
):
    now = int(time.time())
    reset, five_reset, floor = now + 3 * 86400, now + 7200, now - 10
    conn = app.open_db()
    try:
        # The boundary itself is eligible; the higher pre-credit row is not.
        for row_id, captured, weekly, five in (
            (1, floor, 20, 5), (2, floor - 1, 90, 80),
        ):
            _insert(conn, app, row_id=row_id, captured=captured,
                    weekly_reset=reset, five_reset=five_reset,
                    weekly=weekly, five=five)
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc, "
            "observed_pre_credit_pct, applied_at_utc) VALUES (?, ?, 90, ?)",
            (_iso(reset - 7 * 86400)[:10], _iso(floor), _iso(floor)),
        )
        event_id = conn.execute(
            "INSERT INTO five_hour_reset_events (detected_at_utc, "
            "five_hour_window_key, prior_percent, post_percent, "
            "effective_reset_at_utc) VALUES (?, ?, 80, 5, ?)",
            (_iso(floor), app._canonical_5h_window_key(five_reset), _iso(floor)),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    sl = importlib.import_module("_cctally_statusline")
    real_open = sl.open_db

    def open_in_scan_order():
        conn = real_open()
        conn.execute(f"PRAGMA reverse_unordered_selects = {int(reverse)}")
        return conn

    monkeypatch.setattr(sl, "open_db", open_in_scan_order)
    projection = app._read_db_projection_once(account_key="unattributed")
    assert projection.seven_day.percent == 20
    assert projection.seven_day.reset_generation == floor
    assert projection.five_hour.percent == 5
    # #832: the generation is LOGICAL (effective|detected|journal identity),
    # never the rowid.
    assert event_id > 0
    assert projection.five_hour.reset_generation.startswith(_iso(floor) + "|")


def _command(app, monkeypatch, argv, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(
        io.BytesIO(json.dumps(payload).encode())))
    args = app.build_parser().parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert args.func(args) == 0
    return out.getvalue()


def test_real_statusline_payload_executes_both_projection_axes(
    app, monkeypatch,
):
    now = int(time.time())
    sl = importlib.import_module("_cctally_statusline")
    real_persist = sl._statusline_persist
    # Keep the real parser, feeder and reducer; only make child work observable
    # in this process with the existing synchronous persistence test hook.
    monkeypatch.setattr(sl, "_statusline_persist",
                        lambda parsed: real_persist(parsed, sync_for_test=True))
    monkeypatch.setattr(app, "_statusline_oauth_tick", lambda: None)
    monkeypatch.setattr(app, "_nudge_dashboard_repaint", lambda: None)
    payload = {"session_id": "projection-902", "rate_limits": {
        "seven_day": {"used_percentage": 20, "resets_at": _iso(now + 3 * 86400)},
        "five_hour": {"used_percentage": 30, "resets_at": _iso(now + 7200)},
    }}
    with guard.capture_sql_plans() as recorder:
        output = _command(app, monkeypatch, ["statusline", "--no-color"], payload)
    assert output.endswith("\n") and "20%" in output and "30%" in output
    statements = _projection_statements(recorder)
    assert len(statements) >= 2
    assert any("weekly_percent" in s.sql for s in statements)
    assert any("five_hour_percent" in s.sql for s in statements)
    assert {s.temp_store for s in statements} == {2}
    closures = [s for s in recorder.statements
                if "_cctally_record.maybe_update_five_hour_block" in s.call_sites
                and s.sql.startswith("SELECT id FROM five_hour_blocks")]
    assert any("is_closed = 0" in s.sql for s in closures)
    assert any("is_closed = 1" in s.sql for s in closures)
    findings = list(guard.temp_findings(statements))
    assert not findings, guard.format_findings(findings)
    _assert_recurring_plans(recorder)


def test_f2_render_path_does_no_journal_cutover_or_index_work(app, monkeypatch):
    """#825/#832 case F2 (C4): a full CLI statusline render captures and spools
    one bound candidate and does nothing else new — it opens no journal file,
    appends nothing, never establishes the account-bound cutover and runs no
    SQL from the journal/ingest machinery. The detached reducer (stubbed here,
    exactly where the real render forks it) owns all of that."""
    import builtins
    import os

    now = int(time.time())
    core = importlib.import_module("_cctally_core")
    journal = importlib.import_module("_cctally_journal")
    sl = importlib.import_module("_cctally_statusline")
    core.CLAUDE_JSON_PATH.write_text(json.dumps({"oauthAccount": {
        "accountUuid": "render-A", "emailAddress": "a@example.test"}}))
    core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    forks = []
    monkeypatch.setattr(sl, "_fork_persist", lambda fd, reservation: (
        forks.append(fd), sl._release_reservation(reservation)))
    monkeypatch.setattr(app, "_statusline_oauth_tick", lambda: None)
    forbidden = []
    for name in ("ensure_account_bound_cutover", "find_account_bound_cutover",
                 "append_record", "append_records", "run_stats_ingest",
                 "append_legacy_spool_activity", "capture_legacy_spool_state"):
        monkeypatch.setattr(journal, name, lambda *a, _n=name, **k: (
            forbidden.append(_n), (_ for _ in ()).throw(AssertionError(_n)))[1])
    journal_dir = str(core.JOURNAL_DIR)
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def tracking_open(file, *args, **kwargs):
        if str(file).startswith(journal_dir):
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def tracking_os_open(path, *args, **kwargs):
        if str(path).startswith(journal_dir):
            opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(os, "open", tracking_os_open)
    payload = {"session_id": "render-f2", "rate_limits": {
        "seven_day": {"used_percentage": 20, "resets_at": _iso(now + 3 * 86400)},
        "five_hour": {"used_percentage": 30, "resets_at": _iso(now + 7200)},
    }}
    with guard.capture_sql_plans() as recorder:
        output = _command(app, monkeypatch, ["statusline", "--no-color"], payload)
    assert output.endswith("\n")
    assert forks, "the render must hand the bound candidate to the detached reducer"
    assert forbidden == []
    assert opened == []
    assert not core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.exists()
    assert not list(core.JOURNAL_DIR.glob("*.jsonl"))
    for statement in recorder.statements:
        assert not any(site.startswith(("_cctally_journal.", "_cctally_store."))
                       for site in statement.call_sites), statement
    candidates = list((core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts").glob(
        "*/candidates/*.json"))
    assert len(candidates) == 1
    assert not core.STATUSLINE_CANDIDATE_DIR.exists()
