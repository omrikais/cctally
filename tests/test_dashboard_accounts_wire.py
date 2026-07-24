"""#341 Task 4 — the conditional per-account dashboard wire (spec §4, R8).

Two shapes, one guard each:
  * <=1 REAL account  -> ``data`` has NO ``accounts`` key and ``data.hero`` has
    NO ``cycles`` key (byte-identical to today);
  * >1 REAL account   -> ``data.accounts[]`` (one card per account + the
    unattributed bucket when non-empty) and ``data.hero.cycles[]`` (one per
    account with a live weekly cycle) are emitted, per-account spend is scoped,
    and two accounts sharing one physical root each resolve their own cycle.

Monkeypatched module globals (``load_codex_quota_observations``,
``resolve_active_account_keys``) require calling ``build_codex_source_state``
through ``sys.modules["_cctally_dashboard_sources"]`` — the conftest load-script
convention the sibling read-model tests use.
"""
from __future__ import annotations

import datetime as dt
import sys

from _cctally_dashboard_sources import DashboardReadContext
from _lib_quota import QuotaObservation, QuotaWindowIdentity

# Reuse the read-model test's seeding scaffold (a real synced Codex cache).
from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _cache_root_key,
    _install_active_native_cycle,
    _seeded_context,
)

UTC = dt.timezone.utc
_ACCT_A = "a" * 32
_ACCT_B = "b" * 32


def _seed_codex_accounts(stats, rows):
    for r in rows:
        stats.execute(
            "INSERT INTO accounts (account_key, provider, natural_id, email, "
            "label, plan_type, label_source, first_seen_utc, last_seen_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r["account_key"], "codex", r.get("natural_id"), r.get("email"),
             r.get("label"), r.get("plan_type"), "auto",
             "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        )
    stats.commit()


def _insert_account_accounting_row(cache, *, root, account_key, timestamp,
                                   session_id, line_offset):
    """Clone a known-good accounting row, stamping account_key + timestamp."""
    row = cache.execute(
        "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens FROM codex_session_entries "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"/cached/acct-{account_key[:4]}-{line_offset}.jsonl", line_offset,
         timestamp.isoformat(), session_id, row[0], row[1], row[2], row[3],
         row[4], row[5], root, f"conv-{session_id}", account_key),
    )


def _weekly_and_5h(root, account_key, weekly_reset, used_weekly, used_5h):
    return (
        QuotaObservation(
            identity=QuotaWindowIdentity(
                source="codex", source_root_key=root, logical_limit_key="limit",
                observed_slot="primary", window_minutes=10_080,
                account_key=account_key,
            ),
            captured_at=NOW - dt.timedelta(minutes=10),
            used_percent=used_weekly, resets_at=weekly_reset,
            source_path=f"/private/{account_key[:4]}.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=QuotaWindowIdentity(
                source="codex", source_root_key=root, logical_limit_key="limit",
                observed_slot="primary", window_minutes=300,
                account_key=account_key,
            ),
            captured_at=NOW - dt.timedelta(minutes=10),
            used_percent=used_5h, resets_at=NOW + dt.timedelta(hours=4),
            source_path=f"/private/{account_key[:4]}-5h.jsonl", line_offset=2,
        ),
    )


def test_undecorated_source_omits_accounts_and_hero_cycles(tmp_path, monkeypatch):
    """<=1 real account: no `accounts`, no `hero.cycles` -> byte-identical."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    _install_active_native_cycle(
        monkeypatch, source_module,
        reset=NOW + dt.timedelta(days=2), root=_cache_root_key(cache),
    )
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="undecorated-v1",
        )
        # The hero still resolves a single cycle (byte-stable), but the
        # per-account decoration surface is entirely absent.
        assert state.data["hero"]["cycle"] is not None
        assert "accounts" not in state.data
        assert "cycles" not in state.data["hero"]
    finally:
        cache.close()
        stats.close()


def test_decorated_source_emits_per_account_cards_and_cycles(tmp_path, monkeypatch):
    """>1 real account: per-account cards + hero cycles + scoped spend."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    import _cctally_account
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    # Distinct weekly resets so each account resolves its OWN cycle (never
    # collapsing to `conflicting`) even though they share one physical root.
    reset_a = NOW + dt.timedelta(days=2)
    reset_b = NOW + dt.timedelta(days=3)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live", line_offset=90_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=1), session_id="b-live", line_offset=90_002)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=2), session_id="b-live-2", line_offset=90_003)
    cache.commit()
    observations = (
        *_weekly_and_5h(root, _ACCT_A, reset_a, used_weekly=40.0, used_5h=12.0),
        *_weekly_and_5h(root, _ACCT_B, reset_b, used_weekly=55.0, used_5h=30.0),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    monkeypatch.setattr(
        _cctally_account, "resolve_active_account_keys", lambda: {_ACCT_A})
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="decorated-v1",
        )
        accounts = state.data["accounts"]
        by_key = {a["accountKey"]: a for a in accounts}
        assert _ACCT_A in by_key and _ACCT_B in by_key
        a, b = by_key[_ACCT_A], by_key[_ACCT_B]
        assert a["label"] == "alice" and b["label"] == "bob"
        assert a["plan"] == "pro" and b["plan"] == "team"
        assert a["active"] is True and b["active"] is False
        assert a["weeklyPercent"] == 40.0 and b["weeklyPercent"] == 55.0
        assert a["fiveHourPercent"] == 12.0 and b["fiveHourPercent"] == 30.0
        assert a["resetsAt"] == reset_a.isoformat()
        assert b["resetsAt"] == reset_b.isoformat()
        # Per-account spend is scoped: B has two live rows, A has one -> distinct.
        assert a["spendUsd"] > 0 and b["spendUsd"] > 0
        assert b["totalTokens"] == 2 * a["totalTokens"]
        # Hero cycles: one per account with a live weekly cycle.
        cycles = state.data["hero"]["cycles"]
        cyc_keys = {c["accountKey"] for c in cycles}
        assert cyc_keys == {_ACCT_A, _ACCT_B}
        # The unattributed sentinel card renders (pre-feature rows are NULL ->
        # unattributed) but carries no live bars.
        assert _ACCT_B != _ACCT_A
        unattr = by_key.get("unattributed")
        assert unattr is not None and unattr["unattributed"] is True
        assert unattr["weeklyPercent"] is None
    finally:
        cache.close()
        stats.close()
