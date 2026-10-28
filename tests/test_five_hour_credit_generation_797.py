"""#797/#823/#840 T12 and T13 — the five-hour credit generation and
projection fills (spec 4.9; D3, D6, D11, D12; section 6 T12, T13).

T12. The `(prior_percent, post_percent)` pair check is replaced, for
observations recorded under `detector_policy: 2`, by causal retry identity
plus a per-account, per-window credit generation: the selected effective
five-hour credit's stable event identity and its content revision, with the
no-credit sentinel ``"none"``. The confirmation state records the generation
its baseline and its pending descent were established under, and the
confirmation rechecks it, so

* a genuine same-pair second credit in a distinct ten-minute slot is minted;
* two sources armed from the old generation confirm the descent once;
* a source keeping a pre-credit high cannot arm another credit with it;
* D11: growth observed between arming and a delayed confirmation survives as
  the new generation's baseline (`20 -> 5`, then `10 -> 0`);
* a correction or tombstone of the generation's event makes dependent state
  obsolete;
* two credits in one ten-minute slot stay one;
* an observation without `detector_policy` keeps the pair check, and an
  unsupported or malformed value withholds that observation's detector step.

T13. A projection-filled axis of a status-line publication is not a reading
of its source: the whole source-local step is skipped for it (both axes, the
weekly debounce included), while a genuine candidate still cancels.

Every trace is compared live, rebuilt, rederived and completed (the
completing rebuild of a deleted index replays every admitted input through
its scratch), by `collections.Counter` and against literal expected outcomes.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import sys
import time

import pytest

import _outcome_quarantine_trace as T
from conftest import load_script, redirect_paths

NOW = 1791417600  # 2026-10-08T00:00:00Z, a ten-minute boundary
WEEK_END = NOW + 3 * 86400
FIVE_END = NOW + 3 * 3600
MISSING = object()
UTC = dt.timezone.utc


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(int(epoch), UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _slot(offset: int) -> str:
    """The `effective_reset_at_utc` spelling of a ten-minute slot."""
    return dt.datetime.fromtimestamp(NOW + offset, UTC).isoformat(
        timespec="seconds")


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW))
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    T.retain_session_input(cctally, "A")
    T.identity("A")
    return cctally


def _key(name="A"):
    import _lib_accounts
    return _lib_accounts.account_key("claude", name)


def _five_key(app):
    return int(app._canonical_5h_window_key(FIVE_END))


def _week_key():
    import _cctally_core as core
    return int(core._normalize_week_boundary_dt(
        dt.datetime.fromtimestamp(WEEK_END, UTC)).timestamp())


def _plan(app, at, *, weekly, five, weekly_kind="candidate",
          five_kind="candidate", detector=2, fold=1):
    """A frozen status-line publication plan (the fields
    `record_usage_bound` reads), with per-axis provenance kinds."""
    account = _key()

    def axis(kind, window):
        doc = {"kind": kind, "account": account, "window": window}
        if kind == "candidate":
            doc["token"] = "t" * 8
        elif kind == "projection":
            doc["logicalResetGeneration"] = "none"
        else:
            doc["controlGeneration"] = 1
        return doc

    provenance = {"sevenDay": axis(weekly_kind, _week_key())}
    if five is not None:
        provenance["fiveHour"] = axis(five_kind, _five_key(app))
    plan = {"capturedAt": NOW + at, "fiveHourWindowKey": None,
            "provenance": provenance}
    if fold is not MISSING:
        plan["foldPolicy"] = fold
    if detector is not MISSING:
        plan["detectorPolicy"] = detector
    plan["publicationId"] = hashlib.sha256(json.dumps(
        [at, weekly, five, weekly_kind, five_kind, repr(detector)]
    ).encode()).hexdigest()
    return plan


def _tick(app, monkeypatch, at, five, *, weekly=40.0, source="api",
          plan=None, ingest=True):
    """One bound reading at ``NOW + at``. ``plan`` (a dict of `_plan`
    keyword arguments) records it as a status-line publication."""
    import _cctally_journal as jr
    import _cctally_statusline as sl
    monkeypatch.setattr(time, "time", lambda: float(NOW + at))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(NOW + at))
    T.identity("A")
    args = argparse.Namespace(
        percent=weekly, resets_at=str(WEEK_END), five_hour_percent=five,
        five_hour_resets_at=None if five is None else str(FIVE_END),
        source=source)
    publication = None
    if plan is not None:
        publication = _plan(app, at, weekly=weekly, five=five, **plan)
    with monkeypatch.context() as patch:
        if not ingest:
            patch.setattr(jr, "run_stats_ingest", lambda **_kw: jr.IngestResult(
                ran=False, consumed=0, malformed=0, events_emitted=0,
                alerts=[]))
        rc = app.record_usage_bound(
            args, binding=sl._capture_binding(), nudge_dashboard=False,
            publication=publication)
    assert rc == 0, rc


def _run(app, monkeypatch, steps, **kwargs):
    for step in steps:
        at, five, source = step[:3]
        extra = dict(step[3]) if len(step) > 3 else {}
        _tick(app, monkeypatch, at, five, source=source, **extra, **kwargs)


def _credits(app) -> collections.Counter:
    rows = T._rows(app, "SELECT account_key, prior_percent, post_percent, "
                        "effective_reset_at_utc FROM five_hour_reset_events")
    return collections.Counter(
        (r["account_key"], round(r["prior_percent"], 1),
         round(r["post_percent"], 1), r["effective_reset_at_utc"])
        for r in rows)


def _weekly_resets(app) -> collections.Counter:
    rows = T._rows(app, "SELECT account_key, observed_pre_credit_pct, "
                        "effective_reset_at_utc FROM week_reset_events")
    return collections.Counter(json.dumps(r, sort_keys=True) for r in rows)


def _state(app, source):
    rows = T._rows(
        app, "SELECT baseline_pct, pending_low_pct, pending_at_utc, "
             "pending_observation_id, baseline_observation_id, "
             "baseline_generation, pending_generation, pending_position "
             "FROM five_hour_credit_confirmation_state "
             f"WHERE source = '{source}'")
    return rows[0] if rows else None


def _worlds(app, read=_credits):
    """Live, rebuilt, rederived and completed outcome multisets."""
    import _cctally_core as core
    worlds = {"live": read(app)}
    T.rebuild(app)
    worlds["rebuilt"] = read(app)
    T.rederive(app)
    worlds["rederived"] = read(app)
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = core.DB_PATH.with_name(core.DB_PATH.name + suffix)
        if path.exists():
            path.unlink()
    T.rebuild(app)
    worlds["completed"] = read(app)
    return worlds


def _assert_worlds(worlds, expected):
    for name, found in worlds.items():
        assert found == expected, (name, found)


def _expected(*credits):
    return collections.Counter((_key(),) + c for c in credits)


# --------------------------------------------------------------------------
# detector policy (D12)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload, policy", [
    ({}, "historical"),
    ({"detector_policy": 2}, "v2"),
    ({"detector_policy": 3}, "unsupported"),
    ({"detector_policy": "2"}, "unsupported"),
    ({"detector_policy": True}, "unsupported"),
    ({"detector_policy": 2.0}, "unsupported"),
    ({"detector_policy": None}, "unsupported"),
    ({"detector_policy": 1}, "unsupported"),
])
def test_detector_policy_of(payload, policy):
    import _cctally_journal as jr
    assert jr.DETECTOR_POLICY_KEY == "detector_policy"
    assert jr.PLAN_DETECTOR_POLICY_KEY == "detectorPolicy"
    assert jr.DETECTOR_POLICY == 2
    assert jr.detector_policy_of(payload) == policy


def _usage_payloads():
    return [rec["payload"] for rec in T.journal_records()
            if rec.get("t") == "obs" and rec.get("src") == "record-usage"]


def test_new_observations_carry_detector_policy_2_beside_fold_policy_1(
        app, monkeypatch):
    _tick(app, monkeypatch, 0, 20.0)
    (payload,) = _usage_payloads()
    assert payload["fold_policy"] == 1
    assert payload["detector_policy"] == 2


@pytest.mark.parametrize("frozen, recorded", [
    (MISSING, MISSING), (2, 2)])
def test_recovery_copies_the_frozen_detector_policy_or_its_absence(
        app, monkeypatch, frozen, recorded):
    _tick(app, monkeypatch, 0, 20.0, source="statusline",
          plan={"detector": frozen})
    (payload,) = _usage_payloads()
    assert payload["fold_policy"] == 1
    if recorded is MISSING:
        assert "detector_policy" not in payload
    else:
        assert payload["detector_policy"] == recorded


def _valid_plan_document(app):
    import _cctally_statusline as sl
    binding = sl._capture_binding()
    week = _week_key()
    return {
        "publicationId": "a" * 64, "capturedAt": NOW,
        "binding": binding.document(),
        "sevenDay": {"percent": 40.0, "rawResetsAt": WEEK_END,
                     "canonicalKey": week},
        "fiveHour": None, "fiveHourWindowKey": None,
        "provenance": {"sevenDay": {"kind": "candidate", "account": _key(),
                                    "window": week}},
        "control": None, "journalHighWater": None, "intakeSequence": 0,
    }, binding.account_key


@pytest.mark.parametrize("extra", [
    {}, {"foldPolicy": 1}, {"foldPolicy": 1, "detectorPolicy": 2}])
def test_the_strict_plan_validator_accepts_the_detector_policy(app, extra):
    import _cctally_statusline as sl
    doc, account = _valid_plan_document(app)
    doc.update(extra)
    assert sl._validate_publication(doc, account_key=account) == doc


@pytest.mark.parametrize("value", [3, 1, "2", True, 2.0, None])
def test_the_strict_plan_validator_rejects_another_detector_policy(app, value):
    import _cctally_statusline as sl
    import _lib_statusline_candidates as candidates
    doc, account = _valid_plan_document(app)
    doc.update({"foldPolicy": 1, "detectorPolicy": value})
    with pytest.raises(candidates.StateValidationError):
        sl._validate_publication(doc, account_key=account)


# --------------------------------------------------------------------------
# the generation-aware kernel
# --------------------------------------------------------------------------

def _plan_kernel(**kwargs):
    import _lib_record as R
    base = dict(baseline_pct=20.0, pending_low_pct=None,
                pending_observation_id=None, new_pct=5.0,
                observation_id="o:new", drop_threshold=5.0, window_live=True)
    base.update(kwargs)
    return R.plan_five_hour_source_local_credit(**base)


def test_kernel_obsolete_baseline_sets_a_new_baseline_instead_of_arming():
    import _lib_record as R
    decision = _plan_kernel(generation="g2", baseline_generation="g1")
    assert decision.action == R.FIVE_HOUR_SET_BASELINE
    assert decision.baseline_pct == 5.0


def test_kernel_obsolete_pending_descent_cannot_confirm():
    import _lib_record as R
    decision = _plan_kernel(
        pending_low_pct=5.0, pending_observation_id="o:arm", new_pct=6.0,
        generation="g2", baseline_generation="g1", pending_generation="g1")
    assert decision.action == R.FIVE_HOUR_SET_BASELINE
    assert decision.baseline_pct == 6.0


def test_kernel_current_generation_and_unstamped_state_keep_their_rules():
    import _lib_record as R
    assert _plan_kernel(generation="g1", baseline_generation="g1"
                        ).action == R.FIVE_HOUR_ARM
    assert _plan_kernel(generation="g1").action == R.FIVE_HOUR_ARM
    confirm = _plan_kernel(
        pending_low_pct=5.0, pending_observation_id="o:arm", new_pct=6.0,
        generation="g1", baseline_generation="g1", pending_generation="g1")
    assert (confirm.action, confirm.credit_prior_pct, confirm.credit_post_pct
            ) == (R.FIVE_HOUR_CONFIRM, 20.0, 5.0)
    # The historical detector passes no generation at all.
    assert _plan_kernel(baseline_generation="g1").action == R.FIVE_HOUR_ARM


def test_generation_is_event_identity_at_content_revision(app, monkeypatch):
    import _cctally_record as rec
    import _lib_journal as J
    conn = app.open_db()
    try:
        assert rec.five_hour_credit_generation(
            conn, _five_key(app), account_key=_key()) == "none"
    finally:
        conn.close()
    _run(app, monkeypatch, [(0, 20.0, "api"), (60, 5.0, "api"),
                            (120, 6.0, "api")])
    conn = app.open_db()
    try:
        generation = rec.five_hour_credit_generation(
            conn, _five_key(app), account_key=_key())
        identity = J.evt_id("fhc", _key(), _five_key(app), _slot(0))
        assert generation.startswith(identity + "@")
        conn.execute("UPDATE five_hour_reset_events SET post_percent = 4")
        changed = rec.five_hour_credit_generation(
            conn, _five_key(app), account_key=_key())
        assert changed.startswith(identity + "@") and changed != generation
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T12 traces
# --------------------------------------------------------------------------

def test_t12_genuine_same_pair_second_credit_is_minted(app, monkeypatch):
    """RED on the parent: its pair check absorbs the second `20 -> 5`."""
    _run(app, monkeypatch, [
        (0, 20.0, "api"), (60, 5.0, "api"), (120, 6.0, "api"),
        (300, 20.0, "api"), (660, 5.0, "api"), (720, 6.0, "api")])
    _assert_worlds(_worlds(app), _expected(
        (20.0, 5.0, _slot(0)), (20.0, 5.0, _slot(600))))


def test_t12_literal_replay_of_one_descent_reuses_its_decision(
        app, monkeypatch):
    _run(app, monkeypatch, [(0, 20.0, "api"), (60, 5.0, "api"),
                            (120, 6.0, "api")])
    before = T._rows(app, "SELECT * FROM five_hour_reset_events")
    records = [r for r in T.journal_records() if r.get("t") == "obs"
               and r.get("src") == "record-usage"]
    # The arming and the confirming observation, byte for byte, again.
    T.raw_append(records[1:3])
    T.ingest(app)
    assert T._rows(app, "SELECT * FROM five_hour_reset_events") == before
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


@pytest.mark.parametrize("first, second, credit", [
    (("api", 5.0), ("statusline", 4.0), (20.0, 5.0)),
    (("statusline", 4.0), ("api", 5.0), (20.0, 4.0)),
])
def test_t12_two_sources_armed_from_the_old_generation_confirm_once(
        app, monkeypatch, first, second, credit):
    """RED on the parent: the second source confirms its own `20 -> x`."""
    (a, a_low), (b, b_low) = first, second
    _run(app, monkeypatch, [
        (0, 20.0, a), (30, 20.0, b), (60, a_low, a), (660, b_low, b),
        (720, a_low + 1, a), (780, b_low + 1.5, b), (840, b_low + 0.5, b)])
    _assert_worlds(_worlds(app), _expected(credit + (_slot(0),)))


def test_t12_a_pre_credit_high_cannot_arm_another_credit(app, monkeypatch):
    """RED on the parent: statusline's obsolete 20 arms `20 -> 6`."""
    _run(app, monkeypatch, [
        (0, 20.0, "api"), (30, 20.0, "statusline"), (60, 5.0, "api"),
        (120, 6.0, "api"), (660, 6.0, "statusline"),
        (720, 5.5, "statusline")])
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


def test_t12_d11_growth_between_arming_and_confirmation_survives(
        app, monkeypatch):
    """D11's trace: A arms `20 -> 5`, B observes a genuine 10, A confirms,
    B observes `0, 1`. RED on the parent, which mints `20 -> 10`."""
    _run(app, monkeypatch, [
        (0, 20.0, "api"), (30, 20.0, "statusline"), (60, 5.0, "api"),
        (660, 10.0, "statusline"), (720, 6.0, "api")])
    rebased = _state(app, "statusline")
    assert rebased is not None
    assert (rebased["baseline_pct"], rebased["pending_low_pct"]) == (10.0, None)
    api = _state(app, "api")
    assert rebased["baseline_generation"] == api["baseline_generation"] != "none"
    _run(app, monkeypatch, [(1260, 0.0, "statusline"),
                            (1320, 1.0, "statusline")])
    _assert_worlds(_worlds(app), _expected(
        (20.0, 5.0, _slot(0)), (10.0, 0.0, _slot(1200))))


@pytest.mark.parametrize("correction", [
    "DELETE FROM five_hour_reset_events",
    "UPDATE five_hour_reset_events SET prior_percent = 19",
])
def test_t12_a_correction_of_the_generation_invalidates_dependent_state(
        app, monkeypatch, correction):
    """RED on the parent: the baseline raised under the corrected credit
    arms `12 -> 6` and confirms it."""
    _run(app, monkeypatch, [(0, 20.0, "api"), (60, 5.0, "api"),
                            (120, 6.0, "api"), (300, 12.0, "api")])
    conn = app.open_db()
    try:
        conn.execute(correction)
        conn.commit()
    finally:
        conn.close()
    before = _credits(app)
    _run(app, monkeypatch, [(660, 6.0, "api"), (720, 6.5, "api")])
    assert _credits(app) == before
    state = _state(app, "api")
    assert state["pending_low_pct"] is None
    assert state["baseline_pct"] == 6.5


def test_t12_two_credits_in_one_ten_minute_slot_stay_one(app, monkeypatch):
    _run(app, monkeypatch, [
        (0, 20.0, "api"), (60, 5.0, "api"), (120, 6.0, "api"),
        (180, 20.0, "api"), (240, 5.0, "api"), (300, 6.0, "api")])
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


def test_t12_observations_without_detector_policy_keep_the_pair_check(
        app, monkeypatch):
    historical = {"plan": {"detector": MISSING}}
    _run(app, monkeypatch, [
        (0, 20.0, "statusline", historical), (60, 5.0, "statusline", historical),
        (120, 6.0, "statusline", historical),
        (300, 20.0, "statusline", historical),
        (660, 5.0, "statusline", historical),
        (720, 6.0, "statusline", historical)])
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


@pytest.mark.parametrize("value", [3, "2", True, None, 2.0])
def test_t12_an_unsupported_detector_policy_withholds_the_step(
        app, monkeypatch, value):
    """RED on the parent: the unsupported 25 cancels the armed descent."""
    _run(app, monkeypatch, [(0, 20.0, "api"), (60, 5.0, "api")])
    armed = _state(app, "api")
    _tick(app, monkeypatch, 120, 25.0, plan={"detector": value})
    assert _state(app, "api") == armed
    _tick(app, monkeypatch, 180, 6.0)
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


# --------------------------------------------------------------------------
# T13 projection fills
# --------------------------------------------------------------------------

CANDIDATE = {"plan": {}}
FIVE_FILLED = {"plan": {"five_kind": "projection"}, "weekly": 41.0}


def test_t13_a_projected_five_hour_high_leaves_the_state_unchanged(
        app, monkeypatch):
    """RED on the parent: the projected 20 cancels the armed descent."""
    _run(app, monkeypatch, [(0, 20.0, "statusline", CANDIDATE),
                            (60, 5.0, "statusline", CANDIDATE)])
    armed = _state(app, "statusline")
    assert armed["pending_low_pct"] == 5.0
    _run(app, monkeypatch, [(180, 20.0, "statusline", FIVE_FILLED)])
    assert _state(app, "statusline") == armed
    _run(app, monkeypatch, [(240, 6.0, "statusline",
                             {"plan": {}, "weekly": 41.0})])
    _assert_worlds(_worlds(app), _expected((20.0, 5.0, _slot(0))))


def test_t13_a_projected_weekly_high_leaves_the_debounce_armed(
        app, monkeypatch):
    """The symmetric weekly case. RED on the parent: the projected 20
    clears the reset-to-zero marker."""
    steps = [
        (0, 10.0, "statusline", {"plan": {}, "weekly": 20.0}),
        (60, 10.0, "statusline", {"plan": {}, "weekly": 0.0}),
        (120, 11.0, "statusline",
         {"plan": {"weekly_kind": "projection"}, "weekly": 20.0}),
        (180, 10.5, "statusline", {"plan": {}, "weekly": 0.0}),
    ]
    _run(app, monkeypatch, steps)
    worlds = _worlds(app, _weekly_resets)
    assert sum(worlds["live"].values()) == 1, worlds["live"]
    _assert_worlds(worlds, worlds["live"])


def test_t13_a_genuine_candidate_still_cancels(app, monkeypatch):
    _run(app, monkeypatch, [(0, 20.0, "statusline", CANDIDATE),
                            (60, 5.0, "statusline", CANDIDATE),
                            (180, 21.0, "statusline", CANDIDATE)])
    state = _state(app, "statusline")
    assert (state["baseline_pct"], state["pending_low_pct"]) == (21.0, None)
    _run(app, monkeypatch, [(240, 6.0, "statusline", CANDIDATE)])
    _assert_worlds(_worlds(app), collections.Counter())


def test_t13_observations_without_detector_policy_keep_projection_detection(
        app, monkeypatch):
    historical = {"plan": {"detector": MISSING}}
    filled = {"plan": {"detector": MISSING, "five_kind": "projection"},
              "weekly": 41.0}
    _run(app, monkeypatch, [(0, 20.0, "statusline", historical),
                            (60, 5.0, "statusline", historical),
                            (180, 20.0, "statusline", filled),
                            (240, 6.0, "statusline", historical)])
    _assert_worlds(_worlds(app), collections.Counter())


def test_t13_completion_executes_the_same_policy(app, monkeypatch):
    """Every reading appended without its ingest: the completing rebuild
    derives them all under their own recorded policy."""
    import _cctally_core as core
    _run(app, monkeypatch, [(0, 20.0, "statusline", CANDIDATE),
                            (60, 5.0, "statusline", CANDIDATE),
                            (180, 20.0, "statusline", FIVE_FILLED),
                            (240, 6.0, "statusline",
                             {"plan": {}, "weekly": 41.0})], ingest=False)
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = core.DB_PATH.with_name(core.DB_PATH.name + suffix)
        if path.exists():
            path.unlink()
    T.rebuild(app)
    assert _credits(app) == _expected((20.0, 5.0, _slot(0)))
