"""#901 spec §5.6 / §5.7 / G6: the doctor check `performance.dashboard_disk_
writes`, its wording, and instance discovery. Real-HTTP cases (two live
dashboards, a bearer-protected one) are in `tests/test_dashboard_write_io_
endpoint.py`, which needs the dashboard route."""
import dataclasses
import datetime as dt
import importlib
import os
import pathlib
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

doctor = importlib.import_module("_lib_doctor")

UTC = dt.timezone.utc
MiB = 1024 * 1024
NOW = dt.datetime(2026, 10, 3, 12, 30, tzinfo=UTC)
REMEDIATION = ("Run `cctally dashboard-perf` for the breakdown, and report "
               "it if it persists.")


def _state(**kw):
    fields = {
        field.name: (
            field.default if field.default is not dataclasses.MISSING else None)
        for field in dataclasses.fields(doctor.DoctorState)
    }
    fields["now_utc"] = NOW
    fields.update(kw)
    return doctor.DoctorState(**fields)


def _write_io(verdict="ok", *, status="ok", reason=None,
              rate=int(3.2 * MiB), mean=MiB, reasons=None,
              instance="0123456789abcdef"):
    return {
        "status": status, "reason": reason, "instanceId": instance,
        "bytesPerMinute": rate if status == "ok" else None,
        "meanBytesPerTick": mean if status == "ok" else None,
        "budget": {"policyVersion": 1, "verdict": verdict,
                   "reasons": reasons or ["within_limits"],
                   "bytesPerMinuteLimit": 16 * MiB,
                   "meanBytesPerTickLimit": 8 * MiB},
    }


def _instance(write_io=None, *, port=8789, probe="ok", reason=None,
              instance="0123456789abcdef"):
    return {"instance_id": instance, "port": port, "pid": 4242,
            "probe": probe, "reason": reason, "write_io": write_io}


def _check(instances=(), *, mode="discovery", record=None):
    return doctor._check_performance_dashboard_disk_writes(_state(
        dashboard_disk_writes={"mode": mode, "instances": list(instances)},
        conversations_reclaim_pending=record))


def test_no_dashboard_running_is_ok_and_not_applicable():
    result = _check()
    assert (result.id, result.title) == (
        "performance.dashboard_disk_writes", "Dashboard disk writes")
    assert (result.severity, result.summary) == ("ok", "No dashboard is running.")
    assert result.remediation is None
    assert result.details["instances"] == []
    assert result.details["policy_version"] == 1
    assert result.details["maintenance"] == {
        "charged_bytes": 0, "largest_charge_bytes": 0,
        "window_start": "2026-10-02T12:00:00Z",
        "window_end": "2026-10-03T12:30:00Z", "window_minutes": 1470,
        "allowance_bytes": 4 * MiB * 1471, "verdict": "ok"}


def test_within_budget_names_the_rate():
    result = _check([_instance(_write_io())])
    assert (result.severity, result.summary) == (
        "ok", "Within budget: 3.2 MiB/min over the last 5 minutes.")


def test_one_dashboard_over_its_rate_budget_warns():
    result = _check([_instance(_write_io(
        "over", rate=412 * MiB, reasons=["rate_over_limit"]))])
    assert result.severity == "warn"
    assert result.summary == (
        "The running dashboard (port 8789) is writing 412 MiB/min, over its "
        "16 MiB/min budget.")
    assert result.remediation == REMEDIATION


def test_one_dashboard_over_its_per_publication_budget_warns():
    result = _check([_instance(_write_io(
        "over", rate=2 * MiB, mean=12 * MiB,
        reasons=["publication_over_limit"]))])
    assert result.severity == "warn"
    assert result.summary == (
        "The running dashboard (port 8789) is writing 12 MiB per "
        "publication, over its 8 MiB per publication budget.")


def test_a_warming_dashboard_is_ok_with_an_insufficient_detail():
    result = _check([_instance(_write_io(
        "insufficient", reasons=["warming_up"], rate=None, mean=None))])
    assert (result.severity, result.summary) == (
        "ok", "Disk writes: no samples yet (needs 5 minutes).")
    assert result.details["instances"][0]["verdict"] == "insufficient"
    assert result.details["instances"][0]["reason"] == "warming_up"


@pytest.mark.parametrize("verdict, reason, summary", [
    ("insufficient", "insufficient_samples",
     "Disk writes: not enough samples in the last 5 minutes."),
    ("insufficient", "insufficient_coverage",
     "Disk writes: the last 5 minutes were not fully sampled."),
    ("insufficient", "invalid_samples",
     "Disk writes: a counter reading in the last 5 minutes failed."),
    ("unavailable", "counter_reset",
     "Disk writes: the write counter went backwards; measuring again."),
    ("insufficient", "excluded_time_over_limit",
     "Disk writes: transcript maintenance filled over 20% of the last "
     "5 minutes."),
])
def test_a_statistic_without_a_verdict_names_its_reason(
        verdict, reason, summary):
    """PR-8. Every reason without a verdict read "no samples yet (needs 5
    minutes)", so a dashboard running for hours that failed coverage, saw its
    counter reset or spent over 20% of the window in excluded maintenance
    read as warming up. Only `warming_up` keeps that sentence."""
    result = _check([_instance(_write_io(
        verdict, reasons=[reason], rate=None, mean=None))])
    assert (result.severity, result.summary) == ("ok", summary)
    assert result.details["instances"][0]["reason"] == reason


@pytest.mark.parametrize("reason, summary", [
    ("authentication_required",
     "Disk writes: the dashboard on port 8789 needs its access token, so it "
     "is not measured from here."),
    ("not_loopback_reachable",
     "Disk writes: the dashboard on port 8789 is not reachable over "
     "loopback, so it is not measured from here."),
])
def test_a_dashboard_doctor_cannot_measure_from_here_is_ok(reason, summary):
    """PR-9. Doctor sends no token and probes over loopback only, so a
    token-mode dashboard and one bound to a specific non-loopback address can
    never answer it: a WARN for them never cleared. They are an OK note."""
    result = _check([_instance(probe="failed", reason=reason)])
    assert (result.severity, result.summary) == ("ok", summary)
    assert result.remediation is None
    assert result.details["instances"][0]["reason"] == reason


def test_a_counterless_platform_is_ok_with_an_unavailable_detail():
    result = _check([_instance(_write_io(
        "unavailable", status="unavailable", reason="unsupported_platform"))])
    assert (result.severity, result.summary) == (
        "ok", "Disk writes: unavailable (unsupported platform).")


@pytest.mark.parametrize("entry, words", [
    (_instance(_write_io("unavailable", status="unavailable",
                         reason="counter_error")), "counter error"),
    (_instance(probe="failed", reason="identity_mismatch"), "identity mismatch"),
])
def test_a_live_instance_without_valid_telemetry_warns(entry, words):
    result = _check([entry])
    assert result.severity == "warn"
    assert result.summary == (
        f"The running dashboard (port 8789) cannot report its disk writes "
        f"({words}).")


def test_two_instances_are_evaluated_separately():
    result = _check([
        _instance(_write_io(), port=8789),
        _instance(_write_io("over", rate=40 * MiB, reasons=["rate_over_limit"],
                            instance="fedcba9876543210"),
                  port=8790, instance="fedcba9876543210"),
    ])
    assert result.severity == "warn"
    assert result.summary.startswith("The running dashboard (port 8790)")
    assert [row["port"] for row in result.details["instances"]] == [8789, 8790]
    assert [row["verdict"] for row in result.details["instances"]] == [
        "ok", "over"]


def _ledger(buckets, charged, largest):
    start = dt.datetime(2026, 10, 2, 13, tzinfo=UTC)
    return [{"hour": (start + dt.timedelta(hours=h)).strftime(
        "%Y-%m-%dT%H:00:00Z"), "charged": charged, "largest": largest}
        for h in range(buckets)]


def test_maintenance_over_its_allowance_warns_with_no_dashboard():
    record = {"policy_version": 1, "ledger": _ledger(24, 400 * MiB, 400 * MiB)}
    result = _check(record=record)
    assert result.severity == "warn"
    assert result.summary == (
        "Transcript maintenance was charged 9.38 GiB over the last 24 h 30 m, "
        "over its 6.14 GiB allowance.")
    assert result.details["maintenance"]["verdict"] == "over"


def test_the_check_never_fails():
    record = {"policy_version": 1, "ledger": _ledger(24, 400 * MiB, 400 * MiB)}
    result = _check([
        _instance(_write_io("over", rate=99 * MiB, reasons=["rate_over_limit"])),
        _instance(probe="failed", reason="timeout", port=9000,
                  instance="aaaaaaaaaaaaaaaa"),
    ], record=record)
    assert result.severity == "warn"
    assert result.summary.count("The running dashboard") == 2
    assert result.summary.endswith("allowance.")


def test_a_failed_gather_is_ok_and_says_so():
    result = doctor._check_performance_dashboard_disk_writes(_state(
        dashboard_disk_writes={"mode": "error", "instances": []}))
    assert (result.severity, result.summary) == (
        "ok", "Disk writes: unavailable (gather failed).")


def test_the_check_is_the_last_category_and_joins_the_fingerprint():
    assert doctor._CATEGORY_DEFINITIONS[-1] == (
        "performance", "Performance",
        (("performance.dashboard_disk_writes",
          "_check_performance_dashboard_disk_writes"),))
    report = doctor.run_checks(_state(
        dashboard_disk_writes={"mode": "discovery", "instances": []}))
    assert doctor._identity_slice(report)["checks"][-1] == [
        "performance.dashboard_disk_writes", "ok"]


# ── discovery (the CLI / TUI gather) ──────────────────────────────────────

def _gather_env(tmp_path, monkeypatch):
    from conftest import load_script, redirect_paths

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    gather = ns["_load_sibling"]("_cctally_doctor")
    wio = importlib.import_module("_lib_write_io")
    wio.reset_for_tests()
    import _cctally_core
    return ns, gather, wio, _cctally_core.APP_DIR


def test_the_in_process_chip_reads_its_own_snapshot_without_http(
        tmp_path, monkeypatch):
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    wio.register_local_instance(instance_id="0123456789abcdef",
                                host="127.0.0.1", port=8789)
    wio.write_instance_descriptor(app_dir, instance_id="0123456789abcdef",
                                  host="127.0.0.1", port=8789)

    def no_http(*_a, **_k):
        raise AssertionError("the in-process path must not self-HTTP")

    result = gather._gather_dashboard_disk_writes(fetch=no_http)
    assert result["mode"] == "in_process"
    [entry] = result["instances"]
    assert entry["probe"] == "ok"
    assert entry["write_io"]["instanceId"] == "0123456789abcdef"
    wio.clear_local_instance()


def test_discovery_probes_live_instances_and_ignores_stale_ones(
        tmp_path, monkeypatch):
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    wio.write_instance_descriptor(app_dir, instance_id="bbbbbbbbbbbbbbbb",
                                  host="127.0.0.1", port=8790)
    wio.write_instance_descriptor(app_dir, instance_id="aaaaaaaaaaaaaaaa",
                                  host="0.0.0.0", port=8789)
    wio.write_instance_descriptor(app_dir, instance_id="cccccccccccccccc",
                                  host="127.0.0.1", port=8791,
                                  pid=2 ** 22 + 7)
    probed = []

    def fetch(descriptor, *, timeout_s):
        probed.append((descriptor["instanceId"], timeout_s))
        return {"instance_id": descriptor["instanceId"],
                "port": descriptor["port"], "pid": descriptor["pid"],
                "probe": "ok", "reason": None,
                "write_io": {"instanceId": descriptor["instanceId"]}}

    result = gather._gather_dashboard_disk_writes(fetch=fetch)
    assert result["mode"] == "discovery"
    assert [e["port"] for e in result["instances"]] == [8789, 8790]
    assert sorted(i for i, _ in probed) == ["aaaaaaaaaaaaaaaa",
                                            "bbbbbbbbbbbbbbbb"]
    assert all(t == gather.DASHBOARD_WRITE_IO_TIMEOUT_SECONDS for _, t in probed)
    assert len(wio.read_instance_descriptors(app_dir)) == 3, (
        "doctor is read-only: a stale descriptor is ignored, not deleted")


@pytest.mark.parametrize("behaviour, reason", [
    ("401", "authentication_required"), ("mismatch", "identity_mismatch"),
    ("garbage", "malformed_response")])
def test_the_probe_preserves_authentication_and_verifies_identity(
        tmp_path, monkeypatch, behaviour, reason):
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    perf = ns["_cctally_dashboard_perf"]
    calls = []

    def request(host, port, path, *, token, body=None, timeout=5.0):
        calls.append((host, port, path, token, timeout))
        if behaviour == "401":
            raise perf.DashboardPerfError("x", reason="authentication_required")
        if behaviour == "mismatch":
            return {"schemaVersion": 1,
                    "writeIo": {"instanceId": "ffffffffffffffff"}}
        return ["not", "an", "object"]

    monkeypatch.setattr(perf, "_request", request)
    descriptor = {"instanceId": "0123456789abcdef", "pid": os.getpid(),
                  "port": 8789, "host": "127.0.0.1"}
    # timing-budget: `_request` is replaced above, so nothing waits; 2.0 is the value the recorded call must carry
    entry = gather._fetch_dashboard_write_io(descriptor, timeout_s=2.0)
    assert (entry["probe"], entry["reason"]) == ("failed", reason)
    assert calls == [("127.0.0.1", 8789, "/api/debug/backend/write-io",
                      None, 2.0)], "no token: no authentication bypass"


def test_a_lan_only_bind_is_not_reachable_over_loopback(tmp_path, monkeypatch):
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    descriptor = {"instanceId": "0123456789abcdef", "pid": os.getpid(),
                  "port": 8789, "host": "192.168.1.5"}
    # timing-budget: a non-loopback host is refused before any request is made, so nothing waits
    entry = gather._fetch_dashboard_write_io(descriptor, timeout_s=2.0)
    assert (entry["probe"], entry["reason"]) == (
        "failed", "not_loopback_reachable")


def test_the_full_gather_reports_no_dashboard_on_a_fresh_install(
        tmp_path, monkeypatch):
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    state = ns["doctor_gather_state"](discover_dashboards=True)
    assert state.dashboard_disk_writes == {"mode": "discovery", "instances": []}
    result = doctor._check_performance_dashboard_disk_writes(state)
    assert (result.severity, result.summary) == ("ok", "No dashboard is running.")


def test_only_the_cli_gather_probes_other_dashboards(tmp_path, monkeypatch):
    """PR-11. Every process without a local instance (the TUI, a standalone
    snapshot build) probed every live dashboard descriptor serially, 2 s each,
    on every doctor gather. Only the `cctally doctor` CLI discovers now; the
    TUI's gather and any other builder take the discovery-off path."""
    ns, gather, wio, app_dir = _gather_env(tmp_path, monkeypatch)
    wio.write_instance_descriptor(app_dir, instance_id="0123456789abcdef",
                                  host="127.0.0.1", port=8789)
    probed = []

    def fetch(descriptor, *, timeout_s):
        probed.append(descriptor["instanceId"])
        return {"instance_id": descriptor["instanceId"],
                "port": descriptor["port"], "pid": descriptor["pid"],
                "probe": "failed", "reason": "unreachable", "write_io": None}

    monkeypatch.setattr(gather, "_fetch_dashboard_write_io", fetch)
    state = ns["doctor_gather_state"]()
    assert probed == [], "a non-CLI doctor gather probed a dashboard"
    assert state.dashboard_disk_writes == {"mode": "not_probed",
                                           "instances": []}
    result = doctor._check_performance_dashboard_disk_writes(state)
    assert result.severity == "ok"
    assert result.summary == (
        "Disk writes: not measured here; run `cctally doctor` to check "
        "running dashboards.")

    tui = ns["_load_sibling"]("_cctally_tui")
    importlib.import_module("_lib_snapshot_cache")._DOCTOR_MEMO.clear()
    payload = tui._tui_precompute_doctor_payload(NOW, None)
    assert "_error" not in payload, payload
    assert probed == [], "the TUI's doctor gather probed a dashboard"

    calls = []

    def cli_gather(**kwargs):
        calls.append(kwargs)
        return real_gather(**kwargs)

    real_gather = ns["doctor_gather_state"]
    monkeypatch.setattr(sys.modules["cctally"], "doctor_gather_state",
                        cli_gather)
    import argparse
    assert gather.cmd_doctor(argparse.Namespace(
        json=True, quiet=False, verbose=False)) in (0, 2)
    assert calls and calls[0].get("discover_dashboards") is True, calls
    assert probed == ["0123456789abcdef"], "the CLI probes live dashboards"
