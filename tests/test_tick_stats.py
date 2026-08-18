"""Unit gates for `bin/_lib_tick_stats.py` in isolation (#583 S1, spec §1.1).

The module is a leaf: no cctally import, no database, no clock it does not own.
Everything here therefore runs without a corpus. The integration gates that
drive a real tick live in `tests/test_tick_stats_integration.py`.
"""
import pathlib
import sys
import threading

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def test_the_ring_is_bounded_and_keeps_the_newest():
    """Overfill by 10x: exactly RING_CAPACITY records, newest retained."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    writes = ts.RING_CAPACITY * 10
    for i in range(writes):
        t = ts.begin_tick()
        t.set_dispatch("full")
        t.set_codex_regime("idle")
        t.finish(published_ns=i * 1_000_000_000, published_at=f"t{i}")
    snap = ts.snapshot()
    assert len(snap.records) == ts.RING_CAPACITY
    assert snap.tick_seq == writes
    assert snap.records[-1].published_at == f"t{writes - 1}"
    assert snap.records[0].published_at == f"t{writes - ts.RING_CAPACITY}"
    assert writes > 10 * ts.RING_CAPACITY - 1, "non-vacuity: the ring overflowed"


def test_the_lifetime_counts_survive_the_ring_eviction():
    """The ring is bounded; the counters are not. An evicted tick still counts."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    writes = ts.RING_CAPACITY * 10
    for i in range(writes):
        t = ts.begin_tick()
        t.set_dispatch("full" if i % 2 == 0 else "idle")
        t.finish(published_ns=i, published_at="x")
    snap = ts.snapshot()
    assert snap.dispatch_counts["full"] == writes // 2
    assert snap.dispatch_counts["idle"] == writes // 2
    assert snap.dispatch_counts["degraded"] == 0
    assert sum(snap.dispatch_counts.values()) == snap.tick_seq
    assert snap.tick_seq > len(snap.records), "non-vacuity: records were evicted"


def test_a_tick_that_named_no_dispatch_counts_as_degraded():
    """`idle + full + degraded == tick_seq` must hold with no escape hatch.

    A crash publish never reaches a dispatch decision, so an unset dispatch is
    exactly the degraded case; leaving it unclassified would silently break the
    sum the operator reads as "how many ticks ran".
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.finish(published_ns=1, published_at="x")
    snap = ts.snapshot()
    assert snap.records[-1].dispatch == "degraded"
    assert snap.dispatch_counts["degraded"] == 1
    assert sum(snap.dispatch_counts.values()) == snap.tick_seq == 1


def test_the_owned_state_stays_inside_its_budget():
    """Measured over a full ring of DISTINCT worst-case strings.

    A fixed short literal is the same object every time, so `_deep_size`'s
    seen-set counts it once and the budget is met by the test's own
    convenience. Each record here carries a distinct string at the cap, which
    is the largest state the module can hold.
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    longest = "z" * ts.PUBLISHED_AT_MAX_CHARS
    for i in range(ts.RING_CAPACITY * 2):
        t = ts.begin_tick()
        t.set_dispatch("full")
        t.set_codex_regime("active")
        t.finish(published_ns=i, published_at=f"{i:04d}{longest}"[:len(longest)])
    records = ts.snapshot().records
    assert len(records) == ts.RING_CAPACITY, (
        "non-vacuity: the budget must be measured over a FULL ring")
    assert len({r.published_at for r in records}) == ts.RING_CAPACITY, (
        "non-vacuity: a shared string object is counted once, so the budget "
        "must be measured over DISTINCT strings")
    assert all(len(r.published_at) == ts.PUBLISHED_AT_MAX_CHARS
               for r in records), "non-vacuity: measured at the cap"
    assert ts._deep_size(ts.snapshot()) <= ts.MEMORY_BUDGET_BYTES


def test_the_two_rings_stay_inside_the_frozen_budget():
    """The cap is FROZEN at 65,536 (#583 S4 §6). Raising it is not a way to
    pass: a criterion that permits raising the constant to whatever the
    implementation measures can never fail."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    longest = "z" * ts.PUBLISHED_AT_MAX_CHARS
    for i in range(ts.RING_CAPACITY * 2):
        t = ts.begin_tick()
        t.set_dispatch("full")
        t.set_codex_regime("active")
        t.finish(published_ns=i, published_at=f"{i:04d}{longest}"[:len(longest)])
    for i in range(ts.RING_CAPACITY * 2):
        ts.record_conversation_pass(
            seq=i, started_ns=i * 10, ended_ns=i * 10 + 5, duration_ns=5,
            cpu_ns=1, status="ok",
        )
    snap = ts.snapshot()
    assert len(snap.records) == ts.RING_CAPACITY
    assert len(snap.conversation_records) == ts.RING_CAPACITY, (
        "non-vacuity: the budget must be measured over TWO full rings")
    assert ts.MEMORY_BUDGET_BYTES == 65536, (
        "the budget is frozen; raising it is a separate reviewed decision")
    assert ts.RING_CAPACITY == 64
    assert ts._deep_size(snap) <= ts.MEMORY_BUDGET_BYTES


def test_the_conversation_ring_is_bounded_and_keeps_the_newest():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    writes = ts.RING_CAPACITY + 10
    for i in range(writes):
        ts.record_conversation_pass(
            seq=i, started_ns=i * 10, ended_ns=i * 10 + 5, duration_ns=5,
            cpu_ns=1, status="ok",
        )
    recs = ts.snapshot().conversation_records
    assert len(recs) == ts.RING_CAPACITY
    assert recs[-1].seq == writes - 1
    assert recs[0].seq == writes - ts.RING_CAPACITY


def test_the_conversation_ring_does_not_disturb_the_tick_ring():
    """Mixing conversation passes into `records` would evict main-tick samples
    and corrupt every aggregate computed over them — the per-regime publish
    period, the ingest/builder split, the dispatch mix."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.set_codex_regime("idle")
    t.finish(published_ns=1, published_at="t0")
    for i in range(ts.RING_CAPACITY * 3):
        ts.record_conversation_pass(
            seq=i, started_ns=i, ended_ns=i + 1, duration_ns=1, cpu_ns=1,
            status="ok",
        )
    snap = ts.snapshot()
    assert len(snap.records) == 1, "a conversation pass evicted a tick record"
    assert snap.tick_seq == 1
    assert snap.dispatch_counts["full"] == 1


def test_the_conversation_status_is_a_closed_set():
    """A frozen dataclass field typed `str` enforces nothing on its own. The
    recorder is the chokepoint that makes the no-leak claim true — an
    implementation could otherwise record `str(exc)` or a path and publish it
    through the debug endpoint while every happy-path test stayed green."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    for bad in [
        "sqlite3.OperationalError: database is locked",
        "/Users/someone/.local/share/cctally/conversations.db",
        "ok; /etc/passwd",
        "",
        None,
    ]:
        ts.record_conversation_pass(
            seq=1, started_ns=0, ended_ns=1, duration_ns=1, cpu_ns=1,
            status=bad,
        )
        rec = ts.snapshot().conversation_records[-1]
        assert rec.status in ts.CONVERSATION_STATUSES
        assert bad in (None, "") or bad not in rec.status
        assert "/" not in rec.status
    assert set(ts.CONVERSATION_STATUSES) == {"ok", "store_unavailable", "error"}


def test_the_conversation_record_carries_no_free_text_field():
    """Every field is a fixed-size scalar except the validated enum string."""
    import dataclasses
    import _lib_tick_stats as ts
    names = [f.name for f in dataclasses.fields(ts.ConversationSyncRecord)]
    assert names == [
        "seq", "started_ns", "ended_ns", "duration_ns",
        "cpu_ns", "period_ns", "status",
    ]


def test_period_ns_is_the_forward_interval():
    """`period_ns` on record i is `start[i+1] - start[i]` — the interval that
    FOLLOWED that pass, stamped onto it when the next pass records. The newest
    record carries no period, because its successor has not started yet.

    Pairing a pass's CPU with the interval that PRECEDED it instead has no
    upper bound at all: a long pass after short ones is then charged against a
    short interval, and the published share exceeds 100%. Alternating gaps make
    the off-by-one visible; a uniform script would hide it.
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    starts = [0, 10, 100, 130]
    for i, start in enumerate(starts):
        ts.record_conversation_pass(
            seq=i, started_ns=start, ended_ns=start + 1, duration_ns=1,
            cpu_ns=1, status="ok",
        )
    periods = [r.period_ns for r in ts.snapshot().conversation_records]
    assert periods == [10, 90, 30, None]


def test_a_completed_tick_does_not_wipe_the_conversation_ring():
    """`TickContext.finish` is the one mutator that rebuilds `StatsSnapshot`
    field by field instead of using `dataclasses.replace`, so it must carry
    `conversation_records` forward by hand.

    Every other test in the estate records ticks BEFORE conversation passes, and
    that order cannot see the loss. Deleting the passthrough leaves the whole
    suite green while every main refresh tick wipes the conversation ring in
    production, so `dashboard-perf` reads `no samples yet` forever against a
    running dashboard.
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    ts.record_conversation_pass(
        seq=7, started_ns=0, ended_ns=1, duration_ns=1, cpu_ns=1,
        status="ok",
    )
    tick = ts.begin_tick()
    tick.set_dispatch("full")
    tick.finish(published_ns=1, published_at="2026-08-17T00:00:00Z")
    recs = ts.snapshot().conversation_records
    assert [r.seq for r in recs] == [7], (
        "a completed tick dropped the conversation ring"
    )


def test_an_oversized_published_at_is_truncated_to_the_cap():
    """The budget holds by construction, not by caller convention."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x" * 4096)
    stored = ts.snapshot().records[-1].published_at
    assert len(stored) == ts.PUBLISHED_AT_MAX_CHARS
    assert stored == "x" * ts.PUBLISHED_AT_MAX_CHARS


def test_an_iso_instant_survives_the_cap_unchanged():
    """Non-vacuity for the cap: it must not truncate a real production value."""
    import datetime as dt
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    instant = dt.datetime(2026, 8, 15, 12, 34, 56, 789012,
                          tzinfo=dt.timezone.utc).isoformat()
    assert len(instant) <= ts.PUBLISHED_AT_MAX_CHARS
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at=instant)
    assert ts.snapshot().records[-1].published_at == instant


def test_concurrent_increments_are_not_lost():
    """The read-modify-write hazard the bare-rebind pattern would have."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    n, threads = 200, []
    for _ in range(8):
        th = threading.Thread(
            target=lambda: [ts.note_cache_open_failure("daily") for _ in range(n)])
        threads.append(th)
        th.start()
    for th in threads:
        th.join()
    assert ts.snapshot().cache_open_failures["daily"] == 8 * n


def test_period_is_the_gap_between_consecutive_final_publishes():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    for pub in (1_000_000_000, 4_500_000_000):
        t = ts.begin_tick()
        t.set_dispatch("full")
        t.set_codex_regime("idle")
        t.finish(published_ns=pub, published_at="x")
    recs = ts.snapshot().records
    assert recs[0].period_ns is None, "the first publish has no predecessor"
    assert recs[1].period_ns == 3_500_000_000


def test_a_reader_cannot_mutate_what_it_read():
    """`records` is a tuple and both count maps are read-only views."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("idle")
    t.finish(published_ns=1, published_at="x")
    snap = ts.snapshot()
    assert isinstance(snap.records, tuple)
    for mapping in (snap.dispatch_counts, snap.cache_open_failures):
        try:
            mapping["idle"] = 999
        except TypeError:
            continue
        raise AssertionError(f"{mapping!r} accepted a write")


# ── The exclusive split (spec §1.3) ─────────────────────────────────────────


def test_a_build_nested_in_an_ingest_is_subtracted_from_the_ingest():
    """The A2 shape: `build_partial()` runs INSIDE `sync_cache`."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}

    def advance(n):
        clock["ns"] += n

    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    with t.ingest_span():
        advance(40)
        with t.build_span():
            advance(60)
        advance(0)
    t.set_dispatch("full")
    t.finish(published_ns=100, published_at="x")
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ns == 40, "the nested build was not subtracted"
    assert rec.builder_ns == 60
    assert rec.ingest_ran is True


def test_an_ingest_nested_in_a_build_is_subtracted_from_the_build():
    """The direct-build shape: `_tui_build_snapshot`'s internal `sync` region."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}

    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    with t.build_span():
        with t.ingest_span():
            clock["ns"] += 25
        clock["ns"] += 75
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ns == 25
    assert rec.builder_ns == 75, "the nested ingest was not subtracted"


def test_three_levels_of_alternating_nesting_partition_the_whole():
    """ingest -> build -> ingest. The two figures must still sum to the span."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}

    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    with t.ingest_span():
        clock["ns"] += 20
        with t.build_span():
            clock["ns"] += 40
            with t.ingest_span():
                clock["ns"] += 20
        clock["ns"] += 20
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ns == 60
    assert rec.builder_ns == 40
    assert rec.ingest_ns + rec.builder_ns == 100


def test_same_kind_nesting_is_counted_once():
    """A re-entrant span of the same kind must not double its own time."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}

    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    with t.ingest_span():
        clock["ns"] += 30
        with t.ingest_span():
            clock["ns"] += 70
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    assert ts.snapshot().records[-1].ingest_ns == 100


def test_no_ingest_span_means_ingest_ran_is_false_not_null():
    """`--no-sync`: false rather than null, because null reads as unmeasured."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}
    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    with t.build_span():
        clock["ns"] += 10
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ran is False
    assert rec.ingest_ns == 0
    assert rec.builder_ns == 10


def test_a_span_left_open_by_an_exception_still_closes():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    clock = {"ns": 0}
    t = ts.begin_tick(monotonic_ns=lambda: clock["ns"])
    try:
        with t.build_span():
            clock["ns"] += 15
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    t.set_dispatch("degraded")
    t.finish(published_ns=1, published_at="x")
    assert ts.snapshot().records[-1].builder_ns == 15


# ── Aggregation over one outer refresh (spec §1.4, §1.5) ────────────────────


def test_any_full_build_in_a_refresh_makes_the_tick_full():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.set_dispatch("idle")
    t.finish(published_ns=1, published_at="x")
    assert ts.snapshot().records[-1].dispatch == "full"


def test_any_codex_rebuild_in_a_refresh_makes_the_tick_active():
    """Last-write would call this idle. It is not (spec §1.5)."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_codex_regime("active")
    t.set_codex_regime("idle")
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    assert ts.snapshot().records[-1].codex_regime == "active"


def test_a_refresh_no_build_reached_the_decision_is_not_observed():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("idle")
    t.finish(published_ns=1, published_at="x")
    assert ts.snapshot().records[-1].codex_regime == "not_observed"


# ── The standalone context (spec §1.2) ──────────────────────────────────────


def test_a_standalone_tick_does_not_count_as_a_refresh_tick():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick(standalone=True)
    t.set_dispatch("full")
    t.finish(published_ns=7, published_at="s")
    snap = ts.snapshot()
    assert snap.tick_seq == 0, "a standalone build is not a refresh tick"
    assert snap.records == ()
    assert snap.standalone is not None
    assert snap.standalone.published_at == "s"
    assert sum(snap.dispatch_counts.values()) == 0


def test_a_standalone_context_is_not_opened_inside_a_dashboard_tick():
    """`current()` is how `_tui_build_snapshot` decides (spec §1.2)."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    assert ts.current() is None
    outer = ts.begin_tick()
    assert ts.current() is outer
    outer.set_dispatch("full")
    outer.finish(published_ns=1, published_at="x")
    assert ts.current() is None


def test_a_finished_context_ignores_a_second_finish():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.finish(published_ns=1, published_at="x")
    t.finish(published_ns=2, published_at="y")
    snap = ts.snapshot()
    assert snap.tick_seq == 1
    assert snap.records[-1].published_at == "x"


def test_an_unknown_enum_value_is_rejected_rather_than_recorded():
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    for setter, bad in ((t.set_dispatch, "sideways"),
                        (t.set_codex_regime, "warm"),
                        (t.set_publication, "eventual")):
        try:
            setter(bad)
        except ValueError:
            continue
        raise AssertionError(f"{setter.__name__} accepted {bad!r}")
    try:
        ts.note_cache_open_failure("hourly")
    except ValueError:
        return
    raise AssertionError("note_cache_open_failure accepted an unknown kind")


def test_the_cache_pin_accumulates_and_clamps():
    """`mark_cache_pin` sums holds within a tick and never records a negative.

    Accumulation is the same choice `_add` makes for ingest and builder time:
    A2 can run several builds inside one refresh, each opening its own pin, so
    a last-write field would understate a refresh that pinned twice.
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()

    t = ts.begin_tick()
    t.mark_cache_pin(1000)
    t.mark_cache_pin(2500)
    t.mark_cache_pin(-9999)   # a backwards clock contributes nothing
    t.mark_cache_pin("not a number")
    t.finish(published_ns=1, published_at="p")
    record = ts.snapshot().records[-1]
    assert record.cache_pin_ns == 3500
    assert record.as_wire()["cache_pin_ns"] == 3500


def test_a_tick_that_never_pinned_reports_zero_rather_than_none():
    """The field is a fixed-size scalar on every record.

    `period_ns` is the module's only nullable field and it is null for a
    documented reason. A tick that opened no cache pin held one for zero
    nanoseconds, which is a number, and making this nullable too would force
    every consumer to distinguish "did not pin" from "no data".
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.finish(published_ns=1, published_at="p")
    assert ts.snapshot().records[-1].cache_pin_ns == 0
