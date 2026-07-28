"""Issue #372 Task A — effective revision and correction-batch protocol."""
from __future__ import annotations

import copy
import datetime as dt
import importlib

import pytest

import _lib_journal as J
from conftest import load_script, redirect_paths


AT = "2026-07-25T12:00:00Z"
FIXED = dt.datetime(2026, 7, 25, 12, 0, 0, tzinfo=dt.timezone.utc)


def _evt(event_id: str, value: int, *, rev: int = 0) -> dict:
    return J.make_evt(
        kind="test_row",
        id=event_id,
        rev=rev,
        at=AT,
        payload={"value": value},
    )


def _replace(event_id: str, value: int, *, rev: int = 1) -> dict:
    return {
        "action": "replace",
        "id": event_id,
        "rev": rev,
        "at": AT,
        "payload": {"kind": "test_row", "value": value},
    }


def _tombstone(event_id: str, *, rev: int = 1) -> dict:
    return {
        "action": "tombstone",
        "id": event_id,
        "rev": rev,
        "at": AT,
        "payload": None,
    }


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _siblings():
    import _cctally_core
    import _cctally_journal
    import _lib_journal

    return _cctally_core, _cctally_journal, _lib_journal


def _snapshot_payload(percent: float) -> dict:
    return {
        "kind": "snapshot_accept",
        "captured_at_utc": AT,
        "week_start_date": "2026-07-20",
        "week_end_date": "2026-07-27",
        "week_start_at": "2026-07-20T00:00:00+00:00",
        "week_end_at": "2026-07-27T00:00:00+00:00",
        "weekly_percent": percent,
        "source": "test",
        "payload_json": "{}",
        "account_key": "unattributed",
    }


def _block_payload(cost: float) -> dict:
    window_key = 987654
    return {
        "kind": "five_hour_block_close",
        "five_hour_window_key": window_key,
        "five_hour_resets_at": "2026-07-25T15:00:00Z",
        "block_start_at": "2026-07-25T10:00:00Z",
        "first_observed_at_utc": "2026-07-25T10:00:00Z",
        "last_observed_at_utc": "2026-07-25T15:00:00Z",
        "final_five_hour_percent": 50.0,
        "created_at_utc": "2026-07-25T10:00:00Z",
        "last_updated_at_utc": "2026-07-25T15:00:00Z",
        "is_closed": 1,
        "total_cost_usd": cost,
        "account_key": "unattributed",
        "_models": [
            {
                "five_hour_window_key": window_key,
                "model": "claude-opus-4",
                "cost_usd": cost,
                "entry_count": 1,
                "account_key": "unattributed",
            }
        ],
        "_projects": [
            {
                "five_hour_window_key": window_key,
                "project_path": "/repo/x",
                "cost_usd": cost,
                "entry_count": 1,
                "account_key": "unattributed",
            }
        ],
    }


def _arming_payload(fingerprint: str) -> dict:
    return {
        "kind": "quota_alert_arming",
        "source": "codex",
        "source_root_key": "root-a",
        "account_key": "unattributed",
        "logical_limit_key": "5h",
        "observed_slot": "primary",
        "window_minutes": 300,
        "rule_fingerprint": fingerprint,
        "activated_at_utc": AT,
    }


@pytest.mark.parametrize("bad_rev", [-1, True, 1.5, "1", None])
def test_kernel_make_evt_rejects_invalid_explicit_revision(bad_rev):
    with pytest.raises(J.JournalProtocolError, match="non-negative integer"):
        J.make_evt(
            kind="test_row",
            id="evt:x",
            rev=bad_rev,
            at=AT,
            payload={"value": 1},
        )


def test_kernel_selector_defaults_missing_evt_revision_to_zero():
    event = _evt("evt:x", 1)
    del event["rev"]

    selected = J.resolve_effective_events([event])

    assert selected.by_id["evt:x"].rev == 0
    assert selected.by_id["evt:x"].status == "active"
    assert selected.active == [event]


def test_kernel_correction_batch_has_matching_manifest_markers():
    records = J.make_correction_batch(
        batch_id="batch:opaque",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2), _tombstone("evt:y")],
    )

    begin, first, second, commit = records
    assert begin["t"] == commit["t"] == "correction_batch"
    assert begin["phase"] == "begin"
    assert commit["phase"] == "commit"
    assert begin["action_count"] == commit["action_count"] == 2
    assert begin["actions_hash"] == commit["actions_hash"]
    assert begin["actions_hash"].startswith("sha256:")
    assert (first["batch"], first["seq"]) == ("batch:opaque", 0)
    assert (second["batch"], second["seq"]) == ("batch:opaque", 1)


def test_selector_incomplete_batch_preserves_prior_effective_event():
    base = _evt("evt:x", 1)
    incomplete = J.make_correction_batch(
        batch_id="batch:incomplete",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )[:-1]

    selected = J.resolve_effective_events([base, *incomplete])

    assert selected.by_id["evt:x"].rev == 0
    assert selected.by_id["evt:x"].record["payload"]["value"] == 1


def test_selector_completed_batch_chooses_highest_revision():
    base = _evt("evt:x", 1)
    correction = J.make_correction_batch(
        batch_id="batch:complete",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )

    selected = J.resolve_effective_events([base, *correction])

    winner = selected.by_id["evt:x"]
    assert winner.rev == 1
    assert winner.status == "active"
    assert winner.batch_id == "batch:complete"
    assert winner.record["payload"] == {"kind": "test_row", "value": 2}
    assert selected.active == [winner.record]


def test_selector_identity_change_tombstones_old_id_and_adds_new_id():
    base = _evt("evt:old", 1)
    correction = J.make_correction_batch(
        batch_id="batch:identity",
        family="claude-usage",
        at=AT,
        actions=[
            _tombstone("evt:old"),
            _replace("evt:new", 2),
        ],
    )

    selected = J.resolve_effective_events([base, *correction])

    assert selected.by_id["evt:old"].status == "tombstone"
    assert selected.by_id["evt:old"].record is None
    assert selected.by_id["evt:new"].status == "active"
    assert [event["id"] for event in selected.active] == ["evt:new"]


def test_selector_accepts_canonically_identical_same_revision_duplicates():
    event = _evt("evt:x", 1)

    selected = J.resolve_effective_events([event, dict(event)])

    assert selected.active == [event]


def test_selector_quarantines_divergent_same_revision_candidates():
    """#374: divergent same-revision EVENTS no longer wedge the selector. The
    lowest-sequence candidate becomes the provisional winner and the group is
    reported on `EffectiveSelection.conflicts`."""
    first = _evt("evt:x", 1)
    second = _evt("evt:x", 2)

    selected = J.resolve_effective_events([first, second])

    assert selected.active == [first]
    assert selected.by_id["evt:x"].rev == 0
    assert len(selected.conflicts) == 1
    conflict = selected.conflicts[0]
    assert conflict.event_id == "evt:x"
    assert conflict.rev == 0
    assert conflict.content_hashes == tuple(
        sorted({J._sha256_canonical(first), J._sha256_canonical(second)})
    )
    assert conflict.selected_hash == J._sha256_canonical(first)
    assert conflict.to_dict() == {
        "eventId": "evt:x",
        "revision": 0,
        "contentHashes": list(conflict.content_hashes),
        "selectedHash": conflict.selected_hash,
    }


def test_selector_provisional_winner_is_lowest_sequence_across_many_variants():
    """The nine-variant production `wcs:` shape: every distinct hash is reported
    and the FIRST-appended line is the provisional winner regardless of input
    ordering by value."""
    variants = [_evt("evt:x", value) for value in (5, 1, 9, 3)]

    selected = J.resolve_effective_events(variants)

    conflict = selected.conflicts[0]
    assert selected.active == [variants[0]]
    assert conflict.selected_hash == J._sha256_canonical(variants[0])
    assert conflict.content_hashes == tuple(
        sorted(J._sha256_canonical(v) for v in variants)
    )


def test_selector_conflict_is_suppressed_by_a_higher_revision_correction():
    """Revision scoping (#374 §5): a completed rev-1 batch supersedes the rev-0
    group, so `db rederive` is a real remedy rather than a no-op."""
    first = _evt("evt:x", 1)
    second = _evt("evt:x", 2)
    batch = J.make_correction_batch(
        batch_id="rederive:test:abc",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 9, rev=1)],
    )

    selected = J.resolve_effective_events([first, second, *batch])

    assert selected.by_id["evt:x"].rev == 1
    assert selected.by_id["evt:x"].record["payload"]["value"] == 9
    assert selected.conflicts == ()


def test_selector_reports_a_conflict_at_the_winning_revision_only():
    """A divergence AT the winning revision is still reported; a superseded
    lower-revision divergence is not."""
    low_a = _evt("evt:x", 1)
    low_b = _evt("evt:x", 2)
    batch = J.make_correction_batch(
        batch_id="rederive:test:abc",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 9, rev=1)],
    )
    other = J.make_correction_batch(
        batch_id="rederive:test:def",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 11, rev=1)],
    )

    selected = J.resolve_effective_events([low_a, low_b, *batch, *other])

    assert selected.by_id["evt:x"].rev == 1
    assert [(c.event_id, c.rev) for c in selected.conflicts] == [("evt:x", 1)]


def _structural_violation_records(
    kind: str,
    *,
    batch_id: str = "batch:invalid",
    actions: list[dict] | None = None,
) -> list[dict]:
    actions = actions or [
        _replace("evt:x", 99, rev=2),
        _replace("evt:invalid-only", 99, rev=1),
    ]
    begin, first, second, commit = J.make_correction_batch(
        batch_id=batch_id,
        family="test_row",
        at=AT,
        actions=actions,
    )
    if kind == "marker_conflict":
        divergent_begin = copy.deepcopy(begin)
        divergent_begin["protocol_extension"] = "divergent"
        return [begin, divergent_begin, first, second, commit]
    if kind == "commit_without_begin":
        return [first, second, commit]
    if kind == "marker_manifest_mismatch":
        commit["family"] = "other-family"
        return [begin, first, second, commit]
    if kind == "record_order_violation":
        return [begin, commit, first, second]
    if kind == "manifest_action_sequence_mismatch":
        return [begin, first, commit]
    if kind == "manifest_actions_hash_mismatch":
        first["payload"]["value"] = 100
        return [begin, first, second, commit]
    if kind == "action_sequence_conflict":
        divergent_first = copy.deepcopy(first)
        divergent_first["payload"]["value"] = 100
        return [begin, first, divergent_first, second, commit]
    raise AssertionError(f"unknown structural violation kind: {kind}")


@pytest.mark.parametrize(
    "kind",
    [
        "marker_conflict",
        "commit_without_begin",
        "marker_manifest_mismatch",
        "record_order_violation",
        "manifest_action_sequence_mismatch",
        "manifest_actions_hash_mismatch",
        "action_sequence_conflict",
    ],
)
def test_selector_taints_each_structurally_invalid_batch_as_a_whole(kind):
    """#402 Task A RED: every structural class currently raises and wedges a
    rebuild. The target contract omits the entire invalid batch while valid
    batches on both sides remain eligible for the same event id."""
    base = _evt("evt:x", 0)
    earlier = J.make_correction_batch(
        batch_id="batch:valid-before",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 1, rev=1)],
    )
    later = J.make_correction_batch(
        batch_id="batch:valid-after",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 3, rev=3)],
    )

    selected = J.resolve_effective_events(
        [base, *earlier, *_structural_violation_records(kind), *later]
    )

    assert selected.completed_batches == frozenset(
        {"batch:valid-before", "batch:valid-after"}
    )
    assert "evt:invalid-only" not in selected.by_id
    assert selected.by_id["evt:x"].rev == 3
    assert selected.by_id["evt:x"].record["payload"]["value"] == 3
    assert len(selected.protocol_violations) == 1
    violation = selected.protocol_violations[0]
    assert (violation.batch_id, violation.kind) == ("batch:invalid", kind)
    assert 1 <= len(violation.evidence) <= 8
    assert violation.to_dict() == {
        "batchId": "batch:invalid",
        "kind": kind,
        "evidence": dict(violation.evidence),
        "fingerprint": violation.fingerprint,
    }
    assert violation.fingerprint.startswith("sha256:")


def test_later_divergent_marker_adds_a_new_stable_violation_identity():
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:later-divergence",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 1)],
    )
    second = copy.deepcopy(begin)
    second["protocol_extension"] = "second"
    initial = J.resolve_effective_events([begin, second, action, commit])

    third = copy.deepcopy(begin)
    third["protocol_extension"] = "third"
    extended = J.resolve_effective_events(
        [begin, second, third, action, commit]
    )

    assert len(initial.protocol_violations) == 1
    assert len(extended.protocol_violations) == 2
    assert {
        item.fingerprint for item in initial.protocol_violations
    } < {
        item.fingerprint for item in extended.protocol_violations
    }
    with pytest.raises(AttributeError):
        extended.protocol_violations[0].kind = "changed"


def test_selector_reports_every_cross_class_violation_in_one_batch():
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:combined-defects",
        family="test_row",
        at=AT,
        actions=[_replace("evt:x", 1)],
    )
    divergent_begin = copy.deepcopy(begin)
    divergent_begin["protocol_extension"] = "conflicting marker"
    action["payload"]["value"] = 999

    selected = J.resolve_effective_events(
        [begin, divergent_begin, action, commit]
    )

    assert selected.active == []
    assert selected.completed_batches == frozenset()
    assert [item.kind for item in selected.protocol_violations] == [
        "manifest_actions_hash_mismatch",
        "marker_conflict",
    ]
    assert len(
        {item.fingerprint for item in selected.protocol_violations}
    ) == 2


def test_selector_reports_event_conflict_and_protocol_taint_together():
    first = _evt("evt:conflict-with-taint", 1)
    second = _evt("evt:conflict-with-taint", 2)
    batch = J.make_correction_batch(
        batch_id="batch:taint-with-conflict",
        family="test_row",
        at=AT,
        actions=[_replace("evt:other", 3)],
    )
    batch[1]["payload"]["value"] = 999

    selected = J.resolve_effective_events([first, second, *batch])

    assert [item.event_id for item in selected.conflicts] == [
        "evt:conflict-with-taint"
    ]
    assert [item.kind for item in selected.protocol_violations] == [
        "manifest_actions_hash_mismatch"
    ]


def test_selector_reports_no_conflicts_for_a_clean_journal():
    selected = J.resolve_effective_events([_evt("evt:x", 1), _evt("evt:y", 2)])

    assert selected.conflicts == ()


def test_selector_taints_committed_batch_with_tampered_action():
    base = _evt("evt:x", 1)
    correction = J.make_correction_batch(
        batch_id="batch:tampered",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )
    correction[1]["payload"]["value"] = 999

    selected = J.resolve_effective_events([base, *correction])

    assert selected.active == [base]
    assert selected.protocol_violations[0].kind == (
        "manifest_actions_hash_mismatch"
    )


def test_selector_taints_divergent_duplicate_batch_sequence():
    correction = J.make_correction_batch(
        batch_id="batch:dupe",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )
    divergent = dict(correction[1])
    divergent["payload"] = {"kind": "test_row", "value": 3}

    selected = J.resolve_effective_events(
        [*correction[:-1], divergent, correction[-1]]
    )

    assert selected.active == []
    assert selected.protocol_violations[0].kind == "action_sequence_conflict"


def test_selector_accepts_exact_duplicate_batch_records():
    base = _evt("evt:x", 1)
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:exact-duplicates",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )

    selected = J.resolve_effective_events(
        [
            base,
            begin,
            copy.deepcopy(begin),
            action,
            copy.deepcopy(action),
            commit,
            copy.deepcopy(commit),
        ]
    )

    assert selected.by_id["evt:x"].rev == 1
    assert selected.by_id["evt:x"].record["payload"]["value"] == 2


def test_selector_taints_begin_commit_marker_extension_mismatch():
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:marker-extension",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )
    begin["protocol_extension"] = {"shape": "first"}
    commit["protocol_extension"] = {"shape": "different"}

    selected = J.resolve_effective_events([begin, action, commit])

    assert selected.active == []
    assert selected.protocol_violations[0].kind == "marker_manifest_mismatch"


@pytest.mark.parametrize(
    "record_order",
    [
        ("begin", "commit", "action"),
        ("commit", "begin", "action"),
    ],
)
def test_selector_taints_commit_that_does_not_follow_manifest_actions(record_order):
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:record-order",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )
    by_name = {"begin": begin, "action": action, "commit": commit}

    selected = J.resolve_effective_events(
        [by_name[name] for name in record_order]
    )

    assert selected.active == []
    assert selected.protocol_violations[0].kind == "record_order_violation"


def test_selector_taints_manifest_actions_out_of_sequence_order():
    begin, first, second, commit = J.make_correction_batch(
        batch_id="batch:action-order",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2), _replace("evt:y", 3)],
    )

    selected = J.resolve_effective_events([begin, second, first, commit])

    assert selected.active == []
    assert selected.protocol_violations[0].kind == "record_order_violation"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda marker: marker.update(phase="unknown"), "phase"),
        (lambda marker: marker.update(action_count=-1), "action_count"),
        (lambda marker: marker.update(actions_hash="not-a-hash"), "actions_hash"),
    ],
)
def test_selector_keeps_invalid_marker_shapes_fail_closed(mutate, message):
    marker = J.make_correction_batch(
        batch_id="batch:bad-marker",
        family="claude-usage",
        at=AT,
        actions=[],
    )[0]
    mutate(marker)

    with pytest.raises(J.JournalProtocolError, match=message):
        J.resolve_effective_events([marker])


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda action: action.update(seq="0"), "seq"),
        (lambda action: action.update(action="unknown"), "action"),
        (lambda action: action.update(rev=-1), "rev"),
    ],
)
def test_selector_keeps_invalid_correction_shapes_fail_closed(
    mutate, message
):
    action = J.make_correction_batch(
        batch_id="batch:bad-action",
        family="claude-usage",
        at=AT,
        actions=[_replace("evt:x", 2)],
    )[1]
    mutate(action)

    with pytest.raises(J.JournalProtocolError, match=message):
        J.resolve_effective_events([action])


def test_selector_preserves_legacy_quota_arming_last_wins_state_stream():
    first = J.make_evt(
        kind="quota_alert_arming",
        id="qaa:legacy-state",
        at=AT,
        payload={k: v for k, v in _arming_payload("first").items() if k != "kind"},
    )
    second = J.make_evt(
        kind="quota_alert_arming",
        id="qaa:legacy-state",
        at="2026-07-25T12:01:00Z",
        payload={k: v for k, v in _arming_payload("second").items() if k != "kind"},
    )

    selected = J.resolve_effective_events([first, second])

    assert selected.active == [second]
    assert selected.by_id["qaa:legacy-state"].record == second
    # #374: the legacy carve-out keeps its last-wins direction AND its silence —
    # it is not a quarantined conflict.
    assert selected.conflicts == ()


def test_selector_quarantines_versioned_quota_arming_same_revision_conflict():
    first = J.make_evt(
        kind="quota_alert_arming",
        id="qaa:versioned-state",
        at=AT,
        payload={
            **{k: v for k, v in _arming_payload("first").items() if k != "kind"},
            "journal_identity_version": 2,
        },
    )
    second = copy.deepcopy(first)
    second["payload"]["rule_fingerprint"] = "second"

    selected = J.resolve_effective_events([first, second])

    # A VERSIONED arming record is an ordinary evt: it gets the universal
    # quarantine, not the legacy last-wins carve-out.
    assert selected.active == [first]
    assert [(c.event_id, c.rev) for c in selected.conflicts] == [
        ("qaa:versioned-state", 0)
    ]


def test_live_legacy_quota_arming_progression_preserves_last_wins(ns):
    core, jr, lib = _siblings()
    first = lib.make_evt(
        kind="quota_alert_arming",
        id="qaa:legacy-live",
        at=AT,
        payload={k: v for k, v in _arming_payload("first").items() if k != "kind"},
    )
    jr.append_record(first, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    second = lib.make_evt(
        kind="quota_alert_arming",
        id="qaa:legacy-live",
        at="2026-07-25T12:01:00Z",
        payload={
            **{k: v for k, v in _arming_payload("second").items() if k != "kind"},
            "activated_at_utc": "2026-07-25T12:01:00Z",
        },
    )
    jr.append_record(second, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = core.open_db()
    try:
        assert tuple(
            conn.execute(
                "SELECT rule_fingerprint, activated_at_utc "
                "FROM quota_alert_arming WHERE source_root_key = 'root-a'"
            ).fetchone()
        ) == ("second", "2026-07-25T12:01:00Z")
        event_json = conn.execute(
            "SELECT event_json FROM journal_effective_events "
            "WHERE event_id = 'qaa:legacy-live'"
        ).fetchone()[0]
        assert lib.decode_line(event_json.encode("utf-8")) == second
    finally:
        conn.close()


def test_rebuild_folds_only_effective_events_for_all_family_shapes(ns, tmp_path):
    core, jr, lib = _siblings()
    base_events = [
        lib.make_evt(
            kind="snapshot_accept",
            id="sa:generic",
            at=AT,
            payload={k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"},
        ),
        lib.make_evt(
            kind="snapshot_accept",
            id="sa:keep",
            at=AT,
            payload={k: v for k, v in _snapshot_payload(30.0).items() if k != "kind"},
        ),
        lib.make_evt(
            kind="snapshot_accept",
            id="sa:retire",
            at=AT,
            payload={k: v for k, v in _snapshot_payload(40.0).items() if k != "kind"},
        ),
        lib.make_evt(
            kind="weekly_credit_effects",
            id="wce:effects",
            at=AT,
            payload={"suppression": ["sa:keep"]},
        ),
        lib.make_evt(
            kind="five_hour_block_close",
            id="fhbc:block",
            at=AT,
            payload={k: v for k, v in _block_payload(1.0).items() if k != "kind"},
        ),
        lib.make_evt(
            kind="quota_alert_arming",
            id="qaa:state",
            at=AT,
            payload={k: v for k, v in _arming_payload("old").items() if k != "kind"},
        ),
    ]
    for event in base_events:
        jr.append_record(event, now_utc=FIXED)
    correction = lib.make_correction_batch(
        batch_id="batch:families",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:generic",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            },
            {
                "action": "replace",
                "id": "wce:effects",
                "rev": 1,
                "at": AT,
                "payload": {"kind": "weekly_credit_effects", "suppression": []},
            },
            _tombstone("sa:retire"),
            {
                "action": "replace",
                "id": "fhbc:block",
                "rev": 1,
                "at": AT,
                "payload": _block_payload(2.0),
            },
            {
                "action": "replace",
                "id": "qaa:state",
                "rev": 1,
                "at": AT,
                "payload": _arming_payload("new"),
            },
        ],
    )
    for record in correction:
        jr.append_record(record, now_utc=FIXED)

    target = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(target_path=str(target))
    conn = core.open_db(_target_path=str(target))
    try:
        snapshots = {
            row["journal_id"]: row["weekly_percent"]
            for row in conn.execute(
                "SELECT journal_id, weekly_percent FROM weekly_usage_snapshots"
            )
        }
        assert snapshots == {"sa:generic": 20.0, "sa:keep": 30.0}
        assert conn.execute(
            "SELECT total_cost_usd FROM five_hour_blocks "
            "WHERE journal_id = 'fhbc:block'"
        ).fetchone()[0] == 2.0
        assert conn.execute(
            "SELECT cost_usd FROM five_hour_block_models "
            "WHERE five_hour_window_key = 987654"
        ).fetchone()[0] == 2.0
        assert conn.execute(
            "SELECT cost_usd FROM five_hour_block_projects "
            "WHERE five_hour_window_key = 987654"
        ).fetchone()[0] == 2.0
        assert conn.execute(
            "SELECT rule_fingerprint FROM quota_alert_arming "
            "WHERE source_root_key = 'root-a'"
        ).fetchone()[0] == "new"
        metadata = {
            row["event_id"]: (row["rev"], row["status"])
            for row in conn.execute(
                "SELECT event_id, rev, status FROM journal_effective_events"
            )
        }
        assert metadata["sa:generic"] == (1, "active")
        assert metadata["sa:retire"] == (1, "tombstone")
    finally:
        conn.close()


def test_rebuild_taints_all_structural_classes_and_publishes_valid_history(
    ns, tmp_path
):
    """#402 Task A: the common scratch rebuild publishes a usable index while
    omitting all seven tainted batches and retaining valid overlapping history."""
    core, jr, lib = _siblings()
    destination = tmp_path / "rebuilt.db"
    base = lib.make_evt(
        kind="snapshot_accept",
        id="sa:overlap",
        at=AT,
        payload={
            k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"
        },
    )
    earlier = lib.make_correction_batch(
        batch_id="batch:valid-before",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:overlap",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            }
        ],
    )
    later = lib.make_correction_batch(
        batch_id="batch:valid-after",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:overlap",
                "rev": 3,
                "at": AT,
                "payload": _snapshot_payload(30.0),
            }
        ],
    )
    kinds = [
        "marker_conflict",
        "commit_without_begin",
        "marker_manifest_mismatch",
        "record_order_violation",
        "manifest_action_sequence_mismatch",
        "manifest_actions_hash_mismatch",
        "action_sequence_conflict",
    ]
    records = [base, *earlier]
    for index, kind in enumerate(kinds):
        records.extend(
            _structural_violation_records(
                kind,
                batch_id=f"batch:invalid-{index}",
                actions=[
                    {
                        "action": "replace",
                        "id": "sa:overlap",
                        "rev": 2,
                        "at": AT,
                        "payload": _snapshot_payload(90.0 + index),
                    },
                    {
                        "action": "replace",
                        "id": f"sa:invalid-only-{index}",
                        "rev": 1,
                        "at": AT,
                        "payload": _snapshot_payload(90.0 + index),
                    },
                ],
            )
        )
    records.extend(later)
    for record in records:
        jr.append_record(record, now_utc=FIXED)

    result = jr.rebuild_stats_index(target_path=str(destination))

    assert [v.kind for v in result.protocol_violations] == kinds
    conn = core.open_db(_target_path=str(destination))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = conn.execute(
            "SELECT journal_id, weekly_percent FROM weekly_usage_snapshots"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("sa:overlap", 30.0)]
        metadata = conn.execute(
            "SELECT event_id, rev, batch_id FROM journal_effective_events "
            "ORDER BY event_id"
        ).fetchall()
        assert [tuple(row) for row in metadata] == [
            ("sa:overlap", 3, "batch:valid-after")
        ]
    finally:
        conn.close()


def test_db_rebuild_reports_tainted_batch_in_json_and_text(ns, capsys):
    import argparse
    import json

    _core, jr, lib = _siblings()
    base = lib.make_evt(
        kind="snapshot_accept",
        id="sa:reported",
        at=AT,
        payload={
            k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"
        },
    )
    invalid = lib.make_correction_batch(
        batch_id="batch:reported",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:reported",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            }
        ],
    )
    invalid[1]["payload"]["weekly_percent"] = 99.0
    for record in [base, *invalid]:
        jr.append_record(record, now_utc=FIXED)

    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=True)
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["journalConflicts"] == []
    assert [
        (item["batchId"], item["kind"])
        for item in payload["journalProtocolViolations"]
    ] == [("batch:reported", "manifest_actions_hash_mismatch")]

    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=False)
    ) == 0
    text = capsys.readouterr().out
    assert "affected correction batches were tainted and omitted" in text
    assert "batch:reported: manifest_actions_hash_mismatch" in text


def test_db_rebuild_text_lists_every_structural_violation(ns, capsys):
    import argparse

    _core, jr, lib = _siblings()
    for index in range(12):
        invalid = lib.make_correction_batch(
            batch_id=f"batch:text-{index:02d}",
            family="claude-usage",
            at=AT,
            actions=[_replace(f"sa:text-{index:02d}", 20.0, rev=1)],
        )
        invalid[1]["payload"]["weekly_percent"] = 99.0
        for record in invalid:
            jr.append_record(record, now_utc=FIXED)

    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=False)
    ) == 0
    text = capsys.readouterr().out
    for index in range(12):
        assert (
            f"batch:text-{index:02d}: manifest_actions_hash_mismatch"
            in text
        )
    assert "more (see --json for all)" not in text


def test_rebuild_quarantines_divergent_same_revision_events_and_completes(
    ns, tmp_path
):
    """#374: a rebuild over a journal carrying divergent same-revision events
    COMPLETES behind the lowest-sequence provisional winner instead of aborting
    (the epoch-1002 wedge)."""
    core, jr, lib = _siblings()
    destination = tmp_path / "rebuilt.db"

    for percent in (10.0, 20.0, 30.0):
        jr.append_record(
            lib.make_evt(
                kind="snapshot_accept",
                id="sa:conflict",
                at=AT,
                payload={
                    k: v for k, v in _snapshot_payload(percent).items() if k != "kind"
                },
            ),
            now_utc=FIXED,
        )

    jr.rebuild_stats_index(target_path=str(destination))

    conn = core.open_db(_target_path=str(destination))
    try:
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:conflict'"
        ).fetchone()[0] == 10.0
    finally:
        conn.close()


def test_live_completed_correction_automatically_rebuilds_and_converges(
    ns, tmp_path
):
    core, jr, lib = _siblings()
    dispatched = []
    jr.ALERT_DISPATCHER = lambda alerts: dispatched.extend(alerts)
    base = lib.make_evt(
        kind="snapshot_accept",
        id="sa:live",
        at=AT,
        payload={k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"},
    )
    jr.append_record(base, now_utc=FIXED)
    original_bytes = lib.encode_line(base)
    jr.run_stats_ingest(mode="authoritative")

    conn = core.open_db()
    try:
        metadata = conn.execute(
            "SELECT rev, status FROM journal_effective_events "
            "WHERE event_id = 'sa:live'"
        ).fetchone()
        assert tuple(metadata) == (0, "active")
    finally:
        conn.close()

    correction = lib.make_correction_batch(
        batch_id="batch:live",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:live",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            }
        ],
    )
    for record in correction[:-1]:
        jr.append_record(record, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = core.open_db()
    try:
        cursor_before_commit = jr._read_cursor(conn)
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:live'"
        ).fetchone()[0] == 10.0
    finally:
        conn.close()

    jr.append_record(correction[-1], now_utc=FIXED)
    correction_high_water = jr.journal_high_water()
    result = jr.run_stats_ingest(mode="authoritative")
    assert result.error is None

    conn = core.open_db()
    try:
        assert jr._read_cursor(conn) == correction_high_water
        assert jr._read_cursor(conn) != cursor_before_commit
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:live'"
        ).fetchone()[0] == 20.0
        assert conn.execute(
            "SELECT rev FROM journal_effective_events "
            "WHERE event_id = 'sa:live'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    live = core.open_db()
    independent_path = tmp_path / "independent.db"
    jr.rebuild_stats_index(target_path=str(independent_path))
    independent = core.open_db(_target_path=str(independent_path))
    try:
        live_row = tuple(
            live.execute(
                "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots"
            ).fetchone()
        )
        independent_row = tuple(
            independent.execute(
                "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots"
            ).fetchone()
        )
        assert live_row == independent_row == (20.0, "sa:live")
        assert live.execute(
            "SELECT rev FROM journal_effective_events WHERE event_id = 'sa:live'"
        ).fetchone()[0] == 1
        journal_bytes = b"".join(
            (core.JOURNAL_DIR / segment).read_bytes()
            for segment in jr.list_segments()
        )
        assert original_bytes in journal_bytes
        assert dispatched == []
    finally:
        live.close()
        independent.close()


def test_live_malformed_revision_does_not_mutate_or_advance_cursor(ns):
    core, jr, _lib = _siblings()
    invalid = {
        "v": 1,
        "t": "evt",
        "id": "sa:invalid",
        "rev": "1",
        "at": AT,
        "src": "ingest",
        "payload": _snapshot_payload(10.0),
    }
    jr.append_record(invalid, now_utc=FIXED)

    with pytest.raises(J.JournalProtocolError, match="non-negative integer"):
        jr.run_stats_ingest(mode="authoritative")

    conn = core.open_db()
    try:
        assert jr._read_cursor(conn) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("crash_point", ["fold", "swap"])
def test_completed_correction_rebuild_crash_preserves_old_index_and_retries(
    ns, monkeypatch, crash_point
):
    core, jr, lib = _siblings()
    base = lib.make_evt(
        kind="snapshot_accept",
        id="sa:crash",
        at=AT,
        payload={k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"},
    )
    jr.append_record(base, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    correction = lib.make_correction_batch(
        batch_id=f"batch:crash:{crash_point}",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:crash",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            }
        ],
    )
    for record in correction:
        jr.append_record(record, now_utc=FIXED)

    if crash_point == "fold":
        real_apply = jr._apply_evt

        def crash_apply(conn, event):
            if event.get("id") == "sa:crash" and event.get("rev") == 1:
                raise RuntimeError("simulated correction fold crash")
            return real_apply(conn, event)

        monkeypatch.setattr(jr, "_apply_evt", crash_apply)
    else:
        real_replace = jr.os.replace

        def crash_replace(source, destination):
            if ".rebuilding-" in str(source):
                raise RuntimeError("simulated correction swap crash")
            return real_replace(source, destination)

        monkeypatch.setattr(jr.os, "replace", crash_replace)

    with pytest.raises(RuntimeError, match="simulated correction"):
        jr.rebuild_stats_index()

    conn = core.open_db()
    try:
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:crash'"
        ).fetchone()[0] == 10.0
    finally:
        conn.close()

    if crash_point == "fold":
        monkeypatch.setattr(jr, "_apply_evt", real_apply)
    else:
        monkeypatch.setattr(jr.os, "replace", real_replace)
    jr.rebuild_stats_index()
    conn = core.open_db()
    try:
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:crash'"
        ).fetchone()[0] == 20.0
        assert conn.execute(
            "SELECT rev FROM journal_effective_events WHERE event_id = 'sa:crash'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_live_divergent_same_revision_duplicate_is_quarantined_and_advances(ns):
    """#374: a divergent same-revision line already on disk no longer wedges the
    cycle. The preflight reader drops it, the prior effective event stands, the
    cursor advances, and the cycle reports the quarantined group."""
    core, jr, lib = _siblings()
    first = lib.make_evt(
        kind="snapshot_accept",
        id="sa:dupe-live",
        at=AT,
        payload={k: v for k, v in _snapshot_payload(10.0).items() if k != "kind"},
    )
    jr.append_record(first, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    conn = core.open_db()
    try:
        cursor_before = jr._read_cursor(conn)
    finally:
        conn.close()

    divergent = lib.make_evt(
        kind="snapshot_accept",
        id="sa:dupe-live",
        at=AT,
        payload={k: v for k, v in _snapshot_payload(20.0).items() if k != "kind"},
    )
    jr.append_record(divergent, now_utc=FIXED)
    result = jr.run_stats_ingest(mode="authoritative")

    assert result.error is None
    assert result.conflicts_dropped == 1

    conn = core.open_db()
    try:
        assert jr._read_cursor(conn) != cursor_before
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:dupe-live'"
        ).fetchone()[0] == 10.0
        assert conn.execute(
            "SELECT content_hash FROM journal_effective_events "
            "WHERE event_id = 'sa:dupe-live'"
        ).fetchone()[0] == J._sha256_canonical(first)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("raw_epoch", "opened_epoch"),
    [(1004, 1004), (0, 1004), (0, 0)],
)
def test_live_ingest_acquires_maintenance_before_ingest_lock(
    ns, monkeypatch, raw_epoch, opened_epoch
):
    core, jr, _lib = _siblings()
    calls = []

    class FakeConnection:
        def execute(self, sql):
            calls.append("epoch-check")
            return self

        def fetchone(self):
            return (opened_epoch,)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(
        jr,
        "_stats_db_identity",
        lambda: calls.append("identity") or (1, 2),
    )
    monkeypatch.setattr(
        jr,
        "_stats_db_user_version",
        lambda: calls.append("version") or raw_epoch,
    )
    monkeypatch.setattr(
        jr,
        "_acquire_maintenance_shared",
        lambda mode, timeout: calls.append("maintenance-shared") or 11,
    )
    monkeypatch.setattr(
        jr,
        "_acquire_maintenance_exclusive",
        lambda mode, timeout: calls.append("maintenance-exclusive") or 11,
    )
    monkeypatch.setattr(
        jr,
        "_downgrade_maintenance_shared",
        lambda fd: calls.append("maintenance-downgrade"),
    )
    monkeypatch.setattr(
        jr,
        "_acquire_ingest_lock",
        lambda mode, timeout: calls.append("ingest") or 12,
    )
    monkeypatch.setattr(
        core, "open_db", lambda: calls.append("open") or FakeConnection()
    )
    monkeypatch.setattr(
        jr, "_run_cycle", lambda conn, **kwargs: calls.append("cycle") or "ok"
    )
    monkeypatch.setattr(
        jr,
        "_release_ingest_lock",
        lambda fd: calls.append("release-ingest"),
    )
    monkeypatch.setattr(
        jr,
        "_release_maintenance_shared",
        lambda fd: calls.append("release-maintenance"),
    )

    assert jr.run_stats_ingest(mode="authoritative") == "ok"
    if raw_epoch <= core.LEGACY_STATS_HEAD:
        assert calls == [
            "version",
            "maintenance-exclusive",
            "identity",
            "open",
            "maintenance-downgrade",
            "identity",
            "epoch-check",
            "ingest",
            "cycle",
            "close",
            "release-ingest",
            "release-maintenance",
        ]
    else:
        assert calls == [
            "version",
            "identity",
            "open",
            "maintenance-shared",
            "identity",
            "epoch-check",
            "ingest",
            "cycle",
            "close",
            "release-ingest",
            "release-maintenance",
        ]


def test_epoch_resolver_serializes_maintenance_then_ingest(
    ns, monkeypatch
):
    core, jr, _lib = _siblings()
    store = importlib.import_module("_cctally_store")
    db = importlib.import_module("_cctally_db")
    calls = []

    class FakeConnection:
        pass

    monkeypatch.setattr(
        db, "_would_block_prod_stats", lambda path: False
    )
    monkeypatch.setattr(
        store,
        "_heal_flock_blocking",
        lambda path: calls.append("maintenance") or 21,
    )
    monkeypatch.setattr(
        store, "_raw_user_version", lambda path: core.STATS_INDEX_EPOCH - 1
    )
    monkeypatch.setattr(
        jr,
        "_journal_rebuild_snapshot",
        lambda: (("segment", 1), True),
    )
    monkeypatch.setattr(
        jr,
        "_acquire_ingest_lock",
        lambda mode, timeout: calls.append("ingest") or 22,
    )
    # #388: the resolver no longer drains or quarantines before the replacement
    # exists. The common rebuild cutover owns those operations after validation.
    monkeypatch.setattr(
        jr, "run_epoch_transition", lambda: calls.append("rebuild")
    )
    monkeypatch.setattr(
        jr,
        "_release_ingest_lock",
        lambda fd: calls.append("release-ingest"),
    )
    monkeypatch.setattr(
        store,
        "_heal_release_maintenance_flock",
        lambda fd: calls.append("release-maintenance"),
    )
    monkeypatch.setattr(core, "open_db", lambda: FakeConnection())

    result = store.resolve_stats_epoch_mismatch()

    assert isinstance(result, FakeConnection)
    assert calls == [
        "maintenance",
        "ingest",
        "rebuild",
        "release-ingest",
        "release-maintenance",
    ]
