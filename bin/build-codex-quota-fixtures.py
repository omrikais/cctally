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

from _fixture_builders import create_cache_db, create_stats_db, seed_account  # noqa: E402


UTC_DAY = "2026-07-15"
RESET = "2026-07-15T15:00:00Z"
REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "cctally"
AS_OF = "2026-07-15T12:00:00Z"

# #341 Task 4 (Ruling B): the multi-account scenario's two REAL codex accounts.
# Opaque 32-char keys (shape parity with sha256[:32]); resolved in `--account`
# refs by their labels below. Two real accounts trip `provider_is_decorated`.
ACCOUNT_WORK = "a1" * 16
ACCOUNT_PERSONAL = "b2" * 16


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
    account_key: str = "unattributed",
) -> None:
    # `account_key` (#341) defaults to the schema default 'unattributed', so the
    # existing single-account scenarios stay byte-identical; the multi-account
    # scenario stamps a real key per window to exercise the `--account` filter.
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
            plan_type, individual_limit_json, reached_type, account_key)
           VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pro', NULL, NULL, ?)""",
        [
            (root, source_path, offset, captured, slot, limit, f"{limit}-id",
             limit.replace("-", " ").title(), minutes, percent, reset, account_key)
            for captured, offset, percent in captures
        ],
    )


def _seed_entries(
    conn: sqlite3.Connection, *, root: str, source_path: str,
    account_key: str | None = None,
) -> None:
    conn.executemany(
        """INSERT INTO codex_session_entries
           (source_path, line_offset, timestamp_utc, session_id, model,
            input_tokens, cached_input_tokens, output_tokens,
            reasoning_output_tokens, total_tokens, source_root_key, account_key)
           VALUES (?, ?, ?, 'quota-fixture', 'gpt-5', ?, ?, ?, ?, ?, ?, ?)""",
        [
            (source_path, 20, _iso(10), 1000, 0, 100, 0, 1100, root, account_key),
            (source_path, 30, _iso(11), 2000, 0, 200, 0, 2200, root, account_key),
            (source_path, 35, _iso(11, 30), 500, 100, 80, 0, 580, root, account_key),
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


def build_multi_account(out: Path) -> None:
    """Two REAL codex accounts sharing one root (#341 Task 4, Ruling B).

    Trips ``provider_is_decorated("codex")`` (>1 real account) and stamps
    per-account ``quota_window_snapshots.account_key`` so the deterministic
    ``codex quota --account <ref>`` render filters to one account. Account
    ``work`` owns the 330-minute primary window; ``personal`` owns the
    60-minute secondary window — so a ``breakdown`` selection by limit key
    stays unambiguous while ``--account`` still scopes cleanly.
    """
    home, cache_path = _paths(out, "multi-account")
    stats_path = home / ".local" / "share" / "cctally" / "stats.db"
    with sqlite3.connect(stats_path) as stats:
        seed_account(
            stats, account_key=ACCOUNT_WORK, provider="codex",
            natural_id="acct-work", email="work@example.com", label="work",
            plan_type="pro", label_source="user",
            first_seen_utc=_iso(8), last_seen_utc=_iso(12),
        )
        seed_account(
            stats, account_key=ACCOUNT_PERSONAL, provider="codex",
            natural_id="acct-personal", email="personal@example.com",
            label="personal", plan_type="pro", label_source="user",
            first_seen_utc=_iso(8), last_seen_utc=_iso(12),
        )
    root = "root-shared"
    work_path = "/codex/root-shared/work.jsonl"
    personal_path = "/codex/root-shared/personal.jsonl"
    with sqlite3.connect(cache_path) as conn:
        _seed_window(
            conn, root=root, source_path=work_path, limit="limit-primary",
            slot="primary", minutes=330, reset=RESET, account_key=ACCOUNT_WORK,
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4), (_iso(11), 30, 16.1)],
        )
        _seed_window(
            conn, root=root, source_path=personal_path, limit="limit-secondary",
            slot="secondary", minutes=60, reset=RESET, account_key=ACCOUNT_PERSONAL,
            captures=[(_iso(9), 40, 40.0), (_iso(10), 50, 41.0)],
        )
        _seed_entries(conn, root=root, source_path=work_path, account_key=ACCOUNT_WORK)


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
    "multi-account": {
        "history": "--since 2026-07-15 --until 2026-07-16",
        "statusline": "--as-of 2026-07-15T12:00:00Z",
        "forecast": "--as-of 2026-07-15T12:00:00Z",
        "blocks": "--since 2026-07-15 --until 2026-07-16",
        "breakdown": "--root-key root-shared --limit-key limit-primary --reset-at 2026-07-15T15:00:00Z",
    },
}

# #341 Task 4 (Ruling B): the `--account <ref>` render variants for the
# multi-account scenario, exercising all five views at the pinned AS_OF. The
# whole render path is deterministic (reconcile runs `now=as_of`;
# `_resolve_account_and_scope`/`_decorate_account` are pure), so these are safe
# byte-goldens. `work` scopes to the 330m primary window; `personal` to the 60m
# secondary window — proving the filter is non-vacuous (each ref's output
# differs from the merged view and from the other ref).
_ACCOUNT_FLAGS = {
    "history": "--since 2026-07-15 --until 2026-07-16",
    "statusline": "--as-of 2026-07-15T12:00:00Z",
    "forecast": "--as-of 2026-07-15T12:00:00Z",
    "blocks": "--since 2026-07-15 --until 2026-07-16",
    "breakdown": "--root-key root-shared --limit-key limit-primary --reset-at 2026-07-15T15:00:00Z",
}
# (leaf, ref, breakdown-limit-override). `personal` owns limit-secondary, so its
# breakdown selection targets that window.
_ACCOUNT_CASES = (
    ("history", "work", None),
    ("statusline", "work", None),
    ("forecast", "work", None),
    ("blocks", "work", None),
    ("breakdown", "work", None),
    ("history", "personal", None),
    ("statusline", "personal", None),
    ("forecast", "personal", None),
    ("blocks", "personal", None),
    ("breakdown", "personal",
     "--root-key root-shared --limit-key limit-secondary --reset-at 2026-07-15T15:00:00Z"),
)


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
    # #341 Task 4 (Ruling B): the `--account <ref>` variants for all five views.
    multi = fixtures / "multi-account"
    multi.mkdir(parents=True, exist_ok=True)
    for leaf, ref, breakdown_override in _ACCOUNT_CASES:
        base = breakdown_override if breakdown_override is not None else _ACCOUNT_FLAGS[leaf]
        flag_text = f"{base} --account {ref}"
        for suffix, additions in (("terminal", ()), ("json", ("--json",))):
            _write_golden(
                out, multi, "multi-account", leaf, f"account-{ref}-{suffix}",
                flag_text, additions,
            )


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
    build_multi_account(args.out)
    if args.regenerate_goldens:
        regenerate_goldens(args.out, REPO_ROOT / "tests" / "fixtures" / "codex-quota")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
