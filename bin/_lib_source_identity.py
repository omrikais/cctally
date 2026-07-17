"""Pure, opaque provider-qualified identity helpers for #294 S0/S1."""
from __future__ import annotations

import base64
import hashlib
import json
import re


IDENTITY_VERSION = 1
_SOURCES = frozenset(("claude", "codex"))
_SOURCE_ROOT_KEY_RE = re.compile(r"[0-9a-f]{32}\Z")


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def source_root_key(canonical_root: str) -> str:
    """Return the non-reversible, domain-separated key for one source root."""
    root = _required_string(canonical_root, "canonical_root")
    digest = hashlib.sha256(b"cctally-source-root-v1\0" + root.encode("utf-8"))
    return digest.hexdigest()[:32]


def canonical_identity(
    source: str,
    resource_kind: str,
    source_root: str | None,
    native_key: str,
    parent_key: str | None,
) -> str:
    """Encode an opaque IdentityV1 after deriving an optional root key."""
    root_key = None
    if source_root is not None:
        root_key = source_root_key(source_root)
    return canonical_identity_from_root_key(
        source, resource_kind, root_key, native_key, parent_key
    )


def canonical_identity_from_root_key(
    source: str,
    resource_kind: str,
    source_root_key: str | None,
    native_key: str,
    parent_key: str | None,
) -> str:
    """Encode an opaque IdentityV1 from an already-derived source-root key."""
    if source not in _SOURCES:
        raise ValueError(f"source must be one of {sorted(_SOURCES)}")
    kind = _required_string(resource_kind, "resource_kind")
    native = _required_string(native_key, "native_key")
    parent = None if parent_key is None else _required_string(parent_key, "parent_key")
    if source_root_key is not None:
        root_key = _required_string(source_root_key, "source_root_key")
        if not _SOURCE_ROOT_KEY_RE.fullmatch(root_key):
            raise ValueError("source_root_key must be a 32-character lowercase hex key")
    else:
        root_key = None
    payload = {
        "nativeKey": native,
        "parentKey": parent,
        "resourceKind": kind,
        "source": source,
        "sourceRootKey": root_key,
        "version": IDENTITY_VERSION,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "v1." + base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")
