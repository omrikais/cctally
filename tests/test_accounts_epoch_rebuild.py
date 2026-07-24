"""Epoch-1000->1001 legacy rebuild + no-arg production coordinator path
(#341 Task 1, Step 10, spec §2).

Complements test_accounts_epoch_transition.py — which drives the coordinator
with an INJECTED ``claude_json_path`` — by exercising:

  (a) the NO-ARG production entry point ``run_epoch_transition()`` reading
      ``_cctally_core.CLAUDE_JSON_PATH`` (the exact call the epoch-mismatch
      resolver makes: bin/_cctally_store.py resolve_stats_epoch_mismatch ->
      run_epoch_transition()). Review finding P3-3: prior tests only used the
      injected param, never the production path.

  (b) an end-to-end legacy (pre-#341, account-LESS) rebuild over a journal
      carrying every classifier leg at once — a real-account family
      (``five_hour_block_close`` WITH ``_models``/``_projects`` children, which
      locks in the P2-2 child re-derivation), a legacy usage line
      (``snapshot_accept`` -> ``weekly_usage_snapshots``), a vendor-wide
      ``*``-family (``budget`` -> ``budget_milestones``), and an exempt
      effects-only kind (``weekly_credit_effects``) — asserting each family
      lands in the correct account partition: real-account families under the
      cutover Claude account (or ``unattributed`` when identity is unavailable),
      the ``*``-family under ``*`` (never the cutover account), and the exempt
      kind folding as a clean no-op.

Isolation mirrors tests/test_accounts_journal.py (load_script + redirect_paths;
redirect_paths pins CLAUDE_JSON_PATH to tmp so no test reads the real file).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import _cctally_core  # preserved across load_script()
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
# Legacy (account-LESS) journal legs — the pre-#341 wire shapes.
# --------------------------------------------------------------------------

_WINDOW_KEY = 4242


def _legacy_usage_obs(J):
    """A pre-#341 usage obs (no top-level ``account``). Inert in a rebuild (obs
    fold only via the ingest pipeline, not replay), but present so the obs
    normalisation leg is exercised end-to-end."""
    return J.make_obs(at="2026-01-04T09:00:00Z", src="record-usage",
                      provider="claude",
                      payload={"weekly_percent": 40.0, "source": "statusline"})


def _legacy_snapshot_accept_evt(J):
    """A pre-#341 ``snapshot_accept`` Model-A evt (no ``payload.account_key``) —
    the way ``weekly_usage_snapshots`` is materialised. A real-account family:
    the rebuild's legacy normaliser must stamp it to the cutover account."""
    return J.make_evt(
        kind="snapshot_accept", id="sa:legacy:1", at="2026-01-04T09:00:00Z",
        payload={
            "captured_at_utc": "2026-01-04T09:00:00Z",
            "week_start_date": "2026-01-04",
            "week_end_date": "2026-01-11",
            "week_start_at": "2026-01-04T00:00:00+00:00",
            "week_end_at": "2026-01-11T00:00:00+00:00",
            "weekly_percent": 40.0,
            "source": "statusline",
            "payload_json": "{}",
        })


def _legacy_block_close_evt(J):
    """A pre-#341 ``five_hour_block_close`` evt WITH embedded rollup children,
    none carrying account_key — the P2-2 lock-in leg. Both the parent AND the
    children must re-derive to the cutover account on rebuild."""
    wk = _WINDOW_KEY
    return J.make_evt(
        kind="five_hour_block_close", id=f"fhbc:legacy:{wk}",
        at="2026-01-04T14:00:00Z",
        payload={
            "five_hour_window_key": wk,
            "five_hour_resets_at": "2026-01-04T13:00:00Z",
            "block_start_at": "2026-01-04T08:00:00Z",
            "first_observed_at_utc": "2026-01-04T08:00:00Z",
            "last_observed_at_utc": "2026-01-04T13:00:00Z",
            "final_five_hour_percent": 42.0,
            "created_at_utc": "2026-01-04T08:00:00Z",
            "last_updated_at_utc": "2026-01-04T13:00:00Z",
            "is_closed": 1,
            "_models": [
                {"five_hour_window_key": wk, "model": "claude-opus-4",
                 "input_tokens": 10, "output_tokens": 20, "cost_usd": 1.0,
                 "entry_count": 2},
            ],
            "_projects": [
                {"five_hour_window_key": wk, "project_path": "/repo/x",
                 "input_tokens": 10, "output_tokens": 20, "cost_usd": 1.0,
                 "entry_count": 2},
            ],
        })


def _legacy_budget_evt(J):
    """A pre-#341 vendor-tagged ``budget`` evt (no ``payload.account_key``). A
    ``*``-family: the rebuild MUST NOT normalise it to the cutover account — it
    takes the schema DEFAULT ``*`` (spec §2 scope-matrix exemption)."""
    return J.make_evt(
        kind="budget", id="bm:legacy:1", at="2026-01-04T10:00:00Z",
        payload={
            "vendor": "claude",
            "period_start_at": "2026-01-01T00:00:00+00:00",
            "period": "month",
            "threshold": 50,
            "budget_usd": 100.0,
            "spent_usd": 55.0,
            "consumption_pct": 55.0,
            "crossed_at_utc": "2026-01-04T10:00:00Z",
        })


def _legacy_weekly_credit_effects_evt(J):
    """A pre-#341 effects-only ``weekly_credit_effects`` evt. Exempt kind
    (``_EvtSpec.table is None``): no row to stamp, so the rebuild must fold it
    as a clean no-op (empty suppression -> nothing deleted, no crash)."""
    return J.make_evt(
        kind="weekly_credit_effects", id="wce:legacy:1",
        at="2026-01-04T11:00:00Z", payload={"suppression": []})


def _seed_legacy_journal(jr, J):
    for rec in (
        _legacy_usage_obs(J),
        _legacy_snapshot_accept_evt(J),
        _legacy_block_close_evt(J),
        _legacy_budget_evt(J),
        _legacy_weekly_credit_effects_evt(J),
    ):
        jr.append_record(rec, now_utc=FIXED)


def _write_claude_json(path, account_uuid):
    path.write_text(json.dumps({"oauthAccount": {
        "accountUuid": account_uuid, "emailAddress": "me@x.com"}}))


def _partition(ns):
    """Return the account_key partition of each rebuilt derived family."""
    conn = ns["open_db"]()
    try:
        def col(sql, params=()):
            return sorted(r[0] for r in conn.execute(sql, params).fetchall())
        return {
            "usage": col("SELECT account_key FROM weekly_usage_snapshots"),
            "block": col("SELECT account_key FROM five_hour_blocks "
                         "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)),
            "block_models": col("SELECT account_key FROM five_hour_block_models "
                                "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)),
            "block_projects": col(
                "SELECT account_key FROM five_hour_block_projects "
                "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)),
            "budget": col("SELECT account_key FROM budget_milestones"),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (a) no-arg production coordinator path (review P3-3)
# --------------------------------------------------------------------------

def test_no_arg_transition_reads_claude_json_path(ns, tmp_path):
    """P3-3: ``run_epoch_transition()`` with NO ``claude_json_path`` resolves
    the cutover identity from ``_cctally_core.CLAUDE_JSON_PATH`` (the production
    entry point resolve_stats_epoch_mismatch uses), stamps the cutover op, and
    the ensuing rebuild lands every real-account family under that account."""
    jr, J, acc = _siblings()
    _seed_legacy_journal(jr, J)
    # redirect_paths pins CLAUDE_JSON_PATH to tmp_path/.claude.json.
    _write_claude_json(tmp_path / ".claude.json", "uuid-PROD")
    cutover = acc.account_key("claude", "uuid-PROD")

    recorded = jr.run_epoch_transition()  # NO injected path — the production call

    assert recorded == cutover
    assert jr.find_accounts_cutover_op() == cutover
    part = _partition(ns)
    assert part["usage"] == [cutover]
    assert part["block"] == [cutover]
    assert part["block_models"] == [cutover]
    assert part["block_projects"] == [cutover]
    # The '*'-family is NEVER pulled to the cutover account.
    assert part["budget"] == [acc.VENDOR_WIDE]


def test_no_arg_transition_stably_absent_yields_unattributed(ns, tmp_path):
    """Identity unavailable via the production path (no ~/.claude.json at
    CLAUDE_JSON_PATH) -> cutover op records ``unattributed`` and the real-account
    families land under ``unattributed`` (byte-neutral single-account posture),
    while the ``*``-family still lands under ``*``."""
    jr, J, acc = _siblings()
    _seed_legacy_journal(jr, J)
    # Do NOT create tmp_path/.claude.json -> stably absent.

    recorded = jr.run_epoch_transition()  # NO injected path

    assert recorded == acc.UNATTRIBUTED
    assert jr.find_accounts_cutover_op() == acc.UNATTRIBUTED
    part = _partition(ns)
    assert part["usage"] == [acc.UNATTRIBUTED]
    assert part["block"] == [acc.UNATTRIBUTED]
    assert part["block_models"] == [acc.UNATTRIBUTED]
    assert part["block_projects"] == [acc.UNATTRIBUTED]
    assert part["budget"] == [acc.VENDOR_WIDE]


# --------------------------------------------------------------------------
# (b) end-to-end legacy rebuild — all classifier legs, one pass
# --------------------------------------------------------------------------

def test_legacy_rebuild_partitions_all_families(ns):
    """Direct rebuild over a legacy journal + a real cutover op. Every leg lands
    in its correct partition: real-account families (snapshot_accept usage +
    block_close parent/children) under the cutover account, the '*'-family
    (budget) under '*', and the exempt weekly_credit_effects folds as a clean
    no-op (rebuild completes; no spurious row)."""
    jr, J, acc = _siblings()
    cutover = acc.account_key("claude", "uuid-CUT")
    jr.append_accounts_cutover_op(cutover)
    _seed_legacy_journal(jr, J)

    jr.rebuild_stats_index()  # must not raise despite the effects-only wce leg

    part = _partition(ns)
    # Real-account families -> the cutover account.
    assert part["usage"] == [cutover], "legacy snapshot_accept -> cutover"
    assert part["block"] == [cutover], "legacy block_close parent -> cutover"
    assert part["block_models"] == [cutover], (
        "block_close model children re-derive the parent's account (P2-2)")
    assert part["block_projects"] == [cutover], (
        "block_close project children re-derive the parent's account (P2-2)")
    # '*'-family -> '*', NEVER the cutover account (scope-matrix exemption).
    assert part["budget"] == [acc.VENDOR_WIDE], (
        "vendor-wide budget must stay under '*', not the cutover account")


def test_legacy_rebuild_all_children_agree_with_parent(ns):
    """Cross-check the P2-2 partition consistency: within the rebuilt block, the
    parent block's account_key equals each rollup child's — no parent/child
    account split that would break _resolve_primary_model_for_block or the
    composite child UNIQUE partition."""
    jr, J, acc = _siblings()
    cutover = acc.account_key("claude", "uuid-CUT")
    jr.append_accounts_cutover_op(cutover)
    _seed_legacy_journal(jr, J)
    jr.rebuild_stats_index()

    conn = ns["open_db"]()
    try:
        parent = conn.execute(
            "SELECT id, account_key FROM five_hour_blocks "
            "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)).fetchone()
        # Children join to the parent by block_id AND share its account_key.
        model_kids = conn.execute(
            "SELECT account_key, block_id FROM five_hour_block_models "
            "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)).fetchall()
        proj_kids = conn.execute(
            "SELECT account_key, block_id FROM five_hour_block_projects "
            "WHERE five_hour_window_key = ?", (_WINDOW_KEY,)).fetchall()
    finally:
        conn.close()

    assert parent is not None
    for kid in list(model_kids) + list(proj_kids):
        assert kid[0] == parent["account_key"], "child account matches parent"
        assert kid[1] == parent["id"], "child attaches to the parent block_id"
