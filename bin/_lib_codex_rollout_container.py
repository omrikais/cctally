"""Codex rollout containers (#968 spec 5.1, 5.4).

A dependency-light leaf: it imports only the standard library and nothing from
cache, journal, configuration or CLI modules. It owns

* logical normalization (``X.jsonl.zst`` -> ``X.jsonl``) — every persisted key is
  the logical path, whatever container holds the bytes;
* container selection (plain wins, else the ``.zst`` twin, else none);
* the call-time decoder chain (stdlib ``compression.zstd`` on 3.14+, then
  ``backports.zstd``, then a typed unavailable result);
* a decoded binary stream whose ``tell``/``seek`` speak DECODED offsets, so a
  compressed rollout yields exactly the offsets its plain twin would;
* container fingerprints, full-stream validation and the content anchor chain.

JSONL semantics stay in ``_lib_jsonl``; its readers receive these streams.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import stat as _stat
from typing import Callable, NamedTuple, Optional, Union

PLAIN = "plain"
ZSTD = "zstd"
PLAIN_SUFFIX = ".jsonl"
ZSTD_SUFFIX = ".jsonl.zst"
DECODE_CHUNK = 1 << 20
READ_CHUNK = 64 << 10
ANCHOR_VERSION = 1
_ANCHOR_SEED = hashlib.sha256(b"cctally-codex-content-anchor-v1\0").digest()

PathLike = Union[str, "os.PathLike[str]"]
StopPredicate = Optional[Callable[[], bool]]


class ZstdBackend(NamedTuple):
    name: str
    module: object


class ZstdUnavailable(NamedTuple):
    reason: str = "decoder-unavailable"


UNAVAILABLE = ZstdUnavailable()


def resolve_zstd_backend() -> "ZstdBackend | ZstdUnavailable":
    """Resolve the decoder NOW (U1). Never cached; never reads an env switch.

    Tests replace this module attribute with ``lambda: UNAVAILABLE``; every
    caller reaches it through the module, so the replacement is honored.
    Production code has no flag that does the same.
    """
    try:
        from compression import zstd as module  # Python 3.14+
    except ImportError:
        pass
    else:
        return ZstdBackend("stdlib", module)
    try:
        from backports import zstd as module  # type: ignore[no-redef]
    except ImportError:
        return UNAVAILABLE
    return ZstdBackend("backports.zstd", module)


def decoder_capability() -> str:
    backend = resolve_zstd_backend()
    return backend.name if isinstance(backend, ZstdBackend) else "unavailable"


class ContainerError(OSError):
    """A container could not be read; ``category`` is bounded and privacy-safe.

    Subclassing OSError keeps every existing ``except OSError`` per-file
    failure path (keep the accepted state, retry later) correct by default;
    callers that must tell a missing decoder from a missing file test for this
    class FIRST.
    """

    CATEGORIES = frozenset({
        "decoder-unavailable", "decode-error", "read-error", "unstable-stat"})

    def __init__(self, category: str):
        if category not in self.CATEGORIES:
            raise ValueError(f"unknown container error category {category!r}")
        super().__init__(category)
        self.category = category


class ContainerReadInterrupted(Exception):
    """The caller's stop predicate fired inside a read or seek.

    Nothing partial was returned: the stream position is unchanged for a seek,
    and no line was handed out for a read.
    """


def logical_path(p: PathLike):
    """Remove ONE terminal ``.zst`` from a ``*.jsonl.zst`` name; idempotent."""
    if isinstance(p, pathlib.PurePath):
        name = p.name
        return p.with_name(name[:-4]) if name.endswith(ZSTD_SUFFIX) else p
    text = os.fspath(p)
    return text[:-4] if text.endswith(ZSTD_SUFFIX) else text


def is_container_name(name: str) -> bool:
    return name.endswith(PLAIN_SUFFIX) or name.endswith(ZSTD_SUFFIX)


def zstd_twin(logical: PathLike) -> pathlib.Path:
    return pathlib.Path(os.fspath(logical_path(logical)) + ".zst")


class ContainerFingerprint(NamedTuple):
    kind: str
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int

    def watch_sig(self) -> tuple:
        """The live-tail signature: plain keeps today's size; zstd binds the
        container's identity, device included, exactly as the zstd generation
        token and every compressed container-unchanged check do (review P3-2,
        round-2 N9), so a remounted container is observed as a change once."""
        if self.kind == PLAIN:
            return (PLAIN, int(self.size))
        return (ZSTD, int(self.size), int(self.mtime_ns), int(self.device),
                int(self.inode))

    def to_json(self) -> str:
        return json.dumps(list(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text) -> "ContainerFingerprint | None":
        try:
            kind, path, size, mtime_ns, device, inode = json.loads(text)
            if kind not in (PLAIN, ZSTD):
                return None
            return cls(str(kind), str(path), int(size), int(mtime_ns),
                       int(device), int(inode))
        except (TypeError, ValueError):
            return None


def _regular_stat(path: PathLike):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if _stat.S_ISREG(st.st_mode) else None


def fingerprint_of(path: PathLike, kind: str) -> "ContainerFingerprint | None":
    st = _regular_stat(path)
    if st is None:
        return None
    return ContainerFingerprint(kind, os.fspath(path), int(st.st_size),
                                int(st.st_mtime_ns), int(st.st_dev),
                                int(st.st_ino))


class SelectedContainer(NamedTuple):
    logical: pathlib.Path
    kind: str
    path: pathlib.Path
    fingerprint: ContainerFingerprint


def select_container(logical: PathLike) -> "SelectedContainer | None":
    """Plain when it exists, otherwise the ``.zst`` twin, otherwise None (5.2)."""
    lp = pathlib.Path(logical_path(os.fspath(logical)))
    fp = fingerprint_of(lp, PLAIN)
    if fp is not None:
        return SelectedContainer(lp, PLAIN, lp, fp)
    twin = zstd_twin(lp)
    fp = fingerprint_of(twin, ZSTD)
    if fp is not None:
        return SelectedContainer(lp, ZSTD, twin, fp)
    return None


def observe_watch_sig(logical: PathLike) -> "tuple | None":
    selected = select_container(logical)
    return None if selected is None else selected.fingerprint.watch_sig()


class DecodedZstdStream:
    """A binary reader over one zstd container that speaks DECODED offsets.

    ``_buf`` holds decoded bytes ``[_buf_start, _raw_pos)``. ``readline`` keeps
    every byte from the start of the line it returned, so a torn-line rewind
    (``seek(line_start)``) is served from memory. A forward seek decodes and
    discards bounded chunks; a backward seek beyond the window restarts the
    decoder. The stop predicate is consulted between chunks inside large reads
    and seeks, and a stop raises ``ContainerReadInterrupted`` without moving
    the position.
    """

    def __init__(self, path: PathLike, backend: ZstdBackend, *,
                 should_stop: StopPredicate = None):
        self._path = os.fspath(path)
        self._backend = backend
        self._should_stop = should_stop
        self._raw = None
        self._closed = False
        self._open_raw()
        self._pos = 0

    # -- plumbing ---------------------------------------------------------
    def _open_raw(self) -> None:
        if self._raw is not None:
            self._raw.close()
        try:
            self._raw = self._backend.module.ZstdFile(self._path, "rb")
        except OSError:
            raise ContainerError("read-error") from None
        self._raw_pos = 0
        self._buf = bytearray()
        self._buf_start = 0
        self._eof = False

    def _check_stop(self) -> None:
        if self._should_stop is not None and self._should_stop():
            raise ContainerReadInterrupted()

    def _fill(self, size: int) -> int:
        if self._eof:
            return 0
        self._check_stop()
        decode_errors = (EOFError,)
        zstd_error = getattr(self._backend.module, "ZstdError", None)
        if isinstance(zstd_error, type):
            decode_errors = (EOFError, zstd_error)
        try:
            chunk = self._raw.read(min(size, DECODE_CHUNK))
        except decode_errors:
            raise ContainerError("decode-error") from None
        except OSError:
            raise ContainerError("read-error") from None
        if not chunk:
            self._eof = True
            return 0
        self._buf += chunk
        self._raw_pos += len(chunk)
        return len(chunk)

    def _drop_before(self, keep_from: int) -> None:
        keep_from = min(keep_from, self._raw_pos)
        if keep_from > self._buf_start:
            del self._buf[: keep_from - self._buf_start]
            self._buf_start = keep_from

    # -- file protocol ----------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == io.SEEK_CUR:
            offset = self._pos + offset
        elif whence != io.SEEK_SET:
            raise io.UnsupportedOperation("decoded streams seek from the start")
        if offset < 0:
            raise ValueError("negative seek position")
        saved = (self._pos,)
        if offset < self._buf_start:
            self._open_raw()
        if offset > self._raw_pos:
            self._buf = bytearray()
            self._buf_start = self._raw_pos
            while self._raw_pos < offset:
                try:
                    got = self._fill(min(DECODE_CHUNK, offset - self._raw_pos))
                except ContainerReadInterrupted:
                    if saved[0] < self._buf_start:
                        self._open_raw()
                    self._pos = saved[0]
                    raise
                if got == 0:
                    break
                self._buf = bytearray()
                self._buf_start = self._raw_pos
        self._pos = offset
        return self._pos

    def _prepare_read(self) -> None:
        # An interrupted discard may have restarted the decoder while keeping
        # the caller's old decoded position. Materialize it only when needed.
        if self._pos > self._raw_pos and not self._eof:
            self.seek(self._pos)

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        self._prepare_read()
        if self._pos >= self._raw_pos and self._eof:
            return b""
        start = self._pos - self._buf_start
        while size < 0 or len(self._buf) - start < size:
            want = DECODE_CHUNK if size < 0 else max(
                READ_CHUNK, size - (len(self._buf) - start))
            if self._fill(want) == 0:
                break
        end = len(self._buf) if size < 0 else min(len(self._buf), start + size)
        data = bytes(self._buf[start:end])
        self._pos += len(data)
        self._drop_before(self._pos)
        return data

    def readline(self, limit: int = -1) -> bytes:
        self._prepare_read()
        if self._pos >= self._raw_pos and self._eof:
            return b""
        start = self._pos - self._buf_start
        search_from = start
        bounded = limit is not None and limit >= 0
        while True:
            idx = self._buf.find(b"\n", search_from)
            if idx >= 0:
                end = idx + 1
                if bounded and end - start > limit:
                    end = start + limit
                break
            if bounded and len(self._buf) - start >= limit:
                end = start + limit
                break
            search_from = len(self._buf)
            if self._fill(READ_CHUNK) == 0:
                end = len(self._buf)
                break
        line = bytes(self._buf[start:end])
        # Retain from the start of THIS line so a torn-line rewind stays local.
        self._drop_before(self._pos)
        self._pos += len(line)
        return line

    def close(self) -> None:
        if not self._closed and self._raw is not None:
            self._raw.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_decoded(container: SelectedContainer, *, should_stop: StopPredicate = None):
    """Open the selected container as a binary stream with decoded offsets."""
    if container.kind == PLAIN:
        return open(container.path, "rb")
    backend = resolve_zstd_backend()
    if not isinstance(backend, ZstdBackend):
        raise ContainerError("decoder-unavailable")
    return DecodedZstdStream(container.path, backend, should_stop=should_stop)


class StreamValidation(NamedTuple):
    ok: bool
    decoded_length: "int | None"
    complete_line_end: "int | None"
    error: "str | None"


def validate_full_stream(container: SelectedContainer, *,
                         should_stop: StopPredicate = None) -> StreamValidation:
    """Decode the whole stream to frame completion (5.1).

    Reports the decoded length, the complete-line end (offset after the last
    newline, 0 when none) and any decoder error. A container whose stat moved
    during the pass is ``unstable-stat``. ``ContainerReadInterrupted``
    propagates to the caller, which treats it as a budget stop.
    """
    total = 0
    last_line_end = 0
    try:
        with open_decoded(container, should_stop=should_stop) as stream:
            while True:
                if should_stop is not None and should_stop():
                    raise ContainerReadInterrupted()
                chunk = stream.read(DECODE_CHUNK)
                if not chunk:
                    break
                idx = chunk.rfind(b"\n")
                if idx >= 0:
                    last_line_end = total + idx + 1
                total += len(chunk)
    except ContainerError as exc:
        return StreamValidation(False, None, None, exc.category)
    except OSError:
        return StreamValidation(False, None, None, "read-error")
    after = fingerprint_of(container.path, container.kind)
    if after != container.fingerprint:
        return StreamValidation(False, None, None, "unstable-stat")
    return StreamValidation(True, total, last_line_end, None)


def anchor_seed() -> bytes:
    return _ANCHOR_SEED


def anchor_step(prev: bytes, line: bytes) -> bytes:
    """anchor_n = SHA-256(anchor_{n-1} || SHA-256(line_n)) (5.4)."""
    return hashlib.sha256(prev + hashlib.sha256(line).digest()).digest()


def anchor_over(stream, *, start_offset: int, start_anchor: bytes,
                end_offset: int) -> "tuple[bytes, int]":
    """Fold every complete line in ``[start_offset, end_offset)``.

    Stops at a line that is not newline-terminated or would cross
    ``end_offset``; returns ``(anchor, offset reached)``.
    """
    stream.seek(start_offset)
    anchor = start_anchor
    offset = start_offset
    while offset < end_offset:
        line = stream.readline()
        if not line or not line.endswith(b"\n") or offset + len(line) > end_offset:
            break
        anchor = anchor_step(anchor, line)
        offset += len(line)
    return anchor, offset


class LineAnchorRecorder:
    """Wrap an ingest stream and fold each complete, sequential line.

    ``_lib_jsonl``'s fused iterator reads strictly sequentially and only ever
    rewinds to the start of a line it rejected (torn tail or stop point), so
    folding a complete line exactly when it starts at the folded frontier gives
    the chain over every complete raw line — malformed and non-emitting lines
    included — without storing per-line digests. ``start_anchor=None`` means
    the cursor carries no resumable anchor (a legacy row); ``result`` is then
    always None.
    """

    def __init__(self, stream, *, start_offset: int,
                 start_anchor: "bytes | None"):
        self._s = stream
        self._anchor = start_anchor
        self._folded_to = start_offset

    def tell(self) -> int:
        return self._s.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._s.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        return self._s.read(size)

    def readline(self, limit: int = -1) -> bytes:
        start = self._s.tell()
        line = self._s.readline(limit)
        if (self._anchor is not None and line.endswith(b"\n")
                and start == self._folded_to):
            self._anchor = anchor_step(self._anchor, line)
            self._folded_to = start + len(line)
        return line

    def result(self, final_offset: int) -> "bytes | None":
        if self._anchor is None or final_offset != self._folded_to:
            return None
        return self._anchor


def generation_token(fingerprint: ContainerFingerprint,
                     decoded_length: "int | None") -> str:
    """Bind a container fingerprint and its validated content evidence (5.11).

    A plain file keeps one generation while it grows in place (appends change
    the scan target, not the generation). A zstd token binds the whole
    selected fingerprint (5.1, 5.11): device, inode, size and mtime, plus the
    validated decoded length, so any change to one of them, a remount that
    renumbers st_dev included, is a new generation. That is stricter than the
    #769 S6 rule (inode decides, device corroborates), which 5.5 permits.

    The plain token stays inode-only on purpose. An unchanged plain file is
    never re-read, so its cursor's stored device is never refreshed; a device
    in the plain token would make every retained plain file look changed
    after a remount and strand the membership scan and the seal (5.11, U6).
    """
    if fingerprint.kind == PLAIN:
        return f"plain:{fingerprint.inode}"
    return (f"zstd:{fingerprint.device}:{fingerprint.inode}:"
            f"{fingerprint.size}:{fingerprint.mtime_ns}:{decoded_length}")
