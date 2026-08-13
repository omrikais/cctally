"""Registration-table sanity + both parser shapes (#279 S6 W3, gate F14).

build_parser() is a loop over the ordered _REGISTRATION table of per-command
builders. These guard the two invariants the recursive --help byte-sweep can't:
the public-mirror parser shape (cmd_preview=None ⇒ no __preview registration)
and call-time binding (the table stores callables/lambdas, never import-time
resolutions of _cctally() or a cmd_* handler).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _registration():
    # _REGISTRATION / _Reg live in the parser sibling (not re-exported on the
    # cctally namespace); bin/cctally loads it into sys.modules at import.
    return sys.modules["_cctally_parser"]._REGISTRATION


def _top_choices(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return dict(a.choices)
    return {}


def test_root_and_setup_help_describe_both_providers(mod):
    parser = mod.build_parser()
    root_help = parser.format_help()
    setup_help = _top_choices(parser)["setup"].format_help()

    assert "Track Claude and Codex subscription usage" in root_help
    assert "--source {claude,codex,all}" in root_help
    assert "cctally codex quota" in root_help
    assert "Claude hook entries" in setup_help
    assert "native Codex handlers" in setup_help
    assert "configured Codex home" in setup_help


def test_root_provider_discovery_is_derived_from_source_choices(mod, monkeypatch):
    parser_module = sys.modules["_cctally_parser"]
    monkeypatch.setattr(
        parser_module,
        "_PHYSICAL_PROVIDER_SOURCES",
        ("claude", "codex", "nova"),
        raising=False,
    )

    root_help = mod.build_parser().format_help()

    assert "Claude, Codex, and Nova" in root_help
    assert "--source {claude,codex,nova,all}" in root_help
    assert "keep Claude, Codex, and Nova quota percentages" in root_help
    assert "cctally report --source nova" in root_help

    project_parser = _top_choices(mod.build_parser())["project"]
    source_action = next(
        action for action in project_parser._actions
        if "--source" in action.option_strings
    )
    assert tuple(source_action.choices) == ("claude", "codex", "nova", "all")
    assert source_action.help == "Analytics provider."


def test_current_shape_matches_preview_availability(mod):
    choices = _top_choices(mod.build_parser())
    assert ("__preview" in choices) is (mod.cmd_preview is not None)


def test_public_shape_without_preview(mod, monkeypatch):
    # The public mirror ships without cmd_preview; the __preview row's
    # predicate must then skip its registration.
    monkeypatch.setattr(mod, "cmd_preview", None)
    choices = _top_choices(mod.build_parser())
    assert "__preview" not in choices
    # ...and every other command still registers.
    assert "daily" in choices and "budget" in choices


def test_table_names_unique(mod):
    names = [r.name for r in _registration()]
    assert len(names) == len(set(names)), names


def test_table_stores_callables_not_resolved_bindings(mod):
    # Call-time binding (gate F10): builders are callables, predicates are
    # callables-or-None; nothing in the table is a resolved cmd_* handler.
    for r in _registration():
        assert callable(r.builder), r.name
        assert r.predicate is None or callable(r.predicate), r.name


def test_table_covers_top_level_choices(mod):
    # Every registered top-level parser name comes from an enabled table row.
    # The public mirror disables __preview because cmd_preview is absent.
    choices = set(_top_choices(mod.build_parser()))
    table_names = {
        r.name
        for r in _registration()
        if r.predicate is None or r.predicate(mod)
    }
    assert choices == table_names, choices ^ table_names
