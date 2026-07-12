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
        source_path="/tmp/synth.jsonl",
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


def _start_dashboard_server_floor_band_trap(ns, tmp_path, monkeypatch):
    """Seed a stats.db + cache.db with a floor-band-trap shape.

    Mirrors `tests/fixtures/blocks/floor-band-trap`: a prior canonical
    block whose ``five_hour_resets_at`` is OFF the 10-min floor
    (T:39:59 UTC) plus a following block on a :40:00 boundary. The
    8 dense entries in the floor band [T:30, T:39:59) UTC are the trap
    — pre-#76 they got routed into a phantom heuristic block and the
    detail handler 404'd against the EXACT bs of the prior block
    (because panel + detail computed different ``start_time`` shapes).
    """
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    prior_bs   = dt.datetime(2026, 4, 15, 4, 39, 59, tzinfo=dt.timezone.utc)
    prior_rs   = dt.datetime(2026, 4, 15, 9, 39, 59, tzinfo=dt.timezone.utc)
    active_bs  = dt.datetime(2026, 4, 15, 9, 40, 0,  tzinfo=dt.timezone.utc)
    active_rs  = dt.datetime(2026, 4, 15, 14, 40, 0, tzinfo=dt.timezone.utc)
    as_of      = dt.datetime(2026, 4, 15, 9, 45, 0,  tzinfo=dt.timezone.utc)
    # Pin _command_as_of via the CCTALLY_AS_OF env hook so the handler
    # sees the active block as alive.
    monkeypatch.setenv("CCTALLY_AS_OF", as_of.isoformat())

    # Seed stats.db: weekly snapshots + canonical five_hour_blocks rows.
    statsdb = ns["open_db"]()
    week_start = (prior_rs.replace(hour=0, minute=0, second=0, microsecond=0)
                  - dt.timedelta(days=3))
    week_end = week_start + dt.timedelta(days=7)
    statsdb.execute(
        """INSERT INTO weekly_usage_snapshots
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent,
            page_url, source, payload_json,
            five_hour_percent, five_hour_resets_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ((prior_rs - dt.timedelta(seconds=10)).isoformat(),
         week_start.date().isoformat(), week_end.date().isoformat(),
         week_start.isoformat(), week_end.isoformat(),
         20.0, None, "fixture", "{}",
         80.0, prior_rs.isoformat()),
    )
    statsdb.execute(
        """INSERT INTO weekly_usage_snapshots
           (captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent,
            page_url, source, payload_json,
            five_hour_percent, five_hour_resets_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (as_of.isoformat(),
         week_start.date().isoformat(), week_end.date().isoformat(),
         week_start.isoformat(), week_end.isoformat(),
         25.0, None, "fixture", "{}",
         5.0, active_rs.isoformat()),
    )
    prior_window_key  = int(prior_rs.timestamp())  - (int(prior_rs.timestamp())  % 600)
    active_window_key = int(active_rs.timestamp()) - (int(active_rs.timestamp()) % 600)
    statsdb.execute(
        "INSERT INTO five_hour_blocks ("
        "  five_hour_window_key, five_hour_resets_at, block_start_at,"
        "  first_observed_at_utc, last_observed_at_utc,"
        "  final_five_hour_percent, total_cost_usd, is_closed,"
        "  created_at_utc, last_updated_at_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (prior_window_key, prior_rs.isoformat(), prior_bs.isoformat(),
         prior_bs.isoformat(), prior_rs.isoformat(),
         80.0, 0.0, 1,
         prior_bs.isoformat(), prior_rs.isoformat()),
    )
    statsdb.execute(
        "INSERT INTO five_hour_blocks ("
        "  five_hour_window_key, five_hour_resets_at, block_start_at,"
        "  first_observed_at_utc, last_observed_at_utc,"
        "  final_five_hour_percent, total_cost_usd, is_closed,"
        "  created_at_utc, last_updated_at_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (active_window_key, active_rs.isoformat(), active_bs.isoformat(),
         active_bs.isoformat(), as_of.isoformat(),
         5.0, 0.0, 0,
         active_bs.isoformat(), as_of.isoformat()),
    )
    statsdb.commit()

    # Seed cache.db with prior-block + floor-band + active-block entries.
    cache = ns["open_cache_db"]()
    rows = []
    idx = 0
    for i in range(4):
        ts = dt.datetime(2026, 4, 15, 8, 0, i * 10, tzinfo=dt.timezone.utc)
        rows.append(("/x.jsonl", idx, ts.isoformat(),
                     "claude-opus-4-5-20251101", 100, 200, 0, 0))
        idx += 1
    for i in range(8):  # floor-band TRAP
        ts = dt.datetime(2026, 4, 15, 9, 30 + i, 0, tzinfo=dt.timezone.utc)
        rows.append(("/x.jsonl", idx, ts.isoformat(),
                     "claude-opus-4-5-20251101", 300, 400, 0, 0))
        idx += 1
    for i in range(3):
        ts = dt.datetime(2026, 4, 15, 9, 42 + i, 0, tzinfo=dt.timezone.utc)
        rows.append(("/x.jsonl", idx, ts.isoformat(),
                     "claude-opus-4-5-20251101", 500, 600, 0, 0))
        idx += 1
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
        generated_at=as_of,
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


def test_block_detail_floor_band_trap_returns_exact_window(
    tmp_path, monkeypatch,
):
    """Regression for issue #76 — /api/block/:start_at must accept the
    EXACT canonical bs (e.g. ``2026-04-15T04:39:59+00:00``) and return
    the floor-band entries inside the prior block. Pre-fix this 404'd
    because the panel built the prior block with a floored start
    (04:30) while the click came in on the exact bs (04:39:59).
    """
    ns = load_script()
    srv = _start_dashboard_server_floor_band_trap(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]

        # 1. Prior block — exact bs hit (was 404 pre-fix).
        prior_start = "2026-04-15T04:39:59+00:00"
        encoded = urllib.parse.quote(prior_start, safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 200, r.status
        body = json.loads(r.read())
        assert body["start_at"] == prior_start
        # start_at echoes the EXACT canonical bs (04:39:59) — the lookup
        # key — while the display `label` rounds the reset jitter to the
        # nearest 10-min boundary (:39:59 → :40). The two must not be
        # conflated: rounding start_at would 404 the exact-bs lookup.
        # tz-robust: label is "%H:%M %b %d <tz>"; the host zone shifts the
        # hour but the rounded minute field stays :40 (was :39 pre-fix).
        assert body["label"][3:5] == "40", body["label"]
        assert ":39" not in body["label"], body["label"]
        # 4 prior + 8 floor-band entries == 12 inside [04:39:59, 09:39:59).
        assert body["entries_count"] == 12, body["entries_count"]
        assert body["anchor"] == "recorded"
        assert body["is_active"] is False

        # 2. Active block — bs on the :40 boundary.
        active_start = "2026-04-15T09:40:00+00:00"
        encoded = urllib.parse.quote(active_start, safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 200, r.status
        body = json.loads(r.read())
        assert body["start_at"] == active_start
        assert body["entries_count"] == 3
        assert body["is_active"] is True
        assert body["anchor"] == "recorded"

        # 3. Floored prior bs (legacy shape, 04:30) → 404. The exact
        # canonical window no longer renders at the floored start.
        floored_start = "2026-04-15T04:30:00+00:00"
        encoded = urllib.parse.quote(floored_start, safe="")
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/api/block/{encoded}")
        r = c.getresponse()
        assert r.status == 404, r.status
    finally:
        srv.shutdown()
