"""Issue #394 Task A: correction-triggered rebuild orchestration."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

import _lib_journal as journal_lib
from conftest import load_script, redirect_paths


AT = "2026-07-25T12:00:00Z"
FIXED = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
CCTALLY = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _siblings():
    import _cctally_core
    import _cctally_journal
    import _lib_journal

    return _cctally_core, _cctally_journal, _lib_journal


def _snapshot_payload(percent: float) -> dict:
    return {
        "kind": "snapshot_accept",
        "captured_at_utc": AT,
        "week_start_date": "2026-07-20",
        "week_end_date": "2026-07-27",
        "week_start_at": "2026-07-20T00:00:00+00:00",
        "week_end_at": "2026-07-27T00:00:00+00:00",
        "weekly_percent": percent,
        "source": "test",
        "payload_json": "{}",
        "account_key": "unattributed",
    }


def _strand_completed_correction(jr, journal):
    base = journal.make_evt(
        kind="snapshot_accept",
        id="sa:394",
        at=AT,
        payload={
            key: value
            for key, value in _snapshot_payload(10.0).items()
            if key != "kind"
        },
    )
    jr.append_record(base, now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    cursor_before = None
    conn = jr._cctally_core.open_db()
    try:
        cursor_before = jr._read_cursor(conn)
    finally:
        conn.close()

    correction = journal.make_correction_batch(
        batch_id="batch:394",
        family="claude-usage",
        at=AT,
        actions=[
            {
                "action": "replace",
                "id": "sa:394",
                "rev": 1,
                "at": AT,
                "payload": _snapshot_payload(20.0),
            }
        ],
    )
    for record in correction:
        jr.append_record(record, now_utc=FIXED)
    return cursor_before, jr.journal_high_water()


def _live_state(core, jr):
    conn = core.open_db()
    try:
        row = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:394'"
        ).fetchone()
        return (
            None if row is None else float(row[0]),
            jr._read_cursor(conn),
            conn.execute("PRAGMA integrity_check").fetchone()[0],
        )
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_completed_correction_self_heals_in_one_cycle(ns, mode):
    core, jr, journal = _siblings()
    cursor_before, correction_high_water = _strand_completed_correction(jr, journal)

    result = jr.run_stats_ingest(mode=mode)

    assert result.ran is True
    assert result.error is None
    conn = core.open_db()
    try:
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:394'"
        ).fetchone()[0] == 20.0
        assert jr._read_cursor(conn) == correction_high_water
        assert jr._read_cursor(conn) != cursor_before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_rebuild_is_pinned_to_commit_then_retry_consumes_later_bytes(
    ns, monkeypatch
):
    core, jr, journal = _siblings()
    _cursor_before, correction_high_water = _strand_completed_correction(
        jr, journal
    )
    later = journal.make_evt(
        kind="snapshot_accept",
        id="sa:394:later",
        at=AT,
        payload={
            key: value
            for key, value in _snapshot_payload(30.0).items()
            if key != "kind"
        },
    )
    jr.append_record(later, now_utc=FIXED)
    later_high_water = jr.journal_high_water()
    alerts_path = core.APP_DIR / "alerts.log"
    alerts_before = alerts_path.read_bytes() if alerts_path.exists() else b""
    observed = {}
    real_rebuild = jr.rebuild_stats_index

    def _observed_rebuild(*args, **kwargs):
        observed["argument"] = kwargs.get("high_water")
        result = real_rebuild(*args, **kwargs)
        conn = sqlite3.connect(core.DB_PATH)
        try:
            observed["cursor_after_rebuild"] = tuple(
                conn.execute(
                    "SELECT segment, offset FROM journal_cursor WHERE id=1"
                ).fetchone()
            )
            observed["later_rows_after_rebuild"] = conn.execute(
                "SELECT COUNT(*) FROM weekly_usage_snapshots "
                "WHERE journal_id = 'sa:394:later'"
            ).fetchone()[0]
        finally:
            conn.close()
        return result

    monkeypatch.setattr(jr, "rebuild_stats_index", _observed_rebuild)
    result = jr.run_stats_ingest(mode="authoritative")

    assert result.error is None
    assert observed == {
        "argument": correction_high_water,
        "cursor_after_rebuild": correction_high_water,
        "later_rows_after_rebuild": 0,
    }
    conn = core.open_db()
    try:
        assert jr._read_cursor(conn) == later_high_water
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:394'"
        ).fetchone()[0] == 20.0
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:394:later'"
        ).fetchone()[0] == 30.0
    finally:
        conn.close()
    assert (alerts_path.read_bytes() if alerts_path.exists() else b"") == alerts_before


def test_recovery_releases_shared_then_takes_exclusive_in_total_order(
    ns, monkeypatch
):
    _core, jr, journal = _siblings()
    _strand_completed_correction(jr, journal)
    trace = []

    def _wrap(name):
        original = getattr(jr, name)

        def _call(*args, **kwargs):
            trace.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(jr, name, _call)

    for name in (
        "_acquire_maintenance_shared",
        "_acquire_maintenance_exclusive",
        "_acquire_ingest_lock",
        "_release_ingest_lock",
        "_release_maintenance_shared",
    ):
        _wrap(name)

    result = jr.run_stats_ingest(mode="authoritative")

    assert result.error is None
    exclusive = trace.index("_acquire_maintenance_exclusive")
    assert trace[exclusive - 2 : exclusive] == [
        "_release_ingest_lock",
        "_release_maintenance_shared",
    ]
    assert trace[exclusive : exclusive + 2] == [
        "_acquire_maintenance_exclusive",
        "_acquire_ingest_lock",
    ]
    assert trace.count("_acquire_maintenance_exclusive") == 1


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_recovery_lock_contention_is_actionable_and_non_mutating(
    ns, monkeypatch, mode
):
    core, jr, journal = _siblings()
    cursor_before, _commit = _strand_completed_correction(jr, journal)
    monkeypatch.setattr(
        jr, "_acquire_maintenance_exclusive", lambda *_args, **_kwargs: None
    )

    if mode == "authoritative":
        with pytest.raises(
            jr.CorrectionRecoveryError, match="cctally db rebuild --db stats"
        ):
            jr.run_stats_ingest(mode=mode)
    else:
        result = jr.run_stats_ingest(mode=mode)
        assert isinstance(result.error, jr.CorrectionRecoveryError)
        assert "cctally db rebuild --db stats" in str(result.error)

    assert _live_state(core, jr) == (10.0, cursor_before, "ok")


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_locked_revalidation_is_actionable_and_non_mutating(
    ns, monkeypatch, mode
):
    core, jr, journal = _siblings()
    cursor_before, _commit = _strand_completed_correction(jr, journal)

    def _locked(_signal):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(jr, "_correction_index_converged", _locked)

    if mode == "authoritative":
        with pytest.raises(
            jr.CorrectionRecoveryError, match="cctally db rebuild --db stats"
        ):
            jr.run_stats_ingest(mode=mode)
    else:
        result = jr.run_stats_ingest(mode=mode)
        assert isinstance(result.error, jr.CorrectionRecoveryError)
        assert "cctally db rebuild --db stats" in str(result.error)

    assert _live_state(core, jr) == (10.0, cursor_before, "ok")
    assert jr._correction_scratch_mains() == set()


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_lower_revision_live_conflict_never_enters_recovery(
    ns, monkeypatch, mode
):
    core, jr, journal = _siblings()
    conn = core.open_db()
    try:
        current = journal.make_evt(
            kind="snapshot_accept",
            id="sa:394:stale-live",
            at=AT,
            payload={
                key: value
                for key, value in _snapshot_payload(20.0).items()
                if key != "kind"
            },
        )
        selected = journal.resolve_effective_events([current]).by_id[current["id"]]
        jr._insert_effective_metadata(conn, selected)
        conn.execute(
            "UPDATE journal_effective_events SET rev = 1, batch_id = ? "
            "WHERE event_id = ?",
            ("batch:already-applied", current["id"]),
        )
        conn.commit()
        stale = journal.make_evt(
            kind="snapshot_accept",
            id=current["id"],
            at=AT,
            payload={
                key: value
                for key, value in _snapshot_payload(10.0).items()
                if key != "kind"
            },
        )
        with pytest.raises(jr.CorrectionRebuildRequired) as caught:
            jr._classify_live_effective_event(conn, stale)
    finally:
        conn.close()

    monkeypatch.setattr(
        jr,
        "_run_stats_ingest_once",
        lambda **_kwargs: (_ for _ in ()).throw(caught.value),
    )
    monkeypatch.setattr(
        jr,
        "_recover_completed_correction",
        lambda *_args, **_kwargs: pytest.fail(
            "lower-revision live conflict entered recovery"
        ),
    )

    with pytest.raises(jr.CorrectionRebuildRequired) as public:
        jr.run_stats_ingest(mode=mode)
    assert public.value is caught.value


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_caller_owned_connection_is_never_closed_or_replaced(ns, mode):
    core, jr, journal = _siblings()
    cursor_before, _commit = _strand_completed_correction(jr, journal)
    conn = core.open_db()
    try:
        with pytest.raises(
            jr.CorrectionRebuildRequired,
            match="caller-owned.*cctally db rebuild --db stats",
        ):
            jr.run_stats_ingest(mode=mode, conn=conn)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert jr._read_cursor(conn) == cursor_before
        assert conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:394'"
        ).fetchone()[0] == 10.0
    finally:
        conn.close()


def test_sibling_won_recovery_skips_redundant_publication(ns, monkeypatch):
    core, jr, journal = _siblings()
    _cursor_before, correction_high_water = _strand_completed_correction(
        jr, journal
    )
    real_acquire = jr._acquire_maintenance_exclusive
    real_rebuild = jr.rebuild_stats_index
    rebuild_calls = []
    sibling_ran = False

    def _counted_rebuild(*args, **kwargs):
        rebuild_calls.append(kwargs.get("high_water"))
        return real_rebuild(*args, **kwargs)

    def _sibling_then_acquire(mode, timeout_s):
        nonlocal sibling_ran
        if not sibling_ran:
            sibling_ran = True
            _counted_rebuild(
                context=jr.RebuildContext(trigger="test-fixture"),
                high_water=correction_high_water,
            )
        return real_acquire(mode, timeout_s)

    monkeypatch.setattr(jr, "rebuild_stats_index", _counted_rebuild)
    monkeypatch.setattr(
        jr, "_acquire_maintenance_exclusive", _sibling_then_acquire
    )

    result = jr.run_stats_ingest(mode="authoritative")

    assert result.error is None
    assert rebuild_calls == [correction_high_water]
    assert _live_state(core, jr) == (20.0, correction_high_water, "ok")


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_live_reader_refusal_names_holder_and_leaves_no_scratch(
    ns, mode
):
    core, jr, journal = _siblings()
    cursor_before, _commit = _strand_completed_correction(jr, journal)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sqlite3,sys;"
                "c=sqlite3.connect(sys.argv[1]);"
                "c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone();"
                "print('READY',flush=True);"
                "sys.stdin.read();c.close()"
            ),
            str(core.DB_PATH),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        if mode == "authoritative":
            with pytest.raises(jr.CorrectionRecoveryError) as caught:
                jr.run_stats_ingest(mode=mode)
            message = str(caught.value)
        else:
            result = jr.run_stats_ingest(mode=mode)
            message = str(result.error)
        assert "stop the dashboard or other process" in message
        assert "cctally db rebuild --db stats" in message
        assert _live_state(core, jr) == (10.0, cursor_before, "ok")
        assert jr._correction_scratch_mains() == set()
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()
        if holder.stderr is not None:
            holder.stderr.close()


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_tainted_structural_batch_ingests_without_correction_recovery(
    ns, monkeypatch, mode
):
    _core, jr, journal = _siblings()
    commit_without_begin = journal.make_correction_batch(
        batch_id="batch:invalid",
        family="claude-usage",
        at=AT,
        actions=[],
    )[-1]
    jr.append_record(commit_without_begin, now_utc=FIXED)
    monkeypatch.setattr(
        jr,
        "_recover_completed_correction",
        lambda *_args, **_kwargs: pytest.fail("non-correction entered recovery"),
    )

    result = jr.run_stats_ingest(mode=mode)

    assert result.error is None
    assert result.consumed == 1
    selection = journal.resolve_effective_events([commit_without_begin])
    assert [
        (violation.batch_id, violation.kind)
        for violation in selection.protocol_violations
    ] == [("batch:invalid", "commit_without_begin")]
    conn = jr._cctally_core.open_db()
    try:
        persisted = conn.execute(
            "SELECT batch_id, kind FROM journal_protocol_violations"
        ).fetchall()
        assert [tuple(row) for row in persisted] == [
            ("batch:invalid", "commit_without_begin")
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["authoritative", "opportunistic"])
def test_second_correction_signal_is_bounded_to_one_retry(
    ns, monkeypatch, mode
):
    _core, jr, _journal = _siblings()
    calls = []
    signal = jr.CorrectionRebuildRequired(
        "repeat",
        batch_id="batch:repeat",
        event_id="sa:repeat",
        high_water=("observations-2026-07.jsonl", 1),
        expected_metadata=(1, "active", "sha256:x", "batch:repeat"),
        recovery_eligible=True,
    )

    def _always_signal(**_kwargs):
        calls.append("attempt")
        raise signal

    monkeypatch.setattr(jr, "_run_stats_ingest_once", _always_signal)
    monkeypatch.setattr(
        jr,
        "_recover_completed_correction",
        lambda *_args, **_kwargs: calls.append("recovery"),
    )

    if mode == "authoritative":
        with pytest.raises(
            jr.CorrectionRecoveryError, match="single.*retry failed"
        ):
            jr.run_stats_ingest(mode=mode)
    else:
        result = jr.run_stats_ingest(mode=mode)
        assert isinstance(result.error, jr.CorrectionRecoveryError)
    assert calls == ["attempt", "recovery", "attempt"]


def _seed_rederive_source_case(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    source_path = "/tmp/claude/projects/repo/session.jsonl"
    cache = mod.open_cache_db()
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (source_path, 100, 1, 100, AT, "session-a", "/repo"),
    )
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            source_path,
            0,
            "2026-07-25T11:00:00+00:00",
            "claude-3-5-sonnet-20241022",
            0,
            0,
            100,
            0,
            40,
            "acct-a",
        ),
    )
    cache.commit()
    resets = int(
        dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    obs = journal_lib.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        account="acct-a",
        payload={
            "captured_at": AT,
            "source": "statusline",
            "weekly_percent": 10.0,
            "resets_at": resets,
        },
    )
    desired = mod.plan_claude_usage_rederive(
        [obs],
        cache_conn=cache,
        journal_high_water=("observations-2026-07.jsonl", 1),
    )
    cache.close()

    import _cctally_journal as runtime

    wrong_events = []
    for action in desired.actions:
        payload = dict(action.payload or {})
        if payload.get("kind") == "snapshot_accept":
            payload["weekly_percent"] = 99.0
        if payload.get("kind") == "weekly_cost_snapshot":
            payload["cost_usd"] += 5.0
        wrong_events.append(
            journal_lib.make_evt(
                kind=payload.pop("kind"),
                id=action.event_id,
                at=action.at,
                payload=payload,
            )
        )
    for record in [obs, *wrong_events]:
        runtime.append_record(record, now_utc=FIXED)
    runtime.rebuild_stats_index(context=runtime.RebuildContext(trigger="test-fixture"))
    return mod


def _strand_real_rederive_commit(tmp_path, monkeypatch):
    mod = _seed_rederive_source_case(tmp_path, monkeypatch)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(mod.APP_DIR),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CCTALLY_REDERIVE_TEST_MODE": "1",
            "CCTALLY_REDERIVE_TEST_CRASH_STAGE": "after-batch-commit",
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "CODEX_HOME": str(tmp_path / ".codex"),
            "NO_COLOR": "1",
            "TZ": "Etc/UTC",
        }
    )
    killed = subprocess.run(
        [
            sys.executable,
            str(CCTALLY),
            "db",
            "rederive",
            "--family",
            "claude-usage",
            "--yes",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert killed.returncode == -signal.SIGKILL, (killed.stdout, killed.stderr)

    import _cctally_journal as runtime

    high_water = runtime.journal_high_water()
    records = [
        journal_lib.decode_line(raw)
        for _segment, _offset, raw in runtime._read_range(None, high_water)
    ]
    selection = journal_lib.resolve_effective_events(records)
    assert len(selection.completed_batches) == 1
    batch_id = next(iter(selection.completed_batches))
    commit_high_water = runtime._correction_commit_high_water(
        batch_id, high_water
    )
    conn = mod.open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0] == 0
    finally:
        conn.close()
    env.pop("CCTALLY_REDERIVE_TEST_CRASH_STAGE")
    return mod, env, batch_id, commit_high_water


@pytest.mark.parametrize("path", ["authoritative", "statusline"])
def test_real_rederive_crash_converges_through_actual_ingest_paths(
    tmp_path, monkeypatch, path
):
    mod, env, batch_id, commit_high_water = _strand_real_rederive_commit(
        tmp_path, monkeypatch
    )
    assert commit_high_water is not None

    if path == "authoritative":
        command = [
            sys.executable,
            str(CCTALLY),
            "record-usage",
            "--percent",
            "12",
            "--resets-at",
            str(int(time.time()) + 3 * 86400),
        ]
    else:
        script = """
import importlib.machinery, importlib.util, json, sys, time
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("cctally", sys.argv[2])
spec = importlib.util.spec_from_loader("cctally", loader)
mod = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = mod
loader.exec_module(mod)
payload = {
    "session_id": "issue-394-statusline",
    "rate_limits": {
        "seven_day": {
            "used_percentage": 13.0,
            "resets_at": __import__("datetime").datetime.fromtimestamp(
                int(time.time()) + 3 * 86400,
                tz=__import__("datetime").timezone.utc,
            ).isoformat().replace("+00:00", "Z"),
        }
    },
}
parsed = mod._lib_statusline.parse_statusline_stdin(
    json.dumps(payload).encode()
)
mod._statusline_persist(parsed, sync_for_test=True)
"""
        command = [
            sys.executable,
            "-c",
            script,
            str(CCTALLY.parent),
            str(CCTALLY),
        ]

    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    import _cctally_journal as runtime

    final_high_water = runtime.journal_high_water()
    conn = mod.open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0] > 0
        cursor = runtime._read_cursor(conn)
        assert cursor[0] == commit_high_water[0] == final_high_water[0]
        assert commit_high_water[1] < cursor[1] <= final_high_water[1]
        expected_percent = 12.0 if path == "authoritative" else 13.0
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE weekly_percent = ?",
            (expected_percent,),
        ).fetchone()[0] > 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert final_high_water != commit_high_water
