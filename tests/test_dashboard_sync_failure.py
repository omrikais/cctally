"""Privacy-safe dashboard sync-failure classifications for issue #344."""
from __future__ import annotations

import datetime as dt
import dataclasses

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    loaded = load_script()
    redirect_paths(loaded, monkeypatch, tmp_path)
    return loaded


@pytest.mark.parametrize(
    ("raw_error", "kind", "label", "action"),
    [
        (
            "sync-cache: database disk image is malformed at "
            "/private/secret/cache.db",
            "cache_corruption",
            "⚠ cache recovery needed",
            "cctally cache-sync --rebuild",
        ),
        (
            "sync-cache-open: cache.db maintenance is in progress "
            "(/private/secret/cache.db.repairing)",
            "maintenance_active",
            "cache repair in progress",
            None,
        ),
        (
            "sync-cache-open: cache.db maintenance is in progress: could not "
            "remove stale repair marker /private/secret/cache.db.repairing",
            "maintenance_stale",
            "⚠ cache repair blocked",
            "cctally cache-sync --rebuild",
        ),
        (
            "sync-cache: disk gone at /private/secret/cache.db",
            "server_sync",
            "⚠ server sync error",
            None,
        ),
    ],
)
def test_sync_failure_is_actionable_without_leaking_raw_error(
    ns, raw_error, kind, label, action,
):
    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        generated_at=now,
        last_sync_error=raw_error,
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert envelope["sync_failure"]["kind"] == kind
    assert envelope["sync_failure"]["label"] == label
    assert envelope["sync_failure"]["action"] == action
    assert "/private/secret" not in str(envelope["sync_failure"])


def test_sync_failure_is_null_when_sync_is_healthy(ns):
    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    envelope = ns["snapshot_to_envelope"](
        ns["_empty_dashboard_snapshot"](),
        now_utc=now,
    )

    assert envelope["sync_failure"] is None
