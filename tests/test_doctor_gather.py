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
                now_iso: "str | None" = "2026-05-13T14:22:31+00:00",
                env_extra: "dict | None" = None) -> dict:
    """Invoke the in-process gather via a one-liner driver script.

    If now_iso is None, the driver passes now_utc=None — exercising the
    env-fallback path (CCTALLY_AS_OF or wall-clock) inside the gather.
    """
    if now_iso is None:
        now_arg = "None"
    else:
        now_arg = f"dt.datetime.fromisoformat({now_iso!r})"
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
            now_utc={now_arg},
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
    if env_extra:
        env.update(env_extra)
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


def test_gather_state_honors_cctally_as_of_env(tmp_path):
    """When now_utc is not passed, the gather routes through _now_utc()
    and must honor the CCTALLY_AS_OF env hook (same precedent as
    `cctally project`, `cctally weekly`, share-render). Bypassing the
    env path would leave the diagnostic non-deterministic for fixture
    tests."""
    st = _run_gather(
        tmp_path,
        now_iso=None,  # exercise the env-fallback branch
        env_extra={"CCTALLY_AS_OF": "2026-05-13T12:34:56Z"},
    )
    assert st["now_utc"] == "2026-05-13T12:34:56+00:00"


def test_setup_compute_symlink_state_helper(tmp_path):
    """Direct unit test of the extracted _setup_compute_symlink_state
    helper (shared by _setup_status and doctor_gather_state). Covers
    the three state buckets: ok / wrong / missing."""
    driver = textwrap.dedent(f"""
        import sys, json, pathlib, os
        sys.path.insert(0, {str(REPO / 'bin')!r})
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("cctally", {str(CCTALLY)!r})
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)
        repo_root = mod._setup_resolve_repo_root()
        dst_dir = pathlib.Path({str(tmp_path)!r})
        # State 1: missing — empty dst_dir, all entries report "missing".
        missing = mod._setup_compute_symlink_state(repo_root, dst_dir)
        # State 2: ok — create one valid symlink to the first SETUP name.
        first = mod.SETUP_SYMLINK_NAMES[0]
        src = mod._setup_resolve_symlink_source(repo_root, first)
        (dst_dir / first).symlink_to(src)
        # State 3: wrong — create a regular file with the second name.
        second = mod.SETUP_SYMLINK_NAMES[1] if len(mod.SETUP_SYMLINK_NAMES) > 1 else None
        if second is not None:
            (dst_dir / second).write_text("not a symlink")
        present = mod._setup_compute_symlink_state(repo_root, dst_dir)
        out = {{
            "names": list(mod.SETUP_SYMLINK_NAMES),
            "missing": missing,
            "present": present,
        }}
        print(json.dumps(out))
    """)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["TZ"] = "Etc/UTC"
    (tmp_path / "home").mkdir()
    res = subprocess.run([sys.executable, "-c", driver],
                         env=env, capture_output=True, text=True, check=True)
    payload = json.loads(res.stdout)
    names = payload["names"]
    # Missing-only run: every entry's state is "missing".
    for name, state in payload["missing"]:
        assert state == "missing", (name, state)
    # Present run: first → ok; second (if any) → wrong; rest missing.
    state_by_name = dict(payload["present"])
    assert state_by_name[names[0]] == "ok"
    if len(names) > 1:
        assert state_by_name[names[1]] == "wrong"
    for name in names[2:]:
        assert state_by_name[name] == "missing"
