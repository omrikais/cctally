"""Journal segment summaries and the elision predicate — the pure kernel.

#496 S5b Stage 4, spec section 5. A rebuild reads every journal byte on every
pass. Six of the maintainer's seven bootstrap segments hold nothing but Codex
quota observations — roughly 1.64 GB of raw bytes that contribute no distinct
effective event, because Stage 3's coverage certificate already proves the cache
holds them. This module decides when such a segment may be skipped, and records
exactly what the skipping pass must contribute in its place.

**What an elided segment still contributes, and why each piece exists.**

Its **exact count of `decoded` entries**. The rebuild appends one element to
`decoded` for every valid decoded record — the record itself when retained, an
explicit `None` placeholder otherwise. `resolve_effective_events` numbers
candidates with `enumerate(records)`, three of the seven structural violation
kinds put that number inside `ProtocolViolation.evidence`, and the fingerprint
hashes it. That fingerprint is durable: it lands in
`journal_protocol_violations` and is referenced BY NAME from a
`journal_protocol_resolution` op, which `bin/_cctally_journal_repair.py` mints
from the UNFILTERED record list. Contributing the wrong count renumbers every
later candidate and makes a previously acknowledged violation unresolvable.
Malformed lines produce no `decoded` entry and are counted separately, so they
must never be folded into this number.

Its **partial last-seen fold**. `LastSeenAccumulator` is a per-key maximum over
timestamps, and maximum is associative and commutative, so a segment's partial
map merges into the running one in any order. The deferred Claude legacy bucket
still resolves against the cutover account at the end of the pass, and a
quota-only segment cannot contain the cutover op, which is an `op` and therefore
a retained record type.

Its **line, byte, decode and malformed counters**, so the rebuild record reports
the same traversal totals a non-eliding pass reports.

**What it cannot contribute is the prefix hash.** `PrefixHashAccumulator`
absorbs completed segments into one sequential `sha256` and `hashlib` can
neither export nor restore midstate, so a digest over a prefix containing an
elided segment is not computable from the bytes a skipping pass read. Elision is
therefore optimistic: a pass that meets a `journal_protocol_resolution` op
abandons the accumulator and re-reads the prefix from disk for the exact digest.

**Why the extent check is a three-way equality.** `_repair_torn_tail` truncates
a partial trailing line before appending, so a segment's size can decrease or
land back on exactly its previous value while its bytes and its decoded-entry
count both change — "an append strictly increases `st_size`" is false. Storing
only the verified newline boundary collapses the comparison: for a segment of
raw size 120 whose last newline sits at 100, every operand becomes 100 and a
permanently torn segment passes. The summary therefore carries BOTH the raw size
it summarized and the complete-line offset it covered, the pinned vector carries
the raw `st_size`, and elision requires all three to be equal.

This module performs no I/O beyond the sidecar itself and imports nothing from
`_cctally_journal`, so it is unit-testable without a journal on disk — the same
rule `bin/_lib_journal_router.py` and `bin/_lib_cache_coverage.py` follow. The
two modules it does import, `_lib_journal_router` and `_cctally_core`, are
themselves leaf modules; they are imported for the two constants
`summary_version` is derived from, not for behaviour.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field, replace

import _cctally_core
import _lib_journal_router


#: The sidecar basename, beside the journal segments it describes. Journal-side
#: rather than in `stats.db`, because a rebuild frequently runs precisely when
#: `stats.db` is absent or unreadable. It mirrors `.quota-observation-keys` in
#: placement but is a separate artifact: that sidecar stores quota natural-key
#: digests only, with no segment identity, offsets, ordering contribution or
#: selector semantics, so it cannot serve as coverage.
SIDECAR_NAME = ".segment-summaries"

#: Bumped by hand when a summary's FIELD SET or the meaning of one of its
#: numbers changes. It is only one of the three inputs to `summary_version`; the
#: other two are derived, for the reason that function's docstring gives.
SUMMARY_SHAPE_VERSION = 2

#: The refusal reasons `summary_is_elidable` returns. They are stable, because
#: the rebuild record reports them (spec section 6.3, "recorded, not silent").
REASON_OK = "ok"
REASON_RESOLUTION = "resolutionSeen"
REASON_LAST = "lastSegment"
REASON_IDENTITY = "identityMismatch"
REASON_EXTENT = "extentMismatch"
REASON_RETAINED = "retainedRecords"
REASON_UNCOVERED = "notCovered"
REASON_NO_DECODED_COUNT = "decodedCountAbsent"


@dataclass(frozen=True)
class SegmentSummary:
    """Everything a pass needs to skip one segment without reading it.

    Every field has a default so a test can construct the shape it is asserting
    about without restating the rest, and so a later field addition does not
    break a construction site that predates it. The sidecar reader supplies
    every field explicitly, so a defaulted value never reaches a real decision.
    """

    segment_name: str = ""
    st_dev: int = 0
    st_ino: int = 0
    #: The raw `st_size` observed when this summary was written.
    summarized_size: int = 0
    #: The offset after the last COMPLETE line within `summarized_size`.
    complete_line_covered_offset: int = 0
    lines: int = 0
    bytes: int = 0
    decodes: int = 0
    malformed: int = 0
    #: True when the segment holds ZERO records of a retained type.
    quota_only: bool = True
    #: Elements the segment contributed to the rebuild's `decoded` list —
    #: retained records plus placeholders, and never a malformed line. `None`
    #: means the summary predates the field and cannot be elided against.
    decoded_entry_count: "int | None" = 0
    #: The segment's partial `LastSeenAccumulator` state.
    last_seen_stamped: dict = field(default_factory=dict)
    last_seen_legacy_claude_at: "str | None" = None
    last_seen_legacy_codex_at: "str | None" = None


def summary_is_elidable(
    summary, *, pinned_raw_extent, is_last, certificate_covers,
    resolution_seen, stat_identity=None,
) -> "tuple[bool, str]":
    """``(verdict, reason)`` for one segment against spec section 5.5.

    ``stat_identity`` is the ``(st_dev, st_ino)`` of the file on disk. It is
    optional because the caller that has already stat'd the segment passes it
    and a unit test asserting one of the other conditions does not; when it is
    absent the identity condition is the caller's to have checked.

    The order is deliberate, and two parts of it change behaviour rather than
    only wording. `resolution_seen` comes first because it is a property of the
    PASS rather than of the segment, so reporting a segment-shaped reason for it
    would send a reader looking at the wrong file. And the EXTENT check comes
    before the COVERAGE check, because a torn-tail repair moves both: it changes
    the segment's complete-line offset, which is part of the pinned vector, so
    the certificate's identity root moves with it and coverage would refuse the
    segment too. Reporting `notCovered` there would name the certificate for a
    segment the summary's own extent already disqualifies, and a test asserting
    the refusal REASON is what distinguishes the two mechanisms. Reordering
    these two is therefore a behaviour change, not a cleanup.
    """
    if resolution_seen:
        return False, REASON_RESOLUTION
    if is_last:
        return False, REASON_LAST
    if stat_identity is not None:
        if (int(summary.st_dev), int(summary.st_ino)) != (
                int(stat_identity[0]), int(stat_identity[1])):
            return False, REASON_IDENTITY
    # ONE equality over three operands, not two comparisons: see the module
    # docstring for why the raw size and the newline boundary are independent.
    if not (int(summary.complete_line_covered_offset)
            == int(summary.summarized_size)
            == int(pinned_raw_extent)):
        return False, REASON_EXTENT
    if not summary.quota_only:
        return False, REASON_RETAINED
    if not certificate_covers:
        return False, REASON_UNCOVERED
    if summary.decoded_entry_count is None:
        return False, REASON_NO_DECODED_COUNT
    return True, REASON_OK


_FIELDS = (
    "segment_name", "st_dev", "st_ino", "summarized_size",
    "complete_line_covered_offset", "lines", "bytes", "decodes", "malformed",
    "quota_only", "decoded_entry_count", "last_seen_stamped",
    "last_seen_legacy_claude_at", "last_seen_legacy_codex_at",
)


def summary_version() -> str:
    """The version token the sidecar is written with and validated against.

    DERIVED rather than hand-written, because the two things a stale summary can
    silently get wrong are both defined elsewhere and neither would remind
    anybody to bump a literal here.

    `quota_only` is computed from `RETAINED_RECORD_TYPES` at write time. If a
    record type later joins that set, a summary written before the change still
    says `quota_only=True` for a segment that now holds a retained record, the
    segment is elided, and its retained records are replaced by placeholders —
    a wrong selection with no fallback and nothing on stderr. Hashing the sorted
    set makes that change discard every existing sidecar instead.

    `STATS_INDEX_EPOCH` is the repository's existing marker for "what a rebuild
    derives from the journal changed", and every counter in a summary is part of
    that derivation. The coverage certificate's `interpretationVersion` does not
    cover this: it tracks cache materialization, not the stats selector.

    `SUMMARY_SHAPE_VERSION` stays hand-written for the one change neither
    derived input can see — a field added to, removed from or redefined within
    the summary itself.

    Discarding costs exactly one full read, and the sidecar is re-derived by the
    pass that discarded it.
    """
    material = json.dumps(
        {
            "shape": SUMMARY_SHAPE_VERSION,
            "fields": list(_FIELDS),
            "retained": sorted(_lib_journal_router.RETAINED_RECORD_TYPES),
            "epoch": int(_cctally_core.STATS_INDEX_EPOCH),
        },
        separators=(",", ":"), sort_keys=True,
    )
    return "v" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def to_wire(summary) -> dict:
    """One summary as a JSON-safe dict, in the canonical field order."""
    return {
        "segment_name": str(summary.segment_name),
        "st_dev": int(summary.st_dev),
        "st_ino": int(summary.st_ino),
        "summarized_size": int(summary.summarized_size),
        "complete_line_covered_offset": int(
            summary.complete_line_covered_offset),
        "lines": int(summary.lines),
        "bytes": int(summary.bytes),
        "decodes": int(summary.decodes),
        "malformed": int(summary.malformed),
        "quota_only": bool(summary.quota_only),
        "decoded_entry_count": (
            None if summary.decoded_entry_count is None
            else int(summary.decoded_entry_count)),
        "last_seen_stamped": {
            str(key): str(value)
            for key, value in dict(summary.last_seen_stamped).items()
        },
        "last_seen_legacy_claude_at": (
            None if summary.last_seen_legacy_claude_at is None
            else str(summary.last_seen_legacy_claude_at)),
        "last_seen_legacy_codex_at": (
            None if summary.last_seen_legacy_codex_at is None
            else str(summary.last_seen_legacy_codex_at)),
    }


def from_wire(item) -> SegmentSummary:
    """One summary from its wire form. Raises on any shape it did not write."""
    if not isinstance(item, dict):
        raise ValueError("segment summary is not an object")
    missing = [name for name in _FIELDS if name not in item]
    if missing:
        raise ValueError(f"segment summary is missing {missing}")
    stamped = item["last_seen_stamped"]
    if not isinstance(stamped, dict):
        raise ValueError("last_seen_stamped is not an object")
    count = item["decoded_entry_count"]
    return SegmentSummary(
        segment_name=str(item["segment_name"]),
        st_dev=int(item["st_dev"]),
        st_ino=int(item["st_ino"]),
        summarized_size=int(item["summarized_size"]),
        complete_line_covered_offset=int(item["complete_line_covered_offset"]),
        lines=int(item["lines"]),
        bytes=int(item["bytes"]),
        decodes=int(item["decodes"]),
        malformed=int(item["malformed"]),
        quota_only=bool(item["quota_only"]),
        decoded_entry_count=None if count is None else int(count),
        last_seen_stamped={
            str(key): str(value) for key, value in stamped.items()},
        last_seen_legacy_claude_at=(
            None if item["last_seen_legacy_claude_at"] is None
            else str(item["last_seen_legacy_claude_at"])),
        last_seen_legacy_codex_at=(
            None if item["last_seen_legacy_codex_at"] is None
            else str(item["last_seen_legacy_codex_at"])),
    )


def _checksum(segments) -> str:
    payload = json.dumps(segments, separators=(",", ":"), sort_keys=True,
                         ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_sidecar(path, summaries) -> None:
    """Replace the sidecar at ``path`` with ``summaries``, atomically.

    Atomic because a torn write is indistinguishable from a stale one to the
    reader, and the reader's only safe answer to either is to discard the whole
    file — which would silently retire elision until the next successful write.
    Failures are swallowed: the sidecar is a pure cache and a pass that could
    not write it is still a correct pass.
    """
    path = pathlib.Path(path)
    segments = [to_wire(item) for item in summaries]
    body = json.dumps(
        {
            "version": summary_version(),
            "checksum": _checksum(segments),
            "segments": segments,
        },
        separators=(",", ": "), sort_keys=True, ensure_ascii=False,
    )
    handle = None
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", dir=str(path.parent))
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            handle = None
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(path))
        tmp_name = None
    except OSError:
        return
    finally:
        if handle is not None:
            os.close(handle)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def read_sidecar(path):
    """``{segment_name: SegmentSummary}``, or None for anything unusable.

    Invalid, truncated, version-mismatched or checksum-mismatched content is
    DISCARDED rather than repaired. The sidecar is re-derivable from the journal
    by the next non-eliding pass, so discarding costs one full read and repairing
    would risk carrying a wrong extent or a wrong decoded-entry count forward —
    the two values every later elision decision rests on.
    """
    try:
        raw = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != summary_version():
        return None
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return None
    try:
        if payload.get("checksum") != _checksum(segments):
            return None
        return {
            item.segment_name: item
            for item in (from_wire(entry) for entry in segments)
        }
    except (AttributeError, TypeError, ValueError):
        return None


def with_identity(summary, *, st_dev, st_ino) -> SegmentSummary:
    """``summary`` restamped with the inode identity of the file just read."""
    return replace(summary, st_dev=int(st_dev), st_ino=int(st_ino))
