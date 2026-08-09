"""Issue #374 — same-revision divergence produced by live writers.

Task 1 (RED reproductions). Each of the five emitter families enumerated in the
spec's §7 gets an evidenced disposition here: the append-then-abort retry shape
appends a SECOND, divergent line under the same natural-key evt id
(*reproduces*); the retry is byte-identical (*does not reproduce on main*); or —
for `weekly_cost_snapshot` — the reproduction is out of this file's reach and
the family is *contained by the write boundary with its root cause
unidentified*. A test proves what it drives; where it drives less than the
production shape, its header says so rather than claiming a negative.

The mechanism under test is the spec's §3 finding: ``append_record(evt)`` is
durable and runs BEFORE the transactional ``journal_id`` stamp, so a cycle that
aborts after the append leaves the line on disk with the row still unstamped and
eligible for re-harvest. If the retry re-derives an identity-bearing payload from
a moved clock (or moved DB state), the second line diverges from the first under
the same ``(id, rev)`` — which is exactly what wedges ``rebuild_stats_index``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3

import pytest

import _lib_journal as J
from conftest import load_script, redirect_paths


AT = "2026-07-25T15:00:00Z"
WINDOW_KEY = 987654
ACCOUNT = "unattributed"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# --------------------------------------------------------------------------
# journal inspection
# --------------------------------------------------------------------------

def _jr():
    import _cctally_journal

    return _cctally_journal


def _journal_lines():
    """Every decoded record in the journal, in canonical segment order."""
    jr = _jr()
    out = []
    for name in jr.list_segments():
        with open(jr._cctally_core.JOURNAL_DIR / name, "rb") as fh:
            for raw in fh:
                rec = J.decode_line(raw)
                if rec is not None:
                    out.append(rec)
    return out


def _same_rev_conflicts(records):
    """Group evt records by (id, rev) and return the ids with >1 distinct hash."""
    groups: dict = {}
    for rec in records:
        if rec.get("t") != "evt":
            continue
        key = (rec["id"], rec.get("rev", 0))
        groups.setdefault(key, set()).add(
            json.dumps(rec, sort_keys=True, separators=(",", ":"))
        )
    return {k for k, v in groups.items() if len(v) > 1}


def _evts(records, prefix):
    return [r for r in records
            if r.get("t") == "evt" and str(r.get("id", "")).startswith(prefix)]


# --------------------------------------------------------------------------
# cycle drivers — the production emit paths inside one BEGIN IMMEDIATE
# --------------------------------------------------------------------------

def _ctx(conn, **kwargs):
    return _jr().IngestContext(conn=conn, batch=[], **kwargs)


def _run_in_cycle(conn, body, *, abort=False):
    """Run ``body(ctx)`` inside one ``BEGIN IMMEDIATE``, optionally aborting
    before the commit exactly as a crashed/rolled-back cycle does."""
    ctx = _ctx(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = body(ctx)
        if abort:
            raise RuntimeError("forced abort")
        conn.commit()
        return result
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def _run_harvest(conn, *, abort=False):
    return _run_in_cycle(conn, lambda ctx: _jr()._harvest(ctx), abort=abort)


# --------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------

def _seed_closed_block(conn, *, window_key=None, last_updated=AT, cost=10.0,
                       models=("claude-opus-4",), commit=True):
    """Insert one CLOSED five_hour_blocks row (journal_id NULL) plus its rollup
    children — the shape 6b's close hook leaves for the harvest to journal."""
    wk = WINDOW_KEY if window_key is None else window_key
    conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
        " total_cost_usd, is_closed, created_at_utc, last_updated_at_utc, "
        " account_key) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?)",
        (wk, "2026-07-25T15:00:00Z", "2026-07-25T10:00:00Z",
         "2026-07-25T10:00:00Z", "2026-07-25T15:00:00Z", 50.0, cost,
         "2026-07-25T10:00:00Z", last_updated, ACCOUNT),
    )
    block_id = int(conn.execute(
        "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ? "
        "AND account_key = ?", (wk, ACCOUNT)).fetchone()[0])
    for model in models:
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, cost_usd, entry_count, "
            " account_key) VALUES (?,?,?,?,?,?)",
            (block_id, wk, model, cost, 1, ACCOUNT),
        )
    conn.execute(
        "INSERT INTO five_hour_block_projects "
        "(block_id, five_hour_window_key, project_path, cost_usd, entry_count, "
        " account_key) VALUES (?,?,?,?,?,?)",
        (block_id, wk, "/synthetic/repo", cost, 1, ACCOUNT),
    )
    if commit:
        conn.commit()
    return block_id


def _seed_snapshot(conn, *, journal_id, percent=10.0, source="test",
                   captured=AT, commit=True):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key, "
        " journal_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (captured, "2026-07-20", "2026-07-27",
         "2026-07-20T00:00:00+00:00", "2026-07-27T00:00:00+00:00",
         percent, source, "{}", ACCOUNT, journal_id),
    )
    if commit:
        conn.commit()


# --------------------------------------------------------------------------
# Family 2 — five_hour_block_close (harvest). Spec §7 Class C.
# --------------------------------------------------------------------------

def test_red_harvest_retry_after_rollback_appends_divergent_line(ns):
    """A cycle that appends its harvest evt then aborts leaves the row
    unstamped; the retry re-harvests with an advanced ``last_updated_at_utc``
    and appends a SECOND, divergent line under the same natural key."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, last_updated="2026-07-25T15:00:00Z")

        with pytest.raises(RuntimeError, match="forced abort"):
            _run_harvest(conn, abort=True)

        # The append is durable; the stamp is transactional and rolled back.
        assert len(_evts(_journal_lines(), "fhbc:")) == 1
        assert conn.execute(
            "SELECT journal_id FROM five_hour_blocks").fetchone()[0] is None

        # The live upsert moves last_updated_at_utc before the retry.
        conn.execute(
            "UPDATE five_hour_blocks SET last_updated_at_utc = ?",
            ("2026-07-25T15:00:30Z",))
        conn.commit()
        _run_harvest(conn)

        lines = _journal_lines()
        assert len(_evts(lines, "fhbc:")) == 2
        assert _same_rev_conflicts(lines) == {
            (f"fhbc:{ACCOUNT}:{WINDOW_KEY}", 0)
        }, "expected the retry to append a divergent same-revision line"
    finally:
        conn.close()


def test_red_harvest_retry_diverges_on_moved_rollup_totals(ns):
    """Class C is not only the volatile clock: a retry that processes a larger
    prefix also re-harvests moved parent totals and moved rollup children."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0, models=("claude-opus-4",))

        with pytest.raises(RuntimeError, match="forced abort"):
            _run_harvest(conn, abort=True)

        block_id = int(conn.execute(
            "SELECT id FROM five_hour_blocks").fetchone()[0])
        conn.execute(
            "UPDATE five_hour_blocks SET total_cost_usd = 12.5 WHERE id = ?",
            (block_id,))
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, cost_usd, entry_count, "
            " account_key) VALUES (?,?,?,?,?,?)",
            (block_id, WINDOW_KEY, "claude-sonnet-4", 2.5, 1, ACCOUNT))
        conn.commit()
        _run_harvest(conn)

        assert _same_rev_conflicts(_journal_lines()) == {
            (f"fhbc:{ACCOUNT}:{WINDOW_KEY}", 0)
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Issue #399 — production-shaped frozen-close crash/retry reproduction.
# --------------------------------------------------------------------------

def _write_claude_usage_line(
    path,
    *,
    message_id,
    timestamp,
    cwd,
    model,
    input_tokens,
    output_tokens,
    cache_creation_tokens=0,
    cache_read_tokens=0,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "sessionId": f"session-{message_id}",
        "cwd": cwd,
        "timestamp": timestamp,
        "requestId": f"request-{message_id}",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation_tokens,
                "cache_read_input_tokens": cache_read_tokens,
            },
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _record_args(*, percent, weekly_reset, five_hour_percent, five_hour_reset):
    return argparse.Namespace(
        percent=percent,
        resets_at=weekly_reset,
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=five_hour_reset,
        source="statusline",
    )


def _block_logical_shape(conn, window_key):
    parent = conn.execute(
        "SELECT five_hour_window_key, five_hour_resets_at, block_start_at, "
        "first_observed_at_utc, last_observed_at_utc, "
        "final_five_hour_percent, seven_day_pct_at_block_start, "
        "seven_day_pct_at_block_end, crossed_seven_day_reset, "
        "total_input_tokens, total_output_tokens, "
        "total_cache_create_tokens, total_cache_read_tokens, total_cost_usd, "
        "is_closed, created_at_utc, last_updated_at_utc, account_key, journal_id "
        "FROM five_hour_blocks "
        "WHERE five_hour_window_key = ? AND account_key = ?",
        (window_key, ACCOUNT),
    ).fetchone()
    models = conn.execute(
        "SELECT five_hour_window_key, model, input_tokens, output_tokens, "
        "cache_create_tokens, cache_read_tokens, cost_usd, entry_count, "
        "account_key FROM five_hour_block_models "
        "WHERE five_hour_window_key = ? AND account_key = ? ORDER BY model",
        (window_key, ACCOUNT),
    ).fetchall()
    projects = conn.execute(
        "SELECT five_hour_window_key, project_path, input_tokens, output_tokens, "
        "cache_create_tokens, cache_read_tokens, cost_usd, entry_count, "
        "account_key FROM five_hour_block_projects "
        "WHERE five_hour_window_key = ? AND account_key = ? ORDER BY project_path",
        (window_key, ACCOUNT),
    ).fetchall()
    return {
        "parent": None if parent is None else tuple(parent),
        "models": [tuple(row) for row in models],
        "projects": [tuple(row) for row in projects],
    }


def test_real_record_usage_retry_preserves_durable_frozen_block(
    ns, tmp_path, monkeypatch
):
    """#399 decisive regression: real ``cmd_record_usage`` observations close an older
    API-anchored block, the cycle loses its commit after durably appending the
    ``fhbc:`` event, and a retry sees a late-ingested cache row inside that
    already-ended block.

    The durable close fact must freeze the parent and both child sets. Current
    main replays the orphan close and then lets the reprocessed old observation
    overwrite that stamped closed row from the enlarged cache prefix, so live
    state diverges from an independent rebuild even though #374 prevents a
    second conflicting journal line.
    """
    import _cctally_record as record

    jr = _jr()
    clock = {"now": "2026-07-27T11:00:00Z"}
    monkeypatch.setattr(
        record,
        "_command_as_of",
        lambda: dt.datetime.fromisoformat(clock["now"].replace("Z", "+00:00")),
    )
    monkeypatch.setattr(record, "now_utc_iso", lambda value=None: clock["now"])

    projects = tmp_path / ".claude" / "projects"
    first_path = projects / "project-a" / "first.jsonl"
    _write_claude_usage_line(
        first_path,
        message_id="first",
        timestamp="2026-07-27T10:00:00.000Z",
        cwd="/work/project-a",
        model="claude-sonnet-4-20250514",
        input_tokens=100,
        output_tokens=10,
        cache_creation_tokens=20,
        cache_read_tokens=30,
    )

    weekly_reset = int(
        dt.datetime(2026, 8, 2, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    first_reset = int(
        dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    successor_reset = int(
        dt.datetime(2026, 7, 27, 17, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    third_reset = int(
        dt.datetime(2026, 7, 27, 22, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    first_window = first_reset // 600 * 600
    successor_window = successor_reset // 600 * 600
    third_window = third_reset // 600 * 600

    # Queue the first real command observation without allowing its opportunistic
    # ingest attempt to run. The authoritative post-reset observation then
    # processes both retained observations and naturally expires the block in
    # one production cycle.
    held = jr._acquire_ingest_lock("authoritative", 1.0)
    assert held is not None
    try:
        assert record.cmd_record_usage(
            _record_args(
                percent=30.0,
                weekly_reset=weekly_reset,
                five_hour_percent=40.0,
                five_hour_reset=first_reset,
            ),
            ingest_mode="opportunistic",
        ) == 0
    finally:
        jr._release_ingest_lock(held)

    clock["now"] = "2026-07-27T12:05:00Z"
    real_write_cursor = jr._write_cursor

    def lose_commit(conn, segment, offset):
        raise sqlite3.OperationalError("simulated lost close commit")

    monkeypatch.setattr(jr, "_write_cursor", lose_commit)
    with pytest.raises(sqlite3.OperationalError, match="lost close commit"):
        record.cmd_record_usage(
                _record_args(
                    percent=31.0,
                    weekly_reset=weekly_reset,
                    five_hour_percent=41.0,
                    five_hour_reset=first_reset,
                )
            )
    monkeypatch.setattr(jr, "_write_cursor", real_write_cursor)

    first_candidates = _evts(_journal_lines(), f"fhbc:{ACCOUNT}:{first_window}")
    assert len(first_candidates) == 1, "the aborted cycle must durably close once"
    frozen_payload = first_candidates[0]["payload"]
    assert frozen_payload["total_input_tokens"] == 100
    assert [row["model"] for row in frozen_payload["_models"]] == [
        "claude-sonnet-4-20250514"
    ]

    # This row arrives only after the durable close append. Its timestamp lies
    # inside the ended block, so a timestamp-only cache query would include it.
    _write_claude_usage_line(
        projects / "project-b" / "late.jsonl",
        message_id="late",
        timestamp="2026-07-27T10:30:00.000Z",
        cwd="/work/project-b",
        model="claude-opus-4-20250514",
        input_tokens=200,
        output_tokens=20,
    )

    clock["now"] = "2026-07-27T12:06:00Z"
    assert record.cmd_record_usage(
        _record_args(
            percent=32.0,
            weekly_reset=weekly_reset,
            five_hour_percent=6.0,
            five_hour_reset=successor_reset,
        )
    ) == 0

    candidates = _evts(_journal_lines(), f"fhbc:{ACCOUNT}:{first_window}")
    canonical = {
        json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        for candidate in candidates
    }
    content_hashes = {
        jr._lib_journal._sha256_canonical(candidate) for candidate in candidates
    }
    assert len(canonical) == 1, "every retry candidate must be byte-identical"
    assert len(content_hashes) == 1
    assert _same_rev_conflicts(_journal_lines()) == set()

    conn = jr._cctally_core.open_db()
    try:
        metadata = conn.execute(
            "SELECT rev, status, content_hash, batch_id, event_json "
            "FROM journal_effective_events WHERE event_id = ?",
            (f"fhbc:{ACCOUNT}:{first_window}",),
        ).fetchone()
        assert tuple(metadata[:4]) == (
            0,
            "active",
            next(iter(content_hashes)),
            None,
        )
        assert json.loads(metadata["event_json"]) == candidates[0]
        live_shape = _block_logical_shape(conn, first_window)
        assert live_shape["parent"][9] == frozen_payload["total_input_tokens"]
        assert [row[1] for row in live_shape["models"]] == [
            "claude-sonnet-4-20250514"
        ]
        successor = conn.execute(
            "SELECT is_closed, final_five_hour_percent "
            "FROM five_hour_blocks WHERE five_hour_window_key = ? "
            "AND account_key = ?",
            (successor_window, ACCOUNT),
        ).fetchone()
        assert tuple(successor) == (0, 6.0), "the open successor stays mutable"
    finally:
        conn.close()
    assert len(candidates) >= 2, "the lost-close retry must re-emit its duplicate"

    # Several later observations mutate only the open successor. The complete
    # deep conflict inventory must stay at its pre-loop baseline, not merely
    # happen to be empty immediately after one retry.
    conflict_baseline = _same_rev_conflicts(_journal_lines())
    for minute, weekly_pct, block_pct in (
        (7, 33.0, 7.0),
        (8, 34.0, 8.0),
        (9, 35.0, 9.0),
    ):
        clock["now"] = f"2026-07-27T12:{minute:02d}:00Z"
        assert record.cmd_record_usage(
            _record_args(
                percent=weekly_pct,
                weekly_reset=weekly_reset,
                five_hour_percent=block_pct,
                five_hour_reset=successor_reset,
            )
        ) == 0
        assert _same_rev_conflicts(_journal_lines()) == conflict_baseline

    conn = jr._cctally_core.open_db()
    try:
        assert _block_logical_shape(conn, first_window) == live_shape
        successor = conn.execute(
            "SELECT is_closed, final_five_hour_percent "
            "FROM five_hour_blocks WHERE five_hour_window_key = ? "
            "AND account_key = ?",
            (successor_window, ACCOUNT),
        ).fetchone()
        assert tuple(successor) == (0, 9.0)
    finally:
        conn.close()

    # A retained observation for a strictly newer key closes the successor even
    # before its natural reset. The close clock is that retained observation's
    # capture time, not the later ingest/retry wall clock.
    clock["now"] = "2026-07-27T16:55:00Z"
    assert record.cmd_record_usage(
        _record_args(
            percent=36.0,
            weekly_reset=weekly_reset,
            five_hour_percent=3.0,
            five_hour_reset=third_reset,
        )
    ) == 0
    successor_close = _evts(
        _journal_lines(), f"fhbc:{ACCOUNT}:{successor_window}"
    )
    assert len(successor_close) == 1
    assert successor_close[0]["at"] == clock["now"]
    assert successor_close[0]["payload"]["last_updated_at_utc"] == clock["now"]
    conn = jr._cctally_core.open_db()
    try:
        assert conn.execute(
            "SELECT is_closed FROM five_hour_blocks "
            "WHERE five_hour_window_key = ? AND account_key = ?",
            (successor_window, ACCOUNT),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT is_closed FROM five_hour_blocks "
            "WHERE five_hour_window_key = ? AND account_key = ?",
            (third_window, ACCOUNT),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    rebuilt_path = tmp_path / "rebuilt-399.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(rebuilt_path),
    )
    rebuilt = jr._cctally_core.open_db(_target_path=str(rebuilt_path))
    try:
        assert _block_logical_shape(rebuilt, first_window) == live_shape
    finally:
        rebuilt.close()


def test_rebuild_projection_waits_for_retained_successor_close(
    ns, monkeypatch
):
    """A rebuild after wall-clock expiry must not manufacture a close. The
    first retained successor observation owns the close clock and payload."""
    import _cctally_five_hour as five_hour

    jr = _jr()
    weekly_reset = int(
        dt.datetime(2026, 8, 2, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    first_reset = "2026-07-27T12:00:00+00:00"
    successor_reset = "2026-07-27T17:00:00+00:00"
    first_window = int(
        dt.datetime.fromisoformat(first_reset).timestamp()
    ) // 600 * 600

    def observation(*, at, weekly_pct, block_pct, block_reset):
        return J.make_obs(
            at=at,
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": weekly_pct,
                "resets_at": weekly_reset,
                "source": "statusline",
                "captured_at": at,
                "five_hour_percent": block_pct,
                "five_hour_resets_at": block_reset,
            },
        )

    jr.append_record(
        observation(
            at="2026-07-27T11:00:00Z",
            weekly_pct=30.0,
            block_pct=40.0,
            block_reset=first_reset,
        )
    )
    jr.run_stats_ingest(mode="authoritative")

    # Rebuild after wall-clock expiry. This pass may re-materialize the trailing
    # projection, but it cannot turn wall time into a durable close decision.
    monkeypatch.setattr(
        five_hour, "now_utc_iso", lambda: "2026-07-27T13:00:00Z"
    )
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = jr._cctally_core.open_db()
    try:
        projection = conn.execute(
            "SELECT is_closed, created_at_utc, last_updated_at_utc, journal_id "
            "FROM five_hour_blocks WHERE five_hour_window_key = ? "
            "AND account_key = ?",
            (first_window, ACCOUNT),
        ).fetchone()
        assert tuple(projection) == (
            0,
            "2026-07-27T11:00:00Z",
            "2026-07-27T11:00:00Z",
            None,
        )
    finally:
        conn.close()

    # The successor is retained before natural expiry, so its capture stamp is
    # the only valid close clock even though ingest happens after the rebuild.
    jr.append_record(
        observation(
            at="2026-07-27T11:30:00Z",
            weekly_pct=31.0,
            block_pct=5.0,
            block_reset=successor_reset,
        )
    )
    jr.run_stats_ingest(mode="authoritative")
    candidates = _evts(_journal_lines(), f"fhbc:{ACCOUNT}:{first_window}")
    assert len(candidates) == 1
    assert candidates[0]["at"] == "2026-07-27T11:30:00Z"
    assert (
        candidates[0]["payload"]["last_updated_at_utc"]
        == "2026-07-27T11:30:00Z"
    )


def test_block_close_replay_replaces_wrong_parent_orphan_children(ns):
    """The close payload owns child natural keys, even when stale rows point at
    a different parent id and would absorb INSERT OR IGNORE."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        block_id = _seed_closed_block(conn)
        spec = next(
            item for item in jr._HARVEST_SPECS
            if item.kind == "five_hour_block_close"
        )
        parent = conn.execute(
            "SELECT * FROM five_hour_blocks WHERE id = ?", (block_id,)
        ).fetchone()
        evt = jr._build_harvest_evt(_ctx(conn), spec, parent)
        evt["payload"]["_projects"] = []

        conn.execute("DELETE FROM five_hour_block_models")
        conn.execute("DELETE FROM five_hour_block_projects")
        wrong_parent = block_id + 999
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, cost_usd, entry_count, "
            " account_key) VALUES (?,?,?,?,?,?)",
            (wrong_parent, WINDOW_KEY, "claude-opus-4", 99.0, 99, ACCOUNT),
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, cost_usd, "
            " entry_count, account_key) VALUES (?,?,?,?,?,?)",
            (wrong_parent, WINDOW_KEY, "/stale/orphan", 99.0, 99, ACCOUNT),
        )
        conn.commit()

        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()

        models = conn.execute(
            "SELECT block_id, model, cost_usd, entry_count "
            "FROM five_hour_block_models"
        ).fetchall()
        assert [tuple(row) for row in models] == [
            (block_id, "claude-opus-4", 10.0, 1)
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_block_projects"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Family 3 — budget / codex_budget / project_budget (config reconcile). §7.3
# --------------------------------------------------------------------------

def test_red_budget_reconcile_retry_appends_divergent_line(ns, monkeypatch):
    """``_run_config_reconcile`` passes ``as_of=None``, so each reconcile stamps
    ``crossed_at_utc`` from the LIVE clock while the harvest id excludes it. The
    retry after an abort therefore appends a divergent ``bm:`` line."""
    import _cctally_milestones as m

    jr = _jr()
    conn = jr._cctally_core.open_db()
    clock = {"now": "2026-07-25T15:00:00Z"}
    monkeypatch.setattr(m, "now_utc_iso", lambda: clock["now"])

    def _reconcile_then_harvest(ctx):
        # The exact call `_reconcile_budget_milestones_on_set` makes on the
        # config-reconcile path: no `as_of`, so the wall clock lands on the row.
        m.insert_budget_milestone(
            conn,
            vendor="claude",
            period_start_at="2026-07-20T00:00:00+00:00",
            period="subscription-week",
            threshold=80,
            budget_usd=100.0,
            spent_usd=85.0,
            consumption_pct=85.0,
            commit=False,
            as_of=None,
            account_key="*",
        )
        jr._harvest(ctx)

    try:
        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _reconcile_then_harvest, abort=True)

        assert len(_evts(_journal_lines(), "bm:")) == 1

        # A later config write re-runs the same reconcile at its own moment.
        clock["now"] = "2026-07-25T15:00:45Z"
        _run_in_cycle(conn, _reconcile_then_harvest)

        lines = _journal_lines()
        assert len(_evts(lines, "bm:")) == 2
        assert _same_rev_conflicts(lines) == {
            ("bm:*:claude:2026-07-20T00:00:00+00:00:subscription-week:80", 0)
        }, "expected the reconcile retry to append a divergent same-revision line"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Family 1 — weekly_cost_snapshot (Model-A). Spec §7.1 / Class B.
# #410 Task A proved the producer cause end to end: milestone cost sync selected
# the latest usage row rather than the triggering observation's retained window.
# `test_weekly_cost_retained_window_410.py` drives the full pipeline over shifted
# anchors, cache growth, and a post-append/pre-commit abort. The two tests below
# remain narrow emitter-level non-vacuity guards; they are no longer presented
# as the only available disposition or as evidence that the root cause is
# unidentified.
# --------------------------------------------------------------------------

def test_red_weekly_cost_emitter_is_deterministic_for_fixed_inputs(ns):
    """Pins one narrow property: given the SAME ``range_end_iso``, ``cost_usd``
    and retained ``as_of``, a retry after an abort re-emits a byte-identical
    ``wcs:`` line — the emitter adds no wall clock of its own.

    ``range_end_iso`` and ``cost_usd`` arrive here as literals, so this remains
    a low-level guard. The #410 regression drives the whole ``cmd_sync_week``
    chain over retained and later windows."""
    import _cctally_milestones as m

    jr = _jr()
    conn = jr._cctally_core.open_db()

    def _emit(ctx):
        import datetime as _dt

        return m.insert_cost_snapshot(
            conn,
            week_start=_dt.date(2026, 7, 20),
            week_end=_dt.date(2026, 7, 27),
            week_start_at="2026-07-20T00:00:00+00:00",
            week_end_at="2026-07-27T00:00:00+00:00",
            range_start_iso="2026-07-20T00:00:00+00:00",
            range_end_iso="2026-07-25T15:00:00+00:00",
            cost_usd=12.5,
            mode="auto",
            project=None,
            commit=False,
            as_of=AT,                      # the retained capture time
            journal=(ctx, "o:deadbeef01234567"),
            account_key=ACCOUNT,
        )

    try:
        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _emit, abort=True)
        _run_in_cycle(conn, _emit)

        lines = _journal_lines()
        assert len(_evts(lines, "wcs:")) == 2, "both attempts appended"
        assert _same_rev_conflicts(lines) == set(), (
            "with every payload input held fixed, the retained as_of pins "
            "captured_at_utc and the evt `at`, so the retry is byte-identical "
            "(this says nothing about a retry whose range_end/cost moved)"
        )
    finally:
        conn.close()


def test_red_weekly_cost_would_diverge_without_the_retained_as_of(ns, monkeypatch):
    """Non-vacuity guard for the determinism test above: the very same emitter
    DOES diverge when ``as_of`` is absent and ``now_utc_iso()`` supplies the
    stamp — proving the wall-clock mechanism is real, and that the test above
    would have noticed a clock riding this payload. It does NOT establish that
    the retained ``as_of`` is the only way a ``wcs:`` retry can diverge;
    production shows otherwise (see the family header)."""
    import _cctally_milestones as m

    jr = _jr()
    conn = jr._cctally_core.open_db()
    clock = {"now": "2026-07-25T15:00:00Z"}
    monkeypatch.setattr(m, "now_utc_iso", lambda: clock["now"])

    def _emit(ctx):
        import datetime as _dt

        return m.insert_cost_snapshot(
            conn,
            week_start=_dt.date(2026, 7, 20),
            week_end=_dt.date(2026, 7, 27),
            week_start_at="2026-07-20T00:00:00+00:00",
            week_end_at="2026-07-27T00:00:00+00:00",
            range_start_iso="2026-07-20T00:00:00+00:00",
            range_end_iso="2026-07-25T15:00:00+00:00",
            cost_usd=12.5,
            mode="auto",
            project=None,
            commit=False,
            as_of=None,                    # the shape the budget reconcile uses
            journal=(ctx, "o:deadbeef01234567"),
            account_key=ACCOUNT,
        )

    try:
        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _emit, abort=True)
        clock["now"] = "2026-07-25T15:00:45Z"
        _run_in_cycle(conn, _emit)

        assert _same_rev_conflicts(_journal_lines()) == {
            ("wcs:o:deadbeef01234567:2026-07-20", 0)
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Family 4 — weekly_credit_effects (Model-A, effects-only). Spec §7.4.
# --------------------------------------------------------------------------

def test_red_credit_effects_retry_suppression_list(ns):
    """``wce:`` builds its suppression list from LIVE DB queries. An unchanged
    retry is byte-identical (negative); the list only moves when the queried
    state moves between attempts — recorded as a candidate, not an observed
    production defect."""
    jr = _jr()
    conn = jr._cctally_core.open_db()

    def _emit(ctx):
        doomed = [r[0] for r in conn.execute(
            "SELECT journal_id FROM weekly_usage_snapshots "
            "WHERE source = 'record-usage' AND journal_id IS NOT NULL "
            "ORDER BY journal_id").fetchall()]
        return jr.emit_model_a(
            ctx,
            kind="weekly_credit_effects",
            evt_id="wce:o:cafebabe01234567",
            table=None,
            columns={
                "suppression": doomed,
                "suppression_table": "weekly_usage_snapshots",
                "floor_suppression": [],
                "hwm_floor": {"week_start_date": "2026-07-20",
                              "weekly_percent": 31.0},
            },
            at=AT,
        )

    try:
        _seed_snapshot(conn, journal_id="sa:stale:0", source="record-usage")

        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _emit, abort=True)
        _run_in_cycle(conn, _emit)

        assert _same_rev_conflicts(_journal_lines()) == set(), (
            "weekly_credit_effects does NOT reproduce on main under an "
            "unchanged retry"
        )
    finally:
        conn.close()


def test_red_credit_effects_diverges_when_the_queried_state_moves(ns):
    """The candidate shape: a snapshot that appears between the aborted attempt
    and the retry widens the suppression list under the same ``wce:`` id."""
    jr = _jr()
    conn = jr._cctally_core.open_db()

    def _emit(ctx):
        doomed = [r[0] for r in conn.execute(
            "SELECT journal_id FROM weekly_usage_snapshots "
            "WHERE source = 'record-usage' AND journal_id IS NOT NULL "
            "ORDER BY journal_id").fetchall()]
        return jr.emit_model_a(
            ctx,
            kind="weekly_credit_effects",
            evt_id="wce:o:cafebabe01234567",
            table=None,
            columns={
                "suppression": doomed,
                "suppression_table": "weekly_usage_snapshots",
                "floor_suppression": [],
                "hwm_floor": {"week_start_date": "2026-07-20",
                              "weekly_percent": 31.0},
            },
            at=AT,
        )

    try:
        _seed_snapshot(conn, journal_id="sa:stale:0", source="record-usage")

        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _emit, abort=True)

        _seed_snapshot(conn, journal_id="sa:stale:1", source="record-usage")
        _run_in_cycle(conn, _emit)

        assert _same_rev_conflicts(_journal_lines()) == {
            ("wce:o:cafebabe01234567", 0)
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Family 5 — snapshot_accept (Model-A). Spec §7.5.
# EXPECTED NEGATIVE: the payload carries no wall clock.
# --------------------------------------------------------------------------

def test_red_snapshot_accept_retry_is_byte_identical(ns):
    """``snapshot_accept`` passes ``at=plan.captured_iso`` and carries no wall
    clock in its payload, so a retry re-emits byte-identically. Its observed
    production divergence was the one-time #341 account widening, not a
    recurring emitter defect."""
    jr = _jr()
    conn = jr._cctally_core.open_db()

    def _emit(ctx):
        return jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id="sa:o:0123456789abcdef",
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": AT,
                "week_start_date": "2026-07-20",
                "week_end_date": "2026-07-27",
                "week_start_at": "2026-07-20T00:00:00+00:00",
                "week_end_at": "2026-07-27T00:00:00+00:00",
                "weekly_percent": 42.0,
                "source": "record-usage",
                "payload_json": "{}",
                "account_key": ACCOUNT,
            },
            at=AT,
        )

    try:
        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _emit, abort=True)
        _run_in_cycle(conn, _emit)

        lines = _journal_lines()
        assert len(_evts(lines, "sa:")) == 2
        assert _same_rev_conflicts(lines) == set(), (
            "snapshot_accept does NOT reproduce on main"
        )
    finally:
        conn.close()


# ==========================================================================
# Task 3 — the write boundary: classify BEFORE append, converge on conflict
# ==========================================================================

_BLOCK_EVT_ID = f"fhbc:{ACCOUNT}:{WINDOW_KEY}"


def _block_row(conn, window_key=WINDOW_KEY):
    row = conn.execute(
        "SELECT total_cost_usd, journal_id, last_updated_at_utc "
        "FROM five_hour_blocks WHERE five_hour_window_key = ?",
        (window_key,)).fetchone()
    return None if row is None else tuple(row)


def _block_models(conn, window_key=WINDOW_KEY):
    return [tuple(r) for r in conn.execute(
        "SELECT model, cost_usd FROM five_hour_block_models "
        "WHERE five_hour_window_key = ? ORDER BY model", (window_key,))]


def _block_projects(conn, window_key=WINDOW_KEY):
    return [tuple(r) for r in conn.execute(
        "SELECT project_path, cost_usd FROM five_hour_block_projects "
        "WHERE five_hour_window_key = ? ORDER BY project_path", (window_key,))]


def _unstamp_block(conn, window_key=WINDOW_KEY):
    """The production shape a conflict arrives in: the journal already holds the
    effective event, but the physical row is unstamped (its stamp rolled back)
    and therefore eligible for re-harvest."""
    conn.execute(
        "UPDATE five_hour_blocks SET journal_id = NULL "
        "WHERE five_hour_window_key = ?", (window_key,))


def _drift_block(conn, *, cost, window_key=WINDOW_KEY):
    conn.execute(
        "UPDATE five_hour_blocks SET total_cost_usd = ? "
        "WHERE five_hour_window_key = ?", (cost, window_key))


def test_conflict_converges_row_and_children_then_stamps(ns):
    """A conflicting harvest emission appends NOTHING and converges the row to
    the ALREADY-JOURNALED event: parent fields updated, changed child values
    updated, stale extra children removed, missing children inserted.

    Asserting only the final journal_id would be insufficient — the fold
    appliers are INSERT OR IGNORE, so a no-op would pass such a test."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0, models=("claude-opus-4",))
        _run_harvest(conn)
        assert _block_row(conn)[1] == _BLOCK_EVT_ID
        before = len(_journal_lines())

        # The live row drifts away from the journaled event while unstamped.
        _unstamp_block(conn)
        _drift_block(conn, cost=99.0)
        conn.execute(
            "UPDATE five_hour_block_models SET cost_usd = 99.0 "
            "WHERE five_hour_window_key = ?", (WINDOW_KEY,))
        block_id = int(conn.execute(
            "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?",
            (WINDOW_KEY,)).fetchone()[0])
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, cost_usd, entry_count, "
            " account_key) VALUES (?,?,?,?,?,?)",
            (block_id, WINDOW_KEY, "claude-STALE", 5.0, 1, ACCOUNT))
        conn.execute(
            "DELETE FROM five_hour_block_projects "
            "WHERE five_hour_window_key = ?", (WINDOW_KEY,))
        conn.commit()

        _run_harvest(conn)

        assert len(_journal_lines()) == before, "conflict must append nothing"
        cost, journal_id, _updated = _block_row(conn)
        assert cost == 10.0, "parent field must converge to the journaled event"
        assert journal_id == _BLOCK_EVT_ID, "row must be stamped after convergence"
        assert _block_models(conn) == [("claude-opus-4", 10.0)], (
            "changed child value converged AND the stale extra child removed")
        assert _block_projects(conn) == [("/synthetic/repo", 10.0)], (
            "the missing child must be re-inserted from the journaled event")
    finally:
        conn.close()


def test_conflict_preserves_the_parent_rowid(ns):
    """Convergence must UPDATE the existing physical row, never delete and
    recreate it — child FKs point at that rowid."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
        rowid = int(conn.execute("SELECT id FROM five_hour_blocks").fetchone()[0])

        _unstamp_block(conn)
        _drift_block(conn, cost=77.0)
        conn.commit()
        _run_harvest(conn)

        assert int(conn.execute(
            "SELECT id FROM five_hour_blocks").fetchone()[0]) == rowid
        assert int(conn.execute(
            "SELECT block_id FROM five_hour_block_models").fetchone()[0]) == rowid
    finally:
        conn.close()


@pytest.mark.parametrize(
    "damage",
    ["UPDATE journal_effective_events SET event_json = NULL WHERE event_id = ?",
     "UPDATE journal_effective_events SET status = 'tombstone', "
     "event_json = NULL WHERE event_id = ?",
     "UPDATE journal_effective_events SET content_hash = 'sha256:deadbeef' "
     "WHERE event_id = ?"],
    ids=["null-event-json", "tombstoned", "hash-mismatch"],
)
def test_conflict_without_usable_metadata_fails_closed(ns, damage):
    """Missing, tombstoned or hash-mismatched metadata must NOT stamp."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
        _unstamp_block(conn)
        _drift_block(conn, cost=99.0)
        conn.execute(damage, (_BLOCK_EVT_ID,))
        conn.commit()

        with pytest.raises(J.JournalProtocolError, match="cannot converge"):
            _run_harvest(conn)

        assert _block_row(conn)[1] is None, (
            "must not stamp from unusable metadata")
    finally:
        conn.close()


def test_conflict_never_materializes_a_row_that_no_longer_exists(ns, capsys):
    """Convergence UPDATEs the row that carries the event; it must NEVER create
    one. A row can be absent because a suppression effect deliberately DELETED
    it — re-inserting it here would leave the live index holding a row a rebuild
    does not materialise, which is exactly the divergence acceptance 5 forbids.
    (The old insert path had a second failure mode: an insert swallowed by a
    natural-key UNIQUE left the follow-up lookup empty and aborted the whole
    cycle on a `JournalError`.) The emission is dropped and reported instead."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    evt_id = "sa:resurrect:1"

    def _emit(ctx, percent):
        return jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id=evt_id,
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": AT,
                "week_start_date": "2026-07-20",
                "week_end_date": "2026-07-27",
                "week_start_at": "2026-07-20T00:00:00+00:00",
                "week_end_at": "2026-07-27T00:00:00+00:00",
                "weekly_percent": percent,
                "source": "record-usage",
                "payload_json": "{}",
                "account_key": ACCOUNT,
            },
            at=AT,
        )

    def _rows():
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (evt_id,)).fetchone()[0]

    try:
        assert _run_in_cycle(conn, lambda ctx: _emit(ctx, 42.0)) is not None
        assert _rows() == 1

        # the suppression effect removes the row; the journal keeps the event.
        conn.execute(
            "DELETE FROM weekly_usage_snapshots WHERE journal_id = ?", (evt_id,))
        conn.commit()
        before = len(_journal_lines())
        capsys.readouterr()

        # a DIVERGENT re-emission under the same id — classified as a conflict.
        assert _run_in_cycle(conn, lambda ctx: _emit(ctx, 99.0)) is None

        assert len(_journal_lines()) == before, "conflict must append nothing"
        assert _rows() == 0, "the deleted row must NOT be resurrected"
        assert "no live row" in capsys.readouterr().err, (
            "the drop must be reported, not silent")
    finally:
        conn.close()


def test_usage_pipeline_stops_when_duplicate_snapshot_was_suppressed(
    ns, monkeypatch
):
    """A suppressed Model-A row is absent by design, so derive nothing from it."""
    import _cctally_record as record_runtime

    jr = _jr()
    conn = jr._cctally_core.open_db()
    reset = dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc)
    record = J.make_obs(
        at=AT,
        src="record-usage",
        provider="claude",
        payload={
            "captured_at": AT,
            "source": "statusline",
            "weekly_percent": 42.0,
            "resets_at": int(reset.timestamp()),
            "five_hour_percent": 12.0,
            "five_hour_resets_at": "2026-07-25T18:00:00Z",
        },
    )
    derived_snapshot_ids = []

    monkeypatch.setitem(ns, "detect_reset_and_credit", lambda *a, **k: None)
    monkeypatch.setitem(
        ns,
        "maybe_record_milestone",
        lambda saved, **kwargs: derived_snapshot_ids.append(saved["id"]),
    )
    monkeypatch.setitem(
        ns,
        "maybe_update_five_hour_block",
        lambda saved, **kwargs: derived_snapshot_ids.append(saved["id"]),
    )
    monkeypatch.setattr(record_runtime, "_run_dollar_axes", lambda *a, **k: None)

    try:
        _run_in_cycle(
            conn, lambda ctx: record_runtime._pipeline_claude_usage(ctx, record)
        )
        first_row = conn.execute(
            "SELECT id FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{record['id']}",),
        ).fetchone()
        assert first_row is not None
        assert derived_snapshot_ids == [int(first_row[0]), int(first_row[0])]

        conn.execute(
            "DELETE FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{record['id']}",),
        )
        conn.commit()

        _run_in_cycle(
            conn, lambda ctx: record_runtime._pipeline_claude_usage(ctx, record)
        )

        assert derived_snapshot_ids == [int(first_row[0]), int(first_row[0])]
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{record['id']}",),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_duplicate_stamps_and_stops_reharvesting(ns):
    """An exact-duplicate emission still appends (crash-replay contract) but now
    STAMPS the row, so the next cycle re-harvests nothing. Today it returns
    False and the row churns forever."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
        after_first = len(_journal_lines())

        _unstamp_block(conn)          # the stamp rolled back; nothing drifted
        conn.commit()
        _run_harvest(conn)

        assert len(_journal_lines()) == after_first + 1, (
            "the duplicate is still appended (crash-replay contract)")
        assert _block_row(conn)[1] == _BLOCK_EVT_ID, "the row must be stamped"

        _run_harvest(conn)
        assert len(_journal_lines()) == after_first + 1, (
            "a third cycle must re-harvest nothing")
    finally:
        conn.close()


def _seed_milestone_family(conn, *, window_key=None, block_id_override=None):
    """Journal a whole five_hour_milestones dependency chain through ONE real
    cycle: a Model-A `snapshot_accept`, then a natural-keyed reset event, closed
    block and milestone, all harvested in dependency order. `block_id` is a
    DERIVED FK — excluded from the harvested evt — so a wrong value can ride an
    otherwise byte-identical emission."""
    jr = _jr()
    wk = WINDOW_KEY if window_key is None else window_key

    def _body(ctx):
        snap_id = jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id=f"sa:seed:{wk}",
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": AT,
                "week_start_date": "2026-07-20",
                "week_end_date": "2026-07-27",
                "week_start_at": "2026-07-20T00:00:00+00:00",
                "week_end_at": "2026-07-27T00:00:00+00:00",
                "weekly_percent": 42.0,
                "source": "record-usage",
                "payload_json": "{}",
                "account_key": ACCOUNT,
            },
            at=AT,
        )
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc, account_key) "
            "VALUES (?,?,?,?,?,?)",
            (AT, wk, 90.0, 0.0, AT, ACCOUNT),
        )
        reset_id = int(conn.execute(
            "SELECT id FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ?", (wk,)).fetchone()[0])
        block_id = _seed_closed_block(conn, window_key=wk, commit=False)
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, reset_event_id, account_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (block_id if block_id_override is None else block_id_override,
             wk, 50, AT, snap_id, reset_id, ACCOUNT),
        )
        jr._harvest(ctx)
        return block_id

    return _run_in_cycle(conn, _body)


def test_duplicate_validates_excluded_derived_fk(ns):
    """`_build_harvest_evt` excludes derived-FK columns, so byte identity does
    NOT prove `five_hour_milestones.block_id` is right. The duplicate path must
    re-derive and validate it BEFORE stamping."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_milestone_family(conn)
        assert conn.execute(
            "SELECT journal_id FROM five_hour_milestones").fetchone()[0] is not None

        conn.execute(
            "UPDATE five_hour_milestones SET journal_id = NULL, block_id = 999999")
        conn.commit()

        with pytest.raises(jr.JournalError, match="derived FK block_id"):
            _run_harvest(conn)

        assert conn.execute(
            "SELECT journal_id FROM five_hour_milestones").fetchone()[0] is None
    finally:
        conn.close()


def test_duplicate_accepts_an_agreed_unresolvable_derived_fk(ns):
    """An UNRESOLVABLE derived FK is not a defect on its own. `_derived_fk_value`
    returns 0 for "no such parent" and the fold applier stores that same 0, so a
    legitimately parentless row — here a `five_hour_milestones` row whose
    `five_hour_blocks` replica the 5h-credit stale-replica DELETE removed —
    carries `actual == expected == 0` and AGREES.

    Raising on that shape (the old `expected == 0 or …` predicate) escaped
    `_harvest`, rolled the cycle back, left the row unstamped and eligible for
    re-harvest, so every later cycle repeated it. Only disagreement is fatal."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_milestone_family(conn)
        conn.execute("DELETE FROM five_hour_blocks")
        conn.execute(
            "UPDATE five_hour_milestones SET journal_id = NULL, block_id = 0")
        conn.commit()

        assert jr._derived_fk_value(
            conn, "five_hour_blocks", "five_hour_window_key",
            WINDOW_KEY, ACCOUNT) == 0, "non-vacuity: the FK really is unresolvable"

        _run_harvest(conn)              # the duplicate path re-validates first

        assert conn.execute(
            "SELECT journal_id FROM five_hour_milestones"
        ).fetchone()[0] is not None, (
            "an agreed-unresolvable derived FK must stamp, not raise")
    finally:
        conn.close()


def test_scratch_sink_captures_every_candidate_and_stamps_privately(ns, tmp_path):
    """Scratch planning (`event_sink is not None`) must capture EVERY derived
    candidate — never classify or drop — while stamping its private projection
    so later records resolve FKs, and never touching live effective metadata."""
    jr = _jr()
    conn = jr._cctally_core.open_db(_target_path=str(tmp_path / "scratch.db"))
    try:
        sink: list = []                       # an EMPTY list is FALSY
        _seed_closed_block(conn, cost=10.0)

        ctx = _ctx(conn, event_sink=sink, projection_writes=False)
        conn.execute("BEGIN IMMEDIATE")
        jr._harvest(ctx)
        conn.commit()
        assert len(sink) == 1
        assert conn.execute(
            "SELECT journal_id FROM five_hour_blocks").fetchone()[0] == (
                _BLOCK_EVT_ID), "scratch rows must be stamped privately"

        # A second raw record re-derives the SAME natural key with drifted state.
        _unstamp_block(conn)
        _drift_block(conn, cost=99.0)
        conn.commit()
        ctx2 = _ctx(conn, event_sink=sink, projection_writes=False)
        conn.execute("BEGIN IMMEDIATE")
        jr._harvest(ctx2)
        conn.commit()

        assert len(sink) == 2, "scratch must capture BOTH, never classify or drop"
        assert sink[0] != sink[1]
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events").fetchone()[0] == 0, (
                "scratch must not write live effective metadata")
        assert _journal_lines() == [], "scratch must not append to the journal"
    finally:
        conn.close()


def test_scratch_model_a_still_returns_a_rowid(ns, tmp_path):
    """`snapshot_accept` callers store `emit_model_a`'s rowid before deriving
    milestones, so scratch mode must APPLY Model-A events to its projection."""
    jr = _jr()
    conn = jr._cctally_core.open_db(_target_path=str(tmp_path / "scratch.db"))
    try:
        sink: list = []
        ctx = _ctx(conn, event_sink=sink, projection_writes=False)
        conn.execute("BEGIN IMMEDIATE")
        rowid = jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id="sa:o:0123456789abcdef",
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": AT,
                "week_start_date": "2026-07-20",
                "week_end_date": "2026-07-27",
                "week_start_at": "2026-07-20T00:00:00+00:00",
                "week_end_at": "2026-07-27T00:00:00+00:00",
                "weekly_percent": 42.0,
                "source": "record-usage",
                "payload_json": "{}",
                "account_key": ACCOUNT,
            },
            at=AT,
        )
        conn.commit()

        assert isinstance(rowid, int) and rowid > 0
        assert len(sink) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_effective_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_preflight_does_not_raise_on_same_revision_divergence(ns):
    """`_preflight_live_events` is a READER over evt records already in the
    journal: it drops the divergent evt, leaves the prior effective event
    standing, and records the conflict."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)                       # metadata now holds variant A

        divergent = J.make_evt(
            kind="five_hour_block_close",
            id=_BLOCK_EVT_ID,
            at=AT,
            payload={"five_hour_window_key": WINDOW_KEY,
                     "account_key": ACCOUNT,
                     "total_cost_usd": 99.0},
        )
        conflicts: list = []
        applied = jr._preflight_live_events(
            conn, [divergent], jr.journal_high_water(), conflicts=conflicts)

        assert applied == [], "the divergent evt is dropped, not raised on"
        assert [c.event_id for c in conflicts] == [_BLOCK_EVT_ID]
    finally:
        conn.close()


def test_correction_rebuild_required_still_propagates(ns):
    """A revision mismatch stays FATAL at the write boundary (#374 keeps
    `CorrectionRebuildRequired` fatal at all three sites)."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
        _unstamp_block(conn)
        conn.execute(
            "UPDATE journal_effective_events SET rev = 1 WHERE event_id = ?",
            (_BLOCK_EVT_ID,))
        conn.commit()

        with pytest.raises(jr.CorrectionRebuildRequired):
            _run_harvest(conn)
    finally:
        conn.close()


def test_conflict_drops_effects_only_family_without_replaying_effects(ns):
    """`event_json` is authoritative row DATA, never permission to invoke every
    fold applier. An effects-only family (`table=None`) is dropped and reported
    on conflict — its destructive deletes are NOT replayed out of position."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_snapshot(conn, journal_id="sa:doomed", source="record-usage")

        def _emit(suppression):
            def _body(ctx):
                return jr.emit_model_a(
                    ctx,
                    kind="weekly_credit_effects",
                    evt_id="wce:o:cafebabe01234567",
                    table=None,
                    columns={
                        "suppression": suppression,
                        "suppression_table": "weekly_usage_snapshots",
                        "floor_suppression": [],
                        "hwm_floor": {"week_start_date": "2026-07-20",
                                      "weekly_percent": 31.0},
                    },
                    at=AT,
                )
            return _body

        _run_in_cycle(conn, _emit(["sa:doomed"]))
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0] == 0
        before = len(_journal_lines())

        # The row comes back (a later cycle re-accepted it), then a DIVERGENT
        # wce for the same id is derived.
        _seed_snapshot(conn, journal_id="sa:doomed", source="record-usage")
        _run_in_cycle(conn, _emit(["sa:doomed", "sa:other"]))

        assert len(_journal_lines()) == before, "conflict must append nothing"
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0] == 1, (
                "the effective event's destructive effects must NOT be replayed")
    finally:
        conn.close()


def test_conflict_is_counted_and_reported_on_the_context(ns):
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
        _unstamp_block(conn)
        _drift_block(conn, cost=99.0)
        conn.commit()

        ctx = _ctx(conn)
        conn.execute("BEGIN IMMEDIATE")
        jr._harvest(ctx)
        conn.commit()

        assert len(ctx.conflicts_dropped) == 1
        dropped = ctx.conflicts_dropped[0]
        assert dropped.event_id == _BLOCK_EVT_ID
        assert dropped.rev == 0
        assert dropped.rejected_hash.startswith("sha256:")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Acceptance 5 — the live index equals an independent rebuild after BOTH the
# duplicate and the conflict crash paths.
# --------------------------------------------------------------------------

_DUMP_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "weekly_credit_floors", "percent_milestones",
    "five_hour_milestones", "budget_milestones", "projected_milestones",
    "project_budget_milestones", "quota_alert_arming",
)
_DROP_COLS = {"id", "usage_snapshot_id", "cost_snapshot_id", "reset_event_id",
              "block_id"}


def _table_rows(conn, table):
    cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c not in _DROP_COLS]
    rows = [tuple(r) for r in conn.execute(f"SELECT {', '.join(keep)} FROM {table}")]
    return sorted(rows, key=lambda x: tuple(str(v) for v in x))


def _canonical_dump(conn):
    return {t: _table_rows(conn, t) for t in _DUMP_TABLES}


def _closed_block_shape(conn):
    """Exact parent + child assertions for the closed block — the canonical dump
    deliberately omits rowids and derived FKs and would not catch the defects
    this session fixes."""
    parent = conn.execute(
        "SELECT five_hour_window_key, account_key, total_cost_usd, is_closed, "
        "       last_updated_at_utc, journal_id "
        "FROM five_hour_blocks ORDER BY five_hour_window_key").fetchall()
    return {
        "parent": [tuple(r) for r in parent],
        "models": _block_models(conn),
        "projects": _block_projects(conn),
    }


def _assert_child_fks_resolve(conn):
    assert conn.execute(
        "SELECT COUNT(*) FROM five_hour_block_models c "
        "LEFT JOIN five_hour_blocks p ON p.id = c.block_id "
        "WHERE p.id IS NULL").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM five_hour_block_projects c "
        "LEFT JOIN five_hour_blocks p ON p.id = c.block_id "
        "WHERE p.id IS NULL").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM five_hour_milestones m "
        "LEFT JOIN five_hour_blocks b ON b.id = m.block_id "
        "WHERE b.id IS NULL").fetchone()[0] == 0


def test_live_dump_equals_rebuild_after_conflict_and_duplicate_paths(ns, tmp_path):
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0, models=("claude-opus-4",))
        _seed_milestone_family(conn, window_key=WINDOW_KEY + 1)
        _run_harvest(conn)
        assert _block_row(conn) is not None

        # Duplicate crash path: the stamp rolled back, nothing drifted.
        _unstamp_block(conn)
        conn.commit()
        _run_harvest(conn)

        # Conflict crash path: the stamp rolled back AND the row drifted.
        _unstamp_block(conn)
        _drift_block(conn, cost=99.0)
        conn.execute(
            "UPDATE five_hour_block_models SET cost_usd = 99.0 "
            "WHERE five_hour_window_key = ?", (WINDOW_KEY,))
        conn.commit()
        _run_harvest(conn)

        live_dump = _canonical_dump(conn)
        live_blocks = _closed_block_shape(conn)
        _assert_child_fks_resolve(conn)
    finally:
        conn.close()

    rebuilt_path = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(rebuilt_path),
    )
    rb = jr._cctally_core.open_db(_target_path=str(rebuilt_path))
    try:
        assert _canonical_dump(rb) == live_dump
        assert _closed_block_shape(rb) == live_blocks
        _assert_child_fks_resolve(rb)
    finally:
        rb.close()


# ==========================================================================
# Task 4 — containment proof for the families that DID reproduce
#
# Neither reproducing family gets a root-cause fix this session, for reasons
# recorded in the plan and the commit body: `five_hour_block_close`'s parent
# totals and rollup children are derived from `cache.db`, which the spec's §7
# explicitly declares a non-goal for determinism ("No fix attempts determinism
# against a changing cache"); and `crossed_at_utc` is a wall clock BY DESIGN for
# the budget reconcile ("each reconcile stamps at its own (live) moment"), so
# pinning it is a semantic redesign of "when did this threshold cross", not a
# bug fix. Both therefore take acceptance 12's third disposition:
# writer-backstop containment, PROVEN here.
# ==========================================================================

def _run_replay_then_harvest(conn, orphan_records):
    """The next cycle's step 4a (replay orphaned evt lines) followed by step 4c
    (harvest), inside one `BEGIN IMMEDIATE` — the exact order `_run_cycle`
    uses."""
    jr = _jr()
    conflicts: list = []
    to_apply = jr._preflight_live_events(
        conn, orphan_records, jr.journal_high_water(), conflicts=conflicts)
    ctx = _ctx(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for evt in sorted(to_apply, key=jr._fold_order):
            if jr._record_live_effective_event(conn, evt):
                jr._apply_evt(conn, evt)
        jr._harvest(ctx)
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return ctx, conflicts


def test_crash_after_harvest_append_replay_converges_before_next_harvest(
    ns, tmp_path
):
    """A replayed frozen close converges an existing natural-key parent and both
    child sets immediately. The later harvest therefore has no unstamped row to
    classify; #374's conflict path remains covered by the deliberately forged
    divergent-event tests above."""
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0, last_updated=AT)
        with pytest.raises(RuntimeError, match="forced abort"):
            _run_harvest(conn, abort=True)

        orphan = [r for r in _journal_lines() if r.get("t") == "evt"]
        assert len(orphan) == 1, "the aborted cycle left exactly one orphan line"

        # The live upsert moves BOTH volatile axes before the retry.
        conn.execute(
            "UPDATE five_hour_blocks SET last_updated_at_utc = ?, "
            "total_cost_usd = ? WHERE five_hour_window_key = ?",
            ("2026-07-25T15:00:30Z", 99.0, WINDOW_KEY))
        conn.execute(
            "UPDATE five_hour_block_models SET cost_usd = 99.0 "
            "WHERE five_hour_window_key = ?", (WINDOW_KEY,))
        conn.commit()

        ctx, _conflicts = _run_replay_then_harvest(conn, orphan)

        lines = _journal_lines()
        assert len(_evts(lines, "fhbc:")) == 1, (
            "the write boundary must withhold the divergent retry")
        assert _same_rev_conflicts(lines) == set(), (
            "the journal must NOT acquire a same-revision conflict")
        assert len(ctx.conflicts_dropped) == 0
        assert _block_row(conn) == (10.0, _BLOCK_EVT_ID, AT), (
            "the row converged to the journaled event and was stamped")
        assert _block_models(conn) == [("claude-opus-4", 10.0)]

        live_dump = _canonical_dump(conn)
        live_blocks = _closed_block_shape(conn)
        _assert_child_fks_resolve(conn)
    finally:
        conn.close()

    rebuilt = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(rebuilt),
    )
    rb = jr._cctally_core.open_db(_target_path=str(rebuilt))
    try:
        assert _canonical_dump(rb) == live_dump
        assert _closed_block_shape(rb) == live_blocks
        _assert_child_fks_resolve(rb)
    finally:
        rb.close()


def test_budget_reconcile_crash_is_contained_by_replay_then_insert_or_ignore(
    ns, monkeypatch, tmp_path
):
    """The budget families are contained one step earlier. Replaying the orphan
    `bm:` evt at step 4a MATERIALIZES the latched row with its journal_id, so the
    next reconcile's natural-key `INSERT OR IGNORE` is a no-op, nothing is left
    unstamped, and the harvest has nothing to re-derive from a moved clock."""
    import _cctally_milestones as m

    jr = _jr()
    conn = jr._cctally_core.open_db()
    clock = {"now": "2026-07-25T15:00:00Z"}
    monkeypatch.setattr(m, "now_utc_iso", lambda: clock["now"])

    def _reconcile(conn_):
        m.insert_budget_milestone(
            conn_,
            vendor="claude",
            period_start_at="2026-07-20T00:00:00+00:00",
            period="subscription-week",
            threshold=80,
            budget_usd=100.0,
            spent_usd=85.0,
            consumption_pct=85.0,
            commit=False,
            as_of=None,
            account_key="*",
        )

    try:
        def _body(ctx):
            _reconcile(conn)
            jr._harvest(ctx)

        with pytest.raises(RuntimeError, match="forced abort"):
            _run_in_cycle(conn, _body, abort=True)

        orphan = [r for r in _journal_lines() if r.get("t") == "evt"]
        assert len(orphan) == 1

        # A later config write re-runs the reconcile at its own (moved) moment,
        # but the cycle replays the orphan FIRST.
        clock["now"] = "2026-07-25T15:00:45Z"
        jr_conflicts: list = []
        to_apply = jr._preflight_live_events(
            conn, orphan, jr.journal_high_water(), conflicts=jr_conflicts)
        ctx = _ctx(conn)
        conn.execute("BEGIN IMMEDIATE")
        for evt in sorted(to_apply, key=jr._fold_order):
            if jr._record_live_effective_event(conn, evt):
                jr._apply_evt(conn, evt)
        _reconcile(conn)                     # INSERT OR IGNORE -> no-op
        jr._harvest(ctx)
        conn.commit()

        lines = _journal_lines()
        assert len(_evts(lines, "bm:")) == 1
        assert _same_rev_conflicts(lines) == set()
        assert conn.execute(
            "SELECT crossed_at_utc, journal_id FROM budget_milestones"
        ).fetchone()[0] == "2026-07-25T15:00:00Z", (
            "the replayed orphan's moment stands; the retry never re-stamped it")

        live_dump = _canonical_dump(conn)
    finally:
        conn.close()

    rebuilt = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(rebuilt),
    )
    rb = jr._cctally_core.open_db(_target_path=str(rebuilt))
    try:
        assert _canonical_dump(rb) == live_dump
    finally:
        rb.close()


# ==========================================================================
# Task 6 — surfacing: the rebuild summary and the two doctor legs
# ==========================================================================

def _append_divergent_snapshot_pair(jr, *, event_id="sa:conflict"):
    import datetime as _dt

    fixed = _dt.datetime(2026, 7, 25, 15, 0, 0, tzinfo=_dt.timezone.utc)
    for percent in (10.0, 20.0):
        jr.append_record(
            J.make_evt(
                kind="snapshot_accept",
                id=event_id,
                at=AT,
                payload={
                    "captured_at_utc": AT,
                    "week_start_date": "2026-07-20",
                    "week_end_date": "2026-07-27",
                    "week_start_at": "2026-07-20T00:00:00+00:00",
                    "week_end_at": "2026-07-27T00:00:00+00:00",
                    "weekly_percent": percent,
                    "source": "test",
                    "payload_json": "{}",
                    "account_key": ACCOUNT,
                },
            ),
            now_utc=fixed,
        )


def test_rebuild_reports_quarantined_groups_and_exits_zero(ns, tmp_path, capsys):
    """`db rebuild --db stats` completes over a conflicted journal, names the
    groups, and keeps exit 0 — the epoch-1002 wedge is gone."""
    import argparse

    jr = _jr()
    _append_divergent_snapshot_pair(jr)

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(tmp_path / "probe.db"),
    )
    assert [c.event_id for c in result.conflicts] == ["sa:conflict"]
    assert result.conflicts[0].rev == 0
    assert len(result.conflicts[0].content_hashes) == 2

    capsys.readouterr()
    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=False)) == 0
    text = capsys.readouterr().out
    assert "1 quarantined same-revision group(s)" in text
    assert "sa:conflict rev 0" in text
    assert "cctally db rederive --family claude-usage" in text


def test_rebuild_json_uses_journal_conflicts_not_conflicts(ns, capsys):
    """Acceptance 14: the new key is `journalConflicts`; `db rederive --json`'s
    pre-existing `conflicts` key is untouched."""
    import argparse
    import json as _json

    jr = _jr()
    _append_divergent_snapshot_pair(jr)

    capsys.readouterr()
    assert ns["cmd_db_rebuild"](
        argparse.Namespace(db="stats", json=True)) == 0
    payload = _json.loads(capsys.readouterr().out)

    assert "conflicts" not in payload
    assert [c["eventId"] for c in payload["journalConflicts"]] == ["sa:conflict"]
    assert payload["journalConflicts"][0]["revision"] == 0
    assert payload["journalConflicts"][0]["selectedHash"].startswith("sha256:")


def test_doctor_deep_gather_reports_quarantined_groups(ns):
    jr = _jr()
    _append_divergent_snapshot_pair(jr)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    state = ns["doctor_gather_state"](deep=True)

    assert state.journal_protocol_error is None
    assert [c["eventId"] for c in state.journal_conflicts] == ["sa:conflict"]

    import _lib_doctor

    leg = _lib_doctor._check_journal_conflicts(state)
    assert leg.severity == "warn"
    assert "db rederive" in (leg.remediation or "")
    assert _lib_doctor._check_journal_protocol(state).severity == "ok"


def test_doctor_deep_gather_is_clean_on_a_healthy_journal(ns):
    jr = _jr()
    conn = jr._cctally_core.open_db()
    try:
        _seed_closed_block(conn, cost=10.0)
        _run_harvest(conn)
    finally:
        conn.close()

    state = ns["doctor_gather_state"](deep=True)

    assert state.journal_conflicts == []
    assert state.journal_protocol_error is None


def test_doctor_deep_gather_reports_tainted_batch_and_available_conflicts(
    ns,
):
    """#402: selector completion makes event conflicts independently available
    while journal.protocol honestly FAILs on the omitted correction batch."""
    import datetime as _dt
    import _lib_doctor

    jr = _jr()
    fixed = _dt.datetime(2026, 7, 25, 15, 0, 0, tzinfo=_dt.timezone.utc)
    jr.append_record(
        J.make_evt(kind="snapshot_accept", id="sa:x", at=AT,
                   payload={"weekly_percent": 1.0}),
        now_utc=fixed)
    batch = J.make_correction_batch(
        batch_id="batch:tampered-doctor",
        family="claude-usage",
        at=AT,
        actions=[{"action": "replace", "id": "sa:x", "rev": 1, "at": AT,
                  "payload": {"kind": "snapshot_accept", "weekly_percent": 2.0}}],
    )
    batch[1]["payload"]["weekly_percent"] = 99.0     # manifest hash mismatch
    for record in batch:
        jr.append_record(record, now_utc=fixed)

    state = ns["doctor_gather_state"](deep=True)

    assert state.journal_conflicts == []
    assert state.journal_protocol_error is None
    assert [
        (item["batchId"], item["kind"])
        for item in state.journal_protocol_violations
    ] == [("batch:tampered-doctor", "manifest_actions_hash_mismatch")]

    conflicts_leg = _lib_doctor._check_journal_conflicts(state)
    protocol_leg = _lib_doctor._check_journal_protocol(state)
    assert conflicts_leg.severity == "ok"
    assert conflicts_leg.details["available"] is True
    assert protocol_leg.severity == "fail"
    assert "tainted correction batches omitted" in protocol_leg.summary

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    shallow = ns["doctor_gather_state"](deep=False)
    assert [
        (item["batchId"], item["kind"])
        for item in shallow.journal_protocol_violations
    ] == [("batch:tampered-doctor", "manifest_actions_hash_mismatch")]
    assert _lib_doctor._check_journal_protocol(shallow).severity == "fail"


def test_doctor_shallow_gather_does_not_scan_for_conflicts(ns):
    jr = _jr()
    _append_divergent_snapshot_pair(jr)

    state = ns["doctor_gather_state"](deep=False)

    assert state.journal_conflicts is None
    assert state.journal_protocol_violations is None
    assert state.journal_protocol_error is None
