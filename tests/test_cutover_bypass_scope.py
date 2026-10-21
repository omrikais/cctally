"""#825/#832 fix round A (review P3-6): the account-bound cutover test bypass
is not a production switch.

`CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER=1` lets low-level journal mechanics
suites pin the exact bytes of their own records. Honoured alone, it is an
environment variable that turns off a safety fence in production: a stamped
record would then reach the journal with no cutover before it. It is honoured
only inside a pytest test (`PYTEST_CURRENT_TEST`), and the chokepoint stays
enforced everywhere else.
"""
from conftest import load_script


def test_the_bypass_is_ignored_outside_a_test(monkeypatch):
    load_script()
    import _cctally_journal as jr

    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert jr.account_bound_cutover_enforced() is True


def test_the_bypass_still_serves_a_pytest_suite(monkeypatch):
    load_script()
    import _cctally_journal as jr

    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::t (call)")
    assert jr.account_bound_cutover_enforced() is False
    monkeypatch.delenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER")
    assert jr.account_bound_cutover_enforced() is True
