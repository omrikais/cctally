"""Issue #108 — $CODEX_HOME multi-root resolution for Codex commands.

Covers the two resolvers, multi-root config detection, session-id derivation
under multiple roots, and end-to-end ingestion union (totals + session id).
"""
from __future__ import annotations

import importlib.util
import json as _json
import os
import subprocess
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


# ── _codex_home_roots() ───────────────────────────────────────────────────
def test_home_roots_unset_defaults(cc, tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_empty_string_defaults(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_single(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "a"))
    assert cc._codex_home_roots() == [tmp_path / "a"]


def test_home_roots_comma_list_and_blanks(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/a, ,{tmp_path}/b,")
    assert cc._codex_home_roots() == [tmp_path / "a", tmp_path / "b"]


def test_home_roots_all_blank_falls_back(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", " , ,")
    monkeypatch.setattr(cc.pathlib.Path, "home", classmethod(lambda c: tmp_path))
    assert cc._codex_home_roots() == [tmp_path / ".codex"]


def test_home_roots_expands_tilde(cc, tmp_path, monkeypatch):
    # NOTE: Path.expanduser() resolves "~" via os.path.expanduser, which reads
    # $HOME (not the Path.home() classmethod), so we set $HOME rather than
    # monkeypatching cc.pathlib.Path.home here.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", "~/codexdir")
    assert cc._codex_home_roots() == [tmp_path / "codexdir"]


# ── _codex_session_roots() ────────────────────────────────────────────────
def test_session_roots_home_with_sessions(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    assert cc._codex_session_roots() == [tmp_path / "h" / "sessions"]


def test_session_roots_direct_jsonl_dir(cc, tmp_path, monkeypatch):
    # No sessions/ subdir → the entry itself is walked directly.
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "logs"))
    assert cc._codex_session_roots() == [tmp_path / "logs"]


def test_session_roots_nonexistent_skipped(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/missing,{tmp_path}/h")
    assert cc._codex_session_roots() == [tmp_path / "h" / "sessions"]


def test_session_roots_mixed_ordered_and_deduped(cc, tmp_path, monkeypatch):
    (tmp_path / "h" / "sessions").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    # h listed twice → deduped, order preserved.
    monkeypatch.setenv("CODEX_HOME", f"{tmp_path}/h,{tmp_path}/logs,{tmp_path}/h")
    assert cc._codex_session_roots() == [
        tmp_path / "h" / "sessions",
        tmp_path / "logs",
    ]
