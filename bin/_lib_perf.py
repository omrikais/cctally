"""Opt-in backend phase-instrumentation collector (issue #276, Session A).

Stdlib-only. A thread-local nested-phase timing collector, gated on the
CCTALLY_PERF_TRACE env var. Near-noop when off: phase() returns a shared
_NULL_PHASE singleton — no allocation, no perf_counter, no stack push.

Two renderers sit on the same phase tree:
  * flush_stderr(root)  — CLI indented outline (stdout stays byte-identical).
  * stash_last(root)    — the dashboard freezes the completed tree (to_dict)
                          into a process-global slot for the loopback
                          /api/debug/backend endpoint to read.

This surface is a diagnostic, NOT a consumer contract: phase names, nesting,
and fields may change without a version bump.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time

_FALSEY = {"", "0", "false", "no", "off"}
_ENABLED = os.environ.get("CCTALLY_PERF_TRACE", "").strip().lower() not in _FALSEY


def enabled() -> bool:
    return _ENABLED


def set_enabled(value: bool) -> None:
    """Flip tracing immediately (tests, and ``apply_pending`` below).

    Runtime arming goes through ``request_enabled`` + ``apply_pending`` instead
    (#583 S1 §2.2), which defers the flip to a rebuild boundary. This entry
    point stays for tests.

    It deliberately does NOT clear a captured root arm: an already-open root is
    wholly traced or wholly untraced whatever the global does, and that is the
    property the per-root capture exists to give. Every caller that wants the
    new value to take effect calls ``reset_thread()`` afterwards, which is what
    both production root sites already do.
    """
    global _ENABLED
    _ENABLED = bool(value)


_tls = threading.local()


# ── runtime arming: an atomic mailbox, applied per root (#583 S1 §2.2) ──────
# `request_enabled` records the latest desired state and `apply_pending`
# consumes it, both under one lock performing a LATEST-VALUE EXCHANGE — read
# and clear inside one critical section. An unsynchronised test-and-clear loses
# a request arriving between the two steps, which is the race
# `_SnapshotRef.take_sync_request` already documents in this repository.
_UNSET = object()
_MAILBOX_LOCK = threading.Lock()
_PENDING = _UNSET


def request_enabled(value: bool) -> None:
    """Record the desired tracing state. Applies at the next boundary."""
    global _PENDING
    with _MAILBOX_LOCK:
        _PENDING = bool(value)


def apply_pending() -> bool:
    """Consume any pending request and apply it. Returns whether it changed.

    Called ONLY from `_make_run_sync_now_locked._locked`, after the cache
    connection closes and immediately before the authoritative build, so a
    request arriving mid-ingest cannot split one ingest across two tracing
    states. A2 partial builds never consume it.
    """
    global _PENDING
    with _MAILBOX_LOCK:
        value, _PENDING = _PENDING, _UNSET
    if value is _UNSET:
        return False
    changed = bool(value) != _ENABLED
    set_enabled(bool(value))
    return changed


def pending_state() -> "tuple[bool, bool]":
    """``(requested, applied)``. Equal once the request has taken effect."""
    with _MAILBOX_LOCK:
        pending = _PENDING
    applied = _ENABLED
    return (applied if pending is _UNSET else bool(pending), applied)


def applies_at() -> str:
    """When a pending request takes effect. Names the boundary, not a clock.

    Without this the operator cannot tell a request from its effect, and
    ``--trace off`` appears to succeed while tracing is still applied until the
    next authoritative build.
    """
    requested, applied = pending_state()
    return "next_authoritative_build" if requested != applied else "none"


def root_armed() -> "bool | None":
    """The tracing state this thread's open root captured, or None if no root
    scope is open on this thread."""
    return getattr(_tls, "root_armed", None)


@contextlib.contextmanager
def isolated_thread_state():
    """Run a nested build against empty, private phase state (#583 S1 §2.1).

    ``_make_a2_progress_cb`` calls ``build_partial()`` synchronously from
    inside ``sync_cache``'s still-open ``walk`` phase, and that build reaches
    ``_tui_build_snapshot_once``'s unconditional ``reset_thread()``. Each
    ``Phase`` retains its original list in ``Phase._stack`` while
    ``reset_thread()`` rebinds ``_tls.stack`` and clears ``_tls.root``, so
    without isolation the later phases attach to a different list and the outer
    phase closes into a detached or fragmented root.

    Saves the EXACT stack and root object references — by identity, because
    the open phases hold that same list — and restores them in ``finally``,
    including when the body raises.
    """
    saved_stack = getattr(_tls, "stack", None)
    saved_root = getattr(_tls, "root", None)
    saved_armed = getattr(_tls, "root_armed", None)
    _tls.stack = []
    _tls.root = None
    _tls.root_armed = None
    try:
        yield
    finally:
        _tls.stack = saved_stack
        _tls.root = saved_root
        _tls.root_armed = saved_armed


def _stack():
    s = getattr(_tls, "stack", None)
    if s is None:
        s = []
        _tls.stack = s
    return s


class Phase:
    __slots__ = ("name", "elapsed_ms", "count", "meta", "children", "_start", "_stack")

    def __init__(self, name, stack):
        self.name = name
        self.elapsed_ms = 0.0
        self.count = None
        self.meta = None
        self.children = []
        self._start = 0.0
        self._stack = stack

    def set_count(self, n):
        self.count = int(n)

    def set_meta(self, **kw):
        if self.meta is None:
            self.meta = {}
        self.meta.update(kw)

    def __enter__(self):
        self._start = time.perf_counter()
        self._stack.append(self)
        return self

    def __exit__(self, *exc):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        # Identity-aware unwind. If a nested phase leaked (its __exit__ was
        # skipped — e.g. an exception escaped a manually CM-bracketed region),
        # drop the leaked frames sitting above us so we never append a phase to
        # its OWN children (which would make to_dict() self-recurse) and never
        # strand a stack that hides the real root. Pop down to and including
        # self; if self is not on the stack (double __exit__, or an ancestor
        # already unwound us), do nothing.
        stack = self._stack
        if self not in stack:
            return False
        while stack and stack[-1] is not self:
            stack.pop()               # discard a leaked descendant frame
        stack.pop()                   # pop self
        if stack:
            stack[-1].children.append(self)
        else:
            _tls.root = self          # outermost phase closed -> the build root
            # The root scope ends with its root, so a later phase on this
            # thread reads the global again rather than a stale capture.
            _tls.root_armed = None
        return False

    def to_dict(self):
        d = {"name": self.name, "elapsed_ms": round(self.elapsed_ms, 3)}
        if self.count is not None:
            d["count"] = self.count
        if self.meta:
            d["meta"] = dict(self.meta)
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


class _NullPhase:
    """Shared no-op returned when tracing is off. No allocation per phase()."""

    def set_count(self, n):
        pass

    def set_meta(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_PHASE = _NullPhase()


def phase(name):
    # Per-root capture (#583 S1 §2.2). `_ENABLED` is process-global and HTTP
    # handlers open their own roots on their own threads, so a flip landing
    # mid-request would otherwise create traced phases beneath an untraced root
    # or drop later children from a traced one. A root captures the armed state
    # at its own creation and every phase under it consults that value; a
    # thread with no root scope open falls back to the global.
    armed = getattr(_tls, "root_armed", None)
    if not (_ENABLED if armed is None else armed):
        return _NULL_PHASE
    return Phase(name, _stack())


def current_root():
    return getattr(_tls, "root", None)


def reset_thread():
    """Start a fresh root scope on this thread, capturing the armed state.

    Every root in this tree is opened immediately after a `reset_thread()` —
    `_tui_build_snapshot_once` and the dashboard's `_perf_scope` are the two
    sites — so this is where a root's tracing decision is taken. The scope ends
    when the outermost phase closes, or when the next `reset_thread()` recaptures.

    KNOWN LIMIT, recorded rather than fixed. A DISARMED capture is not cleared
    by the closing phase, because no `Phase` is created to close: a thread that
    captures `False` and then never calls `reset_thread()` again keeps reading
    that stale value and stays untraced. Unreachable today — the dashboard
    threads per request and resets at the top of every build — but it would
    bite a thread-POOLED server, where a worker outlives many requests. Fixing
    it needs an explicit root scope that exists when tracing is off, not a
    smarter capture point.
    """
    _tls.stack = []
    _tls.root = None
    _tls.root_armed = _ENABLED


def flush_stderr(root):
    if root is None:
        return
    lines = []

    def walk(p, depth):
        indent = "  " * depth
        extra = ""
        if p.count is not None:
            extra += f"  (count={p.count})"
        if p.meta:
            extra += "  " + " ".join(f"{k}={v}" for k, v in p.meta.items())
        lines.append(f"{indent}{p.name}  {p.elapsed_ms:.1f}ms{extra}")
        for c in p.children:
            walk(c, depth + 1)

    walk(root, 0)
    sys.stderr.write("backend-perf:\n" + "\n".join(lines) + "\n")


# ── process-global last-completed-tree slot (dashboard -> endpoint) ──────────
# The writer freezes the tree with to_dict() then binds the module global in
# ONE statement; once bound the dict is never mutated (the next build binds a
# fresh dict). Assignment is atomic under the GIL, so the HTTP reader thread
# always sees a whole, immutable "last completed build".
_LAST_BACKEND_PERF = None


def stash_last(root, *, generation=None, generated_at=None):
    global _LAST_BACKEND_PERF
    if root is None:
        return
    _LAST_BACKEND_PERF = {
        "generated_at": generated_at,
        "generation": generation,
        "phases": root.to_dict(),
    }


def last_backend_perf():
    return _LAST_BACKEND_PERF
