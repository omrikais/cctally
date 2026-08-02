"""Entry order must not flip net_negative (#443 S3 F20/F21).

Reproduced against current embedded pricing with ORDINARY token counts —
no artificial explicit-dollar fields. Three ``claude-sonnet-4-6`` entries
whose nets cancel almost exactly:

    cache-read 1 token        net =  2.7e-06
    cache-read 999 tokens     net =  0.0026973
    cache-write 3,600 tokens  net = -0.0027

Four of the six naive-fold permutations sum to 0.0 and two sum to
-1.1434944787933055e-20, which trips the strict ``row.net_usd < 0``
predicate. Measured end to end through ``_aggregate_cache_by_day`` +
``_classify_anomalies``, entry order alone flipped ``anomaly_triggered``.

The defect lives in the SAME-MODEL ENTRY FOLD, one layer below the
model-to-row fold: applying ``stable_sum`` only at the outer layer leaves
it intact. These tests therefore drive the real aggregators, not just the
helper.

Note on the settled verdict: ``stable_sum`` is exactly-rounded, and the
true sum of these three binary floats IS negative (by ~1e-20), so every
order now agrees on ``net_negative=True``. ONE verdict is the invariant
under test. Whether the predicate should additionally carry the repo's
1e-9 USD tolerance is a SEPARATE decision on preserve-listed
``net_negative`` behavior and is deliberately not made here.
"""
from __future__ import annotations

import datetime as dt
import itertools
import pathlib
import sys
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

MODEL = "claude-sonnet-4-6"
SPECS = [(0, 1), (0, 999), (3600, 0)]  # (cache_creation, cache_read)
TS = dt.datetime(2026, 4, 15, 12, 0, tzinfo=dt.timezone.utc)


def _pricing():
    import _lib_pricing
    return _lib_pricing.CLAUDE_MODEL_PRICING


def _nets():
    import _lib_cache_report as crk
    out = []
    for cc, cr in SPECS:
        _s, _w, net = crk._compute_entry_cache_dollars(
            MODEL, cc, cr, pricing=_pricing(),
        )
        out.append(net)
    return out


def _zero_cost(_model, _usage, _mode, _cost):
    return 0.0


def test_the_three_entry_nets_still_straddle_zero():
    """Non-vacuity: if pricing moves these values, every test below is inert.

    The fixture only discriminates while the three nets very nearly
    cancel — a set that all shared one sign, or whose total was far from
    zero, would fold to the same verdict in every order no matter what
    the aggregator did.
    """
    nets = _nets()
    assert any(n < 0 for n in nets) and any(n > 0 for n in nets), (
        f"fixture precondition: nets must straddle zero, got {nets}"
    )
    assert abs(sum(sorted(nets))) < 1e-12, (
        f"fixture precondition: the nets must very nearly cancel, got {nets}"
    )


def test_the_naive_fold_is_order_dependent_on_this_fixture():
    """The defect itself, kept as the fixture's own witness.

    A left-to-right ``+=`` fold over these addends disagrees with itself
    across permutations. If this ever stops being true the fixture has
    gone inert and the aggregator tests below prove nothing.
    """
    nets = _nets()
    totals = set()
    for perm in itertools.permutations(range(len(nets))):
        total = 0.0
        for i in perm:
            total += nets[i]
        totals.add(total < 0)
    assert totals == {True, False}, (
        "fixture precondition: a naive fold must disagree across "
        f"permutations on this input, got {totals}"
    )


def test_stable_sum_yields_one_verdict_across_orders():
    import _lib_cache_report as crk
    nets = _nets()
    verdicts = {
        crk.stable_sum(nets[i] for i in perm) < 0
        for perm in itertools.permutations(range(len(nets)))
    }
    assert len(verdicts) == 1, (
        f"entry order alone changed net_negative across permutations: {verdicts}"
    )


def _day_entry(cc, cr):
    """The shape ``_aggregate_cache_by_day`` consumes: a ``usage`` dict.

    Deliberately carries NO ``cache_saved_usd`` / ``cache_wasted_usd`` /
    ``cache_net_usd`` — those would take the explicit-dollar
    short-circuit and bypass the arithmetic under test.
    """
    return NS(
        timestamp=TS,
        model=MODEL,
        cost_usd=0.0,
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
        },
    )


def test_day_aggregator_verdict_is_entry_order_independent():
    """The end-to-end case: the real day fold, all six permutations.

    All three entries share one model and one calendar day, so they land
    in the same ``_Bucket`` and the per-entry fold is what runs.
    """
    import _lib_cache_report as crk
    nets, verdicts = set(), set()
    for perm in itertools.permutations(range(len(SPECS))):
        rows = crk._aggregate_cache_by_day(
            [_day_entry(*SPECS[i]) for i in perm],
            display_tz=None, pricing=_pricing(), cost_calculator=_zero_cost,
        )
        assert len(rows) == 1, "fixture precondition: one model, one day"
        crk._classify_anomalies(rows, threshold_pp=15, window_days=14)
        nets.add(rows[0].net_usd)
        verdicts.add(rows[0].anomaly_triggered)
    assert len(nets) == 1, f"entry order changed the day row's net_usd: {nets}"
    assert len(verdicts) == 1, (
        f"entry order changed the day row's anomaly verdict: {verdicts}"
    )


def _session_entry(cc, cr, idx):
    return NS(
        timestamp=TS + dt.timedelta(seconds=idx),
        model=MODEL,
        session_id="s3-stable-sum-session",
        source_path="/synthetic/jsonl/s3-stable-sum.jsonl",
        project_path="/synthetic/repos/s3",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=cc,
        cache_read_tokens=cr,
    )


def test_session_aggregator_verdict_is_entry_order_independent():
    """The session fold carries the identical two-layer shape."""
    import _lib_cache_report as crk
    nets, verdicts = set(), set()
    for perm in itertools.permutations(range(len(SPECS))):
        entries = [_session_entry(*SPECS[i], idx) for idx, i in enumerate(perm)]
        result = crk._aggregate_cache_by_session(
            entries, pricing=_pricing(), cost_calculator=_zero_cost,
            project_decoder=lambda _p: "/synthetic/repos/s3",
        )
        assert len(result.rows) == 1, "fixture precondition: one session"
        crk._classify_anomalies(result.rows, threshold_pp=15, window_days=14)
        nets.add(result.rows[0].net_usd)
        verdicts.add(result.rows[0].anomaly_triggered)
    assert len(nets) == 1, f"entry order changed the session net_usd: {nets}"
    assert len(verdicts) == 1, (
        f"entry order changed the session anomaly verdict: {verdicts}"
    )


def _breakdown_entry(cc, cr):
    return NS(
        model=MODEL,
        input_tokens=0,
        cache_creation_tokens=cc,
        cache_read_tokens=cr,
    )


def test_breakdown_aggregator_net_is_entry_order_independent():
    """by-project / by-model share the same per-entry fold."""
    import _lib_cache_report as crk
    nets = set()
    for perm in itertools.permutations(range(len(SPECS))):
        rows = crk._aggregate_cache_breakdown(
            [_breakdown_entry(*SPECS[i]) for i in perm],
            key_fn=lambda _e: "one-bucket", pricing=_pricing(),
        )
        assert len(rows) == 1, "fixture precondition: one bucket"
        nets.add(rows[0].net_usd)
    assert len(nets) == 1, f"entry order changed the breakdown net_usd: {nets}"


def test_breakdown_from_rows_net_is_model_order_independent():
    """The row-fold twin: per-model children combined across day rows.

    One model bucket per row, so the fold that runs is the cross-row
    ``net_usd`` combination rather than the per-entry one.
    """
    import _lib_cache_report as crk
    nets = _nets()
    out = set()
    for perm in itertools.permutations(range(len(nets))):
        day_rows = []
        for i in perm:
            row = crk.CacheRow(date="2026-04-15")
            row.model_breakdowns = [crk.CacheModelBreakdown(
                model_name=MODEL, input_tokens=0, output_tokens=0,
                cache_creation_tokens=0, cache_read_tokens=0,
                cache_hit_percent=0.0, cost=0.0,
                saved_usd=0.0, wasted_usd=0.0, net_usd=nets[i],
            )]
            day_rows.append(row)
        rows = crk._aggregate_cache_breakdown_from_rows(day_rows)
        assert len(rows) == 1, "fixture precondition: one model bucket"
        out.add(rows[0].net_usd)
    assert len(out) == 1, f"row order changed the by-model net_usd: {out}"
