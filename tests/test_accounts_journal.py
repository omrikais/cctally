"""Journal protocol contracts for the account dimension (#341 Task 1, Steps 3-4).

Additive `account` field on obs lines, the registered `account_observe` /
`account_label` op kinds with rebuild appliers deriving the `accounts` registry
(with fold-time `last_seen_utc`), and the pure legacy classifier that maps an
account-less data-bearing line to its provider (and thence to the cutover
mapping). Isolation mirrors tests/test_journal_ingest.py.
"""
from __future__ import annotations

import datetime as dt

import pytest

import _cctally_core  # preserved across load_script(), safe at module top
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _siblings():
    import _cctally_journal
    import _lib_journal
    import _lib_accounts
    return _cctally_journal, _lib_journal, _lib_accounts


# --------------------------------------------------------------------------
# make_obs: additive `account` field
# --------------------------------------------------------------------------

def test_make_obs_without_account_is_byte_stable(ns):
    _jr, J, _acc = _siblings()
    rec = J.make_obs(at="2026-07-22T12:00:00Z", src="statusline",
                     provider="claude", payload={"kind": "x"})
    assert "account" not in rec  # byte-identical to the pre-epic line shape


def test_make_obs_with_account_round_trips_and_changes_id(ns):
    _jr, J, _acc = _siblings()
    base = J.make_obs(at="2026-07-22T12:00:00Z", src="statusline",
                      provider="claude", payload={"kind": "x"})
    stamped = J.make_obs(at="2026-07-22T12:00:00Z", src="statusline",
                         provider="claude", payload={"kind": "x"}, account="acct-key-1")
    assert stamped["account"] == "acct-key-1"
    assert stamped["id"] != base["id"]  # account participates in the content id
    # decode round-trips the field
    decoded = J.decode_line(J.encode_line(stamped))
    assert decoded["account"] == "acct-key-1"


# --------------------------------------------------------------------------
# account_observe / account_label -> accounts registry
# --------------------------------------------------------------------------

def _accounts_rows(ns):
    conn = ns["open_db"]()
    try:
        return {
            r["account_key"]: dict(r)
            for r in conn.execute("SELECT * FROM accounts").fetchall()
        }
    finally:
        conn.close()


def test_account_observe_folds_into_registry(ns):
    jr, J, acc = _siblings()
    key = acc.account_key("claude", "uuid-A")
    obs = J.make_account_observe(
        at="2026-07-22T12:00:00Z", account_key=key, provider="claude",
        natural_id="uuid-A", email="a@x.com", plan_type="max",
    )
    jr.append_record(obs, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    rows = _accounts_rows(ns)
    assert key in rows
    row = rows[key]
    assert row["provider"] == "claude"
    assert row["natural_id"] == "uuid-A"
    assert row["email"] == "a@x.com"
    assert row["plan_type"] == "max"
    assert row["first_seen_utc"] == "2026-07-22T12:00:00Z"
    assert row["last_seen_utc"] == "2026-07-22T12:00:00Z"
    assert row["label_source"] == "auto"


def test_last_seen_advances_from_account_stamped_obs(ns):
    jr, J, acc = _siblings()
    key = acc.account_key("claude", "uuid-A")
    jr.append_record(
        J.make_account_observe(at="2026-07-22T12:00:00Z", account_key=key,
                               provider="claude", natural_id="uuid-A"),
        now_utc=FIXED)
    # A later account-stamped usage obs must advance last_seen without any new
    # observe record (spec: last_seen derives from max `at` of stamped lines).
    later = J.make_obs(at="2026-07-22T18:30:00Z", src="statusline",
                       provider="claude",
                       payload={"kind": "heartbeat"}, account=key)
    jr.append_record(later, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    row = _accounts_rows(ns)[key]
    assert row["first_seen_utc"] == "2026-07-22T12:00:00Z"
    assert row["last_seen_utc"] == "2026-07-22T18:30:00Z"


def test_label_precedence_user_beats_later_switcher(ns):
    jr, J, acc = _siblings()
    key = acc.account_key("codex", "acct\x00me@x")
    jr.append_record(
        J.make_account_observe(at="2026-07-22T12:00:00Z", account_key=key,
                               provider="codex", natural_id="acct\x00me@x"),
        now_utc=FIXED)
    # user rename
    jr.append_record(
        J.make_account_label(at="2026-07-22T12:01:00Z", account_key=key,
                             label="My Work", provider="codex"),
        now_utc=FIXED)
    # a LATER switcher-sourced observe must NOT override the user label
    jr.append_record(
        J.make_account_observe(at="2026-07-22T12:02:00Z", account_key=key,
                               provider="codex", natural_id="acct\x00me@x",
                               label="switcher-name", label_source="switcher"),
        now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    row = _accounts_rows(ns)[key]
    assert row["label"] == "My Work"
    assert row["label_source"] == "user"


def test_registry_survives_rebuild(ns):
    jr, J, acc = _siblings()
    key = acc.account_key("claude", "uuid-A")
    jr.append_record(
        J.make_account_observe(at="2026-07-22T12:00:00Z", account_key=key,
                               provider="claude", natural_id="uuid-A",
                               email="a@x.com"),
        now_utc=FIXED)
    jr.append_record(
        J.make_account_label(at="2026-07-22T12:05:00Z", account_key=key,
                             label="Personal", provider="claude"),
        now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    jr.rebuild_stats_index()
    row = _accounts_rows(ns)[key]
    assert row["label"] == "Personal"
    assert row["label_source"] == "user"
    assert row["email"] == "a@x.com"


# --------------------------------------------------------------------------
# legacy classifier (pure)
# --------------------------------------------------------------------------

def test_classify_legacy_usage_obs_is_claude(ns):
    jr, J, acc = _siblings()
    obs = J.make_obs(at="2026-01-01T00:00:00Z", src="statusline",
                     provider="claude", payload={"kind": "weekly_usage_snapshot"})
    assert jr.classify_legacy_provider(obs) == "claude"
    assert jr.legacy_account_key(obs, "claude-legacy-key") == "claude-legacy-key"


def test_classify_legacy_quota_obs_is_codex_unattributed(ns):
    jr, J, acc = _siblings()
    obs = J.make_obs(at="2026-01-01T00:00:00Z", src="codex-hook",
                     provider="codex", payload={"kind": "quota_window_snapshot"})
    assert jr.classify_legacy_provider(obs) == "codex"
    assert jr.legacy_account_key(obs, "claude-legacy-key") == acc.UNATTRIBUTED


def test_classify_legacy_evt_kinds(ns):
    jr, J, acc = _siblings()
    pm = J.make_evt(kind="percent_milestone", id="pm:x", at="2026-01-01T00:00:00Z",
                    payload={})
    assert jr.classify_legacy_provider(pm) == "claude"
    qaa = J.make_evt(kind="quota_alert_arming", id="qaa:x",
                     at="2026-01-01T00:00:00Z", payload={})
    assert jr.classify_legacy_provider(qaa) == "codex"
    budget_codex = J.make_evt(kind="budget", id="bm:x", at="2026-01-01T00:00:00Z",
                              payload={"vendor": "codex"})
    assert jr.classify_legacy_provider(budget_codex) == "codex"
    budget_claude = J.make_evt(kind="budget", id="bm:y", at="2026-01-01T00:00:00Z",
                               payload={"vendor": "claude"})
    assert jr.classify_legacy_provider(budget_claude) == "claude"


def test_legacy_classifier_is_exhaustive_over_evt_and_harvest_kinds(ns):
    """Review finding P2-1: the classifier must be exhaustive. EVERY evt kind in
    `_EVT_SPECS` + every harvest kind in `_HARVEST_SPECS` must have a disposition
    — a provider verdict (`_EVT_KIND_PROVIDER`), the vendor-tagged special case
    (`budget`), or an explicit exemption (`weekly_credit_effects`, effects-only).
    Iterating the spec registries structurally enforces "exhaustive classifier"
    so a future data-bearing kind cannot silently escape classification."""
    jr, J, acc = _siblings()
    kinds = set(jr._EVT_SPECS) | {hs.kind for hs in jr._HARVEST_SPECS}
    # weekly_credit_effects is present and reaches the exempt branch (not a leak).
    assert "weekly_credit_effects" in kinds
    for kind in kinds:
        if kind in jr._CLASSIFIER_EXEMPT_KINDS:
            # Exempt kinds insert no account-bearing row; classifier -> None.
            evt = J.make_evt(kind=kind, id=f"{kind}:x",
                             at="2026-01-01T00:00:00Z", payload={})
            assert jr.classify_legacy_provider(evt) is None
            continue
        if kind in jr._CLASSIFIER_VENDOR_TAGGED_KINDS:
            for vendor in ("claude", "codex"):
                evt = J.make_evt(kind=kind, id=f"{kind}:{vendor}",
                                 at="2026-01-01T00:00:00Z",
                                 payload={"vendor": vendor})
                assert jr.classify_legacy_provider(evt) == vendor
            continue
        assert kind in jr._EVT_KIND_PROVIDER, (
            f"kind {kind!r} has no classifier disposition — add it to "
            "_EVT_KIND_PROVIDER, _CLASSIFIER_VENDOR_TAGGED_KINDS, or "
            "_CLASSIFIER_EXEMPT_KINDS")
        evt = J.make_evt(kind=kind, id=f"{kind}:x", at="2026-01-01T00:00:00Z",
                         payload={})
        assert jr.classify_legacy_provider(evt) in ("claude", "codex")


def test_legacy_classifier_is_exhaustive_over_op_kinds(ns):
    """Review finding P3-E: extend the exhaustiveness guard to OP kinds. Every
    `FOLD_APPLIERS` op kind must have a disposition — the real-account op
    (`weekly_credit_floor` -> claude) or accounts-machinery (`account_observe` /
    `account_label` -> None). Iterating the fold registry structurally enforces
    that a future op kind cannot silently escape classification."""
    jr, J, acc = _siblings()
    for kind in jr.FOLD_APPLIERS:
        op = J.make_op(at="2026-01-01T00:00:00Z", src="x",
                       payload={"kind": kind})
        provider = jr.classify_legacy_provider(op)
        if kind in jr._ACCOUNTS_MACHINERY_KINDS:
            assert provider is None, f"machinery op {kind!r} must not be legacy"
        else:
            assert provider in ("claude", "codex"), (
                f"op kind {kind!r} has no classifier disposition")


def test_two_shaped_already_stamped_guard(ns):
    """#341 rev 4.1: the classifier's already-stamped guard is two-shaped — obs
    carry the account on the top-level `account` field, evt/op carry it inside
    `payload.account_key`. A freshly account-stamped evt/op must classify as None
    (NOT legacy), else a rebuild would re-normalise a correctly-stamped row."""
    jr, J, acc = _siblings()
    # evt stamped via payload.account_key -> not legacy.
    stamped_evt = J.make_evt(kind="percent_milestone", id="pm:z",
                             at="2026-01-01T00:00:00Z",
                             payload={"account_key": "real-key-abc"})
    assert jr.classify_legacy_provider(stamped_evt) is None
    assert jr.legacy_account_key(stamped_evt, "cutover-claude") is None
    # op stamped via payload.account_key -> not legacy.
    stamped_op = J.make_op(at="2026-01-01T00:00:00Z", src="x",
                           payload={"kind": "weekly_credit_floor",
                                    "account_key": "real-key-abc"})
    assert jr.classify_legacy_provider(stamped_op) is None
    # An UNstamped Claude evt is legacy and normalises to the cutover account;
    # normalisation is idempotent (a second pass leaves the stamp untouched).
    legacy_evt = J.make_evt(kind="percent_milestone", id="pm:w",
                            at="2026-01-01T00:00:00Z", payload={})
    jr._normalize_legacy_account_stamp(legacy_evt, "cutover-claude")
    assert legacy_evt["payload"]["account_key"] == "cutover-claude"
    jr._normalize_legacy_account_stamp(legacy_evt, "different-account")
    assert legacy_evt["payload"]["account_key"] == "cutover-claude", (
        "normalisation must be idempotent — never re-stamp an already-stamped row")
    # A `*`-family evt is NEVER normalised (takes the schema DEFAULT '*').
    budget_evt = J.make_evt(kind="budget", id="bm:z", at="2026-01-01T00:00:00Z",
                            payload={"vendor": "claude"})
    jr._normalize_legacy_account_stamp(budget_evt, "cutover-claude")
    assert "account_key" not in budget_evt["payload"], (
        "*-family (budget) must not be legacy-normalised — it uses DEFAULT '*'")


def test_accounts_machinery_records_are_not_legacy(ns):
    jr, J, acc = _siblings()
    obs = J.make_account_observe(at="2026-01-01T00:00:00Z", account_key="k",
                                 provider="claude", natural_id="u")
    lbl = J.make_account_label(at="2026-01-01T00:00:00Z", account_key="k",
                               label="x", provider="claude")
    assert jr.classify_legacy_provider(obs) is None
    assert jr.classify_legacy_provider(lbl) is None
    # An obs that already carries an `account` field is not legacy either.
    stamped = J.make_obs(at="2026-01-01T00:00:00Z", src="statusline",
                         provider="claude",
                         payload={"kind": "weekly_usage_snapshot"}, account="k")
    assert jr.classify_legacy_provider(stamped) is None


# --------------------------------------------------------------------------
# P2-2 (8a review): block_close children re-derive the parent's account on a
# legacy rebuild
# --------------------------------------------------------------------------

def _legacy_block_close_evt(J, *, window_key):
    """A legacy-shaped (UNstamped) five_hour_block_close evt WITH embedded
    _models/_projects rollup children — the pre-#341 wire shape. Neither the
    parent nor the children carry account_key, so a rebuild must re-derive both
    from the cutover mapping (parent via _normalize_legacy_account_stamp, the
    children via _apply_block_close forcing the parent's key)."""
    return J.make_evt(
        kind="five_hour_block_close", id=f"fhbc:legacy:{window_key}",
        at="2026-01-04T14:00:00Z",
        payload={
            "five_hour_window_key": window_key,
            "five_hour_resets_at": "2026-01-04T13:00:00Z",
            "block_start_at": "2026-01-04T08:00:00Z",
            "first_observed_at_utc": "2026-01-04T08:00:00Z",
            "last_observed_at_utc": "2026-01-04T13:00:00Z",
            "final_five_hour_percent": 42.0,
            "created_at_utc": "2026-01-04T08:00:00Z",
            "last_updated_at_utc": "2026-01-04T13:00:00Z",
            "is_closed": 1,
            "_models": [
                {"five_hour_window_key": window_key, "model": "claude-opus-4",
                 "input_tokens": 10, "output_tokens": 20, "cost_usd": 1.0,
                 "entry_count": 2},
            ],
            "_projects": [
                {"five_hour_window_key": window_key, "project_path": "/repo/x",
                 "input_tokens": 10, "output_tokens": 20, "cost_usd": 1.0,
                 "entry_count": 2},
            ],
        })


def test_block_close_children_inherit_parent_account_on_legacy_rebuild(ns):
    """P2-2 (8a review): on the epoch-1000->1001 legacy rebuild,
    `_normalize_legacy_account_stamp` re-derives ONLY the parent block's
    `payload.account_key` to the cutover Claude account; the embedded
    `_models`/`_projects` children stay unstamped and would otherwise take the
    schema DEFAULT 'unattributed' — a parent/child account MISMATCH that breaks
    `_resolve_primary_model_for_block(account_key=<real>)` and the composite
    child UNIQUE partition. `_apply_block_close` must force each child's
    `account_key` to the parent's."""
    jr, J, acc = _siblings()
    cutover = acc.account_key("claude", "uuid-CUT")
    jr.append_accounts_cutover_op(cutover)
    window_key = 111
    jr.append_record(_legacy_block_close_evt(J, window_key=window_key),
                     now_utc=FIXED)
    jr.rebuild_stats_index()

    conn = ns["open_db"]()
    try:
        parent = conn.execute(
            "SELECT id, account_key FROM five_hour_blocks "
            "WHERE five_hour_window_key = ?", (window_key,)).fetchone()
        model_accts = sorted(
            r[0] for r in conn.execute(
                "SELECT account_key FROM five_hour_block_models "
                "WHERE five_hour_window_key = ?", (window_key,)).fetchall())
        proj_accts = sorted(
            r[0] for r in conn.execute(
                "SELECT account_key FROM five_hour_block_projects "
                "WHERE five_hour_window_key = ?", (window_key,)).fetchall())
    finally:
        conn.close()
    assert parent is not None, "the closed block must rebuild"
    assert parent["account_key"] == cutover, (
        "the parent block re-derives to the cutover Claude account")
    assert model_accts == [cutover], (
        "block_close model children must re-derive the parent's account on a "
        f"legacy rebuild (got {model_accts}) — else the child lands under the "
        "DEFAULT 'unattributed', mismatching its parent")
    assert proj_accts == [cutover], (
        "block_close project children must re-derive the parent's account on a "
        f"legacy rebuild (got {proj_accts})")


def test_block_close_children_keep_stamped_account_on_rebuild(ns):
    """Byte-neutrality companion: when the block_close evt is ALREADY
    account-stamped (post-#341 wire shape — parent + children carry the real
    account), the P2-2 force is a no-op. The children keep their own (matching)
    account_key across a rebuild; the force never rewrites a correct stamp."""
    jr, J, acc = _siblings()
    real = acc.account_key("claude", "uuid-REAL")
    window_key = 222
    evt = _legacy_block_close_evt(J, window_key=window_key)
    # Stamp the parent + both children as the live (post-feature) harvest would.
    evt["payload"]["account_key"] = real
    for child in evt["payload"]["_models"] + evt["payload"]["_projects"]:
        child["account_key"] = real
    jr.append_record(evt, now_utc=FIXED)
    jr.rebuild_stats_index()

    conn = ns["open_db"]()
    try:
        model_accts = sorted(
            r[0] for r in conn.execute(
                "SELECT account_key FROM five_hour_block_models "
                "WHERE five_hour_window_key = ?", (window_key,)).fetchall())
        proj_accts = sorted(
            r[0] for r in conn.execute(
                "SELECT account_key FROM five_hour_block_projects "
                "WHERE five_hour_window_key = ?", (window_key,)).fetchall())
    finally:
        conn.close()
    assert model_accts == [real]
    assert proj_accts == [real]
