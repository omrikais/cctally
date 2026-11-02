"""#825/#832 case F17 — the bounded world-enumeration oracle (spec section 9;
section 5.6; constraint C5). The harness is `tests/_world_enumeration.py`.

Thirteen skeleton families of at most eight readings with fixed causal roles
and two excluded slots run through the real pipeline. Per family and variant
(two account orders x two source arrangements x two raw-reset offsets) the
two excluded slots take pairs of four representative values, and each mixed
trace is checked against its 16 reference worlds (each excluded reading
absent, A, B or `unattributed`) at every processed prefix, across a real
rebuild and a real rederive each followed by another reading. The gate runs
`_world_enumeration.GATE_PAIRS` (every value in each slot once: 32 mixed
traces and 544 checked executions per family); `CCTALLY_F17_FULL=1` runs the
full product of spec section 9 (every pair: 128 mixed traces and 2,176
executions per family). Representative values come from repository
constants, never hand-copied thresholds.

Every reference world runs the mixed trace's real rebuild and rederive at the
same positions (fix round O4, review CD5-O01); the arm/rebuild/confirm
mutation pins that a mixed rebuild restoring the disposable five-hour
confirmation state fails.

Non-vacuity: every family's no-exclusion world (both slots read as the
primary account's current readings) mints the family's genuine outcomes over
the uninterrupted schedule (`_uninterrupted_no_exclusion`), and
every family has a certified-recovery variant whose MIXED trace must mint the
genuine post-recovery credit — an implementation that withholds everything
fails there. Every F12 seed and the round-6 seed of F14 run as fixed seeds,
and the round-5 seeds must fail under a deliberately broken gate. Drop
consensus is enumerated at the reducer level (`reduce_candidates`) with the
legacy candidate absent, A, B or `unattributed`.
"""
from __future__ import annotations

import collections
import itertools

import pytest

import _cctally_weekrefs as W
import _lib_five_hour
import _lib_outcome_quarantine as Q
import _outcome_quarantine_trace as T
import _world_enumeration as WE

#: Threshold and tolerance constants, read from the repository.
FIRE = W._RESET_PCT_DROP_THRESHOLD
FIVE_DROP = W._FIVE_HOUR_RESET_PCT_DROP_THRESHOLD
ZERO_FLOOR = W._RESET_ZERO_FLOOR_PCT
ZERO_MIN = W._RESET_ZERO_MIN_DROP_PCT
JITTER = _lib_five_hour._FIVE_HOUR_JITTER_FLOOR_SECONDS
BLOCK_SEARCH = Q.TOLERANCE_BLOCK_SEARCH_S


def _record_constants():
    import _cctally_record as R
    return (R._RECORD_USAGE_WEEK_PAST_SLACK_S, R._RECORD_USAGE_WEEK_FUTURE_BAND_S,
            R._RECORD_USAGE_5H_FUTURE_BAND_S)


DAY = "2026-10-09"
E0 = "2026-10-12T12:00:00Z"
E1 = "2026-10-13T12:00:00Z"
E2 = "2026-10-14T12:00:00Z"
F14 = f"{DAY}T14:00:00Z"
F17 = f"{DAY}T17:00:00Z"


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00Z"


def r(role, hhmm, weekly, *, end=E0, five=None, five_end=F14, **extra):
    values = dict(at=at(hhmm), weekly=weekly, week_end=end, **extra)
    if five is not None:
        values.update(five=five, five_end=five_end)
    return (role, values)


def _plus(iso_text, seconds):
    return T.iso(T.epoch(iso_text) + int(seconds))


def _families():
    week_past, week_future, five_future = _record_constants()
    ten = at("10:00")
    return [
        WE.Skeleton(
            "five-hour-credit",
            ("baseline", "arm", "legacy", "confirm", "legacy", "continue"),
            (r("P", "10:00", 5, five=70), r("P", "10:10", 5, five=20),
             r("X", "10:15", 5, five=20), r("P", "10:20", 5, five=20),
             r("X", "10:25", 5, five=20), r("P", "10:30", 6, five=25)),
            ({}, {"five": 70}, {"five": 20 - FIVE_DROP},
             {"weekly": 80, "five": 95}),
            (r("P", "11:00", 6, five=25), r("P", "11:10", 6, five=25)),
            genuine=1),
        WE.Skeleton(
            "weekly-fire-edge",
            ("baseline", "legacy", "fire", "legacy", "continue", "continue"),
            (r("P", "10:00", 70), r("X", "10:05", 70),
             r("P", "10:10", 70 - FIRE), r("X", "10:15", 70 - FIRE),
             r("P", "10:20", 70 - FIRE + 1), r("P", "10:30", 70 - FIRE + 2)),
            ({}, {"weekly": 70 - FIRE + 0.5}, {"weekly": 0}, {"weekly": 95}),
            (r("P", "10:40", 70 - FIRE + 2), r("P", "10:50", 70 - FIRE + 2)),
            genuine=1),
        WE.Skeleton(
            "reset-to-zero-debounce",
            ("baseline", "legacy", "arm", "legacy", "confirm", "continue"),
            (r("P", "10:00", 20), r("X", "10:05", 20), r("P", "10:10", 0),
             r("X", "10:15", 0), r("P", "10:20", 0), r("P", "10:30", 1)),
            ({}, {"weekly": ZERO_FLOOR}, {"weekly": ZERO_MIN}, {"weekly": 50}),
            (r("P", "10:40", 1), r("P", "10:50", 1)),
            genuine=1),
        WE.Skeleton(
            "moved-week-end",
            ("baseline", "legacy", "transition", "legacy", "continue",
             "continue"),
            (r("P", "10:00", 70), r("X", "10:05", 70), r("P", "10:10", 10, end=E1),
             r("X", "10:15", 10, end=E1), r("P", "10:20", 11, end=E1),
             r("P", "10:30", 12, end=E1)),
            ({}, {"weekly": 40, "week_end": E1}, {"weekly": 10, "week_end": E2},
             {"weekly": 90, "week_end": E0}),
            (r("P", "10:40", 12, end=E1), r("P", "10:50", 12, end=E1)),
            genuine=1),
        WE.Skeleton(
            "shared-date-different-end",
            ("baseline", "legacy", "moved", "fire", "legacy", "continue"),
            (r("P", "12:00", 10, end="2026-10-16T12:00:00Z"),
             r("X", "12:01", 70, end="2026-10-15T12:00:00Z"),
             r("P", "12:02", 50, end="2026-10-15T18:00:00Z"),
             r("P", "12:03", 20, end="2026-10-15T18:00:00Z"),
             r("X", "12:04", 20, end="2026-10-15T18:00:00Z"),
             r("P", "12:05", 21, end="2026-10-15T18:00:00Z")),
            ({}, {"week_end": "2026-10-16T12:00:00Z"}, {"weekly": 5},
             {"weekly": 50, "week_end": "2026-10-15T18:00:00Z"}),
            (r("P", "12:10", 21, end="2026-10-15T18:00:00Z"),
             r("P", "12:20", 21, end="2026-10-15T18:00:00Z")),
            genuine=1),
        WE.Skeleton(
            "raw-canonical-tail",
            ("baseline", "arm", "legacy", "confirm", "legacy", "continue"),
            (r("P", "13:57", 5, five=70, five_end=at("14:09")),
             r("P", "13:58", 5, five=20, five_end=at("14:09")),
             r("X", "14:01", 5, five=20, five_end=at("14:09")),
             r("P", "14:02", 5, five=20, five_end=at("14:09")),
             r("X", "14:03", 5, five=20, five_end=at("14:09")),
             r("P", "14:04", 5, five=55, five_end=at("14:09"))),
            ({}, {"five": 70},
             {"five": 70, "five_end": _plus(at("14:09"), -9 * 60)},
             {"five": 5, "five_end": _plus(at("14:09"), 5 * 3600)}),
            (r("P", "14:05", 5, five=55, five_end=at("14:09")),
             r("P", "14:06", 5, five=55, five_end=at("14:09"))),
            genuine=1),
        WE.Skeleton(
            "ingress-6h-8d-edges",
            ("baseline", "legacy", "arm", "legacy", "confirm", "fire"),
            (r("P", "10:00", 30, five=40, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("X", "10:05", 30, five=40, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("P", "10:10", 30, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("X", "10:15", 30, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("P", "10:20", 30, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("P", "10:30", 30 - FIRE, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future))),
            ({}, {"five_end": _plus(at("10:05"), five_future + JITTER)},
             {"week_end": _plus(at("10:05"), week_future + 3600)},
             {"five": 20, "five_end": _plus(ten, five_future - 60)}),
            (r("P", "10:40", 30 - FIRE, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future)),
             r("P", "10:50", 30 - FIRE, five=20, five_end=_plus(ten, five_future),
               end=_plus(ten, week_future))),
            genuine=2),
        WE.Skeleton(
            "historical-30-day",
            ("baseline", "legacy", "fire", "legacy", "continue", "continue"),
            (r("P", "10:00", 70), r("X", "10:05", 70), r("P", "10:10", 40),
             r("X", "10:15", 40), r("P", "10:20", 41), r("P", "10:30", 42)),
            ({}, {"weekly": 50, "week_end": _plus(ten, -week_past + 2 * 86400)},
             {"weekly": 10, "week_end": _plus(ten, -week_past + 86400)},
             {"weekly": 90, "week_end": _plus(ten, -week_past + 3600)}),
            (r("P", "10:40", 42), r("P", "10:50", 42)),
            genuine=1),
        WE.Skeleton(
            "capture-append-reorder",
            ("baseline", "late-legacy", "arm", "late-legacy", "confirm",
             "continue"),
            (r("P", "12:00", 5, five=70, five_end=F17),
             r("X", "13:00", 5, five=70, five_end=F14, captured=at("09:00")),
             r("P", "13:10", 5, five=60, five_end=F17),
             r("X", "13:15", 5, five=60, five_end=F17, captured=at("12:30")),
             r("P", "13:20", 5, five=60, five_end=F17),
             r("P", "13:30", 6, five=61, five_end=F17)),
            ({}, {"five_end": F17}, {"five": 30, "five_end": F17},
             {"weekly": 50, "five": 95, "five_end": F14}),
            (r("P", "13:40", 6, five=61, five_end=F17),
             r("P", "13:50", 6, five=61, five_end=F17)),
            genuine=1),
        WE.Skeleton(
            "both-sources",
            ("baseline", "other-source", "legacy", "arm", "legacy", "confirm"),
            (r("P", "10:00", 5, five=70, source="api"),
             r("P", "10:05", 5, five=70, source="statusline"),
             r("X", "10:08", 5, five=20, source="api"),
             r("P", "10:10", 5, five=20, source="api"),
             r("X", "10:15", 5, five=20, source="api"),
             r("P", "10:20", 5, five=20, source="api")),
            ({}, {"five": 70, "source": "statusline"},
             {"five": 20, "source": "statusline"}, {"five": 95}),
            (r("P", "10:30", 5, five=20, source="statusline"),
             r("P", "10:40", 5, five=20, source="api")),
            genuine=1),
        WE.Skeleton(
            "held-rows",
            ("baseline", "held", "legacy", "arm", "legacy", "confirm"),
            (r("P", "10:00", 50, five=30), r("P", "10:10", 48, five=40),
             r("X", "10:15", 48, five=40), r("P", "10:20", 48, five=20),
             r("X", "10:25", 48, five=20), r("P", "10:30", 48, five=20)),
            ({}, {"weekly": 52}, {"five": 10}, {"weekly": 20, "five": 90}),
            (r("P", "10:40", 48, five=20), r("P", "10:50", 48, five=20)),
            genuine=1),
        WE.Skeleton(
            "cross-window-predecessor",
            ("baseline", "arm", "legacy", "confirm", "legacy", "continue"),
            (r("P", "12:00", 1, five=70, five_end=F17),
             r("P", "12:10", 2, five=50, five_end=F17),
             r("X", "12:15", 3, five=70, five_end=F14),
             r("P", "12:20", 3, five=50, five_end=F17),
             r("X", "12:25", 3, five=70, five_end=F14),
             r("P", "12:30", 4, five=55, five_end=F17)),
            ({}, {"five_end": F17}, {"five": 10},
             {"five": 95, "five_end": _plus(at("12:15"), BLOCK_SEARCH)}),
            (r("P", "12:40", 5, five=55, five_end=F17),
             r("P", "12:50", 6, five=55, five_end=F17)),
            genuine=1),
        WE.Skeleton(
            "second-account-live-window",
            ("baseline", "other-account", "legacy", "arm", "legacy", "confirm"),
            (r("P", "10:00", 5, five=70), r("Q", "10:05", 9, five=70),
             r("X", "10:10", 5, five=70), r("Q", "10:20", 9, five=20),
             r("X", "10:25", 9, five=20), r("Q", "10:30", 9, five=20)),
            ({}, {"weekly": 9}, {"weekly": 9, "five": 20},
             {"weekly": 60, "five": 95}),
            (r("Q", "10:40", 9, five=20), r("Q", "10:50", 9, five=20)),
            genuine=1),
    ]


FAMILIES = {skel.name: skel for skel in _families()}


# --------------------------------------------------------------------------
# scheduling: the split by gate pair and the four xdist groups
# --------------------------------------------------------------------------
#
# The release candidate's product leg (run 39772699752) ended incomplete: at
# the tail of `--dist load`, four to eight of this module's heavy cases ran at
# once on a disk-bound runner, each died at the 120-second per-test cap
# (pytest-timeout's thread method takes the worker with it), and every
# requeue put several heavy cases back at once. Two changes answer it, and
# neither drops a check.
#
# The split. `test_f17_family` and `test_f17_certified_recovery_mints_the_
# genuine_outcome` each run half of their former pairs per case (`_split`),
# so the slowest case halves. The two halves of a function partition exactly
# the pairs one former case ran, in the gate and under `CCTALLY_F17_FULL=1`
# alike (`test_f17_splits_partition_the_former_pairs` pins that), and the
# family's no-exclusion non-vacuity reference runs once, in half 0, which
# always holds the (0, 0) pair.
#
# The groups. tests/conftest.py marks every item of a module that defines
# `XDIST_GROUP_COUNT` with one of that many `xdist_group`s, balanced by
# `xdist_case_cost` (longest first). `bin/cctally-test-all` runs the pytest
# phase under `--dist loadgroup`, where each group is one work unit on one
# worker, so at most four of this module's cases run at once, and xdist's
# default reorder dispatches the large units first, so the tail is ordinary
# short tests.
SPLITS = (0, 1)
XDIST_GROUP_COUNT = 4
XDIST_GROUP_PREFIX = "f17"

#: Estimated seconds per case, from the lan scheduled run 39724629894 (22
#: October, before the split, with every F17 node present): `test_f17_family`
#: 2,326 s over 104 cases and the recovery cases 1,295 s over 26, each halved
#: by the split; the singletons as measured. The figures only balance the
#: groups; a wrong one costs balance, never coverage.
_CASE_COST_S = {
    "test_f17_family": 11.0,
    "test_f17_certified_recovery_mints_the_genuine_outcome": 25.0,
    "test_f17_lazy_reference_horizon_is_exact": 50.0,
    "test_f17_trie_execution_matches_fresh_stores": 47.0,
    "test_f17_rejects_restored_confirmation_state_across_a_rebuild": 36.0,
    "test_f17_rejects_a_mixed_only_credit_detection_instant": 23.0,
    "test_f17_fixed_seeds": 17.0,
}
_DEFAULT_CASE_COST_S = 2.0


def xdist_case_cost(item) -> float:
    return _CASE_COST_S.get(getattr(item, "originalname", item.name), _DEFAULT_CASE_COST_S)


def _split(pairs, split):
    """Every other pair from ``split``: the halves partition ``pairs``, and
    half 0 holds the first pair, which is (0, 0) in the gate and in the full
    product alike."""
    return tuple(pairs)[split::2]


def _family_pairs(split, count):
    """Half of a family's value pairs. ``count`` is the family's own number of
    values (`len(skel.values)`), the count `WE.mixed_traces` itself enumerates
    over, so a family with another number of values cannot silently run a
    different enumeration from the one these halves partition (review I3c F7)."""
    return _split(WE.value_pairs(count), split)


def _recovery_pairs(split, count):
    """Half of what one former recovery case ran: the gate ran the
    no-exclusion pair and one pair with both slots non-trivial, and
    `CCTALLY_F17_FULL=1` ran every pair."""
    every = WE.value_pairs(count)
    if len(every) > len(WE.GATE_PAIRS):
        return _split(every, split)
    return ((WE.GATE_PAIRS[0], WE.GATE_PAIRS[2])[split],)


@pytest.mark.parametrize("full", [False, True])
def test_f17_splits_partition_the_former_pairs(monkeypatch, full):
    """The union of the halves is exactly what one former case ran, with no
    pair run twice, and the non-vacuity pair (0, 0) is in half 0."""
    if full:
        monkeypatch.setenv("CCTALLY_F17_FULL", "1")
    else:
        monkeypatch.delenv("CCTALLY_F17_FULL", raising=False)
    # Every family's own count, and one no family has yet, so the halves are
    # shown to follow the count rather than a fixed four.
    counts = sorted({len(skel.values) for skel in FAMILIES.values()} | {5})
    for count in counts:
        family = [pair for split in SPLITS for pair in _family_pairs(split, count)]
        assert sorted(family) == sorted(WE.value_pairs(count)), count
        assert len(set(family)) == len(family), count
        assert (0, 0) in _family_pairs(0, count)
        former_recovery = (WE.value_pairs(count) if full
                           else (WE.GATE_PAIRS[0], WE.GATE_PAIRS[2]))
        recovery = [pair for split in SPLITS for pair in _recovery_pairs(split, count)]
        assert sorted(recovery) == sorted(former_recovery), count
        assert len(set(recovery)) == len(recovery), count
        assert all(_family_pairs(split, count) and _recovery_pairs(split, count)
                   for split in SPLITS), count
    if full:
        assert len(_family_pairs(0, 5)) + len(_family_pairs(1, 5)) == 25


def _vid(variant):
    return f"o{variant.order}-{variant.source}-{'jitter' if variant.offset else 'aligned'}"


@pytest.fixture
def store(monkeypatch, tmp_path):
    return WE.Store(monkeypatch, tmp_path)


def _uninterrupted_no_exclusion(store, mixed_steps, variant):
    """The no-exclusion world (both slots read as the primary account's
    current readings) with its rebuild and rederive positions left empty.

    Fix round O4 (review CD5-O01): the oracle's reference worlds run the
    mixed trace's maintenance, but a family's positive control is its
    genuine outcome over the UNINTERRUPTED schedule. The rebuild drops the
    disposable detector state (the admitted five-hour cold-start gap), and
    `cross-window-predecessor`'s only genuine credit in this world is armed
    by a reading after the rebuild, so the maintained world holds none."""
    primary = "A" if variant.order == 0 else "B"
    seq = [("skip", {}) if kind in ("rebuild", "rederive") else (kind, values)
           for kind, values in WE.world_steps(mixed_steps, (primary, primary))]
    [result] = store.run_all([seq])
    return result


@pytest.mark.parametrize("split", SPLITS, ids=lambda split: f"half{split}")
@pytest.mark.parametrize("variant", WE.VARIANTS, ids=_vid)
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_f17_family(store, family, variant, split):
    skel = FAMILIES[family]
    traces = WE.mixed_traces(skel, variant, pairs=_family_pairs(split, len(skel.values)))
    WE.run_and_check(store, traces)
    if split != 0:
        return
    # The non-vacuity reference runs once per (family, variant), here.
    label = next(label for label in traces if label.endswith("/00"))
    reference = _uninterrupted_no_exclusion(store, traces[label], variant)
    assert WE.outcome_total(reference[-1]) >= skel.genuine, (
        f"non-vacuity: {family} mints its genuine outcomes without exclusion",
        reference[-1])


RECOVERY_VARIANTS = (WE.Variant(0, "api", 0), WE.Variant(1, "statusline", -1))


@pytest.mark.parametrize("split", SPLITS, ids=lambda split: f"half{split}")
@pytest.mark.parametrize("variant", RECOVERY_VARIANTS, ids=_vid)
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_f17_certified_recovery_mints_the_genuine_outcome(store, family, variant, split):
    """The mixed trace, quarantined by its first excluded reading, recovers at
    the idle-gap witness and then mints the genuine credit — which every
    reference world mints too (`check`)."""
    skel = FAMILIES[family]
    # Two of the gate pairs (the no-exclusion pair and one with both slots
    # non-trivial) carry the recovery suffix, one per half; `CCTALLY_F17_FULL=1`
    # runs every pair, half in each (`_recovery_pairs`).
    traces = WE.mixed_traces(skel, variant, recovery=True,
                             pairs=_recovery_pairs(split, len(skel.values)))
    results = WE.run_and_check(store, traces)
    recovery_start = len(skel.steps)
    for label, (mixed, _worlds) in results.items():
        before = collections.Counter(mixed[recovery_start - 1]["five_hour"])
        after = collections.Counter(mixed[-1]["five_hour"])
        assert sum((after - before).values()) == WE.RECOVERY_GENUINE, (
            label, mixed[recovery_start - 1], mixed[-1])


# --------------------------------------------------------------------------
# fixed seeds
# --------------------------------------------------------------------------

def _f12_seeds():
    from test_outcome_quarantine_seeds_f12 import SEEDS
    return SEEDS


def _seed_steps(pre, post, further):
    return (list(pre) + list(post)
            + [("rebuild", {}), further[0], ("rederive", {}), further[1]])


ROUND_SIX = [
    ("current", dict(at="2026-10-09T12:00:00Z", weekly=70,
                     week_end="2026-10-16T12:00:00Z", five=70,
                     five_end="2026-10-09T17:00:00Z", account="A")),
    ("legacy", dict(at="2026-10-09T12:01:00Z", weekly=80,
                    week_end="2026-10-08T12:00:00Z", five=80,
                    five_end="2026-10-09T12:00:00Z")),
    ("current", dict(at="2026-10-09T12:02:00Z", weekly=10,
                     week_end="2026-10-15T12:00:00Z", five=10,
                     five_end="2026-10-09T18:00:00Z", account="A")),
    ("rebuild", {}),
    ("current", dict(at="2026-10-09T12:03:00Z", weekly=10,
                     week_end="2026-10-15T12:00:00Z", five=10,
                     five_end="2026-10-09T18:00:00Z", account="A")),
    ("rederive", {}),
    ("current", dict(at="2026-10-09T12:04:00Z", weekly=10,
                     week_end="2026-10-15T12:00:00Z", five=10,
                     five_end="2026-10-09T18:00:00Z", account="A")),
]


def test_f17_fixed_seeds(store):
    """Every F12 seed and the F14 round-6 whole-prefix seed, with all of
    their reference worlds."""
    traces = {f"f12/{name}": _seed_steps(*seed)
              for name, seed in sorted(_f12_seeds().items())}
    traces["f14/round-6"] = ROUND_SIX
    WE.run_and_check(store, traces)


@pytest.mark.parametrize("seed", ["xiv", "xv"])
def test_f17_rejects_a_broken_gate(store, monkeypatch, seed):
    """The oracle is not vacuous: with a gate that always allows, the round-5
    seeds mint an outcome some reference world lacks."""
    import _cctally_quarantine
    monkeypatch.setattr(_cctally_quarantine, "quarantine_gate",
                        lambda ctx, account_key, reading=None: Q.GATE_NO_TRIGGER)
    traces = {f"f12/{seed}": _seed_steps(*_f12_seeds()[seed])}
    with pytest.raises(WE.OracleViolation):
        WE.run_and_check(store, traces)


def test_f17_trie_execution_matches_fresh_stores(monkeypatch, tmp_path):
    """Prefix sharing is an optimization only: running each sequence in its
    own fresh store gives the same per-step outcomes."""
    traces = WE.mixed_traces(FAMILIES["five-hour-credit"], WE.VARIANTS[0])
    picked = {label: steps for label, steps in traces.items()
              if label.endswith(("/00", "/12"))}
    sequences = []
    for steps in picked.values():
        sequences.append(steps)
        for world in itertools.islice(WE.worlds(steps), 0, 16, 5):
            sequences.append(WE.world_steps(steps, world))
    shared = WE.Store(monkeypatch, tmp_path / "shared").run_all(sequences)
    for index, seq in enumerate(sequences):
        alone = WE.Store(monkeypatch, tmp_path / f"alone{index}").run_all([seq])
        assert alone[0] == shared[index], index


# --------------------------------------------------------------------------
# drop consensus at the reducer level
# --------------------------------------------------------------------------
# (see test_f17_drop_consensus_* below)


def test_f17_lazy_reference_horizon_is_exact(store):
    """Fix round O4: `run_and_check` stops a reference world at its first
    maintenance step only when the mixed trace holds no outcome from there
    on. Every position it does run equals the full world's, every position
    it skips constrains nothing, and the full worlds give the same verdict;
    a trace whose mixed side keeps an outcome runs its worlds to the end."""
    traces = WE.mixed_traces(FAMILIES["five-hour-credit"], WE.VARIANTS[0],
                             pairs=((1, 2),))
    traces.update({"detected/in_place": _credit_before_exclusion("in_place")})
    results = WE.run_and_check(store, traces)
    skipped = 0
    for label, (mixed, per_world) in results.items():
        steps = traces[label]
        full = dict(zip(per_world, store.run_all(
            [WE.world_steps(steps, world) for world in per_world])))
        WE.check(steps, mixed, full, label=label)
        for world, lazy in per_world.items():
            for position, snapshot in enumerate(lazy):
                if snapshot is None:
                    skipped += 1
                    assert WE.outcome_total(mixed[position]) == 0
                    assert position >= WE.reference_horizon(steps, mixed)
                else:
                    assert snapshot == full[world][position], (label, world)
    assert skipped, "non-vacuity: the withheld trace skipped its worlds' tails"
    _mixed, kept = results["detected/in_place"]
    assert all(None not in result for result in kept.values())


def test_f17_store_outcomes_match_the_trace_harness(store):
    """`Store.outcomes` is `T.outcomes` over one connection."""
    traces = WE.mixed_traces(FAMILIES["weekly-fire-edge"], WE.VARIANTS[0])
    store.run_all([WE.world_steps(next(iter(traces.values())), ("A", "A"))])
    assert store.outcomes() == T.outcomes(store.app)
    assert WE.outcome_total(store.outcomes()) >= 1


def _credit_before_exclusion(category):
    """A credit of ``category`` minted before an excluded reading, then
    readings across a real rebuild and a real rederive: every world holds
    the credit with the mixed trace's own identity and payload."""
    def current(hhmm, weekly, five=None):
        values = dict(at=at(hhmm), weekly=weekly, week_end=E0, account="A",
                      source="api")
        if five is not None:
            values.update(five=five, five_end=F14)
        return ("current", values)

    def legacy(hhmm, weekly, five=None):
        kind, values = current(hhmm, weekly, five)
        return ("legacy", dict(values, account="B"))

    if category == "five_hour":
        head = [current("10:00", 5, 70), current("10:10", 5, 20),
                current("10:20", 5, 20), legacy("10:25", 5, 20)]
        tail = [current("10:30", 6, 21), ("rebuild", {}),
                current("10:40", 6, 21), ("rederive", {}),
                current("10:50", 6, 21)]
    else:
        head = [current("10:00", 70), current("10:10", 70 - FIRE),
                legacy("10:15", 70 - FIRE)]
        tail = [current("10:20", 70 - FIRE + 1), ("rebuild", {}),
                current("10:30", 70 - FIRE + 1), ("rederive", {}),
                current("10:40", 70 - FIRE + 1)]
    return head + tail


@pytest.mark.parametrize("category", ["five_hour", "in_place"])
def test_f17_rejects_a_mixed_only_credit_detection_instant(
        store, monkeypatch, category):
    """Fix round O (review CD4-O02): a credit is compared by its COMPLETE
    accounting payload. A mixed-only mutation that moves a credit's
    `detected_at_utc` by a minute — the instant the five-hour resolver
    orders and generates by — while keeping its identity, amounts,
    effective instant and multiplicity, and that ingest, rebuild and
    rederive all reproduce, fails the world-enumeration oracle."""
    traces = {f"detected/{category}": _credit_before_exclusion(category)}
    # Unmutated, the real pipeline passes and the mixed trace keeps its
    # credit through rebuild and rederive.
    [(mixed, _worlds)] = WE.run_and_check(store, traces).values()
    assert len(mixed[-1][category]) == 1, mixed[-1]
    mutated: dict = {}
    real_apply = WE.Store.apply

    def apply(self, step):
        real_apply(self, step)
        if step[0] not in ("legacy", "skip"):
            T.shift_credit_detection(self.app, mutated, category)

    monkeypatch.setattr(WE.Store, "apply", apply)
    with pytest.raises(WE.OracleViolation) as raised:
        WE.run_and_check(store, traces)
    assert mutated, "non-vacuity: the mixed trace held a credit to move"
    assert category in str(raised.value), str(raised.value)


def test_f17_store_maintenance_writes_no_forensics_bundle(store):
    """Fix round O4: the store's outcome-neutral forensics stub reaches the
    module the app calls, so a real rebuild writes no bundle (it was inert,
    which more than doubled every rebuild's cost)."""
    import _cctally_core as core
    store.apply(("rebuild", {}))
    assert sorted(core.APP_DIR.rglob("*corruption-forensics*")) == []


def test_f17_reference_worlds_run_the_mixed_maintenance_schedule():
    """Fix round O4 (review CD5-O01): every reference world keeps the mixed
    trace's real rebuild and rederive at the same positions; only the
    excluded readings are reinterpreted."""
    [steps] = WE.mixed_traces(FAMILIES["five-hour-credit"], WE.VARIANTS[0],
                              pairs=((1, 2),)).values()
    kinds = [kind for kind, _values in steps]
    assert kinds.count("rebuild") == kinds.count("rederive") == 1, kinds
    for world in WE.worlds(steps):
        seq = WE.world_steps(steps, world)
        assert len(seq) == len(steps)
        for (kind, values), (world_kind, world_values) in zip(steps, seq):
            if kind == "legacy":
                assert world_kind in ("skip", "current_x"), world_kind
            else:
                assert (world_kind, world_values) == (kind, values), world


def _arm_rebuild_confirm(variant):
    """Fix round O4 (review CD5-O01): a quarantined mixed trace recovers at
    the certified-recovery witness, then arms a genuine same-source
    five-hour 70->20 descent, a real rebuild runs, and the next reading
    reports 20 again. The rebuild drops the disposable confirmation state
    (the admitted five-hour cold-start gap, #751b), so the uniform-current
    trace over the same schedule mints no credit there."""
    skel = FAMILIES["five-hour-credit"]
    picks = iter((skel.values[0], skel.values[0]))
    steps = [WE._concrete(role, values, variant,
                          next(picks) if role == "X" else None)
             for role, values in skel.steps]
    witness, arm, confirm = (WE._concrete(role, values, variant)
                             for role, values in WE.RECOVERY)
    further = WE._concrete(*WE.RECOVERY_FURTHER[0], variant)
    # #948: the automatic OAuth reconciliation that covers the primary.
    coverage = WE.coverage_step(steps, variant)
    return steps + [coverage, witness, arm, ("rebuild", {}), confirm, further]


def _excluded_store() -> bool:
    """True in a store whose journal holds an unstamped (excluded)
    observation: a mixed trace, never a reference world."""
    import _lib_account_protocol as P
    return any(rec.get("t") == "obs" and not P.is_stamped(rec)
               for rec in T.journal_records())


_CONFIRMATION_TABLE = "five_hour_credit_confirmation_state"


def test_f17_rejects_restored_confirmation_state_across_a_rebuild(
        store, monkeypatch):
    """Fix round O4 (review CD5-O01): a MIXED-ONLY mutation in which the
    rebuild wrongly carries the disposable five-hour confirmation state
    across, so the post-rebuild reading confirms the descent armed before it
    and mints a credit the maintained uniform-current schedule does not.
    It fails the oracle, because the reference worlds run the same rebuild.
    A maintenance-free reference does mint that credit, which is why a
    reference without the rebuild accepted the mutation."""
    variant = WE.VARIANTS[0]
    steps = _arm_rebuild_confirm(variant)
    traces = {"arm-rebuild-confirm": steps}
    rebuild_at = steps.index(("rebuild", {}))
    # Unmutated, the real pipeline passes: the mixed trace recovered and
    # armed, and its rebuild left nothing to confirm.
    [(mixed, _worlds)] = WE.run_and_check(store, traces).values()
    assert collections.Counter(mixed[-1]["five_hour"]) == collections.Counter(
        mixed[rebuild_at - 1]["five_hour"]), (mixed[rebuild_at - 1], mixed[-1])
    restored: list = []
    real_apply = WE.Store.apply

    def apply(self, step):
        if step[0] != "rebuild" or not _excluded_store():
            return real_apply(self, step)
        conn = self.app.open_db()
        try:
            columns = [row[1] for row in conn.execute(
                f"PRAGMA table_info({_CONFIRMATION_TABLE})")]
            rows = [tuple(row) for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {_CONFIRMATION_TABLE}")]
        finally:
            conn.close()
        real_apply(self, step)
        conn = self.app.open_db()
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {_CONFIRMATION_TABLE} "
                f"({', '.join(columns)}) VALUES "
                f"({', '.join('?' for _ in columns)})", rows)
            conn.commit()
        finally:
            conn.close()
        restored.extend(rows)

    monkeypatch.setattr(WE.Store, "apply", apply)
    with pytest.raises(WE.OracleViolation) as raised:
        WE.run_and_check(store, traces)
    assert restored, "non-vacuity: the mixed rebuild had armed state to keep"
    assert "five_hour" in str(raised.value), str(raised.value)
    assert f"step {rebuild_at + 1} " in str(raised.value), str(raised.value)
    # Non-vacuity: the no-exclusion world over the same schedule mints no
    # credit across the rebuild, and without the rebuild it confirms the
    # armed descent — one more credit, the one the mutation mints.
    primary = "A" if variant.order == 0 else "B"
    maintained = WE.world_steps(steps, (primary, primary))
    uninterrupted = [("skip", {}) if kind == "rebuild" else (kind, values)
                     for kind, values in maintained]
    monkeypatch.setattr(WE.Store, "apply", real_apply)
    kept, plain = store.run_all([maintained, uninterrupted])
    assert WE.outcome_total(plain[-1]) == WE.outcome_total(kept[-1]) + 1, (
        plain[-1], kept[-1])


# ==========================================================================
# #948 — the real-reducer oracle (spec section 10, oracle contract; D4 C5-V)
# ==========================================================================
#
# Every case below runs `_world_enumeration.run_reducer_oracle`: the mixed
# schedule and every reference world, each through the REAL statusline
# reducer in its own store, compared at every input position — exact C5 for
# what the mixed world mints while its (account, axis) is paused, the D4 (2)
# value keys with `Counter` multiplicity for everything (`check_c5v`). A
# legacy render is absent or a current candidate bound to A, B,
# `unattributed` or C (an account first seen there).

import json
import os

DAY_S = 86400
FIVE_S = 5 * 3600
WEEK_S = 7 * DAY_S
TOKEN_DEADLINE = 180


def period_end(t: int, anchor: int, length: int) -> int:
    """The end of the period of ``length`` seconds containing ``t``, for
    periods ending at ``anchor`` (a reading at an end names the next one)."""
    return anchor + length * ((t - anchor) // length + 1)


class Sched:
    """A schedule under construction: each method appends one step at epoch
    ``t`` and returns its index."""

    def __init__(self, account="A"):
        self.steps = []
        self.account = account

    def _add(self, kind, t, values, *, account=True):
        values = {key: (T.iso(value) if key in ("week_end", "five_end")
                        and isinstance(value, int) else value)
                  for key, value in values.items() if value is not None}
        values["at"] = T.iso(t)
        if account:
            values.setdefault("account", self.account)
        self.steps.append((kind, values))
        return len(self.steps) - 1

    def render(self, t, session="one", **values):
        return self._add("render", t, dict(values, session=session))

    def oauth(self, t, **values):
        return self._add("oauth", t, values)

    def current(self, t, **values):
        values.setdefault("source", "statusline")
        return self._add("current", t, values)

    def tick(self, t):
        return self._add("tick", t, {}, account=False)

    def legacy_render(self, t, session="legacy", **values):
        return self._add("legacy_render", t, dict(values, session=session),
                         account=False)

    def legacy(self, t, **values):
        return self._add("legacy", t, values, account=False)

    def lose_selected(self, t):
        return self._add("lose_selected", t, {}, account=False)


@pytest.fixture
def reducer_store(monkeypatch, tmp_path):
    return WE.ReducerStore(monkeypatch, tmp_path)


def _key(name):
    return T.account_key_of(name)


def _decoded(counter):
    return [json.loads(key) for key, count in counter.items()
            for _ in range(count)]


def _of_kind(counter, kind, account=None, axis=None):
    out = []
    for item in _decoded(counter):
        if item[0] != kind or (account is not None and item[1] != account):
            continue
        if axis is not None and item[2] != axis:
            continue
        out.append(item)
    return out


def _final(run):
    return WE.value_keys(run.snapshots[-1])


def _assert_post_release_multiplicity(mixed, worlds, kind):
    """Every value key of ``kind`` the mixed world minted after its axis's
    release is held with the SAME multiplicity by every reference world at
    the end (drop publications: D4 (2), equal multiplicity)."""
    post = WE.value_keys(mixed.post[-1])
    keys = {key: count for key, count in post.items()
            if json.loads(key)[0] == kind}
    for world, run in worlds.items():
        have = _final(run)
        for key, count in keys.items():
            assert have[key] == count, (world, key, count, have[key])
    return keys


def _axis_keys(counter, axis):
    """The value keys of the ``axis`` outcomes of one snapshot."""
    return WE.value_keys(collections.Counter(
        {item: count for item, count in counter.items() if item[0] == axis}))


def _assert_released_axis_equal(mixed, worlds, account, axis):
    """Review finding F3: C5-V is a sub-multiset check, so a mixed world
    that mints nothing after its release passes it. The recovered outcomes
    of ``axis`` the mixed world minted after its release are non-empty and
    are exactly — key and multiplicity — what every reference world mints
    on that axis from the release position on. Returns them."""
    step = mixed.release_positions()[(account, axis)]
    want = _axis_keys(mixed.post[-1], axis)
    assert want, ("non-vacuity: nothing minted after the release",
                  account, axis, mixed.snapshots[-1])
    for world, run in worlds.items():
        have = _axis_keys(run.snapshots[-1] - run.snapshots[step - 1], axis)
        assert have == want, (world, sorted(want), sorted(have))
    return want


def _kinds(keys):
    return sorted(json.loads(key)[0] for key, count in keys.items()
                  for _ in range(count))


def _epoch_of(text):
    return T.epoch(str(text).replace("+00:00", "Z"))


# --------------------------------------------------------------------------
# T1 — finite continuous recovery (spec 10 T1; Codex Q20)
# --------------------------------------------------------------------------
#
# Legacy activity at 2026-11-01 00:00 UTC: an older process renders 90/90
# while A's current sessions report 70/70, every minute; an automatic OAuth
# reconciliation completes at 00:02 and repeats every 30 minutes (:15 and
# :45). Five-hour windows end 01/06/11/16:00; weekly periods end Saturdays
# 00:00, with a delayed rollover at B = Nov 14 (renders at B and B+60 still
# name end B). A second session renders around the two genuine descents so
# drop consensus can publish them: five-hour 70 -> 20 at 12:01 on Nov 1,
# weekly 70 -> 20 at B+240.

T1_START = T.epoch("2026-11-01T00:00:00Z")
T1_FIVE = T1_START + 3600
T1_WEEK = T.epoch("2026-11-07T00:00:00Z")
T1_B = T1_WEEK + WEEK_S


def t1_values(t):
    end = T1_B if T1_B <= t < T1_B + 120 else period_end(t, T1_WEEK, WEEK_S)
    return dict(weekly=70 if t < T1_B + 240 else 20, week_end=end,
                five=70 if t < T1_START + 12 * 3600 + 60 else 20,
                five_end=period_end(t, T1_FIVE, FIVE_S))


def t1_schedule(*, full=False):
    """The merge-path variant renders every minute over [trigger, J5-release
    + 2 h] and [B - 30 min, J7-release + 10 min] and every 30 minutes
    between; ``full`` renders every minute for all 13 days."""
    s = Sched()
    s.current(T1_START - 240, **t1_values(T1_START - 240))
    for t in range(T1_START - 180, T1_START, 60):
        s.render(t, **t1_values(t))
    s.legacy_render(T1_START, weekly=90, five=90,
                    week_end=t1_values(T1_START)["week_end"],
                    five_end=t1_values(T1_START)["five_end"])
    s.tick(T1_START)
    dense = ((T1_START, T1_START + 13 * 3600), (T1_B - 1800, T1_B + 720))
    pairs = ((T1_START + 11 * 3600 + 55 * 60, T1_START + 13 * 3600),
             (T1_B - 1800, T1_B + 720))
    for t in range(T1_START, T1_B + 721, 60):
        if full or t % 1800 == 0 or any(lo <= t <= hi for lo, hi in dense):
            s.render(t, **t1_values(t))
            if any(lo <= t <= hi for lo, hi in pairs):
                s.render(t, "two", **t1_values(t))
        if t == T1_START + 120 or (t > T1_START + 120 and t % 1800 == 900):
            s.oauth(t, **t1_values(t))
    return s


def _index(steps, kind, t, session=None):
    for index, (k, values) in enumerate(steps):
        if (k == kind and values.get("at") == T.iso(t)
                and (session is None or values.get("session") == session)):
            return index
    raise AssertionError((kind, T.iso(t), session))


def _report_probe(store):
    return T.quarantine_report(store.app)


def _run_t1(store, *, full, interpretations=None):
    s = t1_schedule(full=full)
    steps = s.steps
    probes = {_index(steps, "oauth", T1_START + 120): _report_probe,
              _index(steps, "render", T1_START + 6 * 3600 + 30 * 60): _report_probe,
              _index(steps, "render", T1_WEEK + 12 * 3600): _report_probe}
    mixed, worlds = WE.run_reducer_oracle(store, steps, label="T1",
                                          observe=probes,
                                          interpretations=interpretations)
    a = _key("A")
    tracker = mixed.tracker
    # One spool trigger, journaled by the tick that rescanned after the
    # legacy render; no inventory episode.
    assert [step for step, _p in tracker.of_kind("legacy_spool_activity")] == [
        _index(steps, "tick", T1_START)]
    assert tracker.of_kind("legacy_spool_inventory_completion") == []
    # Jauthority: exactly one completion (00:02, both axes); the later
    # routine refreshes add none, and the completion releases nothing.
    oauth_0002 = _index(steps, "oauth", T1_START + 120)
    assert [(step, p["axes"]) for step, p in tracker.of_kind(
        "outcome_authority_completion")] == [(oauth_0002, ["sevenDay", "fiveHour"])]
    # J5-open/J5-release: the 11:00 first entry (the 06:00 entry of the
    # 11:00 window stays closed); J7-open/J7-release: the B+120 first entry
    # (the Nov 7 entry stays closed; the delayed renders at B and B+60 name
    # the old period).
    j5 = _index(steps, "render", T1_START + 11 * 3600, "one")
    j7 = _index(steps, "render", T1_B + 120, "one")
    assert sorted(mixed.releases()) == sorted([(j5, a, WE.FIVE), (j7, a, WE.WEEKLY)])
    # Drop publications: one per axis, in the released periods.
    key16 = T1_START + 16 * 3600
    key21 = T1_B + WEEK_S
    final = _final(mixed)
    drops = _of_kind(final, "drop", a)
    assert sorted(drops) == sorted([["drop", a, WE.FIVE, key16, 20.0],
                                    ["drop", a, WE.WEEKLY, key21, 20.0]]), final
    publications = tracker.of_kind("publication")
    five_drop_at = next(step for step, p in publications if p["five"] == 20.0)
    week_drop_at = next(step for step, p in publications if p["weekly"] == 20.0)
    # One 70 -> 20 five-hour credit in the 16:00 window: slot 12:00, its
    # boundary the arming publication (the drop publication), minted by a
    # later confirming publication.
    [five] = _of_kind(final, "five-hour-credit", a)
    assert five[2:5] == [key16, 70.0, 20.0], five
    assert _epoch_of(five[5]) == T1_START + 12 * 3600, five
    assert _epoch_of(five[6]) == T.epoch(steps[five_drop_at][1]["at"]), five
    minted = {item[3]: index for index, item in mixed.minted}
    # Review finding F22: minted by the FIRST confirming reading after the
    # drop publication — the next publication of the same source carrying
    # the same low under a new publication identity (the second session's
    # retry of the drop's own publication is that observation replayed and
    # cannot confirm it) — not merely somewhere after it.
    [drop] = [p for step, p in publications if step == five_drop_at][:1]
    confirm = next(step for step, p in publications
                   if step > five_drop_at and p["account"] == a
                   and p["five"] == 20.0 and p["source"] == drop["source"]
                   and p["id"] != drop["id"])
    assert minted[json.dumps(five)] == confirm, (
        minted[json.dumps(five)], confirm, steps[confirm])
    # One 70 -> 20 weekly credit, effective at the weekly drop publication.
    [week] = _of_kind(final, "weekly-credit", a)
    assert week[4:6] == [70.0, 20.0], week
    assert _epoch_of(week[6]) == T.epoch(steps[week_drop_at][1]["at"]) >= T1_B + 240
    # Repeated lows add nothing: these four are all the mixed world minted.
    assert sum(final.values()) == 4, final
    # Doctor's notBefore never postdates the release it bounds (T9 with T1).
    for index, report in mixed.observed.items():
        [row] = [r for r in report["accounts"] if r["account"] == a]
        for axis, release_step in ((WE.FIVE, j5), (WE.WEEKLY, j7)):
            bound = row["axes"][axis]["notBefore"]
            if bound is not None:
                assert T.epoch(bound) <= T.epoch(steps[release_step][1]["at"]), (
                    index, axis, bound)
    return mixed, worlds


# Off the merge path (BROAD_RUN_ONLY in bin/_lib_quick_gate.py); one case
# measured 46-97 s on the runner, so it carries its own 360 s budget instead
# of the broad run's 120 s default rather than flake on a loaded host.
@pytest.mark.timeout(360)
@pytest.mark.parametrize("world", WE.RENDER_INTERPRETATIONS,
                         ids=lambda w: f"legacy-{w or 'absent'}")
def test_t1_finite_continuous_recovery(reducer_store, world):
    """Detects a pause that never ends, a release on the wrong period, and a
    lost or duplicated recovered outcome: both axes of A recover on their
    first fresh period after expiry and the completed OAuth reconciliation,
    each recovered outcome mints exactly once, and C5 holds before / C5-V
    after each release against each of the five reference worlds. One
    reference world per case: every world is compared with the mixed run
    independently, and one case holding all five exceeds the suite's
    120-second per-test timeout (about 200 s on the runner)."""
    _run_t1(reducer_store, full=False, interpretations=[(world,)])


@pytest.mark.skipif(os.environ.get("CCTALLY_948_FULL_TRACE") != "1",
                    reason="the 13-day minute trace is acceptance evidence, "
                           "run once with CCTALLY_948_FULL_TRACE=1")
def test_t1_full_minute_trace(reducer_store):
    """The full 13-day minute-render variant of T1 (spec 10, *T1 cost*)."""
    mixed, worlds = _run_t1(reducer_store, full=True)
    print(f"T1 full: {len(mixed.seq)} steps per world, {len(worlds)} worlds")


# --------------------------------------------------------------------------
# drop consensus through the real reducer (replaces the kernel-level cases)
# --------------------------------------------------------------------------

DROP_T0 = T.epoch("2026-11-03T10:00:00Z")
DROP_WEEK = T.epoch("2026-11-07T00:00:00Z")
#: Legacy render values: agreeing with the current drop, the baseline (a
#: dissent), a rise and a deeper drop.
LEGACY_VALUES = (20.0, 70.0, 90.0, 5.0)


def _drop_schedule(value, primary, *, legacy_renders=1, recovery=True):
    s = Sched(primary)
    s.current(DROP_T0 - 300, weekly=70, week_end=DROP_WEEK)
    for t in (DROP_T0 - 240, DROP_T0 - 180):
        s.render(t, weekly=70, week_end=DROP_WEEK)
        s.render(t, "two", weekly=70, week_end=DROP_WEEK)
    for n in range(legacy_renders):
        s.legacy_render(DROP_T0 + 60 * n, weekly=value, week_end=DROP_WEEK)
        s.tick(DROP_T0 + 60 * n)
    for t in range(DROP_T0 + 60, DROP_T0 + 60 + 2 * TOKEN_DEADLINE + 180, 60):
        s.render(t, weekly=20, week_end=DROP_WEEK)
        s.render(t, "two", weekly=20, week_end=DROP_WEEK)
    if recovery:
        # Expiry (E = H + 95 s), a completed OAuth reconciliation, the
        # Nov 7 week (its start precedes U7.end: stays closed), then the
        # Nov 14 first entry releases and a genuine 60 -> 10 drop follows.
        s.oauth(DROP_T0 + 900, weekly=20, week_end=DROP_WEEK)
        nov14 = DROP_WEEK + WEEK_S
        s.render(DROP_WEEK + 30, weekly=25, week_end=nov14)
        s.oauth(DROP_WEEK + 3 * DAY_S, weekly=25, week_end=nov14)
        s.render(nov14 + 30, weekly=60, week_end=nov14 + WEEK_S)
        for t in range(nov14 + 630, nov14 + 630 + 2 * TOKEN_DEADLINE + 180, 60):
            s.render(t, weekly=10, week_end=nov14 + WEEK_S)
            s.render(t, "two", weekly=10, week_end=nov14 + WEEK_S)
    return s


@pytest.mark.parametrize("value", LEGACY_VALUES)
@pytest.mark.parametrize("primary", ["A", "B"])
def test_f17_drop_consensus_is_withheld_while_paused_and_resumes_on_release(
        reducer_store, primary, value):
    """Detects a drop published while its axis is paused, and a recovered
    drop that never publishes or publishes a key some reference lacks: the
    drop below the baseline after spool-only legacy activity is withheld by
    consensus and deadline alike; after the barrier the genuine drop
    publishes with the multiplicity every reference world holds."""
    s = _drop_schedule(value, primary)
    mixed, worlds = WE.run_reducer_oracle(
        reducer_store, s.steps, label=f"drop/{primary}/{value}")
    who = _key(primary)
    [(release_step, account, axis)] = mixed.releases()
    assert (account, axis) == (who, WE.WEEKLY)
    for index, item in mixed.minted:
        if json.loads(item[3])[0] == "drop":
            assert index >= release_step, ("published while paused", item)
    keys = _assert_post_release_multiplicity(mixed, worlds, "drop")
    nov21 = DROP_WEEK + 2 * WEEK_S
    assert list(keys) == [json.dumps(["drop", who, WE.WEEKLY, nov21, 10.0])]
    # Non-vacuity: without the legacy render the first drop publishes.
    assert _of_kind(_final(worlds[(None,)]), "drop", who, WE.WEEKLY)[0][3] == DROP_WEEK


def test_f17_drop_consensus_oracle_rejects_an_open_gate(reducer_store, monkeypatch):
    """With the drop gate forced open the mixed world publishes the current
    drop while the weekly axis is still paused, and the oracle rejects it as
    an outcome minted before its release."""
    import _cctally_statusline as sl
    monkeypatch.setattr(sl, "_drop_permitted", lambda *args, **kwargs: True)
    s = _drop_schedule(BASELINE_DISSENT, "A", legacy_renders=2, recovery=False)
    mixed, worlds = WE.run_reducer_oracle(
        reducer_store, s.steps, label="open-gate", check=False)
    drop = json.dumps(["drop", _key("A"), WE.WEEKLY, DROP_WEEK, 20.0])
    # Minted while the weekly axis is paused (no release in this schedule).
    assert drop in WE.value_keys(mixed.snapshots[-1] - mixed.post[-1])
    with pytest.raises(WE.OracleViolation, match="before its release"):
        WE.check_c5v(s.steps, mixed, worlds, label="open-gate")


BASELINE_DISSENT = 70.0


# --------------------------------------------------------------------------
# per-axis drop withholding in the frozen transition (primary's ruling)
# --------------------------------------------------------------------------
#
# The frozen transition's `quarantine` document carries one `dropWithheld`
# flag, true while ANY axis is withheld, and the render path reduces with
# drop consensus closed on both axes while it is set — relying on
# `_render_drop_work` to leave a below-baseline evaluation to the detached
# reducer, which gates per axis. One axis released and the other paused,
# then a genuine drop on the released axis through renders: mixed must
# publish it with every reference world's multiplicity, and publish nothing
# on the paused axis.

def _one_axis_prelude(case):
    """A's baseline, an excluded write, an OAuth reading covering both axes
    (five-hour released) or weekly alone (weekly released), then minute
    renders through the released axis's first fresh entry. Returns the
    schedule, the base values, the period ends, the first free instant and
    the released and paused axes."""
    s = Sched()
    if case == "five-hour-released":
        t0 = T.epoch("2026-11-03T10:00:00Z")
        week, anchor = T.epoch("2026-11-07T00:00:00Z"), t0 + 3600
        base = dict(weekly=40, five=50)
        cover = dict(weekly=40, five=50)
    else:
        t0 = T.epoch("2026-11-06T23:00:00Z")
        week, anchor = T.epoch("2026-11-07T00:00:00Z"), t0 + 2 * 3600
        base = dict(weekly=60, five=50)
        cover = dict(weekly=60)            # weekly-only: covers weekly alone

    def ends(t):
        return dict(week_end=period_end(t, week, WEEK_S),
                    five_end=period_end(t, anchor, FIVE_S))

    s.current(t0, **base, **ends(t0))
    for t in (t0 + 30, t0 + 90):
        s.render(t, **base, **ends(t))
    s.legacy(t0 + 240, weekly=base["weekly"] + 2, five=55, **ends(t0 + 240))
    s.tick(t0 + 270)
    values = dict(cover, **ends(t0 + 360))
    if "five" not in cover:
        values.pop("five_end")
    s.oauth(t0 + 360, **values)
    for t in range(t0 + 390, t0 + 3600 + 330, 60):
        s.render(t, **base, **ends(t))
    released, paused = ((WE.FIVE, WE.WEEKLY) if case == "five-hour-released"
                        else (WE.WEEKLY, WE.FIVE))
    return s, base, ends, t0 + 3600 + 600, released, paused


def _one_axis_released(case):
    s, base, ends, start, released, paused = _one_axis_prelude(case)
    low = dict(base)
    low["five" if released == WE.FIVE else "weekly"] = 20
    for t in range(start, start + 1800, 60):
        s.render(t, **low, **ends(t))
        s.render(t, "two", **low, **ends(t))
    lower = dict(low)
    lower["weekly" if paused == WE.WEEKLY else "five"] = 10
    for t in range(start + 1800, start + 2400, 60):
        s.render(t, **lower, **ends(t))
        s.render(t, "two", **lower, **ends(t))
    return s, released, paused


@pytest.mark.parametrize("case", ["five-hour-released", "weekly-released"])
def test_one_axis_released_publishes_its_drop_like_every_reference(
        reducer_store, case):
    """Detects a released axis's drop lost or delayed into a different value
    key by the single frozen `dropWithheld` flag, and a paused axis's drop
    published: the released axis's genuine drop publishes with every
    reference world's multiplicity; the paused axis publishes no drop."""
    s, released, paused = _one_axis_released(case)
    mixed, worlds = WE.run_reducer_oracle(reducer_store, s.steps, label=case)
    a = _key("A")
    assert [(acct, axis) for _step, acct, axis in mixed.releases()] == [
        (a, released)]
    keys = _assert_post_release_multiplicity(mixed, worlds, "drop")
    assert [json.loads(key)[2] for key in keys] == [released], keys
    assert _of_kind(_final(mixed), "drop", a, paused) == []
    # Non-vacuity: with no trigger the paused axis's drop publishes too.
    assert _of_kind(_final(worlds[(None,)]), "drop", a, paused)


_FIELD = {WE.WEEKLY: "weekly", WE.FIVE: "five"}


def _deadline_drop_with_one_axis_withheld(case):
    """Review finding F1: the paused axis's drop is withheld (the frozen
    transition's `dropWithheld` is set), then the released axis's genuine
    drop is published only at its pending deadline — session one renders
    the low while session two still dissents at the baseline, both
    re-rendering unchanged past the deadline — then session two agrees, and
    two rises of the paused axis follow. A reference world forks and
    publishes the released axis's drop at the deadline; a mixed render that
    reduced both axes closed would acknowledge those unchanged evaluations
    as nothing to do and publish later, at another instant."""
    s, base, ends, start, released, paused = _one_axis_prelude(case)
    rel, pau = _FIELD[released], _FIELD[paused]
    withheld = dict(base, **{pau: base[pau] - 10})

    def both(t, one, two=None):
        s.render(t, **one, **ends(t))
        s.render(t, "two", **(two or one), **ends(t))

    t = start
    for _ in range(6):
        both(t, withheld)
        t += 60
    probe = len(s.steps) - 1
    low = dict(withheld, **{rel: 10 if released == WE.FIVE else base[rel] - 30})
    for _ in range(6):
        both(t, low, withheld)
        t += 60
    for _ in range(3):
        both(t, low)
        t += 60
    for bump in (2, 3):
        rise = dict(low, **{pau: base[pau] + bump})
        for _ in range(3):
            both(t, rise)
            t += 60
    return s, released, paused, probe


@pytest.mark.parametrize("case", ["five-hour-released", "weekly-released"])
def test_one_axis_released_publishes_a_deadline_drop_like_every_reference(
        reducer_store, case):
    """Detects a released axis's drop the render path acknowledges under
    the paused axis's withheld flag (review finding F1): with the frozen
    transition's `dropWithheld` set, the released axis's drop that only its
    pending deadline publishes is published at the same instant as in every
    reference world — the drop and the credit it arms carry the references'
    value keys — and the paused axis publishes nothing."""
    import _cctally_statusline as sl
    s, released, paused, probe = _deadline_drop_with_one_axis_withheld(case)
    a = _key("A")

    def drop_withheld(store):
        transition = sl._read_transition(a, now_epoch=store._now[0])
        doc = None if transition is None else transition.quarantine
        return None if doc is None else doc.get("dropWithheld")

    mixed, worlds = WE.run_reducer_oracle(
        reducer_store, s.steps, label=f"deadline/{case}",
        observe={probe: drop_withheld})
    assert mixed.observed[probe] is True, mixed.observed
    assert [(acct, axis) for _step, acct, axis in mixed.releases()] == [
        (a, released)]
    keys = _assert_released_axis_equal(mixed, worlds, a, released)
    credit = "five-hour-credit" if released == WE.FIVE else "weekly-credit"
    assert {"drop", credit} <= set(_kinds(keys)), keys
    assert _of_kind(_final(mixed), "drop", a, paused) == []


# --------------------------------------------------------------------------
# T3 — withheld divergence inside the barrier (spec 10 T3)
# --------------------------------------------------------------------------

T3_T0 = T.epoch("2026-11-01T00:00:00Z")
T3_FIVE = T3_T0 + 3600
T3_WEEK = T.epoch("2026-11-07T00:00:00Z")


def _t3_spool_prelude(s, *, weekly=30):
    """A's baseline, then spool-only legacy activity at T3_T0 (cohort E =
    00:01:35, U5.raw = 06:01:35)."""
    def ends(t):
        return dict(week_end=period_end(t, T3_WEEK, WEEK_S),
                    five_end=period_end(t, T3_FIVE, FIVE_S))
    s.current(T3_T0 - 240, weekly=weekly, five=70, **ends(T3_T0 - 240))
    s.render(T3_T0 - 120, weekly=weekly, five=70, **ends(T3_T0 - 120))
    s.legacy_render(T3_T0, weekly=90, five=90, **ends(T3_T0))
    s.tick(T3_T0)
    return ends


def _genuine_five_descent(s, ends, start, *, weekly=30, high=70, low=20):
    s.render(start, weekly=weekly, five=high, **ends(start))
    for t in range(start + 600, start + 600 + 1800, 60):
        s.render(t, weekly=weekly, five=low, **ends(t))
        s.render(t, "two", weekly=weekly, five=low, **ends(t))


def _t3_successive_windows():
    """70 -> 20 -> 21 -> 22 across successive windows, before and after the
    OAuth reconciliation: no window-crossing credit; then a genuine
    same-window descent mints once."""
    s = Sched()
    ends = _t3_spool_prelude(s)
    for start, five in ((T3_T0 + 3600, 70), (T3_T0 + 6 * 3600, 20)):
        for t in (start + 30, start + 600):
            s.render(t, weekly=30, five=five, **ends(t))
    s.oauth(T3_T0 + 8 * 3600, weekly=30, five=20, **ends(T3_T0 + 8 * 3600))
    for start, five in ((T3_T0 + 11 * 3600, 21), (T3_T0 + 16 * 3600, 22)):
        for t in (start + 30, start + 600):
            s.render(t, weekly=30, five=five, **ends(t))
    _genuine_five_descent(s, ends, T3_T0 + 21 * 3600 + 30)
    return s, 1


def _t3_no_reseeding():
    """70 / OAuth 60 / 40 / 30 inside the paused 11:00 window (the OAuth
    reading covers; nothing reseeds), then a genuine descent in the next
    window mints once."""
    s = Sched()
    ends = _t3_spool_prelude(s)
    start = T3_T0 + 6 * 3600
    s.render(start + 30, weekly=30, five=70, **ends(start + 30))
    s.oauth(start + 3600, weekly=30, five=60, **ends(start + 3600))
    for t in range(start + 3660, start + 3660 + 1200, 60):
        five = 40 if t < start + 3660 + 600 else 30
        s.render(t, weekly=30, five=five, **ends(t))
        s.render(t, "two", weekly=30, five=five, **ends(t))
    _genuine_five_descent(s, ends, T3_T0 + 11 * 3600 + 30)
    return s, 1


def _t3_completion_mid_period():
    """The OAuth reconciliation completes after the 11:00 window's first
    entry: that window is never released (its genuine descent stays
    withheld); the 16:00 window's first entry releases and its genuine
    descent mints once."""
    s = Sched()
    ends = _t3_spool_prelude(s)
    _genuine_five_descent(s, ends, T3_T0 + 11 * 3600 + 30)
    s.oauth(T3_T0 + 11 * 3600 + 300, weekly=30, five=70,
            **ends(T3_T0 + 11 * 3600 + 300))
    _genuine_five_descent(s, ends, T3_T0 + 16 * 3600 + 30)
    return s, 1


def _t3_overlapping_weekly_floors():
    """A weekly in-place credit before the trigger, a reference-only one
    while weekly is paused, and a genuine one after the release: the mixed
    world keeps the first and mints the last exactly once."""
    s = Sched()
    t0 = T.epoch("2026-11-06T22:00:00Z")
    nov7, nov14 = T3_WEEK, T3_WEEK + WEEK_S
    s.current(t0, weekly=70, week_end=nov7)
    s.current(t0 + 600, weekly=30, week_end=nov7)       # pre-trigger credit
    s.current(t0 + 1200, weekly=31, week_end=nov7)
    s.legacy(t0 + 1800, weekly=32, week_end=nov7)
    s.tick(t0 + 1860)
    s.oauth(t0 + 2400, weekly=32, week_end=nov7)
    s.current(t0 + 3000, weekly=5, week_end=nov7)       # paused descent
    s.current(nov7 + 30, weekly=60, week_end=nov14)     # witness
    s.current(nov7 + 600, weekly=61, week_end=nov14)
    s.current(nov7 + 1200, weekly=30, week_end=nov14)   # genuine credit
    s.current(nov7 + 1800, weekly=31, week_end=nov14)
    return s, 2


def _t3_competing_first_entry():
    """Two sessions and an OAuth reading all report the 16:00 window's first
    entry in the same second: one witness, one release, and the genuine
    descent that follows mints once."""
    s = Sched()
    ends = _t3_spool_prelude(s)
    s.oauth(T3_T0 + 600, weekly=30, five=70, **ends(T3_T0 + 600))
    first = T3_T0 + 11 * 3600 + 30
    s.render(first, weekly=30, five=70, **ends(first))
    s.render(first, "two", weekly=30, five=70, **ends(first))
    s.oauth(first, weekly=30, five=70, **ends(first))
    _genuine_five_descent(s, ends, first + 60)
    return s, 1


T3_CASES = {
    "successive-windows": _t3_successive_windows,
    "no-reseeding": _t3_no_reseeding,
    "completion-mid-period": _t3_completion_mid_period,
    "overlapping-weekly-floors": _t3_overlapping_weekly_floors,
    "competing-first-entry": _t3_competing_first_entry,
}


@pytest.mark.parametrize("case", sorted(T3_CASES))
def test_t3_withheld_divergence_inside_the_barrier(reducer_store, case):
    """Detects a same-period release and OAuth reseeding: no outcome the
    mixed world mints before a release is missing from a reference world,
    and each case's positive endpoint — the genuine credit after the
    release — mints exactly once."""
    s, credits = T3_CASES[case]()
    mixed, worlds = WE.run_reducer_oracle(reducer_store, s.steps, label=case)
    a = _key("A")
    final = _final(mixed)
    minted = (_of_kind(final, "five-hour-credit", a)
              + _of_kind(final, "weekly-credit", a))
    assert len(minted) == credits, (case, final)
    if case == "completion-mid-period":
        [(step, _a, axis)] = [r for r in mixed.releases() if r[2] == WE.FIVE]
        assert s.steps[step][1]["at"] == T.iso(T3_T0 + 16 * 3600 + 30)
    if case == "competing-first-entry":
        assert [r[2] for r in mixed.releases()].count(WE.FIVE) == 1


# --------------------------------------------------------------------------
# T6 / T2 BV1 — excluded-only recovery, then selected state lost everywhere
# --------------------------------------------------------------------------

def _t6_excluded_only_then_selected_state_lost():
    """The A1 schedule (an excluded write, no spool activity) through an
    actual weekly release at the Nov 14 week's first entry; then every
    local selected-state document is lost — the same fault in every world —
    then a genuine 60 -> 20 weekly drop through two sessions' renders, and
    an OAuth reading of the lower value. Returns the schedule and the step
    of the loss."""
    s = Sched()
    t0 = T.epoch("2026-11-06T23:00:00Z")
    week = T.epoch("2026-11-07T00:00:00Z")
    anchor = t0 + 2 * 3600

    def ends(t):
        return dict(week_end=period_end(t, week, WEEK_S),
                    five_end=period_end(t, anchor, FIVE_S))

    base = dict(weekly=60, five=50)
    s.current(t0, **base, **ends(t0))
    for t in (t0 + 30, t0 + 90):
        s.render(t, **base, **ends(t))
    s.legacy(t0 + 240, weekly=62, five=55, **ends(t0 + 240))
    s.tick(t0 + 270)
    s.oauth(t0 + 360, **base, **ends(t0 + 360))
    for t in range(t0 + 390, week + 330, 60):
        s.render(t, **base, **ends(t))
    lost = s.lose_selected(week + 360)
    low = dict(base, weekly=20)
    for t in range(week + 420, week + 420 + 600, 60):
        s.render(t, **low, **ends(t))
        s.render(t, "two", **low, **ends(t))
    s.oauth(week + 1200, **low, **ends(week + 1200))
    for t in range(week + 1260, week + 1500, 60):
        s.render(t, **low, **ends(t))
    return s, lost


def test_t6_excluded_only_recovery_survives_selected_state_loss(reducer_store):
    """Detects A1 and BV1 (spec 10 T2, T6): after an excluded-only trigger
    the weekly axis releases at its fresh period's first entry; with every
    local selected-state document lost after the release in every world,
    the genuine drop and its credit mint in the mixed world with exactly
    the references' value keys and multiplicity, and nothing is minted
    before the release that a reference lacks."""
    s, lost = _t6_excluded_only_then_selected_state_lost()
    mixed, worlds = WE.run_reducer_oracle(reducer_store, s.steps, label="T6")
    a = _key("A")
    release = mixed.release_positions()[(a, WE.WEEKLY)]
    assert release == _index(s.steps, "render", T.epoch("2026-11-07T00:00:30Z"))
    assert release < lost
    keys = _assert_released_axis_equal(mixed, worlds, a, WE.WEEKLY)
    assert {"drop", "weekly-credit"} <= set(_kinds(keys)), keys


def test_check_c5v_rejects_a_mixed_only_key_after_the_release():
    """Review finding F5: the post-release branch of `check_c5v` can fail.
    A synthetic run whose mixed world mints, after its axis's release, one
    value key no reference world holds raises; the same key held by the
    reference passes."""
    import dataclasses
    steps = [("current", {}), ("legacy", {}), ("render", {})]
    key = json.dumps(["drop", "A", WE.FIVE, 1, 10.0])
    minted = collections.Counter({(WE.FIVE, "A", key, key): 1})
    empty = collections.Counter()
    mixed = WE.ReducerRun(seq=steps, tracker=None,
                          snapshots=[empty, empty, minted],
                          post=[empty, empty, minted])
    world = WE.ReducerRun(seq=steps, tracker=None,
                          snapshots=[empty, empty, empty])
    with pytest.raises(WE.OracleViolation, match="value keys"):
        WE.check_c5v(steps, mixed, {("A",): world}, label="synthetic")
    WE.check_c5v(steps, mixed, {("A",): dataclasses.replace(
        world, snapshots=[empty, empty, minted])}, label="synthetic")


# --------------------------------------------------------------------------
# T7 — residual characterization (spec 9, 10 T7)
# --------------------------------------------------------------------------
#
# One schedule per residual of D5, its permitted difference fixed before the
# schedule was ever run (commit 0abfebc53; the plan left R2-R4's values to
# this task; R1 is Codex's complete schedule), and a twin that holds provider
# truth constant while keeping the stale or inconsistent observations the
# residual relies on. Every schedule releases its axis inside the trace (R4
# was rebuilt in fix round F2: its excluded write names the current week,
# because one naming the witness week bounds the weekly fresh period past
# it). On the real reducer none of the four is exhibited, so each is
# reported REFUTED for these values and none keeps a permitted difference:
# after its release each mints exactly the recovered value keys every
# reference world mints, the A-bound world included (fix round F3 asserts
# that set non-empty and equal, not merely contained). The precondition
# probes show why: R1's mechanism occurs and changes no outcome; R2's, R3's
# and R4's do not occur at all (`_assert_r_precondition`). Every schedule is
# held to plain C5/C5-V, so an outcome that later appears outside every
# reference world fails here and must be reviewed as a residual.

R_T0 = T.epoch("2026-11-03T00:00:00Z")          # Tuesday
R_WEEK = T.epoch("2026-11-07T00:00:00Z")
R_FIVE = R_T0 + 3600                            # windows end 01/06/11/16:00
R_K = R_T0 + 16 * 3600                          # window K's raw reset R


def _r_five_prelude(s, *, legacy_weekly=65):
    """R1/R2: A at weekly 60, five-hour 20 in window K0 (ends 06:00); a
    legacy render the A-bound reference publishes as weekly 65; the OAuth
    reconciliation (weekly 60, held in the A-bound reference); the 11:00
    window's first entry at 06:30 (before U5.raw: never released)."""
    s.current(R_T0 + 5400, weekly=60, five=20, week_end=R_WEEK,
              five_end=R_T0 + 6 * 3600)
    s.legacy_render(R_T0 + 7200, weekly=legacy_weekly, five=20,
                    week_end=R_WEEK, five_end=R_T0 + 6 * 3600)
    s.tick(R_T0 + 7200)
    s.oauth(R_T0 + 7500, weekly=60, five=20, week_end=R_WEEK,
            five_end=R_T0 + 6 * 3600)
    s.current(R_T0 + 6 * 3600 + 1800, weekly=60, five=20, week_end=R_WEEK,
              five_end=R_T0 + 11 * 3600)


def _r1(twin):
    """R1 (Codex Q17, complete): five-hour releases at K's first entry
    (11:00:30, raw R) while weekly stays paused; a common reading weekly
    61, five-hour 20 moves K's raw reset to R+300 (mixed writes it; the
    A-bound reference, whose weekly baseline is 65, skips it as weekly-held
    and five-hour-unchanged); five-hour 10 with raw R+10, then a distinct
    10. Twin: provider truth stays at 20."""
    s = Sched()
    _r_five_prelude(s)
    s.current(R_T0 + 11 * 3600 + 30, weekly=60, five=20, week_end=R_WEEK,
              five_end=R_K)
    s.current(R_T0 + 11 * 3600 + 600, weekly=61, five=20, week_end=R_WEEK,
              five_end=R_K + 300)
    low = 20 if twin else 10
    s.current(R_T0 + 11 * 3600 + 1200, weekly=61, five=low, week_end=R_WEEK,
              five_end=R_K + 10)
    s.current(R_T0 + 11 * 3600 + 1800, weekly=61, five=low, week_end=R_WEEK,
              five_end=R_K + 10)
    return s


def _r2(twin):
    """R2 (anchor, values fixed here): K's first entry is weekly 60,
    five-hour 70 (11:00:30) — written in mixed, weekly-held in the A-bound
    reference — so K's five-hour block exists only in mixed; then 70 again,
    and a same-source 70 -> 20 descent with a distinct confirmation. Twin:
    five-hour stays at 70."""
    s = Sched()
    _r_five_prelude(s)
    for t, five in ((R_T0 + 11 * 3600 + 30, 70), (R_T0 + 11 * 3600 + 600, 70),
                    (R_T0 + 11 * 3600 + 1200, 70 if twin else 20),
                    (R_T0 + 11 * 3600 + 1800, 70 if twin else 20)):
        s.current(t, weekly=60, five=five, week_end=R_WEEK, five_end=R_K)
    return s


R3_T0 = T.epoch("2026-11-06T23:30:00Z")          # Friday
R3_K = T.epoch("2026-11-07T01:00:00Z")


def _r3(twin):
    """R3 (deferral, values fixed here): A's sessions render weekly only; an
    excluded reading (in the A-bound reference, A's current reading)
    carries five-hour 40 in window K (ends Nov 7 01:00). A weekly-only
    OAuth reading covers weekly; the Nov 14 week's first render (00:00:30)
    releases weekly with five-hour paused. At 00:10:30 both sessions render
    weekly 20 with five-hour 40 in K for the first time in mixed: a frame
    the mixed reducer may defer on its unclassified five-hour axis, while
    the A-bound reference classifies it. Twin: weekly stays at 60."""
    s = Sched()
    nov14 = R_WEEK + WEEK_S
    s.current(R3_T0, weekly=60, week_end=R_WEEK)
    for t in (R3_T0 + 60, R3_T0 + 120):
        s.render(t, weekly=60, week_end=R_WEEK)
    s.legacy(R3_T0 + 600, weekly=60, week_end=R_WEEK, five=40, five_end=R3_K)
    s.tick(R3_T0 + 660)
    s.oauth(R3_T0 + 900, weekly=60, week_end=R_WEEK)
    for t in range(R3_T0 + 960, R_WEEK, 60):
        s.render(t, weekly=60, week_end=R_WEEK)
    for t in range(R_WEEK + 30, R_WEEK + 600, 60):
        s.render(t, weekly=60, week_end=nov14)
    low = 60 if twin else 20
    for t in range(R_WEEK + 630, R_WEEK + 1230, 60):
        s.render(t, weekly=low, week_end=nov14, five=40, five_end=R3_K)
        s.render(t, "two", weekly=low, week_end=nov14, five=40, five_end=R3_K)
    return s


def _r4(twin):
    """R4 (first entry, values fixed here; rebuilt in fix round F2 so that
    weekly releases): an excluded reading at 23:40 (in the A-bound
    reference, A's current reading) names the CURRENT Nov 7 week at weekly
    70, so the A-bound reference holds a 70 baseline the mixed world does
    not; a weekly-only OAuth reading covers weekly; the first fresh render
    (00:00:30, weekly 45, Nov 14) is the mixed witness; at 00:01:30 both
    sessions render the genuine descent to 10. The residual's shape is the
    first fresh render classified `unknown` in one world and published in
    another, changing the later credit's pre-percent. The excluded week
    bounds the witness week (S7.end, X7), so no world can carry a different
    baseline into the witness week itself. Twin: the sessions stay at 45."""
    s = Sched()
    t0 = T.epoch("2026-11-06T23:30:00Z")
    nov14 = R_WEEK + WEEK_S
    s.current(t0, weekly=40, week_end=R_WEEK)
    for t in (t0 + 60, t0 + 120):
        s.render(t, weekly=40, week_end=R_WEEK)
    s.legacy(t0 + 600, weekly=70, week_end=R_WEEK)
    s.tick(t0 + 660)
    s.oauth(t0 + 900, weekly=40, week_end=R_WEEK)
    for t in range(t0 + 960, R_WEEK, 60):
        s.render(t, weekly=40, week_end=R_WEEK)
    s.render(R_WEEK + 30, weekly=45, week_end=nov14)
    low = 45 if twin else 10
    for t in range(R_WEEK + 90, R_WEEK + 90 + 900, 60):
        s.render(t, weekly=low, week_end=nov14)
        s.render(t, "two", weekly=low, week_end=nov14)
    return s


R_SCHEDULES = {"R1": _r1, "R2": _r2, "R3": _r3, "R4": _r4}


#: The axis each residual schedule releases.
R_RELEASED = {"R1": WE.FIVE, "R2": WE.FIVE, "R3": WE.WEEKLY, "R4": WE.WEEKLY}


def _count(store, sql, *params):
    conn = store.app.open_db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _r_probe(name, steps):
    """Review finding F3: the step and probe that show the residual's
    precondition occurred (`_assert_r_precondition` reads them), or None
    when the tracker alone shows it."""
    if name == "R1":
        moved = _index(steps, "current", R_T0 + 11 * 3600 + 600)
        captured = T.iso(R_T0 + 11 * 3600 + 600)[:19]
        return moved, lambda store: _count(
            store, "SELECT COUNT(*) FROM weekly_usage_snapshots "
                   "WHERE substr(captured_at_utc, 1, 19) = ?", captured)
    if name == "R2":
        first = _index(steps, "current", R_T0 + 11 * 3600 + 30)
        return first, lambda store: _count(
            store, "SELECT COUNT(*) FROM five_hour_blocks "
                   "WHERE five_hour_window_key BETWEEN ? AND ?",
            R_K - JITTER, R_K + JITTER)
    return None


def _publication_steps(run, account):
    return {step for step, p in run.tracker.of_kind("publication")
            if p["account"] == account}


def _assert_r_precondition(name, steps, mixed, worlds, probe):
    """What the residual's stated mechanism does on the real reducer (review
    finding F3). R1 occurs: the A-bound reference skips the reading that
    moves K's raw reset, which mixed writes. R2 does not: the weekly hold
    omits no five-hour block — K's block exists in mixed AND in the A-bound
    reference (a new window is never "five-hour-unchanged"). R3 does not:
    mixed publishes the first frame of five-hour 40 in K — the mixed
    five-hour axis holds no row, block or credit, so it is proven empty and
    classified, never deferred — and the genuine weekly descent publishes at
    the same step in mixed and in the A-bound reference. R4 does not: the
    first fresh render is the mixed witness, so mixed publishes it, as every
    reference world does (the excluded week bounds the witness week, so no
    world carries another baseline into it)."""
    a = _key("A")
    a_bound = worlds[("A",)]
    if name in ("R1", "R2"):
        step = probe[0]
        expected = (1, 0) if name == "R1" else (1, 1)
        assert (mixed.observed[step], a_bound.observed[step]) == expected, (
            name, mixed.observed, a_bound.observed)
    elif name == "R3":
        frame = {i for i, (kind, values) in enumerate(steps)
                 if kind == "render" and values["at"] == T.iso(R_WEEK + 630)}
        assert frame & _publication_steps(mixed, a), name

        def descent(run):
            return next(step for step, p in run.tracker.of_kind("publication")
                        if p["account"] == a and p["weekly"] == 20.0)

        assert descent(mixed) == descent(a_bound), name
    else:
        first = _index(steps, "render", R_WEEK + 30)
        assert mixed.release_positions()[(a, WE.WEEKLY)] == first
        for world, run in worlds.items():
            assert first in _publication_steps(run, a), world


@pytest.mark.parametrize("twin", [False, True], ids=["residual", "twin"])
@pytest.mark.parametrize("name", sorted(R_SCHEDULES))
def test_t7_residual_schedules_mint_nothing_outside_the_references(
        reducer_store, name, twin):
    """Detects an outcome minted with no genuine provider descent, and an
    unreviewed residual: each residual schedule and its provider-truth twin
    keep exact C5 before the release and C5-V after it against every
    reference world, with no permitted difference; every schedule releases
    its axis inside the trace, the residual's precondition occurs, the
    residual mints its recovered outcomes exactly as every reference world
    does (review finding F3: a mixed world minting nothing would pass C5-V),
    and the twin mints none."""
    s = R_SCHEDULES[name](twin)
    probe = _r_probe(name, s.steps)
    observe = None if probe is None else dict([probe])
    mixed, worlds = WE.run_reducer_oracle(
        reducer_store, s.steps,
        label=f"{name}/{'twin' if twin else 'residual'}",
        observe=observe, world_observe=observe)
    a = _key("A")
    axis = R_RELEASED[name]
    assert (a, axis) in mixed.release_positions(), (name, mixed.releases())
    if twin:
        assert not _axis_keys(mixed.post[-1], axis), (name, mixed.post[-1])
        return
    _assert_r_precondition(name, s.steps, mixed, worlds, probe)
    _assert_released_axis_equal(mixed, worlds, a, axis)
