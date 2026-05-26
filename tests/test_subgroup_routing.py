"""Structural routing checks for the `cctally claude` / `cctally codex` subgroups.

Issue #86 Session B. Parses argv through ``build_parser()`` and asserts the
wiring directly (independent of fixtures): each subgroup leaf resolves to the
same ``cmd_*`` implementation as its flat alias, ``args.command`` resolves to
the leaf name (so banner-suppression keys match the flat forms), bare subgroups
require a command, and the flat forms still route (guards the extract-to-builder
refactor).
"""
import pytest
from conftest import load_script

# Loaded once; build_parser() is pure (no I/O), so a single namespace suffices.
_NS = load_script()


def _parse(argv):
    return _NS["build_parser"]().parse_args(argv)


CLAUDE_LEAVES = {
    "daily": "cmd_daily",
    "monthly": "cmd_monthly",
    "weekly": "cmd_weekly",
    "session": "cmd_session",
    "blocks": "cmd_blocks",
}
CODEX_LEAVES = {
    "daily": "cmd_codex_daily",
    "monthly": "cmd_codex_monthly",
    "session": "cmd_codex_session",
    "weekly": "cmd_codex_weekly",
}


@pytest.mark.parametrize("leaf,func_name", CLAUDE_LEAVES.items())
def test_claude_subgroup_routes_to_flat_impl(leaf, func_name):
    args = _parse(["claude", leaf])
    assert args.func is _NS[func_name]
    assert args.command == leaf  # nested dest overwrites parent -> leaf name


@pytest.mark.parametrize("leaf,func_name", CODEX_LEAVES.items())
def test_codex_subgroup_routes_to_flat_impl(leaf, func_name):
    args = _parse(["codex", leaf])
    assert args.func is _NS[func_name]
    assert args.command == leaf


def test_bare_subgroups_require_a_command():
    for grp in (["claude"], ["codex"]):
        with pytest.raises(SystemExit):
            _parse(grp)


def test_flat_forms_still_route():
    assert _parse(["daily"]).func is _NS["cmd_daily"]
    assert _parse(["monthly"]).func is _NS["cmd_monthly"]
    assert _parse(["weekly"]).func is _NS["cmd_weekly"]
    assert _parse(["session"]).func is _NS["cmd_session"]
    assert _parse(["blocks"]).func is _NS["cmd_blocks"]
    assert _parse(["codex-daily"]).func is _NS["cmd_codex_daily"]
    assert _parse(["codex-monthly"]).func is _NS["cmd_codex_monthly"]
    assert _parse(["codex-session"]).func is _NS["cmd_codex_session"]
    assert _parse(["codex-weekly"]).func is _NS["cmd_codex_weekly"]


def test_recompute_banner_suppression_parity_flat_vs_subgroup(monkeypatch):
    """The recompute-migration banner gate (`_recompute_banner_should_emit` in
    the `_cctally_db` sibling) reads `sys.argv` directly because migration
    handlers don't receive `args`. Before issue #86 Session B's leaf resolution
    it keyed off `sys.argv[1]` only, so `cctally claude blocks` (argv[1] ==
    "claude", not suppressed) would emit the banner while flat `cctally blocks`
    (argv[1] == "blocks", suppressed) would not — diverging from the spec §3
    parity invariant. This asserts both forms suppress/emit identically.
    """
    db = _NS["_cctally_db"]
    cases = [
        (["cctally", "blocks"], False),           # suppressed (flat)
        (["cctally", "claude", "blocks"], False),  # MUST also be suppressed (subgroup)
        (["cctally", "daily"], True),             # not suppressed (flat)
        (["cctally", "claude", "daily"], True),    # not suppressed (subgroup)
    ]
    for argv, expected in cases:
        monkeypatch.setattr(db.sys, "argv", argv)
        assert db._recompute_banner_should_emit(data_present=True) is expected, argv
