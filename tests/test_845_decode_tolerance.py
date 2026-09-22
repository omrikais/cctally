"""#845 — the decode helper, the malformed-entry predicate and the static gate.

Specification: ``docs/superpowers/specs/2026-09-15-845-846-codex-metadata-decode-tolerance.md``.

Sections 4.1 (the helper and the predicate), 4.7 (the two cache-side rollup
writers) and acceptance rows A8 and A15 live here. The reader-level, counter-
level and identity-map-level agreement of the same predicate is A6, which lives
in ``tests/test_828_metadata_probe.py`` beside the counting populations.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sqlite3

import pytest

from _lib_codex_metadata import (
    DecodedCodexProjectMetadata,
    codex_metadata_is_malformed,
    decode_codex_project_metadata,
    resolve_codex_threads_table,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"

#: An arbitrary byte sequence that is not valid UTF-8. ``0xff`` can never begin
#: a UTF-8 sequence, which is the shape #845 reproduces with
#: ``CAST(x'ff' AS TEXT)``.
BAD = b"/Users/someone/\xffproject"

#: The account the §4.7 scoped-projection arm narrows to.
SCOPED_ACCOUNT = "s" * 32


# ── §4.1, the decoder ──────────────────────────────────────────────────────


def test_bytes_decode_strictly_and_a_failure_is_recorded_per_field():
    decoded = decode_codex_project_metadata(b"/repo/one", BAD)
    assert decoded.cwd == "/repo/one"
    assert decoded.git_json is None
    assert decoded.undecodable_fields == frozenset({"git_json"})

    decoded = decode_codex_project_metadata(BAD, b'{"a": 1}')
    assert decoded.cwd is None
    assert decoded.git_json == '{"a": 1}'
    assert decoded.undecodable_fields == frozenset({"cwd"})

    both = decode_codex_project_metadata(BAD, BAD)
    assert both.undecodable_fields == frozenset({"cwd", "git_json"})


def test_str_is_verbatim_and_none_stays_none():
    decoded = decode_codex_project_metadata("/repo/two", None)
    assert decoded == DecodedCodexProjectMetadata(
        cwd="/repo/two", git_json=None, undecodable_fields=frozenset())
    assert decode_codex_project_metadata(None, None).undecodable_fields == frozenset()


def test_a_non_blob_operand_fails_loudly():
    """The in-scope statements CAST both columns, so an int can only arrive
    from a caller that forgot to. Degrading it to absent would publish
    ``(unassigned)`` for a readable row."""
    with pytest.raises(TypeError):
        decode_codex_project_metadata(17, None)


def test_the_decoder_never_changes_the_connection_text_factory():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (cwd TEXT, git_json TEXT)")
        conn.execute("INSERT INTO t VALUES (CAST(x'2fff' AS TEXT), NULL)")
        row = conn.execute(
            "SELECT CAST(cwd AS BLOB), CAST(git_json AS BLOB) FROM t"
        ).fetchone()
        decoded = decode_codex_project_metadata(row[0], row[1])
        assert decoded.undecodable_fields == frozenset({"cwd"})
        assert conn.text_factory is str
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT cwd FROM t").fetchone()
    finally:
        conn.close()


# ── §4.1, the malformed-entry predicate ────────────────────────────────────


def _meta(cwd=None, git_json=None, bad=()):
    return DecodedCodexProjectMetadata(
        cwd=cwd, git_json=git_json, undecodable_fields=frozenset(bad))


_UNDECODABLE_CWD = _meta(bad=("cwd",))
_UNDECODABLE_GIT = _meta(cwd="", bad=("git_json",))


@pytest.mark.parametrize("case,direct,inherited", [
    # E1 — direct cwd undecodable, whatever else is readable.
    ("E1", _meta(git_json='{"a": 1}', bad=("cwd",)), _meta(cwd="/inherited")),
    ("E1-no-inherited", _UNDECODABLE_CWD, None),
    # E2 — direct cwd absent, inherited cwd undecodable.
    ("E2-direct-row-absent", None, _UNDECODABLE_CWD),
    ("E2-direct-cwd-empty", _meta(cwd="", git_json=None), _UNDECODABLE_CWD),
    # E3 — both cwd absent, direct git_json undecodable.
    ("E3", _meta(cwd="", bad=("git_json",)), _meta(cwd="")),
    ("E3-no-inherited", _meta(cwd=None, bad=("git_json",)), None),
    # E4 — both cwd absent, direct git_json absent, inherited git_json bad.
    ("E4", _meta(cwd="", git_json=""), _UNDECODABLE_GIT),
])
def test_the_four_malformed_cases(case, direct, inherited):
    assert codex_metadata_is_malformed(direct, inherited) is True, case


@pytest.mark.parametrize("case,direct,inherited", [
    # §4.1's three attributed counterexamples. Each one is refused by
    # revision 1's over-refusing rule and attributed by the resolver's path.
    (
        "valid direct cwd beside an undecodable direct git_json",
        _meta(cwd="/repo/one", bad=("git_json",)), None,
    ),
    (
        "empty direct cwd, valid direct git_json, empty inherited cwd, "
        "undecodable inherited git_json",
        _meta(cwd="", git_json='{"a": 1}'), _UNDECODABLE_GIT,
    ),
    (
        "valid inherited cwd beside an undecodable inherited git_json",
        _meta(cwd="", git_json=""),
        _meta(cwd="/inherited", bad=("git_json",)),
    ),
    ("nothing undecodable at all", _meta(cwd="", git_json=""), None),
    ("no metadata at all", None, None),
])
def test_the_attributed_counterexamples(case, direct, inherited):
    assert codex_metadata_is_malformed(direct, inherited) is False, case


# ── §4.3, the raw-table reference ──────────────────────────────────────────


def _create_threads(conn, schema="main"):
    conn.execute(
        f'CREATE TABLE "{schema}".codex_conversation_threads '
        "(conversation_key TEXT PRIMARY KEY, cwd TEXT, git_json TEXT)"
    )


def test_the_raw_table_resolves_on_a_bare_cache_connection():
    conn = sqlite3.connect(":memory:")
    try:
        _create_threads(conn)
        assert resolve_codex_threads_table(conn) == (
            "main.codex_conversation_threads")
    finally:
        conn.close()


def test_the_raw_table_resolves_through_an_attached_cache(tmp_path):
    cache_path = tmp_path / "cache.db"
    cache = sqlite3.connect(cache_path)
    try:
        _create_threads(cache)
        cache.commit()
    finally:
        cache.close()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
        assert resolve_codex_threads_table(conn) == (
            "cache_db.codex_conversation_threads")
    finally:
        conn.close()


def test_a_scoped_temp_view_does_not_hide_the_raw_table(tmp_path):
    """§4.3. ``scope_conversations_db_to_account`` installs an EMPTY TEMP view
    over the unqualified name. The qualified reference must still reach the
    real rows, or the fail-closed count and the viewer's key set would be
    silently empty for every account-scoped request."""
    cache_path = tmp_path / "cache.db"
    cache = sqlite3.connect(cache_path)
    try:
        _create_threads(cache)
        cache.execute(
            "INSERT INTO codex_conversation_threads VALUES ('v1.a', '/repo', NULL)")
        cache.commit()
    finally:
        cache.close()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
        conn.execute(
            "CREATE TEMP VIEW codex_conversation_threads AS "
            "SELECT * FROM cache_db.codex_conversation_threads WHERE 0"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads").fetchone()[0] == 0
        table = resolve_codex_threads_table(conn)
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_unresolvable_connection_raises():
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError):
            resolve_codex_threads_table(conn)
    finally:
        conn.close()


class _ProbeFails:
    """A connection proxy whose schema probe raises like a locked store.

    Everything else is delegated, so only the resolver's own first read
    fails, which is the read a locked cache.db actually fails on.
    """

    def __init__(self, conn, error):
        self._conn = conn
        self._error = error

    def execute(self, sql, *args, **kwargs):
        if "sqlite_master" in sql:
            raise self._error
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_read_failure_during_resolution_is_not_an_absent_table():
    """A store the connection cannot read must not resolve as "no table".

    The probe used to swallow ``sqlite3.Error`` and answer ``False``, so the
    resolver raised its absent-table ``RuntimeError`` and both fail-closed
    reads (#850 M5) answered the healthy value for a locked store. The error
    now propagates as the ``sqlite3.Error`` it is.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE codex_conversation_threads (conversation_key TEXT)")
        assert resolve_codex_threads_table(conn) == (
            "main.codex_conversation_threads")
        locked = _ProbeFails(conn, sqlite3.OperationalError("database is locked"))
        with pytest.raises(sqlite3.OperationalError):
            resolve_codex_threads_table(locked)
    finally:
        conn.close()


# ── A15, the static gate ───────────────────────────────────────────────────

#: The modules of the specification's section 1.1 table. Every one of them
#: reads the two thread columns, and every one of them must do it through a
#: ``CAST(... AS BLOB)`` selection.
_IN_SCOPE_MODULES = (
    "_cctally_source_analytics.py",
    "_cctally_dashboard_sources.py",
    "_cctally_cache.py",
    "_lib_codex_conversation_query.py",
    "_lib_conversation_query.py",
)

#: The one allowed exception, and the reason. ``bin/_cctally_diagnosis_sources.py``
#: is the diagnosis route's reader, which #848 owns (M4). It is WALKED rather
#: than merely omitted so the allowlist is non-vacuous: the assertion below
#: fails if that module ever stops carrying a bare selection, which would mean
#: the allowlist is protecting nothing.
_ALLOWLISTED_MODULES = {"_cctally_diagnosis_sources.py"}

_THREAD_TABLE_REFERENCES = ("codex_conversation_threads", "{threads}")
_COLUMN_RE = re.compile(r"(?:\b\w+\s*\.\s*)?\b(cwd|git_json)\b")
_CAST_BLOB_RE = re.compile(
    r"CAST\s*\(\s*(?:\w+\s*\.\s*)?(?:cwd|git_json)\s+AS\s+BLOB\s*\)",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """Blank out ``--`` line comments, keeping every offset stable."""
    out = list(sql)
    index = 0
    while index < len(out) - 1:
        if out[index] == "-" and out[index + 1] == "-":
            while index < len(out) and out[index] != "\n":
                out[index] = " "
                index += 1
        index += 1
    return "".join(out)


def _projection_spans(sql: str) -> "list[tuple[int, int]]":
    """Every ``SELECT`` projection list, ended at its own depth-zero ``FROM``.

    A subquery's ``FROM`` sits inside parentheses, so it cannot truncate the
    enclosing projection, and the subquery's own ``SELECT`` is matched
    separately. A projection with no ``FROM`` at all runs to the end.
    """
    spans = []
    for match in re.finditer(r"\bSELECT\b", sql, re.IGNORECASE):
        start = match.end()
        depth = 0
        index = start
        end = len(sql)
        while index < len(sql):
            char = sql[index]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    end = index
                    break
                depth -= 1
            elif depth == 0 and sql[index:index + 4].upper() == "FROM" and (
                (index == 0 or not sql[index - 1].isalnum())
                and (index + 4 >= len(sql) or not sql[index + 4].isalnum())
            ):
                end = index
                break
            index += 1
        spans.append((start, end))
    return spans


def _bare_selections(source: str) -> "list[str]":
    """Every SQL literal in ``source`` that selects a bare thread column."""
    findings = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        raw = node.value
        if not any(token in raw for token in _THREAD_TABLE_REFERENCES):
            continue
        sql = _strip_sql_comments(raw)
        cast_spans = [m.span() for m in _CAST_BLOB_RE.finditer(sql)]
        for start, end in _projection_spans(sql):
            for column in _COLUMN_RE.finditer(sql, start, end):
                if any(
                    lo <= column.start() and column.end() <= hi
                    for lo, hi in cast_spans
                ):
                    continue
                findings.append(
                    f"line {node.lineno}: {column.group(0)!r} in "
                    + " ".join(sql[start:end].split())[:120]
                )
    return findings


def test_no_in_scope_statement_selects_a_bare_thread_column():
    """A15. A bare ``cwd``/``git_json`` selection decodes through the default
    ``str`` factory, which is the whole defect: one undecodable byte raises
    ``OperationalError`` for the entire statement."""
    offenders = {}
    for name in _IN_SCOPE_MODULES:
        findings = _bare_selections((BIN / name).read_text(encoding="utf-8"))
        if findings:
            offenders[name] = findings
    assert offenders == {}, (
        "these statements still select the thread metadata columns through "
        f"the default text factory: {offenders}"
    )


def test_the_allowlist_names_only_the_diagnosis_reader_and_is_not_vacuous():
    assert _ALLOWLISTED_MODULES == {"_cctally_diagnosis_sources.py"}
    for name in _ALLOWLISTED_MODULES:
        assert _bare_selections((BIN / name).read_text(encoding="utf-8")), (
            f"{name} no longer carries a bare thread-column selection, so the "
            "allowlist protects nothing and should be removed"
        )


def test_the_static_gate_detects_a_bare_selection():
    """The gate itself, over both spellings and both escapes."""
    assert _bare_selections(
        's = "SELECT cwd, git_json FROM codex_conversation_threads"'
    )
    assert _bare_selections('s = "SELECT t.cwd FROM {threads} AS t"')
    assert not _bare_selections(
        's = "SELECT CAST(t.cwd AS BLOB), CAST(t.git_json AS BLOB) '
        'FROM {threads} AS t"'
    )
    # An INSERT column list is not a selection, and a SQL comment is not code.
    assert not _bare_selections(
        's = "INSERT INTO codex_conversation_threads (cwd, git_json) VALUES (?,?)"'
    )
    assert not _bare_selections(
        's = """\n-- never expose another account\'s cwd/git metadata\n'
        'SELECT * FROM codex_conversation_threads WHERE 0"""'
    )


# ── §4.7 / A8, the two cache-side rollup writers ───────────────────────────


def _rollup_store(ns, tmp_path, monkeypatch):
    """A conversations store with one Codex conversation per named thread."""
    from conftest import redirect_paths

    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
        " first_seen_utc, last_seen_utc) VALUES (?,?,?,?)",
        ("rollup-root", "/synthetic/rollup-root",
         "2026-01-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
    cache.commit()
    return cache


_ROLLUP_THREADS = {
    # (cwd, git_json, corrupt_column, expected project_key is None)
    "malformed-cwd": (None, None, "cwd", True),
    "valid-cwd-bad-git": ("/synthetic/project-keep", None, "git_json", False),
    "no-metadata": (None, None, None, False),
}


def _seed_rollup_conversation(cache, conversations, *, key, cwd, git_json,
                              corrupt):
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id,"
        " source_path, cwd, git_json, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (key, "rollup-root", f"native-{key}", f"native-{key}",
         f"/synthetic/{key}.jsonl", cwd, git_json,
         "2026-01-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    )
    if corrupt is not None:
        cache.execute(
            f"UPDATE codex_conversation_threads SET {corrupt} = CAST(? AS TEXT)"
            " WHERE conversation_key = ?", (BAD, key),
        )
    cache.commit()
    import _cctally_cache

    # Stamped with the account the scoped projection is built for; the scoped
    # views filter the message leaf by that key, so an unstamped row would make
    # the scoped copy empty and the override arm vacuous.
    conversations.execute(
        _cctally_cache._CODEX_ACCOUNT_MSG_INSERT_SQL,
        (key, "rollup-root", f"/synthetic/{key}.jsonl", 1,
         "2026-09-01T00:00:00+00:00", None, None, "message", "message",
         "response_item", "gpt-5", "hello", "digest", 5, None, None, None,
         SCOPED_ACCOUNT),
    )
    conversations.commit()


@pytest.fixture
def rollup_store(tmp_path, monkeypatch):
    from conftest import load_script

    ns = load_script()
    cache = _rollup_store(ns, tmp_path, monkeypatch)
    conversations = ns["open_conversations_db"]()
    try:
        for key, (cwd, git_json, corrupt, _null) in _ROLLUP_THREADS.items():
            _seed_rollup_conversation(
                cache, conversations, key=key, cwd=cwd, git_json=git_json,
                corrupt=corrupt)
        yield ns, cache, conversations
    finally:
        conversations.close()
        cache.close()


def test_845_a8_the_rollup_writer_persists_null_for_a_malformed_thread(
    rollup_store,
):
    """A8's first two arms. `_recompute_codex_rollups` raised
    `OperationalError` on a malformed thread and, once decoded, would have
    persisted `(unassigned)` — a wrong answer every later read prefers."""
    import _cctally_cache

    _ns, _cache, conversations = rollup_store
    _cctally_cache._recompute_codex_rollups(
        conversations, sorted(_ROLLUP_THREADS), advance_render_revision=False)
    conversations.commit()
    rows = {
        row[0]: (row[1], row[2])
        for row in conversations.execute(
            "SELECT conversation_key, project_key, project_label "
            "FROM codex_conversation_rollups")
    }
    assert rows["malformed-cwd"] == (None, None)
    # A decodable thread with no metadata still persists `(unassigned)`: that
    # is a real answer, not a decode failure.
    assert rows["no-metadata"][1] == "(unassigned)"
    # And a valid `cwd` beside an undecodable `git_json` keeps its attribution,
    # because the direct-only resolver stops at the `cwd`.
    assert rows["valid-cwd-bad-git"][0] is not None
    assert rows["valid-cwd-bad-git"][1] == "project-keep"


def test_845_a8_the_scoped_projection_overrides_a_stale_persisted_rollup(
    rollup_store,
):
    """A8's third arm. `scope_conversations_db_to_account` preferred the
    persisted rollup unconditionally, so a scoped copy kept a PRE-CORRUPTION
    attribution for a thread the store can no longer read."""
    import _cctally_cache

    _ns, cache, conversations = rollup_store
    # Materialize the rollups while every thread still reads, so the malformed
    # one carries a real attribution to be overridden.
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = '/synthetic/project-was' "
        "WHERE conversation_key = 'malformed-cwd'")
    cache.commit()
    _cctally_cache._recompute_codex_rollups(
        conversations, sorted(_ROLLUP_THREADS), advance_render_revision=False)
    conversations.commit()
    before = conversations.execute(
        "SELECT project_label FROM codex_conversation_rollups "
        "WHERE conversation_key='malformed-cwd'").fetchone()
    assert before[0] == "project-was", before

    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = 'malformed-cwd'", (BAD,))
    cache.commit()

    scoped = _ns_open_scoped(conversations)
    _cctally_cache.scope_conversations_db_to_account(scoped, SCOPED_ACCOUNT)
    rows = {
        row[0]: (row[1], row[2])
        for row in scoped.execute(
            "SELECT conversation_key, project_key, project_label "
            "FROM codex_conversation_rollups")
    }
    scoped.close()
    assert rows.get("malformed-cwd") == (None, None), rows
    assert rows.get("valid-cwd-bad-git", (None, None))[1] == "project-keep"


def _ns_open_scoped(conversations):
    """A second connection over the same conversations store, cache attached."""
    from conftest import load_script

    ns = load_script()
    return ns["open_conversations_db"]()
