"""conversations.db stays transcripts-only (#500 review finding F1).

``_apply_conversations_schema`` reuses the historical monolithic cache schema —
``_apply_cache_schema`` — and then DROPs every accounting family it dragged in,
so conversation queries resolve those names through the read-only ``cache_db``
ATTACH instead of against an empty local copy.

That drop list was a hand-maintained enumeration with no guard, so #500 added
``codex_window_attributions`` to ``_apply_cache_schema`` without the matching
drop and every conversations golden silently grew the table. The hazard is not
the wasted page: conversation code reaches the accounting families through the
attachment and spells them qualified (``cache_db.codex_file_accounts``), and
SQLite resolves an UNQUALIFIED name against ``main`` first — so an empty local
copy shadows the populated one for any query that ever forgets the qualifier.

The first test is the rule, not a list: every family the #496 S5b coverage
certificate describes is by construction a cache.db accounting family, so a
future addition to ``COVERAGE_CACHE_FAMILIES`` that misses the drop fails here
without anyone editing this file. The second pins the accounting families that
predate the certificate and are therefore outside that rule.
"""
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _cctally_cache as cache  # noqa: E402
import _cctally_db as db  # noqa: E402


#: Accounting families dropped by `_apply_conversations_schema` that are NOT
#: members of `COVERAGE_CACHE_FAMILIES` — the Claude/Codex session corpora and
#: the public-#5 change ledger. Enumerated because no rule generates them.
_UNCOVERED_ACCOUNTING_FAMILIES = (
    "session_entries",
    "session_files",
    "codex_session_entries",
    "codex_session_files",
    "quota_window_change_log",
)


def _fresh_conversations_tables() -> "set[str]":
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        return {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_no_coverage_certificate_family_survives_into_conversations_db():
    present = sorted(
        set(cache.COVERAGE_CACHE_FAMILIES) & _fresh_conversations_tables())
    assert present == [], (
        "conversations.db must stay transcripts-only, but these cache.db "
        f"accounting families rode in via _apply_cache_schema: {present}. "
        "Add a DROP TABLE IF EXISTS for each to _apply_conversations_schema."
    )


def test_no_uncovered_accounting_family_survives_into_conversations_db():
    present = sorted(
        set(_UNCOVERED_ACCOUNTING_FAMILIES) & _fresh_conversations_tables())
    assert present == []
