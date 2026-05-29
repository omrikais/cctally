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

import datetime as dt
import json

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
