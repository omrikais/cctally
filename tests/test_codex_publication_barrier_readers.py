"""#968 T5: the publication barrier through every consumer surface (5.11)."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
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

import _lib_codex_conversation_query as q  # noqa: E402
import _lib_conversation_dispatch as disp  # noqa: E402
from _codex_zstd_fixtures import (  # noqa: E402
    conversation_records, token_records, write_rollout, zstd_bytes)
from conftest import load_script, redirect_paths  # noqa: E402
from test_codex_bounded_ingest import _auth_json  # noqa: E402
from test_codex_publication_barrier import (  # noqa: E402,F401 (env is a fixture)
    HOLDS, OTHER_SESSION, REAL_RESOLVE, SESSION, _cache, _certificate,
    _compress, _degrade, _forget_membership, _key, _meta_only, _name, _one,
    _replace_container, _sync, _sync_stopping_after, _unfixed_prune,
    _with_quota, env)

SPEED = "standard"
TITLE_SESSION = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0a07"


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
    # #939 D5 residual: an uncovered deferral is named, with its cause, and no
    # publication; a residual that missed it would let writeIo settle early.
    assert set(stats.barrier_deferred_keys) == {str(a)}
    assert stats.barrier_deferred_targets[str(a)][1] is False
    assert stats.barrier_retry_causes == {"uncovered"}
    assert stats.barrier_published_paths == set()
    _sync(ns)
    key = _key(ns, a)
    assert _surfaces(ns, key)["list"] is None      # certified, not yet published
    stats = _conv(ns)
    assert stats.barrier_keys_published == 1
    # covered, deferred in phase one and published in phase two of one pass
    assert stats.barrier_deferred_keys[str(a)] == frozenset({key})
    assert stats.barrier_deferred_targets[str(a)][1] is True
    assert stats.barrier_published_paths == {str(a)}
    assert stats.barrier_retry_causes == set()
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
    # #939 D5 residual: a covered deferral left unpublished (no seal) beside an
    # uncovered one; both stay in the residual, neither as published.
    assert stats.barrier_deferred_keys == {str(a): frozenset({key}),
                                           str(b): frozenset({key})}
    assert stats.barrier_deferred_targets[str(a)][1] is True
    assert stats.barrier_deferred_targets[str(b)][1] is False
    assert stats.barrier_published_paths == set()
    assert "uncovered" in stats.barrier_retry_causes
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
    """Barrier case 9: a correct and two stale legacy materializations.

    ``y`` is stale in a thread-derived field, which the first full pass's
    rollup reconcile repairs from the last-good view while its replacement
    container still awaits verification (#972 §5.8 rule 1, revisions 5 and
    6). ``z`` is stale only in its title, a field the reconcile never
    compares: it stays visible while its publication is pending, and
    certification's in-place repair rewrites it (#968 §5.11 as amended)."""
    ns, provider, day, _ = env
    x, y = day / _name(11, 1), day / _name(12, 2)
    z = day / _name(14, 4)
    write_rollout(x, conversation_records(2, session_id=SESSION, label="x",
                                          cwd="/tmp/proj-x"))
    write_rollout(y, conversation_records(2, session_id=OTHER_SESSION, label="y",
                                          cwd="/tmp/proj-y"))
    write_rollout(z, conversation_records(2, session_id=TITLE_SESSION, label="z",
                                          cwd="/tmp/proj-z"))
    _sync(ns)
    _conv(ns)
    kx, ky, kz = _key(ns, x), _key(ns, y), _key(ns, z)
    accounts = _accounts(ns)
    good_z = _surfaces(ns, kz, accounts)
    assert good_z["list"][0] not in (None, "stale")
    conn = ns["open_conversations_db"]()
    try:   # stale materializations an earlier binary left behind
        conn.execute("UPDATE codex_conversation_rollups SET project_key='stale', "
                     "project_label='stale' WHERE conversation_key=?", (ky,))
        conn.execute("UPDATE codex_conversation_rollups SET title='stale' "
                     "WHERE conversation_key=?", (kz,))
        conn.commit()
    finally:
        conn.close()
    good_x = _surfaces(ns, kx, accounts)
    revision_x, revision_y = _render_revision(ns, kx), _render_revision(ns, ky)
    revision_z = _render_revision(ns, kz)
    ids = (_message_ids(ns, kx), _message_ids(ns, ky))
    _compress(x)
    _compress(y)
    _compress(z)
    _sync(ns)
    _conv(ns)
    assert _surfaces(ns, kx, accounts) == good_x        # visible immediately
    pending_z = _surfaces(ns, kz, accounts)
    assert pending_z["list"][0] == "stale"              # legacy value stays
    assert pending_z["list"][1:] == good_z["list"][1:]
    assert _publication(ns, kz)[1] == 1
    assert _render_revision(ns, kz) == revision_z
    # #972 §5.8 rule 1 (revisions 5 and 6): the replacement containers await
    # verification, so the accounting walk is not complete, but a pending
    # mark writes container state only: both of y's cursors keep their
    # logical identity, decoded offsets and the accepted plain generation
    # (size, inode, content anchor), so accounting still covers its
    # `session_meta` from the generation the conversation events came from.
    assert _generation(ns, y)[0] == _generation(ns, y)[1]
    # The reconcile repairs y's thread-derived fields from the published
    # thread the last-good view serves, before certification (the #968
    # amendment), without reading withheld metadata.
    assert not _full_walk_complete(ns)
    pending_y = _surfaces(ns, ky, accounts)
    assert pending_y["list"][1] == _label(ns, "/tmp/proj-y")
    assert pending_y["thread"] == "/tmp/proj-y"
    assert _render_revision(ns, ky) > revision_y
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
    assert _surfaces(ns, kz, accounts)["list"] == good_z["list"]
    assert _render_revision(ns, kz) > revision_z        # certification repaired
    assert _publication(ns, kz)[1] == 0


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
        assert "publication_error" in stats.barrier_retry_causes
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


# --- #972 T1: the R4 plain metadata interleave --------------------------------
#
# A metadata-only contributor B is consumed by the conversation pass before
# accounting accepts it, so the rollup was materialized from A's thread and no
# later pass rewrote it. The failure each test detects is a stored project that
# no longer matches the retained thread metadata, on every reader surface.


def _r4_store(tmp_path, monkeypatch, name):
    """An independent second store (own data dir) over the SAME provider tree,
    so physical identities and conversation keys compare directly."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / name)
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    return ns


def _r4_files(day):
    a, b = day / _name(11, 1), day / _name(12, 2)
    write_rollout(a, _with_quota(conversation_records(
        2, session_id=SESSION, label="a", cwd="/tmp/proj-a")))
    write_rollout(b, _meta_only(SESSION, "/tmp/proj-b"))
    return a, b


def _r4_preserved(conn):
    """What a reconcile must never touch: every message and event account
    stamp, the accounting rows and the quota rows."""
    return (
        conn.execute("SELECT source_path, line_offset, account_key "
                     "FROM main.codex_conversation_messages "
                     "ORDER BY source_path, line_offset").fetchall(),
        conn.execute("SELECT source_path, line_offset, account_key "
                     "FROM main.codex_conversation_events "
                     "ORDER BY source_path, line_offset").fetchall(),
        conn.execute("SELECT * FROM cache_db.codex_session_entries "
                     "ORDER BY source_path, line_offset").fetchall(),
        conn.execute("SELECT * FROM cache_db.quota_window_snapshots "
                     "ORDER BY source, source_path, line_offset, "
                     "logical_limit_key").fetchall(),
    )


def _r4_probe(monkeypatch):
    """Wrap the reconcile so each call records (before, after, written)."""
    cc = _cache()
    real = getattr(cc, "_reconcile_codex_rollup_attribution", None)
    calls: list = []

    def probe(conn, stats):
        before = _r4_preserved(conn)
        written = real(conn, stats)
        calls.append((before, _r4_preserved(conn), written))
        return written

    monkeypatch.setattr(cc, "_reconcile_codex_rollup_attribution", probe,
                        raising=False)
    return calls


def _attribution(ns, cwd: str):
    cache = ns["open_cache_db"]()
    try:
        root = cache.execute(
            "SELECT source_root_key FROM codex_session_files LIMIT 1").fetchone()[0]
    finally:
        cache.close()
    return _cache()._codex_conversation_project_attribution(root, cwd, None)


def _r4_observe(ns, a):
    """The three project surfaces plus the facts the change must preserve."""
    key = _key(ns, a)
    conn = ns["open_conversations_db"]()
    try:
        persisted = conn.execute(
            "SELECT project_key, project_label FROM main.codex_conversation_rollups "
            "WHERE conversation_key=?", (key,)).fetchone()
        row = _list_row(conn, key)
        accounts = sorted({r[0] for r in conn.execute(
            "SELECT account_key FROM main.codex_conversation_messages "
            "WHERE account_key IS NOT NULL")})
        messages = conn.execute(
            "SELECT source_path, line_offset, account_key "
            "FROM main.codex_conversation_messages "
            "ORDER BY source_path, line_offset").fetchall()
        events = conn.execute(
            "SELECT source_path, line_offset FROM main.codex_conversation_events "
            "ORDER BY source_path, line_offset").fetchall()
    finally:
        conn.close()
    assert len(accounts) == 1, accounts
    conn = ns["open_conversations_db"]()
    try:
        ns["scope_conversations_db_to_account"](conn, accounts[0])
        scoped = _list_row(conn, key)
    finally:
        conn.close()
    cache = ns["open_cache_db"]()
    try:
        cursors = sorted(r[0] for r in cache.execute(
            "SELECT path FROM codex_session_files"))
        thread_cwd = cache.execute(
            "SELECT cwd FROM codex_conversation_threads WHERE conversation_key=?",
            (key,)).fetchone()[0]
        entries = cache.execute(
            "SELECT * FROM codex_session_entries "
            "ORDER BY source_path, line_offset").fetchall()
        quota = cache.execute(
            "SELECT * FROM quota_window_snapshots ORDER BY source, source_path, "
            "line_offset, logical_limit_key").fetchall()
        population = cache.execute(
            "SELECT COUNT(*) FROM codex_barrier_population").fetchone()[0]
    finally:
        cache.close()
    return {
        "key": key, "cursors": cursors, "thread_cwd": thread_cwd,
        "population": population,
        "persisted": tuple(persisted) if persisted else None,
        "list": None if row is None else (
            row["project_key"], row["project_label"], row["count"]),
        "scoped": None if scoped is None else (
            scoped["project_key"], scoped["project_label"], scoped["count"]),
        "messages": messages, "events": events,
        "entries": [tuple(r) for r in entries], "quota": [tuple(r) for r in quota],
    }


def _r4_assert_projects(observed, expected, attribution):
    """Persisted, unqualified and account-scoped project fields agree."""
    assert observed["persisted"] == attribution == expected["persisted"]
    assert observed["list"] is not None and observed["list"][:2] == attribution
    assert observed["scoped"] is not None and observed["scoped"][:2] == attribution


def _r4_ordered(ns):
    _sync(ns)
    _conv(ns)


def _r4_interleaved(ns, a):
    # The hook-shaped walk never builds the quota natural-key index (#977 C5);
    # an installed store already has one, so build it first, as one would be.
    import _cctally_journal
    _cctally_journal.ensure_quota_index(budgeted=False)
    _sync_stopping_after(ns, 1)
    cache = ns["open_cache_db"]()
    try:
        assert sorted(r[0] for r in cache.execute(
            "SELECT path FROM codex_session_files")) == [str(a)]
    finally:
        cache.close()
    _conv(ns)
    _sync(ns)
    _conv(ns)


def _r4_invariants(observed, a, b):
    assert observed["cursors"] == sorted([str(a), str(b)])
    assert observed["population"] == 0           # plain, outside the barrier
    assert observed["thread_cwd"] == "/tmp/proj-b"
    assert observed["list"] is not None and observed["list"][2] == 4
    assert len(observed["messages"]) == 4
    assert len(observed["quota"]) > 0


def test_r4_ordered_control_projects(env):
    """The fully ordered control: accounting accepts both files before the
    conversation pass, so every surface shows B's project."""
    ns, provider, day, _ = env
    a, b = _r4_files(day)
    _r4_ordered(ns)
    observed = _r4_observe(ns, a)
    _r4_invariants(observed, a, b)
    attribution = _attribution(ns, "/tmp/proj-b")
    assert attribution[1] == _label(ns, "/tmp/proj-b")
    _r4_assert_projects(observed, observed, attribution)


def test_r4_plain_metadata_interleave_rematerializes_project(
        env, tmp_path, monkeypatch):
    """R4: the interleave converges to the ordered control's project on every
    surface, and changes no identity, account stamp, accounting or quota row."""
    ns, provider, day, _ = env
    a, b = _r4_files(day)
    _r4_ordered(ns)
    ordered = _r4_observe(ns, a)
    ns = _r4_store(tmp_path, monkeypatch, "data-interleaved")
    calls = _r4_probe(monkeypatch)
    _r4_interleaved(ns, a)
    interleaved = _r4_observe(ns, a)
    _r4_invariants(interleaved, a, b)
    assert interleaved["key"] == ordered["key"]
    _r4_assert_projects(interleaved, ordered, _attribution(ns, "/tmp/proj-b"))
    assert {m[:2] for m in interleaved["messages"]} == {
        m[:2] for m in ordered["messages"]}
    assert interleaved["messages"] == ordered["messages"]   # account stamps
    assert interleaved["events"] == ordered["events"]
    assert interleaved["entries"] == ordered["entries"]
    assert interleaved["quota"] == ordered["quota"]
    assert all(before == after for before, after, _ in calls)
    assert any(written > 0 for _, _, written in calls), calls


def test_r4_interleave_lifecycle_converges(env, tmp_path, monkeypatch):
    """After the first repair, a further thread change converges again."""
    ns, provider, day, _ = env
    a, b = _r4_files(day)
    calls = _r4_probe(monkeypatch)
    _r4_interleaved(ns, a)
    observed = _r4_observe(ns, a)
    _r4_assert_projects(observed, observed, _attribution(ns, "/tmp/proj-b"))
    first_calls = len(calls)
    _append(b, [dict(_meta_only(SESSION, "/tmp/proj-a")[0],
                     timestamp="2026-07-20T12:30:00.000Z")])
    _sync(ns)
    _conv(ns)
    observed = _r4_observe(ns, a)
    assert observed["thread_cwd"] == "/tmp/proj-a"
    _r4_assert_projects(observed, observed, _attribution(ns, "/tmp/proj-a"))
    assert all(before == after for before, after, _ in calls)
    assert any(written > 0 for _, _, written in calls[:first_calls]), calls
    assert any(written > 0 for _, _, written in calls[first_calls:]), calls


# --- #972 T6: the reconcile's scope and barrier boundaries --------------------


def _rollup_project(ns, key):
    rows = _conv_rows(ns, "SELECT project_key, project_label "
                          "FROM main.codex_conversation_rollups "
                          "WHERE conversation_key=?", (key,))
    return tuple(rows[0]) if rows else None


def _make_stale(ns, key):
    """A persisted attribution that no longer matches the thread (drift)."""
    conn = ns["open_conversations_db"]()
    try:
        conn.execute("UPDATE codex_conversation_rollups SET project_key='stale', "
                     "project_label='stale' WHERE conversation_key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def _plain_conversation(ns, day):
    a = day / _name(11, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _sync(ns)
    _conv(ns)
    return a, _key(ns, a)


def test_reconcile_refuses_a_scoped_connection_and_a_scoped_read_writes_nothing(env):
    """A scoped connection's TEMP rollups mirror the persisted ones; a
    reconcile there would compare against a shadowed, empty thread view and
    could rewrite every persisted rollup to `(unassigned)`."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    _make_stale(ns, key)
    account = _accounts(ns)[0]
    conn = ns["open_conversations_db"]()
    try:
        ns["scope_conversations_db_to_account"](conn, account)
        assert _list_row(conn, key)["project_label"] == "stale"
        with pytest.raises(RuntimeError, match="account-scoped"):
            _cache()._reconcile_codex_rollup_attribution(
                conn, _cache().CodexIngestStats())
    finally:
        conn.close()
    assert _rollup_project(ns, key) == ("stale", "stale")


def test_a_targeted_pass_leaves_drift_and_the_next_full_pass_repairs_it(env):
    """The reconcile runs only on full passes; the positive control proves a
    full pass repairs the same drift."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    expected = _rollup_project(ns, key)
    _make_stale(ns, key)
    stats = _conv(ns, only_paths={str(a)})
    assert stats.rollups_reattributed == 0
    assert _rollup_project(ns, key) == ("stale", "stale")
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == expected == _attribution(ns, "/tmp/proj-a")


def test_the_reconcile_keeps_a_published_attribution_the_barrier_withholds(env):
    """A population conversation's cache-side thread changed, but no new
    certificate published it: the reconcile reads the last-good view, so the
    rollup keeps the published project instead of exposing withheld metadata.
    A plain conversation's drift in the same pass is the positive control."""
    ns, provider, day, _ = env
    a, o = day / _name(11, 1), day / _name(13, 3)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    _compress(a)
    write_rollout(o, conversation_records(1, session_id=OTHER_SESSION, label="o",
                                          cwd="/tmp/proj-o"))
    _sync(ns)
    _conv(ns)
    key, other = _key(ns, day / _name(11, 1)), _key(ns, o)
    published = _rollup_project(ns, key)
    assert published == _attribution(ns, "/tmp/proj-a")
    assert _publication(ns, key) is not None
    cache = ns["open_cache_db"]()
    try:   # the cache-side thread moves on; no seal publishes it
        cache.execute("UPDATE codex_conversation_threads SET cwd='/tmp/proj-z' "
                      "WHERE conversation_key=?", (key,))
        cache.commit()
    finally:
        cache.close()
    assert _attribution(ns, "/tmp/proj-z") != published
    _make_stale(ns, other)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1                 # the plain control
    assert _rollup_project(ns, other) == _attribution(ns, "/tmp/proj-o")
    assert _surfaces(ns, key)["thread"] == "/tmp/proj-a"
    assert _rollup_project(ns, key) == published


def _full_walk_complete(ns) -> bool:
    cache = ns["open_cache_db"]()
    try:
        return cache.execute(
            "SELECT 1 FROM cache_meta WHERE key=?",
            (_cache()._ingest_frontier.CODEX_FULL_WALK_COMPLETE_KEY,),
        ).fetchone() is not None
    finally:
        cache.close()


def test_unrelated_prune_debt_never_starves_a_repair(env):
    """#972 §5.8 rule 1 (revision 5). Admission is per conversation: prune
    debt for an unrelated vanished rollout keeps the accounting walk's
    completion marker absent for as long as the debt stands (the drain may
    never prove the identity absent), and a reconcile gated on that marker
    left every drifted rollup unrepaired indefinitely."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    expected = _rollup_project(ns, key)
    cache = ns["open_cache_db"]()
    try:   # debt no walk clears: only the drain worker may act on it
        cache.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) SELECT ?, source_root_key, '2026-07-21' "
            "FROM codex_session_files LIMIT 1", (str(day / _name(15, 5)),))
        cache.commit()
    finally:
        cache.close()
    _sync(ns)
    assert not _full_walk_complete(ns)
    _make_stale(ns, key)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == expected == _attribution(ns, "/tmp/proj-a")


def test_the_reconcile_waits_until_accounting_covers_every_session_meta(env):
    """#972 §5.8 rule 1. The R4 pre-state: the conversation pass consumed B's
    `session_meta`, but accounting has no cursor for B yet, so the cache-side
    thread still reflects A alone. Repairing then would write A's project
    (and churn the render revision back once B is accounted); the
    conversation waits until accounting covers the line, and repairs to B's
    project on the next full pass after it does."""
    import _cctally_journal
    ns, provider, day, _ = env
    a, b = _r4_files(day)
    _cctally_journal.ensure_quota_index(budgeted=False)
    _sync_stopping_after(ns, 1)
    _conv(ns)
    key = _key(ns, a)
    assert _one(ns, "SELECT COUNT(*) FROM codex_session_files WHERE path=?",
                (str(b),)) == (0,)
    _make_stale(ns, key)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 0
    assert _rollup_project(ns, key) == ("stale", "stale")   # not A's project
    _sync(ns)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == _attribution(ns, "/tmp/proj-b")


# --- #972 §5.8 rule 1 (revision 6): coverage from the same file generation ----


def _generation(ns, path):
    """Both walks' generation evidence for one path: (accounting, conversation)
    each as (size_bytes, inode, anchor version, anchor offset, anchor)."""
    cols = ("size_bytes, inode, content_anchor_version, "
            "content_anchor_offset, content_anchor")
    cache = ns["open_cache_db"]()
    try:
        f = cache.execute(f"SELECT {cols} FROM codex_session_files WHERE path=?",
                          (str(path),)).fetchone()
    finally:
        cache.close()
    c = _conv_rows(ns, f"SELECT {cols} FROM codex_conversation_source_files "
                       "WHERE path=?", (str(path),))
    return (None if f is None else tuple(f)), (c[0] if c else None)


def _vc1_settled(ns, day):
    """A and B settle on B's project; A then gains project-A metadata."""
    a, b = _r4_files(day)
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    settled = _rollup_project(ns, key)
    assert settled == _attribution(ns, "/tmp/proj-b")
    _append(a, [dict(_meta_only(SESSION, "/tmp/proj-a")[0],
                     timestamp="2026-07-20T12:30:00.000Z")])
    return a, b, key, settled


def _vc1_accounting_reads_a_only(ns, a, b):
    import _cctally_journal
    _cctally_journal.ensure_quota_index(budgeted=False)
    before = _generation(ns, b)[0]
    _sync_stopping_after(ns, 1)
    assert _generation(ns, b)[0] == before          # B's old cursor stands
    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT cwd FROM codex_conversation_threads WHERE conversation_key=?",
            (_key(ns, a),)).fetchone()[0] == "/tmp/proj-a"
    finally:
        cache.close()


def _vc1_ordered_control(tmp_path, monkeypatch, a):
    """The same final provider tree, read in order by a fresh store."""
    ns = _r4_store(tmp_path, monkeypatch, "data-ordered")
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    return _rollup_project(ns, key)


@pytest.mark.parametrize("change", ["replaced", "truncated"])
def test_a_stale_generation_cursor_is_not_coverage(
        env, tmp_path, monkeypatch, change):
    """#972 §5.8 rule 1 (revision 6, VC-1). A path and an offset are
    coordinates, not generation evidence. B's accounting cursor is a legacy
    row with no content anchor, and B is then replaced at its pathname by a
    new file (a new inode at B's own size, so only the inode tells them
    apart) or truncated in place and rewritten smaller (the same inode, so
    only the size tells them apart), and accounting stops after A. The
    conversation pass resets B and consumes its new `session_meta` while B's
    accounting cursor still holds the earlier incarnation's larger offset; a
    reconcile that took that offset as coverage rewrote the rollup to A's
    project from a thread row that never saw B's new metadata. It must keep
    the stored project until accounting re-reads B, then converge to what an
    ordered read of the same files yields."""
    ns, provider, day, _ = env
    a, b, key, settled = _vc1_settled(ns, day)
    old = b.read_bytes()
    cache = ns["open_cache_db"]()
    try:   # a cursor written before content anchors existed
        cache.execute("UPDATE codex_session_files SET content_anchor=NULL, "
                      "content_anchor_version=NULL, "
                      "content_anchor_offset=NULL WHERE path=?", (str(b),))
        cache.commit()
    finally:
        cache.close()
    if change == "replaced":
        new = json.dumps(_meta_only(SESSION, "/tmp/proj-c")[0],
                         separators=(",", ":")).encode() + b"\n"
        assert len(new) == len(old)
        tmp = b.with_name("replacement.tmp")
        tmp.write_bytes(new)
        inode = b.stat().st_ino
        os.replace(tmp, b)
        assert b.stat().st_ino != inode
        final_cwd = "/tmp/proj-c"
    else:
        inode = b.stat().st_ino
        write_rollout(b, _meta_only(SESSION, "/tmp/c"))   # in place, smaller
        assert b.stat().st_ino == inode and b.stat().st_size < len(old)
        final_cwd = "/tmp/c"
    _vc1_accounting_reads_a_only(ns, a, b)
    assert _generation(ns, b)[0][0] == len(old)     # offset beyond line 0
    _conv(ns)
    events = _conv_rows(ns, "SELECT line_offset, payload_json "
                            "FROM codex_conversation_events "
                            "WHERE source_path=? AND record_type='session_meta'",
                        (str(b),))
    assert [row[0] for row in events] == [0] and final_cwd in events[0][1]
    assert _rollup_project(ns, key) == settled      # BASE: A's project
    assert _rollup_project(ns, key) != _attribution(ns, "/tmp/proj-a")
    _sync(ns)
    _conv(ns)
    accounting, conversation = _generation(ns, b)
    assert accounting == conversation
    assert _surfaces(ns, key)["thread"] == final_cwd
    converged = _rollup_project(ns, key)
    assert converged == _attribution(ns, final_cwd)
    assert _vc1_ordered_control(tmp_path, monkeypatch, a) == converged


def _quiet_rollout(day, shape):
    """One quiet conversation in ``shape``; returns its logical path."""
    path = day / _name(11, 1)
    write_rollout(path, conversation_records(2, session_id=SESSION, label="q",
                                             cwd="/tmp/proj-q"))
    if shape == "compressed":
        _compress(path)
    return path


@pytest.mark.parametrize("shape", ["plain", "compressed", "archived"])
def test_a_quiet_rollout_both_walks_read_is_admitted(env, shape):
    """#972 §5.8 rule 1 (revision 6): no starvation. Once both walks have read
    a quiet rollout, plain, compressed or archived, both cursors carry the
    same size, inode and content anchor, so the generation condition admits
    the conversation and the reconcile repairs a drifted rollup. A leg on
    which the two walks wrote different evidence for the same quiet file would
    withhold every repair of that conversation forever."""
    ns, provider, day, _ = env
    path = _quiet_rollout(day, shape)
    _sync(ns)
    _conv(ns)
    if shape == "archived":
        archive = provider / "archived_sessions"
        archive.mkdir()
        os.rename(path, archive / path.name)
        _sync(ns)
        _conv(ns)
    key = _key(ns, path)
    expected = _rollup_project(ns, key)
    assert expected == _attribution(ns, "/tmp/proj-q")
    accounting, conversation = _generation(ns, path)
    assert accounting is not None and accounting == conversation
    _make_stale(ns, key)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == expected


@pytest.mark.parametrize("divergence", ["container", "anchor"])
def test_evidence_the_walks_keep_apart_never_withholds_a_repair(
        env, divergence):
    """#972 §5.8 rule 1 (revision 7): no starvation from evidence the two walks
    maintain independently. Each walk adopts a verified container into its
    own cursor on its own schedule, so for days one cursor can describe the
    adopted `.zst` (its inode) while the other still describes the plain file
    it replaced (on the real store, 26 `session_meta` rows of 9 compressed
    conversations, 4 of them drifted, 2026-11-05). And after a partial read
    followed by an in-place shrink the two walks settle at the same size on
    the same inode with different content anchors. Comparing inodes across
    different containers, or comparing anchors at all, withheld every repair
    of those conversations forever; the size and same-container legs alone
    decide."""
    ns, provider, day, _ = env
    path = _quiet_rollout(day, "compressed")
    _sync(ns)
    _conv(ns)
    key = _key(ns, path)
    expected = _rollup_project(ns, key)
    accounting, conversation = _generation(ns, path)
    assert accounting is not None and accounting == conversation
    cache = ns["open_cache_db"]()
    try:   # accounting's cursor diverges; the conversation pass never writes it
        if divergence == "container":
            cache.execute("UPDATE codex_session_files SET container_kind='plain', "
                          "container_path=NULL, inode=inode+7 WHERE path=?",
                          (str(path),))
        else:
            cache.execute("UPDATE codex_session_files SET "
                          "content_anchor=X'00', content_anchor_offset=1 "
                          "WHERE path=?", (str(path),))
        cache.commit()
    finally:
        cache.close()
    assert _generation(ns, path)[0] != _generation(ns, path)[1]
    _make_stale(ns, key)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == expected


@pytest.mark.parametrize(
    "change", ["thread-deleted", "cwd-unusable", "cursor-regressed"])
def test_a_change_after_detection_is_admitted_again_inside_the_batch(
        env, monkeypatch, change):
    """#972 §5.8 rule 8. Accounting writes under its own locks, so between
    detection and a repair batch a drifted conversation's thread row can
    vanish, its cwd can become underivable, or its accounting cursor can fall
    back behind its `session_meta`. A batch that trusted detection wrote
    `(unassigned)` over a correct label, failed the whole pass on the
    underivable cwd, or wrote from unaccounted metadata; the batch must
    re-admit inside its own snapshot and still repair the other drifted key
    beside it."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    b = day / _name(12, 2)
    write_rollout(b, conversation_records(1, session_id=OTHER_SESSION, label="b",
                                          cwd="/tmp/proj-b"))
    _sync(ns)
    _conv(ns)
    other = _key(ns, b)
    expected = _rollup_project(ns, other)
    _make_stale(ns, key)
    _make_stale(ns, other)
    cc = _cache()
    real = cc._reconcile_codex_rollup_batch
    batches: list = []

    def interleave(conn, batch, stats, attributions):
        batches.append(sorted(batch))
        cache = ns["open_cache_db"]()
        try:   # another writer, between detection and this batch
            if change == "thread-deleted":
                cache.execute("DELETE FROM codex_conversation_threads "
                              "WHERE conversation_key=?", (key,))
            elif change == "cwd-unusable":
                cache.execute("UPDATE codex_conversation_threads "
                              "SET cwd = '/tmp/proj' || char(0) || 'nul' "
                              "WHERE conversation_key=?", (key,))
            else:
                cache.execute("UPDATE codex_session_files SET last_byte_offset=0 "
                              "WHERE path=?", (str(a),))
            cache.commit()
        finally:
            cache.close()
        return real(conn, batch, stats, attributions)

    monkeypatch.setattr(cc, "_reconcile_codex_rollup_batch", interleave)
    stats = _conv(ns)
    assert batches == [sorted([key, other])]       # detection admitted both
    assert stats.rollups_reattributed == 1
    assert stats.rollups_reconcile_skipped == int(change == "cwd-unusable")
    assert _rollup_project(ns, other) == expected
    assert _rollup_project(ns, key) == ("stale", "stale")


def test_one_underivable_rollup_is_skipped_and_the_rest_repair(env):
    """#972 §5.8 rule 4. A cwd the filesystem layer refuses (an embedded NUL
    makes `realpath` raise ValueError) skips that one rollup and is counted;
    it must not fail every full pass and block every other repair."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    b = day / _name(12, 2)
    write_rollout(b, conversation_records(1, session_id=OTHER_SESSION, label="b",
                                          cwd="/tmp/proj-b"))
    _sync(ns)
    _conv(ns)
    other = _key(ns, b)
    expected = _rollup_project(ns, other)
    _make_stale(ns, key)
    _make_stale(ns, other)
    cache = ns["open_cache_db"]()
    try:
        cache.execute("UPDATE codex_conversation_threads "
                      "SET cwd = '/tmp/proj' || char(0) || 'nul' "
                      "WHERE conversation_key=?", (key,))
        cache.commit()
    finally:
        cache.close()
    stats = _conv(ns)
    assert stats.files_failed == 0
    assert stats.rollups_reconcile_skipped == 1
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, other) == expected
    assert _rollup_project(ns, key) == ("stale", "stale")


def test_the_reconcile_keeps_null_attribution_for_malformed_metadata(env):
    """A thread whose metadata cannot be read is NULL, never `(unassigned)`:
    a persisted `(unassigned)` would be a wrong answer every reader prefers."""
    ns, provider, day, _ = env
    a, key = _plain_conversation(ns, day)
    cache = ns["open_cache_db"]()
    try:
        cache.execute("UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
                      "WHERE conversation_key = ?", (b"/tmp/\xffproj", key))
        cache.commit()
    finally:
        cache.close()
    stats = _conv(ns)
    assert stats.rollups_reattributed == 1
    assert _rollup_project(ns, key) == (None, None)
    stats = _conv(ns)
    assert stats.rollups_reattributed == 0
    assert _rollup_project(ns, key) == (None, None)


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
    assert "qualification" in stats.barrier_retry_causes
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


# ── #939 D5: the startup deferral facts writeIo's admission hold anchors ────

def test_the_startup_deferral_facts_read_the_real_schema(env):
    """Detects: writeIo's admission hold reading a column or table the real
    barrier schema does not have (every pass then fails closed and the hold
    never releases), or a fact read from the wrong store; and an append
    beyond the anchored end moving the digest of a deferral blocked at
    startup (a hold that never settles)."""
    import _lib_write_io as wio
    ns, _provider, _day, _spawned = env
    cache = ns["open_cache_db"]()
    try:
        cache.executescript(
            "INSERT INTO codex_barrier_targets (file_identity, source_path,"
            " source_root_key, generation_token, scan_target,"
            " complete_line_end, state, reason, accepted_generation_token,"
            " accepted_complete_line_end) VALUES ('id-a', '/r/a.jsonl', 'root',"
            " 'g1', 100, 100, 'suspended', 'verification-unverified', 'g1', 100);"
            "INSERT INTO codex_barrier_membership (file_identity,"
            " generation_token, conversation_key, source_path) VALUES"
            " ('id-a', 'g1', 'k-member', '/r/a.jsonl'),"
            " ('id-a', 'g0', 'k-stale', '/r/a.jsonl');"
            "INSERT INTO codex_barrier_population (conversation_key,"
            " required_revision, entered_at_utc) VALUES"
            " ('k-member', 3, '2026-11-01T00:00:00Z'),"
            " ('k-named', 2, '2026-11-01T00:00:00Z');"
            "INSERT INTO codex_barrier_certificates (conversation_key, revision,"
            " certificate_json, sealed_at_utc) VALUES"
            " ('k-member', 1, '{}', '2026-11-01T00:00:00Z'),"
            " ('k-member', 2, '{}', '2026-11-01T00:00:00Z');"
            "INSERT INTO codex_barrier_repairs (conversation_key, revision,"
            " reason) VALUES ('k-member', 3, 'x');")
        cache.commit()
    finally:
        cache.close()
    conn = ns["open_conversations_db"]()
    try:
        conn.execute(
            "INSERT INTO codex_conversation_publications (conversation_key,"
            " published_revision, updated_at_utc) VALUES"
            " ('k-member', 1, '2026-11-01T00:00:00Z')")
        conn.commit()
        entries = {"/r/a.jsonl": ("id-a", frozenset({"k-named"})),
                   "/r/gone.jsonl": ("id-gone", frozenset({"k-none"}))}
        facts = _cache().read_codex_startup_deferral_facts(conn, entries)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert facts == {
        "/r/a.jsonl": {
            "target": {"generation": "g1", "complete_line_end": 100,
                       "accepted_generation": "g1", "accepted_end": 100,
                       "state": "suspended",
                       "reason": "verification-unverified"},
            "membership": frozenset({"k-member"}),
            "keys": {
                "k-member": {"required": 3, "certificate": 2, "published": 1,
                             "repair": 3},
                "k-named": {"required": 2, "certificate": None,
                            "published": None, "repair": None}}},
        "/r/gone.jsonl": {
            "target": None, "membership": frozenset(),
            "keys": {"k-none": {"required": None, "certificate": None,
                                "published": None, "repair": None}}}}

    # The anchor resolves from these facts; an append beyond the anchored end
    # (scan target, line end, accepted offset, requirement and repair advance)
    # leaves the deferral's digest unchanged.
    def observe(drain, facts):
        drain.observe({
            "counted": True, "census_complete": True, "certifiable": False,
            "providers": {"codex": {"mode": "full",
                                    "residual": (("container_pending", 1),)}},
            "deferrals": {"/r/a.jsonl": ("id-a", True,
                                         frozenset({"k-named"}))},
            "facts": {"/r/a.jsonl": facts["/r/a.jsonl"]}})
        return drain.digest

    drain = wio.StartupDrain()
    drain.observe_accounting(ended_ns=0, census_complete=True,
                             residuals={"claude": (), "codex": ()})
    before = observe(drain, facts)
    assert drain.anchors["/r/a.jsonl"].end == 100
    cache = ns["open_cache_db"]()
    try:
        cache.executescript(
            "UPDATE codex_barrier_targets SET scan_target=180,"
            " complete_line_end=180, accepted_complete_line_end=180;"
            "UPDATE codex_barrier_population SET required_revision=5"
            " WHERE conversation_key='k-member';"
            "UPDATE codex_barrier_population SET required_revision=4"
            " WHERE conversation_key='k-named';"
            "UPDATE codex_barrier_repairs SET revision=5;")
        cache.commit()
    finally:
        cache.close()
    conn = ns["open_conversations_db"]()
    try:
        appended = _cache().read_codex_startup_deferral_facts(
            conn, {"/r/a.jsonl": ("id-a", frozenset({"k-named"}))})
    finally:
        conn.close()
    assert appended["/r/a.jsonl"]["keys"]["k-member"]["required"] == 5
    assert appended["/r/a.jsonl"]["target"]["complete_line_end"] == 180
    assert observe(drain, appended) == before


# ── #1052: a terminal contributor certifies without publishing ──────────────

#: The equal-length message-text mutation the cache adopts (its legacy check
#: compares no messages it does not hold) and the conversations store rejects
#: (its legacy check compares every event and message): #1052 M3.
_TERMINAL_TEXT = (b'"text":"Answer a0"', b'"text":"Answer q0"')
#: Unit shapes: the terminal file alone, or with a quiet plain sibling that
#: sorts before it (the terminal file is met last) or after it (first).
_TERMINAL_HOURS = {"alone": (11, None), "sibling_first": (12, 11),
                   "sibling_last": (11, 12)}


def _anchorless(ns, path) -> None:
    """Both stores' cursors become legacy rows: no content anchor, so only
    derived-content equivalence decides their verification."""
    for opener, table in (("open_cache_db", "codex_session_files"),
                          ("open_conversations_db",
                           "codex_conversation_source_files")):
        conn = ns[opener]()
        try:
            conn.execute(
                f"UPDATE {table} SET content_anchor=NULL, "
                "content_anchor_version=NULL, content_anchor_offset=NULL "
                "WHERE path=?", (str(path),))
            conn.commit()
        finally:
            conn.close()


def _terminal_unit(ns, day, shape):
    """#1052 M3 on a real two-store fixture: the cache accepted the
    compressed container of ``a`` (target READY, sealed), the conversations
    store ended it ``unverified/legacy-inequivalent``, and the certificate
    naming ``a`` is unpublished. With a sibling, ``b`` feeds the same
    conversation and carries certified bytes the conversations store has not
    consumed. Returns ``(a, zpath, good, b, key)``."""
    terminal_hour, sibling_hour = _TERMINAL_HOURS[shape]
    a = day / _name(terminal_hour, 1)
    write_rollout(a, conversation_records(2, session_id=SESSION, label="a",
                                          cwd="/tmp/proj-a"))
    b = None
    if sibling_hour is not None:
        b = day / _name(sibling_hour, 2)
        write_rollout(b, token_records(0, session_id=SESSION)[:1]
                      + conversation_records(1, session_id=SESSION,
                                             label="b")[1:])
    _sync(ns)
    _conv(ns)
    key = _key(ns, a)
    if b is not None:
        assert _key(ns, b) == key
    _anchorless(ns, a)
    good = a.read_bytes()
    assert _TERMINAL_TEXT[0] in good
    zpath = a.with_name(a.name + ".zst")
    zpath.write_bytes(zstd_bytes(good.replace(*_TERMINAL_TEXT, 1)))
    a.unlink()
    _sync(ns)
    _conv(ns)                       # both stores now hold the transition pending
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    if b is not None:
        _append(b, conversation_records(2, session_id=SESSION,
                                        label="b")[-4:])
    _sync(ns)
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    _sync(ns)
    cache_cursor = _one(ns, "SELECT container_state, container_kind "
                            "FROM codex_session_files WHERE path=?", (str(a),))
    conv_cursor = _conv_rows(
        ns, "SELECT container_state, container_state_reason "
            "FROM codex_conversation_source_files WHERE path=?", (str(a),))
    target = _one(ns, "SELECT state FROM codex_barrier_targets "
                      "WHERE source_path=?", (str(a),))
    assert (tuple(cache_cursor), conv_cursor, tuple(target)) == (
        ("accepted", "zstd"), [("unverified", "legacy-inequivalent")],
        ("ready",)), "the fixture did not reach #1052 M3"
    cert = _certificate(ns, key)
    assert cert is not None and str(a) in _paths_of(cert)
    published = _publication(ns, key)
    assert published is None or published[0] < cert["revision"]
    return a, zpath, good, b, key


def _paths_of(cert) -> set:
    return {c["path"] for c in cert["contributors"]}


def _file_rows(ns, path):
    """The conversations store's cursor, message and event rows of a file."""
    return (
        _conv_rows(ns, "SELECT * FROM codex_conversation_source_files "
                       "WHERE path=?", (str(path),)),
        _conv_rows(ns, "SELECT * FROM codex_conversation_messages "
                       "WHERE source_path=? ORDER BY id", (str(path),)),
        _conv_rows(ns, "SELECT * FROM codex_conversation_events "
                       "WHERE source_path=? ORDER BY line_offset", (str(path),)))


def _seal_tokens(ns):
    import _lib_codex_publication_barrier as barrier
    conn = ns["open_conversations_db"]()
    try:
        sealed = conn.execute("SELECT value FROM cache_db.cache_meta WHERE key=?",
                              (barrier.SEAL_SEQ_KEY,)).fetchone()
        seen = conn.execute("SELECT value FROM main.cache_meta WHERE key=?",
                            (barrier.PUBLISHED_SEQ_KEY,)).fetchone()
    finally:
        conn.close()
    return (None if sealed is None else sealed[0],
            None if seen is None else seen[0])


def _reader_seams(monkeypatch):
    """Count the conversation reader's reads and the full-stream validations
    (decodes) per logical path. The reader seam is the record iterator the
    conversation pass consumes a file through; the bounded root-recognition
    read of a full walk's prune scope is not a publication read."""
    leaf = sys.modules["_lib_codex_rollout_container"]
    cc = _cache()
    opened, validated = [], []
    real_read = cc._iter_codex_fused_records_with_offsets
    real_validate = leaf.validate_full_stream

    def read(fh, path_str, *args, **kwargs):
        opened.append(str(path_str))
        return real_read(fh, path_str, *args, **kwargs)

    def validate_full_stream(container, *args, **kwargs):
        validated.append(str(container.logical))
        return real_validate(container, *args, **kwargs)

    monkeypatch.setattr(cc, "_iter_codex_fused_records_with_offsets", read)
    monkeypatch.setattr(leaf, "validate_full_stream", validate_full_stream)
    return opened, validated


def _codex_roots():
    return tuple(r.walk_root for r in _cache()._codex_provider_roots())


def _seeded_frontier(ns):
    """A trusted Codex conversation frontier seeded from the last pass."""
    import _lib_ingest_frontier as frontier
    app_dir = ns["APP_DIR"] / "frontier-1052"
    state = frontier.ConversationSyncFrontier(app_dir)
    conn = ns["open_conversations_db"]()
    try:
        assert state.seed_provider("codex", conn, roots=_codex_roots(),
                                   trusted=True), state.last_seed_failure
    finally:
        conn.close()
    return state, app_dir


def _plan(ns, state):
    conn = ns["open_conversations_db"]()
    try:
        return state.plan_provider("codex", conn, roots=_codex_roots())
    finally:
        conn.close()


def _force_conversation_state(ns, path, state) -> None:
    conn = ns["open_conversations_db"]()
    try:
        conn.execute("UPDATE codex_conversation_source_files "
                     "SET container_state=? WHERE path=?", (state, str(path)))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("shape", [
    "alone", "sibling_first", "sibling_last",
    "control_pending", "control_executor", "control_other_state"])
def test_a_terminal_unit_certifies_without_publishing(env, monkeypatch, shape):
    """Detects (#1052): a terminal conversations-store verdict that fails the
    Codex transcript pass retryably on every pass (`barrier_retry` /
    `unit_failed`, so neither provider's frontier ever certifies), that
    publishes or reads the unverified container or a sibling of its unit, or
    that is classified by its reason alone. The controls keep every other
    held contributor retryable."""
    import _lib_ingest_frontier as frontier
    ns, _provider, day, _ = env
    unit_shape = "alone" if shape.startswith("control_") else shape
    a, _zpath, _good, b, key = _terminal_unit(ns, day, unit_shape)
    if shape.startswith("control_"):
        kwargs = {}
        if shape == "control_pending":
            _force_conversation_state(ns, a, "pending")
        elif shape == "control_other_state":
            _force_conversation_state(ns, a, "quarantined")
        else:
            kwargs["executor_paths"] = frozenset({str(a)})
        stats = _conv(ns, **kwargs)
        assert getattr(stats, "barrier_keys_terminal", 0) == 0
        assert str(a) not in (getattr(stats, "barrier_terminal_keys", {}) or {})
        if shape != "control_executor":
            assert stats.barrier_retry is True
            assert not frontier.conversation_sync_certifiable("full", stats)
        return
    accounts = _accounts(ns)
    surfaces, publication = _surfaces(ns, key, accounts), _publication(ns, key)
    rows = _file_rows(ns, a)
    sibling_rows = None if b is None else _file_rows(ns, b)[0]
    opened, validated = _reader_seams(monkeypatch)
    for _ in range(2):
        del opened[:], validated[:]
        stats = _conv(ns)
        assert stats.barrier_retry is False, sorted(stats.barrier_retry_causes)
        assert stats.barrier_keys_terminal == 1
        assert stats.barrier_keys_deferred >= stats.barrier_keys_terminal
        assert stats.barrier_terminal_keys == {str(a): frozenset({key})}
        assert stats.container_reads_held == 0
        assert frontier.conversation_sync_certifiable("full", stats)
        assert _surfaces(ns, key, accounts) == surfaces
        assert _publication(ns, key) == publication
        assert _file_rows(ns, a) == rows
        sealed, seen = _seal_tokens(ns)
        assert sealed is not None and seen == sealed
        assert str(a) not in opened and str(a) not in validated
        if b is not None:
            assert str(b) not in opened, "the terminal unit read its sibling"
            assert _file_rows(ns, b)[0] == sibling_rows
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (False, frozenset())
    finally:
        conn.close()
    state, _app_dir = _seeded_frontier(ns)
    plan = _plan(ns, state)
    if b is not None:
        # The sibling's certified bytes stay unconsumed behind its cursor, so
        # the recent-activity restat names it while that cursor is younger
        # than its window: a bounded, certifiable pass that reads nothing.
        assert (plan.mode, plan.reason, plan.paths) == (
            "targeted", "recent_activity", frozenset({str(b)})), plan
        del opened[:], validated[:]
        stats = _conv(ns, only_paths=set(plan.paths))
        assert stats.barrier_terminal_keys == {str(a): frozenset({key})}
        assert frontier.conversation_sync_certifiable(
            "targeted", stats, expected_paths=len(plan.paths))
        assert not set(opened) & {str(a), str(b)} and str(a) not in validated
        real_floor = frontier._recency_floor_iso
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=frontier.FRONTIER_RECENT_ACTIVITY_SECONDS + 1)
        monkeypatch.setattr(frontier, "_recency_floor_iso",
                            lambda now=None: real_floor(now or later))
        plan = _plan(ns, state)
    assert plan.mode == "caught_up", (plan.mode, plan.reason)


@pytest.mark.parametrize("discovery", ["ticket", "expiry"])
def test_a_terminal_unit_recovers_on_new_evidence(env, monkeypatch, discovery):
    """Detects (#1052): a terminal deferral that never retries after a
    genuine container change, or a stamped seal token that hides the change
    from an already-certified frontier. The cache's seal token is held fixed
    while the frontier discovers the change."""
    import _lib_ingest_frontier as frontier
    ns, _provider, day, _ = env
    a, zpath, good, _b, key = _terminal_unit(ns, day, "alone")
    accounts = _accounts(ns)
    surfaces, publication = _surfaces(ns, key, accounts), _publication(ns, key)
    first = _conv(ns)
    assert frontier.conversation_sync_certifiable("full", first)
    state, app_dir = _seeded_frontier(ns)
    assert _plan(ns, state).mode == "caught_up"
    sealed_before, _seen = _seal_tokens(ns)
    # A new fingerprint at the same inode, so no directory moves: the original
    # text, compressed again.
    zpath.write_bytes(zstd_bytes(good))
    if discovery == "ticket":
        assert frontier.record_activity(app_dir, "codex", str(a))
        plan = _plan(ns, state)
        assert plan.mode == "targeted" and str(a) in plan.paths, plan
        stats = _conv(ns, only_paths=set(plan.paths))
        expected = len(plan.paths)
    else:
        # No ticket, and the cursor is older than the recent-activity restat
        # window, so only the certificate's age bound can find the change.
        conn = ns["open_conversations_db"]()
        try:
            conn.execute("UPDATE codex_conversation_source_files SET "
                         "last_ingested_at='2026-07-20T00:00:00+00:00' "
                         "WHERE path=?", (str(a),))
            conn.commit()
        finally:
            conn.close()
        assert _plan(ns, state).mode == "caught_up"
        real_now = frontier._now
        monkeypatch.setattr(
            frontier, "_now",
            lambda: real_now() + frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS + 1)
        plan = _plan(ns, state)
        assert (plan.mode, plan.reason) == ("full", "certificate_expired")
        stats = _conv(ns)
        expected = 0
    assert _seal_tokens(ns)[0] == sealed_before
    assert stats.container_pending >= 1
    assert not frontier.conversation_sync_certifiable(
        plan.mode, stats, expected_paths=expected)
    assert _conv_rows(ns, "SELECT container_state FROM "
                          "codex_conversation_source_files WHERE path=?",
                      (str(a),)) == [("pending",)]
    assert _surfaces(ns, key, accounts) == surfaces
    # Verification adopts the container in the conversations store; the
    # cache's anchor was taken over the mutated text, so its own verdict is
    # `reset/prefix-mismatch`: it re-ingests and re-seals.
    _sync(ns)
    _cache().run_codex_container_verification_drain(wait_for_requests=False)
    assert _conv_rows(ns, "SELECT container_state FROM "
                          "codex_conversation_source_files WHERE path=?",
                      (str(a),)) == [("accepted",)]
    _sync(ns)
    _cache().run_codex_barrier_seal_drain(wait_for_requests=False)
    _sync(ns)
    assert _seal_tokens(ns)[0] != sealed_before
    assert _surfaces(ns, key, accounts) == surfaces
    assert _publication(ns, key) == publication
    published = _conv(ns)
    assert published.barrier_keys_terminal == 0
    assert published.barrier_retry is False, sorted(published.barrier_retry_causes)
    after = _publication(ns, key)
    assert after is not None and after[0] == _certificate(ns, key)["revision"]
    assert publication is None or after[0] > publication[0]


@pytest.mark.parametrize("blocker", [
    "g10_closure", "g10_full", "pending_sibling", "pending_closure_sibling",
    "protected_sibling", "other_state_sibling", "truncated_closure_sibling",
    "replaced_sibling", "loop_meets_terminal"])
def test_a_terminal_unit_with_a_blocked_file_keeps_refusing(
        env, monkeypatch, blocker):
    """Detects (#1052 R1-P1-1, review F2-F4): terminal dominance that hides a
    certification refusal another file of the unit raises (a G10
    requalification hold, a pending verification, a protected path, a state
    that is not ``unverified``, a targeted pass's reset of a sibling that
    arrives only through the unit's closure, a certificate naming another
    generation of a sibling), a held read or a pending skip that the pass
    double counts or does not report, or a loop that meets a terminal
    contributor and no longer fails its unit retryably."""
    import _lib_ingest_frontier as frontier
    ns, _provider, day, _ = env
    # The loop fails a unit at its first refusing file, so a sibling whose
    # own refusal the pass reports must sort before the terminal contributor.
    shape = ("sibling_first" if blocker in (
        "pending_closure_sibling", "truncated_closure_sibling")
        else "sibling_last")
    a, _zpath, _good, b, _key = _terminal_unit(ns, day, shape)
    cc = _cache()
    kwargs = {}
    if blocker in ("g10_closure", "g10_full"):
        # The terminal contributor is G10-held: met in the unit's closure
        # alone (a targeted census of the quiet sibling) or in both phases.
        root_key = _one(ns, "SELECT source_root_key FROM codex_session_files "
                            "WHERE path=?", (str(a),))[0]
        real_held = cc._codex_requalification_held_names

        def held(conn, conv_conn, roots, *, cache_schema=""):
            names = dict(real_held(conn, conv_conn, roots,
                                   cache_schema=cache_schema))
            names[root_key] = frozenset(names.get(root_key, ())) | {a.name}
            return names

        monkeypatch.setattr(cc, "_codex_requalification_held_names", held)
        if blocker == "g10_closure":
            kwargs["only_paths"] = {str(b)}
    elif blocker in ("pending_sibling", "pending_closure_sibling"):
        _force_conversation_state(ns, b, "pending")
        if blocker == "pending_closure_sibling":
            kwargs["only_paths"] = {str(a)}
    elif blocker == "other_state_sibling":
        _force_conversation_state(ns, b, "quarantined")
    elif blocker == "truncated_closure_sibling":
        b.write_bytes(b.read_bytes().splitlines(keepends=True)[0])
        kwargs["only_paths"] = {str(a)}
    elif blocker == "replaced_sibling":
        # Same bytes under a new inode, before the cache re-seals: the
        # certificate names the old generation.
        twin = b.with_name(b.name + ".new")
        twin.write_bytes(b.read_bytes())
        twin.replace(b)
    elif blocker == "protected_sibling":
        real_partition = cc._partition_codex_prune_candidates

        def partition(scope, sources, roots, *, refusal):
            safe, refused, safe_roots, refused_roots = real_partition(
                scope, sources, roots, refusal=refusal)
            for path, root in sources:
                if path == str(b):
                    safe = [s for s in safe if s[0] != path]
                    refused = list(refused) + [(path, root)]
            return safe, refused, safe_roots, refused_roots

        monkeypatch.setattr(cc, "_partition_codex_prune_candidates", partition)
    else:
        monkeypatch.setattr(cc, "_classify_codex_unit_file",
                            lambda *args, **kwargs: "quiet", raising=False)
    stats = _conv(ns, **kwargs)
    assert stats.barrier_retry is True
    assert getattr(stats, "barrier_keys_terminal", 0) == 0
    mode = "targeted" if "only_paths" in kwargs else "full"
    assert not frontier.conversation_sync_certifiable(
        mode, stats, expected_paths=stats.files_total)
    if blocker in ("g10_closure", "g10_full"):
        assert stats.container_reads_held == 1
        assert cc.CODEX_PRUNE_DRAIN_COMMAND in stats.drain_requested
    if blocker == "pending_closure_sibling":
        assert stats.barrier_container_skipped == {"verification-pending": 1}
        assert cc.CODEX_REPLAY_DRAIN_COMMAND in stats.drain_requested
    if blocker == "truncated_closure_sibling":
        assert stats.deferred_reason == "truncation"


def test_classification_blocks_a_cursorless_container_without_decoding(
        env, monkeypatch):
    """Detects (#1052 A5): unit classification that decides a never-ingested
    compressed contributor, whose decision validates (decodes) the whole
    stream, instead of leaving it to the loop's own first sight: a full
    decode per unit on every dashboard pass."""
    import types
    ns, _provider, day, _ = env
    cc = _cache()
    logical = day / _name(13, 3)
    write_rollout(logical, conversation_records(1, session_id=SESSION,
                                                label="z"))
    _compress(logical)
    assert not logical.exists()

    def decided(*args, **kwargs):
        raise AssertionError("classification reached the container decision")

    monkeypatch.setattr(cc, "_codex_container_decision", decided)
    discovered = types.SimpleNamespace(source_path=logical,
                                       source_root_key="unused")
    conn = ns["open_conversations_db"]()
    try:
        assert cc._classify_codex_unit_file(
            conn, discovered, executor_paths=frozenset(),
            protected_paths=frozenset(), requal_held={}, unit=None,
            targeted=False, cursor=None) == "blocked"
    finally:
        conn.close()


@pytest.mark.parametrize("unrelated", [0, 20])
def test_due_pass_file_work_is_fixed_by_the_closure(env, monkeypatch, unrelated):
    """Detects (#1052 A5): a later due pass whose file work grows with
    unrelated stored history rather than with the outstanding closure (the
    ticketed paths plus the contributors of every unpublished certificate),
    or that reads a terminal contributor."""
    import _lib_ingest_frontier as frontier
    ns, _provider, day, _ = env
    for index in range(unrelated):
        path = day / _name(15, 100 + index)
        write_rollout(path, conversation_records(
            1, session_id=f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{2000 + index:04d}",
            label=f"u{index}"))
        _compress(path)
    o = day / _name(13, 3)
    write_rollout(o, conversation_records(1, session_id=OTHER_SESSION,
                                          label="o"))
    _compress(o)
    p = day / _name(14, 5)
    write_rollout(p, token_records(0, session_id=OTHER_SESSION)[:1]
                  + conversation_records(1, session_id=OTHER_SESSION,
                                         label="p")[1:])
    a, _zpath, _good, _b, _key = _terminal_unit(ns, day, "alone")
    assert frontier.conversation_sync_certifiable("full", _conv(ns))
    state, _app_dir = _seeded_frontier(ns)
    assert _plan(ns, state).mode == "caught_up"
    # A new seal for one unrelated conversation, from an append (no directory
    # moves), makes the publication backlog due.
    _append(p, conversation_records(2, session_id=OTHER_SESSION,
                                    label="p")[-4:])
    _sync(ns)
    closure = {str(a), str(o), str(p)}
    conn = ns["open_conversations_db"]()
    try:
        assert frontier._codex_publication_backlog(conn) == (
            True, frozenset(closure))
    finally:
        conn.close()
    plan = _plan(ns, state)
    assert plan.mode == "targeted" and set(plan.paths) == closure, plan
    selected: list = []
    real_select = _cache()._select_codex_container

    def select(logical, *args, **kwargs):
        selected.append(str(logical))
        return real_select(logical, *args, **kwargs)

    monkeypatch.setattr(_cache(), "_select_codex_container", select)
    opened, validated = _reader_seams(monkeypatch)
    stats = _conv(ns, only_paths=set(plan.paths))
    assert set(selected) == closure
    assert str(a) not in opened and str(a) not in validated
    assert frontier.conversation_sync_certifiable(
        "targeted", stats, expected_paths=len(plan.paths)), (
        stats.barrier_retry, sorted(stats.barrier_retry_causes))
