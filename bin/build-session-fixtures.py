#!/usr/bin/env python3
"""Build seeded SQLite fixtures for `cctally session`.

Writes one pair of (stats.db, cache.db) per scenario under
tests/fixtures/session/<scenario>/.local/share/cctally/.
All schema/seeding goes through bin/_fixture_builders.py — do not duplicate
schema here. Idempotent: each builder overwrites existing DBs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    FIXED_LAST_INGESTED_AT,
    create_cache_db,
    create_stats_db,
    seed_session_entry,
    seed_session_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/session"


def _iso(ts: dt.datetime) -> str:
    """Serialize a datetime as UTC-ISO with `Z` suffix, seconds precision."""
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_single_session_single_file():
    """Scenario: one session_files row + multiple session_entries on one
    source_path. No resume, no cwd change. Locks the baseline rendering
    of `cmd_session` for both terminal and --json modes."""
    scenario_dir = FIXTURES_DIR / "single-session-single-file"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Stats.db is pre-seeded (empty) only to bake-in migrations and prevent
    # first-open stderr noise polluting goldens — cmd_session never reads it.
    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        seed_session_file(
            conn,
            path="/fake/jsonl/sssf-session.jsonl",
            session_id="sssf-baseline-session-uuid",
            project_path="/fake/repos/baseline",
        )
        # Three entries spread across one day. Distinct token counts produce
        # a non-zero, deterministic Cost (USD) under CLAUDE_MODEL_PRICING.
        for i, (hours_back, model, input_t, output_t, cache_read) in enumerate([
            (10, "claude-opus-4-7",   400_000, 40_000,  0),
            ( 6, "claude-opus-4-7",   200_000, 20_000, 50_000),
            ( 2, "claude-sonnet-4-6", 100_000, 10_000,  0),
        ]):
            seed_session_entry(
                conn,
                source_path="/fake/jsonl/sssf-session.jsonl",
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read=cache_read,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_resumed_across_files():
    """Scenario: one sessionId, two session_files rows (different paths,
    same project_path). Entries on each path. Verifies the resume-merge
    path in `_aggregate_claude_sessions` collapses two source_paths into
    one output row whose `sourcePaths` JSON field is a 2-element array."""
    scenario_dir = FIXTURES_DIR / "resumed-across-files"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Two session_files rows: SAME session_id, DIFFERENT paths,
        # SAME project_path (no cwd change in this scenario).
        seed_session_file(
            conn,
            path="/fake/jsonl/raf-original.jsonl",
            session_id="raf-resumed-session-uuid",
            project_path="/fake/repos/resume",
        )
        seed_session_file(
            conn,
            path="/fake/jsonl/raf-resumed.jsonl",
            session_id="raf-resumed-session-uuid",
            project_path="/fake/repos/resume",
        )
        # Entries: 2 on the original file (older), 2 on the resumed file
        # (newer). Token counts differ across files so the merge math is
        # observable in the totals.
        for i, (src, hours_back, input_t, output_t) in enumerate([
            ("/fake/jsonl/raf-original.jsonl", 30, 300_000, 30_000),
            ("/fake/jsonl/raf-original.jsonl", 26, 100_000, 10_000),
            ("/fake/jsonl/raf-resumed.jsonl",   8, 250_000, 25_000),
            ("/fake/jsonl/raf-resumed.jsonl",   2, 150_000, 15_000),
        ]):
            seed_session_entry(
                conn,
                source_path=src,
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model="claude-opus-4-7",
                input_tokens=input_t,
                output_tokens=output_t,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_resumed_cwd_changed():
    """Scenario: one sessionId, two session_files rows with DIFFERENT
    project_path values (user `cd`'d between resumes). Verifies the
    `Directory`-column tie-breaker in `_aggregate_claude_sessions`
    (lines 3126-3131): the most-recent entry's project_path wins.

    Seeding constraint: the entry with the latest timestamp MUST live in
    the file whose project_path we expect to appear in the golden's
    Directory cell. Below: the resumed file (`/fake/repos/after-cd`) holds
    the most-recent entry, so its project_path wins. If you flip the
    timestamp ordering, the golden will show `/fake/repos/before-cd`
    instead — that would be a different (also-correct) regression anchor."""
    scenario_dir = FIXTURES_DIR / "resumed-cwd-changed"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Two session_files rows: SAME session_id, DIFFERENT paths,
        # DIFFERENT project_path (the cwd change between resumes).
        seed_session_file(
            conn,
            path="/fake/jsonl/rcc-original.jsonl",
            session_id="rcc-resumed-session-uuid",
            project_path="/fake/repos/before-cd",
        )
        seed_session_file(
            conn,
            path="/fake/jsonl/rcc-resumed.jsonl",
            session_id="rcc-resumed-session-uuid",
            project_path="/fake/repos/after-cd",
        )
        # Entries: 2 on original (older), 2 on resumed (newer).
        # The LATEST entry (as_of - 2h) is on the resumed file, so its
        # project_path (`/fake/repos/after-cd`) wins the Directory cell.
        for i, (src, hours_back, input_t, output_t) in enumerate([
            ("/fake/jsonl/rcc-original.jsonl", 30, 200_000, 20_000),
            ("/fake/jsonl/rcc-original.jsonl", 26, 100_000, 10_000),
            ("/fake/jsonl/rcc-resumed.jsonl",   8, 300_000, 30_000),
            ("/fake/jsonl/rcc-resumed.jsonl",   2, 150_000, 15_000),
        ]):
            seed_session_entry(
                conn,
                source_path=src,
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model="claude-opus-4-7",
                input_tokens=input_t,
                output_tokens=output_t,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_missing_session_id_fallback():
    """Scenario: one session_files row with session_id=None (simulates
    the lazy-population window where sync_cache hasn't backfilled yet).
    Two session_entries on its path. Verifies:

      1. The fallback sessionId in cmd_session output equals the
         filename stem (os.path.splitext(os.path.basename(...))[0]).
         Source path: `/fake/jsonl/<UUID>.jsonl` → sessionId == `<UUID>`.

      2. The stderr warning `Warning: N entries lacked session_files
         rows (cache may be catching up).` (line 3161) appears in the
         captured golden output (the harness lib uses `2>&1`).

    The UUID literal is deterministic (NOT uuid.uuid4()) so goldens are
    byte-stable across regeneration. The first 8 chars (`ssn-miss`)
    appear in the rendered Session column.

    NOTE: This is the only scenario in the suite whose --json golden is
    NOT strictly-valid JSON — the stderr warning lands as line 1 because
    the harness lib captures with 2>&1. Intentional."""
    scenario_dir = FIXTURES_DIR / "missing-session-id-fallback"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
    fallback_uuid = "ssn-miss-0000-0000-deadbeefcafe"
    src_path = f"/fake/jsonl/{fallback_uuid}.jsonl"

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # session_files row with session_id=None (lazy-population window).
        seed_session_file(
            conn,
            path=src_path,
            session_id=None,
            project_path=None,
        )
        # Two entries — both will fall back to the filename-stem sessionId
        # AND both contribute to warn_count, so the warning text is
        # `Warning: 2 entries lacked session_files rows ...`.
        for i, hours_back in enumerate([10, 4]):
            seed_session_entry(
                conn,
                source_path=src_path,
                line_offset=i,
                timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                model="claude-opus-4-7",
                input_tokens=200_000,
                output_tokens=20_000,
            )
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


def build_multi_session_ordering():
    """Scenario: four distinct sessions with varying last_activity
    timestamps. Default golden = ascending order (cmd_session default
    --order asc reverses the aggregator's natural descending). The
    --order desc golden uses the aggregator's natural order (most-recent
    last_activity first).

    Distinct per-session token counts so the diff between asc and desc
    goldens is observable as row-content reordering, not just list
    reversal of identical-looking rows."""
    scenario_dir = FIXTURES_DIR / "multi-session-ordering"
    db_dir = scenario_dir / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    create_stats_db(db_dir / "stats.db")

    as_of = dt.datetime(2026, 4, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    # Four sessions, ordered here by `last_activity` ascending. Each tuple:
    #   (session_id, project_path, [(hours_back, input_tokens, output_tokens), ...])
    # Within each session, the LAST entry's hours_back is the smallest
    # value (most-recent timestamp), which becomes that session's
    # last_activity.
    sessions = [
        ("mso-session-alpha", "/fake/repos/alpha",
         [(96, 100_000, 10_000), (90, 50_000, 5_000)]),
        ("mso-session-bravo", "/fake/repos/bravo",
         [(70, 200_000, 20_000), (60, 80_000, 8_000)]),
        ("mso-session-gamma", "/fake/repos/gamma",
         [(36, 300_000, 30_000), (24, 120_000, 12_000)]),
        ("mso-session-delta", "/fake/repos/delta",
         [(12, 400_000, 40_000), ( 4, 160_000, 16_000)]),
    ]

    create_cache_db(db_dir / "cache.db")
    with sqlite3.connect(db_dir / "cache.db") as conn:
        # Global counter (not reset per session) — fine because each session
        # has a distinct source_path; resetting per session would silently
        # work today but break if any future session shared a path.
        line_offset = 0
        for sid, proj, entries in sessions:
            file_path = f"/fake/jsonl/{sid}.jsonl"
            seed_session_file(
                conn,
                path=file_path,
                session_id=sid,
                project_path=proj,
            )
            for hours_back, input_t, output_t in entries:
                seed_session_entry(
                    conn,
                    source_path=file_path,
                    line_offset=line_offset,
                    timestamp_utc=_iso(as_of - dt.timedelta(hours=hours_back)),
                    model="claude-opus-4-7",
                    input_tokens=input_t,
                    output_tokens=output_t,
                )
                line_offset += 1
        conn.commit()

    (scenario_dir / "input.env").write_text(f'AS_OF="{_iso(as_of)}"\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/session/. Used by cctally-session-test "
            "to write into a per-run scratch dir so the in-tree fixtures "
            "stay byte-stable across harness runs."
        ),
    )
    args = parser.parse_args()
    if args.out is not None:
        FIXTURES_DIR = args.out
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    build_single_session_single_file()
    build_resumed_across_files()
    build_resumed_cwd_changed()
    build_missing_session_id_fallback()
    build_multi_session_ordering()
    print(f"Built fixtures under {FIXTURES_DIR}")
