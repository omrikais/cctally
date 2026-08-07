"""#496 S5b Stage 1 — the epoch-1009 durable selector state.

`resolve_effective_events` accumulates six things over the record stream and
discards all but the summary, so every live tick that meets a correction record
re-derives them by reading the whole journal prefix. Stage 1 makes those
accumulators durable in the disposable stats index, which is what lets Stage 2
seed from them instead.

Durable state that nothing validates is the failure shape epic #496 exists to
remove, so the rebuild's semantic oracle and `stats_index_matches_journal_prefix`
both compare every durable accumulator against a full selection over the same
pinned traversal.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import journal_fixture_496_s5b as F  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


@pytest.fixture
def core(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return importlib.import_module("_cctally_core")


def _open_stats(core, tmp_path, name="scratch-stats.db"):
    return core.open_db(_target_path=str(tmp_path / name))


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

def test_epoch_1009_creates_the_four_new_tables(core, tmp_path):
    conn = _open_stats(core, tmp_path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1009
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert {
            "journal_selector_state",
            "journal_selector_batches",
            "journal_selector_batch_records",
            "stats_quota_projection_state",
        } <= names
    finally:
        conn.close()


def test_effective_events_and_violations_gained_their_columns(core, tmp_path):
    conn = _open_stats(core, tmp_path)
    try:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(journal_effective_events)")
        }
        assert {"winning_sequence", "conflict_hashes_json"} <= columns
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(journal_protocol_violations)")
        }
        assert "available_after" in columns
    finally:
        conn.close()


def test_the_selector_state_table_is_structurally_single_row(core, tmp_path):
    """`journal_selector_state` is a one-row table, unlike
    `stats_publication_stamp`, whose duplicate row is a state that must resolve
    INDETERMINATE and therefore may not be made impossible."""
    conn = _open_stats(core, tmp_path)
    try:
        conn.execute(
            "INSERT INTO journal_selector_state (id, selector_version) "
            "VALUES (1, 1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal_selector_state (id, selector_version) "
                "VALUES (2, 1)")
    finally:
        conn.close()


def test_the_batch_status_domain_is_closed(core, tmp_path):
    conn = _open_stats(core, tmp_path)
    try:
        for status in ("begin_only", "completed", "tainted"):
            conn.execute(
                "INSERT INTO journal_selector_batches (batch_id, status) "
                "VALUES (?, ?)", (f"b:{status}", status))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO journal_selector_batches (batch_id, status) "
                "VALUES ('b:bogus', 'partially_tainted')")
    finally:
        conn.close()


def test_no_fourteenth_stats_migration_was_added(core):
    """The registry stays frozen at 13; a schema change is an epoch bump."""
    db = importlib.import_module("_cctally_db")
    assert len(db._STATS_MIGRATIONS) == 13
    assert core.LEGACY_STATS_HEAD == 13


def test_the_rebuild_table_contract_covers_the_new_tables(core, tmp_path):
    """A scratch index carrying the four new tables must VALIDATE, which it
    only does once `_REBUILD_REQUIRED_TABLES` and the hardcoded schema
    fingerprint moved with the epoch."""
    jr = importlib.import_module("_cctally_journal")
    assert {
        "journal_selector_state",
        "journal_selector_batches",
        "journal_selector_batch_records",
        "stats_quota_projection_state",
    } <= jr._REBUILD_REQUIRED_TABLES
    conn = _open_stats(core, tmp_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO journal_cursor "
            "(id, segment, offset, applied_segment, applied_offset) "
            "VALUES (1, 'observations-2026-07.jsonl', 10, "
            "'observations-2026-07.jsonl', 10)")
        conn.commit()
        jr._validate_rebuilt_stats_index(
            conn, ("observations-2026-07.jsonl", 10))
    finally:
        conn.close()


def test_the_schema_fingerprint_is_a_hardcoded_literal():
    """Deriving it at runtime turns a tripwire into a tautology."""
    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "bin" / "_cctally_journal.py"
    ).read_text(encoding="utf-8")
    marker = "_REBUILD_SCHEMA_FINGERPRINT = ("
    start = source.index(marker) + len(marker)
    literal = source[start:source.index(")", start)].strip()
    assert literal.startswith('"') and literal.endswith('"'), literal
    assert len(literal.strip('"')) == 64, literal


def test_the_doctor_fallback_epoch_matches_the_constant(core):
    """`bin/_lib_doctor.py`'s fallback claims lockstep with
    `STATS_INDEX_EPOCH` and sat eight epochs stale at 1000.

    The gather layer injects the real constant, so the fallback only guards a
    hand-built `DoctorState` that omitted `epoch` — which is exactly what this
    builds. A stale fallback reports a current index as an index MISMATCH.
    """
    import dataclasses

    doctor = importlib.import_module("_lib_doctor")
    fields = {
        field.name: (
            field.default if field.default is not dataclasses.MISSING else None
        )
        for field in dataclasses.fields(doctor.DoctorState)
    }
    fields["stats_db_status"] = {
        "user_version": core.STATS_INDEX_EPOCH,
        "registry_size": core.LEGACY_STATS_HEAD,
    }
    result = doctor._check_db_version_ahead(doctor.DoctorState(**fields))
    assert result.details["stats.db"]["epoch"] == core.STATS_INDEX_EPOCH
    assert result.severity == "ok", result.summary


# --------------------------------------------------------------------------
# the pure kernel
# --------------------------------------------------------------------------

def _kernel():
    return importlib.import_module("_lib_selector_state")


def _select(records):
    """Full selection plus the accumulator out-dict, as one bundle."""
    jl = importlib.import_module("_lib_journal")
    accumulators: dict = {}
    selection = jl.resolve_effective_events(records, accumulators=accumulators)
    return selection, accumulators


def _rows(records, **kwargs):
    ks = _kernel()
    selection, accumulators = _select(records)
    kwargs.setdefault("next_sequence", ks.decoded_entry_count(records))
    return ks.rows_from_selection(
        selection, accumulators=accumulators, **kwargs)


def test_the_kernel_never_imports_the_journal_glue():
    """A pure kernel must be unit-testable without a journal on disk — the same
    rule `bin/_lib_journal_router.py` follows."""
    import ast

    source = (
        pathlib.Path(__file__).resolve().parent.parent
        / "bin" / "_lib_selector_state.py"
    ).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("_cctally_") for name in imported), imported


def test_decoded_entry_count_counts_placeholders_and_not_malformed_lines():
    """Sequence numbering is load-bearing: three structural violation kinds
    hash the enumerate() index into a DURABLE fingerprint, so a quota-only
    segment contributes placeholders rather than zero."""
    ks = _kernel()
    records = [
        F._evt(F.EVENT_CORRECTED, 1.0),
        F.quota_obs(F.AT, 0),
        F.quota_obs(F.AT, 1),
        F._claude_obs(F.AT, 2.0),
    ]
    assert ks.decoded_entry_count(records) == 4
    assert ks.decoded_entry_count([*records, ks.MALFORMED]) == 4
    assert ks.decoded_entry_count([ks.MALFORMED, ks.MALFORMED]) == 0


def test_decoded_entry_count_matches_the_selector_sequence_space(core, tmp_path):
    """Non-vacuity: the count must equal the number of `enumerate` positions the
    selector actually consumed, or an elided segment would renumber the ones
    after it."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    selection, _accumulators = _select(fixture["records"])
    assert ks.decoded_entry_count(fixture["records"]) == len(fixture["records"])
    assert max(
        selected.sequence for selected in selection.by_id.values()
    ) < ks.decoded_entry_count(fixture["records"])


def test_rows_keep_one_row_per_event_id_with_the_distinct_hash_set(core):
    """No per-candidate table: same-revision containment needs only the winning
    revision, the lowest-sequence winner and the distinct hash set."""
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    by_event = {row.event_id: row for row in rows.effective}
    assert len(rows.effective) == len(by_event)
    conflict = by_event[F.EVENT_CONFLICT]
    hashes = json.loads(conflict.conflict_hashes_json)
    assert len(hashes) == 2 and conflict.content_hash in hashes
    assert by_event[F.EVENT_CORRECTED].conflict_hashes_json is None
    assert by_event[F.EVENT_CORRECTED].batch_id == F.BATCH_COMPLETED


def test_every_batch_lands_in_the_status_its_records_imply(core):
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    status = {row.batch_id: row.status for row in rows.batches}
    assert status == {
        F.BATCH_COMPLETED: "completed",
        F.BATCH_BEGIN_ONLY: "begin_only",
        F.BATCH_TAINTED: "tainted",
    }


def test_action_cores_are_retained_while_begin_only_and_while_tainted(core):
    """An early taint does not end a batch's record stream: later actions and a
    commit can still establish `manifest_actions_hash_mismatch`, whose
    derivation hashes every first-seen core."""
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    actions = [row for row in rows.batch_records if row.kind == "action"]
    retained = [
        row for row in actions if row.batch_id == F.BATCH_BEGIN_ONLY]
    assert retained and all(row.action_core_json for row in retained)
    for row in retained:
        assert set(json.loads(row.action_core_json)) == {
            "action", "id", "rev", "at", "src", "payload"}


def test_action_cores_are_dropped_once_completed_but_the_digest_survives(core):
    """A duplicate that matches changes nothing; one that conflicts is detected
    from the retained whole-record digest."""
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    completed = [
        row for row in rows.batch_records
        if row.batch_id == F.BATCH_COMPLETED and row.kind == "action"
    ]
    assert completed
    assert all(row.action_core_json is None for row in completed)
    assert all(row.record_digest for row in completed)


def test_marker_rows_carry_both_digests(core):
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    markers = [row for row in rows.batch_records if row.kind == "marker"]
    assert {row.key for row in markers} >= {"begin", "commit"}
    assert all(row.record_digest and row.identity_digest for row in markers)


def test_violation_rows_carry_their_available_after(core):
    """A `journal_protocol_resolution` op that precedes the violation it names
    is fatal, so the minimum sequence has to survive into durable state."""
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    assert rows.violations
    for row in rows.violations:
        assert row.available_after is not None
        assert json.loads(row.violation_json)["fingerprint"] == row.fingerprint


def test_marker_coordinates_come_from_the_pass_that_read_them(core):
    """The earliest-commit coordinate is what removes
    `_correction_commit_high_water`'s traversal from the fast path."""
    fixture = F.build_selector_scenarios(core.APP_DIR)
    coordinates = {
        index: ("observations-2026-07.jsonl", 100 + index)
        for index, record in enumerate(fixture["records"])
        if record.get("t") == "correction_batch"
    }
    rows = _rows(fixture["records"], coordinates=coordinates)
    completed = next(
        row for row in rows.batches if row.batch_id == F.BATCH_COMPLETED)
    assert completed.begin_offset is not None
    assert completed.earliest_commit_offset is not None
    assert completed.earliest_commit_offset > completed.begin_offset


@pytest.mark.parametrize("split", [1, 4, 6, 9, 11])
def test_merge_delta_matches_full_selection(core, split):
    """The oracle property: incremental never diverges from full."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    full = _rows(records, covered=("observations-2026-08.jsonl", 4242))
    head, tail = records[:split], records[split:]
    merged, transitions = ks.merge_delta(
        _rows(head),
        tail,
        next_sequence=ks.decoded_entry_count(head),
        covered=("observations-2026-08.jsonl", 4242),
    )
    assert transitions == []
    assert merged == full


def test_merge_delta_reports_a_completed_to_tainted_transition(core):
    """A later record conflicting with a durably completed batch is the one
    case an incremental path could make the pre-existing #510 staleness worse,
    so it is named rather than absorbed."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    rows = _rows(records)
    conflicting = _conflicting_duplicate_commit(records)
    merged, transitions = ks.merge_delta(
        rows, [conflicting], next_sequence=len(records))
    assert [t.batch_id for t in transitions] == [F.BATCH_COMPLETED]
    assert transitions[0].causal_sequence == len(records)
    status = {row.batch_id: row.status for row in merged.batches}
    assert status[F.BATCH_COMPLETED] == "tainted"


def test_two_simultaneous_transitions_are_ordered_by_cause(core):
    """Both orders converge, but only causal order converges through the
    NARROWEST prefix, and the caller raises on the first transition.

    With a single transition the two orders are indistinguishable, so a
    regression from causal order back to batch-id order would go unnoticed. This
    fixture makes them disagree: the delta taints beta first and alpha second,
    while batch-id order is alpha then beta.
    """
    ks = _kernel()
    scenario = F.two_completed_batches_scenario()
    rows = _rows(scenario["seed"])
    assert {row.batch_id: row.status for row in rows.batches} == {
        scenario["alpha"]: "completed",
        scenario["beta"]: "completed",
    }, "both batches must be durably COMPLETED before the delta arrives"

    merged, transitions = ks.merge_delta(
        rows,
        scenario["delta"],
        next_sequence=ks.decoded_entry_count(scenario["seed"]),
    )
    order = [item.batch_id for item in transitions]
    assert order == [scenario["beta"], scenario["alpha"]]
    assert transitions[0].causal_sequence < transitions[1].causal_sequence
    assert order != sorted(order), (
        "causal order and batch-id order must differ on this input, or the "
        "assertion above cannot distinguish them"
    )
    assert {row.batch_id: row.status for row in merged.batches} == {
        scenario["alpha"]: "tainted",
        scenario["beta"]: "tainted",
    }


def test_merge_delta_refuses_a_new_action_sequence_for_a_completed_batch(core):
    """Phase 1 taints only on a DUPLICATE whose digest differs, so a correction
    at a sequence the batch never held raises nothing there — and phase 2 is
    skipped for a durably completed batch because its cores were dropped.

    Without a refusal the two paths diverge on exactly this input: a full pass
    re-runs phase 2 over the widened action set, taints the batch and reverts
    the winner to revision 0, while the incremental path keeps the batch
    completed and leaves the correction standing.
    """
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    extra = _extra_action_for(records, F.BATCH_COMPLETED, seq=1)

    # The divergence the refusal exists to prevent, measured on the full path.
    full = _rows([*records, extra])
    assert {row.batch_id: row.status
            for row in full.batches}[F.BATCH_COMPLETED] == "tainted"
    winner = next(
        row for row in full.effective if row.event_id == F.EVENT_CORRECTED)
    assert (winner.rev, winner.batch_id) == (0, None)
    assert "manifest_action_sequence_mismatch" in {
        row.kind for row in full.violations
        if row.batch_id == F.BATCH_COMPLETED
    }

    with pytest.raises(ks.IncrementalSelectionUnavailable, match="completed"):
        ks.merge_delta(_rows(records), [extra], next_sequence=len(records))


def test_merge_delta_refuses_a_new_marker_phase_for_a_completed_batch(core):
    """The same rule on the marker axis.

    A well-formed completed batch holds both `begin` and `commit`, so this state
    is not reachable from an intact generation — it is constructed directly,
    because the guard covers two axes and only one of them has a natural input.
    """
    import dataclasses

    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    rows = _rows(records)
    stripped = dataclasses.replace(
        rows,
        batch_records=tuple(
            row for row in rows.batch_records
            if not (row.batch_id == F.BATCH_COMPLETED
                    and row.kind == "marker" and row.key == "commit")
        ),
    )
    commit = _commit_marker(records, F.BATCH_COMPLETED)
    with pytest.raises(ks.IncrementalSelectionUnavailable, match="completed"):
        ks.merge_delta(stripped, [commit], next_sequence=len(records))


def test_merge_delta_still_carries_a_completed_batch_over_a_duplicate(core):
    """Non-vacuity for both refusals: a DUPLICATE marker at a phase the durable
    rows already hold is safe to carry forward, so it must NOT refuse."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    identical = _commit_marker(records, F.BATCH_COMPLETED)
    merged, transitions = ks.merge_delta(
        _rows(records), [identical], next_sequence=len(records))
    assert transitions == []
    assert {row.batch_id: row.status
            for row in merged.batches}[F.BATCH_COMPLETED] == "completed"


def test_merge_delta_refuses_a_delta_carrying_a_resolution_op(core):
    """Acknowledging a violation authenticates an exact length-framed
    raw-prefix SHA-256, and no durable summary can reconstruct it."""
    ks = _kernel()
    jl = importlib.import_module("_lib_journal")
    fixture = F.build_selector_scenarios(core.APP_DIR)
    rows = _rows(fixture["records"])
    selection, _accumulators = _select(fixture["records"])
    resolution = jl.make_protocol_resolution(
        at=F.LATER,
        violations=list(selection.protocol_violations),
        journal_high_water=("observations-2026-08.jsonl", 10),
        journal_prefix_hash="sha256:" + "0" * 64,
    )
    with pytest.raises(ks.IncrementalSelectionUnavailable):
        ks.merge_delta(
            rows, [resolution], next_sequence=len(fixture["records"]))


def test_a_split_cycle_batch_completes_from_the_stored_cores(core):
    """The case core retention exists for: begin and actions persisted in an
    earlier generation, commit arriving in this tick."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    commit = _commit_marker(records, F.BATCH_BEGIN_ONLY)
    rows = _rows(records)
    merged, transitions = ks.merge_delta(
        rows, [commit], next_sequence=len(records))
    assert transitions == []
    status = {row.batch_id: row.status for row in merged.batches}
    assert status[F.BATCH_BEGIN_ONLY] == "completed"
    winner = next(
        row for row in merged.effective if row.event_id == F.EVENT_PENDING)
    assert winner.rev == 1 and winner.batch_id == F.BATCH_BEGIN_ONLY
    # Spec §7 asks for the manifest property DIRECTLY. Completion implies it,
    # but a failure of it would surface here as a bare status mismatch with no
    # indication of which hash moved.
    jl = importlib.import_module("_lib_journal")
    stored = sorted(
        (row for row in rows.batch_records
         if row.batch_id == F.BATCH_BEGIN_ONLY and row.kind == "action"),
        key=lambda row: int(row.key),
    )
    begin_row = next(
        row for row in rows.batches if row.batch_id == F.BATCH_BEGIN_ONLY)
    actual_actions_hash = jl._sha256_canonical(
        [json.loads(row.action_core_json) for row in stored])
    assert actual_actions_hash == begin_row.action_set_hash
    # And the completion agrees with a full selection over the same stream.
    assert merged.effective == _rows([*records, commit]).effective


def test_a_placeholder_does_not_renumber_a_later_violation(core):
    """A non-retained record consumes a sequence number. If it did not, the
    fingerprints after it would move — and they are durable."""
    ks = _kernel()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    records = fixture["records"]
    with_placeholders = [
        None if record.get("t") == "obs" else record for record in records
    ]
    assert _fingerprints(records) == _fingerprints(with_placeholders)
    without = [record for record in records if record.get("t") != "obs"]
    assert _fingerprints(records) != _fingerprints(without), (
        "the fixture must place a non-retained record BEFORE the violation, "
        "or this test cannot observe a renumbering"
    )
    assert ks.decoded_entry_count(records) == len(records)


def test_a_tainted_batch_that_carries_actions_retains_its_cores(core):
    """`build_selector_scenarios`' tainted batch is an ORPHAN COMMIT, so it has
    no action rows and the retention half of the rule is unobservable there.

    This is the shape spec §7 case 1 names: a tainted batch that still receives
    later actions, which is what forces cores to survive past taint.
    """
    scenario = F.tainted_action_scenario()
    rows = _rows(scenario["seed"])
    status = {row.batch_id: row.status for row in rows.batches}
    assert status[F.BATCH_TAINTED_ACTIONS] == "tainted"
    actions = [
        row for row in rows.batch_records
        if row.batch_id == F.BATCH_TAINTED_ACTIONS and row.kind == "action"
    ]
    assert actions, "the scenario must give the TAINTED batch an action row"
    for row in actions:
        assert set(json.loads(row.action_core_json)) == {
            "action", "id", "rev", "at", "src", "payload"}


def test_a_later_action_derives_the_manifest_check_from_the_retained_core(core):
    """Why the retention rule exists: an early taint does not end the record
    stream, and phase 2 hashes EVERY first-seen action core to decide
    `manifest_actions_hash_mismatch` — including one only the durable row holds.
    """
    ks = _kernel()
    scenario = F.tainted_action_scenario(
        declared_actions_hash=F.WRONG_ACTIONS_HASH)
    seed, delta = scenario["seed"], scenario["delta"]
    merged, transitions = ks.merge_delta(
        _rows(seed), delta, next_sequence=ks.decoded_entry_count(seed))
    assert transitions == []
    assert "manifest_actions_hash_mismatch" in {
        row.kind for row in merged.violations
        if row.batch_id == F.BATCH_TAINTED_ACTIONS
    }
    assert merged == _rows([*seed, *delta])


def test_a_later_action_does_not_invent_a_manifest_mismatch(core):
    """Non-vacuity for the case above: with the manifest the actions really
    produce, the retained core hashes back to it and no violation is added."""
    ks = _kernel()
    scenario = F.tainted_action_scenario()
    seed, delta = scenario["seed"], scenario["delta"]
    merged, _transitions = ks.merge_delta(
        _rows(seed), delta, next_sequence=ks.decoded_entry_count(seed))
    assert {row.kind for row in merged.violations} == {
        "action_sequence_conflict"}
    assert merged == _rows([*seed, *delta])


def test_a_withdrawn_violation_does_not_survive_in_the_durable_rows(core):
    """A phase-2 verdict the next delta withdraws must LEAVE the durable set.

    The seed commits a batch declaring two actions while carrying one, which is
    `manifest_action_sequence_mismatch`. The delta delivers the missing action,
    so the batch is re-resolved and a full pass now reports only
    `record_order_violation`. A merge that UNIONS stored rows with new ones
    keeps the withdrawn kind durable, and `_check_journal_protocol` reads that
    table: a stale row makes `doctor` exit 2 and hands the user a
    `db journal-repair --violation <fingerprint>` command for a fingerprint no
    fresh derivation reproduces.
    """
    ks = _kernel()
    scenario = F.withdrawn_violation_scenario()
    seed, delta = scenario["seed"], scenario["delta"]

    seeded = _rows(seed)
    assert {row.kind for row in seeded.violations} == {
        "manifest_action_sequence_mismatch"
    }, "the seed must establish the kind the delta then withdraws"
    full = _rows([*seed, *delta])
    assert {row.kind for row in full.violations} == {"record_order_violation"}, (
        "the full pass over the concatenated stream must NOT report the "
        "withdrawn kind, or this test proves nothing"
    )

    merged, transitions = ks.merge_delta(
        seeded, delta, next_sequence=ks.decoded_entry_count(seed))
    assert transitions == []
    assert {row.kind for row in merged.violations} == {
        "record_order_violation"}
    assert merged == full


def test_a_phase_one_violation_is_never_withdrawn(core):
    """The counterweight: phase 1 taints from a DUPLICATE record that stays in
    the journal forever, and `_seed_fold` restores only the FIRST record at each
    key — so an incremental re-resolution cannot re-derive it and must not read
    its absence as a withdrawal.

    `tainted_action_scenario` seeds a byte-different duplicate of action 0, and
    its delta re-resolves the same batch, so the withdrawal rule runs over a
    stored row it must keep.
    """
    ks = _kernel()
    scenario = F.tainted_action_scenario()
    seed, delta = scenario["seed"], scenario["delta"]
    seeded = _rows(seed)
    assert {row.kind for row in seeded.violations} == {
        "action_sequence_conflict"}
    merged, _transitions = ks.merge_delta(
        seeded, delta, next_sequence=ks.decoded_entry_count(seed))
    assert "action_sequence_conflict" in {row.kind for row in merged.violations}
    assert merged == _rows([*seed, *delta])


def test_the_delta_writer_deletes_a_withdrawn_violation_row(core, tmp_path):
    """The kernel's withdrawal has to reach the durable table, or `doctor` keeps
    reading the stale row.

    The write path expresses upserts for three of the four groups, so a
    withdrawn violation stayed durable even after the kernel stopped producing
    it. The removal is scoped: `before.violations` is the delta's own scoped
    read, so the difference can only name rows this delta looked at.
    """
    jr = importlib.import_module("_cctally_journal")
    ks = _kernel()
    scenario = F.withdrawn_violation_scenario()
    seed, delta = scenario["seed"], scenario["delta"]
    before = _rows(seed)
    after, _transitions = ks.merge_delta(
        before, delta, next_sequence=ks.decoded_entry_count(seed))

    conn = _open_stats(core, tmp_path, name="withdrawal-stats.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr._write_selector_state(conn, before)
        conn.commit()
        stored = {
            row[0] for row in conn.execute(
                "SELECT kind FROM journal_protocol_violations")
        }
        assert stored == {"manifest_action_sequence_mismatch"}, (
            "the seeded generation must hold the row the delta withdraws"
        )
        conn.execute("BEGIN IMMEDIATE")
        jr._write_selector_delta(conn, before, after)
        conn.commit()
        assert {
            row[0] for row in conn.execute(
                "SELECT kind FROM journal_protocol_violations")
        } == {"record_order_violation"}
    finally:
        conn.close()


def test_a_violation_whose_batch_was_not_re_resolved_survives(core):
    """The withdrawal is SCOPED to the batches phase 2 actually re-resolved.

    A stored violation naming a batch the seeded fold holds no marker or action
    for is outside that set, so `fold.violations` says nothing about it and the
    row stands. Without the scope the row would be dropped on exactly this
    input, because its fingerprint is not re-derived, its kind is a phase-2 kind
    and it carries no operator audit.
    """
    ks = _kernel()
    scenario = F.withdrawn_violation_scenario()
    seeded = _rows(scenario["seed"])
    assert len(seeded.violations) == 1
    orphaned = seeded.violations[0]
    assert orphaned.kind not in ks.PHASE_ONE_VIOLATION_KINDS
    stripped = ks.SelectorRows(
        state=seeded.state,
        batches=(),
        batch_records=(),
        effective=seeded.effective,
        violations=seeded.violations,
    )
    merged, _transitions = ks.merge_delta(
        stripped,
        [F.quota_obs(F.LATER, 78)],
        next_sequence=seeded.state.next_sequence,
    )
    assert merged.violations == (orphaned,)


def test_an_acknowledged_violation_survives_its_own_withdrawal(core):
    """The exemption that keeps an operator audit from vanishing.

    `AcknowledgedProtocolViolation.to_dict` adds `auditId` to the plain
    violation dict, and an incremental fold never carries a resolution, so a
    re-derivation cannot reproduce that shape. Without the exemption the delta
    would withdraw the row — a violation disappearing from the durable table
    that a full derivation still reports, which is the mirror image of the
    stale-row defect the withdrawal exists to fix.
    """
    ks = _kernel()
    scenario = F.withdrawn_violation_scenario()
    seeded = _rows(scenario["seed"])
    assert len(seeded.violations) == 1, "the seed must hold exactly the row"
    plain = seeded.violations[0]
    assert plain.kind not in ks.PHASE_ONE_VIOLATION_KINDS, (
        "a phase-1 kind would be exempt for a different reason"
    )

    # Non-vacuity, stated as a run rather than as a claim: the SAME delta over
    # the SAME row withdraws it when the row is not acknowledged.
    withdrawn, _t = ks.merge_delta(
        seeded, scenario["delta"],
        next_sequence=ks.decoded_entry_count(scenario["seed"]))
    assert plain.fingerprint not in {
        row.fingerprint for row in withdrawn.violations}

    acknowledged = ks.SelectorRows(
        state=seeded.state,
        batches=seeded.batches,
        batch_records=seeded.batch_records,
        effective=seeded.effective,
        violations=(
            ks.SelectorViolationRow(
                fingerprint=plain.fingerprint,
                batch_id=plain.batch_id,
                kind=plain.kind,
                violation_json=json.dumps(
                    {**json.loads(plain.violation_json),
                     "auditId": "audit:s5b-remediation",
                     "journalHighWater": ["observations-2026-01.jsonl", 4096],
                     "journalPrefixHash": "0" * 64},
                    separators=(",", ":"), sort_keys=True),
                available_after=plain.available_after,
            ),
        ),
    )
    kept, _transitions = ks.merge_delta(
        acknowledged, scenario["delta"],
        next_sequence=ks.decoded_entry_count(scenario["seed"]))
    survivor = next(
        row for row in kept.violations if row.fingerprint == plain.fingerprint)
    assert "auditId" in json.loads(survivor.violation_json)


def _violation_row(available_after):
    ks = _kernel()
    return ks.SelectorViolationRow(
        fingerprint="fp-1", batch_id="batch-1", kind="marker_conflict",
        violation_json='{"kind":"marker_conflict"}',
        available_after=available_after,
    )


@pytest.mark.parametrize("stored,derived,expected", [
    # A derived row with no sighting contributes nothing.
    (12, None, 12),
    (None, None, None),
    # The NULL-stored branch, which is the one that actually fires: a durable
    # row written before the column carried a value takes the derived sighting.
    (None, 7, 7),
    # `SelectorFold.taint` accumulates as a MINIMUM keyed by fingerprint, so the
    # earlier of the two wins in both directions.
    (12, 7, 7),
    (7, 12, 7),
    (7, 7, 7),
])
def test_the_earlier_available_after_wins_in_every_direction(
    stored, derived, expected,
):
    """`available_after` is not one of the fingerprint's inputs, so a stored row
    and a re-derived one can legitimately disagree on it. It is a MINIMUM over
    sightings rather than a last-write value, so the merge takes the earlier —
    including when the stored side is NULL, which is the branch a durable row
    written before this column carried a value actually takes."""
    ks = _kernel()
    merged = ks._with_earliest_available_after(
        _violation_row(stored),
        None if derived is None else _violation_row(derived),
    )
    assert merged.available_after == expected


def test_a_missing_derived_row_returns_the_stored_row_unchanged():
    """`derived` is None when the re-resolution did not reproduce the
    fingerprint at all — an acknowledged or phase-1 row. Nothing about the
    stored row may change on that path."""
    ks = _kernel()
    stored = _violation_row(12)
    assert ks._with_earliest_available_after(stored, None) is stored


def test_the_phase_one_kind_set_matches_what_fold_one_raises():
    """A tripwire over `PHASE_ONE_VIOLATION_KINDS`, not a restatement of it.

    A phase-1 kind added to `_fold_one` later and not added to the set would
    become silently WITHDRAWABLE, because the withdrawal branch exempts only the
    kinds the set names. The expected kinds are written here as LITERALS: a test
    that imported the constant it is pinning would pass for any value.
    """
    import ast

    ks = _kernel()
    source = (
        pathlib.Path(__file__).resolve().parent.parent / "bin" / "_lib_journal.py"
    ).read_text(encoding="utf-8")
    fold_one = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_fold_one"
    )
    # The KIND argument specifically — `_protocol_violation(batch_id, kind,
    # **evidence)` — not every string constant that happens to be positional. A
    # kind passed as a variable resolves to None and is collected, so it FAILS
    # here rather than vanishing from the comparison.
    calls = [
        call for call in ast.walk(fold_one)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_protocol_violation"
    ]
    assert calls, "the scan must find calls, or this test proves nothing"
    raised = set()
    for call in calls:
        kind = call.args[1] if len(call.args) > 1 else None
        raised.add(
            kind.value
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str)
            else None
        )
    assert raised == {"marker_conflict", "action_sequence_conflict"}
    assert set(ks.PHASE_ONE_VIOLATION_KINDS) == {
        "marker_conflict", "action_sequence_conflict"}


def test_merge_delta_matches_full_selection_on_a_legacy_qaa_stream(core):
    """`_merge_candidates` is a second implementation of the same-revision
    containment rules, and the legacy `qaa` carve-out — last-wins AND silent —
    had no incremental coverage. This is the oracle case for it."""
    ks = _kernel()
    scenario = F.legacy_qaa_scenario()
    seed, delta = scenario["seed"], scenario["delta"]
    merged, transitions = ks.merge_delta(
        _rows(seed), delta, next_sequence=ks.decoded_entry_count(seed))
    assert transitions == []
    full = _rows([*seed, *delta])
    assert merged == full
    # Non-vacuity: the carve-out must actually have fired. Last-wins means the
    # later line is the winner, and SILENT means no conflict was recorded.
    winner = next(
        row for row in merged.effective
        if row.event_id == scenario["event_id"])
    assert winner.conflict_hashes_json is None
    assert json.loads(winner.event_json)["payload"]["threshold"] == 75


def _sequenceless(rows, event_id):
    """``rows`` with one winner's `winning_sequence` cleared.

    That is what the LIVE emit path leaves behind: `_insert_effective_metadata`
    writes the six legacy columns for an evt the cycle journals past its own
    high-water, and it has no sequence to give the row.
    """
    import dataclasses

    return dataclasses.replace(
        rows,
        effective=tuple(
            dataclasses.replace(row, winning_sequence=None)
            if row.event_id == event_id else row
            for row in rows.effective
        ),
    )


def test_a_sequenceless_prior_is_adopted_when_the_candidate_matches(core):
    """The adoption rule in `_merge_candidates`, tested where it lives.

    A durable row with no winning sequence came from the live emit path. The
    next cycle reads that same journaled line and folds it at a KNOWN sequence,
    so adopting the candidate replaces an unknown with the number a full
    derivation computes. Without it the row stays sequenceless forever and
    `stats_index_matches_journal_prefix` can never agree again. Its only other
    coverage was end to end, where a failure does not localize to this branch.
    """
    ks = _kernel()
    evt = F._evt(F.EVENT_PENDING, 40.0)
    seed = [F._evt(F.EVENT_CORRECTED, 20.0), evt]
    rows = _sequenceless(_rows(seed), F.EVENT_PENDING)
    assert next(
        row for row in rows.effective if row.event_id == F.EVENT_PENDING
    ).winning_sequence is None, "the fixture must start sequenceless"

    # The SAME record read back out of the journal on the next cycle.
    merged, transitions = ks.merge_delta(
        rows, [evt], next_sequence=len(seed))
    assert transitions == []
    adopted = next(
        row for row in merged.effective if row.event_id == F.EVENT_PENDING)
    assert adopted.winning_sequence == len(seed), (
        "the candidate's sequence must replace the unknown"
    )
    assert adopted.conflict_hashes_json is None, (
        "adopting a byte-identical record is not a conflict"
    )
    untouched = next(
        row for row in merged.effective if row.event_id == F.EVENT_CORRECTED)
    assert untouched.winning_sequence == 0, (
        "a row the delta did not name must pass through verbatim"
    )


def test_a_diverging_same_revision_candidate_does_not_adopt(core):
    """The other half of the rule: adoption is gated on equal rev, content hash
    and status. A candidate that diverges at the winning revision must leave the
    prior winner standing and record the conflict instead — otherwise a
    sequenceless row would be a licence for any later line to overwrite it."""
    ks = _kernel()
    seed = [F._evt(F.EVENT_PENDING, 40.0, source="fixture-a")]
    rows = _sequenceless(_rows(seed), F.EVENT_PENDING)
    diverging = F._evt(F.EVENT_PENDING, 41.0, source="fixture-b")
    assert diverging["id"] == F.EVENT_PENDING

    merged, transitions = ks.merge_delta(
        rows, [diverging], next_sequence=len(seed))
    assert transitions == []
    row = next(
        item for item in merged.effective if item.event_id == F.EVENT_PENDING)
    prior = next(
        item for item in rows.effective if item.event_id == F.EVENT_PENDING)
    assert row.content_hash == prior.content_hash, (
        "the PRIOR winner stands; the divergent candidate does not take it"
    )
    hashes = json.loads(row.conflict_hashes_json)
    assert sorted(hashes) == sorted({prior.content_hash, *hashes})
    assert len(hashes) == 2, hashes


def _extra_action_for(records, batch_id, seq):
    """A correction record for ``batch_id`` at a sequence it does not hold."""
    for record in records:
        if (record.get("t") == "correction"
                and record.get("batch") == batch_id):
            assert record.get("seq") != seq
            return {**record, "seq": seq}
    raise AssertionError(f"fixture has no correction action for {batch_id}")


def _fingerprints(records):
    jl = importlib.import_module("_lib_journal")
    return sorted(
        violation.fingerprint
        for violation in jl.resolve_effective_events(
            records).protocol_violations
    )


def _commit_marker(records, batch_id):
    for record in records:
        if (record.get("t") == "correction_batch"
                and record.get("id") == batch_id
                and record.get("phase") == "begin"):
            return {**record, "phase": "commit"}
    raise AssertionError(f"no begin marker for {batch_id}")


def _conflicting_duplicate_commit(records):
    """A second commit marker for the completed batch, byte-different."""
    for record in records:
        if (record.get("t") == "correction_batch"
                and record.get("id") == F.BATCH_COMPLETED
                and record.get("phase") == "commit"):
            return {**record, "at": "2026-09-09T09:09:09Z"}
    raise AssertionError("fixture has no completed-batch commit marker")


# --------------------------------------------------------------------------
# rebuild population
# --------------------------------------------------------------------------

def _rebuild(tmp_path, name="rebuilt-stats.db"):
    jr = importlib.import_module("_cctally_journal")
    dest = tmp_path / name
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest),
    )
    return dest


def test_a_rebuild_populates_selector_state_matching_full_selection(
    core, tmp_path
):
    """The scratch index's durable selector state must equal what a full
    selection over the same pinned traversal produces."""
    jr = importlib.import_module("_cctally_journal")
    fixture = F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path)
    conn = sqlite3.connect(dest)
    try:
        stored = jr._read_selector_rows(conn)
    finally:
        conn.close()
    expected = _rows(
        fixture["records"],
        coordinates=_marker_coordinates(core, fixture),
        covered=fixture["high_water"],
    )
    assert stored == expected


def test_the_selector_prefix_equals_the_applied_cursor(core, tmp_path):
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path)
    conn = sqlite3.connect(dest)
    try:
        cursor = conn.execute(
            "SELECT applied_segment, applied_offset FROM journal_cursor"
        ).fetchone()
        state = jr._read_selector_rows(conn).state
    finally:
        conn.close()
    assert (state.covered_segment, state.covered_offset) == tuple(cursor)


def test_the_generation_identity_is_absent_until_publication(core, tmp_path):
    """`stats_publication_stamp` is written AFTER the scratch is built, so a
    row populated during the scratch build cannot carry the identity it will
    publish under. Stage 2 sets it inside the publication transaction."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path)
    conn = sqlite3.connect(dest)
    try:
        state = jr._read_selector_rows(conn).state
    finally:
        conn.close()
    assert state.generation_record_path is None
    assert state.generation_stamped_at_utc is None
    assert state.selector_version == _kernel().SELECTOR_VERSION


def test_the_rebuild_records_the_earliest_commit_coordinate(core, tmp_path):
    """This is what removes `_correction_commit_high_water`'s traversal from
    the live fast path, so it must agree with that function exactly."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path)
    conn = sqlite3.connect(dest)
    try:
        rows = {row.batch_id: row for row in jr._read_selector_rows(conn).batches}
    finally:
        conn.close()
    completed = rows[F.BATCH_COMPLETED]
    assert (
        completed.earliest_commit_segment,
        completed.earliest_commit_offset,
    ) == jr._correction_commit_high_water(F.BATCH_COMPLETED)


def test_the_rebuild_records_whether_the_prefix_carried_a_cutover_op(
    core, tmp_path
):
    """`cutover_seen` distinguishes "no cutover op exists" from "the op exists
    and recorded no account"; this fixture writes none."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path)
    conn = sqlite3.connect(dest)
    try:
        state = jr._read_selector_rows(conn).state
    finally:
        conn.close()
    assert state.cutover_seen is False
    assert state.cutover_account_key is None


def _marker_coordinates(core, fixture):
    """Sequence -> (segment, end offset) for every correction-batch marker."""
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    coordinates = {}
    sequence = 0
    for segment, offset, raw in jr.iter_range(None, fixture["high_water"]):
        record = jl.decode_line(raw)
        if record is None:
            continue
        if record.get("t") == "correction_batch":
            coordinates[sequence] = (segment, offset + len(raw) + 1)
        sequence += 1
    return coordinates


# --------------------------------------------------------------------------
# semantic validation
# --------------------------------------------------------------------------

def test_wrong_selector_state_in_the_scratch_refuses_publication(
    core, tmp_path, monkeypatch
):
    """Operationally authoritative state that nothing validates would let a
    scratch with correct legacy rows and WRONG selector state be stamped and
    then trusted by the fast path — this epic's own failure shape, one layer
    up. Corrupt one batch's status and the rebuild must refuse."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    real = jr._write_selector_state

    def corrupt(conn, rows):
        real(conn, rows)
        conn.execute(
            "UPDATE journal_selector_batches SET status = 'tainted' "
            "WHERE batch_id = ?", (F.BATCH_COMPLETED,))

    monkeypatch.setattr(jr, "_write_selector_state", corrupt)
    with pytest.raises(jr.JournalError, match="selector"):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            target_path=str(tmp_path / "corrupt.db"),
        )


def test_a_clean_rebuild_still_validates(core, tmp_path):
    """Non-vacuity for the case above: the same fixture publishes fine when
    nothing corrupts it."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path, "clean.db")
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is True


def test_the_prefix_matcher_rejects_wrong_selector_state(core, tmp_path):
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path, "mutated.db")
    conn = sqlite3.connect(dest)
    try:
        conn.execute(
            "UPDATE journal_selector_batches SET status = 'begin_only' "
            "WHERE batch_id = ?", (F.BATCH_COMPLETED,))
        conn.commit()
    finally:
        conn.close()
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is False


def test_the_prefix_matcher_rejects_a_missing_selector_state_row(
    core, tmp_path
):
    """Cardinality is asserted, not assumed. A zero-row state is a degraded
    case, never an empty-but-valid generation."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path, "no-state.db")
    conn = sqlite3.connect(dest)
    try:
        conn.execute("DELETE FROM journal_selector_state")
        conn.commit()
    finally:
        conn.close()
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is False


def test_the_prefix_matcher_rejects_a_dropped_action_core(core, tmp_path):
    """An in-flight batch's cores are what a split cycle completes from, so
    losing one has to fail validation rather than surface as a taint later."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path, "no-core.db")
    conn = sqlite3.connect(dest)
    try:
        conn.execute(
            "UPDATE journal_selector_batch_records SET action_core_json = NULL "
            "WHERE batch_id = ? AND kind = 'action'", (F.BATCH_BEGIN_ONLY,))
        conn.commit()
    finally:
        conn.close()
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is False


def test_the_prefix_matcher_ignores_the_generation_identity(core, tmp_path):
    """Identity is written at PUBLICATION, so a durable generation carries it
    and a fresh derivation cannot. Comparing it would make every validation of
    a published index fail."""
    jr = importlib.import_module("_cctally_journal")
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path, "stamped.db")
    conn = sqlite3.connect(dest)
    try:
        conn.execute(
            "UPDATE journal_selector_state SET generation_record_path = ?, "
            "generation_stamped_at_utc = ?",
            ("/somewhere/record.json", "2026-08-06T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is True


# --------------------------------------------------------------------------
# the fail-closed quota-projection read gate
# --------------------------------------------------------------------------

def _quota():
    return importlib.import_module("_cctally_quota")


def _set_incomplete(conn, value=1):
    conn.execute(
        "INSERT OR REPLACE INTO stats_quota_projection_state "
        "(id, incomplete, target_version, recovery_target_json) "
        "VALUES (1, ?, 1, ?)",
        (value, json.dumps({"targetVersion": 1, "covered": None})),
    )
    conn.commit()


def test_the_gate_raises_the_retry_signal_when_the_flag_is_set(core, tmp_path):
    quota = _quota()
    conn = _open_stats(core, tmp_path)
    try:
        _set_incomplete(conn)
        with pytest.raises(quota.QuotaProjectionIncomplete):
            quota.assert_projection_readable(conn)
    finally:
        conn.close()


def test_the_gate_passes_when_the_flag_is_clear(core, tmp_path):
    """Non-vacuity: an inert gate that always raised would also pass the case
    above."""
    quota = _quota()
    conn = _open_stats(core, tmp_path)
    try:
        quota.assert_projection_readable(conn)
        _set_incomplete(conn, value=0)
        quota.assert_projection_readable(conn)
    finally:
        conn.close()


def test_the_gate_never_acquires_a_lock(core, tmp_path, monkeypatch):
    """Inside a caller transaction the gate must SIGNAL, not reconcile: taking
    the maintenance and cache locks after a SQLite transaction has opened
    inverts the repository's lock order."""
    import fcntl

    quota = _quota()
    locks = importlib.import_module("_lib_cache_writer_lock")
    store = importlib.import_module("_cctally_store")

    def refuse(*_args, **_kwargs):
        raise AssertionError("the projection gate must not acquire a lock")

    conn = _open_stats(core, tmp_path)
    try:
        _set_incomplete(conn)
        # Poison AFTER the open, because `open_db` legitimately takes
        # `stats_write_scope("open-time")`. No `hasattr` guard:
        # `monkeypatch.setattr` raises on a name that does not exist, which is
        # the point — a rename must fail this test rather than silently
        # un-install half of it, which is what the earlier guarded form did for
        # a `_cctally_store.stats_maintenance_lock` that never existed.
        for module, name in (
            (fcntl, "flock"),
            (locks, "acquire_cache_writer_flocks"),
            (store, "_acquire_stats_maintenance_reentrant"),
            (store, "stats_write_scope"),
        ):
            monkeypatch.setattr(module, name, refuse)
        conn.execute("BEGIN")
        with pytest.raises(quota.QuotaProjectionIncomplete):
            quota.assert_projection_readable(conn)
        conn.rollback()
    finally:
        conn.close()


def test_the_gate_carries_the_versioned_recovery_target(core, tmp_path):
    """The target is a VERSIONED identity, not a bare coordinate, so a target
    written by one binary is never misread by another."""
    quota = _quota()
    conn = _open_stats(core, tmp_path)
    try:
        _set_incomplete(conn)
        with pytest.raises(quota.QuotaProjectionIncomplete) as caught:
            quota.assert_projection_readable(conn)
    finally:
        conn.close()
    assert caught.value.target_version == 1
    assert caught.value.recovery_target == {"targetVersion": 1, "covered": None}


def test_a_connection_without_the_table_is_readable(core, tmp_path):
    """Fail-open is correct for a MISSING TABLE and only for that.

    `stats_quota_projection_state` arrived with the epoch-1009 stats index, so
    its absence means the index predates the flag entirely and there is no
    incomplete projection for the flag to describe.
    """
    quota = _quota()
    conn = sqlite3.connect(tmp_path / "not-a-stats-index.db")
    try:
        quota.assert_projection_readable(conn)
    finally:
        conn.close()


def test_the_gate_fails_closed_when_the_flag_cannot_be_read(core, tmp_path):
    """A gate whose purpose is to fail closed must not read "I could not read
    the flag" as "the flag is clear".

    The shipped form swallowed every `sqlite3.Error`, justified by the claim
    that an epoch-1009 index always carries the table, so a failing probe could
    not be serving an incomplete projection. That is false: a connection can be
    alive, hold an epoch-1009 index open and still fail a read.

    Two earlier versions of this docstring made claims about #516 that did not
    survive measurement: first that the sidecar unlink "does not reproduce
    inside one pytest process", then that the production-ordered unlink
    produced no error at all. Re-measured on both LAN runners, the unlink DOES
    break a same-process reader with `disk I/O error` and DOES leave a
    cross-process one on a silently stale generation — but only when something
    writes after the unlink and the reader had read before it. That path is
    deleted, so no claim about #516 is made here and none is needed. The gate's
    own contract is asserted directly instead, in the two shapes the two-step
    probe has, both deterministic on every platform.
    """
    quota = _quota()

    # 1. The projection table is present but unreadable. This is the shape the
    #    structural `sqlite_master` probe cannot rule out: presence says
    #    nothing about readability, and the gate must still fail closed.
    conn = _open_stats(core, tmp_path)
    _set_incomplete(conn, value=0)

    def deny_the_projection_table(action, arg1, arg2, dbname, source):
        if arg1 == "stats_quota_projection_state":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_the_projection_table)
    try:
        # Non-vacuity: the read really does fail, and not with the missing-table
        # error that the one sanctioned fail-open branch matches on.
        with pytest.raises(sqlite3.Error) as raised:
            conn.execute("SELECT 1 FROM stats_quota_projection_state")
        assert "no such table" not in str(raised.value).lower()
        # And the table IS present, so this is not the fail-open case wearing a
        # different error.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='stats_quota_projection_state'").fetchone() is not None

        with pytest.raises(quota.QuotaProjectionIncomplete):
            quota.assert_projection_readable(conn)
    finally:
        conn.set_authorizer(None)
        conn.close()

    # 2. The connection is unusable outright, so even the presence probe fails.
    #    A connection that cannot read `sqlite_master` cannot report absence
    #    either, and must not be granted the fail-open branch.
    closed = _open_stats(core, tmp_path)
    _set_incomplete(closed, value=0)
    closed.close()
    with pytest.raises(quota.QuotaProjectionIncomplete):
        quota.assert_projection_readable(closed)


def test_a_corrupt_index_is_not_reported_as_an_incomplete_projection(
    core, tmp_path,
):
    """Fail-closed must not mean "claim ownership of every failure".

    A corrupt `stats.db` fails the flag probe like any other unreadable one.
    Wrapping it as `QuotaProjectionIncomplete` filed corruption under the wrong
    owner AND cost the right one its signal: `_cctally_tui` catches this
    exception ahead of its corruption branch, so a wrapped corruption error
    never became `_StatsSnapshotCorruption` and never reached the #407 heal —
    the index stayed corrupt while every surface reported a reconcilable quota
    view. It is re-raised unchanged instead, which serves no projection value
    either way.
    """
    import _cctally_db

    quota = _quota()
    path = tmp_path / "not-a-database.db"
    path.write_bytes(b"not a database " * 200)
    conn = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.DatabaseError) as raised:
            quota.assert_projection_readable(conn)
        # Non-vacuity: it really is the corruption shape the predicate owns,
        # and it is NOT the retry signal.
        assert _cctally_db._is_sqlite_corruption_error(raised.value)
        assert not isinstance(raised.value, quota.QuotaProjectionIncomplete)
    finally:
        conn.close()


def test_every_projection_read_site_is_enumerated():
    """Projection reads are scattered across the dashboard, milestone-history,
    quota and library modules, frequently inside `except sqlite3.Error`
    fallbacks that would render a denial as empty data rather than an error. A
    static guard keeps the enumeration complete, the same discipline
    `FROZEN_WRITE_SITES` applies to writes."""
    quota = _quota()
    observed = _scan_projection_read_sites()
    assert observed == set(quota.PROJECTION_READ_CHOKEPOINTS), (
        "projection read sites changed:\n"
        f"  new: {sorted(observed - set(quota.PROJECTION_READ_CHOKEPOINTS))}\n"
        f"  gone: {sorted(set(quota.PROJECTION_READ_CHOKEPOINTS) - observed)}\n"
        "A NEW site must call `assert_projection_readable` BEFORE any "
        "fallback-catching SQL, then be classified in "
        "PROJECTION_READ_SITE_ACTIONS. Do not widen this set to make the test "
        "pass without doing the first."
    )


_PROJECTION_ACTIONS = {"gate", "gate_at_caller", "projector", "diagnostic"}


def test_every_enumerated_site_has_an_action():
    quota = _quota()
    assert set(quota.PROJECTION_READ_SITE_ACTIONS) == set(
        quota.PROJECTION_READ_CHOKEPOINTS)
    assert set(
        quota.PROJECTION_READ_SITE_ACTIONS.values()) <= _PROJECTION_ACTIONS
    assert set(
        quota.PROJECTION_DYNAMIC_READ_ACTIONS.values()) <= _PROJECTION_ACTIONS


def test_every_gated_function_calls_the_gate():
    """Membership alone is not enforcement.

    The earlier form asserted the string `assert_projection_readable` appeared
    anywhere in the module, so a module with three `gate` sites where only one
    function called the gate passed. The check is per FUNCTION.
    """
    quota = _quota()
    for site, action in quota.PROJECTION_READ_SITE_ACTIONS.items():
        if action != "gate":
            continue
        module, function, _table = site.split("::")
        assert _function_calls_the_gate(module, function), site


def test_every_gate_at_caller_site_names_a_caller_that_gates():
    """`gate_at_caller` is an assertion about a DIFFERENT function, so it is
    only as good as the caller it names.

    The first version of this classification named
    `_cctally_dashboard_sources.codex_projection_coherence` as the caller of
    `codex_stats_digest`. That function does not call the kernel at all, and the
    real callers did not call the gate — a false claim nothing tested.
    """
    quota = _quota()
    named = quota.PROJECTION_GATE_CALLERS
    at_caller = {
        site for site, action in quota.PROJECTION_READ_SITE_ACTIONS.items()
        if action == "gate_at_caller"
    }
    assert at_caller, "the guard below is worthless with no site to check"
    assert set(named) == at_caller
    for site, callers in named.items():
        assert callers, site
        for caller in callers:
            module, function = caller.split("::")
            assert _function_calls_the_gate(module, function), (
                f"{site} names {caller}, which does not call the gate")


def test_the_caller_gate_check_is_non_vacuous(tmp_path):
    """It must answer False for a function that does not call the gate."""
    module = tmp_path / "example_module.py"
    module.write_text(
        "def gated(conn):\n"
        "    assert_projection_readable(conn)\n"
        "    return conn.execute('SELECT 1')\n"
        "\n"
        "def ungated(conn):\n"
        "    return conn.execute('SELECT 1')\n",
        encoding="utf-8",
    )
    assert _function_calls_the_gate(module.name, "gated", root=tmp_path)
    assert not _function_calls_the_gate(module.name, "ungated", root=tmp_path)


def _function_calls_the_gate(module: str, function: str, root=None) -> bool:
    """Whether ``function`` in ``module`` calls `assert_projection_readable`."""
    import ast

    if function == "<module>":
        raise AssertionError(
            "a module-scope read cannot gate itself; classify it "
            "`gate_at_caller` and name the caller")
    root = root or (pathlib.Path(__file__).resolve().parent.parent / "bin")
    tree = ast.parse((root / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
        ):
            return any(
                isinstance(inner, ast.Call)
                and (getattr(inner.func, "id", None)
                     or getattr(inner.func, "attr", None))
                == "assert_projection_readable"
                for inner in ast.walk(node)
            )
    raise AssertionError(f"{module} has no function named {function}")


def test_the_scan_is_non_vacuous():
    """It must react to a site placed in a module that has none."""
    observed = _scan_text_for_projection_reads(
        "example.py",
        'def reader(conn):\n'
        '    return conn.execute("SELECT 1 FROM quota_window_blocks").fetchall()\n',
    )
    assert observed == {"example.py::reader::quota_window_blocks"}
    qualified = _scan_text_for_projection_reads(
        "example.py",
        'def reader(conn):\n'
        '    return conn.execute("SELECT 1 FROM main.quota_window_blocks")\n',
    )
    assert qualified == {"example.py::reader::quota_window_blocks"}, (
        "a schema-qualified read resolved to the literal `main` and was "
        "invisible to PROJECTION_READ_CHOKEPOINTS"
    )


_PROJECTION_TABLES = ("quota_window_blocks", "quota_projection_state")
#: The schema qualifier is CONSUMED, not captured. Without that prefix
#: `FROM main.quota_window_blocks` resolves to the literal `main`, so a
#: qualified read of a projection family is invisible to this scan — the same
#: blind spot the write-side `_NAME` pattern had. The alternatives are the
#: schema names this codebase attaches under; `cache_db` comes from
#: `ATTACH DATABASE ? AS cache_db`.
_PROJECTION_READ = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:(?:main|temp|src|cache_db)\s*\.\s*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _scan_text_for_projection_reads(name, text):
    """Every projection read in one module, as `<file>::<function>::<table>`.

    Attribution goes through the AST rather than line indentation, because a
    multi-line SQL literal's continuation lines start at column 0 and a
    line-based scan therefore misattributes them to module scope.
    """
    import ast

    sites: set = set()

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                for found in _PROJECTION_READ.finditer(child.value):
                    if found.group(1) in _PROJECTION_TABLES:
                        sites.add(f"{name}::{owner}::{found.group(1)}")
            visit(child, owner)

    visit(ast.parse(text), "<module>")
    return sites


def _projection_scan_paths():
    """Every runtime source both scans cover.

    `bin/cctally` is yielded explicitly because it is extensionless and a
    `bin/*.py` glob silently misses it.
    """
    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    yield bin_dir / "cctally"
    for path in sorted(bin_dir.glob("*.py")):
        if not path.name.startswith("build-"):
            yield path


def _scan_projection_read_sites():
    sites: set = set()
    for path in _projection_scan_paths():
        sites |= _scan_text_for_projection_reads(
            path.name, path.read_text(encoding="utf-8", errors="replace"))
    return sites


#: Same qualifier prefix, for the same reason: `FROM main.{table}` is a dynamic
#: target that the unqualified pattern did not match, so three such reads in
#: `bin/_cctally_cache.py` were invisible to BOTH guards — while the write-side
#: scan already counted the `INSERT OR IGNORE INTO main.{table}` fifteen lines
#: below them in the same function.
_DYNAMIC_TARGET = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:(?:main|temp|src|cache_db)\s*\.\s*)?\{",
    re.IGNORECASE,
)


def _scan_text_for_dynamic_reads(text) -> int:
    """How many `FROM`/`JOIN` targets in ``text`` are INTERPOLATED.

    Each f-string is reduced to its template — literal parts verbatim and `{}`
    for every interpolation — so `f"SELECT COUNT(*) FROM {table} WHERE {where}"`
    counts while `f"SELECT {columns} FROM quota_window_blocks"` does not, since
    the second names its table and the literal scanner already sees it.
    """
    import ast

    total = 0
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.JoinedStr):
            continue
        template = "".join(
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else "{}"
            for value in node.values
        )
        total += len(_DYNAMIC_TARGET.findall(template))
    return total


def test_dynamic_target_read_sites_are_frozen():
    """The literal scanner reads string LITERALS, so a read written as
    `FROM {table}` reaches a projection family while being invisible to it —
    which is exactly what `_cctally_dashboard._debug_source_counts` did.

    The count cannot say which table a site reaches, but it does stop a new
    dynamic read from arriving silently. Mirrors `FROZEN_DYNAMIC_SITES` in
    `tests/test_stats_writer_surface_386.py`.
    """
    quota = _quota()
    observed = {}
    for path in _projection_scan_paths():
        count = _scan_text_for_dynamic_reads(
            path.read_text(encoding="utf-8", errors="replace"))
        if count:
            observed[path.name] = count
    assert observed == quota.PROJECTION_DYNAMIC_READ_SITES, (
        "dynamic-target read sites changed:\n"
        f"  frozen={quota.PROJECTION_DYNAMIC_READ_SITES}\n"
        f"  found={observed}\n"
        "A dynamic target is invisible to the literal scan AND to lexical "
        "review. Decide whether the new site reaches `quota_window_blocks` or "
        "`quota_projection_state`; if it does, classify it in "
        "PROJECTION_DYNAMIC_READ_ACTIONS before updating this count."
    )


def test_the_dynamic_scan_is_non_vacuous():
    assert _scan_text_for_dynamic_reads(
        'def reader(conn, table):\n'
        '    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchall()\n'
    ) == 1
    assert _scan_text_for_dynamic_reads(
        'def reader(conn, columns):\n'
        '    return conn.execute(f"SELECT {columns} FROM quota_window_blocks")\n'
    ) == 0
    assert _scan_text_for_dynamic_reads(
        'def reader(conn, table):\n'
        '    return conn.execute(f"SELECT 1 FROM main.{table}").fetchall()\n'
    ) == 1, "a qualified dynamic target must not be invisible to both guards"


def test_the_debug_counter_is_classified():
    """The nineteenth read: `_DEBUG_SOURCE_STATS_TABLES` names
    `quota_window_blocks` and the query interpolates it, so no static scan over
    SQL literals can reach it and only a hand classification covers it."""
    quota = _quota()
    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    source = (bin_dir / "_cctally_dashboard.py").read_text(encoding="utf-8")
    start = source.index("_DEBUG_SOURCE_STATS_TABLES = {")
    block = source[start:source.index("\n}\n", start)]
    assert "quota_window_blocks" in block
    assert quota.PROJECTION_DYNAMIC_READ_ACTIONS[
        "_cctally_dashboard.py::_debug_source_counts"] == "diagnostic"


# --------------------------------------------------------------------------
# the gated-caller contract (#496 S5b section 4.7)
# --------------------------------------------------------------------------

def test_every_refusal_names_the_one_remedy(core, tmp_path):
    """A surface that says only "incomplete" leaves the user with no next step.

    Only two things clear the durable flag — a reconciliation, which only
    `cctally cache-sync` and the dashboard server arm, and a later rebuild whose
    coverage came back complete — so the remedy is not guessable from the
    message. BOTH raise paths carry it, which is what lets the ten gated sites
    that render the exception string name it without each restating it.
    """
    quota = _quota()
    assert "cctally cache-sync" in quota.QUOTA_PROJECTION_REMEDY

    conn = _open_stats(core, tmp_path)
    try:
        _set_incomplete(conn)
        with pytest.raises(quota.QuotaProjectionIncomplete) as flagged:
            quota.assert_projection_readable(conn)
    finally:
        conn.close()
    assert quota.QUOTA_PROJECTION_REMEDY in str(flagged.value)

    # The unreadable-flag path, on the connection just closed.
    with pytest.raises(quota.QuotaProjectionIncomplete) as unreadable:
        quota.assert_projection_readable(conn)
    assert quota.QUOTA_PROJECTION_REMEDY in str(unreadable.value)


_TUI_GATED_CALLS = ("_tui_build_source_bundle", "_tui_compute_dispatch_signature")


def _handler_names(handler):
    import ast

    if handler.type is None:
        return {"BaseException"}
    targets = (
        handler.type.elts if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    return {
        node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        for node in targets
    }


def test_the_tui_catches_the_refusal_before_its_broad_handlers():
    """The TUI is the surface the refusal degrades worst on.

    Neither raise site escapes: both sit under `except Exception` handlers that
    sanitize the cause away and fall back to `prior_source_bundle`, which is
    `None` on a cold start — a permanently blank Codex source panel with no
    stated cause and no remedy. The handler has to come FIRST, so this asserts
    ordering rather than mere presence.
    """
    import ast

    bin_dir = pathlib.Path(__file__).resolve().parent.parent / "bin"
    tree = ast.parse((bin_dir / "_cctally_tui.py").read_text(encoding="utf-8"))
    # Nearest enclosing `try` per call, not every ancestor: `ast.walk` descends
    # through nested `Try` nodes, so an outer handler would otherwise be
    # credited with guarding a call an inner one already catches.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_try(node):
        while node is not None:
            parent = parents.get(node)
            if isinstance(parent, ast.Try) and node in parent.body:
                return parent
            node = parent
        return None

    guarded = 0
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in _TUI_GATED_CALLS):
            continue
        node = enclosing_try(call)
        assert node is not None, ast.dump(call.func)
        calls = {call.func.id}
        names = [_handler_names(h) for h in node.handlers]
        typed = [i for i, n in enumerate(names)
                 if "QuotaProjectionIncomplete" in n]
        broad = [i for i, n in enumerate(names)
                 if n & {"Exception", "BaseException"}]
        assert typed, (
            "a try calling "
            f"{sorted(calls & set(_TUI_GATED_CALLS))} must catch "
            "QuotaProjectionIncomplete"
        )
        assert not broad or min(typed) < min(broad), (
            "the typed handler must precede the broad one, or the refusal is "
            "sanitized before it is ever seen"
        )
        guarded += 1
    # Non-vacuity: the walk really found the three call sites the finding named
    # (two in `_tui_build_snapshot_once`, one in `_tui_build_idle_snapshot`).
    assert guarded == 3, guarded
