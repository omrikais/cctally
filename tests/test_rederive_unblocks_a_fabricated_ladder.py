"""§8's repair path, exercised rather than asserted.

Section 8 says an operator whose store already holds the 2026-09-01 damage runs
`cctally db rederive --family claude-usage --yes`, and that this "retires the
fabricated threshold-13 milestone". Whether the ladder actually unblocks turns
on a detail the prose does not state: a segment's forward-only high-water mark
is read from `journal_effective_events` filtered to `status = 'active'`
(`get_max_journaled_milestone_for_segment`). So the repair works only if the
rederive TOMBSTONES the fabricated event. If it merely leaves it active — as
preserved, un-re-derivable history is left — the journal still reports 13 as the
segment's high-water mark, the genuine 1%, 2% and 3% crossings stay blocked, and
§8's documented repair does not repair anything.

This exercises it end to end on a store seeded with the damaged shape: the
correct derivation over the incident's own observations, plus one fabricated
`percent_milestone` event at threshold 13 on the credited epoch.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

WEEK_START_DATE = "2026-08-29"
ACCOUNT = "acct-a"
RESETS_AT = int(
    dt.datetime(2026, 9, 5, 5, 0, tzinfo=dt.timezone.utc).timestamp())
APPEND_CLOCK = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)

#: The incident's own sequence: a genuine climb, the credit to zero (armed and
#: confirmed by two zeros), then the genuine crossings the fabrication blocked.
OBSERVATIONS = (
    ("2026-09-01T17:33:38Z", 13.0),
    ("2026-09-01T17:59:41Z", 0.0),
    ("2026-09-01T17:59:47Z", 0.0),
    ("2026-09-01T19:00:00Z", 1.0),
    ("2026-09-01T21:00:00Z", 2.0),
    ("2026-09-02T01:00:00Z", 3.0),
)


@pytest.fixture
def mod(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _seed_cache(mod):
    """One priced entry, so the cost contract the family validates is met."""
    path = "/tmp/claude/projects/repo/session.jsonl"
    conn = mod.open_cache_db()
    try:
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 100, 1, 100, "2026-09-01T12:00:00Z", "session-a", "/repo"))
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, 0, "2026-09-01T11:00:00+00:00",
             "claude-3-5-sonnet-20241022", 0, 0, 100, 0, 40, ACCOUNT))
        conn.commit()
    finally:
        conn.close()


def _observations(journal):
    return [
        journal.make_obs(
            at=at, src="record-usage", provider="claude", account=ACCOUNT,
            payload={
                "captured_at": at,
                "source": "statusline",
                "weekly_percent": pct,
                "resets_at": RESETS_AT,
            })
        for at, pct in OBSERVATIONS
    ]


def _append_correct_derivation(mod):
    """Append the incident's raw observations and their correct derivation."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    observations = _observations(journal)
    cache = mod.open_cache_db()
    try:
        plan = mod.plan_claude_usage_rederive(
            observations, cache_conn=cache,
            journal_high_water=("observations-2026-09.jsonl",
                                len(observations)))
    finally:
        cache.close()
    events = []
    for action in plan.actions:
        payload = dict(action.payload or {})
        events.append(journal.make_evt(
            kind=payload.pop("kind"), id=action.event_id, at=action.at,
            payload=payload))
    for record in [*observations, *events]:
        runtime.append_record(record, now_utc=APPEND_CLOCK)
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"))
    return events


def _credited_epoch_milestone(events, threshold):
    """A `percent_milestone` on the CREDITED epoch at `threshold`.

    The correct derivation also emits a threshold-13 milestone on the PRE-credit
    epoch (segment ref `0`), which is genuine history — the meter really did
    reach 13% before the credit. The fabrication under test is a threshold-13
    milestone on the epoch the credit opened, whose ref is the credit's own
    journal identity.
    """
    for record in events:
        event_id = str(record.get("id", ""))
        if not event_id.startswith("pm:"):
            continue
        head, _, tail = event_id.rpartition(":")
        if tail == str(threshold) and ":0" != head[-2:]:
            return record
    return None


def _fabricate_threshold_13(mod, events):
    """Append the milestone the stale replica seeded, in its own shape.

    The damaged store's row is a `percent_milestone` on the credited epoch at
    threshold 13 — a level the meter never crossed inside that epoch. It is
    built from a genuine sibling so every field but the threshold is exactly
    what the emitter would have written.
    """
    import _cctally_journal as runtime
    import _lib_journal as journal

    sibling = _credited_epoch_milestone(events, 1)
    assert sibling is not None, [r.get("id") for r in events]
    fabricated_id = sibling["id"].rsplit(":", 1)[0] + ":13"
    payload = dict(sibling.get("payload") or {})
    payload["percent_threshold"] = 13
    record = journal.make_evt(
        kind="percent_milestone", id=fabricated_id,
        at="2026-09-01T18:00:00Z", payload=payload)
    runtime.append_record(record, now_utc=APPEND_CLOCK)
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"))
    return fabricated_id


def _event_status(mod, event_id):
    conn = mod.open_db()
    try:
        row = conn.execute(
            "SELECT status FROM journal_effective_events WHERE event_id = ?",
            (event_id,)).fetchone()
        return None if row is None else row["status"]
    finally:
        conn.close()


def _segment_high_water(mod):
    conn = mod.open_db()
    try:
        row = conn.execute(
            "SELECT id FROM week_reset_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        reset_event_id = 0 if row is None else int(row["id"])
        return mod.get_max_journaled_milestone_for_segment(
            conn, WEEK_START_DATE, reset_event_id=reset_event_id,
            account_key=ACCOUNT)
    finally:
        conn.close()


def test_a_rederive_tombstones_the_fabricated_milestone_and_unblocks_the_ladder(
        mod):
    _seed_cache(mod)
    events = _append_correct_derivation(mod)
    fabricated_id = _fabricate_threshold_13(mod, events)

    # The damaged state: the journal reports 13 as the segment's high-water
    # mark, which is what blocks every genuine crossing beneath it.
    assert _event_status(mod, fabricated_id) == "active"
    assert _segment_high_water(mod) == 13

    assert mod.cmd_db_rederive(argparse.Namespace(
        family="claude-usage", yes=True, json=True)) == 0

    status = _event_status(mod, fabricated_id)
    assert status == "tombstone", (
        "db rederive left the fabricated milestone ACTIVE, so §8's documented "
        f"repair does not unblock the ladder: status={status!r}")
    high_water = _segment_high_water(mod)
    assert high_water != 13, (
        "the segment still reports the fabricated threshold as its high-water "
        f"mark after the repair: {high_water!r}")


def test_the_repair_leaves_the_genuine_crossings_recorded(mod):
    """The repair must not retire the crossings the meter really made."""
    _seed_cache(mod)
    events = _append_correct_derivation(mod)
    _fabricate_threshold_13(mod, events)
    assert mod.cmd_db_rederive(argparse.Namespace(
        family="claude-usage", yes=True, json=True)) == 0
    conn = mod.open_db()
    try:
        row = conn.execute(
            "SELECT id FROM week_reset_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        credited_segment = 0 if row is None else int(row["id"])
        by_segment: dict = {}
        for r in conn.execute(
                "SELECT percent_threshold, reset_event_id "
                "  FROM percent_milestones WHERE week_start_date = ?",
                (WEEK_START_DATE,)):
            by_segment.setdefault(
                int(r["reset_event_id"] or 0), set()).add(
                    int(r["percent_threshold"]))
    finally:
        conn.close()
    # The genuine climb to 13% happened BEFORE the credit, so it stays recorded
    # on the pre-credit segment. Retiring it would be its own fabrication.
    assert 13 in by_segment.get(0, set()), by_segment
    # The credited segment keeps exactly the crossings the meter made inside it.
    assert by_segment.get(credited_segment) == {1, 2, 3}, by_segment
