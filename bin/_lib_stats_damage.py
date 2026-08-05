"""Pure damage-characterization kernel for a corrupt SQLite index (#496 S1 F8).

Two independent sources feed one structured description.

`parse_integrity_rows` converts the row forms `PRAGMA integrity_check` emits
into typed findings. That covers only the minority of incidents: in 54 of the
74 retained production forensics bundles the pragma RAISED before producing any
row, so the bundle holds the plain string ``error: database disk image is
malformed`` and nothing can be derived from it.

`scan_sqlite_file` covers the rest by reading the file itself — the 100-byte
header, then the `sqlite_schema` b-tree rooted at page 1 — and probing the type
byte of every root page that schema names. It opens no SQLite connection, so it
still describes a file SQLite refuses to open.

`shape_token` normalizes either source into a short equality-comparable token
with page numbers, cell indices and rowids removed, so a recurring damage class
is detectable by comparison rather than by reading prose.

Nothing in this module raises. A rebuild must never fail because diagnostic
enrichment failed, so every failure path returns an ``unavailable`` method with
a bounded reason string.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

SCHEMA_VERSION = 1

#: Finding keys are always all present, so consumers never need ``.get``.
_FINDING_KEYS = ("kind", "table", "index", "column", "page", "cell", "rowid", "raw")

_SQLITE_MAGIC = b"SQLite format 3\x00"

#: Leaf/interior table (0x0d/0x05) and leaf/interior index (0x0a/0x02) pages.
_TABLE_PAGE_TYPES = frozenset({0x0D, 0x05})
_INDEX_PAGE_TYPES = frozenset({0x0A, 0x02})

#: Derived, never a separate literal: `observed` below reads "not a table page"
#: as "an index page", which is only sound while these three agree.
_VALID_PAGE_TYPES = _TABLE_PAGE_TYPES | _INDEX_PAGE_TYPES

#: Bounds the `sqlite_schema` walk so a corrupt child pointer cannot make the
#: scan read the whole file.
_MAX_SCHEMA_PAGES = 4096

_REASON_MAX = 200


class _ScanError(Exception):
    """Internal: the raw scan cannot proceed. Never escapes this module."""


def _finding(kind: str, raw: str, **fields) -> dict:
    out = {key: None for key in _FINDING_KEYS}
    out["kind"] = kind
    out["raw"] = raw
    out.update(fields)
    return out


def _reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if len(text) <= _REASON_MAX:
        return text
    return text[: _REASON_MAX - 1] + "…"


# ==========================================================================
# integrity_check row parsing
# ==========================================================================

# The closed set of forms the production corpus actually contains. Anything
# else is retained verbatim as `unparsed` rather than discarded.
_RE_INDEX_ENTRY_COUNT = re.compile(
    r"^wrong # of entries in index (?P<index>\S+)$"
)
_RE_ROW_MISSING = re.compile(
    r"^row (?P<rowid>\d+) missing from index (?P<index>\S+)$"
)
_RE_NON_UNIQUE = re.compile(
    r"^non-unique entry in index (?P<index>\S+)$"
)
_RE_COLUMN_VALUE = re.compile(
    r"^(?P<what>NULL|NUMERIC|TEXT|BLOB|REAL|INTEGER) value in "
    r"(?P<table>[^.\s]+)\.(?P<column>\S+)$"
)
_RE_TREE_CELL = re.compile(
    r"^Tree (?P<tree>\d+) page (?P<page>\d+) cell (?P<cell>\d+): (?P<detail>.+)$"
)
_RE_TREE_PAGE = re.compile(
    r"^Tree (?P<tree>\d+) page (?P<page>\d+): (?P<detail>.+)$"
)
_RE_PAGE_NEVER_USED = re.compile(r"^Page (?P<page>\d+): never used$")
_RE_PAGE_DETAIL = re.compile(r"^Page (?P<page>\d+): (?P<detail>.+)$")
_RE_DATABASE_BANNER = re.compile(r"^\*\*\* in database \S+ \*\*\*$")


def parse_integrity_rows(rows) -> list:
    """Convert `PRAGMA integrity_check` output into typed findings.

    ``rows`` is the value the forensics bundle stores: a list of strings when
    the pragma returned rows, the captured error string when it raised, or
    ``None`` when it never ran. Only the list form can yield findings.
    """
    if not isinstance(rows, (list, tuple)):
        return []
    findings = []
    for row in rows:
        text = str(row).strip()
        if not text:
            continue
        if text.casefold() == "ok" or _RE_DATABASE_BANNER.match(text):
            continue

        match = _RE_INDEX_ENTRY_COUNT.match(text)
        if match:
            findings.append(
                _finding("index_entry_count", text, index=match["index"])
            )
            continue

        match = _RE_ROW_MISSING.match(text)
        if match:
            findings.append(
                _finding(
                    "row_missing_from_index",
                    text,
                    index=match["index"],
                    rowid=int(match["rowid"]),
                )
            )
            continue

        match = _RE_NON_UNIQUE.match(text)
        if match:
            findings.append(
                _finding("index_non_unique", text, index=match["index"])
            )
            continue

        match = _RE_COLUMN_VALUE.match(text)
        if match:
            kind = "null_value" if match["what"] == "NULL" else "type_mismatch"
            findings.append(
                _finding(
                    kind, text, table=match["table"], column=match["column"],
                )
            )
            continue

        match = _RE_TREE_CELL.match(text)
        if match:
            findings.append(
                _finding(
                    "tree_cell",
                    text,
                    page=int(match["page"]),
                    cell=int(match["cell"]),
                )
            )
            continue

        match = _RE_TREE_PAGE.match(text)
        if match:
            kind = (
                "btree_init_error"
                if "btreeInitPage()" in match["detail"]
                else "tree_page"
            )
            findings.append(_finding(kind, text, page=int(match["page"])))
            continue

        match = _RE_PAGE_NEVER_USED.match(text)
        if match:
            findings.append(
                _finding("page_never_used", text, page=int(match["page"]))
            )
            continue

        match = _RE_PAGE_DETAIL.match(text)
        if match:
            findings.append(
                _finding("page_detail", text, page=int(match["page"]))
            )
            continue

        findings.append(_finding("unparsed", text))
    return findings


# ==========================================================================
# raw file scan
# ==========================================================================

def _varint(buf: bytes, pos: int) -> tuple:
    value = 0
    for index in range(9):
        if pos + index >= len(buf):
            raise _ScanError("varint runs past the end of the page")
        byte = buf[pos + index]
        if index == 8:
            return ((value << 8) | byte), pos + 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos + index + 1
    raise _ScanError("malformed varint")


def _serial_size(serial: int) -> int:
    if serial in (0, 8, 9, 10, 11):
        return 0
    if serial <= 4:
        return serial
    if serial == 5:
        return 6
    if serial == 6 or serial == 7:
        return 8
    return (serial - 12) // 2


def _serial_int(buf: bytes, offset: int, serial: int) -> "int | None":
    if serial == 8:
        return 0
    if serial == 9:
        return 1
    size = _serial_size(serial)
    if serial > 6 or size == 0:
        return None
    if offset + size > len(buf):
        raise _ScanError("integer column runs past the end of the page")
    return int.from_bytes(buf[offset:offset + size], "big", signed=True)


def _serial_text(buf: bytes, offset: int, serial: int) -> "str | None":
    if serial < 13 or serial % 2 == 0:
        return None
    size = _serial_size(serial)
    if offset + size > len(buf):
        raise _ScanError("text column runs past the end of the page")
    return buf[offset:offset + size].decode("utf-8", "replace")


def _read_page(handle, page_number: int, page_size: int) -> bytes:
    handle.seek((page_number - 1) * page_size)
    data = handle.read(page_size)
    if len(data) != page_size:
        raise _ScanError(f"page {page_number} is short or absent")
    return data


def _parse_schema_cell(page: bytes, offset: int, objects: dict) -> None:
    """Read `(type, name, rootpage, sql)` out of one `sqlite_schema` leaf cell.

    The `sql` text is needed only to recognise a `WITHOUT ROWID` table, whose
    root legitimately IS an index b-tree. It is the last column, so a payload
    that overflows the page yields `None` and the caller declines to judge that
    object's page kind rather than reporting a healthy table as damaged.
    """
    _payload_size, pos = _varint(page, offset)
    _rowid, pos = _varint(page, pos)
    header_size, header_pos = _varint(page, pos)
    header_end = pos + header_size
    serials = []
    cursor = header_pos
    while cursor < header_end and len(serials) < 5:
        serial, cursor = _varint(page, cursor)
        serials.append(serial)
    if len(serials) < 4:
        raise _ScanError("sqlite_schema record has fewer than four columns")
    value_offsets = []
    body = header_end
    for serial in serials:
        value_offsets.append(body)
        body += _serial_size(serial)
    obj_type = _serial_text(page, value_offsets[0], serials[0])
    name = _serial_text(page, value_offsets[1], serials[1])
    rootpage = _serial_int(page, value_offsets[3], serials[3])
    sql = None
    if len(serials) == 5:
        try:
            sql = _serial_text(page, value_offsets[4], serials[4])
        except (_ScanError, IndexError, ValueError):
            sql = None
    if not name or not obj_type or not rootpage or rootpage < 1:
        return
    objects[name] = (obj_type, int(rootpage), sql)


def _walk_schema_btree(
    handle, page_number: int, page_size: int, objects: dict, visited: set,
) -> None:
    if page_number in visited:
        raise _ScanError(f"sqlite_schema b-tree revisits page {page_number}")
    visited.add(page_number)
    if len(visited) > _MAX_SCHEMA_PAGES:
        raise _ScanError("sqlite_schema b-tree exceeds the page budget")
    page = _read_page(handle, page_number, page_size)
    base = 100 if page_number == 1 else 0
    if base >= len(page):
        raise _ScanError("page 1 is shorter than the file header")
    page_type = page[base]
    cell_count = int.from_bytes(page[base + 3:base + 5], "big")
    if page_type == 0x0D:
        pointer_base = base + 8
        for index in range(cell_count):
            at = pointer_base + 2 * index
            if at + 2 > len(page):
                raise _ScanError("cell pointer array runs past the page")
            cell_offset = int.from_bytes(page[at:at + 2], "big")
            # One unreadable row must not discard the whole map.
            try:
                _parse_schema_cell(page, cell_offset, objects)
            except (_ScanError, IndexError, ValueError):
                continue
    elif page_type == 0x05:
        pointer_base = base + 12
        children = []
        for index in range(cell_count):
            at = pointer_base + 2 * index
            if at + 2 > len(page):
                raise _ScanError("cell pointer array runs past the page")
            cell_offset = int.from_bytes(page[at:at + 2], "big")
            if cell_offset + 4 > len(page):
                raise _ScanError("interior cell runs past the page")
            children.append(
                int.from_bytes(page[cell_offset:cell_offset + 4], "big")
            )
        children.append(int.from_bytes(page[base + 8:base + 12], "big"))
        for child in children:
            if child < 1:
                raise _ScanError("interior page names child page 0")
            _walk_schema_btree(handle, child, page_size, objects, visited)
    else:
        raise _ScanError(
            f"page {page_number} is not a table b-tree page "
            f"(type byte 0x{page_type:02x})"
        )


def _unavailable(reason: str) -> dict:
    return {"method": "unavailable", "findings": [], "reason": reason}


#: `WITHOUT ROWID` as a table-option, i.e. after the column list closes.
_WITHOUT_ROWID_TAIL = re.compile(r"WITHOUT\s+ROWID", re.IGNORECASE)


def _is_without_rowid(sql) -> bool:
    """True only when the table-options tail declares `WITHOUT ROWID`.

    Searching the whole statement would exempt an ordinary rowid table whose
    declaration merely MENTIONS the phrase — in a comment, a CHECK literal or a
    DEFAULT literal — and a table exempted that way is reported as healthy no
    matter how damaged it is, with no signal anywhere. Anchoring past the final
    closing parenthesis costs the `) WITHOUT /*c*/ ROWID` form, which fails
    toward declining to judge rather than toward a false clean bill.
    """
    if not sql:
        return False
    text = str(sql)
    close = text.rfind(")")
    if close < 0:
        return False
    return _WITHOUT_ROWID_TAIL.search(text[close + 1:]) is not None


def scan_sqlite_file(path) -> dict:
    """Describe a SQLite file by reading its own bytes. Never raises.

    Returns ``{"method": "raw_scan"|"unavailable", "findings": [...],
    "reason": str | None}``. Pages are read individually by seek; the file is
    never loaded.
    """
    if path is None:
        return _unavailable("ValueError: no path was supplied")
    try:
        target = pathlib.Path(path)
        with target.open("rb") as handle:
            header = handle.read(100)
            if len(header) < 100:
                raise _ScanError("file is shorter than the 100-byte header")
            if not header.startswith(_SQLITE_MAGIC):
                raise _ScanError("file does not carry the SQLite header magic")
            raw_page_size = int.from_bytes(header[16:18], "big")
            page_size = 65536 if raw_page_size == 1 else raw_page_size
            if page_size < 512 or (page_size & (page_size - 1)) != 0:
                raise _ScanError(f"header declares an invalid page size {page_size}")
            size = target.stat().st_size
            if size % page_size != 0:
                raise _ScanError(
                    "file size is not a whole number of "
                    f"{page_size}-byte pages"
                )

            objects: dict = {}
            _walk_schema_btree(handle, 1, page_size, objects, set())

            findings = []
            for name in sorted(objects):
                obj_type, rootpage, sql = objects[name]
                if obj_type not in ("table", "index"):
                    continue
                handle.seek((rootpage - 1) * page_size + (100 if rootpage == 1 else 0))
                probe = handle.read(1)
                if len(probe) != 1:
                    raise _ScanError(
                        f"root page {rootpage} of {obj_type} {name} is absent"
                    )
                page_type = probe[0]
                if page_type not in _VALID_PAGE_TYPES:
                    findings.append(
                        _finding(
                            "bad_root_page_type",
                            f"root page {rootpage} of {obj_type} {name} has type "
                            f"byte 0x{page_type:02x}",
                            table=name if obj_type == "table" else None,
                            index=name if obj_type == "index" else None,
                            page=rootpage,
                        )
                    )
                    continue
                if obj_type == "table" and sql is None:
                    # Only a TABLE needs its declaration, and only to rule out
                    # WITHOUT ROWID. Gating this on every object type would skip
                    # every `sqlite_autoindex_*` entry, whose `sql` is NULL by
                    # definition — including the automatic index the recurring
                    # production signature names.
                    continue
                if obj_type == "index" or _is_without_rowid(sql):
                    expected = _INDEX_PAGE_TYPES
                else:
                    expected = _TABLE_PAGE_TYPES
                if page_type in expected:
                    continue
                observed = "index" if page_type in _INDEX_PAGE_TYPES else "table"
                article = "an" if observed == "index" else "a"
                findings.append(
                    _finding(
                        "root_page_kind_mismatch",
                        f"root page {rootpage} of {obj_type} {name} holds "
                        f"{article} {observed} b-tree page "
                        f"(type byte 0x{page_type:02x})",
                        table=name if obj_type == "table" else None,
                        index=name if obj_type == "index" else None,
                        page=rootpage,
                    )
                )
            return {"method": "raw_scan", "findings": findings, "reason": None}
    except Exception as exc:  # noqa: BLE001 — characterization never raises
        return _unavailable(_reason(exc))


# ==========================================================================
# normalized shape token
# ==========================================================================

def shape_token(findings) -> str:
    """A short equality-comparable token for one damage class.

    Derived from the SORTED SET of ``(kind, table, index)`` triples, so page
    numbers, cell indices and rowids — which differ between two instances of
    the same fault — cannot make one recurrence look like two.
    """
    try:
        triples = sorted(
            {
                (
                    str(finding.get("kind") or ""),
                    str(finding.get("table") or ""),
                    str(finding.get("index") or ""),
                )
                for finding in (findings or ())
            }
        )
    except Exception:  # noqa: BLE001 — characterization never raises
        return "none"
    if not triples:
        return "none"
    payload = "|".join(":".join(triple) for triple in triples)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def describe_damage(*, integrity_rows, path) -> dict:
    """One structured description from whichever sources are available.

    ``method`` is ``integrity_rows`` when only the pragma produced findings,
    ``raw_scan`` when only the file scan did, ``both`` when each did, and
    ``unavailable`` when neither source could say anything.
    """
    try:
        parsed = parse_integrity_rows(integrity_rows)
        scan = scan_sqlite_file(path)
        scanned = list(scan.get("findings") or ())
        scan_available = scan.get("method") == "raw_scan"

        if parsed and scanned:
            method = "both"
        elif parsed:
            method = "integrity_rows"
        elif scan_available:
            method = "raw_scan"
        else:
            method = "unavailable"

        findings = parsed + scanned
        return {
            "schemaVersion": SCHEMA_VERSION,
            "method": method,
            "findings": findings,
            "shapeToken": shape_token(findings),
            "reason": None if scan_available else scan.get("reason"),
        }
    except Exception as exc:  # noqa: BLE001 — characterization never raises
        return {
            "schemaVersion": SCHEMA_VERSION,
            "method": "unavailable",
            "findings": [],
            "shapeToken": "none",
            "reason": _reason(exc),
        }
