"""#825/#832 fix round A (review P3-7; Codex fix-round-1 Q4): rebuild elision
past the account-bound cutover's frozen extents.

A segment summary describes its lines WITHOUT admission, so the first
account-bound version refused elision for every byte past the cutover's frozen
extents, and every Codex-quota-only segment written after the cutover was read
in full on every rebuild. Elision is restored for such a segment only on a
versioned summary proving that EVERY decoded record in the exact extent is a
positively identified Codex quota observation — no Claude, administrative,
protocol or unknown record. Zero exclusions alone is not enough: an admitted
stamped Claude observation still feeds the quarantine and completion. The
rebuild and the prefix validation apply the same predicate; anything unproven
is scanned.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

import journal_fixture_496_s5b as F
from conftest import load_script, redirect_paths

HISTORICAL = "observations-2026-05.jsonl"
POST_BOUNDARY = "observations-2026-06.jsonl"


@pytest.fixture
def store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = importlib.import_module("_cctally_core")
    jr = importlib.import_module("_cctally_journal")
    ns["open_cache_db"]().close()
    journal_dir = pathlib.Path(core.APP_DIR) / "journal"
    F._append(journal_dir, HISTORICAL, [
        F.codex_quota_obs(line_offset=0, used_percent=10.0)])
    # The cutover lands in the current month's segment, which stays
    # canonically last; both earlier segments are elision candidates.
    jr.ensure_account_bound_cutover()
    return ns, core, jr, journal_dir


@pytest.fixture
def opened(monkeypatch, store):
    _ns, _core, jr, _dir = store
    real = jr._open_segment_for_read
    seen: list = []

    def record(seg_path):
        seen.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return seen


def _rebuild(jr):
    return jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))


def test_a_post_boundary_codex_quota_only_segment_is_elided(store, opened):
    _ns, core, jr, journal_dir = store
    F._append(journal_dir, POST_BOUNDARY, [
        F.codex_quota_obs(line_offset=10 + index, used_percent=20.0 + index)
        for index in range(2)])
    boundary = jr.find_account_bound_cutover()
    assert POST_BOUNDARY not in boundary.frozen_extents
    _rebuild(jr)  # primes the summaries and the coverage certificate

    opened.clear()
    result = _rebuild(jr)
    assert POST_BOUNDARY not in opened, opened
    assert HISTORICAL not in opened, opened
    assert result.traversal["elision"]["elidedSegments"] == 2

    # The prefix validation shares the predicate.
    opened.clear()
    assert jr.stats_index_matches_journal_prefix(
        pathlib.Path(core.DB_PATH), jr.journal_high_water())
    assert POST_BOUNDARY not in opened, opened


def test_a_post_boundary_segment_with_any_claude_record_is_scanned(
        store, opened):
    """A Codex quota segment that also carries one admitted Claude reading is
    not provably Codex-only, so it is read on every rebuild."""
    _ns, _core, jr, journal_dir = store
    import _lib_journal as J
    reading = J.make_obs(
        at="2026-06-02T00:00:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 5.0, "resets_at": 1785196800,
                 "source": "statusline", "captured_at": "2026-06-02T00:00:00Z"})
    F._append(journal_dir, POST_BOUNDARY, [
        F.codex_quota_obs(line_offset=10, used_percent=20.0), reading])
    _rebuild(jr)

    opened.clear()
    result = _rebuild(jr)
    assert POST_BOUNDARY in opened, opened
    assert HISTORICAL not in opened, opened
    assert result.traversal["elision"]["elidedSegments"] == 1


def _prime_codex_quota_only_post_boundary(jr, journal_dir):
    F._append(journal_dir, POST_BOUNDARY, [
        F.codex_quota_obs(line_offset=10 + index, used_percent=20.0 + index)
        for index in range(2)])
    _rebuild(jr)  # writes the sidecar summaries


def test_the_statusline_cutover_lookups_never_read_a_proven_segment(
        store, opened):
    """#825/#832 fix round J2: the cached cutover's precedence check scanned
    every earlier segment its absence evidence did not cover, so a
    Codex-quota-only segment written after the cutover was read on EVERY
    cutover lookup — the statusline reducer's included — undoing the elision
    the summary proves. The same versioned summary proof now stands in for
    that scan."""
    _ns, _core, jr, journal_dir = store
    statusline = importlib.import_module("_cctally_statusline")
    _prime_codex_quota_only_post_boundary(jr, journal_dir)
    first = jr.find_account_bound_cutover()

    opened.clear()
    statusline._ensure_cutover_for_publication()
    statusline._rescan_legacy_spool()
    assert jr.ensure_account_bound_cutover() == first
    assert jr.find_account_bound_cutover() == first
    assert POST_BOUNDARY not in opened, opened
    assert HISTORICAL not in opened, opened


@pytest.mark.parametrize("unproven", ["grown-after-summary", "claude-record"])
def test_a_cutover_lookup_still_scans_an_unproven_earlier_segment(
        store, opened, unproven):
    """The summary proof is fail-closed: bytes no current Codex-quota-only
    summary covers — a segment that grew after its summary was written, or
    one holding any other record — are still scanned for a preceding
    cutover op."""
    _ns, _core, jr, journal_dir = store
    import _lib_journal as J
    _prime_codex_quota_only_post_boundary(jr, journal_dir)
    if unproven == "grown-after-summary":
        extra = F.codex_quota_obs(line_offset=20, used_percent=30.0)
    else:
        extra = J.make_obs(
            at="2026-06-02T00:00:00Z", src="record-usage", provider="claude",
            payload={"weekly_percent": 5.0, "resets_at": 1785196800,
                     "source": "statusline",
                     "captured_at": "2026-06-02T00:00:00Z"})
    F._append(journal_dir, POST_BOUNDARY, [extra])
    if unproven == "claude-record":
        _rebuild(jr)  # a current summary that is not Codex-quota-only

    opened.clear()
    assert jr.find_account_bound_cutover() is not None
    assert POST_BOUNDARY in opened, opened
    assert HISTORICAL not in opened, opened
