"""Firing tests for the equiv-$ budget alert path (Task 3, spec §5).

Exercises ``maybe_record_budget_milestone`` (record-usage Approach A) and
``_reconcile_budget_milestones_on_set`` (forward-only-from-set) against a
redirected tmp stats.db + a seeded ``weekly_usage_snapshots`` window anchor.

Spend is injected via a monkeypatched ``_sum_cost_for_range`` so the
crossing arithmetic is deterministic and isolated from the cache-DB ingest
path (that path's correctness is locked by Task 2's F3 reconcile invariant
in ``bin/cctally-reconcile-test``). Dispatch is captured via a fake
``_dispatch_alert_notification`` so no osascript is spawned.

Covered cases:
  (a) crossing 90 then 100 inserts two rows with alerted_at set + dispatches;
  (b) re-running does NOT re-insert / re-dispatch (fire-once via rowcount);
  (c) forward-only reconcile at 95% records 90 with alerted_at set but does
      NOT dispatch (no popup); a later record-usage at 100% fires ONLY 100;
  (d) _budget_alerts_active False → no rows, no SUM, no dispatch;
  (e) NON-VACUITY of the pre-probe skip: when all thresholds already have
      rows, _sum_cost_for_range is NOT called.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import threading

import pytest

from conftest import load_script, redirect_paths


# Subscription-week window the snapshot anchors. Tuesday 14:00 UTC, 7 days.
WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)
# now_utc placed mid-week (~4 days in) so elapsed/remaining are well-defined.
AS_OF = WEEK_START + dt.timedelta(hours=96)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_week_key():
    """The exact ``week_start_at`` key the production code writes.

    ``_resolve_current_budget_window`` runs the seeded ISO timestamp through
    ``parse_iso_datetime`` (which returns a HOST-LOCAL datetime) and then
    ``isoformat(timespec="seconds")`` — so the stored key carries the host's
    UTC offset. Mirror that derivation here so the test is host-TZ-agnostic
    (and so dedup keying is asserted against the SAME string production uses
    on this machine).
    """
    return dt.datetime.fromisoformat(
        _iso(WEEK_START).replace("Z", "+00:00")
    ).astimezone().isoformat(timespec="seconds")


WEEK_KEY = _expected_week_key()


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Pin _command_as_of deterministically via the documented env hook.
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF))
    return ns


def _seed_window(ns):
    """Seed one boundary-aware weekly_usage_snapshots row so
    ``_resolve_current_budget_window`` resolves the [WEEK_START, WEEK_END)
    window. The percent is irrelevant to budget spend."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " page_url, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(WEEK_START + dt.timedelta(hours=1)),
                WEEK_START.date().isoformat(),
                (WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
                _iso(WEEK_START),
                _iso(WEEK_END),
                40.0,
                None,
                "fixture",
                json.dumps({"fixture": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_budget_config(ns, *, weekly_usd, alerts_enabled=True,
                         thresholds=(90, 100)):
    """Write a config.json carrying the budget block at the redirected
    CONFIG_PATH so ``load_config`` reads it."""
    import _cctally_core
    block = {"alerts_enabled": alerts_enabled,
             "alert_thresholds": list(thresholds)}
    if weekly_usd is not None:
        block["weekly_usd"] = weekly_usd
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": block}) + "\n"
    )


def _patch_spend(ns, monkeypatch, *, value=None, spy=None):
    """Inject a deterministic ``_sum_cost_for_range`` on the cctally
    namespace (resolved at call time by the record-sibling shim). ``spy`` is
    an optional list that records each call's args (non-vacuity proof)."""
    def fake_sum(start, end, mode="auto", project=None, *, skip_sync=False):
        if spy is not None:
            spy.append((start, end, mode))
        return value
    monkeypatch.setitem(ns, "_sum_cost_for_range", fake_sum)


def _patch_dispatch(ns, monkeypatch):
    """Capture dispatched payloads instead of spawning osascript."""
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append((payload, mode))
        return "queued"
    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake_dispatch)
    return captured


def _milestone_rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT week_start_at, threshold, budget_usd, spent_usd, "
            "       consumption_pct, alerted_at "
            "FROM budget_milestones ORDER BY threshold"
        ).fetchall()
    finally:
        conn.close()


# ── (a) crossing 90 then 100 inserts two rows + dispatches both ──────────


def test_crossing_records_rows_and_dispatches(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))
    # $300 spent on a $300 budget → 100% consumption → crosses 90 AND 100.
    _patch_spend(ns, monkeypatch, value=300.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    # Every recorded row carries a set alerted_at (set-then-dispatch).
    assert all(r["alerted_at"] is not None for r in rows)
    assert all(r["week_start_at"] == WEEK_KEY for r in rows)
    assert all(abs(r["budget_usd"] - 300.0) < 1e-9 for r in rows)
    assert all(abs(r["spent_usd"] - 300.0) < 1e-9 for r in rows)
    # Both crossings dispatched, mode=real, axis=budget.
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["axis"] == "budget" for p, _ in captured)
    assert all(mode == "real" for _, mode in captured)


# ── (b) fire-once: a second run inserts/dispatches nothing ───────────────


def test_fire_once_no_reinsert_no_redispatch(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, value=300.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})
    first = len(captured)
    assert first == 2
    rows_after_first = _milestone_rows(ns)

    # Second tick at the same (or higher) spend: rowcount==0 on both, so no
    # dispatch and no new rows.
    ns["maybe_record_budget_milestone"]({})
    assert len(captured) == first  # no re-dispatch
    assert len(_milestone_rows(ns)) == len(rows_after_first) == 2


# ── (c) forward-only-from-set reconcile records-without-dispatch ─────────


def test_reconcile_on_set_records_without_dispatch_then_later_fires(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))
    captured = _patch_dispatch(ns, monkeypatch)

    # `budget set` reconcile at 95% spend ($285): 90 already crossed → record
    # with alerted_at SET but NO dispatch; 100 not crossed → no row.
    _patch_spend(ns, monkeypatch, value=285.0)
    now_utc = ns["_command_as_of"]()
    conn = ns["open_db"]()
    try:
        ns["_reconcile_budget_milestones_on_set"](
            conn, target=300.0, thresholds=(90, 100), now_utc=now_utc,
        )
    finally:
        conn.close()

    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90]
    assert rows[0]["alerted_at"] is not None  # recorded
    assert captured == []  # but NOT dispatched (no instant popup)

    # Later record-usage tick at 100% spend: 90 already a row (skip), 100 is
    # pending → fires ONLY 100.
    _patch_spend(ns, monkeypatch, value=300.0)
    ns["maybe_record_budget_milestone"]({})

    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [100]


# ── (d) gating: alerts off / no budget → no rows, no SUM, no dispatch ────


def test_alerts_disabled_does_nothing(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, alerts_enabled=False)
    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    assert _milestone_rows(ns) == []
    assert captured == []
    assert spy == []  # gate returns BEFORE the SUM (zero overhead)


def test_no_budget_does_nothing(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=None)  # no weekly_usd → no budget
    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    assert _milestone_rows(ns) == []
    assert captured == []
    assert spy == []


# ── (e) NON-VACUITY: pre-probe skips the SUM iff ALL thresholds recorded ──


def test_preprobe_skips_sum_when_all_recorded(ns, monkeypatch):
    """When every configured threshold already has a row, the pre-probe
    early-returns BEFORE _sum_cost_for_range — proven by a spy that records
    zero calls. Crucially: it skips ONLY because nothing is owed."""
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))

    # First run at 100% records both rows (one SUM call).
    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    _patch_dispatch(ns, monkeypatch)
    ns["maybe_record_budget_milestone"]({})
    assert len(spy) == 1  # SUM ran once (work was owed)
    assert len(_milestone_rows(ns)) == 2

    # Second run: all thresholds present → pre-probe short-circuits, NO SUM.
    spy.clear()
    ns["maybe_record_budget_milestone"]({})
    assert spy == []  # the SUM was skipped (non-vacuous optimization)


def test_preprobe_does_not_skip_when_one_threshold_pending(ns, monkeypatch):
    """Counterpart to the above: a partial prior run (only 90 recorded) must
    STILL run the SUM so 100 can later cross — the skip never owes a
    crossing ([Dedup mustn't gate side effects])."""
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))

    # Seed ONLY the 90 row (simulate a partial prior run / forward-only set).
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO budget_milestones "
            "(week_start_at, threshold, budget_usd, spent_usd, "
            " consumption_pct, crossed_at_utc, alerted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (WEEK_KEY, 90, 300.0, 270.0, 90.0, _iso(AS_OF), _iso(AS_OF)),
        )
        conn.commit()
    finally:
        conn.close()

    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    # 100 was pending → SUM ran, 100 recorded + dispatched; 90 untouched.
    assert len(spy) == 1
    assert [r["threshold"] for r in _milestone_rows(ns)] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [100]


# ── snap-up: a 89.9999999% consumption counts as crossing 90 ─────────────


def test_snap_up_crosses_threshold(ns, monkeypatch):
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90,))
    # 269.9999999999 / 300 * 100 == 89.99999... — +1e-9 must snap it to >= 90.
    _patch_spend(ns, monkeypatch, value=269.9999999999)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    assert [r["threshold"] for r in _milestone_rows(ns)] == [90]
    assert [p["threshold"] for p, _ in captured] == [90]


# ── malformed budget config is a quiet warn-once no-op (hot-path safety) ──


def test_malformed_budget_config_is_quiet_noop(ns, monkeypatch, capsys):
    """A hand-edited invalid budget block must NOT crash record-usage nor spam
    stderr every tick: maybe_record_budget_milestone warns once at the config
    gate and no-ops (no rows, no SUM, no dispatch, no raise)."""
    _seed_window(ns)  # creates the schema (incl. budget_milestones)
    import _cctally_core
    # weekly_usd <= 0 fails _get_budget_config -> _BudgetConfigError.
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {"weekly_usd": -5.0,
                               "alert_thresholds": [90, 100]}}) + "\n"
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})  # must not raise

    assert _milestone_rows(ns) == []
    assert captured == []
    assert spy == []  # returned at the config gate, before the SUM
    assert "[budget] invalid config" in capsys.readouterr().err


# ── reconcile is idempotent across a mid-week re-run (no dup / no re-stamp) ──


def test_reconcile_idempotent_on_rerun(ns, monkeypatch):
    """A mid-week target change re-runs the reconcile; UNIQUE(week_start_at,
    threshold) + the `alerted_at IS NULL` UPDATE guard keep it idempotent —
    no duplicate row, no re-stamp, never a dispatch."""
    _seed_window(ns)
    _write_budget_config(ns, weekly_usd=300.0, thresholds=(90, 100))
    captured = _patch_dispatch(ns, monkeypatch)
    _patch_spend(ns, monkeypatch, value=285.0)  # 95% → 90 crossed, 100 not
    now_utc = ns["_command_as_of"]()

    def _reconcile():
        conn = ns["open_db"]()
        try:
            ns["_reconcile_budget_milestones_on_set"](
                conn, target=300.0, thresholds=(90, 100), now_utc=now_utc,
            )
        finally:
            conn.close()

    _reconcile()
    rows_first = _milestone_rows(ns)
    assert [r["threshold"] for r in rows_first] == [90]
    stamp_first = rows_first[0]["alerted_at"]

    _reconcile()  # second run (simulates a mid-week target change)
    rows_second = _milestone_rows(ns)
    assert [r["threshold"] for r in rows_second] == [90]  # no duplicate
    assert rows_second[0]["alerted_at"] == stamp_first  # not re-stamped
    assert captured == []  # reconcile never dispatches


# ── Fix #1: the TWO non-canonical write paths also reconcile forward-only ──
#
# `_reconcile_budget_milestones_on_set` used to run ONLY from `budget set`.
# `config set budget.*` and the dashboard POST /api/settings budget write
# persisted config but skipped the reconcile, so enabling/raising a budget
# while already past a threshold dispatched RETROACTIVE alerts on the next
# record-usage tick. Both paths now route through the shared helper
# `_reconcile_budget_on_config_write`. The smoking-gun assertion mirrors the
# `budget set` reconcile test (case c): the write records the already-crossed
# threshold with alerted_at SET but WITHOUT dispatch, so a subsequent
# `maybe_record_budget_milestone` tick does NOT re-fire it.


def test_config_set_path_reconciles_forward_only(ns, monkeypatch):
    """`config set budget.weekly_usd 300` while already at 95% spend records
    the crossed 90 threshold as already-alerted (no dispatch); the later
    record-usage tick at 100% then fires ONLY 100."""
    _seed_window(ns)
    # No budget block on disk yet — the `config set` write installs weekly_usd
    # 300 and the read-time defaults fill alerts_enabled True + the default
    # thresholds. Pin thresholds explicitly so the crossing math is stable.
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {"alert_thresholds": [90, 100]}}) + "\n"
    )
    # 95% spend ($285 on $300): 90 already crossed, 100 not yet.
    _patch_spend(ns, monkeypatch, value=285.0)
    captured = _patch_dispatch(ns, monkeypatch)

    args = argparse.Namespace(
        key="budget.weekly_usd", value="300", emit_json=False,
    )
    rc = ns["_cmd_config_set"](args)
    assert rc == 0

    # Forward-only reconcile recorded 90 with alerted_at SET, NO dispatch.
    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90]
    assert rows[0]["alerted_at"] is not None
    assert captured == []  # the config set did NOT instant-popup

    # A later record-usage tick at 100% fires ONLY the still-pending 100 —
    # the reconciled 90 is deduped, NOT re-dispatched retroactively.
    _patch_spend(ns, monkeypatch, value=300.0)
    ns["maybe_record_budget_milestone"]({})
    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [100]


# ---- dashboard POST /api/settings budget-write reconcile ------------------


def _wire_dashboard_handlers(ns):
    """Minimal handler wiring (mirrors tests/test_config_budget_settings.py)
    so the POST /api/settings handler can run in a server thread."""
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].no_sync = False
    ns["DashboardHTTPHandler"].display_tz_pref_override = None


def _post_json(host, port, path, body):
    """POST a JSON body with matched Host + Origin (loopback CSRF contract)."""
    c = http.client.HTTPConnection(host, port, timeout=2)
    raw = json.dumps(body).encode()
    host_header = f"{host}:{port}"
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", host_header)
    c.putheader("Origin", f"http://{host_header}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    payload = r.read().decode("utf-8", errors="replace")
    parsed = json.loads(payload) if payload else None
    return r.status, parsed


def test_dashboard_post_settings_reconciles_forward_only(ns, monkeypatch):
    """`POST /api/settings {"budget": {weekly_usd:300, thresholds:[90,100]}}`
    while already at 95% spend records the crossed 90 threshold as
    already-alerted (no dispatch); the later record-usage tick fires only 100.
    """
    _seed_window(ns)
    # 95% spend ($285 on $300): 90 already crossed, 100 not yet. The reconcile
    # helper resolves _sum_cost_for_range via sys.modules["cctally"] at call
    # time, so this monkeypatch is visible in the server thread.
    _patch_spend(ns, monkeypatch, value=285.0)
    captured = _patch_dispatch(ns, monkeypatch)

    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"weekly_usd": 300, "alert_thresholds": [90, 100]}},
        )
        assert status == 200, body
        assert body["budget"]["weekly_usd"] == 300.0
    finally:
        srv.shutdown()

    # Reconcile recorded 90 with alerted_at SET, NO dispatch (the 200 response
    # must not have triggered a retroactive popup).
    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90]
    assert rows[0]["alerted_at"] is not None
    assert captured == []

    # Later record-usage tick at 100% fires ONLY the pending 100.
    _patch_spend(ns, monkeypatch, value=300.0)
    ns["maybe_record_budget_milestone"]({})
    rows = _milestone_rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [100]


# ── Fix #2: recent-rate window clamps to the budget week (no last-week leak) ──
#
# `recent_24h_usd` is NOT display-only — it feeds rate_recent → rate_high →
# projected_high → projected → the ok/warn/over verdict. `recent_start` is now
# clamped at the week start (max(week_start, now-24h)), so a heavy spend just
# before reset can't leak last week's dollars into a brand-new week's verdict.
#
# Smoking-gun: a fresh week (now = week_start + 3h) with a tiny this-week spend
# but a heavy burn in the trailing 24h that PRECEDES the reset. Without the
# clamp the trailing-24h window reaches ~21h back into last week, pulling a
# huge rate_recent that projects WAY over budget → false "over". With the clamp
# recent_24h only sees this-week spend → verdict "ok".


def test_recent_rate_clamped_to_week_no_last_week_leak(ns, monkeypatch):
    # Fresh week, 3h in.
    now_utc = WEEK_START + dt.timedelta(hours=3)
    _seed_window(ns)

    # Window-aware spend: any range whose START precedes the week boundary is
    # last week's pre-reset burn ($300 of heavy spend). Ranges that start AT
    # the week boundary (both the `spent` call and the CLAMPED recent call)
    # see only this week's tiny $2. The unclamped (buggy) recent call would
    # start at now-24h < week_start and so pick up the $300.
    def fake_sum(start, end, mode="auto", project=None, *, skip_sync=False):
        if start < WEEK_START:
            return 300.0  # last week's pre-reset burn — must NOT leak in
        return 2.0        # this week so far
    monkeypatch.setitem(ns, "_sum_cost_for_range", fake_sum)

    conn = ns["open_db"]()
    try:
        inputs = ns["_build_budget_status_inputs"](
            conn, target_usd=300.0, now_utc=now_utc, alert_thresholds=(90, 100),
        )
    finally:
        conn.close()

    assert inputs is not None
    # The clamp pinned recent_24h to this-week spend ($2), NOT last week's $300.
    assert abs(inputs.recent_24h_usd - 2.0) < 1e-9
    assert abs(inputs.spent_usd - 2.0) < 1e-9

    status = ns["compute_budget_status"](inputs)
    # Without the clamp this would project ~$2k EOW → "over". Clamped: "ok".
    assert status.verdict == "ok"
