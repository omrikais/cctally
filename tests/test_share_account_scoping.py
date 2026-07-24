"""#341 Task 4 (acceptance 7) — the server-side account dimension of shares.

Covers the three share deliverables:
  * the captured account participates in the POST body / data_digest / history
    metadata (a focus change registers as composer drift);
  * account labels route through the fail-closed kernel anonymization chokepoint
    (`anonymize_account_label`) — anon-mode maps to ``Account A/B/C``, only an
    explicit reveal shows the real label;
  * shares can NEVER leak emails — the `data.accounts[]` wire (both providers)
    carries only a label, never an email.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import _lib_share  # noqa: E402
import _cctally_dashboard_share as share  # noqa: E402
from _cctally_dashboard_sources import _claude_accounts_wire  # noqa: E402
import _cctally_account  # noqa: E402
from _fixture_builders import (  # noqa: E402
    create_stats_db,
    seed_account,
    seed_weekly_cost_snapshot,
    seed_weekly_usage_snapshot,
)

A = "a" * 32
B = "b" * 32


def test_anonymize_account_label_fail_closed():
    # anon (the default) → positional Account A/B/C by registry index.
    assert _lib_share.anonymize_account_label("work", 0, reveal=False) == "Account A"
    assert _lib_share.anonymize_account_label("personal", 1, reveal=False) == "Account B"
    assert _lib_share.anonymize_account_label("third", 2, reveal=False) == "Account C"
    # explicit reveal → the real label passes through.
    assert _lib_share.anonymize_account_label("work", 0, reveal=True) == "work"
    # a missing index (unresolved) still anonymizes (never leaks) rather than raise.
    assert _lib_share.anonymize_account_label("work", -1, reveal=False) == "Account A"


def test_share_account_selection_validates():
    assert share._share_account_selection({}) is None  # legacy → agnostic
    assert share._share_account_selection({"account": None}) is None
    assert share._share_account_selection({"account": A}) == A
    assert share._share_account_selection({"account": "unattributed"}) == "unattributed"
    with pytest.raises(ValueError):
        share._share_account_selection({"account": "not-a-key"})
    with pytest.raises(ValueError):
        share._share_account_selection({"account": 123})


def test_account_participates_in_data_digest():
    common = dict(
        panel="weekly", template_id="weekly-default", source="claude",
        source_explicit=False, states=(), snapshots=(),
        panel_data={"rows": [1, 2, 3]},
    )
    none_input = share._share_digest_input(**common, account=None)
    a_input = share._share_digest_input(**common, account=A)
    b_input = share._share_digest_input(**common, account=B)
    assert "account" not in none_input  # legacy digest byte-stable
    d_none = _lib_share._data_digest(none_input)
    d_a = _lib_share._data_digest(a_input)
    d_b = _lib_share._data_digest(b_input)
    assert d_a != d_none  # a focused account busts the drift digest
    assert d_a != d_b     # distinct accounts → distinct digests


def test_share_wire_cards_never_carry_email(monkeypatch):
    """The account cards a share reads carry a label but NEVER an email."""
    monkeypatch.setattr(
        "_cctally_account.resolve_active_account_keys", lambda: {A},
    )
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        create_stats_db(db)
        with closing(sqlite3.connect(db)) as conn:
            for key, label, email in ((A, "work", "work@example.com"),
                                      (B, "personal", "personal@example.com")):
                seed_account(
                    conn, account_key=key, provider="claude", natural_id=f"uuid-{label}",
                    email=email, label=label, plan_type="max",
                )
                seed_weekly_usage_snapshot(
                    conn, captured_at_utc="2026-07-15T11:00:00Z",
                    week_start_date="2026-07-13", week_end_date="2026-07-20",
                    weekly_percent=10.0, account_key=key,
                )
                seed_weekly_cost_snapshot(
                    conn, captured_at_utc="2026-07-15T11:00:00Z",
                    week_start_date="2026-07-13", week_end_date="2026-07-20",
                    cost_usd=1.0, account_key=key,
                )
            conn.commit()
            import datetime as dt
            cards = _claude_accounts_wire(conn, now_utc=dt.datetime(2026, 7, 15, 12, tzinfo=dt.timezone.utc))
    assert cards, "expected decorated cards"
    for c in cards:
        assert "email" not in c, f"account card leaked an email field: {c}"
        assert "work@example.com" not in str(c)
        assert "personal@example.com" not in str(c)
        # the label IS carried (it is the anonymizable user datum, not the email).
        assert "label" in c
