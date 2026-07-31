"""The Codex quota change ledger and its SQLite triggers.

Public issue omrikais/cctally#5. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1.

The ledger is maintained by triggers rather than by writer discipline, and that
is the whole point: the entry commits in the same transaction as the mutation it
describes, so ordinary migration DML and manual repair are captured with no rule
for a future author to remember. Migration 028's ``observed_model`` rewrite
bumps no mutation sequence at all today, and would be caught.

That property is therefore tested ADVERSARIALLY — by mutating
``quota_window_snapshots`` with raw SQL, the shape a migration or a hand repair
takes — rather than assumed from the writer paths.

The triggers record RAW coordinates only. They must never try to compute an
interpreted key: interpretation is population-dependent (the account fold reads
every observation of a window) and cannot be done in SQL. ``test_ledger_records_
raw_coordinates_only`` pins that the recorded values are byte-identical to the
stored columns.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths


ROOT = "root-ledger"
SOURCE_PATH = "/codex/root-ledger/rollout.jsonl"
LIMIT_KEY = "limit-primary"
RESET = "2026-07-31T15:00:00Z"

LEDGER = "quota_window_change_log"

#: Every column that feeds interpretation. Anything missing here is a
#: silent-skip hazard: the mutation commits and the projector never learns the
#: window is dirty.
SEMANTIC_COLUMNS = (
    "source",
    "source_root_key",
    "source_path",
    "line_offset",
    "logical_limit_key",
    "observed_slot",
    "window_minutes",
    "resets_at_utc",
    "canonical_resets_at_utc",
    "captured_at_utc",
    "used_percent",
    "limit_id",
    "limit_name",
    "plan_type",
    "individual_limit_json",
    "reached_type",
    "observed_model",
    "account_key",
)

#: One case per semantic column EXCEPT ``source``, whose two flip directions are
#: behaviourally distinct and get their own test below.
UPDATE_CASES = (
    ("source_root_key", "root-other"),
    ("source_path", "/codex/root-ledger/renamed.jsonl"),
    ("line_offset", 11),
    ("logical_limit_key", "limit-secondary"),
    ("observed_slot", "secondary"),
    ("window_minutes", 10080),
    ("resets_at_utc", "2026-07-31T20:00:00Z"),
    ("canonical_resets_at_utc", "2026-07-31T20:00:00Z"),
    ("captured_at_utc", "2026-07-31T10:30:00Z"),
    ("used_percent", 44.0),
    ("limit_id", "other"),
    ("limit_name", "Other"),
    ("plan_type", "team"),
    ("individual_limit_json", "{}"),
    ("reached_type", "hard"),
    ("observed_model", "gpt-5.3-codex"),
    ("account_key", "acct-1"),
)


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    conn.row_factory = sqlite3.Row
    _seed(conn, line_offset=10, used_percent=11.0)
    _seed(conn, line_offset=20, used_percent=12.0)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _seed(conn, *, line_offset, used_percent, source="codex",
          resets_at=RESET, logical_limit_key=LIMIT_KEY,
          canonical_resets_at=RESET, source_path=SOURCE_PATH):
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) "
        "VALUES (?,?,?,?,'2026-07-31T10:00:00Z','primary',?,'codex',NULL,300,?,"
        "        ?,'pro',NULL,NULL,NULL,NULL,?)",
        (source, ROOT, source_path, line_offset, logical_limit_key,
         used_percent, resets_at, canonical_resets_at),
    )


def ledger_max_seq(conn) -> int:
    row = conn.execute(f"SELECT MAX(seq) FROM {LEDGER}").fetchone()
    return 0 if row[0] is None else int(row[0])


def ledger_rows_after(conn, seq) -> list:
    return list(conn.execute(
        f"SELECT * FROM {LEDGER} WHERE seq > ? ORDER BY seq", (seq,)))


def test_insert_is_ledgered(cache):
    before = ledger_max_seq(cache)
    _seed(cache, line_offset=30, used_percent=13.0)
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert len(rows) == 1
    assert rows[0]["op"] == "insert"
    assert rows[0]["new_source_root_key"] == ROOT
    assert rows[0]["new_canonical_resets_at_utc"] == RESET
    assert rows[0]["old_source_root_key"] is None


def test_delete_is_ledgered(cache):
    before = ledger_max_seq(cache)
    cache.execute(
        "DELETE FROM quota_window_snapshots WHERE line_offset=10")
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert len(rows) == 1
    assert rows[0]["op"] == "delete"
    assert rows[0]["old_source_root_key"] == ROOT
    assert rows[0]["old_canonical_resets_at_utc"] == RESET
    assert rows[0]["new_source_root_key"] is None


def test_raw_sql_mutation_is_ledgered(cache):
    """A migration-shaped raw UPDATE must appear in the ledger."""
    before = ledger_max_seq(cache)
    cache.execute(
        "UPDATE quota_window_snapshots SET observed_model='gpt-5.3-codex-spark' "
        "WHERE source='codex' AND line_offset=10"
    )
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert len(rows) == 1
    assert rows[0]["op"] == "update"


@pytest.mark.parametrize("column,value", UPDATE_CASES)
def test_every_semantic_column_fires_the_update_trigger(cache, column, value):
    """One parametrization per column in the trigger's UPDATE OF list.

    A column omitted from that list is not a loud failure — the UPDATE commits
    and the ledger stays silent, so the projector never re-materializes the
    window. Enumerating them is the only way that omission shows up.
    """
    before = ledger_max_seq(cache)
    cache.execute(
        f"UPDATE quota_window_snapshots SET {column}=? WHERE line_offset=10",
        (value,),
    )
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert len(rows) == 1, f"UPDATE OF {column} did not reach the ledger"
    assert rows[0]["op"] == "update"


def test_the_parametrization_covers_every_semantic_column(cache):
    """The per-column cases plus ``source`` are exactly ``SEMANTIC_COLUMNS``.

    Without this the two lists drift silently: a column added to the trigger and
    to ``SEMANTIC_COLUMNS`` but not to ``UPDATE_CASES`` is never exercised, which
    is how ``source`` went 15-against-16 the first time.
    """
    assert {column for column, _ in UPDATE_CASES} | {"source"} == set(
        SEMANTIC_COLUMNS)


@pytest.mark.parametrize("before,after", [("codex", "claude"), ("claude", "codex")])
def test_a_source_flip_is_ledgered_in_both_directions(cache, before, after):
    """``source`` is the trigger's own scope predicate, so both flips matter.

    ``claude -> codex`` MINTS a Codex observation out of nothing the projector
    has ever seen; ``codex -> claude`` RETIRES one, and the block behind it has
    to be swept. The trigger's ``WHEN OLD.source='codex' OR NEW.source='codex'``
    is what covers both — a predicate on one side alone would silently drop
    whichever direction it omitted.
    """
    _seed(cache, line_offset=90, used_percent=21.0, source=before)
    cache.commit()
    seq = ledger_max_seq(cache)

    cache.execute(
        "UPDATE quota_window_snapshots SET source=? WHERE line_offset=90",
        (after,),
    )
    cache.commit()

    rows = ledger_rows_after(cache, seq)
    assert len(rows) == 1, f"{before} -> {after} did not reach the ledger"
    assert rows[0]["op"] == "update"
    assert rows[0]["old_source_root_key"] == ROOT
    assert rows[0]["new_source_root_key"] == ROOT


def test_semantic_column_list_matches_the_installed_trigger(cache):
    """The trigger's declared UPDATE OF list is exactly SEMANTIC_COLUMNS.

    The parametrized test above proves every listed column fires; this proves
    nothing was quietly added that the list does not describe, so the two
    together pin the set in both directions.
    """
    sql = cache.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        ("trg_qws_ledger_upd",),
    ).fetchone()[0]
    head = sql.split(" ON ")[0]
    declared = [
        token.strip().strip(",()")
        for token in head.split("UPDATE OF", 1)[1].split(",")
    ]
    assert [name for name in declared if name] == list(SEMANTIC_COLUMNS)


def test_non_semantic_update_produces_nothing(cache):
    """``id`` is the AUTOINCREMENT surrogate key and the only non-semantic column.

    Nothing in the interpretation path reads it: the loader does not select it,
    no identity carries it, and the projector's group coordinates are built from
    the physical columns. A rewrite of it alone must therefore stay silent — the
    trigger fires on the statement's SET list, so listing it would append a
    ledger entry for a window that did not change.

    Every OTHER column is semantic and is covered by ``UPDATE_CASES`` above.
    This test is what keeps the ledger from becoming "any write dirties
    everything"; it is not a licence to leave a real column out.
    """
    before = ledger_max_seq(cache)
    cache.execute(
        "UPDATE quota_window_snapshots SET id=9001 WHERE line_offset=10")
    cache.commit()

    assert ledger_rows_after(cache, before) == []


def test_rolled_back_transaction_leaves_no_ledger_row(cache):
    before = ledger_max_seq(cache)
    cache.execute("BEGIN")
    _seed(cache, line_offset=40, used_percent=14.0)
    cache.execute(
        "UPDATE quota_window_snapshots SET used_percent=99.0 "
        "WHERE line_offset=10")
    cache.rollback()

    assert ledger_rows_after(cache, before) == []
    assert ledger_max_seq(cache) == before


def test_claude_rows_never_accumulate_entries(cache):
    before = ledger_max_seq(cache)
    _seed(cache, line_offset=50, used_percent=15.0, source="claude")
    cache.execute(
        "UPDATE quota_window_snapshots SET used_percent=16.0 "
        "WHERE source='claude'")
    cache.execute("DELETE FROM quota_window_snapshots WHERE source='claude'")
    cache.commit()

    assert ledger_rows_after(cache, before) == []


def test_an_ignored_insert_produces_nothing(cache):
    """An idempotent re-ingest must not flood the ledger.

    ``INSERT OR IGNORE`` on an existing physical key inserts no row, so the
    AFTER INSERT trigger does not fire.
    """
    before = ledger_max_seq(cache)
    cache.execute(
        "INSERT OR IGNORE INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, window_minutes, used_percent, "
        " resets_at_utc) "
        "VALUES ('codex',?,?,10,'2026-07-31T10:00:00Z','primary',?,300,11.0,?)",
        (ROOT, SOURCE_PATH, LIMIT_KEY, RESET),
    )
    cache.commit()

    assert ledger_rows_after(cache, before) == []


def test_a_group_move_records_both_old_and_new_coordinates(cache):
    """A semantic UPDATE can move rows between physical groups.

    The pass must re-materialize the NEW group and sweep the OLD one, so one
    entry has to carry both — a group that has lost all its members is swept to
    nothing, and that only works if the sweep knows where the rows came from.
    """
    before = ledger_max_seq(cache)
    moved = "2026-08-01T15:00:00Z"
    cache.execute(
        "UPDATE quota_window_snapshots "
        "SET resets_at_utc=?, canonical_resets_at_utc=? WHERE line_offset=10",
        (moved, moved),
    )
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert len(rows) == 1
    assert rows[0]["old_canonical_resets_at_utc"] == RESET
    assert rows[0]["new_canonical_resets_at_utc"] == moved
    assert rows[0]["old_source_root_key"] == rows[0]["new_source_root_key"] == ROOT


def test_ledger_records_raw_coordinates_only(cache):
    """The recorded coordinates are the stored columns, verbatim.

    An interpreted key would have to snap ``window_minutes``, rewrite
    ``logical_limit_key`` from ``observed_model``, and resolve the account fold
    over the window's whole population — none of which SQL can do. Storing raw
    coordinates and letting the Python read path interpret them is the entire
    mechanism, so a trigger that started deriving anything would break it.
    """
    before = ledger_max_seq(cache)
    _seed(
        cache, line_offset=60, used_percent=17.0,
        # 10081 is the provider's real weekly jitter, which the READ path snaps
        # to 10080. The ledger must keep the stored 10081.
        logical_limit_key='{"windowMinutes":10081}',
        resets_at="2026-08-02T15:00:03Z",
        canonical_resets_at="2026-08-02T15:00:00Z",
    )
    cache.execute(
        "UPDATE quota_window_snapshots SET window_minutes=10081 "
        "WHERE line_offset=60")
    cache.commit()

    rows = ledger_rows_after(cache, before)
    assert [row["op"] for row in rows] == ["insert", "update"]
    assert rows[1]["new_window_minutes"] == 10081
    assert rows[1]["new_logical_limit_key"] == '{"windowMinutes":10081}'
    assert rows[1]["new_resets_at_utc"] == "2026-08-02T15:00:03Z"
    assert rows[1]["new_canonical_resets_at_utc"] == "2026-08-02T15:00:00Z"


def test_seq_is_monotonic_and_never_reused(cache):
    """The watermark depends on it.

    ``AUTOINCREMENT`` (not a bare INTEGER PRIMARY KEY) is what guarantees a
    deleted high row's seq is never handed out again — without it a pruned
    ledger could reissue a seq at or below the watermark and the entry would be
    skipped forever.
    """
    _seed(cache, line_offset=70, used_percent=18.0)
    cache.commit()
    high = ledger_max_seq(cache)
    cache.execute(f"DELETE FROM {LEDGER}")
    cache.commit()

    _seed(cache, line_offset=80, used_percent=19.0)
    cache.commit()

    assert ledger_max_seq(cache) > high


def test_ledger_survives_a_second_schema_apply(tmp_path, monkeypatch):
    """``_apply_cache_schema`` runs on every non-current open; re-running it
    must not drop, duplicate or reset the ledger."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    db = importlib.import_module("_cctally_db")
    conn = ns["open_cache_db"]()
    try:
        _seed(conn, line_offset=10, used_percent=11.0)
        conn.commit()
        before = list(conn.execute(f"SELECT * FROM {LEDGER} ORDER BY seq"))
        db._apply_cache_schema(conn)
        after = list(conn.execute(f"SELECT * FROM {LEDGER} ORDER BY seq"))
        triggers = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_qws_ledger%' ORDER BY name")
        ]
    finally:
        conn.close()

    assert len(before) == 1
    assert after == before
    assert triggers == [
        "trg_qws_ledger_del", "trg_qws_ledger_ins", "trg_qws_ledger_upd"]
