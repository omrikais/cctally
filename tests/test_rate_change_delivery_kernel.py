"""#695 — the pure notification-delivery ledger kernel.

Every function here is total and side-effect free. The glue in
`bin/_cctally_quota_model.py` owns the file, the lock and the stats read;
this module owns what a document means.
"""
from __future__ import annotations

import pytest

from tests._script_loader import load_script_module


@pytest.fixture()
def d():
    return load_script_module()._load_sibling("_lib_rate_change_delivery")


def test_an_empty_state_is_unseeded_and_holds_no_decisions(d):
    assert d.empty_state() == {
        "schemaVersion": d.SCHEMA_VERSION,
        "seededFromStats": False,
        "decided": [],
    }


def test_a_well_formed_document_validates(d):
    doc = {"schemaVersion": 1, "seededFromStats": True,
           "decided": [["claude", "unattributed", "2026-08-25T00:00:00+00:00"]]}
    assert d.validate(doc) is doc


@pytest.mark.parametrize("doc", [
    None,
    [],
    "{}",
    {"seededFromStats": True, "decided": []},
    {"schemaVersion": "1", "seededFromStats": True, "decided": []},
    {"schemaVersion": 1.0, "seededFromStats": True, "decided": []},
    {"schemaVersion": 1, "seededFromStats": "yes", "decided": []},
    {"schemaVersion": 1, "seededFromStats": True, "decided": {}},
    {"schemaVersion": 1, "seededFromStats": True, "decided": [["a", "b"]]},
    {"schemaVersion": 1, "seededFromStats": True,
     "decided": [["a", "b", "c", "d"]]},
    {"schemaVersion": 1, "seededFromStats": True, "decided": [["a", "b", 3]]},
    {"schemaVersion": 1, "seededFromStats": True, "decided": ["abc"]},
])
def test_a_malformed_document_is_unusable(d, doc):
    assert d.validate(doc) is None


def test_a_true_schema_version_is_not_version_one(d):
    """`isinstance(True, int)` is true, and the calibration loader excludes it
    for the same reason: a boolean must never read as a version number."""
    assert d.validate(
        {"schemaVersion": True, "seededFromStats": True, "decided": []}) is None


def test_a_version_above_this_binary_is_unusable(d):
    assert d.validate({"schemaVersion": d.SCHEMA_VERSION + 1,
                       "seededFromStats": True, "decided": []}) is None


def test_a_version_below_this_binary_is_usable(d):
    doc = {"schemaVersion": 0, "seededFromStats": False, "decided": []}
    assert d.validate(doc) is doc


def test_duplicates_are_tolerated_on_read_and_collapsed_on_write(d):
    ident = ["claude", "unattributed", "2026-08-25T00:00:00+00:00"]
    doc = {"schemaVersion": 1, "seededFromStats": True,
           "decided": [ident, list(ident)]}
    assert d.validate(doc) is doc
    assert d.decided_set(doc) == {tuple(ident)}
    assert d.with_decided(doc, [])["decided"] == [ident]


def test_with_decided_adds_sorts_and_preserves_the_seed_flag(d):
    state = d.with_decided(
        {"schemaVersion": 1, "seededFromStats": True,
         "decided": [["claude", "b", "2026-01-02T00:00:00+00:00"]]},
        [("claude", "a", "2026-01-01T00:00:00+00:00")])
    assert state["seededFromStats"] is True
    assert state["decided"] == [
        ["claude", "a", "2026-01-01T00:00:00+00:00"],
        ["claude", "b", "2026-01-02T00:00:00+00:00"],
    ]


def test_with_decided_can_set_the_seed_flag(d):
    assert d.with_decided(d.empty_state(), [], seeded=True)[
        "seededFromStats"] is True


def test_owed_is_the_recorded_rows_minus_the_decisions(d):
    a = ("claude", "unattributed", "2026-01-01T00:00:00+00:00")
    b = ("claude", "unattributed", "2026-02-01T00:00:00+00:00")
    assert d.owed([b, a], {a}) == (b,)
    assert d.owed([a], {a}) == ()
    assert d.owed([], {a}) == ()


def test_the_identity_keys_on_the_provider_too(d):
    """The full triple, never a suffix of it. Two providers can record the
    same account key at the same instant, and a key that dropped the provider
    would treat one decision as covering both."""
    claude = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    codex = ("codex", "unattributed", "2026-08-25T00:00:00+00:00")
    state = d.with_decided(d.empty_state(), [claude])
    assert d.decided_set(state) == {claude}
    assert d.owed([claude, codex], d.decided_set(state)) == (codex,)


def test_the_four_claim_outcomes_are_distinct(d):
    outcomes = {d.CLAIM_WON, d.CLAIM_ALREADY_DECIDED, d.CLAIM_UNUSABLE,
                d.CLAIM_WRITE_FAILED}
    assert len(outcomes) == 4
