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

import os
import sys
import threading
import time

_FALSEY = {"", "0", "false", "no", "off"}
_ENABLED = os.environ.get("CCTALLY_PERF_TRACE", "").strip().lower() not in _FALSEY


def enabled() -> bool:
    return _ENABLED


def set_enabled(value: bool) -> None:
    """Flip tracing at runtime (tests; not used by the dashboard, which reads
    the env at import time)."""
    global _ENABLED
    _ENABLED = bool(value)


_tls = threading.local()


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
    if not _ENABLED:
        return _NULL_PHASE
    return Phase(name, _stack())


def current_root():
    return getattr(_tls, "root", None)


def reset_thread():
    _tls.stack = []
    _tls.root = None


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
