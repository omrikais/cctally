"""#869: stored costs need the pricing provenance of their source entries."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from conftest import load_isolated_cctally_module


UNKNOWN = "claude-opus-5-5"
KNOWN = "claude-3-5-sonnet-20241022"
CODEX_LATER_CARD = "gpt-6-sol"
HISTORICAL_BASIS = "historical-zero-cost-inference"
AT = (
    "2026-07-25T12:00:00Z",
    "2026-07-25T16:05:00Z",
)
RESETS = (
    "2026-07-25T15:00:00Z",
    "2026-07-25T20:00:00Z",
)


def _pricing_revision(pricing, *, claude_prices, date, codex_prices=None):
    old = pricing.current_pricing_snapshot()
    return pricing.PricingSnapshot(
        snapshot_date=date,
        claude_pricing=claude_prices,
        codex_pricing=codex_prices if codex_prices is not None else old.codex_pricing,
        aliases=old.aliases,
        tier_thresholds=old.tier_thresholds,
        fallback_model=old.fallback_model,
        cache_write_1h_multiplier=old.cache_write_1h_multiplier,
        fast_multipliers=old.fast_multipliers,
    )


def _seed_cache(mod, *, first_model):
    conn = mod.open_cache_db()
    path = "/tmp/claude/projects/repo/869-session.jsonl"
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        "session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 200, 1, 200, AT[0], "session-869", "/repo"),
    )
    for offset, stamp, model in (
        (0, "2026-07-25T11:55:00+00:00", first_model),
        (100, "2026-07-25T16:00:00+00:00", KNOWN),
    ):
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            "output_tokens, cache_create_tokens, cache_read_tokens, "
            "cache_create_1h_tokens, account_key) "
            "VALUES (?,?,?,?,1000000,0,0,0,0,?)",
            (path, offset, stamp, model, "acct-869"),
        )
    conn.commit()
    conn.close()


def _observe(mod, *, indices=(0, 1)):
    observations = tuple(zip(
        AT, RESETS, (1.0, 2.0), (1.0, 1.0), strict=True,
    ))
    _observe_points(mod, [observations[index] for index in indices])


def _observe_points(mod, points):
    import _cctally_journal as runtime
    import _lib_journal as journal

    weekly_reset = int(dt.datetime(
        2026, 7, 27, tzinfo=dt.timezone.utc
    ).timestamp())
    for at, reset, weekly_pct, five_pct in points:
        runtime.append_record(journal.make_obs(
            at=at,
            src="record-usage",
            provider="claude",
            account="acct-869",
            payload={
                "captured_at": at,
                "source": "statusline",
                "weekly_percent": weekly_pct,
                "resets_at": weekly_reset,
                "five_hour_percent": five_pct,
                "five_hour_resets_at": reset,
            },
        # The ingester appends its derived events to the segment of the real
        # clock and its cursor follows them. A raw line pinned to an earlier
        # month segment would land behind that cursor after the second tick.
        ))
        assert runtime.run_stats_ingest(mode="authoritative").ran is True


def _insert_entries(mod, rows, *, path="/tmp/claude/projects/repo/869-session.jsonl",
                    project="/repo", session="session-869"):
    """Insert Claude cache rows: (offset, stamp, model, tokens..., account, raw)."""
    conn = mod.open_cache_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            "session_id, project_path) VALUES (?,?,?,?,?,?,?)",
            (path, 100000, 1, 100000, AT[0], session, project),
        )
        for (offset, stamp, model, input_tokens, output_tokens, cache_create,
             cache_create_1h, cache_read, account, raw) in rows:
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, input_tokens, "
                "output_tokens, cache_create_tokens, cache_read_tokens, "
                "cache_create_1h_tokens, account_key, cost_usd_raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (path, offset, stamp, model, input_tokens, output_tokens,
                 cache_create, cache_read, cache_create_1h, account, raw),
            )
        conn.commit()
    finally:
        conn.close()


def _strip_open_block_marker(mod):
    """Make the open block look like one computed before provenance existed."""
    db = mod.open_db()
    try:
        db.execute(
            "UPDATE five_hour_blocks SET pricing_provenance_json=NULL "
            "WHERE is_closed=0"
        )
        db.commit()
    finally:
        db.close()


def _events(mod):
    return [json.loads(line) for body in _journal_bytes(mod).values()
            for line in body.splitlines()]


def _without_card(pricing, model, *, date="2026-09-22", codex=False):
    present = pricing.current_pricing_snapshot()
    if codex:
        cards = dict(present.codex_pricing)
        cards.pop(model)
        return _pricing_revision(
            pricing, claude_prices=present.claude_pricing,
            codex_prices=cards, date=date,
        )
    cards = dict(present.claude_pricing)
    cards.pop(model)
    return _pricing_revision(pricing, claude_prices=cards, date=date)


def _entry_cost(pricing, model, input_tokens, output_tokens, cache_create,
                cache_create_1h, cache_read):
    return pricing._calculate_entry_cost(model, pricing.claude_usage_dict(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_tokens=cache_create, cache_read_tokens=cache_read,
        cache_1h_tokens=cache_create_1h, speed=None,
    ))


def _kind_counts(payload, kind):
    return payload["actionCountsByEventKind"][kind]


def _closed_costs(mod):
    conn = mod.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT five_hour_window_key, total_cost_usd, journal_id "
            "FROM five_hour_blocks WHERE is_closed=1 "
            "ORDER BY five_hour_window_key"
        )]
    finally:
        conn.close()


def _journal_bytes(mod):
    return {
        path.name: path.read_bytes()
        for path in sorted(mod.JOURNAL_DIR.glob("*.jsonl"))
    }


def _first_crossing_costs(mod, block_key):
    conn = mod.open_db()
    try:
        def one(sql, params=()):
            row = conn.execute(sql, params).fetchone()
            assert row is not None, sql
            return float(row[0])

        return {
            "weeklySnapshot": one(
                "SELECT cost_usd FROM weekly_cost_snapshots "
                "WHERE captured_at_utc=? AND account_key='acct-869'",
                (AT[0],),
            ),
            "weeklyMilestone": one(
                "SELECT cumulative_cost_usd FROM percent_milestones "
                "WHERE captured_at_utc=? AND percent_threshold=1 "
                "AND account_key='acct-869'", (AT[0],),
            ),
            "fiveHourMilestone": one(
                "SELECT block_cost_usd FROM five_hour_milestones "
                "WHERE five_hour_window_key=? AND percent_threshold=1 "
                "AND account_key='acct-869'", (block_key,),
            ),
            "modelChild": one(
                "SELECT cost_usd FROM five_hour_block_models "
                "WHERE five_hour_window_key=? AND model=? "
                "AND account_key='acct-869'", (block_key, UNKNOWN),
            ),
            "projectChild": one(
                "SELECT cost_usd FROM five_hour_block_projects "
                "WHERE five_hour_window_key=? AND account_key='acct-869'",
                (block_key,),
            ),
        }
    finally:
        conn.close()


def _materialized_costs(path):
    """Logical monetary rows; SQLite row IDs may differ after rebuild."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        queries = {
            "blocks": (
                "SELECT account_key, five_hour_window_key, is_closed, "
                "total_cost_usd FROM five_hour_blocks "
                "ORDER BY account_key, five_hour_window_key"
            ),
            "models": (
                "SELECT account_key, five_hour_window_key, model, cost_usd "
                "FROM five_hour_block_models "
                "ORDER BY account_key, five_hour_window_key, model"
            ),
            "projects": (
                "SELECT account_key, five_hour_window_key, project_path, cost_usd "
                "FROM five_hour_block_projects "
                "ORDER BY account_key, five_hour_window_key, project_path"
            ),
            "fiveHourMilestones": (
                "SELECT account_key, five_hour_window_key, percent_threshold, "
                "block_cost_usd, marginal_cost_usd FROM five_hour_milestones "
                "ORDER BY account_key, five_hour_window_key, percent_threshold"
            ),
            "weeklyMilestones": (
                "SELECT account_key, captured_at_utc, percent_threshold, "
                "cumulative_cost_usd, marginal_cost_usd FROM percent_milestones "
                "ORDER BY account_key, captured_at_utc, percent_threshold"
            ),
            "weeklySnapshots": (
                "SELECT account_key, captured_at_utc, cost_usd "
                "FROM weekly_cost_snapshots "
                "ORDER BY account_key, captured_at_utc"
            ),
        }
        return {name: [tuple(row) for row in conn.execute(sql)]
                for name, sql in queries.items()}
    finally:
        conn.close()


def _rederive(mod, capsys, *, apply):
    code = mod.cmd_db_rederive(argparse.Namespace(
        family="claude-usage", yes=apply, json=True,
    ))
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)


def test_unpriced_closed_cost_is_correctable_but_priced_close_stays_frozen(
    tmp_path, monkeypatch, capsys,
):
    """A later card corrects missing cost, not a genuine closed monetary fact.

    The journal is the source of truth; the cache entries are the retained
    tokens. A future revision also changes the known card deliberately, so a
    blanket closed-block reprice fails this test even if it fixes the $0 row.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    assert UNKNOWN in present.claude_pricing
    assert KNOWN in present.claude_pricing
    before_prices = dict(present.claude_pricing)
    before_prices.pop(UNKNOWN)
    before_revision = _pricing_revision(
        pricing, claude_prices=before_prices, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: before_revision)
    assert pricing._resolve_model_pricing(UNKNOWN, warn=False) is None
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod)
    capsys.readouterr()

    closed_before = _closed_costs(mod)
    assert len(closed_before) == 1, closed_before
    unpriced_key, unpriced_cost, unpriced_event = closed_before[0]
    assert unpriced_cost == 0.0
    assert unpriced_event
    assert _first_crossing_costs(mod, unpriced_key) == dict.fromkeys((
        "weeklySnapshot", "weeklyMilestone", "fiveHourMilestone",
        "modelChild", "projectChild",
    ), 0.0)

    later_prices = dict(present.claude_pricing)
    later_prices[KNOWN] = {
        **later_prices[KNOWN],
        "input_cost_per_token": 7e-6,
    }
    later_revision = _pricing_revision(
        pricing, claude_prices=later_prices, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: later_revision)
    assert pricing._calculate_entry_cost(
        UNKNOWN, {"input_tokens": 1000000, "output_tokens": 0,
                  "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
    ) == pytest.approx(4.0)

    original_journal = _journal_bytes(mod)
    preview = _rederive(mod, capsys, apply=False)
    assert preview["status"] == "preview", preview
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 1, preview
    assert _closed_costs(mod) == closed_before
    assert _journal_bytes(mod) == original_journal

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    after_journal = _journal_bytes(mod)
    for name, original in original_journal.items():
        assert after_journal[name].startswith(original)
    closed_after = _closed_costs(mod)
    assert [row[0] for row in closed_after] == [unpriced_key]
    assert closed_after[0][1] == pytest.approx(4.0)
    assert _first_crossing_costs(mod, unpriced_key) == pytest.approx(
        dict.fromkeys((
            "weeklySnapshot", "weeklyMilestone", "fiveHourMilestone",
            "modelChild", "projectChild",
        ), 4.0)
    )

    stable_journal = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert _journal_bytes(mod) == stable_journal

    independent = tmp_path / "independent-stats.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
    )
    assert _materialized_costs(mod.DB_PATH) == _materialized_costs(independent)

    assert mod.cmd_five_hour_breakdown(argparse.Namespace(
        block_start=None, ago=1, json=True, tz=None, account=None,
    )) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["block"]["totalCost"] == pytest.approx(4.0)


def test_genuinely_priced_close_remains_frozen_after_rate_revision(
    tmp_path, monkeypatch, capsys,
):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    _seed_cache(mod, first_model=KNOWN)
    _observe(mod)
    capsys.readouterr()
    closed_before = _closed_costs(mod)
    assert len(closed_before) == 1, closed_before
    assert closed_before[0][1] == pytest.approx(3.0)
    later_prices = dict(pricing.current_pricing_snapshot().claude_pricing)
    later_prices[KNOWN] = {
        **later_prices[KNOWN], "input_cost_per_token": 7e-6,
    }
    revision = _pricing_revision(
        pricing, claude_prices=later_prices, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revision)

    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 0, preview
    assert _closed_costs(mod) == closed_before


def _five_hour_milestone_costs(mod, block_key):
    conn = mod.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT percent_threshold, block_cost_usd, marginal_cost_usd "
            "FROM five_hour_milestones WHERE five_hour_window_key=? "
            "AND account_key='acct-869' ORDER BY percent_threshold",
            (block_key,),
        )]
    finally:
        conn.close()


def _open_block_keys(mod):
    conn = mod.open_db()
    try:
        return [row[0] for row in conn.execute(
            "SELECT five_hour_window_key FROM five_hour_blocks "
            "WHERE is_closed=0 AND account_key='acct-869'"
        )]
    finally:
        conn.close()


def test_genuinely_priced_close_freezes_its_milestones_875(
    tmp_path, monkeypatch, capsys,
):
    """#875: a frozen, genuinely priced close keeps its milestones' money.

    The close was priced all along, so it carries no #869 correction basis.
    A later rate-card edit must leave its five-hour milestones' monetary
    fields at their at-crossing values beside the close's unchanged total.
    The rederive makes no claim that a milestone stays at or below that
    total (#882). The open block's milestone has no frozen close, so it
    still follows the new card; that control proves the revision reached the
    plan at all.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    # $3 per entry at the embedded card: two entries in the closed block, one
    # in its successor, and two crossings in the closed block so the second
    # carries a non-null marginal cost.
    _insert_entries(mod, [
        (offset, stamp, KNOWN, 1000000, 0, 0, 0, 0, "acct-869", None)
        for offset, stamp in (
            (0, "2026-07-25T11:55:00+00:00"),
            (100, "2026-07-25T12:30:00+00:00"),
            (200, "2026-07-25T16:00:00+00:00"),
        )
    ])
    _observe_points(mod, [
        (AT[0], RESETS[0], 1.0, 1.0),
        ("2026-07-25T12:45:00Z", RESETS[0], 2.0, 2.0),
        (AT[1], RESETS[1], 3.0, 1.0),
    ])
    capsys.readouterr()
    closed_before = _closed_costs(mod)
    assert len(closed_before) == 1, closed_before
    closed_key, closed_total, _close_event = closed_before[0]
    assert closed_total == pytest.approx(6.0)
    closed_milestones = _five_hour_milestone_costs(mod, closed_key)
    assert closed_milestones == [
        (1, pytest.approx(3.0), None),
        (2, pytest.approx(6.0), pytest.approx(3.0)),
    ]
    [open_key] = _open_block_keys(mod)
    assert _five_hour_milestone_costs(mod, open_key) == [
        (1, pytest.approx(3.0), None),
    ]

    later_prices = dict(pricing.current_pricing_snapshot().claude_pricing)
    later_prices[KNOWN] = {
        **later_prices[KNOWN], "input_cost_per_token": 7e-6,
    }
    revision = _pricing_revision(
        pricing, claude_prices=later_prices, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revision)

    preview = _rederive(mod, capsys, apply=False)
    assert preview["status"] == "preview", preview
    assert _kind_counts(preview, "five_hour_block_close")["supersede"] == 0, preview
    # Only the open block's milestone may follow the new card.
    assert _kind_counts(preview, "five_hour_milestone")["supersede"] == 1, preview

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _closed_costs(mod) == closed_before
    assert _five_hour_milestone_costs(mod, closed_key) == closed_milestones
    # A2: the TUI and the historical Blocks detail read this row through one
    # shared reader.
    conn = mod.open_db()
    try:
        shared = mod._tui_build_five_hour_milestones(
            conn, closed_key, "acct-869")
    finally:
        conn.close()
    assert [
        (m["percent_threshold"], m["block_cost_usd"], m["marginal_cost_usd"])
        for m in shared
    ] == [(1, pytest.approx(3.0), None), (2, pytest.approx(6.0), pytest.approx(3.0))]
    assert all(
        block_cost <= closed_total + 1e-9
        for _threshold, block_cost, _marginal in closed_milestones
    )
    assert _five_hour_milestone_costs(mod, open_key) == [
        (1, pytest.approx(7.0), None),
    ]

    stable_journal = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert _journal_bytes(mod) == stable_journal

    independent = tmp_path / "independent-stats.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent,
    )
    assert _materialized_costs(mod.DB_PATH) == _materialized_costs(independent)

    assert mod.cmd_five_hour_breakdown(argparse.Namespace(
        block_start=None, ago=1, json=True, tz=None, account=None,
    )) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["block"]["fiveHourWindowKey"] == closed_key
    assert rendered["block"]["totalCost"] == pytest.approx(6.0)
    assert [
        (m["percentThreshold"], m["blockCostUSD"], m["marginalCostUSD"])
        for m in rendered["milestones"]
    ] == [(1, pytest.approx(3.0), None), (2, pytest.approx(6.0), pytest.approx(3.0))]


def test_open_unpriced_block_keeps_computation_provenance_across_card_adoption(
    tmp_path, monkeypatch, capsys,
):
    """A successor closes the old totals without repricing that old block."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    old_cards = dict(present.claude_pricing)
    old_cards.pop(UNKNOWN)
    old = _pricing_revision(
        pricing, claude_prices=old_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: old)
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod, indices=(0,))
    capsys.readouterr()

    # The first block is still open when a later binary learns this model.
    db = mod.open_db()
    try:
        row = db.execute(
            "SELECT is_closed, total_cost_usd, pricing_provenance_json "
            "FROM five_hour_blocks"
        ).fetchone()
        assert tuple(row[:2]) == (0, 0.0)
        assert json.loads(row[2]) == {
            "version": 1,
            "pricingDate": "2026-09-22",
            "unpricedModels": [UNKNOWN],
        }
    finally:
        db.close()
    newer = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer)
    _observe(mod, indices=(1,))

    assert _closed_costs(mod)[0][1] == 0.0
    events = [json.loads(line) for body in _journal_bytes(mod).values()
              for line in body.splitlines()]
    closed = [event for event in events
              if (event.get("payload") or {}).get("kind")
              == "five_hour_block_close"]
    assert len(closed) == 1
    assert closed[0]["payload"]["_pricing"]["unpricedModels"] == [UNKNOWN]
    assert closed[0]["payload"]["_pricing"]["pricingDate"] == "2026-09-22"
    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 1


def test_unmarked_open_block_does_not_invent_missing_card_proof_at_close(
    tmp_path, monkeypatch, capsys,
):
    """The close carries no marker; rederive proves it from retained entries.

    Harvesting the close must not look up the closing binary's cards. The
    historical rule reaches the same close afterwards, from the exact retained
    token population, and records its own distinct basis.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    old = _without_card(pricing, UNKNOWN)
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: old)
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod, indices=(0,))
    # This is the shape of an open projection from before provenance existed.
    _strip_open_block_marker(mod)

    newer = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer)
    _observe(mod, indices=(1,))
    closed = [event for event in _events(mod)
              if (event.get("payload") or {}).get("kind")
              == "five_hour_block_close"]
    assert len(closed) == 1
    assert "_pricing" not in closed[0]["payload"]
    assert closed[0]["payload"]["total_cost_usd"] == 0.0
    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 1
    assert "uncertainCostFactCount" not in preview
    [action] = [
        action for action in mod.preview_db_rederive("claude-usage").plan.actions
        if action.event_id == closed[0]["id"]
    ]
    assert action.payload["_pricing"]["basis"] == HISTORICAL_BASIS
    assert json.loads(action.payload["pricing_provenance_json"]) == (
        action.payload["_pricing"])


def test_interactive_codex_budget_journals_its_at_fire_pricing_provenance(
    tmp_path, monkeypatch, capsys,
):
    """The status command's opportunistic writer uses the journal context."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    mod.open_db().close()
    monkeypatch.setenv("CCTALLY_AS_OF", AT[0])
    mod.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "Etc/UTC"},
        "budget": {"codex": {
            "amount_usd": 0.125, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [100],
        }},
    }))
    marker = {"version": 1, "fallbackModels": [CODEX_LATER_CARD]}

    def spend(_start, _end, *, speed="auto", account_key=None, skip_sync=False,
              pricing_provenance=None):
        if pricing_provenance is not None:
            pricing_provenance.update(marker)
        return 0.125

    monkeypatch.setattr(mod, "_sum_codex_cost_for_range", spend)
    assert mod.cmd_budget(argparse.Namespace(
        action=None, amount=None, project=None, vendor="codex", period=None,
        config=None, reveal_projects=False, tz=None, json=False,
        format=None, theme="light", no_branding=False, output=None,
        copy=False, open_after_write=False,
    )) == 0
    capsys.readouterr()
    events = [json.loads(line) for body in _journal_bytes(mod).values()
              for line in body.splitlines()]
    codex_budget = [event for event in events
                    if (event.get("payload") or {}).get("kind") == "budget"
                    and event["payload"].get("vendor") == "codex"]
    assert len(codex_budget) == 1
    assert codex_budget[0]["payload"]["_pricing"] == marker


def test_config_reconcile_codex_budget_journals_pricing_provenance(
    tmp_path, monkeypatch,
):
    """A config-time latch retains the same cost evidence as a live crossing."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime

    mod.open_db().close()
    monkeypatch.setenv("CCTALLY_AS_OF", AT[0])
    config = {"display": {"tz": "Etc/UTC"}, "budget": {"codex": {
        "amount_usd": 0.125, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [100],
    }}}
    mod.CONFIG_PATH.write_text(json.dumps(config))
    marker = {"version": 1, "fallbackModels": [CODEX_LATER_CARD]}

    def spend(_start, _end, *, speed="auto", account_key=None, skip_sync=False,
              pricing_provenance=None):
        if pricing_provenance is not None:
            pricing_provenance.update(marker)
        return 0.125

    monkeypatch.setattr(mod, "_sum_codex_cost_for_range", spend)
    assert runtime.run_stats_ingest(mode="authoritative", reconcile_config={
        "budget": config["budget"], "axes": ["codex_budget"],
    }).ran is True
    events = [json.loads(line) for body in _journal_bytes(mod).values()
              for line in body.splitlines()]
    codex_budget = [event for event in events
                    if (event.get("payload") or {}).get("kind") == "budget"
                    and event["payload"].get("vendor") == "codex"]
    assert len(codex_budget) == 1
    assert codex_budget[0]["payload"]["_pricing"] == marker


def test_codex_budget_capture_cutoff_matches_durable_timestamp(
    tmp_path, monkeypatch,
):
    """Subsecond spend must not be counted beyond the stored crossing time."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime

    mod.open_db().close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-25T12:00:00.900000Z")
    config = {"display": {"tz": "Etc/UTC"}, "budget": {"codex": {
        "amount_usd": 0.125, "period": "calendar-month",
        "alerts_enabled": True, "alert_thresholds": [100],
    }}}
    mod.CONFIG_PATH.write_text(json.dumps(config))
    observed = []

    def spend(_start, end, *, speed="auto", account_key=None, skip_sync=False,
              pricing_provenance=None):
        observed.append(end)
        return 0.125

    monkeypatch.setattr(mod, "_sum_codex_cost_for_range", spend)

    def fire(ctx):
        assert mod.maybe_record_codex_budget_milestone(
            {}, conn=ctx.conn, journal_ctx=ctx, alert_sink=ctx.pending_alerts,
        ) == 1

    assert runtime.run_stats_ingest(mode="authoritative", codex_apply=fire).ran
    db = mod.open_db()
    try:
        crossed_at = db.execute(
            "SELECT crossed_at_utc FROM budget_milestones WHERE vendor='codex'"
        ).fetchone()[0]
    finally:
        db.close()
    assert len(observed) == 1
    assert observed[0].microsecond == 900000
    assert observed[0] == dt.datetime.fromisoformat(
        crossed_at.replace("Z", "+00:00")
    )


def test_zero_dollar_direct_card_is_not_mistaken_for_missing_pricing(
    monkeypatch,
):
    """A $0 close proves a missing card only while no used rate is zero.

    The historical rule infers a missing card from a zero amount, which is
    sound only because every embedded Claude card prices each token class,
    each applicable long-context tier and the derived 1-hour cache write at a
    positive rate. The embedded table is checked here, so a future zero-rate
    card fails this test until the rule is reconsidered. A simulated
    nonpositive card is detected, and the per-entry proof refuses it.
    """
    import _lib_cost_provenance as provenance
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    assert len(present.claude_pricing) > 10
    assert provenance.claude_card_rate_violations(present) == []

    for field in (
        "input_cost_per_token", "output_cost_per_token",
        "cache_creation_input_token_cost", "cache_read_input_token_cost",
        "input_cost_per_token_above_200k_tokens",
        "cache_read_input_token_cost_above_200k_tokens",
    ):
        cards = dict(present.claude_pricing)
        cards[KNOWN] = {**cards[KNOWN], field: 0.0}
        simulated = _pricing_revision(
            pricing, claude_prices=cards, date="2026-09-22",
        )
        assert (KNOWN, field) in provenance.claude_card_rate_violations(
            simulated), field
    no_1h = pricing.PricingSnapshot(
        snapshot_date="2026-09-22",
        claude_pricing=present.claude_pricing,
        codex_pricing=present.codex_pricing,
        aliases=present.aliases,
        tier_thresholds=present.tier_thresholds,
        fallback_model=present.fallback_model,
        cache_write_1h_multiplier=0.0,
        fast_multipliers=present.fast_multipliers,
    )
    assert (KNOWN, "cache_write_1h") in provenance.claude_card_rate_violations(
        no_1h)

    entry = SimpleNamespace(
        model=KNOWN, input_tokens=10, output_tokens=10,
        cache_creation_tokens=300000, cache_read_tokens=250000,
        cache_1h_tokens=100000, speed=None,
    )
    card = present.claude_pricing[KNOWN]
    assert provenance.claude_entry_rates_positive(card, entry, present)
    for field in ("cache_read_input_token_cost",
                  "cache_creation_input_token_cost", "input_cost_per_token"):
        assert not provenance.claude_entry_rates_positive(
            {**card, field: 0.0}, entry, present), field
    tiered = {**card, "cache_read_input_token_cost_above_200k_tokens": 0.0}
    assert not provenance.claude_entry_rates_positive(tiered, entry, present)
    below_tier = SimpleNamespace(**{**vars(entry), "cache_read_tokens": 1000})
    assert provenance.claude_entry_rates_positive(tiered, below_tier, present)
    assert not provenance.claude_entry_rates_positive(card, entry, no_1h)


def test_mixed_closed_block_keeps_its_direct_card_amount(
    tmp_path, monkeypatch, capsys,
):
    """A missing model cannot license rewriting a direct model's close."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    old_cards = dict(present.claude_pricing)
    old_cards.pop(UNKNOWN)
    old_revision = _pricing_revision(
        pricing, claude_prices=old_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: old_revision)
    _seed_cache(mod, first_model=UNKNOWN)
    db = mod.open_cache_db()
    db.execute(
        "INSERT INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,input_tokens,"
        "output_tokens,cache_create_tokens,cache_read_tokens,"
        "cache_create_1h_tokens,account_key) "
        "VALUES (?,?,?,?,1000000,0,0,0,0,?)",
        ("/tmp/claude/projects/repo/869-session.jsonl", 50,
         "2026-07-25T11:56:00+00:00", KNOWN, "acct-869"),
    )
    db.commit()
    db.close()
    _observe(mod)
    capsys.readouterr()
    frozen = _closed_costs(mod)
    assert frozen[0][1] == pytest.approx(3.0)

    newer_cards = dict(present.claude_pricing)
    newer_cards[KNOWN] = {
        **newer_cards[KNOWN], "input_cost_per_token": 7e-6,
    }
    newer_revision = _pricing_revision(
        pricing, claude_prices=newer_cards, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer_revision)
    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 0
    assert preview["uncertainCostFactCount"] >= 1
    assert _closed_costs(mod) == frozen


def test_unknown_model_with_observed_cost_is_not_missing_pricing(
    tmp_path, monkeypatch, capsys,
):
    """A retained provider cost is real even without an embedded card."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    old_cards = dict(present.claude_pricing)
    old_cards.pop(UNKNOWN)
    old_revision = _pricing_revision(
        pricing, claude_prices=old_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: old_revision)
    _seed_cache(mod, first_model=UNKNOWN)
    db = mod.open_cache_db()
    db.execute("UPDATE session_entries SET cost_usd_raw=9.0 WHERE line_offset=0")
    db.commit()
    db.close()
    _observe(mod)
    capsys.readouterr()
    frozen = _closed_costs(mod)
    assert frozen[0][1] == pytest.approx(9.0)

    newer_revision = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer_revision)
    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["five_hour_block_close"]["supersede"] == 0
    assert _closed_costs(mod) == frozen


def test_codex_fallback_is_a_nonzero_missing_card_case(monkeypatch):
    """Codex fallback provenance cannot be inferred from a zero-cost check."""
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    assert CODEX_LATER_CARD in present.codex_pricing
    older_cards = dict(present.codex_pricing)
    older_cards.pop(CODEX_LATER_CARD)
    older = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=older_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: older)
    _, was_fallback = pricing._resolve_codex_pricing(CODEX_LATER_CARD)
    fallback_cost = pricing._calculate_codex_entry_cost(
        CODEX_LATER_CARD, 100000, 0, 0, 0, speed="standard",
    )
    assert was_fallback is True
    assert fallback_cost == pytest.approx(0.125)

    newer = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=present.codex_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer)
    _, is_fallback = pricing._resolve_codex_pricing(CODEX_LATER_CARD)
    direct_cost = pricing._calculate_codex_entry_cost(
        CODEX_LATER_CARD, 100000, 0, 0, 0, speed="standard",
    )
    assert is_fallback is False
    assert direct_cost == pytest.approx(0.2)
    assert direct_cost != fallback_cost


def test_codex_fallback_budget_fact_is_durable_and_reprices_after_card(
    tmp_path, monkeypatch, capsys,
):
    """A nonzero fallback spend is journaled by the real Codex budget writer."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_cache as cache
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    older_cards = dict(present.codex_pricing)
    older_cards.pop(CODEX_LATER_CARD)
    older = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=older_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: older)
    conn = mod.open_cache_db()
    conn.execute(
        "INSERT INTO codex_session_files "
        "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
        "VALUES (?,?,?,?,?)",
        ("/tmp/codex/869.jsonl", 100, 1, 100, AT[0]),
    )
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path,line_offset,timestamp_utc,session_id,model,"
        "input_tokens,cached_input_tokens,output_tokens,"
        "reasoning_output_tokens,total_tokens) "
        "VALUES (?,?,?,?,?,100000,0,0,0,100000)",
        ("/tmp/codex/869.jsonl", 0, "2026-07-25T11:55:00+00:00",
         "codex-869", CODEX_LATER_CARD),
    )
    conn.commit()
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
            "amount_usd": 0.125,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [100],
        }},
    }))

    def fire_budget(ctx):
        assert mod.maybe_record_codex_budget_milestone(
            {}, conn=ctx.conn, as_of=AT[0], alert_sink=ctx.pending_alerts,
            journal_ctx=ctx,
        ) == 1

    assert runtime.run_stats_ingest(
        mode="authoritative", codex_apply=fire_budget,
    ).ran is True
    capsys.readouterr()
    db = mod.open_db()
    try:
        before = tuple(db.execute(
            "SELECT spent_usd, consumption_pct, alerted_at, journal_id "
            "FROM budget_milestones WHERE vendor='codex'"
        ).fetchone())
    finally:
        db.close()
    assert before[0:2] == pytest.approx((0.125, 100.0))
    assert before[2] == AT[0]
    assert before[3]
    assert b'"spent_usd":0.125' in b"".join(_journal_bytes(mod).values())
    before_card = _rederive(mod, capsys, apply=False)
    assert before_card["actionCountsByEventKind"]["budget"]["supersede"] == 0

    newer = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=present.codex_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer)
    preview = _rederive(mod, capsys, apply=False)
    assert preview["actionCountsByEventKind"]["budget"]["supersede"] == 1
    original_journal = _journal_bytes(mod)
    db = mod.open_db()
    try:
        assert tuple(db.execute(
            "SELECT spent_usd, consumption_pct, alerted_at, journal_id "
            "FROM budget_milestones WHERE vendor='codex'"
        ).fetchone()) == before
    finally:
        db.close()

    # A changed retained population cannot be silently used to rewrite a
    # threshold that fired against a different set of source entries. The
    # crossing stays exactly as recorded and is disclosed as uncertain; the
    # family itself is not refused.
    cache_db = mod.open_cache_db()
    cache_db.execute(
        "UPDATE codex_session_entries SET input_tokens=200000 "
        "WHERE session_id='codex-869'"
    )
    cache_db.commit()
    cache_db.close()
    kept = _rederive(mod, capsys, apply=True)
    assert kept["status"] == "no-op", kept
    assert kept["uncertainCostFactCount"] == 1
    assert _kind_counts(kept, "budget")["supersede"] == 0
    assert _journal_bytes(mod) == original_journal
    db = mod.open_db()
    try:
        assert tuple(db.execute(
            "SELECT spent_usd, consumption_pct, alerted_at, journal_id "
            "FROM budget_milestones WHERE vendor='codex'"
        ).fetchone()) == before
    finally:
        db.close()
    cache_db = mod.open_cache_db()
    cache_db.execute(
        "UPDATE codex_session_entries SET input_tokens=100000 "
        "WHERE session_id='codex-869'"
    )
    cache_db.commit()
    cache_db.close()

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    for name, original in original_journal.items():
        assert _journal_bytes(mod)[name].startswith(original)
    db = mod.open_db()
    try:
        after = tuple(db.execute(
            "SELECT spent_usd, consumption_pct, alerted_at, journal_id "
            "FROM budget_milestones WHERE vendor='codex'"
        ).fetchone())
    finally:
        db.close()
    assert after[0:2] == pytest.approx((0.2, 160.0))
    assert after[2:] == before[2:]

    monkeypatch.setenv("CCTALLY_AS_OF", AT[0])
    assert mod.cmd_budget(argparse.Namespace(
        action=None, amount=None, project=None, vendor="codex", period=None,
        config=None, reveal_projects=False, tz=None, json=True,
        format=None, theme="light", no_branding=False, output=None,
        copy=False, open_after_write=False,
    )) == 0
    displayed = json.loads(capsys.readouterr().out)
    assert displayed["codex"]["spent_usd"] == pytest.approx(after[0])

    stable_journal = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert _journal_bytes(mod) == stable_journal

    independent = tmp_path / "codex-independent-stats.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent, update_quota_cache=False,
    )
    import sqlite3
    rebuilt = sqlite3.connect(independent)
    try:
        assert tuple(rebuilt.execute(
            "SELECT spent_usd, consumption_pct, alerted_at, journal_id "
            "FROM budget_milestones WHERE vendor='codex'"
        ).fetchone()) == after
    finally:
        rebuilt.close()


def test_legacy_codex_cost_facts_report_uncertainty_without_fabricated_reprice(
    tmp_path, monkeypatch, capsys,
):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_journal as journal

    mod.open_cache_db().close()
    at = AT[0]
    month = "2026-07-01T00:00:00+00:00"
    for kind, event_id, payload in (
        ("budget", "bm:legacy-codex", {
            "vendor": "codex", "account_key": "*",
            "period_start_at": month, "period": "calendar-month",
            "threshold": 100, "budget_usd": 0.125,
            "spent_usd": 0.125, "consumption_pct": 100.0,
            "crossed_at_utc": at, "alerted_at": at,
        }),
        ("projected", "pjm:legacy-codex", {
            "account_key": "*", "week_start_at": month,
            "period": "calendar-month", "metric": "codex_budget_usd",
            "threshold": 100, "projected_value": 0.125,
            "denominator": 0.125, "crossed_at_utc": at,
            "alerted_at": at,
        }),
    ):
        runtime.append_record(journal.make_evt(
            kind=kind, id=event_id, at=at, payload=payload,
        ), now_utc=dt.datetime(2026, 7, 25, tzinfo=dt.timezone.utc))
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        update_quota_cache=False,
    )
    before = _journal_bytes(mod)
    preview = _rederive(mod, capsys, apply=False)
    assert preview["uncertainCostFactCount"] == 2
    assert preview["actionCountsByEventKind"]["budget"]["supersede"] == 0
    assert preview["actionCountsByEventKind"]["projected"]["supersede"] == 0
    assert _journal_bytes(mod) == before


def test_codex_reprice_preserves_direct_card_contribution(
    tmp_path, monkeypatch,
):
    """A simultaneous edit to a genuine card cannot inflate a closed budget."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_cache as cache
    import _cctally_rederive as rederive
    import _lib_cost_provenance as provenance
    import _lib_journal as journal
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    old_cards = dict(present.codex_pricing)
    old_cards.pop(CODEX_LATER_CARD)
    old = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=old_cards, date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: old)
    db = mod.open_cache_db()
    db.execute(
        "INSERT INTO codex_session_files "
        "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
        "VALUES (?,?,?,?,?)",
        ("/tmp/codex/mixed-869.jsonl", 200, 1, 200, AT[0]),
    )
    for offset, model in ((0, CODEX_LATER_CARD), (100, "gpt-5")):
        db.execute(
            "INSERT INTO codex_session_entries "
            "(source_path,line_offset,timestamp_utc,session_id,model,"
            "input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,total_tokens) "
            "VALUES (?,?,?,?,?,100000,0,0,0,100000)",
            ("/tmp/codex/mixed-869.jsonl", offset,
             "2026-07-25T11:55:00+00:00", "mixed-869", model),
        )
    db.commit()
    start = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(AT[0].replace("Z", "+00:00"))
    entries = cache.iter_codex_entries(db, start, end)
    old_total, marker = provenance.codex_cost_with_provenance(
        entries, end=end, speed="standard",
    )
    assert marker["fallbackModels"] == [CODEX_LATER_CARD]
    event = journal.make_evt(
        kind="budget", id="bm:mixed-869", at=AT[0], payload={
            "vendor": "codex", "account_key": "*",
            "period_start_at": start.isoformat(),
            "period": "calendar-month", "threshold": 100,
            "budget_usd": old_total, "spent_usd": old_total,
            "consumption_pct": 100.0, "crossed_at_utc": AT[0],
            "alerted_at": AT[0], "_pricing": marker,
        },
    )

    newer_cards = dict(present.codex_pricing)
    newer_cards["gpt-5"] = {
        **newer_cards["gpt-5"],
        "input_cost_per_token": 2 * newer_cards["gpt-5"]["input_cost_per_token"],
    }
    newer = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=newer_cards, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: newer)
    desired = rederive._desired_codex_budget_costs(
        SimpleNamespace(by_id={event["id"]: SimpleNamespace(
            status="active", record=event,
        )}), db,
    )
    current_total, current_marker = provenance.codex_cost_with_provenance(
        entries, end=end, speed="standard",
    )
    expected = marker["directCostUsd"] + current_marker["modelCostsUsd"][
        CODEX_LATER_CARD]
    assert desired[0]["payload"]["spent_usd"] == pytest.approx(expected)
    assert desired[0]["payload"]["spent_usd"] != pytest.approx(current_total)
    revised_marker = desired[0]["payload"]["_pricing"]
    assert revised_marker["directCostUsd"] == pytest.approx(expected)
    assert revised_marker["fallbackModels"] == []
    assert revised_marker["modelCostsUsd"]["gpt-5"] == pytest.approx(
        marker["modelCostsUsd"]["gpt-5"]
    )
    assert sum(revised_marker["modelCostsUsd"].values()) == pytest.approx(
        desired[0]["payload"]["spent_usd"]
    )
    db.close()


# ---------------------------------------------------------------------------
# Historical (pre-marker) Claude closes: identified afterwards (#869 F2/Q1)
# ---------------------------------------------------------------------------

BLOCK_ONE_POINTS = (
    ("2026-07-25T10:30:00Z", RESETS[0], 1.0, 1.0),
    ("2026-07-25T11:30:00Z", RESETS[0], 2.0, 2.0),
    ("2026-07-25T12:00:00Z", RESETS[0], 3.0, 3.0),
)
BLOCK_TWO_POINT = ("2026-07-25T16:05:00Z", RESETS[1], 4.0, 1.0)


def _block_508_rows(count=158):
    """One model, 158 owned entries, every token class, a mixed TTL split."""
    start = dt.datetime(2026, 7, 25, 10, 0, 30, tzinfo=dt.timezone.utc)
    rows = []
    for index in range(count):
        stamp = (start + dt.timedelta(seconds=45 * index)).isoformat()
        rows.append((
            index * 10, stamp, UNKNOWN,
            2 + index % 3, 1000 + 10 * index, 3000 + index,
            1500 if index % 2 == 0 else 0, 190000 + 100 * index,
            "acct-869", None,
        ))
    return rows


def _record_unmarked_close(mod, monkeypatch, pricing, rows, *, points=None):
    """Record block one while the model has no card, then close it unmarked."""
    monkeypatch.setattr(
        pricing, "current_pricing_snapshot",
        lambda old=_without_card(pricing, UNKNOWN): old,
    )
    _insert_entries(mod, rows)
    _insert_entries(mod, [(
        900000, "2026-07-25T16:00:00+00:00", KNOWN, 1000000, 0, 0, 0, 0,
        "acct-869", None,
    )])
    _observe_points(mod, points or BLOCK_ONE_POINTS)
    _strip_open_block_marker(mod)
    _observe_points(mod, [BLOCK_TWO_POINT])
    [close] = [event for event in _events(mod)
               if (event.get("payload") or {}).get("kind")
               == "five_hour_block_close"]
    assert "_pricing" not in close["payload"]
    return close


def _block_rows(mod, window_key):
    conn = mod.open_db()
    try:
        block = conn.execute(
            "SELECT total_cost_usd, pricing_provenance_json FROM five_hour_blocks "
            "WHERE five_hour_window_key=?", (window_key,),
        ).fetchone()
        models = [tuple(row) for row in conn.execute(
            "SELECT model, cost_usd, entry_count FROM five_hour_block_models "
            "WHERE five_hour_window_key=? ORDER BY model", (window_key,),
        )]
        projects = [tuple(row) for row in conn.execute(
            "SELECT project_path, cost_usd FROM five_hour_block_projects "
            "WHERE five_hour_window_key=? ORDER BY project_path", (window_key,),
        )]
        milestones = [tuple(row) for row in conn.execute(
            "SELECT percent_threshold, block_cost_usd, marginal_cost_usd "
            "FROM five_hour_milestones WHERE five_hour_window_key=? "
            "ORDER BY percent_threshold", (window_key,),
        )]
        return block, models, projects, milestones
    finally:
        conn.close()


def test_historical_unpriced_close_is_corrected_from_retained_entries(
    tmp_path, monkeypatch, capsys,
):
    """Block 508's shape: one unmarked single-model $0 close, 158 entries.

    The model's card ships after the close. The retained entries prove every
    owned token was priced at $0 only because the card was missing, so the
    close, both child sets and its five-hour milestones are corrected once,
    and later card revisions leave them frozen.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    rows = _block_508_rows()
    close = _record_unmarked_close(mod, monkeypatch, pricing, rows)
    window_key = close["payload"]["five_hour_window_key"]
    assert close["payload"]["total_cost_usd"] == 0.0
    [model_child] = close["payload"]["_models"]
    assert model_child["model"] == UNKNOWN
    assert model_child["entry_count"] == 158
    before_block, _models, _projects, before_milestones = _block_rows(
        mod, window_key)
    assert before_block[0] == 0.0
    assert [row[1] for row in before_milestones] == [0.0, 0.0, 0.0]

    card_date = "2026-09-24"
    carded = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date=card_date,
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: carded)
    expected = sum(
        _entry_cost(pricing, UNKNOWN, *row[3:8]) for row in rows
    )
    assert expected > 0
    at_crossing = {
        threshold: sum(
            _entry_cost(pricing, UNKNOWN, *row[3:8]) for row in rows
            if dt.datetime.fromisoformat(row[1]) <= dt.datetime.fromisoformat(
                BLOCK_ONE_POINTS[threshold - 1][0].replace("Z", "+00:00"))
        )
        for threshold in (1, 2, 3)
    }

    preview = _rederive(mod, capsys, apply=False)
    assert preview["status"] == "preview", preview
    assert _kind_counts(preview, "five_hour_block_close")["supersede"] == 1
    assert _kind_counts(preview, "five_hour_milestone")["supersede"] == 3
    assert "uncertainCostFactCount" not in preview

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    block, models, projects, milestones = _block_rows(mod, window_key)
    assert block[0] == pytest.approx(expected)
    assert models == [(UNKNOWN, pytest.approx(expected), 158)]
    assert projects == [("/repo", pytest.approx(expected))]
    assert [row[1] for row in milestones] == pytest.approx(
        [at_crossing[1], at_crossing[2], at_crossing[3]])
    marker = json.loads(block[1])
    assert marker == {
        "version": 1,
        "basis": HISTORICAL_BASIS,
        "inferredModels": [UNKNOWN],
        "pricingDate": card_date,
        "sourceHash": marker["sourceHash"],
        "entryCount": 158,
        "unpricedModels": [],
    }
    assert marker["sourceHash"].startswith("sha256:")
    corrections = [
        record for record in _events(mod)
        if record.get("t") == "correction" and record.get("id") == close["id"]
    ]
    assert len(corrections) == 1
    assert corrections[0]["payload"]["_pricing"] == marker
    assert json.loads(corrections[0]["payload"]["pricing_provenance_json"]) == marker

    stable_journal = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert "uncertainCostFactCount" not in second
    assert _journal_bytes(mod) == stable_journal

    # A third card revision reprices weekly facts from the retained entries,
    # but the corrected close and its milestones are frozen monetary facts.
    revised_cards = dict(present.claude_pricing)
    revised_cards[UNKNOWN] = {
        key: value * 2 for key, value in revised_cards[UNKNOWN].items()
    }
    revised = _pricing_revision(
        pricing, claude_prices=revised_cards, date="2026-09-26",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revised)
    third = _rederive(mod, capsys, apply=False)
    assert _kind_counts(third, "five_hour_block_close")["supersede"] == 0
    assert _kind_counts(third, "five_hour_milestone")["supersede"] == 0
    assert _kind_counts(third, "weekly_cost_snapshot")["supersede"] >= 1
    third_applied = _rederive(mod, capsys, apply=True)
    assert third_applied["status"] == "applied", third_applied
    assert _block_rows(mod, window_key) == (block, models, projects, milestones)

    materialized = _materialized_costs(mod.DB_PATH)
    journal_before_rebuild = _journal_bytes(mod)
    assert mod.cmd_db_rebuild(argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert _journal_bytes(mod) == journal_before_rebuild
    assert _materialized_costs(mod.DB_PATH) == materialized
    assert _block_rows(mod, window_key) == (block, models, projects, milestones)


def test_unmarked_priced_close_is_not_counted_uncertain(
    tmp_path, monkeypatch, capsys,
):
    """Only a zero-cost positive-token model makes an unmarked close uncertain."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    _insert_entries(mod, [
        (0, "2026-07-25T11:55:00+00:00", KNOWN, 1000000, 0, 0, 0, 0,
         "acct-869", None),
        (100, "2026-07-25T16:00:00+00:00", KNOWN, 1000000, 0, 0, 0, 0,
         "acct-869", None),
    ])
    _observe(mod, indices=(0,))
    _strip_open_block_marker(mod)
    _observe(mod, indices=(1,))
    capsys.readouterr()
    assert _closed_costs(mod)[0][1] == pytest.approx(3.0)
    preview = _rederive(mod, capsys, apply=False)
    assert _kind_counts(preview, "five_hour_block_close")["supersede"] == 0
    assert "uncertainCostFactCount" not in preview


def _refusal_case(case, pricing, present):
    """Rows, card table and whether the close's population drifts later."""
    rows = [(0, "2026-07-25T11:55:00+00:00", UNKNOWN, 1000000, 500, 0, 0,
             250000, "acct-869", None)]
    cards = dict(present.claude_pricing)
    # An independent correction every case must still reach: a revised card
    # for the model that priced the (still open) second block's entry.
    cards[KNOWN] = {**cards[KNOWN], "input_cost_per_token": 7e-6}
    if case == "mixed":
        rows.append((50, "2026-07-25T11:56:00+00:00", KNOWN, 1000000, 0, 0,
                     0, 0, "acct-869", None))
    elif case == "raw_cost":
        rows[0] = rows[0][:-1] + (0.0,)
    elif case == "unresolved":
        cards.pop(UNKNOWN)
    elif case == "synthetic":
        rows[0] = rows[0][:2] + ("<synthetic>",) + rows[0][3:]
    elif case == "nonpositive_rate":
        cards[UNKNOWN] = {**cards[UNKNOWN], "cache_read_input_token_cost": 0.0}
    elif case == "account":
        rows.append((60, "2026-07-25T11:57:00+00:00", UNKNOWN, 1000, 0, 0, 0,
                     0, "acct-other", None))
    return rows, cards


@pytest.mark.parametrize("case", [
    "mixed", "raw_cost", "unresolved", "synthetic", "nonpositive_rate",
    "account", "model_drift", "project_drift",
])
def test_historical_close_refusal_is_local_to_that_close(
    tmp_path, monkeypatch, capsys, case,
):
    """Each failed proof keeps only its own close; other corrections apply."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    rows, cards = _refusal_case(case, pricing, present)
    close = _record_unmarked_close(
        mod, monkeypatch, pricing, rows,
        points=[(AT[0], RESETS[0], 1.0, 1.0)],
    )
    capsys.readouterr()
    frozen = _closed_costs(mod)
    if case == "mixed":
        assert frozen[0][1] == pytest.approx(3.0)
    else:
        assert frozen[0][1] == 0.0
    if case == "model_drift":
        _insert_entries(mod, [(70, "2026-07-25T11:58:00+00:00", UNKNOWN, 5,
                               0, 0, 0, 0, "acct-869", None)])
    elif case == "project_drift":
        cache = mod.open_cache_db()
        cache.execute("UPDATE session_files SET project_path='/moved'")
        cache.commit()
        cache.close()
    revision = _pricing_revision(
        pricing, claude_prices=cards, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revision)

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_block_close")["supersede"] == 0
    assert _kind_counts(applied, "weekly_cost_snapshot")["supersede"] >= 1
    assert applied["uncertainCostFactCount"] == 1
    assert _closed_costs(mod) == frozen
    assert close["id"] == frozen[0][2]


@pytest.mark.parametrize("drift", ["model", "project"])
def test_marked_close_population_drift_is_kept_and_counted(
    tmp_path, monkeypatch, capsys, drift,
):
    """A proven missing-card close with a changed population stays unchanged."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    before = _without_card(pricing, UNKNOWN)
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: before)
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod)
    capsys.readouterr()
    frozen = _closed_costs(mod)
    assert frozen[0][1] == 0.0
    if drift == "model":
        _insert_entries(mod, [(50, "2026-07-25T11:56:00+00:00", UNKNOWN, 7,
                               0, 0, 0, 0, "acct-869", None)])
    else:
        cache = mod.open_cache_db()
        cache.execute("UPDATE session_files SET project_path='/moved'")
        cache.commit()
        cache.close()
    carded = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: carded)

    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_block_close")["supersede"] == 0
    assert _kind_counts(applied, "weekly_cost_snapshot")["supersede"] >= 1
    assert applied["uncertainCostFactCount"] == 1
    assert _closed_costs(mod) == frozen


# ---------------------------------------------------------------------------
# Codex budget crossings (#869 F1, F6, F8)
# ---------------------------------------------------------------------------

def _seed_codex(mod, monkeypatch, rows):
    """Retained Codex rows: (offset, stamp, input_tokens)."""
    import _cctally_cache as cache

    conn = mod.open_cache_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
            "VALUES (?,?,?,?,?)",
            ("/tmp/codex/869.jsonl", 100000, 1, 100000, AT[0]),
        )
        for offset, stamp, tokens in rows:
            conn.execute(
                "INSERT INTO codex_session_entries "
                "(source_path,line_offset,timestamp_utc,session_id,model,"
                "input_tokens,cached_input_tokens,output_tokens,"
                "reasoning_output_tokens,total_tokens) "
                "VALUES (?,?,?,?,?,?,0,0,0,?)",
                ("/tmp/codex/869.jsonl", offset, stamp, "codex-869",
                 CODEX_LATER_CARD, tokens, tokens),
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


def _write_codex_budget(mod):
    mod.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "Etc/UTC"},
        "budget": {"codex": {
            "amount_usd": 0.125, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [100],
        }},
    }))


def _fire_codex_budget(mod, as_of):
    import _cctally_journal as runtime

    fired = []

    def fire(ctx):
        fired.append(mod.maybe_record_codex_budget_milestone(
            {}, conn=ctx.conn, as_of=as_of, alert_sink=ctx.pending_alerts,
            journal_ctx=ctx,
        ))

    assert runtime.run_stats_ingest(
        mode="authoritative", codex_apply=fire,
    ).ran is True
    return fired[0]


def _codex_budget_rows(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT threshold, spent_usd, consumption_pct, crossed_at_utc, "
            "alerted_at, journal_id FROM budget_milestones "
            "WHERE vendor='codex' ORDER BY journal_id"
        )]
    finally:
        conn.close()


def test_codex_population_drift_does_not_block_claude_correction(
    tmp_path, monkeypatch, capsys,
):
    """A still-fallback budget is kept before any population comparison."""
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    no_cards = _pricing_revision(
        pricing,
        claude_prices=_without_card(pricing, UNKNOWN).claude_pricing,
        codex_prices=_without_card(
            pricing, CODEX_LATER_CARD, codex=True).codex_pricing,
        date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: no_cards)
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod)
    _seed_codex(mod, monkeypatch, [(0, "2026-07-25T11:55:00+00:00", 100000)])
    _write_codex_budget(mod)
    assert _fire_codex_budget(mod, AT[0]) == 1
    capsys.readouterr()
    recorded = _codex_budget_rows(mod.DB_PATH)
    assert recorded[0][1] == pytest.approx(0.125)

    cache = mod.open_cache_db()
    cache.execute("UPDATE codex_session_entries SET input_tokens=200000")
    cache.commit()
    cache.close()

    # The Claude card ships; the Codex model is still priced by fallback.
    claude_card = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=no_cards.codex_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: claude_card)
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_block_close")["supersede"] == 1
    assert _kind_counts(applied, "budget")["supersede"] == 0
    assert "uncertainCostFactCount" not in applied
    assert _closed_costs(mod)[0][1] == pytest.approx(4.0)
    assert _codex_budget_rows(mod.DB_PATH) == recorded

    # Once the Codex card ships, the drifted population cannot license a new
    # amount: the crossing stays as recorded and is counted as uncertain.
    both = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=present.codex_pricing, date="2026-09-25",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: both)
    journal = _journal_bytes(mod)
    kept = _rederive(mod, capsys, apply=True)
    assert kept["status"] == "no-op", kept
    assert kept["uncertainCostFactCount"] == 1
    assert _journal_bytes(mod) == journal
    assert _codex_budget_rows(mod.DB_PATH) == recorded


def test_overpriced_codex_crossing_retires_and_refires_under_a_new_identity(
    tmp_path, monkeypatch, capsys,
):
    """Fallback overpriced a crossing; the real card puts it below threshold.

    The correction tombstones the false crossing. Rebuild dispatches nothing.
    Later genuine spend crosses once, under a distinct incarnation id, and a
    further rederive neither revives the retired crossing nor rewrites the new
    one.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    fallback = _without_card(pricing, CODEX_LATER_CARD, codex=True)
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: fallback)
    mod.open_db().close()
    _seed_codex(mod, monkeypatch, [(0, "2026-07-25T11:55:00+00:00", 100000)])
    _write_codex_budget(mod)
    dispatched = []
    monkeypatch.setattr(runtime, "ALERT_DISPATCHER",
                        lambda alerts: dispatched.extend(alerts))
    assert _fire_codex_budget(mod, AT[0]) == 1
    assert len(dispatched) == 1
    [(threshold, spent, pct, crossed_at, _alerted, legacy_id)] = (
        _codex_budget_rows(mod.DB_PATH))
    assert (threshold, spent, pct) == (100, pytest.approx(0.125),
                                       pytest.approx(100.0))
    assert legacy_id.startswith("bm:")

    cheaper_cards = dict(present.codex_pricing)
    cheaper_cards[CODEX_LATER_CARD] = {
        **cheaper_cards[CODEX_LATER_CARD], "input_cost_per_token": 0.5e-6,
    }
    cheaper = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=cheaper_cards, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: cheaper)
    capsys.readouterr()
    preview = _rederive(mod, capsys, apply=False)
    assert _kind_counts(preview, "budget")["tombstone"] == 1, preview
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _codex_budget_rows(mod.DB_PATH) == []
    tombstones = [
        record for record in _events(mod)
        if record.get("t") == "correction" and record.get("id") == legacy_id
    ]
    assert [record["action"] for record in tombstones] == ["tombstone"]

    journal = _journal_bytes(mod)
    assert mod.cmd_db_rebuild(argparse.Namespace(db="stats", json=True)) == 0
    capsys.readouterr()
    assert _journal_bytes(mod) == journal
    assert _codex_budget_rows(mod.DB_PATH) == []
    assert len(dispatched) == 1

    # Genuine later spend: 300k tokens at the real card is $0.15 (120%).
    _seed_codex(mod, monkeypatch, [(100, "2026-07-25T12:30:00+00:00", 200000)])
    refire_at = "2026-07-25T13:00:00Z"
    assert _fire_codex_budget(mod, refire_at) == 1
    assert len(dispatched) == 2
    [row] = _codex_budget_rows(mod.DB_PATH)
    assert row[1] == pytest.approx(0.15)
    new_id = row[5]
    assert new_id != legacy_id
    assert new_id.startswith("bm2:")
    assert new_id == "bm2:" + legacy_id[len("bm:"):] + ":" + row[3]
    assert row[3] == refire_at
    [refire] = [
        record for record in _events(mod)
        if record.get("t") == "evt" and record.get("id") == new_id
    ]
    assert refire["payload"]["journal_identity_version"] == 2
    assert refire["payload"]["_pricing"]["fallbackModels"] == []

    stable = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert _journal_bytes(mod) == stable

    independent = tmp_path / "refire-independent-stats.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent, update_quota_cache=False,
    )
    assert _codex_budget_rows(independent) == [row]
    assert len(dispatched) == 2
    # A repeated tick sees the recorded crossing and fires nothing more.
    assert _fire_codex_budget(mod, "2026-07-25T13:30:00Z") == 0
    assert len(dispatched) == 2


def _budget_event(event_id, model_costs, *, spent, fallback, direct,
                  entry_count=3):
    import _lib_journal as journal

    return journal.make_evt(
        kind="budget", id=event_id, at=AT[0], payload={
            "vendor": "codex", "account_key": "*",
            "period_start_at": "2026-07-01T00:00:00+00:00",
            "period": "calendar-month", "threshold": 100,
            "budget_usd": spent, "spent_usd": spent,
            "consumption_pct": 100.0, "crossed_at_utc": AT[0],
            "alerted_at": AT[0], "_pricing": {
                "version": 1, "pricingDate": "2026-09-22",
                "fallbackModels": fallback, "sourceHash": "sha256:" + "0" * 64,
                "entryCount": entry_count, "speed": "standard",
                "directCostUsd": direct, "modelCostsUsd": model_costs,
            },
        },
    )


def test_codex_split_check_is_scale_aware(tmp_path, monkeypatch):
    """Summation order cannot refuse a large spend; a real mismatch still does."""
    import math

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_rederive as rederive
    import _lib_rederive

    import random

    # The at-fire writer sums one interleaved entry stream into a total and
    # into per-model buckets; the two orders round differently at scale.
    rng = random.Random(0)
    spent = 0.0
    fallback_cost = 0.0
    costs = {}
    for _ in range(20000):
        model = rng.choice([CODEX_LATER_CARD, "gpt-5", "gpt-5-codex"])
        cost = rng.uniform(0, 5000)
        spent += cost
        costs[model] = costs.get(model, 0.0) + cost
        if model == CODEX_LATER_CARD:
            fallback_cost += cost
    direct = spent - fallback_cost
    # The premise: naive summation drifts beyond the old absolute tolerance.
    assert abs(sum(costs.values()) - spent) > 1e-9
    db = mod.open_cache_db()
    try:
        valid = _budget_event(
            "bm:large", costs, spent=spent, fallback=[CODEX_LATER_CARD],
            direct=direct,
        )
        kept = rederive._desired_codex_budget_costs(
            SimpleNamespace(by_id={valid["id"]: SimpleNamespace(
                status="active", record=valid)}),
            db,
        )
        assert [record["id"] for record in kept] == ["bm:large"]
        assert kept[0]["payload"] == valid["payload"]

        for bad_spent in (spent + 1.0, spent * 1.001):
            bad = _budget_event(
                "bm:bad", costs, spent=bad_spent,
                fallback=[CODEX_LATER_CARD], direct=direct,
            )
            with pytest.raises(_lib_rederive.RederiveDataGap,
                               match="incomplete at-fire"):
                rederive._desired_codex_budget_costs(
                    SimpleNamespace(by_id={bad["id"]: SimpleNamespace(
                        status="active", record=bad)}),
                    db, uncertain=set(),
                )
        small = _budget_event(
            "bm:small", {CODEX_LATER_CARD: 0.1, "gpt-5": 0.025}, spent=0.13,
            fallback=[CODEX_LATER_CARD], direct=0.025,
        )
        with pytest.raises(_lib_rederive.RederiveDataGap,
                           match="incomplete at-fire"):
            rederive._desired_codex_budget_costs(
                SimpleNamespace(by_id={small["id"]: SimpleNamespace(
                    status="active", record=small)}),
                db, uncertain=set(),
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Claude alert latches a correction retires (#869 V1/V2)
# ---------------------------------------------------------------------------

#: kind -> (latch table, legacy id prefix, re-fire incarnation id prefix)
_CLAUDE_LATCHES = {
    "budget": ("budget_milestones", "bm", "bm2"),
    "projected": ("projected_milestones", "pjm", "pjm2"),
    "project_budget": ("project_budget_milestones", "pbm", "pbm2"),
}


def _arm_claude_latch(mod, monkeypatch, kind):
    """Configure exactly one Claude latch axis that every tick finds crossed."""
    import _cctally_record as record

    budget = {"alert_thresholds": [100]}
    alerts = {"enabled": False}
    if kind == "budget":
        # $3 of retained spend by the first tick against a $1 calendar budget.
        budget.update(
            weekly_usd=1.0, alerts_enabled=True, period="calendar-month",
        )
    elif kind == "projected":
        alerts = {"enabled": True, "projected_enabled": True}
        monkeypatch.setattr(
            record, "_weekly_pct_week_avg_projection",
            lambda conn, now_utc, **_kw: (95.0, False),
        )
    else:
        budget.update(projects={"/repo": 1.0}, project_alerts_enabled=True)
        monkeypatch.setattr(
            mod, "_sum_cost_by_project", lambda *_a, **_kw: {"/repo": 3.0},
        )
        # The fixture's usage belongs to one account while the test HOME has
        # no active identity; resolve the project week from every account.
        real_window = mod._resolve_current_budget_window
        monkeypatch.setattr(
            mod, "_resolve_current_budget_window",
            lambda conn, now_utc, account_key=None: real_window(conn, now_utc),
        )
    mod.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "Etc/UTC"}, "alerts": alerts, "budget": budget,
    }))


def _latch_rows(path, table):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return [tuple(row) for row in conn.execute(
            f"SELECT journal_id, crossed_at_utc, alerted_at FROM {table} "
            "ORDER BY id"
        )]
    finally:
        conn.close()


@pytest.mark.parametrize("kind", sorted(_CLAUDE_LATCHES))
def test_claude_latch_recreated_after_rederive_is_stamped_not_resent(
    tmp_path, monkeypatch, capsys, kind,
):
    """A rederive retires each Claude latch; the next tick recreates it.

    Scratch replay has no historical alert config, so the correction batch
    tombstones every Claude latch. The still-crossed threshold crosses again on
    the next live tick of the same period. A revision-0 event under the legacy
    id can never outrank that tombstone, so the row journals under a version-2
    incarnation id. The user was already told about this crossing, so the row
    is stamped without a second notification.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime

    table, legacy_prefix, refire_prefix = _CLAUDE_LATCHES[kind]
    _arm_claude_latch(mod, monkeypatch, kind)
    dispatched = []
    monkeypatch.setattr(runtime, "ALERT_DISPATCHER",
                        lambda alerts: dispatched.extend(alerts))
    _seed_cache(mod, first_model=KNOWN)
    _observe(mod, indices=(0,))
    assert len(dispatched) == 1, dispatched
    [(legacy_id, crossed_at, alerted_at)] = _latch_rows(mod.DB_PATH, table)
    assert legacy_id.startswith(legacy_prefix + ":")
    assert alerted_at == crossed_at == AT[0]

    capsys.readouterr()
    preview = _rederive(mod, capsys, apply=False)
    assert _kind_counts(preview, kind)["tombstone"] == 1, preview
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _latch_rows(mod.DB_PATH, table) == []

    # The next live tick of the same period still finds the threshold crossed.
    _observe(mod, indices=(1,))
    [(new_id, recrossed_at, realerted_at)] = _latch_rows(mod.DB_PATH, table)
    assert recrossed_at == AT[1]
    assert new_id == (
        refire_prefix + ":" + legacy_id[len(legacy_prefix) + 1:]
        + ":" + recrossed_at
    )
    assert realerted_at == recrossed_at
    assert len(dispatched) == 1, dispatched
    [refire] = [
        record for record in _events(mod)
        if record.get("t") == "evt" and record.get("id") == new_id
    ]
    assert refire["payload"]["journal_identity_version"] == 2
    assert refire["payload"]["alerted_at"] == recrossed_at

    independent = tmp_path / "latch-independent-stats.db"
    runtime.rebuild_stats_index(
        context=runtime.RebuildContext(trigger="test-fixture"),
        target_path=independent, update_quota_cache=False,
    )
    assert _latch_rows(independent, table) == [
        (new_id, recrossed_at, realerted_at)]
    # A later tick of the same period sees the recorded latch.
    _observe_points(mod, [("2026-07-25T16:30:00Z", RESETS[1], 2.0, 1.0)])
    assert _latch_rows(mod.DB_PATH, table) == [
        (new_id, recrossed_at, realerted_at)]
    assert len(dispatched) == 1, dispatched


# ---------------------------------------------------------------------------
# Five-hour milestones of corrected closes (#869 V3/V4)
# ---------------------------------------------------------------------------

RECORDED_BASIS = "computation-time-missing-card"


def _five_hour_alert_latches(mod, window_key):
    conn = mod.open_db()
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT percent_threshold, alerted_at FROM five_hour_milestones "
            "WHERE five_hour_window_key=? ORDER BY percent_threshold",
            (window_key,),
        )]
    finally:
        conn.close()


def _doubled_card(pricing, present, model, *, date):
    cards = dict(present.claude_pricing)
    cards[model] = {key: value * 2 for key, value in cards[model].items()}
    return _pricing_revision(pricing, claude_prices=cards, date=date)


def test_five_hour_alert_latch_survives_close_correction_and_freeze(
    tmp_path, monkeypatch, capsys,
):
    """A corrected close's alerted five-hour milestones keep their latch.

    Scratch replay runs without alert config, so its milestones carry no
    `alerted_at`. The correction carries the recorded latch, as the percent
    milestone path does, and the dependent-milestone freeze compares the
    milestone after that carry: a later card revision leaves it untouched.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_journal as runtime
    import _lib_pricing as pricing

    mod.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True, "five_hour_thresholds": [1, 2, 3],
    }}))
    dispatched = []
    monkeypatch.setattr(runtime, "ALERT_DISPATCHER",
                        lambda alerts: dispatched.extend(alerts))
    present = pricing.current_pricing_snapshot()
    close = _record_unmarked_close(mod, monkeypatch, pricing, _block_508_rows())
    window_key = close["payload"]["five_hour_window_key"]
    latched = _five_hour_alert_latches(mod, window_key)
    assert [row[0] for row in latched] == [1, 2, 3]
    assert all(row[1] for row in latched), latched
    notified = len(dispatched)
    assert notified >= 3

    carded = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: carded)
    capsys.readouterr()
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_milestone")["supersede"] == 3
    milestones = _block_rows(mod, window_key)[3]
    assert all(row[1] > 0 for row in milestones), milestones
    assert _five_hour_alert_latches(mod, window_key) == latched
    corrected = [
        record for record in _events(mod)
        if record.get("t") == "correction"
        and (record.get("payload") or {}).get("kind") == "five_hour_milestone"
        and record["payload"]["five_hour_window_key"] == window_key
    ]
    assert sorted(record["payload"]["alerted_at"] for record in corrected) == (
        sorted(row[1] for row in latched))

    stable = _journal_bytes(mod)
    second = _rederive(mod, capsys, apply=True)
    assert second["status"] == "no-op", second
    assert _journal_bytes(mod) == stable

    revised = _doubled_card(pricing, present, UNKNOWN, date="2026-09-26")
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revised)
    third = _rederive(mod, capsys, apply=False)
    assert _kind_counts(third, "five_hour_block_close")["supersede"] == 0
    assert _kind_counts(third, "five_hour_milestone")["supersede"] == 0, third
    assert _rederive(mod, capsys, apply=True)["status"] == "applied"
    assert _block_rows(mod, window_key)[3] == milestones
    assert _five_hour_alert_latches(mod, window_key) == latched
    assert len(dispatched) == notified


def test_record_time_corrected_close_freezes_its_milestones(
    tmp_path, monkeypatch, capsys,
):
    """A close corrected from its recorded missing-card marker stays frozen.

    The correction records a distinct basis, so a third card revision leaves
    the corrected close AND its five-hour milestones as corrected: a milestone
    can never outrun its frozen block.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    monkeypatch.setattr(
        pricing, "current_pricing_snapshot",
        lambda old=_without_card(pricing, UNKNOWN): old,
    )
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod)
    capsys.readouterr()
    [(window_key, cost, close_id)] = _closed_costs(mod)
    assert cost == 0.0

    carded = _pricing_revision(
        pricing, claude_prices=present.claude_pricing, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: carded)
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_block_close")["supersede"] == 1
    block, models, projects, milestones = _block_rows(mod, window_key)
    assert block[0] > 0
    assert milestones and all(row[1] > 0 for row in milestones), milestones

    revised = _doubled_card(pricing, present, UNKNOWN, date="2026-09-26")
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: revised)
    third = _rederive(mod, capsys, apply=False)
    assert _kind_counts(third, "five_hour_block_close")["supersede"] == 0
    assert _kind_counts(third, "five_hour_milestone")["supersede"] == 0, third
    assert _kind_counts(third, "weekly_cost_snapshot")["supersede"] >= 1
    assert _rederive(mod, capsys, apply=True)["status"] == "applied"
    assert _block_rows(mod, window_key) == (block, models, projects, milestones)

    marker = json.loads(block[1])
    assert marker == {
        "version": 1, "basis": RECORDED_BASIS, "pricingDate": "2026-09-24",
        "unpricedModels": [],
    }
    [correction] = [
        record for record in _events(mod)
        if record.get("t") == "correction" and record.get("id") == close_id
    ]
    assert correction["payload"]["_pricing"] == marker
    assert json.loads(correction["payload"]["pricing_provenance_json"]) == marker


# ---------------------------------------------------------------------------
# Codex budget crossings the plan cannot reprice (#869 V5/V6)
# ---------------------------------------------------------------------------

DIRECT_CODEX = "gpt-5-codex"


def test_codex_budget_that_lost_a_direct_card_is_kept_and_counted(
    tmp_path, monkeypatch, capsys,
):
    """A model priced directly at fire lost its card: keep, count, go on.

    The fallback model now has a card, but a model that had one at fire no
    longer does, so the plan cannot reprice the crossing truthfully. It stays
    exactly as recorded and is counted, and the Claude correction in the same
    plan still applies.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing

    present = pricing.current_pricing_snapshot()
    at_fire = _pricing_revision(
        pricing,
        claude_prices=_without_card(pricing, UNKNOWN).claude_pricing,
        codex_prices=_without_card(
            pricing, CODEX_LATER_CARD, codex=True).codex_pricing,
        date="2026-09-22",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: at_fire)
    assert not pricing._is_codex_fallback(DIRECT_CODEX)
    _seed_cache(mod, first_model=UNKNOWN)
    _observe(mod)
    _seed_codex(mod, monkeypatch, [(0, "2026-07-25T11:55:00+00:00", 100000)])
    cache = mod.open_cache_db()
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path,line_offset,timestamp_utc,session_id,model,"
        "input_tokens,cached_input_tokens,output_tokens,"
        "reasoning_output_tokens,total_tokens) "
        "VALUES (?,?,?,?,?,?,0,0,0,?)",
        ("/tmp/codex/869.jsonl", 50, "2026-07-25T11:56:00+00:00",
         "codex-869", DIRECT_CODEX, 1000, 1000),
    )
    cache.commit()
    cache.close()
    _write_codex_budget(mod)
    assert _fire_codex_budget(mod, AT[0]) == 1
    recorded = _codex_budget_rows(mod.DB_PATH)
    [fired] = [
        record for record in _events(mod)
        if record.get("t") == "evt" and record.get("id") == recorded[0][5]
    ]
    assert fired["payload"]["_pricing"]["fallbackModels"] == [CODEX_LATER_CARD]
    assert DIRECT_CODEX in fired["payload"]["_pricing"]["modelCostsUsd"]

    codex_cards = dict(present.codex_pricing)
    codex_cards.pop(DIRECT_CODEX)
    later = _pricing_revision(
        pricing, claude_prices=present.claude_pricing,
        codex_prices=codex_cards, date="2026-09-24",
    )
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: later)
    assert pricing._is_codex_fallback(DIRECT_CODEX)
    assert not pricing._is_codex_fallback(CODEX_LATER_CARD)
    capsys.readouterr()
    applied = _rederive(mod, capsys, apply=True)
    assert applied["status"] == "applied", applied
    assert _kind_counts(applied, "five_hour_block_close")["supersede"] == 1
    assert _kind_counts(applied, "budget")["supersede"] == 0
    assert _kind_counts(applied, "budget")["tombstone"] == 0
    assert applied["uncertainCostFactCount"] == 1
    assert _closed_costs(mod)[0][1] > 0
    assert _codex_budget_rows(mod.DB_PATH) == recorded


def test_retired_codex_crossing_is_not_revived_without_claude_observations(
    tmp_path, monkeypatch, capsys,
):
    """No Claude observation is retained, yet a retired crossing stays retired.

    With no retained observation every owned event is held as un-re-derivable
    history — except a Codex budget crossing, whose desired state is its own
    recorded pricing evidence. Holding it would keep a crossing the correction
    puts below its threshold, and would revive it once retired.
    """
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_pricing as pricing
    import _lib_rederive

    present = pricing.current_pricing_snapshot()
    fallback = _without_card(pricing, CODEX_LATER_CARD, codex=True)
    monkeypatch.setattr(pricing, "current_pricing_snapshot", lambda: fallback)
    mod.open_db().close()
    _seed_codex(mod, monkeypatch, [(0, "2026-07-25T11:55:00+00:00", 100000)])
    _write_codex_budget(mod)
    assert _fire_codex_budget(mod, AT[0]) == 1
    assert not [record for record in _events(mod) if record.get("t") == "obs"]
    [(*_fields, legacy_id)] = _codex_budget_rows(mod.DB_PATH)

    cheaper_cards = dict(present.codex_pricing)
    cheaper_cards[CODEX_LATER_CARD] = {
        **cheaper_cards[CODEX_LATER_CARD], "input_cost_per_token": 0.5e-6,
    }
    monkeypatch.setattr(
        pricing, "current_pricing_snapshot",
        lambda cheaper=_pricing_revision(
            pricing, claude_prices=present.claude_pricing,
            codex_prices=cheaper_cards, date="2026-09-24",
        ): cheaper,
    )
    capsys.readouterr()
    preview = _rederive(mod, capsys, apply=False)
    assert preview["preservedEventCount"] == 0, preview
    assert _kind_counts(preview, "budget")["tombstone"] == 1, preview
    assert _rederive(mod, capsys, apply=True)["status"] == "applied"
    assert _codex_budget_rows(mod.DB_PATH) == []
    records = _events(mod)
    assert legacy_id not in _lib_rederive.preserved_history(
        records, evidence_retained=False)

    retired = _journal_bytes(mod)
    again = _rederive(mod, capsys, apply=True)
    assert again["status"] == "no-op", again
    assert again["preservedEventCount"] == 0
    assert _kind_counts(again, "budget")["supersede"] == 0
    assert _journal_bytes(mod) == retired
    assert _codex_budget_rows(mod.DB_PATH) == []
