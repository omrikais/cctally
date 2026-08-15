"""Structural schema-delivery assertions shared by cache/conversations tests."""
from __future__ import annotations

import ast
import inspect
import re
import sqlite3
import textwrap


_DDL_PATTERNS = {
    "index": re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s+ON\s",
        re.I,
    ),
    "table": re.compile(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(\w+)\s*(?:\(|USING\s)",
        re.I,
    ),
    "trigger": re.compile(
        r"CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+"
        r"(?:BEFORE|AFTER|INSTEAD\s+OF)\s",
        re.I,
    ),
    "view": re.compile(
        r"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+AS\s",
        re.I,
    ),
}
_FTS_SHADOW_TABLES = frozenset(
    f"{root}_{suffix}"
    for root in (
        "conversation_fts",
        "conversation_title_fts",
        "codex_conversation_fts",
    )
    for suffix in ("config", "content", "data", "docsize", "idx")
)
_CONDITIONAL_FTS_OBJECTS = frozenset(
    {
        ("table", "conversation_fts"),
        ("table", "conversation_title_fts"),
        ("table", "codex_conversation_fts"),
    }
    | {
        ("trigger", f"{prefix}_{suffix}")
        for prefix in ("conv_fts", "conv_title_fts", "codex_conv_fts")
        for suffix in ("ai", "ad", "au")
    }
)


def _string_constants(source: str) -> "list[str]":
    tree = ast.parse(textwrap.dedent(source))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _literal_object_keys(literal: str) -> "set[tuple[str, str]]":
    return {
        (kind, name)
        for kind, pattern in _DDL_PATTERNS.items()
        for name in pattern.findall(literal)
    }


def _source_tree(source: str) -> ast.AST:
    return ast.parse(textwrap.dedent(source))


def _referenced_names(source: str) -> "set[str]":
    return {
        node.id for node in ast.walk(_source_tree(source))
        if isinstance(node, ast.Name)
    }


def _called_names(source: str) -> "set[str]":
    return {
        node.func.id for node in ast.walk(_source_tree(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def schema_object_keys(db, fn, *, transitive: bool, seen=None):
    """Return explicit DDL objects reachable from one schema/migration function."""
    seen = set() if seen is None else seen
    if fn.__name__ in seen:
        return set()
    seen.add(fn.__name__)
    source = inspect.getsource(fn)
    keys = set()
    for literal in _string_constants(source):
        keys |= _literal_object_keys(literal)
    called_names = _called_names(source)
    for ident in sorted(_referenced_names(source)):
        obj = getattr(db, ident, None)
        if isinstance(obj, str):
            keys |= _literal_object_keys(obj)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for member in obj:
                if isinstance(member, str):
                    keys |= _literal_object_keys(member)
        elif isinstance(obj, dict):
            for member in obj.values():
                if isinstance(member, str):
                    keys |= _literal_object_keys(member)
        elif (
            transitive
            and ident in called_names
            and inspect.isfunction(obj)
            and getattr(obj, "__module__", None) == db.__name__
        ):
            keys |= schema_object_keys(db, obj, transitive=True, seen=seen)
    return keys


def declared_schema_object_keys(db, fn):
    """Explicit objects that survive one complete schema application.

    Static discovery is the guard: a new literal DDL statement becomes a
    candidate immediately. The live projection removes SQLite's implicit FTS
    shadow objects and objects deliberately created then dropped while the
    conversations store is projected from the historical cache schema. Every
    surviving explicit object must also be statically discoverable, so dynamic
    DDL cannot disappear through an intersection.
    """
    candidates = schema_object_keys(db, fn, transitive=True)
    conn = sqlite3.connect(":memory:")
    try:
        fn(conn)
        live = {
            (str(kind), str(name))
            for kind, name in conn.execute(
                "SELECT type,name FROM sqlite_schema "
                "WHERE type IN ('table','view','trigger','index') "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conn.close()
    live = {
        key for key in live
        if not (
            key[0] == "table" and key[1] in _FTS_SHADOW_TABLES
        )
    }
    undiscovered = live - candidates
    assert not undiscovered, (
        "live schema objects are not statically discoverable: "
        f"{sorted(undiscovered)}. Keep schema DDL in module-level literal "
        "strings reached by a bare helper name."
    )
    # These explicit objects are deliberately absent when the linked SQLite
    # lacks FTS5. They remain part of the declared schema contract and registry;
    # unlike the shadow tables above, their names occur in source-owned DDL.
    return live | (candidates & _CONDITIONAL_FTS_OBJECTS)


def assert_registry_matches_schema(db, fn, records, *, store):
    declared = declared_schema_object_keys(db, fn)
    registered = {(record.kind, record.name) for record in records}
    assert len(registered) == len(records), f"{store} registry has duplicates"
    assert declared == registered, (
        f"{store} schema objects and delivery registry disagree.\n"
        f"  in schema but unregistered: {sorted(declared - registered)}\n"
        f"  registered but not in schema: {sorted(registered - declared)}\n"
        "An unregistered object has no declared delivery path to an "
        "already-current store. Add a migration-owned ensure helper; only "
        "archaeologically verified pre-registry objects belong in the frozen "
        "baseline."
    )


def assert_migration_delivery(db, records, migrations, *, store):
    handlers = {migration.name: migration.handler for migration in migrations}
    for record in records:
        if record.introduced_by is None:
            continue
        assert record.introduced_by in handlers, (
            f"{store} object {record.kind}:{record.name} names missing "
            f"migration {record.introduced_by!r}"
        )
        handler = handlers[record.introduced_by]
        delivered = schema_object_keys(db, handler, transitive=False)
        if record.ensure_helper is not None:
            assert record.ensure_helper in _called_names(
                inspect.getsource(handler)
            ), (
                f"migration {record.introduced_by} does not call "
                f"{record.ensure_helper}"
            )
            assert hasattr(db, record.ensure_helper), (
                f"missing ensure helper {record.ensure_helper!r}"
            )
            delivered |= schema_object_keys(
                db, getattr(db, record.ensure_helper), transitive=True
            )
        assert (record.kind, record.name) in delivered, (
            f"migration {record.introduced_by} does not deliver "
            f"{record.kind}:{record.name}"
        )


def assert_frozen_baseline(records, expected_keys, *, store):
    observed = {
        (record.kind, record.name)
        for record in records if record.introduced_by is None
    }
    assert observed == expected_keys, (
        f"{store} frozen baseline changed.\n"
        f"  unexpected baseline objects: {sorted(observed - expected_keys)}\n"
        f"  missing baseline objects: {sorted(expected_keys - observed)}\n"
        "A new object may never join the baseline; only an archaeological "
        "correction may remove a pinned identity."
    )
