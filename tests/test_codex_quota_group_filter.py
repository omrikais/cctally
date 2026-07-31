"""The exact physical-group filter on ``load_codex_quota_observations``.

Public issue omrikais/cctally#5. Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``
§1, "Group expansion is mandatory".

The incremental projector reads RAW mutation scope out of the change ledger and
has to turn it into the affected windows' COMPLETE current membership before
interpreting anything — the account fold and the milestone series are both
population-dependent, so a partial group produces a WRONG block rather than a
stale one. That needs a selector the loader did not have: an exact match on a
set of physical group coordinates.

The two ways to get this wrong are both silent, and both are pinned here.
Reusing ``canonical_resets_between`` over-selects (it is an inclusive range over
one dimension, so sparse dirty groups drag in everything between the extremes,
and a single reset instant drags in every unrelated limit key and slot sharing
it). Matching the RAW ``resets_at_utc`` instead of the canonical anchor
under-selects: measured on the real store, raw grouping yields 4,064 windows
where the anchor yields 608, so every physical window fragments about
sevenfold and each fragment materializes as its own wrong block.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

import _fixture_builders
import _lib_jsonl
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
ROOT = "root-groups"
OTHER_ROOT = "root-groups-2"
SOURCE_PATH = "/codex/root-groups/rollout.jsonl"
BASE = dt.datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

FIVE_HOUR = 300
WEEK = 10_080


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limit_key(root: str, minutes: int) -> str:
    return _lib_jsonl._codex_logical_limit_key(root, "codex", "primary", minutes)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


def _seed(conn, *, root=ROOT, minutes=FIVE_HOUR, slot="primary", reset,
          anchor=None, offset, percent=5.0, captured=None, limit_key=None):
    _fixture_builders.seed_codex_quota_snapshot(
        conn,
        source_root_key=root,
        source_path=SOURCE_PATH,
        line_offset=offset,
        captured_at_utc=_iso(captured or (reset - dt.timedelta(minutes=30))),
        observed_slot=slot,
        logical_limit_key=limit_key or _limit_key(root, minutes),
        window_minutes=minutes,
        used_percent=percent,
        resets_at_utc=_iso(reset),
        canonical_resets_at_utc=None if anchor is None else _iso(anchor),
    )


def _group(root=ROOT, minutes=FIVE_HOUR, slot="primary", *, reset,
           limit_key=None):
    return (
        root, limit_key or _limit_key(root, minutes), slot, minutes, _iso(reset))


def _offsets(observations):
    return sorted(observation.line_offset for observation in observations)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A store whose groups differ on every axis the filter has to separate."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        for root in (ROOT, OTHER_ROOT):
            _fixture_builders.seed_codex_source_root(
                conn, source_root_key=root, canonical_root_path=f"/codex/{root}")
        shared_reset = BASE + dt.timedelta(hours=5)
        # offsets 0-1: the target group.
        _seed(conn, reset=shared_reset, offset=0, percent=3.0)
        _seed(conn, reset=shared_reset, offset=1, percent=4.0)
        # offset 2: same root, same slot, same reset INSTANT — different
        # duration, so a different window. A reset-only bound would take it.
        _seed(conn, reset=shared_reset, minutes=WEEK, offset=2)
        # offset 3: same root, same duration, same reset — different SLOT.
        _seed(conn, reset=shared_reset, slot="secondary", offset=3)
        # offset 4: another root entirely, sharing the reset instant.
        _seed(conn, root=OTHER_ROOT, reset=shared_reset, offset=4)
        # offset 5: the same group one window later.
        _seed(conn, reset=BASE + dt.timedelta(hours=10), offset=5)
        conn.commit()
        yield ns, quota, conn, shared_reset
    finally:
        conn.close()


def test_one_group_returns_exactly_its_own_rows(store):
    ns, quota, conn, reset = store
    observations = quota.load_codex_quota_observations(
        cache_conn=conn, physical_groups=[_group(reset=reset)])

    assert _offsets(observations) == [0, 1], (
        "the filter leaked rows from a window sharing the reset instant")


def test_several_groups_union_without_leaking(store):
    ns, quota, conn, reset = store
    observations = quota.load_codex_quota_observations(
        cache_conn=conn,
        physical_groups=[
            _group(reset=reset),
            _group(reset=BASE + dt.timedelta(hours=10)),
        ],
    )

    assert _offsets(observations) == [0, 1, 5]


def test_an_empty_group_set_selects_nothing(store):
    ns, quota, conn, _reset = store
    assert quota.load_codex_quota_observations(
        cache_conn=conn, physical_groups=[]) == ()


def test_the_canonical_anchor_is_what_matches_not_the_raw_reset(
    tmp_path, monkeypatch
):
    """The measured trap: raw grouping fragments one window about sevenfold.

    Every observation of a physical window shares ONE canonical anchor but may
    carry any number of jittered raw spellings. Matching the raw column selects
    the anchor's own row and drops its siblings, and the pass then materializes
    a block from a fraction of the window — wrong, not stale, because the
    milestone ladder and the account fold both read the whole population.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
        anchor = BASE + dt.timedelta(hours=5)
        for index, jitter in enumerate((0, 3, 7, 11, 17)):
            _seed(
                conn,
                reset=anchor + dt.timedelta(seconds=jitter),
                anchor=anchor,
                offset=index,
                percent=1.0 + index,
            )
        conn.commit()

        matched = quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=[_group(reset=anchor)])
        # The same request keyed on a raw spelling that is NOT the anchor finds
        # nothing at all — proof the predicate reads the COALESCE and not the
        # stored reset.
        raw_only = quota.load_codex_quota_observations(
            cache_conn=conn,
            physical_groups=[_group(reset=anchor + dt.timedelta(seconds=7))],
        )
    finally:
        conn.close()

    assert _offsets(matched) == [0, 1, 2, 3, 4], (
        "the group filter matched the raw reset and fragmented the window")
    assert raw_only == ()


def test_a_null_anchor_falls_back_to_the_raw_reset(tmp_path, monkeypatch):
    """A pre-#416 row carries no anchor and every reader falls back to its raw
    reset, so the group predicate has to fall back with them."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
        reset = BASE + dt.timedelta(hours=5)
        _seed(conn, reset=reset, anchor=None, offset=0)
        conn.execute(
            "UPDATE quota_window_snapshots SET canonical_resets_at_utc=NULL")
        conn.commit()

        observations = quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=[_group(reset=reset)])
    finally:
        conn.close()

    assert _offsets(observations) == [0]


def test_many_groups_are_all_returned_exactly_once(tmp_path, monkeypatch):
    """Each group is its own indexed query, so the union has to be assembled.

    A per-group shard that dropped or double-counted a group would be invisible
    on a store with two or three windows, which is every other test here.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    count = 250
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
        resets = [
            BASE + dt.timedelta(minutes=FIVE_HOUR * (index + 1))
            for index in range(count)
        ]
        for index, reset in enumerate(resets):
            _seed(conn, reset=reset, offset=index)
        conn.commit()

        observations = quota.load_codex_quota_observations(
            cache_conn=conn,
            physical_groups=[_group(reset=reset) for reset in resets],
        )
    finally:
        conn.close()

    assert _offsets(observations) == list(range(count))
    assert len(observations) == count, "a group was loaded more than once"


def test_the_returned_order_matches_an_unbounded_load(tmp_path, monkeypatch):
    """A per-group union is not globally ordered unless it is re-sorted.

    The incremental and whole-history passes are compared row-for-row by the
    equivalence oracle, so the bounded load has to reproduce the unbounded
    path's total order rather than the order the shards happened to run in.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    count = 40
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
        resets = [
            BASE + dt.timedelta(minutes=FIVE_HOUR * (index + 1))
            for index in range(count)
        ]
        for index, reset in enumerate(resets):
            _seed(conn, reset=reset, offset=index)
        conn.commit()

        bounded = quota.load_codex_quota_observations(
            cache_conn=conn,
            physical_groups=[_group(reset=reset) for reset in resets],
        )
        unbounded = quota.load_codex_quota_observations(cache_conn=conn)
    finally:
        conn.close()

    assert bounded == unbounded


@pytest.mark.parametrize("kwargs", [
    {"max_rows": 5},
    {"physical_signatures": {}},
])
def test_incompatible_bounds_are_refused(store, kwargs):
    """Both combinations would fail QUIETLY rather than loudly.

    ``max_rows`` appends its ORDER BY/LIMIT parameters after the group
    disjunction's, and a signature accumulated from a bounded cursor certifies a
    fraction of the root's evidence while claiming to certify all of it.
    """
    ns, quota, conn, reset = store
    with pytest.raises(ValueError):
        quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=[_group(reset=reset)], **kwargs)


@pytest.mark.parametrize("group", [
    ("root", "limit", "primary", 300),                       # too short
    ("root", "limit", "primary", "300", "2026-07-20T05:00:00Z"),  # str minutes
    ("root", "limit", "primary", True, "2026-07-20T05:00:00Z"),   # bool minutes
])
def test_malformed_group_coordinates_are_refused(store, group):
    """A string ``window_minutes`` compares unequal against an INTEGER column
    under SQLite's affinity rules, so it would select nothing and read as a
    clean window rather than as a bad request."""
    ns, quota, conn, _reset = store
    with pytest.raises(ValueError):
        quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=[group])


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
