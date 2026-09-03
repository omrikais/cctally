"""Several Anthropic credits in one week are representable (#703 + #707).

`week_reset_events` was keyed on the week boundaries, and two independent gates
kept a second credit in one week out of it entirely: the live detector
pre-checked `new_week_end_at` alone (`bin/_cctally_record.py`), and the backfill
repeated that check (`bin/_cctally_weekrefs.py`). Even without them, the row
constraint `UNIQUE(account_key, old_week_end_at, new_week_end_at)` collided for
two credits inside one hour, because the stored boundary is hour-floored.

Unifying manual credits onto this table would therefore have REGRESSED the
manual path, which is keyed `UNIQUE(account_key, week_start_date,
effective_at_utc)` and already supports several credits per week. So identity
moves off the boundaries onto `credit_key`, derived from the source journal
record, and both singleton gates become an exact `(account_key, credit_key)`
lookup. Because the key comes from the source record, two distinct observations
admit two credits in the same week or the same hour, while a repeat of the
identical source record stays an idempotent retry.

Spec: docs/superpowers/specs/2026-09-02-703-707-anthropic-same-window-credit.md
sections 3.1 and 4.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


WEEK_END_DT = dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_ISO = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_END_EPOCH = int(WEEK_END_DT.timestamp())
WEEK_START_DATE = "2026-08-29"


def _tick(ns, monkeypatch, *, at: str, percent: float) -> int:
    """One `record-usage` observation at a PINNED capture instant."""
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent,
        resets_at=WEEK_END_EPOCH,
        five_hour_percent=None,
        five_hour_resets_at=None,
        week_start_name=None,
    ))


def _events(conn):
    return conn.execute(
        "SELECT id, credit_key, credit_order, week_start_date, "
        "       observed_at_utc, confirming_capture_at_utc, "
        "       observed_post_credit_pct, effective_reset_at_utc "
        "FROM week_reset_events ORDER BY id"
    ).fetchall()


def _epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(
        iso.replace("Z", "+00:00")).timestamp())


# ── two credits in one week ────────────────────────────────────────────

def _drive_two_credits(ns, monkeypatch, *, second_hour: bool):
    """60 -> 20 (credit one), climb to 50, 50 -> 10 (credit two).

    ``second_hour`` puts the two credits in DIFFERENT hours; otherwise both
    hour-floor to the same instant, which is the constraint-collision case.
    """
    assert _tick(ns, monkeypatch, at="2026-08-30T10:00:00Z", percent=60.0) == 0
    assert _tick(ns, monkeypatch, at="2026-08-30T10:10:00Z", percent=20.0) == 0
    if second_hour:
        assert _tick(ns, monkeypatch, at="2026-08-30T12:00:00Z",
                     percent=50.0) == 0
        assert _tick(ns, monkeypatch, at="2026-08-30T12:30:00Z",
                     percent=10.0) == 0
    else:
        assert _tick(ns, monkeypatch, at="2026-08-30T10:30:00Z",
                     percent=50.0) == 0
        assert _tick(ns, monkeypatch, at="2026-08-30T10:50:00Z",
                     percent=10.0) == 0


def test_two_credits_in_one_week_both_persist(ns, monkeypatch):
    _drive_two_credits(ns, monkeypatch, second_hour=True)
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 2, (
            "the second credit was suppressed by a singleton gate: "
            f"{[dict(r) for r in rows]}")
        assert rows[0]["credit_key"] and rows[1]["credit_key"]
        assert rows[0]["credit_key"] != rows[1]["credit_key"]
        assert all(r["week_start_date"] == WEEK_START_DATE for r in rows)
    finally:
        conn.close()


def test_two_credits_in_the_same_hour_both_persist(ns, monkeypatch):
    _drive_two_credits(ns, monkeypatch, second_hour=False)
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 2, (
            "hour-floored boundaries collided: "
            f"{[dict(r) for r in rows]}")
        assert (rows[0]["effective_reset_at_utc"]
                == rows[1]["effective_reset_at_utc"]), (
            "the fixture no longer exercises the collision it exists for")
        assert rows[0]["credit_key"] != rows[1]["credit_key"]
    finally:
        conn.close()


def test_the_credit_order_of_the_second_credit_is_later(ns, monkeypatch):
    """`credit_order` records the SOURCE record's journal position, so it
    increases with real occurrence even when both rows hour-floor together."""
    _drive_two_credits(ns, monkeypatch, second_hour=False)
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 2, [dict(r) for r in rows]
        assert rows[0]["credit_order"] is not None
        assert rows[1]["credit_order"] is not None
        assert rows[1]["credit_order"] > rows[0]["credit_order"]
    finally:
        conn.close()


# ── the facts each automatic row records ───────────────────────────────

def test_the_automatic_row_records_the_exact_capture_domain_instant(
    ns, monkeypatch
):
    """`observed_at_utc` is the triggering observation's `payload.captured_at`,
    NOT the hour-floored display anchor and NOT the detection clock. It filters
    `weekly_usage_snapshots.captured_at_utc`, so it must share that domain."""
    _drive_two_credits(ns, monkeypatch, second_hour=True)
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 2, [dict(r) for r in rows]
        assert _epoch(rows[0]["observed_at_utc"]) == _epoch(
            "2026-08-30T10:10:00Z")
        assert _epoch(rows[1]["observed_at_utc"]) == _epoch(
            "2026-08-30T12:30:00Z")
        # The display anchor stays hour-floored and is a DIFFERENT instant.
        assert _epoch(rows[0]["effective_reset_at_utc"]) == _epoch(
            "2026-08-30T10:00:00Z")
    finally:
        conn.close()


def test_the_automatic_row_records_where_the_counter_landed(ns, monkeypatch):
    _drive_two_credits(ns, monkeypatch, second_hour=True)
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 2, [dict(r) for r in rows]
        assert rows[0]["observed_post_credit_pct"] == 20.0
        assert rows[1]["observed_post_credit_pct"] == 10.0
        # The immediate leg observes and confirms in one tick.
        assert _epoch(rows[0]["confirming_capture_at_utc"]) == _epoch(
            "2026-08-30T10:10:00Z")
    finally:
        conn.close()


def test_the_debounced_leg_records_the_first_zero_capture_instant(
    ns, monkeypatch
):
    """The reset-to-zero leg anchors on the FIRST zero's exact capture instant
    and is confirmed by the second. The hour floor stays display-only."""
    assert _tick(ns, monkeypatch, at="2026-09-01T17:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0
    conn = ns["open_db"]()
    try:
        rows = _events(conn)
        assert len(rows) == 1, [dict(r) for r in rows]
        row = rows[0]
        assert _epoch(row["observed_at_utc"]) == _epoch(
            "2026-09-01T17:59:41Z"), (
            "the exact first-zero capture instant was discarded")
        assert _epoch(row["confirming_capture_at_utc"]) == _epoch(
            "2026-09-01T17:59:47Z")
        assert row["observed_post_credit_pct"] == 0.0
        assert _epoch(row["effective_reset_at_utc"]) == _epoch(
            "2026-09-01T17:00:00Z"), "the display anchor stays hour-floored"
        assert row["credit_key"]
    finally:
        conn.close()


# ── identity is the gate, not the boundary ─────────────────────────────

def test_an_identical_source_record_is_an_idempotent_retry(ns):
    """The same source record fired twice writes one row, because the exact
    `(account_key, credit_key)` lookup is what gates the insert."""
    import _cctally_journal as jr
    from _lib_credit_identity import CreditSource

    conn = ns["open_db"]()
    try:
        source = CreditSource(kind="immediate", identity="o:abc123", order=7)
        effective_dt = dt.datetime(2026, 8, 30, 10, 0, 0,
                                   tzinfo=dt.timezone.utc)
        for _ in range(2):
            ctx = jr.IngestContext(conn=conn, batch=[])
            ns["_fire_in_place_credit"](
                conn, WEEK_START_DATE, WEEK_END_ISO, 20.0,
                observed_pre_credit_pct=60.0, effective_dt=effective_dt,
                as_of="2026-08-30T10:10:00Z", commit=True, ctx=ctx,
                credit_source=source,
                observed_at_utc="2026-08-30T10:10:00Z",
                confirming_capture_at_utc="2026-08-30T10:10:00Z",
            )
        rows = _events(conn)
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["credit_key"] == "o:abc123"
        assert rows[0]["credit_order"] == 7
    finally:
        conn.close()


def test_a_different_source_record_on_the_same_boundary_is_a_second_credit(ns):
    """The old gate refused this outright; the boundary is no longer identity."""
    import _cctally_journal as jr
    from _lib_credit_identity import CreditSource

    conn = ns["open_db"]()
    try:
        effective_dt = dt.datetime(2026, 8, 30, 10, 0, 0,
                                   tzinfo=dt.timezone.utc)
        for ident, order, landed in (("o:aaa", 1, 20.0), ("o:bbb", 2, 10.0)):
            ctx = jr.IngestContext(conn=conn, batch=[])
            ns["_fire_in_place_credit"](
                conn, WEEK_START_DATE, WEEK_END_ISO, landed,
                observed_pre_credit_pct=60.0, effective_dt=effective_dt,
                as_of="2026-08-30T10:10:00Z", commit=True, ctx=ctx,
                credit_source=CreditSource(
                    kind="immediate", identity=ident, order=order),
                observed_at_utc="2026-08-30T10:10:00Z",
                confirming_capture_at_utc="2026-08-30T10:10:00Z",
            )
        rows = _events(conn)
        assert len(rows) == 2, [dict(r) for r in rows]
        assert [r["credit_key"] for r in rows] == ["o:aaa", "o:bbb"]
    finally:
        conn.close()


def test_the_harvest_natural_key_is_the_account_and_the_credit_key():
    """The row constraint and the harvest natural key must be the same
    identity, or the opaque evt id stops being a bijection with the row."""
    import _cctally_journal as jr

    spec = next(s for s in jr._HARVEST_SPECS if s.table == "week_reset_events")
    assert spec.id_parts == ("account_key", "credit_key")


def test_the_row_constraint_is_the_account_and_the_credit_key(ns):
    conn = ns["open_db"]()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' "
            "AND name='week_reset_events'").fetchone()[0]
        assert "UNIQUE(account_key, credit_key)" in sql, sql
        assert "old_week_end_at, new_week_end_at" not in sql.replace(
            "old_week_end_at        TEXT", "").replace(
            "new_week_end_at        TEXT", ""), sql
    finally:
        conn.close()


def test_the_pivots_run_when_the_insert_is_skipped(ns):
    """A crash between the event INSERT and the pivots must heal on the next
    tick, so the pivots run UNCONDITIONALLY while the INSERT stays gated.

    This is the identified half of the pair whose keyless half lives at
    `tests/test_in_place_credit_detection.py::test_weekly_pivots_run_when_event_row_already_committed`.
    Naming the source record is what makes the pre-check recognise the row and
    actually reach the skip.
    """
    import _cctally_journal as jr
    from _lib_credit_identity import CreditSource

    source = CreditSource(kind="immediate", identity="sa:o:pivots", order=3)
    effective_dt = dt.datetime(2026, 8, 30, 10, 0, 0, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        for _ in range(2):
            ctx = jr.IngestContext(conn=conn, batch=[])
            ns["_fire_in_place_credit"](
                conn, WEEK_START_DATE, WEEK_END_ISO, 20.0,
                observed_pre_credit_pct=60.0, effective_dt=effective_dt,
                as_of="2026-08-30T10:10:00Z", commit=True, ctx=ctx,
                credit_source=source,
                observed_at_utc="2026-08-30T10:10:00Z",
                confirming_capture_at_utc="2026-08-30T10:10:00Z",
            )
            # The hwm force-write is a pivot; it must land on BOTH passes,
            # including the one whose INSERT the pre-check skipped.
            hwm = (ns["APP_DIR"] / "hwm-7d").read_text().strip().split()
            assert hwm == [WEEK_START_DATE, "20.0"], hwm
            (ns["APP_DIR"] / "hwm-7d").write_text(
                f"{WEEK_START_DATE} 60.0\n")
        assert len(_events(conn)) == 1
    finally:
        conn.close()


# ── the two shape inferences get real guards (#703 + #707 review) ───────

def test_an_automatic_credit_cannot_write_the_manual_row_shape(ns):
    """A manual credit is recognised by BOTH boundary columns being NULL.

    Four helpers classify on that shape — `_manual_credits_at`,
    `_manual_credits_in_week`, `_EXISTING_MANUAL_CREDIT_SQL` and
    `_week_segment_boundaries` — and the predicate is total only for as long as
    no automatic leg writes NULL there. That is an inference about every future
    caller, so the automatic writer refuses the shape outright rather than
    leaving it merely unlikely.
    """
    import _cctally_journal as jr
    from _lib_credit_identity import CreditSource

    conn = ns["open_db"]()
    try:
        ctx = jr.IngestContext(conn=conn, batch=[])
        with pytest.raises(ValueError, match="boundary"):
            ns["_fire_in_place_credit"](
                conn, WEEK_START_DATE, None, 20.0,
                observed_pre_credit_pct=60.0,
                effective_dt=dt.datetime(2026, 8, 30, 10, 0, 0,
                                         tzinfo=dt.timezone.utc),
                as_of="2026-08-30T10:10:00Z", commit=True, ctx=ctx,
                credit_source=CreditSource(
                    kind="immediate", identity="o:nullbound", order=1),
            )
        assert _events(conn) == []
    finally:
        conn.close()


def test_a_sourceless_automatic_credit_is_not_inserted_twice(ns):
    """`UNIQUE(account_key, credit_key)` treats NULLs as distinct.

    A caller with no source record has no `credit_key`, so the constraint stops
    deduping and every pass would append another row. The one production caller
    always supplies a source, but that is a caller convention rather than a
    guarantee, so the keyless path falls back to the boundary pair — the exact
    identity such a row HAS, and the one the pre-change gate used.
    """
    conn = ns["open_db"]()
    try:
        effective_dt = dt.datetime(2026, 8, 30, 10, 0, 0,
                                   tzinfo=dt.timezone.utc)
        for _ in range(3):
            ns["_fire_in_place_credit"](
                conn, WEEK_START_DATE, WEEK_END_ISO, 20.0,
                observed_pre_credit_pct=60.0, effective_dt=effective_dt,
                as_of="2026-08-30T10:10:00Z", commit=True, ctx=None,
                credit_source=None,
            )
        rows = _events(conn)
        assert len(rows) == 1, (
            "a keyless credit appended a duplicate row on every pass: "
            f"{[dict(r) for r in rows]}")
        assert rows[0]["credit_key"] is None
    finally:
        conn.close()


def test_the_backfill_does_not_double_record_a_manual_credit(ns, monkeypatch):
    """A >=25pp manual credit must not also be synthesized as an automatic one.

    The scan reads `weekly_usage_snapshots`, and a manual credit leaves exactly
    the shape it fires on: the pre-credit reading, then the synthetic
    post-credit reading, same week end, a drop past the threshold. Neither
    existing gate sees the manual row — the snapshot-derived `credit_key` never
    equals the op-derived one, and the keyless-legacy arm requires a NULL key
    the manual row does not have. Both credits then land in ONE table, so the
    week grows a second epoch nobody recorded.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-30T12:00:00Z")
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, page_url, source, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-08-30T09:30:00Z", WEEK_START_DATE, "2026-09-05",
             "2026-08-29T00:00:00+00:00", WEEK_END_ISO, 60.0, None,
             "statusline", "{}"))
        conn.commit()
    finally:
        conn.close()

    assert ns["cmd_record_credit"](argparse.Namespace(
        to=10.0, from_pct=60.0, at="2026-08-30T10:45:00Z",
        week=WEEK_START_DATE, dry_run=False, yes=True, json=False,
        force=False)) == 0

    conn = ns["open_db"]()
    try:
        before = _events(conn)
        assert len(before) == 1, [dict(r) for r in before]
        ns["_backfill_week_reset_events"](conn)
        after = _events(conn)
    finally:
        conn.close()
    assert len(after) == 1, (
        "the backfill synthesized a second, automatic-shaped row for a credit "
        f"record-credit already recorded: {[dict(r) for r in after]}")
    assert after[0]["credit_key"] == before[0]["credit_key"]
