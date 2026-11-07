"""Deterministic actual-path operation counts for #968 historical recovery."""
from __future__ import annotations

import collections
import json
import sqlite3
import sys

import pytest

from test_codex_publication_barrier import (
    env, _cache, _compress, _heal_oracle, _name, _sync, _unfixed_prune,
    _with_quota, conversation_records, write_rollout,
)


def _spy_summary_seeds(patch, record, conns=None) -> None:
    """#1033: call ``record(served)`` for every reset-anchor summary seed,
    ``served`` False when it fell back to the full history read. A group
    seeds through a served summary or through the full read, never both.
    With ``conns``, only seeds on those connections (or a wrapper of one)
    are recorded, as a trace on them would see them."""
    cc = _cache()
    real = cc._codex_reset_anchor_summary_seed

    def spy(conn, *args, **kwargs):
        result = real(conn, *args, **kwargs)
        inner = getattr(conn, "conn", conn)
        if conns is None or any(inner is c for c in conns):
            record(result is not None)
        return result

    patch.setattr(cc, "_codex_reset_anchor_summary_seed", spy)


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
    traced: list = []

    def trace(sql):
        normalized = " ".join(sql.split()).lower()
        counter = counts[phase[0]]
        if normalized.startswith("select source_path, line_offset, id, resets_at_utc"):
            counter["anchor_seed_reads"] += 1
        if normalized.startswith(
                "select observed_slot, logical_limit_key, window_minutes, "
                "source_root_key, source_path, line_offset, id, resets_at_utc"):
            counter["anchor_base_refresh_reads"] += 1
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
        if (normalized.startswith("select timestamp_utc")
                and "from codex_session_entries" in normalized):
            counter["accounting_membership_reads"] += 1
        if normalized == "commit":
            counter["commits"] += 1

    def opened():
        conn = real_open()
        conn.set_trace_callback(trace)
        traced.append(conn)
        return conn

    monkeypatch.setattr(cc, "open_cache_db", opened)
    monkeypatch.setitem(ns, "open_cache_db", opened)
    _spy_summary_seeds(monkeypatch, lambda served: served and counts[
        phase[0]].update(["anchor_summary_seeds"]), traced)
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
    # #970 N1: the retained accounting entries of a cursorless identity read
    # from byte zero are loaded once per identity, not per emitted entry.
    assert counts["cache-walk"]["accounting_membership_reads"] == history_files, counts
    # #975 RC2 (spec 4.2): provenance seeds each anchor group once per pass
    # from the committed rows, and each identity then refreshes that base with
    # only its own stored rows -- so the history-sized seed read no longer
    # grows with the number of identities.
    conn = ns["open_cache_db"]()
    try:
        groups = {cc.CodexResetAnchorResolver.group_key(*row) for row in conn.execute(
            "SELECT DISTINCT source_root_key, observed_slot, logical_limit_key, "
            "window_minutes FROM quota_window_snapshots WHERE source='codex'")}
    finally:
        conn.close()
    # #1033: a group seeds from the evidence summary when it is ready and
    # holds no stale bucket, else from the full read; once per group either way.
    assert (provenance["anchor_seed_reads"]
            + provenance["anchor_summary_seeds"]) == len(groups), counts
    assert provenance["anchor_base_refresh_reads"] == history_files, counts


# --- #975 RC2 / spec 4.2: the provenance committed-evidence seed -------------
#
# The controls below drive the production per-identity provenance write
# (`_codex_provenance_identity_write`) over synthetic journal-shaped quota
# records, and compare each identity's stored anchor with the oracle of
# today's semantics: a NEW resolver per `_apply_quota_records` call
# (`anchors=None`) over the same committed rows. Group G holds two committed
# components, R1 (at the smallest order key) and R3, twenty minutes apart; a
# raw reset R2 halfway between them bridges both within the 600 s tolerance.
# Identity B observes a reset just after R3, so its anchor is R3 unless R2
# leaked into the evidence B resolves against, which would make it R1.

_SEED_ROOT = "root-975"
_SEED_LIMIT = "limit-975"
_R1 = "2026-07-27T00:00:00Z"
_R2 = "2026-07-27T00:10:00Z"
_R3 = "2026-07-27T00:20:00Z"
_RB = "2026-07-27T00:21:00Z"
_P1, _PA, _P2, _P3, _PB = (f"/h/{name}.jsonl" for name in (
    "a-001", "b-002", "b-003", "c-004", "d-005"))


def _qrec(path, offset, raw):
    return {"t": "obs", "provider": "codex", "account": None, "payload": {
        "source": "codex", "source_root_key": _SEED_ROOT,
        "source_path": path, "line_offset": offset,
        "captured_at_utc": "2026-07-20T10:00:00Z", "observed_slot": "secondary",
        "logical_limit_key": _SEED_LIMIT, "limit_id": None, "limit_name": None,
        "window_minutes": 10080, "used_percent": 10.0, "resets_at_utc": raw,
        "plan_type": None, "individual_limit_json": None, "reached_type": None,
        "observed_model": None}}


def _store_rows(ns, *rows):
    """Commit quota rows directly, as another writer would."""
    journal = sys.modules["_cctally_journal"]
    conn = ns["open_cache_db"]()
    try:
        for path, offset, raw in rows:
            conn.execute(journal._QUOTA_SNAPSHOT_INSERT, journal._quota_snapshot_values(
                _qrec(path, offset, raw), raw))
        conn.commit()
    finally:
        conn.close()


def _anchor(ns, path):
    conn = ns["open_cache_db"]()
    try:
        return [r[0] for r in conn.execute(
            "SELECT canonical_resets_at_utc FROM quota_window_snapshots "
            "WHERE source='codex' AND source_path=? ORDER BY line_offset", (path,))]
    finally:
        conn.close()


def _oracle_anchor(ns, records, path):
    """B's anchor under today's semantics, on the committed store, not kept."""
    journal = sys.modules["_cctally_journal"]
    conn = ns["open_cache_db"]()
    try:
        journal._apply_quota_records(conn, records, quiet=True)
        return [r[0] for r in conn.execute(
            "SELECT canonical_resets_at_utc FROM quota_window_snapshots "
            "WHERE source='codex' AND source_path=? ORDER BY line_offset", (path,))]
    finally:
        conn.rollback()
        conn.close()


def _identity(share, path, records, *, fail=False):
    cc, journal = _cache(), sys.modules["_cctally_journal"]

    def apply(conn, anchors):
        journal._apply_quota_records(conn, records, quiet=True, anchors=anchors)
        if fail:
            raise RuntimeError("injected failure after resolving")
        return True

    return cc._codex_provenance_identity_write(
        share, path, apply, wait_for_requests=False)


def _seed_reads(monkeypatch):
    cc = _cache()
    reads = collections.Counter()
    real_open = cc.open_cache_db
    traced: list = []

    def trace(sql):
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith(
                "select source_path, line_offset, id, resets_at_utc"):
            reads["seed"] += 1

    def opened():
        conn = real_open()
        conn.set_trace_callback(trace)
        traced.append(conn)
        return conn

    monkeypatch.setattr(cc, "open_cache_db", opened)
    _spy_summary_seeds(
        monkeypatch, lambda served: served and reads.update(["seed"]), traced)
    return reads


def test_provenance_seed_isolates_declined_evidence(env):
    """A's observation is declined by `INSERT OR IGNORE` (its natural key is
    already stored with R1), yet resolving it unions R1 and R3 in A's
    resolver. That union must stay private to A: B's stored anchor equals the
    new-resolver-per-call oracle (R3), not R1."""
    ns = env[0]
    _store_rows(ns, (_P1, 0, _R1), (_P3, 0, _R3), (_PA, 0, _R1))
    share = _cache()._CodexWalkShare()
    assert _identity(share, _PA, [_qrec(_PA, 0, _R2)]) == (True, True)
    assert _anchor(ns, _PA) == [_R1]          # declined: the stored row stands
    b_records = [_qrec(_PB, 0, _RB)]
    oracle = _oracle_anchor(ns, b_records, _PB)
    assert oracle == [_R3]
    assert _identity(share, _PB, b_records) == (True, True)
    assert _anchor(ns, _PB) == oracle


def test_provenance_seed_reseeds_after_foreign_mutation(env, monkeypatch):
    """Another connection commits the bridging R2 between identities. The
    share's token changes, B reseeds the touched group from the committed
    rows (one seed read), and B's anchor equals the oracle (R1)."""
    ns = env[0]
    _store_rows(ns, (_P1, 0, _R1), (_P3, 0, _R3))
    reads = _seed_reads(monkeypatch)
    share = _cache()._CodexWalkShare()
    assert _identity(share, _PA, [_qrec(_PA, 0, _R3)]) == (True, True)
    assert reads["seed"] == 1
    _store_rows(ns, (_P2, 0, _R2))
    b_records = [_qrec(_PB, 0, _RB)]
    oracle = _oracle_anchor(ns, b_records, _PB)
    assert oracle == [_R1]
    before = reads["seed"]
    assert _identity(share, _PB, b_records) == (True, True)
    assert reads["seed"] - before == 1, reads
    assert _anchor(ns, _PB) == oracle


def test_provenance_seed_rollback_leaves_base(env):
    """A resolves the bridging R2 and then raises: its rows roll back and the
    base gains nothing, so B's anchor equals the oracle (R3)."""
    ns = env[0]
    _store_rows(ns, (_P1, 0, _R1), (_P3, 0, _R3))
    share = _cache()._CodexWalkShare()
    assert _identity(share, _P2, [_qrec(_P2, 5, _R3)]) == (True, True)
    with pytest.raises(RuntimeError, match="injected failure"):
        _identity(share, _PA, [_qrec(_PA, 0, _R2)], fail=True)
    assert _anchor(ns, _PA) == []
    b_records = [_qrec(_PB, 0, _RB)]
    oracle = _oracle_anchor(ns, b_records, _PB)
    assert oracle == [_R3]
    assert _identity(share, _PB, b_records) == (True, True)
    assert _anchor(ns, _PB) == oracle


def test_walk_share_token_guards_reuse(env):
    """The share reuses state only while its token is unchanged: the walk
    resolver survives a sealed unit, a foreign quota insert drops it, a
    missing ledger trigger disables reuse, and the accounting ledger path set
    is read in full once and then only past the last seen `seq`."""
    ns = env[0]
    cc = _cache()
    share = cc._CodexWalkShare()
    conn = ns["open_cache_db"]()
    try:
        statements = []
        conn.set_trace_callback(
            lambda sql: statements.append(" ".join(sql.split()).lower()))
        assert share.token(conn) is not None
        walk = share.bind_walk(conn)
        share.seal(conn)
        assert share.bind_walk(conn) is walk
        share.seal(conn)
        _store_rows(ns, (_P1, 0, _R1))
        assert share.bind_walk(conn) is not walk
        walk = share.bind_walk(conn)
        share.invalidate()
        assert share.bind_walk(conn) is not walk

        conn.execute(
            "INSERT INTO codex_accounting_change_log (mutation_seq, change_kind, "
            "source_root_key, source_path) VALUES (1, 'path', 'r', '/p/one')")
        conn.commit()
        assert share.ledger_paths(conn) == {"/p/one"}
        conn.execute(
            "INSERT INTO codex_accounting_change_log (mutation_seq, change_kind, "
            "source_root_key, source_path) VALUES (2, 'path', 'r', '/p/two')")
        conn.commit()
        assert share.ledger_paths(conn) == {"/p/one", "/p/two"}
        incremental = [s for s in statements if s.startswith(
            "select seq, source_path from codex_accounting_change_log")]
        assert len(incremental) == 2 and "seq >" in incremental[1], incremental

        walk = share.bind_walk(conn)
        share.seal(conn)
        conn.execute("DROP TRIGGER trg_qws_ledger_upd")
        conn.commit()
        assert share.token(conn) is None
        assert share.bind_walk(conn) is not walk
        share.seal(conn)
        walk = share.bind_walk(conn)
        assert share.bind_walk(conn) is not walk
    finally:
        conn.close()


def test_reset_anchor_components_copy_is_independent():
    import datetime as dt
    import _lib_quota

    def at(minutes):
        return dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc) + dt.timedelta(
            minutes=minutes)

    base = _lib_quota.ResetAnchorComponents()
    base.add(at(0), order_key=("/a", 0, 1))
    base.add(at(20), order_key=("/c", 0, 3))
    copy = base.copy()
    assert list(copy) == list(base) and len(copy) == len(base)
    assert copy.canonical(at(20)) == base.canonical(at(20)) == at(20)
    # A bridge in the copy unions its components there only.
    assert copy.add(at(10), order_key=("/b", 0, 2)) == (at(0), (at(10), at(20)))
    assert copy.canonical(at(20)) == at(0)
    assert base.canonical(at(20)) == at(20) and at(10) not in base
    # An addition to the original never appears in the copy, and both keep
    # their own insertion order for order-key-less additions.
    base.add(at(40))
    assert at(40) in base and at(40) not in copy
    assert copy.add(at(60)) == (at(60), ())
    assert list(base) == [at(0), at(20), at(40)]
    assert list(copy) == [at(0), at(20), at(10), at(60)]


# --- #982 R6 / spec 4.2: the heal executor shares history-sized reads -------


def _count_heal_payload_parses(monkeypatch, counts) -> None:
    """Count every JSON parse of a heal record's history-sized payload (a
    heal record's text naming its population; the container status's own
    `population` field is not one): the fence checks must not parse it, and
    a pass parses it once (spec 4.5a)."""
    real_loads = json.loads

    def loads(text, *args, **kwargs):
        if (isinstance(text, str) and '"population":[' in text.replace(" ", "")
                and ('"gen"' in text or '"mode"' in text)):
            counts["heal_payload_parses"] += 1
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)


@pytest.mark.parametrize("files", [2, 5])
def test_replay_heal_parses_payload_once_per_pass(env, monkeypatch, files):
    """A replay heal refills every file with the persisted D1 set without
    re-reading it per unit (review F2). Detects: the history-sized record
    (population and D1 set) parsed, and the D1 set copied, once per unit and
    several times per unit by the fence checks."""
    from test_codex_publication_barrier import _seed_replay
    ns, _provider, day, _ = env
    cc = _cache()
    _seed_replay(ns, day, count=files)
    counts = collections.Counter()
    _count_heal_payload_parses(monkeypatch, counts)
    result = cc.run_codex_admitted_heal(wait_for_requests=False)
    assert (result.mode, result.outcome) == ("replay", "done"), result
    assert result.units >= files
    assert counts["heal_payload_parses"] <= 1, counts

_LEDGER_FULL_READ = ("select seq, source_path from codex_accounting_change_log "
                     "where change_kind='path'")


@pytest.mark.parametrize("history_files,foreign", [(2, False), (6, False),
                                                   (6, True)])
def test_heal_executor_seeds_once_per_group(env, monkeypatch, history_files,
                                            foreign):
    """Per-file units must not multiply history-sized reads by the file count
    (RC3). Detects: a walk unit that re-seeds every anchor group, or re-reads
    the whole accounting ledger, once per population file; and a memo reused
    across another writer's quota mutation (exactly one reseed follows it)."""
    import _lib_codex_drain_admission as adm
    ns, _provider, day, _ = env
    cc = _cache()
    write_rollout(day / _name(20, 99), conversation_records(1, session_id="keeper"))
    _sync(ns)
    # Untraced compressed history carrying quota: nothing retained covers its
    # observations, so every unit resolves anchors (a traced file's retained
    # quota keys are skipped before resolution and would seed nothing), and
    # its classification reads the accounting ledger.
    paths = [day / _name(10, n + 1) for n in range(history_files)]
    for n, path in enumerate(paths):
        write_rollout(path, _with_quota(conversation_records(
            4, session_id=f"seed-{n}", label=f"seed-{n}")))
        _compress(path)
    for n in range(2):
        untraced = day / _name(11, 60 + n)
        write_rollout(untraced, conversation_records(
            1, session_id=f"untraced-{n}", label=f"u{n}"))
        _compress(untraced)
    assert cc.run_codex_history_provenance_drain(
        wait_for_requests=False).outcome in ("settled", "already-settled")
    population = cc._codex_history_candidates()
    assert len(population) == history_files + 2
    counts = collections.Counter()
    real_open = cc.open_cache_db
    traced: list = []

    def trace(sql):
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith(
                "select source_path, line_offset, id, resets_at_utc"):
            counts["anchor_seed_reads"] += 1
        if normalized == _LEDGER_FULL_READ:
            counts["ledger_full_reads"] += 1
        elif normalized.startswith(_LEDGER_FULL_READ + " and seq >"):
            counts["ledger_delta_reads"] += 1

    def opened():
        conn = real_open()
        conn.set_trace_callback(trace)
        traced.append(conn)
        return conn

    monkeypatch.setattr(cc, "open_cache_db", opened)
    monkeypatch.setitem(ns, "open_cache_db", opened)
    _spy_summary_seeds(monkeypatch, lambda served: served and counts.update(
        ["anchor_seed_reads"]), traced)
    foreign_done: list = []

    def committed(kind, *args):
        if (kind == "commit" and foreign and not foreign_done
                and str(paths[0]) in args[0]):
            foreign_done.append(True)

    def after_finish():
        if foreign_done == [True]:
            foreign_done.append("inserted")
            conn = sqlite3.connect(str(ns["_cctally_core"].CACHE_DB_PATH))
            try:
                columns = [r[1] for r in conn.execute(
                    "PRAGMA table_info(quota_window_snapshots)") if r[1] != "id"]
                select = ", ".join("'/foreign/rollout.jsonl'" if c == "source_path"
                                   else c for c in columns)
                conn.execute(
                    f"INSERT INTO quota_window_snapshots ({', '.join(columns)}) "
                    f"SELECT {select} FROM quota_window_snapshots "
                    "WHERE source='codex' AND source_path=? ORDER BY id",
                    (str(paths[0]),))
                conn.commit()
            finally:
                conn.close()

    monkeypatch.setattr(cc, "_CODEX_W11_TRACE", committed)
    monkeypatch.setattr(adm, "_after_finish", after_finish)
    _count_heal_payload_parses(monkeypatch, counts)
    result = cc.run_codex_admitted_heal(wait_for_requests=False,
                                        population=population)
    assert result.outcome == "done", result
    assert result.units >= len(population)
    conn = real_open()
    try:
        groups = {cc.CodexResetAnchorResolver.group_key(*row) for row in conn.execute(
            "SELECT DISTINCT source_root_key, observed_slot, logical_limit_key, "
            "window_minutes FROM quota_window_snapshots WHERE source='codex' "
            "AND source_path != '/foreign/rollout.jsonl'")}
    finally:
        conn.close()
    assert groups
    print("HEAL_OPERATION_COUNTS " + json.dumps({
        "historyFiles": history_files, "foreign": foreign,
        "groups": len(groups), "counts": dict(counts)}, sort_keys=True))
    assert counts["anchor_seed_reads"] == len(groups) * (2 if foreign else 1), counts
    assert counts["ledger_full_reads"] == 1, counts
    # The fence checks read the small key; the population payload is parsed
    # at most once per pass, never once per unit (review F2).
    assert counts["heal_payload_parses"] <= 1, counts
    if foreign:
        assert foreign_done == [True, "inserted"]


# --- #1033 spec §5.1/Q4: the evidence delete trigger is keyed, never a search
#
# Each deleted Codex quota row runs three primary-key statements on the
# evidence summary. A trigger that searched quota_window_snapshots for a
# replacement minimum would make every prune or heal DELETE history-sized (the
# #971 class). The deleted file holds a fixed number of rows in one big bucket;
# only the bucket's unrelated history grows.

_EVIDENCE_FILE = "/roots/r/sessions/0-victim.jsonl"


def _evidence_delete_steps(tmp_path, *, history, victim_holds_minimum, heal):
    import _cctally_db
    conn = sqlite3.connect(tmp_path / f"evidence-{history}-"
                           f"{victim_holds_minimum}-{heal}.db")
    try:
        _cctally_db._apply_cache_schema(conn)
        columns = ("source", "source_root_key", "source_path", "line_offset",
                   "captured_at_utc", "observed_slot", "logical_limit_key",
                   "window_minutes", "used_percent", "resets_at_utc")
        insert = (f"INSERT OR IGNORE INTO quota_window_snapshots"
                  f" ({', '.join(columns)})"
                  f" VALUES ({', '.join('?' for _ in columns)})")

        def row(path, offset):
            return ("codex", "root-1033", path, offset,
                    f"2026-07-20T{offset // 3600 % 24:02d}:"
                    f"{offset // 60 % 60:02d}:{offset % 60:02d}Z",
                    "primary", "limit-1033", 10080, 10.0,
                    "2026-07-25T00:00:00Z")

        victim = (_EVIDENCE_FILE if victim_holds_minimum
                  else "/roots/r/sessions/z-victim.jsonl")
        history_rows = [row(f"/roots/r/sessions/h-{i % 7}.jsonl", i)
                        for i in range(history)]
        victim_rows = [row(victim, 100_000 + i) for i in range(3)]
        conn.executemany(insert, history_rows + victim_rows)
        conn.commit()
        before = conn.execute(
            "SELECT row_count, stale FROM codex_reset_anchor_evidence"
            " WHERE logical_limit_key='limit-1033'").fetchone()
        counter = [0]

        def tick():
            counter[0] += 1
            return 0

        conn.set_progress_handler(tick, 1)
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source = 'codex' "
            "AND source_path = ?", (victim,))
        if heal:
            conn.executemany(insert, victim_rows)
        conn.set_progress_handler(None, 0)
        conn.commit()
        after = conn.execute(
            "SELECT row_count, stale FROM codex_reset_anchor_evidence"
            " WHERE logical_limit_key='limit-1033'").fetchone()
        assert before == (history + 3, 0)
        expected_stale = int(victim_holds_minimum and not heal)
        assert after == (history + (3 if heal else 0), expected_stale), (
            "the delete trigger must maintain the summary", before, after)
        return counter[0]
    finally:
        conn.close()


@pytest.mark.parametrize("heal", [False, True], ids=["prune", "heal"])
@pytest.mark.parametrize("victim_holds_minimum", [True, False],
                         ids=["min-row", "non-min-row"])
def test_1033_evidence_delete_trigger_work_is_bounded_by_history(
        tmp_path, victim_holds_minimum, heal):
    """A history-sized evidence trigger (#971 class): the prune DELETE of one
    file, and a heal's delete-and-reinsert of it, take VM steps independent
    of 20 versus 2,000 unrelated rows in the same bucket, whether or not the
    file held the bucket's minimum."""
    small = _evidence_delete_steps(
        tmp_path, history=20, victim_holds_minimum=victim_holds_minimum,
        heal=heal)
    big = _evidence_delete_steps(
        tmp_path, history=2000, victim_holds_minimum=victim_holds_minimum,
        heal=heal)
    assert big <= small + 256, (small, big)


# --- #1033 spec §5.2: the summary seed replaces the full history read ---------

_SEED_KEY = "limit-1033-seed"


def _summary_seed_store(tmp_path, name="seed"):
    import _cctally_db
    conn = sqlite3.connect(tmp_path / f"{name}.db")
    _cctally_db._apply_cache_schema(conn)
    rows = [("/s/b.jsonl", 1, "2026-07-27T00:00:00Z"),
            ("/s/a.jsonl", 2, "2026-07-27T00:09:00Z"),
            ("/s/c.jsonl", 3, "2026-07-27T00:18:00+00:00"),
            ("/s/c.jsonl", 4, "2026-07-27T00:18:00Z"),
            ("/s/d.jsonl", 5, "not-a-reset")]
    conn.executemany(
        "INSERT INTO quota_window_snapshots (source, source_root_key, "
        " source_path, line_offset, captured_at_utc, observed_slot, "
        " logical_limit_key, window_minutes, used_percent, resets_at_utc) "
        "VALUES ('codex', 'root-seed', ?, ?, '2026-07-20T10:00:00Z', "
        " 'primary', ?, 10080, 10.0, ?)",
        [(path, offset, _SEED_KEY, raw) for path, offset, raw in rows])
    conn.commit()
    return conn


def _traced_resolve(conn, reset="2026-07-27T00:27:00Z"):
    """Resolve one probe: its anchor, the statements on quota history, and
    whether each summary seed served the group (False: it fell back)."""
    statements: list[str] = []
    summary: list[bool] = []
    conn.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
    try:
        with pytest.MonkeyPatch.context() as patch:
            _spy_summary_seeds(patch, summary.append)
            resolver = _cache().CodexResetAnchorResolver(conn)
            anchor = resolver.resolve(
                source_root_key="root-seed", observed_slot="primary",
                logical_limit_key=_SEED_KEY, window_minutes=10080,
                resets_at_utc=reset, source_path="/s/z.jsonl", line_offset=9)
    finally:
        conn.set_trace_callback(None)
    quota = [s for s in statements if "FROM quota_window_snapshots" in s]
    return anchor, quota, summary, resolver


def test_1033_a_ready_seed_reads_no_quota_history(tmp_path):
    """The full read silently returning: a ready summary seeds the group with
    one summary statement and no statement on quota_window_snapshots, and it
    resolves the anchor the full read resolves."""
    conn = _summary_seed_store(tmp_path)
    try:
        anchor, quota, summary, _resolver = _traced_resolve(conn)
        assert quota == [] and summary == [True], (quota, summary)
        conn.execute("UPDATE cache_meta SET value='bootstrap' "
                     "WHERE key='codex_reset_anchor_evidence'")
        fallback, quota, summary, _resolver = _traced_resolve(conn)
        assert len(quota) == 1 and quota[0].startswith(
            "SELECT source_path, line_offset, id, resets_at_utc"), quota
        assert anchor == fallback == "2026-07-27T00:09:00Z"
    finally:
        conn.close()


@pytest.mark.parametrize("damage", ["stale-bucket", "missing-trigger",
                                    "dropped-table", "zero-count",
                                    "non-integer-offset"])
def test_1033_an_unusable_summary_falls_back_to_the_full_read(
        tmp_path, damage):
    """A stale bucket, a missing trigger, a database error or a corrupt row
    must never seed from the summary; the fallback is today's full read, and
    neither a database error from the summary statement nor a corrupt row
    (a ``ValueError`` while folding it) sets ``_seed_failed`` or escapes the
    resolver."""
    conn = _summary_seed_store(tmp_path, damage)
    try:
        expected = {
            "stale-bucket": "2026-07-27T00:18:00Z",
            "missing-trigger": "2026-07-27T00:09:00Z",
            "dropped-table": "2026-07-27T00:09:00Z",
            "zero-count": "2026-07-27T00:09:00Z",
            "non-integer-offset": "2026-07-27T00:09:00Z",
        }[damage]
        if damage == "stale-bucket":
            conn.execute("DELETE FROM quota_window_snapshots "
                         "WHERE source_path='/s/a.jsonl'")
            assert conn.execute(
                "SELECT COUNT(*) FROM codex_reset_anchor_evidence "
                "WHERE stale=1").fetchone()[0] == 0, (
                "precondition: a single-row bucket is removed, not staled")
            conn.execute("DELETE FROM quota_window_snapshots "
                         "WHERE source_path='/s/c.jsonl' AND line_offset=3")
            conn.execute("INSERT INTO quota_window_snapshots (source, "
                         " source_root_key, source_path, line_offset, "
                         " captured_at_utc, observed_slot, logical_limit_key, "
                         " window_minutes, used_percent, resets_at_utc) "
                         "VALUES ('codex', 'root-seed', '/s/e.jsonl', 6, "
                         " '2026-07-20T10:00:00Z', 'primary', ?, 10080, 10.0, "
                         " '2026-07-27T00:00:00Z')", (_SEED_KEY,))
            conn.execute("DELETE FROM quota_window_snapshots "
                         "WHERE source_path='/s/b.jsonl'")
            assert conn.execute(
                "SELECT COUNT(*) FROM codex_reset_anchor_evidence "
                "WHERE stale=1").fetchone()[0] == 1
        elif damage == "missing-trigger":
            conn.execute("DROP TRIGGER trg_qws_anchor_evidence_upd")
        elif damage == "dropped-table":
            conn.execute("DROP TABLE codex_reset_anchor_evidence")
        else:
            # Bypass the table's CHECKs, as a damaged file could.
            conn.commit()
            conn.execute("PRAGMA ignore_check_constraints = ON")
            column, value = (("row_count", 0) if damage == "zero-count"
                             else ("min_line_offset", "x"))
            assert conn.execute(
                f"UPDATE codex_reset_anchor_evidence SET {column} = ? "
                "WHERE min_source_path = '/s/a.jsonl'", (value,)
            ).rowcount == 1
            conn.commit()
            conn.execute("PRAGMA ignore_check_constraints = OFF")
        conn.commit()
        anchor, quota, summary, resolver = _traced_resolve(conn)
        assert len(quota) == 1 and quota[0].startswith(
            "SELECT source_path, line_offset, id, resets_at_utc"), quota
        assert summary == [False], summary
        assert resolver._seed_failed is False
        assert anchor == expected
    finally:
        conn.close()
