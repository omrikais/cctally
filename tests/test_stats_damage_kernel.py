"""#496 S1 — damage characterization kernel.

`PRAGMA integrity_check` raised in 54 of the 74 production forensics bundles,
so the bundle holds only an error string and no description of the damage can
come from it. The kernel therefore has two sources: a parser over the pragma's
row forms, and a raw scan of the file's own header and `sqlite_schema` b-tree.
Both feed one normalized shape token so a recurring damage class is comparable
by equality.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_stats_damage as dmg  # noqa: E402

REAL_SIGNATURE = [
    "wrong # of entries in index sqlite_autoindex_quota_projection_state_1",
    "NULL value in quota_projection_state.source_root_key",
    "NUMERIC value in quota_projection_state.generation",
    "NUMERIC value in quota_projection_state.completed_at_utc",
    "row 1 missing from index sqlite_autoindex_quota_projection_state_1",
]

TREE_FORMS = [
    "*** in database main ***",
    "Tree 55 page 55 cell 24: Rowid 1638 out of order",
    "Tree 50 page 16277 cell 0: Child page depth differs",
    "Tree 29 page 29 cell 0: 2nd reference to page 17828",
    "Tree 3 page 3: btreeInitPage() returns error code 11",
    "Page 15088: never used",
]


# ==========================================================================
# parse_integrity_rows
# ==========================================================================

def test_parses_index_entry_count_form():
    (finding,) = dmg.parse_integrity_rows([REAL_SIGNATURE[0]])
    assert finding["kind"] == "index_entry_count"
    assert finding["index"] == "sqlite_autoindex_quota_projection_state_1"
    assert finding["table"] is None


def test_parses_column_value_forms():
    findings = dmg.parse_integrity_rows(REAL_SIGNATURE[1:4])
    assert [f["kind"] for f in findings] == [
        "null_value", "type_mismatch", "type_mismatch",
    ]
    assert {f["table"] for f in findings} == {"quota_projection_state"}
    assert [f["column"] for f in findings] == [
        "source_root_key", "generation", "completed_at_utc",
    ]


def test_parses_missing_row_form():
    (finding,) = dmg.parse_integrity_rows([REAL_SIGNATURE[4]])
    assert finding["kind"] == "row_missing_from_index"
    assert finding["rowid"] == 1
    assert finding["index"] == "sqlite_autoindex_quota_projection_state_1"


def test_parses_tree_and_page_forms():
    findings = dmg.parse_integrity_rows(TREE_FORMS)
    kinds = [f["kind"] for f in findings]
    assert "tree_cell" in kinds
    assert "page_never_used" in kinds
    assert "btree_init_error" in kinds
    tree = next(f for f in findings if f["kind"] == "tree_cell")
    assert tree["page"] == 55 and tree["cell"] == 24


def test_database_banner_line_is_not_a_finding():
    assert dmg.parse_integrity_rows(["*** in database main ***"]) == []


def test_every_finding_carries_the_full_key_set():
    expected = {"kind", "table", "index", "column", "page", "cell", "rowid", "raw"}
    for finding in dmg.parse_integrity_rows(REAL_SIGNATURE + TREE_FORMS):
        assert set(finding) == expected


def test_unrecognised_message_is_retained_not_dropped():
    (finding,) = dmg.parse_integrity_rows(["something nobody has seen before"])
    assert finding["kind"] == "unparsed"
    assert finding["raw"] == "something nobody has seen before"


def test_string_error_payload_yields_no_findings():
    """54 of 74 production bundles store this exact string, not a row list."""
    assert dmg.parse_integrity_rows("error: database disk image is malformed") == []


def test_none_payload_yields_no_findings():
    assert dmg.parse_integrity_rows(None) == []


def test_ok_row_is_not_a_finding():
    assert dmg.parse_integrity_rows(["ok"]) == []


# ==========================================================================
# scan_sqlite_file
# ==========================================================================

_PAGE_TYPE_OFFSET_IN_PAGE = 0


def _build_probe_db(path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE alpha (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE beta (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO alpha (v) VALUES (?)", [(f"row-{i}",) for i in range(200)]
        )
        conn.executemany(
            "INSERT INTO beta (v) VALUES (?)", [(f"row-{i}",) for i in range(200)]
        )
        conn.commit()
    finally:
        conn.close()


def _page_size(path: pathlib.Path) -> int:
    header = path.read_bytes()[:100]
    raw = int.from_bytes(header[16:18], "big")
    return 65536 if raw == 1 else raw


def _root_page_of(path: pathlib.Path, name: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def _clobber_root_page_type(path: pathlib.Path, name: str) -> None:
    root = _root_page_of(path, name)
    page_size = _page_size(path)
    with path.open("r+b") as handle:
        handle.seek((root - 1) * page_size + _PAGE_TYPE_OFFSET_IN_PAGE)
        handle.write(b"\x7f")
        handle.flush()


def test_scan_reports_a_healthy_file_as_clean(tmp_path):
    db = tmp_path / "healthy.db"
    _build_probe_db(db)
    result = dmg.scan_sqlite_file(db)
    assert result["method"] == "raw_scan"
    assert result["findings"] == []
    assert result["reason"] is None


def test_scan_names_the_object_whose_root_page_type_is_invalid(tmp_path):
    db = tmp_path / "damaged.db"
    _build_probe_db(db)
    _clobber_root_page_type(db, "beta")

    # Proof the scanner reaches a state SQLite itself cannot: the pragma raises
    # on this same file, which is the 54-of-74 production shape.
    raised = False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        raised = True
    assert raised, "expected PRAGMA integrity_check to raise on the damaged file"

    result = dmg.scan_sqlite_file(db)
    assert result["method"] == "raw_scan"
    named = [f for f in result["findings"] if f["kind"] == "bad_root_page_type"]
    assert [f["table"] for f in named] == ["beta"]


def test_scan_of_a_truncated_file_reports_unavailable_without_raising(tmp_path):
    db = tmp_path / "truncated.db"
    _build_probe_db(db)
    page_size = _page_size(db)
    data = db.read_bytes()
    db.write_bytes(data[: page_size + page_size // 2])

    result = dmg.scan_sqlite_file(db)
    assert result["method"] == "unavailable"
    assert isinstance(result["reason"], str) and result["reason"]
    assert result["findings"] == []


def test_scan_of_a_missing_file_reports_unavailable(tmp_path):
    result = dmg.scan_sqlite_file(tmp_path / "absent.db")
    assert result["method"] == "unavailable"
    assert result["findings"] == []


def test_scan_honours_a_non_default_page_size(tmp_path):
    db = tmp_path / "page8k.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA page_size=8192")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE gamma (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO gamma (v) VALUES (?)", [(f"r{i}",) for i in range(400)]
        )
        conn.commit()
    finally:
        conn.close()
    assert _page_size(db) == 8192
    _clobber_root_page_type(db, "gamma")

    result = dmg.scan_sqlite_file(db)
    named = [f for f in result["findings"] if f["kind"] == "bad_root_page_type"]
    assert [f["table"] for f in named] == ["gamma"]


def _set_root_page_type(path: pathlib.Path, name: str, type_byte: int) -> None:
    """Overwrite one object's root page type byte, leaving the rest intact."""
    root = _root_page_of(path, name)
    page_size = _page_size(path)
    with path.open("r+b") as handle:
        handle.seek((root - 1) * page_size + _PAGE_TYPE_OFFSET_IN_PAGE)
        handle.write(bytes([type_byte]))
        handle.flush()


def _build_probe_db_with_index(path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE alpha (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("CREATE INDEX alpha_v ON alpha (v)")
        conn.execute(
            "CREATE TABLE gamma (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO alpha (v) VALUES (?)",
            [(f"row-{i}",) for i in range(200)],
        )
        conn.executemany(
            "INSERT INTO gamma (k, v) VALUES (?, ?)",
            [(f"k-{i}", f"row-{i}") for i in range(200)],
        )
        conn.commit()
    finally:
        conn.close()


def test_scan_names_a_table_whose_root_page_is_an_index_leaf(tmp_path):
    """The production shape: a table rooted at an index leaf reads as healthy
    under the shipped check, which accepts any valid b-tree page type."""
    db = tmp_path / "kindmismatch.db"
    _build_probe_db(db)
    _set_root_page_type(db, "beta", 0x0A)

    result = dmg.scan_sqlite_file(db)
    assert result["method"] == "raw_scan"
    named = [
        f for f in result["findings"] if f["kind"] == "root_page_kind_mismatch"
    ]
    assert [f["table"] for f in named] == ["beta"]
    assert named[0]["index"] is None
    assert named[0]["page"] == _root_page_of(db, "beta")


def test_scan_names_an_index_whose_root_page_is_a_table_leaf(tmp_path):
    db = tmp_path / "indexmismatch.db"
    _build_probe_db_with_index(db)
    _set_root_page_type(db, "alpha_v", 0x0D)

    result = dmg.scan_sqlite_file(db)
    named = [
        f for f in result["findings"] if f["kind"] == "root_page_kind_mismatch"
    ]
    assert [f["index"] for f in named] == ["alpha_v"]
    assert named[0]["table"] is None


def test_scan_accepts_a_without_rowid_table_rooted_at_an_index_page(tmp_path):
    """A WITHOUT ROWID table legitimately uses an index b-tree. Reporting it
    would make every healthy one look damaged."""
    db = tmp_path / "withoutrowid.db"
    _build_probe_db_with_index(db)

    result = dmg.scan_sqlite_file(db)
    assert result["method"] == "raw_scan"
    assert result["findings"] == []


def test_bad_root_page_type_keeps_its_kind_and_token(tmp_path):
    """A byte that is no b-tree page type at all stays `bad_root_page_type`,
    so an incident whose findings are exclusively that kind keeps its token."""
    db = tmp_path / "badtype.db"
    _build_probe_db(db)
    _clobber_root_page_type(db, "beta")

    result = dmg.scan_sqlite_file(db)
    kinds = {f["kind"] for f in result["findings"]}
    assert kinds == {"bad_root_page_type"}
    assert dmg.shape_token(result["findings"]) == dmg.shape_token(
        [
            {
                "kind": "bad_root_page_type",
                "table": "beta",
                "index": None,
            }
        ]
    )


def test_a_mixed_incident_token_differs_from_the_bad_root_only_token(tmp_path):
    """Token stability is narrow: adding a kind mismatch necessarily changes
    the token, because shape_token hashes the whole triple set."""
    bad_only = tmp_path / "badonly.db"
    _build_probe_db(bad_only)
    _clobber_root_page_type(bad_only, "beta")

    mixed = tmp_path / "mixed.db"
    _build_probe_db(mixed)
    _clobber_root_page_type(mixed, "beta")
    _set_root_page_type(mixed, "alpha", 0x0A)

    bad_token = dmg.shape_token(dmg.scan_sqlite_file(bad_only)["findings"])
    mixed_token = dmg.shape_token(dmg.scan_sqlite_file(mixed)["findings"])
    assert bad_token != mixed_token
    assert mixed_token != "none"


def _build_probe_db_mentioning_without_rowid(path: pathlib.Path) -> None:
    """An ordinary ROWID table whose stored SQL merely MENTIONS the phrase.

    SQLite keeps comments and string literals inside `sqlite_schema.sql`, so a
    substring search over the whole statement exempts this healthy-shaped
    declaration — and then reports the table as clean however damaged it is.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE delta (\n"
            "  id INTEGER PRIMARY KEY, -- WITHOUT ROWID was considered\n"
            "  v TEXT CHECK (v <> 'WITHOUT ROWID')\n"
            ")"
        )
        conn.executemany(
            "INSERT INTO delta (v) VALUES (?)", [(f"row-{i}",) for i in range(200)]
        )
        conn.commit()
    finally:
        conn.close()


def test_a_table_that_merely_mentions_without_rowid_is_still_judged(tmp_path):
    db = tmp_path / "mentions.db"
    _build_probe_db_mentioning_without_rowid(db)
    _set_root_page_type(db, "delta", 0x0A)

    named = [
        f
        for f in dmg.scan_sqlite_file(db)["findings"]
        if f["kind"] == "root_page_kind_mismatch"
    ]
    assert [f["table"] for f in named] == ["delta"]


def _build_probe_db_with_automatic_index(path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE eps (id INTEGER PRIMARY KEY, v TEXT, UNIQUE(v))"
        )
        conn.executemany(
            "INSERT INTO eps (v) VALUES (?)", [(f"row-{i}",) for i in range(200)]
        )
        conn.commit()
    finally:
        conn.close()


def test_an_automatic_index_is_judged_for_page_kind(tmp_path):
    """`sqlite_schema.sql` is NULL for `sqlite_autoindex_*`, but an index needs
    no declaration to be judged — only a table does, and only to rule out
    WITHOUT ROWID. Declining on a NULL `sql` for every object type would skip
    every automatic index, including the one the recurring production signature
    names."""
    db = tmp_path / "autoindex.db"
    _build_probe_db_with_automatic_index(db)
    _set_root_page_type(db, "sqlite_autoindex_eps_1", 0x0D)

    named = [
        f
        for f in dmg.scan_sqlite_file(db)["findings"]
        if f["kind"] == "root_page_kind_mismatch"
    ]
    assert [f["index"] for f in named] == ["sqlite_autoindex_eps_1"]
    assert named[0]["table"] is None


def test_kind_mismatch_finding_carries_the_full_key_set(tmp_path):
    db = tmp_path / "keyset.db"
    _build_probe_db(db)
    _set_root_page_type(db, "beta", 0x0A)

    named = [
        f
        for f in dmg.scan_sqlite_file(db)["findings"]
        if f["kind"] == "root_page_kind_mismatch"
    ]
    assert set(named[0]) == set(dmg._FINDING_KEYS)


def test_kind_mismatch_raw_text_names_the_expected_and_observed_kinds(tmp_path):
    """The raw text is persisted verbatim in schemaVersion 2 manifests, so its
    wording is a durable surface rather than a log line."""
    db = tmp_path / "rawtext.db"
    _build_probe_db_with_index(db)
    _set_root_page_type(db, "alpha_v", 0x0D)
    root = _root_page_of(db, "alpha_v")

    named = [
        f
        for f in dmg.scan_sqlite_file(db)["findings"]
        if f["kind"] == "root_page_kind_mismatch"
    ]
    assert named[0]["raw"] == (
        f"root page {root} of index alpha_v holds a table b-tree page "
        f"(type byte 0x0d)"
    )


def test_bad_root_page_type_raw_text_is_unchanged(tmp_path):
    """Pins the text an already-classified incident carries, so a later edit
    cannot silently reclassify the historical corpus."""
    db = tmp_path / "badrawtext.db"
    _build_probe_db(db)
    _clobber_root_page_type(db, "beta")
    root = _root_page_of(db, "beta")

    named = [
        f
        for f in dmg.scan_sqlite_file(db)["findings"]
        if f["kind"] == "bad_root_page_type"
    ]
    assert named[0]["raw"] == (
        f"root page {root} of table beta has type byte 0x7f"
    )


# ==========================================================================
# shape_token / describe_damage
# ==========================================================================

def test_token_ignores_page_numbers_cells_and_rowids():
    a = dmg.parse_integrity_rows([
        "Tree 55 page 55 cell 24: Rowid 1638 out of order",
        "row 1 missing from index sqlite_autoindex_quota_projection_state_1",
    ])
    b = dmg.parse_integrity_rows([
        "Tree 12 page 903 cell 7: Rowid 4 out of order",
        "row 9182 missing from index sqlite_autoindex_quota_projection_state_1",
    ])
    assert dmg.shape_token(a) == dmg.shape_token(b)


def test_different_damage_classes_produce_different_tokens():
    a = dmg.parse_integrity_rows(REAL_SIGNATURE)
    b = dmg.parse_integrity_rows(["Page 15088: never used"])
    assert dmg.shape_token(a) != dmg.shape_token(b)


def test_token_distinguishes_implicated_objects():
    a = dmg.parse_integrity_rows(["NULL value in quota_projection_state.generation"])
    b = dmg.parse_integrity_rows(["NULL value in weekly_usage_snapshots.generation"])
    assert dmg.shape_token(a) != dmg.shape_token(b)


def test_empty_findings_produce_the_stable_none_token():
    assert dmg.shape_token([]) == "none"


def test_token_is_order_independent():
    rows = list(REAL_SIGNATURE)
    forward = dmg.shape_token(dmg.parse_integrity_rows(rows))
    backward = dmg.shape_token(dmg.parse_integrity_rows(list(reversed(rows))))
    assert forward == backward


def test_describe_prefers_integrity_rows_when_present(tmp_path):
    db = tmp_path / "healthy.db"
    _build_probe_db(db)
    result = dmg.describe_damage(integrity_rows=REAL_SIGNATURE, path=db)
    assert result["schemaVersion"] == 1
    assert result["method"] == "integrity_rows"
    assert result["shapeToken"] == dmg.shape_token(
        dmg.parse_integrity_rows(REAL_SIGNATURE)
    )
    assert result["reason"] is None


def test_describe_falls_back_to_the_raw_scan_when_the_pragma_raised(tmp_path):
    db = tmp_path / "damaged.db"
    _build_probe_db(db)
    _clobber_root_page_type(db, "beta")
    result = dmg.describe_damage(
        integrity_rows="error: database disk image is malformed", path=db
    )
    assert result["method"] == "raw_scan"
    assert [f["table"] for f in result["findings"]] == ["beta"]
    assert result["shapeToken"] != "none"


def test_describe_reports_both_when_rows_and_a_scan_finding_exist(tmp_path):
    db = tmp_path / "damaged.db"
    _build_probe_db(db)
    _clobber_root_page_type(db, "beta")
    result = dmg.describe_damage(integrity_rows=REAL_SIGNATURE, path=db)
    assert result["method"] == "both"
    kinds = {f["kind"] for f in result["findings"]}
    assert "bad_root_page_type" in kinds
    assert "null_value" in kinds


def test_describe_never_raises_on_an_unusable_path():
    result = dmg.describe_damage(integrity_rows=None, path=None)
    assert result["method"] == "unavailable"
    assert result["findings"] == []
    assert result["shapeToken"] == "none"


def test_describe_of_a_clean_index_reports_no_findings(tmp_path):
    db = tmp_path / "healthy.db"
    _build_probe_db(db)
    result = dmg.describe_damage(integrity_rows=["ok"], path=db)
    assert result["findings"] == []
    assert result["shapeToken"] == "none"
