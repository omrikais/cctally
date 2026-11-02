"""#944 spec 5.2/5.3 — the pure write-time attribution kernel.

Every case names the failure it detects: a Claude cost record attributed to
the wrong account around an account switch (the rule), or a record inheriting
another record's durable decision (the identity encoders and legacy matching).
"""
import pytest

import _lib_account_binding as binding_lib
import _lib_accounts
import _lib_claude_attribution as K

A = _lib_accounts.account_key("claude", "acct-a")
B = _lib_accounts.account_key("claude", "acct-b")
INGEST = _lib_accounts.account_key("claude", "acct-ingest")
SEC = 1_000_000


def _us(text):
    return K.capture_interval(text)[0]


def _instant(text, *accounts):
    lo, hi = K.capture_interval(text)
    return K.Instant(lo, hi - lo, frozenset(accounts))


FAR_CUTOFF = _us("2026-10-09T23:00:00Z")


def _decide(ts, before=None, overlapping=(), after=None, *, cutoff=FAR_CUTOFF,
            capture=None):
    neighbours = K.Neighbours(before, tuple(overlapping), after)
    return K.decide(K.record_interval(ts), neighbours, clock_cutoff_us=cutoff,
                    closing_capture=capture, ingest_account=INGEST)


def _expect(decision, account, basis, reason):
    assert (decision.account_key, decision.basis, decision.reason) == (
        account, basis, reason)


# --------------------------------------------------------------------------
# the 5.2 table, row by row (detects: a mis-attributed record around a switch)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,reason", [
    (None, "missing_timestamp"),
    ("", "missing_timestamp"),
    ("not-a-time", "invalid_timestamp"),
    (12345, "invalid_timestamp"),
])
def test_unusable_timestamp_falls_back_with_its_reason(value, reason):
    before = _instant("2026-10-09T10:00:00Z", A)
    after = _instant("2026-10-09T10:05:00Z", A)
    _expect(_decide(value, before, (), after), INGEST, "fallback", reason)


def test_record_not_ending_by_the_cutoff_is_clock_order():
    before = _instant("2026-10-09T10:00:00Z", A)
    after = _instant("2026-10-09T10:05:00Z", A)
    cutoff = _us("2026-10-09T10:01:00Z")
    _expect(_decide("2026-10-09T10:01:00.000Z", before, (), after,
                    cutoff=cutoff), INGEST, "fallback", "clock_order")
    # Ending exactly at the cutoff passes the check.
    _expect(_decide("2026-10-09T10:00:59.999Z", before, (), after,
                    cutoff=cutoff), A, "timeline_agreement", None)


def test_no_preceding_instant_is_pre_timeline():
    after = _instant("2026-10-09T10:05:00Z", A)
    _expect(_decide("2026-10-09T10:01:00.000Z", None, (), after),
            INGEST, "fallback", "pre_timeline")
    assert K.decide(K.record_interval("2026-10-09T10:01:00.000Z"), None,
                    clock_cutoff_us=FAR_CUTOFF, closing_capture=None,
                    ingest_account=INGEST).reason == "pre_timeline"


@pytest.mark.parametrize("where", ["before", "overlapping", "after"])
def test_an_ambiguous_neighbour_is_never_skipped(where):
    ambiguous = {"before": _instant("2026-10-09T10:00:00Z", A, B),
                 "overlapping": _instant("2026-10-09T10:01:00Z", A, B),
                 "after": _instant("2026-10-09T10:05:00Z", A, B)}
    clean = {"before": _instant("2026-10-09T10:00:00Z", A),
             "overlapping": None,
             "after": _instant("2026-10-09T10:05:00Z", A)}
    parts = dict(clean)
    parts[where] = ambiguous[where]
    overlapping = () if parts["overlapping"] is None else (parts["overlapping"],)
    _expect(_decide("2026-10-09T10:01:00.500Z", parts["before"], overlapping,
                    parts["after"]),
            INGEST, "fallback", "ambiguous_instant")


def test_no_following_instant_and_no_capture_is_open_tail():
    before = _instant("2026-10-09T10:00:00Z", A)
    _expect(_decide("2026-10-09T10:01:00.000Z", before, (), None),
            INGEST, "fallback", "open_tail")


def test_agreement_names_the_shared_account():
    before = _instant("2026-10-09T10:00:00Z", A)
    after = _instant("2026-10-09T10:05:00Z", A)
    decision = _decide("2026-10-09T10:01:00.000Z", before, (), after)
    _expect(decision, A, "timeline_agreement", None)
    assert decision.used_closing_capture is False


def test_differing_neighbours_are_a_switch_window():
    before = _instant("2026-10-09T10:00:00Z", A)
    after = _instant("2026-10-09T10:05:00Z", B)
    _expect(_decide("2026-10-09T10:01:00.000Z", before, (), after),
            INGEST, "fallback", "switch_window")


def test_closing_capture_serves_as_the_following_instant():
    before = _instant("2026-10-09T10:00:00Z", A)
    agree = _decide("2026-10-09T10:01:00.000Z", before, (), None,
                    capture=K.ClosingCapture(A, _us("2026-10-09T10:02:00Z")))
    _expect(agree, A, "timeline_agreement", None)
    assert agree.used_closing_capture is True
    switched = _decide("2026-10-09T10:01:00.000Z", before, (), None,
                       capture=K.ClosingCapture(B, _us("2026-10-09T10:02:00Z")))
    _expect(switched, INGEST, "fallback", "switch_window")


def test_a_following_observation_wins_over_the_closing_capture():
    before = _instant("2026-10-09T10:00:00Z", A)
    after = _instant("2026-10-09T10:05:00Z", A)
    decision = _decide("2026-10-09T10:01:00.000Z", before, (), after,
                       capture=K.ClosingCapture(B, _us("2026-10-09T10:06:00Z")))
    _expect(decision, A, "timeline_agreement", None)
    assert decision.used_closing_capture is False


def test_the_overlapping_conflict_example_of_section_5_2():
    """A@12:00:00 (second precision), B@12:00:01, T=12:00:00.500: the A
    observation overlaps R and the B one follows it, so they differ."""
    a = _instant("2026-10-09T12:00:00Z", A)
    b = _instant("2026-10-09T12:00:01Z", B)
    rec = K.record_interval("2026-10-09T12:00:00.500Z")
    assert K.relation(a.interval, rec) == "overlapping"
    assert K.relation(b.interval, rec) == "after"
    earlier = _instant("2026-10-09T11:59:30Z", A)
    _expect(_decide("2026-10-09T12:00:00.500Z", earlier, (a,), b),
            INGEST, "fallback", "switch_window")


def test_an_overlapping_instant_agreeing_with_both_neighbours_agrees():
    before = _instant("2026-10-09T11:59:30Z", A)
    over = _instant("2026-10-09T12:00:00Z", A)
    after = _instant("2026-10-09T12:00:30Z", A)
    _expect(_decide("2026-10-09T12:00:00.500Z", before, (over,), after),
            A, "timeline_agreement", None)


def test_an_overlapping_instant_never_stands_in_for_the_predecessor():
    over = _instant("2026-10-09T12:00:00Z", A)
    after = _instant("2026-10-09T12:00:30Z", A)
    _expect(_decide("2026-10-09T12:00:00.500Z", None, (over,), after),
            INGEST, "fallback", "pre_timeline")


def test_a_null_observation_from_a_real_stably_absent_binding_agrees_on_null():
    document = binding_lib.capture({
        "status": "stably_absent", "account_key": "unattributed",
        "natural_id": None, "email": None, "plan_type": None}).document()
    loaded = binding_lib.load(document)
    normalized = K.normalize_binding_account(loaded.account_key)
    assert normalized is None
    assert K.normalize_binding_account(A) == A
    before = _instant("2026-10-09T10:00:00Z", normalized)
    after = _instant("2026-10-09T10:05:00Z", normalized)
    _expect(_decide("2026-10-09T10:01:00.000Z", before, (), after),
            None, "timeline_agreement", None)


def test_precision_follows_the_written_fractional_digits():
    assert K.capture_interval("2026-10-09T12:00:00Z")[1] - K.capture_interval(
        "2026-10-09T12:00:00Z")[0] == SEC
    lo, hi = K.record_interval("2026-10-09T12:00:00.500Z")
    assert hi - lo == 1000
    lo6, hi6 = K.record_interval("2026-10-09T12:00:00.123456+00:00")
    assert hi6 - lo6 == 1
    assert K.record_interval("2026-10-09T14:00:00+02:00")[0] == _us(
        "2026-10-09T12:00:00Z")
    # A second-precision capture at 12:00:00 overlaps a millisecond record
    # at 12:00:00.500, but a millisecond capture at .499 precedes it.
    rec = K.record_interval("2026-10-09T12:00:00.500Z")
    assert K.relation(K.capture_interval("2026-10-09T12:00:00Z"), rec) == (
        "overlapping")
    assert K.relation(K.capture_interval("2026-10-09T12:00:00.499Z"), rec) == (
        "before")
    assert K.relation(K.capture_interval("2026-10-09T12:00:00.501Z"), rec) == (
        "after")
    with pytest.raises(ValueError):
        K.capture_interval("yesterday")


def test_fixed_width_iso_round_trips_and_sorts():
    value = _us("2026-10-09T12:00:00.5Z")
    text = K.iso_from_us(value)
    assert text == "2026-10-09T12:00:00.500000Z"
    assert K.us_from_iso(text) == value
    assert K.iso_from_us(value - 1) < text < K.iso_from_us(value + SEC)


# --------------------------------------------------------------------------
# identity encoders (detects: a record inheriting another record's decision)
# --------------------------------------------------------------------------

def _row(**over):
    row = {
        "timestamp_utc": "2026-10-09T10:00:00+00:00",
        "model": "claude-sonnet-4-5", "input_tokens": 10, "output_tokens": 20,
        "cache_create_tokens": 0, "cache_read_tokens": 5,
        "cache_create_1h_tokens": None, "cache_create_5m_tokens": None,
        "speed": None, "cost_usd_raw": 0.0012,
    }
    row.update(over)
    return row


def test_two_spellings_of_one_root_give_distinct_locations():
    real = K.loc_for("/Users/u/.claude/projects/p/s.jsonl")
    alias = K.loc_for("/Users/u/.config/claude/projects/p/s.jsonl")
    assert real != alias
    assert len(real) == 32 and int(real, 16) >= 0
    assert K.loc_for("/Users/u/.claude/projects/p/s.jsonl") == real


def test_key_forms_are_distinct_and_deterministic():
    keyed = K.keyed_key("msg_1", "req_1")
    assert keyed == 'k1:["msg_1","req_1"]'
    loc = K.loc_for("/p/s.jsonl")
    phys = K.physical_key(loc, 4096, b'{"a":1}')
    assert phys.startswith("p1:") and phys == K.physical_key_from_digest(
        loc, 4096, K.record_sha256(b'{"a":1}'))
    assert phys != K.physical_key(loc, 4096, b'{"a":2}')
    assert phys != K.physical_key(loc, 4097, b'{"a":1}')
    projection = K.legacy_projection(_row())
    legacy = K.legacy_key(loc, 4096, projection)
    assert legacy.startswith("l1:")
    assert legacy == K.legacy_key_from_digest(
        loc, 4096, K.projection_sha256(projection))


def test_legacy_projection_order_and_types():
    projection = K.legacy_projection(_row(speed="fast", cost_usd_raw=1))
    assert projection == ["2026-10-09T10:00:00+00:00", "claude-sonnet-4-5",
                          10, 20, 0, 5, None, None, "fast", 1.0]
    assert type(projection[9]) is float
    assert K.projection_json(projection) == (
        '["2026-10-09T10:00:00+00:00","claude-sonnet-4-5",10,20,0,5,'
        'null,null,"fast",1.0]')


def test_a_projection_differing_only_in_cache_read_tokens_is_a_new_identity():
    loc = K.loc_for("/p/s.jsonl")
    first = K.legacy_projection(_row())
    replaced = K.legacy_projection(_row(cache_read_tokens=6))
    assert K.legacy_key(loc, 0, first) != K.legacy_key(loc, 0, replaced)
    assert K.legacy_match(first, replaced) is None


def test_legacy_matching_with_a_recorded_unknown_split():
    unknown = K.legacy_projection(_row())
    filled = K.legacy_projection(_row(cache_create_1h_tokens=0,
                                      cache_create_5m_tokens=0))
    other_known = K.legacy_projection(_row(cache_create_1h_tokens=3,
                                           cache_create_5m_tokens=0))
    assert K.legacy_match(unknown, unknown) == "exact"
    assert K.legacy_match(unknown, filled) == "unknown_split"
    assert K.legacy_match(filled, filled) == "exact"
    assert K.legacy_match(filled, other_known) is None
    # A known split never matches an unknown one in the other direction.
    assert K.legacy_match(filled, unknown) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_candidate_precedence_in_both_insertion_orders(reverse):
    candidates = [
        ("unknown_split", ("observations-2026-10.jsonl", 10), A),
        ("exact", ("observations-2026-10.jsonl", 900), B),
        ("exact", ("observations-2026-11.jsonl", 5), A),
        ("unknown_split", ("bootstrap-2026-07.jsonl", 0), B),
        (None, ("observations-2026-01.jsonl", 0), A),
    ]
    if reverse:
        candidates.reverse()
    ordered = K.order_legacy_candidates(candidates)
    assert [c[2] for c in ordered] == [B, A, B, A]
    assert ordered[0][1] == ("observations-2026-10.jsonl", 900)
    assert ordered[2][1] == ("bootstrap-2026-07.jsonl", 0)
