"""#901 spec §5.5 / §5.7 / G7: the `writeIo` object, its golden, and the
`dashboard-perf` rows in the approved wording."""
import argparse
import json
import pathlib
import sys

import pytest
from zoneinfo import ZoneInfo

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_write_io as wio  # noqa: E402

S = 1_000_000_000
KiB = 1024
MiB = 1024 * KiB
GOLDEN = (pathlib.Path(__file__).resolve().parent / "fixtures"
          / "dashboard-perf" / "write-io.golden.json")
WALL = 1_791_030_600            # 2026-10-03T12:30:00Z


def scenario():
    """Deterministic writer: 100 KiB/s of steady writes, one 5 MiB deletion
    operation of 1,000 rows in [1200 s, 1205 s], a publication every 5 s,
    one maintenance ledger bucket and a registered local instance."""
    clock = {"ns": 1000 * S}

    def counter_value(_pid=None):
        t = clock["ns"] / S
        extra = 5 * MiB * min(max((t - 1200) / 5, 0.0), 1.0)
        return int(100 * KiB * (t - 1000) + extra)

    wio.reset_for_tests(
        counter=wio.ProcessWriteCounter(platform="darwin",
                                        darwin_reader=counter_value),
        clock_ns=lambda: clock["ns"], wall_s=lambda: WALL)
    wio.register_local_instance(instance_id="0123456789abcdef",
                                host="127.0.0.1", port=8789, pid=4242)
    wio.set_maintenance_record({"ledger": [
        {"hour": "2026-10-03T03:00:00Z", "charged": 250 * MiB,
         "largest": 250 * MiB}]})
    wio.note_publication(cold=True)
    while clock["ns"] < 1310 * S:
        clock["ns"] += 5 * S
        if clock["ns"] == 1200 * S:
            interval = wio.begin_interval("maintenance")
            clock["ns"] += 5 * S
            wio.mark_deletion(wio.end_interval(interval), rows=1000)
        wio.note_publication(cold=False)
    return clock


@pytest.fixture(autouse=True)
def _reset():
    wio.reset_for_tests()
    yield
    wio.reset_for_tests()


def test_the_write_io_object_matches_its_golden():
    scenario()
    payload = wio.write_io_payload()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert rendered == GOLDEN.read_text(encoding="utf-8")
    assert list(payload) == [
        "status", "reason", "source", "scope", "instanceId", "sampledAt",
        "windowSeconds", "bytesWritten", "bytesPerMinute", "tickCount",
        "meanBytesPerTick", "excludedDeletions", "coldStartup",
        "maintenance", "reclaimRefusal", "budget"]


def test_a_reclaim_refusal_is_published_in_memory_and_cleared():
    """#901 Q9: a refused reclaim attempt writes nothing durable; its
    in-memory pass record is `writeIo.reclaimRefusal` until a chunk runs."""
    import datetime as dt

    scenario()
    at = dt.datetime(2026, 10, 3, 12, 31, tzinfo=dt.timezone.utc)
    wio.set_reclaim_refusal("wal_checksum_mismatch", at)
    assert wio.write_io_payload()["reclaimRefusal"] == {
        "reason": "wal_checksum_mismatch", "at": "2026-10-03T12:31:00Z"}
    wio.set_reclaim_refusal("Free text from somewhere", at)
    assert wio.write_io_payload()["reclaimRefusal"]["reason"] == "unknown"
    wio.clear_reclaim_refusal()
    assert wio.write_io_payload()["reclaimRefusal"] is None


def test_the_report_rows_use_the_approved_wording():
    import _cctally_dashboard_perf as perf

    scenario()
    rows = perf.render_write_io_rows(
        wio.write_io_payload(),
        {"process_write_bytes": int(0.9 * MiB), "write_status": "ok"},
        display_tz=ZoneInfo("Etc/UTC"))
    assert rows == [
        "Disk writes (process)  5.8 MiB/min over 5m 00s",
        "Per publication  0.5 MiB (mean, 59 publications)",
        "Newest tick  0.9 MiB",
        "Write budget  ok (limits 16 MiB/min, 8 MiB per publication)",
        "Excluded deletions  1 conversation, 1,000 rows, 5.5 MiB "
        "(5.6 KiB/row) at 12:30",
        "Maintenance charged  250 MiB over 24 h 30 m (since 12:00 UTC "
        "yesterday); allowance 5.99 GiB incl. largest 250 MiB",
    ]


@pytest.mark.parametrize("zone, clock", [
    ("Asia/Kolkata", "18:00"), ("America/Los_Angeles", "05:30"),
    ("Etc/UTC", "12:30")])
def test_the_deletion_time_is_rendered_in_the_display_zone(zone, clock):
    """#901 Amendment 19 PR-12: "at HH:MM" goes through ``format_display_dt``
    in the display zone, not a hard-coded UTC. The maintenance window's
    "since 12:00 UTC" names its zone and stays UTC, as §5.7 states it."""
    import _cctally_dashboard_perf as perf

    scenario()
    rows = perf.render_write_io_rows(
        wio.write_io_payload(), None, display_tz=ZoneInfo(zone))
    [excluded] = [row for row in rows if row.startswith("Excluded deletions")]
    assert excluded.endswith(f"(5.6 KiB/row) at {clock}"), excluded
    [maintenance] = [row for row in rows
                     if row.startswith("Maintenance charged")]
    assert "(since 12:00 UTC yesterday)" in maintenance


def test_the_cli_renders_the_deletion_time_in_the_configured_zone(
    monkeypatch, tmp_path, capsys,
):
    """End to end: ``cctally dashboard-perf`` resolves ``display.tz`` from the
    config and the report's deletion time follows it."""
    from conftest import load_script
    from test_dashboard_perf_cli import _args, _boot
    from tests._support_http import stop

    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        ns["CONFIG_PATH"].write_text(
            json.dumps({"display": {"tz": "Asia/Kolkata"}}))
        scenario()
        rc = ns["cmd_dashboard_perf"](_args(port=srv.server_address[1]))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "(5.6 KiB/row) at 18:00\n" in out, out
        assert "(since 12:00 UTC yesterday)" in out, out
    finally:
        stop(srv, srv._test_thread)


def test_an_unavailable_counter_and_a_short_window_say_so():
    import _cctally_dashboard_perf as perf

    wio.reset_for_tests(counter=wio.ProcessWriteCounter(platform="win32"))
    rows = perf.render_write_io_rows(wio.write_io_payload(), None)
    assert rows[0] == "Disk writes  unavailable (unsupported platform)"
    assert rows[2] == "Newest tick  no samples yet"
    assert rows[-1] == "Maintenance charged  no ledger yet"
    _install_short = wio.ProcessWriteCounter(platform="darwin",
                                             darwin_reader=lambda _p: 4096)
    wio.reset_for_tests(counter=_install_short)
    wio.note_publication(cold=False)
    rows = perf.render_write_io_rows(wio.write_io_payload(), None)
    assert rows[0] == "Disk writes  no samples yet (needs 5 minutes)"
    assert rows[1] == "Per publication  no samples yet (needs 10 publications)"


@pytest.mark.parametrize("verdict, reason, row", [
    ("insufficient", "warming_up",
     "Disk writes  no samples yet (needs 5 minutes)"),
    ("insufficient", "insufficient_samples",
     "Disk writes  not enough samples in the last 5 minutes"),
    ("insufficient", "insufficient_coverage",
     "Disk writes  the last 5 minutes were not fully sampled"),
    ("insufficient", "invalid_samples",
     "Disk writes  a counter reading in the last 5 minutes failed"),
    ("unavailable", "counter_reset",
     "Disk writes  the write counter went backwards; measuring again"),
])
def test_a_window_without_a_rate_names_its_reason(verdict, reason, row):
    """PR-8: only a warming dashboard reads "no samples yet"."""
    import _cctally_dashboard_perf as perf

    write_io = {"status": "ok", "reason": None, "bytesPerMinute": None,
                "meanBytesPerTick": None, "tickCount": 0,
                "budget": {"verdict": verdict, "reasons": [reason],
                           "bytesPerMinuteLimit": 16 * 1024 * 1024,
                           "meanBytesPerTickLimit": 8 * 1024 * 1024}}
    assert perf.render_write_io_rows(write_io, None)[0] == row


def test_an_older_dashboard_without_write_io_renders_no_rows():
    import _cctally_dashboard_perf as perf

    assert perf.render_write_io_rows(None) == []
    report = perf.render_dashboard_perf({"tick": {}, "tracing": {}})
    assert "Disk writes" not in report


def test_dashboard_perf_json_passes_write_io_through_under_schema_one(
        monkeypatch, capsys):
    from conftest import load_script

    ns = load_script()
    perf = ns["_cctally_dashboard_perf"]
    scenario()
    payload = {"tick": {"records": []}, "writeIo": wio.write_io_payload()}
    monkeypatch.setattr(perf, "_request", lambda *a, **k: payload)
    args = argparse.Namespace(host="127.0.0.1", port=8789, token=None,
                              trace=None, json=True)
    assert perf.cmd_dashboard_perf(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out)[0] == "schemaVersion" and out["schemaVersion"] == 1
    assert json.dumps(out["diagnostic"]["writeIo"], indent=2,
                      sort_keys=True) + "\n" == GOLDEN.read_text(encoding="utf-8")


def test_the_human_report_carries_the_rows():
    import _cctally_dashboard_perf as perf

    scenario()
    report = perf.render_dashboard_perf({
        "tick": {"records": [{"process_write_bytes": MiB,
                              "write_status": "ok"}]},
        "tracing": {}, "writeIo": wio.write_io_payload()})
    assert "Disk writes (process)  5.8 MiB/min over 5m 00s" in report
    assert "Newest tick  1 MiB" in report
