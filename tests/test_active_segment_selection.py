"""Active-segment resolution across `week_reset_events` (#750 S3 B3).

Two read sites resolve "which segment of this week is the live one" by
selecting a `week_reset_events` row that matches the week's end instant.
Neither could express the requesting account, and they disagreed with each
other about ordering: `percent-breakdown` ordered on insertion `id` and
`_diff_resolve_anchor` had no `ORDER BY` at all, so with several rows sharing
one end it took whichever row SQLite returned first.

Both defects were latent while one week could hold at most one event. #750 S3
admits several, so both are reachable, and both now order on the semantic
cycle boundary — `unixepoch(effective_reset_at_utc)` — with `id` only as a
deterministic tie-breaker.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))


WEEK_END = "2026-06-12T15:00:00+00:00"
CUT_EARLY = "2026-06-08T09:00:00+00:00"
CUT_MID = "2026-06-09T09:00:00+00:00"
CUT_LATE = "2026-06-10T09:00:00+00:00"
CUT_OTHER_ACCOUNT = "2026-06-11T20:00:00+00:00"


def _stats_db(path: Path) -> None:
    """A stats.db holding four same-end events across two accounts.

    The winning row is written NEITHER first NOR last, on each of the two
    reads this module exercises: `acct-a`'s latest cut is `CUT_LATE` and
    `acct-a` also owns the first and the last row written, and the merged
    read's answer `CUT_OTHER_ACCOUNT` sits third of four. So an unordered
    scan (SQLite answers it in rowid order) and an `ORDER BY id DESC` both
    give a different answer from the instant ordering. Three rows are what
    that costs: with two, one of those two mutations always survives.
    """
    import _fixture_builders as fixtures

    fixtures.create_stats_db(path)
    conn = sqlite3.connect(path)
    try:
        for effective, account in (
            (CUT_EARLY, "acct-a"),
            (CUT_LATE, "acct-a"),
            (CUT_OTHER_ACCOUNT, "acct-b"),
            (CUT_MID, "acct-a"),
        ):
            fixtures.seed_week_reset_event(
                conn,
                detected_at_utc=effective,
                old_week_end_at=effective,
                new_week_end_at=WEEK_END,
                effective_reset_at_utc=effective,
                account_key=account,
            )
        # `acct-a` holds the LATEST capture, so a merged anchor read binds the
        # event lookup to `acct-a` — while `acct-b`'s row is the one an
        # instant-ordered merged read of `week_reset_events` would return.
        # That is what separates "the latest cut of the anchor's own account"
        # from "the latest cut of anyone's".
        for account, captured, pct in (
            ("acct-b", "2026-06-11T20:30:00Z", 8.0),
            ("acct-a", "2026-06-11T21:00:00Z", 12.0),
        ):
            fixtures.seed_weekly_usage_snapshot(
                conn,
                captured_at_utc=captured,
                week_start_date="2026-06-05",
                week_end_date="2026-06-12",
                week_start_at="2026-06-05T15:00:00Z",
                week_end_at=WEEK_END,
                weekly_percent=pct,
                account_key=account,
            )
        for account, email in (("acct-a", "a@example.test"),
                               ("acct-b", "b@example.test")):
            fixtures.seed_account(
                conn, account_key=account, provider="claude", email=email)
        # One milestone per segment, at the SAME threshold, so the rendered
        # row identifies which segment `cmd_percent_breakdown` resolved.
        for effective, account, cumulative in (
            (CUT_EARLY, "acct-a", 100.0),
            (CUT_MID, "acct-a", 77.0),
            (CUT_LATE, "acct-a", 12.0),
            (CUT_OTHER_ACCOUNT, "acct-b", 55.0),
        ):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id, "
                " account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "        (SELECT id FROM week_reset_events "
                "         WHERE effective_reset_at_utc = ?), ?)",
                (effective, "2026-06-05", "2026-06-12",
                 "2026-06-05T15:00:00Z", WEEK_END, 5, cumulative, None,
                 1, 1, effective, account),
            )
        conn.commit()
    finally:
        conn.close()


def _event_id_for(path: Path, effective: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(
            "SELECT id FROM week_reset_events WHERE effective_reset_at_utc = ?",
            (effective,),
        ).fetchone()[0])
    finally:
        conn.close()


@pytest.fixture()
def stats_path(tmp_path: Path) -> Path:
    path = tmp_path / "stats.db"
    _stats_db(path)
    return path


@pytest.fixture()
def opened_conns():
    """An `open_db` stand-in that closes every connection it handed out.

    The production code under test closes what IT opened, so a bare
    `lambda: sqlite3.connect(...)` seam leaves one live handle per call for
    the garbage collector to reclaim whenever it likes — which on a Windows
    or a `tmp_path`-cleanup path is a file still open when the directory goes.
    """
    opened: list[sqlite3.Connection] = []

    def _factory(path: Path):
        def _open(*_args, **_kwargs) -> sqlite3.Connection:
            conn = _row_conn(path)
            opened.append(conn)
            return conn
        return _open

    yield _factory
    for conn in opened:
        conn.close()


def test_percent_breakdown_resolves_the_latest_segment_for_the_account(
    stats_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, opened_conns,
) -> None:
    """`cmd_percent_breakdown` renders the LATEST cut of the asking account.

    Driven through the command rather than through
    `_latest_reset_event_for_end`, because the defect was at the CALL site:
    the command carried its own inline `WHERE new_week_end_at = ? ORDER BY id
    DESC LIMIT 1` with no account predicate. A test that calls the chokepoint
    directly stays green when that inline query comes back, so it pins the
    helper and not the behaviour the helper exists to correct.

    Ordering on insertion `id` answers "whichever row was written last", which
    is not the same question: a backfill can write an older reset after a
    newer one. `CUT_MID` was inserted last, so `id DESC` selects it and an
    unordered scan selects `CUT_EARLY`, the first written. And with no account
    predicate the answer can be another account's cut entirely — `acct-b`'s,
    which is later than any of `acct-a`'s.
    """
    import argparse
    import json

    ns = load_script()
    import _cctally_core
    import _cctally_percent_breakdown as pb

    monkeypatch.setattr(pb, "open_db", opened_conns(stats_path))
    monkeypatch.setattr(_cctally_core, "open_db", opened_conns(stats_path))
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})

    def _breakdown(account):
        args = argparse.Namespace(
            week_start=None, week_start_name=None, json=True, tz=None,
            account=account,
        )
        assert ns["cmd_percent_breakdown"](args) == 0
        return json.loads(capsys.readouterr().out)["milestones"]

    # `acct-a`: its own LATEST cut, so the $12.00 row — not the $100.00 row of
    # the cut written first, not the $77.00 row of the cut written last, and
    # not `acct-b`'s $55.00 row at all.
    assert [m["cumulativeCostUSD"] for m in _breakdown("acct-a")] == [12.0]
    assert [m["cumulativeCostUSD"] for m in _breakdown("acct-b")] == [55.0]


def test_latest_reset_event_for_end_orders_on_the_instant(
    stats_path: Path,
) -> None:
    """The chokepoint itself, at the boundaries the command cannot reach.

    The merged read must keep answering, a week end no event names must
    answer with None — the sentinel `0` the renderer falls back to for a
    pre-credit or uncredited week, whose milestones carry `reset_event_id = 0`
    — and `as_of_utc` must narrow the answer to segments already in effect,
    which is the question the milestone writer asks.
    """
    ns = load_script()
    resolve = ns["_latest_reset_event_for_end"]

    conn = _row_conn(stats_path)
    try:
        assert int(resolve(conn, WEEK_END, account_key="acct-a")["id"]) == (
            _event_id_for(stats_path, CUT_LATE))
        # The merged read answers with the latest cut of ANY account rather
        # than with the last row written.
        assert int(resolve(conn, WEEK_END, account_key=None)["id"]) == (
            _event_id_for(stats_path, CUT_OTHER_ACCOUNT))
        assert resolve(
            conn, "2026-05-01T15:00:00+00:00", account_key="acct-a") is None
        # `as_of_utc` narrows to segments already in effect, and still orders
        # among them on the instant: at an instant between the mid and the
        # late cut the answer is the mid one rather than the earliest, which
        # is what an unordered scan returns. This assertion does NOT also
        # discriminate `ORDER BY id DESC`, because `CUT_MID` is itself the
        # last row the fixture writes; the two assertions above, whose answer
        # is a cut written before it, are what pin that ordering.
        assert int(resolve(
            conn, WEEK_END, account_key="acct-a",
            as_of_utc="2026-06-09T18:00:00Z")["id"]) == (
                _event_id_for(stats_path, CUT_MID))
        assert resolve(
            conn, WEEK_END, account_key="acct-a",
            as_of_utc="2026-06-07T00:00:00Z") is None
    finally:
        conn.close()


def test_diff_anchor_override_takes_the_latest_cut_not_an_arbitrary_row(
    stats_path: Path, monkeypatch: pytest.MonkeyPatch, opened_conns,
) -> None:
    """`_diff_resolve_anchor` had no `ORDER BY`, so it took whichever row
    SQLite returned first for the window's end.

    With no `--account` the anchor snapshot is still chosen merged, but the
    event lookup binds to THAT snapshot's own account: reading another
    account's cut would move the window start to an instant the anchor's own
    account never reset at.
    """
    load_script()  # registers the sibling modules `_lib_diff_kernel` reaches
    import _lib_diff_kernel as dk

    monkeypatch.setattr(dk, "open_db", opened_conns(stats_path))
    now_utc = dt.datetime(2026, 6, 11, 0, 0, 0, tzinfo=dt.timezone.utc)

    # Merged: the anchor snapshot is acct-a's, so the override is acct-a's
    # LATEST cut. The unordered lookup returned acct-b's row instead, because
    # it is first by rowid and nothing ordered the result.
    start, end = dk._diff_resolve_anchor(now_utc)
    assert end == dt.datetime(2026, 6, 12, 15, 0, tzinfo=dt.timezone.utc)
    assert start == dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.timezone.utc)

    # Scoped to the other account: its own cut, and neither of acct-a's.
    start_b, _ = dk._diff_resolve_anchor(now_utc, account_key="acct-b")
    assert start_b == dt.datetime(2026, 6, 11, 20, 0, tzinfo=dt.timezone.utc)
    # The two reads disagree, which is what makes the scoping observable at
    # all: a predicate that happened to match everything would give one answer.
    assert start_b != start


def _row_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class _AnchorReached(BaseException):
    """Raised by the anchor spy so `cmd_diff` stops at the seam under test.

    A `BaseException` subclass, so no `except Exception` between the spy and
    the test can swallow it and turn a missed call into a passing assertion.
    """


def test_cmd_diff_threads_the_resolved_account_into_the_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cmd_diff` must pass the resolved account to `_diff_resolve_anchor`.

    `docs/accounts-gotchas.md` states the rule: an account-filtered read that
    builds its interval from the merged boundary set buckets one account's
    dollars into a window that belongs to neither account.

    Asserted on the CALL rather than on the source text. A `body.index(...) <
    body.index(...)` comparison passes for a `cmd_diff` that resolves the
    account early and then still calls `_diff_resolve_anchor()` with no
    `account_key`, which is the whole defect — the resolution has to REACH the
    anchor, not merely precede it.

    `resolve_account_filter` is stubbed because it fails closed on the entry
    cache (`needs_cache=True`) and this test is about what happens to its
    RESULT. The stub also keeps the assertion honest in the other direction: a
    `cmd_diff` that called the anchor before resolving the filter could not
    have the stub's value to pass.
    """
    ns = load_script()
    import _lib_diff_kernel as dk

    seen: dict = {}

    def _spy(now_utc, *, account_key=None):
        seen["account_key"] = account_key
        raise _AnchorReached

    monkeypatch.setattr(dk, "_diff_resolve_anchor", _spy)
    monkeypatch.setitem(
        ns, "resolve_account_filter", lambda *a, **k: ("acct-b", None))

    args = ns["build_parser"]().parse_args([
        "diff", "--a", "2026-05-01..2026-05-07",
        "--b", "2026-05-08..2026-05-14", "--account", "acct-b",
    ])
    with pytest.raises(_AnchorReached):
        ns["cmd_diff"](args)

    assert seen["account_key"] == "acct-b"


def test_cmd_report_threads_the_resolved_account_into_the_applier(
    stats_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cmd_report` must pass the resolved account to the weekrefs applier.

    It disambiguates the live segment from the synthesized earlier ones by
    `week_start_at`, so a merged applier read lets ANOTHER account's cut shift
    that value and the current-week row is misidentified. The call sat four
    lines above an already account-scoped `get_recent_weeks`, which is what
    makes the omission easy to miss on a read.

    `resolve_account_filter` is stubbed because it fails closed on the entry
    cache (`needs_cache=True`); the assertion is about what happens to its
    result. A `cmd_report` that reached the applier before resolving the
    filter could not have the stub's value to pass.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_forecast as fc

    seen: dict = {}

    def _spy(conn, refs, **kwargs):
        seen["account_key"] = kwargs.get("account_key", "MISSING")
        raise _AnchorReached

    monkeypatch.setattr(fc, "open_db", lambda *a, **k: _row_conn(stats_path))
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})
    monkeypatch.setitem(
        ns, "resolve_account_filter", lambda *a, **k: ("acct-b", None))
    monkeypatch.setitem(ns, "_apply_reset_events_to_weekrefs", _spy)

    args = ns["build_parser"]().parse_args(["report", "--account", "acct-b"])
    with pytest.raises(_AnchorReached):
        ns["cmd_report"](args)

    assert seen["account_key"] == "acct-b"


# --- the WRITER half of the same resolution (#750 S3, Unit B review) --------
#
# `maybe_record_milestone` STAMPS `percent_milestones.reset_event_id`;
# `cmd_percent_breakdown` READS it back through `_latest_reset_event_for_end`.
# The writer carried its own inline `ORDER BY id DESC` and the reader now
# orders on the instant, so the two named different segments of one week and
# the milestone rendered on neither.

W_WEEK_START_DATE = "2026-08-29"
W_WEEK_END_DATE = "2026-09-05"
W_WEEK_START_AT = "2026-08-29T00:00:00+00:00"
W_WEEK_END_AT = "2026-09-05T00:00:00+00:00"
W_EARLY = "2026-09-01T10:00:00+00:00"
W_MID = "2026-09-01T14:00:00+00:00"
W_LATE = "2026-09-01T18:00:00+00:00"


def _seed_two_segment_week(ns) -> "tuple[int, int]":
    """One account, one week end, three in-place cuts out of instant order.

    A backfill row landing after a live-detected one is what puts row id
    order and instant order out of step. The LATE cut — the one live at the
    capture below — is written NEITHER first NOR last, so neither an
    unordered scan nor an `ORDER BY id DESC` reaches it, and every cut is
    already in effect at the capture so the `<= captured_at` filter
    eliminates none of them.
    """
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, cost_usd, mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-09-01T19:00:00Z", W_WEEK_START_DATE, W_WEEK_END_DATE,
             W_WEEK_START_AT, W_WEEK_END_AT, 42.0, "auto"),
        )
        ids = {}
        for effective in (W_EARLY, W_LATE, W_MID):
            cur = conn.execute(
                "INSERT INTO week_reset_events "
                "(detected_at_utc, old_week_end_at, new_week_end_at, "
                " effective_reset_at_utc, observed_pre_credit_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                (effective, effective, W_WEEK_END_AT, effective, 40.0),
            )
            ids[effective] = int(cur.lastrowid)
        # The climb evidence the post-reset seed guard requires, captured
        # after the LATE cut so it is inside either candidate epoch.
        climb_ids = {}
        for captured, pct in (("2026-09-01T18:30:00Z", 1.0),
                              ("2026-09-01T19:30:00Z", 5.0)):
            cur = conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (captured, W_WEEK_START_DATE, W_WEEK_END_DATE,
                 W_WEEK_START_AT, W_WEEK_END_AT, pct, "test", "{}"),
            )
            climb_ids[captured] = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    assert min(ids.values()) < ids[W_LATE] < max(ids.values()), ids
    # The LATER climb snapshot, named by its capture instant rather than left
    # to whichever iteration the loop ended on: the caller records a milestone
    # `capturedAt` that instant, so a reordered seed loop would otherwise
    # silently attribute the milestone to the wrong snapshot.
    return ids[W_LATE], climb_ids["2026-09-01T19:30:00Z"]


def test_milestone_writer_stamps_the_segment_the_reader_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """The stamped `reset_event_id` is the LATEST cut, not the last written.

    Ordering on insertion `id` stamped the cut written last while
    `cmd_percent_breakdown` filters on the latest one. The milestone then
    belongs to a segment no reader asks for and renders nowhere, which is a
    silent loss rather than a wrong number.
    """
    import argparse
    import json

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    late_id, usage_id = _seed_two_segment_week(ns)

    ns["maybe_record_milestone"]({
        "id": usage_id,
        "weeklyPercent": 5.0,
        "weekStartDate": W_WEEK_START_DATE,
        "weekEndDate": W_WEEK_END_DATE,
        "weekStartAt": W_WEEK_START_AT,
        "weekEndAt": W_WEEK_END_AT,
        "fiveHourPercent": None,
        "capturedAt": "2026-09-01T19:30:00Z",
    })

    conn = _row_conn(ns["DB_PATH"])
    try:
        stamped = [(int(r["percent_threshold"]), int(r["reset_event_id"]))
                   for r in conn.execute(
                       "SELECT percent_threshold, reset_event_id "
                       "FROM percent_milestones ORDER BY percent_threshold")]
    finally:
        conn.close()
    assert stamped == [(5, late_id)], stamped

    # And the reader finds it, which is the user-visible half.
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})
    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=True, tz=None,
        account=None,
    )
    assert ns["cmd_percent_breakdown"](args) == 0
    rendered = json.loads(capsys.readouterr().out)["milestones"]
    assert [m["percentThreshold"] for m in rendered] == [5], rendered
