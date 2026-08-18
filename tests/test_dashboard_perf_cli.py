"""`cctally dashboard-perf` — the reader and the runtime trace arm (#583 S1 §3.3).

Contract under test: exit codes, the stamped JSON envelope, the loopback
refusal, the per-regime period derivation, and the literal `no samples yet`
rendering. The zero-sample string is asserted verbatim because the whole point
of the surface is that an operator can tell "measured and fast" from "not
measured", and a dash or a zero cannot say the second.
"""
import argparse
import json
import socket
import socketserver
import threading

import pytest

from conftest import load_script, redirect_paths  # type: ignore


def _boot(ns, tmp_path, monkeypatch, *, token=None):
    redirect_paths(ns, monkeypatch, tmp_path)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.static_dir = ns["STATIC_DIR"]
    H.cctally_host = "127.0.0.1"
    H.cctally_expose_transcripts = False
    H.cctally_api_token = token
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _args(**kw):
    base = {"host": "127.0.0.1", "port": None, "token": None,
            "trace": None, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed_ticks(ts, *, regime, periods_ns):
    """One tick per gap, so `period_ns` is the injected difference."""
    published = 1_000_000_000
    first = ts.begin_tick()
    first.set_dispatch("full")
    first.set_codex_regime(regime)
    first.mark_ingest(2_000_000)
    first.mark_build(3_000_000)
    first.finish(published_ns=published, published_at="2026-08-15T00:00:00Z")
    for gap in periods_ns:
        published += gap
        t = ts.begin_tick()
        t.set_dispatch("full")
        t.set_codex_regime(regime)
        t.mark_ingest(2_000_000)
        t.mark_build(3_000_000)
        t.finish(published_ns=published, published_at="2026-08-15T00:00:00Z")


# ── the pure derivation (spec §3.3) ─────────────────────────────────────────


def test_the_period_summary_is_partitioned_by_regime_and_uses_the_median():
    load_script()
    import _cctally_dashboard_perf as dp
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    # One huge startup outlier plus three tight samples: the mean would be
    # dominated by the outlier, the median must not be.
    _seed_ticks(ts, regime="active",
                periods_ns=[60_000_000_000, 9_000_000_000,
                            10_000_000_000, 11_000_000_000])
    records = [r.as_wire() for r in ts.snapshot().records]
    summary = dp.summarise_regime_periods(records)

    active = summary["active"]
    assert active["samples"] == 4
    assert active["median_ns"] == 10_500_000_000, (
        "the median moved; a mean would have been ~22.5s here")
    assert active["min_ns"] == 9_000_000_000
    assert active["max_ns"] == 60_000_000_000
    assert summary["idle"]["samples"] == 0
    ts.reset_for_tests()


def test_a_null_period_and_an_unobserved_regime_are_discarded():
    load_script()
    import _cctally_dashboard_perf as dp
    records = [
        {"codex_regime": "active", "period_ns": None},      # the first publish
        {"codex_regime": "not_observed", "period_ns": 5},   # never counted
        {"codex_regime": "idle", "period_ns": 7},
    ]
    summary = dp.summarise_regime_periods(records)
    assert summary["active"]["samples"] == 0
    assert summary["idle"]["samples"] == 1
    assert "not_observed" not in summary


def test_a_regime_with_no_samples_says_so_rather_than_showing_a_zero():
    load_script()
    import _cctally_dashboard_perf as dp
    payload = {
        "tick": {"dispatch_counts": {"idle": 0, "full": 0, "degraded": 0},
                 "cache_open_failures": {"daily": 0, "weekly": 0, "monthly": 0},
                 "tick_seq": 0, "records": [], "standalone": None},
        "tracing": {"requested": False, "applied": False, "applies_at": "none"},
        "phases": None, "generated_at": None,
    }
    text = dp.render_dashboard_perf(payload)
    # Three period rows plus the #583 S4 conversation-loop row, which states
    # the same literal for the same reason on an empty second ring.
    assert text.count("no samples yet") == 4, (
        f"all four rows must state it explicitly:\n{text}")
    assert "Codex-active" in text and "Codex-idle" in text
    assert "all ticks" in text
    for token in ("0.0s", " 0s", "0ms", "—", "-\n"):
        assert f"period {token}" not in text


def test_the_report_names_the_ingest_and_builder_halves_and_the_mix():
    load_script()
    import _cctally_dashboard_perf as dp
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    _seed_ticks(ts, regime="active", periods_ns=[9_000_000_000])
    ts.note_cache_open_failure("daily")
    snap = ts.snapshot()
    payload = {
        "tick": {"dispatch_counts": dict(snap.dispatch_counts),
                 "cache_open_failures": dict(snap.cache_open_failures),
                 "tick_seq": snap.tick_seq,
                 "records": [r.as_wire() for r in snap.records],
                 "standalone": None},
        "tracing": {"requested": True, "applied": False,
                    "applies_at": "next_authoritative_build"},
        "phases": None, "generated_at": None,
    }
    text = dp.render_dashboard_perf(payload)
    assert "ingest" in text and "builder" in text
    # The all-ticks row is the flagship figure and must carry a figure
    # whatever the regime partition does with the same records.
    all_ticks = [line for line in text.splitlines() if "all ticks" in line]
    assert len(all_ticks) == 1, text
    assert "no samples yet" not in all_ticks[0], (
        f"the all-ticks period was withheld despite a qualifying record: "
        f"{all_ticks[0]!r}")
    assert "median" in all_ticks[0]
    assert "full" in text and "idle" in text and "degraded" in text
    assert "daily" in text and "1" in text
    assert "next_authoritative_build" in text, "the pending arm must be visible"
    ts.reset_for_tests()


# ── argument validation (exit 2) ────────────────────────────────────────────


@pytest.mark.parametrize("host", ["192.168.0.9", "0.0.0.0", "localhost",
                                  "example.com", "10.0.0.1", "not-an-ip"])
def test_a_non_loopback_target_is_refused_before_any_connection(host, capsys):
    ns = load_script()
    rc = ns["cmd_dashboard_perf"](_args(host=host))
    assert rc == 2, f"{host} was accepted"
    assert host in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "127.5.5.5", "::1"])
def test_every_loopback_literal_is_accepted_as_a_target(host):
    """`::1` is accepted but unreachable in practice, and that is recorded
    rather than fixed: `cctally dashboard --host ::1` cannot bind at all, so
    no dashboard this command could talk to ever listens there. The literal
    stays accepted because refusing it would be a second, different lie about
    what loopback means."""
    ns = load_script()
    import _cctally_dashboard_perf as dp
    assert dp.resolve_loopback_target(host) == host


# ── transport failures (exit 3) ─────────────────────────────────────────────


def test_no_dashboard_running_exits_three(capsys):
    ns = load_script()
    rc = ns["cmd_dashboard_perf"](_args(port=_free_port()))
    assert rc == 3
    err = capsys.readouterr().err
    assert "dashboard-perf:" in err


def test_no_dashboard_running_still_emits_a_stamped_json_envelope(capsys):
    ns = load_script()
    rc = ns["cmd_dashboard_perf"](_args(port=_free_port(), json=True))
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert list(payload)[0] == "schemaVersion", "the stamp must come first"
    assert payload["status"] == "error"


# ── the live surface (exit 0) ───────────────────────────────────────────────


def test_a_running_dashboard_renders_a_report(monkeypatch, tmp_path, capsys):
    ns = load_script()
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    _seed_ticks(ts, regime="active", periods_ns=[9_000_000_000])
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        rc = ns["cmd_dashboard_perf"](_args(port=srv.server_address[1]))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Codex-active" in out
        assert "no samples yet" in out, "the idle regime has no samples here"
        assert "ingest" in out and "builder" in out
    finally:
        ts.reset_for_tests()
        srv.shutdown()
        srv.server_close()


def test_json_mode_stamps_the_envelope_and_passes_the_payload_through(
    monkeypatch, tmp_path, capsys,
):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        rc = ns["cmd_dashboard_perf"](
            _args(port=srv.server_address[1], json=True))
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert list(payload)[0] == "schemaVersion"
        assert payload["schemaVersion"] == 1
        assert payload["status"] == "ok"
        # `diagnostic` is the server payload verbatim and explicitly opaque.
        assert payload["diagnostic"]["tick"]["tick_seq"] >= 0
        assert "tracing" in payload["diagnostic"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_trace_on_and_off_drive_the_gated_post(monkeypatch, tmp_path, capsys):
    ns = load_script()
    import _lib_perf as perf
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        perf.set_enabled(False)
        rc = ns["cmd_dashboard_perf"](
            _args(port=srv.server_address[1], trace="on"))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert perf.pending_state()[0] is True, (
            "the request never reached the mailbox")
        assert "next_authoritative_build" in out

        perf.apply_pending()
        assert perf.enabled() is True
        rc = ns["cmd_dashboard_perf"](
            _args(port=srv.server_address[1], trace="off"))
        assert rc == 0
        assert perf.pending_state()[0] is False
    finally:
        perf.request_enabled(False)
        perf.apply_pending()
        perf.set_enabled(False)
        srv.shutdown()
        srv.server_close()


def test_a_bearer_protected_dashboard_needs_the_token(monkeypatch, tmp_path,
                                                      capsys):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, token="s3cret")
    try:
        port = srv.server_address[1]
        assert ns["cmd_dashboard_perf"](_args(port=port)) == 3
        assert "dashboard-perf:" in capsys.readouterr().err
        assert ns["cmd_dashboard_perf"](_args(port=port, token="s3cret")) == 0
    finally:
        ns["DashboardHTTPHandler"].cctally_api_token = None
        srv.shutdown()
        srv.server_close()


def test_the_command_is_registered_and_help_is_well_formed():
    ns = load_script()
    parser = ns["build_parser"]()
    args = parser.parse_args(["dashboard-perf", "--trace", "on"])
    assert args.func is ns["cmd_dashboard_perf"]
    assert args.trace == "on"
    text = parser.format_help()
    assert "dashboard-perf" in text


def test_an_all_idle_install_still_reports_its_publish_period():
    """The flagship number must not be gated by the regime partition.

    Measured on a real dashboard at `--sync-interval 3`: after 28 ticks both
    regime rows read `no samples yet`, because a dispatch-`idle` tick returns
    before `_tui_build_source_bundle`, no build reaches the Codex decision, and
    the tick is stamped `not_observed` — which the partition discards. Every
    record after the first nevertheless carried a correct `period_ns`. The Goal
    says an operator on a slow install must be able to learn that install's
    publish period, and on a mostly-idle install they learned nothing.
    """
    load_script()
    import _cctally_dashboard_perf as dp
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    published = 0
    for _ in range(6):
        published += 3_000_000_000
        tick = ts.begin_tick()
        tick.set_dispatch("idle")          # no build reaches the Codex decision
        tick.finish(published_ns=published, published_at="2026-08-16T00:00:00Z")
    records = [r.as_wire() for r in ts.snapshot().records]
    assert all(r["codex_regime"] == "not_observed" for r in records), (
        "precondition: every tick must be regime-unobserved")

    summary = dp.summarise_regime_periods(records)
    assert summary["active"]["samples"] == 0 and summary["idle"]["samples"] == 0

    overall = dp.summarise_all_periods(records)
    assert overall["samples"] == 5, "the first publish has no predecessor"
    assert overall["median_ns"] == 3_000_000_000

    text = dp.render_dashboard_perf({
        "tick": {"dispatch_counts": dict(ts.snapshot().dispatch_counts),
                 "cache_open_failures": dict(
                     ts.snapshot().cache_open_failures),
                 "tick_seq": ts.snapshot().tick_seq,
                 "records": records, "standalone": None},
        "tracing": {"requested": False, "applied": False, "applies_at": "none"},
        "phases": None,
    })
    assert "3.00s" in text, f"the period never reached the report:\n{text}"
    # The two regime rows, plus the #583 S4 conversation row: this payload
    # carries no `conversation_sync` key at all, so that second loop has no
    # samples here either.
    assert text.count("no samples yet") == 3, (
        "only the two regime rows and the conversation row may be empty here")
    ts.reset_for_tests()


# --- #583 S4: the conversation sync loop's section -------------------------


def _conversation_payload(rows):
    return {
        "tick": {"dispatch_counts": {"idle": 0, "full": 0, "degraded": 0},
                 "cache_open_failures": {"daily": 0, "weekly": 0,
                                         "monthly": 0},
                 "tick_seq": 0, "records": [], "standalone": None,
                 "conversation_sync": rows},
        "tracing": {"requested": False, "applied": False, "applies_at": "none"},
        "phases": None, "generated_at": None,
    }


def _pass(seq, *, cpu_ns, period_ns, duration_ns, status="ok"):
    return {"seq": seq, "started_ns": 0, "ended_ns": duration_ns,
            "duration_ns": duration_ns, "cpu_ns": cpu_ns,
            "period_ns": period_ns, "status": status}


class _VirtualClock:
    """A monotonic clock nothing but this test advances (D-1: no wall time)."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _drive_the_real_loop(work_costs, *, interval=5.0, cpu_fraction=1.0):
    """Drive the REAL `_conversation_sync_loop` through the REAL recorder on a
    virtual clock, and return the published wire rows.

    Hand-built rows cannot gate the pairing convention. They are written to
    conform to whichever convention the implementation already uses, so
    inverting the recorder in either direction leaves an assertion over them
    green — the criterion has to reach the loop and the recorder to mean
    anything. By default all of the pass's wall time is charged as thread CPU,
    which is the worst case for the share; `cpu_fraction` charges less than the
    wall so a driver can tell the two apart.
    """
    import importlib

    load_script()
    dash = importlib.import_module("_cctally_dashboard")
    import _lib_tick_stats as ts

    ts.reset_for_tests()
    clock = _VirtualClock()
    cpu = {"ns": 0}
    remaining = list(work_costs)

    class _Stop:
        def is_set(self):
            return not remaining

    def run_iteration():
        cost = remaining.pop(0)
        clock.advance(cost)
        cpu["ns"] += int(cost * cpu_fraction * 1e9)
        return "ok"

    try:
        dash._conversation_sync_loop(
            stop=_Stop(),
            interval=interval,
            run_iteration=run_iteration,
            monotonic=clock.monotonic,
            thread_time_ns=lambda: cpu["ns"],
            wait=lambda seconds: clock.advance(max(0.0, seconds)),
            record=ts.record_conversation_pass,
        )
        return [r.as_wire() for r in ts.snapshot().conversation_records]
    finally:
        ts.reset_for_tests()


def _rendered_share(rows):
    """The percentage the operator actually reads, plus the whole section."""
    import _cctally_dashboard_perf as dp

    out = "\n".join(dp._render_conversation_sync({"conversation_sync": rows}))
    line = next(ln for ln in out.splitlines() if "cpu share" in ln)
    return float(line.split()[2].rstrip("%")), out


def test_cpu_share_numerator_and_denominator_cover_the_same_span():
    """Criterion 6b, driven through the real loop and the real recorder.

    Three cheap passes, then one 30-second pass. The retained forward periods
    span the start of pass 1 to the start of pass 4 — 18 seconds — and the loop
    spent 3 seconds of CPU inside that span, so the true duty is 16.7%. Pairing
    each pass's CPU with the interval that PRECEDED it charges the 30-second
    pass against a 6-second interval and reports 177.8%, which is not a
    possible duty for one thread.
    """
    rows = _drive_the_real_loop([1.0, 1.0, 1.0, 30.0], interval=5.0)
    assert [r["period_ns"] for r in rows] == [
        6_000_000_000, 6_000_000_000, 6_000_000_000, None,
    ], "the newest pass's interval is not known until its successor starts"
    share, out = _rendered_share(rows)
    assert share == 16.7, out


def test_the_rendered_share_can_never_exceed_the_duty_bound():
    """Sanity property. When every pass satisfies `work >= interval` the loop's
    own deadline holds the thread at 50% of one core, so a rendered share above
    that is a reporting defect and not a measurement.

    The costs alternate and every millisecond of wall time is charged as CPU,
    which is the extremal case: the share must reach 50.0% and must not pass it.
    """
    rows = _drive_the_real_loop([10.0, 30.0, 7.0, 50.0, 12.0], interval=5.0)
    share, out = _rendered_share(rows)
    assert share <= 50.0, out
    assert share == 50.0, (
        "non-vacuity: the worst case must reach the bound, not sit under it\n"
        + out
    )


def test_the_share_numerator_is_thread_cpu_and_not_wall_time():
    """The numerator must come from `time.thread_time_ns()`, not from the wall.

    Every other driver in this estate charges CPU equal to the pass's wall cost
    or to a constant zero, so `cpu_ns` and `duration_ns` are indistinguishable
    and a loop writing `cpu_ns=int(work * 1e9)` passes the whole suite while the
    published figure silently becomes wall time. Charging HALF the wall as CPU
    separates them: the same cost script that renders 50.0% at full CPU must
    render 25.0% here, and a wall-time numerator renders 50.0% either way.
    """
    costs = [10.0, 30.0, 7.0, 50.0, 12.0]
    full, _ = _rendered_share(_drive_the_real_loop(costs, interval=5.0))
    rows = _drive_the_real_loop(costs, interval=5.0, cpu_fraction=0.5)
    share, out = _rendered_share(rows)
    assert full == 50.0
    assert share == 25.0, (
        "the numerator tracked wall time, not thread CPU\n" + out
    )
    assert [r["cpu_ns"] for r in rows[:-1]] != [
        r["duration_ns"] for r in rows[:-1]
    ], "the fixture failed to separate CPU from wall"


def test_the_newest_pass_carries_no_period_and_is_excluded_from_both_sums():
    """The ring-eviction boundary, driven through the real recorder.

    Under the forward convention the only record without a period is the
    NEWEST one, because its successor has not started. That is what makes the
    renderer's exclusion reachable rather than dead code: with the interval
    read backwards, `period_ns` is null only for `seq == 1`, so after a
    process's first 64 passes no retained record is ever excluded.

    The last pass here costs 20 seconds against 1-second neighbours, so
    including it would visibly inflate the figure.
    """
    load_script()
    import _lib_tick_stats as ts

    costs = [1.0] * (ts.RING_CAPACITY + 5)
    costs[-1] = 20.0
    rows = _drive_the_real_loop(costs, interval=5.0)
    assert len(rows) == ts.RING_CAPACITY
    assert rows[-1]["period_ns"] is None
    assert rows[-1]["cpu_ns"] == 20_000_000_000
    assert all(r["period_ns"] == 6_000_000_000 for r in rows[:-1])
    # 63 retained passes carry a period: 63 s of CPU over 63 * 6 s = 16.7%.
    share, out = _rendered_share(rows)
    assert share == 16.7, out


def test_a_row_missing_its_fields_degrades_instead_of_crashing():
    """The wall and CPU rows already read defensively. A server publishing a
    different field set must not take the reader down on the share row alone.
    """
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        {"seq": 1},
        {"seq": 2, "cpu_ns": 5_000_000_000},
        {"seq": 3, "period_ns": 9_000_000_000},
        _pass(4, cpu_ns=1_000_000_000, period_ns=4_000_000_000,
              duration_ns=1_000_000_000),
    ]
    out = "\n".join(dp._render_conversation_sync({"conversation_sync": rows}))
    assert "25.0% of one core" in out, out


def test_a_non_positive_period_is_excluded_from_the_denominator():
    """A truthiness filter drops a legitimate zero and KEEPS a negative, and a
    negative period subtracts from the denominator instead of being ignored."""
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        _pass(1, cpu_ns=5_000_000_000, period_ns=0,
              duration_ns=1_000_000_000),
        _pass(2, cpu_ns=1_000_000_000, period_ns=-8_000_000_000,
              duration_ns=1_000_000_000),
        _pass(3, cpu_ns=1_000_000_000, period_ns=4_000_000_000,
              duration_ns=1_000_000_000),
    ]
    out = "\n".join(dp._render_conversation_sync({"conversation_sync": rows}))
    assert "25.0% of one core" in out, out


def test_a_row_without_a_status_is_counted_as_malformed():
    """`str(record.get("status"))` files a missing status under the literal
    `None`, which reads on the report as though it were an outcome name."""
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        {"seq": 1, "cpu_ns": 0, "period_ns": None, "duration_ns": 0},
        _pass(2, cpu_ns=0, period_ns=1_000_000_000, duration_ns=0),
    ]
    out = "\n".join(dp._render_conversation_sync({"conversation_sync": rows}))
    assert "malformed 1" in out, out
    assert "None" not in out, out


def test_the_status_row_aligns_with_its_siblings():
    """Every other row pads its label to 14 columns and then writes a space."""
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        _pass(1, cpu_ns=0, period_ns=None, duration_ns=2_000_000_000),
        _pass(2, cpu_ns=0, period_ns=4_000_000_000,
              duration_ns=2_000_000_000),
    ]
    lines = dp._render_conversation_sync({"conversation_sync": rows})
    wall = next(ln for ln in lines if ln.startswith("  wall"))
    status = next(ln for ln in lines if ln.startswith("  status"))
    assert status.index("ok") == wall.index("mean"), (
        "the status row renders one column left of every sibling:\n"
        + "\n".join(lines)
    )


def test_empty_conversation_ring_renders_no_samples_yet_not_zero():
    """`--no-sync` never starts the thread, so this is that mode's correct and
    permanent reading — and a zero would be indistinguishable from a measured
    idle loop."""
    load_script()
    import _cctally_dashboard_perf as dp
    out = "\n".join(dp._render_conversation_sync({"conversation_sync": []}))
    assert "no samples yet" in out
    assert "0.0%" not in out
    assert "0ms" not in out


def test_the_conversation_section_reports_wall_cpu_period_and_status():
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        _pass(1, cpu_ns=1_000_000_000, period_ns=None,
              duration_ns=2_000_000_000),
        _pass(2, cpu_ns=1_000_000_000, period_ns=4_000_000_000,
              duration_ns=2_000_000_000, status="store_unavailable"),
    ]
    out = "\n".join(dp._render_conversation_sync({"conversation_sync": rows}))
    assert "Conversation sync loop" in out
    assert "wall" in out and "thread cpu" in out
    assert "period" in out
    assert "store_unavailable 1" in out and "ok 1" in out


def test_the_conversation_section_appears_in_the_whole_report():
    """The renderer must be reached from `render_dashboard_perf`, not only
    callable on its own."""
    load_script()
    import _cctally_dashboard_perf as dp
    rows = [
        _pass(1, cpu_ns=1_000_000_000, period_ns=None,
              duration_ns=2_000_000_000),
        _pass(2, cpu_ns=1_000_000_000, period_ns=4_000_000_000,
              duration_ns=2_000_000_000),
    ]
    text = dp.render_dashboard_perf(_conversation_payload(rows))
    assert "Conversation sync loop" in text
    assert "25.0% of one core" in text


def test_an_old_server_without_the_key_still_renders():
    """A `dashboard-perf` binary newer than the running dashboard must not
    crash on a payload whose tick block predates the second ring."""
    load_script()
    import _cctally_dashboard_perf as dp
    payload = _conversation_payload([])
    del payload["tick"]["conversation_sync"]
    text = dp.render_dashboard_perf(payload)
    assert "Conversation sync loop" in text
    assert "no samples yet" in text
