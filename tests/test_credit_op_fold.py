"""A manual credit folds into the unified credit table (#703 + #707).

`record-credit` keeps emitting its existing `weekly_credit_floor` op, so no
already-written journal line changes meaning. What changed is where that op's
fold applier puts the row: `week_reset_events`, the single durable
representation of an Anthropic weekly credit, with `journal_id` set to the op's
identifier. Harvest only scans rows whose `journal_id IS NULL`, so the manual
row is never double-emitted as a `wr:` event.

Unifying is what lets a manual credit restart the milestone ladder.
`percent_milestones.reset_event_id`'s derived reference resolves only through
`week_reset_events`, and that missing foreign key is precisely why a manual
credit opened no milestone epoch before this change.

Two legacy op shapes reach the applier and they are NOT alike:

  runtime-journaled  carries the full `CreditPlan`, which already includes
                     `captured_iso` and `to_pct` — exactly the exact-instant and
                     post-credit facts the unified row records. Treating every
                     old op as fact-less would needlessly drop such installs
                     back onto the hour-floored instant after upgrade.
  cutover-exported   comes from `weekly_credit_floors` rows exported directly at
                     cutover. That table retains no week-end timestamp at all,
                     so the boundary columns are NULL, the fact columns are NULL,
                     and the key is the `legacy:` form. Nothing is invented.

Spec: docs/superpowers/specs/2026-09-02-703-707-anthropic-same-window-credit.md
sections 3, 4 and 7.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"
WEEK_END_AT = "2026-09-05T00:00:00+00:00"
EFFECTIVE = "2026-08-30T10:00:00+00:00"
CAPTURED = "2026-08-30T10:37:00Z"


def _runtime_op(op_id="o:runtimecredit"):
    """The shape `cmd_record_credit` appends today (`bin/_cctally_record.py`)."""
    return {
        "v": 1, "t": "op", "id": op_id, "at": "2026-08-30T14:00:00Z",
        "src": "record-credit",
        "payload": {
            "kind": "weekly_credit_floor",
            "week_start_date": WEEK_START_DATE,
            "effective_at_utc": EFFECTIVE,
            "observed_pre_credit_pct": 46.0,
            "applied_at_utc": "2026-08-30T14:00:00Z",
            "plan": {
                "week_start_date": WEEK_START_DATE,
                "week_start_at": WEEK_START_AT,
                "week_end_at": WEEK_END_AT,
                "cur_end_canon": WEEK_END_AT,
                "from_pct": 46.0,
                "from_source": "hwm",
                "to_pct": 31.0,
                "effective_iso": EFFECTIVE,
                "captured_iso": CAPTURED,
            },
            "five_hour": [],
            "forced": False,
            "account_key": "unattributed",
        },
    }


def _cutover_op(op_id="b:weekly_credit_floors:7"):
    """The shape the cutover exports: the floors table's own columns, and that
    table retains no week-end timestamp."""
    return {
        "v": 1, "t": "op", "id": op_id, "at": "2026-08-30T14:00:00Z",
        "src": "cutover",
        "payload": {
            "kind": "weekly_credit_floor",
            "week_start_date": WEEK_START_DATE,
            "effective_at_utc": EFFECTIVE,
            "observed_pre_credit_pct": 46.0,
            "applied_at_utc": "2026-08-30T14:00:00Z",
            "account_key": "unattributed",
        },
    }


def _fold(ns, record):
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        jr.FOLD_APPLIERS[record["payload"]["kind"]](conn, record)
        conn.commit()
        return conn.execute(
            "SELECT * FROM week_reset_events").fetchall()
    finally:
        conn.close()


def test_a_runtime_journaled_op_folds_with_full_facts(ns):
    op = _runtime_op()
    rows = _fold(ns, op)
    assert len(rows) == 1, [dict(r) for r in rows]
    row = rows[0]
    assert row["journal_id"] == op["id"]
    assert row["credit_key"] == op["id"]
    assert row["credit_order"] is not None
    assert row["week_start_date"] == WEEK_START_DATE
    assert row["observed_at_utc"] == CAPTURED
    assert row["observed_post_credit_pct"] == 31.0
    assert row["observed_pre_credit_pct"] == 46.0
    assert row["effective_reset_at_utc"] == EFFECTIVE
    # A retroactive assertion has no confirming observation, so there is no
    # upper bracket end to record (spec section 5.1).
    assert row["confirming_capture_at_utc"] is None


def test_a_manual_credit_never_re_anchors_the_display_window(ns):
    """The boundary columns stay NULL. The window did not move — that is the
    whole rule this design encodes — and a non-NULL boundary here would make the
    display layer truncate or restart the week."""
    rows = _fold(ns, _runtime_op())
    assert rows[0]["old_week_end_at"] is None
    assert rows[0]["new_week_end_at"] is None


def test_a_cutover_exported_op_folds_with_null_boundaries(ns):
    op = _cutover_op()
    rows = _fold(ns, op)
    assert len(rows) == 1, [dict(r) for r in rows]
    row = rows[0]
    assert row["old_week_end_at"] is None
    assert row["new_week_end_at"] is None
    assert row["observed_at_utc"] is None
    assert row["confirming_capture_at_utc"] is None
    assert row["observed_post_credit_pct"] is None
    assert row["credit_key"] == f"legacy:{op['id']}"
    # The facts the impoverished shape DOES carry are still recorded.
    assert row["week_start_date"] == WEEK_START_DATE
    assert row["observed_pre_credit_pct"] == 46.0
    assert row["effective_reset_at_utc"] == EFFECTIVE


def test_the_fold_is_idempotent_under_replay(ns):
    import _cctally_journal as jr
    op = _runtime_op()
    conn = ns["open_db"]()
    try:
        for _ in range(3):
            jr.FOLD_APPLIERS["weekly_credit_floor"](conn, op)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_two_manual_credits_in_one_week_both_fold(ns):
    """The manual path supported several credits per week before unification
    and must not lose that. Distinct ops are distinct credits."""
    import _cctally_journal as jr
    first = _runtime_op("o:manualone")
    second = _runtime_op("o:manualtwo")
    second["payload"]["effective_at_utc"] = "2026-08-30T12:00:00+00:00"
    second["payload"]["plan"]["effective_iso"] = "2026-08-30T12:00:00+00:00"
    second["payload"]["plan"]["captured_iso"] = "2026-08-30T12:15:00Z"
    second["payload"]["plan"]["to_pct"] = 20.0
    conn = ns["open_db"]()
    try:
        for op in (first, second):
            jr.FOLD_APPLIERS["weekly_credit_floor"](conn, op)
        conn.commit()
        keys = [r["credit_key"] for r in conn.execute(
            "SELECT credit_key FROM week_reset_events ORDER BY credit_order")]
        assert keys == ["o:manualone", "o:manualtwo"], keys
    finally:
        conn.close()


def test_the_manual_row_is_never_double_emitted_by_harvest(ns):
    """Harvest scans `journal_id IS NULL`. The op fold stamps the op's id, so
    the row is already journal-identified and harvest must skip it."""
    import _cctally_journal as jr
    op = _runtime_op()
    conn = ns["open_db"]()
    try:
        conn.execute("BEGIN IMMEDIATE")
        jr.FOLD_APPLIERS["weekly_credit_floor"](conn, op)
        ctx = jr.IngestContext(conn=conn, batch=[])
        jr._harvest(ctx)
        conn.commit()
        assert ctx.events_emitted == 0, (
            "harvest re-emitted a row the op fold already identified")
    finally:
        conn.close()


def test_the_op_fold_is_the_only_materialization(ns):
    """`weekly_credit_floors` stops being a second materialization of the same
    concept, so the applier writes exactly one row, in one table."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        jr.FOLD_APPLIERS["weekly_credit_floor"](conn, _runtime_op())
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_credit_floors").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_completion_op_finishes_the_same_credit_rather_than_adding_one(ns):
    """A crash between the credit record and its synthetic snapshot leaves a
    half-applied credit. The plain rerun that finishes it reuses the existing
    record's effective instant, and it must reuse that record's IDENTITY too:
    it is the same credit. Without that the completion op's own content digest
    would file a duplicate, because a distinct op is otherwise a distinct
    credit."""
    import _cctally_journal as jr
    first = _runtime_op("o:halfapplied")
    completion = _runtime_op("o:completion")
    completion["at"] = "2026-08-30T15:07:00Z"
    completion["payload"]["applied_at_utc"] = "2026-08-30T15:07:00Z"
    completion["payload"]["completes_credit_key"] = "o:halfapplied"
    conn = ns["open_db"]()
    try:
        for op in (first, completion):
            jr.FOLD_APPLIERS["weekly_credit_floor"](conn, op)
        conn.commit()
        rows = conn.execute(
            "SELECT credit_key, journal_id, effective_reset_at_utc "
            "FROM week_reset_events").fetchall()
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["credit_key"] == "o:halfapplied"
        assert rows[0]["journal_id"] == "o:halfapplied"
        assert rows[0]["effective_reset_at_utc"] == EFFECTIVE, (
            "the completion moved the effective instant forward")
    finally:
        conn.close()
