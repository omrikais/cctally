"""#901 G1: recurring paths sort, group or materialize nothing history-sized.

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` I1, I3
and §6.2 G1. One test drives every recurring lifecycle the spec names — full and
idle dashboard builds with Codex active (each carrying the in-process doctor
gather), a CLI doctor gather, a conversation-sync pass, a retention visit, and
foreground Claude and Codex ``hook-tick``s — under ``capture_sql_plans()``, then
asserts that every W1-W3 call site executed and that every temp structure in
every captured plan is either bounded (``ALLOWLIST``) or on Part A's work list
(``PENDING``). The RED proof is this test on the unchanged baseline with an
empty ``PENDING``: it names the W1 window query and the W2 join/GROUP BY.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib
import io
import json
import pathlib
import re
import shutil
import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

REPO = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
CLAUDE_SEED = (REPO / "tests" / "fixtures" / "codex-parity" / "v1"
               / "claude-seed" / "11111111-1111-4111-8111-111111111111.jsonl")
NOW = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
RETENTION_NOW = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)

DASHBOARD_PHASES = (
    "dashboard-full-cold", "dashboard-full-delta", "dashboard-idle")

#: (label, phases, call site that must be on the stack, SQL pattern).
REQUIRED_SITES = (
    ("W1 doctor latest-per-identity quota probe",
     DASHBOARD_PHASES + ("doctor-cli",),
     "_cctally_doctor._load_codex_quota_observations_for_doctor",
     r"quota_window_snapshots"),
    ("W2 Codex conversation metadata",
     DASHBOARD_PHASES,
     "_cctally_dashboard_sources._codex_conversation_metadata",
     r"codex_session_entries"),
    ("W3a spend adoption on the dashboard ingest",
     ("dashboard-full-cold", "dashboard-full-delta"),
     "_cctally_cache.apply_codex_window_spend_adoption",
     r"FROM quota_window_snapshots"),
    ("W3a spend adoption on the foreground Codex hook (I3)",
     ("hook-codex",),
     "_cctally_cache.apply_codex_window_spend_adoption",
     r"FROM quota_window_snapshots"),
    ("W3b quota breakdown evidence",
     DASHBOARD_PHASES,
     "_cctally_dashboard_sources._capture_quota_breakdown_evidence",
     r"FROM quota_window_snapshots"),
    ("W3b stats relations digest",
     DASHBOARD_PHASES,
     "_lib_dashboard_sources._stats_relations_digest",
     r"FROM quota_percent_milestones"),
    ("W3b current-week snapshots",
     DASHBOARD_PHASES,
     "_cctally_forecast._fetch_current_week_snapshots",
     r"FROM weekly_usage_snapshots"),
    ("W3b pricing observed models",
     DASHBOARD_PHASES + ("doctor-cli",),
     "_cctally_pricing_check._pricing_observed_models",
     r"GROUP BY model"),
    ("retention visit",
     ("conversation-sync", "retention-visit"),
     "_lib_conversation_retention._maybe_prune_conversation_retention",
     r"."),
)


def _hook_args(source: str) -> argparse.Namespace:
    return argparse.Namespace(
        explain=False, foreground=True, no_oauth=True, throttle_seconds=None,
        event=None, mock_oauth_response=None, source=source,
    )


def _native_limits(value):
    """Give the corpus's windows their native lengths (330 -> 300, 10020 -> 10080).

    The parity corpus spells the weekly window as 10,020 minutes, and the
    dashboard's breakdown-evidence read (a required W3b site) only runs for a
    live 10,080-minute block, so the copy normalizes both lengths exactly as
    ``bin/build-e2e-fixtures.py`` does for the same corpus.
    """
    if isinstance(value, dict):
        for key, item in tuple(value.items()):
            if key == "window_minutes" and item == 10_020:
                value[key] = 10_080
            elif key == "window_minutes" and item == 330:
                value[key] = 300
            else:
                _native_limits(item)
    elif isinstance(value, list):
        for item in value:
            _native_limits(item)
    return value


def _copy_rollout(
    provider: pathlib.Path, name: str, *, weekly_percent: float = 42.0,
    captured_at: str = "2026-07-14T12:02:00Z",
) -> None:
    """Copy the corpus rollout, its one quota capture at ``captured_at``."""
    target = provider / "sessions" / "2026" / "07" / "16" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for line in (CORPUS / "modern-full.jsonl").read_text().splitlines():
        if not line:
            continue
        record = _native_limits(json.loads(line))
        if record.get("payload", {}).get("type") == "token_count":
            record["timestamp"] = captured_at
            limits = record["payload"]["info"]["rate_limits"]
            limits["secondary"]["used_percent"] = weekly_percent
        records.append(json.dumps(record, separators=(",", ":")) + "\n")
    target.write_text("".join(records))


def _run_lifecycles(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider = tmp_path / "provider"
    # Two captures of the live weekly window (40% then 42%), so the setup's
    # projection records crossings and the breakdown-evidence read has
    # milestones to correlate; a lone first capture records none.
    _copy_rollout(provider, "rollout-0.jsonl", weekly_percent=40.0,
                  captured_at="2026-07-14T11:02:00Z")
    _copy_rollout(provider, "rollout-a.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(provider))
    projects = tmp_path / "data" / ".claude" / "projects" / "-synthetic-g1"
    projects.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLAUDE_SEED, projects / CLAUDE_SEED.name)
    monkeypatch.setitem(
        ns, "_hook_tick_read_stdin_event",
        lambda: {"event": "Stop", "session_id": "g1",
                 "transcript_path": "", "cwd": ""},
    )
    quota = importlib.import_module("_cctally_quota")
    doctor = importlib.import_module("_cctally_doctor")
    dashboard = importlib.import_module("_cctally_dashboard")
    retention = importlib.import_module("_lib_conversation_retention")
    # The hook hands the daily whole-history pass to a detached worker; a test
    # must not spawn one, and the pass itself is not on the hook's own path.
    monkeypatch.setattr(
        quota, "_defer_codex_quota_verification", lambda: "throttled")
    monkeypatch.setattr(doctor, "_QUOTA_OBSERVATION_MEMO", {})
    # Setup, outside the capture: ingest the first two rollouts and project
    # their quota so the corpus's live weekly window (40% -> 42%, resetting
    # 2026-07-21T12Z) has milestones, which is what makes the breakdown-evidence
    # read run.
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    ns["reconcile_codex_quota_projection"](now=NOW)

    with guard.capture_sql_plans() as recorder:
        recorder.phase = "dashboard-full-cold"
        ns["_tui_build_snapshot"](
            now_utc=NOW, skip_sync=False, precompute_envelope=True,
            runtime_bind="127.0.0.1")
        _copy_rollout(provider, "rollout-b.jsonl")
        recorder.phase = "dashboard-full-delta"
        ns["_tui_build_snapshot"](
            now_utc=NOW, skip_sync=False, precompute_envelope=True,
            runtime_bind="127.0.0.1")
        recorder.phase = "dashboard-idle"
        ns["_tui_build_snapshot"](
            now_utc=NOW, skip_sync=False, precompute_envelope=True,
            runtime_bind="127.0.0.1")
        recorder.phase = "doctor-cli"
        with contextlib.redirect_stdout(io.StringIO()):
            ns["cmd_doctor"](
                argparse.Namespace(json=True, quiet=False, verbose=False))
        recorder.phase = "conversation-sync"
        dashboard._conversation_sync_pass()
        recorder.phase = "retention-visit"
        conn = ns["open_conversations_db"](attach_cache=False)
        try:
            retention._maybe_prune_conversation_retention(
                conn, now_utc=RETENTION_NOW, retention_days=30, force=True)
        finally:
            conn.close()
        recorder.phase = "hook-claude"
        ns["cmd_hook_tick"](_hook_args("claude"))
        _copy_rollout(provider, "rollout-c.jsonl")
        recorder.phase = "hook-codex"
        ns["cmd_hook_tick"](_hook_args("codex"))
    return recorder


def test_recurring_paths_build_no_history_sized_temp_structure(
    tmp_path, monkeypatch,
):
    recorder = _run_lifecycles(tmp_path, monkeypatch)
    statements = recorder.statements
    problems = []
    for label, phases, call_site, pattern in REQUIRED_SITES:
        if not any(
            statement.phase in phases
            and call_site in statement.call_sites
            and re.search(pattern, statement.sql)
            for statement in statements
        ):
            problems.append(
                f"required call site never executed: {label} "
                f"({call_site}) in phases {phases}")
    unexplained, stale = guard.classify(
        statements, allowlist=guard.ALLOWLIST, pending=guard.PENDING)
    if unexplained:
        problems.append(
            "temp structures with no ALLOWLIST bound and no PENDING owner:\n"
            + guard.format_findings(unexplained))
    if stale:
        problems.append(
            "PENDING rules that no longer match any finding (delete them in "
            "the task that removed the structure): " + ", ".join(stale))
    assert not problems, "\n\n".join(problems)


#: The physical-group shard's equality clause (`_cctally_quota`'s
#: ``group_filter`` branch): one shard per dirty window group.
GROUP_SHARD = re.compile(
    r"AND \(source_root_key=\? AND logical_limit_key=\? AND "
    r"observed_slot=\? AND window_minutes=\? AND unixepoch\(")
TEMP_STORE_CODE = {"FILE": 1, "MEMORY": 2}


def _sync_codex_without_reconcile(ns) -> None:
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, quota_reconcile="defer")
    finally:
        cache.close()


@pytest.mark.parametrize("temp_store", sorted(TEMP_STORE_CODE))
def test_a_stale_cursor_reconciliation_streams_every_shard(
    tmp_path, monkeypatch, temp_store,
):
    """Q11 (901-PA-001 a, spec §5.3a): a reconciliation whose last full pass is
    older than 24 hours sorts nothing. On the hook's path (``defer``) it hands
    the whole-history pass to the worker and still runs its bounded pass, one
    ``idx_qws_physical_group`` shard per dirty window group; inline it runs the
    whole-history pass. A shard used to re-sort its group's whole history,
    because the index stopped at the canonical reset while the shard keeps the
    loader's full ORDER BY. One window group is a week of activity, not a
    bound. Judged under the file AND the in-memory temp store, since moving a
    sort into memory never satisfies I1."""
    import _cctally_core

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    store = importlib.import_module("_cctally_store")
    quota = importlib.import_module("_cctally_quota")
    monkeypatch.setattr(store, "WRITER_TEMP_STORE", temp_store)
    deferred = []
    monkeypatch.setattr(quota, "_defer_codex_quota_verification",
                        lambda: deferred.append(1) or "throttled")
    provider = tmp_path / "provider"
    monkeypatch.setenv("CODEX_HOME", str(provider))
    now = dt.datetime.now(dt.timezone.utc)
    _copy_rollout(provider, "rollout-0.jsonl", weekly_percent=40.0,
                  captured_at="2026-07-14T11:02:00Z")
    _sync_codex_without_reconcile(ns)
    ns["reconcile_codex_quota_projection"](now=now)
    # The cursor goes stale twice over: new evidence the ledger owes a bounded
    # pass, and a last full pass older than the 24-hour verification interval.
    for name, percent, captured in (
            ("rollout-a.jsonl", 42.0, "2026-07-14T12:02:00Z"),
            ("rollout-b.jsonl", 43.0, "2026-07-14T12:02:00+00:00"),
            ("rollout-c.jsonl", 44.0, "2026-07-14T13:02:00Z")):
        _copy_rollout(provider, name, weekly_percent=percent,
                      captured_at=captured)
    _sync_codex_without_reconcile(ns)
    stats = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert stats.execute(
            "UPDATE quota_projection_ledger_state SET last_full_pass_at=?"
            " WHERE source='codex'",
            ((now - dt.timedelta(hours=25)).isoformat(),)).rowcount == 1
        stats.commit()
    finally:
        stats.close()

    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        recorder.phase = "stale-cursor-hook"
        ns["reconcile_codex_quota_projection"](now=now, full_pass="defer")
        recorder.phase = "stale-cursor-inline"
        ns["reconcile_codex_quota_projection"](now=now)
    loads = [
        statement for statement in recorder.statements
        if statement.call_sites[:2] == (
            "_cctally_quota._iter_shard_rows",
            "_cctally_quota.load_codex_quota_observations")
        and "_cctally_quota.reconcile_codex_quota_projection"
        in statement.call_sites
    ]
    shards = [s for s in loads if GROUP_SHARD.search(s.sql)]
    whole = [s for s in loads if s.phase == "stale-cursor-inline"
             and not GROUP_SHARD.search(s.sql)]
    assert deferred, "non-vacuity: the verification was due and deferred"
    assert shards and {s.phase for s in shards} == {"stale-cursor-hook"}, (
        "non-vacuity: the hook's bounded pass loaded per-group shards",
        [s.sql for s in loads])
    assert whole, "non-vacuity: the inline whole-history pass ran"
    assert {s.temp_store for s in shards + whole} == {
        TEMP_STORE_CODE[temp_store]}, "judged under the selected temp store"
    problems = [
        (statement.phase, statement.plan, statement.sql)
        for statement in shards + whole
        if any(marker in detail for detail in statement.plan
               for marker in guard.TEMP_MARKERS)
    ]
    assert problems == [], "\n".join(map(repr, problems))
    assert all(any("USING INDEX idx_qws_physical_group" in detail
                   for detail in shard.plan) for shard in shards), (
        [shard.plan for shard in shards])


LONG_SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LONG_SESSION_MESSAGES = 3000
LATEST_META_SITE = "_lib_conversation_query._session_latest_meta_map"
LATEST_META_INDEXES = (
    "idx_conv_session_latest_cwd", "idx_conv_session_latest_git_branch")


def _long_session_lines(first: int, count: int) -> str:
    """A Claude session whose metadata is sparse: every message carries a cwd,
    and only every 700th carries a git branch."""
    lines = []
    for i in range(first, first + count):
        ts = (dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
              + dt.timedelta(seconds=7 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        role = "user" if i % 2 == 0 else "assistant"
        message = {"content": [{"text": f"long session turn {i}",
                                "type": "text"}], "role": role}
        record = {"cwd": f"/synthetic/long/{i // 1000}", "message": message,
                  "sessionId": LONG_SESSION, "timestamp": ts, "type": role,
                  "uuid": f"long-u{i}"}
        if i % 700 == 0:
            record["gitBranch"] = f"branch-{i}"
        if i % 2:
            message.update(id=f"long-m{i}", model="claude-opus-4-8", usage={
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "input_tokens": 1, "output_tokens": 1})
            record.update(parentUuid=f"long-u{i - 1}", requestId=f"long-r{i}")
        lines.append(json.dumps(record) + "\n")
    return "".join(lines)


@pytest.mark.parametrize("temp_store", sorted(TEMP_STORE_CODE))
def test_a_long_lived_session_resolves_its_latest_metadata_without_a_sorter(
    tmp_path, monkeypatch, temp_store,
):
    """Q11 (901-PA-001 b, spec §5.3a): a conversation-sync pass that touches a
    session with thousands of messages resolves its latest cwd and git branch
    with two index-served LIMIT 1 lookups. The window query it replaces sorted
    the touched session's whole history (one session is not a bound), and its
    generated allowlist rule is gone. Judged under both temp stores."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    store = importlib.import_module("_cctally_store")
    monkeypatch.setattr(store, "WRITER_TEMP_STORE", temp_store)
    ns["CONFIG_PATH"].write_text('{"conversation":{"retention_days":0}}\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    transcript = (tmp_path / "data" / ".claude" / "projects" / "-g1-long"
                  / f"{LONG_SESSION}.jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_long_session_lines(0, LONG_SESSION_MESSAGES))
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id=?",
            (LONG_SESSION,)).fetchone()[0] == LONG_SESSION_MESSAGES, (
            "non-vacuity: the session is long-lived")
    finally:
        conn.close()
    with transcript.open("a") as fh:
        fh.write(_long_session_lines(LONG_SESSION_MESSAGES, 20))
    dashboard = importlib.import_module("_cctally_dashboard")
    with guard.capture_sql_plans(temp_store=temp_store) as recorder:
        recorder.phase = "conversation-sync-long-session"
        assert dashboard._conversation_sync_pass() == "ok"
    lookups = [s for s in recorder.statements
               if LATEST_META_SITE in s.call_sites
               and "FROM conversation_messages" in s.sql]
    assert lookups, "non-vacuity: the pass resolved the touched session"
    assert {s.temp_store for s in lookups} == {TEMP_STORE_CODE[temp_store]}
    problems = [
        (statement.plan, statement.sql) for statement in lookups
        if any(marker in detail for detail in statement.plan
               for marker in guard.TEMP_MARKERS)
    ]
    assert problems == [], "\n".join(map(repr, problems))
    used = {index for s in lookups for detail in s.plan
            for index in LATEST_META_INDEXES if index in detail}
    assert used == set(LATEST_META_INDEXES), [s.plan for s in lookups]
    assert all(s.sql.endswith("LIMIT 1") for s in lookups), [
        s.sql for s in lookups]


def _probe_history_sort(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT a FROM t ORDER BY b").fetchall()


def _probe_cursor_sort(conn: sqlite3.Connection) -> None:
    conn.cursor().execute("SELECT DISTINCT b FROM t").fetchall()


def test_the_classifier_flags_a_sort_and_honours_both_rule_lists(tmp_path):
    """Non-vacuity of the classifier itself, on a statement this module owns."""
    path = tmp_path / "probe.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE t(a, b)")
    seed.commit()
    seed.close()
    here = pathlib.Path(__file__).resolve().parent
    with guard.capture_sql_plans(roots=(here,)) as recorder:
        conn = sqlite3.connect(path)
        try:
            _probe_history_sort(conn)
            _probe_cursor_sort(conn)
        finally:
            conn.close()
    unexplained, stale = guard.classify(
        recorder.statements, allowlist=(), pending=())
    assert {finding.detail for finding in unexplained} == {
        "USE TEMP B-TREE FOR ORDER BY", "USE TEMP B-TREE FOR DISTINCT"}
    assert stale == []
    sort_rule = guard.TempStructureRule(
        rule_id="probe-sort",
        call_site="test_901_sql_plan_guard._probe_history_sort",
        sql_pattern=r"ORDER BY b$", detail_pattern=r"FOR ORDER BY",
        reason="probe")
    unexplained, stale = guard.classify(
        recorder.statements, allowlist=(sort_rule,), pending=())
    assert [finding.detail for finding in unexplained] == [
        "USE TEMP B-TREE FOR DISTINCT"]
    never = guard.TempStructureRule(
        rule_id="never-matches", call_site="nowhere.nothing",
        sql_pattern=r".", detail_pattern=r".", reason="probe")
    _unexplained, stale = guard.classify(
        recorder.statements, allowlist=(sort_rule,), pending=(never,))
    assert stale == ["never-matches"]


def _probe_partial_order_sort(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT a FROM t INDEXED BY t_b ORDER BY b, a").fetchall()


def test_the_classifier_flags_a_partial_order_sorter(tmp_path):
    """Amendment 1c: a tie-run sorter ("USE TEMP B-TREE FOR LAST n TERMS OF
    ORDER BY") is a temp structure like any other. An index that satisfies
    only a prefix of the ORDER BY still sorts every tie run, and a tie run has
    no history-independent bound."""
    path = tmp_path / "probe.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE t(a, b)")
    seed.execute("CREATE INDEX t_b ON t(b)")
    seed.commit()
    seed.close()
    here = pathlib.Path(__file__).resolve().parent
    with guard.capture_sql_plans(roots=(here,)) as recorder:
        conn = sqlite3.connect(path)
        try:
            _probe_partial_order_sort(conn)
        finally:
            conn.close()
    unexplained, _stale = guard.classify(
        recorder.statements, allowlist=(), pending=())
    details = [finding.detail for finding in unexplained]
    assert len(details) == 1, details
    assert re.fullmatch(
        r"USE TEMP B-TREE FOR (LAST (TERM|\d+ TERMS)|RIGHT PART) OF ORDER BY",
        details[0]), details


CYCLE_ROOT_A, CYCLE_ROOT_B, CYCLE_ROOT_C = ("1" * 32, "2" * 32, "3" * 32)
CYCLE_ACCOUNTS = (None, "a" * 32, "b" * 32, "unattributed")


def _seed_weekly_blocks(conn: sqlite3.Connection) -> None:
    """Weekly blocks on three roots: accounts, orphaned and live rows, and
    rows tied on one reset second in several spellings."""
    rows = []
    for root in (CYCLE_ROOT_A, CYCLE_ROOT_B, CYCLE_ROOT_C):
        for account in CYCLE_ACCOUNTS[1:]:
            for week, reset in enumerate((
                    "2026-07-21T12:00:00Z", "2026-07-21T14:00:00+02:00",
                    "2026-07-21T12:00:00.250Z", "2026-07-14T12:00:00Z",
                    "2026-07-07T12:00:00Z")):
                for slot in ("primary", "secondary"):
                    orphaned = "2026-07-15T00:00:00Z" if week % 2 else None
                    rows.append((root, slot, reset, orphaned, account,
                                 float(week)))
    conn.executemany(
        "INSERT INTO quota_window_blocks (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, limit_id,"
        " limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent,"
        " current_percent, last_source_path, last_line_offset, generation,"
        " orphaned_at, account_key)"
        " VALUES ('codex',?,'codex',?,10080,'codex',NULL,?,"
        " '2026-06-30T12:00:00Z','2026-06-30T12:00:00Z',"
        " '2026-06-30T12:00:00Z',1.0,?,'/p.jsonl',1,'g',?,?)",
        [(root, slot, reset, percent, orphaned, account)
         for root, slot, reset, orphaned, account, percent in rows])
    conn.commit()


def test_both_codex_cycle_ordering_branches_stream_without_a_sorter(
    tmp_path, monkeypatch,
):
    """Amendment 1c: ``_load_codex_cycles`` orders by one of two explicit tie
    orders, chosen by the NORMALIZED root count, each pinned to its own index.
    G1 drives both branches across root, account and orphan variants: every
    read uses its designated index and builds no temp structure at all."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_core

    conn = ns["open_db"]()
    try:
        _seed_weekly_blocks(conn)
    finally:
        conn.close()
    mh = importlib.import_module("_cctally_milestone_history")
    variants = []
    with guard.capture_sql_plans() as recorder:
        conn = sqlite3.connect(_cctally_core.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            for roots in ((CYCLE_ROOT_A,), (CYCLE_ROOT_B, "", CYCLE_ROOT_B),
                          (CYCLE_ROOT_A, CYCLE_ROOT_B),
                          (CYCLE_ROOT_C, CYCLE_ROOT_A, CYCLE_ROOT_B)):
                for account in CYCLE_ACCOUNTS:
                    for orphaned in (False, True):
                        recorder.phase = repr((roots, account, orphaned))
                        assert mh._load_codex_cycles(
                            conn, roots, include_orphaned=orphaned,
                            account_key=account, now_utc=NOW) is not None
                        single = len({root for root in roots if root}) == 1
                        variants.append((recorder.phase, single))
        finally:
            conn.close()
    reads = {
        statement.phase: statement for statement in recorder.statements
        if "_cctally_milestone_history._load_codex_cycles"
        in statement.call_sites and "FROM quota_window_blocks" in statement.sql
    }
    assert len(reads) == len(variants) == 32, sorted(reads)
    problems = []
    for phase, single in variants:
        statement = reads[phase]
        index = ("idx_quota_blocks_weekly_single_root_order" if single
                 else "idx_quota_blocks_weekly_reset_order")
        if f"INDEXED BY {index} " not in statement.sql or not any(
                index in detail for detail in statement.plan):
            problems.append((phase, "not on its designated index",
                             statement.sql, statement.plan))
    unexplained, _stale = guard.classify(
        list(reads.values()), allowlist=guard.ALLOWLIST, pending=())
    if unexplained:
        problems.append(guard.format_findings(unexplained))
    assert problems == [], "\n".join(map(repr, problems))


def test_capture_refuses_a_custom_connection_factory(tmp_path):
    with guard.capture_sql_plans():
        with pytest.raises(AssertionError, match="custom factory"):
            sqlite3.connect(tmp_path / "x.db", factory=sqlite3.Connection)


def test_no_pending_rule_remains():
    """Part A's work list is closed: every W1-W3 temp structure is gone.

    A new history-sized temp structure is an I1 violation to fix, not a rule to
    add here; a bounded one goes in ``ALLOWLIST`` with its bound.
    """
    assert guard.PENDING == ()
