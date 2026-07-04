"""Rebuild-path tests for the #268 dashboard perf refactor.

M1: the dashboard rebuild (`_tui_build_snapshot`) must ingest JSONL
exactly ONCE at the top and then read every builder with
``skip_sync=True`` (pure SQLite reads). Before this change each of the
~8 wide builders ran its own ``sync_cache`` → ~8-10 redundant whole-tree
globs per rebuild (spec §4).

The spy counts total ``sync_cache`` invocations across a whole rebuild.
Builder-internal calls resolve to ``_cctally_cache.sync_cache`` (the
bare name inside ``get_entries`` / ``get_claude_session_entries``); the
new top-of-rebuild call resolves through the ``cctally.sync_cache``
re-export. Patching BOTH names to one shared spy therefore counts every
real ingest — which is exactly the "8-10 → 1" story the change lands.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _asst_line(uuid, msg_id, req_id, text, *, ts, model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "cwd": "/Users/u/proj",
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _seed_jsonl(tmp_path):
    """One Claude JSONL file with a couple of recent assistant entries."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / "s1.jsonl"
    p.write_text(
        _asst_line("u1", "m1", "r1", "hi", ts="2026-07-04T09:00:00Z")
        + _asst_line("u2", "m2", "r2", "yo", ts="2026-07-04T10:30:00Z")
    )
    return p


def _install_sync_spy(ns, monkeypatch):
    """Patch both sync_cache re-export sites to one counting spy that
    delegates to the real ingest. Returns the call-count dict."""
    import _cctally_cache

    calls = {"n": 0}
    real = _cctally_cache.sync_cache

    def spy(conn, **kw):
        calls["n"] += 1
        return real(conn, **kw)

    monkeypatch.setattr(_cctally_cache, "sync_cache", spy)
    monkeypatch.setitem(ns, "sync_cache", spy)
    return calls


def test_sync_cache_called_once_per_rebuild(monkeypatch, tmp_path):
    """A dashboard rebuild (caller skip_sync=False) ingests exactly ONCE."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)
    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)

    assert snap is not None
    assert calls["n"] == 1, (
        f"expected exactly 1 sync_cache per rebuild, got {calls['n']} "
        "(pre-change: each wide builder re-globs → ~8-10 redundant syncs)"
    )


def test_no_sync_never_ingests_but_still_reads(monkeypatch, tmp_path):
    """--no-sync (caller skip_sync=True): the top-of-rebuild ingest is
    gated OFF (zero sync_cache), yet builders still read the already-cached
    rows (spec §4)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)

    # Pre-populate the cache with a real ingest BEFORE the spy, so a pure
    # --no-sync read has existing rows to serve.
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()

    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=True)

    assert snap is not None
    assert calls["n"] == 0, (
        f"--no-sync must not ingest, but sync_cache ran {calls['n']} times"
    )
    # The snapshot still populated from the pre-existing cache rows — the
    # seeded entries' tokens surface without any fresh ingest.
    assert snap.daily_total_tokens > 0, (
        "expected --no-sync rebuild to read existing cached entries"
    )


# ===========================================================================
# M2 Task 2.1 — the cached-bucket recompute helper (spec §5.1)
#
# `cached_buckets` is the pure per-bucket assembly loop: recompute the
# current + dirty buckets whole; serve clean past buckets from the
# BucketCache; recompute (cold-miss) any label absent from the cache.
# `build_cached_group_a` is the stateful wrapper that tracks each
# builder's own last-seen (MAX(session_entries.id), extra_signature),
# derives the dirty predicate from the new-entry watermark, invalidates
# on an extra-signature change / id regression, and calls `cached_buckets`.
# ===========================================================================


_MIN_CACHE_SCHEMA = """
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL
);
"""


def _min_cache_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "mincache.db")
    conn.executescript(_MIN_CACHE_SCHEMA)
    conn.commit()
    return conn


def _ins_entry(conn, ts):
    conn.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc, model) "
        "VALUES ('/p/a.jsonl', 0, ?, 'claude-opus-4-8')",
        (ts,),
    )
    conn.commit()


def _spy_agg(calls):
    def agg(label, entries):
        calls.append(label)
        return {"bucket": label, "n": len(entries)}
    return agg


def test_cached_buckets_cold_recomputes_all():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    calls: list = []
    got = sc.cached_buckets(
        "daily",
        cache=cache,
        all_bucket_labels=["a", "b", "c"],
        current_label="c",
        dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [label],
        aggregate_one=_spy_agg(calls),
    )
    # Every label is a cold miss → recompute+cache; assembled in order.
    assert [b["bucket"] for b in got] == ["a", "b", "c"]
    assert sorted(calls) == ["a", "b", "c"]
    # All three cached now.
    assert cache.get("daily", "a") is not None
    assert cache.get("daily", "c") is not None


def test_cached_buckets_warm_only_current_and_dirty():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    # Cold populate.
    sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "b", "c"],
        current_label="c", dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [], aggregate_one=lambda l, e: {"bucket": l, "gen": 0},
    )
    # Warm: only current ("c") + dirty ("b") recomputed; "a" served from cache.
    calls: list = []

    def agg(label, entries):
        calls.append(label)
        return {"bucket": label, "gen": 1}

    got = sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "b", "c"],
        current_label="c", dirty_predicate=lambda l: l == "b",
        fetch_bucket_entries=lambda label: [], aggregate_one=agg,
    )
    assert sorted(calls) == ["b", "c"], "only current + dirty recomputed"
    by = {b["bucket"]: b for b in got}
    assert by["a"]["gen"] == 0, "clean past bucket served from cache"
    assert by["b"]["gen"] == 1 and by["c"]["gen"] == 1


def test_cached_buckets_empty_bucket_omitted_and_uncached():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    got = sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "gap", "c"],
        current_label="c", dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [],
        aggregate_one=lambda l, e: None if l == "gap" else {"bucket": l},
    )
    assert [b["bucket"] for b in got] == ["a", "c"], "gap (None) omitted"
    assert cache.get("daily", "gap") is None, "empty bucket not cached"


def test_build_cached_group_a_cold_then_warm(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-02T10:00:00Z")
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-02", "2026-07-03", "2026-07-04"]

        cold_calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(cold_calls),
            extra_signature=("local",),
        )
        assert sorted(cold_calls) == labels, "cold recomputes every label"

        # Warm with NO new entries: only the current label recomputes.
        warm_calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(warm_calls),
            extra_signature=("local",),
        )
        assert warm_calls == ["2026-07-04"], (
            f"warm should recompute only the current bucket, got {warm_calls}"
        )
    finally:
        conn.close()


def test_build_cached_group_a_late_ingest_marks_past_dirty(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]
        # Cold.
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=lambda l, e: {"bucket": l},
            extra_signature=("local",),
        )
        # Late ingest: a NEW row (new id) carrying an OLD event time (07-01).
        _ins_entry(conn, "2026-07-01T18:00:00Z")
        calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(calls),
            extra_signature=("local",),
        )
        # The past 07-01 bucket must recompute (watermark reached back).
        assert "2026-07-01" in calls, (
            f"late ingest must recompute the affected PAST bucket, got {calls}"
        )
        assert "2026-07-04" in calls  # current always recomputed
    finally:
        conn.close()


def test_build_cached_group_a_full_invalidate_on_signature_change(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-02", "2026-07-03", "2026-07-04"]
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=lambda l, e: {"bucket": l},
            extra_signature=("utc",),
        )
        # extra_signature changed (e.g. display tz flip) → full invalidate.
        calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(calls),
            extra_signature=("local",),
        )
        assert sorted(calls) == labels, (
            f"extra_signature change must recompute all, got {calls}"
        )
    finally:
        conn.close()
