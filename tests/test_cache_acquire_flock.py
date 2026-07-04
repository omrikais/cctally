"""_acquire_cache_flock: timeout=None is a single non-blocking attempt;
timeout>0 retries until the deadline; returns True iff the lock is held."""
from __future__ import annotations
import fcntl, time
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def ns_paths(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_non_blocking_returns_false_when_held(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        me = open(lock_path, "w")
        assert acquire(me, timeout=None) is False
        me.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_timeout_gives_up_after_deadline(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        me = open(lock_path, "w")
        t0 = time.monotonic()
        assert acquire(me, timeout=0.5) is False
        assert time.monotonic() - t0 >= 0.4
        me.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_acquires_when_free(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    me = open(lock_path, "w")
    assert acquire(me, timeout=None) is True
    fcntl.flock(me, fcntl.LOCK_UN); me.close()
