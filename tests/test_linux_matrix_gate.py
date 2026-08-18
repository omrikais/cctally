"""Release-blocking local Linux multi-interpreter gate (#595)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import re
import subprocess
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "cctally-test-linux-matrix"


def _load_gate():
    assert SCRIPT.is_file(), "bin/cctally-test-linux-matrix is missing"
    loader = importlib.machinery.SourceFileLoader(
        "_cctally_test_linux_matrix", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_matrix_covers_every_supported_python_on_hosted_linux_shape():
    gate = _load_gate()
    assert gate.PYTHON_VERSIONS == ("3.11", "3.12", "3.13")
    assert gate.CONTAINER_PLATFORM == "linux/arm64"
    assert gate.CONTAINER_CHECKOUT == "/workspace/cctally-dev"
    assert gate.CONTAINER_DISTRO == "trixie"


def test_container_command_carries_the_fidelity_contract(tmp_path):
    gate = _load_gate()
    command = gate._container_command("docker", "3.11", tmp_path)
    rendered = " ".join(command)
    body = gate._container_script("3.11", "deadbeef" * 5)

    assert command[:2] == ["docker", "run"]
    assert "--platform linux/arm64" in rendered
    assert "--tmpfs /tmp:" in rendered
    assert f"{tmp_path}:/source-repo:ro" in rendered
    assert command[-4:-1] == ["python:3.11-trixie", "bash", "-lc"]
    assert command[-1] == body

    # The suite owns a real indexed checkout at the non-shadowing path and
    # runs as a non-root user with a fresh /tmp.
    assert "apt-get install" in body and " git " in body
    assert " locales rsync" in body
    assert "localedef -i en_US -f UTF-8 en_US.UTF-8" in body
    assert "node-v${node_version}-linux-arm64.tar.xz" in body
    assert "sqlite-autoconf-3530300.tar.gz" in body
    assert "c917d7db16648ec95f714974ace5e5dcf46b7dc70e26600a0a102a3141125db0" in body
    assert "SQLITE_ENABLE_DBPAGE_VTAB" in body
    assert "useradd" in body and "runuser -u cctally" in body
    assert 'chown cctally:cctally "$TMPDIR"' in body
    assert "TMPDIR=/opt/cctally-setup-tmp" in body.split(
        "runuser -u cctally -- env", 1
    )[1]
    assert "cp -a /source-repo/. /workspace/cctally-dev/" in body
    assert "git rev-parse HEAD" in body
    assert "git status --porcelain" in body

    # Match the private manual Linux lane's hard capabilities and explicit
    # agentmem boundary rather than silently skipping missing dependencies.
    assert "npm ci" in body
    assert "sqlite3 :memory: '.recover'" in body
    assert "_lib-fts5-probe.sh require python" in body
    assert "GITHUB_ACTIONS=true" in body
    assert "CCTALLY_AUTHORITATIVE_RUN=1" in body
    assert "CCTALLY_AGENTMEM_TEST_POLICY=hosted-private-unavailable" in body
    assert "CCTALLY_LINUX_MATRIX_RUN=1" in body
    assert "CCTALLY_OUTER_JOBS=2" in body
    assert "CCTALLY_PYTEST_JOBS=2" in body
    assert "TZ=Etc/UTC" in body
    assert "bin/cctally-test-all" in body


def test_dirty_tree_refuses_before_container_engine_probe(monkeypatch, tmp_path):
    gate = _load_gate()
    calls: list[list[str]] = []

    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: " M bin/cctally")

    def _unexpected(command, **kwargs):
        calls.append(command)
        raise AssertionError("dirty-tree refusal must precede external commands")

    monkeypatch.setattr(gate, "_run", _unexpected)
    assert gate.main(["--engine", "docker"]) == 2
    assert calls == []


def _stub_successful_matrix(monkeypatch, gate, tmp_path):
    monkeypatch.setattr(gate, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(gate, "_materialize_clean_head", lambda *args: None)
    monkeypatch.setattr(
        gate,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )


def test_focused_python_selection_is_explicitly_incomplete(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main(["--python", "3.11"]) == 3
    captured = capsys.readouterr()
    assert "INCOMPLETE" in captured.err
    assert "release gate" in captured.err


def test_matrix_refuses_if_head_changes_before_pass(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    heads = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(gate, "_git_head", lambda root: next(heads))
    monkeypatch.setattr(gate, "_git_status", lambda root: "")

    assert gate.main([]) == 2
    captured = capsys.readouterr()
    assert "HEAD changed during matrix" in captured.err
    assert "PASS" not in captured.out


def test_matrix_refuses_if_tree_becomes_dirty_before_pass(
    monkeypatch, tmp_path, capsys,
):
    gate = _load_gate()
    _stub_successful_matrix(monkeypatch, gate, tmp_path)
    statuses = iter(("", " M bin/cctally"))
    monkeypatch.setattr(gate, "_git_head", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "_git_status", lambda root: next(statuses))

    assert gate.main([]) == 2
    captured = capsys.readouterr()
    assert "tree changed during matrix" in captured.err
    assert "PASS" not in captured.out


def test_release_skill_makes_the_matrix_blocking_after_gate_zero():
    skill = REPO / ".agents/skills/release-cctally/SKILL.md"
    if not skill.exists():
        pytest.skip("private release skill absent from public mirror")
    text = skill.read_text()
    gate0 = text.index("**Gate 0 —")
    matrix = text.index("**Gate 0.25 —")
    pricing = text.index("**Gate 0.5 —")
    assert gate0 < matrix < pricing
    section = text[matrix:pricing]
    assert "bin/cctally-test-linux-matrix" in section
    assert "release-blocking" in section
    assert "workflow_dispatch" in section


def test_remote_testing_manual_names_the_container_exception_and_boundaries():
    manual = REPO / "docs/remote-testing.md"
    if not manual.exists():
        pytest.skip("private remote-testing manual absent from public mirror")
    text = manual.read_text()
    assert "Local Linux multi-interpreter release gate" in text
    assert re.search(r"Python 3\.11, 3\.12,\s+and 3\.13", text)
    assert "non-root" in text
    assert "CCTALLY_AGENTMEM_TEST_POLICY=hosted-private-unavailable" in text
    assert "workflow_dispatch" in text
