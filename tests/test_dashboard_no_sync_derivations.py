"""#769 S6 / #802 — `--no-sync` is bounded to schema advancement.

`_dashboard_startup_schema_migration` opens both stores once at startup in
every mode, because under `--no-sync` no other process opens them
write-capable and the read-only reader refuses a store behind head rather than
migrating from a request thread. That open runs `_run_pending_migrations` and
then, as two SEPARATE calls the dispatcher does not own,
`_import_legacy_conversation_rows` and `_ensure_codex_conversation_contract`.
The second consumes `conversation_rebuild_codex_pending` and performs the
rebuild — the measured 135-second startup against comparable launches of 15
and 30 seconds.

`--no-sync` therefore suppresses those two post-dispatch derivations and keeps
the dispatcher. The policy is PROCESS-level rather than call-site-level: read
routes fall back from the read-only opener to the full `open_conversations_db`
on a missing store, a lock or a pending legacy bridge, and live-tail always
uses the full opener, so a startup-only flag would freeze nothing — an ordinary
browse would consume the marker instead.

The accepted consequence is stated rather than hidden: a `--no-sync` dashboard
over a store that owes the rebuild serves `normalization_pending` for Codex
conversation reads until a mode allowed to do work runs.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import threading
import time
import urllib.parse as _u
from http.client import HTTPConnection

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths                # noqa: E402
from tests._support_http import (                               # noqa: E402
    PRESENCE_BACKSTOP_SECONDS, serve_dashboard, stop,
)

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
MARKER = "conversation_rebuild_codex_pending"

#: The startup delta §9 permits between an armed store and a no-marker control.
#: Generous, because the derivation-row count carries the claim: a timing-only
#: assertion would pass on a machine fast enough to do the work anyway.
_STARTUP_DELTA_S = 2.0


# ── staging ────────────────────────────────────────────────────────────────

def _stage(tmp_path, monkeypatch):
    """A current, populated pair of stores with real Codex conversations."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
        # Every relation the observables below are read from is asserted
        # populated here. An empty one makes a byte-equality comparison pass
        # without observing anything, which is the vacuity W5 closed elsewhere.
        for table in ("codex_conversation_events", "codex_conversation_messages",
                      "codex_conversation_rollups"):
            assert conv.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
    finally:
        conv.close()
    return ns


def _arm(ns):
    conv = ns["open_conversations_db"]()
    try:
        conv.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            (MARKER, "armed-1"),
        )
        conv.commit()
    finally:
        conv.close()


def _marker_value(ns):
    conv = ns["open_conversations_db"](attach_cache=False)
    try:
        row = conv.execute(
            "SELECT value FROM cache_meta WHERE key=?", (MARKER,)).fetchone()
    finally:
        conv.close()
    return None if row is None else str(row[0])


def _identity(path):
    stat = pathlib.Path(path).stat()
    return (stat.st_dev, stat.st_ino)


def _fingerprint(ns):
    """Every observable §9 names, read WITHOUT running a derivation."""
    import _cctally_core

    conv = ns["open_conversations_db"](attach_cache=False)
    cache = ns["open_cache_db"]()
    try:
        state = {
            "conversations_cache_meta": dict(
                conv.execute("SELECT key, value FROM cache_meta")),
            "codex_events": conv.execute(
                "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0],
            "codex_messages": conv.execute(
                "SELECT COUNT(*) FROM codex_conversation_messages"
            ).fetchone()[0],
            "max_codex_rollup_revision": conv.execute(
                "SELECT COALESCE(MAX(render_revision), -1) "
                "FROM codex_conversation_rollups").fetchone()[0],
            "codex_source_files": tuple(conv.execute(
                "SELECT source_root_key, path, size_bytes, last_byte_offset "
                "FROM codex_conversation_source_files "
                "ORDER BY source_root_key, path")),
            "cache_meta": dict(
                cache.execute("SELECT key, value FROM cache_meta")),
            "codex_session_files": tuple(cache.execute(
                "SELECT source_root_key, path, size_bytes, last_byte_offset "
                "FROM codex_session_files ORDER BY source_root_key, path")),
            "codex_entries": cache.execute(
                "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0],
        }
    finally:
        conv.close()
        cache.close()
    state["identities"] = (
        _identity(_cctally_core.CONVERSATIONS_DB_PATH),
        _identity(_cctally_core.CACHE_DB_PATH),
    )
    return state


def _forbid_every_sync(monkeypatch, ns):
    """No `sync_*` entry point may be reached from a `--no-sync` startup."""
    cache_module = ns["_cctally_cache"]

    def _forbidden(name):
        def _call(*_args, **_kwargs):
            raise AssertionError(f"a --no-sync startup called {name}")
        return _call

    for name in (
        "sync_cache", "sync_codex_cache",
        "sync_conversations", "sync_codex_conversations",
    ):
        if hasattr(cache_module, name):
            monkeypatch.setattr(cache_module, name, _forbidden(name))


#: Every module instance a test set the policy on. `load_script` can install a
#: fresh `_cctally_cache`, and `bin/cctally` binds its own sibling, so a bare
#: `import _cctally_cache` in a test body can name a DIFFERENT object from the
#: one the opener actually consults — which is why every access below goes
#: through `ns["_cctally_cache"]`.
_POLICY_TOUCHED: list = []


def _suppress(ns, value=True):
    """Set the process-level policy on the module the opener actually uses."""
    cache_module = ns["_cctally_cache"]
    cache_module.set_conversation_derivations_suppressed(value)
    _POLICY_TOUCHED.append(cache_module)
    return cache_module


@pytest.fixture(autouse=True)
def _restore_process_policy():
    """The policy is process-level, so a leaked True would poison the suite."""
    yield
    for module in _POLICY_TOUCHED:
        module.set_conversation_derivations_suppressed(False)
    _POLICY_TOUCHED.clear()


# ── the route harness ──────────────────────────────────────────────────────

def _serve(ns):
    """Start the real dashboard handler in `--no-sync` mode.

    Spec §9 and plan Task 18 both state the claim as a REQUEST reaching the
    opener, not as the opener honouring the policy when called directly. Those
    are different claims: calling the opener a route would eventually reach
    proves the opener, and leaves the route's own path to it unproven, so a
    later change passing `run_derivations=True` explicitly from the route would
    not fail anything. Every leg below therefore drives an HTTP route.

    Wired like `tests/test_dashboard_source_routes.py::_boot` and
    `tests/test_codex_conversation_api.py::_wire_handler`: class attributes
    first, because the handler class is shared, then the shared accept-thread
    primitive.
    """
    handler = ns["DashboardHTTPHandler"]
    handler.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    handler.hub = ns["SSEHub"]()
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda: None)
    handler.static_dir = ns["STATIC_DIR"]
    handler.cctally_host = "127.0.0.1"
    handler.cctally_expose_transcripts = False
    handler.no_sync = True
    return serve_dashboard(ns)


def _get_json(port, path):
    """One GET against the loopback dashboard, returning `(status, body)`."""
    conn = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        status, raw = response.status, response.read()
    finally:
        conn.close()
    return status, (json.loads(raw) if raw else None)


def _entity_path(key, suffix=""):
    return f"/api/conversation/{_u.quote(str(key), safe='')}{suffix}"


def _a_codex_conversation_key(ns):
    conv = ns["open_conversations_db"](attach_cache=False)
    try:
        row = conv.execute(
            "SELECT conversation_key FROM codex_conversation_events "
            "WHERE conversation_key IS NOT NULL LIMIT 1").fetchone()
    finally:
        conv.close()
    assert row is not None, "the staged corpus minted no Codex conversation key"
    return str(row[0])


#: One legacy bridge table, and the columns `_import_legacy_conversation_rows`
#: would copy for it. Small, and unrelated to the Codex corpus the other cases
#: stage, so owing the bridge does not perturb them.
_BRIDGE_TABLE = "conversation_ai_titles"
_BRIDGE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_BRIDGE_TABLE} ("
    "session_id TEXT NOT NULL PRIMARY KEY, ai_title TEXT NOT NULL, "
    "source_path TEXT, byte_offset INTEGER NOT NULL)"
)


def _owe_the_legacy_bridge(ns):
    """Reproduce an interrupted migration 028: rows in `cache.db`, none here.

    That is `conversation_legacy_bridge_pending`'s own condition, and it is
    durable state rather than a one-off startup case, which is why the read
    route falls back to the full opener on it.
    """
    cache = ns["open_cache_db"]()
    try:
        cache.execute(_BRIDGE_DDL)
        cache.execute(
            f"INSERT OR REPLACE INTO {_BRIDGE_TABLE}"
            "(session_id, ai_title, source_path, byte_offset) "
            "VALUES('s-legacy', 'A pre-028 title', '/p/s-legacy.jsonl', 0)")
        cache.commit()
    finally:
        cache.close()
    conv = ns["open_conversations_db"](attach_cache=False)
    try:
        conv.execute(f"DELETE FROM {_BRIDGE_TABLE}")
        conv.commit()
        assert conv.execute(
            f"SELECT COUNT(*) FROM {_BRIDGE_TABLE}").fetchone()[0] == 0
    finally:
        conv.close()


# ── the cases ──────────────────────────────────────────────────────────────

def test_a_no_sync_startup_leaves_every_named_observable_untouched(
    tmp_path, monkeypatch,
):
    """The five observables of spec §9, on an already-at-head armed store."""
    ns = _stage(tmp_path, monkeypatch)
    _arm(ns)
    before = _fingerprint(ns)
    assert before["conversations_cache_meta"].get(MARKER) == "armed-1"

    dashboard = sys.modules["_cctally_dashboard"]
    _forbid_every_sync(monkeypatch, ns)
    _suppress(ns)
    dashboard._dashboard_startup_schema_migration(run_derivations=False)

    after = _fingerprint(ns)
    # 1. The marker's exact value survives — not merely its presence, so a
    #    delete-and-rewrite could not pass.
    assert after["conversations_cache_meta"].get(MARKER) == "armed-1"
    # 2/3. The projection generation and both render revisions are byte-equal.
    #
    #      Each is compared only after its population is asserted. Two of these
    #      five observables could otherwise pass vacuously: `dict.get` answers
    #      `None` on both sides when the key is absent, and the rollup revision
    #      is read through `COALESCE(MAX(...), -1)`, so an empty table compares
    #      `-1 == -1`. An absent key and an empty table are exactly what a
    #      derivation that wiped the store would leave, so the comparison has to
    #      be over something present. This is the guard W5 applied elsewhere in
    #      this session.
    for key in ("codex_find_projection_generation",):
        assert key in before["conversations_cache_meta"], (
            f"{key} is absent, so comparing it proves nothing")
        assert after["conversations_cache_meta"].get(key) == (
            before["conversations_cache_meta"].get(key)), key
    assert "conversation_render_revision" in before[
        "conversations_cache_meta"], (
        "conversation_render_revision is absent, so comparing it proves nothing")
    assert after["conversations_cache_meta"].get(
        "conversation_render_revision") == before[
        "conversations_cache_meta"].get("conversation_render_revision")
    assert before["max_codex_rollup_revision"] > -1, (
        "no Codex rollup was staged, so the revision comparison is vacuous")
    assert after["max_codex_rollup_revision"] == (
        before["max_codex_rollup_revision"])
    # 4. No sync entry point ran (asserted by the patches above), and the
    #    physical-mutation sequence, cursors, source-file rows, counts and both
    #    database identities are unchanged.
    assert after["cache_meta"].get("codex_physical_mutation_seq") == (
        before["cache_meta"].get("codex_physical_mutation_seq"))
    assert after["codex_session_files"] == before["codex_session_files"]
    assert after["codex_source_files"] == before["codex_source_files"]
    assert after["codex_events"] == before["codex_events"]
    assert after["codex_messages"] == before["codex_messages"]
    assert after["codex_entries"] == before["codex_entries"]
    assert after["identities"] == before["identities"]
    # The whole conversations `cache_meta` relation is unchanged, which is the
    # strongest form of "the two derivations wrote nothing".
    assert after["conversations_cache_meta"] == (
        before["conversations_cache_meta"])


def test_the_marker_survives_a_browse_and_a_live_tail_request(
    tmp_path, monkeypatch,
):
    """A startup-only flag would freeze nothing: both request paths reach the
    FULL opener, and either would consume the marker on an ordinary browse.

    The browse leg is driven as an HTTP REQUEST, and over a store that owes the
    legacy bridge, because that is the state in which a browse actually reaches
    the full opener. On a healthy store the read-only opener admits the request
    and no derivation is even offered the chance to run, so a browse over one
    would survive the marker whether the policy existed or not.
    """
    ns = _stage(tmp_path, monkeypatch)
    _arm(ns)
    _suppress(ns)
    _owe_the_legacy_bridge(ns)

    server, thread, port = _serve(ns)
    try:
        status, body = _get_json(port, "/api/conversations?source=codex")
        tail_status, tail = _get_json(port, "/api/conversation/s1/events")
    finally:
        stop(server, thread)
    assert status == 200, body
    # #787 / #861 spec 5.8: the request is refused by its query session, which
    # never reaches the full opener, and `--no-sync` names the bridge.
    assert body["degraded_reason"] == "legacy_bridge_pending", body
    assert _marker_value(ns) == "armed-1", (
        "a browse request consumed the marker")

    # The live-tail preflight, as a request.
    assert tail_status == 200, tail
    assert tail["degraded_reason"] == "legacy_bridge_pending", tail
    assert _marker_value(ns) == "armed-1", (
        "the live-tail request consumed the marker")


def test_a_no_sync_store_owing_the_rebuild_reads_normalization_pending(
    tmp_path, monkeypatch,
):
    """The accepted behavior change, covered at the route level rather than
    inferred from the marker's presence.

    Task 18 Step 2 asks for a route test, so this drives the two routes a
    reader actually hits — the Codex browse facets and one conversation detail
    — rather than the kernels behind them.
    """
    ns = _stage(tmp_path, monkeypatch)
    codex_query = sys.modules["_lib_codex_conversation_query"]
    _arm(ns)
    _suppress(ns)
    key = _a_codex_conversation_key(ns)

    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert codex_query.codex_normalization_authoritative(conn) is False
    finally:
        conn.close()

    server, thread, port = _serve(ns)
    try:
        facets_status, facets = _get_json(
            port, "/api/conversations/facets?source=codex")
        detail_status, detail = _get_json(port, _entity_path(key))
    finally:
        stop(server, thread)

    assert facets_status == 200, facets
    assert facets["status"] == "normalization_pending", facets
    assert detail_status == 200, detail
    assert detail["status"] == "normalization_pending", detail
    assert _marker_value(ns) == "armed-1", (
        "a route read consumed the marker it is meant to report pending on")


def test_a_no_sync_browse_over_an_owed_legacy_bridge_is_typed_degraded(
    tmp_path, monkeypatch,
):
    """The second derivation's suppression is DISCLOSED, not silent (#802).

    `_import_legacy_conversation_rows` derives the browse rollup at DB open
    because nothing else will: `list_conversation_facets` reads the rollup's
    `project_label` with no authoritative gate, so an un-derived rollup empties
    the browse project filter for the life of the process. Under `--no-sync`
    the derivation is suppressed, so the route cannot inherit the policy — and
    spec §9's own alternative applies: it returns a typed degraded response
    naming the state instead of serving an empty surface with nothing said.

    No connection the probe saw may stay in the resolver cache (#854 R2).
    #787 / #861 spec 5.8: the probe now runs inside the route's query
    session, which has no hand-back to a full opener; the session's teardown
    must evict what it seeded.
    """
    import _lib_codex_metadata as metadata

    ns = _stage(tmp_path, monkeypatch)
    _suppress(ns)
    _owe_the_legacy_bridge(ns)
    cache_module = sys.modules["_cctally_cache"]
    real_pending = cache_module.conversation_legacy_bridge_pending
    checked = []

    def pending(conn):
        checked.append((conn, conn in metadata._threads_table_resolution_cache))
        return real_pending(conn)

    monkeypatch.setattr(cache_module, "conversation_legacy_bridge_pending", pending)

    server, thread, port = _serve(ns)
    try:
        rail_status, rail = _get_json(port, "/api/conversations")
        facets_status, facets = _get_json(port, "/api/conversations/facets")
    finally:
        stop(server, thread)

    for status, body in ((rail_status, rail), (facets_status, facets)):
        # 200, like every other reader-admission refusal: the condition is
        # expected and transient, and a client that ignores the marker still
        # renders an empty surface rather than failing to parse.
        assert status == 200, body
        assert body["status"] == "degraded", body
        assert body["degraded_reason"] == "legacy_bridge_pending", body
    # The route's own empty shape, so the marker is additive.
    assert rail["conversations"] == [] and rail["total"] == 0, rail
    assert facets["projects"] == [] and facets["models"] == [], facets
    assert checked, "no route reached the legacy-bridge probe"
    assert not [conn for conn, _member in checked
                if conn in metadata._threads_table_resolution_cache], checked


def test_a_browse_over_an_owed_legacy_bridge_still_derives_when_allowed(
    tmp_path, monkeypatch,
):
    """The refusal is bounded to the suppressed mode.

    #787 / #861 spec 5.8: a read route never runs the bridge itself, so a
    mode allowed to do work answers the owed bridge as transient owner work
    (``maintenance``, which the client retries) rather than naming the
    suppressed state, and once the owner's opener runs the bridge the same
    route serves an ordinary 200. Detects a read route that migrates the
    transcript store, or a degraded answer that outlives the owner's bridge.

    #787 / #861 I3b: the owner is the dashboard's own, woken by the read:
    the session reports the pending bridge to `_dashboard_owner_attention`,
    which dispatches `_wake_schema_writer`; no test call runs the bridge.
    """
    ns = _stage(tmp_path, monkeypatch)
    _owe_the_legacy_bridge(ns)
    core = ns["_cctally_core"]
    dash = sys.modules["_cctally_dashboard"]
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", False)
    monkeypatch.setattr(dash, "_SCHEMA_WAKE_RUN_DERIVATIONS", True)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_LAST", {})
    dash._reset_schema_wake_state()
    woken = []
    real_wake = dash._wake_schema_writer

    def recording_wake(**kwargs):
        # Recorded, then deferred: the bridge must not have run before the
        # route's degraded answer is checked below.
        woken.append(kwargs)

    monkeypatch.setattr(dash, "_wake_schema_writer", recording_wake)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)

    server, thread, port = _serve(ns)
    ns["DashboardHTTPHandler"].no_sync = False
    try:
        owed_status, owed = _get_json(port, "/api/conversations")
        conv = ns["open_conversations_db"](attach_cache=False)
        try:
            assert conv.execute(
                f"SELECT COUNT(*) FROM {_BRIDGE_TABLE}").fetchone()[0] == 0, (
                "a read route ran the bridge")
        finally:
            conv.close()
        assert woken, "the pending bridge never reached the owner"
        real_wake(**woken[0])
        dash._join_schema_wake_thread()
        status, body = _get_json(port, "/api/conversations")
    finally:
        unregister()
        stop(server, thread)

    assert owed_status == 200, owed
    assert owed["status"] == "degraded", owed
    assert owed["degraded_reason"] == "maintenance", owed
    assert status == 200, body
    assert body.get("status") != "degraded", body
    conv = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conv.execute(
            f"SELECT COUNT(*) FROM {_BRIDGE_TABLE}").fetchone()[0] == 1, (
            "the owner's opener did not run the bridge")
    finally:
        conv.close()


def test_a_normal_launch_still_performs_the_rebuild(tmp_path, monkeypatch):
    """The suppression is bounded to one mode. A launch allowed to do work
    consumes the marker and converges the corpus, exactly as before."""
    ns = _stage(tmp_path, monkeypatch)
    _arm(ns)
    assert _marker_value(ns) == "armed-1"

    dashboard = sys.modules["_cctally_dashboard"]
    dashboard._dashboard_startup_schema_migration()

    assert _marker_value(ns) is None, (
        "a normal launch must still consume the marker and rebuild")


def test_the_explicit_flag_defaults_to_todays_behaviour(tmp_path, monkeypatch):
    """Every existing caller is byte-unchanged: the keyword defaults to the
    process policy, and the process policy defaults to running them."""
    ns = _stage(tmp_path, monkeypatch)
    _arm(ns)
    assert ns["_cctally_cache"].conversation_derivations_suppressed() is False

    conn = ns["open_conversations_db"]()
    conn.close()
    assert _marker_value(ns) is None, (
        "the default open must still run the two derivations")


def test_a_no_sync_startup_is_comparable_to_a_no_marker_control(
    tmp_path, monkeypatch,
):
    """§9's fifth observable. The row counts above carry the claim; this only
    refuses a suppression that somehow still pays the rebuild's cost."""
    ns = _stage(tmp_path, monkeypatch)
    dashboard = sys.modules["_cctally_dashboard"]

    # Control: no marker armed, derivations allowed. Nothing is owed, so this
    # is the floor cost of one startup open.
    started = time.perf_counter()
    dashboard._dashboard_startup_schema_migration()
    control = time.perf_counter() - started

    _arm(ns)
    _suppress(ns)
    started = time.perf_counter()
    dashboard._dashboard_startup_schema_migration(run_derivations=False)
    suppressed = time.perf_counter() - started

    assert _marker_value(ns) == "armed-1"
    assert suppressed <= control + _STARTUP_DELTA_S, (
        f"suppressed startup {suppressed:.3f}s exceeded the no-marker control "
        f"{control:.3f}s by more than {_STARTUP_DELTA_S}s"
    )


# ── #787 / #861 I3b: owner attention and the `--no-sync` obligation owner ──
#
# Spec 5.5 / AD12. A request thread no longer performs owner opens, so the
# states those opens drained before are drained by an owner the query
# notifies. Each test below names the availability failure it detects: a
# state that, with the owner path gone from the request thread, nothing would
# ever drain, or a `--no-sync` owner that drains by ingesting.

import os
import sqlite3

_SYNC_ENTRIES = ("sync_cache", "sync_codex_cache", "sync_conversations",
                 "sync_codex_conversations", "sync_cache_request",
                 "sync_codex_cache_request")


def _owner_stores(tmp_path, monkeypatch, *, conversations=True):
    """Isolated stores the owner openers created under `process`, in a
    private data directory, plus the dashboard's attention state reset."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    core = ns["_cctally_core"]
    cache = ns["_cctally_cache"]
    app = pathlib.Path(core.APP_DIR)
    app.mkdir(parents=True, exist_ok=True)
    os.chmod(app, 0o700)
    with core.establish_authority("process"):
        core.open_db().close()
        cache.open_cache_db().close()
        if conversations:
            cache.open_conversations_db().close()
    dash = sys.modules["_cctally_dashboard"]
    event = threading.Event()
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_EVENT", event)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_DEBT_DEFERRED", 0)
    # Notifications coalesce process-wide; an earlier test's identical one
    # must not swallow this test's.
    monkeypatch.setattr(core, "_OWNER_ATTENTION_LAST", {})
    dash._reset_schema_wake_state()
    import _cctally_store_access as access
    return ns, core, dash, access, event


def _flag_projection_incomplete(core):
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        conn.execute("INSERT OR REPLACE INTO stats_quota_projection_state"
                     "(id, incomplete) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


def _stats_obligations(access):
    with access.QueryService.session("stats") as s:
        return s.readiness["primary"].obligations


def _projection_drained(access):
    """Whether a query session reads the projection as reconciled. Read only
    once the owner pass the notification woke has finished (see
    `_count_obligation_passes`): a read DURING the pass holds stats's shared
    maintenance lock, and the reconciling `open_db` takes the exclusive one
    non-blocking, so a polled read made that pass lose the lock and leave the
    flag set, and with an hour's interval no timed pass retried it."""
    return "quota_projection_incomplete" not in _stats_obligations(access)


def _record_every_sync(monkeypatch, ns, calls):
    """Recording stand-ins for every ingest entry. Recording rather than
    raising, because several owner paths swallow an exception, which would
    hide a call a raising stand-in was meant to catch."""
    cache_module = ns["_cctally_cache"]

    def _recorder(name):
        def _call(*_args, **_kwargs):
            calls.append(name)
        return _call

    for name in _SYNC_ENTRIES:
        if hasattr(cache_module, name):
            monkeypatch.setattr(cache_module, name, _recorder(name))


def _count_obligation_passes(monkeypatch, dash):
    """A semaphore released each time a `--no-sync` obligation drain returns,
    so a test waits for the owner pass itself rather than polling the store
    while that pass runs."""
    passes = threading.Semaphore(0)
    real_drain = dash._dashboard_obligation_drain

    def drain():
        try:
            real_drain()
        finally:
            passes.release()
    monkeypatch.setattr(dash, "_dashboard_obligation_drain", drain)
    return passes


def _run_obligation_thread(dash, event, body):
    stop_event = threading.Event()
    thread = dash._make_dashboard_obligation_thread(
        stop=stop_event, attention=event, interval=3600)
    thread.start()
    try:
        return body()
    finally:
        stop_event.set()
        event.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "the obligation thread did not stop"


def _notify_projection(core):
    core.notify_owner("stats", core.Readiness(
        "stats", True, None, frozenset({"quota_projection_incomplete"})))


def test_no_sync_obligation_drains_without_ingest(tmp_path, monkeypatch):
    """Detects a `--no-sync` dashboard whose incomplete quota projection is
    never reconciled once request threads stop running the owner `open_db`
    (AD12), or whose obligation owner drains it by ingesting: the flag reaches
    the recovered read through `_DashboardObligationThread`, with zero ingest
    calls and no restart."""
    ns, core, dash, access, event = _owner_stores(tmp_path, monkeypatch)
    _flag_projection_incomplete(core)
    assert "quota_projection_incomplete" in _stats_obligations(access)
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", True)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", True)
    calls, events = [], []
    _record_every_sync(monkeypatch, ns, calls)
    monkeypatch.setattr(core, "CAPABILITY_OBSERVERS", [events.append])
    passes = _count_obligation_passes(monkeypatch, dash)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)
    try:
        ran = _run_obligation_thread(dash, event, lambda: (
            _notify_projection(core), passes.acquire(timeout=30))[1])
    finally:
        unregister()
    assert ran, "the notification never woke an obligation pass"
    assert _projection_drained(access), (
        "the obligation thread never reconciled the projection")
    assert calls == [], calls
    assert not [e for e in events if e.capability == "INGEST_CURRENT"], events


def test_sync_mode_obligation_drains(tmp_path, monkeypatch):
    """Detects a sync-mode notification that the sync thread ignores until
    its next automatic tick (or never): the owner-attention event starts the
    owner iteration early, and the owner `open_db` it performs reconciles."""
    ns, core, dash, access, event = _owner_stores(tmp_path, monkeypatch)
    _flag_projection_incomplete(core)
    # Disarmed for the loop's first, automatic pass: a reconciliation leaves a
    # durable throttle marker, so only the attention-woken pass may run one.
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", False)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", False)
    passes = []
    # Set once the first pass's `open_db` has RETURNED. Waiting on the pass
    # being recorded instead let a loaded machine arm reconciliation while
    # that first open was still running, so the automatic pass drained the
    # flag itself and the attention-woken pass this test is about never ran.
    first_pass_done = threading.Event()
    # Set once the attention-woken pass's `open_db` has returned; the drain is
    # read only then, for the reason `_projection_drained` gives.
    second_pass_done = threading.Event()

    def owner_iteration():
        # The sync thread's owner phase: its build opens stats as `process`.
        passes.append(core.current_authority().kind)
        core.open_db().close()
        (second_pass_done if first_pass_done.is_set()
         else first_pass_done).set()

    stop_event = threading.Event()
    loop = threading.Thread(target=dash._owner_thread_target(
        lambda: dash._dashboard_sync_loop(
            stop=stop_event, interval=3600.0, run_iteration=owner_iteration,
            attention=event)), daemon=True)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)
    loop.start()
    try:
        assert first_pass_done.wait(timeout=30)
        assert passes == ["process"], passes
        assert "quota_projection_incomplete" in _stats_obligations(access)
        monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", True)
        _notify_projection(core)
        woken = second_pass_done.wait(timeout=30)
    finally:
        unregister()
        stop_event.set()
        loop.join(timeout=10)
    assert woken, f"the notification never woke an owner iteration: {passes}"
    assert _projection_drained(access), (
        f"the attention-woken owner iteration did not reconcile: {passes}")
    assert len(passes) == 2, passes      # the interval is an hour
    assert set(passes) == {"process"}, passes


def test_failed_startup_retried_through_wake(tmp_path, monkeypatch):
    """Detects a `--no-sync` dashboard whose failed startup schema open
    leaves the conversation surface degraded forever: the next read reports
    the absent store to the owner, `_wake_schema_writer` retries the open
    (without derivations), and the same route then serves."""
    ns, core, dash, access, event = _owner_stores(
        tmp_path, monkeypatch, conversations=False)
    cctally = sys.modules["cctally"]
    real_open = cctally.open_conversations_db
    failures = []

    def failing_once(**kwargs):
        if not failures:
            failures.append(kwargs)
            raise sqlite3.OperationalError("disk I/O error")
        return real_open(**kwargs)

    monkeypatch.setattr(cctally, "open_conversations_db", failing_once)
    with core.establish_authority("process"):
        dash._dashboard_startup_schema_migration(run_derivations=False)
    assert failures, "the startup open was not attempted"
    assert not pathlib.Path(core.CONVERSATIONS_DB_PATH).exists()
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", True)
    monkeypatch.setattr(dash, "_SCHEMA_WAKE_RUN_DERIVATIONS", False)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)
    server, thread, port = _serve(ns)
    try:
        degraded_status, degraded = _get_json(port, "/api/conversations")
        dash._join_schema_wake_thread()
        status, body = _get_json(port, "/api/conversations")
    finally:
        unregister()
        stop(server, thread)
    assert degraded_status == 200 and degraded["status"] == "degraded", degraded
    assert len(failures) == 1
    assert pathlib.Path(core.CONVERSATIONS_DB_PATH).exists(), (
        "the wake never retried the failed startup open")
    assert status == 200 and body.get("status") != "degraded", body


def test_no_sync_codex_debt_stays_pending(tmp_path, monkeypatch):
    """V1. Detects a `--no-sync` obligation owner that drains Codex
    maintenance debt, which can ingest: with recorded replay and catch-up
    debt and new rollout records present, the projection still drains, but
    no ingest entry runs, no Codex worker is spawned and the debt is still
    recorded afterwards."""
    ns, core, dash, access, event = _owner_stores(tmp_path, monkeypatch)
    cache = ns["_cctally_cache"]
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    debt = {cache.CODEX_REPLAY_FROM_ZERO_KEY: "1",
            cache.CODEX_FINALIZATION_DEBT_KEY: "catch-up"}
    conn = sqlite3.connect(str(core.CACHE_DB_PATH))
    try:
        for key, value in debt.items():
            conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                         "VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()
    _flag_projection_incomplete(core)
    monkeypatch.setattr(core, "QUOTA_PROJECTION_RECONCILE_ENABLED", True)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", True)
    calls, spawned = [], []
    _record_every_sync(monkeypatch, ns, calls)
    import _cctally_journal
    for module, name in ((cache, "_spawn_codex_worker"),
                         (cache, "request_codex_inventory_bootstrap"),
                         (cache, "_request_codex_drain_worker"),
                         (_cctally_journal, "request_quota_index_rebuild")):
        monkeypatch.setattr(module, name,
                            lambda *a, _n=name, **k: spawned.append(_n))
    passes = _count_obligation_passes(monkeypatch, dash)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)
    try:
        # The request-side debt report a `--no-sync` owner records only.
        core.notify_owner("cache", core.Readiness(
            "cache", True, None, frozenset({"codex_maintenance_debt"})))
        assert dash._OWNER_ATTENTION_DEBT_DEFERRED == 1
        assert not event.is_set(), "Codex debt woke the --no-sync owner"
        ran = _run_obligation_thread(dash, event, lambda: (
            _notify_projection(core), passes.acquire(timeout=30))[1])
    finally:
        unregister()
    assert ran, "the notification never woke an obligation pass"
    assert _projection_drained(access), (
        "the projection did not drain beside the Codex debt")
    assert calls == [], calls
    assert spawned == [], spawned
    conn = sqlite3.connect(str(core.CACHE_DB_PATH))
    try:
        recorded = dict(conn.execute(
            "SELECT key, value FROM cache_meta WHERE key IN (?, ?)",
            tuple(debt)).fetchall())
    finally:
        conn.close()
    assert recorded == debt, recorded
    with access.QueryService.session("cache") as s:
        assert "codex_maintenance_debt" in s.readiness["primary"].obligations


class _ScriptedAttention:
    """An owner-attention stand-in whose waits return at once: ``woken``
    scripts each wait's result, and the scripted run ends by setting ``stop``
    on the last wait. Records each wait's timeout (``None`` is unbounded)."""

    def __init__(self, woken, stop):
        self._woken = list(woken)
        self._stop = stop
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if not self._woken:
            self._stop.set()
            return True
        return self._woken.pop(0)

    def clear(self):
        pass

    def set(self):
        pass

    def is_set(self):
        return False


def test_an_unreadable_repair_marker_does_not_end_the_obligation_thread(
        tmp_path, monkeypatch):
    """F8 (#787 / #861 round 9): the marker stat runs inside the loop's
    guard, and an unreadable marker counts as outstanding, so an `OSError`
    from the stat retries the marker's owner open on the next interval
    instead of ending the thread for the dashboard's life (and with it every
    `--no-sync` obligation pass)."""
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    stop_event = threading.Event()
    # The startup pass drains everything; then four timed waits whose
    # marker stat raises, then a timed wait that finds no marker.
    attention = _ScriptedAttention([False] * 6, stop_event)
    stats = iter([OSError("stat failed")] * 4 + [False] * 4)
    passes, marker_passes = [], []

    def marker_present():
        value = next(stats, False)
        if isinstance(value, BaseException):
            raise value
        return value

    dash._dashboard_obligation_loop(
        stop=stop_event, attention=attention, interval=5.0,
        drain=lambda: passes.append(1), outstanding=lambda: False,
        marker_present=marker_present,
        marker_drain=lambda: marker_passes.append(1))
    assert marker_passes, "an unreadable marker was not treated as outstanding"
    assert passes, "non-vacuity: the startup pass ran"


def test_undrainable_obligation_backs_off(tmp_path, monkeypatch):
    """Detects a `--no-sync` dashboard that reruns the owner opens every sync
    interval for as long as an obligation it cannot drain stays reported (a
    migration that fails the same way each time, an armed projection reconcile
    that swallows its failure and reads the whole journal again): timed passes
    that leave the obligation outstanding double their wait up to the cap,
    and owner attention or a drained pass resets it to the interval."""
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    stop_event = threading.Event()
    # Eight timed passes fail, then attention wakes one (still failing), then
    # a timed pass drains everything and the thread waits on attention only.
    attention = _ScriptedAttention([False] * 8 + [True, False], stop_event)
    outstanding = iter([True] * 9 + [False])
    passes = []
    dash._dashboard_obligation_loop(
        stop=stop_event, attention=attention, interval=5.0,
        drain=lambda: passes.append(1), outstanding=lambda: next(outstanding))
    assert len(passes) == 10, passes
    assert attention.timeouts == [
        5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0, 5.0, None]
