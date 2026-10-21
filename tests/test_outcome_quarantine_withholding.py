"""#825/#832 Task 3.4 — withhold every automatic outcome while quarantined
(spec 5.6 item 2).

For each outcome class — the >=25 pp in-place fire, the reset-to-zero debounce
confirmation, the five-hour source-local credit and the provider-moved
week-end transition — a quarantined account gets no event, no index row, no
stale-replica deletion, no floor or HWM change and no Model-A or harvest
append: the detector takes its no-outcome branch. Clearing detector state
never establishes recovery, outcomes minted before the trigger are kept, and
ordinary usage recording continues (C3).
"""
import json

import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
E1 = "2026-10-13T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"

#: name -> (pre-trigger steps, post-trigger steps producing the outcome)
CLASSES = {
    "fire": ([("current", dict(at="2026-10-09T10:00:00Z", weekly=70, week_end=E0))],
             [("current", dict(at="2026-10-09T10:20:00Z", weekly=40, week_end=E0))]),
    "zero": ([("current", dict(at="2026-10-09T10:00:00Z", weekly=20, week_end=E0))],
             [("current", dict(at="2026-10-09T10:10:00Z", weekly=0, week_end=E0)),
              ("current", dict(at="2026-10-09T10:20:00Z", weekly=0, week_end=E0))]),
    "five-hour": ([("current", dict(at="2026-10-09T10:00:00Z", weekly=5, week_end=E0,
                                    five=70, five_end=F0))],
                  [("current", dict(at="2026-10-09T10:10:00Z", weekly=5, week_end=E0,
                                    five=20, five_end=F0)),
                   ("current", dict(at="2026-10-09T10:20:00Z", weekly=5, week_end=E0,
                                    five=20, five_end=F0))]),
    "moved-end": ([("current", dict(at="2026-10-09T10:00:00Z", weekly=70, week_end=E0))],
                  [("current", dict(at="2026-10-09T10:20:00Z", weekly=10, week_end=E1))]),
}

OUTCOME_KINDS = {"week_reset", "five_hour_credit", "weekly_credit_effects"}


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _trigger(app, monkeypatch):
    T.legacy(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=1,
             week_end=E0, five=1, five_end=F0, account="B")


def _journal_kinds():
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    kinds = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec and rec.get("t") == "evt":
                kinds.append((rec.get("payload") or {}).get("kind"))
    return kinds


def _snapshots(app):
    return T._rows(app, "SELECT captured_at_utc, weekly_percent, five_hour_percent "
                        "FROM weekly_usage_snapshots ORDER BY captured_at_utc, id")


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_the_reference_run_mints_the_outcome(app, monkeypatch, name):
    """Non-vacuity: without the trigger the same readings mint the outcome."""
    pre, post = CLASSES[name]
    T.run_steps(app, monkeypatch, pre + post)
    assert T.outcome_count(T.outcomes(app)) == 1


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_quarantined_account_takes_the_no_outcome_branch(app, monkeypatch, name):
    import _cctally_core as core
    pre, post = CLASSES[name]
    T.run_steps(app, monkeypatch, pre)
    _trigger(app, monkeypatch)
    snapshots_before = _snapshots(app)
    kinds_before = _journal_kinds()
    hwm_before = {
        path: (core.APP_DIR / path).read_text() if (core.APP_DIR / path).exists() else None
        for path in ("hwm-7d", "hwm-5h")}
    T.run_steps(app, monkeypatch, post)
    # No event and no index row.
    assert T.outcome_count(T.outcomes(app)) == 0
    # No Model-A or harvest append of an outcome family.
    new_kinds = _journal_kinds()[len(kinds_before):]
    assert not OUTCOME_KINDS & set(new_kinds), new_kinds
    # No stale-replica deletion: every earlier snapshot row survives, and the
    # ordinary readings are still recorded (C3).
    after = _snapshots(app)
    assert all(row in after for row in snapshots_before)
    assert len(after) >= len(snapshots_before)
    assert T.quarantine_state(app)["triggers"]
    # No floor change and no forced HWM decrease.
    assert T._rows(app, "SELECT * FROM weekly_credit_floors") == []
    for path, text in hwm_before.items():
        now = (core.APP_DIR / path).read_text() if (core.APP_DIR / path).exists() else None
        # The ordinary monotonic writer may move to a NEW window key; within
        # one key a forced decrease would be the withheld credit's pivot.
        if (text is not None and now is not None
                and now.split()[0] == text.split()[0]):
            assert float(now.split()[-1]) >= float(text.split()[-1]), (path, text, now)


def test_clearing_detector_state_never_establishes_recovery(app, monkeypatch):
    pre, post = CLASSES["five-hour"]
    T.run_steps(app, monkeypatch, pre)
    _trigger(app, monkeypatch)
    T.run_steps(app, monkeypatch, post[:1])
    conn = app.open_db()
    try:
        conn.execute("DELETE FROM five_hour_credit_confirmation_state")
        conn.execute("DELETE FROM weekly_reset_debounce_state")
        conn.commit()
    finally:
        conn.close()
    T.run_steps(app, monkeypatch, [
        ("current", dict(at="2026-10-09T10:20:00Z", weekly=5, week_end=E0,
                         five=70, five_end=F0)),
        ("current", dict(at="2026-10-09T10:30:00Z", weekly=5, week_end=E0,
                         five=20, five_end=F0)),
        ("current", dict(at="2026-10-09T10:40:00Z", weekly=5, week_end=E0,
                         five=20, five_end=F0)),
    ])
    assert T.account_quarantined(app, "A")
    assert T.outcome_count(T.outcomes(app)) == 0


def test_outcomes_minted_before_the_trigger_are_kept(app, monkeypatch):
    pre, post = CLASSES["five-hour"]
    T.run_steps(app, monkeypatch, pre + post)
    before = T.outcomes(app)
    assert T.outcome_count(before) == 1
    _trigger(app, monkeypatch)
    T.run_steps(app, monkeypatch, [
        ("current", dict(at="2026-10-09T10:30:00Z", weekly=5, week_end=E0,
                         five=60, five_end=F0)),
        ("current", dict(at="2026-10-09T10:40:00Z", weekly=5, week_end=E0,
                         five=10, five_end=F0)),
        ("current", dict(at="2026-10-09T10:50:00Z", weekly=5, week_end=E0,
                         five=10, five_end=F0)),
    ])
    assert T.outcomes(app) == before
    assert json.loads(before["five_hour"][0])["post_percent"] == 20.0
