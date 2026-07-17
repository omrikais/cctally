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

The established pure payload primitives (`_alert_text_weekly`,
`_alert_text_five_hour`, `_escape_applescript_string`,
`_build_alert_payload_weekly`, `_build_alert_payload_five_hour`) live
in `bin/_lib_alerts_payload.py` (Phase A extraction); this module
imports them directly via `_load_lib`, which keeps the dispatch path
free of an extra bounce through cctally's re-exports. The S2 quota payload is
defined below beside its provider-neutral dispatch text because this stage does
not yet expose a dashboard payload consumer.

Kernel reads from `bin/_cctally_core` (call-time module-attribute access):
- `LOG_DIR` — base dir under which `alerts.log` lives. Promoted to
  `_cctally_core` 2026-05-22 (#84); test fixtures redirect via
  `monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp)` (or the
  conftest `redirect_paths()` helper).
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
import os
import pathlib
import shutil
import subprocess
import sys

import _cctally_core


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_alerts_payload = _load_lib("_lib_alerts_payload")
_alert_text_weekly = _lib_alerts_payload._alert_text_weekly
_alert_text_five_hour = _lib_alerts_payload._alert_text_five_hour
_alert_text_budget = _lib_alerts_payload._alert_text_budget
_alert_text_project_budget = _lib_alerts_payload._alert_text_project_budget
_alert_text_codex_budget = _lib_alerts_payload._alert_text_codex_budget
_alert_text_projected = _lib_alerts_payload._alert_text_projected
_escape_applescript_string = _lib_alerts_payload._escape_applescript_string
_build_alert_payload_weekly = _lib_alerts_payload._build_alert_payload_weekly
_build_alert_payload_five_hour = _lib_alerts_payload._build_alert_payload_five_hour
_build_alert_payload_budget = _lib_alerts_payload._build_alert_payload_budget
_build_alert_payload_project_budget = _lib_alerts_payload._build_alert_payload_project_budget
_build_alert_payload_codex_budget = _lib_alerts_payload._build_alert_payload_codex_budget
_build_alert_payload_projected = _lib_alerts_payload._build_alert_payload_projected


def _build_alert_payload_quota(
    *, source: str, source_root_key: str, logical_limit_key: str,
    observed_slot: str, window_minutes: int, resets_at_utc: str,
    threshold: int, kind: str, crossed_at_utc: str,
    qualifying_percent: float | None, projected_percent: float | None,
) -> dict:
    """Build the provider-neutral durable quota alert payload."""
    context = {
        "source": str(source), "source_root_key": str(source_root_key),
        "logical_limit_key": str(logical_limit_key), "observed_slot": str(observed_slot),
        "window_minutes": int(window_minutes), "resets_at_utc": str(resets_at_utc),
        "kind": str(kind),
        "qualifying_percent": (
            None if qualifying_percent is None else float(qualifying_percent)
        ),
        "projected_percent": (
            None if projected_percent is None else float(projected_percent)
        ),
    }
    return {
        "id": "quota:{source}:{root}:{limit}:{slot}:{minutes}:{reset}:{threshold}".format(
            source=source, root=source_root_key, limit=logical_limit_key,
            slot=observed_slot, minutes=int(window_minutes), reset=resets_at_utc,
            threshold=int(threshold),
        ),
        "axis": "quota", "threshold": int(threshold), "kind": str(kind),
        "crossed_at": crossed_at_utc, "alerted_at": crossed_at_utc,
        **context, "context": context,
    }


def _alert_text_quota(payload: dict, _tz) -> tuple[str, str, str]:
    """Render provider-native quota wording without Claude-window aliases."""
    context = payload.get("context") or {}
    threshold = int(payload["threshold"])
    source = context.get("source") or payload.get("source") or "provider"
    slot = context.get("observed_slot") or payload.get("observed_slot") or "quota"
    minutes = context.get("window_minutes") or payload.get("window_minutes")
    title = f"cctally - {source} quota {threshold}% reached"
    subtitle = f"{slot} · {minutes}m window" if minutes is not None else str(slot)
    kind = context.get("kind") or payload.get("kind") or "actual"
    if kind == "projected":
        body = f"Projected {float(context.get('projected_percent') or 0):.0f}% by reset"
    else:
        body = f"Actual usage {float(context.get('qualifying_percent') or 0):.0f}%"
    return title, subtitle, body

# Phase B: severity policy + the cross-platform dispatch kernel. The kernel is
# pure (parameterized on platform + which_on_path); this module is the I/O glue
# that injects the real sys.platform / shutil.which and spawns with shell=False.
_lib_alert_axes = _load_lib("_lib_alert_axes")
severity_for = _lib_alert_axes.severity_for
_lib_alert_dispatch = _load_lib("_lib_alert_dispatch")
resolve_notifier = _lib_alert_dispatch.resolve_notifier
build_command = _lib_alert_dispatch.build_command
severity_to_urgency = _lib_alert_dispatch.severity_to_urgency


# `load_config` STAYS a shim that bounces through cctally's namespace (mirrors
# bin/_cctally_record.py): production monkeypatches `cctally.load_config`, and
# the dispatch tests patch this module-level name directly. Its natural home is
# _cctally_config; a direct import would silently bypass those patches.
def load_config(*args, **kwargs):
    return sys.modules["cctally"].load_config(*args, **kwargs)


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17 §3.3: kernel symbols import from _cctally_core.
# LOG_DIR was promoted to _cctally_core 2026-05-22 (#84) and is read
# via call-time module-attribute access (this sibling no longer needs
# the historical _cctally() accessor).
from _cctally_core import now_utc_iso


def _alerts_log_path() -> "pathlib.Path":
    """Return ``~/.local/share/cctally/logs/alerts.log`` (parent dirs created).

    Reads ``LOG_DIR`` from ``_cctally_core`` at call time. Tests patch via
    ``monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp)`` (or the
    conftest ``redirect_paths()`` helper).
    """
    log_dir = _cctally_core.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "alerts.log"


def _dispatch_alert_notification(
    payload: dict,
    *,
    popen_factory=subprocess.Popen,
    mode: str = "real",
    tz: "object | None" = None,
    platform: "str | None" = None,
    which_on_path=None,
) -> str:
    """Dispatch a notification for a crossed threshold (non-blocking, best-effort).

    Picks the active notifier (osascript / notify-send / a config-driven
    command_template / none) via the pure ``_lib_alert_dispatch`` kernel, builds
    its exact arg-list, and spawns it with ``shell=False``. Returns one of:
      ``"queued"``                  Popen succeeded
      ``"no_notifier:none"``        auto/none resolved to no popup on this host
      ``"no_notifier:unavailable"`` an explicit osascript/notify-send is missing
      ``"spawn_error: <ExcType>: <msg>"`` Popen raised
    Writes EXACTLY ONE line to ``alerts.log`` with the terminal status PLUS the
    crossing's 3-tier severity as a trailing column. Never raises: the config
    read, Popen-spawn failures, and log-write failures are all swallowed so the
    dispatch contract stays independent of the OS / FS / user-config state.

    ``platform`` (sys.platform-style) and ``which_on_path`` (name -> bool) are
    injectable so every OS branch + the no-notifier paths are testable from any
    host; both default to the real ``sys.platform`` / ``shutil.which``.

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
    elif axis == "budget":
        title, subtitle, body = _alert_text_budget(payload, tz)
    elif axis == "project_budget":
        title, subtitle, body = _alert_text_project_budget(payload, tz)
    elif axis == "codex_budget":
        title, subtitle, body = _alert_text_codex_budget(payload, tz)
    elif axis == "projected":
        title, subtitle, body = _alert_text_projected(payload, tz)
    elif axis == "quota":
        title, subtitle, body = _alert_text_quota(payload, tz)
    else:
        title, subtitle, body = (
            "cctally - alert",
            "",
            f"axis={axis} threshold={payload.get('threshold')}",
        )

    # Severity (3-tier) drives both the notify-send urgency token and the
    # trailing log column. A missing threshold (defensive — shouldn't happen for
    # a real crossing) floors at "info".
    threshold = payload.get("threshold")
    try:
        severity = severity_for(int(threshold)) if threshold is not None else "info"
    except (TypeError, ValueError):
        severity = "info"
    urgency = severity_to_urgency(severity)

    if platform is None:
        platform = sys.platform
    if which_on_path is None:
        which_on_path = lambda name: shutil.which(name) is not None

    # Guarded so a malformed user config (or a load_config raise) never breaks
    # the never-raise contract: fall back to auto-detect / no custom command.
    try:
        alerts_cfg = _cctally_core._get_alerts_config(load_config())
    except Exception:
        alerts_cfg = {"notifier": "auto", "command_template": None}

    notifier = resolve_notifier(
        alerts_cfg, platform=platform, which_on_path=which_on_path
    )
    args = build_command(
        notifier,
        title=title,
        subtitle=subtitle,
        body=body,
        severity=severity,
        urgency=urgency,
        payload=payload,
        command_template=alerts_cfg.get("command_template"),
    )

    status: str
    if args is None:
        # 'none' (auto resolved to no popup, or notifier='none') vs an
        # explicitly-selected native backend that is unavailable on this host.
        selector = alerts_cfg.get("notifier", "auto")
        reason = "unavailable" if selector in ("osascript", "notify-send") else "none"
        status = f"no_notifier:{reason}"
    else:
        try:
            popen_factory(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            status = "queued"
        except (FileNotFoundError, PermissionError, OSError) as exc:
            status = f"spawn_error: {exc.__class__.__name__}: {exc}"

    # SINGLE log line per dispatch attempt (Codex P1#2 fix: no contradictory
    # "queued" + "spawn_error" pair). Severity is appended as the 7th column.
    try:
        log_path = _alerts_log_path()
        ctx = payload.get("context") or {}
        window_key = (
            ctx.get("week_start_date")
            or ctx.get("five_hour_window_key")
            or ctx.get("week_start_at")
            or ctx.get("period_start_at")
            or ""
        )
        line = (
            f"{now_utc_iso()}\t{axis}\t{payload.get('threshold')}\t{window_key}"
            f"\t{mode}\t{status}\t{severity}\n"
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
    if args.axis == "weekly":
        axis = "weekly"
    elif args.axis == "budget":
        axis = "budget"
    elif args.axis == "project-budget":
        axis = "project_budget"
    elif args.axis == "codex-budget":
        axis = "codex_budget"
    elif args.axis == "projected":
        axis = "projected"
    else:
        axis = "five_hour"
    threshold = int(args.threshold)
    # --threshold range stays [1, 100] (F5): the cap is axis-uniform with the
    # existing weekly/5h thresholds. Over-budget tiers (>100%) are a v2
    # deferral, not an oversight — see spec §2 (F5).
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
    elif axis == "budget":
        # Synthetic budget payload — NO DB writes (test/real divergence
        # contract). spent scaled to the threshold so the body line reads
        # plausibly (e.g. 100% → $300 of $300).
        payload = _build_alert_payload_budget(
            threshold=threshold,
            crossed_at_utc=now_utc_iso(),
            week_start_at=dt.date.today().isoformat(),
            budget_usd=300.0,
            spent_usd=300.0 * threshold / 100.0,
            consumption_pct=float(threshold),
        )
    elif axis == "project_budget":
        # Synthetic per-project budget payload — NO DB writes (test/real
        # divergence contract), NO real budget.projects entry required. A small
        # $25 budget at $26 spent (104%) reads plausibly regardless of the
        # --threshold (the body line shows the at-crossing snapshot the dashboard
        # would render).
        payload = _build_alert_payload_project_budget(
            threshold=threshold,
            crossed_at_utc=now_utc_iso(),
            week_start_at=dt.date.today().isoformat(),
            project="example-project",
            project_key="/example/repos/example-project",
            budget_usd=25.0,
            spent_usd=26.0,
            consumption_pct=104.0,
        )
    elif axis == "codex_budget":
        # Synthetic Codex budget payload — NO DB writes (test/real divergence
        # contract), NO real budget.codex entry required. A $200 calendar-month
        # budget reads plausibly; spent scaled to the threshold so the body line
        # reads as the at-crossing snapshot the dashboard would render.
        payload = _build_alert_payload_codex_budget(
            threshold=threshold,
            crossed_at_utc=now_utc_iso(),
            period_start_at=dt.date.today().replace(day=1).isoformat(),
            period="calendar-month",
            budget_usd=200.0,
            spent_usd=200.0 * threshold / 100.0,
            consumption_pct=float(threshold),
        )
    elif axis == "projected":
        # Synthetic projected-pace payload — NO DB writes (test/real divergence
        # contract). The metric discriminator picks the wiring; projected_value
        # is the threshold's denominator-relative value (so the body reads
        # plausibly, e.g. weekly 100% → "~100% of cap", budget 100% → "$300 of
        # $300"). denominator is the at-crossing target the row would carry
        # (Codex P0-4): 100.0 for weekly_pct, $300 for budget_usd, $200 for
        # codex_budget_usd (matching the codex_budget axis test-alert budget).
        metric = getattr(args, "metric", "weekly_pct")
        if metric == "budget_usd":
            denominator = 300.0
            projected_value = 300.0 * threshold / 100.0
        elif metric == "codex_budget_usd":
            denominator = 200.0
            projected_value = 200.0 * threshold / 100.0
        else:  # weekly_pct
            denominator = 100.0
            projected_value = float(threshold)
        payload = _build_alert_payload_projected(
            metric=metric,
            threshold=threshold,
            projected_value=projected_value,
            denominator=denominator,
            week_start_at=dt.date.today().isoformat(),
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
    # Resolve and report the active notifier for display BEFORE dispatch — the
    # config read is guarded the same way `_dispatch_alert_notification` guards
    # its own (so a malformed config never crashes `alerts test`). This is
    # purely informational; the dispatch path re-resolves independently.
    try:
        alerts_cfg = _cctally_core._get_alerts_config(load_config())
    except Exception:
        alerts_cfg = {"notifier": "auto", "command_template": None}
    notifier = resolve_notifier(
        alerts_cfg,
        platform=sys.platform,
        which_on_path=lambda name: shutil.which(name) is not None,
    )
    print(f"notifier: {notifier}")
    status = _dispatch_alert_notification(payload, mode="test")
    if status == "queued":
        print("Test alert dispatched (mode=test). Check Notification Center.")
        return 0
    if "FileNotFoundError" in status:
        print(f"cctally: {status}", file=sys.stderr)
        return 1
    print(f"cctally: {status}", file=sys.stderr)
    return 3
