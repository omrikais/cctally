"""Doctor `accounts.*` legs (#341 Task 3, spec §3). Drives the pure check
functions directly with a stubbed `accounts_state` (the checks read only that
field), proving the WARN paths are non-vacuous and the common legacy /
single-account shape stays OK (so the doctor goldens don't shift severity).
"""
from __future__ import annotations

import types

import pytest

from conftest import load_script


@pytest.fixture(scope="module")
def D():
    load_script()
    import _lib_doctor
    return _lib_doctor


def _s(**accounts_state):
    return types.SimpleNamespace(accounts_state=accounts_state or None)


# -- identity --------------------------------------------------------------

def test_identity_ok_stably_absent(D):
    r = D._check_accounts_identity(_s(claude_identity_status="stably_absent"))
    assert r.severity == "ok"


def test_identity_warn_torn(D):
    r = D._check_accounts_identity(_s(claude_identity_status="torn"))
    assert r.severity == "warn"


def test_identity_ok_identified(D):
    r = D._check_accounts_identity(
        _s(claude_identity_status="identified", claude_email="a@x.com"))
    assert r.severity == "ok"
    assert "a@x.com" in r.summary


# -- registry --------------------------------------------------------------

def test_registry_ok_no_accounts(D):
    r = D._check_accounts_registry(_s(real_account_count=0, by_provider={},
                                      missing_provider=0))
    assert r.severity == "ok"


def test_registry_warn_missing_provider(D):
    r = D._check_accounts_registry(_s(real_account_count=1,
                                      by_provider={"claude": 1},
                                      missing_provider=2))
    assert r.severity == "warn"


# -- freshness (always ok, informational) ----------------------------------

def test_freshness_ok_reports_age(D):
    r = D._check_accounts_freshness(_s(freshest_last_seen_age_s=90000))
    assert r.severity == "ok"
    assert "1d" in r.summary


# -- attribution (the writer-discipline backstop) --------------------------

def test_attribution_warn_identified_but_all_unattributed(D):
    r = D._check_accounts_attribution(_s(
        claude_identity_status="identified",
        recent_attributed=0, recent_unattributed=5))
    assert r.severity == "warn"


def test_attribution_ok_legacy_unattributed(D):
    # stably_absent identity + unattributed data = a normal legacy install
    r = D._check_accounts_attribution(_s(
        claude_identity_status="stably_absent",
        recent_attributed=0, recent_unattributed=5))
    assert r.severity == "ok"


def test_attribution_ok_identified_and_attributed(D):
    r = D._check_accounts_attribution(_s(
        claude_identity_status="identified",
        recent_attributed=7, recent_unattributed=0))
    assert r.severity == "ok"


def test_attribution_warn_torn_with_flow(D):
    r = D._check_accounts_attribution(_s(
        claude_identity_status="torn",
        recent_attributed=0, recent_unattributed=3))
    assert r.severity == "warn"


def test_all_four_legs_ok_on_empty_state(D):
    # `accounts_state=None` (fresh install) must be all-OK on every leg.
    s = _s()
    for fn in (D._check_accounts_identity, D._check_accounts_registry,
               D._check_accounts_freshness, D._check_accounts_attribution):
        assert fn(s).severity == "ok"


# -- Codex identity / torn-auth deferral (#416 fix-round review B4) ---------
#
# A persistently torn `<root>/auth.json` defers every growing rollout under that
# root, so Codex spend and quota stop updating while `cache-sync` exits 0. Not
# R8-gated: it names no account and adds no per-account column.

def _codex(**state):
    return types.SimpleNamespace(codex_torn_deferred=state or None)


def test_codex_identity_ok_without_a_marker(D):
    assert D._check_accounts_codex_identity(_codex()).severity == "ok"


def test_codex_identity_ok_when_the_marker_says_zero(D):
    assert D._check_accounts_codex_identity(
        _codex(files=0, at="2026-07-28T00:00:00Z")).severity == "ok"


def test_codex_identity_warns_while_ingest_is_deferred(D):
    r = D._check_accounts_codex_identity(
        _codex(files=3, at="2026-07-28T00:00:00Z"))
    assert r.severity == "warn"
    assert "3" in r.summary
    assert r.remediation
    assert r.details["files"] == 3


def test_codex_identity_leg_is_registered(D):
    ids = [cid for _key, _title, checks in D._CATEGORY_DEFINITIONS
           for cid, _fn in checks]
    assert "accounts.codex_identity" in ids


# -- claude_cost_attribution (#944 spec 5.9: U2, U3, U4') -------------------

_U3_REMEDIATION = (
    "Keep the cctally status line or hooks installed so the active account "
    "is recorded as Claude writes. cctally cache-sync --rebuild keeps these "
    "decisions; it does not change them.")
_U4_REMEDIATION = (
    "Check that the cctally journal directory is writable; the export "
    "retries on the next cctally command.")


def _cca(*, claude_accounts=1, total=0, fallback=0, real=0, preserved=0,
         state="complete", saved=0, of=0):
    return _s(by_provider={"claude": claude_accounts},
              claude_cost_attribution={
                  "totalRows": total, "fallbackRows": fallback,
                  "fallbackRealAccountRows": real,
                  "fallbackUnattributedRows": fallback - real,
                  "preservedRows": preserved,
                  "preservation": {"state": state, "saved": saved,
                                   "total": of, "lastError": None,
                                   "lastErrorAt": None}})


@pytest.mark.parametrize("kw,severity,summary,remediation", [
    # No decisions (also a fresh install with no cache.db).
    ({}, "ok", "Claude cost decided in the last 7 days was attributed from "
     "write-time account evidence (0 rows)", None),
    # All evidence.
    ({"total": 5}, "ok", "Claude cost decided in the last 7 days was "
     "attributed from write-time account evidence (5 rows)", None),
    # Preserved history alone is neither evidence nor fallback.
    ({"preserved": 4}, "ok", "Claude cost decided in the last 7 days was "
     "attributed from write-time account evidence (0 rows)", None),
    # Mixed preserved / fallback, single real account: OK with the count.
    ({"total": 3, "fallback": 2, "real": 2, "preserved": 4}, "ok",
     "2 of 3 Claude cost rows decided in the last 7 days had no write-time "
     "account evidence and took the account active at ingest; this does not "
     "mean they are wrong", None),
    # Two real accounts and a fallback that picked one: WARN.
    ({"claude_accounts": 2, "total": 3, "fallback": 1, "real": 1}, "warn",
     "1 of 3 Claude cost rows decided in the last 7 days had no write-time "
     "account evidence and took the account active at ingest; this does not "
     "mean they are wrong", _U3_REMEDIATION),
    # Two real accounts but every fallback unattributed: OK.
    ({"claude_accounts": 2, "total": 3, "fallback": 1, "real": 0}, "ok",
     "1 of 3 Claude cost rows decided in the last 7 days had no write-time "
     "account evidence and took the account active at ingest; this does not "
     "mean they are wrong", None),
    # Export running: OK, the progress and the fallback sentence both shown.
    ({"total": 2, "fallback": 1, "real": 1, "state": "running", "saved": 3,
      "of": 10}, "ok",
     "Recording existing Claude cost attribution (3 of 10 rows saved); "
     "1 of 2 Claude cost rows decided in the last 7 days had no write-time "
     "account evidence and took the account active at ingest; this does not "
     "mean they are wrong", None),
    # Export stalled: WARN on any install.
    ({"state": "stalled", "saved": 3, "of": 10}, "warn",
     "Recording existing Claude cost attribution is stalled at 3 of 10 rows: "
     "the journal could not be written; Claude cost decided in the last 7 "
     "days was attributed from write-time account evidence (0 rows)",
     _U4_REMEDIATION),
    # Stalled and a decorated real-account fallback: both remediations.
    ({"claude_accounts": 2, "total": 1, "fallback": 1, "real": 1,
      "state": "stalled", "saved": 3, "of": 10}, "warn",
     "Recording existing Claude cost attribution is stalled at 3 of 10 rows: "
     "the journal could not be written; 1 of 1 Claude cost rows decided in "
     "the last 7 days had no write-time account evidence and took the "
     "account active at ingest; this does not mean they are wrong",
     _U4_REMEDIATION + " " + _U3_REMEDIATION),
], ids=["none", "evidence", "preserved-only", "mixed-single-account",
        "two-accounts-real", "two-accounts-null", "running", "stalled",
        "stalled-and-fallback"])
def test_claude_cost_attribution_states(D, kw, severity, summary,
                                        remediation):
    """Detects: a fallback hidden behind export progress, a WARN on a
    single-account install (or for unattributed fallbacks), a stalled export
    reported OK, or a remediation shown on OK."""
    r = D._check_accounts_claude_cost_attribution(_cca(**kw))
    assert (r.severity, r.summary, r.remediation) == (
        severity, summary, remediation)


def test_claude_cost_attribution_leg_is_registered_after_attribution(D):
    ids = [cid for _key, _title, checks in D._CATEGORY_DEFINITIONS
           for cid, _fn in checks]
    assert ids.index("accounts.claude_cost_attribution") == (
        ids.index("accounts.attribution") + 1)
