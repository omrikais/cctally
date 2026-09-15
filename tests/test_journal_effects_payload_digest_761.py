"""#761 residual 5 — `effects_payload_digest` covers `refs`, byte-preservingly.

`emit_model_a` assembles the line's payload by copying `columns` and updating it
with `refs` when `refs` is non-empty (`_cctally_journal.py:4476`). The digest
folded into an effects-only evt id read `columns` alone, so two events that
differed only in their refs would digest identically, collide on one id, and the
second would be withheld as a conflict — leaving its effect applied inline and
undone by the next rebuild.

Widening the digest to the effective payload is a straight edit rather than a
versioned change, and this module is what establishes that. The two byte
stability cases below carry digests captured from the implementation BEFORE the
change, so a refactor that alters the hashed bytes for a payload without refs
fails here rather than silently moving every existing identity. The only
production caller passes no refs (`_cctally_record.py:3575`), so no retained
identity moves.
"""
from __future__ import annotations

import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def _lj(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["_lib_journal"]


#: The recovery payload shape `_fire_in_place_credit` emits, and its digest as
#: produced before `refs` was covered.
_RECOVERY_PAYLOAD = {
    "suppression": ["sa:o:abc:syn:0", "sa:o:def:syn:1"],
    "suppression_table": "weekly_usage_snapshots",
    "hwm_floor": {"week_start_date": "2026-09-01", "weekly_percent": 0.0},
}
_RECOVERY_DIGEST = "1e7f4ccddabdcd2f"
_EMPTY_DIGEST = "44136fa355b3678a"


def test_a_payload_without_refs_digests_to_the_bytes_it_always_did(_lj):
    assert _lj.effects_payload_digest(_RECOVERY_PAYLOAD) == _RECOVERY_DIGEST
    assert _lj.effects_payload_digest({}) == _EMPTY_DIGEST


def test_absent_and_empty_refs_are_the_same_input(_lj):
    """`emit_model_a` updates the payload only when `refs` is truthy."""
    assert _lj.effects_payload_digest(_RECOVERY_PAYLOAD, refs=None) == _RECOVERY_DIGEST
    assert _lj.effects_payload_digest(_RECOVERY_PAYLOAD, refs={}) == _RECOVERY_DIGEST


def test_refs_discriminate_two_otherwise_identical_payloads(_lj):
    one = _lj.effects_payload_digest(_RECOVERY_PAYLOAD, refs={"week_ref": "w:1"})
    two = _lj.effects_payload_digest(_RECOVERY_PAYLOAD, refs={"week_ref": "w:2"})
    assert one != two
    assert one != _RECOVERY_DIGEST


def test_the_digest_matches_the_payload_emit_model_a_actually_writes(_lj):
    """The digest must be reproducible from the line, not from a parallel rule.

    `emit_model_a` writes `dict(columns)` updated with non-empty `refs`, so the
    digest of a payload plus refs must equal the digest of that merged mapping
    passed on its own.
    """
    refs = {"week_ref": "w:1"}
    merged = dict(_RECOVERY_PAYLOAD)
    merged.update(refs)
    assert _lj.effects_payload_digest(_RECOVERY_PAYLOAD, refs=refs) == (
        _lj.effects_payload_digest(merged)
    )


def test_an_unconditional_envelope_is_not_what_was_built(_lj):
    """A `{columns, refs}` envelope would change the existing bytes."""
    envelope = {"columns": _RECOVERY_PAYLOAD, "refs": {}}
    assert _lj.effects_payload_digest(envelope) != _RECOVERY_DIGEST
