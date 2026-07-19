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
                env_extra: "dict | None" = None,
                pre_call: str = "") -> dict:
    """Invoke the in-process gather via a one-liner driver script.

    If now_iso is None, the driver passes now_utc=None — exercising the
    env-fallback path (CCTALLY_AS_OF or wall-clock) inside the gather.

    pre_call is python source injected after the module loads but before
    doctor_gather_state runs — used to monkeypatch module-level helpers
    (e.g. force `_setup_is_brew_install` to True without a keg copy).
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
        {pre_call}
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
    # Issue #109: the codex_jsonl_present probe now reads $CODEX_HOME. Neutralize
    # a dev's exported value so the default-root probe resolves to <home>/.codex;
    # tests that exercise multi-root opt in by passing CODEX_HOME via env_extra.
    env.pop("CODEX_HOME", None)
    if env_extra:
        env.update(env_extra)
    res = subprocess.run([sys.executable, "-c", driver],
                         env=env, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def test_gather_sets_reachable_and_pinned(tmp_path):
    """Issue #119: doctor_gather_state precomputes two availability
    booleans for the kernel — `cctally_reachable_on_path`
    (`shutil.which("cctally") is not None`) and `symlinks_path_pinned`
    (true iff cctally runs ONLY through a legacy ~/.local/bin link).

    The gather harness scrubs PATH to an empty dir below, so `cctally`
    is not reachable on $PATH and no legacy link exists → reachable is
    False and pinned is False. Both fields must be present with the
    right types regardless."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "projects").mkdir()
    empty_path_dir = tmp_path / "empty-bin"
    empty_path_dir.mkdir()
    st = _run_gather(tmp_path, env_extra={"PATH": str(empty_path_dir)})
    assert "cctally_reachable_on_path" in st
    assert "symlinks_path_pinned" in st
    assert isinstance(st["symlinks_path_pinned"], bool)
    # Empty scrubbed PATH + no legacy link → both False.
    assert st["cctally_reachable_on_path"] is False
    assert st["symlinks_path_pinned"] is False
    # install_is_brew flows from _setup_is_brew_install(repo_root); the test
    # runs the worktree binary (not a keg), so it is present and False.
    assert "install_is_brew" in st
    assert st["install_is_brew"] is False


def test_gather_install_is_brew_true_when_keg(tmp_path):
    """install_is_brew reads `_setup_is_brew_install(repo_root)`. The gather
    subprocess runs the worktree binary (never a keg copy), so force the
    helper True to prove the wiring populates the field rather than
    hardcoding False."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "projects").mkdir()
    st = _run_gather(
        tmp_path,
        pre_call="mod._setup_is_brew_install = lambda *a, **k: True",
    )
    assert st["install_is_brew"] is True


# --- U9: conversation_sessions rollup gather (#217 S1) ----------------------

def _seed_rollup_cache(home: pathlib.Path, *, rollup_rows: int,
                       msg_sessions: int, pending_flag: "str | None" = None):
    """Build a cache.db at <home>/.local/share/cctally/cache.db (via the real
    _apply_cache_schema) with `rollup_rows` conversation_sessions rows and
    `msg_sessions` distinct conversation_messages session_ids, optionally arming
    a `pending_flag` in cache_meta. Mirrors the path the gather reads."""
    import sqlite3
    sys.path.insert(0, str(REPO / "bin"))
    import _cctally_db as db  # noqa: PLC0415
    cdir = home / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cdir / "cache.db"))
    try:
        db._apply_cache_schema(conn)
        for i in range(rollup_rows):
            conn.execute(
                "INSERT INTO conversation_sessions(session_id, msg_count) "
                "VALUES (?, ?)", (f"sess-{i}", 1))
        for i in range(msg_sessions):
            conn.execute(
                "INSERT INTO conversation_messages"
                "(session_id, uuid, source_path, byte_offset, timestamp_utc, "
                " entry_type, text, blocks_json, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (f"sess-{i}", f"u-{i}", "a.jsonl", i,
                 "2026-06-01T00:00:00Z", "human", "hi", "[]"))
        if pending_flag is not None:
            db._set_cache_meta(conn, pending_flag, "1")
        conn.commit()
    finally:
        conn.close()


def _seed_codex_project_metadata_cache(home: pathlib.Path):
    """Seed a retained all-history partition through the real cache schema."""
    import sqlite3
    sys.path.insert(0, str(REPO / "bin"))
    import _cctally_db as db  # noqa: PLC0415
    cdir = home / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cdir / "cache.db"))
    try:
        db._apply_cache_schema(conn)
        conn.execute(
            "INSERT INTO codex_session_entries(source_path, line_offset, timestamp_utc, session_id, model, "
            "total_tokens, source_root_key, conversation_key) VALUES "
            "('/codex/a.jsonl', 1, '2026-05-12T00:00:00Z', 'native-a', 'gpt-test', 1, 'root-a', 'key-a')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_threads(conversation_key, source_root_key, native_thread_id, "
            "root_thread_id, source_path) VALUES ('key-a', 'root-a', 'native-a', 'root-a', '/codex/a.jsonl')"
        )
        conn.execute(
            "INSERT INTO codex_session_entries(source_path, line_offset, timestamp_utc, session_id, model, "
            "total_tokens, source_root_key, conversation_key) VALUES "
            "('/codex/b.jsonl', 1, '2026-05-12T00:01:00Z', 'native-b', 'gpt-test', 1, 'root-a', NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def test_gather_codex_project_metadata_is_all_history_and_identity_safe(tmp_path):
    _seed_codex_project_metadata_cache(tmp_path)
    state = _run_gather(tmp_path)
    assert state["codex_project_metadata_health"] == {
        "total_rows": 2,
        "qualified_rows": 1,
        "missing_conversation_key_rows": 1,
        "missing_thread_join_rows": 0,
    }
    assert state["codex_project_metadata_error"] is None


def test_gather_codex_project_metadata_query_failure_is_explicit(tmp_path):
    """A pre-#312 cache shape is a Doctor FAIL input, never healthy zero."""
    import sqlite3

    cache_dir = tmp_path / ".local" / "share" / "cctally"
    cache_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(cache_dir / "cache.db"))
    try:
        conn.execute("CREATE TABLE codex_session_entries (timestamp_utc TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()

    state = _run_gather(tmp_path)
    assert state["codex_project_metadata_health"] is None
    assert state["codex_project_metadata_error"] == "OperationalError"


def test_gather_rollup_consistent(tmp_path):
    """Equal rollup + distinct-message session counts, no pending flag → the
    three rollup fields populate, equal, and not in progress."""
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    _seed_rollup_cache(tmp_path, rollup_rows=3, msg_sessions=3)
    st = _run_gather(tmp_path)
    assert st["conv_sessions_rollup_count"] == 3
    assert st["conv_messages_distinct_sessions"] == 3
    assert st["conv_rollup_sync_in_progress"] is False


def test_gather_rollup_mismatch_quiescent(tmp_path):
    """Unequal counts, no pending flag, lock free → in_progress False (so the
    kernel WARNs on the genuine drift)."""
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    _seed_rollup_cache(tmp_path, rollup_rows=2, msg_sessions=4)
    st = _run_gather(tmp_path)
    assert st["conv_sessions_rollup_count"] == 2
    assert st["conv_messages_distinct_sessions"] == 4
    assert st["conv_rollup_sync_in_progress"] is False


def test_gather_rollup_in_progress_via_pending_flag(tmp_path):
    """A pending reingest/backfill flag → in_progress True even with a count
    mismatch (the kernel then stays OK)."""
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    _seed_rollup_cache(tmp_path, rollup_rows=2, msg_sessions=4,
                       pending_flag="conversation_sessions_backfill_pending")
    st = _run_gather(tmp_path)
    assert st["conv_rollup_sync_in_progress"] is True


def test_gather_rollup_in_progress_via_held_lock(tmp_path):
    """#218 I-C.4: the NON-BLOCKING cache.db.lock flock probe — a writer holding
    the lock (no pending flag) → in_progress True via the flock branch, NOT the
    pending-flag branch. This is the mirror image of
    test_gather_rollup_mismatch_quiescent: the SAME seed (rollup != msgs, no
    flag) is in_progress False when the lock is FREE, so a held lock is the only
    thing that flips it True — isolating the LOCK_EX|LOCK_NB probe (the branch
    with the theoretical fd/lock-leak risk).

    The lock is held from THIS (parent) process; the gather runs in a subprocess,
    so its probe opens a fresh fd and the cross-process flock conflict is the
    signal. The gather never blocks (LOCK_NB)."""
    import fcntl
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    _seed_rollup_cache(tmp_path, rollup_rows=2, msg_sessions=4)  # no pending flag
    lock_path = tmp_path / ".local" / "share" / "cctally" / "cache.db.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)   # a writer mid-walk holds it
        st = _run_gather(tmp_path)
        assert st["conv_rollup_sync_in_progress"] is True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_gather_rollup_none_when_cache_absent(tmp_path):
    """No cache.db → both counts None, in_progress False (kernel degrades OK)."""
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    st = _run_gather(tmp_path)
    assert st["conv_sessions_rollup_count"] is None
    assert st["conv_messages_distinct_sessions"] is None
    assert st["conv_rollup_sync_in_progress"] is False


def test_gather_state_fresh_home_returns_state(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "projects").mkdir()
    st = _run_gather(tmp_path)
    assert st["cctally_version"]
    assert st["dashboard_bind_stored"] in ("loopback", "127.0.0.1")
    assert st["runtime_bind"] is None
    assert st["claude_jsonl_present"] is False
    assert st["codex_jsonl_present"] is False


def test_gather_state_codex_present_default_root(tmp_path):
    # JSONL under the default ~/.codex/sessions root → present, no $CODEX_HOME.
    sess = tmp_path / ".codex" / "sessions" / "a"
    sess.mkdir(parents=True)
    (sess / "sess-1.jsonl").write_text("{}\n")
    st = _run_gather(tmp_path)
    assert st["codex_jsonl_present"] is True


def test_gather_state_codex_present_via_codex_home_multiroot(tmp_path):
    # Issue #109: with a multi-root $CODEX_HOME, the probe is true when ANY
    # root carries session JSONL — here only the SECOND (non-default) root does.
    empty_root = tmp_path / "root-a"
    (empty_root / "sessions").mkdir(parents=True)
    data_root = tmp_path / "root-b"
    sess = data_root / "sessions" / "p"
    sess.mkdir(parents=True)
    (sess / "sess-2.jsonl").write_text("{}\n")
    # tmp_path/.codex (the default fallback) intentionally absent — proving the
    # probe followed $CODEX_HOME rather than the hardcoded home dir.
    st = _run_gather(tmp_path, env_extra={"CODEX_HOME": f"{empty_root},{data_root}"})
    assert st["codex_jsonl_present"] is True


def test_gather_state_codex_absent_under_custom_codex_home(tmp_path):
    # $CODEX_HOME points at a root with NO session JSONL → absent (the bug:
    # the old probe glob'd ~/.codex/sessions and could miss/misreport this).
    custom = tmp_path / "custom-codex"
    (custom / "sessions").mkdir(parents=True)
    st = _run_gather(tmp_path, env_extra={"CODEX_HOME": str(custom)})
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
    # Scrub PATH (issue #114): _setup_compute_symlink_state now falls back
    # to shutil.which(name) for an empty slot. The driver runs via
    # sys.executable (absolute), so it needs nothing on PATH — but an
    # inherited real PATH would let which() find the host's installed
    # `cctally`, flipping the "missing"-only assertions to "ok". Point
    # PATH at an empty dir so every empty slot stays deterministically
    # "missing".
    empty_path_dir = tmp_path / "empty-bin"
    empty_path_dir.mkdir()
    env["PATH"] = str(empty_path_dir)
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


def test_setup_compute_symlink_state_cross_install(tmp_path):
    """Regression: a symlink that points at a DIFFERENT valid cctally
    install (e.g., user has npm-installed cctally + the source clone
    they're running doctor from) must still report "ok". The strict
    source-equality check produced 0/13 false negatives on every fresh
    `cctally dashboard` launched from the dev tree, even though the
    `~/.local/bin/cctally-*` symlinks were perfectly healthy."""
    other_install = tmp_path / "other-cctally" / "bin"
    other_install.mkdir(parents=True)
    dst_dir = tmp_path / "local-bin"
    dst_dir.mkdir()
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
        dst_dir = pathlib.Path({str(dst_dir)!r})
        other_bin = pathlib.Path({str(other_install)!r})
        # Seed the foreign install with an executable file per SETUP name
        # and symlink each name in dst_dir to it. None of the symlinks
        # point at `repo_root/bin/<name>` — the strict pre-fix check
        # would classify all of them "wrong".
        for name in mod.SETUP_SYMLINK_NAMES:
            target = other_bin / name
            target.write_text("#!/bin/sh\\nexit 0\\n")
            target.chmod(0o755)
            (dst_dir / name).symlink_to(target)
        state = mod._setup_compute_symlink_state(repo_root, dst_dir)
        print(json.dumps(state))
    """)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["TZ"] = "Etc/UTC"
    (tmp_path / "home").mkdir()
    res = subprocess.run([sys.executable, "-c", driver],
                         env=env, capture_output=True, text=True, check=True)
    state_by_name = dict(json.loads(res.stdout))
    for name, st in state_by_name.items():
        assert st == "ok", (name, st)


def test_setup_compute_symlink_state_dangling_symlink(tmp_path):
    """Dangling symlink → "wrong" (not "missing", not "ok"). The
    loose check derives reachability via `resolve(strict=True)` rather
    than path equality, so a target that vanished after install must
    not be misreported as a healthy slot."""
    dst_dir = tmp_path / "local-bin"
    dst_dir.mkdir()
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
        dst_dir = pathlib.Path({str(dst_dir)!r})
        first = mod.SETUP_SYMLINK_NAMES[0]
        (dst_dir / first).symlink_to(pathlib.Path({str(tmp_path)!r}) / "does-not-exist")
        state = dict(mod._setup_compute_symlink_state(repo_root, dst_dir))
        print(json.dumps({{"first": first, "state": state[first]}}))
    """)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["TZ"] = "Etc/UTC"
    (tmp_path / "home").mkdir()
    res = subprocess.run([sys.executable, "-c", driver],
                         env=env, capture_output=True, text=True, check=True)
    payload = json.loads(res.stdout)
    assert payload["state"] == "wrong"
