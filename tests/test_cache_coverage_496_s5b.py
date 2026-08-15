"""#496 S5b Stage 3 — the journal-to-cache coverage certificate (spec §4).

A stats rebuild replays every retained Codex quota observation into `cache.db`
under both cache writer flocks whether or not the cache already holds them. The
certificate is what lets an intact cache be recognized instead of replayed, and
everything it does NOT claim is as load-bearing as what it does: it certifies
`journal ⊆ cache` only, never `cache ⊆ journal`, and never row-value
correctness.

Two properties decide its shape and are tested separately here. The identity
root binds the ordered vector of `(segment, raw extent, covered offset)`
triples, because a late append into a non-last segment changes no name and moves
no ordering — a name-bound root was rejected in review for exactly that. And a
covered extent is always a verified newline boundary, because `_repair_torn_tail`
can truncate a partial trailing line and append a complete record ending at the
same raw size.
"""
from __future__ import annotations

import ast
import importlib
import json
import pathlib
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

from conftest import load_script, redirect_paths  # noqa: E402


SEG_A = "bootstrap-0001.jsonl"
SEG_B = "observations-2026-01.jsonl"


@pytest.fixture
def core(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return importlib.import_module("_cctally_core")


def _kernel():
    return importlib.import_module("_lib_cache_coverage")


def _vector():
    return [(SEG_A, 50, 50), (SEG_B, 120, 100)]


def _valid_cert(**overrides):
    kernel = _kernel()
    cert = kernel.advance(
        None, covered=(SEG_B, 100), applied_through=(SEG_B, 120),
        pinned_vector=_vector(), physical_seq=7)
    cert.update(overrides)
    return cert


# --------------------------------------------------------------------------
# the pure kernel
# --------------------------------------------------------------------------

def test_the_kernel_imports_nothing_outside_the_stdlib():
    """A pure kernel must be unit-testable without a cache or a journal on
    disk — the same rule `bin/_lib_journal_router.py` follows."""
    import ast

    path = pathlib.Path(__file__).resolve().parent.parent / "bin" / (
        "_lib_cache_coverage.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not {name for name in imported
                if name.startswith(("_cctally", "_lib"))}, imported


def test_the_root_binds_extents_not_just_names():
    """A late append into a non-last segment changes no name and moves no
    ordering. A name-only root would stay valid while the cache lacks that
    observation, and the fast path would skip a quota replay today's rebuild
    performs."""
    kernel = _kernel()
    before = kernel.identity_root([(SEG_B, 100, 100)])
    after = kernel.identity_root([(SEG_B, 140, 140)])
    assert before != after


def test_the_root_separates_the_raw_extent_from_the_covered_offset():
    """Both operands are stored, so a segment repaired back to the same raw size
    with a different covered boundary is a different root."""
    kernel = _kernel()
    assert kernel.identity_root([(SEG_B, 120, 100)]) != kernel.identity_root(
        [(SEG_B, 120, 120)])


def test_a_bootstrap_insertion_invalidates_the_root():
    """`list_segments` sorts bootstraps before observations, so an inserted one
    changes the canonical order without changing any existing segment."""
    kernel = _kernel()
    before = kernel.identity_root([(SEG_B, 100, 100)])
    after = kernel.identity_root([(SEG_A, 50, 50), (SEG_B, 100, 100)])
    assert before != after


def test_the_root_is_order_sensitive():
    """Non-vacuity for the case above: the same triples in a different order
    must not hash the same, or an insertion could land on an equal root."""
    kernel = _kernel()
    assert kernel.identity_root([(SEG_A, 50, 50), (SEG_B, 100, 100)]) != (
        kernel.identity_root([(SEG_B, 100, 100), (SEG_A, 50, 50)]))


def test_coverage_is_bounded_to_the_complete_line_offset():
    """The promise concerns decoded records, so a raw torn-tail extent must
    never be covered: repair could truncate that suffix and append a complete
    record ending at the same size."""
    kernel = _kernel()
    cert = kernel.advance(
        None, covered=(SEG_B, 100), applied_through=(SEG_B, 120),
        pinned_vector=[(SEG_B, 120, 100)], physical_seq=7)
    assert cert["coveredHighWater"] == [SEG_B, 100]


def test_an_advance_does_not_carry_a_stale_field_forward():
    """A certificate is a statement about the CURRENT physical state, not an
    accumulation over previous ones."""
    kernel = _kernel()
    stale = _valid_cert(strayKey="stale")
    fresh = kernel.advance(
        stale, covered=(SEG_B, 100), applied_through=(SEG_B, 120),
        pinned_vector=_vector(), physical_seq=9)
    assert "strayKey" not in fresh
    assert fresh["physicalMutationSeq"] == 9


def test_a_freshly_advanced_certificate_validates():
    """Non-vacuity for the degraded cases below: the un-mutated certificate must
    pass, or every one of them would pass for the wrong reason."""
    kernel = _kernel()
    ok, why = kernel.certificate_is_valid(
        _valid_cert(), pinned_vector=_vector(), physical_seq=7)
    assert (ok, why) == (True, kernel.REASON_OK)


def _moved_physical_seq(cert):
    return {**cert, "physicalMutationSeq": cert["physicalMutationSeq"] + 1}


def _older_coverage_version(cert):
    return {**cert, "coverageVersion": cert["coverageVersion"] - 1}


def _older_interpretation_version(cert):
    return {**cert, "interpretationVersion": cert["interpretationVersion"] - 1}


def _dropped_field(cert):
    return {key: value for key, value in cert.items() if key != "identityRoot"}


def _covered_past_the_boundary(cert):
    segment, offset = cert["coveredHighWater"]
    return {**cert, "coveredHighWater": [segment, offset + 1]}


@pytest.mark.parametrize("mutation,reason_attr", [
    (_moved_physical_seq, "REASON_PHYSICAL_SEQ"),
    (_older_coverage_version, "REASON_COVERAGE_VERSION"),
    (_older_interpretation_version, "REASON_INTERPRETATION_VERSION"),
    (_dropped_field, "REASON_MALFORMED"),
    (_covered_past_the_boundary, "REASON_COVERED_HIGH_WATER"),
    (lambda cert: None, "REASON_ABSENT"),
])
def test_every_degraded_state_is_invalid_with_a_reason(mutation, reason_attr):
    kernel = _kernel()
    ok, why = kernel.certificate_is_valid(
        mutation(_valid_cert()), pinned_vector=_vector(), physical_seq=7)
    assert not ok
    assert why == getattr(kernel, reason_attr)


def test_a_certificate_without_the_applied_coordinate_is_malformed():
    """`appliedThrough` is required, not optional. A certificate lacking it
    cannot answer the next writer's contiguity question, and treating the
    absence as "extend anyway" is the laundering this field exists to stop."""
    kernel = _kernel()
    without = {k: v for k, v in _valid_cert().items() if k != "appliedThrough"}
    ok, why = kernel.certificate_is_valid(
        without, pinned_vector=_vector(), physical_seq=7)
    assert (ok, why) == (False, kernel.REASON_MALFORMED)


def test_an_out_of_vector_mint_raises_rather_than_asserting():
    """The guard must survive `python -O`, which strips every `assert`.

    Under an optimized interpreter an assertion-only guard vanishes and the
    out-of-vector certificate is STORED. `certificate_is_valid` rejects it on
    first use, so the outcome is a replay either way — but a guard that is not
    load-bearing in the interpreter production may run under is not a guard.
    """
    kernel = _kernel()
    with pytest.raises(kernel.CoverageOutOfVector):
        kernel.advance(
            None, covered=(SEG_B, 500), applied_through=(SEG_B, 500),
            pinned_vector=_vector(), physical_seq=7)
    assert issubclass(kernel.CoverageOutOfVector, ValueError), (
        "callers catch ValueError to fall back silently"
    )


# --------------------------------------------------------------------------
# `prior_is_extendable` — what an ADVANCE may check, and what it may not
# --------------------------------------------------------------------------

def test_an_extendable_predecessor_is_accepted():
    """Non-vacuity for the refusals below."""
    kernel = _kernel()
    assert kernel.prior_is_extendable(
        _valid_cert(), applied_through=(SEG_B, 120)) == (
            True, kernel.REASON_OK)


@pytest.mark.parametrize("mutation,reason_attr", [
    (lambda cert: None, "REASON_ABSENT"),
    (lambda cert: _older_coverage_version(cert), "REASON_COVERAGE_VERSION"),
    (lambda cert: _older_interpretation_version(cert),
     "REASON_INTERPRETATION_VERSION"),
    (lambda cert: {k: v for k, v in cert.items() if k != "appliedThrough"},
     "REASON_MALFORMED"),
    (lambda cert: {**cert, "appliedThrough": [SEG_B, 119]},
     "REASON_COVERED_HIGH_WATER"),
])
def test_an_outdated_predecessor_may_not_be_extended(mutation, reason_attr):
    """`advance` re-stamps the CURRENT module constants and discards `prior`.

    Extending a certificate written under an older `interpretationVersion` would
    therefore LAUNDER it into a current-version one, and the next rebuild would
    skip exactly the replay the version bump exists to force. The constant's own
    docstring says such a certificate "is rejected rather than compared".
    """
    kernel = _kernel()
    ok, why = kernel.prior_is_extendable(
        mutation(_valid_cert()), applied_through=(SEG_B, 120))
    assert not ok
    assert why == getattr(kernel, reason_attr)


def test_extendability_does_not_require_the_identity_root_or_the_sequence():
    """A full `certificate_is_valid` is the WRONG check for an advance.

    An advance's predecessor necessarily describes an older, smaller journal —
    the writer is about to certify records that grew a segment after it was
    stored — and a `physicalMutationSeq` from before the bump this transaction
    is about to make. Requiring either would refuse every advance that has ever
    been correct.
    """
    kernel = _kernel()
    prior = _valid_cert()
    grown = [(SEG_A, 50, 50), (SEG_B, 300, 300)]
    assert kernel.certificate_is_valid(
        prior, pinned_vector=grown, physical_seq=99)[0] is False
    assert kernel.prior_is_extendable(
        prior, applied_through=(SEG_B, 120))[0] is True


def test_a_changed_extent_is_invalid_on_the_identity_root():
    """The case that matters most: an append into a NON-LAST segment, which
    changes no name and moves no ordering."""
    kernel = _kernel()
    grown = [(SEG_A, 90, 90), (SEG_B, 120, 100)]
    ok, why = kernel.certificate_is_valid(
        _valid_cert(), pinned_vector=grown, physical_seq=7)
    assert (ok, why) == (False, kernel.REASON_IDENTITY_ROOT)


# --------------------------------------------------------------------------
# the cache accessors and the invalidation sites
# --------------------------------------------------------------------------

def _cache():
    return importlib.import_module("_cctally_cache")


def _open_cache(core):
    """A schema'd cache.db, through the same opener production uses."""
    core.CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _cache().open_cache_db()


def _prime(core, conn):
    cache = _cache()
    conn.execute("BEGIN IMMEDIATE")
    cache._store_codex_journal_coverage_certificate(conn, _valid_cert())
    conn.commit()
    return cache.load_codex_journal_coverage_certificate(conn)


def test_the_certificate_round_trips_through_cache_meta(core):
    cache = _cache()
    conn = _open_cache(core)
    try:
        assert cache.load_codex_journal_coverage_certificate(conn) is None
        stored = _prime(core, conn)
        assert stored == _valid_cert()
    finally:
        conn.close()


def test_a_malformed_stored_certificate_reads_as_absent(core):
    """Unreadable answers None rather than raising, because every degraded state
    falls back to a full replay silently (spec §6.3)."""
    kernel = _kernel()
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
            (kernel.CERTIFICATE_KEY, "{not json"),
        )
        conn.commit()
        assert cache.load_codex_journal_coverage_certificate(conn) is None
    finally:
        conn.close()


def test_the_two_certificates_are_stored_under_distinct_keys(core):
    """Coverage binds journal-to-cache, the projection certificate binds
    cache-to-stats. Neither may satisfy the other's gate."""
    kernel = _kernel()
    quota = importlib.import_module("_cctally_quota")
    assert kernel.CERTIFICATE_KEY != quota._DASHBOARD_PROJECTION_CERTIFICATE_KEY


def test_clearing_the_derived_rows_invalidates_the_certificate(core):
    """`_clear_codex_derived_rows` deletes the physical quota state the
    certificate describes, so leaving it would make it stale-valid."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        assert _prime(core, conn) is not None
        conn.execute("BEGIN IMMEDIATE")
        cache._clear_codex_derived_rows(conn)
        conn.commit()
        assert cache.load_codex_journal_coverage_certificate(conn) is None
    finally:
        conn.close()


def test_a_file_reset_invalidates_rather_than_leaving_it_standing(core):
    """`_delete_codex_file_derived_rows` runs `DELETE FROM
    quota_window_snapshots ... AND source_path = ?` while the journal still
    retains every observation for those bytes. Leaving the certificate would
    assert coverage for durable observations whose materialization was just
    deleted — the `journal ⊃ cache` direction the certificate excludes."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        assert _prime(core, conn) is not None
        conn.execute("BEGIN IMMEDIATE")
        cache._delete_codex_file_derived_rows(conn, "/tmp/codex/x.jsonl")
        conn.commit()
        assert cache.load_codex_journal_coverage_certificate(conn) is None
    finally:
        conn.close()


def test_an_invalidation_that_rolls_back_leaves_the_certificate(core):
    """The invalidation is in the CALLER's transaction, so a rolled-back clear
    must not have removed it."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        before = _prime(core, conn)
        conn.execute("BEGIN IMMEDIATE")
        cache._delete_codex_file_derived_rows(conn, "/tmp/codex/x.jsonl")
        conn.rollback()
        assert cache.load_codex_journal_coverage_certificate(conn) == before
    finally:
        conn.close()


def test_clearing_reports_state_changed_for_a_lone_certificate(core):
    """`_clear_codex_derived_rows`' return value decides whether the caller
    advances `codex_physical_mutation_seq`. A stored coverage certificate is
    state, so removing it is a change even when every row family was empty."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        _prime(core, conn)
        conn.execute("BEGIN IMMEDIATE")
        changed = cache._clear_codex_derived_rows(conn)
        conn.commit()
        assert changed is True
    finally:
        conn.close()


def test_clearing_an_empty_cache_reports_no_change(core):
    """Non-vacuity for the case above: without a certificate the same call must
    report no change, or the assertion there proves nothing."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = cache._clear_codex_derived_rows(conn)
        conn.commit()
        assert changed is False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the covered boundary is bounded by what the pass DECODED, not by the file
# --------------------------------------------------------------------------

def _journal():
    return importlib.import_module("_cctally_journal")


def _write_segment(core, name, payload: bytes) -> int:
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = core.JOURNAL_DIR / name
    path.write_bytes(payload)
    return len(payload)


def test_a_repaired_torn_tail_must_not_widen_the_covered_boundary(core):
    """The defect, reproduced on disk rather than argued from source.

    `coverage_pinned_vector` re-stats the segment AFTER the pass finished
    reading it. A segment torn at the pass's high water and repaired by a later
    append has a current complete-line offset at or PAST that high water, so
    bounding by `min(raw_high_water, complete_line_offset_now)` returns the raw
    high water — a boundary whose trailing bytes the traversal never decoded,
    because `_iter_segment_lines` skipped the partial line. §4.2 forbids exactly
    that: "a covered extent is always a verified newline boundary, never a raw
    size".
    """
    jr = _journal()
    line = b'{"v":1,"t":"obs"}\n'
    torn_prefix = len(line)                     # the one complete line
    high_water = torn_prefix + len(b'{"v":1,"t":"o')   # the torn tail's end
    # The repaired state: the partial suffix was truncated and a whole record
    # appended, so the file is longer than the pass's high water and every byte
    # of it now ends on a newline.
    size = _write_segment(core, "observations-2026-01.jsonl", line + line + line)
    assert size > high_water, "the repair must have grown the segment"

    vector = jr.coverage_pinned_vector()
    assert vector == (("observations-2026-01.jsonl", size, size),), (
        "the repaired segment must present a complete-line offset PAST the "
        "pass's high water, or this reproduces nothing"
    )

    plan = jr._coverage_advance_plan(
        ("observations-2026-01.jsonl", 0),
        ("observations-2026-01.jsonl", high_water),
        decoded_end=("observations-2026-01.jsonl", torn_prefix),
    )
    _vec, covered, applied_through = plan
    assert covered == ("observations-2026-01.jsonl", torn_prefix)
    assert applied_through == ("observations-2026-01.jsonl", high_water)


def test_a_malformed_trailing_line_bounds_the_boundary_too(core):
    """Same defect without any repair. A newline-terminated line that does not
    decode moves the complete-line offset to the raw high water while the pass
    decoded nothing past the record before it."""
    jr = _journal()
    line = b'{"v":1,"t":"obs"}\n'
    size = _write_segment(
        core, "observations-2026-01.jsonl", line + b"not json at all\n")
    _vec, covered, applied_through = jr._coverage_advance_plan(
        ("observations-2026-01.jsonl", 0),
        ("observations-2026-01.jsonl", size),
        decoded_end=("observations-2026-01.jsonl", len(line)),
    )
    assert covered == ("observations-2026-01.jsonl", len(line))
    assert applied_through == ("observations-2026-01.jsonl", size)


def test_nothing_decoded_in_the_high_water_segment_covers_none_of_it(core):
    """A `decoded_end` in an earlier segment answers 0: everything before this
    segment is covered and none of this segment is, which stays true."""
    jr = _journal()
    line = b'{"v":1,"t":"obs"}\n'
    _write_segment(core, "bootstrap-0001.jsonl", line)
    size = _write_segment(core, "observations-2026-01.jsonl", b"not json\n")
    _vec, covered, _applied = jr._coverage_advance_plan(
        ("bootstrap-0001.jsonl", len(line)),
        ("observations-2026-01.jsonl", size),
        decoded_end=("bootstrap-0001.jsonl", len(line)),
    )
    assert covered == ("observations-2026-01.jsonl", 0)


def test_an_untorn_segment_covers_its_whole_high_water(core):
    """Non-vacuity for the three above: with the pass having decoded through the
    high water, the bound must not shrink anything."""
    jr = _journal()
    line = b'{"v":1,"t":"obs"}\n'
    size = _write_segment(core, "observations-2026-01.jsonl", line + line)
    _vec, covered, applied_through = jr._coverage_advance_plan(
        ("observations-2026-01.jsonl", 0),
        ("observations-2026-01.jsonl", size),
        decoded_end=("observations-2026-01.jsonl", size),
    )
    assert covered == ("observations-2026-01.jsonl", size)
    assert applied_through == ("observations-2026-01.jsonl", size)


def test_an_unresolvable_boundary_is_not_reported_as_a_moved_root(core):
    """`REASON_IDENTITY_ROOT` tells an operator the journal identity moved. When
    no boundary can be resolved at all the certificate was never consulted, so
    that verdict would be a false statement about which check failed."""
    jr = _journal()
    kernel = _kernel()
    _write_segment(core, "observations-2026-01.jsonl", b'{"v":1}\n')
    _vector_out, covered, verdict, snapshot = jr._resolve_quota_cache_coverage(
        core.CACHE_DB_PATH, None)
    assert covered is None
    assert verdict == kernel.REASON_NO_BOUNDARY
    assert snapshot is None, (
        "the certificate was never consulted, so no snapshot may be left open")


def test_every_covered_writer_carries_exactly_one_action(core):
    """The vocabulary is closed. `prohibited` is the default for anything
    absent, so it is never written as a value.

    `mint` is a separate word from `advance` because §4.3 defines advancing as
    including a `codex_physical_mutation_seq` bump and the rebuild leg makes
    none: it ESTABLISHES coverage over a prefix it read from the journal rather
    than extending a predecessor's claim.
    """
    cache = _cache()
    actions = cache.COVERAGE_WRITER_ACTIONS
    assert set(actions.values()) == {
        "advance", "mint", "preserve", "invalidate"}
    assert actions[
        "_cctally_cache._write_codex_file_batch(reset_file=True)"] == "invalidate"
    # #500 review finding F4 fused the two journal-derived Codex rehydrations
    # into one traversal, so the inventory names the fused writer — the wrappers
    # that replaced the old entries hold no DML of their own.
    assert actions[
        "_cctally_journal.rehydrate_codex_journal_families"] == "preserve"
    assert actions["_cctally_journal._cache_applier"] == "advance"
    assert actions["_cctally_journal._rebuild_quota_cache_leg_raw"] == "mint"


def test_the_mint_refuses_to_move_a_stored_certificate_backward(core):
    """The mint stores with `prior=None`, so it reads what is present first.

    Unreachable today because the only other certificate writer runs under the
    ingest lock `cmd_db_rebuild` holds exclusively — but that safety comes from
    a lock in another module, not from the mint.
    """
    cache = _cache()
    kernel = _kernel()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ahead = kernel.advance(
            None, covered=(SEG_B, 100), applied_through=(SEG_B, 120),
            pinned_vector=_vector(), physical_seq=7)
        cache._store_codex_journal_coverage_certificate(conn, ahead)
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        minted = cache._advance_codex_journal_coverage(
            conn, prior=None, covered=(SEG_B, 40),
            applied_through=(SEG_B, 40), pinned_vector=_vector(),
            allow_mint=True)
        conn.commit()
        assert minted is False
        assert cache.load_codex_journal_coverage_certificate(conn) == ahead

        # Non-vacuity: the same mint at or past the stored coordinate lands.
        conn.execute("BEGIN IMMEDIATE")
        assert cache._advance_codex_journal_coverage(
            conn, prior=None, covered=(SEG_B, 100),
            applied_through=(SEG_B, 120), pinned_vector=_vector(),
            allow_mint=True) is True
        conn.commit()
    finally:
        conn.close()


def test_an_authoritative_rehydrate_refuses_while_coverage_still_stands(core):
    """`rehydrate_codex_file_accounts(authoritative=True)` runs
    `DELETE FROM codex_file_accounts`, a covered family, yet its inventory entry
    is `preserve`.

    That holds only because its one caller ran `_clear_codex_derived_rows` and
    committed first, which deleted the certificate. A second caller or a
    reordering inside `sync_codex_cache` would break it silently, and the static
    scanner cannot see it because the key is already in the inventory with a
    green label.
    """
    jr = _journal()
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._store_codex_journal_coverage_certificate(conn, _valid_cert())
        conn.commit()
        with pytest.raises(jr.CoverageInvariantViolation):
            jr.rehydrate_codex_file_accounts(conn, authoritative=True)

        # The additive branch is unaffected — it writes no quota row and clears
        # nothing, which is what makes `preserve` correct there.
        jr.rehydrate_codex_file_accounts(conn, authoritative=False)

        # Non-vacuity: once the certificate is gone the authoritative branch
        # runs, which is the ordering the one real caller establishes.
        conn.execute("BEGIN IMMEDIATE")
        cache._invalidate_codex_journal_coverage_certificate(conn)
        conn.commit()
        jr.rehydrate_codex_file_accounts(conn, authoritative=True)
    finally:
        conn.close()


def test_the_authoritative_refusal_also_sees_a_lone_progress_record(core):
    """An in-flight recovery checkpoint is the other half of the state a
    destructive clear removes, so an authoritative replay may not run over one
    either."""
    jr = _journal()
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert cache._store_codex_recovery_progress(conn, _progress())
        conn.commit()
        with pytest.raises(jr.CoverageInvariantViolation):
            jr.rehydrate_codex_file_accounts(conn, authoritative=True)
    finally:
        conn.close()


def _discovered(core, path_str):
    cache = _cache()
    path = pathlib.Path(path_str)
    return cache.CodexDiscoveredFile(
        source_path=path, physical_path=path, provider_root=path.parent,
        walk_root=path.parent, source_root_key="root-a",
    )


def _batch(core, conn, path_str, *, reset_file):
    _cache()._write_codex_file_batch(
        conn, discovered=_discovered(core, path_str), path_str=path_str,
        size=10, mtime_ns=1, final_offset=10, last_session_id=None,
        last_model=None, last_total_tokens=None, last_native_thread_id=None,
        last_root_thread_id=None, last_parent_thread_id=None,
        last_conversation_key=None, last_turn_id=None, reset_file=reset_file,
        accounting_rows=[], quota_rows=[], thread_rows=[],
        active_root_keys={"root-a"}, prune_roots=False,
    )


def _clear(core, conn):
    _cache()._clear_codex_derived_rows(conn)


def _delete_file(core, conn):
    _cache()._delete_codex_file_derived_rows(conn, "/tmp/codex/x.jsonl")


def _reset_file_batch(core, conn):
    _batch(core, conn, "/tmp/codex/x.jsonl", reset_file=True)


def _fused_ingest_rebuild(core, conn):
    """Migration 024. It owns its own `BEGIN IMMEDIATE` and commit."""
    importlib.import_module("_cctally_db")._024_codex_fused_ingest_rebuild(conn)


@pytest.mark.parametrize("key,invoke,owns_transaction", [
    ("_cctally_cache._clear_codex_derived_rows", _clear, False),
    ("_cctally_cache._delete_codex_file_derived_rows", _delete_file, False),
    ("_cctally_cache._write_codex_file_batch(reset_file=True)",
     _reset_file_batch, False),
    ("_cctally_db._024_codex_fused_ingest_rebuild",
     _fused_ingest_rebuild, True),
])
def test_a_path_labelled_invalidate_actually_invalidates(
    core, key, invoke, owns_transaction,
):
    """The inventory guard checks the KEY SET, never the behaviour behind it.

    Migration 024 was labelled `invalidate` and deleted nothing: it runs
    `DELETE FROM quota_window_snapshots WHERE source = 'codex'` and does not
    bump `codex_physical_mutation_seq`, so a stored certificate stayed
    stale-VALID over a cache holding zero Codex quota rows — reachable through
    `db skip 024` then `db unskip 024`. This test is what turns the label into a
    claim about what the code does.
    """
    cache = _cache()
    assert cache.COVERAGE_WRITER_ACTIONS[key] == "invalidate"
    conn = _open_cache(core)
    try:
        assert _prime(core, conn) is not None
        if not owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        invoke(core, conn)
        if not owns_transaction:
            conn.commit()
        assert cache.load_codex_journal_coverage_certificate(conn) is None
    finally:
        conn.close()


def test_the_preserve_deviation_is_safe_because_of_the_sequence_bump(core):
    """`_write_codex_file_batch` deviates from spec §4.3 by preserving.

    The shipped comment claimed it was safe because its own appends grow a
    segment, and that reason is FALSE: `_append_codex_quota_obs` is best-effort
    and swallows every exception, so a batch can write cache rows while growing
    no segment at all. The real reason is the UNCONDITIONAL
    `_bump_codex_physical_mutation_seq` in the same transaction, which moves the
    sequence the certificate is bound to. `_cache_applier` already makes its own
    bump conditional, so a future change doing the same here would silently turn
    `preserve` into a stale-valid path with a green suite. This pins it.
    """
    kernel = _kernel()
    cache = _cache()
    conn = _open_cache(core)
    try:
        # The primed certificate carries `physicalMutationSeq` 7, so the stored
        # sequence is set to 7 too — otherwise it would be invalid before the
        # batch ever ran and the assertion below would pass for the wrong
        # reason.
        conn.execute(
            "INSERT INTO cache_meta(key, value) "
            "VALUES ('codex_physical_mutation_seq', '7') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        conn.commit()
        before = _prime(core, conn)
        assert kernel.certificate_is_valid(
            before, pinned_vector=_vector(), physical_seq=7) == (
                True, kernel.REASON_OK), "it must be VALID before the batch"

        conn.execute("BEGIN IMMEDIATE")
        _batch(core, conn, "/tmp/codex/y.jsonl", reset_file=False)
        conn.commit()

        after = cache.load_codex_journal_coverage_certificate(conn)
        assert after == before, "an ordinary batch PRESERVES the certificate"
        seq_after = int(conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_physical_mutation_seq'").fetchone()[0])
        assert seq_after == 8, (
            "the bump is what makes preserving safe; without it this test "
            "would prove nothing"
        )
        ok, why = kernel.certificate_is_valid(
            after, pinned_vector=_vector(), physical_seq=seq_after)
        assert (ok, why) == (False, kernel.REASON_PHYSICAL_SEQ)
    finally:
        conn.close()


_DML = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)"
    r"\s+(\w+)",
    re.IGNORECASE,
)


#: What an f-string interpolation contributes to the scanned SQL text when its
#: value is not statically known. It must be a token the DML regex can MATCH as
#: a table name, so `f"DELETE FROM {table}"` produces a table position the scan
#: can see rather than collapsing to `DELETE FROM ` and matching nothing. The
#: sentinel is then reported, not silently dropped.
_UNRESOLVED_TABLE = "CCTALLYUNRESOLVEDTABLE"


def _sql_text(node, constants):
    """One AST node's SQL text, following module-level constants and f-strings.

    `_apply_quota_records` selects between two module-level statements, and
    `backfill_codex_quota_observed_model` executes an f-string built from a
    second constant. A scan that read only inline string literals would miss
    both, and would then certify an inventory that omits the two functions that
    do the most covered writing in the repository.

    An interpolation whose value is not statically known becomes
    `_UNRESOLVED_TABLE` rather than the empty string. Substituting nothing made
    `f"DELETE FROM {table}"` match no table at all, so a site whose table name
    is computed was invisible to the scan; the sentinel makes it visible, and
    `test_no_covered_write_hides_behind_a_computed_table_name` fails on it
    rather than letting it pass unexamined.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            (_sql_text(part, constants)
             if isinstance(part, ast.Constant)
             else (_sql_text(part.value, constants) or _UNRESOLVED_TABLE))
            or ""
            for part in node.values
        )
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _sql_text(node.left, constants) or ""
        right = _sql_text(node.right, constants) or ""
        return (left + right) or None
    return None


def _python_sources(root):
    """Every Python file under ``bin/``, extension or not.

    Globbing `*.py` never parsed `bin/cctally` — the extensionless entry point,
    and a recorded failure shape in this repository. The shebang is what decides
    membership, exactly as the interpreter does.
    """
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            yield path
            continue
        try:
            head = path.read_bytes()[:64]
        except OSError:  # pragma: no cover — unreadable entry
            continue
        if head.startswith(b"#!") and b"python" in head.split(b"\n", 1)[0]:
            yield path


def _scan_cache_materialization_sites(families):
    """`{module.function}` for every `bin/` function whose body writes ``families``.

    Attribution is to the function that ISSUES the DML, not to the one that
    causes it: `_039_codex_quota_observed_model_backfill` delegates to
    `backfill_codex_quota_observed_model`, and following call graphs would make
    the inventory a transitive closure over most of the module. The leaf is what
    the certificate has to reason about, because it is what touches the rows.

    Fixture builders are excluded by name. They construct a cache from nothing
    for a test, so there is no certificate to keep honest and no journal behind
    the rows they seed.
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "bin"
    sites: set = set()
    for path in _python_sources(root):
        if path.name.startswith("build-") or path.name == "_fixture_builders.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                text = _sql_text(node.value, constants)
                if text:
                    constants[node.targets[0].id] = text
        tainted = {
            name for name, text in constants.items()
            if any(t in families for t in _DML.findall(text))
        }
        stack: list = []
        classes: list = []

        class Visitor(ast.NodeVisitor):
            def visit_ClassDef(self, node):
                # Qualified, so two same-named methods on different classes are
                # two inventory entries rather than one masking the other.
                classes.append(node.name)
                self.generic_visit(node)
                classes.pop()

            def visit_FunctionDef(self, node):
                stack.append(".".join([*classes, node.name]))
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Constant(self, node):
                if isinstance(node.value, str) and stack and any(
                    table in families for table in _DML.findall(node.value)
                ):
                    sites.add(f"{path.stem}.{stack[0]}")

            def visit_Name(self, node):
                if node.id in tainted and stack:
                    sites.add(f"{path.stem}.{stack[0]}")

        Visitor().visit(tree)
    return sites


def test_the_inventory_is_complete(core):
    """Spec §4.3's inventory is enforceable only if nothing outside it writes.

    This is the guard that turns "anything else → prohibited" from a sentence
    into a check: it scans `bin/` for DML against the covered families and
    asserts the observed set equals the inventory's scannable half. A writer
    added later with no assigned action fails here rather than silently leaving
    a certificate stale-valid.
    """
    cache = _cache()
    observed = _scan_cache_materialization_sites(cache.COVERAGE_CACHE_FAMILIES)
    assert observed, "the scan must find writers, or this test proves nothing"
    scannable = set(cache.COVERAGE_WRITER_ACTIONS) - cache.COVERAGE_NON_LEAF_ACTIONS
    assert observed == scannable


def test_the_scan_parses_the_extensionless_entry_point(core):
    """`bin/cctally` carries no `.py`, so a `*.py` glob never parsed it — a
    recorded failure shape in this repository. Membership is decided by the
    shebang, exactly as the interpreter decides it."""
    root = pathlib.Path(__file__).resolve().parent.parent / "bin"
    assert root / "cctally" in set(_python_sources(root))


#: Every `bin/` function whose DML names its table through an interpolation the
#: scan cannot resolve statically. None of them can name a covered family today,
#: and each was read to confirm it. The set is pinned rather than tolerated: a
#: computed table name is exactly where a covered write could hide from
#: `test_the_inventory_is_complete`, so a new one must be read and classified
#: rather than silently skipped.
#: Each was read: every one interpolates a name that comes from a stats-family
#: table spec, a five-hour child table, a migration's own scratch name or a
#: schema-migration bookkeeping table. None of the three covered families is
#: ever assigned into a variable that reaches a table position anywhere in
#: `bin/` — they are named only as literals — which is what makes this list
#: safe today and worth re-reading whenever it grows.
_COMPUTED_TABLE_SITES = frozenset({
    "_cctally_db._migration_budget_milestone_period_keys",
    "_cctally_db._migration_merge_5h_block_duplicates_v1",
    "_cctally_db._recover_version_ahead",
    "_cctally_journal._apply_reset_with_suppression",
    "_cctally_journal._apply_weekly_credit_effects",
    "_cctally_journal._converge_row_from_effective",
    "_cctally_journal._emit_harvest_row",
    "_cctally_journal._insert_or_ignore",
    "_cctally_journal._replace_block_children",
    "_cctally_journal.run_cutover",
})


def _scan_computed_table_sites():
    """`{module.function}` for DML whose table position did not resolve."""
    root = pathlib.Path(__file__).resolve().parent.parent / "bin"
    sites: set = set()
    for path in _python_sources(root):
        if path.name.startswith("build-") or path.name == "_fixture_builders.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                text = _sql_text(node.value, constants)
                if text:
                    constants[node.targets[0].id] = text
        stack: list = []
        classes: list = []

        class Visitor(ast.NodeVisitor):
            def visit_ClassDef(self, node):
                # Class-qualified for the reason the sibling scanner gives:
                # without it a computed-table site inside a method would be
                # recorded unqualified, and two same-named methods on different
                # classes would collapse into one entry. No such site exists
                # today, which is exactly when the omission is invisible.
                classes.append(node.name)
                self.generic_visit(node)
                classes.pop()

            def visit_FunctionDef(self, node):
                stack.append(".".join([*classes, node.name]))
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_JoinedStr(self, node):
                text = _sql_text(node, constants)
                if text and _UNRESOLVED_TABLE in _DML.findall(text) and stack:
                    sites.add(f"{path.stem}.{stack[0]}")

        Visitor().visit(tree)
    return sites


def test_the_computed_table_scan_qualifies_a_method_by_its_class():
    """Non-vacuity for the `visit_ClassDef` above.

    The scanner is fed a synthetic module rather than waiting for `bin/` to grow
    such a site, because a guard whose corrected behaviour nothing can reach is
    a guard nobody can tell apart from the omission it replaced.
    """
    source = (
        "class Holder:\n"
        "    def wipe(self, conn, table):\n"
        "        conn.execute(f'DELETE FROM {table}')\n"
    )
    tree = ast.parse(source)
    stack: list = []
    classes: list = []
    found: set = set()

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            classes.append(node.name)
            self.generic_visit(node)
            classes.pop()

        def visit_FunctionDef(self, node):
            stack.append(".".join([*classes, node.name]))
            self.generic_visit(node)
            stack.pop()

        def visit_JoinedStr(self, node):
            text = _sql_text(node, {})
            if text and _UNRESOLVED_TABLE in _DML.findall(text) and stack:
                found.add(stack[0])

    Visitor().visit(tree)
    assert found == {"Holder.wipe"}


def test_no_covered_write_hides_behind_a_computed_table_name(core):
    """An interpolated table name is where a covered write could hide.

    `f"DELETE FROM {table}"` used to collapse to `DELETE FROM ` and match no
    table at all, so the inventory guard could not see it. The sentinel makes
    the position visible; this test refuses to let a NEW one through unread.
    """
    assert _scan_computed_table_sites() == _COMPUTED_TABLE_SITES


def test_the_non_leaf_keys_are_exactly_the_unscannable_ones(core):
    """Non-vacuity for the guard above. Without this, an inventory key nobody
    can reach could be parked in `COVERAGE_NON_LEAF_ACTIONS` and the equality
    would still hold."""
    cache = _cache()
    assert cache.COVERAGE_NON_LEAF_ACTIONS == frozenset({
        "_cctally_cache._write_codex_file_batch(reset_file=True)",
        "_cctally_journal._cache_applier",
        "_cctally_journal._rebuild_quota_cache_leg_raw",
    })
    assert cache.COVERAGE_NON_LEAF_ACTIONS <= set(cache.COVERAGE_WRITER_ACTIONS)


def test_the_scan_reads_the_families_the_certificate_covers(core):
    """The scan is only as complete as its family list. These four are the
    tables a journal record materializes into: a Codex quota observation
    becomes a `quota_window_snapshots` row, a file-account decision becomes a
    `codex_file_accounts` row plus its `codex_file_incarnations` MAX-set, and
    an operator window attribution (#500) becomes a
    `codex_window_attributions` row."""
    cache = _cache()
    assert set(cache.COVERAGE_CACHE_FAMILIES) == {
        "quota_window_snapshots",
        "codex_file_accounts",
        "codex_file_incarnations",
        "codex_window_attributions",
    }


def test_the_stored_certificate_is_canonical_json(core):
    """A byte-stable encoding, so two writers of the same certificate produce
    the same `cache_meta` row and a comparison never turns on key order."""
    kernel = _kernel()
    cache = _cache()
    conn = _open_cache(core)
    try:
        _prime(core, conn)
        raw = conn.execute(
            "SELECT value FROM cache_meta WHERE key = ?",
            (kernel.CERTIFICATE_KEY,),
        ).fetchone()[0]
        assert raw == json.dumps(
            _valid_cert(), separators=(",", ":"), sort_keys=True)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the bounded-recovery kernel (spec §4.5)
# --------------------------------------------------------------------------

def _progress(**overrides):
    kernel = _kernel()
    base = kernel.make_progress(
        pass_id="pass-a", started_at=1000, chunks=2,
        identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-1", covered=(SEG_B, 100))
    base.update(overrides)
    return base


def test_the_progress_record_carries_no_write_only_applied_count():
    """`applied` was written into every checkpoint and read by nothing.

    A durable field nobody consumes is a field a later reader will mistake for
    state the mechanism depends on, so it is gone rather than documented.
    """
    kernel = _kernel()
    assert "applied" not in kernel.PROGRESS_FIELDS
    assert "applied" not in _progress()
    assert set(_progress()) == set(kernel.PROGRESS_FIELDS)




def test_chunk_spans_cap_by_bytes_and_by_record_count():
    """Capping by records alone lets one chunk of large observations blow the
    memory bound; capping by bytes alone lets a chunk of tiny ones carry far
    more rows than one transaction should."""
    kernel = _kernel()
    by_records = kernel.chunk_spans(
        [10] * 7, byte_cap=10_000, record_cap=2)
    assert [(a, b) for a, b, _n in by_records] == [
        (0, 2), (2, 4), (4, 6), (6, 7)]
    by_bytes = kernel.chunk_spans(
        [100] * 7, byte_cap=250, record_cap=1000)
    assert [(a, b) for a, b, _n in by_bytes] == [(0, 2), (2, 4), (4, 6), (6, 7)]


def test_one_record_larger_than_the_cap_still_gets_a_chunk():
    """Refusing it would stall the pass on a record it is required to apply."""
    kernel = _kernel()
    assert kernel.chunk_spans(
        [5_000], byte_cap=10, record_cap=10) == [(0, 1, 5_000)]


def test_chunk_spans_over_nothing_is_no_chunks():
    kernel = _kernel()
    assert kernel.chunk_spans([], byte_cap=10, record_cap=10) == []


def test_a_pass_resumes_its_own_unchanged_progress():
    """Non-vacuity for every refusal below."""
    kernel = _kernel()
    assert kernel.resume_verdict(
        _progress(), pass_id="pass-a", started_at=1000,
        identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-1") == (
            kernel.RESUME, kernel.REASON_OK, False)


@pytest.mark.parametrize("stored,reason_attr", [
    (None, "REASON_ABSENT"),
    ("not a dict", "REASON_MALFORMED"),
])
def test_a_deleted_or_malformed_progress_record_restarts(stored, reason_attr):
    """Every destructive clear deletes the progress record in the same
    transaction as its deletes, so an absent one is how a resumed pass learns a
    writer emptied the cache underneath it."""
    kernel = _kernel()
    verdict, why, _seen = kernel.resume_verdict(
        stored, pass_id="pass-a", started_at=1000, identity_root="root-1",
        physical_seq=7, source_roots_digest="digest-1")
    assert verdict == kernel.RESTART
    assert why == getattr(kernel, reason_attr)


def test_a_moved_identity_root_restarts():
    kernel = _kernel()
    verdict, why, _seen = kernel.resume_verdict(
        _progress(), pass_id="pass-a", started_at=1000,
        identity_root="root-2", physical_seq=7,
        source_roots_digest="digest-1")
    assert (verdict, why) == (kernel.RESTART, kernel.REASON_IDENTITY_ROOT)


def test_a_concurrent_additive_writer_is_reported_but_does_not_restart():
    """The deliberate deviation from spec §4.5, stated in `resume_verdict`.

    It narrows exactly ONE of the four compared quantities. An additive writer —
    an ordinary rollout batch — bumps `physicalMutationSeq` on every status-line
    tick without deleting anything, and restarting on that would abandon a pass
    every time a tick landed mid-recovery, then report an uncovered remainder
    for a cache with no shortfall. That axis is reported instead.
    """
    kernel = _kernel()
    verdict, why, seen = kernel.resume_verdict(
        _progress(), pass_id="pass-a", started_at=1000,
        identity_root="root-1", physical_seq=9,
        source_roots_digest="digest-1")
    assert (verdict, why) == (kernel.RESUME, kernel.REASON_OK)
    assert seen is True


def test_a_changed_source_roots_digest_restarts():
    """The digest is the SECOND witness of a destructive clear, so acting on it
    is the whole reason it is computed.

    `_clear_codex_derived_rows` empties `codex_source_roots` along with the
    quota rows, and the digest is taken over the SET of `source_root_key`
    values, so an ordinary batch's `ON CONFLICT DO UPDATE SET last_seen_utc`
    does not move it. Folding it into the additive-writer report left the one
    mechanism designed to catch a clear whose progress delete was missed
    computed and then discarded.
    """
    kernel = _kernel()
    verdict, why, seen = kernel.resume_verdict(
        _progress(), pass_id="pass-a", started_at=1000,
        identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-2")
    assert verdict == kernel.RESTART
    assert why == "sourceRootsDigest"
    assert seen is False, (
        "the sequence did not move, so no additive writer was observed")


def test_a_newer_foreign_pass_yields_and_an_older_one_is_overwritten():
    """Two passes that each restarted on seeing the other would make no
    progress at all, so the older one stops instead."""
    kernel = _kernel()
    assert kernel.resume_verdict(
        _progress(passId="pass-b", startedAt=2000), pass_id="pass-a",
        started_at=1000, identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-1")[:2] == (kernel.YIELD, "newerPass")
    older = kernel.resume_verdict(
        _progress(passId="pass-b", startedAt=500), pass_id="pass-a",
        started_at=1000, identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-1")
    assert older[:2] == (kernel.RESTART, "foreignPass")
    assert older[2] is False, (
        "a dead pass's leftover record is not a concurrent writer, and "
        "reporting one puts a writer that does not exist on the rebuild record")


def test_an_orphan_from_before_a_backward_clock_step_is_not_yielded_to():
    """`startedAt` is a wall clock compared with `>` and carries no pid,
    heartbeat or TTL.

    Two passes on one machine read the same clock, so a live competitor starts
    at most seconds ahead. A record arbitrarily far in the future means the
    clock stepped BACKWARD — an NTP correction or a VM restore — and without a
    bound every later pass looks older than that orphan and yields to it on
    every rebuild until the clock catches up.
    """
    kernel = _kernel()
    skew = kernel.PROGRESS_YIELD_MAX_SKEW_US
    # Just inside the bound is still a live competitor.
    assert kernel.resume_verdict(
        _progress(passId="pass-b", startedAt=1000 + skew), pass_id="pass-a",
        started_at=1000, identity_root="root-1", physical_seq=7,
        source_roots_digest="digest-1")[:2] == (kernel.YIELD, "newerPass")
    # Past it, it cannot be one.
    orphan = kernel.resume_verdict(
        _progress(passId="pass-b", startedAt=1000 + skew + 1),
        pass_id="pass-a", started_at=1000, identity_root="root-1",
        physical_seq=7, source_roots_digest="digest-1")
    assert orphan[:2] == (kernel.RESTART, kernel.REASON_ORPHANED_PASS)
    assert orphan[2] is False


def test_a_mint_may_not_move_a_stored_certificate_backward():
    """The recovery mint stores with `prior=None`, so without this it overwrites
    whatever is present. `progress_supersedes` already applies this monotonicity
    to progress records; the certificate had none of its own."""
    kernel = _kernel()
    stored = _valid_cert()          # appliedThrough (SEG_B, 120)
    assert kernel.applied_through_regresses(
        stored, (SEG_B, 100), _vector()) is True
    assert kernel.applied_through_regresses(
        stored, (SEG_A, 50), _vector()) is True, (
            "an earlier SEGMENT is behind too, and the ordering comes from the "
            "pinned vector rather than from the segment name")
    assert kernel.applied_through_regresses(
        stored, (SEG_B, 120), _vector()) is False
    assert kernel.applied_through_regresses(
        stored, (SEG_B, 200), _vector()) is False


@pytest.mark.parametrize("stored", [
    None,
    "not a dict",
    {"appliedThrough": ["some-other-segment.jsonl", 999]},
    {"appliedThrough": None},
    {},
])
def test_an_incomparable_predecessor_does_not_block_a_mint(stored):
    """A stored coordinate the pinned vector does not offer describes a journal
    this pass is not looking at, and `certificate_is_valid` already rejects it
    on the identity root. Refusing the mint there would leave a certificate that
    can never be replaced."""
    kernel = _kernel()
    assert kernel.applied_through_regresses(
        stored, (SEG_B, 100), _vector()) is False


@pytest.mark.parametrize("stored,candidate,expected", [
    (None, {"passId": "a", "startedAt": 1, "chunks": 1}, True),
    ({"passId": "a", "startedAt": 1, "chunks": 2},
     {"passId": "a", "startedAt": 1, "chunks": 3}, True),
    # A pass may not move its OWN progress backwards.
    ({"passId": "a", "startedAt": 1, "chunks": 3},
     {"passId": "a", "startedAt": 1, "chunks": 2}, False),
    # An older worker cannot overwrite a newer pass's progress.
    ({"passId": "b", "startedAt": 9, "chunks": 1},
     {"passId": "a", "startedAt": 1, "chunks": 50}, False),
    ({"passId": "b", "startedAt": 1, "chunks": 50},
     {"passId": "a", "startedAt": 9, "chunks": 1}, True),
])
def test_the_progress_compare_and_swap_is_monotonic(
    stored, candidate, expected,
):
    assert _kernel().progress_supersedes(stored, candidate) is expected


def test_the_progress_record_is_deleted_with_the_certificate(core):
    """Spec §4.5 requires that every destructive clear delete the progress
    record in the same transaction as the certificate. Both deletes live behind
    ONE function, so a writer cannot get half of it right."""
    kernel = _kernel()
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cache._store_codex_journal_coverage_certificate(conn, _valid_cert())
        assert cache._store_codex_recovery_progress(conn, _progress())
        conn.commit()
        assert cache.load_codex_recovery_progress(conn) == _progress()

        conn.execute("BEGIN IMMEDIATE")
        cache._clear_codex_derived_rows(conn)
        conn.commit()
        assert cache.load_codex_journal_coverage_certificate(conn) is None
        assert cache.load_codex_recovery_progress(conn) is None
    finally:
        conn.close()


def test_a_lone_progress_record_counts_as_state_for_the_clear(core):
    """`_clear_codex_derived_rows`' return value decides whether the caller
    advances `codex_physical_mutation_seq`, so an in-flight recovery checkpoint
    has to count as state even when every row family is empty."""
    cache = _cache()
    conn = _open_cache(core)
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert cache._store_codex_recovery_progress(conn, _progress())
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        changed = cache._clear_codex_derived_rows(conn)
        conn.commit()
        assert changed is True
    finally:
        conn.close()
