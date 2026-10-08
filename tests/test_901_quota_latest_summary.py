"""#901 W1/W3a (spec §5.1, T1, G2): the loader reads the maintained summary,
streams its general populations, and no population changes.

Every equivalence assertion compares the summary path with the window path on
the same store. The window path is reached by reporting the summary as not
ready, which is exactly what a store without migration 047's objects does. The
plan assertions run the real loader under ``capture_sql_plans()``.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
WEEK_RESET = NOW + dt.timedelta(days=3)
FIVE_HOUR_RESET = NOW + dt.timedelta(hours=2)
ROOT_LIVE = "a" * 32
ROOT_ANCIENT = "b" * 32
ROOT_RETIRED = "c" * 32
ACCOUNT_A = "1" * 32
ACCOUNT_B = "2" * 32
MIGRATION = "047_spill_free_read_paths"
LOADER_ROW_SITES = {
    "_cctally_quota._iter_shard_rows",
    "_cctally_quota._codex_quota_latest_rows",
    "_cctally_quota.load_codex_quota_observations",
}


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _observation(cache, *, root, limit, captured, resets, percent, line_offset,
                 window_minutes=10080, slot="primary", account_key=None,
                 observed_model=None, limit_name=None, canonical=None,
                 verb="INSERT"):
    if root != ROOT_RETIRED:
        cache.execute(
            "INSERT OR IGNORE INTO codex_source_roots (source_root_key,"
            " canonical_root_path, first_seen_utc, last_seen_utc)"
            " VALUES (?,?,?,?)",
            (root, f"/synthetic/codex/{root[:4]}", captured, captured))
    cache.execute(
        f"{verb} INTO quota_window_snapshots (source, source_root_key,"
        " source_path, line_offset, captured_at_utc, observed_slot,"
        " logical_limit_key, limit_id, limit_name, window_minutes,"
        " used_percent, resets_at_utc, account_key, observed_model,"
        " canonical_resets_at_utc)"
        " VALUES ('codex', ?, ?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, ?)",
        (root, f"/synthetic/codex/{root[:4]}/rollout.jsonl", line_offset,
         captured, slot, limit, limit_name, window_minutes, percent, resets,
         account_key, observed_model, canonical))


def _seed(cache, *, account_order=(ACCOUNT_A, ACCOUNT_B)):
    offset = 0

    def add(**kwargs):
        nonlocal offset
        offset += 1
        _observation(cache, line_offset=offset, **kwargs)

    for day in range(4):  # an identity whose whole history is a year old
        add(root=ROOT_ANCIENT, limit="weekly",
            captured=_iso(NOW - dt.timedelta(days=400 - day)),
            resets=_iso(NOW - dt.timedelta(days=393 - day)), percent=10.0 + day)
    for day in range(3):  # a root no longer in codex_source_roots
        add(root=ROOT_RETIRED, limit="weekly",
            captured=_iso(NOW - dt.timedelta(days=90 - day)),
            resets=_iso(NOW - dt.timedelta(days=83 - day)), percent=40.0 + day)
    for minute in range(6):
        add(root=ROOT_LIVE, limit="weekly",
            captured=_iso(NOW - dt.timedelta(minutes=30 - minute)),
            resets=_iso(WEEK_RESET), percent=50.0 + minute)
    tie = NOW - dt.timedelta(minutes=25)
    add(root=ROOT_LIVE, limit="weekly",
        captured=_iso(tie).replace("Z", ".400000Z"),
        resets=_iso(WEEK_RESET), percent=55.5)
    add(root=ROOT_LIVE, limit="weekly",
        captured=_iso(tie).replace("Z", "+00:00"),
        resets=_iso(WEEK_RESET), percent=55.7)
    for account in account_order:  # two accounts on one physical window
        for minute in range(3):
            add(root=ROOT_LIVE, limit="weekly",
                captured=_iso(NOW - dt.timedelta(minutes=20 - minute)),
                resets=_iso(WEEK_RESET), percent=51.0 + minute,
                account_key=account)
    for model, name in ((None, None), ("", None),
                        ("gpt-5.3-codex-spark", "GPT-5.3-Codex-Spark")):
        add(root=ROOT_LIVE, limit="weekly", slot="secondary",
            captured=_iso(NOW - dt.timedelta(minutes=10)),
            resets=_iso(WEEK_RESET), percent=30.0, observed_model=model,
            limit_name=name)
    for minute in range(4):
        add(root=ROOT_LIVE, limit="five_hour", window_minutes=300,
            captured=_iso(NOW - dt.timedelta(minutes=8 - minute)),
            resets=_iso(FIVE_HOUR_RESET), percent=20.0 + minute,
            account_key="" if minute % 2 else None)
    add(root=ROOT_LIVE, limit="weekly", slot=" ", captured=_iso(NOW),
        resets=_iso(WEEK_RESET), percent=99.0)  # refused: blank slot
    cache.commit()


@pytest.fixture
def store(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    try:
        _seed(cache)
    finally:
        cache.close()
    return ns


def _both_paths(quota, monkeypatch, **kwargs):
    summary = quota.load_codex_quota_observations(
        latest_per_identity=True, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(quota, "_codex_quota_latest_summary_ready",
                      lambda _objects: False)
        window = quota.load_codex_quota_observations(
            latest_per_identity=True, **kwargs)
    return summary, window


def _attribution_revision(cache):
    import _cctally_cache

    cache.execute(
        "INSERT INTO codex_window_attributions (op_id, account_key,"
        " source_root_key, logical_limit_key, observed_slot, window_minutes,"
        " raw_resets_at_utc, canonical_resets_at_utc, asserted_at_utc,"
        " retracted_by_op_id) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        ("o:901", ACCOUNT_A, ROOT_LIVE, "five_hour", "primary", 300,
         json.dumps([_iso(FIVE_HOUR_RESET)], separators=(",", ":")),
         _iso(FIVE_HOUR_RESET), "2026-07-20T12:00:00.000000Z"))
    _cctally_cache.bump_codex_window_attribution_revision(cache)


def _canonical_reset_update(cache):
    cache.execute(
        "UPDATE quota_window_snapshots SET canonical_resets_at_utc=?"
        " WHERE logical_limit_key='weekly' AND source_root_key=?",
        (_iso(WEEK_RESET - dt.timedelta(minutes=5)), ROOT_LIVE))


def _delete_a_tied_maximum(cache):
    cache.execute(
        "DELETE FROM quota_window_snapshots WHERE id IN (SELECT id FROM"
        " quota_window_snapshots WHERE source_root_key=?"
        " AND logical_limit_key='weekly' AND account_key IS NULL"
        " AND observed_slot='primary'"
        " AND unixepoch(captured_at_utc)=unixepoch(?) LIMIT 1)",
        (ROOT_LIVE, _iso(NOW - dt.timedelta(minutes=25))))


def _insert_or_ignore_conflict(cache):
    offset = cache.execute(
        "SELECT MIN(line_offset) FROM quota_window_snapshots"
        " WHERE source_root_key=? AND logical_limit_key='weekly'",
        (ROOT_LIVE,)).fetchone()[0]
    _observation(cache, root=ROOT_LIVE, limit="weekly", captured=_iso(NOW),
                 resets=_iso(WEEK_RESET), percent=77.0, line_offset=offset,
                 verb="INSERT OR IGNORE")


def _rebuild(cache):
    import _cctally_cache

    _cctally_cache._clear_codex_derived_rows(cache)
    cache.commit()
    _seed(cache)


def _prune_change_ledger(cache):
    cache.execute("DELETE FROM quota_window_change_log")


SCENARIOS = {
    "base": None,
    "attribution-revision": _attribution_revision,
    "canonical-reset-update": _canonical_reset_update,
    "delete-a-tied-maximum": _delete_a_tied_maximum,
    "insert-or-ignore-conflict": _insert_or_ignore_conflict,
    "rebuild": _rebuild,
    "change-ledger-pruned": _prune_change_ledger,
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_latest_population_is_identical_on_both_read_paths(
    store, monkeypatch, scenario,
):
    import _cctally_core
    import _cctally_quota as quota

    mutate = SCENARIOS[scenario]
    if mutate is not None:
        cache = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        try:
            mutate(cache)
            cache.commit()
        finally:
            cache.close()
    summary, window = _both_paths(quota, monkeypatch)
    assert summary == window
    assert summary, "non-vacuity: the store holds latest observations"
    rooted = _both_paths(
        quota, monkeypatch, source_root_keys={ROOT_LIVE, ROOT_RETIRED})
    assert rooted[0] == rooted[1]
    assert {o.identity.source_root_key for o in rooted[0]} == {
        ROOT_LIVE, ROOT_RETIRED}


def test_two_accounts_read_identically_in_either_insertion_order(
    monkeypatch, tmp_path,
):
    observed = []
    for order in ((ACCOUNT_A, ACCOUNT_B), (ACCOUNT_B, ACCOUNT_A)):
        ns = load_script()
        redirect_paths(ns, monkeypatch, tmp_path / order[0][:1])
        import _cctally_quota as quota

        cache = ns["open_cache_db"]()
        try:
            _seed(cache, account_order=order)
        finally:
            cache.close()
        summary, window = _both_paths(quota, monkeypatch)
        assert summary == window
        observed.append({
            (o.identity, o.captured_at, o.used_percent) for o in summary})
    assert observed[0] == observed[1]


def test_reupgrade_after_an_older_binary_trim_reads_identically(
    store, monkeypatch,
):
    import _cctally_core
    import _cctally_quota as quota

    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        raw.execute("DELETE FROM schema_migrations WHERE name=?", (MIGRATION,))
        raw.execute("PRAGMA user_version=46")
        _observation(raw, root=ROOT_LIVE, limit="weekly",
                     captured=_iso(NOW - dt.timedelta(minutes=1)),
                     resets=_iso(WEEK_RESET), percent=88.0, line_offset=900)
        raw.commit()
    finally:
        raw.close()
    store["open_cache_db"]().close()
    summary, window = _both_paths(quota, monkeypatch)
    assert summary == window
    assert any(o.used_percent == 88.0 for o in summary)


def test_doctor_quota_output_is_identical_on_both_read_paths(
    store, monkeypatch,
):
    import _cctally_doctor
    import _cctally_quota as quota
    import _lib_doctor

    def gather():
        monkeypatch.setattr(_cctally_doctor, "_QUOTA_OBSERVATION_MEMO", {})
        state = _cctally_doctor.doctor_gather_state(now_utc=NOW)
        check = _lib_doctor._check_data_codex_quota(state)
        return (state.codex_quota_windows,
                (check.severity, check.summary, check.details))

    summary_path = gather()
    with monkeypatch.context() as patch:
        patch.setattr(quota, "_codex_quota_latest_summary_ready",
                      lambda _objects: False)
        window_path = gather()
    assert summary_path == window_path
    assert summary_path[0], "non-vacuity: the probe reported windows"


def _loader_statements(**kwargs):
    import _cctally_core
    import _cctally_quota as quota

    with guard.capture_sql_plans() as recorder:
        conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
        try:
            tuple(quota.load_codex_quota_observations(cache_conn=conn, **kwargs))
        finally:
            conn.close()
    return [statement for statement in recorder.statements
            if "quota_window_snapshots" in statement.sql
            and statement.call_sites[0] in LOADER_ROW_SITES]


def _has_temp(statement) -> bool:
    return any(marker in detail for detail in statement.plan
               for marker in guard.TEMP_MARKERS)


def test_the_latest_read_scans_the_summary_and_seeks_the_partition_index(store):
    statements = _loader_statements(latest_per_identity=True)
    assert len(statements) == 1, [s.sql for s in statements]
    (statement,) = statements
    assert ("FROM codex_quota_partition_latest AS s CROSS JOIN "
            "quota_window_snapshots AS q") in statement.sql
    assert statement.plan[0] == "SCAN s", statement.plan
    assert statement.plan[1].startswith(
        "SEARCH q USING INDEX idx_qws_partition_capture (source=? AND <expr>=?"
    ), statement.plan
    assert not _has_temp(statement), statement.plan


GENERAL_LOADS = {
    "all-roots": {},
    "three-roots": {"source_root_keys": {ROOT_LIVE, ROOT_ANCIENT, ROOT_RETIRED}},
    "one-root": {"source_root_keys": {ROOT_LIVE}},
    "canonical-reset-range": {
        "source_root_keys": {ROOT_LIVE, ROOT_ANCIENT, ROOT_RETIRED},
        "canonical_resets_between": (NOW - dt.timedelta(days=7),
                                     NOW + dt.timedelta(days=7)),
    },
    "captured-since-all-roots": {
        "captured_at_or_after": NOW - dt.timedelta(days=1)},
    "captured-since-one-root": {
        "source_root_keys": {ROOT_LIVE},
        "captured_at_or_after": NOW - dt.timedelta(days=1)},
}


@pytest.mark.parametrize("label", sorted(GENERAL_LOADS))
def test_general_loads_stream_through_the_load_order_index(store, label):
    statements = _loader_statements(**GENERAL_LOADS[label])
    assert len(statements) == 1, [s.sql for s in statements]
    (statement,) = statements
    assert any("USING INDEX idx_qws_codex_load_order" in detail
               for detail in statement.plan), statement.plan
    assert not _has_temp(statement), statement.plan


def test_group_shards_stream_from_the_physical_group_index(store):
    """Q11 (901-PA-001 a): the shard seeks all five group members and reads
    them in the loader's order straight off the extended index. Before it, the
    shard sorted the group's whole history ("USE TEMP B-TREE FOR LAST 4 TERMS
    OF ORDER BY"), which one window group does not bound."""
    group = (ROOT_LIVE, "weekly", "primary", 10080, _iso(WEEK_RESET))
    statements = _loader_statements(physical_groups=[group])
    assert len(statements) == 1, [s.sql for s in statements]
    (statement,) = statements
    assert statement.plan[0].startswith(
        "SEARCH quota_window_snapshots USING INDEX idx_qws_physical_group "
        "(source_root_key=? AND logical_limit_key=? AND observed_slot=? AND "
        "window_minutes=? AND <expr>=?)"), statement.plan
    assert not _has_temp(statement), statement.plan
    unexplained, _stale = guard.classify(
        statements, allowlist=guard.ALLOWLIST, pending=())
    assert unexplained == [], guard.format_findings(unexplained)


# ── Q11 G2: the extended group index changes no loaded population ───────────

#: The pre-#901-Q11 definition: five equality members, no order columns.
LEGACY_GROUP_INDEX = (
    "CREATE INDEX idx_qws_physical_group ON quota_window_snapshots("
    "source_root_key, logical_limit_key, observed_slot, window_minutes,"
    " unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc)))"
    " WHERE source='codex'")
GROUP_ORDER_COLUMNS = (
    "source_root_key", "logical_limit_key", "observed_slot", "window_minutes",
    None, "captured_at_utc", "resets_at_utc", "source_path", "line_offset")
GROUP_MIGRATION = "048_codex_quota_physical_group_order"


def _group_index_columns(conn):
    return tuple(
        row[2] for row in conn.execute(
            "PRAGMA index_xinfo(idx_qws_physical_group)") if row[5])


def _install_legacy_group_index(conn):
    conn.execute("DROP INDEX idx_qws_physical_group")
    conn.execute(LEGACY_GROUP_INDEX)
    conn.commit()


def _reset_spellings(cache):
    """One weekly group whose members spell the same reset three ways, plus a
    same-second capture tie inside it, so the group's order reaches every
    column the index now carries."""
    spellings = (_iso(WEEK_RESET), _iso(WEEK_RESET).replace("Z", "+00:00"),
                 _iso(WEEK_RESET).replace("Z", ".000000Z"))
    tie = _iso(NOW - dt.timedelta(minutes=3))
    for offset, (resets, captured) in enumerate((
            (spellings[1], tie), (spellings[2], tie),
            (spellings[0], tie.replace("Z", "+00:00")),
            (spellings[1], _iso(NOW - dt.timedelta(minutes=2))))):
        _observation(cache, root=ROOT_LIVE, limit="weekly",
                     captured=captured, resets=resets, percent=60.0 + offset,
                     line_offset=500 + offset,
                     account_key=(ACCOUNT_B, ACCOUNT_A)[offset % 2])
    _observation(cache, root=ROOT_LIVE, limit="weekly", captured=tie,
                 resets=_iso(WEEK_RESET - dt.timedelta(minutes=4)),
                 percent=61.5, line_offset=520,
                 canonical=spellings[1])  # canonical anchor joins the group
    cache.commit()


def _physical_groups(conn):
    """One entry per physical group (the reset in one spelling, so no group
    is loaded twice)."""
    return sorted(
        (str(root), str(limit), str(slot), int(minutes),
         dt.datetime.fromisoformat(str(reset)).replace(
             tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for root, limit, slot, minutes, reset in conn.execute(
            "SELECT DISTINCT source_root_key, logical_limit_key,"
            " observed_slot, window_minutes,"
            " datetime(unixepoch(COALESCE(canonical_resets_at_utc,"
            " resets_at_utc)), 'unixepoch')"
            " FROM quota_window_snapshots WHERE source='codex'"
            " AND trim(coalesce(observed_slot, '')) <> ''"))


def _group_loads(quota, conn):
    """Every group alone, then every group in one multi-shard load."""
    groups = _physical_groups(conn)
    assert len(groups) >= 6, groups
    loads = [
        tuple(quota.load_codex_quota_observations(
            cache_conn=conn, physical_groups=[group]))
        for group in groups
    ]
    loads.append(tuple(quota.load_codex_quota_observations(
        cache_conn=conn, physical_groups=groups)))
    loads.append(tuple(quota.load_codex_quota_observations(
        cache_conn=conn, source_root_keys={ROOT_LIVE},
        physical_groups=groups)))
    return loads


def _old_and_new(quota, path):
    """The loader's results and projections on the extended index, then on
    the legacy one, over the same rows."""
    import _lib_quota

    conn = sqlite3.connect(path)
    try:
        assert _group_index_columns(conn) == GROUP_ORDER_COLUMNS
        new = _group_loads(quota, conn)
        _install_legacy_group_index(conn)
        old = _group_loads(quota, conn)
    finally:
        conn.close()
    assert any(len(load) > 1 for load in new), "non-vacuity: groups with rows"
    projections = [(_lib_quota.build_blocks(n), _lib_quota.build_blocks(o))
                   for n, o in zip(new, old)]
    return new, old, projections


def _assert_identical(quota, path):
    new, old, projections = _old_and_new(quota, path)
    assert new == old
    for new_blocks, old_blocks in projections:
        assert new_blocks == old_blocks


def _fresh(ns, path):
    """A store created at head by the current binary."""


def _upgrade(ns, path):
    """A 047-head store (legacy index, no 048 marker) opened by the current
    binary: migration 048 replaces the index."""
    raw = sqlite3.connect(path)
    try:
        _install_legacy_group_index(raw)
        raw.execute("DELETE FROM schema_migrations WHERE name=?",
                    (GROUP_MIGRATION,))
        raw.execute("PRAGMA user_version=47")
        raw.commit()
    finally:
        raw.close()
    ns["open_cache_db"]().close()


def _rebuild_rows(ns, path):
    """Every Codex row cleared and re-ingested under the extended index."""
    import _cctally_cache

    cache = ns["open_cache_db"]()
    try:
        _cctally_cache._clear_codex_derived_rows(cache)
        cache.commit()
        cache.execute("DELETE FROM quota_window_snapshots")
        cache.commit()
        _seed(cache, account_order=(ACCOUNT_B, ACCOUNT_A))
        _reset_spellings(cache)
    finally:
        cache.close()


def _reupgrade(ns, path):
    """Upgrade, then an older binary trims the marker and leaves the legacy
    index while rows keep arriving; the current binary converges again."""
    _upgrade(ns, path)
    raw = sqlite3.connect(path)
    try:
        _install_legacy_group_index(raw)
        raw.execute("DELETE FROM schema_migrations WHERE name=?",
                    (GROUP_MIGRATION,))
        raw.execute("PRAGMA user_version=47")
        _observation(raw, root=ROOT_LIVE, limit="weekly",
                     captured=_iso(NOW - dt.timedelta(minutes=1)),
                     resets=_iso(WEEK_RESET).replace("Z", "+00:00"),
                     percent=88.0, line_offset=900, account_key=ACCOUNT_A)
        raw.commit()
    finally:
        raw.close()
    ns["open_cache_db"]().close()


GROUP_INDEX_DELIVERIES = {
    "fresh-install": _fresh,
    "upgrade": _upgrade,
    "rebuild": _rebuild_rows,
    "re-upgrade": _reupgrade,
}


@pytest.mark.parametrize("delivery", sorted(GROUP_INDEX_DELIVERIES))
@pytest.mark.parametrize("account_order", [(ACCOUNT_A, ACCOUNT_B),
                                           (ACCOUNT_B, ACCOUNT_A)])
def test_group_shard_loads_are_identical_on_the_legacy_and_extended_index(
    monkeypatch, tmp_path, delivery, account_order,
):
    import _cctally_core

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_quota as quota

    cache = ns["open_cache_db"]()
    try:
        _seed(cache, account_order=account_order)
        _reset_spellings(cache)
    finally:
        cache.close()
    path = _cctally_core.CACHE_DB_PATH
    GROUP_INDEX_DELIVERIES[delivery](ns, path)
    if delivery in ("upgrade", "re-upgrade"):
        conn = sqlite3.connect(path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
                (GROUP_MIGRATION,)).fetchone()[0] == 1
        finally:
            conn.close()
    _assert_identical(quota, path)


def test_the_legacy_index_is_what_sorted_each_shard(store):
    """Non-vacuity of the parity above: on the legacy index the same shard
    builds the sorter the extended index removes."""
    import _cctally_core

    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        _install_legacy_group_index(raw)
    finally:
        raw.close()
    group = (ROOT_LIVE, "weekly", "primary", 10080, _iso(WEEK_RESET))
    statements = _loader_statements(physical_groups=[group])
    assert len(statements) == 1
    assert "USE TEMP B-TREE FOR LAST 4 TERMS OF ORDER BY" in statements[0].plan


def test_the_bounded_recent_read_keeps_only_its_limit_sorter(store):
    statements = _loader_statements(
        source_root_keys={ROOT_LIVE},
        captured_at_or_after=NOW - dt.timedelta(days=1),
        active_at=NOW, max_rows=5)
    assert len(statements) == 1, [s.sql for s in statements]
    assert statements[0].sql.endswith("LIMIT ?")
    unexplained, _stale = guard.classify(
        statements, allowlist=guard.ALLOWLIST, pending=())
    assert unexplained == [], guard.format_findings(unexplained)


def test_a_store_without_the_summary_falls_back_to_the_window_query(store):
    import _cctally_core
    import _cctally_quota as quota

    before = quota.load_codex_quota_observations(latest_per_identity=True)
    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        raw.execute("DROP TRIGGER trg_qws_latest_upd")
        raw.commit()
    finally:
        raw.close()
    statements = _loader_statements(latest_per_identity=True)
    assert any("OVER (PARTITION BY" in s.sql for s in statements), (
        "non-vacuity: with a trigger missing the summary must not be trusted")
    assert quota.load_codex_quota_observations(
        latest_per_identity=True) == before
