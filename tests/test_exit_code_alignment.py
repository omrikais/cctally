"""Exit-code alignment for project / forecast bad-input paths (#279 S6 W2).

Both commands are cctally-native (not ccusage drop-ins) but historically exited
1 on their own usage/validation errors, out of step with the rest of the native
family (diff / budget / five-hour-* / pricing-check / doctor …) which exit 2.
This session aligns them to 2, documented in docs/cli-contract.md + the command
docs + CHANGELOG. These regressions pin the new codes.

The validation branches all return before open_db(), so a hand-built namespace
(via build_parser().parse_args) driven straight into the cmd_* handler exercises
them without any DB/corpus.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _args(mod, argv):
    return mod.build_parser().parse_args(argv)


# ── project: all four bad-input paths exit 2 ────────────────────────────

def test_project_bad_weeks_exits_2(mod):
    assert mod.cmd_project(_args(mod, ["project", "--weeks", "0"])) == 2


def test_project_weeks_with_since_exits_2(mod):
    assert mod.cmd_project(
        _args(mod, ["project", "--weeks", "2", "--since", "2026-01-01"])) == 2


def test_project_since_after_until_exits_2(mod):
    assert mod.cmd_project(
        _args(mod, ["project", "--since", "2026-02-01", "--until", "2026-01-01"])) == 2


def test_project_bad_since_format_exits_2(mod):
    # The _parse_cli_date_range pass-through (gate F7): the parity-family helper
    # returns its own 1, which project now translates to the native 2.
    assert mod.cmd_project(_args(mod, ["project", "--since", "garbage"])) == 2


# ── forecast: both validation paths exit 2 ──────────────────────────────

def test_forecast_json_status_line_mutex_exits_2(mod):
    # The CLI argparse mutex catches --json --status-line first (exit 2); the
    # handler's own guard is reachable only via a direct call, so set the
    # attribute past the parser to exercise the aligned code.
    args = _args(mod, ["forecast", "--json"])
    args.status_line = True
    assert mod.cmd_forecast(args) == 2


def test_forecast_bad_targets_exits_2(mod):
    assert mod.cmd_forecast(_args(mod, ["forecast", "--targets", "garbage"])) == 2
