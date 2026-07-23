"""Pure journal kernel — line codec, identity, segment naming/order, tail scan.

The durable-truth journal (design spec
docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md §4) stores one
compact JSON object per line in monthly segments. This module owns everything
about that format that is *pure*: encoding/decoding a line, deriving the stable
`id` for every line class, naming and canonically ordering segments, and
scanning a file's tail for the last complete line (torn-tail repair support).

I/O-free by construction — stdlib only (`json`, `hashlib`, `datetime`), no
imports from `_cctally_*`, no filesystem or lock access. The append/ingest I/O
lives in `bin/_cctally_journal.py`; the durability discipline that consumes
`valid_tail_offset`/`journal_high_water` lives there.

Line format (spec §4.2), additive-evolution — readers tolerate unknown keys
and unknown `t` values:

    {"v":1,"t":"obs","id":"o:…","at":"…Z","src":"…","provider":"…","payload":{…}}
    {"v":1,"t":"op","id":"o:…","at":"…Z","src":"record-credit","payload":{…}}
    {"v":1,"t":"evt","id":"<natural-key>","rev":0,"at":"…Z","src":"ingest","payload":{"kind":…}}

- `id`: obs/op carry a content digest over (t, at, src[, provider], payload);
  bootstrap-exported lines use `b:<table>:<rowid>`; evt lines carry their target
  table's full natural key with logical-id FK refs (spec §4.2 FK rule).
- `rev`: evt revision, default 0; the fold keeps the highest rev per id
  (reserved for the deferred correction subsystem, spec §5.5).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

LINE_VERSION = 1

SEGMENT_PREFIX = "observations-"
BOOTSTRAP_PREFIX = "bootstrap-"


# --------------------------------------------------------------------------
# line codec
# --------------------------------------------------------------------------

def _canonical_json(obj: dict) -> str:
    """Deterministic compact JSON: sorted keys, no separator whitespace,
    non-ASCII preserved literally. The single serialization shape used for
    both the on-disk line and the content digest, so an id recomputed from a
    decoded line matches the id computed at write time."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def encode_line(record: dict) -> bytes:
    """Canonical compact JSON for ``record`` plus a trailing ``\\n`` (UTF-8)."""
    return _canonical_json(record).encode("utf-8") + b"\n"


def decode_line(raw: bytes) -> dict | None:
    """Decode one journal line. Returns the dict, or ``None`` on ANY parse or
    shape failure — the line must be a JSON object carrying a string ``t``.

    ``None`` is how the ingester distinguishes a malformed line (skip + count,
    spec §4.4) from a real record; it never raises on bad input."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("t"), str):
        return None
    return obj


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def content_id(record_sans_id: dict) -> str:
    """Content digest id for an obs/op line: ``"o:" + sha256(canonical)[:16]``.

    ``record_sans_id`` is the identity-bearing subset — for obs/op that is
    ``{t, at, src[, provider], payload}`` (no ``v``, no ``id``). Stable under
    key insertion order (canonical JSON sorts keys), so re-deriving the id from
    a decoded line reproduces it (spec §4.2)."""
    digest = hashlib.sha256(_canonical_json(record_sans_id).encode("utf-8")).hexdigest()
    return "o:" + digest[:16]


def bootstrap_id(table: str, rowid: int) -> str:
    """Stable id for a row exported at cutover: ``b:<table>:<legacy rowid>``.
    Stable across cutover re-runs, which is what makes double-fold idempotent
    (spec §8)."""
    return f"b:{table}:{rowid}"


def evt_id(kind: str, *parts: object) -> str:
    """Natural-key id for an evt line: ``"<kind>:" + ":".join(str(p) …)``.

    Each caller passes its target table's full UNIQUE-constraint components,
    with any DB-assigned integer FK replaced by the *logical id* of the
    referenced record (spec §4.2). e.g.
    ``evt_id("pm", week_start_at, reset_segment_logical_id, pct)``."""
    return f"{kind}:" + ":".join(str(p) for p in parts)


# --------------------------------------------------------------------------
# fully-formed line records
# --------------------------------------------------------------------------

def make_obs(at: str, src: str, provider: str, payload: dict) -> dict:
    """Build a complete ``obs`` line record (raw capture; canonicalization is
    derivation-time, never baked into the stored line — spec §4.2)."""
    core = {"t": "obs", "at": at, "src": src, "provider": provider, "payload": payload}
    return {"v": LINE_VERSION, **core, "id": content_id(core)}


def make_op(at: str, src: str, payload: dict) -> dict:
    """Build a complete ``op`` (operator record) line — no ``provider``."""
    core = {"t": "op", "at": at, "src": src, "payload": payload}
    return {"v": LINE_VERSION, **core, "id": content_id(core)}


def make_evt(kind: str, id: str, at: str, payload: dict, rev: int = 0) -> dict:
    """Build a complete ``evt`` (derived) line record.

    The ``id`` is the caller-built natural key (see ``evt_id``); ``kind`` is the
    fold-dispatch family written into ``payload["kind"]`` (spec §5.3). The
    caller's ``payload`` dict is not mutated. ``src`` is always ``"ingest"`` —
    evt lines exist only because the ingester derived them."""
    body = dict(payload)
    body["kind"] = kind
    return {"v": LINE_VERSION, "t": "evt", "id": id, "rev": rev,
            "at": at, "src": "ingest", "payload": body}


# --------------------------------------------------------------------------
# segment naming + canonical order
# --------------------------------------------------------------------------

def segment_name(now_utc: dt.datetime) -> str:
    """``observations-YYYY-MM.jsonl`` for the UTC calendar month of ``now_utc``.

    A tz-aware datetime is converted to UTC first (spec §4.1: segments are cut
    by the UTC month of the append); a naive datetime is treated as UTC."""
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(dt.timezone.utc)
    return f"{SEGMENT_PREFIX}{now_utc.year:04d}-{now_utc.month:02d}.jsonl"


def segment_sort_key(name: str) -> tuple:
    """Canonical segment order key (spec §4.1): bootstrap segments first, then
    observation segments, each class lexicographic by name. Anything else sorts
    last so a stray file can never wedge before real segments."""
    if name.startswith(BOOTSTRAP_PREFIX):
        return (0, name)
    if name.startswith(SEGMENT_PREFIX):
        return (1, name)
    return (2, name)


# --------------------------------------------------------------------------
# torn-tail scan
# --------------------------------------------------------------------------

def valid_tail_offset(chunk: bytes, chunk_start: int) -> int:
    """Absolute file offset just past the last ``\\n`` in ``chunk``.

    ``chunk`` is the file's final ≤64 KiB window; ``chunk_start`` is that
    window's absolute file offset. Used by the appender to ``ftruncate`` a torn
    tail back to the last complete line (spec §4.3). When the window holds no
    newline at all, returns ``chunk_start`` (the whole window is one incomplete
    line — the appender treats a >64 KiB such window as a hard error)."""
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return chunk_start
    return chunk_start + idx + 1


# --------------------------------------------------------------------------
# decode helper
# --------------------------------------------------------------------------

def iter_decoded(lines):
    """Yield ``(offset, decode_line(raw))`` for each ``(offset, raw)`` in
    ``lines`` — a thin pairing helper so the ingester can count malformed lines
    (``None`` results) while keeping their offsets for diagnostics."""
    for offset, raw in lines:
        yield offset, decode_line(raw)
