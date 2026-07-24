"""#341 Task 4 (spec §4 finding 10) — server-side account row-ownership on the
source-detail lookup.

The leak this guards: two accounts can share ONE opaque resource key (a decorated
wire row carries an ``account_key`` sibling — e.g. two accounts observing one
physical quota window). A key-only lookup returns whichever row comes first, so a
detail fetch qualified to account X could surface account Y's row. The fix: when
``account`` is supplied and the matched rows are account-scoped, the lookup
returns ONLY the account-owned row and raises ``SourceResourceNotFound`` rather
than leak another account's data.

The RED evidence (captured in the implementing commit body): with the ownership
branch removed, ``account=X`` returns Y's row — the leak these assertions catch.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from _cctally_dashboard_sources import (  # noqa: E402
    SourceResourceNotFound,
    source_detail_lookup,
)

ACCT_X = "a" * 32
ACCT_Y = "b" * 32
SHARED_KEY = "codex:block:shared-physical-window"


def _bundle_two_accounts_one_key():
    """A decorated bundle: two block rows sharing ONE opaque key, Y listed FIRST
    (so a key-only lookup would return Y — the leak direction)."""
    row_y = {"key": SHARED_KEY, "account_key": ACCT_Y, "owner": "Y"}
    row_x = {"key": SHARED_KEY, "account_key": ACCT_X, "owner": "X"}
    state = SimpleNamespace(
        availability="ok",
        data={"quota": {"blocks": [row_y, row_x]}},
    )
    return SimpleNamespace(sources={"codex": state})


def test_account_qualified_lookup_returns_the_owned_row_never_the_other():
    bundle = _bundle_two_accounts_one_key()
    # X's request must return X's row — NOT Y's (which is listed first).
    got_x = source_detail_lookup(bundle, "codex", "block", SHARED_KEY, account=ACCT_X)
    assert got_x["owner"] == "X"
    assert got_x["account_key"] == ACCT_X
    # Y's request must return Y's row.
    got_y = source_detail_lookup(bundle, "codex", "block", SHARED_KEY, account=ACCT_Y)
    assert got_y["owner"] == "Y"


def test_unowned_account_is_not_found_not_leaked():
    """A key that resolves only to other accounts' rows must 404, never leak."""
    bundle = _bundle_two_accounts_one_key()
    with pytest.raises(SourceResourceNotFound):
        source_detail_lookup(
            bundle, "codex", "block", SHARED_KEY, account="c" * 32,
        )


def test_no_account_qualifier_is_byte_stable_first_match():
    """account=None keeps today's behavior: the first key match (byte-stable)."""
    bundle = _bundle_two_accounts_one_key()
    got = source_detail_lookup(bundle, "codex", "block", SHARED_KEY)
    assert got["owner"] == "Y"  # the first-listed row, unchanged from today


def test_account_agnostic_rows_ignore_the_qualifier():
    """Rows WITHOUT account_key (sessions/projects per R4, undecorated sources)
    ignore the qualifier and match by key — the undecorated path is unchanged."""
    row = {"key": "codex:session:s1"}  # no account_key
    state = SimpleNamespace(availability="ok", data={"sessions": {"rows": [row]}})
    bundle = SimpleNamespace(sources={"codex": state})
    got = source_detail_lookup(
        bundle, "codex", "session", "codex:session:s1", account=ACCT_X,
    )
    assert got["key"] == "codex:session:s1"
