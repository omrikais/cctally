"""#948 (spec 7, T9) — the doctor leg `accounting.outcome_quarantine`.

Rewritten from the #825/#832 F16 certificate cases. Per account and per limit
the leg renders the journal-derived recovery barrier in the operator-approved
spec 7.3 wording: WARN while any limit of any account is paused, naming per
limit what it waits for (with a "not before" lower bound only when one exists
and is still ahead); OK otherwise, carrying the recovered note for seven days
after an account's last limit recovered; unavailable — never OK — on an
unvalidated index. `journal.legacy_writer` is OK after seven quiet days while
this leg may still WARN. `--json` carries the same states.
"""
import datetime as dt
import json

import pytest

import _outcome_quarantine_trace as T

#: Spec 7.3, operator-approved 2026-11-01 ("Per-limit detail", "Note for 7
#: days"), verbatim.
PAUSED_TEXT = (
    "Automatic credit, early-reset and status-line drop detection is paused "
    "for {account} since {time} because {reason}. It resumes on its own, "
    "separately for each limit. Usage recording continues; no command is "
    "needed. If your usage was reset in place meanwhile, you can record it "
    "with `cctally record-credit`.")
PAUSE_REASONS = {
    "legacy-spool-activity":
        "an older cctally process wrote status-line data after the upgrade",
    "inventory-incomplete":
        "cctally could not read the status-line files an older cctally "
        "release used",
    "excluded-write": "an older cctally process wrote data after the upgrade",
}
RECOVERED_TEXT = ("Automatic detection resumed for {account} on {time} after "
                  "a pause {reason}.")
RECOVERED_REASONS = {
    "legacy-spool-activity": "caused by an older cctally process",
    "excluded-write": "caused by an older cctally process",
    "inventory-incomplete":
        "while cctally could not read the status-line files an older cctally "
        "release used",
}
REFRESH = "paused - waiting for an automatic usage refresh"
UNAVAILABLE_SUMMARY = "unavailable (no validated stats index)"
UNAVAILABLE_REMEDIATION = ("Run any cctally command to bring the stats index "
                           "up to date, then run `cctally doctor` again.")

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"
TRIGGER_AT = "2026-10-09T10:05:00Z"
W_END = "2026-10-19T13:00:00Z"
W_FIVE = "2026-10-12T18:00:00Z"
WITNESS_AT = "2026-10-12T13:00:00Z"
B_WITNESS_AT = "2026-10-12T13:01:00Z"
NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)


def paused_message(account, since, cause, five, weekly):
    return "\n".join([
        PAUSED_TEXT.format(account=account, time=since,
                           reason=PAUSE_REASONS[cause]),
        f"five-hour: {five}", f"weekly: {weekly}"])


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _leg(app, now=None):
    import _cctally_doctor
    import _lib_doctor
    state = _cctally_doctor._gather_outcome_quarantine()
    from dataclasses import fields
    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(outcome_quarantine=state, now_utc=now or NOW)
    return _lib_doctor._check_accounting_outcome_quarantine(
        _lib_doctor.DoctorState(**kwargs))


def _rows(result):
    return {row["accountLabel"]: row for row in result.details["accounts"]}


def _json_leg(app):
    import contextlib
    import io
    args = app.build_parser().parse_args(["doctor", "--json"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        args.func(args)
    report = json.loads(out.getvalue())
    checks = [c for cat in report["categories"] for c in cat["checks"]
              if c["id"] == "accounting.outcome_quarantine"]
    assert len(checks) == 1
    return checks[0]


def _states(result):
    """The per-account states, without the time-dependent message."""
    details = result["details"] if isinstance(result, dict) else result.details
    return [{k: v for k, v in row.items() if k != "message"}
            for row in details["accounts"]]


def _seed(app, monkeypatch):
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0, account="A")


def _trigger(app, monkeypatch):
    T.legacy(app, monkeypatch, at=TRIGGER_AT, weekly=50, week_end=E0,
             five=60, five_end=F0)
    T.ingest(app)


def _cover(app, monkeypatch):
    """The automatic OAuth reconciliation after the trigger: both axes."""
    T.oauth(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=11,
            week_end=E0, five=31, five_end=F0)


def _witness(app, monkeypatch):
    """The first entry of a fresh weekly period and five-hour window."""
    T.current(app, monkeypatch, at=WITNESS_AT, weekly=5, week_end=W_END,
              five=5, five_end=W_FIVE)


# --------------------------------------------------------------------------
# the rendering kernel: every line, cause, bound and the seven-day note
# --------------------------------------------------------------------------

def _row(cause="excluded-write", five=None, weekly=None):
    paused = {"state": "paused", "waitingFor": "usage-refresh",
              "notBefore": None, "since": TRIGGER_AT, "recoveredAt": None,
              "revocation": None}
    return {"account": "acct-a", "accountLabel": "A", "cause": cause,
            "axes": {"fiveHour": {**paused, **(five or {})},
                     "sevenDay": {**paused, **(weekly or {})}}}


@pytest.mark.parametrize("five,weekly,lines", [
    ({"waitingFor": "inventory"}, {"waitingFor": "inventory"},
     ("paused - waiting until cctally can read /data/legacy.json",
      "paused - waiting until cctally can read /data/legacy.json")),
    ({"waitingFor": "expiry"}, {"waitingFor": "usage-refresh"},
     (REFRESH, REFRESH)),
    ({"waitingFor": "fresh-period", "notBefore": "2026-10-09T14:00:00Z"},
     {"waitingFor": "fresh-period", "notBefore": "2026-10-12T12:00:00Z"},
     ("paused - waiting for the next five-hour window (not before "
      "2026-10-09T14:00:00Z)",
      "paused - waiting for the next weekly period (not before 2026-10-12)")),
    # Spec 7.1: the clause is omitted when the bound is null or already past.
    ({"waitingFor": "fresh-period", "notBefore": None},
     {"waitingFor": "fresh-period", "notBefore": "2026-10-09T11:00:00Z"},
     ("paused - waiting for the next five-hour window",
      "paused - waiting for the next weekly period")),
    ({"state": "recovered", "waitingFor": None,
      "recoveredAt": "2026-10-09T11:30:00Z"},
     {"waitingFor": "fresh-period", "notBefore": "2026-10-12T12:00:00Z"},
     ("resumed 2026-10-09T11:30:00Z",
      "paused - waiting for the next weekly period (not before 2026-10-12)")),
], ids=["inventory", "refresh", "not-before", "bound-null-or-past",
        "partial-recovery"])
def test_each_limit_line_is_rendered_in_the_approved_wording(five, weekly, lines):
    """Detects a limit line that names the wrong wait, an estimated or past
    "not before" time, or a recovered limit still shown paused."""
    import _lib_doctor
    message = _lib_doctor._outcome_quarantine_message(
        _row(five=five, weekly=weekly), now=NOW,
        inventory_path="/data/legacy.json")
    assert message == paused_message("A", TRIGGER_AT, "excluded-write", *lines)


@pytest.mark.parametrize("cause", sorted(PAUSE_REASONS))
def test_each_cause_has_its_reason_and_its_recovered_note(cause):
    """Detects a pause blamed on an older process when cctally only could not
    read the old status-line files, and a recovered note that outlives its
    seven days."""
    import _lib_doctor
    message = _lib_doctor._outcome_quarantine_message(_row(cause), now=NOW)
    assert message == paused_message("A", TRIGGER_AT, cause, REFRESH, REFRESH)
    recovered = {"state": "recovered", "waitingFor": None}
    row = _row(cause, five={**recovered, "recoveredAt": "2026-10-10T09:00:00Z"},
               weekly={**recovered, "recoveredAt": "2026-10-12T13:00:00Z"})
    last = dt.datetime(2026, 10, 12, 13, tzinfo=dt.timezone.utc)
    note = RECOVERED_TEXT.format(account="A", time="2026-10-12T13:00:00Z",
                                 reason=RECOVERED_REASONS[cause])
    for now in (last, last + dt.timedelta(days=7, seconds=-1)):
        assert _lib_doctor._outcome_quarantine_message(row, now=now) == note
    assert _lib_doctor._outcome_quarantine_message(
        row, now=last + dt.timedelta(days=7)) is None


# --------------------------------------------------------------------------
# through the real writers
# --------------------------------------------------------------------------

def test_ok_with_no_quarantine(app, monkeypatch):
    _seed(app, monkeypatch)
    result = _leg(app)
    assert result.id == "accounting.outcome_quarantine"
    assert result.severity == "ok"
    assert result.details["available"] is True
    assert result.details["accounts"] == []
    assert result.remediation is None


def test_a_recovery_is_rendered_limit_by_limit_until_the_note_expires(
        app, monkeypatch):
    """Detects a doctor that claims an account recovered while one of its
    limits is still paused, names the wrong wait, or keeps warning once
    every limit resumed (spec 7.2): paused waiting for a usage refresh, then
    for fresh periods with their lower bounds, then five-hour resumed alone,
    then OK with the recovered note for seven days, then plain OK."""
    _seed(app, monkeypatch)
    T.current(app, monkeypatch, at="2026-10-09T10:01:00Z", weekly=12,
              week_end=E0, five=40, five_end=F0, account="B")
    _trigger(app, monkeypatch)
    result = _leg(app)
    assert result.severity == "warn"
    rows = _rows(result)
    assert set(rows) == {"A@example.test", "B@example.test"}
    for label, row in rows.items():
        assert row["cause"] == "excluded-write"
        assert {a: (d["state"], d["waitingFor"], d["since"])
                for a, d in row["axes"].items()} == {
            "fiveHour": ("paused", "usage-refresh", TRIGGER_AT),
            "sevenDay": ("paused", "usage-refresh", TRIGGER_AT)}
        assert row["message"] == paused_message(
            label, TRIGGER_AT, "excluded-write", REFRESH, REFRESH)
    assert result.remediation == "\n".join(
        row["message"] for row in result.details["accounts"])
    assert result.details["inventoryEpisode"] is None
    assert result.details["cause"] == "excluded-write"

    _cover(app, monkeypatch)
    T.oauth(app, monkeypatch, at="2026-10-09T10:11:00Z", weekly=13,
            week_end=E0, five=41, five_end=F0, account="B")
    message = _rows(_leg(app))["A@example.test"]["message"]
    assert message == paused_message(
        "A@example.test", TRIGGER_AT, "excluded-write",
        f"paused - waiting for the next five-hour window (not before {F0})",
        "paused - waiting for the next weekly period (not before 2026-10-12)")

    # A stale weekly reading opening a fresh five-hour window: five-hour
    # resumes on its own while weekly still waits.
    T.current(app, monkeypatch, at="2026-10-09T15:00:00Z", weekly=12,
              week_end=E0, five=10, five_end="2026-10-09T20:00:00Z")
    after = dt.datetime(2026, 10, 9, 15, 30, tzinfo=dt.timezone.utc)
    result = _leg(app, after)
    assert result.severity == "warn"
    assert _rows(result)["A@example.test"]["message"] == paused_message(
        "A@example.test", TRIGGER_AT, "excluded-write",
        "resumed 2026-10-09T15:00:00Z",
        "paused - waiting for the next weekly period (not before 2026-10-12)")

    _witness(app, monkeypatch)
    T.current(app, monkeypatch, at=B_WITNESS_AT, weekly=6, week_end=W_END,
              five=6, five_end=W_FIVE, account="B")
    last = dt.datetime(2026, 10, 12, 13, 1, tzinfo=dt.timezone.utc)
    rows = _rows(_leg(app, last))
    recovered = {label: max(d["recoveredAt"] for d in row["axes"].values())
                 for label, row in rows.items()}
    assert recovered == {"A@example.test": WITNESS_AT,
                         "B@example.test": B_WITNESS_AT}, rows
    result = _leg(app, last)
    assert result.severity == "ok", result
    notes = {label: RECOVERED_TEXT.format(
                 account=label, time=recovered[label],
                 reason=RECOVERED_REASONS["excluded-write"])
             for label in recovered}
    assert result.remediation == "\n".join(
        notes[row["accountLabel"]] for row in result.details["accounts"])
    json_leg = _json_leg(app)
    assert _states(json_leg) == _states(result)
    # The recovered note reaches `--json`, and with it the dashboard modal,
    # although the check is OK (spec 7.2: the OK result carries the note).
    assert json_leg.get("remediation") == result.remediation
    # Seven days after A's last limit recovered its note expires; B's stays.
    a_expired = T.epoch(WITNESS_AT) + 7 * 86400
    result = _leg(app, dt.datetime.fromtimestamp(a_expired, dt.timezone.utc))
    assert result.severity == "ok"
    assert result.remediation == notes["B@example.test"]
    result = _leg(app, last + dt.timedelta(days=7))
    assert result.severity == "ok" and result.remediation is None
    assert result.summary == "no account is quarantined"


def test_a_witness_awaiting_its_resumed_release_shows_no_future_bound(
        app, monkeypatch):
    """Detects a "not before" time after the witness that the next writer's
    resumed release will beat (spec 7.1: computed before that witness)."""
    import _cctally_quarantine as G
    _seed(app, monkeypatch)
    _trigger(app, monkeypatch)
    _cover(app, monkeypatch)
    real = G.run_release_step
    calls = []

    def crash_after_witness(account_key):
        # The record path runs the step before each append (resume) and
        # after it (live); the process dies before the live release.
        calls.append(account_key)
        if len(calls) % 2 == 0:
            return []   # the process died before the release append
        return real(account_key)

    monkeypatch.setattr(G, "run_release_step", crash_after_witness)
    _witness(app, monkeypatch)
    witnessed = dt.datetime(2026, 10, 12, 13, tzinfo=dt.timezone.utc)
    result = _leg(app, witnessed)
    assert result.severity == "warn"
    assert _rows(result)["A@example.test"]["message"] == paused_message(
        "A@example.test", TRIGGER_AT, "excluded-write",
        "paused - waiting for the next five-hour window",
        "paused - waiting for the next weekly period")


def test_an_account_idle_for_seven_days_is_not_listed(app, monkeypatch):
    """Detects a doctor that WARNs forever for an account nobody uses any
    more (operator decision F7, 2026-11-02: "Hide after 7 idle days"): an
    account with no reading in the last seven days is left out of the leg,
    while its limits stay paused (it resumes on its own when used again),
    and an account in use is still listed."""
    _seed(app, monkeypatch)
    T.current(app, monkeypatch, at="2026-10-09T10:01:00Z", weekly=12,
              week_end=E0, five=40, five_end=F0, account="B")
    _trigger(app, monkeypatch)
    assert set(_rows(_leg(app))) == {"A@example.test", "B@example.test"}
    # A keeps recording a week later (no usage refresh yet, so still paused);
    # B's last reading is now more than seven days old.
    T.current(app, monkeypatch, at="2026-10-16T09:00:00Z", weekly=12,
              week_end=W_END, five=5, five_end="2026-10-16T12:00:00Z")
    later = dt.datetime(2026, 10, 16, 10, 30, tzinfo=dt.timezone.utc)
    result = _leg(app, later)
    assert result.severity == "warn"
    assert set(_rows(result)) == {"A@example.test"}
    assert result.summary == "automatic outcomes paused for 1 account(s)"
    assert "B@example.test" not in (result.remediation or "")
    assert T.account_quarantined(app, "B")
    # Once A is idle for seven days as well, nothing is listed: OK.
    idle = dt.datetime(2026, 10, 23, 9, 1, tzinfo=dt.timezone.utc)
    result = _leg(app, idle)
    assert result.severity == "ok", result
    assert result.details["accounts"] == []
    assert result.summary == "no account is quarantined"
    assert result.remediation is None


def test_a_revocation_pauses_the_limit_again(app, monkeypatch):
    """Detects a revoked limit still reported resumed: a backdated weekly
    reading after the recovery pauses weekly again, with its reason in
    `--json`, while five-hour stays resumed."""
    _seed(app, monkeypatch)
    _trigger(app, monkeypatch)
    _cover(app, monkeypatch)
    _witness(app, monkeypatch)
    T.current(app, monkeypatch, at="2026-10-12T13:05:00Z", weekly=30,
              week_end=E0, five=5, five_end=W_FIVE)
    later = dt.datetime(2026, 10, 12, 13, 10, tzinfo=dt.timezone.utc)
    result = _leg(app, later)
    assert result.severity == "warn"
    row = _rows(result)["A@example.test"]
    assert row["axes"]["sevenDay"]["state"] == "paused"
    assert row["axes"]["sevenDay"]["revocation"] == "backward-key"
    lines = row["message"].split("\n")
    assert lines[0] == PAUSED_TEXT.format(
        account="A@example.test", time=row["axes"]["sevenDay"]["since"],
        reason=PAUSE_REASONS["excluded-write"])
    assert lines[1] == f"five-hour: resumed {WITNESS_AT}"
    assert lines[2].startswith("weekly: paused - ")


def test_legacy_writer_quiet_for_seven_days_while_this_leg_still_warns(
        app, monkeypatch):
    import _cctally_doctor
    import _lib_doctor
    _seed(app, monkeypatch)
    _trigger(app, monkeypatch)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
    # A keeps recording (no usage refresh yet, so it stays paused): an
    # account idle for seven days would not be listed at all (F7).
    recent = int((later - dt.timedelta(days=1)).timestamp()) // 3600 * 3600
    T.current(app, monkeypatch, at=T.iso(recent), weekly=12,
              week_end=T.iso(recent + 5 * 86400), five=5,
              five_end=T.iso(recent + 3 * 3600))
    from dataclasses import fields
    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(
        now_utc=later,
        legacy_writer=_cctally_doctor._gather_legacy_writer(None, None),
        outcome_quarantine=_cctally_doctor._gather_outcome_quarantine())
    state = _lib_doctor.DoctorState(**kwargs)
    assert _lib_doctor._check_journal_legacy_writer(state).severity == "ok"
    assert _lib_doctor._check_accounting_outcome_quarantine(state).severity == "warn"


def test_unavailable_never_ok_on_an_unvalidated_index(app, monkeypatch):
    _seed(app, monkeypatch)
    conn = app.open_db()
    try:
        conn.execute("PRAGMA user_version = 1017")
        conn.commit()
    finally:
        conn.close()
    result = _leg(app)
    assert result.severity == "warn"
    assert result.details["available"] is False
    # Operator decision dec-5d2a9c2b004a (escalation esc-8a386c207966).
    assert result.summary == UNAVAILABLE_SUMMARY
    assert result.remediation == UNAVAILABLE_REMEDIATION


def _spool_trigger(app, monkeypatch, at):
    """A legacy render's spool write, journaled by the current reducer as a
    digest-only `legacy_spool_activity` op, then ingested."""
    import _cctally_core as core
    import _cctally_journal as jr
    T.set_clock(monkeypatch, at)
    core.STATUSLINE_CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    (core.STATUSLINE_CANDIDATE_DIR / f"legacy-{at[11:13]}{at[14:16]}.json"
     ).write_text('{"legacy": 90}')
    jr.append_legacy_spool_activity(
        changed=(f"statusline-candidates/legacy-{at[11:13]}{at[14:16]}.json",),
        new_state=jr.capture_legacy_spool_state(), reason="changed")
    T.ingest(app)


def test_a_spool_trigger_names_its_cause_and_survives_a_rebuild(
        app, monkeypatch):
    """Detects a spool-only pause blamed on a data write, or rendered
    differently by `--json` or after `db rebuild` (the barrier is
    journal-only)."""
    _seed(app, monkeypatch)
    _spool_trigger(app, monkeypatch, TRIGGER_AT)
    result = _leg(app)
    assert result.severity == "warn"
    row = _rows(result)["A@example.test"]
    since = row["axes"]["sevenDay"]["since"]
    assert row["cause"] == "legacy-spool-activity"
    assert row["message"] == paused_message(
        "A@example.test", since, "legacy-spool-activity", REFRESH, REFRESH)
    assert _states(_json_leg(app)) == _states(result)
    T.rebuild(app)
    assert _leg(app).details == result.details


def test_an_account_waiting_for_the_inventory_names_that_cause(
        app, monkeypatch):
    """Review finding F8: the cause is derived per account. An account whose
    limits wait for the spool inventory is paused because the inventory is
    incomplete, even when a later excluded write is the generation's latest
    trigger; the report-level cause stays that latest trigger's. Detects a
    paused account told the wrong reason (and so the wrong remedy)."""
    import errno
    import _cctally_core as core
    import _cctally_journal as jr
    _seed(app, monkeypatch)
    T.set_clock(monkeypatch, "2026-10-09T10:02:00Z")
    jr.record_legacy_spool_inventory_incomplete(jr.LegacySpoolInventoryIncomplete(
        errno.EIO, "injected", str(core.STATUSLINE_CANDIDATE_DIR),
        operation="list", path=core.STATUSLINE_CANDIDATE_DIR))
    _trigger(app, monkeypatch)
    result = _leg(app)
    assert result.details["cause"] == "excluded-write"
    row = _rows(result)["A@example.test"]
    assert {axis["waitingFor"] for axis in row["axes"].values()} == {"inventory"}
    assert row["cause"] == "inventory-incomplete"
