"""#620 S3 — the dashboard route's transcript gate (spec §5.3, C6, C20, D-E).

The route evaluates `_transcripts_visible_to_request` ONCE, before the plan is
resolved, and passes the result into plan stage 1. A class denied there is
settled: the store it would have needed is never opened, probed or digested,
and the identifier the response publishes describes what actually ran.

Three things this file pins that nothing else can:

  * a denied request is 200, not 403. A whole-route 403 would discard the
    accounting evidence the request IS authorized to see.
  * a denied request opens no conversations connection at all. Before this
    session's fix the route called `build_diagnosis` without the gate value,
    so the `True` default applied and every request — denied or not — paid for
    the projection and published a `generationId` bound to transcript-derived
    facts.
  * Codex `subagent_fanout` still measures on a denied route, because it reads
    `codex_conversation_threads` and `codex_session_entries`, both of which
    live in `cache.db`. That is the Codex table of Section 3, and it is the
    one cell where the same class name renders two verdicts in one report.
"""
from __future__ import annotations

import ast
import datetime as dt
import fcntl
import functools
import hashlib
import http.client
import inspect
import json
import os
import pathlib
import sys
import textwrap
import threading

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_route import _run_bounded_in_process
from test_620_s2_diagnosis_sources import (  # reuse the seeded corpora
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
    _seed_codex_windows,
)
from test_620_s3_evaluators import (  # the S3 corpora
    CODEX_MODEL, _seed_churn, _seed_codex_entry, _seed_codex_thread,
)

from tests._support_http import start, stop

UTC = dt.timezone.utc
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"
_WINDOW_QUERY = f"{_WINDOW}&tz=Etc%2FUTC"

# A loopback IP literal is visible; a HOSTNAME is the DNS-rebinding vector the
# gate rejects, so it is how a denied request is produced without binding this
# test to a LAN interface. Both go through `_transcripts_visible_to_request`
# unmodified — D-E requires exercising the configuration, never inventing a
# denial by patching the predicate.
_DENIED_HOST = "dashboard.example.test"


def _dash():
    return sys.modules["_cctally_dashboard"]


def _sources():
    """The adapter module, loaded the way the route loads it.

    `sys.modules` alone is not enough: the route lazy-loads this sibling on
    the first request, so a test that patches it before any request has run
    would raise `KeyError` rather than patch anything.
    """
    return load_script()["_load_sibling"]("_cctally_diagnosis_sources")


class _Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)


class _Client:
    def __init__(self, port):
        self.port = port

    def get(self, path, *, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {}
            if host is not None:
                headers["Host"] = host
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read().decode()
            return _Response(response.status, dict(response.getheaders()), body)
        finally:
            conn.close()


def _boot(ns, *, bind="127.0.0.1", expose=False):
    dash = _dash()
    handler = dash.DashboardHTTPHandler
    handler.hub = ns["SSEHub"]()
    handler.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    handler.hub.publish(handler.snapshot_ref.get())
    handler.no_sync = True
    # Stated rather than inherited: these are CLASS attributes, so a value a
    # neighbouring module left behind would decide this test's gate.
    handler.cctally_host = bind
    handler.cctally_expose_transcripts = expose
    handler.cctally_api_token = None
    server = dash._QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = start(server)
    return server, thread, _Client(server.server_address[1])


@pytest.fixture
def claude_server(tmp_path, monkeypatch):
    """A Claude store with accounting evidence AND transcript evidence."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    # Distinct session names, because `_seed_churn` owns
    # `/tmp/projects/sess-a.jsonl` and the two seeders would otherwise collide
    # on `session_entries(source_path, line_offset)`.
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)),
                 projects=("/repo/x", "/repo/y"),
                 sessions=("sess-x", "sess-y"))
    _seed_claude_blocks(ns)
    _seed_churn(ns, compaction_before_window=False)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def codex_server(tmp_path, monkeypatch):
    """A Codex store whose fan-out lives entirely in `cache.db`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_codex_fanout_population(ns)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


def _seed_codex_fanout_population(ns):
    """One resolvable parent and two delegated children, at REPORT scale.

    The evaluator unit tests seed four entries, which is enough to decide the
    predicate and not enough to clear `min_priced_entries`. A route test that
    asserts a VERDICT needs a population the class can be measured over, or it
    would assert `withheld / insufficient_population` and prove nothing about
    the gate.
    """
    cache = ns["open_cache_db"]()
    try:
        _seed_codex_thread(cache, key="v1.root-a.parent", native="p1",
                           root_thread="user")
        for index in range(12):
            _seed_codex_entry(
                cache, key="v1.root-a.parent", offset=index,
                at=WINDOW_START + dt.timedelta(minutes=5 * index + 5))
        for slot, child in enumerate(("c1", "c2")):
            _seed_codex_thread(cache, key=f"v1.root-a.{child}", native=child,
                               root_thread="subagent", parent="p1")
            for index in range(12):
                _seed_codex_entry(
                    cache, key=f"v1.root-a.{child}", offset=index,
                    at=WINDOW_START + dt.timedelta(
                        minutes=80 + 60 * slot + 5 * index),
                    model=CODEX_MODEL, input_tokens=4_000)
        cache.commit()
    finally:
        cache.close()
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])


def _by_kind(response, index=0):
    return {c["contributorClass"]: c
            for c in response.json["results"][index]["classes"]}


# --- the denial is partial, never a 403 ---------------------------------

def test_a_denied_request_returns_200_with_s2_classes_still_measured(
        claude_server):
    """A whole-route 403 would discard accounting evidence the request IS
    authorized to see, and would contradict S2's partially-withheld contract.
    """
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                                 host=_DENIED_HOST)
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    assert by_kind["model_mix"]["verdict"] != "withheld"
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert by_kind[kind]["code"] == "transcripts_not_visible", kind


def test_the_same_request_from_a_loopback_host_measures_them(claude_server):
    """The discriminating half. Without it the test above would pass over a
    fixture that had nothing to measure in the first place."""
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                                 host="127.0.0.1")
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert by_kind[kind]["code"] != "transcripts_not_visible", kind


def test_a_denied_request_never_opens_the_conversations_store(
        claude_server, monkeypatch):
    """C20. Before the gate was threaded the route paid for the projection on
    every request and published a `generationId` bound to transcript-derived
    facts, whether or not the request was allowed to read them."""
    sources = _sources()
    opened = []
    real = sources.open_read_only

    def _spy(kind):
        opened.append(kind)
        return real(kind)

    monkeypatch.setattr(sources, "open_read_only", _spy)
    # #787 / #861 AD21: the bundle opens stats and cache as query sessions.
    real_session = sources.query_session

    def _spy_session(kind):
        opened.append(kind)
        return real_session(kind)

    monkeypatch.setattr(sources, "query_session", _spy_session)
    # #1000: conversations opens through the bundle's read admission.
    real_conversations = sources.StoreBundle._open_conversations

    def _spy_conversations(self):
        opened.append("conversations")
        return real_conversations(self)

    monkeypatch.setattr(sources.StoreBundle, "_open_conversations",
                        _spy_conversations)
    _run_bounded_in_process(monkeypatch, sources)
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                                 host=_DENIED_HOST)
    assert response.status == 200, response.body
    assert "conversations" not in opened, opened
    # Non-vacuity: the accounting stores WERE opened, so the absence above is
    # a decision rather than a request that never reached the adapter.
    assert "cache" in opened, opened


def test_a_denied_plan_publishes_no_conversations_generation_component(
        claude_server):
    """The identifier describes what actually ran. A denied plan reads no
    conversation bytes, so it publishes no `conversations` component — and its
    `generationId` differs from the allowed plan's, because the plan is part
    of the identity."""
    denied = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                               host=_DENIED_HOST).json
    allowed = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                                host="127.0.0.1").json
    assert "conversations" not in denied["results"][0]["generation"]
    assert "conversations" in allowed["results"][0]["generation"]
    assert (denied["results"][0]["generation"]["generationId"]
            != allowed["results"][0]["generation"]["generationId"])


def test_the_route_worker_reads_transcripts_under_the_parents_maintenance_lock(
        claude_server, tmp_path, monkeypatch):
    """#1000 §5.10: the route builds in a forkserver worker that reloads
    cctally and is handed the parent's store paths. Its read admission must
    take the PARENT's conversation maintenance lock, or it opens the
    transcript store while the pre-rebuild probe reads it in place (or while
    recovery quarantines it), and under a redirected data folder it writes a
    lock file into the default one. A worker re-derives its other paths from
    the forkserver's environment, frozen when that server started, so the
    parent's lock sits where no environment derives it: only a worker handed
    the lock path can find it. Holding that lock exclusive, as the probe
    does, must make the worker decline the store, so the report publishes no
    `conversations` component."""
    lock = tmp_path / "parent-only" / "conversations.db.maintenance.lock"
    lock.parent.mkdir()
    # #787 AD22: the lock exists, as an owner open leaves it; a read never
    # creates it, so a worker handed a wrong path finds no lock and declines.
    lock.touch()
    monkeypatch.setattr(sys.modules["_cctally_core"],
                        "CONVERSATIONS_LOCK_MAINTENANCE_PATH", lock)
    query = f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}"
    free = claude_server.get(query, host="127.0.0.1")
    assert free.status == 200, free.body
    # Non-vacuity: with the lock free, this worker does read transcripts.
    assert "conversations" in free.json["results"][0]["generation"]
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        response = claude_server.get(query, host="127.0.0.1")
    assert response.status == 200, response.body
    assert "conversations" not in response.json["results"][0]["generation"]


def _probe_stub_worker_init(cctally_path, store_paths, *rest, delay, log,
                            move_identity):
    """The worker's initializer with AD19's probe stubbed (#787 AD30).

    Module-level and bound with `functools.partial`, so it pickles by
    reference into the forkserver worker, where a parent monkeypatch never
    reaches. It runs the real initializer (paths, and the handed-over header
    verdicts), then delays every probe child by DELAY seconds and records
    each launch in LOG. MOVE_IDENTITY bumps stats.db's mtime after the
    handoff, so the verdict the worker was handed no longer matches."""
    sys.modules["_cctally_diagnosis_sources"]._spawn_worker_init(
        cctally_path, store_paths, *rest)
    access = sys.modules["_cctally_store_access"]
    real_argv = access._header_probe_argv

    def slow_argv(target):
        with open(log, "a") as fh:
            fh.write("probe\n")
        return [sys.executable, "-c",
                f"import os,sys,time; time.sleep({delay}); "
                "os.execv(sys.argv[1], sys.argv[1:])", *real_argv(target)]

    access._header_probe_argv = slow_argv
    if move_identity:
        st = os.stat(store_paths["DB_PATH"])
        os.utime(store_paths["DB_PATH"],
                 ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    with open(log, "a") as fh:
        fh.write("installed\n")


@pytest.mark.parametrize("moved", [None, "before_handoff", "in_worker"])
def test_the_diagnosis_worker_inherits_the_parents_header_verdict(
        claude_server, tmp_path, monkeypatch, moved):
    """#787 AD30. The route builds in a one-shot forkserver worker whose AD19
    header cache starts empty, so a probe child whose launch outlasts the
    500 ms interactive budget refused stats.db and served 503 under load
    (I5f-1). The parent hands the worker its cached verdict for stats.db's
    current stat identity, so the worker launches no probe and serves 200.
    A stat identity that moved, before the handoff (the parent holds only
    the old identity's verdict) or after it (the worker was handed a verdict
    for an identity the file no longer has), re-probes exactly as an empty
    cache would: the delayed probe refuses and the route serves base's 503.
    The conversations maintenance lock is held, as in the route-gate test
    above, so the worker provably ran with the parent's handed-over paths."""
    lock = tmp_path / "parent-only" / "conversations.db.maintenance.lock"
    lock.parent.mkdir()
    lock.touch()
    # `_sources()` first: it reloads the siblings, and the route reads the
    # modules it leaves registered.
    sources = _sources()
    core = sys.modules["_cctally_core"]
    access = sys.modules["_cctally_store_access"]
    monkeypatch.setattr(core, "CONVERSATIONS_LOCK_MAINTENANCE_PATH", lock)
    log = tmp_path / "probe-stub.log"
    monkeypatch.setattr(sources, "_spawn_worker_init", functools.partial(
        _probe_stub_worker_init, delay=0.8, log=str(log),
        move_identity=(moved == "in_worker")))
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    # The parent's own stats read leaves its verdict, as a snapshot build does.
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    if moved == "before_handoff":
        st = os.stat(core.DB_PATH)
        os.utime(core.DB_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    query = f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}"
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        response = claude_server.get(query, host="127.0.0.1")
    launches = log.read_text().split()
    # Non-vacuity: the stub ran in the worker that served this request.
    assert "installed" in launches
    if moved is None:
        assert response.status == 200, response.body
        assert "probe" not in launches
        assert "conversations" not in response.json["results"][0]["generation"]
    else:
        assert "probe" in launches
        assert response.status == 503, response.body


def test_the_verdict_handoff_never_probes_and_follows_the_identity(
        claude_server, monkeypatch):
    """#787 AD30, in process: the handoff only reads the parent's cache (a
    parent without a verdict hands nothing and launches no child), and a
    seeded verdict is keyed by the identity it was handed for, so it serves
    that identity without a probe and never the one the file moves to."""
    core = sys.modules["_cctally_core"]
    access = sys.modules["_cctally_store_access"]
    launches = []
    real_run = access._run_header_probe

    def _counting(target):
        launches.append(str(target))
        return real_run(target)

    monkeypatch.setattr(access, "_run_header_probe", _counting)
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    assert access.header_verdict_handoff((str(core.DB_PATH),)) == ()
    assert launches == []
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    handed = access.header_verdict_handoff((str(core.DB_PATH),))
    assert [verdict for _key, verdict in handed] == ["rollback"]
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    access.seed_header_verdicts(handed + (("not-a-key", "rollback"),
                                          (handed[0][0], "unknown")))
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    assert len(launches) == 1           # the parent's read only
    st = os.stat(core.DB_PATH)
    os.utime(core.DB_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    assert len(launches) == 2           # the moved identity re-probed


def test_a_returned_worker_verdict_serves_only_its_own_identity(
        claude_server, tmp_path, monkeypatch):
    """#787 AD30 (revision 29), in process: the verdicts a worker returns
    with its result are added to the parent's cache under the identity the
    worker probed, so the parent hands them on without a probe, and never
    serve another identity: an entry for an identity the file does not have
    is never handed over for it, and once the file moves the parent
    re-probes."""
    import pickle

    sources = _sources()
    core = sys.modules["_cctally_core"]
    access = sys.modules["_cctally_store_access"]
    launches = []
    real_run = access._run_header_probe

    def _counting(target):
        launches.append(str(target))
        return real_run(target)

    monkeypatch.setattr(access, "_run_header_probe", _counting)
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    current = access._header_stat_key(os.stat(core.DB_PATH))
    other = current[:3] + (current[3] + 7, current[4])
    result = tmp_path / "worker-result"
    with open(result, "wb") as fh:
        pickle.dump(("ok", "report", ((current, "rollback"), (other, "wal"))),
                    fh, protocol=5)
    assert sources._payload_result(sources._read_payload(str(result))) == \
        "report"
    assert access.header_verdict_handoff((str(core.DB_PATH),)) == (
        (current, "rollback"),)
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    assert launches == []               # the returned verdict served it
    st = os.stat(core.DB_PATH)
    os.utime(core.DB_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert access.header_verdict_handoff((str(core.DB_PATH),)) == ()
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    assert len(launches) == 1           # the moved identity re-probed


def _prompt_probe_worker_init(cctally_path, store_paths, *rest, log):
    """The worker's initializer with a prompt AD19 probe that logs each call
    (#787 AD30): it reads the 20 header bytes in process, so a worker probe
    costs no child launch and every one is counted in LOG."""
    sys.modules["_cctally_diagnosis_sources"]._spawn_worker_init(
        cctally_path, store_paths, *rest)
    access = sys.modules["_cctally_store_access"]

    def prompt(target):
        with open(log, "a") as fh:
            fh.write("probe\n")
        with open(target, "rb") as fh:
            return fh.read(20)

    access._run_header_probe = prompt
    with open(log, "a") as fh:
        fh.write("installed\n")


@pytest.mark.parametrize("no_sync", [True, False], ids=["no-sync", "sync"])
def test_a_workers_probe_returns_to_the_parent_for_the_next_request(
        claude_server, tmp_path, monkeypatch, no_sync):
    """#787 AD30 (revision 29, R28-5). After a stats change the parent holds
    no verdict for stats.db's new identity, so the first request's worker
    probes. That worker returns its verdict with its result and the parent
    adopts it, so the second request's worker is handed it and probes
    nothing: two consecutive real-worker requests probe once in total, in
    both dashboard modes. Without the return path every request's one-shot
    worker probed again, and under load each probe could spend the whole
    interactive budget and serve 503."""
    sources = _sources()
    core = sys.modules["_cctally_core"]
    access = sys.modules["_cctally_store_access"]
    monkeypatch.setattr(_dash().DashboardHTTPHandler, "no_sync", no_sync)
    log = tmp_path / "worker-probes.log"
    monkeypatch.setattr(sources, "_spawn_worker_init", functools.partial(
        _prompt_probe_worker_init, log=str(log)))
    parent = []
    real_run = access._run_header_probe
    monkeypatch.setattr(access, "_run_header_probe",
                        lambda target: parent.append(str(target))
                        or real_run(target))
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    # The parent knew the old identity; the stats change moves it.
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    parent.clear()
    st = os.stat(core.DB_PATH)
    os.utime(core.DB_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    query = f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}"
    first = claude_server.get(query, host="127.0.0.1")
    second = claude_server.get(query, host="127.0.0.1")
    assert (first.status, second.status) == (200, 200), (first.body,
                                                         second.body)
    lines = log.read_text().split()
    # Non-vacuity: each request ran in its own stubbed worker.
    assert lines.count("installed") == 2, lines
    assert lines.count("probe") + len(parent) == 1, (lines, parent)
    assert lines[:3] == ["installed", "probe", "installed"], lines


def _child_only_probe_worker_init(cctally_path, store_paths, *rest, log):
    """The worker's initializer with an AD19 probe that only a process
    forked from the worker can run (#787 AD30, revision 31): in the worker
    itself the probe fails closed, so the `source=all` provider child is the
    one process that learns stats.db's current identity. Every call is
    logged in LOG with the process role."""
    sys.modules["_cctally_diagnosis_sources"]._spawn_worker_init(
        cctally_path, store_paths, *rest)
    access = sys.modules["_cctally_store_access"]
    worker = os.getpid()

    def child_only(target):
        role = "worker" if os.getpid() == worker else "child"
        with open(log, "a") as fh:
            fh.write(f"probe:{role}\n")
        if role == "worker":
            return None
        with open(target, "rb") as fh:
            return fh.read(20)

    access._run_header_probe = child_only
    with open(log, "a") as fh:
        fh.write("installed\n")


def test_a_provider_childs_probe_crosses_the_worker_to_the_next_request(
        claude_server, tmp_path, monkeypatch):
    """#787 AD30 (revision 31, R30-3). A worker returns every verdict it
    learned during the request, including one adopted from its own
    `source=all` forked provider child, under the identity that child
    probed. Here only the child can probe stats.db's new identity, and the
    next request's worker is handed that verdict, so neither it nor its
    provider child probes again. Detects a worker that returns only the
    verdicts it probed itself: the child's verdict stopped at the worker,
    and every later request probed again inside its interactive budget."""
    sources = _sources()
    core = sys.modules["_cctally_core"]
    access = sys.modules["_cctally_store_access"]
    log = tmp_path / "worker-probes.log"
    monkeypatch.setattr(sources, "_spawn_worker_init", functools.partial(
        _child_only_probe_worker_init, log=str(log)))
    parent = []
    real_run = access._run_header_probe
    monkeypatch.setattr(access, "_run_header_probe",
                        lambda target: parent.append(str(target))
                        or real_run(target))
    with access._HEADER_CACHE_LOCK:
        access._HEADER_CACHE.clear()
    assert access._stats_header_kind(core.DB_PATH) == "rollback"
    parent.clear()
    st = os.stat(core.DB_PATH)
    os.utime(core.DB_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    current = access._header_stat_key(os.stat(core.DB_PATH))
    query = f"/api/diagnosis?source=all&window={_WINDOW_QUERY}"
    claude_server.get(query, host="127.0.0.1")
    second = claude_server.get(query, host="127.0.0.1")
    lines = log.read_text().split()
    assert lines.count("installed") == 2, lines
    split = lines.index("installed", 1)
    # Non-vacuity: only the first request's provider child learned it.
    assert "probe:child" in lines[:split], lines
    assert lines[split + 1:] == [], lines
    assert parent == []
    assert second.status == 200, second.body
    # The verdict the parent adopted is keyed by the identity the child
    # probed, never another one.
    assert access.header_verdict_handoff((str(core.DB_PATH),)) == (
        (current, "rollback"),)


def test_codex_fanout_is_measured_on_a_denied_route(codex_server):
    """The Codex table of Section 3: fan-out reads only `cache.db`, so it
    needs no transcript authorization and none is claimed."""
    response = codex_server.get(f"/api/diagnosis?source=codex&window={_WINDOW_QUERY}",
                                host=_DENIED_HOST)
    assert response.status == 200, response.body
    by_kind = _by_kind(response)
    assert by_kind["subagent_fanout"]["verdict"] == "contributor"
    assert by_kind["cache_churn"]["verdict"] == "not_applicable"
    assert by_kind["short_high_context"]["code"] == "transcripts_not_visible"


# --- the predicate itself is untouched ----------------------------------

def _function_source_sha(module_path, name):
    source = pathlib.Path(module_path).read_text()
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = "".join(lines[node.lineno - 1:node.end_lineno])
            return hashlib.sha256(textwrap.dedent(body).encode()).hexdigest()
    raise AssertionError(f"{name} not found in {module_path}")


# The predicate as of #620 S3. D-E: `_transcripts_visible_to_request` is
# unchanged and the twelve routes that call it are untouched, so a change to
# it must be a deliberate act that also updates this literal.
_GATE_SHA = "af98055decf2a3bcbf9e576fb2ba002a4a9b87c2e72b160ae210025762621799"


def test_transcripts_visible_predicate_is_not_modified():
    module = sys.modules.get("_cctally_dashboard")
    if module is None:
        load_script()
        module = sys.modules["_cctally_dashboard"]
    assert _function_source_sha(
        inspect.getfile(module), "_transcripts_visible_to_request"
    ) == _GATE_SHA


# --- the parameter is required, so an unthreaded call fails loudly ------

def test_build_diagnosis_requires_the_gate_value_explicitly():
    """R9. The `True` default let a call site forget the gate and silently
    read transcripts; the route was exactly that call site. A missing keyword
    must now raise rather than default to visible."""
    sources = _sources()
    for name in ("build_diagnosis", "build_provider_diagnosis"):
        parameter = inspect.signature(
            getattr(sources, name)).parameters["transcripts_visible"]
        assert parameter.default is inspect.Parameter.empty, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name


def test_the_route_threads_the_gate_rather_than_defaulting():
    """The call site, read as source. The behavioural tests above would also
    pass if the route hard-coded `True` on a build that had no gate at all, so
    the argument it actually passes is stated here."""
    module = sys.modules.get("_cctally_dashboard")
    if module is None:
        load_script()
        module = sys.modules["_cctally_dashboard"]
    body = _handler_method_source(inspect.getfile(module),
                                  "_handle_get_diagnosis")
    assert "transcripts_visible=" in body
    assert "_transcripts_visible_to_request()" in body


def _handler_method_source(module_path, name):
    source = pathlib.Path(module_path).read_text()
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name} not found in {module_path}")


# --- the retryable state ------------------------------------------------

def test_generation_incoherent_is_503_and_the_body_names_the_cause(
        claude_server, monkeypatch):
    """A component that moves twice while it is read is published as a
    retryable failure rather than as a digest over a state that never existed
    as a whole."""
    sources = _sources()
    counter = {"n": 0}

    def _moving(component, bundle):
        counter["n"] += 1
        return f"{component}:{counter['n']}"

    monkeypatch.setattr(sources, "_probe_component", _moving)
    _run_bounded_in_process(monkeypatch, sources)
    response = claude_server.get(f"/api/diagnosis?source=claude&window={_WINDOW_QUERY}",
                                 host="127.0.0.1")
    assert response.status == 503, response.body
    assert response.json["code"] == "generation_incoherent"
