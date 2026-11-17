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
import json

import pytest

import _fixture_builders
import _lib_jsonl
import _lib_quota_ledger
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


# ── #1071: the loading-unit closure ───────────────────────────────────────

SPARK = "gpt-5.3-codex-spark"
UNIT_RESET = BASE + dt.timedelta(days=7)


def _week_key(minutes=WEEK, *, model=None, pool=None, root=ROOT, slot="primary"):
    key = _lib_jsonl._codex_logical_limit_key(root, "codex", slot, minutes, model)
    if pool is not None:
        payload = json.loads(key)
        payload["modelPool"] = pool
        key = _lib_jsonl._codex_canonical_json(payload)
    return key


def _unit(root=ROOT, slot="primary", reset=UNIT_RESET):
    return _lib_quota_ledger.loading_unit_from_raw(
        (root, _week_key(root=root, slot=slot), slot, WEEK, _iso(reset)))


#: Each case: the (key, column minutes) spellings the unit's rows are stored
#: under. Every one of them is the same weekly window.
_SPELLINGS = {
    "spark_only": [(_week_key(model=SPARK), WEEK)],
    "mixed": [(_week_key(), WEEK), (_week_key(model=SPARK), WEEK)],
    "new_pool": [(_week_key(), WEEK), (_week_key(pool="future-pool"), WEEK)],
    "minute_variants": [
        (_week_key(WEEK + 1), WEEK),
        (_week_key(WEEK), WEEK - 1),
        (_week_key(WEEK - 1, model=SPARK), WEEK + 1),
    ],
}


def _seed_unit_store(conn, spellings):
    _fixture_builders.seed_codex_source_root(
        conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
    _fixture_builders.seed_codex_source_root(
        conn, source_root_key=OTHER_ROOT,
        canonical_root_path=f"/codex/{OTHER_ROOT}")
    offset = 0
    for key, minutes in spellings:
        for _ in range(2):
            _seed(conn, minutes=minutes, limit_key=key, reset=UNIT_RESET,
                  offset=offset)
            offset += 1
    unit_offsets = list(range(offset))
    # Unrelated units, each differing from the unit on exactly one axis.
    unrelated = [
        dict(minutes=WEEK, limit_key=_week_key(model=SPARK),
             reset=UNIT_RESET + dt.timedelta(days=7)),           # reset
        dict(minutes=WEEK, slot="secondary",
             limit_key=_week_key(model=SPARK, slot="secondary"),
             reset=UNIT_RESET),                                   # slot
        dict(minutes=FIVE_HOUR, reset=UNIT_RESET),                # length
        dict(root=OTHER_ROOT, minutes=WEEK,
             limit_key=_week_key(model=SPARK, root=OTHER_ROOT),
             reset=UNIT_RESET),                                   # root
    ]
    for kwargs in unrelated:
        _seed(conn, offset=offset, **kwargs)
        offset += 1
    conn.commit()
    return unit_offsets


@pytest.mark.parametrize("case", sorted(_SPELLINGS))
def test_unit_closure_discovers_every_stored_spelling(
    tmp_path, monkeypatch, case,
):
    """Detects #1071's partial load: the closure must reach every stored key
    spelling of a loading unit — the ``modelPool`` form, a pool name no code
    knows yet, and the minute jitter in the key and in the column — so loading
    it hands the projector the unit's complete population."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        unit_offsets = _seed_unit_store(conn, _SPELLINGS[case])
        closure = quota.codex_unit_loading_closure(conn, {_unit()})
        covered = {group[:4] for group in closure}
        for key, minutes in _SPELLINGS[case]:
            assert (ROOT, key, "primary", minutes) in covered, (key, minutes)
        observations = quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=closure)
    finally:
        conn.close()
    assert _offsets(observations) == unit_offsets


def test_unit_closure_excludes_unrelated_units(tmp_path, monkeypatch):
    """Detects an over-wide closure: a window sharing the unit's key but not
    its reset, slot, length or root must not be loaded with it."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        unit_offsets = _seed_unit_store(conn, _SPELLINGS["mixed"])
        closure = quota.codex_unit_loading_closure(conn, {_unit()})
        assert {group[0] for group in closure} == {ROOT}
        assert {group[2] for group in closure} == {"primary"}
        observations = quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=closure)
    finally:
        conn.close()
    assert _offsets(observations) == unit_offsets


def test_unit_closure_keeps_a_unit_with_no_stored_row(tmp_path, monkeypatch):
    """A unit whose rows were all deleted still names its own key, so the
    request is never empty for a unit the pass was told about."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=ROOT, canonical_root_path=f"/codex/{ROOT}")
        conn.commit()
        closure = quota.codex_unit_loading_closure(conn, {_unit()})
    finally:
        conn.close()
    assert (ROOT, _week_key(), "primary", WEEK) in {g[:4] for g in closure}


def test_unit_closure_seeks_the_physical_group_index(tmp_path, monkeypatch):
    """Detects key discovery that stops using ``idx_qws_physical_group``: a
    read of the root's rows by any other path is a history-sized scan on the
    hook, so every statement the closure runs over ``quota_window_snapshots``
    must be planned on that index."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        _seed_unit_store(conn, _SPELLINGS["minute_variants"])
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            quota.codex_unit_loading_closure(
                conn, {_unit(), _unit(reset=UNIT_RESET + dt.timedelta(days=7))})
        finally:
            conn.set_trace_callback(None)
        reads = [sql for sql in statements
                 if "quota_window_snapshots" in sql
                 and "sqlite_master" not in sql]
        assert reads, statements
        for sql in reads:
            plan = [str(row[-1]) for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sql)]
            table_lines = [
                line for line in plan if "quota_window_snapshots" in line]
            assert table_lines and all(
                "idx_qws_physical_group" in line for line in table_lines), (
                sql, plan)
    finally:
        conn.close()


def test_unit_closure_fallback_without_the_index_returns_the_same_set(
    tmp_path, monkeypatch,
):
    """Detects an incomplete key list on a store missing the index: the
    fallback must return exactly what the indexed discovery returns."""
    ns, quota = _load(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conn.row_factory = None
    try:
        _seed_unit_store(conn, _SPELLINGS["minute_variants"]
                         + _SPELLINGS["new_pool"])
        units = {_unit(), _unit(slot="secondary"), _unit(root=OTHER_ROOT)}
        indexed = quota.codex_unit_loading_closure(conn, units)
        conn.execute("DROP INDEX idx_qws_physical_group")
        conn.commit()
        fallback = quota.codex_unit_loading_closure(conn, units)
    finally:
        conn.close()
    assert fallback == indexed


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
