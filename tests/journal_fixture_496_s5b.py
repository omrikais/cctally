"""#496 S5b — the deterministic selector fixture for durable replay selection.

Stage 1 makes `resolve_effective_events`' accumulators durable, so every
assertion in this session compares a stored row set against the selection the
same record stream produces in memory. That comparison is only meaningful over
a journal that actually exercises the accumulators, which is why this fixture
carries one of each shape the durable rows have to represent:

- a **completed** correction batch, so `journal_selector_batches.status` has a
  `completed` row and the action cores are the ones that get dropped;
- an **incomplete** batch (begin plus actions, no commit), so a `begin_only`
  row exists and its cores must be retained for a later split-cycle commit;
- a **tainted** batch (an orphan commit — the `commit_without_begin`
  violation), so the taint path and `violation_available_after` are non-empty;
- a **same-revision conflict** at the winning revision, so
  `journal_effective_events.conflict_hashes_json` has more than one hash;
- **two segments**, so a pass that re-reads a segment produces a visibly
  different open sequence;
- **non-retained records** (Codex quota observations and a Claude obs) mixed
  between the retained ones, so the `decoded`-entry count is strictly greater
  than the retained-record count and a placeholder bug moves a sequence number.

Spec §7.1 states the fixture caution this module obeys: new scenarios are added
as their own fixture rather than by enriching `journal_fixture_496_s5.py`,
because adding records to a shared fixture can retire the shape an existing
test was covering.

The records are written as bytes, in one deterministic order, so the journal is
byte-identical on every machine and every run.
"""
from __future__ import annotations

import pathlib

import _lib_journal as jl


AT = "2026-07-27T06:00:00Z"
LATER = "2026-08-03T06:00:00Z"

SEG_A = "observations-2026-07.jsonl"
SEG_B = "observations-2026-08.jsonl"

SEGMENTS = (SEG_A, SEG_B)

#: The batch ids this fixture writes, by the durable status they must produce.
BATCH_COMPLETED = "batch:s5b-complete"
BATCH_BEGIN_ONLY = "batch:s5b-incomplete"
BATCH_TAINTED = "batch:s5b-orphan"

#: The event ids, by the selector shape they exercise.
EVENT_CORRECTED = "sa:s5b-corrected"
EVENT_CONFLICT = "sa:s5b-conflict"
EVENT_PENDING = "sa:s5b-pending"

#: `tainted_action_scenario`'s own ids. They are deliberately disjoint from the
#: ones above: spec §7.1 cautions that adding records to a shared fixture can
#: retire the shape an existing test was covering, so the tainted-with-actions
#: case is a SEPARATE scenario rather than an enrichment of
#: `build_selector_scenarios`' orphan batch.
BATCH_TAINTED_ACTIONS = "batch:s5b-tainted-actions"
EVENT_TAINT_TARGET = "sa:s5b-taint-target"
EVENT_TAINT_OTHER = "sa:s5b-taint-other"

#: A syntactically valid manifest hash that is not the one the actions produce.
WRONG_ACTIONS_HASH = "sha256:" + "1" * 64

#: `legacy_qaa_scenario`'s single reused natural id.
EVENT_LEGACY_QAA = "qaa:rk:weekly:primary:10080:2026-08-03T00:00:00Z"


def _snapshot_payload(percent: float, source: str) -> dict:
    return {
        "captured_at_utc": AT,
        "week_start_date": "2026-07-27",
        "week_end_date": "2026-08-03",
        "week_start_at": "2026-07-27T00:00:00+00:00",
        "week_end_at": "2026-08-03T00:00:00+00:00",
        "weekly_percent": percent,
        "source": source,
        "payload_json": "{}",
        "account_key": "unattributed",
    }


def _evt(event_id: str, percent: float, source: str = "fixture") -> dict:
    return jl.make_evt(
        kind="snapshot_accept",
        id=event_id,
        at=AT,
        payload=_snapshot_payload(percent, source),
    )


def _claude_obs(at: str, weekly_percent: float) -> dict:
    return jl.make_obs(
        at=at,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload={
            "captured_at": at,
            "source": "statusline",
            "weekly_percent": weekly_percent,
            "resets_at": 1785196800,
        },
    )


def quota_obs(at: str, line_offset: int) -> dict:
    """One Codex quota observation — a NON-retained record.

    The rebuild folds it into a `decoded.append(None)` placeholder, so a segment
    made only of these still contributes to the selector's sequence numbering.
    """
    return jl.make_obs(
        at=at,
        src="codex-quota",
        provider="codex",
        payload={
            "source": "codex",
            "source_path": "/tmp/codex/sessions/s5b.jsonl",
            "line_offset": line_offset,
            "logical_limit_key": "weekly",
            "captured_at": at,
            "used_percent": 12.5,
            "resets_at": 1785196800,
        },
    )


def _replace_action(event_id: str, rev: int, percent: float) -> dict:
    return {
        "action": "replace",
        "id": event_id,
        "rev": rev,
        "at": AT,
        # A replacement action's payload must carry its own `kind`: unlike
        # `make_evt`, `_validate_action_core` does not inject one.
        "payload": {**_snapshot_payload(percent, "correction"),
                    "kind": "snapshot_accept"},
    }


def tainted_action_scenario(*, declared_actions_hash: "str | None" = None) -> dict:
    """A TAINTED batch that CARRIES actions, split across two generations.

    `build_selector_scenarios`' tainted batch is an orphan commit, so it has no
    action rows at all and the retention half of "cores are retained while a
    batch is `begin_only` OR `tainted`" is unobservable there. This scenario
    supplies the missing half, and spec §7.1 requires it to be its own fixture
    rather than an enrichment of that one.

    The taint comes from PHASE 1 — a byte-different duplicate of action 0, which
    is `action_sequence_conflict` — because phase 2 never even examines a batch
    that has no commit yet. So the seed generation ends with a batch that is
    tainted, carries one action, and is still open.

    ``delta`` then delivers the second action and the commit. That is the case
    the retention rule exists for: phase 2 now hashes EVERY first-seen action
    core, including the one only the durable row still holds, to decide
    `manifest_actions_hash_mismatch`. Pass ``declared_actions_hash`` to make the
    begin/commit markers claim a manifest the actions do not produce, which is
    what turns that violation on.

    Returns the two record lists; no journal is written, because every assertion
    over this scenario is a pure-kernel comparison against a full selection over
    the concatenated stream.
    """
    actions = [
        _replace_action(EVENT_TAINT_TARGET, 1, 77.0),
        _replace_action(EVENT_TAINT_OTHER, 1, 88.0),
    ]
    begin, action_zero, action_one, commit = jl.make_correction_batch(
        batch_id=BATCH_TAINTED_ACTIONS,
        family="claude-usage",
        at=AT,
        actions=actions,
    )
    if declared_actions_hash is not None:
        # BOTH markers carry it: an unequal pair is `marker_manifest_mismatch`,
        # a different violation that would mask the one under test.
        begin = {**begin, "actions_hash": declared_actions_hash}
        commit = {**commit, "actions_hash": declared_actions_hash}
    conflicting_zero = {**action_zero, "at": "2026-07-28T07:07:07Z"}
    return {
        "batch_id": BATCH_TAINTED_ACTIONS,
        "seed": [
            _evt(EVENT_TAINT_TARGET, 21.0),
            _evt(EVENT_TAINT_OTHER, 22.0),
            quota_obs(AT, 7),
            begin,
            action_zero,
            conflicting_zero,
        ],
        "delta": [action_one, commit],
    }


#: `withdrawn_violation_scenario`'s own ids, disjoint from every other
#: scenario's for the §7.1 reason the module docstring states.
BATCH_WITHDRAWN = "batch:s5b-withdrawn"
EVENT_WITHDRAWN_A = "sa:s5b-withdrawn-a"
EVENT_WITHDRAWN_B = "sa:s5b-withdrawn-b"


def withdrawn_violation_scenario() -> dict:
    """A phase-2 violation the NEXT generation withdraws.

    The seed declares `action_count=2` and commits carrying only action 0, which
    is `manifest_action_sequence_mismatch`. The delta then delivers action 1, so
    the action set becomes complete and that violation is no longer derivable —
    a full pass over the concatenated stream reports only
    `record_order_violation`, because action 1 sits past the commit.

    This is the one shape that distinguishes a violation set advanced by union
    from one advanced by re-resolution, and the durable table is read by
    `_check_journal_protocol`, which FAILS `doctor` and prints a
    `db journal-repair --violation <fingerprint>` command for whatever it holds.
    """
    actions = [
        _replace_action(EVENT_WITHDRAWN_A, 1, 61.0),
        _replace_action(EVENT_WITHDRAWN_B, 1, 62.0),
    ]
    begin, action_zero, action_one, commit = jl.make_correction_batch(
        batch_id=BATCH_WITHDRAWN,
        family="claude-usage",
        at=AT,
        actions=actions,
    )
    return {
        "batch_id": BATCH_WITHDRAWN,
        "seed": [
            _evt(EVENT_WITHDRAWN_A, 31.0),
            _evt(EVENT_WITHDRAWN_B, 32.0),
            quota_obs(AT, 11),
            begin,
            action_zero,
            commit,
        ],
        "delta": [action_one],
    }


#: `two_completed_batches_scenario`'s own ids. `alpha` sorts before `beta`.
BATCH_TAINT_ALPHA = "batch:s5b-taint-alpha"
BATCH_TAINT_BETA = "batch:s5b-taint-beta"
EVENT_TAINT_ALPHA = "sa:s5b-taint-alpha"
EVENT_TAINT_BETA = "sa:s5b-taint-beta"


def two_completed_batches_scenario() -> dict:
    """Two COMPLETED batches that ONE delta taints together.

    The delta carries BETA's byte-different duplicate commit BEFORE ALPHA's, so
    beta's `marker_conflict` is established at the lower sequence. Causal order
    is therefore `[beta, alpha]` while batch-id order is `[alpha, beta]`, which
    is what makes an ordering assertion over the pair non-vacuous: the caller
    raises on the FIRST transition, and raising the batch whose causal record
    sits later would rebuild through a longer prefix than necessary.
    """
    alpha = jl.make_correction_batch(
        batch_id=BATCH_TAINT_ALPHA,
        family="claude-usage",
        at=AT,
        actions=[_replace_action(EVENT_TAINT_ALPHA, 1, 71.0)],
    )
    beta = jl.make_correction_batch(
        batch_id=BATCH_TAINT_BETA,
        family="claude-usage",
        at=AT,
        actions=[_replace_action(EVENT_TAINT_BETA, 1, 72.0)],
    )
    return {
        "alpha": BATCH_TAINT_ALPHA,
        "beta": BATCH_TAINT_BETA,
        "seed": [
            _evt(EVENT_TAINT_ALPHA, 41.0),
            _evt(EVENT_TAINT_BETA, 42.0),
            *alpha,
            *beta,
        ],
        "delta": [
            {**beta[-1], "at": "2026-09-09T09:09:09Z"},
            {**alpha[-1], "at": "2026-09-10T10:10:10Z"},
        ],
    }


def _legacy_arming(threshold: int) -> dict:
    """One pre-#372 `quota_alert_arming` line, reusing the natural id.

    The ABSENCE of `journal_identity_version` in the payload is what makes the
    record legacy, so it is left out deliberately rather than by omission.
    """
    return jl.make_evt(
        kind="quota_alert_arming",
        id=EVENT_LEGACY_QAA,
        at=AT,
        payload={
            "source": "codex",
            "source_root_key": "rk",
            "logical_limit_key": "weekly",
            "observed_slot": "primary",
            "window_minutes": 10080,
            "resets_at_utc": "2026-08-03T00:00:00Z",
            "threshold": threshold,
        },
    )


def legacy_qaa_scenario() -> dict:
    """Two legacy arming lines for one natural id, split across a generation.

    `_lib_selector_state._merge_candidates` re-implements the same-revision
    containment rules `resolve_effective_events` applies, and that includes the
    legacy `qaa` carve-out: last-wins AND silent, because such a record
    deliberately reuses its natural id as a state stream rather than diverging
    at one revision. The two implementations agree today; this scenario is what
    makes a future divergence visible on the INCREMENTAL path, which had no
    coverage for the carve-out at all.
    """
    return {
        "event_id": EVENT_LEGACY_QAA,
        "seed": [_evt(EVENT_CORRECTED, 20.0), _legacy_arming(50)],
        "delta": [quota_obs(LATER, 9), _legacy_arming(75)],
    }


#: `cutover_scenario`'s own ids, disjoint from every other scenario's.
CUTOVER_ACCOUNT = "claude:cutover-account"
EVENT_CUTOVER_LEGACY = "sa:s5b-cutover-legacy"

#: `_cctally_journal.CUTOVER_OP_ID`, restated so the fixture stays importable
#: without the glue module. A drift guard asserts the two agree.
CUTOVER_OP_ID = "accounts-cutover-v1"


def _cutover_op(claude_legacy_account: str) -> dict:
    """The canonical accounts-cutover op, as `append_accounts_cutover_op` writes
    it: a content-id `op` whose id is then overridden with the stable token."""
    record = jl.make_op(
        at=AT,
        src="accounts-cutover",
        payload={
            "kind": "accounts_cutover",
            "claude_legacy_account": claude_legacy_account,
        },
    )
    record["id"] = CUTOVER_OP_ID
    return record


def _legacy_claude_obs(at: str, weekly_percent: float) -> dict:
    """A pre-#341 Claude observation, which carries NO account stamp.

    That absence is the whole point: `_normalize_legacy_account_stamp` injects
    the cutover op's account into exactly these records, so a selection that has
    folded the cutover and one that has not produce different `content_hash` and
    `event_json` for them.
    """
    record = jl.make_obs(
        at=at,
        src="record-usage",
        provider="claude",
        payload={
            "captured_at": at,
            "source": "statusline",
            "weekly_percent": weekly_percent,
            "resets_at": 1785196800,
        },
    )
    record.pop("account", None)
    return record


def _legacy_evt(event_id: str, percent: float) -> dict:
    """A pre-#341 `snapshot_accept` evt, whose payload carries no `account_key`."""
    record = _evt(event_id, percent)
    record["payload"].pop("account_key", None)
    record["id"] = event_id
    return record


def cutover_scenario() -> dict:
    """A prefix of legacy unstamped Claude lines, then the cutover op.

    Returns ``seed`` (the lines the durable prefix folds) and ``delta`` (the
    cutover op plus one more legacy line). The delta's op is the case an
    incremental merge may not decide alone: adopting it would re-normalize every
    legacy line in the SEED, changing those events' `content_hash` and
    `event_json`, while the incremental path normalizes only the delta.
    """
    return {
        "account": CUTOVER_ACCOUNT,
        "op_id": CUTOVER_OP_ID,
        "seed": [
            _legacy_claude_obs(AT, 10.0),
            _legacy_evt(EVENT_CUTOVER_LEGACY, 20.0),
        ],
        "delta": [
            _cutover_op(CUTOVER_ACCOUNT),
            _legacy_evt(EVENT_CORRECTED, 30.0),
        ],
    }


def _append(journal_dir: pathlib.Path, name: str, records) -> list:
    """Write ``records`` and return each one's `(segment, end offset)`.

    The end offset is what the journal's own readers report — the start offset
    plus the line length plus its newline — so a caller can pin a rebuild
    exactly at a chosen record boundary and hand the same coordinate to the
    incremental selector.
    """
    journal_dir.mkdir(parents=True, exist_ok=True)
    coordinates = []
    with open(journal_dir / name, "ab") as handle:
        for record in records:
            handle.write(jl.encode_line(record))
            coordinates.append((name, handle.tell()))
    return coordinates


def append_to_segment(app_dir, name: str, records) -> list:
    """Append ``records`` to an existing segment and return their coordinates.

    Deliberately NOT `append_record`: that resolves its target segment from the
    wall clock, and this fixture's segment names are fixed, so a test written
    against it would start refusing the moment the real month moved past
    2026-08 (#511's target revalidation, working correctly).
    """
    return _append(pathlib.Path(app_dir) / "journal", name, records)


def build_selector_scenarios(app_dir) -> dict:
    """Write the two-segment selector fixture and return its shape.

    Returns the journal high-water, the segment list, and the exact record
    stream in journal order — the same list a full traversal decodes, so a test
    can compare durable rows against `resolve_effective_events(records)` without
    re-reading the journal.
    """
    app_dir = pathlib.Path(app_dir)
    journal_dir = app_dir / "journal"

    stream: list[dict] = []

    completed_batch = jl.make_correction_batch(
        batch_id=BATCH_COMPLETED,
        family="claude-usage",
        at=AT,
        actions=[_replace_action(EVENT_CORRECTED, 1, 44.0)],
    )
    segment_a = [
        _claude_obs(AT, 10.0),
        _evt(EVENT_CORRECTED, 20.0),
        # Two rev-0 lines for one event id with DIFFERENT content: the #374
        # same-revision conflict. Nothing supersedes this id, so the winning
        # revision is 0 and the group is reported rather than resolved.
        _evt(EVENT_CONFLICT, 30.0, source="fixture-a"),
        quota_obs(AT, 0),
        _evt(EVENT_CONFLICT, 31.0, source="fixture-b"),
        *completed_batch,
    ]
    coordinates = _append(journal_dir, SEG_A, segment_a)
    stream.extend(segment_a)

    incomplete_batch = jl.make_correction_batch(
        batch_id=BATCH_BEGIN_ONLY,
        family="claude-usage",
        at=LATER,
        actions=[_replace_action(EVENT_PENDING, 1, 55.0)],
    )
    orphan_batch = jl.make_correction_batch(
        batch_id=BATCH_TAINTED,
        family="claude-usage",
        at=LATER,
        actions=[_replace_action(EVENT_PENDING, 2, 66.0)],
    )
    segment_b = [
        _claude_obs(LATER, 11.0),
        quota_obs(LATER, 1),
        _evt(EVENT_PENDING, 40.0),
        # begin + action, NO commit: the batch stays inert and its action cores
        # must survive so a commit arriving in a later generation can complete
        # it (spec §3.2, the split-cycle case).
        *incomplete_batch[:-1],
        # A commit with no begin: `commit_without_begin`, which taints the batch
        # and registers a `violation_available_after` entry.
        orphan_batch[-1],
    ]
    coordinates += _append(journal_dir, SEG_B, segment_b)
    stream.extend(segment_b)

    return {
        "segments": list(SEGMENTS),
        "high_water": (SEG_B, (journal_dir / SEG_B).stat().st_size),
        "records": stream,
        #: `(segment, end offset)` per record, positionally parallel to
        #: ``records``. A pinned rebuild at ``coordinates[k]`` covers exactly
        #: ``records[:k + 1]``.
        "coordinates": coordinates,
        "batches": {
            "completed": BATCH_COMPLETED,
            "begin_only": BATCH_BEGIN_ONLY,
            "tainted": BATCH_TAINTED,
        },
        "events": {
            "corrected": EVENT_CORRECTED,
            "conflict": EVENT_CONFLICT,
            "pending": EVENT_PENDING,
        },
    }


# --------------------------------------------------------------------------
# Stage 4 — segment elision (spec §5)
#
# Its own scenario rather than an enrichment of `build_selector_scenarios`,
# per §7.1's fixture caution: adding quota-only segments to a shared fixture
# retires the shape an existing test was covering.
# --------------------------------------------------------------------------

#: Two quota-only segments and one mixed last segment. The two are ADJACENT so
#: cumulative placeholder counts have to survive a boundary, and both sort
#: before the last one under `segment_sort_key`.
SEG_E1 = "observations-2026-04.jsonl"
SEG_E2 = "observations-2026-05.jsonl"
SEG_E3 = "observations-2026-06.jsonl"

ELISION_SEGMENTS = (SEG_E1, SEG_E2, SEG_E3)

ELISION_AT = "2026-04-15T12:00:00Z"
ELISION_RESET = "2026-04-15T15:00:00+00:00"
ELISION_ROOT = "root-elide"

#: The orphan commit in the LAST segment. `commit_without_begin` puts
#: `commitSequence` — the `enumerate(records)` index — inside its evidence, and
#: the fingerprint hashes it. A wrong placeholder count from an elided segment
#: therefore moves this fingerprint, which is the defect §5.4 exists to prevent.
BATCH_ELISION_ORPHAN = "batch:s5b-elision-orphan"
EVENT_ELISION_TAIL = "sa:s5b-elision-tail"


def codex_quota_obs(*, line_offset: int, at: str = ELISION_AT,
                    used_percent: float = 10.0, root: str = ELISION_ROOT):
    """A REAL Codex quota observation — `kind: quota_window_snapshot`.

    Distinct from `quota_obs` above, which omits the kind and is therefore a
    plain non-retained record. The cache leg only replays, and only mints a
    coverage certificate over, observations carrying the kind, and elision
    requires that certificate.
    """
    return jl.make_obs(at=at, src="codex-quota", provider="codex", payload={
        "kind": "quota_window_snapshot",
        "source": "codex", "source_root_key": root,
        "source_path": f"/codex/{root}/rollout.jsonl",
        "line_offset": line_offset, "captured_at_utc": at,
        "observed_slot": "primary", "logical_limit_key": "limit-primary",
        "limit_id": "native-primary", "limit_name": "Primary",
        "window_minutes": 300, "used_percent": used_percent,
        "resets_at_utc": ELISION_RESET, "plan_type": "pro",
        "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5.3-codex",
    })


def build_elision_scenario(app_dir, *, malformed_in_first=False) -> dict:
    """Two quota-only segments followed by one holding retained records.

    ``malformed_in_first`` appends one undecodable line to the first segment,
    which must be counted separately and must NOT contribute a placeholder —
    the case that separates "lines read" from "`decoded` entries contributed".
    """
    app_dir = pathlib.Path(app_dir)
    journal_dir = app_dir / "journal"

    stream: list = []
    first = [codex_quota_obs(line_offset=index, used_percent=10.0 + index)
             for index in range(3)]
    _append(journal_dir, SEG_E1, first)
    stream.extend(first)
    if malformed_in_first:
        journal_dir.mkdir(parents=True, exist_ok=True)
        with open(journal_dir / SEG_E1, "ab") as handle:
            handle.write(b"{ this is not a record\n")

    second = [codex_quota_obs(line_offset=10 + index,
                              used_percent=20.0 + index)
              for index in range(2)]
    _append(journal_dir, SEG_E2, second)
    stream.extend(second)

    orphan = jl.make_correction_batch(
        batch_id=BATCH_ELISION_ORPHAN,
        family="claude-usage",
        at=LATER,
        actions=[_replace_action(EVENT_ELISION_TAIL, 1, 44.0)],
    )
    third = [
        _claude_obs(LATER, 11.0),
        _evt(EVENT_ELISION_TAIL, 20.0),
        # A commit with no begin: `commit_without_begin`, whose evidence carries
        # the sequence number the placeholder count decides.
        orphan[-1],
    ]
    _append(journal_dir, SEG_E3, third)
    stream.extend(third)

    return {
        "segments": list(ELISION_SEGMENTS),
        "elidable": [SEG_E1, SEG_E2],
        "last": SEG_E3,
        "high_water": (SEG_E3, (journal_dir / SEG_E3).stat().st_size),
        "records": stream,
        "root": ELISION_ROOT,
    }


# --------------------------------------------------------------------------
# Stage 4 fix round — refill layouts (spec §8 criterion 4b)
#
# `build_elision_scenario`'s two elidable segments are its ONLY quota-bearing
# ones, so `quota_raw` is EMPTY by the time `_refill_elided_quota_raw` runs over
# it. With nothing to splice into, every ordering of the recovered lines is
# indistinguishable from every other, and a test over that scenario cannot see
# whether the refill reproduced journal order. The two builders below supply the
# shape it cannot: a quota-bearing segment the pass READ, positioned so the
# recovered lines have to land around it.
# --------------------------------------------------------------------------

#: One segment per position in a refill layout, in `segment_sort_key` order.
#: A distinct year from every other scenario's, so a layout journal can never be
#: confused with one of theirs while debugging.
REFILL_LAYOUT_SEGMENTS = tuple(
    f"observations-2027-{month:02d}.jsonl" for month in range(1, 13))


def build_refill_layout(journal_dir, layout: str, *, per_segment: int = 2) -> dict:
    """Write ``layout`` to disk and return the refill's inputs and its answer.

    ``layout`` is one character per segment: ``E`` for a segment the pass
    ELIDED, ``N`` for one it read. EVERY segment carries Codex quota
    observations, which is exactly what `build_elision_scenario` cannot express.

    ``gaps`` is built by the rule `_elide_segment` applies: a gap's insertion
    index is ``len(quota_raw)`` at the moment the segment was skipped, which is
    the number of observations the READ segments before it contributed, and its
    line count is the one that segment's summary recorded.

    ``journal_order`` is every segment's raw lines in journal order — the answer
    the refill has to reproduce, since the quota insert is `INSERT OR IGNORE`
    and resolves first-wins on the natural key and `CodexResetAnchorResolver`
    decides per record in stream order.
    """
    journal_dir = pathlib.Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    quota_raw: list = []
    gaps: list = []
    journal_order: list = []
    line_offset = 0
    for position, mark in enumerate(layout):
        if mark not in ("E", "N"):
            raise ValueError(f"layout character {mark!r} is neither E nor N")
        name = REFILL_LAYOUT_SEGMENTS[position]
        records = [
            codex_quota_obs(line_offset=line_offset + index,
                            used_percent=10.0 + line_offset + index)
            for index in range(per_segment)
        ]
        line_offset += per_segment
        path = journal_dir / name
        path.write_bytes(b"".join(jl.encode_line(record) for record in records))
        # WITHOUT the trailing newline, which is the shape `_iter_segment_lines`
        # yields and the shape the rebuild appends to `quota_raw`.
        raw_lines = [jl.encode_line(record)[:-1] for record in records]
        journal_order.extend(raw_lines)
        if mark == "E":
            gaps.append((name, len(quota_raw), path.stat().st_size,
                         len(records)))
        else:
            quota_raw.extend(raw_lines)
    return {
        "layout": layout,
        "segments": list(REFILL_LAYOUT_SEGMENTS[:len(layout)]),
        "quota_raw": quota_raw,
        "gaps": gaps,
        "journal_order": journal_order,
    }


def build_mixed_elidable_refill(journal_dir) -> dict:
    """One elidable segment with quota observations around a Claude obs.

    `quota_only` means the segment has no retained record types; it does not
    mean every decoded line is a Codex quota observation. The line-count check
    therefore has to compare every line read with the summary's count, while
    returning only the two Codex observations to the quota replay stream.
    """
    journal_dir = pathlib.Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    name = REFILL_LAYOUT_SEGMENTS[0]
    records = [
        codex_quota_obs(line_offset=0, used_percent=10.0),
        _claude_obs(ELISION_AT, 11.0),
        codex_quota_obs(line_offset=2, used_percent=12.0),
    ]
    path = journal_dir / name
    path.write_bytes(b"".join(jl.encode_line(record) for record in records))
    expected = [
        jl.encode_line(records[0])[:-1],
        jl.encode_line(records[2])[:-1],
    ]
    return {
        "gaps": [(name, 0, path.stat().st_size, len(records))],
        "expected": expected,
    }


#: `build_interleaved_elision_scenario`'s segments. Its own year again, and its
#: own event id, per the §7.1 fixture caution the module docstring states.
SEG_X1 = "observations-2025-09.jsonl"
SEG_X2 = "observations-2025-10.jsonl"
SEG_X3 = "observations-2025-11.jsonl"
SEG_X4 = "observations-2025-12.jsonl"

INTERLEAVED_SEGMENTS = (SEG_X1, SEG_X2, SEG_X3, SEG_X4)

EVENT_INTERLEAVED_MIDDLE = "sa:s5b-interleaved-middle"
EVENT_INTERLEAVED_TAIL = "sa:s5b-interleaved-tail"
BATCH_INTERLEAVED_ORPHAN = "batch:s5b-interleaved-orphan"


def build_interleaved_elision_scenario(app_dir) -> dict:
    """Two elidable segments SEPARATED by a quota-bearing one the pass reads.

    The middle segment carries a retained `evt` beside its quota observation, so
    it is refused for its CONTENT while still contributing to `quota_raw`. That
    is what makes the second gap's insertion index non-zero, and a non-zero
    index is the only arrangement in which a refill that appends the recovered
    lines somewhere other than their journal position produces a different
    stream.

    The last segment carries a quota observation too, so the refill also has to
    leave the observations that follow the final gap in place.
    """
    app_dir = pathlib.Path(app_dir)
    journal_dir = app_dir / "journal"

    stream: list = []
    first = [codex_quota_obs(line_offset=index, used_percent=10.0 + index)
             for index in range(2)]
    _append(journal_dir, SEG_X1, first)
    stream.extend(first)

    middle = [
        codex_quota_obs(line_offset=10, used_percent=20.0),
        _evt(EVENT_INTERLEAVED_MIDDLE, 30.0),
    ]
    _append(journal_dir, SEG_X2, middle)
    stream.extend(middle)

    third = [codex_quota_obs(line_offset=20 + index, used_percent=30.0 + index)
             for index in range(2)]
    _append(journal_dir, SEG_X3, third)
    stream.extend(third)

    orphan = jl.make_correction_batch(
        batch_id=BATCH_INTERLEAVED_ORPHAN,
        family="claude-usage",
        at=LATER,
        actions=[_replace_action(EVENT_INTERLEAVED_TAIL, 1, 44.0)],
    )
    last = [
        _claude_obs(LATER, 11.0),
        _evt(EVENT_INTERLEAVED_TAIL, 20.0),
        codex_quota_obs(line_offset=30, used_percent=40.0),
        orphan[-1],
    ]
    _append(journal_dir, SEG_X4, last)
    stream.extend(last)

    return {
        "segments": list(INTERLEAVED_SEGMENTS),
        "elidable": [SEG_X1, SEG_X3],
        "read": [SEG_X2, SEG_X4],
        "last": SEG_X4,
        "high_water": (SEG_X4, (journal_dir / SEG_X4).stat().st_size),
        "records": stream,
        "root": ELISION_ROOT,
    }


def _elision_prefix_digest(journal_dir, high_water) -> str:
    """`journal_prefix_hash`'s framing, written out independently.

    Independently on purpose: a fixture that asked the implementation under test
    for the digest it is then asserted against would accept a changed framing
    silently, and this framing is durable inside every
    `journal_protocol_resolution` payload already written.
    """
    import hashlib

    segment_name, offset = high_water
    digest = hashlib.sha256()
    for name in sorted(
        (path.name for path in journal_dir.glob("*.jsonl")),
        key=jl.segment_sort_key,
    ):
        path = journal_dir / name
        size = offset if name == segment_name else path.stat().st_size
        data = path.read_bytes()[:size]
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if name == segment_name:
            break
    return "sha256:" + digest.hexdigest()


def _elision_violation_for(records, batch_id: str, evidence=()):
    """The live violation identity, over the EXACT record list.

    `commit_without_begin` puts the selector's `enumerate` sequence inside the
    evidence its fingerprint hashes, so this cannot be computed over a filtered
    or partial list.
    """
    selection = jl.resolve_effective_events(
        records, protocol_prefix_evidence=tuple(evidence))
    for violation in selection.protocol_violations:
        if violation.batch_id == batch_id:
            return violation
    raise AssertionError(f"fixture batch {batch_id} produced no violation")


def add_elision_resolution_op(app_dir, shape) -> dict:
    """Acknowledge the orphan commit with a `journal_protocol_resolution` op.

    The op sits AFTER the two elidable segments, which is the case spec 5.1
    describes: the digest it binds spans a prefix containing them, and
    `PrefixHashAccumulator` cannot compose over a gap, so an eliding pass has to
    abandon the accumulator and re-read from disk for the exact hash.

    Production contains ZERO resolution operations, which is exactly why this
    path needs a fixture rather than a claim.
    """
    journal_dir = pathlib.Path(app_dir) / "journal"
    stream = list(shape["records"])
    boundary = (shape["last"], (journal_dir / shape["last"]).stat().st_size)
    digest = _elision_prefix_digest(journal_dir, boundary)
    audit = jl.make_protocol_resolution(
        at=LATER,
        violations=[_elision_violation_for(stream, BATCH_ELISION_ORPHAN)],
        journal_high_water=boundary,
        journal_prefix_hash=digest,
    )
    _append(journal_dir, shape["last"], [audit])
    stream.append(audit)
    updated = dict(shape)
    updated["records"] = stream
    updated["high_water"] = (
        shape["last"], (journal_dir / shape["last"]).stat().st_size)
    updated["resolution_id"] = audit["id"]
    return updated
