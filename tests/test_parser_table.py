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


def test_private_shape_has_preview(mod):
    assert "__preview" in _top_choices(mod.build_parser())


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
    # Every registered top-level parser name comes from a table row (private
    # shape, which includes __preview).
    choices = set(_top_choices(mod.build_parser()))
    table_names = {r.name for r in _registration()}
    assert choices == table_names, choices ^ table_names
