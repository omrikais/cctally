"""M6 Task 6.1 (#268) — SIGUSR1 faulthandler dump in the dashboard entry.

The dashboard registers a SIGUSR1 handler that dumps all-thread tracebacks, so
any future sync-thread spin is self-diagnosing (`kill -USR1 <pid>`) without root
py-spy. Guarded for platforms lacking SIGUSR1 (Windows). Unit-tested in
isolation — the registration is a small pure helper so we don't have to launch
the server.
"""
from __future__ import annotations

import faulthandler
import signal

from conftest import load_script  # type: ignore


def test_register_faulthandler_sigusr1_registers_all_threads(monkeypatch):
    ns = load_script()
    calls = []

    def fake_register(sig, all_threads=False, **kw):
        calls.append((sig, all_threads))

    monkeypatch.setattr(faulthandler, "register", fake_register)
    ret = ns["_register_faulthandler_sigusr1"]()

    if hasattr(signal, "SIGUSR1"):
        assert ret is True
        assert calls == [(signal.SIGUSR1, True)], (
            "must register SIGUSR1 with all_threads=True for a full dump"
        )
    else:  # pragma: no cover - non-POSIX
        assert ret is False
        assert calls == []


def test_register_faulthandler_sigusr1_guarded_without_sigusr1(monkeypatch):
    """On a platform lacking SIGUSR1 the helper is a silent no-op (returns
    False), never raising — so the dashboard still starts on Windows."""
    ns = load_script()
    monkeypatch.delattr(signal, "SIGUSR1", raising=False)
    called = []
    monkeypatch.setattr(faulthandler, "register",
                        lambda *a, **k: called.append(a))
    assert ns["_register_faulthandler_sigusr1"]() is False
    assert called == []
