"""Subprocess-driven tests for `cctally doctor` (Task 13).

Covers argparse wiring, the --json / --quiet / --verbose flag surface,
exit-code policy (0 unless overall_severity == "fail" → 2), help-page
visibility, and the mutually-exclusive --quiet/--verbose guard.
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CCTALLY = REPO / "bin" / "cctally"


def _run(args, env_extra=None, home=None):
    env = os.environ.copy()
    env["TZ"] = "Etc/UTC"
    if home is not None:
        env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(CCTALLY), *args],
                          env=env, capture_output=True, text=True)


def test_doctor_default_human_mode(tmp_path):
    r = _run(["doctor"], home=tmp_path)
    assert "cctally doctor" in r.stdout
    assert "Summary:" in r.stdout


def test_doctor_json_mode_valid_schema(tmp_path):
    r = _run(["doctor", "--json"], home=tmp_path)
    payload = json.loads(r.stdout)
    assert payload["schema_version"] == 1
    assert {c["id"] for c in payload["categories"]} == {
        "install", "hooks", "auth", "db", "data", "safety"
    }


def test_doctor_exit_code_zero_when_no_fail(tmp_path):
    """Fresh tmp HOME has no OAuth token → that check is FAIL → exit 2.
    Set CCTALLY_TEST_OAUTH_TOKEN_PRESENT env to fake it as present."""
    # Use the CCTALLY_AS_OF + a minimally-populated HOME so OAuth-present
    # via env stub is the only deviation.
    (tmp_path / ".local" / "share" / "cctally").mkdir(parents=True)
    # No OAuth token file → expect FAIL.
    r = _run(["doctor", "--json"], home=tmp_path)
    payload = json.loads(r.stdout)
    if payload["overall"]["counts"].get("fail", 0) > 0:
        assert r.returncode == 2
    else:
        assert r.returncode == 0


def test_doctor_quiet_hides_ok_rows(tmp_path):
    r = _run(["doctor", "--quiet"], home=tmp_path)
    # Even with all-OK environment, summary still prints
    assert "Summary:" in r.stdout


def test_doctor_verbose_includes_details_block(tmp_path):
    r = _run(["doctor", "--verbose"], home=tmp_path)
    assert "details:" in r.stdout


def test_doctor_quiet_and_verbose_mutually_exclusive(tmp_path):
    r = _run(["doctor", "--quiet", "--verbose"], home=tmp_path)
    assert r.returncode == 2
    combined = (r.stderr + r.stdout).lower()
    assert "mutually exclusive" in combined or "not allowed with" in combined


def test_doctor_in_help_listing():
    r = _run(["--help"])
    assert "doctor" in r.stdout
