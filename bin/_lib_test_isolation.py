"""Fail-closed production-write detector for the pytest half of the estate.

#529 S4, spec sections 5.2 and 5.3. Preserve-item 3 says no test may write to
the maintainer's real production data. Until now that was an assertion; this
module is the mechanism that enforces it.

WHAT IT COVERS, and what it deliberately does not. A `sys.addaudithook` in each
pytest worker refuses a covered write against a canonical protected path. Two
independent mechanisms extend that policy to child processes: the worker
publishes the bootstrap envelope into its OWN environment, so any child that
inherits it installs the detector whatever launch primitive created it, and a
`subprocess.Popen` interceptor injects the same envelope into a child launched
with an explicit environment dict that would not have inherited it. A launch
audit backstop rejects a Python child that reaches neither, and rejects equally
a Python child whose own `-S`, `-E` or `-I` makes the bootstrap unreachable
however complete its environment is. It promises nothing for a native
non-Python child, which carries no hook and is never attributed to the active
test, nor for Python reached through an external launcher that deliberately
clears the injected environment, nor for a Python child started by a primitive
that raises no audit event at all. `bin/_lib-harness-env.sh` governs the shell
half of the estate by pinning six environment variables; it enforces no
filesystem policy at all, so it is a different guarantee rather than the same
one by another route.

FAIL-CLOSED MEANS THE LEDGER, NOT THE EXCEPTION. Every violation is written to
a worker-private ledger BEFORE the hook raises, and teardown fails from the
ledger. A test that catches the raised error and returns normally still fails.
Under `pytest -n`, a worker's `session.exitstatus` does not reach the run's exit
code, so every worker ledger lives under one per-session directory and the
controller aggregates them at session finish, where setting `exitstatus` does
decide the run.

THE WRITE PREDICATE DECODES FLAGS BECAUSE `os.open` CARRIES NO MODE STRING.
Verified by execution against a live audit hook: `os.open` raises `open` with
`mode` set to None and only the `flags` integer meaningful, and the estate uses
`os.open` widely -- `_append_json_line` below is one example. A predicate that
inspects the mode string for 'w', 'a' or '+', which is the natural way to write
it, therefore misses every such open. The mode string is consulted only as a
supplement that can add a positive, never as the criterion.

SQLITE IS GUARDED AT `connect`, NOT AT ITS OPENS. Verified by execution: a real
INSERT plus commit on an already-open connection raises NO audit event at all,
while a fresh connect raises `sqlite3.connect`. Two consequences are recorded
in the boundary rather than papered over -- an `ATTACH DATABASE` issued on a
connection that was allowed to open reaches no `sqlite3.connect` event, and any
write through a descriptor obtained before the guard installed is likewise
invisible.

This module is test-only. It ships in the public mirror because a public
checkout must be able to run its own suite, and it is deliberately absent from
the npm `files[]` list for the same reason `bin/_lib_test_evidence.py` is.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import pathlib
import re
import sys
import threading
import time

# --- reserved environment keys -------------------------------------------
#
# The interceptor OVERWRITES exactly these in a child's environment and copies
# everything else the caller supplied byte for byte. HOME, CCTALLY_DATA_DIR,
# PATH and the rest are preserved, so the tests that build a fresh env dict on
# purpose keep the data-directory layout they were written for.
ENV_LEDGER = "CCTALLY_ISOLATION_LEDGER"
ENV_HANDSHAKE = "CCTALLY_ISOLATION_HANDSHAKE"
ENV_NODE = "CCTALLY_ISOLATION_NODE"
ENV_TOKEN = "CCTALLY_ISOLATION_TOKEN"
ENV_KERNEL_DIR = "CCTALLY_ISOLATION_KERNEL"
ENV_ROOTS = "CCTALLY_ISOLATION_ROOTS"
ENV_BOOTSTRAP_DIR = "CCTALLY_ISOLATION_BOOTSTRAP"
ENV_SCRATCH = "CCTALLY_ISOLATION_SCRATCH"
ENV_SESSION = "CCTALLY_ISOLATION_SESSION"

RESERVED_ENV_KEYS = (
    ENV_LEDGER, ENV_HANDSHAKE, ENV_NODE, ENV_TOKEN,
    ENV_KERNEL_DIR, ENV_ROOTS, ENV_BOOTSTRAP_DIR, ENV_SCRATCH,
    ENV_SESSION,
)


class ProductionWriteBlocked(RuntimeError):
    """Raised by the audit hook when a covered write reaches a protected path."""


class IsolationBypassBlocked(RuntimeError):
    """Raised by the backstop when a Python launch carries no bootstrap envelope."""


@dataclasses.dataclass(frozen=True)
class Violation:
    node_id: str
    worker_id: str
    pid: int
    event: str
    path: str
    kind: str = "write"
    detail: str = ""

    def describe(self) -> str:
        if self.kind == "temp-hygiene":
            # #963 spec section 8: the wording the temp hygiene guard reports.
            return f"temp hygiene: {self.path} left {self.detail} by {self.node_id}"
        return (
            f"{self.kind} violation: {self.event} -> {self.path} "
            f"(node={self.node_id} worker={self.worker_id} pid={self.pid})"
        )


# --- pure predicates ------------------------------------------------------

# O_RDONLY is 0, O_WRONLY is 1, O_RDWR is 2, so a bare `flags & O_WRONLY` test
# reads False for O_RDWR. The access mode is the low two bits and has to be
# extracted, not masked. os.O_ACCMODE is not present on every platform this
# runs on, so the constant is spelled out.
_O_ACCMODE = 0o3
_WRITE_INTENT_FLAGS = (
    getattr(os, "O_CREAT", 0)
    | getattr(os, "O_TRUNC", 0)
    | getattr(os, "O_APPEND", 0)
)


def is_write_flags(flags) -> bool:
    """True when an `open` audit event's flags integer describes a write.

    Decodes O_WRONLY, O_RDWR, O_CREAT, O_TRUNC and O_APPEND. Never consults a
    mode string -- `os.open` raises this event with mode set to None.
    """
    if not isinstance(flags, int) or flags < 0:
        return False
    if (flags & _O_ACCMODE) in (os.O_WRONLY, os.O_RDWR):
        return True
    return bool(flags & _WRITE_INTENT_FLAGS)


def mode_suggests_write(mode) -> bool:
    """A SUPPLEMENT to `is_write_flags`, never a substitute for it.

    `io.open` raises its own `open` event carrying the mode string and a zero
    flags integer, so reading the mode adds a positive there. It can never be
    the criterion, because an `os.open` carries mode None.
    """
    if not isinstance(mode, str):
        return False
    return any(ch in mode for ch in ("w", "a", "x", "+"))


def is_writable_sqlite_target(database) -> bool:
    """True unless the connect target is PROVABLY read-only.

    The `sqlite3.connect` audit event carries only the database argument and no
    mode field, so a read-only connect is distinguishable solely by parsing the
    URI form. Anything not provably read-only is treated as a write.
    """
    if not isinstance(database, str):
        # A Path or bytes target names a real file; nothing proves it read-only.
        return not isinstance(database, int)
    text = database.strip()
    if text == ":memory:":
        return False
    if not text.startswith("file:"):
        return True
    query = text.partition("?")[2]
    if not query:
        return True
    import urllib.parse

    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    modes = params.get("mode", [])
    if any(m in ("ro", "memory") for m in modes):
        return False
    if any(m.lower() in ("1", "true", "yes") for m in params.get("immutable", [])):
        return False
    return True


def _password_home() -> pathlib.Path:
    """The real user's home from the password database, not from $HOME.

    Every test runs under a faked HOME, so a guard derived from the environment
    would watch the fixture directory rather than the directory at risk.
    """
    try:
        import pwd

        return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return pathlib.Path.home()


def resolve_protected_roots(env=None) -> tuple[pathlib.Path, ...]:
    """The canonical protected paths, resolved once at hook-install time.

    `_cctally_core._real_prod_data_dir()` is a deliberate monkeypatch seam, so
    resolving it here -- before any test runs -- is what stops a test moving the
    guard by patching the resolver.

    An inherited CCTALLY_DATA_DIR is NOT optional. It outranks HOME in
    `_init_paths_from_env`, while `_real_prod_data_dir()` unconditionally
    returns the password-home path, so a maintainer with a custom data directory
    would otherwise have tests resolving and writing there while a detector
    built only on the password home watched somewhere else entirely.
    """
    env = os.environ if env is None else env
    # Imported lazily and never at module scope: the child bootstrap imports
    # this module before anything else runs, and a module-scope import would put
    # _cctally_core into every child's sys.modules before its own first line.
    try:
        import _cctally_core

        prod = pathlib.Path(_cctally_core._real_prod_data_dir())
        statusline = pathlib.Path(_cctally_core.STATUSLINE_OAUTH_CACHE_PATH)
    except Exception:
        home = _password_home()
        prod = home / ".local" / "share" / "cctally"
        statusline = pathlib.Path("/tmp/claude-statusline-usage-cache.json")

    home = _password_home()
    candidates = [
        prod,
        home / ".local" / "share" / "cctally-dev",
        home / ".local" / "share" / "ccusage-subscription",
        home / ".claude",
        home / ".codex",
        statusline,
    ]
    override = (env.get("CCTALLY_DATA_DIR") or "").strip()
    if override:
        candidates.append(pathlib.Path(override).expanduser())

    seen: dict[str, pathlib.Path] = {}
    for path in candidates:
        resolved = pathlib.Path(os.path.realpath(str(path)))
        seen.setdefault(str(resolved), resolved)
    return tuple(seen.values())


def resolve_child_app_dir(env) -> str | None:
    """The APP_DIR the CHILD's environment resolves to, not the parent's.

    Mirrors `_init_paths_from_env`'s first-match rule for the two branches an
    environment mapping can decide on its own. The dev-checkout branch is not
    reproduced, because whether a child is a checkout is a property of the
    binary it runs rather than of its environment; both HOME-derived layouts are
    returned instead so neither is missed.
    """
    override = (env.get("CCTALLY_DATA_DIR") or "").strip()
    if override:
        return str(pathlib.Path(override).expanduser())
    home = (env.get("HOME") or "").strip()
    if not home:
        return None
    return str(pathlib.Path(home) / ".local" / "share" / "cctally")


# --- process state --------------------------------------------------------

_LOCAL = threading.local()
_LOCK = threading.Lock()

_INSTALLED = False
_INTERCEPTOR_INSTALLED = False

_ROOTS: tuple[str, ...] = ()
_LEDGER_PATH: str = ""
_HANDSHAKE_PATH: str = ""
_TOKEN: str = ""
_WORKER_ID: str = ""
_NODE_GETTER = None
_SCRATCH_ROOTS: tuple[str, ...] = ()
_SESSION_ID: str = ""

# Short options that make the bootstrap unreachable no matter what the
# environment carries: -S skips site processing, -E ignores PYTHON* variables
# including PYTHONPATH, and -I implies both.
_BOOTSTRAP_DEFEATING_FLAGS = frozenset("SEI")
_VALUE_TAKING_FLAGS = frozenset(("-W", "-X", "--check-hash-based-pycs"))


@dataclasses.dataclass
class _Expected:
    """One Python launch this process made, awaiting its handshake."""

    node_id: str
    proc: object
    bypass: str = ""


# child pid -> _Expected, for the Python launches this process made.
_EXPECTED_HANDSHAKES: dict = {}
# Launches whose handshake had not arrived when their test finished. Reporting
# them against that test would make a blameless test fail whenever a loaded
# runner delayed an interpreter past the grace, so they are resolved once at
# session finish instead, where no deadline is being raced.
_PENDING_HANDSHAKES: dict = {}

_ORIGINAL_POPEN_INIT = None


def worker_id(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("PYTEST_XDIST_WORKER") or "main"


def session_id(env=None) -> str:
    """The identifier every process of one pytest run shares.

    An xdist worker adopts its controller's session, which is what lets the
    controller find the worker ledgers. Anything else mints its own -- notably a
    nested pytest run launched BY a test, whose deliberate violations must never
    be aggregated into the outer run's exit code.

    ADOPTION TAKES TWO CONDITIONS, AND NEITHER ALONE IS ENOUGH. `PYTEST_XDIST_WORKER`
    is inherited by every descendant, so a nested run started by a worker carries
    it too. And being a direct child of the session owner is not enough either,
    because a nested run started by a non-xdist controller is exactly that. So
    the identifier carries its minting process's pid, and a process adopts the
    inherited session only when it is an xdist worker AND its parent is the
    process that minted it. An xdist worker satisfies both, because execnet's
    popen gateway launches it from the controller with `subprocess.Popen`.
    """
    global _SESSION_ID
    if _SESSION_ID:
        return _SESSION_ID
    env = os.environ if env is None else env
    inherited = (env.get(ENV_SESSION) or "").strip()
    owner = inherited.partition("-")[0]
    if inherited and env.get("PYTEST_XDIST_WORKER") and owner == str(os.getppid()):
        _SESSION_ID = inherited
    else:
        _SESSION_ID = f"{os.getpid()}-{os.urandom(4).hex()}"
    return _SESSION_ID


def _published_session_id(env) -> str:
    """The session identifier to hand a child, WITHOUT minting one.

    A child bootstrap runs before pytest-xdist sets `PYTEST_XDIST_WORKER`, so
    minting there would give a worker its own session and hide its ledger from
    the controller that has to read it. Every process that legitimately owns a
    session has already minted one by the time it publishes.
    """
    if _SESSION_ID:
        return _SESSION_ID
    try:
        return (env.get(ENV_SESSION) or "").strip()
    except Exception:
        return ""


def session_dir(env=None) -> pathlib.Path:
    """The per-session directory, inside the suite generation when there is one.

    #963 (note R11): bin/cctally-test-all exports PYTEST_DEBUG_TEMPROOT, the
    generation's private pytest temp root; the session directory lives beneath
    it and is removed with the generation. Without it (a bare pytest run, or a
    root that does not exist) the location is the one it always was.
    """
    import tempfile

    source = os.environ if env is None else env
    root = (source.get("PYTEST_DEBUG_TEMPROOT") or "").strip()
    if root and os.path.isabs(root) and os.path.isdir(root):
        base = pathlib.Path(root) / "cctally-isolation"
    else:
        base = pathlib.Path(tempfile.gettempdir()) / "cctally-isolation"
    return base / session_id(env)


def state_dir(env=None) -> pathlib.Path:
    """The per-worker directory holding the ledger and the handshake file.

    Worker-private is what makes attribution sound. `_guard_real_prod_migration_log`
    warns in its own docstring that under xdist a sibling worker's legitimate
    write can be misattributed to the wrong test; a per-worker file removes that.
    It sits under the per-session directory so the controller, whose exit status
    is the only one the run honours, can read every worker's ledger.
    """
    return session_dir(env) / f"{worker_id(env)}-{os.getpid()}"


def ledger_path() -> str:
    return _LEDGER_PATH


def handshake_path() -> str:
    return _HANDSHAKE_PATH


def token() -> str:
    return _TOKEN


def protected_roots() -> tuple[str, ...]:
    return _ROOTS


# --- ledger ---------------------------------------------------------------


class _FileLock:
    """Serializes the append path against the truncate-and-rewrite path.

    A grandchild appends to the same ledger file while the parent's teardown
    rewrites it to drop one node's rows, and a violation written inside that
    window is lost to the rewrite. `fcntl.flock` is what closes it; on a
    platform without `fcntl` this degrades to no lock rather than to a failure.
    """

    def __init__(self, path: str):
        self._path = (path or "") + ".lock"
        self._fd = None

    def __enter__(self):
        if not self._path:
            return self
        try:
            import fcntl
        except ImportError:
            return self
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except Exception:
            # Not just OSError. `tests/test_selector_state_496_s5b.py` proves a
            # lock-order invariant by monkeypatching `fcntl.flock` to raise, and
            # a teardown that reads the ledger must not turn that into an error
            # of its own. Degrading to no lock is the correct failure here: this
            # is test bookkeeping, not the data the estate is guarding.
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        return self

    def __exit__(self, *exc):
        if self._fd is None:
            return False
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass
        self._fd = None
        return False


def _append_json_line(path: str, payload: dict) -> None:
    if not path:
        return
    line = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _FileLock(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except OSError:
        # Never let the ledger's own failure change a test outcome silently in
        # the other direction: the raise below still fires.
        pass


def _read_json_lines(path: str) -> list[dict]:
    # flock is held per open file description, so a second acquisition from the
    # same thread would block on itself. Every caller that already holds the
    # lock uses the unlocked form directly.
    with _FileLock(path):
        return _read_json_lines_unlocked(path)


def _read_json_lines_unlocked(path: str) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, FileNotFoundError):
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_ledger(path: str | None = None) -> list[Violation]:
    return _violations(_read_json_lines(path or _LEDGER_PATH))


def _violations(records) -> list[Violation]:
    return [
        Violation(
            node_id=r.get("node_id", ""),
            worker_id=r.get("worker_id", ""),
            pid=int(r.get("pid", 0)),
            event=r.get("event", ""),
            path=r.get("path", ""),
            kind=r.get("kind", "write"),
            detail=r.get("detail", ""),
        )
        for r in records
    ]


def read_handshakes(path: str | None = None) -> set[int]:
    return {
        int(r["pid"])
        for r in _read_json_lines(path or _HANDSHAKE_PATH)
        if isinstance(r.get("pid"), int)
    }


def _current_node_id() -> str:
    if _NODE_GETTER is not None:
        try:
            value = _NODE_GETTER()
            if value:
                return str(value)
        except Exception:
            pass
    return os.environ.get(ENV_NODE, "") or "<no active test>"


def _record(event: str, path: str, kind: str = "write") -> Violation:
    violation = Violation(
        node_id=_current_node_id(),
        worker_id=_WORKER_ID or worker_id(),
        pid=os.getpid(),
        event=event,
        path=path,
        kind=kind,
    )
    _append_json_line(
        _LEDGER_PATH,
        {
            "node_id": violation.node_id,
            "worker_id": violation.worker_id,
            "pid": violation.pid,
            "event": violation.event,
            "path": violation.path,
            "kind": violation.kind,
            "at": time.time(),
        },
    )
    return violation



def record_finding(node_id: str, kind: str, event: str, path: str, detail: str = "") -> Violation:
    """Record a finding for an explicit owner (#963). The hygiene guard knows
    the owning node or fixture itself, which may not be the running test, so
    it does not go through ``_current_node_id``."""
    violation = Violation(
        node_id=node_id,
        worker_id=_WORKER_ID or worker_id(),
        pid=os.getpid(),
        event=event,
        path=path,
        kind=kind,
        detail=detail,
    )
    _append_json_line(
        _LEDGER_PATH,
        {
            "node_id": violation.node_id,
            "worker_id": violation.worker_id,
            "pid": violation.pid,
            "event": violation.event,
            "path": violation.path,
            "kind": violation.kind,
            "detail": violation.detail,
            "at": time.time(),
        },
    )
    return violation

# --- the #965 test retention policy: declared launch controls -------------

RETENTION_SWITCH = "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF"
RETENTION_CONTROL_KINDS = ("production-behavior", "independent-start")


@contextlib.contextmanager
def retention_launch_control(kind: str, reason: str):
    """Declare the launches inside this block a deliberate #965 control.

    ``production-behavior``: the switch is deliberately absent, or the code is
    deliberately packaged, so production retention applies.
    ``independent-start``: the switch is deliberately absent to prove an entry
    point sets it itself; the executed code must still be checkout code. The
    declaration is thread-local, ends with the block and is the only way a
    covered launch may run without the test policy (spec 5.7)."""
    if kind not in RETENTION_CONTROL_KINDS:
        raise ValueError(f"unknown retention control kind {kind!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a retention control needs a stated reason")
    previous = getattr(_LOCAL, "retention_control", None)
    _LOCAL.retention_control = kind
    try:
        yield
    finally:
        _LOCAL.retention_control = previous


# --- the #965 test retention launch guard (spec 5.7) ----------------------

RETENTION_POLICY_KIND = "retention-policy"
#: The standalone transcript builders of spec 4.3. tests/_retention_entry_points.py
#: derives the same set from the builders' own AST, and
#: tests/test_harness_dev_autodetect_coverage.py::
#: test_965_the_builder_population_is_derived_not_listed asserts equality.
RETENTION_COVERED_BUILDERS = ("build-bench-fixtures.py", "build-codex-reader-fixtures.py")
_RETENTION_AUDIT = False
_RET_SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh"))
#: Long shell options whose argument is the next word. A short-option cluster
#: consumes one following word per `o` / `O` in it (`-o pipefail`, `+o posix`,
#: `-eo pipefail`, `-O extglob`).
_RET_SHELL_ARG_OPTIONS = frozenset(("--rcfile", "--init-file", "--emulate"))
_RET_SHELL_PREFIXES = frozenset(("exec", "command", "nohup", "time", "builtin"))
#: Per command prefix: the single-letter options it takes, and the ones among
#: them whose argument follows (attached, or as the next word). Any other
#: option fails closed (965-DIFF-002).
_RET_PREFIX_OPTIONS = {"exec": ("cla", "a"), "command": ("pvV", ""), "time": ("p", ""),
                       "nohup": ("", ""), "builtin": ("", "")}
#: Reserved words that may open a command (`if cmd`, `! cmd`, `{ cmd; }`, `do cmd`).
_RET_RESERVED = frozenset(("!", "{", "if", "then", "elif", "else", "do", "while", "until"))
#: `env`'s short options (GNU `+C:iS:u:v0`, BSD `0iL:P:S:U:u:v`) that take an
#: argument, and its long options. -C / --chdir, -P, -L and -U change where the
#: program resolves or what its environment holds, so they fail closed.
_RET_ENV_ARG_FLAGS = "CLPSUu"
_RET_ENV_LONG = ("ignore-environment", "unset", "split-string", "chdir", "null", "debug",
                 "block-signal", "default-signal", "ignore-signal",
                 "list-signal-handling", "help", "version")
#: Python long options whose argument is the next word.
_RET_PYTHON_ARG_LONG = frozenset(("--check-hash-based-pycs",))
_RET_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RET_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


class RetentionPolicyBlocked(IsolationBypassBlocked):
    """Raised when a covered cctally or transcript-builder launch would run
    without the #965 test retention policy."""


def install_retention_launch_audit() -> None:
    """Enable the retention arm in THIS process. tests/conftest.py calls it;
    `install_child_from_env` never does, so only pytest processes judge."""
    global _RETENTION_AUDIT
    _RETENTION_AUDIT = True


def _ret_covered(token) -> bool:
    """Token-level and deliberately broad: any basename `cctally` or a covered
    builder. `_ret_is_stub` narrows it once the program is resolved."""
    name = os.path.basename(token)
    return name == "cctally" or name in RETENTION_COVERED_BUILDERS


def _ret_is_stub(resolved) -> bool:
    """A resolved `cctally` with no `_cctally_core.py` beside it is a stub (the
    fake installs of setup, update and doctor tests), not cctally code: every
    real entry point -- a checkout, a worktree, a copied layout, an npm package,
    a Cellar libexec, the frozen release -- imports its siblings from its own
    directory, so such a file cannot reach a prune path and is not a covered
    launch (spec 5.7: "a `bin/cctally` invocation"). An unresolvable program is
    never a stub: it fails closed. That includes a path that names no file, such
    as a relative program the guard resolves against the launch's cwd after a
    `cd` inside the shell string moved the real one: only an existing file can
    be shown to be a stub."""
    if resolved is None:
        return False
    path = pathlib.Path(resolved)
    return (path.name == "cctally" and path.is_file()
            and not (path.parent / "_cctally_core.py").is_file())


def _ret_expand(token: str, env: dict) -> str:
    return _RET_VAR_RE.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), token)


def _ret_unparsed(words, env):
    """Fail closed: words the guard cannot interpret that name a covered program."""
    if any(_ret_covered(word) for word in words):
        yield (None, env)


def _ret_env_long(name):
    """GNU getopt_long resolution: an exact name, else an unambiguous prefix."""
    if name in _RET_ENV_LONG:
        return name
    matches = [option for option in _RET_ENV_LONG if option.startswith(name)]
    return matches[0] if name and len(matches) == 1 else None


def _ret_env_launches(argv, env, depth):
    """`env [OPTION]... [NAME=VALUE]... [COMMAND [ARG]...]`: every option is
    interpreted, `-S` / `--split-string` is split into the arguments it stands
    for, and anything else fails closed (965-DIFF-002)."""
    import shlex
    child, rest, index = dict(env), list(argv), 1
    while index < len(rest):
        arg, after = rest[index], index + 1
        action = value = None
        if arg == "--":
            index += 1
            break
        if arg == "-":  # the obsolete spelling of -i
            child, index = {}, after
            continue
        if arg.startswith("--"):
            name, has_value, value = arg[2:].partition("=")
            action = _ret_env_long(name)
            if action in ("unset", "split-string", "chdir") and not has_value:
                value, after = (rest[after] if after < len(rest) else None), after + 1
        elif arg.startswith("-"):
            cluster = arg[1:]
            for pos, flag in enumerate(cluster):
                if flag == "i":
                    child = {}
                elif flag in "v0":
                    continue
                elif flag in _RET_ENV_ARG_FLAGS:
                    action = {"u": "unset", "S": "split-string"}.get(flag, "chdir")
                    value = cluster[pos + 1:]
                    if not value:
                        value, after = (rest[after] if after < len(rest) else None), after + 1
                    break
                else:
                    action = None
                    yield from _ret_unparsed(rest[index:], child)
                    return
            else:
                index = after
                continue
        elif _RET_ASSIGNMENT_RE.match(arg):
            key, value = arg.split("=", 1)
            child[key], index = value, after
            continue
        else:
            break
        if action == "ignore-environment":
            child = {}
        elif action in ("help", "version"):
            return  # prints and runs nothing
        elif action == "unset" and value is not None:
            child.pop(value, None)
        elif action == "split-string" and value is not None:
            try:
                rest = rest[:after] + shlex.split(value) + rest[after:]
            except ValueError:
                yield from _ret_unparsed(rest[index:], child)
                return
        elif action not in ("null", "debug", "block-signal", "default-signal",
                            "ignore-signal", "list-signal-handling"):
            # chdir, -P, -L, -U, an unknown or ambiguous option, a missing argument
            yield from _ret_unparsed(rest[index:], child)
            return
        index = after
    yield from _ret_argv_launches(rest[index:], child, depth + 1)


def _ret_argv_launches(argv, env, depth=0):
    """Yield (program, final child env) for every covered program argv starts.
    A program of None marks a launch the guard cannot interpret (fail closed)."""
    if depth > 6 or not argv:
        return
    argv = [os.fsdecode(a) for a in argv]
    head = os.path.basename(argv[0])
    if head == "env":
        yield from _ret_env_launches(argv, env, depth)
        return
    if head in _RET_SHELLS:
        # Options first, wherever -c sits among them; the command string is the
        # first operand after them (`bash -c -e 'cmd'`, `bash -o pipefail -c ...`).
        index, command_mode = 1, False
        while index < len(argv):
            arg = argv[index]
            if arg in ("--", "-"):
                index += 1
                break
            if arg in _RET_SHELL_ARG_OPTIONS:
                index += 2
                continue
            if arg.startswith("--"):
                index += 1
                continue
            if len(arg) > 1 and arg[0] in "-+":
                command_mode = command_mode or (arg[0] == "-" and "c" in arg[1:])
                index += 1 + sum(1 for flag in arg[1:] if flag in "oO")
                continue
            break
        if command_mode and index < len(argv):
            yield from _ret_shell_launches(argv[index], env, depth + 1)
        return  # `bash script.sh`: a script file is the static check's job
    if head.startswith("python"):
        index = 1
        while index < len(argv):
            arg = argv[index]
            if arg in _RET_PYTHON_ARG_LONG:
                index += 2
                continue
            if arg.startswith("--"):
                index += 1
                continue
            if arg.startswith("-") and arg != "-":
                # A short cluster: -W / -X take the rest of it or the next
                # word (`-W error`, `-bW error`, `-Xdev`); -c / -m start a driver.
                cluster = arg[1:]
                for pos, flag in enumerate(cluster):
                    if flag in "cm":
                        return
                    if flag in "WX":
                        index += pos == len(cluster) - 1
                        break
                index += 1
                continue
            if _ret_covered(arg):
                yield (arg, env)
            return
        return
    if _ret_covered(argv[0]):
        yield (argv[0], env)


def _ret_shell_launches(command, env, depth):
    import shlex
    # A backslash-newline is a line continuation, removed before parsing (POSIX
    # 2.2.1): it must not become a `;` that splits one command into two.
    text = os.fsdecode(command).replace("\\\n", "")
    try:
        lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        if any(_ret_covered(word) for word in re.split(r"[\s;&|()'\"]+", text) if word):
            yield (None, {})  # names a covered program but cannot be parsed: fail closed
        return
    shell_env, segment = dict(env), []
    for token in tokens + [";"]:
        if token and set(token) <= set(";&|()"):
            yield from _ret_segment_launches(segment, shell_env, depth)
            segment = []
        else:
            segment.append(token)


def _ret_prefixes(words):
    """Parse the command prefixes (`exec`, `command`, `nohup`, `time`,
    `builtin`) heading ``words``, with their options.

    Returns (rest, clears): ``rest`` is what then runs -- empty after `command
    -v` / `-V`, which only describe a program -- and ``clears`` says `exec -c`
    emptied the environment. None when a prefix carries an option the guard
    cannot interpret: the caller fails closed, never skips the segment
    (965-DIFF-002)."""
    clears = False
    while words and words[0] in _RET_SHELL_PREFIXES:
        prefix, index = words[0], 1
        flags, with_argument = _RET_PREFIX_OPTIONS[prefix]
        while index < len(words) and len(words[index]) > 1 and words[index][0] == "-":
            word = words[index]
            index += 1
            if word == "--":
                break
            seen = ""
            for pos, flag in enumerate(word[1:]):
                if flag not in flags:
                    return None
                seen += flag
                if flag in with_argument:
                    index += pos == len(word) - 2  # the argument is the next word
                    break
            if "v" in seen or "V" in seen:
                return [], clears
            clears = clears or "c" in seen
        words = words[index:]
    return words, clears


def _ret_segment_launches(words, shell_env, depth):
    while words and words[0] in _RET_RESERVED:
        words = words[1:]
    parsed = _ret_prefixes(words)
    if parsed is None:
        yield from _ret_unparsed([_ret_expand(w, shell_env) for w in words], shell_env)
        return
    words, clears = parsed
    if not words:
        return
    if words[0] == "export":
        for word in words[1:]:
            if _RET_ASSIGNMENT_RE.match(word):
                key, value = word.split("=", 1)
                shell_env[key] = _ret_expand(value, shell_env)
        return
    if words[0] == "unset":
        for word in words[1:]:
            if not word.startswith("-"):
                shell_env.pop(word, None)
        return
    local, index = dict(shell_env), 0
    while index < len(words) and _RET_ASSIGNMENT_RE.match(words[index]):
        key, value = words[index].split("=", 1)
        local[key] = _ret_expand(value, shell_env)
        index += 1
    if index == len(words) and not clears:  # a bare assignment changes an exported variable
        for word in words:
            key, value = word.split("=", 1)
            if key in shell_env:
                shell_env[key] = _ret_expand(value, shell_env)
        return
    # A prefix may also follow the assignments (`FOO=1 exec cmd`), which reach it.
    parsed = _ret_prefixes(words[index:])
    if parsed is None:
        yield from _ret_unparsed([_ret_expand(w, local) for w in words[index:]], local)
        return
    rest, cleared = parsed
    argv = [_ret_expand(w, local) for w in rest]
    yield from _ret_argv_launches(argv, {} if clears or cleared else local, depth + 1)


def _ret_resolve(program, cwd, env):
    if not program:
        return None
    token = _ret_expand(program, env)
    if "$" in token:
        return None
    if "/" not in token:
        import shutil
        found = shutil.which(token, path=env.get("PATH", os.defpath))
        return os.path.realpath(found) if found else None
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(token)))


def _retention_launch_failure(event, args) -> str | None:
    """The #965 retention arm of the launch audit (spec 5.7).

    A covered launch -- `bin/cctally` or a standalone transcript builder --
    must reach its child with the switch at exactly "1" AND execute code with a
    .git entry at its root, unless the calling thread declared a control. It
    records to the ledger before the caller raises, and never repairs or
    injects the switch."""
    if not (_RETENTION_AUDIT and _INTERCEPTOR_INSTALLED):
        return None
    if event == "os.system":
        command = args[0] if args else None
        if command is None:
            return None
        launches, cwd = list(_ret_shell_launches(command, dict(os.environ), 0)), os.getcwd()
    else:
        if event == "subprocess.Popen":
            executable, argv, cwd, env = (list(args) + [None] * 4)[:4]
        else:  # os.posix_spawn and os.exec: (path, argv, env)
            executable, argv, env = (list(args) + [None] * 3)[:3]
            cwd = None
        if isinstance(argv, (str, bytes, os.PathLike)):
            argv = [argv]
        argv = [os.fsdecode(a) for a in (argv or [])]
        if executable is not None and argv:
            argv[0] = os.fsdecode(executable)
        effective = dict(os.environ) if env is None else {
            os.fsdecode(k): os.fsdecode(v) for k, v in dict(env).items()}
        launches = list(_ret_argv_launches(argv, effective))
        cwd = os.fsdecode(cwd) if cwd is not None else os.getcwd()
    control = getattr(_LOCAL, "retention_control", None)
    if control == "production-behavior":
        return None
    for program, final_env in launches:
        resolved = _ret_resolve(program, cwd, final_env)
        if _ret_is_stub(resolved):
            continue
        eligible = resolved is not None and (
            pathlib.Path(resolved).parent.parent / ".git").exists()
        value = final_env.get(RETENTION_SWITCH)
        if eligible and (value == "1" or control == "independent-start"):
            continue
        reasons = []
        if program is None:
            reasons.append("the launch names a covered program but cannot be parsed")
        elif not eligible:
            reasons.append(f"it executes {resolved or program!r}, which has no .git "
                           "entry at its root")
        if value != "1" and control != "independent-start":
            reasons.append(f"{RETENTION_SWITCH} is absent" if value is None
                           else f"{RETENTION_SWITCH} is {value!r}, not '1'")
        violation = _record(event, resolved or program or "<shell>",
                            kind=RETENTION_POLICY_KIND)
        return (violation.describe()
                + " -- a covered launch would run without the #965 test retention "
                "policy: " + "; ".join(reasons) + ". Carry "
                f"{RETENTION_SWITCH}=1 into the child from checkout code, or declare "
                "a deliberate control with _lib_test_isolation.retention_launch_control"
                "(kind, reason). See docs/fixtures-gotchas.md, \"Conversation "
                "retention is off in test environments (#965)\".")
    return None


# --- path matching --------------------------------------------------------


def _match_root(resolved: str) -> str | None:
    for root in _ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return root
    return None


def _guarded_path(raw) -> str | None:
    """The canonical form of `raw` when it lands inside a protected root.

    Canonicalization is not optional: a write reaching a guarded root through a
    symlink whose own path is not guarded has to be caught, and only realpath
    sees it. `os.path.realpath` is non-strict, so a leaf that does not exist yet
    still resolves.
    """
    if raw is None or isinstance(raw, int):
        return None
    try:
        text = os.fsdecode(raw)
    except (TypeError, ValueError):
        return None
    if not text:
        return None
    try:
        resolved = os.path.realpath(text)
    except (OSError, ValueError):
        return None
    return resolved if _match_root(resolved) else None


# --- the audit hook -------------------------------------------------------


def _open_targets(args):
    path = args[0] if len(args) > 0 else None
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if is_write_flags(flags) or mode_suggests_write(mode):
        return (path,)
    return ()


def _sqlite_targets(args):
    database = args[0] if args else None
    return (database,) if is_writable_sqlite_target(database) else ()


def _first(args):
    return (args[0],) if args else ()


def _second(args):
    return (args[1],) if len(args) > 1 else ()


def _both(args):
    return tuple(args[:2])


# os.remove and os.unlink are the same C entry point and both raise `os.remove`;
# os.replace raises `os.rename`. os.link guards BOTH ends, because a hardlink
# into a protected file is a write-capable alias for it; os.symlink guards only
# the entry being created, since its target is just a string.
_EVENT_TARGETS = {
    "open": _open_targets,
    "os.remove": _first,
    "os.rename": _both,
    "os.truncate": _first,
    "os.mkdir": _first,
    "os.rmdir": _first,
    "os.chmod": _first,
    "os.chown": _first,
    "os.link": _both,
    "os.symlink": _second,
    "os.utime": _first,
    "sqlite3.connect": _sqlite_targets,
    "shutil.copymode": _second,
    "shutil.copystat": _second,
}


# Every launch primitive in the standard library that raises an audit event.
# `subprocess.Popen` is not the only one, and three of the others were in live
# use in this estate: os.posix_spawn, os.system and the os.exec family all
# start processes that no Popen interceptor ever sees.
_LAUNCH_EVENTS = ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.system")


def _audit_hook(event, args):
    if getattr(_LOCAL, "busy", False):
        return
    if event in _LAUNCH_EVENTS:
        _LOCAL.busy = True
        try:
            retention = _retention_launch_failure(event, args)
            failure = None if retention is not None else _launch_backstop_failure(event, args)
        finally:
            _LOCAL.busy = False
        if retention is not None:
            raise RetentionPolicyBlocked(retention)
        if failure is not None:
            raise IsolationBypassBlocked(failure)
        return
    extract = _EVENT_TARGETS.get(event)
    if extract is None:
        return
    if not _ROOTS:
        return
    _LOCAL.busy = True
    try:
        hit = None
        for target in extract(args):
            hit = _guarded_path(target)
            if hit is not None:
                break
        if hit is None:
            return
        violation = _record(event, hit)
    finally:
        _LOCAL.busy = False
    raise ProductionWriteBlocked(
        violation.describe()
        + " -- preserve-item 3 forbids a test writing to the maintainer's real "
        "production data. Pin the path with redirect_paths(ns, monkeypatch, "
        "tmp_path), or with a narrower monkeypatch.setattr(_cctally_core, ...)."
    )


# --- the subprocess interceptor ------------------------------------------


def _looks_like_python(executable, args) -> bool:
    candidates = []
    if isinstance(executable, (str, bytes, os.PathLike)):
        candidates.append(os.fsdecode(executable))
    if isinstance(args, (list, tuple)) and args:
        head = args[0]
        if isinstance(head, (str, bytes, os.PathLike)):
            candidates.append(os.fsdecode(head))
    for candidate in candidates:
        name = os.path.basename(candidate)
        if name.startswith("python") or name == "pytest":
            return True
    # A script run through its own shebang is a Python launch that no basename
    # test can see. bin/cctally is exactly that shape.
    for candidate in candidates:
        try:
            with open(candidate, "rb") as fh:
                first = fh.readline(256)
        except (OSError, ValueError):
            continue
        if first.startswith(b"#!") and b"python" in first:
            return True
    return False


def envelope_present(env) -> bool:
    """True when `env` would let a Python child install the detector itself."""
    try:
        return env.get(ENV_TOKEN) == _TOKEN and bool(env.get(ENV_LEDGER))
    except Exception:
        return False


def python_flags_defeat_bootstrap(argv) -> bool:
    """True when the interpreter's own options make the bootstrap unreachable.

    `-S`, `-E` and `-I` each stop `site` from importing the bootstrap that the
    envelope points at, so such a child carries no detector however complete its
    environment is. Scanning stops at `-c` or `-m`, after which the remaining
    tokens belong to the program rather than to the interpreter.
    """
    if not isinstance(argv, (list, tuple)):
        return False
    tokens = []
    for raw in list(argv)[1:]:
        if isinstance(raw, (str, bytes, os.PathLike)):
            try:
                tokens.append(os.fsdecode(raw))
            except (TypeError, ValueError):
                return False
        else:
            return False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _VALUE_TAKING_FLAGS:
            index += 2
            continue
        if not token.startswith("-") or token == "-":
            return False
        if token.startswith("--"):
            index += 1
            continue
        letters = set(token[1:])
        if letters & _BOOTSTRAP_DEFEATING_FLAGS:
            return True
        if letters & {"c", "m"}:
            return False
        index += 1
    return False


_SHELL_SEPARATORS = frozenset((";", "&&", "||", "|", "&"))


def _shell_command_defeats_bootstrap(command) -> bool:
    """True when a shell command line starts a Python interpreter with -S, -E or -I.

    `os.system` carries only the command string, so the argv every other audited
    primitive hands the backstop directly has to be recovered from it. This is
    best effort by construction: a command whose head the shell would produce by
    expansion is not reconstructed, and neither is one `shlex` cannot split.
    """
    if not isinstance(command, (str, bytes, os.PathLike)):
        return False
    try:
        text = os.fsdecode(command)
    except (TypeError, ValueError):
        return False
    import shlex

    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        # Every token, not only the head: `env -i ... python -S -c ...` puts the
        # interpreter in the middle of its own segment.
        for index, token in enumerate(segment):
            if _looks_like_python(token, [token]) and python_flags_defeat_bootstrap(
                segment[index:]
            ):
                return True
    return False


_FLAG_BYPASS_REASON = (
    "started with -S, -E or -I, which stops site from importing the bootstrap"
)


def _launch_backstop_failure(event, args) -> str | None:
    """The bypass backstop: reject a Python launch the detector cannot reach.

    Two conditions put a child outside the detector, and both are refused here.
    The child's final environment may carry no bootstrap envelope, which is what
    the two injection mechanisms -- this process's own exported environment and
    the `Popen` interceptor -- exist to prevent. Or the child's own interpreter
    options may make the bootstrap unreachable however complete that environment
    is, which no environment mechanism can fix. Each audit event fires before the
    child executes, so rejecting here rejects before it runs.
    """
    if not _INTERCEPTOR_INSTALLED:
        return None
    if event == "os.system":
        command = args[0] if args else None
        if _shell_command_defeats_bootstrap(command):
            _record(event, "<shell>", kind="bypass")
            return (
                "a shell command starting a Python interpreter "
                f"{_FLAG_BYPASS_REASON} was launched, so that child would run "
                "without the write detector."
            )
        # The shell inherits this process's environment and nothing else, so the
        # remaining question this event can answer is whether that environment
        # would let a Python command bootstrap itself.
        if envelope_present(os.environ):
            return None
        _record(event, "<shell>", kind="bypass")
        return (
            "a shell command was launched from a process whose own environment "
            "carries no isolation bootstrap envelope, so a Python command inside "
            "it would run without the write detector."
        )
    if event == "subprocess.Popen":
        executable = args[0] if len(args) > 0 else None
        argv = args[1] if len(args) > 1 else None
        env = args[3] if len(args) > 3 else None
    else:
        executable = args[0] if len(args) > 0 else None
        argv = args[1] if len(args) > 1 else None
        env = args[2] if len(args) > 2 else None
    if not _looks_like_python(executable, argv):
        return None
    if python_flags_defeat_bootstrap(argv):
        _record(event, str(executable), kind="bypass")
        return (
            f"a Python child was {_FLAG_BYPASS_REASON} "
            f"(event={event} executable={executable!r}). No environment the "
            "launch carries can install the write detector in that child, so "
            "preserve-item 3 would not be enforced there."
        )
    effective = os.environ if env is None else env
    if envelope_present(effective):
        return None
    _record(event, str(executable), kind="bypass")
    return (
        "a Python child was launched without the isolation bootstrap envelope "
        f"(event={event} executable={executable!r}). That launch path does not "
        "install the write detector in the child, so preserve-item 3 would not "
        "be enforced there."
    )


def _backstop_failure(args) -> str | None:
    """The `subprocess.Popen` arm, kept as a name its own tests already drive."""
    return _launch_backstop_failure("subprocess.Popen", args)


def bootstrap_dir() -> str:
    return str(pathlib.Path(__file__).resolve().parent.parent
               / "tests" / "isolation_bootstrap")


def kernel_dir() -> str:
    return str(pathlib.Path(__file__).resolve().parent)


def _pythonpath_with_bootstrap(existing) -> str:
    boot = bootstrap_dir()
    parts = [p for p in (existing or "").split(os.pathsep) if p and p != boot]
    return os.pathsep.join([boot] + parts)


def export_env_envelope(env=None) -> None:
    """Publish the bootstrap envelope into THIS process's own environment.

    `subprocess.Popen` is not the only way to start a process, and the estate
    uses others. Measured from inside a live pytest worker, an `os.posix_spawn`,
    an `os.system` and a `multiprocessing` spawn child each wrote into a
    protected root with no detector installed and no ledger row; the last is in
    live use in six test modules, and on POSIX it reaches
    `_posixsubprocess.fork_exec` directly rather than through any audited launch
    primitive, so no audit backstop can reach it. What every one of them does
    share is that the child inherits this process's environment -- so putting
    the envelope there is what covers them, and it covers a launch primitive
    nobody has thought of yet for the same reason.
    """
    target = os.environ if env is None else env
    if not _LEDGER_PATH:
        return
    published = _published_session_id(target)
    if published:
        target[ENV_SESSION] = published
    target[ENV_LEDGER] = _LEDGER_PATH
    target[ENV_HANDSHAKE] = _HANDSHAKE_PATH
    target[ENV_NODE] = _current_node_id()
    target[ENV_TOKEN] = _TOKEN
    target[ENV_KERNEL_DIR] = kernel_dir()
    target[ENV_BOOTSTRAP_DIR] = bootstrap_dir()
    target[ENV_ROOTS] = os.pathsep.join(_ROOTS)
    target[ENV_SCRATCH] = os.pathsep.join(_SCRATCH_ROOTS)
    target["PYTHONPATH"] = _pythonpath_with_bootstrap(target.get("PYTHONPATH", ""))


#: The temporary-directory variables a fresh child environment must not lose
#: (#940 D4, spec 6.5). Without them Python's `tempfile` falls back to `/tmp`
#: and SQLite to `/var/tmp`, which the confined runner kills.
_TEMP_VARIABLES = ("TMPDIR", "TMP", "TEMP", "SQLITE_TMPDIR")


def _restore_temp_variables(child, parent, platform=None) -> None:
    """Give `child` each temporary variable it lacks, from `parent`.

    Only a missing or empty value is filled; any value the caller set is a
    deliberate override and is kept exactly, valid or not, because tests drive
    precisely that. A parent value is copied only when it names an absolute,
    existing directory, so nothing is invented for a child whose parent has no
    usable one.

    #940 D-2 F9: a copied TMP or TEMP is the root `tempfile` reads when the
    child has no TMPDIR, so the same bound applies to it as to TMPDIR at
    session start (`multiprocessing_temp_root_problem`): a copied value that
    the child would read and Darwin multiprocessing could not use is not
    copied.

    Fix round 2 (V2): a long copy is dropped only when the child still keeps
    a root to read without it: the first remaining variable in `tempfile`'s
    order (TMPDIR, TEMP, TMP) that names an existing directory, when that one
    is bounded or the caller's own. `tempfile` skips a variable that names no
    usable directory, so a caller's own unusable value is not such a root
    (#940 F2, D-2 P3 #5). Dropping the child's only root would send its
    `tempfile` to /tmp, the failure this chokepoint exists to stop, so a long
    copy with nothing to read instead is kept: the child's `tempfile` keeps
    the long root, and only multiprocessing, which cannot use it, falls back
    to /tmp, where confinement kills the write. A caller's own value is never
    dropped, long or not.
    """
    copied = []
    for name in _TEMP_VARIABLES:
        if child.get(name):
            continue
        value = parent.get(name)
        if value and os.path.isabs(value) and os.path.isdir(value):
            child[name] = value
            copied.append(name)

    def long_copy(name):
        return name in copied and bool(child.get(name)) and \
            multiprocessing_temp_root_problem({name: child[name]}, platform) is not None

    def usable(name):
        """A variable `tempfile` would read: set, naming an existing
        directory."""
        return bool(child.get(name)) and os.path.isdir(child[name])

    order = ("TMPDIR", "TEMP", "TMP")  # tempfile's order
    for k in (2, 1):  # TMP, then TEMP: each against what the child reads
        name = order[k]
        if not long_copy(name):
            continue
        others = [n for n in order if n != name and usable(n)]
        # What the child reads with this copy gone: the first remaining
        # variable in tempfile's order that names a usable directory; it is
        # a root to keep when it is bounded or the caller's own.
        if others and not long_copy(others[0]):
            del child[name]


#: The longest temporary root Darwin multiprocessing may use (#940, spec 6.5):
#: a longer one no longer fits its AF_UNIX socket paths, and multiprocessing
#: then ignores it and falls back to `/tmp` -- killed at 68 bytes, fine at 45.
MULTIPROCESSING_TEMP_ROOT_MAX_BYTES = 63


def _restore_suite_temp_root(child, parent) -> None:
    """Give a fresh child environment the suite's private pytest temp root (#963 spec 5.4).

    Only when the child lacks it, the parent's value is an absolute existing
    directory, and the child still reads the parent's TMPDIR: a caller that
    pointed the child's TMPDIR elsewhere decided where its temp goes, and that
    decision is kept. Deliberately not a member of _TEMP_VARIABLES, whose
    membership tests/test_isolation_contract.py pins.
    """
    value = parent.get("PYTEST_DEBUG_TEMPROOT") or ""
    if not value or child.get("PYTEST_DEBUG_TEMPROOT"):
        return
    if not os.path.isabs(value) or not os.path.isdir(value):
        return
    if (child.get("TMPDIR") or "") != (parent.get("TMPDIR") or ""):
        return
    child["PYTEST_DEBUG_TEMPROOT"] = value


def multiprocessing_temp_root_problem(env=None, platform=None):
    """Why Darwin multiprocessing would leave this temporary root, or None.

    Measures the variable `tempfile` would read -- TMPDIR, then TEMP, then
    TMP, skipping empty ones -- in its exact exported spelling, trailing
    separator included. Only Darwin is affected; elsewhere this is None.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return None
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = env.get(name)
        if not value:
            continue
        size = len(os.fsencode(value))
        if size <= MULTIPROCESSING_TEMP_ROOT_MAX_BYTES:
            return None
        return (
            f"{name} is {size} bytes; Darwin multiprocessing needs a temporary "
            f"root of at most {MULTIPROCESSING_TEMP_ROOT_MAX_BYTES} bytes and "
            "otherwise falls back to /tmp (#940, spec 6.5). Run the tests with "
            "a shorter temporary root."
        )
    return None


def _child_environment(base) -> dict:
    """A COPY of the caller's environment with only the reserved keys overwritten.

    Copy rather than merge, because a material set of tests builds a fresh env
    dict that deliberately omits os.environ -- tests/test_five_hour_blocks_json.py
    says so in its own comment -- and those launches are load-bearing: they exist
    so the child resolves a specific data-directory layout.
    The one exception is the temporary variables: a fresh dict that omits them
    sends the child's scratch to `/tmp`, so `_restore_temp_variables` fills
    the ones it lacks from this process.
    """
    child = dict(os.environ) if base is None else {
        os.fsdecode(k): os.fsdecode(v) for k, v in dict(base).items()
    }
    _restore_temp_variables(child, os.environ)
    _restore_suite_temp_root(child, os.environ)

    extra_roots = list(_ROOTS)
    app_dir = resolve_child_app_dir(child)
    if app_dir:
        resolved = os.path.realpath(app_dir)
        if not _under_scratch(resolved) and resolved not in extra_roots:
            extra_roots.append(resolved)

    published = _published_session_id(os.environ)
    if published:
        child[ENV_SESSION] = published
    child[ENV_LEDGER] = _LEDGER_PATH
    child[ENV_HANDSHAKE] = _HANDSHAKE_PATH
    child[ENV_NODE] = _current_node_id()
    child[ENV_TOKEN] = _TOKEN
    child[ENV_KERNEL_DIR] = kernel_dir()
    child[ENV_BOOTSTRAP_DIR] = bootstrap_dir()
    child[ENV_ROOTS] = os.pathsep.join(extra_roots)
    # Scratch roots have to travel with the child. Without them a GRANDCHILD
    # re-classifies its own fake-HOME APP_DIR -- which is legitimate per-test
    # scratch -- as a protected root, and a detached background worker creating
    # that directory after the test finished then arrives as a late violation.
    # Measured: every `cctally five-hour-blocks --json` child in
    # tests/test_five_hour_blocks_json.py produced exactly that false positive.
    child[ENV_SCRATCH] = os.pathsep.join(_SCRATCH_ROOTS)
    child["PYTHONPATH"] = _pythonpath_with_bootstrap(child.get("PYTHONPATH", ""))
    return child


def _under_scratch(resolved: str) -> bool:
    for root in _SCRATCH_ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def install_popen_interceptor() -> None:
    global _INTERCEPTOR_INSTALLED, _ORIGINAL_POPEN_INIT
    import subprocess

    with _LOCK:
        if _INTERCEPTOR_INSTALLED:
            return
        _ORIGINAL_POPEN_INIT = subprocess.Popen.__init__

        def _intercepted_init(self, *a, **kw):
            # `env` is the 11th parameter after self; the estate passes it as a
            # keyword, but a positional caller must not slip past.
            if "env" in kw:
                kw["env"] = _child_environment(kw["env"])
            elif len(a) >= 11:
                a = list(a)
                a[10] = _child_environment(a[10])
                a = tuple(a)
            else:
                kw["env"] = _child_environment(None)
            _ORIGINAL_POPEN_INIT(self, *a, **kw)
            pid = getattr(self, "pid", None)
            if isinstance(pid, int) and pid > 0:
                argv = a[0] if a else kw.get("args")
                # `executable` is the third parameter after self, so a caller
                # that passes it positionally would otherwise be classified from
                # argv alone.
                executable = kw.get("executable")
                if executable is None and len(a) > 2:
                    executable = a[2]
                if _looks_like_python(executable, argv):
                    bypass = ""
                    if python_flags_defeat_bootstrap(argv):
                        bypass = (
                            "the interpreter was started with -S, -E or -I, "
                            "which stops site from importing the bootstrap"
                        )
                    _EXPECTED_HANDSHAKES[pid] = _Expected(
                        node_id=_current_node_id(),
                        proc=self,
                        bypass=bypass,
                    )

        subprocess.Popen.__init__ = _intercepted_init
        _INTERCEPTOR_INSTALLED = True


def unwrapped_popen_init():
    """The saved, uninstrumented `Popen.__init__`, for the backstop's RED case."""
    return _ORIGINAL_POPEN_INIT


# --- installation ---------------------------------------------------------


def install(roots=None, ledger_path=None, node_id_getter=None, env=None) -> None:
    """Install the audit hook once per process and (re)bind its policy.

    An audit hook cannot be removed, so the hook itself is installed once and
    every later call only rebinds the state it reads.
    """
    global _INSTALLED, _ROOTS, _LEDGER_PATH, _HANDSHAKE_PATH
    global _TOKEN, _WORKER_ID, _NODE_GETTER

    env = os.environ if env is None else env
    with _LOCK:
        _WORKER_ID = worker_id(env)
        if roots is None:
            roots = resolve_protected_roots(env)
        # THIS entry point only ever GROWS the policy; `remove_protected_root`
        # is what shrinks it, and the detector's own fixtures use that to retire
        # a synthetic root. Replacing the set wholesale here discarded whatever
        # the previous call had established -- the roots a child received from
        # its parent, or a synthetic root a test registered -- which is a sharp
        # edge on an entry point those fixtures call once per test.
        _ROOTS = tuple(dict.fromkeys(
            list(_ROOTS) + [os.path.realpath(str(r)) for r in roots]
        ))
        if ledger_path is None:
            # state_dir() is what mints or adopts the session identifier, so it
            # is reached ONLY here -- a child handed an explicit ledger must not
            # mint one, because at bootstrap time xdist has not yet stamped
            # PYTEST_XDIST_WORKER and the mint would be wrong.
            ledger_path = str(state_dir(env) / "ledger.jsonl")
        _LEDGER_PATH = str(ledger_path)
        _HANDSHAKE_PATH = str(pathlib.Path(_LEDGER_PATH).parent / "handshakes.jsonl")
        if node_id_getter is not None:
            _NODE_GETTER = node_id_getter
        if not _TOKEN:
            _TOKEN = env.get(ENV_TOKEN) or os.urandom(12).hex()
        try:
            os.makedirs(os.path.dirname(_LEDGER_PATH), exist_ok=True)
        except OSError:
            pass
        if not _INSTALLED:
            sys.addaudithook(_audit_hook)
            _INSTALLED = True
    export_env_envelope()


def install_child_from_env(env=None) -> None:
    """The child half, driven entirely by the reserved keys the parent injected.

    The child NEVER re-derives the protected roots: the parent already
    classified this child's environment-resolved APP_DIR against the registered
    scratch roots, and re-deriving here would lose that decision.
    """
    global _TOKEN
    env = os.environ if env is None else env
    ledger = env.get(ENV_LEDGER)
    if not ledger:
        return
    _TOKEN = env.get(ENV_TOKEN) or ""
    roots = [r for r in (env.get(ENV_ROOTS) or "").split(os.pathsep) if r]
    for scratch in (env.get(ENV_SCRATCH) or "").split(os.pathsep):
        if scratch:
            register_scratch_root(scratch)
    install(roots=roots, ledger_path=ledger, env=env)
    _append_json_line(
        _HANDSHAKE_PATH,
        {"pid": os.getpid(), "token": _TOKEN, "node": env.get(ENV_NODE, ""),
         "at": time.time()},
    )
    install_popen_interceptor()


# --- per-test state -------------------------------------------------------


def add_protected_root(path) -> None:
    """Register a SYNTHETIC protected root for the duration of a test.

    The detector's own tests must never write to a real guarded path, so they
    inject a root under tmp_path and drive the mechanism against that. An
    explicit root wins over a scratch root, which is what lets a synthetic
    production directory live inside tmp_path.
    """
    global _ROOTS
    resolved = os.path.realpath(str(path))
    if resolved not in _ROOTS:
        _ROOTS = _ROOTS + (resolved,)
    export_env_envelope()


def remove_protected_root(path) -> None:
    global _ROOTS
    resolved = os.path.realpath(str(path))
    _ROOTS = tuple(r for r in _ROOTS if r != resolved)
    export_env_envelope()


def register_scratch_root(path) -> None:
    global _SCRATCH_ROOTS
    resolved = os.path.realpath(str(path))
    if resolved not in _SCRATCH_ROOTS:
        _SCRATCH_ROOTS = _SCRATCH_ROOTS + (resolved,)
    export_env_envelope()


def set_node_id_getter(getter) -> None:
    global _NODE_GETTER
    _NODE_GETTER = getter
    export_env_envelope()


def expected_handshakes() -> dict:
    return {pid: entry.node_id for pid, entry in _EXPECTED_HANDSHAKES.items()}


def pending_handshakes() -> dict:
    return {pid: entry.node_id for pid, entry in _PENDING_HANDSHAKES.items()}


def forget_expected_handshakes(node_id: str) -> None:
    for pid, entry in list(_EXPECTED_HANDSHAKES.items()):
        if entry.node_id == node_id:
            _EXPECTED_HANDSHAKES.pop(pid, None)


def _was_signalled_before_handshaking(entry) -> bool:
    """True when this child exited on a signal and never installed the detector.

    A child the test signals before `site` has finished running never reaches
    its own first line, so there is nothing the detector could have failed to
    guard. Found by running the estate: `tests/test_setup_legacy_migrate.py`
    launches a fake poller and SIGTERMs it within about 250 milliseconds.

    THE CARVE-OUT IS DELIBERATELY NOT BOUNDED BY ELAPSED TIME, and it was.
    `flush_late_handshake_problems` re-evaluates this predicate at session
    finish, where the elapsed time is launch-to-session-finish and therefore
    always past any plausible window, so the bound reported every genuine
    startup kill whose `poll()` still returned None at teardown. A threshold
    whose correctness depends on runner load is the class of defect this session
    exists to remove, so the residue it filtered is accepted instead.

    That residue is narrow because the two confounds are covered structurally: a
    child whose own interpreter options defeat the bootstrap is rejected at
    launch by `python_flags_defeat_bootstrap` and never reaches here, and a
    bootstrap that raises writes its own `kind="bootstrap"` ledger row.
    """
    if entry.bypass:
        return False
    try:
        returncode = entry.proc.poll()
    except Exception:
        return False
    return isinstance(returncode, int) and returncode < 0


def _missing_handshake_message(pid: int, node_id: str, reason: str = "") -> str:
    tail = f" ({reason})" if reason else ""
    return (
        f"missing handshake: the Python child pid={pid} launched by {node_id} "
        f"never installed the write detector, so preserve-item 3 was not "
        f"enforced inside it{tail}"
    )


def collect_test_violations(
    node_id: str,
    handshake_grace: float = 2.0,
) -> list[str]:
    """Everything that must fail the test identified by `node_id`.

    This is the function the conftest teardown fails from, so a test that wants
    to prove the teardown contract drives THIS -- never a copy of it.

    Handshakes are waited for within a bounded grace period, because a child
    launched without `wait()` may not have reached its first line yet. Anything
    still missing when the grace elapses is carried to session finish rather
    than blamed on this test, EXCEPT a launch whose own interpreter options
    made the bootstrap unreachable -- no amount of further waiting changes that
    one, and waiting is the only thing session finish adds. Carrying the rest is
    what stops a loaded runner's slow interpreter failing a blameless test.
    """
    problems = [v.describe() for v in read_ledger() if v.node_id == node_id]

    expected = {pid: entry for pid, entry in _EXPECTED_HANDSHAKES.items()
                if entry.node_id == node_id}
    if expected:
        seen = read_handshakes()
        for pid, entry in sorted(expected.items()):
            if pid in seen:
                continue
            if _was_signalled_before_handshaking(entry):
                expected.pop(pid, None)
                _EXPECTED_HANDSHAKES.pop(pid, None)
        deadline = time.monotonic() + handshake_grace
        while expected and not set(expected) <= seen and time.monotonic() < deadline:
            time.sleep(0.02)
            seen = read_handshakes()
        for pid, entry in sorted(expected.items()):
            _EXPECTED_HANDSHAKES.pop(pid, None)
            if pid in seen:
                continue
            if entry.bypass:
                problems.append(
                    _missing_handshake_message(pid, node_id, entry.bypass)
                )
                continue
            _PENDING_HANDSHAKES[pid] = entry
    _prune_resolved_expectations()
    # CONSUME what was reported. Everything still in the ledger at session
    # finish is therefore a late arrival that no test's report could carry, and
    # nothing is counted twice.
    drop_node_from_ledger(node_id)
    return problems


def _prune_resolved_expectations() -> None:
    """Drop every expectation whose handshake has arrived.

    Without this, a launch made outside any test -- keyed `<no active test>`,
    which no `forget_expected_handshakes(node_id)` call ever names -- kept a
    live `Popen` reference for the worker's whole lifetime.
    """
    if not _EXPECTED_HANDSHAKES:
        return
    seen = read_handshakes()
    for pid in list(_EXPECTED_HANDSHAKES):
        if pid in seen:
            _EXPECTED_HANDSHAKES.pop(pid, None)


def flush_late_handshake_problems() -> list[str]:
    """Resolve the handshakes carried past their test, and RECORD what is missing.

    Called by every process at session finish. The findings are written into
    this process's own ledger rather than returned to it alone, because under
    xdist the process that can fail the run is the controller, and the ledger
    is what the controller reads.
    """
    reported = []
    seen = read_handshakes()
    for pid, entry in sorted(list(_PENDING_HANDSHAKES.items())
                             + list(_EXPECTED_HANDSHAKES.items())):
        if pid in seen:
            continue
        if _was_signalled_before_handshaking(entry):
            continue
        message = _missing_handshake_message(pid, entry.node_id, entry.bypass)
        _append_json_line(
            _LEDGER_PATH,
            {
                "node_id": entry.node_id,
                "worker_id": _WORKER_ID or worker_id(),
                "pid": os.getpid(),
                "event": "missing handshake",
                "path": f"pid={pid}",
                "kind": "handshake",
                "detail": message,
                "at": time.time(),
            },
        )
        reported.append(message)
    _PENDING_HANDSHAKES.clear()
    _EXPECTED_HANDSHAKES.clear()
    return reported


def collect_session_violations() -> list[str]:
    """Every violation this RUN produced, across every worker's ledger.

    A violation that only reaches the ledger after its test's report is final
    cannot fail that test, so it fails the session instead. Under `pytest -n`
    the worker that recorded it cannot fail the run either -- a worker's
    `session.exitstatus` is not the controller's, measured as `-n 2` exiting 0
    while the hook fired in gw0 and gw1 -- so this reads every ledger under the
    per-session directory and the controller is what calls it.
    """
    flush_late_handshake_problems()
    out = []
    for path in _session_ledger_paths():
        out.extend(v.describe() for v in read_ledger(path))
    return out


def _session_ledger_paths() -> list[str]:
    base = session_dir()
    try:
        children = sorted(base.iterdir())
    except OSError:
        return [_LEDGER_PATH] if _LEDGER_PATH else []
    paths = [str(child / "ledger.jsonl") for child in children if child.is_dir()]
    if _LEDGER_PATH and _LEDGER_PATH not in paths:
        paths.append(_LEDGER_PATH)
    return paths


def drop_node_from_ledger(node_id: str) -> None:
    """Forget one node's violations. For the detector's OWN tests only.

    Those tests provoke real violations on purpose, so without this their own
    teardown would fail on the evidence they were written to produce. Scoped to
    one node rather than truncating the file, so a genuine violation recorded
    earlier in this worker still reaches session finish.

    The read and the rewrite are one locked critical section: a grandchild
    appending between them would otherwise have its violation erased by the
    rewrite.
    """
    with _FileLock(_LEDGER_PATH):
        records = [
            r for r in _read_json_lines_unlocked(_LEDGER_PATH)
            if r.get("node_id") != node_id
        ]
        try:
            with open(_LEDGER_PATH, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass
    forget_expected_handshakes(node_id)
