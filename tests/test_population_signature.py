"""The accounting population signature and the project caches built on it.

#769 S6 T2a, #716 Task B. `_cached_project_labeled_entries` proved a hit by
building a `frozenset` of every entry's `(project_key, project_label)` pair and
comparing it, and `_cached_projects_wire` did the same before it could reuse
anything. The nominal reuse path was therefore proportional to the whole
population, every tick, whether or not one row had changed — 891,584 pairs
visited across one run on the sealed 222,888-row measurement corpus.

`build_cached_codex_accounting` now returns an exact `population_signature`,
and the project caches compare that instead of scanning. A clean hit visits
zero accounting rows.

The direction of the guarantee is deliberate and asymmetric. An unnecessary
MISS is acceptable — it costs a rebuild. A stale HIT is not, because it serves
a population that no longer exists. Every assertion below is written in that
direction: each thing that can change the population must move the signature,
and no test asserts that anything fails to move it.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
from dataclasses import replace

import pytest

import _lib_snapshot_cache as snapshot  # noqa: E402  (bin/ on sys.path)

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)
from test_snapshot_bounded_work import (  # noqa: E402
    _broaden_corpus,
    _widen_corpus,
)


class _CountingTuple(tuple):
    """A population that reports every scan of itself.

    `len()` and indexing do not iterate; a `frozenset(... for entry in
    entries)` comprehension does. Counting `__iter__` is therefore exactly the
    question "did this code path visit the accounting rows?".
    """

    scans = 0

    def __iter__(self):
        type(self).scans += 1
        return super().__iter__()


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _widen_corpus(cache)
    shape = _broaden_corpus(cache)
    assert shape["conversations"] > 1 and shape["sessions"] > 1, shape
    module = sys.modules["_cctally_dashboard_sources"]
    module.reset_codex_source_caches()
    try:
        yield ns, cache, stats, module
    finally:
        module.reset_codex_source_caches()
        cache.close()
        stats.close()


def _context(module, cache, stats):
    return module.DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START, now_utc=NOW,
        display_tz_name="UTC",
    )


def _accounting(cache, *, range_end=None, extra=("speed", ("root",))):
    """One `build_cached_codex_accounting` round over the seeded corpus."""
    from _cctally_source_analytics import load_cached_rooted_codex_accounting_entries

    end = range_end or (NOW + dt.timedelta(microseconds=1))

    def load_all():
        return load_cached_rooted_codex_accounting_entries(
            START, end, speed="auto", cache_conn=cache)

    def load_paths(identities):
        return tuple(
            entry for entry in load_all()
            if (str(entry.source_root_key), str(entry.source_path)) in set(
                identities)
        )

    return snapshot.build_cached_codex_accounting(
        cache_conn=cache,
        range_start=START,
        range_end=end,
        extra_signature=extra,
        load_all=load_all,
        load_paths=load_paths,
        path_of=lambda e: (str(e.source_root_key), str(e.source_path)),
        account_of=lambda e: str(e.account_key),
        order_key=lambda e: (
            e.timestamp, str(e.source_root_key), str(e.source_path),
            str(e.session_id),
        ),
        identity_of=lambda e: (
            e.timestamp, str(e.source_path), str(e.session_id)),
    )


# ── the signature exists and is exact ──────────────────────────────────────

def test_the_result_carries_a_population_signature(env):
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    result = _accounting(cache)
    assert result.cold is True
    assert result.population_signature is not None
    assert isinstance(result.population_signature, tuple)


def test_a_clean_second_round_reports_the_same_signature(env):
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    first = _accounting(cache).population_signature
    second = _accounting(cache)
    assert second.cold is False
    assert second.population_signature == first


def test_an_insert_moves_the_signature(env):
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    before = _accounting(cache).population_signature
    _widen_corpus(cache, files=1, per_file=1, prefix="inserted",
                  offset_base=90_000)
    assert _accounting(cache).population_signature != before


def test_a_caller_supplied_signature_leg_moves_the_signature(env):
    """The caller's own `extra_signature` is one leg among several.

    This is NOT the store-replacement test. It was named that and asserted
    nothing about a store: it changed `("speed", ("root",))` to
    `("speed", ("other-root",))`, which is the CALLER's argument. The
    production store leg is `_cache_database_identity(context.cache_conn)`,
    and the test below is what covers it.
    """
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    before = _accounting(cache, extra=("speed", ("root",))).population_signature
    after = _accounting(
        cache, extra=("speed", ("other-root",))).population_signature
    assert after != before


def test_replacing_the_store_moves_the_signature(env, tmp_path):
    """A genuinely different cache.db, carrying the same logical rows.

    `_cache_database_identity` is the leg #809 added, and the failure it
    prevents is a second corpus inheriting the first's memo. So the test opens
    a REAL second store — a byte copy of the first, at a different path, with
    the same rows — and requires the signature to move anyway.
    """
    _ns, cache, _stats, module = env
    snapshot.reset_codex_accounting_cache_state()
    first_path = _main_path(cache)
    before = _accounting(cache).population_signature
    identity_before = module._cache_database_identity(cache)

    # The SQLite backup API rather than a byte copy of the file: the store is
    # in WAL mode, so copying `main` alone leaves the committed rows in a
    # sidecar this replica would never see.
    replica_path = tmp_path / "replica-cache.db"
    assert pathlib.Path(first_path).exists()
    replica = sqlite3.connect(str(replica_path))
    cache.backup(replica)
    try:
        identity_after = module._cache_database_identity(replica)
        assert identity_after != identity_before, (
            "two distinct stores holding the same rows must not share an "
            "identity, or a second corpus inherits the first's memo")
        snapshot.reset_codex_accounting_cache_state()
        after = _accounting(replica).population_signature
    finally:
        replica.close()
    assert after != before, (
        "the same rows in a different store must not reuse the same "
        "population name")


def _main_path(conn):
    return next(
        str(row[2]) for row in conn.execute("PRAGMA database_list")
        if str(row[1]) == "main"
    )


def test_an_update_moves_the_signature_and_misses(env):
    """Task 11 step 1 names insert, update AND delete. Only insert was covered."""
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    before = _accounting(cache)
    assert before.population_signature is not None

    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_session_entries SET account_key = ? WHERE id = ?",
        ("acct-restamped", row_id))
    cache.commit()

    after = _accounting(cache)
    assert after.population_signature != before.population_signature, (
        "an in-place attribution restamp changes which account every "
        "downstream domain files the row under, so it must move the name")
    assert any(
        str(getattr(entry, "account_key", "")) == "acct-restamped"
        for entry in after.entries), (
        "non-vacuity: the update must be visible in the reloaded population")


def test_a_delete_moves_the_signature_and_misses(env):
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    before = _accounting(cache)
    count_before = len(before.entries)
    assert count_before > 1

    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    cache.execute("DELETE FROM codex_session_entries WHERE id = ?", (row_id,))
    cache.commit()

    after = _accounting(cache)
    assert after.population_signature != before.population_signature
    assert len(after.entries) == count_before - 1, (
        "non-vacuity: the delete must have left the population")


def test_a_sequence_gap_forces_cold_reconstruction(env):
    """The gap predicate `changes[0][0] > last_seq + 1` was untested.

    A gap means the change log no longer explains how the population got from
    the memo's sequence to the store's, so an incremental delta would apply an
    incomplete set of paths. The only safe answer is a cold rebuild.
    """
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    first = _accounting(cache)
    assert first.cold is True
    assert _accounting(cache).cold is False, (
        "non-vacuity: a clean second round must be warm, or `cold` below "
        "proves nothing")

    # Advance the head past the log, which is exactly a missing sequence.
    head = int(cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'").fetchone()[0])
    cache.execute(
        "UPDATE cache_meta SET value=? "
        "WHERE key='codex_accounting_mutation_seq'", (str(head + 5),))
    cache.commit()

    assert _accounting(cache).cold is True, (
        "a sequence gap must force cold construction rather than applying a "
        "delta the change log cannot account for")


def test_a_sequence_regression_forces_cold_reconstruction(env):
    """A rebuilt store lowers the ledger head; that can only go cold."""
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    _accounting(cache)
    cache.execute(
        "UPDATE cache_meta SET value='0' "
        "WHERE key='codex_accounting_mutation_seq'")
    cache.commit()
    assert _accounting(cache).cold is True


def test_an_upper_bound_that_reveals_a_row_moves_the_signature(env):
    """The bound itself is not a leg; what it makes VISIBLE is.

    `range_end` advances on every tick, so a signature carrying it raw would
    move every tick and nothing could ever be reused — the same trap
    `_cached_projects_wire` already records for its own key. A bound that
    reveals a row dirties that row's path and moves the population generation,
    which is what the signature carries instead.
    """
    _ns, cache, _stats, _module = env
    snapshot.reset_codex_accounting_cache_state()
    end = NOW + dt.timedelta(microseconds=1)
    before = _accounting(cache, range_end=end).population_signature

    template = cache.execute(
        "SELECT session_id, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens, total_tokens, "
        "source_root_key, conversation_key FROM codex_session_entries "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is not None
    cache.execute(
        "INSERT INTO codex_session_entries (source_path, line_offset, "
        "timestamp_utc, session_id, model, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("/cached/future.jsonl", 500_001,
         (end + dt.timedelta(hours=6)).isoformat(), *template),
    )
    cache.commit()

    unchanged = _accounting(cache, range_end=end).population_signature
    revealed = _accounting(
        cache, range_end=end + dt.timedelta(days=1)).population_signature
    assert revealed != unchanged, (
        "a bound that brings a stored future row into range changes the "
        "population and must change its name")
    assert unchanged != before, (
        "non-vacuity: the insert itself moved the ledger head")


# ── the project caches reuse by signature, not by scanning ────────────────

def test_a_clean_project_label_hit_visits_zero_accounting_rows(env):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")

    rows = module._CODEX_VISIBLE_POPULATION_CACHE.get("rows_by_id") or {}
    assert rows, "non-vacuity: the build must have indexed a population"
    population = _CountingTuple(
        sorted(rows.values(), key=module._codex_population_order_key))
    signature = module._codex_project_population_signature()
    assert signature is not None, (
        "the build must publish a population signature for the project caches")

    # Prime, then read back with a matching signature.
    primed = module._cached_project_labeled_entries(
        population, ("account", "probe"), population_signature=signature)
    _CountingTuple.scans = 0
    reused = module._cached_project_labeled_entries(
        population, ("account", "probe"), population_signature=signature)
    assert _CountingTuple.scans == 0, (
        f"a clean hit visited the population {_CountingTuple.scans} times")
    # CONTENT, not length. `len(reused) == len(population)` passes for any
    # stale population of the same size, and an `account_key` or label restamp
    # is exactly a change that alters attribution without altering row count.
    assert _labelled_identity(reused) == _labelled_identity(primed)
    assert _labelled_identity(reused) == _labelled_identity(
        module._cached_project_labeled_entries(
            population, ("account", "fresh"), population_signature=None))


def test_a_moved_signature_misses_rather_than_serving_a_stale_population(env):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    rows = module._CODEX_VISIBLE_POPULATION_CACHE.get("rows_by_id") or {}
    population = tuple(
        sorted(rows.values(), key=module._codex_population_order_key))
    signature = module._codex_project_population_signature()

    primed = module._cached_project_labeled_entries(
        population, ("account", "probe"), population_signature=signature)
    # Restamp attribution on one row WITHOUT changing the row count, then move
    # the signature. A length assertion cannot tell the rebuilt answer from the
    # stale one here; the content assertion can.
    restamped = tuple(
        replace(entry, account_key="acct-moved") if index == 0 else entry
        for index, entry in enumerate(population)
    )
    reused = module._cached_project_labeled_entries(
        restamped, ("account", "probe"),
        population_signature=(*signature, "moved"))
    assert len(reused) == len(restamped)
    assert _labelled_identity(reused) != _labelled_identity(primed), (
        "a moved signature served the prior population back")
    assert any(
        str(getattr(entry, "account_key", "")) == "acct-moved"
        for entry in reused), (
        "the rebuilt answer must carry the restamped attribution")


def _labelled_identity(entries):
    """Everything a stale hit could get wrong that a row count cannot see."""
    return tuple(
        (int(entry.cache_entry_id), str(entry.project_key),
         str(entry.display_label), str(getattr(entry, "account_key", "")))
        for entry in entries
    )


def test_an_absent_signature_never_serves_a_hit(env):
    """No signature is "cannot establish identity", and that is a cold read."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    rows = module._CODEX_VISIBLE_POPULATION_CACHE.get("rows_by_id") or {}
    population = _CountingTuple(
        sorted(rows.values(), key=module._codex_population_order_key))
    signature = module._codex_project_population_signature()
    module._cached_project_labeled_entries(
        population, ("account", "probe"), population_signature=signature)
    _CountingTuple.scans = 0
    module._cached_project_labeled_entries(
        population, ("account", "probe"), population_signature=None)
    assert _CountingTuple.scans > 0, (
        "without a signature the cache must prove its hit the old way")


def test_the_label_algorithm_version_is_part_of_the_signature(env, monkeypatch):
    """A changed labelling algorithm invalidates every retained label.

    The prior version of this test asserted that
    `CODEX_POPULATION_SIGNATURE_VERSION` was a non-empty string. That is the
    ACCOUNTING carrier's own version, the label algorithm's version is
    `_CODEX_PROJECT_LABEL_ALGORITHM_VERSION`, and neither was shown to be part
    of any signature. Both are asserted here, by moving each one and requiring
    the project signature to move with it.
    """
    _ns, cache, stats, module = env
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="label-version")

    before = module._codex_project_population_signature()
    assert before is not None, (
        "non-vacuity: the build must publish a project population signature")
    assert module._CODEX_PROJECT_LABEL_ALGORITHM_VERSION in before, (
        "the label algorithm's version must be a member of the signature the "
        "label caches compare")

    monkeypatch.setattr(
        module, "_CODEX_PROJECT_LABEL_ALGORITHM_VERSION",
        "collision-safe-labels-vNEXT")
    after = module._codex_project_population_signature()
    assert after != before, (
        "a changed labelling algorithm must invalidate every retained label")

    assert isinstance(snapshot.CODEX_POPULATION_SIGNATURE_VERSION, str)
    assert snapshot.CODEX_POPULATION_SIGNATURE_VERSION in before, (
        "the accounting carrier's own signature version must be a member too")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
