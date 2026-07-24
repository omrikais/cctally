"""§9.3 — `cctally session -i / --id` filter.

Spec §7.4: exact-string match on post-resume-merge ``ClaudeSessionUsage.session_id``.
Filter runs BEFORE the `--order asc` reversal and BEFORE the JSON /
share / table render branches. Empty result reuses the existing
"no sessions" empty-render branch and exits 0.

Test strategy: drive `cmd_session` in-process via the importable
cctally module with monkeypatched `get_claude_session_entries` +
`build_sessions_view`. This gives us deterministic input data without
needing a fixture-built `~/.claude/projects/` tree (which would
require many MB of synthetic JSONL).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture(scope="module")
def cctally_mod():
    """Load ``bin/cctally`` as a Python module."""
    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _fake_session(mod, *, session_id, project_path="proj", cost=1.0):
    """Build a minimal ClaudeSessionUsage that satisfies the renderer."""
    cls = mod._lib_aggregators.ClaudeSessionUsage
    now = dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc)
    earlier = dt.datetime(2026, 1, 15, 10, 0, tzinfo=dt.timezone.utc)
    return cls(
        session_id=session_id,
        project_path=project_path,
        source_paths=[f"{project_path}.jsonl"],
        first_activity=earlier,
        last_activity=now,
        input_tokens=100,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=50,
        total_tokens=150,
        cost_usd=cost,
        models=["claude-3-5-sonnet-20241022"],
        model_breakdowns=[],
    )


def _make_args(mod, **overrides):
    """Build an argparse.Namespace matching `cmd_session`'s expected args."""
    ns = argparse.Namespace(
        since=None, until=None, breakdown=False, order="asc",
        reveal_projects=False, top_n=15, tz=None, timezone=None,
        json=False, format=None, theme="light", output=None, copy=False,
        open_after_write=False, no_branding=False,
        debug=False, debug_samples=5, single_thread=False,
        offline=False, compact=False, config=None, color=False,
        no_color=False,
        id=None,
        # Session C (#86): cmd_session now threads args.mode into
        # build_sessions_view; the real parser defaults it to "auto".
        mode="auto",
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def patched_session(cctally_mod, monkeypatch):
    """Patch the session-data pipeline to return two deterministic sessions."""
    mod = cctally_mod
    sid_a = "session-a-uuid-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sid_b = "session-b-uuid-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    sess_a = _fake_session(mod, session_id=sid_a, project_path="proj-a")
    sess_b = _fake_session(mod, session_id=sid_b, project_path="proj-b", cost=2.0)

    # Pre-merged ClaudeSessionUsage list, in newest-first order (the
    # aggregator's natural shape).
    aggregated = [sess_b, sess_a]

    # Replace `get_claude_session_entries` (DB read) → empty entries list.
    # cmd_session now passes account_key= (#341), so absorb it via **kwargs.
    monkeypatch.setattr(mod, "get_claude_session_entries",
                        lambda start, end, **kwargs: [])
    # Replace `build_sessions_view` with a stub returning our mock data.
    fake_view = SimpleNamespace(aggregated=tuple(aggregated), rows=())
    monkeypatch.setattr(
        mod, "build_sessions_view",
        lambda *a, **kw: fake_view,
    )
    # Skip the share-validation noise (no --format used in these tests).
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)
    # Pinned "now" so test data is in-range.
    monkeypatch.setattr(
        mod, "_command_as_of",
        lambda: dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    return mod, sid_a, sid_b


def _capture(fn):
    """Run `fn()` capturing stdout+stderr; return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn()
    return rc, out.getvalue(), err.getvalue()


def test_filter_to_known_id_table(patched_session):
    mod, sid_a, sid_b = patched_session
    args = _make_args(mod, id=sid_a)
    rc, stdout, stderr = _capture(lambda: mod.cmd_session(args))
    assert rc == 0, stderr
    # sid_a's project basename must appear; sid_b's must not.
    assert "proj-a" in stdout
    assert "proj-b" not in stdout


def test_filter_to_known_id_json(patched_session):
    mod, sid_a, sid_b = patched_session
    args = _make_args(mod, id=sid_a, json=True)
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    payload = json.loads(stdout)
    # JSON shape from `_claude_sessions_to_json` is {"sessions": [...], ...}.
    sessions = payload.get("sessions") if isinstance(payload, dict) else payload
    assert isinstance(sessions, list)
    assert len(sessions) == 1
    assert sessions[0].get("sessionId") == sid_a


def test_short_alias_i_with_full_id(patched_session):
    # The short form `-i` is just argparse-syntactic; cmd_session reads
    # `args.id` either way. Test the same code path with id set.
    mod, sid_a, sid_b = patched_session
    args = _make_args(mod, id=sid_a, json=True)
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    payload = json.loads(stdout)
    sessions = payload.get("sessions", payload)
    assert len(sessions) == 1


def test_unknown_id_exits_zero_empty_render(patched_session):
    mod, *_ = patched_session
    args = _make_args(mod, id="totally-unknown-session-id-zzzz")
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    # The cmd_session empty-render branch prints
    # "No Claude session data found." when sessions is empty.
    assert "No Claude session data found." in stdout


def test_unknown_id_json_empty_array(patched_session):
    mod, *_ = patched_session
    args = _make_args(mod, id="totally-unknown-session-id-zzzz", json=True)
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    payload = json.loads(stdout)
    sessions = payload.get("sessions") if isinstance(payload, dict) else payload
    assert isinstance(sessions, list)
    assert sessions == []


def test_id_without_args_returns_all(patched_session):
    # Sanity: no --id supplied → both sessions render.
    mod, sid_a, sid_b = patched_session
    args = _make_args(mod, json=True)
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    payload = json.loads(stdout)
    sessions = payload.get("sessions", payload)
    assert len(sessions) == 2


def test_empty_string_id_filters_to_empty_not_all(patched_session):
    # Regression (code-review): an explicit `--id ''` must ENGAGE the filter
    # (no session matches the empty id → empty render), NOT fall through to
    # "show all sessions". The pre-fix truthiness guard
    # (`if getattr(args, "id", None):`) treated '' as falsy → no filter →
    # both sessions rendered. The fix uses `is not None`.
    mod, sid_a, sid_b = patched_session
    args = _make_args(mod, id="", json=True)
    rc, stdout, _ = _capture(lambda: mod.cmd_session(args))
    assert rc == 0
    payload = json.loads(stdout)
    sessions = payload.get("sessions") if isinstance(payload, dict) else payload
    assert sessions == []


def test_filter_applies_to_share_export(cctally_mod, monkeypatch):
    """Codex review (round 1): `session --id X --format ...` must export
    ONLY the matching session. The terminal/JSON paths filter the local
    `sessions` list, but the share builder reads `view.aggregated`, so
    `cmd_session` must hand it a view whose `aggregated` is the filtered
    list — otherwise `--id` is silently ignored for HTML/Markdown/SVG.

    Uses a real `SessionsView` (frozen dataclass) so the
    `dataclasses.replace(view, aggregated=...)` in `cmd_session` works,
    and intercepts `_build_session_snapshot` to assert the view it
    receives carries only the filtered session.
    """
    mod = cctally_mod
    sid_a = "session-a-uuid-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sid_b = "session-b-uuid-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    sess_a = _fake_session(mod, session_id=sid_a, project_path="proj-a")
    sess_b = _fake_session(mod, session_id=sid_b, project_path="proj-b", cost=2.0)

    real_view = mod._lib_view_models.SessionsView(
        rows=(),
        aggregated=(sess_b, sess_a),
        total_sessions=2,
        total_cost_usd=3.0,
    )
    monkeypatch.setattr(mod, "get_claude_session_entries",
                        lambda start, end, **kwargs: [])
    monkeypatch.setattr(mod, "build_sessions_view",
                        lambda *a, **kw: real_view)
    monkeypatch.setattr(mod, "_share_validate_args", lambda args: None)
    monkeypatch.setattr(
        mod, "_command_as_of",
        lambda: dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.timezone.utc),
    )

    captured = {}

    def fake_build_session_snapshot(view, **kwargs):
        captured["aggregated"] = tuple(view.aggregated)
        return SimpleNamespace()  # value only forwarded to the stubbed emitter

    monkeypatch.setattr(mod, "_build_session_snapshot",
                        fake_build_session_snapshot)
    monkeypatch.setattr(mod, "_share_render_and_emit", lambda snap, args: None)

    args = _make_args(mod, id=sid_a, format="md")
    rc, _, stderr = _capture(lambda: mod.cmd_session(args))
    assert rc == 0, stderr
    # The builder must see ONLY the filtered session.
    assert "aggregated" in captured, "snapshot builder was not invoked"
    agg = captured["aggregated"]
    assert len(agg) == 1
    assert agg[0].session_id == sid_a


def test_id_subprocess_short_flag_parses(tmp_path, monkeypatch):
    """Smoke: `cctally session -i <unknown> --json` on an empty home
    parses cleanly and exits 0 with an empty sessions list.

    This is the only end-to-end subprocess test in the suite. It
    proves the `-i` short alias is registered on the argparse parser
    (the in-process tests above can pass even if the flag wasn't
    wired to the parser, as long as the namespace key is `id`).
    """
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("XDG_DATA_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    r = subprocess.run(
        [sys.executable, str(CCTALLY), "session",
         "-i", "totally-unknown-id-aaaa-bbbb-cccc",
         "--json"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert "unrecognized arguments" not in r.stderr
    payload = json.loads(r.stdout)
    sessions = payload.get("sessions") if isinstance(payload, dict) else payload
    assert isinstance(sessions, list)
    assert sessions == []
