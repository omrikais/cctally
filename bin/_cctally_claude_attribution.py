"""Write-time Claude cost attribution glue (#944).

The pure rule and identity encoders live in ``_lib_claude_attribution``; this
module owns their cache.db and journal side (spec
``docs/superpowers/specs/2026-10-29-944-claude-write-time-cost-attribution-design.md``
sections 5.1 and 5.4-5.6):

* the two derived, journal-backed tables -- ``claude_account_timeline`` (one
  row per admissible Claude ``record-usage`` observation) and
  ``claude_cost_decisions`` (one row per decision key a
  ``claude_cost_account`` op names) -- and their replay from the journal;
* timeline neighbour seeks and decision lookup;
* the fail-closed append of decision batches (journal first, then the
  decision rows in the caller's transaction);
* alignment of an unlinked accounting row to the decision its identity
  already holds, and the preserve-before-wipe export of unlinked rows.

Transactions: every function here runs its statements on the caller's
connection and never commits, except ``record_deferral``, which is its own
small transaction. Locks: the caller holds the Claude cache writer flock; the
only lock taken here is the journal's leaf ``journal.lock`` inside
``append_records``, released before it returns (the lock-order law).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from typing import Sequence

import _cctally_core
import _lib_account_binding
import _lib_account_protocol
import _lib_claude_attribution as K
import _lib_journal


CURSOR_KEY = "claude_attribution_rehydrated_hw"
CONFLICTS_KEY = "claude_cost_decision_conflicts"
ALIGNED_KEY = "claude_cost_aligned_rows"
PRESERVATION_KEY = "claude_cost_preservation"
DEFERRAL_KEY = "claude_attribution_last_deferral"
#: The deferral reason a sync reports when the journal cannot be read or
#: written (the reason the Codex attribution replay already uses).
DEFERRAL_REASON = "attribution_rehydration"
MUTATION_SEQ_KEY = "session_entries_mutation_seq"

#: Decision batches stay well under the journal's 64 KiB line limit.
_BATCH_LINE_BUDGET = 48 * 1024
#: Rows one preserve chunk scans before it appends.
_PRESERVE_CHUNK = 5_000
_MAX_PRECISION_US = 1_000_000

_OBS_MARKERS = (b'"account_binding"', b'"record-usage"')
_DECISION_MARKER = b'"' + _lib_journal.CLAUDE_COST_ACCOUNT_KIND.encode() + b'"'

_PROJECTION_COLUMNS = ", ".join(K.PROJECTION_FIELDS)


class AttributionJournalUnavailable(Exception):
    """The journal could not be read or written, so no attribution decision
    may be made durable. ``contended`` distinguishes lock contention (a
    silent deferral) from a genuine failure."""

    def __init__(self, message: str, *, contended: bool = False):
        super().__init__(message)
        self.contended = contended


@dataclasses.dataclass(frozen=True)
class RecordIdentity:
    """The durable identity a cost record or cached row is decided under.

    ``k1`` names a logical ``(msg_id, req_id)``; ``p1`` a record lacking an id
    by its exact raw bytes at ``(loc, offset)`` (``projection`` additionally
    lets lookup fall back to a legacy decision at that location); ``l1`` a
    cached row lacking an id by its legacy projection."""

    form: str
    key: str
    msg_id: "str | None" = None
    req_id: "str | None" = None
    loc: "str | None" = None
    offset: "int | None" = None
    digest: "str | None" = None
    projection: "list | None" = None


@dataclasses.dataclass(frozen=True)
class StoredDecision:
    key: str
    account_key: "str | None"
    basis: str
    reason: "str | None"
    decided_at_utc: str
    journal_pos: tuple


@dataclasses.dataclass(frozen=True)
class PreserveResult:
    saved: int
    aligned: int
    exported: int
    last_id: int
    remaining: bool


# --------------------------------------------------------------------------
# identities
# --------------------------------------------------------------------------

def keyed_identity(msg_id: str, req_id: str) -> RecordIdentity:
    return RecordIdentity(form=K.FORM_KEYED, key=K.keyed_key(msg_id, req_id),
                          msg_id=msg_id, req_id=req_id)


def record_identity(*, msg_id, req_id, source_path: str, byte_offset: int,
                    record_bytes: bytes, projection: list) -> RecordIdentity:
    """A cost record's identity: keyed when both ids are valid record ids
    (``K.keyed_ids``), otherwise physical (exact raw bytes) with its
    projection for the legacy fallback. ``record_bytes`` is read only for a
    physical identity (the caller may pass None for a keyed record)."""
    ids = K.keyed_ids(msg_id, req_id)
    if ids is not None:
        return keyed_identity(*ids)
    loc = K.loc_for(source_path)
    digest = K.record_sha256(record_bytes)
    return RecordIdentity(
        form=K.FORM_PHYSICAL,
        key=K.physical_key_from_digest(loc, byte_offset, digest),
        loc=loc, offset=int(byte_offset), digest=digest,
        projection=list(projection))


def row_identity(row) -> RecordIdentity:
    """A cached accounting row's identity: keyed (both ids valid record ids,
    ``K.keyed_ids``), or the legacy projection of the row itself (an export
    never reads transcript bytes). A row stored before #944 F1 with an empty
    or non-string id therefore exports in legacy form, which is where the
    re-read record's physical lookup falls back to."""
    ids = K.keyed_ids(row["msg_id"], row["req_id"])
    if ids is not None:
        return keyed_identity(*ids)
    projection = K.legacy_projection(row)
    loc = K.loc_for(row["source_path"])
    digest = K.projection_sha256(projection)
    return RecordIdentity(
        form=K.FORM_LEGACY,
        key=K.legacy_key_from_digest(loc, row["line_offset"], digest),
        loc=loc, offset=int(row["line_offset"]), digest=digest,
        projection=projection)


# --------------------------------------------------------------------------
# schema and cache_meta
# --------------------------------------------------------------------------

def ensure_claude_attribution_schema(conn) -> None:
    import _cctally_db
    _cctally_db._apply_claude_attribution_schema(conn)


def schema_present(conn) -> bool:
    import _cctally_db
    return _cctally_db.claude_attribution_schema_present(conn)


def _meta_json(conn, key):
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _meta_set(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, sort_keys=True, separators=(",", ":"))))


def _now_iso() -> str:
    return _cctally_core.now_utc_iso()


def _bump_counter(conn, key, by: int, at) -> None:
    current = _meta_json(conn, key)
    count = 0
    if isinstance(current, dict) and type(current.get("count")) is int:
        count = current["count"]
    _meta_set(conn, key, {"count": count + int(by), "last_at": at})


def record_deferral(conn, reason: str) -> None:
    """Record ``claude_attribution_last_deferral`` in its own small
    transaction. Call with no transaction of the caller open."""
    _meta_set(conn, DEFERRAL_KEY, {"reason": reason, "at": _now_iso()})
    conn.commit()


def clear_deferral(conn) -> None:
    """Drop a recorded deferral, in the caller's transaction (a sync that
    appended successfully)."""
    conn.execute("DELETE FROM cache_meta WHERE key=?", (DEFERRAL_KEY,))


def deferral_recorded(conn) -> bool:
    return _meta_json(conn, DEFERRAL_KEY) is not None


def last_deferral(conn):
    """``claude_attribution_last_deferral`` as recorded, or None."""
    doc = _meta_json(conn, DEFERRAL_KEY)
    return doc if isinstance(doc, dict) else None


def read_preservation(conn):
    """Migration 050's ``claude_cost_preservation`` progress document
    (``afterId, saved, total, lastError, lastErrorAt, lastChunkAt``), or None
    before its first invocation."""
    doc = _meta_json(conn, PRESERVATION_KEY)
    return doc if isinstance(doc, dict) else None


def write_preservation(conn, doc) -> None:
    """Store the progress document in the caller's transaction."""
    _meta_set(conn, PRESERVATION_KEY, {
        key: doc.get(key) for key in (
            "afterId", "saved", "total", "lastError", "lastErrorAt",
            "lastChunkAt")})


def aligned_rows(conn) -> int:
    doc = _meta_json(conn, ALIGNED_KEY)
    if isinstance(doc, dict) and type(doc.get("count")) is int:
        return doc["count"]
    return 0


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def _admission_fingerprint(boundary):
    if boundary is None:
        return None
    revision = boundary.revision
    return [boundary.segment, int(boundary.end_offset),
            None if revision is None else revision.segment,
            None if revision is None else int(revision.end_offset)]


def _stored_cursor(conn, fingerprint):
    """The replay cursor, or ``None`` when it is absent, malformed, or was
    written under another rule version or admission boundary (each of which
    invalidates the derived tables)."""
    doc = _meta_json(conn, CURSOR_KEY)
    if not isinstance(doc, dict):
        return None
    if doc.get("rule") != K.RULE_VERSION or doc.get(
            "admission") != fingerprint:
        return None
    segment, offset = doc.get("segment"), doc.get("offset")
    if not isinstance(segment, str) or not segment or type(
            offset) is not int or offset < 0:
        return None
    return (segment, offset)


def _store_cursor(conn, hw, fingerprint) -> None:
    _meta_set(conn, CURSOR_KEY, {
        "segment": str(hw[0]), "offset": int(hw[1]),
        "rule": K.RULE_VERSION, "admission": fingerprint})


def _clear_derived(conn) -> None:
    conn.execute("DELETE FROM claude_cost_decisions")
    conn.execute("DELETE FROM claude_account_timeline")


def _is_claude_usage_obs(rec) -> bool:
    return (rec.get("t") == "obs" and rec.get("provider") == "claude"
            and rec.get("src") == "record-usage")


def _observation_row(rec):
    """``(captured_at_utc, precision_us, account_key)`` of an admissible
    timeline observation (spec 5.1), or ``None``."""
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        lo, hi = K.capture_interval(payload.get("captured_at"))
        binding = _lib_account_binding.load(payload.get("account_binding"))
    except (ValueError, TypeError, OverflowError, KeyError):
        return None
    account = K.normalize_binding_account(binding.account_key)
    top = rec.get("account")
    if top != account:
        # An omitted top-level stamp is NULL; anything else must equal the
        # binding's normalized account.
        return None
    return (K.iso_from_us(lo), hi - lo, account)


def _apply_observation(conn, rec, segment, offset) -> None:
    row = _observation_row(rec)
    if row is None:
        return
    captured, precision, account = row
    conn.execute(
        "INSERT INTO claude_account_timeline (captured_at_utc, precision_us,"
        " account_key, journal_segment, journal_offset) SELECT ?,?,?,?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM claude_account_timeline WHERE "
        "captured_at_utc=? AND journal_segment=? AND journal_offset=?)",
        (captured, precision, account, segment, int(offset),
         captured, segment, int(offset)))


def _item_values(item, payload):
    """``(decision_key, form, msg_id, req_id, loc, byte_offset, digest,
    record_ts_utc, projection_json)`` of one validated item."""
    form = item[0]
    if form == K.FORM_KEYED:
        ts = item[3] if len(item) == 4 else None
        return (K.keyed_key(item[1], item[2]), form, item[1], item[2],
                None, None, None, ts, None)
    if form == K.FORM_PHYSICAL:
        return (K.physical_key_from_digest(item[1], item[2], item[3]), form,
                None, None, item[1], item[2], item[3], item[4], None)
    return (K.legacy_key_from_digest(item[1], item[2], item[3]), form,
            None, None, item[1], item[2], item[3], None,
            K.projection_json(item[4]))


_DECISION_INSERT = (
    "INSERT INTO claude_cost_decisions (decision_key, form, msg_id, req_id,"
    " loc, byte_offset, digest, account_key, basis, reason, rule,"
    " decided_at_utc, record_ts_utc, projection_json, journal_segment,"
    " journal_offset) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(decision_key) DO NOTHING")


def _insert_decision_items(conn, payload, segment, offset) -> int:
    """Insert every item of one validated op first-wins; return how many
    contradicted an existing decision for the same key (conflicts)."""
    account = payload.get("account_key")
    conflicts = 0
    for item in payload["items"]:
        (key, form, msg_id, req_id, loc, byte_offset, digest, ts,
         projection) = _item_values(item, payload)
        before = conn.total_changes
        conn.execute(_DECISION_INSERT, (
            key, form, msg_id, req_id, loc, byte_offset, digest, account,
            payload["basis"], payload.get("reason"), payload["rule"],
            payload["decided_at"], ts, projection, segment, int(offset)))
        if conn.total_changes > before:
            continue
        prior = conn.execute(
            "SELECT account_key FROM claude_cost_decisions "
            "WHERE decision_key=?", (key,)).fetchone()
        if prior is not None and prior[0] != account:
            conflicts += 1
    return conflicts


def _decision_payload(rec):
    if rec.get("t") != "op":
        return None
    payload = rec.get("payload")
    if not isinstance(payload, dict) or payload.get(
            "kind") != _lib_journal.CLAUDE_COST_ACCOUNT_KIND:
        return None
    try:
        _lib_journal.validate_claude_cost_account(payload)
    except ValueError:
        return None
    return payload


def rehydrate_claude_attribution(conn, *, authoritative: bool
                                 ) -> "tuple | None":
    """Replay the journal suffix past the Claude attribution cursor into
    ``claude_cost_decisions`` and ``claude_account_timeline``, in ONE
    traversal. Returns the safe high-water the replay consumed --
    ``(segment, offset)``, or the current segment at offset 0 when no journal
    exists -- which every decision the caller then appends records as its
    ``journal_hw`` (spec 5.4: the safe high-water the rule ran against, not
    one sampled later, past another writer's appends). Returns None when the
    journal could not be read (the caller rolls back and defers).

    ``authoritative`` clears both tables and replays from the journal start
    (an explicit rebuild, the truncation escalation, a recreated cache). An
    absent or invalid cursor, or one written under another rule version or
    admission boundary, forces the same. Admission (``classify_admission``)
    runs before interpretation. A malformed decision payload is skipped, never
    applied. Conflicts -- a decision contradicting the first one recorded for
    its key -- are a pure function of the journal: an authoritative replay
    recounts them from zero, an incremental one adds to the count.

    Runs on the caller's transaction and never commits; the cursor and the
    rows it describes therefore commit together."""
    import _cctally_journal as jr
    try:
        hw = jr.journal_safe_high_water()
        boundary = None if hw is None else jr.find_account_bound_cutover()
    except (OSError, jr.JournalError):
        return None
    fingerprint = _admission_fingerprint(boundary)
    cursor = _stored_cursor(conn, fingerprint)
    if cursor is None:
        authoritative = True
    if hw is None:
        if authoritative:
            # No journal: the truth is that nothing has been decided, and a
            # cursor must never outlive the rows it describes.
            _clear_derived(conn)
            conn.execute("DELETE FROM cache_meta WHERE key=?", (CURSOR_KEY,))
            conn.execute("DELETE FROM cache_meta WHERE key=?",
                         (CONFLICTS_KEY,))
        return _empty_journal_high_water()
    if authoritative:
        _clear_derived(conn)
        cursor = None
    segments = jr.list_segments()
    conflicts = 0
    last_conflict_at = None
    try:
        for segment, offset, raw in jr.iter_range(cursor, hw, segments):
            decision_hit = _DECISION_MARKER in raw
            obs_hit = not decision_hit and all(
                marker in raw for marker in _OBS_MARKERS)
            if not (decision_hit or obs_hit):
                continue
            rec = _lib_journal.decode_line(raw)
            if rec is None:
                continue
            if boundary is not None and not (
                    _lib_account_protocol.classify_admission(
                        rec, _lib_account_protocol.Coordinate(
                            segment, int(offset)), boundary).admitted):
                continue
            if decision_hit:
                payload = _decision_payload(rec)
                if payload is not None:
                    found = _insert_decision_items(
                        conn, payload, segment, offset)
                    if found:
                        conflicts += found
                        last_conflict_at = payload["decided_at"]
                    continue
            if _is_claude_usage_obs(rec):
                _apply_observation(conn, rec, segment, offset)
    except OSError:
        return None
    if authoritative or cursor != (str(hw[0]), int(hw[1])):
        _store_cursor(conn, hw, fingerprint)
    if authoritative:
        _meta_set(conn, CONFLICTS_KEY,
                  {"count": conflicts, "last_at": last_conflict_at})
    elif conflicts:
        _bump_counter(conn, CONFLICTS_KEY, conflicts, last_conflict_at)
    return (str(hw[0]), int(hw[1]))


def decision_conflicts(conn) -> int:
    doc = _meta_json(conn, CONFLICTS_KEY)
    if isinstance(doc, dict) and type(doc.get("count")) is int:
        return doc["count"]
    return 0


# --------------------------------------------------------------------------
# timeline neighbours
# --------------------------------------------------------------------------

def _neighbours_with_evidence(conn, rec):
    rec_lo, rec_hi = int(rec[0]), int(rec[1])
    groups = {}

    def add(row):
        captured, precision, account, segment, offset = row
        lo = K.us_from_iso(captured)
        key = (lo, int(precision))
        entry = groups.setdefault(key, [set(), []])
        entry[0].add(account)
        entry[1].append([captured, segment, int(offset)])
        return lo, lo + int(precision)

    best = None  # (hi, lo, precision) of the latest instant before R
    for row in conn.execute(
            "SELECT captured_at_utc, precision_us, account_key,"
            " journal_segment, journal_offset FROM claude_account_timeline"
            " WHERE captured_at_utc < ? ORDER BY captured_at_utc DESC",
            (K.iso_from_us(rec_lo),)):
        lo = K.us_from_iso(row[0])
        if best is not None and lo + _MAX_PRECISION_US < best[0]:
            # No instant this early can end later than the latest one found,
            # nor reach into R.
            break
        lo, hi = add(row)
        if hi <= rec_lo and (best is None or (hi, lo) > best[:2]):
            best = (hi, lo, hi - lo)
    after = None
    for row in conn.execute(
            "SELECT captured_at_utc, precision_us, account_key,"
            " journal_segment, journal_offset FROM claude_account_timeline"
            " WHERE captured_at_utc >= ? ORDER BY captured_at_utc ASC",
            (K.iso_from_us(rec_lo),)):
        lo = K.us_from_iso(row[0])
        if after is not None and lo > after[0]:
            break
        lo, hi = add(row)
        if lo >= rec_hi:
            candidate = (lo, hi - lo)
            if after is None or candidate < after:
                after = candidate

    def instant(key):
        accounts, _evidence = groups[key]
        return K.Instant(key[0], key[1], frozenset(accounts))

    before = None if best is None else instant((best[1], best[2]))
    overlapping = tuple(
        instant(key) for key in sorted(groups)
        if K.relation((key[0], key[0] + key[1]), (rec_lo, rec_hi))
        == "overlapping")
    after_instant = None if after is None else instant(after)
    evidence = {key: value[1] for key, value in groups.items()}
    return K.Neighbours(before, overlapping, after_instant), evidence


def timeline_neighbours(conn, rec) -> K.Neighbours:
    """``Pb``, ``Ov`` and ``Sa`` of a record interval ``rec`` by indexed
    seeks around it (spec 5.2). The timeline is never loaded whole: the scan
    before ``R`` stops once no earlier instant can end later than the latest
    one found, and the scan after stops past the earliest instant after
    ``R``."""
    return _neighbours_with_evidence(conn, rec)[0]


def _supporting(evidence, neighbours) -> list:
    out = []
    instants = [neighbours.before, *neighbours.overlapping, neighbours.after]
    for instant in instants:
        if instant is None:
            continue
        out.extend(evidence.get((instant.lo_us, instant.precision_us), ()))
    return out


def neighbour_evidence(conn, rec, neighbours) -> list:
    """The ``[captured_at, segment, offset]`` entries supporting every instant
    of ``neighbours`` (an agreement's evidence)."""
    _found, evidence = _neighbours_with_evidence(conn, rec)
    return _supporting(evidence, neighbours)


def timeline_neighbours_with_evidence(conn, rec):
    """``(neighbours, evidence)`` of ``rec`` from ONE pair of seeks: the
    ``timeline_neighbours`` result and the supporting entries of every
    instant it names (what an agreement journals)."""
    found, evidence = _neighbours_with_evidence(conn, rec)
    return found, _supporting(evidence, found)


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

_DECISION_COLUMNS = ("decision_key, account_key, basis, reason,"
                     " decided_at_utc, journal_segment, journal_offset")


def _stored(row) -> StoredDecision:
    return StoredDecision(key=row[0], account_key=row[1], basis=row[2],
                          reason=row[3], decided_at_utc=row[4],
                          journal_pos=(row[5], int(row[6])))


def lookup_decision(conn, identity: RecordIdentity) -> "StoredDecision | None":
    """The decision ``identity`` holds, in the spec 5.3 lookup order: the
    keyed form, otherwise the physical form (exact raw bytes), then a legacy
    decision at the same ``(loc, offset)`` whose projection matches -- an
    exact match before a recorded-unknown-split one, then earliest journal
    position."""
    if identity.form in (K.FORM_KEYED, K.FORM_PHYSICAL):
        row = conn.execute(
            f"SELECT {_DECISION_COLUMNS} FROM claude_cost_decisions "
            "WHERE decision_key=?", (identity.key,)).fetchone()
        if row is not None:
            return _stored(row)
        if identity.form == K.FORM_KEYED:
            return None
    if identity.projection is None or identity.loc is None:
        return None
    candidates = []
    for row in conn.execute(
            f"SELECT {_DECISION_COLUMNS}, projection_json FROM "
            "claude_cost_decisions WHERE loc=? AND byte_offset=? AND form=?",
            (identity.loc, int(identity.offset), K.FORM_LEGACY)):
        try:
            projection = json.loads(row[7])
        except (TypeError, ValueError):
            continue
        kind = K.legacy_match(projection, list(identity.projection))
        candidates.append((kind, (row[5], int(row[6])), row))
    ordered = K.order_legacy_candidates(candidates)
    return _stored(ordered[0][2]) if ordered else None


# --------------------------------------------------------------------------
# append
# --------------------------------------------------------------------------

def _item_for(identity: RecordIdentity, basis: str, ts):
    if identity.form == K.FORM_KEYED:
        if basis == K.BASIS_PRESERVED:
            return ["k1", identity.msg_id, identity.req_id]
        return ["k1", identity.msg_id, identity.req_id, ts]
    if identity.form == K.FORM_PHYSICAL:
        return ["p1", identity.loc, int(identity.offset), identity.digest, ts]
    return ["l1", identity.loc, int(identity.offset), identity.digest,
            list(identity.projection)]


def _capture_doc(capture: "K.ClosingCapture | None"):
    if capture is None:
        return None
    doc = {"at": K.iso_from_us(capture.at_us)}
    if capture.account_key is not None:
        doc["account_key"] = capture.account_key
    return doc


def safe_high_water():
    """The journal's safe high-water, or the current segment at offset 0 when
    no journal exists yet; ``AttributionJournalUnavailable`` when unreadable."""
    import _cctally_journal as jr
    try:
        hw = jr.journal_safe_high_water()
    except OSError as exc:
        raise AttributionJournalUnavailable(
            f"the journal could not be read: {exc}") from exc
    if hw is None:
        return _empty_journal_high_water()
    return (str(hw[0]), int(hw[1]))


def _empty_journal_high_water():
    """The high-water of a journal that does not exist yet: the current
    segment at offset 0."""
    return (_lib_journal.segment_name(dt.datetime.now(dt.timezone.utc)), 0)


def _encoded_len(record) -> int:
    return len(_lib_journal.encode_line(_lib_journal.prepare_record(record)))


def _json_len(value) -> int:
    """The UTF-8 length of ``value`` in the journal's canonical encoding,
    which is also its length as an element of any list in a journal line."""
    return len(_lib_journal._canonical_json(value).encode("utf-8"))


#: What a line gains when its payload's ``evidence`` goes from absent to
#: present, beyond its entries and one comma each: ``,"evidence":[]`` less
#: the one comma the entries over-count (n entries need n-1 separators).
_EVIDENCE_KEY_LEN = len(',"evidence":[]') - 1


def _build_batches(groups, *, decided_at, journal_hw, closing_capture,
                   evidence):
    """One op per homogeneous group, split so every line stays under the
    budget. Returns ``[(record, [(identity, item), ...]), ...]``.

    The partition is the greedy rule: members are added in order, and one
    that would take the encoded line over ``_BATCH_LINE_BUDGET`` starts the
    next batch (a lone member is always emitted, even over budget). An
    agreement batch carries the deduplicated union of its members' evidence
    in first-seen order; other bases carry none.

    Linear: each batch's line length is measured once from a one-item
    skeleton and then grown by exact arithmetic (canonical JSON appends an
    element as its own encoding plus one comma), so validation runs once per
    emitted batch. Backstop: a built line whose length differs from the
    prediction, or that is over budget with more than one member, makes the
    group fall back to the exact greedy rebuild, so the budget can never be
    exceeded silently and the output stays the greedy rule's."""
    out = []
    for (basis, reason, account, uses_capture), members in groups.items():
        capture = _capture_doc(closing_capture) if uses_capture else None
        agreement = basis == K.BASIS_AGREEMENT

        def build(chunk, factory=_lib_journal.make_claude_cost_account):
            chunk_evidence = []
            seen = set()
            for _identity, _item, item_evidence in chunk:
                for entry in item_evidence:
                    marker = tuple(entry)
                    if marker not in seen:
                        seen.add(marker)
                        chunk_evidence.append(list(entry))
            return factory(
                basis=basis, reason=reason, account_key=account,
                decided_at=decided_at, journal_hw=journal_hw,
                closing_capture=capture,
                evidence=chunk_evidence if agreement else [],
                items=[item for _identity, item, _e in chunk])

        group_out = _linear_batches(members, build, agreement)
        if group_out is None:
            group_out = _greedy_batches(members, build)
        out.extend(group_out)
    return out


def _linear_batches(members, build, agreement):
    """The greedy partition of one group, sized by arithmetic; ``None`` when
    a built line disagrees with its predicted length (the backstop)."""
    budget = _BATCH_LINE_BUDGET
    out = []
    chunk, seen, length = [], set(), 0

    def emit():
        record = build(chunk)
        actual = _encoded_len(record)
        if actual != length or (len(chunk) > 1 and actual > budget):
            return False
        out.append((record, chunk))
        return True

    for member in members:
        item_evidence = member[2] if agreement else ()
        if chunk:
            fresh, fresh_seen = [], set()
            for entry in item_evidence:
                marker = tuple(entry)
                if marker not in seen and marker not in fresh_seen:
                    fresh_seen.add(marker)
                    fresh.append(entry)
            grown = length + _json_len(member[1]) + 1
            if fresh:
                grown += sum(_json_len(list(entry)) + 1 for entry in fresh)
                if not seen:
                    grown += _EVIDENCE_KEY_LEN
            if grown <= budget:
                chunk.append(member)
                seen |= fresh_seen
                length = grown
                continue
            if not emit():
                return None
        chunk = [member]
        seen = {tuple(entry) for entry in item_evidence}
        length = _encoded_len(build(
            chunk, _lib_journal.claude_cost_account_op_unvalidated))
    if chunk and not emit():
        return None
    return out


def _greedy_batches(members, build):
    """The exact greedy partition of one group, rebuilding the growing batch
    per member (quadratic); only the backstop of ``_linear_batches``."""
    out = []
    chunk = []
    for member in members:
        chunk.append(member)
        if len(chunk) > 1 and _encoded_len(build(chunk)) > (
                _BATCH_LINE_BUDGET):
            chunk.pop()
            out.append((build(chunk), chunk))
            chunk = [member]
    if chunk:
        out.append((build(chunk), chunk))
    return out


def append_decisions(conn, decisions: Sequence, *, journal_hw,
                     closing_capture: "K.ClosingCapture | None",
                     evidence: list) -> list:
    """Make ``decisions`` durable, journal first: append their
    ``claude_cost_account`` batches in ONE ``append_records`` call (fsync),
    then insert them into ``claude_cost_decisions`` on the caller's open
    transaction. Returns the ``StoredDecision`` each identity now holds, in
    input order (first-wins: an existing decision for the same key is kept).

    Each element is ``(identity, decision, evaluated_timestamp)`` or
    ``(identity, decision, evaluated_timestamp, item_evidence)``; the
    timestamp is ``None`` for ``preserved_history``. ``evidence`` is the
    supporting-observation list of every agreement lacking its own;
    ``closing_capture`` applies to decisions that used it. Batches group by
    basis, reason, account and closing capture.

    Raises ``AttributionJournalUnavailable`` when the append fails; nothing
    is inserted then."""
    if not decisions:
        return []
    import _cctally_journal as jr
    decided_at = _now_iso()
    groups = {}
    order = []
    for element in decisions:
        identity, decision, ts = element[0], element[1], element[2]
        item_evidence = element[3] if len(element) > 3 else evidence
        if decision.basis != K.BASIS_AGREEMENT:
            item_evidence = ()
        group = (decision.basis, decision.reason, decision.account_key,
                 bool(decision.used_closing_capture))
        groups.setdefault(group, []).append(
            (identity, _item_for(identity, decision.basis, ts),
             tuple(tuple(e) for e in (item_evidence or ()))))
        order.append(identity.key)
    hw = (str(journal_hw[0]), int(journal_hw[1]))
    batches = _build_batches(groups, decided_at=decided_at, journal_hw=hw,
                             closing_capture=closing_capture,
                             evidence=evidence)
    records = [_lib_journal.prepare_record(record) for record, _m in batches]
    lengths = [len(_lib_journal.encode_line(record)) for record in records]
    try:
        segment, end = jr.append_records(records)
    except (OSError, jr.JournalError) as exc:
        raise AttributionJournalUnavailable(
            f"the journal could not be written: {exc}") from exc
    start = end - sum(lengths)
    conflicts = 0
    for (record, _members), length in zip(batches, lengths):
        conflicts += _insert_decision_items(
            conn, record["payload"], segment, start)
        start += length
    if conflicts:
        _bump_counter(conn, CONFLICTS_KEY, conflicts, decided_at)
    stored = {}
    for key in order:
        if key in stored:
            continue
        row = conn.execute(
            f"SELECT {_DECISION_COLUMNS} FROM claude_cost_decisions "
            "WHERE decision_key=?", (key,)).fetchone()
        stored[key] = _stored(row)
    return [stored[key] for key in order]


# --------------------------------------------------------------------------
# alignment and preservation
# --------------------------------------------------------------------------

def _bump_mutation_seq(conn) -> int:
    """The ``session_entries`` mutation counter, advanced exactly as an
    in-place upsert advances it (``_cctally_cache._bump_mutation_seq``)."""
    row = conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET "
        "    value = CAST(cache_meta.value AS INTEGER) + 1 "
        "RETURNING value", (MUTATION_SEQ_KEY,)).fetchone()
    return int(row[0])


def align_row(conn, row_id: int, decision: StoredDecision) -> bool:
    """Link an unlinked accounting row to the decision its identity already
    holds, setting the row's account to the decision's (spec 5.4). An
    account change sets the row's mutation signal and advances the mutation
    counter exactly as a successful in-place upsert does, and is counted in
    ``claude_cost_aligned_rows``; a link alone changes no cost and no
    account, so it advances nothing. Returns whether the account changed.

    Accounts compare under the read rule (``K.preserved_stamp``): a literal
    ``unattributed`` or empty stamp already is the NULL account a decision
    carries, so its row keeps its bytes. Rewriting them would change no
    account but would drift a reviewed rederive's cache fingerprint."""
    row = conn.execute(
        "SELECT account_key FROM session_entries WHERE id=?",
        (int(row_id),)).fetchone()
    if row is None:
        return False
    changed = K.preserved_stamp(row[0]) != decision.account_key
    if changed:
        seq = _bump_mutation_seq(conn)
        conn.execute(
            "UPDATE session_entries SET account_key=?,"
            " attribution_decision_key=?, mutation_seq=?,"
            " mutation_min_ts=MIN(COALESCE(mutation_min_ts, timestamp_utc),"
            " timestamp_utc) WHERE id=?",
            (decision.account_key, decision.key, seq, int(row_id)))
        _bump_counter(conn, ALIGNED_KEY, 1, _now_iso())
    else:
        conn.execute(
            "UPDATE session_entries SET attribution_decision_key=? WHERE id=?",
            (decision.key, int(row_id)))
    return changed


def _unlinked_rows(conn, *, source_paths, after_id, limit):
    sql = (f"SELECT id, source_path, line_offset, msg_id, req_id, account_key,"
           f" {_PROJECTION_COLUMNS} FROM session_entries "
           "WHERE attribution_decision_key IS NULL AND id > ?")
    params = [int(after_id)]
    if source_paths is not None:
        sql += " AND source_path IN (%s)" % ",".join("?" * len(source_paths))
        params.extend(source_paths)
    sql += " ORDER BY id LIMIT ?"
    params.append(int(limit))
    cursor = conn.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _unlinked_remaining(conn, *, source_paths, after_id) -> bool:
    sql = ("SELECT 1 FROM session_entries WHERE attribution_decision_key "
           "IS NULL AND id > ?")
    params = [int(after_id)]
    if source_paths is not None:
        sql += " AND source_path IN (%s)" % ",".join("?" * len(source_paths))
        params.extend(source_paths)
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def preserve_unlinked_rows(conn, *, journal_hw,
                           source_paths: "Sequence[str] | None" = None,
                           after_id: int = 0, limit: "int | None" = None
                           ) -> PreserveResult:
    """Link every unlinked ``session_entries`` row (``id > after_id``, at
    most ``limit``, optionally only those of ``source_paths``) in ``id``
    order: a row whose identity already holds a decision is aligned to it;
    every other row's CURRENT stamp (NULL included) is exported as a
    ``preserved_history`` decision -- keyed form for a row with both ids,
    legacy projection form otherwise -- appended (fsync) before its link is
    set. Never consults the kernel and never reads transcript bytes.
    ``journal_hw`` is the high-water the caller's preceding replay
    (``rehydrate_claude_attribution``) consumed; every export records it.

    Runs on the caller's transaction and never commits, so the caller
    commits the links with whatever progress it records. Raises
    ``AttributionJournalUnavailable`` when an append fails; the caller rolls
    back, and decisions already appended are recovered by the next replay."""
    if source_paths is not None:
        source_paths = list(source_paths)
        if not source_paths:
            return PreserveResult(0, 0, 0, int(after_id), False)
    saved = aligned = exported = 0
    last_id = int(after_id)
    budget = None if limit is None else int(limit)
    hw = (str(journal_hw[0]), int(journal_hw[1]))
    while budget is None or budget > 0:
        size = _PRESERVE_CHUNK if budget is None else min(
            budget, _PRESERVE_CHUNK)
        rows = _unlinked_rows(conn, source_paths=source_paths,
                              after_id=last_id, limit=size)
        if not rows:
            break
        to_export = []
        for row in rows:
            identity = row_identity(row)
            found = lookup_decision(conn, identity)
            if found is not None:
                align_row(conn, row["id"], found)
                aligned += 1
            else:
                to_export.append((row, identity))
        if to_export:
            # One identity can appear once per chunk at most: every identity
            # names at most one accounting row (spec 5.4).
            # A literal ``unattributed`` stamp (read paths treat it as NULL),
            # an empty or a non-string stamp is preserved as NULL: a decision
            # never carries any of them. Its row keeps its bytes (align_row).
            stored = append_decisions(
                conn,
                [(identity, K.Decision(
                    K.preserved_stamp(row["account_key"]),
                    K.BASIS_PRESERVED, None, False), None)
                 for row, identity in to_export],
                journal_hw=hw, closing_capture=None, evidence=[])
            for (row, _identity), decision in zip(to_export, stored):
                if decision.account_key == K.preserved_stamp(
                        row["account_key"]):
                    conn.execute(
                        "UPDATE session_entries SET attribution_decision_key=?"
                        " WHERE id=?", (decision.key, row["id"]))
                else:
                    align_row(conn, row["id"], decision)
            exported += len(to_export)
        saved += len(rows)
        last_id = int(rows[-1]["id"])
        if budget is not None:
            budget -= len(rows)
    remaining = _unlinked_remaining(conn, source_paths=source_paths,
                                    after_id=last_id)
    return PreserveResult(saved, aligned, exported, last_id, remaining)
