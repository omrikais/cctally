#!/usr/bin/env python3
"""Builder: the input-only R-DEDUP1..5 corpus (#529 S4 F12).

Writes ONE coherent fake HOME under ``--out`` (default
``tests/fixtures/dedup/``):

  * ``.claude/projects/**/*.jsonl`` — duplicate-bearing streaming pairs, the
    only hand-authored numbers in the corpus.
  * ``.local/share/cctally/cache.db`` — built by the REAL ``open_cache_db`` +
    ``sync_cache``, so its ``session_entries`` are whatever the v1.12.0
    ccusage-parity dedup ingest decides they are.
  * ``.local/share/cctally/stats.db`` — seeded with deliberately ABSURD
    pre-dedup values and with migrations 008/009/010 marked pending, then
    opened through the real dispatcher so those three recompute handlers
    overwrite every value the invariants read, then rebuilt from the resulting
    journal so live-mode checks consume the real effective-event selector.
  * ``input.env`` — the pinned ``AS_OF``. ``bin/cctally-reconcile-test`` sources
    this file, so it is shell input rather than data.

The stats-side rows are hand-authored scaffolding
-------------------------------------------------
``_seed_pre_migration_stats`` writes ``weekly_cost_snapshots``,
``five_hour_blocks`` and ``percent_milestones`` with raw ``INSERT`` and
``executemany``. ``_cctally_milestones.insert_cost_snapshot``,
``_cctally_five_hour._backfill_five_hour_blocks`` and
``_cctally_milestones.insert_percent_milestone`` never run here. §6.2 of the
design record originally asked for two production layers on this side, the real
writers AND the migrations; only the migrations are real, and that section now
records the correction.

Every value an invariant COMPARES is still input-only, because migration 008,
009 or 010 overwrites each one, so this is not the vacuity failure §6.2 exists to
prevent. What it does cost is that a defect in how a real writer computes a
window bound, a ``captured_at_utc`` or an ``is_closed`` flag cannot be observed
from this corpus.

The improvement, deliberately not made in #529 S4: seed each row through its
real writer and then overwrite only the cost column with the absurd pre-dedup
value, which keeps the recompute proof and additionally puts the writers' own
bound arithmetic under test. It is recorded here rather than built because it is
scope beyond E1.

Why input-only
--------------
``bin/_fixture_builders.create_stats_db`` / ``create_cache_db`` write the full
current schema with every migration applied, and the module offers direct row
seeders. A corpus seeded that way would have each invariant compare a seeded
value against a recomputation over seeded values: all five would execute, all
five would pass, all five mutation tests would still fail exactly the right
invariant, and not one line of production dedup, cost-writer or migration code
would have run. ``create_stats_db`` is therefore used here for SCHEMA ONLY —
no invariant reads a schema — and every value an invariant checks is produced
by ``sync_cache`` or by migration 008, 009 or 010.

Deliberately absurd seeds
-------------------------
The seeded ``cost_usd`` / ``total_cost_usd`` / ``cumulative_cost_usd`` values
are three orders of magnitude above anything this corpus can recompute to. If a
recompute migration silently defers, the seeded value survives, ``verify()``
below refuses to write the corpus, and the harness never sees a green built on
an unmigrated store.

NOT cache-wired
---------------
This builder is invoked directly by ``bin/cctally-reconcile-test``, never
through ``build_fixtures_cached``. It runs the real ingest, the real dispatcher
and the real cutover, each of which stamps wall-clock instants into both stores
and into the journal segments the cutover exports, so its output is not
byte-reproducible and it cannot satisfy the cache's content-manifest contract
(``tests/test_fixture_cache.py``). ``bin/build-codex-reader-fixtures.py`` and
``bin/build-bench-fixtures.py`` are the same class of builder for the same
reason.

Nothing here is committed: ``tests/fixtures/dedup/`` carries only a tracked
``.gitignore``.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "dedup"

# ── Pinned instants ────────────────────────────────────────────────────────
# AS_OF sits inside the active 5h block's window, which is what makes R-DEDUP3
# decidable without a wall-clock read. Everything else is far enough behind it
# that no invariant depends on the day the corpus is built.
AS_OF = "2026-04-27T12:00:00Z"

WEEK_START = "2026-04-20T00:00:00+00:00"
WEEK_END = "2026-04-27T00:00:00+00:00"

# One fixed mtime for every JSONL, so `session_files.mtime_ns` does not carry
# the build instant.
JSONL_MTIME_NS = 1776672000_000_000_000  # 2026-04-20T00:00:00Z

# Provenance instants, pinned by `_pin_provenance_timestamps`. The ordering is
# the whole point: the last entry lands at 10:15, the ingest walks at 10:55, the
# active block records its last observation at 11:00, and the recompute
# migrations run at 11:45. Every cache-drift baseline in both modes therefore
# sits AFTER the ingest, so no invariant skips for drift on an undisturbed
# corpus — including R-DEDUP3, whose live-mode baseline is the block's own last
# tick rather than a migration stamp.
INGESTED_AT = "2026-04-27T10:55:00Z"
RECOMPUTED_AT = "2026-04-27T11:45:00Z"

MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"

# The four seeded pre-dedup values — one per table an invariant reads, plus a
# second, narrower weekly window. Absurd by construction; see the module
# docstring.
STALE_WEEK_COST = 9_999.0
STALE_NARROW_COST = 8_888.0
STALE_BLOCK_COST = 7_777.0
STALE_MILESTONE_COST = 6_666.0
# A `mode='display'` row and a per-project row are OUT of migration 008's scope
# and out of R-DEDUP2's. They stay at their seeded value on purpose: if either
# the migration or the invariant ever widened its scope, this corpus turns red.
PRESERVED_DISPLAY_COST = 5_555.0
PRESERVED_PROJECT_COST = 4_444.0

GITIGNORE_BODY = """\
# Everything under tests/fixtures/dedup/ is generated by
# bin/build-dedup-fixtures.py, which runs the real ingest and the real
# recompute migrations and therefore produces host-dependent bytes. The
# reconcile harness always rebuilds it into a scratch --out dir, so nothing
# here is committed except this file.
*
!.gitignore
"""

# ── Raw JSONL corpus ───────────────────────────────────────────────────────
# (project dir, session file, [(msg_id, req_id, model, day-time, intermediate,
#  final, cache_create, cache_read, input)])
#
# Every logical call is emitted TWICE — the streaming intermediate
# (output_tokens=1, no `speed`) and the post-stream finalization. The ingest
# must collapse each pair to the higher-token row; R-DEDUP1 is exactly that
# claim, checked against the raw file.
#
# No entry carries a cache-creation TTL split, and no entry carries
# `speed='fast'`: `cache_1h_tokens` is never passed to `emit_streaming_pair`, and
# that helper writes `speed="standard"`, whose multiplier is 1.0.
#
# That shape is what the corpus is, not a divergence it accommodates. R-DEDUP2..5
# now recompute through `claude_usage_dict` over the same nine columns migrations
# 008/009/010 read, so an entry carrying either shape would price identically on
# both sides. What the shape does mean is that this corpus CANNOT observe either
# axis: a green suite is not evidence that the recomputation reads the split or
# the effective tier. That evidence is the structural scan in
# tests/test_cost_usage_dict_chokepoint.py plus the read-only measurements
# against a real store recorded in bin/_lib_dedup_invariants.py's header — where
# the same `msg_a1` row prices at 0.110435 without the two columns, 0.116060 with
# the 1-hour split, and 0.662610 at the fast multiplier.
#
# One pair sits outside EVERY cost window. Without it, R-DEDUP1 has no
# independent mutation: its discriminator is the deduped token total, which is
# also the input to R-DEDUP2..5's recomputation, so lowering the tokens of any
# in-window entry reddens two invariants at once and the mutation matrix stops
# proving the five are separate instruments.
PAIRS = (
    # projA / sessD — before the subscription week, before every block, before
    # every milestone. Reachable by R-DEDUP1 alone. See the note above.
    ("projA", "sessD", "msg_d1", "req_d1", MODEL_OPUS,
     "2026-04-19T08:00:00", 1, 900, 200, 300, 3),
    # projA / sessA — inside closed block 1 and inside the subscription week.
    ("projA", "sessA", "msg_a1", "req_a1", MODEL_OPUS,
     "2026-04-21T10:00:00", 1, 4000, 1500, 2000, 12),
    ("projA", "sessA", "msg_a2", "req_a2", MODEL_OPUS,
     "2026-04-21T13:15:00", 1, 2500, 600, 800, 9),
    # projB / sessB — inside closed block 2 and inside the subscription week.
    ("projB", "sessB", "msg_b1", "req_b1", MODEL_SONNET,
     "2026-04-22T09:30:00", 1, 3100, 900, 1200, 7),
    ("projB", "sessB", "msg_b2", "req_b2", MODEL_SONNET,
     "2026-04-22T11:05:00", 1, 1800, 400, 500, 5),
    # projA / sessC — inside the ACTIVE block, and AFTER the subscription week
    # ends, so it feeds R-DEDUP3 without moving R-DEDUP2 or R-DEDUP5.
    ("projA", "sessC", "msg_c1", "req_c1", MODEL_OPUS,
     "2026-04-27T09:30:00", 1, 2200, 700, 900, 6),
    ("projA", "sessC", "msg_c2", "req_c2", MODEL_SONNET,
     "2026-04-27T10:15:00", 1, 1400, 300, 400, 4),
)

# (window_key, resets_at, block_start_at, first_obs, last_obs, is_closed)
BLOCKS = (
    (1777129200, "2026-04-21T14:00:00Z", "2026-04-21T09:00:00+00:00",
     "2026-04-21T09:05:00Z", "2026-04-21T13:55:00Z", 1),
    (1777215600, "2026-04-22T14:00:00Z", "2026-04-22T09:00:00+00:00",
     "2026-04-22T09:05:00Z", "2026-04-22T13:55:00Z", 1),
    # ACTIVE at AS_OF: 09:00 + 5h = 14:00, and AS_OF is 12:00.
    (1777647600, "2026-04-27T14:00:00Z", "2026-04-27T09:00:00+00:00",
     "2026-04-27T09:05:00Z", "2026-04-27T11:00:00Z", 0),
)

# (threshold, captured_at_utc)
MILESTONES = (
    (10, "2026-04-21T14:00:00Z"),
    (20, "2026-04-22T12:00:00Z"),
)


def _pin_environment(home: Path) -> None:
    """Point every path cctally resolves at the corpus root, BEFORE any cctally
    module is imported — `_cctally_core` captures the layout at import time.

    Setting HOME as well as the two explicit overrides is what makes the
    builder's output independent of the environment it was launched from:
    `bin/cctally`'s `_get_claude_data_dirs` falls back to `Path.home()`, and a
    builder that inherited the maintainer's HOME would ingest the maintainer's
    transcripts into the fixture.
    """
    os.environ["HOME"] = str(home)
    os.environ["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_UPDATE_CHECK"] = "1"
    os.environ["CCTALLY_DISABLE_RETENTION_SWEEP"] = "1"
    os.environ["CCTALLY_DISABLE_TELEMETRY"] = "1"
    os.environ["CCTALLY_AS_OF"] = AS_OF
    os.environ["TZ"] = "Etc/UTC"
    os.environ.pop("CODEX_HOME", None)
    time.tzset()


def _load_cctally():
    """Path-load the extensionless ``bin/cctally`` as module ``"cctally"``.

    A plain ``import cctally`` cannot find an extensionless file, and
    ``_cctally_cache._cctally()`` resolves ``sys.modules["cctally"]`` at call
    time, so the sibling modules are unusable until this registration happens.
    Same idiom as ``bin/build-codex-reader-fixtures.py``. Must run AFTER
    ``_pin_environment``.
    """
    # Both submodules are imported explicitly. `import importlib` alone does
    # NOT bind `importlib.machinery`, and this builder ran green under pytest
    # while failing from a plain shell precisely because the test-session
    # bootstrap had already imported it into the shared package namespace.
    import importlib.machinery
    import importlib.util

    cached = sys.modules.get("cctally")
    if cached is not None:
        return cached
    loader = importlib.machinery.SourceFileLoader(
        "cctally", str(BIN_DIR / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    return module


def _write_jsonl(home: Path) -> list[Path]:
    from _fixture_builders import emit_streaming_pair

    projects = home / ".claude" / "projects"
    written: dict[str, Path] = {}
    for (
        project, session, msg_id, req_id, model, when,
        intermediate, final, cache_create, cache_read, inp,
    ) in PAIRS:
        slug = f"-Users-fixture-{project}"
        directory = projects / slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session}.jsonl"
        written.setdefault(str(path), path)
        emit_streaming_pair(
            path,
            model=model,
            msg_id=msg_id,
            req_id=req_id,
            ts_intermediate=f"{when}.100Z",
            ts_final=f"{when}.500Z",
            intermediate_output_tokens=intermediate,
            final_output_tokens=final,
            cache_create_tokens=cache_create,
            cache_read_tokens=cache_read,
            input_tokens=inp,
            session_id=session,
            cwd=f"/Users/fixture/{project}",
            append=True,
        )
    paths = sorted(written.values())
    for path in paths:
        os.utime(path, ns=(JSONL_MTIME_NS, JSONL_MTIME_NS))
    return paths


def _seed_pre_migration_stats(stats_path: Path) -> None:
    """Write the stats.db the dispatcher will correct.

    ``create_stats_db`` supplies the SCHEMA only. ``stamp_all_stats_migrations_
    applied`` then marks the whole registry applied, and the three recompute
    markers are deleted again so exactly 008, 009 and 010 are pending —
    running only the migrations under test rather than replaying eleven
    handlers against a schema that already has their output.
    """
    from _fixture_builders import (
        create_stats_db, stamp_all_stats_migrations_applied,
    )

    create_stats_db(stats_path)
    conn = sqlite3.connect(stats_path)
    try:
        stamp_all_stats_migrations_applied(conn)
        conn.execute(
            "DELETE FROM schema_migrations WHERE name IN (?, ?, ?)",
            (
                "008_recompute_weekly_cost_snapshots_dedup_fix",
                "009_recompute_five_hour_blocks_dedup_fix",
                "010_recompute_percent_milestones_dedup_fix",
            ),
        )
        # Below the registry head, so the dispatcher walks rather than
        # fast-pathing; the markers above decide WHICH handlers actually run.
        conn.execute("PRAGMA user_version = 7")

        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-04-22T12:00:00Z", "2026-04-20", "2026-04-27",
             WEEK_START, WEEK_END, 20.0, "statusline", json.dumps({})),
        )
        conn.executemany(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, range_start_iso, range_end_iso, cost_usd, mode, "
            " project) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-04-27T00:05:00Z", "2026-04-20", "2026-04-27",
                 WEEK_START, WEEK_END, WEEK_START, WEEK_END,
                 STALE_WEEK_COST, "auto", None),
                # A second in-scope window, so R-DEDUP2's checked count can
                # exceed one and a mutation of one row cannot be mistaken for
                # the invariant having only ever looked at one.
                ("2026-04-22T00:05:00Z", "2026-04-20", "2026-04-27",
                 WEEK_START, WEEK_END, WEEK_START,
                 "2026-04-22T00:00:00+00:00",
                 STALE_NARROW_COST, "auto", None),
                ("2026-04-27T00:05:00Z", "2026-04-20", "2026-04-27",
                 WEEK_START, WEEK_END, WEEK_START, WEEK_END,
                 PRESERVED_DISPLAY_COST, "display", None),
                ("2026-04-27T00:05:00Z", "2026-04-20", "2026-04-27",
                 WEEK_START, WEEK_END, WEEK_START, WEEK_END,
                 PRESERVED_PROJECT_COST, "auto", "projA"),
            ],
        )
        conn.executemany(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, total_cost_usd, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (key, resets, start, first, last, 42.0, STALE_BLOCK_COST,
                 closed, first, last)
                for key, resets, start, first, last, closed in BLOCKS
            ],
        )
        conn.executemany(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (captured, "2026-04-20", "2026-04-27", WEEK_START, WEEK_END,
                 threshold, STALE_MILESTONE_COST, STALE_MILESTONE_COST, 1, 1)
                for threshold, captured in MILESTONES
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run_production_ingest() -> int:
    """Real ``open_cache_db`` + real ``sync_cache`` over the corpus JSONL.

    This is the only writer of ``session_entries`` in the corpus, so R-DEDUP1
    compares the raw file against what the production dedup path decided.
    """
    from _cctally_cache import open_cache_db, sync_cache

    conn = open_cache_db()
    try:
        sync_cache(conn)
        return conn.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone()[0]
    finally:
        conn.close()


def _run_recompute_migrations(stats_path: Path) -> set[str]:
    """Open stats.db through the real dispatcher until 008/009/010 land.

    The recompute gate can defer once while the cache dispatcher settles, so
    the open is retried — the same absorb-a-transient-deferral loop
    ``tests/test_migration_ancient_to_head.py`` uses.
    """
    import _cctally_core

    wanted = {
        "008_recompute_weekly_cost_snapshots_dedup_fix",
        "009_recompute_five_hour_blocks_dedup_fix",
        "010_recompute_percent_milestones_dedup_fix",
    }
    applied: set[str] = set()
    for _ in range(5):
        conn = _cctally_core.open_db()
        try:
            applied = {
                row[0] for row in
                conn.execute("SELECT name FROM schema_migrations")
            }
        finally:
            conn.close()
        if wanted <= applied:
            break
    return applied


def _run_stats_rebuild(stats_path: Path) -> None:
    """Publish durable facts through the selector and restore the open projection.

    The open block is deliberately absent from the journal. Because this
    fixture's pinned block is historical relative to wall clock, rebuild does
    not re-materialize it. Preserve the migration-009-produced projection
    across the rebuild so strict R-DEDUP3 keeps its independent discriminator.
    """
    from _cctally_journal import RebuildContext, rebuild_stats_index
    from _cctally_store import stats_write_scope

    before = sqlite3.connect(stats_path)
    before.row_factory = sqlite3.Row
    try:
        open_row = before.execute(
            "SELECT * FROM five_hour_blocks WHERE is_closed=0"
        ).fetchone()
    finally:
        before.close()
    if open_row is None:
        raise SystemExit("build-dedup-fixtures: no open projection before rebuild")
    projection = {
        key: open_row[key]
        for key in open_row.keys()
        if key not in {"id", "journal_id"}
    }

    # This standalone builder is the one serialized writer for its private
    # corpus, equivalent to pytest's in-process fixture sanction.
    with stats_write_scope("dedup-fixture-rebuild"):
        rebuild_stats_index(
            # Shipped bin/*.py callers must use a production incident identity;
            # this is the fixture-root equivalent of an explicit `db rebuild`.
            context=RebuildContext(trigger="db-rebuild"),
            update_quota_cache=False,
        )

    rebuilt = sqlite3.connect(stats_path)
    try:
        columns = tuple(projection)
        rebuilt.execute(
            f"INSERT INTO five_hour_blocks ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(projection[column] for column in columns),
        )
        rebuilt.commit()
    finally:
        rebuilt.close()


def _pin_provenance_timestamps(home: Path) -> None:
    """Give the cache-drift baseline a decidable ordering.

    The drift guard asks whether any session file was tail-ingested after the
    recompute baseline, comparing ``session_files.last_ingested_at`` against the
    recompute migration's ``applied_at_utc`` as strings. Both are wall-clock
    here and land in the same second, so the answer would rest on whether
    ``'.'`` sorts before ``'Z'`` — true today, and not something a fixture
    should depend on. Pinning both to instants a quarter of an hour apart makes
    the comparison mean what it says: the ingest happened, then the recompute.

    Neither value is read by any invariant's comparison; both are provenance.
    ``bin/build-migrations-fixtures.py`` pins the same marker for the same
    reason (``TS_008_APPLIED``).
    """
    share = home / ".local" / "share" / "cctally"
    cache = sqlite3.connect(share / "cache.db")
    try:
        cache.execute(
            "UPDATE session_files SET last_ingested_at = ?", (INGESTED_AT,))
        cache.commit()
    finally:
        cache.close()
    stats = sqlite3.connect(share / "stats.db")
    try:
        stats.execute(
            "UPDATE schema_migrations SET applied_at_utc = ? "
            "WHERE name IN (?, ?, ?)",
            (RECOMPUTED_AT,
             "008_recompute_weekly_cost_snapshots_dedup_fix",
             "009_recompute_five_hour_blocks_dedup_fix",
             "010_recompute_percent_milestones_dedup_fix"),
        )
        stats.commit()
    finally:
        stats.close()


def _quiesce_stores(home: Path) -> None:
    """Fold each store's WAL back into its main file and drop the sidecars.

    The validator connects ``file:<path>?mode=ro``, and a WAL-mode SQLite
    database can only be read through its ``-shm`` file, which a read-only
    connection may not create. Leaving a live WAL behind would therefore make
    the corpus openable by the builder and not by the thing that consumes it.
    """
    share = home / ".local" / "share" / "cctally"
    for name in ("cache.db", "stats.db"):
        path = share / name
        if not path.is_file():
            continue
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        finally:
            conn.close()


def verify(home: Path, entry_count: int, applied: set[str]) -> None:
    """Refuse to publish a corpus that would certify the builder's arithmetic.

    Two guards the spec calls for, plus the migration-marker check:

      * a positive SOURCE count — a recomputation over zero ``session_entries``
        agrees with a stored zero and reports a healthy check over nothing;
      * a non-zero RECOMPUTED value — same reason, one layer up.
    """
    stats_path = home / ".local" / "share" / "cctally" / "stats.db"
    missing = sorted({
        "008_recompute_weekly_cost_snapshots_dedup_fix",
        "009_recompute_five_hour_blocks_dedup_fix",
        "010_recompute_percent_milestones_dedup_fix",
    } - applied)
    if missing:
        raise SystemExit(
            "build-dedup-fixtures: the recompute migrations never applied "
            f"({', '.join(missing)}); the corpus still carries its seeded "
            "pre-dedup values and would certify nothing")

    if entry_count <= 0:
        raise SystemExit(
            "build-dedup-fixtures: sync_cache ingested no session_entries")
    raw = len(PAIRS) * 2
    if entry_count >= raw:
        raise SystemExit(
            f"build-dedup-fixtures: {entry_count} session_entries for {raw} raw "
            "emissions — the dedup ingest did not collapse the streaming pairs")

    conn = sqlite3.connect(stats_path)
    try:
        checks = (
            ("weekly_cost_snapshots", "cost_usd",
             "SELECT cost_usd FROM weekly_cost_snapshots "
             "WHERE mode = 'auto' AND project IS NULL"),
            ("five_hour_blocks", "total_cost_usd",
             "SELECT total_cost_usd FROM five_hour_blocks"),
            ("percent_milestones", "cumulative_cost_usd",
             "SELECT cumulative_cost_usd FROM percent_milestones"),
        )
        for table, column, query in checks:
            values = [row[0] for row in conn.execute(query)]
            if not values:
                raise SystemExit(
                    f"build-dedup-fixtures: {table} holds no eligible row")
            if any(value > 1_000.0 for value in values):
                raise SystemExit(
                    f"build-dedup-fixtures: {table}.{column} still carries a "
                    f"seeded pre-dedup value {values} — the recompute did not "
                    "run over this row")
            if not any(value > 0.0 for value in values):
                raise SystemExit(
                    f"build-dedup-fixtures: every {table}.{column} recomputed "
                    "to zero, which a stored zero would satisfy vacuously")
        preserved = {
            row[0] for row in conn.execute(
                "SELECT cost_usd FROM weekly_cost_snapshots "
                "WHERE mode = 'display' OR project IS NOT NULL")
        }
        if preserved != {PRESERVED_DISPLAY_COST, PRESERVED_PROJECT_COST}:
            raise SystemExit(
                "build-dedup-fixtures: migration 008 rewrote a row outside its "
                f"documented scope (mode='auto' AND project IS NULL): "
                f"{sorted(preserved)}")
    finally:
        conn.close()


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    _pin_environment(out)
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))

    (out / ".local" / "share" / "cctally").mkdir(parents=True)
    _write_jsonl(out)

    cctally = _load_cctally()
    # CCTALLY_DATA_DIR is captured at import, so re-resolve in case an earlier
    # import in this process already bound the globals elsewhere.
    cctally._cctally_core._init_paths_from_env()

    stats_path = out / ".local" / "share" / "cctally" / "stats.db"
    _seed_pre_migration_stats(stats_path)

    entry_count = _run_production_ingest()
    applied = _run_recompute_migrations(stats_path)
    _run_stats_rebuild(stats_path)
    _pin_provenance_timestamps(out)
    _quiesce_stores(out)
    verify(out, entry_count, applied)

    (out / "input.env").write_text(f"AS_OF={AS_OF}\n", encoding="utf-8")
    (out / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help="Output directory; the corpus root IS a fake HOME. Defaults to "
             "the in-tree tests/fixtures/dedup/, which is gitignored.")
    args = parser.parse_args()
    out = args.out.resolve()
    build(out)
    print(f"Built the dedup corpus under {out}")


if __name__ == "__main__":
    main()
