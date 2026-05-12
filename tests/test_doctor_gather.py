"""Gather-layer tests for doctor_gather_state in bin/cctally.

Drives the function via subprocess so the bin/cctally import-time
side effects (path constants, banner machinery) execute in a fresh
process under a fake HOME.
"""
import json
import os
import pathlib
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
CCTALLY = REPO / "bin" / "cctally"


def _run_gather(home: pathlib.Path, *, runtime_bind: "str | None" = None,
                now_iso: str = "2026-05-13T14:22:31+00:00") -> dict:
    """Invoke the in-process gather via a one-liner driver script."""
    driver = textwrap.dedent(f"""
        import sys, json, datetime as dt
        sys.path.insert(0, {str(REPO / 'bin')!r})
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("cctally", {str(CCTALLY)!r})
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        # Register BEFORE exec — dataclass()'s frozen path inspects
        # sys.modules[cls.__module__].__dict__, which would NPE otherwise.
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        st = mod.doctor_gather_state(
            now_utc=dt.datetime.fromisoformat({now_iso!r}),
            runtime_bind={runtime_bind!r},
        )
        # Serialize the dataclass via dataclasses.asdict for assertion.
        import dataclasses
        d = dataclasses.asdict(st)
        # datetimes → isoformat for JSON-safety
        def _norm(v):
            if isinstance(v, dt.datetime):
                return v.isoformat()
            if isinstance(v, dict):
                return {{k: _norm(vv) for k, vv in v.items()}}
            if isinstance(v, list):
                return [_norm(x) for x in v]
            return v
        print(json.dumps(_norm(d), default=str))
    """)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["TZ"] = "Etc/UTC"
    res = subprocess.run([sys.executable, "-c", driver],
                         env=env, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def test_gather_state_fresh_home_returns_state(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "projects").mkdir()
    st = _run_gather(tmp_path)
    assert st["cctally_version"]
    assert st["dashboard_bind_stored"] in ("loopback", "127.0.0.1")
    assert st["runtime_bind"] is None
    assert st["claude_jsonl_present"] is False
    assert st["codex_jsonl_present"] is False


def test_gather_state_with_runtime_bind(tmp_path):
    st = _run_gather(tmp_path, runtime_bind="0.0.0.0")
    assert st["runtime_bind"] == "0.0.0.0"


def test_gather_state_detects_claude_jsonl(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "p1"
    proj.mkdir(parents=True)
    (proj / "session-abc.jsonl").write_text("{}\n")
    st = _run_gather(tmp_path)
    assert st["claude_jsonl_present"] is True


def test_gather_state_corrupt_config_captured(tmp_path):
    cdir = tmp_path / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True)
    (cdir / "config.json").write_text("{not valid json")
    st = _run_gather(tmp_path)
    assert st["config_json_error"]
    assert "json" in st["config_json_error"].lower() or "expecting" in st["config_json_error"].lower()


def test_gather_state_does_not_create_config_json(tmp_path):
    """Spec invariant: doctor MUST NOT write to config.json. Pre-codex bug
    was that load_config() auto-creates on first run."""
    cfg_path = tmp_path / ".local" / "share" / "cctally" / "config.json"
    assert not cfg_path.exists()
    _run_gather(tmp_path)
    assert not cfg_path.exists(), "doctor must not mutate config.json"
