"""#901 §5.5: process-write fields on every always-on record, and the
65,536-byte state budget with the write state included."""
import dataclasses
import pathlib
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_tick_stats as ts  # noqa: E402
import _lib_write_io as wio  # noqa: E402

S = 1_000_000_000


class _Counter:
    """Darwin-shaped counter: each read returns the next queued value."""

    def __init__(self, *values):
        self.values = list(values)
        self.last = 0

    def __call__(self, _pid=None):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


@pytest.fixture(autouse=True)
def _reset():
    ts.reset_for_tests()
    wio.reset_for_tests()
    yield
    ts.reset_for_tests()
    wio.reset_for_tests()


def _counter(*values, platform="darwin"):
    wio.reset_for_tests(counter=wio.ProcessWriteCounter(
        platform=platform, darwin_reader=_Counter(*values)))


def test_a_tick_record_carries_its_interval_write_delta():
    _counter(1000, 5000, 5000)
    tick = ts.begin_tick()
    tick.set_dispatch("full")
    tick.finish(published_ns=1, published_at="2026-10-03T00:00:00Z")
    record = ts.snapshot().records[-1]
    assert record.process_write_bytes == 4000
    assert record.write_status == "ok"
    assert record.write_overlap is False
    assert set(record.as_wire()) >= {
        "process_write_bytes", "write_status", "write_overlap"}


def test_an_unsupported_platform_records_null_never_zero():
    wio.reset_for_tests(counter=wio.ProcessWriteCounter(platform="win32"))
    tick = ts.begin_tick()
    tick.finish(published_ns=1, published_at="2026-10-03T00:00:00Z")
    record = ts.snapshot().records[-1]
    assert record.process_write_bytes is None
    assert record.write_status == "unsupported_platform"


def test_a_refresh_tick_counts_a_publication_and_a_standalone_does_not():
    _counter(*range(0, 10_000, 10))
    standalone = ts.begin_tick(standalone=True)
    standalone.finish(published_ns=1, published_at="2026-10-03T00:00:00Z")
    assert wio.state_for_budget()[2][-1] == 0
    tick = ts.begin_tick()
    tick.finish(published_ns=2, published_at="2026-10-03T00:00:01Z")
    assert wio.state_for_budget()[2][-1] == 1


def test_the_conversation_record_validates_its_write_fields():
    ts.record_conversation_pass(
        seq=1, started_ns=0, ended_ns=1, duration_ns=1, cpu_ns=1,
        status="ok", process_write_bytes=8192, write_status="ok",
        write_overlap=True)
    ts.record_conversation_pass(
        seq=2, started_ns=2, ended_ns=3, duration_ns=1, cpu_ns=1,
        status="ok", process_write_bytes=8192, write_status="counter_error")
    ts.record_conversation_pass(
        seq=3, started_ns=4, ended_ns=5, duration_ns=1, cpu_ns=1,
        status="ok", process_write_bytes=-5, write_status="ok")
    ts.record_conversation_pass(
        seq=4, started_ns=6, ended_ns=7, duration_ns=1, cpu_ns=1,
        status="ok", write_status="/private/leak")
    rows = ts.snapshot().conversation_records
    assert (rows[0].process_write_bytes, rows[0].write_status,
            rows[0].write_overlap) == (8192, "ok", True)
    assert (rows[1].process_write_bytes, rows[1].write_status) == (
        None, "counter_error")
    assert (rows[2].process_write_bytes, rows[2].write_status) == (
        None, "not_sampled")
    assert rows[3].write_status == "not_sampled"
    assert rows[0].write_status in ts.WRITE_STATUSES


def test_an_operation_record_keeps_its_charge_and_a_negative_balance():
    ts.record_maintenance_phase("delete", {
        "op_id": 7, "started_at": "2026-10-03T03:12:00Z", "duration_s": 1.5,
        "rows": 27_395, "pages_reclaimed": 0, "charged_bytes": 336_773_120,
        "process_write_bytes": None, "write_status": "unsupported_platform",
        "write_overlap": True, "balance_before_bytes": 4_194_304,
        "balance_after_bytes": -332_578_816, "outcome": "ok",
        "mode": "spill_free", "page_count": 4_532_015, "usable_size": 4096,
        "page_size": 4096, "pointer_map_cap": 5_529,
        "reservation_version": 2})
    record = ts.snapshot().maintenance_records[-1]
    assert (record.page_count, record.usable_size, record.page_size,
            record.pointer_map_cap, record.reservation_version) == (
        4_532_015, 4096, 4096, 5_529, 2), "Q8: the reservation inputs"
    assert record.phase == "delete"
    assert record.op_id == 7
    assert record.started_at == "2026-10-03T03:12:00Z"
    assert record.duration_ns == 1_500_000_000
    assert record.rows == 27_395
    assert record.charged_bytes == 336_773_120
    assert record.process_write_bytes is None
    assert record.write_status == "unsupported_platform"
    assert record.write_overlap is True
    assert record.balance_before_bytes == 4_194_304
    assert record.balance_after_bytes == -332_578_816
    assert (record.outcome, record.mode, record.pending) == (
        "ok", "spill_free", False)


def test_an_operation_record_coerces_unknown_enums():
    ts.record_maintenance_phase("reclaim", {
        "op_id": 1, "outcome": "exploded", "mode": "/tmp/x",
        "write_status": "weird", "process_write_bytes": 10})
    record = ts.snapshot().maintenance_records[-1]
    assert record.outcome == "ok"
    assert record.mode == ""
    assert (record.write_status, record.process_write_bytes) == (
        "not_sampled", None)
    assert record.pending is True, "a reclaim chunk leaves its episode open"


def test_a_traced_phase_records_its_write_bytes():
    import _lib_perf as perf

    _counter(100, 900)
    previous = perf.enabled()
    perf.set_enabled(True)
    perf.reset_thread()
    try:
        with perf.phase("root"):
            pass
        tree = perf.current_root().to_dict()
    finally:
        perf.set_enabled(previous)
        perf.reset_thread()
    assert tree["write_bytes"] == 800


def test_every_ring_and_the_write_state_stay_inside_the_frozen_budget():
    """Non-vacuity: every ring FULL with distinct worst-case values, a full
    timeline, a full exclusion ring, a 25-bucket ledger and a local
    instance. The cap stays frozen at 65,536 bytes."""
    _counter(*range(10 ** 9, 10 ** 9 + 4096 * 3000, 4096))
    clock = {"ns": 1000 * S}
    wio.reset_for_tests(
        counter=wio.ProcessWriteCounter(
            platform="darwin",
            darwin_reader=_Counter(*range(10 ** 9, 10 ** 9 + 4096 * 3000, 4096))),
        clock_ns=lambda: clock["ns"])
    longest = "z" * ts.PUBLISHED_AT_MAX_CHARS
    for i in range(ts.RING_CAPACITY * 2):
        tick = ts.begin_tick()
        tick.set_dispatch("full")
        tick.set_codex_regime("active")
        tick.finish(published_ns=i,
                    published_at=f"{i:04d}{longest}"[:len(longest)])
        ts.record_conversation_pass(
            seq=i, started_ns=i * 10, ended_ns=i * 10 + 5, duration_ns=5,
            cpu_ns=1, status="ok", process_write_bytes=10 ** 9 + i,
            write_status="ok", write_overlap=bool(i % 2))
        ts.record_maintenance_phase("delete", {
            "op_id": 10 ** 6 + i,
            "started_at": f"2026-10-{(i % 28) + 1:02d}T00:00:{i % 60:02d}Z",
            "duration_s": 1.0 + i, "rows": 10 ** 5 + i, "pages_reclaimed": 0,
            "charged_bytes": 10 ** 9 + i, "process_write_bytes": 10 ** 9 + i,
            "write_status": "ok", "write_overlap": True,
            "balance_before_bytes": 4 * 1024 * 1024 + i,
            "balance_after_bytes": -(10 ** 9) - i, "outcome": "ok",
            "mode": "spill_free", "page_count": 10 ** 9 + i,
            "usable_size": 65_536 - i, "page_size": 65_536,
            "pointer_map_cap": 10 ** 6 + i, "reservation_version": 2,
            "skip_reason": "wal_endpoint_mismatch", "planner_version": 1,
            "sqlite_source_id": "2026-07-17 12:00:00 " + "f" * 64 + " x" * 6,
            "freelist_count": 10 ** 9 + i, "steps": 16, "kind_free": 4,
            "kind_overflow": 4, "kind_leaf": 4, "kind_interior": 4,
            "identified_pages": 10 ** 5 + i, "unidentified_pages": 10 ** 5 + i,
            "fixed_bytes": 65_536 + i, "plan_digest": f"{i:016x}",
            "reader_status": "read_budget_exhausted",
            "inspected_bytes": 10 ** 9 + i, "inspection_ms": 10 ** 4 + i,
            "freelist_reduction": 10 ** 4 + i,
            "page_count_reduction": 10 ** 4 + i})
        obs = wio.end_interval(wio.begin_interval("maintenance"))
        wio.mark_deletion(obs, rows=10 ** 5 + i)
        clock["ns"] += 5 * S
    wio.set_maintenance_record({"ledger": [
        {"hour": f"2026-10-0{1 + h // 24}T{h % 24:02d}:00:00Z",
         "charged": 10 ** 9 + h, "largest": 10 ** 8 + h} for h in range(25)]})
    wio.register_local_instance(instance_id="abcdefabcdefabcd",
                                host="127.0.0.1", port=8789)
    snap = ts.snapshot()
    assert len(snap.records) == ts.RING_CAPACITY
    assert len(snap.conversation_records) == ts.RING_CAPACITY
    assert len(snap.maintenance_records) == ts.MAINTENANCE_RING_CAPACITY
    write_state = wio.state_for_budget()
    assert len(write_state[0]) == wio.TIMELINE_CAPACITY
    assert len(write_state[4]) == wio.EXCLUSION_CAPACITY
    assert len(write_state[3]) == 3 * 25
    assert ts.MEMORY_BUDGET_BYTES == 65536, (
        "the budget is frozen; raising it is a separate reviewed decision")
    total = ts._deep_size(snap) + ts._deep_size(write_state)
    assert total <= ts.MEMORY_BUDGET_BYTES, total


def test_a_reclaim_record_carries_its_plan_and_coerces_free_text():
    """Q9 (§5.5): a chunk's plan fields ride the record; a skip reason or
    reader status outside the closed set, or a digest that is not hex,
    never reaches the published state."""
    ts.record_maintenance_phase("reclaim", {
        "op_id": 3, "outcome": "ok", "mode": "reclaim", "steps": 16,
        "kind_free": 9, "kind_overflow": 2, "kind_leaf": 4, "kind_interior": 1,
        "identified_pages": 40, "unidentified_pages": 70, "fixed_bytes": 65536,
        "plan_digest": "0123456789abcdef", "reader_status": "ok",
        "inspected_bytes": 9_000_000, "inspection_ms": 41,
        "freelist_reduction": 16, "page_count_reduction": 17,
        "sqlite_source_id": "2026-07-01 abc", "planner_version": 1})
    record = ts.snapshot().maintenance_records[-1]
    assert (record.steps, record.identified_pages, record.unidentified_pages,
            record.freelist_reduction, record.page_count_reduction,
            record.reader_status, record.plan_digest) == (
        16, 40, 70, 16, 17, "ok", "0123456789abcdef")
    ts.record_maintenance_phase("reclaim", {
        "op_id": 4, "outcome": "skipped", "skip_reason": "/etc/passwd",
        "reader_status": "it broke", "plan_digest": "../../x"})
    record = ts.snapshot().maintenance_records[-1]
    assert (record.skip_reason, record.reader_status, record.plan_digest) == (
        "unknown", "unknown", None)


def test_the_skip_reasons_mirror_the_retention_module():
    import _lib_conversation_retention as retention
    assert set(ts.MAINTENANCE_SKIP_REASONS) == set(
        retention.RECLAIM_SKIP_REASONS)
