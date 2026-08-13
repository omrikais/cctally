"""Child-side bootstrap for the #529 S4 write detector (spec section 5.3).

The `subprocess.Popen` interceptor in each pytest worker prepends the directory
holding this file to the child's PYTHONPATH, so `site` imports it before the
child's own first line. It installs the same audit hook, emits the process-id
and token handshake the parent's teardown checks for, and installs the
interceptor again so the child's own descendants are covered.

It contains no policy of its own. The protected roots arrive in
CCTALLY_ISOLATION_ROOTS, already classified by the parent against the registered
per-test scratch roots; re-deriving them here would lose that decision and would
guard the wrong directory for a child whose environment names its own APP_DIR.

Nothing here prints, and nothing here raises into the child. It does not fail
silently either: a swallowed exception leaves a child that is indistinguishable
from one where this file never ran, so the failure is appended to the ledger the
envelope names before the exception is dropped.
"""
import os
import sys


def _locked_append(ledger, line):
    """Append under the SAME `<ledger>.lock` the kernel's own appender takes.

    `drop_node_from_ledger` reads the ledger and rewrites it as one locked
    critical section, so a row appended without that lock inside the window is
    erased by the rewrite. This file cannot import the kernel to reuse its lock
    -- the failure being reported may be that very import -- but it can take the
    same lock by hand, and it must, because this is the loud replacement for a
    failure that was previously silent.
    """
    lock_fd = None
    try:
        import fcntl

        lock_fd = os.open(ledger + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except Exception:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            lock_fd = None
    try:
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    finally:
        if lock_fd is not None:
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def _report_failure(exc):
    """Append the failure to the parent's ledger. Never raises into the child."""
    ledger = os.environ.get("CCTALLY_ISOLATION_LEDGER")
    if not ledger:
        return
    try:
        import json
        import time

        payload = {
            "node_id": os.environ.get("CCTALLY_ISOLATION_NODE", "") or "<no active test>",
            "worker_id": os.environ.get("PYTEST_XDIST_WORKER") or "main",
            "pid": os.getpid(),
            "event": "bootstrap",
            "path": f"{type(exc).__name__}: {exc}",
            "kind": "bootstrap",
            "at": time.time(),
        }
        line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        _locked_append(ledger, line)
    except Exception:
        pass


def _install():
    ledger = os.environ.get("CCTALLY_ISOLATION_LEDGER")
    if not ledger:
        return
    kernel_dir = os.environ.get("CCTALLY_ISOLATION_KERNEL")
    if kernel_dir and kernel_dir not in sys.path:
        # Appended, never inserted: this runs before the child's own first line,
        # and prepending bin/ would let it shadow a same-named module the child
        # meant to import from somewhere else.
        sys.path.append(kernel_dir)
    import _lib_test_isolation

    _lib_test_isolation.install_child_from_env(os.environ)


def _run():
    try:
        _install()
    except Exception as exc:  # noqa: BLE001 -- must never raise into the child
        _report_failure(exc)


_run()
