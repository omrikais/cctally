"""Pure legacy statusline spool baseline (#825/#832, spec sections 5.5 and 7).

After the account-bound cutover the previous release's statusline spool
(``statusline-candidates/``, ``statusline-selected.json``, the authoritative
tombstones and the freshness markers) is never read as authority. It is only
compared with a baseline captured inside the locked cutover establishment and
anchored in the journal: the cutover op records the baseline digest, and every
later ``legacy_spool_activity`` op records the digest the baseline advances to.
A change against the baseline is evidence that a legacy process wrote after the
cutover, which quarantines automatic outcomes (spec 5.6 item 1(b)).

This module owns the baseline's value shape, its digest, its on-disk document
codec and the comparison. It is pure: the filesystem capture lives in
``_cctally_journal.capture_legacy_spool_state``. File content is digested,
never interpreted.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re

DOCUMENT_SCHEMA_VERSION = 1

_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False).encode("utf-8")


def content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclasses.dataclass(frozen=True)
class LegacySpoolState:
    """The observed legacy spool: one ``(relative path, size, mtime_ns,
    sha256 of content)`` entry per regular file, sorted by path."""

    entries: tuple[tuple[str, int, int, str], ...]

    def digest(self) -> str:
        return content_digest(_canonical([list(e) for e in self.entries]))


def make_state(entries) -> LegacySpoolState:
    """Normalize ``entries`` into a sorted, validated state."""
    normalized = []
    seen = set()
    for entry in entries:
        path, size, mtime_ns, sha = entry
        if not isinstance(path, str) or not path or path in seen:
            raise ValueError("legacy spool entry path is invalid")
        if type(size) is not int or size < 0:
            raise ValueError("legacy spool entry size is invalid")
        if type(mtime_ns) is not int:
            raise ValueError("legacy spool entry mtime is invalid")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise ValueError("legacy spool entry digest is invalid")
        seen.add(path)
        normalized.append((path, size, mtime_ns, sha))
    return LegacySpoolState(entries=tuple(sorted(normalized)))


def diff(baseline: LegacySpoolState,
         current: LegacySpoolState) -> tuple[str, ...]:
    """The relative paths that were added, removed or changed (any of size,
    modification time or content), sorted."""
    before = {e[0]: e[1:] for e in baseline.entries}
    after = {e[0]: e[1:] for e in current.entries}
    return tuple(sorted(
        path for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    ))


def change_evidence(changed, current: LegacySpoolState) -> list[dict]:
    """Per changed path, the observed after-state (or its absence). Digests
    and metadata only, never a legacy value."""
    after = {e[0]: e for e in current.entries}
    out = []
    for path in sorted(changed):
        entry = after.get(path)
        if entry is None:
            out.append({"path": path, "absent": True})
        else:
            out.append({"path": path, "size": entry[1], "mtimeNs": entry[2],
                        "sha256": entry[3]})
    return out


def to_document(state: LegacySpoolState) -> dict:
    return {
        "schemaVersion": DOCUMENT_SCHEMA_VERSION,
        "digest": state.digest(),
        "entries": [
            {"path": p, "size": s, "mtimeNs": m, "sha256": h}
            for (p, s, m, h) in state.entries
        ],
    }


def from_document(doc) -> LegacySpoolState:
    """Decode a baseline document. Raises ``ValueError`` on any shape failure
    or when the recorded digest does not match the entries."""
    if not isinstance(doc, dict) or doc.get("schemaVersion") != (
            DOCUMENT_SCHEMA_VERSION):
        raise ValueError("legacy spool baseline document shape is invalid")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise ValueError("legacy spool baseline entries are invalid")
    try:
        state = make_state(
            (e["path"], e["size"], e["mtimeNs"], e["sha256"])
            for e in entries if isinstance(e, dict)
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("legacy spool baseline entry is invalid") from exc
    if len(state.entries) != len(entries):
        raise ValueError("legacy spool baseline entry is invalid")
    if doc.get("digest") != state.digest():
        raise ValueError("legacy spool baseline digest does not match")
    return state
