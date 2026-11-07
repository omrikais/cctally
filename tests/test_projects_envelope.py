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
    assert all(
        finalized[bp].session_ids == frozenset(value["sessions"])
        for bp, value in mut.items()
    ), "the finalized week cache must retain identities for window dedup"
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


def test_usage_snapshot_read_is_bounded_before_python():
    """#627: old status-line ticks must not cross the SQLite/Python boundary.

    The live store has tens of thousands of snapshot rows but the Projects
    envelope can render at most ``weeks_back`` week percentages. Seed many
    rows far outside the rendered window and two credited captures in the
    current week. The DATE WINDOW is what bounds the read, and the later,
    lower credit remains authoritative.

    #750 S4 §2.2 removed the per-date `ROW_NUMBER()` reduction this test used
    to also assert. It kept exactly one row per calendar date, and both
    cycles of a credited week share that date, so the panel could never give
    the second cycle its own reading however the Python below resolved
    intervals. The rows now cross with their `captured_at_utc` and `id`, and
    the shared reducer applies the segment bounds and picks the latest. The
    row bound therefore becomes a window bound rather than a per-date one,
    which is what #627 was really about: the 500 seeded 2019 ticks are
    excluded by the date range, not by the ranking.
    """
    source = _open(FIXTURE_DIR / "multi-week.db")
    raw = sqlite3.connect(":memory:")
    source.backup(raw)
    source.close()

    old_rows = [
        (
            f"2020-01-01T00:{i % 60:02d}:00Z",
            "2019-12-30",
            "2020-01-06",
            "2019-12-30T00:00:00Z",
            "2020-01-06T00:00:00Z",
            float(i % 100),
        )
        for i in range(500)
    ]
    raw.executemany(
        "INSERT INTO weekly_usage_snapshots("
        " captured_at_utc, week_start_date, week_end_date, week_start_at,"
        " week_end_at, weekly_percent) VALUES (?,?,?,?,?,?)",
        old_rows,
    )
    current_start = _cctally_dashboard._projects_week_start_monday_utc(NOW_UTC)
    current_end = current_start + dt.timedelta(days=7)
    raw.executemany(
        "INSERT INTO weekly_usage_snapshots("
        " captured_at_utc, week_start_date, week_end_date, week_start_at,"
        " week_end_at, weekly_percent) VALUES (?,?,?,?,?,?)",
        [
            ("2026-05-19T12:10:00Z", current_start.date().isoformat(),
             current_end.date().isoformat(), current_start.isoformat(),
             current_end.isoformat(), 90.0),
            ("2026-05-19T12:20:00Z", current_start.date().isoformat(),
             current_end.date().isoformat(), current_start.isoformat(),
             current_end.isoformat(), 40.0),
        ],
    )
    raw.commit()

    class CountingCursor:
        def __init__(self, inner, owner):
            self.inner = inner
            self.owner = owner

        def fetchall(self):
            rows = self.inner.fetchall()
            self.owner.snapshot_rows = len(rows)
            return rows

        def __getattr__(self, name):
            return getattr(self.inner, name)

    class CountingConnection:
        def __init__(self, inner):
            self.inner = inner
            self.snapshot_rows = None

        def execute(self, sql, parameters=()):
            cursor = self.inner.execute(sql, parameters)
            if (
                "SELECT week_start_date, week_start_at, weekly_percent" in sql
                and "FROM weekly_usage_snapshots" in sql
            ):
                return CountingCursor(cursor, self)
            return cursor

        def __getattr__(self, name):
            return getattr(self.inner, name)

    conn = CountingConnection(raw)
    _cctally_dashboard._projects_reset_memo()
    env = _build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )

    assert conn.snapshot_rows is not None
    assert conn.snapshot_rows <= 7 * 12, (
        "the attribution query must stay bounded by the render window; the "
        "500 seeded 2019 ticks must not cross into Python, got "
        f"{conn.snapshot_rows}"
    )
    current = next(
        row for row in env["trend"]["weeks"]
        if row["week_start_date"] == current_start.date().isoformat()
    )
    assert current["total_pct"] == 40.0, (
        "latest-row semantics must preserve an in-place credit"
    )
    raw.close()


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
        "mutation_seq INTEGER NOT NULL DEFAULT 0, mutation_min_ts TEXT, "
        "speed TEXT, "
        # #195: NULLable split columns, last two ordinals (production adds
        # them via add_column_if_missing); the projects cost SELECT reads
        # cache_create_1h_tokens.
        "cache_create_1h_tokens INTEGER, cache_create_5m_tokens INTEGER)")
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


# --- #750 S4 §2.2 / §2.3: a credited week on the projects panel -------------

CREDITED_WEEK_START = dt.datetime(2026, 5, 18, 0, tzinfo=dt.timezone.utc)
# BEFORE `NOW_UTC`. `window_ending_at` renders intervals up to and including
# the one containing "now", so a cut in the future would leave the tail cycle
# legitimately unrendered and the test would prove nothing.
CREDITED_CUT = dt.datetime(2026, 5, 19, 9, tzinfo=dt.timezone.utc)
CREDITED_WEEK_END = dt.datetime(2026, 5, 25, 0, tzinfo=dt.timezone.utc)


def _credited_store():
    """`multi-week.db` with the current week credited in place.

    One `week_reset_events` row in the shape both appliers require of an
    in-place credit (`old_week_end_at == effective_reset_at_utc`), plus one
    observation on each side of the cut.
    """
    source = _open(FIXTURE_DIR / "multi-week.db")
    raw = sqlite3.connect(":memory:")
    source.backup(raw)
    source.close()
    raw.row_factory = sqlite3.Row
    raw.execute("DELETE FROM weekly_usage_snapshots")
    for captured, pct in (
        ("2026-05-19T08:00:00Z", 88.0),   # pre-credit peak
        ("2026-05-19T10:00:00Z", 11.0),   # post-credit
    ):
        raw.execute(
            "INSERT INTO weekly_usage_snapshots("
            " captured_at_utc, week_start_date, week_end_date, week_start_at,"
            " week_end_at, weekly_percent) VALUES (?,?,?,?,?,?)",
            (captured, CREDITED_WEEK_START.date().isoformat(),
             CREDITED_WEEK_END.date().isoformat(),
             CREDITED_WEEK_START.isoformat(), CREDITED_WEEK_END.isoformat(),
             pct),
        )
    cut = CREDITED_CUT.isoformat()
    raw.execute(
        "CREATE TABLE IF NOT EXISTS week_reset_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at_utc TEXT NOT NULL,"
        " old_week_end_at TEXT NOT NULL, new_week_end_at TEXT NOT NULL,"
        " effective_reset_at_utc TEXT NOT NULL, observed_pre_credit_pct REAL,"
        " account_key TEXT)"
    )
    raw.execute(
        "INSERT INTO week_reset_events(detected_at_utc, old_week_end_at,"
        " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct,"
        " account_key) VALUES (?,?,?,?,?,?)",
        (cut, cut, CREDITED_WEEK_END.isoformat(), cut, 88.0, "unattributed"),
    )
    raw.commit()
    return raw


def test_every_segment_of_a_credited_week_gets_its_own_percent_in_the_envelope():
    """The `ROW_NUMBER() PARTITION BY week_start_date` kept ONE row per
    calendar date, and both cycles of a credited week share that date, so the
    panel could never give the second cycle its own reading. Asserted against
    the panel's own envelope, not the CLI's."""
    conn = _credited_store()
    try:
        _cctally_dashboard._projects_reset_memo()
        env = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        by_instant = {
            w["week_start_at"]: w["total_pct"] for w in env["trend"]["weeks"]
        }
        head = CREDITED_WEEK_START.isoformat().replace("+00:00", "Z")
        tail = CREDITED_CUT.isoformat().replace("+00:00", "Z")
        assert head in by_instant and tail in by_instant, (
            "the credited week must contribute BOTH cycles as intervals; "
            f"got {sorted(by_instant)}"
        )
        # One percentage per CYCLE. Under the ranked query only one row per
        # calendar date survived, so the second cycle could carry nothing.
        assert by_instant[head] == 88.0, by_instant
        assert by_instant[tail] == 11.0, by_instant
    finally:
        conn.close()


def test_projects_trend_weeks_carry_week_start_at():
    """§3.5. `week_start_date` is the shared join key, so two cycles of a
    credited week carry one value for it and it cannot be a React key."""
    conn = _open(FIXTURE_DIR / "multi-week.db")
    try:
        _cctally_dashboard._projects_reset_memo()
        env = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        weeks = env["trend"]["weeks"]
        assert weeks, env["trend"]
        for w in weeks:
            assert "week_start_at" in w, w
            assert w["week_start_at"].endswith("Z"), w["week_start_at"]
        instants = [w["week_start_at"] for w in weeks]
        assert len(set(instants)) == len(instants), instants
    finally:
        conn.close()


def test_total_pct_agrees_with_the_weekly_surfaces_across_a_credited_boundary():
    """#743 item 2: the earlier segment carried the LATER segment's percent
    and the later carried null. The panel must read what the shared reducer
    reads on the same store."""
    conn = _credited_store()
    try:
        subweeks = _NS["_compute_subscription_weeks"](
            conn, CREDITED_WEEK_START - dt.timedelta(days=1),
            NOW_UTC, account_key=None,
        )
        by_segment = _NS["latest_usage_by_segment"](
            conn, subweeks, account_key=None)
        _cctally_dashboard._projects_reset_memo()
        env = _build_projects_envelope(
            conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
        )
        panel = {
            w["week_start_at"]: w["total_pct"] for w in env["trend"]["weeks"]
        }
        for segment_key, pct in by_segment.items():
            iso_z = segment_key.replace("+00:00", "Z")
            if iso_z in panel and pct is not None:
                assert panel[iso_z] == pct, (
                    f"panel and reducer disagree at {iso_z}: "
                    f"{panel[iso_z]} vs {pct}"
                )
    finally:
        conn.close()


def test_start_for_date_returns_none_when_a_date_resolves_to_two_segments():
    """#737. The later interval used to win. A legacy row carrying only a
    date is genuinely ambiguous on a credited week, and
    `tests/test_620_projects_unresolvable_week.py` already requires an
    unresolvable week to contribute nothing rather than be attributed to a
    guess."""
    same_day_cut = CREDITED_WEEK_START + dt.timedelta(hours=12)
    grid = _cctally_dashboard._ProjectsWeekGrid([
        (CREDITED_WEEK_START, same_day_cut),
        (same_day_cut, CREDITED_WEEK_END),
    ])
    assert grid.start_for_date(CREDITED_WEEK_START.date()) is None, (
        "both cycles start on 2026-05-18, so the date resolves to neither"
    )
    unambiguous = _cctally_dashboard._ProjectsWeekGrid([
        (CREDITED_WEEK_START, CREDITED_WEEK_END),
    ])
    assert unambiguous.start_for_date(
        CREDITED_WEEK_START.date()) == CREDITED_WEEK_START, (
        "a date owned by exactly one interval still resolves"
    )


# ===========================================================================
# #1059 — warm project regrouping on filesystem changes.
#
# A warm dashboard retains resolved project identities in its closed-week
# cache, its current-week accumulator and its whole-envelope memo. Each case
# warms those through the real cached builder, changes ONLY the filesystem
# (no cache is cleared by hand), advances the injected monotonic clock past
# the sweep interval and drives the real orchestration. The decisive failure
# is a stale or split grouping: the published rows still name the old bucket.
# ===========================================================================

from conftest import (  # noqa: E402
    git_init, make_regroup_tree, recreate_worktree, remove_worktree,
)

_W_CUR = WK_MON
_W_1 = WK_MON - dt.timedelta(days=7)      # affected closed week
_W_2 = WK_MON - dt.timedelta(days=14)     # affected closed week
_W_CTRL = WK_MON - dt.timedelta(days=21)  # control: otherrepo only
_ARM = 1000.0


def _regroup_ns():
    ns = load_script()
    return (ns, ns["_load_sibling"]("_cctally_dashboard"),
            ns["_load_sibling"]("_cctally_tui"))


def _ts(week, hours):
    return (week + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _regroup_rows(tree, spellings):
    """Entries for `spellings` in two closed weeks and the current week, plus
    `otherrepo` in every week including the control week. Costs are distinct
    so a misplaced entry shows in the totals."""
    rows = []
    eid = 0

    def add(week, hours, sp, cost, sid, pp):
        nonlocal eid
        eid += 1
        rows.append((eid, f"/j/{sid}.jsonl", _ts(week, hours),
                     "claude-opus-4-8", cost, sid, pp))

    for w_index, week in enumerate((_W_CTRL, _W_2, _W_1, _W_CUR)):
        add(week, 1, None, 0.5 + w_index, f"other{w_index}", str(tree.other))
        if week is _W_CTRL:
            continue
        add(week, 2, None, 0.25 + w_index, f"main{w_index}", str(tree.main))
        for s_index, sp in enumerate(spellings):
            add(week, 3 + s_index, None, 1.0 + w_index + s_index / 8,
                f"s{w_index}_{s_index}", str(sp))
    return rows


def _rows_by_bucket(env):
    return {row["bucket_path"]: row for row in env["current_week"]["rows"]}


def _trend_buckets(env):
    return {p["bucket_path"]: p for p in env["trend"]["projects"]}


def _seed_regroup_usage(conn):
    """Date-only weekly readings for every regroup week, so the envelope's
    quota denominator (`total_pct`) and every row's `attributed_pct` /
    `weekly_pct` are populated rather than all `None` (spec 9 R2)."""
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots (id INTEGER PRIMARY KEY, "
        " captured_at_utc TEXT NOT NULL, week_start_date TEXT, "
        " week_end_date TEXT, week_start_at TEXT, week_end_at TEXT, "
        " weekly_percent REAL)")
    for index, week in enumerate((_W_CTRL, _W_2, _W_1, _W_CUR)):
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (captured_at_utc, "
            " week_start_date, week_end_date, weekly_percent) "
            "VALUES (?, ?, ?, ?)",
            (_ts(week, 12), week.date().isoformat(),
             (week + dt.timedelta(days=7)).date().isoformat(),
             11.0 + 7 * index))
    conn.commit()


def _week_sums(env):
    """Per-week sums across every project row: a regroup that moves rows
    between projects must keep each of them equal."""
    weeks = len(env["trend"]["weeks"])
    cost = [0.0] * weeks
    pct = [0.0] * weeks
    for project in env["trend"]["projects"]:
        for i in range(weeks):
            cost[i] += project["weekly_cost"][i]
            pct[i] += project["weekly_pct"][i] or 0.0
    return cost, pct


def _assert_totals_equal(pre, post, *, appended=0.0):
    """Spec 9 R2: a filesystem-only transition keeps every cost, quota and
    denominator value; an append adds exactly the appended entry's cost (it
    moves no weekly reading, so quota fields stay exact)."""
    cur_index = len(pre["trend"]["weeks"]) - 1
    assert post["current_week"]["total_cost_usd"] == pytest.approx(
        pre["current_week"]["total_cost_usd"] + appended, abs=1e-9)
    for i, (a, b) in enumerate(zip(pre["trend"]["weeks"],
                                   post["trend"]["weeks"])):
        extra = appended if i == cur_index else 0.0
        assert b["total_cost_usd"] == pytest.approx(
            a["total_cost_usd"] + extra, abs=1e-9)
        assert b["week_start_at"] == a["week_start_at"]
        assert b["total_pct"] == a["total_pct"], "quota denominator moved"
    pre_cost, pre_pct = _week_sums(pre)
    post_cost, post_pct = _week_sums(post)
    for i in range(len(pre_cost)):
        extra = appended if i == cur_index else 0.0
        assert post_cost[i] == pytest.approx(pre_cost[i] + extra, abs=1e-9)
        assert post_pct[i] == pytest.approx(pre_pct[i], abs=1e-9)
    assert sum(r["attributed_pct"] or 0.0
               for r in post["current_week"]["rows"]) == pytest.approx(
        sum(r["attributed_pct"] or 0.0
            for r in pre["current_week"]["rows"]), abs=1e-9)
    for env in (pre, post):
        assert sum(r["cost_usd"] for r in env["current_week"]["rows"]) == (
            pytest.approx(env["current_week"]["total_cost_usd"], abs=1e-9))
        for week, week_cost in zip(env["trend"]["weeks"], _week_sums(env)[0]):
            assert week_cost == pytest.approx(week["total_cost_usd"], abs=1e-9)


def _transition(case, tree):
    if case in ("removed_worktree", "append"):
        remove_worktree(tree)
    elif case == "git_init_parent":
        git_init(tree.plain)
    elif case == "metadata_flip":
        git_init(tree.solo)
    elif case == "moved_repo":
        tree.movable.rename(tree.root / "moved")
    else:
        raise AssertionError(case)


_CASES = {
    # case: (spellings, {spelling: bucket before}, {spelling: bucket after})
    "removed_worktree": (
        lambda t: [t.feature, t.feature / "src"],
        lambda t: {t.feature: t.feature, t.feature / "src": t.feature},
        lambda t: {t.feature: t.main, t.feature / "src": t.main}),
    "append": (
        lambda t: [t.feature],
        lambda t: {t.feature: t.feature},
        lambda t: {t.feature: t.main}),
    "git_init_parent": (
        lambda t: [t.plain_sub],
        lambda t: {t.plain_sub: t.plain_sub},
        lambda t: {t.plain_sub: t.plain}),
    "metadata_flip": (
        lambda t: [t.alias],
        lambda t: {t.alias: t.solo},
        lambda t: {t.alias: t.solo}),
    "moved_repo": (
        lambda t: [t.movable, t.movable_sub],
        lambda t: {t.movable: t.movable, t.movable_sub: t.movable},
        lambda t: {t.movable: t.movable, t.movable_sub: t.movable_sub}),
}


def _cold_oracle(dash, conn):
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()
    return dash._build_projects_envelope(
        conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_warm_envelope_regroups_after_a_filesystem_change(case, tmp_path):
    """Detects a warm Projects envelope that keeps the grouping it computed
    before a worktree removal, a `git init`, a metadata flip or a move, while
    a restart would regroup — and a regroup that rebuilds unaffected weeks or
    changes any total."""
    import copy
    _ns, dash, tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    spellings_of, before_of, after_of = _CASES[case]
    spellings = spellings_of(tree)
    conn = _seed_conn(_regroup_rows(tree, spellings))
    _seed_regroup_usage(conn)
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()
    clock = {"t": _ARM}

    def build():
        tui._tui_revalidate_project_identities(now_monotonic=clock["t"])
        return dash._build_projects_envelope(
            conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)

    env1 = build()
    clock["t"] += 10
    env2 = build()
    assert env2 is env1, "precondition: the warm memo serves the second build"
    for sp, bucket in before_of(tree).items():
        assert any(bp == str(bucket) for (bp, _w) in sc._PROJECTS_ENV_WEEK_CACHE), (
            f"precondition: the warm cache holds {sp} under {bucket}")
    if case == "metadata_flip":
        assert _rows_by_bucket(env2)[str(tree.solo)]["key"] == "alias"
    assert all(w["total_pct"] is not None for w in env2["trend"]["weeks"]), (
        "non-vacuity: every week carries a quota denominator")
    published = copy.deepcopy(env2)

    _transition(case, tree)
    appended = 0.0
    if case == "append":
        appended = 0.375
        _insert(conn, 999, "/j/new.jsonl", _ts(_W_CUR, 30), "claude-opus-4-8",
                appended, "snew", str(tree.feature))
    work = sc.project_work_stats()
    batches = sc.project_identity_stats()["flipBatches"]
    clock["t"] = _ARM + 30
    before_deadline = build()
    assert sc.project_identity_stats()["flipBatches"] == batches
    if case != "append":
        assert before_deadline is env2, "filesystem-only: nothing republished"
        assert sc.project_work_stats() == work

    clock["t"] = _ARM + 61
    work = sc.project_work_stats()
    env3 = build()
    rows = _rows_by_bucket(env3)
    trend = _trend_buckets(env3)
    for sp, bucket in after_of(tree).items():
        assert str(bucket) in rows and str(bucket) in trend, (sp, bucket)
    stale = {str(b) for b in before_of(tree).values()} - {
        str(b) for b in after_of(tree).values()}
    for bucket in stale:
        assert bucket not in rows and bucket not in trend, (
            f"stale grouping: {bucket} is still its own project")
    if case == "removed_worktree":
        assert rows[str(tree.main)]["sessions_count"] == 3
    if case == "metadata_flip":
        assert rows[str(tree.solo)]["key"] == "solo"
    _assert_totals_equal(published, env3, appended=appended)
    assert sc.project_identity_stats()["flipBatches"] == batches + 1
    computes = (sc.project_work_stats()["closedWeekComputes"]
                - work["closedWeekComputes"])
    assert computes == 2, "exactly the two affected closed weeks recompute"
    assert sc.projects_env_week_get(
        sc.projects_env_week_key(_W_CTRL)) is not None, "control week stays a hit"
    assert env2 == published, "an earlier published envelope is never mutated"

    warm = copy.deepcopy(env3)
    assert _cold_oracle(dash, conn) == warm


def test_warm_envelope_intermediate_identity_evicts_retained_current_week(
    tmp_path,
):
    """A→B→A: a current-week row folded under the repository while the
    worktree was gone stays split under the repository after the worktree
    returns unless the sweep invalidates every identity observed since the
    last sweep, not only the first one."""
    import copy
    _ns, dash, tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    conn = _seed_conn(_regroup_rows(tree, [tree.feature]))
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()
    clock = {"t": _ARM}

    def build():
        tui._tui_revalidate_project_identities(now_monotonic=clock["t"])
        return dash._build_projects_envelope(
            conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)

    build()
    remove_worktree(tree)
    _insert(conn, 999, "/j/new.jsonl", _ts(_W_CUR, 30), "claude-opus-4-8",
            0.375, "snew", str(tree.feature))
    clock["t"] += 10
    split = build()
    assert str(tree.main) in _rows_by_bucket(split)
    recreate_worktree(tree)
    clock["t"] = _ARM + 61
    env = build()
    assert str(tree.feature) in _rows_by_bucket(env)
    assert _rows_by_bucket(env)[str(tree.feature)]["sessions_count"] == 2
    warm = copy.deepcopy(env)
    assert _cold_oracle(dash, conn) == warm


def test_project_share_follows_the_regrouped_envelope(tmp_path):
    """A share rendered after the regroup must carry the regrouped rows for
    every window, the closed weeks of the 4/8/12-week windows included."""
    import types
    ns, dash, tui = _regroup_ns()
    share = ns["_load_sibling"]("_cctally_dashboard_share")
    tree = make_regroup_tree(tmp_path / "tree")
    conn = _seed_conn(_regroup_rows(tree, [tree.feature]))
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()
    tui._tui_revalidate_project_identities(now_monotonic=_ARM)
    dash._build_projects_envelope(
        conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)
    remove_worktree(tree)
    tui._tui_revalidate_project_identities(now_monotonic=_ARM + 61)
    env = dash._build_projects_envelope(
        conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)
    snap = types.SimpleNamespace(projects_envelope=env)
    for weeks in (1, 4, 8, 12):
        data = share._build_projects_share_panel_data(
            {"windowWeeks": weeks}, snap)
        buckets = {row["bucket_path"] for row in data["rows"]}
        assert str(tree.main) in buckets, weeks
        assert str(tree.feature) not in buckets, weeks


@pytest.mark.parametrize("failing_step", [
    "evict_projects_env_weeks_for_buckets",
    "force_codex_accounting_paths",
    "mark_project_regroup_pending",
])
def test_flip_survives_a_failed_regroup_step(failing_step, tmp_path,
                                             monkeypatch):
    """A flip step that raises after the sweep committed its fresh
    observations would leave no stale identity for any later sweep to see,
    so the stale grouping would last until the process restarted. The next
    due sweep (not an earlier build) must report the flip again, record `G`
    and regroup the envelope."""
    _ns, dash, tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    conn = _seed_conn(_regroup_rows(tree, [tree.feature]))
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()
    tui._tui_revalidate_project_identities(now_monotonic=_ARM)
    dash._build_projects_envelope(
        conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)
    remove_worktree(tree)

    real = getattr(sc, failing_step)
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected regroup step failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(sc, failing_step, fail_once)
    batches = sc.project_identity_stats()["flipBatches"]
    with pytest.raises(RuntimeError, match="injected regroup step failure"):
        tui._tui_revalidate_project_identities(now_monotonic=_ARM + 61)
    assert sc.project_identity_stats()["flipBatches"] == batches + 1
    assert sc.project_regroup_pending() is None
    generation = sc.current_generation()
    tui._tui_revalidate_project_identities(now_monotonic=_ARM + 70)
    assert sc.current_generation() == generation, (
        "the retry waits for the next interval, not the next build")
    tui._tui_revalidate_project_identities(now_monotonic=_ARM + 122)
    assert sc.project_identity_stats()["flipBatches"] == batches + 2, (
        "lost flip: the next due sweep no longer sees the stale identity")
    assert sc.current_generation() == generation + 1
    assert sc.project_regroup_pending() == sc.current_generation()
    env = dash._build_projects_envelope(
        conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)
    assert str(tree.feature) not in _rows_by_bucket(env)
    assert str(tree.feature) not in _trend_buckets(env)
    assert str(tree.main) in _rows_by_bucket(env)
    conn.close()


def test_owner_paths_register_their_spellings(tmp_path):
    """A retained owner-side resolution through a plain dict escapes the sweep
    silently: the cached assembly and the observed full-window walk must both
    feed the registry."""
    _ns, dash, _tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    conn = _seed_conn(_regroup_rows(tree, [tree.feature]))
    for cached in (True, False):
        sc.reset_projects_env_state()
        dash._projects_reset_memo()
        sc.reset_project_identity_state()
        kwargs = ({"use_projects_env_cache": True} if cached else
                  {"project_resolver_cache": sc.observing_project_memo()})
        dash._build_projects_envelope(
            conn, now_utc=NOW_UTC, weeks_back=12, **kwargs)
        assert set(sc._PROJECT_IDENTITY_STATE["observed"]) >= {
            str(tree.feature), str(tree.main), str(tree.other)}, cached


@pytest.mark.parametrize("entry", ["envelope", "drill"])
def test_request_thread_fallback_bypasses_the_envelope_memo(entry, tmp_path):
    """The drill fallback runs on a request thread; reading or writing the
    process-wide memo there would publish a request-time grouping into the
    owner's state. `drill` goes through `_project_detail_for_window` with no
    snapshot envelope, so it fails if that call site stops passing
    `use_envelope_memo=False`."""
    _ns, dash, _tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    conn = _seed_conn(_regroup_rows(tree, [tree.feature]))
    dash._projects_reset_memo()
    sentinel = {"current_week": {"rows": []}, "trend": {"projects": []}}
    dash._PROJECTS_ENV_MEMO["key"] = ("stale",)
    dash._PROJECTS_ENV_MEMO["value"] = sentinel
    if entry == "envelope":
        env = dash._build_projects_envelope(
            conn, now_utc=NOW_UTC, weeks_back=12, use_envelope_memo=False)
        assert env is not sentinel and env["current_week"]["rows"]
    else:
        detail = dash._project_detail_for_window(
            conn, project_key="feature", weeks_back=12, now_utc=NOW_UTC,
            projects_envelope=None)
        assert detail is not None, "non-vacuity: the drill resolved the key"
    assert dash._PROJECTS_ENV_MEMO["value"] is sentinel
    assert dash._PROJECTS_ENV_MEMO["key"] == ("stale",)
    dash._projects_reset_memo()
    conn.close()


@pytest.mark.parametrize("claude_first", [True, False])
def test_alias_labels_stay_per_consumer_in_both_orders(claude_first, tmp_path):
    """A shared resolver memo would hand the Codex load the alias spelling the
    Claude envelope saw first (or the reverse), renaming a project on an
    unchanged filesystem (P2a). Each consumer keeps its own first spelling,
    and the unchanged sweep sees no flip."""
    ns, dash, tui = _regroup_ns()
    tree = make_regroup_tree(tmp_path / "tree")
    second = tree.root / "links" / "second"
    second.symlink_to(tree.solo)
    claude_order = [tree.alias, second]
    codex_order = list(reversed(claude_order))
    conn = _seed_conn(_regroup_rows(tree, claude_order))
    sc.reset_projects_env_state()
    dash._projects_reset_memo()
    sc.reset_project_identity_state()

    codex = sqlite3.connect(tmp_path / "cache.db")
    ns["_cctally_db"]._apply_cache_schema(codex)
    for index, cwd in enumerate(codex_order):
        codex.execute(
            "INSERT INTO codex_conversation_threads (conversation_key, "
            " source_root_key, native_thread_id, root_thread_id, source_path, "
            " cwd) VALUES (?, 'root-a', ?, ?, ?, ?)",
            (f"c{index}", f"n{index}", f"n{index}", f"/t/{index}.jsonl",
             str(cwd)))
        codex.execute(
            "INSERT INTO codex_session_entries (source_path, source_root_key, "
            " line_offset, timestamp_utc, session_id, model, input_tokens, "
            " cached_input_tokens, output_tokens, reasoning_output_tokens, "
            " total_tokens, conversation_key) "
            "VALUES (?, 'root-a', 1, ?, ?, 'gpt-5', 10, 2, 3, 1, 13, ?)",
            (f"/r/{index}.jsonl", _ts(_W_CUR, 1 + index), f"s{index}",
             f"c{index}"))
    codex.commit()
    analytics = ns["_load_sibling"]("_cctally_source_analytics")

    def codex_labels():
        entries = analytics.load_qualified_codex_entries(
            _W_CUR, _W_CUR + dt.timedelta(days=7), speed="standard",
            sync=False, cache_conn=codex,
            project_resolver_cache=sc.observing_project_memo())
        assert len({e.project_key for e in entries}) == 1
        return {e.project_label for e in entries}

    def claude_label():
        env = dash._build_projects_envelope(
            conn, now_utc=NOW_UTC, weeks_back=12, use_projects_env_cache=True)
        return _rows_by_bucket(env)[str(tree.solo)]["key"]

    try:
        tui._tui_revalidate_project_identities(now_monotonic=_ARM)
        if claude_first:
            claude, labels = claude_label(), codex_labels()
        else:
            labels, claude = codex_labels(), claude_label()
        assert claude == "alias"
        assert labels == {"second"}
        before = sc.project_identity_stats()["flips"]
        tui._tui_revalidate_project_identities(now_monotonic=_ARM + 61)
        assert sc.project_identity_stats()["flips"] == before
        assert sc.project_regroup_pending() is None
    finally:
        codex.close()
        conn.close()
