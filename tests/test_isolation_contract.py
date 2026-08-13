"""The pytest half of the estate's isolation contract (#529 S4).

`bin/_lib-harness-env.sh` neutralizes six environment variables for every
shell harness. This module carries the pytest half of that same contract, plus
the fail-closed production-write detector that enforces preserve-item 3.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_ASSIGNED = {
    "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
    "CCTALLY_DISABLE_UPDATE_CHECK": "1",
    "CCTALLY_DISABLE_RETENTION_SWEEP": "1",
}
_REMOVED = ("CODEX_HOME", "DO_NOT_TRACK", "CCTALLY_DISABLE_TELEMETRY")

_PROBE = r'''
import json, os, sys
# HALF ONE: prove the hostile values were actually inherited. Without this the
# whole test passes vacuously when the parent forgot to set them.
inherited = {k: os.environ.get(k) for k in %(all)r}
# HALF TWO: prove _cctally_core has not yet consumed them. Asserting the
# normalized environment AFTER the fact says nothing about path constants
# computed at import, which rewriting os.environ does not rebuild.
core_preloaded = "_cctally_core" in sys.modules
sys.path.insert(0, %(tests)r)
import conftest  # noqa: F401  -- the pin runs at import
after = {k: os.environ.get(k) for k in %(all)r}
sys.path.insert(0, %(bin)r)
import _cctally_core
app_dir = str(_cctally_core.APP_DIR)
print(json.dumps({"inherited": inherited, "core_preloaded": core_preloaded,
                  "after": after, "app_dir": app_dir}))
'''


def test_conftest_normalizes_all_six_preamble_variables(tmp_path):
    hostile = {
        "CCTALLY_DISABLE_DEV_AUTODETECT": "0",
        "CCTALLY_DISABLE_UPDATE_CHECK": "0",
        "CCTALLY_DISABLE_RETENTION_SWEEP": "0",
        "CODEX_HOME": str(tmp_path / "hostile-codex"),
        "DO_NOT_TRACK": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
    }
    names = tuple(_ASSIGNED) + _REMOVED
    src = _PROBE % {"all": names, "tests": str(REPO_ROOT / "tests"),
                    "bin": str(REPO_ROOT / "bin")}
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TZ": "Etc/UTC", **hostile}
    proc = subprocess.run([sys.executable, "-c", src], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    # The child really did start hostile.
    assert got["inherited"] == hostile, "the probe never inherited the hostile values"
    # _cctally_core had not been imported before conftest ran, so the pin
    # cannot have been applied after the constants were derived.
    assert got["core_preloaded"] is False

    for name, value in _ASSIGNED.items():
        assert got["after"][name] == value, f"{name} was not pinned"
    for name in _REMOVED:
        assert got["after"][name] is None, f"{name} was not removed"
    # And the pin had its intended effect: the prod layout, not cctally-dev.
    assert got["app_dir"].endswith("/.local/share/cctally")


# ---------------------------------------------------------------------------
# The statusline cache seam (#529 S4, spec §5.4)
# ---------------------------------------------------------------------------


def test_the_statusline_cache_path_is_redirectable(tmp_path, monkeypatch):
    """bin/_cctally_refresh.py bound the path as a DEFAULT ARGUMENT, fixed at
    import, so patching the module constant could not redirect the deletion --
    the ten per-callsite stubs were the only defence. Resolve at call time.
    """
    import _cctally_core
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    target = pathlib.Path(_cctally_core.STATUSLINE_OAUTH_CACHE_PATH)
    assert tmp_path in target.parents, "redirect_paths did not pin the statusline cache"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    state = ns["_bust_statusline_cache"]()
    assert not target.exists(), "the bust did not act on the redirected path"
    assert state == "busted"

    # Name the defect directly, so a reintroduced default argument is RED even
    # if some future refactor stops the behavioural half above from observing
    # it. A default binds at import; only None can mean "resolve at call time".
    import inspect
    assert inspect.signature(
        ns["_bust_statusline_cache"]
    ).parameters["path"].default is None, (
        "path is bound as a default argument again; a default binds at import "
        "and cannot be redirected by patching either constant"
    )


def test_the_production_default_is_unchanged():
    """The file is genuinely shared with the real Claude Code statusline, so
    relocating it would change behaviour for real users.

    Asked of a FRESH interpreter rather than of importlib.reload: a reload
    re-runs _init_paths_from_env() against whatever HOME the worker currently
    carries and rebinds every path constant in a module the whole worker shares,
    which is a side effect this assertion does not need.
    """
    probe = (
        "import sys; sys.path.insert(0, %r); import _cctally_core; "
        "print(_cctally_core.STATUSLINE_OAUTH_CACHE_PATH)" % str(REPO_ROOT / "bin")
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/tmp/claude-statusline-usage-cache.json"


# ===========================================================================
# The write detector (#529 S4, spec sections 5.2 / 5.3, RED matrix 7.1)
# ===========================================================================
#
# NO TEST IN THIS SECTION MAY WRITE TO A REAL GUARDED PATH, including the tests
# of the detector itself. Every behavioural case drives a SYNTHETIC root
# injected under tmp_path; the real root set is asserted by identity, not by
# writing into it.

import os as _os  # noqa: E402  -- deliberate, the predicate tests read O_* flags


# --- pure predicates -------------------------------------------------------


def test_write_flags_recognises_a_plain_write():
    import _lib_test_isolation as iso
    assert iso.is_write_flags(_os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC)


def test_write_flags_recognises_a_raw_os_open():
    """VERIFIED BY EXECUTION: `os.open` raises `open` with mode=None and only
    the flags integer meaningful. A predicate reading the MODE STRING misses
    every such open, and the estate uses `os.open` widely -- the detector's own
    `_append_json_line` is one caller.
    """
    import _lib_test_isolation as iso
    assert iso.is_write_flags(_os.O_RDWR | _os.O_CREAT | _os.O_EXCL)


def test_write_flags_recognises_a_bare_read_write_open():
    """O_RDWR is 2 and O_WRONLY is 1, so `flags & O_WRONLY` reads False for
    O_RDWR. The access mode is the low two bits and has to be extracted."""
    import _lib_test_isolation as iso
    assert iso.is_write_flags(_os.O_RDWR)


def test_write_flags_rejects_a_read():
    import _lib_test_isolation as iso
    assert not iso.is_write_flags(_os.O_RDONLY)


def test_write_flags_never_consults_a_mode_string():
    """The predicate takes an integer. Handing it a mode string must not make
    it guess, because that is the API shape that produced the SQLite blind
    spot in the first place."""
    import _lib_test_isolation as iso
    assert not iso.is_write_flags("w")
    assert not iso.is_write_flags(None)


def test_a_read_only_sqlite_uri_is_not_a_write():
    import _lib_test_isolation as iso
    assert not iso.is_writable_sqlite_target("file:/x/y.db?mode=ro")
    assert not iso.is_writable_sqlite_target("file:/x/y.db?immutable=1")


def test_a_plain_sqlite_path_is_treated_as_a_write():
    """sqlite3.connect's audit event carries ONLY the database argument -- no
    mode field -- so anything not provably read-only is a write."""
    import _lib_test_isolation as iso
    assert iso.is_writable_sqlite_target("/x/y.db")
    assert iso.is_writable_sqlite_target("file:/x/y.db")
    assert iso.is_writable_sqlite_target("file:/x/y.db?mode=rwc")


# --- the guarded root set --------------------------------------------------


def test_every_guarded_root_is_present_and_password_home_derived():
    """Spec section 7.1 requires a case per guarded root. They are asserted by
    IDENTITY rather than by writing into them, because writing into them is the
    thing this detector exists to forbid.

    The production directory resolves through the password database rather than
    $HOME, so it is immune to the faked HOME every test uses.
    """
    import pwd
    import _cctally_core
    import _lib_test_isolation as iso

    home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    roots = {str(p) for p in iso.resolve_protected_roots({})}
    for expected in (
        _cctally_core._real_prod_data_dir(),
        home / ".local" / "share" / "cctally-dev",
        home / ".local" / "share" / "ccusage-subscription",
        home / ".claude",
        home / ".codex",
        pathlib.Path(_cctally_core.STATUSLINE_OAUTH_CACHE_PATH),
    ):
        assert os.path.realpath(str(expected)) in roots, f"{expected} is unguarded"


def test_the_production_root_ignores_a_faked_home(monkeypatch, tmp_path):
    """Every test runs under a faked HOME. A guard derived from the environment
    would watch the fixture directory instead of the directory at risk."""
    import _lib_test_isolation as iso

    monkeypatch.setenv("HOME", str(tmp_path))
    roots = {str(p) for p in iso.resolve_protected_roots({})}
    assert not any(r.startswith(str(tmp_path)) for r in roots)


def test_an_inherited_data_dir_override_becomes_a_protected_root(tmp_path):
    """CCTALLY_DATA_DIR outranks HOME in _init_paths_from_env, while
    _real_prod_data_dir() always returns the password-home path. Guarding only
    the latter leaves a maintainer with a custom data dir completely exposed.
    """
    import _lib_test_isolation as iso
    custom = tmp_path / "custom-data"
    roots = iso.resolve_protected_roots({"CCTALLY_DATA_DIR": str(custom)})
    assert custom.resolve() in roots


# --- the hook, against a synthetic root ------------------------------------


class _Guarded:
    def __init__(self, root, outside, node_id):
        self.root = root
        self.outside = outside
        self.node_id = node_id


@pytest.fixture
def guarded(tmp_path, request):
    """A synthetic protected root under tmp_path, plus its seeded contents.

    The seeding happens BEFORE the root is registered, so the fixture itself
    never performs a guarded write. An explicitly registered root wins over the
    scratch registration of tmp_path, which is what lets a synthetic production
    directory live inside a per-test directory at all.
    """
    import _lib_test_isolation as iso

    root = tmp_path / "synthetic-prod"
    (root / "sub").mkdir(parents=True)
    (root / "seed.txt").write_text("seed")
    (root / "doomed.txt").write_text("doomed")
    import sqlite3
    con = sqlite3.connect(str(root / "seed.db"))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.commit()
    con.close()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    node_id = request.node.nodeid
    iso.install(node_id_getter=lambda: node_id)
    iso.register_scratch_root(tmp_path)
    iso.add_protected_root(root)
    try:
        yield _Guarded(root, outside, node_id)
    finally:
        iso.remove_protected_root(root)
        # These tests provoke real violations on purpose; drop their own rows so
        # the teardown contract does not fail the tests that prove it works.
        iso.drop_node_from_ledger(node_id)


def _blocked(fn, *a, **kw):
    import _lib_test_isolation as iso
    with pytest.raises(iso.ProductionWriteBlocked):
        fn(*a, **kw)


def test_a_write_open_into_a_guarded_root_is_blocked(guarded):
    _blocked(open, guarded.root / "new.txt", "w")
    assert not (guarded.root / "new.txt").exists()


def test_a_nested_descendant_is_blocked(guarded):
    """Guarding the root must guard everything under it, at any depth, even
    when the intermediate directories do not exist yet."""
    _blocked(open, guarded.root / "a" / "b" / "c.txt", "w")


def test_a_symlink_whose_own_path_is_unguarded_is_still_blocked(guarded, tmp_path):
    """Canonicalization is the point: the alias is not a guarded path, and only
    realpath sees that the write lands inside the root."""
    alias = tmp_path / "alias"
    os.symlink(guarded.root, alias)
    assert not str(alias).startswith(str(guarded.root))
    _blocked(open, alias / "through-the-link.txt", "w")


def test_a_raw_os_open_is_blocked(guarded):
    """The shape an `os.open` arrives in: mode is None and only the flags
    integer is meaningful."""
    _blocked(
        os.open,
        str(guarded.root / "raw.db"),
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
    )


def test_os_remove_is_blocked(guarded):
    _blocked(os.remove, guarded.root / "doomed.txt")
    assert (guarded.root / "doomed.txt").exists()


def test_os_rename_is_blocked(guarded):
    _blocked(os.rename, guarded.root / "seed.txt", guarded.root / "moved.txt")


def test_os_replace_is_blocked(guarded):
    """os.replace raises the `os.rename` audit event, not one of its own."""
    _blocked(os.replace, guarded.outside, guarded.root / "replaced.txt")


def test_os_truncate_is_blocked(guarded):
    _blocked(os.truncate, guarded.root / "seed.txt", 0)


def test_os_mkdir_is_blocked(guarded):
    _blocked(os.mkdir, guarded.root / "made")


def test_os_rmdir_is_blocked(guarded):
    _blocked(os.rmdir, guarded.root / "sub")
    assert (guarded.root / "sub").is_dir()


def test_os_chmod_is_blocked(guarded):
    _blocked(os.chmod, guarded.root / "seed.txt", 0o600)


def test_os_link_is_blocked(guarded):
    _blocked(os.link, guarded.outside, guarded.root / "hard")


def test_os_symlink_is_blocked(guarded):
    _blocked(os.symlink, guarded.outside, guarded.root / "soft")


def test_a_writable_sqlite_connect_is_blocked(guarded):
    import sqlite3
    _blocked(sqlite3.connect, str(guarded.root / "new.db"))


# --- the negatives, which are what stop this being a blanket refusal -------


def test_reading_a_guarded_path_is_allowed(guarded):
    assert (guarded.root / "seed.txt").read_text() == "seed"


def test_a_read_only_sqlite_connect_is_allowed(guarded):
    import sqlite3
    con = sqlite3.connect(
        f"file:{guarded.root / 'seed.db'}?mode=ro", uri=True,
    )
    try:
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    finally:
        con.close()


def test_a_write_outside_every_guarded_root_is_allowed(guarded, tmp_path):
    (tmp_path / "free.txt").write_text("fine")
    assert (tmp_path / "free.txt").read_text() == "fine"


# --- fail-closed means the ledger, not the exception -----------------------


def test_a_swallowed_exception_still_leaves_a_ledger_violation(guarded):
    """A test that catches the raised error and returns normally must still
    fail. The hook writes the violation BEFORE it raises, and teardown fails
    from the ledger.

    This drives `collect_test_violations`, which IS the function the conftest
    teardown fails from -- not a copy of it.
    """
    import _lib_test_isolation as iso

    try:
        with open(guarded.root / "swallowed.txt", "w") as fh:
            fh.write("x")
    except iso.ProductionWriteBlocked:
        pass  # exactly the swallow the contract has to survive

    problems = iso.collect_test_violations(guarded.node_id)
    assert problems, "the swallowed violation left no evidence"
    assert any("swallowed.txt" in p for p in problems)


def test_a_violation_carries_node_worker_pid_and_path(guarded):
    import _lib_test_isolation as iso

    try:
        open(guarded.root / "attributed.txt", "w")
    except iso.ProductionWriteBlocked:
        pass

    rows = [v for v in iso.read_ledger() if v.node_id == guarded.node_id]
    assert rows, "nothing was recorded"
    row = rows[-1]
    assert row.node_id == guarded.node_id
    assert row.worker_id == iso.worker_id()
    assert row.pid == os.getpid()
    assert row.path.endswith("attributed.txt")
    assert row.event == "open"


# --- subprocess coverage (spec section 5.3) --------------------------------


def _fresh_env(tmp_path, **extra):
    """The exact environment shape the estate already builds.

    tests/test_five_hour_blocks_json.py:54 says so in its own comment: "this
    fresh env dict omits os.environ, so it does NOT inherit conftest's
    process-level CCTALLY_DISABLE_DEV_AUTODETECT". Those launches are
    load-bearing -- they exist so the child resolves a specific data-directory
    layout -- so any design that requires them to stop is changing behaviour
    those tests depend on.
    """
    env = {
        "HOME": str(tmp_path / "fake-home"),
        "TZ": "Etc/UTC",
        "PATH": "/usr/bin:/bin",
    }
    env.update(extra)
    return env


def test_a_fresh_env_child_is_still_covered(guarded, tmp_path):
    """E4's proof. Such a child inherits neither the ledger variables nor any
    PYTHONPATH, so environment inheritance alone leaves the detector hollow."""
    import _lib_test_isolation as iso

    victim = guarded.root / "should-not-appear"
    env = _fresh_env(tmp_path, CCTALLY_DATA_DIR=str(guarded.root))
    code = f"open({str(victim)!r}, 'w').write('x')"
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)

    assert not victim.exists(), "the child wrote into a protected root"
    assert proc.returncode != 0, proc.stdout
    rows = [v for v in iso.read_ledger()
            if v.path == os.path.realpath(str(victim))]
    assert rows, "the child's violation never reached the ledger"
    assert rows[-1].pid != os.getpid(), "the violation was not attributed to the child"
    assert rows[-1].node_id == guarded.node_id


def test_the_child_keeps_the_layout_its_caller_asked_for(guarded, tmp_path):
    """The interceptor COPIES the caller's environment and overwrites only the
    reserved keys, so a fresh-env launch keeps HOME, CCTALLY_DATA_DIR and PATH
    byte for byte. Without this the fresh-env tests would silently change the
    data directory they were written for."""
    env = _fresh_env(tmp_path, CCTALLY_DATA_DIR=str(tmp_path / "declared"))
    code = (
        "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
        "('HOME', 'PATH', 'TZ', 'CCTALLY_DATA_DIR')}))"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "HOME": env["HOME"], "PATH": env["PATH"], "TZ": env["TZ"],
        "CCTALLY_DATA_DIR": env["CCTALLY_DATA_DIR"],
    }


def test_an_inheriting_child_is_covered(guarded):
    """The other environment shape: env=None, so the child inherits os.environ."""
    import _lib_test_isolation as iso

    victim = guarded.root / "inherited-write"
    code = f"open({str(victim)!r}, 'w').write('x')"
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120)
    assert not victim.exists()
    assert proc.returncode != 0
    assert any(v.path == os.path.realpath(str(victim)) for v in iso.read_ledger())


def test_a_grandchild_is_covered_too(guarded, tmp_path):
    """The child bootstrap installs the same interceptor for its own
    descendants, so depth does not buy an escape."""
    import _lib_test_isolation as iso

    victim = guarded.root / "grandchild-write"
    inner = f"open({str(victim)!r}, 'w').write('x')"
    outer = (
        "import subprocess, sys; "
        f"raise SystemExit(subprocess.run([sys.executable, '-c', {inner!r}]).returncode)"
    )
    proc = subprocess.run([sys.executable, "-c", outer], env=_fresh_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert not victim.exists()
    assert proc.returncode != 0
    assert any(v.path == os.path.realpath(str(victim)) for v in iso.read_ledger())


def test_a_grandchild_may_still_create_its_own_scratch_app_dir(guarded, tmp_path):
    """Scratch roots have to TRAVEL with the child, or protection over-fires.

    A grandchild that re-classifies on its own knows no scratch roots, so it
    treats its perfectly legitimate fake-HOME APP_DIR as protected. Found by
    running the estate: every `cctally five-hour-blocks --json` child in
    tests/test_five_hour_blocks_json.py spawns a detached post-command worker,
    and each one arrived as a late violation for creating its own tmp data
    directory. The tests still passed, so only the session-finish report showed
    it -- which is exactly how a guard that cries wolf gets ignored.
    """
    home = tmp_path / "grandchild-home"
    app_dir = home / ".local" / "share" / "cctally"
    inner = (
        f"import os; os.makedirs({str(app_dir)!r}); "
        f"open({str(app_dir / 'marker')!r}, 'w').write('ok')"
    )
    outer = (
        "import subprocess, sys; raise SystemExit("
        f"subprocess.run([sys.executable, '-c', {inner!r}]).returncode)"
    )
    proc = subprocess.run([sys.executable, "-c", outer],
                          env=_fresh_env(tmp_path, HOME=str(home)),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert (app_dir / "marker").exists()


def test_a_child_data_dir_outside_every_scratch_root_is_protected_for_that_child(
    guarded, tmp_path,
):
    """Per-child protected roots. The interceptor classifies the CHILD's
    environment-resolved APP_DIR -- not the parent's -- and protects it when it
    is not beneath a registered per-test scratch root. This is what closes the
    CCTALLY_DATA_DIR hole at the child boundary as well as the parent's.
    """
    import shutil
    import tempfile
    import _lib_test_isolation as iso

    outside = pathlib.Path(tempfile.mkdtemp(prefix="cctally-not-scratch-"))
    try:
        assert not iso._under_scratch(os.path.realpath(str(outside)))
        assert os.path.realpath(str(outside)) not in iso.protected_roots(), (
            "the parent must not be guarding it; only the child should"
        )
        victim = outside / "child-only.txt"
        code = f"open({str(victim)!r}, 'w').write('x')"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=_fresh_env(tmp_path, CCTALLY_DATA_DIR=str(outside)),
            capture_output=True, text=True, timeout=120,
        )
        assert not victim.exists()
        assert proc.returncode != 0
    finally:
        shutil.rmtree(outside, ignore_errors=True)


class _FakeSignalledProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return -15


class _SkewedClock:
    """Stands in for the `time` module the detector reads, with monotonic moved.

    A shim rather than a patch on the real module, so nothing else running in
    this process observes the skew.
    """

    def __init__(self, offset):
        self._offset = offset

    def monotonic(self):
        return time.monotonic() + self._offset

    def time(self):
        return time.time()

    def sleep(self, seconds):
        return time.sleep(seconds)


def test_a_bypass_expectation_fails_its_own_test_rather_than_being_carried(guarded):
    """A launch whose interpreter options defeat the bootstrap is not carried.

    Every other missing handshake is carried to session finish, because waiting
    longer can still resolve it. This one cannot be resolved by waiting, so it
    fails the test that made it -- and a signal exit does not exempt it either,
    which is the half `_was_signalled_before_handshaking` short-circuits on.

    This drives `collect_test_violations`, which IS the function the conftest
    teardown fails from. The expectation is injected rather than produced by a
    real launch, because the backstop now rejects such a launch outright.
    """
    import _lib_test_isolation as iso

    pid = 10_000_000
    iso._EXPECTED_HANDSHAKES[pid] = iso._Expected(
        node_id=guarded.node_id,
        proc=_FakeSignalledProc(pid),
        bypass="started with -S",
    )
    try:
        problems = iso.collect_test_violations(guarded.node_id, handshake_grace=0.0)
    finally:
        iso._EXPECTED_HANDSHAKES.pop(pid, None)
    assert any("missing handshake" in p and f"pid={pid}" in p for p in problems), (
        problems
    )
    assert pid not in iso.pending_handshakes()


def test_a_child_killed_during_startup_is_not_reported_as_missing(guarded, tmp_path):
    """A child the parent signals before `site` finishes never reaches its own
    first line, so there is no user code the detector could have failed to
    guard. Found by running the estate: tests/test_setup_legacy_migrate.py
    launches a fake poller and SIGTERMs it within about 250 milliseconds, which
    is inside the interpreter's own startup window on a loaded runner, and the
    resulting false 'missing handshake' would fail a blameless test.

    The carve-out is narrow: it applies only when the handshake is ABSENT, the
    exit status shows a signal, AND the child had been running for less than the
    startup window. The elapsed bound is what stops it exempting a child that
    ran for minutes and wrote before it was killed.

    It is resolved through `flush_late_handshake_problems`, which is where a
    missing handshake is reported now -- reporting it at the test's own teardown
    is what made a loaded runner able to fail a blameless test.
    """
    import signal
    import _lib_test_isolation as iso

    script = tmp_path / "slow-start.py"
    script.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(script)],
                            env=_fresh_env(tmp_path))
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=60)
    assert proc.returncode < 0, "the child was not killed by a signal"

    # Simulate the startup race deterministically rather than by repetition:
    # drop the handshake the child may or may not have won the race to write.
    handshakes = pathlib.Path(iso.handshake_path())
    if handshakes.exists():
        kept = [
            line for line in handshakes.read_text().splitlines()
            if f'"pid": {proc.pid}' not in line
        ]
        handshakes.write_text("".join(line + "\n" for line in kept))
    assert proc.pid not in iso.read_handshakes()

    problems = iso.collect_test_violations(guarded.node_id, handshake_grace=0.2)
    assert not any("missing handshake" in p for p in problems), problems
    # Settled at teardown by the carve-out, so it is not even carried forward.
    assert proc.pid not in iso.pending_handshakes()
    assert not iso.flush_late_handshake_problems()


def test_a_child_that_skips_site_is_rejected_before_it_can_write(guarded, tmp_path):
    """`python -S` used to run, write, and be reported only afterwards.

    Measured before the backstop learned the flag: `wrote=True returncode=-15
    problems=[]` for the signalled form, and `wrote=true status=0 ledger_rows=[]`
    for an `os.posix_spawn`. `-S` skips site processing, so the bootstrap on
    PYTHONPATH is never imported however complete the envelope is -- which makes
    a report after the fact the wrong instrument. The launch is refused instead.
    """
    import _lib_test_isolation as iso

    victim = guarded.root / "dash-s-write.txt"
    code = f"open({str(victim)!r}, 'w').write('x')"
    with pytest.raises(iso.IsolationBypassBlocked):
        subprocess.Popen([sys.executable, "-S", "-c", code],
                         env=_fresh_env(tmp_path))
    assert not victim.exists(), "the -S child ran before the backstop refused it"
    iso.drop_node_from_ledger(guarded.node_id)


def test_the_startup_carve_out_does_not_depend_on_elapsed_time(guarded, tmp_path):
    """The carve-out was bounded by a five-second window, and the bound was wrong.

    `flush_late_handshake_problems` re-evaluates the same predicate at session
    finish, where the elapsed time is launch-to-session-finish and is therefore
    always past the window, so every genuine startup kill whose `poll()` still
    returned None at teardown was reported unconditionally. That is a
    load-correlated false failure, which is the class this session removes.

    Driven by moving the clock rather than by sleeping: an hour of skew is past
    any window the bound could have carried, and the child is still exempt.
    """
    import signal
    import _lib_test_isolation as iso

    script = tmp_path / "slow-start.py"
    script.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(script)], env=_fresh_env(tmp_path))
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=60)
    assert proc.returncode < 0, "the child was not killed by a signal"

    handshakes = pathlib.Path(iso.handshake_path())
    if handshakes.exists():
        kept = [line for line in handshakes.read_text().splitlines()
                if f'"pid": {proc.pid}' not in line]
        handshakes.write_text("".join(line + "\n" for line in kept))
    assert proc.pid not in iso.read_handshakes()

    with pytest.MonkeyPatch.context() as skewed:
        skewed.setattr(iso, "time", _SkewedClock(3600.0))
        assert not iso.collect_test_violations(guarded.node_id, handshake_grace=0.2)
        assert proc.pid not in iso.pending_handshakes()
        assert not iso.flush_late_handshake_problems()
    iso.drop_node_from_ledger(guarded.node_id)


def test_a_slow_child_is_carried_to_session_finish_not_blamed_on_its_test(
    guarded, tmp_path,
):
    """The docstring said carried; the code appended `missing handshake` to the
    test's own problems after a two-second grace and then dropped the pid, so a
    loaded runner's slow interpreter failed a blameless test. Carried is the
    behaviour that does not depend on the machine.
    """
    import _lib_test_isolation as iso

    script = tmp_path / "slow-to-start.py"
    script.write_text("import time\ntime.sleep(20)\n")
    proc = subprocess.Popen([sys.executable, str(script)], env=_fresh_env(tmp_path))
    try:
        # Grace of zero: the handshake cannot possibly have arrived yet.
        problems = iso.collect_test_violations(guarded.node_id, handshake_grace=0.0)
        assert not any("missing handshake" in p for p in problems), problems
        assert proc.pid in iso.pending_handshakes()
        # And it resolves cleanly once the child really has started.
        deadline = time.monotonic() + 60
        while proc.pid not in iso.read_handshakes() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not iso.flush_late_handshake_problems()
    finally:
        proc.terminate()
        proc.wait(timeout=60)
    iso.drop_node_from_ledger(guarded.node_id)


def test_a_bootstrap_failure_is_recorded_rather_than_swallowed(tmp_path, monkeypatch):
    """A bootstrap that fails silently is indistinguishable from one that never
    ran, so the blanket except records the failure before it drops it."""
    import importlib.util

    # By path and under its own name: `sitecustomize` is already bound to the
    # interpreter's own copy in sys.modules.
    spec = importlib.util.spec_from_file_location(
        "_cctally_isolation_bootstrap_probe",
        REPO_ROOT / "tests" / "isolation_bootstrap" / "sitecustomize.py",
    )
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    ledger = tmp_path / "probe-ledger.jsonl"
    monkeypatch.setenv("CCTALLY_ISOLATION_LEDGER", str(ledger))
    monkeypatch.setenv("CCTALLY_ISOLATION_NODE", "some::node")

    def _boom():
        raise RuntimeError("the kernel could not be imported")

    monkeypatch.setattr(bootstrap, "_install", _boom)
    bootstrap._run()

    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line]
    assert rows, "the failure left no evidence at all"
    assert rows[-1]["kind"] == "bootstrap"
    assert rows[-1]["node_id"] == "some::node"
    assert "could not be imported" in rows[-1]["path"]


# --- launch primitives other than subprocess.Popen -------------------------
#
# Measured from inside a live pytest worker before the fix, each writing into a
# protected root: os.posix_spawn `exists=True rows=[]`, os.system `rc=0
# exists=True rows=[]`, multiprocessing spawn `exitcode=0 exists=True`. The
# multiprocessing case is not theoretical -- six test modules use it, and
# tests/test_journal_append.py records in its own docstring that spawn is the
# macOS default.


def _write_from_a_child(path):
    with open(path, "w") as fh:
        fh.write("x")


def test_a_multiprocessing_spawn_child_is_covered(guarded):
    """`multiprocessing`'s POSIX spawn reaches `_posixsubprocess.fork_exec`
    directly, so it raises no audit event any backstop could see. What it does
    do is inherit this process's environment, which is why the worker publishes
    the bootstrap envelope into its own environment rather than only into the
    environments it constructs.
    """
    import multiprocessing
    import _lib_test_isolation as iso

    victim = guarded.root / "mp-spawn.txt"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_write_from_a_child, args=(str(victim),))
    proc.start()
    proc.join(120)

    assert not victim.exists(), "the multiprocessing child wrote into a protected root"
    assert proc.exitcode not in (0, None), f"exitcode={proc.exitcode}"
    assert any(v.path == os.path.realpath(str(victim)) for v in iso.read_ledger())


def test_a_posix_spawn_child_is_covered(guarded, tmp_path):
    import _lib_test_isolation as iso

    victim = guarded.root / "posix-spawn.txt"
    code = f"open({str(victim)!r}, 'w').write('x')"
    pid = os.posix_spawn(sys.executable, [sys.executable, "-c", code],
                         dict(os.environ))
    _, status = os.waitpid(pid, 0)

    assert not victim.exists(), "the posix_spawn child wrote into a protected root"
    assert status != 0
    assert any(v.path == os.path.realpath(str(victim)) for v in iso.read_ledger())


def test_a_posix_spawn_with_a_stripped_environment_is_rejected(guarded, tmp_path):
    """No audit hook can rewrite the arguments of the launch it observes, so an
    explicit environment that would leave the child unguarded is refused."""
    import _lib_test_isolation as iso

    marker = tmp_path / "posix-spawn-ran"
    code = f"open({str(marker)!r}, 'w').write('ran')"
    with pytest.raises(iso.IsolationBypassBlocked):
        os.posix_spawn(sys.executable, [sys.executable, "-c", code],
                       _fresh_env(tmp_path))
    assert not marker.exists()


def test_a_bootstrap_defeating_child_is_rejected_by_every_audited_primitive(
    guarded, tmp_path,
):
    """The flag check belongs in the backstop, not only in the interceptor.

    `_EXPECTED_HANDSHAKES` was populated inside `_intercepted_init` alone, so
    `python_flags_defeat_bootstrap` was consulted for `subprocess.Popen` and for
    nothing else. Measured from inside a live pytest worker before this closed:
    `os.posix_spawn(python, [python, "-S", "-c", <write>], os.environ)` gave
    `wrote=true status=0 ledger_rows=[] test_problems=[] late_problems=[]`, and
    `os.system('python -S -c "…"')` gave `wrote=true rc=0 rows=[]`. Both wrote
    into a protected root and the test passed.
    """
    import _lib_test_isolation as iso

    victim = guarded.root / "flag-defeated.txt"
    code = f"open({str(victim)!r}, 'w').write('x')"

    with pytest.raises(iso.IsolationBypassBlocked):
        os.posix_spawn(sys.executable, [sys.executable, "-S", "-c", code],
                       dict(os.environ))
    assert not victim.exists(), "the posix_spawn -S child wrote into a protected root"

    with pytest.raises(iso.IsolationBypassBlocked):
        os.system(f'{sys.executable} -S -c "{code}"')
    assert not victim.exists(), "the os.system -S child wrote into a protected root"

    with pytest.raises(iso.IsolationBypassBlocked):
        subprocess.Popen([sys.executable, "-I", "-c", code])
    assert not victim.exists(), "the Popen -I child wrote into a protected root"
    iso.drop_node_from_ledger(guarded.node_id)


def test_an_ordinary_python_child_is_not_rejected_by_the_flag_check(guarded, tmp_path):
    """The boundary is pinned in both directions.

    A rejection rule that also refused ordinary launches would satisfy the test
    above while breaking every subprocess test in the estate, so the negative
    arm is what makes the positive one mean anything.
    """
    marker = tmp_path / "ordinary-child-ran"
    code = f"open({str(marker)!r}, 'w').write('ran')"
    pid = os.posix_spawn(sys.executable, [sys.executable, "-u", "-c", code],
                         dict(os.environ))
    _, status = os.waitpid(pid, 0)
    assert status == 0
    assert marker.read_text() == "ran"

    second = tmp_path / "ordinary-shell-child-ran"
    rc = os.system(f'{sys.executable} -c "open({str(second)!r},\'w\').write(\'ran\')"')
    assert rc == 0
    assert second.read_text() == "ran"


def test_an_os_system_child_is_covered(guarded):
    """`os.system` hands the command to a shell that inherits this process's
    environment and nothing else, so the exported envelope is what covers it."""
    import _lib_test_isolation as iso

    victim = guarded.root / "os-system.txt"
    rc = os.system(f'{sys.executable} -c "open({str(victim)!r},\'w\').write(\'x\')"')

    assert not victim.exists(), "the os.system child wrote into a protected root"
    assert rc != 0
    assert any(v.path == os.path.realpath(str(victim)) for v in iso.read_ledger())


def test_the_worker_publishes_the_envelope_into_its_own_environment(guarded):
    """The property every inheriting launch primitive rests on."""
    import _lib_test_isolation as iso

    assert iso.envelope_present(os.environ)
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == iso.bootstrap_dir()
    assert os.path.realpath(str(guarded.root)) in os.environ[iso.ENV_ROOTS].split(
        os.pathsep
    )


@pytest.mark.parametrize(
    "argv,expected",
    [
        ([sys.executable, "-c", "pass"], False),
        ([sys.executable, "-S", "-c", "pass"], True),
        ([sys.executable, "-E", "-c", "pass"], True),
        ([sys.executable, "-I", "-c", "pass"], True),
        ([sys.executable, "-Es", "-c", "pass"], True),
        ([sys.executable, "-u", "-c", "pass"], False),
        # After -c every token belongs to the program, not the interpreter.
        ([sys.executable, "-c", "pass", "-S"], False),
        ([sys.executable, "-W", "ignore", "-S", "-c", "pass"], True),
        ([sys.executable, "script.py", "-S"], False),
    ],
)
def test_the_interpreter_flags_that_defeat_the_bootstrap_are_recognised(argv, expected):
    import _lib_test_isolation as iso

    assert iso.python_flags_defeat_bootstrap(argv) is expected


def test_the_unwrapped_launch_path_is_rejected_before_the_child_runs(
    guarded, tmp_path,
):
    """The bypass backstop, behind the interceptor: an independent
    `subprocess.Popen` audit event rejects any Python launch whose final
    environment lacks a valid bootstrap envelope. The event fires inside
    `_execute_child` before the fork, so the rejection precedes execution.
    """
    import subprocess as _sp
    import _lib_test_isolation as iso

    marker = tmp_path / "the-child-ran"

    class _Unwrapped(_sp.Popen):
        __init__ = iso.unwrapped_popen_init()

    code = f"open({str(marker)!r}, 'w').write('ran')"
    with pytest.raises(iso.IsolationBypassBlocked):
        _Unwrapped([sys.executable, "-c", code], env=_fresh_env(tmp_path))
    assert not marker.exists(), "the child executed before the backstop rejected it"


def test_the_backstop_leaves_a_non_python_launch_alone(guarded, tmp_path):
    """Native children carry no hook and are outside this guarantee by design,
    so the backstop must not refuse them -- refusing would make the boundary a
    lie in the other direction."""
    import subprocess as _sp
    import _lib_test_isolation as iso

    class _Unwrapped(_sp.Popen):
        __init__ = iso.unwrapped_popen_init()

    proc = _Unwrapped(["/bin/echo", "fine"], env=_fresh_env(tmp_path),
                      stdout=_sp.PIPE)
    out, _ = proc.communicate(timeout=60)
    assert out.strip() == b"fine"


def test_the_conftest_teardown_really_fails_a_swallowed_violation(tmp_path):
    """End-to-end proof that the wiring is live, not merely present.

    A nested pytest run loads THIS repo's tests/conftest.py as a plugin, so the
    fixture under test is the shipped one rather than a copy. Its single test
    swallows a blocked write and returns normally; the run must still fail.
    """
    synthetic = tmp_path / "nested-prod"
    synthetic.mkdir()
    module = tmp_path / "test_nested_swallow.py"
    module.write_text(
        "import _lib_test_isolation as iso\n"
        "\n"
        "def test_it_swallows():\n"
        f"    target = {str(synthetic / 'written.txt')!r}\n"
        "    try:\n"
        "        open(target, 'w').write('x')\n"
        "    except iso.ProductionWriteBlocked:\n"
        "        pass\n"
    )
    env = _fresh_env(
        tmp_path,
        CCTALLY_DATA_DIR=str(synthetic),
        PYTHONPATH=os.pathsep.join(
            [str(REPO_ROOT / "tests"), str(REPO_ROOT / "bin")]
        ),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-q", "-p",
         "no:cacheprovider", str(module)],
        env=env, capture_output=True, text=True, timeout=110,
    )
    assert proc.returncode != 0, (
        "the nested run passed; the teardown does not fail from the ledger\n"
        + proc.stdout
    )
    assert "preserve-item 3" in proc.stdout or "write violation" in proc.stdout, (
        proc.stdout + proc.stderr
    )


def test_the_ledger_is_worker_private(guarded):
    """`_guard_real_prod_migration_log`'s own docstring warns that under xdist a
    sibling worker's legitimate write can be misattributed to the wrong test. A
    per-worker ledger removes that failure rather than documenting it."""
    import _lib_test_isolation as iso

    path = iso.ledger_path()
    assert iso.worker_id() in pathlib.Path(path).parent.name
    assert str(os.getpid()) in pathlib.Path(path).parent.name
    # ... and it sits under the per-session directory, which is what lets the
    # controller read it. Without that the controller's own path is
    # `main-<controller pid>`, always empty, because it launches no children.
    assert pathlib.Path(path).parent.parent == iso.session_dir()


def _nested_pytest(module_path, tmp_path, synthetic, extra_args=()):
    env = _fresh_env(
        tmp_path,
        CCTALLY_DATA_DIR=str(synthetic),
        PYTHONPATH=os.pathsep.join(
            [str(REPO_ROOT / "tests"), str(REPO_ROOT / "bin")]
        ),
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-q",
         "-p", "no:cacheprovider", *extra_args, str(module_path)],
        env=env, capture_output=True, text=True, timeout=110,
    )


_LATE_VIOLATION_MODULE = """\
import pytest
import _lib_test_isolation as iso


@pytest.fixture(scope="session", autouse=True)
def _writes_after_every_report_is_final():
    yield
    try:
        open({victim!r}, "w").write("x")
    except iso.ProductionWriteBlocked:
        pass


def test_one():
    assert True


def test_two():
    assert True
"""


@pytest.mark.parametrize("jobs", ["0", "2"])
def test_a_late_violation_fails_the_run_including_under_xdist(tmp_path, jobs):
    """The gate has to be able to fail in the configuration the gate runs in.

    `bin/cctally-test-all` phase 3 invokes pytest with `-n "$PYTEST"`, and a
    worker's `session.exitstatus` is not the controller's: measured, `-n 2`
    exited 0 while the session-finish hook fired in gw0 and gw1. A gate that
    cannot fail is worse than no gate, because it trains the reader to believe
    it passed.
    """
    if jobs != "0":
        pytest.importorskip("xdist")
    synthetic = tmp_path / "nested-prod"
    synthetic.mkdir()
    module = tmp_path / "test_nested_late_violation.py"
    module.write_text(
        _LATE_VIOLATION_MODULE.format(victim=str(synthetic / "late.txt"))
    )
    extra = () if jobs == "0" else ("-n", jobs)
    proc = _nested_pytest(module, tmp_path, synthetic, extra)
    assert proc.returncode != 0, (
        f"the nested run with jobs={jobs} passed; the late violation could not "
        "fail it\n" + proc.stdout + proc.stderr
    )
    assert "isolation contract violated" in proc.stdout, proc.stdout + proc.stderr


def test_a_nested_run_is_not_aggregated_into_its_launching_session(tmp_path):
    """The nested runs above provoke violations deliberately. They must land in
    their own session directory, or the outer controller would fail the outer
    run on evidence a test created on purpose.

    Both discriminators are exercised: this child inherits the session
    identifier and, under `-n`, `PYTEST_XDIST_WORKER` as well, and it must still
    mint its own. It is a direct child of a non-xdist controller in the first
    case and a non-direct child of the controller in the second.
    """
    import _lib_test_isolation as iso

    probe = (
        "import sys, os; sys.path.insert(0, %r); "
        "import _lib_test_isolation as iso; "
        "print(iso.session_dir())" % str(REPO_ROOT / "bin")
    )
    proc = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True, timeout=110)
    assert proc.returncode == 0, proc.stderr
    assert os.environ[iso.ENV_SESSION] == iso.session_id(), (
        "the child did not even inherit the session, so this proves nothing"
    )
    assert proc.stdout.strip() != str(iso.session_dir())


def test_an_expectation_outside_a_test_node_does_not_leak(guarded, tmp_path):
    """`<no active test>` is a node id no `forget_expected_handshakes(node_id)`
    call ever names, so those entries -- each holding a live Popen -- stayed for
    the worker's lifetime. Every resolved expectation is pruned instead."""
    import _lib_test_isolation as iso

    iso.set_node_id_getter(lambda: "<no active test>")
    try:
        proc = subprocess.run([sys.executable, "-c", "pass"],
                              env=_fresh_env(tmp_path),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert any(node == "<no active test>"
                   for node in iso.expected_handshakes().values())
    finally:
        iso.set_node_id_getter(lambda: guarded.node_id)

    iso.collect_test_violations(guarded.node_id, handshake_grace=2.0)
    assert not any(node == "<no active test>"
                   for node in iso.expected_handshakes().values()), (
        iso.expected_handshakes()
    )


def test_a_concurrent_append_survives_the_ledger_rewrite(guarded, tmp_path, monkeypatch):
    """`drop_node_from_ledger` reads the ledger, filters, and rewrites it. A
    grandchild appending between the read and the rewrite had its violation
    erased by the rewrite.

    Driven by a barrier rather than by repetition: the append is launched from
    inside the read, at the exact instant the window would be open, and it comes
    from another PROCESS because the two halves have to contend for a real lock.
    """
    import _lib_test_isolation as iso

    ledger = iso.ledger_path()
    iso._append_json_line(ledger, {
        "node_id": "some::other::node", "worker_id": "main", "pid": 1,
        "event": "open", "path": "/keep/me", "kind": "write", "at": 0,
    })

    started = tmp_path / "racer-reached-the-append"
    racer = tmp_path / "racer.py"
    racer.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
        "import _lib_test_isolation as iso\n"
        "open(sys.argv[2], 'w').write('here')\n"
        "iso._append_json_line(sys.argv[1], {\n"
        "    'node_id': 'a::grandchild', 'worker_id': 'main', 'pid': os.getpid(),\n"
        "    'event': 'open', 'path': '/raced/in', 'kind': 'write', 'at': 0})\n"
    )

    original = iso._read_json_lines_unlocked
    launched = []

    def _raced_in_file():
        return any(r.get("path") == "/raced/in" for r in original(ledger))

    def _read_then_race(path):
        rows = original(path)
        if path == ledger and not launched:
            child = subprocess.Popen(
                [sys.executable, str(racer), ledger, str(started)],
                env=_fresh_env(tmp_path),
            )
            launched.append(child)
            # Two observable waits, no clock guess. First until the racer is
            # inside `_append_json_line`, then until its row lands -- which is
            # what an UNLOCKED append does inside this window, and what a locked
            # one cannot do while the rewrite holds the lock.
            deadline = time.monotonic() + 60
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert started.exists(), "the racer never reached the append"
            deadline = time.monotonic() + 2
            while not _raced_in_file() and time.monotonic() < deadline:
                time.sleep(0.02)
        return rows

    monkeypatch.setattr(iso, "_read_json_lines_unlocked", _read_then_race)
    try:
        iso.drop_node_from_ledger("some::other::node")
    finally:
        monkeypatch.undo()
        for child in launched:
            child.wait(timeout=60)

    paths = {v.path for v in iso.read_ledger(ledger)}
    assert "/raced/in" in paths, (
        "the concurrent append was erased by the rewrite that raced it"
    )
    iso.drop_node_from_ledger("a::grandchild")
    iso.drop_node_from_ledger(guarded.node_id)


def test_a_bootstrap_failure_row_survives_the_ledger_rewrite(
    guarded, tmp_path, monkeypatch,
):
    """The bootstrap appends to the same ledger, so it needs the same lock.

    `_report_failure` did a raw `os.open(..., O_APPEND)` with no lock while
    `drop_node_from_ledger` held one across its read-and-rewrite, so a bootstrap
    failure landing in that window was erased. That is the one appender the
    detector has no second chance to hear from: it is the loud replacement for a
    child that would otherwise be indistinguishable from one where the bootstrap
    never ran. The existing race test drives `_append_json_line`, which is
    locked, so it cannot reach this path.
    """
    import _lib_test_isolation as iso

    ledger = iso.ledger_path()
    iso._append_json_line(ledger, {
        "node_id": "some::other::node", "worker_id": "main", "pid": 1,
        "event": "open", "path": "/keep/me", "kind": "write", "at": 0,
    })

    started = tmp_path / "bootstrap-racer-reached-the-append"
    racer = tmp_path / "bootstrap_racer.py"
    racer.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('_probe_bootstrap', "
        f"{str(REPO_ROOT / 'tests' / 'isolation_bootstrap' / 'sitecustomize.py')!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "open(sys.argv[1], 'w').write('here')\n"
        "mod._report_failure(RuntimeError('raced-bootstrap-failure'))\n"
    )

    original = iso._read_json_lines_unlocked
    launched = []

    def _bootstrap_row_in_file():
        return any("raced-bootstrap-failure" in str(r.get("path", ""))
                   for r in original(ledger))

    def _read_then_race(path):
        rows = original(path)
        if path == ledger and not launched:
            child = subprocess.Popen(
                [sys.executable, str(racer), str(started)],
                env=_fresh_env(tmp_path),
            )
            launched.append(child)
            deadline = time.monotonic() + 60
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert started.exists(), "the racer never reached the append"
            deadline = time.monotonic() + 2
            while not _bootstrap_row_in_file() and time.monotonic() < deadline:
                time.sleep(0.02)
        return rows

    monkeypatch.setattr(iso, "_read_json_lines_unlocked", _read_then_race)
    try:
        iso.drop_node_from_ledger("some::other::node")
    finally:
        monkeypatch.undo()
        for child in launched:
            child.wait(timeout=60)

    assert any(
        v.kind == "bootstrap" and "raced-bootstrap-failure" in v.path
        for v in iso.read_ledger(ledger)
    ), "the bootstrap failure row was erased by the rewrite that raced it"
    iso.drop_node_from_ledger(guarded.node_id)


def test_install_without_roots_does_not_replace_the_policy(guarded):
    """`install(roots=None)` on an already-installed process re-derived the whole
    root set and replaced it, discarding a child's inherited roots and any
    synthetic root a test had registered."""
    import _lib_test_isolation as iso

    before = iso.protected_roots()
    assert os.path.realpath(str(guarded.root)) in before
    iso.install()
    assert iso.protected_roots() == before


# --- what SQLite guarding really is, and the two holes it leaves -----------


def test_a_sqlite_write_on_an_open_connection_raises_no_audit_event(tmp_path):
    """Measured, not assumed: `EVENTS DURING SQLITE WRITE: 0`.

    The module docstring used to say SQLite's own opens arrive as an `open`
    event with `mode=None`. They arrive as nothing at all -- libsqlite3 opens
    its files through the C library, which raises no Python audit event. SQLite
    is guarded at `connect`, and this test is what keeps that statement honest.
    """
    import sqlite3

    db = tmp_path / "events.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.commit()

    events = []
    sys.addaudithook(lambda event, args: events.append(event))
    events.clear()
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    during_write = list(events)

    events.clear()
    sqlite3.connect(str(tmp_path / "second.db")).close()
    during_connect = list(events)
    con.close()

    assert during_write == [], during_write
    assert "sqlite3.connect" in during_connect


def test_the_flags_predicate_is_required_by_os_open_not_by_sqlite(guarded):
    """`os.open` raises `open` with `mode=None` and only the flags integer
    meaningful, and the estate uses it widely -- the detector's own
    `_append_json_line` is one caller. That is what a mode-string predicate
    would miss; SQLite is out of its reach either way."""
    _blocked(
        os.open,
        str(guarded.root / "raw-flags.db"),
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
    )


def test_the_two_recorded_sqlite_holes_are_still_the_shape_recorded(guarded, tmp_path):
    """The boundary is stated rather than hidden, so it is asserted rather than
    assumed. Both are consequences of guarding at `connect`: neither an ATTACH
    on an already-allowed connection nor a descriptor obtained before the guard
    installed reaches a guarded event.
    """
    import sqlite3
    import _lib_test_isolation as iso

    later = tmp_path / "protected-later"
    later.mkdir()
    # INSIDE `later`, and opened before `later` is registered. An earlier
    # version of this test opened the handle at `tmp_path / "before-the-guard"`,
    # which no guard would ever have refused, so the arm was satisfied by any
    # implementation whatsoever and carried no assertion at all.
    predates = later / "predates-the-guard.txt"
    handle = open(predates, "w")
    outside = sqlite3.connect(str(tmp_path / "allowed.db"))
    iso.add_protected_root(later)
    try:
        handle.write("the descriptor predates the guard")
        handle.close()
        assert predates.read_text() == "the descriptor predates the guard", (
            "a write through a pre-guard descriptor is now blocked; the "
            "recorded boundary is out of date"
        )
        assert not any(
            v.path == os.path.realpath(str(predates)) for v in iso.read_ledger()
        ), "the pre-guard descriptor write reached the ledger"
        # A fresh open of that same path IS refused, which is what makes the
        # successful write above evidence about the descriptor rather than
        # about a path no guard was watching.
        with pytest.raises(iso.ProductionWriteBlocked):
            open(predates, "a").close()

        attached = later / "attached.db"
        outside.execute(f"ATTACH DATABASE '{attached}' AS g")
        outside.execute("CREATE TABLE g.t (a INTEGER)")
        outside.commit()
        assert attached.exists(), (
            "ATTACH is now guarded; the recorded boundary is out of date"
        )
    finally:
        outside.close()
        iso.remove_protected_root(later)
        iso.drop_node_from_ledger(guarded.node_id)


def test_an_inherited_data_dir_override_is_captured_at_conftest_import(tmp_path):
    """Spec section 5.2 asks for the CAPTURE, not for the resolver.

    Passing an explicit dict to `resolve_protected_roots` unit-tests the
    resolver and says nothing about whether the value inherited from the
    maintainer's shell is read before any test can move it. This runs a real
    conftest import under a hostile override and reads the resulting policy.
    """
    import shlex

    custom = tmp_path / "custom-data"
    probe = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
        "import conftest  # noqa: F401 -- installs the detector at import\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
        "import _lib_test_isolation as iso\n"
        "print(json.dumps(list(iso.protected_roots())))\n"
    )
    # Through a shell that strips the environment first. A Python child launched
    # directly receives the bootstrap envelope from the interceptor and installs
    # the roots its PARENT classified, so it could never show whether a fresh
    # process reads the override for itself.
    command = (
        f"env -i HOME={shlex.quote(str(tmp_path / 'fake-home'))} "
        f"PATH=/usr/bin:/bin TZ=Etc/UTC "
        f"CCTALLY_DATA_DIR={shlex.quote(str(custom))} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}"
    )
    proc = subprocess.run(["/bin/sh", "-c", command],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    roots = json.loads(proc.stdout.strip().splitlines()[-1])
    assert os.path.realpath(str(custom)) in roots, roots
