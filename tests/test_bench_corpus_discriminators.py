"""The bench corpus must carry the properties the dashboard goldens cannot.

The 16 dashboard golden scenarios pin SHAPE at 71 and 11 session entries. This
corpus pins SCALE, and it is worthless for that purpose unless it also reaches
the multi-account Codex, colliding-basename and model-pool paths. Every
assertion here is a discriminator check, not a count check.
"""
import importlib.machinery
import importlib.util
import pathlib
import shutil
import sqlite3
import sys

import pytest

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


def _assert_every_discriminator(data_dir):
    """The three spec §4 discriminators, over one built corpus."""
    import _lib_codex_pools

    conn = sqlite3.connect(data_dir / "cache.db")
    try:
        accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT account_key FROM codex_session_entries "
            "WHERE account_key IS NOT NULL")]
        assert len(accounts) >= 2, f"need two real Codex accounts, got {accounts}"

        spend = list(conn.execute(
            "SELECT account_key, SUM(input_tokens + output_tokens) "
            "FROM codex_session_entries WHERE account_key IS NOT NULL "
            "GROUP BY account_key"))
        totals = {k: v for k, v in spend}
        assert len(set(totals.values())) == len(totals), (
            f"account spend must be unequal so a merge is visible: {totals}")

        quota_accounts = [r[0] for r in conn.execute(
            "SELECT DISTINCT account_key FROM quota_window_snapshots "
            "WHERE account_key IS NOT NULL")]
        assert len(quota_accounts) >= 2, (
            f"need quota under two accounts, got {quota_accounts}")
        quota_used = {k: v for k, v in conn.execute(
            "SELECT account_key, SUM(used_percent) FROM quota_window_snapshots "
            "WHERE account_key IS NOT NULL GROUP BY account_key")}
        assert len(set(quota_used.values())) == len(quota_used), (
            f"account quota must be unequal so a merge is visible: {quota_used}")

        # `quota_window_snapshots` has no `model_pool` column: the pool axis is
        # DERIVED by bin/_lib_codex_pools.py from two independent inputs, and
        # #373 requires each to fire on its own. So assert each axis separately
        # rather than counting distinct values of a column that does not exist.
        rows = list(conn.execute(
            "SELECT logical_limit_key, limit_name FROM quota_window_snapshots"))
        assert rows, "no quota windows at all"
        by_key = [r for r in rows
                  if _lib_codex_pools._key_has_model_pool(r[0])]
        by_name = [r for r in rows
                   if not _lib_codex_pools._key_has_model_pool(r[0])
                   and _lib_codex_pools.codex_model_scoped_quota_pool(r[1])]
        standard = [r for r in rows
                    if not _lib_codex_pools.is_model_scoped_codex_quota(r[0], r[1])]
        assert by_key, "no window carries a modelPool member in its limit key"
        assert by_name, "no window carries a Spark limit_name on its own"
        assert standard, "no account-level standard quota window"

        roots = [r[0] for r in conn.execute(
            "SELECT DISTINCT project_path FROM session_files "
            "WHERE project_path IS NOT NULL")]
        basenames = [r.rstrip("/").rsplit("/", 1)[-1] for r in roots]
        collided = [b for b in set(basenames) if basenames.count(b) >= 2]
        assert collided, (
            f"need two distinct roots sharing a basename, got {sorted(roots)}")
    finally:
        conn.close()


@pytest.mark.parametrize("scale", ["tiny", "small"])
def test_the_cheap_profiles_carry_every_discriminator(scale, shared_corpus):
    """REALISED, not declared. Both halves of the >=10x pair.

    `_emit_codex_corpus` is cardinality-dependent — the per-root session split,
    the quota-event division, and the `local_index % 4` pool selector all read
    the profile — so a profile can declare every discriminator and emit none.
    `large` is covered by the static declaration test below plus the receipt's
    own `validate_corpus` call, because building it here would cost minutes.
    """
    _assert_every_discriminator(shared_corpus(scale))


@pytest.mark.parametrize("scale", ["tiny", "small"])
def test_the_cheap_profiles_realise_exactly_the_counts_they_declare(
    scale, shared_corpus
):
    """`validate_corpus` is what the `large` receipt runs; run it here too."""
    bbf = _load_build_bench()
    conn = bbf.open_fixture_db(shared_corpus(scale))
    try:
        got = bbf.validate_corpus(conn, scale)
    finally:
        conn.close()
    assert got == bbf.expected_counts(bbf.SCALES[scale])


def test_the_pair_differs_by_at_least_ten_times_on_both_provider_axes():
    """Spec §7.1's precondition, asserted on the profiles rather than assumed.

    Implementor 2's row-count-invariance gate needs one tick over two corpora
    whose Claude AND Codex row counts differ by at least 10x. It must not have
    to touch SCALES to get that, so the property is pinned here.
    """
    bbf = _load_build_bench()
    tiny = bbf.expected_counts(bbf.SCALES["tiny"])
    small = bbf.expected_counts(bbf.SCALES["small"])
    for axis in ("entries", "codex_entries"):
        assert small[axis] >= 10 * tiny[axis], (
            f"{axis}: small={small[axis]} tiny={tiny[axis]} is only "
            f"{small[axis] / tiny[axis]:.1f}x, below the 10x the gate needs")


def test_the_shared_corpus_is_built_once_per_run(small_corpus, corpus_root):
    """The marker must already match, so a second call rebuilds nothing."""
    import fcntl

    from conftest import corpus_lock_path  # type: ignore

    bbf = _load_build_bench()

    marker = bbf._marker_path(small_corpus)
    assert marker.exists(), "the shared fixture must leave its marker"
    stamp = marker.stat().st_mtime_ns

    # Under the SAME flock the fixture takes. Rebuilding the shared root
    # outside it would be the very race the fixture exists to prevent.
    with open(corpus_lock_path(corpus_root, "small"), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            bbf.build_fixture_isolated(
                scale="small", seed=42, root=small_corpus.parent)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    assert marker.stat().st_mtime_ns == stamp, (
        "a second build rewrote the marker, so the corpus was not reused")


def test_large_declares_the_same_discriminators_as_the_cheap_profiles():
    """Every profile carries the same discriminators; only cardinality differs.

    Asserted statically rather than by building `large`, which takes 1m49s and
    1.2 GiB. A `pytest.skip` for the expensive profile is exactly the F28
    pattern this session exists to remove — a skip reads as non-failing — so
    the structural claim is asserted instead of waived. `large`'s REALISED
    discriminators are asserted by `validate_corpus`, which the receipt in
    `bench/baselines/backend.json` runs before it measures anything; the two
    cheap profiles are checked realised above.
    """
    bbf = _load_build_bench()
    discriminator_keys = {
        "codex_sessions", "codex_events_per_session", "codex_accounts",
        "quota_windows", "colliding_basename",
    }
    profiles = {name: bbf.SCALES[name] for name in ("tiny", "small", "large")}
    for name, params in profiles.items():
        assert discriminator_keys <= set(params), (
            name, sorted(discriminator_keys - set(params)))
        assert params["codex_accounts"] >= 2
        assert params["colliding_basename"] is True
        # Enough sessions per Codex root that BOTH pool axes can fire: the
        # `local_index % 4` selector reaches the Spark model at 1 and the Spark
        # limit_name at 2, so a root with fewer than three sessions carries one
        # axis at most.
        shares = bbf._codex_session_share(
            params["codex_sessions"], params["codex_accounts"])
        assert min(shares) >= 3, (name, shares)
    for key in ("codex_sessions", "codex_events_per_session", "quota_windows"):
        assert (profiles["large"][key] > profiles["small"][key]
                > profiles["tiny"][key]), (
            f"{key} must scale up across the three profiles, not merely exist")


# ── Task 4: the fingerprint must cover the new axes ───────────────────────


def _writable_fixture_db(data_dir):
    """conversations.db as main with cache.db attached READ-WRITE.

    `open_fixture_db` attaches the cache `mode=ro`, which is right for the
    benchmark and wrong here: this test has to mutate a row and re-hash. The
    attachment NAME must stay `cache_db`, because that is what `semantic_hash`
    qualifies its cache-side statements with.
    """
    conn = sqlite3.connect(pathlib.Path(data_dir) / "conversations.db")
    conn.execute("ATTACH DATABASE ? AS cache_db",
                 (str(pathlib.Path(data_dir) / "cache.db"),))
    return conn


@pytest.mark.parametrize("axis,mutation", [
    ("codex spend",
     "UPDATE cache_db.codex_session_entries SET output_tokens = output_tokens + 1 "
     "WHERE rowid = (SELECT MIN(rowid) FROM cache_db.codex_session_entries)"),
    ("account attribution",
     "UPDATE cache_db.codex_session_entries SET account_key = 'not-a-real-key' "
     "WHERE rowid = (SELECT MIN(rowid) FROM cache_db.codex_session_entries)"),
    ("quota usage",
     "UPDATE cache_db.quota_window_snapshots SET used_percent = used_percent + 1 "
     "WHERE rowid = (SELECT MIN(rowid) FROM cache_db.quota_window_snapshots)"),
    ("model pool",
     "UPDATE cache_db.quota_window_snapshots "
     "SET logical_limit_key = replace(logical_limit_key, '\"modelPool\"', '\"other\"') "
     "WHERE logical_limit_key LIKE '%modelPool%'"),
])
def test_semantic_hash_separates_codex_content(axis, mutation, tmp_path,
                                               small_corpus):
    """Two corpora differing only in Codex content must not share a hash.

    Mutates a COPY. The shared corpus is built once per run and read by every
    other gate, so a test that wrote to it in place would be handing the next
    test a different corpus than the one it was promised.
    """
    bbf = _load_build_bench()
    data_dir = tmp_path / "corpus"
    shutil.copytree(small_corpus, data_dir)

    conn = _writable_fixture_db(data_dir)
    try:
        before = bbf.semantic_hash(conn)
        assert conn.execute(mutation).rowcount > 0, (
            f"the {axis} mutation matched no row, so this proves nothing")
        after = bbf.semantic_hash(conn)
    finally:
        conn.close()
    assert before != after, (
        f"the fingerprint must cover {axis}, or the envelope oracle keyed on "
        "it proves nothing about the Codex path")


def test_a_profile_change_rebuilds_rather_than_appending(tmp_path):
    """A marker miss must REBUILD, not re-emit on top of the previous corpus.

    The generator names its rollout files deterministically, so a content-only
    change rewrites each one at the same path. If the new bytes are the same
    length the delta ingest sees no growth and skips the file, and the old rows
    survive in `cache.db`. Measured before the fix: bounding the Codex
    quota-reset spread changed only integers inside the rollouts, the
    `params_hash` correctly detected the change and triggered a re-emit, and
    the rebuilt `large` corpus still carried the OLD 2029-04-19 reset dates —
    so the receipt measured a corpus nobody had asked for. A marker that
    detects a change is worth nothing if the rebuild it triggers does not.
    """
    bbf = _load_build_bench()
    root = tmp_path / "rebuilt"

    first = bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
    conn = bbf.open_fixture_db(first)
    try:
        before = bbf.dataset_counts(conn)
    finally:
        conn.close()
    assert before == bbf.expected_counts(bbf.SCALES["tiny"])

    original = dict(bbf.SCALES["tiny"])
    try:
        bbf.SCALES["tiny"] = {**original,
                              "codex_sessions": original["codex_sessions"] - 4,
                              "sessions": original["sessions"] - 4}
        want = bbf.expected_counts(bbf.SCALES["tiny"])
        assert want != before, "precondition: the mutated profile must differ"
        second = bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
        conn = bbf.open_fixture_db(second)
        try:
            after = bbf.dataset_counts(conn)
        finally:
            conn.close()
    finally:
        bbf.SCALES["tiny"] = original

    assert after == want, (
        "the rebuild did not clear the previous corpus: expected "
        f"{want}, got {after}")


# ── The Codex weekly cycle must RESOLVE, or the expensive leg is never run ──


def _codex_source_state(data_dir, bbf):
    """Build a real snapshot over `data_dir` at the corpus clock.

    Returns the published `sources["codex"]` mapping. Pins all four provider
    axes at the corpus's own roots, because the source build reads them.
    """
    root = pathlib.Path(data_dir).parent
    codex_roots = sorted(p for p in root.glob("codex-*") if p.is_dir())
    with bbf.pinned_env(root / "data", root / "claude",
                        ",".join(str(p) for p in codex_roots), root / "home"):
        cctally = sys.modules["cctally"]
        snapshot = cctally._cctally_tui._tui_build_snapshot(
            now_utc=bbf.CORPUS_CLOCK_UTC,
            skip_sync=True,
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )
    return snapshot


@pytest.mark.parametrize("scale", ["tiny", "small"])
def test_the_corpus_resolves_a_live_codex_weekly_cycle(scale, shared_corpus):
    """The per-cycle accounting read must actually execute.

    `_resolve_codex_weekly_cycle` admits a boundary only when its canonical
    reset is strictly after `now`, and needs EXACTLY ONE live boundary per
    account across every slot. Measured before the fix: every account owned
    four live weekly boundaries at the frozen clock (`conflicting`) and none at
    a real clock (`missing`), so `sources["codex"]` published
    `availability="partial"` with a `codex_cycle_unavailable` warning and no
    hero, and `load_cached_rooted_codex_accounting_entries` — the read spec
    §6.1 names as 79% of a profiled build — was never called at any scale or
    any clock. A corpus that silently takes the short branch is the same
    failure class as the future-dated resets: caught, logged, invisible.
    """
    bbf = _load_build_bench()
    snapshot = _codex_source_state(shared_corpus(scale), bbf)
    bundle = snapshot.source_bundle
    assert bundle is not None, "the snapshot carries no source bundle"
    codex = (bundle.sources or {}).get("codex")
    assert codex is not None, "no Codex source in the snapshot at all"

    warnings = [getattr(w, "code", w)
                for w in (getattr(codex, "warnings", None) or [])]
    assert "codex_cycle_unavailable" not in warnings, (
        f"the weekly cycle did not resolve: warnings={warnings}")
    assert getattr(codex, "availability", None) == "ok", (
        f"availability={getattr(codex, 'availability', None)!r} "
        f"warnings={warnings}")
    hero = (getattr(codex, "data", None) or {}).get("hero")
    assert hero, "no hero in the Codex source data"
    assert hero.get("cycle"), "the hero carries no resolved cycle"
    # The hero's spend comes from the per-cycle accounting read. Zero here
    # would mean the cycle resolved but covered none of the corpus, which is
    # the same short branch wearing a different hat.
    assert (hero.get("total_tokens") or 0) > 0, (
        f"hero cycle carries no spend: {hero.get('cycle')}")


@pytest.mark.parametrize("scale", ["tiny", "small"])
def test_the_cycle_accounting_read_is_actually_executed(scale, shared_corpus,
                                                        monkeypatch):
    """Count the ROWS, not the calls.

    `calls["n"] > 0` discriminates nothing and is kept only as a precondition:
    measured with the cycle unresolvable, the loader was still called 3 times
    and returned 0 rows, so a corpus taking the short branch passes that
    assertion. `calls["rows"] > 0` is the one doing the work — do not trim it
    as redundant.
    """
    bbf = _load_build_bench()
    shared_corpus(scale)  # ensure the module and corpus are loaded first
    import _cctally_dashboard_sources as sources_mod

    calls = {"n": 0, "rows": 0}
    real = sources_mod.load_cached_rooted_codex_accounting_entries

    def counting(*args, **kwargs):
        calls["n"] += 1
        out = real(*args, **kwargs)
        calls["rows"] += len(out or ())
        return out

    monkeypatch.setattr(
        sources_mod, "load_cached_rooted_codex_accounting_entries", counting)
    _codex_source_state(shared_corpus(scale), bbf)

    assert calls["n"] > 0, (
        "precondition: the loader was never called at all")
    assert calls["rows"] > 0, (
        f"the cycle read ran {calls['n']} time(s) and returned NO rows. That is "
        "the degraded shape, not the absence of a call: the cycle did not "
        "resolve, or the corpus's Codex entries lie outside its window")


# ── The benchmark set must not perturb the corpus fingerprint ──────────────


def test_the_benchmark_set_is_semantic_hash_neutral(small_corpus):
    """The contract block describes two instants of the corpus; prove they agree.

    `bin/cctally-bench` reads `dataset_counts` and the discriminators BEFORE
    the benchmarks and `semantic_hash` after, because full-content hashing
    would warm the page cache for the tables `snapshot.cold` measures. That is
    only sound while no benchmark perturbs the hash — and `sync.delta`
    deliberately deletes a `session_files` row and re-ingests it. It is neutral
    because the hash covers no `source_path`, no offsets and no
    `last_ingested_at`. State the dependency here, or it breaks silently.
    """
    bbf = _load_build_bench()
    conn = bbf.open_fixture_db(small_corpus)
    try:
        before = bbf.semantic_hash(conn)
    finally:
        conn.close()

    # Re-hash after the ingest-metadata write `sync.delta` performs.
    import sqlite3 as _sq
    conn = _sq.connect(pathlib.Path(small_corpus) / "cache.db")
    try:
        row = conn.execute(
            "SELECT path FROM session_files ORDER BY path LIMIT 1").fetchone()
        assert row, "precondition: the corpus has a tracked session file"
        conn.execute(
            "UPDATE session_files SET last_ingested_at = '2099-01-01T00:00:00Z' "
            "WHERE path = ?", (row[0],))
        conn.commit()
    finally:
        conn.close()

    conn = bbf.open_fixture_db(small_corpus)
    try:
        after = bbf.semantic_hash(conn)
    finally:
        conn.close()
    assert before == after, (
        "semantic_hash moved when only ingest metadata changed, so the "
        "contract block's counts and fingerprint no longer describe one "
        "instant of the corpus and the fingerprint must move back before the "
        "benchmarks")


# ── The destructive-clear guards ───────────────────────────────────────────


def test_the_builder_refuses_a_root_it_did_not_create(tmp_path):
    """A mistyped `--out` must be refused, not emptied.

    `build_fixture` rmtrees the data dir, the Claude projects tree and every
    Codex root. The refusal was previously unreachable: the builder created its
    directories and wrote the sentinel BEFORE `_clear_previous_corpus` asked
    whether the root was its own, so the predicate answered yes about the
    builder's own work. Measured against a directory holding three user files:
    the guard said `False` beforehand, the build returned with no refusal, and
    all three files were gone.
    """
    bbf = _load_build_bench()
    root = tmp_path / "not-ours"
    (root / "data").mkdir(parents=True)
    (root / "codex-a").mkdir()
    keepers = {
        root / "README.txt": "user file",
        root / "data" / "user-file.txt": "user data",
        root / "codex-a" / "keepme.txt": "user codex",
    }
    for path, text in keepers.items():
        path.write_text(text)

    with pytest.raises(ValueError, match="refusing to build into"):
        bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)

    for path, text in keepers.items():
        assert path.exists(), f"{path} was deleted by a refused build"
        assert path.read_text() == text


def test_the_builder_accepts_an_empty_root_and_then_its_own(tmp_path):
    """The refusal must not block the two legitimate cases."""
    bbf = _load_build_bench()
    root = tmp_path / "fresh"
    bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
    assert (root / bbf._ROOT_SENTINEL).exists(), "no sentinel after a build"
    # Second call on a root that is now ours: reuse, no refusal.
    bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)


def test_the_build_lock_sits_outside_every_path_the_clear_removes(tmp_path):
    """`cache.db.lock` lives inside the data dir the clear deletes, so a build
    lock in there is on an inode the next clear unlinks — after which two
    processes hold locks on different inodes and mutual exclusion is gone."""
    bbf = _load_build_bench()
    root = tmp_path / "locked"
    lock = bbf.build_lock_path(root)
    assert lock.parent == root.parent, "the lock must be a SIBLING of the root"
    assert root not in lock.parents, "the lock must not live inside the root"
    assert lock not in set(bbf.destroyable_paths(root)), (
        "the lock is one of the paths the clear removes")

    bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
    assert lock.exists(), "the build did not take its lock"

    # The real property: a SECOND build, whose clear does delete the previous
    # corpus, must leave the lock in place. The first build on a fresh root
    # clears nothing, so asserting survival there proves nothing.
    before = lock.stat().st_ino
    original = dict(bbf.SCALES["tiny"])
    try:
        bbf.SCALES["tiny"] = {**original,
                              "codex_sessions": original["codex_sessions"] - 2}
        bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
    finally:
        bbf.SCALES["tiny"] = original
    assert lock.exists(), "the lock did not survive a rebuild's clear"
    assert lock.stat().st_ino == before, (
        "the clear unlinked the lock inode; a concurrent holder would now be "
        "serialising on a different file")


@pytest.mark.parametrize("scale", ["tiny", "small", "large"])
def test_every_codex_entry_is_placed_inside_the_live_cycle(scale):
    """Asserted on the PLAN, so `large` is covered without building it.

    `_codex_base_minute` is self-consistent only while
    `codex_events_per_session` stays under the span it divides: past that
    `span` clamps to 1, records run past `CORPUS_CLOCK_UTC`, `quota_freshness`
    reports `future`, and the cycle silently stops resolving. The end-to-end
    test is parametrised over the two cheap profiles only, so a future profile
    would trip this at `large` unseen.
    """
    bbf = _load_build_bench()
    params = bbf.SCALES[scale]
    plan = bbf._codex_emission_plan(params)
    events = params["codex_events_per_session"]
    usable = int((bbf.CORPUS_CLOCK_UTC - bbf._REF_EPOCH).total_seconds() // 60)
    last = max(
        bbf._codex_base_minute(i, len(plan), events) + 2 + events
        for i in range(len(plan)))
    assert last < usable, (
        f"{scale}: the last Codex record lands {last} minutes after the epoch, "
        f"past the corpus clock at {usable}; the cycle would stop resolving")
