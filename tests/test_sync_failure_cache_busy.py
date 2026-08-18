"""#583 S2 — typed SQLITE_BUSY classification.

A blanket uncorrupted-`database="cache"` branch would be too broad: any
exception raised while reading cache.db produces that attribution, and every
one would then be labelled busy and told to checkpoint. The predicate is the
exception's numeric SQLite code instead, so this stays typed attribution and
adds no raw-text matching.
"""
import importlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths

BUSY, LOCKED, BUSY_SNAPSHOT = 5, 6, 517


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    tui = importlib.import_module("_cctally_tui")
    env_mod = importlib.import_module("_cctally_dashboard_envelope")
    return ns, tui, env_mod


def _attr(tui, **kw):
    base = dict(leg="sessions", database="cache", corruption=False,
                sqlite_busy=False)
    base.update(kw)
    return tui.SyncFailureAttribution(**base)


def _capture(tui, exc, *, database="cache", leg="sessions"):
    """Run the real capture path and return the one attribution it recorded."""
    conn = sqlite3.connect(":memory:")
    try:
        errors, failures = [], []
        tui._tui_capture_sync_failure(
            conn, errors, failures, leg=leg, database=database, exc=exc,
            stats_heal_attempted=True,
        )
        assert len(failures) == 1
        return failures[0]
    finally:
        conn.close()


def test_busy_cache_is_named(mods):
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope(
        "database is locked", (_attr(tui, sqlite_busy=True),))
    assert out["kind"] == "cache_busy"
    assert out["label"] == "⚠ cache database busy"
    assert out["detail"] == (
        "The dashboard could not complete sync because cache.db stayed locked.")
    assert out["action"] == "cctally db checkpoint"


def test_extended_busy_snapshot_code_is_masked_to_the_primary(mods):
    """SQLite reports extended codes such as SQLITE_BUSY_SNAPSHOT, which this
    repository already contends with in its multi-writer locking. An unmasked
    comparison would miss the most likely case."""
    _ns, tui, _env_mod = mods
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = BUSY_SNAPSHOT      # 517 & 0xFF == 5
    assert _capture(tui, exc).sqlite_busy is True


def test_locked_code_also_counts(mods):
    _ns, tui, _env_mod = mods
    exc = sqlite3.OperationalError("database table is locked")
    exc.sqlite_errorcode = LOCKED
    assert _capture(tui, exc).sqlite_busy is True


def test_plain_busy_code_counts(mods):
    _ns, tui, _env_mod = mods
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = BUSY
    assert _capture(tui, exc).sqlite_busy is True


def test_a_codeless_exception_is_not_busy(mods):
    """Text alone must never set the flag — Preserve 10 forbids widening raw
    matching, and `database is locked` is exactly the string a serialized
    inner failure would carry."""
    _ns, tui, _env_mod = mods
    assert _capture(tui, RuntimeError("database is locked")).sqlite_busy is False


def test_non_busy_cache_failure_is_not_reclassified(mods):
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope("disk I/O error", (_attr(tui),))
    assert out["kind"] == "server_sync"


def test_stats_or_cache_busy_stays_generic(mods):
    """That value means ownership was not established, and a guess would
    produce a confidently wrong remedy."""
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope(
        "database is locked",
        (_attr(tui, database="stats_or_cache", sqlite_busy=True),))
    assert out["kind"] == "server_sync"


def test_typed_stats_ownership_still_wins_a_mixed_failure(mods):
    """The new branch sits after the typed stats, quota-projection and
    conversations branches."""
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope(
        "database is locked",
        (_attr(tui, database="stats", leg="trend"),
         _attr(tui, database="cache", sqlite_busy=True)),
    )
    assert out["kind"] == "maintenance_active"


def test_every_existing_classification_is_unchanged(mods):
    """The False default must leave the whole current vocabulary intact."""
    _ns, tui, env_mod = mods
    cases = [
        (("stats", True, False), "stats_corruption"),
        (("stats", False, False), "maintenance_active"),
        (("conversations", False, False), "server_sync"),
        (("cache", True, False), "cache_corruption"),
    ]
    for (db, corrupt, busy), expected in cases:
        out = env_mod._sync_failure_envelope(
            "something went wrong",
            (_attr(tui, database=db, corruption=corrupt, sqlite_busy=busy),))
        assert out["kind"] == expected, (db, corrupt, busy)


def test_cache_corruption_outranks_a_separate_busy_attribution(mods):
    """`attributed(...)` scans the WHOLE list, so the branch that appears
    first in the function wins even when a later branch describes a different
    attribution in the same tick.

    A tick producing a corruption-shaped cache attribution and a separate busy
    one therefore rendered "cache database busy" and dropped the corruption
    message entirely — the more urgent of the two, and the one whose remedy
    (`cache-sync --rebuild`) is not interchangeable with a checkpoint.
    """
    _ns, tui, env_mod = mods
    both = (
        _attr(tui, leg="sessions", database="cache", corruption=True),
        _attr(tui, leg="blocks", database="cache", sqlite_busy=True),
    )
    out = env_mod._sync_failure_envelope("database is locked", both)
    assert out["kind"] == "cache_corruption"
    assert out["action"] == "cctally cache-sync --rebuild"
    # Order within the tick must not decide it either.
    out = env_mod._sync_failure_envelope("database is locked", tuple(reversed(both)))
    assert out["kind"] == "cache_corruption"


def test_one_attribution_that_is_both_corrupt_and_busy_reads_as_corruption(mods):
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope(
        "database is locked",
        (_attr(tui, database="cache", corruption=True, sqlite_busy=True),))
    assert out["kind"] == "cache_corruption"


def test_quota_projection_leg_is_unchanged(mods):
    _ns, tui, env_mod = mods
    out = env_mod._sync_failure_envelope(
        "cache recovery interrupted",
        (_attr(tui, database="other", leg="quota-projection"),))
    assert out["kind"] == "quota_projection_incomplete"


def test_attribution_defaults_to_not_busy(mods):
    """Every existing construction site keeps building without the field."""
    _ns, tui, _env_mod = mods
    attr = tui.SyncFailureAttribution(
        leg="stats-open", database="stats", corruption=True)
    assert attr.sqlite_busy is False
