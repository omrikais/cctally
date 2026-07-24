"""Per-account budget ladders + the `*`-anchor rule (#341 Task 3 Step 4-eval,
spec §6).

Each real account in `budget.accounts` fires its OWN ladder over its OWN stamped
spend (`budget_milestones.account_key` = the real key); the vendor-wide `*`
ladder keeps today's semantics (sum across ALL accounts incl. unattributed).
The Claude vendor-wide subscription-week ladder anchors on the ACTIVE account's
week and is SKIPPED + WARNed when the active identity is genuinely unavailable
(a torn `~/.claude.json` read).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import load_script, redirect_paths

WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)
AS_OF = WEEK_START + dt.timedelta(hours=96)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF))
    return ns


def _seed_window(ns, account_key=None):
    """Seed the current subscription-week snapshot. ``account_key`` stamps it to
    a real account (#341 Step 4-eval + slice C): a per-account ladder anchors on
    its OWN account's week (spec §6), so a per-account fixture must carry the
    account's own snapshot; the vendor-wide `*` ladder anchors on the ACTIVE
    account's week. ``None`` keeps the schema default (`unattributed`)."""
    conn = ns["open_db"]()
    try:
        if account_key is None:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, page_url, source, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_iso(WEEK_START + dt.timedelta(hours=1)),
                 WEEK_START.date().isoformat(),
                 (WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
                 _iso(WEEK_START), _iso(WEEK_END), 40.0, None, "fixture", "{}"),
            )
        else:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, page_url, source, payload_json, "
                " account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_iso(WEEK_START + dt.timedelta(hours=1)),
                 WEEK_START.date().isoformat(),
                 (WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
                 _iso(WEEK_START), _iso(WEEK_END), 40.0, None, "fixture", "{}",
                 account_key),
            )
        conn.commit()
    finally:
        conn.close()


def _set_active(monkeypatch, account_key):
    """Force the active Claude identity to a specific real account (identified)."""
    import _cctally_core
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": account_key, "status": "identified",
                 "natural_id": "nat", "email": "a@x.com", "plan_type": "max"})


def _write_config(ns, *, weekly_usd, accounts, thresholds=(90, 100)):
    import _cctally_core
    block = {"alerts_enabled": True, "alert_thresholds": list(thresholds)}
    if weekly_usd is not None:
        block["weekly_usd"] = weekly_usd
    if accounts is not None:
        block["accounts"] = accounts
    _cctally_core.CONFIG_PATH.write_text(json.dumps({"budget": block}) + "\n")


def _patch_spend(ns, monkeypatch, mapping, default=0.0):
    """Deterministic per-account cost double. The vendor-wide (`*`) path calls
    WITHOUT `account_key` -> key None; a per-account ladder passes account_key."""
    def fake_sum(start, end, mode="auto", project=None, *, skip_sync=False,
                 account_key=None):
        return mapping.get(account_key, default)
    monkeypatch.setitem(ns, "_sum_cost_for_range", fake_sum)


def _patch_dispatch(ns, monkeypatch):
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append(payload)
        return "queued"
    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake_dispatch)
    return captured


def _rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT account_key, threshold, budget_usd, spent_usd, alerted_at "
            "FROM budget_milestones WHERE vendor = 'claude' "
            "ORDER BY account_key, threshold"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# per-account ladder fires on its own scoped spend
# --------------------------------------------------------------------------

def test_per_account_ladder_fires_on_scoped_spend(ns, monkeypatch):
    ka = _key("uuid-A")
    # A is the active account; its own week anchors BOTH its per-account ladder
    # and (active-account) the vendor-wide `*` ladder (spec §6 `*`-anchor).
    _seed_window(ns, account_key=ka)
    _set_active(monkeypatch, ka)
    _write_config(ns, weekly_usd=1000.0, accounts={ka: 100.0})
    # Vendor-wide (None) $500 -> 50% of $1000 (no cross). Account A $95 -> 95% of
    # $100 -> crosses 90 only.
    _patch_spend(ns, monkeypatch, {None: 500.0, ka: 95.0})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    rows = _rows(ns)
    a_rows = [r for r in rows if r["account_key"] == ka]
    star_rows = [r for r in rows if r["account_key"] == "*"]
    assert [r["threshold"] for r in a_rows] == [90]        # A crossed 90
    assert abs(a_rows[0]["budget_usd"] - 100.0) < 1e-9
    assert abs(a_rows[0]["spent_usd"] - 95.0) < 1e-9       # A-only spend
    assert star_rows == []                                 # vendor-wide 50% < 90
    assert any(p["account_key"] == ka and p["threshold"] == 90 for p in captured)


def test_vendor_wide_and_per_account_are_independent(ns, monkeypatch):
    ka = _key("uuid-A")
    _seed_window(ns, account_key=ka)
    _set_active(monkeypatch, ka)
    _write_config(ns, weekly_usd=100.0, accounts={ka: 100.0})
    # Vendor-wide $95 (95% -> crosses 90); account A $50 (50% -> no cross).
    _patch_spend(ns, monkeypatch, {None: 95.0, ka: 50.0})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    rows = _rows(ns)
    assert [r["threshold"] for r in rows if r["account_key"] == "*"] == [90]
    assert [r["threshold"] for r in rows if r["account_key"] == ka] == []


def test_per_account_only_budget_no_vendor_wide_row(ns, monkeypatch):
    ka = _key("uuid-A")
    # A per-account-ONLY ladder anchors on A's own week (spec §6).
    _seed_window(ns, account_key=ka)
    # No vendor-wide weekly_usd — a per-account-ONLY budget.
    _write_config(ns, weekly_usd=None, accounts={ka: 100.0})
    _patch_spend(ns, monkeypatch, {ka: 100.0})  # A at 100% -> crosses 90 + 100
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(r["account_key"] == ka for r in rows)       # no `*` row at all


# --------------------------------------------------------------------------
# `*`-anchor: torn active identity skips the vendor-wide ladder + WARNs
# --------------------------------------------------------------------------

def test_torn_identity_skips_vendor_wide_but_fires_per_account(ns, monkeypatch, capsys):
    import _cctally_core
    ka = _key("uuid-A")
    # A's own week anchors its per-account ladder even while the ACTIVE identity
    # is torn (the torn read only skips the vendor-wide `*` ladder).
    _seed_window(ns, account_key=ka)
    _write_config(ns, weekly_usd=100.0, accounts={ka: 100.0})
    # Both would cross: vendor-wide $95 (95%), account A $95 (95%).
    _patch_spend(ns, monkeypatch, {None: 95.0, ka: 95.0})
    _patch_dispatch(ns, monkeypatch)
    # Force a TORN active-identity read (genuinely unavailable anchor).
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": "unattributed", "status": "torn",
                 "natural_id": None, "email": None, "plan_type": None})
    # Reset the process-global one-shot so the WARN is observable here.
    import _cctally_record
    _cctally_record._BUDGET_ANCHOR_WARNED = False

    ns["maybe_record_budget_milestone"]({})

    rows = _rows(ns)
    # Vendor-wide `*` ladder SKIPPED (anchor unavailable); per-account still fires.
    assert [r["threshold"] for r in rows if r["account_key"] == "*"] == []
    assert [r["threshold"] for r in rows if r["account_key"] == ka] == [90]
    assert "active account anchor is unavailable" in capsys.readouterr().err


def test_stably_absent_identity_still_fires_vendor_wide(ns, monkeypatch):
    # No ~/.claude.json -> stably-absent -> a RESOLVED unattributed anchor, NOT
    # unavailable: the vendor-wide ladder fires exactly as today (byte-stable).
    _seed_window(ns)
    _write_config(ns, weekly_usd=100.0, accounts=None)
    _patch_spend(ns, monkeypatch, {None: 95.0})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_budget_milestone"]({})

    rows = _rows(ns)
    assert [r["threshold"] for r in rows if r["account_key"] == "*"] == [90]


# --------------------------------------------------------------------------
# fail-closed under a cache degrade (#341 Task 4): a per-account ladder whose
# account-scoped spend read hits ``AccountAttributionUnavailable`` (the read
# guard) is SKIPPED this tick — never fired on wrong/merged data, never crashing
# the PASSED-CONN (ingest) cycle, which re-raises every OTHER exception. spec §6
# "unresolvable keys ... skipped ... never a crash".
# --------------------------------------------------------------------------

def _record_for_vendor(ns, monkeypatch, raiser):
    import _cctally_record as rec
    ka = _key("uuid-A")
    monkeypatch.setattr(rec, "_budget_spend_for_vendor", raiser)
    conn = ns["open_db"]()
    try:
        return rec._record_budget_milestone_for_vendor(
            vendor="codex", target=10.0, thresholds=[90, 100],
            period="calendar-month", config={}, tz=None,
            build_payload=lambda **k: {}, account_key=ka,
            conn=conn, as_of=_iso(AS_OF),
        )
    finally:
        conn.close()


def test_account_ladder_cache_degrade_skips_not_crashes_passed_conn(ns, monkeypatch):
    import _cctally_cache

    def _boom(*a, **k):
        raise _cctally_cache.AccountAttributionUnavailable("cache required")

    # Must NOT raise on the passed-conn path; skips the ladder this tick (fires 0).
    assert _record_for_vendor(ns, monkeypatch, _boom) == 0


def test_account_ladder_generic_error_still_reraises_passed_conn(ns, monkeypatch):
    """Non-vacuity: a GENERIC exception on the passed-conn path STILL re-raises
    (invariant ii — a real failure aborts the cycle). Only the attribution-
    unavailable degrade is downgraded to a skip. This raising ALSO proves the
    window resolved and the spend leg was reached (the skip test is non-vacuous)."""
    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    with pytest.raises(RuntimeError):
        _record_for_vendor(ns, monkeypatch, _boom)
