"""Pure planning kernel for in-place stats index publication (#496 S3).

No I/O and no SQLite connection: every function takes plain data so the
publication protocol's decisions can be tested without a database.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

_INTERNAL = "sqlite_"
_VIRTUAL = re.compile(r"CREATE\s+VIRTUAL\s+TABLE", re.IGNORECASE)
# `INSERT INTO t SELECT * FROM src.t` is not valid for a generated column, so a
# table carrying one is refused rather than mis-copied. Both spellings SQLite
# accepts are matched, and the pattern is anchored on what actually precedes the
# `AS (` of a generated column: either the `GENERATED ALWAYS` keywords, or the
# end of a column's type/constraint text at a `,` or `(` boundary. A bare
# `\bAS\s*\(` would also match a `CHECK (x AS (…))`-shaped expression or any
# future DDL that merely contains those characters, and the refusal is a hard,
# non-fallback-eligible raise — so a false positive would abort publication on a
# table the copy handles perfectly well.
_GENERATED = re.compile(
    r"GENERATED\s+ALWAYS\s+AS\s*\("
    r"|[(,]\s*\"?\w+\"?(?:[^,()]|\([^()]*\))*?\bAS\s*\(",
    re.IGNORECASE,
)

# Drop order: dropping a table already removes its own indexes and triggers, so
# the dependent classes go first and a precomputed flat list never touches an
# object that no longer exists.
_DROP_ORDER = (
    ("view", "DROP VIEW IF EXISTS"),
    ("trigger", "DROP TRIGGER IF EXISTS"),
    ("index", "DROP INDEX IF EXISTS"),
    ("table", "DROP TABLE IF EXISTS"),
)
# Create order for everything that is not a table: after the rows, so index
# builds are single-pass, and indexes before triggers before views.
_CREATE_ORDER = ("index", "trigger", "view")


@dataclasses.dataclass(frozen=True)
class GenerationSwapPlan:
    """One transaction's worth of statements, in the order they must run."""

    drop_statements: tuple
    create_table_statements: tuple
    copy_tables: tuple
    # Indexes, then triggers, then views — every non-table object, created
    # after the row copy. Named for the only class the stats schema uses today.
    create_index_statements: tuple
    rejected: tuple


def _usable(objects):
    """Objects a swap may act on: named by the user, and carrying their DDL.

    `sqlite_sequence` cannot be dropped, and an automatic index created by a
    UNIQUE constraint has `sql IS NULL` and no independent existence.
    """
    for kind, name, sql in objects:
        if str(name).startswith(_INTERNAL):
            continue
        if sql is None:
            continue
        yield str(kind), str(name), str(sql)


def _unsupported(kind: str, sql: str) -> bool:
    if _VIRTUAL.search(sql):
        return True
    return kind == "table" and bool(_GENERATED.search(sql))


def plan_generation_swap(dest_objects, src_objects) -> GenerationSwapPlan:
    """Plan the swap of ``dest_objects`` for ``src_objects``.

    Each argument is a sequence of ``(type, name, sql)`` triples exactly as
    ``SELECT type, name, sql FROM sqlite_schema`` returns them. Deriving the
    drop list from the destination and the create list from the source retires
    whatever the live generation holds without a maintained table of names, so
    a table added in a later epoch cannot be forgotten.
    """
    rejected = tuple(
        name
        for kind, name, sql in _usable(src_objects)
        if _unsupported(kind, sql)
    )

    drops = []
    for wanted, statement in _DROP_ORDER:
        for kind, name, _sql in _usable(dest_objects):
            if kind == wanted:
                drops.append(f'{statement} "{name}"')

    creates, copies = [], []
    others: dict = {kind: [] for kind in _CREATE_ORDER}
    for kind, name, sql in _usable(src_objects):
        if _unsupported(kind, sql):
            continue
        if kind == "table":
            creates.append(sql)
            copies.append(name)
        elif kind in others:
            others[kind].append(sql)

    post = [sql for kind in _CREATE_ORDER for sql in others[kind]]
    return GenerationSwapPlan(
        drop_statements=tuple(drops),
        create_table_statements=tuple(creates),
        copy_tables=tuple(copies),
        create_index_statements=tuple(post),
        rejected=rejected,
    )


# --------------------------------------------------------------------------
# The publication phase machine (#496 S3 §4.1)
# --------------------------------------------------------------------------
#
# "The transaction raised" is not a safe discriminator, because a failure
# before the commit can roll back while a failure after it cannot, and a
# commit-time I/O error leaves the outcome genuinely unknown. The publisher
# therefore records which of these four phases it had reached when it failed.

# Rollback is proven, the old generation is intact, and physical fallback is
# legal.
PRE_COMMIT = "pre_commit"
# The commit's outcome is undetermined. Do not fall back; reopen and resolve
# through the publication stamp.
COMMIT_UNKNOWN = "commit_unknown"
# The new generation is live. Physical fallback is never legal from here.
COMMITTED = "committed"
# The record and the marker agree with the bytes.
VERDICT_SETTLED = "verdict_settled"

# Only a structural inability to operate on the destination authorizes
# discarding it. Busy, full, out-of-memory and I/O-resource failures leave a
# perfectly good generation live and must return a retryable failure instead.
# The structural tokens are `_cctally_db._SQLITE_CORRUPTION_MESSAGES` plus the
# encrypted-file spelling of SQLITE_NOTADB.
_STRUCTURAL = (
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
    "encrypted or is not a database",
)
_RETRYABLE = (
    "locked",
    "busy",
    "full",
    "out of memory",
    "i/o error",
    "readonly",
    "permission denied",
)


# --------------------------------------------------------------------------
# Three-state stamp resolution (#496 S3 §5)
# --------------------------------------------------------------------------
#
# An in-place publish attaches its scratch read-only and detaches it, so the
# scratch survives commit and rollback identically and the publication marker's
# `scratchPath` crash discriminator inverts. The publication writes its own
# identity into `stats_publication_stamp` inside the publication transaction
# instead, and the opener compares that against the marker's `recordPath`.
#
# The answer is three-valued, not boolean. "The stamp does not name this
# record" proves a rollback only if the stamp was READ successfully; a missing
# table, a corrupt page, an unreadable schema, a malformed or duplicated row,
# or any query error proves nothing at all. Treating one of those as
# never-committed would discard a verdict owed on bytes that are live.

#: The stamp names this marker's rebuild record: the publication COMMITTED.
STAMP_MATCH = "MATCH"
#: The stamp was read and does not name this record: the publication never
#: became live, and the live bytes are its untouched predecessor.
STAMP_PROVEN_PREDECESSOR = "PROVEN_PREDECESSOR"
#: The stamp could not be read, or read as something it may not be. Preserve
#: the marker and fail closed; only PROVEN_PREDECESSOR may discard it.
STAMP_INDETERMINATE = "INDETERMINATE"


def resolve_stamp(stamp, marker_record_path) -> str:
    """Resolve a pending publication against the stamp read from its destination.

    ``stamp`` is whatever the read produced: the exception that prevented it,
    ``None`` or an empty sequence when the table read cleanly and held no row,
    a single row mapping, or the sequence of row mappings the table held. Each
    mapping carries at least ``record_path``.

    ``marker_record_path`` is the marker's ``recordPath`` as a string.

    Returns one of `STAMP_MATCH`, `STAMP_PROVEN_PREDECESSOR` or
    `STAMP_INDETERMINATE`. Anything the function cannot interpret resolves
    INDETERMINATE, because that is the state that preserves evidence.
    """
    if isinstance(stamp, BaseException):
        return STAMP_INDETERMINATE
    if not isinstance(marker_record_path, str) or not marker_record_path:
        return STAMP_INDETERMINATE
    if stamp is None:
        return STAMP_PROVEN_PREDECESSOR
    if isinstance(stamp, (list, tuple)):
        if not stamp:
            return STAMP_PROVEN_PREDECESSOR
        if len(stamp) != 1:
            # The publication transaction deletes before it inserts, so more
            # than one row is a state the protocol cannot produce.
            return STAMP_INDETERMINATE
        stamp = stamp[0]
    if not isinstance(stamp, dict):
        return STAMP_INDETERMINATE
    recorded = stamp.get("record_path")
    if not isinstance(recorded, str) or not recorded:
        return STAMP_INDETERMINATE
    if recorded == marker_record_path:
        return STAMP_MATCH
    return STAMP_PROVEN_PREDECESSOR


def may_fall_back_to_replacement(exc) -> bool:
    """Whether ``exc`` authorizes physically replacing the destination.

    Fails closed: anything unrecognized returns False, leaving the old
    generation live and the failure retryable. SQLite's numeric code is
    preferred where the exception still carries one, mirroring
    ``_cctally_db._is_sqlite_corruption_error``; the canonical messages are the
    string-only boundary.
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        primary = code & 0xFF
        if primary in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            return True
        return False
    text = str(exc).casefold()
    if any(token in text for token in _RETRYABLE):
        return False
    return any(token in text for token in _STRUCTURAL)
