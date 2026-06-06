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
        # A concrete period keys the UNIQUE (#137); NULL periods are distinct
        # under SQLite UNIQUE semantics, so the fire-once contract is asserted
        # with the same concrete period production always writes.
        n1 = ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, period="subscription-week",
            metric="weekly_pct", threshold=100, projected_value=102.0,
            denominator=100.0, commit=True,
        )
        n2 = ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, period="subscription-week",
            metric="weekly_pct", threshold=100, projected_value=105.0,
            denominator=100.0, commit=True,
        )
        assert n1 == 1   # genuinely new
        assert n2 == 0   # INSERT OR IGNORE no-op (fire-once)
    finally:
        conn.close()


def test_projected_levels_already_latched_predicate(ns):
    conn = ns["open_db"]()
    try:
        latched = ns["_projected_levels_already_latched"]
        assert latched(conn, week_start_at=WEEK_KEY, period="subscription-week",
                       metric="weekly_pct", levels=(90, 100)) is False
        ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, period="subscription-week",
            metric="weekly_pct", threshold=90, projected_value=95.0,
            denominator=100.0, commit=True,
        )
        # 90 present, 100 missing → not all latched.
        assert latched(conn, week_start_at=WEEK_KEY, period="subscription-week",
                       metric="weekly_pct", levels=(90, 100)) is False
        ns["insert_projected_milestone"](
            conn, week_start_at=WEEK_KEY, period="subscription-week",
            metric="weekly_pct", threshold=100, projected_value=105.0,
            denominator=100.0, commit=True,
        )
        assert latched(conn, week_start_at=WEEK_KEY, period="subscription-week",
                       metric="weekly_pct", levels=(90, 100)) is True
        # Empty levels → vacuously latched.
        assert latched(conn, week_start_at=WEEK_KEY, period="subscription-week",
                       metric="weekly_pct", levels=()) is True
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
    # _build_vendor_budget_inputs → zero new cost calls.
    spy.clear()
    ns["maybe_record_projected_alert"]({})
    assert spy == []


# ── budget_usd band discrimination (the P0-1 guard, budget leg) ──────────


def test_budget_usd_does_not_fire_when_week_average_below_threshold(ns, monkeypatch):
    """Hot trailing-24h rate but a below-threshold WEEK-AVERAGE projection must
    NOT fire (parallels the weekly_pct band-discrimination case). The firing
    value is ``spent + rate_avg*remaining`` (cumulative-driven); the recent-24h
    rate only feeds the displayed high-end verdict, never the alert trigger.

    spent=50 at 84h elapsed → rate_avg=50/84, week_avg_projection_usd
    = 50 + (50/84)*84 = 100 << 90% of $300 ($270). recent-24h is jacked to
    $200 (which would push the displayed HIGH-end verdict over) — yet nothing
    fires, proving the trigger binds to the week-average, not the band end.
    """
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"weekly_usd": 300.0, "alerts_enabled": True,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    _patch_spend(ns, monkeypatch, value=50.0, recent=200.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    assert [r for r in _rows(ns) if r["metric"] == "budget_usd"] == []
    assert captured == []


def test_budget_usd_fires_when_week_average_crosses_even_if_recent_below(ns, monkeypatch):
    """Inverse band-discrimination: a COLD trailing-24h rate still fires when
    the WEEK-AVERAGE projection crosses. spent=150 at 84h → week-average
    projection = $300 (crosses 90%/$270 and 100%/$300), even though recent-24h
    is only $10 (which would keep the high-end verdict comfortable)."""
    _seed_snapshots(ns, _high_conf_samples(50.0))
    _write_config(
        ns,
        budget={"weekly_usd": 300.0, "alerts_enabled": True,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    _patch_spend(ns, monkeypatch, value=150.0, recent=10.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = [r for r in _rows(ns) if r["metric"] == "budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(abs(r["projected_value"] - 300.0) < 1e-6 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}


# ── carry-forward MUST-FIX #1: no "unknown config key" warning on the tick ──


def test_projected_record_tick_emits_no_unknown_config_key_warning(ns, monkeypatch, capsys):
    """With projected_enabled set on BOTH blocks, a record tick must NOT emit a
    'warning: ignoring unknown alerts/budget config key: projected_enabled' on
    stderr — the detector reads the validated getter dicts, where the key is a
    recognized valid key (carry-forward MUST-FIX #1)."""
    _seed_snapshots(ns, _high_conf_samples(60.0))
    _write_config(
        ns,
        alerts={"enabled": True, "projected_enabled": True},
        budget={"weekly_usd": 300.0, "alerts_enabled": True,
                "alert_thresholds": [90, 100], "projected_enabled": True},
    )
    _patch_spend(ns, monkeypatch, value=150.0)
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    err = capsys.readouterr().err
    assert "unknown alerts config key" not in err
    assert "unknown budget config key" not in err
    assert "projected_enabled" not in err


# ── codex_budget_usd payload text (R1) ───────────────────────────────────


def test_projected_codex_text_is_codex_flavored(ns):
    """The ``codex_budget_usd`` projected metric renders Codex-flavored text
    (NOT the generic Claude ``budget_usd`` wording) — the real branch lives in
    ``bin/_lib_alerts_payload.py::_alert_text_projected``, NOT the shim
    (R1). ``_build_alert_payload_projected`` already threads ``metric`` into
    context (no metric-specific change needed there)."""
    payload = ns["_build_alert_payload_projected"](
        metric="codex_budget_usd", threshold=100,
        projected_value=230.0, denominator=200.0,
        week_start_at="2026-06-01T00:00:00+00:00",
    )
    assert payload["metric"] == "codex_budget_usd"
    assert payload["context"]["metric"] == "codex_budget_usd"
    title, _subtitle, body = ns["_alert_text_projected"](payload, None)
    assert "Codex" in title or "Codex" in body


# ── calendar-period + Codex projected firing (#135) ──────────────────────
#
# Unlike the subscription-week tests above (which seed weekly_usage_snapshots),
# the calendar/Codex legs resolve their window purely from `now` + a calendar
# period and read spend via _build_vendor_budget_inputs (monkeypatched
# _sum_cost_for_range / _sum_codex_cost_for_range). With display.tz pinned to
# UTC and `now` placed exactly mid-month, elapsed_fraction == 0.5 so the
# week-average projection (spent / elapsed_fraction) == 2*spent — $150 spent on
# a $300/$200 budget projects to $300/$400, crossing 90% and 100%.

# June 2026 has 30 days; 2026-06-16T00:00:00Z is exactly 15 days in (0.5
# elapsed) so projection == 2*spent and elapsed_fraction (0.5) >= 0.15 so the
# status is NOT low-confidence.
CAL_AS_OF = dt.datetime(2026, 6, 16, 0, 0, 0, tzinfo=dt.timezone.utc)
CAL_AS_OF_JULY = dt.datetime(2026, 7, 16, 0, 0, 0, tzinfo=dt.timezone.utc)
JUNE_KEY = dt.datetime(
    2026, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc
).isoformat(timespec="seconds")
JULY_KEY = dt.datetime(
    2026, 7, 1, 0, 0, 0, tzinfo=dt.timezone.utc
).isoformat(timespec="seconds")


def _write_full_config(ns, cfg):
    """Write an arbitrary config dict at the redirected CONFIG_PATH."""
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(json.dumps(cfg) + "\n")


def _patch_codex_spend(ns, monkeypatch, *, value=None, spy=None):
    """Inject a deterministic ``_sum_codex_cost_for_range`` (the cctally
    namespace function _build_vendor_budget_inputs resolves at call time). The
    fake accepts ``skip_sync`` so the Codex projected leg's
    ``skip_sync=False`` full-window call is honored."""
    def fake_sum(start, end, *, speed="auto", skip_sync=False):
        if spy is not None:
            spy.append((start, end, speed, skip_sync))
        return value
    monkeypatch.setitem(ns, "_sum_codex_cost_for_range", fake_sum)


def _codex_block(*, amount_usd=200.0, period="calendar-month",
                 alerts_enabled=True, projected_enabled=True,
                 thresholds=(90, 100)):
    return {
        "amount_usd": amount_usd,
        "period": period,
        "alerts_enabled": alerts_enabled,
        "projected_enabled": projected_enabled,
        "alert_thresholds": list(thresholds),
    }


# (a) calendar-month Claude projected fires once + rolls over ──────────────


def test_calendar_month_claude_projected_fires_once_and_rolls_over(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF))
    _write_full_config(ns, {
        "display": {"tz": "utc"},
        "budget": {
            "weekly_usd": 300.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
            "projected_enabled": True,
        },
    })
    # spent=$150 on a $300 budget at 0.5 elapsed → projection $300 → crosses
    # 90% ($270) and 100% ($300, snap-up).
    _patch_spend(ns, monkeypatch, value=150.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = [r for r in _rows(ns) if r["metric"] == "budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(r["week_start_at"] == JUNE_KEY for r in rows)
    assert all(abs(r["projected_value"] - 300.0) < 1e-6 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["metric"] == "budget_usd" for p, _ in captured)

    # Second tick same month: all latched → no new rows / dispatch.
    first = len(captured)
    ns["maybe_record_projected_alert"]({})
    assert len(captured) == first
    assert len([r for r in _rows(ns) if r["metric"] == "budget_usd"]) == 2

    # Roll over to July: a fresh period key re-arms.
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF_JULY))
    captured.clear()
    ns["maybe_record_projected_alert"]({})
    july = [r for r in _rows(ns) if r["metric"] == "budget_usd"
            and r["week_start_at"] == JULY_KEY]
    assert [r["threshold"] for r in july] == [90, 100]
    assert {p["threshold"] for p, _ in captured} == {90, 100}


# (b) Codex projected fires once + rolls over ─────────────────────────────


def test_codex_projected_fires_once_and_rolls_over(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF))
    _write_full_config(ns, {
        "display": {"tz": "utc"},
        "budget": {"codex": _codex_block(amount_usd=200.0)},
    })
    # spent=$100 on $200 at 0.5 elapsed → projection $200 → crosses 90/100.
    _patch_codex_spend(ns, monkeypatch, value=100.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = [r for r in _rows(ns) if r["metric"] == "codex_budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100]
    assert all(r["week_start_at"] == JUNE_KEY for r in rows)
    assert all(abs(r["projected_value"] - 200.0) < 1e-6 for r in rows)
    assert all(abs(r["denominator"] - 200.0) < 1e-9 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["metric"] == "codex_budget_usd" for p, _ in captured)
    assert all(p["axis"] == "projected" for p, _ in captured)

    # Second tick: no refire.
    first = len(captured)
    ns["maybe_record_projected_alert"]({})
    assert len(captured) == first
    assert len([r for r in _rows(ns) if r["metric"] == "codex_budget_usd"]) == 2

    # Roll over: re-arm.
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF_JULY))
    captured.clear()
    ns["maybe_record_projected_alert"]({})
    july = [r for r in _rows(ns) if r["metric"] == "codex_budget_usd"
            and r["week_start_at"] == JULY_KEY]
    assert [r["threshold"] for r in july] == [90, 100]
    assert {p["threshold"] for p, _ in captured} == {90, 100}


# (c) only_metrics={"codex_budget_usd"} scopes the opportunistic fire ──────


def test_only_metrics_codex_does_not_fire_claude_or_weekly(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF))
    # Seed a current-week snapshot so weekly_pct would ALSO be eligible — at
    # CAL_AS_OF the snapshot window is irrelevant for the calendar legs but the
    # weekly_pct leg reads it; we make weekly eligible to prove it's suppressed.
    _seed_snapshots(
        ns, _high_conf_samples(60.0),
        week_start=CAL_AS_OF - dt.timedelta(hours=84),
        week_end=CAL_AS_OF + dt.timedelta(hours=84),
    )
    _write_full_config(ns, {
        "display": {"tz": "utc"},
        "alerts": {"enabled": True, "projected_enabled": True},
        "budget": {
            "weekly_usd": 300.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
            "projected_enabled": True,
            "codex": _codex_block(amount_usd=200.0),
        },
    })
    _patch_spend(ns, monkeypatch, value=150.0)
    _patch_codex_spend(ns, monkeypatch, value=100.0)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({}, only_metrics={"codex_budget_usd"})

    rows = _rows(ns)
    metrics = {r["metric"] for r in rows}
    assert metrics == {"codex_budget_usd"}, metrics
    assert all(p["metric"] == "codex_budget_usd" for p, _ in captured)
    assert {p["threshold"] for p, _ in captured} == {90, 100}


# (d) Claude calendar + Codex same period → two distinct rows, no collision ─


def test_claude_and_codex_same_period_no_collision(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF))
    _write_full_config(ns, {
        "display": {"tz": "utc"},
        "budget": {
            "weekly_usd": 300.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
            "projected_enabled": True,
            "codex": _codex_block(amount_usd=200.0),
        },
    })
    _patch_spend(ns, monkeypatch, value=150.0)        # Claude → $300 proj
    _patch_codex_spend(ns, monkeypatch, value=100.0)  # Codex → $200 proj
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_projected_alert"]({})

    rows = _rows(ns)
    # Both metrics share the SAME period-start key but are distinct rows.
    claude = [r for r in rows if r["metric"] == "budget_usd"]
    codex = [r for r in rows if r["metric"] == "codex_budget_usd"]
    assert [r["threshold"] for r in claude] == [90, 100]
    assert [r["threshold"] for r in codex] == [90, 100]
    assert all(r["week_start_at"] == JUNE_KEY for r in claude)
    assert all(r["week_start_at"] == JUNE_KEY for r in codex)
    assert len(rows) == 4  # neither suppressed the other
    assert {(p["metric"]) for p, _ in captured} == {"budget_usd", "codex_budget_usd"}


# ── R3: config-tz key stability near a civil-month boundary ──────────────


def test_codex_projected_key_is_config_tz_stable_near_month_boundary(ns, monkeypatch):
    """The firing path resolves CONFIG tz (``Namespace(tz=None)``), never a
    display ``--tz``. Near a civil-month boundary where two zones straddle the
    month, the resolved ``period_start_at`` dedup key must be the CONFIG-tz
    period start — so a `cctally budget --tz X` opportunistic fire can never fork
    the key / double-fire (R3). We assert KEY stability (one row, config-tz key),
    NOT value equality.

    ``now`` = 2026-07-01T02:00:00Z. Config ``display.tz=America/New_York``
    (UTC-4 in summer) places that instant at 2026-06-30T22:00 → the JUNE civil
    month, whose start (2026-06-01T00:00 NY = 04:00 UTC) is the firing key. The
    SAME UTC instant is in July under UTC — but the firing path ignores the host
    zone / any display ``--tz`` and keys on the config-tz (NY) June period start.
    Spend is pinned just over budget so the ~1.0-elapsed projection still
    crosses; two fires at the same instant must NOT fork the key (one row each).
    """
    boundary = dt.datetime(2026, 7, 1, 2, 0, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(boundary))
    _write_full_config(ns, {
        "display": {"tz": "America/New_York"},
        "budget": {"codex": _codex_block(amount_usd=200.0)},
    })
    # At ~1.0 elapsed the projection ≈ spent, so spend at budget crosses 90/100.
    _patch_codex_spend(ns, monkeypatch, value=200.0)
    _patch_dispatch(ns, monkeypatch)

    # Fire twice (a second opportunistic fire at the same instant). A forked key
    # would produce a SECOND set of rows under a different period start.
    ns["maybe_record_projected_alert"]({}, only_metrics={"codex_budget_usd"})
    ns["maybe_record_projected_alert"]({}, only_metrics={"codex_budget_usd"})

    rows = [r for r in _rows(ns) if r["metric"] == "codex_budget_usd"]
    keys = {r["week_start_at"] for r in rows}
    # Config-tz (NY) June start = 2026-06-01T00:00 NY = 04:00 UTC.
    config_tz_june_key = "2026-06-01T04:00:00+00:00"
    utc_july_key = "2026-07-01T00:00:00+00:00"
    assert keys == {config_tz_june_key}, keys
    assert utc_july_key not in keys  # never the host-zone (UTC) civil month
    # One key → no fork; thresholds latched once each (no double-fire).
    assert sorted(r["threshold"] for r in rows) == [90, 100]


# ── R5: cold-cache Codex projected counts (skip_sync=False self-syncs) ────


def _write_codex_rollout(jsonl_path, session_id, model, inp, cached, out, ts):
    """Write a minimal real Codex rollout JSONL (session_meta → turn_context →
    one token_count event), mirroring tests/test_codex_home.py::_write_rollout
    but with a caller-supplied timestamp so the entry lands in a chosen period.
    """
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    iso = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    records = [
        {"timestamp": iso, "type": "session_meta", "payload": {"id": session_id}},
        {"timestamp": iso, "type": "turn_context", "payload": {"model": model}},
        {"timestamp": iso, "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {
                    "input_tokens": inp, "cached_input_tokens": cached,
                    "output_tokens": out, "reasoning_output_tokens": 0,
                    "total_tokens": inp + out},
                "total_token_usage": {"total_tokens": inp + out}}}},
    ]
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def test_codex_projected_counts_with_cold_cache(ns, monkeypatch, tmp_path):
    """R5 non-vacuity: the Codex projected leg passes ``skip_sync=False`` so it
    self-syncs from ``~/.codex/sessions`` even when no other record-path warmer
    ran. We make the codex_budget ACTUAL axis all-latched (it short-circuits
    BEFORE its cost SUM, leaving the cache cold) yet assert the PROJECTED leg
    STILL fires the crossing — proving it does not depend on a pre-warmed cache.

    The proof that this is non-vacuous (flip the leg to ``skip_sync=True`` →
    RED) is cited in the commit body; here the production code is ``skip_sync=
    False`` and the entries are NEVER synced before the projected fire, so a
    pass means the leg's own sync ingested them.
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(CAL_AS_OF))  # mid-June, 0.5 elapsed
    # Real Codex entry timestamped inside June → one gpt-5.5 entry ≈ $134.56.
    # 0.5 elapsed → projection ≈ 2×spent ≈ $269 → crosses 90%/100% of a $200
    # Codex budget.
    _write_codex_rollout(
        tmp_path / ".codex" / "sessions" / "2026" / "06" / "10"
        / "rollout-cold.jsonl",
        session_id="cold-cache-sess", model="gpt-5.5",
        inp=5_000_000, cached=0, out=2_000_000,
        ts=dt.datetime(2026, 6, 10, 12, 0, 0),
    )
    _write_full_config(ns, {
        "display": {"tz": "utc"},
        "budget": {"codex": _codex_block(amount_usd=200.0)},
    })
    captured = _patch_dispatch(ns, monkeypatch)

    # Pre-latch the codex_budget ACTUAL axis for all thresholds at the June
    # period key, so maybe_record_codex_budget_milestone short-circuits BEFORE
    # its SUM (the cache stays cold — exactly the R5 hazard scenario). The
    # ACTUAL axis now lives in the unified vendor-tagged table (#143): seed via
    # the shared helper with `vendor="codex"` (period left NULL — the record
    # path's pre-probe matches `period = ? OR period IS NULL`, so the
    # short-circuit fires regardless of the configured period noun).
    conn = ns["open_db"]()
    try:
        for t in (90, 100):
            ns["insert_budget_milestone"](
                conn, vendor="codex", period_start_at=JUNE_KEY, threshold=t,
                budget_usd=200.0, spent_usd=200.0, consumption_pct=100.0,
                commit=True,
            )
    finally:
        conn.close()
    # Actual axis: confirms it short-circuits (no cache warm) — best-effort,
    # never raises into the test.
    ns["maybe_record_codex_budget_milestone"]({})

    # The projected leg (NOT monkeypatched — real _sum_codex_cost_for_range)
    # self-syncs via skip_sync=False and reads the real cost.
    ns["maybe_record_projected_alert"]({}, only_metrics={"codex_budget_usd"})

    rows = [r for r in _rows(ns) if r["metric"] == "codex_budget_usd"]
    assert [r["threshold"] for r in rows] == [90, 100], rows
    assert all(r["week_start_at"] == JUNE_KEY for r in rows)
    # Projection ≈ 2 × $134.56 = $269.12 → comfortably over both thresholds.
    assert all(r["projected_value"] > 200.0 for r in rows)
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["metric"] == "codex_budget_usd" for p, _ in captured)


# ── (#137) period column: collision-free key + NULL-row latched ───────────


def test_budget_usd_calendar_week_to_month_switch_no_collision(ns):
    """Symptom 2 (projected): a budget_usd crossing under calendar-week at
    instant X must not block a budget_usd crossing under calendar-month at the
    SAME X — period now discriminates UNIQUE(week_start_at, period, metric,
    threshold), so the second INSERT returns rowcount 1."""
    X = "2026-06-01T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        assert ns["insert_projected_milestone"](
            conn, week_start_at=X, period="calendar-week", metric="budget_usd",
            threshold=90, projected_value=270.0, denominator=300.0, commit=True,
        ) == 1
        # Same instant, same metric+threshold, DIFFERENT period → no collision.
        assert ns["insert_projected_milestone"](
            conn, week_start_at=X, period="calendar-month", metric="budget_usd",
            threshold=90, projected_value=290.0, denominator=300.0, commit=True,
        ) == 1
        periods = [
            r[0] for r in conn.execute(
                "SELECT period FROM projected_milestones "
                "WHERE week_start_at=? AND metric='budget_usd' AND threshold=90 "
                "ORDER BY period", (X,)
            )
        ]
        assert periods == ["calendar-month", "calendar-week"]
    finally:
        conn.close()


def test_latched_predicate_period_wildcard_matches_null(ns):
    """P1-1 (projected): a pre-011 NULL-period row counts as latched for the
    current window (period=? OR period IS NULL), so an upgrading user never
    re-fires a spurious projected alert. A row under a DIFFERENT concrete period
    does NOT mask a fresh period's crossing."""
    X = "2026-06-01T00:00:00+00:00"
    latched = ns["_projected_levels_already_latched"]
    conn = ns["open_db"]()
    try:
        # Seed a pre-011 NULL-period budget_usd@90 row.
        conn.execute(
            "INSERT INTO projected_milestones (week_start_at, period, metric, "
            "threshold, projected_value, denominator, crossed_at_utc, "
            "alerted_at) VALUES (?, NULL, 'budget_usd', 90, 290.0, 300.0, ?, ?)",
            (X, "2026-06-05T00:00:00Z", "2026-06-05T00:00:00Z"),
        )
        conn.commit()
        # The wildcard treats the NULL row as latched for the live period.
        assert latched(
            conn, week_start_at=X, period="calendar-month",
            metric="budget_usd", levels=(90,),
        ) is True
        # But a DIFFERENT concrete period that has its OWN row + a missing level
        # is still not fully latched (sanity: the wildcard doesn't over-match).
        assert latched(
            conn, week_start_at=X, period="calendar-month",
            metric="budget_usd", levels=(90, 100),
        ) is False
    finally:
        conn.close()
