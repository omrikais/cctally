"""Kernel seams the goldens round away (#443 S3 F28).

Three surfaces whose exact intermediate values matter but which rendered
output either rounds off or never reaches:

  * the per-call >200K tiered cache rate,
  * the explicit-dollar short-circuit, which exists at TWO structurally
    identical and independently reachable sites,
  * ``_sort_cache_rows`` across all six keys in both modes.

Each test carries the non-vacuity condition that makes it discriminate.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# claude-sonnet-4-5, not -4-6: only a handful of models carry
# `*_above_200k_tokens` rates, and -4-6 is not one of them. The
# precondition below fails loudly rather than silently measuring a flat
# rate if that ever changes.
TIERED_MODEL = "claude-sonnet-4-5"


def _pricing():
    import _lib_pricing
    return _lib_pricing.CLAUDE_MODEL_PRICING


def _zero_cost(_model, _usage, _mode, _cost):
    return 0.0


# === the tiered rate =====================================================

def test_the_tiered_model_actually_carries_above_200k_rates():
    """Non-vacuity: a model without tiered rates measures nothing."""
    p = _pricing()[TIERED_MODEL]
    for key in (
        "input_cost_per_token_above_200k_tokens",
        "cache_read_input_token_cost_above_200k_tokens",
        "cache_creation_input_token_cost_above_200k_tokens",
    ):
        assert key in p, (
            f"{TIERED_MODEL} lost {key}; pick a model that still has the "
            "tiered rate rather than weakening the assertions below"
        )


def test_cache_read_dollars_apply_the_tiered_rate_above_200k():
    import _lib_cache_report as crk
    pricing = _pricing()
    threshold = crk.DEFAULT_TIERED_THRESHOLD
    below = crk._compute_entry_cache_dollars(
        TIERED_MODEL, 0, threshold - 1, pricing=pricing)[0]
    above = crk._compute_entry_cache_dollars(
        TIERED_MODEL, 0, threshold * 2, pricing=pricing)[0]
    rate_below = below / (threshold - 1)
    rate_above = above / (threshold * 2)
    assert rate_above != rate_below, (
        "the >200K tier never engaged for cache reads; this asserts the "
        "blended RATE changed, not merely that the dollars differ"
    )


def test_cache_write_dollars_apply_the_tiered_rate_above_200k():
    import _lib_cache_report as crk
    pricing = _pricing()
    threshold = crk.DEFAULT_TIERED_THRESHOLD
    below = crk._compute_entry_cache_dollars(
        TIERED_MODEL, threshold - 1, 0, pricing=pricing)[1]
    above = crk._compute_entry_cache_dollars(
        TIERED_MODEL, threshold * 2, 0, pricing=pricing)[1]
    rate_below = below / (threshold - 1)
    rate_above = above / (threshold * 2)
    assert rate_above != rate_below, (
        "the >200K tier never engaged for cache writes"
    )


def test_the_tier_boundary_itself_is_not_crossed_at_exactly_200k():
    """`tokens > tiered_threshold`, strictly — 200,000 stays on base rate."""
    import _lib_cache_report as crk
    pricing = _pricing()
    threshold = crk.DEFAULT_TIERED_THRESHOLD
    at = crk._compute_entry_cache_dollars(
        TIERED_MODEL, 0, threshold, pricing=pricing)[0]
    one_below = crk._compute_entry_cache_dollars(
        TIERED_MODEL, 0, threshold - 1, pricing=pricing)[0]
    assert at / threshold == pytest.approx(one_below / (threshold - 1))


# === the explicit-dollar short-circuit, at BOTH sites ====================

EXPLICIT = {"cache_saved_usd": 99.0, "cache_wasted_usd": 7.0, "cache_net_usd": 92.0}
_SHORT_CIRCUIT_TOKENS = (4_000, 250_000)  # (cache_creation, cache_read)


def _day_net(explicit: bool):
    """Day aggregation — entries carry a ``usage`` dict."""
    import _lib_cache_report as crk
    cc, cr = _SHORT_CIRCUIT_TOKENS
    entry = NS(
        timestamp=dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.timezone.utc),
        model=TIERED_MODEL,
        cost_usd=0.0,
        usage={
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr,
        },
        **(EXPLICIT if explicit else {}),
    )
    rows = crk._aggregate_cache_by_day(
        [entry], display_tz=None, pricing=_pricing(),
        cost_calculator=_zero_cost,
    )
    return (rows[0].saved_usd, rows[0].wasted_usd, rows[0].net_usd)


def _breakdown_net(explicit: bool):
    """Breakdown aggregation — entries carry flat token attributes."""
    import _lib_cache_report as crk
    cc, cr = _SHORT_CIRCUIT_TOKENS
    entry = NS(
        model=TIERED_MODEL, input_tokens=0,
        cache_creation_tokens=cc, cache_read_tokens=cr,
        **(EXPLICIT if explicit else {}),
    )
    rows = crk._aggregate_cache_breakdown(
        [entry], key_fn=lambda _e: "k", pricing=_pricing(),
    )
    return (None, None, rows[0].net_usd)


@pytest.mark.parametrize("site", [_day_net, _breakdown_net], ids=["day", "breakdown"])
def test_explicit_dollars_short_circuit_the_recomputation(site):
    """The bypass exists twice and is independently reachable.

    Both are reachable from the Codex path — day rows and breakdowns are
    built from separate calls — so covering one can leave days and
    breakdowns disagreeing.

    NON-VACUITY: the fixture is built so the two paths would yield
    DIFFERENT dollars. A fixture where they agree passes without
    discriminating anything.
    """
    recomputed = site(explicit=False)
    explicit = site(explicit=True)
    assert recomputed[2] != pytest.approx(EXPLICIT["cache_net_usd"]), (
        "fixture precondition: the recomputed dollars must differ from the "
        f"explicit ones, got recomputed net={recomputed[2]}"
    )
    assert explicit[2] == pytest.approx(EXPLICIT["cache_net_usd"])


def test_a_partial_explicit_triple_falls_back_to_recomputation():
    """All three must be present; the guard is ``all(... is not None)``."""
    import _lib_cache_report as crk
    cc, cr = _SHORT_CIRCUIT_TOKENS
    entry = NS(
        model=TIERED_MODEL, input_tokens=0,
        cache_creation_tokens=cc, cache_read_tokens=cr,
        cache_saved_usd=99.0, cache_wasted_usd=7.0, cache_net_usd=None,
    )
    rows = crk._aggregate_cache_breakdown(
        [entry], key_fn=lambda _e: "k", pricing=_pricing(),
    )
    assert rows[0].net_usd != pytest.approx(92.0)
    assert rows[0].net_usd == pytest.approx(_breakdown_net(explicit=False)[2])


# === _sort_cache_rows, all six keys, both modes ==========================

SORT_KEYS = ("date", "net", "cache", "recent", "cost", "anomaly")
_T = dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.timezone.utc)


def _row(crk, *, date=None, session_id=None, last_activity=None,
         net=0.0, hit=0, cost=0.0, triggered=False):
    r = crk.CacheRow(date=date, session_id=session_id,
                     last_activity=last_activity)
    # cache_hit_percent is a derived property: read/(input+create+read).
    r.input_tokens = 100 - hit
    r.cache_read_tokens = hit
    r.net_usd = net
    r.cost = cost
    r.anomaly_triggered = triggered
    return r


def _day_rows(crk):
    """Four day rows whose ordering differs under every key."""
    spec = [
        # (date, net, hit, cost, triggered)
        ("2026-05-01", 3.0, 10, 1.0, False),
        ("2026-05-02", 1.0, 40, 4.0, False),
        ("2026-05-03", 4.0, 20, 2.0, True),
        ("2026-05-04", 2.0, 30, 3.0, False),
    ]
    return [
        _row(crk, date=d, net=n, hit=h, cost=c, triggered=t)
        for d, n, h, c, t in spec
    ]


def _session_rows(crk):
    spec = [
        # (session_id, hours, net, hit, cost, triggered)
        ("s-a", 1, 3.0, 10, 1.0, False),
        ("s-b", 2, 1.0, 40, 4.0, False),
        ("s-c", 3, 4.0, 20, 2.0, True),
        ("s-d", 4, 2.0, 30, 3.0, False),
    ]
    return [
        _row(crk, session_id=s, last_activity=_T + dt.timedelta(hours=hrs),
             net=n, hit=h, cost=c, triggered=t)
        for s, hrs, n, h, c, t in spec
    ]


def _sorted_labels(rows, key, mode, label):
    import _cctally_cache_report as cli
    cli._sort_cache_rows(rows, key, mode)
    return [label(r) for r in rows]


DAY_EXPECTED = {
    "date":    ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"],
    "net":     ["2026-05-02", "2026-05-04", "2026-05-01", "2026-05-03"],
    "cache":   ["2026-05-01", "2026-05-03", "2026-05-04", "2026-05-02"],
    "recent":  ["2026-05-04", "2026-05-03", "2026-05-02", "2026-05-01"],
    "cost":    ["2026-05-02", "2026-05-04", "2026-05-03", "2026-05-01"],
    # mode default (date) first, then a stable sort putting triggered first.
    "anomaly": ["2026-05-03", "2026-05-01", "2026-05-02", "2026-05-04"],
}

SESSION_EXPECTED = {
    "date":    ["s-a", "s-b", "s-c", "s-d"],
    "net":     ["s-b", "s-d", "s-a", "s-c"],
    "cache":   ["s-a", "s-c", "s-d", "s-b"],
    "recent":  ["s-d", "s-c", "s-b", "s-a"],
    "cost":    ["s-b", "s-d", "s-c", "s-a"],
    # mode default is NET in session mode, then triggered first.
    "anomaly": ["s-c", "s-b", "s-d", "s-a"],
}


@pytest.mark.parametrize("key", SORT_KEYS)
def test_day_mode_sort_keys(key):
    import _lib_cache_report as crk
    got = _sorted_labels(_day_rows(crk), key, "day", lambda r: r.date)
    assert got == DAY_EXPECTED[key]


@pytest.mark.parametrize("key", SORT_KEYS)
def test_session_mode_sort_keys(key):
    import _lib_cache_report as crk
    got = _sorted_labels(_session_rows(crk), key, "session",
                         lambda r: r.session_id)
    assert got == SESSION_EXPECTED[key]


@pytest.mark.parametrize(
    "expected,mode", [(DAY_EXPECTED, "day"), (SESSION_EXPECTED, "session")],
    ids=["day", "session"],
)
def test_every_sort_key_yields_a_distinct_order(expected, mode):
    """Non-vacuity: rows ordered the same way under two keys prove nothing.

    Without this, a fixture whose `net` and `cost` orders coincided would
    make either test pass no matter which branch actually ran.
    """
    orders = {tuple(v) for v in expected.values()}
    assert len(orders) == len(SORT_KEYS), (
        f"{mode}: two sort keys share an expected ordering, so at least one "
        "case cannot discriminate its branch"
    )


def test_day_mode_value_ties_break_on_ascending_date():
    import _lib_cache_report as crk
    rows = [
        _row(crk, date="2026-05-09", net=5.0, hit=50),
        _row(crk, date="2026-05-02", net=5.0, hit=50),
        _row(crk, date="2026-05-05", net=5.0, hit=50),
    ]
    assert _sorted_labels(rows, "net", "day", lambda r: r.date) == [
        "2026-05-02", "2026-05-05", "2026-05-09",
    ]


def test_session_mode_value_ties_break_on_date_then_ascending_session_id():
    import _lib_cache_report as crk
    rows = [
        _row(crk, session_id="s-z", last_activity=_T, net=5.0, hit=50),
        _row(crk, session_id="s-a", last_activity=_T, net=5.0, hit=50),
        _row(crk, session_id="s-m", last_activity=_T - dt.timedelta(hours=1),
             net=5.0, hit=50),
    ]
    assert _sorted_labels(rows, "net", "session", lambda r: r.session_id) == [
        "s-m", "s-a", "s-z",
    ]


def test_session_mode_recent_ties_break_on_ascending_session_id():
    import _lib_cache_report as crk
    rows = [
        _row(crk, session_id="s-z", last_activity=_T),
        _row(crk, session_id="s-a", last_activity=_T),
    ]
    assert _sorted_labels(rows, "recent", "session", lambda r: r.session_id) == [
        "s-a", "s-z",
    ]


def test_session_rows_without_last_activity_sort_last_under_recent():
    """`None -> 0.0`, which negates to 0 and lands after every real time."""
    import _lib_cache_report as crk
    rows = [
        _row(crk, session_id="s-none", last_activity=None),
        _row(crk, session_id="s-old", last_activity=_T - dt.timedelta(days=30)),
        _row(crk, session_id="s-new", last_activity=_T),
    ]
    assert _sorted_labels(rows, "recent", "session", lambda r: r.session_id) == [
        "s-new", "s-old", "s-none",
    ]
