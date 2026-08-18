from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "acceptance-matrix.json"


def _rows() -> dict[str, dict]:
    payload = json.loads(MATRIX.read_text(encoding="utf-8"))
    return {row["id"]: row for row in payload["requirements"]}


def _help(*args: str) -> str:
    env = os.environ.copy()
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    result = subprocess.run(
        [str(ROOT / "bin" / "cctally"), *args, "--help"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def test_final_acceptance_matrix_has_no_deferred_contracts_or_dead_targets():
    rows = _rows()
    deferred = sorted(row_id for row_id, row in rows.items()
                      if row["contractState"] == "deferred")
    assert deferred == []

    dead = sorted(
        (row_id, target)
        for row_id, row in rows.items()
        for target in row["futureTestTargets"]
        if not (ROOT / target).exists()
    )
    assert dead == []


def test_s9_rows_name_the_final_executable_evidence():
    rows = _rows()
    expected = {
        "s5-s8-ui-qa-gates": "supported",
        "root-docs-cover-both-sources": "supported",
        "production-scale-final-certification": "supported",
        "tui-freeze-explicit-disposition": "not_applicable",
    }
    assert {row_id: rows[row_id]["contractState"] for row_id in expected} == expected

    production_targets = set(
        rows["production-scale-final-certification"]["futureTestTargets"]
    )
    assert {
        "tests/test_snapshot_bounded_work.py",
        "tests/test_conversation_assembly_perf.py",
        "tests/test_dashboard_drill_perf.py",
        "tests/test_bench.py",
        "bin/cctally-bench-test",
    } <= production_targets


def test_root_and_setup_help_name_both_providers_and_their_native_contracts():
    root_help = _help()
    assert "Claude and Codex subscription usage" in root_help
    assert "Claude Code hooks" in root_help
    assert "Native Codex handlers" in root_help
    assert "keep Claude and Codex quota percentages" in root_help

    setup_help = _help("setup")
    assert "Claude hook entries" in setup_help
    assert "native Codex handlers" in setup_help


def test_root_documentation_is_cross_provider_and_tui_scope_is_explicit():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Claude Code and/or OpenAI's Codex CLI" in readme
    assert "Claude Code and Codex transcripts" in readme
    assert "Claude-only, bugfix-only" in readme
    assert "not part of cross-provider parity" in readme

    required_docs = {
        "docs/commands/dashboard.md": ("Claude", "Codex", "All", "source selector"),
        "docs/commands/transcript.md": ("Claude", "Codex", "anonymized"),
        "docs/commands/share.md": ("Claude", "Codex", "source"),
        "docs/commands/config.md": ("Claude", "Codex", "Dashboard writable"),
        "docs/commands/setup.md": ("Claude", "Codex", "uninstall"),
        "docs/commands/doctor.md": ("Claude", "Codex", "privacy"),
    }
    for rel, tokens in required_docs.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert all(token in text for token in tokens), (rel, tokens)

    parity = (ROOT / "docs" / "codex-parity.md").read_text(encoding="utf-8")
    assert "S9 certification is complete" in parity
    assert "TUI stays under the approved bugfix-only freeze" in parity


def test_scale_evidence_is_structural_and_non_vacuous():
    bounded = (ROOT / "tests" / "test_snapshot_bounded_work.py").read_text(
        encoding="utf-8"
    )
    assert "bounds WORK, never wall-clock time" in bounded
    assert "total_rows > distinct_files" in bounded
    assert "SCAN files\" not in plan" in bounded

    drill = (ROOT / "tests" / "test_dashboard_drill_perf.py").read_text(
        encoding="utf-8"
    )
    assert "Neither assertion is wall-clock-bound" in drill
    assert "INNER JOIN _drill_paths" in drill
    assert 'fallback_calls["n"] == 0' in drill

    bench = (ROOT / "bench" / "README.md").read_text(encoding="utf-8")
    assert "~300K-entry-class corpus" in bench
    assert "15 benchmarks" in bench
    assert "--scale large" in bench
