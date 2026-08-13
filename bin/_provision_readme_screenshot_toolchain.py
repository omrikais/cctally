#!/usr/bin/env python3
"""Provision and validate the maintainer README screenshot toolchain."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile


class ToolchainError(RuntimeError):
    """A dependency action failed with an operator-actionable diagnosis."""


_COMPLETE_MARKER = ".cctally-screenshot-toolchain-complete.json"


def _has_complete_marker(venv: pathlib.Path) -> bool:
    try:
        payload = json.loads((venv / _COMPLETE_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return payload == {"schemaVersion": 2}


def _write_complete_marker(venv: pathlib.Path) -> None:
    fd, raw_path = tempfile.mkstemp(prefix=".complete-", dir=venv)
    path = pathlib.Path(raw_path)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"schemaVersion": 2}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path, venv / _COMPLETE_MARKER)
    finally:
        if path.exists():
            path.unlink()


def _python_for(venv: pathlib.Path) -> pathlib.Path:
    candidates = (
        venv / "bin" / "python3",
        venv / "bin" / "python",
        venv / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ToolchainError(f"venv interpreter missing under {venv}")


_VALIDATE = r'''
import json
import os
import pathlib
import site
import sys

if sys.prefix == sys.base_prefix:
    raise RuntimeError(f"interpreter is not an isolated venv: {sys.executable}")
if not (pathlib.Path(sys.prefix) / "pyvenv.cfg").is_file():
    raise RuntimeError(f"pyvenv.cfg missing under {sys.prefix}")
expected_venv = pathlib.Path(os.environ["CCTALLY_SCREENSHOTS_EXPECTED_VENV"])
if pathlib.Path(sys.prefix).resolve() != expected_venv.resolve():
    raise RuntimeError(
        f"interpreter selected a different venv: {sys.prefix} != {expected_venv}"
    )

sites = [pathlib.Path(value) for value in site.getsitepackages()]
site_path = next((value for value in sites if value.is_dir()), None)
if site_path is None:
    raise RuntimeError(f"venv site-packages missing: {sites}")
expected_site = os.environ.get("CCTALLY_SCREENSHOTS_EXPECTED_SITE")
if expected_site and site_path.resolve() != pathlib.Path(expected_site).resolve():
    raise RuntimeError(
        f"override is not the selected venv site-packages: {expected_site} != {site_path}"
    )

import rich.style as rich_style
import playwright.sync_api as playwright_sync_api

for dependency, module in (
    ("rich.style", rich_style),
    ("playwright.sync_api", playwright_sync_api),
):
    module_path = pathlib.Path(module.__file__).resolve()
    if not module_path.is_relative_to(site_path.resolve()):
        raise RuntimeError(
            f"{dependency} resolved outside selected site-packages: {module_path}"
        )

browser = pathlib.Path()
if os.environ.get("CCTALLY_SCREENSHOTS_REQUIRE_BROWSER") == "1":
    with playwright_sync_api.sync_playwright() as playwright:
        browser = pathlib.Path(playwright.chromium.executable_path)
    if not browser.is_file() or not os.access(browser, os.X_OK):
        raise RuntimeError(f"matching Chromium executable missing: {browser}")

print(json.dumps({
    "python": sys.executable,
    "sitePackages": str(site_path),
    "browser": str(browser),
}, sort_keys=True))
'''


def _validate(
    venv: pathlib.Path,
    browser_root: pathlib.Path,
    *,
    pythonpath: pathlib.Path | None = None,
    require_browser: bool = True,
) -> dict[str, str]:
    python = _python_for(venv)
    env = dict(os.environ)
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
        env.pop("CCTALLY_SCREENSHOTS_EXPECTED_SITE", None)
    else:
        env["PYTHONPATH"] = str(pythonpath)
        env["CCTALLY_SCREENSHOTS_EXPECTED_SITE"] = str(pythonpath)
    env["CCTALLY_SCREENSHOTS_EXPECTED_VENV"] = str(venv)
    env["CCTALLY_SCREENSHOTS_REQUIRE_BROWSER"] = "1" if require_browser else "0"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    result = subprocess.run(
        [str(python), "-c", _VALIDATE],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exit {result.returncode}"
        raise ToolchainError(reason)
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ToolchainError(f"validation returned malformed output: {exc}") from exc
    if not isinstance(payload, dict):
        raise ToolchainError("validation returned a non-object result")
    return {str(key): str(value) for key, value in payload.items()}


def _run_action(label: str, command: list[str], *, env: dict[str, str]) -> None:
    # stdout is the helper's machine-readable JSON channel. Keep installer
    # progress visible to the operator without allowing it into that channel.
    result = subprocess.run(command, env=env, stdout=sys.stderr)
    if result.returncode != 0:
        raise ToolchainError(
            f"{label} failed (exit {result.returncode}): "
            + shlex.join(command)
        )


def _install_phase(
    phase: str, venv: pathlib.Path, browser_root: pathlib.Path
) -> None:
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    test_installer = env.get("README_SCREENSHOTS_TOOLCHAIN_INSTALLER")
    if test_installer:
        if env.get("README_SCREENSHOTS_SELFTEST") != "toolchain":
            raise ToolchainError(
                "README_SCREENSHOTS_TOOLCHAIN_INSTALLER is only valid with "
                "README_SCREENSHOTS_SELFTEST=toolchain"
            )
        _run_action(
            "stub package/browser installation",
            [test_installer, phase, str(venv), str(browser_root)],
            env=env,
        )
        return

    python = _python_for(venv)
    if phase == "packages":
        _run_action(
            "Python package installation",
            [str(python), "-m", "pip", "install", "--upgrade", "rich", "playwright"],
            env=env,
        )
    elif phase == "browser":
        _run_action(
            "Playwright Chromium installation",
            [str(python), "-m", "playwright", "install", "chromium"],
            env=env,
        )
    else:
        raise ToolchainError(f"unknown installation phase: {phase}")


def _provision_locked(venv: pathlib.Path, browser_root: pathlib.Path) -> dict[str, str]:
    replace_existing = False
    if venv.exists():
        if not _has_complete_marker(venv):
            replace_existing = True
        else:
            try:
                result = _validate(venv, browser_root)
            except ToolchainError:
                replace_existing = True
            else:
                result["status"] = "reused"
                return result

    venv.parent.mkdir(parents=True, exist_ok=True)
    browser_root.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{venv.name}.partial-", dir=venv.parent)
    )
    backup: pathlib.Path | None = None
    published = False
    published_complete = False
    try:
        shutil.rmtree(staging)
        _run_action(
            "isolated venv creation",
            [sys.executable, "-m", "venv", str(staging)],
            env=dict(os.environ),
        )
        _install_phase("packages", staging, browser_root)
        _validate(staging, browser_root, require_browser=False)

        if replace_existing:
            backup = pathlib.Path(
                tempfile.mkdtemp(
                    prefix=f".{venv.name}.replaced-", dir=venv.parent
                )
            )
            backup.rmdir()
            os.replace(venv, backup)
        os.replace(staging, venv)
        published = True
        # Playwright records the absolute package path in its shared browser
        # registry. Install the browser only after the venv has its durable
        # name, while the lock and absent completion marker keep it private.
        _install_phase("browser", venv, browser_root)
        result = _validate(venv, browser_root)
        _write_complete_marker(venv)
        published_complete = True
        result["status"] = "provisioned"
        return result
    except Exception:
        if published and venv.exists():
            # Never discard the backup if removal fails. Leaving both exact
            # paths for inspection is safer than deleting the only prior state.
            shutil.rmtree(venv)
        if backup is not None and backup.exists():
            if not venv.exists():
                os.replace(backup, venv)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if published_complete and backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _provision(venv: pathlib.Path, browser_root: pathlib.Path) -> dict[str, str]:
    venv.parent.mkdir(parents=True, exist_ok=True)
    lock_path = venv.with_name(f"{venv.name}.lock")
    with lock_path.open("a+") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return _provision_locked(venv, browser_root)


def _validate_override(
    pythonpath: str, browser_root: pathlib.Path
) -> dict[str, str]:
    entries = [value for value in pythonpath.split(os.pathsep) if value]
    if len(entries) != 1:
        raise ToolchainError(
            "SCREENSHOTS_PYTHONPATH must name one venv site-packages directory"
        )
    site_path = pathlib.Path(entries[0]).expanduser().resolve()
    if not site_path.is_dir():
        raise ToolchainError(
            f"SCREENSHOTS_PYTHONPATH directory missing: {site_path}"
        )
    venv = next(
        (parent for parent in (site_path, *site_path.parents)
         if (parent / "pyvenv.cfg").is_file()),
        None,
    )
    if venv is None:
        raise ToolchainError(
            "SCREENSHOTS_PYTHONPATH is not inside an isolated venv "
            f"(pyvenv.cfg not found): {site_path}"
        )
    result = _validate(venv, browser_root, pythonpath=site_path)
    result["status"] = "override"
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", required=True, type=pathlib.Path)
    parser.add_argument("--browser-root", required=True, type=pathlib.Path)
    parser.add_argument("--pythonpath")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.pythonpath:
            result = _validate_override(
                args.pythonpath, args.browser_root.expanduser()
            )
        else:
            result = _provision(
                args.venv.expanduser(), args.browser_root.expanduser()
            )
    except (OSError, ToolchainError) as exc:
        if args.pythonpath:
            pipeline = pathlib.Path(__file__).resolve().with_name(
                "build-readme-screenshots.sh"
            )
            recovery = shlex.join(
                ["env", "-u", "SCREENSHOTS_PYTHONPATH", str(pipeline)]
            )
        else:
            recovery = shlex.join(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve()),
                    "--venv",
                    str(args.venv),
                    "--browser-root",
                    str(args.browser_root),
                ]
            )
        print(
            f"readme screenshot toolchain: {exc}\n"
            f"  recovery: {recovery}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
