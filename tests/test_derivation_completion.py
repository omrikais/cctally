"""#825/#832 fix round A — derivation completion and the physical collision
check (review P1-1/P3-12; Codex fix-round-1 Q1/Q2), in process.

A rebuild is apply-only: it replays journaled decisions and never derives a
raw observation. Whatever left an admitted current reading without an admitted
decision — a corrupt index whose heal rebuilt past it, an old process whose
ingest derived it unstamped (its decision is excluded), an interrupted
recovery — the shared completion step replays admitted inputs from a proven
complete predecessor state and journals exactly the decisions that are
missing. Its appends follow the binding policy: no physical event under the id
is an ordinary revision-0 event; an admitted identical decision appends
nothing; an id whose only physical decision is an excluded legacy event is a
completed correction batch above every retained revision — never a second
revision-0 twin, which the previous release would read as a same-revision
conflict. Live Model-A and harvest emission apply the same collision check,
while a metadata-only copy of an ADMITTED retained decision stays suppressed
(F5).

The frozen-release end-to-end trace lives in
`tests/test_derivation_completion_frozen.py` (maintainer-only).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import sys
import types

import pytest

import _statusline_continuation as D
from conftest import load_script, redirect_paths

_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def no_spawn(monkeypatch):
    import _cctally_update

    spawned: list = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached_with_pid",
        lambda command, **_kw: spawned.append(command) or os.getpid())
    return spawned


def _mods():
    import _cctally_core
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal, _cctally_core


def _obs(J, at, pct):
    return J.make_obs(at=at, src="record-usage", provider="claude", payload={
        "weekly_percent": pct, "resets_at": _W1, "source": "statusline",
        "captured_at": at})


def _legacy(record):
    """The previous release's shape of ``record`` (no writer stamp)."""
    import _lib_journal as J
    return J.unstamped_record(record)


def _legacy_snapshot_accept(J, reading):
    """The unstamped decision an old process's ingest journals for a current
    reading (its content is never accounting input to the current binary)."""
    return _legacy(J.make_evt(
        kind="snapshot_accept", id="sa:" + reading["id"], at=reading["at"],
        payload={
            "captured_at_utc": reading["at"],
            "week_start_date": "2026-01-01",
            "week_end_date": "2026-01-08",
            "weekly_percent": reading["payload"]["weekly_percent"],
            "source": "statusline",
            "payload_json": "{}",
        }))


def _raw_append_tail(records):
    """Append lines exactly as given to the canonical last segment, bypassing
    the stamping append APIs — what a previous-release writer does."""
    jr, J, core = _mods()
    path = core.JOURNAL_DIR / jr.list_segments()[-1]
    with open(path, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _records():
    jr, J, core = _mods()
    out = []
    for name in jr.list_segments():
        data = (core.JOURNAL_DIR / name).read_bytes()
        out.extend(J.decode_line(line) for line in data.split(b"\n") if line)
    return out


def _journal_bytes():
    jr, _J, core = _mods()
    return b"".join((core.JOURNAL_DIR / name).read_bytes()
                    for name in jr.list_segments())


def _percents():
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"))
    finally:
        conn.close()


def _twins(event_id):
    return [(r["t"], r.get("rev", 0), "wpg" in r)
            for r in _records() if r.get("id") == event_id]


def _rebuild(ns):
    return ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True))


def _seed(jr, J):
    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 7.0))
    jr.run_stats_ingest(mode="authoritative")


def test_a_reading_deferred_by_a_corrupt_index_materializes_after_the_heal(
        ns, no_spawn):
    """The corrupt-index heal is closed by the same completion: a recorder
    appends its reading before its ingest opens the index, the open finds the
    index corrupt and defers the heal, and the heal worker's rebuild must not
    consume the reading without deriving it."""
    jr, J, core = _mods()
    import _cctally_db
    import _cctally_store as st
    _seed(jr, J)
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)

    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    with pytest.raises(_cctally_db.StatsHealDeferred):
        core.open_db()
    assert st.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0

    assert _percents() == [7.0, 9.0]
    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a second completion appended lines"
    assert _percents() == [7.0, 9.0]


def test_interrupted_completion_retries_to_one_completed_batch(
        ns, monkeypatch, capsys):
    """A crash between the correction batch's begin and commit leaves an
    inert batch; the retry completes it, and a further retry appends
    nothing."""
    jr, J, core = _mods()
    _seed(jr, J)
    reading = _obs(J, "2026-01-04T10:00:00Z", 9.0)
    jr.append_record(reading)
    _raw_append_tail([_legacy_snapshot_accept(J, reading)])

    real_append = jr.append_records
    crashed = []

    def crash_after_two(records, **kwargs):
        crashed.append(records)
        real_append(records[:2], **kwargs)
        raise RuntimeError("simulated crash between begin and commit")

    monkeypatch.setattr(jr, "append_records", crash_after_two)
    first = _rebuild(ns)
    monkeypatch.setattr(jr, "append_records", real_append)
    second = _rebuild(ns)
    capsys.readouterr()

    assert _percents() == [7.0, 9.0]
    assert crashed and crashed[0][0]["t"] == "correction_batch", crashed
    assert first == 3 and second == 0
    twins = _twins("sa:" + reading["id"])
    assert ("evt", 0, True) not in twins, twins
    batches = {r["batch"] for r in _records()
               if r.get("t") == "correction" and r["id"] == "sa:" + reading["id"]}
    assert len(batches) == 1, batches
    selection = J.resolve_effective_events(_records())
    assert batches <= selection.completed_batches

    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a completed batch was appended again"
    assert _percents() == [7.0, 9.0]


def test_a_live_decision_over_an_excluded_legacy_twin_is_a_correction(ns):
    """P3-12, legacy first then current: admission hides the excluded twin,
    so the live writer would classify the id NEW and append a stamped
    revision-0 twin. The physical collision check makes it a completed
    correction above the retained revision instead."""
    jr, J, core = _mods()
    _seed(jr, J)
    reading = _obs(J, "2026-01-04T10:00:00Z", 9.0)
    jr.append_record(reading)
    _raw_append_tail([_legacy_snapshot_accept(J, reading)])

    jr.run_stats_ingest(mode="authoritative")
    assert _percents() == [7.0, 9.0]
    event_id = "sa:" + reading["id"]
    twins = _twins(event_id)
    assert ("evt", 0, True) not in twins, twins
    assert ("correction", 1, True) in twins, twins
    # The previous release has no admission: it must see a higher revision,
    # not a same-revision conflict.
    legacy_view = J.resolve_effective_events(_records())
    assert legacy_view.by_id[event_id].rev == 1
    assert legacy_view.conflicts == ()

    before = _journal_bytes()
    jr.run_stats_ingest(mode="authoritative")
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before
    assert _percents() == [7.0, 9.0]


def test_an_admitted_twin_still_suppresses_a_metadata_only_append(
        ns, monkeypatch, tmp_path):
    """F5 is unchanged for ADMITTED retained decisions: a historical
    (pre-boundary) unstamped decision that the current pipeline re-derives
    differs only by the writer stamp, so nothing is appended."""
    jr, J, core = _mods()
    reading = _legacy(_obs(J, "2026-01-04T09:00:00Z", 7.0))
    # Store one: the current pipeline's own decision for that reading.
    redirect_paths(ns, monkeypatch, tmp_path / "one")
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(
        J.encode_line(reading))
    jr.run_stats_ingest(mode="authoritative")
    [stamped] = [r for r in _records() if r.get("t") == "evt"
                 and r["id"] == "sa:" + reading["id"]]
    historical = _legacy(stamped)

    # Store two: the journal retains only the historical, unstamped copy,
    # before the cutover op.
    redirect_paths(ns, monkeypatch, tmp_path / "two")
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(
        J.encode_line(reading) + J.encode_line(historical))
    jr.ensure_account_bound_cutover()
    jr.run_stats_ingest(mode="authoritative")
    assert _twins("sa:" + reading["id"]) == [("evt", 0, False)]
    assert _percents() == [7.0]
    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before


def test_rederive_allocates_a_revision_above_an_excluded_twin(ns):
    """Codex Q2 for rederive: an id whose only physical decision is an
    excluded legacy event is added at a revision above it, never at
    revision 0 beside it."""
    jr, J, core = _mods()
    import _cctally_rederive
    ns["open_cache_db"]().close()
    # A 0% reading needs no retained cost entries to be planned.
    reading = _obs(J, "2026-01-04T10:00:00Z", 0.0)
    jr.append_record(reading)
    _raw_append_tail([_legacy_snapshot_accept(J, reading)])

    preview = _cctally_rederive.preview_db_rederive("claude-usage")
    [action] = [a for a in preview.plan.actions
                if a.event_id == "sa:" + reading["id"]]
    assert action.disposition == "add"
    assert action.revision == 1


def test_a_rate_change_over_an_excluded_legacy_twin_is_a_correction(ns):
    """Codex Q2 for the third emission path. `meter_rate_change` is a
    Claude-event family whatever its provider, so an unstamped decision a
    previous-release process journals for the same transition past the
    cutover is excluded and invisible to the live classifier. Without the
    physical collision check `record_meter_rate_change` appends a stamped
    revision-0 twin, which the previous release reads as a same-revision
    conflict; with it, the decision lands as a completed correction above the
    retained revision and the row materializes once."""
    jr, J, core = _mods()
    import _lib_meter_rate_change as mrc
    _seed(jr, J)
    transition = mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from="2026-01-03T00:00:00+00:00",
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0, severity="alarm",
        detected_at="2026-01-04T11:00:00+00:00")
    event_id = J.evt_id(mrc.EVT_ID_PREFIX_V2, *transition.identity())
    legacy_at = "2026-01-04T11:00:00+00:00"
    _raw_append_tail([_legacy(J.make_evt(
        kind=mrc.EVT_KIND, id=event_id, at=legacy_at,
        payload=mrc.event_payload_v2(transition, created_at=legacy_at)))])

    result = jr.run_stats_ingest(
        mode="authoritative",
        meter_rate_change={"transition": transition, "notify": False,
                           "created_at": "2026-01-04T11:05:00+00:00",
                           "notification_owed": False})
    assert result.conflicts_dropped == 0
    twins = _twins(event_id)
    assert ("evt", 0, True) not in twins, twins
    assert ("correction", 1, True) in twins, twins
    legacy_view = J.resolve_effective_events(_records())
    assert legacy_view.by_id[event_id].rev == 1
    assert legacy_view.conflicts == ()

    def rows():
        conn = core.open_db()
        try:
            return conn.execute(
                "SELECT provider, account_key, effective_from "
                "FROM meter_rate_change_events").fetchall()
        finally:
            conn.close()

    expected = [("claude", "unattributed", "2026-01-03T00:00:00+00:00")]
    assert [tuple(r) for r in rows()] == expected
    before = _journal_bytes()
    jr.run_stats_ingest(mode="authoritative")
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before
    assert [tuple(r) for r in rows()] == expected


_DESCENT_TABLES = (
    "weekly_usage_snapshots", "week_reset_events", "weekly_credit_floors",
    "usage_snapshot_suppressions", "weekly_reset_debounce_state",
    "percent_milestones",
)


def _descent_state():
    """The weekly accounting a descent decides, without surrogate rowids."""
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        out = {}
        for table in _DESCENT_TABLES:
            columns = [row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")
                if row[1] not in ("id", "rowid", "journal_id")
                and not row[1].endswith("_id")]
            if not columns:
                continue
            out[table] = sorted(
                (tuple(row) for row in conn.execute(
                    f"SELECT {', '.join(columns)} FROM {table}")), key=repr)
        return out
    finally:
        conn.close()


def _descent_trace(ns, monkeypatch, tmp_path, *, interrupted):
    """70% (ingested) -> 60% for the same reset -> 20%. ``interrupted``: the
    60% reading's ingest never runs; a `db rebuild` (whose completion step
    must derive it) runs before the 20% reading is recorded."""
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J, core = _mods()
    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 70.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 60.0))
    if interrupted:
        # The completion must start from the trusted frontier the 70%
        # cycle left behind — the scenario is vacuous without it.
        frontier = jr.read_trusted_derivation_frontier(core.DB_PATH)
        assert frontier is not None
        assert _rebuild(ns) == 0
    else:
        jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-01-04T11:00:00Z", 20.0))
    jr.run_stats_ingest(mode="authoritative")
    return _descent_state()


def test_completion_predecessor_carries_the_frontier_cycles_outputs(
        ns, monkeypatch, tmp_path, capsys):
    """Fix round C, P1-1. The frontier is the consumed-input high water: the
    cycle that derived the 70% reading advanced it to its captured high water
    while its own `snapshot_accept` landed beyond it. A completion that
    rebuilt its predecessor through the frontier alone, and ignored admitted
    decisions in its replay window, derived the interrupted 60% reading as a
    first reading of the week — and the following 20% reading then received
    a different credit than uninterrupted processing gives it."""
    uninterrupted = _descent_trace(
        ns, monkeypatch, tmp_path / "live", interrupted=False)
    completed = _descent_trace(
        ns, monkeypatch, tmp_path / "completed", interrupted=True)
    capsys.readouterr()
    assert completed == uninterrupted


def test_the_frontier_names_where_its_cycles_decisions_end(ns):
    """Fix round C, P1-1: the proof keeps consumed-input coverage and output
    positions apart. After a cycle derives a reading, the frontier is the
    cycle's captured high water and the output end is past the decision the
    cycle journaled beyond it; an unsound output end makes the whole proof
    untrusted rather than silently dropping the decisions it names."""
    import sqlite3
    jr, J, core = _mods()
    reading = _obs(J, "2026-01-04T09:00:00Z", 70.0)
    jr.append_record(reading)
    jr.run_stats_ingest(mode="authoritative")
    state = jr.read_trusted_derivation_state(core.DB_PATH)
    assert state is not None and state.output_end is not None
    positions = {}
    for name in jr.list_segments():
        offset = 0
        for line in (core.JOURNAL_DIR / name).read_bytes().split(b"\n")[:-1]:
            record = J.decode_line(line)
            positions[record["id"]] = (name, offset)
            offset += len(line) + 1
    key = jr.journal_coordinate_key
    accept = positions["sa:" + reading["id"]]
    assert key(positions[reading["id"]]) < key(state.frontier)
    assert key(state.frontier) <= key(accept) < key(state.output_end)

    conn = sqlite3.connect(core.DB_PATH)
    try:
        conn.execute("UPDATE journal_derivation_frontier "
                     "SET output_offset = output_offset - 1")
        conn.commit()
    finally:
        conn.close()
    assert jr.read_trusted_derivation_state(core.DB_PATH) is None
    assert jr.read_trusted_derivation_frontier(core.DB_PATH) is None


def test_whole_history_reconstruction_keeps_a_previous_release_decision(
        ns, monkeypatch, tmp_path):
    """Fix round E (spec 4.7 revision 1): without a trusted frontier the
    reconstruction replays the whole retained journal, so it re-derives
    inputs a previous release decided. Such an input is history, not a gap:
    a release that journaled only its snapshot decision keeps exactly that,
    and the current pipeline's extra decisions for it (its cost snapshot and
    milestones) are never appended — uninterrupted ingest never derived
    them."""
    jr, J, core = _mods()
    reading = _legacy(_obs(J, "2026-01-04T09:00:00Z", 7.0))
    # The current pipeline's own decisions for that reading, from a scratch
    # store; the previous release journaled only the snapshot.
    redirect_paths(ns, monkeypatch, tmp_path / "one")
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(
        J.encode_line(reading))
    jr.run_stats_ingest(mode="authoritative")
    derived = [r["id"] for r in _records() if r.get("t") == "evt"]
    [snapshot] = [_legacy(r) for r in _records() if r.get("t") == "evt"
                  and r["id"] == "sa:" + reading["id"]]
    assert len(derived) > 1, derived  # non-vacuity: more than the snapshot

    redirect_paths(ns, monkeypatch, tmp_path / "two")
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "observations-2026-01.jsonl").write_bytes(
        J.encode_line(reading) + J.encode_line(snapshot))
    # The previous release's ingest consumed both before the upgrade.
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    jr.ensure_account_bound_cutover()
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    jr.run_stats_ingest(mode="authoritative")
    assert _percents() == [7.0, 9.0]

    before = _journal_bytes()
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)
    count = len(_records())
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, (
        "the reconstruction appended decisions for a previous release's input",
        [(r.get("t"), r.get("id"), r.get("action"))
         for r in _records()[count:]])
    assert _percents() == [7.0, 9.0]


# --------------------------------------------------------------------------
# Fix round J (review cycle 3 P1-1, Codex fix-round-4 Q1): completion's
# final round publishes only through the validated proven input endpoint.
# --------------------------------------------------------------------------

#: The readings a current recorder appends during completion rounds 0, 1 and
#: 2 (one per round, the last being 75), then the reading after them.
_ROUND_READINGS = (("2026-01-04T10:10:00Z", 60.0),
                   ("2026-01-04T10:20:00Z", 70.0),
                   ("2026-01-04T10:30:00Z", 75.0))
_AFTER_ROUNDS = ("2026-01-04T11:00:00Z", 20.0)


def _rederive():
    import sys
    parser = sys.modules["cctally"].build_parser()
    args = parser.parse_args(
        ["db", "rederive", "--family", "claude-usage", "--yes", "--json"])
    return args.func(args)


def _snapshot_multiplicity():
    """``{captured_at: rows}`` of the materialized usage snapshots."""
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        out: dict = {}
        for (captured,) in conn.execute(
                "SELECT captured_at_utc FROM weekly_usage_snapshots"):
            out[captured] = out.get(captured, 0) + 1
        return out
    finally:
        conn.close()


def _in_place_credits():
    """Every in-place weekly credit: identity, predecessor and payload."""
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(week_reset_events)") if row[1] != "id"]
        return sorted(tuple(row) for row in conn.execute(
            f"SELECT {', '.join(columns)} FROM week_reset_events"))
    finally:
        conn.close()


def _retain_session_input(ns):
    """`db rederive` re-derives from retained session input: one priced
    entry for the trace's account, identical in both traces."""
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT OR IGNORE INTO session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            ("/retained/session.jsonl", 100, 1, 100, "2026-01-01T00:00:00Z",
             "retained", "/retained"))
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, "
            "timestamp_utc, model, input_tokens, output_tokens, "
            "cache_create_tokens, cache_read_tokens, cache_create_1h_tokens, "
            "account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("/retained/session.jsonl", 0, "2026-01-04T08:00:00Z",
             "claude-3-5-sonnet-20241022", 100, 10, 0, 0, 0, "unattributed"))
        cache.commit()
    finally:
        cache.close()


@contextlib.contextmanager
def _at_publication(capture):
    """Within the block, run ``capture()`` right after each live-index
    publication, while the publisher still holds its locks: what the index
    published, before any post-unlock continuation acts on it. Yields the
    list of captures."""
    jr, _J, _core = _mods()
    real_locked = jr._rebuild_stats_index_locked
    captured = []

    def publishing(*args, **kwargs):
        result = real_locked(*args, **kwargs)
        if kwargs.get("target_path") is None:
            captured.append(capture())
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jr, "_rebuild_stats_index_locked", publishing)
        yield captured


def _drive_completion(ns, driver):
    """Run one production rebuild of the live index: `db rebuild`, or the
    corruption heal's detached worker."""
    _jr, _J, core = _mods()
    if driver == "db-rebuild":
        assert _rebuild(ns) == 0
        return
    import _cctally_db
    import _cctally_store as st
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)
    with pytest.raises(_cctally_db.StatsHealDeferred):
        core.open_db()
    assert st.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0


def _final_round_trace(ns, monkeypatch, tmp_path, *, driver):
    """50% (ingested) -> 55% (left underived) -> a completion whose three
    rounds each meet one more current append -> nothing further until 20%.
    ``driver`` None is the uninterrupted trace: every reading ingested in
    order. Returns the state right after the round readings, the final
    state, the published boundary evidence and the in-place credits.

    Fix round J4 (Codex cycle 5 P1-1): the residual reading's recorder never
    ingests it here — no test-issued ingest, hook, tick or maintenance
    command runs between the completion and 20% — so completion's own
    post-unlock continuation must materialize it."""
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J, core = _mods()
    import _cctally_completion
    _retain_session_input(ns)
    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 50.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 55.0))
    evidence = {}
    if driver is None:
        jr.run_stats_ingest(mode="authoritative")
        for at, pct in _ROUND_READINGS:
            jr.append_record(_obs(J, at, pct))
            jr.run_stats_ingest(mode="authoritative")
    else:
        assert jr.read_trusted_derivation_frontier(core.DB_PATH) is not None
        real_window = _cctally_completion._complete_window
        rounds = []
        appended = {}

        def interfering_window(base, tmp, start, target, boundary,
                               round_index, **kwargs):
            # A current recorder appends after the round's pinned input,
            # before the round journals its own outputs.
            if round_index < len(_ROUND_READINGS):
                at, pct = _ROUND_READINGS[round_index]
                reading = _obs(J, at, pct)
                jr.append_record(reading)
                appended[pct] = reading
            rounds.append(round_index)
            return real_window(base, tmp, start, target, boundary,
                               round_index, **kwargs)

        monkeypatch.setattr(
            _cctally_completion, "_complete_window", interfering_window)
        with _at_publication(
                lambda: _publication_boundary(appended[75.0])) as published:
            _drive_completion(ns, driver)
        monkeypatch.setattr(
            _cctally_completion, "_complete_window", real_window)
        # Non-vacuity: every completion round met one interfering append.
        assert rounds == list(range(_cctally_completion.MAX_ROUNDS)), rounds
        assert sorted(appended) == [60.0, 70.0, 75.0]
        [evidence] = published
    after_rounds = _descent_state()
    at, pct = _AFTER_ROUNDS
    jr.append_record(_obs(J, at, pct))
    jr.run_stats_ingest(mode="authoritative")
    return after_rounds, _descent_state(), evidence, _in_place_credits()


def _publication_boundary(residual):
    """What the index published right after the completion rebuild claims,
    against the coordinate of the residual reading (the last round's
    append): neither applied-cursor field, the admission summary, the
    durable selector prefix nor the derivation frontier may cover it."""
    import sqlite3
    jr, J, core = _mods()
    position = None
    for name in jr.list_segments():
        offset = 0
        for line in (core.JOURNAL_DIR / name).read_bytes().split(b"\n")[:-1]:
            record = J.decode_line(line)
            if record is not None and record.get("id") == residual["id"]:
                position = (name, offset)
            offset += len(line) + 1
    assert position is not None
    key = jr.journal_coordinate_key
    conn = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True)
    try:
        cursor_row = conn.execute(
            "SELECT segment, offset, applied_segment, applied_offset "
            "FROM journal_cursor WHERE id = 1").fetchone()
        summary = jr._read_admission_summary(conn)
        state = jr._read_derivation_state(conn)
    finally:
        conn.close()
    cursor = (cursor_row[0], cursor_row[1])
    applied = (cursor_row[2], cursor_row[3])
    return {
        "cursor_covers_residual": key(cursor) > key(position),
        "applied_covers_residual": key(applied) > key(position),
        "summary_covers_residual": (
            summary is not None and summary.complete_through is not None
            and key(summary.complete_through) > key(position)),
        "frontier_covers_residual": (
            state is not None and key(state.frontier) > key(position)),
        "frontier_is_cursor": state is not None and state.frontier == cursor,
        "output_end_past_cursor": (
            state is not None and state.output_end is not None
            and key(state.output_end) > key(cursor)),
    }


@pytest.mark.parametrize("driver", ["db-rebuild", "corruption-heal"])
def test_completion_final_round_leaves_the_residual_input_to_ingest(
        ns, monkeypatch, tmp_path, capsys, driver):
    """Review cycle 3 P1-1 (Codex fix-round-4 Q1). One current reading lands
    during each of completion's three rounds, ending with 75%, and the
    interference then stops. The final round's residual input stays outside
    both applied-cursor fields and the admission coverage of what the index
    PUBLISHED, and the proof keeps its separate output end. Fix round J4
    (Codex cycle 5 P1-1): completion's post-unlock continuation, not a
    test-issued ingest, then applies the owed decisions before deriving 75%
    — so right after the completion all three readings are materialized
    exactly once, and the following 20% gets the uninterrupted trace's
    credit identity, predecessor (75%) and payload. Rebuild and rederive
    afterwards change nothing."""
    expected_rounds, expected, _none, expected_credits = _final_round_trace(
        ns, monkeypatch, tmp_path / "live", driver=None)
    rounds_state, state, boundary, credits = _final_round_trace(
        ns, monkeypatch, tmp_path / "rounds", driver=driver)
    capsys.readouterr()

    # The continuation materialized the residual with nothing else run.
    assert rounds_state == expected_rounds
    # Non-vacuity: the uninterrupted 20% is an in-place credit from 75%.
    assert len(expected_credits) == 1, expected_credits
    assert credits == expected_credits
    assert state == expected
    multiplicity = _snapshot_multiplicity()
    assert sorted(multiplicity.values()) == [1] * 6, multiplicity
    # The publication boundary itself claimed no false coverage.
    assert boundary == {
        "cursor_covers_residual": False,
        "applied_covers_residual": False,
        "summary_covers_residual": False,
        "frontier_covers_residual": False,
        "frontier_is_cursor": True,
        "output_end_past_cursor": True,
    }, boundary

    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a later rebuild appended decisions"
    assert _descent_state() == expected
    assert _rederive() == 0
    assert _descent_state() == expected
    assert _in_place_credits() == expected_credits
    capsys.readouterr()


# --------------------------------------------------------------------------
# Fix round J4 (Codex cycle 5 P1-1): the recorders whose readings land during
# completion's rounds are REAL `record-usage` invocations. Each one appends
# its reading, exhausts its bounded maintenance wait against the running
# completion and exits; once the interference ends nothing else runs, so the
# residual reading must still materialize exactly once, automatically.
# --------------------------------------------------------------------------

#: The bounded waits the real recorder exhausts, shortened so three timed-out
#: recorders cost under a second. The code paths are the production ones.
_SHORT_WAIT_S = 0.2


def _real_recorder(ns, monkeypatch, at, pct):
    """One real `cctally record-usage` invocation captured at ``at``: its
    parser, its CLI adapter and its own authoritative ingest."""
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    parser = sys.modules["cctally"].build_parser()
    args = parser.parse_args(
        ["record-usage", "--percent", str(pct), "--resets-at", str(_W1)])
    return args.func(args)


def _recorder_process(ns, monkeypatch, at, pct, ingests):
    """Run the real recorder as a separate process would: in its own thread
    and execution context (no lock this test's thread holds is its), so its
    flock requests contend with the completion's. Returns once it has
    exited, with how it exited and every ingest result it saw."""
    import contextvars
    import threading
    import _cctally_db
    outcome: dict = {}
    monkeypatch.setenv("CCTALLY_AS_OF", at)

    def run():
        try:
            outcome["exit"] = _real_recorder(ns, monkeypatch, at, pct)
        except _cctally_db.StatsDbMaintenanceError as exc:
            # `main()` maps this to exit 3: the recorder gave up.
            outcome["exit"] = 3
            outcome["error"] = exc
        except BaseException as exc:  # noqa: BLE001 — reported below
            outcome["unexpected"] = exc
        outcome["ingests"] = list(ingests.get(threading.get_ident(), ()))

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), "the recorder did not exit"
    assert "unexpected" not in outcome, outcome
    return outcome


def _recorder_trace(ns, monkeypatch, tmp_path, *, driver, cache_stop=False):
    """50% (ingested) -> 55% (left underived) -> 60%, 70%, 75% from real
    recorders -> 20% from a real recorder. ``driver`` None is the
    uninterrupted trace: every recorder ingests its own reading. Otherwise
    the three recorders run during completion's three rounds and time out.
    Returns the state right after 75%, the final state, the credits and the
    evidence.

    ``cache_stop`` (fix round K5, Codex cycle 6 P1-1): a Codex quota
    observation lands just before 75%, and from the publication on another
    process holds the real cache writer lock, so the continuation's ONE
    ingest prefix-stops before the residual reading. The continuation runs
    in real threads (`tests/_statusline_continuation.Dispatch`), waiters
    included; once its attempt has returned, the lock is released and
    nothing else is run."""
    import threading
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J, core = _mods()
    import _cctally_completion
    import _cctally_store
    quota = _codex_quota_obs(J) if cache_stop else None
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    _retain_session_input(ns)
    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 50.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 55.0))
    evidence: dict = {}
    if driver is None:
        jr.run_stats_ingest(mode="authoritative")
        for at, pct in _ROUND_READINGS:
            if quota is not None and pct == 75.0:
                jr.append_record(quota)
            assert _real_recorder(ns, monkeypatch, at, pct) == 0
    else:
        assert jr.read_trusted_derivation_frontier(core.DB_PATH) is not None
        import _cctally_statusline as sl
        inline_spawn = sl._spawn_continuation
        dispatch = D.Dispatch(monkeypatch) if cache_stop else None
        # Every ingest and who ran it: the recorders' timed-out attempts,
        # and anything that runs once the completion has published.
        ingests: dict = {}
        real_ingest = jr.run_stats_ingest

        def observed_ingest(*args, **kwargs):
            try:
                result = real_ingest(*args, **kwargs)
            except BaseException as exc:
                ingests.setdefault(threading.get_ident(), []).append(
                    ("raised", type(exc).__name__))
                raise
            ingests.setdefault(threading.get_ident(), []).append((
                "ran" if result.ran else "declined",
                getattr(result.blocked_by, "lock", None), result.consumed,
                type(result.error).__name__ if result.error else None))
            return result

        monkeypatch.setattr(jr, "run_stats_ingest", observed_ingest)
        monkeypatch.setattr(_cctally_store, "_STATS_OPEN_MAINTENANCE_WAIT_S",
                            _SHORT_WAIT_S)
        monkeypatch.setitem(real_ingest.__kwdefaults__, "timeout_s",
                            _SHORT_WAIT_S)
        real_window = _cctally_completion._complete_window
        rounds = []
        recorders = []

        def interfering_window(base, tmp, start, target, boundary,
                               round_index, **kwargs):
            # A real recorder runs after the round's pinned input, before
            # the round journals its own outputs: it appends, waits out its
            # bound against the completion's locks, and exits.
            if round_index < len(_ROUND_READINGS):
                at, pct = _ROUND_READINGS[round_index]
                if quota is not None and pct == 75.0:
                    jr.append_record(quota)
                recorders.append(_recorder_process(
                    ns, monkeypatch, at, pct, ingests))
            rounds.append(round_index)
            return real_window(base, tmp, start, target, boundary,
                               round_index, **kwargs)

        monkeypatch.setattr(
            _cctally_completion, "_complete_window", interfering_window)
        main = threading.get_ident()
        held = []

        def at_publication():
            if cache_stop:
                # Every ingest after this point, whichever thread runs it.
                before = {key: len(value) for key, value in ingests.items()}
                held.append(_HeldCacheWriterLock().__enter__())
                return before
            return len(ingests.get(main, ()))

        try:
            with _at_publication(at_publication) as published:
                _drive_completion(ns, driver)
            if cache_stop:
                # The continuation's one attempt has returned, under the
                # held lock, before anything is released.
                dispatch.settle("worker")
                [before] = published
                stopped = [entry for key, value in ingests.items()
                           for entry in value[before.get(key, 0):]]
                evidence["under_stop"] = stopped
                evidence["quota_rows_under_stop"] = _codex_quota_rows(ns)
                evidence["marker_under_stop"] = (
                    _cctally_completion.residual_derivation_owed())
        finally:
            for lock in held:
                lock.__exit__(None, None, None)
        if cache_stop:
            # The lock's release is the only further event: nothing here
            # ingests, ticks, renders or runs a hook.
            dispatch.join()
            assert not dispatch.errors, dispatch.errors
            [before] = published
            evidence["after_publication"] = [
                entry for key, value in ingests.items()
                for entry in value[before.get(key, 0):]]
            evidence["cache_waiters"] = dispatch.count("wait", "cache")
            evidence["marker_after_release"] = (
                _cctally_completion.residual_derivation_owed())
            evidence["quota_rows_after_release"] = _codex_quota_rows(ns)
            # Back to the in-process default: what follows runs inline.
            monkeypatch.setattr(sl, "_spawn_continuation", inline_spawn)
        monkeypatch.setattr(
            _cctally_completion, "_complete_window", real_window)
        monkeypatch.setattr(jr, "run_stats_ingest", real_ingest)
        # Non-vacuity: every round met one recorder, and each recorder
        # appended its reading, exhausted its bounded wait and exited
        # without consuming anything.
        assert rounds == list(range(_cctally_completion.MAX_ROUNDS)), rounds
        assert len(recorders) == len(_ROUND_READINGS), recorders
        for outcome in recorders:
            assert outcome["ingests"], outcome
            assert all(entry[0] != "ran" for entry in outcome["ingests"]), (
                outcome)
            assert outcome["exit"] == 3 or any(
                entry[0] == "declined" for entry in outcome["ingests"]), outcome
        if not cache_stop:
            [at_publication_count] = published
            evidence["after_publication"] = ingests.get(main, [])[
                at_publication_count:]
    after_rounds = _descent_state()
    multiplicity = _snapshot_multiplicity()
    at, pct = _AFTER_ROUNDS
    assert _real_recorder(ns, monkeypatch, at, pct) == 0
    return after_rounds, multiplicity, _descent_state(), _in_place_credits(), (
        evidence)


@pytest.mark.parametrize("driver", ["db-rebuild", "corruption-heal"])
def test_residual_input_of_timed_out_recorders_materializes_after_completion(
        ns, no_spawn, monkeypatch, tmp_path, capsys, driver):
    """Fix round J4 (Codex cycle 5 P1-1). Three real recorders append 60%,
    70% and 75% during completion's three rounds; each exhausts its bounded
    maintenance wait and exits. Completion publishes only through its proven
    input endpoint, so 75% is residual. Once the interference ends the test
    runs nothing — no ingest, hook, tick or maintenance command — and the
    residual reading must still be materialized, exactly once, by exactly
    one ingest that ran after the publication, with the uninterrupted
    trace's state; the following 20% then gets the uninterrupted credit."""
    expected_rounds, _multiplicity, expected, expected_credits, _none = (
        _recorder_trace(ns, monkeypatch, tmp_path / "live", driver=None))
    rounds_state, multiplicity, state, credits, evidence = _recorder_trace(
        ns, monkeypatch, tmp_path / "rounds", driver=driver)
    capsys.readouterr()

    assert rounds_state == expected_rounds, "the residual input was left"
    assert sorted(multiplicity.values()) == [1] * 5, multiplicity
    ran = [entry for entry in evidence["after_publication"]
           if entry[0] == "ran" and entry[2] > 0]
    assert len(ran) == 1, evidence
    # Non-vacuity: the uninterrupted 20% is an in-place credit from 75%.
    assert len(expected_credits) == 1, expected_credits
    assert credits == expected_credits
    assert state == expected
    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a later rebuild appended decisions"
    assert _descent_state() == expected
    capsys.readouterr()


@pytest.mark.parametrize("driver", ["db-rebuild", "corruption-heal"])
def test_residual_input_materializes_once_a_continuation_cache_stop_is_released(
        ns, no_spawn, monkeypatch, tmp_path, capsys, driver):
    """Fix round K5 (Codex cycle 6 P1-1). The timed-out recorders' trace, with
    a Codex quota observation just before the residual 75% and the real
    cache writer lock held by another process from the publication on. The
    continuation's ONE authoritative ingest therefore returns a cache-leg
    prefix stop before 75%. That attempt must neither discharge the
    obligation nor drop the lock dependency: once the lock is released, and
    with nothing else run, the residual reading is materialized exactly
    once, through the one-shot waiter of the cache lock, with the
    uninterrupted trace's state and credit."""
    expected_rounds, _multiplicity, expected, expected_credits, _none = (
        _recorder_trace(ns, monkeypatch, tmp_path / "live", driver=None,
                        cache_stop=True))
    expected_quota = _codex_quota_rows(ns)
    rounds_state, multiplicity, state, credits, evidence = _recorder_trace(
        ns, monkeypatch, tmp_path / "rounds", driver=driver, cache_stop=True)
    capsys.readouterr()

    # Non-vacuity: the continuation's attempt ran and prefix-stopped on the
    # held cache lock, consuming nothing past the published prefix.
    assert expected_quota == 1, expected_quota
    stopped = [entry for entry in evidence["under_stop"] if entry[0] == "ran"]
    assert stopped and all(entry[1] == "cache" and entry[2] == 0
                           for entry in stopped), evidence
    assert evidence["quota_rows_under_stop"] == 0, evidence
    # The obligation outlived the blocked attempt, and its lock dependency
    # was handed to the cache lock's one-shot waiter.
    assert evidence["marker_under_stop"], (
        "the blocked attempt discharged the residual obligation")
    assert evidence["cache_waiters"] == 1, evidence
    # Released: materialized automatically, exactly once, and discharged.
    assert rounds_state == expected_rounds, "the residual input was left"
    assert sorted(multiplicity.values()) == [1] * 5, multiplicity
    consumed = [entry for entry in evidence["after_publication"]
                if entry[0] == "ran" and entry[2] > 0]
    assert len(consumed) == 1, evidence
    assert evidence["quota_rows_after_release"] == 1, evidence
    assert not evidence["marker_after_release"], (
        "a covered residual left its obligation behind")
    # Non-vacuity: the uninterrupted 20% is an in-place credit from 75%.
    assert len(expected_credits) == 1, expected_credits
    assert credits == expected_credits
    assert state == expected
    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a later rebuild appended decisions"
    assert _descent_state() == expected
    capsys.readouterr()


# --------------------------------------------------------------------------
# Fix round J3 (Codex cycle 3 verification P1-1): the decisions completion's
# final round owes past the published cursor must reach the index before the
# residual input is derived, even when the cache leg prefix-stops the
# recorder's ingest between that input and those decisions.
# --------------------------------------------------------------------------

#: The readings a current recorder appends during completion rounds 0, 1 and
#: 2; the last round's residual descent (20%) is followed by a Codex quota
#: observation before the round journals the decisions owed for 70%.
_STOP_ROUND_READINGS = (("2026-01-04T10:10:00Z", 60.0),
                        ("2026-01-04T10:20:00Z", 70.0),
                        ("2026-01-04T10:30:00Z", 20.0))
_QUOTA_AT = "2026-01-04T10:31:00Z"


def _codex_quota_obs(J):
    return J.make_obs(at=_QUOTA_AT, src="codex-quota", provider="codex",
                      payload={
                          "kind": "quota_window_snapshot",
                          "source": "codex", "source_root_key": "root-a",
                          "source_path": "/codex/root-a/r.jsonl",
                          "line_offset": 10, "captured_at_utc": _QUOTA_AT,
                          "observed_slot": "primary",
                          "logical_limit_key": "limit-primary",
                          "limit_id": "native-primary", "limit_name": "Primary",
                          "window_minutes": 300, "used_percent": 10.0,
                          "resets_at_utc": "2026-01-04T15:00:00Z",
                          "plan_type": "pro", "individual_limit_json": None,
                          "reached_type": None,
                          "observed_model": "gpt-5.3-codex"})


def _codex_quota_rows(ns):
    cache = ns["open_cache_db"]()
    try:
        return cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source = 'codex'").fetchone()[0]
    finally:
        cache.close()


def _credit_predecessors():
    """The pre-credit percent each in-place weekly credit was derived
    against."""
    _jr, _J, core = _mods()
    conn = core.open_db()
    try:
        return sorted(row[0] for row in conn.execute(
            "SELECT observed_pre_credit_pct FROM week_reset_events"))
    finally:
        conn.close()


def _effective_credit_decisions():
    """``(id, payload)`` of every effective `week_reset` decision, with
    multiplicity."""
    import json
    _jr, J, _core = _mods()
    selection = J.resolve_effective_events(_records())
    return sorted(
        (selected.event_id, json.dumps(selected.record["payload"],
                                       sort_keys=True))
        for selected in selection.by_id.values()
        if selected.status == "active"
        and selected.record["payload"].get("kind") == "week_reset")


def _coordinate_of(record_id):
    jr, J, core = _mods()
    for name in jr.list_segments():
        offset = 0
        for line in (core.JOURNAL_DIR / name).read_bytes().split(b"\n")[:-1]:
            record = J.decode_line(line)
            if record is not None and record.get("id") == record_id:
                return (name, offset)
            offset += len(line) + 1
    return None


class _HeldCacheWriterLock:
    """The global cache writer flock, held by another open file description
    (BSD flock contends in process), so the ingest cycle's cache leg finds
    it busy and prefix-stops."""

    def __enter__(self):
        import fcntl
        import os
        _jr, _J, core = _mods()
        self._fd = os.open(str(core.CACHE_LOCK_PATH),
                           os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *exc):
        import fcntl
        import os
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        return False


def _prefix_stop_trace(ns, monkeypatch, tmp_path, *, driver):
    """50% (ingested) -> 55% (left underived) -> a completion whose rounds
    meet 60%, 70% and then 20% plus a Codex quota observation -> the waiting
    recorder's ingest under a held cache writer lock -> its ingest after the
    release. ``driver`` None is the uninterrupted trace. Returns the state,
    the in-place credits and the evidence of the separation."""
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J, core = _mods()
    import _cctally_completion
    _retain_session_input(ns)
    jr.append_record(_obs(J, "2026-01-04T09:00:00Z", 50.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 55.0))
    quota = _codex_quota_obs(J)
    evidence: dict = {}
    if driver is None:
        jr.run_stats_ingest(mode="authoritative")
        for at, pct in _STOP_ROUND_READINGS:
            jr.append_record(_obs(J, at, pct))
            if pct == 20.0:
                jr.append_record(quota)
            jr.run_stats_ingest(mode="authoritative")
        return _descent_state(), _in_place_credits(), evidence
    assert jr.read_trusted_derivation_frontier(core.DB_PATH) is not None
    real_window = _cctally_completion._complete_window
    rounds = []
    appended = {}

    def interfering_window(base, tmp, start, target, boundary,
                           round_index, **kwargs):
        if round_index < len(_STOP_ROUND_READINGS):
            at, pct = _STOP_ROUND_READINGS[round_index]
            reading = _obs(J, at, pct)
            jr.append_record(reading)
            appended[pct] = reading
            if pct == 20.0:
                jr.append_record(quota)
        rounds.append(round_index)
        return real_window(base, tmp, start, target, boundary,
                           round_index, **kwargs)

    monkeypatch.setattr(
        _cctally_completion, "_complete_window", interfering_window)
    held = []

    def at_publication():
        boundary = _publication_boundary(appended[20.0])
        # Fix round J4: the first ingest after the publication is
        # completion's own post-unlock continuation, so the cache writer
        # lock is busy from the publication on, and that ingest is the one
        # that prefix-stops.
        held.append(_HeldCacheWriterLock().__enter__())
        return boundary

    # Fix round K5 (Codex cycle 6 P1-1): the continuation and the cache
    # lock's one-shot waiter run in real threads, so the lock's release is
    # what resumes the residual ingest; nothing below runs one.
    import _cctally_statusline as sl
    inline_spawn = sl._spawn_continuation
    dispatch = D.Dispatch(monkeypatch)
    try:
        with _at_publication(at_publication) as published:
            _drive_completion(ns, driver)
        dispatch.settle("worker")
        quota_rows_under_stop = _codex_quota_rows(ns)
        evidence["predecessors_under_stop"] = _credit_predecessors()
        owed_under_stop = _cctally_completion.residual_derivation_owed()
    finally:
        for lock in held:
            lock.__exit__(None, None, None)
    dispatch.join()
    assert not dispatch.errors, dispatch.errors
    monkeypatch.setattr(sl, "_spawn_continuation", inline_spawn)
    monkeypatch.setattr(
        _cctally_completion, "_complete_window", real_window)
    # Non-vacuity: every round met its append, the published index stops
    # before the residual 20%, and the decisions owed for 70% lie past the
    # quota observation, so a prefix stop at it separates them.
    assert rounds == list(range(_cctally_completion.MAX_ROUNDS)), rounds
    assert sorted(appended) == [20.0, 60.0, 70.0]
    [boundary] = published
    assert not boundary["cursor_covers_residual"], boundary
    assert boundary["output_end_past_cursor"], boundary
    key = jr.journal_coordinate_key
    owed = _coordinate_of("sa:" + appended[70.0]["id"])
    assert owed is not None
    assert key(_coordinate_of(appended[20.0]["id"])) < key(
        _coordinate_of(quota["id"])) < key(owed)
    assert quota_rows_under_stop == 0, "non-vacuity: the cache leg stopped"
    assert owed_under_stop, "the blocked attempt discharged the obligation"
    assert dispatch.count("wait", "cache") == 1
    # The release alone resumed the residual ingest.
    assert _codex_quota_rows(ns) == 1
    assert not _cctally_completion.residual_derivation_owed()
    return _descent_state(), _in_place_credits(), evidence


@pytest.mark.parametrize("driver", ["db-rebuild", "corruption-heal"])
def test_owed_decisions_precede_the_residual_input_across_a_cache_prefix_stop(
        ns, monkeypatch, tmp_path, capsys, driver):
    """Codex cycle 3 verification P1-1. Completion's final round publishes
    through 70% and owes 70%'s decisions past the residual 20% and a Codex
    quota observation. The first ingest after the publication (completion's
    own post-unlock continuation since fix round J4) then finds the cache
    writer lock busy, so the cache leg prefix-stops at the quota observation
    — before the owed decisions. Deriving 20% in that cycle credits it
    against 60%; it must instead wait until the owed decisions are applied,
    and since fix round K5 the lock's release alone (its one-shot waiter)
    resumes that ingest, with no test-issued one. The credit is
    uninterrupted ingest's 70% -> 20%, with identical
    identities, payloads and multiplicities, and a later rebuild and
    rederive change nothing."""
    expected, expected_credits, _none = _prefix_stop_trace(
        ns, monkeypatch, tmp_path / "live", driver=None)
    expected_predecessors = _credit_predecessors()
    expected_decisions = _effective_credit_decisions()
    state, credits, evidence = _prefix_stop_trace(
        ns, monkeypatch, tmp_path / "rounds", driver=driver)
    capsys.readouterr()

    # Non-vacuity: the uninterrupted 20% is one in-place credit from 70%.
    assert expected_predecessors == [70.0], expected_predecessors
    assert len(expected_decisions) == 1, expected_decisions
    # No cycle derived the residual 20% against 60% under the stop.
    assert evidence["predecessors_under_stop"] in ([], [70.0]), evidence
    assert _credit_predecessors() == [70.0]
    assert credits == expected_credits
    assert _effective_credit_decisions() == expected_decisions
    assert state == expected
    multiplicity = _snapshot_multiplicity()
    assert sorted(multiplicity.values()) == [1] * 5, multiplicity

    before = _journal_bytes()
    assert _rebuild(ns) == 0
    assert _journal_bytes() == before, "a later rebuild appended decisions"
    assert _descent_state() == expected
    assert _rederive() == 0
    assert _descent_state() == expected
    assert _in_place_credits() == expected_credits
    assert _effective_credit_decisions() == expected_decisions
    capsys.readouterr()


def _classification_coverage():
    """What drop classification reads from the live index for the trace's
    account: covered (True) or `_UNCOVERED` (False)."""
    import _cctally_statusline as sl
    frontier = sl._PublicationFrontier(sl._journal_extents(), False)
    return sl._covered_drop_status("unattributed", frontier) is not sl._UNCOVERED


def test_classification_requires_derivation_completeness_not_applied_coverage(
        ns):
    """Codex fix-round-4 Q1: classification verifies consumed-input and
    output completeness, not applied coverage alone. An apply-only rebuild
    that consumed an underived current reading leaves an index whose applied
    cursor covers the whole journal while its derivation proof stops before
    that reading; it must not classify. Once completion and ordinary ingest
    have derived the reading, it does."""
    jr, J, core = _mods()
    _seed(jr, J)
    jr.run_stats_ingest(mode="authoritative")
    assert _classification_coverage(), "non-vacuity: a derived index classifies"
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    # Apply-only: no completion, the replaced index's proof is carried.
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _percents() == [7.0], "non-vacuity: the reading was not derived"
    state = jr.read_trusted_derivation_state(core.DB_PATH)
    assert state is not None
    assert jr.journal_coordinate_key(state.frontier) < (
        jr.journal_coordinate_key(jr.journal_safe_high_water()))
    assert not _classification_coverage()

    assert _rebuild(ns) == 0          # completion derives the reading
    jr.run_stats_ingest(mode="authoritative")
    jr.run_stats_ingest(mode="authoritative")
    assert _percents() == [7.0, 9.0]
    assert _classification_coverage()


def test_a_rederive_racing_a_recorder_leaves_a_proof_ingest_can_extend(
        ns, monkeypatch, capsys):
    """Codex fix-round-4 Q1: ordinary ingest advances coverage
    transactionally after completion. `db rederive` rebuilds through its
    correction batch's commit high water, and a recorder appends its reading
    after that pin and before completion runs (the ingest lock keeps its
    ingest waiting). Nothing derived anything between the pin and
    completion, so the published proof must reach the pinned prefix and the
    recorder's own ingest must extend it -- otherwise the proof stays behind
    the cursor until some later rebuild, and drop classification is withheld
    meanwhile."""
    jr, J, core = _mods()
    import _cctally_rederive
    _retain_session_input(ns)
    _seed(jr, J)
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.run_stats_ingest(mode="authoritative")
    # More retained cost input than the cost snapshots priced: the rederive
    # has a correction to journal.
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, "
            "timestamp_utc, model, input_tokens, output_tokens, "
            "cache_create_tokens, cache_read_tokens, cache_create_1h_tokens, "
            "account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("/retained/session.jsonl", 50, "2026-01-04T08:30:00Z",
             "claude-3-5-sonnet-20241022", 5000, 500, 0, 0, 0,
             "unattributed"))
        cache.commit()
    finally:
        cache.close()
    raced = []

    def hook(stage):
        if stage == "after-batch-commit" and not raced:
            raced.append(_obs(J, "2026-01-04T11:00:00Z", 12.0))
            jr.append_record(raced[0])

    monkeypatch.setattr(_cctally_rederive, "_REDERIVE_CRASH_HOOK", hook)
    with _at_publication(_percents) as published:
        assert _rederive() == 0
    monkeypatch.setattr(_cctally_rederive, "_REDERIVE_CRASH_HOOK", None)
    capsys.readouterr()
    # Non-vacuity: the rederive journaled a batch and the race happened
    # after its pin, so the index its rebuild PUBLISHED stopped before the
    # later reading (fix round J4: completion's post-unlock continuation may
    # derive it before the rederive returns).
    assert raced
    assert published and published[-1] == [7.0, 9.0], published
    jr.run_stats_ingest(mode="authoritative")
    jr.run_stats_ingest(mode="authoritative")
    assert _percents() == [7.0, 9.0, 12.0]
    state = jr.read_trusted_derivation_state(core.DB_PATH)
    assert state is not None and state.frontier == jr.journal_safe_high_water()
    assert _classification_coverage()


def test_the_residual_obligation_outlives_an_ingest_that_never_returns(
        ns, monkeypatch):
    """The completion's residual marker (fix round J4) is discharged only by
    an ingest attempt that RETURNS: a worker killed inside the ingest leaves
    the obligation for the next activating event, and a marker a newer
    completion wrote during the attempt survives it (review cycle 6 primary
    correction). Fix round K5 (Codex cycle 6 P1-1) narrows the rest: a
    returned attempt discharges the marker only when its applied cursor
    covers the residual, and a lock-contention stop keeps it and hands its
    lock to the one-shot release handoff.

    Fix round K6 (Codex cycle 6 verification P1-1): a failed attempt that is
    NOT a lock-contention stop no longer deletes the durable obligation. It
    records the failure in the marker it attempted, with a backoff, and does
    not re-arm itself: until the backoff ends, neither a further attempt nor
    an activating event runs another ingest, and the statusline does not
    count the obligation as pending work. A later independent event after
    the backoff runs the next attempt, which discharges it."""
    import _cctally_completion
    import _cctally_statusline as sl
    jr, J, _core = _mods()
    seen = []
    calls = []
    now = [1_800_000_000.0]
    monkeypatch.setattr(_cctally_completion, "_now", lambda: now[0],
                        raising=False)

    class Killed(BaseException):
        """Stands for the worker process dying inside the ingest."""

    def dies(**_kwargs):
        seen.append(_cctally_completion.residual_derivation_owed())
        raise Killed()

    _cctally_completion.owe_residual_derivation(("2026-01.jsonl", 10))
    monkeypatch.setattr(jr, "run_stats_ingest", dies)
    with pytest.raises(Killed):
        _cctally_completion.run_owed_residual_derivation()
    assert seen == [True], "the marker was gone while its ingest ran"
    assert _cctally_completion.residual_derivation_owed(), (
        "a worker that died inside the ingest discharged the obligation")

    # A lock-contention stop: the obligation stays, and the attempt reports
    # the lock that refused it — once, with no retry of its own.
    contended = jr.LockBlock("ingest", "exclusive", True)

    def blocked(**_kwargs):
        calls.append("blocked")
        return jr.IngestResult(ran=False, consumed=0, malformed=0,
                               events_emitted=0, alerts=[],
                               blocked_by=contended)

    monkeypatch.setattr(jr, "run_stats_ingest", blocked)
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert _cctally_completion.residual_derivation_owed(), (
        "a lock-contention stop discharged the residual obligation")
    assert calls == ["blocked"], "the attempt retried on its own"
    assert attempt.owed and attempt.blocked_by == contended, attempt
    assert attempt.disposition == "blocked", attempt

    # A failed attempt that is not a lock-contention stop keeps the durable
    # obligation and records its failure.
    def fails(**_kwargs):
        calls.append("fails")
        raise RuntimeError("a genuine ingest error")

    monkeypatch.setattr(jr, "run_stats_ingest", fails)
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert _cctally_completion.residual_derivation_owed(), (
        "a failed attempt deleted the durable obligation")
    assert calls == ["blocked", "fails"]
    assert attempt.owed and attempt.blocked_by is None, attempt
    assert attempt.disposition == "failed", attempt
    assert "RuntimeError" in attempt.reason, attempt
    [document] = _cctally_completion.residual_obligations()
    assert document["failures"] == 1, document
    assert "RuntimeError" in document["lastFailure"], document
    first_retry = document["retryAt"]
    assert first_retry > now[0], document
    # The obligation registered as pending work when it was recorded (fix
    # round K7). In production every attempt runs inside the continuation
    # worker, and the pass that follows it records the outcome there: a
    # backed-off obligation is not pending work.
    assert sl._read_continuation_state()["pending"], (
        "the recorded obligation was not registered as pending work")
    sl._record_pass_outcome(())
    assert not sl._read_continuation_state()["pending"]

    # Its own failure does not re-arm it: inside the backoff a further
    # attempt runs no ingest, and an activating event dispatches nothing,
    # because the statusline does not count the obligation as pending.
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert calls == ["blocked", "fails"], "the failure re-armed its own attempt"
    assert attempt.owed and attempt.disposition == "backoff", attempt
    assert _cctally_completion.residual_derivation_owed()
    assert not sl._work_pending_on_disk(), (
        "a backed-off obligation kept the coordination state pending")
    dispatch = D.Dispatch(monkeypatch)
    sl.post_wake("oauth")
    dispatch.join()
    assert dispatch.count("worker") == 0, (
        "an event inside the backoff dispatched another attempt")

    # The next failure, after the backoff, backs off longer.
    now[0] = first_retry
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert calls == ["blocked", "fails", "fails"]
    assert attempt.disposition == "failed", attempt
    [document] = _cctally_completion.residual_obligations()
    assert document["failures"] == 2, document
    assert document["retryAt"] - now[0] > first_retry - 1_800_000_000.0, document

    # A later independent event after the backoff: the next attempt runs,
    # its clean cycle covers the residual, and the obligation is discharged.
    def clean(**_kwargs):
        # A clean cycle consumes through the high water it reads.
        calls.append("clean")
        return jr.IngestResult(ran=True, consumed=1, malformed=0,
                               events_emitted=0, alerts=[],
                               applied_through=jr.journal_safe_high_water())

    monkeypatch.setattr(jr, "run_stats_ingest", clean)
    now[0] = document["retryAt"]
    assert sl._work_pending_on_disk(), (
        "an obligation past its backoff is not pending work")
    sl.post_wake("oauth")
    dispatch.join()
    assert not dispatch.errors, dispatch.errors
    assert dispatch.count("worker") == 1, dispatch.spawns
    assert calls[-1] == "clean", calls
    assert not _cctally_completion.residual_derivation_owed(), (
        "the attempt after the backoff left the obligation")
    assert not _cctally_completion.run_owed_residual_derivation().owed

    # An I/O error on a lock is no contention either: no release wake can
    # answer it, so it backs off the same way and is kept. The obligation
    # owes a real admitted reading, so only a cycle that consumed it covers
    # it.
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    _cctally_completion.owe_residual_derivation(("2026-01.jsonl", 10))
    io_error = jr.LockBlock("cache", "exclusive", False)
    monkeypatch.setattr(jr, "run_stats_ingest", lambda **_kwargs: (
        jr.IngestResult(ran=True, consumed=0, malformed=0, events_emitted=0,
                        alerts=[], blocked_by=io_error)))
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert _cctally_completion.residual_derivation_owed()
    assert attempt.disposition == "failed" and attempt.blocked_by is None
    monkeypatch.setattr(jr, "run_stats_ingest", clean)
    now[0] = _cctally_completion.residual_obligations()[0]["retryAt"]

    # A clean cycle consumed through its own high water, which it read
    # after the marker: the obligation is discharged.
    attempt = _cctally_completion.run_owed_residual_derivation()
    assert not _cctally_completion.residual_derivation_owed()
    assert attempt.disposition == "discharged", attempt
    assert not _cctally_completion.run_owed_residual_derivation().owed

    def newer_completion(**_kwargs):
        _cctally_completion.owe_residual_derivation(("2026-01.jsonl", 20))

    _cctally_completion.owe_residual_derivation(("2026-01.jsonl", 10))
    monkeypatch.setattr(jr, "run_stats_ingest", newer_completion)
    assert _cctally_completion.run_owed_residual_derivation().owed
    assert _cctally_completion.residual_derivation_owed(), (
        "the attempt removed the obligation a newer completion recorded")


def test_a_newer_completion_recorded_between_discharge_and_removal_materializes(
        ns, monkeypatch):
    """Fix round K6 (Codex cycle 6 verification P1-1). The continuation's
    attempt returns, its applied cursor covers the residual it read, and it
    removes the obligation. Exactly between proving that and the removal,
    another completion (an independently invoked `db rebuild`, which the
    reducer reservation does not exclude) publishes a capped prefix over a
    new residual reading and records its own obligation, then posts its
    publication wake. K5 compared the marker's bytes and then unlinked the
    shared path, so it removed the newer obligation, and with no statusline
    intake the reducer pass cleared pending work: the newer residual never
    materialized. Each completion's obligation is now its own durable
    record, and a discharge removes only the record it proved covered, so
    the newer residual materializes automatically — no recorder, ingest or
    other command runs after the first wake."""
    import contextvars
    import pathlib
    import threading
    import _cctally_completion
    import _cctally_statusline as sl
    jr, J, core = _mods()
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    _seed(jr, J)
    # Completion A's residual: an admitted derivable reading past its
    # published prefix.
    published = jr.journal_safe_high_water()
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 20.0))
    _cctally_completion.owe_residual_derivation(
        published, residual_through=jr.journal_safe_high_water())
    ingests = []
    real_ingest = jr.run_stats_ingest

    def observed_ingest(*args, **kwargs):
        result = real_ingest(*args, **kwargs)
        ingests.append(result.consumed)
        return result

    monkeypatch.setattr(jr, "run_stats_ingest", observed_ingest)
    real_unlink = pathlib.Path.unlink
    raced = []

    def newer_completion():
        # Another process: its own execution context, no lock of the worker.
        before = jr.journal_safe_high_water()
        jr.append_record(_obs(J, "2026-01-04T11:00:00Z", 30.0))
        _cctally_completion.owe_residual_derivation(
            before, residual_through=jr.journal_safe_high_water())
        sl.post_wake("index-publication")
        raced.append(before)

    def unlink(self, *args, **kwargs):
        try:
            relative = self.relative_to(core.APP_DIR)
        except ValueError:
            relative = None
        if (not raced and relative is not None
                and str(relative).startswith("derivation-residual")
                and not self.name.startswith(".")):
            # The attempt has proved its residual covered: race the removal.
            assert _percents() == [7.0, 20.0], "non-vacuity: A was derived"
            thread = threading.Thread(
                target=contextvars.Context().run, args=(newer_completion,),
                daemon=True)
            thread.start()
            thread.join(30)
            assert not thread.is_alive()
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    dispatch = D.Dispatch(monkeypatch)
    sl.post_wake("index-publication")
    dispatch.join()
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    assert not dispatch.errors, dispatch.errors
    assert raced, "non-vacuity: the newer completion raced the removal"
    # Nothing ran after the first wake but the continuation itself.
    assert _percents() == [7.0, 20.0, 30.0], (
        "the newer completion's residual reading never materialized")
    # One consuming attempt per obligation, both the continuation's.
    assert len([n for n in ingests if n > 0]) == 2, ingests
    assert dispatch.count("worker") == 1, dispatch.spawns
    assert not _cctally_completion.residual_derivation_owed(), (
        "a covered obligation was left behind")


def test_a_newer_completion_waking_after_the_pending_scan_materializes(
        ns, monkeypatch):
    """Fix round K7 (Codex round 14, P1-1). The continuation discharges
    completion A's residual, and its pass's end-of-pass record scans the
    disk: no intake, no frozen plan, no obligation, so pending is False.
    Exactly then -- after that scan and before the record takes the
    continuation-state lock -- an independently invoked completion B (a
    `db rebuild`, which the reducer reservation does not exclude) publishes
    a capped prefix over a new reading, records its own obligation and posts
    its real publication wake. Pending is still True, so K6's wake advanced
    only the wake generation and its dispatch lost the reservation; the
    record then cleared pending because no capture had moved the pending
    generation, and the worker's final hand-off ignored the changed wake
    because it required pending. B's file survived, but nothing would ever
    derive its reading. A completion's obligation now registers through the
    generation protocol a capture uses, so the stale clear is refused and
    the worker answers B's wake: the newer reading materializes, exactly
    once, with no further event."""
    import contextvars
    import threading
    import _cctally_completion
    import _cctally_statusline as sl
    jr, J, _core = _mods()
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    _seed(jr, J)
    published = jr.journal_safe_high_water()
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 20.0))
    _cctally_completion.owe_residual_derivation(
        published, residual_through=jr.journal_safe_high_water())
    ingests = []
    real_ingest = jr.run_stats_ingest

    def observed_ingest(*args, **kwargs):
        result = real_ingest(*args, **kwargs)
        ingests.append(result.consumed)
        return result

    monkeypatch.setattr(jr, "run_stats_ingest", observed_ingest)
    raced = []

    def newer_completion():
        # Another process: its own execution context, no lock of the worker.
        before = jr.journal_safe_high_water()
        jr.append_record(_obs(J, "2026-01-04T11:00:00Z", 30.0))
        _cctally_completion.owe_residual_derivation(
            before, residual_through=jr.journal_safe_high_water())
        sl.post_wake("index-publication")
        raced.append(before)

    real_scan = sl._work_pending_on_disk

    def scan():
        pending = real_scan()
        if (not pending and not raced
                and sys._getframe(1).f_code.co_name == "_record_pass_outcome"):
            # A's residual is derived and nothing else is owed: race the
            # window between this scan and the continuation-state lock.
            assert _percents() == [7.0, 20.0], "non-vacuity: A was derived"
            assert sl._read_continuation_state()["pending"], (
                "non-vacuity: B's wake finds pending work")
            thread = threading.Thread(
                target=contextvars.Context().run, args=(newer_completion,),
                daemon=True)
            thread.start()
            thread.join(30)
            assert not thread.is_alive(), "the racing completion hung"
        return pending

    monkeypatch.setattr(sl, "_work_pending_on_disk", scan)
    dispatch = D.Dispatch(monkeypatch)
    sl.post_wake("index-publication")
    dispatch.join()
    assert not dispatch.errors, dispatch.errors
    assert raced, "non-vacuity: the newer completion raced the pending clear"
    # Nothing ran after the first wake but the continuation itself.
    assert _percents() == [7.0, 20.0, 30.0], (
        "the newer completion's residual reading never materialized")
    assert len([n for n in ingests if n > 0]) == 2, ingests
    assert dispatch.count("worker") == 1, dispatch.spawns
    assert not _cctally_completion.residual_derivation_owed(), (
        "a covered obligation was left behind")
    assert not sl._read_continuation_state()["pending"], (
        "the drained coordination state was left pending")


@pytest.mark.parametrize("lost", ["state-write", "state-lock"])
def test_a_lost_registration_never_strands_a_durable_obligation(
        ns, monkeypatch, lost):
    """Fix round K13 (review cycle 7 P1-1, spec 4.7). As in the K7 race
    above, a newer completion B records its durable obligation after the
    older worker's end-of-pass disk scan found nothing, and B's publication
    wake lands while pending is still True — but B's pending-generation
    registration is lost: only its continuation-state write fails
    (``state-write``), or the continuation lock itself cannot be taken for
    it (``state-lock``). B's wake still advanced the wake generation, so the
    older worker's pending clear and its final release must honour that
    wake: B's reading is derived exactly once with no further event, and
    the lost registration is not silently absorbed."""
    import contextvars
    import errno
    import threading
    import _cctally_completion
    import _cctally_statusline as sl
    jr, J, _core = _mods()
    monkeypatch.setattr(sys.modules["cctally"], "_nudge_dashboard_repaint",
                        lambda: None)
    _seed(jr, J)
    published = jr.journal_safe_high_water()
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 20.0))
    _cctally_completion.owe_residual_derivation(
        published, residual_through=jr.journal_safe_high_water())
    ingests = []
    real_ingest = jr.run_stats_ingest

    def observed_ingest(*args, **kwargs):
        result = real_ingest(*args, **kwargs)
        ingests.append(result.consumed)
        return result

    monkeypatch.setattr(jr, "run_stats_ingest", observed_ingest)
    registering = threading.local()
    failed = []
    real_register = sl.register_durable_work
    real_write = sl._write_continuation_state
    real_lock = sl._ContinuationStateLock

    def register():
        registering.active = True
        try:
            return real_register()
        finally:
            registering.active = False

    def write(state):
        if getattr(registering, "active", False) and lost == "state-write":
            failed.append("write")
            raise OSError(errno.EIO, "injected registration write failure")
        return real_write(state)

    class Lock(real_lock):
        def __enter__(self):
            if getattr(registering, "active", False) and lost == "state-lock":
                failed.append("lock")
                raise OSError(errno.EIO, "injected registration lock failure")
            return super().__enter__()

    monkeypatch.setattr(sl, "register_durable_work", register)
    monkeypatch.setattr(sl, "_write_continuation_state", write)
    monkeypatch.setattr(sl, "_ContinuationStateLock", Lock)
    raced = []

    def newer_completion():
        before = jr.journal_safe_high_water()
        jr.append_record(_obs(J, "2026-01-04T11:00:00Z", 30.0))
        _cctally_completion.owe_residual_derivation(
            before, residual_through=jr.journal_safe_high_water())
        assert failed, "non-vacuity: B's registration was lost"
        wake = sl._read_continuation_state()["wakeGeneration"]
        sl.post_wake("index-publication")
        assert sl._read_continuation_state()["wakeGeneration"] == wake + 1, (
            "non-vacuity: B's publication wake was recorded")
        raced.append(before)

    real_scan = sl._work_pending_on_disk

    def scan():
        pending = real_scan()
        if (not pending and not raced
                and sys._getframe(1).f_code.co_name == "_record_pass_outcome"):
            assert _percents() == [7.0, 20.0], "non-vacuity: A was derived"
            assert sl._read_continuation_state()["pending"], (
                "non-vacuity: B's wake finds pending work")
            thread = threading.Thread(
                target=contextvars.Context().run, args=(newer_completion,),
                daemon=True)
            thread.start()
            thread.join(30)
            assert not thread.is_alive(), "the racing completion hung"
        return pending

    monkeypatch.setattr(sl, "_work_pending_on_disk", scan)
    dispatch = D.Dispatch(monkeypatch)
    sl.post_wake("index-publication")
    dispatch.join()
    assert not dispatch.errors, dispatch.errors
    assert raced, "non-vacuity: the newer completion raced the pending clear"
    assert _percents() == [7.0, 20.0, 30.0], (
        "a lost registration stranded the newer completion's residual")
    assert len([n for n in ingests if n > 0]) == 2, ingests
    assert dispatch.count("worker") == 1, dispatch.spawns
    assert not _cctally_completion.residual_derivation_owed(), (
        "a covered obligation was left behind")
    assert not sl._read_continuation_state()["pending"], (
        "the drained coordination state was left pending")


# --------------------------------------------------------------------------
# Review cycle 7 P1-1: a completion's residual obligation is durable or the
# completion fails; it is never best effort.
# --------------------------------------------------------------------------

def _publication_wakes(wakes):
    """The index-publication wakes among ``wakes`` (a journal append posts
    its own `append` wake)."""
    return [reason for reason in wakes if reason == "index-publication"]


class _FailingWrite:
    """A text handle whose every write fails, as on a full disk."""

    def __init__(self, handle):
        self._handle = handle

    def write(self, _data):
        import errno
        raise OSError(errno.ENOSPC, "injected obligation write failure")

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._handle.close()
        return False


@pytest.mark.parametrize("failure", ["none", "create", "write", "fsync"])
def test_a_completion_never_reports_success_without_its_durable_obligation(
        ns, no_spawn, monkeypatch, capsys, failure):
    """A real `db rebuild --db stats` publishes its capped index; a current
    reading appended while it ran is the residual it owes. When the
    obligation file cannot be created, written or fsync'd, the completion
    reported success with no obligation, so its wake found nothing to
    resume. It must fail instead, and the residual reading stays retained:
    the next ordinary ingest still derives it."""
    import errno
    import os
    import _cctally_completion
    jr, J, _core = _mods()
    _seed(jr, J)
    real_locked = jr._rebuild_stats_index_locked
    published = []

    def publish_then_record(*args, **kwargs):
        result = real_locked(*args, **kwargs)
        if kwargs.get("target_path") is None and not published:
            published.append(1)
            # A current recorder appends after the capped publication.
            jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
        return result

    monkeypatch.setattr(jr, "_rebuild_stats_index_locked", publish_then_record)
    residual = os.fspath(_cctally_completion.residual_dir())
    tracked = {}
    active = [failure != "none"]
    real_open, real_fdopen, real_fsync = os.open, os.fdopen, os.fsync

    def guarded_open(path, flags, *args, **kwargs):
        inside = (not isinstance(path, int)
                  and os.fspath(path).startswith(residual + os.sep))
        if active[0] and inside and failure == "create" and flags & os.O_CREAT:
            raise OSError(errno.ENOSPC, "injected obligation create failure",
                          os.fspath(path))
        fd = real_open(path, flags, *args, **kwargs)
        if inside and flags & os.O_CREAT:
            st = os.fstat(fd)
            tracked[fd] = (st.st_dev, st.st_ino)
        return fd

    def is_obligation_file(fd):
        # The same descriptor number may be reused once the file closed.
        try:
            st = os.fstat(fd)
        except OSError:
            return False
        return tracked.get(fd) == (st.st_dev, st.st_ino)

    def guarded_fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        if active[0] and failure == "write" and is_obligation_file(fd):
            return _FailingWrite(handle)
        return handle

    def guarded_fsync(fd):
        if active[0] and failure == "fsync" and is_obligation_file(fd):
            raise OSError(errno.EIO, "injected obligation fsync failure")
        return real_fsync(fd)

    # The completion wake would let the continuation discharge the
    # obligation at once; observing the file itself needs it held back, so
    # the wakes are recorded rather than posted.
    import _cctally_statusline
    wakes = []
    monkeypatch.setattr(_cctally_statusline, "post_wake", wakes.append)
    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "fdopen", guarded_fdopen)
    monkeypatch.setattr(os, "fsync", guarded_fsync)
    rc = _rebuild(ns)
    active[0] = False
    owed = _cctally_completion.residual_obligations()
    assert published, "non-vacuity: the capped index was published"
    if failure == "none":
        # Non-vacuity: the residual is real and its obligation is recorded.
        assert rc == 0
        assert len(owed) == 1, owed
        assert _publication_wakes(wakes) == ["index-publication"]
        return
    assert owed == [], "non-vacuity: the injected failure prevented the file"
    assert rc != 0, (
        "the completion reported success with no durable obligation")
    # Fix round K11 (K9K10-R7): the index WAS published, so its completion
    # wake is still posted, and the failure is reported as the lost
    # obligation, never as a failed rebuild.
    assert _publication_wakes(wakes) == ["index-publication"], (
        "the published index posted no completion wake")
    err = capsys.readouterr().err
    assert "stats.db rebuild failed" not in err, err
    assert "stats.db was rebuilt" in err, err
    # The residual reading is retained: ordinary ingest still derives it.
    assert _percents() == [7.0]
    assert jr.run_stats_ingest(mode="authoritative").error is None
    assert _percents() == [7.0, 9.0]


def test_a_reused_residual_directory_is_made_durable_by_every_writer(
        ns, no_spawn, monkeypatch):
    """The residual directory exists, but its creator died before fsyncing
    its parent, so a host crash can still lose the directory entry. A later
    completion that only trusted the existing directory skipped that
    barrier and lost its own obligation with it. Every writer fsyncs the
    parent after ensuring the directory exists."""
    import os
    import _cctally_completion
    directory = _cctally_completion.residual_dir()
    directory.mkdir(parents=True)        # its creator died right here
    parent = os.stat(directory.parent)
    synced = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        st = os.fstat(fd)
        synced.append((st.st_dev, st.st_ino))
        return real_fsync(fd)

    real_write = _cctally_completion._write_obligation
    writing = [False]

    def scoped_write(*args, **kwargs):
        writing[0] = True
        try:
            return real_write(*args, **kwargs)
        finally:
            writing[0] = False

    def scoped_fsync(fd):
        if writing[0]:
            return recording_fsync(fd)
        return real_fsync(fd)

    monkeypatch.setattr(_cctally_completion, "_write_obligation", scoped_write)
    monkeypatch.setattr(os, "fsync", scoped_fsync)
    _cctally_completion.owe_residual_derivation(
        ("2026-01.jsonl", 10), residual_through=("2026-01.jsonl", 20))
    assert len(_cctally_completion.residual_obligations()) == 1
    assert (parent.st_dev, parent.st_ino) in synced, (
        "the reused directory's parent entry was never made durable")
    own = os.stat(directory)
    assert (own.st_dev, own.st_ino) in synced


# --------------------------------------------------------------------------
# Fix round K11 (K9K10-R7): every completion caller posts the publication
# wake when the obligation is lost after publication, and reports it as such.
# --------------------------------------------------------------------------

def _losing_obligation_writes(monkeypatch):
    """Every residual obligation write fails, as on a full disk."""
    import errno
    import _cctally_completion

    def refusing_write(path, document):
        raise OSError(errno.ENOSPC, "injected obligation write failure",
                      str(path))

    monkeypatch.setattr(_cctally_completion, "_write_obligation",
                        refusing_write)


def _recorded_wakes(monkeypatch):
    import _cctally_statusline
    wakes = []
    monkeypatch.setattr(_cctally_statusline, "post_wake", wakes.append)
    return wakes


def test_a_heal_that_lost_its_obligation_still_wakes(
        ns, no_spawn, monkeypatch):
    """The corruption heal's rebuild publishes, then cannot record the
    residual obligation a reading appended meanwhile owes. The worker
    recorded the heal as failed and posted no wake, although the healed index
    was live. It records the rebuild with the obligation error, finalizes
    the request and posts the publication wake; ingest still derives the
    residual reading."""
    import _cctally_db
    import _cctally_store as st
    jr, J, core = _mods()
    _seed(jr, J)
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)
    with pytest.raises(_cctally_db.StatsHealDeferred):
        core.open_db()
    real_locked = jr._rebuild_stats_index_locked
    published = []

    def publish_then_record(*args, **kwargs):
        result = real_locked(*args, **kwargs)
        if kwargs.get("target_path") is None and not published:
            published.append(1)
            jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
        return result

    monkeypatch.setattr(jr, "_rebuild_stats_index_locked", publish_then_record)
    _losing_obligation_writes(monkeypatch)
    wakes = _recorded_wakes(monkeypatch)
    assert st.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0
    assert published, "non-vacuity: the healed index was published"
    assert _publication_wakes(wakes) == ["index-publication"], (
        "the healed index posted no completion wake")
    [event] = st.read_stats_heal_events()
    assert event["outcome"] == "rebuilt", event
    assert event["error"] == "ResidualObligationNotDurable", event
    assert st.STATS_HEAL_RESIDUAL_NOT_DURABLE in (
        st._stats_heal_log_path().read_text())
    assert st._read_stats_heal_request() is None, "the heal stayed owed"
    assert _percents() == [7.0]
    assert jr.run_stats_ingest(mode="authoritative").error is None
    assert _percents() == [7.0, 9.0]


def test_an_epoch_rebuild_that_lost_its_obligation_still_wakes(
        ns, no_spawn, monkeypatch):
    """The epoch worker's rebuild publishes a current index, then cannot
    record its residual obligation. It returned "error", kept its marker and
    posted no wake. It converges (the marker goes), logs the obligation
    failure and posts the publication wake."""
    import errno
    import types as _types
    import _cctally_completion
    import _cctally_store as st
    jr, J, core = _mods()
    _seed(jr, J)
    calls = []

    def publishing_resolve():
        calls.append(1)
        exc = _cctally_completion.ResidualObligationNotDurable(
            errno.ENOSPC, "injected obligation write failure")
        exc.result = _types.SimpleNamespace()
        raise exc

    monkeypatch.setattr(st, "stats_epoch_rebuild_pending", lambda *a: True)
    monkeypatch.setattr(st, "resolve_stats_epoch_mismatch", publishing_resolve)
    marker = []
    monkeypatch.setattr(st, "_unlink_stats_epoch_marker",
                        lambda: marker.append("unlinked"))
    wakes = _recorded_wakes(monkeypatch)
    assert st.cmd_stats_epoch_rebuild_internal(_types.SimpleNamespace()) == 0
    assert calls, "non-vacuity: the epoch rebuild ran"
    assert _publication_wakes(wakes) == ["index-publication"], (
        "the published epoch index posted no completion wake")
    assert marker == ["unlinked"], "the converged epoch kept its marker"
    log = st._stats_epoch_rebuild_log_path().read_text()
    assert st.STATS_EPOCH_RESIDUAL_NOT_DURABLE in log, log


def test_the_real_epoch_resolver_that_lost_its_obligation_still_wakes(
        ns, no_spawn, monkeypatch):
    """Fix round K12 (K11R-15): the epoch case above, driven through the
    REAL resolver. The index is one epoch behind, the worker's real
    `resolve_stats_epoch_mismatch` rebuilds and publishes, a reading lands
    after the publication, and only the obligation write fails (the failure
    is injected at the obligation file, below the completion). The worker
    converges, logs the obligation failure and posts the publication wake;
    ingest still derives the residual reading."""
    import sqlite3
    import _cctally_store as st
    jr, J, core = _mods()
    _seed(jr, J)
    conn = sqlite3.connect(core.DB_PATH)
    try:
        conn.execute(
            f"PRAGMA user_version = {core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()
    assert st.stats_epoch_rebuild_pending(), (
        "non-vacuity: the index is one epoch behind")
    real_locked = jr._rebuild_stats_index_locked
    published = []

    def publish_then_record(*args, **kwargs):
        result = real_locked(*args, **kwargs)
        if kwargs.get("target_path") is None and not published:
            published.append(1)
            jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
        return result

    monkeypatch.setattr(jr, "_rebuild_stats_index_locked", publish_then_record)
    _losing_obligation_writes(monkeypatch)
    wakes = _recorded_wakes(monkeypatch)
    assert st.cmd_stats_epoch_rebuild_internal(types.SimpleNamespace()) == 0
    assert published, "non-vacuity: the epoch rebuild published"
    assert _publication_wakes(wakes) == ["index-publication"], (
        "the published epoch index posted no completion wake")
    assert not st.stats_epoch_rebuild_pending(), "the epoch did not converge"
    log = st._stats_epoch_rebuild_log_path().read_text()
    assert st.STATS_EPOCH_RESIDUAL_NOT_DURABLE in log, log
    assert _percents() == [7.0]
    assert jr.run_stats_ingest(mode="authoritative").error is None
    assert _percents() == [7.0, 9.0]


def test_an_in_band_correction_that_lost_its_obligation_still_wakes(
        ns, no_spawn, monkeypatch):
    """The in-band correction recovery's rebuild publishes, then cannot
    record its residual obligation. It raised the generic recovery error
    ("run `cctally db rebuild --db stats` and retry") and posted no wake. It
    posts the publication wake with its locks released, and its error says
    the rebuild was published and the obligation was not saved."""
    import errno
    import _cctally_completion
    import _cctally_db
    jr, J, core = _mods()
    _seed(jr, J)
    signal = jr.CorrectionRebuildRequired(
        "injected completed correction", batch_id="batch-1",
        event_id="evt-1", high_water=jr.journal_safe_high_water(),
        expected_metadata=("meta",), recovery_eligible=True)
    monkeypatch.setattr(jr, "_correction_index_converged", lambda _s: False)
    monkeypatch.setattr(_cctally_db, "_would_block_prod_stats",
                        lambda _path: False)

    def publishing_rebuild(**_kwargs):
        raise _cctally_completion.ResidualObligationNotDurable(
            errno.ENOSPC, "injected obligation write failure")

    monkeypatch.setattr(jr, "rebuild_stats_index", publishing_rebuild)
    locks_free = []

    def wake(reason):
        # Posted with every lock released: maintenance EXCLUSIVE is free.
        import fcntl
        import os
        fd = os.open(core.STATS_LOCK_MAINTENANCE_PATH,
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            locks_free.append(reason)
        finally:
            os.close(fd)

    import _cctally_statusline
    monkeypatch.setattr(_cctally_statusline, "post_wake", wake)
    with pytest.raises(jr.CorrectionRecoveryError) as raised:
        jr._recover_completed_correction(
            signal, mode="authoritative", timeout_s=30.0)
    assert locks_free == ["index-publication"], (
        "the published correction rebuild posted no wake (or held its locks)")
    message = str(raised.value)
    assert "was published" in message, message
    assert "cctally db rebuild" not in message, message


def _legacy_interrupted_rebuild(core):
    """The legacy crash shape an open recovers: the live family moved into a
    complete prebuild-quarantine incident, and the killed rebuild's exact
    scratch family left beside the (now absent) destination."""
    import json
    import pathlib
    db = pathlib.Path(core.DB_PATH)
    incident_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    incident = core.APP_DIR / "quarantine" / (
        f"stats.db-{incident_at:%Y%m%dT%H%M%SZ}")
    incident.mkdir(parents=True)
    moved = []
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(f"{db}{suffix}")
        if member.exists():
            member.replace(incident / member.name)
            moved.append(member.name)
    (incident / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1,
        "quarantinedAtUtc": incident_at.isoformat().replace("+00:00", "Z"),
        "originalPath": str(db), "movedFiles": moved, "complete": True}))
    scratch_at = incident_at + dt.timedelta(seconds=1)
    scratch = db.parent / f"stats.db.rebuilding-{scratch_at:%Y%m%dT%H%M%S_%f}"
    scratch.write_bytes(b"")
    assert not db.exists()
    return scratch


def test_an_interrupted_rebuild_recovery_that_lost_its_obligation_still_wakes(
        ns, no_spawn, monkeypatch, capsys):
    """Fix round K12 (K11R-1). An open recovers a legacy interrupted rebuild;
    the recovery's rebuild publishes, then cannot record the residual
    obligation a reading appended meanwhile owes. The open reported the
    published index as a failed recovery, left the scratch family behind and
    posted no wake, so every later open rebuilt again. The cleanup completes,
    the open continues over the published index, the publication wake is
    posted once, and stderr says what was lost (message N1)."""
    import pathlib
    jr, J, core = _mods()
    _seed(jr, J)
    scratch = _legacy_interrupted_rebuild(core)
    real_locked = jr._rebuild_stats_index_locked
    published = []

    def publish_then_record(*args, **kwargs):
        result = real_locked(*args, **kwargs)
        if kwargs.get("target_path") is None and not published:
            published.append(1)
            jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
        return result

    monkeypatch.setattr(jr, "_rebuild_stats_index_locked", publish_then_record)
    _losing_obligation_writes(monkeypatch)
    wakes = _recorded_wakes(monkeypatch)
    conn = core.open_db()
    try:
        rows = sorted(r[0] for r in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"))
    finally:
        conn.close()
    assert published, "non-vacuity: the recovery published its index"
    assert rows == [7.0]
    assert _publication_wakes(wakes) == ["index-publication"], (
        "the recovered index posted no completion wake (or posted it twice)")
    assert not list(pathlib.Path(core.DB_PATH).parent.glob(
        "stats.db.rebuilding-*")), "the recovered scratch family was left"
    assert not scratch.exists()
    err = capsys.readouterr().err
    assert "interrupted-rebuild recovery failed" not in err, err
    assert (
        "cctally: stats.db was recovered from an interrupted rebuild, but the "
        "record that the inputs written during it still need deriving could "
        "not be saved (") in err, err
    assert (
        "). Those inputs stay in the journal and the next ingest derives "
        "them; check that the data directory is writable.") in err, err
    # A second open finds nothing left to recover: no further rebuild.
    published_before = len(published)
    monkeypatch.setattr(jr, "rebuild_stats_index", _no_rebuild)
    core.open_db().close()
    assert len(published) == published_before
    assert _publication_wakes(wakes) == ["index-publication"]
    # The residual reading is retained: ordinary ingest still derives it.
    assert jr.run_stats_ingest(mode="authoritative").error is None
    assert _percents() == [7.0, 9.0]


def _no_rebuild(**_kwargs):
    raise AssertionError("a recovered open rebuilt the index again")


_N2_HEAD = (
    "cctally: the corrected stats.db was published, but the record that the "
    "inputs written during the rebuild still need deriving could not be "
    "saved (")
_N2_TAIL = (
    "). Those inputs stay in the journal and the next ingest derives them; "
    "check that the data directory is writable.")


def _publishing_then_losing_rebuild(jr, monkeypatch):
    """The real rebuild publishes; its residual obligation is then lost, as
    `complete_and_rebuild` reports it (the exception carries the result)."""
    import errno
    import _cctally_completion
    real = jr.rebuild_stats_index
    published = []

    def rebuild(*args, **kwargs):
        result = real(*args, **kwargs)
        published.append(result)
        exc = _cctally_completion.ResidualObligationNotDurable(
            errno.ENOSPC, "injected obligation write failure")
        exc.result = result
        raise exc

    monkeypatch.setattr(jr, "rebuild_stats_index", rebuild)
    return published


def _cli(argv):
    parser = sys.modules["cctally"].build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


@pytest.mark.parametrize("as_json", [True, False])
def test_a_rederive_that_lost_its_obligation_reports_the_published_index(
        ns, monkeypatch, capsys, as_json):
    """Fix round K12 (K11R-2). `db rederive --yes` publishes the corrected
    index, then cannot record the residual obligation. It reported a failed
    apply, and because the failure unwound its lock context the holder wake
    was never posted. The lock context exits normally (wake posted), the
    command exits 3 with message N2, and the JSON failure envelope adds
    `indexPublished: true`."""
    import json
    jr, J, core = _mods()
    ns["open_cache_db"]().close()
    # A 0% reading needs no retained cost entries; its excluded legacy twin
    # makes the plan non-empty, so the apply rebuilds.
    reading = _obs(J, "2026-01-04T10:00:00Z", 0.0)
    jr.append_record(reading)
    _raw_append_tail([_legacy_snapshot_accept(J, reading)])
    published = _publishing_then_losing_rebuild(jr, monkeypatch)
    wakes = _recorded_wakes(monkeypatch)
    capsys.readouterr()
    rc = _cli(["db", "rederive", "--family", "claude-usage", "--yes"]
              + (["--json"] if as_json else []))
    out, err = capsys.readouterr()
    assert published, "non-vacuity: the corrected index was published"
    assert rc == 3
    assert "holder:rederive" in wakes, (
        "the rederive's lock context unwound without its holder wake")
    if as_json:
        payload = json.loads(out)
        assert payload["status"] == "failed", payload
        assert payload["indexPublished"] is True, payload
        assert payload["schemaVersion"] == 1
        return
    assert _N2_HEAD in err and _N2_TAIL in err, err
    assert "db rederive failed" not in err, err


@pytest.mark.parametrize("as_json", [True, False])
def test_a_journal_repair_that_lost_its_obligation_reports_the_published_index(
        ns, monkeypatch, capsys, as_json):
    """Fix round K12 (K11R-3). `db journal-repair --yes` appends its audit
    and publishes the rebuilt index, then cannot record the residual
    obligation. The JSON said plain failure. It keeps the `failed` status,
    exits 3, adds `indexPublished: true`, and text mode prints message
    N2."""
    import copy
    import json
    monkeypatch.setenv("CCTALLY_TEST_NO_ACCOUNT_BOUND_CUTOVER", "1")
    jr, J, core = _mods()
    at = "2026-01-04T09:00:00Z"
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:invalid-marker", family="claude-usage", at=at,
        actions=[{"action": "replace", "id": "sa:missing", "rev": 1, "at": at,
                  "payload": {"kind": "snapshot_accept",
                              "captured_at_utc": at,
                              "week_start_date": "2026-01-01",
                              "week_start_at": "2026-01-01T00:00:00Z",
                              "weekly_percent": 50.0, "source": "fixture",
                              "account_key": "unattributed"}}])
    divergent = copy.deepcopy(begin)
    divergent["protocol_extension"] = "divergent"
    jr.append_record(_obs(J, at, 7.0))
    _raw_append_tail([begin, divergent, action, commit])
    capsys.readouterr()
    assert _cli(["db", "journal-repair", "--json"]) == 0
    [violation] = json.loads(capsys.readouterr().out)[
        "unacknowledgedViolations"]
    published = _publishing_then_losing_rebuild(jr, monkeypatch)
    wakes = _recorded_wakes(monkeypatch)
    rc = _cli(["db", "journal-repair", "--violation",
               violation["fingerprint"], "--yes"]
              + (["--json"] if as_json else []))
    out, err = capsys.readouterr()
    assert published, "non-vacuity: the repaired index was published"
    assert rc == 3
    assert "holder:journal-repair" in wakes
    if as_json:
        payload = json.loads(out)
        assert payload["status"] == "failed", payload
        assert payload["indexPublished"] is True, payload
        assert payload["auditId"], payload
        return
    assert _N2_HEAD in err and _N2_TAIL in err, err


# --------------------------------------------------------------------------
# Fix round K11 (K9K10-R8): an unreadable obligation directory is owed work.
# --------------------------------------------------------------------------

def test_an_unreadable_obligation_directory_never_hides_owed_work(
        ns, no_spawn, monkeypatch):
    """`_obligation_paths` answered "no obligation" for ANY listing failure,
    so while the residual directory could not be listed every owed residual
    ingest was invisible: nothing was pending and nothing ran. Only a
    missing directory is empty. An unlistable one is owed and due, answered
    by one bounded ingest that discharges nothing (nothing was read) and
    backs off 60 s, doubling; a readable directory again discharges the
    covered obligation and ends the backoff."""
    import errno
    import os
    import _cctally_completion as C
    jr, J, _core = _mods()
    _seed(jr, J)
    published = jr.journal_safe_high_water()
    jr.append_record(_obs(J, "2026-01-04T10:00:00Z", 9.0))
    C.owe_residual_derivation(published,
                              residual_through=jr.journal_safe_high_water())
    residual = os.fspath(C.residual_dir())
    unreadable = [True]
    real_scandir = os.scandir

    def refusing_scandir(path=".", *args, **kwargs):
        if (unreadable[0] and not isinstance(path, int)
                and os.fspath(path) == residual):
            raise PermissionError(errno.EACCES, "injected listing failure",
                                  residual)
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", refusing_scandir)
    clock = [1_000_000.0]
    monkeypatch.setattr(C, "_now", lambda: clock[0])
    ingests = []
    real_ingest = jr.run_stats_ingest

    def counted_ingest(*args, **kwargs):
        ingests.append(1)
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(jr, "run_stats_ingest", counted_ingest)
    assert C.residual_derivation_owed(), "an unlistable directory hid its work"
    assert C.residual_derivation_due(clock[0])
    attempt = C.run_owed_residual_derivation()
    assert attempt.owed and attempt.disposition == "failed", attempt
    assert "injected listing failure" in attempt.reason
    assert ingests == [1], "the owed ingest never ran"
    assert _percents() == [7.0, 9.0], "the residual reading was not derived"
    # Bounded: the next attempt waits out a non-sliding 60 s backoff.
    assert not C.residual_derivation_due(clock[0] + 59)
    assert C.run_owed_residual_derivation().disposition == "backoff"
    assert ingests == [1]
    assert C.residual_derivation_due(clock[0] + 60)
    clock[0] += 60
    assert C.run_owed_residual_derivation().disposition == "failed"
    assert ingests == [1, 1]
    assert not C.residual_derivation_due(clock[0] + 119), (
        "the backoff did not double")
    assert C.residual_derivation_owed()
    # Readable again: the covered obligation is discharged, the backoff ends.
    unreadable[0] = False
    assert C.run_owed_residual_derivation().disposition == "discharged"
    assert not C.residual_derivation_owed()
    assert not C._unreadable_backoff_path().exists()
