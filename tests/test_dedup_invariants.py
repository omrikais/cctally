"""The R-DEDUP1..5 corpus contract and the strict-mode validator (#529 S4 F12).

Before this module existed, ``bin/cctally-reconcile-test``'s ``dedup_invariants``
block read the maintainer's real ``~/.local/share/cctally`` and every JSONL under
``~/.claude/projects``, and every one of the five invariants carried a skip arm
that still let the block print ``OK``. Two runs of the identical tree on the two
LAN runners on 2026-08-11 disagreed about which invariants ran and both reported
the same ``passed: 76``.

The corpus this module builds is therefore INPUT-ONLY. ``bin/_fixture_builders``
offers ``create_cache_db`` / ``create_stats_db`` plus direct row seeders, and a
corpus written that way would compare a seeded value against a recomputation over
seeded values: all five invariants would execute, all five would pass, all five
mutation tests would still fail exactly the right invariant, and no production
dedup, writer or migration code would have run. So the builder writes raw JSONL
and deliberately WRONG stats rows, and the real ingest plus migrations 008/009/010
produce every value an invariant reads.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER = REPO_ROOT / "bin" / "build-dedup-fixtures.py"
BIN = REPO_ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_dedup_invariants as dedup  # noqa: E402

AS_OF = "2026-04-27T12:00:00Z"


def _build_corpus(out: Path) -> None:
    """Run the builder into ``out``, failing loudly with its stderr."""
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.returncode == 0, (
        f"build-dedup-fixtures.py failed ({proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def _count_emissions_in(jsonl_paths) -> int:
    """Raw assistant emissions carrying both a message id and a request id.

    This is the pre-dedup population: ``emit_streaming_pair`` writes two of these
    per logical call, and the ingest must collapse each pair to one row.
    """
    total = 0
    for path in jsonl_paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message") or {}
            if message.get("id") and obj.get("requestId"):
                total += 1
    return total


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    out = tmp_path_factory.mktemp("dedup-corpus") / "corpus"
    _build_corpus(out)
    return out


def test_the_dedup_corpus_is_produced_by_production_paths_not_seeded(corpus):
    """THE central risk of this tranche.

    ``_fixture_builders.create_stats_db`` and ``create_cache_db`` write fully
    migrated schemas and the module offers direct row seeders, so a builder using
    them would produce a corpus where every invariant compares a seeded value
    against a recompute over seeded values. All five would execute, all five
    would pass, all five mutation tests would still fail the right invariant, and
    NO production dedup, cost-writer or migration code would have run.
    """
    jsonl = sorted((corpus / ".claude" / "projects").rglob("*.jsonl"))
    assert jsonl, "no JSONL input -- the corpus is not input-driven"

    cache = sqlite3.connect(corpus / ".local/share/cctally/cache.db")
    try:
        stored = cache.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone()[0]
    finally:
        cache.close()

    raw_emissions = _count_emissions_in(jsonl)
    assert raw_emissions > 0, "the JSONL carries no assistant emissions at all"
    assert stored > 0, "session_entries is empty -- the ingest never ran"
    assert stored < raw_emissions, (
        f"session_entries holds {stored} rows for {raw_emissions} raw emissions, "
        f"so the dedup ingest never collapsed a streaming pair and the corpus "
        f"proves only the builder's own arithmetic"
    )

    # A row count alone cannot separate an ingest from a seeder that happened to
    # write one row per logical call. `session_files` is the ingest's own
    # delta-resume bookkeeping: one row per walked file, resumed at that file's
    # exact byte length. No invariant reads it, so nothing but a real
    # `sync_cache` puts it there.
    cache = sqlite3.connect(corpus / ".local/share/cctally/cache.db")
    try:
        walked = dict(cache.execute(
            "SELECT path, last_byte_offset FROM session_files"))
    finally:
        cache.close()
    assert set(walked) == {str(p) for p in jsonl}, (
        f"session_files does not cover the corpus JSONL: "
        f"{sorted(walked)} vs {[str(p) for p in jsonl]}")
    for path, offset in walked.items():
        assert offset == Path(path).stat().st_size, (
            f"{path} resumed at {offset} but is {Path(path).stat().st_size} "
            f"bytes -- the walk never completed it")


def test_the_corpus_stats_values_were_written_by_the_real_migrations(corpus):
    """The three seeded pre-dedup values are deliberately absurd, so a stats.db
    still carrying any of them means the recompute migrations never ran and every
    later assertion in this module would be measuring the builder."""
    stats = sqlite3.connect(corpus / ".local/share/cctally/stats.db")
    try:
        applied = {
            row[0] for row in stats.execute("SELECT name FROM schema_migrations")
        }
        for name in (
            "008_recompute_weekly_cost_snapshots_dedup_fix",
            "009_recompute_five_hour_blocks_dedup_fix",
            "010_recompute_percent_milestones_dedup_fix",
        ):
            assert name in applied, f"{name} never applied against the corpus"

        stale = [
            row[0] for row in stats.execute(
                "SELECT cost_usd FROM weekly_cost_snapshots "
                "WHERE mode = 'auto' AND project IS NULL")
        ]
        assert stale, "no in-scope weekly_cost_snapshots row survived the build"
        assert all(value < 100.0 for value in stale), (
            f"weekly_cost_snapshots still carries a seeded pre-dedup value: "
            f"{stale}")

        totals = [
            row[0] for row in stats.execute(
                "SELECT total_cost_usd FROM five_hour_blocks")
        ]
        assert totals and all(value < 100.0 for value in totals), (
            f"five_hour_blocks still carries a seeded pre-dedup value: {totals}")

        cumulative = [
            row[0] for row in stats.execute(
                "SELECT cumulative_cost_usd FROM percent_milestones")
        ]
        assert cumulative and all(value < 100.0 for value in cumulative), (
            f"percent_milestones still carries a seeded pre-dedup value: "
            f"{cumulative}")
    finally:
        stats.close()


def test_the_corpus_exposes_selected_events_for_every_durable_audit_row(corpus):
    """Live replay fidelity must consume the real selector, not a test shim or
    an unselected raw journal event."""
    stats = sqlite3.connect(corpus / ".local/share/cctally/stats.db")
    try:
        missing = stats.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT journal_id FROM weekly_cost_snapshots "
            " WHERE mode='auto' AND project IS NULL "
            " UNION ALL "
            " SELECT journal_id FROM five_hour_blocks WHERE is_closed=1 "
            " UNION ALL "
            " SELECT journal_id FROM percent_milestones"
            ") audited LEFT JOIN journal_effective_events e "
            "ON e.event_id=audited.journal_id "
            "WHERE audited.journal_id IS NULL OR e.event_id IS NULL"
        ).fetchone()[0]
    finally:
        stats.close()
    assert missing == 0, (
        f"{missing} durable audit rows have no selected effective event")


def test_both_corpus_stores_open_read_only(corpus):
    """The validator connects ``mode=ro``, which a WAL-mode database cannot
    serve without an ``-shm`` file it is not allowed to create. A corpus left
    with a live WAL opens for the builder and refuses the consumer."""
    share = corpus / ".local" / "share" / "cctally"
    for name in ("cache.db", "stats.db"):
        conn = sqlite3.connect(f"file:{share / name}?mode=ro", uri=True)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        assert not (share / f"{name}-wal").exists(), (
            f"{name} still carries a WAL sidecar")


def test_the_corpus_pins_an_as_of(corpus):
    """The harness reads ``input.env`` for the pinned instant; without it the
    active-block invariant would fall back to wall clock and the corpus would
    stop being reproducible the moment it aged past its own active block."""
    text = (corpus / "input.env").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line)
    assert values.get("AS_OF"), f"input.env carries no AS_OF: {text!r}"


def test_the_corpus_builds_under_the_environment_the_harness_gives_it(tmp_path):
    """``bin/cctally-reconcile-test`` invokes the builder from bash, not from a
    pytest process, so the builder never sees ``PYTHONPATH``, the isolation
    bootstrap, or anything else the test session has already imported.

    This is not a hypothetical difference. The first implementation resolved
    ``importlib.machinery`` without importing it, which the bootstrap had
    already bound inside a pytest child — so the builder was green here and
    raised ``AttributeError`` the moment the harness ran it.
    """
    out = tmp_path / "sanitized" / "corpus"
    home = tmp_path / "sanitized-home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": str(tmp_path / "tmp"),
        "TZ": "Etc/UTC",
        "LANG": "C",
        "LC_ALL": "C",
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
    }
    (tmp_path / "tmp").mkdir()
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(out)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 0, (
        f"the builder failed under a bash-shaped environment:\n{proc.stderr}")
    assert (out / ".local/share/cctally/stats.db").is_file()


def test_the_corpus_writes_nothing_outside_its_own_root(tmp_path):
    """A builder that resolved a path from the ambient HOME would write into the
    maintainer's real tree, which is the whole defect this tranche removes."""
    out = tmp_path / "sandbox" / "corpus"
    home = tmp_path / "ambient-home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("CCTALLY_DATA_DIR", None)
    proc = subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(out)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.returncode == 0, proc.stderr
    assert list(home.iterdir()) == [], (
        f"the builder wrote into the ambient HOME: "
        f"{[p.name for p in home.iterdir()]}")


# --------------------------------------------------------------------------
# Task 12 — the shared validator in strict mode
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_corpus(corpus, tmp_path):
    """A writable copy of the module-scoped corpus, for the negative cases and
    the mutation matrix. The corpus itself is never mutated in place."""
    dest = tmp_path / "corpus"
    shutil.copytree(corpus, dest)
    return dest


def _stats(root):
    return sqlite3.connect(root / ".local/share/cctally/stats.db")


def _cache(root):
    return sqlite3.connect(root / ".local/share/cctally/cache.db")


def test_strict_mode_executes_exactly_the_five_invariants(corpus):
    """The green control. Every name must carry a checked count above zero, a
    positive source count, and a non-zero recomputed magnitude — a checked
    count alone is satisfiable over an empty window, because a stored zero
    equals a recomputed zero."""
    result = dedup.check(corpus, as_of=AS_OF, strict=True)

    assert result.failures == [], result.failures
    assert result.skips == [], result.skips
    assert result.executed() == set(dedup.INVARIANT_NAMES), (
        f"executed {sorted(result.executed())}, expected "
        f"{sorted(dedup.INVARIANT_NAMES)}")
    for name in dedup.INVARIANT_NAMES:
        assert result.checked[name] > 0, f"{name} checked nothing"
        assert result.source_counts[name] > 0, (
            f"{name} recomputed over zero source rows")
        assert result.recomputed_magnitude[name] > 0.0, (
            f"{name} recomputed to zero, which a stored zero satisfies "
            f"vacuously")


def test_strict_mode_fails_on_a_missing_database(scratch_corpus):
    (scratch_corpus / ".local/share/cctally/stats.db").unlink()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.skips == [], f"strict mode skipped instead of failing: {result.skips}"
    assert any("stats.db" in f for f in result.failures), result.failures
    assert result.executed() < set(dedup.INVARIANT_NAMES)

    # The paired live-mode arm is what makes the assertion above about STRICT
    # rather than about the condition. Without it the test would pass against a
    # validator that failed in both modes, and the two-mode split would be
    # untested.
    live = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert live.failures == [], live.failures
    assert any("stats.db" in s for s in live.skips), live.skips


def test_strict_mode_fails_on_an_unopenable_database(scratch_corpus):
    """A `mode=ro` connect against a directory raises the same
    `OperationalError` a concurrent writer produces, which live mode treats as
    a skip and strict mode must not."""
    path = scratch_corpus / ".local/share/cctally/cache.db"
    path.unlink()
    path.mkdir()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.skips == [], f"strict mode skipped instead of failing: {result.skips}"
    assert result.failures, "an unopenable cache.db produced no failure"

    live = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert live.failures == [], live.failures
    assert live.skips, "live mode neither failed nor skipped"


def test_strict_mode_fails_when_an_invariant_has_no_eligible_row(scratch_corpus):
    conn = _stats(scratch_corpus)
    try:
        conn.execute("DELETE FROM percent_milestones")
        conn.commit()
    finally:
        conn.close()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.skips == [], result.skips
    assert any("R-DEDUP5" in f for f in result.failures), result.failures
    assert result.checked["R-DEDUP5"] == 0

    live = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert live.failures == [], live.failures
    assert any("R-DEDUP5" in s for s in live.skips), live.skips


def test_strict_mode_fails_on_cache_drift(scratch_corpus):
    """Strict migration evidence becomes invalid after another fixture ingest;
    live replay evidence is independent of that cache provenance timestamp."""
    conn = _cache(scratch_corpus)
    try:
        conn.execute(
            "UPDATE session_files SET last_ingested_at = ?",
            ("2099-01-01T00:00:00Z",))
        conn.commit()
    finally:
        conn.close()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.skips == [], result.skips
    assert any("drift" in f for f in result.failures), result.failures

    live = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert live.failures == [], live.failures
    assert live.executed() == set(dedup.INVARIANT_NAMES), (
        f"live replay-fidelity checks were masked by unrelated cache ingest: "
        f"{live.skips}")


def test_live_replay_fidelity_rejects_a_self_consistent_reprice(scratch_corpus):
    """A durable fact and today's cache can be changed to agree with each
    other while both disagree with the selected journal event.

    The old stored-versus-live check accepts this shape.  Replay fidelity must
    reject it: changing current pricing inputs cannot rewrite a retained fact.
    """
    cache = _cache(scratch_corpus)
    try:
        changed = cache.execute(
            "UPDATE session_entries SET cost_usd_raw = 123.0 "
            "WHERE msg_id = 'msg_a1'").rowcount
        cache.commit()
    finally:
        cache.close()
    assert changed == 1

    stats = _stats(scratch_corpus)
    cache = _cache(scratch_corpus)
    try:
        rows = stats.execute(
            "SELECT id, range_start_iso, range_end_iso "
            "FROM weekly_cost_snapshots "
            "WHERE mode='auto' AND project IS NULL").fetchall()
        assert rows
        ctx = dedup._Context(scratch_corpus, AS_OF, strict=False)
        for rowid, start, end in rows:
            recomputed, _ = ctx.recompute(
                cache, dedup._utc(start), dedup._utc(end))
            stats.execute(
                "UPDATE weekly_cost_snapshots SET cost_usd=? WHERE id=?",
                (recomputed, rowid),
            )
        stats.commit()
    finally:
        stats.close()
        cache.close()

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert any(f.startswith("R-DEDUP2") for f in result.failures), (
        f"R-DEDUP2 accepted a materialized value that disagrees with its "
        f"selected journal event: {result.failures}")


def test_live_replay_fidelity_fails_closed_on_missing_selector_row(
    scratch_corpus,
):
    stats = _stats(scratch_corpus)
    try:
        journal_id = stats.execute(
            "SELECT journal_id FROM weekly_cost_snapshots "
            "WHERE mode='auto' AND project IS NULL LIMIT 1"
        ).fetchone()[0]
        removed = stats.execute(
            "DELETE FROM journal_effective_events WHERE event_id=?",
            (journal_id,),
        ).rowcount
        stats.commit()
    finally:
        stats.close()
    assert removed == 1

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert any(
        f.startswith("R-DEDUP2") and "selected retained event" in f
        for f in result.failures
    ), result.failures


def test_live_r_dedup5_uses_the_reset_segment_journal_fact(scratch_corpus):
    """A post-reset milestone is a retained segment fact, not a recomputation
    from the week's original lower bound.

    Give an existing milestone a nonzero reset identity and a segment-local
    durable cost.  The selected event is authoritative even though a live
    week-start recomputation intentionally returns a different number.
    """
    stats = _stats(scratch_corpus)
    try:
        row = stats.execute(
            "SELECT id, journal_id FROM percent_milestones "
            "WHERE percent_threshold=20").fetchone()
        assert row is not None
        milestone_id, _old_journal_id = row

        reset_journal_id = "test:week-reset:segment"
        cur = stats.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc,old_week_end_at,new_week_end_at,"
            " effective_reset_at_utc,account_key,journal_id) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-04-22T11:00:00Z", "2026-04-27T00:00:00Z",
             "2026-05-04T00:00:00Z", "2026-04-22T11:00:00Z",
             "unattributed", reset_journal_id),
        )
        reset_id = cur.lastrowid
        new_journal_id = "test:percent-milestone:segment"
        segment_cost = 0.123456789
        stats.execute(
            "UPDATE percent_milestones "
            "SET reset_event_id=?, cumulative_cost_usd=?, journal_id=? "
            "WHERE id=?",
            (reset_id, segment_cost, new_journal_id, milestone_id),
        )

        event = {
            "v": 1,
            "t": "evt",
            "id": new_journal_id,
            "rev": 0,
            "at": "2026-04-22T12:00:00Z",
            "payload": {
                "kind": "percent_milestone",
                "cumulative_cost_usd": segment_cost,
                "reset_event_ref": reset_journal_id,
            },
        }
        stats.execute(
            "INSERT INTO journal_effective_events "
            "(event_id,rev,status,content_hash,event_json) "
            "VALUES (?,0,'active','test-segment',?)",
            (new_journal_id, json.dumps(event, sort_keys=True)),
        )
        stats.commit()
    finally:
        stats.close()

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert not any(f.startswith("R-DEDUP5") for f in result.failures), (
        f"R-DEDUP5 recomputed across the reset boundary instead of checking "
        f"the selected segment fact: {result.failures}")
    assert result.checked["R-DEDUP5"] > 0


def test_live_r_dedup5_rejects_the_wrong_reset_segment_ref(scratch_corpus):
    stats = _stats(scratch_corpus)
    try:
        row = stats.execute(
            "SELECT journal_id, captured_at_utc, cumulative_cost_usd "
            "FROM percent_milestones WHERE percent_threshold=20"
        ).fetchone()
        assert row is not None
        journal_id, captured_at, cost = row
        event = {
            "v": 1,
            "t": "evt",
            "id": journal_id,
            "rev": 0,
            "at": captured_at,
            "payload": {
                "kind": "percent_milestone",
                "cumulative_cost_usd": cost,
                "reset_event_ref": "test:wrong-reset",
            },
        }
        changed = stats.execute(
            "UPDATE journal_effective_events "
            "SET content_hash='test-wrong-reset', event_json=? "
            "WHERE event_id=?",
            (json.dumps(event, sort_keys=True), journal_id),
        ).rowcount
        assert changed == 1
        stats.commit()
    finally:
        stats.close()

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert any(
        f.startswith("R-DEDUP5") and "reset segment" in f
        for f in result.failures
    ), result.failures


def test_live_r_dedup3_refuses_a_closed_time_eligible_block(scratch_corpus):
    stats = _stats(scratch_corpus)
    try:
        changed = stats.execute(
            "UPDATE five_hour_blocks SET is_closed=1 WHERE is_closed=0"
        ).rowcount
        stats.commit()
    finally:
        stats.close()
    assert changed == 1

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert result.checked["R-DEDUP3"] == 0
    assert any(
        s.startswith("R-DEDUP3") and "open 5h block" in s
        for s in result.skips
    ), result.skips


def test_strict_mode_fails_on_an_unapplied_migration(scratch_corpus):
    conn = _stats(scratch_corpus)
    try:
        conn.execute(
            "DELETE FROM schema_migrations WHERE name = ?",
            ("009_recompute_five_hour_blocks_dedup_fix",))
        conn.commit()
    finally:
        conn.close()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.skips == [], result.skips
    assert any("009_recompute_five_hour_blocks_dedup_fix" in f
               for f in result.failures), result.failures

    live = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert live.failures == [], live.failures
    assert any("009_recompute_five_hour_blocks_dedup_fix" in s
               for s in live.skips), live.skips


def test_strict_mode_requires_a_pinned_as_of(corpus):
    with pytest.raises(ValueError):
        dedup.check(corpus, as_of=None, strict=True)


def test_strict_mode_reads_only_the_root_it_is_given(corpus, monkeypatch):
    """The whole point of F12: no HOME, no password database, no ambient
    `CCTALLY_DATA_DIR`. A validator that consulted any of them would put the
    maintainer's real store back inside the suite."""
    monkeypatch.setenv("HOME", "/nonexistent-home-for-this-test")
    monkeypatch.setenv("CCTALLY_DATA_DIR", "/nonexistent-data-dir")
    result = dedup.check(corpus, as_of=AS_OF, strict=True)
    assert result.failures == [], result.failures
    assert result.executed() == set(dedup.INVARIANT_NAMES)


def test_strict_mode_fails_a_check_that_inspected_no_source_row(scratch_corpus):
    """A checked count above zero is not evidence. A window holding no
    `session_entries` recomputes to zero, a stored zero matches it exactly, and
    the invariant reports a healthy check over nothing."""
    conn = _stats(scratch_corpus)
    try:
        conn.execute(
            "DELETE FROM weekly_cost_snapshots "
            "WHERE mode = 'auto' AND project IS NULL")
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-04-27T00:05:00Z", "2026-01-05", "2026-01-12",
             "2026-01-05T00:00:00+00:00", "2026-01-12T00:00:00+00:00",
             0.0, "auto", None))
        conn.commit()
    finally:
        conn.close()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.checked["R-DEDUP2"] > 0, (
        "the empty window did not even reach the comparison, so this case "
        "cannot observe the guard it exists for")
    assert result.source_counts["R-DEDUP2"] == 0
    assert any("R-DEDUP2" in f and "zero source rows" in f
               for f in result.failures), result.failures


def test_strict_mode_fails_a_check_that_recomputed_to_zero(scratch_corpus):
    """The third non-vacuity condition, which the case above cannot reach.

    `test_strict_mode_fails_a_check_that_inspected_no_source_row` trips the
    SECOND condition, and `_enforce_non_vacuity` `continue`s there, so without
    this case the `recomputed_magnitude <= 0.0` arm is never executed by the
    suite and would survive deletion. Here the window keeps its source rows, so
    the second condition passes, and every one of those rows prices to zero —
    which the stored zero matches exactly, and which is precisely the shape a
    checked count cannot distinguish from a healthy check.
    """
    conn = _cache(scratch_corpus)
    try:
        # `cost_usd_raw` has to go too: `mode="auto"` prefers a recorded cost
        # over the computed one, so a surviving recorded value would keep the
        # magnitude positive and this case would not reach the third condition.
        conn.execute(
            "UPDATE session_entries SET input_tokens = 0, output_tokens = 0, "
            "cache_create_tokens = 0, cache_read_tokens = 0, "
            "cost_usd_raw = NULL")
        conn.commit()
    finally:
        conn.close()
    conn = _stats(scratch_corpus)
    try:
        changed = conn.execute(
            "UPDATE weekly_cost_snapshots SET cost_usd = 0.0 "
            "WHERE mode = 'auto' AND project IS NULL").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 2, f"expected the two in-scope rows, matched {changed}"

    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert result.checked["R-DEDUP2"] > 0, (
        "the zeroed window never reached the comparison, so this case cannot "
        "observe the guard it exists for")
    assert result.source_counts["R-DEDUP2"] > 0, (
        "the second condition fires first, so this case would not reach the "
        "third")
    assert result.recomputed_magnitude["R-DEDUP2"] == 0.0
    assert any("R-DEDUP2" in f and "vacuously" in f
               for f in result.failures), result.failures


def test_live_mode_rejects_an_unjournaled_durable_row(scratch_corpus):
    """A durable family row with no selected event cannot be replay-faithful."""
    conn = _stats(scratch_corpus)
    try:
        conn.execute(
            "DELETE FROM weekly_cost_snapshots "
            "WHERE mode = 'auto' AND project IS NULL")
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " range_start_iso, range_end_iso, cost_usd, mode, project) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-01-12T00:05:00Z", "2026-01-05", "2026-01-12",
             "2026-01-05T00:00:00+00:00", "2026-01-12T00:00:00+00:00",
             0.0, "auto", None))
        conn.commit()
    finally:
        conn.close()
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=False)
    assert any(
        f.startswith("R-DEDUP2") and "journal_id" in f
        for f in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# Task 14 — the mutation matrix
# --------------------------------------------------------------------------
#
# Each mutation corrupts ONE invariant's discriminator in the output a
# production path produced, never a value the builder wrote. A mutation applied
# to a builder-authored expectation would test the comparison operator and
# nothing else; the same mutation applied to what the ingest or a migration
# produced tests the path that produced it.
#
# Both halves of each case are required. Asserting only that the named invariant
# fails proves that something broke; asserting that the other four do not is
# what proves the five are separate instruments rather than one guard wearing
# five names.


def _mutate_r_dedup1(root):
    """The deduped token total for the one key no cost window covers.

    `session_entries.output_tokens` is written by `sync_cache`, and lowering it
    below the maximum raw emission is exactly the `_should_replace` regression
    R-DEDUP1 exists to catch. The row chosen is the 2026-04-19 pair, which sits
    before the subscription week, before every block and before every milestone,
    so no cost recomputation reads it.
    """
    conn = _cache(root)
    try:
        changed = conn.execute(
            "UPDATE session_entries SET output_tokens = output_tokens - 1 "
            "WHERE msg_id = 'msg_d1'").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, f"the R-DEDUP1 mutation matched {changed} rows"


def _mutate_r_dedup2(root):
    """`weekly_cost_snapshots.cost_usd`, written by migration 008."""
    conn = _stats(root)
    try:
        changed = conn.execute(
            "UPDATE weekly_cost_snapshots SET cost_usd = cost_usd + 0.01 "
            "WHERE mode = 'auto' AND project IS NULL "
            "  AND range_end_iso = '2026-04-27T00:00:00+00:00'").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, f"the R-DEDUP2 mutation matched {changed} rows"


def _mutate_r_dedup3(root):
    """The ACTIVE block's `total_cost_usd`, written by migration 009.

    R-DEDUP4 selects `is_closed = 1`, so it cannot see this row.
    """
    conn = _stats(root)
    try:
        changed = conn.execute(
            "UPDATE five_hour_blocks "
            "SET total_cost_usd = total_cost_usd + 0.01 "
            "WHERE is_closed = 0").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, f"the R-DEDUP3 mutation matched {changed} rows"


def _mutate_r_dedup4(root):
    """A CLOSED block's `total_cost_usd`, written by migration 009.

    R-DEDUP3 only ever looks at the block that is active at `as_of`, so it
    cannot see this row.
    """
    conn = _stats(root)
    try:
        changed = conn.execute(
            "UPDATE five_hour_blocks "
            "SET total_cost_usd = total_cost_usd + 0.01 "
            "WHERE is_closed = 1 "
            "  AND block_start_at = '2026-04-21T09:00:00+00:00'").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, f"the R-DEDUP4 mutation matched {changed} rows"


def _mutate_r_dedup5(root):
    """`percent_milestones.cumulative_cost_usd`, written by migration 010."""
    conn = _stats(root)
    try:
        changed = conn.execute(
            "UPDATE percent_milestones "
            "SET cumulative_cost_usd = cumulative_cost_usd + 0.01 "
            "WHERE percent_threshold = 20").rowcount
        conn.commit()
    finally:
        conn.close()
    assert changed == 1, f"the R-DEDUP5 mutation matched {changed} rows"


MUTATIONS = {
    "R-DEDUP1": _mutate_r_dedup1,
    "R-DEDUP2": _mutate_r_dedup2,
    "R-DEDUP3": _mutate_r_dedup3,
    "R-DEDUP4": _mutate_r_dedup4,
    "R-DEDUP5": _mutate_r_dedup5,
}


def _failed_names(result):
    return {
        name for name in dedup.INVARIANT_NAMES
        if any(f.startswith(name) for f in result.failures)
    }


@pytest.mark.parametrize("target", dedup.INVARIANT_NAMES)
def test_each_invariant_observes_its_own_discriminator(target, scratch_corpus):
    MUTATIONS[target](scratch_corpus)
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    failed = _failed_names(result)
    assert target in failed, (
        f"{target} did not observe a mutation of its own discriminator; "
        f"failures were {result.failures}")
    assert failed == {target}, (
        f"mutating {target}'s discriminator also reddened "
        f"{sorted(failed - {target})}, so the invariants are not independent; "
        f"failures were {result.failures}")


def test_the_unmutated_corpus_fails_nothing(scratch_corpus):
    """The control the five cases above are read against. Without it, a
    validator that failed everything unconditionally would satisfy the first
    half of every mutation case."""
    result = dedup.check(scratch_corpus, as_of=AS_OF, strict=True)
    assert _failed_names(result) == set(), result.failures
