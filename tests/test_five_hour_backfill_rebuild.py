"""Regression coverage for rebuild-only five-hour block projection."""

from __future__ import annotations

from conftest import load_script, redirect_paths


def test_rebuild_backfill_materializes_only_latest_window_per_account(
    monkeypatch, tmp_path,
):
    """A tombstoned historical block must not return as an unstamped row.

    Rebuild replays closed ``five_hour_block_close`` events first, then asks
    the backfill for the one trailing open projection per account. If the
    backfill selects every missing window, a deliberately absent historical
    block is recreated with ``journal_id IS NULL`` and the next harvest tries
    to append its obsolete revision-0 event over the effective correction.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(
        ns, "get_claude_session_entries", lambda *_args, **_kwargs: [],
    )

    conn = ns["open_db"]()
    try:
        rows = [
            (
                "2026-04-13T11:32:21Z",
                "2026-04-13",
                "2026-04-20",
                "2026-04-13T00:00:00Z",
                "2026-04-20T00:00:00Z",
                16.0,
                5.0,
                "2026-04-13T16:00:00Z",
                1_776_096_000,
                "acct-a",
            ),
            (
                "2026-07-29T08:00:00Z",
                "2026-07-29",
                "2026-08-05",
                "2026-07-29T00:00:00Z",
                "2026-08-05T00:00:00Z",
                1.0,
                2.0,
                "2026-07-29T12:00:00Z",
                1_785_307_200,
                "acct-a",
            ),
        ]
        conn.executemany(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            " account_key, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', '{}')",
            rows,
        )
        conn.commit()

        inserted = ns["_backfill_five_hour_blocks"](
            conn, only_missing=True,
        )
        materialized = conn.execute(
            "SELECT five_hour_window_key, is_closed, journal_id "
            "FROM five_hour_blocks ORDER BY five_hour_window_key"
        ).fetchall()

        assert inserted == 1
        assert [tuple(row) for row in materialized] == [
            (1_785_307_200, 0, None),
        ]
    finally:
        conn.close()
