"""#857 Task B — the warm stats-digest memo must never outlive a stats commit.

The memo in ``_lib_dashboard_sources._stats_relations_digest`` spares the
dashboard's idle tick the whole-relation scans behind ``codex_stats_digest``
and ``claude_stats_digest``. Its first key was the journal cursor, on the claim
that the cursor advances in the same transaction as every materialized row.
That claim is false: ``_run_cycle`` runs the Codex projection leg and the
budget-config reconcile on a cycle that consumed no journal bytes, and then
rewrites the cursor to the same high-water mark. The two ingest-backed tests
below reproduce exactly that, through the real ``run_stats_ingest`` path.

The remaining tests pin the cold-path conditions of the replacement key and the
property the memo exists for: repeated idle cycles that write nothing keep
reusing it.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sqlite3
import time

import pytest

from conftest import load_script, redirect_paths

pytestmark = pytest.mark.usefixtures("isolated_home")

UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"
FIXED = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
_CODEX_MARKER = "FROM QUOTA_PROJECTION_STATE"
_CLAUDE_MARKER = "FROM PERCENT_MILESTONES"


@pytest.fixture
def kernel(monkeypatch):
    """The pure kernel, with an empty memo and a clock past the settle window.

    ``load_script`` never reloads a ``_lib_*`` kernel, so the module-level memo
    would otherwise carry entries across tests. The clock is moved an hour
    ahead so a file this test has just written already counts as settled; the
    settle window has its own test below, which restores the real clock.
    """
    module = importlib.import_module("_lib_dashboard_sources")
    monkeypatch.setattr(module, "_STATS_RELATIONS_DIGEST_MEMO", {})
    monkeypatch.setattr(
        module, "_stats_memo_now", lambda: time.time() + 3600.0, raising=False,
    )
    return module


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")
    quota = importlib.import_module("_cctally_quota")
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, quota, jr, jl


def _claude_obs(jl, *, at, weekly_percent):
    return jl.make_obs(
        at=at, src="statusline", provider="claude",
        payload={"week_start_date": "2026-07-13",
                 "weekly_percent": weekly_percent,
                 "source": "statusline", "payload_json": "{}"},
    )


def _ingested_store(ns, jr, jl, *, rebuild=False):
    """A stats store whose journal cursor exists, as every live install's does.

    ``rebuild`` publishes a validated selector generation first, so each later
    cycle also runs the selector-delta write a production cycle runs.
    """
    ns["open_db"]().close()
    jr.append_record(_claude_obs(jl, at="2026-07-15T11:00:00Z", weekly_percent=1.0))
    jr.run_stats_ingest(mode="authoritative")
    if rebuild:
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
        jr.append_record(
            _claude_obs(jl, at="2026-07-15T11:05:00Z", weekly_percent=2.0))
        jr.run_stats_ingest(mode="authoritative")


def _iso(hour, minute=0):
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


def _seed_cache_quota(ns, *, root):
    """Write Codex quota observations to cache.db only — no journal line."""
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?)",
            (root, f"/codex/{root}", _iso(10), _iso(10)),
        )
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc,
                plan_type, individual_limit_json, reached_type)
               VALUES ('codex', ?, ?, ?, ?, 'primary', 'limit-primary',
                       'native-primary', 'Primary', 300, ?, ?, 'pro', NULL, NULL)""",
            [
                (root, f"/codex/{root}/rollout.jsonl", 10, _iso(11, 0), 20.0, RESET),
                (root, f"/codex/{root}/rollout.jsonl", 20, _iso(11, 30), 40.0, RESET),
            ],
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def _reads(statements, marker):
    return sum(marker in statement.upper() for statement in statements)


def _cursor(conn):
    return tuple(conn.execute(
        "SELECT segment, offset, applied_segment, applied_offset "
        "FROM journal_cursor WHERE id=1"
    ).fetchone())


def _warm(ns, digest, marker):
    """Compute a digest twice and prove the second call was served warm."""
    conn = ns["open_db"]()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        value = digest(conn)
        assert _reads(statements, marker) == 1
        assert digest(conn) == value
        assert _reads(statements, marker) == 1, (
            "the memo must be warm before the write under test"
        )
        return value, _cursor(conn)
    finally:
        conn.set_trace_callback(None)
        conn.close()


# --------------------------------------------------------------------------
# The reviewer's scenarios: a real cycle that consumed no journal bytes.
# --------------------------------------------------------------------------

def test_a_codex_projection_without_new_journal_bytes_moves_the_digest(
    tmp_path, monkeypatch, kernel,
):
    ns, quota, jr, jl = _load(tmp_path, monkeypatch)
    _ingested_store(ns, jr, jl)
    warm, cursor = _warm(ns, kernel.codex_stats_digest, _CODEX_MARKER)

    # The observations reach cache.db directly, so the projection pass below is
    # a `run_stats_ingest(codex_apply=…)` cycle over an unchanged journal.
    _seed_cache_quota(ns, root="root-a")
    quota.reconcile_codex_quota_projection(now=FIXED)

    conn = ns["open_db"]()
    try:
        assert _cursor(conn) == cursor, "the scenario needs a cursorless write"
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_projection_state"
        ).fetchone()[0] == 1
        cold = kernel._stats_relations_digest(
            conn, kernel._CODEX_STATS_DIGEST_RELATIONS)
        assert cold != warm, "the projection write must move the relations"
        assert kernel.codex_stats_digest(conn) == cold
    finally:
        conn.close()


def test_a_budget_config_reconcile_without_new_journal_bytes_moves_the_digests(
    tmp_path, monkeypatch, kernel,
):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _ingested_store(ns, jr, jl)
    budget = {
        "weekly_usd": 50.0, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [50, 100],
        "codex": {
            "amount_usd": 200.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
        },
    }
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.write_text(
        json.dumps({"display": {"tz": "utc"}, "budget": budget}) + "\n")
    monkeypatch.setitem(
        ns, "_sum_cost_for_range", lambda _start, _end, **_kw: 100.0)
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range", lambda _start, _end, **_kw: 200.0)
    warm_codex, cursor = _warm(ns, kernel.codex_stats_digest, _CODEX_MARKER)
    warm_claude, _ = _warm(ns, kernel.claude_stats_digest, _CLAUDE_MARKER)

    jr.reconcile_budget_config(budget, axes={"budget", "codex_budget"})

    conn = ns["open_db"]()
    try:
        assert _cursor(conn) == cursor, "the scenario needs a cursorless write"
        latched = conn.execute(
            "SELECT vendor, COUNT(*) FROM budget_milestones "
            "WHERE alerted_at IS NOT NULL GROUP BY vendor ORDER BY vendor"
        ).fetchall()
        assert [tuple(row) for row in latched] == [("claude", 2), ("codex", 2)]
        cold_codex = kernel._stats_relations_digest(
            conn, kernel._CODEX_STATS_DIGEST_RELATIONS)
        cold_claude = kernel._stats_relations_digest(
            conn, kernel._CLAUDE_STATS_DIGEST_RELATIONS)
        assert cold_codex != warm_codex and cold_claude != warm_claude
        assert kernel.codex_stats_digest(conn) == cold_codex
        assert kernel.claude_stats_digest(conn) == cold_claude
    finally:
        conn.close()


def test_a_stats_commit_during_a_source_build_is_seen_as_a_moved_generation(
    tmp_path, monkeypatch, kernel,
):
    """`stats_generation_moved` compares a pre-build and a post-build digest.

    Both come through the memo, so the memo key must move with a commit that
    lands between them even when that commit leaves the cursor alone.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _ingested_store(ns, jr, jl)
    tui = ns["_cctally_tui"]
    stats = ns["open_db"]()
    try:
        prior = tui._tui_build_source_bundle(
            projects_envelope={}, stats_conn=stats, now_utc=FIXED,
            display_tz_name="UTC", codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0,
        )
        compose = tui.compose_all_state
        commits: list[str] = []

        def compose_after_a_concurrent_commit(claude, codex):
            if not commits:
                writer = ns["open_db"]()
                try:
                    writer.execute(
                        "INSERT INTO quota_projection_state "
                        "(source_root_key, physical_signature, generation, "
                        " completed_at_utc) VALUES "
                        "('root-mid-build', 'signature', 'generation', ?)",
                        (FIXED.isoformat(),),
                    )
                    writer.commit()
                finally:
                    writer.close()
                commits.append("root-mid-build")
            return compose(claude, codex)

        monkeypatch.setattr(
            tui, "compose_all_state", compose_after_a_concurrent_commit)
        rebuilt = tui._tui_build_source_bundle(
            projects_envelope={}, stats_conn=stats,
            now_utc=FIXED + dt.timedelta(minutes=1),
            display_tz_name="UTC", codex_ingest_contended=False,
            claude_cost_usd=0.0, claude_total_tokens=0, prior_bundle=prior,
        )
        assert commits == ["root-mid-build"]
        assert rebuilt is prior, (
            "a stats commit between the two digest reads must reject the build"
        )
    finally:
        stats.close()


# --------------------------------------------------------------------------
# The key's cold-path conditions, over a small file-backed store.
# --------------------------------------------------------------------------

def _file_store(path, *, wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE quota_projection_state (
            id INTEGER PRIMARY KEY, source_root_key TEXT, physical_signature TEXT
        );
        CREATE TABLE journal_cursor (
            id INTEGER PRIMARY KEY, segment TEXT, offset INTEGER,
            applied_segment TEXT, applied_offset INTEGER
        );
        INSERT INTO quota_projection_state VALUES (1, 'root-a', 'physical-a');
        INSERT INTO journal_cursor VALUES
            (1, 'observations-2026-07.jsonl', 100,
             'observations-2026-07.jsonl', 100);
        """
    )
    conn.commit()
    return conn


def _traced(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return statements


def _write_elsewhere(path, sql, params=(), *, advance_stamp=False):
    before = os.stat(path)
    writer = sqlite3.connect(path)
    try:
        writer.execute(sql, params)
        writer.commit()
    finally:
        writer.close()
    if advance_stamp:
        _advance_stamp_past(path, before)


def _advance_stamp_past(path, before):
    """Give a commit a newer mtime than ``before`` when the filesystem did not.

    The ``kernel`` fixture fakes the settle clock, and the settle window is
    what guarantees production a strictly newer stamp on a coarse-timestamp
    filesystem. These tests commit milliseconds after the memo stored its
    entry, so there the commit could share the prior stamp. Moving it only in
    that case keeps a nanosecond filesystem proving the real commit moved the
    key.
    """
    after = os.stat(path)
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
            before.st_size, before.st_mtime_ns, before.st_ctime_ns):
        os.utime(path, ns=(after.st_atime_ns, after.st_mtime_ns + 1_000_000_000))


def test_a_warm_store_serves_repeat_reads_without_scanning(tmp_path, kernel):
    conn = _file_store(tmp_path / "stats.db")
    statements = _traced(conn)
    try:
        first = kernel.codex_stats_digest(conn)
        assert kernel.codex_stats_digest(conn) == first
        assert kernel.codex_stats_digest(conn) == first
        assert _reads(statements, _CODEX_MARKER) == 1
    finally:
        conn.close()


def test_a_write_that_leaves_the_cursor_unchanged_invalidates_the_memo(
    tmp_path, kernel,
):
    path = tmp_path / "stats.db"
    conn = _file_store(path)
    statements = _traced(conn)
    try:
        before = kernel.codex_stats_digest(conn)
        _write_elsewhere(
            path,
            "UPDATE quota_projection_state SET physical_signature='physical-b'",
            advance_stamp=True,
        )
        after = kernel.codex_stats_digest(conn)
        assert after != before
        assert after == kernel._stats_relations_digest(
            conn, kernel._CODEX_STATS_DIGEST_RELATIONS)
        assert conn.execute(
            "SELECT offset, applied_offset FROM journal_cursor"
        ).fetchone() == (100, 100)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "surgery",
    [
        # A mismatched public/applied pair — the cursor-only surgery the
        # previous key refused to trust.
        "UPDATE journal_cursor SET offset=101",
        # A half-null applied pair.
        "UPDATE journal_cursor SET applied_offset=NULL",
    ],
)
def test_cursor_surgery_is_an_ordinary_write_to_the_file_key(
    tmp_path, kernel, surgery,
):
    """The cursor no longer enters the key, so its states need no special case.

    Any committed edit, cursor-only or not, is a write to the file, and the
    next read rescans rather than trusting the prior entry.
    """
    path = tmp_path / "stats.db"
    conn = _file_store(path)
    statements = _traced(conn)
    try:
        before = kernel.codex_stats_digest(conn)
        _write_elsewhere(path, surgery, advance_stamp=True)
        assert kernel.codex_stats_digest(conn) == before
        assert _reads(statements, _CODEX_MARKER) == 2
    finally:
        conn.close()


def test_an_in_memory_store_never_memoizes(kernel):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE quota_projection_state "
        "(source_root_key TEXT, physical_signature TEXT)")
    statements = _traced(conn)
    try:
        kernel.codex_stats_digest(conn)
        kernel.codex_stats_digest(conn)
        assert _reads(statements, _CODEX_MARKER) == 2
        assert kernel._STATS_RELATIONS_DIGEST_MEMO == {}
    finally:
        conn.close()


def test_a_wal_store_never_memoizes(tmp_path, kernel):
    """A WAL commit lands in the `-wal` file and leaves the main file alone."""
    path = tmp_path / "stats.db"
    conn = _file_store(path, wal=True)
    statements = _traced(conn)
    try:
        before = kernel.codex_stats_digest(conn)
        kernel.codex_stats_digest(conn)
        assert _reads(statements, _CODEX_MARKER) == 2
        _write_elsewhere(
            path,
            "UPDATE quota_projection_state SET physical_signature='physical-b'",
        )
        assert kernel.codex_stats_digest(conn) != before
        assert kernel._STATS_RELATIONS_DIGEST_MEMO == {}
    finally:
        conn.close()


def test_an_open_transaction_never_memoizes(tmp_path, kernel):
    """The connection's own uncommitted write is invisible to the file key."""
    conn = _file_store(tmp_path / "stats.db")
    try:
        baseline = kernel.codex_stats_digest(conn)
        conn.execute(
            "UPDATE quota_projection_state SET physical_signature='uncommitted'")
        assert conn.in_transaction
        inside = kernel.codex_stats_digest(conn)
        assert inside != baseline
        conn.rollback()
        assert kernel.codex_stats_digest(conn) == baseline
    finally:
        conn.close()


def test_a_replaced_file_under_the_same_path_takes_the_cold_path(
    tmp_path, kernel,
):
    path = tmp_path / "stats.db"
    first = _file_store(path)
    try:
        before = kernel.codex_stats_digest(first)
    finally:
        first.close()
    replacement = tmp_path / "stats.db.replacement"
    other = _file_store(replacement)
    other.execute(
        "UPDATE quota_projection_state SET physical_signature='physical-new'")
    other.commit()
    other.close()
    os.replace(replacement, path)

    conn = sqlite3.connect(path)
    statements = _traced(conn)
    try:
        after = kernel.codex_stats_digest(conn)
        assert _reads(statements, _CODEX_MARKER) == 1
        assert after != before
        assert after == kernel._stats_relations_digest(
            conn, kernel._CODEX_STATS_DIGEST_RELATIONS)
    finally:
        conn.close()


def test_a_recently_written_file_is_not_memoized_until_it_settles(
    tmp_path, kernel, monkeypatch,
):
    """A coarse file timestamp cannot tell two commits in one tick apart.

    So nothing is memoized until the file's newest timestamp is at least the
    settle window old: a later commit then necessarily carries a newer one.
    """
    path = tmp_path / "stats.db"
    conn = _file_store(path)
    statements = _traced(conn)
    stat = os.stat(path)
    newest = max(stat.st_mtime_ns, stat.st_ctime_ns) / 1e9
    try:
        monkeypatch.setattr(kernel, "_stats_memo_now", lambda: newest + 0.5)
        kernel.codex_stats_digest(conn)
        kernel.codex_stats_digest(conn)
        assert _reads(statements, _CODEX_MARKER) == 2
        assert kernel._STATS_RELATIONS_DIGEST_MEMO == {}

        monkeypatch.setattr(
            kernel, "_stats_memo_now",
            lambda: newest + kernel._STATS_DIGEST_MEMO_SETTLE_SECONDS,
        )
        kernel.codex_stats_digest(conn)
        kernel.codex_stats_digest(conn)
        assert _reads(statements, _CODEX_MARKER) == 3
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The property the memo exists for.
# --------------------------------------------------------------------------

def test_repeated_idle_ingest_cycles_keep_reusing_the_memo(
    tmp_path, monkeypatch, kernel,
):
    """An idle cycle rewrites the cursor and selector rows to identical values.

    SQLite skips an overwrite whose bytes are unchanged, so the file is not
    written and the memo survives. Measured before this key was chosen: two
    `total_changes` per idle cycle, and no movement in the file's size, mtime,
    ctime or header change counter.
    """
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    _ingested_store(ns, jr, jl, rebuild=True)
    warm_codex, cursor = _warm(ns, kernel.codex_stats_digest, _CODEX_MARKER)
    warm_claude, _ = _warm(ns, kernel.claude_stats_digest, _CLAUDE_MARKER)
    core = importlib.import_module("_cctally_core")
    before = os.stat(core.DB_PATH)

    for _ in range(3):
        result = jr.run_stats_ingest(mode="authoritative")
        assert result.ran and result.consumed == 0

    after = os.stat(core.DB_PATH)
    assert (after.st_mtime_ns, after.st_ctime_ns, after.st_size) == (
        before.st_mtime_ns, before.st_ctime_ns, before.st_size)
    conn = ns["open_db"]()
    statements = _traced(conn)
    try:
        assert _cursor(conn) == cursor
        assert kernel.codex_stats_digest(conn) == warm_codex
        assert kernel.claude_stats_digest(conn) == warm_claude
        assert _reads(statements, _CODEX_MARKER) == 0
        assert _reads(statements, _CLAUDE_MARKER) == 0
    finally:
        conn.close()
