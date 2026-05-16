"""Per-site regression tests for spec §3.3 (Codex r1 finding 2).

Each test exercises ONE of the six sites that need ``reset_event_id``
threading:

* Site A — ``MAX(percent_threshold)`` lookup at
  ``bin/_cctally_record.py:973``.
* Site B — prior-cost lookup at ``bin/_cctally_record.py:993``.
* Site C — INSERT OR IGNORE at ``bin/_cctally_record.py:1005``.
* Site D — ``alerted_at`` UPDATE at ``bin/_cctally_record.py:1065``.
* Site E — alert payload reread at ``bin/_cctally_record.py:1077``.
* Site F — dashboard alerts list at ``bin/_cctally_dashboard.py:2609``.

Sites A-E are exercised via ``maybe_update_five_hour_block`` called
directly from a synthesized ``saved`` dict — this isolates the milestone
detection path from the surrounding ``cmd_record_usage`` wiring and lets
us assert pre-vs-post-credit segment behavior with seeded fixtures.

Site F asserts the dashboard alerts envelope renders BOTH pre-credit
(seg=0) and post-credit (seg=N) crossings of the same threshold as
distinct rows with widened row identities (bucket-C pattern per spec
§3.2 — row-identity widening, NOT filter).
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_block_and_event(
    ns,
    *,
    window_key: int,
    resets_at_iso: str,
    block_start_at: str,
    last_observed_at_utc: str,
    pre_threshold_max: int,
    pre_credit_block_cost_per_pct: float = 0.5,
    credit_effective_iso: str | None = None,
    credit_prior_pct: float = 28.0,
    credit_post_pct: float = 8.0,
    credit_detected_at: str | None = None,
) -> tuple[int, int]:
    """Seed:
      * one ``five_hour_blocks`` row,
      * ``pre_threshold_max`` ``five_hour_milestones`` rows at seg=0
        (1..pre_threshold_max), each with ``block_cost_usd`` =
        ``pre_credit_block_cost_per_pct * threshold``,
      * one ``five_hour_reset_events`` row (so ``_resolve_active`` returns
        its id).

    Returns ``(block_id, event_id)``.
    """
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (window_key, resets_at_iso, block_start_at, block_start_at,
             last_observed_at_utc, float(pre_threshold_max),
             block_start_at, last_observed_at_utc),
        )
        block_id = conn.execute(
            "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?",
            (window_key,),
        ).fetchone()["id"]

        for t in range(1, pre_threshold_max + 1):
            conn.execute(
                "INSERT INTO five_hour_milestones "
                "(block_id, five_hour_window_key, percent_threshold, "
                " captured_at_utc, usage_snapshot_id, block_cost_usd, "
                " marginal_cost_usd, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (block_id, window_key, t, block_start_at, 100 + t,
                 float(t) * pre_credit_block_cost_per_pct,
                 None if t == 1 else pre_credit_block_cost_per_pct,
                 0),
            )

        effective_iso = credit_effective_iso or last_observed_at_utc
        detected_at = credit_detected_at or last_observed_at_utc
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (detected_at, window_key, credit_prior_pct, credit_post_pct,
             effective_iso),
        )
        event_id = conn.execute(
            "SELECT id FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ? ORDER BY id DESC LIMIT 1",
            (window_key,),
        ).fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    return int(block_id), int(event_id)


def _saved_dict(
    *,
    snapshot_id: int = 999,
    captured_at: str,
    weekly_percent: float,
    five_hour_percent: float,
    five_hour_resets_at_iso: str,
    five_hour_window_key: int,
) -> dict:
    """Build a ``saved`` dict matching ``maybe_update_five_hour_block``'s
    expected shape (the same dict ``insert_usage_snapshot`` would produce).
    """
    return {
        "id": snapshot_id,
        "capturedAt": captured_at,
        "weeklyPercent": weekly_percent,
        "fiveHourPercent": five_hour_percent,
        "fiveHourResetsAt": five_hour_resets_at_iso,
        "fiveHourWindowKey": five_hour_window_key,
    }


# ── Site A — MAX(percent_threshold) scopes to segment ──────────────────


def test_site_a_max_threshold_scopes_to_segment(ns, tmp_path):
    """Post-credit climb resumes milestone emission. Without Site-A
    threading, ``MAX(percent_threshold)`` returns the pre-credit max
    (28) and post-credit thresholds 1..28 are never emitted — a
    silent data-loss bug.

    With segment threading, ``MAX`` over (window_key=W, reset_event_id=N)
    returns ``None`` for the fresh segment so ``start_threshold =
    current_floor`` (first-observation path) and the first post-credit
    crossing at 10% emits exactly the threshold 10 row.
    """
    window_key = 1746550800
    block_id, event_id = _seed_block_and_event(
        ns,
        window_key=window_key,
        resets_at_iso="2026-05-16T19:30:00+00:00",
        block_start_at="2026-05-16T14:30:00+00:00",
        last_observed_at_utc="2026-05-16T17:00:00Z",
        pre_threshold_max=28,
        credit_effective_iso="2026-05-16T17:00:00+00:00",
    )
    saved = _saved_dict(
        captured_at="2026-05-16T17:05:00Z",
        weekly_percent=42.0,
        five_hour_percent=10.0,
        five_hour_resets_at_iso="2026-05-16T19:30:00+00:00",
        five_hour_window_key=window_key,
    )
    ns["maybe_update_five_hour_block"](saved)

    conn = ns["open_db"]()
    try:
        seg0 = [
            r["percent_threshold"]
            for r in conn.execute(
                "SELECT percent_threshold FROM five_hour_milestones "
                "WHERE reset_event_id = 0 "
                "ORDER BY percent_threshold"
            ).fetchall()
        ]
        seg1 = [
            r["percent_threshold"]
            for r in conn.execute(
                "SELECT percent_threshold FROM five_hour_milestones "
                "WHERE reset_event_id = ? "
                "ORDER BY percent_threshold",
                (event_id,),
            ).fetchall()
        ]
        assert seg0 == list(range(1, 29)), (
            f"pre-credit segment intact; got {seg0}"
        )
        # Post-credit first observation: segment-scoped MAX returns None,
        # so start_threshold = current_floor = 10 — only that one row
        # emitted (not 1..10).
        assert seg1 == [10], (
            "post-credit first-observation: only current floor emitted "
            f"(max_existing=None branch); got {seg1}"
        )
    finally:
        conn.close()


# ── Site B — prior-cost lookup scopes to segment ───────────────────────


def test_site_b_prior_cost_scoped_to_segment(ns, tmp_path):
    """Post-credit threshold (N+1) marginal_cost arithmetic must use
    in-segment threshold N's cost, not the pre-credit threshold N row.

    Without Site-B threading the prior-cost lookup returns the
    pre-credit row at threshold N (synthetic block_cost_usd = N * 0.5),
    and ``marginal = totals['cost_usd'] - that_cost`` confuses arithmetic
    across segments. With segment threading the lookup is scoped to
    seg=N for the active block so marginal stays within-segment.
    """
    window_key = 1746550800
    block_id, event_id = _seed_block_and_event(
        ns,
        window_key=window_key,
        resets_at_iso="2026-05-16T19:30:00+00:00",
        block_start_at="2026-05-16T14:30:00+00:00",
        last_observed_at_utc="2026-05-16T17:00:00Z",
        pre_threshold_max=28,
        credit_effective_iso="2026-05-16T17:00:00+00:00",
    )

    # First post-credit observation at 10% — seg=N threshold-10 lands.
    saved1 = _saved_dict(
        snapshot_id=200, captured_at="2026-05-16T17:05:00Z",
        weekly_percent=42.0, five_hour_percent=10.0,
        five_hour_resets_at_iso="2026-05-16T19:30:00+00:00",
        five_hour_window_key=window_key,
    )
    ns["maybe_update_five_hour_block"](saved1)

    # Second post-credit observation at 11% — seg=N threshold-11 lands;
    # its marginal_cost is computed via Site-B lookup of seg=N threshold-10's
    # block_cost_usd, NOT against the pre-credit threshold-10 row whose
    # synthetic block_cost is 10 * 0.5 = 5.0.
    saved2 = _saved_dict(
        snapshot_id=201, captured_at="2026-05-16T17:10:00Z",
        weekly_percent=42.0, five_hour_percent=11.0,
        five_hour_resets_at_iso="2026-05-16T19:30:00+00:00",
        five_hour_window_key=window_key,
    )
    ns["maybe_update_five_hour_block"](saved2)

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT block_cost_usd, marginal_cost_usd FROM five_hour_milestones "
            "WHERE percent_threshold = 11 AND reset_event_id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None, (
            "post-credit threshold-11 must exist (seg-scoped Site C insert)"
        )
        # marginal_cost == block_cost_now - seg=N threshold-10's block_cost.
        # Both come from _compute_block_totals (likely 0.0 in tmp HOME
        # with no JSONL), so marginal is 0.0 - 0.0 = 0.0. The pre-credit
        # threshold-10 row had synthetic block_cost = 5.0 — if Site B
        # leaked across segments the marginal would be 0.0 - 5.0 = -5.0.
        # Assert the value is NOT a leakage signature.
        assert row["marginal_cost_usd"] is not None
        assert row["marginal_cost_usd"] != -5.0, (
            "marginal_cost must not be computed against pre-credit row"
        )
        # And the new row's block_cost is the seg-N value, not the
        # pre-credit synthetic 11 * 0.5 = 5.5.
        assert row["block_cost_usd"] != 5.5, (
            "post-credit row's cost must not equal pre-credit synthetic"
        )
    finally:
        conn.close()


# ── Site C — INSERT writes resolved reset_event_id ─────────────────────


def test_site_c_insert_stamps_active_segment(ns, tmp_path):
    """Site C: new milestone INSERT carries the resolved
    ``reset_event_id`` (event.id for the post-credit segment).
    """
    window_key = 1746550800
    block_id, event_id = _seed_block_and_event(
        ns,
        window_key=window_key,
        resets_at_iso="2026-05-16T19:30:00+00:00",
        block_start_at="2026-05-16T14:30:00+00:00",
        last_observed_at_utc="2026-05-16T17:00:00Z",
        pre_threshold_max=28,
        credit_effective_iso="2026-05-16T17:00:00+00:00",
    )
    saved = _saved_dict(
        captured_at="2026-05-16T17:05:00Z",
        weekly_percent=42.0, five_hour_percent=10.0,
        five_hour_resets_at_iso="2026-05-16T19:30:00+00:00",
        five_hour_window_key=window_key,
    )
    ns["maybe_update_five_hour_block"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "  FROM five_hour_milestones "
            " WHERE percent_threshold = 10 "
            " ORDER BY reset_event_id"
        ).fetchall()
        segs = [int(r["reset_event_id"]) for r in rows]
        # Pre-credit seg=0 at threshold 10 stays; the new post-credit
        # row at threshold 10 lands as seg=event_id. UNIQUE(window_key,
        # threshold, reset_event_id) admits both.
        assert segs == [0, event_id], (
            f"both pre- and post-credit rows at threshold 10 must exist; "
            f"got segs {segs}"
        )
    finally:
        conn.close()


# ── Sites D + E — alerted_at UPDATE + payload reread scoped to segment ──


def test_sites_d_e_post_credit_alert_targets_new_segment(ns, tmp_path):
    """Site D: alerted_at UPDATE targets the post-credit row (not the
    pre-credit row at the same threshold that was already alerted).

    Site E: the alert payload reread reads the post-credit row's
    block_cost, not the pre-credit row's stale value.

    Pre-credit threshold-10 has alerted_at='<prior>' and synthetic
    block_cost = 5.0; post-credit threshold-10 must fire fresh.
    """
    # Enable alerts at 5h threshold 10. Both threshold lists must be
    # non-empty per _validate_threshold_list (the weekly one isn't
    # exercised in this test — just present to satisfy validation).
    cfg_path = ns["CONFIG_PATH"]
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "alerts": {
            "enabled": True,
            "five_hour_thresholds": [10],
            "weekly_thresholds": [50],
        }
    }))
    window_key = 1746550800
    block_id, event_id = _seed_block_and_event(
        ns,
        window_key=window_key,
        resets_at_iso="2026-05-16T19:30:00+00:00",
        block_start_at="2026-05-16T14:30:00+00:00",
        last_observed_at_utc="2026-05-16T17:00:00Z",
        pre_threshold_max=28,
        credit_effective_iso="2026-05-16T17:00:00+00:00",
    )
    # Pre-credit threshold-10 already has alerted_at set.
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE five_hour_milestones SET alerted_at = ? "
            "WHERE five_hour_window_key = ? "
            "  AND percent_threshold = ? "
            "  AND reset_event_id = 0",
            ("2026-05-16T16:00:00Z", window_key, 10),
        )
        conn.commit()
    finally:
        conn.close()

    saved = _saved_dict(
        captured_at="2026-05-16T17:05:00Z",
        weekly_percent=42.0, five_hour_percent=10.0,
        five_hour_resets_at_iso="2026-05-16T19:30:00+00:00",
        five_hour_window_key=window_key,
    )
    ns["maybe_update_five_hour_block"](saved)

    conn = ns["open_db"]()
    try:
        pre = conn.execute(
            "SELECT alerted_at FROM five_hour_milestones "
            "WHERE five_hour_window_key = ? "
            "  AND percent_threshold = ? "
            "  AND reset_event_id = 0",
            (window_key, 10),
        ).fetchone()
        post = conn.execute(
            "SELECT alerted_at, block_cost_usd FROM five_hour_milestones "
            "WHERE five_hour_window_key = ? "
            "  AND percent_threshold = ? "
            "  AND reset_event_id = ?",
            (window_key, 10, event_id),
        ).fetchone()
        # Pre-credit row's alerted_at preserved (was '<prior>'), not
        # overwritten by the Site-D UPDATE which must target seg=event_id.
        assert pre is not None
        assert pre["alerted_at"] == "2026-05-16T16:00:00Z", (
            "pre-credit alerted_at must NOT be touched by post-credit "
            "alert UPDATE"
        )
        # Post-credit row exists and fired fresh (its alerted_at is set).
        assert post is not None, "post-credit row must exist (Site C)"
        assert post["alerted_at"] is not None, (
            "post-credit alerted_at must be set independently of pre-credit's"
        )
        # Site E: post-credit row's cost is NOT the pre-credit synthetic 5.0.
        assert post["block_cost_usd"] != 5.0, (
            "post-credit row's cost must reflect post-credit totals, "
            "not pre-credit reread"
        )
    finally:
        conn.close()


# ── Site F — Dashboard alerts list row-identity widening ──────────────


def test_site_f_dashboard_alerts_widens_id_not_filters(ns, tmp_path):
    """Both pre-credit (seg=0) and post-credit (seg=N) crossings of the
    same threshold must appear in the alerts envelope as distinct rows
    with widened ids (``five_hour:{key}:{threshold}:{reset_event_id}``).

    Mirrors the weekly precedent at ``bin/_cctally_dashboard.py:2597``.
    Per Codex r2 finding 2 / spec §3.3 site F: widen identity, do NOT
    filter — older clients tolerate longer ids (id is opaque).
    """
    window_key = 1746550800
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (window_key, "2026-05-16T19:30:00+00:00",
             "2026-05-16T14:30:00+00:00", "2026-05-16T14:35:00Z",
             "2026-05-16T17:30:00Z", 10.0,
             "2026-05-16T14:35:00Z", "2026-05-16T17:30:00Z"),
        )
        block_id = conn.execute(
            "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?",
            (window_key,),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-05-16T17:00:00Z", window_key, 28.0, 8.0,
             "2026-05-16T17:00:00+00:00"),
        )
        event_id = conn.execute(
            "SELECT id FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ? ORDER BY id DESC LIMIT 1",
            (window_key,),
        ).fetchone()["id"]
        # Pre-credit alert (seg=0) at threshold 10:
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, block_cost_usd, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, window_key, 10, "2026-05-16T16:00:00Z",
             100, 5.0, "2026-05-16T16:05:00Z", 0),
        )
        # Post-credit alert (seg=event_id) at threshold 10:
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, block_cost_usd, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, window_key, 10, "2026-05-16T17:20:00Z",
             101, 1.5, "2026-05-16T17:25:00Z", event_id),
        )
        conn.commit()

        dashboard_mod = ns["_cctally_dashboard"]
        envelope = dashboard_mod._build_alerts_envelope_array(conn)
        fh = [a for a in envelope if a.get("axis") == "five_hour"
              and a.get("threshold") == 10
              and a.get("context", {}).get("five_hour_window_key") == window_key]
        assert len(fh) == 2, (
            "both pre- and post-credit alerted rows must surface "
            f"(got {len(fh)}: {fh})"
        )
        ids = [a["id"] for a in fh]
        assert len(set(ids)) == 2, (
            f"alerts envelope ids must be unique across segments; got {ids}"
        )
        assert all(s.startswith(f"five_hour:{window_key}:10:") for s in ids), (
            f"ids must follow the widened shape; got {ids}"
        )
        # The two segment suffixes are {0, event_id}.
        seg_suffixes = sorted(int(s.rsplit(":", 1)[1]) for s in ids)
        assert seg_suffixes == sorted({0, event_id}), (
            f"row identities must carry reset_event_id; got {ids}"
        )
        # Each row also exposes reset_event_id in its context block.
        ctx_segs = sorted(
            int(a["context"].get("reset_event_id", -1)) for a in fh
        )
        assert ctx_segs == sorted({0, event_id}), (
            "context.reset_event_id must surface for client-side filtering"
        )
    finally:
        conn.close()


def test_weekly_alerts_context_exposes_reset_event_id(ns, tmp_path):
    """Round-3 Item 3: weekly alerts envelope ``context`` block must
    expose ``reset_event_id`` in parallel to the 5h shape.

    Pre-Round-3 the weekly path widened the row identity (``id``
    string includes ``:{reset_event_id}``) but did NOT include the
    segment in the ``context`` block, while the 5h path did — asymmetric
    payload that forced downstream consumers to scrape the opaque
    ``id`` string to discriminate pre- vs post-credit crossings of the
    same (week, threshold). Round-3 adds ``context.reset_event_id`` to
    weekly so both axes mirror the same shape (parallel-not-identical
    consistency).
    """
    week_start_date = "2026-05-10"
    week_end_date = "2026-05-17"
    week_start_at = "2026-05-10T00:00:00+00:00"
    week_end_at = "2026-05-17T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        # Cost snapshot (the milestone-writer's marginal lookup; not
        # strictly required for the alerts envelope which reads
        # ``percent_milestones`` directly, but mirrors the weekly
        # precedent test setup at site F).
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, cost_usd, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T18:00:00Z", week_start_date, week_end_date,
             week_start_at, week_end_at, 12.0, "auto"),
        )
        # Reset event so the post-credit segment has a positive id.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-05-14T17:00:00Z", "2026-05-14T17:00:00+00:00",
             week_end_at, "2026-05-14T17:00:00+00:00"),
        )
        event_id = conn.execute(
            "SELECT id FROM week_reset_events "
            "WHERE new_week_end_at = ? ORDER BY id DESC LIMIT 1",
            (week_end_at,),
        ).fetchone()["id"]
        # Pre-credit alerted milestone (seg=0) at threshold 10:
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-13T10:00:00Z", week_start_date, week_end_date,
             week_start_at, week_end_at, 10, 5.0, 200, 200,
             "2026-05-13T10:05:00Z", 0),
        )
        # Post-credit alerted milestone (seg=event_id) at threshold 10:
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T11:00:00Z", week_start_date, week_end_date,
             week_start_at, week_end_at, 10, 1.5, 201, 201,
             "2026-05-15T11:05:00Z", event_id),
        )
        conn.commit()

        dashboard_mod = ns["_cctally_dashboard"]
        envelope = dashboard_mod._build_alerts_envelope_array(conn)
        weekly = [
            a for a in envelope
            if a.get("axis") == "weekly" and a.get("threshold") == 10
            and a.get("context", {}).get("week_start_date") == week_start_date
        ]
        assert len(weekly) == 2, (
            "both pre- and post-credit weekly alerts must surface "
            f"(got {len(weekly)}: {weekly})"
        )
        # Round-3 invariant: context.reset_event_id is exposed.
        ctx_segs = sorted(
            int(a["context"].get("reset_event_id", -1)) for a in weekly
        )
        assert ctx_segs == sorted({0, event_id}), (
            "context.reset_event_id must surface on weekly alerts "
            "for client-side filtering parity with 5h"
        )
        # Sanity: the post-credit row's context.reset_event_id is the
        # positive event id.
        post = [a for a in weekly
                if int(a["context"]["reset_event_id"]) == event_id]
        assert len(post) == 1
        assert int(post[0]["context"]["reset_event_id"]) == event_id
    finally:
        conn.close()
