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
        # #1003 D2: the page cache that keeps a replay batch's dirty pages
        # from spilling (and being re-written) before the commit.
        "cache_size": int(sqlite3.Connection.execute(
            conn, "PRAGMA cache_size").fetchone()[0]),
    }


#: #1003 T8: SQLite's compiled default page cache, which every connection
#: the disposable policy does not govern keeps.
def _compiled_default_cache_size() -> int:
    conn = sqlite3.connect(":memory:")
    try:
        return int(conn.execute("PRAGMA cache_size").fetchone()[0])
    finally:
        conn.close()


_DEFAULT_CACHE_SIZE = _compiled_default_cache_size()
#: #1003 D2: `PRAGMA cache_size=-65536` (64 MiB) on disposable scratches.
_SCRATCH_CACHE_SIZE = -65536


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


@pytest.mark.parametrize("legacy_input", [False, True])
@pytest.mark.parametrize("reference", ["none", "lacks", "holds"])
def test_companion_withholding_agrees_with_decide_for_a_tombstoned_id(
        ns, legacy_input, reference):
    """#943 review R-4. The five-hour confirm leg asks `withholds` whether a
    credit's boundary companion will materialize before the companion's
    content exists. A disagreement with `decide` makes the stale-replica
    cleanup band from the wrong instant: from the slot for a companion that
    materializes, or from the exact boundary for a credit read from its slot.
    For a companion tombstoned in the effective set, both agree with no
    reference, with a with-gap reference that lacks the id (affected: the
    fresh candidate) and with one holding its unchanged content (the
    tombstone stands), on a current and on a previous-release input."""
    import _cctally_journal
    import _lib_journal as J
    evt = {"t": "evt", "v": 1, "src": "ingest", "rev": 0,
           "id": "fhcb:acct:1792633200:2026-10-22T00:20:00+00:00",
           "at": "2026-10-22T00:33:05Z",
           "payload": {"kind": "five_hour_credit_boundary",
                       "boundary_at_utc": "2026-10-22T00:29:51+00:00"}}
    references = {"none": None, "lacks": {},
                  "holds": {evt["id"]: J.event_content_hash(evt)}}
    retention = _cctally_journal.ScratchRetention(
        {evt["id"]: None}, reference=references[reference],
        retained_only=frozenset({6}) if legacy_input else frozenset())
    retention.current_input = 6
    predicted = retention.withholds(evt["id"])
    chosen, _fresh = retention.decide(copy.deepcopy(evt))
    assert predicted == (chosen is None), (reference, legacy_input, chosen)


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
                "indexes": _REPLAY_INDEXES,
                "cache_size": _SCRATCH_CACHE_SIZE}
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
                          "indexes": set(),
                          "cache_size": _DEFAULT_CACHE_SIZE}, policy
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
            "journal_mode": "memory", "synchronous": 0, "indexes": set(),
            "cache_size": _SCRATCH_CACHE_SIZE}
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
        # #1004 D4: `window` is a `RetainedRecords` (each access of a
        # streamed position is a fresh decode; placeholders are retained).
        before = json.dumps(list(prefix.window), sort_keys=True)
        try:
            return real(base, tmp, name, prefix, *args, **kwargs)
        finally:
            checked.append(
                json.dumps(list(prefix.window), sort_keys=True) == before)

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


# --------------------------------------------------------------------------
# #1003 T2/T3 — the incremental block-totals engine's scope and equality
# --------------------------------------------------------------------------

def _seed_block_cache(mod, account="acct-a"):
    """Claude cache rows every 20 minutes from five hours before ``W1`` to
    seven hours after it, two projects, a raw-cost row, a fast row and a
    model with no card: the five-hour blocks of `_readings` own a growing
    population, so each tick's totals differ from the last."""
    paths = ("/tmp/claude/projects/repo-a/s-a.jsonl",
             "/tmp/claude/projects/repo-b/s-b.jsonl")
    conn = mod.open_cache_db()
    for path, session, project in zip(paths, ("sa", "sb"),
                                      ("/repo-a", "/repo-b")):
        conn.execute(
            "INSERT INTO session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, 100, 1, 100, "2026-07-25T12:00:00Z", session, project))
    start = W1 - 5 * 3600
    for index in range(36):
        moment = dt.datetime.fromtimestamp(start + index * 1200, UTC)
        model = ("claude-zz-1003-unpriced" if index % 11 == 5
                 else "claude-opus-4-6" if index % 5 == 0
                 else "claude-3-5-sonnet-20241022")
        conn.execute(
            "INSERT INTO session_entries (source_path, line_offset, "
            "timestamp_utc, model, input_tokens, output_tokens, "
            "cache_create_tokens, cache_read_tokens, cache_create_1h_tokens, "
            "account_key, cost_usd_raw, speed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (paths[index % 2], index * 10,
             moment.isoformat() if index % 3 else
             moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
             model, 1000 + index, 200 + index, 300, 400 + index,
             100 if index % 2 else 0, account,
             0.25 if index % 7 == 3 else None,
             "fast" if index % 5 == 0 else None))
    conn.commit()
    return conn


def _block_readings(J):
    """`_readings` (a weekly reset, a five-hour block spanning several K=4
    batches) plus a week-2 in-place drop of more than 25 points (a credit)."""
    readings = _readings(J)
    for offset, pct in ((W1 + 4 * 3600, 60.0), (W1 + 4 * 3600 + 900, 20.0)):
        captured = offset
        readings.append(J.make_obs(
            at=_iso(captured), src="record-usage", provider="claude",
            account="acct-a", payload={
                "captured_at": _iso(captured), "source": "statusline",
                "weekly_percent": pct, "resets_at": W2,
                "five_hour_percent": 40.0,
                "five_hour_resets_at": _iso(W1 + 6 * 3600)}))
    return readings


@pytest.fixture
def engine_off(monkeypatch):
    """Force the engine slot empty inside every scratch replay: the
    engine-free reference path."""
    import _cctally_rederive

    def apply():
        monkeypatch.setattr(_cctally_rederive, "BlockTotalsEngine",
                            lambda cache_conn: None)
    return apply


def test_engine_slot_is_scoped_to_scratch_read_adapters(ns, tmp_path):
    """#1003 T2: `_scratch_read_adapters` installs a fresh engine for its
    context and restores the previous slot on normal exit and on an
    exception; `_no_cache_read_adapters` installs none. Detects an engine
    that leaks past a replay into live recording (which would cache a
    `cache.db` that live ingest keeps changing)."""
    import _cctally_completion
    import _cctally_record as rec
    import _cctally_rederive

    cache = ns["open_cache_db"]()
    sentinel = object()
    previous = rec.swap_block_totals_engine(sentinel)
    try:
        with _cctally_rederive._scratch_read_adapters(cache):
            first = rec._BLOCK_TOTALS_ENGINE
            assert isinstance(first, _cctally_rederive.BlockTotalsEngine)
        assert rec._BLOCK_TOTALS_ENGINE is sentinel
        with pytest.raises(RuntimeError, match="replay failed"):
            with _cctally_rederive._scratch_read_adapters(cache):
                second = rec._BLOCK_TOTALS_ENGINE
                assert isinstance(second, _cctally_rederive.BlockTotalsEngine)
                assert second is not first
                raise RuntimeError("replay failed")
        assert rec._BLOCK_TOTALS_ENGINE is sentinel
        with _cctally_completion._no_cache_read_adapters():
            assert rec._BLOCK_TOTALS_ENGINE is sentinel
    finally:
        rec.swap_block_totals_engine(previous)
        cache.close()
    assert _cctally_rederive.BlockTotalsEngine.verify is False


def test_live_and_final_rebuild_paths_never_see_an_engine(
        ns, monkeypatch, capsys):
    """#1003 T2: live ingest (`maybe_update_five_hour_block`) and the final
    rebuild's open-block projection run with an empty slot; only a
    completion replay over a readable cache uses the engine. Detects a live
    or published path answered from a replay's cache snapshot."""
    import argparse
    import _cctally_journal as jr
    import _cctally_record as rec
    import _cctally_rederive
    import _lib_journal as J

    mod = __import__("sys").modules["cctally"]
    _seed_block_cache(mod).close()
    calls: list = []
    depth = [0]
    real_replay = _cctally_rederive.replay_scratch_records
    real_body = rec._block_totals_reprice
    real_totals = _cctally_rederive.BlockTotalsEngine.totals

    def spy_replay(*args, **kwargs):
        depth[0] += 1
        try:
            return real_replay(*args, **kwargs)
        finally:
            depth[0] -= 1

    def spy_body(*args, **kwargs):
        calls.append(("body", depth[0] > 0, rec._BLOCK_TOTALS_ENGINE))
        return real_body(*args, **kwargs)

    def spy_totals(self, *args, **kwargs):
        calls.append(("engine", depth[0] > 0, self))
        return real_totals(self, *args, **kwargs)

    monkeypatch.setattr(_cctally_rederive, "replay_scratch_records",
                        spy_replay)
    monkeypatch.setattr(rec, "_block_totals_reprice", spy_body)
    monkeypatch.setattr(_cctally_rederive.BlockTotalsEngine, "totals",
                        spy_totals)

    readings = _readings(J, 10)
    for record in readings[:6]:
        jr.append_record(record)
    jr.run_stats_ingest(mode="authoritative")
    live = list(calls)
    for record in readings[6:]:
        jr.append_record(record)
    calls.clear()
    assert ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()

    assert live and all(kind == "body" and not in_replay and slot is None
                        for kind, in_replay, slot in live), live
    outside = [c for c in calls if not c[1]]
    inside = [c for c in calls if c[1]]
    assert outside, "non-vacuity: the final rebuild projected an open block"
    assert all(kind == "body" and slot is None
               for kind, _, slot in outside), outside
    assert inside and all(kind == "engine" for kind, _, _ in inside), inside


class _RecordingSink(dict):
    """A `block_source_evidence` sink that asks for every block and records
    each stored population with its by-value snapshot at storage time."""

    def __init__(self):
        super().__init__()
        self.log: list = []

    def __contains__(self, key):
        return True

    def __setitem__(self, key, value):
        self.log.append((key, value, tuple(_entry_value(e) for e in value)))
        super().__setitem__(key, value)


def _entry_value(entry) -> tuple:
    return tuple((name, value.isoformat() if isinstance(value, dt.datetime)
                  else value) for name, value in vars(entry).items())


def test_shared_owned_entries_are_never_mutated(
        ns, tmp_path, monkeypatch, engine_off):
    """#1003 T2: the engine shares one entry object between calls where the
    full re-price built fresh ones, and `block_source_evidence` retains the
    owned entries of every tick. Each stored population must still equal,
    by value, its snapshot at storage time after the whole replay, and the
    populations must equal the engine-free replay's. Detects a later tick
    mutating an entry an earlier tick's evidence holds."""
    import _cctally_rederive
    import _lib_journal as J

    monkeypatch.setattr(_cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", 4)
    mod = __import__("sys").modules["cctally"]
    cache = _seed_block_cache(mod)
    records = _block_readings(J)
    logs = {}
    try:
        for mode in ("engine", "off"):
            if mode == "off":
                engine_off()
            sink = _RecordingSink()
            scratch_dir = tmp_path / mode
            scratch_dir.mkdir()
            _cctally_rederive._derive_desired_events(
                records, cache, scratch_dir, block_sources=sink)
            for key, value, snapshot in sink.log:
                assert tuple(_entry_value(e) for e in value) == snapshot, key
            logs[mode] = [(key, snapshot) for key, _, snapshot in sink.log]
    finally:
        cache.close()
    assert any(snapshot for _, snapshot in logs["engine"]), (
        "non-vacuity: a tick retained owned entries")
    assert logs["engine"] == logs["off"]


def _replay_with_cache(conn, records, cache, *, held_ids, retention):
    import _cctally_quarantine
    import _cctally_rederive
    fold = _cctally_quarantine.QuarantineFold.empty(held_ids=held_ids)
    return _cctally_rederive.replay_scratch_records(
        conn, records, cache, fold=fold, held_ids=held_ids,
        retention=retention, prepare=copy.deepcopy)


def test_scratch_replay_equal_with_and_without_engine(
        ns, tmp_path, monkeypatch, engine_off):
    """#1003 T3: a scratch replay over a five-hour block spanning several
    commit batches, a weekly reset, an in-place credit, a reviewed hold and
    a retained decision offered mid-batch derives byte-identical candidates
    and scratch tables with the engine (shadow verification on) and with the
    slot forced empty. Detects any engine answer that changes a derived
    row, milestone or candidate."""
    import _cctally_rederive
    import _lib_journal as J

    monkeypatch.setattr(_cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", 4)
    mod = __import__("sys").modules["cctally"]
    cache = _seed_block_cache(mod)
    readings = _block_readings(J)
    held = frozenset({readings[3]["id"]})
    try:
        probe = _scratch(tmp_path, "probe.db")
        try:
            derived = _replay_with_cache(probe, readings, cache,
                                         held_ids=held, retention=None)
        finally:
            probe.close()
        records, effective = _with_retained_offer(J, readings, derived)
        results = {}
        engines: list = []
        real_engine = _cctally_rederive.BlockTotalsEngine

        def tracking(cache_conn):
            engines.append(real_engine(cache_conn))
            return engines[-1]

        for mode in ("engine", "off"):
            if mode == "engine":
                monkeypatch.setattr(real_engine, "verify", True)
                monkeypatch.setattr(_cctally_rederive, "BlockTotalsEngine",
                                    tracking)
            else:
                engine_off()
            conn = _scratch(tmp_path, f"{mode}.db")
            try:
                events = _replay_with_cache(
                    conn, records, cache, held_ids=held,
                    retention=_retention(effective))
                results[mode] = (json.dumps(events, sort_keys=True),
                                 _dump(conn))
            finally:
                conn.close()
    finally:
        cache.close()
    assert results["engine"][0] == results["off"][0]
    assert results["engine"][1] == results["off"][1]
    assert json.loads(results["engine"][0]), "the fixture derived nothing"
    assert results["engine"][1]["five_hour_blocks"], "no five-hour block"
    assert any(row for row in results["engine"][1]["five_hour_block_models"])
    [engine] = engines
    assert engine.counts["extended"] + engine.counts["cached"] > 0


def _snapshot_dir(share, dest):
    import shutil
    shutil.copytree(share, dest)


def _restore_dir(share, src):
    import shutil
    shutil.rmtree(share)
    shutil.copytree(src, share)


def _journal_bytes_of(share) -> dict:
    return {path.name: path.read_bytes()
            for path in sorted((share / "journal").glob("*.jsonl"))}


#: Published tables whose differing columns are the rebuild's own volatile
#: stamps (spec T10's frozen allowlist), not derived output.
_PUBLISHED_VOLATILE = frozenset({
    "schema_migrations", "journal_selector_state", "quota_percent_milestones",
    "quota_projection_ledger_state", "quota_projection_state",
    "quota_window_blocks", "stats_publication_stamp",
})


def _published(path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        out = {}
        for (table,) in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if table in _PUBLISHED_VOLATILE:
                continue
            out[table] = sorted((tuple(r) for r in conn.execute(
                f"SELECT * FROM {table}")), key=repr)
        return out
    finally:
        conn.close()


def _pin_wall_clocks(monkeypatch):
    """Pin every wall clock that reaches journal bytes, so the engine and
    engine-free runs (seconds apart) append identical lines. The correction
    batch's begin and commit `at` is `_cctally_rederive._iso_now()` for
    `db rederive` and `_cctally_completion._iso_now()` for completion, at
    one-second resolution: unpinned, the two runs differed whenever they
    straddled a second boundary (observed: `12:46:28Z` against `12:46:29Z`)."""
    import _cctally_claude_attribution
    import _cctally_completion
    import _cctally_journal as jr
    import _cctally_rederive
    monkeypatch.setattr(jr, "_now_iso", lambda: "2026-07-30T00:00:00Z")
    monkeypatch.setattr(_cctally_claude_attribution, "_now_iso",
                        lambda: "2026-07-30T00:00:00Z")
    monkeypatch.setattr(_cctally_rederive, "_iso_now",
                        lambda: "2026-07-30T00:00:00Z")
    monkeypatch.setattr(_cctally_completion, "_iso_now",
                        lambda: "2026-07-30T00:00:00Z")


def test_completion_equal_with_and_without_engine(
        ns, tmp_path, monkeypatch, capsys, engine_off):
    """#1003 T3: a completing `db rebuild` (the predecessor owes the inputs
    appended after its ingest) over a readable cache appends identical
    journal bytes, publishes an identical index and derives identical
    replay candidates with the engine (verification on) and with the slot
    forced empty. Detects a completion correction batch that the engine
    changes."""
    import argparse
    import _cctally_core
    import _cctally_journal as jr
    import _cctally_rederive
    import _lib_journal as J

    _pin_wall_clocks(monkeypatch)
    monkeypatch.setattr(_cctally_rederive, "_SCRATCH_REPLAY_COMMIT_EVERY", 4)
    mod = __import__("sys").modules["cctally"]
    _seed_block_cache(mod).close()
    readings = _block_readings(J)
    for record in readings[:5]:
        jr.append_record(record)
    jr.run_stats_ingest(mode="authoritative")
    for record in readings[5:]:
        jr.append_record(record)
    share = _cctally_core.APP_DIR
    saved = tmp_path / "saved-share"
    _snapshot_dir(share, saved)
    real_replay = _cctally_rederive.replay_scratch_records
    real_engine = _cctally_rederive.BlockTotalsEngine
    outcomes = {}
    for mode in ("engine", "off"):
        replays: list = []
        engines: list = []

        def spy(*args, **kwargs):
            events = real_replay(*args, **kwargs)
            replays.append(json.dumps(events, sort_keys=True))
            return events

        monkeypatch.setattr(_cctally_rederive, "replay_scratch_records", spy)
        if mode == "engine":
            monkeypatch.setattr(real_engine, "verify", True)

            def tracking(cache_conn):
                engines.append(real_engine(cache_conn))
                return engines[-1]

            monkeypatch.setattr(_cctally_rederive, "BlockTotalsEngine",
                                tracking)
        else:
            _restore_dir(share, saved)
            engine_off()
        before = _journal_bytes_of(share)
        assert ns["cmd_db_rebuild"](
            argparse.Namespace(db="stats", json=True)) == 0
        capsys.readouterr()
        outcomes[mode] = (replays, _journal_bytes_of(share),
                          _published(_cctally_core.DB_PATH))
        if mode == "engine":
            assert replays, "non-vacuity: no completion replay ran"
            assert engines and any(
                e.counts["extended"] + e.counts["cached"] for e in engines)
            assert _journal_bytes_of(share) != before, (
                "non-vacuity: completion appended nothing")
    assert outcomes["engine"][0] == outcomes["off"][0]
    assert outcomes["engine"][1] == outcomes["off"][1]
    assert outcomes["engine"][2] == outcomes["off"][2]


def test_historical_close_rederive_equal_with_and_without_engine(
        tmp_path, monkeypatch, capsys, engine_off):
    """#1003 T3: `db rederive` over #869's historical unpriced close (158
    retained entries, a card shipped after the close) previews the same
    plan and appends the same bytes with the engine (verification on) and
    with the slot forced empty. Detects an engine answer that changes a
    historical-close correction or its source hash."""
    import test_cost_repricing_869 as R

    mod = R.load_isolated_cctally_module(tmp_path, monkeypatch)
    # Imported after the isolated load, which re-executes the siblings.
    import _cctally_core
    import _cctally_rederive
    import _lib_pricing as pricing
    _pin_wall_clocks(monkeypatch)
    present = pricing.current_pricing_snapshot()
    R._record_unmarked_close(mod, monkeypatch, pricing, R._block_508_rows())
    carded = R._pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24")
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: carded)
    share = _cctally_core.APP_DIR
    saved = tmp_path / "saved-869"
    _snapshot_dir(share, saved)
    real_engine = _cctally_rederive.BlockTotalsEngine
    outcomes = {}
    for mode in ("engine", "off"):
        engines: list = []
        if mode == "engine":
            monkeypatch.setattr(real_engine, "verify", True)

            def tracking(cache_conn):
                engines.append(real_engine(cache_conn))
                return engines[-1]

            monkeypatch.setattr(_cctally_rederive, "BlockTotalsEngine",
                                tracking)
        else:
            _restore_dir(share, saved)
            engine_off()
        preview = R._rederive(mod, capsys, apply=False)
        preview.pop("generatedAt", None)
        applied = R._rederive(mod, capsys, apply=True)
        for key in ("generatedAt", "durationSeconds"):
            applied.pop(key, None)
        (applied.get("rebuild") or {}).pop("durationSeconds", None)
        outcomes[mode] = (preview, applied, _journal_bytes_of(share))
        if mode == "engine":
            assert engines and any(
                e.counts["extended"] + e.counts["cached"] for e in engines)
            assert R._kind_counts(
                preview, "five_hour_block_close")["supersede"] == 1
    engine, off = outcomes["engine"], outcomes["off"]
    assert engine[0] == off[0], "preview differs"
    assert {k for k in engine[1] if engine[1][k] != off[1].get(k)} == set()
    assert engine[2] == off[2], "appended journal bytes differ"
