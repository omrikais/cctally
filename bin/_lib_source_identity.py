"""Pure, opaque provider-qualified identity helpers for #294 S0/S1."""
from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re


IDENTITY_VERSION = 1
_SOURCES = frozenset(("claude", "codex"))
_SOURCE_ROOT_KEY_RE = re.compile(r"[0-9a-f]{32}\Z")
_IDENTITY_PATH_ALIAS = contextvars.ContextVar(
    "cctally_identity_path_alias", default=None,
)


@contextlib.contextmanager
def identity_path_alias(physical_root: object, logical_root: object):
    """Temporarily map one physical fixture root to a stable logical root.

    Production callers never enter this context.  The generated benchmark
    corpus does so when a byte-stable cross-platform oracle needs provider and
    file identities that do not change merely because macOS resolves ``/tmp``
    through ``/private/tmp``.  The mapping is boundary-aware and scoped through
    a ContextVar so nested callers restore the previous identity exactly.
    """
    physical = os.path.normpath(os.fspath(physical_root))
    logical = os.path.normpath(os.fspath(logical_root))
    if not os.path.isabs(physical) or not os.path.isabs(logical):
        raise ValueError("identity path aliases must use absolute roots")
    token = _IDENTITY_PATH_ALIAS.set((physical, logical))
    try:
        yield
    finally:
        _IDENTITY_PATH_ALIAS.reset(token)


def identity_path(value: str) -> str:
    """Return the active logical spelling for an identity-bearing path.

    This is deliberately a no-op outside ``identity_path_alias``.  Oracle
    renderers use it at the last path-bearing identity seam; ordinary runtime
    callers therefore retain their existing physical-path identity contract.
    """
    alias = _IDENTITY_PATH_ALIAS.get()
    if alias is None:
        return value
    physical, logical = alias
    normalized = os.path.normpath(value)
    if normalized == physical:
        return logical
    prefix = physical + os.sep
    if normalized.startswith(prefix):
        return logical + normalized[len(physical):]
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def source_root_key(canonical_root: str) -> str:
    """Return the non-reversible, domain-separated key for one source root."""
    root = identity_path(_required_string(canonical_root, "canonical_root"))
    digest = hashlib.sha256(b"cctally-source-root-v1\0" + root.encode("utf-8"))
    return digest.hexdigest()[:32]


def codex_file_key(root_key: str, canonical_physical_path: str) -> str:
    """Return the durable identity of one Codex rollout file (#416 spec §3.2).

    Keyed on ``(source_root_key, canonical physical path)`` — deliberately NOT
    on the configured walk spelling, because Codex discovery deduplicates on the
    canonical physical path but persists the FIRST configured candidate
    spelling, so reordering ``$CODEX_HOME`` roots or respelling a symlink makes
    the same physical file miss a path-keyed map.

    Scoping by ``root_key`` is what makes a root requalification safe for free:
    the same physical file reached under a different provider root is a
    different identity, so it carries no prior attribution decision and takes a
    fresh one. Non-reversible and fixed-width like ``source_root_key``, so the
    durable key never embeds an operator path.
    """
    root = _required_string(root_key, "root_key")
    path = identity_path(
        _required_string(canonical_physical_path, "canonical_physical_path"))
    digest = hashlib.sha256(
        b"cctally-codex-file-v1\0" + root.encode("utf-8")
        + b"\0" + path.encode("utf-8")
    )
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
