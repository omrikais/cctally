"""#341 Task 4 (Ruling C) — the conditional per-account CLAUDE dashboard wire.

Symmetric with the Codex ``data.accounts[]`` (``test_dashboard_accounts_wire``):

  * <=1 REAL account  -> ``provider_is_decorated("claude")`` is False, so the
    bundle never calls ``_claude_accounts_wire`` and ``data`` has NO ``accounts``
    key (byte-identical to today; the envelope goldens hold);
  * >1 REAL account   -> ``_claude_accounts_wire`` emits one card per registry
    account (+ the unattributed bucket when it has a retained snapshot), each
    drawn from the already-account-scoped ``weekly_usage_snapshots`` /
    ``weekly_cost_snapshots`` reads (Section 6 scope matrix).

The card fields are asserted against distinct per-account seed values, so the
test is non-vacuous: a wire that ignored ``account_key`` (returned the same row
for both accounts, or the aggregate) would fail.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import _cctally_account  # noqa: E402
from _cctally_dashboard_sources import _claude_accounts_wire  # noqa: E402
from _fixture_builders import (  # noqa: E402
    create_stats_db,
    seed_account,
    seed_weekly_cost_snapshot,
    seed_weekly_usage_snapshot,
)

NOW = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
ACCT_WORK = "a" * 32
ACCT_PERSONAL = "b" * 32
UNATTR = "unattributed"


def _stats_conn(tmp: Path) -> sqlite3.Connection:
    db = tmp / "stats.db"
    create_stats_db(db)
    return sqlite3.connect(db)


def _seed_two_real_accounts(conn: sqlite3.Connection) -> None:
    seed_account(
        conn, account_key=ACCT_WORK, provider="claude", natural_id="uuid-work",
        email="work@example.com", label="work", plan_type="max",
        label_source="user", first_seen_utc="2026-07-01T00:00:00Z",
        last_seen_utc="2026-07-15T00:00:00Z",
    )
    seed_account(
        conn, account_key=ACCT_PERSONAL, provider="claude",
        natural_id="uuid-personal", email="personal@example.com",
        label="personal", plan_type="pro", label_source="user",
        first_seen_utc="2026-07-02T00:00:00Z",
        last_seen_utc="2026-07-15T00:00:00Z",
    )
    # work: 42% weekly / 60% 5h / $12.50; personal: 8% weekly / 3% 5h / $1.25.
    seed_weekly_usage_snapshot(
        conn, captured_at_utc="2026-07-15T11:00:00Z", week_start_date="2026-07-13",
        week_end_date="2026-07-20", week_start_at="2026-07-13T00:00:00Z",
        week_end_at="2026-07-20T00:00:00Z", weekly_percent=42.0,
        five_hour_percent=60.0, five_hour_resets_at="2026-07-15T15:00:00Z",
        account_key=ACCT_WORK,
    )
    seed_weekly_usage_snapshot(
        conn, captured_at_utc="2026-07-15T11:00:00Z", week_start_date="2026-07-13",
        week_end_date="2026-07-20", week_start_at="2026-07-13T00:00:00Z",
        week_end_at="2026-07-20T00:00:00Z", weekly_percent=8.0,
        five_hour_percent=3.0, five_hour_resets_at="2026-07-15T15:00:00Z",
        account_key=ACCT_PERSONAL,
    )
    seed_weekly_cost_snapshot(
        conn, captured_at_utc="2026-07-15T11:00:00Z", week_start_date="2026-07-13",
        week_end_date="2026-07-20", cost_usd=12.50, account_key=ACCT_WORK,
    )
    seed_weekly_cost_snapshot(
        conn, captured_at_utc="2026-07-15T11:00:00Z", week_start_date="2026-07-13",
        week_end_date="2026-07-20", cost_usd=1.25, account_key=ACCT_PERSONAL,
    )
    conn.commit()


def test_decorated_claude_emits_per_account_cards(monkeypatch):
    # String-target form: patches the LIVE ``sys.modules["_cctally_account"]``
    # that `_claude_accounts_wire` imports at call time. cctally's
    # `_load_sibling("_cctally_account")` can replace that module object after
    # this test module captured its top-level `_cctally_account` reference, so a
    # setattr on the captured object would miss the wire in a full-suite run.
    monkeypatch.setattr(
        "_cctally_account.resolve_active_account_keys", lambda: {ACCT_WORK},
    )
    with tempfile.TemporaryDirectory() as td, closing(_stats_conn(Path(td))) as conn:
        _seed_two_real_accounts(conn)
        assert _cctally_account.provider_is_decorated(conn, "claude") is True
        cards = _claude_accounts_wire(conn, now_utc=NOW)
    by_key = {c["accountKey"]: c for c in cards}
    assert set(by_key) == {ACCT_WORK, ACCT_PERSONAL}
    work = by_key[ACCT_WORK]
    assert work["label"] == "work"
    assert work["plan"] == "max"
    assert work["active"] is True
    assert work["weeklyPercent"] == 42.0
    assert work["fiveHourPercent"] == 60.0
    assert work["resetsAt"] == "2026-07-20T00:00:00Z"
    assert work["spendUsd"] == 12.50
    assert "unattributed" not in work
    personal = by_key[ACCT_PERSONAL]
    assert personal["active"] is False
    assert personal["weeklyPercent"] == 8.0  # NOT work's 42 — proves per-account
    assert personal["spendUsd"] == 1.25


def test_single_real_account_is_undecorated_no_wire():
    """A lone real account never trips the R8 gate — the bundle skips the wire."""
    with tempfile.TemporaryDirectory() as td, closing(_stats_conn(Path(td))) as conn:
        seed_account(
            conn, account_key=ACCT_WORK, provider="claude", natural_id="uuid-work",
            email="work@example.com", label="work", plan_type="max",
        )
        conn.commit()
        assert _cctally_account.provider_is_decorated(conn, "claude") is False


def test_unattributed_bucket_appended_when_it_has_a_snapshot(monkeypatch):
    monkeypatch.setattr(
        "_cctally_account.resolve_active_account_keys", lambda: set(),
    )
    with tempfile.TemporaryDirectory() as td, closing(_stats_conn(Path(td))) as conn:
        _seed_two_real_accounts(conn)
        seed_weekly_cost_snapshot(
            conn, captured_at_utc="2026-07-15T11:00:00Z",
            week_start_date="2026-07-13", week_end_date="2026-07-20",
            cost_usd=0.75, account_key=UNATTR,
        )
        conn.commit()
        cards = _claude_accounts_wire(conn, now_utc=NOW)
    assert cards[-1]["accountKey"] == UNATTR
    assert cards[-1]["unattributed"] is True
    # dimmed / totals-only: no live weekly/5h bars, but the spend total shows.
    assert cards[-1]["weeklyPercent"] is None
    assert cards[-1]["fiveHourPercent"] is None
    assert cards[-1]["spendUsd"] == 0.75
