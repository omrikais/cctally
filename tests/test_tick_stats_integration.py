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
import ast
import contextlib
import dataclasses
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import time
import types

import pytest
from conftest import copy_shared_corpus, corpus_lock_path, load_script
from tests._support_http import PRESENCE_BACKSTOP_SECONDS

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


def _private_corpus(data_dir, tmp_path, name="corpus"):
    """Copy every corpus axis so this test owns cache.db and its flock.

    Delegates to ``conftest.copy_shared_corpus``, the ONE copier in the estate.
    It resolves the source root, excludes the SQLite sidecars a concurrent
    reader can create and remove between ``copytree``'s directory scan and its
    ``copy2`` call, and holds the scale's build lock SHARED for the whole walk
    so a copy can never observe ``_clear_previous_corpus`` mid-rebuild.

    ``name`` distinguishes two copies inside one test, which the writer-hazard
    regression needs: one tree standing in for the shared corpus, and one
    private copy taken from it.
    """
    return copy_shared_corpus(data_dir, tmp_path / name)


def _plan_detail(plan, note=""):
    """A frontier plan's mode AND its reason, for an assertion message.

    ``plan_provider`` distinguishes eleven refusals — ``certificate_expired``,
    ``database_replaced``, ``schema_changed``, ``incomplete_store``,
    ``maintenance_changed``, ``hook_config_changed``, ``filesystem_changed``,
    ``marker_replaced``, ``ambiguous_activity``, ``target_outside_scope`` and
    ``cursor_gap`` — and every one of them presents to a bare
    ``assert plan.mode == "caught_up"`` as the single word ``full``. That is
    why no retained log from any past failure of this module could be
    diagnosed after the fact, and why every assertion over a plan here reports
    the reason whether or not it asserts one.
    """
    detail = f"plan mode={plan.mode!r} reason={plan.reason!r}"
    return f"{note}\n{detail}" if note else detail


def _assert_plan(plan, *, mode=None, reason=None, note=""):
    """Assert a plan's mode and/or reason, reporting both on failure."""
    if mode is not None:
        assert plan.mode == mode, _plan_detail(plan, note)
    if reason is not None:
        assert plan.reason == reason, _plan_detail(plan, note)
    return plan


def _seed_detail(state, note=""):
    """A frontier's seed refusals, for an assertion message.

    ``seed_provider`` returns a bare boolean and records WHY it refused in
    ``last_seed_failure``. An assertion that reports only the boolean says a
    seed failed and nothing about which of the refusals fired.
    """
    detail = f"last_seed_failure={dict(getattr(state, 'last_seed_failure', {}) or {})}"
    return f"{note}\n{detail}" if note else detail


#: The four tables a frontier reads its ``roots`` from, by the database that
#: holds them. Stated as a closed list rather than as "any store": both stores
#: carry many other path-bearing columns, and claiming to cover all of them
#: while checking four would be a false claim, not a conservative one.
_FRONTIER_ROOT_TABLES = {
    "cache": ("session_files", "codex_session_files"),
    "conversations": (
        "conversation_source_files", "codex_conversation_source_files",
    ),
}


def _assert_clean_rebuild(label, stats):
    """A rederive that failed files, or never took its lock, proves nothing."""
    assert not stats.lock_contended, (
        f"{label} never took its writer lock, so it rederived nothing")
    assert stats.files_failed == 0, (
        f"{label} failed {stats.files_failed} of {stats.files_total} files")
    assert stats.files_processed > 0, (
        f"{label} processed no files at all, so every containment check below "
        f"would pass over an empty table")


def _assert_roots_are_private(conn, tables, private_root):
    """Every frontier root in ``tables`` resolves INSIDE ``private_root``.

    Resolved-path containment, not a string prefix. ``startswith`` admitted
    ``/tmp/corpus-evil/x.jsonl`` against ``/tmp/corpus``, and on macOS it also
    disagreed with itself whenever one side had been through ``/private/var``
    and the other had not.

    Each table must also be NON-EMPTY. "No stray rows" is satisfied vacuously
    by a table a failed rebuild left with no rows at all, which is the state
    this check most needs to catch.

    EVERY row is selected, not only the ones matching ``path LIKE '/%'``. A
    filter on absolute paths exempted a relative path from both halves of this
    check — it counted toward neither the non-empty assertion nor the
    containment one — while the docstring claimed every frontier root resolves
    inside the private root. A stored ``../shared/x.jsonl`` would have passed.
    A non-absolute path is therefore reported on its own, and it is reported
    BEFORE the containment test because ``Path.resolve`` grounds a relative
    path against the current working directory, which could place it inside
    ``private_root`` by accident.
    """
    for table in tables:
        rows = [str(row[0]) for row in conn.execute(f"SELECT path FROM {table}")]
        assert rows, (
            f"{table} holds no source path at all, so a containment check "
            f"over it would pass without examining anything")
        relative = [raw for raw in rows if not raw.startswith("/")]
        assert not relative, (
            f"{table} holds a non-absolute source path, which resolves "
            f"against the process working directory rather than against "
            f"{private_root}: {relative[:3]}"
        )
        stray = [
            raw for raw in rows
            if not pathlib.Path(raw).resolve().is_relative_to(private_root)
        ]
        assert not stray, (
            f"{table} still points outside the private corpus "
            f"{private_root}: {stray[:3]}"
        )


def _private_frontier_corpus(data_dir, tmp_path, bbf, name="corpus"):
    """Copy then rederive cache paths so an exhaustive walk can certify it.

    WHAT IS REDERIVED AND WHAT IS COPIED. The JSONL sources, the Codex
    rollouts, ``home/`` and every other file under the corpus root are COPIED
    verbatim. ``cache.db`` and ``conversations.db`` are copied and then
    REDERIVED in place, by a full-rebuild ``sync_cache``, ``sync_codex_cache``,
    ``sync_claude_conversations`` and ``sync_codex_conversations`` over the
    private tree. ``stats.db`` and the append-only journal are neither: they
    are copied and left as they are, because no frontier reads a root from
    them.

    Both databases store ABSOLUTE source paths. A plain private copy therefore
    quite correctly sees the shared fixture paths as orphaned and withholds its
    walk-complete sentinel, so a frontier test needs a genuinely self-contained
    cache rather than a copied database whose source estate lives elsewhere.

    ``conversations.db`` is rederived for that reason and one more: the
    frontier tests take their ``roots`` from ``conversation_source_files.path``,
    so a copied conversations database aims them at the SESSION-SCOPED corpus
    that every xdist worker shares. ``plan_provider`` stats those roots, and a
    write by any other worker then trips its ``filesystem_changed`` guard and
    degrades the plan to ``full`` — a cross-worker race whose victim is
    whichever test happens to be between its seed and its plan.
    """
    corpus = _private_corpus(data_dir, tmp_path, name)
    private_root = pathlib.Path(corpus).parent.resolve()
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            claude = cctally.sync_cache(conn, rebuild=True)
            assert claude.full_walk_complete
            _assert_clean_rebuild("sync_cache(rebuild=True)", claude)
            codex = cctally.sync_codex_cache(conn, rebuild=True)
            assert codex.full_walk_complete
            _assert_clean_rebuild("sync_codex_cache(rebuild=True)", codex)
            # Fail here, deterministically, rather than inside whichever test
            # later draws a root from this table: a path outside the private
            # tree IS the cross-worker race, and it is invisible at the point
            # it actually causes a failure.
            _assert_roots_are_private(
                conn, _FRONTIER_ROOT_TABLES["cache"], private_root)
        finally:
            conn.close()
        conv = cctally.open_conversations_db()
        try:
            _assert_clean_rebuild(
                "sync_claude_conversations(rebuild=True)",
                cctally.sync_claude_conversations(conv, rebuild=True))
            _assert_clean_rebuild(
                "sync_codex_conversations(rebuild=True)",
                cctally.sync_codex_conversations(conv, rebuild=True))
            _assert_roots_are_private(
                conv, _FRONTIER_ROOT_TABLES["conversations"], private_root)
        finally:
            conv.close()
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


def test_no_sync_reports_ingest_ran_false_and_zero(private_corpus):
    """Preserve 17: `--no-sync` is a full non-hydrating seed with no ingest."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = private_corpus("small")
    _ref, hub = _run_refresh(corpus, bbf, skip_sync=True)
    rec = ts.snapshot().records[-1]
    assert rec.ingest_ran is False
    assert rec.ingest_ns == 0
    assert rec.builder_ns > 0, "non-vacuity: the build still ran"
    assert hub.published and hub.published[-1].hydrating is False


def test_a_standalone_build_is_recorded_without_a_dashboard_tick(private_corpus):
    """`tui --render-once` and `cctally-snapshot-measure` reach this path."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = private_corpus("small")
    with _corpus_env(corpus, bbf) as cctally:
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


def test_a_refresh_whose_early_build_rebuilt_codex_is_active():
    """Last-write would call this idle. It is not (spec §1.5, review P1-2).

    Drives the classifier directly with the two realised decisions a single
    refresh can produce, in the order that makes last-write wrong: an early
    build rebuilds, a later one reuses. Simulating the DECISIONS rather than
    contriving a corpus that produces them is deliberate — the corpus carries
    exactly one weekly cycle, so the disagreement cannot be provoked from data.

    It takes NO corpus fixture. It requested `small_corpus` and never read it,
    which forced the shared build for a test that touches no data at all.
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


def test_dispatch_counts_sum_to_completed_ticks(private_corpus):
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = private_corpus("small")
    for _ in range(3):
        _run_refresh(corpus, bbf, skip_sync=True)
    snap = ts.snapshot()
    counts = snap.dispatch_counts
    assert snap.tick_seq == 3
    assert counts["idle"] + counts["full"] + counts["degraded"] == snap.tick_seq


def test_a_warm_refresh_idles_and_does_not_touch_the_codex_leg(private_corpus):
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
    corpus = private_corpus("small")
    for _ in range(2):
        _run_refresh(corpus, bbf, skip_sync=True)
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
    kind, caller, monkeypatch, private_corpus
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

    with _corpus_env(private_corpus("small"), bbf):
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
    private_corpus
):
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    dash = _dashboard()
    with _corpus_env(private_corpus("small"), bbf):
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
    private_corpus, monkeypatch,
):
    """Constant overhead, asserted as a fixed call sequence (spec §7.1.1)."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()

    # BOTH halves of the >=10x pair are private: each arm drives a real
    # refresh, so a shared corpus would be written by the very comparison.
    small = private_corpus("small")
    tiny = private_corpus("tiny")
    small_rows = _provider_row_counts(small)
    tiny_rows = _provider_row_counts(tiny)
    assert small_rows["claude"] >= 10 * tiny_rows["claude"], (
        f"non-vacuity: the pair must differ by >=10x on Claude rows, got "
        f"{small_rows['claude']} vs {tiny_rows['claude']}")
    assert small_rows["codex"] >= 10 * tiny_rows["codex"], (
        f"non-vacuity: the pair must differ by >=10x on Codex rows, got "
        f"{small_rows['codex']} vs {tiny_rows['codex']}")

    sequences = {}
    for label, corpus in (("tiny", tiny), ("small", small)):
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
    private_corpus, monkeypatch,
):
    """Spec §7.1.2. The instrument must add no query and change no plan."""
    import _lib_tick_stats as ts
    bbf = _load_build_bench()
    ts.reset_for_tests()
    corpus = private_corpus("small")

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
            _run_refresh(corpus, bbf, skip_sync=True, before=before)
        return statements

    _run_refresh(corpus, bbf, skip_sync=True)        # warm the memo
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
    """A trusted frontier skips, targets an append, and fails safe on structure.

    A REDERIVED private corpus, not a plain copy (#741). This test touches the
    directory holding a tracked source file to provoke `filesystem_changed`,
    and a plain copy keeps the absolute paths of the tree it was copied FROM —
    so the mutation landed in the session-shared corpus, changing a directory
    identity for every other worker. That is #721's mechanism, performed
    deliberately. The write detector now refuses it.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
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
            assert state.seed_provider(
                "claude", conn, roots=claude_roots), _seed_detail(state)
            assert state.seed_provider(
                "codex", conn, roots=codex_roots), _seed_detail(state)

            _assert_plan(state.plan_provider(
                "claude", conn, roots=claude_roots,
            ), mode="caught_up")
            frontier.record_activity(app_dir, "claude", claude_path)
            targeted = state.plan_provider(
                "claude", conn, roots=claude_roots,
            )
            assert targeted.mode == "targeted", _plan_detail(targeted)
            assert targeted.paths == frozenset({claude_path})

            pathlib.Path(claude_path).parent.touch()
            _assert_plan(state.plan_provider(
                "claude", conn, roots=claude_roots,
            ), mode="full", reason="filesystem_changed")

            bounded = dict(state.memory_stats())
            assert bounded["entryCount"] == 2
            assert bounded["estimatedBytes"] <= bounded["maxBytes"]
            monkeypatch.setattr(frontier, "FRONTIER_MAX_BYTES", 1)
            assert not state.seed_provider(
                "claude", conn, roots=claude_roots), _seed_detail(state)
            assert state.last_seed_failure["claude"] == "memory_budget"
            _assert_plan(state.plan_provider(
                "claude", conn, roots=claude_roots), mode="full", reason="unseeded")
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
            ), _seed_detail(state)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="full", reason="unseeded")
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="caught_up")

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
            assert plan.mode == "full", _plan_detail(plan)
            assert plan.reason == "marker_replaced", _plan_detail(plan)
        finally:
            conn.close()


def test_caught_up_frontier_never_requeries_the_session_file_estate(
    small_corpus, tmp_path, monkeypatch,
):
    """The fast-negative may stat its saved directories, not walk DB paths."""
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
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caught-up path requeried every source path")

            monkeypatch.setattr(frontier, "_source_paths", forbidden)
            statements = []
            conn.set_trace_callback(statements.append)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "caught_up", _plan_detail(plan)
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="caught_up")

            frontier.record_activity(app_dir, "claude", source_path)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted", _plan_detail(plan)
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caught-up path queried transcript estate")

            monkeypatch.setattr(frontier, "_conversation_source_paths", forbidden)
            statements = []
            conn.set_trace_callback(statements.append)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "caught_up", _plan_detail(plan)
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_rebuild_claude_pending", "1"),
            )
            conn.commit()
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="full", reason="maintenance_changed")
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
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="full", reason="cursor_gap")
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
            assert state.seed_provider(provider, conn, roots=roots), _seed_detail(state)

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
            assert plan.mode == "full", _plan_detail(plan)
            assert plan.reason == "source_replaced", _plan_detail(plan)
        finally:
            conn.close()


def test_frontier_can_certificate_a_stably_missing_tracked_directory(
    small_corpus, tmp_path,
):
    """A deletion already observed by the full seed remains a stable guard."""
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
            missing_root = tmp_path / "already-removed-source-root"
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider(
                "claude", conn, roots=(missing_root,)
            ), _seed_detail(state)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=(missing_root,)
            ), mode="caught_up")
        finally:
            conn.close()


def test_ingest_frontier_database_replacement_and_bad_marker_fail_full(
    small_corpus, tmp_path,
):
    """Replacement and ambiguous activity can never certify caught-up state."""
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)
            marker = frontier.activity_marker_path(app_dir)
            with marker.open("ab") as fh:
                fh.write(b"not-json\n")
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="full", reason="malformed")
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
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="full", reason="database_replaced")
        finally:
            conn.close()


def test_ambiguous_activity_forces_one_full_pass_then_can_reseed(
    small_corpus, tmp_path,
):
    """A pathless hook event is fail-safe without poisoning every later tick."""
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
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            assert frontier.record_activity(app_dir, "claude", "")
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "full", _plan_detail(plan)
            assert plan.reason == "ambiguous_activity", _plan_detail(plan)

            state.commit_provider(plan, conn, roots=roots)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="caught_up")
        finally:
            conn.close()


def test_full_seed_preserves_activity_that_arrives_after_its_cutoff(
    small_corpus, tmp_path,
):
    """A ticket written during a full walk belongs to the following tick."""
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
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()

            # Models an append after the full walk passed this file but before
            # its successful result tried to mint a certificate.
            frontier.record_activity(app_dir, "claude", source_path)
            assert state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            ), _seed_detail(state)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted", _plan_detail(plan)
            assert plan.paths == frozenset({source_path})
        finally:
            conn.close()


def test_full_seed_preserves_first_activity_when_marker_was_absent(
    small_corpus, tmp_path,
):
    """An initially absent marker still has a pre-walk zero-byte cutoff."""
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
            if marker.exists():
                marker.unlink()
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()
            assert cutoff is not None
            assert cutoff.end == 0

            frontier.record_activity(app_dir, "claude", source_path)
            assert state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            ), _seed_detail(state)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "targeted", _plan_detail(plan)
            assert plan.paths == frozenset({source_path})
        finally:
            conn.close()


def test_full_seed_rejects_a_malformed_marker_prefix(small_corpus, tmp_path):
    """A full pass cannot certify over malformed pre-cutoff evidence."""
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
            frontier.record_activity(app_dir, "claude", source_path)
            marker = frontier.activity_marker_path(app_dir)
            marker.write_bytes(b"not-json\n" + marker.read_bytes())
            state = frontier.DashboardIngestFrontier(app_dir)
            cutoff = state.capture_cutoff()

            assert not state.seed_provider(
                "claude", conn, roots=roots, cutoff=cutoff,
            ), _seed_detail(state)
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
            assert state.seed_provider(provider, conn, roots=roots), _seed_detail(state)

            conn.execute(
                "DELETE FROM cache_meta WHERE key=?", (complete_key,)
            )
            conn.commit()
            plan = state.plan_provider(provider, conn, roots=roots)
            assert plan.mode == "full", _plan_detail(plan)
            assert plan.reason == "incomplete_store", _plan_detail(plan)
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
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)
            frontier.record_activity(app_dir, "claude", invalid)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == "full", _plan_detail(plan)
            assert plan.reason == "target_outside_scope", _plan_detail(plan)
        finally:
            conn.close()


def test_ingest_frontier_hook_maintenance_and_cursor_changes_fail_full(
    small_corpus, tmp_path,
):
    """Every cheap source-of-truth guard invalidates before a provider skip."""
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
            guard = app_dir / "synthetic-hook-settings.json"
            guard.write_text("{}")
            frontier.record_activity(app_dir, "claude", source_path)
            state = frontier.DashboardIngestFrontier(app_dir)

            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), _seed_detail(state)
            guard.write_text('{"changed":true}')
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), reason="hook_config_changed")

            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), _seed_detail(state)
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_backfill_pending", "1"),
            )
            conn.commit()
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), mode="full", reason="maintenance_changed")

            conn.execute(
                "DELETE FROM cache_meta WHERE key='conversation_backfill_pending'"
            )
            conn.commit()
            assert state.seed_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), _seed_detail(state)
            conn.execute(
                "UPDATE session_files SET last_byte_offset=size_bytes+1 "
                "WHERE path=?",
                (source_path,),
            )
            conn.commit()
            frontier.record_activity(app_dir, "claude", source_path)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots, guard_paths=(guard,),
            ), reason="cursor_gap")
        finally:
            conn.close()


def test_frontier_refuses_to_seed_while_maintenance_is_pending(
    small_corpus, tmp_path,
):
    """An already-present repair marker cannot become certified normal."""
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
            frontier.record_activity(app_dir, "claude", source_path)
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                ("conversation_backfill_pending", "1"),
            )
            conn.commit()
            state = frontier.DashboardIngestFrontier(app_dir)
            assert not state.seed_provider(
                "claude", conn, roots=roots), _seed_detail(state)
            assert state.last_seed_failure["claude"] == "maintenance_pending"

            conn.execute(
                "DELETE FROM cache_meta WHERE key='conversation_backfill_pending'"
            )
            conn.commit()
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)
        finally:
            conn.close()


class _FrozenClock:
    """A monotonic clock a test advances deliberately."""

    def __init__(self, start=1_000.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


_AGE_BOUND_FRONTIERS = [
    ("DashboardIngestFrontier", "open_cache_db", "session_files"),
    (
        "ConversationSyncFrontier",
        "open_conversations_db",
        "conversation_source_files",
    ),
]


@pytest.mark.parametrize(
    "frontier_class,open_db,source_table", _AGE_BOUND_FRONTIERS)
def test_an_unchanged_certificate_expires_once_it_reaches_its_maximum_age(
    small_corpus, tmp_path, monkeypatch, frontier_class, open_db, source_table,
):
    """Staleness is bounded by elapsed time, not only by writer-supplied evidence.

    Every other guard compares evidence that some writer must produce: a hook
    ticket, a directory mtime, a schema version, a cursor.  When the hook that
    writes tickets never runs at all, appending to an already-tracked source
    file moves none of them, so the certificate stays authoritative for as long
    as the process lives.  The age bound is the only check that fires without
    any writer's cooperation, so it is what makes worst-case staleness finite.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    clock = _FrozenClock()
    monkeypatch.setattr(frontier, "_now", clock)
    with _corpus_env(corpus, bbf) as cctally:
        conn = getattr(cctally, open_db)()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {source_table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            state = getattr(frontier, frontier_class)(app_dir)
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            bound = frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS
            clock.advance(bound * 0.5)
            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="caught_up", note=(
                "a certificate inside its age bound must still skip the walk, "
                "or the bound has destroyed the fast negative outright"))

            clock.advance(bound * 0.5)
            expired = state.plan_provider("claude", conn, roots=roots)
            assert expired.mode == "full", _plan_detail(expired)
            assert expired.reason == "certificate_expired", _plan_detail(expired)
        finally:
            conn.close()


@pytest.mark.parametrize(
    "frontier_class,open_db,source_table", _AGE_BOUND_FRONTIERS)
@pytest.mark.parametrize("commit_mode", ["caught_up", "targeted"])
def test_committing_a_non_full_plan_never_postpones_the_age_bound(
    small_corpus, tmp_path, monkeypatch,
    frontier_class, open_db, source_table, commit_mode,
):
    """Only an exhaustive walk may restart the clock.

    A caught-up commit advances the marker cursor, and a targeted commit also
    refreshes the cheap guards, but neither one reads any source file the
    tickets did not name.  Restarting the age on either would let a steady tick
    rate, or a steady ticket stream over one busy file, defer the exhaustive
    walk forever -- reinstating exactly the unbounded staleness this bound
    exists to remove.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    clock = _FrozenClock()
    monkeypatch.setattr(frontier, "_now", clock)
    with _corpus_env(corpus, bbf) as cctally:
        conn = getattr(cctally, open_db)()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {source_table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            state = getattr(frontier, frontier_class)(app_dir)
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            half = frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS / 2.0
            clock.advance(half)
            if commit_mode == "targeted":
                assert frontier.record_activity(app_dir, "claude", source_path)
            plan = state.plan_provider("claude", conn, roots=roots)
            assert plan.mode == commit_mode, _plan_detail(plan)
            state.commit_provider(plan, conn, roots=roots)

            clock.advance(half)
            expired = state.plan_provider("claude", conn, roots=roots)
            assert expired.mode == "full", _plan_detail(
                expired,
                f"a committed {commit_mode} plan restarted the age bound")
            assert expired.reason == "certificate_expired", _plan_detail(expired)
        finally:
            conn.close()


@pytest.mark.parametrize(
    "frontier_class,open_db,source_table", _AGE_BOUND_FRONTIERS)
def test_the_full_pass_an_expired_certificate_forces_restarts_the_age_bound(
    small_corpus, tmp_path, monkeypatch, frontier_class, open_db, source_table,
):
    """Expiry costs one walk, not the fast negative itself.

    Committing the forced full plan reseeds the provider, so the very next tick
    is cheap again.  Without this the bound would degrade into an unconditional
    full pass on every tick once the first certificate aged out.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    corpus = _private_frontier_corpus(small_corpus, tmp_path, bbf)
    clock = _FrozenClock()
    monkeypatch.setattr(frontier, "_now", clock)
    with _corpus_env(corpus, bbf) as cctally:
        conn = getattr(cctally, open_db)()
        try:
            source_path = conn.execute(
                f"SELECT path FROM {source_table} WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            app_dir = pathlib.Path(corpus)
            state = getattr(frontier, frontier_class)(app_dir)
            assert state.seed_provider("claude", conn, roots=roots), _seed_detail(state)

            clock.advance(frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS)
            forced = state.plan_provider("claude", conn, roots=roots)
            assert forced.mode == "full", _plan_detail(forced)
            state.commit_provider(forced, conn, roots=roots)

            _assert_plan(state.plan_provider(
                "claude", conn, roots=roots,
            ), mode="caught_up")
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
        assert probe.mode == "caught_up", _plan_detail(probe, (
            probe.reason, locked._ingest_frontier.last_seed_failure,
        ))
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


def test_the_environment_is_unchanged_after_a_gate_that_pins_it(private_corpus):
    """Spec §7.5. `_pin_env` deliberately leaves the process pinned, which is
    right for the builder and wrong for a gate: a leaked override wins over a
    later test's HOME-based resolution and points APP_DIR at a deleted scratch
    directory. Every gate in this file goes through `pinned_env`, which
    restores all four axes — absence restored AS absence."""
    import os
    bbf = _load_build_bench()
    corpus = private_corpus("small")
    before = {key: os.environ.get(key) for key in bbf.PINNED_ENV_KEYS}
    _run_refresh(corpus, bbf, skip_sync=True)
    after = {key: os.environ.get(key) for key in bbf.PINNED_ENV_KEYS}
    assert after == before, (
        f"the refresh left the environment changed: "
        f"{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }")


def test_the_owner_thread_tripwire_stays_armed_across_a_tick(private_corpus):
    """Preserve 11: the tick boundary opens AFTER `mark_owner_thread`, so the
    thread holding `sync_lock` for this rebuild still owns the accelerator
    caches and a lock-bypassing foreign-thread mutation is still caught."""
    import threading
    bbf = _load_build_bench()
    with _corpus_env(private_corpus("small"), bbf) as cctally:
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


def test_the_published_envelope_is_unchanged_by_the_recorder(private_corpus,
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
    corpus = private_corpus("small")

    def build(null_recorder):
        with monkeypatch.context() as mp:
            with _corpus_env(corpus, bbf) as cctally:
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


def test_a_refresh_applies_a_pending_trace_request(private_corpus):
    """Acceptance item 3, end to end: the POST records, the TICK applies.

    The mailbox and the endpoint are covered elsewhere. This is the missing
    link between them — that `_make_run_sync_now_locked` consumes the request
    at its authoritative-build boundary, so `--trace on` reaches a running
    process without a restart.
    """
    import _lib_perf as perf
    bbf = _load_build_bench()
    corpus = private_corpus("small")
    saved = perf.enabled()
    try:
        perf.set_enabled(False)
        perf.request_enabled(True)
        assert perf.enabled() is False, (
            "precondition: the request must not have flipped anything yet")
        _run_refresh(corpus, bbf, skip_sync=True)
        assert perf.enabled() is True, (
            "the refresh did not consume the pending trace request")
        assert perf.pending_state() == (True, True)

        perf.request_enabled(False)
        assert perf.enabled() is True, "still armed until the next build"
        _run_refresh(corpus, bbf, skip_sync=True)
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


# ── #740/#741/#721: the isolation the tests above depend on ────────────────


def _load_bench_runner():
    """Path-load `bin/cctally-bench`; a plain import cannot find a hyphen."""
    path = BIN / "cctally-bench"
    loader = importlib.machinery.SourceFileLoader("cctally_bench", str(path))
    spec = importlib.util.spec_from_loader("cctally_bench", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@contextlib.contextmanager
def _restored_process_pins(bbf):
    """Undo the environment `bin/cctally-bench` deliberately leaves pinned.

    `run_all` pins `CCTALLY_DATA_DIR`, `CLAUDE_CONFIG_DIR` and friends and does
    not restore them, which is correct for the tool and wrong inside a test:
    the next item on this worker would resolve user state through a scratch
    directory that no longer exists.
    """
    keys = (*bbf.PINNED_ENV_KEYS, "CCTALLY_AS_OF")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import _cctally_core

        _cctally_core._init_paths_from_env()


def test_a_benchmark_run_leaves_the_certificate_age_bound_intact(
    tiny_corpus, tmp_path, monkeypatch,
):
    """#740's RED/GREEN pair, deterministic and in ONE process.

    `bin/cctally-bench` suspends `FRONTIER_CERTIFICATE_MAX_AGE_SECONDS` for the
    length of a measurement run by assigning `float("inf")` onto the shared
    `_lib_ingest_frontier` module object — the same object `_load_sibling`
    registers in `sys.modules` and this test imports. Before the suspension was
    scoped, `run_all` returned with `inf` still standing, and the next
    certificate test on the same xdist worker computed
    `clock.advance(inf * 0.5)`, reached `inf - seeded_at >= inf`, and failed
    with `assert 'full' == 'caught_up'` at half the nominal age.

    The emergent form of that failure depends on xdist placement, which is why
    two full-suite reproductions of it passed. This one does not depend on
    placement: it runs the benchmark path and then the age-bound logic in one
    process, in order, and it fails on the pre-fix tree.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    bench = _load_bench_runner()
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == 120.0, (
        "precondition: something before this test already leaked the bound")

    with _restored_process_pins(bbf):
        bench.run_all(scale="tiny", seed=42, iterations=1, trace=False,
                      root=tmp_path / "bench")

    # The module the bench mutated IS this module: `_load_sibling` shares it
    # through `sys.modules`, which is the whole mechanism of the leak.
    assert sys.modules["_lib_ingest_frontier"] is frontier
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == 120.0, (
        "run_all returned with the certificate age bound still suspended; "
        "every later certificate test on this worker now reads "
        f"{frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS!r} as its bound")

    # And now the logic that the leak silently broke, over a real corpus.
    corpus = _private_frontier_corpus(tiny_corpus, tmp_path, bbf)
    clock = _FrozenClock()
    monkeypatch.setattr(frontier, "_now", clock)
    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_cache_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM session_files WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            roots = (pathlib.Path(source_path).parent,)
            state = frontier.DashboardIngestFrontier(pathlib.Path(corpus))
            assert state.seed_provider(
                "claude", conn, roots=roots), _seed_detail(state)

            bound = frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS
            clock.advance(bound * 0.5)
            _assert_plan(
                state.plan_provider("claude", conn, roots=roots),
                mode="caught_up",
                note="half the age bound expired the certificate, which is "
                     "what a leaked `inf` bound does: inf * 0.5 is inf, and "
                     "inf - seeded_at >= inf holds")
            clock.advance(bound * 0.5)
            _assert_plan(
                state.plan_provider("claude", conn, roots=roots),
                mode="full", reason="certificate_expired")
        finally:
            conn.close()


# ── criterion 5: the private-copy rule, checked over the module itself ─────

#: The session-scoped fixtures. A test may name one, but may only hand it to a
#: copier — never open it, tick over it, or pass it to a helper that does.
#: `shared_corpus` is the builder itself and is listed for the same reason: it
#: is a live fixture that another module takes directly, and a test here that
#: took it would reach every built scale without copying any of them.
_SHARED_CORPUS_FIXTURES = ("small_corpus", "tiny_corpus", "shared_corpus")

#: The helpers that take a private copy before anything else touches it.
#: `_copy_in_child` belongs here for the same reason as the other two: it
#: hands the shared path to a process whose only job is to copy it.
_CORPUS_COPIERS = (
    "_private_corpus", "_private_frontier_corpus", "_copy_in_child",
)


def _shared_corpus_misuses(source):
    """Every read of a shared-corpus fixture this module does not copy first.

    An AST rule rather than a list of test names, because a list of names is
    correct only until the next test is written and then silently stops being
    the rule it claims to be.
    """
    problems = []
    for node in ast.walk(ast.parse(source)):
        # `AsyncFunctionDef` is a sibling of `FunctionDef`, not a subclass, so
        # a matcher naming only the latter lets `async def test_x(small_corpus)`
        # through without examining it at all.
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        # All three parameter lists: pytest injects a fixture into a
        # positional-only or a keyword-only parameter exactly as it does into an
        # ordinary one, so reading `args.args` alone exempts both spellings.
        declared = {
            arg.arg
            for arg in (node.args.posonlyargs + node.args.args
                        + node.args.kwonlyargs)
        } & set(_SHARED_CORPUS_FIXTURES)
        if not declared:
            continue
        copied = set()
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in _CORPUS_COPIERS):
                # Keywords as well as positionals: `_private_corpus(
                # data_dir=small_corpus, tmp_path=tmp_path)` is a legitimate
                # copy, and reading `call.args` alone reported it as a direct
                # use — a false positive that blocks a correctly written test.
                for argument in list(call.args) + [
                        keyword.value for keyword in call.keywords]:
                    if isinstance(argument, ast.Name):
                        copied.add(id(argument))
        reads = [
            name for name in ast.walk(node)
            if isinstance(name, ast.Name)
            and isinstance(name.ctx, ast.Load)
            and name.id in declared
        ]
        if not reads:
            problems.append(
                f"{node.name} declares {sorted(declared)} and never reads it, "
                f"so it pays for the shared build and uses nothing")
        for name in reads:
            if id(name) not in copied:
                problems.append(
                    f"{node.name} (line {name.lineno}) uses {name.id} "
                    f"directly; hand it to one of {list(_CORPUS_COPIERS)} or "
                    f"take the `private_corpus` fixture instead")
    return problems


def test_no_test_here_uses_the_shared_corpus_without_copying_it():
    """#741: a writer into the session-scoped corpus is a cross-test hazard.

    Twelve tests here drove real refreshes and builds straight against the
    shared corpus. That is the same hazard class as #721's copied cursors, and
    it is what made #721 fire: whichever frontier certificate happened to be
    seeded over a directory another worker wrote to degraded to `full`.
    """
    problems = _shared_corpus_misuses(
        pathlib.Path(__file__).read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


def test_the_private_copy_rule_reports_a_direct_user():
    """Non-vacuity: the rule above must be able to fail."""
    offender = textwrap.dedent('''
        def test_writes_in_place(small_corpus):
            _run_refresh(small_corpus, bbf)
    ''')
    assert _shared_corpus_misuses(offender), (
        "the checker accepted a test that drives a refresh straight against "
        "the shared corpus, so its silence over this module proves nothing")

    unused = textwrap.dedent('''
        def test_declares_and_ignores(small_corpus):
            assert True
    ''')
    assert _shared_corpus_misuses(unused)

    accepted = textwrap.dedent('''
        def test_copies_first(small_corpus, tmp_path):
            corpus = _private_corpus(small_corpus, tmp_path)
            _run_refresh(corpus, bbf)
    ''')
    assert _shared_corpus_misuses(accepted) == []

    # A keyword-only declaration. pytest injects into it exactly as it does
    # into an ordinary parameter, and `node.args.args` does not contain it.
    keyword_only = textwrap.dedent('''
        def test_keyword_only(*, small_corpus):
            _run_refresh(small_corpus, bbf)
    ''')
    assert _shared_corpus_misuses(keyword_only), (
        "a keyword-only fixture declaration escaped the rule entirely")

    # `AsyncFunctionDef` is a sibling of `FunctionDef`, not a subclass.
    asynchronous = textwrap.dedent('''
        async def test_async(small_corpus):
            _run_refresh(small_corpus, bbf)
    ''')
    assert _shared_corpus_misuses(asynchronous), (
        "an async test was never examined at all")

    # `shared_corpus` is the builder itself: a test taking it reaches every
    # built scale, and no test in this module may.
    builder = textwrap.dedent('''
        def test_takes_the_builder(shared_corpus):
            _run_refresh(shared_corpus("small"), bbf)
    ''')
    assert _shared_corpus_misuses(builder), (
        "the builder fixture was not in the guarded set, so a test could take "
        "every scale uncopied")

    # The false positive: a copy whose source is passed by KEYWORD.
    keyword_copy = textwrap.dedent('''
        def test_copies_by_keyword(small_corpus, tmp_path):
            corpus = _private_corpus(data_dir=small_corpus, tmp_path=tmp_path)
            _run_refresh(corpus, bbf)
    ''')
    assert _shared_corpus_misuses(keyword_copy) == [], (
        "a legitimate copy taken through keyword arguments was reported as a "
        "direct use, which would block a correctly written test")


#: Receivers a `.mode` or `.reason` assertion may be read off. `Name` is
#: `plan.mode`, `Call` is `state.plan_provider(...).mode`, `Attribute` is
#: `result.plan.mode` and `Subscript` is `plans["claude"].mode`. All four are
#: the same assertion and all four report `full` and nothing else.
_PLAN_RECEIVERS = (ast.Name, ast.Call, ast.Attribute, ast.Subscript)


def _is_seed_provider_call(node):
    """A ``<anything>.seed_provider(...)`` call, whatever the receiver is."""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "seed_provider")


def _message_helper_calls(msg):
    """The helper names an assertion message actually CALLS.

    Substring-matching ``ast.dump(node.msg)`` accepted the string literal
    ``"see _plan_detail for why"`` as a report, which is the one shape a
    message check has to reject: prose naming the helper is not the helper.
    """
    if msg is None:
        return frozenset()
    names = set()
    for node in ast.walk(msg):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return frozenset(names)


def _seed_bound_names(func):
    """Names ``func`` assigns from a ``seed_provider`` call.

    ``ok = state.seed_provider(...)`` followed by ``assert ok`` is the same
    undiagnosable assertion written in two statements, so the rule carries the
    binding across them. Deliberately simple and intra-function: a plain
    assignment to a bare name, and nothing else.
    """
    bound = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_seed_provider_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif (isinstance(node, ast.AnnAssign)
                and node.value is not None
                and _is_seed_provider_call(node.value)
                and isinstance(node.target, ast.Name)):
            bound.add(node.target.id)
    return bound


def _iter_asserts_with_bindings(node, bound=frozenset()):
    """Every ``assert`` under ``node``, with its enclosing function's bindings.

    A ``seed_provider`` call that is not inside an ``Assert`` is SETUP, not an
    assertion, and this yields nothing for it — the rule is about what an
    assertion reports when it fails.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _iter_asserts_with_bindings(
                child, frozenset(bound) | _seed_bound_names(child))
            continue
        if isinstance(child, ast.Assert):
            yield child, bound
        yield from _iter_asserts_with_bindings(child, bound)


def _asserted_operands(test):
    """One assertion's test, split into the expressions it actually asserts.

    ``assert plan.mode == "caught_up" and plan.reason is None`` asserts two
    things, and a rule that reads only the ``BoolOp`` examines neither.
    """
    if isinstance(test, ast.BoolOp):
        for value in test.values:
            yield from _asserted_operands(value)
        return
    if isinstance(test, ast.UnaryOp):
        yield test.operand
        return
    yield test


def _undiagnosable_frontier_assertions(source):
    """Every plan or seed assertion here that would report `full` and nothing else.

    `plan_provider` refuses for eleven distinct reasons and every one of them
    reads as the single word `full`; `seed_provider` returns a bare boolean and
    records its reason out of band. An assertion that reports neither cannot be
    diagnosed from a retained log, which is why no past failure of this module
    could be attributed after the fact.
    """
    problems = []
    for node, seed_bound in _iter_asserts_with_bindings(ast.parse(source)):
        reported = _message_helper_calls(node.msg)
        for operand in _asserted_operands(node.test):
            if (_is_seed_provider_call(operand)
                    or (isinstance(operand, ast.Compare)
                        and _is_seed_provider_call(operand.left))
                    or (isinstance(operand, ast.Name)
                        and operand.id in seed_bound)):
                if "_seed_detail" not in reported:
                    problems.append(
                        f"line {node.lineno}: a seed_provider assertion that "
                        f"does not report last_seed_failure")
                continue
            if (isinstance(operand, ast.Compare)
                    and isinstance(operand.left, ast.Attribute)
                    and operand.left.attr in ("mode", "reason")
                    and isinstance(operand.left.value, _PLAN_RECEIVERS)):
                if "_plan_detail" not in reported:
                    problems.append(
                        f"line {node.lineno}: a plan.{operand.left.attr} "
                        f"assertion that does not report the plan's reason")
    return problems


def test_every_frontier_assertion_reports_why_the_plan_refused():
    """#740 D2. Diagnosability is asserted, not left to each author's habit."""
    problems = _undiagnosable_frontier_assertions(
        pathlib.Path(__file__).read_text(encoding="utf-8"))
    assert problems == [], "\n".join(problems)


def test_the_diagnosability_check_reports_a_bare_assertion():
    """Non-vacuity: the rule above must be able to fail."""
    bare = textwrap.dedent('''
        def test_bare():
            assert plan.mode == "caught_up"
            assert state.seed_provider("claude", conn, roots=roots)
    ''')
    assert len(_undiagnosable_frontier_assertions(bare)) == 2

    reported = textwrap.dedent('''
        def test_reported():
            assert plan.mode == "caught_up", _plan_detail(plan)
            assert state.seed_provider(
                "claude", conn, roots=roots), _seed_detail(state)
    ''')
    assert _undiagnosable_frontier_assertions(reported) == []


#: The six shapes the first form of this rule returned NO finding for. Each is
#: an assertion that reports `full`, or a bare `False`, and nothing else. Named
#: rather than folded into one blob so a regression says which shape came back.
_UNDIAGNOSABLE_SHAPES = {
    "attribute_receiver": ('''
        def test_attribute_receiver():
            assert state.plan.mode == "caught_up"
    ''', 1),
    "subscript_receiver": ('''
        def test_subscript_receiver():
            assert plans["a"].mode == "caught_up"
    ''', 1),
    "prose_naming_the_helper": ('''
        def test_prose_naming_the_helper():
            assert plan.mode == "caught_up", "see _plan_detail for why"
    ''', 1),
    "seed_inside_a_comparison": ('''
        def test_seed_inside_a_comparison():
            assert state.seed_provider("c", conn) is True
    ''', 1),
    "boolean_conjunction": ('''
        def test_boolean_conjunction():
            assert plan.mode == "caught_up" and plan.reason is None
    ''', 2),
    "seed_bound_to_a_name": ('''
        def test_seed_bound_to_a_name():
            ok = state.seed_provider("c", conn)
            assert ok
    ''', 1),
}

#: The same six, written the way this module writes them.
_DIAGNOSABLE_SHAPES = {
    "attribute_receiver": '''
        def test_attribute_receiver():
            assert state.plan.mode == "caught_up", _plan_detail(state.plan)
    ''',
    "subscript_receiver": '''
        def test_subscript_receiver():
            assert plans["a"].mode == "caught_up", _plan_detail(plans["a"])
    ''',
    "prose_naming_the_helper": '''
        def test_prose_naming_the_helper():
            assert plan.mode == "caught_up", _plan_detail(plan, "why")
    ''',
    "seed_inside_a_comparison": '''
        def test_seed_inside_a_comparison():
            assert state.seed_provider("c", conn) is True, _seed_detail(state)
    ''',
    "boolean_conjunction": '''
        def test_boolean_conjunction():
            assert (plan.mode == "caught_up"
                    and plan.reason is None), _plan_detail(plan)
    ''',
    "seed_bound_to_a_name": '''
        def test_seed_bound_to_a_name():
            ok = state.seed_provider("c", conn)
            assert ok, _seed_detail(state)
    ''',
}


@pytest.mark.parametrize("shape", sorted(_UNDIAGNOSABLE_SHAPES))
def test_the_diagnosability_check_reports_every_evaded_shape(shape):
    """Each shape must be REPORTED, and its repaired twin ACCEPTED.

    Every one of these returned no finding from the first form of the rule, so
    a guard that reads clean over this module proved nothing about them. The
    accepted half is asserted alongside, because a rule that reports both forms
    would block the repair it exists to require.
    """
    source, expected = _UNDIAGNOSABLE_SHAPES[shape]
    found = _undiagnosable_frontier_assertions(textwrap.dedent(source))
    assert len(found) == expected, (
        f"{shape} produced {found}, not {expected} finding(s)")

    repaired = _undiagnosable_frontier_assertions(
        textwrap.dedent(_DIAGNOSABLE_SHAPES[shape]))
    assert repaired == [], (
        f"{shape} written correctly was still reported: {repaired}")


def test_a_seed_call_outside_an_assertion_is_setup_and_not_reported():
    """The rule is about assertions, and this module seeds as setup too.

    A `seed_provider` call in an expression statement, or bound to a name that
    is never asserted, arranges the state a later assertion examines. Reporting
    it would demand a diagnostic message on a statement that cannot fail.
    """
    setup = textwrap.dedent('''
        def test_seeds_as_setup():
            state.seed_provider("claude", conn, roots=roots)
            ok = state.seed_provider("codex", conn, roots=roots)
            assert plan.mode == "caught_up", _plan_detail(plan)
    ''')
    assert _undiagnosable_frontier_assertions(setup) == []


# ── criterion 6: the copier waits on the corpus build lock ─────────────────

_COPIER_CHILD = textwrap.dedent('''
    """Copy the shared corpus through the ONE copier, from another process."""
    import pathlib
    import sys

    sys.path.insert(0, {tests_dir!r})
    from _shared_corpus import copy_shared_corpus

    data_dir, destination, started, done = sys.argv[1:5]
    # Written immediately before the call, so the parent can distinguish a
    # child that is BLOCKED from one that has not started yet.
    pathlib.Path(started).write_text("started\\n", encoding="utf-8")
    copy_shared_corpus(data_dir, destination)
    pathlib.Path(done).write_text("done\\n", encoding="utf-8")
''')


def _copy_in_child(data_dir, destination, *, scratch, started, done):
    """Launch another process that copies `data_dir` through the ONE copier.

    Another PROCESS, because an exclusive flock is held per open file
    description: a same-process probe would simply be granted the lock this
    test is holding, and would prove nothing.
    """
    script = pathlib.Path(scratch) / "copy_child.py"
    script.write_text(
        _COPIER_CHILD.format(tests_dir=str(pathlib.Path(__file__).parent)),
        encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(script), str(data_dir), str(destination),
         str(started), str(done)])


def test_a_copy_waits_while_the_corpus_build_lock_is_held(
    small_corpus, corpus_root, tmp_path,
):
    """The copier takes the build lock SHARED, so a rebuild excludes it.

    `build_fixture` clears the whole data directory before it re-emits, so a
    `copytree` racing that clear copies a half-deleted tree. Six sites took
    such a copy with no lock at all.

    Both halves are asserted: the copy does not complete while the lock is
    held, and it does complete once it is released.
    """
    destination = tmp_path / "child-copy"
    started = tmp_path / "started"
    done = tmp_path / "done"

    with open(corpus_lock_path(corpus_root, "small"), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        child = _copy_in_child(
            small_corpus, destination,
            scratch=tmp_path, started=started, done=done)
        try:
            deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
            while not started.exists() and time.monotonic() < deadline:
                assert child.poll() is None, (
                    f"the child exited ({child.returncode}) before it reached "
                    f"the copier at all")
                time.sleep(0.05)
            assert started.exists(), "the child never reached the copier"

            # The blocked half. This is a structural claim, not a timing one:
            # the child has started, has not exited, and cannot have copied,
            # because this process holds the same lock exclusively.
            for _ in range(20):
                time.sleep(0.05)
                assert child.poll() is None, (
                    f"the child exited ({child.returncode}) while the build "
                    f"lock was held exclusively")
                assert not done.exists(), (
                    "the copy completed while the build lock was held "
                    "exclusively, so the copier does not take that lock")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    assert child.wait(timeout=PRESENCE_BACKSTOP_SECONDS) == 0, (
        "the copier failed once the lock was released")
    assert done.exists(), "the copy never completed after the lock was freed"
    assert (destination / "data" / "cache.db").exists(), (
        "the released copy is not a usable corpus")


# ── criterion 10: a certificate is immune to writes into its SOURCE ────────


def test_a_private_certificate_ignores_a_write_into_the_corpus_it_copied(
    small_corpus, tmp_path,
):
    """#721's mechanism, proved on a directory the certificate actually covers.

    `plan_provider` stats every directory that holds a tracked source file and
    refuses with `filesystem_changed` when one of their identities moves. A
    certificate whose roots still aimed at the shared corpus therefore degraded
    to `full` the moment any other worker wrote there — and the victim was
    never the writer.

    The mutation lands on a directory that is IN the seeded certificate's own
    directory set, which is asserted rather than assumed. A write under `data/`
    would not be in that set at all, so the same test shape over `data/` would
    pass without exercising the guard once.

    `upstream` stands in for the session-shared corpus. The real one is not
    mutated here because writing into it is precisely the hazard under test,
    and the write detector now refuses it.
    """
    import _lib_ingest_frontier as frontier

    bbf = _load_build_bench()
    upstream = _private_corpus(small_corpus, tmp_path, name="upstream")
    corpus = _private_frontier_corpus(upstream, tmp_path, bbf, name="private")
    upstream_root = pathlib.Path(upstream).parent.resolve()
    private_root = pathlib.Path(corpus).parent.resolve()

    with _corpus_env(corpus, bbf) as cctally:
        conn = cctally.open_conversations_db()
        try:
            source_path = conn.execute(
                "SELECT path FROM conversation_source_files "
                "WHERE path LIKE '/%' LIMIT 1"
            ).fetchone()[0]
            private_dir = pathlib.Path(source_path).parent
            roots = (private_dir,)
            state = frontier.ConversationSyncFrontier(pathlib.Path(corpus))
            assert state.seed_provider(
                "claude", conn, roots=roots), _seed_detail(state)

            covered = dict(state._states["claude"].directory_identity)
            assert covered, "the certificate covers no directory at all"
            # Leg 3: the seed's roots are private.
            outside = [
                raw for raw in covered
                if not pathlib.Path(raw).resolve().is_relative_to(private_root)
            ]
            assert not outside, (
                f"the certificate covers directories outside the private "
                f"corpus {private_root}: {outside[:3]}")
            assert str(private_dir) in covered, (
                "the directory this test mutates is not in the certificate, "
                "so the mutation would prove nothing about filesystem_changed")

            upstream_dir = upstream_root / private_dir.resolve().relative_to(
                private_root)
            assert upstream_dir.is_dir(), upstream_dir

            shared_before = frontier._stat_identity(upstream_dir)
            private_before = frontier._stat_identity(private_dir)
            (upstream_dir / "another-worker-wrote-this.jsonl").write_text(
                "{}\n", encoding="utf-8")
            # Leg 1 and leg 2.
            assert frontier._stat_identity(upstream_dir) != shared_before, (
                "the write did not move the source tree's directory identity, "
                "so this test could not distinguish the two trees")
            assert frontier._stat_identity(private_dir) == private_before, (
                "the private copy's directory identity moved too")
            # Leg 4.
            _assert_plan(
                state.plan_provider("claude", conn, roots=roots),
                mode="caught_up",
                note="a write into the corpus this copy was taken from "
                     "degraded a certificate seeded over the copy")

            # Non-vacuity: the identical mutation on the PRIVATE side does
            # degrade the plan, so leg 4 is a real negative rather than a
            # guard that never fires.
            (private_dir / "this-worker-wrote-this.jsonl").write_text(
                "{}\n", encoding="utf-8")
            _assert_plan(
                state.plan_provider("claude", conn, roots=roots),
                mode="full", reason="filesystem_changed")
        finally:
            conn.close()
