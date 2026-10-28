"""#825/#832 Task 1.4 — admission before interpretation (spec 5.3, 8; F4, F7).

After the account-bound cutover, an unstamped record past the historical
boundary is excluded from current accounting by ONE predicate every consumer
applies before it interprets anything: live ingest, rebuild, rederive,
journal-repair, correction selection and the deep doctor scan. An excluded
record keeps its positional slot, so cursors, sequence numbers and raw-prefix
hashes are unchanged; its count feeds the disposable admission summary.

Legacy records here are built with `_lib_journal.unstamped_record`, the exact
shape the previous release writes, and appended raw (the current append APIs
always stamp).
"""
import datetime as dt
import json

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
SEGMENT = "observations-2026-10.jsonl"
EARLIER = "observations-2026-09.jsonl"
ACCOUNT = "claude:acct-a"
CODEX_ACCOUNT = "codex:acct-x"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_journal as jr
    monkeypatch.setattr(jr, "_utc_now", lambda: FIXED)
    jr.PIPELINE.append(_snapshot_accept_hook(jr))
    return ns


def _mods():
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal


def _snapshot_accept_hook(jr):
    """Synthetic Model-A deriver (as in test_journal_ingest): one
    `snapshot_accept` per Claude obs, stamped with the obs account."""
    def hook(ctx, rec):
        if rec.get("t") != "obs" or rec.get("provider") != "claude":
            return
        p = rec["payload"]
        jr.emit_model_a(
            ctx, kind="snapshot_accept", evt_id="sa:" + rec["id"],
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": rec["at"],
                "week_start_date": "2026-10-05",
                "week_end_date": "2026-10-12",
                "week_start_at": "2026-10-05T00:00:00+00:00",
                "week_end_at": "2026-10-12T00:00:00+00:00",
                "weekly_percent": float(p["weekly_percent"]),
                "source": "statusline",
                "payload_json": "{}",
                "account_key": rec.get("account") or "unattributed",
            },
            at=ctx.as_of_for(rec))
    return hook


def _usage_obs(J, pct, at, account=ACCOUNT):
    return J.make_obs(at=at, src="statusline", provider="claude",
                      payload={"weekly_percent": pct}, account=account)


def _raw_append(segment, records):
    """Append exactly these bytes (a legacy writer bypasses the stamp)."""
    _, J = _mods()
    jdir = _cctally_core.JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    with open(jdir / segment, "ab") as fh:
        for record in records:
            fh.write(J.encode_line(record))


def _rows(sql):
    conn = _cctally_core.open_db()
    try:
        return [tuple(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _percents():
    return sorted(r[0] for r in _rows(
        "SELECT weekly_percent FROM weekly_usage_snapshots"))


def _summary():
    rows = _rows(
        "SELECT cutover_id, scanned_high_water, excluded_total, "
        "family_counts_json, first_excluded_coordinate, "
        "latest_excluded_coordinate, coordinate_digest, complete_through "
        "FROM journal_admission_summary")
    return rows[0] if rows else None


def _rebuild():
    jr, _ = _mods()
    res = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert res is not None


def _seed_with_cutover():
    """Current history, then the cutover, then one current reading."""
    jr, J = _mods()
    jr.append_record(J.make_account_observe(
        at="2026-10-09T09:00:00Z", account_key=ACCOUNT, provider="claude"))
    jr.append_record(_usage_obs(J, 10.0, "2026-10-09T09:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")
    boundary = jr.ensure_account_bound_cutover()
    jr.append_record(_usage_obs(J, 20.0, "2026-10-09T10:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")
    return boundary


# --------------------------------------------------------------------------
# Claude observations and derived events
# --------------------------------------------------------------------------

def test_excluded_observation_is_invisible_after_ingest_rebuild_and_rederive(ns):
    jr, J = _mods()
    _seed_with_cutover()
    assert _percents() == [10.0, 20.0]

    legacy_obs = J.unstamped_record(_usage_obs(J, 70.0, "2026-10-09T11:00:00Z"))
    legacy_evt = J.unstamped_record(J.make_evt(
        kind="snapshot_accept", id="sa:" + legacy_obs["id"],
        at="2026-10-09T11:00:00Z",
        payload={"captured_at_utc": "2026-10-09T11:00:00Z",
                 "week_start_date": "2026-10-05",
                 "week_end_date": "2026-10-12",
                 "weekly_percent": 70.0, "source": "statusline",
                 "payload_json": "{}", "account_key": ACCOUNT}))
    _raw_append(SEGMENT, [legacy_obs, legacy_evt])
    hw = jr.journal_high_water()

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.error is None
    assert _percents() == [10.0, 20.0]
    # The cursor advanced across the excluded lines.
    cursor = _rows("SELECT segment, offset FROM journal_cursor WHERE id = 1")[0]
    assert cursor == hw

    _rebuild()
    assert _percents() == [10.0, 20.0]

    import _cctally_rederive as rederive
    records, _hw, _ends, _evidence = rederive.read_rederive_journal_prefix(hw)
    ids = [r.get("id") for r in records]
    assert legacy_obs["id"] not in ids
    assert legacy_evt["id"] not in ids
    assert sum(1 for r in records if r.get("t") == "excluded") == 2


def test_stamped_post_boundary_records_are_admitted(ns):
    jr, J = _mods()
    _seed_with_cutover()
    jr.append_record(_usage_obs(J, 30.0, "2026-10-09T11:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")
    _rebuild()
    assert _percents() == [10.0, 20.0, 30.0]


# --------------------------------------------------------------------------
# F7: old corrections targeting current facts
# --------------------------------------------------------------------------

def test_excluded_correction_batch_never_reaches_correction_selection(ns):
    jr, J = _mods()
    _seed_with_cutover()
    sa = _rows("SELECT journal_id FROM weekly_usage_snapshots "
               "WHERE weekly_percent = 20.0")[0][0]
    legacy = J.legacy_writer_view()
    batch = legacy.make_correction_batch(
        batch_id="rederive:claude-usage:legacy", family="claude-usage",
        at="2026-10-09T11:00:00Z",
        actions=[{"action": "tombstone", "id": sa, "rev": 1,
                  "at": "2026-10-09T11:00:00Z", "payload": None}])
    _raw_append(SEGMENT, batch)
    hw = jr.journal_high_water()

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.error is None
    assert _percents() == [10.0, 20.0]
    selection = jr._full_effective_selection(hw)
    assert "rederive:claude-usage:legacy" not in selection.completed_batches
    _rebuild()
    assert _percents() == [10.0, 20.0]

    import _cctally_journal_repair as repair
    snapshot = repair._selection_snapshot()
    assert "rederive:claude-usage:legacy" not in (
        snapshot.selection.completed_batches)


# --------------------------------------------------------------------------
# F4: earlier segment and a later bootstrap segment
# --------------------------------------------------------------------------

def test_appends_into_an_earlier_or_new_bootstrap_segment_are_excluded(ns):
    jr, J = _mods()
    _raw_append(EARLIER, [J.unstamped_record(
        _usage_obs(J, 5.0, "2026-09-20T09:00:00Z"))])
    _seed_with_cutover()
    assert _percents() == [5.0, 10.0, 20.0]

    def legacy_budget(threshold):
        return J.unstamped_record(J.make_evt(
            kind="budget", id=f"bm:test:{threshold}",
            at="2026-10-09T11:00:00Z",
            payload={"vendor": "claude", "period_start_at": "2026-10-01",
                     "period": "monthly", "threshold": threshold,
                     "budget_usd": 1.0, "spent_usd": 1.0,
                     "consumption_pct": 100.0,
                     "crossed_at_utc": "2026-10-09T11:00:00Z",
                     "account_key": ACCOUNT}))

    # A legacy process appending into an EARLIER segment after the cutover…
    _raw_append(EARLIER, [legacy_budget(50)])
    # …and a bootstrap segment created after the cutover (sorts first).
    _raw_append("bootstrap-20260101T000000_000000.jsonl", [legacy_budget(75)])
    # A stamped record in the same earlier segment is admitted.
    stamped = J.make_evt(
        kind="budget", id="bm:test:90", at="2026-10-09T11:00:00Z",
        payload={"vendor": "claude", "period_start_at": "2026-10-01",
                 "period": "monthly", "threshold": 90, "budget_usd": 1.0,
                 "spent_usd": 1.0, "consumption_pct": 100.0,
                 "crossed_at_utc": "2026-10-09T11:00:00Z",
                 "account_key": ACCOUNT})
    _raw_append(EARLIER, [stamped])

    _rebuild()
    thresholds = sorted(r[0] for r in _rows(
        "SELECT threshold FROM budget_milestones"))
    assert thresholds == [90]
    assert _percents() == [5.0, 10.0, 20.0]


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def test_summary_counts_by_family_and_survives_a_rebuild(ns):
    jr, J = _mods()
    _seed_with_cutover()
    assert _summary()[2] == 0
    legacy_obs = J.unstamped_record(_usage_obs(J, 70.0, "2026-10-09T11:00:00Z"))
    legacy_op = J.unstamped_record(J.make_op(
        at="2026-10-09T11:00:00Z", src="sync-week",
        payload={"kind": "sync_week"}))
    _raw_append(SEGMENT, [legacy_obs, legacy_op])
    jr.run_stats_ingest(mode="authoritative")
    live = _summary()
    assert live[0] == "account-bound-cutover-v1"
    assert live[2] == 2
    assert json.loads(live[3]) == {"claude-observation": 1, "claude-op": 1}
    assert live[6] is not None and live[6].startswith("sha256:")
    assert live[7] is not None  # complete through its frontier
    first = json.loads(live[4])
    latest = json.loads(live[5])
    assert first["segment"] == latest["segment"] == SEGMENT
    assert first["offset"] < latest["offset"]

    _rebuild()
    assert _summary() == live


def test_no_cutover_admits_everything_and_counts_nothing(ns):
    jr, J = _mods()
    _raw_append(SEGMENT, [J.unstamped_record(
        _usage_obs(J, 15.0, "2026-10-09T09:00:00Z"))])
    jr.run_stats_ingest(mode="authoritative")
    assert _percents() == [15.0]
    summary = _summary()
    assert summary[0] is None and summary[2] == 0


# --------------------------------------------------------------------------
# accounts: Codex admitted, last-seen unaffected
# --------------------------------------------------------------------------

def test_codex_account_observe_is_admitted_and_claude_last_seen_unaffected(ns):
    jr, J = _mods()
    _seed_with_cutover()
    before = _rows("SELECT account_key, last_seen_utc FROM accounts "
                   "WHERE account_key = ?".replace("?", f"'{ACCOUNT}'"))
    _raw_append(SEGMENT, [
        J.unstamped_record(J.make_account_observe(
            at="2026-10-09T11:30:00Z", account_key=CODEX_ACCOUNT,
            provider="codex", email="x@example.test")),
        J.unstamped_record(J.make_account_observe(
            at="2026-10-09T11:45:00Z", account_key=ACCOUNT,
            provider="claude")),
        J.unstamped_record(_usage_obs(J, 71.0, "2026-10-09T11:50:00Z")),
    ])
    jr.run_stats_ingest(mode="authoritative")
    for _phase in ("ingest", "rebuild"):
        keys = [r[0] for r in _rows("SELECT account_key FROM accounts")]
        assert CODEX_ACCOUNT in keys
        after = _rows("SELECT account_key, last_seen_utc FROM accounts "
                      "WHERE account_key = ?".replace("?", f"'{ACCOUNT}'"))
        assert after == before
        _rebuild()


# --------------------------------------------------------------------------
# review cycle 7 825-832-CD3-J02: an incomplete boundary lookup never
# disables admission
# --------------------------------------------------------------------------

#: The functions whose own `os.stat` calls size the cutover lookup's ranges
#: (`getsize` is `_segment_sizes`'s).
_LOOKUP_STAT_SITES = frozenset({"getsize", "_whole_prefix_admission_boundary"})


@pytest.mark.parametrize("mode", ["read", "stat"])
def test_a_transient_boundary_lookup_failure_never_admits_legacy_records(
        ns, monkeypatch, mode):
    """The coordinate cache is unusable and the rebuild's boundary discovery
    cannot read (or stat) the segment holding the cutover, while its later
    traversal reads it fine. Treating the failed lookup as "no cutover"
    disabled the admission filter and folded the post-cutover legacy 70 into
    the published index. The real `db rebuild --db stats` must abort without
    publishing, and once reads recover it still excludes the legacy record
    under the one original boundary."""
    import argparse
    import errno
    import os
    import pathlib
    import sys
    jr, J = _mods()
    first = _seed_with_cutover()
    legacy_obs = J.unstamped_record(_usage_obs(J, 70.0, "2026-10-09T11:00:00Z"))
    legacy_evt = J.unstamped_record(J.make_evt(
        kind="snapshot_accept", id="sa:" + legacy_obs["id"],
        at="2026-10-09T11:00:00Z",
        payload={"captured_at_utc": "2026-10-09T11:00:00Z",
                 "week_start_date": "2026-10-05",
                 "week_end_date": "2026-10-12",
                 "weekly_percent": 70.0, "source": "statusline",
                 "payload_json": "{}", "account_key": ACCOUNT}))
    _raw_append(SEGMENT, [legacy_obs, legacy_evt])
    assert jr.run_stats_ingest(mode="authoritative").error is None
    assert _percents() == [10.0, 20.0]
    journal_before = b"".join(
        (_cctally_core.JOURNAL_DIR / s).read_bytes() for s in jr.list_segments())

    discovering = [False]

    def failure(path):
        return OSError(errno.EIO, "injected transient journal I/O failure",
                       str(path))

    real_open = jr._open_segment_for_read
    real_stat = os.stat

    def refusing_open(seg_path):
        if (discovering[0] and mode == "read"
                and pathlib.Path(seg_path).name == SEGMENT):
            raise failure(seg_path)
        return real_open(seg_path)

    def refusing_stat(path, *args, **kwargs):
        # The stat sites of the lookup itself; the listing is not this case.
        if (discovering[0] and mode == "stat" and not isinstance(path, int)
                and pathlib.Path(os.fspath(path)).name == SEGMENT
                and sys._getframe(1).f_code.co_name in _LOOKUP_STAT_SITES):
            raise failure(path)
        return real_stat(path, *args, **kwargs)

    real_discovery = jr._whole_prefix_admission_boundary
    discoveries = []

    def discovery():
        # The cache is absent at every discovery, and only discovery fails:
        # the rebuild's own traversal afterwards reads every byte.
        _cctally_core.ACCOUNT_BOUND_CUTOVER_CACHE_PATH.unlink(missing_ok=True)
        discovering[0] = True
        try:
            return real_discovery()
        finally:
            discovering[0] = False
            discoveries.append(1)

    monkeypatch.setattr(jr, "_open_segment_for_read", refusing_open)
    monkeypatch.setattr(os, "stat", refusing_stat)
    monkeypatch.setattr(jr, "_whole_prefix_admission_boundary", discovery)
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True))
    assert discoveries, "non-vacuity: the rebuild discovered its boundary"
    assert _percents() == [10.0, 20.0], (
        "a failed boundary lookup admitted the post-cutover legacy record")
    assert rc != 0, "the rebuild reported success without its boundary"
    journal_after = b"".join(
        (_cctally_core.JOURNAL_DIR / s).read_bytes() for s in jr.list_segments())
    assert journal_after == journal_before

    # Reads recover: one original boundary, the legacy record still excluded.
    monkeypatch.setattr(jr, "_whole_prefix_admission_boundary", real_discovery)
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True))
    assert rc == 0
    assert _percents() == [10.0, 20.0]
    assert jr.find_account_bound_cutover() == first
    ops = [line for s in jr.list_segments()
           for line in (_cctally_core.JOURNAL_DIR / s).read_bytes().splitlines()
           if b'"account_bound_cutover"' in line]
    assert len(ops) == 1


# --------------------------------------------------------------------------
# K9K10-R9 / R11 (fix round K11): an unreadable segment is never absence
# --------------------------------------------------------------------------

def test_an_unsizable_segment_never_hides_the_accounts_cutover_op(
        ns, monkeypatch):
    """K9K10-R9: `find_accounts_cutover_op` skipped a segment it could not
    size, so it answered None although the op exists — a duplicate op
    append, or an `unattributed` fallback during a rebuild. A stat failure
    other than a vanished segment must raise."""
    import errno
    import os
    import sys
    jr, _J = _mods()
    jr.append_accounts_cutover_op(ACCOUNT)
    assert jr.find_accounts_cutover_op() == ACCOUNT
    real_stat = os.stat

    def refusing_stat(path, *args, **kwargs):
        if (not isinstance(path, int)
                and os.path.basename(os.fspath(path)) == SEGMENT
                and sys._getframe(2).f_code.co_name
                == "find_accounts_cutover_op"):
            raise OSError(errno.EIO, "injected stat failure", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", refusing_stat)
    with pytest.raises(OSError):
        jr.find_accounts_cutover_op()
    monkeypatch.setattr(os, "stat", real_stat)
    assert jr.find_accounts_cutover_op() == ACCOUNT


@pytest.mark.parametrize("handler", ["cache-029", "conversations-005"])
def test_an_unreadable_cutover_lookup_defers_the_account_backfill(
        ns, monkeypatch, handler):
    """K9K10-R9: a raising account-bound (or accounts) cutover lookup inside
    an account backfill migration reached the dispatcher as an ordinary
    exception: logged, banner, and the registry stopped. It is transient, so
    the migration must defer (`MigrationGateNotMet`) and stay pending."""
    import errno
    import sqlite3
    import _cctally_db
    jr, _J = _mods()

    def unreadable():
        raise jr.AccountBoundCutoverUnavailable(
            errno.EIO, "injected unreadable journal", "segment")

    monkeypatch.setattr(jr, "find_accounts_cutover_op", unreadable)
    conn = sqlite3.connect(":memory:")
    if handler == "cache-029":
        conn.execute("CREATE TABLE session_entries (account_key TEXT)")
        conn.execute("INSERT INTO session_entries VALUES (NULL)")
        run = _cctally_db._029_backfill_claude_account
    else:
        conn.execute("CREATE TABLE conversation_messages (account_key TEXT)")
        conn.execute("INSERT INTO conversation_messages VALUES (NULL)")
        monkeypatch.setattr(
            _cctally_db, "_acquire_conversations_db_claude_provider_flock",
            lambda conn, migration: [])
        monkeypatch.setattr(
            _cctally_db, "_acquire_conversations_db_codex_provider_flock",
            lambda conn, migration: [])
        run = _cctally_db._conv_005_conversation_account_dimension
    try:
        with pytest.raises(_cctally_db.MigrationGateNotMet):
            run(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM "
            + ("session_entries" if handler == "cache-029"
               else "conversation_messages")
            + " WHERE account_key IS NULL").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_unreadable_segment_tail_never_undercounts_the_excluded_population(
        ns, monkeypatch):
    """K9K10-R11: `excluded_population` sized each segment with
    `_complete_line_offset`, whose swallowed tail-read failure answered 0 and
    so skipped the segment: an undercount whose new digest restarts the
    legacy-writer age. An unreadable segment must raise (unavailable)."""
    import builtins
    import errno
    import os
    import sys
    jr, J = _mods()
    boundary = _seed_with_cutover()
    _raw_append(SEGMENT, [J.unstamped_record(
        _usage_obs(J, 70.0, "2026-10-09T11:00:00Z"))])
    assert len(jr.excluded_population(boundary)) == 1

    def refusing_open(file, *args, **kwargs):
        if (not isinstance(file, int)
                and os.path.basename(os.fspath(file)) == SEGMENT
                and sys._getframe(1).f_code.co_name
                == "_complete_line_offset"):
            raise OSError(errno.EIO, "injected tail read failure",
                          os.fspath(file))
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(jr, "open", refusing_open, raising=False)
    with pytest.raises(OSError):
        jr.excluded_population(boundary)


# --------------------------------------------------------------------------
# An excluded legacy decision over a current reading (#825/#832 review P1-1;
# the M20 oracle's `old-ingest-over-current` schedule, extracted)
# --------------------------------------------------------------------------

def _readings_at(app, percent):
    conn = app.open_db()
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT account_key, weekly_percent FROM weekly_usage_snapshots "
            "WHERE weekly_percent = ?", (percent,))]
    finally:
        conn.close()


def test_an_excluded_legacy_decision_never_suppresses_the_current_reading(
        monkeypatch, tmp_path):
    """Detects the #825/#832 P1-1 data loss: a stamped current reading whose
    ingest was deferred, then derived by a previous-release ingest that
    journaled an UNSTAMPED `sa:<observation id>` decision. Admission excludes
    that decision and a rebuild applies only admitted decisions, so before the
    derivation-completion step the reading vanished from the index on the
    next rebuild and never came back. It must materialize exactly once, under
    the account it was captured for, through ingest, rebuild and rederive.

    Runs through the real bound recording pipeline (`record_usage_bound`) and
    the real `db rebuild` / `db rederive` commands; the previous release's
    ingest is the current one with the writer stamp withheld."""
    import _outcome_quarantine_trace as T

    app = T.make_app(monkeypatch, tmp_path)
    # After make_app: loading the script imports these modules afresh.
    import _cctally_journal as jr
    import _lib_account_protocol as P
    import _lib_journal as J
    T.retain_session_input(app, "A")
    captured = T.identity("A")
    end = "2026-10-12T12:00:00Z"
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=30,
              week_end=end)
    # The current reading's ingest is deferred (it found an old-epoch index).
    with monkeypatch.context() as deferred:
        deferred.setattr(jr, "run_stats_ingest", lambda **_kw: None)
        T.current(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=32,
                  week_end=end)
    [reading] = [r for r in T.journal_records()
                 if r.get("t") == "obs" and r.get("provider") == "claude"
                 and (r.get("payload") or {}).get("weekly_percent") == 32]
    assert P.is_stamped(reading) and reading.get("account") == captured
    # The previous release's ingest derives it, stamping nothing, into the
    # index it republished at its own epoch, which certifies no derivation.
    with monkeypatch.context() as old_release:
        old_release.setattr(J, "prepare_record", J.unstamped_record)
        jr.run_stats_ingest(mode="authoritative")
    import sqlite3
    old_index = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        old_index.execute(
            f"PRAGMA user_version = {jr.DERIVATION_FRONTIER_EPOCH - 1}")
        old_index.commit()
    finally:
        old_index.close()
    decision = "sa:" + reading["id"]
    assert any(r.get("t") == "evt" and r.get("id") == decision
               and not P.is_stamped(r) for r in T.journal_records()), (
        "non-vacuity: the excluded legacy decision exists")

    T.rebuild(app)              # the current epoch transition's rebuild
    assert _readings_at(app, 32.0) == [(captured, 32.0)], "after rebuild"
    T.ingest(app)
    assert _readings_at(app, 32.0) == [(captured, 32.0)], "after ingest"
    T.rebuild(app)
    assert _readings_at(app, 32.0) == [(captured, 32.0)], "after a rerun"
    T.rederive(app)
    assert _readings_at(app, 32.0) == [(captured, 32.0)], "after rederive"
    assert _readings_at(app, 30.0) == [(captured, 30.0)]
