"""Pure account-identity kernel for the multi-account epic (#341).

Mirrors ``bin/_lib_source_identity.py``: an opaque, non-reversible per-account
key plus the small set of pure helpers that derive an account identity from the
provider's own on-disk credential state. I/O-free by construction beyond the
stat-read-stat protocol (stdlib ``os`` only) and read-only SQLite lookups for
ref resolution — no ``_cctally_*`` imports, no locks, no writes.

Identity model (design spec §1):
  - ``account_key = sha256("cctally-account-v1\\0" + provider + "\\0" + natural_id)[:32]``
    (opaque, domain-separated, mirroring ``source_root_key``).
  - Natural ids: Claude -> ``oauthAccount.accountUuid``; Codex ->
    ``chatgpt_account_id + "\\0" + email`` (the pair, since neither is unique
    alone), decoded from the ``~/.codex/auth.json`` id_token JWT payload (plain
    base64 body decode; NO signature verification — we read our own disk state,
    not authenticate).
  - Reserved sentinel ``UNATTRIBUTED = "unattributed"`` means "account could not
    be determined" (pre-feature history and any stably-unreadable ingest).
  - ``VENDOR_WIDE = "*"`` is the vendor-wide budget sentinel (used by later
    tasks; defined here as the single home for the constant).

Stable-read protocol (review finding 7): identity files are rewritten in place
by other programs, so every read is stat-read-stat on ``(st_ino, st_size,
st_mtime_ns)`` with bounded retries. Three-valued outcome: *identified* (stamp
the account), *stably_absent* (missing file / api-key mode -> stamp
``unattributed``), or *torn* (stats never stabilised, or content unparseable
mid-write -> defer, never stamp a guess).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, TypeVar


ACCOUNT_KEY_VERSION = 1
UNATTRIBUTED = "unattributed"
VENDOR_WIDE = "*"

# The namespaced OpenAI OIDC claim carrying the ChatGPT account identity.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"

T = TypeVar("T")


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


# --------------------------------------------------------------------------
# key derivation
# --------------------------------------------------------------------------

def account_key(provider: str, natural_id: str) -> str:
    """Opaque, non-reversible 32-hex key for one account.

    ``sha256("cctally-account-v1\\0" + provider + "\\0" + natural_id)[:32]``.
    Stable across processes; distinct per provider and per natural id."""
    prov = _required_string(provider, "provider")
    nat = _required_string(natural_id, "natural_id")
    digest = hashlib.sha256(
        b"cctally-account-v1\0" + prov.encode("utf-8") + b"\0" + nat.encode("utf-8")
    )
    return digest.hexdigest()[:32]


# --------------------------------------------------------------------------
# natural-id extraction (pure; operate on already-decoded dicts)
# --------------------------------------------------------------------------

def claude_natural_id(oauth_account: object) -> Optional[str]:
    """Claude natural id = ``oauthAccount.accountUuid`` (or None if absent)."""
    if not isinstance(oauth_account, dict):
        return None
    uuid = oauth_account.get("accountUuid")
    if isinstance(uuid, str) and uuid:
        return uuid
    return None


def claude_email(oauth_account: object) -> Optional[str]:
    """Best-effort Claude email (``oauthAccount.emailAddress``) for display."""
    if not isinstance(oauth_account, dict):
        return None
    email = oauth_account.get("emailAddress")
    if isinstance(email, str) and email:
        return email
    return None


def _codex_auth_claim(payload: dict) -> dict:
    """Return the OpenAI auth claim object, tolerating the flat dotted form."""
    claim = payload.get(_OPENAI_AUTH_CLAIM)
    if isinstance(claim, dict):
        return claim
    return {}


def codex_natural_id(id_token_payload: object) -> Optional[str]:
    """Codex natural id = ``chatgpt_account_id + "\\0" + email`` (the pair).

    Returns None when either component is missing — e.g. ``OPENAI_API_KEY``
    mode, which has no ChatGPT identity at all."""
    if not isinstance(id_token_payload, dict):
        return None
    email = id_token_payload.get("email")
    claim = _codex_auth_claim(id_token_payload)
    account_id = claim.get("chatgpt_account_id")
    # Flat fallback: a token that hoisted the id to the top level.
    if not isinstance(account_id, str):
        account_id = id_token_payload.get("chatgpt_account_id")
    if not isinstance(email, str) or not email:
        return None
    if not isinstance(account_id, str) or not account_id:
        return None
    return account_id + "\0" + email


def codex_email(id_token_payload: object) -> Optional[str]:
    if not isinstance(id_token_payload, dict):
        return None
    email = id_token_payload.get("email")
    return email if isinstance(email, str) and email else None


def codex_plan_type(id_token_payload: object) -> Optional[str]:
    """Best-effort ChatGPT plan type for registry enrichment."""
    if not isinstance(id_token_payload, dict):
        return None
    claim = _codex_auth_claim(id_token_payload)
    plan = claim.get("chatgpt_plan_type")
    if not isinstance(plan, str):
        plan = id_token_payload.get("chatgpt_plan_type")
    return plan if isinstance(plan, str) and plan else None


# --------------------------------------------------------------------------
# JWT id_token decode (base64 body, NO signature verification)
# --------------------------------------------------------------------------

def decode_id_token_payload(id_token: object) -> Optional[dict]:
    """Decode the payload segment of a JWT ``id_token``.

    Returns the payload dict, or None on ANY failure. We are reading our own
    disk state, not authenticating, so the signature is neither present-checked
    nor verified — only the base64url body is decoded and JSON-parsed."""
    if not isinstance(id_token, str) or not id_token:
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    body = parts[1]
    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError):
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------
# stat-read-stat stable-read protocol
# --------------------------------------------------------------------------

class TornRead(Exception):
    """Raised by a stable-read reader when the bytes are unparseable mid-write.

    The stable-read machinery treats it as a *torn* outcome (defer), distinct
    from a reader that returns None (parsed cleanly, but carries no identity ->
    stably_absent)."""


@dataclass
class StableRead:
    """Outcome of :func:`stable_read_identity`.

    ``status`` is one of ``"identified"`` / ``"stably_absent"`` / ``"torn"``;
    ``value`` is the reader's result only when ``identified``."""

    status: str
    value: object = None


def _stat_signature(st) -> tuple:
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def stable_read_identity(
    path: str,
    reader: Callable[[bytes], T],
    *,
    max_retries: int = 3,
    _stat=None,
    _open=None,
) -> StableRead:
    """stat -> read -> stat an identity file, retrying on a changed signature.

    Outcomes:
      * ``stably_absent`` — the file is missing (any attempt), or ``reader``
        returns None over a stat-stable read (content present, no identity).
      * ``torn`` — the ``(inode, size, mtime_ns)`` signature never stabilised
        across ``max_retries`` attempts, or ``reader`` raised :class:`TornRead`.
      * ``identified`` — a stat-stable read whose ``reader`` returned a value.

    The private ``_stat``/``_open`` hooks exist only for deterministic torn-read
    testing; production always uses ``os.stat`` / ``open``."""
    stat = _stat or os.stat
    opener = _open or open
    for _ in range(max_retries + 1):
        try:
            st1 = stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return StableRead("stably_absent")
        except OSError:
            continue
        sig1 = _stat_signature(st1)
        try:
            with opener(path, "rb") as fh:
                data = fh.read()
        except (FileNotFoundError, NotADirectoryError):
            return StableRead("stably_absent")
        except OSError:
            continue
        try:
            st2 = stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return StableRead("stably_absent")
        except OSError:
            continue
        if sig1 != _stat_signature(st2):
            continue  # file changed during the read window -> retry
        try:
            value = reader(data)
        except TornRead:
            return StableRead("torn")
        if value is None:
            return StableRead("stably_absent")
        return StableRead("identified", value)
    return StableRead("torn")


# --------------------------------------------------------------------------
# account-ref resolution (read-only over the accounts registry)
# --------------------------------------------------------------------------

class AccountRefError(Exception):
    """Raised when an account ref is ambiguous or unknown.

    ``candidates`` is the list of ``account_key``s that the caller can print on
    stderr (the tied matches for an ambiguous ref; every known account for an
    unknown ref)."""

    def __init__(self, ref: str, candidates):
        self.ref = ref
        self.candidates = list(candidates)
        super().__init__(f"account ref {ref!r} is ambiguous or unknown")


@dataclass
class _AccountRow:
    account_key: str
    provider: str
    label: Optional[str] = None
    email: Optional[str] = None


def _load_account_rows(conn, provider: Optional[str]):
    sql = "SELECT account_key, provider, label, email FROM accounts"
    params: tuple = ()
    if provider is not None:
        sql += " WHERE provider = ?"
        params = (provider,)
    rows = []
    for account_key_, prov, label, email in conn.execute(sql, params).fetchall():
        rows.append(_AccountRow(account_key_, prov, label, email))
    return rows


def resolve_account_ref(conn, ref: str, provider: Optional[str] = None) -> str:
    """Resolve a user-supplied account ref to an ``account_key``.

    Precedence: case-insensitive label -> case-insensitive email -> unique
    ``account_key`` prefix. The literal ``"unattributed"`` sentinel is accepted
    directly. Ambiguity within a tier, or no match in any tier, raises
    :class:`AccountRefError` (candidates = the tied matches, or every known
    account when unknown)."""
    if not isinstance(ref, str) or not ref:
        raise AccountRefError(ref, [])
    if ref == UNATTRIBUTED:
        return UNATTRIBUTED
    rows = _load_account_rows(conn, provider)
    needle = ref.strip().lower()

    # Tier 1: exact case-insensitive label.
    label_matches = [r for r in rows if (r.label or "").lower() == needle]
    if len(label_matches) == 1:
        return label_matches[0].account_key
    if len(label_matches) > 1:
        raise AccountRefError(ref, [r.account_key for r in label_matches])

    # Tier 2: exact case-insensitive email.
    email_matches = [r for r in rows if (r.email or "").lower() == needle]
    if len(email_matches) == 1:
        return email_matches[0].account_key
    if len(email_matches) > 1:
        raise AccountRefError(ref, [r.account_key for r in email_matches])

    # Tier 3: account_key prefix (case-sensitive hex).
    prefix_matches = [r for r in rows if r.account_key.startswith(ref)]
    if len(prefix_matches) == 1:
        return prefix_matches[0].account_key
    if len(prefix_matches) > 1:
        raise AccountRefError(ref, [r.account_key for r in prefix_matches])

    raise AccountRefError(ref, [r.account_key for r in rows])
