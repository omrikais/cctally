"""Durable provider-neutral quota alert configuration and firing contracts."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import threading

import pytest

from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
RESET = "2026-07-15T15:00:00+00:00"


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC)


def _iso(value: dt.datetime) -> str:
    return value.isoformat()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns, importlib.import_module("_cctally_quota")


def _seed(
    ns, *, observations, root="root-a", limit="limit-primary", label="Primary",
    reset=RESET,
):
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_root_key) DO UPDATE SET
                 last_seen_utc=excluded.last_seen_utc""",
            (root, f"/codex/{root}", _iso(_at(10)), _iso(_at(10))),
        )
        conn.executemany(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset,
                captured_at_utc, observed_slot, logical_limit_key, limit_id,
                limit_name, window_minutes, used_percent, resets_at_utc,
                plan_type, individual_limit_json, reached_type)
               VALUES ('codex', ?, ?, ?, ?, 'primary', ?, 'native-primary',
                       ?, 300, ?, ?, 'pro', NULL, NULL)""",
            [
                (root, f"/codex/{root}/rollout.jsonl", offset, _iso(captured),
                limit, label, percent, reset)
                for captured, offset, percent in observations
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _write_config(ns, *, global_enabled=True, quota_enabled=True,
                  actual=(90,), projected=(), rules=()):
    import _cctally_core

    _cctally_core.CONFIG_PATH.write_text(json.dumps({"alerts": {
        "enabled": global_enabled,
        "quota": {
            "enabled": quota_enabled,
            "actual_thresholds": list(actual),
            "projected_thresholds": list(projected),
            "rules": list(rules),
        },
    }}) + "\n")


def _rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            """SELECT source_root_key, logical_limit_key, resets_at_utc, threshold,
                      qualifying_kind, qualifying_percent, projected_percent,
                      disposition, alerted_at, suppressed_at, orphaned_at
                 FROM quota_threshold_events
                ORDER BY source_root_key, logical_limit_key, resets_at_utc, threshold"""
        ).fetchall()
    finally:
        conn.close()


def _arming(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            """SELECT source_root_key, logical_limit_key, rule_fingerprint,
                      activated_at_utc
                 FROM quota_alert_arming
                ORDER BY source_root_key, logical_limit_key"""
        ).fetchall()
    finally:
        conn.close()


def _reconcile(quota, *, roots=("root-a",), eligible=("root-a",), now):
    return quota.reconcile_codex_quota_projection(
        source_root_keys=set(roots), alert_eligible_root_keys=set(eligible), now=now,
    )


def _capture_dispatch(ns, monkeypatch, *, status="queued"):
    captured = []

    def fake(payload, *, mode="real", **_kwargs):
        captured.append((payload, mode))
        return status

    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake)
    return captured


def test_quota_config_defaults_are_opt_in_and_rules_are_exact(runtime):
    ns, _quota = runtime
    config = ns["_get_quota_alerts_config"]({})
    assert config == {
        "enabled": False,
        "actual_thresholds": [90, 95],
        "projected_thresholds": [],
        "rules": [],
    }

    configured = ns["_get_quota_alerts_config"]({"alerts": {"quota": {
        "enabled": True,
        "actual_thresholds": [],
        "projected_thresholds": [95],
        "rules": [{
            "source": "codex", "source_root_key": "root-a",
            "logical_limit_key": "limit-primary", "actual_thresholds": [80, 90],
            "projected_thresholds": [],
        }],
    }}})
    assert configured["actual_thresholds"] == []
    assert configured["rules"][0]["actual_thresholds"] == [80, 90]

    bad_rule = {"source": "codex", "source_root_key": "root-a"}
    with pytest.raises(ns["_AlertsConfigError"], match="exactly"):
        ns["_get_quota_alerts_config"]({"alerts": {"quota": {
            "enabled": True, "actual_thresholds": [], "projected_thresholds": [],
            "rules": [bad_rule],
        }}})
    with pytest.raises(ns["_AlertsConfigError"], match="strictly increasing"):
        ns["_get_quota_alerts_config"]({"alerts": {"quota": {
            "enabled": True, "actual_thresholds": [95, 90],
            "projected_thresholds": [], "rules": [],
        }}})
    duplicate = {
        "source": "codex", "source_root_key": "root-a",
        "logical_limit_key": "limit-primary", "actual_thresholds": [],
        "projected_thresholds": [],
    }
    with pytest.raises(ns["_AlertsConfigError"], match="unique"):
        ns["_get_quota_alerts_config"]({"alerts": {"quota": {
            "enabled": True, "actual_thresholds": [], "projected_thresholds": [],
            "rules": [duplicate, duplicate],
        }}})


def test_quota_config_cli_round_trips_json_and_unsets_to_defaults(runtime, capsys):
    ns, _quota = runtime
    raw = json.dumps({
        "enabled": True, "actual_thresholds": [80], "projected_thresholds": [],
        "rules": [],
    })
    args = argparse.Namespace(
        action="set", key="alerts.quota", value=raw, emit_json=True,
    )
    assert ns["cmd_config"](args) == 0
    assert json.loads(capsys.readouterr().out) == {"alerts": {"quota": {
        "enabled": True, "actual_thresholds": [80], "projected_thresholds": [],
        "rules": [],
    }}}

    assert ns["cmd_config"](argparse.Namespace(
        action="unset", key="alerts.quota", emit_json=False,
    )) == 0
    assert ns["_config_known_value"](ns["load_config"](), "alerts.quota") == {
        "enabled": False, "actual_thresholds": [90, 95],
        "projected_thresholds": [], "rules": [],
    }


def test_quota_payload_has_provider_neutral_identity_and_dispatch_text(runtime):
    ns, _quota = runtime
    payload = ns["_build_alert_payload_quota"](
        source="codex", source_root_key="root-a", logical_limit_key="limit-primary",
        observed_slot="primary", window_minutes=300, resets_at_utc=RESET,
        threshold=95, kind="projected", crossed_at_utc=_iso(_at(12)),
        qualifying_percent=None, projected_percent=96.5,
    )
    assert payload["axis"] == "quota"
    assert {"source", "source_root_key", "logical_limit_key", "observed_slot",
            "window_minutes", "resets_at_utc", "threshold", "kind"} <= set(payload)
    assert payload["context"]["kind"] == "projected"
    title, subtitle, body = ns["_alert_text_quota"](payload, None)
    assert "quota" in title.lower()
    assert "primary" in subtitle
    assert "projected" in body.lower()


def test_initial_activation_suppresses_backfill_then_later_actual_claims_once(
    runtime, monkeypatch,
):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 80.0)])
    _write_config(ns, actual=(90,))
    captured = _capture_dispatch(ns, monkeypatch)

    _reconcile(quota, now=_at(11))
    assert _rows(ns) == []
    assert len(_arming(ns)) == 1

    _seed(ns, observations=[(_at(11, 10), 20, 95.0)])
    _reconcile(quota, now=_at(11, 20))
    rows = _rows(ns)
    assert [(row["threshold"], row["qualifying_kind"], row["disposition"])
            for row in rows] == [(90, "actual", "alerted")]
    assert len(captured) == 1
    assert captured[0][0]["context"]["source_root_key"] == "root-a"

    _reconcile(quota, now=_at(11, 30))
    assert len(_rows(ns)) == len(captured) == 1


def test_initially_enabled_already_satisfied_suppresses_without_dispatch(
    runtime, monkeypatch,
):
    """Initial activation is a baseline, even when both gates start enabled."""
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 95.0)])
    _write_config(ns, actual=(90,))
    captured = _capture_dispatch(ns, monkeypatch)

    _reconcile(quota, now=_at(11))

    assert [(row["threshold"], row["qualifying_kind"], row["disposition"])
            for row in _rows(ns)] == [(90, "actual", "suppressed_backfill")]
    assert len(_arming(ns)) == 1
    assert captured == []


def test_quota_disabled_never_arms_claims_or_dispatches(runtime, monkeypatch):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 80.0)])
    _write_config(ns, global_enabled=True, quota_enabled=True, actual=(90,))
    captured = _capture_dispatch(ns, monkeypatch)

    _reconcile(quota, now=_at(11))
    assert len(_arming(ns)) == 1

    _write_config(ns, global_enabled=True, quota_enabled=False, actual=(90,))
    _seed(ns, observations=[(_at(11, 10), 20, 95.0)])
    _reconcile(quota, now=_at(11, 20))

    assert _arming(ns) == []
    assert _rows(ns) == []
    assert captured == []

    _write_config(ns, global_enabled=True, quota_enabled=True, actual=(90,))
    _reconcile(quota, now=_at(11, 30))
    assert [(row["threshold"], row["disposition"]) for row in _rows(ns)] == [
        (90, "suppressed_backfill"),
    ]
    assert captured == []


def test_global_or_quota_gate_reenable_and_rule_change_suppress_already_crossed(
    runtime, monkeypatch,
):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 95.0)])
    captured = _capture_dispatch(ns, monkeypatch)
    _write_config(ns, global_enabled=False, quota_enabled=True, actual=(90,))
    _reconcile(quota, now=_at(11))
    assert _rows(ns) == [] and captured == []

    _write_config(ns, global_enabled=True, quota_enabled=True, actual=(90,))
    _reconcile(quota, now=_at(11, 10))
    rows = _rows(ns)
    assert [(row["threshold"], row["disposition"]) for row in rows] == [
        (90, "suppressed_backfill"),
    ]
    prior_fingerprint = _arming(ns)[0]["rule_fingerprint"]

    _write_config(ns, actual=(90, 95))
    _reconcile(quota, now=_at(11, 20))
    rows = _rows(ns)
    assert [(row["threshold"], row["disposition"]) for row in rows] == [
        (90, "suppressed_backfill"), (95, "suppressed_backfill"),
    ]
    assert _arming(ns)[0]["rule_fingerprint"] != prior_fingerprint

    _write_config(ns, actual=(90,))
    _reconcile(quota, now=_at(11, 30))
    _write_config(ns, actual=(90, 95))
    _reconcile(quota, now=_at(11, 40))
    assert len(_rows(ns)) == 2
    assert captured == []


def test_new_reset_window_claims_a_new_terminal_lifecycle(runtime, monkeypatch):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 80.0)])
    _write_config(ns, actual=(90,))
    captured = _capture_dispatch(ns, monkeypatch)

    _reconcile(quota, now=_at(11))
    _seed(ns, observations=[(_at(11, 10), 20, 95.0)])
    _reconcile(quota, now=_at(11, 20))

    next_reset = "2026-07-15T20:00:00+00:00"
    _seed(
        ns, observations=[(_at(11, 30), 30, 95.0)], reset=next_reset,
    )
    _reconcile(quota, now=_at(11, 40))

    assert [(row["resets_at_utc"], row["threshold"], row["disposition"])
            for row in _rows(ns)] == [
        (RESET, 90, "alerted"),
        (next_reset, 90, "alerted"),
    ]
    assert len(captured) == 2


def test_nonmatching_exact_rule_uses_default_thresholds(runtime, monkeypatch):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 80.0)])
    _write_config(
        ns,
        actual=(90,),
        rules=({
            "source": "codex", "source_root_key": "other-root",
            "logical_limit_key": "other-limit", "actual_thresholds": [99],
            "projected_thresholds": [],
        },),
    )
    captured = _capture_dispatch(ns, monkeypatch)

    _reconcile(quota, now=_at(11))
    _seed(ns, observations=[(_at(11, 10), 20, 95.0)])
    _reconcile(quota, now=_at(11, 20))

    assert [(row["threshold"], row["disposition"]) for row in _rows(ns)] == [
        (90, "alerted"),
    ]
    assert len(captured) == 1


def test_projected_claim_prevents_later_actual_and_stale_future_do_not_qualify(
    runtime, monkeypatch,
):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 50.0), (_at(11), 20, 51.0)])
    _write_config(ns, actual=(90,), projected=(90,))
    captured = _capture_dispatch(ns, monkeypatch)
    _reconcile(quota, now=_at(11, 10))  # arm below both thresholds

    _seed(ns, observations=[(_at(12), 30, 80.0)])
    _reconcile(quota, now=_at(12, 10))
    rows = _rows(ns)
    assert [(row["threshold"], row["qualifying_kind"]) for row in rows] == [
        (90, "projected"),
    ]
    _seed(ns, observations=[(_at(12, 20), 40, 95.0)])
    _reconcile(quota, now=_at(12, 30))
    assert len(_rows(ns)) == len(captured) == 1

    # A future latest capture prevents BOTH kinds from claiming a new threshold.
    _seed(ns, root="future", observations=[(_at(15), 10, 100.0)])
    _write_config(ns, actual=(90,), projected=(90,))
    _reconcile(quota, roots=("future",), eligible=("future",), now=_at(12, 30))
    assert [row for row in _rows(ns) if row["source_root_key"] == "future"] == []

    # Stale local evidence may still claim actual, but can never claim projected.
    _seed(ns, root="stale", observations=[(_at(10), 10, 50.0), (_at(11), 20, 51.0)])
    _write_config(ns, actual=(), projected=(90,))
    _reconcile(quota, roots=("stale",), eligible=("stale",), now=_at(11, 10))
    _seed(ns, root="stale", observations=[(_at(12), 30, 80.0)])
    _reconcile(quota, roots=("stale",), eligible=("stale",), now=_at(16))
    assert [row for row in _rows(ns) if row["source_root_key"] == "stale"] == []


def test_parallel_claim_rebuild_reappearance_and_label_change_never_refire(
    runtime, monkeypatch,
):
    ns, quota = runtime
    _seed(ns, observations=[(_at(10), 10, 80.0)])
    _write_config(ns, actual=(90,))
    captured = _capture_dispatch(ns, monkeypatch, status="spawn_error: OSError: nope")
    _reconcile(quota, now=_at(11))
    _seed(ns, observations=[(_at(11, 10), 20, 95.0)], label="Renamed")

    failures = []
    barrier = threading.Barrier(2)

    def run():
        try:
            barrier.wait()
            _reconcile(quota, now=_at(11, 20))
        except Exception as exc:  # assertion below preserves the worker error
            failures.append(exc)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert len(_rows(ns)) == len(captured) == 1
    arming_before = _arming(ns)[0]

    cache = ns["open_cache_db"]()
    try:
        cache.execute("DELETE FROM quota_window_snapshots WHERE source_root_key='root-a'")
        cache.commit()
    finally:
        cache.close()
    _reconcile(quota, now=_at(11, 30))
    assert _rows(ns)[0]["orphaned_at"] is not None

    _seed(ns, observations=[(_at(10), 10, 80.0), (_at(11, 10), 20, 95.0)],
          label="Another label")
    _reconcile(quota, now=_at(11, 40))
    assert len(_rows(ns)) == len(captured) == 1
    assert _rows(ns)[0]["orphaned_at"] is None
    assert _arming(ns)[0]["rule_fingerprint"] == arming_before["rule_fingerprint"]
