"""The tick record, driven through a real dashboard refresh (#583 S1 §7).

Every gate here follows D-1: work bounds and same-process relative
comparisons, never a load-sensitive `elapsed < N` ceiling.

Two facts govern how these tests are written, and both were established by
measurement rather than by reading:

* **`CCTALLY_AS_OF` does not reach `_tui_build_snapshot`.** That variable is
  translated at the dashboard entry point; `_tui_build_snapshot_once` resolves
  `now_utc = now_utc or dt.datetime.now(...)`. A tick built without an explicit
  clock silently takes the degraded Codex branch over this corpus —
  `availability="partial"`, no hero, zero cycle rows — and measures the short
  branch. Every tick below is built at `CORPUS_CLOCK_UTC`.
* **A warm or idle tick reuses the Codex source bundle and never executes the
  Codex leg.** The whole cost of the resolved cycle sits in the cold build, so
  no gate here may expect Codex work on a warm or idle tick.
"""
import contextlib
import importlib.machinery
import importlib.util
import json
import pathlib
import shutil
import sqlite3
import sys

import pytest
from conftest import load_script

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def _load_build_bench():
    """Path-load the hyphenated generator; a plain import cannot find it."""
    path = BIN / "build-bench-fixtures.py"
    loader = importlib.machinery.SourceFileLoader("build_bench_fixtures", str(path))
    spec = importlib.util.spec_from_loader("build_bench_fixtures", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _CapturingHub:
    """A real hub's publish contract, with the frames retained in order."""

    def __init__(self):
        self.published = []

    def publish(self, snap):
        self.published.append(snap)


def _corpus_env(data_dir, bbf):
    """`bbf.pinned_env` over all four axes this corpus actually created."""
    root = pathlib.Path(data_dir).parent
    codex_roots = sorted(p for p in root.glob("codex-*") if p.is_dir())
    return bbf.pinned_env(root / "data", root / "claude",
                          ",".join(str(p) for p in codex_roots), root / "home")


def _run_refresh(data_dir, bbf, *, skip_sync=False, force_a2=False,
                 monkeypatch=None, before=None):
    """Drive one real `_make_run_sync_now_locked` refresh over the corpus.

    `force_a2` drops the A2 throttle interval to zero. `sync_cache` calls its
    progress callback unconditionally once after the walk, so the throttle then
    fires and a REAL `build_partial()` runs synchronously inside the real
    `sync_cache` — which is the nesting spec §1.3 exists for. Nothing about the
    ingest is stubbed.
    """
    with _corpus_env(data_dir, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        if force_a2:
            assert monkeypatch is not None
            monkeypatch.setattr(tui, "_A2_PARTIAL_THROTTLE_S", 0.0)
        if before is not None:
            before(cctally, tui)
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        hub = _CapturingHub()
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=hub, pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )
        locked(skip_sync)
    return ref, hub


def _private_corpus(data_dir, tmp_path):
    """Copy every corpus axis so this test owns cache.db and its flock."""
    source_root = pathlib.Path(data_dir).parent
    private_root = tmp_path / "corpus"
    shutil.copytree(source_root, private_root)
    return private_root / pathlib.Path(data_dir).name


# ── §7.2 the exclusive split never exceeds the whole ────────────────────────


def test_the_exclusive_split_never_exceeds_the_whole(
    small_corpus, monkeypatch, tmp_path,
):
    """A2 runs build_partial INSIDE sync_cache, so naive spans double-count."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = _private_corpus(small_corpus, tmp_path)
    _ref, hub = _run_refresh(corpus, bbf, force_a2=True,
                             monkeypatch=monkeypatch)

    hydrating = [s for s in hub.published if getattr(s, "hydrating", False)]
    assert hydrating, (
        "non-vacuity: no A2 partial published, so nothing was nested and this "
        "gate could not have detected a double count")

    snap = ts.snapshot()
    assert snap.records, "the refresh wrote no tick record at all"
    rec = snap.records[-1]
    assert rec.ingest_ran is True
    assert rec.ingest_ns > 0, "non-vacuity: this tick must have ingested"
    assert rec.builder_ns > 0, "non-vacuity: this tick must have built"
    assert rec.ingest_ns + rec.builder_ns <= rec.duration_ns, (
        f"ingest {rec.ingest_ns} + builder {rec.builder_ns} exceeds "
        f"total {rec.duration_ns}; a nested span was counted twice")


def test_one_refresh_writes_exactly_one_record(
    small_corpus, monkeypatch, tmp_path,
):
    """The A2 partial is a nested build, not a second tick (spec §1.2)."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = _private_corpus(small_corpus, tmp_path)
    _ref, hub = _run_refresh(corpus, bbf, force_a2=True,
                             monkeypatch=monkeypatch)
    assert [s for s in hub.published if getattr(s, "hydrating", False)], (
        "non-vacuity: no nested partial build ran")
    snap = ts.snapshot()
    assert snap.tick_seq == 1
    assert len(snap.records) == 1
    assert snap.standalone is None, (
        "the nested partial opened a standalone context inside a live tick")


def test_no_sync_reports_ingest_ran_false_and_zero(small_corpus):
    """Preserve 17: `--no-sync` is a full non-hydrating seed with no ingest."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    _ref, hub = _run_refresh(small_corpus, bbf, skip_sync=True)
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ran is False
    assert rec.ingest_ns == 0
    assert rec.builder_ns > 0, "non-vacuity: the build still ran"
    assert hub.published and hub.published[-1].hydrating is False


def test_a_standalone_build_is_recorded_without_a_dashboard_tick(small_corpus):
    """`tui --render-once` and `cctally-snapshot-measure` reach this path."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    with _corpus_env(small_corpus, bbf) as cctally:
        cctally._cctally_tui._tui_build_snapshot(
            now_utc=bbf.CORPUS_CLOCK_UTC, skip_sync=True,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
    snap = ts.snapshot()
    assert snap.tick_seq == 0, "a standalone build is not a refresh tick"
    assert snap.records == ()
    assert snap.standalone is not None
    assert snap.standalone.builder_ns > 0
    assert snap.standalone.ingest_ran is False


# ── §7.4 regime aggregation ─────────────────────────────────────────────────


def test_a_cold_refresh_realises_a_codex_rebuild(small_corpus, monkeypatch):
    """The precondition for the aggregation gate: a cold tick IS active."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    _run_refresh(small_corpus, bbf, force_a2=True, monkeypatch=monkeypatch)
    rec = ts.snapshot().records[-1]
    assert rec.codex_regime == "active", (
        f"a cold refresh did not realise a Codex rebuild: {rec.codex_regime}")
    assert rec.dispatch == "full"
    assert rec.cold is True


def test_a_refresh_whose_early_build_rebuilt_codex_is_active(small_corpus,
                                                             monkeypatch):
    """Last-write would call this idle. It is not (spec §1.5, review P1-2).

    Drives the classifier directly with the two realised decisions a single
    refresh can produce, in the order that makes last-write wrong: an early
    build rebuilds, a later one reuses. Simulating the DECISIONS rather than
    contriving a corpus that produces them is deliberate — the corpus carries
    exactly one weekly cycle, so the disagreement cannot be provoked from data.
    """
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    tick = ts.begin_tick()
    tick.set_dispatch("full")
    tick.set_codex_regime("active")   # the early, expensive build
    tick.set_dispatch("idle")
    tick.set_codex_regime("idle")     # the later build, off the reuse memo
    tick.finish(published_ns=1, published_at="x")
    rec = ts.snapshot().records[-1]
    assert rec.codex_regime == "active"
    assert rec.dispatch == "full"


def test_dispatch_counts_sum_to_completed_ticks(small_corpus):
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    for _ in range(3):
        _run_refresh(small_corpus, bbf, skip_sync=True)
    snap = ts.snapshot()
    counts = snap.dispatch_counts
    assert snap.tick_seq == 3
    assert counts["idle"] + counts["full"] + counts["degraded"] == snap.tick_seq


def test_a_warm_refresh_idles_and_does_not_touch_the_codex_leg(small_corpus):
    """Measured, not assumed: a warm tick reuses the whole source bundle.

    KNOWN GAP, recorded rather than fixed: `codex_regime == "idle"` has no
    end-to-end coverage anywhere. It needs a refresh whose Codex leg REUSES
    while a full build runs, and the corpus carries exactly one weekly cycle,
    so a full build over it always rebuilds Codex and an idle tick never
    reaches the decision at all. The classification itself is covered at unit
    level; only the realised-reuse path over real data is not.
    """
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    for _ in range(2):
        _run_refresh(small_corpus, bbf, skip_sync=True)
    records = ts.snapshot().records
    assert records[0].dispatch == "full", "the first tick must be the cold one"
    assert records[1].dispatch == "idle", (
        f"the second tick did not idle: {records[1].dispatch}")
    assert records[1].codex_regime == "not_observed", (
        "an idle tick reuses the bundle, so no build reaches the Codex "
        f"decision: got {records[1].codex_regime}")
    assert records[1].cold is False


# ── §7.1.4 Group A cache-open attribution ───────────────────────────────────


def _dashboard():
    cctally = load_script()
    return cctally["_load_sibling"]("_cctally_dashboard")


@pytest.mark.parametrize("kind,caller", [
    ("daily", "_group_a_daily_buckets"),
    ("weekly", "_group_a_weekly_buckets"),
    ("monthly", "_group_a_monthly_buckets"),
])
def test_each_group_a_open_failure_increments_its_own_counter(
    kind, caller, monkeypatch, small_corpus
):
    """Exactly one fixed counter, no SQL, and the original error unchanged."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    dash = _dashboard()
    sentinel = sqlite3.OperationalError("unable to open database file")
    opened = {"n": 0}

    def failing_open(*a, **kw):
        opened["n"] += 1
        raise sentinel

    monkeypatch.setattr(dash, "_raw_open_cache_db", failing_open)
    monkeypatch.setattr(dash, "_GROUP_A_CACHE_ENABLED", True)

    with _corpus_env(small_corpus, bbf):
        fn = getattr(dash, caller)
        if caller == "_group_a_weekly_buckets":
            got = fn(None, bbf.CORPUS_CLOCK_UTC, weeks=[])
        else:
            got = fn(bbf.CORPUS_CLOCK_UTC, n=3, display_tz=None) if (
                caller == "_group_a_daily_buckets"
            ) else fn(bbf.CORPUS_CLOCK_UTC, n=3,
                      range_start=bbf.CORPUS_CLOCK_UTC, display_tz=None)

    assert got is None, "the helper must still fall back, byte-identically"
    assert opened["n"] == 1, "non-vacuity: the raw open was never attempted"
    counts = ts.snapshot().cache_open_failures
    assert counts[kind] == 1, f"{kind} was not attributed: {dict(counts)}"
    assert sum(counts.values()) == 1, f"another counter moved: {dict(counts)}"


def test_the_wrapper_reraises_the_original_exception_unchanged(monkeypatch):
    """A diagnostic must never replace the error it was observing."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    dash = _dashboard()
    sentinel = sqlite3.OperationalError("unable to open database file")

    def failing_open(*a, **kw):
        raise sentinel

    monkeypatch.setattr(dash, "_raw_open_cache_db", failing_open)
    with pytest.raises(sqlite3.OperationalError) as caught:
        dash.open_cache_db()
    assert caught.value is sentinel
    assert sum(ts.snapshot().cache_open_failures.values()) == 0, (
        "an unmatched caller must increment nothing (the fail-open rule)")


def test_an_unmatched_caller_does_not_guess_by_name(monkeypatch):
    """Identity, not `co_name`: a same-named stranger must not be credited."""
    import _lib_tick_stats as ts
    ts.reset_for_tests()
    dash = _dashboard()

    def failing_open(*a, **kw):
        raise sqlite3.OperationalError("nope")

    monkeypatch.setattr(dash, "_raw_open_cache_db", failing_open)

    ns: dict = {}
    exec(  # noqa: S102 — a deliberate same-named impostor
        "def _group_a_daily_buckets(open_cache_db):\n"
        "    try:\n"
        "        open_cache_db()\n"
        "    except Exception:\n"
        "        return None\n",
        ns,
    )
    assert ns["_group_a_daily_buckets"](dash.open_cache_db) is None
    assert sum(ts.snapshot().cache_open_failures.values()) == 0, (
        "a function merely NAMED _group_a_daily_buckets was credited")


def test_a_successful_open_costs_no_counter_and_returns_the_connection(
    small_corpus
):
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    dash = _dashboard()
    with _corpus_env(small_corpus, bbf):
        conn = dash.open_cache_db()
        try:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            conn.close()
    assert sum(ts.snapshot().cache_open_failures.values()) == 0


# ── §7.1.1 the instrument's work does not scale with the corpus ─────────────


class _NullTick:
    """Every `TickContext` method, doing nothing. The §7.1.2 comparison arm."""

    def _span(self):
        return contextlib.nullcontext()

    ingest_span = build_span = _span

    def mark_ingest(self, ns): pass
    def mark_build(self, ns): pass
    def set_dispatch(self, value): pass
    def set_codex_regime(self, value): pass
    def set_publication(self, value): pass
    def set_cold(self, value): pass
    def mark_degraded(self): pass
    def finish(self, **kw): pass

    @property
    def finished(self):
        return True


def _log_every_entry_point(monkeypatch, ts, log):
    """Wrap every `_lib_tick_stats` entry point the tick can reach."""
    real_begin = ts.begin_tick

    def begin(*a, **kw):
        log.append("begin_tick")
        return real_begin(*a, **kw)

    monkeypatch.setattr(ts, "begin_tick", begin)

    def wrap(name):
        real = getattr(ts.TickContext, name)

        def wrapper(self, *a, **kw):
            log.append(name)
            return real(self, *a, **kw)
        return wrapper

    for name in ("ingest_span", "build_span", "mark_ingest", "mark_build",
                 "set_dispatch", "set_codex_regime", "set_publication",
                 "set_cold", "mark_degraded", "finish"):
        monkeypatch.setattr(ts.TickContext, name, wrap(name))

    real_note = ts.note_cache_open_failure

    def note(kind):
        log.append(f"note_cache_open_failure:{kind}")
        return real_note(kind)

    monkeypatch.setattr(ts, "note_cache_open_failure", note)


def _provider_row_counts(data_dir):
    conn = sqlite3.connect(pathlib.Path(data_dir) / "cache.db")
    try:
        return {
            "claude": conn.execute(
                "SELECT COUNT(*) FROM session_entries").fetchone()[0],
            "codex": conn.execute(
                "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0],
        }
    finally:
        conn.close()


def test_the_instrument_does_the_same_work_over_a_ten_times_larger_corpus(
    tiny_corpus, small_corpus, monkeypatch,
):
    """Constant overhead, asserted as a fixed call sequence (spec §7.1.1)."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()

    small_rows = _provider_row_counts(small_corpus)
    tiny_rows = _provider_row_counts(tiny_corpus)
    assert small_rows["claude"] >= 10 * tiny_rows["claude"], (
        f"non-vacuity: the pair must differ by >=10x on Claude rows, got "
        f"{small_rows['claude']} vs {tiny_rows['claude']}")
    assert small_rows["codex"] >= 10 * tiny_rows["codex"], (
        f"non-vacuity: the pair must differ by >=10x on Codex rows, got "
        f"{small_rows['codex']} vs {tiny_rows['codex']}")

    sequences = {}
    for label, corpus in (("tiny", tiny_corpus), ("small", small_corpus)):
        with monkeypatch.context() as mp:
            ts.reset_for_tests()
            log: list[str] = []
            _log_every_entry_point(mp, ts, log)
            _run_refresh(corpus, bbf, skip_sync=True)
            sequences[label] = list(log)

    assert sequences["tiny"] == sequences["small"], (
        "the instrument's call sequence changed with the corpus size:\n"
        f"  tiny  {sequences['tiny']}\n  small {sequences['small']}")
    counted = sequences["tiny"]
    assert counted.count("begin_tick") == 1
    assert counted.count("finish") == 1
    assert small_rows["claude"] > len(counted), (
        f"non-vacuity: the larger corpus must hold more rows "
        f"({small_rows['claude']}) than the instrument has operations "
        f"({len(counted)})")


def _trace_every_connection(cctally, tui, statements):
    """Install a SQL trace callback on every connection the tick opens."""
    def wrap(opener):
        def opened(*a, **kw):
            conn = opener(*a, **kw)
            try:
                conn.set_trace_callback(statements.append)
            except Exception:  # noqa: BLE001 — a stubbed conn in some paths
                pass
            return conn
        return opened
    return wrap(tui.open_db), wrap(cctally.open_cache_db)


def test_an_idle_tick_issues_identical_sql_with_and_without_the_recorder(
    small_corpus, monkeypatch,
):
    """Spec §7.1.2. The instrument must add no query and change no plan."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()

    def run(null_recorder):
        statements: list[str] = []
        with monkeypatch.context() as mp:
            def before(cctally, tui):
                db, cache = _trace_every_connection(cctally, tui, statements)
                mp.setattr(tui, "open_db", db)
                mp.setattr(cctally, "open_cache_db", cache)
                if null_recorder:
                    null = _NullTick()
                    mp.setattr(ts, "begin_tick",
                               lambda **kw: null)
                    mp.setattr(ts, "current", lambda: null)
                    mp.setattr(ts, "note_cache_open_failure", lambda kind: None)
            _run_refresh(small_corpus, bbf, skip_sync=True, before=before)
        return statements

    _run_refresh(small_corpus, bbf, skip_sync=True)        # warm the memo
    real = run(False)
    assert ts.snapshot().records[-1].dispatch == "idle", (
        "precondition: both arms must take the SAME (idle) branch")
    null = run(True)

    assert len(real) > 10, f"non-vacuity: only {len(real)} statements captured"
    assert real == null, (
        "the recorder changed the SQL an idle tick issues:\n"
        f"  only with the recorder: {[s for s in real if s not in null][:5]}\n"
        f"  only without it:        {[s for s in null if s not in real][:5]}")

    import _lib_tick_stats as module
    source = pathlib.Path(module.__file__).read_text()
    assert "sqlite3" not in source, (
        "the instrument imported a database driver; it owns no storage")


# ── §7.3 the publish period is computed correctly ───────────────────────────


def test_the_period_is_the_gap_between_two_injected_final_publishes(
    monkeypatch,
):
    """Injected clocks, the real publication helper, a capturing hub.

    No elapsed ceiling, no sleep, no machine-speed assertion — this is the
    timing guard's explicitly permitted compare-two-observed-events shape,
    with both events supplied rather than measured.
    """
    import datetime as dt
    import _lib_tick_stats as ts
    import _cctally_tui as tui
    ts.reset_for_tests()

    injected = [
        (1_000_000_000, dt.datetime(2026, 8, 15, 0, 0, 0,
                                    tzinfo=dt.timezone.utc)),
        (4_500_000_000, dt.datetime(2026, 8, 15, 0, 0, 3, 500000,
                                    tzinfo=dt.timezone.utc)),
    ]
    ring_lengths_at_publish = []

    class _Watching:
        def publish(self, snap):
            # The record must not exist yet: a reader that sees a ring entry
            # must know the frame reached the hub (spec §7.3).
            ring_lengths_at_publish.append(len(ts.snapshot().records))

    for index, (mono, when) in enumerate(injected):
        tick = ts.begin_tick(monotonic_ns=lambda: mono)
        tick.set_dispatch("full")
        tui._tui_publish_final(
            tick, _Watching(), object(),
            monotonic_ns=lambda mono=mono: mono,
            utcnow=lambda when=when: when,
        )
        assert len(ts.snapshot().records) == index + 1

    assert ring_lengths_at_publish == [0, 1], (
        f"a ring entry appeared BEFORE its publish: {ring_lengths_at_publish}")
    records = ts.snapshot().records
    assert [r.published_ns for r in records] == [m for m, _ in injected]
    assert [r.published_at for r in records] == [w.isoformat()
                                                 for _, w in injected]
    assert records[0].period_ns is None, "the first publish has no predecessor"
    assert records[1].period_ns == 3_500_000_000
    assert records[1].publication == "final"
    ts.reset_for_tests()


# ── §7.5 non-regression ─────────────────────────────────────────────────────


def test_the_environment_is_unchanged_after_a_gate_that_pins_it(small_corpus):
    """Spec §7.5. `_pin_env` deliberately leaves the process pinned, which is
    right for the builder and wrong for a gate: a leaked override wins over a
    later test's HOME-based resolution and points APP_DIR at a deleted scratch
    directory. Every gate in this file goes through `pinned_env`, which
    restores all four axes — absence restored AS absence."""
    import os
    bbf = _load_build_bench()
    before = {key: os.environ.get(key) for key in bbf.PINNED_ENV_KEYS}
    _run_refresh(small_corpus, bbf, skip_sync=True)
    after = {key: os.environ.get(key) for key in bbf.PINNED_ENV_KEYS}
    assert after == before, (
        f"the refresh left the environment changed: "
        f"{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }")


def test_the_owner_thread_tripwire_stays_armed_across_a_tick(small_corpus):
    """Preserve 11: the tick boundary opens AFTER `mark_owner_thread`, so the
    thread holding `sync_lock` for this rebuild still owns the accelerator
    caches and a lock-bypassing foreign-thread mutation is still caught."""
    import threading
    bbf = _load_build_bench()
    with _corpus_env(small_corpus, bbf) as cctally:
        sc = cctally._load_sibling("_lib_snapshot_cache")
        dash = cctally._load_sibling("_cctally_dashboard")
        tui = cctally._cctally_tui
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=_CapturingHub(), pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )
        locked(True)
        owner = sc._OWNER_THREAD_IDENT
    assert owner is not None, "the tripwire was disarmed by the tick boundary"
    assert owner == threading.get_ident(), (
        "ownership did not transfer to the thread that held the lock")
    with pytest.raises(RuntimeError):
        # A foreign thread must still be refused while the tripwire is armed.
        error = {}

        def foreign():
            try:
                sc._assert_owner()
            except RuntimeError as exc:
                error["exc"] = exc

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join()
        if "exc" in error:
            raise error["exc"]


def test_the_published_envelope_is_unchanged_by_the_recorder(small_corpus,
                                                             monkeypatch):
    """Spec §7.5. S1 may add instrumentation; it may not move a byte.

    Compared against the SAME tree with every `_lib_tick_stats` entry point
    replaced by a no-op, so the difference under test is the recorder's
    presence and nothing else. `bench/baselines/envelope-oracle.json` states
    the absolute reference for the generated corpus, and is verified by
    `bin/cctally-snapshot-measure --corpus small`, whose pinned corpus root is
    load-bearing for the hash and therefore cannot be reproduced from a pytest
    tmp directory.
    """
    import _lib_tick_stats as ts
    bbf = _load_build_bench()

    def build(null_recorder):
        with monkeypatch.context() as mp:
            with _corpus_env(small_corpus, bbf) as cctally:
                if null_recorder:
                    null = _NullTick()
                    mp.setattr(ts, "begin_tick", lambda **kw: null)
                    mp.setattr(ts, "current", lambda: null)
                    mp.setattr(ts, "note_cache_open_failure", lambda k: None)
                snap = cctally._cctally_tui._tui_build_snapshot(
                    now_utc=bbf.CORPUS_CLOCK_UTC, skip_sync=True,
                    precompute_envelope=True, runtime_bind="127.0.0.1",
                )
                return cctally.snapshot_to_envelope(
                    snap, now_utc=bbf.CORPUS_CLOCK_UTC,
                    runtime_bind="127.0.0.1",
                )

    with_recorder = build(False)
    without_recorder = build(True)
    assert len(json.dumps(with_recorder)) > 100_000, (
        "non-vacuity: the envelope must be the real, populated one")
    assert with_recorder == without_recorder, (
        "the recorder changed the published envelope")


def test_a_refresh_applies_a_pending_trace_request(small_corpus):
    """Acceptance item 3, end to end: the POST records, the TICK applies.

    The mailbox and the endpoint are covered elsewhere. This is the missing
    link between them — that `_make_run_sync_now_locked` consumes the request
    at its authoritative-build boundary, so `--trace on` reaches a running
    process without a restart.
    """
    import _lib_perf as perf
    bbf = _load_build_bench()
    saved = perf.enabled()
    try:
        perf.set_enabled(False)
        perf.request_enabled(True)
        assert perf.enabled() is False, (
            "precondition: the request must not have flipped anything yet")
        _run_refresh(small_corpus, bbf, skip_sync=True)
        assert perf.enabled() is True, (
            "the refresh did not consume the pending trace request")
        assert perf.pending_state() == (True, True)

        perf.request_enabled(False)
        assert perf.enabled() is True, "still armed until the next build"
        _run_refresh(small_corpus, bbf, skip_sync=True)
        assert perf.enabled() is False, "the disarm did not reach the tick"
    finally:
        perf.request_enabled(saved)
        perf.apply_pending()
        perf.set_enabled(saved)
        perf.reset_thread()


def test_the_cache_pin_hold_is_measured_at_its_own_boundaries(
    small_corpus, monkeypatch,
):
    """The recorded hold is the BEGIN-to-ROLLBACK span, not the function's cost.

    #583 S5 acceptance criterion 16. The 8.651 s figure quoted around this
    session is `_tui_build_source_bundle`'s cumulative duration, which also
    counts the work before `BEGIN` and after `ROLLBACK`; it is an upper bound
    on the hold rather than the hold, and no document may quote it as one.

    So the gate is a same-process relative comparison, per D-1: the recorded
    hold must be STRICTLY LESS than the traced `build.source_bundle` phase it
    sits inside. An implementation that "measured" the hold by timing the
    whole function would report the two as equal and fail here, which is the
    only way to tell the two quantities apart without a wall-clock ceiling.
    """
    import _lib_perf
    import _lib_tick_stats as ts

    bbf = _load_build_bench()
    ts.reset_for_tests()
    _lib_perf.set_enabled(True)
    try:
        _lib_perf.reset_thread()
        _run_refresh(small_corpus, bbf, monkeypatch=monkeypatch)
        root = _lib_perf.current_root()
        tree = root.to_dict() if root is not None else {}
    finally:
        _lib_perf.set_enabled(False)
        _lib_perf.reset_thread()

    def find(node, name):
        if node.get("name") == name:
            return node
        for child in node.get("children", ()):
            hit = find(child, name)
            if hit is not None:
                return hit
        return None

    bundle = find(tree, "build.source_bundle")
    assert bundle is not None, (
        "non-vacuity: the traced build must contain a source_bundle phase, or "
        "there is nothing to compare the recorded hold against")

    record = ts.snapshot().records[-1]
    assert record.cache_pin_ns > 0, (
        "non-vacuity: this refresh must actually have opened a cache pin")
    bundle_ns = bundle["elapsed_ms"] * 1_000_000
    assert record.cache_pin_ns < bundle_ns, (
        f"the recorded hold {record.cache_pin_ns}ns is not strictly inside "
        f"build.source_bundle's {bundle_ns}ns, so it is the function's "
        "duration rather than the transaction's hold")
    assert record.cache_pin_ns <= record.duration_ns
