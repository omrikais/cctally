"""Issue #86 Session D — Codex `--speed` tier.

Ports the upstream ryoppippi/ccusage adapter/codex/speed.rs test cases for
the service_tier line-scan, plus cctally's fast-multiplier table, the
`_resolve_codex_speed` auto-detection, and the kernel multiply.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


def _load_cctally_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cc():
    return _load_cctally_module()


# ── service_tier line-scan (ported from speed.rs tests) ───────────────────
@pytest.mark.parametrize("content", [
    'service_tier = "fast"',
    "service_tier = 'priority' # use higher tier",
    '  service_tier="fast"  ',
    'service_tier = "priority"',
])
def test_detects_fast_or_priority(cc, content):
    assert cc._codex_config_requests_fast_service_tier(content) is True


@pytest.mark.parametrize("content", [
    'service_tier_override = "fast"',
    'service_tier = "breakfast"',
    'service_tier = "standard"',
    'service_tier = "default"',
    "# service_tier = \"fast\"",
    "",
])
def test_ignores_unrelated(cc, content):
    assert cc._codex_config_requests_fast_service_tier(content) is False


def test_detects_across_multiline_and_tables(cc):
    content = "[profiles.work]\nmodel = \"gpt-5.2\"\nservice_tier = \"fast\"\n"
    assert cc._codex_config_requests_fast_service_tier(content) is True


# ── fast multiplier table ─────────────────────────────────────────────────
@pytest.mark.parametrize("model,expected", [
    ("gpt-5.5", 2.5),
    ("gpt-5.4", 2.0),
    ("gpt-5.3-codex", 2.0),
    ("gpt-5", 2.0),            # unlisted → fallback
    ("gpt-5.2-codex", 2.0),    # unlisted → fallback
    ("totally-unknown", 2.0),  # unknown → fallback
])
def test_fast_multiplier(cc, model, expected):
    assert cc._codex_fast_multiplier(model) == expected


# ── kernel multiply ───────────────────────────────────────────────────────
def test_kernel_fast_scales_standard(cc):
    args = ("gpt-5.5", 1_000, 0, 500, 0)
    std = cc._calculate_codex_entry_cost(*args, speed="standard")
    fast = cc._calculate_codex_entry_cost(*args, speed="fast")
    assert std > 0
    assert fast == pytest.approx(std * 2.5)


def test_kernel_default_is_standard(cc):
    args = ("gpt-5.5", 1_000, 0, 500, 0)
    assert cc._calculate_codex_entry_cost(*args) == cc._calculate_codex_entry_cost(*args, speed="standard")


# ── resolver auto-detection (HOME-scoped) ─────────────────────────────────
def test_resolve_auto_fast_with_config(cc, tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('service_tier = "fast"\n')
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert cc._resolve_codex_speed("auto") == "fast"


def test_resolve_auto_standard_without_config(cc, tmp_path, monkeypatch):
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert cc._resolve_codex_speed("auto") == "standard"


@pytest.mark.parametrize("requested", ["fast", "standard"])
def test_resolve_passthrough(cc, requested, tmp_path, monkeypatch):
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    assert cc._resolve_codex_speed(requested) == requested
