"""#997 T-A1/T-A2 — the full replays' disposable scratch policy, batched
commits and per-record preparation (spec D1, D2, D5, D6).

A full replay (derivation completion's `all`/`without`/`with` copies, its
predecessor `base`, and `db rederive`'s `stats.rederive.db`) writes a scratch
index nothing ever publishes or trusts. Before #997 it ran under the stats
store policy (`journal_mode=DELETE`, `synchronous=FULL`) and committed once per
input, so every input created, wrote, fsynced and unlinked a rollback journal:
the burst of #997. These tests pin the policy to exactly those scratches, the
commit boundary to one per `_SCRATCH_REPLAY_COMMIT_EVERY` modifying inputs,
and the replay's output to what the per-input commit produced.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
#: The first subscription week's reset; the fixture crosses it.
W1 = int(dt.datetime(2026, 7, 27, tzinfo=UTC).timestamp())
W2 = W1 + 7 * 86400
_REPLAY_INDEXES = {
    "replay_usage_latest_weekly",
    "replay_usage_latest_five_hour",
    "replay_usage_week_start",
}


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _policy_of(conn) -> dict:
    """What a connection reports: journal mode, synchronous level, and the
    replay indexes present on its database."""
    return {
        "journal_mode": str(sqlite3.Connection.execute(
            conn, "PRAGMA journal_mode").fetchone()[0]).lower(),
        "synchronous": int(sqlite3.Connection.execute(
            conn, "PRAGMA synchronous").fetchone()[0]),
        "indexes": {str(row[0]) for row in sqlite3.Connection.execute(
            conn, "SELECT name FROM sqlite_schema WHERE type = 'index' "
            "AND name LIKE 'replay_usage_%'")},
    }


def _db_path(conn) -> str:
    return str(sqlite3.Connection.execute(
        conn, "PRAGMA database_list").fetchone()[2])


# --------------------------------------------------------------------------
# Fixture: Claude readings crossing a weekly reset, a five-hour block spanning
# two batches.
# --------------------------------------------------------------------------

def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _readings(J, count=16):
    """``count`` readings every 30 minutes from four hours before ``W1``:
    the first eight in week 1 (rising), the rest in week 2 after the reset.
    The first five-hour block resets one hour after ``W1``, so it spans the
    inputs either side of a K=4 batch boundary."""
    base = W1 - 4 * 3600
    out = []
    for index in range(count):
        captured = base + index * 1800
        week_one = captured < W1
        payload = {
            "captured_at": _iso(captured),
            "source": "statusline",
            "weekly_percent": (10.0 + 4 * index) if week_one
            else (1.0 + index - 8),
            "resets_at": W1 if week_one else W2,
            "five_hour_percent": float(5 + 3 * (index % 10)),
            "five_hour_resets_at": _iso(
                W1 + 3600 if captured < W1 + 3600 else W1 + 6 * 3600),
        }
        out.append(J.make_obs(at=_iso(captured), src="record-usage",
                              provider="claude", account="acct-a",
                              payload=payload))
    return out


def _scratch(tmp_path, name, *, policy=True):
    import _cctally_core
    import _cctally_store
    conn = _cctally_core.open_db(_target_path=str(tmp_path / name))
    if policy:
        _cctally_store.apply_disposable_scratch_policy(conn)
    return conn


def _replay(conn, records, *, retention=None, prepare=None):
    import _cctally_completion
    import _cctally_quarantine
    import _cctally_rederive
    fold = _cctally_quarantine.QuarantineFold.empty(held_ids=frozenset())
    kwargs = {} if prepare is None else {"prepare": prepare}
    return _cctally_rederive.replay_scratch_records(
        conn, records, None, fold=fold,
        read_adapters=_cctally_completion._no_cache_read_adapters(),
        retention=retention, **kwargs)


#: Tables whose rows carry the scratch's own creation time, not replay output.
_VOLATILE_TABLES = frozenset({"schema_migrations"})


def _dump(conn) -> dict:
    """Every user table's rows, surrogate ids included (both replays insert
    in the same order, so even rowids must agree)."""
    out = {}
    for (table,) in sqlite3.Connection.execute(
            conn, "SELECT name FROM sqlite_schema WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if table in _VOLATILE_TABLES:
            continue
        out[table] = sorted(
            (tuple(row) for row in sqlite3.Connection.execute(
                conn, f"SELECT * FROM {table}")), key=repr)
    return out


def _with_retained_offer(J, readings, derived_events):
    """``readings`` with one retained decision offered mid-batch: the
    snapshot decision input 6 derives, journaled just before input 6 (window
    position 6, the third modifying input of the second K=4 batch). Returns
    ``(records, effective)``."""
    target = "sa:" + readings[6]["id"]
    [evt] = [e for e in derived_events if e["id"] == target]
    records = list(readings[:6]) + [evt] + list(readings[6:])
    return records, {target: evt}


def _retention(effective):
    import _cctally_journal
    return _cctally_journal.ScratchRetention(effective)


# --------------------------------------------------------------------------
# T1 — the scratch policy reaches exactly the replay scratches
# --------------------------------------------------------------------------

def _obs(J, at, pct):
    return J.make_obs(at=at, src="record-usage", provider="claude", payload={
        "weekly_percent": pct, "resets_at": W1, "source": "statusline",
        "captured_at": at})


def test_replay_scratch_connections_use_disposable_policy(
        ns, monkeypatch, tmp_path, capsys):
    """Spec T1. A completion whose predecessor owes decisions (a reading
    ingested, a second reading appended and never derived, then `db
    rebuild`) opens the predecessor `base` and the replay copies; `db
    rederive`'s scratch derivation opens `stats.rederive.db`. Each reports
    `journal_mode=memory`, `synchronous=0` and the three replay indexes.
    Fails before #997: they ran under the stats store policy."""
    import argparse
    import _cctally_journal as jr
    import _cctally_rederive
    import _lib_journal as J

    seen: dict = {}
    real_apply = jr._apply_evt
    real_replay = _cctally_rederive.replay_scratch_records

    def spy_apply(conn, record, *args, **kwargs):
        path = _db_path(conn)
        if path.endswith("predecessor.db") and "base" not in seen:
            seen["base"] = _policy_of(conn)
        return real_apply(conn, record, *args, **kwargs)

    def spy_replay(conn, records, *args, **kwargs):
        name = _db_path(conn).rsplit("/", 1)[-1]
        seen.setdefault(name, _policy_of(conn))
        return real_replay(conn, records, *args, **kwargs)

    monkeypatch.setattr(jr, "_apply_evt", spy_apply)
    monkeypatch.setattr(_cctally_rederive, "replay_scratch_records",
                        spy_replay)

    jr.append_record(_obs(J, "2026-07-26T09:00:00Z", 70.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-07-26T10:00:00Z", 60.0))
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0

    cache = ns["open_cache_db"]()
    try:
        _cctally_rederive._derive_desired_events(
            [_obs(J, "2026-07-26T11:00:00Z", 75.0)], cache, tmp_path)
    finally:
        cache.close()
    capsys.readouterr()

    expected = {"journal_mode": "memory", "synchronous": 0,
                "indexes": _REPLAY_INDEXES}
    assert seen.get("base") == expected, seen
    assert seen.get("all-0.db") == expected, seen
    assert seen.get("stats.rederive.db") == expected, seen


def test_live_rebuild_scratch_keeps_store_policy_and_fingerprint(
        ns, monkeypatch, tmp_path):
    """Spec T1, the other side: the live rebuild's `.rebuilding-*` scratch is
    published, so it keeps the stats policy (`delete`/2), carries none of the
    replay indexes, and the published index's schema fingerprint is
    unchanged."""
    import argparse
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J

    seen: list = []
    real_apply = jr._apply_evt

    def spy_apply(conn, record, *args, **kwargs):
        if ".rebuilding-" in _db_path(conn):
            seen.append(_policy_of(conn))
        return real_apply(conn, record, *args, **kwargs)

    monkeypatch.setattr(jr, "_apply_evt", spy_apply)
    jr.append_record(_obs(J, "2026-07-26T09:00:00Z", 70.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-07-26T10:00:00Z", 60.0))
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0

    assert seen, "the fixture never folded into a rebuild scratch"
    for policy in seen:
        assert policy == {"journal_mode": "delete", "synchronous": 2,
                          "indexes": set()}, policy
    conn = core.open_db()
    try:
        assert _policy_of(conn)["indexes"] == set()
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
    finally:
        conn.close()


def test_scratch_policy_without_usage_table_sets_pragmas_only(tmp_path):
    """The policy creates the replay indexes only where the table exists, so
    it is safe on any scratch connection."""
    import _cctally_store
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        _cctally_store.apply_disposable_scratch_policy(conn)
        assert _policy_of(conn) == {
            "journal_mode": "memory", "synchronous": 0, "indexes": set()}
        assert not conn.in_transaction
    finally:
        conn.close()


# --------------------------------------------------------------------------
# T2 — batched commits
# --------------------------------------------------------------------------

def _transaction_trace(conn) -> list:
    statements: list = []

    def trace(sql):
        head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in ("BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT",
                    "RELEASE"):
            statements.append(head)

    conn.set_trace_callback(trace)
    return statements


def test_batched_commit_count(ns, monkeypatch, tmp_path):
    """Spec T2. With the bound patched to 4, sixteen readings (crossing a
    weekly reset, a five-hour block spanning two batches) plus one retained
    decision offered mid-batch are 17 modifying inputs: exactly
    ceil(17 / 4) = 5 transactions, and no other transaction control from the
    hooks. Before #997 it committed once per input (17)."""
    import _cctally_rederive
    import _lib_journal as J

    readings = _readings(J)
    probe = _scratch(tmp_path, "probe.db")
    try:
        derived = _replay(probe, readings)
    finally:
        probe.close()
    records, effective = _with_retained_offer(J, readings, derived)

    monkeypatch.setattr(_cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", 4)
    conn = _scratch(tmp_path, "batched.db")
    try:
        statements = _transaction_trace(conn)
        _replay(conn, records, retention=_retention(effective))
        conn.set_trace_callback(None)
    finally:
        conn.close()
    assert statements.count("COMMIT") == 5, statements
    assert statements.count("BEGIN") == 5, statements
    assert set(statements) == {"BEGIN", "COMMIT"}, statements


def test_batched_equals_per_input(ns, monkeypatch, tmp_path):
    """Spec T2: only the commit boundary moves. K=4 and K=1 derive
    byte-identical candidates and leave identical scratch tables, including
    the retained decision offered mid-batch."""
    import _cctally_rederive
    import _lib_journal as J

    readings = _readings(J)
    probe = _scratch(tmp_path, "probe.db")
    try:
        derived = _replay(probe, readings)
    finally:
        probe.close()
    records, effective = _with_retained_offer(J, readings, derived)

    results = {}
    for bound in (4, 1):
        monkeypatch.setattr(
            _cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", bound)
        conn = _scratch(tmp_path, f"k{bound}.db")
        try:
            events = _replay(conn, records, retention=_retention(effective))
            results[bound] = (
                json.dumps(events, sort_keys=True), _dump(conn))
        finally:
            conn.close()
    assert results[4][0] == results[1][0]
    assert results[4][1] == results[1][1]
    assert json.loads(results[4][0]), "the fixture derived nothing"


def test_mid_batch_failure_rolls_back(ns, monkeypatch, tmp_path):
    """Spec T2: a hook raising on input 6 (the second input of the second K=4
    batch) propagates, leaves no open transaction, and leaves no row of
    inputs 5–6; the first batch stays committed."""
    import _cctally_record
    import _cctally_rederive
    import _lib_journal as J

    readings = _readings(J)
    monkeypatch.setattr(_cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", 4)
    real = _cctally_record._pipeline_record_credit
    failing = readings[5]["id"]

    def hook(ctx, record):
        if record.get("id") == failing:
            raise RuntimeError("simulated hook failure")
        return real(ctx, record)

    reference = _scratch(tmp_path, "first-batch.db")
    try:
        _replay(reference, readings[:4])
        expected = _dump(reference)
    finally:
        reference.close()

    monkeypatch.setattr(_cctally_record, "_pipeline_record_credit", hook)
    conn = _scratch(tmp_path, "failing.db")
    try:
        with pytest.raises(RuntimeError, match="simulated hook failure"):
            _replay(conn, readings)
        assert not conn.in_transaction
        left = _dump(conn)
    finally:
        conn.close()
    assert expected["weekly_usage_snapshots"], "non-vacuity: batch 1 wrote"
    assert left == expected, "rows of the failed batch survived"


# --------------------------------------------------------------------------
# T5 — per-record preparation (spec D5)
# --------------------------------------------------------------------------

def _is_record_list(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, dict) and "t" in item
                    for item in value))


@pytest.fixture
def no_record_list_deepcopy(monkeypatch):
    """`copy.deepcopy` that raises when handed a list of journal records at
    top level. `copy`'s own recursion calls the patched name too, so nested
    lists inside one record pass through at depth > 0."""
    real = copy.deepcopy
    depth = [0]
    refused: list = []

    def spy(value, memo=None, *rest):
        if depth[0] == 0 and _is_record_list(value):
            refused.append(len(value))
            raise AssertionError(
                f"deep copy of a {len(value)}-record list")
        depth[0] += 1
        try:
            return real(value, memo, *rest)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(copy, "deepcopy", spy)
    return refused


def _legacy_claude(J, at, pct, *, resets=W1):
    """A previous-release Claude reading: no writer stamp, no account."""
    return J.unstamped_record(J.make_obs(
        at=at, src="record-usage", provider="claude", payload={
            "weekly_percent": pct, "resets_at": resets,
            "source": "statusline", "captured_at": at}))


def _cutover(J, account="acct-a"):
    import _cctally_journal as runtime
    op = J.make_op(at="2026-07-25T11:00:00Z", src="accounts-cutover",
                   payload={"kind": "accounts_cutover",
                            "claude_legacy_account": account})
    op["id"] = runtime.CUTOVER_OP_ID
    return op


def _seed_cache(mod, account="acct-a"):
    path = "/tmp/claude/projects/repo/session.jsonl"
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 100, 1, 100, "2026-07-25T12:00:00Z", "session-a", "/repo"))
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, 0, "2026-07-25T11:30:00+00:00",
         "claude-3-5-sonnet-20241022", 10, 10, 100, 0, 40, account))
    conn.commit()
    return conn


def _plan(mod, records, cache):
    return mod.plan_claude_usage_rederive(
        records, cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 900))


def _upfront_plan(mod, records, cache):
    """The pre-#997 planner input: the whole list deep-copied, then
    normalized in place by the same global pass."""
    import _cctally_rederive
    upfront = copy.deepcopy(list(records))
    _cctally_rederive._normalize_legacy_accounts(upfront)
    return _plan(mod, upfront, cache)


def test_replay_never_deepcopies_record_list(
        ns, monkeypatch, tmp_path, no_record_list_deepcopy, capsys):
    """Spec T5: a completion's replays and the rederive planner prepare one
    record at a time; neither deep-copies the record list (13 GB resident on
    the incident copy). Fails before #997 on `_replay`'s and
    `plan_claude_usage`'s list copies."""
    import argparse
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_completion

    replays: list = []
    real = _cctally_completion._replay

    def counting(*args, **kwargs):
        replays.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(_cctally_completion, "_replay", counting)
    jr.append_record(_obs(J, "2026-07-26T09:00:00Z", 70.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-07-26T10:00:00Z", 60.0))
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert replays, "the fixture ran no completion replay"

    mod = __import__("sys").modules["cctally"]
    cache = _seed_cache(mod)
    try:
        plan = _plan(mod, [_cutover(J), _legacy_claude(
            J, "2026-07-25T12:00:00Z", 10.0)], cache)
    finally:
        cache.close()
    assert plan.actions
    assert no_record_list_deepcopy == []


def test_prefix_window_unchanged_after_replays(ns, monkeypatch, capsys):
    """Spec T5: the replays prepare copies, so the completion window each
    replay reads is byte-for-byte what `_read_prefix` produced."""
    import argparse
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_completion

    checked: list = []
    real = _cctally_completion._replay

    def checking(base, tmp, name, prefix, *args, **kwargs):
        before = json.dumps(prefix.window, sort_keys=True)
        try:
            return real(base, tmp, name, prefix, *args, **kwargs)
        finally:
            checked.append(
                json.dumps(prefix.window, sort_keys=True) == before)

    monkeypatch.setattr(_cctally_completion, "_replay", checking)
    jr.append_record(_legacy_claude(J, "2026-07-26T08:00:00Z", 50.0))
    jr.ensure_account_bound_cutover()
    jr.append_record(_obs(J, "2026-07-26T09:00:00Z", 70.0))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_obs(J, "2026-07-26T10:00:00Z", 60.0))
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert checked and all(checked), checked


def test_prepared_replay_equals_upfront_copy(ns, tmp_path):
    """Spec T5: preparing each record (deep copy, then legacy account
    normalization; an excluded placeholder only copied) derives exactly what
    the up-front whole-window copy derived, on a window of legacy and current
    Claude readings, a Codex quota observation and an excluded record."""
    import _cctally_completion
    import _cctally_journal as runtime
    import _lib_account_protocol
    import _lib_journal as J

    excluded = _legacy_claude(J, "2026-07-26T07:30:00Z", 45.0)
    placeholder = {**_lib_account_protocol.excluded_placeholder(),
                   "quarantine": {"record": excluded,
                                  "segment": "observations-2026-07.jsonl",
                                  "offset": 0}}
    window = [
        _legacy_claude(J, "2026-07-26T07:00:00Z", 40.0),
        placeholder,
        J.unstamped_record(J.make_obs(
            at="2026-07-26T07:40:00Z", src="codex-quota", provider="codex",
            payload={"kind": "quota_window_snapshot", "used_percent": 3.0})),
        _legacy_claude(J, "2026-07-26T08:00:00Z", 50.0),
        *_readings(J, 6),
    ]
    snapshot = json.dumps(window, sort_keys=True)

    upfront = copy.deepcopy(window)
    for record in upfront:
        if record.get("t") != _lib_account_protocol.EXCLUDED_PLACEHOLDER_TYPE:
            runtime._normalize_legacy_account_stamp(record, "acct-a")
    conn = _scratch(tmp_path, "upfront.db")
    try:
        expected = (json.dumps(_replay(conn, upfront), sort_keys=True),
                    _dump(conn))
    finally:
        conn.close()

    conn = _scratch(tmp_path, "prepared.db")
    try:
        got = (json.dumps(_replay(
            conn, window, prepare=lambda record: (
                _cctally_completion._normalized_copy(record, "acct-a"))),
            sort_keys=True), _dump(conn))
    finally:
        conn.close()
    assert got == expected
    assert json.loads(got[0]), "the fixture derived nothing"
    assert json.dumps(window, sort_keys=True) == snapshot


def test_plan_does_not_mutate_caller_records(ns, tmp_path):
    """Spec D5: the planner's normalized view never writes into the caller's
    records, which `apply_db_rederive` reads afterwards."""
    import _lib_journal as J
    mod = __import__("sys").modules["cctally"]
    records = [_cutover(J), _legacy_claude(J, "2026-07-25T12:00:00Z", 10.0),
               J.make_evt(kind="snapshot_accept", id="sa:legacy", at=(
                   "2026-07-25T12:00:00Z"), payload={"weekly_percent": 10.0})]
    records[2] = J.unstamped_record(records[2])
    before = copy.deepcopy(records)
    cache = _seed_cache(mod)
    try:
        _plan(mod, records, cache)
    finally:
        cache.close()
    assert records == before


def test_plan_view_legacy_account_cache_validation(ns, tmp_path):
    """Spec T5: a legacy positive Claude reading that `accounts_cutover` maps
    to a real account is validated against THAT account's cache rows and
    planned under it, exactly as the up-front copy did."""
    import _lib_journal as J
    mod = __import__("sys").modules["cctally"]
    records = [_cutover(J), _legacy_claude(J, "2026-07-25T12:00:00Z", 10.0)]
    cache = _seed_cache(mod, account="acct-a")
    try:
        plan = _plan(mod, records, cache)
        expected = _upfront_plan(mod, records, cache)
    finally:
        cache.close()
    snapshots = [a.payload for a in plan.actions
                 if (a.payload or {}).get("kind") == "snapshot_accept"]
    assert snapshots and {p["account_key"] for p in snapshots} == {"acct-a"}
    assert plan.to_bytes() == expected.to_bytes()


def test_plan_view_conflicting_cutover_refused(ns, tmp_path):
    """Spec T5: conflicting cutover values refuse before anything is
    planned, with the same conflict as before."""
    import _cctally_rederive
    import _lib_journal as J
    import _lib_rederive
    mod = __import__("sys").modules["cctally"]
    other = _cutover(J, account="acct-b")
    other["at"] = "2026-07-25T11:30:00Z"
    records = [_cutover(J), other,
               _legacy_claude(J, "2026-07-25T12:00:00Z", 10.0)]
    cache = _seed_cache(mod)
    try:
        with pytest.raises(_lib_rederive.RederiveConflict,
                           match="conflicting accounts_cutover"):
            _plan(mod, records, cache)
        with pytest.raises(_lib_rederive.RederiveConflict,
                           match="conflicting accounts_cutover"):
            _cctally_rederive._normalized_planning_view(records)
    finally:
        cache.close()


def test_plan_view_repeated_raw_id(ns, tmp_path):
    """Spec T5: a repeated raw reading (two equal records, and one record
    object at two positions) keeps its multiplicity and positions through the
    view, so snapshot-identity reconciliation sees what it saw before."""
    import _lib_journal as J
    mod = __import__("sys").modules["cctally"]
    reading = _legacy_claude(J, "2026-07-25T12:00:00Z", 10.0)
    later = _legacy_claude(J, "2026-07-25T12:05:00Z", 12.0)
    records = [_cutover(J), reading, copy.deepcopy(reading), later, later]
    cache = _seed_cache(mod)
    try:
        plan = _plan(mod, records, cache)
        expected = _upfront_plan(mod, records, cache)
    finally:
        cache.close()
    assert plan.to_bytes() == expected.to_bytes()
    import _cctally_rederive
    view = _cctally_rederive._normalized_planning_view(records)
    assert len(view) == len(records)
    assert view[3] is view[4] and view[1] is not view[2]
    assert view[1] == view[2] and view[1]["account"] == "acct-a"


def test_plan_view_reviewed_baseline(ns, tmp_path):
    """Spec T5: a retained reviewed weekly decision holding a legacy reading,
    over a retained baseline of previous-release decisions, plans exactly as
    the up-front copy did."""
    import _lib_journal as J
    mod = __import__("sys").modules["cctally"]
    first = _legacy_claude(J, "2026-07-25T12:00:00Z", 60.0)
    stale = _legacy_claude(J, "2026-07-25T12:01:00Z", 10.0, resets=W2)
    genuine = _legacy_claude(J, "2026-07-25T12:02:00Z", 61.0)
    cache = _seed_cache(mod)
    try:
        baseline = _plan(mod, [_cutover(J), first, stale, genuine], cache)
        retained = []
        for action in baseline.actions:
            payload = dict(action.payload or {})
            payload.pop("account_key", None)
            retained.append(J.unstamped_record(J.make_evt(
                kind=payload.pop("kind"), id=action.event_id, at=action.at,
                payload=payload)))
        reviewed = J.make_op(
            at="2026-07-25T12:10:00Z", src="db-rederive", payload={
                "kind": "claude_weekly_observation_decision",
                "schema_version": 1,
                "journal_high_water": {
                    "segment": "observations-2026-07.jsonl", "offset": 1},
                "journal_prefix_hash": "sha256:" + "0" * 64,
                "reason": "operator review",
                "decisions": [{"observationId": stale["id"],
                               "disposition": "hold"}],
            })
        records = [_cutover(J), first, stale, genuine, *retained, reviewed]
        plan = _plan(mod, records, cache)
        expected = _upfront_plan(mod, records, cache)
    finally:
        cache.close()
    assert retained, "non-vacuity: the baseline planned decisions"
    assert plan.to_bytes() == expected.to_bytes()


def test_plan_view_shared_stub_normalized_once(ns, monkeypatch):
    """Spec D4/D5: one shared inert observation object at many positions is
    normalized once, every position receives that one normalized object, a
    record normalization leaves unchanged is shared with the caller, and the
    caller's objects are untouched."""
    import _cctally_rederive
    import _lib_journal as J

    stub = {"t": "obs", "provider": "codex", "src": "codex-quota",
            "payload": {"kind": "quota_window_snapshot"}}
    current = _obs(J, "2026-07-25T12:00:00Z", 10.0)
    current["account"] = "acct-a"
    records = [_cutover(J), stub, current, stub, stub]
    copies: list = []
    real = copy.deepcopy

    def counting(value, memo=None, *rest):
        if value is stub:
            copies.append(value)
        return real(value, memo, *rest)

    monkeypatch.setattr(copy, "deepcopy", counting)
    view = _cctally_rederive._normalized_planning_view(records)
    monkeypatch.setattr(copy, "deepcopy", real)
    assert len(copies) == 1
    assert view[1] is view[3] is view[4]
    assert view[1] is not stub and "account" not in stub
    assert view[1]["account"]
    assert view[2] is current and view[0] is records[0]
