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
import dataclasses
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import types

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
    # SQLite readers can create and remove these sidecars between copytree's
    # directory scan and its copy2 call.  The built corpus is checkpointed;
    # transient sidecars are neither fixture inputs nor safe copy candidates.
    shutil.copytree(
        source_root,
        private_root,
        ignore=shutil.ignore_patterns("*.db-shm", "*.db-wal"),
    )
    return private_root / pathlib.Path(data_dir).name


def _private_frontier_corpus(data_dir, tmp_path, bbf):
    """Copy then rederive cache paths so an exhaustive walk can certify it.

    ``cache.db`` stores absolute source paths.  A plain private copy therefore
    quite correctly sees the shared fixture paths as orphaned and withholds its
    walk-complete sentinel.  Frontier integration tests need a genuinely
    self-contained cache, not a copied database whose source estate lives
    elsewhere.
    """
    corpus = _private_corpus(data_dir, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            assert cctally.sync_cache(conn, rebuild=True).full_walk_complete
            assert cctally.sync_codex_cache(conn, rebuild=True).full_walk_complete
        finally:
            conn.close()
    return corpus


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


def test_refresh_retains_bounded_ingest_and_final_build_phase_trees(
    small_corpus, monkeypatch, tmp_path,
):
    """The authoritative build must not overwrite ingest attribution (#680).

    This drives the real refresh orchestration over both providers.  Requiring
    nested names from the retained tree is non-vacuous: before #680 the final
    snapshot reset the collector and no ingest tree was retained at all.
    """
    import _lib_perf as perf

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    perf.set_enabled(True)
    try:
        ref, _hub = _run_refresh(
            corpus, bbf, force_a2=True, monkeypatch=monkeypatch,
        )
        ingest = perf.last_ingest_perf()
        build = perf.last_backend_perf()
        assert ingest is not None, ref.get().last_sync_error
        assert ingest["phases"]["name"] == "ingest"

        def names(node):
            return {node["name"]} | {
                name
                for child in node.get("children", ())
                for name in names(child)
            }

        ingest_names = names(ingest["phases"])
        assert {
            "ingest.store_open",
            "ingest.claude",
            "ingest.codex",
            "frontier",
            "walk",
            "accounting",
            "projector",
        } <= ingest_names
        assert build is not None
        assert build["phases"]["name"] == "snapshot"
        record = __import__("_lib_tick_stats").snapshot().records[-1]
        # The same seam is covered.  This test deliberately forces synchronous
        # A2 builds inside ingest: the phase tree is inclusive wall time while
        # TickContext subtracts those nested builds from its exclusive ingest
        # total, so the aggregate is bounded by (not equal to) the tree root.
        traced_ns = int(ingest["phases"]["elapsed_ms"] * 1_000_000)
        assert 0 < record.ingest_ns <= traced_ns
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


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


def test_a_cold_refresh_realises_a_codex_rebuild(small_corpus, monkeypatch,
                                                 tmp_path):
    """The precondition for the aggregation gate: a cold tick IS active."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    # #630 S2: this refresh omits `skip_sync=True`, so it runs a REAL ingest.
    # Against the session-scoped corpus that writes ingest state and WAL into
    # a store every other consumer is promised unchanged.
    corpus = _private_corpus(small_corpus, tmp_path)
    _run_refresh(corpus, bbf, force_a2=True, monkeypatch=monkeypatch)
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


def test_ingest_frontier_caught_up_targeted_and_structural_fallback(
    small_corpus, tmp_path, monkeypatch,
):
    """A trusted frontier skips, targets an append, and fails safe on structure."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            claude_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            codex_path = conn.execute(
                "SELECT path FROM codex_session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, "claude", claude_path)
            frontier.record_activity(app_dir, "codex", codex_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            claude_roots = (pathlib.Path(claude_path).parent,)
            codex_roots = (pathlib.Path(codex_path).parent,)
            state.seed_provider("claude", conn, roots=claude_roots)
            state.seed_provider("codex", conn, roots=codex_roots)

            assert state.plan_provider(
                "claude", conn, roots=claude_roots,
            ).mode == "caught_up"
            frontier.record_activity(app_dir, "claude", claude_path)
            targeted = state.plan_provider(
                "claude", conn, roots=claude_roots,
            )
            assert targeted.mode == "targeted"
            assert targeted.paths == frozenset({claude_path})

            pathlib.Path(claude_path).parent.touch()
            assert state.plan_provider(
                "claude", conn, roots=claude_roots,
            ).mode == "full"

            bounded = dict(state.memory_stats())
            assert bounded["entryCount"] == 2
            assert bounded["estimatedBytes"] <= bounded["maxBytes"]
            monkeypatch.setattr(frontier, "FRONTIER_MAX_BYTES", 1)
            assert not state.seed_provider("claude", conn, roots=claude_roots)
            assert state.last_seed_failure["claude"] == "memory_budget"
            assert state.plan_provider(
                "claude", conn, roots=claude_roots).mode == "full"
            assert dict(state.memory_stats())["fallbackCount"] == 1
        finally:
            conn.close()


@pytest.mark.parametrize(
    "frontier_class,open_db,source_table",
    [
        ("DashboardIngestFrontier", "open_cache_db", "session_files"),
        (
            "ConversationSyncFrontier",
            "open_conversations_db",
            "conversation_source_files",
        ),
    ],
)
def test_failed_pre_walk_cutoff_cannot_recapture_a_post_walk_boundary(
    small_corpus, tmp_path, monkeypatch, frontier_class, open_db, source_table,
):
    """A failed cutoff must leave the completed full pass uncertified.

    The production change this catches is treating the failed ``None`` result
    as "capture a boundary now" during finalization.  The ticket written after
    the failed capture models activity arriving while the full walk was in
    flight; consuming it would make the next ordinary tick falsely caught up.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = getattr(cctally, open_db)()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {source_table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            state = getattr(frontier, frontier_class)(app_dir)
            lock_path = frontier.activity_marker_path(app_dir).with_name(
                "dashboard-ingest-activity.lock"
            )
            real_open = frontier.os.open
            failed = {"value": False}

            def fail_first_lock_open(path, flags, mode=0o777):
                if not failed["value"] and os.fspath(path) == os.fspath(lock_path):
                    failed["value"] = True
                    raise OSError("forced cutoff acquisition failure")
                return real_open(path, flags, mode)

            monkeypatch.setattr(frontier.os, "open", fail_first_lock_open)
            cutoff = state.capture_cutoff()
            assert failed["value"], "the cutoff failure injection never fired"

            assert frontier.record_activity(app_dir, "claude", source_path)
            assert not state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            )
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "full"
        finally:
            conn.close()


def test_marker_replacement_read_binds_identity_and_bytes_to_one_descriptor(
    small_corpus, tmp_path, monkeypatch,
):
    """Replacing the marker between path stat and open must force a full pass.

    The replacement carries byte-identical content and length, so only binding
    the identity and bytes to one opened descriptor can distinguish it.  A
    split stat/open reader returns the old identity with the new file's empty
    tail and incorrectly reports ``caught_up``.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            marker = frontier.activity_marker_path(app_dir)
            assert frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "caught_up"

            replacement = marker.with_name(marker.name + ".replacement")
            replacement.write_bytes(marker.read_bytes())
            real_open = pathlib.Path.open
            raced = {"value": False}

            def replace_before_open(path, *args, **kwargs):
                if path == marker and not raced["value"]:
                    raced["value"] = True
                    replacement.replace(marker)
                return real_open(path, *args, **kwargs)

            monkeypatch.setattr(pathlib.Path, "open", replace_before_open)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert raced["value"], "the marker replacement interleaving never fired"
            assert plan.mode == "full"
            assert plan.reason == "marker_replaced"
        finally:
            conn.close()


def test_caught_up_frontier_never_requeries_the_session_file_estate(
    small_corpus, tmp_path, monkeypatch,
):
    """The fast-negative may stat its saved directories, not walk DB paths."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caught-up path requeried every source path")

            monkeypatch.setattr(frontier, "_source_paths", forbidden)
            statements = []
            conn.set_trace_callback(statements.append)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "caught_up"
            state.commit_provider(plan, conn, roots=roots)
            assert not any(
                "session_files" in statement for statement in statements
            ), statements
        finally:
            conn.set_trace_callback(None)
            conn.close()


def test_conversation_frontier_caught_up_and_targeted_use_transcript_cursors(
    small_corpus, tmp_path,
):
    """Transcript sync owns an independent cursor over the shared activity log."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_conversations_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM conversation_source_files LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.ConversationSyncFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "caught_up"

            frontier.record_activity(app_dir, "claude", source_path)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted"
            assert plan.paths == frozenset({source_path})
        finally:
            conn.close()


def test_conversation_frontier_caught_up_never_queries_all_source_paths(
    small_corpus, tmp_path, monkeypatch,
):
    """The transcript fast-negative is O(saved directories), never O(files)."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_conversations_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM conversation_source_files LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.ConversationSyncFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caught-up path queried transcript estate")

            monkeypatch.setattr(frontier, "_conversation_source_paths", forbidden)
            statements = []
            conn.set_trace_callback(statements.append)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "caught_up"
            state.commit_provider(plan, conn, roots=roots)
            assert not any(
                "conversation_source_files" in statement
                or "codex_conversation_source_files" in statement
                for statement in statements
            ), statements
        finally:
            conn.set_trace_callback(None)
            conn.close()


def test_conversation_frontier_replacement_pending_and_cursor_gap_fail_full(
    small_corpus, tmp_path,
):
    """No structural ambiguity may advance the transcript certificate."""
    import _lib_ingest_frontier as frontier

    clean = type("Stats", (), {
        "lock_contended": False, "files_failed": 0,
        "files_deferred_torn": 0, "deferred_reason": None,
        "prune_refused": False, "budget_exhausted": False,
        "maintenance_failed": False, "files_total": 0,
        "files_processed": 0, "files_skipped_unchanged": 0,
    })()
    assert not frontier.conversation_sync_certifiable(
        "targeted", clean, expected_paths=1)
    clean.files_total = 2
    clean.files_processed = 1
    clean.files_skipped_unchanged = 1
    assert frontier.conversation_sync_certifiable("full", clean)

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_conversations_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM conversation_source_files LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.ConversationSyncFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)

            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_rebuild_claude_pending", "1"),
            )
            conn.commit()
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "full"
            conn.execute(
                "DELETE FROM cache_meta WHERE key=?",
                ("conversation_rebuild_claude_pending",),
            )
            conn.commit()

            frontier.record_activity(app_dir, "claude", source_path)
            # Simulate a stored cursor beyond the physical file without
            # mutating the shared benchmark source estate copied into this DB.
            actual_size = pathlib.Path(source_path).stat().st_size
            conn.execute(
                "UPDATE conversation_source_files SET size_bytes=? WHERE path=?",
                (actual_size + 1, source_path),
            )
            conn.commit()
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "full"
        finally:
            conn.close()


@pytest.mark.parametrize(
    ("provider", "table"),
    [
        ("claude", "conversation_source_files"),
        ("codex", "codex_conversation_source_files"),
    ],
)
def test_conversation_frontier_same_size_source_replacement_requires_rebuild(
    small_corpus, tmp_path, provider, table,
):
    """A ticketed same-size rewrite is replacement evidence, never unchanged."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_conversations_db()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            path = pathlib.Path(source_path)
            roots = (path.parent,)
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, provider, source_path)
            state = frontier.ConversationSyncFrontier(app_dir)
            assert state.seed_provider(provider, conn, roots=roots)

            original = path.read_bytes()
            replacement = bytes([original[0] ^ 1]) + original[1:]
            path.write_bytes(replacement)
            current = path.stat()
            stored_mtime = conn.execute(
                f"SELECT mtime_ns FROM {table} WHERE path=?", (source_path,),
            ).fetchone()[0]
            if current.st_mtime_ns == stored_mtime:
                os.utime(
                    path,
                    ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000),
                )
            frontier.record_activity(app_dir, provider, source_path)

            plan = state.plan_provider(provider, conn, roots=roots)
            assert plan.mode == "full"
            assert plan.reason == "source_replaced"
        finally:
            conn.close()


def test_frontier_can_certificate_a_stably_missing_tracked_directory(
    small_corpus, tmp_path,
):
    """A deletion already observed by the full seed remains a stable guard."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            missing_root = tmp_path / "already-removed-source-root"
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider(
                "claude", conn, roots=(missing_root,)
            ), state.last_seed_failure
            assert state.plan_provider(
                "claude", conn, roots=(missing_root,)
            ).mode == "caught_up"
        finally:
            conn.close()


def test_ingest_frontier_database_replacement_and_bad_marker_fail_full(
    small_corpus, tmp_path,
):
    """Replacement and ambiguous activity can never certify caught-up state."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            row = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()
            source_path = row[0]
            app_dir = pathlib.Path(corpus)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            roots = (pathlib.Path(source_path).parent,)
            state.seed_provider("claude", conn, roots=roots)
            marker = frontier.activity_marker_path(app_dir)
            with marker.open("ab") as fh:
                fh.write(b"not-json\n")
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "full"
        finally:
            conn.close()

        # A new connection at the same pathname but a different inode must
        # invalidate the certificate before any provider is skipped.
        replacement = pathlib.Path(corpus) / "cache-replacement.db"
        replacement.write_bytes((pathlib.Path(corpus) / "cache.db").read_bytes())
        (pathlib.Path(corpus) / "cache.db").replace(
            pathlib.Path(corpus) / "cache-old.db")
        replacement.replace(pathlib.Path(corpus) / "cache.db")
        conn = cctally.open_cache_db()
        try:
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "full"
        finally:
            conn.close()


def test_ambiguous_activity_forces_one_full_pass_then_can_reseed(
    small_corpus, tmp_path,
):
    """A pathless hook event is fail-safe without poisoning every later tick."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)

            assert frontier.record_activity(app_dir, "claude", "")
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "full"
            assert plan.reason == "ambiguous_activity"

            state.commit_provider(plan, conn, roots=roots)
            assert state.plan_provider(
                "claude", conn, roots=roots,
            ).mode == "caught_up"
        finally:
            conn.close()


def test_full_seed_preserves_activity_that_arrives_after_its_cutoff(
    small_corpus, tmp_path,
):
    """A ticket written during a full walk belongs to the following tick."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()

            # Models an append after the full walk passed this file but before
            # its successful result tried to mint a certificate.
            frontier.record_activity(app_dir, "claude", source_path)
            assert state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            )
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted"
            assert plan.paths == frozenset({source_path})
        finally:
            conn.close()


def test_full_seed_preserves_first_activity_when_marker_was_absent(
    small_corpus, tmp_path,
):
    """An initially absent marker still has a pre-walk zero-byte cutoff."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            marker = frontier.activity_marker_path(app_dir)
            if marker.exists():
                marker.unlink()
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()
            assert cutoff is not None
            assert cutoff.end == 0

            frontier.record_activity(app_dir, "claude", source_path)
            assert state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            )
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted"
            assert plan.paths == frozenset({source_path})
        finally:
            conn.close()


def test_full_seed_rejects_a_malformed_marker_prefix(small_corpus, tmp_path):
    """A full pass cannot certify over malformed pre-cutoff evidence."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, "claude", source_path)
            marker = frontier.activity_marker_path(app_dir)
            marker.write_bytes(b"not-json\n" + marker.read_bytes())
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()

            assert not state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            )
            assert "malformed" in state.last_seed_failure["claude"]
        finally:
            conn.close()


@pytest.mark.parametrize("provider,table,complete_key", [
    ("claude", "session_files", "claude_ingest_walk_complete"),
    ("codex", "codex_session_files", "dashboard_codex_full_walk_complete"),
])
def test_missing_full_walk_sentinel_invalidates_a_same_inode_store(
    small_corpus, tmp_path, provider, table, complete_key,
):
    """An interrupted destructive rebuild cannot retain its certificate."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, provider, source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider(provider, conn, roots=roots)

            conn.execute(
                "DELETE FROM cache_meta WHERE key=?", (complete_key,)
            )
            conn.commit()
            plan = state.plan_provider(provider, conn, roots=roots)
            assert plan.mode == "full"
            assert plan.reason == "incomplete_store"
        finally:
            conn.close()


@pytest.mark.parametrize("dirty", [
    {"lock_contended": True},
    {"files_failed": 1},
    {"files_deferred_torn": 1},
    {"maintenance_failed": True},
    {"full_walk_complete": False},
])
def test_a_dirty_full_provider_result_can_never_mint_a_certificate(dirty):
    import _lib_ingest_frontier as frontier

    values = {
        "lock_contended": False,
        "files_failed": 0,
        "files_deferred_torn": 0,
        "deferred_reason": None,
        "prune_refused": False,
        "budget_exhausted": False,
        "maintenance_failed": False,
        "full_walk_complete": True,
    }
    values.update(dirty)
    assert not frontier.provider_sync_certifiable(
        "full", types.SimpleNamespace(**values)
    )


@pytest.mark.parametrize("invalid_kind", [
    "directory",
    "outside",
    "relative",
    "non_jsonl",
    "symlink_escape",
])
def test_frontier_rejects_invalid_provider_targets_before_ingest(
    small_corpus, tmp_path, invalid_kind,
):
    """Raw hook payloads are evidence, never filesystem authority."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            outside = tmp_path / "unrelated.jsonl"
            outside.write_text("{}\n")
            if invalid_kind == "directory":
                candidate = roots[0] / "directory.jsonl"
                candidate.mkdir()
                invalid = str(candidate)
            elif invalid_kind == "outside":
                invalid = str(outside)
            elif invalid_kind == "relative":
                invalid = "relative.jsonl"
            elif invalid_kind == "non_jsonl":
                candidate = roots[0] / "not-a-session.txt"
                candidate.write_text("{}\n")
                invalid = str(candidate)
            else:
                candidate = roots[0] / "escaped-session.jsonl"
                candidate.symlink_to(outside)
                invalid = str(candidate)
            if invalid_kind in {"directory", "non_jsonl", "symlink_escape"}:
                assert cctally.sync_cache(conn).full_walk_complete
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots)
            frontier.record_activity(app_dir, "claude", invalid)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "full"
            assert plan.reason == "target_outside_scope"
        finally:
            conn.close()


def test_ingest_frontier_hook_maintenance_and_cursor_changes_fail_full(
    small_corpus, tmp_path,
):
    """Every cheap source-of-truth guard invalidates before a provider skip."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            guard = app_dir / "synthetic-hook-settings.json"
            guard.write_text("{}")
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)

            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            )
            guard.write_text('{"changed":true}')
            assert state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ).reason == "hook_config_changed"

            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_backfill_pending", "1"),
            )
            conn.commit()
            assert state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ).mode == "full"

            conn.execute(
                "DELETE FROM cache_meta WHERE key='conversation_backfill_pending'"
            )
            conn.commit()
            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            )
            conn.execute(
                "UPDATE session_files SET last_byte_offset=size_bytes+1 "
                "WHERE path=?",
                (source_path,),
            )
            conn.commit()
            frontier.record_activity(app_dir, "claude", source_path)
            assert state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ).reason == "cursor_gap"
        finally:
            conn.close()


def test_frontier_refuses_to_seed_while_maintenance_is_pending(
    small_corpus, tmp_path,
):
    """An already-present repair marker cannot become certified normal."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_corpus(small_corpus, tmp_path)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            app_dir = pathlib.Path(corpus)
            roots = (pathlib.Path(source_path).parent,)
            frontier.record_activity(app_dir, "claude", source_path)
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_backfill_pending", "1"),
            )
            conn.commit()
            state = frontier.DashboardIngestFrontier(app_dir)
            assert not state.seed_provider("claude", conn, roots=roots)
            assert state.last_seed_failure["claude"] == "maintenance_pending"

            conn.execute(
                "DELETE FROM cache_meta WHERE key='conversation_backfill_pending'"
            )
            conn.commit()
            assert state.seed_provider("claude", conn, roots=roots)
        finally:
            conn.close()


def test_dashboard_tick_skips_caught_up_estates_and_ingests_append_immediately(
    small_corpus, monkeypatch, tmp_path,
):
    """The real refresh uses the certificate and publishes a new row on tick 1."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        conn = cctally.open_cache_db()
        try:
            claude_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            codex_path = conn.execute(
                "SELECT path FROM codex_session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        frontier.record_activity(pathlib.Path(corpus), "claude", claude_path)
        frontier.record_activity(pathlib.Path(corpus), "codex", codex_path)

        calls = {"claude": [], "codex": []}
        real_claude = tui.sync_cache
        real_codex = tui.sync_codex_cache

        def claude_spy(conn, **kwargs):
            calls["claude"].append(kwargs.get("only_paths"))
            return real_claude(conn, **kwargs)

        def codex_spy(conn, **kwargs):
            calls["codex"].append(kwargs.get("only_paths"))
            return real_codex(conn, **kwargs)

        monkeypatch.setattr(tui, "sync_cache", claude_spy)
        monkeypatch.setattr(tui, "sync_codex_cache", codex_spy)
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        hub = _CapturingHub()
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=hub, pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )

        locked(False)  # establishes both trusted full-sync certificates
        assert ref.get().last_sync_error is None
        first_counts = {name: len(values) for name, values in calls.items()}
        conn = cctally.open_cache_db()
        try:
            probe = locked._ingest_frontier.plan_provider(
                "claude", conn,
                roots=tuple(tui._cctally_core._resolve_claude_projects_dirs()),
                guard_paths=(tui._cctally_core.CLAUDE_SETTINGS_PATH,),
            )
        finally:
            conn.close()
        assert probe.mode == "caught_up", (
            probe.reason, locked._ingest_frontier.last_seed_failure,
        )
        locked(False)  # genuinely caught up: neither provider sync is called
        assert {name: len(values) for name, values in calls.items()} == first_counts

        conn = cctally.open_cache_db()
        try:
            before = conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]
        finally:
            conn.close()
        row = {
            "type": "assistant",
            "uuid": "issue-680-a",
            "parentUuid": None,
            "sessionId": "issue-680-session",
            "timestamp": bbf.CORPUS_CLOCK_UTC.isoformat(),
            "cwd": "/bench/issue-680",
            "gitBranch": "main",
            "requestId": "issue-680-request",
            "message": {
                "id": "issue-680-message",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": "bounded append",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
        with pathlib.Path(claude_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        frontier.record_activity(pathlib.Path(corpus), "claude", claude_path)
        locked(False)
        assert calls["claude"][-1] == {claude_path}
        assert len(calls["codex"]) == first_counts["codex"]
        conn = cctally.open_cache_db()
        try:
            after = conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]
        finally:
            conn.close()
        assert after == before + 1
        assert ref.get().last_sync_error is None


@pytest.mark.parametrize("rotate", [False, True])
def test_activity_marker_write_all_handles_regular_file_short_writes(
    tmp_path, monkeypatch, rotate,
):
    """A successful hook ticket is one complete newline-delimited record."""
    import _lib_ingest_frontier as frontier

    app_dir = tmp_path / "app"
    marker = frontier.activity_marker_path(app_dir)
    monkeypatch.setattr(
        frontier,
        "_MARKER_ROTATE_BYTES",
        0 if rotate else 1024 * 1024,
    )
    real_write = frontier.os.write
    writes = []

    def short_then_complete(fd, payload):
        writes.append(len(payload))
        if len(writes) == 1:
            prefix = max(1, len(payload) // 2)
            return real_write(fd, payload[:prefix])
        return real_write(fd, payload)

    monkeypatch.setattr(frontier.os, "write", short_then_complete)
    assert frontier.record_activity(app_dir, "claude", "/tmp/session.jsonl")
    raw = marker.read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw.decode("utf-8")) == {
        "path": "/tmp/session.jsonl", "provider": "claude",
    }
    assert len(writes) >= 2


@pytest.mark.parametrize(
    "provider,table",
    [
        ("claude", "session_entries"),
        ("codex", "codex_session_entries"),
    ],
)
def test_failed_hook_ticket_publishes_same_file_append_on_first_normal_tick(
    small_corpus, monkeypatch, tmp_path, provider, table,
):
    """Both hook paths fail closed to a real full ingest on tick one."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        record = cctally._load_sibling("_cctally_record")
        conn = cctally.open_cache_db()
        try:
            claude_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            codex_path, codex_prior_total = conn.execute(
                "SELECT path,last_total_tokens FROM codex_session_files "
                "WHERE path LIKE '/%' AND last_total_tokens IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        paths = {"claude": claude_path, "codex": codex_path}
        for source, source_path in paths.items():
            assert frontier.record_activity(
                pathlib.Path(corpus), source, source_path,
            )

        calls = {"claude": [], "codex": []}
        real_claude = tui.sync_cache
        real_codex = tui.sync_codex_cache

        def claude_spy(conn, **kwargs):
            calls["claude"].append(kwargs.get("only_paths"))
            return real_claude(conn, **kwargs)

        def codex_spy(conn, **kwargs):
            calls["codex"].append(kwargs.get("only_paths"))
            return real_codex(conn, **kwargs)

        monkeypatch.setattr(tui, "sync_cache", claude_spy)
        monkeypatch.setattr(tui, "sync_codex_cache", codex_spy)
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        hub = _CapturingHub()
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=hub, pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )

        locked(False)
        first_counts = {name: len(values) for name, values in calls.items()}
        locked(False)
        assert {name: len(values) for name, values in calls.items()} == first_counts

        conn = cctally.open_cache_db()
        try:
            before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

        source_path = pathlib.Path(paths[provider])
        if provider == "claude":
            row = {
                "type": "assistant",
                "uuid": "issue-680-failed-ticket-claude",
                "parentUuid": None,
                "sessionId": "issue-680-failed-ticket-session",
                "timestamp": bbf.CORPUS_CLOCK_UTC.isoformat(),
                "cwd": "/bench/issue-680-failed-ticket",
                "gitBranch": "main",
                "requestId": "issue-680-failed-ticket-request",
                "message": {
                    "id": "issue-680-failed-ticket-message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": "failed ticket first-tick publication",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        else:
            row = {
                "timestamp": bbf.CORPUS_CLOCK_UTC.isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 200,
                            "cached_input_tokens": 25,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 25,
                            "total_tokens": 300,
                        },
                        "total_token_usage": {
                            "total_tokens": codex_prior_total + 300,
                        },
                    },
                },
            }
        with source_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

        monkeypatch.setattr(
            record,
            "_hook_tick_read_stdin_event",
            lambda: {"event": "Stop", "transcript_path": str(source_path)},
        )

        def fail_ticket_write(*_args, **_kwargs):
            raise OSError("forced activity-ticket write failure")

        real_record_activity = frontier.record_activity

        def record_with_failed_write(*args, **kwargs):
            real_write = frontier.os.write
            frontier.os.write = fail_ticket_write
            try:
                return real_record_activity(*args, **kwargs)
            finally:
                frontier.os.write = real_write

        monkeypatch.setattr(
            frontier, "record_activity", record_with_failed_write,
        )
        args = types.SimpleNamespace(
            explain=False,
            foreground=(provider == "codex"),
            no_oauth=False,
            throttle_seconds=None,
            event=None,
            mock_oauth_response=None,
            source=provider,
        )
        if provider == "codex":
            monkeypatch.setattr(record, "_cmd_hook_tick_codex", lambda *a, **k: 0)
        else:
            monkeypatch.setattr(record.os, "fork", lambda: 12345)

        assert cctally.cmd_hook_tick(args) == 0
        assert not frontier.activity_marker_path(pathlib.Path(corpus)).exists()
        monkeypatch.setattr(frontier, "record_activity", real_record_activity)

        publishes_before = len(hub.published)
        locked(False)
        assert calls[provider][-1] is None, "failed activity must force a full pass"
        assert len(hub.published) == publishes_before + 1
        conn = cctally.open_cache_db()
        try:
            after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()
        assert after == before + 1
        assert ref.get().last_sync_error is None


def test_dashboard_recovery_promotes_both_caught_up_providers_to_full(
    small_corpus, monkeypatch, tmp_path,
):
    """A replacement shared cache must be rebuilt by both provider legs now."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        cache_mod = cctally._load_sibling("_cctally_cache")
        conn = cctally.open_cache_db()
        try:
            claude_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            codex_path = conn.execute(
                "SELECT path FROM codex_session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        frontier.record_activity(pathlib.Path(corpus), "claude", claude_path)
        frontier.record_activity(pathlib.Path(corpus), "codex", codex_path)

        calls = {"claude": [], "codex": []}
        force_dirty_codex = {"value": False}
        real_claude = tui.sync_cache
        real_codex = tui.sync_codex_cache

        def claude_spy(conn, **kwargs):
            calls["claude"].append(kwargs.get("only_paths"))
            return real_claude(conn, **kwargs)

        def codex_spy(conn, **kwargs):
            calls["codex"].append(kwargs.get("only_paths"))
            stats = real_codex(conn, **kwargs)
            if force_dirty_codex["value"]:
                stats.maintenance_failed = True
            return stats

        monkeypatch.setattr(tui, "sync_cache", claude_spy)
        monkeypatch.setattr(tui, "sync_codex_cache", codex_spy)
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=_CapturingHub(), pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )
        locked(False)
        calls = {"claude": [], "codex": []}

        recovery_round = {"value": 0}

        def replace_then_run(conn, operations, *, origins):
            assert len(operations) == 3, (
                "recovery plan dropped a provider or frontier finalizer")
            recovery_round["value"] += 1
            cache_path = pathlib.Path(corpus) / "cache.db"
            suffix = recovery_round["value"]
            replacement = pathlib.Path(corpus) / f"cache-recovery-copy-{suffix}.db"
            conn.close()
            shutil.copy2(cache_path, replacement)
            cache_path.replace(
                pathlib.Path(corpus) / f"cache-before-recovery-{suffix}.db")
            replacement.replace(cache_path)
            new_conn = cctally.open_cache_db()
            if force_dirty_codex["value"]:
                # Model an A2 publication after the tick captured its prior
                # complete snapshot but before recovered finalization fails.
                held = ref.get()
                ref.set(dataclasses.replace(held, sessions=()))
            return tuple(operation(new_conn) for operation in operations), new_conn

        monkeypatch.setattr(
            cache_mod, "_run_cache_plan_with_recovery", replace_then_run,
        )
        locked(False)
        assert calls == {"claude": [None], "codex": [None]}
        assert ref.get().last_sync_error is None

        # A later recovered full pass that reports any dirty provider result
        # must retain the last complete publication.  It may surface the error,
        # but it cannot replace visible data with a partially rebuilt family.
        prior = ref.get()
        force_dirty_codex["value"] = True
        calls = {"claude": [], "codex": []}
        locked(False)
        after = ref.get()
        assert calls == {"claude": [None], "codex": [None]}
        assert after.sessions == prior.sessions
        assert after.trend == prior.trend
        assert after.current_week == prior.current_week
        assert after.last_sync_at == prior.last_sync_at
        assert after.last_sync_error == (
            "sync-cache: replacement cache ingest incomplete; "
            "retaining the prior snapshot"
        )


def test_dashboard_second_recovery_failure_retains_complete_snapshot(
    small_corpus, monkeypatch, tmp_path,
):
    """The real recover-once loop cannot publish its partial replacement."""
    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        cache_mod = cctally._load_sibling("_cctally_cache")
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=_CapturingHub(), pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )
        locked(False)
        prior = ref.get()
        assert prior.last_sync_error is None

        calls = {"plan": 0, "recover": 0}

        def fail_on_both_families(*args, **kwargs):
            calls["plan"] += 1
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(
            locked._ingest_frontier, "plan_provider", fail_on_both_families,
        )

        def replace_once(exc, *, origin, active_conn):
            calls["recover"] += 1
            cache_path = pathlib.Path(corpus) / "cache.db"
            replacement = pathlib.Path(corpus) / "cache-retry.db"
            backup = pathlib.Path(corpus) / "cache-before-retry.db"
            replacement_conn = sqlite3.connect(replacement)
            try:
                active_conn.backup(replacement_conn)
                # Model the replacement after destructive rebuild started but
                # before either provider completed. If the retry also fails,
                # reading this family would visibly publish an empty partial.
                replacement_conn.execute("DELETE FROM session_entries")
                replacement_conn.execute("DELETE FROM codex_session_entries")
                replacement_conn.execute(
                    "DELETE FROM cache_meta WHERE key IN (?, ?)",
                    (
                        "claude_ingest_walk_complete",
                        "dashboard_codex_full_walk_complete",
                    ),
                )
                replacement_conn.commit()
            finally:
                replacement_conn.close()
                active_conn.close()
            cache_path.replace(backup)
            replacement.replace(cache_path)
            return True

        monkeypatch.setattr(cache_mod, "_recover_corrupt_cache", replace_once)
        locked(False)

        after = ref.get()
        assert calls == {"plan": 2, "recover": 1}
        assert after.sessions == prior.sessions
        assert after.trend == prior.trend
        assert after.current_week == prior.current_week
        assert after.last_sync_at == prior.last_sync_at
        assert after.last_sync_error.startswith("sync-cache:")


def test_frontier_database_failure_is_inside_the_shared_recovery_plan(
    small_corpus, monkeypatch, tmp_path,
):
    """A corrupt guard query must reach the family recovery boundary."""
    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    with _corpus_env(corpus, bbf) as cctally:
        tui = cctally._cctally_tui
        dash = cctally._load_sibling("_cctally_dashboard")
        cache_mod = cctally._load_sibling("_cctally_cache")
        ref = dash._SnapshotRef(tui._tui_empty_snapshot(bbf.CORPUS_CLOCK_UTC))
        locked = tui._make_run_sync_now_locked(
            ref=ref, hub=_CapturingHub(), pinned_now=bbf.CORPUS_CLOCK_UTC,
            display_tz_pref_override=None, runtime_bind="127.0.0.1",
        )
        locked(False)
        assert ref.get().last_sync_error is None

        real_plan = locked._ingest_frontier.plan_provider
        failed = {"value": False}

        def fail_once(*args, **kwargs):
            if not failed["value"]:
                failed["value"] = True
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real_plan(*args, **kwargs)

        monkeypatch.setattr(locked._ingest_frontier, "plan_provider", fail_once)
        observed = {"inside": False}

        def recover_inside_plan(conn, operations, *, origins):
            try:
                operations[0](conn)
            except sqlite3.DatabaseError:
                observed["inside"] = True
            return tuple(operation(conn) for operation in operations), conn

        monkeypatch.setattr(
            cache_mod, "_run_cache_plan_with_recovery", recover_inside_plan,
        )
        locked(False)
        assert observed["inside"] is True
        assert ref.get().last_sync_error is None

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
    small_corpus, monkeypatch, tmp_path,
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
        # #630 S2: a real ingest (no `skip_sync`), so it owns its own copy.
        _run_refresh(_private_corpus(small_corpus, tmp_path), bbf,
                     monkeypatch=monkeypatch)
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
