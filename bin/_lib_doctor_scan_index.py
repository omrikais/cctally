"""The deep doctor journal scan's checkpoint — the pure kernel (#1054).

`cctally doctor` decodes every journal line on every deep run to compute its
journal legs: the malformed and torn counts, the same-revision conflicts, the
structural protocol violations and acknowledgements, the cutover value and the
excluded population. On a production-sized journal that is about 2.6 million
decodes, of which roughly 5 % are records the scan keeps; the rest are
observations that only consume a selector position.

Doctor therefore keeps one disposable file beside the segments,
``JOURNAL_DIR/.doctor-scan-index``. Per segment it records a SHA-256 of the
complete-line prefix it scanned, the number of decoded records and malformed
lines in that prefix, and the line offset and decoded ordinal of every record
that matters to the scan (a "hit"). A later run proves the prefix against the
current bytes, decodes only the hits, fills every other position with the
``None`` placeholder the selector expects, and decodes the fresh suffix. The
excluded population is cached the same way, under its own digest of the range
it classified and an explicit fingerprint of the admission boundary.

**Nothing here is trusted without a content proof.** There is no stat-identity
shortcut: every reuse hashes the bytes it skips. A document whose version or
checksum does not match is ignored as a whole, and an entry that fails the
structural rules below is dropped while the others are kept. Torn status is
never stored, because a torn tail can be repaired and re-torn at the same size.

**The version is derived, not stamped.** `index_version` hashes `SHAPE_VERSION`
with the descriptors the glue supplies (the retained record types, the special
ids, the protocol-resolution kinds, every constant the admission predicate
reads and the stats epoch), so a change to any of those invalidates every
checkpoint. `SHAPE_VERSION` is bumped by hand for any change those inputs do
not capture: the meaning of a stored field, the decoder, or the scan's
per-record steps.

This module performs no I/O and imports nothing from the journal modules, the
same rule `bin/_lib_segment_summary.py` and `bin/_lib_journal_router.py` follow,
so every rule here is unit-testable without a journal on disk. The glue — the
read, the per-record step, the exclusion read and the atomic publication —
lives in `bin/_cctally_doctor.py`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Iterable

#: Bumped by hand whenever a stored field's meaning, the decoder semantics or
#: the scan's per-record steps change in a way the derived descriptors do not
#: capture (see `docs/journal-gotchas.md`).
SHAPE_VERSION = 1

_HEX_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_ENTRY_KEYS = frozenset(
    ("name", "covered", "sha256", "decoded", "malformed", "hits", "exclusion"))
_EXCLUSION_KEYS = frozenset(("boundary", "lo", "covered", "sha256", "excluded"))


@dataclasses.dataclass(frozen=True)
class Exclusion:
    """One segment's cached excluded population over ``[lo, covered)``.

    ``boundary`` is the `boundary_fingerprint` it was computed under and
    ``sha256`` the hex digest of exactly those bytes; ``excluded`` lists each
    excluded record's line-start offset and admission family, in offset
    order."""

    boundary: str
    lo: int
    covered: int
    sha256: str
    excluded: tuple


@dataclasses.dataclass(frozen=True)
class Entry:
    """One segment's main-scan checkpoint over ``[0, covered)``.

    ``covered`` is a complete-line end or 0; ``sha256`` the hex digest of
    those bytes; ``decoded`` and ``malformed`` the decoded records and the
    malformed non-blank complete lines in that prefix; ``hits`` the
    ``(line-start offset, 0-based decoded ordinal)`` of every record that
    matters to the scan, in offset order."""

    name: str
    covered: int
    sha256: str
    decoded: int
    malformed: int
    hits: tuple
    exclusion: "Exclusion | None"


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(obj) -> str:
    return "sha256:" + hashlib.sha256(_canonical(obj)).hexdigest()


def index_version(descriptors: dict) -> str:
    """The checkpoint version: `SHAPE_VERSION` plus every derived input."""
    return _digest({"shape": SHAPE_VERSION, "descriptors": descriptors})


def boundary_fingerprint(fields: dict) -> str:
    """The digest of the admission-boundary fields the glue serializes."""
    return _digest(fields)


def _exclusion_document(exclusion: "Exclusion | None"):
    if exclusion is None:
        return None
    return {"boundary": exclusion.boundary, "lo": exclusion.lo,
            "covered": exclusion.covered, "sha256": exclusion.sha256,
            "excluded": [[offset, family]
                         for offset, family in exclusion.excluded]}


def _entry_document(entry: Entry) -> dict:
    return {"name": entry.name, "covered": entry.covered,
            "sha256": entry.sha256, "decoded": entry.decoded,
            "malformed": entry.malformed,
            "hits": [[offset, ordinal] for offset, ordinal in entry.hits],
            "exclusion": _exclusion_document(entry.exclusion)}


def encode(version: str, entries: Iterable[Entry]) -> bytes:
    """The canonical checkpoint document: segments in the given order."""
    segments = [_entry_document(entry) for entry in entries]
    return _canonical({"version": version, "checksum": _digest(segments),
                       "segments": segments})


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _hex(value) -> bool:
    return isinstance(value, str) and _HEX_DIGEST.match(value) is not None


def _pairs(value, second_ok, lo: int, hi: int, second_hi=None):
    """``value`` as a tuple of ``(offset, second)`` pairs with offsets
    strictly increasing in ``[lo, hi)`` — and, when ``second_hi`` is given,
    integer seconds strictly increasing in ``[0, second_hi)`` — else None."""
    if not isinstance(value, list):
        return None
    out = []
    last_offset = lo - 1
    last_second = -1
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            return None
        offset, second = item
        if not _count(offset) or not (last_offset < offset < hi):
            return None
        if not second_ok(second):
            return None
        if second_hi is not None:
            if not (last_second < second < second_hi):
                return None
            last_second = second
        last_offset = offset
        out.append((offset, second))
    return tuple(out)


def _exclusion_from_document(doc):
    """``(valid, Exclusion | None)``."""
    if doc is None:
        return True, None
    if not isinstance(doc, dict) or set(doc) != _EXCLUSION_KEYS:
        return False, None
    boundary, lo, covered = doc["boundary"], doc["lo"], doc["covered"]
    if not isinstance(boundary, str) or not boundary:
        return False, None
    if not (_count(lo) and _count(covered) and lo <= covered):
        return False, None
    if not _hex(doc["sha256"]):
        return False, None
    excluded = _pairs(doc["excluded"], lambda family: isinstance(family, str),
                      lo, covered)
    if excluded is None:
        return False, None
    return True, Exclusion(boundary, lo, covered, doc["sha256"], excluded)


def _entry_from_document(doc) -> "Entry | None":
    if not isinstance(doc, dict) or set(doc) != _ENTRY_KEYS:
        return None
    name, covered = doc["name"], doc["covered"]
    decoded, malformed = doc["decoded"], doc["malformed"]
    if not isinstance(name, str) or not name:
        return None
    if not (_count(covered) and _count(decoded) and _count(malformed)):
        return None
    # Each counted line occupies at least one byte of `[0, covered)`: its
    # newline.
    if malformed + decoded > covered:
        return None
    if not _hex(doc["sha256"]):
        return None
    hits = _pairs(doc["hits"], _count, 0, covered, second_hi=decoded)
    if hits is None:
        return None
    valid, exclusion = _exclusion_from_document(doc["exclusion"])
    if not valid:
        return None
    return Entry(name, covered, doc["sha256"], decoded, malformed, hits,
                 exclusion)


def decode(raw: bytes, version: str) -> "dict[str, Entry] | None":
    """The valid entries of a checkpoint document, keyed by segment name.

    None when the document does not parse, names another version or fails
    its checksum. A structurally invalid entry is dropped and the others are
    kept; a segment named twice keeps neither entry."""
    return decode_document(raw, version)[0]


def decode_document(raw: bytes, version: str):
    """``(decode(raw, version), clean)``: ``clean`` is True only when the
    document is valid and every one of its entries was kept, so a writer
    holding the same entries has nothing to rewrite."""
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None, False
    if not isinstance(doc, dict) or doc.get("version") != version:
        return None, False
    segments = doc.get("segments")
    if not isinstance(segments, list):
        return None, False
    try:
        if doc.get("checksum") != _digest(segments):
            return None, False
    except (ValueError, TypeError, RecursionError):
        return None, False
    out: dict = {}
    duplicated = set()
    for item in segments:
        entry = _entry_from_document(item)
        if entry is None:
            continue
        if entry.name in out:
            duplicated.add(entry.name)
        out[entry.name] = entry
    for name in duplicated:
        del out[name]
    return out, len(out) == len(segments)


def verify_prefix(entry_sha: str, covered: int, data: bytes):
    """The running SHA-256 after ``data[:covered]`` when that prefix is a
    complete-line prefix of ``data`` whose digest is ``entry_sha``, else None.

    The caller continues the returned object over the fresh suffix, so every
    byte is hashed once."""
    if len(data) < covered:
        return None
    if covered and data[covered - 1] != 0x0A:
        return None
    hasher = hashlib.sha256()
    hasher.update(memoryview(data)[:covered])
    if hasher.hexdigest() != entry_sha:
        return None
    return hasher


class RangeHasher:
    """An ``on_bytes`` callback hashing every chunk of one ``[lo, hi)`` read.

    With ``covered`` set it also proves the cached prefix ``[lo, covered)``:
    the chunk that crosses ``covered`` is split, the prefix digest compared
    with ``expected_prefix``, and ``prefix_state`` settled before the call
    returns — so before the reader yields any line starting at or after
    ``covered``. With ``covered == lo`` the empty prefix is compared at
    construction. ``prefix_state`` is ``"none"`` without ``covered``,
    otherwise ``"pending"`` until settled as ``"match"`` or ``"mismatch"``;
    a read that ends while it is still pending never reached ``covered``."""

    def __init__(self, lo: int, covered: "int | None",
                 expected_prefix: "str | None"):
        if covered is not None and covered < lo:
            raise ValueError("covered precedes lo")
        self._lo = lo
        self._covered = covered
        self._expected = expected_prefix
        self._hash = hashlib.sha256()
        self.consumed = 0
        if covered is None:
            self.prefix_state = "none"
        elif covered == lo:
            self.prefix_state = self._settle()
        else:
            self.prefix_state = "pending"

    def _settle(self) -> str:
        return ("match" if self._hash.hexdigest() == self._expected
                else "mismatch")

    def __call__(self, chunk: bytes) -> None:
        if self.prefix_state == "pending":
            need = self._covered - self._lo - self.consumed
            if len(chunk) >= need:
                view = memoryview(chunk)
                self._hash.update(view[:need])
                self.prefix_state = self._settle()
                self._hash.update(view[need:])
                self.consumed += len(chunk)
                return
        self._hash.update(chunk)
        self.consumed += len(chunk)

    def digest(self) -> str:
        """The hex digest of every byte consumed so far."""
        return self._hash.hexdigest()
