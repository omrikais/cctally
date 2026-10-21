"""#968 T8: the Codex live-tail polling seam over containers (spec 5.9).

Every test drives the real ``_run_conversation_events_stream`` through the real
qualified handler. The peer probe is replaced so a stream runs an exact number
of poll cycles and a scripted action can run before a given cycle; nothing
else about the driver is stubbed.
"""
from __future__ import annotations

import io
import pathlib
import sqlite3
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import _lib_codex_conversation_query as q  # noqa: E402
from _codex_zstd_fixtures import conversation_records, write_rollout  # noqa: E402
from test_codex_publication_barrier import (  # noqa: E402,F401 (env is a fixture)
    OTHER_SESSION, SESSION, _cache, _compress, _key, _name, _sync,
    _sync_stopping_after, env)
from test_codex_publication_barrier_readers import (  # noqa: E402
    _accounts, _append, _conv)


class _StreamHandler:
    """The handler surface the real live-tail stream touches."""

    no_sync = False

    def __init__(self) -> None:
        self.wfile = io.BytesIO()
        self.responses: list = []
        self.errors: list[str] = []

    def send_response(self, code, message=None) -> None:
        self.responses.append(code)

    def send_header(self, *_args) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def _respond_json(self, code, body) -> None:
        self.responses.append((code, body))

    def log_error(self, fmt, *args) -> None:
        self.errors.append(fmt % args)

    def frames(self, name: str) -> int:
        return self.wfile.getvalue().decode("utf-8").count(f"event: {name}\n")


def _conversation_module(ns):
    ns["DashboardHTTPHandler"]  # loads the dashboard siblings
    return sys.modules["_cctally_dashboard_conversation"]


def _script_cycles(conv, monkeypatch, cycles: int, actions: dict) -> dict:
    """The stream runs exactly ``cycles`` polls; ``actions[n]`` runs before
    poll ``n`` (1-based). ``state["cycle"]`` is 0 until the first poll."""
    state = {"cycle": 0}

    def peer_gone(handler, *, timeout=0.0):
        state["cycle"] += 1
        if state["cycle"] > cycles:
            return True
        action = actions.get(state["cycle"])
        if action is not None:
            action()
        return False

    monkeypatch.setattr(conv, "_request_peer_gone", peer_gone)
    monkeypatch.setattr(conv, "_LIVE_TAIL_DEBOUNCE", 0.0)
    return state


def _run_codex_stream(ns, monkeypatch, key, *, account=None, cycles=8,
                      actions=None, reset_every=3, after_ingest=None,
                      before_accounting=None):
    """The real qualified Codex live tail. Returns the handler and the
    ``only_paths`` of every transcript ingest after the connect ingest."""
    conv = _conversation_module(ns)
    real = conv.sync_codex_conversations
    real_accounting = conv.sync_codex_cache
    monkeypatch.setattr(conv, "_LIVE_TAIL_FILE_RESET_EVERY", reset_every)
    state = _script_cycles(conv, monkeypatch, cycles, actions or {})
    ingests: list = []
    accounting_ingests: list = []
    transcript_stats: list = []

    def transcript(writer, **kwargs):
        if state["cycle"]:            # the connect ingest runs before poll 1
            ingests.append(sorted(kwargs.get("only_paths") or ()))
        stats = real(writer, **kwargs)
        if state["cycle"]:
            transcript_stats.append(stats)
        if state["cycle"] and after_ingest is not None:
            after_ingest()
        return stats

    def accounting(writer, **kwargs):
        if state["cycle"]:
            accounting_ingests.append(sorted(kwargs.get("only_paths") or ()))
            if before_accounting is not None:
                before_accounting()
        return real_accounting(writer, **kwargs)

    monkeypatch.setattr(conv, "sync_codex_cache", accounting)
    monkeypatch.setattr(conv, "sync_codex_conversations", transcript)
    handler = _StreamHandler()
    conv._qualified_conversation_events(handler, key, account)
    assert handler.errors == [], handler.errors
    assert handler.responses == [200], handler.responses
    assert handler.frames("baselined") == 1
    handler.accounting_ingests = accounting_ingests
    handler.transcript_stats = transcript_stats
    return handler, ingests


def _message_count(ns, key) -> int:
    conn = ns["open_conversations_db"]()
    try:
        return conn.execute("SELECT COUNT(*) FROM codex_conversation_messages "
                            "WHERE conversation_key=?", (key,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("scoped", [False, True])
def test_unchanged_plain_and_compressed_files_are_never_rescheduled(
        env, monkeypatch, scoped):
    """A plain and a compressed contributor, neither changing: no poll ever
    reschedules either (including the unscoped stream's child discovery)."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, account=_accounts(ns)[0] if scoped else None)
    assert ingests == []
    assert handler.frames("tail") == 0


@pytest.mark.parametrize("scoped", [False, True])
def test_a_compression_after_connect_is_observed_once(env, monkeypatch, scoped):
    """The plain -> compressed transition is observed (not skipped), ingested
    once, and stays quiet through pending verification and its adoption."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(3, session_id=SESSION, label="a"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    before = _message_count(ns, key)

    def verify():
        _cache().run_codex_container_verification_drain(wait_for_requests=False)

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, account=_accounts(ns)[0] if scoped else None,
        actions={2: lambda: _compress(a), 5: verify})
    assert ingests == [[str(a)]]
    # The scoped stream waits quietly for accounting adoption, then retries
    # once to advance transcripts. Pending accounting is never polled as an
    # ingest on every cycle.
    assert handler.accounting_ingests == [[str(a)]] * (2 if scoped else 1)
    # An unscoped stream emits for every clean ingest; a scoped one only when
    # its account's rows changed, and an equal-content compression changes none.
    assert handler.frames("tail") == (0 if scoped else 1)
    assert _message_count(ns, key) == before


def test_a_held_container_is_observed_once(env, monkeypatch):
    """No decoder: the compressed container is held (nothing is recorded on
    the cursor), and the stream neither skips it nor reschedules it."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(3, session_id=SESSION, label="a"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    before = _message_count(ns, key)

    def compress_without_decoder():
        _compress(a)
        monkeypatch.setattr(leaf, "resolve_zstd_backend",
                            lambda: leaf.UNAVAILABLE)

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, actions={2: compress_without_decoder})
    assert ingests == [[str(a)]]
    assert handler.frames("tail") == 1
    assert _message_count(ns, key) == before


@pytest.mark.parametrize("scoped", [False, True])
def test_a_barrier_deferred_plain_append_waits_quietly_for_the_seal(
        env, monkeypatch, scoped):
    """Review S-P2e, spec T5 case 8 through the real driver: an append to the
    plain contributor of a population conversation whose key cannot seal yet
    is deferred by the conversation pass. The stream acknowledges it at its
    pre-ingest observation: no pending flicker, no per-poll re-ingest and no
    `tail` frame until the seal, then exactly one ingest and one update."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    before = _message_count(ns, key)
    _sync_stopping_after(ns, 0)     # an incomplete walk census: nothing seals
    statuses: list = []

    def append():
        _append(b, conversation_records(2, session_id=SESSION, label="b")[-4:])

    def observe():
        conn = ns["open_conversations_db"]()
        try:
            statuses.append(q.get_codex_conversation(
                conn, key, effective_speed="standard", limit=0)["status"])
        finally:
            conn.close()

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, account=_accounts(ns)[0] if scoped else None,
        cycles=9, actions={2: append, 3: observe, 4: observe, 5: observe,
                           6: lambda: _sync(ns)})
    # RED on the Task 11 tree: the deferred append is re-ingested on polls 2
    # to 5 and, unscoped, a `tail` frame is written for each of them.
    assert ingests == [[str(b)], [str(b)]]   # the append, then once after the seal
    assert statuses == ["ok", "ok", "ok"]    # the last good publication stays
    assert handler.frames("tail") == 1       # one update, after the seal
    assert _message_count(ns, key) > before


def test_committed_signatures_compare_containers(env):
    """Both helpers return the observation's shape: accepted plain, accepted
    zstd, and a container recorded as pending verification."""
    ns, provider, day, _ = env
    rc = sys.modules["_lib_codex_rollout_container"]
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    conv = _conversation_module(ns)

    def signatures():
        conn = ns["open_conversations_db"]()
        try:
            return (conv._codex_all_committed_sizes(conn),
                    conv._codex_cached_file_sigs(conn, [str(a), str(b)]))
        finally:
            conn.close()

    every, cached = signatures()
    assert every[str(a)] == cached[str(a)] == rc.observe_watch_sig(str(a))
    assert every[str(b)] == cached[str(b)] == ("plain", b.stat().st_size)
    _compress(b)                       # now pending verification in both stores
    _sync(ns)
    _conv(ns)
    every, cached = signatures()
    assert every[str(b)] == cached[str(b)] == rc.observe_watch_sig(str(b))
    assert cached[str(b)][0] == "zstd"


def test_a_stream_without_a_seam_keeps_the_integer_probe(env, monkeypatch,
                                                          tmp_path):
    """Claude streams pass no seam: an integer committed cursor stays quiet
    while unchanged and reschedules exactly once on growth."""
    ns, *_ = env
    conv = _conversation_module(ns)
    path = tmp_path / "claude-session.jsonl"
    path.write_text("{}\n")
    committed = {str(path): path.stat().st_size}
    calls: list = []

    def ingest(changed):
        calls.append(list(changed))
        committed[str(path)] = path.stat().st_size
        return types.SimpleNamespace(targeted_clean=True)

    def grow():
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{}\n")

    _script_cycles(conv, monkeypatch, 6, {3: grow})
    handler = _StreamHandler()
    conv._run_conversation_events_stream(
        handler, None, passive=False, tail_data={"sessionId": "s1"},
        resolve=lambda: [str(path)], ingest=ingest,
        cached_sigs=lambda paths: {p: committed.get(p) for p in paths})
    assert calls == [[str(path)], [str(path)]]   # the connect ingest + the growth
    assert handler.frames("tail") == 1


def test_a_container_replaced_during_ingest_is_observed_again(env, monkeypatch):
    """An acknowledgment names the pre-ingest container, so a replacement
    arriving during that ingest must trigger one more observation."""
    ns, _provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    before = _message_count(ns, key)
    replaced = []

    def replace_once():
        if not replaced:
            write_rollout(a, conversation_records(3, session_id=SESSION,
                                                   label="replacement"))
            replaced.append(_compress(a))

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, actions={2: lambda: _compress(a)},
        after_ingest=replace_once)
    assert len(replaced) == 1
    assert ingests == [[str(a)], [str(a)]]
    assert handler.frames("tail") == 2
    assert _message_count(ns, key) == before


def test_an_unreadable_seal_token_does_not_rearm_a_deferred_file(
        env, monkeypatch):
    """Missing seal evidence never implies a new certificate: keep waiting
    at the pre-ingest observation until a readable token actually advances."""
    ns, _provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    _sync_stopping_after(ns, 0)
    conv = _conversation_module(ns)
    real_token = conv._codex_seal_token

    def append():
        _append(b, conversation_records(2, session_id=SESSION, label="b")[-4:])

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, cycles=9,
        actions={2: append,
                 3: lambda: monkeypatch.setattr(conv, "_codex_seal_token",
                                               lambda _conn: None),
                 5: lambda: monkeypatch.setattr(conv, "_codex_seal_token",
                                               real_token),
                 6: lambda: _sync(ns)})
    assert ingests == [[str(b)], [str(b)]]
    assert handler.frames("tail") == 1


def test_a_scoped_plain_append_ingested_beyond_the_probe_emits_once(
        env, monkeypatch):
    """CD01: X is probed, then Y arrives before the real accounting walk.

    The same ingest publishes X+Y. Its committed cursor is ahead of X,
    which is successful growth rather than unresolved barrier deferral.
    """
    ns, _provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    before = _message_count(ns, key)
    rc = sys.modules["_lib_codex_rollout_container"]
    real_probe = rc.observe_watch_sig
    observations = []
    appended = []

    def probe(path):
        sig = real_probe(path)
        if path == str(b):
            observations.append(sig)
        return sig

    monkeypatch.setattr(rc, "observe_watch_sig", probe)

    def append_x():
        _append(b, conversation_records(2, session_id=SESSION, label="b")[-4:])

    def append_y_once():
        if appended:
            return
        # Both real poll sites saw X; no observation of Y precedes ingestion.
        x = ("plain", b.stat().st_size)
        assert observations[-2:] == [x, x]
        _append(b, conversation_records(3, session_id=SESSION, label="b")[-4:])
        appended.append((x, ("plain", b.stat().st_size)))

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, account=_accounts(ns)[0],
        actions={2: append_x}, before_accounting=append_y_once)
    assert len(appended) == 1 and appended[0][1][1] > appended[0][0][1]
    # Prove the transcript actually published both appends before checking SSE.
    assert _message_count(ns, key) == before + 4
    assert handler.transcript_stats[0].barrier_files_deferred == 1
    assert handler.transcript_stats[0].barrier_keys_published == 1
    conn = ns["open_conversations_db"]()
    try:
        assert _conversation_module(ns)._codex_cached_file_sigs(
            conn, [str(b)])[str(b)] == appended[0][1]
    finally:
        conn.close()
    assert handler.frames("tail") == 1
    assert ingests == [[str(b)]]


def test_a_container_hold_never_acknowledges_a_failed_plain_publication(
        env, monkeypatch):
    """CD02: pending child + presealed parent + one-shot SQLite failure.

    The executor owns the child hold, but the plain parent's failed
    publication still needs an ordinary poll retry under the unchanged seal.
    """
    ns, _provider, day, _ = env
    a, parent, child = (day / _name(11, 1), day / _name(12, 2),
                        day / _name(13, 3))
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(parent, conversation_records(1, session_id=SESSION, label="p"))
    records = conversation_records(1, session_id=OTHER_SESSION, label="c")
    records[0]["payload"].update({
        "forked_from_id": SESSION, "parent_thread_id": SESSION,
        "agent_path": "/root/child", "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": SESSION, "agent_path": "/root/child",
            "depth": 1}}}})
    write_rollout(child, records)
    _sync(ns)
    _conv(ns)
    key = _key(ns, parent)
    before = _message_count(ns, key)
    failures = []
    seals = []
    observed_after_failure = []
    conv = _conversation_module(ns)

    def seal_token():
        conn = ns["open_conversations_db"]()
        try:
            return conv._codex_seal_token(conn)
        finally:
            conn.close()

    def fail_once(_conn, keys):
        if key in keys and not failures:
            failures.append(tuple(keys))
            raise sqlite3.OperationalError("CD02 one-shot publication failure")

    def append_and_hold():
        _append(parent, conversation_records(2, session_id=SESSION, label="p")[-4:])
        _sync(ns)              # seals the parent before the stream's ingest
        seals.append(seal_token())
        _compress(child)        # independent hold cannot invalidate that seal
        monkeypatch.setattr(_cache(), "_CODEX_PUBLICATION_PRECOMMIT_HOOK", fail_once)

    def after_ingest():
        seals.append(seal_token())
        if len(seals) == 2:
            observed_after_failure.append(_message_count(ns, key))

    handler, ingests = _run_codex_stream(
        ns, monkeypatch, key, cycles=7, reset_every=50,
        actions={2: append_and_hold}, after_ingest=after_ingest)
    # No discovery fallback runs: recovery must be the ordinary changed-path poll.
    assert len(failures) == 1
    assert observed_after_failure == [before]  # failed transaction kept last-good
    failed = handler.transcript_stats[0]
    assert failed.container_pending or any(failed.container_skipped.values())
    assert failed.barrier_retry and failed.files_failed == 0
    assert len(seals) >= 3 and len(set(seals)) == 1
    assert _message_count(ns, key) == before + 2
    assert ingests == [sorted([str(parent), str(child)])] * 2
    assert handler.frames("tail") == 1
