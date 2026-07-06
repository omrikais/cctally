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
import inspect
import json
import sqlite3
import sys
from zoneinfo import ZoneInfo

import pytest

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
UTC_TZ = ZoneInfo("Etc/UTC")


def _sync_kwargs(fn):
    """Kwargs that activate the Group A cache the way the sync thread does.

    The sync-thread rebuild (`_tui_build_snapshot`) opts a builder into the
    shared Group A cache via an explicit `use_group_a_cache=True` — NOT via
    `skip_sync` (which the off-sync-thread share-period-override path also
    sets). Resolving the flag by signature introspection keeps this file's
    cache-active call sites correct across the API change: it returns the
    explicit opt-in once the builders grow the parameter, and an empty dict
    (falling back to the historical `skip_sync`-only gate) before then, so
    the interleave regression test below reproduces RED on the pre-fix code
    and passes GREEN once the dedicated signal lands.
    """
    if "use_group_a_cache" in inspect.signature(fn).parameters:
        return {"use_group_a_cache": True}
    return {}


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


def _seed_reset_event_week(ns, stats_conn):
    """Seed weekly_usage_snapshots for a handful of weeks, then attach a
    reset event to one of them so ``_week_ref_has_reset_event`` returns True.

    The reset-event branch of ``build_trend_view`` is the one that calls
    ``_compute_cost_for_weekref`` → ``_sum_cost_for_range`` → ``sync_cache``.
    The golden fixtures (and ``test_sync_cache_called_once_per_rebuild``)
    have NO reset-event weeks, so that redundant-sync path never fired in a
    test — this helper closes that fixture gap.

    Keying the event off the *canonical* ``week_end_at`` (discovered via a
    ``get_recent_weeks`` probe) rather than the raw inserted string dodges the
    ``_canonicalize_optional_iso`` normalization that would otherwise make the
    hand-written ``new_week_end_at`` fail to match the ref boundary.
    """
    # A few clean, monotonically-increasing weeks so the historical backfill
    # (invoked by open_db) never synthesizes an *extra* reset of its own.
    weeks = [
        ("2026-06-08", "2026-06-15", 20.0),
        ("2026-06-15", "2026-06-22", 40.0),
        ("2026-06-22", "2026-06-29", 60.0),
        ("2026-06-29", "2026-07-06", 80.0),
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()

    # Probe the canonical boundaries BEFORE the event exists, then pin the
    # reset onto the most-recent week so it lands in BOTH the n=8 trend panel
    # window and the n=12 weekly-history window.
    refs = ns["get_recent_weeks"](stats_conn, 12)
    target = next(
        r for r in refs if r.week_start_at and r.week_end_at
    )
    # A standard post-reset event: new_week_end_at == the week's canonical end
    # rewrites that ref's week_start_at to `effective`, so
    # `effective IN (week_start_at, week_end_at)` holds → has_reset_event True.
    # old_week_end_at is a distinct sentinel (NOT equal to effective, so this
    # is not classified as an in-place credit and no ref is split/dropped).
    effective = target.week_start_at[:11] + "06:00:00+00:00"
    stats_conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) "
        "VALUES (?,?,?,?)",
        (effective, "2020-01-01T00:00:00+00:00", target.week_end_at, effective),
    )
    stats_conn.commit()
    return target, effective


def test_sync_cache_called_once_per_rebuild_with_reset_event_week(
    monkeypatch, tmp_path
):
    """A rebuild whose trend window contains a RESET-EVENT week must STILL
    ingest exactly ONCE (#268 scale-verification finding).

    ``build_trend_view`` live-recomputes cost for reset-affected weeks via
    ``_compute_cost_for_weekref`` → ``_sum_cost_for_range``. Before the
    ``skip_sync`` thread-through, that helper ran a full ``sync_cache``
    (10K-file glob at scale) once per reset-event week — and it fires from
    BOTH the n=8 trend-panel call and the n=12 weekly-history call, so a
    single reset week costs 2 redundant syncs on top of the intended
    top-of-rebuild ingest. The plain golden fixtures have no reset weeks, so
    ``test_sync_cache_called_once_per_rebuild`` passed (==1) despite the bug;
    this fixture exercises the path and pins the count at 1.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        target, effective = _seed_reset_event_week(ns, stats)
        # Guard: the reset-event branch is actually reachable for this week —
        # otherwise the test would be vacuous (it would pass at ==1 without
        # ever touching _compute_cost_for_weekref).
        adjusted = ns["get_recent_weeks"](stats, 12)
        assert any(
            ns["_week_ref_has_reset_event"](stats, r) for r in adjusted
        ), "fixture must produce at least one reset-event week"
    finally:
        stats.close()

    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)

    assert snap is not None
    # The trend sub-build must not have errored out (else the reset path never
    # ran and the count would be a false 1). ``last_sync_error`` is the
    # "; "-joined error string (or None).
    assert "trend" not in (snap.last_sync_error or ""), (
        f"trend/weekly-history sub-build errored: {snap.last_sync_error}"
    )
    assert calls["n"] == 1, (
        f"expected exactly 1 sync_cache per rebuild even with a reset-event "
        f"week, got {calls['n']} (pre-fix: build_trend_view's "
        "_compute_cost_for_weekref re-globs per reset week × 2 call sites)"
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
    model               TEXT    NOT NULL,
    -- #270: the per-row mutation stamps + the cache_meta counter the seq
    -- watermark / signature leg read. `_ins_entry` stamps mutation_seq == id
    -- and advances the counter, so the seq path == the id path (pure inserts).
    mutation_seq        INTEGER NOT NULL DEFAULT 0,
    mutation_min_ts     TEXT
);
CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);
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
    # #270: stamp mutation_seq == id + mutation_min_ts == ts, and advance the
    # cache_meta counter `_entry_mutation_seq` reads, mirroring production so the
    # seq watermark behaves identically to the id watermark for these fixtures.
    conn.execute(
        "UPDATE session_entries SET mutation_seq = id, "
        "mutation_min_ts = timestamp_utc WHERE id = last_insert_rowid()"
    )
    conn.execute(
        "INSERT INTO cache_meta(key, value) "
        "VALUES ('session_entries_mutation_seq', "
        "        CAST((SELECT MAX(mutation_seq) FROM session_entries) AS TEXT)) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
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


# ===========================================================================
# M2 Task 2.2 — wire `_dashboard_build_daily_panel` through the cache.
#
# Parity gate: the cached rebuild's `DailyPanelRow` list (incl.
# `intensity_bucket`, `models` first-seen order, `is_today`, tokens) must
# equal the from-scratch (`_GROUP_A_CACHE_ENABLED=False`) build on the same
# DB — cold, warm (new current-day entry), and late-ingest (new row with an
# OLD event time in a past day).
# ===========================================================================


def _seed_multiday_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several days + two models (first-seen order
    matters). `extra` appends more (uuid, msg, req, text, ts, model) lines."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("d1", "m1", "r1", "a", "2026-06-20T08:00:00Z", "claude-sonnet-4-5"),
        ("d2", "m2", "r2", "b", "2026-06-28T09:00:00Z", "claude-opus-4-8"),
        ("d3", "m3", "r3", "c", "2026-06-28T09:30:00Z", "claude-sonnet-4-5"),
        ("d4", "m4", "r4", "d", "2026-07-01T10:00:00Z", "claude-opus-4-8"),
        ("d5", "m5", "r5", "e", "2026-07-03T11:00:00Z", "claude-sonnet-4-5"),
        ("d6", "m6", "r6", "f", "2026-07-03T11:30:00Z", "claude-opus-4-8"),
        ("d7", "m7", "r7", "g", "2026-07-04T09:00:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _prime_cache(ns):
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()


def _build_daily(ns, stats_conn, *, enabled, display_tz=None):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_daily_panel"](
            stats_conn, NOW_UTC, n=30, skip_sync=True,
            use_group_a_cache=True, display_tz=display_tz
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_daily_panel_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_daily(ns, stats, enabled=True)
        # Non-vacuity: the cached path actually populated the bucket cache.
        assert sc.group_a_cache().get("daily", "2026-07-03") is not None, (
            "cached daily build should have populated the Group A cache"
        )
        wide = _build_daily(ns, stats, enabled=False)
        assert cached == wide, "cached daily rows must equal from-scratch"
        # Sanity: real data present (not the vacuous all-gap case).
        assert any(r.cost_usd > 0 for r in cached)
        assert any(r.intensity_bucket > 0 for r in cached)
    finally:
        stats.close()


def test_daily_panel_warm_new_current_entry(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        # Cold build (populates cache).
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                           use_group_a_cache=True)
        # New entry in the CURRENT day (07-04); re-ingest.
        _seed_multiday_jsonl(tmp_path, extra=[
            ("w1", "wm", "wr", "z", "2026-07-04T10:30:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        # Warm build reuses the module cache (no reset).
        warm = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        # From-scratch on the same post-insert DB.
        wide = _build_daily(ns, stats, enabled=False)
        assert warm == wide, "warm rebuild must equal from-scratch after a current-day entry"
    finally:
        stats.close()


def test_daily_panel_late_ingest_recomputes_past_day(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        cold_0701 = next(r for r in cold if r.date == "2026-07-01")
        # Late ingest: a NEW row (new id) with an OLD event time in a PAST day
        # that is within the current week (07-01). The past bucket must update.
        _seed_multiday_jsonl(tmp_path, extra=[
            ("l1", "lm", "lr", "late", "2026-07-01T15:00:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        warm_0701 = next(r for r in warm if r.date == "2026-07-01")
        assert warm_0701.cost_usd > cold_0701.cost_usd, (
            "the late-ingested past day must be recomputed (cost grew)"
        )
        wide = _build_daily(ns, stats, enabled=False)
        assert warm == wide, "late-ingest warm rebuild must equal from-scratch"
    finally:
        stats.close()


# NOTE (#271 M1 review, Minor 3): an end-to-end real-builder test of the
# mid-CURRENT-bucket late-ingest fallback (a new row with `id > last_seen`
# timestamped BEFORE the current tail) was evaluated and deliberately NOT added
# here. The shared `_asst_line` fixture emits identical token counts for every
# entry, so all per-entry costs are equal and their sum commutes exactly, and
# `DailyPanelRow.models` is sorted by cost (not first-seen order) — so the
# fold-order divergence the ordering guard prevents is not observable through
# `DailyPanelRow`, and disabling the guard leaves such a test GREEN (verified).
# Making it non-vacuous would require extending the shared fixture to vary token
# counts. The guard itself is covered non-vacuously at the unit level by
# `test_accumulate_mid_bucket_late_ingest_falls_back` in tests/test_snapshot_cache.py
# (its `acc.tail` assertion goes RED under the misordered append path).


# ===========================================================================
# M2 Task 2.3 — wire `_dashboard_build_monthly_periods` through the cache.
#
# Parity gate incl. delta_cost_pct + is_current: the cached rebuild's
# MonthlyPeriodRow list must equal the from-scratch build. The
# current-month-only-new-data case must still see the prior (cached) month
# so its delta_cost_pct is non-None (Codex F3).
# ===========================================================================


def _seed_multimonth_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several months + two models."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("m01", "mm1", "mr1", "a", "2025-12-15T08:00:00Z", "claude-sonnet-4-5"),
        ("m02", "mm2", "mr2", "b", "2026-02-10T09:00:00Z", "claude-opus-4-8"),
        ("m03", "mm3", "mr3", "c", "2026-04-05T10:00:00Z", "claude-opus-4-8"),
        ("m04", "mm4", "mr4", "d", "2026-04-06T10:30:00Z", "claude-sonnet-4-5"),
        ("m05", "mm5", "mr5", "e", "2026-06-20T11:00:00Z", "claude-sonnet-4-5"),
        ("m06", "mm6", "mr6", "f", "2026-07-02T11:30:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _build_monthly(ns, stats_conn, *, enabled, display_tz=None):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_monthly_periods"](
            stats_conn, NOW_UTC, n=12, skip_sync=True,
            use_group_a_cache=True, display_tz=display_tz
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_monthly_periods_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_monthly(ns, stats, enabled=True)
        assert sc.group_a_cache().get("monthly", "2026-04") is not None, (
            "cached monthly build should have populated the Group A cache"
        )
        wide = _build_monthly(ns, stats, enabled=False)
        assert cached == wide, "cached monthly rows must equal from-scratch"
        # Non-vacuity: real deltas exist across the multi-month set.
        assert any(r.delta_cost_pct is not None for r in cached)
        assert any(r.is_current for r in cached)
    finally:
        stats.close()


def test_monthly_current_delta_sees_prior_cached_month(monkeypatch, tmp_path):
    """A new entry ONLY in the current month must still compute
    delta_cost_pct against the prior (cached) month (Codex F3)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                               use_group_a_cache=True)
        # New entry in the CURRENT month (2026-07); re-ingest.
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("wm", "wmm", "wmr", "z", "2026-07-04T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        wide = _build_monthly(ns, stats, enabled=False)
        assert warm == wide, "warm monthly rebuild must equal from-scratch"
        cur = next(r for r in warm if r.is_current)
        assert cur.delta_cost_pct is not None, (
            "current month's delta must see the prior cached month"
        )
    finally:
        stats.close()


def test_monthly_late_ingest_recomputes_past_month(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        cold_apr = next(r for r in cold if r.label == "2026-04")
        # Late ingest in a PAST month (2026-04); re-ingest.
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("lm", "lmm", "lmr", "late", "2026-04-20T12:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        warm_apr = next(r for r in warm if r.label == "2026-04")
        assert warm_apr.cost_usd > cold_apr.cost_usd, (
            "the late-ingested past month must be recomputed (cost grew)"
        )
        wide = _build_monthly(ns, stats, enabled=False)
        assert warm == wide, "late-ingest monthly rebuild must equal from-scratch"
    finally:
        stats.close()


# ===========================================================================
# M2 Task 2.4 — wire `_dashboard_build_weekly_periods` through the cache.
#
# The widest special-case surface: build_weekly_view overlay + Bug-K
# synthesized pre-credit rows + delta + is_current, and a multi-table
# dependency (weekly_usage_snapshots / weekly_cost_snapshots / reset
# events feed the SubWeek boundaries). Cached raw per-week BucketUsage is
# full-invalidated whenever a weekly-relevant leg moves (scoped M2.4
# fallback); the presentation reruns fresh every tick. Parity gate:
# cached == from-scratch for plain / warm / late-ingest / stats-change.
# The Bug-K credit-week byte-identity is covered by the reset-week golden
# (bin/cctally-dashboard-test).
# ===========================================================================


def _seed_multiweek_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several subscription weeks + two models."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("w01", "wk1", "wr1", "a", "2026-06-06T08:00:00Z", "claude-sonnet-4-5"),
        ("w02", "wk2", "wr2", "b", "2026-06-15T09:00:00Z", "claude-opus-4-8"),
        ("w03", "wk3", "wr3", "c", "2026-06-16T09:30:00Z", "claude-sonnet-4-5"),
        ("w04", "wk4", "wr4", "d", "2026-06-24T10:00:00Z", "claude-opus-4-8"),
        ("w05", "wk5", "wr5", "e", "2026-07-01T11:00:00Z", "claude-sonnet-4-5"),
        ("w06", "wk6", "wr6", "f", "2026-07-02T11:30:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _build_weekly(ns, stats_conn, *, enabled):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_weekly_periods"](
            stats_conn, NOW_UTC, n=12, skip_sync=True, use_group_a_cache=True
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def _weekly_total(rows):
    return sum(r.cost_usd for r in rows)


def test_weekly_periods_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_weekly(ns, stats, enabled=True)
        # Non-vacuity: the cached path populated the weekly namespace.
        assert any(
            k[0] == "weekly" for k in sc.group_a_cache()._store
        ), "cached weekly build should have populated the Group A cache"
        wide = _build_weekly(ns, stats, enabled=False)
        assert cached == wide, "cached weekly rows must equal from-scratch"
        assert any(r.cost_usd > 0 for r in cached)
    finally:
        stats.close()


def test_weekly_warm_current_entry(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                              use_group_a_cache=True)
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("ww", "wkw", "wrw", "z", "2026-07-03T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, "warm weekly rebuild must equal from-scratch"
    finally:
        stats.close()


def test_weekly_late_ingest_past_week(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        # Late ingest into a PAST week (mid-June).
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("wl", "wkl", "wrl", "late", "2026-06-16T14:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, "late-ingest weekly rebuild must equal from-scratch"
        assert _weekly_total(warm) > _weekly_total(cold), (
            "the late-ingested past week must be recomputed (total grew)"
        )
    finally:
        stats.close()


def test_weekly_stats_change_full_invalidate(monkeypatch, tmp_path):
    """A weekly_usage_snapshots insert with NO new session entry must still
    make the affected weeks recompute (the snapshot legs feed the SubWeek
    boundaries / overlay) — cached == from-scratch (spec §5.1 F2)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                              use_group_a_cache=True)
        # Insert a weekly_usage_snapshot overlay for a covered week — NO new
        # session_entries row. The extra-signature leg must move → full
        # invalidate → recompute.
        stats.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES ('2026-07-02T00:00:00Z', '2026-06-29', '2026-07-06', "
            "'2026-06-29T00:00:00+00:00', '2026-07-06T00:00:00+00:00', 42.0, '{}')"
        )
        stats.commit()
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, (
            "a weekly_usage_snapshots change (no new entry) must recompute "
            "the affected weeks — cached == from-scratch"
        )
    finally:
        stats.close()


# ===========================================================================
# Bundle-2 review — share-period-override must NOT pollute the shared Group A
# cache with a partial past bucket.
#
# `_share_apply_period_override` rebuilds daily/monthly/weekly on an HTTP
# handler thread with a shifted (PAST) `now_override` and `skip_sync=True`.
# When the Group A cache activated on `skip_sync=True` (the pre-fix gate),
# that off-sync-thread caller shared the SAME module-level `_GROUP_A_CACHE`
# as the sync thread: the bucket that is *current relative to now_override*
# was recomputed clamped to that past instant → a PARTIAL aggregate → cached
# under a real PAST-period label. The next sync tick then served that
# truncated bucket as a "clean past bucket" → the live dashboard showed a
# truncated past day/week/month.
#
# These tests INTERLEAVE a share-override build at a PAST `now` with a
# sync-thread rebuild at the real `now` WITHOUT resetting the cache between
# them (modelling the real shared-process interleave) and assert the sync
# rebuild's past bucket is byte-identical to a from-scratch build. The fix
# gates the cache on a dedicated `use_group_a_cache` signal that ONLY the
# sync-thread rebuild sets — so the share path never touches the cache.
# ===========================================================================


def _share_build_daily(ns, stats_conn, now_override, *, display_tz):
    """Mirror `_share_apply_period_override`'s daily invocation exactly:
    `skip_sync=True`, a shifted `now`, and NO `use_group_a_cache` opt-in."""
    return ns["_dashboard_build_daily_panel"](
        stats_conn, now_override, n=30, skip_sync=True, display_tz=display_tz
    )


def _sync_build_daily(ns, stats_conn, *, display_tz):
    """Mirror the sync thread's daily invocation (cache-active)."""
    fn = ns["_dashboard_build_daily_panel"]
    return fn(stats_conn, NOW_UTC, n=30, skip_sync=True,
              display_tz=display_tz, **_sync_kwargs(fn))


def test_daily_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH: a PAST instant that splits the 07-03 day BETWEEN its two
        # entries (11:00 and 11:30). Pinning display_tz to UTC keeps the day
        # bucket + the split deterministic regardless of host tz.
        past = dt.datetime(2026, 7, 3, 11, 15, 0, tzinfo=dt.timezone.utc)
        _share_build_daily(ns, stats, past, display_tz=UTC_TZ)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        # Pre-fix: reads the truncated 07-03 bucket the share build cached.
        sync_rows = _sync_build_daily(ns, stats, display_tz=UTC_TZ)
        sync_0703 = next(r for r in sync_rows if r.date == "2026-07-03")
        # From-scratch reference (fresh cache, cache path disabled).
        wide = _build_daily(ns, stats, enabled=False, display_tz=UTC_TZ)
        wide_0703 = next(r for r in wide if r.date == "2026-07-03")
        # Non-vacuity: 07-03 genuinely has data on both sides of the split, so
        # a truncated bucket is strictly cheaper than the full one.
        assert wide_0703.cost_usd > 0
        assert sync_0703.cost_usd == wide_0703.cost_usd, (
            "share-override build at a PAST now must not pollute the sync "
            "cache with a truncated past-day bucket (07-03 lost its 11:30 "
            f"entry): sync={sync_0703.cost_usd} wide={wide_0703.cost_usd}"
        )
        assert sync_rows == wide, "sync rebuild must equal from-scratch"
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


def _share_build_monthly(ns, stats_conn, now_override, *, display_tz):
    return ns["_dashboard_build_monthly_periods"](
        stats_conn, now_override, n=12, skip_sync=True, display_tz=display_tz
    )


def _sync_build_monthly(ns, stats_conn, *, display_tz):
    fn = ns["_dashboard_build_monthly_periods"]
    return fn(stats_conn, NOW_UTC, n=12, skip_sync=True,
              display_tz=display_tz, **_sync_kwargs(fn))


def test_monthly_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH: a PAST instant that splits 2026-04 BETWEEN its two
        # entries (04-05 10:00 and 04-06 10:30).
        past = dt.datetime(2026, 4, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
        _share_build_monthly(ns, stats, past, display_tz=UTC_TZ)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        sync_rows = _sync_build_monthly(ns, stats, display_tz=UTC_TZ)
        sync_apr = next(r for r in sync_rows if r.label == "2026-04")
        wide = _build_monthly(ns, stats, enabled=False, display_tz=UTC_TZ)
        wide_apr = next(r for r in wide if r.label == "2026-04")
        assert wide_apr.cost_usd > 0
        assert sync_apr.cost_usd == wide_apr.cost_usd, (
            "share-override build must not pollute the sync cache with a "
            f"truncated past-month bucket: sync={sync_apr.cost_usd} "
            f"wide={wide_apr.cost_usd}"
        )
        assert sync_rows == wide, "sync rebuild must equal from-scratch"
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


def _share_build_weekly(ns, stats_conn, now_override):
    return ns["_dashboard_build_weekly_periods"](
        stats_conn, now_override, n=12, skip_sync=True
    )


def _sync_build_weekly(ns, stats_conn):
    fn = ns["_dashboard_build_weekly_periods"]
    return fn(stats_conn, NOW_UTC, n=12, skip_sync=True, **_sync_kwargs(fn))


def test_weekly_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        # Discover the real subscription-week boundaries so the reproduction
        # is robust to the env's default week-start. Find a PAST week (ends
        # before NOW) that carries ≥2 session entries, then pick a now_override
        # strictly between its earliest and latest entry so the share build
        # clamps that week to a partial aggregate.
        conn = ns["open_db"]()
        try:
            weeks = ns["_compute_subscription_weeks"](
                conn, NOW_UTC - dt.timedelta(days=7 * 13), NOW_UTC
            )
        finally:
            conn.close()
        parse = ns["parse_iso_datetime"]
        entry_times = [
            dt.datetime(2026, 6, 6, 8, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 15, 9, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 16, 9, 30, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 24, 10, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 7, 1, 11, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 7, 2, 11, 30, tzinfo=dt.timezone.utc),
        ]
        target_label = None
        past = None
        for w in weeks:
            s = parse(w.start_ts, "week.start_ts")
            e = parse(w.end_ts, "week.end_ts")
            if e >= NOW_UTC:
                continue  # not a fully-past week
            in_week = sorted(t for t in entry_times if s <= t < e)
            if len(in_week) >= 2 and in_week[0] < in_week[-1]:
                target_label = w.start_date.isoformat()
                # midpoint strictly between first and last entry of the week
                past = in_week[0] + (in_week[-1] - in_week[0]) / 2
                break
        assert target_label is not None and past is not None, (
            "fixture must contain a fully-past subscription week with ≥2 "
            "entries spanning a mid-point"
        )

        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH at the PAST midpoint → clamps `target_label` week partial.
        _share_build_weekly(ns, stats, past)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        sync_rows = _sync_build_weekly(ns, stats)
        wide = _build_weekly(ns, stats, enabled=False)
        # Compare the whole list (the polluted week would show a lower cost).
        assert sync_rows == wide, (
            "share-override build at a PAST now must not pollute the sync "
            "cache with a truncated past-week bucket — sync rebuild must "
            "equal from-scratch"
        )
        assert _weekly_total(wide) > 0
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


# ===========================================================================
# M3 (Group B) — sessions cache.
#
# `_tui_build_sessions` serves the sessions pane from a module-level
# `SessionCache` holding ALL sessions in the 365-day window. On a warm tick
# it re-aggregates ONLY the sessions changed since the last tick, then
# sort+truncates the FULL cached set — so a session below the top-N can
# promote once it gets new activity (Codex F5). Gated on `use_session_cache`
# (sync-thread only), NEVER on `skip_sync`, so no non-sync caller can pollute
# the shared cache (the Bundle 2 Group A lesson).
# ===========================================================================


def _sess_line(session_id, uuid, msg_id, req_id, text, *, ts,
               cwd="/Users/u/proj", model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts, "cwd": cwd,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _write_session_file(tmp_path, session_id, text):
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"
    p.write_text(text)
    return p


def _seed_sessions(tmp_path, specs):
    """specs: {session_id: [(ts, text), ...]}. One JSONL file per session,
    named `<session_id>.jsonl`. Each line's (msg_id, req_id) is derived from
    `(session_id, index)` so it is STABLE across rewrites and UNIQUE across
    sessions — a global counter would collide on rewrite and hit the
    `(msg_id, req_id)` dedup index, silently dropping the appended line."""
    for sid, entries in specs.items():
        lines = []
        for i, (ts, text) in enumerate(entries):
            key = f"{sid}-{i}"
            lines.append(_sess_line(
                sid, f"u-{key}", f"m-{key}", f"r-{key}", text, ts=ts,
            ))
        _write_session_file(tmp_path, sid, "".join(lines))


def _build_sessions(ns, *, enabled, now=NOW_UTC, limit=100):
    """Cold cached (enabled=True) vs from-scratch (enabled=False) — resets the
    module SessionCache first so the cached build starts cold."""
    import _lib_snapshot_cache as sc
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    prev = getattr(tui, "_SESSION_CACHE_ENABLED", True)
    tui._SESSION_CACHE_ENABLED = enabled
    try:
        return ns["_tui_build_sessions"](
            now, skip_sync=True, use_session_cache=True, limit=limit,
        )
    finally:
        tui._SESSION_CACHE_ENABLED = prev


def _ids(rows):
    return [r.session_id for r in rows]


def test_sessions_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1"), ("2026-07-03T10:30:00Z", "b2")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)

    cached = _build_sessions(ns, enabled=True)
    # Non-vacuity: the cached path actually populated the module cache.
    assert sc.session_cache().get_all(), (
        "cached sessions build must populate the SessionCache"
    )
    wide = _build_sessions(ns, enabled=False)
    assert cached == wide, "cached sessions rows must equal from-scratch"
    # Real data present (not the vacuous empty case).
    assert cached and any(r.cost_usd > 0 for r in cached)
    # Descending by last_activity (started_at desc for these single/paired sessions).
    assert _ids(cached) == ["sess-a", "sess-b", "sess-c"]


def test_sessions_warm_matches_from_scratch(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    # Cold populate.
    cold = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    cold_b = next(r for r in cold if r.session_id == "sess-b")

    # New activity in an EXISTING session (sess-b) — append a later line.
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1"), ("2026-07-04T11:45:00Z", "b2")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    warm_b = next(r for r in warm if r.session_id == "sess-b")
    assert warm_b.cost_usd > cold_b.cost_usd, "changed session must re-aggregate (cost grew)"

    wide = _build_sessions(ns, enabled=False)
    assert warm == wide, "warm sessions rebuild must equal from-scratch"


def test_sessions_promotion_below_topN(monkeypatch, tmp_path):
    """A session below the top-N gets new activity → it promotes into the
    visible slice and the evicted one drops (Codex F5). Uses limit=3."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    # 4 sessions; sess-d is the OLDEST (rank 4, outside a top-3 view).
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T09:00:00Z", "b")],
        "sess-c": [("2026-07-02T09:00:00Z", "c")],
        "sess-d": [("2026-06-01T09:00:00Z", "d")],
    })
    _prime_cache(ns)
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    cold = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    assert _ids(cold) == ["sess-a", "sess-b", "sess-c"]
    assert "sess-d" not in _ids(cold)

    # sess-d (previously outside the top-3) gets the LATEST activity.
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T09:00:00Z", "b")],
        "sess-c": [("2026-07-02T09:00:00Z", "c")],
        "sess-d": [("2026-06-01T09:00:00Z", "d"), ("2026-07-04T11:59:00Z", "d2")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    assert "sess-d" in _ids(warm), "session with new activity must promote into top-N"
    assert "sess-c" not in _ids(warm), "the evicted (now rank-4) session must drop"
    assert _ids(warm) == ["sess-d", "sess-a", "sess-b"]

    # Parity with from-scratch on the post-append DB.
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = False
    wide = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    tui._SESSION_CACHE_ENABLED = True
    assert warm == wide


def test_sessions_straddling_reaggregates_whole(monkeypatch, tmp_path):
    """A resumed session straddling multiple calendar days aggregates WHOLE
    (no split-row) — cached cost/tokens equal the sum of ALL its entries, and
    a warm append on yet another day still re-aggregates the whole session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        # Same session_id across three different days.
        "sess-straddle": [
            ("2026-07-01T08:00:00Z", "s1"),
            ("2026-07-03T10:00:00Z", "s2"),
        ],
        "sess-other": [("2026-07-04T09:00:00Z", "o")],
    })
    _prime_cache(ns)

    cached = _build_sessions(ns, enabled=True)
    wide = _build_sessions(ns, enabled=False)
    assert cached == wide
    straddle = next(r for r in cached if r.session_id == "sess-straddle")
    one = next(r for r in wide if r.session_id == "sess-other")
    # The straddling session folded BOTH its entries (2 lines) into one row →
    # ~2x the single-entry session's cost (same per-line usage).
    assert straddle.cost_usd > one.cost_usd * 1.5

    # Warm: a THIRD entry on a new day. The whole session re-aggregates.
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    _seed_sessions(tmp_path, {
        "sess-straddle": [
            ("2026-07-01T08:00:00Z", "s1"),
            ("2026-07-03T10:00:00Z", "s2"),
            ("2026-07-04T11:00:00Z", "s3"),
        ],
        "sess-other": [("2026-07-04T09:00:00Z", "o")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    wide2 = _build_sessions(ns, enabled=False)
    assert warm == wide2
    straddle2 = next(r for r in warm if r.session_id == "sess-straddle")
    assert straddle2.cost_usd > straddle.cost_usd, "3rd entry must fold into the whole session"


def test_sessions_from_scratch_call_does_not_populate_cache(monkeypatch, tmp_path):
    """Caller-audit guard: a default (use_session_cache=False) call must NOT
    touch the shared SessionCache. `_tui_build_sessions`' ONLY caller today is
    `_tui_build_snapshot` (real-now sync thread), but this locks in that no
    future non-sync caller with a shifted `now` can pollute the cache — the
    Bundle 2 Group A pollution lesson applied to Group B."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {"sess-a": [("2026-07-04T09:00:00Z", "a")]})
    _prime_cache(ns)
    sc.reset_session_cache_state()
    # Default flag (False): the from-scratch path. A shifted PAST now, too.
    past = dt.datetime(2026, 7, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    rows = ns["_tui_build_sessions"](past, skip_sync=True)
    assert sc.session_cache().get_all() == {}, (
        "a use_session_cache=False call must never write the shared SessionCache"
    )
    # And it still renders from-scratch.
    assert isinstance(rows, list)


# ===========================================================================
# M5 Task 5.1 — idle-path short-circuit + republish (spec §3, §7)
#
# When the composite data-version signature is unchanged and no wall-clock
# day/week/month boundary rolled over, the dashboard rebuild reuses the prior
# published snapshot's heavy period/session rows and re-patches ONLY the
# time-derived fields (generated_at / last_sync_at) + the doctor payload on
# TTL — running NO re-aggregation, so an idle dashboard sits near 0% CPU.
# Dashboard-only (precompute_envelope=True) and sync-thread-only.
# ===========================================================================


def _spy_heavy_builders(monkeypatch):
    """Count every heavy re-aggregation a full rebuild runs: the three Group A
    period builders (each wraps `_aggregate_daily/_monthly/_weekly`) + the
    sessions aggregator. The idle path must invoke NONE of them. All four are
    resolved by `_tui_build_snapshot` through the `cctally` namespace shims, so
    patching that dict intercepts the real calls."""
    cd = sys.modules["cctally"].__dict__
    calls = {"n": 0}
    for _name in ("_dashboard_build_daily_panel",
                  "_dashboard_build_monthly_periods",
                  "_dashboard_build_weekly_periods",
                  "_aggregate_claude_sessions"):
        _real = cd[_name]

        def _spy(*a, _real=_real, **k):
            calls["n"] += 1
            return _real(*a, **k)

        monkeypatch.setitem(cd, _name, _spy)
    return calls


def _spy_doctor_gather_local(monkeypatch):
    calls = {"n": 0}
    real = sys.modules["cctally"].doctor_gather_state

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(sys.modules["cctally"].__dict__, "doctor_gather_state", spy)
    return calls


def test_idle_rebuild_reuses_rows_and_skips_reaggregation(monkeypatch, tmp_path):
    """Two rebuilds with no DB change: the second takes the IDLE path — no
    heavy builder / aggregator runs, the prior snapshot's rows are reused
    verbatim, and only the time-derived fields are re-patched (spec §3)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    # First rebuild (cold/full): ingest + aggregate, store the dispatch state.
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap1.daily_panel and snap1.weekly_periods, "cold build has data"

    # No DB change → the second rebuild must be IDLE.
    calls = _spy_heavy_builders(monkeypatch)
    later = NOW_UTC + dt.timedelta(seconds=5)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 0, (
        f"idle rebuild must skip all re-aggregation, ran {calls['n']} heavy builds"
    )
    # Heavy rows reused verbatim (shared reference — copied, not rebuilt).
    assert snap2.daily_panel is snap1.daily_panel
    assert snap2.weekly_periods is snap1.weekly_periods
    assert snap2.monthly_periods is snap1.monthly_periods
    assert snap2.sessions is snap1.sessions
    # Fresh DataSnapshot object; time-derived fields re-patched.
    assert snap2 is not snap1
    assert snap2.generated_at == later
    assert snap2.last_sync_at is not None
    # Doctor carried (present); within TTL it is not re-gathered.
    assert snap2.doctor_payload is not None


def test_idle_rebuild_recomputes_after_new_usage(monkeypatch, tmp_path):
    """A new entry advances the composite signature → the next rebuild is NOT
    idle: the heavy builders run and the new usage lands (non-vacuity guard for
    the idle short-circuit)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    base_cost = snap1.daily_total_cost_usd

    # New current-day usage → signature advances → full rebuild.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("n1", "nm", "nr", "new", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    calls = _spy_heavy_builders(monkeypatch)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 4, "a signature change must trigger a full rebuild"
    assert snap2.daily_total_cost_usd > base_cost, "new usage must land"
    assert snap2.daily_panel is not snap1.daily_panel, "fresh rows built"


def test_idle_rebuild_refreshes_doctor_on_ttl(monkeypatch, tmp_path):
    """The doctor TTL is an INDEPENDENT invalidation (Codex F6): on a long-idle
    dashboard, once the doctor memo TTL elapses an idle tick still re-gathers
    doctor and republishes, so the doctor chip never freezes."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)
    calls = _spy_doctor_gather_local(monkeypatch)

    # Full build gathers doctor once.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1
    # Idle within TTL → memo hit, no re-gather.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1, "within the TTL the idle tick must not re-fork doctor"
    # Idle PAST the TTL → doctor refreshes on the idle tick.
    past_ttl = NOW_UTC + dt.timedelta(seconds=sc.DOCTOR_MEMO_TTL_S + 5)
    snap3 = ns["_tui_build_snapshot"](
        now_utc=past_ttl, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 2, "an idle tick past the doctor TTL must re-gather doctor"
    assert snap3.doctor_payload is not None


def test_idle_path_off_for_tui_rebuild(monkeypatch, tmp_path):
    """The idle short-circuit is dashboard-only: a TUI-path rebuild
    (precompute_envelope=False) never consults or stores the dispatch memo, so
    it always does a full build (no regression to the terminal TUI)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    _seed_multiday_jsonl(tmp_path)

    ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    # A TUI rebuild must not have stored dispatch state.
    prior_sig, prior_snap = sc.dispatch_state()
    assert prior_sig is None and prior_snap is None
    # And the second TUI rebuild still runs the heavy builders (the three
    # period builders always run on a non-idle rebuild; the sessions aggregator
    # is skipped only because the session cache is warm with no new entries).
    calls = _spy_heavy_builders(monkeypatch)
    ns["_tui_build_snapshot"](now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False)
    assert calls["n"] >= 3, "TUI rebuild must not idle-short-circuit"


def test_config_change_forces_full_rebuild_not_idle(monkeypatch, tmp_path):
    """A config edit (e.g. `POST /api/settings` changing display.tz) advances
    neither MAX(id) nor the reset legs, so the DB signature is stable — but it
    changes the rendered envelope + re-buckets the calendar builders. The idle
    key bundles a render key (resolved tz + config), so a config change forces a
    full (non-idle) rebuild rather than serving the stale snapshot."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    _seed_multiday_jsonl(tmp_path)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    # Edit config.json (display.tz) — no DB change at all.
    ns["CONFIG_PATH"].write_text(json.dumps({"display": {"tz": "Asia/Jerusalem"}}))

    calls = _spy_heavy_builders(monkeypatch)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 3, (
        "a config change must force a full (non-idle) rebuild via the render key"
    )


def test_idle_rebuild_refreshes_update_state(monkeypatch, tmp_path):
    """The update-state / update-suppress reads live on the snapshot's
    `envelope_precompute` (M4). `update-state.json` is written OUT OF BAND by
    `_DashboardUpdateCheckThread`, advancing neither MAX(id) nor the reset legs
    nor the config render key — so the idle path must refresh
    `envelope_precompute` each tick, else a newly-detected release never surfaces
    on a long-idle / --no-sync dashboard until new usage or a config edit lands
    (#268 M5 Finding 1). Models the REAL idle interleave: no module-state reset
    between the two builds."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    # Full build with NO update-state.json → update-state is null.
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap1.envelope_precompute is not None
    assert snap1.envelope_precompute["update_state"] is None
    env1 = ns["snapshot_to_envelope"](snap1, now_utc=NOW_UTC)
    assert env1["update"]["state"] is None

    # A new release is detected out of band: update-state.json now names 9.9.9.
    # NO DB change, NO config change, NO module-state reset.
    ns["UPDATE_STATE_PATH"].write_text(json.dumps({"latest_version": "9.9.9"}))

    # No DB / config change → the second rebuild is IDLE (assert zero
    # re-aggregation), yet it must pick up the freshly-written update-state.
    calls = _spy_heavy_builders(monkeypatch)
    later = NOW_UTC + dt.timedelta(seconds=5)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 0, (
        f"second rebuild must be IDLE, ran {calls['n']} heavy builds"
    )
    # Heavy rows still reused verbatim — proves this is the idle path, not a
    # silent full rebuild that would refresh update-state as a side effect.
    assert snap2.daily_panel is snap1.daily_panel
    assert snap2.weekly_periods is snap1.weekly_periods
    # The fix: envelope_precompute is refreshed on the idle tick, NOT frozen to
    # `prior`'s. update-state now reflects the out-of-band write.
    assert snap2.envelope_precompute["update_state"] == {"latest_version": "9.9.9"}
    env2 = ns["snapshot_to_envelope"](snap2, now_utc=later)
    assert env2["update"]["state"]["latest_version"] == "9.9.9"


def test_snapshot_period_rollover_detects_5h_reset(monkeypatch, tmp_path):
    """A zero-usage 5h reset on a truly-idle dashboard advances no DB signature
    and crosses no day/week/month boundary, so the idle-rollover guard must
    independently force a full rebuild once `now` crosses the prior snapshot's
    5h reset instant (#268 M5 Finding 4). Otherwise the 'current block' surface
    goes stale until real usage lands. Pure-function test on
    `_snapshot_period_rolled_over` — no DB, no flakiness."""
    ns = load_script()
    import _cctally_tui as tui

    # NOW = 2026-07-04 12:00Z. 5h block resets in 2h (14:00Z), still the SAME
    # calendar day and inside the subscription week — so ONLY the 5h leg can
    # trip the guard (day/week legs stay False), keeping the test non-vacuous.
    reset_at = NOW_UTC + dt.timedelta(hours=2)
    cw = ns["TuiCurrentWeek"](
        week_start_at=NOW_UTC - dt.timedelta(days=1),
        week_end_at=NOW_UTC + dt.timedelta(days=6),
        used_pct=12.0, five_hour_pct=30.0, five_hour_resets_at=reset_at,
        spent_usd=1.0, dollars_per_percent=None, latest_snapshot_at=NOW_UTC,
    )
    prior = ns["DataSnapshot"](cw, None, [], [], None, None, NOW_UTC)

    # Just BEFORE the reset instant, same day/week → NOT rolled over.
    assert tui._snapshot_period_rolled_over(
        prior, reset_at - dt.timedelta(minutes=1), UTC_TZ,
    ) is False
    # AT the 5h reset instant (no day/week change) → rolled over.
    assert tui._snapshot_period_rolled_over(prior, reset_at, UTC_TZ) is True
    # And past it.
    assert tui._snapshot_period_rolled_over(
        prior, reset_at + dt.timedelta(minutes=1), UTC_TZ,
    ) is True

    # A prior snapshot with NO 5h reset (field None) must not trip the guard.
    cw_none = ns["TuiCurrentWeek"](
        week_start_at=NOW_UTC - dt.timedelta(days=1),
        week_end_at=NOW_UTC + dt.timedelta(days=6),
        used_pct=12.0, five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=1.0, dollars_per_percent=None, latest_snapshot_at=NOW_UTC,
    )
    prior_none = ns["DataSnapshot"](cw_none, None, [], [], None, None, NOW_UTC)
    assert tui._snapshot_period_rolled_over(
        prior_none, reset_at + dt.timedelta(hours=1), UTC_TZ,
    ) is False


# ===========================================================================
# M5 Task 5.3 — concurrency invariant: no shared-row mutation (Codex F7)
# ===========================================================================


def test_rebuild_never_mutates_previously_published_rows(monkeypatch, tmp_path):
    """The SSE client threads read the previously-published DataSnapshot's row
    objects concurrently while the sync thread rebuilds. So a rebuild must build
    FRESH row objects and never mutate any object reachable from an
    already-published snapshot (spec §7 / Codex F7). Capture a published
    snapshot's daily/weekly rows + doctor payload, run a full recompute, and
    assert the captured objects are byte-unchanged."""
    import copy
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    captured_daily = snap1.daily_panel
    captured_weekly = snap1.weekly_periods
    captured_doctor = snap1.doctor_payload
    deep_daily = copy.deepcopy(captured_daily)
    deep_weekly = copy.deepcopy(captured_weekly)
    deep_doctor = copy.deepcopy(captured_doctor)

    # A new entry advances the signature → a genuine (non-idle) recompute that
    # exercises the Group A / session caches' fresh-object discipline.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(minutes=1), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )

    # The previously-published rows are byte-unchanged (no in-place mutation).
    assert captured_daily == deep_daily
    assert captured_weekly == deep_weekly
    assert captured_doctor == deep_doctor
    # And the rebuild produced FRESH row containers (new list identity).
    assert snap2.daily_panel is not captured_daily
    assert snap2.weekly_periods is not captured_weekly


# ===========================================================================
# M5 additional (a) — bound the SessionCache: aged-out sessions must be DROPPED
# from the store, not just filtered from the returned view (spec §5.2).
# ===========================================================================


def test_session_cache_drops_aged_out_sessions(monkeypatch, tmp_path):
    """Under a sliding `now`, a session whose last_activity ages past the
    365-day window must be evicted from the module store — otherwise the store
    grows unboundedly over long dashboard uptime."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_session_cache_state()
    T0 = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    # One session, active only on 2026-07-03 — inside [T0-365d, T0].
    _seed_sessions(tmp_path, {"sess-old": [("2026-07-03T11:00:00Z", "hi")]})
    _prime_cache(ns)

    # Cold build at T0 populates the store with the session.
    ns["_tui_build_sessions"](T0, skip_sync=True, use_session_cache=True)
    assert "sess-old" in sc.session_cache().get_all(), "cold build populated the store"

    # Slide `now` forward 366 days → the session is now BEFORE range_start
    # (now-365d), i.e. out of the window. A warm tick touches no session.
    T1 = T0 + dt.timedelta(days=366)
    ns["_tui_build_sessions"](T1, skip_sync=True, use_session_cache=True)
    assert "sess-old" not in sc.session_cache().get_all(), (
        "an aged-out session must be dropped from the store, not just the view"
    )


# ===========================================================================
# #269 M1 — B1 (trend / weekly-history) + B3 (forecast) share the per-weekref
# immutable-cost cache (spec §4). With `use_weekref_cost_cache=True` a closed
# subscription week's cost is served from `_WEEKREF_COST_CACHE` after the first
# compute; the OPEN week always recomputes. The flag defaults OFF so CLI callers
# stay byte-identical. Each wiring test ends with a cached-vs-from-scratch parity
# assert (the acceptance gate) or a spied reuse assert (the cache genuinely
# short-circuits the compute primitive on a same-signature second pass).
#
# The tests drive `reconcile_weekref_cache` BEFORE the cached builder pass,
# exactly as #269 M3 will wire it into the live rebuild (sync → dispatch
# signature → idle-check → reconcile → builders).
# ===========================================================================


def _seed_closed_reset_event_week(ns, stats_conn):
    """Seed four monotone weeks + attach a reset event to a CLOSED past week.

    Clone of ``_seed_reset_event_week`` above, but the reset is pinned onto a
    week whose ``week_end_at`` is strictly before ``NOW_UTC`` (2026-06-15 →
    2026-06-22 < 2026-07-04), so ``build_trend_view``'s reset branch treats it
    as CLOSED and ``cached_weekref_cost`` actually caches it (the open-week
    fixture the sibling helper builds never populates the cache). The current
    open week (2026-06-29 → 2026-07-06) carries NO reset event, so it flows the
    cheap ``get_latest_cost_for_week`` snapshot path — leaving exactly ONE
    reset-event week, and it is closed.
    """
    weeks = [
        ("2026-06-08", "2026-06-15", 20.0),
        ("2026-06-15", "2026-06-22", 40.0),
        ("2026-06-22", "2026-06-29", 60.0),
        ("2026-06-29", "2026-07-06", 80.0),
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()

    # Probe the canonical boundaries of the CLOSED 2026-06-15 week, then pin the
    # reset onto it. ``new_week_end_at == that week's canonical end`` rewrites
    # its ``week_start_at`` to ``effective`` (06:00), so ``effective IN
    # (week_start_at, week_end_at)`` holds → has_reset_event True. The distinct
    # ``old_week_end_at`` sentinel keeps this out of the in-place-credit path
    # (no ref split/drop).
    refs = ns["get_recent_weeks"](stats_conn, 12)
    target = next(
        r for r in refs
        if r.week_start_at and r.week_end_at
        and r.week_start.isoformat() == "2026-06-15"
    )
    effective = target.week_start_at[:11] + "06:00:00+00:00"
    stats_conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) "
        "VALUES (?,?,?,?)",
        (effective, "2020-01-01T00:00:00+00:00", target.week_end_at, effective),
    )
    stats_conn.commit()
    return target, effective


def test_trend_view_cached_matches_from_scratch(monkeypatch, tmp_path):
    """`use_weekref_cost_cache=True` yields a byte-identical TrendView."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        # Guard: a reset-event week is actually reachable (else the reset branch
        # never runs and the parity assert is vacuous).
        refs = ns["get_recent_weeks"](stats, 12)
        assert any(ns["_week_ref_has_reset_event"](stats, r) for r in refs), (
            "fixture must produce at least one reset-event week"
        )

        base = ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, display_tz=None, skip_sync=True,
        )
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id,
            max_mutation_seq=sig.entry_mutation_seq, reset_sig=sig.reset_sig,
        )
        cached = ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, display_tz=None, skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    assert cached == base


def test_trend_view_cached_reuses_closed_reset_week(monkeypatch, tmp_path):
    """A same-signature second pass serves the CLOSED reset week from cache —
    `_compute_cost_for_weekref` is NOT re-called for it (spy proves the hit)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id,
            max_mutation_seq=sig.entry_mutation_seq, reset_sig=sig.reset_sig,
        )
        ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, skip_sync=True,
            use_weekref_cost_cache=True,
        )
        first = calls["n"]
        ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    # Cold pass computed the (single, closed) reset week; the second pass with an
    # unchanged signature is a pure cache hit — zero further computes.
    assert first >= 1
    assert calls["n"] - first == 0


def _seed_forecast_4wk(ns, stats_conn):
    """Seed a low-percent current week + four CLOSED prior weeks so forecast's
    `_select_dollars_per_percent` takes the trailing-4-week median fallback.

    p_now < 10 on the current week (2026-06-29 → 2026-07-06, containing NOW)
    skips the this-week path; the four prior weeks all satisfy the eligibility
    filter (``ws < current_week_start AND we < now AND final_pct >= 1``), so the
    fallback loop calls ``_sum_cost_for_range`` once per closed week — the leg
    the weekref cache serves.
    """
    weeks = [
        ("2026-06-01", "2026-06-08", 55.0),
        ("2026-06-08", "2026-06-15", 60.0),
        ("2026-06-15", "2026-06-22", 65.0),
        ("2026-06-22", "2026-06-29", 70.0),
        ("2026-06-29", "2026-07-06", 5.0),   # current (open) week, low percent
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()


def test_forecast_dpp_cached_matches_from_scratch(monkeypatch, tmp_path):
    """`use_weekref_cost_cache=True` yields a byte-identical ForecastView."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_forecast_4wk(ns, stats)
        base = ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
        )
        # Guard: the trailing-4-week fallback actually fired (else the cache
        # serves nothing and the parity assert is vacuous).
        assert base.output is not None, "forecast produced no output"
        assert (
            base.output.inputs.dollars_per_percent_source
            == "trailing_4wk_median"
        ), "fixture must drive the trailing-4-week fallback path"

        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id,
            max_mutation_seq=sig.entry_mutation_seq, reset_sig=sig.reset_sig,
        )
        cached = ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    assert cached == base


def test_forecast_trailing_weeks_cache_hit_second_tick(monkeypatch, tmp_path):
    """The four trailing closed weeks cache-hit on a same-signature second pass;
    only the (uncached) current-week spend recomputes (spy `_sum_cost_for_range`)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    calls = {"n": 0}
    real = ns["_sum_cost_for_range"]

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(ns, "_sum_cost_for_range", spy)

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_forecast_4wk(ns, stats)
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id,
            max_mutation_seq=sig.entry_mutation_seq, reset_sig=sig.reset_sig,
        )
        ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
        first = calls["n"]
        ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    # Cold pass: 4 trailing weeks + the current-week spend all recompute.
    assert first >= 4
    # Warm pass (unchanged signature): the 4 closed weeks are cache hits; only
    # the open current-week spend (never cached) still calls the primitive.
    assert calls["n"] - first <= 1


# ===========================================================================
# #269 M3.1 — dispatch wiring in `_tui_build_snapshot` (spec §6). The live
# dashboard rebuild must (a) DRIVE `reconcile_weekref_cache` once per non-idle
# rebuild using the already-computed dispatch signature, and (b) pass
# `use_weekref_cost_cache=True` at the trend / weekly-history / forecast call
# sites. These end-to-end tests exercise the whole `_tui_build_snapshot` and
# prove the wiring via a spy on `_compute_cost_for_weekref` — the closed
# reset-week cost primitive the weekref cache serves — plus a warm-vs-fresh
# trend parity (the byte-identity acceptance gate). B2 (cache-report cache) was
# dropped at the M2.0 gate, so only the weekref cache is wired here.
# ===========================================================================


def _reset_rebuild_caches(sc):
    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    sc.reset_weekref_cost_state()


def test_snapshot_warm_rebuild_serves_closed_reset_week_from_cache(monkeypatch, tmp_path):
    """A warm (non-idle) dashboard rebuild whose only change is a CURRENT-week
    entry serves the CLOSED reset week from the weekref cache — proving
    `use_weekref_cost_cache=True` reached the trend + weekly-history builders
    (else the closed week would recompute on every non-idle tick). Spy on
    `_compute_cost_for_weekref`: zero calls on the warm rebuild; and the warm
    trend/weekly-history equal a from-scratch (reset-cache) rebuild."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)      # includes a 2026-06-20 entry (closed week)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        refs = ns["get_recent_weeks"](stats, 12)
        assert any(ns["_week_ref_has_reset_event"](stats, r) for r in refs), (
            "fixture must produce at least one reset-event week"
        )
    finally:
        stats.close()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    # Cold dashboard rebuild: populates the weekref cache with the closed week.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 1, "cold rebuild must compute the closed reset week"

    # Warm: a NEW current-week entry → signature advances → NON-IDLE rebuild,
    # but the closed reset week is unchanged → served from the weekref cache.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    before = calls["n"]
    later = NOW_UTC + dt.timedelta(seconds=5)
    warm = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] - before == 0, (
        f"warm rebuild must serve the closed reset week from the weekref cache "
        f"(use_weekref_cost_cache=True must reach the trend/weekly-history "
        f"builders), but _compute_cost_for_weekref ran {calls['n'] - before} times"
    )

    # Acceptance gate: the warm trend equals a from-scratch (reset-cache) rebuild
    # on the same post-insert DB at the same `now`.
    sc.reset_weekref_cost_state()
    sc.reset_dispatch_state()
    fresh = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert warm.trend == fresh.trend, "warm trend must equal from-scratch"
    assert warm.weekly_history == fresh.weekly_history, (
        "warm weekly-history must equal from-scratch"
    )


def test_snapshot_reconcile_evicts_late_ingest_closed_week(monkeypatch, tmp_path):
    """`_tui_build_snapshot` DRIVES `reconcile_weekref_cache` on the non-idle
    path: a late-ingest whose event time lands INSIDE the CLOSED reset week
    advances the new-entry watermark, so reconcile evicts that week and the warm
    rebuild recomputes it (spy delta >= 1) — and the warm trend equals a
    from-scratch rebuild. Were reconcile NOT driven (flag set but no reconcile),
    the stale cached cost would persist and the warm trend would diverge."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
    finally:
        stats.close()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 1, "cold rebuild computed the closed reset week"

    # Late-ingest: a NEW row with an OLD event time INSIDE the closed reset week
    # (2026-06-15 -> 2026-06-22). Its ingest id is new (advances the signature);
    # its timestamp reaches back → reconcile's watermark must evict the week.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("l1", "lm", "lr", "late", "2026-06-18T12:00:00Z", "claude-opus-4-8"),
    ])
    before = calls["n"]
    later = NOW_UTC + dt.timedelta(seconds=5)
    warm = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] - before >= 1, (
        "a late-ingest into the closed reset week must make reconcile evict it "
        "so the warm rebuild recomputes it (proves reconcile_weekref_cache runs "
        "on the non-idle path)"
    )

    # Acceptance gate: the warm trend equals a from-scratch (reset-cache) rebuild
    # — the reconcile-wiring guard (a stale hit would diverge here).
    sc.reset_weekref_cost_state()
    sc.reset_dispatch_state()
    fresh = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert warm.trend == fresh.trend, (
        "warm trend must equal from-scratch after a late-ingest into a closed "
        "reset week (reconcile must have evicted the stale cached cost)"
    )


# ===========================================================================
# #269 M3.3 — no-shared-mutation (spec §6 / Codex F7) for the weekref cache.
# The weekref-cost cache holds IMMUTABLE floats; each rebuild builds FRESH
# trend / weekly-history presentation rows from them. A rebuild must never
# mutate a row object reachable from an already-published DataSnapshot (SSE
# client threads read it concurrently).
# ===========================================================================


def test_snapshot_weekref_cache_never_mutates_published_trend_rows(monkeypatch, tmp_path):
    """Capture a published snapshot's trend + weekly-history rows (deep copy),
    run a warm (non-idle) recompute that re-reads the weekref cache, and assert
    the captured objects are byte-unchanged and the rebuild produced FRESH
    containers. If a builder mutated a cached value in place, the deep-copy
    compare would diverge."""
    import copy
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
    finally:
        stats.close()

    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    captured_trend = snap1.trend
    captured_history = snap1.weekly_history
    deep_trend = copy.deepcopy(captured_trend)
    deep_history = copy.deepcopy(captured_history)
    # Non-vacuity: the cold rebuild produced trend rows (the weekref cache served
    # the reset week into them).
    assert captured_trend, "cold rebuild must produce trend rows"

    # A new current-week entry → non-idle warm recompute that re-reads the
    # weekref cache (closed weeks served from cache, current recomputed).
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(minutes=1), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )

    assert captured_trend == deep_trend, (
        "the previously-published trend rows must be byte-unchanged (no in-place "
        "mutation of an object reachable from an already-published snapshot)"
    )
    assert captured_history == deep_history, (
        "the previously-published weekly-history rows must be byte-unchanged"
    )
    assert snap2.trend is not captured_trend, "the rebuild built a FRESH trend container"


# ===========================================================================
# #269 M4.1 — Win 1: raw-`project_path` fast path in `_resolve_project_key`
# (spec §14 Win 1, Codex-M4 P1). The expensive realpath/lstat walk runs once
# per DISTINCT raw spelling, not once per entry — WITHOUT dropping the
# normalized cache that collapses `full-path` symlink aliases.
# ===========================================================================
def test_resolve_project_key_raw_fast_path_dedups_realpath(monkeypatch, tmp_path):
    import os
    import _cctally_cache

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    raw = str(repo / "sub" / "dir")
    (repo / "sub" / "dir").mkdir(parents=True)

    real = os.path.realpath
    calls = {"n": 0}

    def spy(p):
        calls["n"] += 1
        return real(p)

    monkeypatch.setattr(os.path, "realpath", spy)

    cache: dict = {}
    keys = [
        _cctally_cache._resolve_project_key(raw, "git-root", cache)
        for _ in range(5)
    ]
    # All five resolutions agree, and realpath ran exactly ONCE (the raw
    # fast path short-circuits the other four). Pre-change: realpath ran 5x.
    assert all(k == keys[0] for k in keys)
    assert calls["n"] == 1, (
        f"raw fast path must dedup realpath to 1 call, got {calls['n']}"
    )


def test_resolve_project_key_parity_across_modes(tmp_path):
    import _cctally_cache

    repo = tmp_path / "gitrepo"
    (repo / ".git").mkdir(parents=True)
    nogit = tmp_path / "plain"
    nogit.mkdir()

    # A shared cache (memoized) and a fresh-cache-per-call reference must
    # agree for git-root, no-git, and NULL inputs (byte-identical resolution).
    shared: dict = {}
    for raw in (str(repo), str(nogit), None):
        memoized = _cctally_cache._resolve_project_key(raw, "git-root", shared)
        fresh = _cctally_cache._resolve_project_key(raw, "git-root", {})
        assert memoized == fresh
        assert memoized.display_key == fresh.display_key
        assert memoized.git_root == fresh.git_root
        assert memoized.is_no_git == fresh.is_no_git
        assert memoized.is_unknown == fresh.is_unknown

    git_key = _cctally_cache._resolve_project_key(str(repo), "git-root", shared)
    assert git_key.git_root == str(repo)
    nogit_key = _cctally_cache._resolve_project_key(str(nogit), "git-root", shared)
    assert nogit_key.is_no_git is True
    unknown_key = _cctally_cache._resolve_project_key(None, "git-root", shared)
    assert unknown_key.is_unknown is True and unknown_key.bucket_path == "(unknown)"


def test_resolve_project_key_fullpath_alias_collapse_preserved(tmp_path):
    """Codex-M4 P1: two symlink-alias spellings of one physical path still
    collapse to the FIRST-spelling `display_key` in mode='full-path'. The raw
    fast path must NOT replace the normalized cache."""
    import os
    import _cctally_cache

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    os.symlink(physical, alias)

    cache: dict = {}
    # Resolve the ALIAS spelling first — it becomes the first-seen display_key.
    k_alias = _cctally_cache._resolve_project_key(str(alias), "full-path", cache)
    # Then the PHYSICAL spelling (a different raw string, same realpath).
    k_phys = _cctally_cache._resolve_project_key(str(physical), "full-path", cache)

    assert k_alias.bucket_path == k_phys.bucket_path  # same normalized path
    assert k_phys.display_key == str(alias), (
        "the second alias must collapse to the FIRST spelling's display_key"
    )
    assert k_phys is k_alias  # returned via the normalized cache, not a new key


# ===========================================================================
# #269 M4.3 — wire `_build_projects_envelope` through the per-(project,week)
# cache (spec §14 Win 2). Governing invariant: the envelope JSON is
# byte-identical with the flag OFF *and* ON (reconcile-tested R-PROJ1/2/5).
# ===========================================================================
_PROJ_FIXTURE_DIR = None  # resolved lazily below
_PROJ_NOW = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _proj_fixture_path(name):
    import pathlib
    return pathlib.Path(__file__).resolve().parent / "fixtures" / "projects" / name


def _open_proj_copy(name, tmp_path):
    import shutil
    dst = tmp_path / name
    shutil.copy(_proj_fixture_path(name), dst)
    return sqlite3.connect(dst)


def _proj_sig_legs(conn):
    mid = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM session_entries"
    ).fetchone()[0]
    try:
        mw = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        mw = 0
    return int(mid), int(mw)


def _proj_reconcile(sc, conn, mid, mw):
    sc.reconcile_projects_env_cache(
        conn, max_entry_id=mid, max_mutation_seq=sc._entry_mutation_seq(conn),
        max_wus_id=mw, sf_sig=sc.session_files_sig(conn),
    )


def _build_env(d, conn, *, now, flag, current_week=None):
    d._projects_reset_memo()
    return d._build_projects_envelope(
        conn, now_utc=now, current_week=current_week, weeks_back=12,
        use_projects_env_cache=flag,
    )


@pytest.mark.parametrize("fixture", ["multi-week.db", "edge-cases.db"])
def test_projects_env_cached_matches_from_scratch(fixture):
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = sqlite3.connect(_proj_fixture_path(fixture))
    sc.reset_projects_env_state()
    base = _build_env(d, conn, now=_PROJ_NOW, flag=False)

    mid, mw = _proj_sig_legs(conn)
    # Cold cached pass: every week is a miss → recompute-and-populate.
    _proj_reconcile(sc, conn, mid, mw)
    cached_cold = _build_env(d, conn, now=_PROJ_NOW, flag=True)
    assert cached_cold == base, "cold cached envelope must equal from-scratch"

    # Warm cached pass (same signature): closed weeks are cache hits, only the
    # current week recomputes.
    _proj_reconcile(sc, conn, mid, mw)
    cached_warm = _build_env(d, conn, now=_PROJ_NOW, flag=True)
    assert cached_warm == base, "warm cached envelope must equal from-scratch"


def test_projects_env_warm_recomputes_only_current_week(monkeypatch):
    """#271 M4: a warm same-signature tick does NO full-window fold at all — the
    closed weeks are cache hits AND the current week takes the accumulator's
    empty-delta fast path (finalize the cached running ``mut`` unchanged, no
    re-fold). Both the closed-week wrapper and the current-week cold seed go
    through ``_aggregate_projects_week_raw``, so spying it captures every full
    fold: cold folds all weeks; warm folds none. (Non-vacuous: a buggy
    cold-refold-every-tick accumulator would re-fetch the current week via
    ``_fetch_all_raw`` → ``_aggregate_projects_week_raw`` → RED.)
    """
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = sqlite3.connect(_proj_fixture_path("multi-week.db"))
    sc.reset_projects_env_state()
    mid, mw = _proj_sig_legs(conn)

    real = d._aggregate_projects_week_raw
    seen = {"weeks": []}

    def spy(conn_, *, week_start, week_end, resolver_cache):
        seen["weeks"].append(week_start)
        return real(conn_, week_start=week_start, week_end=week_end,
                    resolver_cache=resolver_cache)

    monkeypatch.setattr(d, "_aggregate_projects_week_raw", spy)

    # Cold pass: every week (closed misses + the current-week cold seed) folds.
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)
    assert len(seen["weeks"]) >= 2, "cold pass must fold multiple weeks"

    seen["weeks"].clear()
    # Warm pass (same sig, empty delta): closed weeks are cache hits and the
    # current week finalizes the cached mut incrementally → ZERO full folds.
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)
    assert seen["weeks"] == [], (
        f"warm pass must do NO full-window fold (closed cached + current "
        f"incremental), got {seen['weeks']}"
    )


def test_projects_env_late_ingest_recomputes_past_week(tmp_path):
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = _open_proj_copy("multi-week.db", tmp_path)
    sc.reset_projects_env_state()
    mid, mw = _proj_sig_legs(conn)
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)  # cold populate

    # Late-ingest an OLD-timestamp entry into a PAST closed week (id > last_seen,
    # ts back in week 2026-04-13). New session_files row too.
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, cost_usd_raw) "
        "VALUES (?, 0, ?, ?, 0, 0, 0, 0, ?)",
        ("/jsonl/late/x.jsonl", "2026-04-15T10:00:00Z", "claude-opus-4-8", 1.25),
    )
    conn.execute(
        "INSERT OR REPLACE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?, 0, 0, 0, ?, ?, ?)",
        ("/jsonl/late/x.jsonl", "2026-05-19T11:50:00Z", "late-s", "/repos/late"),
    )
    conn.commit()

    mid2, mw2 = _proj_sig_legs(conn)
    _proj_reconcile(sc, conn, mid2, mw2)  # watermark evicts the past week
    cached = _build_env(d, conn, now=_PROJ_NOW, flag=True)
    fresh = _build_env(d, conn, now=_PROJ_NOW, flag=False)
    assert cached == fresh, "late-ingest cached rebuild must equal from-scratch"


def test_projects_env_max_wus_change_full_recompute(tmp_path):
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = _open_proj_copy("multi-week.db", tmp_path)
    sc.reset_projects_env_state()
    mid, mw = _proj_sig_legs(conn)
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)

    # A new weekly_usage_snapshots row (attribution denominator) → full clear.
    cw_start = d._projects_week_start_monday_utc(_PROJ_NOW)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
        " source, payload_json) VALUES (?, ?, ?, ?, 'test', '{}')",
        ("2026-05-19T06:00:00Z", cw_start.date().isoformat(),
         (cw_start + dt.timedelta(days=7)).date().isoformat(), 42.0),
    )
    conn.commit()

    mid2, mw2 = _proj_sig_legs(conn)
    assert mw2 != mw
    _proj_reconcile(sc, conn, mid2, mw2)
    cached = _build_env(d, conn, now=_PROJ_NOW, flag=True)
    fresh = _build_env(d, conn, now=_PROJ_NOW, flag=False)
    assert cached == fresh


def test_projects_env_session_files_backfill_invalidates(tmp_path):
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = _open_proj_copy("multi-week.db", tmp_path)
    sc.reset_projects_env_state()
    mid, mw = _proj_sig_legs(conn)
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)

    # Simulate a lazy attribution backfill: a NEW session_files row (Codex-M4
    # P2) that moves sf_sig WITHOUT any new session_entries / WUS row.
    conn.execute(
        "INSERT OR REPLACE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?, 0, 0, 0, ?, ?, ?)",
        ("/jsonl/cctally-dev/w00.jsonl", "2026-05-19T11:50:00Z",
         "cctally-dev-w00-s0", "/repos/moved-elsewhere"),
    )
    conn.commit()

    mid2, mw2 = _proj_sig_legs(conn)
    assert mid2 == mid and mw2 == mw  # neither entry nor WUS id moved
    _proj_reconcile(sc, conn, mid2, mw2)  # sf_sig moved → full clear
    cached = _build_env(d, conn, now=_PROJ_NOW, flag=True)
    fresh = _build_env(d, conn, now=_PROJ_NOW, flag=False)
    assert cached == fresh


def test_projects_env_rollover_populates_previously_current_week(tmp_path):
    load_script()
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    conn = _open_proj_copy("multi-week.db", tmp_path)
    sc.reset_projects_env_state()
    mid, mw = _proj_sig_legs(conn)
    _proj_reconcile(sc, conn, mid, mw)
    _build_env(d, conn, now=_PROJ_NOW, flag=True)  # cold at the original now

    # Advance now past the Monday so the previously-current week closes. Same
    # signature (no DB change) → reconcile is a no-op; the newly-closed week is
    # a cache MISS (never cached as the current week) → recompute-and-populate.
    later = _PROJ_NOW + dt.timedelta(days=7)
    _proj_reconcile(sc, conn, mid, mw)
    cached = _build_env(d, conn, now=later, flag=True)
    fresh = _build_env(d, conn, now=later, flag=False)
    assert cached == fresh


def test_projects_env_reconstruction_picks_global_earliest_week_key():
    """Codex-M4 P1 non-vacuity: when ONE ``bucket_path`` is reached by two
    DIFFERENT raw ``project_path`` spellings across two closed weeks, the no-git
    fallback (`_resolve_project_key`, `bin/_cctally_cache.py`) gives each week a
    DIFFERENT ``display_key`` for the SAME bucket — ``os.path.basename(raw) or
    raw`` differs by spelling while the normalized realpath ``bucket_path`` is
    identical. ``_assemble_projects_via_cache`` must reconstruct
    ``key_by_bucket[bp]`` as the ProjectKey of the GLOBAL-earliest
    ``(first_order, first_id)`` entry — reproducing the from-scratch walk's
    global first-seen (``timestamp_utc ASC, id ASC``) — NOT the later week's key.

    Every existing envelope fixture reaches each bucket by a SINGLE spelling, so
    every week's ``first_key`` for a bucket is identical and inverting the argmin
    (pick the LATEST week instead of the EARLIEST) is byte-invisible — the parity
    tests stay green. This test makes the two weeks' keys DIFFER, so the argmin
    DIRECTION is observable: inverting `cand < best` to `cand > best` in
    `_merge_week` flips the emitted key from ``EARLY`` to ``LATE``.
    """
    load_script()
    import _cctally_cache
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    sc.reset_projects_env_state()

    # Minimal conn whose CURRENT-week slice is EMPTY (no session_entries rows), so
    # the ONLY contributors to key_by_bucket are the two cached CLOSED weeks
    # seeded below — the current-week recompute adds nothing.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE session_entries (id INTEGER PRIMARY KEY, source_path TEXT, "
        "timestamp_utc TEXT, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_create_tokens INTEGER, "
        "cache_read_tokens INTEGER, cost_usd_raw REAL, "
        # #270 §8: the accumulator reads MAX(mutation_seq) + seeks the seq index.
        "mutation_seq INTEGER NOT NULL DEFAULT 0, mutation_min_ts TEXT)"
    )
    conn.execute(
        "CREATE INDEX idx_entries_mutation_seq "
        "ON session_entries(mutation_seq, mutation_min_ts)"
    )
    conn.execute(
        "CREATE TABLE session_files (path TEXT, session_id TEXT, project_path TEXT)"
    )
    conn.commit()

    bp = "/repos/proj"  # ONE normalized bucket_path, reached by two raw spellings.
    week1 = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)      # global-earliest
    week2 = dt.datetime(2026, 1, 12, tzinfo=dt.timezone.utc)     # later closed week
    cw_start = dt.datetime(2026, 1, 19, tzinfo=dt.timezone.utc)  # current (empty)

    # Same bucket_path, but the earlier week's entry carries display_key EARLY and
    # the later week's carries LATE — the no-git fallback's per-spelling display.
    early = _cctally_cache.ProjectKey(
        bucket_path=bp, display_key="EARLY", git_root=None,
    )
    late = _cctally_cache.ProjectKey(
        bucket_path=bp, display_key="LATE", git_root=None,
    )
    # ProjectKey equality is bucket_path-only, so these ARE the same bucket — the
    # exact condition that makes key_by_bucket[bp] non-deterministic per bucket.
    assert early == late

    wb_early = d._ProjWeekBucket(
        cost_usd=1.0, sessions_count=1,
        first_seen=week1, last_seen=week1,
        first_order="2026-01-05T09:00:00Z", first_id=5, first_key=early,
    )
    wb_late = d._ProjWeekBucket(
        cost_usd=2.0, sessions_count=1,
        first_seen=week2, last_seen=week2,
        first_order="2026-01-12T09:00:00Z", first_id=500, first_key=late,
    )
    sc.projects_env_week_put(sc.projects_env_week_key(week1), {bp: wb_early}, 1.0)
    sc.projects_env_week_put(sc.projects_env_week_key(week2), {bp: wb_late}, 2.0)

    # The current week is EMPTY (no session_entries), so cur_max_id = 0 and the
    # accumulator cold-folds nothing — key_by_bucket comes only from the two
    # seeded closed weeks.
    cur_max_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM session_entries"
    ).fetchone()[0]
    cur_max_seq = conn.execute(
        "SELECT COALESCE(MAX(mutation_seq), 0) FROM session_entries"
    ).fetchone()[0]
    _, _, key_by_bucket = d._assemble_projects_via_cache(
        conn, weeks_full=[week1, week2, cw_start],
        cw_start=cw_start, cw_end=cw_start + dt.timedelta(days=7),
        cur_max_id=cur_max_id, cur_max_seq=cur_max_seq,
    )

    assert key_by_bucket[bp].display_key == "EARLY", (
        "reconstruction must pick the GLOBAL-earliest week's ProjectKey "
        "(argmin over (first_order, first_id)), not the later week's; got "
        f"{key_by_bucket[bp].display_key!r}"
    )


# ===========================================================================
# #269 M4.5 — dispatch wiring: activate the envelope cache in the live rebuild.
# ===========================================================================
def test_live_rebuild_warm_reuses_closed_week_envelope(monkeypatch, tmp_path):
    """Two full `_tui_build_snapshot` rebuilds (dashboard mode). A new
    current-week entry between them forces a non-idle WARM rebuild that reuses
    the cached closed weeks AND folds the current week incrementally through the
    #271 M4 accumulator (only the one new row appended, no full-window fold) —
    and `snap.projects_envelope` stays byte-identical to a cold from-scratch
    rebuild on the same DB. The spy on `_aggregate_projects_week_raw` (the shared
    full-window fold behind both the closed-week wrapper and the current-week
    cold seed) proves the warm tick does NO full fold."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as d
    import _lib_snapshot_cache as sc

    for reset in (sc.reset_dispatch_state, sc.reset_group_a_state,
                  sc.reset_session_cache_state, sc.reset_doctor_memo,
                  sc.reset_weekref_cost_state, sc.reset_projects_env_state):
        reset()
    _seed_multiday_jsonl(tmp_path)

    # Cold rebuild → populates the per-(project, week) cache for the closed weeks.
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap1.projects_envelope is not None

    # New CURRENT-week entry → signature advances → non-idle WARM rebuild.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    seen = {"weeks": []}
    real = d._aggregate_projects_week_raw

    def spy(conn_, *, week_start, week_end, resolver_cache):
        seen["weeks"].append(week_start)
        return real(conn_, week_start=week_start, week_end=week_end,
                    resolver_cache=resolver_cache)

    monkeypatch.setattr(d, "_aggregate_projects_week_raw", spy)
    now2 = NOW_UTC + dt.timedelta(minutes=1)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=now2, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap2.projects_envelope is not None
    # WARM: NO full-window fold — closed weeks came from the cache, and the
    # current week's one new row was appended incrementally by the accumulator
    # (not re-folded via _fetch_all_raw → _aggregate_projects_week_raw).
    assert seen["weeks"] == [], (
        f"warm live rebuild must do NO full-window fold, got "
        f"{sorted(set(seen['weeks']))}"
    )

    # Byte-identity: a cold from-scratch rebuild on the same DB at the same now
    # (dispatch + envelope cache reset → full recompute) matches the warm one.
    monkeypatch.setattr(d, "_aggregate_projects_week_raw", real)
    sc.reset_dispatch_state()
    sc.reset_projects_env_state()
    snap3 = ns["_tui_build_snapshot"](
        now_utc=now2, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap2.projects_envelope == snap3.projects_envelope, (
        "warm cached envelope must byte-match the cold from-scratch rebuild"
    )


# ===========================================================================
# #271 M1 — current-bucket accumulator threaded through the three builders.
#
# The cached-parity / warm / late-ingest tests above now ALSO exercise the
# accumulator (each _group_a_*_buckets closure sets use_current_accumulator
# =True). These add the accumulator-specific behaviors, each proven
# cached==from-scratch, byte-identical:
#   - the append path is actually taken (acc mutated IN PLACE, not a cold /
#     fallback fresh acc);
#   - the now-advances delta leg folds an already-ingested future-dated row
#     once now reaches it (Codex-1);
#   - rollover across a bucket boundary cold-refolds the new current bucket;
#   - the prune-site reset clears the accumulator too.
# ===========================================================================


def _daily_cached(ns, stats, now, *, display_tz=UTC_TZ):
    """Cached (accumulator-on) daily build at ``now``; does NOT reset state."""
    dash = sys.modules["_cctally_dashboard"]
    dash._GROUP_A_CACHE_ENABLED = True
    return ns["_dashboard_build_daily_panel"](
        stats, now, n=30, skip_sync=True, use_group_a_cache=True,
        display_tz=display_tz)


def _daily_from_scratch(ns, stats, now, *, display_tz=UTC_TZ):
    """From-scratch (accumulator-off) daily build at ``now``."""
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    sc.reset_group_a_state()
    dash._GROUP_A_CACHE_ENABLED = False
    try:
        return ns["_dashboard_build_daily_panel"](
            stats, now, n=30, skip_sync=True, use_group_a_cache=True,
            display_tz=display_tz)
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def _seed_two_intraday(tmp_path):
    """Two 07-04 rows: 07:00 (in window at now=08:00) + 10:00 (future then)."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "s1.jsonl").write_text(
        _asst_line("a1", "am1", "ar1", "early", ts="2026-07-04T07:00:00Z")
        + _asst_line("a2", "am2", "ar2", "later", ts="2026-07-04T10:00:00Z")
    )


def test_current_accumulator_engaged_and_append_daily(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        _daily_cached(ns, stats, NOW_UTC)  # cold — populates the accumulator
        assert "daily" in sc._GROUP_A_CURRENT, "accumulator engaged for daily"
        acc_obj = sc._GROUP_A_CURRENT["daily"].acc
        # A new CURRENT-day (07-04) entry → non-empty delta → append path.
        _seed_multiday_jsonl(tmp_path, extra=[
            ("w1", "wm", "wr", "z", "2026-07-04T10:30:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        warm = _daily_cached(ns, stats, NOW_UTC)
        assert sc._GROUP_A_CURRENT["daily"].acc is acc_obj, (
            "append must mutate the accumulator in place (not cold / fallback)"
        )
        wide = _daily_from_scratch(ns, stats, NOW_UTC)
        assert warm == wide, "accumulator append == from-scratch (byte-identical)"
    finally:
        stats.close()


def test_current_accumulator_now_advances_daily(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_two_intraday(tmp_path)
    _prime_cache(ns)  # BOTH rows ingested (ids fixed) before any build
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        now1 = dt.datetime(2026, 7, 4, 8, 0, tzinfo=dt.timezone.utc)  # before 10:00 row
        cold = _daily_cached(ns, stats, now1)  # current 07-04 folds only the 07:00 row
        assert "daily" in sc._GROUP_A_CURRENT, "accumulator engaged (delta path under test)"
        cold_0704 = next(r for r in cold if r.date == "2026-07-04")
        now2 = dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.timezone.utc)  # past 10:00 row
        warm = _daily_cached(ns, stats, now2)  # the ts>last_now leg folds the 10:00 row
        warm_0704 = next(r for r in warm if r.date == "2026-07-04")
        assert warm_0704.cost_usd > cold_0704.cost_usd, (
            "advancing now must fold the already-ingested future-dated row (Codex-1)"
        )
        wide = _daily_from_scratch(ns, stats, now2)
        assert warm == wide, "now-advances warm build == from-scratch"
    finally:
        stats.close()


def test_current_accumulator_rollover_daily(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        now1 = dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.timezone.utc)  # current 07-04
        _daily_cached(ns, stats, now1)
        assert sc._GROUP_A_CURRENT["daily"].label == "2026-07-04"
        # New entry on 07-05 (the NEW current day after rollover).
        _seed_multiday_jsonl(tmp_path, extra=[
            ("r1", "rm", "rr", "roll", "2026-07-05T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        now2 = dt.datetime(2026, 7, 5, 12, 0, tzinfo=dt.timezone.utc)  # current 07-05
        rolled = _daily_cached(ns, stats, now2)
        assert sc._GROUP_A_CURRENT["daily"].label == "2026-07-05", (
            "rollover: accumulator cold-refolds the new current day"
        )
        wide = _daily_from_scratch(ns, stats, now2)
        assert rolled == wide, "rollover warm build == from-scratch"
    finally:
        stats.close()


def test_current_accumulator_prune_resets_daily(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    # Two CURRENT-day (07-04) entries so one can be deleted as a NON-max row.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("p1", "pm1", "pr1", "x1", "2026-07-04T10:00:00Z", "claude-opus-4-8"),
        ("p2", "pm2", "pr2", "x2", "2026-07-04T11:00:00Z", "claude-opus-4-8"),
    ])
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        _daily_cached(ns, stats, NOW_UTC)  # accumulator folds both extras
        assert "daily" in sc._GROUP_A_CURRENT
        # Delete a NON-max current-bucket row directly (an orphan prune that does
        # NOT lower MAX(id)): the signature can't catch it — the explicit
        # prune-site reset must clear the accumulator (else it serves stale).
        cconn = ns["open_cache_db"]()
        try:
            cconn.execute(
                "DELETE FROM session_entries "
                "WHERE timestamp_utc LIKE '2026-07-04T10:00:00%'"
            )
            cconn.commit()
        finally:
            cconn.close()
        sc.reset_group_a_state()  # the prune-site clear (_dashboard_self_heal_orphans)
        pruned = _daily_cached(ns, stats, NOW_UTC)
        wide = _daily_from_scratch(ns, stats, NOW_UTC)
        assert pruned == wide, "post-prune build (accumulator reset) == from-scratch"
    finally:
        stats.close()


def _monthly_cached(ns, stats, now, *, display_tz=UTC_TZ):
    dash = sys.modules["_cctally_dashboard"]
    dash._GROUP_A_CACHE_ENABLED = True
    return ns["_dashboard_build_monthly_periods"](
        stats, now, n=12, skip_sync=True, use_group_a_cache=True,
        display_tz=display_tz)


def _monthly_from_scratch(ns, stats, now, *, display_tz=UTC_TZ):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    sc.reset_group_a_state()
    dash._GROUP_A_CACHE_ENABLED = False
    try:
        return ns["_dashboard_build_monthly_periods"](
            stats, now, n=12, skip_sync=True, use_group_a_cache=True,
            display_tz=display_tz)
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_current_accumulator_engaged_and_append_monthly(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        _monthly_cached(ns, stats, NOW_UTC)  # cold; current month 2026-07
        assert "monthly" in sc._GROUP_A_CURRENT
        acc_obj = sc._GROUP_A_CURRENT["monthly"].acc
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("mx", "mmx", "mrx", "z", "2026-07-03T09:00:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        warm = _monthly_cached(ns, stats, NOW_UTC)
        assert sc._GROUP_A_CURRENT["monthly"].acc is acc_obj, "append path taken"
        wide = _monthly_from_scratch(ns, stats, NOW_UTC)
        assert warm == wide
    finally:
        stats.close()


def test_current_accumulator_rollover_monthly(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        now1 = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.timezone.utc)  # month 2026-07
        _monthly_cached(ns, stats, now1)
        assert sc._GROUP_A_CURRENT["monthly"].label == "2026-07"
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("ax", "amx", "arx", "aug", "2026-08-05T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        now2 = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.timezone.utc)  # month 2026-08
        rolled = _monthly_cached(ns, stats, now2)
        assert sc._GROUP_A_CURRENT["monthly"].label == "2026-08", (
            "rollover: accumulator cold-refolds the new current month"
        )
        wide = _monthly_from_scratch(ns, stats, now2)
        assert rolled == wide, "monthly rollover warm build == from-scratch"
    finally:
        stats.close()


def _weekly_cached(ns, stats, now):
    dash = sys.modules["_cctally_dashboard"]
    dash._GROUP_A_CACHE_ENABLED = True
    return ns["_dashboard_build_weekly_periods"](
        stats, now, n=12, skip_sync=True, use_group_a_cache=True)


def _weekly_from_scratch(ns, stats, now):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    sc.reset_group_a_state()
    dash._GROUP_A_CACHE_ENABLED = False
    try:
        return ns["_dashboard_build_weekly_periods"](
            stats, now, n=12, skip_sync=True, use_group_a_cache=True)
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_current_accumulator_engaged_and_append_weekly(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        _weekly_cached(ns, stats, NOW_UTC)  # cold
        assert "weekly" in sc._GROUP_A_CURRENT
        acc_obj = sc._GROUP_A_CURRENT["weekly"].acc
        # An entry in the CURRENT week (same day as now, before now).
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("wx", "wkx", "wrx", "z", "2026-07-04T06:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = _weekly_cached(ns, stats, NOW_UTC)
        assert sc._GROUP_A_CURRENT["weekly"].acc is acc_obj, "append path taken"
        wide = _weekly_from_scratch(ns, stats, NOW_UTC)
        assert warm == wide
    finally:
        stats.close()


def test_current_accumulator_rollover_weekly(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        now1 = NOW_UTC  # 2026-07-04
        _weekly_cached(ns, stats, now1)
        label1 = sc._GROUP_A_CURRENT["weekly"].label
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("wr", "wkr", "wrr", "roll", "2026-07-10T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        now2 = now1 + dt.timedelta(days=7)  # 2026-07-11 — a later subscription week
        rolled = _weekly_cached(ns, stats, now2)
        label2 = sc._GROUP_A_CURRENT["weekly"].label
        assert label2 != label1, "rollover: accumulator tracks the new current week"
        wide = _weekly_from_scratch(ns, stats, now2)
        assert rolled == wide, "weekly rollover warm build == from-scratch"
    finally:
        stats.close()


# ===========================================================================
# #271 M3 — Bug-K pre-credit segment cache (spec §18). Real-builder parity:
# the CLOSED `[original_start, effective)` pre-credit aggregate the Weekly
# panel re-folds per in-place credit event every warm tick is cacheable
# byte-identically. Fixtures here MUST contain an in-place credit event
# (`week_reset_events WHERE old_week_end_at = effective_reset_at_utc`) or the
# Bug-K synthesis never fires and the test is vacuous — so this seeds one,
# mirroring `test_dashboard_weekly_synthesizes_pre_credit_row`.
# ===========================================================================
NOW_BUGK = dt.datetime(2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc)


def _seed_bugk_credit_db(ns, tmp_path):
    """Seed stats.db (2 snapshots + an in-place credit event) and cache.db
    (two pre-credit entries in different models + one post-credit entry) so the
    Weekly Bug-K synthesis produces a multi-model pre-credit segment row.

    Returns ``(original_start_iso, effective_iso, week_end_iso)``.
    """
    import pathlib
    import sqlite3
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
        seed_weekly_usage_snapshot,
    )

    share = tmp_path / ".local" / "share" / "cctally"
    open_db = ns["open_db"]
    week_start = "2026-05-09T15:00:00+00:00"
    week_end = "2026-05-16T15:00:00+00:00"
    effective = "2026-05-15T17:00:00+00:00"

    with open_db() as conn:
        seed_weekly_usage_snapshot(
            conn, captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date="2026-05-09", week_end_date="2026-05-16",
            week_start_at=week_start, week_end_at=week_end, weekly_percent=67.0,
        )
        seed_weekly_usage_snapshot(
            conn, captured_at_utc="2026-05-15T19:00:00Z",
            week_start_date="2026-05-09", week_end_date="2026-05-16",
            week_start_at=week_start, week_end_at=week_end, weekly_percent=4.0,
        )
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-05-15T17:01:00Z", effective, week_end, effective),
        )
        conn.commit()

    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as cconn:
        seed_session_file(
            cconn, path="/fake/sess.jsonl", session_id="s1", project_path="/p",
        )
        # Two pre-credit models so the segment's `models` tuple has a
        # non-trivial (multi-entry) first-seen order; distinct costs so the
        # cost-desc sort is decisive.
        seed_session_entry(
            cconn, source_path="/fake/sess.jsonl", line_offset=0,
            timestamp_utc="2026-05-13T12:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=10_000, output_tokens=5_000,
            cache_create=100_000, cache_read=500_000,
        )
        seed_session_entry(
            cconn, source_path="/fake/sess.jsonl", line_offset=1,
            timestamp_utc="2026-05-13T13:00:00Z",
            model="claude-sonnet-4-5-20250929",
            input_tokens=2_000, output_tokens=1_000,
            cache_create=10_000, cache_read=50_000,
        )
        seed_session_entry(  # post-credit
            cconn, source_path="/fake/sess.jsonl", line_offset=2,
            timestamp_utc="2026-05-15T18:30:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1_000, cache_read=5_000,
        )
    return week_start, effective, week_end


def _bugk_expected_key(sc):
    return sc._bugk_key(
        dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 5, 15, 17, tzinfo=dt.timezone.utc),
    )


def test_bugk_segment_cache_parity(monkeypatch, tmp_path):
    """cache-off == cache-on (cold AND warm), byte-identical WeeklyPeriodRow
    lists — exact cost/tokens/models order for the synthesized pre-credit row."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _orig, eff, _wend = _seed_bugk_credit_db(ns, tmp_path)
    builder = ns["_dashboard_build_weekly_periods"]

    sc.reset_bugk_segment_state()
    with ns["open_db"]() as conn:
        off = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                      use_bugk_segment_cache=False)
    sc.reset_bugk_segment_state()
    with ns["open_db"]() as conn:
        on_cold = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                          use_bugk_segment_cache=True)
    with ns["open_db"]() as conn:  # warm — served from cache
        on_warm = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                          use_bugk_segment_cache=True)

    pre_off = [r for r in off if r.week_end_at == eff]
    assert len(pre_off) == 1, [r.label for r in off]  # non-vacuous: Bug-K fired
    assert len(pre_off[0].models) >= 2, "multi-model pre-credit segment expected"
    assert off == on_cold, "cache-on(cold) must be byte-identical to cache-off"
    assert off == on_warm, "cache-on(warm) must be byte-identical to cache-off"
    assert _bugk_expected_key(sc) in sc._BUGK_SEGMENT_CACHE


def test_bugk_segment_cache_warm_no_refetch(monkeypatch, tmp_path):
    """Warm tick serves the segment from cache: `get_entries` is NOT re-called
    for the closed pre-credit window (spy). The cold tick DID fetch it."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _orig, eff, _wend = _seed_bugk_credit_db(ns, tmp_path)
    builder = ns["_dashboard_build_weekly_periods"]
    eff_instant = dt.datetime(2026, 5, 15, 17, tzinfo=dt.timezone.utc)

    calls: list = []
    real = dash.get_entries

    def spy(a, b, **kw):
        calls.append((a, b))
        return real(a, b, **kw)

    monkeypatch.setattr(dash, "get_entries", spy)

    sc.reset_bugk_segment_state()
    with ns["open_db"]() as conn:
        cold = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                       use_bugk_segment_cache=True)
    # The pre-credit window fetch is identified by its `effective` upper bound
    # (only Bug-K fetches with `hi == effective`).
    cold_bugk = [c for c in calls if c[1] == eff_instant]
    assert len(cold_bugk) >= 1, "cold build must fetch the pre-credit window once"

    calls.clear()
    with ns["open_db"]() as conn:
        warm = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                       use_bugk_segment_cache=True)
    warm_bugk = [c for c in calls if c[1] == eff_instant]
    assert warm_bugk == [], (
        "warm build must NOT re-fetch the cached pre-credit window"
    )
    assert cold == warm


def test_bugk_segment_cache_late_ingest_recomputes(monkeypatch, tmp_path):
    """F1 late-ingest: an OLD-timestamp row lands inside a cached pre-credit
    window → reconcile evicts its segment → the next warm build recomputes,
    byte-identically to a from-scratch pass over the post-ingest DB."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib
    import sqlite3
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import seed_session_entry
    import _lib_snapshot_cache as sc

    _orig, eff, _wend = _seed_bugk_credit_db(ns, tmp_path)
    builder = ns["_dashboard_build_weekly_periods"]
    RS = (1, 1)  # constant reset-sig across both ticks (no credit change)

    # Tick 1: reconcile (cold, records last-seen) then a cold build populates
    # the segment cache.
    sc.reset_bugk_segment_state()
    cconn = ns["open_cache_db"]()
    try:
        max0 = sc._max_id(cconn, "session_entries")
        seq0 = sc._entry_mutation_seq(cconn)
        sc.reconcile_bugk_cache(cconn, max_entry_id=max0,
                                max_mutation_seq=seq0, reset_sig=RS)
    finally:
        cconn.close()
    with ns["open_db"]() as conn:
        cold = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                       use_bugk_segment_cache=True)
    cold_pre = next(r for r in cold if r.week_end_at == eff)
    assert _bugk_expected_key(sc) in sc._BUGK_SEGMENT_CACHE

    # Late-ingest an OLD-timestamp row (new id) INSIDE the pre-credit window.
    cache_path = tmp_path / ".local" / "share" / "cctally" / "cache.db"
    with sqlite3.connect(cache_path) as cc:
        seed_session_entry(
            cc, source_path="/fake/sess.jsonl", line_offset=99,
            timestamp_utc="2026-05-11T09:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=50_000, output_tokens=20_000,
            cache_create=200_000, cache_read=900_000,
        )
        # #270: mirror production ingest — bump the mutation counter and stamp
        # the just-seeded row (mutation_seq = the bumped counter, mutation_min_ts
        # = its event time), so the #270 seq watermark reaches this late row.
        cc.execute(
            "INSERT INTO cache_meta(key, value) "
            "VALUES ('session_entries_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(cache_meta.value AS INTEGER) + 1"
        )
        _seqv = cc.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='session_entries_mutation_seq'"
        ).fetchone()[0]
        cc.execute(
            "UPDATE session_entries SET mutation_seq = ?, "
            "mutation_min_ts = timestamp_utc WHERE source_path = '/fake/sess.jsonl'",
            (_seqv,),
        )

    # Tick 2: reconcile sees the advanced seq → watermark 05-11 < effective
    # 05-15 → the segment evicts.
    cconn = ns["open_cache_db"]()
    try:
        max1 = sc._max_id(cconn, "session_entries")
        seq1 = sc._entry_mutation_seq(cconn)
        assert max1 > max0
        assert seq1 > seq0
        sc.reconcile_bugk_cache(cconn, max_entry_id=max1,
                                max_mutation_seq=seq1, reset_sig=RS)
    finally:
        cconn.close()
    assert _bugk_expected_key(sc) not in sc._BUGK_SEGMENT_CACHE  # evicted

    with ns["open_db"]() as conn:
        warm = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                       use_bugk_segment_cache=True)
    with ns["open_db"]() as conn:
        scratch = builder(conn, NOW_BUGK, n=6, skip_sync=True,
                          use_bugk_segment_cache=False)
    warm_pre = next(r for r in warm if r.week_end_at == eff)
    assert warm_pre.cost_usd > cold_pre.cost_usd, "late row must raise the cost"
    assert warm == scratch, "post-eviction warm build == from-scratch"


# ===========================================================================
# #270 M2 — id-stable in-place finalization must recompute a CLOSED bucket.
#
# The primary #270 regression: a streaming-intermediate `session_entries` row
# in a now-CLOSED bucket is finalized IN PLACE by an appended duplicate-
# `(msg_id, req_id)` line (append-only JSONL) — same `id`, higher tokens, so
# `MAX(session_entries.id)` stays flat. Pre-M2 the id watermark / id gate MISS
# it → the Group A past bucket / Group B session serves a STALE aggregate.
# With the `mutation_seq` watermark the warm rebuild recomputes the affected
# bucket byte-identically to from-scratch. (Non-vacuity: reverting the Group A
# watermark to `new_min_timestamp(...max_id)` turns the daily case RED — the
# stash→RED proof in the M2 report.)
# ===========================================================================


def _tok_line(session_id, msg_id, req_id, *, ts, out_tokens, in_tokens=100,
              model="claude-opus-4-8", cwd="/Users/u/proj"):
    """One assistant JSONL line with EXPLICIT token counts (so a finalization
    can raise the tokens of an existing (msg_id, req_id) row)."""
    return json.dumps({
        "type": "assistant", "uuid": f"u-{msg_id}", "sessionId": session_id,
        "requestId": req_id, "timestamp": ts, "cwd": cwd,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _write_single_file(tmp_path, name, text):
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / name
    p.write_text(text)
    return p


@pytest.mark.parametrize("builder_name,n,label_attr,closed_ts,closed_label", [
    ("_dashboard_build_daily_panel", 30, "date",
     "2026-07-01T10:00:00Z", "2026-07-01"),
    ("_dashboard_build_monthly_periods", 12, "label",
     "2026-05-10T10:00:00Z", "2026-05"),
])
def test_group_a_idstable_finalization_recomputes_closed_bucket(
    monkeypatch, tmp_path, builder_name, n, label_attr, closed_ts, closed_label
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    # A CLOSED past bucket carrying a streaming-intermediate row (low tokens),
    # plus a current-bucket row so the builder has a live current label.
    intermediate = _tok_line("s1", "mC", "rC", ts=closed_ts, out_tokens=5)
    current = _tok_line("s1", "mNow", "rNow", ts="2026-07-04T09:00:00Z",
                        out_tokens=40)
    _write_single_file(tmp_path, "s1.jsonl", intermediate + current)
    _prime_cache(ns)

    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns[builder_name](stats, NOW_UTC, n=n, skip_sync=True,
                                use_group_a_cache=True)
        cold_row = next(r for r in cold if getattr(r, label_attr) == closed_label)

        # id-stable in-place finalization: APPEND a duplicate-(mC, rC) line with
        # higher tokens (append-only JSONL). sync_cache tail-ingests it → the
        # ON CONFLICT(msg_id, req_id) UPSERT finalizes the EXISTING row in place
        # (same id → MAX(id) flat), advancing only mutation_seq.
        finalization = _tok_line("s1", "mC", "rC", ts=closed_ts, out_tokens=9000)
        _write_single_file(tmp_path, "s1.jsonl",
                           intermediate + current + finalization)
        _prime_cache(ns)

        warm = ns[builder_name](stats, NOW_UTC, n=n, skip_sync=True,
                                use_group_a_cache=True)
        warm_row = next(r for r in warm if getattr(r, label_attr) == closed_label)
        assert warm_row.cost_usd > cold_row.cost_usd, (
            f"an id-stable finalization in CLOSED {closed_label} must recompute "
            "it (pre-#270-M2 the id watermark missed it → stale)"
        )
        # Byte-identity to a fresh from-scratch build on the post-UPSERT DB.
        sc.reset_group_a_state()
        prev = dash._GROUP_A_CACHE_ENABLED
        dash._GROUP_A_CACHE_ENABLED = False
        try:
            wide = ns[builder_name](stats, NOW_UTC, n=n, skip_sync=True,
                                    use_group_a_cache=True)
        finally:
            dash._GROUP_A_CACHE_ENABLED = prev
        assert warm == wide, "warm rebuild must equal from-scratch"
    finally:
        stats.close()


def test_weekly_idstable_finalization_recomputes_closed_week(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    # A CLOSED past subscription week (mid-June) with a streaming-intermediate
    # row + a current-week row.
    intermediate = _tok_line("s1", "mW", "rW", ts="2026-06-16T10:00:00Z",
                             out_tokens=5)
    current = _tok_line("s1", "mNow", "rNow", ts="2026-07-02T09:00:00Z",
                        out_tokens=40)
    _write_single_file(tmp_path, "s1.jsonl", intermediate + current)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        cold = _build_weekly(ns, stats, enabled=True)
        cold_total = _weekly_total(cold)
        finalization = _tok_line("s1", "mW", "rW", ts="2026-06-16T10:00:00Z",
                                 out_tokens=9000)
        _write_single_file(tmp_path, "s1.jsonl",
                           intermediate + current + finalization)
        _prime_cache(ns)
        # Warm build WITHOUT resetting the Group A cache (mirrors the sync tick).
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        warm = ns["_dashboard_build_weekly_periods"](
            stats, NOW_UTC, n=12, skip_sync=True, use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, "warm weekly rebuild must equal from-scratch"
        assert _weekly_total(warm) > cold_total, (
            "the id-stable finalization in a CLOSED week must recompute it"
        )
    finally:
        stats.close()


def test_group_a_timestamp_move_recomputes_both_buckets(monkeypatch, tmp_path):
    """A finalization whose new timestamp MOVES the row to a different closed
    day: the OLD day loses the row (mutation_min_ts reaches it) and the NEW day
    gains it — both byte-match from-scratch (Codex-2a)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    intermediate = _tok_line("s1", "mC", "rC", ts="2026-07-01T10:00:00Z",
                             out_tokens=5)
    current = _tok_line("s1", "mNow", "rNow", ts="2026-07-04T09:00:00Z",
                        out_tokens=40)
    _write_single_file(tmp_path, "s1.jsonl", intermediate + current)
    _prime_cache(ns)

    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                           use_group_a_cache=True)
        # Finalization MOVES the row 07-01 → 07-02 (both closed days).
        finalization = _tok_line("s1", "mC", "rC", ts="2026-07-02T08:00:00Z",
                                 out_tokens=9000)
        _write_single_file(tmp_path, "s1.jsonl",
                           intermediate + current + finalization)
        _prime_cache(ns)
        warm = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30,
                                                  skip_sync=True,
                                                  use_group_a_cache=True)
        wide = _build_daily(ns, stats, enabled=False)
        assert warm == wide, "both old + new day must byte-match from-scratch"
        # Non-vacuity: from-scratch really moved the row (07-02 has cost, and
        # 07-01 lost its only row).
        wide_0702 = next((r for r in wide if r.date == "2026-07-02"), None)
        assert wide_0702 is not None and wide_0702.cost_usd > 0
        wide_0701 = next((r for r in wide if r.date == "2026-07-01"), None)
        assert wide_0701 is None or wide_0701.cost_usd == 0
    finally:
        stats.close()


def test_sessions_idstable_finalization_reaggregates(monkeypatch, tmp_path):
    """Group B: an id-stable in-place finalization of an EXISTING session's row
    (appended duplicate (msg,req), higher tokens) must re-aggregate that session
    even though MAX(id) is flat — via the seq gate + seq-keyed affected set +
    seq-keyed _fetch_affected_session_entries (Codex-2c)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    a = _tok_line("sess-a", "ma", "ra", ts="2026-07-04T09:00:00Z", out_tokens=40)
    b_partial = _tok_line("sess-b", "mb", "rb", ts="2026-07-03T10:00:00Z",
                          out_tokens=5)
    _write_single_file(tmp_path, "sess-a.jsonl", a)
    _write_single_file(tmp_path, "sess-b.jsonl", b_partial)
    _prime_cache(ns)

    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    cold = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True)
    cold_b = next(r for r in cold if r.session_id == "sess-b")

    # Finalize sess-b's row IN PLACE (same (mb, rb), higher tokens).
    b_final = _tok_line("sess-b", "mb", "rb", ts="2026-07-03T10:00:00Z",
                        out_tokens=9000)
    _write_single_file(tmp_path, "sess-b.jsonl", b_partial + b_final)
    _prime_cache(ns)

    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True)
    warm_b = next(r for r in warm if r.session_id == "sess-b")
    assert warm_b.cost_usd > cold_b.cost_usd, (
        "an id-stable finalization of an EXISTING session must re-aggregate it "
        "(pre-#270-M2 the id gate missed it → stale)"
    )
    wide = _build_sessions(ns, enabled=False)
    assert warm == wide, "warm sessions rebuild must equal from-scratch"


def test_projects_env_outer_memo_busts_on_idstable_update(monkeypatch, tmp_path):
    """#270 §7d (Codex-2b): the OUTER whole-envelope memo `_PROJECTS_ENV_MEMO`
    must fold the mutation signal into its key, so an id-stable in-place
    finalization (MAX(id) flat) advances the key and the memo does NOT serve the
    stale envelope. Drives a LIVE `_build_projects_envelope` twice with a real
    cache.db conn, asserting the memo key's `max_id` leg is flat but the whole
    key changed via the `entry_mutation_seq` leg."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = sys.modules["_cctally_dashboard"]

    intermediate = _tok_line("s1", "mC", "rC", ts="2026-07-01T10:00:00Z",
                             out_tokens=5)
    _write_single_file(tmp_path, "s1.jsonl", intermediate)
    _prime_cache(ns)

    dash._projects_reset_memo()
    conn = ns["open_cache_db"]()
    dash._build_projects_envelope(conn, now_utc=NOW_UTC, current_week=None)
    key_before = dash._PROJECTS_ENV_MEMO["key"]
    max_id_before = conn.execute(
        "SELECT MAX(id) FROM session_entries").fetchone()[0]
    conn.close()
    assert key_before is not None

    # id-stable in-place finalization: appended duplicate-(mC, rC), higher tokens.
    finalization = _tok_line("s1", "mC", "rC", ts="2026-07-01T10:00:00Z",
                             out_tokens=9000)
    _write_single_file(tmp_path, "s1.jsonl", intermediate + finalization)
    _prime_cache(ns)

    conn = ns["open_cache_db"]()
    max_id_after = conn.execute(
        "SELECT MAX(id) FROM session_entries").fetchone()[0]
    # A second live build WITHOUT resetting the memo — it must NOT serve stale.
    dash._build_projects_envelope(conn, now_utc=NOW_UTC, current_week=None)
    key_after = dash._PROJECTS_ENV_MEMO["key"]
    conn.close()

    assert max_id_after == max_id_before, "the finalization left MAX(id) flat"
    assert key_before[0] == key_after[0], "the memo key's max_id leg is flat"
    assert key_after != key_before, (
        "the outer memo key must advance on an id-stable update (pre-#270 the "
        "key was identical → the memo stale-served the envelope)"
    )
    # The entry_mutation_seq leg (index 2) carried the change.
    assert key_after[2] > key_before[2], (
        "the entry_mutation_seq memo-key leg must advance"
    )


# ===========================================================================
# #272 — cache-report per-day cache parity (mirrors test_bugk_segment_cache_*).
#
# The builder serves CLOSED days from an immutable per-day cache and recomputes
# only the current (open) day; the warm (cache-served) envelope must be
# byte-identical to a from-scratch (cache-off) rebuild. Non-vacuous: the fixture
# seeds real cache activity across 2 projects × 14 closed days so by_project
# net_usd != 0 AND `have_all` becomes True (the warm today-only-fetch path is
# genuinely exercised — a stale cache would fail `cached_matches_from_scratch`).
# NOW_CR is deliberately at noon so `since = now - 14d` straddles midnight →
# 15 overlapping display dates (Codex-2), exercising the classify-pre-cap set.
# ===========================================================================
NOW_CR = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
CR_TZ = ZoneInfo("Etc/UTC")
CR_A_PATH = "/fake/a.jsonl"
CR_B_PATH = "/fake/b.jsonl"


def _seed_cache_report_db(ns, tmp_path, *, second_project_unattributed=False):
    """Seed cache.db with cache activity across two projects over 14 CLOSED days
    (2026-06-06 .. 06-19) plus the current open day (06-20), all inside the
    build's ``[now-14d, now]`` window for NOW_CR.

    Closed-day entries land at 18:00 (after ``since``'s 12:00 time-of-day, so the
    oldest straddling day 06-06 is IN the window and caches); the current-day
    entries land at 09:00 (before NOW_CR's 12:00). Distinct per-project seconds
    keep the two entries' ``timestamp_utc`` non-equal. Returns the closed date
    keys. With ``second_project_unattributed`` the second project's session_files
    row is NOT seeded, so its entries LEFT-JOIN to ``project_path=NULL`` →
    by_project shows ``(unknown)`` until a session_files row is later inserted
    (Codex-1 re-attribution).
    """
    import pathlib
    import sqlite3
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
    )

    share = tmp_path / ".local" / "share" / "cctally"
    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    model = "claude-sonnet-4-5"
    with sqlite3.connect(cache_path) as cc:
        seed_session_file(cc, path=CR_A_PATH, session_id="sa", project_path="/pa")
        if not second_project_unattributed:
            seed_session_file(cc, path=CR_B_PATH, session_id="sb", project_path="/pb")
        off = 0
        for day in range(6, 20):  # 06-06 .. 06-19 (14 closed days)
            seed_session_entry(
                cc, source_path=CR_A_PATH, line_offset=off,
                timestamp_utc=f"2026-06-{day:02d}T18:00:00Z", model=model,
                input_tokens=1000, output_tokens=200,
                cache_create=120_000, cache_read=500_000,
            )
            off += 1
            seed_session_entry(
                cc, source_path=CR_B_PATH, line_offset=off,
                timestamp_utc=f"2026-06-{day:02d}T18:00:01Z", model=model,
                input_tokens=1500, output_tokens=300,
                cache_create=80_000, cache_read=700_000,
            )
            off += 1
        # Current OPEN day (06-20) at 09:00 — before NOW_CR (12:00).
        seed_session_entry(
            cc, source_path=CR_A_PATH, line_offset=off,
            timestamp_utc="2026-06-20T09:00:00Z", model=model,
            input_tokens=500, output_tokens=80,
            cache_create=40_000, cache_read=250_000,
        )
        off += 1
        seed_session_entry(
            cc, source_path=CR_B_PATH, line_offset=off,
            timestamp_utc="2026-06-20T09:00:01Z", model=model,
            input_tokens=600, output_tokens=90,
            cache_create=30_000, cache_read=300_000,
        )
    return [f"2026-06-{d:02d}" for d in range(6, 20)]


def _build_cache_report(dash, *, use):
    return dash.build_cache_report_snapshot(
        now_utc=NOW_CR, anomaly_threshold_pp=15, anomaly_window_days=14,
        display_tz=CR_TZ, skip_sync=True, use_cache_report_cache=use,
    )


def _cr_reconcile(ns, sc, *, reset_sig=(1, 1)):
    """Run one `reconcile_cache_report_cache` on a fresh short-lived cache conn,
    threading the current signature legs (mirrors the TUI reconcile block)."""
    cconn = ns["open_cache_db"]()
    try:
        sc.reconcile_cache_report_cache(
            cconn,
            max_entry_id=sc._max_id(cconn, "session_entries"),
            max_mutation_seq=sc._entry_mutation_seq(cconn),
            reset_sig=reset_sig,
            sf_sig=sc.session_files_sig(cconn),
            bucket_tz=dt.timezone.utc,
            tz_key="Etc/UTC",
        )
    finally:
        cconn.close()


def test_cache_report_cached_matches_from_scratch(monkeypatch, tmp_path):
    """Warm (cache-served) == from-scratch, byte-identical serialized envelope —
    non-vacuous (by_project net_usd != 0; 14 closed days actually cached)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _seed_cache_report_db(ns, tmp_path)

    sc.reset_cache_report_state()
    scratch = _build_cache_report(dash, use=False)
    sc.reset_cache_report_state()
    cold = _build_cache_report(dash, use=True)   # primes the cache (full fetch)
    warm = _build_cache_report(dash, use=True)   # served from cache (today-only)

    to_dict = dash._cache_report_snapshot_to_dict
    # Non-vacuity: real activity + the cache genuinely populated with closed days.
    assert scratch.is_empty is False
    assert any(r.net_usd != 0.0 for r in scratch.by_project), "by_project must be non-zero"
    assert len(sc._CACHE_REPORT_DAY_CACHE) == 14, "the 14 closed days are cached"
    assert to_dict(cold) == to_dict(scratch), "cache-on(cold) == from-scratch"
    assert to_dict(warm) == to_dict(scratch), "cache-on(warm) == from-scratch"


def test_cache_report_cache_warm_no_refetch(monkeypatch, tmp_path):
    """Warm tick fetches ONLY the current day: exactly one narrow
    [today_start, now] query, NO full-window [since, now] fetch (spy)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _seed_cache_report_db(ns, tmp_path)
    since = NOW_CR - dt.timedelta(days=14)
    today_start = dt.datetime(2026, 6, 20, 0, 0, tzinfo=dt.timezone.utc)

    real = ns["get_claude_session_entries"]
    calls: list = []

    def spy(a, b, **kw):
        calls.append((a, b))
        return real(a, b, **kw)

    monkeypatch.setattr(dash, "get_claude_session_entries", spy)

    sc.reset_cache_report_state()
    cold = _build_cache_report(dash, use=True)
    # Cold: one full-window fetch (lower bound == since).
    assert len(calls) == 1, calls
    assert calls[0][0] == since, "cold build must fetch the full window once"

    calls.clear()
    warm = _build_cache_report(dash, use=True)
    # Warm: exactly ONE narrow current-day fetch; no full-window refetch.
    assert len(calls) == 1, calls
    assert calls[0][0] == today_start, "warm build must fetch only the current day"
    assert calls[0][0] != since, "warm build must NOT refetch the full window"
    assert dash._cache_report_snapshot_to_dict(warm) == \
        dash._cache_report_snapshot_to_dict(cold)


def test_cache_report_cache_late_ingest_recomputes(monkeypatch, tmp_path):
    """F1 late-ingest: an OLD-timestamp row lands inside a CLOSED cached day →
    the seq-gated reconcile evicts that day (and later ones) → the next warm
    build recomputes, byte-identical to a from-scratch pass over the new DB."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib
    import sqlite3
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import seed_session_entry
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _seed_cache_report_db(ns, tmp_path)

    sc.reset_cache_report_state()
    _cr_reconcile(ns, sc)               # cold — records last-seen (seq0)
    cold = _build_cache_report(dash, use=True)   # primes the cache
    assert "2026-06-10" in sc._CACHE_REPORT_DAY_CACHE
    assert "2026-06-09" in sc._CACHE_REPORT_DAY_CACHE

    # Late-ingest an OLD-timestamp row INSIDE the closed 06-10 day; bump + stamp
    # ONLY the new row so the #270 seq watermark lands on 06-10 (not earlier).
    cache_path = tmp_path / ".local" / "share" / "cctally" / "cache.db"
    with sqlite3.connect(cache_path) as cc:
        seed_session_entry(
            cc, source_path=CR_A_PATH, line_offset=900,
            timestamp_utc="2026-06-10T09:00:00Z", model="claude-sonnet-4-5",
            input_tokens=90_000, output_tokens=20_000,
            cache_create=300_000, cache_read=1_000_000,
        )
        cc.execute(
            "INSERT INTO cache_meta(key, value) "
            "VALUES ('session_entries_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(cache_meta.value AS INTEGER) + 1"
        )
        seqv = cc.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='session_entries_mutation_seq'"
        ).fetchone()[0]
        cc.execute(
            "UPDATE session_entries SET mutation_seq = ?, "
            "mutation_min_ts = timestamp_utc "
            "WHERE source_path = ? AND line_offset = 900",
            (seqv, CR_A_PATH),
        )

    _cr_reconcile(ns, sc)               # seq advanced → wm 06-10 → evict >= 06-10
    assert "2026-06-10" not in sc._CACHE_REPORT_DAY_CACHE, "06-10 evicted"
    assert "2026-06-09" in sc._CACHE_REPORT_DAY_CACHE, "earlier days survive"

    warm = _build_cache_report(dash, use=True)
    scratch = _build_cache_report(dash, use=False)
    to_dict = dash._cache_report_snapshot_to_dict
    assert to_dict(cold) != to_dict(scratch), "the late row must change the output"
    assert to_dict(warm) == to_dict(scratch), "post-eviction warm == from-scratch"


def test_cache_report_cache_session_files_reattribution(monkeypatch, tmp_path):
    """Codex-1: a CLOSED day whose session_files attribution moves
    ``(unknown)``→``/pb`` (via a new session_files row → sf_sig moves) with no
    session_entries change → the sf_sig full-clear → by_project == from-scratch."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib
    import sqlite3
    import sys as _sys

    _sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import seed_session_file
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    # Second project starts WITHOUT a session_files row → (unknown).
    _seed_cache_report_db(ns, tmp_path, second_project_unattributed=True)

    cconn = ns["open_cache_db"]()
    try:
        sf0 = sc.session_files_sig(cconn)
    finally:
        cconn.close()

    sc.reset_cache_report_state()
    _cr_reconcile(ns, sc)               # cold — records last-seen (sf_sig=sf0)
    cold = _build_cache_report(dash, use=True)   # primes the cache
    assert any(r.key == "(unknown)" for r in cold.by_project), \
        "second project must start unattributed"

    # Attribute /fake/b.jsonl → /pb by INSERTing a session_files row (COUNT+1 →
    # session_files_sig moves); NO new session_entries, flat max_id/seq.
    cache_path = tmp_path / ".local" / "share" / "cctally" / "cache.db"
    with sqlite3.connect(cache_path) as cc:
        seed_session_file(cc, path=CR_B_PATH, session_id="sb", project_path="/pb")

    cconn = ns["open_cache_db"]()
    try:
        assert sc.session_files_sig(cconn) != sf0, "sf_sig must move on the new row"
    finally:
        cconn.close()
    _cr_reconcile(ns, sc)               # sf_sig changed → full-clear
    assert not sc._CACHE_REPORT_DAY_CACHE, "sf_sig change must full-clear the cache"

    warm = _build_cache_report(dash, use=True)
    scratch = _build_cache_report(dash, use=False)
    assert any(r.key == "/pb" for r in scratch.by_project), \
        "re-attribution must surface /pb from-scratch"
    assert not any(r.key == "(unknown)" for r in warm.by_project), \
        "warm must reflect the re-attribution (not stale-serve (unknown))"
    assert dash._cache_report_snapshot_to_dict(warm) == \
        dash._cache_report_snapshot_to_dict(scratch)


def test_cache_report_cache_window_straddle(monkeypatch, tmp_path):
    """Codex-2: a since-straddling window yields window_days+1 (=15) display
    dates; the cached restitch classifies over the FULL set before the `days`
    cap drops the oldest, and matches from-scratch byte-for-byte."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _seed_cache_report_db(ns, tmp_path)  # NOW_CR (noon) straddles → 15 dates

    sc.reset_cache_report_state()
    scratch = _build_cache_report(dash, use=False)
    sc.reset_cache_report_state()
    cold = _build_cache_report(dash, use=True)   # primes
    warm = _build_cache_report(dash, use=True)

    to_dict = dash._cache_report_snapshot_to_dict
    # 15 overlapping display dates (06-06..06-20); the cap keeps the newest 14.
    assert len(warm.days) == 14
    assert "2026-06-06" in sc._CACHE_REPORT_DAY_CACHE, \
        "the oldest straddling day IS cached (Codex-2 non-vacuity)"
    assert "2026-06-06" not in {d.date for d in warm.days}, \
        "the oldest straddling day is dropped from the days cap"
    assert to_dict(cold) == to_dict(scratch)
    assert to_dict(warm) == to_dict(scratch), \
        "warm restitch over the full straddling set == from-scratch"


def test_cache_report_cache_f7_no_shared_mutation(monkeypatch, tmp_path):
    """F7: a cached day unit is never mutated tick-to-tick — its object identity
    and every field are stable across two later rebuilds (the reconstructed rows
    are fresh objects, so classify never touches the frozen cached primitives)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_dashboard as dash
    import _lib_snapshot_cache as sc

    _seed_cache_report_db(ns, tmp_path)

    sc.reset_cache_report_state()
    _build_cache_report(dash, use=True)          # cold prime
    unit = sc.cache_report_day_get("2026-06-10")
    assert unit is not None
    captured_net = unit.net_usd
    captured_pp = unit.project_partials

    _build_cache_report(dash, use=True)          # warm tick (reconstruct+classify)
    _build_cache_report(dash, use=True)          # another tick

    same = sc.cache_report_day_get("2026-06-10")
    assert same is unit, "cached day object identity is stable across ticks"
    assert same.net_usd == captured_net, "cached net_usd is never mutated (F7)"
    assert same.project_partials is captured_pp, "project_partials tuple identity stable"
    assert same.project_partials == captured_pp
