#!/usr/bin/env python3
"""Build single-model Codex fixtures for the --speed reconcile invariants
(issue #86 Session D).

Two scenarios under tests/fixtures/speed/:
  - override-model: all gpt-5.5 entries  -> fast == standard * 2.5 (override)
  - fallback-model: all gpt-5 entries     -> fast == standard * 2.0 (fallback)

Single-model so `Sigma(--speed fast) == Sigma(--speed standard) * multiplier`
is an exact scalar check (no per-model breakdown parsing). The fake HOME has
NO ~/.codex/config.toml, so default `auto` resolves to `standard`.

Run: bin/build-speed-fixtures.py (idempotent; overwrites DBs).

Note on create_cache_db: it does NOT return a connection — it writes the
schema then closes its own connection. So after create_cache_db(path) we
open our OWN connection to seed rows (same pattern as
bin/build-codex-fixtures.py / build-mode-fixtures.py). Only
tests/fixtures/speed/.gitignore is committed — the seeded DBs / input.env are
gitignored (mirrors tests/fixtures/mode/).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixture_builders import (  # noqa: E402
    create_cache_db,
    create_stats_db,
    seed_codex_session_entry,
    seed_codex_session_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/speed"
# Mirror tests/fixtures/mode/.gitignore: reconcile ALWAYS rebuilds fresh into
# a --out scratch dir, so the seeded DBs / input.env are NOT committed — only
# this parent .gitignore is tracked. Anything a default (no --out) build or a
# CLI run writes in-tree stays out of the working tree.
GITIGNORE_BODY = """\
# Runtime artifacts generated when bin/build-speed-fixtures.py runs WITHOUT
# --out (the default writes here). The reconcile harness
# (bin/cctally-reconcile-test) always rebuilds these fixtures fresh into a
# --out scratch dir, so the seeded SQLite DBs are NOT committed in-tree —
# only this .gitignore is. Anything the builder or a CLI run writes here
# stays out of the working tree.
*.db
*.db-wal
*.db-shm
*.log
input.env
.local/
.claude/
config.json
.local/share/cctally/cache.db*.lock
"""

SCENARIOS = {
    "override-model": "gpt-5.5",
    "fallback-model": "gpt-5",
}


def _build_scenario(base: Path, model: str) -> None:
    home = base
    (home / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    share = home / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    create_stats_db(share / "stats.db")
    cache = share / "cache.db"
    create_cache_db(cache)
    src = ".codex/sessions/2026/04/17/rollout-2026-04-17T10-00-00-aaaa.jsonl"
    with sqlite3.connect(cache) as conn:
        seed_codex_session_file(
            conn, path=src, last_session_id="aaaa", last_model=model,
        )
        # Three entries across one day; tokens chosen so cost is clearly
        # nonzero. LiteLLM convention: input_tokens INCLUDES cached, and
        # output_tokens INCLUDES reasoning.
        base_ts = dt.datetime(2026, 4, 17, 10, 0, 0, tzinfo=dt.timezone.utc)
        rows = [
            (1_000_000, 200_000, 500_000, 0),
            (300_000, 0, 150_000, 50_000),
            (50_000, 10_000, 25_000, 0),
        ]
        for i, (inp, cached, out, reason) in enumerate(rows):
            seed_codex_session_entry(
                conn,
                source_path=src,
                line_offset=i,
                timestamp_utc=(base_ts + dt.timedelta(minutes=i)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                session_id="aaaa",
                model=model,
                input_tokens=inp,
                cached_input_tokens=cached,
                output_tokens=out,
                reasoning_output_tokens=reason,
                total_tokens=inp + out,
            )
        conn.commit()
    # input.env: the reconcile harness sources this and reads $AS_OF (NOT
    # CCTALLY_AS_OF — that var name only exists in the process env the harness
    # then exports). AS_OF must post-date the seeded entries (2026-04-17).
    (home / "input.env").write_text('AS_OF="2026-04-20T00:00:00Z"\n')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(FIXTURES_DIR))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Write the parent .gitignore once (the ONLY tracked artifact in-tree).
    (out / ".gitignore").write_text(GITIGNORE_BODY)
    for scenario, model in SCENARIOS.items():
        _build_scenario(out / scenario, model)
        print(f"built {scenario} ({model})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
