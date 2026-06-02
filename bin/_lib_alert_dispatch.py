"""Pure kernel: cross-platform alert dispatch decisions + severity->urgency.

resolve_notifier() picks the active notifier; build_command() returns the exact
arg-list to spawn (osascript / notify-send / a config-driven command_template),
or None ("no popup; log + dashboard only"). NO I/O at import time, and the
decision is parameterized on `platform` + `which_on_path` so every OS branch is
unit-testable from any host. bin/_cctally_alerts.py is the I/O glue: it injects
the real sys.platform / shutil.which and spawns the result with shell=False.

Trust model (spec Q3): alerts.command_template is *trusted local command
execution* (the user owns config.json). shell=False + the arg-list form block
alert-text shell-injection (a week label with $(...) or ; is one literal arg);
the native notify-send path adds a `--` end-of-options delimiter against
option-injection.

Stdlib-only. bin/cctally re-exports the public symbols.
Spec: docs/superpowers/specs/2026-06-02-alerts-dispatch-severity-seams-design.md
"""
from __future__ import annotations

import importlib.util as _ilu
import pathlib
import sys

# notify-send urgency tokens keyed by the 3-tier severity from
# _lib_alert_axes.severity_for.
_SEVERITY_URGENCY = {"info": "low", "warn": "normal", "critical": "critical"}

# The documented command_template substitution tokens (one-pass, no re-scan).
_TEMPLATE_TOKENS = frozenset(
    {"title", "subtitle", "body", "severity", "urgency", "axis", "threshold", "metric"}
)


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


def severity_to_urgency(severity: str) -> str:
    """Map a 3-tier severity to a notify-send urgency token."""
    return _SEVERITY_URGENCY.get(severity, "normal")


def resolve_notifier(cfg: dict, *, platform: str, which_on_path) -> str:
    """Return the effective notifier id for this host + validated alerts cfg.

    `platform` is a sys.platform-style string; `which_on_path` is a callable
    name -> bool. An explicitly-selected native notifier that is unavailable on
    this host downgrades to 'none' (rather than being spawned and failing).
    """
    selector = cfg.get("notifier", "auto")
    template = cfg.get("command_template")
    is_darwin = platform == "darwin"
    is_linux = platform.startswith("linux")

    if selector == "osascript":
        return "osascript" if is_darwin else "none"
    if selector == "notify-send":
        return "notify-send" if (is_linux and which_on_path("notify-send")) else "none"
    if selector == "command":
        return "command" if template else "none"
    if selector == "none":
        return "none"
    # auto
    if template:
        return "command"
    if is_darwin:
        return "osascript"
    if is_linux and which_on_path("notify-send"):
        return "notify-send"
    return "none"


def _substitute_tokens(arg: str, values: dict) -> str:
    """One-pass left-to-right substitution: replace only the documented
    {tokens}; leave every other character (including stray/unmatched braces)
    literal; substituted values are NOT re-scanned. Missing keys -> ""."""
    out = []
    i, n = 0, len(arg)
    while i < n:
        ch = arg[i]
        if ch == "{":
            close = arg.find("}", i + 1)
            if close != -1 and arg[i + 1:close] in _TEMPLATE_TOKENS:
                out.append(str(values.get(arg[i + 1:close], "")))
                i = close + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def build_command(
    notifier: str,
    *,
    title: str,
    subtitle: str,
    body: str,
    severity: str,
    urgency: str,
    payload: dict,
    command_template,
):
    """Return the arg-list to spawn for `notifier`, or None ('no popup')."""
    if notifier == "osascript":
        esc = _load_lib("_lib_alerts_payload")._escape_applescript_string
        script = (
            f'display notification "{esc(body)}"'
            f' with title "{esc(title)}"'
            f' subtitle "{esc(subtitle)}"'
        )
        return ["osascript", "-e", script]
    if notifier == "notify-send":
        folded = f"{subtitle}\n{body}" if subtitle.strip() else body
        return ["notify-send", "-u", urgency, "--", title, folded]
    if notifier == "command":
        if not command_template:
            return None
        # A payload key present-but-None (e.g. a weekly payload's metric=None)
        # substitutes as "" — same surface as an absent key.
        def _pv(key):
            v = payload.get(key)
            return "" if v is None else v

        values = {
            "title": title, "subtitle": subtitle, "body": body,
            "severity": severity, "urgency": urgency,
            "axis": _pv("axis"),
            "threshold": _pv("threshold"),
            "metric": _pv("metric"),
        }
        return [_substitute_tokens(a, values) for a in command_template]
    return None  # "none" or unknown -> no popup
