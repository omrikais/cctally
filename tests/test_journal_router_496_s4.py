"""#496 S4 — the pure router kernel.

The `last_seen_utc` contribution rule is the load-carrying part: a naive
provider-wide maximum over every legacy line over-counts, because
`_normalize_legacy_account_stamp` writes a legacy evt/op account into
`payload.account_key` and `_account_of` reads only a top-level `account`
field or an `account_observe` op's key. See spec section 4.6.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import _lib_journal_router as R


UNATTR = "unattributed"
CUTOVER = "acct-claude-legacy"


def _provider_of_legacy(record):
    """Stand-in for `classify_legacy_provider` in kernel unit tests."""
    if record.get("account"):
        return None
    return record.get("provider")


def test_retained_types_match_the_doctor_conflict_scan():
    assert R.RETAINED_RECORD_TYPES == frozenset(
        {"evt", "correction", "correction_batch", "op"})


def test_retained_types_do_not_drift_from_the_doctor_scan():
    """Both feed the same shared selector; a drift would make the rebuild and
    the doctor disagree about which records a conflict can involve."""
    import _cctally_doctor as D
    assert R.RETAINED_RECORD_TYPES == D._CONFLICT_SCAN_RECORD_TYPES


def test_selector_slot_keeps_positions_without_retaining_observation_dicts():
    """The selector input is one pointer per decoded line, not one decoded map.

    A large observation population must therefore grow only the positional
    placeholder list; the retained decoded objects track decision history.
    """
    observations = [
        {"t": "obs", "payload": {"blob": "x" * 4096}, "id": f"o:{index}"}
        for index in range(2_000)
    ]
    decision = {"t": "evt", "id": "sa:decision", "payload": {}}

    slots = [R.selector_slot(record) for record in [*observations, decision]]

    assert len(slots) == len(observations) + 1
    assert slots[:-1] == [None] * len(observations)
    assert slots[-1] is decision
    assert [slot for slot in slots if slot is not None] == [decision]


def test_stamped_record_contributes_under_its_own_key():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "account": "a1", "at": "2026-01-02T00:00:00Z"},
                _provider_of_legacy)
    acc.observe({"t": "obs", "account": "a1", "at": "2026-01-01T00:00:00Z"},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {"a1": "2026-01-02T00:00:00Z"}


def test_account_observe_op_contributes_its_payload_key():
    acc = R.LastSeenAccumulator()
    acc.observe(
        {"t": "op", "at": "2026-01-03T00:00:00Z",
         "payload": {"kind": "account_observe", "account_key": "a2"}},
        _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {"a2": "2026-01-03T00:00:00Z"}


def test_legacy_claude_observation_folds_into_the_cutover_account():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "provider": "claude", "at": "2026-02-01T00:00:00Z"},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {CUTOVER: "2026-02-01T00:00:00Z"}


def test_legacy_codex_observation_folds_into_unattributed():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "provider": "codex", "at": "2026-02-02T00:00:00Z"},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {UNATTR: "2026-02-02T00:00:00Z"}


def test_legacy_event_does_not_contribute_even_when_later():
    """The defect the pre-plan gate caught. A legacy evt is normalized into
    `payload.account_key`, which `_account_of` does not read, so it never
    advances `last_seen_utc` today."""
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "account": "a1", "at": "2026-01-01T00:00:00Z"},
                _provider_of_legacy)
    acc.observe({"t": "evt", "provider": "claude", "at": "2099-01-01T00:00:00Z",
                 "payload": {"kind": "snapshot_accept"}},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {"a1": "2026-01-01T00:00:00Z"}


def test_legacy_vendor_tagged_budget_event_does_not_contribute():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "evt", "provider": "claude", "at": "2099-01-01T00:00:00Z",
                 "payload": {"kind": "budget", "vendor": "claude"}},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {}


def test_legacy_non_observe_op_does_not_contribute():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "op", "provider": "claude", "at": "2099-01-01T00:00:00Z",
                 "payload": {"kind": "weekly_credit_floor"}},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {}


def test_unrecognized_legacy_observation_does_not_contribute():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "at": "2099-01-01T00:00:00Z"}, _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {}


def test_record_without_at_is_ignored():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "account": "a1"}, _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {}


def test_legacy_bucket_does_not_lower_an_existing_stamped_maximum():
    acc = R.LastSeenAccumulator()
    acc.observe({"t": "obs", "account": CUTOVER, "at": "2026-05-01T00:00:00Z"},
                _provider_of_legacy)
    acc.observe({"t": "obs", "provider": "claude", "at": "2026-01-01T00:00:00Z"},
                _provider_of_legacy)
    assert acc.resolve(CUTOVER, UNATTR) == {CUTOVER: "2026-05-01T00:00:00Z"}


def test_accumulator_reproduces_the_live_derivation_over_the_same_records():
    """The kernel and `_derive_account_last_seen` must agree exactly.

    The live rule is "normalize, then take `_account_of`". This drives both over
    one mixed record set and compares the maps, so the kernel cannot drift from
    the derivation it replaces without a failure here.
    """
    import _cctally_journal as jr

    records = [
        {"t": "obs", "account": "a1", "at": "2026-01-01T00:00:00Z",
         "provider": "claude", "payload": {}},
        {"t": "obs", "provider": "claude", "at": "2026-03-01T00:00:00Z",
         "src": "record-usage", "payload": {"weekly_percent": 1.0}},
        {"t": "obs", "provider": "codex", "at": "2026-04-01T00:00:00Z",
         "src": "codex-quota", "payload": {"kind": "quota_window_snapshot"}},
        {"t": "evt", "at": "2099-01-01T00:00:00Z",
         "payload": {"kind": "snapshot_accept"}},
        {"t": "evt", "at": "2099-02-01T00:00:00Z",
         "payload": {"kind": "budget", "vendor": "claude"}},
        {"t": "op", "at": "2026-06-01T00:00:00Z",
         "payload": {"kind": "account_observe", "account_key": "a2"}},
        {"t": "op", "at": "2099-03-01T00:00:00Z",
         "payload": {"kind": "weekly_credit_floor"}},
        {"t": "correction", "at": "2099-04-01T00:00:00Z", "payload": {}},
        # An `account_observe` op that ALSO carries a top-level `account`
        # naming a different key. Both implementations prefer the top-level
        # field, and nothing else in this set makes the two fields disagree, so
        # without this record a kernel that read `payload.account_key` first
        # would still pass.
        {"t": "op", "account": "a3", "at": "2026-07-01T00:00:00Z",
         "payload": {"kind": "account_observe", "account_key": "a4"}},
    ]

    acc = R.LastSeenAccumulator()
    for record in records:
        acc.observe(record, jr.classify_legacy_provider)
    streamed = acc.resolve(CUTOVER, UNATTR)

    normalized = [dict(r, payload=dict(r.get("payload") or {})) for r in records]
    for record in normalized:
        jr._normalize_legacy_account_stamp(record, CUTOVER)
    expected: dict = {}
    for record in normalized:
        key = jr._account_of(record)
        at = record.get("at")
        if not key or not at:
            continue
        if expected.get(key) is None or at > expected[key]:
            expected[key] = at

    assert streamed == expected


# ==========================================================================
# PrefixHashAccumulator — the streaming form of `journal_prefix_hash`
# ==========================================================================


def _reference_digest(frames) -> str:
    """`journal_prefix_hash`'s framing, written out independently."""
    digest = hashlib.sha256()
    for name, data in frames:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def test_prefix_hash_matches_the_framing_at_a_segment_boundary():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\ntwo\n")
    assert acc.digest_at(("a.jsonl", 8)) == _reference_digest(
        [("a.jsonl", b"one\ntwo\n")])


def test_prefix_hash_matches_the_framing_mid_segment():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\ntwo\n")
    assert acc.digest_at(("a.jsonl", 4)) == _reference_digest(
        [("a.jsonl", b"one\n")])


def test_prefix_hash_spans_completed_segments():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\n")
    acc.begin_segment("b.jsonl", boundary=("a.jsonl", 4))
    acc.extend(b"two\nthree\n")
    assert acc.digest_at(("b.jsonl", 4)) == _reference_digest(
        [("a.jsonl", b"one\n"), ("b.jsonl", b"two\n")])


def test_prefix_hash_serves_the_previous_segments_registered_boundary():
    """A resolution op that is the FIRST line of a segment names the previous
    segment's last line end, which is gone by the time the op is decoded."""
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\ntorn-tail-without-newline")
    acc.begin_segment("b.jsonl", boundary=("a.jsonl", 4))
    acc.extend(b"two\n")
    assert acc.digest_at(("a.jsonl", 4)) == _reference_digest(
        [("a.jsonl", b"one\n")])


def test_prefix_hash_frames_an_empty_segment():
    """`journal_prefix_hash` frames a zero-byte segment; `iter_range` skips it,
    so the accumulator must still be told the segment exists."""
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.begin_segment("b.jsonl", boundary=None)
    acc.extend(b"one\n")
    assert acc.digest_at(("b.jsonl", 4)) == _reference_digest(
        [("a.jsonl", b""), ("b.jsonl", b"one\n")])


def test_prefix_hash_none_high_water_is_none():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    assert acc.digest_at(None) is None


def test_prefix_hash_refuses_an_unstreamed_prefix():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\n")
    acc.begin_segment("b.jsonl", boundary=("a.jsonl", 4))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 2))


def test_prefix_hash_counts_only_the_bytes_it_hashed():
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl")
    acc.extend(b"one\ntwo\n")
    assert acc.bytes_hashed == 0
    assert acc.digests_computed == 0
    acc.digest_at(("a.jsonl", 4))
    assert acc.bytes_hashed == 4
    assert acc.digests_computed == 1
