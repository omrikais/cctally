"""#1050 — the trigger-maintained content revision of stats.db's Claude domain.

The kernel (`bin/_lib_stats_content_revision.py`) is the one allocator behind
every #1050 dashboard signal: an in-place rewrite of a Claude-published stats
row must advance its global `seq` and dirty the scopes the rewritten row
belongs to, while an identical rewrite, a bookkeeping-only change, a rolled
back write and a normal append must not. Its installation must never write on
a `current` open, must repair a damaged domain without resetting `seq`, must
leave untrusted stores untouched, and must survive a rebuild publication.

Every test here names the stored-data or accuracy failure it detects. Spec:
`docs/superpowers/specs/2026-11-04-1050-correction-signal-design.md` §4.1–§4.4
and §8.3–§8.4.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import pathlib
import re
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_stats_content_revision as k  # noqa: E402


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _db_path():
    import _cctally_core
    return pathlib.Path(_cctally_core.DB_PATH)


def _fresh(ns) -> pathlib.Path:
    conn = ns["open_db"]()
    conn.close()
    return _db_path()


@contextlib.contextmanager
def _raw(path):
    conn = sqlite3.connect(str(path))
    try:
        yield conn
    finally:
        conn.close()


def _seq(conn) -> int:
    return conn.execute(
        "SELECT seq FROM stats_content_revision WHERE singleton = 1"
    ).fetchone()[0]


def _singleton(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT format_version, domain_generation, seq "
        "FROM stats_content_revision")]


def _scopes(conn):
    return {
        (r[0], r[1], r[2]): (r[3], r[4], r[5])
        for r in conn.execute(
            "SELECT kind, account_key, scope_key, start_epoch, end_epoch, "
            "revision FROM stats_content_revision_scopes")
    }


def _file_digest(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# ── seeds ─────────────────────────────────────────────────────────────────

_BLOCK_KEY = 1777566600  # 2026-04-30T16:30:00Z, the reset of the probe block


def _insert(conn, table, **cols):
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    return conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks})",
        tuple(cols.values())).lastrowid


def _snapshot(conn, *, week="2026-04-27", percent=40.0, account="unattributed"):
    return _insert(
        conn, "weekly_usage_snapshots", captured_at_utc="2026-04-30T11:00:00Z",
        week_start_date=week, week_end_date="2026-05-04",
        weekly_percent=percent, payload_json="{}", account_key=account)


def _block(conn, *, account="unattributed", key=_BLOCK_KEY,
           start="2026-04-30T11:30:00+00:00",
           resets="2026-04-30T16:30:00+00:00"):
    return _insert(
        conn, "five_hour_blocks", five_hour_window_key=key,
        five_hour_resets_at=resets, block_start_at=start,
        first_observed_at_utc="2026-04-30T11:30:00Z",
        last_observed_at_utc="2026-04-30T12:00:00Z",
        final_five_hour_percent=10.0, created_at_utc="2026-04-30T11:30:00Z",
        last_updated_at_utc="2026-04-30T12:00:00Z", account_key=account)


def _five_hour_milestone(conn, *, block_id, snapshot_id, key=_BLOCK_KEY,
                         account="unattributed", threshold=10):
    return _insert(
        conn, "five_hour_milestones", block_id=block_id,
        five_hour_window_key=key, percent_threshold=threshold,
        captured_at_utc="2026-04-30T12:00:00Z", usage_snapshot_id=snapshot_id,
        block_cost_usd=1.0, account_key=account)


def _seed_every_table(conn) -> dict:
    """One row in every tracked table; returns table → rowid (or key)."""
    snap = _snapshot(conn)
    block = _block(conn)
    rows = {
        "weekly_usage_snapshots": snap,
        "five_hour_blocks": block,
        "weekly_cost_snapshots": _insert(
            conn, "weekly_cost_snapshots",
            captured_at_utc="2026-04-30T11:00:00Z",
            week_start_date="2026-04-27", week_end_date="2026-05-04",
            cost_usd=2.0),
        "percent_milestones": _insert(
            conn, "percent_milestones", captured_at_utc="2026-04-30T11:00:00Z",
            week_start_date="2026-04-27", week_end_date="2026-05-04",
            percent_threshold=40, cumulative_cost_usd=2.0,
            usage_snapshot_id=snap, cost_snapshot_id=0, reset_event_id=0),
        "five_hour_milestones": _five_hour_milestone(
            conn, block_id=block, snapshot_id=snap),
        "five_hour_block_models": _insert(
            conn, "five_hour_block_models", block_id=block,
            five_hour_window_key=_BLOCK_KEY, model="m"),
        "five_hour_block_projects": _insert(
            conn, "five_hour_block_projects", block_id=block,
            five_hour_window_key=_BLOCK_KEY, project_path="/p"),
        "five_hour_reset_events": _insert(
            conn, "five_hour_reset_events",
            detected_at_utc="2026-04-30T12:00:00Z",
            five_hour_window_key=_BLOCK_KEY, prior_percent=50.0,
            post_percent=5.0, effective_reset_at_utc="2026-04-30T12:00:00Z"),
        "week_reset_events": _insert(
            conn, "week_reset_events", detected_at_utc="2026-04-30T12:00:00Z",
            old_week_end_at="2026-05-04T00:00:00Z",
            new_week_end_at="2026-05-07T00:00:00Z",
            effective_reset_at_utc="2026-04-30T12:00:00Z"),
        "weekly_credit_floors": _insert(
            conn, "weekly_credit_floors", week_start_date="2026-04-27",
            effective_at_utc="2026-04-30T12:00:00Z",
            observed_pre_credit_pct=40.0,
            applied_at_utc="2026-04-30T12:00:00Z"),
        "claude_weekly_observation_index": _insert(
            conn, "claude_weekly_observation_index", observation_id="o1",
            account_key="unattributed",
            captured_at_utc="2026-04-30T12:00:00Z", weekly_percent=40.0,
            weekly_reading_genuine=1, weekly_freshness_eligible=1) and "o1",
        "budget_milestones": _insert(
            conn, "budget_milestones", vendor="claude",
            period_start_at="2026-04-27T00:00:00Z", threshold=50,
            budget_usd=10.0, spent_usd=5.0, consumption_pct=50.0,
            crossed_at_utc="2026-04-30T12:00:00Z"),
        "projected_milestones": _insert(
            conn, "projected_milestones", week_start_at="2026-04-27T00:00:00Z",
            metric="weekly_percent", threshold=90, projected_value=95.0,
            denominator=100.0, crossed_at_utc="2026-04-30T12:00:00Z"),
        "project_budget_milestones": _insert(
            conn, "project_budget_milestones",
            week_start_at="2026-04-27T00:00:00Z", project_key="/p",
            threshold=50, budget_usd=10.0, spent_usd=5.0,
            consumption_pct=50.0, crossed_at_utc="2026-04-30T12:00:00Z"),
    }
    conn.commit()
    return rows


def _key_column(table):
    return "observation_id" if table == "claude_weekly_observation_index" else "id"


def _changed_value(conn, table, column, key):
    """A value of ``column`` different from the row's current one that keeps
    every constraint (types, CHECKs, the natural-key UNIQUEs)."""
    current = conn.execute(
        f"SELECT {column} FROM {table} WHERE {_key_column(table)} = ?",
        (key,)).fetchone()[0]
    if column == "id":
        return 9000 + int(current or 0)
    if column == "weekly_observation_held":
        return 1 - int(current)
    if column in ("weekly_reading_genuine", "weekly_freshness_eligible",
                  "is_closed", "crossed_seven_day_reset"):
        return 1 - int(current or 0)
    if column == "observation_id":
        return "o-moved"
    if isinstance(current, (int, float)):
        return current + 1
    if isinstance(current, str) and re.match(r"\d{4}-\d\d-\d\d", current):
        return current.replace("2026-", "2025-", 1)
    return f"{current or ''}-moved"


# ── classification ────────────────────────────────────────────────────────

def test_every_column_of_every_tracked_table_is_classified(ns):
    """#1050 §4.2/§8.4. Detects: a column added to a tracked table without a
    classification — an in-place rewrite of it would publish a new value that
    no signal reports."""
    with _raw(_fresh(ns)) as conn:
        for table, cls in k.TRACKED_TABLES.items():
            columns = [r[1] for r in conn.execute(
                f"PRAGMA table_info({table})")]
            assert columns, table
            assert set(cls.compared).isdisjoint(cls.excluded), table
            assert sorted(cls.compared + cls.excluded) == sorted(columns), table
            assert cls.insert_rule in (
                "tracked", "below_max", "exempt", "untracked"), table
            assert set(cls.scopes) <= set(k.SCOPE_KINDS), table


# ── triggers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", sorted(k.TRACKED_TABLES))
def test_trigger_fires_only_on_a_compared_value_change(ns, table):
    """#1050 §4.1/§8.3. Detects, per tracked table: an identical assignment or
    an excluded (bookkeeping) column change that advances `seq` (the
    dashboard would never idle), and a compared column change or a DELETE
    that does not advance it exactly once (the rewrite would stay
    invisible)."""
    cls = k.TRACKED_TABLES[table]
    keycol = _key_column(table)
    with _raw(_fresh(ns)) as conn:
        key = _seed_every_table(conn)[table]
        columns = cls.compared + cls.excluded
        before = _seq(conn)
        conn.execute(
            f"UPDATE {table} SET "
            + ", ".join(f"{c} = {c}" for c in columns)
            + f" WHERE {keycol} = ?", (key,))
        conn.commit()
        assert _seq(conn) == before, "identical assignment"
        for column in cls.excluded:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {keycol} = ?",
                (_changed_value(conn, table, column, key), key))
            conn.commit()
            assert _seq(conn) == before, f"excluded {column}"
        for column in cls.compared:
            value = _changed_value(conn, table, column, key)
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {keycol} = ?",
                (value, key))
            conn.commit()
            assert _seq(conn) == before + 1, f"compared {column}"
            before += 1
            if column == keycol:
                key = value
        conn.execute(f"DELETE FROM {table} WHERE {keycol} = ?", (key,))
        conn.commit()
        assert _seq(conn) == before + 1, "delete"


def test_a_rolled_back_update_leaves_seq(ns):
    """#1050 §8.3. Detects: trigger effects that survive the rollback of the
    write that fired them — the identity would move for a correction that
    never happened."""
    with _raw(_fresh(ns)) as conn:
        _seed_every_table(conn)
        before = _seq(conn), _scopes(conn)
        conn.execute("UPDATE weekly_usage_snapshots SET weekly_percent = 1.0")
        assert _seq(conn) == before[0] + 1
        conn.rollback()
        assert (_seq(conn), _scopes(conn)) == before


def test_snapshot_inserts_and_deletes_follow_the_below_max_rule(ns):
    """#1050 §4.2/§8.3. Detects: a normal snapshot append that advances `seq`
    (every recorded reading would leave the idle path twice), and an insert
    below `MAX(id)` or a non-max DELETE that does not (`MAX(id)` would not
    move either, so the rewrite would be invisible)."""
    with _raw(_fresh(ns)) as conn:
        for _ in range(3):
            _snapshot(conn)
        conn.commit()
        base = _seq(conn)
        assert base == 0
        conn.execute("DELETE FROM weekly_usage_snapshots WHERE id = 2")
        conn.commit()
        assert _seq(conn) == base + 1, "a non-max delete"
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (id, captured_at_utc, "
            "week_start_date, week_end_date, weekly_percent, payload_json) "
            "VALUES (2, 'x', '2026-04-27', '2026-05-04', 1.0, '{}')")
        conn.commit()
        assert _seq(conn) == base + 2, "an insert below MAX(id)"
        _snapshot(conn)
        conn.execute(
            "INSERT INTO weekly_cost_snapshots (captured_at_utc, "
            "week_start_date, week_end_date, cost_usd) "
            "VALUES ('x', '2026-04-27', '2026-05-04', 1.0)")
        conn.commit()
        assert _seq(conn) == base + 2, "an append"


def test_a_change_dirties_its_scopes_from_old_and_new(ns):
    """#1050 §4.1/§4.2. Detects: a rewrite that advances `seq` but does not
    mark the scopes a cycle stamp reads — the cached cycle detail of the
    moved row's old or new week, or of the block whose milestone references a
    rewritten snapshot, would never refetch."""
    with _raw(_fresh(ns)) as conn:
        rows = _seed_every_table(conn)
        conn.execute(
            "UPDATE weekly_usage_snapshots SET week_start_date = '2026-05-04' "
            "WHERE id = ?", (rows["weekly_usage_snapshots"],))
        conn.commit()
        seq = _seq(conn)
        scopes = _scopes(conn)
        assert scopes[("bucket", "unattributed", "2026-04-27")][2] == seq
        assert scopes[("bucket", "unattributed", "2026-05-04")][2] == seq
        start, end, revision = scopes[("block", "unattributed", str(_BLOCK_KEY))]
        assert (start, end, revision) == (1777548600, 1777566600, seq)

        conn.execute("UPDATE percent_milestones SET reset_event_id = 7")
        conn.commit()
        seq = _seq(conn)
        scopes = _scopes(conn)
        assert scopes[("cohort", "unattributed", "2026-04-27|0")][2] == seq
        assert scopes[("cohort", "unattributed", "2026-04-27|7")][2] == seq
        assert scopes[("bucket", "unattributed", "2026-04-27")][2] == seq


def test_a_five_hour_milestone_falls_back_to_its_natural_block_key(ns):
    """#1050 §4.2. Detects: a five-hour milestone whose `block_id` names no
    block (an orphan of a parent rebuild) dirtying no block, so the detail
    that lists it under its natural block would keep the old cost."""
    with _raw(_fresh(ns)) as conn:
        snap = _snapshot(conn)
        _block(conn)
        milestone = _five_hour_milestone(conn, block_id=424242, snapshot_id=snap)
        conn.commit()
        conn.execute(
            "UPDATE five_hour_milestones SET block_cost_usd = 9.0 WHERE id = ?",
            (milestone,))
        conn.commit()
        assert _scopes(conn)[("block", "unattributed", str(_BLOCK_KEY))][2] == (
            _seq(conn))


def test_an_account_move_dirties_both_accounts(ns):
    """#1050 §8.3. Detects: an account-moving UPDATE that dirties only the
    new account — the old account's scoped history would keep a detail that
    still lists the moved row."""
    with _raw(_fresh(ns)) as conn:
        _block(conn, account="acct-a")
        conn.commit()
        conn.execute("UPDATE five_hour_blocks SET account_key = 'acct-b'")
        conn.commit()
        seq = _seq(conn)
        scopes = _scopes(conn)
        assert scopes[("block", "acct-a", str(_BLOCK_KEY))][2] == seq
        assert scopes[("block", "acct-b", str(_BLOCK_KEY))][2] == seq


def test_a_straddling_block_widens_its_epoch_range(ns):
    """#1050 §4.1/§8.3. Detects: a block scope that keeps only the latest
    range, so a block whose start moved out of a cycle stops re-stamping the
    cycle it used to straddle."""
    with _raw(_fresh(ns)) as conn:
        block = _block(conn)
        conn.commit()
        conn.execute(
            "UPDATE five_hour_blocks SET block_start_at = "
            "'2026-04-26T22:00:00+00:00' WHERE id = ?", (block,))
        conn.execute(
            "UPDATE five_hour_blocks SET block_start_at = "
            "'2026-04-30T12:30:00+00:00', five_hour_resets_at = "
            "'2026-04-30T17:30:00+00:00' WHERE id = ?", (block,))
        conn.commit()
        start, end, revision = _scopes(conn)[
            ("block", "unattributed", str(_BLOCK_KEY))]
        assert start == 1777240800  # 2026-04-26T22:00Z: the widest start
        assert end == 1777570200    # 2026-04-30T17:30Z: the widest end
        assert revision == _seq(conn) == 3


def test_a_reset_moved_twice_then_deleted_marks_every_instant(ns):
    """#1050 §4.1/§8.3. Detects: a weekly reset rewrite that marks only its
    final instant, so the cycles its earlier boundaries closed keep their
    stamps (and their stale segment split)."""
    with _raw(_fresh(ns)) as conn:
        reset = _insert(
            conn, "week_reset_events", detected_at_utc="2026-04-30T12:00:00Z",
            old_week_end_at="2026-05-04T00:00:00Z",
            new_week_end_at="2026-05-07T00:00:00Z",
            effective_reset_at_utc="2026-04-30T12:00:00Z")
        conn.commit()
        assert _seq(conn) == 0, "a reset insert is covered by _reset_sig"
        for instant in ("2026-04-30T13:00:00Z", "2026-04-30T14:00:00Z"):
            conn.execute(
                "UPDATE week_reset_events SET effective_reset_at_utc = ? "
                "WHERE id = ?", (instant, reset))
            conn.commit()
        conn.execute("DELETE FROM week_reset_events WHERE id = ?", (reset,))
        conn.commit()
        assert _seq(conn) == 3
        points = {key[2]: value for key, value in _scopes(conn).items()
                  if key[0] == "reset_point"}
        expected = {
            "1777550400": 1,  # 12:00, the first instant, dirtied by update 1
            "1777554000": 2,  # 13:00, moved to then away from
            "1777557600": 3,  # 14:00, the last instant, dirtied by the delete
            "1777852800": 3,  # old_week_end_at
            "1778112000": 3,  # new_week_end_at
        }
        assert {p: v[2] for p, v in points.items()} == expected
        assert all(v[0] == v[1] == int(p) for p, v in points.items())


@pytest.mark.parametrize(("table", "column", "codex", "claude"), (
    ("budget_milestones", "vendor", "codex", "claude"),
    ("projected_milestones", "metric", "codex_budget_usd", "weekly_percent"),
))
def test_alert_tables_track_only_claude_owned_rows(ns, table, column, codex,
                                                   claude):
    """#1050 §4.2. Detects: a Codex-owned budget or projection row advancing
    the Claude identity (the Claude source would rebuild on every Codex alert
    update), or a Claude-owned one not advancing it."""
    with _raw(_fresh(ns)) as conn:
        key = _seed_every_table(conn)[table]
        before = _seq(conn)
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?",
                     (codex, key))
        conn.commit()
        moved_to_codex = _seq(conn)
        assert moved_to_codex == before + 1, "leaving Claude ownership"
        conn.execute(f"UPDATE {table} SET alerted_at = 'x' WHERE id = ?",
                     (key,))
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (key,))
        conn.commit()
        assert _seq(conn) == moved_to_codex, "a Codex row is not tracked"


def test_trigger_lookups_use_an_index_without_a_temp_btree(ns):
    """#1050 §8.4. Detects: a trigger lookup that scans a table or sorts
    through a temp b-tree — every tracked write (a whole replay included)
    would pay a scan per row."""
    with _raw(_fresh(ns)) as conn:
        for name, sql in k.trigger_lookup_queries():
            params = {p: 1 for p in re.findall(r":(\w+)", sql)}
            plan = [str(r[3]) for r in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, params)]
            for detail in plan:
                assert "TEMP B-TREE" not in detail, (name, plan)
                assert not (detail.startswith("SCAN ")
                            and detail != "SCAN CONSTANT ROW"), (name, plan)


# ── installation states ───────────────────────────────────────────────────

def _drop_kernel_objects(conn):
    for name in k.KERNEL_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(f"DROP INDEX IF EXISTS {k.USAGE_SNAPSHOT_INDEX}")
    conn.execute(f"DROP TABLE IF EXISTS {k.REVISION_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {k.SCOPE_TABLE}")
    conn.commit()


def test_a_current_reopen_writes_nothing(ns):
    """#1050 §4.3/§8.4. Detects: the hot-path open writing or locking for an
    installed store — every statusline render would rewrite stats.db."""
    path = _fresh(ns)
    digest, mtime = _file_digest(path), os.stat(path).st_mtime_ns
    conn = ns["open_db"]()
    try:
        assert k.classify(conn) == k.STATE_CURRENT
        assert conn.total_changes == 0
    finally:
        conn.close()
    assert (_file_digest(path), os.stat(path).st_mtime_ns) == (digest, mtime)


def test_a_current_epoch_store_without_the_objects_installs_on_open(ns):
    """#1050 §4.3/§8.4. Detects: an index published by an older binary (no
    kernel objects) staying untracked at the current epoch — no rewrite on it
    would ever be reported."""
    with _raw(_fresh(ns)) as conn:
        _drop_kernel_objects(conn)
        assert k.classify(conn) == k.STATE_ABSENT
    conn = ns["open_db"]()
    try:
        assert k.classify(conn) == k.STATE_CURRENT
        assert _singleton(conn) == [(k.KERNEL_FORMAT_VERSION, 0, 0)]
        rev = k.read_claude_content_revision(conn)
        assert rev == k.ContentRevision(k.STATE_CURRENT, "", None)
    finally:
        conn.close()


@pytest.mark.parametrize("damage", ("trigger", "index", "older_format"))
def test_a_partial_store_is_repaired_without_resetting_seq(
        ns, monkeypatch, damage):
    """#1050 §4.3/§8.4. Detects: a repair that resets `seq` or leaves
    `domain_generation` alone — rows rewritten while a trigger was missing
    would share an identity with the store before them."""
    with _raw(_fresh(ns)) as conn:
        _seed_every_table(conn)
        conn.execute("UPDATE five_hour_blocks SET total_cost_usd = 3.0")
        conn.commit()
        seq = _seq(conn)
        token_before = k.read_claude_content_revision(conn).token
        assert token_before
        if damage == "trigger":
            conn.execute(
                "DROP TRIGGER trg_content_revision_five_hour_blocks_update")
        elif damage == "index":
            conn.execute(f"DROP INDEX {k.USAGE_SNAPSHOT_INDEX}")
        conn.commit()
    if damage == "older_format":
        monkeypatch.setattr(k, "KERNEL_FORMAT_VERSION", 2)
    expected_format = k.KERNEL_FORMAT_VERSION
    with _raw(_db_path()) as conn:
        assert k.classify(conn) == k.STATE_PARTIAL
        rev = k.read_claude_content_revision(conn)
        assert rev.token is None and rev.cycle_basis is None
    conn = ns["open_db"]()
    try:
        assert k.classify(conn) == k.STATE_CURRENT
        assert _singleton(conn) == [(expected_format, 1, seq + 1)]
        rev = k.read_claude_content_revision(conn)
        assert rev.token not in ("", token_before)
        assert rev.cycle_basis is not None, "a repaired domain re-stamps cycles"
        conn.execute("UPDATE five_hour_blocks SET total_cost_usd = 4.0")
        conn.commit()
        assert _seq(conn) == seq + 2, "the repaired trigger fires"
    finally:
        conn.close()


@pytest.mark.parametrize("state, damage", (
    ("unsupported", "UPDATE stats_content_revision SET format_version = 99"),
    ("malformed", "DROP TABLE stats_content_revision_scopes"),
    ("malformed", "DELETE FROM stats_content_revision"),
    ("malformed", "UPDATE stats_content_revision SET seq = 'not-a-number'"),
))
def test_untrusted_stores_are_never_touched(ns, state, damage):
    """#1050 §4.3/§8.4. Detects: an open that reseeds or rewrites a store it
    does not understand — a newer binary's domain, or a damaged one, would be
    given a fresh identity equal to a store it never was."""
    with _raw(_fresh(ns)) as conn:
        conn.execute(damage)
        conn.commit()
    digest = _file_digest(_db_path())
    conn = ns["open_db"]()
    try:
        assert k.classify(conn) == state
        if state == "malformed":
            with pytest.raises(k.StatsContentRevisionUnreadable):
                k.read_claude_content_revision(conn)
        else:
            assert k.read_claude_content_revision(conn) == (
                k.ContentRevision(state, None, None))
    finally:
        conn.close()
    assert _file_digest(_db_path()) == digest


def test_install_contention_leaves_the_store_unchanged(ns):
    """#1050 §4.3/§8.4. Detects: an install that waits on, or writes past,
    another process's stats maintenance or write transaction — an opener
    would stall behind a rebuild, or race it."""
    import _cctally_core
    with _raw(_fresh(ns)) as conn:
        _drop_kernel_objects(conn)
    digest = _file_digest(_db_path())
    lock = os.open(str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
                   os.O_RDWR | os.O_CREAT, 0o600)
    with _raw(_db_path()) as conn:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert k.ensure_stats_content_revision(conn, live=True) == (
                k.STATE_ABSENT)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
        assert _file_digest(_db_path()) == digest, "maintenance contention"
        with _raw(_db_path()) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                assert k.ensure_stats_content_revision(
                    conn, live=False) == k.STATE_ABSENT
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] != 0
            finally:
                writer.rollback()
        assert _file_digest(_db_path()) == digest, "write-lock contention"
        assert k.ensure_stats_content_revision(conn, live=False) == (
            k.STATE_CURRENT)


# ── the reader ────────────────────────────────────────────────────────────

def test_fresh_and_mutated_tokens(ns):
    """#1050 §4.4/§8.3. Detects: a never-mutated store whose token is not
    empty (every fresh store's `data_version` and stamps would change bytes),
    and a tracked mutation that leaves the token unchanged."""
    with _raw(_fresh(ns)) as conn:
        assert k.read_claude_content_revision(conn) == k.ContentRevision(
            k.STATE_CURRENT, "", None)
        _seed_every_table(conn)
        conn.execute("UPDATE weekly_usage_snapshots SET weekly_percent = 1.0")
        conn.commit()
        rev = k.read_claude_content_revision(conn)
        assert rev.state == k.STATE_CURRENT and re.fullmatch(
            r"[0-9a-f]{16}", rev.token)
        assert rev.cycle_basis is None, "seq alone does not move every cycle"
        assert k.revision_leg(rev) == rev.token
        assert k.comparable_identity(rev) == (k.STATE_CURRENT, rev.token)


def test_untrusted_leg_is_a_fresh_nonce_and_identity_is_stable(ns):
    """#1050 §4.4. Detects: an untrusted store publishing a stable leg (a
    stale source would be reused under it) or an unstable comparable identity
    (every build bracket would reject)."""
    with _raw(_fresh(ns)) as conn:
        _drop_kernel_objects(conn)
        first = k.read_claude_content_revision(conn)
        second = k.read_claude_content_revision(conn)
    assert first == second == k.ContentRevision(k.STATE_ABSENT, None, None)
    legs = {k.revision_leg(first) for _ in range(3)}
    assert len(legs) == 3 and all(leg.startswith("?") for leg in legs)
    assert k.comparable_identity(first) == k.comparable_identity(second) == (
        k.STATE_ABSENT,)


def test_the_reader_joins_an_open_transaction_and_otherwise_owns_one(ns):
    """#1050 §4.4. Detects: a reader that commits or rolls back its caller's
    transaction (the detail route's identity, scopes and values would come
    from different snapshots) or leaves its own open (a held SHARED lock)."""
    with _raw(_fresh(ns)) as conn:
        k.read_claude_content_revision(conn)
        assert not conn.in_transaction
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM weekly_usage_snapshots").fetchall()
        k.read_claude_content_revision(conn)
        assert conn.in_transaction
        conn.rollback()


def test_more_than_one_publication_stamp_is_unreadable(ns):
    """#1050 §4.4. Detects: a store carrying two publication identities
    being given one token, which could equal either generation's."""
    with _raw(_fresh(ns)) as conn:
        for path in ("/a.json", "/b.json"):
            conn.execute(
                "INSERT INTO stats_publication_stamp VALUES (?, 'x', 'y')",
                (path,))
        conn.commit()
        with pytest.raises(k.StatsContentRevisionUnreadable):
            k.read_claude_content_revision(conn)


def test_scope_rows_are_strict_when_scoped_and_merged_otherwise(ns):
    """#1050 §4.6. Detects: a scoped history reading another account's
    scopes (its stamps would churn on that account's corrections) or the
    merged history missing `unattributed` ones."""
    with _raw(_fresh(ns)) as conn:
        _block(conn, account="acct-a")
        _block(conn, account="unattributed", key=_BLOCK_KEY + 18000)
        conn.commit()
        conn.execute("UPDATE five_hour_blocks SET total_cost_usd = 2.0")
        conn.commit()
        merged = k.read_scope_rows(conn, account_key=None)
        scoped = k.read_scope_rows(conn, account_key="acct-a")
    assert {r.account_key for r in merged} == {"acct-a", "unattributed"}
    assert [r.account_key for r in scoped] == ["acct-a"]
    assert all(isinstance(r, k.ScopeRow) for r in merged)
    # One UPDATE statement over two rows fires the row trigger twice.
    assert {r.revision for r in merged} == {3, 4}


# ── rebuild and publication ───────────────────────────────────────────────

def test_a_rebuilt_generation_validates_and_carries_the_objects(ns):
    """#1050 §4.3/§8.4. Detects: a rebuild whose scratch lacks the kernel
    objects (or a stale schema fingerprint) failing validation, a published
    generation without the triggers, and a publication that keeps the old
    token (a source certified before the rebuild would be reused after it)."""
    import _cctally_journal as jr
    import _lib_journal as J
    assert set(k.KERNEL_TABLES) <= jr._REBUILD_REQUIRED_TABLES
    assert set(k.KERNEL_TRIGGERS) <= jr._REBUILD_REQUIRED_TRIGGERS
    assert set(k.KERNEL_INDEXES) <= jr._REBUILD_REQUIRED_INDEXES
    jr.append_record(J.make_obs(
        at="2026-01-04T09:00:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 7.0, "source": "statusline",
                 "resets_at": 1767830400,
                 "captured_at": "2026-01-04T09:00:00Z"}))
    jr.run_stats_ingest(mode="authoritative")
    conn = ns["open_db"]()
    try:
        before = k.read_claude_content_revision(conn)
    finally:
        conn.close()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    with _raw(_db_path()) as conn:
        assert k.classify(conn) == k.STATE_CURRENT, "published with the objects"
    conn = ns["open_db"]()
    try:
        after = k.read_claude_content_revision(conn)
        assert after.token not in ("", before.token)
        assert after.cycle_basis is not None
        stamp = conn.execute(
            "SELECT record_path FROM stats_publication_stamp").fetchone()[0]
        assert stamp not in after.cycle_basis, "record_path is hashed"
    finally:
        conn.close()


def test_the_publication_copy_does_not_fire_the_triggers(ns, tmp_path):
    """#1050 §4.3. Detects: a publication whose row copy runs under the
    kernel triggers — every publication would inflate `seq` and rewrite every
    scope, so the copy itself would look like a correction of every cycle."""
    import _cctally_journal as jr
    scratch, dest = tmp_path / "scratch.db", tmp_path / "dest.db"
    for path in (scratch, dest):
        ns["open_db"](_target_path=path).close()
    with _raw(scratch) as conn:
        _seed_every_table(conn)
        conn.execute("UPDATE five_hour_blocks SET total_cost_usd = 5.0")
        conn.commit()
        scratch_state = _singleton(conn), _scopes(conn)
    conn = sqlite3.connect(dest.resolve().as_uri(), uri=True)
    try:
        jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json",
            started_at="2026-11-04T00:00:00Z")
    finally:
        conn.close()
    with _raw(dest) as conn:
        assert (_singleton(conn), _scopes(conn)) == scratch_state
        assert k.classify(conn) == k.STATE_CURRENT
        conn.execute("UPDATE five_hour_blocks SET total_cost_usd = 6.0")
        conn.commit()
        assert _seq(conn) == scratch_state[0][0][2] + 1
