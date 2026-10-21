"""#825/#832 spec 4.1: the immutable account binding (`_lib_account_binding`).

A binding is captured once from the stable-read identity resolver and then
travels with a candidate, a plan or an OAuth publication. These tests pin the
capture rules (identified / stably-absent / torn), the strict serialization and
the credential evidence, which is a salted digest and never the credential.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_account_binding as binding_lib  # noqa: E402
import _lib_accounts  # noqa: E402


def _identified(natural="uuid-A", email="a@example.test", plan="max"):
    return {
        "status": "identified",
        "account_key": _lib_accounts.account_key("claude", natural),
        "natural_id": natural,
        "email": email,
        "plan_type": plan,
    }


def _absent():
    return {"status": "stably_absent", "account_key": "unattributed",
            "natural_id": None, "email": None, "plan_type": None}


def test_identified_binds_the_real_account_key():
    binding = binding_lib.capture(_identified())
    assert binding is not None
    assert binding.status == "identified"
    assert binding.account_key == _lib_accounts.account_key("claude", "uuid-A")
    assert binding.natural_id == "uuid-A"
    assert binding.credential_evidence is None


def test_identified_key_must_match_the_natural_id():
    forged = dict(_identified(), account_key=_lib_accounts.account_key("claude", "uuid-B"))
    assert binding_lib.capture(forged) is None


def test_stably_absent_binds_unattributed_deliberately():
    binding = binding_lib.capture(_absent())
    assert binding is not None
    assert binding.status == "stably_absent"
    assert binding.account_key == _lib_accounts.UNATTRIBUTED


@pytest.mark.parametrize("identity", [
    {"status": "torn", "account_key": "unattributed", "natural_id": None,
     "email": None, "plan_type": None},
    {"status": "unreadable", "account_key": "unattributed"},
    {"status": "stably_absent", "account_key": "unattributed", "natural_id": "x"},
    {"status": "identified", "account_key": "unattributed", "natural_id": None},
    None,
    "identified",
])
def test_torn_or_inconsistent_identity_yields_no_binding(identity):
    assert binding_lib.capture(identity) is None


def test_document_round_trips_with_the_exact_key_set():
    binding = binding_lib.capture(_identified(), credential="secret-token")
    document = binding.document()
    assert set(document) == {
        "status", "accountKey", "identityEvidence", "credentialEvidence",
        "naturalId", "email", "planType",
    }
    assert binding_lib.load(json.loads(json.dumps(document))) == binding


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("email"),
    lambda d: d.update(identityEvidence="0" * 64),
    lambda d: d.update(email="other@example.test"),
    lambda d: d.update(accountKey="unattributed"),
    lambda d: d.update(credentialEvidence="not-a-digest"),
    lambda d: d.update(credentialEvidence=7),
])
def test_load_rejects_any_mismatch(mutate):
    document = binding_lib.capture(_identified(), credential="secret-token").document()
    mutate(document)
    with pytest.raises(ValueError):
        binding_lib.load(document)


@pytest.mark.parametrize("document", [None, [], "binding"])
def test_load_rejects_non_objects(document):
    with pytest.raises(ValueError):
        binding_lib.load(document)


def test_credential_evidence_is_a_salted_digest_never_the_credential():
    binding = binding_lib.capture(_identified(), credential="secret-token")
    expected = hashlib.sha256(
        b"cctally-selected-credential-v1\0secret-token").hexdigest()
    assert binding.credential_evidence == expected
    assert "secret-token" not in json.dumps(binding.document())


def test_identity_returns_the_resolver_shape():
    binding = binding_lib.capture(_identified())
    assert binding.identity() == {
        "status": "identified",
        "account_key": _lib_accounts.account_key("claude", "uuid-A"),
        "natural_id": "uuid-A",
        "email": "a@example.test",
        "plan_type": "max",
    }


def test_same_identity_with_different_credentials_differs_only_in_credential():
    a = binding_lib.capture(_identified(), credential="token-1")
    b = binding_lib.capture(_identified(), credential="token-2")
    assert a.identity_evidence == b.identity_evidence
    assert a != b
    assert a.without_credential() == b.without_credential()
