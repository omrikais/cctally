"""Pure record-routing kernel for the stats rebuild's journal read pass (#496 S4).

The rebuild used to materialize the whole journal twice: `_read_range` built a
list of every raw line, and the decode loop built a second list of every parsed
record while the first was still referenced. On the maintainer's install that is
1,954,007 lines and 1.72 GB producing an 8.08 GiB peak, of which the stats fold
consumes 99,289 records (5.08%).

`bin/_cctally_doctor.py`'s conflict scan already established the shape this
kernel generalizes: retain only what the effective selector consumes and drop
everything else as it is decoded (#374 review, measured at 4.3 GB of peak RSS
for an identical result).

No I/O and no imports from `_cctally_journal`, so the rules here are unit
testable without a journal on disk.
"""
from __future__ import annotations

import hashlib


#: The record types the stats rebuild retains decoded. `resolve_effective_events`
#: acts only on evt / correction / correction_batch and the protocol-resolution
#: op; the op-fold stream takes only ops whose kind is in `FOLD_APPLIERS`.
#: Deliberately identical to `_cctally_doctor._CONFLICT_SCAN_RECORD_TYPES` — the
#: two must not drift, because both feed the same shared selector.
RETAINED_RECORD_TYPES = frozenset({"evt", "correction", "correction_batch", "op"})


def selector_slot(record):
    """Return one position-preserving input slot for the shared selector.

    Every successfully decoded journal line consumes one sequence number in
    ``resolve_effective_events``.  Decision records therefore stay decoded,
    while observations and other irrelevant records become ``None`` rather
    than being dropped (which renumbers durable protocol fingerprints) or kept
    as dictionaries (which makes memory follow observation volume).
    """
    if record.get("t") in RETAINED_RECORD_TYPES:
        return record
    return None


class LastSeenAccumulator:
    """Reproduce `_derive_account_last_seen`'s contribution set from a stream.

    The rebuild normalizes every record and then takes `_account_of`, which
    reads a top-level ``account`` or an ``account_observe`` op's
    ``payload.account_key``. `_normalize_legacy_account_stamp` writes a
    top-level ``account`` ONLY for ``t == "obs"``; a legacy evt or op instead
    gets ``payload.account_key``, which `_account_of` does not read.

    So exactly three classes contribute, and a provider-wide maximum over every
    legacy line would over-count — advancing `last_seen_utc` from legacy events
    and vendor-tagged budget events that contribute nothing today. The Claude
    legacy bucket is deferred because the cutover account is not known until the
    stream reaches it, at 92.9% of a production journal.
    """

    __slots__ = ("stamped", "legacy_claude_at", "legacy_codex_at")

    def __init__(self) -> None:
        self.stamped: dict = {}
        self.legacy_claude_at = None
        self.legacy_codex_at = None

    def observe(self, record, provider_of_legacy) -> None:
        """Fold one record. `provider_of_legacy` is `classify_legacy_provider`."""
        at = record.get("at")
        if not at:
            return
        account = record.get("account")
        if isinstance(account, str) and account:
            self._bump(account, at)
            return
        record_type = record.get("t")
        if record_type == "op":
            payload = record.get("payload") or {}
            if payload.get("kind") == "account_observe":
                key = payload.get("account_key")
                if isinstance(key, str) and key:
                    self._bump(key, at)
            return
        if record_type != "obs":
            # A legacy evt normalizes into `payload.account_key`, which
            # `_account_of` ignores. Contributing here would move last-seen.
            return
        provider = provider_of_legacy(record)
        if provider == "claude":
            if self.legacy_claude_at is None or at > self.legacy_claude_at:
                self.legacy_claude_at = at
        elif provider == "codex":
            if self.legacy_codex_at is None or at > self.legacy_codex_at:
                self.legacy_codex_at = at

    def _bump(self, key: str, at: str) -> None:
        previous = self.stamped.get(key)
        if previous is None or at > previous:
            self.stamped[key] = at

    def merge(self, stamped, legacy_claude_at=None, legacy_codex_at=None):
        """Fold another accumulator's partial state into this one.

        The fold is a per-key MAXIMUM over timestamps, and maximum is
        associative and commutative, so a segment's partial contribution merges
        in any order and produces the same map a single pass would. That is what
        lets #496 S5b Stage 4 import an elided segment's stored contribution
        instead of reading the segment (spec section 5.4).
        """
        for key, at in dict(stamped).items():
            if isinstance(key, str) and key and isinstance(at, str) and at:
                self._bump(key, at)
        if legacy_claude_at is not None and (
                self.legacy_claude_at is None
                or legacy_claude_at > self.legacy_claude_at):
            self.legacy_claude_at = legacy_claude_at
        if legacy_codex_at is not None and (
                self.legacy_codex_at is None
                or legacy_codex_at > self.legacy_codex_at):
            self.legacy_codex_at = legacy_codex_at

    def resolve(self, cutover_claude: str, unattributed: str) -> dict:
        """Apply the deferred legacy buckets and return the final MAX map."""
        out = dict(self.stamped)
        if self.legacy_claude_at is not None:
            previous = out.get(cutover_claude)
            if previous is None or self.legacy_claude_at > previous:
                out[cutover_claude] = self.legacy_claude_at
        if self.legacy_codex_at is not None:
            previous = out.get(unattributed)
            if previous is None or self.legacy_codex_at > previous:
                out[unattributed] = self.legacy_codex_at
        return out


class PrefixEvidenceUnavailable(LookupError):
    """A protocol-evidence digest was asked for a prefix the stream dropped.

    Never expected: an evidence point is always the end of the line immediately
    preceding a `journal_protocol_resolution` op, so it is either inside the
    segment being streamed or the boundary registered at the last segment
    transition. Raised rather than silently degraded, because the alternative is
    a rebuild that quietly disagrees with `journal_prefix_hash`.
    """


class PrefixHashAccumulator:
    """`journal_prefix_hash` computed from the bytes the rebuild already read.

    `journal_prefix_hash(prior_high_water)` does `path.read_bytes()[:size]` on
    every segment through the prefix, so one `journal_protocol_resolution` op
    re-reads the whole journal up to its own position and builds a full-segment
    bytes transient. This accumulator reproduces the identical durable digest
    from the bytes the single streaming pass is reading anyway (#496 S4 §5.2).

    The framing is `journal_prefix_hash`'s, verbatim: per segment, the 4-byte
    big-endian name length, the name, the 8-byte big-endian data length, then
    the data. The data length precedes the data, so `begin_segment` takes the
    exact byte count the pass will feed (`size`) and frames the segment header
    with it; every chunk is then hashed as it arrives and NO segment bytes are
    retained (#997 D12). The buffered form held the whole current segment in a
    `bytearray` grown by `extend` — 410 MB on the maintainer's largest segment
    — and the freed growth steps stayed resident in the macOS allocator.

    A digest at a fully fed segment end (every transition, and every boundary
    not short of a torn tail) comes from the streamed state. A digest at any
    other offset — an evidence point inside the current segment, a boundary
    short of a torn trailing line, a feed that ended short of its declared
    size — re-reads the segment through `open_segment(name)` in 1 MiB chunks,
    which the production callers bind to the journal's one segment-read seam,
    so an evidence re-read is visible to it.

    Consumed extent (#997 D12, revision 10). Per segment the accumulator also
    tracks the bytes actually fed (`consumed`) and a raw SHA-256 of those bytes
    alone (the witness). An offset beyond `consumed` raises. Every re-read reads
    through `consumed`, not only through the requested offset: one pass frames
    the prefix digest at the offset and hashes `[0, consumed)` raw, and the
    digest is returned only when the read was not short and that raw hash
    equals the witness. A torn tail an appender truncated and replaced after
    the pass read it, or a line the reader assembled from old and replacement
    bytes across two reads, can therefore never be certified. A registered
    boundary keeps its segment's `consumed` and witness with its saved hash
    state. A segment fed short of its declared size is re-framed from its
    consumed bytes through the same verified re-read at the next
    `begin_segment`; when that re-read is unavailable or fails verification
    the accumulator is poisoned and every later digest raises. A missing
    opener, a short re-read or a failed verification raises
    `PrefixEvidenceUnavailable`.
    """

    # A transition can only ever register one boundary, and consecutive empty
    # segments re-register the same one. A handful of slots is therefore already
    # generous; the cap exists so a pathological journal cannot grow this map.
    # Eviction is safe because a registered boundary is only ever READ
    # IMMEDIATELY AFTER the `begin_segment` that registered it: a resolution op
    # can name a previous segment's end only when it is the first line of the
    # new segment, and every later position falls in the `_current_name` branch
    # of `digest_at`. So the cap bounds a map that never needs more than the
    # most recent entry; widening it buys nothing and narrowing it below the
    # runs of empty segments a journal can contain would start dropping the one
    # entry that is still live.
    _MAX_BOUNDARIES = 16

    #: Chunk size of an evidence re-read through the opener.
    _REREAD_CHUNK = 1 << 20

    def __init__(self, *, open_segment=None) -> None:
        self._open_segment = open_segment
        #: The hash of every segment before the current one, absorbed.
        self._running = hashlib.sha256()
        self._current_name = None
        #: `_running` plus the current segment's header (framed with its
        #: declared size) and every byte fed so far.
        self._segment = None
        self._declared = 0
        #: Bytes of the current segment actually fed (its consumed extent),
        #: and the raw SHA-256 of exactly those bytes (the witness).
        self._fed = 0
        self._witness = hashlib.sha256()
        #: Set when a short segment could not be re-framed from verified
        #: bytes: the running state no longer describes the journal.
        self._poisoned = None
        #: `(segment, offset) -> (state before that segment, final digest or
        #: None, consumed, witness digest)`; a `None` final digest is re-read
        #: through the opener, verified against the witness, when read.
        self._boundaries: dict = {}
        self._boundary_order: list = []
        #: Bytes fed into an evidence digest, reported as the `protocol_evidence`
        #: traversal pass. Zero on any journal with no resolution op.
        self.bytes_hashed = 0
        self.digests_computed = 0

    # -- feeding ----------------------------------------------------------

    def begin_segment(self, name: str, boundary=None, *, size: int) -> None:
        """Start `name`, which the pass will feed `size` bytes of, absorbing
        whatever segment was open before it.

        `boundary` is the `(segment, offset)` the next record's evidence would
        name — i.e. the streaming loop's current `prior_high_water`. Its hash
        state is kept HERE because the outgoing segment's state is folded into
        the running hash immediately afterwards.
        """
        if boundary is not None:
            self._register_boundary(boundary)
        if self._current_name is not None:
            self._running = self._completed_segment()
        self._current_name = name
        self._declared = int(size)
        self._fed = 0
        self._witness = hashlib.sha256()
        self._segment = self._running.copy()
        self._header(self._segment, name, self._declared)

    def extend(self, data) -> None:
        """Hash raw bytes read from the segment currently open."""
        self._segment.update(data)
        self._witness.update(data)
        self._fed += len(data)

    # -- reading ----------------------------------------------------------

    def digest_at(self, high_water) -> "str | None":
        """`journal_prefix_hash(high_water)`, from the streamed state."""
        if high_water is None:
            return None
        segment, offset = high_water
        if self._poisoned is not None:
            raise PrefixEvidenceUnavailable(
                f"no verified prefix evidence for {segment}@{offset}: "
                f"{self._poisoned}"
            )
        if segment == self._current_name:
            if self._streamed_end(offset):
                digest = self._segment.copy()
            else:
                digest = self._verified_rehash(
                    self._running, segment, offset, self._fed,
                    self._witness.digest())
        else:
            cached = self._boundaries.get((segment, offset))
            if cached is None:
                raise PrefixEvidenceUnavailable(
                    f"no streamed prefix evidence for {segment}@{offset}"
                )
            before, final, consumed, witness = cached
            digest = (final.copy() if final is not None
                      else self._verified_rehash(
                          before, segment, offset, consumed, witness))
        self.bytes_hashed += offset
        self.digests_computed += 1
        return "sha256:" + digest.hexdigest()

    # -- internals --------------------------------------------------------

    def _streamed_end(self, offset) -> bool:
        """Whether the streamed state IS the digest at `offset`: the current
        segment was fed exactly its declared size and `offset` is its end."""
        return offset == self._fed == self._declared

    def _completed_segment(self):
        """The running hash with the current segment absorbed as fed."""
        if self._fed == self._declared or self._poisoned is not None:
            return self._segment
        # The feed ended short of the declared size (the segment shrank under
        # the pass): frame the bytes actually fed, as the buffered form did,
        # from a re-read verified against them. Bytes that cannot be verified
        # leave no running state that describes the journal, so every later
        # digest raises rather than certifying one.
        try:
            return self._verified_rehash(
                self._running, self._current_name, self._fed, self._fed,
                self._witness.digest())
        except (PrefixEvidenceUnavailable, OSError) as exc:
            self._poisoned = (
                f"short segment {self._current_name} could not be re-framed "
                f"from its consumed bytes ({exc})"
            )
            return self._segment

    def _register_boundary(self, boundary) -> None:
        segment, offset = boundary
        if segment != self._current_name:
            # Already registered at an earlier transition (a run of empty
            # segments leaves `prior_high_water` unchanged), or never streamed.
            return
        key = (segment, offset)
        if key in self._boundaries:
            return
        final = (self._segment.copy() if self._streamed_end(offset)
                 else None)
        self._boundaries[key] = (self._running.copy(), final, self._fed,
                                 self._witness.digest())
        self._boundary_order.append(key)
        while len(self._boundary_order) > self._MAX_BOUNDARIES:
            self._boundaries.pop(self._boundary_order.pop(0), None)

    def _verified_rehash(self, before, name: str, offset: int,
                         consumed: int, witness: bytes):
        """`before` plus `name`'s first `offset` bytes, re-read from the
        segment through the opener in `_REREAD_CHUNK` pieces.

        The read always runs through `consumed`, the bytes the pass fed, and
        its raw SHA-256 must equal `witness`; only then are the bytes the
        digest frames the bytes the pass read."""
        if offset > consumed:
            raise PrefixEvidenceUnavailable(
                f"prefix evidence {name}@{offset} lies beyond the "
                f"{consumed} bytes the pass consumed"
            )
        digest = before.copy()
        self._header(digest, name, offset)
        if consumed <= 0:
            return digest
        if self._open_segment is None:
            raise PrefixEvidenceUnavailable(
                f"no segment opener to re-read prefix evidence for "
                f"{name}@{offset}"
            )
        raw = hashlib.sha256()
        pos = 0
        with self._open_segment(name) as handle:
            while pos < consumed:
                chunk = handle.read(min(self._REREAD_CHUNK, consumed - pos))
                if not chunk:
                    raise PrefixEvidenceUnavailable(
                        f"short re-read of prefix evidence for {name}@{offset}"
                    )
                raw.update(chunk)
                if pos < offset:
                    with memoryview(chunk)[:offset - pos] as framed:
                        digest.update(framed)
                pos += len(chunk)
        if raw.digest() != witness:
            raise PrefixEvidenceUnavailable(
                f"segment {name} no longer holds the {consumed} bytes the "
                f"pass consumed; prefix evidence {name}@{offset} is not "
                f"certified"
            )
        return digest

    @staticmethod
    def _header(digest, name: str, length: int) -> None:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(int(length).to_bytes(8, "big"))
