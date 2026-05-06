"""Unit tests for CC version discovery + UA derivation."""
import pytest
from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def test_semver_basic(ns):
    fn = ns["_parse_cc_semver"]
    assert fn("2.1.116") == "2.1.116"


def test_semver_prerelease_preserved(ns):
    fn = ns["_parse_cc_semver"]
    assert fn("2.1.116-beta.1") == "2.1.116-beta.1"
    assert fn("10.20.30-rc.5") == "10.20.30-rc.5"


def test_semver_extracts_first_match_in_string(ns):
    fn = ns["_parse_cc_semver"]
    # `claude --version` may emit "claude-code 2.1.116 (build abc)"
    assert fn("claude-code 2.1.116 (build abc)") == "2.1.116"


def test_semver_rejects_non_semver(ns):
    fn = ns["_parse_cc_semver"]
    assert fn("latest") is None
    assert fn("2.1") is None
    assert fn("") is None
    assert fn("2.1.116.4") is None  # 4-part not allowed


def test_semver_rejects_none(ns):
    fn = ns["_parse_cc_semver"]
    assert fn(None) is None


import subprocess
import pathlib


def test_discover_uses_claude_version_first(ns, monkeypatch, tmp_path):
    """When `claude --version` returns a parseable line, that wins
    even if versions/ has other dirs."""
    versions = tmp_path / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    (versions / "9.9.9").mkdir()  # would lose to subprocess
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="claude-code 2.1.116 (Bun)\n", stderr=""
        )
    monkeypatch.setattr(ns["subprocess"], "run", fake_run)

    assert ns["_discover_cc_version"]() == "2.1.116"


def test_discover_falls_back_to_versions_dir(ns, monkeypatch, tmp_path):
    """When `claude --version` fails, pick highest semver from versions/."""
    versions = tmp_path / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    (versions / "2.0.0").mkdir()
    (versions / "2.1.116").mkdir()
    (versions / "latest").mkdir()  # non-semver, must be skipped
    (versions / "2.0.50-beta.1").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude not on PATH")
    monkeypatch.setattr(ns["subprocess"], "run", fake_run)

    assert ns["_discover_cc_version"]() == "2.1.116"


def test_discover_falls_back_to_sentinel(ns, monkeypatch, tmp_path):
    """When neither `claude --version` nor versions/ yields a hit, use
    the frozen sentinel."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no claude")
    monkeypatch.setattr(ns["subprocess"], "run", fake_run)

    assert ns["_discover_cc_version"]() == ns["CLAUDE_CODE_UA_FALLBACK_VERSION"]


def test_discover_handles_subprocess_timeout(ns, monkeypatch, tmp_path):
    """subprocess.TimeoutExpired must not propagate; falls through."""
    versions = tmp_path / ".local" / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    (versions / "2.1.116").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=5)
    monkeypatch.setattr(ns["subprocess"], "run", fake_run)

    assert ns["_discover_cc_version"]() == "2.1.116"


def test_resolve_user_agent_default(ns):
    """No override -> claude-code/<discovered>."""
    fn = ns["_resolve_oauth_usage_user_agent"]
    cfg = {"user_agent": None, "throttle_seconds": 15,
           "fresh_threshold_seconds": 30, "stale_after_seconds": 90}
    ua = fn(cfg, version_resolver=lambda: "2.1.116")
    assert ua == "claude-code/2.1.116"


def test_resolve_user_agent_override(ns):
    """Explicit override flows through verbatim."""
    fn = ns["_resolve_oauth_usage_user_agent"]
    cfg = {"user_agent": "cctally/0.1", "throttle_seconds": 15,
           "fresh_threshold_seconds": 30, "stale_after_seconds": 90}
    ua = fn(cfg, version_resolver=lambda: "2.1.116")
    assert ua == "cctally/0.1"
