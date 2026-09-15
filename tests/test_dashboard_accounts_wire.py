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
import pathlib
import sys
import types

import pytest

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


def _weekly_and_5h(
    root, account_key, weekly_reset, used_weekly, used_5h, *, captured_at=None,
):
    captured_at = captured_at or NOW - dt.timedelta(minutes=10)
    return (
        QuotaObservation(
            identity=QuotaWindowIdentity(
                source="codex", source_root_key=root, logical_limit_key="limit",
                observed_slot="primary", window_minutes=10_080,
                account_key=account_key,
            ),
            captured_at=captured_at,
            used_percent=used_weekly, resets_at=weekly_reset,
            source_path=f"/private/{account_key[:4]}.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=QuotaWindowIdentity(
                source="codex", source_root_key=root, logical_limit_key="limit",
                observed_slot="primary", window_minutes=300,
                account_key=account_key,
            ),
            captured_at=captured_at,
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
    cycle_index_calls = []

    def _cycle_index(_conn, *, identity, now_utc, account_key=None):
        cycle_index_calls.append(account_key)
        return ({"key": f"cycle:{account_key or 'merged'}"},)

    monkeypatch.setattr(
        sys.modules["cctally"], "build_codex_cycle_index", _cycle_index)
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
        # A decorated parent has no single cycle history. The parent index
        # would belong to whichever account supplied the representative cycle,
        # so only the account-scoped children may build one.
        assert state.data["quota"]["cycle_index"] == ()
        assert None not in cycle_index_calls
        assert set(cycle_index_calls) == {_ACCT_A, _ACCT_B}
        assert state.data["account_scopes"][_ACCT_A]["quota"]["cycle_index"] == (
            {"key": f"cycle:{_ACCT_A}"},)
        assert state.data["account_scopes"][_ACCT_B]["quota"]["cycle_index"] == (
            {"key": f"cycle:{_ACCT_B}"},)
    finally:
        cache.close()
        stats.close()


def test_decorated_cards_disclose_staleness_per_account(tmp_path, monkeypatch):
    """#360: one stale account cannot borrow another account's fresh marker."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    reset = NOW + dt.timedelta(days=2)
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, reset, used_weekly=40.0, used_5h=12.0,
            captured_at=NOW - dt.timedelta(hours=2),
        ),
        *_weekly_and_5h(
            root, _ACCT_B, reset + dt.timedelta(hours=1),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="decorated-staleness-v1",
        )
        by_key = {card["accountKey"]: card for card in state.data["accounts"]}
        assert by_key[_ACCT_A]["cycleFreshness"] == "stale"
        assert "cycleFreshness" not in by_key[_ACCT_B]
        assert by_key[_ACCT_A]["weeklyPercent"] == 40.0
        assert by_key[_ACCT_A]["resetsAt"] == reset.isoformat()
    finally:
        cache.close()
        stats.close()


def test_expired_account_clears_cycle_fields_but_keeps_historical_spend(
    tmp_path, monkeypatch,
):
    """#360: a reset account cannot keep its old weekly percentage or reset."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-history",
        line_offset=90_101,
    )
    cache.commit()
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, NOW - dt.timedelta(minutes=1),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW + dt.timedelta(days=2),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="decorated-expiry-v1",
        )
        by_key = {card["accountKey"]: card for card in state.data["accounts"]}
        expired = by_key[_ACCT_A]
        assert expired["weeklyPercent"] is None
        assert expired["resetsAt"] is None
        assert expired["spendUsd"] > 0
        assert _ACCT_A not in {
            cycle["accountKey"] for cycle in state.data["hero"]["cycles"]
        }
    finally:
        cache.close()
        stats.close()


def _drop_corpus_accounting_rows(cache):
    """Remove the rows ``_seeded_context`` imported from ``modern-full.jsonl``.

    Those rows carry no account, so ``NULL ≡ unattributed`` files them under the
    sentinel. A test that asserts the sentinel's exact total must delete them
    first or it measures the corpus instead of the change (#564).
    """
    cache.execute(
        "DELETE FROM codex_session_entries WHERE source_path NOT LIKE '/cached/%'")


def test_fallback_cards_are_bounded_to_one_native_cycle_width(tmp_path, monkeypatch):
    """#564: every addend of the decorated hero covers one native cycle at most.

    A card with no live weekly cycle — a real account whose boundary expired, and
    the unattributed sentinel — used to be totalled over the whole ~30-day
    accounting range while the headline summing those cards is labelled as the
    week. Every row below is a clone of one template row, so the live-cycle
    account's single-row card is the unit the bounded cards are compared against.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    window_start = NOW - dt.timedelta(minutes=10_080)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live",
        line_offset=93_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(days=2), session_id="b-recent",
        line_offset=93_002)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(days=10), session_id="b-old",
        line_offset=93_003)
    # The sentinel's in-window row sits EXACTLY on the seven-day edge, which is
    # included because the window starts at `now` rather than at the `+1us` SQL
    # upper bound.
    _insert_account_accounting_row(
        cache, root=root, account_key="unattributed",
        timestamp=window_start, session_id="u-edge", line_offset=93_004)
    _insert_account_accounting_row(
        cache, root=root, account_key="unattributed",
        timestamp=NOW - dt.timedelta(days=12), session_id="u-old",
        line_offset=93_005)
    _drop_corpus_accounting_rows(cache)
    cache.commit()
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, NOW + dt.timedelta(days=2),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW - dt.timedelta(minutes=1),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="bounded-fallback-v1",
        )
        cards = state.data["accounts"]
        by_key = {card["accountKey"]: card for card in cards}
        live, expired = by_key[_ACCT_A], by_key[_ACCT_B]
        sentinel = by_key["unattributed"]
        # Non-vacuity: one row is worth something, so one row and two rows are
        # distinguishable totals.
        assert live["spendUsd"] > 0
        assert expired["resetsAt"] is None
        # Exclusion first: on the unbounded read both of these are twice the unit.
        assert expired["spendUsd"] == pytest.approx(live["spendUsd"])
        assert sentinel["spendUsd"] == pytest.approx(live["spendUsd"])
        for field in (
            "inputTokens", "cachedInputTokens", "outputTokens",
            "reasoningOutputTokens", "totalTokens",
        ):
            assert expired[field] == live[field], field
            assert sentinel[field] == live[field], field
        hero = state.data["hero"]
        assert hero["cost_usd"] == pytest.approx(
            sum(card["spendUsd"] for card in cards), abs=1e-9)
        for hero_field, card_field in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
            ("total_tokens", "totalTokens"),
        ):
            assert hero[hero_field] == sum(card[card_field] for card in cards)
        expected_window = {
            "kind": "trailing-cycle",
            "startAt": window_start.isoformat(),
            "endAt": NOW.isoformat(),
        }
        assert expired["spendWindow"] == expected_window
        assert sentinel["spendWindow"] == expected_window
        assert "spendWindow" not in live
    finally:
        cache.close()
        stats.close()


def test_sentinel_window_card_survives_when_all_its_spend_is_older(
    tmp_path, monkeypatch,
):
    """#564 spec §8 criterion 2a: existence is decided over the wide range.

    Bounding the load that decides whether the sentinel gets a card at all would
    delete the card on any install whose unattributed spend is older than a week.
    The card stays, reports $0.00 for the window, and says which window that is.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live",
        line_offset=94_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=1), session_id="b-live",
        line_offset=94_002)
    _insert_account_accounting_row(
        cache, root=root, account_key="unattributed",
        timestamp=NOW - dt.timedelta(days=12), session_id="u-old",
        line_offset=94_003)
    _drop_corpus_accounting_rows(cache)
    cache.commit()
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, NOW + dt.timedelta(days=2),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW + dt.timedelta(days=3),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="sentinel-window-v1",
        )
        by_key = {card["accountKey"]: card for card in state.data["accounts"]}
        sentinel = by_key.get("unattributed")
        assert sentinel is not None, "the sentinel keeps its card"
        assert sentinel["spendUsd"] == 0.0
        assert sentinel["totalTokens"] == 0
        assert sentinel["spendWindow"]["kind"] == "trailing-cycle"
    finally:
        cache.close()
        stats.close()


def _seed_five_hour_block(stats, *, root, account_key, start, reset, pct=42.0):
    stats.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, "
        "current_percent, last_source_path, last_line_offset, generation, "
        "account_key) "
        "VALUES ('codex', ?, ?, 'primary', 300, '5-hour limit', ?, ?, ?, ?, "
        "1, ?, ?, 1, 'g', ?)",
        (root, f"five-hour-{account_key[:4]}", reset.isoformat(),
         start.isoformat(), start.isoformat(), NOW.isoformat(), pct,
         f"/private/5h-{account_key[:4]}.jsonl", account_key),
    )
    stats.commit()


def test_decorated_hero_spend_and_tokens_merge_every_account(tmp_path, monkeypatch):
    """#416 QA P0-A / spec §6 + D6: the "All accounts" headline is the MERGED
    spend and tokens, not one representative account's.

    The production shape is reproduced exactly: each account owns its own Codex
    root, so the parent's single-cycle read (`cycles_all[0]` plus that cycle's
    `source_root_keys`) cannot see a sibling's spend at all and the headline
    silently equals ONE card while the cards beneath it sum to more. Spend and
    tokens are the ONLY axes D6 lets "All accounts" merge — percentage, reset
    and $/1% stay per-account, which is asserted separately.
    """
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    import _cctally_account
    root_a = _cache_root_key(cache)
    root_b = root_a + "-b"
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    reset_a = NOW + dt.timedelta(days=2)
    reset_b = NOW + dt.timedelta(days=3)
    _insert_account_accounting_row(
        cache, root=root_a, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live", line_offset=91_001)
    # B lives on its OWN root, exactly as a second `~/.codex` profile does.
    _insert_account_accounting_row(
        cache, root=root_b, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=1), session_id="b-live", line_offset=91_002)
    _insert_account_accounting_row(
        cache, root=root_b, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=2), session_id="b-live-2", line_offset=91_003)
    cache.commit()
    observations = (
        *_weekly_and_5h(root_a, _ACCT_A, reset_a, used_weekly=40.0, used_5h=12.0),
        *_weekly_and_5h(root_b, _ACCT_B, reset_b, used_weekly=55.0, used_5h=30.0),
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
            data_version="merged-hero-v1",
        )
        hero = state.data["hero"]
        cards = state.data["accounts"]
        assert len(cards) >= 2, "precondition: a decorated install with real cards"
        # Non-vacuity: at least one sibling's spend is invisible to the
        # representative cycle, so a merged headline MUST exceed the largest card.
        assert max(card["spendUsd"] for card in cards) > 0
        assert sum(card["spendUsd"] for card in cards) \
            > max(card["spendUsd"] for card in cards)
        assert hero["cost_usd"] == pytest.approx(
            sum(card["spendUsd"] for card in cards), abs=1e-9)
        for hero_field, card_field in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
            ("total_tokens", "totalTokens"),
        ):
            assert hero[hero_field] == sum(card[card_field] for card in cards), (
                f"hero.{hero_field} is not the merged sum of accounts[].{card_field}")
    finally:
        cache.close()
        stats.close()


def test_decorated_merged_blocks_are_the_union_of_every_account(tmp_path, monkeypatch):
    """#416 QA P1-A: the "All accounts" Blocks panel must list EVERY account's
    5-hour blocks, not only the representative cycle's.

    `_quota_wire` filters `str(root_key) not in cycle.source_root_keys` against
    a single `cycle` that is `cycles_all[0]`, so in the production shape — one
    Codex root per account — every sibling account's live block is dropped. The
    merged view then UNDERCOUNTS: it reads "1 blocks · $0.86" while focusing the
    sibling reveals a second live block the merged view never showed.

    This is the separate CYCLE-ROOT axis; the account predicate that
    `_quota_wire` applies under focus is correct and stays strict. The merged
    parent is asserted to CONTAIN every per-account child's rows, so it can
    never disagree with the chip the operator focuses next.

    Containment, not equality: the merge is strictly additive over the
    representative-cycle read, because a child whose account has no live cycle
    emits nothing at all. `test_a_block_only_account_key_still_gets_a_scope`
    (tests/test_codex_account_read_model.py) is the guard on that direction — a
    key surviving only in `quota_window_blocks` must stay listed.
    """
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    import _cctally_account
    root_a = _cache_root_key(cache)
    root_b = root_a + "-b"
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    reset_a = NOW + dt.timedelta(days=2)
    reset_b = NOW + dt.timedelta(days=3)
    _insert_account_accounting_row(
        cache, root=root_a, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live", line_offset=92_001)
    _insert_account_accounting_row(
        cache, root=root_b, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=1), session_id="b-live", line_offset=92_002)
    cache.commit()
    # One live 5h block per account, each on its OWN root — exactly the shape a
    # second `~/.codex` profile produces.
    block_start = NOW - dt.timedelta(hours=3)
    block_reset = NOW + dt.timedelta(hours=2)
    _seed_five_hour_block(
        stats, root=root_a, account_key=_ACCT_A,
        start=block_start, reset=block_reset, pct=42.0)
    _seed_five_hour_block(
        stats, root=root_b, account_key=_ACCT_B,
        start=block_start, reset=block_reset, pct=17.0)
    observations = (
        *_weekly_and_5h(root_a, _ACCT_A, reset_a, used_weekly=40.0, used_5h=12.0),
        *_weekly_and_5h(root_b, _ACCT_B, reset_b, used_weekly=55.0, used_5h=30.0),
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
            data_version="merged-blocks-v1",
        )
        parent_blocks = state.data["quota"]["blocks"]
        scopes = state.data["account_scopes"]
        child_blocks = [
            block for scope in scopes.values()
            for block in scope["quota"]["blocks"]
        ]
        child_keys = {str(block["key"]) for block in child_blocks}
        # Non-vacuity: two accounts each own a live block, on distinct roots, so
        # a representative-cycle read can only ever see ONE of them.
        assert len(child_keys) == 2, child_keys
        assert {str(block["account_key"]) for block in child_blocks} == {
            _ACCT_A, _ACCT_B}
        parent_keys = {str(block["key"]) for block in parent_blocks}
        assert child_keys <= parent_keys
        # In this scenario the two are equal — every block belongs to an account
        # with a live cycle — so the containment above is not satisfied trivially
        # by a parent that simply kept its own representative row.
        assert parent_keys == child_keys
        # The footer's "N blocks · $X" is a sum over the very rows above.
        assert sum(float(b["cost_usd"]) for b in parent_blocks) == pytest.approx(
            sum(float(b["cost_usd"]) for b in child_blocks), abs=1e-9)
    finally:
        cache.close()
        stats.close()


def _account_cards(state):
    """``{accountKey: card}`` from a built Codex source state."""
    return {card["accountKey"]: card for card in state.data.get("accounts", ())}


def test_narrow_custom_share_range_keeps_full_cycle_card_spend(
    tmp_path, monkeypatch,
):
    """A one-day share range must not truncate a card whose cycle is a week old.

    `build_codex_source_state` serves the share path too
    (bin/_cctally_dashboard_share.py:1419), where `range_start` is the user's
    custom start verbatim. Partitioning a parent population bounded by that
    start would drop up to six days of each card's spend with no error.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    # Each live cycle begins days before `now`, and most of each account's spend
    # sits INSIDE its cycle but OUTSIDE a one-day share range. That is exactly
    # the population a naive partition of the published accounting range drops.
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(days=5), session_id="a-cycle",
        line_offset=95_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-today",
        line_offset=95_002)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(days=4), session_id="b-cycle",
        line_offset=95_003)
    _drop_corpus_accounting_rows(cache)
    cache.commit()
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, NOW + dt.timedelta(days=1),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW + dt.timedelta(days=2),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        narrow_cards = _account_cards(source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=NOW - dt.timedelta(days=1),
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="narrow-share-v1",
        ))
        wide_cards = _account_cards(source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats,
                range_start=NOW - dt.timedelta(days=30),
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="wide-share-v1",
        ))
        live_keys = [
            key for key, card in wide_cards.items()
            if card.get("weeklyPercent") is not None
        ]
        # Non-vacuity: both real accounts must resolve a live cycle, and each
        # must carry spend that a one-day range cannot see on its own.
        assert sorted(live_keys) == sorted((_ACCT_A, _ACCT_B)), wide_cards
        for key in live_keys:
            card = wide_cards[key]
            assert card["spendUsd"] > 0, key
            assert card["totalTokens"] > 0, key
            assert narrow_cards[key]["spendUsd"] == card["spendUsd"], (
                f"card {key} lost cycle spend under a one-day share range"
            )
            assert narrow_cards[key]["totalTokens"] == card["totalTokens"], (
                f"card {key} lost cycle tokens under a one-day share range"
            )
    finally:
        cache.close()
        stats.close()


def _complete_codex_thread_metadata(cache):
    """Join a thread to every accounting row, so the QUALIFIED reader is used.

    `_insert_account_accounting_row` stamps `conv-<session_id>` and seeds no
    matching `codex_conversation_threads` row, so
    `load_codex_project_metadata_health` reports incomplete rows and a build
    falls back to `load_cached_rooted_codex_accounting_entries`. That fallback
    is a real branch, but it is not the one a healthy dashboard takes. Returns
    how many rows it seeded, which is the non-vacuity evidence that the fixture
    needed them.
    """
    missing = cache.execute(
        "SELECT DISTINCT entries.source_root_key, entries.conversation_key, "
        "       entries.source_path "
        "  FROM codex_session_entries AS entries "
        "  LEFT JOIN codex_conversation_threads AS threads "
        "    ON threads.conversation_key = entries.conversation_key "
        "   AND threads.source_root_key = entries.source_root_key "
        " WHERE threads.conversation_key IS NULL "
        "   AND entries.conversation_key IS NOT NULL "
        "   AND entries.conversation_key <> ''"
    ).fetchall()
    for root_key, conversation_key, source_path in missing:
        cache.execute(
            "INSERT OR IGNORE INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, "
            " root_thread_id, source_path, cwd) VALUES (?,?,?,?,?,?)",
            (conversation_key, root_key, f"native-{conversation_key}",
             f"root-{conversation_key}", source_path, "/tmp/bounded-project"),
        )
    cache.commit()
    return len(missing)


def _three_branch_card_fixture(cache, stats, *, root):
    """Seed the three card branches `_codex_accounts_wire` can take.

    Account A resolves a live weekly cycle (the cycle branch), account B's
    weekly window has already reset (the trailing-cycle fallback branch), and
    the unattributed sentinel holds spend inside the trailing window (the
    sentinel branch). Returns the quota observations both accounts' cards are
    built from.
    """
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(days=3), session_id="a-cycle",
        line_offset=97_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=2), session_id="a-today",
        line_offset=97_002)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(days=2), session_id="b-recent",
        line_offset=97_003)
    _insert_account_accounting_row(
        cache, root=root, account_key="unattributed",
        timestamp=NOW - dt.timedelta(days=1), session_id="u-recent",
        line_offset=97_004)
    _drop_corpus_accounting_rows(cache)
    return (
        *_weekly_and_5h(
            root, _ACCT_A, NOW + dt.timedelta(days=1),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW - dt.timedelta(minutes=1),
            used_weekly=55.0, used_5h=30.0,
        ),
    )


def test_card_values_are_identical_from_either_accounting_reader(
    tmp_path, monkeypatch,
):
    """#583 S5 change 2's reader-equivalence claim, asserted on card VALUES.

    The cards used to be derived from `load_cached_rooted_codex_accounting_
    entries` and are now derived from whichever population the build already
    holds, which on a healthy dashboard is `load_qualified_codex_entries`. That
    substitution is a claim that the two readers admit the same rows, and a
    read-count bound cannot test it: a reader admitting different rows would
    still issue one read. So both populations are loaded over the same store and
    the same range here, and the resulting cards are compared to each other.

    The fixture reaches all three card branches, because the two readers could
    agree on one of them and disagree on another: the live-cycle branch, the
    trailing-cycle fallback for a real account whose weekly window has reset,
    and the unattributed sentinel.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    observations = _three_branch_card_fixture(cache, stats, root=root)
    seeded = _complete_codex_thread_metadata(cache)
    assert seeded, "non-vacuity: the fixture must have lacked thread metadata"
    cache.commit()
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        context = DashboardReadContext(
            cache_conn=cache, stats_conn=stats, range_start=START,
            now_utc=NOW, display_tz_name="UTC",
        )
        accounting_end = NOW + dt.timedelta(microseconds=1)
        cycles = source_module._resolve_codex_weekly_cycle(observations, NOW)
        qualified = source_module.load_qualified_codex_entries(
            START, accounting_end, speed=context.speed, sync=False,
            cache_conn=cache,
        )
        rooted = source_module.load_cached_rooted_codex_accounting_entries(
            START, accounting_end, speed=context.speed, cache_conn=cache,
        )
        # Non-vacuity: two empty populations would produce two equal empty card
        # sets and prove nothing about either reader.
        assert qualified, "the qualified reader must admit rows here"
        assert len(qualified) == len(rooted), (
            f"the two readers admitted {len(qualified)} and {len(rooted)} rows")

        wire = dict(
            quota_observations=observations, cycles=cycles,
            accounting_start=START, accounting_end=accounting_end,
        )
        qualified_cards, qualified_cycles, qualified_certificate = (
            source_module._codex_accounts_wire(
            context, population=qualified, **wire)
        )
        rooted_cards, rooted_cycles, rooted_certificate = (
            source_module._codex_accounts_wire(
            context, population=rooted, **wire)
        )

        by_key = {card["accountKey"]: card for card in qualified_cards}
        live, fallback = by_key[_ACCT_A], by_key[_ACCT_B]
        sentinel = by_key["unattributed"]
        # Each branch must have produced a non-zero card, so equality below is
        # equality of real figures rather than of three zeroes.
        assert live["weeklyPercent"] == 40.0 and live["resetsAt"] is not None
        assert live["spendUsd"] > 0 and live["totalTokens"] > 0
        assert fallback["weeklyPercent"] is None
        assert fallback["spendWindow"]["kind"] == "trailing-cycle"
        assert fallback["spendUsd"] > 0 and fallback["totalTokens"] > 0
        assert sentinel["unattributed"] is True
        assert sentinel["spendUsd"] > 0 and sentinel["totalTokens"] > 0

        assert qualified_cards == rooted_cards, (
            "the two accounting readers produced different account cards")
        assert qualified_cycles == rooted_cycles, (
            "the two accounting readers produced different hero cycles")
        assert qualified_certificate == rooted_certificate
        assert qualified_certificate["status"] == "certified"
        contributions = qualified_certificate["contributions"]
        assert [item["account_key"] for item in contributions] == [
            card["accountKey"] for card in qualified_cards
        ]
        contribution_by_key = {
            item["account_key"]: item for item in contributions
        }
        for card in qualified_cards:
            assert contribution_by_key[card["accountKey"]]["cost_usd"] == (
                card["spendUsd"]
            )
        assert contribution_by_key[_ACCT_A]["period"] == {
            "kind": "native_7_day_cycle",
            "start_at": (NOW - dt.timedelta(days=6)).isoformat(),
            "end_at": (NOW + dt.timedelta(days=1)).isoformat(),
        }
        fallback_period = {
            "kind": "trailing_7_day_cycle",
            "start_at": max(START, NOW - dt.timedelta(days=7)).isoformat(),
            "end_at": NOW.isoformat(),
        }
        assert contribution_by_key[_ACCT_B]["period"] == fallback_period
        assert contribution_by_key["unattributed"]["period"] == fallback_period
    finally:
        cache.close()
        stats.close()


def test_an_empty_source_path_row_parts_the_two_accounting_readers(
    tmp_path, monkeypatch,
):
    """The one residual asymmetry, and why it cannot publish a wrong number.

    `load_cached_rooted_codex_accounting_entries` requires a non-empty
    `source_path` and raises `QualifiedMetadataUnavailable` for the whole read
    when a row lacks one; `load_qualified_codex_entries` requires only the
    joined thread identity, so it admits that row.
    `load_codex_project_metadata_health` classifies a row by its
    `conversation_key` and its thread join and never by its `source_path`, so
    the build does not take the rooted fallback on account of one either.

    The divergence is recorded rather than removed because the card derivation
    fails loudly on such a row: `_codex_entries_from_accounting` raises
    `SourceCapabilityUnavailable` for an entry with no session identity, so the
    two readers cannot silently publish different card totals. No shipped
    Codex ingest statement writes an empty `source_path`.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    # Taken off the module under test rather than by a fresh import: the
    # conftest load-script convention can leave a second copy of
    # `_cctally_source_analytics` in the interpreter, whose exception class is
    # a different object and would not be caught here.
    QualifiedMetadataUnavailable = source_module.QualifiedMetadataUnavailable
    root = _cache_root_key(cache)
    _drop_corpus_accounting_rows(cache)
    template = cache.execute(
        "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens FROM codex_session_entries "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is None, (
        "precondition: the corpus rows are dropped, so the only row read below "
        "is the one this test inserts")
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key, account_key) "
        "VALUES ('', 1, ?, 'no-path', 'gpt-5', 10, 0, 5, 0, 15, ?, "
        "'conv-no-path', NULL)",
        ((NOW - dt.timedelta(hours=1)).isoformat(), root),
    )
    _complete_codex_thread_metadata(cache)
    cache.commit()
    try:
        end = NOW + dt.timedelta(microseconds=1)
        with pytest.raises(QualifiedMetadataUnavailable):
            source_module.load_cached_rooted_codex_accounting_entries(
                START, end, speed="standard", cache_conn=cache)
        qualified = source_module.load_qualified_codex_entries(
            START, end, speed="standard", sync=False, cache_conn=cache)
        assert [entry.source_path for entry in qualified] == [""], (
            "the qualified reader admits the row the rooted reader refuses")
        with pytest.raises(source_module.SourceCapabilityUnavailable):
            source_module._codex_entries_from_accounting(qualified)
    finally:
        cache.close()
        stats.close()


# ── #819: a registry read failure is disclosed, not read as undecorated ─────


def _seed_decorated_codex_install(cache, stats, monkeypatch, source_module):
    """Two real Codex accounts with live spend and their own weekly cycles.

    The same shape ``test_decorated_source_emits_per_account_cards_and_cycles``
    builds, extracted so the failure cases below start from an install that is
    provably decorated rather than from one whose registry happens to be empty.
    Returns the patched observations so a caller can assert on them.
    """
    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    reset_a = NOW + dt.timedelta(days=2)
    reset_b = NOW + dt.timedelta(days=3)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(hours=1), session_id="a-live",
        line_offset=90_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=1), session_id="b-live",
        line_offset=90_002)
    cache.commit()
    observations = (
        *_weekly_and_5h(root, _ACCT_A, reset_a, used_weekly=40.0, used_5h=12.0),
        *_weekly_and_5h(root, _ACCT_B, reset_b, used_weekly=55.0, used_5h=30.0),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    return observations


def test_a_codex_registry_read_failure_is_named_rather_than_read_as_undecorated(
    tmp_path, monkeypatch,
):
    """#819. The bare ``except Exception`` converted a registry read failure
    into a healthy-looking ``False``, so a two-account install published
    exactly the <=1-real-account envelope R8 forbids there, and reported
    nothing at all about why the three gated subtrees had gone.

    The decoration itself cannot survive the failure — there is no count to
    decorate by — so this case pins the DISCLOSURE, not the subtrees: the
    source stays available with its non-account data intact, the envelope
    carries a named warning, and the authoritative account fact is
    ``unresolved`` rather than a count nobody read.
    """
    import sqlite3 as _sqlite3

    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    import _cctally_account
    _seed_decorated_codex_install(cache, stats, monkeypatch, source_module)
    # Sanity: the install really is decorated, so the assertions below cannot
    # be satisfied by an accidentally-empty registry.
    assert _cctally_account.real_account_count(stats, "codex") == 2

    def _unreadable(*_args, **_kwargs):
        raise _sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(_cctally_account, "real_account_count", _unreadable)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="registry-unreadable-v1",
        )

        # The source stays available and its non-account data is preserved.
        assert state.availability in ("ok", "partial", "empty")
        assert state.data is not None
        assert state.data["hero"]["cycle"] is not None
        assert state.data["periods"]["daily"] is not None
        assert state.data["sessions"] is not None
        # The failure is NAMED, and it is not the generic build failure.
        codes = {warning.code for warning in state.warnings}
        assert "codex_account_scope_unresolved" in codes, codes
        assert "source_build_failed" not in codes, codes
        # The authoritative account fact is unresolved, never a healthy count.
        assert state.account_scope is None
    finally:
        cache.close()
        stats.close()


def test_a_healthy_codex_registry_read_publishes_its_count_and_no_warning(
    tmp_path, monkeypatch,
):
    """#819. The control for the case above: the same install, read normally,
    must carry the decoration AND the count that produced it, with nothing
    added to the warning tuple."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    _seed_decorated_codex_install(cache, stats, monkeypatch, source_module)
    try:
        state = source_module.build_codex_source_state(
            DashboardReadContext(
                cache_conn=cache, stats_conn=stats, range_start=START,
                now_utc=NOW, display_tz_name="UTC",
            ),
            data_version="registry-healthy-v1",
        )

        assert "accounts" in state.data
        assert dict(state.account_scope or {}) == {"real_account_count": 2}
        codes = {warning.code for warning in state.warnings}
        assert "codex_account_scope_unresolved" not in codes, codes
    finally:
        cache.close()
        stats.close()


def test_one_codex_registry_reading_feeds_decoration_and_the_account_scope(
    tmp_path, monkeypatch,
):
    """#819 item 1. The decoration gate and ``account_scope`` were two separate
    reads of one registry — ``bin/_cctally_dashboard_sources.py`` inside the
    build and ``bin/_cctally_tui.py`` after it — so one tick could publish a
    decorated Codex envelope beside a combined figure withheld for
    ``account_scope_unresolved``.

    The stub below succeeds once and then fails, which is the disagreement in
    its smallest form: under two reads the first decorates and the second
    withholds. One reading cannot disagree with itself.
    """
    import sqlite3 as _sqlite3

    from _fixture_builders import seed_account
    from conftest import load_script, redirect_paths

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    # AFTER `load_script`: it drops cached `_cctally_*` siblings from
    # `sys.modules`, so a module imported earlier is a stale instance the
    # builder's own deferred import would not resolve to.
    import _cctally_account

    stats = ns["open_db"]()
    try:
        for key, label in ((_ACCT_A, "alice"), (_ACCT_B, "bob")):
            seed_account(
                stats, account_key=key, provider="codex",
                natural_id=f"uuid-{label}", email=f"{label}@x.com",
                label=label, plan_type="pro", label_source="user",
                first_seen_utc="2026-07-01T00:00:00Z",
                last_seen_utc="2026-07-01T00:00:00Z",
            )
        stats.commit()
        inner = _cctally_account.real_account_count
        codex_reads = []

        def _first_read_only(conn, provider):
            if provider != "codex":
                return inner(conn, provider)
            codex_reads.append(provider)
            if len(codex_reads) > 1:
                raise _sqlite3.OperationalError(
                    "no such table: accounts")
            return inner(conn, provider)

        monkeypatch.setattr(
            _cctally_account, "real_account_count", _first_read_only)
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=NOW,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
    finally:
        stats.close()

    codex = bundle.sources["codex"]
    assert len(codex_reads) == 1, (
        "the Codex registry was read more than once in one tick, so the "
        f"decoration gate and the account scope can disagree: {codex_reads}")
    assert dict(codex.account_scope or {}) == {"real_account_count": 2}
    # The two consumers agree BY CONSTRUCTION: decoration is published exactly
    # when the one established count is above one.
    decorated = "accounts" in (codex.data or {})
    scoped = dict(codex.account_scope or {}).get("real_account_count", 0) > 1
    assert decorated == scoped, (codex.account_scope, sorted(codex.data or {}))


def test_the_share_period_rebuild_carries_the_same_registry_contract(
    tmp_path, monkeypatch,
):
    """#819 item 1, the share half. A historical share period rebuilds the
    Codex read model through ``_share_codex_state_for_period``, which calls the
    same builder directly, so the disclosure contract has to hold on a surface
    the dashboard tick never touches.

    The call is DERIVED from the share module's own source rather than asserted
    from memory, so moving the share rebuild onto another builder fails here
    instead of silently leaving this surface uncovered.
    """
    import sqlite3 as _sqlite3

    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    share = sys.modules["_cctally_dashboard_share"]
    import _cctally_account

    share_source = pathlib.Path(share.__file__).read_text(encoding="utf-8")
    assert "build_codex_source_state(" in share_source, (
        "the share period rebuild no longer calls the Codex source builder, "
        "so this case no longer covers the surface it names")

    _seed_decorated_codex_install(cache, stats, monkeypatch, source_module)
    cache.close()
    stats.close()

    def _unreadable(*_args, **_kwargs):
        raise _sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(_cctally_account, "real_account_count", _unreadable)
    state = share._share_codex_state_for_period(
        None, panel="daily",
        options={"period": {
            "kind": "custom",
            "start": START.isoformat(),
            "end": NOW.isoformat(),
        }},
    )

    codes = {warning.code for warning in state.warnings}
    assert "codex_account_scope_unresolved" in codes, codes
    assert state.account_scope is None
    assert state.data is not None


def test_the_reading_decorates_on_exactly_the_registrys_own_rule(tmp_path, monkeypatch):
    """#819. ``AccountRegistryReading.decorated`` evaluates R8 on the count it
    established rather than issuing a second query, so the threshold it applies
    is compared against ``provider_is_decorated``'s own answer instead of being
    restated here as ``> 1``."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    import _cctally_account
    try:
        for expected_count, rows in (
            (0, []),
            (1, [dict(account_key=_ACCT_A, email="a@x.com", label="alice")]),
            (2, [dict(account_key=_ACCT_B, email="b@x.com", label="bob")]),
        ):
            _seed_codex_accounts(stats, rows)
            reading = source_module.read_account_registry(stats, "codex")
            assert reading.count == expected_count
            assert reading.error is None
            assert reading.decorated is _cctally_account.provider_is_decorated(
                stats, "codex")
            assert dict(reading.account_scope or {}) == {
                "real_account_count": expected_count}
    finally:
        cache.close()
        stats.close()


def test_the_share_period_rebuild_reaches_the_codex_builder_at_all(
    tmp_path, monkeypatch,
):
    """#819 step 5, the precondition the contract case above rests on.

    ``_share_codex_state_for_period`` composed its ``data_version`` from
    ``semantics.identity``. ``DashboardSourceSemantics`` carried one
    ``identity`` field when that line was written (`d4e801cfd`) and was later
    split into ``claude_identity`` and ``codex_identity`` without updating it,
    so every historical Codex share period raised ``AttributeError`` before it
    reached the builder. Nothing caught it, because every existing share case
    monkeypatches this function away and never runs its body.

    The identity is asserted to be the CODEX one, not merely present: a Codex
    read model versioned by the Claude configuration digest would reuse a
    rebuilt share across a Codex-only configuration change.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    share = sys.modules["_cctally_dashboard_share"]
    _seed_decorated_codex_install(cache, stats, monkeypatch, source_module)
    cache.close()
    stats.close()

    state = share._share_codex_state_for_period(
        None, panel="daily",
        options={"period": {
            "kind": "custom",
            "start": START.isoformat(),
            "end": NOW.isoformat(),
        }},
    )

    semantics = source_module.resolve_dashboard_source_semantics(
        sys.modules["cctally"].load_config(), display_tz_name=None,
    )
    assert state.source == "codex"
    # `combined_accounting_version` appends its own fragment after the version
    # the share builder composes, so this is containment rather than a suffix.
    assert f":{semantics.codex_identity}" in state.data_version, (
        state.data_version)
    assert semantics.claude_identity not in state.data_version
    assert semantics.codex_identity != semantics.claude_identity


def test_an_unresolved_account_scope_refuses_the_reuse_of_that_generation():
    """#819 item 1. A withheld account scope must not outlive its cause.

    The comment that shipped with the degraded build said `partial` disqualified
    the state from `reuse_coherent_source_state`. It did not: `_coherent_provider`
    admitted `partial`, the build always sets `freshness="fresh"`, and the Codex
    reuse version is an identity digest that a transient registry read failure
    does not move — so nothing in the kernel stopped the degraded object being
    handed straight back for the life of the process.

    #834 S2 (#830) moved the refusal into the kernel itself. `_reusable_provider`
    refuses EVERY `partial` prior, so this generation is no longer reusable by
    construction rather than because a hand-maintained list of warning strings
    happens to name its code. That list, `_CODEX_NON_REUSABLE_WARNING_CODES`,
    is gone: it was a second and incomplete definition of non-reusability, and
    it admitted every partial generation whose cause nobody had added to it.

    Refusing is also what keeps the published envelope internally consistent.
    The reuse branch re-resolves `_tui_resolve_account_scope` on every tick, so
    a registry that recovered while this object was being reused would publish
    an `account_scope` naming two accounts beside a `data` carrying no
    `accounts` and a warning still saying the scope is unresolved — the
    disagreement between the two consumers that item 1's single authoritative
    read exists to make impossible. That argument rules out RETAINING this
    generation under the retry kernel too, which is why its cause is absent
    from `RETAINABLE_PARTIAL_CAUSES`.
    """
    import _lib_dashboard_sources as lds
    import _lib_source_retry as retry
    from _lib_dashboard_sources import SourceDashboardWarning

    def degraded(*codes):
        return lds.SourceDashboardState(
            source="codex",
            availability="partial",
            freshness="fresh",
            warnings=tuple(
                SourceDashboardWarning(code, "message", "accounts")
                for code in codes
            ),
            data_version="codex-version",
            last_success_at=None,
            capabilities={},
            data={"hero": {}},
        )

    for codes in (
        ("codex_account_scope_unresolved",),
        ("codex_projection_incoherent",),
        (),
        ("codex_metadata_incomplete", "codex_cycle_unavailable"),
    ):
        prior = degraded(*codes)
        assert lds._reusable_provider(prior) is False, codes
        assert lds.reuse_coherent_source_state(
            prior, data_version="codex-version") is None, codes

    # Neither of the two codes the retired allowlist named may be RETAINED by
    # the retry kernel either, for the envelope-consistency reason above.
    for code in ("codex_account_scope_unresolved", "codex_projection_incoherent"):
        assert retry.is_retainable_partial_cause(
            retry.normalize_partial_cause(code)) is False, code
