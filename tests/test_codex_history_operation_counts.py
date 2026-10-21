"""Deterministic actual-path operation counts for #968 historical recovery."""
from __future__ import annotations

import collections
import json
import sys

import pytest

from test_codex_publication_barrier import (
    env, _cache, _compress, _heal_oracle, _name, _sync, _unfixed_prune,
    _with_quota, conversation_records, write_rollout,
)


@pytest.mark.parametrize("history_files", [2, 6])
def test_history_quota_membership_is_loaded_once_per_identity(
        env, monkeypatch, history_files):
    """Recovery preserves every row; the following walk range-loads its keys.

    Count actual SQLite statements in the real admitted drain and cache walk,
    rather than elapsed time. All stores and rollouts are remote tmp fixtures.
    """
    ns, _provider, day, _ = env
    cc = _cache()
    paths = [day / _name(10, n + 1) for n in range(history_files)]
    for n, path in enumerate(paths):
        write_rollout(path, _with_quota(conversation_records(
            8, session_id=f"count-{n}", label=f"count-{n}")))
    keeper = day / _name(20, 99)
    write_rollout(keeper, conversation_records(1, session_id="keeper"))
    _sync(ns)
    expected = _heal_oracle(ns)
    for path in paths:
        _compress(path)
    _unfixed_prune(ns, monkeypatch)

    phase = ["provenance"]
    counts = collections.defaultdict(collections.Counter)
    real_open = cc.open_cache_db

    def trace(sql):
        normalized = " ".join(sql.split()).lower()
        counter = counts[phase[0]]
        if normalized.startswith("select source_path, line_offset, id, resets_at_utc"):
            counter["anchor_seed_reads"] += 1
        if normalized.startswith("select account_key from quota_window_snapshots"):
            counter["quota_natural_key_reads"] += 1
        if ("from quota_window_snapshots" in normalized
                and "source_path=" in normalized.replace(" ", "")
                and normalized.startswith("select")
                and "logical_limit_key" in normalized):
            if normalized.startswith("select 1 "):
                counter["quota_membership_reads"] += 1
            elif normalized.startswith("select line_offset, logical_limit_key"):
                counter["quota_membership_reads"] += 1
        if normalized == "commit":
            counter["commits"] += 1

    def opened():
        conn = real_open()
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(cc, "open_cache_db", opened)
    monkeypatch.setitem(ns, "open_cache_db", opened)
    journal = sys.modules["_cctally_journal"]
    real_iter = journal.iter_range
    def journal_range(*args, **kwargs):
        counts[phase[0]]["journal_traversals"] += 1
        return real_iter(*args, **kwargs)
    monkeypatch.setattr(journal, "iter_range", journal_range)
    real_anchor_factory = journal._codex_anchor_resolver

    class SeedCursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchall(self):
            rows = self.cursor.fetchall()
            counts[phase[0]]["anchor_seed_rows"] += len(rows)
            return rows

    class SeedConnection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, *args):
            cursor = self.conn.execute(sql, *args)
            if sql.startswith("SELECT source_path, line_offset, id, resets_at_utc"):
                return SeedCursor(cursor)
            return cursor

    def anchor_factory(conn):
        resolver = real_anchor_factory(conn)
        if resolver is not None:
            resolver._conn = SeedConnection(conn)
        return resolver

    monkeypatch.setattr(journal, "_codex_anchor_resolver", anchor_factory)
    real_resolve_anchor = journal._resolve_obs_anchor
    def resolve_anchor(*args, **kwargs):
        counts[phase[0]]["quota_anchor_resolutions"] += 1
        return real_resolve_anchor(*args, **kwargs)
    monkeypatch.setattr(journal, "_resolve_obs_anchor", resolve_anchor)
    real_quota_apply = journal._apply_quota_records
    def apply_quota(conn, records, **kwargs):
        counter = counts[phase[0]]
        counter["quota_input_records"] += len(records)
        before = conn.total_changes
        log_before = conn.execute("SELECT COUNT(*) FROM quota_window_change_log").fetchone()[0]
        rows_before = conn.execute("SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0]
        result = real_quota_apply(conn, records, **kwargs)
        counter["quota_apply_total_changes"] += conn.total_changes - before
        counter["quota_change_log_rows"] += conn.execute(
            "SELECT COUNT(*) FROM quota_window_change_log").fetchone()[0] - log_before
        counter["quota_rows_restored"] += conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] - rows_before
        return result
    monkeypatch.setattr(journal, "_apply_quota_records", apply_quota)
    for name, key in (("_invalidate_codex_journal_coverage_certificate", "coverage_invalidations"),
                      ("_bump_codex_physical_mutation_seq", "mutation_bumps")):
        real = getattr(cc, name)
        def counted(*args, _real=real, _key=key, **kwargs):
            counts[phase[0]][_key] += 1
            return _real(*args, **kwargs)
        monkeypatch.setattr(cc, name, counted)

    result = cc.run_codex_history_provenance_drain(wait_for_requests=False)
    assert result.outcome == "settled" and result.candidates == history_files
    phase[0] = "cache-walk"
    _sync(ns)
    phase[0] = "assertions"
    assert _heal_oracle(ns) == expected
    provenance = counts["provenance"]
    assert provenance["journal_traversals"] == 1
    assert provenance["commits"] == history_files + 1
    assert provenance["coverage_invalidations"] == history_files
    assert provenance["mutation_bumps"] == history_files
    assert provenance["quota_rows_restored"] == history_files * 16
    assert provenance["quota_change_log_rows"] == history_files * 16
    print("OPERATION_COUNTS " + json.dumps({
        "historyFiles": history_files, "observationsPerFile": 16,
        "phases": {key: dict(value) for key, value in counts.items()},
    }, sort_keys=True))
    assert counts["cache-walk"]["quota_membership_reads"] == history_files, (
        "a historical identity must load retained quota membership once, "
        "rather than issue one natural-key seek for every recovered row", counts)
