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
    the data. Completed segments are absorbed into a running `sha256`; only the
    segment currently being streamed is buffered, so residency is bounded by one
    segment. At a segment transition the caller passes the boundary the next
    record's `prior_high_water` will name, which is the ONE offset in the
    outgoing segment that can still be asked for; its digest is precomputed
    before the buffer is released.
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

    def __init__(self) -> None:
        self._running = hashlib.sha256()
        self._current_name = None
        self._current = bytearray()
        self._boundaries: dict = {}
        self._boundary_order: list = []
        #: Bytes fed into an evidence digest, reported as the `protocol_evidence`
        #: traversal pass. Zero on any journal with no resolution op.
        self.bytes_hashed = 0
        self.digests_computed = 0

    # -- feeding ----------------------------------------------------------

    def begin_segment(self, name: str, boundary=None) -> None:
        """Start `name`, absorbing whatever segment was open before it.

        `boundary` is the `(segment, offset)` the next record's evidence would
        name — i.e. the streaming loop's current `prior_high_water`. It is
        resolved and cached HERE because the outgoing segment's bytes are gone
        immediately afterwards.
        """
        if boundary is not None:
            self._register_boundary(boundary)
        if self._current_name is not None:
            # `hashlib.update` accepts the buffer directly, so the outgoing
            # segment is framed WITHOUT a copy of itself. The maintainer's
            # largest segment is 410 MB, and copying it here briefly doubled
            # that at every transition.
            with memoryview(self._current) as data:
                self._absorb(self._current_name, data)
        self._current_name = name
        self._current = bytearray()

    def extend(self, data: bytes) -> None:
        """Absorb raw bytes read from the segment currently open."""
        self._current.extend(data)

    # -- reading ----------------------------------------------------------

    def digest_at(self, high_water) -> "str | None":
        """`journal_prefix_hash(high_water)`, from the streamed bytes."""
        if high_water is None:
            return None
        segment, offset = high_water
        if segment == self._current_name:
            digest = self._running.copy()
            with memoryview(self._current)[:offset] as data:
                self._frame(digest, segment, data)
                self.bytes_hashed += len(data)
            self.digests_computed += 1
            return "sha256:" + digest.hexdigest()
        cached = self._boundaries.get((segment, offset))
        if cached is None:
            raise PrefixEvidenceUnavailable(
                f"no streamed prefix evidence for {segment}@{offset}"
            )
        self.bytes_hashed += cached[1]
        self.digests_computed += 1
        return cached[0]

    # -- internals --------------------------------------------------------

    def _register_boundary(self, boundary) -> None:
        segment, offset = boundary
        if segment != self._current_name:
            # Already registered at an earlier transition (a run of empty
            # segments leaves `prior_high_water` unchanged), or never streamed.
            return
        key = (segment, offset)
        if key in self._boundaries:
            return
        digest = self._running.copy()
        with memoryview(self._current)[:offset] as data:
            self._frame(digest, segment, data)
            size = len(data)
        self._boundaries[key] = ("sha256:" + digest.hexdigest(), size)
        self._boundary_order.append(key)
        while len(self._boundary_order) > self._MAX_BOUNDARIES:
            self._boundaries.pop(self._boundary_order.pop(0), None)

    def _absorb(self, name, data) -> None:
        self._frame(self._running, name, data)

    @staticmethod
    def _frame(digest, name: str, data) -> None:
        """`data` is any bytes-like buffer; `hashlib` consumes it in place."""
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
