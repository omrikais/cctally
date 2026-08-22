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

    The verdict lands as a teardown ERROR rather than a FAILED, because that
    is pytest's own taxonomy for a report whose `when` is `teardown` — the
    call phase really did pass. It is still attributed to the leaking item by
    node id and it still exits 1, which is what the acceptance criterion asks
    for.
    """
    result = _run_child(tmp_path, LEAKY)
    combined = result.stdout + result.stderr
    assert "2 passed, 1 error" in combined, combined
    assert "ERROR at teardown of test_this_one_leaks_a_thread" in combined, combined


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
