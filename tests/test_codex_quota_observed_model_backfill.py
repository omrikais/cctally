"""``quota_window_snapshots`` becomes the complete dependency set.

Public issue omrikais/cctally#5. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1, "The cross-table dependency is eliminated, not ledgered".

The change ledger records mutations of ``quota_window_snapshots``. That is only
sufficient if nothing OUTSIDE that table can change how a window is interpreted
— and until now something could: when ``observed_model`` was NULL the loader
fell back to the nearest preceding ``codex_session_entries.model`` at or before
the snapshot's ``line_offset``. An accounting row appearing later could
therefore change a window's model pool with no quota-row mutation to observe.

Rather than extend the triggers to a second table, the dependency is removed:
a one-time migration backfills ``observed_model`` using the EXACT read-time
expression, ingest already stamps the same value forward, and the fallback is
deleted. The equivalence proof is the point of this module, so it runs against
a frozen copy of the pre-change expression rather than against whatever the
loader does today.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

from conftest import load_script, redirect_paths


MIGRATION = "039_codex_quota_observed_model_backfill"

#: A real Codex rollout: ``session_meta`` + ``turn_context`` + the
#: ``token_count`` event that carries BOTH ``last_token_usage`` (accounting) and
#: ``rate_limits`` (quota). Ingesting the first three records is the smallest
#: end-to-end path that emits a quota row at all.
_MODERN_FULL_LINES = [
    line for line in (
        pathlib.Path(__file__).resolve().parent / "fixtures" / "codex-parity"
        / "v1" / "rollouts" / "modern-full.jsonl"
    ).read_text().splitlines() if line.strip()
]
ROOT = "root-model"
FILE_A = "/codex/root-model/a.jsonl"
FILE_B = "/codex/root-model/b.jsonl"
FILE_C = "/codex/root-model/c.jsonl"

#: The one model family ``codex_model_scoped_quota_pool`` treats as its own
#: allowance, and therefore the only one whose resolution visibly rewrites the
#: interpreted ``logical_limit_key``. A test that used an ordinary model would
#: pass whether or not the fallback ran.
SPARK = "gpt-5.3-codex-spark"

#: A FROZEN, verbatim copy of the ``entry_lookup`` correlated subquery that
#: ``_cctally_quota.load_codex_quota_observations`` used before the fallback was
#: removed. Frozen on purpose: an equivalence test that read the expression back
#: out of the code it is validating would prove nothing.
LEGACY_FALLBACK_SQL = (
    "SELECT id, COALESCE(observed_model, "
    "(SELECT entries.model FROM codex_session_entries AS entries "
    "WHERE entries.source_path=quota_window_snapshots.source_path "
    "AND entries.line_offset<=quota_window_snapshots.line_offset "
    "ORDER BY entries.line_offset DESC LIMIT 1)) "
    "FROM quota_window_snapshots WHERE source='codex' ORDER BY id"
)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    db = importlib.import_module("_cctally_db")
    return ns, quota, db


def _handler(db):
    for migration in db._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _entry(conn, *, source_path, line_offset, model):
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        " input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, source_root_key) "
        "VALUES (?,?,'2026-07-31T09:00:00Z','sess',?,1,0,1,0,1,?)",
        (source_path, line_offset, model, ROOT),
    )


def _quota(conn, *, source_path, line_offset, observed_model=None,
           resets_at="2026-07-31T15:00:00Z"):
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) "
        "VALUES ('codex',?,?,?,'2026-07-31T10:00:00Z','primary',"
        "        'limit-primary','codex',NULL,300,11.0,?,'pro',NULL,NULL,?,"
        "        NULL,?)",
        (ROOT, source_path, line_offset, resets_at, observed_model, resets_at),
    )


def _seed_mixed_store(conn):
    """Every shape the fallback can encounter, in one store.

    Each row exists because it is a way the backfill could be wrong: resolving
    across files, picking the wrong preceding entry, resolving from a LATER
    entry, or overwriting an existing stamp.
    """
    conn.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?)",
        (ROOT, "/codex/root-model", "2026-07-31T09:00:00Z",
         "2026-07-31T12:00:00Z"),
    )
    _entry(conn, source_path=FILE_A, line_offset=10, model="gpt-5")
    _entry(conn, source_path=FILE_A, line_offset=30, model="gpt-5.3-codex")
    _entry(conn, source_path=FILE_B, line_offset=5, model=SPARK)
    _entry(conn, source_path=FILE_C, line_offset=10, model=SPARK)

    # No preceding entry in its own file -> stays NULL.
    _quota(conn, source_path=FILE_A, line_offset=5)
    # Exactly one preceding entry.
    _quota(conn, source_path=FILE_A, line_offset=20)
    # Two preceding entries -> the NEAREST one wins, not the first.
    _quota(conn, source_path=FILE_A, line_offset=40)
    # Same offset as an entry -> `<=`, so it resolves.
    _quota(conn, source_path=FILE_A, line_offset=30)
    # A different file's entry must never leak in: file B has a model at
    # offset 5, and this row would resolve to it if the join dropped the
    # source_path predicate.
    _quota(conn, source_path=FILE_B, line_offset=1)
    # Already stamped -> the stamp wins over any preceding entry.
    _quota(conn, source_path=FILE_A, line_offset=50,
           observed_model="gpt-5.5")
    # The one row whose resolution is VISIBLE in the interpreted key: a Spark
    # entry precedes it, so the fallback would rewrite its logical_limit_key
    # with a modelPool member.
    _quota(conn, source_path=FILE_C, line_offset=20)
    conn.commit()


def _legacy_resolution(conn):
    return list(conn.execute(LEGACY_FALLBACK_SQL))


def _stored_models(conn):
    return list(conn.execute(
        "SELECT id, observed_model FROM quota_window_snapshots "
        "WHERE source='codex' ORDER BY id"))


def test_backfill_matches_the_read_time_fallback_exactly(tmp_path, monkeypatch):
    """The whole safety argument: reading the column alone after the backfill
    must give the same answer the COALESCE gave before it."""
    ns, _quota_mod, db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        expected = _legacy_resolution(conn)
        # Non-vacuity: at least one row must actually be resolved BY the
        # fallback, or the equivalence holds trivially.
        assert sum(
            1 for (row_id, model), (_, stored) in zip(expected, _stored_models(conn))
            if model is not None and stored is None
        ) >= 3
        _handler(db)(conn)
        assert _stored_models(conn) == expected
    finally:
        conn.close()


def test_a_row_with_no_determinable_model_stays_null(tmp_path, monkeypatch):
    """Do not fabricate a value. NULL reads as unscoped, which is exactly
    today's behaviour when both sources are NULL."""
    ns, _quota_mod, db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        _handler(db)(conn)
        unresolved = conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source_path=? AND line_offset=5", (FILE_A,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert unresolved is None


def test_an_existing_stamp_is_never_overwritten(tmp_path, monkeypatch):
    ns, _quota_mod, db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        _handler(db)(conn)
        stamped = conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source_path=? AND line_offset=50", (FILE_A,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert stamped == "gpt-5.5"


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    ns, _quota_mod, db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        handler = _handler(db)
        handler(conn)
        once = _stored_models(conn)
        handler(conn)
        assert _stored_models(conn) == once
    finally:
        conn.close()


def test_the_backfill_is_ledgered(tmp_path, monkeypatch):
    """A migration's DML is captured with no rule for its author to remember.

    Migration 028 rewrote ``observed_model`` and bumped no mutation sequence at
    all, staling the projection certificate silently. Under the ledger the same
    rewrite dirties its windows automatically.
    """
    ns, _quota_mod, db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        stored_before = _stored_models(conn)
        seq_before = conn.execute(
            "SELECT COALESCE(MAX(seq),0) FROM quota_window_change_log"
        ).fetchone()[0]
        _handler(db)(conn)
        ops = [
            row[0] for row in conn.execute(
                "SELECT op FROM quota_window_change_log WHERE seq > ?",
                (seq_before,))
        ]
        stored_after = _stored_models(conn)
        # A re-run must append nothing: an unresolvable row written NULL to NULL
        # would fire the trigger and dirty a window on every markerless retry.
        _handler(db)(conn)
        ops_after_rerun = conn.execute(
            "SELECT COUNT(*) FROM quota_window_change_log WHERE seq > ?",
            (seq_before,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert ops and set(ops) == {"update"}
    changed = sum(
        1 for (_, before_model), (_, after_model)
        in zip(stored_before, stored_after)
        if before_model != after_model
    )
    assert len(ops) == changed, (
        "the backfill ledgered rows whose model it did not change")
    assert ops_after_rerun == len(ops)


def test_loader_no_longer_reads_codex_session_entries(tmp_path, monkeypatch):
    """The dependency is removed, not merely unused.

    A quota row with NULL ``observed_model`` sitting after an accounting entry
    that names a model-scoped pool used to inherit that pool at read time. It
    must now read as unscoped — otherwise an accounting row could still change
    a window's interpretation with no quota-row mutation for the ledger to
    record.
    """
    ns, quota, _db = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _seed_mixed_store(conn)
        observations = quota.load_codex_quota_observations(
            source_root_keys={ROOT}, cache_conn=conn)
    finally:
        conn.close()

    keys = {
        observation.identity.logical_limit_key
        for observation in observations
        if observation.source_path == FILE_C
        and observation.line_offset == 20
    }
    assert keys == {"limit-primary"}, (
        "the loader still resolved a model from codex_session_entries")


def test_ingest_stamps_observed_model_so_new_rows_never_need_a_fallback(
    tmp_path, monkeypatch
):
    """Guard on the forward half of the argument.

    Removing the fallback is only safe because the fused ingest already writes
    the sticky model onto every quota row it emits. A change that stopped doing
    so would silently unscope every future model-pool window, so the write is
    pinned by INGESTING a rollout and reading the stored column — not by
    asserting that a substring appears in the writer's SQL, which is satisfied
    just as well by binding ``None`` to it forever.
    """
    ns, _quota_mod, _db = _load(tmp_path, monkeypatch)
    home = tmp_path / "codex-home"
    rollout = home / "sessions" / "2026" / "07" / "14" / "rollout-stamp.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("\n".join(_MODERN_FULL_LINES[:3]) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(home))

    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        stamped = conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source='codex'"
        ).fetchall()
    finally:
        conn.close()

    assert stamped, "the rollout produced no quota observation to check"
    assert all(row[0] for row in stamped), (
        f"ingest wrote a quota row with no observed_model: {stamped!r}")


def test_a_fresh_cache_that_skipped_the_migration_still_resolves_the_pool(
    tmp_path, monkeypatch
):
    """#373's Spark guarantee must not depend on migration 039 having run.

    Two supported paths skip it: ``cctally db skip 039_…``, and a fresh cache
    repopulated from the journal — a fresh install fast-stamps every migration
    handler without invoking it, and the journal cache leg re-materializes rows
    carrying whatever ``observed_model`` was journaled, which is NULL for
    anything captured before the column existed. Either way the row lands
    unstamped, and with the read-time fallback gone a Spark window would be
    filed as ordinary account weekly quota.

    ``sync_codex_cache`` therefore runs the same resolution on every Codex sync.
    """
    ns, quota, _db = _load(tmp_path, monkeypatch)
    home = tmp_path / "codex-home"
    rollout = home / "sessions" / "2026" / "07" / "14" / "rollout-spark.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(_spark_rollout())
    monkeypatch.setenv("CODEX_HOME", str(home))

    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        # Reproduce the unstamped shape by hand. The rows are real, ingested
        # rows against a real rollout — the synthetic paths a hand-seeded store
        # would use are pruned as orphans by the very sync under test. Clearing
        # the column is exactly what a journal-repopulated cache carries when the
        # journalled obs predates the column, and what a `db skip 039_…` install
        # is left with.
        conn.execute(
            "UPDATE quota_window_snapshots SET observed_model=NULL "
            "WHERE source='codex'")
        conn.commit()
        before = _codex_models(conn)
        seq_before = _mutation_seq(conn)

        ns["sync_codex_cache"](conn)

        after = _codex_models(conn)
        seq_after = _mutation_seq(conn)
        observations = quota.load_codex_quota_observations(cache_conn=conn)
    finally:
        conn.close()

    assert before and set(before) == {None}
    assert set(after) == {SPARK}
    assert seq_after > seq_before, (
        "a resolution that changes interpretation must advance the physical "
        "mutation sequence, or the projection certificate still reads as "
        "current and the reconcile short-circuits past the ledger entries")
    pools = {
        observation.identity.logical_limit_key
        for observation in observations
    }
    assert pools and all("modelPool" in key for key in pools), (
        f"the Spark pool was not resolved after the sync: {pools!r}")


def _spark_rollout() -> str:
    """A minimal real rollout whose sticky model is the Spark pool.

    Built from the parity corpus's ``modern-full`` records so it stays a shape
    the fused reader genuinely accepts, with only the model swapped.
    """
    import json as _json

    meta, turn, tokens = (
        _json.loads(line) for line in _MODERN_FULL_LINES[:3])
    meta["payload"]["model"] = SPARK
    turn["payload"]["model"] = SPARK
    return "".join(
        _json.dumps(record) + "\n" for record in (meta, turn, tokens))


def _codex_models(conn) -> list:
    return [
        row[0] for row in conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source='codex' ORDER BY id")
    ]


def _mutation_seq(conn) -> int:
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_physical_mutation_seq'"
    ).fetchone()
    return 0 if row is None else int(row[0])
