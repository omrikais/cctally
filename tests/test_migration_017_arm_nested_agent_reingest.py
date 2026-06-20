"""Unit coverage for cache migration ``017_arm_nested_agent_reingest`` (#217 S1 /
U6) — the flag-only reingest that re-links existing >16 KB nested-subagent
grandchildren whose ``agentId:`` trailer landed past the 16 KB tool_result clip.

017 is FLAG-ONLY: it arms the DISTINCT
``cache_meta['conversation_reingest_nested_agent_pending'] = '1'`` flag so the
flock-held #179 resumable per-file reingest re-parses every JSONL through the
parser that now stamps a structured ``agent_id`` at INGEST (over the FULL raw,
before the clip). It MUST NOT re-arm the shared ``conversation_reingest_pending``
(which also gates the kernel's migration-005 read-time human-fallback — re-arming
it could misclassify a genuine human prompt during the pre-reingest window).

The five wiring sites in ``bin/_cctally_cache.py`` (``_TARGETED_DECLINE_FLAGS``,
``_REINGEST_FLAG_KEYS``, both flag SELECTs, both cleanup DELETE lists) are
exercised here too: ``sync_cache`` must trigger the offset-0 backfill on the new
flag and clear it (NOT the shared flag) when the reingest completes.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest


BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "017_arm_nested_agent_reingest"
_FLAG = "conversation_reingest_nested_agent_pending"
_SHARED_FLAG = "conversation_reingest_pending"


@pytest.fixture(scope="module")
def cctally_module():
    """Load bin/cctally once per module (registers the cache migrations)."""
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _flag(conn, key) -> "str | None":
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def _fresh_cache_db(cctally_module, path):
    db = sys.modules["_cctally_db"]
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    db._apply_cache_schema(conn)
    return conn


def test_017_registered_and_contiguous(cctally_module):
    """017 is registered at its contiguous position in the cache registry (its
    ``017`` numeric prefix matches its 1-based index), so the import-time
    contiguity assertion held. (017 was the registry head when it landed; #217 S2
    appended 018, so this no longer asserts 017 is *last* — only that it sits at
    index 17.)"""
    names = [m.name for m in cctally_module._CACHE_MIGRATIONS]
    assert _MIGRATION in names
    assert names.index(_MIGRATION) + 1 == 17, "017 must sit at registry index 17"


def test_017_arms_nested_agent_reingest_flag(cctally_module, tmp_path):
    """The handler arms the DISTINCT nested-agent reingest flag and does NOT touch
    the shared conversation_reingest_pending flag."""
    conn = _fresh_cache_db(cctally_module, tmp_path / "cache.db")
    try:
        assert _flag(conn, _FLAG) is None
        assert _flag(conn, _SHARED_FLAG) is None

        _handler(cctally_module)(conn)

        assert _flag(conn, _FLAG) == "1", "handler must arm the distinct flag"
        assert _flag(conn, _SHARED_FLAG) is None, \
            "handler must NOT re-arm the shared conversation_reingest_pending flag"
    finally:
        conn.close()


def test_017_handler_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and leaves the flag set to '1'."""
    conn = _fresh_cache_db(cctally_module, tmp_path / "cache.db")
    try:
        handler = _handler(cctally_module)
        handler(conn)
        handler(conn)
        assert _flag(conn, _FLAG) == "1"
    finally:
        conn.close()


def test_017_flag_wired_into_all_five_cctally_cache_sites(cctally_module):
    """The flag must appear at all five reingest-wiring sites in _cctally_cache.py
    (the _TARGETED_DECLINE_FLAGS tuple, the _REINGEST_FLAG_KEYS tuple, the
    resumable-reingest flag SELECT, and BOTH cleanup DELETE lists). Missing one
    either never triggers the reingest or re-arms the flag forever."""
    cc = sys.modules["_cctally_cache"]
    assert _FLAG in cc._TARGETED_DECLINE_FLAGS
    assert _FLAG in cc._REINGEST_FLAG_KEYS
    # The two cleanup DELETEs + the flag SELECT are SQL string literals; assert
    # the flag name appears in the module source the requisite number of times.
    src = Path(cc.__file__).read_text()
    # tuple membership (2) + flag SELECT (1) + two cleanup DELETEs (2) = 5 code
    # sites (comments may add more occurrences, so assert AT LEAST 5).
    assert src.count(_FLAG) >= 5, (
        f"{_FLAG} must be wired into all five reingest sites; "
        f"found {src.count(_FLAG)} occurrences"
    )
