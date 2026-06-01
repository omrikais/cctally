"""Binding-semantics regression for cmd_diff (spec §5.C).

Proves cmd_diff's config-bridge + display-tz + color accessor paths resolve
through cctally's namespace (the `c.<name>` accessor in bin/_cctally_diff.py),
NOT honest imports — so monkeypatch.setattr(cctally, "X", …) is preserved after
the glue extraction. Patches only STAYS / accessor-routed symbols that cmd_diff
actually calls on the terminal path (resolve_display_tz / _resolve_color_enabled
/ _load_claude_config_for_args). The dedicated kernel calls (dk._parse_diff_window,
dk._build_diff_result, …) are honest `dk.`-imported by design and NOT patched.

Vacuity guard (Codex r2): cmd_diff returns at the `if args.emit_json:` gate
BEFORE `_resolve_color_enabled`. So the args MUST drive the normal terminal
path — NO --json, NO --debug-now — with valid same-length explicit-date-range
windows (which resolve without a subscription-week anchor, so an empty fake HOME
still renders rather than bailing at NoAnchorError). The test asserts each spy's
call-count >= 1.

Non-vacuity (Task 5 Step 3): RED if any patched symbol becomes an honest import
in the glue (temporarily honest-import a spied symbol, assert RED, revert, assert
GREEN — feedback_prove_test_non_vacuous).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    """Load bin/cctally under the canonical isolated data dir (#127).

    Via the shared ``load_isolated_cctally_module`` helper so the loader
    gets ``_cctally_core`` path redirection — without it, a cached
    ``_cctally_core`` made these tests read the real prod DB (#127).
    """
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _diff_args(mod):
    """Build a fully-formed diff Namespace via the live parser.

    Explicit date-range tokens (NOT `last-week`/`this-week`): subscription-week
    tokens raise NoAnchorError on an empty fake HOME and bail BEFORE the
    color/tz path. Date ranges resolve without an anchor, so cmd_diff renders
    the full terminal output and reaches every spied helper. Same-length
    windows avoid WindowMismatchError. NO --json / --debug-now.
    """
    return mod.build_parser().parse_args(
        ["diff", "--a", "2026-05-01..2026-05-07", "--b", "2026-05-08..2026-05-14"]
    )


def test_cmd_diff_resolves_accessors_through_cctally_ns(cctally_mod, monkeypatch):
    """cmd_diff (terminal path): patch the accessor-routed symbols cmd_diff
    actually calls and assert each fires — proving the glue reaches them via
    c.<name>, not honest import."""
    c = cctally_mod
    calls = {
        "resolve_display_tz": 0,
        "_resolve_color_enabled": 0,
        "_load_claude_config_for_args": 0,
    }
    real = {k: getattr(c, k) for k in calls}

    def mk(name):
        def spy(*a, **k):
            calls[name] += 1
            return real[name](*a, **k)
        return spy

    for name in calls:
        monkeypatch.setattr(c, name, mk(name))

    args = _diff_args(c)
    rc = c.cmd_diff(args)
    assert rc == 0, f"cmd_diff returned {rc} (expected terminal-path success)"

    for name in calls:
        assert calls[name] >= 1, (
            f"{name} not reached via the cctally ns (terminal-path cmd_diff)"
        )
