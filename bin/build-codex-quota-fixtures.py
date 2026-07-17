#!/usr/bin/env python3
"""Build deterministic physical-Codex-quota fixtures for the S2 CLI harness."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fixture_builders import create_cache_db, create_stats_db  # noqa: E402


UTC_DAY = "2026-07-15"
RESET = "2026-07-15T15:00:00Z"
REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "cctally"
AS_OF = "2026-07-15T12:00:00Z"


def _iso(hour: int, minute: int = 0) -> str:
    return f"{UTC_DAY}T{hour:02d}:{minute:02d}:00Z"


def _paths(out: Path, scenario: str) -> tuple[Path, Path]:
    home = out / scenario
    share = home / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    create_cache_db(share / "cache.db")
    create_stats_db(share / "stats.db")
    (share / "config.json").write_text(json.dumps({"display": {"tz": "utc"}}) + "\n")
    return home, share / "cache.db"


def _seed_window(
    conn: sqlite3.Connection, *, root: str, source_path: str, limit: str,
    slot: str, minutes: int, reset: str, captures: list[tuple[str, int, float]],
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO codex_source_roots
           (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
           VALUES (?, ?, ?, ?)""",
        (root, f"/codex/{root}", _iso(8), _iso(12)),
    )
    conn.executemany(
        """INSERT INTO quota_window_snapshots
           (source, source_root_key, source_path, line_offset,
            captured_at_utc, observed_slot, logical_limit_key, limit_id,
            limit_name, window_minutes, used_percent, resets_at_utc,
            plan_type, individual_limit_json, reached_type)
           VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pro', NULL, NULL)""",
        [
            (root, source_path, offset, captured, slot, limit, f"{limit}-id",
             limit.replace("-", " ").title(), minutes, percent, reset)
            for captured, offset, percent in captures
        ],
    )


def _seed_entries(conn: sqlite3.Connection, *, root: str, source_path: str) -> None:
    conn.executemany(
        """INSERT INTO codex_session_entries
           (source_path, line_offset, timestamp_utc, session_id, model,
            input_tokens, cached_input_tokens, output_tokens,
            reasoning_output_tokens, total_tokens, source_root_key)
           VALUES (?, ?, ?, 'quota-fixture', 'gpt-5', ?, ?, ?, ?, ?, ?)""",
        [
            (source_path, 20, _iso(10), 1000, 0, 100, 0, 1100, root),
            (source_path, 30, _iso(11), 2000, 0, 200, 0, 2200, root),
            (source_path, 35, _iso(11, 30), 500, 100, 80, 0, 580, root),
        ],
    )


def build_full(out: Path) -> None:
    _home, cache_path = _paths(out, "full")
    with sqlite3.connect(cache_path) as conn:
        source_a = "/codex/root-a/rollout.jsonl"
        _seed_window(
            conn, root="root-a", source_path=source_a, limit="limit-primary",
            slot="primary", minutes=330, reset=RESET,
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4), (_iso(11), 30, 16.1)],
        )
        # Same native-looking provider window under a different root: must be
        # rendered as a separate identity rather than blended or deduplicated.
        _seed_window(
            conn, root="root-b", source_path="/codex/root-b/rollout.jsonl",
            limit="limit-primary", slot="primary", minutes=330, reset=RESET,
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4)],
        )
        _seed_window(
            conn, root="root-a", source_path=source_a, limit="limit-secondary",
            slot="secondary", minutes=60, reset=RESET,
            captures=[(_iso(9), 40, 40.0), (_iso(10), 50, 41.0)],
        )
        _seed_entries(conn, root="root-a", source_path=source_a)


def build_stale_future(out: Path) -> None:
    _home, cache_path = _paths(out, "stale-future")
    with sqlite3.connect(cache_path) as conn:
        _seed_window(
            conn, root="stale-root", source_path="/codex/stale/rollout.jsonl",
            limit="limit-stale", slot="primary", minutes=60, reset=RESET,
            captures=[("2026-07-14T00:00:00Z", 10, 75.0)],
        )
        _seed_window(
            conn, root="future-root", source_path="/codex/future/rollout.jsonl",
            limit="limit-future", slot="secondary", minutes=60, reset=RESET,
            captures=[("2026-07-15T12:10:00Z", 20, 25.0)],
        )


def build_future_prior(out: Path) -> None:
    _home, cache_path = _paths(out, "future-prior")
    with sqlite3.connect(cache_path) as conn:
        _seed_window(
            conn, root="prior-future-root", source_path="/codex/prior-future/rollout.jsonl",
            limit="limit-prior-future", slot="primary", minutes=60, reset=RESET,
            captures=[(_iso(11), 10, 20.0), (_iso(12, 10), 20, 25.0)],
        )


def build_future_skew(out: Path) -> None:
    _home, cache_path = _paths(out, "future-skew")
    with sqlite3.connect(cache_path) as conn:
        _seed_window(
            conn, root="skew-root", source_path="/codex/skew/rollout.jsonl",
            limit="limit-skew", slot="primary", minutes=60, reset=RESET,
            captures=[(_iso(12, 5), 10, 30.0)],
        )


def build_empty(out: Path) -> None:
    _paths(out, "empty")


_SCENARIO_FLAGS = {
    "full": {
        "history": "--since 2026-07-15 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-15 --until 2026-07-16",
        "breakdown": "--root-key root-a --limit-key limit-primary --reset-at 2026-07-15T15:00:00Z",
    },
    "stale-future": {
        "history": "--since 2026-07-14 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-14 --until 2026-07-16",
        "breakdown": "--root-key stale-root --limit-key limit-stale --reset-at 2026-07-15T15:00:00Z",
    },
    "future-prior": {
        "history": "--since 2026-07-15 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-15 --until 2026-07-16",
        "breakdown": "--root-key prior-future-root --limit-key limit-prior-future --reset-at 2026-07-15T15:00:00Z",
    },
    "future-skew": {
        "history": "--since 2026-07-15 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-15 --until 2026-07-16",
        "breakdown": "--root-key skew-root --limit-key limit-skew --reset-at 2026-07-15T15:00:00Z",
    },
    "empty": {
        "history": "--since 2026-07-15 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-15 --until 2026-07-16",
        "breakdown": "--root-key missing-root --limit-key missing-limit --reset-at 2026-07-15T15:00:00Z",
    },
}


_ERROR_CASES = {
    "zero-match": (
        "history", "--root-key missing-root --limit-key limit-primary",
    ),
    "ambiguous-identity": (
        "breakdown", "--reset-at 2026-07-15T15:00:00Z",
    ),
    "no-block": (
        "breakdown", "--root-key root-a --limit-key limit-primary --reset-at 2026-07-15T16:00:00Z",
    ),
    "malformed-reset": (
        "breakdown", "--root-key root-a --limit-key limit-primary --reset-at not-a-timestamp",
    ),
    "date-only-reset": (
        "breakdown", "--root-key root-a --limit-key limit-primary --reset-at 2026-07-15",
    ),
}


def regenerate_goldens(out: Path, fixtures: Path) -> None:
    """Capture goldens through the same deterministic command posture as CI.

    This is an explicit canonical fixture-builder operation, not a harness
    bless mode: inputs remain the physical rows written above, and all command
    output is rebuilt from a clean scratch HOME.
    """
    for scenario, leaves in _SCENARIO_FLAGS.items():
        target = fixtures / scenario
        target.mkdir(parents=True, exist_ok=True)
        for leaf, flag_text in leaves.items():
            for mode, additions in (("terminal", ()), ("json", ("--json",))):
                _write_golden(out, target, scenario, leaf, mode, flag_text, additions)
        _write_golden(
            out, target, scenario, "breakdown", "speed-fast",
            leaves["breakdown"], ("--speed", "fast"),
        )
    full = fixtures / "full"
    for mode, (leaf, flags) in _ERROR_CASES.items():
        _write_golden(out, full, "full", leaf, mode, flags, ())


def _write_golden(out: Path, target: Path, scenario: str, leaf: str,
                  mode: str, flag_text: str, additions: tuple[str, ...]) -> None:
    env = {
        "HOME": str(out / scenario), "TZ": "Etc/UTC", "NO_COLOR": "1",
        "CCTALLY_AS_OF": AS_OF, "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1", "CCTALLY_DISABLE_TELEMETRY": "1",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    result = subprocess.run(
        [sys.executable, str(CLI), "codex", "quota", leaf, "--no-sync",
         *flag_text.split(), *additions],
        cwd=REPO_ROOT, env=env, text=True, capture_output=True,
    )
    # Golden errors are intentional for empty/ambiguous breakdown selection;
    # preserve the shell harness's combined stdout/stderr capture exactly.
    (target / f"golden-{leaf}-{mode}.txt").write_text(
        result.stdout + result.stderr, encoding="utf-8",
    )
    (target / f"golden-{leaf}-{mode}.exit").write_text(
        f"{result.returncode}\n", encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--regenerate-goldens", action="store_true")
    args = parser.parse_args()
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    build_full(args.out)
    build_stale_future(args.out)
    build_future_prior(args.out)
    build_future_skew(args.out)
    build_empty(args.out)
    if args.regenerate_goldens:
        regenerate_goldens(args.out, REPO_ROOT / "tests" / "fixtures" / "codex-quota")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
