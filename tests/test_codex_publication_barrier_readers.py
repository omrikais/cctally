"""#968 T5: the publication barrier through every consumer surface (5.11)."""
from __future__ import annotations

import concurrent.futures
import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import _lib_codex_conversation_query as q  # noqa: E402
import _lib_conversation_dispatch as disp  # noqa: E402
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, token_records, write_rollout, zstd_bytes)
from test_codex_bounded_ingest import _auth_json  # noqa: E402
from test_codex_publication_barrier import (  # noqa: E402,F401 (env is a fixture)
    HOLDS, OTHER_SESSION, REAL_RESOLVE, SESSION, _cache, _certificate,
    _compress, _degrade, _forget_membership, _key, _meta_only, _name, _one,
    _replace_container, _sync, _sync_stopping_after, _unfixed_prune, env)

SPEED = "standard"


def _conv(ns, **kwargs):
    conn = ns["open_conversations_db"]()
    try:
        return ns["sync_codex_conversations"](conn, **kwargs)
    finally:
        conn.close()


def _conv_rows(ns, sql, params=()):
    conn = ns["open_conversations_db"]()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _lines(records) -> bytes:
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _append(path, records) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _accounts(ns) -> list[str]:
    return sorted({row[0] or "unattributed" for row in _conv_rows(
        ns, "SELECT account_key FROM codex_conversation_messages")})


def _label(ns, cwd: str):
    """The project label the rollup writer derives for a bare cwd."""
    cache = ns["open_cache_db"]()
    try:
        root = cache.execute(
            "SELECT source_root_key FROM codex_session_files LIMIT 1").fetchone()[0]
    finally:
        cache.close()
    return _cache()._codex_conversation_project_attribution(root, cwd, None)[1]


def _list_row(conn, key):
    rows = q.list_codex_conversations(conn, effective_speed=SPEED, limit=500)["rows"]
    return next((row for row in rows if row["conversation_key"] == key), None)


def _surfaces(ns, key, accounts=()):
    """What every consumer surface reports for one conversation."""
    out = {}
    conn = ns["open_conversations_db"]()
    try:
        row = _list_row(conn, key)
        out["list"] = None if row is None else (
            row["title"], row["project_label"], row["count"])
        detail = q.get_codex_conversation(
            conn, key, effective_speed=SPEED, limit=0)
        out["detail"] = (detail["status"], len(detail.get("items") or ()))
        out["find"] = q.find_in_codex_conversation(conn, key, "Answer").get("total")
        hits = disp.neutral_search(
            conn, "Answer", source="codex", limit=50).get("hits") or ()
        out["search"] = key in {hit.get("conversation_key") for hit in hits}
        out["export"] = disp.neutral_export(conn, key)["status"]
        out["preflight"] = disp.neutral_events_preflight(conn, key)["status"]
        thread = conn.execute(
            "SELECT cwd FROM codex_conversation_threads WHERE conversation_key=?",
            (key,)).fetchone()
        out["thread"] = None if thread is None else thread[0]
    finally:
        conn.close()
    for account in accounts:
        conn = ns["open_conversations_db"]()
        try:   # a fresh scoped connection, as every qualified request gets
            ns["scope_conversations_db_to_account"](conn, account)
            row = _list_row(conn, key)
            out[f"scoped:{account}"] = None if row is None else (
                row["project_label"], row["count"])
        finally:
            conn.close()
    return out


def _publication(ns, key):
    rows = _conv_rows(ns, "SELECT published_revision, legacy_uncertified "
                          "FROM codex_conversation_publications "
                          "WHERE conversation_key=?", (key,))
    return rows[0] if rows else None


def _render_revision(ns, key):
    rows = _conv_rows(ns, "SELECT render_revision FROM codex_conversation_rollups "
                          "WHERE conversation_key=?", (key,))
    return rows[0][0] if rows else None


def _message_ids(ns, key):
    return [row[0] for row in _conv_rows(
        ns, "SELECT id FROM codex_conversation_messages "
            "WHERE conversation_key=? ORDER BY id", (key,))]


def test_the_conversation_pass_first_publishes_nothing_until_certified(env):
    """Barrier case 1: the conversation pass runs before cache accounting."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    stats = _conv(ns)
    assert _conv_rows(ns, "SELECT COUNT(*) FROM codex_conversation_messages") == [(0,)]
    assert stats.barrier_files_deferred == 1
    _sync(ns)
    key = _key(ns, a)
    assert _surfaces(ns, key)["list"] is None      # certified, not yet published
    stats = _conv(ns)
    assert stats.barrier_keys_published == 1
    accounts = _accounts(ns)
    s = _surfaces(ns, key, accounts)
    assert s["list"][1] == _label(ns, "/tmp/proj-a") and s["list"][2] > 0
    assert (s["detail"][0], s["export"], s["preflight"]) == ("ok", "ok", "ok")
    assert s["thread"] == "/tmp/proj-a" and s["search"] and s["find"] > 0
    assert all(s[f"scoped:{acct}"] is not None for acct in accounts)


def test_a_new_conversation_waits_for_every_readable_contributor(env):
    """Barrier cases 2 and 4 for a conversation with no publication yet."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync_stopping_after(ns, 1)      # accounting stops after the message-bearing file
    key = _key(ns, a)
    assert _certificate(ns, key) is None
    stats = _conv(ns)
    assert stats.barrier_files_deferred == 2
    assert stats.barrier_keys_published == 0
    pending = _surfaces(ns, key)
    assert pending["list"] is None and pending["search"] is False
    assert pending["detail"][0] == "normalization_pending"
    assert pending["export"] == pending["preflight"] == "normalization_pending"
    _sync(ns)
    _conv(ns)
    accounts = _accounts(ns)
    s = _surfaces(ns, key, accounts)
    assert s["thread"] == "/tmp/proj-b"
    assert s["list"][1] == _label(ns, "/tmp/proj-b")
    for acct in accounts:
        assert s[f"scoped:{acct}"][0] == _label(ns, "/tmp/proj-b")


def test_an_existing_publication_never_takes_unready_metadata(env):
    """Barrier case 2 for a published conversation: the I7 per-file
    counterexample (a metadata-only contributor emits no message)."""
    ns, provider, day, _ = env
    a, u, b = day / _name(11, 1), day / _name(11, 5), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(u, token_records(1, session_id=OTHER_SESSION, model="gpt-5"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-a"
    _append(u, token_records(2, session_id=OTHER_SESSION, model="gpt-5",
                             start_minute=20))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync_stopping_after(ns, 1)      # U is accounted; B is not
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    _sync(ns)
    stats = _conv(ns)
    assert stats.barrier_keys_published == 1
    after = _surfaces(ns, key, accounts)
    assert after["thread"] == "/tmp/proj-b"
    assert after["list"][1] == _label(ns, "/tmp/proj-b") != before["list"][1]
    assert after["list"][2] == before["list"][2]       # no message was added
    for acct in accounts:
        assert after[f"scoped:{acct}"][0] == _label(ns, "/tmp/proj-b")


def test_a_replacement_keeps_the_old_publication_until_certified(env):
    """Barrier case 3: G0's cursor never certifies G1, and G0 stays published."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(6, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    zpath = _compress(a)
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    _replace_container(zpath, _lines(conversation_records(
        1, session_id=SESSION, label="z", cwd="/tmp/proj-z")))
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    after = _surfaces(ns, key, accounts)
    assert after["thread"] == "/tmp/proj-z"
    assert after["list"][2] < before["list"][2]


def test_a_torn_final_line_publishes_and_converges(env):
    """Barrier case 5."""
    ns, provider, day, _ = env
    records = conversation_records(3, session_id=SESSION, label="a")
    head, tail = _lines(records[:-4]), _lines(records[-4:])
    zpath = day / (_name(11, 1) + ".zst")
    zpath.write_bytes(zstd_bytes(head + tail[:25]))
    _sync(ns)
    _conv(ns)
    key = _key(ns, day / _name(11, 1))
    first = _surfaces(ns, key)
    assert first["detail"][0] == "ok" and first["list"] is not None
    _replace_container(zpath, head + tail)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key) == first
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    after = _surfaces(ns, key)
    assert after["list"][2] > first["list"][2]
    assert after["find"] > first["find"]


@pytest.mark.parametrize("hold", ["decoder-unavailable", "decode-error"])
def test_a_degraded_contributor_never_withdraws_a_publication(
        env, monkeypatch, hold):
    """Barrier case 6 through the readers."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a, b, o = day / _name(11, 1), day / _name(12, 2), day / _name(13, 3)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    good = a.read_bytes()
    zpath = a.with_name(a.name + ".zst")
    if hold == "decoder-unavailable":
        zpath.write_bytes(zstd_bytes(good))
        monkeypatch.setattr(leaf, "resolve_zstd_backend", lambda: leaf.UNAVAILABLE)
    else:
        zpath.write_bytes(zstd_bytes(good)[:-7])        # a truncated frame
    a.unlink()
    write_rollout(o, conversation_records(1, session_id=OTHER_SESSION, label="o"))
    if hold == "decode-error":
        _compress(o)        # with a decoder, the unrelated conversation is compressed
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    assert _surfaces(ns, _key(ns, o))["list"] is not None    # others progress
    monkeypatch.setattr(leaf, "resolve_zstd_backend", REAL_RESOLVE)
    _replace_container(zpath, good)                          # recovery
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    assert _publication(ns, key)[0] > 0


# Review P1-1, P2-4 and N2: barrier case 6 over the matrix Task 10 states.
# The test above is known membership with retained evidence; the three below
# are unknown membership with retained evidence, a mixed legacy-unverified
# and live-plain conversation, and unknown membership without retained
# evidence.


@pytest.mark.parametrize("hold", HOLDS)
def test_an_unknown_membership_contributor_never_withdraws_a_publication(
        env, monkeypatch, hold):
    """Unknown membership, retained accepted evidence, through the readers
    (review N2): the degraded contributor is an exclusion, the key seals over
    the readable contributor and its publication advances, every surface
    keeps the thread the degraded contributor supplied (from its retained
    facts), and no "(unassigned)" label appears."""
    ns, provider, day, _ = env
    # `b` sorts first, so the full walk (path order, an unconditional thread
    # upsert) leaves `a`, the metadata-bearing contributor, as the last writer.
    a, b = day / _name(11, 1), day / _name(10, 2)
    write_rollout(a, _meta_only(SESSION, "/tmp/proj-a")
                  + conversation_records(2, session_id=SESSION, label="a",
                                         cwd="/tmp/proj-a")[1:])
    write_rollout(b, token_records(2, session_id=SESSION, start_minute=20))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-a"
    _forget_membership(ns, a)
    _degrade(ns, monkeypatch, a, hold)
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _cache().run_codex_barrier_membership_scan(wait_for_requests=False)
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    # 92e06df85 semantics: the walk cannot see `a.jsonl.zst` and prunes `a`
    # with the thread row that names it, and `b`'s id-only session_meta
    # supplies no cwd, so every surface loses `a`'s thread (I7). Round 1
    # froze this key instead.
    cert = _certificate(ns, key)
    assert cert is not None, "a degraded contributor froze its key"
    assert [e["path"] for e in cert["exclusions"]] == [str(a)]
    assert _publication(ns, key)[0] == cert["revision"]
    assert _surfaces(ns, key, accounts) == before
    labels = [row[0] for row in _conv_rows(
        ns, "SELECT project_label FROM codex_conversation_rollups")]
    assert "(unassigned)" not in labels


def test_a_mixed_legacy_unverified_and_live_plain_conversation_publishes(
        env, monkeypatch):
    """Review N2 through the readers: a conversation fed by a legacy
    contributor that ends `verification-unverified` and by a live plain one
    publishes the plain contributor's new turn, keeps the legacy thread and
    project label, and keeps the legacy contributor's retained messages."""
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
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-a"
    _forget_membership(ns, a)
    _degrade(ns, monkeypatch, a, "verification-unverified")
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    _append(b, conversation_records(3, session_id=SESSION, label="b")[-4:])
    _sync(ns)
    _conv(ns)
    cert = _certificate(ns, key)
    assert cert is not None, "the legacy contributor froze the conversation"
    assert _publication(ns, key)[0] == cert["revision"]
    after = _surfaces(ns, key, accounts)
    assert after["thread"] == "/tmp/proj-a"
    assert after["list"][1] == before["list"][1] == _label(ns, "/tmp/proj-a")
    assert after["list"][2] > before["list"][2]       # b's new turn published
    assert after["find"] == before["find"] + 1        # "Answer b2"
    assert after["search"] and after["detail"][0] == "ok"
    for acct in accounts:
        assert after[f"scoped:{acct}"][0] == _label(ns, "/tmp/proj-a")
    assert _conv_rows(ns, "SELECT COUNT(*) FROM codex_conversation_messages "
                          "WHERE source_path=?", (str(a),))[0][0] > 0


@pytest.mark.parametrize("hold", ["decoder-unavailable", "decode-error"])
def test_a_never_ingested_unreadable_contributor_changes_no_reader(
        env, monkeypatch, hold):
    """Unknown membership without retained evidence, through the readers: an
    unreadable compressed rollout that was never ingested is counted (Task
    10), and the published conversation it may feed keeps every surface."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a, c = day / _name(11, 1), day / _name(14, 4)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    write_rollout(c, conversation_records(1, session_id=SESSION, label="c",
                                          cwd="/tmp/proj-c"))
    zc = _compress(c)
    if hold == "decode-error":
        zc.write_bytes(zc.read_bytes()[:-7])
    else:
        monkeypatch.setattr(leaf, "resolve_zstd_backend",
                            lambda: leaf.UNAVAILABLE)
    _sync(ns)
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    _conv(ns)
    # Passes on 92e06df85, where the container is invisible; it pins that the
    # phase-2 publication never advances a key it cannot certify.
    assert _surfaces(ns, key, accounts) == before


def test_a_lost_cache_side_thread_never_retracts_a_published_thread(env):
    """Review r3 F4: the conversations side guards the published thread too.
    The cache-side retained thread (latest certificate, entry snapshot and
    accounting row) is gone, as after `rm cache.db`, and the only contributor
    is then excluded with unknown membership, so its retained-generation row
    carries no metadata and the seal is thread None with an exclusion.
    Publishing that certificate keeps the published thread."""
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    good = a.read_bytes()
    zpath = _compress(a)
    # A readable newest sibling keeps the provider root positively recognized
    # while the candidate becomes unreadable; this isolates U6 from root safety.
    write_rollout(day / _name(20, 9), token_records(1, session_id="keeper-0009"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-a"
    first = _publication(ns, key)
    assert first is not None and first[0] > 0, "nothing published the key"
    conn = ns["open_cache_db"]()
    try:   # the cache-side retained thread is gone
        for table in ("codex_barrier_certificates", "codex_barrier_entry_threads",
                      "codex_conversation_threads"):
            conn.execute(f"DELETE FROM {table} WHERE conversation_key=?", (key,))
        conn.commit()
    finally:
        conn.close()
    _forget_membership(ns, a)
    zpath.write_bytes(zstd_bytes(good)[:-7])           # a truncated frame
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    cert = _certificate(ns, key)
    # Non-vacuity: the seal really is thread None over an exclusion, and it
    # was published.
    assert cert is not None and cert["thread"] is None
    assert [(e["path"], e["reason"]) for e in cert["exclusions"]] == [
        (str(a), "decode-error")]
    assert _publication(ns, key)[0] == cert["revision"] > first[0]
    assert _surfaces(ns, key, accounts) == before
    assert _conv_rows(ns, "SELECT cwd FROM codex_conversation_published_threads "
                          "WHERE conversation_key=?", (key,)) == [("/tmp/proj-a",)]


@pytest.mark.parametrize("decoder", ["present", "absent"])
def test_compression_under_verification_changes_nothing_visible(
        env, monkeypatch, decoder):
    """Barrier case 7: no retraction on container change, no adoption churn."""
    ns, provider, day, _ = env
    leaf = sys.modules["_lib_codex_rollout_container"]
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(3, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    # A readable newest sibling keeps the provider root positively recognized
    # while the candidate becomes unreadable; this isolates U6 from root safety.
    write_rollout(day / _name(20, 9), token_records(1, session_id="keeper-0009"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    revision, ids = _render_revision(ns, key), _message_ids(ns, key)
    _compress(a)
    if decoder == "absent":
        monkeypatch.setattr(leaf, "resolve_zstd_backend", lambda: leaf.UNAVAILABLE)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    assert _render_revision(ns, key) == revision
    assert _message_ids(ns, key) == ids
    assert _publication(ns, key)[0] > 0        # certified without a derived write


def test_a_live_append_publishes_only_when_eligible(env):
    """Barrier case 8: no withdrawal on append, no early leak."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    _append(b, conversation_records(2, session_id=SESSION, label="b")[-4:])
    stats = _conv(ns)                       # the cache has not accounted it
    assert stats.barrier_files_deferred == 1
    assert _surfaces(ns, key, accounts) == before
    _sync_stopping_after(ns, 0)             # the hook's budget ends first
    _conv(ns)
    assert _surfaces(ns, key, accounts) == before
    _sync(ns)
    _conv(ns)
    after = _surfaces(ns, key, accounts)
    assert after["list"][2] > before["list"][2]
    assert after["detail"][0] == "ok" and after["thread"] == before["thread"]


def test_legacy_rollups_stay_visible_and_repair_in_place(env):
    """Barrier case 9: a correct and a stale legacy materialization."""
    ns, provider, day, _ = env
    x, y = day / _name(11, 1), day / _name(12, 2)
    write_rollout(x, conversation_records(2, session_id=SESSION, label="x",
                                          cwd="/tmp/proj-x"))
    write_rollout(y, conversation_records(2, session_id=OTHER_SESSION, label="y",
                                          cwd="/tmp/proj-y"))
    _sync(ns)
    _conv(ns)
    kx, ky = _key(ns, x), _key(ns, y)
    conn = ns["open_conversations_db"]()
    try:   # a stale materialization an earlier binary left behind
        conn.execute("UPDATE codex_conversation_rollups SET project_key='stale', "
                     "project_label='stale' WHERE conversation_key=?", (ky,))
        conn.commit()
    finally:
        conn.close()
    accounts = _accounts(ns)
    good_x = _surfaces(ns, kx, accounts)
    revision_x, revision_y = _render_revision(ns, kx), _render_revision(ns, ky)
    ids = (_message_ids(ns, kx), _message_ids(ns, ky))
    _compress(x)
    _compress(y)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, kx, accounts) == good_x        # visible immediately
    assert _surfaces(ns, ky, accounts)["list"][1] == "stale"
    assert _publication(ns, kx)[1] == 1 and _publication(ns, ky)[1] == 1
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, kx, accounts) == good_x
    assert _render_revision(ns, kx) == revision_x       # certified, no derived write
    repaired = _surfaces(ns, ky, accounts)
    assert repaired["list"][1] == _label(ns, "/tmp/proj-y")
    for acct in accounts:
        assert repaired[f"scoped:{acct}"][0] == _label(ns, "/tmp/proj-y")
    assert _render_revision(ns, ky) > revision_y        # repaired in place
    assert (_message_ids(ns, kx), _message_ids(ns, ky)) == ids
    assert _publication(ns, kx)[1] == 0 and _publication(ns, ky)[1] == 0


@pytest.mark.parametrize(
    "scenario", ["interrupted", "superseded", "newer-requirement"])
def test_publication_is_atomic_and_monotonic(env, monkeypatch, scenario):
    """Barrier case 10."""
    ns, provider, day, _ = env
    cc = _cache()
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    published = _publication(ns, key)[0]
    ids = _message_ids(ns, key)
    _append(b, conversation_records(2, session_id=SESSION, label="b")[-4:])
    _sync(ns)
    certified = _certificate(ns, key)["revision"]
    assert certified > published
    real_plan = cc._plan_codex_publication_units
    if scenario == "interrupted":
        def explode(conn, keys):
            raise sqlite3.OperationalError("injected interruption before commit")
        monkeypatch.setattr(cc, "_CODEX_PUBLICATION_PRECOMMIT_HOOK", explode)
        stats = _conv(ns)
        assert stats.barrier_retry is True
        assert _surfaces(ns, key, accounts) == before
        assert _message_ids(ns, key) == ids
        assert _publication(ns, key)[0] == published
        monkeypatch.setattr(cc, "_CODEX_PUBLICATION_PRECOMMIT_HOOK", None)
        _conv(ns)
        assert _publication(ns, key)[0] == certified
        assert _surfaces(ns, key, accounts)["list"][2] > before["list"][2]
    elif scenario == "superseded":
        newer = certified + 10**6

        def plan_then_compete(conn, work):
            units = real_plan(conn, work)
            assert not conn.in_transaction
            competitor = sqlite3.connect(
                str(sys.modules["_cctally_core"].CONVERSATIONS_DB_PATH), timeout=0)
            try:
                competitor.execute(
                    "UPDATE codex_conversation_publications "
                    "SET published_revision=? WHERE conversation_key=?",
                    (newer, key))
                competitor.commit()
            finally:
                competitor.close()
            return units

        monkeypatch.setattr(cc, "_plan_codex_publication_units", plan_then_compete)
        _conv(ns)
        assert _publication(ns, key)[0] == newer      # the newer publication stands
        assert _message_ids(ns, key) == ids           # the stale batch wrote nothing
        assert _surfaces(ns, key, accounts) == before
    else:
        def plan_then_rearm(conn, work):
            units = real_plan(conn, work)
            cache = ns["open_cache_db"]()
            try:   # a newer requirement lands after the certificate was captured
                cache.execute(
                    "UPDATE codex_barrier_population SET "
                    "required_revision=required_revision+5 WHERE conversation_key=?",
                    (key,))
                cache.execute(
                    "INSERT OR REPLACE INTO codex_barrier_repairs "
                    "(conversation_key, revision, reason) SELECT conversation_key, "
                    "required_revision, 'test' FROM codex_barrier_population "
                    "WHERE conversation_key=?", (key,))
                cache.commit()
            finally:
                cache.close()
            return units

        monkeypatch.setattr(cc, "_plan_codex_publication_units", plan_then_rearm)
        _conv(ns)
        assert _publication(ns, key)[0] == certified  # the captured one still publishes
        published_view = _surfaces(ns, key, accounts)
        assert published_view["list"][2] > before["list"][2]
        monkeypatch.setattr(cc, "_plan_codex_publication_units", real_plan)
        revision = _render_revision(ns, key)
        _sync(ns)
        _conv(ns)
        assert _publication(ns, key)[0] == certified + 5
        assert _surfaces(ns, key, accounts) == published_view
        assert _render_revision(ns, key) == revision


@pytest.mark.parametrize("heal", ["automatic", "rebuild", "conversation-first"])
def test_a_pruned_store_heals_through_every_reader(env, monkeypatch, heal):
    """T5 heal: rows, attribution and every consumer surface equal the
    never-pruned control, with no materialized "(unassigned)"."""
    ns, provider, day, _ = env
    cc = _cache()
    premech = day / _name(11, 2)
    decided = day / _name(12, 1)
    keeper = day / _name(20, 9)          # newest: keeps the root recognized
    write_rollout(premech, conversation_records(
        2, session_id="0199aaaa-bbbb-7ccc-8ddd-eeeeffff0b02", label="p",
        cwd="/tmp/proj-p"))
    with monkeypatch.context() as m:     # pre-#416 history: no decision anywhere
        m.setattr(cc, "_append_codex_file_account_decision", lambda **_kw: None)
        m.setattr(cc, "record_codex_file_account", lambda *_a, **_kw: None)
        _sync(ns)
    cache = ns["open_cache_db"]()
    try:
        root_key = cache.execute("SELECT source_root_key FROM codex_session_files "
                                 "WHERE path=?", (str(premech),)).fetchone()[0]
        cache.execute("DELETE FROM codex_file_incarnations WHERE file_identity=?",
                      (cc.codex_file_key_for_entry_path(root_key, str(premech)),))
        cache.execute("UPDATE codex_session_entries SET account_key=NULL "
                      "WHERE source_path=?", (str(premech),))
        cache.commit()
    finally:
        cache.close()
    write_rollout(decided, conversation_records(
        3, session_id="0199aaaa-bbbb-7ccc-8ddd-eeeeffff0b01", label="d",
        cwd="/tmp/proj-d"))
    write_rollout(keeper, token_records(1, session_id="keeper-0009",
                                        start_minute=50))
    _sync(ns)
    _conv(ns)
    keys = sorted(row[0] for row in _conv_rows(
        ns, "SELECT conversation_key FROM codex_conversation_rollups"))
    assert len(keys) == 2
    accounts = _accounts(ns)
    control = {key: _surfaces(ns, key, accounts) for key in keys}
    _compress(premech)
    _compress(decided)
    _unfixed_prune(ns, monkeypatch)
    gone = (str(premech), str(decided))
    count_sql = ("SELECT COUNT(*) FROM codex_conversation_messages "
                 "WHERE source_path IN (?,?)")
    assert _conv_rows(ns, count_sql, gone) == [(0,)]
    (provider / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    if heal == "conversation-first":
        _conv(ns)
        assert _conv_rows(ns, count_sql, gone) == [(0,)]
        _sync(ns)
    elif heal == "rebuild":
        # Exercise the documented entrypoint: its off-hook provenance preparation
        # precedes the raw historical walk, as Task 10 requires.
        import argparse
        assert cc.cmd_cache_sync(argparse.Namespace(
            source="codex", rebuild=True, prune_orphans=False,
            prune_conversations=False)) == 0
    else:
        _sync(ns)
    # Automatic fixture workers are intentionally not spawned. Drive the real
    # admitted provenance worker so its queued history hold can settle.
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome in {"settled", "already-settled"}
    _sync(ns)
    _conv(ns)
    assert {key: _surfaces(ns, key, accounts) for key in keys} == control
    labels = [row[0] for row in _conv_rows(
        ns, "SELECT project_label FROM codex_conversation_rollups")]
    assert "(unassigned)" not in labels


@pytest.mark.parametrize("first", ["cache", "conversations"])
def test_a_drained_contributor_deletion_keeps_readers_coherent(env, first):
    """Review P3-10: one contributor of a published two-file conversation is
    legitimately deleted. Each prune executor deletes in its own per-identity
    transaction; the cache executor re-arms the key in the same transaction
    as its deletion, so the key is queued and repaired, and every reader sees
    the old publication or the new one, never a mix, whichever executor
    commits first."""
    ns, provider, day, _ = env
    cc = _cache()
    a, b = day / _name(11, 1), day / _name(12, 2)
    keeper = day / _name(20, 9)          # newest: keeps the root recognized
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    write_rollout(keeper, token_records(1, session_id="keeper-0009",
                                        start_minute=50))
    _compress(a)
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-b"   # the later contributor's thread
    ids = _message_ids(ns, key)
    b.unlink()
    _sync(ns)        # records cache prune debt; the hook path deletes nothing
    _conv(ns)        # records conversations prune debt
    assert _surfaces(ns, key, accounts) == before
    executors = [cc.run_codex_prune_drain, cc.run_codex_conversation_prune_drain]
    if first == "conversations":
        executors.reverse()
    executors[0](wait_for_requests=False)
    assert _surfaces(ns, key, accounts) == before      # the old publication, whole
    executors[1](wait_for_requests=False)
    assert _surfaces(ns, key, accounts) == before
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_repairs "
                    "WHERE conversation_key=?", (key,)) == (1,)
    _sync(ns)
    cc.run_codex_barrier_seal_drain(wait_for_requests=False)
    _conv(ns)
    after = _surfaces(ns, key, accounts)
    # 92e06df85 semantics: `a.jsonl.zst` is invisible, so the conversation never
    # holds a's messages and the deletion withdraws it entirely.
    assert after["thread"] == "/tmp/proj-a"            # the new publication
    assert _message_ids(ns, key) == ids
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_repairs "
                    "WHERE conversation_key=?", (key,)) == (0,)
    labels = [row[0] for row in _conv_rows(
        ns, "SELECT project_label FROM codex_conversation_rollups")]
    assert "(unassigned)" not in labels


def test_rebuild_replay_and_backfill_republish_the_same_state(env):
    """The explicit conversations rebuild, the retained-event contract replay
    and the find-projection backfill all read through the barrier."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    accounts = _accounts(ns)
    before = _surfaces(ns, key, accounts)
    assert before["thread"] == "/tmp/proj-b"
    stats = _conv(ns, rebuild=True)
    assert stats.barrier_keys_published == 1
    assert _surfaces(ns, key, accounts) == before
    conn = ns["open_conversations_db"]()
    try:   # the next open replays retained events under a stale contract
        conn.execute("UPDATE cache_meta SET value='0' "
                     "WHERE key='codex_conversation_contract_version'")
        conn.commit()
    finally:
        conn.close()
    assert _surfaces(ns, key, accounts) == before
    conn = ns["open_conversations_db"]()
    try:
        conn.execute("DELETE FROM codex_find_projection")
        conn.execute("DELETE FROM cache_meta WHERE key IN "
                     "('codex_find_projection_backfill_cursor',"
                     " 'codex_find_projection_complete_version')")
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                     "VALUES('codex_find_projection_backfill_pending','1')")
        conn.commit()
        while not _cache().run_codex_find_projection_backfill(conn)["complete"]:
            conn.commit()
        conn.commit()
    finally:
        conn.close()
    assert _surfaces(ns, key, accounts) == before


def test_the_frontier_schedules_the_publication_backlog(env):
    import _lib_ingest_frontier as frontier
    ns, provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    _sync(ns)
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (
            True, frozenset({str(a)}))
    finally:
        conn.close()
    stats = _conv(ns)
    assert frontier.conversation_sync_certifiable("full", stats)
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (False, frozenset())
    finally:
        conn.close()


def test_a_debt_hold_still_schedules_a_due_publication(env, monkeypatch):
    """Review N3: Task 4's debt hold defers only the full walk. A publication
    the cache has sealed still runs, as a targeted pass over its
    contributors, while conversations prune debt drains."""
    import _lib_ingest_frontier as frontier
    ns, _provider, day, _ = env
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    _sync(ns)
    monkeypatch.setattr(
        frontier, "_debt_hold_plan",
        lambda owner, prov, conn: frontier.FrontierPlan(
            prov, "caught_up", reason="debt_hold"))
    roots = tuple(r.walk_root for r in _cache()._codex_provider_roots())
    state = frontier.ConversationSyncFrontier(ns["APP_DIR"] / "frontier-n3")
    conn = ns["open_conversations_db"]()
    try:
        plan = state.plan_provider("codex", conn, roots=roots)
    finally:
        conn.close()
    assert (plan.mode, plan.reason, plan.paths) == (
        "targeted", "publication_backlog", frozenset({str(a)}))


def test_family_links_and_the_cli_follow_the_last_good_threads(env, capsys):
    """The unqualified family readers (`_children_of`, `_parent_of`, the spawn
    links behind `_attach_spawn_child_links`, `codex_conversation_source_paths`)
    and the CLI `transcript export` see a compressed child only once it is
    published; `transcript search` goes through `neutral_search`, which
    `_surfaces` covers."""
    ns, provider, day, _ = env
    parent, child = day / _name(11, 1), day / _name(12, 2)
    write_rollout(parent, conversation_records(2, session_id=SESSION, label="p",
                                               cwd="/tmp/proj-a"))
    _sync(ns)
    _conv(ns)
    parent_key = _key(ns, parent)
    records = conversation_records(2, session_id=OTHER_SESSION, label="c",
                                   cwd="/tmp/proj-a")
    records[0]["payload"].update({
        "forked_from_id": SESSION, "parent_thread_id": SESSION,
        "agent_path": "/root/child", "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": SESSION, "agent_path": "/root/child",
            "depth": 1}}}})
    write_rollout(child, records)
    _compress(child)

    def family():
        conn = ns["open_conversations_db"]()
        try:
            kids = [c["conversation_key"]
                    for c in q._children_of(conn, parent_key, SPEED)]
            paths = set(q.codex_conversation_source_paths(conn, parent_key))
            link = q._spawn_child_link(conn, parent_key, "/root/child")
            return kids, paths, link
        finally:
            conn.close()

    _conv(ns)                       # the conversation pass first: deferred
    assert _conv_rows(ns, "SELECT COUNT(*) FROM codex_conversation_messages "
                          "WHERE source_path=?", (str(child),)) == [(0,)]
    _sync(ns)                       # certified, not yet published
    assert _one(ns, "SELECT 1 FROM codex_session_files WHERE path=?",
                (str(child),)) is not None
    child_key = _key(ns, child)
    _kids, paths, link = family()
    assert str(child) not in paths and link is None
    capsys.readouterr()
    assert ns["main"](["transcript", "export", child_key]) == 1
    # D5: the barrier-pending cause prints the generic note; exit 1 unchanged.
    assert ("transcript: Codex conversation is not yet normalized — retry shortly"
            in capsys.readouterr().err)
    _conv(ns)                       # published
    kids, paths, link = family()
    assert kids == [child_key]
    assert str(child) in paths
    assert link is not None and link["conversation_key"] == child_key
    conn = ns["open_conversations_db"]()
    try:
        assert q._parent_of(conn, child_key)["conversation_key"] == parent_key
    finally:
        conn.close()
    capsys.readouterr()
    assert ns["main"](["transcript", "export", child_key]) == 0
    assert "Answer c0" in capsys.readouterr().out


def test_plain_only_conversations_keep_todays_behavior(env):
    """A conversation outside the population publishes exactly as before.

    This is not the R4 reproduction: it never interleaves a metadata-only
    contributor with the conversation pass, so it cannot show R4. Task 15
    Step 4 runs the plain-only variant of T5 case 2 against 92e06df85."""
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    _sync(ns)
    stats = _conv(ns)
    assert (stats.barrier_files_deferred, stats.barrier_keys_published) == (0, 0)
    assert _conv_rows(ns, "SELECT COUNT(*) FROM codex_conversation_publications") == [(0,)]
    conn = ns["open_conversations_db"]()
    try:
        view = conn.execute("SELECT * FROM codex_conversation_threads "
                            "ORDER BY conversation_key").fetchall()
        raw = conn.execute("SELECT * FROM cache_db.codex_conversation_threads "
                           "ORDER BY conversation_key").fetchall()
    finally:
        conn.close()
    assert view == raw


def test_publication_statements_build_no_history_sized_temp_structures(env):
    """#901 G1 for every statement the conversation barrier adds."""
    import _sql_plan_guard as guard
    ns, provider, day, _ = env
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a"))
    _compress(a)
    write_rollout(b, conversation_records(1, session_id=SESSION, label="b"))
    _sync(ns)
    with guard.capture_sql_plans() as recorder:
        _conv(ns)
        key = _key(ns, a)
        _surfaces(ns, key, _accounts(ns))
    names = ("codex_conversation_publications", "codex_conversation_published_threads",
             "codex_barrier", "codex_last_good_threads")
    findings = [f for f in guard.temp_findings(recorder.statements)
                if any(name in f.statement.sql for name in names)]
    # The list SQL names the shadow table, while its expanded plan names the
    # last-good view. Judge the actual call site, never just SQL spelling.
    lists = [st for st in recorder.statements
             if "_lib_codex_conversation_query._stored_browse_page_queries"
             in st.call_sites and "SELECT r.conversation_key," in st.sql]
    assert lists, "the real stored list page must be assessed"
    assert all("FROM codex_conversation_rollups r" in st.sql for st in lists)
    expanded = [st for st in recorder.statements
                if st in lists or any(name in line for name in names for line in st.plan)]
    list_findings = [f for f in guard.temp_findings(expanded)
                     if not (f.statement in lists
                             and f.detail == "USE TEMP B-TREE FOR ORDER BY")]
    for st in lists:
        assert not any("<explain failed:" in line for line in st.plan)
        print("TASK11_R01_LIST_PLAN " + json.dumps({
            "sql": st.sql, "plan": st.plan, "callSites": st.call_sites}))
    # The existing ORDER BY sorter predates the barrier. Its unchanged shape
    # is outside this correction; every new materialization/index is judged.
    assert findings + list_findings == [], guard.format_findings(
        findings + list_findings)


def test_shared_contributor_publication_keeps_every_conversation(env):
    """One shared file's keys publish as a settled unit without losing a key."""
    ns, _provider, day, _ = env
    shared, metadata = day / _name(11, 1), day / _name(12, 2)
    write_rollout(shared, conversation_records(2, session_id=SESSION, label="a",
                                              cwd="/tmp/proj-a")
                  + conversation_records(2, session_id=OTHER_SESSION, label="b",
                                         cwd="/tmp/proj-b"))
    _compress(shared)
    write_rollout(metadata, _meta_only(SESSION, "/tmp/proj-final"))
    _sync(ns)
    stats = _conv(ns)
    conn = ns["open_conversations_db"]()
    try:
        keys = [r[0] for r in conn.execute(
            "SELECT conversation_key FROM codex_conversation_rollups ORDER BY conversation_key")]
        assert len(keys) == 2
        assert all(q.get_codex_conversation(conn, key, effective_speed=SPEED,
                                           limit=0)["items"] for key in keys)
        assert conn.execute("SELECT COUNT(*) FROM codex_conversation_publications "
                            "WHERE published_revision>0").fetchone() == (2,)
    finally:
        conn.close()
    assert stats.barrier_keys_published == 2
    accounts = _accounts(ns)
    states = [_surfaces(ns, key, accounts) for key in keys]
    assert {s["thread"] for s in states} == {"/tmp/proj-final", "/tmp/proj-b"}
    assert all(s["find"] == 2 and s["search"] for s in states)
    assert all(s[f"scoped:{account}"] for s in states for account in accounts)


def test_publication_planning_reads_one_cache_snapshot_between_key_facts(env, monkeypatch):
    """CD01: accounting commits BETWEEN two individual key-fact captures.

    A new plain file feeds both keys of the unchanged compressed contributor.
    A mixed snapshot publishes its new A messages under old A metadata.
    """
    ns, _provider, day, _ = env
    cc = _cache()
    shared, added = day / _name(11, 1), day / _name(12, 2)
    write_rollout(shared,
                  conversation_records(2, session_id=SESSION, label="a", cwd="/tmp/old-a")
                  + conversation_records(2, session_id=OTHER_SESSION, label="b", cwd="/tmp/old-b"))
    _compress(shared)
    _sync(ns)
    cache = ns["open_cache_db"]()
    try:
        keys = [r[0] for r in cache.execute(
            "SELECT conversation_key FROM codex_barrier_population ORDER BY conversation_key")]
    finally:
        cache.close()
    assert len(keys) == 2
    old = {key: _certificate(ns, key) for key in keys}
    real = cc._codex_publication_facts
    captured = []
    changed = []

    def interleave(conn, key):
        result = real(conn, key)
        captured.append((key, result.certificate["revision"]))
        if not changed:
            changed.append(key)
            write_rollout(added,
                          conversation_records(1, session_id=SESSION, label="new-a", cwd="/tmp/new-a")
                          + conversation_records(1, session_id=OTHER_SESSION, label="new-b", cwd="/tmp/new-b"))
            # Separate cache writer connection: the commit occurs while the
            # conversation planner is paused, before it reads the next key.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(_sync, ns).result(timeout=60)
            assert all(_certificate(ns, k)["revision"] > old[k]["revision"] for k in keys)
        return result

    monkeypatch.setattr(cc, "_codex_publication_facts", interleave)
    stats = _conv(ns)
    assert len(captured) == 2 and changed == [keys[0]]
    assert stats.barrier_keys_published == 2
    accounts = _accounts(ns)
    for key in keys:
        state = _surfaces(ns, key, accounts)
        cwd = old[key]["thread"]["cwd"]
        assert state["thread"] == cwd
        assert state["list"][1:] == (_label(ns, cwd), 4)
        assert state["detail"] == ("ok", 4)
        assert state["find"] == 2 and state["search"]
        assert state["export"] == state["preflight"] == "ok"
        assert all(state[f"scoped:{a}"] == (_label(ns, cwd), 4) for a in accounts)
        assert _publication(ns, key) == (old[key]["revision"], 0)
        assert _render_revision(ns, key) is not None
    # The new seal remains outstanding. Recovery needs no further accounting
    # change and publishes both messages and metadata at the newer revision.
    monkeypatch.setattr(cc, "_codex_publication_facts", real)
    before_ids = {key: _message_ids(ns, key) for key in keys}
    before_render = {key: _render_revision(ns, key) for key in keys}
    stats = _conv(ns)
    assert stats.barrier_keys_published == 2
    for key in keys:
        latest = _certificate(ns, key)
        state = _surfaces(ns, key, accounts)
        cwd = latest["thread"]["cwd"]
        assert state["thread"] == cwd and cwd.startswith("/tmp/new-")
        assert state["list"][1:] == (_label(ns, cwd), 6)
        assert state["detail"] == ("ok", 6) and state["find"] == 3
        assert all(state[f"scoped:{a}"] == (_label(ns, cwd), 6) for a in accounts)
        assert state["search"] and state["export"] == state["preflight"] == "ok"
        assert _publication(ns, key) == (latest["revision"], 0)
        assert set(before_ids[key]) < set(_message_ids(ns, key))
        assert _render_revision(ns, key) > before_render[key]


def test_unqualified_certificate_contributor_keeps_retry_without_a_new_seal(env, monkeypatch):
    """CD02: temporary qualification failure cannot acknowledge the seal."""
    import _lib_ingest_frontier as frontier
    import _lib_codex_publication_barrier as barrier
    ns, _provider, day, _ = env
    cc = _cache()
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a", cwd="/tmp/old"))
    _compress(a)
    write_rollout(b, _meta_only(SESSION, "/tmp/final"))
    _sync(ns)
    key = _key(ns, a)
    seal = _one(ns, "SELECT value FROM cache_meta WHERE key=?", (barrier.SEAL_SEQ_KEY,))
    real = cc._qualify_codex_targets
    calls = []

    def unavailable(paths):
        if paths == {str(b)}:
            calls.append(paths)
            return []
        return real(paths)

    monkeypatch.setattr(cc, "_qualify_codex_targets", unavailable)
    stats = _conv(ns, only_paths={str(a)})
    assert calls and stats.barrier_keys_deferred == 1
    assert stats.barrier_retry, "unplannable sealed contributor must retain retry debt"
    assert not frontier.conversation_sync_certifiable("targeted", stats, expected_paths=1)
    assert not frontier.conversation_sync_certifiable("full", stats)
    assert _publication(ns, key) is None
    assert _surfaces(ns, key)["list"] is None
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (True, frozenset({str(a), str(b)}))
        assert conn.execute("SELECT value FROM cache_meta WHERE key=?",
                            (barrier.PUBLISHED_SEQ_KEY,)).fetchone() is None
    finally:
        conn.close()
    monkeypatch.setattr(cc, "_qualify_codex_targets", real)
    assert _one(ns, "SELECT value FROM cache_meta WHERE key=?", (barrier.SEAL_SEQ_KEY,)) == seal
    stats = _conv(ns)       # unchanged restoration, no accounting or hook event
    assert stats.barrier_keys_published == 1 and not stats.barrier_retry
    assert frontier.conversation_sync_certifiable("full", stats)
    state = _surfaces(ns, key, _accounts(ns))
    assert state["thread"] == "/tmp/final" and state["detail"] == ("ok", 4)
    assert state["list"][1:] == (_label(ns, "/tmp/final"), 4)
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (False, frozenset())
    finally:
        conn.close()


def test_parent_list_probes_search_nonempty_snapshot_branches_for_found_and_missing_parents(env):
    """CD05: missing parents must not exhaust retained published/entry rows."""
    import _sql_plan_guard as guard
    ns, _provider, day, _ = env
    ids = [f"{i:08d}-1111-4111-8111-{i:012d}" for i in range(1, 8)]
    paths = [day / _name(11 + i, i + 1) for i in range(6)]
    parents = [None, None, ids[0], ids[1], ids[6], ids[5]]
    for i, path in enumerate(paths):
        records = conversation_records(1, session_id=ids[i], label=f"parent-plan-{i}",
                                       cwd=f"/tmp/parent-plan-{i}")
        if parents[i] is not None:
            records[0]["payload"].update({
                "forked_from_id": parents[i], "parent_thread_id": parents[i]})
        write_rollout(path, records)
        if i != 1:               # one plain legacy parent for the entry branch
            _compress(path)
    _sync(ns)
    _conv(ns)
    keys = [_key(ns, path) for path in paths]
    _compress(paths[1])
    _sync(ns)                    # admits legacy parent; no conversation pass yet
    assert _one(ns, "SELECT COUNT(*) FROM codex_barrier_entry_threads "
                    "WHERE conversation_key=?", (keys[1],)) == (1,)
    assert _publication(ns, keys[1]) is None
    assert _conv_rows(ns, "SELECT COUNT(*) FROM codex_conversation_published_threads")[0][0] >= 5
    cases = [("published-parent", 2, keys[0]), ("entry-parent", 3, keys[1]),
             ("absent-parent", 4, None), ("self-parent-excluded", 5, None)]
    assessed = []
    for case, child, expected in cases:
        with guard.capture_sql_plans() as recorder:
            conn = ns["open_conversations_db"]()
            try:
                project = _list_row(conn, keys[child])["project_key"]
                rows = q.list_codex_conversations(
                    conn, effective_speed=SPEED, project_key=project, limit=5)["rows"]
            finally:
                conn.close()
        assert len(rows) == 1 and rows[0]["conversation_key"] == keys[child]
        parent = rows[0]["parent"]
        assert (None if parent is None else parent["conversation_key"]) == expected
        pages = [st for st in recorder.statements
                 if "_lib_codex_conversation_query._stored_browse_page_queries"
                 in st.call_sites and "SELECT r.conversation_key," in st.sql]
        assert pages, "assess the actual list statement for each parent case"
        for st in pages:
            print("TASK11_R02_PARENT_PLAN " + json.dumps({
                "case": case, "expectedParent": expected, "sql": st.sql,
                "plan": st.plan, "callSites": st.call_sites}))
            assert not any("<explain failed:" in line for line in st.plan)
            assert not any("MATERIALIZE" in line or "AUTOMATIC" in line for line in st.plan)
            assessed.append(st)
    for st in assessed:           # capture found AND missing cases before RED
        for alias, index in (("w", "idx_codex_published_threads_parent"),
                             ("e", "idx_codex_barrier_entry_threads_parent")):
            assert not any(line.startswith(f"SCAN {alias}") for line in st.plan), st.plan
            assert any(f"SEARCH {alias} USING COVERING INDEX {index} " in line
                       and "(source_root_key=? AND native_thread_id=?)" in line
                       for line in st.plan), st.plan
