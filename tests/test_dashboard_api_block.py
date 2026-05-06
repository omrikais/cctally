"""Tests for /api/block/:start_at — detail builder + HTTP handler."""
import datetime as dt

from conftest import load_script


def _make_entry(ns, *, ts: dt.datetime, model: str,
                input_tokens=100, output_tokens=50,
                cache_create=1000, cache_read=5000):
    UsageEntry = ns["UsageEntry"]
    return UsageEntry(
        timestamp=ts,
        model=model,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
        },
        cost_usd=None,
    )


def test_build_block_detail_completed_block_shape():
    ns = load_script()
    start = dt.datetime(2026, 4, 26, 9, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=5)
    entries = [
        _make_entry(ns, ts=start + dt.timedelta(minutes=10),
                    model="claude-opus-4-5-20251101"),
        _make_entry(ns, ts=start + dt.timedelta(minutes=90),
                    model="claude-opus-4-5-20251101"),
        _make_entry(ns, ts=start + dt.timedelta(minutes=200),
                    model="claude-sonnet-4-6-20251015"),
    ]
    block = ns["_build_activity_block"](
        entries, start, end,
        now=end + dt.timedelta(hours=1),  # block is completed
        mode="auto",
        anchor="recorded",
    )
    detail = ns["_build_block_detail"](block, entries)
    # Schema fields all present
    for k in ("start_at", "end_at", "actual_end_at", "anchor", "is_active",
              "label", "entries_count", "cost_usd", "total_tokens",
              "input_tokens", "output_tokens", "cache_creation_tokens",
              "cache_read_tokens", "cache_hit_pct", "models",
              "burn_rate", "projection", "samples"):
        assert k in detail, f"missing {k}"
    # Completed block: burn + projection are null
    assert detail["is_active"] is False
    assert detail["burn_rate"] is None
    assert detail["projection"] is None
    assert detail["entries_count"] == 3
    assert detail["anchor"] == "recorded"
    # Samples carry per-entry cumulative cost in entry order
    assert len(detail["samples"]) == 3
    cums = [s["cum"] for s in detail["samples"]]
    assert cums == sorted(cums)  # monotone non-decreasing


def test_build_block_detail_reconcile_invariant():
    """Sum of per-entry costs (last cumulative sample) must equal
    block.cost_usd within the project's 1e-9 USD tolerance."""
    ns = load_script()
    start = dt.datetime(2026, 4, 26, 9, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=5)
    entries = [
        _make_entry(ns, ts=start + dt.timedelta(minutes=i * 7),
                    model="claude-opus-4-5-20251101")
        for i in range(20)
    ]
    block = ns["_build_activity_block"](
        entries, start, end,
        now=end + dt.timedelta(hours=1),
        mode="auto", anchor="recorded",
    )
    detail = ns["_build_block_detail"](block, entries)
    last_cum = detail["samples"][-1]["cum"]
    assert abs(last_cum - detail["cost_usd"]) < 1e-9


def test_build_block_detail_active_block_has_burn_and_projection():
    ns = load_script()
    start = dt.datetime(2026, 4, 26, 14, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=5)
    now = start + dt.timedelta(hours=2)  # mid-window
    entries = [
        _make_entry(ns, ts=start + dt.timedelta(minutes=15),
                    model="claude-opus-4-5-20251101"),
        _make_entry(ns, ts=start + dt.timedelta(minutes=80),
                    model="claude-opus-4-5-20251101"),
    ]
    block = ns["_build_activity_block"](
        entries, start, end, now=now, mode="auto", anchor="recorded",
    )
    detail = ns["_build_block_detail"](block, entries)
    assert detail["is_active"] is True
    assert detail["burn_rate"] is not None
    assert detail["projection"] is not None
    assert "tokens_per_minute" in detail["burn_rate"]
    assert "cost_per_hour" in detail["burn_rate"]
    assert "total_tokens" in detail["projection"]
    assert "total_cost_usd" in detail["projection"]
    assert "remaining_minutes" in detail["projection"]


def test_build_block_detail_cache_hit_pct_null_when_zero_denominator():
    ns = load_script()
    start = dt.datetime(2026, 4, 26, 9, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=5)
    entries = [
        _make_entry(ns, ts=start + dt.timedelta(minutes=10),
                    model="claude-opus-4-5-20251101",
                    input_tokens=0, cache_create=0, cache_read=0),
    ]
    block = ns["_build_activity_block"](
        entries, start, end,
        now=end + dt.timedelta(hours=1),
        mode="auto", anchor="recorded",
    )
    detail = ns["_build_block_detail"](block, entries)
    assert detail["cache_hit_pct"] is None


def test_build_block_detail_cache_hit_pct_includes_cache_creation():
    """cache_hit_pct denominator MUST include cache_creation_tokens.
    Real-world data from a heavy-cache active block where the bug
    surfaced as 100.0% in the dashboard modal: input=817,
    cache_create=1,425,691, cache_read=41,689,401. Correct ratio is
    cache_read / (input + cache_create + cache_read) ≈ 96.69%.
    Without cache_creation in the denominator the ratio inflates to
    99.998% which floor-rounds to 100.0% on display.
    """
    ns = load_script()
    start = dt.datetime(2026, 4, 26, 17, 30, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=5)
    entries = [
        _make_entry(ns, ts=start + dt.timedelta(minutes=10),
                    model="claude-opus-4-5-20251101",
                    input_tokens=817, output_tokens=286769,
                    cache_create=1_425_691, cache_read=41_689_401),
    ]
    block = ns["_build_activity_block"](
        entries, start, end,
        now=end + dt.timedelta(hours=1),
        mode="auto", anchor="recorded",
    )
    detail = ns["_build_block_detail"](block, entries)
    expected = (41_689_401 / (817 + 1_425_691 + 41_689_401)) * 100.0
    assert abs(detail["cache_hit_pct"] - expected) < 1e-9
    # Sanity: the bug used to display 100.0% — assert the fix is well
    # below that threshold, not just numerically close to expected.
    assert detail["cache_hit_pct"] < 97.0


# ---- HTTP endpoint tests (Task 2) ----

import json
import pathlib
import sys
import threading
import urllib.parse
from http.client import HTTPConnection


def _start_dashboard_server(ns, tmp_path, monkeypatch):
    """Boot a real DashboardHTTPHandler against a fixture cache.

    Mirrors the pattern in tests/test_dashboard_api_events.py — see that
    file for the canonical wiring.
    """
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    # Seed one block worth of entries via cache.db (Apr 22, 14:00 UTC).
    # Minute offsets exceed 59 (90, 150) — convert to wall-clock via timedelta.
    cache = ns["open_cache_db"]()
    block_start = dt.datetime(2026, 4, 22, 14, 0, tzinfo=dt.timezone.utc)
    cache.executemany(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("/x.jsonl", i,
             (block_start + dt.timedelta(minutes=m)).isoformat(),
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000)
            for i, m in enumerate([5, 20, 45, 90, 150])
        ],
    )
    cache.commit()

    # Build a snapshot, install class attrs the handler reads, start server.
    import socketserver
    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]
    DataSnapshot = ns["DataSnapshot"]

    snap = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=ns["dt"].datetime(2026, 4, 22, 20, 0,
                                        tzinfo=ns["dt"].timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )
    HandlerCls.snapshot_ref = SnapshotRef(snap)
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)

    srv = socketserver.TCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_api_block_endpoint_happy_path(tmp_path, monkeypatch):
    ns = load_script()
    srv = _start_dashboard_server(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        start_at = "2026-04-22T14:00:00+00:00"
        encoded = urllib.parse.quote(start_at, safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 200, r.status
        body = json.loads(r.read())
        assert body["start_at"] == start_at
        assert body["entries_count"] == 5
        assert isinstance(body["samples"], list) and len(body["samples"]) == 5
        # Reconcile holds across the wire too
        assert abs(body["samples"][-1]["cum"] - body["cost_usd"]) < 1e-9
    finally:
        srv.shutdown()


def test_api_block_endpoint_404_unknown_start_at(tmp_path, monkeypatch):
    ns = load_script()
    srv = _start_dashboard_server(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        encoded = urllib.parse.quote("2030-01-01T00:00:00+00:00", safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 404
    finally:
        srv.shutdown()


def test_api_block_endpoint_400_malformed_start_at(tmp_path, monkeypatch):
    ns = load_script()
    srv = _start_dashboard_server(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/block/not-a-datetime")
        r = c.getresponse()
        assert r.status == 400
    finally:
        srv.shutdown()


def _start_dashboard_server_with_neighbour_block(ns, tmp_path, monkeypatch):
    """Same as `_start_dashboard_server` but seeds an extra entry 2h
    BEFORE the requested block. Reproduces the cross-week-boundary
    scenario that used to 404 when the detail handler grouped entries
    over a wider window than the panel did.
    """
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    cache = ns["open_cache_db"]()
    block_start = dt.datetime(2026, 4, 22, 14, 0, tzinfo=dt.timezone.utc)
    # Two entries inside the requested block AND one entry 2h before it
    # (which is < BLOCK_DURATION away — the bug case). The earlier entry
    # would heuristic-anchor a block at 12:00, swallowing the 14:00 entries
    # if the detail handler doesn't filter to [start_at, end_at) before
    # grouping.
    rows = [
        ("/x.jsonl", 0,
         (block_start - dt.timedelta(hours=2)).isoformat(),
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        ("/x.jsonl", 1,
         (block_start + dt.timedelta(minutes=10)).isoformat(),
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        ("/x.jsonl", 2,
         (block_start + dt.timedelta(minutes=120)).isoformat(),
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    ]
    cache.executemany(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    cache.commit()

    import socketserver
    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]
    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=ns["dt"].datetime(2026, 4, 22, 20, 0,
                                        tzinfo=ns["dt"].timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )
    HandlerCls.snapshot_ref = SnapshotRef(snap)
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    srv = socketserver.TCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_api_block_endpoint_ignores_entries_outside_requested_window(
    tmp_path, monkeypatch,
):
    """Regression: an entry less than BLOCK_DURATION before start_at must
    not bleed into the grouping and shift the heuristic anchor. The
    panel filters entries to the week before grouping; the detail
    handler must mirror that discipline (filter to [start_at, end_at)).
    """
    ns = load_script()
    srv = _start_dashboard_server_with_neighbour_block(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        start_at = "2026-04-22T14:00:00+00:00"
        encoded = urllib.parse.quote(start_at, safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 200, r.status
        body = json.loads(r.read())
        # Block starts where requested (NOT shifted earlier by the 12:00
        # entry).
        assert body["start_at"] == start_at
        # Only the two entries inside [14:00, 19:00) are counted.
        assert body["entries_count"] == 2
    finally:
        srv.shutdown()
