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
