"""Alert dispatch I/O + `cctally alerts test` entry point.

Lazy I/O sibling: holds the two helpers that perform real-world side
effects for the threshold-actions feature, plus the test-entry command:

- `_alerts_log_path()` — resolve / mkdir the `alerts.log` path under
  `LOG_DIR`. Pure path-derivation that touches the filesystem (creates
  the parent dir) on every call.
- `_dispatch_alert_notification(payload, *, popen_factory, mode, tz)` —
  spawn `osascript` (best-effort, non-blocking) to display a macOS
  Notification Center popup, then append a single tab-delimited line to
  `alerts.log` with the terminal status. Fire-and-forget contract; never
  raises.
- `cmd_alerts_test(args)` — synthetic-payload entry point exposed via
  `cctally alerts test`. Builds a payload through the same
  `_build_alert_payload_*` helpers production uses, routes through
  `_dispatch_alert_notification` with `mode="test"`, and reports the
  outcome via stdout / exit code.

The pure payload primitives (`_alert_text_weekly`,
`_alert_text_five_hour`, `_escape_applescript_string`,
`_build_alert_payload_weekly`, `_build_alert_payload_five_hour`) live
in `bin/_lib_alerts_payload.py` (Phase A extraction); this module
imports them directly via `_load_lib`, which keeps the dispatch path
free of an extra bounce through cctally's re-exports.

bin/cctally back-references via `_cctally()` (spec §5.5 pattern, same
as `bin/_cctally_setup.py`):
- `LOG_DIR` — base dir under which `alerts.log` lives (subject to
  HOME-redirection by test fixtures via `monkeypatch.setitem(ns,
  "LOG_DIR", ...)`).
- `now_utc_iso` — single timestamp source used for both the log-line
  timestamp and the synthetic test payload's `crossed_at_utc`.

bin/cctally re-exports every public symbol below so the
`bin/cctally-alerts-dispatch-test` harness (SourceFileLoader-based,
attribute access via `m._dispatch_alert_notification(...)`) and the
existing internal call sites in `cmd_record_usage` + the dashboard
alerts/test handler resolve unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util as _ilu
import os
import pathlib
import subprocess
import sys


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_alerts_payload = _load_lib("_lib_alerts_payload")
_alert_text_weekly = _lib_alerts_payload._alert_text_weekly
_alert_text_five_hour = _lib_alerts_payload._alert_text_five_hour
_escape_applescript_string = _lib_alerts_payload._escape_applescript_string
_build_alert_payload_weekly = _lib_alerts_payload._build_alert_payload_weekly
_build_alert_payload_five_hour = _lib_alerts_payload._build_alert_payload_five_hour


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core. `LOG_DIR` stays on the _cctally() accessor
# per Q1=B (path constants propagate via monkeypatch.setitem against the
# cctally namespace).
from _cctally_core import now_utc_iso


def _alerts_log_path() -> "pathlib.Path":
    """Return ``~/.local/share/cctally/logs/alerts.log`` (parent dirs created).

    Resolves through the same ``APP_DIR`` / ``LOG_DIR`` derived at module
    import time from ``Path.home()``, so a HOME override before import (the
    pattern used elsewhere in this codebase — e.g. ``cctally-config-test``)
    transparently relocates the log without a separate env-var convention.
    """
    log_dir = _cctally().LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "alerts.log"


def _dispatch_alert_notification(
    payload: dict,
    *,
    popen_factory=subprocess.Popen,
    mode: str = "real",
    tz: "object | None" = None,
) -> str:
    """Spawn osascript to display a macOS notification (non-blocking, best-effort).

    Returns ``"queued"`` on successful Popen, ``"spawn_error: <ExcType>: <msg>"``
    on failure. Writes EXACTLY ONE line to ``alerts.log`` with the terminal
    status (no contradictory pre-/post-Popen log pair). Never raises:
    Popen-spawn failures and log-write failures are both swallowed so the
    dispatch contract stays independent of the OS / FS state.

    Production callers ignore the return value (fire-and-forget); test
    callers assert on it via an injected ``popen_factory``.

    Integration-harness escape hatch: when ``popen_factory`` is left as
    its default (``subprocess.Popen``) AND the env var
    ``CCTALLY_TEST_POPEN_FACTORY=raise_filenotfound`` is set, swap in a
    factory that raises ``FileNotFoundError("no osascript")``. Used by
    ``bin/cctally-alerts-test`` to exercise the spawn-error branch
    end-to-end (subprocess invocation of ``cctally record-usage`` —
    direct kwargs-injection isn't reachable through the CLI). Only the
    one canonical token is honored; unknown values fall through to real
    Popen so a typo can't silently neuter dispatch in production.
    """
    if (
        popen_factory is subprocess.Popen
        and os.environ.get("CCTALLY_TEST_POPEN_FACTORY") == "raise_filenotfound"
    ):
        def _raise_filenotfound(*_args, **_kwargs):
            raise FileNotFoundError("no osascript")
        popen_factory = _raise_filenotfound

    axis = payload["axis"]
    if axis == "weekly":
        title, subtitle, body = _alert_text_weekly(payload, tz)
    elif axis == "five_hour":
        title, subtitle, body = _alert_text_five_hour(payload, tz)
    else:
        title, subtitle, body = (
            "cctally - alert",
            "",
            f"axis={axis} threshold={payload.get('threshold')}",
        )

    script = (
        f'display notification "{_escape_applescript_string(body)}"'
        f' with title "{_escape_applescript_string(title)}"'
        f' subtitle "{_escape_applescript_string(subtitle)}"'
    )

    status: str
    try:
        popen_factory(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        status = "queued"
    except (FileNotFoundError, PermissionError, OSError) as exc:
        status = f"spawn_error: {exc.__class__.__name__}: {exc}"

    # SINGLE log line per dispatch attempt (Codex P1#2 fix: no
    # contradictory "queued" + "spawn_error" pair for the same call).
    try:
        log_path = _alerts_log_path()
        ctx = payload.get("context") or {}
        window_key = (
            ctx.get("week_start_date")
            or ctx.get("five_hour_window_key")
            or ""
        )
        line = (
            f"{now_utc_iso()}\t{axis}\t{payload.get('threshold')}\t{window_key}"
            f"\t{mode}\t{status}\n"
        )
        with open(log_path, "a") as f:
            f.write(line)
    except OSError:
        pass  # log-write failures must not affect dispatch contract

    return status


def cmd_alerts_test(args: argparse.Namespace) -> int:
    """Send a synthetic test alert through the dispatch pipeline.

    Builds a synthetic payload via the same ``_build_alert_payload_*``
    helpers production uses, then routes through ``_dispatch_alert_notification``
    with ``mode="test"`` so the alerts.log line carries the ``test``
    discriminator (5th tab-delimited field) — distinguishes from real
    threshold-crossing alerts written by ``cmd_record_usage``.

    No DB writes: this path exists purely to validate end-to-end
    osascript + log behavior. Exit codes:
      0  alert was queued (Popen succeeded)
      1  osascript missing on this host (FileNotFoundError)
      2  --threshold out of [1, 100] range
      3  other spawn error (PermissionError, OSError, ...)
    """
    axis = "weekly" if args.axis == "weekly" else "five_hour"
    threshold = int(args.threshold)
    if not (1 <= threshold <= 100):
        print(
            f"cctally: --threshold must be in [1, 100], got {threshold}",
            file=sys.stderr,
        )
        return 2
    if axis == "weekly":
        payload = _build_alert_payload_weekly(
            threshold=threshold,
            crossed_at_utc=now_utc_iso(),
            week_start_date=dt.date.today().isoformat(),
            cumulative_cost_usd=1.23,
            dollars_per_percent=0.01,
        )
    else:
        payload = _build_alert_payload_five_hour(
            threshold=threshold,
            crossed_at_utc=now_utc_iso(),
            five_hour_window_key=int(dt.datetime.now(dt.timezone.utc).timestamp()),
            block_start_at=now_utc_iso(),
            block_cost_usd=1.23,
            primary_model="claude-sonnet-4-6",
        )
    status = _dispatch_alert_notification(payload, mode="test")
    if status == "queued":
        print("Test alert dispatched (mode=test). Check Notification Center.")
        return 0
    if "FileNotFoundError" in status:
        print(f"cctally: {status}", file=sys.stderr)
        return 1
    print(f"cctally: {status}", file=sys.stderr)
    return 3
