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
