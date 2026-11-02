"""Regression coverage for rebuild-only five-hour block projection."""

from __future__ import annotations

import json
import sys

import pytest

import test_account_bound_replay_m19 as m19
from conftest import load_script, redirect_paths


def test_rebuild_backfill_materializes_only_latest_window_per_account(
    monkeypatch, tmp_path,
):
    """A tombstoned historical block must not return as an unstamped row.

    Rebuild replays closed ``five_hour_block_close`` events first, then asks
    the backfill for the one trailing open projection per account. If the
    backfill selects every missing window, a deliberately absent historical
    block is recreated with ``journal_id IS NULL`` and the next harvest tries
    to append its obsolete revision-0 event over the effective correction.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(
        ns, "get_claude_session_entries", lambda *_args, **_kwargs: [],
    )

    conn = ns["open_db"]()
    try:
        rows = [
            (
                "2026-04-13T11:32:21Z",
                "2026-04-13",
                "2026-04-20",
                "2026-04-13T00:00:00Z",
                "2026-04-20T00:00:00Z",
                16.0,
                5.0,
                "2026-04-13T16:00:00Z",
                1_776_096_000,
                "acct-a",
            ),
            (
                "2026-07-29T08:00:00Z",
                "2026-07-29",
                "2026-08-05",
                "2026-07-29T00:00:00Z",
                "2026-08-05T00:00:00Z",
                1.0,
                2.0,
                "2026-07-29T12:00:00Z",
                1_785_307_200,
                "acct-a",
            ),
        ]
        conn.executemany(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            " account_key, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', '{}')",
            rows,
        )
        conn.commit()

        inserted = ns["_backfill_five_hour_blocks"](
            conn, only_missing=True,
        )
        materialized = conn.execute(
            "SELECT five_hour_window_key, is_closed, journal_id "
            "FROM five_hour_blocks ORDER BY five_hour_window_key"
        ).fetchall()

        assert inserted == 1
        assert [tuple(row) for row in materialized] == [
            (1_785_307_200, 0, None),
        ]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# #956 class 2 (spec 4.6, test 6.7): every block creation retains an opening
# fact, `five_hour_block_open` (`fhbo`, order 35), whose fold materializes
# the open block before the five-hour milestones (order 60) fold, so an
# apply-only replay (rebuild, epoch transition, corruption heal, a crashed
# cycle's step 4a) reproduces a block no surviving snapshot names and its
# milestones join their real `block_id`. The schedules drive the real
# recorders through `tests/test_account_bound_replay_m19.py`'s harness.
# --------------------------------------------------------------------------

_NOW = m19.NOW
_CLOSE = m19._CLOSE_RESET
_SUCCESSOR = m19._SUCCESSOR_RESET
_APPLY_ONLY = ("rebuild", "epoch-transition", "corruption-heal")


def _key(reset):
    """The canonical window key of a reset (floored to ten minutes)."""
    return reset - reset % 600


def _start(ns, monkeypatch, tmp_path, *, historical=False, account="B",
           offsets=(-10,)):
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    app = sys.modules["cctally"]
    monkeypatch.setattr(app, "_nudge_dashboard_repaint", lambda: None)
    m19._tick(monkeypatch, 0)
    app.open_db().close()
    if historical:
        m19._record_historical_policy(monkeypatch)
    key = (m19._unattributed() if account is None
           else m19._identity(account))
    m19._retain_session_input(
        app, "unattributed" if account is None else account, key, offsets)
    return app, m19._Recorder(app, monkeypatch, owe=False), key


def _rows(app, sql, params=()):
    conn = app.open_db()
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _block(app, account_key, reset):
    rows = _rows(app, "SELECT * FROM five_hour_blocks WHERE account_key = ? "
                      "AND five_hour_window_key = ?",
                 (account_key, _key(reset)))
    return rows[0] if rows else None


def _assert_joined(app, account_key, reset, label):
    """The window's block exists and every five-hour milestone of the window
    joins it by its real `block_id` (never the `0` no-parent sentinel)."""
    block = _block(app, account_key, reset)
    assert block is not None, (label, "the window's block was lost")
    milestones = _rows(
        app, "SELECT percent_threshold, block_id FROM five_hour_milestones "
             "WHERE account_key = ? AND five_hour_window_key = ?",
        (account_key, _key(reset)))
    assert milestones, (label, "non-vacuity: the window crossed a threshold")
    assert {m["block_id"] for m in milestones} == {block["id"]}, (
        label, block["id"], milestones)
    return block


def _heal_cli(r):
    r.cli("W0", 0, 40, 30, _CLOSE)
    r.cli("W1", 2, 40, 30, _SUCCESSOR)


def _heal_statusline(r):
    r.bound("W0", 0, 40, 30, _CLOSE)
    r.bound("W1", 2, 40, 30, _SUCCESSOR)


def _heal_publication(r):
    r.publication(None, 0, 40, 30, _CLOSE)
    r.publication(None, 2, 40, 30, _SUCCESSOR)


def _heal_held_without_basis(r):
    # Current fold policy. The successor reading is weekly-held (its raw 39
    # is below the stored 40) and the fold finds no accepted weekly basis, so
    # it writes no snapshot (`weekly_held and basis is None` -> SKIP) and the
    # rollover heal opens the block. The fold computes the clamp maximum and
    # the basis from the same unheld rows of the week, so on this tree the
    # branch is reached only when the basis lookup itself returns None; the
    # test supplies that, live only. A rebuild and an epoch transition never
    # fold, but the corruption heal's derivation completion re-derives the
    # skipped reading with the real lookup (and so writes a held snapshot), so
    # this shape checks the block and its joins, not whole-state equality.
    import _cctally_journal as jr
    r.cli("W0", 0, 40, 30, _CLOSE)
    with r.monkeypatch.context() as patch:
        patch.setattr(jr, "_latest_weekly_basis", lambda *a, **k: None)
        r.cli("W1", 2, 39, 30, _SUCCESSOR)


_HEAL_SHAPES = {
    # name: (stages, historical fold policy, active identity, whole-state
    # equality with live)
    "manual-cli": (_heal_cli, True, "B", True),
    "statusline": (_heal_statusline, True, "B", True),
    "statusline-unattributed": (_heal_publication, True, None, True),
    "manual-cli-unattributed": (_heal_cli, True, None, True),
    "held-without-basis": (_heal_held_without_basis, False, "B", False),
}


@pytest.mark.parametrize("shape", sorted(_HEAL_SHAPES))
def test_956_a_heal_opened_block_survives_every_apply_only_replay(
        monkeypatch, tmp_path, shape):
    """Detects an apply-only replay losing a block that a snapshotless
    rollover heal opened (F5): the heal wrote no snapshot of its window, so
    the rebuild's open-block projection (one latest-captured window per
    account, from snapshots) never re-materialized it and its five-hour
    milestones resolved to `block_id = 0`. Four writer shapes under the
    historical fold policy and the current policy's held-without-basis
    skip; after a rebuild, an epoch transition and a corruption heal the
    block exists, equals live, and its milestones join it."""
    stages, historical, account, whole_state = _HEAL_SHAPES[shape]
    ns = load_script()
    app, recorder, key = _start(ns, monkeypatch, tmp_path,
                                historical=historical, account=account)
    stages(recorder)
    assert not _rows(app, "SELECT 1 FROM weekly_usage_snapshots "
                          "WHERE five_hour_window_key = ?",
                     (_key(_SUCCESSOR),)), (
        "non-vacuity: the heal wrote no snapshot of its window")
    live_block = _assert_joined(app, key, _SUCCESSOR, "live")
    live = m19._logical_state(app)
    for replay in _APPLY_ONLY:
        m19._drive_replay(app, monkeypatch, replay)
        block = _assert_joined(app, key, _SUCCESSOR, replay)
        assert block["is_closed"] == 0 and block["journal_id"] is None, block
        for column in ("first_observed_at_utc", "block_start_at",
                       "five_hour_resets_at", "seven_day_pct_at_block_start",
                       "created_at_utc", "last_observed_at_utc",
                       "final_five_hour_percent",
                       "seven_day_pct_at_block_end"):
            assert block[column] == live_block[column], (replay, column)
        if whole_state:
            m19._assert_same(live, m19._logical_state(app))


def _rebuild(app, monkeypatch):
    m19._drive_replay(app, monkeypatch, "rebuild")


def _evts(kind=None):
    return [r for r in m19._journal_records()
            if r.get("t") == "evt"
            and (kind is None or (r.get("payload") or {}).get("kind") == kind)]


def _opening_id(account_key, reset):
    [record] = [r for r in _evts("five_hour_block_open")
                if r["payload"]["account_key"] == account_key
                and r["payload"]["five_hour_window_key"] == _key(reset)]
    return record["id"]


def _correct(app, actions):
    """Append one completed correction batch (``actions``: ``(id, payload)``,
    a None payload tombstoning the id) above each id's effective revision."""
    import _cctally_journal as jr
    import _lib_journal as J
    selection = J.resolve_effective_events(m19._journal_records())
    at = m19._iso(_NOW + 30)
    rows = []
    for event_id, payload in actions:
        rows.append({
            "action": "tombstone" if payload is None else "replace",
            "id": event_id, "rev": selection.by_id[event_id].rev + 1,
            "at": at, "payload": payload})
    jr.append_records(J.make_correction_batch(
        batch_id=f"test-956:{rows[0]['id']}:{rows[0]['rev']}",
        family="test-956", at=at, actions=rows))


def _without_openings(patch):
    """Record as a binary before opening facts did (legacy, residual R4):
    block creation writes no `five_hour_block_openings` row. Patched in the
    globals of the block writer the pipeline actually calls (the module
    `load_script` bound), not a possibly different import of it."""
    writer = sys.modules["cctally"].maybe_update_five_hour_block
    patch.setitem(writer.__globals__, "_insert_five_hour_block_opening",
                  lambda *args, **kwargs: None)


def test_956_a_crashed_creating_cycle_replays_its_opening_before_its_milestones(
        monkeypatch, tmp_path):
    """Detects a crash replay that folds a new block's five-hour milestones
    before any block row exists (finding P1-2): the creating cycle appended
    its `five_hour_block_open` and milestone evts and crashed before commit;
    the next ordinary ingest's step 4a replays them (the opening at order 35
    materializes the block before the milestones at 60) and step 4b re-runs
    the observation. `block_id` is real after the recovery and after a
    rebuild."""
    ns = load_script()
    import _cctally_journal as jr  # after load_script: the module it bound
    app, r, key = _start(ns, monkeypatch, tmp_path, historical=True)
    r.cli("W0", 0, 40, 30, _CLOSE)

    crashed = []

    def crash(conn, segment, offset):
        crashed.append((segment, offset))
        raise RuntimeError("simulated crash before commit")

    # The crash escapes the recorder; its observation stays appended for the
    # next ingest.
    with monkeypatch.context() as patch:
        patch.setattr(jr, "_write_cursor", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            r.cli("W1", 2, 40, 30, _SUCCESSOR)
    assert crashed, "non-vacuity: the creating cycle reached its commit"
    assert _block(app, key, _SUCCESSOR) is None, "the cycle rolled back"
    assert _opening_id(key, _SUCCESSOR), "non-vacuity: the opening was appended"
    assert any(e["payload"]["five_hour_window_key"] == _key(_SUCCESSOR)
               for e in _evts("five_hour_milestone")), (
        "non-vacuity: the window's milestone was appended")
    jr.run_stats_ingest(mode="authoritative")
    _assert_joined(app, key, _SUCCESSOR, "recovered")
    recovered = m19._logical_state(app)
    _rebuild(app, monkeypatch)
    _assert_joined(app, key, _SUCCESSOR, "rebuilt")
    m19._assert_same(recovered, m19._logical_state(app))


def test_956_an_opening_followed_by_snapshots_keeps_the_seed_start_columns(
        monkeypatch, tmp_path):
    """Detects a rebuild taking a seeded block's start columns from its
    window's first snapshot (finding P2-8): a heal opened the block at t=2
    with no snapshot, and later readings of the window wrote snapshots from
    t=4. Its start columns are the opening's (first observed at t=2, weekly
    40), never the first snapshot's (t=4, weekly 41), on live and replay."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path, historical=True)
    _heal_cli(r)
    r.cli("W1b", 4, 41, 35, _SUCCESSOR)
    r.cli("W1c", 6, 41, 37, _SUCCESSOR)
    live = m19._logical_state(app)
    block = _block(app, key, _SUCCESSOR)
    assert m19._epoch(block["first_observed_at_utc"]) == _NOW + 2, block
    assert block["seven_day_pct_at_block_start"] == 40.0, block
    assert m19._epoch(block["last_observed_at_utc"]) == _NOW + 6, block
    for replay in _APPLY_ONLY:
        m19._drive_replay(app, monkeypatch, replay)
        m19._assert_same(live, m19._logical_state(app))


def test_956_a_closed_fact_wins_over_its_opening(monkeypatch, tmp_path):
    """Detects an opening fact overriding the block's later close: the
    heal-opened block is closed by a successor window's reading, and after
    a rebuild it is the `five_hour_block_close` fact (closed, its journal
    id), not the opening's open projection, while the opening fact itself
    is retained."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path, historical=True)
    _heal_cli(r)
    # After the healed window's reset, a reading of the next window.
    r.cli("W2", _SUCCESSOR - _NOW + 60, 41, 5, _SUCCESSOR + 5 * 3600)
    live = m19._logical_state(app)
    closed = _block(app, key, _SUCCESSOR)
    assert closed["is_closed"] == 1 and closed["journal_id"], closed
    _rebuild(app, monkeypatch)
    rebuilt = _block(app, key, _SUCCESSOR)
    assert (rebuilt["is_closed"], rebuilt["journal_id"]) == (
        1, closed["journal_id"]), rebuilt
    assert _rows(app, "SELECT 1 FROM five_hour_block_openings "
                      "WHERE account_key = ? AND five_hour_window_key = ?",
                 (key, _key(_SUCCESSOR))), "the opening fact is retained"
    m19._assert_same(live, m19._logical_state(app))


def test_956_a_legacy_open_block_is_reproduced_until_and_after_its_close(
        monkeypatch, tmp_path):
    """Detects the upgrade losing or changing a block a previous binary
    created without an opening fact: the epoch rebuild reproduces it by the
    legacy candidate rule (each account's latest-captured window) while it
    stays open, and once a newer window closes it, by its close fact; the
    newer window's block comes from its opening."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path)
    with monkeypatch.context() as patch:
        _without_openings(patch)
        r.cli("W0", 0, 40, 30, _CLOSE)
        r.cli("W0b", 2, 40, 34, _CLOSE)
    assert not _evts("five_hour_block_open"), "non-vacuity: a legacy journal"
    legacy = m19._logical_state(app)
    m19._drive_replay(app, monkeypatch, "epoch-transition")
    m19._assert_same(legacy, m19._logical_state(app))
    r.cli("W1", 4, 41, 5, _SUCCESSOR)
    assert _block(app, key, _CLOSE)["is_closed"] == 1
    live = m19._logical_state(app)
    for replay in _APPLY_ONLY:
        m19._drive_replay(app, monkeypatch, replay)
        m19._assert_same(live, m19._logical_state(app))


def test_956_an_empty_selection_projects_the_opening_reading(
        monkeypatch, tmp_path):
    """Detects an open block whose every five-hour reading a correction
    removed being lost or keeping a removed reading's end (spec 4.5.4): its
    end is the opening fact's opening reading (last observed = first
    observed, final percent = opening percent, weekly end = weekly start) on
    every apply-only replay. (`record-credit --force` cannot empty a seeded
    window on this tree: a credit's weekly cleanup keeps any unheld row that
    carries a five-hour reading, weekly-held, and its synthetic copies that
    reading, so the correction is the reachable way.)"""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path)
    r.cli("W0", 0, 40, 30, _CLOSE)
    r.cli("W0b", 2, 40, 34, _CLOSE)
    accepted = {e["id"] for e in _evts("snapshot_accept")}
    assert len(accepted) == 2, accepted
    # Every decision that names the removed readings goes with them.
    named = [e["id"] for e in _evts()
             if e["id"] in accepted
             or (e.get("payload") or {}).get("usage_snapshot_ref") in accepted]
    _correct(app, [(event_id, None) for event_id in named])
    for replay in ("rebuild", "epoch-transition"):
        m19._drive_replay(app, monkeypatch, replay)
        assert not _rows(app, "SELECT 1 FROM weekly_usage_snapshots "
                              "WHERE five_hour_window_key = ?",
                         (_key(_CLOSE),)), "non-vacuity: no reading survives"
        block = _block(app, key, _CLOSE)
        assert block is not None, (replay, "the seeded block was lost")
        assert block["is_closed"] == 0, block
        assert block["last_observed_at_utc"] == block["first_observed_at_utc"]
        assert m19._epoch(block["first_observed_at_utc"]) == _NOW, block
        assert block["final_five_hour_percent"] == 30.0, block
        assert block["seven_day_pct_at_block_end"] == 40.0, block
        assert block["total_input_tokens"] == m19._ENTRY_INPUT, block
    opening = _evts("five_hour_block_open")
    # The rederive leg. A rederive re-derives every derivable event from the
    # retained observations, and a correction of derivable events is not
    # retained truth, so it restores both readings: on rederive the window is
    # NOT an empty selection (no claude-usage decision removes a five-hour
    # reading). What must hold is that its apply, and a rebuild of what it
    # journaled, project the restored latest reading (W0b) instead of keeping
    # the opening reading the empty selection left, and keep the opening fact
    # as the block's first writer.
    for replay in ("rederive", "rebuild"):
        m19._drive_replay(app, monkeypatch, replay)
        restored = _rows(app, "SELECT five_hour_percent "
                              "FROM weekly_usage_snapshots "
                              "WHERE five_hour_window_key = ? "
                              "ORDER BY captured_at_utc", (_key(_CLOSE),))
        assert [r["five_hour_percent"] for r in restored] == [30.0, 34.0], (
            replay, restored)
        block = _block(app, key, _CLOSE)
        assert block is not None and block["is_closed"] == 0, (replay, block)
        assert m19._epoch(block["first_observed_at_utc"]) == _NOW, block
        assert m19._epoch(block["last_observed_at_utc"]) == _NOW + 2, block
        assert block["final_five_hour_percent"] == 34.0, block
        assert block["seven_day_pct_at_block_end"] == 40.0, block
        assert _evts("five_hour_block_open") == opening, replay


def test_956_r4_a_second_legacy_open_block_is_lost_until_live_recreates_it(
        monkeypatch, tmp_path):
    """Pins residual R4 and detects live failing to re-link it: two legacy
    (unseeded) blocks are open at the upgrade — a newer window recorded
    first, then an earlier capture of the older, overlapping window. The
    upgrade rebuild keeps only each account's latest-captured window, so
    the older block is lost and its milestones resolve to `block_id = 0`. A
    later reading of that window recreates it live (now with an opening
    fact) and re-links its milestones, and live then equals a rebuild with
    a real `block_id`."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path, offsets=(-10, 800))
    overlap = m19._OVERLAP_RESET
    with monkeypatch.context() as patch:
        _without_openings(patch)
        r.bound("A", 0, 40, 30, _CLOSE, during=lambda: r.bound(
            "B", 1000, 40, 50, overlap))
    assert {b["is_closed"] for b in (_block(app, key, _CLOSE),
                                     _block(app, key, overlap))} == {0}
    m19._drive_replay(app, monkeypatch, "epoch-transition")
    assert _block(app, key, _CLOSE) is None, "R4: the older block is lost"
    assert _block(app, key, overlap) is not None
    lost = _rows(app, "SELECT block_id FROM five_hour_milestones "
                      "WHERE five_hour_window_key = ?", (_key(_CLOSE),))
    assert lost and {m["block_id"] for m in lost} == {0}, lost
    r.bound("A2", 5, 40, 32, _CLOSE)
    _assert_joined(app, key, _CLOSE, "recreated live")
    live = m19._logical_state(app)
    _rebuild(app, monkeypatch)
    _assert_joined(app, key, _CLOSE, "rebuilt")
    m19._assert_same(live, m19._logical_state(app))


def test_956_a_tombstoned_opening_is_never_a_legacy_candidate(
        monkeypatch, tmp_path):
    """Detects a rebuild resurrecting a block whose opening fact a
    correction tombstoned: its window still has qualifying snapshots (the
    latest-captured of the account), so without the retired-openings set
    the legacy candidate rule would re-materialize it."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path)
    r.cli("W0", 0, 40, 30, _CLOSE)
    _correct(app, [(_opening_id(key, _CLOSE), None)])
    for replay in ("rebuild", "epoch-transition"):
        m19._drive_replay(app, monkeypatch, replay)
        assert _rows(app, "SELECT 1 FROM weekly_usage_snapshots "
                          "WHERE five_hour_window_key = ?",
                     (_key(_CLOSE),)), "non-vacuity: snapshots survive"
        assert _block(app, key, _CLOSE) is None, replay


def _utc(epoch):
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def _milestone(app, account_key, reset, threshold):
    [row] = _rows(app, "SELECT * FROM five_hour_milestones "
                       "WHERE account_key = ? AND five_hour_window_key = ? "
                       "AND percent_threshold = ?",
                  (account_key, _key(reset), threshold))
    return row


def test_956_a_completion_converges_the_open_block_to_a_superseded_opening(
        monkeypatch, tmp_path):
    """Detects a derivation completion deriving an owed observation against
    an open block whose start columns a retained opening supersession has
    already moved (spec 4.6.3, coupled convergence): the opening's
    replacement shifts the window's reset (and so its start) by 30 s inside
    the same canonical key, which takes the entry at t=-17390 out of the
    block. The owed reading at t=4 crosses 31..34%, and its milestone money
    is computed over the block as the scratch holds it, so it must already
    follow the new seed (one entry, never the old start's two)."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path, offsets=(-10, -17390))
    r.cli("W0", 0, 40, 30, _CLOSE)
    [opening] = _evts("five_hour_block_open")
    shifted = _CLOSE + 30
    superseded = dict(opening["payload"],
                      five_hour_resets_at=_utc(shifted),
                      block_start_at=_utc(shifted - 5 * 3600))
    _correct(app, [(opening["id"], superseded)])
    r.cli("W0b", 4, 40, 34, _CLOSE, batched=True)
    _rebuild(app, monkeypatch)
    assert any(e["id"] == "sa:" + r.obs["W0b"]["id"]
               for e in _evts("snapshot_accept")), (
        "non-vacuity: the completion derived the owed reading")
    block = _block(app, key, _CLOSE)
    assert (block["five_hour_resets_at"], block["block_start_at"]) == (
        superseded["five_hour_resets_at"], superseded["block_start_at"]), block
    assert block["total_input_tokens"] == m19._ENTRY_INPUT, block
    owed = _milestone(app, key, _CLOSE, 34)
    assert owed["block_input_tokens"] == m19._ENTRY_INPUT, owed
    assert _milestone(app, key, _CLOSE, 30)["block_input_tokens"] == (
        2 * m19._ENTRY_INPUT), "the live crossing keeps its own money"


def test_956_a_completion_removes_a_tombstoned_opening_with_its_open_block(
        monkeypatch, tmp_path):
    """Detects a derivation completion keeping an open block whose opening a
    retained tombstone removed (spec 4.6.3, coupled convergence): two blocks
    are open (a newer window recorded first, then the older, overlapping
    one, which owns the overlap entry at t=-10). A correction tombstones the
    older window's opening with its five-hour milestones; the owed reading of
    the newer window then crosses 51-52%. The older block and its children
    are removed, no milestone of its window remains, and the newer block —
    refreshed — owns the overlap entry, in its totals and in the owed
    crossing's money."""
    ns = load_script()
    app, r, key = _start(ns, monkeypatch, tmp_path, offsets=(-10, 800))
    overlap = m19._OVERLAP_RESET
    r.bound("A", 0, 40, 30, _CLOSE, during=lambda: r.bound(
        "B", 1000, 40, 50, overlap))
    assert _block(app, key, _CLOSE)["total_input_tokens"] == m19._ENTRY_INPUT
    removed = [_opening_id(key, _CLOSE)] + [
        e["id"] for e in _evts("five_hour_milestone")
        if e["payload"]["five_hour_window_key"] == _key(_CLOSE)]
    assert len(removed) > 1, "non-vacuity: the window has milestones"
    _correct(app, [(event_id, None) for event_id in removed])
    r.bound("B2", 1100, 40, 52, overlap, batched=True)
    _rebuild(app, monkeypatch)
    assert _block(app, key, _CLOSE) is None
    for table in ("five_hour_block_models", "five_hour_block_projects",
                  "five_hour_milestones"):
        assert not _rows(app, f"SELECT 1 FROM {table} WHERE account_key = ? "
                              "AND five_hour_window_key = ?",
                         (key, _key(_CLOSE))), table
    newer = _block(app, key, overlap)
    assert newer["total_input_tokens"] == 2 * m19._ENTRY_INPUT, newer
    owed = _milestone(app, key, overlap, 52)
    assert owed["block_input_tokens"] == 2 * m19._ENTRY_INPUT, owed
