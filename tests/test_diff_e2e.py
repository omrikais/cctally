"""End-to-end smoke tests for the diff subcommand."""
import json
import os
import subprocess
import sys
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


@pytest.fixture
def empty_home(tmp_path):
    """A HOME with no DB — exercises the no-anchor path for week tokens."""
    return tmp_path


def _run(args, env_override=None, cwd=None):
    env = {**os.environ, "TZ": "Etc/UTC"}
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30, cwd=cwd,
    )


def test_diff_help_lists_all_flags():
    r = _run(["diff", "--help"])
    assert r.returncode == 0
    for flag in ("--a", "--b", "--allow-mismatch", "--only", "--with",
                 "--all", "--min-delta", "--sort", "--top", "--sync",
                 "--tz", "--no-color", "--json"):
        assert flag in r.stdout, f"flag {flag} missing from --help"


def test_diff_no_anchor_exits_1(empty_home):
    r = _run(["diff", "--a", "this-week", "--b", "last-week"],
             env_override={"HOME": str(empty_home),
                           "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"})
    assert r.returncode == 1
    assert "no subscription-week" in r.stderr.lower()


def test_diff_mismatched_length_exits_2(empty_home):
    r = _run(["diff", "--a", "last-7d", "--b", "prev-14d"],
             env_override={"HOME": str(empty_home),
                           "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"})
    assert r.returncode == 2
    assert "mismatch" in r.stderr.lower()


def test_diff_bad_token_exits_2(empty_home):
    r = _run(["diff", "--a", "bogus-foo", "--b", "last-week"],
             env_override={"HOME": str(empty_home),
                           "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"})
    assert r.returncode == 2


def test_diff_json_emits_valid_envelope(empty_home):
    r = _run(["diff", "--a", "last-7d", "--b", "prev-7d", "--json"],
             env_override={"HOME": str(empty_home),
                           "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"})
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["schema_version"] == 1
    assert payload["subcommand"] == "diff"
    assert payload["windows"]["a"]["label"] == "last-7d"
    assert payload["windows"]["b"]["label"] == "prev-7d"


def test_diff_with_trend_exits_1_with_clean_stderr(empty_home):
    r = _run(["diff", "--a", "last-7d", "--b", "prev-7d", "--with", "trend"],
             env_override={"HOME": str(empty_home),
                           "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"})
    assert r.returncode == 1
    assert "not yet implemented" in r.stderr
    assert "Traceback" not in r.stderr   # no Python traceback


# Markers that uniquely appear in the --debug pricing-mismatch report
# (`_emit_diff_debug_samples` -> `_render_pricing_mismatch_report`). On an
# empty HOME the report degrades to the no-data line, but either marker
# proves the debug scan ran.
_DEBUG_REPORT_MARKERS = (
    "Pricing Mismatch Debug Report",
    "No pricing data found to analyze.",
)


def test_diff_invalid_only_fails_fast_no_debug_no_sync(empty_home):
    """Codex round-4 P3: an invalid --only must fail (exit 2) BEFORE the
    --debug scan emits any report or --sync mutates the cache.

    Regression: previously `_emit_diff_debug_samples` ran (and, under
    --sync, ingested into cache.db) before the --only validation gate, so
    a fail-fast usage error still printed unrelated debug output and
    touched local cache state.
    """
    cache_db = empty_home / ".local" / "share" / "cctally" / "cache.db"
    assert not cache_db.exists()  # precondition: virgin HOME
    r = _run(
        ["diff", "--a", "last-7d", "--b", "prev-7d",
         "--only", "bogus", "--debug", "--sync"],
        env_override={"HOME": str(empty_home),
                      "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"},
    )
    assert r.returncode == 2, r.stderr
    assert "unknown section" in r.stderr
    # No debug report was emitted before the validation error.
    for marker in _DEBUG_REPORT_MARKERS:
        assert marker not in r.stderr, f"debug report leaked: {marker!r}"
    # --sync did not run: the cache DB was never created.
    assert not cache_db.exists(), "cache sync ran before validation"
    assert "Traceback" not in r.stderr


def test_diff_valid_only_still_emits_debug(empty_home):
    """Guard the reorder didn't suppress --debug on a VALID invocation:
    a well-formed --only + --debug must still emit the report."""
    r = _run(
        ["diff", "--a", "last-7d", "--b", "prev-7d",
         "--only", "overall", "--debug"],
        env_override={"HOME": str(empty_home),
                      "CCTALLY_AS_OF": "2026-04-25T19:30:00Z"},
    )
    assert r.returncode == 0, r.stderr
    assert any(marker in r.stderr for marker in _DEBUG_REPORT_MARKERS), (
        "expected a --debug report on a valid invocation"
    )
