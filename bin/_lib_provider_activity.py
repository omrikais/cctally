"""Provider-activity evidence for status-line contributors (#797, spec 4.1, 4.2).

For each status-line arrival, render captures ONE provider-activity instant
``t_m``: the latest timestamp, across the session's main transcript and its
subagent transcripts (``<transcript without .jsonl>/subagents/agent-*.jsonl``),
of an ``assistant`` entry that carries a nonempty provider ``requestId`` and is
not synthetic (``message.model == "<synthetic>"``). A provider-linked error
response that carries a ``requestId`` counts; file mtime and request-send time
are never evidence. ``t_m`` is an upper bound on the instant the input's quota
sample was taken (assumption A-T), so a response the scan missed is the unsafe
direction: anything not fully covered is ``unknown``, and unknown evidence is
always eligible (O5).

- Scanner: each file is scanned backward from the size captured at arrival,
  through its opened descriptor, at most ``MAX_FILE_BYTES`` in ``CHUNK_BYTES``
  chunks, considering only complete JSON lines. A potentially qualifying
  malformed or unfinished record makes the file unknown, never skipped. The
  file's result is its LAST qualifying record in file order; ``t_m`` is the
  latest of those across every file.
- Coverage and the scan cache (D10a): the subagent inventory is enumerated
  completely up to ``MAX_INVENTORY``. One cache document per contributor
  records, per file incarnation (device, inode, captured size, ``mtime_ns``)
  and parser revision, the result of a completed scan of that captured prefix.
  Each arrival examines the main transcript plus at most
  ``ARRIVAL_FILE_BUDGET`` uncached or changed subagent files, rotated after a
  persisted cursor. The cache is an accelerator, never an authority: its loss,
  contention or invalidity degrades to unknown, and a cached unknown is never
  rescanned into known while its key is unchanged.

The result is frozen into a compact ``ActivityWitness`` carried inside the v2
candidate document; nothing downstream of capture rescans or consults the
cache. Pure apart from reading the transcripts and the cache it is handed: no
database, journal, clock or ``cctally`` namespace dependency.
"""
from __future__ import annotations

import calendar
import dataclasses
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import time
from typing import Literal

EVIDENCE_REVISION = 1
PARSER_REVISION = 1
UNKNOWN_REASONS = (
    "no-transcript-path", "missing", "unreadable", "malformed",
    "scan-exhausted", "coverage-incomplete", "no-qualifying-record",
)
MAX_FILE_BYTES = 1 << 20
CHUNK_BYTES = 64 << 10
MAX_INVENTORY = 4096
ARRIVAL_FILE_BUDGET = 8

CACHE_SCHEMA_VERSION = 1
#: 4,097 file records stay far below this; a larger document is invalid.
CACHE_DOCUMENT_MAX_BYTES = 4 << 20
_COUNT_MAX = 2**31 - 1
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_INSTANT_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(Z|[+-]\d{2}:\d{2})\Z")
_CANONICAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")
_MAIN = "main"

# Per-file result statuses (cache records and the digest). ``none`` is a
# completed scan of the captured prefix that found no qualifying record.
_KNOWN, _NONE, _UNKNOWN, _UNEXAMINED = "known", "none", "unknown", "unexamined"


class MalformedWitness(ValueError):
    """A persisted activity witness is missing, extra or ill-typed."""


@dataclasses.dataclass(frozen=True)
class ActivityWitness:
    evidence_revision: int
    parser_revision: int
    status: Literal["known", "unknown"]
    #: The latest qualifying record's timestamp at its original precision,
    #: normalized to UTC ``...Z``; None when unknown.
    instant_utc: str | None
    #: One of ``UNKNOWN_REASONS`` when unknown, else None.
    reason: str | None
    files_total: int
    files_examined: int
    #: sha256 over the canonical serialization of the captured inventory and
    #: per-file results. It binds what render captured; it does not verify the
    #: omitted per-file records, which stay in the scan cache.
    digest: str


# --- instants ----------------------------------------------------------------


def canonical_instant(value: object) -> str | None:
    """``value`` as a UTC ``YYYY-MM-DDTHH:MM:SS[.f]Z`` keeping its fractional
    digits exactly, or None when it is not an ISO 8601 instant with an offset."""
    if not isinstance(value, str):
        return None
    match = _INSTANT_RE.fullmatch(value)
    if match is None:
        return None
    fields = tuple(int(match.group(i)) for i in range(1, 7))
    fraction, offset = match.group(7), match.group(8)
    try:
        seconds = calendar.timegm(fields + (0, 0, 0))
        # Round-tripping rejects impossible dates and times (Feb 30, 24:00).
        if tuple(time.gmtime(seconds)[:6]) != fields:
            return None
        if offset != "Z":
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 23 or minutes > 59:
                return None
            sign = 1 if offset[0] == "+" else -1
            seconds -= sign * (hours * 3600 + minutes * 60)
        parts = time.gmtime(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    if not 1 <= parts.tm_year <= 9999:
        return None
    text = "%04d-%02d-%02dT%02d:%02d:%02d" % tuple(parts[:6])
    return text + ("" if fraction is None else "." + fraction) + "Z"


def instant_key(text: str) -> int:
    """Integer nanoseconds since the epoch of a canonical instant."""
    match = _INSTANT_RE.fullmatch(text)
    if match is None or match.group(8) != "Z":
        raise ValueError("not a canonical instant")
    seconds = calendar.timegm(tuple(int(match.group(i)) for i in range(1, 7)) + (0, 0, 0))
    fraction = match.group(7) or ""
    return seconds * 1_000_000_000 + int((fraction + "0" * 9)[:9])


def instant_precision(text: str) -> int:
    """The number of fractional-second digits a canonical instant carries."""
    match = _INSTANT_RE.fullmatch(text)
    if match is None:
        raise ValueError("not a canonical instant")
    return len(match.group(7) or "")


# --- the witness codec ---------------------------------------------------------


_WITNESS_KEYS = frozenset({
    "evidenceRevision", "parserRevision", "status", "instant", "reason",
    "filesTotal", "filesExamined", "digest",
})


def witness_to_json(w: ActivityWitness) -> dict:
    return {
        "evidenceRevision": w.evidence_revision,
        "parserRevision": w.parser_revision,
        "status": w.status,
        "instant": w.instant_utc,
        "reason": w.reason,
        "filesTotal": w.files_total,
        "filesExamined": w.files_examined,
        "digest": w.digest,
    }


def _strict_int(value: object, *, minimum: int) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and minimum <= value <= _COUNT_MAX)


def witness_from_json(doc: object) -> ActivityWitness:
    """Validate one persisted witness; ``MalformedWitness`` on any missing,
    extra or ill-typed field. Never defaults: ``unknown`` is only ever an
    explicit capture result."""
    if not isinstance(doc, dict) or set(doc) != _WITNESS_KEYS:
        raise MalformedWitness("activity witness keys")
    if not (_strict_int(doc["evidenceRevision"], minimum=1)
            and _strict_int(doc["parserRevision"], minimum=1)
            and _strict_int(doc["filesTotal"], minimum=0)
            and _strict_int(doc["filesExamined"], minimum=0)):
        raise MalformedWitness("activity witness counts")
    if doc["filesExamined"] > doc["filesTotal"]:
        raise MalformedWitness("activity witness coverage")
    if not isinstance(doc["digest"], str) or _DIGEST_RE.fullmatch(doc["digest"]) is None:
        raise MalformedWitness("activity witness digest")
    status, instant, reason = doc["status"], doc["instant"], doc["reason"]
    if status == "known":
        if (not isinstance(instant, str) or _CANONICAL_RE.fullmatch(instant) is None
                or canonical_instant(instant) != instant or reason is not None):
            raise MalformedWitness("known activity witness")
    elif status == "unknown":
        if instant is not None or reason not in UNKNOWN_REASONS:
            raise MalformedWitness("unknown activity witness")
    else:
        raise MalformedWitness("activity witness status")
    return ActivityWitness(
        evidence_revision=doc["evidenceRevision"],
        parser_revision=doc["parserRevision"],
        status=status, instant_utc=instant, reason=reason,
        files_total=doc["filesTotal"], files_examined=doc["filesExamined"],
        digest=doc["digest"],
    )


# --- the per-file scanner ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Scan:
    status: str                  # _KNOWN, _NONE or _UNKNOWN
    instant: str | None = None
    reason: str | None = None
    #: The offset just past the last complete line inside the captured prefix
    #: (None when the scan never established it).
    lines_end: int | None = None


def _open_file(path: str) -> int:
    # O_NONBLOCK: a FIFO at the path must never block a render.
    return os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))


_QUALIFYING, _SKIP, _MALFORMED = "qualifying", "skip", "malformed"


def _classify_line(line: bytes) -> tuple[str, str | None]:
    text = line.strip()
    if not text:
        return _SKIP, None
    # A record that is (or could be) an assistant entry spells "assistant",
    # literally or through a JSON escape; anything else cannot qualify even
    # when it does not parse.
    if b"assistant" not in text and b"\\u" not in text:
        return _SKIP, None
    try:
        record = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return _MALFORMED, None
    if not isinstance(record, dict):
        return _MALFORMED, None
    if record.get("type") != "assistant":
        return _SKIP, None
    request_id = record.get("requestId")
    if request_id is None or request_id == "":
        return _SKIP, None
    if not isinstance(request_id, str):
        return _MALFORMED, None
    message = record.get("message")
    if isinstance(message, dict) and message.get("model") == "<synthetic>":
        return _SKIP, None
    instant = canonical_instant(record.get("timestamp"))
    if instant is None:
        return _MALFORMED, None
    return _QUALIFYING, instant


def _scan_region(fd: int, start: int, end: int) -> _Scan:
    """Scan ``[start, end)`` backward for its last qualifying record. ``start``
    is a line boundary (0, or a cached prefix's ``lines_end``); at most
    ``MAX_FILE_BYTES`` are read."""
    if end <= start:
        return _Scan(_NONE, lines_end=start)
    budget = MAX_FILE_BYTES
    pos = end
    carry = b""
    lines_end = None
    while pos > start:
        if budget <= 0:
            return _Scan(_UNKNOWN, reason="scan-exhausted", lines_end=lines_end)
        size = min(CHUNK_BYTES, pos - start, budget)
        pos -= size
        budget -= size
        try:
            chunk = os.pread(fd, size, pos)
        except OSError:
            return _Scan(_UNKNOWN, reason="unreadable")
        if len(chunk) != size:
            return _Scan(_UNKNOWN, reason="unreadable")
        data = chunk + carry
        if lines_end is None:
            newline = data.rfind(b"\n")
            if newline < 0:
                if pos > start:
                    carry = data
                    continue
                if data.strip():  # one unfinished record
                    return _Scan(_UNKNOWN, reason="malformed", lines_end=start)
                return _Scan(_NONE, lines_end=start)
            lines_end = pos + newline + 1
            if data[newline + 1:].strip():  # an unfinished final record
                return _Scan(_UNKNOWN, reason="malformed", lines_end=lines_end)
            data = data[:newline + 1]
        pieces = data.split(b"\n")
        pieces.pop()  # ``data`` ends with a newline
        if pos > start:
            carry = pieces.pop(0) if pieces else b""
        else:
            carry = b""
        for line in reversed(pieces):
            kind, instant = _classify_line(line)
            if kind == _QUALIFYING:
                return _Scan(_KNOWN, instant=instant, lines_end=lines_end)
            if kind == _MALFORMED:
                return _Scan(_UNKNOWN, reason="malformed", lines_end=lines_end)
    if lines_end is None:
        lines_end = start
    return _Scan(_NONE, lines_end=lines_end)


# --- the scan cache --------------------------------------------------------------


_RECORD_KEYS = frozenset({
    "device", "inode", "size", "mtimeNs", "linesEnd", "status", "instant", "reason"})
_MAIN_KEYS = _RECORD_KEYS | {"pathDigest"}
_CACHE_KEYS = frozenset({"schemaVersion", "parserRevision", "main", "subagents", "cursor",
                         "observedInstant"})


@dataclasses.dataclass(frozen=True)
class _Record:
    key: tuple[int, int, int, int]       # device, inode, captured size, mtime_ns
    scan: _Scan

    def document(self) -> dict:
        device, inode, size, mtime_ns = self.key
        return {"device": device, "inode": inode, "size": size, "mtimeNs": mtime_ns,
                "linesEnd": self.scan.lines_end, "status": self.scan.status,
                "instant": self.scan.instant, "reason": self.scan.reason}


class _InvalidCache(ValueError):
    pass


def _record_from(doc: object, keys: frozenset) -> _Record:
    if not isinstance(doc, dict) or set(doc) != keys:
        raise _InvalidCache("record keys")
    numbers = [doc[k] for k in ("device", "inode", "size", "mtimeNs")]
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in numbers):
        raise _InvalidCache("record key")
    lines_end = doc["linesEnd"]
    if lines_end is not None and not (
            isinstance(lines_end, int) and not isinstance(lines_end, bool)
            and 0 <= lines_end <= doc["size"]):
        raise _InvalidCache("record lines end")
    status, instant, reason = doc["status"], doc["instant"], doc["reason"]
    if status == _KNOWN:
        if (not isinstance(instant, str) or canonical_instant(instant) != instant
                or reason is not None or lines_end is None):
            raise _InvalidCache("known record")
    elif status == _NONE:
        if instant is not None or reason is not None or lines_end is None:
            raise _InvalidCache("none record")
    elif status == _UNKNOWN:
        if instant is not None or reason not in UNKNOWN_REASONS:
            raise _InvalidCache("unknown record")
    else:
        raise _InvalidCache("record status")
    return _Record(tuple(numbers), _Scan(status, instant, reason, lines_end))


@dataclasses.dataclass
class _Cache:
    main: _Record | None = None
    main_path_digest: str | None = None
    subagents: dict = dataclasses.field(default_factory=dict)
    cursor: str | None = None
    #: The latest qualifying instant any earlier arrival of this transcript
    #: path found in any of its files (F-I3-1). ``t_m`` is an upper bound on
    #: the input's sample instant, so a response already observed keeps
    #: contributing after its file leaves the listing or is replaced: a
    #: deletion never lowers ``t_m``.
    observed_instant: str | None = None


def cache_lock_path(cache_path) -> pathlib.Path:
    path = pathlib.Path(cache_path)
    return path.with_name(path.name + ".lock")


def _load_cache(cache_path: pathlib.Path) -> tuple[_Cache, bool]:
    """The cache and whether it was valid. An absent cache is a valid, empty
    one (it cannot be told apart from a first arrival); a cache written
    under another parser revision is a miss, not invalid data."""
    try:
        with open(cache_path, "rb") as fh:
            raw = fh.read(CACHE_DOCUMENT_MAX_BYTES + 1)
    except FileNotFoundError:
        return _Cache(), True
    except OSError:
        return _Cache(), False
    try:
        if len(raw) > CACHE_DOCUMENT_MAX_BYTES:
            raise _InvalidCache("cache size")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or set(doc) != _CACHE_KEYS:
            raise _InvalidCache("cache keys")
        if doc["schemaVersion"] != CACHE_SCHEMA_VERSION:
            raise _InvalidCache("cache schema")
        revision = doc["parserRevision"]
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise _InvalidCache("cache revision")
        if revision != PARSER_REVISION:
            return _Cache(), True
        cache = _Cache()
        if doc["main"] is not None:
            main = doc["main"]
            if not isinstance(main, dict):
                raise _InvalidCache("main record")
            digest = main.get("pathDigest")
            if (set(main) != _MAIN_KEYS or not isinstance(digest, str)
                    or _DIGEST_RE.fullmatch(digest) is None):
                raise _InvalidCache("main record")
            cache.main = _record_from(
                {k: v for k, v in main.items() if k != "pathDigest"}, _RECORD_KEYS)
            cache.main_path_digest = digest
        subagents = doc["subagents"]
        if not isinstance(subagents, dict) or len(subagents) > MAX_INVENTORY:
            raise _InvalidCache("subagent records")
        for name, record in subagents.items():
            if not _is_subagent_name(name):
                raise _InvalidCache("subagent name")
            cache.subagents[name] = _record_from(record, _RECORD_KEYS)
        cursor = doc["cursor"]
        if cursor is not None and not (isinstance(cursor, str) and _is_subagent_name(cursor)):
            raise _InvalidCache("cursor")
        cache.cursor = cursor
        observed = doc["observedInstant"]
        if observed is not None and (not isinstance(observed, str)
                                     or canonical_instant(observed) != observed):
            raise _InvalidCache("observed instant")
        cache.observed_instant = observed
        return cache, True
    except (_InvalidCache, ValueError, TypeError, KeyError):
        return _Cache(), False


def _cache_document(cache: _Cache) -> dict:
    main = None
    if cache.main is not None:
        main = dict(cache.main.document(), pathDigest=cache.main_path_digest)
    return {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "parserRevision": PARSER_REVISION,
        "main": main,
        "subagents": {name: cache.subagents[name].document()
                      for name in sorted(cache.subagents)},
        "cursor": cache.cursor,
        "observedInstant": cache.observed_instant,
    }


def _store_cache(cache_path: pathlib.Path, document: dict) -> None:
    """Atomic, unsynced, best-effort: the cache is never authoritative."""
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > CACHE_DOCUMENT_MAX_BYTES:
        return
    temp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(temp, cache_path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass


def _lock_cache(cache_path: pathlib.Path) -> int | None:
    """The contributor's cache lock, non-blocking; None when another arrival
    holds it or it cannot be taken (contention degrades to unknown)."""
    try:
        cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(cache_lock_path(cache_path),
                     os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock_cache(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# --- inventory -------------------------------------------------------------------


def _is_subagent_name(name: object) -> bool:
    return (isinstance(name, str) and name.startswith("agent-") and name.endswith(".jsonl")
            and len(name) > len("agent-.jsonl") and "/" not in name and "\0" not in name)


def _subagent_dir(transcript_path: str) -> str:
    base = transcript_path[:-len(".jsonl")] if transcript_path.endswith(".jsonl") else transcript_path
    return os.path.join(base, "subagents")


def _list_subagents(directory: str) -> tuple[tuple[str, ...], bool]:
    """The subagent transcript names and whether the listing reached a
    confirmed end of directory within ``MAX_INVENTORY`` entries. An absent
    directory is a complete, empty inventory."""
    names = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not _is_subagent_name(entry.name):
                    continue
                if len(names) >= MAX_INVENTORY:
                    return tuple(sorted(names)), False
                names.append(entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return (), True
    except OSError:
        return (), False
    return tuple(sorted(names)), True


# --- one file ------------------------------------------------------------------


def _stat_key(path: str) -> tuple[int, int, int, int] | str:
    """The path's current incarnation key, or a reason it has none."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return "unreadable"
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _changed(cached: _Record, key: tuple) -> _Scan | None:
    """None when ``key`` may extend ``cached`` (the same incarnation grown or
    touched in place), else the unknown result of a replacement or
    shrinkage."""
    if (cached.key[0], cached.key[1]) != (key[0], key[1]) or key[2] < cached.key[2]:
        return _Scan(_UNKNOWN, reason="coverage-incomplete")
    return None


def _examine(path: str, cached: _Record | None) -> tuple[_Record | None, _Scan]:
    """Scan one file through its opened descriptor, extending ``cached``
    when this incarnation only grew. Returns the record to cache (None when
    the file has no incarnation) and this arrival's result."""
    try:
        fd = _open_file(path)
    except FileNotFoundError:
        return None, _Scan(_UNKNOWN, reason="missing")
    except OSError:
        return None, _Scan(_UNKNOWN, reason="unreadable")
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return None, _Scan(_UNKNOWN, reason="unreadable")
        if not stat.S_ISREG(st.st_mode):
            return None, _Scan(_UNKNOWN, reason="unreadable")
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if cached is not None:
            if cached.key == key:
                return cached, cached.scan
            refused = _changed(cached, key)
            if refused is not None:
                return _Record(key, refused), refused
            prefix = cached.scan
            if prefix.lines_end is not None and prefix.reason != "unreadable":
                suffix = _scan_region(fd, prefix.lines_end, key[2])
                if suffix.status == _NONE:
                    result = dataclasses.replace(prefix, lines_end=suffix.lines_end)
                else:
                    result = suffix
                return _Record(key, result), result
        result = _scan_region(fd, 0, key[2])
        return _Record(key, result), result
    finally:
        os.close(fd)


# --- capture ----------------------------------------------------------------------


def _digest(entries: list, inventory_complete: bool, cache_valid: bool) -> str:
    return hashlib.sha256(json.dumps({
        "evidenceRevision": EVIDENCE_REVISION,
        "parserRevision": PARSER_REVISION,
        "inventoryComplete": inventory_complete,
        "cacheValid": cache_valid,
        "files": entries,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _entry(name: str, key, scan: _Scan) -> list:
    key = list(key) if isinstance(key, tuple) else [None, None, None, None]
    return [name, *key, scan.status, scan.instant, scan.reason]


def _witness(status, instant, reason, total, examined, digest) -> ActivityWitness:
    return ActivityWitness(
        evidence_revision=EVIDENCE_REVISION, parser_revision=PARSER_REVISION,
        status=status, instant_utc=instant, reason=reason,
        files_total=total, files_examined=examined, digest=digest)


def capture_activity(transcript_path: str | None, *, cache_path: pathlib.Path) -> ActivityWitness:
    """Capture one arrival's provider-activity evidence (spec 4.1). Called
    once per arrival, before the intake commit and outside the intake
    critical section; never raises for anything a transcript or the cache
    can contain."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return _witness("unknown", None, "no-transcript-path", 0, 0, _digest([], True, True))
    try:
        return _capture_locked(transcript_path, pathlib.Path(cache_path))
    except Exception:
        # Every transcript and cache condition is classified above; anything
        # else is still an explicit, eligible unknown capture, never a
        # failed render.
        return _witness("unknown", None, "unreadable", 0, 0, _digest([], False, False))


def _capture_locked(transcript_path: str, cache_path: pathlib.Path) -> ActivityWitness:
    lock = _lock_cache(cache_path)
    try:
        if lock is None:
            cache, cache_valid, contended = _Cache(), True, True
        else:
            (cache, cache_valid), contended = _load_cache(cache_path), False
        before = None if contended else _cache_document(cache)
        result = _capture(transcript_path, cache, cache_valid=cache_valid)
        if lock is not None:
            after = _cache_document(cache)
            if after != before or not cache_valid:
                _store_cache(cache_path, after)
        return result
    finally:
        if lock is not None:
            _unlock_cache(lock)


def _latest(instants) -> str:
    return max(instants, key=lambda s: (instant_key(s), instant_precision(s), s))


def _latest_or_none(instants) -> str | None:
    instants = [i for i in instants if i is not None]
    return _latest(instants) if instants else None


def _capture(transcript_path: str, cache: _Cache, *, cache_valid: bool) -> ActivityWitness:
    path_digest = hashlib.sha256(transcript_path.encode("utf-8", "surrogatepass")).hexdigest()
    if cache.main_path_digest != path_digest:
        # Another transcript path: every cached record (the subagent ones
        # name files of another directory) and the cursor are someone else's.
        cache.main, cache.subagents, cache.cursor = None, {}, None
        cache.observed_instant = None
    # Fold every instant earlier arrivals cached before this arrival may
    # drop or replace a record (F-I3-1).
    cache.observed_instant = _latest_or_none(
        [cache.observed_instant]
        + [record.scan.instant for record in [cache.main, *cache.subagents.values()]
           if record is not None and record.scan.status == _KNOWN])
    cached_main = cache.main
    main_key = _stat_key(transcript_path)
    if cached_main is not None and main_key == cached_main.key:
        record, main = cached_main, cached_main.scan
    else:
        record, main = _examine(transcript_path, cached_main)
    cache.main, cache.main_path_digest = record, (None if record is None else path_digest)
    entries = [_entry(_MAIN, None if record is None else record.key, main)]
    if main.status == _UNKNOWN:
        return _witness("unknown", None, main.reason, 1, 1,
                        _digest(entries, True, cache_valid))

    directory = _subagent_dir(transcript_path)
    names, inventory_complete = _list_subagents(directory)
    listed = set(names)
    vanished = inventory_complete and any(name not in listed for name in cache.subagents)
    if inventory_complete:
        for name in [n for n in cache.subagents if n not in listed]:
            del cache.subagents[name]
    results: dict[str, tuple[object, _Scan]] = {}
    work = []
    for name in names:
        path = os.path.join(directory, name)
        key = _stat_key(path)
        cached = cache.subagents.get(name)
        if isinstance(key, str):
            # Listed, but gone or unreadable when examined: a membership
            # change this arrival cannot resolve, or an unreadable member.
            reason = "coverage-incomplete" if key == "missing" else key
            cache.subagents.pop(name, None)
            results[name] = (None, _Scan(_UNKNOWN, reason=reason))
            continue
        if cached is not None and cached.key == key:
            results[name] = (key, cached.scan)
            continue
        if cached is not None:
            refused = _changed(cached, key)
            if refused is not None:
                cache.subagents[name] = _Record(key, refused)
                results[name] = (key, refused)
                continue
        work.append(name)
    # Fair rotation: continue after the persisted cursor, wrapping around.
    if cache.cursor is not None:
        work = ([n for n in work if n > cache.cursor] + [n for n in work if n <= cache.cursor])
    for name in work[:ARRIVAL_FILE_BUDGET]:
        record, scan = _examine(os.path.join(directory, name), cache.subagents.get(name))
        if record is None:
            cache.subagents.pop(name, None)
            if scan.reason == "missing":
                scan = _Scan(_UNKNOWN, reason="coverage-incomplete")
        else:
            cache.subagents[name] = record
        results[name] = (None if record is None else record.key, scan)
        cache.cursor = name
    for name in work[ARRIVAL_FILE_BUDGET:]:
        results[name] = (None, _Scan(_UNEXAMINED))

    for name in names:
        key, scan = results[name]
        entries.append(_entry(name, key, scan))
    total = 1 + len(names)
    examined = total - sum(1 for _key, scan in results.values() if scan.status == _UNEXAMINED)
    digest = _digest(entries, inventory_complete, cache_valid)

    def unknown(reason):
        return _witness("unknown", None, reason, total, examined, digest)

    if not inventory_complete or vanished:
        return unknown("coverage-incomplete")
    for name in names:
        scan = results[name][1]
        if scan.status == _UNKNOWN:
            return unknown(scan.reason)
    if examined < total or not cache_valid:
        return unknown("coverage-incomplete")
    instants = [scan.instant for _key, scan in [(None, main)] + list(results.values())
                if scan.status == _KNOWN]
    if cache.observed_instant is not None:
        instants.append(cache.observed_instant)
    if not instants:
        return unknown("no-qualifying-record")
    latest = _latest(instants)
    return _witness("known", latest, None, total, examined, digest)
