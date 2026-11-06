"""#1004 T4 and T5 (library part) — coordinate-backed retained records.

The rederive reader, the final rebuild and completion's window each kept every
retained journal record decoded (≈ 0.9 KB a record) and two lists of
``(segment, offset)`` tuples (≈ 100 bytes a record each). `LineCoordinates`
keeps 12 bytes a position and `RetainedRecords` re-reads a record's line on
access. These tests pin the failures that would cost accuracy or memory:

* a coordinate view that differs from the list it replaces (a wrong commit
  cursor or quarantine coordinate), or an ``index`` that finds a cursor the
  list would not (a wrong materialization decision);
* a view that copies the coordinates back into tuples (the memory regression);
* a record re-read after its segment was rewritten in place with the same
  length, or truncated, that is silently decoded instead of refused.
"""
from __future__ import annotations

import gc
import importlib
import json
import tracemalloc

import pytest


class _Changed(Exception):
    pass


def _lib():
    return importlib.import_module("_lib_retained_records")


class _Reader:
    """A framed line reader over ``root`` (the shape the readers use), one
    open handle at a time, recording every segment it opens."""

    def __init__(self, root, opened):
        self._root = root
        self._opened = opened
        self._name = None
        self._fh = None

    def read(self, segment, start, length):
        jr = importlib.import_module("_cctally_journal")
        if segment != self._name:
            self.close()
            self._fh = open(self._root / segment, "rb")
            self._name = segment
            self._opened.append(segment)
        try:
            return jr._read_framed_line(self._fh, segment, start, length)
        except jr.QuotaLineSourceError as exc:
            raise _Changed(str(exc)) from exc

    def close(self):
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._name = None


def _segment_lines(index):
    """Records of one segment; segment 0 holds a malformed line."""
    lines = []
    for n in range(5):
        lines.append(json.dumps({
            "t": "obs", "id": f"o-{index}-{n}", "seq": n,
            "payload": {"weekly_percent": 10, "pad": "x" * (n * 7)}},
            separators=(",", ":")).encode())
        if index == 0 and n == 2:
            lines.append(b'{"t": broken')
    return lines


def _write_journal(root, segments=("seg-a.jsonl", "seg-b.jsonl")):
    """Write the fixture and return today's two lists and the decoded
    records, built the way the readers built them."""
    starts, ends, records, lines_out = [], [], [], []
    for index, name in enumerate(segments):
        lines = _segment_lines(index)
        (root / name).write_bytes(b"".join(line + b"\n" for line in lines))
        offset = 0
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if record is not None:
                starts.append((name, offset))
                ends.append((name, offset + len(line) + 1))
                records.append(record)
                lines_out.append((name, offset, line))
            offset += len(line) + 1
    return starts, ends, records, lines_out


def _coordinates(lines_out):
    coords = _lib().LineCoordinates()
    for name, offset, line in lines_out:
        coords.append(name, offset, len(line))
    return coords


def _retained(tmp_path, *, retained_at=(), opened=None):
    """A `RetainedRecords` over the fixture: every position streamed except
    those in ``retained_at``, which keep a marker object."""
    lib = _lib()
    starts, ends, records, lines_out = _write_journal(tmp_path)
    opened = [] if opened is None else opened
    coords = lib.LineCoordinates()
    seq = lib.RetainedRecords(
        coords, lambda: _Reader(tmp_path, opened), json.loads,
        error=_Changed)
    markers = {}
    for position, (name, offset, line) in enumerate(lines_out):
        if position in retained_at:
            markers[position] = {"t": "stub", "position": position}
            seq.append_retained(markers[position], name, offset, len(line))
        else:
            seq.append_streamed(line, name, offset, len(line))
    expected = [markers.get(i, record) for i, record in enumerate(records)]
    return seq, expected, ends, markers


# --- T4: line coordinates ----------------------------------------------------


SLICES = [slice(None), slice(2, 7), slice(4, 6), slice(-3, None),
          slice(1, None, 2), slice(None, None, -1), slice(8, 2, -2),
          slice(20, 30), slice(3, 3)]


def test_coordinate_views_equal_todays_lists(tmp_path):
    """Spec T4: as starts and as ends, element for element, in slices and in
    iteration, across the segment boundary and around the malformed line.
    A view that drifts by one position (the skipped malformed line) or keeps
    the wrong segment after the boundary names a wrong commit cursor."""
    starts, ends, _records, lines_out = _write_journal(tmp_path)
    coords = _coordinates(lines_out)
    assert len(coords) == len(starts) == 10
    for view, expected in ((coords.starts(), starts), (coords.ends(), ends)):
        assert len(view) == len(expected)
        assert list(view) == expected
        assert view == expected and expected == view
        assert [view[i] for i in range(len(view))] == expected
        assert [view[-i] for i in range(1, len(view) + 1)] == [
            expected[-i] for i in range(1, len(expected) + 1)]
        for cut in SLICES:
            assert list(view[cut]) == expected[cut], cut
            assert list(view[cut][1:]) == expected[cut][1:], cut
        with pytest.raises(IndexError):
            view[len(expected)]
    for i, (name, offset, line) in enumerate(lines_out):
        assert coords.line(i) == (name, offset, len(line))


def test_coordinate_end_index_matches_list_index(tmp_path):
    """Spec T4: `ends().index` agrees with `list.index` for every present
    cursor, in the whole view and in a slice, and raises `ValueError` for an
    absent cursor, one between two ends and one in an unknown segment, as
    the materialization check relies on."""
    _starts, ends, _records, lines_out = _write_journal(tmp_path)
    view = _coordinates(lines_out).ends()
    for cursor in ends:
        assert view.index(cursor) == ends.index(cursor)
    tail = view[4:]
    for cursor in ends[4:]:
        assert tail.index(cursor) == ends[4:].index(cursor)
    absent = [
        ("seg-a.jsonl", ends[0][1] + 1),       # between two ends
        ("seg-a.jsonl", ends[0][1] - 1),
        ("seg-a.jsonl", 0),
        ("seg-b.jsonl", ends[-1][1] + 100),    # past the last end
        ("seg-z.jsonl", ends[0][1]),           # unknown segment
        ("seg-a.jsonl",),                      # not a cursor
    ]
    for cursor in absent:
        with pytest.raises(ValueError):
            ends.index(cursor)
        with pytest.raises(ValueError):
            view.index(cursor)
    with pytest.raises(ValueError):
        tail.index(ends[0])


def test_coordinate_views_peak_bounded():
    """Spec T4: on 100 k records the coordinates and both views, built and
    iterated, peak under 16 bytes a record (the two tuple lists took ≈ 200).
    A view that builds a tuple list back fails here."""
    lib = _lib()
    segments = [f"observations-2026-{m:02d}.jsonl" for m in range(1, 5)]
    count = 100_000
    gc.collect()
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        coords = lib.LineCoordinates()
        offset = 0
        for n in range(count):
            coords.append(segments[n * len(segments) // count], offset, 300)
            offset += 301
        starts, ends = coords.starts(), coords.ends()
        walked = 0
        for _item in starts:
            walked += 1
        for _item in ends:
            walked += 1
        tail = ends[count // 2:]
        assert tail.index(ends[count - 1]) == count - 1 - count // 2
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert walked == 2 * count
    per_record = (peak - base) / count
    print(f"T4 coordinates peak: {per_record:.2f} bytes per record")
    assert per_record < 16, per_record


# --- T5 library part: coordinate-backed records ------------------------------


def test_retained_records_equal_todays_list(tmp_path):
    """Spec T5: the sequence equals the decoded list in iteration, indexing,
    slices, position subsets and the appended-extra view; retained objects
    come back as the same object, streamed ones as fresh private copies."""
    seq, expected, _ends, markers = _retained(tmp_path, retained_at={0, 6})
    assert len(seq) == len(expected) == 10
    assert list(seq) == expected and seq == expected
    assert [seq[i] for i in range(len(seq))] == expected
    assert seq[-1] == expected[-1]
    for cut in SLICES:
        assert list(seq[cut]) == expected[cut], cut
        assert list(seq[cut][1:]) == expected[cut][1:], cut
    picked = [1, 3, 6, 9]
    subset = seq.subset(picked)
    assert list(subset) == [expected[i] for i in picked]
    assert list(subset[1:]) == [expected[i] for i in picked[1:]]
    assert list(subset.subset([0, 2])) == [expected[1], expected[6]]
    extra = {"t": "op", "id": "reviewed"}
    viewed = seq.with_extra(extra)
    assert list(viewed) == [*expected, extra]
    assert viewed[len(expected)] is extra and viewed[-1] is extra
    assert list(viewed[8:]) == [*expected[8:], extra]
    assert list(viewed[5:][3:]) == [*expected[8:], extra]
    assert list(viewed.subset([2, 10])) == [expected[2], extra]
    assert viewed.slot(len(expected)) is extra
    assert not viewed.is_streamed(len(expected))
    # Identity: retained slots are the object; a streamed read is fresh.
    assert seq[0] is markers[0] and seq.slot(6) is markers[6]
    assert seq.slot(1) is _lib().STREAMED
    assert seq.is_streamed(1) and not seq.is_streamed(0)
    assert seq[1] == seq[1] and seq[1] is not seq[1]
    first = seq[1]
    first["mutated"] = True
    assert "mutated" not in seq[1], "a fresh decode must be private"
    seq.close()


def test_placeholder_assignment_and_slot_snapshot(tmp_path):
    """Item assignment stores a placeholder; a slot snapshot taken before it
    still decodes the original, so a placeholder can carry it in full."""
    seq, expected, _ends, _markers = _retained(tmp_path)
    originals = seq.slot_snapshot()
    placeholder = {"t": "excluded"}
    seq[4] = placeholder
    assert seq[4] is placeholder and not seq.is_streamed(4)
    assert originals[4] == expected[4]
    assert list(originals) == expected
    assert list(seq) == [*expected[:4], placeholder, *expected[5:]]


def test_normalizer_applies_to_fresh_decodes_only(tmp_path):
    """Spec D4: the installed hook normalizes every fresh decode in place,
    on indexing and on iteration; a retained object is never touched."""
    seq, expected, _ends, markers = _retained(tmp_path, retained_at={2})
    calls = []

    def normalize(record):
        calls.append(record.get("id"))
        record["account"] = "acct-n"

    seq.set_normalizer(normalize)
    out = list(seq)
    assert out[2] is markers[2] and "account" not in markers[2]
    assert all(record["account"] == "acct-n"
               for i, record in enumerate(out) if i != 2)
    assert seq[3] == {**expected[3], "account": "acct-n"}
    assert len(calls) == len(expected)  # nine in the pass, one indexed
    seq.set_normalizer(None)
    assert seq[3] == expected[3]


def test_iteration_opens_each_segment_once_per_pass(tmp_path):
    """Spec D4: a pass reads each segment once, in order, and releases its
    handle when the pass ends, even when abandoned early."""
    opened: list = []
    seq, expected, _ends, _markers = _retained(tmp_path, opened=opened)
    assert list(seq) == expected
    assert opened == ["seg-a.jsonl", "seg-b.jsonl"]
    opened.clear()
    assert list(seq[3:]) == expected[3:]
    assert opened == ["seg-a.jsonl", "seg-b.jsonl"]
    opened.clear()
    assert any(record.get("seq") == 0 for record in seq)
    assert opened == ["seg-a.jsonl"]


@pytest.mark.parametrize("change", ["rewrite", "truncate"])
def test_changed_line_raises_on_access(tmp_path, change):
    """Spec T5: after the read pass, a same-length rewrite of a retained
    line (`"weekly_percent":10` to `"weekly_percent":90`) and a truncation
    both raise the reader's error on access, by index and by iteration.
    Framing alone passes the rewrite (the line is still one framed line of
    the same length), so only the digest taken in the read pass refuses it;
    decoding it would plan from a reading the journal never recorded."""
    seq, expected, _ends, _markers = _retained(tmp_path)
    path = tmp_path / "seg-b.jsonl"
    data = path.read_bytes()
    if change == "rewrite":
        changed = data.replace(b'"weekly_percent":10', b'"weekly_percent":90',
                               1)
        assert len(changed) == len(data) and changed != data
    else:
        changed = data[:len(data) // 2]
    path.write_bytes(changed)
    assert seq[0] == expected[0], "an unchanged segment still reads"
    with pytest.raises(_Changed):
        seq[5 if change == "rewrite" else len(expected) - 1]
    with pytest.raises(_Changed):
        list(seq)
