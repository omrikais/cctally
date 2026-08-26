"""§9.2a — _resolve_claude_tz_name precedence tests.

Spec: ``docs/superpowers/specs/2026-05-22-issue-86-session-a-ccusage-alias-pass.md``
§7.2 (issue #86 Session A).

Precedence (top wins):
  1. ``args.tz`` (canonical cctally flag)
  2. ``args.timezone`` (ccusage-codex ``-z`` / ``--timezone`` alias)
  3. ``config['display']['tz']`` (persisted user pref)
  4. ``None`` → host-local (existing fallback in ``resolve_display_tz``)
"""
from __future__ import annotations

import argparse

import pytest

from _script_loader import load_script_module


@pytest.fixture(scope="module")
def cctally_mod():
    """Load ``bin/cctally`` under its own identity and keep that reference.

    The ``cctally_cli`` name gives this module a durable ``sys.modules`` entry
    that later ``load_script()`` calls, which rebind ``sys.modules["cctally"]``,
    do not disturb.
    """
    return load_script_module("cctally_cli")


def _ns(**kwargs):
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_only_tz_set(cctally_mod):
    ns = _ns(tz="utc", timezone=None)
    assert cctally_mod._resolve_claude_tz_name(ns, {}) == "utc"


def test_only_z_set(cctally_mod):
    ns = _ns(tz=None, timezone="UTC")
    assert cctally_mod._resolve_claude_tz_name(ns, {}) == "UTC"


def test_only_config_set(cctally_mod):
    ns = _ns(tz=None, timezone=None)
    config = {"display": {"tz": "America/New_York"}}
    assert cctally_mod._resolve_claude_tz_name(ns, config) == "America/New_York"


def test_neither_flag_nor_config(cctally_mod):
    ns = _ns(tz=None, timezone=None)
    # Returns None → resolve_display_tz interprets as host-local.
    assert cctally_mod._resolve_claude_tz_name(ns, {}) is None


def test_z_beats_config(cctally_mod):
    ns = _ns(tz=None, timezone="UTC")
    config = {"display": {"tz": "America/New_York"}}
    assert cctally_mod._resolve_claude_tz_name(ns, config) == "UTC"


def test_config_beats_local(cctally_mod):
    ns = _ns(tz=None, timezone=None)
    config = {"display": {"tz": "Europe/Berlin"}}
    assert cctally_mod._resolve_claude_tz_name(ns, config) == "Europe/Berlin"


def test_tz_beats_z_beats_config(cctally_mod):
    ns = _ns(tz="utc", timezone="UTC")
    config = {"display": {"tz": "America/New_York"}}
    assert cctally_mod._resolve_claude_tz_name(ns, config) == "utc"
