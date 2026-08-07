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


def test_typed_conversations_corruption_never_becomes_cache_recovery(ns):
    """Typed transcript ownership wins over corruption-shaped raw text."""
    import _cctally_tui as tui

    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    raw_error = (
        "conversation-title: database disk image is malformed at "
        "/private/secret/conversations.db"
    )
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        generated_at=now,
        last_sync_error=raw_error,
        sync_failures=(
            tui.SyncFailureAttribution(
                leg="conversation-title",
                database="conversations",
                corruption=True,
            ),
        ),
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert envelope["sync_failure"] == {
        "kind": "server_sync",
        "label": "⚠ server sync error",
        "detail": "The server could not complete its background sync.",
        "action": None,
    }
    assert "/private/secret" not in str(envelope["sync_failure"])


def test_tui_attribution_preserves_conversations_ownership(ns):
    import _cctally_tui as tui

    class NoStatsProbe:
        def execute(self, _sql):
            raise AssertionError("transcript attribution queried stats.db")

    database, corruption = tui._tui_attribute_corruption(
        NoStatsProbe(),
        RuntimeError("database disk image is malformed"),
        database="conversations",
    )

    assert database == "conversations"
    assert corruption is True


def test_a_refused_quota_projection_is_rendered_with_its_remedy(ns):
    """#496 S5b. The refusal message names `cctally cache-sync`, but it lands in
    `last_sync_error`, which this module's contract forbids putting in a
    visible text, title or aria surface. Without a branch of its own the
    attribution reached the terminal default — "⚠ server sync error" with no
    action — because `database="other"` matches nothing above it.
    """
    import _cctally_tui as tui

    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        generated_at=now,
        last_sync_error=(
            "quota-projection: the published quota projection is incomplete; "
            "run `cctally cache-sync` to reconcile it"
        ),
        sync_failures=(
            tui.SyncFailureAttribution(
                leg="quota-projection", database="other", corruption=False,
            ),
        ),
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert envelope["sync_failure"]["kind"] == "quota_projection_incomplete"
    assert envelope["sync_failure"]["label"] == "quota view reconciling"
    assert envelope["sync_failure"]["action"] == "cctally cache-sync"


def test_another_other_leg_is_not_captured_by_the_projection_branch(ns):
    """Non-vacuity: the branch keys on the LEG, not on `database="other"`,
    which is deliberately generic and carries several unrelated legs."""
    import _cctally_tui as tui

    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        generated_at=now,
        last_sync_error="prior-source-bundle: something else went wrong",
        sync_failures=(
            tui.SyncFailureAttribution(
                leg="prior-source-bundle", database="other", corruption=False,
            ),
        ),
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert envelope["sync_failure"]["kind"] == "server_sync"
    assert envelope["sync_failure"]["action"] is None


def test_a_stats_fault_still_wins_a_mixed_projection_failure(ns):
    """Ordering: stats ownership is decided above the projection branch, so a
    tick that met both reports the one with the destructive remedy."""
    import _cctally_tui as tui

    now = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.timezone.utc)
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        generated_at=now,
        last_sync_error="stats-open: database disk image is malformed",
        sync_failures=(
            tui.SyncFailureAttribution(
                leg="quota-projection", database="other", corruption=False,
            ),
            tui.SyncFailureAttribution(
                leg="stats-open", database="stats", corruption=True,
            ),
        ),
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert envelope["sync_failure"]["kind"] == "stats_corruption"
