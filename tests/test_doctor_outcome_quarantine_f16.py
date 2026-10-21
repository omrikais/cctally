"""#825/#832 Task 3.7 — the doctor leg `accounting.outcome_quarantine`, case F16
(spec section 8).

OK with no quarantine. WARN per account, naming the paused outcome classes and
since when, in the operator-approved wording. `journal.legacy_writer` is OK
after seven quiet days while this leg still WARNs. Unavailable — never OK — on
an unvalidated index. Recovery clears the account; `--json` carries the same
states.
"""
import datetime as dt
import json

import pytest

import _outcome_quarantine_trace as T

ACCOUNTING_TEXT = (
    "Automatic credit and reset detection is paused for {account} since {time} "
    "because an older cctally process wrote data after the upgrade. It resumes "
    "only after cctally verifies a clean reading for this account, typically "
    "the first one after an idle period spanning a weekly reset; there is no "
    "guaranteed time. If your usage was reset in place meanwhile, record it "
    "with `cctally record-credit`.")
CONTROL_TEXT = ("Status-line drop detection also waits for a successful usage "
                "refresh for this account.")
CONTROL_ONLY_TEXT = (
    "Status-line drop detection is paused for {account} since {time}. It "
    "resumes after a successful usage refresh for this account.")
UNAVAILABLE_SUMMARY = "unavailable (no validated stats index)"
UNAVAILABLE_REMEDIATION = ("Run any cctally command to bring the stats index "
                           "up to date, then run `cctally doctor` again.")

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _leg(app, now=None):
    import _cctally_doctor
    import _lib_doctor
    state = _cctally_doctor._gather_outcome_quarantine()
    from dataclasses import fields
    kwargs = {f.name: None for f in fields(_lib_doctor.DoctorState)}
    kwargs.update(outcome_quarantine=state,
                  now_utc=now or dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc))
    return _lib_doctor._check_accounting_outcome_quarantine(
        _lib_doctor.DoctorState(**kwargs))


def _seed(app, monkeypatch):
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0, account="A")


def test_ok_with_no_quarantine(app, monkeypatch):
    _seed(app, monkeypatch)
    result = _leg(app)
    assert result.id == "accounting.outcome_quarantine"
    assert result.severity == "ok"
    assert result.details["available"] is True
    assert result.details["accounts"] == []


def test_warn_per_account_with_the_approved_wording(app, monkeypatch):
    _seed(app, monkeypatch)
    T.current(app, monkeypatch, at="2026-10-09T10:01:00Z", weekly=12,
              week_end=E0, five=40, five_end=F0, account="B")
    T.legacy(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0)
    T.ingest(app)
    result = _leg(app)
    assert result.severity == "warn"
    rows = {row["accountLabel"]: row for row in result.details["accounts"]}
    assert set(rows) == {"A@example.test", "B@example.test"}
    for label, row in rows.items():
        assert row["accountingPaused"] and row["controlPaused"]
        assert row["accountingSince"] == "2026-10-09T10:05:00Z"
        assert row["message"] == (
            ACCOUNTING_TEXT.format(account=label, time="2026-10-09T10:05:00Z")
            + " " + CONTROL_TEXT)
    # The leg never claims when recovery will happen.
    assert "will resume" not in result.summary


def test_legacy_writer_quiet_for_seven_days_while_this_leg_still_warns(
        app, monkeypatch):
    import _cctally_doctor
    import _lib_doctor
    _seed(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0)
    T.ingest(app)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8)
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


def test_control_only_row_uses_the_operator_approved_standalone_wording():
    """An account whose accounting certificate is back but whose control
    certificate is still missing gets a standalone sentence. The approved
    control sentence starts with "also" and is only an addendum to the
    accounting one (operator decision dec-5d2a9c2b004a)."""
    import _lib_doctor
    row = {"account": "acct-a", "accountLabel": "A", "accountingPaused": False,
           "controlPaused": True, "controlSince": F0}
    assert _lib_doctor._outcome_quarantine_message(row) == (
        CONTROL_ONLY_TEXT.format(account="A", time=F0))
    both = {**row, "accountingPaused": True, "accountingSince": F0}
    assert _lib_doctor._outcome_quarantine_message(both) == (
        ACCOUNTING_TEXT.format(account="A", time=F0) + " " + CONTROL_TEXT)


def test_recovery_clears_the_account_and_json_carries_the_states(
        app, monkeypatch):
    _seed(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0)
    T.ingest(app)
    # An idle gap spanning the weekly reset and the five-hour window end; a
    # bound two-axis OAuth publication certifies accounting, witnesses control
    # in the index AND records its commit in A's reconciled control
    # transition — the combined predicate doctor's "recovered" claim needs
    # (#825/#832 fix-round-1 Q6).
    import argparse
    import _cctally_statusline as sl
    T.set_clock(monkeypatch, "2026-10-12T13:00:00Z")
    T.identity("A")
    published = sl._authoritative_record_usage(
        argparse.Namespace(
            percent=5, resets_at=str(T.epoch("2026-10-19T13:00:00Z")),
            five_hour_percent=5,
            five_hour_resets_at=str(T.epoch("2026-10-12T18:00:00Z")),
            source="api"),
        ("sevenDay", "fiveHour"),
        binding=sl._capture_binding(credential="token-A"),
        nudge_dashboard=False)
    assert published.status == "ok", published
    result = _leg(app)
    labels = {row["accountLabel"] for row in result.details["accounts"]}
    assert "A@example.test" not in labels
    import _cctally_doctor
    args = app.build_parser().parse_args(["doctor", "--json"])
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        args.func(args)
    report = json.loads(out.getvalue())
    checks = [c for cat in report["categories"] for c in cat["checks"]
              if c["id"] == "accounting.outcome_quarantine"]
    assert len(checks) == 1
    assert checks[0]["details"]["accounts"] == result.details["accounts"]


#: Spec revision 3 (section 8): the operator-approved wording for an account
#: paused by an unresolved spool marker, verbatim.
MARKER_TEXT = (
    "Automatic credit, reset and status-line drop detection is paused for "
    "{account} since {time} because an older cctally process wrote "
    "status-line data after the upgrade. This version of cctally cannot yet "
    "confirm a safe point to resume, so the pause does not end on its own; "
    "usage recording continues. If your usage was reset in place meanwhile, "
    "record it with `cctally record-credit`.")


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


def _assert_marker_warn(app, count, labels, first=None):
    """The WARN in the marker wording; returns the rows and the markers. The
    op's `at` is its append time, so the pause dates from the first marker
    (``first``, once known)."""
    result = _leg(app)
    assert result.severity == "warn", result
    markers = result.details["unresolvedSpoolMarkers"]
    assert len(markers) == count, markers
    assert all(m["segment"] and isinstance(m["offset"], int) and m["at"]
               for m in markers), markers
    if first is not None:
        assert markers[0] == first, (markers, first)
    since = markers[0]["at"]
    rows = {row["accountLabel"]: row for row in result.details["accounts"]}
    assert set(rows) == set(labels), rows
    for label, row in rows.items():
        assert row["accountingPaused"] and row["unresolvedSpoolMarker"], row
        assert row["accountingSince"] == since, row
        assert row["message"] == MARKER_TEXT.format(account=label, time=since)
    assert result.remediation == " ".join(
        MARKER_TEXT.format(account=label, time=since)
        for label in [row["accountLabel"]
                      for row in result.details["accounts"]])
    # `--json` carries the same state.
    assert _json_leg(app)["details"] == result.details
    return rows, markers[0]


def test_an_unresolved_spool_marker_keeps_the_warn_in_the_revision_3_wording(
        app, monkeypatch):
    """Spec revision 3 (F16, F15 (f)): after a spool-only trigger the WARN
    persists through the recovery schedule that clears an excluded-write
    quarantine — an idle gap spanning the weekly reset and the five-hour end
    and a bound two-axis OAuth publication — and through `db rebuild`, in the
    section 8 marker wording. A later trigger keeps the earlier marker."""
    import argparse
    import _cctally_statusline as sl
    _seed(app, monkeypatch)
    _spool_trigger(app, monkeypatch, "2026-10-09T10:05:00Z")
    _rows, first = _assert_marker_warn(app, 1, {"A@example.test"})
    T.set_clock(monkeypatch, "2026-10-12T13:00:00Z")
    T.identity("A")
    published = sl._authoritative_record_usage(
        argparse.Namespace(
            percent=5, resets_at=str(T.epoch("2026-10-19T13:00:00Z")),
            five_hour_percent=5,
            five_hour_resets_at=str(T.epoch("2026-10-12T18:00:00Z")),
            source="api"),
        ("sevenDay", "fiveHour"),
        binding=sl._capture_binding(credential="token-A"),
        nudge_dashboard=False)
    assert published.status == "ok", published
    rows, _first = _assert_marker_warn(app, 1, {"A@example.test"}, first)
    # The control certificate reconciled independently; the marker still
    # pauses drop detection, which needs the accounting certificate too.
    assert rows["A@example.test"]["controlPaused"] is False
    T.rebuild(app)
    _assert_marker_warn(app, 1, {"A@example.test"}, first)
    # A later trigger starts a new generation and keeps the earlier marker.
    _spool_trigger(app, monkeypatch, "2026-10-12T13:05:00Z")
    _assert_marker_warn(app, 2, {"A@example.test"}, first)
    T.rebuild(app)
    _assert_marker_warn(app, 2, {"A@example.test"}, first)
