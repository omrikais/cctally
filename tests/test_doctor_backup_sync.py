"""Read-only doctor backup/sync-root classification."""
from __future__ import annotations

import pathlib
import subprocess
import sys

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN_DIR))

import _cctally_doctor as gather
import _lib_doctor as doctor


def _completed(args, returncode=0, stdout=""):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def test_backup_sync_gather_classifies_cloud_roots_without_external_commands():
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("cloud-root classification must not invoke a tool")

    home = pathlib.Path("/Users/example")
    cases = (
        (
            home / "Library/Mobile Documents/com~apple~CloudDocs/cctally",
            "iCloud Drive",
        ),
        (home / "Dropbox/custom-cctally-data", "Dropbox"),
        (
            home / "Library/CloudStorage/Dropbox-Team/custom-cctally-data",
            "Dropbox",
        ),
    )
    for app_dir, provider in cases:
        state = gather._gather_backup_sync_state(
            app_dir,
            platform_name="darwin",
            home_dir=home,
            which=lambda _name: "/usr/bin/tmutil",
            run=run,
        )
        assert state == {"status": "included", "provider": provider}
    assert calls == []


def test_backup_sync_gather_distinguishes_absent_excluded_and_included():
    home = pathlib.Path("/Users/example")
    app_dir = home / ".local/share/cctally"

    def classify(destination_rc, exclusion):
        commands = []

        def run(args, **kwargs):
            commands.append(args)
            if args[-1] == "destinationinfo":
                stdout = (
                    "Name : Backup\n"
                    if destination_rc == 0
                    else "No destinations configured.\n"
                )
                return _completed(args, destination_rc, stdout)
            return _completed(args, 0, f"[{exclusion}] {app_dir}\n")

        state = gather._gather_backup_sync_state(
            app_dir,
            platform_name="darwin",
            home_dir=home,
            which=lambda _name: "/usr/bin/tmutil",
            run=run,
        )
        return state, commands

    absent, absent_commands = classify(1, "Included")
    excluded, excluded_commands = classify(0, "Excluded")
    included, included_commands = classify(0, "Included")

    assert absent == {"status": "absent", "provider": "Time Machine"}
    assert excluded == {"status": "excluded", "provider": "Time Machine"}
    assert included == {"status": "included", "provider": "Time Machine"}
    assert absent_commands == [["/usr/bin/tmutil", "destinationinfo"]]
    assert excluded_commands == [
        ["/usr/bin/tmutil", "destinationinfo"],
        ["/usr/bin/tmutil", "isexcluded", str(app_dir)],
    ]
    assert included_commands == excluded_commands


def test_backup_sync_gather_is_fail_soft_for_missing_tool_and_non_macos():
    home = pathlib.Path("/Users/example")
    app_dir = home / ".local/share/cctally"

    missing = gather._gather_backup_sync_state(
        app_dir,
        platform_name="darwin",
        home_dir=home,
        which=lambda _name: None,
        run=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
    )
    degraded = gather._gather_backup_sync_state(
        pathlib.Path("/custom/cctally"),
        platform_name="linux",
        home_dir=home,
        which=lambda _name: (_ for _ in ()).throw(AssertionError()),
        run=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
    )
    empty_output = gather._gather_backup_sync_state(
        app_dir,
        platform_name="darwin",
        home_dir=home,
        which=lambda _name: "/usr/bin/tmutil",
        run=lambda args, **_kwargs: _completed(args, 0, ""),
    )

    assert missing == {"status": "unavailable", "provider": "Time Machine"}
    assert degraded == {"status": "unsupported", "provider": None}
    assert empty_output == {"status": "unavailable", "provider": "Time Machine"}


def test_backup_sync_gather_skips_time_machine_tool_for_shallow_gather():
    state = gather._gather_backup_sync_state(
        pathlib.Path("/custom/cctally"),
        platform_name="darwin",
        home_dir=pathlib.Path("/Users/example"),
        probe_time_machine=False,
        which=lambda _name: (_ for _ in ()).throw(AssertionError()),
        run=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
    )

    assert state == {"status": "unavailable", "provider": "Time Machine"}


def test_backup_sync_check_warns_only_for_confirmed_inclusion():
    included = doctor._check_safety_backup_sync(
        doctor.DoctorState(
            **{
                field.name: (
                    {"status": "included", "provider": "Dropbox"}
                    if field.name == "backup_sync_state" else None
                )
                for field in doctor.dataclasses.fields(doctor.DoctorState)
            }
        )
    )
    assert included.severity == "warn"
    assert included.summary == "cctally data is inside Dropbox"
    assert "db backup --db stats" in (included.remediation or "")
    assert "--db cache" in (included.remediation or "")
    assert "/Users/" not in str(included.details)

    for status in ("absent", "excluded", "unavailable", "unsupported"):
        state = {
            field.name: (
                {"status": status, "provider": "Time Machine"}
                if field.name == "backup_sync_state" else None
            )
            for field in doctor.dataclasses.fields(doctor.DoctorState)
        }
        result = doctor._check_safety_backup_sync(doctor.DoctorState(**state))
        assert result.severity == "ok"
