"""#416 Slice 3 — the Codex read model's account axis (spec sections 5.2-5.6).

Decision D2 says selecting a Codex account re-scopes the WHOLE Codex view, not
just which hero card renders. The server therefore emits the legacy merged
bucket unchanged as a PARENT and per-account CHILDREN beside it (section 5.3,
review F10): the aggregator accumulates in encounter order and preserves
first-seen model order plus merged `model_breakdowns`, so re-parenthesizing
floats per account can move a ULP and scalar summation cannot reconstruct
`models` / `model_breakdowns` at all. Weekly rows additionally carry
non-additive `used_pct` and `dollar_per_pct`. No aggregate is reconstructed in
JavaScript.

Everything here is R8-gated: the whole `account_scopes` surface is absent below
two REAL accounts, so a single-account install's envelope is byte-identical.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Mapping

import pytest

from _cctally_dashboard_sources import DashboardReadContext
from _lib_quota import QuotaObservation, QuotaWindowIdentity

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _cache_root_key,
    _install_active_native_cycle,
    _seeded_context,
)
from test_dashboard_accounts_wire import (  # noqa: E402
    _ACCT_A,
    _ACCT_B,
    _seed_codex_accounts,
)

UTC = dt.timezone.utc
UNATTRIBUTED = "unattributed"


def _split_corpus_accounts(cache, keys=(_ACCT_A, _ACCT_B), *, clones=3):
    """Clone the synced corpus row, then stamp every row round-robin over `keys`.

    The clones deliberately REUSE the corpus row's `conversation_key` and
    `source_root_key`, so the thread join still succeeds and the source takes the
    QUALIFIED read path — the one the dashboard actually takes. Fresh orphan rows
    would flip it into its `metadata_incomplete` fallback and exercise a
    different reader (whose entry type already carries the account, which is
    exactly the gap this partition has to close on the main path).
    """
    template = cache.execute(
        "SELECT source_path, line_offset, timestamp_utc, session_id, model, "
        "input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, source_root_key, "
        "conversation_key FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is not None, "precondition: the corpus synced a row"
    for index in range(clones):
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"/cached/clone-{index}.jsonl", 500_000 + index,
             (NOW - dt.timedelta(hours=index + 1)).isoformat(),
             f"clone-session-{index}", template[4], template[5], template[6],
             template[7], template[8], template[9], template[10], template[11]),
        )
    ids = [int(r[0]) for r in cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id")]
    assert len(ids) >= 2, "precondition: the corpus has rows to split"
    for position, row_id in enumerate(ids):
        cache.execute(
            "UPDATE codex_session_entries SET account_key=? WHERE id=?",
            (keys[position % len(keys)], row_id))
    cache.commit()
    return ids


def _observations(root, account_key, *, weekly_reset, used_weekly, used_5h):
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


def _context(cache, stats):
    return DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START,
        now_utc=NOW, display_tz_name="UTC",
    )


@pytest.fixture
def codex_env(tmp_path, monkeypatch):
    """A real synced Codex cache whose rows are split across two accounts."""
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _split_corpus_accounts(cache)
    observations = (
        *_observations(root, _ACCT_A, weekly_reset=NOW + dt.timedelta(days=2),
                       used_weekly=40.0, used_5h=12.0),
        *_observations(root, _ACCT_B, weekly_reset=NOW + dt.timedelta(days=3),
                       used_weekly=55.0, used_5h=30.0),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    try:
        yield ns, cache, stats, source_module, root
    finally:
        cache.close()
        stats.close()


def _build(source_module, cache, stats, *, version):
    return source_module.build_codex_source_state(
        _context(cache, stats), data_version=version)


def _decorate(stats):
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])


# --------------------------------------------------------------------------
# Task 11 — the in-memory partition, parent + children.
# --------------------------------------------------------------------------

# `quota` and `periods` are deliberately NOT here: decoration legitimately
# ADDS ownership keys to their merged rows, so byte identity would be false.
# Dedicated tests below make the precise claim for each: keys ADDED, none
# removed, ZERO pre-existing values changed.
_PARENT_SUBTREES = ("sessions", "projects", "cache_report")


def _flatten(value, prefix=""):
    """Every leaf of a wire subtree as ``path -> scalar``, so a diff can say
    which keys were ADDED versus which values MOVED.

    `Mapping`/`Sequence`, not `dict`/`list`: the wire is frozen into
    `MappingProxyType` and tuples, and `MappingProxyType` is NOT a `dict`
    subclass — matching on `dict` silently collapses the whole subtree into one
    opaque leaf, which reports every real change as zero added keys.
    """
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key in value:
            out.update(_flatten(value[key], f"{prefix}.{key}"))
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{index}]"))
        return out
    return {prefix: value}


def test_the_merged_quota_subtree_only_gains_account_keys(codex_env):
    """F6. Acceptance criterion 7 for the subtree the account rounds changed
    most. `quota` cannot be asserted byte-identical (decoration adds
    `account_key` per history row by design), so assert the exact movement
    instead — every added path is an `account_key`, nothing is removed, and no
    value that existed before changed. A pooled percentage, a moved boundary or
    a re-costed milestone would all show up as a CHANGED value.

    #429 §3.1: the active rows are projections of those history rows and now
    carry the same R8-gated ownership key, because a bare resource key is not
    an identity under decoration — two accounts sharing one `$CODEX_HOME` root
    emit the same key. The claim is unchanged in kind: keys ADDED, none removed,
    zero pre-existing values changed."""
    _ns, cache, stats, source_module, _root = codex_env
    before = _build(source_module, cache, stats, version="quota-v1").data["quota"]
    _decorate(stats)
    after = _build(source_module, cache, stats, version="quota-v1").data["quota"]

    assert len(after["histories"]) == 4, (
        "precondition: two accounts x (weekly, 5h) histories")
    before_flat, after_flat = _flatten(before), _flatten(after)
    added = sorted(set(after_flat) - set(before_flat))
    removed = sorted(set(before_flat) - set(after_flat))
    changed = sorted(
        key for key in set(before_flat) & set(after_flat)
        if before_flat[key] != after_flat[key]
    )
    assert added == sorted(
        [f".histories[{index}].account_key" for index in range(4)]
        + [
            f".summary.active[{index}].account_key"
            for index in range(len(after["summary"]["active"]))
        ]
    )
    assert len(after["summary"]["active"]) == 4, (
        "precondition: all four windows are live, so every active row is "
        "stamped — an empty active set would make the added-keys claim vacuous")
    assert removed == []
    assert changed == []


def test_the_merged_parent_only_gains_declared_ownership_keys(codex_env):
    """Decoration preserves every aggregate and adds only ownership axes."""
    _ns, cache, stats, source_module, _root = codex_env
    before = _build(source_module, cache, stats, version="merged-v1")
    _decorate(stats)
    after = _build(source_module, cache, stats, version="merged-v1")

    assert "account_scopes" in after.data, (
        "precondition: the decorated build must actually emit children")
    for subtree in _PARENT_SUBTREES:
        assert json.dumps(after.data[subtree], sort_keys=True, default=str) == \
            json.dumps(before.data[subtree], sort_keys=True, default=str), (
                f"the merged `{subtree}` moved when a second account appeared")

    before_flat = _flatten(before.data["periods"])
    after_flat = _flatten(after.data["periods"])
    added = sorted(set(after_flat) - set(before_flat))
    removed = sorted(set(before_flat) - set(after_flat))
    changed = sorted(
        key for key in set(before_flat) & set(after_flat)
        if before_flat[key] != after_flat[key]
    )
    assert added == [".weekly.rows[0].account_keys[0]"]
    assert after_flat[added[0]] == _ACCT_A
    assert removed == []
    assert changed == []


def test_an_undecorated_source_emits_no_account_scopes(codex_env):
    """R8. A <=1-real-account install adds NOTHING — the whole surface is
    absent, not present-and-empty."""
    _ns, cache, stats, source_module, _root = codex_env
    state = _build(source_module, cache, stats, version="undecorated-v1")
    assert "account_scopes" not in state.data


def test_every_account_gets_a_scope_keyed_by_account_key(codex_env):
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    assert set(scopes) >= {_ACCT_A, _ACCT_B}
    for scope in scopes.values():
        assert set(scope) >= {
            "periods", "sessions", "projects", "cache_report", "budget",
            "quota", "alerts", "is_empty",
        }
        assert set(scope["periods"]) == {"daily", "monthly", "weekly"}


def test_children_sum_to_the_parent_totals(codex_env):
    """The children partition the parent — no row is dropped and none is
    double-counted. USD only: percentages and resets are NEVER summed (D6)."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    data = _build(source_module, cache, stats, version="v1").data
    scopes = data["account_scopes"]
    for period in ("daily", "monthly"):
        parent = data["periods"][period]
        assert parent["total_cost_usd"] > 0, "precondition: the parent has spend"
        assert sum(
            scope["periods"][period]["total_cost_usd"] for scope in scopes.values()
        ) == pytest.approx(parent["total_cost_usd"], abs=1e-9)
        assert sum(
            scope["periods"][period]["total_tokens"] for scope in scopes.values()
        ) == parent["total_tokens"]


def test_a_child_carries_only_its_own_accounts_rows(codex_env):
    """The whole point of D2: two accounts with disjoint rows must produce
    disjoint children, and a child must not equal the merged parent."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    data = _build(source_module, cache, stats, version="v1").data
    scopes = data["account_scopes"]
    a_sessions = {row["key"] for row in scopes[_ACCT_A]["sessions"]["rows"]}
    b_sessions = {row["key"] for row in scopes[_ACCT_B]["sessions"]["rows"]}
    parent_sessions = {row["key"] for row in data["sessions"]["rows"]}
    assert a_sessions and b_sessions
    assert a_sessions.isdisjoint(b_sessions)
    assert a_sessions | b_sessions == parent_sessions
    assert scopes[_ACCT_A]["periods"]["daily"]["total_cost_usd"] \
        != data["periods"]["daily"]["total_cost_usd"]


def test_an_account_with_no_rows_reports_empty_rather_than_the_parents_numbers(
        codex_env):
    """The literal reported symptom (acceptance criterion 2): selecting an
    account with no usage must not leave the previous account's numbers on
    screen."""
    _ns, cache, stats, source_module, _root = codex_env
    empty_key = "c" * 32
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=empty_key, email="c@x.com", label="carol", plan_type="pro"),
    ])
    data = _build(source_module, cache, stats, version="v1").data
    scope = data["account_scopes"][empty_key]
    assert scope["is_empty"] is True
    assert scope["periods"]["daily"]["rows"] == ()
    assert scope["periods"]["daily"]["total_cost_usd"] == 0.0
    assert scope["sessions"]["rows"] == ()
    assert scope["projects"]["rows"] == ()
    assert data["account_scopes"][_ACCT_A]["is_empty"] is False


def test_the_unattributed_bucket_is_a_scope_of_its_own(codex_env):
    """D1: history that was never durably stamped stays `unattributed` and is
    selectable — after D1 it holds the bulk of Codex history, so it cannot be
    silently folded into a real account."""
    _ns, cache, stats, source_module, _root = codex_env
    cache.execute(
        "UPDATE codex_session_entries SET account_key=NULL WHERE id IN "
        "(SELECT id FROM codex_session_entries ORDER BY id LIMIT 1)")
    cache.commit()
    _decorate(stats)
    data = _build(source_module, cache, stats, version="v1").data
    scope = data["account_scopes"][UNATTRIBUTED]
    assert scope["is_empty"] is False
    assert scope["periods"]["daily"]["total_cost_usd"] > 0


def test_a_child_weekly_row_keeps_its_own_non_additive_percent(codex_env):
    """Review F10's second half: weekly rows carry `used_pct` and
    `dollar_per_pct`, which are NOT additive. A child must carry its own
    account's percentage, never a share of the merged one.

    NOTE — this fixture seeds NO ``window_minutes=10080`` durable rows, so
    ``_codex_weekly_periods``'s SQL returns nothing and the only boundary in
    play is the caller's per-account ``active_cycle``. It therefore cannot
    observe the durable weekly read at all; the Slice 3A B1 blocker is pinned by
    ``test_a_child_weekly_period_carries_only_its_own_accounts_percentage``
    below, which seeds that read's rows."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    assert source_module._codex_weekly_periods(
        stats, source_root_keys=(root,), active_cycle=None) == (), (
            "precondition: this fixture exercises `active_cycle` ONLY — the "
            "durable weekly SQL must return nothing here")
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    a_rows = scopes[_ACCT_A]["periods"]["weekly"]["rows"]
    b_rows = scopes[_ACCT_B]["periods"]["weekly"]["rows"]
    assert a_rows and b_rows
    assert {row.get("used_pct") for row in a_rows} == {40.0}
    assert {row.get("used_pct") for row in b_rows} == {55.0}


# --------------------------------------------------------------------------
# Task 12 — account-scoped quota blocks and alerts (spec sections 5.2, 5.4).
#
# `_quota_wire` filtered blocks by `source_root_key` + time against a SINGLE
# `cycle` — which is `cycles_all[0]`, i.e. the FIRST account's — and never by
# account, so two accounts sharing one physical root saw each other's 5h blocks
# (and its physical dedup then collapsed the two into one). `_alerts_wire`'s
# three SELECTs neither selected nor emitted `account_key`, so removing the
# `alerts-unfiltered-note` disclaimer without this would expose cross-account
# alerts outright.
# --------------------------------------------------------------------------

def _seed_5h_block(stats, *, root, account_key, limit_key, start_at, resets_at):
    stats.execute(
        "INSERT INTO quota_window_blocks (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, limit_id, "
        "limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, "
        "current_percent, last_source_path, last_line_offset, generation, "
        "account_key) VALUES ('codex',?,?, 'primary', 300, 'codex', "
        "'5h limit', ?, ?, ?, ?, 1.0, 9.0, '/x.jsonl', 1, 'g1', ?)",
        (root, limit_key, resets_at.isoformat(), start_at.isoformat(),
         start_at.isoformat(), resets_at.isoformat(), account_key),
    )
    stats.commit()


def _seed_quota_alert(stats, *, root, account_key, threshold, created_at):
    stats.execute(
        "INSERT INTO quota_threshold_events (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
        "threshold, qualifying_kind, qualifying_percent, projected_percent, "
        "severity, created_at_utc, disposition, alerted_at, account_key) "
        "VALUES ('codex',?, 'limit', 'primary', 10080, "
        "'2026-07-22T00:00:00+00:00', ?, 'actual', 92.5, NULL, 'warn', ?, "
        "'alerted', ?, ?)",
        (root, threshold, created_at, created_at, account_key),
    )
    stats.commit()


def _seed_vendor_wide_budget_alert(stats):
    stats.execute(
        "INSERT INTO budget_milestones (vendor, period_start_at, period, "
        "threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, "
        "alerted_at, account_key) VALUES ('codex', '2026-07-01T00:00:00Z', "
        "'monthly', 90, 100.0, 92.0, 92.0, '2026-07-19T00:00:00Z', "
        "'2026-07-19T00:00:00Z', '*')")
    stats.commit()


def test_quota_blocks_are_scoped_to_the_block_identity_account(codex_env):
    """Two accounts sharing one physical root, one 5h window each. Each child
    must see only its own, and the two must be DIFFERENT blocks — the merged
    wire's physical dedup key previously collapsed them into one, so a focused
    account got the other account's block key with its own cost pasted in."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    start_at = NOW - dt.timedelta(hours=4)
    resets_at = NOW + dt.timedelta(hours=1)
    _seed_5h_block(stats, root=root, account_key=_ACCT_A, limit_key="limit-a",
                   start_at=start_at, resets_at=resets_at)
    _seed_5h_block(stats, root=root, account_key=_ACCT_B, limit_key="limit-b",
                   start_at=start_at, resets_at=resets_at)
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    a_blocks = scopes[_ACCT_A]["quota"]["blocks"]
    b_blocks = scopes[_ACCT_B]["quota"]["blocks"]
    assert len(a_blocks) == 1 and len(b_blocks) == 1
    assert {block["account_key"] for block in a_blocks} == {_ACCT_A}
    assert {block["account_key"] for block in b_blocks} == {_ACCT_B}
    assert a_blocks[0]["key"] != b_blocks[0]["key"], (
        "both accounts resolved to ONE block — the physical dedup key still "
        "merges two accounts' windows")


def test_the_merged_block_wire_keeps_both_accounts_windows(codex_env):
    """The parent is "All accounts", not "the first account": both windows must
    be listed there, never silently deduplicated down to one."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    start_at = NOW - dt.timedelta(hours=4)
    resets_at = NOW + dt.timedelta(hours=1)
    _seed_5h_block(stats, root=root, account_key=_ACCT_A, limit_key="limit-a",
                   start_at=start_at, resets_at=resets_at)
    _seed_5h_block(stats, root=root, account_key=_ACCT_B, limit_key="limit-b",
                   start_at=start_at, resets_at=resets_at)
    blocks = _build(source_module, cache, stats, version="v1").data["quota"]["blocks"]
    assert {block["account_key"] for block in blocks} == {_ACCT_A, _ACCT_B}


def test_alerts_carry_the_account_and_vendor_wide_stays_visible(codex_env):
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    _seed_quota_alert(stats, root=root, account_key=_ACCT_A, threshold=90,
                      created_at="2026-07-18T00:00:00+00:00")
    _seed_quota_alert(stats, root=root, account_key=_ACCT_B, threshold=95,
                      created_at="2026-07-18T01:00:00+00:00")
    _seed_vendor_wide_budget_alert(stats)
    rows = _build(source_module, cache, stats, version="v1").data["alerts"]["rows"]
    assert all("account_key" in row for row in rows)
    assert {row["account_key"] for row in rows} == {_ACCT_A, _ACCT_B, "*"}


def test_a_focused_alert_list_hides_another_account_but_keeps_vendor_wide(
        codex_env):
    """Spec section 5.4's explicit policy: a vendor-wide `*` crossing is not
    attributable to one account, so it stays visible under focus and is labelled
    as vendor-wide — while another ACCOUNT's alert must not appear at all."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    _seed_quota_alert(stats, root=root, account_key=_ACCT_A, threshold=90,
                      created_at="2026-07-18T00:00:00+00:00")
    _seed_quota_alert(stats, root=root, account_key=_ACCT_B, threshold=95,
                      created_at="2026-07-18T01:00:00+00:00")
    _seed_vendor_wide_budget_alert(stats)
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    a_rows = scopes[_ACCT_A]["alerts"]["rows"]
    assert {row["account_key"] for row in a_rows} == {_ACCT_A, "*"}
    assert all(row["threshold"] != 95 for row in a_rows)


def test_an_undecorated_alert_row_carries_no_account_key(codex_env):
    """R8 guard-rail: below two REAL accounts nothing is added, so the alerts
    wire is byte-identical."""
    _ns, cache, stats, source_module, root = codex_env
    _seed_quota_alert(stats, root=root, account_key=_ACCT_A, threshold=90,
                      created_at="2026-07-18T00:00:00+00:00")
    rows = _build(source_module, cache, stats, version="v1").data["alerts"]["rows"]
    assert rows and all("account_key" not in row for row in rows)


# --------------------------------------------------------------------------
# Task 13 — share bodies must honour the focused account (spec section 5.6,
# review F16).
#
# This is a DISCLOSURE bug, not a missing feature: the handler already parses
# `account` and already stamps it on the digest, the response and the history
# metadata — it just never passes it to the snapshot builder. A focused user
# therefore exports an ALL-ACCOUNT body LABELLED with the focused account, which
# is worse than an unlabelled one.
# --------------------------------------------------------------------------

def _share_snapshot(source_module, cache, stats, *, panel, account, version="v1"):
    import _cctally_dashboard_share as share_mod
    from types import SimpleNamespace

    state = _build(source_module, cache, stats, version=version)
    data_snap = SimpleNamespace(
        source_bundle=SimpleNamespace(sources={"codex": state}))
    snaps, _states, _panel_data = share_mod._share_build_source_snapshots(
        ls=share_mod._share_load_lib(),
        template=None, template_id="codex-default", panel=panel,
        options={}, source="codex", source_explicit=True,
        data_snap=data_snap, account=account,
    )
    return snaps[0]


def _share_cost(snapshot):
    return sum(
        float(row.cells["cost"].usd) for row in snapshot.rows
        if "cost" in row.cells
    )


def test_a_focused_share_body_contains_only_that_accounts_rows(codex_env):
    """Acceptance criterion 9. The handler already stamped the focused account
    on the digest, the response and the history metadata — so an unscoped body
    is not merely incomplete, it is MISLABELLED as one account's."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    data = _build(source_module, cache, stats, version="v1").data
    scoped = data["account_scopes"][_ACCT_A]["sessions"]
    merged = data["sessions"]
    assert 0 < len(scoped["rows"]) < len(merged["rows"]), (
        "precondition: the focused account owns strictly fewer sessions than "
        "the merged view")

    snapshot = _share_snapshot(
        source_module, cache, stats, panel="sessions", account=_ACCT_A)
    assert len(snapshot.rows) == len(scoped["rows"]), (
        f"the share body carried {len(snapshot.rows)} sessions; the focused "
        f"account owns {len(scoped['rows'])} of {len(merged['rows'])} — "
        "another account's sessions leaked into a focused export")
    assert _share_cost(snapshot) == pytest.approx(
        scoped["total_cost_usd"], abs=1e-9)
    assert _share_cost(snapshot) != pytest.approx(
        merged["total_cost_usd"], abs=1e-9)


def test_an_account_agnostic_share_body_is_unchanged(codex_env):
    """Back-compat guard-rail: a legacy request (no `account`) still exports the
    merged body."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    data = _build(source_module, cache, stats, version="v1").data
    snapshot = _share_snapshot(
        source_module, cache, stats, panel="sessions", account=None)
    assert len(snapshot.rows) == len(data["sessions"]["rows"])
    assert _share_cost(snapshot) == pytest.approx(
        data["sessions"]["total_cost_usd"], abs=1e-9)


def test_a_focused_share_of_an_unknown_account_fails_closed(codex_env):
    """Never fall back to the merged body for an account the decorated source
    does not know — that fallback IS the leak."""
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    with pytest.raises(ValueError):
        _share_snapshot(
            source_module, cache, stats, panel="sessions", account="f" * 32)


def test_a_focused_trend_share_uses_the_accounts_own_periods(codex_env):
    _ns, cache, stats, source_module, _root = codex_env
    _decorate(stats)
    snapshot = _share_snapshot(
        source_module, cache, stats, panel="daily", account=_ACCT_A)
    data = _build(source_module, cache, stats, version="v1").data
    scoped = data["account_scopes"][_ACCT_A]["periods"]["daily"]
    merged = data["periods"]["daily"]
    assert len(snapshot.rows) == len(scoped["rows"])
    assert _share_cost(snapshot) == pytest.approx(
        scoped["total_cost_usd"], abs=1e-9)
    assert _share_cost(snapshot) != pytest.approx(
        merged["total_cost_usd"], abs=1e-9), (
        "the focused daily body still carries every account's spend")


# --------------------------------------------------------------------------
# Task 14 — the Slice 3A review's account-predicate class (B1-B4).
#
# One defect shape, four instances: a stats.db or cache.db read reachable from
# `_codex_account_scopes_wire` that filters by ROOT, TIME and SLOT but not by
# ACCOUNT. `quota_window_blocks` is `UNIQUE(source, source_root_key,
# account_key, logical_limit_key, observed_slot, window_minutes,
# resets_at_utc)`, so two accounts on ONE root genuinely produce two rows — and
# an unscoped read pools them. Pooling a percentage is the never-combine
# violation D6 forbids outright; pooling spend silently re-attributes money.
# --------------------------------------------------------------------------

_WEEK_MINUTES = 10_080


def _seed_weekly_block(stats, *, root, account_key, start_at, resets_at,
                       current_percent, limit_key="limit"):
    stats.execute(
        "INSERT INTO quota_window_blocks (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, limit_id, "
        "limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, "
        "current_percent, last_source_path, last_line_offset, generation, "
        "account_key) VALUES ('codex',?,?, 'primary', 10080, 'codex', "
        "'7-day limit', ?, ?, ?, ?, 0.0, ?, '/x.jsonl', 1, 'g1', ?)",
        (root, limit_key, resets_at.isoformat(), start_at.isoformat(),
         start_at.isoformat(), resets_at.isoformat(), current_percent,
         account_key),
    )
    stats.commit()


def _seed_weekly_milestone(stats, *, root, account_key, resets_at, threshold,
                           captured_at, source_path, line_offset,
                           limit_key="limit", window_minutes=_WEEK_MINUTES):
    stats.execute(
        "INSERT INTO quota_percent_milestones (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
        "percent_threshold, captured_at_utc, source_path, line_offset, "
        "high_water_percent, generation, account_key) "
        "VALUES ('codex',?,?, 'primary', ?, ?, ?, ?, ?, ?, ?, 'g1', ?)",
        (root, limit_key, window_minutes, resets_at.isoformat(), threshold,
         captured_at.isoformat(), source_path, line_offset, threshold,
         account_key),
    )
    stats.commit()


def _seed_quota_snapshot(cache, *, root, account_key, resets_at, captured_at,
                         source_path, line_offset, limit_key="limit",
                         window_minutes=_WEEK_MINUTES, used_percent=1.0):
    cache.execute(
        "INSERT INTO quota_window_snapshots (source, source_root_key, "
        "source_path, line_offset, captured_at_utc, observed_slot, "
        "logical_limit_key, limit_id, limit_name, window_minutes, "
        "used_percent, resets_at_utc, account_key) "
        "VALUES ('codex',?,?,?,?, 'primary', ?, 'codex', '7-day limit', "
        "?,?,?,?)",
        (root, source_path, line_offset, captured_at.isoformat(), limit_key,
         window_minutes, used_percent, resets_at.isoformat(), account_key),
    )
    cache.commit()


def _seed_accounting_row(cache, *, root, account_key, timestamp, source_path,
                         line_offset, session_id, total_tokens):
    cache.execute(
        "INSERT INTO codex_session_entries (source_path, line_offset, "
        "timestamp_utc, session_id, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, total_tokens, "
        "source_root_key, account_key) "
        "VALUES (?,?,?,?, 'gpt-5', ?, 0, 0, 0, ?, ?, ?)",
        (source_path, line_offset, timestamp.isoformat(), session_id,
         total_tokens, total_tokens, root, account_key),
    )
    cache.commit()


# -- B1: the durable weekly read ------------------------------------------

def test_a_child_weekly_period_carries_only_its_own_accounts_percentage(
        codex_env):
    """B1 (BLOCKING). `_codex_weekly_periods` selects `FROM
    quota_window_blocks WHERE source='codex' AND window_minutes=10080 AND
    source_root_key IN (...)` with NO account predicate, merges boundaries
    within `_FIVE_HOUR_JITTER_FLOOR_SECONDS`, pools their `current_percent`
    values and takes `max(...)`. Two accounts on one root whose weekly
    boundaries land 30s apart therefore hand the focused account the OTHER
    account's percentage — and `dollar_per_pct` becomes this account's spend
    divided by that account's percentage."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    # A's live cycle is anchored at `resets_at - 7d`, so putting A's durable row
    # on that same instant makes the live boundary and the durable row ONE
    # period; B's row lands 30s later, inside the jitter floor, and merges too.
    a_reset = NOW + dt.timedelta(days=2)
    a_start = a_reset - dt.timedelta(minutes=_WEEK_MINUTES)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_A,
                       start_at=a_start, resets_at=a_reset,
                       current_percent=40.0)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_B,
                       start_at=a_start + dt.timedelta(seconds=30),
                       resets_at=NOW + dt.timedelta(days=3),
                       current_percent=97.0)
    # Non-vacuity: the RED must come from the durable weekly SQL, not from
    # `active_cycle`. Unscoped, that SQL sees BOTH rows and merges them into one
    # period whose pooled percentage is the max of the two.
    merged = source_module._codex_weekly_periods(
        stats, source_root_keys=(root,), active_cycle=None)
    assert len(merged) == 1 and merged[0].used_percent == 97.0, (
        "precondition: the unscoped weekly read must pool both accounts")

    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    a_rows = scopes[_ACCT_A]["periods"]["weekly"]["rows"]
    assert a_rows, "precondition: account A owns weekly spend"
    assert {row.get("used_pct") for row in a_rows} <= {40.0}, (
        "account A's weekly used_pct carries account B's percentage")
    for row in a_rows:
        assert row.get("dollar_per_pct") == pytest.approx(
            row["cost_usd"] / 40.0, abs=1e-9), (
                "$/1% divides A's spend by another account's percentage")


def test_the_merged_weekly_period_discloses_every_account_in_a_jitter_blend(
        codex_env):
    """#424: a pooled percentage must carry every account that contributed."""
    _ns, cache, stats, source_module, root = codex_env
    a_reset = NOW + dt.timedelta(days=2)
    a_start = a_reset - dt.timedelta(minutes=_WEEK_MINUTES)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_A,
                       start_at=a_start, resets_at=a_reset,
                       current_percent=40.0)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_B,
                       start_at=a_start + dt.timedelta(seconds=30),
                       resets_at=NOW + dt.timedelta(days=3),
                       current_percent=97.0)
    periods = source_module._codex_weekly_periods(
        stats, source_root_keys=(root,), active_cycle=None)
    assert len(periods) == 1
    assert periods[0].used_percent == 97.0
    assert periods[0].start_at == a_start
    assert periods[0].account_keys == (_ACCT_A, _ACCT_B)

    undecorated = _build(
        source_module, cache, stats, version="undecorated-v1").data
    assert undecorated["periods"]["weekly"]["rows"]
    assert all(
        "account_keys" not in row
        for row in undecorated["periods"]["weekly"]["rows"]
    ), "R8: a <=1-real-account provider must keep the old wire shape"

    _decorate(stats)
    parent = _build(source_module, cache, stats, version="v1").data
    parent_rows = parent["periods"]["weekly"]["rows"]
    assert parent_rows, "precondition: the parent owns weekly spend"
    assert parent_rows[0]["account_keys"] == (_ACCT_A, _ACCT_B)
    assert parent_rows[0]["used_pct"] == 97.0

    # Focused children already carry one account's percentage and must not
    # redundantly label themselves with the pooled-parent account axis.
    child_rows = parent["account_scopes"][_ACCT_A]["periods"]["weekly"]["rows"]
    assert child_rows
    assert all("account_keys" not in row for row in child_rows)


# -- B2/F4: the 5h correlation and the milestone ladder --------------------

def _seed_weekly_breakdown_evidence(cache, stats, *, root, account_key,
                                    resets_at, threshold, tag):
    """Durable evidence that makes `codex_quota_breakdown` produce one row."""
    _seed_quota_snapshot(
        cache, root=root, account_key=account_key, resets_at=resets_at,
        captured_at=NOW - dt.timedelta(days=6),
        source_path=f"/private/{tag}.jsonl", line_offset=1)
    _seed_weekly_milestone(
        stats, root=root, account_key=account_key, resets_at=resets_at,
        threshold=threshold, captured_at=NOW - dt.timedelta(minutes=5),
        source_path=f"/private/{tag}.jsonl", line_offset=9)


def test_a_focused_milestone_correlates_only_its_own_five_hour_window(
        codex_env):
    """B2/F4. `_quota_read_model`'s 5h correlation calls
    `load_codex_quota_observations(source_root_keys={identity.source_root_key},
    ...)` and filters by slot and `limit_id` — never by account. The focused
    account's weekly milestone is then annotated with whichever account's 5h
    observation happens to sort last."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    a_reset = NOW + dt.timedelta(days=2)
    _seed_weekly_breakdown_evidence(
        cache, stats, root=root, account_key=_ACCT_A, resets_at=a_reset,
        threshold=41, tag="aaaa")
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    rows = [row for row in scopes[_ACCT_A]["quota"]["milestones"]
            if row["percent"] == 41]
    assert rows, "precondition: A's durable breakdown produced its milestone"
    assert rows[0]["five_hour_percent"] == 12.0, (
        "account A's weekly milestone is annotated with account B's 5h percent")


def test_a_focused_milestone_ladder_excludes_another_accounts_spend(codex_env):
    """B2. `codex_quota_breakdown` reads `codex_session_entries WHERE
    source_root_key=? AND timestamp_utc BETWEEN ? AND ?` — root and time only —
    so a focused account's `cumulative_usd` / `marginal_usd` / token counts
    carry every account's spend on that root. The children must PARTITION the
    parent's ladder, never each repeat it."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    a_reset = NOW + dt.timedelta(days=2)
    b_reset = NOW + dt.timedelta(days=3)
    _seed_weekly_breakdown_evidence(
        cache, stats, root=root, account_key=_ACCT_A, resets_at=a_reset,
        threshold=41, tag="aaaa")
    _seed_weekly_breakdown_evidence(
        cache, stats, root=root, account_key=_ACCT_B, resets_at=b_reset,
        threshold=56, tag="bbbb")
    data = _build(source_module, cache, stats, version="v1").data
    scopes = data["account_scopes"]

    def _one(rows, percent):
        hit = [row for row in rows if row["percent"] == percent]
        assert len(hit) == 1, f"expected exactly one milestone at {percent}%"
        return hit[0]

    parent = _one(data["quota"]["milestones"], 41)
    child_a = _one(scopes[_ACCT_A]["quota"]["milestones"], 41)
    child_b = _one(scopes[_ACCT_B]["quota"]["milestones"], 56)
    assert parent["cumulative_usd"] > 0, "precondition: the root has spend"
    assert child_a["cumulative_usd"] + child_b["cumulative_usd"] == \
        pytest.approx(parent["cumulative_usd"], abs=1e-9), (
            "the two children each repeat the whole root's spend instead of "
            "partitioning it")
    assert child_a["total_tokens"] + child_b["total_tokens"] == \
        parent["total_tokens"]


def test_the_breakdown_start_boundary_is_scoped_to_the_account(codex_env):
    """B2. `_first_block_physical_tuple` selects the first
    `quota_window_snapshots` row for the identity's root/limit/slot/window and
    reset, with NO account predicate. Two accounts sharing one canonical reset
    therefore give the focused account the OTHER account's earlier start
    boundary, and its first milestone segment then absorbs spend from before its
    own block began."""
    from _lib_quota import QuotaWindowIdentity as _Ident
    import _cctally_quota as quota_mod

    _ns, cache, stats, source_module, _root = codex_env
    synthetic = "synthetic-shared-root"
    reset = NOW + dt.timedelta(days=1)
    # B's window opened two hours before A's, on the SAME canonical reset.
    _seed_quota_snapshot(cache, root=synthetic, account_key=_ACCT_B,
                         resets_at=reset, captured_at=NOW - dt.timedelta(hours=5),
                         source_path="/s/b.jsonl", line_offset=1)
    _seed_quota_snapshot(cache, root=synthetic, account_key=_ACCT_A,
                         resets_at=reset, captured_at=NOW - dt.timedelta(hours=3),
                         source_path="/s/a.jsonl", line_offset=1)
    # One of A's rows predates A's own start; only the later one belongs.
    _seed_accounting_row(cache, root=synthetic, account_key=_ACCT_A,
                         timestamp=NOW - dt.timedelta(hours=4),
                         source_path="/s/a.jsonl", line_offset=10,
                         session_id="early", total_tokens=1_000)
    _seed_accounting_row(cache, root=synthetic, account_key=_ACCT_A,
                         timestamp=NOW - dt.timedelta(hours=2),
                         source_path="/s/a.jsonl", line_offset=20,
                         session_id="late", total_tokens=7)
    _seed_weekly_milestone(stats, root=synthetic, account_key=_ACCT_A,
                           resets_at=reset, threshold=50,
                           captured_at=NOW - dt.timedelta(hours=1),
                           source_path="/s/a.jsonl", line_offset=99)
    identity = _Ident(
        source="codex", source_root_key=synthetic, logical_limit_key="limit",
        observed_slot="primary", window_minutes=_WEEK_MINUTES,
        account_key=_ACCT_A,
    )
    # Non-vacuity + the R8 byte-stability guard in one: the unscoped read is
    # today's behaviour and must keep starting at B's earlier boundary.
    merged = quota_mod.codex_quota_breakdown(
        identity, reset, speed="auto", cache_conn=cache, stats_conn=stats)
    assert len(merged) == 1 and merged[0].total_tokens == 1_007, (
        "precondition: the unscoped ladder starts at account B's boundary")
    rows = quota_mod.codex_quota_breakdown(
        identity, reset, speed="auto", cache_conn=cache, stats_conn=stats,
        account_key=_ACCT_A,
    )
    assert len(rows) == 1, "precondition: exactly one seeded milestone"
    assert rows[0].total_tokens == 7, (
        "the ladder started at account B's earlier boundary and swallowed a "
        "row from before account A's own block opened")


def test_a_focused_ladder_survives_a_pre_fold_unattributed_boundary(codex_env):
    """F1. `_quota_read_model` reaches `codex_quota_breakdown` with the CHILD's
    scope key, and that key is post-fold: it comes from the stats `accounts`
    registry or from `obs_partition`, whose observations `load_codex_quota_-
    observations` already ran `adopt_unidentified_observations` over. The
    boundary read it reaches — `_first_block_physical_tuple` over
    `quota_window_snapshots` — is the PRE-fold raw cache, which the fold never
    writes back to. Scope key and rows are stamped by DIFFERENT mechanisms, so a
    row genuinely belonging to the focused account can still carry the sentinel
    there, and the read must widen.

    Strict equality finds no row, and `codex_quota_breakdown` returns `()`
    outright when the boundary blanks — so the child's WHOLE ladder disappears
    while the merged parent renders the crossing (#373 root cause 3 through a
    different door).

    The unattributed accounting row is the other half of the adjudicated rule:
    the boundary is a selection read and widens, the entries read is a COST read
    and stays strict, so those tokens must NOT land in account A's ladder.
    """
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    a_reset = NOW + dt.timedelta(days=2)
    # The fold adopted this window under A, but the snapshot it was derived from
    # still carries no stamp at all.
    _seed_quota_snapshot(
        cache, root=root, account_key=None, resets_at=a_reset,
        captured_at=NOW - dt.timedelta(days=6),
        source_path="/private/aaaa.jsonl", line_offset=1)
    _seed_weekly_milestone(
        stats, root=root, account_key=_ACCT_A, resets_at=a_reset, threshold=41,
        captured_at=NOW - dt.timedelta(minutes=5),
        source_path="/private/aaaa.jsonl", line_offset=9)
    _seed_accounting_row(
        cache, root=root, account_key=None, timestamp=NOW - dt.timedelta(hours=2),
        source_path="/private/unattributed.jsonl", line_offset=42,
        session_id="unattributed-spend", total_tokens=5_000)
    assert cache.execute(
        "SELECT account_key FROM quota_window_snapshots WHERE line_offset=1"
    ).fetchone()[0] is None, "precondition: the boundary evidence is unstamped"

    data = _build(source_module, cache, stats, version="v1").data
    parent = [row for row in data["quota"]["milestones"] if row["percent"] == 41]
    assert len(parent) == 1, "precondition: the merged parent renders the crossing"
    child = [row for row in data["account_scopes"][_ACCT_A]["quota"]["milestones"]
             if row["percent"] == 41]
    assert len(child) == 1, (
        "the focused child's block-start boundary blanked, so its whole ladder "
        "vanished while the parent still renders the crossing")
    assert 0 < child[0]["total_tokens"] < parent[0]["total_tokens"], (
        "the focused ladder is not a partition of the parent's")
    assert child[0]["total_tokens"] + 5_000 <= parent[0]["total_tokens"], (
        "the focused ladder adopted the unattributed accounting row: a cost "
        "read must stay strict, or one row lands in two scopes")


# -- B3: the per-account cycle index ---------------------------------------

def test_each_child_carries_its_own_cycle_index(codex_env):
    """B3. The parent sets `quota.cycle_index`; the children set `blocks` only,
    while the client reads `codex.quota.cycle_index`. Falling back to the
    parent's index under focus would render account A's milestone HISTORY on
    account B's hero, so each child gets a genuinely per-account index."""
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    a_reset = NOW + dt.timedelta(days=2)
    b_reset = NOW + dt.timedelta(days=3)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_A,
                       start_at=a_reset - dt.timedelta(minutes=_WEEK_MINUTES),
                       resets_at=a_reset, current_percent=40.0)
    _seed_weekly_block(stats, root=root, account_key=_ACCT_B,
                       start_at=b_reset - dt.timedelta(minutes=_WEEK_MINUTES),
                       resets_at=b_reset, current_percent=97.0)
    scopes = _build(source_module, cache, stats, version="v1").data["account_scopes"]
    a_index = scopes[_ACCT_A]["quota"]["cycle_index"]
    b_index = scopes[_ACCT_B]["quota"]["cycle_index"]
    assert a_index and b_index, "each child must expose its own cycle index"
    assert len(a_index) == 1 and len(b_index) == 1, (
        "an unscoped `_load_codex_cycles` enumerates both accounts' weekly "
        "boundaries into one index")
    assert {entry["key"] for entry in a_index}.isdisjoint(
        {entry["key"] for entry in b_index})


def test_an_empty_account_gets_an_empty_cycle_index_not_the_parents(codex_env):
    """GUARD-RAIL, not coverage of B3 (#416 closeout F5). It pins a decision
    that was never implemented — falling back to the parent's index — so no
    mutation of the B3 change reddens it: `cycle_index` is guarded by
    `if cycle is not None`, and an account with no cycle short-circuits to `()`
    whether or not `build_codex_cycle_index` takes an `account_key` at all. Keep
    it (the fallback stays tempting), but do not count it as B3 coverage —
    `test_each_child_carries_its_own_cycle_index` is that test."""
    _ns, cache, stats, source_module, _root = codex_env
    empty_key = "c" * 32
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=empty_key, email="c@x.com", label="carol", plan_type="pro"),
    ])
    data = _build(source_module, cache, stats, version="v1").data
    assert data["account_scopes"][empty_key]["quota"]["cycle_index"] == ()


# -- B4: partition keys the registry does not know -------------------------

def test_an_account_key_absent_from_the_registry_still_gets_a_scope(codex_env):
    """B4. The scope set was built from the stats `accounts` registry cards
    while the partition keys off `codex_session_entries.account_key`. A key
    present in the data but not the registry — cache/stats drift after a stats
    rebuild — landed in a partition bucket with no scope, so the union of the
    children was silently LESS than the parent."""
    _ns, cache, stats, source_module, _root = codex_env
    ghost = "d" * 32
    cache.execute(
        "UPDATE codex_session_entries SET account_key=? WHERE id IN "
        "(SELECT id FROM codex_session_entries ORDER BY id LIMIT 1)", (ghost,))
    cache.commit()
    _decorate(stats)  # the registry knows A and B only
    data = _build(source_module, cache, stats, version="v1").data
    scopes = data["account_scopes"]
    assert ghost in scopes, (
        "a data-only account key was dropped: its rows are in no child")
    assert scopes[ghost]["periods"]["daily"]["total_cost_usd"] > 0, (
        "precondition: the ghost key owns real spend")
    assert scopes[ghost]["is_empty"] is False
    parent = data["periods"]["daily"]
    assert sum(scope["periods"]["daily"]["total_cost_usd"]
               for scope in scopes.values()) == pytest.approx(
        parent["total_cost_usd"], abs=1e-9)
    assert sum(scope["periods"]["daily"]["total_tokens"]
               for scope in scopes.values()) == parent["total_tokens"]


def test_a_quota_only_account_key_absent_from_the_registry_gets_a_scope(
        codex_env):
    """The same hole on the OTHER partition axis: an observation stamped to a
    key the registry does not carry must not lose its quota evidence."""
    _ns, cache, stats, source_module, root = codex_env
    ghost = "e" * 32
    observations = (
        *_observations(root, _ACCT_A, weekly_reset=NOW + dt.timedelta(days=2),
                       used_weekly=40.0, used_5h=12.0),
        *_observations(root, ghost, weekly_reset=NOW + dt.timedelta(days=4),
                       used_weekly=61.0, used_5h=8.0),
    )
    monkeypatch_target = source_module
    original = monkeypatch_target.load_codex_quota_observations
    monkeypatch_target.load_codex_quota_observations = lambda **_k: observations
    try:
        _decorate(stats)
        scopes = _build(
            source_module, cache, stats, version="v1").data["account_scopes"]
    finally:
        monkeypatch_target.load_codex_quota_observations = original
    assert ghost in scopes, (
        "a data-only quota account key was dropped: its observations are in "
        "no child")
    assert scopes[ghost]["is_empty"] is False


def test_a_block_only_account_key_still_gets_a_scope(codex_env):
    """F2. The residual-key union read TWO axes — `codex_session_entries.-
    account_key` (the in-memory partition) and `observation.identity.-
    account_key`. `quota_window_blocks.account_key` is a THIRD stamping
    mechanism, written by the quota-observation fold, so a key present only
    there — an observation outside the dashboard's bounded 35-day / 1000-row
    load, or a cache pruned behind a retained durable projection — resolved to
    NO scope at all. The merged parent still lists the block, so the union of
    the children was strictly less than the parent: exactly the failure B4
    exists to prevent, on the one axis B4 missed.
    """
    _ns, cache, stats, source_module, root = codex_env
    _decorate(stats)
    ghost = "f" * 32
    _seed_5h_block(stats, root=root, account_key=ghost, limit_key="limit-ghost",
                   start_at=NOW - dt.timedelta(hours=4),
                   resets_at=NOW + dt.timedelta(hours=1))
    assert not cache.execute(
        "SELECT 1 FROM codex_session_entries WHERE account_key=?", (ghost,)
    ).fetchall(), "precondition: the key exists on NO other axis"

    data = _build(source_module, cache, stats, version="v1").data
    assert ghost in {block["account_key"] for block in data["quota"]["blocks"]}, (
        "precondition: the merged parent lists the block")
    assert ghost in data["account_scopes"], (
        "a durable block's account key resolved to no scope at all, so its "
        "bucket is unreachable from every child")
    assert data["account_scopes"][ghost]["is_empty"] is True, (
        "a block-only key owns neither accounting rows nor observations, so it "
        "must report the honest empty state rather than another account's")


# --------------------------------------------------------------------------
# #429 §4.3 — account scopes are independent evidence domains and must be
# clocked, each at its OWN deadline.
# --------------------------------------------------------------------------


def _weekly_observation(root, account_key, *, captured_at, weekly_reset, used_weekly):
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex", source_root_key=root, logical_limit_key="limit",
            observed_slot="primary", window_minutes=10_080,
            account_key=account_key,
        ),
        captured_at=captured_at,
        used_percent=used_weekly, resets_at=weekly_reset,
        source_path=f"/private/{account_key[:4]}-429.jsonl", line_offset=1,
    )


def _build_decorated_codex_state(
    tmp_path, monkeypatch, *, account_a_captured_at, account_b_captured_at,
    now_utc=NOW, data_version="429-scopes-v1",
):
    """A decorated two-account state whose accounts carry STAGGERED captures.

    The shared `codex_env` fixture stamps both accounts with one timestamp, so
    their freshness flips together and a never-clocked scope is indistinguishable
    from a correctly-clocked one.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    _split_corpus_accounts(cache)
    observations = (
        _weekly_observation(
            root, _ACCT_A, captured_at=account_a_captured_at,
            weekly_reset=now_utc + dt.timedelta(days=2), used_weekly=40.0,
        ),
        _weekly_observation(
            root, _ACCT_B, captured_at=account_b_captured_at,
            weekly_reset=now_utc + dt.timedelta(days=3), used_weekly=55.0,
        ),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    _decorate(stats)
    try:
        return source_module.build_codex_source_state(
            _context(cache, stats), data_version=data_version)
    finally:
        cache.close()
        stats.close()


def test_account_scopes_are_clocked_independently(tmp_path, monkeypatch):
    """#429 §4.3: account scopes were never clocked, so a focused account read
    frozen freshness. They are independent evidence domains and must each flip
    at their OWN deadline, not together."""
    now = NOW
    state = _build_decorated_codex_state(
        tmp_path, monkeypatch,
        account_a_captured_at=now - dt.timedelta(minutes=10),
        account_b_captured_at=now - dt.timedelta(minutes=55),
        now_utc=now,
    )
    scopes = state.data["account_scopes"]
    assert scopes[_ACCT_A]["quota"]["summary"]["freshness"] == "fresh"
    assert scopes[_ACCT_B]["quota"]["summary"]["freshness"] == "fresh"

    # +10 min: only B has crossed its 3600 s weekly bound.
    later = now + dt.timedelta(minutes=10)
    clocked = sys.modules["_cctally_dashboard_sources"].refresh_codex_source_clock(
        state, now_utc=later)
    clocked_scopes = clocked.data["account_scopes"]
    assert clocked_scopes[_ACCT_A]["quota"]["summary"]["freshness"] == "fresh"
    assert clocked_scopes[_ACCT_B]["quota"]["summary"]["freshness"] == "stale"
    # Untouched siblings survive the copy chain.
    assert clocked_scopes[_ACCT_A]["is_empty"] is False
    assert "cycle_index" in clocked_scopes[_ACCT_A]["quota"]
    assert "blocks" in clocked_scopes[_ACCT_A]["quota"]
    assert clocked_scopes[_ACCT_A]["periods"] == scopes[_ACCT_A]["periods"]


def _rebuild_state_with_active(state, active_rows):
    """A copy of `state` whose TOP-LEVEL quota summary carries `active_rows`."""
    import dataclasses

    data = dict(state.data)
    quota = dict(data["quota"])
    summary = dict(quota["summary"])
    summary["active"] = tuple(dict(row) for row in active_rows)
    quota["summary"] = summary
    data["quota"] = quota
    return dataclasses.replace(state, data=data)


def test_clock_restores_prior_active_order_per_account(tmp_path, monkeypatch):
    """#429 §3.1: the clock restores the PRIOR active order, and two decorated
    accounts sharing one `$CODEX_HOME` root publish one bare resource key.

    Keying that restoration on the bare key collapses both rows into a single
    map entry — every colliding row then draws the same sort index and the
    stable sort silently falls back to history order. This test fails if
    `_scoped_quota_identity` is replaced by `str(row["key"])`, which the
    pre-#429 code did.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    state = _build_decorated_codex_state(
        tmp_path, monkeypatch,
        account_a_captured_at=NOW - dt.timedelta(minutes=10),
        account_b_captured_at=NOW - dt.timedelta(minutes=20),
        data_version="429-order-v1",
    )
    active = list(state.data["quota"]["summary"]["active"])
    assert len(active) == 2, "fixture must publish both accounts' live windows"
    assert len({str(row["key"]) for row in active}) == 1, (
        "fixture must have both accounts colliding on ONE bare resource key — "
        "otherwise bare-key restoration would work and this proves nothing"
    )

    # Publish a state whose active order is the REVERSE of history order, so
    # restoring it requires telling the two same-key rows apart.
    reordered = _rebuild_state_with_active(state, tuple(reversed(active)))
    expected = [
        row["account_key"]
        for row in reordered.data["quota"]["summary"]["active"]
    ]
    assert expected == list(reversed(
        [row["account_key"] for row in active])), "reversal did not take"

    clocked = source_module.refresh_codex_source_clock(reordered, now_utc=NOW)
    got = [
        row["account_key"] for row in clocked.data["quota"]["summary"]["active"]
    ]
    assert got == expected


def _decorated_codex_envelope_with_staggered_captures(tmp_path, monkeypatch):
    """The SERIALIZED codex source entry of a decorated, staggered fixture."""
    state = _build_decorated_codex_state(
        tmp_path, monkeypatch,
        account_a_captured_at=NOW - dt.timedelta(minutes=10),
        account_b_captured_at=NOW - dt.timedelta(minutes=55),
        data_version="429-guard-v1",
    )
    wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(state)
    return {"sources": {"codex": wire}}


def _walk_scoped_quota_evidence(envelope):
    """Yield (scoped_identity, subtree_name, evidence_tuple) for every quota row."""
    source_module = sys.modules["_cctally_dashboard_sources"]
    codex = envelope["sources"]["codex"]["data"]

    def rows(container, name):
        for row in container:
            yield (
                source_module._scoped_quota_identity(row), name,
                (row.get("captured_at"), row.get("freshness"),
                 row.get("stale_after_seconds")),
            )
    yield from rows(codex["hero"]["quota"]["active"], "hero.quota.active")
    yield from rows(codex["quota"]["summary"]["active"], "quota.summary.active")
    yield from rows(codex["quota"]["histories"], "quota.histories")
    for scope_key, scope in (codex.get("account_scopes") or {}).items():
        yield from rows(
            scope["quota"]["summary"]["active"], f"scope[{scope_key}].active")
        yield from rows(
            scope["quota"]["histories"], f"scope[{scope_key}].histories")


def test_one_scoped_identity_resolves_to_one_evidence_tuple(tmp_path, monkeypatch):
    """#429 §5.2."""
    envelope = _decorated_codex_envelope_with_staggered_captures(
        tmp_path, monkeypatch)
    seen: dict[tuple, tuple] = {}
    subtrees: dict[tuple, set] = {}
    for identity, subtree, evidence in _walk_scoped_quota_evidence(envelope):
        if identity in seen:
            assert seen[identity] == evidence, (
                f"{identity} disagrees between {subtrees[identity]} and {subtree}")
        seen[identity] = evidence
        subtrees.setdefault(identity, set()).add(subtree)

    # Non-vacuity — all three, because "appears in two subtrees" is satisfiable
    # by the equal hero/summary copies alone while skipping scopes entirely.
    assert any(
        {"quota.histories"} & s and any(t.endswith(".active") for t in s)
        for s in subtrees.values()
    ), "no identity appeared in both a history and its active projection"
    assert any(
        any(t.startswith("scope[") for t in s) and "quota.summary.active" in s
        for s in subtrees.values()
    ), "no identity appeared in both the parent and a child scope"
    codex = envelope["sources"]["codex"]["data"]
    stamped = 0
    for scope_key, scope in codex["account_scopes"].items():
        for row in scope["quota"]["summary"]["active"]:
            if "account_key" in row:
                assert row["account_key"] == scope_key
                stamped += 1
    assert stamped >= 2, (
        "R8/§3.1: a decorated envelope must stamp `account_key` on child active "
        "rows — without it every scoped identity degenerates to ('', key) and "
        "the invariant above holds vacuously")
    # The scoped identity is load-bearing, not decorative: these two accounts
    # share one $CODEX_HOME root and therefore ONE bare resource key, with
    # different evidence. A bare-key invariant would be false by construction.
    parent_active = codex["quota"]["summary"]["active"]
    assert len({row["key"] for row in parent_active}) < len(parent_active), (
        "precondition: the fixture must collide two accounts on one bare key")
