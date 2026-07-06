"""#270 regression suite — the durable `session_entries` mutation signal.

Milestone M1 (schema + ingest + reader helpers):

* Task 1 — the `mutation_seq` / `mutation_min_ts` columns + covering index,
  added to the `CREATE TABLE` DDL AND via `add_column_if_missing` on an
  existing cache.db, placed BEFORE the legacy FTS early-return in
  `_apply_cache_schema`.
* Task 2 — the per-file `cache_meta` counter bump (`_bump_mutation_seq`) and
  the stamped UPSERT inside `sync_cache` (every insert + every WHERE-passing
  in-place UPSERT), with `stats.rows_changed` byte-identical and an idle sync
  never bumping the counter.
* Task 3 — the `_entry_mutation_seq` signature-leg reader and the
  `changed_min_timestamp` change-aware watermark on `_lib_snapshot_cache`.

Tests use `load_script()` + `redirect_paths()` (a temp cache.db under a
fixture HOME) — NOT a HOME-only loader — so they never touch the prod DB
(the `test_loader_home_only_reads_prod_db` gotcha).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _cctally_db  # noqa: E402  (bin/ is on sys.path, see above)
import _lib_snapshot_cache as _sc  # noqa: E402


# Production `session_entries` shape BEFORE #270 — no mutation_seq /
# mutation_min_ts. Used to build a "legacy" cache.db and prove the upgrade
# column-adds land.
_PRE_COLUMN_SESSION_ENTRIES_DDL = """
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    msg_id              TEXT,
    req_id              TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_create_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    usage_extra_json    TEXT,
    cost_usd_raw        REAL,
    speed               TEXT
);
"""


def _table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})").fetchall()}


# ── Task 1: schema ────────────────────────────────────────────────────────


def test_schema_fresh_db_has_mutation_columns_and_index(tmp_path):
    """A fresh cache.db (CREATE TABLE path) carries both columns + the index."""
    conn = sqlite3.connect(tmp_path / "cache.db")
    _cctally_db._apply_cache_schema(conn)
    cols = _table_columns(conn, "session_entries")
    assert "mutation_seq" in cols
    assert "mutation_min_ts" in cols
    assert "idx_entries_mutation_seq" in _index_names(conn, "session_entries")
    conn.close()


def test_schema_upgrades_existing_db_via_add_column(tmp_path):
    """A PRE-column cache.db gains both columns + the covering index when
    reopened through the real schema-apply path (add_column_if_missing)."""
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.executescript(_PRE_COLUMN_SESSION_ENTRIES_DDL)
    conn.commit()
    assert "mutation_seq" not in _table_columns(conn, "session_entries")

    _cctally_db._apply_cache_schema(conn)

    cols = _table_columns(conn, "session_entries")
    assert "mutation_seq" in cols
    assert "mutation_min_ts" in cols
    assert "idx_entries_mutation_seq" in _index_names(conn, "session_entries")
    # Existing-row default semantics (§5): seq 0, min_ts NULL.
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model) "
        "VALUES ('/x.jsonl', 0, '2026-07-01T00:00:00Z', 'claude-opus-4-7')"
    )
    row = conn.execute(
        "SELECT mutation_seq, mutation_min_ts FROM session_entries"
    ).fetchone()
    assert row == (0, None)
    conn.close()


def test_schema_columns_land_before_legacy_fts_early_return(tmp_path):
    """A legacy single-column `conversation_fts` trips the FTS early-return in
    `_apply_cache_schema`; the mutation columns + index must STILL land because
    they precede that return (the
    `_apply_cache_schema_legacy_early_return_before_new_table` gotcha)."""
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.executescript(_PRE_COLUMN_SESSION_ENTRIES_DDL)
    # A plain (non-split) table named conversation_fts => legacy_present=True.
    conn.execute("CREATE TABLE conversation_fts (text)")
    conn.commit()

    _cctally_db._apply_cache_schema(conn)

    cols = _table_columns(conn, "session_entries")
    assert "mutation_seq" in cols, (
        "mutation_seq must be added BEFORE the legacy FTS early-return"
    )
    assert "mutation_min_ts" in cols
    assert "idx_entries_mutation_seq" in _index_names(conn, "session_entries")
    conn.close()


# ── Task 2: ingest (counter bump + stamped UPSERT) ────────────────────────


def _assistant_line(msg_id, req_id, *, out_tokens, ts="2026-06-15T12:00:00Z",
                    speed=None, in_tokens=0):
    """One JSONL assistant entry (mirrors `_iter_jsonl_entries_with_offsets`)."""
    usage = {
        "input_tokens": in_tokens, "output_tokens": out_tokens,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    if speed is not None:
        usage["speed"] = speed
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": req_id,
        "message": {"id": msg_id, "model": "claude-opus-4-7", "usage": usage},
    }) + "\n"


def _counter(conn):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='session_entries_mutation_seq'"
    ).fetchone()
    return int(row[0]) if row else 0


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    """Drive the production `sync_cache` against a temp cache.db under a fixture
    HOME (never the prod DB). Returns (ns, conn, jsonl, sync)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / "sess.jsonl"
    conn = ns["open_cache_db"]()

    def sync():
        return ns["sync_cache"](conn)

    yield ns, conn, jsonl, sync
    try:
        conn.close()
    except Exception:
        pass


def test_fresh_insert_stamps_seq_and_min_ts(ingest):
    """A fresh insert carries mutation_seq >= 1 and mutation_min_ts ==
    timestamp_utc (the insert-path byte-identity anchor: min_ts == event time)."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=100))
    sync()
    seq, min_ts, ts = conn.execute(
        "SELECT mutation_seq, mutation_min_ts, timestamp_utc FROM session_entries"
    ).fetchone()
    assert seq >= 1
    assert min_ts == ts


def test_inplace_finalization_bumps_seq_same_id(ingest):
    """A streaming-intermediate row finalized in place (same msg_id/req_id,
    higher tokens) keeps the SAME id but gets a NEW higher mutation_seq."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=1))
    sync()
    id1, seq1, out1 = conn.execute(
        "SELECT id, mutation_seq, output_tokens FROM session_entries"
    ).fetchone()
    assert out1 == 1
    with jsonl.open("a") as fh:
        fh.write(_assistant_line(
            "m1", "r1", out_tokens=3881, ts="2026-06-15T12:00:05Z",
            speed="standard"))
    sync()
    id2, seq2, out2 = conn.execute(
        "SELECT id, mutation_seq, output_tokens FROM session_entries"
    ).fetchone()
    assert id2 == id1, "an id-stable in-place UPSERT keeps the same id"
    assert out2 == 3881, "the finalization won the dedup contest"
    assert seq2 > seq1, "the WHERE-passing UPSERT re-stamped mutation_seq"


def test_timestamp_moving_finalization_records_min(ingest):
    """A finalization whose new timestamp_utc crosses a day boundary overwrites
    timestamp_utc but mutation_min_ts holds the EARLIEST event time (min(old,new))
    so the closed-bucket watermark reaches the OLD bucket."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=1,
                                     ts="2026-06-15T23:59:00Z"))
    sync()
    early_stored = conn.execute(
        "SELECT timestamp_utc FROM session_entries").fetchone()[0]
    with jsonl.open("a") as fh:
        fh.write(_assistant_line("m1", "r1", out_tokens=3881,
                                 ts="2026-06-16T00:05:00Z"))
    sync()
    ts_now, min_ts = conn.execute(
        "SELECT timestamp_utc, mutation_min_ts FROM session_entries").fetchone()
    assert ts_now != early_stored, "timestamp_utc is overwritten to the later value"
    assert min_ts == early_stored, "mutation_min_ts keeps the earliest event time"


def test_speed_tiebreak_upsert_stamps(ingest):
    """The equal-tokens speed-tiebreak branch is WHERE-passing → it must also
    stamp mutation_seq (Codex-2d — not just strict token increases)."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=100))
    sync()
    seq1 = conn.execute("SELECT mutation_seq FROM session_entries").fetchone()[0]
    with jsonl.open("a") as fh:
        fh.write(_assistant_line("m1", "r1", out_tokens=100, speed="standard"))
    sync()
    seq2, speed = conn.execute(
        "SELECT mutation_seq, speed FROM session_entries").fetchone()
    assert speed == "standard", "the speed-set row won the equal-tokens tiebreak"
    assert seq2 > seq1, "the WHERE-passing speed-tiebreak UPSERT stamped the seq"


def test_idle_sync_does_not_bump_counter(ingest):
    """A genuinely idle sync (no file grows) must NOT bump the counter — the
    `if rows:` block never runs, so the idle-0% invariant holds."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=100))
    sync()
    c1 = _counter(conn)
    assert c1 >= 1
    stats = sync()
    assert _counter(conn) == c1, "an idle sync must not advance the counter"
    assert stats.files_skipped_unchanged >= 1


def test_rows_changed_not_inflated_by_counter_bump(ingest):
    """`stats.rows_changed` stays byte-identical: the per-file counter bump lands
    BEFORE `before = conn.total_changes`, so it is outside the counted window.
    Two fresh inserts => rows_changed == 2 (not 3)."""
    _, conn, jsonl, sync = ingest
    jsonl.write_text(
        _assistant_line("m1", "r1", out_tokens=10)
        + _assistant_line("m2", "r2", out_tokens=20)
    )
    stats = sync()
    assert stats.rows_changed == 2


# ── Carry-forward (M1 review): legacy-row min_ts hardening ────────────────
#
# A row written BEFORE the #270 columns existed is `mutation_seq = 0,
# mutation_min_ts = NULL`. A finalization UPSERT on such a legacy row runs the
# `DO UPDATE SET mutation_min_ts = MIN(<existing>, excluded.timestamp_utc)`; if
# <existing> is the raw NULL column, SQLite scalar `MIN(NULL, x)` returns NULL,
# leaving the row's `mutation_min_ts` NULL — and the `changed_min_timestamp`
# watermark (aggregate MIN, which ignores NULLs) then MISSES the affected
# bucket. The SET clause COALESCEs the pre-update `timestamp_utc` in so both the
# old and the new bucket are reached.


def _make_legacy_row(conn, jsonl, sync, *, ts, msg="m1", req="r1", out=1):
    """Ingest one row, then demote it to the pre-#270 legacy shape
    (mutation_seq=0, mutation_min_ts=NULL) as if written before the columns
    existed. Returns the row id."""
    jsonl.write_text(_assistant_line(msg, req, out_tokens=out, ts=ts))
    sync()
    (row_id,) = conn.execute("SELECT id FROM session_entries").fetchone()
    conn.execute(
        "UPDATE session_entries SET mutation_seq=0, mutation_min_ts=NULL "
        "WHERE id=?",
        (row_id,),
    )
    conn.commit()
    return row_id


def test_legacy_row_upsert_yields_non_null_min_ts(ingest):
    """A legacy row (seq=0, min_ts=NULL) finalized in place must get a NON-NULL
    mutation_min_ts (COALESCE(old timestamp_utc, ...)), which the seq watermark
    then reaches — otherwise MIN(NULL, x)=NULL leaves it stranded."""
    _, conn, jsonl, sync = ingest
    ts = "2026-06-15T12:00:00Z"
    _make_legacy_row(conn, jsonl, sync, ts=ts)
    # `sync_cache` normalizes the stored timestamp_utc (…Z → …+00:00); compare
    # against the stored form, not the raw JSONL input.
    stored_ts = conn.execute(
        "SELECT timestamp_utc FROM session_entries").fetchone()[0]
    # Counter is at 1 (the initial ingest bumped it once).
    last_seen_seq = _counter(conn)
    with jsonl.open("a") as fh:
        fh.write(_assistant_line("m1", "r1", out_tokens=3881, ts=ts,
                                 speed="standard"))
    sync()
    seq, min_ts = conn.execute(
        "SELECT mutation_seq, mutation_min_ts FROM session_entries").fetchone()
    assert seq > last_seen_seq, "the finalization re-stamped the seq"
    assert min_ts is not None, (
        "a legacy row's finalization must not leave mutation_min_ts NULL"
    )
    assert min_ts == stored_ts, "same-timestamp finalization => min(old,new) == old ts"
    # The watermark reaches the row's bucket.
    assert _sc.changed_min_timestamp(conn, last_seen_seq) == _utc(2026, 6, 15, 12)


def test_legacy_row_timestamp_move_records_min(ingest):
    """A timestamp-MOVING finalization of a legacy row records min(old,new) —
    reaching the OLD (earlier) bucket, not just the new one."""
    _, conn, jsonl, sync = ingest
    _make_legacy_row(conn, jsonl, sync, ts="2026-06-15T23:59:00Z", out=1)
    # The normalized stored form of the earlier (old) event time.
    early_stored = conn.execute(
        "SELECT timestamp_utc FROM session_entries").fetchone()[0]
    last_seen_seq = _counter(conn)
    with jsonl.open("a") as fh:
        fh.write(_assistant_line("m1", "r1", out_tokens=3881,
                                 ts="2026-06-16T00:05:00Z"))
    sync()
    ts_now, min_ts = conn.execute(
        "SELECT timestamp_utc, mutation_min_ts FROM session_entries").fetchone()
    assert ts_now != early_stored, "timestamp_utc moved to the later value"
    assert min_ts == early_stored, (
        "mutation_min_ts keeps the EARLIEST event time even for a legacy row"
    )
    assert _sc.changed_min_timestamp(conn, last_seen_seq) == _utc(2026, 6, 15, 23, 59)


# ── Task 3: reader helpers (_entry_mutation_seq + changed_min_timestamp) ───


_SE_SCHEMA_WITH_MUTATION = """
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_create_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    mutation_seq        INTEGER NOT NULL DEFAULT 0,
    mutation_min_ts     TEXT
);
CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _mk_mutation_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SE_SCHEMA_WITH_MUTATION)
    return conn


def _insert_row(conn, *, ts, seq, min_ts=None):
    """Insert one session_entries row. mutation_min_ts defaults to ts (the
    pure-insert invariant: min_ts == timestamp_utc)."""
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, "
        " mutation_seq, mutation_min_ts) "
        "VALUES ('/x.jsonl', 0, ?, 'claude-opus-4-7', ?, ?)",
        (ts, seq, ts if min_ts is None else min_ts),
    )


def _utc(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)


def test_entry_mutation_seq_reads_counter():
    conn = _mk_mutation_conn()
    assert _sc._entry_mutation_seq(conn) == 0  # key absent → 0
    conn.execute(
        "INSERT INTO cache_meta(key, value) "
        "VALUES ('session_entries_mutation_seq', '7')")
    assert _sc._entry_mutation_seq(conn) == 7


def test_entry_mutation_seq_degrades_on_missing_table():
    conn = sqlite3.connect(":memory:")  # no cache_meta at all
    assert _sc._entry_mutation_seq(conn) == 0


def test_changed_min_timestamp_min_over_new_rows():
    conn = _mk_mutation_conn()
    _insert_row(conn, ts="2026-06-15T10:00:00Z", seq=1)
    _insert_row(conn, ts="2026-06-15T08:00:00Z", seq=2)  # earlier ts, higher seq
    _insert_row(conn, ts="2026-06-15T12:00:00Z", seq=3)
    # seq > 1 → rows at 08:00 and 12:00 → MIN = 08:00, as aware UTC.
    assert _sc.changed_min_timestamp(conn, 1) == _utc(2026, 6, 15, 8)


def test_changed_min_timestamp_none_when_no_new_rows():
    conn = _mk_mutation_conn()
    _insert_row(conn, ts="2026-06-15T10:00:00Z", seq=1)
    assert _sc.changed_min_timestamp(conn, 1) is None
    assert _sc.changed_min_timestamp(conn, 5) is None


def test_changed_min_timestamp_degrades_on_missing_table():
    conn = sqlite3.connect(":memory:")  # no session_entries
    assert _sc.changed_min_timestamp(conn, 0) is None


def test_changed_min_timestamp_equals_new_min_timestamp_on_pure_insert():
    """The byte-identity anchor (§7b): on a PURE-INSERT fixture — every row
    carries mutation_min_ts == timestamp_utc and mutation_seq monotone with id
    (seq == id here) — {seq > last} == {id > last}, so changed_min_timestamp
    reduces to new_min_timestamp for every matching last-seen. Includes a
    late-ingest row (new id, OLD timestamp) to exercise the reach-back."""
    conn = _mk_mutation_conn()
    _insert_row(conn, ts="2026-06-15T10:00:00Z", seq=1)   # id 1
    _insert_row(conn, ts="2026-06-15T11:00:00Z", seq=2)   # id 2
    _insert_row(conn, ts="2026-06-14T09:00:00Z", seq=3)   # id 3 late-ingest
    _insert_row(conn, ts="2026-06-15T12:00:00Z", seq=4)   # id 4
    for last in range(0, 5):
        assert (
            _sc.changed_min_timestamp(conn, last)
            == _sc.new_min_timestamp(conn, last)
        ), f"watermarks must agree on pure inserts at last_seen={last}"


# ── Task 4: signature leg (entry_mutation_seq) ────────────────────────────


def test_signature_advances_on_idstable_upsert(ingest):
    """An id-stable in-place finalization UPSERT advances the dispatch signature
    (entry_mutation_seq) even though MAX(session_entries.id) is flat — so the
    dashboard leaves the idle path. This closes the primary #270 hole (§7a)."""
    _, conn, jsonl, sync = ingest
    stats = sqlite3.connect(":memory:")  # stats legs degrade to 0 (missing tables)
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=1))
    sync()
    sig0 = _sc.compute_signature(conn, stats, generation=0)
    assert sig0.entry_mutation_seq >= 1
    # id-stable in-place finalization: same msg/req, higher tokens, same id.
    with jsonl.open("a") as fh:
        fh.write(_assistant_line("m1", "r1", out_tokens=3881,
                                 ts="2026-06-15T12:00:05Z", speed="standard"))
    sync()
    sig1 = _sc.compute_signature(conn, stats, generation=0)
    assert sig1 != sig0, "the signature must move off the idle path"
    assert sig1.max_entry_id == sig0.max_entry_id, "MAX(id) is flat (id-stable)"
    assert sig1.entry_mutation_seq > sig0.entry_mutation_seq, (
        "the entry_mutation_seq leg carried the change"
    )
    stats.close()


def test_signature_flat_on_idle_tick(ingest):
    """A genuinely idle tick (no file grows) keeps the signature flat → idle."""
    _, conn, jsonl, sync = ingest
    stats = sqlite3.connect(":memory:")
    jsonl.write_text(_assistant_line("m1", "r1", out_tokens=100))
    sync()
    sig0 = _sc.compute_signature(conn, stats, generation=0)
    sync()  # idle: no file changed
    sig1 = _sc.compute_signature(conn, stats, generation=0)
    assert sig1 == sig0, "an idle tick must not move the signature"
    stats.close()


# ── Task 6: reconcile trio — id-stable seq gate (max_id FLAT) ──────────────
#
# Direct proof that the weekref / projects-env / Bug-K reconciles evict a
# CLOSED cached window on an id-stable in-place finalization: `max_entry_id`
# stays flat (== the last-seen id) while `mutation_seq` advances, so the OLD
# id gate (`max_entry_id > last_seen`) would NOT fire but the seq gate does.


def _seed_changed_only(conn, *, ts, seq):
    """A row whose mutation_seq is `seq` and mutation_min_ts is `ts` — modelling
    an existing row that was finalized in place (its id is irrelevant to the
    reconcile, which reads only the seq/min_ts columns + the passed watermarks)."""
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, mutation_seq, mutation_min_ts) "
        "VALUES ('/x.jsonl', 0, ?, 'claude-opus-4-8', ?, ?)",
        (ts, seq, ts),
    )
    conn.commit()


def test_reconcile_weekref_idstable_update_evicts():
    """An id-stable finalization inside a cached CLOSED week evicts it — even
    though max_entry_id is FLAT (the seq gate drives it, not the id gate)."""
    conn = _mk_mutation_conn()
    _sc.reset_weekref_cost_state()
    wk = (dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc),
          dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc))
    _sc._WEEKREF_COST_CACHE[_sc._weekref_key(*wk)] = 2.0
    # Last reconcile saw max_id=5, max_seq=1.
    _sc._WEEKREF_COST_LAST_SEEN.update(max_id=5, max_seq=1, reset_sig=(0, 0))
    # An id-stable finalization: a changed row (seq=2 > 1) whose event time lands
    # inside wk. max_entry_id stays 5 (FLAT).
    _seed_changed_only(conn, ts="2026-06-25T10:00:00Z", seq=2)
    _sc.reconcile_weekref_cache(conn, max_entry_id=5, max_mutation_seq=2,
                                reset_sig=(0, 0))
    assert _sc._weekref_key(*wk) not in _sc._WEEKREF_COST_CACHE, (
        "the seq gate must evict the closed week on an id-stable finalization"
    )


def test_reconcile_projects_env_idstable_update_evicts():
    conn = _mk_mutation_conn()
    _sc.reset_projects_env_state()
    wk = _sc.projects_env_week_key(dt.datetime(2026, 6, 22, tzinfo=dt.timezone.utc))
    _sc.projects_env_week_put(wk, {"/p": ("x",)}, 2.0)
    _sc._PROJECTS_ENV_LAST_SEEN.update(max_id=5, max_seq=1, max_wus_id=0,
                                       sf_sig=(0, 0))
    _seed_changed_only(conn, ts="2026-06-25T10:00:00Z", seq=2)  # inside wk
    _sc.reconcile_projects_env_cache(conn, max_entry_id=5, max_mutation_seq=2,
                                     max_wus_id=0, sf_sig=(0, 0))
    assert _sc.projects_env_week_get(wk) is None, (
        "the seq gate must evict the closed week (max_id flat, seq advanced)"
    )


def test_reconcile_bugk_idstable_update_evicts():
    conn = _mk_mutation_conn()
    _sc.reset_bugk_segment_state()
    key = (dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc).isoformat(),
           dt.datetime(2026, 5, 15, tzinfo=dt.timezone.utc).isoformat())
    _sc._BUGK_SEGMENT_CACHE[key] = object()
    _sc._BUGK_SEGMENT_LAST_SEEN.update(max_id=5, max_seq=1, reset_sig=(0, 0))
    # Changed row inside the half-open [05-09, 05-15) segment.
    _seed_changed_only(conn, ts="2026-05-11T09:00:00Z", seq=2)
    _sc.reconcile_bugk_cache(conn, max_entry_id=5, max_mutation_seq=2,
                             reset_sig=(0, 0))
    assert key not in _sc._BUGK_SEGMENT_CACHE, (
        "the seq gate must evict the pre-credit segment (max_id flat, seq up)"
    )


def test_reconcile_bugk_idstable_at_effective_not_evicted():
    """Bug-K keeps its STRICT `>` half-open bound (Codex-BK-5): a changed row
    EXACTLY at `effective` is OUTSIDE [start, effective) → NOT evicted."""
    conn = _mk_mutation_conn()
    _sc.reset_bugk_segment_state()
    eff = dt.datetime(2026, 5, 15, tzinfo=dt.timezone.utc)
    key = (dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc).isoformat(),
           eff.isoformat())
    _sc._BUGK_SEGMENT_CACHE[key] = object()
    _sc._BUGK_SEGMENT_LAST_SEEN.update(max_id=5, max_seq=1, reset_sig=(0, 0))
    _seed_changed_only(conn, ts="2026-05-15T00:00:00Z", seq=2)  # exactly effective
    _sc.reconcile_bugk_cache(conn, max_entry_id=5, max_mutation_seq=2,
                             reset_sig=(0, 0))
    assert key in _sc._BUGK_SEGMENT_CACHE, (
        "a row AT effective is outside the half-open segment → not evicted"
    )


# ── Task 7 (M3): current-bucket accumulators — no double-count (§8) ─────────
#
# The #271 current-bucket accumulators fold the open bucket incrementally. An
# id-stable in-place finalization re-stamps a row's mutation_seq (id flat), so
# the seq-keyed delta now surfaces it — but the fold cannot un-fold the row's
# already-folded stale partial. A finalization that keeps OR increases its
# timestamp sorts AT-OR-AFTER `tail`; the `(ts,id) <= tail` gate catches only the
# AT case, so a LATER-ts finalization would be APPENDED and DOUBLE-COUNTED. §8's
# fix: any delta row that is a PRE-EXISTING row (`id <= reconciled_max_id`)
# forces a cold refold instead of an append. These tests assert the current
# bucket byte-matches a from-scratch fold in BOTH the same-ts and later-ts cases,
# for BOTH accumulators, and that a genuine fresh insert still appends
# incrementally (byte-identical to today).

_NS_T7 = load_script()  # registers sys.modules["cctally"] for the dashboard import
import _cctally_dashboard as _d  # noqa: E402
from _lib_jsonl import UsageEntry  # noqa: E402


def _utc_iso(ts):
    return dt.datetime.fromisoformat(
        ts.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


# ---- Group A `accumulate_current_bucket` ---------------------------------


def _ce(ts, *, inp, model="claude-opus-4-8"):
    """One in-memory current-bucket UsageEntry."""
    return UsageEntry(
        timestamp=_utc_iso(ts), model=model,
        usage={"input_tokens": inp, "output_tokens": 0,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        cost_usd=None, source_path="/p/a.jsonl")


def _group_a_tick(prior, store, *, now, label="2026-07-01", spy=None):
    """Drive ONE `accumulate_current_bucket` tick over an in-memory `store`
    (list of {id, seq, e}). fetch_all folds the whole bucket; fetch_delta yields
    the `(mutation_seq > after_seq OR ts > after_ts)` delta — exactly the SQL
    `iter_entries_with_id` now emits. `spy["all"]` counts cold folds."""
    def fetch_all():
        if spy is not None:
            spy["all"] = spy.get("all", 0) + 1
        rows = sorted(store, key=lambda r: (r["e"].timestamp, r["id"]))
        return [(r["id"], r["e"]) for r in rows]

    def fetch_delta(after_seq, after_ts):
        rows = [r for r in store
                if r["seq"] > after_seq or r["e"].timestamp > after_ts]
        rows.sort(key=lambda r: (r["e"].timestamp, r["id"]))
        return [(r["id"], r["e"]) for r in rows]

    return _sc.accumulate_current_bucket(
        prior, current_label=label, cur_now=_utc_iso(now),
        cur_max_id=max((r["id"] for r in store), default=0),
        cur_max_seq=max((r["seq"] for r in store), default=0),
        fetch_all=fetch_all, fetch_delta=fetch_delta,
        membership=lambda e: True, mode="auto")


@pytest.mark.parametrize("final_ts, case", [
    ("2026-07-01T10:00:00Z", "same-ts"),    # sorts AT tail (existing gate catches)
    ("2026-07-01T11:00:00Z", "later-ts"),   # sorts AFTER tail (§8 id-refold catches)
])
def test_group_a_current_bucket_inplace_finalization_no_double_count(final_ts, case):
    # Fold a streaming partial (id 1, seq 1, 1 token).
    store = [{"id": 1, "seq": 1, "e": _ce("2026-07-01T10:00:00Z", inp=1)}]
    _, prior = _group_a_tick(None, store, now="2026-07-01T10:30:00Z")
    # id-stable in-place finalization: SAME id, bumped seq, higher tokens, `final_ts`.
    store[0] = {"id": 1, "seq": 2, "e": _ce(final_ts, inp=100)}
    warm, _ = _group_a_tick(prior, store, now="2026-07-01T11:30:00Z")
    # From-scratch reference = a fresh cold fold of the post-finalization store.
    ref, _ = _group_a_tick(None, store, now="2026-07-01T11:30:00Z")
    assert warm == ref, f"{case}: warm bucket must byte-match from-scratch"
    assert warm.input_tokens == 100, (
        f"{case}: no double-count (a naïve append would fold 1 + 100 = 101)"
    )


def test_group_a_current_bucket_insert_path_appends_incrementally():
    """A GENUINE new insert (id > reconciled_max_id) still appends incrementally —
    NO cold refold — and byte-matches from-scratch (the insert-path identity)."""
    store = [{"id": 1, "seq": 1, "e": _ce("2026-07-01T10:00:00Z", inp=5)}]
    spy = {}
    _, prior = _group_a_tick(None, store, now="2026-07-01T10:30:00Z", spy=spy)
    assert spy["all"] == 1  # cold seed
    store.append({"id": 2, "seq": 2, "e": _ce("2026-07-01T11:00:00Z", inp=7)})
    warm, _ = _group_a_tick(prior, store, now="2026-07-01T11:30:00Z", spy=spy)
    assert spy["all"] == 1, "a fresh insert must append, NOT cold-refold"
    ref, _ = _group_a_tick(None, store, now="2026-07-01T11:30:00Z")
    assert warm == ref and warm.input_tokens == 12


# ---- projects-envelope `accumulate_projects_current_week` -----------------

_PROJ_WK = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)   # a Monday
_PROJ_WK_END = _PROJ_WK + dt.timedelta(days=7)


def _proj_seed_conn(rows):
    """rows = (id, source_path, ts_iso, model, cost, session_id, project_path, seq).
    Mirrors the production `session_entries`/`session_files` co-located schema
    with the #270 `mutation_seq` column + index (the delta seeks by it)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE session_entries (id INTEGER PRIMARY KEY, source_path TEXT, "
        "timestamp_utc TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cache_create_tokens INTEGER, cache_read_tokens INTEGER, cost_usd_raw REAL, "
        "mutation_seq INTEGER NOT NULL DEFAULT 0, mutation_min_ts TEXT)")
    conn.execute("CREATE INDEX idx_entries_mutation_seq "
                 "ON session_entries(mutation_seq, mutation_min_ts)")
    conn.execute(
        "CREATE TABLE session_files (path TEXT, session_id TEXT, project_path TEXT)")
    files = {}
    for (eid, sp, ts, model, cost, sid, pp, seq) in rows:
        conn.execute(
            "INSERT INTO session_entries (id, source_path, timestamp_utc, model, "
            "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
            "cost_usd_raw, mutation_seq, mutation_min_ts) "
            "VALUES (?,?,?,?,0,0,0,0,?,?,?)", (eid, sp, ts, model, cost, seq, ts))
        files[sp] = (sp, sid, pp)
    for f in files.values():
        conn.execute(
            "INSERT INTO session_files (path, session_id, project_path) VALUES (?,?,?)", f)
    conn.commit()
    return conn


def _proj_tick(conn, spy=None, cw_start=_PROJ_WK, cw_end=_PROJ_WK_END):
    """Drive ONE `accumulate_projects_current_week` tick through the SAME closures
    `_assemble_projects_via_cache` builds (seq-keyed delta). `spy["all"]` counts
    cold folds."""
    d = _d
    resolver_cache: dict = {}
    cur_max_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM session_entries").fetchone()[0]
    cur_max_seq = conn.execute(
        "SELECT COALESCE(MAX(mutation_seq), 0) FROM session_entries").fetchone()[0]

    def _all():
        if spy is not None:
            spy["all"] = spy.get("all", 0) + 1
        return d._aggregate_projects_week_raw(
            conn, week_start=cw_start, week_end=cw_end, resolver_cache=resolver_cache)

    def _delta(after_seq):
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

    return _sc.accumulate_projects_current_week(
        week_key=_sc.projects_env_week_key(cw_start),
        cur_max_id=cur_max_id, cur_max_seq=cur_max_seq,
        fetch_all_raw=_all, fetch_delta_rows=_delta,
        finalize=d._finalize_projects_mut,
        fold=lambda mut, row: d._fold_projects_entry(
            mut, row, resolver_cache=resolver_cache, week_start=cw_start))


def _proj_fresh(conn, cw_start=_PROJ_WK, cw_end=_PROJ_WK_END):
    return _d._aggregate_projects_week(
        conn, week_start=cw_start, week_end=cw_end, resolver_cache={})


@pytest.mark.parametrize("final_ts, case", [
    ("2026-05-19T10:00:00Z", "same-ts"),    # sorts AT tail
    ("2026-05-19T14:00:00Z", "later-ts"),   # sorts AFTER tail — §8 id-refold catches
])
def test_projects_current_week_inplace_finalization_no_double_count(final_ts, case):
    _sc.reset_projects_env_current_state()
    conn = _proj_seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.10, "s1",
         "/repos/foo", 1),
    ])
    _proj_tick(conn)  # cold: fold the streaming partial (cost 0.10)
    # id-stable in-place finalization: SAME id, bumped seq, higher cost, `final_ts`.
    conn.execute(
        "UPDATE session_entries SET mutation_seq = 2, cost_usd_raw = 0.90, "
        "timestamp_utc = ?, mutation_min_ts = MIN(mutation_min_ts, ?) WHERE id = 1",
        (final_ts, final_ts))
    conn.commit()
    warm, warm_total = _proj_tick(conn)
    fresh, fresh_total = _proj_fresh(conn)
    assert warm == fresh, f"{case}: warm buckets must byte-match from-scratch"
    assert abs(warm_total - fresh_total) < 1e-12, f"{case}: week_total must match"
    bp = next(iter(fresh))
    assert abs(fresh[bp].cost_usd - 0.90) < 1e-9, (
        f"{case}: no double-count (a naïve append would fold 0.10 + 0.90 = 1.00)"
    )


def test_projects_current_week_insert_path_appends_incrementally():
    """A GENUINE new insert (id > reconciled_max_id) appends incrementally — NO
    cold refold — and byte-matches from-scratch."""
    _sc.reset_projects_env_current_state()
    conn = _proj_seed_conn([
        (1, "/j/a.jsonl", "2026-05-19T10:00:00Z", "claude-opus-4-8", 0.10, "s1",
         "/repos/foo", 1),
    ])
    spy = {}
    _proj_tick(conn, spy=spy)  # cold seed
    assert spy["all"] == 1
    conn.execute(
        "INSERT INTO session_entries (id, source_path, timestamp_utc, model, "
        "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        "cost_usd_raw, mutation_seq, mutation_min_ts) "
        "VALUES (2, '/j/b.jsonl', '2026-05-19T12:00:00Z', 'claude-opus-4-8', "
        "0, 0, 0, 0, 0.20, 2, '2026-05-19T12:00:00Z')")
    conn.execute(
        "INSERT INTO session_files (path, session_id, project_path) "
        "VALUES ('/j/b.jsonl', 's2', '/repos/bar')")
    conn.commit()
    warm, warm_total = _proj_tick(conn, spy=spy)
    assert spy["all"] == 1, "a fresh insert must append, NOT cold-refold"
    fresh, fresh_total = _proj_fresh(conn)
    assert warm == fresh and abs(warm_total - fresh_total) < 1e-12
