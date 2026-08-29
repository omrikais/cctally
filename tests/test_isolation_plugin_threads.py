"""#630 S2 — the leaked-thread guard, proven through a real child pytest run.

The guard is exercised through a child process rather than by calling its
functions, because what is under test is pytest's hook ordering: a thread
leaked during teardown must fail the item that leaked it and not its
successor, and only a real run can show that.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

LEAKY = textwrap.dedent('''
    import threading, time

    def test_this_one_leaks_a_thread():
        stop = threading.Event()
        t = threading.Thread(target=lambda: stop.wait(60),
                             name="deliberate-leak", daemon=True)
        t.start()

    def test_this_one_is_clean():
        assert True
''')

CLEAN = textwrap.dedent('''
    import threading

    def test_a_joined_thread_is_not_a_leak():
        done = threading.Event()
        t = threading.Thread(target=done.set, name="joined-worker")
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
''')

REGISTERED = textwrap.dedent('''
    import threading

    import pytest

    from tests._pytest_isolation_plugin import register_long_lived_thread

    @pytest.fixture(scope="module")
    def long_lived():
        stop = threading.Event()
        t = threading.Thread(target=lambda: stop.wait(60),
                             name="registered-worker", daemon=True)
        t.start()
        register_long_lived_thread(t)
        yield t
        stop.set()
        t.join(timeout=30)

    def test_first_user(long_lived):
        assert long_lived.is_alive()

    def test_second_user(long_lived):
        assert long_lived.is_alive()
''')


MIXED = textwrap.dedent('''
    import pytest

    def test_this_one_really_fails():
        assert False

    @pytest.mark.skip(reason="skipped on purpose")
    def test_this_one_is_skipped():
        pass

    @pytest.fixture
    def broken():
        raise RuntimeError("setup blew up")

    def test_this_one_errors_in_setup(broken):
        pass

    def test_this_one_is_fine():
        assert True
''')


XPASS = textwrap.dedent('''
    import pytest

    @pytest.mark.xfail(strict=False, reason="expected to fail, but passes")
    def test_this_one_unexpectedly_passes():
        assert True
''')


TEARDOWN_ERROR = textwrap.dedent('''
    import pytest

    @pytest.fixture
    def raises_on_finalize():
        yield
        raise RuntimeError("finalizer blew up")

    def test_this_one_tears_down_badly(raises_on_finalize):
        assert True
''')


def _run_child(tmp_path, source, name="test_child_leak.py"):
    test_file = tmp_path / name
    test_file.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-p",
         "tests._pytest_isolation_plugin", "-q", "-p", "no:randomly"],
        capture_output=True, text=True, timeout=120,
    )


def test_a_leaked_thread_fails_its_own_test_by_name(tmp_path):
    result = _run_child(tmp_path, LEAKY)
    assert result.returncode == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "test_this_one_leaks_a_thread" in combined
    assert "deliberate-leak" in combined


def test_the_clean_test_in_the_same_file_is_not_blamed(tmp_path):
    result = _run_child(tmp_path, LEAKY)
    combined = result.stdout + result.stderr
    leak_at = combined.find("deliberate-leak")
    assert leak_at != -1
    window = combined[max(0, leak_at - 2000):leak_at]
    assert "test_this_one_is_clean" not in window, (
        "the guard blamed the following test; a thread leaked in teardown must "
        "fail the leaking item, not its successor"
    )


def test_exactly_one_item_is_blamed_and_the_other_is_not(tmp_path):
    """Non-vacuity: the guard must not simply fail everything it sees.

    The verdict is a FAILED item, not a teardown ERROR beside a passed call.
    pytest builds the call report before a teardown leak is observable, so the
    verdict is moved rather than duplicated: `pytest_report_teststatus`
    suppresses the status of a passing call report and the item's single
    verdict is emitted from its teardown report, which is the phase that can
    see the leak. Detection stays in `pytest_runtest_makereport`, so nothing is
    raised from a hookwrapper and the INTERNALERROR that would cause cannot
    occur.
    """
    result = _run_child(tmp_path, LEAKY)
    combined = result.stdout + result.stderr
    assert "1 failed, 1 passed" in combined, combined
    # ONE line carrying both. Two independent `in combined` checks were
    # satisfiable by a `FAILED` on one line and the test's name on another, so
    # they did not prove what the criterion asks: that the short summary LINE
    # names the leaking test.
    assert any("FAILED" in line and "test_this_one_leaks_a_thread" in line
               for line in combined.splitlines()), combined
    assert "ERROR at teardown of test_this_one_leaks_a_thread" not in combined, combined


def test_a_joined_thread_is_not_reported_as_a_leak(tmp_path):
    result = _run_child(tmp_path, CLEAN, name="test_child_clean.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined


def test_a_registered_thread_survives_its_item_without_being_blamed(tmp_path):
    """The broader-scope contract is a REGISTRATION, not an inference.

    A module-scoped fixture's thread is created during the FIRST item's setup,
    is absent from that item's baseline, and legitimately survives that item's
    teardown. Registration is what makes that expressible; an inference against
    a pre-setup baseline would fail the fixture on its first use.
    """
    result = _run_child(tmp_path, REGISTERED, name="test_child_registered.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "2 passed" in combined


def test_the_verdict_move_distorts_no_other_outcome(tmp_path):
    """The hook sees every report in the run, not only a leaking one.

    A call-phase failure must keep its own verdict and gain no second one; a
    skip must stay a skip; an item that errored in SETUP has no passing call
    report and must not acquire a teardown pass; a clean item must pass exactly
    once.
    """
    result = _run_child(tmp_path, MIXED, name="test_child_mixed.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "1 failed, 1 passed, 1 skipped, 1 error" in combined, combined


def test_a_non_strict_xpass_is_still_reported_as_an_xpass(tmp_path):
    """An XPASS is a passing CALL report, so the suppression reached it.

    `@pytest.mark.xfail(strict=False)` on a test that passes produces a call
    report whose outcome is `passed` and which carries a `wasxfail` attribute.
    Suppressing that report's status hid the XPASS and made the teardown emit
    PASSED in its place, which silently rewrote a verdict this plugin has no
    business touching. The `wasxfail` check is what excludes it.
    """
    result = _run_child(tmp_path, XPASS, name="test_child_xpass.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 xpassed" in combined, combined
    assert "1 passed" not in combined, combined


def test_a_teardown_failure_this_plugin_did_not_raise_stays_an_error(tmp_path):
    """Reclassification is for this plugin's own verdicts, not every teardown.

    A fixture finalizer that raises fails the teardown report on its own.
    Reclassifying it as FAILED misreported pytest's ERROR taxonomy for a
    problem the isolation guard never detected, so the verdict is changed only
    for a node id the guard itself blamed.
    """
    result = _run_child(
        tmp_path, TEARDOWN_ERROR, name="test_child_teardown_error.py")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "1 error" in combined, combined
    assert "1 failed" not in combined, combined
    assert "FAILED" not in combined, combined
    assert "ERROR at teardown of test_this_one_tears_down_badly" in combined, (
        combined)
