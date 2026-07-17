"""Per-migration goldens for cache migration ``022_index_conversation_messages_model``
(#301 — the partial covering index on ``conversation_messages(model, session_id)``).

Loads ``tests/fixtures/migrations/per-migration/022_index_conversation_messages_model/pre.sqlite``
(an existing install at the 021 head with
``idx_conversation_messages_model_session`` absent and a handful of seeded
``conversation_messages`` rows spanning multiple model families), runs the
production 022 handler against a copy, and asserts the index is created and the
three model queries flip from an index→heap walk to index-only walks/seeks. The
committed ``post.sqlite`` is the golden.

The 021 template's "index name appears anywhere in EQP" check is TOO WEAK for
#301 (Codex P1): a hardcoded augmented query would pass even if production
``_model_clause`` were left unchanged, and a wrong index shape
(``(session_id, model)`` or non-partial) could still satisfy a "name present"
assertion. So this module strengthens every axis:

  * index-SHAPE assertions (PRAGMA index_info / index_list / sqlite_master.sql),
    not just name-present;
  * the Q3 plan is derived from the SQL fragment PRODUCTION ``_model_clause``
    returns (not a hardcoded string) so the test cannot pass with an unfixed
    production;
  * a paired NEGATIVE check — the un-augmented Q3 does not use the partial index
    even when it exists — proving the augmentation is load-bearing;
  * an ANALYZED-state check — the ``INDEXED BY`` pin keeps Q3 covering after an
    ``ANALYZE`` (the state where the planner would otherwise regress to a heap
    walk).

022 is a pure index add (no data work): a single ``CREATE INDEX IF NOT EXISTS``.
The dispatcher central-stamps the marker (#140); the handler does NOT self-stamp.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "022_index_conversation_messages_model"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "022_index_conversation_messages_model"
_PRIOR = "021_index_conversation_messages_cwd"
_INDEX = "idx_conversation_messages_model_session"

# Q1 (facets) + Q2 (distinct-model): the exact production shapes from
# list_conversation_facets / _model_clause. Both match the partial WHERE so the
# DISTINCT is answered index-only with no temp b-tree.
_Q1_FACETS = (
    "SELECT DISTINCT session_id, model FROM conversation_messages "
    "WHERE session_id IS NOT NULL AND model IS NOT NULL AND model != ''"
)
_Q2_DISTINCT_MODEL = (
    "SELECT DISTINCT model FROM conversation_messages "
    "WHERE model IS NOT NULL AND model != ''"
)
# The pre-#301 (un-augmented) Q3 shape: no explicit partial predicate, no
# INDEXED BY. Used as the paired NEGATIVE lever — even with the index present
# SQLite does not pick it, proving the production augmentation is load-bearing.
_Q3_UNAUGMENTED = (
    "SELECT session_id FROM conversation_messages "
    "WHERE session_id IS NOT NULL AND model IN (?)"
)


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def convq_module():
    """Load ``bin/_lib_conversation_query.py`` standalone so the test derives Q3
    from the REAL production ``_model_clause`` (Codex P1: the plan guard must be
    tied to production, not a hardcoded string)."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = ilu.spec_from_file_location(
        "_lib_conversation_query", BIN_DIR / "_lib_conversation_query.py"
    )
    mod = ilu.module_from_spec(spec)
    sys.modules["_lib_conversation_query"] = mod
    spec.loader.exec_module(mod)
    return mod


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _marker_count(conn, name):
    return conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (name,)
    ).fetchone()[0]


def _plan_text(conn, sql, params=()):
    """The concatenated EXPLAIN QUERY PLAN detail text for *sql* (all cells of
    all rows joined), so a substring check spans every plan node."""
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " || ".join(str(cell) for row in rows for cell in row)


def _prod_q3(convq_module, conn, families=("opus",)):
    """Derive the production Q3 EQP inputs from ``_model_clause`` (the augmented
    ``session_id IN (...)`` fragment) — NOT a hardcoded string."""
    clause_sql, params = convq_module._model_clause(conn, list(families))
    return "SELECT 1 FROM conversation_messages WHERE " + clause_sql, params


# ── pre / post sanity + RED levers ───────────────────────────────────────────

def test_pre_lacks_index_and_022_marker(cctally_module, convq_module):
    """RED lever: pre.sqlite is at the 021 head (021 marker present, NOT the 022
    marker), LACKS the model index, and NONE of Q1/Q2/Q3's plan names the model
    index. NOT asserting a literal SCAN — pre-index Q1/Q3 legitimately use the
    session-first indexes (idx_conv_session_ts / idx_conv_session_uuid), so
    "SCAN" would be a false discriminator (Codex P1)."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert _marker_count(conn, _PRIOR) == 1
        assert _marker_count(conn, _MIGRATION) == 0
        assert _INDEX not in _indexes(conn), "pre must lack the model index"
        # Seeded rows exist so the DISTINCTs are meaningful (non-vacuous plans).
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] >= 4
        assert _INDEX not in _plan_text(conn, _Q1_FACETS)
        assert _INDEX not in _plan_text(conn, _Q2_DISTINCT_MODEL)
        # Un-augmented Q3 (a valid pre-#301 shape that does not reference the
        # index) must not name it either.
        assert _INDEX not in _plan_text(
            conn, _Q3_UNAUGMENTED, ("claude-opus-4-8",))
        # And the PRODUCTION Q3 pins the index via INDEXED BY, so preparing its
        # plan on an index-absent DB RAISES — extra proof production pins it.
        prod_sql, prod_params = _prod_q3(convq_module, conn)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("EXPLAIN QUERY PLAN " + prod_sql, prod_params)
    finally:
        conn.close()


def test_post_has_index_and_marker(cctally_module):
    """Sanity: post.sqlite has the 022 marker stamped and the index present."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _marker_count(conn, _MIGRATION) == 1
        assert _INDEX in _indexes(conn)
    finally:
        conn.close()


# ── index SHAPE (not just name-present) ──────────────────────────────────────

def test_index_shape_is_partial_model_session(cctally_module):
    """The index columns are exactly ``(model, session_id)`` IN THAT ORDER, the
    index is PARTIAL, and its predicate is exactly ``model IS NOT NULL AND model
    != ''`` — a wrong shape ((session_id, model), non-partial, or a different
    predicate) would fail here even if the name matched (Codex P1)."""
    conn = sqlite3.connect(POST_DB)
    try:
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({_INDEX})")]
        assert cols == ["model", "session_id"], (
            f"index columns must be [model, session_id] in order, got {cols}"
        )
        # PRAGMA index_list columns: (seq, name, unique, origin, partial) —
        # the partial flag is column 4 (column 3 is origin: 'c'/'u'/'pk').
        partial = {
            r[1]: r[4]  # name -> partial flag
            for r in conn.execute("PRAGMA index_list(conversation_messages)")
        }
        assert partial.get(_INDEX) == 1, "index must be PARTIAL (partial=1)"
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (_INDEX,),
        ).fetchone()[0]
        assert "WHERE model IS NOT NULL AND model != ''" in sql, (
            f"index predicate must be the exact partial WHERE, got: {sql}"
        )
    finally:
        conn.close()


# ── per-query plan proofs on post.sqlite ─────────────────────────────────────

def test_q1_facets_plan_is_covering_no_distinct_btree(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        text = _plan_text(conn, _Q1_FACETS)
        assert f"COVERING INDEX {_INDEX}" in text, (
            f"Q1 must use the covering index, plan: {text}"
        )
        assert "USE TEMP B-TREE FOR DISTINCT" not in text, (
            f"Q1 DISTINCT must be index-only (no temp b-tree), plan: {text}"
        )
    finally:
        conn.close()


def test_q2_distinct_model_plan_is_covering_no_distinct_btree(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        text = _plan_text(conn, _Q2_DISTINCT_MODEL)
        assert f"COVERING INDEX {_INDEX}" in text, (
            f"Q2 must use the covering index, plan: {text}"
        )
        assert "USE TEMP B-TREE FOR DISTINCT" not in text, (
            f"Q2 DISTINCT must be index-only (no temp b-tree), plan: {text}"
        )
    finally:
        conn.close()


def test_q3_production_filter_plan_is_covering_model_seek(
    cctally_module, convq_module
):
    """Q3 is derived from PRODUCTION ``_model_clause`` — so this fails if the
    production augmentation is reverted. Its ``?models=`` subquery must be a
    covering SEARCH constrained by ``model=?`` with no DISTINCT temp b-tree."""
    conn = sqlite3.connect(POST_DB)
    try:
        prod_sql, prod_params = _prod_q3(convq_module, conn)
        text = _plan_text(conn, prod_sql, prod_params)
        assert f"COVERING INDEX {_INDEX}" in text, (
            f"Q3 must use the covering model index, plan: {text}"
        )
        assert "model=?" in text, (
            f"Q3 must be a model SEEK (model=? constraint), plan: {text}"
        )
        assert "USE TEMP B-TREE FOR DISTINCT" not in text, (
            f"Q3's dropped DISTINCT must leave no temp b-tree, plan: {text}"
        )
    finally:
        conn.close()


def test_q3_unaugmented_does_not_use_partial_index(cctally_module, convq_module):
    """Paired NEGATIVE check (Codex P1): with the index PRESENT, the un-augmented
    Q3 (no explicit partial predicate, no INDEXED BY) does NOT pick the partial
    index — proving the production augmentation is what makes the plan covering.
    Belt-and-suspenders: also assert the production string carries both the
    INDEXED BY pin and the explicit predicate, so if a future SQLite happened to
    pick the index anyway the augmentation is still proven present."""
    conn = sqlite3.connect(POST_DB)
    try:
        text = _plan_text(conn, _Q3_UNAUGMENTED, ("claude-opus-4-8",))
        assert _INDEX not in text, (
            f"un-augmented Q3 must NOT use the partial index, plan: {text}"
        )
        clause_sql, _ = convq_module._model_clause(conn, ["opus"])
        assert f"INDEXED BY {_INDEX}" in clause_sql
        assert "model IS NOT NULL AND model != ''" in clause_sql
    finally:
        conn.close()


def test_analyzed_state_keeps_covering_plans(cctally_module, convq_module, tmp_path):
    """After ``ANALYZE`` (sqlite_stat1 present), the shipped Q1/Q2/Q3 plans must
    all STAY on the covering index — no regression to idx_conv_session_uuid +
    heap fetches (Codex P2).

    Scope honesty: at this fixture's tiny row count the planner keeps Q3 covering
    even WITHOUT the ``INDEXED BY`` pin, so this test does not, on its own, force
    the pin RED — it asserts the real property that the *shipped* (pinned) query
    stays covering in both the un-analyzed and analyzed states. The pin's
    load-bearing value is at production scale (a large DB whose sqlite_stat1 makes
    the planner prefer the session index for a selective multi-model IN — Codex
    reproduced that flip); its *presence* is pinned structurally by the paired-
    negative test's belt-and-suspenders assertion on the clause string."""
    work = tmp_path / "cache.db"
    shutil.copy(POST_DB, work)
    conn = sqlite3.connect(work)
    try:
        conn.execute("ANALYZE")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0] == 1, "ANALYZE must have produced sqlite_stat1"

        q1 = _plan_text(conn, _Q1_FACETS)
        assert f"COVERING INDEX {_INDEX}" in q1
        assert "USE TEMP B-TREE FOR DISTINCT" not in q1

        q2 = _plan_text(conn, _Q2_DISTINCT_MODEL)
        assert f"COVERING INDEX {_INDEX}" in q2
        assert "USE TEMP B-TREE FOR DISTINCT" not in q2

        prod_sql, prod_params = _prod_q3(convq_module, conn)
        q3 = _plan_text(conn, prod_sql, prod_params)
        assert f"COVERING INDEX {_INDEX}" in q3, (
            f"the INDEXED BY pin must keep Q3 covering post-ANALYZE, plan: {q3}"
        )
        assert "model=?" in q3
    finally:
        conn.close()


# ── handler / fresh-schema / idempotency ─────────────────────────────────────

def test_handler_creates_index_and_plan_uses_it(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite: it must create the
    index and flip Q1 from a non-covering plan to the covering index. With the
    dispatcher's central stamp reproduced, 022 is marked applied."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _INDEX not in _indexes(conn)
        assert _INDEX not in _plan_text(conn, _Q1_FACETS)

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _INDEX in _indexes(conn)
        assert f"COVERING INDEX {_INDEX}" in _plan_text(conn, _Q1_FACETS)
        assert _marker_count(conn, _MIGRATION) == 1
    finally:
        conn.close()


def test_handler_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run on the post state (index already present) must be a
    no-op that does not raise (CREATE INDEX IF NOT EXISTS) and leaves the index
    and covering plan intact."""
    work = tmp_path / "cache.db"
    shutil.copy(POST_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)  # must not raise
        assert _INDEX in _indexes(conn)
        assert f"COVERING INDEX {_INDEX}" in _plan_text(conn, _Q1_FACETS)
    finally:
        conn.close()


def test_fresh_apply_cache_schema_has_index(cctally_module, tmp_path):
    """Base-schema placement: a DB built by ``_apply_cache_schema`` alone (no
    migration replay — the fresh-install / cache-sync --rebuild path, which the
    dispatcher stamps WITHOUT running the handler) already carries the index and
    answers all three model query plans via it."""
    import _cctally_db as _db

    conn = sqlite3.connect(tmp_path / "fresh.db")
    try:
        _db._apply_cache_schema(conn)
        conn.commit()
        assert _INDEX in _indexes(conn), (
            "a fresh _apply_cache_schema DB must already have the model index"
        )
        rows = [
            ("s1", "u1", "/p/a.jsonl", 0, "assistant", "claude-opus-4-8"),
            ("s1", "u2", "/p/a.jsonl", 100, "assistant", "claude-haiku-4-5"),
            ("s2", "u3", "/p/b.jsonl", 0, "assistant", "claude-sonnet-5"),
        ]
        conn.executemany(
            "INSERT INTO conversation_messages"
            "(session_id, uuid, source_path, byte_offset, entry_type, model) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        assert f"COVERING INDEX {_INDEX}" in _plan_text(conn, _Q1_FACETS)
        assert f"COVERING INDEX {_INDEX}" in _plan_text(conn, _Q2_DISTINCT_MODEL)
    finally:
        conn.close()


# ── output-equality guards (Task 5): the index + augmentation must NOT change
#    any result — facets output and the ?models= session set are byte-identical.

def test_facets_output_fold_then_count(convq_module):
    """``list_conversation_facets`` over the seeded multi-model data must fold
    then count: s1 (opus + an opus point-release + haiku) counts ONCE under opus
    (the two opus ids fold) AND once under haiku; s2 counts under sonnet;
    NULL/'' model rows contribute nothing. Projects is empty (the fixture seeds
    no conversation_sessions rollup rows). This pins the acceptance criterion
    that facets output is unchanged by #301."""
    conn = sqlite3.connect(POST_DB)
    try:
        facets = convq_module.list_conversation_facets(conn)
        assert facets == {
            "projects": [],
            "models": [
                {"family": "opus", "count": 1},
                {"family": "sonnet", "count": 1},
                {"family": "haiku", "count": 1},
            ],
        }, facets
    finally:
        conn.close()


def test_model_filter_session_set_unchanged_by_augmentation(convq_module):
    """The ?models=["opus"] session set must be identical whether resolved by the
    augmented production ``_model_clause`` (INDEXED BY + explicit predicate,
    DISTINCT dropped) or by the pre-#301 un-augmented subquery — output-identity
    is the whole point of the augmentation. Both must yield exactly {s1} (the
    only opus session)."""
    conn = sqlite3.connect(POST_DB)
    try:
        clause_sql, params = convq_module._model_clause(conn, ["opus"])
        augmented = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT session_id FROM conversation_messages "
                "WHERE " + clause_sql, params
            )
        }
        # Reconstruct the pre-#301 (un-augmented) clause over the SAME ids.
        ph = ",".join("?" for _ in params)
        legacy_sql = (
            "SELECT DISTINCT session_id FROM conversation_messages WHERE "
            "session_id IN (SELECT DISTINCT session_id FROM conversation_messages "
            "WHERE session_id IS NOT NULL AND model IN (%s))" % ph
        )
        legacy = {r[0] for r in conn.execute(legacy_sql, params)}
        assert augmented == legacy, (
            f"augmentation changed the session set: {augmented} != {legacy}"
        )
        assert augmented == {"s1"}, augmented
    finally:
        conn.close()
