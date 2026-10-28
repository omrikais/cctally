"""#997 T-C1 — the prefix hash streams instead of buffering each segment (D12, T12).

`PrefixHashAccumulator` used to buffer the whole segment being read in a
`bytearray` grown by `extend`. The maintainer's largest segment is 410 MB, and
every growth step freed the previous buffer: the macOS allocator kept about
2 GB of those freed large blocks resident during the read pass. The
accumulator now frames each segment with the byte count the pass will feed and
hashes every chunk as it arrives. A digest anywhere other than a fully fed
segment end re-hashes that segment's prefix through the caller's opener, which
in production is the journal's one segment-read seam.

The durable digest is recorded inside `journal_protocol_resolution` and
reviewed-decision payloads, so every assertion here compares against an
INDEPENDENT copy of `journal_prefix_hash`'s framing (or a literal), never
against the code under test.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import pathlib
import tracemalloc

import pytest

import journal_fixture_496_s5 as S5
from conftest import load_script, redirect_paths


def _router():
    return importlib.import_module("_lib_journal_router")


def _reference(frames) -> str:
    """`journal_prefix_hash`'s framing, written out independently."""
    digest = hashlib.sha256()
    for name, data in frames:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


class _Files:
    """An in-memory opener that records every open by name."""

    def __init__(self, files):
        self.files = dict(files)
        self.opened: list = []

    def __call__(self, name):
        self.opened.append(name)
        return io.BytesIO(self.files[name])


def _feed(acc, name, data, *, boundary=None, size=None, chunk=3):
    acc.begin_segment(name, boundary, size=len(data) if size is None else size)
    for start in range(0, len(data), chunk):
        acc.extend(data[start:start + chunk])


# --------------------------------------------------------------------------
# The accumulator against the reference framing
# --------------------------------------------------------------------------


def test_streamed_digest_matches_reference():
    a = b"one\ntwo\n"
    b = b"three\nfour\nfive\n"
    torn = b"six\nseven-without-newline"
    files = _Files({"a.jsonl": a, "b.jsonl": b, "c.jsonl": torn,
                    "e.jsonl": b""})
    R = _router()

    # Full end of the segment being streamed: no re-read.
    acc = R.PrefixHashAccumulator(open_segment=files)
    _feed(acc, "a.jsonl", a)
    assert acc.digest_at(("a.jsonl", len(a))) == _reference([("a.jsonl", a)])
    assert files.opened == []

    # Mid-segment: re-read through the opener.
    assert acc.digest_at(("a.jsonl", 4)) == _reference([("a.jsonl", a[:4])])
    assert files.opened == ["a.jsonl"]

    # Spanning completed segments, at the current segment's full end and
    # inside it.
    files.opened.clear()
    _feed(acc, "b.jsonl", b, boundary=("a.jsonl", len(a)))
    assert acc.digest_at(("b.jsonl", len(b))) == _reference(
        [("a.jsonl", a), ("b.jsonl", b)])
    assert acc.digest_at(("b.jsonl", 6)) == _reference(
        [("a.jsonl", a), ("b.jsonl", b[:6])])
    assert files.opened == ["b.jsonl"]

    # The previous segment's full end, served from the registered boundary.
    files.opened.clear()
    _feed(acc, "e.jsonl", b"", boundary=("b.jsonl", len(b)))
    assert acc.digest_at(("b.jsonl", len(b))) == _reference(
        [("a.jsonl", a), ("b.jsonl", b)])
    assert files.opened == []

    # An empty segment is framed.
    _feed(acc, "c.jsonl", torn, boundary=None)
    assert acc.digest_at(("c.jsonl", 4)) == _reference(
        [("a.jsonl", a), ("b.jsonl", b), ("e.jsonl", b""),
         ("c.jsonl", torn[:4])])

    # A boundary short of a torn trailing line, read after the transition.
    files.opened.clear()
    acc.begin_segment("d.jsonl", ("c.jsonl", 4), size=0)
    assert acc.digest_at(("c.jsonl", 4)) == _reference(
        [("a.jsonl", a), ("b.jsonl", b), ("e.jsonl", b""),
         ("c.jsonl", torn[:4])])
    assert files.opened == ["c.jsonl"]
    # The torn segment was absorbed whole (its streamed bytes), exactly as
    # `journal_prefix_hash` frames a prior segment at its full size.
    assert acc.digest_at(("d.jsonl", 0)) == _reference(
        [("a.jsonl", a), ("b.jsonl", b), ("e.jsonl", b""),
         ("c.jsonl", torn), ("d.jsonl", b"")])

    # `None` stays `None`.
    assert acc.digest_at(None) is None


def test_short_feed_absorbs_only_the_bytes_fed():
    """A feed that ended short of its declared size (the segment shrank under
    the pass) frames the bytes actually fed, as the buffered form did."""
    data = b"one\ntwo\n"
    files = _Files({"a.jsonl": data, "b.jsonl": b"x\n"})
    acc = _router().PrefixHashAccumulator(open_segment=files)
    acc.begin_segment("a.jsonl", size=100)
    acc.extend(data)
    assert acc.digest_at(("a.jsonl", len(data))) == _reference(
        [("a.jsonl", data)])
    _feed(acc, "b.jsonl", b"x\n", boundary=("a.jsonl", len(data)))
    assert acc.digest_at(("b.jsonl", 2)) == _reference(
        [("a.jsonl", data), ("b.jsonl", b"x\n")])
    assert acc.digest_at(("a.jsonl", len(data))) == _reference(
        [("a.jsonl", data)])


def test_short_reread_raises():
    files = _Files({"a.jsonl": b"one\n"})
    acc = _router().PrefixHashAccumulator(open_segment=files)
    _feed(acc, "a.jsonl", b"one\ntwo\n")
    with pytest.raises(_router().PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 6))


def test_unstreamed_prefix_raises():
    files = _Files({"a.jsonl": b"one\n", "b.jsonl": b"two\n"})
    R = _router()
    acc = R.PrefixHashAccumulator(open_segment=files)
    _feed(acc, "a.jsonl", b"one\n")
    _feed(acc, "b.jsonl", b"two\n", boundary=("a.jsonl", 4))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 2))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("never.jsonl", 0))


def test_missing_opener_raises():
    R = _router()
    acc = R.PrefixHashAccumulator()
    _feed(acc, "a.jsonl", b"one\ntwo\n")
    # A full-end digest needs no re-read, so no opener is needed for it.
    assert acc.digest_at(("a.jsonl", 8)) == _reference(
        [("a.jsonl", b"one\ntwo\n")])
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 4))


def test_size_is_required():
    acc = _router().PrefixHashAccumulator()
    with pytest.raises(TypeError):
        acc.begin_segment("a.jsonl")


def test_counters_keep_their_meaning():
    files = _Files({"a.jsonl": b"one\ntwo\n"})
    acc = _router().PrefixHashAccumulator(open_segment=files)
    _feed(acc, "a.jsonl", b"one\ntwo\n")
    assert (acc.bytes_hashed, acc.digests_computed) == (0, 0)
    acc.digest_at(("a.jsonl", 4))
    assert (acc.bytes_hashed, acc.digests_computed) == (4, 1)
    acc.digest_at(("a.jsonl", 8))
    assert (acc.bytes_hashed, acc.digests_computed) == (12, 2)


def test_accumulator_retains_no_segment_bytes():
    """64 MiB fed in 256 KiB chunks; the buffered form held all of it."""
    chunk = b"x" * (256 * 1024)
    count = 256
    acc = _router().PrefixHashAccumulator()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        acc.begin_segment("big.jsonl", size=len(chunk) * count)
        for _ in range(count):
            acc.extend(chunk)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024, peak
    expected = hashlib.sha256()
    name = b"big.jsonl"
    expected.update(len(name).to_bytes(4, "big"))
    expected.update(name)
    expected.update((len(chunk) * count).to_bytes(8, "big"))
    for _ in range(count):
        expected.update(chunk)
    assert acc.digest_at(("big.jsonl", len(chunk) * count)) == (
        "sha256:" + expected.hexdigest())


# --------------------------------------------------------------------------
# Over a real journal, through the segment-read seam
# --------------------------------------------------------------------------


#: Captured from the pre-change implementation (`test_segment_reads_496_s5`).
FIXTURE_PREFIX_HASH = (
    "sha256:092e517cd9560221590ff91264300a94f4895fe914ff8f55970cadc51b8ed472"
)


@pytest.fixture
def tainted(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = importlib.import_module("_cctally_journal")
    built = S5.build_tainted(core.APP_DIR, seed_cache=False)
    real = jr._open_segment_for_read
    opened: list = []

    def record(seg_path):
        opened.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return {"built": built, "opened": opened, "jr": jr, "core": core}


def _drive(jr, high_water, wanted):
    """Run the production accumulator over a real pass and read `wanted`
    at the moment the pass reaches it."""
    hasher = jr._new_prefix_hasher()
    state = {"prior": None}
    out = {}
    for segment, offset, raw in jr._iter_range_with_segments(
        None, high_water, jr.list_segments(),
        on_segment=lambda name, size: hasher.begin_segment(
            name, state["prior"], size=size),
        on_bytes=hasher.extend,
    ):
        if state["prior"] in wanted:
            out[state["prior"]] = hasher.digest_at(state["prior"])
        state["prior"] = (segment, offset + len(raw) + 1)
    if high_water in wanted:
        out[high_water] = hasher.digest_at(high_water)
    return out


def _resolution_points(jr, high_water):
    """The prior high-water of every protocol-resolution op, in order."""
    jl = importlib.import_module("_lib_journal")
    points = []
    prior = None
    for segment, offset, raw in jr.iter_range(None, high_water):
        record = jl.decode_line(raw)
        if (record is not None and record.get("t") == "op"
                and (record.get("payload") or {}).get("kind")
                in jl.PROTOCOL_RESOLUTION_KINDS):
            points.append(prior)
        prior = (segment, offset + len(raw) + 1)
    return points


def test_fixture_prefix_hash_literal_unchanged(tainted):
    jr, built = tainted["jr"], tainted["built"]
    hw = built["high_water"]
    assert _drive(jr, hw, {hw})[hw] == FIXTURE_PREFIX_HASH
    assert jr.journal_prefix_hash(hw) == FIXTURE_PREFIX_HASH


def test_mid_segment_digest_rereads_through_seam(tainted):
    """One extra open, of exactly the segment holding the evidence point."""
    jr, opened = tainted["jr"], tainted["opened"]
    hw = tainted["built"]["high_water"]
    journal_dir = tainted["core"].JOURNAL_DIR
    mid_b, end_b = _resolution_points(jr, hw)
    assert mid_b[0] == S5.SEG_B and end_b[0] == S5.SEG_B
    assert mid_b[1] < end_b[1] == (journal_dir / S5.SEG_B).stat().st_size
    opened.clear()
    digests = _drive(jr, hw, {mid_b})
    assert digests[mid_b] == S5._prefix_digest(journal_dir, mid_b)
    assert opened == [S5.SEG_A, S5.SEG_B, S5.SEG_B, S5.SEG_C]


def _append_reviewed_decision_mid_segment(core, jl, built):
    """A valid retained reviewed decision inside segment C, followed by one
    more line so its evidence point is not the segment's end."""
    journal_dir = pathlib.Path(core.JOURNAL_DIR)
    rederive = importlib.import_module("_cctally_rederive")
    point = (S5.SEG_C, (journal_dir / S5.SEG_C).stat().st_size)
    digest = S5._prefix_digest(journal_dir, point)
    payload = {
        "kind": rederive._REVIEWED_WEEKLY_KIND,
        "schema_version": 2,
        "journal_high_water": {"segment": point[0], "offset": point[1]},
        "journal_prefix_hash": digest,
        "reason": "fixture review",
        "weekly_axis_decisions": [{
            "observationId": built["records"][0]["id"],
            "disposition": "hold",
        }],
        "snapshot_identity_decisions": [],
        "expected_baseline_plan_hash": "sha256:" + "0" * 64,
        "expected_decision_plan_hash": "sha256:" + "1" * 64,
    }
    op = jl.make_op(at=S5.LATER, src="rederive", payload=payload,
                    writer_generation=None)
    S5._append(journal_dir, S5.SEG_C, [op, S5._claude_obs(S5.LATER, 27.0)])
    return point, digest


def test_evidence_digests_equal_across_four_passes(tainted, monkeypatch):
    """The rebuild, completion, rederive and journal-repair passes take each
    evidence digest from the streamed accumulator, and every one equals the
    independent framing."""
    jr, core = tainted["jr"], tainted["core"]
    jl = importlib.import_module("_lib_journal")
    built = tainted["built"]
    journal_dir = pathlib.Path(core.JOURNAL_DIR)
    reviewed_point, reviewed_digest = _append_reviewed_decision_mid_segment(
        core, jl, built)
    hw = jr.journal_high_water()
    resolution_points = set(_resolution_points(jr, hw))
    assert len(resolution_points) == 2

    R = _router()
    taken: list = []
    real_digest_at = R.PrefixHashAccumulator.digest_at

    def spy(self, high_water):
        digest = real_digest_at(self, high_water)
        taken.append((high_water, digest))
        return digest

    monkeypatch.setattr(R.PrefixHashAccumulator, "digest_at", spy)

    def check(label, expected_points):
        seen = {hw_ for hw_, _digest in taken if hw_ is not None}
        assert expected_points <= seen, (label, seen)
        for point, digest in taken:
            if point is None:
                continue
            assert digest == S5._prefix_digest(journal_dir, point), (
                label, point)
        taken.clear()

    # journal repair
    repair = importlib.import_module("_cctally_journal_repair")
    repair._read_prefix(hw)
    check("journal-repair", resolution_points | {hw})

    # rederive (the only pass that validates the reviewed decision)
    rederive = importlib.import_module("_cctally_rederive")
    rederive.read_rederive_journal_prefix(hw)
    check("rederive", resolution_points | {reviewed_point})
    assert S5._prefix_digest(journal_dir, reviewed_point) == reviewed_digest

    # completion
    completion = importlib.import_module("_cctally_completion")
    prefix = completion._read_prefix((S5.SEG_A, 0), hw, None)
    assert len(prefix.evidence) == 2
    check("completion", resolution_points)

    # the final rebuild's read pass
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    check("rebuild", resolution_points)


# --------------------------------------------------------------------------
# Consumed extent (#997 D12, revision 10): every re-read is verified against
# the bytes the pass actually consumed
# --------------------------------------------------------------------------


def test_short_segment_reframed_from_verified_consumed_bytes():
    """`begin(a, size=8) -> feed(4) -> begin(b)`: `a` is re-framed from its
    four consumed bytes, which the opener still holds unchanged."""
    files = _Files({"a.jsonl": b"one\n", "b.jsonl": b"x\n"})
    acc = _router().PrefixHashAccumulator(open_segment=files)
    acc.begin_segment("a.jsonl", size=8)
    acc.extend(b"one\n")
    _feed(acc, "b.jsonl", b"x\n", boundary=("a.jsonl", 4))
    assert files.opened == ["a.jsonl"]
    assert acc.digest_at(("b.jsonl", 2)) == _reference(
        [("a.jsonl", b"one\n"), ("b.jsonl", b"x\n")])
    assert acc.digest_at(("a.jsonl", 4)) == _reference([("a.jsonl", b"one\n")])


def test_short_segment_replaced_bytes_poisons():
    """The same sequence, but an appender truncated `a` and appended after
    the pass fed it: the re-frame fails verification, so the accumulator is
    poisoned and every later digest raises."""
    files = _Files({"a.jsonl": b"one\n", "b.jsonl": b"x\ny\n"})
    R = _router()
    acc = R.PrefixHashAccumulator(open_segment=files)
    acc.begin_segment("a.jsonl", size=8)
    acc.extend(b"one\n")
    files.files["a.jsonl"] = b"on" + b"E\nappended\n"
    _feed(acc, "b.jsonl", b"x\ny\n", boundary=("a.jsonl", 4))
    assert files.opened == ["a.jsonl"], "the re-frame was attempted"
    for point in (("b.jsonl", 4), ("b.jsonl", 2), ("a.jsonl", 4)):
        with pytest.raises(R.PrefixEvidenceUnavailable):
            acc.digest_at(point)
    # Still poisoned after the next transition.
    _feed(acc, "c.jsonl", b"z\n", boundary=("b.jsonl", 4))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("c.jsonl", 2))
    assert acc.digest_at(None) is None


def test_short_segment_unavailable_reread_poisons():
    """A re-frame with no opener, or whose segment is gone, poisons too."""
    R = _router()
    acc = R.PrefixHashAccumulator()
    acc.begin_segment("a.jsonl", size=8)
    acc.extend(b"one\n")
    _feed(acc, "b.jsonl", b"x\n")
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("b.jsonl", 2))

    def missing(name):
        raise FileNotFoundError(name)

    acc = R.PrefixHashAccumulator(open_segment=missing)
    acc.begin_segment("a.jsonl", size=8)
    acc.extend(b"one\n")
    _feed(acc, "b.jsonl", b"x\n")
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("b.jsonl", 2))


def test_digest_beyond_consumed_raises():
    """The opener holds bytes the pass never consumed; they are never
    certified, in the current segment or at a registered boundary."""
    files = _Files({"a.jsonl": b"one\ntwo\n", "b.jsonl": b"x\n"})
    R = _router()
    acc = R.PrefixHashAccumulator(open_segment=files)
    acc.begin_segment("a.jsonl", size=8)
    acc.extend(b"one\n")
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 8))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 6))
    # At the consumed end it is certified, from the verified re-read.
    assert acc.digest_at(("a.jsonl", 4)) == _reference([("a.jsonl", b"one\n")])
    _feed(acc, "b.jsonl", b"x\n", boundary=("a.jsonl", 6))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", 6))
    # A fully fed segment: one past its end is beyond the consumed bytes.
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("b.jsonl", 3))


def test_replaced_torn_tail_never_certified():
    """A torn tail the pass consumed is truncated and replaced by an
    appender before the digest re-reads it. No digest that needs a re-read
    of that segment is certified: not at the consumed end of a short feed,
    not short of the torn line in the current segment, and not at the
    boundary registered short of it."""
    head = b"one\n"
    torn = b"torn-tail"
    R = _router()

    files = _Files({"a.jsonl": head + torn})
    acc = R.PrefixHashAccumulator(open_segment=files)
    acc.begin_segment("a.jsonl", size=len(head + torn) + 5)
    acc.extend(head)
    acc.extend(torn)
    files.files["a.jsonl"] = head + b"TORN-TAIL\n"
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", len(head + torn)))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", len(head)))

    files = _Files({"a.jsonl": head + torn, "b.jsonl": b"x\n"})
    acc = R.PrefixHashAccumulator(open_segment=files)
    _feed(acc, "a.jsonl", head + torn)
    files.files["a.jsonl"] = head + b"TORN-TAIL\n"
    _feed(acc, "b.jsonl", b"x\n", boundary=("a.jsonl", len(head)))
    with pytest.raises(R.PrefixEvidenceUnavailable):
        acc.digest_at(("a.jsonl", len(head)))
    # Non-vacuity: the unchanged tail is certified on the same path.
    files.files["a.jsonl"] = head + torn
    assert acc.digest_at(("a.jsonl", len(head))) == _reference(
        [("a.jsonl", head)])


class _InterleavedHandle:
    """A segment handle that runs `hook` once, right after its first read."""

    def __init__(self, handle, hook):
        self._handle = handle
        self._hook = hook

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, *args):
        data = self._handle.read(*args)
        hook, self._hook = self._hook, None
        if hook is not None:
            hook()
        return data


@pytest.fixture
def straddle(tmp_path, monkeypatch):
    """One segment whose torn tail an appender truncates and replaces while
    the real reader is between two reads, so the reader assembles a line
    from old and new bytes, followed by a protocol-resolution op."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    journal_dir = pathlib.Path(core.JOURNAL_DIR)
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / S5.SEG_A
    head = jl.encode_line(S5._claude_obs(S5.AT, 10.0))
    torn = b'{"t":"obs","torn":"' + b"X" * 200
    op = jl.encode_line({
        "v": 1, "t": "op", "id": "op:after-assembled-line", "at": S5.LATER,
        "src": "journal-repair",
        "payload": {"kind": sorted(jl.PROTOCOL_RESOLUTION_KINDS)[0]},
    })
    replacement = (b"Y" * 16 + b"\n" + op
                   + jl.encode_line(S5._claude_obs(S5.LATER, 11.0)))
    first_read = len(head) + 8
    # The first read ends eight bytes into the torn line.
    monkeypatch.setattr(jr, "_SEGMENT_READ_CHUNK", first_read)
    high_water = (S5.SEG_A, len(head) + len(replacement))
    assembled = torn[:8] + replacement[8:16]
    evidence_point = (S5.SEG_A, len(head) + 17)

    def replace_torn_tail():
        with open(path, "r+b") as handle:
            handle.truncate(len(head))
            handle.seek(len(head))
            handle.write(replacement)

    state = {"armed": False}
    real_open = jr._open_segment_for_read

    def opener(seg_path):
        if state["armed"]:
            state["armed"] = False
            # Unbuffered, as production reads are in effect: a 256 KiB read
            # exceeds the 8 KiB buffer and goes straight to the file, while a
            # buffered handle over this small fixture would read the whole
            # original file ahead and never see the replacement.
            return _InterleavedHandle(
                open(seg_path, "rb", buffering=0), replace_torn_tail)
        return real_open(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", opener)

    def arm():
        path.write_bytes(head + torn)
        state["armed"] = True

    return {"jr": jr, "jl": jl, "arm": arm, "high_water": high_water,
            "assembled": assembled, "evidence_point": evidence_point,
            "journal_dir": journal_dir}


def test_line_assembled_across_truncation_never_certified(straddle):
    """Through the real reader: the evidence digest of the op that follows a
    line assembled from old and replacement bytes raises; it is never
    certified, by the streaming loop or by the journal-repair pass."""
    jr, jl = straddle["jr"], straddle["jl"]
    hw = straddle["high_water"]
    R = _router()

    straddle["arm"]()
    hasher = jr._new_prefix_hasher()
    state = {"prior": None}
    raws: list = []
    evidence: list = []
    with pytest.raises(R.PrefixEvidenceUnavailable):
        for segment, offset, raw in jr._iter_range_with_segments(
            None, hw, jr.list_segments(),
            on_segment=lambda name, size: hasher.begin_segment(
                name, state["prior"], size=size),
            on_bytes=hasher.extend,
        ):
            raws.append(raw)
            record = jl.decode_line(raw)
            if record is not None:
                jr._capture_protocol_prefix_evidence(
                    record, state["prior"], evidence, hasher=hasher)
            state["prior"] = (segment, offset + len(raw) + 1)
    # Non-vacuity: the reader really assembled a line across the truncation,
    # and the op that follows it was reached with that line's end as its
    # evidence point.
    assert straddle["assembled"] in raws, raws
    assert state["prior"] == straddle["evidence_point"]
    assert evidence == []

    straddle["arm"]()
    repair = importlib.import_module("_cctally_journal_repair")
    with pytest.raises(R.PrefixEvidenceUnavailable):
        repair._read_prefix(hw)
