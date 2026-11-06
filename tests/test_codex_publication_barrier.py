"""#968 T5 (cache side): the publication barrier's certificates and the heal."""
from __future__ import annotations

import itertools
import json
import os
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import _lib_codex_publication_barrier as barrier  # noqa: E402


def _t(identity, *, gen="g1", end=100, hold=None, wm_gen="g1", wm_off=100,
       path=None):
    return barrier.Target(identity, path or f"/r/{identity}.jsonl", gen, end,
                          hold, wm_gen, wm_off)


def test_classify_target_states_and_precedence():
    assert barrier.classify_target(_t("a")) == (barrier.READY, None)
    assert barrier.classify_target(_t("a", end=None)) == (
        barrier.REQUIRED, "accounting-incomplete")
    assert barrier.classify_target(_t("a", wm_gen="g0")) == (
        barrier.REQUIRED, "membership-unproven")
    assert barrier.classify_target(_t("a", wm_off=None)) == (
        barrier.REQUIRED, "membership-unproven")
    assert barrier.classify_target(_t("a", wm_off=40)) == (
        barrier.REQUIRED, "membership-unproven")
    assert barrier.classify_target(_t("a", hold=barrier.PENDING_HOLD)) == (
        barrier.REQUIRED, barrier.PENDING_HOLD)
    for hold in sorted(barrier.SUSPENSION_HOLDS):
        assert barrier.classify_target(
            _t("a", hold=hold, wm_gen="g0", wm_off=60)) == (
                barrier.SUSPENDED, hold)
        # Review N2: a suspension whose accepted membership was never proven
        # is still an exclusion; degraded contributors degrade, they do not
        # freeze (U6). Only the pending hold is a dependency.
        assert barrier.classify_target(
            _t("a", hold=hold, wm_gen=None, wm_off=None)) == (
                barrier.SUSPENDED, hold)
    assert barrier.classify_target(
        _t("a", hold=barrier.PENDING_HOLD, wm_gen=None, wm_off=None)) == (
            barrier.REQUIRED, barrier.PENDING_HOLD)
    with pytest.raises(ValueError):
        barrier.classify_target(_t("a", hold="mystery"))


def test_a_stale_generation_never_satisfies_a_new_target():
    """Case 3, kernel half: G0's watermark far beyond G1's end proves nothing."""
    stale = _t("a", gen="g1", end=50, wm_gen="g0", wm_off=10_000)
    assert barrier.classify_target(stale).state == barrier.REQUIRED


def _fact(identity, path, offset, cwd, gen="g1"):
    meta = json.dumps({"conversation_key": "k", "cwd": cwd,
                       "source_path": path}, sort_keys=True)
    return barrier.MemberFact(identity, gen, path, meta, offset)


def test_thread_selection_is_the_complete_walks_last_writer_in_any_order():
    facts = [
        _fact("a", "/r/a.jsonl", 0, "A0"),
        _fact("a", "/r/a.jsonl", 900, "A9"),
        _fact("b", "/r/b.jsonl", 0, "B0"),
        barrier.MemberFact("c", "g1", "/r/c.jsonl", None, None),
    ]
    for order in itertools.permutations(facts):
        assert barrier.select_thread(order)["cwd"] == "B0"
    assert barrier.select_thread([]) is None


def test_a_retained_thread_a_contributor_supplies_is_kept():
    """Review P2-2: legacy certification keeps the retained thread row when
    some contributor supplies exactly that metadata (7 of the 243 measured
    multi-file keys store a row the max-path rule would not choose)."""
    facts = [_fact("a", "/r/a.jsonl", 0, "A0"), _fact("b", "/r/b.jsonl", 0, "B0")]
    retained = json.loads(facts[0].metadata_json)
    for order in itertools.permutations(facts):
        assert barrier.select_thread(order, retained=retained)["cwd"] == "A0"
    stranger = dict(retained, cwd="elsewhere")     # matches no contributor
    assert barrier.select_thread(facts, retained=stranger)["cwd"] == "B0"


def test_a_seal_never_drops_a_published_thread_because_of_an_exclusion():
    """Reviews P1-1 and N2: with an exclusion, a seal that finds no metadata
    keeps the retained thread whatever path it names (a key never freezes,
    and an excluded contributor never retracts a thread to None); only an
    all-readable conversation, or one with no retained thread, seals None."""
    a = _t("a", path="/r/a.jsonl")
    b = _t("b", path="/r/b.jsonl", gen="new", end=None,
           hold="decode-error", wm_gen="old", wm_off=60)
    members = [barrier.MemberFact("a", "g1", "/r/a.jsonl", None, None),
               barrier.MemberFact("b", "old", "/r/b.jsonl", None, None)]
    kept = {"conversation_key": "k", "source_path": "/r/b.jsonl", "cwd": "B"}
    sealed = barrier.decide_seal("k", 5, [a, b], members, retained_thread=kept)
    assert sealed.sealable and sealed.certificate["thread"]["cwd"] == "B"
    stranger = dict(kept, source_path="/r/z.jsonl", cwd="Z")
    still = barrier.decide_seal("k", 5, [a, b], members,
                                retained_thread=stranger)
    assert still.sealable and still.certificate["thread"]["cwd"] == "Z"
    nothing = barrier.decide_seal("k", 5, [a, b], members)
    assert nothing.sealable and nothing.certificate["thread"] is None
    readable = barrier.decide_seal("k", 5, [a], members[:1],
                                   retained_thread=stranger)
    assert readable.sealable and readable.certificate["thread"] is None


def test_an_unknown_membership_exclusion_supplies_its_retained_thread():
    """Review N2: a suspended contributor whose membership was never proven
    is an exclusion; its RETAINED_GENERATION row carries the retained thread
    it supplied, which outranks a readable contributor's later path only
    through the retained-row preference, and never a real generation."""
    retained = {"conversation_key": "k", "source_path": "/r/a.jsonl",
                "cwd": "A"}
    a = _t("a", path="/r/a.jsonl", gen="new", end=None,
           hold="verification-unverified", wm_gen=None, wm_off=None)
    b = _t("b", path="/r/b.jsonl")
    members = [
        barrier.MemberFact("a", barrier.RETAINED_GENERATION, "/r/a.jsonl",
                           barrier.metadata_from_row(retained), 0),
        _fact("b", "/r/b.jsonl", 0, "B-id-only"),
    ]
    for order in itertools.permutations(members):
        sealed = barrier.decide_seal("k", 9, [a, b], list(order),
                                     retained_thread=retained)
        cert = sealed.certificate
        assert sealed.sealable and cert["thread"]["cwd"] == "A"
        assert [c["identity"] for c in cert["contributors"]] == ["b"]
        assert cert["exclusions"] == [{
            "identity": "a", "path": "/r/a.jsonl",
            "reason": "verification-unverified", "acceptedGeneration": None}]
    # A real-generation candidate never takes the sentinel row's metadata.
    ready_a = _t("a", path="/r/a.jsonl")
    sealed = barrier.decide_seal("k", 9, [ready_a, b], members)
    assert sealed.certificate["thread"]["cwd"] == "B-id-only"


def test_decide_seal_blocks_on_required_and_excludes_suspended():
    members = [_fact("a", "/r/a.jsonl", 0, "A"),
               _fact("b", "/r/b.jsonl", 0, "B", gen="old")]
    blocked = barrier.decide_seal(
        "k", 7, [_t("a", path="/r/a.jsonl"),
                 _t("b", path="/r/b.jsonl", end=None)], members)
    assert blocked.sealable is False
    assert blocked.blocking == (("b", "accounting-incomplete"),)
    suspended_b = _t("b", path="/r/b.jsonl", gen="new", end=None,
                     hold="decoder-unavailable", wm_gen="old", wm_off=60)
    sealed = barrier.decide_seal(
        "k", 7, [_t("a", path="/r/a.jsonl"), suspended_b], members,
        timestamps=("t0", "t1"))
    cert = sealed.certificate
    assert sealed.sealable and cert["revision"] == 7
    assert cert["conversationKey"] == "k"
    assert cert["contributors"] == [{"identity": "a", "path": "/r/a.jsonl",
                                     "generation": "g1",
                                     "completeLineEnd": 100}]
    assert cert["exclusions"] == [{"identity": "b", "path": "/r/b.jsonl",
                                   "reason": "decoder-unavailable",
                                   "acceptedGeneration": "old"}]
    # U6: the suspended contributor's retained accepted metadata still counts.
    assert cert["thread"]["cwd"] == "B"
    assert (cert["thread"]["firstSeenUtc"], cert["thread"]["lastSeenUtc"]) == (
        "t0", "t1")


def test_certificate_round_trip_coverage_and_monotonic_publication():
    cert = barrier.decide_seal("k", 3, [_t("a", path="/r/a.jsonl")], []).certificate
    assert cert["thread"] is None
    text = barrier.certificate_json(cert)
    assert barrier.parse_certificate(text) == cert
    assert barrier.parse_certificate("{}") is None
    assert barrier.parse_certificate("not json") is None
    assert barrier.certificate_covers(cert, "/r/a.jsonl", "g1") == 100
    assert barrier.certificate_covers(cert, "/r/a.jsonl", "g0") is None
    assert barrier.certificate_covers(cert, "/r/b.jsonl", "g1") is None
    assert not barrier.certificate_excludes(cert, "/r/a.jsonl")
    assert barrier.publication_advances(0, 3)
    assert barrier.publication_advances(None, 1)
    assert not barrier.publication_advances(3, 3)
    assert not barrier.publication_advances(4, 3)


def test_metadata_round_trips_every_thread_field():
    import _lib_jsonl
    thread = _lib_jsonl.CodexThreadMetadata(
        source_root_key="root", source_path="/r/a.jsonl", native_thread_id="n",
        root_thread_id="user", parent_thread_id=None, conversation_key="k",
        cwd="/tmp/\udcff", git_json='{"b":1}', source_kind="cli",
        thread_source_json='"user"', model_provider="openai",
        context_window=272000)
    meta = json.loads(barrier.metadata_from_thread(thread))
    assert set(meta) == set(barrier.THREAD_FIELDS)
    assert meta["cwd"] == "/tmp/\udcff"            # #845: undecodable cwd survives
    assert meta["context_window"] == 272000

from conftest import (  # noqa: E402
    load_script, redirect_paths)
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, token_records, write_rollout, zstd_bytes)
from test_codex_bounded_ingest import _auth_json  # noqa: E402

import _lib_codex_rollout_container as rc  # noqa: E402

SESSION = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a01"
OTHER_SESSION = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a03"
REAL_RESOLVE = rc.resolve_zstd_backend


def _name(hour: int, suffix: int) -> str:
    return (f"rollout-2026-07-20T{hour:02d}-00-00-"
            f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{suffix:04d}.jsonl")


def _meta_only(session_id: str, cwd: str) -> list[dict]:
    """A metadata-only contributor: one `session_meta`, nothing else."""
    return [{"timestamp": "2026-07-20T12:00:00.000Z", "type": "session_meta",
             "payload": {"id": session_id, "session_id": session_id,
                         "cwd": cwd, "model": "gpt-5", "source": "cli",
                         "thread_source": "user"}}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    # The heal and Task 11's reader tests run conversation passes over
    # July-2026 fixtures (Global Constraints: retention).
    redirect_paths(
        ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "codex"
    day = provider / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(provider))
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    spawned: list[str] = []
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: spawned.append(command) or True)
    return ns, provider, day, spawned


def _cache():
    return sys.modules["_cctally_cache"]


def _sync(ns, **kwargs):
    conn = ns["open_cache_db"]()
    try:
        return ns["sync_codex_cache"](conn, **kwargs)
    finally:
        conn.close()


def _sync_stopping_after(ns, files_committed: int):
    """A budgeted (hook-shaped) walk whose clock passes the deadline as soon
    as ``files_committed`` file batches have committed."""
    cc = _cache()
    real_batch, real_clock = cc._write_codex_file_batch, cc._walk_clock
    state = {"committed": 0, "calls": 0}

    def counting(*args, **kwargs):
        result = real_batch(*args, **kwargs)
        state["committed"] += 1
        return result

    def clock():
        state["calls"] += 1
        if state["calls"] == 1:
            return 0.0
        return 0.0 if state["committed"] < files_committed else 10_000.0

    cc._write_codex_file_batch = counting
    cc._walk_clock = clock
    try:
        return _sync(ns, budget_seconds=5.0)
    finally:
        cc._write_codex_file_batch = real_batch
        cc._walk_clock = real_clock


def _compress(path: pathlib.Path) -> pathlib.Path:
    target = path.with_name(path.name + ".zst")
    target.write_bytes(zstd_bytes(path.read_bytes()))
    path.unlink()
    return target


def _replace_container(zpath: pathlib.Path, data: bytes) -> None:
    """A new container at the same name with a new inode (Codex writes and renames)."""
    tmp = zpath.with_name("replacement.tmp")
    tmp.write_bytes(zstd_bytes(data))
    os.replace(tmp, zpath)


def _one(ns, sql, params=()):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _key(ns, path) -> str:
    return _one(ns, "SELECT last_conversation_key FROM codex_session_files "
                    "WHERE path=?", (str(path),))[0]


def _cursor_row(ns, path) -> dict:
    conn = ns["open_cache_db"]()
    try:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM codex_session_files WHERE path=?",
                                 (str(path),)).fetchone())
    finally:
        conn.close()


def _certificate(ns, key):
    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute(
            "SELECT revision, certificate_json FROM codex_barrier_certificates "
            "WHERE conversation_key=? ORDER BY revision", (key,)).fetchall()
    finally:
        conn.close()
    assert len(rows) <= 1, "only the latest certificate is kept"
    return None if not rows else barrier.parse_certificate(rows[0][1])


def _required_revision(ns, key):
    row = _one(ns, "SELECT required_revision FROM codex_barrier_population "
                   "WHERE conversation_key=?", (key,))
    return None if row is None else int(row[0])


def _paths(cert) -> list[str]:
    return sorted(c["path"] for c in cert["contributors"])


def _target(ns, path):
    return _one(ns, "SELECT generation_token, state, reason, complete_line_end "
                    "FROM codex_barrier_targets WHERE source_path=?", (str(path),))


def test_membership_and_targets_follow_the_parser(env):
    """Every key the parser meets, metadata-only records included (5.11)."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    key = _key(ns, a)
    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute(
            "SELECT source_path, conversation_key, metadata_json, metadata_offset "
            "FROM codex_barrier_membership ORDER BY source_path").fetchall()
        ends = dict(conn.execute(
            "SELECT path, last_byte_offset FROM codex_session_files"))
        population = conn.execute(
            "SELECT COUNT(*) FROM codex_barrier_population").fetchone()[0]
    finally:
        conn.close()
    assert [(r[0], r[1]) for r in rows] == [(str(a), key), (str(b), key)]
    assert json.loads(rows[0][2])["cwd"] == "/tmp/proj-a" and rows[0][3] == 0
    assert json.loads(rows[1][2])["cwd"] == "/tmp/proj-b" and rows[1][3] == 0
    for path in (a, b):
        generation, state, reason, end = _target(ns, path)
        assert (state, reason, end) == ("ready", None, ends[str(path)])
        assert generation.startswith("plain:")
    assert population == 0          # plain-only conversations keep today's rules (R4)


def test_a_compressed_read_admits_the_conversation_and_snapshots_its_thread(env):
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _sync(ns)
    key = _key(ns, a)
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _compress(b)
    _sync(ns)
    entry = _one(ns, "SELECT cwd, source_path FROM codex_barrier_entry_threads "
                     "WHERE conversation_key=?", (key,))
    current = _one(ns, "SELECT cwd FROM codex_conversation_threads "
                       "WHERE conversation_key=?", (key,))
    assert entry == ("/tmp/proj-a", str(a))   # the row before B overwrote it
    assert current == ("/tmp/proj-b",)
    cert = _certificate(ns, key)
    assert _paths(cert) == sorted([str(a), str(b)])
    assert cert["thread"]["cwd"] == "/tmp/proj-b"
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_repairs")[0] == 0


@pytest.mark.parametrize("second", ["metadata-only", "message-bearing"])
@pytest.mark.parametrize("order", ["compressed-first", "plain-first"])
def test_the_certificate_waits_for_every_readable_contributor(env, second, order):
    """Barrier cases 2 and 4: a budget stop after the first contributor seals
    nothing; the certificate names both, and the thread is the last writer."""
    ns, provider, day, _ = env
    first_hour, second_hour = (11, 12) if order == "compressed-first" else (12, 11)
    a = day / _name(first_hour, 1)                     # compressed, message-bearing
    b = day / _name(second_hour, 2)                    # plain
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b")
                  if second == "metadata-only" else
                  conversation_records(1, session_id=SESSION, label="b",
                                       cwd="/tmp/proj-b"))
    _sync_stopping_after(ns, 1)          # accounting stops after the first file
    assert _one(ns, "SELECT value FROM cache_meta WHERE key=?",
                (_cache().CODEX_BARRIER_CENSUS_KEY,)) == ("0",)
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_certificates")[0] == 0
    _sync(ns)
    key = _key(ns, a)
    cert = _certificate(ns, key)
    assert _paths(cert) == sorted([str(a), str(b)])
    assert cert["exclusions"] == []
    last_writer = "/tmp/proj-a" if order == "plain-first" else "/tmp/proj-b"
    assert cert["thread"]["cwd"] == last_writer


def test_a_stale_accepted_cursor_never_certifies_a_new_generation(env):
    """Barrier case 3 (968-SR-012) at the certificate level."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(6, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    zpath = _compress(a)
    _sync(ns)
    key = _key(ns, a)
    first = _certificate(ns, key)
    (g0,) = first["contributors"]
    shorter = a.with_name("shorter.jsonl.tmp")
    write_rollout(shorter, conversation_records(1, session_id=SESSION,
                                                label="z", cwd="/tmp/proj-z"))
    new_bytes = shorter.read_bytes()
    shorter.unlink()
    assert len(new_bytes) < g0["completeLineEnd"]
    _replace_container(zpath, new_bytes)
    _sync(ns)
    generation, state, reason, _end = _target(ns, a)
    assert generation != g0["generation"]
    assert (state, reason) == ("required", barrier.PENDING_HOLD)
    assert _cursor_row(ns, a)["last_byte_offset"] == g0["completeLineEnd"]
    assert _certificate(ns, key) == first          # old certificate stays
    assert _required_revision(ns, key) > first["revision"]
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    (only,) = cert["contributors"]
    assert only["generation"] not in (g0["generation"], generation)
    assert only["completeLineEnd"] == len(new_bytes)
    assert cert["thread"]["cwd"] == "/tmp/proj-z"
    assert cert["revision"] > first["revision"]


def test_a_torn_final_line_is_ready_at_its_last_complete_line(env):
    """Barrier case 5 at the certificate level."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    complete = a.read_bytes()
    late = (json.dumps({"timestamp": "2026-07-20T10:59:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message",
                                    "message": "late"}}) + "\n").encode()
    zpath = a.with_name(a.name + ".zst")
    zpath.write_bytes(zstd_bytes(complete + late[:25]))
    a.unlink()
    _sync(ns)
    key = _key(ns, a)
    cursor = _cursor_row(ns, a)
    assert cursor["last_byte_offset"] == len(complete)
    assert cursor["decoded_size_bytes"] == len(complete) + 25
    first = _certificate(ns, key)
    assert first["contributors"][0]["completeLineEnd"] == len(complete)
    _replace_container(zpath, complete + late)
    _sync(ns)                    # the transition is observed and re-armed
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    assert cert["revision"] > first["revision"]
    assert cert["contributors"][0]["completeLineEnd"] == len(complete) + len(late)


HOLDS = ["decoder-unavailable", "decode-error", "verification-unverified"]


def _degrade(ns, monkeypatch, path, hold) -> "tuple[pathlib.Path, bytes]":
    """Replace the plain rollout at ``path`` by a compressed container that
    ends in ``hold`` (a missing decoder, a truncated frame, or an
    equal-length mutation of a legacy prefix that the cache-side legacy
    equivalence rejects, so verification ends ``unverified``)."""
    leaf = sys.modules["_lib_codex_rollout_container"]
    good = path.read_bytes()
    zpath = path.with_name(path.name + ".zst")
    if hold == "decoder-unavailable":
        zpath.write_bytes(zstd_bytes(good))
        monkeypatch.setattr(leaf, "resolve_zstd_backend", lambda: leaf.UNAVAILABLE)
    elif hold == "decode-error":
        zpath.write_bytes(zstd_bytes(good)[:-7])        # a truncated frame
    else:
        conn = ns["open_cache_db"]()
        try:   # a legacy row: no anchor, so only derived equivalence decides
            conn.execute(
                "UPDATE codex_session_files SET content_anchor=NULL, "
                "content_anchor_version=NULL, content_anchor_offset=NULL "
                "WHERE path=?", (str(path),))
            conn.commit()
        finally:
            conn.close()
        # Review r3 F1b: mutate bytes the cache side compares. Its legacy check
        # (`_prefix_facts_equal`) compares accounting, quotas, threads and
        # resume state, and messages only where cache.db holds them (it holds
        # none), so a message-text mutation would be adopted. One accounting
        # row's output tokens, at equal length, ends `legacy-inequivalent`.
        marker = b'"output_tokens":40'
        assert marker in good
        zpath.write_bytes(zstd_bytes(
            good.replace(marker, b'"output_tokens":41', 1)))
    path.unlink()
    return zpath, good


def _forget_membership(ns, path) -> None:
    """Make ``path`` a legacy prefix: no parser-proven membership, exactly
    the state `_codex_barrier_init_targets` gives an identity accounted
    before #968 (unknown membership)."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_barrier_membership WHERE source_path=?",
                     (str(path),))
        conn.execute(
            "UPDATE codex_barrier_targets SET accepted_generation_token=NULL, "
            "accepted_complete_line_end=NULL, state='required', "
            "reason='membership-unproven' WHERE source_path=?", (str(path),))
        conn.commit()
    finally:
        conn.close()


def _thread(ns, key):
    return _one(ns, "SELECT cwd, source_path FROM codex_conversation_threads "
                    "WHERE conversation_key=?", (key,))


def _barrier_status(ns) -> dict:
    return json.loads(_one(ns, "SELECT value FROM cache_meta WHERE key=?",
                           ("codex_container_status",))[0])["barrier"]


def _seed_unrelated(ns, day) -> "tuple[str, pathlib.Path]":
    """An unrelated population conversation, read and sealed while a decoder
    is present, and the path of a plain sibling that later gives it new
    activity no decoder is needed for. "Others progress" then holds for every
    hold, `decoder-unavailable` included: a compressed rollout written after
    `_degrade` removed the decoder could never be read."""
    o = day / _name(13, 3)
    write_rollout(o, conversation_records(1, session_id=OTHER_SESSION,
                                          label="o"))
    _compress(o)
    _sync(ns)
    key = _key(ns, o)
    assert _certificate(ns, key) is not None
    return key, day / _name(14, 5)


def _unrelated_activity(sibling: pathlib.Path) -> None:
    """New plain activity for the unrelated conversation (id-only meta)."""
    write_rollout(sibling, token_records(0, session_id=OTHER_SESSION)[:1]
                  + conversation_records(1, session_id=OTHER_SESSION,
                                         label="p")[1:])


# Review P1-1: barrier case 6 over the full matrix. The axes are the degraded
# contributor's membership (known: a proven watermark; unknown: a legacy
# prefix) and its retained accepted evidence (with: an accounted cursor and
# retained rows; without: a compressed rollout never ingested). "Known
# without" has no realizable state, because a proven watermark IS retained
# accepted membership evidence; and "unknown without" has no inconclusive
# verification, because verification only ever judges an accepted cursor.
# In every cell a suspended contributor is an exclusion, never a dependency
# (review N2: U6 "degraded contributors degrade; they do not freeze"); its
# retained accepted facts keep what they supplied, and a transient pending
# hold is the only hold that blocks.


@pytest.mark.parametrize("hold", HOLDS)
def test_a_degraded_contributor_is_excluded_and_rejoins_on_recovery(
        env, monkeypatch, hold):
    """Barrier case 6 (U6) at the certificate level: known membership, with
    retained accepted evidence."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    key = _key(ns, a)
    other_key, sibling = _seed_unrelated(ns, day)
    zpath, good = _degrade(ns, monkeypatch, a, hold)
    _unrelated_activity(sibling)
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    assert cert is not None, "a degraded contributor must not freeze the key"
    assert _paths(cert) == [str(b)]
    (excluded,) = cert["exclusions"]
    assert (excluded["path"], excluded["reason"]) == (str(a), hold)
    assert excluded["acceptedGeneration"] is not None   # retained membership kept
    assert cert["thread"]["cwd"] == "/tmp/proj-b"
    assert str(sibling) in _paths(_certificate(ns, other_key))  # others progress
    status = _barrier_status(ns)
    assert status["suspended"] == 1
    assert status["unresolved"][hold] == 1
    assert (status["membershipKnown"], status["membershipUnknown"]) == (1, 0)
    assert status["withRetainedFallback"] == 1
    assert status["keysMembershipUnknown"] == 0
    monkeypatch.setattr(leaf, "resolve_zstd_backend", REAL_RESOLVE)
    _replace_container(zpath, good)                     # recovery
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    assert _paths(cert) == sorted([str(a), str(b)])
    assert cert["exclusions"] == []


@pytest.mark.parametrize("hold", HOLDS)
def test_a_degraded_contributor_of_unknown_membership_is_excluded_with_its_thread(
        env, monkeypatch, hold):
    """Review N2 (replacing P1-1's freeze): unknown membership, with retained
    accepted evidence. The contributor is an exclusion, not a dependency: the
    key seals over the readable contributor, the thread the degraded
    contributor supplied is kept from its retained facts, unrelated keys
    progress, and the key is counted under unknown membership."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    # `b` sorts first, so the full walk (path order, an unconditional thread
    # upsert) leaves `a`, the metadata-bearing contributor, as the last writer.
    a, b = day / _name(11, 1), day / _name(10, 2)
    write_rollout(a, _meta_only(SESSION, "/tmp/proj-a")
                  + conversation_records(2, session_id=SESSION, label="a",
                                         cwd="/tmp/proj-a")[1:])
    write_rollout(b, token_records(2, session_id=SESSION, start_minute=20))
    _sync(ns)
    key = _key(ns, a)
    thread = _thread(ns, key)
    assert thread == ("/tmp/proj-a", str(a))       # `a` supplies the thread
    other_key, sibling = _seed_unrelated(ns, day)
    _forget_membership(ns, a)
    zpath, good = _degrade(ns, monkeypatch, a, hold)
    _unrelated_activity(sibling)
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    # 92e06df85 RED: the walk prunes nothing yet reads no `.zst`, and there is
    # no barrier at all (`codex_container_status` is absent). The round-1
    # plan's RED: `cert is None` (the key froze on `membership-unknown`).
    status = _barrier_status(ns)
    cert = _certificate(ns, key)
    assert cert is not None, "a degraded contributor froze its key"
    assert _paths(cert) == [str(b)]
    (excluded,) = cert["exclusions"]
    assert (excluded["path"], excluded["reason"]) == (str(a), hold)
    assert excluded["acceptedGeneration"] is None        # membership unknown
    assert cert["thread"]["cwd"] == "/tmp/proj-a"        # from retained facts
    assert cert["thread"]["source_path"] == str(a)
    assert _thread(ns, key) == thread
    assert _one(ns, "SELECT cwd FROM codex_barrier_entry_threads "
                    "WHERE conversation_key=?", (key,)) == ("/tmp/proj-a",)
    assert str(sibling) in _paths(_certificate(ns, other_key))  # others progress
    assert status["keysMembershipUnknown"] == 1
    assert status["membershipUnknown"] >= 1
    assert status["suspended"] >= 1
    assert status["unresolved"][hold] == 1
    monkeypatch.setattr(leaf, "resolve_zstd_backend", REAL_RESOLVE)
    _replace_container(zpath, good)                     # recovery
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _cache().run_codex_barrier_membership_scan(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    assert _paths(cert) == sorted([str(a), str(b)])
    assert cert["exclusions"] == []
    assert cert["thread"]["cwd"] == "/tmp/proj-a"
    assert _barrier_status(ns)["keysMembershipUnknown"] == 0
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_membership "
                    "WHERE generation_token=?",
                (barrier.RETAINED_GENERATION,)) == (0,)


def test_a_mixed_legacy_unverified_and_live_plain_conversation_keeps_advancing(
        env, monkeypatch):
    """Review N2: a conversation fed by a legacy contributor that ends
    `verification-unverified` and by a live plain contributor. The plain
    contributor's new activity is certified at its new end, and the thread
    stays the legacy contributor's (U6: degrade, never freeze)."""
    ns, provider, day, _ = env
    # `b` sorts first, so the full walk (path order, an unconditional thread
    # upsert) leaves `a`, the metadata-bearing contributor, as the last writer.
    a, b = day / _name(11, 1), day / _name(10, 2)
    write_rollout(a, _meta_only(SESSION, "/tmp/proj-a")
                  + conversation_records(2, session_id=SESSION, label="a",
                                         cwd="/tmp/proj-a")[1:])
    # `b` opens with an id-only session_meta (no cwd), then message turns.
    write_rollout(b, token_records(0, session_id=SESSION)[:1]
                  + conversation_records(2, session_id=SESSION, label="b")[1:])
    _sync(ns)
    key = _key(ns, a)
    thread = _thread(ns, key)
    assert thread == ("/tmp/proj-a", str(a))
    _forget_membership(ns, a)
    _degrade(ns, monkeypatch, a, "verification-unverified")
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    first = _certificate(ns, key)
    assert first is not None, "the legacy contributor froze the conversation"
    assert [e["reason"] for e in first["exclusions"]] == [
        "verification-unverified"]
    end_before = first["contributors"][0]["completeLineEnd"]
    with b.open("a", encoding="utf-8") as fh:        # new plain activity
        for record in conversation_records(3, session_id=SESSION,
                                           label="b")[-4:]:
            fh.write(json.dumps(record) + "\n")
    _sync(ns)
    cert = _certificate(ns, key)
    assert cert["revision"] > first["revision"]
    (contributor,) = cert["contributors"]
    assert contributor["path"] == str(b)
    assert contributor["completeLineEnd"] == b.stat().st_size > end_before
    assert cert["thread"]["cwd"] == "/tmp/proj-a"
    assert _thread(ns, key) == thread


@pytest.mark.parametrize("hold", ["decoder-unavailable", "decode-error"])
def test_a_never_ingested_unreadable_rollout_is_counted_never_dropped(
        env, monkeypatch, hold):
    """Review P1-1 and P2-4: unknown membership without retained accepted
    evidence. The walk records the hold, the rollout is counted, and no key
    it cannot be shown to feed changes."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a, c = day / _name(11, 1), day / _name(14, 4)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _sync(ns)
    key = _key(ns, a)
    thread = _thread(ns, key)
    write_rollout(c, conversation_records(1, session_id=SESSION, label="c",
                                          cwd="/tmp/proj-c"))
    zc = _compress(c)
    if hold == "decode-error":
        zc.write_bytes(zc.read_bytes()[:-7])
    else:
        monkeypatch.setattr(leaf, "resolve_zstd_backend",
                            lambda: leaf.UNAVAILABLE)
    stats = _sync(ns)
    assert stats.container_skipped.get(hold) == 1
    # Review N2: a suspension of unknown membership is an exclusion. With no
    # retained rows no key is known to depend on `c`, so it admits, blocks
    # and changes nothing: `key` keeps its thread and is not even queued.
    assert _target(ns, c)[1:3] == (barrier.SUSPENDED, hold)
    assert _thread(ns, key) == thread
    assert _certificate(ns, key) is None
    status = _barrier_status(ns)
    assert status["membershipUnknown"] == 1
    assert status["withoutRetainedFallback"] == 1
    assert status["keysMembershipUnknown"] == 0
    assert status["unresolved"][hold] == 1


def test_a_live_append_rearms_and_seals_only_once_accounted(env):
    """Barrier cases 8 and 10 at the certificate level."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    key = _key(ns, a)
    first = _certificate(ns, key)

    def append_turn(index: int) -> None:
        records = conversation_records(index + 1, session_id=SESSION, label="b")
        with b.open("a", encoding="utf-8") as fh:
            for record in records[-4:]:
                fh.write(json.dumps(record) + "\n")

    append_turn(1)
    stats = _sync(ns, only_paths={str(b)})          # the live-tail targeted sync
    second = _certificate(ns, key)
    assert second["revision"] > first["revision"] and stats.barrier_sealed >= 1
    ends = {c["path"]: c["completeLineEnd"] for c in second["contributors"]}
    assert ends[str(b)] == b.stat().st_size
    append_turn(2)
    _sync_stopping_after(ns, 0)                     # the hook's budget ends first
    assert _certificate(ns, key) == second          # never withdrawn, never advanced
    assert _required_revision(ns, key) == second["revision"]
    _sync(ns)
    third = _certificate(ns, key)
    assert third["revision"] > second["revision"]
    ends = {c["path"]: c["completeLineEnd"] for c in third["contributors"]}
    assert ends[str(b)] == b.stat().st_size


def test_the_prune_executor_forgets_a_contributor_and_rearms_its_keys(env):
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    keeper = day / _name(20, 9)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    write_rollout(keeper, token_records(1, session_id="keeper-0009",
                                        start_minute=50))
    _sync(ns)
    key = _key(ns, a)
    before = _certificate(ns, key)
    assert _paths(before) == sorted([str(a), str(b)])
    b.unlink()
    _sync(ns)                              # records prune debt, deletes nothing
    _cache().run_codex_prune_drain(wait_for_requests=False)
    conn = ns["open_cache_db"]()
    try:
        members = {r[0] for r in conn.execute(
            "SELECT source_path FROM codex_barrier_membership "
            "WHERE conversation_key=?", (key,))}
    finally:
        conn.close()
    assert members == {str(a)} and _target(ns, b) is None
    assert _required_revision(ns, key) > before["revision"]
    _sync(ns)
    after = _certificate(ns, key)
    assert _paths(after) == [str(a)]
    assert after["thread"]["cwd"] == "/tmp/proj-a"


def test_legacy_prefixes_are_proven_by_the_scan_before_anything_seals(env):
    ns, provider, day, spawned = env
    a, b, c = day / _name(11, 1), day / _name(12, 2), day / _name(13, 3)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    conn = ns["open_cache_db"]()
    try:   # a store ingested before #968: no barrier state at all
        conn.execute("DELETE FROM codex_barrier_membership")
        conn.execute("DELETE FROM codex_barrier_targets")
        conn.execute("DELETE FROM cache_meta WHERE key=?",
                     (_cache().CODEX_BARRIER_TARGETS_INIT_KEY,))
        conn.commit()
    finally:
        conn.close()
    write_rollout(c, conversation_records(1, session_id=SESSION, label="c",
                                          cwd="/tmp/proj-c"))
    _compress(c)
    stats = _sync(ns)
    key = _key(ns, a)
    assert _certificate(ns, key) is None    # A and B are unproven legacy prefixes
    assert stats.barrier_pending >= 1
    assert _cache().CODEX_REPLAY_DRAIN_COMMAND in stats.drain_requested
    assert _cache().run_codex_barrier_membership_scan(
        wait_for_requests=False).proven == 2
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    cert = _certificate(ns, key)
    assert _paths(cert) == sorted([str(a), str(b), str(c)])
    assert cert["thread"]["cwd"] == "/tmp/proj-c"


def test_an_admitted_barrier_write_opens_the_store_before_admission(env):
    """Review N5. `_codex_admitted_cache_write` (every seal, scan, hold and
    provenance write) opens cache.db BEFORE admission takes the cache flocks.
    A crashed repair's stale marker alone is harmless here (the opener clears
    it under the maintenance flock, without the cache flock), but an opener
    that meets a store off its steady state takes the maintenance AND cache
    flocks itself, bounded at 15 s (`bin/_cctally_cache.py:11124-11140`).
    Opened after admission, it waits for the cache flock the admission
    already holds and raises "cache writer is busy"."""
    import threading
    import time
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _sync(ns)
    core = sys.modules["_cctally_core"]
    marker = sys.modules["_cctally_db"]._repair_marker_path(
        pathlib.Path(core.CACHE_DB_PATH))
    # A crashed repair's marker with a malformed owner record, which
    # `_repair_marker_is_live` reports as not live, and a store left out of
    # WAL mode, so the next `open_cache_db` takes the non-steady branch.
    # W9 owns a read keeper after sync; close that test-process handle
    # before constructing a non-WAL store, just as a fresh process would.
    sys.modules["_lib_wal_checkpoint"].finalize()
    marker.write_text("{}")
    raw = sqlite3.connect(str(core.CACHE_DB_PATH))
    try:
        assert raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        raw.close()
    outcome: list[object] = []
    started = time.monotonic()
    worker = threading.Thread(target=lambda: outcome.append(
        _cache().run_codex_barrier_seal_drain(wait_for_requests=False)),
        daemon=True)
    worker.start()
    worker.join(60)
    assert not worker.is_alive(), "the admitted write hung"
    # RED (opened after admission): the opener waits out its 15 s bound on
    # the cache flock admission holds and raises, so nothing is appended.
    assert outcome, "the admitted write raised: it opened the store after admission"
    # timing-budget: the unchanged 10s ceiling rejects the known 15s self-lock while completion, marker and WAL assertions prove recovery.
    assert time.monotonic() - started < 10.0
    assert outcome[0].census != "yielded"
    assert not marker.exists()
    assert _one(ns, "PRAGMA journal_mode")[0] == "wal"


def test_barrier_statements_build_no_history_sized_temp_structures(env):
    """#901 G1 for every barrier statement on the walk, scan and seal paths."""
    import _sql_plan_guard as guard
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    with b.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_meta_only(SESSION, "/tmp/proj-b2")[0]) + "\n")
    with guard.capture_sql_plans() as recorder:
        _sync(ns)
        _sync_stopping_after(ns, 0)
        _cache().run_codex_barrier_membership_scan(wait_for_requests=False)
        _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    findings = [f for f in guard.temp_findings(recorder.statements)
                if "codex_barrier" in f.statement.sql]
    assert findings == [], guard.format_findings(findings)


RATE_LIMITS = {
    "primary": {"resets_at": 1784559600, "used_percent": 13.0,
                "window_minutes": 330},
    "secondary": {"resets_at": 1784937600, "used_percent": 43.0,
                  "window_minutes": 10020},
}


def _with_quota(records: list[dict]) -> list[dict]:
    """The same rollout with a rate-limit reading on every token count, so the
    journal retains quota observations keyed by its logical path (the shape
    `tests/test_codex_orphan_prune_safety.py` already ingests)."""
    out = []
    for rec in json.loads(json.dumps(records)):
        payload = rec.get("payload") or {}
        if rec.get("type") == "event_msg" and payload.get("type") == "token_count":
            payload["info"]["rate_limits"] = RATE_LIMITS
        out.append(rec)
    return out


ACCOUNTING_VIEW = (
    "SELECT source_path, line_offset, timestamp_utc, session_id, model, "
    "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
    "total_tokens, conversation_key, account_key FROM codex_session_entries "
    "ORDER BY source_path, line_offset",
    "SELECT conversation_key, source_path, native_thread_id, root_thread_id, "
    "cwd FROM codex_conversation_threads ORDER BY conversation_key",
)


def _accounting_view(ns):
    conn = ns["open_cache_db"]()
    try:
        return [conn.execute(sql).fetchall() for sql in ACCOUNTING_VIEW]
    finally:
        conn.close()


def _unfixed_prune(ns, monkeypatch):
    """What v1.114.0 did to a compressed store, in both stores: plain-only
    discovery, then the eligible orphan prune of every compressed rollout."""
    leaf = sys.modules["_lib_codex_rollout_container"]
    real = leaf.select_container

    def plain_only(logical, **kwargs):  # #970: the wrapper may pass archive_dir
        selected = real(logical, **kwargs)
        if selected is not None and selected.kind == leaf.PLAIN:
            return selected
        return None

    real_probe = leaf.probe_containers

    def plain_only_probe(logical, **kwargs):
        # #970: the drains prove absence with the strict probe; v1.114.0's
        # drain knew only the plain container, so a compressed one read as
        # absent there too.
        state, selected = real_probe(logical, **kwargs)
        if selected is not None and selected.kind != leaf.PLAIN:
            return "absent", None
        return state, selected

    monkeypatch.setattr(leaf, "select_container", plain_only)
    monkeypatch.setattr(leaf, "probe_containers", plain_only_probe)
    try:
        _sync(ns)
        _cache().run_codex_prune_drain(wait_for_requests=False)
        conv = ns["open_conversations_db"]()
        try:
            ns["sync_codex_conversations"](conv)
        finally:
            conv.close()
        _cache().run_codex_conversation_prune_drain(wait_for_requests=False)
    finally:
        monkeypatch.setattr(leaf, "select_container", real)
        monkeypatch.setattr(leaf, "probe_containers", real_probe)
    # v1.114.0 never wrote the provenance marker (review P1-3), so the store an
    # upgrade meets is unsettled whatever this fixture's first walk recorded.
    # The literal is the durable cache_meta key, not an import, so the helper
    # also runs on the Task 9 tree for the RED.
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM cache_meta WHERE key='codex_history_provenance'")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("heal", ["automatic", "rebuild"])
def test_a_pruned_store_heals_from_the_journal(env, monkeypatch, heal):
    """T5 heal (cache side): rows and attribution come back from retained
    truth; never-decided history stays unattributed; never the live account."""
    ns, provider, day, _ = env
    cc = _cache()
    premech = day / _name(11, 2)
    decided = day / _name(12, 1)
    keeper = day / _name(20, 9)                 # newest: keeps the root recognized
    write_rollout(premech, _with_quota(token_records(
        2, session_id="premech-0002", start_minute=30)))
    with monkeypatch.context() as m:    # pre-#416 history: no decision anywhere
        m.setattr(cc, "_append_codex_file_account_decision", lambda **_kw: None)
        m.setattr(cc, "record_codex_file_account", lambda *_a, **_kw: None)
        _sync(ns)
    root_key = _one(ns, "SELECT source_root_key FROM codex_session_files "
                        "WHERE path=?", (str(premech),))[0]
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_file_incarnations WHERE file_identity=?",
                     (cc.codex_file_key_for_entry_path(root_key, str(premech)),))
        conn.execute("UPDATE codex_session_entries SET account_key=NULL "
                     "WHERE source_path=?", (str(premech),))
        conn.commit()
    finally:
        conn.close()
    write_rollout(decided, token_records(3, session_id="decided-0001"))
    write_rollout(keeper, token_records(1, session_id="keeper-0009",
                                        start_minute=50))
    _sync(ns)
    control = _accounting_view(ns)
    by_path = {}
    for row in control[0]:
        by_path.setdefault(row[0], set()).add(row[-1])
    acct_a = by_path[str(decided)]
    assert len(acct_a) == 1 and None not in acct_a
    assert by_path[str(premech)] == {None}
    _compress(premech)
    _compress(decided)
    _unfixed_prune(ns, monkeypatch)
    pruned = {row[0] for row in _accounting_view(ns)[0]}
    assert pruned == {str(keeper)}, "the unfixed prune deleted both rollouts"
    identity = cc.codex_file_key_for_entry_path(root_key, str(premech))
    conn = ns["open_cache_db"]()
    try:
        # Review P1-3: one production mass prune deleted 289,912 entries, far
        # past the ledger's 50,000-sequence retention, so no ledger trace of
        # the pruned paths survives.
        conn.execute("DELETE FROM codex_accounting_change_log")
        conn.commit()
    finally:
        conn.close()
    # Non-vacuity: the journal is premech's only surviving trace.
    for sql, arg in (
            ("SELECT COUNT(*) FROM codex_file_incarnations WHERE file_identity=?",
             identity),
            ("SELECT COUNT(*) FROM codex_file_accounts WHERE file_identity=?",
             identity),
            ("SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex' "
             "AND source_path=?", str(premech))):
        assert _one(ns, sql, (arg,)) == (0,), sql
    calls: list[str] = []
    real_resolve = cc._resolve_codex_account_for_root

    def spy(provider_root):
        calls.append(str(provider_root))
        return real_resolve(provider_root)

    monkeypatch.setattr(cc, "_resolve_codex_account_for_root", spy)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    stats = _sync(ns, rebuild=heal == "rebuild")
    # The defect (5.11 Attribution, I7): the Task 9 walk reads the pruned
    # premech as first-seen and resolves the live account for it.
    assert calls == [], "the heal consulted the live auth.json"
    assert str(premech) not in {row[0] for row in _accounting_view(ns)[0]}
    assert stats.container_reads_held == 2
    journal = sys.modules["_cctally_journal"]
    assert journal.codex_journal_history_sources([str(premech)])[0] == {
        str(premech)}
    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert (result.outcome, result.candidates, result.traced) == (
        "settled", 2, 1)
    _sync(ns)
    assert calls == [], "the settled walk consulted the live auth.json"
    assert _accounting_view(ns) == control


def test_an_untraced_compressed_rollout_takes_the_plain_decision(
        env, monkeypatch):
    """Review P1-3, the residual: a fresh store settles on its first walk; an
    unsettled store holds an untraced compressed rollout until the drain has
    read the journal, then reads it with the first-ingest decision a plain
    rollout gets (T2 parity)."""
    ns, provider, day, _ = env
    cc = _cache()
    plain = day / _name(11, 1)
    write_rollout(plain, token_records(1, session_id="plain-0001"))
    _sync(ns)
    marker = ("SELECT COUNT(*) FROM cache_meta "
              "WHERE key='codex_history_provenance'")
    # RED on the Task 9 tree: the walk records no provenance state at all.
    assert _one(ns, marker) == (1,), "a store with no journal did not settle"
    conn = ns["open_cache_db"]()
    try:   # the state v1.114.0 leaves: a journal and no marker
        conn.execute("DELETE FROM cache_meta WHERE key='codex_history_provenance'")
        conn.commit()
    finally:
        conn.close()
    fresh = day / _name(12, 2)
    write_rollout(fresh, token_records(2, session_id="fresh-0002",
                                       start_minute=20))
    _compress(fresh)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    stats = _sync(ns)
    assert stats.container_reads_held == 1
    assert str(fresh) not in {row[0] for row in _accounting_view(ns)[0]}
    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert (result.outcome, result.candidates, result.traced) == (
        "settled", 1, 0)
    _sync(ns)
    rows = _accounting_view(ns)[0]
    fresh_accounts = {row[-1] for row in rows if row[0] == str(fresh)}
    plain_accounts = {row[-1] for row in rows if row[0] == str(plain)}
    assert len(fresh_accounts) == 1 and None not in fresh_accounts
    # The live acct-b decision, exactly as a first-seen plain rollout gets it.
    assert fresh_accounts != plain_accounts
    assert _one(ns, "SELECT value FROM cache_meta "
                    "WHERE key='codex_history_provenance'")[0] is not None


@pytest.mark.parametrize("compressed", [False, True])
def test_complete_line_target_counts_malformed_and_non_emitting_lines(env, compressed):
    """A target describes complete raw lines, not only emitted parser facts."""
    ns, provider, day, _ = env
    path = day / _name(11, 1)
    write_rollout(path, conversation_records(1, session_id=SESSION))
    complete = path.read_bytes() + b'not-json\n' + b'{"type":"ignored"}\n'
    raw = complete + b'{"type":"event_msg"'
    path.write_bytes(raw)
    if compressed:
        _compress(path)
    _sync(ns)
    target = _target(ns, path)
    assert target[1:] == ("ready", None, len(complete))
    if compressed:
        cert = _certificate(ns, _key(ns, path))
        assert cert["contributors"][0]["completeLineEnd"] == len(complete)


def _heal_oracle(ns):
    """Stable accounting, quota, thread, resume, and durable decision facts."""
    cc = _cache()
    conn = ns["open_cache_db"]()
    try:
        result = {
            "accounting": conn.execute(ACCOUNTING_VIEW[0]).fetchall(),
            "threads": conn.execute(ACCOUNTING_VIEW[1]).fetchall(),
            "quota": conn.execute(
                "SELECT source_path,line_offset,captured_at_utc,observed_slot,"
                "logical_limit_key,limit_id,limit_name,window_minutes,used_percent,"
                "resets_at_utc,canonical_resets_at_utc,account_key "
                "FROM quota_window_snapshots WHERE source='codex' "
                "ORDER BY source_path,line_offset,observed_slot").fetchall(),
            "cursors": conn.execute(
                "SELECT path,size_bytes,last_byte_offset,last_session_id,last_model,"
                "last_total_tokens,last_native_thread_id,last_root_thread_id,"
                "last_parent_thread_id,last_conversation_key,ingest_complete "
                "FROM codex_session_files ORDER BY path").fetchall(),
            "decisions": conn.execute(
                "SELECT * FROM codex_file_accounts ORDER BY file_identity,"
                "incarnation,from_offset").fetchall(),
        }
        result["decidedIncarnations"] = conn.execute(
            "SELECT i.* FROM codex_file_incarnations i WHERE EXISTS ("
            "SELECT 1 FROM codex_file_accounts a WHERE "
            "a.file_identity=i.file_identity) ORDER BY i.file_identity").fetchall()
        return result
    finally:
        conn.close()


def _journal_records():
    journal = sys.modules["_cctally_journal"]
    hw = journal.journal_safe_high_water()
    assert hw is not None, "the heal oracle requires a nonempty real journal"
    import _lib_journal
    return [_lib_journal.decode_line(raw)
            for _, _, raw in journal.iter_range(None, hw, journal.list_segments())]


def _seed_production_heal(ns, provider, day, monkeypatch, *, retain_incarnation=False):
    """Prune through real store paths; retain journal-only and range truth."""
    cc = _cache()
    premech, decided, sentinel, keeper = [day / _name(hour, suffix)
        for hour, suffix in ((11, 2), (12, 1), (13, 4), (20, 9))]
    write_rollout(premech, _with_quota(conversation_records(
        2, session_id="premech-0002", label="pre", cwd="/tmp/premech")))
    with monkeypatch.context() as m:
        m.setattr(cc, "_append_codex_file_account_decision", lambda **_kw: None)
        m.setattr(cc, "record_codex_file_account", lambda *_a, **_kw: None)
        _sync(ns)
    conn = ns["open_cache_db"]()
    try:
        root = conn.execute("SELECT source_root_key FROM codex_session_files "
                            "WHERE path=?", (str(premech),)).fetchone()[0]
        identity = cc.codex_file_key_for_entry_path(root, str(premech))
        if not retain_incarnation:
            conn.execute("DELETE FROM codex_file_incarnations WHERE file_identity=?",
                         (identity,))
        conn.execute("UPDATE codex_session_entries SET account_key=NULL "
                     "WHERE source_path=?", (str(premech),))
        conn.commit()
    finally:
        conn.close()
    write_rollout(decided, conversation_records(2, session_id="decided-0001"))
    write_rollout(keeper, token_records(1, session_id="keeper-0009", start_minute=50))
    _sync(ns)
    # An explicit unattributed range is a decision, never permission to ask
    # the current login. Its later live append gets a second, nonzero range.
    (provider / "auth.json").write_text('{"OPENAI_API_KEY":"fixture-key"}')
    write_rollout(sentinel, conversation_records(1, session_id="sentinel-0004"))
    _sync(ns)
    first_end = sentinel.stat().st_size
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    with sentinel.open("a", encoding="utf-8") as fh:
        for record in conversation_records(2, session_id="sentinel-0004")[-4:]:
            fh.write(json.dumps(record) + "\n")
    _sync(ns)
    control = _heal_oracle(ns)
    assert all(control.values()), {name: len(rows) for name, rows in control.items()}
    conn = ns["open_cache_db"]()
    try:
        identity = cc.codex_file_key_for_entry_path(root, str(sentinel))
        ranges = cc.load_codex_file_account_ranges(conn, identity,
                    cc.codex_file_incarnation(conn, identity))
    finally:
        conn.close()
    assert ranges[0] == (0, None)
    assert len(ranges) == 2 and ranges[1][0] == first_end and ranges[1][1]
    journal_before = {record["id"] for record in _journal_records() if record}
    for path in (premech, decided, sentinel):
        _compress(path)
    _unfixed_prune(ns, monkeypatch)
    assert {row[0] for row in _accounting_view(ns)[0]} == {str(keeper)}
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_accounting_change_log")
        conn.commit()
    finally:
        conn.close()
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    return control, journal_before


@pytest.mark.parametrize("entrypoint", ["cache-sync", "cache-sync-rebuild", "replay-drain"])
def test_production_heal_entrypoints_preserve_every_retained_family(
        env, monkeypatch, entrypoint):
    """The real CLI/worker paths settle provenance before historical ingest."""
    import argparse
    ns, provider, day, _ = env
    cc = _cache()
    control, journal_before = _seed_production_heal(ns, provider, day, monkeypatch)
    calls = []
    real_resolve = cc._resolve_codex_account_for_root
    def resolve(root):
        calls.append(str(root))
        return real_resolve(root)
    monkeypatch.setattr(cc, "_resolve_codex_account_for_root", resolve)
    if entrypoint == "replay-drain":
        assert cc.cmd_codex_replay_drain_internal(argparse.Namespace()) == 0
    else:
        assert cc.cmd_cache_sync(argparse.Namespace(
            source="codex", rebuild=entrypoint == "cache-sync-rebuild",
            prune_orphans=False, prune_conversations=False)) == 0
    assert calls == [], "a production heal consulted live authentication over history"
    actual = _heal_oracle(ns)
    if actual != control:
        _dump_heal_differences(ns, control, actual)
    assert _heal_oracle(ns) == control
    assert journal_before <= {record["id"] for record in _journal_records() if record}
    assert _one(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?",
                (cc.CODEX_REPLAY_FROM_ZERO_KEY,)) == (0,)
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_certificates")[0] >= 3


@pytest.mark.parametrize("order", ["compressed-first", "plain-first"])
def test_ready_metadata_only_append_advances_with_older_contributor_present(env, order):
    """An eligible new revision selects new metadata, preserving entry facts."""
    ns, provider, day, _ = env
    ah, bh = (11, 12) if order == "compressed-first" else (12, 11)
    a, b = day / _name(ah, 1), day / _name(bh, 2)
    write_rollout(a, conversation_records(1, session_id=SESSION, cwd="/tmp/A"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/B0"))
    _sync(ns)
    key = _key(ns, a)
    first = _certificate(ns, key)
    assert first is not None
    with b.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_meta_only(SESSION, "/tmp/B1")[0]) + "\n")
    _sync(ns, only_paths={str(b)})
    second = _certificate(ns, key)
    assert second["revision"] > first["revision"]
    assert second["thread"]["cwd"] == "/tmp/B1"
    assert _paths(second) == sorted([str(a), str(b)])
    assert {c["path"]: c["completeLineEnd"] for c in second["contributors"]}[str(b)] == b.stat().st_size
    assert _certificate(ns, key) == second
    assert first["thread"]["cwd"] != "/tmp/B1"  # captured prior revision is immutable


def test_an_eligible_nonmax_legacy_thread_survives_equal_certification(env):
    """Certification preserves a legacy retained choice supplied by a member."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(1, session_id=SESSION, cwd="/tmp/A"))
    write_rollout(b, _meta_only(SESSION, "/tmp/B"))
    _sync(ns)
    key = _key(ns, a)
    conn = ns["open_cache_db"]()
    try:
        # A valid retained row from the earlier contributor, the measured
        # legacy case where max-path alone would needlessly change metadata.
        conn.execute("UPDATE codex_conversation_threads SET source_path=?,cwd=? "
                     "WHERE conversation_key=?", (str(a), "/tmp/A", key))
        conn.commit()
        before = conn.execute("SELECT * FROM codex_conversation_threads "
                              "WHERE conversation_key=?", (key,)).fetchone()
    finally:
        conn.close()
    _compress(b)
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key)
    assert cert is not None and cert["thread"]["cwd"] == "/tmp/A"
    assert _one(ns, "SELECT * FROM codex_conversation_threads WHERE conversation_key=?", (key,)) == before
    assert _one(ns, "SELECT * FROM codex_barrier_entry_threads WHERE conversation_key=?", (key,)) == before



def _dump_heal_differences(ns, before, after):
    """Failure-only full-fidelity evidence; the equality oracle is unchanged."""
    quota_columns = ("source_path", "line_offset", "captured_at_utc",
        "observed_slot", "logical_limit_key", "limit_id", "limit_name",
        "window_minutes", "used_percent", "resets_at_utc",
        "canonical_resets_at_utc", "account_key")
    conn = ns["open_cache_db"]()
    try:
        decision_columns = tuple(r[1] for r in conn.execute(
            "PRAGMA table_info(codex_file_accounts)"))
    finally:
        conn.close()
    evidence = {}
    for family, columns, key_columns in (
        ("quota", quota_columns, ("source_path", "line_offset", "observed_slot")),
        ("decisions", decision_columns, ("file_identity", "incarnation", "from_offset")),
    ):
        def named(rows):
            return [dict(zip(columns, row)) for row in rows]
        lhs, rhs = named(before[family]), named(after[family])
        def indexed(rows):
            return {tuple(row[k] for k in key_columns): row for row in rows}
        old, new = indexed(lhs), indexed(rhs)
        evidence[family] = {
            "columns": columns, "keyColumns": key_columns,
            "before": lhs, "after": rhs,
            "missingKeys": sorted(set(old) - set(new)),
            "extraKeys": sorted(set(new) - set(old)),
            "changedRows": [{"key": key, "before": old[key], "after": new[key],
                "differingColumns": [col for col in columns
                                     if old[key][col] != new[key][col]]}
                for key in sorted(set(old) & set(new)) if old[key] != new[key]],
        }
    evidence["journalRecords"] = _journal_records()
    print("SR7_HEAL_DIFF=" + json.dumps(evidence, sort_keys=True, ensure_ascii=True))


@pytest.mark.parametrize("retry", ["per-file", "file-savepoint", "batch-reprepare"])
def test_a_decision_materializes_its_exact_journal_timestamp_after_retry(env, monkeypatch, retry):
    """The same durable timestamp survives buffering and both W11 retry paths."""
    ns, provider, day, _ = env
    cc = _cache()
    path = day / _name(11, 1)
    write_rollout(path, conversation_records(1, session_id=SESSION))
    real_write = cc._write_codex_file_batch
    writes = []
    def write(conn, **kwargs):
        result = real_write(conn, **kwargs)
        writes.append(kwargs["path_str"])
        if retry != "per-file" and len(writes) == 1:
            if retry == "batch-reprepare":
                conn.rollback()  # SQLite can end a transaction on a write error
            raise sqlite3.OperationalError("fixture: retry this prepared file")
        return result
    monkeypatch.setattr(cc, "_write_codex_file_batch", write)
    stats = _sync(ns, **({"budget_seconds": 5.0} if retry == "per-file" else {}))
    assert stats.files_processed == 1 and stats.files_failed == 0
    assert len(writes) == (1 if retry == "per-file" else 2)
    records = [r for r in _journal_records()
               if (r.get("payload") or {}).get("kind") == "codex_file_account"]
    assert len(records) == 1, "a retry must reuse its already durable decision"
    record = records[0]
    payload = record["payload"]
    row = _one(ns, "SELECT from_offset,account_key,decided_at_utc "
                   "FROM codex_file_accounts WHERE file_identity=? AND incarnation=?",
               (payload["file_identity"], payload["incarnation"]))
    assert row == (payload["from_offset"], payload.get("account_key"), record["at"])


def test_journal_quota_precedes_heal_with_a_surviving_incarnation(env, monkeypatch):
    ns, provider, day, _ = env
    cc = _cache()
    control, journal_before = _seed_production_heal(
        ns, provider, day, monkeypatch, retain_incarnation=True)
    premech = day / _name(11, 2)
    root = _one(ns, "SELECT source_root_key FROM codex_session_files LIMIT 1")[0]
    identity = cc.codex_file_key_for_entry_path(root, str(premech))
    assert _one(ns, "SELECT COUNT(*) FROM codex_file_incarnations WHERE file_identity=?",
                (identity,)) == (1,)
    assert _one(ns, "SELECT COUNT(*) FROM codex_file_accounts WHERE file_identity=?",
                (identity,)) == (0,)
    calls = []
    real_resolve = cc._resolve_codex_account_for_root
    def resolve(root):
        calls.append(str(root))
        return real_resolve(root)
    monkeypatch.setattr(cc, "_resolve_codex_account_for_root", resolve)
    held = _sync(ns)
    assert held.container_reads_held == 3, "every cursorless compressed identity must retain its quota hold"
    assert str(premech) not in {row[0] for row in _accounting_view(ns)[0]}
    settled = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert settled.outcome == "settled"
    _sync(ns)
    assert calls == []
    actual = _heal_oracle(ns)
    if actual != control:
        _dump_heal_differences(ns, control, actual)
    assert actual == control
    assert journal_before <= {record["id"] for record in _journal_records() if record}


@pytest.mark.parametrize("refusal", ["fresh-hook-request", "writer-contention"])
def test_quota_provenance_yields_between_identities_and_settles_only_after_retry(
        env, monkeypatch, refusal):
    """Real drain admission may commit one identity, never a partial settlement."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
    # An additional candidate makes per-identity admission observable; only
    # the first candidate has retained quota facts in this journal.
    fresh = day / _name(14, 6)
    write_rollout(fresh, token_records(1, session_id="fresh-candidate"))
    _compress(fresh)
    journal = sys.modules["_cctally_journal"]
    real_iter = journal.iter_range
    scans = []
    def scan(*args, **kwargs):
        scans.append(1)
        return real_iter(*args, **kwargs)
    monkeypatch.setattr(journal, "iter_range", scan)
    completions, held = [], []
    def after_identity():
        completions.append(1)
        if len(completions) != 1:
            return
        if refusal == "fresh-hook-request":
            assert adm.publish_waiting_request(ns["APP_DIR"], root_keys=()) is not None
        else:
            token = cc._cache_store_locks()
            assert token is not None
            held.append(token)
    monkeypatch.setattr(adm, "_after_finish", after_identity)
    try:
        result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
        assert result.outcome == "yielded", "provenance settled despite a per-identity refusal"
        assert result.candidates == 4 and result.traced == 1
        assert _one(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?",
                    (cc.CODEX_HISTORY_PROVENANCE_KEY,)) == (0,)
        # The first identity's canonical quota restoration commits before the
        # second admission, even though the overall pass remains unsettled.
        actual = _heal_oracle(ns)
        assert actual["quota"] == control["quota"]
        assert len(scans) == 1, "a journal pass must not repeat per identity"
    finally:
        for token in held:
            cc._release_cache_store_locks(token)
    if refusal == "fresh-hook-request":
        now = adm._monotonic_ns()
        monkeypatch.setattr(adm, "_monotonic_ns", lambda: now + adm.REQUEST_TTL_NS + 1)
    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert result.outcome == "settled" and len(scans) == 2
    assert _one(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?",
                (cc.CODEX_HISTORY_PROVENANCE_KEY,)) == (1,)
    assert _heal_oracle(ns)["quota"] == control["quota"]


def _commit_historical_prefix(ns, path, monkeypatch):
    """Expire the real walk after one accounting emission, then commit it.

    This uses the parser's stop-before-line and the ordinary file writer;
    no cursor, row, seed or anchor is rewound to manufacture a partial read.
    """
    cc = _cache()
    clock = {"now": 0.0}
    real_iter = cc._iter_codex_fused_records_with_offsets

    def expire_after_entry(stream, source_path, **kwargs):
        for emission in real_iter(stream, source_path, **kwargs):
            yield emission
            if source_path == str(path) and emission.accounting is not None:
                clock["now"] = 10_000.0

    with monkeypatch.context() as m:
        m.setattr(cc, "_walk_clock", lambda: clock["now"])
        m.setattr(cc, "_iter_codex_fused_records_with_offsets", expire_after_entry)
        stats = _sync(ns, budget_seconds=100, quota_reconcile="defer")
    cursor = _cursor_row(ns, path)
    assert stats.budget_exhausted
    assert cursor["ingest_complete"] == 0
    assert 0 < cursor["last_byte_offset"] < cursor["size_bytes"]
    rows = [r for r in _heal_oracle(ns)["accounting"] if r[0] == str(path)]
    assert len(rows) == 1 and rows[0][-1] is None
    assert _one(ns, "SELECT COUNT(*) FROM codex_file_accounts WHERE file_identity=?",
                (cc.codex_file_key_for_entry_path(cursor["source_root_key"],
                                                 str(path)),)) == (0,)
    return cursor


@pytest.mark.parametrize("budgeted_resume", [False, True])
def test_a_committed_historical_prefix_never_decides_its_resumed_backlog(
        env, monkeypatch, budgeted_resume):
    """T10-CD-01: a genuine nonempty stop must not turn old bytes into B's.

    Replacing the incomplete-resume history guard with first-ingest auth
    creates a B range and changes exact accounting/quota/journal families.
    """
    ns, provider, day, _ = env
    control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
    cc = _cache()
    path = day / _name(11, 2)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    # The initial historical prefix is committed while A is logged in.
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    partial = _commit_historical_prefix(ns, path, monkeypatch)
    journal_before = _journal_records()
    incarnations = _one(ns, "SELECT COUNT(*) FROM codex_file_incarnations")[0]
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))

    def no_live_auth(*_args, **_kwargs):
        raise AssertionError("historical resume consulted live auth B")

    with monkeypatch.context() as m:
        m.setattr(cc, "_resolve_codex_account_for_root", no_live_auth)
        _sync(ns, **({"budget_seconds": 100} if budgeted_resume else {}))
    assert _heal_oracle(ns) == control
    assert _cursor_row(ns, path)["last_byte_offset"] > partial["last_byte_offset"]
    assert _one(ns, "SELECT COUNT(*) FROM codex_file_incarnations") == (incarnations,)
    assert _journal_records() == journal_before


def test_growth_during_a_historical_partial_is_new_only_after_its_captured_target(
        env, monkeypatch):
    """T10-CD-01: finishing the captured backlog cannot decide it; a suffix
    appended meanwhile must still take B's legitimate later range."""
    ns, provider, day, _ = env
    control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
    cc = _cache()
    path = day / _name(11, 2)
    cc.run_codex_history_provenance_drain(wait_for_requests=False)
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    partial = _commit_historical_prefix(ns, path, monkeypatch)
    target = partial["size_bytes"]
    journal_before = _journal_records()
    original = _with_quota(conversation_records(
        2, session_id="premech-0002", label="pre", cwd="/tmp/premech"))
    grown = _with_quota(conversation_records(
        3, session_id="premech-0002", label="pre", cwd="/tmp/premech"))
    assert grown[:len(original)] == original
    payload = b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n"
                       for r in grown)
    _replace_container(path.with_name(path.name + ".zst"), payload)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))

    def no_live_auth(*_args, **_kwargs):
        raise AssertionError("growing historical target consulted live auth B")

    with monkeypatch.context() as m:
        m.setattr(cc, "_resolve_codex_account_for_root", no_live_auth)
        # The implicit walk observes the replacement, then real verification
        # adopts it and accounts only the already captured incomplete target.
        _sync(ns)
        result = cc.run_codex_container_verification_drain(wait_for_requests=False)
    assert result.ingested >= 1
    cursor = _cursor_row(ns, path)
    assert (cursor["size_bytes"], cursor["last_byte_offset"],
            cursor["ingest_complete"]) == (target, target, 1)
    assert [r for r in _heal_oracle(ns)["accounting"] if r[0] == str(path)] == [
        r for r in control["accounting"] if r[0] == str(path)]
    assert _journal_records() == journal_before
    identity = cc.codex_file_key_for_entry_path(cursor["source_root_key"], str(path))
    _sync(ns)                              # now the post-target suffix is new
    from test_codex_bounded_ingest import _expected_key
    account_b = _expected_key("acct-b", "b@example.com")
    assert _one(ns, "SELECT from_offset,account_key FROM codex_file_accounts "
                    "WHERE file_identity=?", (identity,)) == (target, account_b)
    rows = [r for r in _heal_oracle(ns)["accounting"] if r[0] == str(path)]
    assert len(rows) == 3
    assert [r[-1] for r in rows] == [None, None, account_b]
    quota = [r for r in _heal_oracle(ns)["quota"] if r[0] == str(path)]
    historical = [r for r in quota if r[1] < target]
    retained = [r for r in control["quota"] if r[0] == str(path) and r[1] < target]
    assert len(retained) == 4
    assert {r[3] for r in retained} == {"primary", "secondary"}
    assert {r[-1] for r in retained} == {_expected_key("acct-a", "a@example.com")}
    assert historical == retained            # all twelve quota fields
    appended = [r for r in quota if r[1] >= target]
    assert len(appended) == 2
    assert {(r[3], r[7], r[8], r[-1]) for r in appended} == {
        ("primary", 330, 13.0, account_b),
        ("secondary", 10020, 43.0, account_b),
    }
    assert _cursor_row(ns, path)["last_byte_offset"] == len(payload)


@pytest.mark.parametrize("already_published", [False, True])
def test_a_pending_generation_cannot_hide_a_new_metadata_only_conversation(
        env, already_published):
    """T10-CD-02: B's old X census cannot prove its unread Y suffix absent.

    Allowing a pending target's old watermark to finish the census seals Y
    from A alone, either creating a wrong certificate or advancing a good one.
    """
    ns, provider, day, _ = env
    b = day / _name(12, 2)
    write_rollout(b, conversation_records(1, session_id=SESSION, cwd="/tmp/x"))
    if already_published:
        prior = day / _name(10, 3)
        write_rollout(prior, conversation_records(
            1, session_id=OTHER_SESSION, cwd="/tmp/y-old"))
        _compress(prior)
    _sync(ns)
    old_x_generation = _target(ns, b)[0]
    old_y = _certificate(ns, _key(ns, prior)) if already_published else None
    with b.open("ab") as fh:
        fh.write(json.dumps(_meta_only(OTHER_SESSION, "/tmp/y-from-b")[0]).encode()
                 + b"\n")
    _compress(b)
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(
        1, session_id=OTHER_SESSION, cwd="/tmp/y-from-a"))
    _compress(a)
    _sync(ns)                              # full implicit census, B pending
    key_y = _key(ns, a)
    assert _target(ns, b)[1:3] == ("required", barrier.PENDING_HOLD)
    assert _target(ns, b)[0] != old_x_generation
    assert _one(ns, "SELECT accepted_generation_token FROM codex_barrier_targets "
                    "WHERE source_path=?", (str(b),)) == (old_x_generation,)
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_membership "
                    "WHERE source_path=? AND conversation_key=?",
                (str(b), key_y)) == (0,)    # unknown Y is the actual defect
    assert _required_revision(ns, key_y) is not None
    assert _certificate(ns, key_y) == old_y, "old membership sealed unread Y"
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    cert = _certificate(ns, key_y)
    assert {str(a), str(b)} <= set(_paths(cert))
    assert cert["exclusions"] == []
    assert cert["thread"]["cwd"] == "/tmp/y-from-b"
    assert cert["thread"]["source_path"] == str(b)
    assert cert["revision"] == _required_revision(ns, key_y)
    if old_y is not None:
        assert cert["revision"] > old_y["revision"]


def _all_quota_values(ns):
    """Every natural-key/value column, excluding only the surrogate row id."""
    conn = ns["open_cache_db"]()
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(quota_window_snapshots)")
                   if row[1] != "id"]
        return conn.execute(
            "SELECT " + ",".join(columns) + " FROM quota_window_snapshots "
            "ORDER BY source,source_path,line_offset,logical_limit_key").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("pending_decision", [False, True])
@pytest.mark.parametrize("entrypoint", ["cache-sync", "replay-drain"])
def test_automatic_quota_heal_equals_same_input_rebuild(
        env, monkeypatch, tmp_path, pending_decision, entrypoint):
    """AC7 compares all quota values after a historical rollout rewrite."""
    import argparse
    import shutil
    ns, provider, day, _ = env
    cc = _cache()
    path = day / _name(11, 2)
    keeper = day / _name(20, 9)
    write_rollout(path, _with_quota(conversation_records(
        2, session_id="premech-0002", label="pre", cwd="/tmp/premech")))
    outside = json.loads(json.dumps(_with_quota(token_records(1, session_id="keeper"))))
    for rec in outside:
        limits = (rec.get("payload", {}).get("info") or {}).get("rate_limits")
        if limits:
            for window in limits.values():
                window["resets_at"] += 120
    write_rollout(keeper, outside)
    _sync(ns)
    original = _all_quota_values(ns)
    _compress(path)
    _unfixed_prune(ns, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        # Durable journal decisions exist, but cache replay has not committed
        # them: the ordinary journal-before-cache interruption boundary.
        if pending_decision:
            root = conn.execute("SELECT source_root_key FROM codex_session_files LIMIT 1").fetchone()[0]
            conn.execute("DELETE FROM codex_file_accounts WHERE file_identity=?",
                         (cc.codex_file_key_for_entry_path(root, str(path)),))
            conn.execute("DELETE FROM cache_meta WHERE key=?", (cc.CODEX_FILE_ACCOUNT_CURSOR_KEY,))
        conn.commit()
    finally:
        conn.close()
    # Retained journal facts came from an older rollout generation. The
    # current bytes have fewer quota observations and changed captures.
    records = _with_quota(conversation_records(
        1, session_id="premech-0002", label="pre", cwd="/tmp/premech"))
    records = json.loads(json.dumps(records))
    records[-1]["timestamp"] = "2026-07-20T12:44:00.000Z"
    for window in records[-1]["payload"]["info"]["rate_limits"].values():
        window["resets_at"] += 60
        window["used_percent"] += 1
    payload = b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n"
                       for r in records)
    _replace_container(path.with_name(path.name + ".zst"), payload)
    app = pathlib.Path(ns["APP_DIR"])
    prepared = tmp_path / "prepared"
    shutil.copytree(app, prepared)
    def command(rebuild):
        return cc.cmd_cache_sync(argparse.Namespace(source="codex", rebuild=rebuild,
            prune_orphans=False, prune_conversations=False))
    if entrypoint == "replay-drain":
        assert cc.cmd_codex_replay_drain_internal(argparse.Namespace()) == 0
    else:
        assert command(False) == 0
    automatic = _all_quota_values(ns)
    assert automatic == original, "journal facts must precede changed raw facts"
    app.rename(tmp_path / "automatic")
    shutil.copytree(prepared, app)
    assert command(True) == 0
    reference = _all_quota_values(ns)
    assert automatic == reference
    assert cc.run_codex_history_provenance_drain(wait_for_requests=False).outcome == "already-settled"
    assert command(False) == 0
    assert _all_quota_values(ns) == reference


# ── #970 T6: census coverage of archive-resident rollouts ───────────────────

def _archived_logical(provider, name: str, day: str) -> pathlib.Path:
    return provider / "sessions" / "2026" / "07" / day / name


def _drop_stores(ns) -> None:
    """`rm cache.db conversations.db`: the journal is the only survivor."""
    core = ns["_cctally_core"]
    for db in (core.CACHE_DB_PATH, core.CONVERSATIONS_DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            target = pathlib.Path(str(db) + suffix)
            if target.exists():
                target.unlink()


def _entry_accounts(ns, path) -> list:
    conn = ns["open_cache_db"]()
    try:
        return [r[0] for r in conn.execute(
            "SELECT account_key FROM codex_session_entries WHERE source_path=? "
            "ORDER BY line_offset", (str(path),))]
    finally:
        conn.close()


def _sync_conversations(ns):
    conv = ns["open_conversations_db"]()
    try:
        return ns["sync_codex_conversations"](conv)
    finally:
        conv.close()


def _conversation_rows(ns, path) -> int:
    conv = ns["open_conversations_db"]()
    try:
        return conv.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE source_path=?", (str(path),)).fetchone()[0]
    finally:
        conv.close()


def test_journaled_decision_replays_for_archived_rollout(env):
    """A rollout decided for account A, then archived and lost from the cache
    (`rm cache.db`), is re-read from the archive under login B. Its journaled
    decision must replay (A), never the live login: the census must cover a
    PLAIN archive-resident rollout, which the compressed-only census skipped,
    letting the first sight take B over retained truth."""
    ns, provider, day, _ = env
    rollout = day / _name(9, 70)
    write_rollout(rollout, token_records(3, session_id="replayed-0070"))
    write_rollout(day / _name(21, 71), token_records(1, session_id="keeper-0071",
                                                     start_minute=50))
    _sync(ns)
    decided = set(_entry_accounts(ns, rollout))
    assert len(decided) == 1 and None not in decided
    archive = provider / "archived_sessions"
    archive.mkdir()
    os.rename(rollout, archive / rollout.name)
    _drop_stores(ns)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    stats = _sync(ns)
    # BASE: the plain archived rollout was never held; it took login B.
    assert _entry_accounts(ns, rollout) == []
    assert stats.container_reads_held >= 1
    result = _cache().run_codex_history_provenance_drain(wait_for_requests=False)
    assert result.outcome == "settled"
    _sync(ns)
    assert set(_entry_accounts(ns, rollout)) == decided
    assert len(_entry_accounts(ns, rollout)) == 3


def test_old_settled_marker_holds_archive_candidates_until_covered(env):
    """G12: a store settled before archives were discovered never traced
    them, so an archive-resident rollout first seen after the upgrade would
    take a first-ingest decision before its journal-retained provenance was
    restored. It must be held, the census must cover it and rewrite the
    marker with archive coverage (keeping the traced paths), while
    sessions-resident rollouts keep settling exactly as before."""
    ns, provider, day, _ = env
    write_rollout(day / _name(21, 72), token_records(1, session_id="keeper-0072",
                                                     start_minute=50))
    _sync(ns)
    legacy = {"journalHighWater": None, "traced": ["/retained/elsewhere.jsonl"]}
    conn = ns["open_cache_db"]()
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES "
                     "('codex_history_provenance', ?)", (json.dumps(legacy),))
        conn.commit()
    finally:
        conn.close()
    sessions_new = day / _name(10, 73)
    write_rollout(sessions_new, token_records(2, session_id="new-0073"))
    _compress(sessions_new)
    archive = provider / "archived_sessions"
    archived = archive / _name(8, 74).replace("07-20", "07-19")
    write_rollout(archived, token_records(3, session_id="arch-0074"))
    _compress(archived)
    logical = _archived_logical(provider, archived.name, "19")
    stats = _sync(ns)
    assert len(_entry_accounts(ns, sessions_new)) == 2      # settled as before
    # BASE: the settled marker classified the archive rollout "new".
    assert _entry_accounts(ns, logical) == []
    assert stats.container_reads_held == 1
    cc = _cache()
    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert (result.outcome, result.candidates) == ("settled", 1)
    marker = json.loads(_one(ns, "SELECT value FROM cache_meta "
                                 "WHERE key='codex_history_provenance'")[0])
    assert marker["archives"] == 1
    assert "/retained/elsewhere.jsonl" in marker["traced"]
    assert marker["journalHighWater"] is None
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "already-settled"
    _sync(ns)
    assert _entry_accounts(ns, logical) == [None, None, None]   # P2


def test_journal_first_recovery_without_dated_parent_precedes_publication(env):
    """Journal-first recovery of an archived rollout whose dated directory is
    gone: the census restores its quota facts before any raw read, and the
    restored identity (now retained with no dated parent) must then be
    ingested and published — the logical-parent witness protected it, so the
    rollout stayed uncounted after its quota history came back."""
    ns, provider, day, _ = env
    rollout = day / _name(9, 75)
    write_rollout(rollout, _with_quota(conversation_records(
        2, session_id="recovered-0075", label="rec", cwd="/tmp/rec")))
    keeper_day = provider / "sessions" / "2026" / "07" / "21"
    keeper_day.mkdir(parents=True)
    write_rollout(keeper_day / _name(9, 76).replace("07-20", "07-21"),
                  token_records(1, session_id="keeper-0076"))
    _sync(ns)
    _sync_conversations(ns)
    control = _entry_accounts(ns, rollout)
    assert control and None not in control
    quota_control = _one(ns, "SELECT COUNT(*) FROM quota_window_snapshots "
                             "WHERE source='codex' AND source_path=?",
                         (str(rollout),))[0]
    assert quota_control > 0
    archive = provider / "archived_sessions"
    archive.mkdir()
    os.rename(rollout, archive / rollout.name)
    _compress(archive / rollout.name)
    day.rmdir()                       # the dated directory is gone
    _drop_stores(ns)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    _sync(ns)
    _sync_conversations(ns)
    assert _entry_accounts(ns, rollout) == []          # held for the census
    result = _cache().run_codex_history_provenance_drain(wait_for_requests=False)
    assert result.outcome == "settled"
    restored = _one(ns, "SELECT COUNT(*) FROM quota_window_snapshots "
                        "WHERE source='codex' AND source_path=?",
                    (str(rollout),))[0]
    assert restored == quota_control                   # before any raw read
    assert _entry_accounts(ns, rollout) == []
    for _ in range(2):
        _sync(ns)
        _sync_conversations(ns)
    # BASE: protected for the missing dated parent, never ingested.
    assert _entry_accounts(ns, rollout) == control     # journaled A replayed
    assert _conversation_rows(ns, rollout) > 0


# ── #982 / #975 T2: the admitted history heal (spec 4.1, 4.3, 4.4, 4.6) ──────
#
# Every test below drives the real executor (`run_codex_admitted_heal`) or the
# real worker body over held history: cursorless compressed rollouts that a
# settled provenance census hands to the heal. The heal record is armed through
# raw SQL where a test needs the fence on its own, so the base binary reaches
# the behavioural assertion rather than a missing helper.

import argparse as _argparse  # noqa: E402
import fcntl as _fcntl  # noqa: E402

HEAL_WALK_KEY = "codex_heal_walk"
#: The record's history-sized half (population, D1 set), bound to the
#: fence key by its `gen` (#982 implementation review).
HEAL_PAYLOAD_KEY = "codex_heal_walk_payload"
WEEK_RESET = 1784937600                         # 2026-07-25T00:00Z
WEEKLY_LIMITS = {
    "primary": {"resets_at": 1784559600, "used_percent": 13.0,
                "window_minutes": 330},
    "secondary": {"resets_at": WEEK_RESET, "used_percent": 20.0,
                  "window_minutes": 10080},
}


class _Killed(BaseException):
    """An injected worker death (never an ordinary exception a worker logs)."""


def _with_weekly_quota(records: list[dict]) -> list[dict]:
    """The rollout with an account-level WEEKLY reading on every token count,
    so window-scoped spend adoption has a window to decide from."""
    out = []
    for rec in json.loads(json.dumps(records)):
        payload = rec.get("payload") or {}
        if rec.get("type") == "event_msg" and payload.get("type") == "token_count":
            payload["info"]["rate_limits"] = WEEKLY_LIMITS
        out.append(rec)
    return out


def _heal_record(ns):
    """The fence key with its payload's population and D1 set merged in."""
    row = _one(ns, "SELECT value FROM cache_meta WHERE key=?", (HEAL_WALK_KEY,))
    if row is None:
        return None
    record = json.loads(row[0])
    payload = _one(ns, "SELECT value FROM cache_meta WHERE key=?",
                   (HEAL_PAYLOAD_KEY,))
    if payload is not None:
        for key, value in json.loads(payload[0]).items():
            if key in ("population", "known"):
                record[key] = value
    return record


def _write_heal_record(ns, fence, payload) -> None:
    """Write both keys of a heal record as raw text or JSON (``None`` leaves
    a key absent)."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM cache_meta WHERE key IN (?, ?)",
                     (HEAL_WALK_KEY, HEAL_PAYLOAD_KEY))
        for key, value in ((HEAL_WALK_KEY, fence), (HEAL_PAYLOAD_KEY, payload)):
            if value is None:
                continue
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                (key, value if isinstance(value, str) else json.dumps(value)))
        conn.commit()
    finally:
        conn.close()


def _arm_heal_record(ns, population) -> None:
    """The fence a start unit writes, armed directly (spec 4.3 shape)."""
    _write_heal_record(
        ns, {"v": 1, "mode": "history", "phase": "units", "gen": "armed"},
        {"v": 1, "gen": "armed",
         "population": sorted(str(p) for p in population)})


def _drop_heal_record(ns) -> None:
    _write_heal_record(ns, None, None)


def _clear_marker(ns, key) -> None:
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM cache_meta WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def _full_walk_marker(ns) -> bool:
    cc = _cache()
    return _one(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?", (
        cc._ingest_frontier.CODEX_FULL_WALK_COMPLETE_KEY,)) == (1,)


class _HealProbe:
    """Observe population commits (the W11 commit trace) and every admission
    finish (``adm._after_finish``), probing both store locks at each finish
    with a non-blocking exclusive flock."""

    def __init__(self, ns, monkeypatch, population, *, on_commit=None,
                 on_finish=None, claude_holder=None):
        import _lib_codex_drain_admission as adm
        core = ns["_cctally_core"]
        self.population = {str(p) for p in population}
        self.events: list = []
        self._locks = (core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH)
        # #1055: a predicate naming the T1 Claude hook as the holder of
        # ``cache.db.lock``. Only that hook may hold the Claude writer lock at
        # a finish; ``cache.db.codex.lock`` must always be free.
        self._claude_holder = claude_holder

        def trace(kind, *args):
            if kind != "commit":
                return
            for path in args[0]:
                self.events.append(("commit", path))
                if on_commit is not None and path in self.population:
                    on_commit(path)

        def after_finish():
            self.events.append(("finish", self.locks_free()))
            if on_finish is not None:
                on_finish()

        monkeypatch.setattr(_cache(), "_CODEX_W11_TRACE", trace)
        monkeypatch.setattr(adm, "_after_finish", after_finish)

    def locks_free(self) -> bool:
        fds = []
        try:
            for path in self._locks:
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                fds.append(fd)
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                except OSError:
                    if (path == self._locks[0]
                            and self._claude_holder is not None
                            and self._claude_holder()):
                        continue
                    return False
            return True
        finally:
            for fd in fds:
                os.close(fd)

    def commits(self) -> list:
        return [i for i, event in enumerate(self.events)
                if event[0] == "commit" and event[1] in self.population]

    def committed(self, path) -> bool:
        return ("commit", str(path)) in self.events

    def assert_admitted_per_file(self) -> None:
        commits = self.commits()
        assert sorted(self.events[i][1] for i in commits) == sorted(
            self.population), ("each population file commits exactly once",
                               self.events)
        for start, end in zip(commits, commits[1:] + [len(self.events)]):
            assert ("finish", True) in self.events[start + 1:end], (
                "no admission finish with both store locks free followed a "
                "population commit: the heal held the writer locks across "
                "files", self.events)


def _settle_and_heal(ns, *, wait_for_requests=True):
    cc = _cache()
    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert result.outcome == "settled", result
    return cc.run_codex_admitted_heal(
        wait_for_requests=wait_for_requests,
        population=cc._codex_history_candidates())


def _split_attribution_rollout(ns, provider, path, session_id) -> None:
    """A rollout whose first range was decided unattributed (an API-key
    login) and whose append was decided for acct-a: unattributed spend plus
    weekly window evidence naming acct-a, in one file."""
    (provider / "auth.json").write_text('{"OPENAI_API_KEY":"fixture-key"}')
    write_rollout(path, _with_weekly_quota(conversation_records(
        2, session_id=session_id, label="split")))
    _sync(ns)
    (provider / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    with path.open("a", encoding="utf-8") as fh:
        for record in _with_weekly_quota(conversation_records(
                3, session_id=session_id, label="split"))[-4:]:
            fh.write(json.dumps(record) + "\n")
    _sync(ns)


def _seed_split_heal(ns, provider, day):
    """P1 (split attribution) as held compressed history after `rm cache.db`;
    the journal is its only surviving trace."""
    cc = _cache()
    p1 = day / _name(11, 31)
    keeper = day / _name(20, 39)
    write_rollout(keeper, token_records(1, session_id="keeper-0039",
                                        start_minute=50))
    _split_attribution_rollout(ns, provider, p1, "split-0031")
    accounts = _entry_accounts(ns, p1)
    assert len(accounts) == 3 and accounts[2] is not None
    _compress(p1)
    _drop_stores(ns)
    _sync(ns)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    assert _entry_accounts(ns, p1) == [], "the census holds P1"
    return p1, keeper


def _hook_log(ns) -> list:
    path = ns["APP_DIR"] / "logs" / "hook-tick.log"
    if not path.exists():
        return []
    return [dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)
            for line in path.read_text().splitlines()]


def _hook_tick(ns) -> None:
    args = _argparse.Namespace(
        explain=False, foreground=True, no_oauth=True, throttle_seconds=None,
        event=None, mock_oauth_response=None, source="codex")
    assert ns["cmd_hook_tick"](args) == 0


@pytest.mark.parametrize("case", ["plain", "yield", "recheck", "hook",
                                  "hook-budget"])
def test_heal_executor_admits_each_population_file(env, monkeypatch, case):
    """R1 (#982): the worker's history heal is admitted per file.

    Detects: one unadmitted walk holding both cache writer flocks across every
    held rollout (the 932 s production hold), a heal unit admitted while a
    hook request is fresh, a unit that skips the after-store-locks recheck,
    and a hook in the yield interval that cannot ingest or report."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    population = [day / _name(h, s) for h, s in ((11, 2), (12, 1), (13, 4))]
    live = day / _name(21, 41)
    if case == "hook-budget":
        core = ns["_cctally_core"]
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-25T12:00:00Z")
        core.CONFIG_PATH.write_text(json.dumps({
            "display": {"tz": "utc"}, "budget": {"codex": {
                "amount_usd": 1000.0, "period": "calendar-month",
                "alerts_enabled": True, "alert_thresholds": [90]}}}) + "\n")
    clock = {"now": int(adm._monotonic_ns())}
    published: list = []
    commit_times: dict = {}
    hooked: list = []
    projections: list = []
    monkeypatch.setattr(adm, "_monotonic_ns", lambda: clock["now"])
    real_projection = ns["reconcile_codex_quota_projection"]

    def projection(**kwargs):
        projections.append(kwargs)
        return real_projection(**kwargs)

    def sleep(seconds):
        if case.startswith("hook") and published and not hooked:
            write_rollout(live, token_records(2, session_id="live-0041"))
            with monkeypatch.context() as m:
                m.setitem(ns, "reconcile_codex_quota_projection", projection)
                _hook_tick(ns)
            hooked.append([f for f in _hook_log(ns) if "sync" in f][-1])
        clock["now"] += int(seconds * 1_000_000_000)

    monkeypatch.setattr(adm, "_sleep", sleep)

    def publish():
        assert adm.publish_waiting_request(ns["APP_DIR"], root_keys=()) is not None
        published.append(clock["now"])
        published.append(len(commit_times))     # commits before the request

    def on_commit(path):
        commit_times[path] = clock["now"]

    def on_finish():
        if case in ("yield", "hook", "hook-budget") and probe.commits() and not published:
            publish()

    def pre_store_locks():
        if case == "recheck" and probe.commits() and not published:
            publish()

    monkeypatch.setattr(cc, "_CODEX_PRE_STORE_LOCK_HOOK", pre_store_locks,
                        raising=False)
    probe = _HealProbe(ns, monkeypatch, population, on_commit=on_commit,
                       on_finish=on_finish)
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    probe.assert_admitted_per_file()
    worker = [f for f in _hook_log(ns) if f.get("op") == "replay-drain"][-1]
    assert worker["result"] == "success", worker
    assert worker.get("heal") == "done" and int(worker["heal_units"]) >= 3, worker
    assert _heal_record(ns) is None
    if case == "plain":
        assert _heal_oracle(ns) == control
        assert _full_walk_marker(ns), "an uninterrupted heal certifies the walk"
        # The worker's log line reports the heal's own counts, summed over
        # its units, not the zeroes of a sync that never ran.
        assert int(worker["files"]) >= 3, worker
        return
    assert published, "the request was never published"
    # The fake clock advances only in the backoff sleep, so order (not time)
    # tells the commits after the request from the one that preceded it.
    later = list(commit_times.values())[published[1]:]
    first = min(commit_times.values())
    assert later, "no population unit committed after the request"
    assert len(commit_times) == 3 and first <= published[0]
    assert all(t >= published[0] + adm.REQUEST_TTL_NS for t in later), (
        "a population unit committed while a hook request was fresh",
        published, commit_times)
    if case.startswith("hook"):
        assert hooked, "no hook ran in the yield interval"
        assert _entry_accounts(ns, live), "the hook did not ingest the live rollout"
        assert projections, "the hook in the yield interval skipped its projection"
        if case == "hook":
            assert hooked[0]["result"] == "success", hooked
        else:
            assert (hooked[0]["sync"], hooked[0]["result"],
                    hooked[0].get("reason")) == (
                "deferred", "noop", "budget_ingest_incomplete"), hooked


def test_heal_fence_holds_population(env, monkeypatch):
    """R3 (a): while the fence holds, only the executor ingests population
    files. Detects: a hook's budgeted sync, an ordinary unbudgeted sync or a
    targeted call ingesting held population out of the single walk's order."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    p2 = day / _name(12, 33)
    write_rollout(p2, token_records(2, session_id="held-0033"))
    _compress(p2)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    _arm_heal_record(ns, (p1, p2))
    live = day / _name(21, 34)
    write_rollout(live, token_records(2, session_id="live-0034"))
    for kwargs in ({"budget_seconds": 60.0}, {}, {"only_paths": {str(p1)}}):
        stats = _sync(ns, maintenance="caller", **kwargs)
        assert _entry_accounts(ns, p1) == [] and _entry_accounts(ns, p2) == [], (
            "a non-executor sync ingested held population", kwargs)
        assert stats.container_reads_held >= 1, kwargs
        assert cc.CODEX_REPLAY_DRAIN_COMMAND in stats.drain_requested, kwargs
        assert not stats.full_walk_complete
        if "only_paths" in kwargs:
            assert not stats.targeted_clean
    assert _entry_accounts(ns, live), "non-population rollouts keep ingesting"
    assert _heal_record(ns)["phase"] == "units"


@pytest.mark.parametrize("driver", ["single-walk", "heal-with-hook"])
def test_heal_fence_defers_adoption_two_accounts(env, monkeypatch, driver):
    """R3 (b): one-way adoption waits for the whole population. P1 holds
    unattributed spend plus weekly evidence for acct-a; P2, later in the walk,
    claims the same window for acct-b. Detects: a per-file unit's own
    settlement, or a hook between the units, stamping P1's spend for acct-a
    before acct-b's claim exists (the single-walk oracle leaves it NULL)."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    p2 = day / _name(12, 32)
    write_rollout(p2, _with_weekly_quota(conversation_records(
        2, session_id="claim-0032", label="b")))
    _compress(p2)
    if driver == "single-walk":
        assert cc.run_codex_history_provenance_drain(
            wait_for_requests=False).outcome == "settled"
        _sync(ns)                     # today's single unadmitted heal walk
    else:
        hooked: list = []

        def on_finish():
            if probe.committed(p1) and not probe.committed(p2) and not hooked:
                hooked.append(_sync(ns, budget_seconds=60.0))

        probe = _HealProbe(ns, monkeypatch, (p1, p2), on_finish=on_finish)
        assert _settle_and_heal(ns).outcome == "done"
        assert hooked, "no hook ran between the population units"
        probe.assert_admitted_per_file()
    claims = set(_entry_accounts(ns, p2))
    assert len(claims) == 1 and None not in claims
    accounts = _entry_accounts(ns, p1)
    assert accounts[:2] == [None, None], (
        "adoption stamped P1's unattributed spend before acct-b's claim "
        "was ingested", accounts)
    assert accounts[2] is not None and accounts[2] not in claims


def test_heal_fence_blocks_direct_settlement(env, monkeypatch):
    """R3 (c): every one-way stamping path checks the fence. Detects: the #980
    executor unit, the unbudgeted settlement or a direct kernel call stamping
    window-derived attribution while a heal holds population."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)

    def on_finish():
        if probe.committed(p1):
            raise _Killed()

    probe = _HealProbe(ns, monkeypatch, (p1,), on_finish=on_finish)
    with pytest.raises(_Killed):
        _settle_and_heal(ns)
    import _lib_codex_drain_admission as adm
    monkeypatch.setattr(adm, "_after_finish", None)
    assert _heal_record(ns)["phase"] == "units"
    accounts = _entry_accounts(ns, p1)
    assert accounts[:2] == [None, None] and accounts[2] is not None
    conn = ns["open_cache_db"]()
    try:
        assert not cc.load_codex_finalization_debt(conn).empty, (
            "the unit left its adoption owed")
    finally:
        conn.close()
    conn = ns["open_cache_db"]()
    locks = cc._cache_store_locks()
    try:
        outcome = cc._codex_settle_finalization(conn, deadline=None)
        assert outcome.fenced and not outcome.adopted
        conn.rollback()
        cc._run_codex_executor_unit(conn, "finalization")
        assert cc.apply_codex_window_spend_adoption(conn) == 0
        with pytest.raises(cc.CodexHealFenced, match="deferred_heal"):
            cc.apply_codex_window_spend_adoption(conn, strict=True)
        with pytest.raises(cc.CodexHealFenced, match="deferred_heal"):
            cc.reconcile_codex_window_attribution_spend(conn, strict=True)
        assert cc.reconcile_codex_window_attribution_spend(conn) == (0, 0)
        conn.rollback()
    finally:
        cc._release_cache_store_locks(locks)
        conn.close()
    cc.settle_codex_finalization_unbudgeted()
    assert _entry_accounts(ns, p1)[:2] == [None, None], "a fenced path stamped"
    # The heal resumes from its record and completes; then the stamp lands.
    assert cc.run_codex_admitted_heal(wait_for_requests=True).outcome == "done"
    assert _heal_record(ns) is None
    assert set(_entry_accounts(ns, p1)) == {accounts[2]}


def test_heal_fence_suspends_change_log_prune(env, monkeypatch):
    """R3 (d): the change-log prune deletes nothing while the fence holds and
    stays owed. Detects: a prune that removes ledger rows the heal's shared
    ledger path set has already read, or a due prune silently dropped."""
    ns, provider, day, _ = env
    cc = _cache()
    for n in range(3):
        write_rollout(day / _name(10, 50 + n),
                      token_records(2, session_id=f"prune-{n:04d}"))
    _sync(ns)
    count_sql = "SELECT COUNT(*) FROM codex_accounting_change_log"
    before = _one(ns, count_sql)[0]
    _arm_heal_record(ns, ())
    conn = ns["open_cache_db"]()
    try:
        assert cc._codex_change_log_prune_due(conn, retain_sequences=1)
        assert cc._prune_codex_accounting_change_log(
            conn, retain_sequences=1) == 0
        conn.commit()
    finally:
        conn.close()
    assert _one(ns, count_sql)[0] == before
    monkeypatch.setattr(cc, "_codex_change_log_prune_due",
                        lambda conn, **kwargs: True)
    _sync(ns, maintenance="caller")
    assert _one(ns, count_sql)[0] >= before
    conn = ns["open_cache_db"]()
    try:
        assert cc.load_codex_owed_maintenance(conn)["change_log_prune"], (
            "a due prune during the fence was not left owed")
        conn.execute("DELETE FROM cache_meta WHERE key IN (?, ?)",
                     (HEAL_WALK_KEY, HEAL_PAYLOAD_KEY))
        conn.commit()
        assert cc._prune_codex_accounting_change_log(
            conn, retain_sequences=1) > 0
    finally:
        conn.close()


def test_heal_thread_rows_joint_timeline(env, monkeypatch):
    """R3 (e): with one hook writing live contributors of conversations X and
    Y between the population units of X and Y, both thread rows end as the
    population rows (the timeline in which the hook ran before the walk).
    Detects: a live contributor replacing a population file's thread row."""
    ns, provider, day, _ = env
    cc = _cache()
    session_x = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a61"
    session_y = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a63"
    write_rollout(day / _name(20, 69), token_records(
        1, session_id="keeper-0069", start_minute=50))
    _sync(ns)
    px, py = day / _name(11, 61), day / _name(13, 63)
    write_rollout(px, conversation_records(2, session_id=session_x,
                                           cwd="/tmp/pop-x", label="x"))
    write_rollout(py, conversation_records(2, session_id=session_y,
                                           cwd="/tmp/pop-y", label="y"))
    _compress(px)
    _compress(py)
    _clear_marker(ns, "codex_history_provenance")
    _sync(ns)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    assert _entry_accounts(ns, px) == [] and _entry_accounts(ns, py) == []
    hooked: list = []

    def on_finish():
        if probe.committed(px) and not probe.committed(py) and not hooked:
            write_rollout(day / _name(14, 64), _meta_only(session_x, "/tmp/live-x"))
            write_rollout(day / _name(15, 65), _meta_only(session_y, "/tmp/live-y"))
            hooked.append(_sync(ns, budget_seconds=60.0))

    probe = _HealProbe(ns, monkeypatch, (px, py), on_finish=on_finish)
    assert _settle_and_heal(ns).outcome == "done"
    assert hooked and hooked[0].files_processed >= 2, "the hook wrote nothing"
    assert _thread(ns, _key(ns, px)) == ("/tmp/pop-x", str(px)), (
        "a live contributor replaced a population file's thread row")
    assert _thread(ns, _key(ns, py)) == ("/tmp/pop-y", str(py))


def test_heal_fence_ends_with_unreadable_population_file(env, monkeypatch):
    """R3 (f): an unreadable population container cannot keep the fence. The
    first clean complete fenced round deletes the record, adoption settles for
    every other row, and the file stays degraded (U6). Detects: a fence that
    waits forever on a file no walk can read."""
    ns, provider, day, _ = env
    p1, _keeper = _seed_split_heal(ns, provider, day)
    bad = day / _name(12, 35)
    (bad.with_name(bad.name + ".zst")).write_bytes(
        b"\x28\xb5\x2f\xfd" + b"not a zstd frame" * 8)
    result = _settle_and_heal(ns)
    assert result.outcome == "done", result
    assert result.degraded >= 1, result
    assert _heal_record(ns) is None, "the fence outlived its pass"
    accounts = _entry_accounts(ns, p1)
    assert len(accounts) == 3 and len(set(accounts)) == 1 and None not in accounts, (
        "adoption did not settle for the readable population", accounts)
    assert _one(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
                (str(bad),)) == (0,)


def test_heal_same_size_replacement_reset_before_adoption(env, monkeypatch):
    """R3 (g): a cursored plain file replaced at the same size after the owed
    pass, whose re-read claims P1's window for acct-b, is reset by a fenced
    certification round before any adoption. Detects: settlement that runs
    before the fenced rounds and stamps P1 from stale evidence."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    q = day / _name(19, 37)
    write_rollout(q, _with_weekly_quota(conversation_records(
        2, session_id="claim-0037", label="q")))
    _sync(ns)
    acct_a = _entry_accounts(ns, q)
    assert len(set(acct_a)) == 1 and None not in acct_a
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    real_sync = cc.sync_codex_cache
    replaced: list = []

    def sync(conn, **kwargs):
        if kwargs.get("_ingest_limit") is not None and not replaced:
            data = q.read_bytes()
            tmp = q.with_name("replacement.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, q)           # same size, new identity
            replaced.append(True)
        return real_sync(conn, **kwargs)

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    assert _settle_and_heal(ns).outcome == "done"
    assert replaced, "the replacement never ran before a certification round"
    claims = set(_entry_accounts(ns, q))
    assert len(claims) == 1 and claims != set(acct_a), (
        "the fenced round did not reset the replaced file", claims)
    accounts = _entry_accounts(ns, p1)
    assert accounts[:2] == [None, None], (
        "adoption ran before the replaced file's evidence was read", accounts)


@pytest.mark.parametrize("kill", ["history-units", "history-transition",
                                  "replay-units",
                                  "replay-start-before-rehydration"])
def test_heal_resume_after_kill(env, monkeypatch, kill):
    """R4: a worker killed mid-heal leaves its record; hooks hold the
    population (history) or decline (replay) and request the worker; a new
    worker resumes and the stores equal an uninterrupted run. In replay, the
    resume never clears again (surviving rows keep their ids), a crash
    between the clear and the rehydration completes the rehydration first,
    and a pre-mechanism rollout stays unattributed after `auth.json`
    switches (D1, I7). Detects: a crash that strands the fence, releases
    held population to out-of-order writers, re-clears a replay or
    recaptures its D1 snapshot from a partial store, or diverges."""
    if kill.startswith("replay"):
        return _replay_resume_after_kill(env, monkeypatch, kill)
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    population = [day / _name(h, s) for h, s in ((11, 2), (12, 1), (13, 4))]

    def on_finish():
        done = len(probe.commits())
        if kill == "history-units" and done == 1:
            raise _Killed()
        if (kill == "history-transition" and done == 3
                and _heal_record(ns) is None):
            raise _Killed()

    probe = _HealProbe(ns, monkeypatch, population, on_finish=on_finish)
    with pytest.raises(_Killed):
        cc.cmd_codex_replay_drain_internal(_argparse.Namespace())
    monkeypatch.setattr(adm, "_after_finish", None)
    if kill == "history-units":
        assert _heal_record(ns)["phase"] == "units"
        stats = _sync(ns, budget_seconds=60.0, maintenance="caller")
        assert cc.CODEX_REPLAY_DRAIN_COMMAND in stats.drain_requested
        assert [_entry_accounts(ns, p) == [] for p in population[1:]] == [
            True, True], "a hook ingested held population after the crash"
    else:
        assert _heal_record(ns) is None
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    cc.run_codex_prune_drain(wait_for_requests=False)
    assert _heal_record(ns) is None
    actual = _heal_oracle(ns)
    if actual != control:
        _dump_heal_differences(ns, control, actual)
    assert actual == control


@pytest.mark.parametrize("arrival", ["two-late", "growing", "new-files"])
def test_heal_walk_piece_ingests_one_file(env, monkeypatch, arrival):
    """R8 (#982 spec 4.1 steps 4 and 7): each piece of the fenced and the
    closing walk ingests or resets at most one file, and a walk ends within
    one piece more than its census has files, whatever arrives meanwhile.
    Detects: a piece that re-creates a whole-walk hold over late files, a
    piece that ingests a visited file's appends beside the next file (two
    reads in one hold), and files created after the census that keep a walk
    open forever."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    grows = [day / _name(15, 45), day / _name(15, 48)]
    lates = [day / _name(16, 46), day / _name(17, 47)]
    arrived: list = []
    if arrival == "growing":
        for n, path in enumerate(grows):
            write_rollout(path, token_records(2, session_id=f"grow-{n:04d}"))
        _sync(ns)
    initial = {path: _cursor_row(ns, path)["last_byte_offset"]
               for path in grows if arrival == "growing"}
    real_sync = cc.sync_codex_cache
    pieces: list = []

    def sync(conn, **kwargs):
        piece = kwargs.get("_ingest_limit") is not None
        if piece:
            assert len(pieces) < 40, ("a heal walk never ended", pieces)
            if arrival == "two-late" and not pieces:
                for n, path in enumerate(lates):
                    write_rollout(path, token_records(
                        2, session_id=f"late-{n:04d}"))
            elif arrival == "growing":
                for n, path in enumerate(grows):
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(token_records(
                            1, session_id=f"grow-{n:04d}",
                            start_minute=10 + len(pieces))[-1]) + "\n")
            elif arrival == "new-files":
                for n in range(2):
                    path = day / _name(18, 200 + len(arrived))
                    write_rollout(path, token_records(
                        2, session_id=f"new-{len(arrived):04d}"))
                    arrived.append(path)
        stats = real_sync(conn, **kwargs)
        if piece:
            pieces.append({
                "fenced": bool(kwargs.get("_defer_finalization")),
                "processed": stats.files_processed,
                "total": stats.files_total,
                "complete": getattr(stats, "heal_walk_complete", None)})
        return stats

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    result = _settle_and_heal(ns)
    assert result.outcome == "done", (result, pieces)
    assert _heal_record(ns) is None
    assert all(p["processed"] <= 1 for p in pieces), (
        "a walk piece ingested or reset more than one file", pieces)
    fenced = [p for p in pieces if p["fenced"]]
    closing = [p for p in pieces if not p["fenced"]]
    for walk in (fenced, closing):
        # The census is the first piece's discovery set.
        assert walk and len(walk) <= walk[0]["total"] + 1, (
            "a walk took more pieces than its census allows", pieces)
        assert walk[-1]["complete"] and not any(
            p["complete"] for p in walk[:-1]), pieces
    if arrival == "two-late":
        assert [p["processed"] for p in fenced] == [1, 1], pieces
        assert [p["processed"] for p in closing] == [0], pieces
        assert _full_walk_marker(ns), "the heal ended without its certificate"
        for path in lates:
            assert _entry_accounts(ns, path)
    elif arrival == "growing":
        for path in grows:
            assert _cursor_row(ns, path)["last_byte_offset"] > initial[path], (
                "a growing file was never read", pieces)
    else:
        # Files created after the closing walk's census are left to later
        # syncs: cursorless after the heal, ingested by the next ordinary one.
        cursorless = [path for path in arrived if _one(
            ns, "SELECT 1 FROM codex_session_files WHERE path=?",
            (str(path),)) is None]
        assert cursorless, ("every arrival was read inside the heal", pieces)
        monkeypatch.setattr(cc, "sync_codex_cache", real_sync)
        _sync(ns)
        for path in arrived:
            assert _entry_accounts(ns, path), path


def test_heal_publication_between_units_waits_for_both_files(env, monkeypatch):
    """R10 (intermediate publication): a conversation pass between the two
    population units of one two-file conversation publishes nothing for it,
    and publishes once both files are accounted. Detects: a per-file heal
    exposing a half-ingested conversation to readers."""
    ns, provider, day, _ = env
    cc = _cache()
    session = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a81"
    write_rollout(day / _name(20, 89), token_records(
        1, session_id="keeper-0089", start_minute=50))
    pa, pb = day / _name(11, 81), day / _name(13, 83)
    write_rollout(pa, conversation_records(2, session_id=session, label="a"))
    write_rollout(pb, conversation_records(1, session_id=session, label="b"))
    _sync(ns)
    _sync_conversations(ns)
    assert _conversation_rows(ns, pa) > 0 and _conversation_rows(ns, pb) > 0
    _compress(pa)
    _compress(pb)
    _drop_stores(ns)
    _sync(ns)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    seen: list = []

    def on_finish():
        if probe.committed(pa) and not probe.committed(pb) and not seen:
            _sync_conversations(ns)
            seen.append((_conversation_rows(ns, pa), _conversation_rows(ns, pb)))

    probe = _HealProbe(ns, monkeypatch, (pa, pb), on_finish=on_finish)
    assert _settle_and_heal(ns).outcome == "done"
    assert seen == [(0, 0)], (
        "a conversation published before every contributor was accounted", seen)
    _sync_conversations(ns)
    assert _conversation_rows(ns, pa) > 0 and _conversation_rows(ns, pb) > 0


def _fail_container(cc, monkeypatch, path):
    """``path`` fails persistently through the cache walk's file-failure
    path: every write of its batch raises a (non-corruption) database error,
    so its one retry fails too and the walk counts it in `files_failed`
    (never a U6 skip). Discovery, provenance and the barrier scan still see
    the file. Returns the undo."""
    real = cc._write_codex_file_batch

    def write(conn, *args, **kwargs):
        if str(kwargs.get("path_str")) == str(path):
            raise sqlite3.OperationalError("injected write failure")
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(cc, "_write_codex_file_batch", write)
    return lambda: monkeypatch.setattr(cc, "_write_codex_file_batch", real)


def test_heal_fence_seals_nothing(env, monkeypatch):
    """R3 (h): no sync seals barrier keys while the fence holds, and after a
    transition over a failed contributor the census blocks new seals. P1 and
    P2 are the two population contributors of one conversation; P1's unit
    commits and P2 then fails persistently. Detects: a per-file unit sealing
    the conversation from P1 alone under a census set before the fence, which
    a pass already past its entry check would publish half, and which P2's
    later failure would never withdraw."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    session = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a91"
    write_rollout(day / _name(20, 99), token_records(
        1, session_id="keeper-0099", start_minute=50))
    pa, pb = day / _name(11, 91), day / _name(13, 93)
    write_rollout(pa, conversation_records(2, session_id=session, label="a"))
    write_rollout(pb, conversation_records(1, session_id=session, label="b"))
    _sync(ns)
    _sync_conversations(ns)
    _compress(pa)
    _compress(pb)
    _drop_stores(ns)
    _sync(ns)
    cc.run_codex_inventory_bootstrap(wait_for_requests=False)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    population = cc._codex_history_candidates()
    assert {str(pa), str(pb)} <= set(population)
    restore = _fail_container(cc, monkeypatch, pb)
    seen: list = []

    def on_finish():
        if seen or not probe.committed(pa) or _heal_record(ns) is None:
            return
        key = _key(ns, pa)
        # A pass that entered before the fence: its entry check has passed.
        real_pending = cc._cache_side_history_heal_pending
        cc._cache_side_history_heal_pending = lambda conn: False
        try:
            stats = _sync_conversations(ns)
        finally:
            cc._cache_side_history_heal_pending = real_pending
        seen.append((key, _certificate(ns, key) is not None,
                     _conversation_rows(ns, pa), stats.deferred_reason))

    probe = _HealProbe(ns, monkeypatch, (pa, pb), on_finish=on_finish)
    result = cc.run_codex_admitted_heal(wait_for_requests=True,
                                        population=population)
    monkeypatch.setattr(adm, "_after_finish", None)
    assert seen, "no conversation pass ran inside the fence"
    key, sealed, published, _reason = seen[0]
    assert not sealed, ("a unit sealed a conversation under the fence", seen)
    assert published == 0, (
        "a pass past its entry check published from one contributor", seen)
    assert result.outcome == "done", result
    assert _heal_record(ns) is None
    assert _certificate(ns, key) is None, (
        "a conversation with a failed contributor was sealed after the "
        "transition")
    _sync_conversations(ns)
    assert _conversation_rows(ns, pa) == 0
    # Control: once P2 reads, the conversation seals and publishes whole.
    restore()
    _sync(ns)
    assert _certificate(ns, key) is not None
    _sync_conversations(ns)
    assert _conversation_rows(ns, pa) > 0 and _conversation_rows(ns, pb) > 0


def test_heal_history_transition_with_failed_file(env, monkeypatch):
    """#982 spec 4.1 step 5: a persistently failing population file cannot
    hold the fence. The fenced walk's last piece deletes the history record,
    adoption settles for the readable rows, and the failed file stays owed
    exactly as the single walk leaves it (census 0, no full-walk
    certificate). Detects: a fence that waits forever on a failed file."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    bad = day / _name(12, 36)
    write_rollout(bad, token_records(2, session_id="bad-0036"))
    _compress(bad)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    population = cc._codex_history_candidates()
    assert str(bad) in population
    _fail_container(cc, monkeypatch, bad)
    result = cc.run_codex_admitted_heal(wait_for_requests=True,
                                        population=population)
    assert _heal_record(ns) is None, ("the fence outlived a failed file", result)
    assert result.outcome == "done", result
    accounts = _entry_accounts(ns, p1)
    assert len(accounts) == 3 and len(set(accounts)) == 1 and None not in accounts, (
        "adoption did not settle for the readable population", accounts)
    assert _entry_accounts(ns, bad) == []
    assert _one(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
                (str(bad),)) == (0,), "the failed file is no longer owed"
    assert _one(ns, "SELECT value FROM cache_meta WHERE key=?",
                (cc.CODEX_BARRIER_CENSUS_KEY,)) == ("0",)
    assert not _full_walk_marker(ns)


# ── #982 / #975 T3: the byte-zero replay through the executor (spec 4.5) ─────
#
# An armed replay (`codex_replay_from_zero_pending`) is cleared and refilled
# by the heal executor: a start unit (D1 snapshot, preflight, the clear that
# writes the replay record, the authoritative rehydration), then the same
# admitted population units, fenced walk, transition, finalization units and
# closing walk as a history heal. The marker is armed through raw SQL, as the
# migrations arm it.


def _arm_replay(ns) -> None:
    conn = ns["open_cache_db"]()
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, '1')",
                     (_cache().CODEX_REPLAY_FROM_ZERO_KEY,))
        conn.commit()
    finally:
        conn.close()


def _replay_armed(ns) -> bool:
    return _one(ns, "SELECT COUNT(*) FROM cache_meta WHERE key=?",
                (_cache().CODEX_REPLAY_FROM_ZERO_KEY,)) == (1,)


def _meta_value(ns, key):
    row = _one(ns, "SELECT value FROM cache_meta WHERE key=?", (key,))
    return None if row is None else row[0]


def _entry_rowids(ns, path) -> list:
    conn = ns["open_cache_db"]()
    try:
        return [row[0] for row in conn.execute(
            "SELECT rowid FROM codex_session_entries WHERE source_path=? "
            "ORDER BY rowid", (str(path),))]
    finally:
        conn.close()


def _seed_replay(ns, day, count=3):
    """``count`` ingested plain rollouts, then an armed replay over them."""
    paths = [day / _name(11 + n, 71 + n) for n in range(count)]
    for n, path in enumerate(paths):
        write_rollout(path, conversation_records(
            2, session_id=f"replay-{n:04d}", label=f"r{n}"))
    _sync(ns)
    control = _heal_oracle(ns)
    _arm_replay(ns)
    return paths, control


def _replay_hook(ns, monkeypatch):
    """One real Codex hook tick, observing its projection, budget and
    worker-request legs; returns its lifecycle line, the calls and the
    replay-deferral record it left."""
    cc = _cache()
    journal = sys.modules["_cctally_journal"]
    calls = {"projection": 0, "budget": 0, "drain": 0}
    real_projection = ns["reconcile_codex_quota_projection"]
    real_ingest = journal.run_stats_ingest

    def projection(**kwargs):
        calls["projection"] += 1
        return real_projection(**kwargs)

    def ingest(*args, **kwargs):
        if kwargs.get("codex_apply") is not None:
            calls["budget"] += 1
        return real_ingest(*args, **kwargs)

    def defer():
        calls["drain"] += 1
        return "throttled"

    with monkeypatch.context() as m:
        m.setitem(ns, "reconcile_codex_quota_projection", projection)
        m.setattr(journal, "run_stats_ingest", ingest)
        m.setattr(cc, "_defer_codex_replay_drain", defer)
        _hook_tick(ns)
    line = [f for f in _hook_log(ns) if "sync" in f][-1]
    return line, calls, _meta_value(ns, cc.CODEX_REPLAY_DEFERRED_KEY)


@pytest.mark.parametrize("case", ["plain", "yield", "recheck", "hook",
                                  "contended"])
def test_replay_refill_admits_each_file(env, monkeypatch, case):
    """R2 (#982): the worker's byte-zero replay is a start unit followed by
    one admitted commit per refilled file, each followed by a finish with both
    store locks free.

    Detects: the replay's single unadmitted clear-and-walk holding both cache
    writer flocks across every rollout, a refill unit admitted while a hook
    request is fresh or skipping the after-store-locks recheck, a hook during
    the refill that ingests out of order, projects or evaluates a budget over
    the cleared, partly refilled store, or reports success, and a hook that
    meets a refill unit's locks and publishes a waiting request: every tick
    declines during the refill, so the request only made the worker idle for
    its 60 s lifetime (measured: 71 such waits, 4,265 s of a 6,045 s replay)."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    population, control = _seed_replay(ns, day)
    clock = {"now": int(adm._monotonic_ns())}
    published: list = []
    commit_times: dict = {}
    hooked: list = []
    at_first_commit: list = []
    slept: list = []
    requests_after_hook: list = []
    monkeypatch.setattr(adm, "_monotonic_ns", lambda: clock["now"])

    def sleep(seconds):
        slept.append(seconds)
        if case == "hook" and published and not hooked:
            hooked.append(_replay_hook(ns, monkeypatch))
        clock["now"] += int(seconds * 1_000_000_000)

    monkeypatch.setattr(adm, "_sleep", sleep)

    def publish():
        assert adm.publish_waiting_request(ns["APP_DIR"], root_keys=()) is not None
        published.append(clock["now"])
        published.append(len(commit_times))

    def on_commit(path):
        if not at_first_commit:
            at_first_commit.append(_heal_record(ns))
        commit_times[path] = clock["now"]
        if case == "contended" and not hooked:
            # The unit still holds both store locks here, so the hook's
            # non-blocking acquisition meets them.
            hooked.append(_replay_hook(ns, monkeypatch))
            requests_after_hook.append(len(adm.fresh_requests(ns["APP_DIR"])))

    def on_finish():
        if case in ("yield", "hook") and probe.commits() and not published:
            publish()

    def pre_store_locks():
        if case == "recheck" and probe.commits() and not published:
            publish()

    monkeypatch.setattr(cc, "_CODEX_PRE_STORE_LOCK_HOOK", pre_store_locks,
                        raising=False)
    probe = _HealProbe(ns, monkeypatch, population, on_commit=on_commit,
                       on_finish=on_finish)
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    probe.assert_admitted_per_file()
    record = at_first_commit[0]
    assert record is not None and (record["mode"], record["phase"],
                                   record["rehydrated"]) == (
        "replay", "units", True), ("the refill began before the start unit "
                                   "committed its clear and rehydration", record)
    assert set(record["population"]) == {str(p) for p in population}
    worker = [f for f in _hook_log(ns) if f.get("op") == "replay-drain"][-1]
    assert worker["result"] == "success" and worker.get("heal") == "done", worker
    assert not _replay_armed(ns) and _heal_record(ns) is None
    if case == "contended":
        assert hooked, "no hook ran while a refill unit held the store locks"
        line, calls, _deferral = hooked[0]
        assert (line["sync"], line["result"], line.get("reason")) == (
            "contended", "noop", "replay_pending"), line
        assert line.get("drain_request") is None, line
        assert requests_after_hook == [0], (
            "the contended hook published a waiting request that the refill "
            "could only wait out", requests_after_hook)
        assert calls["projection"] == 0 and calls["budget"] == 0, calls
        assert calls["drain"] >= 1, "the hook did not request the worker"
        assert not slept, ("the refill waited for a request no tick could use",
                           slept)
    if case in ("plain", "contended"):
        assert _heal_oracle(ns) == control
        assert _full_walk_marker(ns), "an uninterrupted replay certifies the walk"
        return
    assert published, "the request was never published"
    later = list(commit_times.values())[published[1]:]
    assert later, "no refill unit committed after the request"
    assert len(commit_times) == 3 and min(commit_times.values()) <= published[0]
    assert all(t >= published[0] + adm.REQUEST_TTL_NS for t in later), (
        "a refill unit committed while a hook request was fresh",
        published, commit_times)
    if case == "hook":
        assert hooked, "no hook ran during the refill"
        line, calls, deferral = hooked[0]
        assert (line["sync"], line["result"], line.get("reason")) == (
            "deferred", "noop", "replay_pending"), line
        assert calls["projection"] == 0 and calls["budget"] == 0, (
            "the hook read the cleared, partly refilled store", calls)
        assert calls["drain"] >= 1, "the hook did not request the worker"
        assert deferral is not None, "the declining hook recorded no deferral"


def _claude_assistant_line(n: int) -> str:
    """One priced Claude assistant line for the #1055 hook transcript."""
    return json.dumps({
        "type": "assistant", "sessionId": "claude-1055",
        "timestamp": f"2026-07-20T12:{n:02d}:00.000Z",
        "requestId": f"req_1055_{n}", "cwd": "/work/claude-1055",
        "message": {"id": f"msg_1055_{n}", "model": "claude-opus-5-5",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    })


def _claude_rows(ns, path) -> tuple:
    """(session_entries rows, cursor offset) for one Claude transcript."""
    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM session_entries "
                            "WHERE source_path=?", (str(path),)).fetchone()[0]
        cursor = conn.execute("SELECT last_byte_offset FROM session_files "
                              "WHERE path=?", (str(path),)).fetchone()
    finally:
        conn.close()
    return rows, None if cursor is None else cursor[0]


def _exclusive_free(path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


@pytest.mark.parametrize("mode", ["history", "replay"])
def test_contended_claude_hook_ingests_between_admitted_units(
        env, monkeypatch, mode):
    """A1 (#1055): a routine Claude hook that meets an admitted Codex drain
    unit's writer lock registers as the Claude waiter, the executor yields its
    next admission to it, and the hook ingests its new line within its bound.

    Detects: a Claude hook that only makes one non-blocking attempt and so
    ends ``lock_contended`` behind back-to-back heal or refill units (Claude
    spend reaching cache.db late, #1055); a drain that admits a unit past a
    registered waiter; a waiter released before the Claude writer unlock; and
    a yield that changes the heal's or the replay's Codex end state."""
    import threading
    import time as _time
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    core = ns["_cctally_core"]
    record = sys.modules["_cctally_record"]
    transcript = (ns["_get_claude_data_dirs"]()[0] / "projects" / "p1055"
                  / "claude-1055.jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_claude_assistant_line(0) + "\n")
    conn = ns["open_cache_db"]()
    try:
        assert not ns["sync_cache"](conn).lock_contended
    finally:
        conn.close()
    assert _claude_rows(ns, transcript)[0] == 1, "the seed line was not ingested"
    if mode == "history":
        control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
        cc.run_codex_inventory_bootstrap(wait_for_requests=False)
        population = [day / _name(h, s) for h, s in ((11, 2), (12, 1), (13, 4))]
    else:
        population, control = _seed_replay(ns, day)

    hook: dict = {"ident": None, "refused_at": None, "first_free": False,
                  "acquired_at": None, "holding": False, "registered": False,
                  "released": False, "stats": [], "exc": None, "rc": None}
    refused, registered = threading.Event(), threading.Event()
    events: list = []                   # one ordered trace; append is atomic
    schedule = {"started": False, "registration": None}

    def on_hook_thread() -> bool:
        return threading.get_ident() == hook["ident"]

    real_acquire = cc._acquire_cache_flock

    def admitted_after_release() -> bool:
        trace = list(events)
        if ("unit-released",) not in trace:
            return False
        later = trace[trace.index(("unit-released",)) + 1:]
        return any(e[0] == "admit" for e in later)

    def acquire(lock_fh, **kwargs):
        got = real_acquire(lock_fh, **kwargs)
        if on_hook_thread() and getattr(lock_fh, "name", None) == str(
                core.CACHE_LOCK_PATH):
            now = _time.monotonic()
            if hook["refused_at"] is None and not hook["first_free"]:
                if got:
                    hook["first_free"] = True
                else:
                    hook["refused_at"] = now
                refused.set()
            if got:
                hook["acquired_at"] = now
                hook["holding"] = True
                events.append(("hook-acquired",))
                # Hold the acquired lock until the executor has attempted its
                # next admission, so that attempt certainly meets the
                # registered waiter (bounded; the hook's sync is only later).
                end = _time.monotonic() + 2.0
                while _time.monotonic() < end and not admitted_after_release():
                    _time.sleep(0.01)
        return got

    monkeypatch.setattr(cc, "_acquire_cache_flock", acquire)
    waiter_cls = getattr(adm, "ClaudeWaiter", None)
    if waiter_cls is not None:          # the base tree has no waiter at all
        real_register = waiter_cls.register.__func__
        real_release = waiter_cls.release

        def register(cls, app_dir, **kwargs):
            waiter = real_register(cls, app_dir, **kwargs)
            if waiter is not None and on_hook_thread():
                hook["registered"] = True
                events.append(("registered",))
                registered.set()
            return waiter

        def release(self):
            if on_hook_thread() and hook["registered"] and not hook["released"]:
                hook["released"] = True
                events.append(("waiter-release",
                               _exclusive_free(core.CACHE_LOCK_PATH)))
            hook["holding"] = False
            return real_release(self)

        monkeypatch.setattr(waiter_cls, "register", classmethod(register))
        monkeypatch.setattr(waiter_cls, "release", release)
    real_admit = adm.admit

    def admit(*args, **kwargs):
        result = real_admit(*args, **kwargs)
        events.append(("admit", result.admitted))
        return result

    monkeypatch.setattr(adm, "admit", admit)
    real_sync = ns["sync_cache"]

    def sync(conn, **kwargs):
        stats = real_sync(conn, **kwargs)
        if on_hook_thread():
            hook["stats"].append(stats)
            events.append(("hook-synced", stats.lock_contended))
        return stats

    monkeypatch.setitem(ns, "sync_cache", sync)
    monkeypatch.setattr(record, "_hook_tick_read_stdin_event",
                        lambda *a, **k: {
                            "event": "Stop", "session_id": "claude-1055",
                            "transcript_path": str(transcript),
                            "cwd": "/work/claude-1055"})

    def run_hook():
        hook["ident"] = threading.get_ident()
        try:
            hook["rc"] = ns["cmd_hook_tick"](_argparse.Namespace(
                explain=False, foreground=True, no_oauth=True,
                throttle_seconds=None, event=None, mock_oauth_response=None,
                source=None))
        except BaseException as exc:  # reported as its own failure below
            hook["exc"] = exc

    thread = threading.Thread(target=run_hook, name="claude-hook-1055",
                              daemon=True)

    def on_commit(path):
        events.append(("commit", path))
        if schedule["started"]:
            return
        schedule["started"] = True
        # The unit still holds both store locks and the gate here.
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write(_claude_assistant_line(1) + "\n")
        thread.start()
        if not refused.wait(20):
            schedule["registration"] = "no-first-attempt"
            return
        end = _time.monotonic() + 20
        while (not registered.is_set() and thread.is_alive()
               and _time.monotonic() < end):
            registered.wait(0.01)
        schedule["registration"] = (
            "registered" if registered.is_set()
            else "hook-ended" if not thread.is_alive() else "timeout")
        events.append(("unit-released",))

    probe = _HealProbe(ns, monkeypatch, population, on_commit=on_commit,
                       claude_holder=lambda: hook["holding"])
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    if schedule["started"]:
        thread.join(30)
    assert schedule["started"], "no population unit committed: nothing paused"
    assert not thread.is_alive(), "the Claude hook thread never finished"
    assert hook["exc"] is None, ("the Claude hook thread raised", hook["exc"])
    assert hook["rc"] == 0, hook["rc"]
    assert schedule["registration"] != "no-first-attempt", (
        "the hook never attempted cache.db.lock while the unit held it")
    assert not hook["first_free"], (
        "the hook's first cache.db.lock attempt was not refused: the paused "
        "unit did not hold the Claude writer lock")
    assert schedule["registration"] != "timeout", (
        "timed out waiting for the hook's waiter registration")
    assert hook["stats"], "the hook never returned from sync_cache"
    rows, offset = _claude_rows(ns, transcript)
    assert not hook["stats"][0].lock_contended and rows == 2, (
        "the contended Claude hook ended lock_contended and its new line was "
        "not ingested between admitted units",
        hook["stats"][0].lock_contended, rows)
    # GREEN: acquired inside its bound, ingested once, cursor advanced.
    assert offset == transcript.stat().st_size, "the hook's cursor did not advance"
    assert len(hook["stats"]) == 1, "the hook synced more than once"
    assert schedule["registration"] == "registered", schedule
    assert (hook["acquired_at"] - hook["refused_at"]
            < adm.CLAUDE_HOOK_DRAIN_WAIT_SECONDS), hook
    reg = events.index(("registered",))
    acquired = events.index(("hook-acquired",))
    release = next(i for i, e in enumerate(events) if e[0] == "waiter-release")
    assert reg < events.index(("unit-released",)) < acquired < release, events
    assert events[release][1] is True, (
        "the waiter was released while the hook still held cache.db.lock",
        events)
    during = events[reg:release]
    admits = [e for e in during if e[0] == "admit"]
    assert admits and not any(e[1] for e in admits), (
        "the executor admitted a unit while the Claude waiter was registered",
        events)
    assert not [e for e in during if e[0] == "commit"], (
        "a population unit committed before the waiting hook ingested", events)
    assert [e for e in events[release:] if e[0] == "commit"], (
        "the executor never resumed its units after the hook", events)
    # The executor resumed and completed with an unchanged Codex end state.
    probe.assert_admitted_per_file()
    worker = [f for f in _hook_log(ns) if f.get("op") == "replay-drain"][-1]
    assert worker["result"] == "success" and worker.get("heal") == "done", worker
    assert _heal_record(ns) is None
    if mode == "replay":
        assert not _replay_armed(ns)
    assert _heal_oracle(ns) == control
    assert _claude_rows(ns, transcript)[0] == 2


class _ClockedTime:
    """The cache module's ``time`` with an injected ``monotonic``."""

    def __init__(self, real, clock):
        self._real, self._clock = real, clock

    def monotonic(self):
        return self._clock["now"]

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("case", ["waiter", "sustained", "no-wait"])
def test_heal_raw_contention_resets_on_a_fresh_waiter(env, monkeypatch, case):
    """F6 (#1055): a raw store-lock refusal seen while a request is fresh (here
    the Claude waiter, registered after the admission checks) is a deliberate
    wait, not continuous contention, so it resets the 120 s bound.

    Detects: an executor that ends the pass ``contended`` (keeping the fence
    and requesting another worker) because Claude hooks took their turns just
    before the bound; a reset that masks a hung ordinary writer; and a
    ``wait_for_requests=False`` pass that waits instead of yielding."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    _seed_split_heal(ns, provider, day)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    population = cc._codex_history_candidates()
    assert population
    clock = {"now": 1000.0}
    monkeypatch.setattr(cc, "time", _ClockedTime(cc.time, clock))
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    codex_lock = ns["_cctally_core"].CACHE_LOCK_CODEX_PATH
    state = {"calls": 0, "holder": None, "waiter": None, "contend": True,
             "first": None, "registered": False}

    def pre_store_locks():
        state["calls"] += 1
        if state["contend"] and state["holder"] is None:
            fd = os.open(codex_lock, os.O_RDWR | os.O_CREAT, 0o600)
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            state["holder"] = fd
        elif not state["contend"] and state["holder"] is not None:
            os.close(state["holder"])
            state["holder"] = None
        if state["first"] is None:
            state["first"] = clock["now"]
        if (case == "waiter" and not state["registered"]
                and clock["now"] - state["first"]
                >= cc.CODEX_REPLAY_DRAIN_LOCK_TIMEOUT_SECONDS):
            # The first attempt whose raw refusal would end the pass without
            # the reset; registered after admission's checks, so the raw
            # branch is what sees it fresh.
            state["registered"] = True
            state["waiter"] = adm.ClaudeWaiter.register(
                ns["APP_DIR"], deadline_ns=adm._monotonic_ns() + 5_000_000_000)
            assert state["waiter"] is not None

    backoffs: list = []

    def backoff(_adm):
        backoffs.append(clock["now"])
        if len(backoffs) > 40:
            raise AssertionError("the heal waited on the store locks unbounded")
        if state["waiter"] is not None:
            state["waiter"].release()
            state["waiter"] = None
            state["contend"] = False
        clock["now"] += 7.0

    monkeypatch.setattr(cc, "_CODEX_PRE_STORE_LOCK_HOOK", pre_store_locks,
                        raising=False)
    monkeypatch.setattr(cc, "_drain_backoff", backoff)
    try:
        result = cc.run_codex_admitted_heal(
            wait_for_requests=case != "no-wait", population=population)
    finally:
        if state["waiter"] is not None:
            state["waiter"].release()
        if state["holder"] is not None:
            os.close(state["holder"])
    if case == "no-wait":
        assert result.outcome == "yielded" and backoffs == [], (result, backoffs)
        return
    # The refusals spanned the bound: without the reset this pass gives up.
    assert clock["now"] - backoffs[0] >= cc.CODEX_REPLAY_DRAIN_LOCK_TIMEOUT_SECONDS
    if case == "sustained":
        assert result.outcome == "contended", result
        assert _heal_record(ns) is not None, "the contended pass kept its fence"
        return
    assert result.outcome == "done", (
        "a raw refusal met while the Claude waiter was fresh counted as "
        "continuous contention", result)
    assert _heal_record(ns) is None


@pytest.mark.parametrize("case", ["codex-hook", "refill-hook", "gate"])
def test_a_probe_collision_loses_no_work(env, monkeypatch, case):
    """F5 (#1055): a Claude hook's momentary shared probe that coincides with
    another writer's non-blocking attempt. The probe is reproduced exactly (a
    read-only descriptor holding ``LOCK_SH | LOCK_NB``) at that instant.

    Detects: an ordinary Codex hook refused by the provider-lock probe that
    loses its rollout or leaves a request the drain never outlives (it must
    publish one 60 s request and the worker must then finish unchanged); a
    replay-refill hook that publishes a request the refill could only wait
    out; and a worker refused by a gate probe that busy-spins or loses work
    instead of taking one ordinary 5 s step."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    core = ns["_cctally_core"]
    if case == "codex-hook":
        control, _ = _seed_production_heal(ns, provider, day, monkeypatch)
        cc.run_codex_inventory_bootstrap(wait_for_requests=False)
        population = [day / _name(h, s) for h, s in ((11, 2), (12, 1), (13, 4))]
    else:
        population, control = _seed_replay(ns, day)
    clock = {"now": int(adm._monotonic_ns())}
    monkeypatch.setattr(adm, "_monotonic_ns", lambda: clock["now"])
    slept: list = []

    def sleep(seconds):
        slept.append(seconds)
        clock["now"] += int(seconds * 1_000_000_000)

    monkeypatch.setattr(adm, "_sleep", sleep)
    collided: list = []
    commit_times: dict = {}

    def shared_probe(path):
        fd = os.open(path, os.O_RDONLY)
        _fcntl.flock(fd, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        return fd

    real_admit = adm.admit

    def admit(*args, **kwargs):
        if collided == ["armed"]:
            fd = shared_probe(core.APP_DIR / adm.GATE_FILE_NAME)
            try:
                result = real_admit(*args, **kwargs)
            finally:
                os.close(fd)
            collided.append(("gate", result.admitted))
            return result
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(adm, "admit", admit)

    def on_commit(path):
        commit_times[path] = clock["now"]

    def on_finish():
        if collided or not probe.commits():
            return
        if case == "gate":
            collided.append("armed")
            return
        fd = shared_probe(core.CACHE_LOCK_CODEX_PATH)
        try:
            if case == "codex-hook":
                _hook_tick(ns)
                collided.append(([f for f in _hook_log(ns) if "sync" in f][-1],
                                 adm.fresh_requests(core.APP_DIR), clock["now"]))
            else:
                collided.append(_replay_hook(ns, monkeypatch)
                                + (adm.fresh_requests(core.APP_DIR),))
        finally:
            os.close(fd)

    probe = _HealProbe(ns, monkeypatch, population, on_commit=on_commit,
                       on_finish=on_finish)
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    probe.assert_admitted_per_file()
    worker = [f for f in _hook_log(ns) if f.get("op") == "replay-drain"][-1]
    assert worker["result"] == "success" and worker.get("heal") == "done", worker
    assert _heal_record(ns) is None
    assert _heal_oracle(ns) == control, "the collision lost or changed work"
    assert collided, "the collision never happened"
    if case == "codex-hook":
        line, requests, published_at = collided[0]
        assert (line["sync"], line.get("drain_request")) == (
            "contended", "published"), line
        assert len(requests) == 1 and adm.CLAUDE_WAITER_SENTINEL not in requests
        later = [t for t in commit_times.values() if t > published_at]
        assert later and all(t >= published_at + adm.REQUEST_TTL_NS
                             for t in later), (published_at, commit_times)
        assert set(slept) == {adm.POLL_SECONDS}
    elif case == "refill-hook":
        line, _calls, _deferral, requests = collided[0]
        assert (line["sync"], line.get("reason")) == (
            "contended", "replay_pending"), line
        assert line.get("drain_request") is None and requests == []
        assert slept == []
    else:
        assert collided[1] == ("gate", False), collided
        assert slept == [adm.POLL_SECONDS], slept


def test_replay_corrupt_zst_still_consumes_marker(env, monkeypatch):
    """R2 (#982): a corrupt cursorless `.zst` that reaches the refill degrades
    under U6 and does not block consumption, as a container that turns
    unreadable after the single replay's preflight does today. One already
    unreadable at the start unit is refused by today's preflight (I5): the
    start clears nothing and leaves every marker. Detects: a per-file replay
    whose unreadable container keeps the marker (and every Codex hook
    declining) forever, and a start unit that clears past the refusal."""
    ns, provider, day, _ = env
    cc = _cache()
    population, _control = _seed_replay(ns, day, count=2)
    bad = (day / _name(15, 79)).with_name(_name(15, 79) + ".zst")
    garbage = b"\x28\xb5\x2f\xfd" + b"not a zstd frame" * 8
    rows = {path: _entry_rowids(ns, path) for path in population}
    bad.write_bytes(garbage)
    refused = cc.run_codex_admitted_heal(wait_for_requests=True)
    assert (refused.mode, refused.outcome) == ("replay", "refused"), refused
    assert _replay_armed(ns) and _heal_record(ns) is None
    assert {path: _entry_rowids(ns, path) for path in population} == rows, (
        "a refused start unit cleared the store")
    bad.unlink()
    # A second rollout readable at the start unit (so in its population)
    # that turns unreadable before its refill unit; `bad` arrives after the
    # census. Only the population file counts as degraded (review F8).
    member = day / _name(15, 78)
    write_rollout(member, conversation_records(1, session_id="member-0078"))
    member = _compress(member)
    real_sync = cc.sync_codex_cache

    def sync(conn, **kwargs):
        stats = real_sync(conn, **kwargs)
        if kwargs.get("_replay_stage") == "start":
            bad.write_bytes(garbage)     # after the start unit's preflight
            member.write_bytes(garbage)
        return stats

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    result = cc.run_codex_admitted_heal(wait_for_requests=True)
    assert bad.exists()
    assert not _replay_armed(ns), ("the unreadable container blocked the "
                                   "replay", result)
    assert _heal_record(ns) is None
    assert _meta_value(ns, cc.CODEX_REPLAY_BLOCKED_KEY) is None
    assert (result.mode, result.outcome) == ("replay", "done"), result
    assert result.degraded == 1, ("only the population file still owed is "
                                  "degraded", result)
    for path in population:
        assert _entry_accounts(ns, path), path
    assert _one(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
                (str(bad).removesuffix(".zst"),)) == (0,)


def test_replay_blocked_by_failed_file_keeps_certify_record(env, monkeypatch):
    """R2 (#982): a refill blocked by today's condition (a file that fails)
    keeps the marker and moves the record to phase `certify`: the fence ends,
    adoption settles, sealing resumes, and no later sync clears again.
    Detects: a blocked replay that holds the fence forever, or one whose
    next unbudgeted sync re-clears and recaptures D1 from a partial store."""
    ns, provider, day, _ = env
    cc = _cache()
    population, _control = _seed_replay(ns, day)
    bad = population[1]
    restore = _fail_container(cc, monkeypatch, bad)
    result = cc.run_codex_admitted_heal(wait_for_requests=True)
    record = _heal_record(ns)
    assert _replay_armed(ns), ("a blocked replay consumed its marker", result)
    assert record is not None and (record["mode"], record["phase"]) == (
        "replay", "certify"), (record, result)
    assert _meta_value(ns, cc.CODEX_REPLAY_BLOCKED_KEY) is not None
    assert result.mode == "replay", result
    conn = ns["open_cache_db"]()
    try:
        assert not cc._codex_heal_fence_active(conn)
        assert cc.load_codex_finalization_debt(conn).empty, (
            "adoption did not settle after the blocked replay")
    finally:
        conn.close()
    assert cc.run_codex_barrier_seal_drain(
        wait_for_requests=False).census != "heal-fenced"
    # The fence is over, so a hook declines the replay as it does for a
    # blocked replay today but still runs its projection and budget leg
    # (spec 4.4: the skip is for a cleared, partly refilled store only).
    _line, calls, _deferral = _replay_hook(ns, monkeypatch)
    assert calls["projection"] >= 1 and calls["budget"] >= 1, calls
    kept = {path: _entry_rowids(ns, path) for path in population if path != bad}
    assert all(kept.values()), kept
    _sync(ns, maintenance="caller")
    assert {path: _entry_rowids(ns, path) for path in kept} == kept, (
        "the next unbudgeted sync cleared the blocked replay again")
    assert _replay_armed(ns) and _heal_record(ns)["phase"] == "certify"
    # Control: once the file reads, an ordinary walk consumes both.
    restore()
    _sync(ns, maintenance="caller")
    assert not _replay_armed(ns) and _heal_record(ns) is None
    assert _entry_accounts(ns, bad)
    assert {path: _entry_rowids(ns, path) for path in kept} == kept


def _replay_resume_after_kill(env, monkeypatch, kill):
    """R4 (replay): see ``test_heal_resume_after_kill``."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    journal = sys.modules["_cctally_journal"]
    premech = day / _name(10, 70)
    write_rollout(premech, conversation_records(
        2, session_id="premech-0070", label="pre"))
    with monkeypatch.context() as m:
        m.setattr(cc, "_append_codex_file_account_decision", lambda **_kw: None)
        m.setattr(cc, "record_codex_file_account", lambda *_a, **_kw: None)
        _sync(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_entries SET account_key=NULL "
                     "WHERE source_path=?", (str(premech),))
        conn.commit()
    finally:
        conn.close()
    population, control = _seed_replay(ns, day)
    population = [premech] + population
    assert set(_entry_accounts(ns, premech)) == {None}
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    real_rehydrate = journal.rehydrate_codex_journal_families
    rehydrations: list = []

    def rehydrate(conn, *args, **kwargs):
        record = cc.load_codex_heal_record(conn)
        if (kill == "replay-start-before-rehydration" and not rehydrations
                and record is not None and record.get("mode") == "replay"
                and not record.get("rehydrated")):
            rehydrations.append("killed")
            raise _Killed()
        rehydrations.append(kwargs.get("authoritative"))
        return real_rehydrate(conn, *args, **kwargs)

    monkeypatch.setattr(journal, "rehydrate_codex_journal_families", rehydrate)

    def on_finish():
        if kill == "replay-units" and len(probe.commits()) == 1:
            raise _Killed()

    probe = _HealProbe(ns, monkeypatch, population, on_finish=on_finish)
    with pytest.raises(_Killed):
        cc.cmd_codex_replay_drain_internal(_argparse.Namespace())
    monkeypatch.setattr(adm, "_after_finish", None)
    record = _heal_record(ns)
    assert record is not None and (record["mode"], record["phase"]) == (
        "replay", "units"), record
    assert record["rehydrated"] is (kill == "replay-units"), record
    assert _replay_armed(ns)
    stats = _sync(ns, budget_seconds=60.0, maintenance="caller")
    assert stats.deferred_reason == "replay_pending" and stats.replay_refill
    survivors = {path: _entry_rowids(ns, path) for path in population}
    if kill == "replay-units":
        assert survivors[premech], "the first refill unit did not commit"
    resumed_from = len(rehydrations)
    assert cc.cmd_codex_replay_drain_internal(_argparse.Namespace()) == 0
    if kill == "replay-start-before-rehydration":
        assert rehydrations[resumed_from] is True, (
            "the resume read rollouts before completing the authoritative "
            "rehydration", rehydrations)
    assert not _replay_armed(ns) and _heal_record(ns) is None
    for path, ids in survivors.items():
        if ids:
            assert _entry_rowids(ns, path) == ids, (
                "the resumed replay cleared again", path)
    assert set(_entry_accounts(ns, premech)) == {None}, (
        "pre-mechanism spend took the live login after the resume (D1, I7)")
    actual = _heal_oracle(ns)
    if actual != control:
        _dump_heal_differences(ns, control, actual)
    assert actual == control


def test_cache_sync_replay_runs_before_provenance(env, monkeypatch):
    """#982 spec 4.6: `cache-sync --source codex` with an armed replay and
    held history runs the replay executor first, then provenance, then the
    history executor. Detects: an unadmitted first replay sync, or a replay
    that runs after provenance settles the census it then clears."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    _arm_replay(ns)
    order: list = []
    real_heal = cc.run_codex_admitted_heal
    real_provenance = cc.run_codex_history_provenance_drain

    def heal(**kwargs):
        result = real_heal(**kwargs)
        order.append(("heal", result.mode, result.outcome))
        return result

    def provenance(**kwargs):
        order.append(("provenance", _replay_armed(ns)))
        return real_provenance(**kwargs)

    monkeypatch.setattr(cc, "run_codex_admitted_heal", heal)
    monkeypatch.setattr(cc, "run_codex_history_provenance_drain", provenance)
    assert cc.cmd_cache_sync(_argparse.Namespace(
        source="codex", rebuild=False, prune_orphans=False,
        prune_conversations=False)) == 0
    assert order[:2] == [("heal", "replay", "done"), ("provenance", False)], order
    assert ("heal", "history", "done") in order[2:], order
    assert not _replay_armed(ns) and _heal_record(ns) is None
    accounts = _entry_accounts(ns, p1)
    assert len(accounts) == 3 and accounts[2] is not None, accounts


def _token_events(session_id, first_total, count, minute):
    """``count`` token events continuing a rollout's cumulative total."""
    out = []
    for index in range(count):
        record = token_records(1, session_id=session_id,
                               start_minute=minute + index)[-1]
        record["payload"]["info"]["total_token_usage"]["total_tokens"] = (
            first_total + 100 * index)
        out.append(record)
    return out


def _append_records(path, records) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def test_heal_last_piece_revalidates_visited_rows(env, monkeypatch):
    """R8 (#982 spec 4.1 step 7, revision 7): another writer that partially
    ingests a visited live file between two pieces of the closing walk leaves
    it in the backlog; the barrier census is 0 and the full-walk certificate
    is withheld, as a single walk followed by that partial hook leaves them.
    Detects: a last piece that trusts the visited set and certifies a store
    holding an incomplete cursor."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    live_a, live_b = day / _name(15, 45), day / _name(16, 46)
    for n, path in enumerate((live_a, live_b)):
        write_rollout(path, token_records(2, session_id=f"live-{n:04d}"))
    _sync(ns)
    real_sync = cc.sync_codex_cache
    closing: list = []
    partial: list = []

    def partial_hook():
        """A budgeted sync whose deadline passes after live_a's first new
        accounting emission (the parser's own stop-before-line)."""
        clock = {"now": 0.0}
        real_iter = cc._iter_codex_fused_records_with_offsets

        def expire_after_entry(stream, source_path, **kwargs):
            for emission in real_iter(stream, source_path, **kwargs):
                yield emission
                if (source_path == str(live_a)
                        and emission.accounting is not None):
                    clock["now"] = 10_000.0

        with monkeypatch.context() as m:
            m.setattr(cc, "_walk_clock", lambda: clock["now"])
            m.setattr(cc, "_iter_codex_fused_records_with_offsets",
                      expire_after_entry)
            return _sync(ns, budget_seconds=100, quota_reconcile="defer")

    def sync(conn, **kwargs):
        piece = (kwargs.get("_ingest_limit") is not None
                 and not kwargs.get("_defer_finalization"))
        if piece and not closing:
            for n, path in enumerate((live_a, live_b)):
                _append_records(path, _token_events(f"live-{n:04d}", 300, 1, 20))
        stats = real_sync(conn, **kwargs)
        if piece:
            closing.append(stats)
            if len(closing) == 1 and not stats.heal_walk_complete:
                _append_records(live_a, _token_events("live-0000", 400, 2, 30))
                partial.append(partial_hook())
                partial.append(_cursor_row(ns, live_a))
        return stats

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    result = _settle_and_heal(ns)
    monkeypatch.setattr(cc, "sync_codex_cache", real_sync)
    assert result.outcome == "done", result
    assert len(closing) >= 2 and partial, ("the closing walk took one piece",
                                           closing)
    assert partial[1]["ingest_complete"] == 0, partial[1]
    assert _cursor_row(ns, live_a)["ingest_complete"] == 0, (
        "a later piece re-examined the visited file")
    assert _meta_value(ns, cc.CODEX_BARRIER_CENSUS_KEY) == "0", (
        "the last piece certified the barrier census over a visited file "
        "another writer left incomplete")
    assert not _full_walk_marker(ns), (
        "the heal certified a store holding an incomplete cursor")
    conn = ns["open_cache_db"]()
    try:
        backlog = cc._load_codex_backlog_record(conn)
    finally:
        conn.close()
    assert backlog is not None and backlog["files"] >= 1, backlog
    assert closing[-1].heal_visit_changed == {str(live_a)}
    # Control: the next ordinary sync resumes the file and certifies.
    _sync(ns)
    assert _cursor_row(ns, live_a)["ingest_complete"] == 1
    assert _full_walk_marker(ns)


# --- #982 #975 implementation-review corrections (spec 4.5a) ---------------


def _premech_rollout(ns, monkeypatch, path, session_id) -> None:
    """A rollout ingested before the durable-attribution mechanism: no
    journaled decision and unattributed spend (D1, I7)."""
    cc = _cache()
    write_rollout(path, conversation_records(2, session_id=session_id,
                                             label="pre"))
    with monkeypatch.context() as m:
        m.setattr(cc, "_append_codex_file_account_decision", lambda **_kw: None)
        m.setattr(cc, "record_codex_file_account", lambda *_a, **_kw: None)
        _sync(ns)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE codex_session_entries SET account_key=NULL "
                     "WHERE source_path=?", (str(path),))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("arm", ["helper", "migration"])
def test_replay_rearmed_over_blocked_record_clears_again(env, monkeypatch, arm):
    """Spec 4.5a: a replay marker re-armed (a later arming migration) while a
    blocked replay's `certify` record exists makes the next unbudgeted sync
    clear again, with the record's persisted D1 set in its snapshot.

    Detects: a re-armed replay that only refills (the rows the new parser
    must re-derive are never cleared, so the migration's repair never
    happens), and a re-clear that recaptures D1 from the partly refilled
    store, sending a pre-mechanism rollout the refill never reached to the
    live `auth.json` (I7)."""
    ns, provider, day, _ = env
    cc = _cache()
    premech = day / _name(10, 70)
    _premech_rollout(ns, monkeypatch, premech, "premech-0070")
    population, _control = _seed_replay(ns, day)
    restore = _fail_container(cc, monkeypatch, premech)
    cc.run_codex_admitted_heal(wait_for_requests=True)
    assert _heal_record(ns)["phase"] == "certify" and _replay_armed(ns)
    assert _entry_rowids(ns, premech) == [], "the failing rollout was refilled"
    kept = {path: _entry_rowids(ns, path) for path in population}
    assert all(kept.values()), kept
    # Leave the persisted D1 set as the rollout's only pre-mechanism
    # evidence: no cursor (the clear erased it and the refill failed), and,
    # as on a store that predates them, no incarnation and no ledger row.
    conn = ns["open_cache_db"]()
    try:
        identity = next(
            cc.codex_file_key_for_entry_path(root, path)
            for root, path in conn.execute(
                "SELECT DISTINCT source_root_key, source_path "
                "FROM codex_accounting_change_log WHERE source_path=?",
                (str(premech),)))
        assert identity in _heal_record(ns)["known"]
        conn.execute("DELETE FROM codex_file_incarnations WHERE file_identity=?",
                     (identity,))
        conn.execute("DELETE FROM codex_accounting_change_log "
                     "WHERE source_path=?", (str(premech),))
        conn.commit()
    finally:
        conn.close()
    restore()
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    conn = ns["open_cache_db"]()
    try:
        if arm == "helper":
            cc.arm_codex_replay_from_zero(conn)
            conn.commit()
        else:
            sys.modules["_cctally_db"]._035_codex_thread_source_inference_replay(
                conn)
    finally:
        conn.close()
    assert _heal_record(ns).get("reclear") is True, _heal_record(ns)
    _sync(ns, maintenance="caller")
    assert all(_entry_rowids(ns, path) != ids for path, ids in kept.items()), (
        "the re-armed replay did not clear again")
    assert not _replay_armed(ns) and _heal_record(ns) is None
    assert _entry_rowids(ns, premech), "the re-clear did not refill the rollout"
    assert set(_entry_accounts(ns, premech)) == {None}, (
        "the re-clear lost the persisted D1 set (I7)")


@pytest.mark.parametrize("damage", ["known", "rehydrated", "unparseable"])
def test_malformed_replay_record_fails_closed(env, monkeypatch, damage):
    """Spec 4.5a: a replay record whose D1 set or rehydration flag is missing
    or malformed declines every writer without clearing, and the executor
    ends `degraded`. Detects: a re-clear with a recaptured D1 set (the
    persisted one is lost) or a refill with a corrupted one."""
    ns, provider, day, _ = env
    cc = _cache()
    population, _control = _seed_replay(ns, day)
    rows = {path: _entry_rowids(ns, path) for path in population}
    fence = {"v": 1, "mode": "replay", "phase": "units", "rehydrated": True,
             "gen": "damaged"}
    payload = {"v": 1, "gen": "damaged",
               "population": sorted(str(p) for p in population), "known": []}
    if damage == "known":
        payload["known"] = 7
    elif damage == "rehydrated":
        fence["rehydrated"] = "yes"
    else:
        fence = "{not json"
    _write_heal_record(ns, fence, payload)
    stats = _sync(ns, maintenance="caller")
    assert stats.deferred_reason == "replay_pending", stats
    assert {path: _entry_rowids(ns, path) for path in population} == rows, (
        "a writer cleared under a malformed replay record")
    result = cc.run_codex_admitted_heal(wait_for_requests=False)
    assert (result.mode, result.outcome) == ("replay", "degraded"), result
    assert {path: _entry_rowids(ns, path) for path in population} == rows
    assert _replay_armed(ns)


def test_heal_record_shortens_replay_worker_throttle(env, monkeypatch):
    """Spec 4.5a: while a heal record exists, a worker request is throttled
    for minutes, not the hourly spawn throttle, and a `contended` pass
    requests the worker. Detects: a stalled pass leaving the global fence
    standing until the hourly spawn."""
    import _lib_codex_drain_admission as adm
    ns, provider, day, _ = env
    cc = _cache()
    core = ns["_cctally_core"]
    held = day / _name(11, 51)
    write_rollout(held, conversation_records(1, session_id="held-0051"))
    keeper = day / _name(20, 52)
    write_rollout(keeper, token_records(1, session_id="keeper-0052"))
    _sync(ns)
    _arm_heal_record(ns, [held])
    spawns: list = []
    monkeypatch.setattr(cc, "_spawn_codex_worker",
                        lambda command: spawns.append(command) or True)
    monkeypatch.setattr(cc, "CODEX_REPLAY_DRAIN_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(adm, "_sleep", lambda _s: None)
    marker = ns["APP_DIR"] / cc.CODEX_REPLAY_DRAIN_MARKER_NAME
    if marker.exists():
        marker.unlink()
    fd = os.open(core.CACHE_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        result = cc.run_codex_admitted_heal(wait_for_requests=True)
    finally:
        os.close(fd)
    assert result.outcome == "contended", result
    replay = cc.CODEX_REPLAY_DRAIN_COMMAND

    def requested() -> int:
        return spawns.count(replay)

    assert requested() == 1, ("the contended pass did not request the worker",
                              spawns)
    assert cc.CODEX_HEAL_RETRY_THROTTLE_SECONDS == 300
    now = __import__("time").time()
    os.utime(marker, (now - 301, now - 301))
    _hook_tick(ns)
    assert requested() == 2, ("a hook 301 s after the request did not spawn "
                              "the worker while the record exists", spawns)
    now = __import__("time").time()
    os.utime(marker, (now - 299, now - 299))
    _hook_tick(ns)
    assert requested() == 2, ("a hook 299 s after the request spawned", spawns)
    # Control: without a record the hourly throttle is unchanged.
    _drop_heal_record(ns)
    now = __import__("time").time()
    os.utime(marker, (now - 301, now - 301))
    assert cc._defer_codex_replay_drain() == "throttled"
    assert requested() == 2


def test_heal_finalization_units_bound_lock_contention(env, monkeypatch):
    """Spec 4.1 step 6 (review F10): a finalization unit whose store locks
    another writer holds continuously ends the pass `contended` after the
    replay worker's lock wait, as every other heal unit does. Detects: a
    finalization loop that waits on the store locks without a bound."""
    ns, provider, day, _ = env
    cc = _cache()
    _seed_split_heal(ns, provider, day)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    transitioned: list = []
    real_sync = cc.sync_codex_cache

    def sync(conn, **kwargs):
        stats = real_sync(conn, **kwargs)
        if getattr(stats, "heal_transitioned", False):
            transitioned.append(True)
        return stats

    real_locks = cc._cache_store_locks
    real_debt = cc.load_codex_finalization_debt
    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    monkeypatch.setattr(cc, "_cache_store_locks",
                        lambda: None if transitioned else real_locks())
    monkeypatch.setattr(
        cc, "load_codex_finalization_debt",
        lambda conn: (cc._codex_malformed_debt() if transitioned
                      else real_debt(conn)))
    monkeypatch.setattr(cc, "CODEX_REPLAY_DRAIN_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    backoffs: list = []

    def backoff(_adm):
        backoffs.append(1)
        if len(backoffs) > 50:
            raise AssertionError("the finalization units waited on the store "
                                 "locks without a bound")

    monkeypatch.setattr(cc, "_drain_backoff", backoff)
    result = cc.run_codex_admitted_heal(
        wait_for_requests=True, population=cc._codex_history_candidates())
    assert transitioned, result
    assert result.outcome == "contended", result


def test_heal_replay_armed_during_history_heal_ends_pass(env, monkeypatch):
    """Spec 4.5a: a replay armed while a history heal runs ends the pass
    before its fenced walk, without a transition, and the next pass runs in
    replay mode. Detects: a fenced walk whose every piece clears and refills
    under `_ingest_limit=1` (quadratic in the corpus)."""
    ns, provider, day, _ = env
    cc = _cache()
    p1, _keeper = _seed_split_heal(ns, provider, day)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    population = cc._codex_history_candidates()
    real_owed = cc._codex_heal_owed_files

    def owed(conn, files):
        if not _replay_armed(ns):
            _arm_replay(ns)                  # after the population units
        return real_owed(conn, files)

    current: list = []
    clears: list = []
    real_sync = cc.sync_codex_cache
    real_clear = cc._clear_codex_derived_rows

    def sync(conn, **kwargs):
        current.append(kwargs)
        try:
            return real_sync(conn, **kwargs)
        finally:
            current.pop()

    def clear(conn, *args, **kwargs):
        clears.append(current[-1].get("_ingest_limit") if current else "outside")
        return real_clear(conn, *args, **kwargs)

    monkeypatch.setattr(cc, "_codex_heal_owed_files", owed)
    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    monkeypatch.setattr(cc, "_clear_codex_derived_rows", clear)
    first = cc.run_codex_admitted_heal(wait_for_requests=False,
                                       population=population)
    assert (first.mode, first.outcome) == ("history", "replay_due"), first
    record = _heal_record(ns)
    assert record is not None and record["mode"] == "history", (
        "the ended pass made a transition", record)
    second = cc.run_codex_admitted_heal(wait_for_requests=False)
    assert (second.mode, second.outcome) == ("replay", "done"), second
    assert not _replay_armed(ns) and _heal_record(ns) is None
    assert clears and 1 not in clears, (
        "a walk piece cleared the store under `_ingest_limit=1`", clears)
    accounts = _entry_accounts(ns, p1)
    assert len(accounts) == 3 and accounts[2] is not None, accounts


@pytest.mark.parametrize("mode", ["history", "replay"])
def test_cache_sync_heal_summary_sums_units(env, monkeypatch, capsys, mode):
    """Review F7: `cache-sync --source codex` prints the existing "codex
    done" line once for the heal, with every count summed over the heal's
    units and walk pieces and each file reported at most once per pass.
    Detects: a history heal that prints no summary, a replay summary mixing
    summed and last-piece counts, and a skipped count inflated by the
    pass's later walks re-finding (or skipping as visited) files it already
    counted."""
    ns, provider, day, _ = env
    cc = _cache()
    write_rollout(day / _name(20, 99), conversation_records(
        1, session_id="keeper-0099"))
    _sync(ns)
    paths = [day / _name(10, n + 1) for n in range(2)]
    for n, path in enumerate(paths):
        write_rollout(path, conversation_records(
            3, session_id=f"sum-{n:04d}", label=f"s{n}"))
    _sync(ns)
    if mode == "history":
        # Held compressed history whose journal is its only trace (as in
        # `_seed_split_heal`): the first leg holds it, provenance settles the
        # census, and the heal ingests it.
        for path in paths:
            _compress(path)
        _drop_stores(ns)
        _sync(ns)
        cc.run_codex_inventory_bootstrap(wait_for_requests=False)
        assert all(_entry_accounts(ns, path) == [] for path in paths)
    else:
        _arm_replay(ns)
    units: list = []
    real_sync = cc.sync_codex_cache

    def sync(conn, **kwargs):
        stats = real_sync(conn, **kwargs)
        if kwargs.get("_walk_share") is not None:
            units.append(stats)
        return stats

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    capsys.readouterr()
    assert cc.cmd_cache_sync(_argparse.Namespace(
        source="codex", rebuild=False, prune_orphans=False,
        prune_conversations=False)) == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "codex done:" in line]
    assert units and lines, err

    def total(name):
        return sum(int(getattr(stats, name) or 0) for stats in units)

    assert total("files_processed") >= 2 and total("rows_changed") > 0
    # Each file at most once per pass, as the single walk reports it: the
    # replay re-reads all three files; the history heal reads the two held
    # files and walks the keeper unchanged. The certification and closing
    # walks find every file again, and their pieces skip visited files;
    # neither may count a file twice.
    processed, skipped = (3, 0) if mode == "replay" else (2, 1)
    assert total("files_processed") == processed, total("files_processed")
    expected = (
        f"codex done: {processed} processed, "
        f"{skipped} skipped, "
        f"{total('files_reset_truncated')} reset, "
        f"{total('rows_changed')} rows changed, "
        f"{total('lines_malformed')} malformed, "
        f"{total('token_events_skipped')} drift-skipped")
    assert lines[-1].endswith(expected), (lines, expected)
    assert sum(1 for line in lines if line.endswith(expected)) == 1, lines


# --- #982 #975 round 7: every heal unit is bound to its pass generation ----


def _heal_store_state(ns):
    """Everything a heal unit could write: cursors, accounting rows (by
    rowid) and every `cache_meta` key (the record, the replay marker, the
    finalization debt, the full-walk certificate)."""
    conn = ns["open_cache_db"]()
    try:
        return (
            [tuple(row) for row in conn.execute(
                "SELECT * FROM codex_session_files ORDER BY path")],
            [row[0] for row in conn.execute(
                "SELECT rowid FROM codex_session_entries ORDER BY rowid")],
            [tuple(row) for row in conn.execute(
                "SELECT key, value FROM cache_meta ORDER BY key")],
        )
    finally:
        conn.close()


def test_replay_rearm_during_pass_restarts_from_fresh_clear(env, monkeypatch):
    """Spec 4.5a (Generation, round-7 P1): a replay re-armed between two
    pieces of an executor pass ends that pass `restart` without another
    write; the next pass's start unit clears freshly with the union D1 set
    and refills the pre-mechanism rollout, and only that pass consumes the
    new marker. Detects: a later piece of the old pass that performs the
    fresh clear, skips a file it visited before that clear and consumes the
    marker, leaving that file cursorless with its D1 evidence gone, so a
    later sync attributes it from the live `auth.json` (I7)."""
    ns, provider, day, _ = env
    cc = _cache()
    x, premech, b = (day / _name(10, 81), day / _name(11, 82),
                     day / _name(12, 83))
    for path, sid in ((x, "x-0081"), (b, "b-0083")):
        write_rollout(path, conversation_records(2, session_id=sid, label=sid))
    _sync(ns)
    _premech_rollout(ns, monkeypatch, premech, "premech-0082")
    assert set(_entry_accounts(ns, premech)) == {None}
    _arm_replay(ns)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    real_sync = cc.sync_codex_cache
    real_clear = cc._clear_codex_derived_rows
    current: list = []
    clears: list = []
    pieces: list = []
    rearmed: list = []

    def sync(conn, **kwargs):
        fenced = kwargs.get("_heal_transition")
        if fenced and not pieces and not rearmed:
            # Give the fenced walk bytes to stop at around the visited
            # pre-mechanism rollout: X's tail (ingested) and B's (the stop).
            _append_records(x, _token_events("x-0081", 900_000, 1, 40))
            _append_records(b, _token_events("b-0083", 900_000, 1, 40))
        current.append(kwargs)
        try:
            stats = real_sync(conn, **kwargs)
        finally:
            current.pop()
        if fenced and not rearmed:
            pieces.append(stats)
            assert str(premech) in stats.heal_examined, stats.heal_examined
            assert not stats.heal_walk_complete, "the piece did not stop"
            arm_conn = ns["open_cache_db"]()
            try:
                cc.arm_codex_replay_from_zero(arm_conn)
                arm_conn.commit()
            finally:
                arm_conn.close()
            rearmed.append(_heal_store_state(ns))
        return stats

    def clear(conn, *args, **kwargs):
        clears.append(dict(current[-1]) if current else {"outside": True})
        return real_clear(conn, *args, **kwargs)

    monkeypatch.setattr(cc, "sync_codex_cache", sync)
    monkeypatch.setattr(cc, "_clear_codex_derived_rows", clear)
    first = cc.run_codex_admitted_heal(wait_for_requests=False)
    assert rearmed, "the fenced walk never ran a piece"
    assert all(c.get("_replay_stage") == "start" for c in clears), (
        "a unit other than a pass's start unit performed the fresh clear",
        [sorted(k for k in c if c[k] is not None) for c in clears])
    assert (first.mode, first.outcome) == ("replay", "restart"), first
    assert _heal_store_state(ns) == rearmed[0], (
        "the old pass wrote after the re-arm")
    assert _replay_armed(ns) and _heal_record(ns).get("reclear") is True
    before_rows = {path: _entry_rowids(ns, path) for path in (x, b)}
    clears.clear()
    second = cc.run_codex_admitted_heal(wait_for_requests=False)
    assert (second.mode, second.outcome) == ("replay", "done"), second
    assert [c.get("_replay_stage") for c in clears] == ["start"], clears
    assert all(_entry_rowids(ns, path) != ids
               for path, ids in before_rows.items()), "no fresh clear ran"
    assert not _replay_armed(ns) and _heal_record(ns) is None
    assert _entry_rowids(ns, premech), "the new pass did not refill A"
    monkeypatch.setattr(cc, "sync_codex_cache", real_sync)
    _sync(ns)
    assert set(_entry_accounts(ns, premech)) == {None}, (
        "the pre-mechanism rollout was attributed from the live auth.json")


@pytest.mark.parametrize("mode", ["history", "replay"])
@pytest.mark.parametrize("unit", ["population", "piece"])
def test_heal_unit_with_stale_generation_writes_nothing(env, monkeypatch,
                                                        mode, unit):
    """Spec 4.5a (Generation): an executor unit whose pass generation is not
    the record's commits no row, cursor, record change or settlement and
    reports `heal_generation_stale`; the same unit with the record's
    generation ingests (control). Detects: a unit of an ended pass writing
    over a record a newer pass (or a fresh clear) wrote."""
    ns, provider, day, _ = env
    cc = _cache()
    keeper = day / _name(20, 91)
    write_rollout(keeper, token_records(1, session_id="keeper-0091"))
    if mode == "history":
        _sync(ns)
        held = day / _name(11, 92)
        write_rollout(held, conversation_records(2, session_id="held-0092"))
        _arm_heal_record(ns, [held])
        target = held
    else:
        population, _control = _seed_replay(ns, day, count=2)
        start = _sync(ns, maintenance="caller", lock_timeout=0,
                      _walk_share=cc._CodexWalkShare(), _replay_stage="start")
        assert not start.rebuild_refused and not start.prune_refused, start
        target = population[0]
    record = _heal_record(ns)
    assert record is not None and record["mode"] == mode, record
    kwargs = ({"only_paths": {str(target)}} if unit == "population" else
              {"_ingest_limit": 1, "_heal_transition": True,
               "_defer_finalization": True})
    before = _heal_store_state(ns)
    stale = _sync(ns, maintenance="caller", lock_timeout=0,
                  _walk_share=cc._CodexWalkShare(),
                  _heal_generation="an-older-pass", **kwargs)
    assert stale.heal_generation_stale, stale
    assert _heal_store_state(ns) == before, (
        "a unit with a stale generation wrote")
    control = _sync(ns, maintenance="caller", lock_timeout=0,
                    _walk_share=cc._CodexWalkShare(),
                    _heal_generation=record["gen"], **kwargs)
    assert not control.heal_generation_stale, control
    assert _entry_rowids(ns, target), "the control unit did not ingest"


@pytest.mark.parametrize("damage", ["known", "unparseable"])
def test_rebuild_declines_malformed_replay_record(env, monkeypatch, damage):
    """Spec 4.5a (Validation, revision 10): an automatic unbudgeted sync and
    an explicit `cache-sync --rebuild` both decline over a malformed replay
    record, leaving every row, the marker and the pre-mechanism rollout's
    D1 protection in place. Detects: any writer that re-clears with a
    recaptured D1 set, which would attribute a rollout not yet refilled
    from the live `auth.json` (I7)."""
    ns, provider, day, _ = env
    cc = _cache()
    premech = day / _name(10, 70)
    _premech_rollout(ns, monkeypatch, premech, "premech-0070")
    population, _control = _seed_replay(ns, day)
    rows = {path: _entry_rowids(ns, path) for path in population}
    fence = {"v": 1, "mode": "replay", "phase": "units", "rehydrated": True,
             "gen": "damaged"}
    payload = {"v": 1, "gen": "damaged",
               "population": sorted(str(p) for p in population),
               "known": 7 if damage == "known" else []}
    _write_heal_record(ns, "{not json" if damage == "unparseable" else fence,
                       payload)
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    stats = _sync(ns, maintenance="caller")
    assert stats.deferred_reason == "replay_pending", stats
    assert {path: _entry_rowids(ns, path) for path in population} == rows
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    cc.cmd_cache_sync(_argparse.Namespace(
        source="codex", rebuild=True, prune_orphans=False,
        prune_conversations=False))
    assert {path: _entry_rowids(ns, path) for path in population} == rows, (
        "an explicit rebuild cleared under a malformed replay record")
    assert _replay_armed(ns), "the rebuild consumed the marker"
    assert set(_entry_accounts(ns, premech)) <= {None}, (
        "a pre-mechanism rollout took the live login (I7)")


# --- #982 #975 fix-verification (P3) ---------------------------------------


@pytest.mark.parametrize("driver", ["sync", "executor"])
def test_reclear_record_without_marker_refills_and_retires(env, monkeypatch,
                                                           driver):
    """A replay record marked `reclear` whose replay marker is gone (only a
    damaged `cache_meta` produces it) is a record that no longer asks for a
    replay: an unbudgeted sync and an executor pass refill with it and retire
    it, clearing nothing. Detects: the `refill_record` read that raised
    `AttributeError` on every unbudgeted sync and executor start unit, and a
    fresh clear no armed marker asked for (the conversation side, which waits
    only on the marker, would then publish over the cleared store)."""
    ns, provider, day, _ = env
    cc = _cache()
    paths = [day / _name(11 + n, 61 + n) for n in range(2)]
    for n, path in enumerate(paths):
        write_rollout(path, conversation_records(
            2, session_id=f"reclear-{n:04d}", label=f"c{n}"))
    _sync(ns)
    rows = {path: _entry_rowids(ns, path) for path in paths}
    assert all(rows.values()), rows
    _write_heal_record(
        ns, {"v": 1, "mode": "replay", "phase": "units", "rehydrated": True,
             "reclear": True, "gen": "lost-marker"},
        {"v": 1, "gen": "lost-marker",
         "population": sorted(str(p) for p in paths), "known": []})
    assert not _replay_armed(ns)
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    clears: list = []
    real_clear = cc._clear_codex_derived_rows

    def clear(conn, *args, **kwargs):
        clears.append(True)
        return real_clear(conn, *args, **kwargs)

    monkeypatch.setattr(cc, "_clear_codex_derived_rows", clear)
    if driver == "sync":
        stats = _sync(ns, maintenance="caller")
        assert stats.deferred_reason is None, stats
    else:
        result = cc.run_codex_admitted_heal(wait_for_requests=False)
        assert (result.mode, result.outcome) == ("replay", "done"), result
    assert not clears, "a writer cleared without an armed replay marker"
    assert {path: _entry_rowids(ns, path) for path in paths} == rows
    assert _heal_record(ns) is None and not _replay_armed(ns)


def test_cache_sync_heal_rerun_refusal_exits_3(env, monkeypatch, capsys):
    """`cache-sync`'s second Codex leg reports a refused inline re-run (a
    replay armed during the history heal whose start unit the #485/#968
    preflight refuses) exactly as the first leg reports a refused replay:
    the existing refusal message and exit 3. Detects: a refused replay that
    prints an ordinary "codex done" line and exits 0."""
    ns, provider, day, _ = env
    cc = _cache()
    _seed_split_heal(ns, provider, day)
    real_owed = cc._codex_heal_owed_files

    def owed(conn, files):
        if not _replay_armed(ns):
            _arm_replay(ns)                  # after the population units
        return real_owed(conn, files)

    monkeypatch.setattr(cc, "_codex_heal_owed_files", owed)
    monkeypatch.setattr(cc, "_codex_compressed_rebuild_refusal",
                        lambda files: "decode-error")
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    capsys.readouterr()
    code = cc.cmd_cache_sync(_argparse.Namespace(
        source="codex", rebuild=False, prune_orphans=False,
        prune_conversations=False))
    err = capsys.readouterr().err
    assert code == 3, err
    assert cc.CODEX_REBUILD_REFUSED_MESSAGE in err, err
    assert _replay_armed(ns), "the refused replay consumed its marker"


def test_replay_drain_request_skips_spawn_while_heal_runs(env, monkeypatch):
    """A worker request while `codex-heal-walk.lock` is held (a heal pass is
    running) spawns nothing; with the lock free and a heal record present
    it spawns under the 300 s retry throttle. Detects: a redundant
    `_codex-replay-drain` every 300 s of a healthy heal, running provenance,
    verification, reset, membership and seal drains beside the executor."""
    ns, provider, day, _ = env
    cc = _cache()
    held = day / _name(11, 53)
    write_rollout(held, conversation_records(1, session_id="held-0053"))
    _sync(ns)
    _arm_heal_record(ns, [held])
    spawns: list = []
    monkeypatch.setattr(cc, "_spawn_codex_worker",
                        lambda command: spawns.append(command) or True)
    marker = ns["APP_DIR"] / cc.CODEX_REPLAY_DRAIN_MARKER_NAME
    marker.touch()
    now = __import__("time").time()
    os.utime(marker, (now - 301, now - 301))
    lock_path = ns["APP_DIR"] / cc.CODEX_HEAL_WALK_LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        assert cc._defer_codex_replay_drain() == "throttled"
        assert cc._defer_codex_replay_drain(short=True) == "throttled"
        assert spawns == [], ("a request spawned while the heal ran", spawns)
    finally:
        os.close(fd)
    assert cc._defer_codex_replay_drain() == "spawned"
    assert spawns == [cc.CODEX_REPLAY_DRAIN_COMMAND], spawns


def test_heal_history_start_unit_bounds_lock_contention(env, monkeypatch):
    """The history start unit, like every other heal unit, ends the pass
    `contended` after the replay worker's lock wait when another writer
    holds the store locks continuously. Detects: a start unit that waits on
    the store locks without a bound."""
    ns, provider, day, _ = env
    cc = _cache()
    _seed_split_heal(ns, provider, day)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome == "settled"
    population = cc._codex_history_candidates()
    assert population and _heal_record(ns) is None
    monkeypatch.setattr(cc, "_cache_store_locks", lambda: None)
    monkeypatch.setattr(cc, "CODEX_REPLAY_DRAIN_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(cc, "_spawn_codex_worker", lambda command: True)
    backoffs: list = []

    def backoff(_adm):
        backoffs.append(1)
        if len(backoffs) > 50:
            raise AssertionError("the history start unit waited on the store "
                                 "locks without a bound")

    monkeypatch.setattr(cc, "_drain_backoff", backoff)
    result = cc.run_codex_admitted_heal(wait_for_requests=True,
                                        population=population)
    assert result.outcome == "contended", result
    assert _heal_record(ns) is None, "the contended start unit wrote a record"
