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

    def plain_only(logical):
        selected = real(logical)
        if selected is not None and selected.kind == leaf.PLAIN:
            return selected
        return None

    monkeypatch.setattr(leaf, "select_container", plain_only)
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
