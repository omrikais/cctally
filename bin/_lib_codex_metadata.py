"""Decode-tolerant Codex thread project metadata (#845).

Codex persists a conversation's working directory and git context as
``codex_conversation_threads.cwd`` and ``.git_json``, both TEXT. Python's
``sqlite3`` decodes TEXT with the default ``str`` factory and raises
``OperationalError: Could not decode to UTF-8`` on any statement that selects
the column, so one undecodable byte failed every reader that touched the two
columns — whatever the row's age, and whatever else the reader was doing.

Every in-scope statement therefore selects ``CAST(t.cwd AS BLOB) AS cwd_blob``
and ``CAST(t.git_json AS BLOB) AS git_json_blob`` and passes both through
``decode_codex_project_metadata``. The columns are named ``cwd_blob`` and
``git_json_blob`` so a reader that forgets to decode fails with a ``KeyError``
instead of silently receiving bytes. The shared connection's ``text_factory``
is never changed: it is process-wide state a concurrent reader also depends on.

This module is a stdlib-only leaf. It imports nothing from the rest of the
package, so the qualified accounting reader, the dashboard conversation map,
the two cache-side rollup writers and the Conversation Viewer can all call one
implementation of the rule without an import cycle.
"""
from __future__ import annotations

import dataclasses
import sqlite3


#: The physical name of the raw threads table. ``resolve_codex_threads_table``
#: returns it qualified by whichever schema actually holds it.
CODEX_THREADS_TABLE = "codex_conversation_threads"


@dataclasses.dataclass(frozen=True)
class DecodedCodexProjectMetadata:
    """One thread row's two project-metadata fields, decoded field by field.

    ``undecodable_fields`` names the fields whose stored bytes are not valid
    UTF-8. Such a field is present in the store and unreadable here, which is
    a third state beside "absent" and "usable": its own value is ``None``, so a
    caller that ignores the set degrades it to absent, and the malformed
    predicate below is what tells the two apart.
    """

    cwd: str | None
    git_json: str | None
    undecodable_fields: frozenset[str]


def _decode_field(value: "bytes | str | None", name: str) -> "tuple[str | None, bool]":
    """Decode one stored field; report whether its bytes are undecodable."""
    if value is None:
        return None, False
    if isinstance(value, str):
        # A caller that already holds a decoded value (a fixture builder, or a
        # store whose text_factory a test changed) passes it verbatim.
        return value, False
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8"), False
        except UnicodeDecodeError:
            return None, True
    raise TypeError(
        f"{name} must be bytes, str or None, not {type(value).__name__}; "
        "an in-scope statement selects the column as CAST(... AS BLOB)"
    )


def decode_codex_project_metadata(
    cwd_blob: "bytes | str | None", git_json_blob: "bytes | str | None",
) -> DecodedCodexProjectMetadata:
    """Decode one thread row's ``cwd`` and ``git_json`` independently.

    ``bytes`` is decoded with strict UTF-8; a failure records the field name in
    ``undecodable_fields`` and leaves that field ``None``. ``str`` is accepted
    verbatim and ``None`` stays ``None``. The two fields never share a verdict:
    a row whose ``git_json`` is corrupt but whose ``cwd`` reads fine is still
    attributable, because the resolver stops at a usable ``cwd``.
    """
    cwd, cwd_bad = _decode_field(cwd_blob, "cwd")
    git_json, git_bad = _decode_field(git_json_blob, "git_json")
    undecodable = set()
    if cwd_bad:
        undecodable.add("cwd")
    if git_bad:
        undecodable.add("git_json")
    return DecodedCodexProjectMetadata(
        cwd=cwd, git_json=git_json, undecodable_fields=frozenset(undecodable),
    )


def _absent(metadata: "DecodedCodexProjectMetadata | None", field: str) -> bool:
    """Whether the resolver would move past this field.

    "Absent" means the row is missing, or the field is NULL or empty — exactly
    the values ``cwd = direct.cwd or inherited.cwd`` skips. An undecodable
    field is absent by this test too, which is why the caller checks
    ``_undecodable`` first.
    """
    if metadata is None:
        return True
    return not getattr(metadata, field)


def _undecodable(metadata: "DecodedCodexProjectMetadata | None", field: str) -> bool:
    return metadata is not None and field in metadata.undecodable_fields


def codex_metadata_is_malformed(
    direct: "DecodedCodexProjectMetadata | None",
    inherited: "DecodedCodexProjectMetadata | None",
) -> bool:
    """Whether the resolver's path reaches an undecodable field (§4.1).

    ``_qualify_codex_rows`` merges each field independently, ``cwd =
    direct.cwd or inherited.cwd`` and ``git_json = direct.git_json or
    inherited.git_json``, and ``_CodexProjectResolution.resolve`` stops at a
    usable merged ``cwd`` and consults ``git_json`` only otherwise. A field is
    "consulted" when the resolver's outcome depends on it, and an undecodable
    field is present but unreadable, so whenever the path reaches one the entry
    cannot be attributed:

    - **E1.** Direct ``cwd`` undecodable.
    - **E2.** Direct ``cwd`` absent and inherited ``cwd`` undecodable.
    - **E3.** Direct and inherited ``cwd`` absent, direct ``git_json``
      undecodable.
    - **E4.** Direct and inherited ``cwd`` absent, direct ``git_json`` absent,
      inherited ``git_json`` undecodable.

    Otherwise the entry is attributable, which is the half that keeps healthy
    rows healthy: a valid direct ``cwd`` beside an undecodable direct
    ``git_json`` still resolves, and so does a valid inherited ``cwd`` beside
    an undecodable inherited ``git_json``.

    The qualified reader raises for a malformed entry, the counter counts it,
    the identity map withholds its attribution and the viewer's key set admits
    it, and all four call THIS function with the entry's direct row and its
    path's inherited winner, so the refusal and the count cannot disagree.
    """
    if _undecodable(direct, "cwd"):
        return True
    if not _absent(direct, "cwd"):
        return False
    if _undecodable(inherited, "cwd"):
        return True
    if not _absent(inherited, "cwd"):
        return False
    if _undecodable(direct, "git_json"):
        return True
    if not _absent(direct, "git_json"):
        return False
    return _undecodable(inherited, "git_json")


def _schema_holds_threads_table(conn: sqlite3.Connection, schema: str) -> bool:
    """Whether ``schema`` holds the threads table; a read failure propagates.

    ``main`` always exists and ``cache_db`` is probed only after
    ``PRAGMA database_list`` named it, so the only ``sqlite3.Error`` this
    statement can raise is a store the connection cannot read (locked,
    malformed). Swallowing it here answered ``False``, which the resolver
    turned into its absent-table ``RuntimeError`` and the two fail-closed
    reads (#850 M5) then answered with the healthy value; a locked store hits
    this probe before it hits their own statement.
    """
    row = conn.execute(
        f'SELECT 1 FROM "{schema}".sqlite_master '
        "WHERE type = 'table' AND name = ?",
        (CODEX_THREADS_TABLE,),
    ).fetchone()
    return row is not None


def resolve_codex_threads_table(conn: sqlite3.Connection) -> str:
    """The RAW threads table reference for this connection, resolved at call time.

    A bare cache connection (``open_cache_db()``) holds the table in its own
    ``main`` schema; a conversations connection
    (``open_conversations_db``/``open_conversations_db_readonly``) reaches it
    through the cache attached as ``cache_db``. A qualified reference resolves
    to the real table even when account scoping has installed an empty TEMP
    view over the unqualified name, which is what lets the fail-closed
    anonymization count and the viewer's key set be observed by a scoped
    request too.

    Readers that must HONOR scoping keep the unqualified name; this is only for
    readers that must see the raw rows. A hard-coded ``cache_db.`` prefix fails
    with ``no such table`` on the bare cache connection, which is why the
    reference is resolved rather than written out.

    ``RuntimeError`` means exactly one thing: the table is absent from every
    schema this connection can see. A ``sqlite3.Error`` raised while
    resolving is a read the store could not perform and propagates unchanged,
    so a caller that answers the absent-table case with a healthy value
    (zero undecodable rows, the empty key set) never gives that answer for a
    locked or malformed store.
    """
    if _schema_holds_threads_table(conn, "main"):
        return f"main.{CODEX_THREADS_TABLE}"
    attached = tuple(conn.execute("PRAGMA database_list"))
    for row in attached:
        name = str(row[1]) if len(row) > 1 else ""
        if name == "cache_db" and _schema_holds_threads_table(conn, name):
            return f"cache_db.{CODEX_THREADS_TABLE}"
    raise RuntimeError(
        "the Codex threads table is not present in this connection's main "
        "schema and no cache database is attached as cache_db"
    )
