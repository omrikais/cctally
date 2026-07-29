"""#416 Task 14 / spec section 6 (review F19) — population-aware display labels.

Two Codex accounts auto-label as `omrikais@me.com`: the `pro` and one `team`
account share the email, and `account_key` correctly differs because it derives
from the `chatgpt_account_id + email` pair. Nothing disambiguated them, so the
chip, the alert prefix and the share label all read the same for two different
accounts.

Collision handling cannot live in `account_label` / `account_label_from_row`:
both are SCALAR — one row, no population and no plan context — so they cannot
know a label collides. A provider-population display-label MAP is built instead.

Two constraints keep it honest:

* D5 — auto-disambiguate ONLY on collision. A non-colliding label is returned
  untouched, so a single-account install and every existing golden are
  unaffected.
* `resolve_account_ref` accepts only STORED labels, emails or key prefixes
  (`bin/_lib_accounts.py`), so a GENERATED label is not a resolvable ref. Any
  surface that prints one for the user to type back must print a resolvable key
  prefix beside it.
"""
from __future__ import annotations

import sqlite3

import pytest

import _cctally_account as acct
import _lib_accounts


PRO = "1" * 32
TEAM_A = "2" * 32
TEAM_B = "3" * 32
GMAIL = "4" * 32
COLLIDING_EMAIL = "omrikais@me.com"


@pytest.fixture
def stats_conn(tmp_path):
    import _cctally_core
    conn = sqlite3.connect(tmp_path / "stats.db")
    conn.executescript(
        "CREATE TABLE accounts ("
        " account_key TEXT PRIMARY KEY, provider TEXT NOT NULL,"
        " natural_id TEXT, email TEXT, label TEXT, plan_type TEXT,"
        " label_source TEXT, first_seen_utc TEXT, last_seen_utc TEXT)"
    )
    assert _cctally_core is not None
    yield conn
    conn.close()


def _seed(conn, rows):
    for order, row in enumerate(rows):
        conn.execute(
            "INSERT INTO accounts (account_key, provider, natural_id, email, "
            "label, plan_type, label_source, first_seen_utc, last_seen_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (row["account_key"], row.get("provider", "codex"),
             row.get("natural_id"), row.get("email"), row.get("label"),
             row.get("plan_type"), row.get("label_source", "auto"),
             f"2026-07-0{order + 1}T00:00:00Z", "2026-07-10T00:00:00Z"),
        )
    conn.commit()


def _live_population(conn):
    _seed(conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, plan_type="pro"),
        dict(account_key=TEAM_A, email=COLLIDING_EMAIL, plan_type="team"),
        dict(account_key=GMAIL, email="omrikais@gmail.com", plan_type="plus"),
    ])


def test_colliding_emails_gain_a_plan_discriminator(stats_conn):
    _live_population(stats_conn)
    labels = acct.display_label_map(stats_conn, "codex")
    assert labels[PRO] == f"{COLLIDING_EMAIL} (pro)"
    assert labels[TEAM_A] == f"{COLLIDING_EMAIL} (team)"


def test_a_non_colliding_label_is_untouched(stats_conn):
    """D5: auto-disambiguate ONLY on collision."""
    _live_population(stats_conn)
    labels = acct.display_label_map(stats_conn, "codex")
    assert labels[GMAIL] == "omrikais@gmail.com"


def test_a_single_account_provider_is_never_decorated(stats_conn):
    _seed(stats_conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, plan_type="pro")])
    assert acct.display_label_map(stats_conn, "codex") == {
        PRO: COLLIDING_EMAIL}


def test_a_plan_that_does_not_disambiguate_falls_back_to_a_key_prefix(
        stats_conn):
    """Two `team` accounts on one email is exactly the case D1 says a heuristic
    cannot separate, so the discriminator must be the one thing that IS unique:
    a resolvable key prefix. The pro account in the same group still gets the
    readable plan discriminator — partial disambiguation is not all-or-nothing."""
    _seed(stats_conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, plan_type="pro"),
        dict(account_key=TEAM_A, email=COLLIDING_EMAIL, plan_type="team"),
        dict(account_key=TEAM_B, email=COLLIDING_EMAIL, plan_type="team"),
    ])
    labels = acct.display_label_map(stats_conn, "codex")
    assert labels[PRO] == f"{COLLIDING_EMAIL} (pro)"
    assert labels[TEAM_A] == f"{COLLIDING_EMAIL} ({TEAM_A[:8]})"
    assert labels[TEAM_B] == f"{COLLIDING_EMAIL} ({TEAM_B[:8]})"
    assert len(set(labels.values())) == 3, "the map must be injective"


def test_a_missing_plan_falls_back_to_a_key_prefix(stats_conn):
    _seed(stats_conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, plan_type=None),
        dict(account_key=TEAM_A, email=COLLIDING_EMAIL, plan_type=None),
    ])
    labels = acct.display_label_map(stats_conn, "codex")
    assert labels[PRO] == f"{COLLIDING_EMAIL} ({PRO[:8]})"
    assert labels[TEAM_A] == f"{COLLIDING_EMAIL} ({TEAM_A[:8]})"


def test_a_manual_label_still_wins_as_the_base(stats_conn):
    """`cctally account label` sits at the top of the user > switcher > auto
    precedence, so it is the BASE the map decorates — it is not overridden by
    the email. It is still disambiguated when the USER creates a collision,
    because a display that reads the same for two accounts is ambiguous however
    it was produced."""
    _seed(stats_conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, label="work",
             plan_type="pro", label_source="user"),
        dict(account_key=GMAIL, email="omrikais@gmail.com", label="personal",
             plan_type="plus", label_source="user"),
    ])
    labels = acct.display_label_map(stats_conn, "codex")
    assert labels[PRO] == "work"
    assert labels[GMAIL] == "personal"


def test_a_manual_collision_is_still_disambiguated(stats_conn):
    _seed(stats_conn, [
        dict(account_key=PRO, email=COLLIDING_EMAIL, label="work",
             plan_type="pro", label_source="user"),
        dict(account_key=TEAM_A, email="other@example.com", label="Work",
             plan_type="team", label_source="user"),
    ])
    labels = acct.display_label_map(stats_conn, "codex")
    assert len(set(labels.values())) == 2, (
        "two manually-identical labels still render identically")
    assert labels[PRO].startswith("work")
    assert labels[TEAM_A].startswith("Work")


def test_the_map_is_scoped_to_one_provider(stats_conn):
    """A Claude account sharing a Codex account's email is NOT a collision —
    the two never appear in one list."""
    _seed(stats_conn, [
        dict(account_key=PRO, provider="codex", email=COLLIDING_EMAIL,
             plan_type="pro"),
        dict(account_key=TEAM_A, provider="claude", email=COLLIDING_EMAIL,
             plan_type="max"),
    ])
    assert acct.display_label_map(stats_conn, "codex") == {
        PRO: COLLIDING_EMAIL}
    assert acct.display_label_map(stats_conn, "claude") == {
        TEAM_A: COLLIDING_EMAIL}


def test_the_sentinels_are_never_decorated(stats_conn):
    _live_population(stats_conn)
    assert acct.display_account_label(
        stats_conn, _lib_accounts.UNATTRIBUTED) == "Unattributed"
    assert acct.display_account_label(
        stats_conn, _lib_accounts.VENDOR_WIDE) == "All accounts"


def test_the_single_key_lookup_agrees_with_the_map(stats_conn):
    """Every scalar consumer (the alert prefix, the share label, `--account`
    JSON) goes through this, so it must return exactly what the population map
    would."""
    _live_population(stats_conn)
    labels = acct.display_label_map(stats_conn, "codex")
    for key, expected in labels.items():
        assert acct.display_account_label(stats_conn, key) == expected


def test_an_unknown_key_degrades_to_the_scalar_label(stats_conn):
    _live_population(stats_conn)
    unknown = "9" * 32
    assert acct.display_account_label(stats_conn, unknown) == unknown[:8]


def test_ambiguity_candidates_print_a_resolvable_prefix(stats_conn, capsys):
    """A GENERATED label is not a resolvable ref — `resolve_account_ref` accepts
    only stored labels, emails and key prefixes — so the candidate list must
    print a key prefix the user can actually type back."""
    _live_population(stats_conn)
    with pytest.raises(_lib_accounts.AccountRefError):
        _lib_accounts.resolve_account_ref(stats_conn, COLLIDING_EMAIL, "codex")
    key = acct._resolve_ref_or_exit(stats_conn, COLLIDING_EMAIL)
    assert key is None
    err = capsys.readouterr().err
    assert PRO[:8] in err and TEAM_A[:8] in err
    assert f"{COLLIDING_EMAIL} (pro)" in err
    assert f"{COLLIDING_EMAIL} (team)" in err
