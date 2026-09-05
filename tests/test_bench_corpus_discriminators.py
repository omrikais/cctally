"""The bench corpus must carry the properties the dashboard goldens cannot.

The 16 dashboard golden scenarios pin SHAPE at 71 and 11 session entries. This
corpus pins SCALE, and it is worthless for that purpose unless it also reaches
the multi-account Codex, colliding-basename and model-pool paths. Every
assertion here is a discriminator check, not a count check.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import textwrap

import pytest
from conftest import (  # type: ignore
    copy_shared_corpus, corpus_lock_path, describe_fingerprint_change,
    logical_corpus_fingerprint, suspend_corpus_protection)

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


def _readonly_fixture_db(data_dir):
    """`open_fixture_db`'s read-only twin, for the SHARED corpus (#741).

    `bbf.open_fixture_db` opens `conversations.db` WRITE-CAPABLE and attaches
    the cache `mode=ro`. That is right for a private copy and wrong for the
    session-scoped corpus, whose roots are now registered with the write
    detector: a write-capable connect there fails the item that opened it.
    The attachment NAME must stay `cache_db`, because that is what
    `semantic_hash` and `validate_corpus` qualify their cache-side statements
    with.
    """
    data_dir = pathlib.Path(data_dir)
    conn = sqlite3.connect(
        (data_dir / "conversations.db").resolve().as_uri() + "?mode=ro",
        uri=True)
    conn.execute(
        "ATTACH DATABASE ? AS cache_db",
        ((data_dir / "cache.db").resolve().as_uri() + "?mode=ro",))
    return conn


def _assert_every_discriminator(data_dir):
    """The three spec §4 discriminators, over one built corpus.

    Opened READ-ONLY: this reads the shared corpus in place, and a plain
    `sqlite3.connect` is not provably read-only, so the detector treats it as
    a write.
    """
    import _lib_codex_pools

    conn = sqlite3.connect(
        (pathlib.Path(data_dir) / "cache.db").resolve().as_uri() + "?mode=ro",
        uri=True)
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
def test_corpus_carries_both_provider_frontier_tickets(scale, shared_corpus):
    """The no-op dashboard receipt must exercise the trusted fast-negative."""
    import json
    import _lib_ingest_frontier as frontier

    marker = frontier.activity_marker_path(shared_corpus(scale))
    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert {row["provider"] for row in rows} == {"claude", "codex"}
    assert all(pathlib.Path(row["path"]).is_file() for row in rows)


@pytest.mark.parametrize("scale", ["tiny", "small"])
def test_the_cheap_profiles_realise_exactly_the_counts_they_declare(
    scale, shared_corpus
):
    """`validate_corpus` is what the `large` receipt runs; run it here too."""
    bbf = _load_build_bench()
    conn = _readonly_fixture_db(shared_corpus(scale))
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
    """The marker must already match, so a second call rebuilds nothing.

    THE ONE SANCTIONED SUSPENSION of the corpus write protection (#741). This
    test deliberately re-invokes the shared builder, and `build_fixture`
    re-creates `data/`, the provider roots and its own root sentinel before it
    re-checks its marker — so even the reuse path this test asserts writes
    inside the protected root. The suspension names one root and is restored on
    every exit path, so a failure here cannot leave the corpus unguarded for
    the rest of the worker's session.
    """
    import fcntl

    bbf = _load_build_bench()

    marker = bbf._marker_path(small_corpus)
    assert marker.exists(), "the shared fixture must leave its marker"
    stamp = marker.stat().st_mtime_ns

    # Under the SAME flock the fixture takes. Rebuilding the shared root
    # outside it would be the very race the fixture exists to prevent.
    with open(corpus_lock_path(corpus_root, "small"), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with suspend_corpus_protection(small_corpus):
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
        "quota_windows", "colliding_basename", "history_rows",
    }
    profiles = {name: bbf.SCALES[name] for name in ("tiny", "small", "large")}
    for name, params in profiles.items():
        assert discriminator_keys <= set(params), (
            name, sorted(discriminator_keys - set(params)))
        assert params["codex_accounts"] >= 2
        assert params["colliding_basename"] is True
        assert params["history_rows"] > 0
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
    data_dir = copy_shared_corpus(small_corpus, tmp_path / "corpus",
                                  scope="data")

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
def test_the_corpus_resolves_a_live_codex_weekly_cycle(scale, private_corpus):
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
    # A PRIVATE copy (#741): this drives a real `_tui_build_snapshot`, which
    # opens the cache and the transcript store write-capable and may fold
    # journal records, so it must not run against the shared corpus.
    snapshot = _codex_source_state(private_corpus(scale), bbf)
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
def test_the_cycle_accounting_fold_is_actually_executed(scale, private_corpus,
                                                        monkeypatch):
    """Count the cycle ROWS at the post-capture consumption seam.

    #617 deliberately removed the cycle-specific SQLite read when the captured
    accounting population already covers that cycle. The discriminating branch
    is now the dedicated pure cycle-accounting kernel that consumes those
    sliced rows for the hero; account-card totals cannot reach that seam.
    """
    bbf = _load_build_bench()
    # A PRIVATE copy, for the same reason as the test above.
    corpus = private_corpus(scale)  # loads the module and the corpus first
    import _cctally_dashboard_sources as sources_mod

    calls = {"n": 0, "rows": 0}
    real = getattr(sources_mod, "_build_codex_cycle_accounting", None)
    assert real is not None, (
        "the cycle carrier has no dedicated observable consumption seam")

    def counting(entries, *args, **kwargs):
        rows = tuple(entries)
        calls["n"] += 1
        calls["rows"] += len(rows)
        return real(rows, *args, **kwargs)

    monkeypatch.setattr(sources_mod, "_build_codex_cycle_accounting", counting)
    _codex_source_state(corpus, bbf)

    assert calls["n"] > 0, (
        "the resolved cycle never reached its post-capture pricing fold")
    assert calls["rows"] > 0, (
        f"the cycle fold ran {calls['n']} time(s) with NO rows. The cycle did "
        "not resolve, or the corpus's Codex entries lie outside its window")


# ── The benchmark set must not perturb the corpus fingerprint ──────────────


def test_the_benchmark_set_is_semantic_hash_neutral(small_corpus, tmp_path):
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
    # #630 S2: this test COMMITS an `UPDATE session_files SET
    # last_ingested_at = '2099-01-01T00:00:00Z'` below. Run in place that hands
    # every later consumer of the session-scoped corpus a store whose ingest
    # metadata has moved to the year 2099, so it works on its own copy.
    corpus = copy_shared_corpus(small_corpus, tmp_path / "corpus",
                                scope="data")
    conn = bbf.open_fixture_db(corpus)
    try:
        before = bbf.semantic_hash(conn)
    finally:
        conn.close()

    # Re-hash after the ingest-metadata write `sync.delta` performs.
    import sqlite3 as _sq
    conn = _sq.connect(corpus / "cache.db")
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

    conn = bbf.open_fixture_db(corpus)
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


# ── #630 S2: the shared-corpus contamination reproduction ───────────────────
#
# One item, not a pair. This was a mutating test followed by an observing test
# that relied on file order to run second on the same worker. Under
# `--dist load` — which is exactly what `bin/cctally-test-load-invariance`
# runs — the two land on arbitrary workers in arbitrary order, and an observer
# scheduled first passes without observing anything, which is a vacuous green
# on the one assertion the reproduction exists to make. Both halves are
# asserted in the same item instead, so no scheduling can weaken either.
#
# `_CONTAMINATION_SENTINEL` is deliberately a value no builder ever produces,
# so an assertion about it cannot be satisfied by ordinary corpus content.

_CONTAMINATION_SENTINEL = "2099-12-31T23:59:59Z"


def test_a_mutating_consumer_writes_only_to_its_own_copy(small_corpus, tmp_path):
    """Mutating a private copy must leave the shared corpus byte-identical.

    Red before the private copy: a consumer that writes in place puts the
    sentinel into the session-scoped corpus every later consumer is handed.
    Green after. `corpus_root` resolves to the same directory in every xdist
    worker, so "shared" here means shared across the whole run, not merely
    across one worker's items.
    """
    import sqlite3 as _sq

    shared_db = pathlib.Path(small_corpus) / "cache.db"
    private = copy_shared_corpus(small_corpus, tmp_path / "corpus",
                                 scope="data")
    conn = _sq.connect(private / "cache.db")
    try:
        changed = conn.execute(
            "UPDATE session_files SET last_ingested_at = ?",
            (_CONTAMINATION_SENTINEL,),
        ).rowcount
        conn.commit()
        stamped = conn.execute(
            "SELECT COUNT(*) FROM session_files WHERE last_ingested_at = ?",
            (_CONTAMINATION_SENTINEL,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert changed > 0, (
        "non-vacuity: the mutation matched no row, so the assertion below "
        "would hold whether or not the copy did its job")
    assert stamped == changed, (
        "non-vacuity: the write did not land in the private copy either, so "
        "nothing was actually mutated anywhere")

    conn = _sq.connect(f"file:{shared_db}?mode=ro", uri=True)
    try:
        leaked = conn.execute(
            "SELECT COUNT(*) FROM session_files WHERE last_ingested_at = ?",
            (_CONTAMINATION_SENTINEL,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert leaked == 0, (
        f"{leaked} row(s) in the SHARED session-scoped corpus carry the "
        f"sentinel this test wrote into its private copy. A consumer that "
        f"mutates or syncs into the shared corpus hands every later consumer "
        f"a different store than the one it was promised; see "
        f"docs/superpowers/plans/assets/2026-08-21-630-s2-corpus-consumers.md")


# ── #741: the shared corpus is write-protected, and the guard names the writer


def test_the_shared_corpus_refuses_a_write_and_names_the_writing_test(
    small_corpus, request,
):
    """Every leg of the protection, over the REAL session-scoped corpus.

    Reads stay legal — a plain file read, and a `mode=ro` SQLite connection
    whose own sidecar churn the guard cannot and need not see. A write is
    refused before it reaches the filesystem, and the ledger row carries this
    node id, this pid and this worker id, so a write by one xdist worker can
    never be reported against a reader on another: `collect_test_violations`
    answers per node, and no other node's query returns this row.

    The provoked violation is dropped from the ledger in `finally`, the way
    `tests/test_isolation_contract.py` drops the ones it provokes; without that
    the autouse teardown would fail the very test that proves the mechanism.
    """
    import _lib_test_isolation as iso

    node_id = request.node.nodeid
    root = pathlib.Path(small_corpus).parent
    assert os.path.realpath(root) in os.environ.get(
        iso.ENV_ROOTS, "").split(os.pathsep), (
        "the shared corpus root is not registered with the write detector, so "
        "nothing below proves anything")

    try:
        # A plain file reader passes.
        assert (pathlib.Path(small_corpus) / ".bench-fixture.json").read_text()

        # A read-only SQLite reader passes, and its sidecar churn is invisible
        # to the guard: SQLite's own file I/O is C-level and raises no `open`
        # audit event, which is why `mode=ro` is the sanctioned shape.
        conn = sqlite3.connect(
            (pathlib.Path(small_corpus) / "cache.db").resolve().as_uri()
            + "?mode=ro", uri=True)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM session_files").fetchone()[0] > 0
        finally:
            conn.close()
        assert iso.collect_test_violations(node_id) == [], (
            "reading the shared corpus was recorded as a violation")

        # The write is refused, and it never reaches the filesystem.
        doomed = pathlib.Path(small_corpus) / "another-worker-wrote-this.jsonl"
        with pytest.raises(iso.ProductionWriteBlocked):
            with open(doomed, "w") as handle:
                handle.write("{}\n")
        assert not doomed.exists(), (
            "the guard raised but the write landed anyway")

        rows = [v for v in iso.read_ledger() if v.node_id == node_id]
        assert rows, "the refused write left no ledger evidence"
        row = rows[-1]
        assert row.path.endswith("another-worker-wrote-this.jsonl")
        assert row.pid == os.getpid()
        assert row.worker_id == iso.worker_id()
        assert iso.collect_test_violations(node_id), (
            "the violation is not reportable against the test that caused it")
        assert iso.collect_test_violations(
            "tests/test_not_this_one.py::test_an_innocent_reader") == [], (
            "a violation was attributed to a node that did not cause it, "
            "which is exactly the cross-worker misattribution a per-test "
            "fingerprint would have produced")
    finally:
        iso.drop_node_from_ledger(node_id)


def test_a_writable_sqlite_connect_to_the_shared_corpus_is_refused(
    small_corpus, request,
):
    """`bbf.open_fixture_db(shared_corpus(...))` was exactly this shape.

    It opens `conversations.db` write-capable, which is not provably read-only
    and is therefore treated as a write. The read-only twin
    `_readonly_fixture_db` exists because of this.
    """
    import _lib_test_isolation as iso

    node_id = request.node.nodeid
    try:
        with pytest.raises(iso.ProductionWriteBlocked):
            sqlite3.connect(
                str(pathlib.Path(small_corpus) / "conversations.db"))
        assert iso.collect_test_violations(node_id)
    finally:
        iso.drop_node_from_ledger(node_id)


def test_the_corpus_fingerprint_hashes_content_semantic_hash_ignores(
    small_corpus, tmp_path,
):
    """The session backstop must be a LOGICAL hash, and not `semantic_hash`.

    `semantic_hash` excludes source paths, byte offsets, ingest timestamps and
    `stats.db` entirely — which is precisely the contamination a tick or an
    in-place re-ingest produces. A backstop built on it would be silent for the
    whole class it is supposed to catch, so this asserts the two disagree on a
    mutation of exactly that kind.
    """
    bbf = _load_build_bench()
    shared_root = pathlib.Path(small_corpus).parent
    fingerprint = logical_corpus_fingerprint(shared_root)
    # EVERY database, not the two that were named. `logical_sqlite_digest`
    # degrades to a constant `unopenable:` marker rather than raising, and a
    # marker on both sides compares equal, so an unasserted database is one the
    # backstop may be silently blind to. `stats.db` is the specific database the
    # docstring's reason for rejecting `semantic_hash` names, and it was the one
    # this check did not look at.
    databases = [rel for rel in fingerprint if rel.endswith(".db")]
    assert {"data/cache.db", "data/conversations.db",
            "data/stats.db"} <= set(databases), sorted(fingerprint)[:40]
    for relative in sorted(databases):
        entry = fingerprint[relative]
        assert entry[0] == "sqlite", entry
        assert len(entry[2]) == 64, (
            f"{relative} produced {entry[2]!r} rather than a sha256 digest, "
            f"so the backstop covers no content in that database at all")
    assert not [
        relative for relative in fingerprint
        if relative.endswith(("-wal", "-shm", "-journal"))
    ], "a transient SQLite sidecar is part of the fingerprint"

    private = copy_shared_corpus(small_corpus, tmp_path / "corpus",
                                 scope="data")
    before = logical_corpus_fingerprint(private)
    conn = bbf.open_fixture_db(private)
    try:
        semantic_before = bbf.semantic_hash(conn)
    finally:
        conn.close()

    conn = sqlite3.connect(str(private / "cache.db"))
    try:
        changed = conn.execute(
            "UPDATE session_files SET last_ingested_at = ?",
            (_CONTAMINATION_SENTINEL,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed > 0, "non-vacuity: the mutation matched no row"

    conn = bbf.open_fixture_db(private)
    try:
        semantic_after = bbf.semantic_hash(conn)
    finally:
        conn.close()
    assert semantic_after == semantic_before, (
        "semantic_hash moved on an ingest-metadata write, so the reason this "
        "backstop does not use it no longer holds and the docstring above "
        "must be corrected")
    problems = describe_fingerprint_change(
        before, logical_corpus_fingerprint(private))
    # An `unopenable:` line reports that the backstop was BLIND, not that
    # anything moved, so it must not be what satisfies this assertion: the
    # copier excludes the SQLite sidecars, and a copied WAL database with no
    # `-shm` cannot be opened read-only at either end.
    moved = [line for line in problems if "unopenable:" not in line]
    assert any("cache.db" in line for line in moved), (
        "the logical fingerprint did not move on an ingest-metadata write, so "
        "it is no better than semantic_hash for the class it guards; "
        f"reported: {problems}")


# ── the session-teardown fingerprint must be able to FAIL a run ────────────

#: A conftest that reuses the repository's real `shared_corpus` fixture, and
#: NOT `_isolation_detector`. Leaving the ledger fixture out is the point: the
#: child must fail from the session fingerprint or from nothing, which is what
#: makes this a test of the backstop rather than of the write detector.
#:
#: The repository conftest is executed under its own module name rather than
#: imported as `conftest`, because pytest imports the child's own file as
#: `conftest` and a `from conftest import ...` inside it would resolve to
#: itself, mid-import.
_BACKSTOP_CHILD_CONFTEST = '''
    import importlib.util
    import json
    import pathlib
    import sys

    import pytest

    REPO = pathlib.Path({repo!r})
    for _extra in (str(REPO / "tests"), str(REPO / "bin")):
        if _extra not in sys.path:
            sys.path.insert(0, _extra)

    _spec = importlib.util.spec_from_file_location(
        "cctally_root_conftest", str(REPO / "tests" / "conftest.py"))
    _root = importlib.util.module_from_spec(_spec)
    sys.modules["cctally_root_conftest"] = _root
    _spec.loader.exec_module(_root)

    corpus_root = _root.corpus_root
    shared_corpus = _root.shared_corpus
    _isolation_scratch_root = _root._isolation_scratch_root

    # #745. `shared_corpus`'s teardown calls `remove_protected_root` and
    # nothing asserted it, so a call that had stopped being EFFECTIVE would
    # have looked identical to one that worked. Registered autouse and
    # session-scoped so it is set up BEFORE `shared_corpus`: same-scope
    # finalization is LIFO, which is what puts this observer after the fixture
    # it observes. Pytest keeps running the remaining finalizers after one
    # raises, so the report is still written even though `shared_corpus`
    # deliberately fails the session above.
    import _lib_test_isolation as _iso_mod

    _FINALIZER_REPORT = pathlib.Path({report!r})

    # `suspend_corpus_protection` ALSO calls `remove_protected_root`, from
    # inside the test body, so a bare call count is satisfied by the suspension
    # and says nothing about the finalizer. This flag separates the two, and it
    # is FUNCTION-scoped for a reason measured here: session-scoped fixtures are
    # torn down inside the LAST item's teardown, so `pytest_runtest_logfinish`
    # fires after `shared_corpus`'s finalizer and marked its call as in-test.
    # Function-scoped finalizers run before higher-scoped ones, so this one is
    # the boundary.
    _PHASE = {{"in_test": False}}


    @pytest.fixture(autouse=True)
    def _mark_test_phase():
        _PHASE["in_test"] = True
        yield
        _PHASE["in_test"] = False


    @pytest.fixture(scope="session", autouse=True)
    def _observe_corpus_protection(corpus_root):
        real_remove = _iso_mod.remove_protected_root
        calls = []

        def _record(root):
            calls.append([str(root), _PHASE["in_test"]])
            return real_remove(root)

        _iso_mod.remove_protected_root = _record
        before = sorted(str(entry) for entry in _iso_mod.protected_roots())
        try:
            yield
        finally:
            _iso_mod.remove_protected_root = real_remove
            # Braces doubled: this template is rendered with `str.format`.
            _FINALIZER_REPORT.write_text(json.dumps({{
                "expected_root": str(corpus_root / "tiny"),
                "removal_calls": calls,
                "protected_before": before,
                "protected_after": sorted(
                    str(entry) for entry in _iso_mod.protected_roots()),
            }}, sort_keys=True), encoding="utf-8")
'''

#: The child's test. `tiny` is the cheap scale, and the write lands at the
#: scale root, which is where a native child or a writable ATTACH would write.
_BACKSTOP_CHILD_TEST = '''
    import pathlib

    from _shared_corpus import suspend_corpus_protection

    WRITTEN = "a-write-the-detector-could-not-see.jsonl"


    def test_writes_into_the_shared_corpus_unseen(shared_corpus):
        data_dir = pathlib.Path(shared_corpus("tiny"))
        with suspend_corpus_protection(data_dir):
            (data_dir.parent / WRITTEN).write_text("{}", encoding="utf-8")
'''

_BACKSTOP_WRITTEN_FILE = "a-write-the-detector-could-not-see.jsonl"


def test_the_session_fingerprint_backstop_fails_a_run_it_should_fail(tmp_path):
    """The `pytest.fail` in `shared_corpus`'s finalizer must be able to fire.

    D5 designates this fingerprint as the cover for the write detector's three
    documented blind spots — a writable `ATTACH`, a native child process, and a
    descriptor opened before the protection was installed. An inert finalizer
    leaves all three uncovered and says nothing about it, which is the failure
    class `tests/conftest.py` names F28 two docstrings above `shared_corpus`.

    Out of process, because the fixture is session-scoped: its finalizer runs
    at session teardown, and this session's own corpus must not be written to.
    The child suppresses the write detector over one root through the one
    sanctioned escape, so the write lands and only the fingerprint can catch it.
    """
    repo = str(pathlib.Path(__file__).resolve().parents[1])
    report_path = tmp_path / "finalizer-report.json"
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(_BACKSTOP_CHILD_CONFTEST).format(
            repo=repo, report=str(report_path)),
        encoding="utf-8")
    (tmp_path / "test_backstop_child.py").write_text(
        textwrap.dedent(_BACKSTOP_CHILD_TEST), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         str(tmp_path / "test_backstop_child.py"),
         "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=90,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        "the shared corpus was written to and the session still passed, so "
        "the fingerprint backstop is inert:\n" + combined)
    assert _BACKSTOP_WRITTEN_FILE in combined, (
        "the report does not name the path that changed:\n" + combined)
    assert "s1-bench-corpus/tiny" in combined, (
        "the report does not name the corpus root it found changed:\n"
        + combined)
    assert ("the write detector's backstop found the shared bench corpus"
            in combined), (
        "the run failed for some other reason than the fingerprint:\n"
        + combined)

    # ── #745: the finalizer's `remove_protected_root` is asserted ──────────
    assert report_path.exists(), (
        "the observer never ran, so nothing below is evidence about the "
        f"finalizer:\n{combined}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected = report["expected_root"]

    at_teardown = [root for root, in_test in report["removal_calls"]
                   if root == expected and not in_test]
    assert len(at_teardown) == 1, (
        "the shared corpus root was not un-protected exactly once at session "
        f"teardown: {report['removal_calls']}")
    # Counting calls is not enough on its own. A call that had stopped taking
    # effect would still be counted, so membership afterwards is asserted too.
    real_expected = os.path.realpath(expected)
    assert not any(os.path.realpath(entry) == real_expected
                   for entry in report["protected_after"]), (
        "the corpus root is still protected after teardown, so the removal "
        f"was called but ineffective: {report['protected_after']}")
    assert report["protected_after"] == report["protected_before"], (
        "teardown changed roots unrelated to the corpus: "
        f"{report['protected_before']} -> {report['protected_after']}")


_UNOPENABLE = "unopenable:OperationalError:unable to open database file"


def test_an_unopenable_database_is_reported_even_when_both_sides_match():
    """A backstop that could not READ is not a backstop that found nothing.

    `logical_sqlite_digest` degrades to a constant marker string rather than
    raising, so a database that fails to open at the baseline AND at the
    teardown — which a WAL database with no `-shm` does, because a read-only
    connection cannot create one — compares equal to itself while every change
    inside it stays invisible. Equality there is silence, not a clean result.
    """
    unreadable = {"data/stats.db": ("sqlite", 0o644, _UNOPENABLE)}
    problems = describe_fingerprint_change(unreadable, dict(unreadable))
    assert problems, (
        "an unreadable database compared equal to itself and the comparison "
        "reported nothing at all")
    assert "data/stats.db" in problems[0], problems
    assert "invisible to the backstop" in problems[0], problems

    # Non-vacuity in the other direction: a database that DID open, unchanged,
    # is not a problem, or every clean session would report one.
    readable = {"data/stats.db": ("sqlite", 0o644, "0" * 64)}
    assert describe_fingerprint_change(readable, dict(readable)) == []

    # An unreadable side that also differs keeps the ordinary difference line.
    changed = describe_fingerprint_change(
        unreadable, {"data/stats.db": ("sqlite", 0o600, _UNOPENABLE)})
    assert len(changed) == 2, changed


def test_suspending_protection_over_an_unprotected_root_installs_none(tmp_path):
    """#741 D5: the suspension RESTORES membership, it does not install it.

    An unconditional `add_protected_root` at exit protects a root that had
    none, which is a save-and-restore that never saved. Both call sites in the
    estate happen to enter over a protected root, so without this test the
    False branch is exercised by nothing and the next edit can re-break it.
    """
    import _lib_test_isolation as iso

    data_dir = tmp_path / "never-protected" / "data"
    data_dir.mkdir(parents=True)
    real = os.path.realpath(data_dir.parent)
    before = tuple(iso.protected_roots())
    assert not any(os.path.realpath(entry) == real for entry in before), (
        "non-vacuity: this root must be unprotected on entry, or the False "
        "branch under test is never taken")

    with suspend_corpus_protection(data_dir) as root:
        assert os.path.realpath(root) == real

    after = tuple(iso.protected_roots())
    assert not any(os.path.realpath(entry) == real for entry in after), (
        "the suspension INSTALLED protection over a root that had none")
    assert set(after) == set(before), (
        f"the suspension changed unrelated protected roots: {before} -> {after}")


# ── the tree that BUILT the corpus (#718) ──────────────────────────────────


def test_the_producer_digest_is_stable_and_covers_the_declared_sources():
    """`producer_sha256` is a deterministic digest over the whole manifest."""
    bbf = _load_build_bench()
    records = bbf.producer_source_records()
    assert [r["path"] for r in records] == sorted(bbf.PRODUCER_SOURCES)
    assert all(len(r["sha256"]) == 64 for r in records)
    assert all(r["sha256"] == r["sha256"].lower() for r in records)
    assert bbf.producer_sha256() == bbf.producer_sha256()
    assert len(bbf.producer_sha256()) == 64


def test_the_producer_digest_moves_when_a_producer_source_changes():
    """A comment-only byte change must move the digest.

    This is the whole point of the manifest: `GENERATOR_VERSION` is
    hand-maintained, so it cannot prove source identity on its own, and
    `semantic_hash` reads semantic columns and therefore cannot see a
    difference in what another tree's ingest wrote.
    """
    bbf = _load_build_bench()
    records = {r["path"]: r["sha256"] for r in bbf.producer_source_records()}
    assert "bin/_cctally_cache.py" in records, (
        "precondition: the ingest module must be in the manifest")
    mutated = dict(records, **{"bin/_cctally_cache.py": "0" * 64})
    assert bbf.digest_producer_records(
        [{"path": p, "sha256": s} for p, s in sorted(mutated.items())]
    ) != bbf.producer_sha256()


def test_the_producer_digest_frames_each_record():
    """Two different manifests must not hash to one byte stream.

    Without a length prefix per field, `("ab", d1), ("c", d2)` and
    `("abc", d1 + d2)`-shaped pairs concatenate identically.
    """
    bbf = _load_build_bench()
    left = [{"path": "ab", "sha256": "1" * 64},
            {"path": "c", "sha256": "2" * 64}]
    right = [{"path": "abc", "sha256": "1" * 64},
             {"path": "", "sha256": "2" * 64}]
    assert (bbf.digest_producer_records(left)
            != bbf.digest_producer_records(right))


def test_a_missing_declared_producer_source_fails_closed(monkeypatch):
    """A digest over a smaller set than declared still looks like a digest."""
    bbf = _load_build_bench()
    monkeypatch.setattr(
        bbf, "PRODUCER_SOURCES",
        tuple(bbf.PRODUCER_SOURCES) + ("bin/_does_not_exist.py",))
    with pytest.raises(FileNotFoundError):
        bbf.producer_source_records()


def test_the_manifest_covers_the_ingest_entry_points():
    """The closure must reach every file that writes the corpus stores."""
    bbf = _load_build_bench()
    declared = set(bbf.PRODUCER_SOURCES)
    for required in ("bin/build-bench-fixtures.py", "bin/cctally",
                     "bin/_cctally_cache.py", "bin/_cctally_db.py",
                     "bin/_cctally_store.py", "bin/_lib_jsonl.py"):
        assert required in declared, required


def test_the_manifest_is_the_resolved_closure():
    """The frozen tuple cannot drift from the walk that produced it.

    Frozen so the set is reviewable in a diff, and re-derived here so freezing
    it does not turn into a stale hand-maintained list — which is exactly the
    failure `GENERATOR_VERSION` already has.
    """
    bbf = _load_build_bench()
    resolved = bbf.resolve_producer_closure()
    assert resolved == tuple(sorted(bbf.PRODUCER_SOURCES)), (
        "PRODUCER_SOURCES disagrees with resolve_producer_closure(); "
        f"missing={sorted(set(resolved) - set(bbf.PRODUCER_SOURCES))} "
        f"extra={sorted(set(bbf.PRODUCER_SOURCES) - set(resolved))}")


def test_the_manifest_excludes_only_sources_the_public_tree_lacks():
    """The exclusions are the mirror-private `__preview` pair and nothing else.

    An exclusion set is a hole in a fail-closed manifest, so its membership is
    tied to a checkable fact rather than to judgement: `bin/cctally` loads both
    inside a `try` precisely because the public clone does not carry them.
    """
    bbf = _load_build_bench()
    assert set(bbf.PRODUCER_SOURCE_EXCLUSIONS) == {
        "bin/_cctally_preview.py",  # mirror-private-ok: asserted absent, never read
        "bin/_lib_preview.py"}  # mirror-private-ok: asserted absent, never read
    assert not set(bbf.PRODUCER_SOURCE_EXCLUSIONS) & set(bbf.PRODUCER_SOURCES)
    source = (pathlib.Path(bbf.__file__).resolve().parent / "cctally").read_text()
    assert '_load_sibling("_lib_preview")' in source
    assert "cmd_preview = None" in source, (
        "bin/cctally no longer tolerates the preview modules being absent, so "
        "excluding them from the producer manifest is no longer justified")


def _load_cctally_bench():
    """Path-load the hyphenated benchmark runner."""
    path = BIN / "cctally-bench"
    loader = importlib.machinery.SourceFileLoader("cctally_bench", str(path))
    spec = importlib.util.spec_from_loader("cctally_bench", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_the_benchmark_announces_a_rebuild_over_another_trees_corpus(
        tmp_path, capsys):
    """The benchmark rebuilds, so it owes the same announcement the capture does.

    `bin/cctally-bench` defaults to a machine-global scratch root, so a
    `--compare` or `--gate` run can replace a `large` corpus another checkout
    built — 1m49s and 1.2 GiB — and until now it did that in silence, because
    the announcement lived only in `bin/cctally-snapshot-measure`.

    The whole call is wrapped in `pinned_env`, because `build_fixture` pins four
    environment axes and deliberately leaves them pinned for the benchmark that
    is about to open the cache through them.
    """
    bbf = _load_build_bench()
    bench = _load_cctally_bench()
    root = tmp_path / "corpus"
    with bbf.pinned_env(root / "data", root / "claude"):
        bbf.build_fixture(scale="tiny", seed=42, root=root)
        marker = bbf._marker_path(root / "data")
        payload = json.loads(marker.read_text())
        payload["build_provenance"]["producer_sha256"] = "a" * 64
        marker.write_text(json.dumps(payload, sort_keys=True))

        bench._build_fixture_announced(
            bbf, scale="tiny", seed=42, root=root, label="bench")

    err = capsys.readouterr().err
    assert "REBUILT" in err, err
    assert "a" * 64 in err, err
    assert bbf.producer_sha256() in err, err
    rebuilt = json.loads(marker.read_text())["build_provenance"]
    assert rebuilt["producer_sha256"] == bbf.producer_sha256()


def test_the_benchmark_announces_nothing_when_it_reuses_a_corpus(
        tmp_path, capsys):
    """Non-vacuity: a reuse must stay quiet, or the announcement means nothing."""
    bbf = _load_build_bench()
    bench = _load_cctally_bench()
    root = tmp_path / "corpus"
    with bbf.pinned_env(root / "data", root / "claude"):
        bbf.build_fixture(scale="tiny", seed=42, root=root)
        capsys.readouterr()
        bench._build_fixture_announced(
            bbf, scale="tiny", seed=42, root=root, label="bench")
    err = capsys.readouterr().err
    assert "REBUILT" not in err, (
        "a reuse announced a replacement, so the announcement no longer "
        f"discriminates:\n{err}")


def _complete_corpus_stub(data_dir):
    """The two stores `classify_marker` requires before it reads the marker."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "cache.db").write_bytes(b"")
    (data_dir / "conversations.db").write_bytes(b"")
    return data_dir


def _provenance_want(digest):
    return {"seed": 1, "build_provenance": {
        "schema_version": 1, "producer_sha256": digest, "sources": []}}


def test_an_absent_corpus_classifies_absent(tmp_path):
    bbf = _load_build_bench()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert bbf.classify_marker(
        bbf._marker_path(data_dir), {"seed": 1}, data_dir) == bbf.MARKER_ABSENT


def test_a_complete_corpus_with_a_different_producer_classifies_mismatch(
        tmp_path):
    """The state the boolean could not express, and the whole defect (#718)."""
    bbf = _load_build_bench()
    data_dir = _complete_corpus_stub(tmp_path / "data")
    want = _provenance_want("a" * 64)
    on_disk = _provenance_want("b" * 64)
    bbf._marker_path(data_dir).write_text(json.dumps(on_disk))
    assert bbf.classify_marker(
        bbf._marker_path(data_dir), want, data_dir) == bbf.MARKER_MISMATCH


def test_a_legacy_marker_without_provenance_classifies_mismatch(tmp_path):
    """A corpus built before provenance existed identifies no producer."""
    bbf = _load_build_bench()
    data_dir = _complete_corpus_stub(tmp_path / "data")
    bbf._marker_path(data_dir).write_text(json.dumps({"seed": 1}))
    assert bbf.classify_marker(
        bbf._marker_path(data_dir), _provenance_want("a" * 64),
        data_dir) == bbf.MARKER_MISMATCH


def test_a_corrupt_marker_classifies_absent_so_it_rebuilds(tmp_path):
    """A marker nobody can read is not evidence of a mismatch."""
    bbf = _load_build_bench()
    data_dir = _complete_corpus_stub(tmp_path / "data")
    bbf._marker_path(data_dir).write_text("{not json")
    assert bbf.classify_marker(
        bbf._marker_path(data_dir), {"seed": 1}, data_dir) == bbf.MARKER_ABSENT


def test_an_equal_marker_classifies_match(tmp_path):
    """Non-vacuity: the three states must not collapse into two."""
    bbf = _load_build_bench()
    data_dir = _complete_corpus_stub(tmp_path / "data")
    want = _provenance_want("a" * 64)
    bbf._marker_path(data_dir).write_text(json.dumps(want))
    assert bbf.classify_marker(
        bbf._marker_path(data_dir), want, data_dir) == bbf.MARKER_MATCH


def test_the_marker_payload_carries_the_current_producer():
    bbf = _load_build_bench()
    import cctally  # noqa: F401  (loaded for the pricing/epoch fields)

    payload = bbf._marker_payload(cctally, seed=42, scale="tiny")
    provenance = payload["build_provenance"]
    assert provenance["schema_version"] == 1
    assert provenance["producer_sha256"] == bbf.producer_sha256()
    assert provenance["sources"] == bbf.producer_source_records()


def test_the_marker_records_provenance_only_after_a_successful_build(tmp_path):
    bbf = _load_build_bench()
    data_dir = bbf.build_fixture_isolated(
        scale="tiny", seed=42, root=tmp_path / "corpus")
    marker = json.loads(bbf._marker_path(data_dir).read_text())
    provenance = marker["build_provenance"]
    assert provenance["schema_version"] == 1
    assert provenance["producer_sha256"] == bbf.producer_sha256()
    assert provenance["sources"] == bbf.producer_source_records()


def test_an_unknown_on_mismatch_policy_is_refused(tmp_path):
    """A typo must not silently select the permissive branch."""
    bbf = _load_build_bench()
    with pytest.raises(ValueError, match="on_mismatch"):
        bbf.build_fixture_isolated(
            scale="tiny", seed=42, root=tmp_path / "corpus",
            on_mismatch="rebuidl")


def test_the_pytest_fixture_rebuilds_rather_than_refusing(tmp_path):
    """A routine refusal in the shared fixture would damage the estate.

    Its root is scoped to the pytest numbered directory and it builds only the
    cheap profiles, so rebuilding there is both safe and correct — unlike a
    capture, which must never publish over stores it did not cause.
    """
    bbf = _load_build_bench()
    root = tmp_path / "corpus"
    data_dir = bbf.build_fixture_isolated(scale="tiny", seed=42, root=root)
    marker = bbf._marker_path(data_dir)
    payload = json.loads(marker.read_text())
    payload["build_provenance"]["producer_sha256"] = "a" * 64
    marker.write_text(json.dumps(payload, sort_keys=True))
    assert bbf.classify_marker(
        marker, bbf._marker_payload(
            __import__("cctally"), seed=42, scale="tiny"),
        data_dir) == bbf.MARKER_MISMATCH, "precondition: the marker must differ"

    rebuilt = bbf.build_fixture_isolated(
        scale="tiny", seed=42, root=root, on_mismatch="rebuild")

    provenance = json.loads(
        bbf._marker_path(rebuilt).read_text())["build_provenance"]
    assert provenance["producer_sha256"] == bbf.producer_sha256()


# --------------------------------------------------------------------------
# The rebuild announcement, at the unit level.
#
# The end-to-end capture tests reach two of the announcement's outcomes. The
# rest are reachable only from a marker a person edited, and the reason this
# session exists is that a check which covers less than its name claims reports
# a result nobody measured. These pin every kind and every wording.


def _marker_with(tmp_path, payload):
    marker = tmp_path / ".bench-fixture.json"
    marker.write_text(json.dumps(payload))
    return marker


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def test_read_marker_producer_names_every_kind_it_can_encounter(tmp_path):
    bbf = _load_build_bench()
    missing = tmp_path / "nothing-here.json"
    assert bbf.read_marker_producer(missing) == (bbf.MARKER_PRODUCER_ABSENT, None)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert bbf.read_marker_producer(broken) == (bbf.MARKER_PRODUCER_UNREADABLE, None)

    not_utf8 = tmp_path / "not-utf8.json"
    not_utf8.write_bytes(b'{"seed": "\xff\xfe"}')
    assert bbf.read_marker_producer(not_utf8) == (bbf.MARKER_PRODUCER_UNREADABLE, None)

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]")
    assert bbf.read_marker_producer(not_an_object) == (
        bbf.MARKER_PRODUCER_UNREADABLE, None)

    # No `build_provenance` at all is the only marker that genuinely predates
    # the field, and it is the only one the legacy wording is true of.
    assert bbf.read_marker_producer(_marker_with(tmp_path, {"seed": 42})) == (
        bbf.MARKER_PRODUCER_LEGACY, None)
    assert bbf.read_marker_producer(
        _marker_with(tmp_path, {"build_provenance": "not-an-object"})) == (
        bbf.MARKER_PRODUCER_LEGACY, None)

    for unusable in ("abc", "", _DIGEST_A[:63], 17, None):
        kind, built = bbf.read_marker_producer(
            _marker_with(tmp_path, {"build_provenance": {"producer_sha256": unusable}}))
        assert (kind, built) == (bbf.MARKER_PRODUCER_MALFORMED, None), unusable

    assert bbf.read_marker_producer(
        _marker_with(tmp_path, {"build_provenance": {"producer_sha256": _DIGEST_A}})) == (
        bbf.MARKER_PRODUCER_IDENTIFIED, _DIGEST_A)


def test_the_rebuild_announcement_states_a_true_reason_for_every_kind():
    bbf = _load_build_bench()

    # Nothing to announce: there was no corpus, or this tree built the one
    # that was there.
    assert bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_ABSENT, None), _DIGEST_A) is None
    assert bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_IDENTIFIED, _DIGEST_A), _DIGEST_A) is None

    replaced = bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_IDENTIFIED, _DIGEST_B), _DIGEST_A)
    assert "this tree did not build" in replaced
    assert _DIGEST_B in replaced and _DIGEST_A in replaced

    legacy = bbf.rebuild_announcement((bbf.MARKER_PRODUCER_LEGACY, None), _DIGEST_A)
    assert "predates build provenance" in legacy

    # A marker that CARRIES the field with an unusable value does not predate
    # it, so it must not be told that it does.
    malformed = bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_MALFORMED, None), _DIGEST_A)
    assert "not usable" in malformed
    assert "predates" not in malformed

    # The headline elsewhere asserts that another tree built the corpus. This
    # is the one reading that cannot establish it, so it must not claim it.
    unreadable = bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_UNREADABLE, None), _DIGEST_A)
    assert "cannot be established" in unreadable
    assert "this tree did not build" not in unreadable

    assert "(the rebuild recorded none)" in bbf.rebuild_announcement(
        (bbf.MARKER_PRODUCER_LEGACY, None), None)
