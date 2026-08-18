from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "bin" / "build-web-fixture.py"
COMMITTED_FIXTURE = (
    ROOT / "dashboard" / "web" / "__tests__" / "fixtures" / "envelope.json"
)


def test_generator_refuses_to_drop_existing_fixture_keys_without_explicit_consent(
    tmp_path: Path,
) -> None:
    """Removing the pre-write key check would overwrite the shared fixture."""
    output = tmp_path / "envelope.json"
    shutil.copyfile(COMMITTED_FIXTURE, output)
    before = output.read_bytes()

    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert output.read_bytes() == before
    assert "refusing to remove existing top-level keys" in result.stderr
    for key in (
        "blocks",
        "cache_report",
        "daily",
        "default_source",
        "monthly",
        "source_order",
        "source_schema_version",
        "sources",
        "weekly",
    ):
        assert key in result.stderr
    assert "--allow-key-removal" in result.stderr


def test_generator_allows_an_explicit_lossy_rebuild(tmp_path: Path) -> None:
    """The safety gate must retain a deliberate escape for fixture reconstruction."""
    output = tmp_path / "envelope.json"
    shutil.copyfile(COMMITTED_FIXTURE, output)

    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--out",
            str(output),
            "--allow-key-removal",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    rebuilt = json.loads(output.read_text())
    assert rebuilt["envelope_version"] == 2
    assert "sources" not in rebuilt
