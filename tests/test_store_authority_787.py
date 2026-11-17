"""#787 / #861 Task B group 2 — store authority.

Every test here names the failure it detects. The authority model is the
spec's 5.1 capability matrix: a query or diagnostic scope that reaches an
owner entry point (create, migrate, recover, launch, ingest) is a persistent
write from a read path, which is exactly what #787 removes.

Authority state lives in `_cctally_core` because `tests/conftest.py`
reloads every sibling module but never the kernel.
"""
from __future__ import annotations

import contextvars
import importlib
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _cctally_core as core  # noqa: E402


AUTHORITY_KINDS = sorted(core.AUTHORITY_CAPABILITIES)


# ── the capability matrix ────────────────────────────────────────────────


def test_capability_tables_match_the_frozen_matrix():
    """Detects a drifted capability table: a query scope that silently gains
    INGEST_CURRENT or WRITE would let a read path persist."""
    assert core.CAPABILITIES == frozenset({
        "READ", "METADATA", "INGEST_CURRENT", "WRITE", "STARTUP",
        "MAINTENANCE", "LAUNCH"})
    t = core.AUTHORITY_CAPABILITIES
    assert t["query"] == {"READ"}
    assert t["diagnostic"] == {"READ", "METADATA"}
    assert t["request_writer"] == {"READ", "INGEST_CURRENT"}
    assert t["process"] == core.CAPABILITIES
    assert t["writer"] == {"READ", "INGEST_CURRENT", "WRITE"}
    assert t["startup"] == {"READ", "STARTUP"}
    assert t["maintenance"] == {"READ", "MAINTENANCE", "LAUNCH"}


@pytest.mark.parametrize("outer", AUTHORITY_KINDS)
@pytest.mark.parametrize("inner", AUTHORITY_KINDS)
def test_capability_matrix_narrowing(outer, inner):
    """Detects a widening scope: a query phase that re-enters `process` (or any
    superset) would regain owner capabilities inside a read."""
    with core.establish_authority(outer):
        allowed = (core.AUTHORITY_CAPABILITIES[inner]
                   <= core.AUTHORITY_CAPABILITIES[outer])
        if allowed:
            with core.authority_scope(inner) as a:
                assert a.kind == inner
                assert core.current_authority().kind == inner
            assert core.current_authority().kind == outer
        else:
            with pytest.raises(core.QueryAuthorityViolation):
                with core.authority_scope(inner):
                    pass  # pragma: no cover - must not enter
            assert core.current_authority().kind == outer


def test_scope_restores_enclosing_authority():
    """Detects a scope that leaks its narrowed authority to the next phase:
    sequential CLI phases (query, then refresh) must each see the enclosing
    `process` again, also after an exception, and the test thread's own
    authority (conftest D5) again afterwards."""
    before = core.current_authority()
    with core.establish_authority("process"):
        with core.authority_scope("query", policy="command"):
            assert core.current_authority().policy == "command"
        assert core.current_authority().kind == "process"
        with pytest.raises(RuntimeError):
            with core.authority_scope("diagnostic"):
                raise RuntimeError("boom")
        assert core.current_authority().kind == "process"
    assert core.current_authority() == before


def test_establish_on_a_thread_with_authority_only_narrows():
    """Detects `establish_authority` resetting an existing ceiling: an
    in-process `main()` under a query ceiling must not obtain `process`."""
    with core.establish_authority("query"):
        with pytest.raises(core.QueryAuthorityViolation):
            with core.establish_authority("process"):
                pass  # pragma: no cover


def test_owner_wait_only_tightens():
    """Detects an inner scope that loosens the AD7 bound back to unbounded."""
    with core.establish_authority("process", owner_wait_s=5.0):
        with core.authority_scope("process", owner_wait_s=None) as a:
            assert a.owner_wait_s == 5.0
        with core.authority_scope("process", owner_wait_s=1.0) as a:
            assert a.owner_wait_s == 1.0
    assert core._CLI_OWNER_MAINTENANCE_WAIT_S == 5.0


def test_violation_is_base_exception():
    """Detects a violation an `except Exception` handler could turn into an
    empty successful result (AD4)."""
    assert issubclass(core.QueryAuthorityViolation, BaseException)
    assert not issubclass(core.QueryAuthorityViolation, Exception)
    with core.establish_authority("query"):
        with pytest.raises(core.QueryAuthorityViolation) as info:
            try:
                core.require_capability("WRITE", entry="sync_cache")
            except Exception:  # pragma: no cover - must not swallow
                pass
    assert info.value.entry == "sync_cache"
    assert info.value.capability == "WRITE"
    assert info.value.authority == "query"


def test_unknown_capability_is_a_programming_error():
    """Detects a typo in a guard that would otherwise never refuse."""
    with pytest.raises(ValueError):
        core.require_capability("WRTIE", entry="x")


# ── thread, fork and reload isolation (D1, AD16) ─────────────────────────


@pytest.mark.parametrize("copy_context", [False, True])
def test_new_thread_has_no_authority(copy_context):
    """Detects a worker thread that inherits its parent's `process` authority
    (even through a copied context), which would let an unestablished owner
    thread pass every guard."""
    seen = {}

    def target():
        seen["authority"] = core.current_authority()

    with core.establish_authority("process"):
        if copy_context:
            ctx = contextvars.copy_context()
            t = threading.Thread(target=lambda: ctx.run(target))
        else:
            t = threading.Thread(target=target)
        t.start()
        t.join()
    assert seen["authority"] is None


_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _run_fork_script(body: str) -> "subprocess.CompletedProcess":
    """Run `body` in a fresh interpreter whose only thread forks (N4, as the
    W9 keeper fork test does since F9): forking an xdist worker, which runs
    threads, raises Python's fork-with-threads DeprecationWarning and risks
    a deadlock in the child. `body` sees `core` and `fork_child(fn)`, which
    asserts that no other thread runs, forks, runs `fn` in the child (exit 0
    when it returns True, 7 otherwise) and returns the child's exit code."""
    prelude = (
        "import os, sys, threading, time\n"
        f"sys.path.insert(0, {str(_BIN)!r})\n"
        "import _cctally_core as core\n"
        "def fork_child(fn):\n"
        "    assert threading.active_count() == 1, 'a thread runs at the fork'\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        ok = False\n"
        "        try:\n"
        "            ok = bool(fn())\n"
        "        finally:\n"
        "            os._exit(0 if ok else 7)\n"
        "    _, status = os.waitpid(pid, 0)\n"
        "    return os.waitstatus_to_exitcode(status)\n")
    run = subprocess.run(
        [sys.executable, "-c", prelude + body],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-4000:]
    # No thread runs at the fork, so the fork-with-threads warning never
    # fires (and the test itself cannot deadlock the child).
    assert "DeprecationWarning" not in run.stderr, run.stderr[-4000:]
    return run


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork unavailable")
def test_forked_child_has_no_authority():
    """Detects a forked continuation (hook-tick forks without exec) that keeps
    its parent's authority, deadline and violation latch. The parent's
    values are asserted set at the fork, so the child's empty ones are the
    at-fork handler's work, not an empty parent."""
    run = _run_fork_script(
        "with core.operation_boundary('parent'):\n"
        "    with core.operation_deadline('command'):\n"
        "        with core.establish_authority('process'):\n"
        "            assert core.current_authority() is not None\n"
        "            assert core._STORE_DEADLINE.get() is not None\n"
        "            assert core._VIOLATION_LATCH.get() is not None\n"
        "            code = fork_child(lambda: (\n"
        "                core.current_authority() is None\n"
        "                and core._STORE_AUTHORITY.get() is None\n"
        "                and core._STORE_DEADLINE.get() is None\n"
        "                and core._VIOLATION_LATCH.get() is None))\n"
        "print('CHILD', code)\n")
    assert run.stdout.strip().splitlines()[-1] == "CHILD 0", run.stdout


@pytest.mark.parametrize("copy_context", [False, True])
def test_another_thread_starts_its_own_admission_budget(copy_context):
    """5.3 / AD29: an admission budget record belongs to the thread that
    created it. Another thread, even one running a context copied from
    inside the parent's admission, finds no budget and no enclosing
    admission, starts its own and debits it, and the parent's record is
    untouched. Detects a record shared across threads (a worker's
    admissions would spend a request's budget, or a worker would take the
    parent's admission as enclosing and debit nothing)."""
    seen = {}

    def target():
        seen["found"] = core.admission_budget()
        with core.operation_budget("interactive") as mine:
            with core.admission_scope("interactive") as deadline:
                seen["deadline"] = deadline
                time.sleep(0.05)
            seen["mine"] = mine
            seen["mine_spent"] = mine.total_s - mine.remaining_s

    with core.operation_budget("interactive") as parent:
        with core.admission_scope("interactive") as parent_deadline:
            time.sleep(0.02)
            if copy_context:
                ctx = contextvars.copy_context()
                t = threading.Thread(target=lambda: ctx.run(target))
            else:
                t = threading.Thread(target=target)
            t.start()
            t.join()
        spent = parent.total_s - parent.remaining_s
        assert core.admission_budget() is parent
    assert seen["found"] is None
    assert seen["mine"] is not parent
    assert seen["deadline"] != parent_deadline
    assert 0.05 <= seen["mine_spent"] < 0.2, seen
    assert spent < 0.05 + 0.2, spent   # only the parent's own admission


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork unavailable")
def test_forked_child_starts_with_no_admission_budget():
    """5.3 / AD29: a forked child starts with no admission budget and no
    enclosing admission, so its first session creates a fresh one. Detects
    a forked continuation that keeps its parent's half-spent budget (or
    believes itself inside the parent's admission and debits nothing). The
    parent's budget is asserted spent and its admission active at the fork,
    so the child's fresh state is the at-fork handler's work."""
    run = _run_fork_script(
        "def child():\n"
        "    ok = (core.admission_budget() is None\n"
        "          and core._ADMISSION_ACTIVE.get() is None)\n"
        "    with core.operation_budget('command') as mine:\n"
        "        ok = ok and mine is not parent\n"
        "        ok = ok and mine.remaining_s == mine.total_s == 5.0\n"
        "        with core.admission_scope('command'):\n"
        "            time.sleep(0.01)\n"
        "        ok = ok and mine.remaining_s < 5.0\n"
        "    return ok\n"
        "with core.operation_budget('command') as parent:\n"
        "    with core.admission_scope('command'):\n"
        "        time.sleep(0.02)\n"
        "        assert core._ADMISSION_ACTIVE.get() is not None\n"
        "        code = fork_child(child)\n"
        "    assert parent.remaining_s < parent.total_s\n"
        "print('CHILD', code)\n")
    assert run.stdout.strip().splitlines()[-1] == "CHILD 0", run.stdout


def test_caller_deadline_inside_an_active_admission_tightens_it():
    """5.3 / AD29 (K2): a tighter caller deadline wins, also when the caller
    enters `operation_deadline` inside an admission already running. Every
    wait reads `admission_deadline()`, so returning the admission's own
    deadline there would let a lock poll, the AD19 probe or the AD23
    journal wait run past the caller's bound (here 5 s against 0.5 s). A
    looser caller deadline never extends the admission's."""
    with core.operation_budget("command"):
        with core.admission_scope("command") as admitted:
            assert core.admission_deadline() == admitted
            with core.operation_deadline("interactive") as caller:
                assert caller < admitted
                assert core.admission_deadline() == caller
                assert core.remaining_deadline() <= 0.5
            assert core.admission_deadline() == admitted
    with core.operation_budget("interactive"):
        with core.admission_scope("interactive") as admitted:
            with core.operation_deadline("command") as caller:
                assert caller > admitted
                assert core.admission_deadline() == admitted


def test_authority_survives_sibling_reload():
    """Detects authority state stored in a sibling module, which conftest's
    `load_script()` reload would silently replace mid-scope."""
    import _cctally_store_access as access
    with core.establish_authority("query"):
        importlib.reload(access)
        assert core.current_authority().kind == "query"
        with pytest.raises(core.QueryAuthorityViolation):
            core.require_capability("STARTUP", entry="open_db")


# ── unspecified authority (final enforcement, landing part 3) ─────────────


def _on_a_fresh_thread(fn):
    """Run `fn` on a new thread, which has no authority (D1), and return what
    it returned or raised."""
    out = {}

    def target():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — handed back
            out["raised"] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join()
    return out


def test_unspecified_authority_raises_after_enforcement():
    """Detects an owner path that runs with no authority established once
    landing part 3 makes unspecified authority raise (spec 5.1, AD5): a
    thread or process entry that forgot `establish_authority` would otherwise
    keep creating, migrating or launching unobserved. A bare scope cannot
    mint `process` either; only `establish_authority` can."""
    assert core.UNSPECIFIED_AUTHORITY_RAISES is True

    def unestablished():
        assert core.current_authority() is None
        assert core.has_capability("WRITE") is False
        core.require_capability("MAINTENANCE", entry="open_db")

    out = _on_a_fresh_thread(unestablished)
    assert isinstance(out.get("raised"), core.QueryAuthorityViolation), out

    def bare_scope():
        with core.authority_scope("process"):
            pass  # pragma: no cover

    out = _on_a_fresh_thread(bare_scope)
    assert isinstance(out.get("raised"), core.QueryAuthorityViolation), out

    def established():
        with core.establish_authority("process"):
            core.require_capability("MAINTENANCE", entry="open_db")
            return core.current_authority().kind

    assert _on_a_fresh_thread(established) == {"value": "process"}


# ── violation latch and publication ──────────────────────────────────────


def test_latch_records_refusal_and_denial():
    """Detects a refusal or an authorizer denial that is not recorded before
    it raises, so a later publication check could not see it."""
    with core.operation_boundary("op") as latch:
        assert latch.violated is False
        with core.establish_authority("query"):
            with pytest.raises(core.QueryAuthorityViolation):
                core.require_capability("LAUNCH", entry="defer_stats_epoch_rebuild")
        core.record_authorizer_denial("query-session", "INSERT main.t")
        assert latch.violated is True
        entries = [e.entry for e in latch.events]
        assert entries == ["defer_stats_epoch_rebuild", "query-session"]
        assert all(not e.permitted for e in latch.events)


def test_swallowed_refusal_blocks_publication():
    """Detects the `_cctally_dashboard_share.py:410` shape: a refusal raised
    under a facade and caught by a broad handler must still refuse
    publication."""
    def facade():
        core.require_capability("WRITE", entry="sync_cache")

    with core.operation_boundary("share"):
        with core.establish_authority("query"):
            try:
                facade()
            except BaseException:
                result = []  # the swallowed "empty success"
        assert result == []
        with pytest.raises(core.QueryAuthorityViolation):
            core.check_publishable()
    # A fresh boundary is clean.
    with core.operation_boundary("next"):
        core.check_publishable()


def test_nested_boundary_forwards_to_enclosing():
    """Detects a violation recorded only in a nested unit that the enclosing
    publication (an HTTP response around a snapshot build) never sees."""
    with core.operation_boundary("request") as outer:
        with core.operation_boundary("unit"):
            core.record_authorizer_denial("s", "DELETE main.t")
        assert outer.violated


# ── observers (D2) ───────────────────────────────────────────────────────


def test_observer_sees_every_entry(monkeypatch):
    """Detects a guard that skips its observer when it permits, which would
    hide an effect-free owner entry from the Task C recorder."""
    events = []
    monkeypatch.setattr(core, "CAPABILITY_OBSERVERS", [events.append])
    core.require_capability("MAINTENANCE", entry="_prune_orphaned_cache_entries")
    with core.establish_authority("process"):
        core.require_capability("WRITE", entry="sync_cache")
    with core.establish_authority("query"):
        with pytest.raises(core.QueryAuthorityViolation):
            core.require_capability("STARTUP", entry="open_db")
    # The test thread runs under the suite's `process` authority (D5).
    assert [(e.entry, e.authority, e.permitted) for e in events] == [
        ("_prune_orphaned_cache_entries", "process", True),
        ("sync_cache", "process", True),
        ("open_db", "query", False),
    ]


def test_failing_observer_does_not_change_the_decision(monkeypatch):
    """Detects an observer exception that aborts an owner operation."""
    def broken(event):
        raise RuntimeError("observer bug")
    monkeypatch.setattr(core, "CAPABILITY_OBSERVERS", [broken])
    with core.establish_authority("process"):
        core.require_capability("WRITE", entry="sync_cache")


# ── deadline (5.3) ───────────────────────────────────────────────────────


_SCOPES = {
    "authority": (lambda: core.authority_scope("query", policy="interactive"),
                  core._STORE_AUTHORITY),
    "latch": (lambda: core.operation_boundary("f5"), core._VIOLATION_LATCH),
    "deadline": (lambda: core.operation_deadline("interactive"),
                 core._STORE_DEADLINE),
    "request_ingest": (lambda: core.request_ingest_scope("sync_cache"),
                       core._REQUEST_INGEST),
    "admission_budget": (lambda: core.operation_budget("interactive"),
                         core._ADMISSION_BUDGET),
}


@pytest.mark.parametrize("name", sorted(_SCOPES))
def test_scope_exit_in_another_context_leaves_this_context_alone(name):
    """F5: a scope whose token was made in another context (`reset` raises
    `ValueError`) must not write None into the context it exits in: that
    value is not the scope's to clear. Detects the fallback that cleared the
    wrong context's authority, latch, deadline or request scope (a thread
    holding `process` left with no authority, or an operation losing its
    latch). The event is recorded instead."""
    factory, var = _SCOPES[name]
    if name == "authority":
        sentinel = var.get()      # the test thread's `process` (D5)
        assert sentinel is not None and sentinel.kind == "process"
    elif name == "deadline":
        sentinel = time.monotonic() + 3600.0
    else:
        sentinel = object()
    token = var.set(sentinel)
    try:
        scope = factory()
        contextvars.copy_context().run(scope.__enter__)
        before = len(getattr(core, "CROSS_CONTEXT_RESETS", ()))
        scope.__exit__(None, None, None)
        assert var.get() is sentinel
        assert list(core.CROSS_CONTEXT_RESETS)[before:] == [var.name]
    finally:
        var.reset(token)


def test_outermost_deadline_wins_and_nested_never_extends():
    """Detects a nested opener that restarts the operation's admission
    allowance, which would let one route wait 500 ms per store."""
    assert core.remaining_deadline() is None
    with core.operation_deadline("interactive") as d1:
        r = core.remaining_deadline()
        assert 0 < r <= 0.5
        with core.operation_deadline("command") as d2:
            assert d2 == d1
    assert core.remaining_deadline() is None
    with core.operation_deadline("command") as d3:
        with core.operation_deadline("interactive") as d4:
            assert d4 < d3  # a tighter nested deadline wins
        assert core._STORE_DEADLINE.get() == d3


# ── typed states (5.5) ───────────────────────────────────────────────────


def test_store_unavailable_shape():
    """Detects a typed refusal that existing `except sqlite3.DatabaseError`
    handlers (and the CLI exit-1 path) no longer catch."""
    assert core.STORE_REASONS == (
        "absent", "maintenance", "schema_behind", "schema_ahead",
        "recovery_pending", "corrupt", "legacy_bridge_pending", "busy",
        "unavailable")
    exc = core.StoreUnavailable("x", store="stats", reason="absent")
    assert isinstance(exc, core.sqlite3.DatabaseError)
    assert (exc.store, exc.reason, str(exc)) == ("stats", "absent", "x")
    bare = core.StoreUnavailable("y")
    assert (bare.store, bare.reason) == ("unknown", "unavailable")
    r = core.Readiness("cache", True, None, frozenset({"migration_error_sentinel"}))
    assert r.readable and "migration_error_sentinel" in r.obligations


def test_owner_resolved_reasons_are_the_complement_of_the_facade_raised():
    """#787 / #861 spec 5.11: every typed reason is either one a facade raised
    before the query layer (the direct-JSONL fallback keeps serving) or one
    the full opener resolved inline (the owner's: publishers keep their last
    coherent frame, detail routes notify it). Detects a reason added to the
    enumeration without a side, or one on both, which would make the
    dashboard routes and the TUI/dashboard publishers disagree on whether a
    refusal is the owner's."""
    cache = importlib.import_module("_cctally_cache")
    facade = cache._QUERY_READER_FALLBACK_REASONS
    assert core.OWNER_RESOLVED_REASONS == (
        frozenset(core.STORE_REASONS) - facade)
    assert facade <= frozenset(core.STORE_REASONS)


def test_owner_attention_callbacks(monkeypatch):
    """Detects a notification that raises into a query (a callback error must
    never fail the read) or that cannot be unregistered."""
    seen = []
    unregister = core.register_owner_attention(
        lambda store, r: seen.append((store, r.reason)))
    unregister_bad = core.register_owner_attention(
        lambda store, r: 1 / 0)
    try:
        core.notify_owner("conversations",
                          core.Readiness("conversations", False, "absent",
                                         frozenset()))
    finally:
        unregister()
        unregister_bad()
    core.notify_owner("cache", core.Readiness("cache", False, "absent",
                                              frozenset()))
    assert seen == [("conversations", "absent")]


# ── owner entry points (spec 5.1 "each entry point checks its own capability")


import csv  # noqa: E402
import inspect  # noqa: E402
import sqlite3  # noqa: E402

from conftest import load_script, redirect_paths  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parents[1]
_OWNER_ENTRIES = (_REPO / "docs/superpowers/specs/861-task-a-evidence/census/"
                  "owner-entries.tsv")


def _named_entries(path):
    with path.open() as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    return [(r["module"], r["function"]) for r in rows
            if r["kind"].startswith("named entry")]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    app = pathlib.Path(sys.modules["_cctally_core"].APP_DIR)
    app.mkdir(parents=True, exist_ok=True)
    os.chmod(app, 0o700)
    return ns, tmp_path


def _listing(root: pathlib.Path):
    out = []
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns,
                    st.st_mode))
    return out


def _seed_state(path: pathlib.Path, state: str, version: int) -> None:
    if state == "absent":
        return
    if state == "corrupt":
        path.write_bytes(b"this is not a database" * 64)
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (x)")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _openers():
    core_ = sys.modules["_cctally_core"]
    db = sys.modules["_cctally_db"]
    cache = sys.modules["_cctally_cache"]
    return {
        "stats": (core_.open_db, core_.DB_PATH, core_.STATS_INDEX_EPOCH - 1),
        "cache": (cache.open_cache_db, core_.CACHE_DB_PATH,
                  len(db._CACHE_MIGRATIONS) - 1),
        "conversations": (cache.open_conversations_db,
                          core_.CONVERSATIONS_DB_PATH,
                          len(db._CONVERSATIONS_MIGRATIONS) - 1),
    }


@pytest.mark.parametrize("store", ["stats", "cache", "conversations"])
@pytest.mark.parametrize("state", ["absent", "behind", "corrupt"])
@pytest.mark.parametrize("kind", ["writer", "query"])
def test_full_opener_branch_requires_capability(isolated, store, state, kind):
    """Detects a full opener that creates, migrates, recovers or launches
    under an authority lacking that branch's capability, or that refuses only
    after its first file effect (mkdir, chmod, lock or store creation)."""
    _ns, tmp_path = isolated
    opener, path, behind = _openers()[store]
    import sys as _sys
    core_ = _sys.modules["_cctally_core"]
    path = pathlib.Path(path)
    _seed_state(path, state, behind)
    before = _listing(tmp_path)
    with core_.establish_authority(kind):
        with pytest.raises(core_.QueryAuthorityViolation):
            opener()
    assert _listing(tmp_path) == before


def _dummy_call(fn):
    sig = inspect.signature(fn)
    args, kwargs = [], {}
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is not p.empty:
            continue
        if p.kind == p.KEYWORD_ONLY:
            kwargs[p.name] = None
        else:
            args.append(None)
    if fn.__name__ == "ensure_quota_index":
        kwargs["budgeted"] = False
    result = fn(*args, **kwargs)
    if hasattr(result, "__enter__") and hasattr(result, "__exit__"):
        # A context-manager entry (`WriterService.request_writer`) runs its
        # guard only when it is entered.
        with result:
            pass
    return result


def _resolve_named_entry(module: str, function: str):
    """The census entry's definition, resolved at call time through the
    live module: a module-level function, or the one class defined in that
    module whose body defines ``function`` (the census names a method by its
    bare name, e.g. ``WriterService.request_writer``). None when it is
    absent or ambiguous, which the caller reports as unguarded."""
    mod = importlib.import_module(module)
    fn = getattr(mod, function, None)
    if fn is not None:
        return fn
    owners = [cls for cls in vars(mod).values()
              if isinstance(cls, type) and cls.__module__ == mod.__name__
              and function in vars(cls)]
    if len(owners) != 1:
        return None
    return getattr(owners[0], function)


@pytest.mark.parametrize("kind", ["query", "diagnostic"])
def test_named_entries_refused_under_query_and_diagnostic(isolated, kind):
    """Detects an owner entry point (owner-entries.tsv) without a guard in
    its own frame: under a query or diagnostic authority it must refuse
    before any side effect, here on an empty data directory where the entry
    is otherwise effect-free. An entry is resolved through the live module
    when it is called (a method by its class), and one that cannot be
    resolved is reported, never skipped. The census is private evidence, so
    the public mirror skips this test."""
    if not _OWNER_ENTRIES.exists():
        pytest.skip("owner-entries.tsv is private census evidence")
    entries = _named_entries(_OWNER_ENTRIES)
    assert entries
    _ns, tmp_path = isolated
    core_ = sys.modules["_cctally_core"]
    unguarded = []
    for module, function in entries:
        fn = _resolve_named_entry(module, function)
        if fn is None:
            unguarded.append(f"{module}.{function} (unresolved)")
            continue
        events = []
        before = _listing(tmp_path)
        with core_.operation_boundary("t") as latch:
            with core_.establish_authority(kind):
                core_.CAPABILITY_OBSERVERS.append(events.append)
                try:
                    try:
                        _dummy_call(fn)
                    except core_.QueryAuthorityViolation:
                        refused = True
                    else:
                        refused = False
                finally:
                    core_.CAPABILITY_OBSERVERS.remove(events.append)
        if not (refused and latch.violated
                and any(e.entry == function and not e.permitted
                        for e in events)
                and _listing(tmp_path) == before):
            unguarded.append(f"{module}.{function}")
    assert unguarded == []


def test_owner_entry_refused_with_writable_conn(isolated):
    """Detects an owner entry that trusts a writable connection handed to it:
    the check is at the entry, not at the connection."""
    _ns, tmp_path = isolated
    core_ = sys.modules["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    conn = sqlite3.connect(tmp_path / "writable.db")
    try:
        with core_.establish_authority("query"):
            with pytest.raises(core_.QueryAuthorityViolation):
                cache.sync_codex_cache(conn)
    finally:
        conn.close()


def test_successful_orchestration_under_process(isolated, monkeypatch):
    """Detects a guard that breaks today's owner orchestration: under
    `process` a future-epoch stats open still defers and launches exactly one
    rebuild worker, and a stale interrupted-rebuild artifact is recovered."""
    ns, tmp_path = isolated
    core_ = sys.modules["_cctally_core"]
    db = sys.modules["_cctally_db"]
    import _cctally_update
    spawned = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached_with_pid",
        lambda command, **_kw: spawned.append(command) or os.getpid())
    with core_.establish_authority("process"):
        core_.open_db().close()
    conn = sqlite3.connect(core_.DB_PATH)
    conn.execute(f"PRAGMA user_version = {core_.STATS_INDEX_EPOCH + 1}")
    conn.commit()
    conn.close()
    with core_.establish_authority("process"):
        with pytest.raises(db.StatsEpochRebuildDeferred):
            core_.open_db()
    assert spawned == ["_stats-epoch-rebuild"]

    # Interrupted rebuild: a stale scratch family with no quarantine incident
    # is reclaimed by the startup open, which then succeeds.
    conn = sqlite3.connect(core_.DB_PATH)
    conn.execute(f"PRAGMA user_version = {core_.STATS_INDEX_EPOCH}")
    conn.commit()
    conn.close()
    monkeypatch.setattr(core_, "STATS_INDEX_EPOCH", core_.STATS_INDEX_EPOCH)
    path = pathlib.Path(core_.DB_PATH)
    artifact = path.with_name(path.name + ".rebuilding-20260101T000000_000000")
    artifact.write_bytes(b"")
    store = sys.modules["_cctally_store"]
    monkeypatch.setattr(store, "_pending_stats_publication_never_replaced",
                        lambda _p: True)
    with core_.establish_authority("process"):
        core_.open_db().close()
    assert not artifact.exists()


def test_installer_refused_under_query(isolated):
    """Detects the content-revision installer running from a read: called
    inside a query scope on a current store it refuses before its reads."""
    ns, _tmp = isolated
    core_ = sys.modules["_cctally_core"]
    with core_.establish_authority("process"):
        core_.open_db().close()
    conn = sqlite3.connect(core_.DB_PATH)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        with core_.establish_authority("query"):
            with pytest.raises(core_.QueryAuthorityViolation):
                core_._ensure_stats_content_revision(conn, live=True)
    finally:
        conn.close()
    assert statements == []


# ── request entries (spec 5.9) ───────────────────────────────────────────

_REQUEST_ENTRIES = ["sync_cache_request", "sync_codex_cache_request",
                    "sync_claude_conversations_request",
                    "sync_codex_conversations_request"]


@pytest.mark.parametrize("entry", _REQUEST_ENTRIES)
def test_request_entries_need_ingest_current(isolated, entry):
    """Detects a request entry reachable from a pure query scope."""
    core_ = sys.modules["_cctally_core"]
    fn = getattr(sys.modules["_cctally_cache"], entry)
    with core_.establish_authority("query"):
        with pytest.raises(core_.QueryAuthorityViolation):
            fn(None, ["/x.jsonl"])


def _owner_conns():
    core_ = sys.modules["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    with core_.establish_authority("process"):
        return cache.open_cache_db(), cache.open_conversations_db()


@pytest.mark.parametrize("entry,store", [
    ("sync_cache_request", "cache"),
    ("sync_claude_conversations_request", "conversations"),
    ("sync_codex_conversations_request", "conversations"),
])
def test_request_entries_reach_only_ingest_current(isolated, entry, store,
                                                   tmp_path):
    """Detects a request entry whose transitive reach needs WRITE, STARTUP,
    MAINTENANCE or LAUNCH (spec 5.9): under the request_writer ceiling every
    guard event is permitted and none asks for an owner capability."""
    core_ = sys.modules["_cctally_core"]
    cache_conn, conv_conn = _owner_conns()
    conn = cache_conn if store == "cache" else conv_conn
    events = []
    core_.CAPABILITY_OBSERVERS.append(events.append)
    try:
        with core_.establish_authority("request_writer"):
            result = getattr(sys.modules["_cctally_cache"], entry)(
                conn, [str(tmp_path / "absent-session.jsonl")])
    finally:
        core_.CAPABILITY_OBSERVERS.remove(events.append)
        cache_conn.close()
        conv_conn.close()
    assert isinstance(result.pending, frozenset)
    assert events and all(e.permitted for e in events)
    assert {e.capability for e in events} <= {"INGEST_CURRENT", "READ"}


def test_codex_request_entry_dispositions(isolated, monkeypatch):
    """Detects the X8 reach from a request: the Codex request entry must use
    the budgeted walk with quota_reconcile='defer', never request a drain
    worker or the quota-index rebuild, and report both as pending."""
    core_ = sys.modules["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    import _cctally_journal
    seen = {}

    def fake_locked(conn, **kwargs):
        seen.update(kwargs)
        stats = cache.CodexIngestStats()
        stats.deferred_reason = "quota_index_pending"
        stats.drain_requested = (cache.CODEX_REPLAY_DRAIN_COMMAND,)
        return stats

    monkeypatch.setattr(cache, "_sync_codex_cache_locked", fake_locked)
    monkeypatch.setattr(_cctally_journal, "request_quota_index_rebuild",
                        lambda: pytest.fail("request entry launched"))
    monkeypatch.setattr(cache, "_ensure_quota_index_before_producer_locks",
                        lambda: pytest.fail("unbudgeted quota index"))
    notified = []
    unregister = core_.register_owner_attention(
        lambda store, r: notified.append((store, r.obligations)))
    cache_conn, conv_conn = _owner_conns()
    try:
        with core_.establish_authority("request_writer"):
            result = cache.sync_codex_cache_request(cache_conn, ["/a.jsonl"])
        assert seen["quota_reconcile"] == "defer"
        assert seen["budget_seconds"] is not None
        assert result.ingested is False
        assert "quota_index_pending" in result.pending
        assert f"drain:{cache.CODEX_REPLAY_DRAIN_COMMAND}" in result.pending
        assert notified and "codex_maintenance_debt" in notified[0][1]
        # A pending from-zero replay is refused up front, before any walk.
        seen.clear()
        cache_conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, '1')",
            (cache.CODEX_REPLAY_FROM_ZERO_KEY,))
        cache_conn.commit()
        with core_.establish_authority("request_writer"):
            result = cache.sync_codex_cache_request(cache_conn, ["/a.jsonl"])
        assert result.pending == {"codex_replay_pending"}
        assert seen == {}
    finally:
        unregister()
        cache_conn.close()
        conv_conn.close()


def test_request_writer_requires_schema_current(isolated):
    """Detects a request writer admitted on a store it would have to create
    or migrate first (spec 5.10)."""
    core_ = sys.modules["_cctally_core"]
    import _cctally_store_access as access
    with core_.establish_authority("request_writer"):
        with pytest.raises(core_.StoreUnavailable) as info:
            with access.WriterService.request_writer("conversations"):
                pass  # pragma: no cover
    assert info.value.reason == "absent"
    cache_conn, conv_conn = _owner_conns()
    cache_conn.close()
    conv_conn.close()
    with core_.establish_authority("request_writer"):
        with access.WriterService.request_writer("conversations") as conn:
            assert conn.execute(
                "SELECT 1 FROM cache_db.sqlite_master LIMIT 1").fetchone()


# ── RC-3: configuration reads outside an owner (spec rev 24-25) ───────────


def _config_env(monkeypatch, tmp_path):
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["_cctally_config"], pathlib.Path(core.APP_DIR)


def _tree(root: pathlib.Path):
    if not root.exists():
        return None
    return sorted((str(p.relative_to(root)), p.stat().st_mode)
                  for p in [root, *root.rglob("*")])


@pytest.mark.parametrize("absent", ["directory", "config"])
@pytest.mark.parametrize("kind", ["query", "diagnostic"])
def test_config_reads_outside_an_owner_create_nothing(
        monkeypatch, tmp_path, absent, kind):
    """A request thread's config read (query, diagnostic) must not reach
    `ensure_dirs()` or the first-run branch: no mkdir/chmod of the data
    directory or `logs`, no writer lock file, no saved defaults (the
    recorder's RC-3, R_A and R_AUDIT_FS). It reads in-memory defaults."""
    import shutil
    config, app = _config_env(monkeypatch, tmp_path)
    if app.exists():
        shutil.rmtree(app)
    if absent == "config":
        app.mkdir(parents=True)
        os.chmod(app, 0o755)    # an owner would chmod it; a reader never
    before = _tree(app)
    with core.authority_scope(kind):
        loaded = config.load_config()
        unlocked = config._load_config_unlocked()
    for data in (loaded, unlocked):
        assert data["collector"]["week_start"] == config.DEFAULT_WEEK_START
    assert _tree(app) == before


def test_config_explicit_missing_path_still_errors_outside_an_owner(
        monkeypatch, tmp_path):
    config, _app = _config_env(monkeypatch, tmp_path)
    with core.authority_scope("query"):
        with pytest.raises(SystemExit) as info:
            config.load_config(tmp_path / "missing.json")
    assert info.value.code == 2


def test_config_first_run_still_creates_under_an_owner(monkeypatch, tmp_path):
    """The owner's first-run branch is unchanged: the data directory and a
    persisted config with its collector token."""
    config, app = _config_env(monkeypatch, tmp_path)
    data = config.load_config()
    assert (app / "config.json").exists()
    assert config.load_config()["collector"]["token"] == \
        data["collector"]["token"]
    assert (app.stat().st_mode & 0o777) == 0o700
