"""Binding-semantics regression for cmd_percent_breakdown (spec §5.C).

Proves cmd_percent_breakdown's data-path + render accessor paths resolve through
cctally's namespace (the `c.<name>` accessor in bin/_cctally_percent_breakdown.py),
NOT honest imports — so monkeypatch.setattr(cctally, "X", …) is preserved after
the glue extraction. The headline reach is get_milestones_for_week, a C2 symbol
that now lives in sibling bin/_cctally_milestones.py but is reached on the cctally
ns (c.get_milestones_for_week), NOT a direct sibling-to-sibling import.

Per-mode matrix (spec §5.C): the handler returns at `if args.json:` BEFORE the
render helpers, so JSON mode proves ONLY the data-path accessors
(get_milestones_for_week / _get_canonical_boundary_for_date / resolve_display_tz),
while md mode proves the data-path PLUS the two render helpers
(_format_ts_compact / _boxed_table).

Vacuity guard: cmd_percent_breakdown early-returns at `if not milestone_list:`
before the table render, so the patched get_milestones_for_week MUST return >=1
milestone row (reset_event_id == active_segment == 0 on the empty DB), and
_get_canonical_boundary_for_date MUST return non-None ISO bounds so md takes the
`display_start_iso and display_end_iso` branch and the _format_ts_compact spy fires.

Non-vacuity (Task 6 Step 3): RED if any spied symbol becomes an honest import in
the sibling (the implementer temporarily honest-imports get_milestones_for_week,
asserts RED, reverts, asserts GREEN — feedback_prove_test_non_vacuous).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    """Load bin/cctally as a module under an isolated, empty data dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _fake_milestone_row():
    # dict supports m["key"] exactly like sqlite3.Row; reset_event_id must
    # equal the active_segment cmd_percent_breakdown computes (0 on an empty DB).
    return {
        "percent_threshold": 1,
        "cumulative_cost_usd": 0.5,
        "marginal_cost_usd": 0.5,
        "captured_at_utc": "2026-05-01T00:00:00Z",
        "five_hour_percent_at_crossing": 12.0,
        "reset_event_id": 0,
    }


def _install_spies(cctally, monkeypatch):
    calls = {k: 0 for k in (
        "get_milestones_for_week", "_get_canonical_boundary_for_date",
        "resolve_display_tz", "_format_ts_compact", "_boxed_table",
    )}

    real_tz = cctally.resolve_display_tz
    real_box = cctally._boxed_table
    real_ts = cctally._format_ts_compact

    def spy_milestones(conn, week_start_date):
        calls["get_milestones_for_week"] += 1
        return [_fake_milestone_row()]

    def spy_canon(conn, week_start_date):
        calls["_get_canonical_boundary_for_date"] += 1
        # non-None ISO bounds so md takes the display_start_iso branch -> _format_ts_compact fires
        return ("2026-05-01T00:00:00Z", "2026-05-07T23:59:59Z")

    def spy_tz(args, config):
        calls["resolve_display_tz"] += 1
        return real_tz(args, config)

    def spy_ts(*a, **k):
        calls["_format_ts_compact"] += 1
        return real_ts(*a, **k)

    def spy_box(*a, **k):
        calls["_boxed_table"] += 1
        return real_box(*a, **k)

    monkeypatch.setattr(cctally, "get_milestones_for_week", spy_milestones)
    monkeypatch.setattr(cctally, "_get_canonical_boundary_for_date", spy_canon)
    monkeypatch.setattr(cctally, "resolve_display_tz", spy_tz)
    monkeypatch.setattr(cctally, "_format_ts_compact", spy_ts)
    monkeypatch.setattr(cctally, "_boxed_table", spy_box)
    return calls


def _run(cctally, json_mode):
    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=json_mode, tz=None,
    )
    rc = cctally.cmd_percent_breakdown(args)
    assert rc == 0
    return rc


def test_percent_breakdown_md_reaches_all_accessors_via_ns(cctally_mod, monkeypatch):
    calls = _install_spies(cctally_mod, monkeypatch)
    _run(cctally_mod, json_mode=False)
    # md proves data-path AND render helpers
    for name in ("get_milestones_for_week", "_get_canonical_boundary_for_date",
                 "resolve_display_tz", "_format_ts_compact", "_boxed_table"):
        assert calls[name] >= 1, f"{name} not reached via the cctally ns in md mode"


def test_percent_breakdown_json_reaches_data_path_via_ns(cctally_mod, monkeypatch):
    calls = _install_spies(cctally_mod, monkeypatch)
    _run(cctally_mod, json_mode=True)
    # JSON returns before render -> prove ONLY the data-path accessors
    for name in ("get_milestones_for_week", "_get_canonical_boundary_for_date",
                 "resolve_display_tz"):
        assert calls[name] >= 1, f"{name} not reached via the cctally ns in json mode"
    # render helpers are unreachable in json mode (handler returns at if args.json:)
    assert calls["_format_ts_compact"] == 0
    assert calls["_boxed_table"] == 0
