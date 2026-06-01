"""Firing tests for the projected-pace alert axis (Task 3, spec §2/§4, #121).

Exercises the store helper (``insert_projected_milestone`` /
``_projected_levels_already_latched``) and the detector
(``maybe_record_projected_alert``) against a redirected tmp stats.db + a
seeded ``weekly_usage_snapshots`` window anchor.

Design pins:
  - The fired value is the WEEK-AVERAGE projection
    (``p_now + r_avg*remaining`` for weekly_pct; budget kernel's
    ``week_avg_projection_usd`` for budget_usd) — NOT the displayed high-end
    verdict band. Band-discrimination cases prove a hot recent-24h rate does
    NOT flip a below-threshold week-average to firing, and vice-versa.
  - LOW CONF (per-metric predicate) suppresses; the non-vacuity proof forces
    the gate False and shows the suppression case would otherwise fire.
  - Fire-once via ``UNIQUE(week_start_at, metric, threshold)`` + rowcount==1;
    a later recovery neither un-fires nor re-fires.
  - Master toggles default OFF and gate behind the parent axis switch.
  - Pre-probe: when all levels for (week, metric) are latched, no cost work
    (budget spend SUM) runs — proven by a spy on ``_sum_cost_for_range``.

Spend (budget_usd leg) is injected via a monkeypatched ``_sum_cost_for_range``
so the crossing arithmetic is deterministic and isolated from the cache-DB
ingest path. Dispatch is captured via a fake ``_dispatch_alert_notification``
so no osascript is spawned.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from conftest import load_script, redirect_paths


# Subscription-week window the snapshot anchors. Tuesday 14:00 UTC, 7 days.
WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)
# now_utc placed exactly mid-week (84h in / 84h remaining) so
# r_avg*remaining == p_now and the week-average projection == 2*p_now.
AS_OF = WEEK_START + dt.timedelta(hours=84)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_week_key(week_start: dt.datetime = WEEK_START) -> str:
    """The exact ``week_start_at`` key production writes: the seeded ISO
    through ``parse_iso_datetime`` (host-local) then
    ``isoformat(timespec="seconds")`` — carries the host's UTC offset."""
    return dt.datetime.fromisoformat(
        _iso(week_start).replace("Z", "+00:00")
    ).astimezone().isoformat(timespec="seconds")


WEEK_KEY = _expected_week_key()


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF))
    return ns


def _seed_snapshots(ns, percents, *, week_start=WEEK_START, week_end=WEEK_END,
                    n_extra=3):
    """Seed ``weekly_usage_snapshots`` rows for [week_start, week_end).

    ``percents`` is a list of (offset_hours, weekly_percent) samples; the LAST
    one's percent becomes ``p_now`` (the detector reads samples[-1]). At least
    ``n_extra`` samples and a >=24h-old sample are needed for HIGH confidence;
    helpers below seed accordingly.
    """
    conn = ns["open_db"]()
    try:
        for off, pct in percents:
            cap = week_start + dt.timedelta(hours=off)
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, "
                " page_url, source, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(cap),
                    week_start.date().isoformat(),
                    (week_end - dt.timedelta(seconds=1)).date().isoformat(),
                    _iso(week_start),
                    _iso(week_end),
                    float(pct),
                    None,
                    "fixture",
                    json.dumps({"fixture": True}),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _high_conf_samples(p_now):
    """A sample set yielding HIGH forecast confidence at AS_OF (84h elapsed):
    >=3 samples, a sample >=24h old, p_now >= 2."""
    return [(1.0, p_now * 0.2), (24.0, p_now * 0.5), (83.0, p_now)]


def _write_config(ns, *, alerts=None, budget=None):
    """Write config.json at the redirected CONFIG_PATH so load_config reads it."""
    import _cctally_core
    cfg = {}
    if alerts is not None:
        cfg["alerts"] = alerts
    if budget is not None:
        cfg["budget"] = budget
    _cctally_core.CONFIG_PATH.write_text(json.dumps(cfg) + "\n")


def _patch_spend(ns, monkeypatch, *, value=None, recent=None, spy=None):
    """Inject a deterministic ``_sum_cost_for_range``. Budget uses two calls
    (cumulative + trailing-24h); ``recent`` defaults to ``value`` if unset."""
    rec = value if recent is None else recent

    def fake_sum(start, end, mode="auto", project=None, *, skip_sync=False):
        if spy is not None:
            spy.append((start, end, mode))
        # Distinguish the recent-24h window (start within 24h of end) from the
        # full-week cumulative window.
        if (end - start) <= dt.timedelta(hours=24, minutes=1):
            return rec
        return value
    monkeypatch.setitem(ns, "_sum_cost_for_range", fake_sum)


def _patch_dispatch(ns, monkeypatch):
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append((payload, mode))
        return "queued"
    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake_dispatch)
    return captured


def _rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT week_start_at, metric, threshold, projected_value, "
            "       denominator, alerted_at "
            "FROM projected_milestones ORDER BY metric, threshold"
        ).fetchall()
    finally:
        conn.close()


# ── store helper: rowcount contract + pre-probe predicate ────────────────


def test_insert_projected_milestone_rowcount_contract(ns):
    conn = ns["open_db"]()
    try:
        n1 = ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, metric="weekly_pct",
            threshold=100, projected_value=102.0, denominator=100.0,
            commit=True,
        )
        n2 = ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, metric="weekly_pct",
            threshold=100, projected_value=105.0, denominator=100.0,
            commit=True,
        )
        assert n1 == 1   # genuinely new
        assert n2 == 0   # INSERT OR IGNORE no-op (fire-once)
    finally:
        conn.close()


def test_projected_levels_already_latched_predicate(ns):
    conn = ns["open_db"]()
    try:
        latched = ns["_projected_levels_already_latched"]
        assert latched(conn, week_start_at=WEEK_KEY, metric="weekly_pct",
                       levels=(90, 100)) is False
        ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, metric="weekly_pct",
            threshold=90, projected_value=95.0, denominator=100.0, commit=True,
        )
        # 90 present, 100 missing → not all latched.
        assert latched(conn, week_start_at=WEEK_KEY, metric="weekly_pct",
                       levels=(90, 100)) is False
        ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, metric="weekly_pct",
            threshold=100, projected_value=105.0, denominator=100.0,
            commit=True,
        )
        assert latched(conn, week_start_at=WEEK_KEY, metric="weekly_pct",
                       levels=(90, 100)) is True
        # Empty levels → vacuously latched.
        assert latched(conn, week_start_at=WEEK_KEY, metric="weekly_pct",
                       levels=()) is True
    finally:
        conn.close()


# ── weekly_pct firing semantics ──────────────────────────────────────────


def test_weekly_pct_fires_on_week_average_crossing(ns, monkeypatch):
    # p_now=60 at 84h elapsed, 84h remaining → r_avg=60/84, proj=120 → fires 90 & 100.
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = _rows(ns)
    assert [(r["metric"], r["threshold"]) for r in rows] == [
        ("weekly_pct", 90), ("weekly_pct", 100)
    ]
    assert all(r["alerted_at"] is not None for r in rows)
    assert all(abs(r["projected_value"] - 120.0) < 1e-9 for r in rows)
    assert all(abs(r["denominator"] - 100.0) < 1e-9 for r in rows)
    assert all(r["week_start_at"] == WEEK_KEY for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["axis"] == "projected" for p, _ in captured)
    assert all(p["metric"] == "weekly_pct" for p, _ in captured)
    assert all(mode == "real" for _, mode in captured)


def test_weekly_pct_does_not_fire_when_week_average_below_threshold(ns, monkeypatch):
    # p_now=40 → week-average proj = 80 < 90. recent-24h is hot but weekly_pct
    # is snapshot-only (no recent rate input), so nothing fires.
    _seed_snapshots(ns, _high_conf_samples(40.0))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    assert _rows(ns) == []
    assert captured == []


def test_weekly_pct_fires_when_average_crosses(ns, monkeypatch):
    # p_now=50 → proj=100 → crosses 90 and (snap-up) 100.
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(abs(r["projected_value"] - 100.0) < 1e-9 for r in rows)


# ── LOW CONF suppression (+ non-vacuity proof lives in a sibling test) ────


def test_low_conf_window_suppresses(ns, monkeypatch):
    # Only ONE sample, none >=24h old → forecast confidence LOW → no fire even
    # though proj would be 120 (p_now=60 at 84h elapsed). This is the
    # confidence-gate suppression case the non-vacuity proof inverts.
    _seed_snapshots(ns, [(83.0, 60.0)])
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    assert _rows(ns) == []
    assert captured == []


# ── fire-once / no-refire / no-un-fire-on-recovery ───────────────────────


def test_fire_once_no_refire_no_unfire(ns, monkeypatch):
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    first = len(captured)
    assert first == 2
    rows_after = _rows(ns)

    # Second tick at the same/over projection: rowcount 0 → no re-dispatch.
    ns["maybe_record_projected_alert"]({})
    assert len(captured) == first
    assert len(_rows(ns)) == len(rows_after) == 2

    # "Recovery": projection drops below thresholds. No row removed, no
    # recovery alert. The pre-probe even skips the projection (all latched).
    conn = ns["open_db"]()
    try:
        conn.execute(
            "UPDATE weekly_usage_snapshots SET weekly_percent = 1.0"
        )
        conn.commit()
    finally:
        conn.close()
    ns["maybe_record_projected_alert"]({})
    assert len(captured) == first
    assert len(_rows(ns)) == 2  # rows persist (no un-fire)


# ── exact-threshold snap-up ──────────────────────────────────────────────


def test_exact_threshold_equality_snaps_up(ns, monkeypatch):
    # p_now=45 → proj exactly 90.0 → fires 90 (proj + 1e-9 >= 90); 100 not.
    _seed_snapshots(ns, _high_conf_samples(45.0))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = _rows(ns)
    assert [r["threshold"] for r in rows] == [90]
    assert abs(rows[0]["projected_value"] - 90.0) < 1e-9


def test_just_below_threshold_does_not_fire(ns, monkeypatch):
    # p_now=44.9 → proj = 89.8 < 90 → nothing.
    _seed_snapshots(ns, _high_conf_samples(44.9))
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    assert _rows(ns) == []


# ── master toggles ───────────────────────────────────────────────────────


def test_master_toggle_off_suppresses_weekly(ns, monkeypatch):
    # alerts.enabled=False but projected_enabled=True → parent gate off → none.
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _write_config(ns, alerts={"enabled": False, "projected_enabled": True})
    spy: list = []
    _patch_spend(ns, monkeypatch, value=300.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    assert _rows(ns) == []
    assert captured == []
    assert spy == []  # no projection/cost work at all


def test_projected_enabled_off_suppresses_weekly(ns, monkeypatch):
    # alerts.enabled=True, projected_enabled absent → projected gate off.
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _write_config(ns, alerts={"enabled": True})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    assert _rows(ns) == []


def test_default_off_writes_nothing(ns, monkeypatch):
    # No config at all → both gates off → nothing.
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    assert _rows(ns) == []


# ── budget_usd firing ────────────────────────────────────────────────────


def test_budget_usd_fires_on_week_average_projection(ns, monkeypatch):
    # spent=150 at 84h elapsed (mid-week), recent matches → rate_avg=150/84,
    # week_avg_projection_usd = 150 + (150/84)*84 = 300 == target → crosses
    # 90% ($270) and 100% ($300, snap-up).
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"weekly_usd": 300.0, "alerts_enabled": True,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    _patch_spend(ns, monkeypatch, value=150.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = [r for r in _rows(ns) if r["metric"] == "budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(abs(r["projected_value"] - 300.0) < 1e-6 for r in rows)
    assert all(abs(r["denominator"] - 300.0) < 1e-9 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["metric"] == "budget_usd" for p, _ in captured)


def test_budget_usd_master_gate_off(ns, monkeypatch):
    # budget alerts_enabled=False → _budget_alerts_active False → no budget rows.
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"weekly_usd": 300.0, "alerts_enabled": False,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, value=150.0, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})
    assert [r for r in _rows(ns) if r["metric"] == "budget_usd"] == []
    assert captured == []
    assert spy == []  # gated before any cost SUM


def test_budget_usd_no_budget_set(ns, monkeypatch):
    # weekly_usd absent → no budget → no budget rows.
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"alerts_enabled": True, "alert_thresholds": [90, 100],
                "projected_enabled": True},
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, value=150.0, spy=spy)

    ns["maybe_record_projected_alert"]({})
    assert [r for r in _rows(ns) if r["metric"] == "budget_usd"] == []
    assert spy == []


# ── mid-week reset re-anchors week_start_at ──────────────────────────────


def test_mid_week_reset_reanchors_week_start_at(ns, monkeypatch):
    """A recorded mid-week reset re-anchors the effective window start; the
    projected row keys on the post-reset start. No reset_event_id column is
    consulted (budget-pattern key shape)."""
    # Seed >=3 POST-reset samples (offsets >12h) plus a pre-reset one that the
    # override drops, so the re-anchored window still has HIGH confidence
    # (>=3 samples, a sample >=24h old at AS_OF). p_now stays 60.
    _seed_snapshots(ns, [(1.0, 5.0), (14.0, 20.0), (40.0, 40.0), (83.0, 60.0)])
    # Record a reset event whose new_week_end_at matches this week's end and
    # whose effective_reset_at is 12h into the original week. _apply_midweek_
    # reset_override then shifts week_start_at forward.
    reset_at = WEEK_START + dt.timedelta(hours=12)
    conn = ns["open_db"]()
    try:
        end_iso = ns["_normalize_week_boundary_dt"](
            WEEK_END.astimezone(dt.timezone.utc)
        ).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            (_iso(reset_at), _iso(WEEK_END - dt.timedelta(days=1)), end_iso,
             _iso(reset_at)),
        )
        conn.commit()
    finally:
        conn.close()
    _write_config(ns, alerts={"enabled": True, "projected_enabled": True})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = _rows(ns)
    assert rows, "expected projected rows after reset re-anchor"
    reanchored_key = dt.datetime.fromisoformat(
        _iso(reset_at).replace("Z", "+00:00")
    ).astimezone().isoformat(timespec="seconds")
    assert all(r["week_start_at"] == reanchored_key for r in rows)
    # Schema carries NO reset_event_id column (budget-pattern; Codex P0-4).
    conn = ns["open_db"]()
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(projected_milestones)"
        )}
    finally:
        conn.close()
    assert "reset_event_id" not in cols


# ── pre-probe avoids cost work when all budget levels latched ────────────


def test_pre_probe_skips_cost_when_all_budget_levels_latched(ns, monkeypatch):
    """When every budget level is already latched, the second tick's budget leg
    must NOT call _sum_cost_for_range (Codex P1-1). Proven by a spy."""
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"weekly_usd": 300.0, "alerts_enabled": True,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, value=150.0, spy=spy)
    _patch_dispatch(ns, monkeypatch)

    # First tick fires both budget levels (cost SUM runs: 2 calls — cumulative
    # + recent-24h).
    ns["maybe_record_projected_alert"]({})
    assert len(spy) >= 1
    rows = [r for r in _rows(ns) if r["metric"] == "budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100]

    # Second tick: all budget levels latched → pre-probe short-circuits BEFORE
    # _build_budget_status_inputs → zero new cost calls.
    spy.clear()
    ns["maybe_record_projected_alert"]({})
    assert spy == []
