"""Firing + reconcile tests for the Codex budget alert path (Task 3, spec §6).

Exercises ``maybe_record_codex_budget_milestone`` (record-usage firing /
opportunistic ``cctally budget`` firing share the same helper) and
``_reconcile_budget_milestones_on_set(vendor="codex", …)`` (forward-only-from-set)
against a redirected tmp stats.db. Codex rows now live in the unified
vendor-tagged ``budget_milestones`` table (``vendor='codex'``, #143).

Unlike the Claude budget axis, the Codex axis has NO subscription week: the
period window is derived purely from ``now`` + the configured calendar period
(``calendar-month`` here → ``period_start_at`` = the 1st of the civil month),
so NO ``weekly_usage_snapshots`` anchor is seeded. Codex spend is injected via a
monkeypatched ``_sum_codex_cost_for_range`` so the crossing arithmetic is
deterministic and isolated from the cache-DB ingest path (that path's
correctness is locked by Task 2's reconcile invariant). Dispatch is captured via
a fake ``_dispatch_alert_notification`` so no osascript is spawned — exactly the
seam ``tests/test_budget_alerts.py`` uses.

Covered cases (mirror the global budget firing tests, keyed by period instead
of subscription week):
  (a) forward-only-from-set — a mid-period `set` while already over latches
      the crossed thresholds with alerted_at SET but does NOT dispatch;
  (b) a genuine new crossing returns rowcount==1 and fires once; re-running
      does NOT re-fire (rowcount==0);
  (c) period rollover (period_start_at changes) re-arms — fresh crossings fire;
  (d) gating: alerts off / no Codex budget → 0 rows, no SUM, no dispatch.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

from conftest import load_script, redirect_paths


# Calendar-month period the firing resolves from `now`. June 2026, mid-month.
# Use a UTC display tz so period_start_at = 2026-06-01T00:00:00+00:00
# deterministically (independent of the host zone). The stored key is the
# `isoformat(timespec="seconds")` `+00:00` offset form, NOT a `Z` suffix.
AS_OF = dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
# After rollover to July (case c).
AS_OF_JULY = dt.datetime(2026, 7, 3, 9, 0, 0, tzinfo=dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _period_key(month_start_utc: dt.datetime) -> str:
    """The exact period_start_at key the production code writes: the resolved
    calendar-month window start, isoformat(timespec='seconds'). With display.tz
    pinned to UTC the civil month start is the UTC instant 1st 00:00, stored as
    the `+00:00` offset form (e.g. `2026-06-01T00:00:00+00:00`), NOT a `Z`
    suffix."""
    return month_start_utc.isoformat(timespec="seconds")


JUNE_START = dt.datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
JULY_START = dt.datetime(2026, 7, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
JUNE_KEY = _period_key(JUNE_START)
JULY_KEY = _period_key(JULY_START)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Pin _command_as_of deterministically via the documented env hook.
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF))
    return ns


def _ensure_schema(ns):
    """Open + close the DB once so the schema (incl. the unified
    vendor-tagged budget_milestones table, #143) is created before the firing
    helper runs."""
    ns["open_db"]().close()


def _write_codex_config(ns, *, amount_usd=200.0, period="calendar-month",
                        alerts_enabled=True, thresholds=(90, 100)):
    """Write a config.json carrying budget.codex + display.tz=utc so the period
    window is host-zone-agnostic. ``load_config`` reads it at the redirected
    CONFIG_PATH."""
    import _cctally_core
    codex_block = {
        "amount_usd": amount_usd,
        "period": period,
        "alerts_enabled": alerts_enabled,
        "alert_thresholds": list(thresholds),
    }
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({
            "display": {"tz": "utc"},
            "budget": {"codex": codex_block},
        }) + "\n"
    )


def _patch_codex_spend(ns, monkeypatch, *, value=None, spy=None):
    """Inject a deterministic ``_sum_codex_cost_for_range`` on the cctally
    namespace (resolved at call time by the record-sibling shim). ``spy`` records
    each call's args (non-vacuity proof)."""
    def fake_sum(start, end, *, speed="auto"):
        if spy is not None:
            spy.append((start, end, speed))
        return value
    monkeypatch.setitem(ns, "_sum_codex_cost_for_range", fake_sum)


def _patch_dispatch(ns, monkeypatch):
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append((payload, mode))
        return "queued"
    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake_dispatch)
    return captured


def _rows(ns):
    conn = ns["open_db"]()
    try:
        # Unified vendor-tagged table (#143): filter to the Codex axis so a
        # stray Claude row never pollutes the Codex firing assertions.
        return conn.execute(
            "SELECT vendor, period_start_at, threshold, budget_usd, spent_usd, "
            "       consumption_pct, alerted_at "
            "FROM budget_milestones WHERE vendor = 'codex' "
            "ORDER BY period_start_at, threshold"
        ).fetchall()
    finally:
        conn.close()


# ── (a) forward-only-from-set: mid-period set when already over ───────────────


def test_reconcile_on_set_records_without_dispatch_then_later_fires(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90, 100))
    captured = _patch_dispatch(ns, monkeypatch)

    # `budget set --vendor codex` reconcile at 95% spend ($190): 90 already
    # crossed → record with alerted_at SET but NO dispatch; 100 not crossed.
    _patch_codex_spend(ns, monkeypatch, value=190.0)
    now_utc = ns["_command_as_of"]()
    conn = ns["open_db"]()
    try:
        import argparse
        config = ns["load_config"]()
        tz = ns["resolve_display_tz"](argparse.Namespace(tz=None), config)
        ns["_reconcile_budget_milestones_on_set"](
            conn,
            vendor="codex",
            target=200.0,
            thresholds=(90, 100),
            now_utc=now_utc,
            period="calendar-month",
            config=config,
            tz=tz,
        )
    finally:
        conn.close()

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90]
    assert rows[0]["period_start_at"] == JUNE_KEY
    assert rows[0]["vendor"] == "codex"
    assert rows[0]["alerted_at"] is not None  # recorded
    assert captured == []  # but NOT dispatched (no instant popup)

    # Later tick at 100% spend ($200): 90 already a row (skip), 100 pending →
    # fires ONLY 100.
    _patch_codex_spend(ns, monkeypatch, value=200.0)
    assert ns["maybe_record_codex_budget_milestone"]({}) == 1

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [100]
    assert all(p["axis"] == "codex_budget" for p, _ in captured)


# ── (b) genuine new crossing fires once; re-run does not re-fire ──────────────


def test_crossing_fires_once_no_refire(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90, 100))
    # $200 spent on a $200 budget → 100% → crosses 90 AND 100.
    _patch_codex_spend(ns, monkeypatch, value=200.0)
    captured = _patch_dispatch(ns, monkeypatch)

    assert ns["maybe_record_codex_budget_milestone"]({}) == 2

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(r["alerted_at"] is not None for r in rows)
    assert all(r["period_start_at"] == JUNE_KEY for r in rows)
    # Each fired row carries the Codex vendor tag (#143 unified table).
    assert all(r["vendor"] == "codex" for r in rows)
    assert all(abs(r["budget_usd"] - 200.0) < 1e-9 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["axis"] == "codex_budget" for p, _ in captured)
    assert all(mode == "real" for _, mode in captured)
    assert all(
        (p["context"].get("period") == "calendar-month") for p, _ in captured
    )
    assert all(
        p["context"].get("period_start_at") == JUNE_KEY for p, _ in captured
    )

    # Second tick at the same spend: rowcount==0 on both → no re-dispatch, no
    # new rows.
    first = len(captured)
    assert first == 2
    assert ns["maybe_record_codex_budget_milestone"]({}) == 0
    assert len(captured) == first
    assert len(_rows(ns)) == 2


def test_budget_core_is_resilient_by_default_but_strict_for_lifecycle(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90,))

    def fail_sum(*_args, **_kwargs):
        raise RuntimeError("budget boom")

    monkeypatch.setitem(ns, "_sum_codex_cost_for_range", fail_sum)
    assert ns["maybe_record_codex_budget_milestone"]({}) == 0
    with pytest.raises(RuntimeError, match="budget boom"):
        ns["maybe_record_codex_budget_milestone"]({}, raise_errors=True)


# ── (c) period rollover re-arms ──────────────────────────────────────────────


def test_period_rollover_rearms(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90, 100))
    _patch_codex_spend(ns, monkeypatch, value=200.0)
    captured = _patch_dispatch(ns, monkeypatch)

    # June: fire both thresholds.
    ns["maybe_record_codex_budget_milestone"]({})
    assert [p["threshold"] for p, _ in captured] == [90, 100]
    june_rows = _rows(ns)
    assert all(r["period_start_at"] == JUNE_KEY for r in june_rows)

    # Roll over to July: the period_start_at changes → fresh crossings fire,
    # the June rows stay deduped (different period key).
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF_JULY))
    captured.clear()
    ns["maybe_record_codex_budget_milestone"]({})

    rows = _rows(ns)
    july_rows = [r for r in rows if r["period_start_at"] == JULY_KEY]
    assert [r["threshold"] for r in july_rows] == [90, 100]
    assert [p["threshold"] for p, _ in captured] == [90, 100]
    assert all(
        p["context"].get("period_start_at") == JULY_KEY for p, _ in captured
    )
    # Both periods now have their own rows (4 total).
    assert len(rows) == 4


# ── (d) gating: alerts off / no Codex budget → 0 rows, no SUM, no dispatch ────


def test_alerts_disabled_does_nothing(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, alerts_enabled=False)
    spy: list = []
    _patch_codex_spend(ns, monkeypatch, value=200.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_codex_budget_milestone"]({})

    assert _rows(ns) == []
    assert captured == []
    assert spy == []  # gate returns BEFORE the SUM (zero overhead)


def test_no_codex_budget_does_nothing(ns, monkeypatch):
    _ensure_schema(ns)
    # No budget.codex block at all.
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"display": {"tz": "utc"}, "budget": {}}) + "\n"
    )
    spy: list = []
    _patch_codex_spend(ns, monkeypatch, value=200.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_codex_budget_milestone"]({})

    assert _rows(ns) == []
    assert captured == []
    assert spy == []


# ── pre-probe non-vacuity: all thresholds recorded → no SUM ───────────────────


def test_preprobe_skips_sum_when_all_recorded(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90, 100))
    spy: list = []
    _patch_codex_spend(ns, monkeypatch, value=200.0, spy=spy)
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_codex_budget_milestone"]({})
    assert len(spy) == 1  # SUM ran once (work owed)
    assert len(_rows(ns)) == 2

    spy.clear()
    ns["maybe_record_codex_budget_milestone"]({})
    assert spy == []  # SUM skipped (non-vacuous optimization)


# ── snap-up: 89.9999999% counts as crossing 90 ───────────────────────────────


def test_snap_up_crosses_threshold(ns, monkeypatch):
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90,))
    # 179.9999999999 / 200 * 100 == 89.99999... — +1e-9 must snap it to >= 90.
    _patch_codex_spend(ns, monkeypatch, value=179.9999999999)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_codex_budget_milestone"]({})

    assert [r["threshold"] for r in _rows(ns)] == [90]
    assert [p["threshold"] for p, _ in captured] == [90]


# ── (#137) period column: collision-free key + NULL-row no-re-fire ────────


def test_calendar_week_to_month_switch_no_collision(ns):
    """Symptom 2 (Codex): a calendar-week crossing at instant X must not block a
    later calendar-month crossing at the SAME instant X — period now
    discriminates the UNIQUE key, so the second INSERT returns rowcount 1."""
    _ensure_schema(ns)
    X = JUNE_KEY
    conn = ns["open_db"]()
    try:
        assert ns["insert_budget_milestone"](
            conn, vendor="codex", period_start_at=X, period="calendar-week",
            threshold=90, budget_usd=100.0, spent_usd=92.0,
            consumption_pct=92.0, commit=True,
        ) == 1
        assert ns["insert_budget_milestone"](
            conn, vendor="codex", period_start_at=X, period="calendar-month",
            threshold=90, budget_usd=200.0, spent_usd=190.0,
            consumption_pct=95.0, commit=True,
        ) == 1
        periods = [
            r[0] for r in conn.execute(
                "SELECT period FROM budget_milestones "
                "WHERE vendor='codex' AND period_start_at=? AND threshold=90 "
                "ORDER BY period", (X,)
            )
        ]
        assert periods == ["calendar-month", "calendar-week"]
    finally:
        conn.close()


def test_null_period_row_does_not_refire(ns, monkeypatch):
    """P1-1 (Codex): a pre-011 NULL-period crossing for the CURRENT period must
    be read as present by the firing pre-probe (period=? OR period IS NULL) — no
    spurious upgrade alert, no second row."""
    _ensure_schema(ns)
    _write_codex_config(ns, amount_usd=200.0, thresholds=(90, 100))
    captured = _patch_dispatch(ns, monkeypatch)
    _patch_codex_spend(ns, monkeypatch, value=190.0)  # 95% — would cross 90

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO budget_milestones (vendor, period_start_at, period, "
            "threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, "
            "alerted_at) "
            "VALUES ('codex', ?, NULL, 90, 200.0, 190.0, 95.0, ?, ?)",
            (JUNE_KEY, "2026-06-15T00:00:00Z", "2026-06-15T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    ns["maybe_record_codex_budget_milestone"]({})

    assert all(p["threshold"] != 90 for p, _ in captured), captured
    rows = [r for r in _rows(ns) if r["threshold"] == 90]
    assert len(rows) == 1, _rows(ns)


# ── #981 Part A: the hook's budget reads on a pinned snapshot ────────────────


def _two_account_store(h):
    """Two attributed rollouts with distinct nonzero spend."""
    import _lib_accounts
    acct_a = _lib_accounts.account_key("codex", "acct-a")
    acct_b = _lib_accounts.account_key("codex", "acct-b")
    path_a = h.rollout("a", 3)
    path_b = h.rollout("b", 1)
    h.seed_sync()
    conn = h.cache_conn()
    try:
        for path, acct in ((path_a, acct_a), (path_b, acct_b)):
            conn.execute(
                "UPDATE codex_session_entries SET account_key=? "
                "WHERE source_path=?", (acct, str(path)))
        conn.commit()
    finally:
        conn.close()
    return acct_a, acct_b


def test_vendor_crossing_with_debt_pending_accounts_deferred_then_evaluated(
    tmp_path, monkeypatch,
):
    """#981 T-A2. Detects: per-account ladders evaluated while finalization
    debt can still move `account_key` (a mislabeled per-account total), the
    vendor-wide crossing withheld for debt that cannot change any row's cost,
    and per-account ladders that never evaluate once the debt drains."""
    from test_968_gate_reader_contracts import (
        BUDGET_PERIOD_START, BudgetHarness)
    import _lib_accounts
    acct_a = _lib_accounts.account_key("codex", "acct-a")
    acct_b = _lib_accounts.account_key("codex", "acct-b")
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 0.001, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [50],
        "accounts": {acct_a: 1000.0, acct_b: 1000.0}})
    assert _two_account_store(h) == (acct_a, acct_b)
    cache = h.cache
    # Adoption work the budgeted hook may not attempt: a unit already
    # cancelled on two consecutive budgeted calls belongs to the worker.
    conn = h.cache_conn()
    try:
        cache.add_codex_finalization_debt(conn, spans={
            h.root_key: (BUDGET_PERIOD_START, BUDGET_PERIOD_START)})
        debt = cache.load_codex_finalization_debt(conn)
        debt.strikes = cache._CODEX_FINALIZATION_STRIKE_LIMIT
        cache._store_codex_finalization_debt(conn, debt)
        conn.commit()
    finally:
        conn.close()
    vendor_total = h.oracle()
    totals = {acct: h.oracle(account_key=acct) for acct in (acct_a, acct_b)}
    assert vendor_total > 0 and 0 < totals[acct_b] < totals[acct_a]

    h.hook()
    rows = h.budget_rows()
    assert [(r[0], r[2]) for r in rows] == [("*", 50)], rows
    assert rows[0][3] == pytest.approx(vendor_total, abs=1e-12)
    assert [s for s in h.spend if s[1] is not None] == [], (
        "a per-account ladder evaluated while debt was pending")
    fields = h.log()[-1]
    assert (fields["sync"], fields["result"], fields["reason"]) == (
        "deferred", "noop", "budget_finalization_debt")
    assert not h.marker().exists()

    assert cache.run_codex_prune_drain(wait_for_requests=False) is not None
    conn = h.ns["open_cache_db"]()
    try:
        assert cache.load_codex_finalization_debt(conn).empty
    finally:
        conn.close()
    h.spend.clear()
    h.hook()
    evaluated = {acct: total for _start, acct, total in h.spend if acct}
    assert evaluated == pytest.approx(totals, abs=1e-12)
    assert h.syncs == ["budgeted", "budgeted"] and h.direct_parses == []
    assert (h.log()[-1]["result"]) == "success"
    assert h.marker().is_file()


def test_pending_claude_obs_reaches_run_dollar_axes_codex_axis_snapshot_or_deferred(
    tmp_path, monkeypatch,
):
    """#981 T-A4. A pending Claude observation consumed inside the hook's
    ingest cycle reaches `_run_dollar_axes`, whose projected Codex leg reads
    Codex spend. Detects: that indirect read running an implicit Codex sync
    inside the cycle, and a Codex deferral that suppresses the Claude axes."""
    import _cctally_journal as jr
    import _lib_journal as jl
    from test_968_gate_reader_contracts import (
        BUDGET_PERIOD_START, BudgetHarness)
    h = BudgetHarness(tmp_path, monkeypatch, codex_budget={
        "amount_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "projected_enabled": True,
        "alert_thresholds": [90]}, claude={
        "weekly_usd": 1000.0, "period": "calendar-month",
        "alerts_enabled": True, "projected_enabled": True,
        "alert_thresholds": [90]})
    h.rollout("seed", 3)
    h.seed_sync()
    claude_sums: list = []
    monkeypatch.setitem(
        h.ns, "_sum_cost_for_range",
        lambda start, end, **kw: claude_sums.append((start, end)) or 5.0)
    record = sys.modules["_cctally_record"]
    reached: list = []
    real_axes = record._run_dollar_axes

    def axes(*args, **kwargs):
        active = getattr(h.cache, "_active_hook_codex_read_policy", None)
        reached.append(active is not None and active() is not None)
        return real_axes(*args, **kwargs)

    monkeypatch.setattr(record, "_run_dollar_axes", axes)
    jr.append_record(jl.make_obs(
        at="2026-07-25T11:59:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 12.0, "source": "statusline",
                 "resets_at": int(dt.datetime(
                     2026, 7, 28, tzinfo=dt.timezone.utc).timestamp())},
        account=None))
    oracle = h.oracle()

    h.hook()

    assert h.syncs == ["budgeted"], (
        "a Codex read inside the hook's ingest cycle ran an implicit sync")
    assert h.direct_parses == []
    assert reached and all(reached), (
        "the pending Claude observation did not reach the dollar axes inside "
        "the hook's read policy")
    assert claude_sums, "the Claude budget axes did not evaluate"
    assert h.log()[-1]["result"] == "success"
    codex_full = [t for start, acct, t in h.spend
                  if acct is None and start == BUDGET_PERIOD_START]
    assert codex_full and all(
        t == pytest.approx(oracle, abs=1e-12) for t in codex_full), h.spend
    assert oracle > 0
