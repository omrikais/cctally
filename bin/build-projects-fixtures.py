#!/usr/bin/env python3
"""Generate deterministic SQLite fixtures for `tests/test_projects_envelope.py`.

Three scenarios:
  - multi-week.db: 4 projects across 12 weeks; one trending up, one
    fading, one constant, one short-lived. `weekly_usage_snapshots` for
    8 of the 12 weeks (covers the attribution `—` case).
  - single-week.db: current subscription week only; 3 projects (covers
    the "history < weeks_back" clamp).
  - edge-cases.db: disambiguation-collision pair (`foo` under different
    parents → `foo (repos)` vs `foo (forks)`), one `(no-git)` project,
    one `(unknown)` (NULL `project_path`). NO `weekly_usage_snapshots`
    rows (covers the `attributed_pct=None` case).

The fixtures are SINGLE SQLite files carrying BOTH `session_entries` /
`session_files` (the cache.db side) AND `weekly_usage_snapshots` (the
stats.db side). The two table sets are disjoint, so co-existence in one
file is mechanical; this lets `_build_projects_envelope(conn, ...)`
take a single conn under unit tests (production wiring composes the
production paths separately — see `_cctally_dashboard.py`).

Determinism: `register_fixture_db()` is called for each output DB so
the atexit hook in `_fixture_builders` zeros the SQLite writer-version
header bytes after gc-closing any lingering Connection objects (memory:
*SQLite writer-version dirties fixtures*).
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

# Make `_fixture_builders` importable when run directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _fixture_builders as fb  # noqa: E402


# Pinned "now" for the fixtures — matches tests/test_projects_envelope.py
# (Tuesday 2026-05-19 12:00 UTC; current subscription week starts
# Monday 2026-05-18 00:00 UTC for the test's default Monday-anchored
# fallback). Treat as a literal constant — do NOT compute from
# `datetime.now()` in this file.
NOW_UTC = dt.datetime(2026, 5, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


# Fixed `last_ingested_at` so session_files inserts are byte-stable.
_FIXED_LAST_INGESTED_AT = "2026-05-19T11:50:00Z"


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _week_starts_back(n: int, *, anchor: dt.datetime) -> list[dt.datetime]:
    """Return n Monday-anchored UTC week starts ending with the week of `anchor`.

    Oldest → newest. Index n-1 is the current subscription week.
    """
    # The Monday of the week containing `anchor` (UTC).
    cw_start = (anchor - dt.timedelta(days=anchor.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=dt.timezone.utc,
    )
    return [cw_start - dt.timedelta(days=7 * (n - 1 - i)) for i in range(n)]


def _open_with_full_schema(path: pathlib.Path) -> sqlite3.Connection:
    """Create a fresh DB carrying both cache- and stats-side tables we
    need: ``session_entries`` + ``session_files`` (entries) and
    ``weekly_usage_snapshots`` (attribution). All columns are pre-baked
    so no inline ALTER TABLE migration fires on first open.
    """
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    fb.register_fixture_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE session_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL,
            session_id       TEXT,
            project_path     TEXT
        );
        CREATE INDEX idx_session_files_session_id
            ON session_files(session_id);

        CREATE TABLE session_entries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path         TEXT    NOT NULL,
            line_offset         INTEGER NOT NULL,
            timestamp_utc       TEXT    NOT NULL,
            model               TEXT    NOT NULL,
            msg_id              TEXT,
            req_id              TEXT,
            input_tokens        INTEGER NOT NULL DEFAULT 0,
            output_tokens       INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
            usage_extra_json    TEXT,
            cost_usd_raw        REAL,
            speed               TEXT,
            -- #270: durable per-row mutation signal (mirrors production schema);
            -- the projects-envelope current-week accumulator seeks its warm delta
            -- by `mutation_seq > ?`, so the fixture rows must carry it.
            mutation_seq        INTEGER NOT NULL DEFAULT 0,
            mutation_min_ts     TEXT
        );
        CREATE INDEX idx_entries_timestamp
            ON session_entries(timestamp_utc);
        CREATE INDEX idx_entries_source
            ON session_entries(source_path);
        CREATE INDEX idx_entries_mutation_seq
            ON session_entries(mutation_seq, mutation_min_ts);

        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL NOT NULL,
            page_url TEXT,
            source TEXT NOT NULL DEFAULT 'userscript',
            payload_json TEXT NOT NULL DEFAULT '{}',
            five_hour_percent REAL,
            five_hour_resets_at TEXT,
            five_hour_window_key INTEGER
        );
        CREATE INDEX idx_usage_week_time
            ON weekly_usage_snapshots(week_start_date, captured_at_utc DESC, id DESC);
        """
    )
    return conn


def _insert_entry(
    conn: sqlite3.Connection,
    *,
    source_path: str,
    session_id: str,
    project_path: str | None,
    ts: dt.datetime,
    model: str,
    input_tokens: int,
    output_tokens: int,
    line_offset: int,
) -> None:
    """Insert one session_entries row + upsert a matching session_files row.

    Cost is recomputed by the production code from CLAUDE_MODEL_PRICING
    at query time; we keep `cost_usd_raw` NULL so the fixture stays
    pricing-agnostic.
    """
    conn.execute(
        "INSERT OR IGNORE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) "
        "VALUES (?, 0, 0, 0, ?, ?, ?)",
        (source_path, _FIXED_LAST_INGESTED_AT, session_id, project_path),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, "
        " input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        " mutation_seq, mutation_min_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, "
        "        (SELECT COALESCE(MAX(mutation_seq), 0) + 1 FROM session_entries), "
        "        ?)",
        (source_path, line_offset, _iso(ts), model, input_tokens, output_tokens,
         _iso(ts)),
    )


def _seed_weekly_snapshot(
    conn: sqlite3.Connection,
    *,
    week_start: dt.datetime,
    weekly_percent: float,
) -> None:
    week_end = week_start + dt.timedelta(days=7)
    # Capture ~6h into the week so ordering is well-defined.
    captured_at = week_start + dt.timedelta(hours=6)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, "
        " page_url, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 'fixture', '{}')",
        (
            _iso(captured_at),
            week_start.date().isoformat(),
            week_end.date().isoformat(),
            _iso(week_start),
            _iso(week_end),
            weekly_percent,
        ),
    )


# --- Scenario builders ----------------------------------------------------

def build_multi_week(path: pathlib.Path) -> None:
    """4 projects × 12 weeks. One trending up, one fading, one constant,
    one short-lived (only last 3 weeks). `weekly_usage_snapshots` for
    weeks 5..12 (8 of 12 weeks have a snapshot).
    """
    conn = _open_with_full_schema(path)
    weeks = _week_starts_back(12, anchor=NOW_UTC)

    # Per-project (display_key, bucket_path, per-week token output scaling).
    # We use OUTPUT tokens per week to control cost — the model's pricing
    # multiplies by a fixed rate, so a flat input + variable output
    # produces a deterministic, well-ordered weekly cost distribution.
    projects: list[tuple[str, str, list[int]]] = [
        # (key, bucket_path, weekly_output_tokens for [w0..w11])
        ("cctally-dev",   "/repos/cctally-dev",   [1000] * 12),  # constant
        ("ccusage",       "/repos/ccusage",       [3000] * 4 + [1000] * 8),  # fading
        ("house-of-mass", "/repos/house-of-mass", [200] * 6 + [2500] * 6),  # trending up
        ("scratch",       "/tmp/scratch",         [0] * 9 + [500] * 3),  # short-lived
    ]

    model = "claude-sonnet-4-5-20250929"
    line_off = 0
    for proj_idx, (key, bucket_path, weekly_out) in enumerate(projects):
        # Each project gets ONE source_path / session_id per week to keep
        # row counts predictable. project_path is the bucket itself
        # (production resolves git_root from this; for the fixture we
        # pin project_path = bucket_path so the LEFT JOIN attributes
        # cleanly).
        for w_idx, week_start in enumerate(weeks):
            out_tok = weekly_out[w_idx]
            if out_tok == 0:
                continue
            ts = week_start + dt.timedelta(hours=12 + proj_idx)
            source_path = f"/jsonl/{key}/w{w_idx:02d}.jsonl"
            session_id = f"{key}-w{w_idx:02d}-s0"
            _insert_entry(
                conn,
                source_path=source_path,
                session_id=session_id,
                project_path=bucket_path,
                ts=ts,
                model=model,
                input_tokens=500,
                output_tokens=out_tok,
                line_offset=line_off,
            )
            line_off += 1

    # weekly_usage_snapshots for the last 8 of 12 weeks; weekly_percent
    # scaled by week index (4..11 ⇒ 40..68 to be roughly proportional).
    for w_idx, week_start in enumerate(weeks):
        if w_idx < 4:
            continue
        _seed_weekly_snapshot(
            conn,
            week_start=week_start,
            weekly_percent=40.0 + (w_idx - 4) * 4.0,  # 40, 44, ..., 68
        )

    conn.commit()
    conn.close()


def build_single_week(path: pathlib.Path) -> None:
    """Current subscription week only; 3 projects. Exercises the
    `window_weeks` clamp when `weeks_back=12` is requested against a
    cache with only 1 week of history.
    """
    conn = _open_with_full_schema(path)
    cw_start = _week_starts_back(1, anchor=NOW_UTC)[0]

    model = "claude-sonnet-4-5-20250929"
    projects = [
        ("alpha", "/repos/alpha", 1500),
        ("beta",  "/repos/beta",   800),
        ("gamma", "/repos/gamma",  300),
    ]
    for idx, (key, bucket_path, out_tok) in enumerate(projects):
        ts = cw_start + dt.timedelta(hours=12 + idx)
        _insert_entry(
            conn,
            source_path=f"/jsonl/{key}/cw.jsonl",
            session_id=f"{key}-cw-s0",
            project_path=bucket_path,
            ts=ts,
            model=model,
            input_tokens=400,
            output_tokens=out_tok,
            line_offset=idx,
        )

    _seed_weekly_snapshot(conn, week_start=cw_start, weekly_percent=22.0)

    conn.commit()
    conn.close()


def build_edge_cases(path: pathlib.Path) -> None:
    """Disambiguation-collision pair + (no-git) + (unknown).

    - `/repos/foo` and `/forks/foo` share basename `foo`; the
      disambiguator suffixes to `foo (repos)` / `foo (forks)`.
    - `/tmp/loose` is a no-git path; display_key falls back to its
      basename `loose` (NOT suffixed; only basename collisions are
      disambiguated).
    - `project_path = NULL` produces a `(unknown)` ProjectKey row.

    No `weekly_usage_snapshots` rows — exercises the `attributed_pct =
    None` path (spec §2.7).
    """
    conn = _open_with_full_schema(path)
    cw_start = _week_starts_back(1, anchor=NOW_UTC)[0]

    model = "claude-sonnet-4-5-20250929"
    rows = [
        # (source_path basename, session_id, project_path, output_tokens)
        ("foo-repos", "foo-repos-s0", "/repos/foo",  1200),
        ("foo-forks", "foo-forks-s0", "/forks/foo",   900),
        ("loose",     "loose-s0",     "/tmp/loose",   600),
        ("unknown",   "unknown-s0",   None,           300),
    ]
    for idx, (slug, session_id, project_path, out_tok) in enumerate(rows):
        ts = cw_start + dt.timedelta(hours=12 + idx)
        _insert_entry(
            conn,
            source_path=f"/jsonl/{slug}/edge.jsonl",
            session_id=session_id,
            project_path=project_path,
            ts=ts,
            model=model,
            input_tokens=200,
            output_tokens=out_tok,
            line_offset=idx,
        )

    # Deliberately NO weekly_usage_snapshots → attributed_pct = None
    # everywhere.

    conn.commit()
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="tests/fixtures/projects",
                    help="Output directory (default: tests/fixtures/projects)")
    args = ap.parse_args()

    out = pathlib.Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    build_multi_week(out / "multi-week.db")
    build_single_week(out / "single-week.db")
    build_edge_cases(out / "edge-cases.db")
    print(f"Built fixtures in {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
