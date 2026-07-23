"""Task 9 — in-place cutover to the epoch-gated stats index (spec §7.1/§8).

A REAL pre-journal install (stats.db at legacy migration head 13, no journal)
must upgrade automatically, in place, with zero loss: on the first new-binary
open, ``open_db`` runs the legacy dispatcher to head 13, then ``run_cutover``
exports every journal-covered row into a ``bootstrap-<ts>.jsonl`` segment,
stamps ``journal_id = b:<table>:<rowid>`` on every exported row, advances the
ingest cursor past the bootstrap, and stamps ``user_version = STATS_INDEX_EPOCH``
— all crash-safe. Thereafter the steady-state open is zero-DDL; the frozen
legacy dispatcher fences old binaries via ``DowngradeDetected``.

These tests cover ITEM 1 (the cutover), ITEM 2 (the epoch gate + registry
freeze), and ITEM 3 (invariant tests): round-trip determinism, no-NULL
survivors, byte-identical reader output pre/post, retry-safety at each crash
point, fencing / version-mismatch, steady-state zero-DDL, and the prod guard.

Isolation mirrors tests/test_rebuild_heal.py: load_script() drops cached
_cctally_* siblings; fresh modules are imported AFTER; redirect_paths pins the
data dir. Cutover runs OUTSIDE CCTALLY_MIGRATION_TEST_MODE, so the epoch gate is
engaged (len(_STATS_MIGRATIONS) == 13).
"""
from __future__ import annotations

import argparse
import sqlite3

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _core():
    import _cctally_core
    return _cctally_core


def _jr():
    import _cctally_journal
    return _cctally_journal


# --- canonical logical dump (spec §10; ORDER BY natural key; drop rowid FKs) ---

_DUMP_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "weekly_credit_floors", "percent_milestones",
    "five_hour_milestones", "budget_milestones", "projected_milestones",
    "project_budget_milestones",
    # quota_alert_arming (§5.3 "state") — folded by natural-key upsert; carries
    # no journal_id, so it never appears in the no-NULL-survivors invariant, but
    # its forward-only activation boundary IS part of the canonical logical dump.
    "quota_alert_arming",
)
_DROP_COLS = {"id", "usage_snapshot_id", "cost_snapshot_id", "reset_event_id",
              "block_id"}


def _table_rows(conn, table, where=""):
    cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c not in _DROP_COLS]
    rows = [tuple(r) for r in conn.execute(
        f"SELECT {', '.join(keep)} FROM {table} {where}")]
    return sorted(rows, key=lambda x: tuple(str(v) for v in x))


def _canonical_dump(conn):
    return {t: _table_rows(conn, t) for t in _DUMP_TABLES}


def _block_map(conn, where=""):
    cols = [d[1] for d in conn.execute("PRAGMA table_info(five_hour_blocks)")]
    keep = [c for c in cols if c not in _DROP_COLS]
    return {
        row[keep.index("five_hour_window_key")]: tuple(row)
        for row in conn.execute(
            f"SELECT {', '.join(keep)} FROM five_hour_blocks {where}")
    }


def _assert_blocks_converge(live, rb):
    """CLOSED blocks are journal-covered (block_close evts) and must match live
    EXACTLY; the rebuild may additionally RE-MATERIALIZE extra projection blocks
    from snapshots whose window key the live cutover's gated backfill never
    materialized (a §5.3 documented projection edge). So require every closed-in-
    live block to exist unchanged in the rebuild, and tolerate rebuild extras."""
    live_closed = _block_map(live, "WHERE is_closed = 1")
    rb_all = _block_map(rb)
    for key, row in live_closed.items():
        assert rb_all.get(key) == row, (
            f"closed block {key} diverged: live={row!r} rb={rb_all.get(key)!r}")
    for child in ("five_hour_block_models", "five_hour_block_projects"):
        keys = ", ".join(str(int(k)) for k in live_closed) or "NULL"
        lr = _table_rows(live, child, f"WHERE five_hour_window_key IN ({keys})")
        rr = _table_rows(rb, child, f"WHERE five_hour_window_key IN ({keys})")
        assert lr == rr, f"{child} for closed blocks diverged: {lr!r} vs {rr!r}"


# --- realistic legacy-head-13 fixture (populated, NO journal, journal_id NULL) --

_JOURNAL_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "five_hour_blocks", "weekly_credit_floors",
    "percent_milestones", "five_hour_milestones", "budget_milestones",
    "projected_milestones", "project_budget_milestones",
)

_WSA = "2026-01-01T00:00:00+00:00"
_WEA = "2026-01-07T23:59:59+00:00"


def _seed_stats(conn, *, with_open_block=False, with_arming=False):
    """Seed one realistic row per journal-covered stats table, with valid FK
    links (percent milestone -> snapshot/cost/reset; 5h milestone -> snapshot/
    block-by-window-key). All journal_id left NULL (pre-cutover)."""
    ex = conn.execute
    # snapshots (rowid 1 = weekly milestone anchor, rowid 2 = 5h milestone anchor)
    ex("INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date, "
       "week_end_date, week_start_at, week_end_at, weekly_percent, page_url, "
       "source, payload_json, five_hour_percent, five_hour_resets_at, "
       "five_hour_window_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       ("2026-01-04T09:00:00Z", "2026-01-01", "2026-01-08", _WSA, _WEA, 40.0,
        None, "statusline", "{}", 55.0, "2026-01-04T14:00:00Z", 111))
    ex("INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date, "
       "week_end_date, week_start_at, week_end_at, weekly_percent, page_url, "
       "source, payload_json, five_hour_percent, five_hour_resets_at, "
       "five_hour_window_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       ("2026-01-04T13:00:00Z", "2026-01-01", "2026-01-08", _WSA, _WEA, 60.0,
        None, "statusline", "{}", 80.0, "2026-01-04T14:00:00Z", 222))
    # cost snapshot (rowid 1)
    ex("INSERT INTO weekly_cost_snapshots (captured_at_utc, week_start_date, "
       "week_end_date, week_start_at, week_end_at, range_start_iso, "
       "range_end_iso, cost_usd, source, mode, project) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
       ("2026-01-04T09:00:00Z", "2026-01-01", "2026-01-08", _WSA, _WEA,
        _WSA, "2026-01-04T09:00:00+00:00", 12.5, "cctally-range-cost", "auto",
        None))
    # week reset event (rowid 1)
    ex("INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
       "new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct) "
       "VALUES (?,?,?,?,?)",
       ("2026-01-04T11:00:00Z", _WEA, "2026-01-14T23:59:59+00:00",
        "2026-01-04T11:00:00+00:00", 40.0))
    # five hour reset event (rowid 1)
    ex("INSERT INTO five_hour_reset_events (detected_at_utc, "
       "five_hour_window_key, prior_percent, post_percent, "
       "effective_reset_at_utc) VALUES (?,?,?,?,?)",
       ("2026-01-04T12:00:00Z", 222, 80.0, 20.0, "2026-01-04T12:00:00+00:00"))
    # closed five hour block (rowid 1) + children
    ex("INSERT INTO five_hour_blocks (five_hour_window_key, five_hour_resets_at, "
       "block_start_at, first_observed_at_utc, last_observed_at_utc, "
       "final_five_hour_percent, seven_day_pct_at_block_start, "
       "seven_day_pct_at_block_end, crossed_seven_day_reset, total_input_tokens, "
       "total_output_tokens, total_cache_create_tokens, total_cache_read_tokens, "
       "total_cost_usd, is_closed, created_at_utc, last_updated_at_utc) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (111, "2026-01-04T14:00:00Z", "2026-01-04T09:00:00Z",
        "2026-01-04T09:00:00Z", "2026-01-04T13:59:00Z", 90.0, 40.0, 60.0, 0,
        1000, 2000, 500, 300, 8.75, 1, "2026-01-04T09:00:00Z",
        "2026-01-04T13:59:00Z"))
    ex("INSERT INTO five_hour_block_models (block_id, five_hour_window_key, "
       "model, input_tokens, output_tokens, cache_create_tokens, "
       "cache_read_tokens, cost_usd, entry_count) VALUES (?,?,?,?,?,?,?,?,?)",
       (1, 111, "claude-opus-4", 1000, 2000, 500, 300, 8.75, 4))
    ex("INSERT INTO five_hour_block_projects (block_id, five_hour_window_key, "
       "project_path, input_tokens, output_tokens, cache_create_tokens, "
       "cache_read_tokens, cost_usd, entry_count) VALUES (?,?,?,?,?,?,?,?,?)",
       (1, 111, "/repo/app", 1000, 2000, 500, 300, 8.75, 4))
    # weekly credit floor (rowid 1)
    ex("INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc, "
       "observed_pre_credit_pct, applied_at_utc) VALUES (?,?,?,?)",
       ("2026-01-01", "2026-01-04T10:00:00+00:00", 46.0,
        "2026-01-04T10:00:05Z"))
    # percent milestone (rowid 1): usage->1, cost->1, reset->1
    ex("INSERT INTO percent_milestones (captured_at_utc, week_start_date, "
       "week_end_date, week_start_at, week_end_at, percent_threshold, "
       "cumulative_cost_usd, marginal_cost_usd, usage_snapshot_id, "
       "cost_snapshot_id, reset_event_id, five_hour_percent_at_crossing, "
       "alerted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
       ("2026-01-04T09:00:00Z", "2026-01-01", "2026-01-08", _WSA, _WEA, 40,
        12.5, 3.0, 1, 1, 1, 55.0, None))
    # 5h milestone (rowid 1): usage->2, block->1 (window key 111), reset->0
    ex("INSERT INTO five_hour_milestones (block_id, five_hour_window_key, "
       "percent_threshold, captured_at_utc, usage_snapshot_id, "
       "block_input_tokens, block_output_tokens, block_cache_create_tokens, "
       "block_cache_read_tokens, block_cost_usd, marginal_cost_usd, "
       "seven_day_pct_at_crossing, reset_event_id) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (1, 111, 80, "2026-01-04T13:00:00Z", 2, 1000, 2000, 500, 300, 8.75,
        1.5, 60.0, 0))
    # budget / projected / project milestones
    ex("INSERT INTO budget_milestones (vendor, period_start_at, period, "
       "threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, "
       "alerted_at) VALUES (?,?,?,?,?,?,?,?,?)",
       ("claude", _WSA, "week", 90, 300.0, 275.0, 91.6,
        "2026-01-04T09:00:00Z", None))
    ex("INSERT INTO projected_milestones (week_start_at, period, metric, "
       "threshold, projected_value, denominator, crossed_at_utc, alerted_at) "
       "VALUES (?,?,?,?,?,?,?,?)",
       (_WSA, "week", "weekly_pct", 90, 95.0, 100.0,
        "2026-01-04T09:00:00Z", None))
    ex("INSERT INTO project_budget_milestones (week_start_at, project_key, "
       "threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, "
       "alerted_at) VALUES (?,?,?,?,?,?,?,?)",
       (_WSA, "/repo/app", 90, 25.0, 24.0, 96.0, "2026-01-04T09:00:00Z", None))
    if with_arming:
        # A quota_alert_arming row — the §5.3 "state" family. Its
        # activated_at_utc is a FORWARD-ONLY alert boundary that must survive
        # cutover→rebuild VERBATIM (not re-armed at `now`). Distinct root key
        # (`root-arm`) so no seeded quota observation / reconcile ever touches
        # it: the reconcile only re-arms identities it builds a history for.
        ex("INSERT INTO quota_alert_arming (source, source_root_key, "
           "logical_limit_key, observed_slot, window_minutes, rule_fingerprint, "
           "activated_at_utc) VALUES (?,?,?,?,?,?,?)",
           ("codex", "root-arm", "5h", "primary", 300, "fp-arm-1",
            "2026-01-04T10:30:00+00:00"))
    if with_open_block:
        ex("INSERT INTO five_hour_blocks (five_hour_window_key, "
           "five_hour_resets_at, block_start_at, first_observed_at_utc, "
           "last_observed_at_utc, final_five_hour_percent, "
           "seven_day_pct_at_block_start, seven_day_pct_at_block_end, "
           "crossed_seven_day_reset, total_input_tokens, total_output_tokens, "
           "total_cache_create_tokens, total_cache_read_tokens, total_cost_usd, "
           "is_closed, created_at_utc, last_updated_at_utc) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (999, "2026-01-04T19:00:00Z", "2026-01-04T14:00:00Z",
            "2026-01-04T14:00:00Z", "2026-01-04T15:00:00Z", 30.0, 60.0, 65.0,
            0, 100, 200, 50, 30, 1.0, 0, "2026-01-04T14:00:00Z",
            "2026-01-04T15:00:00Z"))


def _seed_quota(core):
    """Seed one cache.db quota_window_snapshot (rowid 1) via a RAW connection.

    Deliberately NOT via ``open_cache_db()``: on a fresh cache.db that opener
    runs the migration dispatcher, which arms + fires a Codex re-ingest — a
    heavyweight side effect that also drives a stats reconcile through
    ``open_db``. We only need one quota row for the cutover to export, so we
    apply the cache schema, stamp every migration applied (so any later
    ``open_cache_db`` fast-paths with no re-ingest), and insert the row."""
    import _cctally_db as db
    cache = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        db._apply_cache_schema(cache)
        cache.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        for m in db._CACHE_MIGRATIONS:
            cache.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (?, 't')",
                (m.name,))
        cache.execute(f"PRAGMA user_version = {len(db._CACHE_MIGRATIONS)}")
        cache.execute(
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            "source_path, line_offset, captured_at_utc, observed_slot, "
            "logical_limit_key, limit_id, limit_name, window_minutes, "
            "used_percent, resets_at_utc, plan_type, individual_limit_json, "
            "reached_type, observed_model) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("codex", "root-a", "/x/rollout.jsonl", 10, "2026-01-04T09:00:00Z",
             "primary", "5h", "L1", "5h", 300, 42.0, "2026-01-04T14:00:00Z",
             "pro", None, None, "gpt-5"))
        cache.commit()
    finally:
        cache.close()


def _build_legacy_install(ns, *, with_open_block=False, with_quota=False,
                          with_arming=False):
    """Build a legacy-head-13 stats.db (no journal, journal_id NULL, uv=13)."""
    core = _core()
    conn = core.open_db()  # fresh -> empty cutover to epoch (no bootstrap)
    try:
        _seed_stats(conn, with_open_block=with_open_block, with_arming=with_arming)
        for t in _JOURNAL_TABLES:
            conn.execute(f"UPDATE {t} SET journal_id = NULL")
        conn.execute("DROP TABLE IF EXISTS stats_open_fixups")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()
    if with_quota:
        _seed_quota(core)


# ==========================================================================
# ITEM 2 — registry freeze + epoch constants
# ==========================================================================

def test_stats_registry_is_frozen_at_13(ns):
    import _cctally_db as db
    assert db.STATS_REGISTRY_FROZEN_HEAD == 13
    assert len(db._STATS_MIGRATIONS) == 13, (
        "stats registry must stay frozen at 13 (spec §7.1) — the module-load "
        "assertion in _cctally_db enforces this")


def test_epoch_constants(ns):
    core = _core()
    assert core.STATS_INDEX_EPOCH == 1000
    assert core.LEGACY_STATS_HEAD == 13


# ==========================================================================
# ITEM 1 — the cutover: trigger, export, stamp, epoch
# ==========================================================================

def test_cutover_fires_on_legacy_open_and_stamps_epoch(ns):
    _build_legacy_install(ns)
    core = _core()
    conn = core.open_db()  # uv<=13 -> cutover
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
        # a bootstrap segment now exists
        segs = _jr().list_segments()
        assert any(s.startswith("bootstrap-") for s in segs), segs
    finally:
        conn.close()


def test_no_null_journal_id_survivors(ns):
    # Includes an OPEN block: it is a re-materialized projection, so it stays
    # journal_id NULL (never harvested until it closes) — every OTHER journal-
    # covered row, and every CLOSED block, must carry a stamped journal_id.
    _build_legacy_install(ns, with_open_block=True)
    core = _core()
    conn = core.open_db()
    try:
        for t in (
            "weekly_usage_snapshots", "weekly_cost_snapshots",
            "week_reset_events", "five_hour_reset_events", "weekly_credit_floors",
            "percent_milestones", "five_hour_milestones", "budget_milestones",
            "projected_milestones", "project_budget_milestones",
        ):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE journal_id IS NULL").fetchone()[0]
            assert n == 0, f"{t} has {n} NULL journal_id rows post-cutover"
        # closed blocks stamped, open block(s) left NULL (projection)
        closed_null = conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks "
            "WHERE is_closed = 1 AND journal_id IS NULL").fetchone()[0]
        assert closed_null == 0, "closed blocks must carry journal_id"
        open_null = conn.execute(
            "SELECT COUNT(*) FROM five_hour_blocks "
            "WHERE is_closed = 0 AND journal_id IS NULL").fetchone()[0]
        assert open_null == 1, "the open block stays a NULL-journal_id projection"
    finally:
        conn.close()


def test_bootstrap_ids_are_rowid_stable(ns):
    _build_legacy_install(ns)
    core = _core()
    conn = core.open_db()
    try:
        # every journal_id is the b:<table>:<rowid> cutover scheme
        for t in ("weekly_usage_snapshots", "percent_milestones",
                  "weekly_credit_floors"):
            for row in conn.execute(f"SELECT id, journal_id FROM {t}"):
                assert row["journal_id"] == f"b:{t}:{row['id']}"
    finally:
        conn.close()


# ==========================================================================
# ITEM 3.1 — cutover round-trip: rebuild from the bootstrap reproduces the DB
# ==========================================================================

def test_rebuild_from_bootstrap_reproduces_cutover_db(ns, tmp_path):
    _build_legacy_install(ns)
    core, jr = _core(), _jr()
    live = core.open_db()  # cutover
    live_dump = _canonical_dump(live)

    target = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(target_path=str(target))
    rb = core.open_db(_target_path=str(target))
    try:
        rb_dump = _canonical_dump(rb)
        for tbl in live_dump:
            assert live_dump[tbl] == rb_dump[tbl], (
                f"{tbl} diverged:\nlive={live_dump[tbl]!r}\nrb  ={rb_dump[tbl]!r}")
        _assert_blocks_converge(live, rb)
        # FK remap: the rebuilt percent milestone's usage_snapshot_id resolves to
        # the rebuilt snapshot carrying the equivalent logical id.
        mrow = rb.execute(
            "SELECT usage_snapshot_id FROM percent_milestones").fetchone()
        srow = rb.execute(
            "SELECT journal_id FROM weekly_usage_snapshots WHERE id = ?",
            (mrow[0],)).fetchone()
        assert srow[0] == "b:weekly_usage_snapshots:1", (
            "milestone FK did not remap to the equivalent snapshot")
    finally:
        rb.close()
        live.close()


def test_cutover_rematerializes_quota_from_bootstrap(ns, tmp_path):
    _build_legacy_install(ns, with_quota=True)
    core, jr = _core(), _jr()
    core.open_db().close()  # cutover exports the quota obs into the bootstrap
    import _cctally_cache
    cache = _cctally_cache.open_cache_db()
    try:
        cache.execute("DELETE FROM quota_window_snapshots")
        cache.commit()
    finally:
        cache.close()
    jr.rebuild_stats_index(target_path=str(tmp_path / "rb.db"))
    cache = _cctally_cache.open_cache_db()
    try:
        n = cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source_root_key = 'root-a'").fetchone()[0]
    finally:
        cache.close()
    assert n == 1, "cutover-journaled quota obs must re-materialize the cache"


def test_cutover_arming_boundary_survives_rebuild_verbatim(ns, tmp_path):
    # Task-9 P2 / Task-11 Item 0: the quota_alert_arming activation boundary is a
    # forward-only alert clock that MUST survive cutover → rebuild-from-bootstrap
    # VERBATIM — never re-armed at `now`. quota_alert_arming carries NO journal_id
    # column: the fold applier upserts by natural key, so cutover exports it as a
    # `qaa:` evt STATE record and does NOT stamp it (hence its exclusion from the
    # no-NULL-journal_id-survivors invariant).
    _build_legacy_install(ns, with_arming=True)
    core, jr = _core(), _jr()
    live = core.open_db()  # cutover exports the arming row as a qaa: evt
    try:
        live_boundary = live.execute(
            "SELECT activated_at_utc FROM quota_alert_arming "
            "WHERE source_root_key = 'root-arm'").fetchone()
        assert live_boundary is not None, "cutover dropped the arming row"
        assert live_boundary[0] == "2026-01-04T10:30:00+00:00"
    finally:
        live.close()
    # Rebuild from the bootstrap segment ALONE (no live reconcile) — the boundary
    # must be byte-verbatim, proving replay of the journaled state, not a re-arm.
    target = tmp_path / "rb.db"
    jr.rebuild_stats_index(target_path=str(target))
    rb = core.open_db(_target_path=str(target))
    try:
        row = rb.execute(
            "SELECT activated_at_utc, rule_fingerprint FROM quota_alert_arming "
            "WHERE source_root_key = 'root-arm'").fetchone()
        assert row is not None, "rebuild lost the arming boundary"
        assert row[0] == "2026-01-04T10:30:00+00:00", (
            f"arming boundary re-armed instead of replayed verbatim: {row[0]!r}")
        assert row[1] == "fp-arm-1"
    finally:
        rb.close()


# ==========================================================================
# ITEM 3.3 — byte-identical reader output pre/post cutover
# ==========================================================================

def _percent_breakdown_json(ns, capsys):
    args = argparse.Namespace(week_start=None, week_start_name=None,
                              json=True, tz=None)
    rc = ns["cmd_percent_breakdown"](args)
    assert rc == 0
    return capsys.readouterr().out


def test_reader_output_byte_identical_pre_post_cutover(ns, capsys, monkeypatch):
    import _cctally_store as st
    # A mutable toggle drives the epoch gate WITHOUT monkeypatch.undo() (which
    # would reset the redirect_paths patches back to the real prod dir — the
    # documented gotcha). BEFORE: gate OFF, so open_db reads the uv=13 fixture
    # without cutting over. AFTER: gate ON, so the read cuts over first.
    gate = {"on": False}
    monkeypatch.setattr(st, "stats_epoch_enabled", lambda: gate["on"])
    _build_legacy_install(ns, with_quota=False)  # ONE uv=13 fixture
    before = _percent_breakdown_json(ns, capsys)  # gate OFF -> reads uv=13, no cutover
    gate["on"] = True
    after = _percent_breakdown_json(ns, capsys)   # gate ON -> cuts over the SAME fixture
    assert before == after, "cutover changed reader output"


def _cmd_json(ns, capsys, argv):
    """Drive a reader command through the REAL parser (all defaults populated,
    no fragile hand-built Namespace) and return its --json stdout."""
    parser = ns["build_parser"]()
    args = parser.parse_args(argv)
    rc = args.func(args)
    assert rc == 0, f"{argv} exited {rc}"
    return capsys.readouterr().out


def test_reader_output_byte_identical_pre_post_cutover_broadened(
        ns, capsys, monkeypatch):
    # Task-9 P3 / Task-11 Item 0: broaden the gate-toggle byte-identity proof
    # beyond percent-breakdown to the reader families that read the stats index
    # (report / weekly / five-hour-blocks / budget). CCTALLY_AS_OF pins `now` so
    # the two runs (gate OFF, then ON = cutover) can't drift on wall-clock.
    import _cctally_store as st
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-01-10T00:00:00+00:00")
    gate = {"on": False}
    monkeypatch.setattr(st, "stats_epoch_enabled", lambda: gate["on"])
    _build_legacy_install(ns)  # ONE uv=13 fixture
    commands = (
        ["report", "--json"],
        ["weekly", "--json"],
        ["five-hour-blocks", "--json"],
        ["budget", "--json"],
    )
    before = {tuple(c): _cmd_json(ns, capsys, c) for c in commands}  # gate OFF
    gate["on"] = True
    after = {tuple(c): _cmd_json(ns, capsys, c) for c in commands}   # gate ON = cutover
    for c in commands:
        assert before[tuple(c)] == after[tuple(c)], (
            f"cutover changed reader output for {c[0]}")


# ==========================================================================
# ITEM 3.4 — retry-safety at each crash point
# ==========================================================================

def _legacy_still_functional(core):
    """A crashed cutover leaves uv<=13; the FROZEN legacy dispatcher fast-paths
    it (old binary fully functional)."""
    import _cctally_db as db
    conn = sqlite3.connect(core.DB_PATH)
    try:
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv <= 13, f"crashed cutover left uv={uv} (should stay legacy)"
        db._run_pending_migrations(conn, registry=db._STATS_MIGRATIONS,
                                   db_label="stats.db")  # must not raise
    finally:
        conn.close()


def test_crash_before_rename_retries_clean(ns, monkeypatch):
    _build_legacy_install(ns)
    core, jr = _core(), _jr()
    real_wbs = jr._write_bootstrap_segment
    monkeypatch.setattr(jr, "_write_bootstrap_segment",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("crash before rename")))
    with pytest.raises(RuntimeError):
        core.open_db()
    _legacy_still_functional(core)
    # Restore ONLY the crashed function (never monkeypatch.undo() — that would
    # reset the redirect_paths patches back to the real prod dir, the gotcha).
    monkeypatch.setattr(jr, "_write_bootstrap_segment", real_wbs)
    conn = core.open_db()  # retry succeeds
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
        n = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id IS NULL").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_crash_after_rename_mid_txn_rolls_back_and_retries(ns, monkeypatch):
    _build_legacy_install(ns)
    core, jr = _core(), _jr()
    real_write_cursor = jr._write_cursor

    def boom(*a, **k):
        raise RuntimeError("crash after rename, mid-txn")

    monkeypatch.setattr(jr, "_write_cursor", boom)
    with pytest.raises(RuntimeError):
        core.open_db()
    # the bootstrap file was renamed into place (a harmless orphan), but the
    # journal_id stamps + epoch bump rolled back.
    assert any(s.startswith("bootstrap-") for s in jr.list_segments())
    _legacy_still_functional(core)
    monkeypatch.setattr(jr, "_write_cursor", real_write_cursor)
    conn = core.open_db()  # retry -> a second bootstrap, idempotent
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
        assert conn.execute(
            "SELECT COUNT(*) FROM percent_milestones "
            "WHERE journal_id IS NULL").fetchone()[0] == 0
    finally:
        conn.close()


def test_double_cutover_is_idempotent(ns, tmp_path):
    _build_legacy_install(ns)
    core, jr = _core(), _jr()
    core.open_db().close()  # cutover #1
    _c1 = core.open_db()
    try:
        d1 = _canonical_dump(_c1)
    finally:
        _c1.close()
    # force a SECOND cutover by reverting the epoch stamp (simulates a stray
    # re-trigger); bootstrap ids are stable so the second export is idempotent.
    conn = sqlite3.connect(core.DB_PATH)
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    live = core.open_db()  # cutover #2
    try:
        d2 = _canonical_dump(live)
        assert d1 == d2, "second cutover changed the logical state"
    finally:
        live.close()
    # a rebuild over BOTH bootstrap segments folds idempotently.
    jr.rebuild_stats_index(target_path=str(tmp_path / "rb.db"))
    rb = core.open_db(_target_path=str(tmp_path / "rb.db"))
    try:
        assert _canonical_dump(rb) == d1
    finally:
        rb.close()


# ==========================================================================
# ITEM 3.5 — fencing + version mismatch
# ==========================================================================

def test_old_binary_frozen_dispatcher_fences_epoch_db(ns):
    _build_legacy_install(ns)
    core = _core()
    core.open_db().close()  # cutover -> uv=1000
    import _cctally_db as db
    conn = sqlite3.connect(core.DB_PATH)
    try:
        # The FROZEN legacy registry IS what an OLD binary runs: uv 1000 > 13.
        with pytest.raises(db.DowngradeDetected):
            db._run_pending_migrations(conn, registry=db._STATS_MIGRATIONS,
                                       db_label="stats.db")
    finally:
        conn.close()


def test_epoch_mismatch_with_journal_rebuilds(ns):
    _build_legacy_install(ns)
    core = _core()
    live = core.open_db()  # cutover -> uv=1000, bootstrap exists
    before = _canonical_dump(live)
    live.close()
    # bump to a FUTURE epoch (a newer binary touched it) — a mismatch on THIS
    # binary that must resolve by journal rebuild, NOT by corruption heal.
    conn = sqlite3.connect(core.DB_PATH)
    conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH + 1}")
    conn.commit()
    conn.close()
    healed = core.open_db()  # detects mismatch -> rebuild
    try:
        assert healed.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
        assert _canonical_dump(healed) == before, "rebuild lost data"
    finally:
        healed.close()
    incidents = list((core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1, "the version-ahead DB is preserved in quarantine"


def test_epoch_mismatch_without_journal_hard_errors(ns):
    core = _core()
    core.open_db().close()  # fresh empty install -> uv=1000, NO journal
    # remove any journal dir and bump to a future epoch with no journal to
    # rebuild from -> hard error, never a silent rebuild-to-empty.
    import shutil
    if core.JOURNAL_DIR.exists():
        shutil.rmtree(core.JOURNAL_DIR)
    conn = sqlite3.connect(core.DB_PATH)
    conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH + 1}")
    conn.commit()
    conn.close()
    import _cctally_db as db
    with pytest.raises(db.StatsEpochMismatchError):
        core.open_db()


# ==========================================================================
# ITEM 3.6 — steady-state zero-DDL
# ==========================================================================

def _trace_open(ns, monkeypatch, **kw):
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    conn = ns["open_db"](**kw)
    conn.close()
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    return seen


def test_steady_state_open_runs_zero_ddl(ns, monkeypatch):
    _build_legacy_install(ns)
    core = _core()
    core.open_db().close()  # cutover -> uv=1000 steady state
    seen = _trace_open(ns, monkeypatch)
    offenders = [
        s for s in seen
        if any(tok in s.upper() for tok in (
            "CREATE TABLE", "CREATE INDEX", "ALTER TABLE"))
        or "schema_migrations" in s
        or s.strip().upper().startswith(("INSERT", "UPDATE"))
        or "PRAGMA USER_VERSION =" in s.upper()
    ]
    assert offenders == [], f"steady-state open ran schema work: {offenders}"


# ==========================================================================
# ITEM 3.7 — prod guard: a dev checkout must refuse to cut over a prod DB
# ==========================================================================

def _wire_prod_guard(core, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_real_prod_data_dir", lambda: core.DB_PATH.parent)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)


def test_cutover_refuses_prod_from_dev_checkout(ns, tmp_path, monkeypatch):
    _build_legacy_install(ns, with_quota=False)
    core = _core()
    import _cctally_db as db
    _wire_prod_guard(core, tmp_path, monkeypatch)
    with pytest.raises(db.ProdMigrationRefused):
        core.open_db()
    # DB left at legacy head, untouched
    conn = sqlite3.connect(core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    finally:
        conn.close()


def test_cutover_prod_override_allows(ns, tmp_path, monkeypatch):
    _build_legacy_install(ns, with_quota=False)
    core = _core()
    _wire_prod_guard(core, tmp_path, monkeypatch)
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    conn = core.open_db()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
    finally:
        conn.close()


# ==========================================================================
# db recover --db stats retirement (spec §7.1)
# ==========================================================================

def test_db_recover_stats_is_retired(ns, capsys):
    import _cctally_db as db
    _build_legacy_install(ns, with_quota=False)
    core = _core()
    core.open_db().close()  # uv=1000
    rc = db.cmd_db_recover(argparse.Namespace(db="stats", yes=True))
    err = capsys.readouterr().err
    assert rc == 2
    assert "db rebuild --db stats" in err
    # the DB is untouched by the retired command
    conn = sqlite3.connect(core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            core.STATS_INDEX_EPOCH
    finally:
        conn.close()
