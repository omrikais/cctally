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
