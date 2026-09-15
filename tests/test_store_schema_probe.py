"""#780 — a schema-qualified tri-state schema probe.

`schema_current` cannot serve the read-only opener. It always runs an
unqualified `PRAGMA user_version` against `main`, so it cannot address an
attached store, and it returns a bare boolean, so it cannot distinguish a store
that is BEHIND head — where a writer can still advance it and the reader may
fail soft — from one that is AHEAD, where the reader must fail closed with no
recovery attempt, matching the existing version-ahead posture.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _cctally_db as db  # noqa: E402
import _cctally_store as store  # noqa: E402


def _head(name: str) -> int:
    if name == "cache":
        return len(db._CACHE_MIGRATIONS)
    return len(db._CONVERSATIONS_MIGRATIONS)


@pytest.mark.parametrize("name", ["cache", "conversations"])
@pytest.mark.parametrize("delta,expected", [
    (0, "current"),
    (-1, "behind"),
    (1, "ahead"),
])
def test_schema_state_over_main(tmp_path, name, delta, expected):
    conn = sqlite3.connect(tmp_path / f"{name}.db")
    try:
        conn.execute(f"PRAGMA user_version={_head(name) + delta}")
        assert store.schema_state(conn, name) == expected
    finally:
        conn.close()


@pytest.mark.parametrize("delta,expected", [
    (0, "current"),
    (-1, "behind"),
    (1, "ahead"),
])
def test_schema_state_over_an_attached_cache_db(tmp_path, delta, expected):
    """The attached half is the case `schema_current` structurally cannot
    reach: its `PRAGMA user_version` is unqualified, so it reports `main`'s
    version however the attachment is stamped."""
    cache_path = tmp_path / "cache.db"
    attached = sqlite3.connect(cache_path)
    try:
        attached.execute(f"PRAGMA user_version={_head('cache') + delta}")
        attached.commit()
    finally:
        attached.close()
    conn = sqlite3.connect(tmp_path / "conversations.db")
    try:
        conn.execute(f"PRAGMA user_version={_head('conversations')}")
        conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
        assert store.schema_state(conn, "conversations") == "current"
        assert store.schema_state(
            conn, "cache", schema="cache_db") == expected
    finally:
        conn.close()


def test_a_store_with_no_registry_head_reports_behind(tmp_path):
    """`stats` has no registry head to gate on here, so the probe reports the
    soft direction rather than claiming currency — the same conservative answer
    `schema_current` gives with its bare False."""
    conn = sqlite3.connect(tmp_path / "stats.db")
    try:
        conn.execute("PRAGMA user_version=13")
        assert store.schema_state(conn, "stats") == "behind"
    finally:
        conn.close()


def test_schema_current_still_answers_its_own_callers(tmp_path):
    """The tri-state probe is additive: `schema_current` keeps its contract."""
    conn = sqlite3.connect(tmp_path / "conversations.db")
    try:
        conn.execute(f"PRAGMA user_version={_head('conversations')}")
        assert store.schema_current(conn, "conversations") is True
        conn.execute(f"PRAGMA user_version={_head('conversations') - 1}")
        assert store.schema_current(conn, "conversations") is False
    finally:
        conn.close()
