"""Immutable account binding carried by every current statusline/OAuth producer.

#825/#832 spec 4.1. A binding is captured ONCE, from the stable-read identity
resolver (`_cctally_core._resolve_active_claude_identity`), and then travels
with the candidate, the publication plan or the OAuth publication it belongs
to. No later stage re-resolves the active account: an A->B switch between
capture and publication can therefore never re-label A's reading as B's.

- ``identified`` binds the real account key, which must equal
  ``_lib_accounts.account_key("claude", natural_id)``;
- ``stably_absent`` (no ``~/.claude.json`` account, api-key mode) binds the
  ``unattributed`` sentinel deliberately;
- ``torn`` and every unreadable or internally inconsistent read yield NO
  binding. The caller then writes no candidate, mutates no control state and
  consumes no retry attempt.

The credential evidence is a salted digest of the selected OAuth credential,
never the credential itself. Serialization is strict: an exact camelCase key
set, and ``load`` re-derives the identity evidence and compares it.

Pure: no filesystem, clock or ``cctally`` namespace dependency.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re

import _lib_accounts

STATUS_IDENTIFIED = "identified"
STATUS_STABLY_ABSENT = "stably_absent"

_CREDENTIAL_SALT = "cctally-selected-credential-v1\0"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_DOCUMENT_KEYS = frozenset({
    "status", "accountKey", "identityEvidence", "credentialEvidence",
    "naturalId", "email", "planType",
})


@dataclasses.dataclass(frozen=True)
class AccountBinding:
    status: str
    account_key: str
    identity_evidence: str
    credential_evidence: str | None = None
    natural_id: str | None = None
    email: str | None = None
    plan_type: str | None = None

    def document(self) -> dict:
        """The exact persisted shape (camelCase, every key always present)."""
        return {
            "status": self.status,
            "accountKey": self.account_key,
            "identityEvidence": self.identity_evidence,
            "credentialEvidence": self.credential_evidence,
            "naturalId": self.natural_id,
            "email": self.email,
            "planType": self.plan_type,
        }

    def identity(self) -> dict:
        """The resolver's own shape, for consumers that take an identity."""
        return {
            "status": self.status,
            "account_key": self.account_key,
            "natural_id": self.natural_id,
            "email": self.email,
            "plan_type": self.plan_type,
        }

    def without_credential(self) -> "AccountBinding":
        """The same identity with no credential evidence (identity equality)."""
        return dataclasses.replace(self, credential_evidence=None)


def credential_digest(credential: str) -> str:
    """The salted credential evidence; the credential itself is never kept."""
    return hashlib.sha256(
        (_CREDENTIAL_SALT + credential).encode("utf-8")).hexdigest()


def _identity_evidence(fields: dict) -> str:
    return hashlib.sha256(json.dumps(
        fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def capture(identity, *, credential: str | None = None) -> AccountBinding | None:
    """Bind one resolver result, or return None for a torn, unreadable or
    internally inconsistent read. Never guesses an account."""
    if not isinstance(identity, dict):
        return None
    status = identity.get("status")
    key = identity.get("account_key")
    natural = identity.get("natural_id")
    if status == STATUS_IDENTIFIED:
        if not isinstance(natural, str) or not natural:
            return None
        if key != _lib_accounts.account_key("claude", natural):
            return None
    elif status == STATUS_STABLY_ABSENT:
        if key != _lib_accounts.UNATTRIBUTED or natural is not None:
            return None
    else:
        return None
    fields = {
        "status": status,
        "account_key": key,
        "natural_id": natural,
        "email": identity.get("email"),
        "plan_type": identity.get("plan_type"),
    }
    if any(value is not None and not isinstance(value, str)
           for value in fields.values()):
        return None
    if credential is not None and (not isinstance(credential, str)
                                   or not credential):
        return None
    return AccountBinding(
        status=status,
        account_key=key,
        identity_evidence=_identity_evidence(fields),
        credential_evidence=(
            None if credential is None else credential_digest(credential)),
        natural_id=natural,
        email=fields["email"],
        plan_type=fields["plan_type"],
    )


def load(document) -> AccountBinding:
    """Validate a persisted binding document; ValueError on any mismatch."""
    if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
        raise ValueError("invalid account binding document")
    binding = capture({
        "status": document["status"],
        "account_key": document["accountKey"],
        "natural_id": document["naturalId"],
        "email": document["email"],
        "plan_type": document["planType"],
    })
    if binding is None or binding.identity_evidence != document["identityEvidence"]:
        raise ValueError("inconsistent account binding")
    evidence = document["credentialEvidence"]
    if evidence is not None and (not isinstance(evidence, str)
                                 or _DIGEST_RE.fullmatch(evidence) is None):
        raise ValueError("invalid credential evidence")
    return dataclasses.replace(binding, credential_evidence=evidence)
