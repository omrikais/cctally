"""Stdlib-logging chokepoint (issue #279 S2, F2).

One place decides backend verbosity: ``CCTALLY_DEBUG`` gates DEBUG-level
stderr output (tracebacks via ``exc_info=True``); the default posture is
WARNING+ only, so adopters like the dashboard ``log_error`` surface
errors without any env flag. Pure leaf — no sibling imports (mirrors
``_lib_perf``'s env handling). The falsey set matches
``_cctally_core._truthy_env`` EXACTLY (pinned by tests/test_lib_log.py,
not by import coupling): unset/empty/``0``/``false``/``no`` are off,
anything else is on. Deliberately NOT ``_lib_perf._FALSEY`` (which also
treats ``off`` as false) — #279 S2 Codex review P2-5.

Optional file sink: ``CCTALLY_DEBUG_LOG=<path>`` attaches an append-mode
FileHandler at that explicit path (no LOG_DIR default — the module stays
leaf-pure). Handler-attach failures degrade silently to stderr-only.

The stderr handler resolves ``sys.stderr`` at EMIT time (late binding) so
pytest capsys replacement and hook-tick's dup2 stdio redirect both see
records; a handler bound at configure time would write to a dead stream.

The configure-once latch lives on ``logging.getLogger("cctally").handlers``
(process-global), NOT on a module global — a SourceFileLoader re-import of
this module must not attach a second handler and double-print.
"""
from __future__ import annotations

import logging
import os
import sys

_FALSEY = ("", "0", "false", "no")


def _env_truthy(name: str) -> bool:
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in _FALSEY


_DEBUG = _env_truthy("CCTALLY_DEBUG")


def debug_enabled() -> bool:
    return _DEBUG


def set_debug(value: bool) -> None:
    """Flip debug at runtime (tests). Re-levels an already-configured
    logger so a latch created before the flip follows it."""
    global _DEBUG
    _DEBUG = bool(value)
    root = logging.getLogger("cctally")
    if root.handlers:
        root.setLevel(logging.DEBUG if _DEBUG else logging.WARNING)


class _LateStderrHandler(logging.StreamHandler):
    """StreamHandler that resolves sys.stderr at emit time."""

    def __init__(self):
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):  # StreamHandler.__init__ assigns; ignore.
        pass


_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def get_logger(name: str = "cctally") -> logging.Logger:
    """Return the chokepoint logger. Pass the bare suffix:
    ``get_logger("dashboard")`` -> logger ``cctally.dashboard``."""
    root = logging.getLogger("cctally")
    if not root.handlers:  # configure-once latch (process-global)
        formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
        handler = _LateStderrHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
        sink = (os.environ.get("CCTALLY_DEBUG_LOG") or "").strip()
        if sink:
            try:
                fh = logging.FileHandler(sink, encoding="utf-8")
                fh.setFormatter(formatter)
                root.addHandler(fh)
            except OSError:
                pass  # degrade to stderr-only
        root.setLevel(logging.DEBUG if _DEBUG else logging.WARNING)
        root.propagate = False
    if name in ("", "cctally"):
        return root
    return logging.getLogger(f"cctally.{name}")
