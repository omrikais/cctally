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
            claude_input_identity=tui._tui_read_claude_input_identity(stats),
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
            claude_input_identity=tui._tui_read_claude_input_identity(stats),
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


# --------------------------------------------------------------------------
# #1050 S1: the read-order seam. An identity certifies only data read after it.
# --------------------------------------------------------------------------

_1050_S1_NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _1050_s1_seed(conn):
    """A store whose Claude card shows 40 %, with unarmed milestone rows."""
    snap_id = conn.execute(
        "INSERT INTO weekly_usage_snapshots (week_start_date, week_end_date, "
        "captured_at_utc, week_start_at, week_end_at, weekly_percent, "
        "five_hour_percent, payload_json) VALUES ('2026-07-13', '2026-07-20', "
        "'2026-07-15T11:00:00Z', '2026-07-13T00:00:00Z', "
        "'2026-07-20T00:00:00Z', 40.0, 30.0, '{}')").lastrowid
    conn.execute(
        "INSERT INTO percent_milestones (week_start_date, week_end_date, "
        "week_start_at, week_end_at, percent_threshold, captured_at_utc, "
        "usage_snapshot_id, cost_snapshot_id, cumulative_cost_usd, "
        "marginal_cost_usd, five_hour_percent_at_crossing, reset_event_id) "
        "VALUES ('2026-07-13', '2026-07-20', '2026-07-13T00:00:00Z', "
        "'2026-07-20T00:00:00Z', 40, '2026-07-15T11:00:00Z', ?, 0, 2.0, 0.1, "
        "30.0, 0)", (snap_id,))
    conn.commit()


# variant → (the statement another connection commits, the value it stores)
_1050_S1_WRITERS = {
    "correction": (
        "UPDATE weekly_usage_snapshots SET weekly_percent = 31.0 "
        "WHERE week_start_date = '2026-07-13'", 31.0),
    "append": (
        "INSERT INTO weekly_usage_snapshots (week_start_date, week_end_date, "
        "captured_at_utc, week_start_at, week_end_at, weekly_percent, "
        "five_hour_percent, payload_json) VALUES ('2026-07-13', '2026-07-20', "
        "'2026-07-15T11:30:00Z', '2026-07-13T00:00:00Z', "
        "'2026-07-20T00:00:00Z', 45.0, 30.0, '{}')", 45.0),
}


@pytest.mark.parametrize("variant", sorted(_1050_S1_WRITERS))
def test_1050_read_order_seam_publishes_corrected_source(
        tmp_path, monkeypatch, variant):
    """#1050 §8.1 S1 (G1). Detects: a correction or a snapshot append that
    commits after the legacy and Projects assembly and before the source
    builder's own start read certifies the OLD supplied values under the NEW
    legs, so tick 2 and every later tick reuse a Claude source carrying the
    old percent by exact version."""
    import _lib_snapshot_cache as sc

    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    tui = ns["_cctally_tui"]
    sc.reset_dispatch_state()
    statement, value = _1050_S1_WRITERS[variant]
    conn = ns["open_db"]()
    try:
        _1050_s1_seed(conn)
    finally:
        conn.close()

    import _cctally_core
    real = tui._tui_build_source_bundle
    fired: list[str] = []

    def commit_at_entry(*args, **kwargs):
        # Every argument (the legacy envelope's Claude projection and the
        # Projects envelope) is already evaluated here; the builder's own
        # start read has not run yet.
        if not fired:
            writer = sqlite3.connect(_cctally_core.DB_PATH, timeout=10)
            try:
                writer.execute(statement)
                writer.commit()
            finally:
                writer.close()
            fired.append(variant)
        return real(*args, **kwargs)

    monkeypatch.setattr(tui, "_tui_build_source_bundle", commit_at_entry)

    def tick(minutes):
        now = _1050_S1_NOW + dt.timedelta(minutes=minutes)
        snap = ns["_tui_build_snapshot"](
            now_utc=now, skip_sync=True, precompute_envelope=True,
            runtime_bind="127.0.0.1", display_tz_pref_override="utc")
        source = snap.source_bundle.sources["claude"]
        return snap, source.data["hero"]["current_week"]["used_pct"]

    try:
        _snap1, pct1 = tick(0)
        assert fired == [variant], "the seam writer committed during tick 1"
        assert pct1 == 40.0, "tick 1's supplied inputs predate the commit"
        reader = sqlite3.connect(_cctally_core.DB_PATH)
        try:
            assert reader.execute(
                "SELECT weekly_percent FROM weekly_usage_snapshots "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
            ).fetchone()[0] == value, "the stored value changed"
        finally:
            reader.close()
        snap2, pct2 = tick(1)
        snap3, pct3 = tick(2)
        print(f"[#1050 legs] s1:{variant}: card percent over ticks 1-3 = "
              f"{[pct1, pct2, pct3]}; tick2 data_version "
              f"{snap2.data_version!r}; tick3 {snap3.data_version!r}")
        failed = [label for ok, label in (
            (pct2 == value, f"tick 2 publishes the corrected {value}"),
            (pct3 == value, f"tick 3 publishes the corrected {value}"),
        ) if not ok]
        assert not failed, f"#1050 s1:{variant}: GREEN assertions failed: {failed}"
    finally:
        sc.reset_dispatch_state()
        tui._tui_reset_partial_retry_state()


# --------------------------------------------------------------------------
# #1050 §8.4 coherence: a tracked mutation committed from another connection at
# each seam of a tick, then the published Claude card over three ticks; and
# the reconciliation tuple (H1, H2) that brackets the build.
# --------------------------------------------------------------------------

#: An append to the weekly cost table for a week no card reads. It moves the
#: append-shaped `max_wcs_id` leg (on `main` too), so the tick it precedes is a
#: real full build that reaches every seam, and it changes no published value.
_1050_NUDGE = (
    "INSERT INTO weekly_cost_snapshots (captured_at_utc, week_start_date, "
    "week_end_date, cost_usd) VALUES (?, '2026-06-01', '2026-06-08', 1.0)")
_1050_CORRECTION, _1050_CORRECTED = _1050_S1_WRITERS["correction"]


@pytest.fixture
def ns1050(tmp_path, monkeypatch):
    import _lib_snapshot_cache as sc

    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sc.reset_dispatch_state()
    conn = ns["open_db"]()
    try:
        _1050_s1_seed(conn)
    finally:
        conn.close()
    yield ns
    sc.reset_dispatch_state()
    ns["_cctally_tui"]._tui_reset_partial_retry_state()


def _1050_commit(statement, params=()):
    """Commit ``statement`` from a connection of its own, as another process."""
    import _cctally_core
    writer = sqlite3.connect(_cctally_core.DB_PATH, timeout=10)
    try:
        writer.execute(statement, params)
        writer.commit()
    finally:
        writer.close()


def _1050_nudge(n):
    _1050_commit(_1050_NUDGE, (f"2026-06-02T00:00:{n:02d}Z",))


def _1050_claude_entry(ns, offset):
    """Append one Claude cache entry inside the seeded week (cache.db only)."""
    path = "/tmp/claude/projects/repo/1050-seam.jsonl"
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 100000, 1, 100000, "2026-07-15T11:00:00Z", "session-1050",
             "/repo"))
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens) VALUES (?,?,?,?,1000,0,0,0,0)",
            (path, offset, "2026-07-15T11:30:00+00:00",
             "claude-3-5-sonnet-20241022"))
        conn.commit()
    finally:
        conn.close()


def _1050_tick(ns, minutes):
    return ns["_tui_build_snapshot"](
        now_utc=_1050_S1_NOW + dt.timedelta(minutes=minutes), skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
        display_tz_pref_override="utc")


def _1050_claude(snap):
    return snap.source_bundle.sources["claude"]


def _1050_pct(snap):
    return _1050_claude(snap).data["hero"]["current_week"]["used_pct"]


def _1050_once(fired, action):
    """Run ``action`` the first time only, recording that it fired."""
    if not fired:
        action()
        fired.append(True)


def _1050_arm_seam(ns, monkeypatch, seam, action):
    """Fire ``action`` once, at ``seam`` of the next tick."""
    import _lib_snapshot_cache as sc
    tui = ns["_cctally_tui"]
    fired: list[bool] = []
    if seam == "dispatch_to_legacy":
        real = tui._tui_compute_dispatch_signature

        def after_dispatch(*args, **kwargs):
            result = real(*args, **kwargs)
            _1050_once(fired, action)
            return result
        monkeypatch.setattr(tui, "_tui_compute_dispatch_signature",
                            after_dispatch)
    elif seam == "assembly_to_start_read":
        real = tui._tui_build_source_bundle

        def at_entry(*args, **kwargs):
            _1050_once(fired, action)
            return real(*args, **kwargs)
        monkeypatch.setattr(tui, "_tui_build_source_bundle", at_entry)
    elif seam == "start_read_to_reconciliation":
        real = tui.compose_all_state

        def at_compose(claude, codex):
            _1050_once(fired, action)
            return real(claude, codex)
        monkeypatch.setattr(tui, "compose_all_state", at_compose)
    elif seam == "reconciliation_to_store":
        real = sc.store_dispatch_state

        def before_store(*args, **kwargs):
            _1050_once(fired, action)
            return real(*args, **kwargs)
        monkeypatch.setattr(sc, "store_dispatch_state", before_store)
    elif seam == "idle_dispatch_to_adapter":
        real = tui._tui_build_idle_snapshot
        can_idle = tui._tui_source_bundle_can_idle

        def at_idle(*args, **kwargs):
            _1050_once(fired, action)
            return real(*args, **kwargs)

        def bounded_once(*args, **kwargs):
            # The idle tick that fired takes the bounded source adapter
            # rather than the clock-only branch; later ticks are unforced.
            if len(fired) == 1 and not getattr(bounded_once, "used", False):
                bounded_once.used = True
                return False
            return can_idle(*args, **kwargs)
        monkeypatch.setattr(tui, "_tui_build_idle_snapshot", at_idle)
        monkeypatch.setattr(tui, "_tui_source_bundle_can_idle", bounded_once)
    else:
        raise AssertionError(seam)
    return fired


def _1050_run_seam(ns, monkeypatch, seam, *, publishes_mutation_tick=False):
    """``publishes_mutation_tick``: the correction moves only the content
    revision inside a warm build, so revision 6 (§4.5 item 5) publishes that
    build on tick 1 instead of retaining the prior Claude source."""
    snap0 = _1050_tick(ns, 0)
    assert _1050_pct(snap0) == 40.0
    fired = _1050_arm_seam(
        ns, monkeypatch, seam, lambda: _1050_commit(_1050_CORRECTION))
    # Seams inside a build need a building tick 1; the two seams that follow a
    # dispatch read (i, v) are exercised on the idle tick that read leaves.
    if seam not in ("dispatch_to_legacy", "idle_dispatch_to_adapter"):
        _1050_nudge(1)
    snaps = [_1050_tick(ns, minutes) for minutes in (1, 2, 3)]
    assert fired, f"the {seam} seam was reached on tick 1"
    reader = sqlite3.connect(importlib.import_module("_cctally_core").DB_PATH)
    try:
        assert reader.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"
        ).fetchone()[0] == _1050_CORRECTED, "the stored value changed"
    finally:
        reader.close()
    pcts = [_1050_pct(s) for s in snaps]
    print(f"[#1050 seam] {seam}: card percent over ticks 1-3 = {pcts}; "
          f"data_version {[s.data_version for s in snaps]}")
    checks = []
    if publishes_mutation_tick:
        checks.append((_1050_claude(snaps[0]) is not _1050_claude(snap0),
                       "tick 1 publishes its newly built Claude source"))
    failed = [label for ok, label in (
        *checks,
        (pcts[1] == _1050_CORRECTED, "tick 2 publishes the corrected value"),
        (pcts[2] == _1050_CORRECTED, "tick 3 publishes the corrected value"),
    ) if not ok]
    assert not failed, f"#1050 seam {seam}: GREEN assertions failed: {failed}"


def test_1050_seam_dispatch_to_legacy_publishes_corrected_value(
        ns1050, monkeypatch):
    """#1050 §8.4 seam (i). Detects: a correction committed between the
    dispatch read and the legacy builders being certified under the older
    dispatch key, so later ticks idle on the old card percent."""
    _1050_run_seam(ns1050, monkeypatch, "dispatch_to_legacy")


def test_1050_seam_assembly_to_start_read_publishes_corrected_value(
        ns1050, monkeypatch):
    """#1050 §8.4 seam (ii), on a warm dashboard (S1 is the cold twin).
    Detects: a correction committed after the legacy and Projects assembly
    certifying the OLD supplied values under the builder's NEW start read, so
    later ticks reuse a Claude source carrying the old percent; and, under
    revision 6, a warm build discarded because only the content revision
    moved against the dispatch read."""
    _1050_run_seam(ns1050, monkeypatch, "assembly_to_start_read",
                   publishes_mutation_tick=True)


def test_1050_seam_start_read_to_reconciliation_publishes_corrected_value(
        ns1050, monkeypatch):
    """#1050 §8.4 seam (iii). Detects: a correction committed while the
    source is being constructed escaping the build bracket, so the built
    source with the old percent is published and later ticks idle on it;
    and, under revision 6, a warm build discarded because only the content
    revision moved against the dispatch read."""
    _1050_run_seam(ns1050, monkeypatch, "start_read_to_reconciliation",
                   publishes_mutation_tick=True)


def test_1050_seam_reconciliation_to_store_publishes_corrected_value(
        ns1050, monkeypatch):
    """#1050 §8.4 seam (iv). Detects: a correction committed after the
    bracket and before the dispatch state is stored leaving the stored key
    equal to the next tick's read, so the dashboard idles on the old card."""
    _1050_run_seam(ns1050, monkeypatch, "reconciliation_to_store")


def test_1050_seam_idle_dispatch_to_adapter_publishes_corrected_value(
        ns1050, monkeypatch):
    """#1050 §8.4 seam (v). Detects: a correction committed between an idle
    tick's dispatch read and its bounded source adapter re-certifying the
    prior snapshot's values under the adapter's newer read, so the old card
    percent is reused on every later tick."""
    _1050_run_seam(ns1050, monkeypatch, "idle_dispatch_to_adapter")


def _1050_seed_block(*, unavailable):
    """One five-hour block and milestone in the seeded week; the milestone's
    weekly join is the ``0`` sentinel (unavailable, so the retired digest is
    nonempty) when ``unavailable``."""
    import _cctally_core
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        snap_id = conn.execute(
            "SELECT id FROM weekly_usage_snapshots").fetchone()[0]
        block_id = conn.execute(
            "INSERT INTO five_hour_blocks (five_hour_window_key, "
            "five_hour_resets_at, block_start_at, first_observed_at_utc, "
            "last_observed_at_utc, final_five_hour_percent, is_closed, "
            "created_at_utc, last_updated_at_utc, total_cost_usd) VALUES "
            "(1784124000, '2026-07-15T15:00:00+00:00', "
            "'2026-07-15T10:00:00+00:00', '2026-07-15T10:30:00+00:00', "
            "'2026-07-15T11:00:00+00:00', 30.0, 0, "
            "'2026-07-15T10:30:00+00:00', '2026-07-15T11:00:00+00:00', 1.0)"
        ).lastrowid
        conn.execute(
            "INSERT INTO five_hour_milestones (block_id, five_hour_window_key, "
            "percent_threshold, captured_at_utc, usage_snapshot_id, "
            "block_cost_usd, reset_event_id) VALUES (?, 1784124000, 30, "
            "'2026-07-15T11:00:00Z', ?, 1.0, 0)",
            (block_id, 0 if unavailable else snap_id))
        conn.commit()
    finally:
        conn.close()


def _1050_retired_digest(ns):
    kernel = importlib.import_module("_lib_dashboard_sources")
    conn = ns["open_db"]()
    try:
        return kernel.claude_weekly_retired_digest(conn)
    finally:
        conn.close()


def _1050_failed_legs(snap):
    return [f.leg for f in getattr(snap, "sync_failures", ())]


def test_1050_stable_retired_store_builds_cold_and_warm(ns1050):
    """#1050 §8.4 reconciliation tuple (H1). Detects: the post-build
    signature omitting the re-read retired digest, which then defaults to
    ``""`` and differs from the input identity's on every store with a
    retired snapshot or unavailable five-hour join — every build rejected,
    the cold start publishing no Claude source at all."""
    ns = ns1050
    _1050_seed_block(unavailable=True)
    digest = _1050_retired_digest(ns)
    assert digest, "non-vacuity: a stable nonempty retired digest"
    cold = _1050_tick(ns, 0)
    assert "source-bundle" not in _1050_failed_legs(cold), cold.sync_failures
    assert f":r{digest}" in _1050_claude(cold).data_version
    _1050_nudge(1)
    warm = _1050_tick(ns, 1)
    assert "source-bundle" not in _1050_failed_legs(warm), warm.sync_failures
    assert _1050_claude(warm) is not _1050_claude(cold), (
        "the warm build was rejected and the prior source retained")


def test_1050_retirement_transition_publishes_by_tick_3(ns1050, monkeypatch):
    """#1050 §8.4 reconciliation tuple (H1). Detects: a retirement committed
    during the build (a five-hour milestone's weekly join rewritten to the
    ``0`` sentinel) never reaching the published Claude source version."""
    ns = ns1050
    _1050_seed_block(unavailable=False)
    assert _1050_retired_digest(ns) == ""
    _1050_tick(ns, 0)
    fired = _1050_arm_seam(
        ns, monkeypatch, "start_read_to_reconciliation",
        lambda: _1050_commit("UPDATE five_hour_milestones "
                             "SET usage_snapshot_id = 0"))
    _1050_nudge(1)
    snaps = [_1050_tick(ns, minutes) for minutes in (1, 2, 3)]
    assert fired
    digest = _1050_retired_digest(ns)
    assert digest, "the retirement is stored"
    assert f":r{digest}" in _1050_claude(snaps[2]).data_version, [
        _1050_claude(s).data_version for s in snaps]


def test_1050_cache_append_after_pin_does_not_reject(ns1050, monkeypatch):
    """#1050 §8.4 reconciliation tuple (H2). Detects: the bracket re-reading
    the cache legs after the builder released its pinned cache transaction,
    so an ordinary ingest commit during the pure folds rejects every coherent
    build and the dashboard keeps the prior source."""
    ns = ns1050
    first = _1050_tick(ns, 0)
    fired = _1050_arm_seam(ns, monkeypatch, "start_read_to_reconciliation",
                           lambda: _1050_claude_entry(ns, 1))
    _1050_nudge(1)
    second = _1050_tick(ns, 1)
    assert fired
    assert "source-bundle" not in _1050_failed_legs(second)
    assert _1050_claude(second) is not _1050_claude(first), (
        "a cache commit after the pin rejected the build")


def test_1050_cache_append_before_start_read_publishes_then_rebuilds(
        ns1050, monkeypatch):
    """#1050 §8.4 reconciliation tuple (H2), revision 6 §4.5 item 5.
    Detects: a warm Claude source discarded whenever an external cache commit
    lands between the dispatch read and the builder's start read, so steady
    hook-tick ingestion starves it. The build publishes under the dispatch
    read's version instead; that read precedes the commit, so the next tick's
    dispatch read differs, the tick is non-idle and rebuilds."""
    ns = ns1050
    first = _1050_tick(ns, 0)
    fired = _1050_arm_seam(ns, monkeypatch, "assembly_to_start_read",
                           lambda: _1050_claude_entry(ns, 1))
    _1050_nudge(1)
    second = _1050_tick(ns, 1)
    third = _1050_tick(ns, 2)
    assert fired
    failed = [label for ok, label in (
        ("source-bundle" not in _1050_failed_legs(second),
         "tick 1 records no source failure"),
        (_1050_claude(second) is not _1050_claude(first),
         "tick 1 publishes a new Claude source, not the prior one"),
        (third.data_version != second.data_version,
         "tick 2's dispatch read sees the commit, so it is not idle"),
        (_1050_claude(third) is not _1050_claude(second)
         and _1050_claude(third).data_version
         != _1050_claude(second).data_version,
         "tick 2 rebuilds the Claude source under the newer identity"),
    ) if not ok]
    assert not failed, f"#1050 cache-before-start: GREEN assertions failed: {failed}"
