"""Unit tests for `_build_projects_envelope` (spec §5.2, §6.2 / plan Task 1).

Drives `bin/build-projects-fixtures.py`'s three SQLite scenarios against
the envelope builder. Tests are pure-function (no fake HOME / monkeypatching
of `CACHE_DB_PATH`): the fixture DBs carry both cache-side
(``session_entries``, ``session_files``) and stats-side
(``weekly_usage_snapshots``) tables in one file, so a single
``sqlite3.connect()`` is sufficient.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

# `_cctally_dashboard` does `sys.modules["cctally"].BLOCK_DURATION` at
# import time, so the `cctally` namespace must be populated first.
# `conftest.load_script` registers it. Resolve the dashboard sibling
# *afterwards* so its module-level ``sys.modules["cctally"].X`` reads
# resolve cleanly.
from conftest import load_script  # noqa: E402


_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402

import _lib_snapshot_cache as sc  # noqa: E402  (bin/ on sys.path above)

_build_projects_envelope = _cctally_dashboard._build_projects_envelope


FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "projects"
NOW_UTC = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _open(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_aggregate_projects_week_raw_finalize_matches_public():
    """#271 §20 Codex-P1a: the raw fold + finalize split reproduces the public
    ``_aggregate_projects_week`` output byte-for-byte, and the raw ``mut`` keeps
    real session SETS (not a ``range`` of the count) so the accumulator can dedup
    a resumed session across a later warm delta.
    """
    conn = _open(FIXTURE_DIR / "multi-week.db")
    ws = _cctally_dashboard._projects_week_start_monday_utc(NOW_UTC)
    we = ws + dt.timedelta(days=7)
    public, public_total = _cctally_dashboard._aggregate_projects_week(
        conn, week_start=ws, week_end=we, resolver_cache={})
    mut, raw_total, tail = _cctally_dashboard._aggregate_projects_week_raw(
        conn, week_start=ws, week_end=we, resolver_cache={})
    finalized = _cctally_dashboard._finalize_projects_mut(mut)
    assert finalized == public, "raw+finalize must equal the public shape"
    assert raw_total == public_total, "entry-order week_total must match"
    # mut keeps real sets (not range) so the accumulator can dedup later.
    assert mut, "multi-week fixture has current-week activity"
    assert all(isinstance(v["sessions"], set) for v in mut.values())
    # tail is the (ts_iso, id) of the last folded current-week entry.
    assert tail is not None
    assert isinstance(tail[0], str) and isinstance(tail[1], int)


def test_projects_iter_after_seq_filters_by_mutation_seq():
    """``after_seq`` returns exactly the rows with ``mutation_seq > after_seq``
    from the ``[since, until]`` window (order-independent set equality) — the warm
    delta seek is a correct subset of the full window fetch. The fixture stamps
    ``mutation_seq == id`` (#270 §8).
    """
    conn = _open(FIXTURE_DIR / "multi-week.db")
    ws = _cctally_dashboard._projects_week_start_monday_utc(NOW_UTC)
    we = ws + dt.timedelta(days=7)
    full = list(_cctally_dashboard._projects_iter_session_entries(
        conn, since=ws, until=we))
    assert full, "current week has rows"
    seqs = sorted(
        r[0] for r in conn.execute(
            "SELECT mutation_seq FROM session_entries").fetchall())
    cut = seqs[len(seqs) // 2]  # a mid mutation_seq
    delta = list(_cctally_dashboard._projects_iter_session_entries(
        conn, since=ws, until=we, after_seq=cut))
    seq_by_id = dict(conn.execute(
        "SELECT id, mutation_seq FROM session_entries").fetchall())
    expected = {r for r in full if seq_by_id[r[0]] > cut}
    assert set(delta) == expected, "after_seq must return exactly mutation_seq>cut rows"
    assert all(seq_by_id[r[0]] > cut for r in delta)


def test_projects_after_seq_uses_mutation_seq_index():
    """#270 §8 (re-key of #271 §20 Codex-P2b): the warm-delta ``after_seq`` fetch
    seeks by ``idx_entries_mutation_seq``, NOT a full ``idx_entries_timestamp``
    scan NOR a bare table ``SCAN`` over the whole current week — otherwise the
    ~63ms floor creeps back. The ``+e.timestamp_utc`` unary-plus no-op deprioritizes
    the timestamp index and ``ORDER BY e.mutation_seq`` matches the index's leading
    column, so the planner drives off the mutation_seq seek WITHOUT ``INDEXED BY``
    (unusable — production runs this against a TEMP VIEW). Mirrors the SQL emitted
    by ``_projects_iter_session_entries``' ``after_seq`` branch, exercised BOTH on
    the base table (here) AND on a view.
    """
    conn = _open(FIXTURE_DIR / "multi-week.db")
    entries_sql = (
        "SELECT e.id, e.timestamp_utc, e.model, e.input_tokens, "
        "       e.output_tokens, e.cache_create_tokens, e.cache_read_tokens, "
        "       e.cost_usd_raw, e.source_path, sf.session_id, sf.project_path "
        "FROM {tbl} e "
        "LEFT JOIN session_files sf ON sf.path = e.source_path "
        "WHERE e.mutation_seq > ? AND +e.timestamp_utc >= ? AND +e.timestamp_utc <= ? "
        "ORDER BY e.mutation_seq ASC"
    )
    # Also assert against a TEMP VIEW — the production wiring wraps
    # `session_entries` as a view over the ATTACHed cache.db (so `INDEXED BY`
    # would be invalid and a plan that only works on the base table is a trap).
    conn.execute(
        "CREATE TEMP VIEW se_view AS SELECT * FROM session_entries")
    for tbl in ("session_entries", "se_view"):
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + entries_sql.format(tbl=tbl),
            (0, "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),
        ).fetchall()
        text = " ".join(str(r) for r in plan).lower()
        assert "idx_entries_mutation_seq" in text, \
            f"[{tbl}] must be seeked by the mutation_seq index; plan was: {text}"
        assert "idx_entries_timestamp" not in text, \
            f"[{tbl}] must NOT scan the current-week timestamp index; plan was: {text}"
        assert "scan" not in text, \
            f"[{tbl}] must NOT be a bare table SCAN; plan was: {text}"


def test_current_week_rows_sorted_desc_by_cost():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    rows = env["current_week"]["rows"]
    assert len(rows) >= 2
    costs = [r["cost_usd"] for r in rows]
    assert costs == sorted(costs, reverse=True), \
        f"rows not desc by cost: {costs}"


def test_current_week_total_matches_row_sum():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    cw = env["current_week"]
    assert abs(
        cw["total_cost_usd"] - sum(r["cost_usd"] for r in cw["rows"])
    ) < 1e-9


def test_attributed_pct_none_when_no_snapshot():
    """Per spec §2.7: weeks without weekly_usage_snapshots → attributed_pct=None."""
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    pcts = [r["attributed_pct"] for r in env["current_week"]["rows"]]
    # edge-cases fixture has no weekly_usage_snapshots row this week
    assert all(p is None for p in pcts), f"expected all-None: {pcts}"


def test_disambiguation_collision_keys():
    """`foo (repos)` vs `foo (forks)` in edge-cases.db."""
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    keys = {r["key"] for r in env["current_week"]["rows"]}
    assert "foo (repos)" in keys, f"keys: {keys}"
    assert "foo (forks)" in keys, f"keys: {keys}"


def test_unknown_bucket_emitted():
    conn = _open(FIXTURE_DIR / "edge-cases.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    keys = {r["key"] for r in env["current_week"]["rows"]}
    assert "(unknown)" in keys, f"keys: {keys}"


def test_trend_weeks_oldest_to_newest():
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    trend = env["trend"]
    dates = [w["week_start_date"] for w in trend["weeks"]]
    assert dates == sorted(dates), f"weeks not oldest→newest: {dates}"


def test_trend_per_project_weekly_cost_aligned():
    """`weekly_cost[j]` index aligns with `weeks[j]`."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    n_weeks = len(env["trend"]["weeks"])
    for p in env["trend"]["projects"]:
        assert len(p["weekly_cost"]) == n_weeks
        assert len(p["weekly_pct"]) == n_weeks


def test_window_weeks_clamped_to_history():
    """`weeks_back=12` on a fixture whose entries cover ≤1 week →
    `window_weeks` reflects the actual emitted span (≤12)."""
    conn = _open(FIXTURE_DIR / "single-week.db")
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    assert env["trend"]["window_weeks"] <= 12
    assert env["trend"]["window_weeks"] == len(env["trend"]["weeks"])


def test_determinism():
    """Same inputs → byte-identical output (memory: R-PROJ5 invariant)."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    assert json.dumps(env_a, sort_keys=True) == json.dumps(env_b, sort_keys=True)


def test_memo_cache_hit_returns_same_object():
    """Pre-probe memo: second call with the same (max_id, cw_key,
    weeks_back) returns the IDENTICAL object (id() match), proving the
    inner aggregation walk did not re-run."""
    # Reset the memo so we measure a clean state.
    _cctally_dashboard._projects_reset_memo()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    # Cache HIT: the second call returns the very same dict.
    assert env_a is env_b, (
        "memo MUST return the same object reference on cache hit"
    )


def test_memo_invalidates_on_weeks_back_change():
    """Different `weeks_back` → different memo key → fresh aggregation."""
    _cctally_dashboard._projects_reset_memo()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    env_a = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    env_b = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=4,
    )
    assert env_a is not env_b
    # Smaller window: trend.window_weeks shrinks.
    assert env_b["trend"]["window_weeks"] <= 4


def test_current_week_rows_populated_after_midweek_reset():
    """B1 regression: ``TuiCurrentWeek.week_start_at`` shifted by
    ``_apply_midweek_reset_override`` (e.g. Friday 13:00 UTC after an
    Anthropic-shifted mid-week reset) MUST NOT empty out the panel.

    Buggy behavior: ``cw_start`` was set to the raw mid-week instant
    (Friday 00:00 UTC after a `.replace(microsecond=0)` snap), but the
    bucket aggregator anchors every entry to its ISO-Monday via
    ``_week_for``. The lookup ``buckets.get((bp, cw_start))`` then
    targeted Friday 00:00 UTC and missed every Monday-keyed bucket,
    yielding ``rows: []`` / ``total_cost_usd: 0.0``.

    Fixed behavior: ``cw_start`` is canonicalized to the containing
    ISO-Monday-UTC week start via ``_projects_week_start_monday_utc``,
    so the lookup matches whichever bucket the aggregator wrote.

    Authoritative CLAUDE.md memory: "``TuiCurrentWeek.week_start_at``
    is NOT a valid ``week_start_date`` lookup key after a mid-week
    reset."
    """
    from types import SimpleNamespace

    # multi-week.db has 4 projects active in the current week (the
    # NOW_UTC=2026-05-19 Tuesday-anchored bucket = Monday 2026-05-18 UTC).
    conn = _open(FIXTURE_DIR / "multi-week.db")

    # Mid-week reset instant: Friday 2026-05-22 13:00 UTC — same calendar
    # week as NOW_UTC, but a non-Monday boundary that the legacy code
    # would have stranded as the bucket-lookup key.
    midweek_reset_at = dt.datetime(
        2026, 5, 22, 13, 0, 0, tzinfo=dt.timezone.utc,
    )
    cw_stub = SimpleNamespace(week_start_at=midweek_reset_at)

    _cctally_dashboard._projects_reset_memo()
    env = _build_projects_envelope(
        conn,
        now_utc=NOW_UTC,
        current_week=cw_stub,
        weeks_back=12,
    )

    cw = env["current_week"]
    # Canonical Monday anchor: 2026-05-18 (the Monday containing both
    # NOW_UTC and the mid-week reset instant).
    assert cw["week_start_date"] == "2026-05-18", \
        f"cw_start should snap to Monday-UTC: {cw['week_start_date']}"
    assert cw["week_start_at"] == "2026-05-18T00:00:00Z", \
        f"cw_start ISO should be Monday-UTC 00:00:00Z: {cw['week_start_at']}"
    assert len(cw["rows"]) > 0, (
        "B1 regression: current_week.rows MUST be populated after a "
        f"mid-week reset; got {cw['rows']!r}"
    )
    assert cw["total_cost_usd"] > 0.0, (
        "B1 regression: current_week.total_cost_usd MUST be > 0 after "
        f"a mid-week reset; got {cw['total_cost_usd']!r}"
    )
    # Row-sum invariant still holds (mirrors test_current_week_total_matches_row_sum).
    assert abs(
        cw["total_cost_usd"] - sum(r["cost_usd"] for r in cw["rows"])
    ) < 1e-9


def test_memo_invalidates_on_new_session_entry():
    """A new row in `session_entries` bumps `MAX(id)` → memo must miss.

    This is the per-tick raison d'être of the memo (cache busts when fresh
    activity arrives between two sync ticks). Without this test the
    invalidation path is silently regressable.
    """
    import shutil
    import tempfile

    _cctally_dashboard._projects_reset_memo()
    # Copy multi-week.db to a temp file so we can mutate it freely.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        shutil.copyfile(FIXTURE_DIR / "multi-week.db", tmp_path)
        conn = _open(tmp_path)
        env_a = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        # Insert one new session_entries row. We don't care about the
        # numeric values — only that MAX(id) advances by 1.
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, "
            " input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("/tmp/synthetic.jsonl", 0,
             NOW_UTC.isoformat().replace("+00:00", "Z"),
             "claude-sonnet-4-5", 100, 100),
        )
        conn.commit()
        env_b = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        assert env_a is not env_b, (
            "memo MUST invalidate when MAX(session_entries.id) advances"
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_memo_invalidates_on_new_weekly_usage_snapshot():
    """A new row in `weekly_usage_snapshots` bumps `MAX(id)` → memo MUST
    miss so attributed_pct / trend total_pct reflect the fresh
    weekly_percent. Regression for code-review Fix 3: the throttled OAuth
    refresh path advances weekly_percent independently from
    session_entries writes; previously the memo only probed
    session_entries.MAX(id) and served stale attribution.
    """
    import shutil
    import tempfile

    _cctally_dashboard._projects_reset_memo()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)
    try:
        shutil.copyfile(FIXTURE_DIR / "multi-week.db", tmp_path)
        conn = _open(tmp_path)
        env_a = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        # Insert a synthetic weekly_usage_snapshots row. Match the
        # columns that exist in the fixture schema (table is shared with
        # the live DB shape; we only need MAX(id) to advance).
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(week_start_date, week_end_date, captured_at_utc, "
            " weekly_percent) "
            "VALUES (?, ?, ?, ?)",
            ("2026-05-18", "2026-05-25",
             NOW_UTC.isoformat().replace("+00:00", "Z"),
             42.0),
        )
        conn.commit()
        env_b = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        assert env_a is not env_b, (
            "memo MUST invalidate when MAX(weekly_usage_snapshots.id) "
            "advances (code-review Fix 3)"
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ===========================================================================
# #271 M4 — projects-envelope CURRENT-week incremental accumulator (spec §20).
#
# Drive `accumulate_projects_current_week` directly through the SAME closures
# `_assemble_projects_via_cache` builds, asserting the finalized
# `(buckets, week_total)` byte-matches a fresh `_aggregate_projects_week` on the
# same conn across cold / warm / delta / rollover / fold-order-gate ticks.
# ===========================================================================

WK_MON = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)  # Monday of NOW_UTC's week
WK_END = WK_MON + dt.timedelta(days=7)
PREV_MON = WK_MON - dt.timedelta(days=7)                    # a prior (closed) week


def _seed_conn(rows):
    """Build an in-memory conn (session_entries + session_files) from
    ``rows`` = list of ``(id, source_path, ts_iso, model, cost, session_id,
    project_path)``. Cost is threaded verbatim (mode=auto + cost_usd)."""
    conn = sqlite3.connect(":memory:")
    # #270 §8: the projects delta seek is keyed on `mutation_seq`, so the
    # in-memory table must carry the column + index; seed `mutation_seq == id`
    # (the pure-insert invariant).
    conn.execute(
        "CREATE TABLE session_entries (id INTEGER PRIMARY KEY, source_path TEXT, "
        "timestamp_utc TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_create_tokens INTEGER, cache_read_tokens INTEGER, cost_usd_raw REAL, "
        "mutation_seq INTEGER NOT NULL DEFAULT 0, mutation_min_ts TEXT)")
    conn.execute("CREATE INDEX idx_entries_timestamp ON session_entries(timestamp_utc)")
    conn.execute("CREATE INDEX idx_entries_mutation_seq "
                 "ON session_entries(mutation_seq, mutation_min_ts)")
    conn.execute(
        "CREATE TABLE session_files (path TEXT, session_id TEXT, project_path TEXT)")
    files = {}
    for (eid, sp, ts, model, cost, sid, pp) in rows:
        conn.execute(
            "INSERT INTO session_entries (id, source_path, timestamp_utc, model, "
            "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
            "cost_usd_raw, mutation_seq, mutation_min_ts) "
            "VALUES (?,?,?,?,0,0,0,0,?,?,?)", (eid, sp, ts, model, cost, eid, ts))
        files[sp] = (sp, sid, pp)
    for f in files.values():
        conn.execute(
            "INSERT INTO session_files (path, session_id, project_path) VALUES (?,?,?)", f)
    conn.commit()
    return conn


def _insert(conn, eid, sp, ts, model, cost, sid, pp, mutation_seq=None):
    # #270 §8: stamp `mutation_seq` (defaults to `id`, the pure-insert invariant)
    # so the seq-keyed warm delta discriminates the new row.
    seq = eid if mutation_seq is None else mutation_seq
    conn.execute(
        "INSERT INTO session_entries (id, source_path, timestamp_utc, model, "
        "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        "cost_usd_raw, mutation_seq, mutation_min_ts) "
        "VALUES (?,?,?,?,0,0,0,0,?,?,?)", (eid, sp, ts, model, cost, seq, ts))
    conn.execute(
        "INSERT OR REPLACE INTO session_files (path, session_id, project_path) "
        "VALUES (?,?,?)", (sp, sid, pp))
    conn.commit()


def _max_id(conn):
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM session_entries").fetchone()[0]


def _fresh(conn, cw_start=WK_MON, cw_end=WK_END):
    """From-scratch reference: the finalized public aggregate for the week."""
    return _cctally_dashboard._aggregate_projects_week(
        conn, week_start=cw_start, week_end=cw_end, resolver_cache={})


def _max_seq(conn):
    return conn.execute(
        "SELECT COALESCE(MAX(mutation_seq), 0) FROM session_entries").fetchone()[0]


def _acc_tick(conn, cur_max_id, cw_start=WK_MON, cw_end=WK_END, calls=None,
              cur_max_seq=None):
    """Drive ONE `accumulate_projects_current_week` tick with the SAME closures
    `_assemble_projects_via_cache` builds. ``calls`` (optional dict) records
    ``all_raw`` (cold-fold count) + ``delta_after_seqs`` (the after_seq watermark
    each warm fetch used) for the spy-based tests. ``cur_max_seq`` defaults to
    ``MAX(mutation_seq)`` off the conn (== ``cur_max_id`` under the seq==id
    fixtures)."""
    d = _cctally_dashboard
    resolver_cache = {}
    if cur_max_seq is None:
        cur_max_seq = _max_seq(conn)

    def _fetch_all_raw():
        if calls is not None:
            calls["all_raw"] = calls.get("all_raw", 0) + 1
        return d._aggregate_projects_week_raw(
            conn, week_start=cw_start, week_end=cw_end, resolver_cache=resolver_cache)

    def _fetch_delta_rows(after_seq):
        if calls is not None:
            calls.setdefault("delta_after_seqs", []).append(after_seq)
        out = []
        for r in d._projects_iter_session_entries(
                conn, since=cw_start, until=cw_end, after_seq=after_seq):
            if r[2] == "<synthetic>":
                continue
            ts = d.parse_iso_datetime(r[1], "session_entries.timestamp_utc")
            if d._projects_week_start_monday_utc(ts) != cw_start:
                continue
            out.append(r)
        out.sort(key=lambda r: (r[1], r[0]))
        return out

    return sc.accumulate_projects_current_week(
        week_key=sc.projects_env_week_key(cw_start),
        cur_max_id=cur_max_id,
        cur_max_seq=cur_max_seq,
        fetch_all_raw=_fetch_all_raw,
        fetch_delta_rows=_fetch_delta_rows,
        finalize=d._finalize_projects_mut,
        fold=lambda mut, row: d._fold_projects_entry(
            mut, row, resolver_cache=resolver_cache, week_start=cw_start))


def test_accumulator_cold_then_warm_matches_from_scratch():
    """Cold seed + an empty-delta warm tick both byte-match the from-scratch
    `_aggregate_projects_week` on the real multi-week fixture."""
    sc.reset_projects_env_current_state()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    mid = _max_id(conn)
    fresh, fresh_total = _fresh(conn)
    cold, cold_total = _acc_tick(conn, mid)
    assert cold == fresh and cold_total == fresh_total, "cold must == from-scratch"
    warm, warm_total = _acc_tick(conn, mid)
    assert warm == fresh and warm_total == fresh_total, "empty-delta warm must == from-scratch"


def test_accumulator_empty_delta_skips_fetch():
    """An empty-delta warm tick (cur_max_id unchanged) does NOT call the delta
    fetch NOR a full re-fold — the M4 win."""
    sc.reset_projects_env_current_state()
    conn = _open(FIXTURE_DIR / "multi-week.db")
    mid = _max_id(conn)
    calls = {}
    _acc_tick(conn, mid, calls=calls)          # cold
    assert calls["all_raw"] == 1
    _acc_tick(conn, mid, calls=calls)          # warm, same max_id/max_seq
    assert calls["all_raw"] == 1, "no full re-fold on empty-delta warm"
    assert "delta_after_seqs" not in calls, "no delta fetch when cur_max_seq unchanged"


def test_accumulator_warm_delta_dedups_resumed_session():
    """#271 §20 Codex-P1a: a warm delta adds a SECOND row for the SAME session
    (across two files) → sessions_count stays 1, proving the cold seed kept the
    real SET (a finalized `range(count)` seed would AttributeError on `.add`)."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
    ])
    cold, _ = _acc_tick(conn, _max_id(conn))
    bp = next(iter(cold))
    assert cold[bp].sessions_count == 1
    # Resume the SAME session s1 in a different file, later ts + higher id.
    _insert(conn, 2, "/j/b.jsonl", "2026-05-19T11:00:00Z", "claude-opus-4-8", 0.2, "s1", "/repos/foo")
    warm, warm_total = _acc_tick(conn, _max_id(conn))
    assert warm[bp].sessions_count == 1, "resumed session must dedup to 1"
    fresh, fresh_total = _fresh(conn)
    assert warm == fresh and warm_total == fresh_total


def test_accumulator_new_bucket_in_delta_first_seen():
    """A project's FIRST current-week activity arriving in a warm delta captures
    its first_order/first_id/first_key correctly (== from-scratch)."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
    ])
    _acc_tick(conn, _max_id(conn))  # cold: only foo
    # A brand-new project bar's first entry arrives in the delta.
    _insert(conn, 2, "/j/c.jsonl", "2026-05-19T12:00:00Z", "claude-opus-4-8", 0.3, "s2", "/repos/bar")
    warm, warm_total = _acc_tick(conn, _max_id(conn))
    fresh, fresh_total = _fresh(conn)
    assert warm == fresh and warm_total == fresh_total
    assert len(warm) == 2, "both foo and bar present"


def test_accumulator_fold_order_gate_forces_cold_refold():
    """#271 §20 fold-order gate: a late OLD-timestamp backfill that sorts <= tail
    forces a cold refold so the per-bucket cost + week_total left-folds stay in
    (ts, id) order == from-scratch. NON-VACUOUS: the costs (0.1, 0.2, 0.57) are
    non-associative — a naive append (no gate) folds in the wrong order and the
    float sum diverges (0.8700000000000001 vs 0.8699999999999999)."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
        (2, "/j/a.jsonl", "2026-05-19T12:00:00Z", "claude-opus-4-8", 0.2, "s1", "/repos/foo"),
    ])
    calls = {}
    _acc_tick(conn, _max_id(conn), calls=calls)  # cold: tail = (12:00, 2)
    assert calls["all_raw"] == 1
    # Late-ingest a row timestamped BETWEEN the two cold rows (sorts <= tail).
    _insert(conn, 3, "/j/a.jsonl", "2026-05-19T11:00:00Z", "claude-opus-4-8", 0.57, "s1", "/repos/foo")
    warm, warm_total = _acc_tick(conn, _max_id(conn), calls=calls)
    assert calls["all_raw"] == 2, "fold-order gate must trigger a cold refold"
    fresh, fresh_total = _fresh(conn)
    assert warm == fresh and warm_total == fresh_total, \
        "cold refold must reproduce the (ts, id)-ordered from-scratch fold"


def test_accumulator_quiet_current_week_uses_global_max_watermark():
    """#271 §20 Codex-P2a (re-keyed to seq, #270 §8): reconciled_max_seq is the
    tick's GLOBAL max mutation_seq, NOT the max seq FOLDED into the current week.
    A quiet current week (high seqs landing in a PAST week) must seek the delta by
    `mutation_seq > global_max` (returning nothing), never re-scan from the lower
    folded-max."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
        (2, "/j/a.jsonl", "2026-05-19T12:00:00Z", "claude-opus-4-8", 0.2, "s1", "/repos/foo"),
        # A PAST-week entry with the GLOBAL max id — folded-max (2) << global (3).
        (3, "/j/p.jsonl", "2026-05-11T10:00:00Z", "claude-opus-4-8", 0.9, "s9", "/repos/past"),
    ])
    calls = {}
    _acc_tick(conn, _max_id(conn), calls=calls)  # cold; cur_max_id = 3
    # A NEW past-week entry → global max advances to 4, current week unchanged.
    _insert(conn, 4, "/j/p.jsonl", "2026-05-11T11:00:00Z", "claude-opus-4-8", 0.5, "s9", "/repos/past")
    warm, warm_total = _acc_tick(conn, _max_id(conn), calls=calls)
    # The delta seek used the GLOBAL max seq (3) — the prior tick's cur_max_seq —
    # NOT the folded-max (2). (Both yield an empty in-window delta, but the
    # watermark VALUE is the non-vacuous assertion.)
    assert calls["delta_after_seqs"] == [3]
    assert calls["all_raw"] == 1, "quiet week must NOT cold-refold"
    fresh, fresh_total = _fresh(conn)
    assert warm == fresh and warm_total == fresh_total


def test_accumulator_monday_rollover_cold_refolds():
    """A tick at a NEW cw_start (Monday rollover) changes the label → cold refold
    of the new (here empty) current week."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
    ])
    calls = {}
    _acc_tick(conn, _max_id(conn), calls=calls)  # cold at WK_MON
    assert calls["all_raw"] == 1
    nxt = WK_MON + dt.timedelta(days=7)
    rolled, rolled_total = _acc_tick(
        conn, _max_id(conn), cw_start=nxt, cw_end=nxt + dt.timedelta(days=7), calls=calls)
    assert calls["all_raw"] == 2, "rollover (label change) must cold-refold"
    fresh_next, fresh_next_total = _fresh(conn, cw_start=nxt, cw_end=nxt + dt.timedelta(days=7))
    assert rolled == fresh_next and rolled_total == fresh_next_total  # empty next week


def test_accumulator_maxid_regression_cold_refolds():
    """cache.db rebuilt (cur_max_id < reconciled_max_id) → cold refold."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
        (2, "/j/a.jsonl", "2026-05-19T11:00:00Z", "claude-opus-4-8", 0.2, "s1", "/repos/foo"),
    ])
    calls = {}
    _acc_tick(conn, _max_id(conn), calls=calls)  # cold; reconciled = 2
    _acc_tick(conn, 1, calls=calls)              # cur_max_id regressed to 1
    assert calls["all_raw"] == 2, "max-id regression must cold-refold"


def test_accumulator_f7_no_torn_value():
    """F7: a finalized bucket captured from one tick is NOT mutated by a later
    appending tick (finalize builds fresh immutable buckets each tick)."""
    sc.reset_projects_env_current_state()
    conn = _seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.1, "s1", "/repos/foo"),
    ])
    cold, _ = _acc_tick(conn, _max_id(conn))
    bp = next(iter(cold))
    captured = cold[bp]
    captured_cost = captured.cost_usd
    # Append another row into the same bucket.
    _insert(conn, 2, "/j/a.jsonl", "2026-05-19T11:00:00Z", "claude-opus-4-8", 0.2, "s1", "/repos/foo")
    warm, _ = _acc_tick(conn, _max_id(conn))
    assert warm[bp].cost_usd != captured_cost, "the live tick advanced the running cost"
    assert captured.cost_usd == captured_cost, "the previously-captured bucket is untouched (F7)"
