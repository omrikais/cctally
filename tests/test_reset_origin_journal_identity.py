"""#750 S3 Task A2 — the dual-shaped reset-event identity in the journal.

Spec §1.1. Moving identity onto the originating observation in SQL alone is
not enough: `_HARVEST_SPECS` derives the `week_reset_events` evt id from the
`(account_key, old_week_end_at, new_week_end_at)` tuple, `_build_harvest_evt`
uses those same fields for the id AND for the `ctx.suppression_map` lookup,
and `_emit_harvest_row` converges a same-id row away. Changing only the SQL
constraint would leave the journal collapsing two distinct-origin events into
one.

Identity is therefore dual-shaped and produced by ONE shared helper:

* non-null origin -> `(account_key, "origin", origin_observation_id)`,
  spelled `wr:<account>:origin:o:<hex>`;
* null origin -> `(account_key, old_week_end_at, new_week_end_at)`, keeping
  the existing `wr:<account>:<old>:<new>` spelling so retained legacy events
  and the milestone references that name them keep resolving.

Every assertion below goes through real harvest and a real rebuild rather
than a direct SQL insert, because the defect this task fixes lives in the
journal layer and a direct insert would never touch it.
"""
from __future__ import annotations

import datetime as dt

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)

OLD_END = "2026-09-05T10:00:00+00:00"
NEW_END = "2026-09-12T15:00:00+00:00"
OTHER_OLD_END = "2026-08-29T10:00:00+00:00"
ORIGIN_A = "o:" + "a" * 16
ORIGIN_B = "o:" + "b" * 16


def _siblings():
    import _cctally_journal as jr
    import _lib_journal as J
    return jr, J


def _usage_obs(J, pct, at="2026-09-05T09:00:00Z"):
    return J.make_obs(
        at=at, src="record-usage", provider="claude",
        payload={"weekly_percent": pct, "source": "statusline"},
    )


def _insert_reset(conn, at, *, old, new, origin):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " origin_observation_id) VALUES (?,?,?,?,?,?,?)",
        (at, old, new, old, 46.0, "unattributed", origin),
    )


def _reset_hook(rows):
    """Pipeline hook inserting one un-stamped `week_reset_events` row set.

    It fires ONCE. A second insert of the same rows would collide with the
    partial unique index, which is correct behaviour but not what these tests
    are about.
    """
    fired = []

    def hook(ctx, rec):
        if rec.get("t") != "obs" or fired:
            return
        fired.append(True)
        for old, new, origin in rows:
            _insert_reset(ctx.conn, rec["at"], old=old, new=new, origin=origin)
    return hook


def _events(ns):
    conn = ns["open_db"]()
    try:
        return [
            (str(r["journal_id"]), r["old_week_end_at"], r["new_week_end_at"],
             r["origin_observation_id"])
            for r in conn.execute(
                "SELECT journal_id, old_week_end_at, new_week_end_at, "
                "origin_observation_id FROM week_reset_events ORDER BY id")
        ]
    finally:
        conn.close()


def _rebuild(jr):
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_a2_two_distinct_origins_sharing_one_tuple_survive_harvest(
        ns, monkeypatch):
    """The load-bearing case. Two events for one account share
    `(old_week_end_at, new_week_end_at)` and differ only in the observation
    that caused them. Before the dual shape they harvested to ONE id and
    `_emit_harvest_row` converged the second away."""
    jr, J = _siblings()
    monkeypatch.setattr(
        jr, "PIPELINE",
        list(jr.PIPELINE) + [_reset_hook([
            (OLD_END, NEW_END, ORIGIN_A),
            (OLD_END, NEW_END, ORIGIN_B),
        ])])

    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    rows = _events(ns)
    assert len(rows) == 2, "the second distinct-origin event was collapsed"
    ids = [r[0] for r in rows]
    assert ids == [
        f"wr:unattributed:origin:{ORIGIN_A}",
        f"wr:unattributed:origin:{ORIGIN_B}",
    ], ids


def test_a2_two_distinct_origins_survive_a_rebuild_from_the_journal(
        ns, monkeypatch):
    """Asserted through rebuild rather than through the live index, because a
    rebuild folds the journal alone: if the two events shared an id the fold
    would keep one row."""
    jr, J = _siblings()
    monkeypatch.setattr(
        jr, "PIPELINE",
        list(jr.PIPELINE) + [_reset_hook([
            (OLD_END, NEW_END, ORIGIN_A),
            (OLD_END, NEW_END, ORIGIN_B),
        ])])

    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    _rebuild(jr)

    rows = _events(ns)
    assert len(rows) == 2, "the rebuild folded two distinct events into one"
    assert {r[3] for r in rows} == {ORIGIN_A, ORIGIN_B}
    assert len({r[0] for r in rows}) == 2


def test_a2_legacy_null_origin_rows_keep_their_tuple_spelling(
        ns, monkeypatch):
    """Retained events must not be re-identified. A legacy row has no origin,
    so its id keeps the exact `wr:<account>:<old>:<new>` spelling that every
    already-journaled milestone reference names."""
    jr, J = _siblings()
    monkeypatch.setattr(
        jr, "PIPELINE",
        list(jr.PIPELINE) + [_reset_hook([
            (OLD_END, NEW_END, None),
            (OTHER_OLD_END, NEW_END, None),
        ])])

    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    ids = sorted(r[0] for r in _events(ns))
    assert ids == sorted([
        f"wr:unattributed:{OLD_END}:{NEW_END}",
        f"wr:unattributed:{OTHER_OLD_END}:{NEW_END}",
    ]), ids


def test_a2_a_retained_event_replays_its_recorded_id_verbatim(
        ns, monkeypatch):
    """Replay inserts the recorded id verbatim and harvest scans only rows
    whose `journal_id IS NULL`, so a legacy event is never re-harvested under
    a new spelling. Asserted explicitly rather than left to the suite."""
    jr, J = _siblings()
    monkeypatch.setattr(
        jr, "PIPELINE",
        list(jr.PIPELINE) + [_reset_hook([(OLD_END, NEW_END, None)])])

    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    before = _events(ns)
    assert len(before) == 1

    # A second cycle over a new observation must not re-harvest the stamped
    # row, and a rebuild must reinstate the SAME id from the retained line.
    jr.append_record(
        _usage_obs(J, 58.0, at="2026-09-05T09:05:00Z"), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    _rebuild(jr)

    after = _events(ns)
    assert [r[0] for r in after][:1] == [before[0][0]], (
        "the retained event's recorded id did not replay verbatim")


def test_a2_the_suppression_map_key_follows_the_same_shape(ns, monkeypatch):
    """`_build_harvest_evt` looks the suppression list up by the row's id
    parts. If the map key and the id came from different helpers the
    destructive effect would silently stop riding the event."""
    jr, J = _siblings()
    ns  # the fixture redirects paths; the namespace itself is unused here

    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        ctx.conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, weekly_percent,"
            " payload_json, journal_id) VALUES (?,?,?,?,?,?)",
            (rec["at"], "2026-09-05", "2026-09-12", 46.0, "{}", "sa:doomed"))
        _insert_reset(
            ctx.conn, rec["at"], old=OLD_END, new=NEW_END, origin=ORIGIN_A)
        ctx.suppression_map[
            ("unattributed", "origin", ORIGIN_A)] = ["sa:doomed"]

    monkeypatch.setattr(jr, "PIPELINE", list(jr.PIPELINE) + [hook])
    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    seg = jr.list_segments()[-1]
    raw = (_cctally_core.JOURNAL_DIR / seg).read_bytes()
    wr_lines = [ln for ln in raw.split(b"\n")
                if b'"wr:unattributed:origin:' in ln]
    assert wr_lines, "no origin-shaped wr evt was journaled"
    assert b'"suppression"' in wr_lines[-1], (
        "the suppression list did not ride the origin-shaped event, so the "
        "map key and the id parts came from different helpers")


def test_a2_one_helper_produces_both_id_parts_shapes():
    """The structural half of the contract: one function, consulted by both
    the harvest id and the suppression-map key."""
    jr, _ = _siblings()
    parts = jr.week_reset_identity_parts
    assert parts("acct", OLD_END, NEW_END, ORIGIN_A) == (
        "acct", "origin", ORIGIN_A)
    assert parts("acct", OLD_END, NEW_END, None) == (
        "acct", OLD_END, NEW_END)
    # An empty string is not an identity either — it is what a NULL column
    # round-tripped through a payload can degrade into.
    assert parts("acct", OLD_END, NEW_END, "") == ("acct", OLD_END, NEW_END)
