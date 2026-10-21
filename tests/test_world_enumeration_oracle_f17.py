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


@pytest.mark.parametrize("variant", WE.VARIANTS, ids=_vid)
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_f17_family(store, family, variant):
    skel = FAMILIES[family]
    traces = WE.mixed_traces(skel, variant)
    WE.run_and_check(store, traces)
    label = next(label for label in traces if label.endswith("/00"))
    reference = _uninterrupted_no_exclusion(store, traces[label], variant)
    assert WE.outcome_total(reference[-1]) >= skel.genuine, (
        f"non-vacuity: {family} mints its genuine outcomes without exclusion",
        reference[-1])


RECOVERY_VARIANTS = (WE.Variant(0, "api", 0), WE.Variant(1, "statusline", -1))


@pytest.mark.parametrize("variant", RECOVERY_VARIANTS, ids=_vid)
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_f17_certified_recovery_mints_the_genuine_outcome(store, family, variant):
    """The mixed trace, quarantined by its first excluded reading, recovers at
    the idle-gap witness and then mints the genuine credit — which every
    reference world mints too (`check`)."""
    skel = FAMILIES[family]
    # Two of the gate pairs (the no-exclusion pair and one with both slots
    # non-trivial) carry the recovery suffix; `CCTALLY_F17_FULL=1` runs every
    # pair.
    pairs = None if len(WE.value_pairs()) > len(WE.GATE_PAIRS) else (
        WE.GATE_PAIRS[0], WE.GATE_PAIRS[2])
    traces = WE.mixed_traces(skel, variant, recovery=True, pairs=pairs)
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
    return steps + [witness, arm, ("rebuild", {}), confirm, further]


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


# Each legacy candidate (a frozen render the current reducer never reads) is
# interpreted, per world, as absent or as a candidate in the A, B or
# `unattributed` partition; two legacy candidates give 16 worlds. The mixed
# reducer sees only current candidates, and its drop-consensus gate is the
# kernel's own state (`QuarantineState`), with both certificates required as
# the real reducer requires them (`_cctally_quarantine.control_recovered`):
# the legacy write is a trigger the moment it is observed (the reducer
# journals `legacy_spool_activity` before any publication planned from that
# snapshot), and, being spool-only, it leaves the unresolved marker of spec
# revision 3 (section 5.6 item 1(b)), which no later event clears.
# Outcomes are drop-consensus publications by their frozen (account, axis,
# canonical window, value); at every tick the mixed multiset must be inside
# every world's.

import _lib_statusline_candidates as SC

KEY = 500_000
TICK = 10
NOW0 = 1_791_540_000
BASELINE = 70.0
ACCOUNTS = ("A", "B", "unattributed")
#: Four representative legacy candidate values: agreeing with the current
#: drop, the database baseline (a dissent), a rise, and a deeper drop.
LEGACY_VALUES = (20.0, BASELINE, 90.0, 5.0)
CURRENT_DROP = 20.0
RECOVERED_DROP = 30.0
RECOVERED_BASELINE = 55.0


def _projection(percent, generation=0, captured=NOW0 - 1):
    return SC.DbProjection(five_hour=None, seven_day=SC.AxisProjection(
        percent=percent, raw_resets_at=KEY, canonical_key=KEY,
        captured_at=captured, source="statusline", reset_generation=generation))


def _fresh_partition(percent=BASELINE, generation=0, captured=NOW0 - 1):
    db = _projection(percent, generation, captured)
    return {"db": db, "control": SC.ControlState(
        db_projection=db, pending_drops={"fiveHour": None, "sevenDay": None})}


def _candidate(token, percent, received_at):
    return SC.Candidate(token=token * 64, received_at=received_at,
                        seven_day=SC.AxisValue(percent, KEY, canonical_key=KEY))


def _witness(account):
    at = NOW0 + 10 * 86400
    return Q.Reading(
        observation_id=f"witness-{account}", account_key=account,
        captured_at_utc=T.iso(at), capture_epoch=at, detection_epoch=at,
        week_start_date="2026-10-19", week_start_at_epoch=at - 3600,
        weekly_end_epoch=at + 7 * 86400 - 3600, five_hour_key=at + 4 * 3600,
        raw_five_hour_reset=at + 4 * 3600, two_axis=True)


def _drop_schedule(values, primary, *, recovery):
    """Ticks: ``(now, {account: [current candidates]}, legacy appearances,
    event)``. Two current sessions of the primary account report the drop
    with staggered refreshes; legacy candidates appear at ticks 1 and 3."""
    ticks = []
    for index in range(10):
        now = NOW0 + index * TICK
        current = {primary: [_candidate("a", CURRENT_DROP, now),
                             _candidate("b", CURRENT_DROP, now - 1)]}
        legacy = {1: (0, values[0]), 3: (1, values[1])}.get(index)
        ticks.append((now, current, legacy, None))
    if recovery:
        # An idle gap longer than any candidate's lifetime, then the bound
        # authoritative publication that is the control witness, then a
        # genuine drop reported by both sessions.
        start = NOW0 + 10 * 86400
        ticks.append((start, {}, None, "authoritative"))
        for index in range(1, 8):
            now = start + index * TICK
            ticks.append((now, {primary: [
                _candidate("a", RECOVERED_DROP, now),
                _candidate("b", RECOVERED_DROP, now - 1)]}, None, None))
    return ticks


def _run_reducer(schedule, *, world=None, primary="A"):
    """Run the schedule; return the cumulative publication multiset after
    every tick. ``world`` None is the mixed run."""
    partitions = {account: _fresh_partition() for account in ACCOUNTS}
    state = Q.QuarantineState()
    present = {}            # legacy slot -> (account, candidate)
    published = collections.Counter()
    out = []
    for now, current, legacy, event in schedule:
        if event == "authoritative":
            present.clear()   # every legacy render has aged out
            partitions[primary] = _fresh_partition(
                RECOVERED_BASELINE, generation=1, captured=now - 1)
            if world is None:
                # The fresh two-axis reading cannot certify while the spool
                # marker is unresolved; the OAuth publication still
                # reconciles the control certificate (F15 (d)).
                assert not state.on_reading(primary, _witness(primary)).allowed
                state.on_control_witness(primary, observation_id="oauth",
                                         captured_at_utc=T.iso(now))
                assert not state.control_quarantined(primary)
                assert state.accounting_quarantined(primary)
            out.append(collections.Counter(published))
            continue
        if legacy is not None:
            slot, value = legacy
            if world is None:
                import _cctally_quarantine as G
                state.on_trigger(kind=G.TRIGGER_LEGACY_SPOOL, reason="changed")
            elif world[slot] is not None:
                present[slot] = (world[slot], _candidate(
                    "cd"[slot], value, now))
        for account in ACCOUNTS:
            part = partitions[account]
            candidates = list(current.get(account, ()))
            candidates += [cand for owner, cand in present.values()
                           if owner == account]
            allowed = True if world is not None else not (
                state.accounting_quarantined(account)
                or state.control_quarantined(account))
            decision = SC.reduce_candidates(
                tuple(candidates), db=part["db"], control=part["control"],
                tombstones={"fiveHour": None, "sevenDay": None},
                now_epoch=now, account_key=account,
                drop_consensus_allowed=allowed)
            plan = decision.plan.seven_day if decision.plan else None
            if decision.action == "PUBLISH_DB" and plan is not None:
                if plan.percent < part["db"].seven_day.percent:
                    published[(account, "sevenDay", plan.canonical_key,
                               plan.percent)] += 1
                partitions[account] = _fresh_partition(
                    plan.percent, part["db"].seven_day.reset_generation,
                    captured=now)
            else:
                part["control"] = decision.control
        out.append(collections.Counter(published))
    return out


def _check_drop_consensus(schedule, primary):
    mixed = _run_reducer(schedule, primary=primary)
    worlds = {world: _run_reducer(schedule, world=world, primary=primary)
              for world in itertools.product((None,) + ACCOUNTS, repeat=2)}
    first = next(index for index, tick in enumerate(schedule) if tick[2])
    for index, have in enumerate(mixed):
        for world, result in worlds.items():
            if index < first:
                assert result[index] == have, (world, index)
            missing = have - result[index]
            if missing:
                violation = WE.OracleViolation(
                    f"tick {index}: {dict(missing)} absent from world {world}")
                violation.world, violation.missing = world, dict(missing)
                raise violation
    return mixed, worlds


@pytest.mark.parametrize("primary", ["A", "B"])
def test_f17_drop_consensus_is_withheld_in_every_mixed_schedule(primary):
    for values in itertools.product(LEGACY_VALUES, repeat=2):
        mixed, worlds = _check_drop_consensus(
            _drop_schedule(values, primary, recovery=False), primary)
        assert not mixed[-1], (values, mixed[-1])
    # Non-vacuity: without the legacy renders the drop publishes by consensus.
    reference = worlds[(None, None)][-1]
    assert reference[(primary, "sevenDay", KEY, CURRENT_DROP)] == 1, reference


@pytest.mark.parametrize("primary", ["A", "B"])
def test_f17_drop_consensus_stays_withheld_after_spool_only_activity(primary):
    """Spec revision 3, F15 (f): a spool-trigger family's expected outcome is
    persistent drop withholding. An idle gap longer than every candidate's
    lifetime, a fresh two-axis reading and the control witness leave the
    unresolved marker in place, so the genuine recovered drop is withheld.
    Positive control: the no-trigger world publishes both drops."""
    for values in itertools.product(LEGACY_VALUES, repeat=2):
        mixed, worlds = _check_drop_consensus(
            _drop_schedule(values, primary, recovery=True), primary)
        assert not mixed[-1], (values, mixed[-1])
    reference = worlds[(None, None)][-1]
    assert reference == collections.Counter({
        (primary, "sevenDay", KEY, CURRENT_DROP): 1,
        (primary, "sevenDay", KEY, RECOVERED_DROP): 1}), reference


def test_f17_drop_consensus_oracle_rejects_an_open_gate(monkeypatch):
    """With the gate forced open the mixed run publishes the current drop,
    which the worlds holding a dissenting legacy candidate lack."""
    monkeypatch.setattr(Q.QuarantineState, "control_quarantined",
                        lambda self, account_key: False)
    monkeypatch.setattr(Q.QuarantineState, "accounting_quarantined",
                        lambda self, account_key: False)
    with pytest.raises(WE.OracleViolation) as raised:
        _check_drop_consensus(
            _drop_schedule((BASELINE, BASELINE), "A", recovery=False), "A")
    assert raised.value.missing == {("A", "sevenDay", KEY, CURRENT_DROP): 1}
    assert "A" in raised.value.world
