"""Binding-semantics regression for cmd_cache_report (spec §5.C).

Proves cmd_cache_report's render + window + IO accessor paths resolve through
cctally's namespace (the `c.<name>` accessor in bin/_cctally_cache_report.py),
NOT honest imports — so monkeypatch.setattr(cctally, "X", …) is preserved after
the glue extraction. Patches only STAYS / accessor-routed symbols that
cache-report actually calls (get_entries / get_claude_session_entries /
resolve_display_tz / _short_model_name / format_display_dt). Moved intra-module
helpers (_layout_cache_table, _sort_cache_rows, _aggregate_*, …) are NOT patched
— post-cut they are sibling-local globals.

Vacuity guard (Codex r2): cmd_cache_report early-returns at `if not rows:`
(bin/cctally cmd_cache_report) BEFORE render, so the patched get_entries /
get_claude_session_entries MUST return non-empty cache-active entries or the
render-time spies (_short_model_name / format_display_dt) never fire. The test
asserts each render-time spy's call-count >= 1, for BOTH day and --by-session
mode.

Non-vacuity (Task 3 Step 2): RED if any patched symbol becomes an honest import
in the glue (the implementer honest-imports a spied symbol, asserts RED, reverts,
asserts GREEN — feedback_prove_test_non_vacuous).
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

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


_TS = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _day_entry() -> SimpleNamespace:
    """Minimal cache-active SessionEntry-shaped object (day-mode input).

    Cribbed from tests/test_cache_report_builder.py::_make_entry — day mode
    reads the ``usage`` dict. cache_read > 0 so the row is cache-active and
    survives to render (non-empty `rows` → past the `if not rows:` guard).
    """
    return SimpleNamespace(
        timestamp=_TS,
        model="claude-sonnet-4-6",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 1000,
        },
        cost_usd=0.01,
        source_path="/tmp/session.jsonl",
    )


def _session_entry() -> SimpleNamespace:
    """Minimal cache-active ClaudeSessionEntry-shaped object (session input).

    Cribbed from tests/test_cache_report_builder.py::_make_session_entry —
    session mode reads flat ``input_tokens`` / ``cache_*_tokens`` attributes,
    plus ``session_id`` / ``project_path`` / ``source_path``. A non-None
    ``last_activity`` (= timestamp) is what drives the format_display_dt spy.
    """
    return SimpleNamespace(
        timestamp=_TS,
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=200,
        cache_read_tokens=1000,
        cost_usd=0.01,
        source_path="/tmp/abc-1234.jsonl",
        session_id="sess-1",
        project_path="/home/user/proj",
    )


def _cache_report_args(mod, *, by_session: bool) -> argparse.Namespace:
    """Build a fully-formed cache-report Namespace via the live parser.

    Using the real build_parser() (vs. hand-rolling a Namespace) keeps the
    arg surface in lockstep with the parser as it evolves — no drift.
    """
    argv = ["cache-report"]
    if by_session:
        argv.append("--by-session")
    return mod.build_parser().parse_args(argv)


def test_cmd_cache_report_resolves_accessors_through_cctally_ns(
    cctally_mod, monkeypatch
):
    """cmd_cache_report (day + --by-session): patch the accessor-routed symbols
    cache-report actually calls and assert each fires — proving the glue reaches
    them via c.<name>, not honest import."""
    c = cctally_mod
    calls = {
        "get_entries": 0,
        "get_claude_session_entries": 0,
        "resolve_display_tz": 0,
        "_short_model_name": 0,
        "format_display_dt": 0,
    }

    real_tz = c.resolve_display_tz
    real_short = c._short_model_name
    real_fmt = c.format_display_dt

    def spy(name, real):
        def w(*a, **k):
            calls[name] += 1
            return real(*a, **k)
        return w

    def fake_get_entries(*a, **k):
        calls["get_entries"] += 1
        return [_day_entry()]

    def fake_get_claude_session_entries(*a, **k):
        calls["get_claude_session_entries"] += 1
        return [_session_entry()]

    monkeypatch.setattr(c, "get_entries", fake_get_entries)
    monkeypatch.setattr(
        c, "get_claude_session_entries", fake_get_claude_session_entries
    )
    monkeypatch.setattr(c, "resolve_display_tz", spy("resolve_display_tz", real_tz))
    monkeypatch.setattr(c, "_short_model_name", spy("_short_model_name", real_short))
    monkeypatch.setattr(c, "format_display_dt", spy("format_display_dt", real_fmt))

    for by_session in (False, True):
        args = _cache_report_args(c, by_session=by_session)
        rc = c.cmd_cache_report(args)
        assert rc == 0, f"cmd_cache_report returned {rc} (by_session={by_session})"

    # Vacuity guard: the patched cache readers AND the render-time spies fired.
    # get_entries fires in day mode (also via _emit_debug_samples loader, which
    # is a no-op without --debug); get_claude_session_entries fires in session
    # mode. Each render-time spy must have fired at least once across the two
    # invocations or the patch is not on the live call path.
    assert calls["get_entries"] >= 1, "get_entries not reached via the namespace"
    assert calls["get_claude_session_entries"] >= 1, (
        "get_claude_session_entries not reached via the namespace"
    )
    assert calls["resolve_display_tz"] >= 1, (
        "resolve_display_tz not reached via the namespace"
    )
    assert calls["_short_model_name"] >= 1, (
        "_short_model_name not reached via the namespace"
    )
    assert calls["format_display_dt"] >= 1, (
        "format_display_dt not reached via the namespace (session render)"
    )
