"""#825/#832 Task 3.8 — the quarantine seed set, case F12 (i)-(xv) (spec 5.6).

Every trace is a review-round counterexample to a window-local taint: after the
first post-cutover legacy write, every automatic outcome is withheld for every
account until a certified recovery, and none of these traces contains a
recovery witness. Unless stated: account A, source `api`, distinct observation
ids, and the legacy recorder paused after its real append and before its
ingest. Each trace is continued across a real rebuild, a real rederive and one
further reading after each. Outcomes are compared by logical identity, payload
and multiplicity; outcomes minted BEFORE the trigger are compared separately,
so withholding cannot hide a lost earlier outcome.
"""
import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
E1 = "2026-10-13T12:00:00Z"
E2 = "2026-10-14T12:00:00Z"
F14 = "2026-10-09T14:00:00Z"


def C(at, weekly, *, end=E0, five=None, five_end=F14, account="A", **extra):
    values = dict(at=f"2026-10-09T{at}:00Z" if len(at) == 5 else at,
                  weekly=weekly, week_end=end, account=account, **extra)
    if five is not None:
        values.update(five=five, five_end=five_end)
    return ("current", values)


def L(at, weekly, *, end=E0, five=None, five_end=F14, account=None, **extra):
    kind, values = C(at, weekly, end=end, five=five, five_end=five_end,
                     account=account, **extra)
    return ("legacy", values)


def _five(seq, *, weekly=5, start_minute=0, five_end=F14, hour=10):
    """Readings at ten-minute intervals from ``hour``:``start_minute``."""
    steps = []
    for index, item in enumerate(seq):
        minute = start_minute + 10 * index
        at = f"{hour + minute // 60:02d}:{minute % 60:02d}"
        kind, value = item
        maker = C if kind == "c" else L
        steps.append(maker(at, weekly, five=value, five_end=five_end))
    return steps


#: (id, pre-trigger steps, post-trigger steps (the first is the trigger), the
#: further readings after rebuild and rederive)
SEEDS = {
    # (i) current 70 -> 20 -> legacy 70 -> current 20.
    "i": (_five([("c", 70), ("c", 20)]),
          _five([("l", 70), ("c", 20)], start_minute=20),
          (C("11:00", 5, five=20), C("11:10", 5, five=20))),
    # (ii) current 70 -> 20 -> legacy 70 -> current 60 -> 50 -> 55.
    "ii": (_five([("c", 70), ("c", 20)]),
           _five([("l", 70), ("c", 60), ("c", 50), ("c", 55)], start_minute=20),
           (C("11:20", 5, five=50), C("11:30", 5, five=50))),
    # (iii) a credit confirmed BEFORE the trigger in the same window, kept;
    # then a second descent after the trigger is withheld.
    "iii": (_five([("c", 70), ("c", 20), ("c", 20)]),
            _five([("l", 70), ("c", 60), ("c", 10), ("c", 10)], start_minute=30),
            (C("11:20", 5, five=10), C("11:30", 5, five=10))),
    # (iv-a) a >=25 pp in-place fire after the trigger in the same week.
    "iv-fire": ([C("10:00", 70)],
                [L("10:10", 70), C("10:20", 40)],
                (C("10:30", 40), C("10:40", 40))),
    # (iv-b) the reset-to-zero debounce outcome after the trigger.
    "iv-zero": ([C("10:00", 20)],
                [L("10:05", 20), C("10:10", 0), C("10:20", 0)],
                (C("10:30", 0), C("10:40", 0))),
    # (v) a provider-moved week end whose first reading is excluded.
    "v": ([C("10:00", 70)],
          [L("10:10", 10, end=E1), C("10:20", 10, end=E1)],
          (C("10:30", 10, end=E1), C("10:40", 10, end=E1))),
    # (vi) a second account B with a live window at the trigger that wrote
    # nothing at the time; nothing is minted for B.
    "vi": ([C("10:00", 5, five=70), C("10:05", 9, five=70, account="B")],
           [L("10:10", 5, five=70), C("10:20", 9, five=20, account="B"),
            C("10:30", 9, five=20, account="B")],
           (C("10:40", 9, five=20, account="B"),
            C("10:50", 9, five=20, account="B"))),
    # (vii) the raw-reset boundary tail: raw reset 14:09 (canonical 14:00).
    "vii": ([C("13:57", 5, five=70, five_end="2026-10-09T14:09:00Z"),
             C("13:58", 5, five=20, five_end="2026-10-09T14:09:00Z")],
            [L("14:01", 5, five=70, five_end="2026-10-09T14:09:00Z"),
             C("14:02", 5, five=60, five_end="2026-10-09T14:09:00Z"),
             C("14:03", 5, five=50, five_end="2026-10-09T14:09:00Z"),
             C("14:04", 5, five=55, five_end="2026-10-09T14:09:00Z")],
            (C("14:05", 5, five=55, five_end="2026-10-09T14:09:00Z"),
             C("14:06", 5, five=55, five_end="2026-10-09T14:09:00Z"))),
    # (viii) consecutive week-end moves, the middle one excluded: (A, E0, E2)
    # must never exist.
    "viii": ([C("10:00", 70, end=E0)],
             [L("10:10", 40, end=E1), C("10:20", 10, end=E2)],
             (C("10:30", 10, end=E2), C("10:40", 10, end=E2))),
    # (ix) after a withheld transition, a >=25 pp in-place fire in the new
    # week whose interval does not contain the legacy capture instant.
    "ix": ([C("10:00", 70, end=E0)],
           [L("10:10", 40, end=E1), C("10:20", 10, end=E2),
            C("10:30", 50, end=E2), C("10:40", 20, end=E2)],
           (C("10:50", 20, end=E2), C("11:00", 20, end=E2))),
    # (x) a legacy reading captured earlier in the predecessor's week (before
    # the predecessor) followed by a current moved week end.
    "x": ([C("10:00", 70, end=E0)],
          [L("10:05", 30, end=E0, captured="2026-10-09T09:00:00Z"),
           C("10:10", 10, end=E1)],
          (C("10:20", 10, end=E1), C("10:30", 10, end=E1))),
    # (xi) a five-hour legacy reading captured before any capture-time
    # interval: raw reset 14:00 throughout.
    "xi": ([C("08:40", 5, five=70)],
           [L("08:45", 5, five=80), C("09:00", 5, five=70),
            C("09:10", 5, five=20), C("09:20", 5, five=25)],
           (C("09:30", 5, five=25), C("09:40", 5, five=25))),
    # (xii) a weekly legacy reading captured before the week's nominal start:
    # fixed raw weekly reset E, current 40 at E-7d-3h, legacy 70 at E-7d-2h,
    # current 50 at E-7d-30m, current 20 at E-7d+10m.
    "xii": ([C("2026-10-09T09:00:00Z", 40, end="2026-10-16T12:00:00Z")],
            [L("2026-10-09T10:00:00Z", 70, end="2026-10-16T12:00:00Z"),
             C("2026-10-09T11:30:00Z", 50, end="2026-10-16T12:00:00Z"),
             C("2026-10-09T12:10:00Z", 20, end="2026-10-16T12:00:00Z")],
            (C("2026-10-09T12:20:00Z", 20, end="2026-10-16T12:00:00Z"),
             C("2026-10-09T12:30:00Z", 20, end="2026-10-16T12:00:00Z"))),
    # (xiii) a delayed legacy append: captured 70 at 09:00 with raw reset
    # 14:00, appended at 13:00; current 70 at 12:00 then 60, 50, 55 with raw
    # reset 17:00. Round 5 withdrew the promise that the 17:00 window stays
    # available.
    "xiii": ([C("12:00", 5, five=70, five_end="2026-10-09T17:00:00Z")],
             [L("13:00", 5, five=70, captured="2026-10-09T09:00:00Z"),
              C("13:10", 5, five=60, five_end="2026-10-09T17:00:00Z"),
              C("13:20", 5, five=50, five_end="2026-10-09T17:00:00Z"),
              C("13:30", 5, five=55, five_end="2026-10-09T17:00:00Z")],
             (C("13:40", 5, five=55, five_end="2026-10-09T17:00:00Z"),
              C("13:50", 5, five=55, five_end="2026-10-09T17:00:00Z"))),
    # (xiv) round 5, cross-window predecessor; weekly percentages 1..7 so
    # every observation materializes.
    "xiv": ([C("12:00", 1, five=70, five_end="2026-10-09T17:00:00Z"),
             C("12:10", 2, five=60, five_end="2026-10-09T17:00:00Z")],
            [L("12:15", 3, five=70, five_end="2026-10-09T14:00:00Z"),
             C("12:20", 3, five=50, five_end="2026-10-09T17:00:00Z"),
             C("12:30", 4, five=55, five_end="2026-10-09T17:00:00Z"),
             C("12:40", 5, five=50, five_end="2026-10-09T17:00:00Z"),
             C("12:50", 6, five=52, five_end="2026-10-09T17:00:00Z")],
            (C("13:00", 7, five=52, five_end="2026-10-09T17:00:00Z"),
             C("13:10", 7, five=52, five_end="2026-10-09T17:00:00Z"))),
    # (xv) round 5, shared date key, no five-hour fields.
    "xv": ([C("12:00", 10, end="2026-10-16T12:00:00Z")],
           [L("12:01", 70, end="2026-10-15T12:00:00Z"),
            C("12:02", 50, end="2026-10-15T18:00:00Z"),
            C("12:03", 20, end="2026-10-15T18:00:00Z")],
           (C("12:04", 20, end="2026-10-15T18:00:00Z"),
            C("12:05", 20, end="2026-10-15T18:00:00Z"))),
}


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


@pytest.mark.parametrize("seed", sorted(SEEDS))
def test_f12_seed_withholds_every_outcome_after_the_trigger(app, monkeypatch, seed):
    pre, post, further = SEEDS[seed]
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    T.run_steps(app, monkeypatch, pre)
    before = T.outcomes(app)
    if seed == "iii":
        # The credit confirmed before the trigger exists...
        assert len(before["five_hour"]) == 1, before
    T.run_steps(app, monkeypatch, post)
    # ...and survives with identical identity and payload, while nothing is
    # minted after the trigger, for any account, in any phase.
    assert T.outcomes(app) == before, ("live", T.outcomes(app), before)
    result = T.continue_through_maintenance(app, monkeypatch, further=further)
    for phase in ("rebuilt", "after_rebuild", "after_rederive"):
        assert result[phase] == before, (phase, result[phase], before)
    assert result["live_state"]["triggers"], "the legacy write is a trigger"
    for name in ("A", "B", "unattributed"):
        assert T.account_quarantined(app, name), name
