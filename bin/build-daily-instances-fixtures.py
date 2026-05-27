#!/usr/bin/env python3
"""Build the seeded SQLite fixture for `cctally daily --instances` / `-p`.

Writes ONE scenario, `multi-project`, under
``tests/fixtures/daily-instances/multi-project/`` (or ``<--out>/multi-project``):
  * ``.local/share/cctally/cache.db``   — seeded session_files + session_entries
  * real ``.git`` directories per project root (under the fixture HOME)
  * ``input.env``                       — AS_OF for the harness

Schema mirrors production (delegates to ``_fixture_builders`` helpers, which
set ``PRAGMA journal_mode=WAL`` and register the DB for atexit writer-version
normalization — see the SQLite-writer-version gotcha). Idempotent — overwrites
existing DBs.

Project layout (git-root grouping via ``_resolve_project_key("git-root")``):
  * ``work/app``       — entries 2026-05-20 & 2026-05-21, claude-sonnet-4-5
  * ``personal/app``   — entry 2026-05-20, claude-opus-4-1 (higher cost; SAME
                         basename ``app`` as ``work/app`` but DISTINCT git-root,
                         so it must NOT merge — disambiguates to ``app (personal)``)
  * ``repos/lib``      — entry 2026-05-20, claude-haiku-4-5 (low cost)
  * one entry with ``project_path = NULL`` → ``(unknown)``

The builder materializes REAL ``.git`` dirs under the fixture HOME (at the
realpath'd ``--out`` location, so the git-root walk in ``_resolve_project_key``
finds ``.git`` on the FIRST iteration — robust against the macOS
``/var → /private/var`` symlink that would otherwise defeat the
``cur == home`` walk-stop guard). Output shows only project LABELS
(``app (work)`` / ``app (personal)`` / ``lib`` / ``(unknown)``), never absolute
paths, so committed goldens are location-independent and byte-stable. The
committed in-tree dir holds only ``input.env`` + ``golden-*.txt`` (NO cache.db,
NO home/.git trees — those are rebuilt into a scratch dir by
``bin/cctally-daily-instances-test`` / ``bin/cctally-reconcile-test``).

Why one scenario (not many like build-project-fixtures): `daily --instances`
needs ONE rich multi-project / multi-day / multi-model / same-basename /
null-project dataset that every golden + reconcile invariant slices with
different flags. The harness drives the flag matrix; the data stays constant.
"""

from __future__ import annotations
import argparse
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

# Make _fixture_builders importable when run directly (bin/ is not on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import (  # noqa: E402
    create_cache_db,
    seed_session_entry,
    seed_session_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/daily-instances"

# Fixed AS_OF: a few days after the latest seeded entry so the default date
# window the harness passes (--since 20260520 --until 20260521) is wholly in
# the past. Value only needs to be stable + later than every entry.
_AS_OF = dt.datetime(2026, 5, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_git_root(home: Path, rel: str) -> str:
    """Create a real (empty) ``.git`` dir at ``<home>/<rel>`` and return the
    absolute realpath'd project path. Storing the realpath in the DB means the
    git-root walk in ``_resolve_project_key`` (which realpaths its input) finds
    ``.git`` on the first iteration regardless of any symlink between the scratch
    mount point and its canonical location (macOS ``/var`` → ``/private/var``)."""
    proj = (home / rel).resolve()
    (proj / ".git").mkdir(parents=True, exist_ok=True)
    return str(proj)


def build_multi_project(out_dir: Path) -> None:
    scenario_dir = out_dir / "multi-project"
    # HOME the harness/reconcile will export is the scenario dir itself.
    # Realpath it so project paths + .git dirs land at the canonical location
    # _resolve_project_key resolves to.
    home = scenario_dir.resolve()
    db_dir = home / ".local/share/cctally"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Real git roots under the fixture HOME.
    work_app = _make_git_root(home, "work/app")          # basename "app"
    personal_app = _make_git_root(home, "personal/app")  # SAME basename "app"
    repos_lib = _make_git_root(home, "repos/lib")        # basename "lib"

    cache_path = db_dir / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as conn:
        # work/app — two days, claude-sonnet-4-5. Distinct source files per day
        # so session_files rows carry the project_path the aggregator reads.
        for i, (day, inp, out, sid) in enumerate([
            (dt.datetime(2026, 5, 20, 10, 0, 0, tzinfo=dt.timezone.utc),
             400_000, 80_000, "work-s1"),
            (dt.datetime(2026, 5, 21, 11, 0, 0, tzinfo=dt.timezone.utc),
             300_000, 60_000, "work-s2"),
        ]):
            src = f"/fake/jsonl/work-app-{i}.jsonl"
            seed_session_file(conn, path=src, session_id=sid, project_path=work_app)
            seed_session_entry(
                conn, source_path=src, line_offset=0,
                timestamp_utc=_iso(day), model="claude-sonnet-4-5",
                input_tokens=inp, output_tokens=out,
            )

        # personal/app — one day, claude-opus-4-1 (higher cost than sonnet).
        src = "/fake/jsonl/personal-app-0.jsonl"
        seed_session_file(conn, path=src, session_id="personal-s1",
                          project_path=personal_app)
        seed_session_entry(
            conn, source_path=src, line_offset=0,
            timestamp_utc=_iso(dt.datetime(2026, 5, 20, 9, 0, 0, tzinfo=dt.timezone.utc)),
            model="claude-opus-4-1",
            input_tokens=600_000, output_tokens=120_000,
        )

        # repos/lib — one day, claude-haiku-4-5 (low cost).
        src = "/fake/jsonl/repos-lib-0.jsonl"
        seed_session_file(conn, path=src, session_id="lib-s1",
                          project_path=repos_lib)
        seed_session_entry(
            conn, source_path=src, line_offset=0,
            timestamp_utc=_iso(dt.datetime(2026, 5, 20, 8, 0, 0, tzinfo=dt.timezone.utc)),
            model="claude-haiku-4-5",
            input_tokens=200_000, output_tokens=40_000,
        )

        # NULL project_path → (unknown) bucket under -i.
        src = "/fake/jsonl/orphan-0.jsonl"
        seed_session_file(conn, path=src, session_id="orphan-s1", project_path=None)
        seed_session_entry(
            conn, source_path=src, line_offset=0,
            timestamp_utc=_iso(dt.datetime(2026, 5, 20, 7, 0, 0, tzinfo=dt.timezone.utc)),
            model="claude-sonnet-4-5",
            input_tokens=100_000, output_tokens=20_000,
        )

    # COLUMNS_OVERRIDE=200 so the wide `Project: app (personal)` section header
    # doesn't force the default 120-col render to scale-down + wrap the Date
    # column ("2026" / "05-20" split). At 200 cols every date renders single-
    # line, keeping goldens clean and obviously correct.
    (scenario_dir / "input.env").write_text(
        f'AS_OF="{_iso(_AS_OF)}"\nCOLUMNS_OVERRIDE=200\n'
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Override output directory. Defaults to the in-tree path "
            "tests/fixtures/daily-instances/. Used by "
            "cctally-daily-instances-test / cctally-reconcile-test to write "
            "into a per-run scratch dir so the in-tree fixtures stay byte-"
            "stable across harness runs."
        ),
    )
    args = parser.parse_args()
    out_dir = args.out if args.out is not None else FIXTURES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    build_multi_project(out_dir)
    print(f"Built fixtures under {out_dir}")
