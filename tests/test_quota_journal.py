"""Task 7 — Codex quota journaling.

Covers the Codex-side reroute onto the 6a ingest machinery:

  * Item 1 — ``sync_codex_cache`` appends a Codex quota ``obs`` per newly-read
    observation (the durable truth for the evaporating rollout JSONL), keeping
    the direct cache.db write byte-identical.
  * Item 2 — the ``QUOTA_APPLIER`` cache leg materializes those obs into cache.db
    ``quota_window_snapshots`` under a NON-BLOCKING ``cache.db.codex.lock``, and
    PREFIX-STOPS on a busy flock so the scalar cursor never advances past an
    unmaterialized obs.
  * Item 3 — ``reconcile_codex_quota_projection`` runs its stats writes through
    the single-flight ingest cycle (covered end-to-end by the existing
    ``test_codex_quota_projection`` suite; this file adds the journaling seam).
  * Item 4 — the on-demand codex budget firing routes through the cycle's
    ``codex_apply`` seam, so its ``budget_milestones`` crossing is journaled as a
    ``budget`` evt and its alert dispatches post-commit.
  * Item 5 — a genuine arming activation journals a ``quota_alert_arming`` evt
    whose ``activated_at_utc`` survives replay; a reconcile over the replayed
    arming honors it (no historical re-fire).

Isolation mirrors tests/test_quota_alerts.py + tests/test_journal_ingest.py:
``load_script()`` drops cached ``_cctally_*`` siblings and reloads fresh, so the
journal/quota siblings are imported AFTER it; ``redirect_paths`` sets the tmp
JOURNAL_DIR / data dir and ``sys.modules["cctally"]``.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib
import json
import os
import shutil
from pathlib import Path

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"
FIXED = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
CODEX_S1_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl"
)


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, quota, jr, jl


def _iso(hour: int, minute: int = 0) -> str:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).isoformat()


def _journal_lines(jr, jl):
    core = importlib.import_module("_cctally_core")
    out = []
    for seg in jr.list_segments():
        for raw in (core.JOURNAL_DIR / seg).read_bytes().splitlines():
            rec = jl.decode_line(raw)
            if rec is not None:
                out.append(rec)
    return out


def _codex_quota_obs(jl, *, source_root_key, source_path, line_offset,
                     captured_at_utc, used_percent=10.0,
                     logical_limit_key="limit-primary", observed_slot="primary",
                     window_minutes=300, at="2026-07-15T12:00:00Z"):
    return jl.make_obs(at=at, src="codex-quota", provider="codex", payload={
        "kind": "quota_window_snapshot",
        "source": "codex", "source_root_key": source_root_key,
        "source_path": source_path, "line_offset": line_offset,
        "captured_at_utc": captured_at_utc, "observed_slot": observed_slot,
        "logical_limit_key": logical_limit_key, "limit_id": "native-primary",
        "limit_name": "Primary", "window_minutes": window_minutes,
        "used_percent": used_percent, "resets_at_utc": RESET,
        "plan_type": "pro", "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5.3-codex",
    })


def _seed_quota(ns, *, root, observations, limit="limit-primary"):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_root_key) DO UPDATE SET
                 last_seen_utc=excluded.last_seen_utc""",
            (root, f"/codex/{root}", _iso(10), _iso(10)),
        )
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc,
                plan_type, individual_limit_json, reached_type)
               VALUES ('codex', ?, ?, ?, ?, 'primary', ?, 'native-primary',
                       'Primary', 300, ?, ?, 'pro', NULL, NULL)""",
            [(root, f"/codex/{root}/rollout.jsonl", off, captured, limit,
              pct, RESET) for captured, off, pct in observations],
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def _write_quota_config(ns, *, actual=(90,)):
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": True,
        "quota": {
            "enabled": True,
            "actual_thresholds": list(actual),
            "projected_thresholds": [],
            "rules": [],
        },
    }}) + "\n")


# ==========================================================================
# Item 2 — QUOTA_APPLIER cache leg
# ==========================================================================

def test_quota_applier_materializes_obs_into_cache(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()  # create the cache.db schema

    obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10), used_percent=42.0)
    jr.append_record(obs, now_utc=FIXED)

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran and res.consumed == 1

    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute(
            "SELECT source_root_key, line_offset, used_percent, observed_model "
            "FROM quota_window_snapshots WHERE source='codex'").fetchall()
    finally:
        conn.close()
    assert rows == [("root-a", 10, 42.0, "gpt-5.3-codex")]

    # cursor advanced past the fully-consumed obs
    assert jr.run_stats_ingest(mode="authoritative").consumed == 0


def test_quota_applier_prefix_stops_on_busy_codex_flock(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    core = importlib.import_module("_cctally_core")

    # A non-quota obs at index 0, a codex quota obs at index 1.
    non_quota = jl.make_obs(
        at="2026-07-15T12:00:00Z", src="statusline", provider="claude",
        payload={"week_start_date": "2026-07-15", "weekly_percent": 1.0})
    quota_obs = _codex_quota_obs(
        jl, source_root_key="root-a", source_path="/codex/root-a/r.jsonl",
        line_offset=10, captured_at_utc=_iso(10))
    decoded = [(non_quota, "seg", 0), (quota_obs, "seg", 100)]

    # Hold the codex flock (a second open() fd competes even in-process — BSD
    # flock is per-open-file-description) so the applier sees it busy.
    held = os.open(str(core.CACHE_LOCK_CODEX_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        stop = jr._quota_applier(decoded)
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert stop == 1, "prefix-stop at the FIRST codex quota obs index"

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 0, "nothing materialized"
    finally:
        conn.close()

    # Flock free now -> the remainder materializes, full consumption (None).
    assert jr._quota_applier(decoded) is None
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source='codex'").fetchone()[0] == 1
    finally:
        conn.close()


# ==========================================================================
# Item 1 — sync_codex_cache appends a Codex quota obs per observation
# ==========================================================================

def test_sync_codex_cache_appends_quota_obs(tmp_path, monkeypatch):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    provider_root = tmp_path / "fake-codex-home"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout-s1.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_S1_FIXTURE, rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    # Cut over the (empty) stats.db FIRST — matching production ordering, where
    # the one-time §8 cutover runs before any Codex sync writes cache quota rows,
    # so the bootstrap carries no quota obs. Otherwise the reconcile's own
    # ``open_db`` (invoked at the tail of ``sync_codex_cache``) would cut over a
    # legacy stats.db and re-export the just-written cache quota rows as bootstrap
    # obs, double-counting them here (DB journal redesign §8).
    ns["open_db"]().close()

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()

    conn = ns["open_cache_db"]()
    try:
        cache_rows = conn.execute(
            "SELECT line_offset, used_percent FROM quota_window_snapshots "
            "WHERE source='codex'").fetchall()
    finally:
        conn.close()
    assert cache_rows, "the S1 fixture must carry quota events"

    obs = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "obs" and line.get("provider") == "codex"
        and (line.get("payload") or {}).get("kind") == "quota_window_snapshot"
    ]
    assert len(obs) == len(cache_rows), "one journal obs per materialized row"
    obs_keys = {(o["payload"]["line_offset"], o["payload"]["used_percent"]) for o in obs}
    assert obs_keys == {(r[0], r[1]) for r in cache_rows}


# ==========================================================================
# Item 5 — quota_alert_arming journaled state + replay honored
# ==========================================================================

def test_arming_journaled_and_replay_honored(tmp_path, monkeypatch):
    ns, quota, jr, jl = _load(tmp_path, monkeypatch)
    _seed_quota(ns, root="root-a", observations=[(_iso(10), 10, 95.0)])
    _write_quota_config(ns, actual=(90,))
    now = dt.datetime(2026, 7, 15, 12, tzinfo=UTC)

    # Reconcile #1 (eligible): a fresh fingerprint activates -> the arming state
    # is journaled as a `quota_alert_arming` evt (Item 5).
    quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=now)

    arming_evts = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
    ]
    assert len(arming_evts) == 1
    evt = arming_evts[0]
    assert evt["id"].startswith("qaa:codex:root-a:")
    assert evt["payload"]["activated_at_utc"] == quota._utc_iso(now)
    assert evt["payload"]["rule_fingerprint"]

    conn = ns["open_db"]()
    try:
        arm = conn.execute(
            "SELECT source_root_key, rule_fingerprint, activated_at_utc "
            "FROM quota_alert_arming").fetchall()
    finally:
        conn.close()
    assert len(arm) == 1 and arm[0][2] == quota._utc_iso(now)

    # (b) Fold-applier round-trip: clear the arming table, replay the evt, and
    # the boundary (activated_at) is reproduced verbatim.
    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM quota_alert_arming")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()
        replayed = conn.execute(
            "SELECT source, source_root_key, rule_fingerprint, activated_at_utc "
            "FROM quota_alert_arming").fetchone()
    finally:
        conn.close()
    assert replayed is not None
    assert replayed[0] == "codex" and replayed[1] == "root-a"
    assert replayed[2] == evt["payload"]["rule_fingerprint"]
    assert replayed[3] == quota._utc_iso(now)

    # (c) A reconcile over the replayed arming HONORS it: no second arming evt,
    # no historical re-fire.
    result = quota.reconcile_codex_quota_projection(
        source_root_keys={"root-a"}, alert_eligible_root_keys={"root-a"}, now=now)
    arming_evts2 = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "quota_alert_arming"
    ]
    assert len(arming_evts2) == 1, "replayed boundary honored -> no re-arm evt"
    assert result.alerts_dispatched == 0, "no historical re-fire"


# ==========================================================================
# Item 4 — on-demand codex budget firing routes through the cycle + journals
# ==========================================================================

def test_on_demand_codex_budget_routes_through_cycle_and_journals(
    tmp_path, monkeypatch
):
    ns, _quota, jr, jl = _load(tmp_path, monkeypatch)
    ns["open_db"]().close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-15T12:00:00Z")
    core = importlib.import_module("_cctally_core")
    core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"},
        "budget": {"codex": {
            "amount_usd": 200.0, "period": "calendar-month",
            "alerts_enabled": True, "alert_thresholds": [90, 100],
        }},
    }) + "\n")
    # Inject deterministic Codex spend crossing BOTH thresholds.
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, end, *, speed="auto": 200.0)
    captured = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **k: captured.append((payload, mode)))

    # The production reroute (hook-tick / `cctally budget`) runs the helper on the
    # cycle's conn via the codex_apply seam.
    def _leg(ctx):
        ns["maybe_record_codex_budget_milestone"](
            {}, conn=ctx.conn, alert_sink=ctx.pending_alerts)

    jr.run_stats_ingest(mode="authoritative", codex_apply=_leg)

    budget_evts = [
        line for line in _journal_lines(jr, jl)
        if line.get("t") == "evt"
        and (line.get("payload") or {}).get("kind") == "budget"
        and (line.get("payload") or {}).get("vendor") == "codex"
    ]
    assert {e["payload"]["threshold"] for e in budget_evts} == {90, 100}

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT threshold, journal_id FROM budget_milestones "
            "WHERE vendor='codex' ORDER BY threshold").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [90, 100]
    assert all(r[1] is not None for r in rows), "harvested rows stamped journal_id"
    assert {p["threshold"] for p, _ in captured} == {90, 100}
    assert all(p["axis"] == "codex_budget" for p, _ in captured)
