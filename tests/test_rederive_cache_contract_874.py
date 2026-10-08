"""#874 S1: `db rederive` refuses an unusable cache with a typed gap.

The planner reads cache.db from a raw file snapshot, so nothing re-adds a
column a hand-built or truncated cache lacks. Its contract check exists to turn
such a cache into a typed `RederiveDataGap` (`missing-source`, exit 2) before
any planning or mutation. Before #874 a cache missing a column the planner
reads passed the check and failed in a reader instead, which the command
reported as an internal failure (`status: failed`, exit 3).

The declared contract is structural: every column any planner reader of a
table reads, required whenever the planner reads that table at all. These
tests pin that set independently of the implementation, then drop every
column (and every table) of the real cache schema in turn: a contracted one
must be refused at the contract check, before the first reader runs, and any
other must reproduce the intact cache's plan exactly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3

import pytest

from conftest import load_isolated_cctally_module

AT = ("2026-07-25T12:00:00Z", "2026-07-25T16:05:00Z")
RESETS = ("2026-07-25T15:00:00Z", "2026-07-25T20:00:00Z")
CLAUDE_MODEL = "claude-3-5-sonnet-20241022"
CODEX_MODEL = "gpt-6-sol"
CLAUDE_PATH = "/tmp/claude/projects/repo/874-session.jsonl"
CODEX_PATH = "/tmp/codex/874.jsonl"
ACCOUNT = "acct-874"

#: The reviewed read set of the claude-usage planner (#874 S1 spec §3). Pinned
#: here, not read from the implementation, so a contract that omits a read
#: column or names an unread one fails, whichever side drifted.
CLAUDE_ENTRY_CONTRACT = frozenset({
    "id", "source_path", "line_offset", "timestamp_utc", "model",
    "input_tokens", "output_tokens", "cache_create_tokens",
    "cache_read_tokens", "cache_create_1h_tokens", "cost_usd_raw", "speed",
    "account_key",
})
CLAUDE_FILE_CONTRACT = frozenset({"path", "session_id", "project_path"})
CODEX_ENTRY_CONTRACT = frozenset({
    "source_path", "line_offset", "timestamp_utc", "session_id", "model",
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens", "account_key",
})
TABLES = ("session_entries", "session_files", "codex_session_entries")
#: The payload fields that state the plan. An uncontracted column must leave
#: every one of them exactly as the intact cache produced it.
PLAN_FIELDS = (
    "status", "planHash", "batchId", "actionCounts",
    "actionCountsByEventKind", "preservedEventCount", "noOp", "dataGaps",
    "conflicts", "errors", "uncertainCostFactCount",
)
READERS = ("_validate_cache_rows", "_cache_fingerprint", "_joined_entries",
           "iter_entries", "iter_codex_entries")


def _args(*, yes=False, as_json=True):
    return argparse.Namespace(family="claude-usage", yes=yes, json=as_json)


def _seed_claude(mod):
    """Two retained entries and two observations that close one 5h block."""
    import _cctally_journal as runtime
    import _lib_journal as journal

    conn = mod.open_cache_db()
    try:
        conn.execute(
            "INSERT INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            "session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (CLAUDE_PATH, 200, 1, 200, AT[0], "session-874", "/repo"),
        )
        for offset, stamp in (
            (0, "2026-07-25T11:55:00+00:00"),
            (100, "2026-07-25T16:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, input_tokens, "
                "output_tokens, cache_create_tokens, cache_read_tokens, "
                "cache_create_1h_tokens, account_key, cost_usd_raw, speed) "
                "VALUES (?,?,?,?,1000000,0,0,0,0,?,NULL,'standard')",
                (CLAUDE_PATH, offset, stamp, CLAUDE_MODEL, ACCOUNT),
            )
        conn.commit()
    finally:
        conn.close()
    weekly_reset = int(
        dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc).timestamp()
    )
    for at, reset, weekly_pct in zip(AT, RESETS, (1.0, 2.0), strict=True):
        runtime.append_record(journal.make_obs(
            at=at,
            src="record-usage",
            provider="claude",
            account=ACCOUNT,
            payload={
                "captured_at": at,
                "source": "statusline",
                "weekly_percent": weekly_pct,
                "resets_at": weekly_reset,
                "five_hour_percent": 1.0,
                "five_hour_resets_at": reset,
            },
        ))
        assert runtime.run_stats_ingest(mode="authoritative").ran is True


def _seed_codex_budget(mod, monkeypatch, *, fallback):
    """A retained Codex entry and a 100 % budget crossing priced from it.

    The crossing's `_pricing` provenance is what makes the planner read the
    Codex cache at all. With ``fallback`` the model has no card at fire and
    regains it before planning, so the planner re-prices the crossing through
    `iter_codex_entries` (#869); without it the crossing is directly priced
    and the planner keeps it without that read.
    """
    import _cctally_alerts
    import _cctally_cache as cache
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    # The crossing fires a budget alert; never spawn the host's notifier.
    def no_notifier(_payload, **_kwargs):
        return "no_notifier:none"

    monkeypatch.setattr(
        _cctally_alerts, "_dispatch_alert_notification", no_notifier)
    monkeypatch.setitem(
        mod.__dict__, "_dispatch_alert_notification", no_notifier)

    present = pricing.current_pricing_snapshot()
    if fallback:
        cards = dict(present.codex_pricing)
        cards.pop(CODEX_MODEL)
        no_card = pricing.PricingSnapshot(
            snapshot_date="2026-09-22",
            claude_pricing=present.claude_pricing,
            codex_pricing=cards,
            aliases=present.aliases,
            tier_thresholds=present.tier_thresholds,
            fallback_model=present.fallback_model,
            cache_write_1h_multiplier=present.cache_write_1h_multiplier,
            fast_multipliers=present.fast_multipliers,
        )
        monkeypatch.setattr(
            pricing, "current_pricing_snapshot", lambda: no_card)

    conn = mod.open_cache_db()
    try:
        conn.execute(
            "INSERT INTO codex_session_files "
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
            "VALUES (?,?,?,?,?)",
            (CODEX_PATH, 100000, 1, 100000, AT[0]),
        )
        conn.execute(
            "INSERT INTO codex_session_entries "
            "(source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,total_tokens) "
            "VALUES (?,0,?,?,?,100000,0,0,0,100000)",
            (CODEX_PATH, "2026-07-25T11:55:00+00:00", "codex-874",
             CODEX_MODEL),
        )
        conn.commit()
    finally:
        conn.close()

    def retained_entries(start, end, *, skip_sync=False, account_key=None):
        db = mod.open_cache_db()
        try:
            return cache.iter_codex_entries(db, start, end,
                                            account_key=account_key)
        finally:
            db.close()

    monkeypatch.setattr(mod, "get_codex_entries", retained_entries)
    monkeypatch.setattr(mod, "_resolve_codex_speed", lambda _speed: "standard")
    mod.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "Etc/UTC"},
        "budget": {"codex": {
            "amount_usd": 0.125, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [100],
        }},
    }))
    fired = []

    def fire(ctx):
        fired.append(mod.maybe_record_codex_budget_milestone(
            {}, conn=ctx.conn, as_of=AT[0], alert_sink=ctx.pending_alerts,
            journal_ctx=ctx,
        ))

    assert runtime.run_stats_ingest(
        mode="authoritative", codex_apply=fire,
    ).ran is True
    assert fired == [1]
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: present)


def _settle(path: pathlib.Path):
    """Fold any WAL into the main file so a byte copy is the whole database."""
    conn = sqlite3.connect(path)
    try:
        busy, _log, _done = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)


def _restore(pristine: pathlib.Path, path: pathlib.Path):
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(pristine, path)


def _columns(path: pathlib.Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _drop_column(path: pathlib.Path, table: str, column: str):
    """Rebuild ``table`` without ``column``, as a hand-built cache would be.

    `ALTER TABLE DROP COLUMN` refuses indexed and key columns, which are
    exactly the ones that matter here, so the table is copied instead. That
    also drops the table's indexes, constraints and triggers: this models a
    missing column, not every possible damaged schema.
    """
    kept = [name for name in _columns(path, table) if name != column]
    assert len(kept) == len(_columns(path, table)) - 1
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.executescript(
            f"CREATE TABLE _without_874 AS SELECT {', '.join(kept)} "
            f"FROM {table};"
            f"DROP TABLE {table};"
            f"ALTER TABLE _without_874 RENAME TO {table};"
        )
    finally:
        conn.close()
    _settle(path)


def _drop_table(path: pathlib.Path, table: str):
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
    finally:
        conn.close()
    _settle(path)


class _Readers:
    """Counts the planner's cache readers per run."""

    def __init__(self, monkeypatch):
        import _cctally_cache as cache
        import _cctally_rederive as rederive_cmd

        self.calls: dict[str, int] = {}
        for owner, name in (
            (rederive_cmd, "_validate_cache_rows"),
            (rederive_cmd, "_cache_fingerprint"),
            (rederive_cmd, "_joined_entries"),
            (cache, "iter_entries"),
            (cache, "iter_codex_entries"),
        ):
            original = getattr(owner, name)

            def counted(*args, _name=name, _original=original, **kwargs):
                self.calls[_name] = self.calls.get(_name, 0) + 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(owner, name, counted)


def _run(mod, capsys, readers, **kwargs):
    """One `db rederive` run, with its payload and the readers it reached."""
    readers.calls.clear()
    try:
        code = mod.cmd_db_rederive(_args(**kwargs))
    except Exception as exc:  # noqa: BLE001 - an escaping error is a defect
        capsys.readouterr()
        return {"raised": f"{type(exc).__name__}: {exc}",
                "calls": dict(readers.calls)}
    captured = capsys.readouterr()
    outcome = {"exit": code, "calls": dict(readers.calls)}
    if kwargs.get("as_json", True):
        payload = json.loads(captured.out)
        outcome["plan"] = {field: payload.get(field) for field in PLAN_FIELDS}
    else:
        outcome["stderr"] = captured.err
    return outcome


def _refused_at_contract(outcome, table, column=None) -> bool:
    """Exit 2 `missing-source` with the contract's own gap, before any reader."""
    if outcome.get("exit") != 2:
        return False
    plan = outcome.get("plan") or {}
    if plan.get("status") != "missing-source":
        return False
    if any(outcome["calls"].get(name, 0) for name in READERS):
        return False
    for gap in plan.get("dataGaps") or ():
        if column is None and gap == f"missing cache.db table {table}":
            return True
        prefix = f"missing cache.db {table} column(s): "
        if column is not None and gap.startswith(prefix):
            if column in [part.strip() for part in
                          gap[len(prefix):].split(",")]:
                return True
    return False


def _same_plan(outcome, baseline) -> bool:
    return (
        outcome.get("exit") == baseline["exit"]
        and outcome.get("plan") == baseline["plan"]
    )


def _report(bad):
    return "\n" + "\n".join(
        f"{key}: {value}" for key, value in sorted(bad.items()))


def _sweep(mod, capsys, readers, path, pristine, contract, tables, baseline,
           refused_tables):
    """Drop each column and each table of ``tables``; return the violations."""
    bad = {}
    for table in tables:
        for column in _columns(pristine, table):
            _restore(pristine, path)
            _drop_column(path, table, column)
            outcome = _run(mod, capsys, readers)
            if column in contract.get(table, frozenset()):
                ok = _refused_at_contract(outcome, table, column)
            else:
                ok = _same_plan(outcome, baseline)
            if not ok:
                bad[(table, column)] = outcome
        _restore(pristine, path)
        _drop_table(path, table)
        outcome = _run(mod, capsys, readers)
        if table in refused_tables:
            ok = _refused_at_contract(outcome, table)
        else:
            ok = _same_plan(outcome, baseline)
        if not ok:
            bad[(table, "<table>")] = outcome
    _restore(pristine, path)
    return bad


def _pristine(mod, tmp_path):
    import _cctally_core as core

    path = pathlib.Path(core.CACHE_DB_PATH)
    _settle(path)
    pristine = tmp_path / "pristine-cache.db"
    shutil.copyfile(path, pristine)
    return path, pristine


def _journal_bytes(mod):
    return {
        item.name: item.read_bytes()
        for item in sorted(mod.JOURNAL_DIR.glob("*.jsonl"))
    }


def test_declared_contract_is_the_reviewed_read_set():
    import _lib_rederive as rederive

    assert rederive._SESSION_ENTRY_COLUMNS == CLAUDE_ENTRY_CONTRACT
    assert rederive._SESSION_FILE_COLUMNS == CLAUDE_FILE_CONTRACT
    assert getattr(
        rederive, "_CODEX_SESSION_ENTRY_COLUMNS", None
    ) == CODEX_ENTRY_CONTRACT


def test_every_claude_cache_column_is_contracted_or_unread(
    tmp_path, monkeypatch, capsys,
):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    _seed_claude(mod)
    path, pristine = _pristine(mod, tmp_path)
    readers = _Readers(monkeypatch)

    # Non-vacuity: the intact cache plans normally and reaches every Claude
    # reader, and (no Codex provenance) never the Codex one.
    baseline = _run(mod, capsys, readers)
    assert baseline["exit"] == 0, baseline
    assert baseline["plan"]["status"] in ("preview", "no-op"), baseline
    for name in ("_validate_cache_rows", "_cache_fingerprint",
                 "_joined_entries", "iter_entries"):
        assert baseline["calls"].get(name, 0) >= 1, baseline
    assert baseline["calls"].get("iter_codex_entries", 0) == 0, baseline

    # Without Codex provenance the planner never reads the Codex table, so no
    # Codex column, and not the table itself, may refuse this journal.
    bad = _sweep(
        mod, capsys, readers, path, pristine,
        {"session_entries": CLAUDE_ENTRY_CONTRACT,
         "session_files": CLAUDE_FILE_CONTRACT},
        TABLES, baseline,
        refused_tables={"session_entries", "session_files"},
    )
    assert not bad, _report(bad)


@pytest.mark.parametrize("fallback", [True, False], ids=["fallback", "direct"])
def test_every_codex_cache_column_is_contracted_or_unread(
    tmp_path, monkeypatch, capsys, fallback,
):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    _seed_claude(mod)
    _seed_codex_budget(mod, monkeypatch, fallback=fallback)
    capsys.readouterr()
    path, pristine = _pristine(mod, tmp_path)
    readers = _Readers(monkeypatch)

    baseline = _run(mod, capsys, readers)
    assert baseline["exit"] == 0, baseline
    assert baseline["plan"]["status"] in ("preview", "no-op"), baseline
    for name in ("_validate_cache_rows", "_cache_fingerprint"):
        assert baseline["calls"].get(name, 0) >= 1, baseline
    # The fallback crossing is re-priced from the cache; the directly priced
    # one is kept without that read, so the contract is structural there: a
    # column only the re-pricing reader reads is still required (spec §4).
    if fallback:
        assert baseline["calls"].get("iter_codex_entries", 0) >= 1, baseline
    else:
        assert baseline["calls"].get("iter_codex_entries", 0) == 0, baseline

    bad = _sweep(
        mod, capsys, readers, path, pristine,
        {"codex_session_entries": CODEX_ENTRY_CONTRACT},
        ("codex_session_entries",), baseline,
        refused_tables={"codex_session_entries"},
    )
    assert not bad, _report(bad)


def test_malformed_cache_refuses_on_every_lifecycle_path(
    tmp_path, monkeypatch, capsys,
):
    """Apply, text mode and interrupted-batch recovery all refuse first."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_journal as journal

    _seed_claude(mod)
    _seed_codex_budget(mod, monkeypatch, fallback=True)
    capsys.readouterr()
    path, pristine = _pristine(mod, tmp_path)
    readers = _Readers(monkeypatch)

    preview = _run(mod, capsys, readers)
    assert preview["plan"]["status"] == "preview", preview
    assert preview["plan"]["noOp"] is False, preview
    assert all(preview["calls"].get(name, 0) >= 1 for name in (
        "_validate_cache_rows", "_cache_fingerprint", "iter_codex_entries",
    )), preview
    before = _journal_bytes(mod)

    bad = {}
    for table, column in (
        ("session_entries", "line_offset"),
        ("session_entries", "speed"),
        ("codex_session_entries", "model"),
        ("codex_session_entries", "total_tokens"),
    ):
        _restore(pristine, path)
        _drop_column(path, table, column)
        outcome = _run(mod, capsys, readers, yes=True)
        if not _refused_at_contract(outcome, table, column):
            bad[("apply", table, column)] = outcome
    for yes in (False, True):
        _restore(pristine, path)
        _drop_column(path, "session_entries", "line_offset")
        outcome = _run(mod, capsys, readers, yes=yes, as_json=False)
        if not (
            outcome.get("exit") == 2
            and "db rederive missing source" in outcome["stderr"]
            and "line_offset" in outcome["stderr"]
            and not any(outcome["calls"].get(name, 0) for name in READERS)
        ):
            bad[("text", "yes" if yes else "preview")] = outcome
    assert not bad, _report(bad)
    assert _journal_bytes(mod) == before

    # An interrupted correction batch: every line but its commit. Recovery
    # re-plans first, so a malformed cache is refused before the repair.
    _restore(pristine, path)
    plan = mod.preview_db_rederive("claude-usage")
    import _cctally_journal as runtime
    records = journal.make_correction_batch(
        batch_id=preview["plan"]["batchId"],
        family="claude-usage",
        at=plan.generated_at,
        actions=plan.plan.to_correction_actions(),
    )
    for record in records[:-1]:
        runtime.append_record(record)
    interrupted = _journal_bytes(mod)
    _drop_column(path, "session_entries", "line_offset")
    refused = _run(mod, capsys, readers, yes=True)
    assert _refused_at_contract(refused, "session_entries", "line_offset"), (
        refused)
    assert _journal_bytes(mod) == interrupted

    # Non-vacuity: the same journal on the intact cache really recovers.
    _restore(pristine, path)
    recovered = _run(mod, capsys, readers, yes=True)
    assert recovered["exit"] == 0, recovered
    assert recovered["plan"]["status"] == "recovered", recovered


def _add_unusable_virtual_table(path: pathlib.Path):
    """Declare a virtual table whose module this SQLite build lacks.

    A cache.db last written by an FTS5-capable build keeps its
    `conversation_fts` virtual tables; on a build without FTS5 any statement
    that touches one raises `no such module`. The runner's SQLite has FTS5,
    so an unregistered module name stands in for it.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
            "VALUES ('table', 'legacy_fts_874', 'legacy_fts_874', 0, "
            "'CREATE VIRTUAL TABLE legacy_fts_874 USING missing_module_874(body)')"
        )
        conn.commit()
        conn.execute("PRAGMA writable_schema=OFF")
    finally:
        conn.close()
    _settle(path)


def test_contract_reads_only_the_tables_it_names(
    tmp_path, monkeypatch, capsys,
):
    """An unusable virtual table elsewhere in cache.db is not the planner's."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    _seed_claude(mod)
    path, pristine = _pristine(mod, tmp_path)
    readers = _Readers(monkeypatch)
    baseline = _run(mod, capsys, readers)
    assert baseline["exit"] == 0, baseline

    _add_unusable_virtual_table(path)
    with_vtable = _run(mod, capsys, readers)
    assert _same_plan(with_vtable, baseline), with_vtable

    _drop_column(path, "session_entries", "line_offset")
    refused = _run(mod, capsys, readers)
    assert _refused_at_contract(refused, "session_entries", "line_offset"), (
        refused)
