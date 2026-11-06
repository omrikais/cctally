"""Coordinate-backed retained journal records (#1004, spec D3 and D4).

A journal reader that keeps every record it decoded until its planning or
replay passes end holds ≈ 0.9 KB per record, plus two lists of
``(segment, offset)`` tuples at ≈ 100 bytes per record each. This module keeps
coordinates instead and re-reads a record's line when a consumer asks for it:

* `LineCoordinates` holds one ``array('Q')`` of line start offsets and one
  ``array('I')`` of line lengths (the newline excluded), 12 bytes a position,
  plus a table of segment runs. Its ``starts()`` and ``ends()`` views yield
  the ``(segment, start)`` and ``(segment, start + length + 1)`` tuples the
  readers' two lists held.
* `RetainedRecords` is a sequence over those positions. A slot holds either a
  retained object (an interned stub, an admission placeholder, a ``None``
  slot) or the private `STREAMED` sentinel, which means "decode from the
  coordinates". A streamed line's 8-byte BLAKE2b digest is taken in the read
  pass and checked on every re-read, so a rewrite of the same length, which
  framing alone cannot detect, raises too.

Pure and stdlib-only: the caller supplies the line reader, the decoder and the
error type, so the readers keep their own framing check and error contract.
"""
from __future__ import annotations

import bisect
import hashlib
from array import array
from collections.abc import Sequence
from typing import Callable, Protocol

#: Test switch (#1004 T6): when true, the readers build today's list form (a
#: plain list of decoded records and a plain list of end tuples) instead of a
#: `RetainedRecords`. Read at reader time. Never set by product code.
MATERIALIZE_FOR_TESTS = False


class _Streamed:
    """The slot marker for "decode this position from its coordinates"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "STREAMED"


#: The one slot value meaning a position is decoded on access.
STREAMED = _Streamed()


def line_digest(line: bytes) -> int:
    """The 8-byte BLAKE2b digest of ``line``, as an unsigned integer."""
    return int.from_bytes(
        hashlib.blake2b(line, digest_size=8).digest(), "little")


def _sequence_eq(left, right):
    if not isinstance(right, (list, tuple, Sequence)) or isinstance(
            right, (str, bytes)):
        return NotImplemented
    if len(left) != len(right):
        return False
    return all(a == b for a, b in zip(left, right))


# --------------------------------------------------------------------------
# D3: per-position coordinates
# --------------------------------------------------------------------------


class LineCoordinates:
    """Per-position line coordinates in two arrays and a segment-run table.

    ``append(segment, start, length)`` adds one position: ``start`` is the
    line's first byte and ``length`` excludes its newline. Positions are in
    journal order, so within one run the end coordinates strictly increase.
    """

    __slots__ = ("_starts", "_lengths", "_run_segments", "_run_firsts",
                 "__weakref__")

    def __init__(self) -> None:
        self._starts = array("Q")
        self._lengths = array("I")
        #: One entry per run of consecutive positions in the same segment.
        self._run_segments: list = []
        self._run_firsts = array("Q")

    def append(self, segment: str, start: int, length: int) -> None:
        segments = self._run_segments
        if not segments or segments[-1] != segment:
            segments.append(segment)
            self._run_firsts.append(len(self._starts))
        self._starts.append(start)
        self._lengths.append(length)

    def __len__(self) -> int:
        return len(self._starts)

    def _run_of(self, index: int) -> int:
        return bisect.bisect_right(self._run_firsts, index) - 1

    def _normalize(self, index: int) -> int:
        size = len(self._starts)
        if index < 0:
            index += size
        if not 0 <= index < size:
            raise IndexError("line coordinate index out of range")
        return index

    def line(self, i: int) -> "tuple[str, int, int]":
        """``(segment, start, length)`` of position ``i``."""
        i = self._normalize(i)
        return (self._run_segments[self._run_of(i)], self._starts[i],
                self._lengths[i])

    def _start(self, i: int):
        i = self._normalize(i)
        return (self._run_segments[self._run_of(i)], self._starts[i])

    def _end(self, i: int):
        i = self._normalize(i)
        return (self._run_segments[self._run_of(i)],
                self._starts[i] + self._lengths[i] + 1)

    def _iter(self, positions, end: bool):
        starts = self._starts
        lengths = self._lengths
        segments = self._run_segments
        firsts = self._run_firsts
        run = -1
        run_stop = -1
        segment = None
        for i in positions:
            if not (run >= 0 and firsts[run] <= i < run_stop):
                run = bisect.bisect_right(firsts, i) - 1
                segment = segments[run]
                run_stop = (firsts[run + 1] if run + 1 < len(firsts)
                            else len(starts))
            if end:
                yield (segment, starts[i] + lengths[i] + 1)
            else:
                yield (segment, starts[i])

    def _index_end(self, cursor) -> int:
        """The position whose end coordinate is ``cursor``; ValueError when
        none is. A binary search inside each run of ``cursor``'s segment."""
        try:
            segment, end = cursor
        except (TypeError, ValueError):
            raise ValueError(f"{cursor!r} is not in list") from None
        starts = self._starts
        lengths = self._lengths
        firsts = self._run_firsts
        for run, name in enumerate(self._run_segments):
            if name != segment:
                continue
            lo = firsts[run]
            hi = firsts[run + 1] if run + 1 < len(firsts) else len(starts)
            while lo < hi:
                mid = (lo + hi) // 2
                value = starts[mid] + lengths[mid] + 1
                if value < end:
                    lo = mid + 1
                elif value > end:
                    hi = mid
                else:
                    return mid
        raise ValueError(f"{cursor!r} is not in list")

    def starts(self) -> "_CoordinateView":
        """Every position's ``(segment, start)``, as a view."""
        return _CoordinateView(self, None, end=False)

    def ends(self) -> "_CoordinateView":
        """Every position's ``(segment, start + length + 1)``, as a view."""
        return _CoordinateView(self, None, end=True)


class _CoordinateView(Sequence):
    """A view of `LineCoordinates` positions as start or end tuples.

    ``positions`` is ``None`` for the whole (live) coordinate set, otherwise a
    ``range`` of positions. Slicing returns a view; nothing is copied."""

    __slots__ = ("_coords", "_positions", "_end", "__weakref__")

    def __init__(self, coords: LineCoordinates, positions, *, end: bool):
        self._coords = coords
        self._positions = positions
        self._end = end

    def _range(self):
        if self._positions is None:
            return range(len(self._coords))
        return self._positions

    def __len__(self) -> int:
        return len(self._range())

    def __getitem__(self, index):
        positions = self._range()
        if isinstance(index, slice):
            return _CoordinateView(self._coords, positions[index],
                                   end=self._end)
        position = positions[index]
        return (self._coords._end(position) if self._end
                else self._coords._start(position))

    def __iter__(self):
        return self._coords._iter(self._range(), self._end)

    def index(self, cursor, start=0, stop=None):
        if not self._end:
            return Sequence.index(self, cursor, start, stop)
        position = self._coords._index_end(cursor)
        positions = self._range()
        try:
            found = positions.index(position)
        except ValueError:
            raise ValueError(f"{cursor!r} is not in list") from None
        if stop is None:
            stop = len(positions)
        if start < 0:
            start = max(len(positions) + start, 0)
        if stop < 0:
            stop += len(positions)
        if not start <= found < stop:
            raise ValueError(f"{cursor!r} is not in list")
        return found

    def __eq__(self, other):
        return _sequence_eq(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        kind = "ends" if self._end else "starts"
        return f"<LineCoordinates.{kind} view of {len(self)}>"


# --------------------------------------------------------------------------
# D4: coordinate-backed records
# --------------------------------------------------------------------------


class LineReader(Protocol):
    """Reads one journal line back by its coordinates."""

    def read(self, segment: str, start: int, length: int) -> bytes:
        """The ``length`` bytes at ``start``, verified to still be one framed
        line; raises the caller's error on a framing mismatch."""

    def close(self) -> None:
        """Release every handle."""


class RetainedRecords(Sequence):
    """A reader's retained records, streamed from coordinates (spec D4).

    Each position's slot holds either a retained object or `STREAMED`. A
    streamed position is re-read on access through a reader from
    ``open_reader``, digest-checked, decoded with ``decode`` and normalized in
    place by the installed normalizer: each access returns a fresh private
    object equal to the one the reader retained before. ``error(message)``
    builds the exception a changed line raises."""

    def __init__(self, coordinates: LineCoordinates,
                 open_reader: "Callable[[], LineReader]",
                 decode: "Callable[[bytes], dict]", *,
                 error: "Callable[[str], Exception]") -> None:
        self._coords = coordinates
        self._open_reader = open_reader
        self._decode = decode
        self._error = error
        self._slots: list = []
        #: One digest per position (zero for a retained slot).
        self._digests = array("Q")
        self._normalizer = None
        self._reader = None

    # -- building -----------------------------------------------------------

    def append_retained(self, obj, segment: str, start: int,
                        length: int) -> None:
        self._coords.append(segment, start, length)
        self._slots.append(obj)
        self._digests.append(0)

    def append_streamed(self, line: bytes, segment: str, start: int,
                        length: int) -> None:
        self._coords.append(segment, start, length)
        self._slots.append(STREAMED)
        self._digests.append(line_digest(line))

    def set_normalizer(self, fn: "Callable[[dict], None] | None") -> None:
        """Install ``fn``, applied in place to every fresh decode."""
        self._normalizer = fn

    @property
    def coordinates(self) -> LineCoordinates:
        return self._coords

    # -- reading ------------------------------------------------------------

    def _decode_at(self, reader, index: int):
        segment, start, length = self._coords.line(index)
        line = reader.read(segment, start, length)
        if line_digest(line) != self._digests[index]:
            raise self._error(
                f"{segment}@{start}: the line the read pass saw has changed")
        record = self._decode(line)
        if record is None:
            raise self._error(
                f"{segment}@{start}: the line the read pass saw no longer "
                "decodes")
        if self._normalizer is not None:
            self._normalizer(record)
        return record

    def _random_reader(self):
        if self._reader is None:
            self._reader = self._open_reader()
        return self._reader

    def _item(self, index: int):
        slot = self._slots[index]
        if slot is STREAMED:
            return self._decode_at(self._random_reader(), index)
        return slot

    def _iter_positions(self, positions, marked: bool = False):
        """The records at ``positions`` (ascending, for one handle per
        segment), through one reader opened for this pass. ``marked`` yields
        ``(fresh, record)`` pairs, ``fresh`` true for a streamed decode."""
        slots = self._slots
        reader = None
        try:
            for index in positions:
                slot = slots[index]
                if slot is STREAMED:
                    if reader is None:
                        reader = self._open_reader()
                    record = self._decode_at(reader, index)
                    yield (True, record) if marked else record
                else:
                    yield (False, slot) if marked else slot
        finally:
            if reader is not None:
                reader.close()

    def iter_marked(self):
        """Every position as ``(fresh, record)``: ``fresh`` is true when the
        record is a private decode of a streamed slot, false for a retained
        object (which a consumer must not mutate)."""
        return self._iter_positions(range(len(self._slots)), marked=True)

    def __len__(self) -> int:
        return len(self._slots)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _RecordsView(self, range(len(self._slots))[index], ())
        size = len(self._slots)
        if index < 0:
            index += size
        if not 0 <= index < size:
            raise IndexError("retained record index out of range")
        return self._item(index)

    def __iter__(self):
        return self._iter_positions(range(len(self._slots)))

    def __setitem__(self, index: int, placeholder) -> None:
        # The digest stays: a `slot_snapshot` shares the digest array and
        # still decodes the original at this position.
        self._slots[index] = placeholder

    def is_streamed(self, i: int) -> bool:
        return self._slots[i] is STREAMED

    def slot(self, i: int) -> object:
        """The raw slot: the retained object, or `STREAMED`."""
        return self._slots[i]

    def subset(self, positions: "Sequence[int]") -> "Sequence[dict]":
        return _RecordsView(self, array("Q", positions), ())

    def with_extra(self, obj) -> "Sequence[dict]":
        return _RecordsView(self, range(len(self._slots)), (obj,))

    def slot_snapshot(self) -> "RetainedRecords":
        """A copy of the slot list sharing the coordinates and digests."""
        copy = RetainedRecords(self._coords, self._open_reader, self._decode,
                               error=self._error)
        copy._slots = list(self._slots)
        copy._digests = self._digests
        copy._normalizer = self._normalizer
        return copy

    def close(self) -> None:
        """Release the random-access reader's handles. Iteration passes
        release their own when they end."""
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.close()

    def __del__(self) -> None:
        # A sequence dropped without `close()` (a preview that is simply
        # discarded) still releases its handle at once.
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from a finalizer
            pass

    def __eq__(self, other):
        return _sequence_eq(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<RetainedRecords of {len(self._slots)}>"


class _RecordsView(Sequence):
    """Positions of one `RetainedRecords`, followed by retained extras: a
    slice, a position subset, or the sequence with one object appended."""

    __slots__ = ("_base", "_positions", "_extras", "__weakref__")

    def __init__(self, base: RetainedRecords, positions, extras: tuple):
        self._base = base
        self._positions = positions
        self._extras = extras

    def __len__(self) -> int:
        return len(self._positions) + len(self._extras)

    def _locate(self, index: int):
        """``(True, base position)`` or ``(False, extra index)``."""
        size = len(self)
        if index < 0:
            index += size
        if not 0 <= index < size:
            raise IndexError("retained record index out of range")
        count = len(self._positions)
        if index < count:
            return True, self._positions[index]
        return False, index - count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._slice(index)
        in_base, where = self._locate(index)
        if in_base:
            return self._base._item(where)
        return self._extras[where]

    def _slice(self, index: slice):
        selected = range(len(self))[index]
        count = len(self._positions)
        step = selected.step
        if step < 0:
            return [self[i] for i in selected]
        start, stop = selected.start, selected.stop
        if start < count:
            base_part = self._positions[start:min(stop, count):step]
            first_extra = start + -(-(count - start) // step) * step
        else:
            base_part = self._positions[0:0]
            first_extra = start
        extras = self._extras[first_extra - count:max(stop - count, 0):step]
        return _RecordsView(self._base, base_part, tuple(extras))

    def __iter__(self):
        yield from self._base._iter_positions(self._positions)
        yield from self._extras

    def iter_marked(self):
        yield from self._base._iter_positions(self._positions, marked=True)
        for extra in self._extras:
            yield False, extra

    def is_streamed(self, i: int) -> bool:
        in_base, where = self._locate(i)
        return in_base and self._base.is_streamed(where)

    def slot(self, i: int) -> object:
        in_base, where = self._locate(i)
        return self._base.slot(where) if in_base else self._extras[where]

    def subset(self, positions: "Sequence[int]") -> "Sequence[dict]":
        count = len(self._positions)
        base_part = array("Q")
        extras = []
        for i in positions:
            if i < 0 or i >= len(self):
                raise IndexError("retained record index out of range")
            if i < count:
                if extras:
                    return [self[j] for j in positions]
                base_part.append(self._positions[i])
            else:
                extras.append(self._extras[i - count])
        return _RecordsView(self._base, base_part, tuple(extras))

    def with_extra(self, obj) -> "Sequence[dict]":
        return _RecordsView(self._base, self._positions,
                            (*self._extras, obj))

    def __eq__(self, other):
        return _sequence_eq(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"<RetainedRecords view of {len(self)}>"
