"""#968 spec 5.1: the container leaf."""
from __future__ import annotations

import hashlib
import io
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _codex_zstd_fixtures import zstd_bytes  # noqa: E402

import _lib_codex_rollout_container as rc  # noqa: E402

LINES = [f'{{"n":{i},"pad":"{"x" * (i % 7)}"}}\n'.encode() for i in range(2000)]
DATA = b"".join(LINES)


def _pair(tmp_path, data=DATA, name="rollout-a.jsonl"):
    plain = tmp_path / "p" / name
    plain.parent.mkdir(parents=True)
    plain.write_bytes(data)
    zdir = tmp_path / "z"
    zdir.mkdir()
    (zdir / (name + ".zst")).write_bytes(zstd_bytes(data))
    return plain, zdir / name


def test_logical_path_strips_exactly_one_terminal_zst_and_is_idempotent():
    assert rc.logical_path("/r/s/a.jsonl.zst") == "/r/s/a.jsonl"
    assert rc.logical_path("/r/s/a.jsonl") == "/r/s/a.jsonl"
    assert rc.logical_path(rc.logical_path("/r/a.jsonl.zst")) == "/r/a.jsonl"
    assert rc.logical_path("/r/a.zst") == "/r/a.zst"
    assert rc.logical_path(pathlib.Path("/r/a.jsonl.zst")) == pathlib.Path("/r/a.jsonl")
    assert rc.is_container_name("a.jsonl") and rc.is_container_name("a.jsonl.zst")
    assert not rc.is_container_name("a.json") and not rc.is_container_name("a.zst")


def test_selection_prefers_plain_then_zstd_then_none(tmp_path):
    plain, zlogical = _pair(tmp_path)
    assert rc.select_container(plain).kind == rc.PLAIN
    selected = rc.select_container(zlogical)
    assert selected.kind == rc.ZSTD and selected.path.name.endswith(".jsonl.zst")
    assert selected.logical == zlogical
    # Both forms present: plain wins even when the twin is garbage (5.2).
    (plain.parent / (plain.name + ".zst")).write_bytes(b"not zstd")
    assert rc.select_container(plain).kind == rc.PLAIN
    assert rc.select_container(tmp_path / "missing.jsonl") is None


def test_resolver_is_call_time_and_replaceable(monkeypatch):
    assert rc.decoder_capability() in {"stdlib", "backports.zstd"}
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    assert rc.decoder_capability() == "unavailable"


def test_unavailable_decoder_is_a_typed_error(tmp_path, monkeypatch):
    _plain, zlogical = _pair(tmp_path)
    monkeypatch.setattr(rc, "resolve_zstd_backend", lambda: rc.UNAVAILABLE)
    with pytest.raises(rc.ContainerError) as info:
        rc.open_decoded(rc.select_container(zlogical))
    assert info.value.category == "decoder-unavailable"
    assert isinstance(info.value, OSError)


def test_decoded_stream_matches_plain_offsets_readline_seek_and_tell(tmp_path):
    plain, zlogical = _pair(tmp_path)
    with open(plain, "rb") as p, rc.open_decoded(rc.select_container(zlogical)) as z:
        for _ in range(50):
            assert z.tell() == p.tell()
            assert z.readline() == p.readline()
        target = len(b"".join(LINES[:1500]))           # forward beyond the buffer
        p.seek(target)
        z.seek(target)
        assert z.tell() == target and z.readline() == p.readline()
        line_start = z.tell()
        z.readline()
        z.seek(line_start)                               # torn-line rewind: in-buffer
        assert z.readline() == LINES[1501]
        z.seek(10)                                       # backward beyond buffer: restart
        p.seek(10)
        assert z.read(100) == p.read(100)
        z.seek(len(DATA))
        assert z.readline() == b"" and z.read() == b""


def test_stop_predicate_fires_inside_a_large_forward_seek(tmp_path):
    big = b"".join([b'{"k":"' + b"y" * 1000 + b'"}\n'] * 5000)  # ~5 MB decoded
    _plain, zlogical = _pair(tmp_path, data=big)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    with rc.open_decoded(rc.select_container(zlogical), should_stop=stop) as z:
        with pytest.raises(rc.ContainerReadInterrupted):
            z.seek(len(big) - 10)
        assert z.tell() == 0  # nothing partial was committed


def test_full_stream_validation_reports_length_line_end_and_errors(tmp_path):
    torn = DATA + b'{"torn":'
    _plain, zlogical = _pair(tmp_path, data=torn)
    result = rc.validate_full_stream(rc.select_container(zlogical))
    assert result.ok and result.decoded_length == len(torn)
    assert result.complete_line_end == len(DATA)
    container = rc.select_container(zlogical)
    raw = container.path.read_bytes()
    container.path.write_bytes(raw[: len(raw) // 2])     # truncated frame
    bad = rc.validate_full_stream(rc.select_container(zlogical))
    assert not bad.ok and bad.error == "decode-error"


def test_anchor_chain_is_resumable_and_bound_to_offsets(tmp_path):
    plain, _z = _pair(tmp_path)
    seed = rc.anchor_seed()
    expected = seed
    for line in LINES[:10]:
        expected = hashlib.sha256(expected + hashlib.sha256(line).digest()).digest()
    with open(plain, "rb") as fh:
        anchor, offset = rc.anchor_over(
            fh, start_offset=0, start_anchor=seed,
            end_offset=len(b"".join(LINES[:10])))
    assert anchor == expected and offset == len(b"".join(LINES[:10]))
    with open(plain, "rb") as fh:
        first, mid = rc.anchor_over(fh, start_offset=0, start_anchor=seed,
                                    end_offset=len(b"".join(LINES[:4])))
        resumed, end = rc.anchor_over(fh, start_offset=mid, start_anchor=first,
                                      end_offset=len(b"".join(LINES[:10])))
    assert resumed == expected and end == offset


def test_line_anchor_recorder_folds_only_complete_sequential_lines(tmp_path):
    plain, _z = _pair(tmp_path, data=DATA + b'{"torn":')
    with open(plain, "rb") as fh:
        rec = rc.LineAnchorRecorder(fh, start_offset=0, start_anchor=rc.anchor_seed())
        while True:
            start = rec.tell()
            line = rec.readline()
            if not line or not line.endswith(b"\n"):
                rec.seek(start)
                break
        assert rec.tell() == len(DATA)
        with open(plain, "rb") as again:
            full, _ = rc.anchor_over(again, start_offset=0,
                                     start_anchor=rc.anchor_seed(), end_offset=len(DATA))
        assert rec.result(len(DATA)) == full
        assert rec.result(len(DATA) - 1) is None
    with open(plain, "rb") as fh:
        assert rc.LineAnchorRecorder(fh, start_offset=0, start_anchor=None).result(0) is None


def test_fingerprint_and_generation_token_and_watch_sig(tmp_path):
    plain, zlogical = _pair(tmp_path)
    p = rc.select_container(plain).fingerprint
    z = rc.select_container(zlogical).fingerprint
    assert p.watch_sig() == (rc.PLAIN, len(DATA))
    assert z.watch_sig() == (rc.ZSTD, z.size, z.mtime_ns, z.device, z.inode)
    assert z._replace(device=z.device + 1).watch_sig() != z.watch_sig()
    assert rc.ContainerFingerprint.from_json(z.to_json()) == z
    assert rc.ContainerFingerprint.from_json("{not json") is None
    assert rc.generation_token(p, None).startswith("plain:")
    assert rc.generation_token(z, len(DATA)) != rc.generation_token(z, len(DATA) + 1)
    # Review P3-2: the zstd token binds the device; the plain token does not.
    assert (rc.generation_token(z._replace(device=z.device + 1), len(DATA))
            != rc.generation_token(z, len(DATA)))
    assert (rc.generation_token(p._replace(device=p.device + 1), None)
            == rc.generation_token(p, None))
    assert rc.observe_watch_sig(zlogical) == z.watch_sig()


def test_large_read_checks_cancellation_before_returning_partial_data(tmp_path):
    data = b'x' * (rc.DECODE_CHUNK * 5)
    _plain, logical = _pair(tmp_path, data=data)
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return calls > 2

    with rc.open_decoded(rc.select_container(logical), should_stop=stop) as stream:
        with pytest.raises(rc.ContainerReadInterrupted):
            stream.read(len(data))
        assert stream.tell() == 0


def test_decoder_reads_are_bounded_even_for_a_large_requested_read(tmp_path, monkeypatch):
    from types import SimpleNamespace

    data = b'x' * (rc.DECODE_CHUNK * 4)
    _plain, logical = _pair(tmp_path, data=data)
    backend = rc.resolve_zstd_backend()
    requests = []

    class Reader:
        def __init__(self, *args):
            self.raw = backend.module.ZstdFile(*args)

        def read(self, size):
            requests.append(size)
            return self.raw.read(size)

        def close(self):
            self.raw.close()

    module = SimpleNamespace(ZstdFile=Reader, ZstdError=backend.module.ZstdError)
    monkeypatch.setattr(rc, 'resolve_zstd_backend', lambda: rc.ZstdBackend(backend.name, module))
    with rc.open_decoded(rc.select_container(logical)) as stream:
        assert stream.read(len(data)) == data
    assert requests and max(requests) <= rc.DECODE_CHUNK


def test_interrupted_seek_retains_nonzero_position_and_readable_bytes(tmp_path):
    data = b'line\n' + b'x' * (rc.DECODE_CHUNK * 5)
    _plain, logical = _pair(tmp_path, data=data)
    calls = 0
    enabled = True

    def stop():
        nonlocal calls
        calls += 1
        return enabled and calls > 2

    with rc.open_decoded(rc.select_container(logical), should_stop=stop) as stream:
        assert stream.readline() == b'line\n'
        before = stream.tell()
        with pytest.raises(rc.ContainerReadInterrupted):
            stream.seek(len(data) - 1)
        assert stream.tell() == before
        enabled = False
        assert stream.read(100) == data[before:before + 100]


def test_large_line_cancellation_keeps_the_line_start(tmp_path):
    data = b'first\n' + b'x' * (rc.DECODE_CHUNK * 4) + b'\n'
    _plain, logical = _pair(tmp_path, data=data)
    calls = 0
    enabled = True

    def stop():
        nonlocal calls
        calls += 1
        return enabled and calls > 2

    with rc.open_decoded(rc.select_container(logical), should_stop=stop) as stream:
        assert stream.readline() == b'first\n'
        with pytest.raises(rc.ContainerReadInterrupted):
            stream.readline()
        assert stream.tell() == len(b'first\n')
        enabled = False
        assert stream.readline() == data[len(b'first\n'):]


@pytest.mark.parametrize('suffix', [b'not a frame', None])
def test_validation_checks_every_concatenated_frame_and_trailing_bytes(tmp_path, suffix):
    _plain, logical = _pair(tmp_path)
    selected = rc.select_container(logical)
    second = zstd_bytes(DATA)
    selected.path.write_bytes(selected.path.read_bytes() +
                              (second[:-1] if suffix is None else suffix))
    result = rc.validate_full_stream(rc.select_container(logical))
    assert not result.ok and result.error == 'decode-error'


def test_validation_unavailable_unstable_and_cancelled_are_distinct(tmp_path, monkeypatch):
    _plain, logical = _pair(tmp_path)
    selected = rc.select_container(logical)
    with monkeypatch.context() as patch:
        patch.setattr(rc, 'resolve_zstd_backend', lambda: rc.UNAVAILABLE)
        assert rc.validate_full_stream(selected).error == 'decoder-unavailable'
    with monkeypatch.context() as patch:
        patch.setattr(rc, 'fingerprint_of', lambda *args: selected.fingerprint._replace(size=1))
        assert rc.validate_full_stream(selected).error == 'unstable-stat'
    with pytest.raises(rc.ContainerReadInterrupted):
        rc.validate_full_stream(selected, should_stop=lambda: True)


def test_anchor_excludes_torn_or_boundary_crossing_lines():
    data = b'bad json\n' + b'{"ok":true}\n' + b'torn'
    first = rc.anchor_step(rc.anchor_seed(), b'bad json\n')
    assert rc.anchor_over(io.BytesIO(data), start_offset=0,
                          start_anchor=rc.anchor_seed(), end_offset=10) == (first, 9)
    expected = rc.anchor_step(first, b'{"ok":true}\n')
    assert rc.anchor_over(io.BytesIO(data), start_offset=0,
                          start_anchor=rc.anchor_seed(), end_offset=len(data)) == (expected, 21)
