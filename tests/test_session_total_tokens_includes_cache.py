"""Issue #104 — `session` `totalTokens` includes cache (ccusage v20 parity).

Per the #104 decision (option 2), the per-session roll-up `totalTokens`
counts ALL four token components (input + output + cacheCreation +
cacheRead), matching `daily`/`monthly` and upstream `ccusage` v20. This
test pins the aggregator semantic at the source (`_aggregate_claude_sessions`)
so a future "parity" edit that reverts to input+output-only is caught.

The terminal-table render path recomputes the per-row / footer total from
`s.total_tokens`, and the breakdown row sums the four component cells; the
golden harness (`bin/cctally-session-test`) covers those surfaces.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture(scope="module")
def cctally_mod():
    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _entry(mod, **kw):
    cls = mod._cctally_cache._JoinedClaudeEntry
    ts = kw.pop("timestamp", dt.datetime(2026, 4, 15, 10, 0, tzinfo=dt.timezone.utc))
    return cls(
        timestamp=ts,
        model=kw.pop("model", "claude-opus-4-7"),
        input_tokens=kw.pop("input_tokens", 0),
        output_tokens=kw.pop("output_tokens", 0),
        cache_creation_tokens=kw.pop("cache_creation_tokens", 0),
        cache_read_tokens=kw.pop("cache_read_tokens", 0),
        source_path=kw.pop("source_path", "/fake/jsonl/s.jsonl"),
        session_id=kw.pop("session_id", "sess-uuid"),
        project_path=kw.pop("project_path", "/fake/repos/baseline"),
    )


def test_session_total_tokens_includes_cache(cctally_mod):
    """`total_tokens` sums all four components, not just input+output."""
    entries = [
        _entry(
            cctally_mod,
            input_tokens=700_000,
            output_tokens=70_000,
            cache_creation_tokens=120_000,
            cache_read_tokens=50_000,
        ),
    ]
    sessions = cctally_mod._aggregate_claude_sessions(entries)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.input_tokens == 700_000
    assert s.output_tokens == 70_000
    assert s.cache_creation_tokens == 120_000
    assert s.cache_read_tokens == 50_000
    # The crux of #104: cache is summed into the roll-up.
    assert s.total_tokens == 700_000 + 70_000 + 120_000 + 50_000
    assert s.total_tokens == (
        s.input_tokens + s.output_tokens
        + s.cache_creation_tokens + s.cache_read_tokens
    )


def test_session_json_total_tokens_includes_cache(cctally_mod):
    """The `--json` per-session and totals `totalTokens` carry the cache sum."""
    import json as _json

    entries = [
        _entry(
            cctally_mod,
            session_id="a",
            input_tokens=100,
            output_tokens=10,
            cache_creation_tokens=5,
            cache_read_tokens=3,
        ),
        _entry(
            cctally_mod,
            session_id="b",
            source_path="/fake/jsonl/b.jsonl",
            input_tokens=200,
            output_tokens=20,
            cache_creation_tokens=7,
            cache_read_tokens=1,
        ),
    ]
    sessions = cctally_mod._aggregate_claude_sessions(entries)
    payload = _json.loads(cctally_mod._claude_sessions_to_json(sessions))

    by_id = {s["sessionId"]: s for s in payload["sessions"]}
    assert by_id["a"]["totalTokens"] == 100 + 10 + 5 + 3
    assert by_id["b"]["totalTokens"] == 200 + 20 + 7 + 1
    assert payload["totals"]["totalTokens"] == (100 + 10 + 5 + 3) + (200 + 20 + 7 + 1)
