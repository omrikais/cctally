"""Codex rollout containers (#968 spec 5.1, 5.4).

A dependency-light leaf: it imports only the standard library and nothing from
cache, journal, configuration or CLI modules. It owns

* logical normalization (``X.jsonl.zst`` -> ``X.jsonl``) — every persisted key is
  the logical path, whatever container holds the bytes;
* container selection (plain wins, else the ``.zst`` twin, else — when the
  caller supplies an archive directory (#970) — the archived plain, then the
  archived ``.zst``, else none), plus a strict tri-state presence probe;
* native-home recognition and the pure archive-name mapping (#970 spec 5.1,
  5.2). The leaf never guesses a layout: an archive location is used only when
  a caller passes it explicitly;
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
import datetime as _dt
import errno as _errno
import os
import pathlib
import re
import stat as _stat
from typing import Callable, Iterable, NamedTuple, Optional, Union

PLAIN = "plain"
ZSTD = "zstd"
DATED = "dated"
ARCHIVE = "archive"
SESSIONS_DIR = "sessions"
ARCHIVE_DIR = "archived_sessions"
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

    def watch_sig(self, location: str = DATED) -> tuple:
        """The live-tail signature: plain keeps today's size; zstd binds the
        container's identity, device included, exactly as the zstd generation
        token and every compressed container-unchanged check do (review P3-2,
        round-2 N9), so a remounted container is observed as a change once.

        #970 A8: a container outside the dated location carries its location
        as a trailing element, so a pure archive move (same size and stat
        identity) is observed once. A dated container keeps today's shape
        exactly, so every committed signature stays valid.
        """
        if self.kind == PLAIN:
            sig = (PLAIN, int(self.size))
        else:
            sig = (ZSTD, int(self.size), int(self.mtime_ns), int(self.device),
                   int(self.inode))
        return sig if location == DATED else sig + (str(location),)

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
    #: #970: ``"dated"`` (beside the logical path) or ``"archive"`` (the
    #: caller-supplied archive directory). Trailing with a default, so every
    #: four-field construction stays valid.
    location: str = DATED


class CodexLocations(NamedTuple):
    """One configured Codex root's container locations (#970 spec 5.1).

    ``walk_root`` is the directory discovery walks and every logical path lives
    under; ``archive_root`` is a native home's flat archive directory, or None
    for a direct directory. Either may not exist yet.
    """

    walk_root: pathlib.Path
    archive_root: "pathlib.Path | None"


def home_locations(configured: PathLike) -> "CodexLocations | None":
    """Native-home recognition (#970 spec 5.1, A7).

    A configured directory with a ``sessions/`` or an ``archived_sessions/``
    child is a native home: ``walk_root = <home>/sessions`` and
    ``archive_root = <home>/archived_sessions``, either possibly missing, so
    the first unarchive (which creates ``sessions/``) never changes an
    identity. Any other existing directory is walked directly with no archive.
    Anything else is not a root.
    """
    home = pathlib.Path(configured)
    sessions, archive = home / SESSIONS_DIR, home / ARCHIVE_DIR
    try:
        native = sessions.is_dir() or archive.is_dir()
    except OSError:
        native = False
    if native:
        return CodexLocations(sessions, archive)
    try:
        if home.is_dir():
            return CodexLocations(home, None)
    except OSError:
        pass
    return None


def release_walked_archives(
        locations: "Iterable[CodexLocations]") -> "list[CodexLocations]":
    """Drop each archive another configured root already walks (#970 N4).

    ``CODEX_HOME=<home>,<home>/archived_sessions`` names one directory twice:
    as the home's archive and as a direct root. The direct root spells its
    identities ``<archive>/X.jsonl``, which the home would read as a retained
    direct-era spelling and hold its dated twins forever. A native home whose
    canonical archive equals, or lies inside, another location's canonical
    walk root therefore gets ``archive_root=None``: the root that walks the
    directory keeps it, exactly as without #970. Order and every other field
    are kept; canonicalization follows symlinks without requiring existence.
    """
    locations = list(locations)

    def canonical(path):
        try:
            return pathlib.Path(os.path.realpath(path))
        except (OSError, ValueError):
            return pathlib.Path(os.path.abspath(path))

    walks = [canonical(location.walk_root) for location in locations]
    out: "list[CodexLocations]" = []
    for index, location in enumerate(locations):
        if location.archive_root is not None:
            archive = canonical(location.archive_root)
            if any(other != index and (archive == walk or walk in archive.parents)
                   for other, walk in enumerate(walks)):
                location = location._replace(archive_root=None)
        out.append(location)
    return out


# The UUID shape `_lib_jsonl._CODEX_FILENAME_UUID_RE` uses; duplicated here
# because the leaf imports only the standard library.
_ARCHIVE_NAME_RE = re.compile(
    r"rollout-(\d{4})-(\d{2})-(\d{2})T\d{2}-\d{2}-\d{2}-[0-9a-fA-F-]{36}"
    r"\.jsonl(?:\.zst)?")


def archive_name_date(name: str) -> "tuple[str, str, str] | None":
    """``(YYYY, MM, DD)`` of a complete rollout basename, else None (#970 5.2).

    Only ``rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl[.zst]`` with a valid
    calendar date qualifies. The date is the filename's literal date: no
    timezone conversion.
    """
    match = _ARCHIVE_NAME_RE.fullmatch(str(name))
    if match is None:
        return None
    year, month, day = match.group(1), match.group(2), match.group(3)
    try:
        _dt.date(int(year), int(month), int(day))
    except ValueError:
        return None
    return year, month, day


def archive_dir_for(logical: PathLike,
                    locations: "Iterable[CodexLocations]") -> "pathlib.Path | None":
    """The archive directory whose containers belong to ``logical`` (#970 G2).

    Pure path arithmetic, no I/O: only a logical path that is exactly
    ``walk_root/YYYY/MM/DD/<name>.jsonl`` of a location with an archive, and
    whose name's date is that ``YYYY/MM/DD``, has archive candidates.
    """
    lp = pathlib.Path(logical_path(os.fspath(logical)))
    if not lp.name.endswith(PLAIN_SUFFIX):
        return None
    date = archive_name_date(lp.name)
    if date is None:
        return None
    day = lp.parent
    if (day.parent.parent.name, day.parent.name, day.name) != date:
        return None
    base = day.parent.parent.parent
    for location in locations:
        if location.archive_root is not None and base == location.walk_root:
            return location.archive_root
    return None


def dated_logical_for_archive(
        container: PathLike,
        locations: "Iterable[CodexLocations]") -> "pathlib.Path | None":
    """Map ``archive_root/<name>[.zst]`` to ``walk_root/YYYY/MM/DD/<name>``.

    None for an unmappable name or a path that is not a direct child of any
    location's archive directory (archive discovery is flat).
    """
    cp = pathlib.Path(container)
    date = archive_name_date(cp.name)
    if date is None:
        return None
    for location in locations:
        if location.archive_root is not None and cp.parent == location.archive_root:
            return location.walk_root.joinpath(*date, logical_path(cp.name))
    return None


def container_candidates(
        logical: PathLike, *, archive_dir: "PathLike | None" = None,
) -> "list[tuple[str, str, pathlib.Path]]":
    """``(kind, location, path)`` in selection order (#970 G2)."""
    lp = pathlib.Path(logical_path(os.fspath(logical)))
    out = [(PLAIN, DATED, lp), (ZSTD, DATED, zstd_twin(lp))]
    if archive_dir is not None:
        archive = pathlib.Path(archive_dir)
        out.append((PLAIN, ARCHIVE, archive / lp.name))
        out.append((ZSTD, ARCHIVE, archive / (lp.name + ".zst")))
    return out


def select_container(logical: PathLike, *,
                     archive_dir: "PathLike | None" = None,
                     ) -> "SelectedContainer | None":
    """The first regular container in selection order, else None.

    Plain when it exists, otherwise the ``.zst`` twin (#968 5.2); with an
    explicit ``archive_dir``, then ``archive_dir/<name>.jsonl`` and
    ``archive_dir/<name>.jsonl.zst`` (#970 5.2). Lenient: a stat error reads as
    absent. Destructive callers use ``probe_containers`` instead.
    """
    lp = pathlib.Path(logical_path(os.fspath(logical)))
    for kind, location, path in container_candidates(lp, archive_dir=archive_dir):
        fp = fingerprint_of(path, kind)
        if fp is not None:
            return SelectedContainer(lp, kind, path, fp, location)
    return None


_ABSENT_ERRNOS = frozenset({_errno.ENOENT, _errno.ENOTDIR})


def probe_containers(logical: PathLike, *,
                     archive_dir: "PathLike | None" = None,
                     ) -> "tuple[str, SelectedContainer | None]":
    """Strict tri-state presence (#970 spec 5.2, A4).

    ``("present", selected)`` for the first regular candidate in selection
    order; ``("absent", None)`` only when EVERY candidate's stat failed with
    ``ENOENT``/``ENOTDIR``; ``("unknown", None)`` otherwise — any other stat
    error (an unreadable directory) or a candidate that exists but is not a
    regular file. Absence is never inferred from an error.
    """
    lp = pathlib.Path(logical_path(os.fspath(logical)))
    unknown = False
    for kind, location, path in container_candidates(lp, archive_dir=archive_dir):
        try:
            st = os.stat(path)
        except OSError as exc:
            if exc.errno not in _ABSENT_ERRNOS:
                unknown = True
            continue
        if not _stat.S_ISREG(st.st_mode):
            unknown = True
            continue
        fp = ContainerFingerprint(kind, os.fspath(path), int(st.st_size),
                                  int(st.st_mtime_ns), int(st.st_dev),
                                  int(st.st_ino))
        return "present", SelectedContainer(lp, kind, path, fp, location)
    return ("unknown", None) if unknown else ("absent", None)


def observe_watch_sig(logical: PathLike, *,
                      archive_dir: "PathLike | None" = None) -> "tuple | None":
    selected = select_container(logical, archive_dir=archive_dir)
    return (None if selected is None
            else selected.fingerprint.watch_sig(selected.location))


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
