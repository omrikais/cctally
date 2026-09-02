"""#661 S2 Task C4 — recorded by default, notified on request (§6.2), and
`doctor`'s new `quota` category (§7).

The estate's documented convention is that every alert toggle defaults off so
an upgrade never produces surprise notifications. This session honours it AND
splits recording from notifying: the event is recorded from the first upgrade,
so every pull surface shows the state with no configuration, while the push
follows the existing default-off toggle. A user therefore learns that the rate
changed without opting in, and nothing arrives unbidden.

Neither `doctor` check can FAIL, so `doctor`'s exit code is unaffected — the
same posture `pricing.coverage` and `data.parse_health` take. And neither may
quarantine the calibration file, because `doctor` is documented read-only.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
BOUNDARY = "2026-08-25T00:00:00+00:00"
WITHHELD_STATUS = "unsupported-model-mix"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    return ns


def _transition(ns):
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    return mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from=BOUNDARY, previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0, severity="alarm",
        detected_at=NOW.isoformat())


def _seed_journal():
    import _cctally_journal as jr
    import _lib_journal as J
    jr.append_record(
        J.make_obs(at="2026-08-29T09:00:00Z", src="record-usage",
                   provider="claude",
                   payload={"weekly_percent": 12.0, "source": "statusline"}),
        now_utc=NOW)


def _set_config(block):
    path = _cctally_core.CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alerts": block}), encoding="utf-8")


def _events(ns):
    conn = ns["open_db"]()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT provider, account_key, effective_from"
            " FROM meter_rate_change_events")]
    finally:
        conn.close()


def _record(ns, monkeypatch):
    """Drive the shipped §6.5 steps 3-6 entry point, not the raw ingest.

    #695 moved the dispatch one level up: `record_rate_change_transition`
    returns the emitter result and the payload, and `_settle_rate_change`
    fires it after winning the delivery ledger's compare-and-set. So the
    helper drives BOTH, which keeps every case below testing what it always
    tested — whether the two toggles produce a notification — rather than
    silently asserting the absence of a dispatch this function no longer
    performs at all.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    glue = ns["_load_sibling"]("_cctally_quota_model")
    transition = _transition(ns)
    glue._settle_rate_change(
        transition,
        glue.record_rate_change_transition(transition, now=NOW),
        glue.load_delivery_state())
    return dispatched


# --------------------------------------------------------------------------
# §6.2 — recording is unconditional, push is not
# --------------------------------------------------------------------------
def test_c4_events_are_recorded_with_no_configuration(ns, monkeypatch):
    """A fresh install has no alerts config at all."""
    _seed_journal()
    _record(ns, monkeypatch)
    assert _events(ns) == [("claude", "unattributed", BOUNDARY)]


def test_c4_no_notification_dispatches_without_the_toggle(ns, monkeypatch):
    _seed_journal()
    assert _record(ns, monkeypatch) == []


def test_c4_the_global_switch_alone_is_not_enough(ns, monkeypatch):
    """Both switches must be on, exactly as the quota threshold axis
    requires. A user who turned alerts on generally has not asked for this
    family."""
    _seed_journal()
    _set_config({"enabled": True})
    assert _record(ns, monkeypatch) == []


def test_c4_the_family_toggle_alone_is_not_enough(ns, monkeypatch):
    _seed_journal()
    _set_config({"enabled": False, "rate_change_enabled": True})
    assert _record(ns, monkeypatch) == []


def test_c4_the_toggle_enables_dispatch(ns, monkeypatch):
    """The discriminating twin of all three negatives above."""
    _seed_journal()
    _set_config({"enabled": True, "rate_change_enabled": True})
    dispatched = _record(ns, monkeypatch)
    assert len(dispatched) == 1, dispatched
    assert dispatched[0]["axis"] == "meter_rate_change"
    assert dispatched[0]["severity"] == "alarm"
    assert _events(ns), "the recording must happen on this path too"


def test_c4_a_malformed_alerts_block_records_without_dispatching(
        ns, monkeypatch):
    """§6.2's point is that recording happens with NO configuration, so a
    broken config must not turn a recording path into an error path."""
    _seed_journal()
    _set_config({"enabled": "yes-please"})
    assert _record(ns, monkeypatch) == []
    assert _events(ns)


def test_c4_the_toggle_defaults_off_in_the_validated_block():
    block = _cctally_core._get_alerts_config({})
    assert block["rate_change_enabled"] is False


def test_c4_the_toggle_must_be_a_json_boolean():
    with pytest.raises(_cctally_core._AlertsConfigError) as excinfo:
        _cctally_core._get_alerts_config(
            {"alerts": {"rate_change_enabled": "true"}})
    assert excinfo.value.field == "alerts.rate_change_enabled"


# --------------------------------------------------------------------------
# §7 — the doctor category
# --------------------------------------------------------------------------
def _doctor():
    import _lib_doctor
    return _lib_doctor


def _report(**state_kwargs):
    import dataclasses
    ld = _doctor()
    fields = {
        field.name: (field.default
                     if field.default is not dataclasses.MISSING else None)
        for field in dataclasses.fields(ld.DoctorState)
    }
    fields.update(state_kwargs)
    return ld.run_checks(ld.DoctorState(**fields))


def _category(report, cat_id):
    return next(c for c in report.categories if c.id == cat_id)


def test_c4_the_quota_category_exists_with_exactly_two_checks():
    report = _report()
    quota = _category(report, "quota")
    assert [c.id for c in quota.checks] == [
        "quota.meter_drift", "quota.calibration"]


def test_c4_the_category_inventory_is_pinned():
    """§13 requires `doctor`'s exact category inventory after adding
    `quota`, so the whole list is asserted rather than membership alone."""
    report = _report()
    assert [c.id for c in report.categories] == [
        "install", "hooks", "auth", "db", "journal", "data", "accounts",
        "pricing", "quota", "safety", "telemetry"]


@pytest.mark.parametrize("rejection,expected", [
    (None, "ok"),
    ("calibration-absent", "ok"),
    ("calibration-detection-only", "ok"),
    ("calibration-malformed", "warn"),
    ("calibration-schema-version", "warn"),
    ("calibration-fingerprint-mismatch", "warn"),
    ("calibration-revision-mismatch", "warn"),
    ("calibration-unreadable", "warn"),
])
def test_c4_doctor_quota_checks_never_fail(rejection, expected):
    report = _report(quota_calibration={
        "rejection": rejection, "status": None,
        "present": rejection is None})
    quota = _category(report, "quota")
    assert quota.severity in {"ok", "warn"}, rejection
    check = next(c for c in quota.checks if c.id == "quota.calibration")
    assert check.severity == expected, rejection


def test_c4_a_rate_change_is_reported_without_warning():
    """A rate change is the provider's behaviour, not a cctally
    malfunction."""
    report = _report(quota_rate_change={
        "active": True, "effective_from": BOUNDARY,
        "previous_units_per_point": 2_442_620.0,
        "new_units_per_point": 1_685_000.0})
    quota = _category(report, "quota")
    drift = next(c for c in quota.checks if c.id == "quota.meter_drift")
    assert drift.severity == "ok"
    assert "2026-08-25 UTC" in drift.summary
    assert drift.details["active"] is True


def test_the_effective_day_is_parsed_and_labelled_not_text_sliced():
    """`str(stamp)[:10]` names the UTC day only when the offset happens to
    agree (#661 S2 Stage C review, F8). The store holds mixed offset
    spellings, so the same instant spelled `+02:00` sliced to the wrong day.
    The doctor kernel has no display-timezone plumbing, so the day is
    normalized to UTC and says so."""
    report = _report(quota_rate_change={
        "active": True, "effective_from": "2026-08-26T01:00:00+02:00",
        "previous_units_per_point": 2_442_620.0,
        "new_units_per_point": 1_685_000.0})
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert "2026-08-25 UTC" in drift.summary, drift.summary


def test_c4_no_rate_change_says_so():
    """A FITTED regime holding no confirmed predecessor is a real negative
    finding, so it keeps saying so. `assessed` is explicit because #688 made
    the unmarked form mean the opposite of what it claimed."""
    report = _report(quota_rate_change={"active": False, "assessed": True})
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert drift.severity == "ok"
    assert drift.summary == "no change detected"
    assert drift.details["active"] is False


def test_688_an_absent_calibration_is_not_assessed_not_no_change():
    """Absence of a fitted regime is absence of evidence, never evidence of
    absence.

    `docs/commands/quota.md` forbids conflating "the evidence needed to
    decide is missing" with "your metering is unchanged", and on 2026-08-30
    `doctor` made exactly the forbidden claim: a green `no change detected`
    while `quota`'s own detector had confirmed a split on the same store at
    a Holm-corrected p of 0.000876. The fit was withheld, so no regime
    persisted, so the calibration was absent — and the gather mapped that
    absence onto a negative finding.
    """
    import _cctally_doctor as gd
    state = gd._gather_quota_rate_change(None, rejection="calibration-absent")
    report = _report(quota_rate_change=state)
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert "no change detected" not in drift.summary, drift.summary
    assert drift.details["assessed"] is False


def test_688_an_unassessed_state_never_renders_a_negative_finding():
    """The kernel gate is fail-closed: a state must CLAIM it assessed the
    regime pair before the negative finding is available to it. A state that
    merely omits the claim is not assessed, because the omission is how #688
    reached the user."""
    report = _report(quota_rate_change={"active": False})
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert "no change detected" not in drift.summary, drift.summary
    assert drift.details["assessed"] is False


def test_688_the_not_assessed_details_keep_every_published_key():
    """A published JSON surface nulls a key rather than dropping it, so a
    consumer reading `effective_from` finds it absent-VALUED. The first fix
    dropped all three regime fields on this branch."""
    report = _report(quota_rate_change=None)
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert set(drift.details) == {
        "active", "assessed", "effective_from",
        "previous_units_per_point", "new_units_per_point",
        # #688: the successor's own calibration status joins the published
        # shape, and is nulled on this branch by the same rule.
        "calibration_status"}
    assert drift.details["effective_from"] is None
    assert drift.details["calibration_status"] is None


def test_688_an_active_change_is_reported_without_an_assessed_claim():
    """The gate is asymmetric ON PURPOSE, and this pins the asymmetry.

    An active transition is itself proof the regime pair was examined, so it
    must render whether or not the state carries the separate `assessed`
    claim. Gating this branch too would trade #688's false all-clear for a
    suppressed real change, which is the worse of the two failures. My first
    fix did exactly that, and two existing tests caught it.
    """
    report = _report(quota_rate_change={
        "active": True, "effective_from": BOUNDARY,
        "previous_units_per_point": 2_442_620.0,
        "new_units_per_point": 1_665_096.0})
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert "2026-08-25 UTC" in drift.summary, drift.summary
    assert drift.details["assessed"] is True


def test_688_an_unreadable_calibration_is_also_not_assessed():
    """`None` reaches the same rendering by the same reasoning, and now
    carries the `assessed` key so a JSON consumer can tell the two silences
    apart from a finding without parsing prose."""
    report = _report(quota_rate_change=None)
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert "no change detected" not in drift.summary, drift.summary
    assert drift.details["assessed"] is False


def test_c4_stale_means_fingerprint_mismatch_not_calendar_age():
    """§7 states this explicitly, because `stale` reads like an age word.
    S1 defines no age, so a calibration fitted in January under the current
    constants is healthy and one fitted yesterday under other constants is
    not."""
    old_but_current = _report(quota_calibration={
        "rejection": None, "status": "ok", "present": True})
    assert next(c for c in _category(old_but_current, "quota").checks
                if c.id == "quota.calibration").severity == "ok"
    fresh_but_stale = _report(quota_calibration={
        "rejection": "calibration-fingerprint-mismatch",
        "status": None, "present": False})
    assert next(c for c in _category(fresh_but_stale, "quota").checks
                if c.id == "quota.calibration").severity == "warn"


def test_c4_doctor_never_renames_the_calibration_file(ns, tmp_path):
    """`doctor` is documented read-only; `load_calibrations` quarantines a
    malformed or version-ahead file by RENAMING it aside."""
    import sys as _sys
    app = _sys.modules["cctally"]
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    before = sorted(q.name for q in path.parent.iterdir())
    doctor_glue = ns["_load_sibling"]("_cctally_doctor")
    assert doctor_glue._gather_quota_rate_change(app) is None
    assert doctor_glue._gather_quota_rate_change(
        app, "calibration-absent")["active"] is False
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    qcg.read_calibration_file(account_key=None)
    after = sorted(q.name for q in path.parent.iterdir())
    assert after == before, (
        "the read-only path renamed a file aside, which makes `doctor` a "
        "writer")


def test_c4_the_marker_predicate_is_derived_from_an_open_successor(ns):
    """§6.6: the predicate is "the active open regime has a confirmed
    predecessor", and the marker shows for the whole of that successor
    regime. No new durable state, which §1 forbids adding."""
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    closed = {"effectiveFrom": "2026-07-25T00:00:00+00:00",
              "effectiveUntil": BOUNDARY, "unitsPerPoint": 2_442_620.0,
              "status": "ok"}
    open_successor = {"effectiveFrom": BOUNDARY, "effectiveUntil": None,
                      "unitsPerPoint": 1_685_000.0,
                      "status": "insufficient-history"}
    active = mrc.active_rate_change([closed, open_successor])
    assert active is not None and active["active"] is True
    assert active["effective_from"] == BOUNDARY
    assert active["severity"] == "alarm"
    # The discriminating twin: one regime with no predecessor is a new
    # user's first fit, not a transition.
    assert mrc.active_rate_change([open_successor]) is None
    # And a CLOSED successor no longer shows the marker, which is what makes
    # it clear rather than latch forever.
    assert mrc.active_rate_change([
        closed, dict(open_successor, effectiveUntil="2026-09-01T00:00:00+00:00")
    ]) is None


def test_the_effective_date_renders_in_the_display_timezone(ns):
    """#661 S2 Stage C review. `str(payload["effective_from"])[:10]` renders
    the UTC calendar date of a clock INSTANT and ignores the `tz` parameter
    the builder is handed.

    `_alert_text_weekly` documents in a comment exactly when bypassing the
    display timezone is correct — a calendar DAY, which cannot be shifted
    without changing which day it names. `effectiveFrom` is not that: it is
    a clock instant, so a user at UTC+13 or UTC-8 was shown a date one day
    away from their own.
    """
    from zoneinfo import ZoneInfo
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    payload = mrc.alert_payload(mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from="2026-08-25T00:00:00+00:00",
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0,
        severity="alarm", detected_at=NOW.isoformat()))
    _t, _s, west = alerts._alert_text_meter_rate_change(
        payload, ZoneInfo("America/Los_Angeles"))
    _t, _s, east = alerts._alert_text_meter_rate_change(
        payload, ZoneInfo("Pacific/Auckland"))
    assert "2026-08-24" in west, west
    assert "2026-08-25" in east, east


def test_the_family_states_its_own_affordance_rather_than_the_shared_one(ns):
    """The documented exception to `_with_next_step` (#661 S2 Stage C
    review, F7b).

    Every threshold builder ends with `_with_next_step`, which derives a
    scoped command through `alert_next_step_command`. That kernel branches on
    `AXIS_REGISTRY` axes and this family is deliberately not one (spec
    section 6.1), so it has no window to scope and no branch to reach. The
    family states its affordance directly instead, and this test is what
    keeps that sentence from silently disappearing.
    """
    alerts = ns["_load_sibling"]("_cctally_alerts")
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    payload = mrc.alert_payload(mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from="2026-08-25T00:00:00+00:00",
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_685_000.0,
        severity="alarm", detected_at=NOW.isoformat()))
    _title, _subtitle, body = alerts._alert_text_meter_rate_change(
        payload, None)
    assert "cctally quota" in body


# --------------------------------------------------------------------------
# #688 — the successor's own calibration status reaches `doctor`
# --------------------------------------------------------------------------
def _pair(successor_status):
    closed = {"effectiveFrom": "2026-07-25T00:00:00+00:00",
              "effectiveUntil": BOUNDARY, "unitsPerPoint": 2_442_620.0,
              "status": "ok"}
    successor = {"effectiveFrom": BOUNDARY, "effectiveUntil": None,
                 "unitsPerPoint": 1_665_096.0, "status": successor_status}
    return [closed, successor]


def _drift(**state):
    report = _report(quota_rate_change=dict(
        {"active": True, "effective_from": BOUNDARY,
         "previous_units_per_point": 2_442_620.0,
         "new_units_per_point": 1_665_096.0}, **state))
    return next(c for c in _category(report, "quota").checks
                if c.id == "quota.meter_drift")


def test_688_an_active_pair_carries_the_successor_status(ns):
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    got = mrc.active_rate_change(_pair("unsupported-model-mix"))
    assert got["calibration_status"] == "unsupported-model-mix"
    assert mrc.active_rate_change(_pair("ok"))["calibration_status"] == "ok"


def test_688_the_gather_forwards_the_status_in_all_three_shapes(ns):
    """The two constant shapes null the key rather than dropping it, exactly
    as they already do for the three regime fields."""
    import _cctally_doctor as gd
    assert gd._QUOTA_NO_CHANGE["calibration_status"] is None
    assert gd._QUOTA_NOT_ASSESSED["calibration_status"] is None
    assert gd._gather_quota_rate_change(
        None, rejection="calibration-absent")["calibration_status"] is None


def test_688_the_gather_forwards_the_status_on_the_active_shape(ns):
    """The three shapes above all null the key, so none of them can show that
    the ACTIVE shape carries a real status through. That shape is the one
    #688 needs and the only one reaching `active_rate_change`, so it is
    pinned through the gather rather than by building the state directly."""
    gd = ns["_load_sibling"]("_cctally_doctor")
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")

    class _Glue:
        @staticmethod
        def read_stored_state_readonly():
            return {"accounts": {}}

        @staticmethod
        def stored_regimes(_loaded, _account):
            return [
                {"effectiveFrom": "2026-08-01T00:00:00+00:00",
                 "effectiveUntil": BOUNDARY, "status": "ok",
                 "unitsPerPoint": 2_442_620.0},
                {"effectiveFrom": BOUNDARY, "effectiveUntil": None,
                 "status": WITHHELD_STATUS, "unitsPerPoint": 1_665_096.0},
            ]

    class _Ctx:
        @staticmethod
        def _load_sibling(name):
            return mrc if name == "_lib_meter_rate_change" else _Glue

    got = gd._gather_quota_rate_change(_Ctx)
    assert got["active"] is True
    assert got["assessed"] is True
    assert got["calibration_status"] == WITHHELD_STATUS


def test_688_an_ok_successor_keeps_the_fitted_budget_remediation():
    drift = _drift(calibration_status="ok")
    assert drift.remediation == "Run `cctally quota` for the fitted budget."
    assert drift.details["calibration_status"] == "ok"


@pytest.mark.parametrize("status", ["unsupported-model-mix",
                                    "insufficient-history", None])
def test_688_a_non_ok_successor_promises_no_fitted_budget(status):
    """A successor that is not prediction-ready has no fitted budget, so the
    older remediation sent that user to a command that would refuse. That was
    already true for `insufficient-history` before #688 made a second status
    reachable here."""
    # Pinned EXACTLY rather than by forbidden substrings. A `not in` over
    # one phrasing passes on a rewrite that gives the same false instruction
    # in different words, which is the failure this test exists to stop.
    drift = _drift(calibration_status=status)
    assert drift.remediation == (
        f"No fitted budget is available ({status}). "
        "Run `cctally quota` for the evidence." if status
        else "Run `cctally quota` for the evidence.")


def test_688_an_unsupported_successor_is_still_active_and_assessed():
    drift = _drift(calibration_status="unsupported-model-mix")
    assert drift.details["active"] is True
    assert drift.details["assessed"] is True
    assert drift.severity == "ok"
    assert drift.summary.startswith("rate changed")


def test_688_the_negative_finding_still_carries_no_remediation():
    """Only the ACTIVE branch grew a remediation branch. `no change detected`
    has nothing to remediate and must stay silent."""
    report = _report(quota_rate_change={"active": False, "assessed": True,
                                        "calibration_status": None})
    drift = next(c for c in _category(report, "quota").checks
                 if c.id == "quota.meter_drift")
    assert drift.summary == "no change detected"
    assert drift.remediation is None
    assert drift.details["calibration_status"] is None
