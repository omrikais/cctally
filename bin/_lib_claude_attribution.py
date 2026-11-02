"""Pure write-time account attribution kernel for Claude cost records (#944).

Claude cost ingest used to stamp every newly read JSONL line with the account
active when the ingest RAN. This kernel decides the account active when the
line was WRITTEN instead, from the Claude account timeline the #825/#832 usage
fence already captures (spec
``docs/superpowers/specs/2026-10-29-944-claude-write-time-cost-attribution-design.md``,
sections 5.2 and 5.3), and falls back to the ingest-time identity only when
that evidence is absent or inconclusive.

Two halves:

* the rule (``decide``): a record's timestamp interval against its timeline
  neighbours -- the latest observation instant entirely before it, every
  instant overlapping it, and the earliest instant entirely after it (or the
  sync's closing capture when no observation follows). Agreement of every one
  of them names the account; anything else falls back with a typed reason;
* the identity encoders: the durable decision key a record is filed under
  (keyed by its logical ``(msg_id, req_id)``, physical by its exact raw bytes,
  or the legacy projection of an already-cached row), and legacy matching.

Pure stdlib leaf: no I/O, no clock, no SQLite, no project imports.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from typing import Iterable, Mapping

#: The attribution rule's version. A change invalidates the derived timeline
#: and rebuilds it from the journal; existing decisions are never
#: reinterpreted (spec 5.1).
RULE_VERSION = 1

BASIS_AGREEMENT = "timeline_agreement"
BASIS_FALLBACK = "fallback"
BASIS_PRESERVED = "preserved_history"
BASES = (BASIS_AGREEMENT, BASIS_FALLBACK, BASIS_PRESERVED)

#: The fallback reasons, in the order the rule evaluates them (spec 5.2).
FALLBACK_REASONS = (
    "missing_timestamp", "invalid_timestamp", "clock_order", "pre_timeline",
    "ambiguous_instant", "open_tail", "switch_window",
)

#: The binding sentinel a ``stably_absent`` capture carries (spec 5.1).
UNATTRIBUTED = "unattributed"

FORM_KEYED = "k1"
FORM_PHYSICAL = "p1"
FORM_LEGACY = "l1"
FORMS = (FORM_KEYED, FORM_PHYSICAL, FORM_LEGACY)

#: The retained accounting fields of a cost row, in projection order (5.3).
PROJECTION_FIELDS = (
    "timestamp_utc", "model", "input_tokens", "output_tokens",
    "cache_create_tokens", "cache_read_tokens", "cache_create_1h_tokens",
    "cache_create_5m_tokens", "speed", "cost_usd_raw",
)
_INT_FIELDS = frozenset((
    "input_tokens", "output_tokens", "cache_create_tokens",
    "cache_read_tokens", "cache_create_1h_tokens", "cache_create_5m_tokens",
))
_SPLIT_INDICES = (6, 7)

MATCH_EXACT = "exact"
MATCH_UNKNOWN_SPLIT = "unknown_split"

_LOC_DOMAIN = "cctally-claude-loc-v1\0"
_US = 1_000_000
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?\Z")


# --------------------------------------------------------------------------
# instants and intervals
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Instant:
    """One coalesced observation instant: every admissible observation whose
    precision interval is exactly ``[lo_us, lo_us + precision_us)``, as the
    set of accounts they name (``None`` is an explicit NULL-account
    observation). More than one account makes the instant ambiguous."""

    lo_us: int
    precision_us: int
    accounts: frozenset

    @property
    def interval(self) -> tuple[int, int]:
        return (self.lo_us, self.lo_us + self.precision_us)

    @property
    def ambiguous(self) -> bool:
        return len(self.accounts) != 1


@dataclasses.dataclass(frozen=True)
class Neighbours:
    """A record's timeline neighbours (spec 5.2): ``before`` is ``Pb``,
    ``overlapping`` is ``Ov`` and ``after`` is ``Sa``."""

    before: "Instant | None"
    overlapping: tuple
    after: "Instant | None"


@dataclasses.dataclass(frozen=True)
class ClosingCapture:
    """The stable ingest identity resolved after the sync froze its source
    prefixes, normalized like an observation, at the ``clock_cutoff`` instant."""

    account_key: "str | None"
    at_us: int


@dataclasses.dataclass(frozen=True)
class Decision:
    account_key: "str | None"
    basis: str
    reason: "str | None"
    used_closing_capture: bool


def _parse_instant(text: str) -> tuple[int, int]:
    """``(lo_us, precision_us)`` of an ISO-8601 instant, the precision
    following from the fractional digits it was written with (none: one
    second). A value without an offset is UTC. ``ValueError`` otherwise."""
    if not isinstance(text, str):
        raise ValueError("instant is not a string")
    match = _ISO_RE.match(text.strip())
    if match is None:
        raise ValueError(f"unparseable instant {text!r}")
    year, month, day, hour, minute, second, frac, offset = match.groups()
    moment = dt.datetime(int(year), int(month), int(day), int(hour),
                         int(minute), int(second), tzinfo=dt.timezone.utc)
    if offset and offset not in ("Z", "z"):
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        delta = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        moment = moment - sign * delta
    whole = moment - _EPOCH
    lo_us = (whole.days * 86_400 + whole.seconds) * _US
    if frac:
        digits = len(frac)
        if digits <= 6:
            precision_us = 10 ** (6 - digits)
            lo_us += int(frac) * precision_us
        else:
            precision_us = 1
            lo_us += int(frac[:6])
    else:
        precision_us = _US
    return lo_us, precision_us


def capture_interval(captured_at: str) -> tuple[int, int]:
    """The precision interval ``(lo_us, hi_us)`` of an observation's
    ``captured_at``; ``ValueError`` when it does not parse."""
    lo_us, precision_us = _parse_instant(captured_at)
    return lo_us, lo_us + precision_us


def record_interval(value: object) -> "tuple[int, int] | str":
    """The interval of a record timestamp, or the fallback reason
    ``missing_timestamp`` / ``invalid_timestamp``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "missing_timestamp"
    try:
        lo_us, precision_us = _parse_instant(value)
    except (ValueError, OverflowError):
        return "invalid_timestamp"
    return lo_us, lo_us + precision_us


def relation(obs: tuple[int, int], rec: tuple[int, int]) -> str:
    """``before`` when ``obs`` ends at or before ``rec`` starts, ``after``
    when it starts at or after ``rec`` ends, ``overlapping`` otherwise (its
    order relative to the record is unknown)."""
    if obs[1] <= rec[0]:
        return "before"
    if obs[0] >= rec[1]:
        return "after"
    return "overlapping"


def normalize_binding_account(account_key: str) -> "str | None":
    """A binding ``accountKey``: the ``unattributed`` sentinel becomes NULL."""
    return None if account_key == UNATTRIBUTED else account_key


def valid_record_id(value) -> bool:
    """Whether a transcript ``message.id`` / ``requestId`` can name a record:
    only a non-empty string can. Anything else (empty, numeric, an object)
    counts as missing, so the record is identified by its bytes (physical) or
    its projection (legacy), never by a keyed item the journal would reject."""
    return isinstance(value, str) and bool(value)


def keyed_ids(msg_id, req_id) -> "tuple[str, str] | None":
    """The ``(msg_id, req_id)`` a keyed identity uses, or None when either
    is not a valid record id (the one identity rule every path shares)."""
    if valid_record_id(msg_id) and valid_record_id(req_id):
        return msg_id, req_id
    return None


def preserved_stamp(account_key) -> "str | None":
    """A cached row's stamp as ``preserved_history`` carries it: a real
    account key, otherwise NULL (the ``unattributed`` sentinel, an empty
    string or any non-string value; a decision never carries those)."""
    if not isinstance(account_key, str) or not account_key:
        return None
    return normalize_binding_account(account_key)


def iso_from_us(value_us: int) -> str:
    """A fixed-width UTC ISO string (microseconds, ``Z``), so lexicographic
    order is chronological order."""
    moment = _EPOCH + dt.timedelta(microseconds=int(value_us))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def us_from_iso(text: str) -> int:
    return _parse_instant(text)[0]


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------

def _fallback(ingest_account, reason) -> Decision:
    return Decision(ingest_account, BASIS_FALLBACK, reason, False)


def decide(interval, neighbours, *, clock_cutoff_us: int,
           closing_capture: "ClosingCapture | None",
           ingest_account: "str | None") -> Decision:
    """The spec 5.2 table, evaluated in its order.

    ``interval`` is ``record_interval``'s result. ``neighbours`` holds the
    observation instants around it (``None``: no timeline). When no
    observation follows the record and a closing capture is given, the
    capture serves as ``Sa``; it is after the record whenever the clock-order
    check passed. An ambiguous instant is never skipped to reach an
    unambiguous one, and an overlapping instant never stands in for ``Pb``
    or ``Sa``."""
    if isinstance(interval, str):
        return _fallback(ingest_account, interval)
    if interval[1] > clock_cutoff_us:
        return _fallback(ingest_account, "clock_order")
    before = None if neighbours is None else neighbours.before
    if before is None:
        return _fallback(ingest_account, "pre_timeline")
    overlapping = tuple(neighbours.overlapping)
    after = neighbours.after
    used_capture = False
    if after is None and closing_capture is not None:
        after = Instant(int(closing_capture.at_us), 1,
                        frozenset((closing_capture.account_key,)))
        used_capture = True
    if before.ambiguous or any(i.ambiguous for i in overlapping) or (
            after is not None and after.ambiguous):
        return _fallback(ingest_account, "ambiguous_instant")
    if after is None:
        return _fallback(ingest_account, "open_tail")
    accounts = set(before.accounts) | set(after.accounts)
    for instant in overlapping:
        accounts |= instant.accounts
    if len(accounts) == 1:
        (account,) = accounts
        return Decision(account, BASIS_AGREEMENT, None, used_capture)
    return _fallback(ingest_account, "switch_window")


# --------------------------------------------------------------------------
# identity encoders (spec 5.3)
# --------------------------------------------------------------------------

def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def loc_for(source_path: str) -> str:
    """The 32-hex location of the exact stored ``session_entries.source_path``.
    Deliberately not canonicalized: two spellings of one root are two
    physical rows, so a physical or legacy identity names at most one."""
    data = (_LOC_DOMAIN + source_path).encode("utf-8", "surrogateescape")
    return hashlib.sha256(data).hexdigest()[:32]


def keyed_key(msg_id: str, req_id: str) -> str:
    return FORM_KEYED + ":" + _compact([msg_id, req_id])


def record_sha256(record_bytes: bytes) -> str:
    """The SHA-256 of a record's exact raw bytes, terminator removed."""
    return hashlib.sha256(record_bytes).hexdigest()


def physical_key_from_digest(loc: str, byte_offset: int, digest: str) -> str:
    return FORM_PHYSICAL + ":" + _compact([loc, int(byte_offset), digest])


def physical_key(loc: str, byte_offset: int, record_bytes: bytes) -> str:
    return physical_key_from_digest(loc, byte_offset,
                                    record_sha256(record_bytes))


def _projection_value(name: str, value):
    if value is None:
        return None
    if name in _INT_FIELDS:
        if isinstance(value, bool):
            raise ValueError(f"{name} is not an integer")
        return int(value)
    if name == "cost_usd_raw":
        if isinstance(value, bool):
            raise ValueError("cost_usd_raw is not a number")
        return float(value)
    if isinstance(value, str):
        return value
    # A TEXT column (``speed``) stores a non-string scalar as its text form;
    # project the value a parsed record carries the same way the stored row
    # reads back, so a walk and a cached row agree.
    if isinstance(value, bool):
        return str(int(value))
    return str(value)


def legacy_projection(row: Mapping) -> list:
    """Every retained accounting field of a cached row, in projection order:
    SQL NULL as ``None``, integers as integers, ``cost_usd_raw`` as a float
    (JSON renders it as its shortest round-trip form)."""
    return [_projection_value(name, row[name]) for name in PROJECTION_FIELDS]


def projection_sha256(projection: list) -> str:
    return _sha256_text(_compact(list(projection)))


def projection_json(projection: list) -> str:
    return _compact(list(projection))


def legacy_key(loc: str, line_offset: int, projection: list) -> str:
    return FORM_LEGACY + ":" + _compact(
        [loc, int(line_offset), projection_sha256(projection)])


def legacy_key_from_digest(loc: str, line_offset: int, digest: str) -> str:
    return FORM_LEGACY + ":" + _compact([loc, int(line_offset), digest])


def _split_unknown(projection) -> bool:
    return all(projection[i] is None for i in _SPLIT_INDICES)


def legacy_match(decision_projection: list, record_projection: list
                 ) -> "str | None":
    """Whether a record matches a legacy decision: ``exact`` when every field
    is equal, ``unknown_split`` when the decision recorded the TTL split as
    unknown (both split fields NULL) and every other field is equal, else
    ``None``. The exception is exactly the #195 re-walk's enrichment of a
    retained row."""
    if len(decision_projection) != len(PROJECTION_FIELDS) or len(
            record_projection) != len(PROJECTION_FIELDS):
        return None
    for index, (left, right) in enumerate(
            zip(decision_projection, record_projection)):
        if index in _SPLIT_INDICES:
            continue
        if left != right or type(left) is not type(right):
            return None
    split_equal = all(
        decision_projection[i] == record_projection[i]
        and type(decision_projection[i]) is type(record_projection[i])
        for i in _SPLIT_INDICES)
    if split_equal:
        return MATCH_EXACT
    if _split_unknown(decision_projection):
        return MATCH_UNKNOWN_SPLIT
    return None


def journal_position_key(position) -> tuple:
    """Canonical order of a ``(segment, offset)`` journal position: bootstrap
    segments first, then observation segments, each by name (the journal's
    own segment order), then offset."""
    segment, offset = position
    if segment.startswith("bootstrap-"):
        klass = 0
    elif segment.startswith("observations-"):
        klass = 1
    else:
        klass = 2
    return (klass, segment, int(offset))


_MATCH_RANK = {MATCH_EXACT: 0, MATCH_UNKNOWN_SPLIT: 1}


def order_legacy_candidates(candidates: Iterable) -> list:
    """Order matching legacy candidates per spec 5.3: a match that needed no
    recorded-unknown split first, then earliest journal position. Each
    candidate is ``(match_kind, (segment, offset), ...)``; extra elements ride
    along unchanged. Non-matches (``None`` kind) are dropped."""
    kept = [c for c in candidates if c[0] in _MATCH_RANK]
    return sorted(kept, key=lambda c: (_MATCH_RANK[c[0]],
                                       journal_position_key(c[1])))
