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
import sys
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    """Load bin/cctally under the canonical isolated data dir (#127).

    Goes through the shared ``load_isolated_cctally_module`` helper so the
    loader gets ``_cctally_core`` path redirection — without it, a cached
    ``_cctally_core`` made these tests read the real prod DB (#127).
    """
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def test_ns_patch_loader_isolates_db_when_core_cached(tmp_path, monkeypatch):
    """#127 regression: the shared loader must isolate ``_cctally_core``'s
    ``DB_PATH`` to the per-test tmp dir EVEN WHEN ``_cctally_core`` is already
    cached pointing at a non-tmp (real prod) DB.

    Pre-#127 the bespoke loader only ``setenv("HOME", …)``; a cached
    ``_cctally_core`` kept its real ``~/.local/share/cctally/stats.db`` and
    ``cmd_percent_breakdown`` read the developer's actual database (which
    intermittently failed the accessor-reach tests once that DB held a
    ``week_reset_events`` row for the current week — the original symptom).
    """
    import _cctally_core
    from pathlib import Path as _Path
    # Simulate a prior test having cached _cctally_core with the real prod DB.
    monkeypatch.setattr(
        _cctally_core, "DB_PATH",
        _Path("/Users/dev/.local/share/cctally/stats.db"),
    )
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    assert mod is sys.modules["cctally"]
    assert str(_cctally_core.DB_PATH).startswith(str(tmp_path)), (
        f"#127: loader must re-isolate DB_PATH under tmp, got "
        f"{_cctally_core.DB_PATH}"
    )


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

    def spy_milestones(conn, week_start_date, **kwargs):  # **kwargs: #341 account_key=
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
