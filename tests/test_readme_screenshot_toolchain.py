"""Hermetic lifecycle tests for the README screenshot toolchain (#505).

The real pipeline entrypoint is exercised through its ``toolchain`` self-test.
Package and browser installation are replaced at the external installer
boundary; venv creation, interpreter discovery, imports, validation, locking,
and publication remain production behavior.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bin" / "build-readme-screenshots.sh"
HELPER = REPO_ROOT / "bin" / "_provision_readme_screenshot_toolchain.py"


FAKE_INSTALLER = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import time

if len(sys.argv) == 3:
    phase = "all"
    venv = pathlib.Path(sys.argv[1])
    browser_root = pathlib.Path(sys.argv[2])
else:
    phase = sys.argv[1]
    venv = pathlib.Path(sys.argv[2])
    browser_root = pathlib.Path(sys.argv[3])
log = pathlib.Path(os.environ["README_SCREENSHOTS_INSTALL_LOG"])
with log.open("a") as fh:
    fh.write(json.dumps({
        "phase": phase,
        "venv": str(venv),
        "browserRoot": str(browser_root),
    }) + "\n")
if os.environ.get("README_SCREENSHOTS_INSTALL_STDOUT"):
    print("fake installer progress on stdout", flush=True)

delay = float(os.environ.get("README_SCREENSHOTS_INSTALL_DELAY", "0"))
if delay:
    time.sleep(delay)

python = venv / "bin" / "python3"
site = pathlib.Path(subprocess.check_output(
    [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
    text=True,
).strip())

if phase in {"all", "packages"}:
    (site / "rich").mkdir(parents=True, exist_ok=True)
    (site / "rich" / "__init__.py").write_text("")
    (site / "rich" / "partial.txt").write_text("installer reached package stage")
    if os.environ.get("README_SCREENSHOTS_INSTALL_MODE") == "fail":
        sys.exit(23)
    (site / "rich" / "style.py").write_text("class Style: pass\n")

if phase in {"all", "packages"}:
    (site / "playwright" / "driver" / "package").mkdir(
        parents=True, exist_ok=True
    )
    (site / "playwright" / "__init__.py").write_text("")
    (site / "playwright" / "sync_api.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "class _Manager:\n"
        "    def __enter__(self):\n"
        "        executable = Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']) / "
        "'chromium_headless_shell-test' / 'headless_shell'\n"
        "        if os.environ.get('README_SCREENSHOTS_INSTALL_MODE') == "
        "'nonrelocatable' and '.partial-' not in sys.prefix:\n"
        "            executable = executable.with_name('missing-after-publication')\n"
        "        chromium = type('Chromium', (), {'executable_path': str(executable)})()\n"
        "        return type('Playwright', (), {'chromium': chromium})()\n"
        "    def __exit__(self, *args): return False\n"
        "def sync_playwright(): return _Manager()\n"
    )

if phase in {"all", "browser"}:
    browser = browser_root / "chromium_headless_shell-test" / "headless_shell"
    browser.parent.mkdir(parents=True, exist_ok=True)
    browser.write_text("#!/bin/sh\nexit 0\n")
    browser.chmod(0o755)
    registry = browser_root / ".links" / "fake-registry-link"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(str((site / "playwright" / "driver" / "package").resolve()))
'''


def _write_installer(tmp_path: Path) -> Path:
    installer = tmp_path / "fake-installer.py"
    installer.write_text(FAKE_INSTALLER)
    installer.chmod(0o755)
    return installer


def _toolchain_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "README_SCREENSHOTS_SELFTEST": "toolchain",
            "SCREENSHOTS_VENV_DIR": str(tmp_path / "toolchain"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "browsers"),
            "README_SCREENSHOTS_TOOLCHAIN_INSTALLER": str(
                _write_installer(tmp_path)
            ),
            "README_SCREENSHOTS_INSTALL_LOG": str(tmp_path / "install.log"),
        }
    )
    env.pop("SCREENSHOTS_PYTHONPATH", None)
    return env


def _run_toolchain(env: dict[str, str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fields(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def test_missing_owned_toolchain_is_provisioned_before_capture(tmp_path: Path):
    """Removing the owned venv must trigger a complete validated provision."""
    env = _toolchain_env(tmp_path)

    result = _run_toolchain(env)

    assert result.returncode == 0, result.stderr
    fields = _fields(result.stdout)
    assert fields["toolchain"] == "provisioned"
    assert Path(fields["python"]).is_file()
    assert Path(fields["site-packages"]).is_dir()
    assert Path(fields["browser"]).is_file()


def test_directory_without_pyvenv_cfg_is_repaired_atomically(tmp_path: Path):
    """A residual directory must not count as a venv or block self-repair."""
    env = _toolchain_env(tmp_path)
    venv = Path(env["SCREENSHOTS_VENV_DIR"])
    venv.mkdir(parents=True)
    (venv / "torn-install.txt").write_text("not a venv")

    result = _run_toolchain(env)

    assert result.returncode == 0, result.stderr
    fields = _fields(result.stdout)
    assert fields["toolchain"] == "provisioned"
    assert (venv / "pyvenv.cfg").is_file()
    assert not (venv / "torn-install.txt").exists()


def test_valid_warm_toolchain_short_circuits_without_reinstall(tmp_path: Path):
    """A fully valid cache must converge without invoking installation again."""
    env = _toolchain_env(tmp_path)
    first = _run_toolchain(env)
    assert first.returncode == 0, first.stderr
    install_log = Path(env["README_SCREENSHOTS_INSTALL_LOG"])
    installs_after_first = install_log.read_text()

    second = _run_toolchain(env)

    assert second.returncode == 0, second.stderr
    assert _fields(second.stdout)["toolchain"] == "reused"
    assert install_log.read_text() == installs_after_first


def test_selected_interpreter_must_execute_the_selected_venv(tmp_path: Path):
    """A wrapper dispatching to another venv must not satisfy validation."""
    selected_root = tmp_path / "selected"
    external_root = tmp_path / "external"
    selected_root.mkdir()
    external_root.mkdir()
    selected_env = _toolchain_env(selected_root)
    external_env = _toolchain_env(external_root)
    selected = _run_toolchain(selected_env)
    external = _run_toolchain(external_env)
    assert selected.returncode == 0, selected.stderr
    assert external.returncode == 0, external.stderr

    selected_python = Path(_fields(selected.stdout)["python"])
    external_python = Path(_fields(external.stdout)["python"])
    selected_python.unlink()
    selected_python.write_text(
        f"#!/bin/sh\nexec {shlex.quote(str(external_python))} \"$@\"\n"
    )
    selected_python.chmod(0o755)

    repaired = _run_toolchain(selected_env)

    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"
    assert Path(_fields(repaired.stdout)["python"]).is_relative_to(
        Path(selected_env["SCREENSHOTS_VENV_DIR"]).resolve()
    )


def test_imports_must_resolve_inside_the_selected_site_packages(tmp_path: Path):
    """A .pth escape to another venv must not satisfy package validation."""
    selected_root = tmp_path / "selected"
    external_root = tmp_path / "external"
    selected_root.mkdir()
    external_root.mkdir()
    selected_env = _toolchain_env(selected_root)
    external_env = _toolchain_env(external_root)
    selected = _run_toolchain(selected_env)
    external = _run_toolchain(external_env)
    assert selected.returncode == 0, selected.stderr
    assert external.returncode == 0, external.stderr

    selected_site = Path(_fields(selected.stdout)["site-packages"])
    external_site = Path(_fields(external.stdout)["site-packages"])
    shutil.rmtree(selected_site / "rich")
    shutil.rmtree(selected_site / "playwright")
    (selected_site / "external-toolchain.pth").write_text(str(external_site) + "\n")

    repaired = _run_toolchain(selected_env)

    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"
    repaired_site = Path(_fields(repaired.stdout)["site-packages"])
    assert (repaired_site / "rich" / "style.py").is_file()
    assert (repaired_site / "playwright" / "sync_api.py").is_file()


def test_incomplete_rich_package_is_rebuilt_not_reused(tmp_path: Path):
    """A venv whose Rich import is torn must be replaced as one unit."""
    env = _toolchain_env(tmp_path)
    first = _run_toolchain(env)
    assert first.returncode == 0, first.stderr
    site = Path(_fields(first.stdout)["site-packages"])
    (site / "rich" / "style.py").unlink()

    repaired = _run_toolchain(env)

    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"
    assert (Path(_fields(repaired.stdout)["site-packages"]) / "rich" / "style.py").is_file()


def test_missing_playwright_browser_revision_is_rebuilt(tmp_path: Path):
    """Imports alone must not make a missing matching browser look healthy."""
    env = _toolchain_env(tmp_path)
    first = _run_toolchain(env)
    assert first.returncode == 0, first.stderr
    Path(_fields(first.stdout)["browser"]).unlink()

    repaired = _run_toolchain(env)

    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"
    assert Path(_fields(repaired.stdout)["browser"]).is_file()


def test_browser_registry_is_written_for_the_durable_venv_path(tmp_path: Path):
    """Playwright's shared-cache registry must not retain a staging path."""
    env = _toolchain_env(tmp_path)

    result = _run_toolchain(env)

    assert result.returncode == 0, result.stderr
    registry = Path(env["PLAYWRIGHT_BROWSERS_PATH"]) / ".links" / "fake-registry-link"
    target = Path(registry.read_text())
    assert target.is_dir()
    assert target.is_relative_to(Path(env["SCREENSHOTS_VENV_DIR"]).resolve())


def test_interrupted_install_is_not_published_and_next_run_repairs(tmp_path: Path):
    """Failure after partial package writes must leave no discoverable venv."""
    env = _toolchain_env(tmp_path)
    env["README_SCREENSHOTS_INSTALL_MODE"] = "fail"

    interrupted = _run_toolchain(env)

    assert interrupted.returncode == 1
    assert "stub package/browser installation failed (exit 23)" in interrupted.stderr
    assert "recovery:" in interrupted.stderr
    assert not Path(env["SCREENSHOTS_VENV_DIR"]).exists()

    env.pop("README_SCREENSHOTS_INSTALL_MODE")
    repaired = _run_toolchain(env)
    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"


def test_failed_validation_after_publish_rolls_back_visibility(tmp_path: Path):
    """A staging-only success must not leave the final owned path discoverable."""
    env = _toolchain_env(tmp_path)
    env["README_SCREENSHOTS_INSTALL_MODE"] = "nonrelocatable"

    failed = _run_toolchain(env)

    assert failed.returncode == 1
    assert "matching Chromium executable missing" in failed.stderr
    assert not Path(env["SCREENSHOTS_VENV_DIR"]).exists()

    env.pop("README_SCREENSHOTS_INSTALL_MODE")
    repaired = _run_toolchain(env)
    assert repaired.returncode == 0, repaired.stderr
    assert _fields(repaired.stdout)["toolchain"] == "provisioned"


def test_concurrent_provisioners_publish_once_and_both_converge(tmp_path: Path):
    """Two cold callers must serialize instead of racing partial publications."""
    env = _toolchain_env(tmp_path)
    env["README_SCREENSHOTS_INSTALL_DELAY"] = "0.75"
    processes = [
        subprocess.Popen(
            [str(SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        results.append((process.returncode, stdout, stderr))

    assert [result[0] for result in results] == [0, 0], results
    assert sorted(_fields(result[1])["toolchain"] for result in results) == [
        "provisioned",
        "reused",
    ]
    install_lines = Path(env["README_SCREENSHOTS_INSTALL_LOG"]).read_text().splitlines()
    assert len(install_lines) == 2
    assert [json.loads(line)["phase"] for line in install_lines] == [
        "packages",
        "browser",
    ]


def test_valid_pythonpath_override_is_validated_without_mutation(tmp_path: Path):
    """The operator override must reuse its venv and never provision owned state."""
    seed_env = _toolchain_env(tmp_path)
    seeded = _run_toolchain(seed_env)
    assert seeded.returncode == 0, seeded.stderr
    seeded_fields = _fields(seeded.stdout)
    install_log = Path(seed_env["README_SCREENSHOTS_INSTALL_LOG"])
    installs_before_override = install_log.read_text()

    override_env = dict(seed_env)
    override_env["SCREENSHOTS_PYTHONPATH"] = seeded_fields["site-packages"]
    override_env["SCREENSHOTS_VENV_DIR"] = str(tmp_path / "must-not-exist")
    overridden = _run_toolchain(override_env)

    assert overridden.returncode == 0, overridden.stderr
    assert _fields(overridden.stdout)["toolchain"] == "override"
    assert _fields(overridden.stdout)["python"] == seeded_fields["python"]
    assert not Path(override_env["SCREENSHOTS_VENV_DIR"]).exists()
    assert install_log.read_text() == installs_before_override


def test_invalid_pythonpath_override_fails_with_nonmutating_recovery(tmp_path: Path):
    """An operator-owned non-venv path must fail without provisioning around it."""
    env = _toolchain_env(tmp_path)
    override = tmp_path / "operator-path"
    override.mkdir()
    (override / "keep.txt").write_text("operator owned")
    env["SCREENSHOTS_PYTHONPATH"] = str(override)

    result = _run_toolchain(env)

    assert result.returncode == 1
    assert "SCREENSHOTS_PYTHONPATH is not inside an isolated venv" in result.stderr
    assert "recovery: env -u SCREENSHOTS_PYTHONPATH" in result.stderr
    assert str(SCRIPT) in result.stderr
    assert (override / "keep.txt").read_text() == "operator owned"
    assert not Path(env["SCREENSHOTS_VENV_DIR"]).exists()
    assert not Path(env["README_SCREENSHOTS_INSTALL_LOG"]).exists()


def test_installer_stub_is_refused_outside_toolchain_selftest(tmp_path: Path):
    """The no-download seam must not be usable by the real capture pipeline."""
    env = _toolchain_env(tmp_path)
    env.pop("README_SCREENSHOTS_SELFTEST")

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--venv",
            env["SCREENSHOTS_VENV_DIR"],
            "--browser-root",
            env["PLAYWRIGHT_BROWSERS_PATH"],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "only valid with README_SCREENSHOTS_SELFTEST=toolchain" in result.stderr
    assert not Path(env["SCREENSHOTS_VENV_DIR"]).exists()


def test_installer_progress_cannot_contaminate_toolchain_json(tmp_path: Path):
    """Real pip stdout must remain visible without entering the JSON handoff."""
    env = _toolchain_env(tmp_path)
    env["README_SCREENSHOTS_INSTALL_STDOUT"] = "1"

    result = _run_toolchain(env)

    assert result.returncode == 0, result.stderr
    assert _fields(result.stdout)["toolchain"] == "provisioned"
    assert "fake installer progress on stdout" in result.stderr
