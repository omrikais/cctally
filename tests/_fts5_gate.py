"""The ONE FTS5 capability gate (#630 S6, F44).

Fifty-seven capability-skip decisions across seven test modules each decided,
independently, whether this SQLite build can create an FTS5 table. Fifty-five
were an inline `if not db._fts5_available(conn): pytest.skip(...)`, two were a
`@pytest.mark.skipif` over a file-local reimplementation of the probe in
`tests/test_fixture_builder_contract.py`. Fifty-seven copies of one decision is
fifty-seven places for it to drift, and the file-local reimplementation had
already drifted into a second probe with its own reason string.

TWO ADAPTERS, because the sites are two shapes and `tests/_agentmem_gate.py`'s
single mark object fits only one of them. `requires_fts5` is the decorator form.
`require_fts5()` is the inline form, which is where 55 of the 57 sites are: they
sit INSIDE the test body after a connection has been opened, so a decorator
cannot replace them without restructuring every one.

NO CUSTOM MARKER IS REGISTERED HERE, and none may be. `pytest.ini` records a
settled decision that this estate uses only pytest built-ins and registers no
marker speculatively; `tests/_agentmem_gate.py` carries the same note for the
same reason.

ONE PROBE, RUN ONCE. The capability is a property of the linked SQLite library,
not of any particular connection, so it cannot change between two connections in
one interpreter. The production seam `_cctally_db._fts5_available(conn)` probes
per connection because production code holds one and must not open another; a
test module asking "can this build do FTS5 at all" is asking a different
question with a constant answer.

THIS GATE IS NOT THE PRODUCTION SEAM AND MUST NOT BE CONFUSED WITH IT. Tests
that `monkeypatch.setattr(db, "_fts5_available", lambda conn: False)` are
exercising the LIKE-fallback branch of the product, and every one of those stays
exactly as it is. So do the conditional branches and the one ternary that assert
what the product does WITHOUT FTS5: collapsing those behind a skip would delete
coverage rather than consolidate it. `bin/_lib-fts5-probe.sh`, which the
authoritative admission path uses, is untouched — it answers the same question
for a shell caller in a different process.

The reason strings are unchanged from the sites they replace. Fifty-four inline
sites said "sqlite build lacks FTS5", one said "FTS5 unavailable on this SQLite
build", and both decorator sites said "this SQLite has no FTS5"; the `reason`
argument exists so that one outlier still says what it always said, rather than
being silently renormalized by a cleanup.
"""
from __future__ import annotations

import sqlite3

import pytest

INLINE_REASON = "sqlite build lacks FTS5"
DECORATOR_REASON = "this SQLite has no FTS5"


def _probe() -> bool:
    """Can the linked SQLite create an FTS5 table? Answered once, at import."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


FTS5_AVAILABLE = _probe()

requires_fts5 = pytest.mark.skipif(not FTS5_AVAILABLE, reason=DECORATOR_REASON)


def require_fts5(reason: str = INLINE_REASON) -> None:
    """Skip the calling test when this SQLite build has no FTS5.

    The call replaces the SKIP, never the condition guarding it. Several
    parametrized tests exercise the FTS and fallback paths together and gate only
    the FTS case; there the form is `if fa: require_fts5()`, and replacing the
    whole block with a bare call would skip the fallback case too — deleting
    coverage that an FTS5-capable runner can never expose.
    """
    if not FTS5_AVAILABLE:
        pytest.skip(reason)
