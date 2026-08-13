"""Leaf-of-the-graph kernel for cctally.

Contains primitives that every sibling and bin/cctally itself depend on:
logging (eprint), datetime helpers, week-name/bounds, time-of-day,
alerts-config validation, open_db, WeekRef + make_week_ref,
get_latest_usage_for_week.

Path constants (APP_DIR, DB_PATH, LOG_DIR, etc.) live in this module as
of 2026-05-22 (issue #84); `_cctally_core` is the single source of truth
and the only legal monkeypatch target for the 23 promoted globals listed
below. See docs/superpowers/specs/2026-05-22-cctally-core-data-globals.md.
"""
from __future__ import annotations
import contextvars
import datetime as dt
import math
import os
import pathlib
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from typing import Any


def _cctally():
    return sys.modules["cctally"]


# === Path constants ==================================================
#
# Promoted from bin/cctally per docs/superpowers/specs/2026-05-22-cctally-core-data-globals.md.
# After this promotion `_cctally_core` is the single source of truth and
# the only legal monkeypatch target. `bin/cctally` keeps eager re-exports
# for ad-hoc REPL / scripts; tests MUST target this module directly.
#
# Path-constant initialization is wrapped in `_init_paths_from_env()` so
# `tests/conftest.py:load_script()` can re-derive them from the current
# HOME env var without re-importing this module (which would invalidate
# tests' module-top `import _cctally_core` references). The bare module
# attributes below are populated by the call to _init_paths_from_env()
# at import time; subsequent load_script calls invoke it again.


def _init_paths_from_env() -> None:
    """(Re)bind the 23 in-scope path globals from the current process env.

    22 of the 23 resolve under ``Path.home()`` (i.e. the ``HOME`` env var).
    The 23rd, ``CHANGELOG_PATH``, resolves from ``CCTALLY_TEST_CHANGELOG_PATH``
    when set, else from ``__file__`` (``<repo>/CHANGELOG.md`` relative to
    this kernel module's location) — independent of ``HOME``. Tests that
    redirect the changelog (e.g. ``tests/test_release_internals.py``) drive
    that override and rely on this re-init.

    Called once at module import to populate the defaults, then again
    by `tests/conftest.py:load_script()` after each `setenv("HOME", …)`
    or `setenv("CCTALLY_TEST_CHANGELOG_PATH", …)` so the test sees a fresh
    path set without the cost of re-importing `_cctally_core` (which would
    break tests that cached the module object via a top-level
    `import _cctally_core`).
    """
    global APP_DIR, LEGACY_APP_DIR, LOG_DIR, DEV_MODE
    global DB_PATH, CACHE_DB_PATH, CONVERSATIONS_DB_PATH
    global CACHE_LOCK_PATH, CACHE_LOCK_CODEX_PATH, CACHE_LOCK_MAINTENANCE_PATH
    global CONVERSATIONS_LOCK_PATH, CONVERSATIONS_LOCK_CODEX_PATH
    global CONVERSATIONS_LOCK_MAINTENANCE_PATH
    global STATS_LOCK_MAINTENANCE_PATH
    global JOURNAL_DIR, JOURNAL_LOCK_PATH, JOURNAL_INGEST_LOCK_PATH
    global ARTIFACT_RETENTION_LOCK_PATH
    global CONFIG_LOCK_PATH
    global CONFIG_PATH, MIGRATION_ERROR_LOG_PATH, CHANGELOG_PATH
    global HOOK_TICK_LOG_DIR, HOOK_TICK_LOG_PATH, HOOK_TICK_LOG_ROTATED_PATH
    global HOOK_TICK_THROTTLE_PATH, HOOK_TICK_THROTTLE_LOCK_PATH
    global STATUSLINE_OBSERVE_MARKER_PATH, STATUSLINE_PERSIST_LOCK_PATH
    global STATUSLINE_CANDIDATE_DIR, STATUSLINE_SELECTED_PATH
    global STATUSLINE_TRANSPORT_MARKER_PATH
    global STATUSLINE_AUTHORITATIVE_7D_PATH, STATUSLINE_AUTHORITATIVE_5H_PATH
    global OAUTH_BACKOFF_MARKER_PATH, OAUTH_BACKOFF_COUNT_PATH
    global UPDATE_STATE_PATH, UPDATE_SUPPRESS_PATH
    global UPDATE_LOCK_PATH, UPDATE_LOG_PATH, UPDATE_LOG_ROTATED_PATH
    global UPDATE_CHECK_LAST_FETCH_PATH, CLAUDE_SETTINGS_PATH
    global CLAUDE_PROJECTS_DIR, CLAUDE_JSON_PATH
    global TELEMETRY_INSTALL_ID_PATH, TELEMETRY_LAST_BEAT_PATH
    global TELEMETRY_NOTICE_SHOWN_PATH, TELEMETRY_FIRST_SEEN_PATH

    home = pathlib.Path.home()

    # Dev-instance isolation (docs/superpowers/specs/2026-05-26-dev-instance-
    # isolation-design.md). Resolve the APP_DIR base first; all other path
    # constants derive from it. First match wins:
    #   1. explicit CCTALLY_DATA_DIR override (also the test/harness pin)
    #   2. auto-detected dev checkout -> cctally-dev (sets DEV_MODE)
    #   3. prod default (byte-identical to pre-feature behavior)
    _data_dir_override = os.environ.get("CCTALLY_DATA_DIR", "").strip()
    if _data_dir_override:
        APP_DIR = pathlib.Path(_data_dir_override).expanduser()
        DEV_MODE = False
    elif _is_dev_checkout():
        APP_DIR = home / ".local" / "share" / "cctally-dev"
        DEV_MODE = True
    else:
        APP_DIR = home / ".local" / "share" / "cctally"
        DEV_MODE = False
    LEGACY_APP_DIR = home / ".local" / "share" / "ccusage-subscription"
    LOG_DIR = APP_DIR / "logs"

    DB_PATH = APP_DIR / "stats.db"
    CACHE_DB_PATH = APP_DIR / "cache.db"
    CONVERSATIONS_DB_PATH = APP_DIR / "conversations.db"

    CACHE_LOCK_PATH = APP_DIR / "cache.db.lock"
    CACHE_LOCK_CODEX_PATH = APP_DIR / "cache.db.codex.lock"
    # #313 P3 (F7): dedicated maintenance flock serializing the transcript
    # retention prune across processes, held ABOVE the two provider flocks so a
    # rebuild/reingest cannot land between candidate selection and deletion.
    CACHE_LOCK_MAINTENANCE_PATH = APP_DIR / "cache.db.maintenance.lock"
    # #320: transcript ingest and maintenance are physically independent from
    # the latency-sensitive token/quota cache.  Never reuse the cache.db flock
    # namespace for conversations.db writes.
    CONVERSATIONS_LOCK_PATH = APP_DIR / "conversations.db.lock"
    CONVERSATIONS_LOCK_CODEX_PATH = APP_DIR / "conversations.db.codex.lock"
    CONVERSATIONS_LOCK_MAINTENANCE_PATH = (
        APP_DIR / "conversations.db.maintenance.lock"
    )
    # Stats index maintenance flock (2026-07-22 DB journal redesign, spec §6.3 /
    # §6.4). Held at the TOP of the lock-order law (maintenance → ingest →
    # provider flocks → txn → journal.lock) by the classifier-gated corruption
    # auto-heal and the `db rebuild --db stats` operator command, so a rebuild
    # never races a concurrent healer/vacuum and only one process quarantines +
    # recreates stats.db at a time. cache.db already has its own maintenance
    # flock above; stats gets the symmetric one now that it, too, auto-heals.
    STATS_LOCK_MAINTENANCE_PATH = APP_DIR / "stats.db.maintenance.lock"
    # Append-only observation journal (2026-07-22 DB journal redesign, spec
    # docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md §4).
    # JOURNAL_DIR holds the monthly observations-YYYY-MM.jsonl segments plus
    # the one-time bootstrap-<ts>.jsonl export; the two lock files sit
    # alongside the DBs in APP_DIR. journal.lock is the µs-scale, blocking,
    # LEAF append lock (spec §4.3 — no other lock is ever taken while it is
    # held); journal.ingest.lock admits at most one stats.db writer (spec
    # §5.1). All three are APP_DIR-derived so dev/data-dir redirection carries
    # them along, exactly like the cache/conversations locks above.
    JOURNAL_DIR = APP_DIR / "journal"
    JOURNAL_LOCK_PATH = APP_DIR / "journal.lock"
    JOURNAL_INGEST_LOCK_PATH = APP_DIR / "journal.ingest.lock"
    # Retained-evidence reclamation flock (#496 S6, spec §5.3). It enters the
    # lock-order law AFTER the conversation provider flocks and BEFORE SQLite
    # transactions, which keeps `journal.lock` the leaf. Every producer of
    # retained evidence — corruption forensics, quarantine manifests, rebuild
    # records, the backups `db repair` writes — holds it SHARED across the span
    # in which its evidence is being published; the reclamation worker takes it
    # EXCLUSIVE holding nothing earlier, marks, releases, and only then deletes.
    ARTIFACT_RETENTION_LOCK_PATH = APP_DIR / "artifact-retention.lock"
    CONFIG_LOCK_PATH = APP_DIR / "config.json.lock"

    CONFIG_PATH = APP_DIR / "config.json"

    MIGRATION_ERROR_LOG_PATH = LOG_DIR / "migration-errors.log"

    # CHANGELOG_PATH: honor CCTALLY_TEST_CHANGELOG_PATH env override; otherwise
    # resolves to <repo>/CHANGELOG.md based on bin/_cctally_core.py's
    # location (alongside bin/cctally, so the parent chain is the same).
    override = os.environ.get("CCTALLY_TEST_CHANGELOG_PATH")
    if override:
        CHANGELOG_PATH = pathlib.Path(override)
    else:
        CHANGELOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"

    HOOK_TICK_LOG_DIR = APP_DIR / "logs"
    HOOK_TICK_LOG_PATH = HOOK_TICK_LOG_DIR / "hook-tick.log"
    HOOK_TICK_LOG_ROTATED_PATH = HOOK_TICK_LOG_DIR / "hook-tick.log.1"
    HOOK_TICK_THROTTLE_PATH = APP_DIR / "hook-tick.last-fetch"
    HOOK_TICK_THROTTLE_LOCK_PATH = APP_DIR / "hook-tick.last-fetch.lock"

    # Statusline candidate arbitration (#318). The spool and derived selected
    # state are entirely APP_DIR-derived so dev/data-dir redirection remains
    # complete. The observation marker now means selected/authoritative
    # freshness; transport liveness has its own marker.
    STATUSLINE_OBSERVE_MARKER_PATH = APP_DIR / "statusline-observe.last"
    STATUSLINE_PERSIST_LOCK_PATH = APP_DIR / "statusline-persist.lock"
    STATUSLINE_CANDIDATE_DIR = APP_DIR / "statusline-candidates"
    STATUSLINE_SELECTED_PATH = APP_DIR / "statusline-selected.json"
    STATUSLINE_TRANSPORT_MARKER_PATH = APP_DIR / "statusline-transport.last"
    STATUSLINE_AUTHORITATIVE_7D_PATH = APP_DIR / "statusline-authoritative-7d.json"
    STATUSLINE_AUTHORITATIVE_5H_PATH = APP_DIR / "statusline-authoritative-5h.json"
    OAUTH_BACKOFF_MARKER_PATH = APP_DIR / "oauth-backoff.until"
    # Consecutive-429 counter (text int) driving the headerless exponential
    # backoff (base * 2**count). Separate from the deadline marker so the
    # deadline file stays a single parseable float.
    OAUTH_BACKOFF_COUNT_PATH = APP_DIR / "oauth-backoff.count"

    UPDATE_STATE_PATH = APP_DIR / "update-state.json"
    UPDATE_SUPPRESS_PATH = APP_DIR / "update-suppress.json"
    UPDATE_LOCK_PATH = APP_DIR / "update.lock"
    UPDATE_LOG_PATH = APP_DIR / "update.log"
    UPDATE_LOG_ROTATED_PATH = APP_DIR / "update.log.1"
    UPDATE_CHECK_LAST_FETCH_PATH = APP_DIR / "update-check.last-fetch"

    # Anonymous install-count telemetry markers (see spec 2026-07-07).
    # All four derive from APP_DIR and are re-bound here so a redirected
    # APP_DIR (tests, dev-instance isolation) carries them along.
    TELEMETRY_INSTALL_ID_PATH = APP_DIR / "install_id"
    TELEMETRY_LAST_BEAT_PATH = APP_DIR / "telemetry.last-beat"
    TELEMETRY_NOTICE_SHOWN_PATH = APP_DIR / "telemetry.notice-shown"
    TELEMETRY_FIRST_SEEN_PATH = APP_DIR / "telemetry.first-seen"

    CLAUDE_SETTINGS_PATH = home / ".claude" / "settings.json"

    # Claude's own identity file (#341): `~/.claude.json` carries the
    # `oauthAccount` block (accountUuid / emailAddress / plan) maintained by
    # Claude Code. cctally reads it READ-ONLY for account attribution; never
    # written. Module constant so tests can `monkeypatch.setattr(_cctally_core,
    # "CLAUDE_JSON_PATH", ...)`. Rewritten in place by Claude Code, so every read
    # goes through the stat-read-stat stable-read protocol.
    CLAUDE_JSON_PATH = home / ".claude.json"

    # Claude session JSONL root. Production path is `~/.claude/projects`;
    # exposed as a module-level constant so cross-DB migrations (e.g.
    # stats migration 008) and the dispatcher's empty-disk fallback can
    # honor a fixture override via tests' `monkeypatch.setattr(
    # _cctally_core, "CLAUDE_PROJECTS_DIR", tmp_path / "...")`. The
    # `_get_claude_data_dirs()` helper in bin/cctally remains the
    # authoritative resolver for ad-hoc reads (multi-root + env-aware);
    # this constant is the single-rooted production default that 99% of
    # callers want. For multi-root, env-aware resolution (mirroring
    # `_get_claude_data_dirs`), use `_resolve_claude_projects_dirs()`.
    CLAUDE_PROJECTS_DIR = home / ".claude" / "projects"


# The statusline OAuth cache is HOST-GLOBAL and shared with the real Claude
# Code statusline, so /tmp stays the production location -- relocating it would
# change behaviour for real users. It lives here, as a module constant rather
# than inside _init_paths_from_env(), because it is deliberately NOT derived
# from APP_DIR or HOME; putting it here is what lets redirect_paths pin it and
# lets _bust_statusline_cache resolve it at CALL time instead of binding it as
# a default argument at import (#529 S4, spec section 5.4).
STATUSLINE_OAUTH_CACHE_PATH = "/tmp/claude-statusline-usage-cache.json"


def _truthy_env(name: str) -> bool:
    """A ``1``/``true``/``yes``/any-other-non-empty env value is truthy;
    unset, empty, ``0``, ``false``, ``no`` are falsey (case-insensitive,
    whitespace-stripped).

    Canonical home for boolean env-flag parsing (#279 S1 F1) — presence-only
    ``os.environ.get(...)`` checks made ``FLAG=0`` mean *enabled*, which for
    ``CCTALLY_ALLOW_PROD_MIGRATION`` / ``CCTALLY_DISABLE_DEV_AUTODETECT`` was
    the exact opposite of intent. ``_cctally_telemetry._truthy_env`` delegates
    here."""
    v = os.environ.get(name)
    return v is not None and v.strip().lower() not in ("", "0", "false", "no")


def _repo_root() -> pathlib.Path:
    """Repo root when running from a source checkout: this file lives at
    ``<repo>/bin/_cctally_core.py``, so the root is two parents up. Factored
    out as the single monkeypatch seam for the dev-mode tests."""
    return pathlib.Path(__file__).resolve().parent.parent


def _is_dev_checkout() -> bool:
    """True iff running from a git checkout (a ``.git`` entry at the repo
    root — a directory for a main checkout, a file for a worktree) AND the
    test/harness suppressor ``CCTALLY_DISABLE_DEV_AUTODETECT`` is unset.

    Deliberately INDEPENDENT of ``CCTALLY_DATA_DIR``: this predicate gates
    the ``setup`` guard (which protects WHICH BINARY gets wired into
    ~/.claude/settings.json), not the data-dir relocation. The npm/brew
    install copies ship without ``.git`` so they never read True."""
    if _truthy_env("CCTALLY_DISABLE_DEV_AUTODETECT"):
        return False
    return (_repo_root() / ".git").exists()


def is_preview_channel() -> bool:
    """True when running under the maintainer-local preview channel
    (the `cctally-preview` wrapper sets CCTALLY_CHANNEL=preview). Single
    source of truth for every preview-marker surface (dashboard port +
    envelope, TUI header, --version, doctor) so the gate can't drift."""
    return os.environ.get("CCTALLY_CHANNEL") == "preview"


def _real_prod_data_dir() -> pathlib.Path:
    """The REAL user's prod data dir (~/.local/share/cctally), resolved from
    the password database rather than $HOME so it is immune to a faked HOME.

    The prod-migration guard (bin/_cctally_db.py, issue #142) compares the
    connection's DB directory against this to tell a fake-HOME test 'prod'
    (e.g. a golden harness's /tmp/scratch/.local/share/cctally) apart from
    the actual prod dir. Monkeypatchable seam: tests point it at a tmp dir to
    exercise the guard's fire path without touching real prod. Falls back to
    Path.home() only if `pwd` is unavailable (cctally targets Unix only)."""
    try:
        import pwd
        home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        home = pathlib.Path.home()
    return home / ".local" / "share" / "cctally"


# === Statusline-persist / OAuth-backfill tunables ==========================
# Internal (no config UI — YAGNI, spec §Out of scope); test injection only.
# Spec 2026-07-17-usage-statusline-fallback-design.
#
# STATUSLINE_OAUTH_POLL_SECONDS: the account-wide authoritative confirmation
#   cadence driven by Claude Code's 30-second statusline timer. Keep it below
#   the timer so scheduling jitter does not skip every other tick.
# OAUTH_BACKFILL_STALE_SECONDS: the matching selected-freshness gate shared by
#   statusline-driven and hook-driven automatic OAuth refreshes.
# OAUTH_BACKOFF_BASE_SECONDS / OAUTH_BACKOFF_CAP_SECONDS: the headerless
#   exponential 429 backoff (base * 2**consecutive_429, capped).
STATUSLINE_CANDIDATE_TTL_SECONDS = 90
STATUSLINE_CANDIDATE_FUTURE_SKEW_SECONDS = 5
STATUSLINE_CANDIDATE_DOCUMENT_MAX_BYTES = 4 * 1024
STATUSLINE_SELECTED_DOCUMENT_MAX_BYTES = 1024 * 1024
STATUSLINE_TOMBSTONE_DOCUMENT_MAX_BYTES = 1024
# STATUSLINE_REFRESH_INTERVAL_DEFAULT (#311): the value `cctally setup`
# writes into Claude Code's settings.json `statusLine.refreshInterval` when a
# recognized cctally statusLine block lacks one. Claude Code re-runs the
# statusline command on this fixed timer "in addition to the event-driven
# updates", which keeps the usage-persistence feeder ticking while a parent
# session waits on a long subagent (event-driven updates go quiet then). MUST
# Add-when-absent only; a user-set value is never mutated.
STATUSLINE_REFRESH_INTERVAL_DEFAULT = 30
STATUSLINE_OAUTH_POLL_SECONDS = 25.0
OAUTH_BACKFILL_STALE_SECONDS = STATUSLINE_OAUTH_POLL_SECONDS
OAUTH_BACKOFF_BASE_SECONDS = 60.0
OAUTH_BACKOFF_CAP_SECONDS = 3600.0


_init_paths_from_env()


# === stats.db epoch-rebuild versioning (DB journal redesign §7.1/§8) ==
#
# stats.db is no longer a versioned migration target — it is a DISPOSABLE index
# materialized from the append-only journal. A single ``STATS_INDEX_EPOCH``
# integer (stamped via ``PRAGMA user_version``, safely above the legacy 0–13
# range) versions the index; ANY mismatch resolves by journal rebuild, and
# ``DowngradeDetected``-bricking ceases for stats. ``LEGACY_STATS_HEAD`` is the
# frozen legacy migration head (``len(_STATS_MIGRATIONS)``): a stats.db whose
# ``user_version`` is still in the legacy range (<= 13) is a pre-journal install
# that ``open_db`` cuts over to the epoch on first open (spec §8). Schema change
# = bump ``STATS_INDEX_EPOCH`` (never a new stats migration — the registry is
# frozen).
#
# 1000 -> 1001 (#341, multi-account): the stats index gains the account
# dimension (account_key on every derived table + the accounts registry). An
# existing epoch-1000 index resolves the mismatch through the account
# epoch-transition coordinator (``_cctally_journal.run_epoch_transition``):
# resolve the cutover identity, append the canonical cutover op, then rebuild
# account-scoped. See docs/superpowers/specs/2026-07-23-multi-account-design.md §2.
# 1001 -> 1002 (#372 Task A): the disposable index gains the effective-event
# revision summary consumed by correction planning/live replay. Rebuild selects
# the highest completed revision before folding; the legacy migration registry
# remains frozen.
# 1002 -> 1003 (#402 Task A): persist the selector's bounded structural
# correction-batch violations in the disposable index so shallow Dashboard/TUI
# Doctor gathers cannot report false health without rescanning the whole journal.
# 1003 -> 1004 (#410 Task B): pair the public journal cursor with the exact
# prefix atomically applied to the materialized index. A cursor-only hand edit
# can no longer skip an already-durable event and make its natural key look new.
# 1004 -> 1005 (public #5): the incremental Codex quota projection. Adds the
# reverse map + composable per-group digest on `quota_window_blocks` and the
# `quota_projection_ledger_state` row (change-ledger watermark, interpretation
# version, and the two alert axes that are not functions of window dirtiness).
# It is an epoch bump and NOT a stats migration because the registry is frozen
# AND because an epoch-current open returns before any schema work — an
# `add_column_if_missing` would never run on an upgraded install, so the column
# would simply never appear. Every upgrading install therefore rebuilds stats.db
# from the journal on first open; that is the documented resolution for an epoch
# mismatch and a real one-time cost, not a free change.
# 1005 -> 1006 (public #5, I2 review): the periodic verification. Adds
# `quota_projection_ledger_state.last_full_pass_at`, the deadline a time-based
# full pass is measured against. Same mechanical reason as 1005 — an
# epoch-current open returns before any schema work — so it is a second bump
# rather than an amendment to the first.
# 1006 -> 1007 (#460): scheduled quota-alert ownership. Adds the per-root
# future-capture schedule that lets a matured boundary widen to its owning root
# instead of deferring forever on a quiet hook-only install.
# 1007 -> 1008 (#496 S3): in-place transactional publication. Adds
# `stats_publication_stamp`, the publication identity written inside the
# publication transaction. It replaces the marker's `scratchPath` crash
# discriminator, which an in-place publish inverts because it attaches the
# scratch read-only and leaves it on disk whether the transaction committed or
# rolled back. A stats schema change is an epoch bump and never a migration;
# the 13-migration registry stays frozen. Each install pays one rebuild on
# upgrade, deferred to the background worker by #453.
# 1008 -> 1009 (#496 S5b): durable replay selection. Adds the three
# `journal_selector_*` tables reproducing `resolve_effective_events`' six
# accumulators, `stats_quota_projection_state` (reserved by Stage 1, set by
# Stage 3), plus `journal_effective_events.winning_sequence` /
# `.conflict_hashes_json` and `journal_protocol_violations.available_after`.
# Same mechanical reason as every bump since 1005: an epoch-current open returns
# before any schema work, so an `add_column_if_missing` would never run on an
# upgraded install and the column would simply never appear. The registry stays
# frozen at 13 and an epoch mismatch resolves by rebuild.
# 1009 -> 1010 (#538 Task A): retire WAL for the disposable stats index. Every
# stats connection now uses DELETE/FULL rollback journaling, structurally
# removing the shared-memory WAL page map implicated by the retained corruption
# bundles. The one-time rebuild is the mode transition; no stats migration is
# added and cache/conversations remain WAL/NORMAL.
STATS_INDEX_EPOCH = 1010
LEGACY_STATS_HEAD = 13

#: #496 S1 F1. A NEW branch, for a state that cannot occur before the
#: publication transaction exists: a replacement index was published and then
#: failed validation on a fresh connection. The existing corrupt-stats text
#: says the database "is never auto-recreated", which would be false here, so
#: this path gets its own wording. It does not alter the heal message or any
#: other corruption path.
STATS_PUBLICATION_FAILED_MSG = (
    "stats.db published a rebuilt index that then FAILED validation, so the "
    "live index is known bad and cctally refuses to use it. path: {path}. "
    "The rebuild record naming the failing check is at {record}. The damaged "
    "predecessor was preserved under quarantine/ with a forensics bundle in "
    "logs/. Recovery: run `cctally db rebuild --db stats`."
)

#: #496 S3. The text above describes PHYSICAL replacement, which is now the
#: fallback. In-place publication drops the live generation and installs the
#: scratch's inside one transaction, so it preserves nothing and allocates no
#: quarantine directory — and the sentence about a preserved predecessor would
#: send a user whose index is already known bad to a directory that does not
#: exist. Selected by the mechanism the publication marker records.
STATS_PUBLICATION_FAILED_IN_PLACE_MSG = (
    "stats.db published a rebuilt index that then FAILED validation, so the "
    "live index is known bad and cctally refuses to use it. path: {path}. "
    "The rebuild record naming the failing check is at {record}. This index "
    "was published in place, so no copy of the previous index was kept; every "
    "row it holds is derived from the append-only journal, which the "
    "publication did not touch. Recovery: run `cctally db rebuild --db stats`."
)


# === Telemetry constants (non-path; see spec 2026-07-07) =============
#
# These are static (not APP_DIR-derived) so they live outside
# `_init_paths_from_env()`. The kernel `bin/_cctally_telemetry.py` reads
# them at call time via its `_core()` accessor.
#
# Public, non-secret domain-separation constant folded into the monthly
# rotating token (SHA-256, truncated to 32 hex). It only namespaces
# cctally's token from any other consumer of the same install_id — it is
# NOT a secret and leaking it discloses nothing about the install.
TELEMETRY_PEPPER = "cctally-install-count-v1"
# Default beat endpoint; overridable for tests via CCTALLY_TELEMETRY_ENDPOINT.
TELEMETRY_ENDPOINT_DEFAULT = "https://cctally-telemetry.cctally.workers.dev/beat"
# Send at most one beat per this many seconds (mtime-gated on the beat marker).
TELEMETRY_BEAT_THROTTLE_SECONDS = 24 * 3600
# Wait this long after first eligibility before sending the first beat.
TELEMETRY_FIRST_BEAT_GRACE_SECONDS = 24 * 3600


def _resolve_claude_projects_dirs() -> list[pathlib.Path]:
    """Return Claude Code projects dirs that exist on disk, env-aware.

    Mirrors `_get_claude_data_dirs()` in bin/cctally but returns the
    `projects/` subdir directly (since cross-DB migrations only care
    about the JSONL root, not the parent Claude data dir). Honors
    ``CLAUDE_CONFIG_DIR`` (comma-separated multi-root) and falls back
    to ``~/.config/claude`` then ``~/.claude``.

    Used by stats migration 008's gate helper to avoid falsely
    short-circuiting Layer C's empty-disk fallback when the user has
    ``CLAUDE_CONFIG_DIR=/other/path`` set AND no ``~/.claude/projects``
    dir on disk: the gate would otherwise see zero JSONL files at the
    hardcoded ``CLAUDE_PROJECTS_DIR`` and "pass" the gate, then run the
    recompute as a no-op against an empty cache.

    Tests can also feed an explicit list to the gate helper directly,
    skipping this resolver.
    """
    env_val = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env_val:
        candidates = [pathlib.Path(p.strip()) for p in env_val.split(",") if p.strip()]
        result = [
            d / "projects"
            for d in candidates
            if d.is_dir() and (d / "projects").is_dir()
        ]
        if result:
            return result

    home = pathlib.Path.home()
    defaults = [
        home / ".config" / "claude",
        home / ".claude",
    ]
    return [d / "projects" for d in defaults if d.is_dir() and (d / "projects").is_dir()]


# === Logging =========================================================


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


# === Datetime helpers ================================================


def now_utc_iso(now_utc: dt.datetime | None = None) -> str:
    """Return a UTC-ISO 'Z'-suffixed timestamp with seconds precision.

    When ``now_utc`` is omitted (the default), reads wall-clock — existing
    behavior, preserved byte-for-byte for all existing callers. When a
    tz-aware UTC datetime is supplied (typically via ``_command_as_of()``),
    it is used verbatim so callers that honor ``CCTALLY_AS_OF`` get a
    stable, caller-pinned timestamp.
    """
    value = now_utc if now_utc is not None else dt.datetime.now(dt.timezone.utc)
    return (
        value.astimezone(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _iso_to_epoch(s: str) -> int:
    """Parse an ISO-8601 timestamp and return Unix epoch seconds.

    Naive ISO strings (no timezone) are treated as UTC, matching the
    statusline-command.sh ``_iso_to_epoch`` helper. ``Z`` suffix is
    handled by mapping to ``+00:00`` since ``datetime.fromisoformat``
    accepts ``Z`` natively from Python 3.11.
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def _format_short_duration(seconds: int) -> str:
    """Format a duration as a short top-two-units string.

    Examples: ``6d 4h``, ``2h 15m``, ``2h``, ``45m``, ``30s``, ``0s``.
    Mirrors the shape used by ``~/.claude/statusline-command.sh``'s
    format_duration helper. Negative inputs clamp to ``0s``.
    """
    s = max(0, int(seconds))
    if s >= 86400:
        days = s // 86400
        hours = (s % 86400) // 3600
        return f"{days}d {hours}h" if hours else f"{days}d"
    if s >= 3600:
        hours = s // 3600
        minutes = (s % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date_str(value: str, label: str) -> dt.date:
    s = value.strip()
    if not _DATE_RE.match(s):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return dt.date.fromisoformat(s)


def parse_iso_datetime(value: str, label: str) -> dt.datetime:
    s = value.strip()
    if not s:
        raise ValueError(f"{label} must be a non-empty ISO datetime")
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO datetime") from exc

    if parsed.tzinfo is None:
        # internal fallback: host-local intentional
        local_tz = dt.datetime.now().astimezone().tzinfo
        parsed = parsed.replace(tzinfo=local_tz)
    # internal fallback: host-local intentional
    return parsed.astimezone()


def format_local_iso(d: dt.date, end_of_day: bool) -> str:
    t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
    # internal fallback: host-local intentional
    local_dt = dt.datetime.combine(d, t).astimezone()
    return local_dt.isoformat(timespec="seconds")


def _normalize_week_boundary_dt(value: dt.datetime) -> dt.datetime:
    """
    Normalize known Anthropic boundary jitter.

    Anthropic resets are always on hour boundaries. Relative reset text
    ("in XX hr YY min") produces minute-level drift on every capture, and
    the UI occasionally alternates between HH:00 and HH-1:59 for the same
    logical reset.

    Canonicalization: round to the nearest hour.
    - minutes 0..29 -> HH:00
    - minutes 30..59 -> (HH+1):00
    """
    normalized = value.replace(second=0, microsecond=0)
    if normalized.minute >= 30:
        normalized = (normalized + dt.timedelta(hours=1)).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
    elif normalized.minute > 0:
        normalized = normalized.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
    return normalized


# === Time-of-day (CCTALLY_AS_OF hooks) ==============================


def _command_as_of() -> dt.datetime:
    """Testing hook: CCTALLY_AS_OF env var overrides wall-clock `now` for
    time-dependent commands. Shared by cmd_project, cmd_weekly,
    cmd_cache_report, cmd_codex_weekly, cmd_diff (and any future
    time-dependent command). Format: ISO-8601 with Z or explicit tz offset.
    """
    override = os.environ.get("CCTALLY_AS_OF")
    if override:
        override = override.strip()
        if override.endswith("Z"):
            override = override[:-1] + "+00:00"
        return dt.datetime.fromisoformat(override).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _as_of_or_command(as_of: "str | None") -> dt.datetime:
    """Capture-time reference datetime for the derivation chokepoints (DB
    journal redesign spec §5.2.3). When the ingester injects a record's ``at``
    as ``as_of`` (ISO-8601 with ``Z`` / explicit offset), parse it to UTC so
    derivation is replay-deterministic; otherwise fall back to the legacy
    ``_command_as_of()`` wall clock (honoring the ``CCTALLY_AS_OF`` test hook).
    Passing ``None`` keeps today's behavior bit-identical for legacy callers.
    """
    if as_of:
        s = as_of.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(s)
        # I3 gate pickup: reject a naive capture time. `.astimezone()` on a
        # naive datetime silently assumes HOST-LOCAL, which would make
        # capture-time-pure derivation non-deterministic across hosts (the very
        # thing §5.2.3 injection exists to prevent). A journal `at` is always
        # UTC ISO-Z / explicit-offset by construction, so a naive value here is
        # a caller bug — fail loud instead of guessing the offset.
        if parsed.tzinfo is None:
            raise ValueError(
                f"_as_of_or_command: naive capture time {as_of!r} "
                "(expected ISO-8601 with 'Z' or an explicit offset)"
            )
        return parsed.astimezone(dt.timezone.utc)
    return _command_as_of()


def _now_utc() -> dt.datetime:
    """UTC now, with CCTALLY_AS_OF env override for fixture-stability.

    Single time source for the `update` subcommand and its supporting
    state machine (TTL gates, ``remind_after.until_utc`` comparisons,
    log timestamps, install-method detection cache). Mirrors the
    documented CCTALLY_AS_OF precedent (see CLAUDE.md — `project` has
    a hidden `CCTALLY_AS_OF` env hook, and `_command_as_of` /
    `_share_now_utc` reuse it for `weekly`/`forecast`/share-render).
    Accepts ISO-8601 with `Z` or explicit offset; result is always
    tz-aware UTC.

    Raises ValueError on malformed CCTALLY_AS_OF — deliberate fail-loud
    for the dev hook so fixture authors notice typos immediately rather
    than silently falling back to wall-clock time.
    """
    override = os.environ.get("CCTALLY_AS_OF")
    if override:
        override = override.strip()
        if override.endswith("Z"):
            override = override[:-1] + "+00:00"
        return dt.datetime.fromisoformat(override).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


# === Week-name + bounds =============================================


DEFAULT_WEEK_START = "monday"

WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def get_week_start_name(config: dict[str, Any], override: str | None = None) -> str:
    if override:
        name = override.strip().lower()
    else:
        name = str(config.get("collector", {}).get("week_start", DEFAULT_WEEK_START)).strip().lower()
    if name not in WEEKDAY_MAP:
        raise ValueError(
            f"Invalid week start '{name}'. Allowed: {', '.join(WEEKDAY_MAP.keys())}"
        )
    return name


def compute_week_bounds(anchor_dt: dt.datetime, week_start_name: str) -> tuple[dt.date, dt.date]:
    start_idx = WEEKDAY_MAP[week_start_name]
    # internal fallback: host-local intentional
    local_anchor = anchor_dt.astimezone()
    local_date = local_anchor.date()
    diff = (local_date.weekday() - start_idx) % 7
    start = local_date - dt.timedelta(days=diff)
    end = start + dt.timedelta(days=6)
    return start, end


# === Path primitive =================================================


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # cache.db holds plaintext conversation prose at rest (Plan 2, spec §5), so
    # the data dir must be 0700. Hardening it here in the shared primitive means
    # a stats-first cold start — open_db() materializing APP_DIR before any
    # cache.db open (e.g. record-usage) — is covered, not only the
    # open_cache_db backstop (which keeps its own chmod). Best-effort and
    # idempotent: swallow OSError + continue (issue #150).
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError as exc:
        eprint(f"[core] could not chmod data dir 0700 ({exc}); continuing")
    # #496 S6 §9.4: LOG_DIR was created at the umask and was drwxr-xr-x, the
    # same class of defect as the 0644 stats family. It holds corruption
    # forensics bundles and rebuild records, which name paths and carry damage
    # detail, so it gets the data directory's mode rather than the umask's.
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError as exc:
        eprint(f"[core] could not chmod log dir 0700 ({exc}); continuing")


# === stats.db maintenance-hold tracking (#386) ======================
#
# `flock` conflicts are per open-file-DESCRIPTION and apply WITHIN a process:
# holding LOCK_EX on one fd and then requesting LOCK_SH on a second fd of the
# same file blocks forever. That matters because `_cctally_store`'s #386 opener
# protocol takes `stats.db.maintenance.lock` SHARED around every live stats
# open, while `run_stats_ingest`'s legacy/fresh branch already holds it
# EXCLUSIVE when it calls `open_db()` (bin/_cctally_journal.py:2788). Without a
# re-entrancy signal that is an unconditional self-deadlock on first open of a
# pre-cutover install.
#
# A ContextVar, not a module global, and deliberately so: the suppressor must
# fire only for the execution context that actually owns the lock. A dashboard
# thread that does NOT own it and requests SHARED while another thread holds
# EXCLUSIVE is correctly made to WAIT — that is the protocol working, not a
# deadlock — and a process-global flag would wrongly wave it straight through
# into a family being replaced underneath it.
#
# Every acquisition of STATS_LOCK_MAINTENANCE_PATH in bin/ that is HELD ACROSS
# other work pairs with these (a site that takes the flock and releases it
# before returning does not, and must not — see `stats_open_guarded`):
#   bin/_cctally_journal.py  _acquire_maintenance_{shared,exclusive} / _release
#   bin/_cctally_store.py    _heal_flock_blocking, reached through
#                            _acquire_stats_maintenance_reentrant by the epoch
#                            resolver
#   bin/_cctally_store.py    detached corruption-heal worker maintenance acquire
#                            (the detector itself never acquires it; #530)
#   bin/_cctally_db.py       cmd_db_rebuild, _acquire_db_admin_writer_flocks
#                            (db skip / db unskip), _cmd_db_repair_exclusive,
#                            _vacuum_one_db
#   bin/_cctally_rederive.py _rederive_locks
#   bin/_cctally_store.py    stats_open_guarded's interrupted-rebuild-recovery
#                            branch, which upgrades to EXCLUSIVE and then calls
#                            rebuild_stats_index (#496 S3)
# Adding another acquisition site without noting it here reintroduces the hang.
#
# The opener (`_cctally_store.stats_open_guarded`) takes the lock SHARED around
# an ordinary open and releases it before handing the connection back, so that
# acquire deliberately does NOT note a hold — but it DOES consult
# `holds_stats_maintenance()` to skip the acquire entirely when this context
# already owns the exclusive side. Its interrupted-rebuild-recovery branch is
# the exception: that one upgrades to EXCLUSIVE and holds it across a rebuild,
# whose in-place publisher opens the destination through `stats_open_guarded`
# again, so it notes the hold like every other exclusive site.

_STATS_MAINTENANCE_HELD = contextvars.ContextVar(
    "cctally_stats_maintenance_held", default=0
)


def holds_stats_maintenance() -> bool:
    """True when THIS execution context already holds stats.db.maintenance.lock."""
    return _STATS_MAINTENANCE_HELD.get() > 0


# === #496 S5b §4.7 open-time quota-projection reconciliation (opt-in) ======
#
# A rebuild that published a valid index over a cache with an uncovered
# remainder durably marks its quota projection incomplete, and some later open
# has to resume that recovery or the gate never lifts. `open_db` is where that
# resumption lives, but it is NOT something every open may pay: `open_db` runs
# on `cctally statusline` and on every hook tick, and the resumption reads the
# journal from zero to the current high water — the 1.64 GB working set the S4
# measurements describe — before it can apply anything.
#
# So the trigger is armed per process rather than unconditional. A maintenance
# command that is already doing journal-scale work arms it; the interactive
# render paths never do, and pay only the one indexed flag SELECT that was
# already there. A process global rather than a ContextVar deliberately: this
# is a property of the COMMAND that is running, not of one thread inside it,
# and the dashboard arms it once for the whole server.
QUOTA_PROJECTION_RECONCILE_ENABLED = False


def enable_quota_projection_reconciliation() -> None:
    """Arm the open-time quota-projection reconciliation for this process.

    A PROCESS global, deliberately, and not a ContextVar or a thread-local: the
    two armers are `cmd_cache_sync` and `cmd_dashboard`, and the dashboard has
    no dedicated maintenance thread to hang a thread-local on. So in the
    dashboard whichever thread reaches `open_db` first runs the attempt, and one
    attempt can stall that thread while it holds the maintenance flock. That is
    accepted rather than overlooked: the attempt takes every lock
    non-blocking and returns immediately when any is busy, the throttle bounds
    the repeat to one per interval, and the alternative — routing this through a
    worker the dashboard does not currently have — is a larger change than the
    contention it would avoid.
    """
    global QUOTA_PROJECTION_RECONCILE_ENABLED
    QUOTA_PROJECTION_RECONCILE_ENABLED = True


# === stats.db sanctioned-write scope (#386) =========================
#
# The state behind `_cctally_store.stats_write_scope` / `in_stats_write_scope`
# / `holds_ingest_lock`. It lives HERE, beside the maintenance tracker, rather
# than in `_cctally_store` for one concrete reason: `tests/conftest.py`'s
# `load_script()` drops every cached `_cctally_*` sibling from `sys.modules`
# (deliberately — see its docstring) but KEEPS `_cctally_core`. A ContextVar
# owned by `_cctally_store` would therefore be silently replaced by a fresh,
# empty one halfway through a test, so a scope entered before the reload would
# stop counting and an authorized write would be denied. `_cctally_core` is the
# kernel and is never reloaded, so the sanction survives.
#
# ContextVars, NOT module globals: the dashboard is threaded and a global would
# let one sanctioned thread authorize another (spec section 6.1).

_STATS_WRITE_SCOPE = contextvars.ContextVar(
    "cctally_stats_write_scope", default=0
)
_STATS_INGEST_LOCK_HELD = contextvars.ContextVar(
    "cctally_stats_ingest_held", default=0
)
_STATS_INTERRUPTED_RECOVERY_SUPPRESSED = contextvars.ContextVar(
    "cctally_stats_interrupted_recovery_suppressed", default=0
)


def note_stats_maintenance_acquired() -> None:
    """Record that this context now holds stats.db.maintenance.lock."""
    _STATS_MAINTENANCE_HELD.set(_STATS_MAINTENANCE_HELD.get() + 1)


def note_stats_maintenance_released() -> None:
    """Record that this context released stats.db.maintenance.lock.

    Clamped at zero rather than asserting: an unbalanced release is a bug, but
    turning it into an exception inside a ``finally`` would mask the original
    failure that got us there.
    """
    _STATS_MAINTENANCE_HELD.set(max(0, _STATS_MAINTENANCE_HELD.get() - 1))


# === Alerts validation cluster ======================================


class _AlertsConfigError(ValueError):
    """Raised by _get_alerts_config on invalid alerts block.

    ``field`` carries the offending dotted config path (e.g.
    ``"alerts.notifier"``) so ``POST /api/settings`` can answer
    ``{error, field}`` on every rejection. It is set explicitly at each
    raise site and never inferred from the message text, because inferring
    it would let a reworded message silently move a machine-readable
    pointer. Keyword-only with a ``None`` default, so every existing CLI
    caller keeps working unchanged.
    """

    def __init__(self, message: str, *, field: "str | None" = None) -> None:
        super().__init__(message)
        self.field = field


_ALERTS_CONFIG_VALID_KEYS = {
    "enabled",
    "weekly_thresholds",
    "five_hour_thresholds",
    "projected_enabled",
    "notifier",
    "command_template",
}

# Dispatch backends (Phase B). "auto" picks a platform default; "command"
# routes through alerts.command_template (which it then requires).
_ALERTS_VALID_NOTIFIERS = ("auto", "osascript", "notify-send", "command", "none")


def _validate_threshold_list(name: str, value: object) -> "list[int]":
    """Validate one of the alerts threshold lists.

    Rules: non-empty list of plain ints (NOT bools — `bool` is an `int`
    subclass), each in [1, 100], strictly increasing (no duplicates).
    Error messages mention `alerts.<name>` so users can locate the
    offending key in their config.json.
    """
    field = f"alerts.{name}"
    if not isinstance(value, list):
        raise _AlertsConfigError(
            f"alerts.{name} must be a list of integers", field=field
        )
    if len(value) == 0:
        raise _AlertsConfigError(
            f"alerts.{name} must not be empty (disable alerts via alerts.enabled=false)",
            field=field,
        )
    out: "list[int]" = []
    prev = -1
    seen: "set[int]" = set()
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise _AlertsConfigError(
                f"alerts.{name} items must be integers, got {type(item).__name__}: {item!r}",
                field=field,
            )
        if item < 1 or item > 100:
            raise _AlertsConfigError(
                f"alerts.{name} items must be in [1, 100], got {item}",
                field=field,
            )
        if item in seen:
            raise _AlertsConfigError(
                f"alerts.{name} contains duplicate value {item}",
                field=field,
            )
        if item <= prev:
            raise _AlertsConfigError(
                f"alerts.{name} must be strictly increasing, got {prev} then {item}",
                field=field,
            )
        seen.add(item)
        prev = item
        out.append(item)
    return out


def _get_alerts_config(cfg: "dict | None") -> dict:
    """Return the validated alerts block. Raises _AlertsConfigError on failure.

    Defaults applied at read time so future default-tuning takes effect
    for users who never customized. Unknown sub-keys under `alerts.*`
    emit a one-line warn-and-ignore (mirrors the `display.tz` posture
    for forward compatibility).
    """
    block = (cfg or {}).get("alerts", {}) or {}
    if not isinstance(block, dict):
        raise _AlertsConfigError("alerts must be an object", field="alerts")
    # warn-and-ignore unknown keys (forward compat; matches display.tz posture)
    for k in block.keys():
        if k not in _ALERTS_CONFIG_VALID_KEYS:
            print(
                f"warning: ignoring unknown alerts config key: {k}",
                file=sys.stderr,
            )
    enabled = block.get("enabled", False)
    if not isinstance(enabled, bool):
        raise _AlertsConfigError(
            f"alerts.enabled must be a JSON boolean, got {type(enabled).__name__}: {enabled!r}",
            field="alerts.enabled",
        )
    weekly = _validate_threshold_list(
        "weekly_thresholds", block.get("weekly_thresholds", [90, 95])
    )
    five_hour = _validate_threshold_list(
        "five_hour_thresholds", block.get("five_hour_thresholds", [90, 95])
    )
    # projected-pace opt-in (#121); default OFF so upgrades fire no surprise
    # notifications. Bool-validated (NOT coerced) so a non-bool is a config
    # error, not silently truthy.
    projected_enabled = block.get("projected_enabled", False)
    if not isinstance(projected_enabled, bool):
        raise _AlertsConfigError(
            f"alerts.projected_enabled must be a JSON boolean, got "
            f"{type(projected_enabled).__name__}: {projected_enabled!r}",
            field="alerts.projected_enabled",
        )
    # Dispatch-global keys (Phase B). `notifier` selects the backend;
    # `command_template` is an argv list for the `command` backend (and may be
    # set ahead of switching the backend). The cross-field constraint
    # (notifier='command' requires a template) is enforced last.
    notifier = block.get("notifier", "auto")
    if notifier not in _ALERTS_VALID_NOTIFIERS:
        raise _AlertsConfigError(
            f"alerts.notifier must be one of {list(_ALERTS_VALID_NOTIFIERS)}, "
            f"got {notifier!r}",
            field="alerts.notifier",
        )
    command_template = block.get("command_template", None)
    if command_template is not None:
        if not isinstance(command_template, list) or not command_template:
            raise _AlertsConfigError(
                "alerts.command_template must be null or a non-empty list of strings",
                field="alerts.command_template",
            )
        for el in command_template:
            if not isinstance(el, str):
                raise _AlertsConfigError(
                    f"alerts.command_template elements must be strings, "
                    f"got {type(el).__name__}: {el!r}",
                    field="alerts.command_template",
                )
            if "\x00" in el:
                raise _AlertsConfigError(
                    "alerts.command_template elements must not contain a NUL byte",
                    field="alerts.command_template",
                )
        if not command_template[0].strip():
            raise _AlertsConfigError(
                "alerts.command_template[0] (the program) must not be empty/whitespace",
                field="alerts.command_template",
            )
    if notifier == "command" and command_template is None:
        # Cross-field: point at the leaf the caller just set, not at the
        # absent one, so a dashboard save highlights the field it sent.
        raise _AlertsConfigError(
            "alerts.notifier='command' requires alerts.command_template to be set",
            field="alerts.notifier",
        )
    return {
        "enabled": enabled,
        "weekly_thresholds": weekly,
        "five_hour_thresholds": five_hour,
        "projected_enabled": projected_enabled,
        "notifier": notifier,
        "command_template": command_template,
    }


# === Budget validation cluster ======================================


class _BudgetConfigError(ValueError):
    """Raised by _get_budget_config on an invalid budget block.

    ``field`` follows the same contract as ``_AlertsConfigError.field``:
    the offending dotted path (e.g. ``"budget.codex.alerts_enabled"``),
    set explicitly at the raise site, keyword-only, defaulting to ``None``.
    """

    def __init__(self, message: str, *, field: "str | None" = None) -> None:
        super().__init__(message)
        self.field = field


def _validate_positive_budget_amount(v: object, label: str) -> float:
    """Validate a budget *amount* value: a non-bool finite number > 0.

    Single-sources the rule shared by ``budget.weekly_usd``,
    ``budget.codex.amount_usd``, and each ``budget.projects`` value (code-review
    #5). ``bool`` is an ``int`` subclass, so it's rejected explicitly. ``label``
    is the human field name used in the raised message (e.g.
    ``"budget.weekly_usd"``). Null handling stays at the call site — this helper
    only validates a value the caller has already decided must be a number.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _BudgetConfigError(f"{label} must be a number", field=label)
    if not math.isfinite(float(v)) or float(v) <= 0:
        raise _BudgetConfigError(
            f"{label} must be a finite number > 0", field=label
        )
    return float(v)


# Per-vendor budget period enums (calendar-period + Codex budgets feature).
# Claude budgets may use any of the three (default subscription-week, the
# existing reset-aware behavior); Codex budgets may NOT use subscription-week
# (it's an Anthropic-only concept), so Codex defaults to calendar-month. These
# are reused by the parser (`--period` choices) and the config layer.
BUDGET_PERIODS = ("subscription-week", "calendar-week", "calendar-month")
CODEX_BUDGET_PERIODS = ("calendar-week", "calendar-month")
CODEX_BUDGET_LEAVES = (
    "amount_usd", "period", "alerts_enabled", "alert_thresholds",
    "projected_enabled", "accounts",
)
_BUDGET_DEFAULTS = {
    "weekly_usd": None,            # None = no budget (default)
    "alerts_enabled": True,        # "on when set"
    "alert_thresholds": [90, 100],
    "projected_enabled": False,    # projected-pace opt-in (#121); default OFF
    "period": "subscription-week",  # Claude period; default = existing behavior
    "projects": {},               # per-project weekly $ budgets, keyed by git-root
    "project_alerts_enabled": False,  # per-project alerts opt-in (#19/#121); default OFF
    "accounts": {},               # per-account weekly $ budgets, keyed by account_key (#341)
    "codex": None,                # None = no Codex budget (nested block when set)
}
_BUDGET_CONFIG_VALID_KEYS = {
    "weekly_usd",
    "alerts_enabled",
    "alert_thresholds",
    "projected_enabled",
    "period",
    "projects",
    "project_alerts_enabled",
    "accounts",
    "codex",
}


def _validate_account_budget_map(v: object, label: str) -> "dict[str, float]":
    """Validate a per-account ``{account_key: usd}`` budget map (#341, spec §6).

    Mirrors the ``budget.projects`` value rule: keys are strings (immutable
    account keys — refs are normalized to keys at ``config set`` write time),
    each value a non-bool finite number > 0. Returns a cleaned copy."""
    if not isinstance(v, dict):
        raise _BudgetConfigError(
            f"{label} must be an object, got {type(v).__name__}", field=label
        )
    cleaned: "dict[str, float]" = {}
    for acc_key, acc_val in v.items():
        if not isinstance(acc_key, str) or not acc_key:
            raise _BudgetConfigError(
                f"{label} keys must be non-empty strings (account keys)",
                field=label,
            )
        if isinstance(acc_val, bool) or not isinstance(acc_val, (int, float)):
            raise _BudgetConfigError(
                f"{label} values must be numbers, "
                f"got {type(acc_val).__name__} for key {acc_key!r}",
                field=label,
            )
        if not math.isfinite(float(acc_val)) or float(acc_val) <= 0:
            raise _BudgetConfigError(
                f"{label} values must be finite numbers > 0, "
                f"got {acc_val!r} for key {acc_key!r}",
                field=label,
            )
        cleaned[acc_key] = float(acc_val)
    return cleaned


def _get_budget_config(cfg: dict) -> dict:
    """Return the validated, defaults-filled budget block.

    Raises _BudgetConfigError on invalid values. Unknown sub-keys emit a
    one-line warn-and-ignore (mirrors _get_alerts_config / the display.tz
    posture for forward compatibility).
    """
    out = dict(_BUDGET_DEFAULTS)
    out["alert_thresholds"] = list(_BUDGET_DEFAULTS["alert_thresholds"])
    out["projects"] = dict(_BUDGET_DEFAULTS["projects"])
    block = cfg.get("budget") if isinstance(cfg, dict) else None
    if block is None:
        return out
    if not isinstance(block, dict):
        raise _BudgetConfigError(
            f"budget must be an object, got {type(block).__name__}",
            field="budget",
        )
    # warn-and-ignore unknown keys (forward compat; matches _get_alerts_config)
    for k in block.keys():
        if k not in _BUDGET_CONFIG_VALID_KEYS:
            print(
                f"warning: ignoring unknown budget config key: {k}",
                file=sys.stderr,
            )

    if "weekly_usd" in block:
        v = block["weekly_usd"]
        if v is None:
            out["weekly_usd"] = None
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise _BudgetConfigError(
                "budget.weekly_usd must be a number or null",
                field="budget.weekly_usd",
            )
        elif not math.isfinite(float(v)) or float(v) <= 0:
            raise _BudgetConfigError(
                "budget.weekly_usd must be a finite number > 0",
                field="budget.weekly_usd",
            )
        else:
            out["weekly_usd"] = float(v)

    if "alerts_enabled" in block:
        v = block["alerts_enabled"]
        if not isinstance(v, bool):
            raise _BudgetConfigError(
                "budget.alerts_enabled must be a boolean",
                field="budget.alerts_enabled",
            )
        out["alerts_enabled"] = v

    if "alert_thresholds" in block:
        out["alert_thresholds"] = _validate_budget_thresholds(
            block["alert_thresholds"], "budget.alert_thresholds"
        )

    if "period" in block:
        v = block["period"]
        if not isinstance(v, str) or v not in BUDGET_PERIODS:
            raise _BudgetConfigError(
                "budget.period must be one of "
                f"{', '.join(BUDGET_PERIODS)}, got {v!r}",
                field="budget.period",
            )
        out["period"] = v

    if "projected_enabled" in block:
        v = block["projected_enabled"]
        if not isinstance(v, bool):
            raise _BudgetConfigError(
                "budget.projected_enabled must be a boolean",
                field="budget.projected_enabled",
            )
        out["projected_enabled"] = v

    if "projects" in block:
        v = block["projects"]
        if not isinstance(v, dict):
            raise _BudgetConfigError(
                f"budget.projects must be an object, got {type(v).__name__}",
                field="budget.projects",
            )
        cleaned: "dict[str, float]" = {}
        for proj_key, proj_val in v.items():
            if not isinstance(proj_key, str):
                raise _BudgetConfigError(
                    "budget.projects keys must be strings (canonical git-root paths)",
                    field="budget.projects",
                )
            # Reuse the weekly_usd numeric rule per value: a non-bool finite
            # number > 0 (bool is an int subclass, so reject it explicitly).
            if isinstance(proj_val, bool) or not isinstance(proj_val, (int, float)):
                raise _BudgetConfigError(
                    f"budget.projects values must be numbers, "
                    f"got {type(proj_val).__name__} for key {proj_key!r}",
                    field="budget.projects",
                )
            if not math.isfinite(float(proj_val)) or float(proj_val) <= 0:
                raise _BudgetConfigError(
                    f"budget.projects values must be finite numbers > 0, "
                    f"got {proj_val!r} for key {proj_key!r}",
                    field="budget.projects",
                )
            cleaned[proj_key] = float(proj_val)
        out["projects"] = cleaned

    if "project_alerts_enabled" in block:
        v = block["project_alerts_enabled"]
        if not isinstance(v, bool):
            raise _BudgetConfigError(
                "budget.project_alerts_enabled must be a boolean",
                field="budget.project_alerts_enabled",
            )
        out["project_alerts_enabled"] = v

    if "accounts" in block:
        out["accounts"] = _validate_account_budget_map(
            block["accounts"], "budget.accounts"
        )

    if "codex" in block:
        out["codex"] = _validate_codex_budget_block(block["codex"])

    return out


def _validate_budget_thresholds(v: object, label: str) -> "list[int]":
    """Validate + canonicalize a budget alert-thresholds list.

    Shared by the top-level ``budget.alert_thresholds`` and the nested
    ``budget.codex.alert_thresholds`` leaves. Entries must be ints in [1, 100]
    (bool is an int subclass and is rejected). Returns a sorted, deduped list;
    an empty list is allowed (alerts silenced).
    """
    if not isinstance(v, list):
        raise _BudgetConfigError(f"{label} must be a list of ints", field=label)
    cleaned: "list[int]" = []
    for t in v:
        if isinstance(t, bool) or not isinstance(t, int):
            raise _BudgetConfigError(
                f"{label} entries must be integers", field=label
            )
        if t < 1 or t > 100:
            raise _BudgetConfigError(
                f"{label} entries must be in [1, 100]", field=label
            )
        cleaned.append(t)
    return sorted(set(cleaned))  # empty list allowed (silenced)


def _validate_codex_budget_block(v: object) -> "dict | None":
    """Validate the nested ``budget.codex`` block (Codex per-vendor budget).

    ``None`` is the no-Codex-budget sentinel. When set, it's an object with a
    finite ``amount_usd`` > 0, a ``period`` in CODEX_BUDGET_PERIODS (NOT
    subscription-week — Anthropic-only), ``alerts_enabled`` bool (default
    False — opt-in, like every alert axis), ``alert_thresholds`` validated like
    the top-level budget thresholds (default [90, 100]), and
    ``projected_enabled`` bool (default False). Returns a defaults-filled copy.
    """
    if v is None:
        return None
    if not isinstance(v, dict):
        raise _BudgetConfigError(
            f"budget.codex must be an object or null, got {type(v).__name__}",
            field="budget.codex",
        )
    # warn-and-ignore unknown sub-keys (forward compat, like the parent block)
    for k in v.keys():
        if k not in CODEX_BUDGET_LEAVES:
            print(
                f"warning: ignoring unknown budget.codex config key: {k}",
                file=sys.stderr,
            )
    out: "dict" = {
        "amount_usd": None,
        "period": "calendar-month",     # Codex default (NO subscription-week)
        "alerts_enabled": False,        # opt-in, like every alert axis
        "alert_thresholds": [90, 100],
        "projected_enabled": False,
    }
    # amount_usd — the vendor-wide Codex budget, finite > 0. Required UNLESS a
    # non-empty per-account map (`accounts`, #341) is present: a Codex block may
    # be per-account-only (no vendor-wide amount), in which case amount_usd stays
    # None. Shares the positive-amount rule with weekly_usd / projects.
    _codex_accounts_raw = v.get("accounts")
    _has_codex_accounts = (
        isinstance(_codex_accounts_raw, dict) and len(_codex_accounts_raw) > 0
    )
    if "amount_usd" in v and v["amount_usd"] is not None:
        out["amount_usd"] = _validate_positive_budget_amount(
            v["amount_usd"], "budget.codex.amount_usd"
        )
    elif not _has_codex_accounts:
        raise _BudgetConfigError(
            "budget.codex.amount_usd is required (or set a per-account "
            "budget.codex.accounts map)",
            field="budget.codex.amount_usd",
        )

    if "period" in v:
        p = v["period"]
        if not isinstance(p, str) or p not in CODEX_BUDGET_PERIODS:
            raise _BudgetConfigError(
                "budget.codex.period must be one of "
                f"{', '.join(CODEX_BUDGET_PERIODS)} (NOT subscription-week), "
                f"got {p!r}",
                field="budget.codex.period",
            )
        out["period"] = p

    if "alerts_enabled" in v:
        ae = v["alerts_enabled"]
        if not isinstance(ae, bool):
            raise _BudgetConfigError(
                "budget.codex.alerts_enabled must be a boolean",
                field="budget.codex.alerts_enabled",
            )
        out["alerts_enabled"] = ae

    if "alert_thresholds" in v:
        out["alert_thresholds"] = _validate_budget_thresholds(
            v["alert_thresholds"], "budget.codex.alert_thresholds"
        )

    if "projected_enabled" in v:
        pe = v["projected_enabled"]
        if not isinstance(pe, bool):
            raise _BudgetConfigError(
                "budget.codex.projected_enabled must be a boolean",
                field="budget.codex.projected_enabled",
            )
        out["projected_enabled"] = pe

    # Per-account Codex budgets (#341, spec §6). Conditionally present so an
    # existing codex block (no accounts) validates byte-identically.
    if "accounts" in v:
        out["accounts"] = _validate_account_budget_map(
            v["accounts"], "budget.codex.accounts"
        )

    return out


def _budget_alerts_active(budget_cfg: dict) -> bool:
    """True iff a budget is set AND alerts are enabled."""
    return budget_cfg.get("weekly_usd") is not None and bool(
        budget_cfg.get("alerts_enabled")
    )


# === DB primitive ===================================================


def _apply_quota_projection_schema(conn: sqlite3.Connection) -> None:
    """Create the current durable provider-neutral quota projection schema.

    The physical observations remain in cache.db.  These tables are the
    idempotent interpreted index consumed by the Codex quota adapter; migration
    013 calls this same helper for old databases while ``open_db`` calls it for
    fresh installs before the migration dispatcher stamps the migration.
    """
    # account_key (#341 Task 2): every quota identity is (source_root_key,
    # account_key)-qualified — the UNIQUEs/PK are extended so two accounts on one
    # physical window never merge, and each carries `DEFAULT 'unattributed'` so a
    # NULL/single-account cache renders byte-identically (spec §2). No new epoch
    # bump: this rides Task 1's STATS_INDEX_EPOCH 1000->1001 rebuild.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS quota_window_blocks (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source                TEXT    NOT NULL,
            source_root_key       TEXT    NOT NULL,
            logical_limit_key     TEXT    NOT NULL,
            observed_slot         TEXT    NOT NULL,
            window_minutes        INTEGER NOT NULL CHECK(window_minutes > 0),
            limit_id              TEXT,
            limit_name            TEXT,
            resets_at_utc         TEXT    NOT NULL,
            nominal_start_at_utc  TEXT    NOT NULL,
            first_observed_at_utc TEXT    NOT NULL,
            last_observed_at_utc  TEXT    NOT NULL,
            first_percent         REAL    NOT NULL,
            current_percent       REAL    NOT NULL,
            last_source_path      TEXT    NOT NULL,
            last_line_offset      INTEGER NOT NULL,
            generation            TEXT    NOT NULL,
            orphaned_at           TEXT,
            account_key           TEXT    NOT NULL DEFAULT 'unattributed',
            -- public #5: the REVERSE MAP. `physical_group_key` records the
            -- physical window this block was materialized from, so the
            -- generation sweep can be scoped to the groups a bounded pass
            -- actually re-materialized instead of to whole roots.
            -- `physical_group_digest` is that group's contribution to its
            -- root's physical signature: the root value is a digest over the
            -- root's sorted (group key, group digest) pairs, which makes it
            -- ASSOCIATIVE. A bounded pass recomputes only the dirty groups'
            -- digests and re-derives the root value from the stored set —
            -- O(groups), 608 on the real store, against O(observations) at
            -- 211K. Hanging it off the blocks is what makes it self-maintaining:
            -- a group swept to nothing loses its blocks and drops out of the
            -- composition with them.
            physical_group_key    TEXT,
            physical_group_digest TEXT,
            UNIQUE(source, source_root_key, account_key, logical_limit_key,
                   observed_slot, window_minutes, resets_at_utc)
        );
        CREATE INDEX IF NOT EXISTS idx_quota_blocks_active
            ON quota_window_blocks(source, source_root_key, account_key, orphaned_at,
                                   logical_limit_key, observed_slot,
                                   window_minutes, resets_at_utc);

        CREATE TABLE IF NOT EXISTS quota_percent_milestones (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source                TEXT    NOT NULL,
            source_root_key       TEXT    NOT NULL,
            logical_limit_key     TEXT    NOT NULL,
            observed_slot         TEXT    NOT NULL,
            window_minutes        INTEGER NOT NULL CHECK(window_minutes > 0),
            resets_at_utc         TEXT    NOT NULL,
            percent_threshold     INTEGER NOT NULL CHECK(percent_threshold BETWEEN 1 AND 100),
            captured_at_utc       TEXT    NOT NULL,
            source_path           TEXT    NOT NULL,
            line_offset           INTEGER NOT NULL,
            high_water_percent    INTEGER NOT NULL CHECK(high_water_percent BETWEEN 1 AND 100),
            generation            TEXT    NOT NULL,
            orphaned_at           TEXT,
            account_key           TEXT    NOT NULL DEFAULT 'unattributed',
            UNIQUE(source, source_root_key, account_key, logical_limit_key,
                   observed_slot, window_minutes, resets_at_utc, percent_threshold)
        );
        CREATE INDEX IF NOT EXISTS idx_quota_milestones_active
            ON quota_percent_milestones(source, source_root_key, account_key, orphaned_at,
                                        logical_limit_key, observed_slot,
                                        window_minutes, resets_at_utc,
                                        percent_threshold);

        CREATE TABLE IF NOT EXISTS quota_threshold_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source                TEXT    NOT NULL,
            source_root_key       TEXT    NOT NULL,
            logical_limit_key     TEXT    NOT NULL,
            observed_slot         TEXT    NOT NULL,
            window_minutes        INTEGER NOT NULL CHECK(window_minutes > 0),
            resets_at_utc         TEXT    NOT NULL,
            threshold             INTEGER NOT NULL CHECK(threshold BETWEEN 1 AND 100),
            qualifying_kind       TEXT    NOT NULL CHECK(qualifying_kind IN ('actual','projected')),
            qualifying_percent    REAL,
            projected_percent     REAL,
            severity              TEXT    NOT NULL,
            created_at_utc        TEXT    NOT NULL,
            disposition           TEXT    NOT NULL CHECK(disposition IN ('alerted','suppressed_backfill')),
            alerted_at             TEXT,
            suppressed_at          TEXT,
            orphaned_at            TEXT,
            account_key           TEXT    NOT NULL DEFAULT 'unattributed',
            CHECK((disposition = 'alerted' AND alerted_at IS NOT NULL AND suppressed_at IS NULL)
               OR (disposition = 'suppressed_backfill' AND suppressed_at IS NOT NULL AND alerted_at IS NULL)),
            UNIQUE(source, source_root_key, account_key, logical_limit_key,
                   observed_slot, window_minutes, resets_at_utc, threshold)
        );
        CREATE INDEX IF NOT EXISTS idx_quota_threshold_events_active
            ON quota_threshold_events(source, source_root_key, account_key, orphaned_at,
                                      logical_limit_key, observed_slot,
                                      window_minutes, resets_at_utc, threshold);

        CREATE TABLE IF NOT EXISTS quota_projection_state (
            source_root_key    TEXT NOT NULL,
            account_key        TEXT NOT NULL DEFAULT 'unattributed',
            generation         TEXT NOT NULL,
            physical_signature TEXT NOT NULL,
            completed_at_utc   TEXT NOT NULL,
            PRIMARY KEY(source_root_key, account_key)
        );

        -- public #5: everything the incremental projector needs to know about
        -- its own last pass, keyed by provider source. One row, read once per
        -- reconcile.
        --
        -- `watermark_seq` is the highest `quota_window_change_log.seq` this
        -- index has consumed. It is written INSIDE the same stats transaction
        -- as the projection it describes, so the two commit or roll back
        -- together; a crash therefore replays a ledger range rather than
        -- skipping one, which is safe because re-materializing a group is
        -- idempotent. (Writing it after the commit would need a second stats
        -- transaction, and `run_stats_ingest` is the sole stats writer.)
        --
        -- `interpretation_version` invalidates the mechanism itself: a
        -- classification change alters interpreted keys with no row mutation
        -- for the ledger to observe, so a bump queues one complete pass.
        --
        -- `alerts_enabled` / `next_evaluation_at_utc` are the two alert axes
        -- that are not functions of window dirtiness — a delivery-gate
        -- transition, and a future-clocked observation that becomes eligible
        -- when wall time passes it with no row mutation at all.
        -- `next_evaluation_by_root_json` owns that scalar minimum: one earliest
        -- future capture per root, so a due hook tick can reconcile only the
        -- roots whose instants matured.
        --
        -- `last_full_pass_at` is the periodic verification's deadline. Two
        -- cases a scoped sweep structurally cannot see — a block whose physical
        -- group is absent from the cache entirely, and a milestone on a historic
        -- root no longer active — are otherwise repairable only by an
        -- interpretation bump, a rebuild or a burst overflow, none of which
        -- happen on a normal install. Every full pass stamps it, whatever
        -- triggered it, so the deadline is satisfied by whichever caller reaches
        -- it first and the bound on staleness is one interval rather than
        -- forever. NULL means "never verified", which reads as due.
        CREATE TABLE IF NOT EXISTS quota_projection_ledger_state (
            source                 TEXT NOT NULL,
            watermark_seq          INTEGER NOT NULL DEFAULT 0,
            interpretation_version INTEGER NOT NULL DEFAULT 0,
            alerts_enabled         INTEGER,
            next_evaluation_at_utc TEXT,
            last_full_pass_at      TEXT,
            next_evaluation_by_root_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(source)
        );

        CREATE TABLE IF NOT EXISTS quota_alert_arming (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            source            TEXT NOT NULL,
            source_root_key   TEXT NOT NULL,
            logical_limit_key TEXT NOT NULL,
            observed_slot     TEXT NOT NULL,
            window_minutes    INTEGER NOT NULL CHECK(window_minutes > 0),
            rule_fingerprint  TEXT NOT NULL,
            activated_at_utc  TEXT NOT NULL,
            account_key       TEXT NOT NULL DEFAULT 'unattributed',
            UNIQUE(source, source_root_key, account_key, logical_limit_key,
                   observed_slot, window_minutes)
        );
        """
    )
    # account_key backstops (#341): idempotent no-ops on the fresh CREATEs above
    # (which already carry the column), but they keep an already-1001 stats.db
    # that predates the column consistent. quota_projection_state widened its
    # PRIMARY KEY to (source_root_key, account_key) — a PK cannot be added by
    # ALTER, so a pre-#341-quota shape (disposable, re-derivable coherence index)
    # is DROP+recreated rather than back-filled.
    _c = sys.modules.get("cctally")
    add_column_if_missing = _c.add_column_if_missing if _c is not None else None
    _proj_cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(quota_projection_state)")
    }
    if "account_key" not in _proj_cols:
        conn.execute("DROP TABLE quota_projection_state")
        # SQLite stores `sqlite_schema.sql` verbatim apart from stripping
        # `IF NOT EXISTS`, and `_stats_schema_fingerprint` hashes that text, so
        # this body must stay character-for-character equal to the fresh-path
        # definition above (#496 S1 F18).
        conn.execute(
            "CREATE TABLE quota_projection_state (\n"
            "            source_root_key    TEXT NOT NULL,\n"
            "            account_key        TEXT NOT NULL DEFAULT 'unattributed',\n"
            "            generation         TEXT NOT NULL,\n"
            "            physical_signature TEXT NOT NULL,\n"
            "            completed_at_utc   TEXT NOT NULL,\n"
            "            PRIMARY KEY(source_root_key, account_key)\n"
            "        )"
        )
    if add_column_if_missing is not None:
        for _tbl in ("quota_window_blocks", "quota_percent_milestones",
                     "quota_threshold_events", "quota_alert_arming"):
            add_column_if_missing(
                conn, _tbl, "account_key", "TEXT NOT NULL DEFAULT 'unattributed'")
        # public #5 backstop, and NOT redundant with the CREATE above. An
        # epoch-MISMATCHED index resolves by rebuild and gets the fresh CREATE;
        # a LEGACY index (`user_version <= LEGACY_STATS_HEAD`) takes the
        # in-place cutover, where `CREATE TABLE IF NOT EXISTS` is a no-op over
        # the table it already has — so the reverse map would never appear and
        # every reconcile after the cutover would fail on `no such column`.
        for _col in ("physical_group_key", "physical_group_digest"):
            add_column_if_missing(conn, "quota_window_blocks", _col, "TEXT")
        # Same seam, epoch 1006: a LEGACY index that already took the epoch-1005
        # cutover carries `quota_projection_ledger_state` WITHOUT the periodic
        # verification's deadline, and `CREATE TABLE IF NOT EXISTS` above is a
        # no-op over the table it already has.
        add_column_if_missing(
            conn, "quota_projection_ledger_state", "last_full_pass_at", "TEXT")
        # Epoch 1007 / #460: same legacy-cutover seam. Current-epoch indexes
        # rebuild; a legacy index cuts over in place and needs the column added.
        add_column_if_missing(
            conn, "quota_projection_ledger_state",
            "next_evaluation_by_root_json", "TEXT NOT NULL DEFAULT '{}'")


def open_db(*, _target_path=None) -> sqlite3.Connection:
    # ``_target_path`` (internal, keyword-only) builds/opens the stats index at
    # an ALTERNATE path instead of the module-global ``DB_PATH`` — the seam
    # ``rebuild_stats_index`` uses to materialize a FRESH schema'd index at a
    # scratch/target path without rebinding the global (thread-safe: no other
    # caller's ``open_db()`` is affected). Every existing call passes nothing and
    # is byte-identical (spec §5.4 rebuild / §6.3 heal). ``None`` -> ``DB_PATH``.
    db_path = pathlib.Path(_target_path) if _target_path is not None else DB_PATH
    c = _cctally()
    # Spec §2.6 carve-out: open_db reaches the migration framework
    # (lives in _cctally_db + bin/cctally). Direct imports would
    # create a cycle (_cctally_db imports kernel from this module).
    # Local-binding via the call-time accessor preserves byte-stable
    # behavior with the reach list explicit at the top of the function.
    # Enforced by tests/test_kernel_extraction_invariants.py
    # test_core_accessor_use_is_bounded (lands in I2).
    add_column_if_missing = c.add_column_if_missing
    _canonical_5h_window_key = c._canonical_5h_window_key
    _backfill_week_reset_events = c._backfill_week_reset_events
    _backfill_five_hour_blocks = c._backfill_five_hour_blocks
    _run_pending_migrations = c._run_pending_migrations
    _STATS_MIGRATIONS = c._STATS_MIGRATIONS
    _log_migration_error = c._log_migration_error
    _clear_migration_error_log_entries = c._clear_migration_error_log_entries
    _reconcile_durable_applied_migration_errors = (
        c._reconcile_durable_applied_migration_errors
    )

    def _reconcile_incomplete_quota_projection(_conn):
        """#496 S5b §4.7's open-time gate, reached only on the steady-state path.

        A rebuild that published a valid index over a cache with an uncovered
        remainder durably marks its quota projection incomplete, and every
        projection read is then gated. Some open has to be able to resume that
        recovery, or the gate never lifts. Everything past the flag probe lives
        in `_cctally_journal`, which owns the recovery leg and the lock order;
        the reach is call-time for the same reason the opener policy's is.

        ARMED PROCESSES ONLY. The resumption reads the whole journal, and this
        function is reached by `cctally statusline` and by every hook tick. An
        unarmed process returns here, before the `_cctally_journal` import — so
        an interactive render pays neither the import nor the read. See
        `enable_quota_projection_reconciliation`.

        Any failure is swallowed. This runs on the hot open path, and a
        reconciliation that cannot proceed must leave the flag set — the
        fail-closed direction — rather than fail an unrelated command.
        """
        if not QUOTA_PROJECTION_RECONCILE_ENABLED:
            return
        try:
            import importlib as _il
            _il.import_module(
                "_cctally_journal").reconcile_incomplete_quota_projection(_conn)
        except Exception:
            return
    # Unified opener policy (spec §6.1). Call-time import so the shared PRAGMA
    # policy applies without a module-load cycle (_cctally_store imports this
    # module). Routed through importlib.import_module rather than a bare
    # `import _cctally_store` statement so core stays a static IMPORT leaf
    # (tests/test_kernel_extraction_invariants.py::test_core_imports_no_siblings):
    # the leaf rule guards against module-LOAD cycles, and a call-time reach
    # inside open_db() creates none — but the guard's regex matches any
    # `import _cctally_*` statement, so the deliberate call-time reach uses the
    # importlib form (still a recognized runtime-loader edge for the package
    # closure test). stats keeps its own schema-apply until Task 9 flips it to
    # the epoch gate — this task only routes the PRAGMAs through the shared table.
    import importlib
    _cctally_store = importlib.import_module("_cctally_store")

    ensure_dirs()
    # #453: probe the live index before the maintenance-shared opener. During
    # a large detached rebuild that opener can wait for its bounded 5-second
    # guard; a statusline arriving mid-replay would otherwise pay that delay
    # (and can reach more than one stats opener in a single render). The probe
    # reads only the fixed user_version field in the main-file header; it never
    # opens SQLite or bypasses the maintenance opener fence. Missing, invalid-
    # header and legacy indexes retain their established guarded-open paths.
    # The post-open epoch gate below remains a race-closing defense if the
    # version changes after this probe.
    if (
        _target_path is None
        and _cctally_store.stats_epoch_enabled()
        and _cctally_store.stats_epoch_rebuild_pending(db_path)
    ):
        outcome = _cctally_store.defer_stats_epoch_rebuild()
        raise c.StatsEpochRebuildDeferred(outcome)
    # #386: the opener half of the physical-replacement protocol. This replaces
    # three bare `repair_marker.exists()` checks around an unguarded connect —
    # which observed no quarantine-pending record and held no maintenance lock,
    # so a destructive maintenance path could publish its record, scan for
    # handles, and still be raced by an opener arriving before the first rename.
    # The guarded opener holds maintenance-SHARED across the marker/pending
    # checks AND the connect. Scratch (`_target_path`) opens keep the old
    # marker-only behaviour; see `_cctally_store.stats_open_guarded`.
    conn = _cctally_store.stats_open_guarded(db_path)
    conn.row_factory = sqlite3.Row
    # #279 S1 F4: probe connect + initial PRAGMAs so a corrupt stats.db
    # surfaces as a one-line diagnosis + staged exit 3 instead of a raw
    # traceback. With retained journal data stats.db is a disposable index and
    # the heal hook below rebuilds it; the guided error is for the pre-cutover
    # install with no retained journal data, whose stats.db may be the only
    # copy of its recorded history and is therefore never auto-recreated.
    # The catch boundary is DELIBERATELY narrow — ONLY the
    # connect/PRAGMA/probe below. The DDL + `_run_pending_migrations` region
    # further down is NOT wrapped: migration-handler failures have their own
    # logging/suppression contract and must not be mislabeled as corruption
    # (corruption that only surfaces mid-DDL stays a raw error; the probe
    # catches the common case).
    try:
        # §6.1 PRAGMA policy via the shared table (stats: DELETE / FULL /
        # busy_timeout 15000 / journal_size_limit 16 MiB / auto_vacuum unset).
        # #297: busy_timeout 15000 lets a writer wait out a slow-but-normal sync
        # (>5 s) instead of erroring "database is locked". The corruption probe
        # (SELECT 1) stays here inside the narrow
        # StatsDbCorruptError boundary.
        _cctally_store.apply_policy(conn, "stats")
        conn.execute("SELECT 1").fetchone()
        # §9.2 (#496 S6 F23). AFTER `apply_policy`, so any rollback journal
        # materialized during the transition is included in family hardening.
        _cctally_store._harden_stats_family(db_path)
    except sqlite3.DatabaseError as exc:
        try:
            conn.close()
        except Exception:
            pass
        # Classifier-gated corruption auto-heal (spec §6.3, Task 8). On a
        # POSITIVELY classified corruption of the REAL stats index (never a
        # ``_target_path`` rebuild build — that path is disarmed to avoid
        # recursion), hand off to the store's HEAL_HOOK: it re-checks under the
        # maintenance lock, writes the forensics bundle FIRST, builds and
        # validates a fresh index while the damaged family remains in place,
        # then preserves that family and atomically publishes the replacement.
        # A single retry of the connect+probe then runs against the fresh index;
        # a second failure surfaces loudly. HEAL returns False (declined) for a
        # non-corruption ``DatabaseError`` (BUSY, disk-full, permissions), the
        # dev-checkout-on-prod guard, or when a concurrent healer already fixed
        # it — all of which fall through to the original guided
        # StatsDbCorruptError so the manual path still applies.
        heal = getattr(_cctally_store, "HEAL_HOOK", None)
        heal_enabled = (
            os.environ.get("CCTALLY_TEST_DISABLE_STATS_AUTO_HEAL") != "1"
        )
        if (
            _target_path is None
            and heal is not None
            and heal_enabled
            and heal("stats", exc)
        ):
            # #386: the post-heal retry is an opener too. The heal released the
            # maintenance lock before returning, so another maintenance path can
            # legitimately own the family by now.
            conn = _cctally_store.stats_open_guarded(db_path)
            conn.row_factory = sqlite3.Row
            try:
                _cctally_store.apply_policy(conn, "stats")
                conn.execute("SELECT 1").fetchone()
                _cctally_store._harden_stats_family(db_path)
            except sqlite3.DatabaseError as exc2:
                try:
                    conn.close()
                except Exception:
                    pass
                raise c.StatsDbCorruptError(
                    f"stats.db is still unreadable after an auto-heal rebuild "
                    f"({exc2}). path: {db_path}. The damaged original was "
                    "quarantined under quarantine/ with a forensics bundle in "
                    "logs/. This is not expected — please report it."
                ) from exc2
        else:
            raise c.StatsDbCorruptError(
                f"stats.db appears corrupt or unreadable ({exc}). path: {db_path}. "
                "When journal data is retained for this install, stats.db is a "
                "disposable index and `cctally db rebuild --db stats` rebuilds "
                "it from the journal, losing nothing. On a pre-cutover install "
                "with no retained journal data it may be the only copy of your "
                "recorded history, so it is never auto-recreated: run `cctally "
                "db repair --db stats --yes`, which preserves the corrupt "
                "original before replacing anything. Do not copy, restore, or "
                "move the live DB by hand."
            ) from exc

    # ── stats.db epoch gate / in-place cutover (DB journal redesign §7.1/§8) ──
    # The epoch machinery engages ONLY against the FROZEN production stats
    # registry (len(_STATS_MIGRATIONS) == LEGACY_STATS_HEAD); under
    # CCTALLY_MIGRATION_TEST_MODE the injected 14th stats migration lifts the
    # count and disables it, so the migration-framework harness keeps exercising
    # the legacy dispatcher path with no cutover.
    _epoch_engaged = _cctally_store.stats_epoch_enabled()
    if _epoch_engaged:
        _uv = conn.execute("PRAGMA user_version").fetchone()[0]
        if _uv == STATS_INDEX_EPOCH:
            # STEADY STATE — zero schema work (spec §6.2/§7.1). Applies to a
            # ``_target_path`` open too: a rebuilt/scratch index is stamped at the
            # epoch, so opening it must be a pure connect + PRAGMA + version read.
            # Only the live path owns the live migration-error sentinel: a
            # scratch rebuild must not clear it before validated publication.
            if _target_path is None:
                _reconcile_durable_applied_migration_errors(
                    conn, _STATS_MIGRATIONS, "stats.db"
                )
                _reconcile_incomplete_quota_projection(conn)
            return conn
        if _target_path is None:
            if _uv > LEGACY_STATS_HEAD:
                # Neither legacy (<=13) nor the current epoch — a future epoch or
                # a stray value. Ordinary live callers must not pay whole-journal
                # replay inline or read the schema-incompatible old index. Hand
                # the existing synchronous resolver to its dedicated worker and
                # fail promptly; explicit maintenance and the worker itself call
                # that resolver directly. Corruption/legacy paths stay disjoint.
                conn.close()
                outcome = _cctally_store.defer_stats_epoch_rebuild()
                raise c.StatsEpochRebuildDeferred(outcome)
            # _uv <= LEGACY_STATS_HEAD → a pre-journal install: cut over below. A
            # dev/worktree binary must REFUSE to cut over a DB in the real prod
            # dir (mirrors #146 — the epoch stamp would brick the installed
            # release via DowngradeDetected). Leave the DB untouched; exit like
            # ProdMigrationRefused.
            if _cctally_store.would_block_prod_stats_cutover(db_path):
                conn.close()
                raise c.ProdMigrationRefused("stats.db", "cutover→epoch")
        # else: a ``_target_path`` build below LEGACY_STATS_HEAD (a fresh scratch
        # index) — fall through to the schema apply; the final block stamps the
        # epoch directly (no cutover export).

    # §6.2 one-shot backfill gate (Task 8). Read the ``stats_open_fixups`` marker
    # ONCE, right after the corruption probe. When True (the marker is stamped at
    # the binary's current fixup version) the three self-extinguishing open-time
    # backfills below — five_hour_window_key backfill, quota-projection schema,
    # historical five_hour_blocks rollup — plus their probe SELECTs are skipped
    # entirely, so the steady-state open does ZERO backfill work. Fresh / pre-gate
    # DBs read False, run all three once, and re-stamp the marker at the end. The
    # surrounding schema apply (CREATE TABLE IF NOT EXISTS / add_column_if_missing
    # / dispatcher) still runs every open — Task 9 folds THAT under the
    # STATS_INDEX_EPOCH gate; this task only removes the recurring backfill cost.
    # ── #386: the OPEN-TIME MUTATION REGIME (spec §3.1 second clause) ──
    # Everything below mutates stats.db: the full schema DDL, the quota
    # projection schema, the migration dispatcher, two backfills, the fixups
    # marker and the in-place cutover. Before #386 it ran under NO lock,
    # reachable from any of the 57 production `open_db` call sites — so two
    # commands racing a first open, an upgrade or a cutover both ran DDL on
    # the same file. The guard takes `stats.db.maintenance.lock` EXCLUSIVE
    # (re-entrant: the common case arrives from a caller that already holds
    # it) and enters the sanctioned write scope the authorizer checks.
    #
    # It is BELOW the epoch gate deliberately: the steady-state open returns
    # at `_uv == STATS_INDEX_EPOCH` above and never reaches here, so the hot
    # path takes no exclusive lock at all.
    with _cctally_store.stats_open_time_guard(live=_target_path is None):
        # Another opener can win the exclusive guard after this connection's
        # pre-lock epoch read, initialize/cut over the index, and stamp the
        # current epoch while this opener waits. Recheck under the guard before
        # acting on that stale legacy/fresh decision; otherwise the loser enters
        # the frozen migration dispatcher with user_version=1006 and reports a
        # false downgrade against legacy head 13.
        if _epoch_engaged and conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == STATS_INDEX_EPOCH:
            if _target_path is None:
                _reconcile_durable_applied_migration_errors(
                    conn, _STATS_MIGRATIONS, "stats.db"
                )
            return conn
        _fixups_current = _cctally_store.stats_open_fixups_current(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_usage_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                weekly_percent REAL NOT NULL,
                page_url TEXT,
                source TEXT NOT NULL DEFAULT 'userscript',
                payload_json TEXT NOT NULL,
                account_key TEXT NOT NULL DEFAULT 'unattributed'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_week_time
            ON weekly_usage_snapshots(week_start_date, captured_at_utc DESC, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                range_start_iso TEXT,
                range_end_iso TEXT,
                cost_usd REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'cctally-range-cost',
                mode TEXT NOT NULL DEFAULT 'auto',
                project TEXT,
                account_key TEXT NOT NULL DEFAULT 'unattributed'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cost_week_time
            ON weekly_cost_snapshots(week_start_date, captured_at_utc DESC, id DESC)
            """
        )

        add_column_if_missing(conn, "weekly_usage_snapshots", "week_start_at", "TEXT")
        add_column_if_missing(conn, "weekly_usage_snapshots", "week_end_at", "TEXT")
        # account_key (#341): the account dimension rides the STATS_INDEX_EPOCH bump
        # (a fresh rebuild carries it via the CREATE TABLE above); this backstop keeps
        # an already-1001 DB that predates the column consistent. DEFAULT
        # 'unattributed' — every production writer passes the key explicitly (rev 4.1
        # defensive-backstop rule), enforced by the structural writer-audit test.
        add_column_if_missing(
            conn, "weekly_usage_snapshots", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        add_column_if_missing(conn, "weekly_usage_snapshots", "five_hour_percent", "REAL")
        add_column_if_missing(conn, "weekly_usage_snapshots", "five_hour_resets_at", "TEXT")
        # five_hour_window_key — canonical (10-min-floored epoch) key for
        # jitter-tolerant equality. Anthropic's status-line API jitters
        # rate_limits.5h.resets_at by ~seconds within the same physical 5h
        # window; joining on the raw ISO string treats each jittered fetch as
        # a new window, escaping the monotonic clamp at cmd_record_usage.
        # Backfill is RESUMABLE: Python's sqlite3 auto-commits DDL,
        # so a process killed mid-loop would leave the column added with NULL
        # keys for unprocessed rows. The gating below detects that partial
        # state on the next open_db() call (`five_hour_resets_at IS NOT NULL
        # AND five_hour_window_key IS NULL`) and completes the backfill, so
        # the original Bug B can't silently re-emerge for half-migrated rows.
        needs_5h_key_backfill = add_column_if_missing(
            conn, "weekly_usage_snapshots", "five_hour_window_key", "INTEGER"
        )
        # §6.2 backfill gate (Task 8): the resumable-partial probe + the backfill
        # loop are open-time backfill work — skipped once the fixups marker is
        # stamped. (The `add_column_if_missing` above is schema apply, Task 9's.)
        if not _fixups_current:
            if not needs_5h_key_backfill and conn.execute(
                "SELECT 1 FROM weekly_usage_snapshots "
                "WHERE five_hour_resets_at IS NOT NULL "
                "  AND five_hour_window_key IS NULL "
                "LIMIT 1"
            ).fetchone() is not None:
                needs_5h_key_backfill = True
        else:
            needs_5h_key_backfill = False

        if needs_5h_key_backfill:
            backfill_rows = conn.execute(
                "SELECT id, five_hour_resets_at FROM weekly_usage_snapshots "
                "WHERE five_hour_resets_at IS NOT NULL "
                "  AND five_hour_window_key IS NULL"
            ).fetchall()
            for row in backfill_rows:
                try:
                    iso = row[1]
                    d = parse_iso_datetime(iso, "five_hour_resets_at backfill")
                    epoch = int(d.timestamp())
                    key = _canonical_5h_window_key(epoch)
                    conn.execute(
                        "UPDATE weekly_usage_snapshots "
                        "SET five_hour_window_key = ? WHERE id = ?",
                        (key, row[0]),
                    )
                except (ValueError, TypeError) as exc:
                    eprint(f"[migration] skipped row {row[0]}: {exc}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_weekly_usage_snapshots_5h_window_key "
                "ON weekly_usage_snapshots(five_hour_window_key)"
            )
            conn.commit()

        add_column_if_missing(conn, "weekly_cost_snapshots", "week_start_at", "TEXT")
        add_column_if_missing(conn, "weekly_cost_snapshots", "week_end_at", "TEXT")
        add_column_if_missing(conn, "weekly_cost_snapshots", "range_start_iso", "TEXT")
        add_column_if_missing(conn, "weekly_cost_snapshots", "range_end_iso", "TEXT")
        add_column_if_missing(
            conn, "weekly_cost_snapshots", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_week_start_at_time
            ON weekly_usage_snapshots(week_start_at, captured_at_utc DESC, id DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cost_week_start_at_time
            ON weekly_cost_snapshots(week_start_at, captured_at_utc DESC, id DESC)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS percent_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                week_start_date TEXT NOT NULL,
                week_end_date TEXT NOT NULL,
                week_start_at TEXT,
                week_end_at TEXT,
                percent_threshold INTEGER NOT NULL,
                cumulative_cost_usd REAL NOT NULL,
                marginal_cost_usd REAL,
                usage_snapshot_id INTEGER NOT NULL,
                cost_snapshot_id INTEGER NOT NULL,
                reset_event_id INTEGER NOT NULL DEFAULT 0,
                account_key TEXT NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, week_start_date, percent_threshold, reset_event_id)
            )
            """
        )

        add_column_if_missing(conn, "percent_milestones", "five_hour_percent_at_crossing", "REAL")
        add_column_if_missing(
            conn, "percent_milestones", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        # reset_event_id: segment column added by migration 005. Fresh-install
        # DBs get it via the live CREATE TABLE above + the dispatcher
        # fast-stamps the migration. Existing pre-005 DBs trip the migration's
        # rename-recreate-copy idiom (handler in _cctally_db.py); the handler's
        # fast-path probe stamps the marker when the column is already present
        # (covers the corner case where a partially-upgraded DB has the column
        # but not the new UNIQUE — re-run is safe).

        # alerted_at: populated by the alert-dispatch path when a milestone-INSERT
        # row's threshold matches the user's configured alerts.weekly_thresholds /
        # alerts.five_hour_thresholds (and alerts.enabled is true). NULL means
        # "alerts were disabled at the moment of crossing OR the threshold wasn't
        # in the configured list" — never "alert delivery failed" (dispatch is
        # best-effort and write-once forward-only). The matching ALTER for
        # `five_hour_milestones` lives right after that table's CREATE block
        # below, since the table doesn't exist yet at this point in `open_db()`.
        add_column_if_missing(conn, "percent_milestones", "alerted_at", "TEXT")

        # Mid-week reset events: when Anthropic advances `rate_limits.seven_day.
        # resets_at` before the previously-declared reset actually fires (i.e.,
        # gives the user a fresh weekly window before the old one naturally
        # expired), we record one row here so display + cost layers can treat
        # the effective reset moment as the old week's end AND the new week's
        # start — preventing the API's -7d-derived new week from overlapping
        # the old week. Inserted by cmd_record_usage on detection; read by
        # _apply_reset_events_to_weekrefs and the cost live-recompute path.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS week_reset_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at_utc        TEXT NOT NULL,
                old_week_end_at        TEXT NOT NULL,
                new_week_end_at        TEXT NOT NULL,
                effective_reset_at_utc TEXT NOT NULL,
                observed_pre_credit_pct REAL,
                account_key TEXT NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, old_week_end_at, new_week_end_at)
            )
            """
        )
        add_column_if_missing(
            conn, "week_reset_events", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        _backfill_week_reset_events(conn)

        # ── five_hour_reset_events (Anthropic-issued in-place 5h credits) ──
        # Parallel concept to ``week_reset_events`` for the 5h dimension; lives
        # adjacent in ``_apply_schema`` because the two carry the same kind of
        # signal at different cadences. Diverges from weekly in that the payload
        # is the *percent values* (prior + post) rather than boundary keys,
        # because the 5h variant has a stable ``five_hour_window_key`` and only
        # the percent moves. See spec
        # docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md §3.1
        # for rationale.
        #
        # UNIQUE(five_hour_window_key, effective_reset_at_utc) — supports stacked
        # credits across DISTINCT 10-min slots inside one block (see spec §2.3
        # "Bounded stacked-credit resolution" for the cap statement: ~30 distinct
        # slots per 5h block when floor matches ``_canonical_5h_window_key``'s
        # 600-second floor; same-slot collisions silently absorbed by
        # INSERT OR IGNORE — an intentional cap, not a bug).
        #
        # No FK per CLAUDE.md gotcha: FKs in this codebase are documentation-only
        # (``PRAGMA foreign_keys`` not enabled). ``five_hour_window_key`` provides
        # the join key without a formal FK.
        #
        # No ``_backfill_five_hour_reset_events`` call follows (forward-only ship
        # per spec Q5; historical backfill deferred to a future issue).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS five_hour_reset_events (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at_utc        TEXT NOT NULL,
                five_hour_window_key   INTEGER NOT NULL,
                prior_percent          REAL NOT NULL,
                post_percent           REAL NOT NULL,
                effective_reset_at_utc TEXT NOT NULL,
                account_key            TEXT NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, five_hour_window_key, effective_reset_at_utc)
            )
            """
        )
        add_column_if_missing(
            conn, "five_hour_reset_events", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")

        # ── five_hour_blocks (rollup, one row per API-anchored 5h block) ──
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS five_hour_blocks (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                five_hour_window_key          INTEGER NOT NULL,
                five_hour_resets_at           TEXT    NOT NULL,
                block_start_at                TEXT    NOT NULL,
                first_observed_at_utc         TEXT    NOT NULL,
                last_observed_at_utc          TEXT    NOT NULL,
                final_five_hour_percent       REAL    NOT NULL,
                seven_day_pct_at_block_start  REAL,
                seven_day_pct_at_block_end    REAL,
                crossed_seven_day_reset       INTEGER NOT NULL DEFAULT 0,
                total_input_tokens            INTEGER NOT NULL DEFAULT 0,
                total_output_tokens           INTEGER NOT NULL DEFAULT 0,
                total_cache_create_tokens     INTEGER NOT NULL DEFAULT 0,
                total_cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
                total_cost_usd                REAL    NOT NULL DEFAULT 0,
                is_closed                     INTEGER NOT NULL DEFAULT 0,
                created_at_utc                TEXT    NOT NULL,
                last_updated_at_utc           TEXT    NOT NULL,
                account_key                   TEXT    NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, five_hour_window_key)
            )
            """
        )
        add_column_if_missing(
            conn, "five_hour_blocks", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_blocks_block_start
            ON five_hour_blocks(block_start_at DESC)
            """
        )

        # ── five_hour_milestones (per-percent crossings inside a 5h block) ──
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS five_hour_milestones (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                percent_threshold           INTEGER NOT NULL,
                captured_at_utc             TEXT    NOT NULL,
                usage_snapshot_id           INTEGER NOT NULL,
                block_input_tokens          INTEGER NOT NULL DEFAULT 0,
                block_output_tokens         INTEGER NOT NULL DEFAULT 0,
                block_cache_create_tokens   INTEGER NOT NULL DEFAULT 0,
                block_cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                block_cost_usd              REAL    NOT NULL DEFAULT 0,
                marginal_cost_usd           REAL,
                seven_day_pct_at_crossing   REAL,
                reset_event_id              INTEGER NOT NULL DEFAULT 0,
                account_key                 TEXT    NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, five_hour_window_key, percent_threshold, reset_event_id),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_milestones_block
            ON five_hour_milestones(block_id)
            """
        )

        # alerted_at: see the matching ALTER on `percent_milestones` above for
        # rationale. Same write-once forward-only semantics: the alert-dispatch
        # path stamps this column on milestone-INSERT rows whose threshold
        # matches the user's configured `alerts.five_hour_thresholds`. NULL =
        # "alerts disabled at moment of crossing OR threshold not configured"
        # — never "delivery failed".
        add_column_if_missing(conn, "five_hour_milestones", "alerted_at", "TEXT")
        add_column_if_missing(
            conn, "five_hour_milestones", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")

        # reset_event_id: segment column added by migration 006. Fresh-install
        # DBs get it via the live CREATE TABLE above + the dispatcher fast-stamps
        # the migration marker (the live DDL must carry the column AND the 3-col
        # UNIQUE for fast-stamp to be safe — see spec §3.2). Existing pre-006
        # DBs trip the migration's rename-recreate-copy idiom (handler in
        # bin/_cctally_db.py); the handler's fast-path probe stamps the marker
        # when the column is already present (covers the corner case where a
        # partially-upgraded DB has the column but not the new UNIQUE — re-run
        # is safe). Mirrors weekly migration 005 / `percent_milestones`.

        # ── five_hour_block_models (per-(block, model) rollup-child) ──
        # MUST be created BEFORE the parent-backfill gate below, because
        # _backfill_five_hour_blocks writes into this table on the fresh-install
        # path. UNIQUE keyed on (five_hour_window_key, model) — durable across
        # parent rebuilds. Live writes use DELETE WHERE five_hour_window_key = ?.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS five_hour_block_models (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                model                       TEXT    NOT NULL,
                input_tokens                INTEGER NOT NULL DEFAULT 0,
                output_tokens               INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens         INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens           INTEGER NOT NULL DEFAULT 0,
                cost_usd                    REAL    NOT NULL DEFAULT 0,
                entry_count                 INTEGER NOT NULL DEFAULT 0,
                account_key                 TEXT    NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, five_hour_window_key, model),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            )
            """
        )
        add_column_if_missing(
            conn, "five_hour_block_models", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_block_models_block
            ON five_hour_block_models(block_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_block_models_window
            ON five_hour_block_models(five_hour_window_key)
            """
        )

        # ── five_hour_block_projects (per-(block, project_path) rollup-child) ──
        # NULL session_files.project_path → '(unknown)' sentinel at write time,
        # keeping reconcile invariant SUM(child.cost) == parent.total intact.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS five_hour_block_projects (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                block_id                    INTEGER NOT NULL,
                five_hour_window_key        INTEGER NOT NULL,
                project_path                TEXT    NOT NULL,
                input_tokens                INTEGER NOT NULL DEFAULT 0,
                output_tokens               INTEGER NOT NULL DEFAULT 0,
                cache_create_tokens         INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens           INTEGER NOT NULL DEFAULT 0,
                cost_usd                    REAL    NOT NULL DEFAULT 0,
                entry_count                 INTEGER NOT NULL DEFAULT 0,
                account_key                 TEXT    NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, five_hour_window_key, project_path),
                FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
            )
            """
        )
        add_column_if_missing(
            conn, "five_hour_block_projects", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_block_projects_block
            ON five_hour_block_projects(block_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_five_hour_block_projects_window
            ON five_hour_block_projects(five_hour_window_key)
            """
        )

        # ── budget_milestones (equiv-$ budget threshold crossings — issue #19) ──
        # Write-once, forward-only (the exact posture of `five_hour_milestones`). A
        # mid-week quota reset re-anchors `week_start_at` (see
        # `_resolve_current_budget_window`), so the new window naturally gets
        # fresh rows under UNIQUE(week_start_at, period, threshold) — no
        # `reset_event_id` segment column needed (unlike the percent/5h tables).
        # `week_start_at` stores the effective/re-anchored ISO string from the
        # resolver (`isoformat(timespec="seconds")`); the resolver's
        # `parse_iso_datetime` returns a HOST-LOCAL tz-aware datetime, so this
        # dedup key carries the host's UTC offset (e.g. `…T07:00:00-07:00`) —
        # host-consistent, NOT portable across hosts, same posture as
        # `five_hour_blocks.block_start_at`. Firing + reconcile + the dashboard
        # envelope all read/write the identical string on a given host, so the
        # UNIQUE dedup is exact. `alerted_at` is stamped BEFORE the osascript Popen
        # (set-then-dispatch invariant); NULL = "recorded without dispatch" (the
        # forward-only-from-set reconcile path) OR "not yet dispatched", never
        # "delivery failed".
        # Unified vendor-tagged table (#143): one row per (vendor, period_start_at,
        # period, threshold). `vendor` ∈ 'claude'|'codex'. `period_start_at` is the
        # resolved period-window start instant (subscription-week OR calendar
        # period-start). `period` is the configured period at crossing; NULL = pre-012
        # unknown. Owned by migration 012_unify_budget_milestones_vendor (merge of the
        # former budget_milestones + codex_budget_milestones). The Codex table is NO
        # LONGER live-created here — migration 012 drops it and this CREATE must not
        # resurrect it; migration 011 is hardened to skip it when absent (#143).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                vendor          TEXT    NOT NULL,
                period_start_at TEXT    NOT NULL,
                period          TEXT,
                threshold       INTEGER NOT NULL,
                budget_usd      REAL    NOT NULL,
                spent_usd       REAL    NOT NULL,
                consumption_pct REAL    NOT NULL,
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                account_key     TEXT    NOT NULL DEFAULT '*',
                UNIQUE(vendor, account_key, period_start_at, period, threshold)
            )
            """
        )
        add_column_if_missing(
            conn, "budget_milestones", "account_key", "TEXT NOT NULL DEFAULT '*'")

        # ── projected_milestones (week-average-pace projection crossings — #121) ──
        # Write-once, forward-only — same posture as `budget_milestones` (no
        # `reset_event_id` segment column). Two metrics share the table, keyed by
        # `metric` ('weekly_pct' | 'budget_usd'); a level fires once the
        # WEEK-AVERAGE projection (not the displayed high-end verdict) crosses
        # `threshold`. `denominator` snapshots the target AT crossing (target_usd
        # for budget_usd, 100.0 for weekly_pct) so the dashboard envelope renders
        # context "$312 of $300" / "102% of cap" from the ROW, not from live config
        # that may have changed since (Codex P0-4). A mid-week reset re-anchors
        # `week_start_at` (new window → fresh rows under the UNIQUE key), the
        # budget-pattern reset handling — hence NO `reset_event_id` column.
        # `alerted_at` is stamped BEFORE the osascript Popen (set-then-dispatch).
        # Schema owned by migration 011_budget_milestone_period_keys (the `period`
        # column + the period-inclusive UNIQUE; see _cctally_db.py). `period` is
        # NULL for pre-011 rows.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projected_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_at   TEXT    NOT NULL,   -- period-start instant (subscription-week OR calendar period-start; back-compat name)
                period          TEXT,               -- configured period at crossing; NULL = pre-011 unknown (migration 011)
                metric          TEXT    NOT NULL,   -- 'weekly_pct' | 'budget_usd' | 'codex_budget_usd'
                threshold       INTEGER NOT NULL,   -- 90 | 100
                projected_value REAL    NOT NULL,
                denominator     REAL    NOT NULL,   -- target_usd (budget / codex_budget) | 100.0 (weekly)
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                account_key     TEXT    NOT NULL DEFAULT '*',  -- '*' for vendor-budget metrics; real account for weekly_pct (Task 3)
                UNIQUE(account_key, week_start_at, period, metric, threshold)
            )
            """
        )
        add_column_if_missing(
            conn, "projected_milestones", "account_key", "TEXT NOT NULL DEFAULT '*'")

        # ── project_budget_milestones (per-project equiv-$ budget crossings) ──────
        # Plain CREATE TABLE IF NOT EXISTS, NO migration handler / backfill — the
        # same posture as `budget_milestones` / `projected_milestones` (write-once,
        # forward-only, framework-untracked). `project_key` is the NEW dimension in
        # the UNIQUE key: each project crosses each threshold once per week,
        # independently of every other project (issue #19 / #121, spec §5.1). It
        # stores the canonical git-root (`ProjectKey.bucket_path`), matched by string
        # equality against each session entry's resolved git-root. `budget_usd`
        # snapshots the project's target AT crossing time so the dashboard renders
        # "$26 of $25" from the ROW, not from live config that may have changed since
        # (the Codex P0-4 lesson, already baked into `budget_milestones` /
        # `projected_milestones`). A mid-week quota reset re-anchors `week_start_at`
        # (new window → fresh rows under the UNIQUE key) — budget-pattern reset
        # handling, hence NO `reset_event_id` segment column. `alerted_at` is stamped
        # BEFORE dispatch (set-then-dispatch invariant); NULL = "recorded without
        # dispatch" (forward-only-from-set reconcile) OR "not yet dispatched", never
        # "delivery failed". Lives BEFORE the migration dispatcher: a plain CREATE on
        # a framework-untracked table never touches `schema_migrations`, so the
        # dispatcher's fresh-install snapshot is unaffected.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_budget_milestones (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_at   TEXT    NOT NULL,
                project_key     TEXT    NOT NULL,   -- canonical git-root (bucket_path)
                threshold       INTEGER NOT NULL,
                budget_usd      REAL    NOT NULL,   -- project's target snapshotted AT crossing
                spent_usd       REAL    NOT NULL,
                consumption_pct REAL    NOT NULL,
                crossed_at_utc  TEXT    NOT NULL,
                alerted_at      TEXT,
                account_key     TEXT    NOT NULL DEFAULT '*',  -- account-blind this epic (spec §6): always '*'
                UNIQUE(account_key, week_start_at, project_key, threshold)
            )
            """
        )
        add_column_if_missing(
            conn, "project_budget_milestones", "account_key",
            "TEXT NOT NULL DEFAULT '*'")

        # In-place weekly partial-credit floor (issue #209, record-credit M2).
        # Plain CREATE TABLE IF NOT EXISTS, NO migration handler / NO user_version
        # bump — the same framework-untracked posture as `project_budget_milestones`
        # above. A `record-credit` invocation records a weekly credit (e.g.
        # 46% -> 31%) WITHOUT writing a `week_reset_events` row: a credit lowers the
        # current-7d clamp floor only and must NOT re-anchor the week window (the
        # `week_reset_events`-driven window-resolution code would otherwise show a
        # spurious "new week" and corrupt the forecast rate). `_reset_aware_floor`
        # (below) unions this table with `week_reset_events` so the four MAX-clamp
        # sites floor the current % to the post-credit value while the window stays
        # put. `effective_at_utc` is `floor_to_hour(at)` in UTC; `applied_at_utc` is
        # audit-only (kept out of goldens). Lives BEFORE the migration dispatcher: a
        # plain CREATE on a framework-untracked table never touches
        # `schema_migrations`, so the dispatcher's fresh-install snapshot is
        # unaffected. See docs/superpowers/specs/2026-06-19-record-credit-weekly-design.md §2/§4a.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_credit_floors (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_date         TEXT    NOT NULL,
                effective_at_utc        TEXT    NOT NULL,
                observed_pre_credit_pct REAL    NOT NULL,
                applied_at_utc          TEXT    NOT NULL,
                account_key             TEXT    NOT NULL DEFAULT 'unattributed',
                UNIQUE(account_key, week_start_date, effective_at_utc)
            )
            """
        )
        add_column_if_missing(
            conn, "weekly_credit_floors", "account_key",
            "TEXT NOT NULL DEFAULT 'unattributed'")

        # ── accounts registry (multi-account epic #341, spec §1/§2) ───────────────
        # Derived from the journal like all stats.db state: `account_observe` op
        # lines fold into rows here (via _apply_op_account_observe), `account_label`
        # ops set the user label. Framework-untracked (plain CREATE TABLE IF NOT
        # EXISTS, no migration / no user_version bump — same posture as
        # weekly_credit_floors above), so it never touches `schema_migrations` and
        # the stats-schema change rides the STATS_INDEX_EPOCH bump + rebuild, not a
        # new stats migration (the frozen-registry rule). `last_seen_utc` is derived
        # at fold time from the max `at` of any account-stamped line, NOT carried by
        # the observe record. `label_source` records provenance for the
        # user > switcher > auto precedence rule. A new empty table is byte-invisible
        # to every existing render, preserving the R8 byte-stability contract.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                account_key    TEXT PRIMARY KEY,
                provider       TEXT NOT NULL,
                natural_id     TEXT,
                email          TEXT,
                label          TEXT,
                plan_type      TEXT,
                label_source   TEXT NOT NULL DEFAULT 'auto',
                first_seen_utc TEXT,
                last_seen_utc  TEXT
            )
            """
        )

        # Stats migration 013 owns durable quota interpretation.  Keep the current
        # schema in the fresh-install path before the dispatcher, exactly like the
        # existing live CREATE tables; its handler calls this same idempotent helper
        # for an older stats.db and the dispatcher central-stamps on clean return.
        # §6.2 backfill gate (Task 8): the quota-projection schema apply is one of
        # the three open-time backfills — skipped once the fixups marker is stamped
        # (the marker is set only AFTER this ran, so marker-present ⇒ tables present).
        # Fresh installs read False and apply it here (the dispatcher fast-stamps 013
        # without running its handler, so this open-time call is the sole creator).
        if not _fixups_current:
            _apply_quota_projection_schema(conn)

        # Migration framework dispatcher. Replaces the prior inline gate stack
        # (has_blocks + _migration_done) with the framework's _run_pending_-
        # migrations entry point. See spec §2.3, §5.2 + the migration handlers
        # decorated with @stats_migration further down in this file.
        #
        # MUST run BEFORE any DDL or write that touches `schema_migrations`
        # (Codex P1 #1 fix on c3625ee + e7fdcc8): the dispatcher's fresh-install
        # detection snapshots `schema_migrations`'s existence in sqlite_master
        # BEFORE its own CREATE TABLE IF NOT EXISTS. Pre-creating the table
        # earlier in open_db() (or letting `_backfill_five_hour_blocks` insert
        # markers first) flips that snapshot to True on a brand-new DB and
        # dead-codes the stamp-only fast path. The dispatcher is now the sole
        # creator of `schema_migrations` + `schema_migrations_skipped`.
        _run_pending_migrations(
            conn, registry=_STATS_MIGRATIONS, db_label="stats.db",
        )

        # One-time historical backfill of five_hour_blocks (rollup only;
        # milestones are forward-only per spec §4.3 / [Write-once milestones]).
        # Idempotent via UNIQUE(five_hour_window_key) + INSERT OR IGNORE.
        # Runs AFTER the dispatcher so `schema_migrations` exists for the
        # marker INSERTs inside the backfill body, and so any fresh-install
        # stamp-only path the dispatcher took above is already committed.
        # §6.2 backfill gate (Task 8): the two probe SELECTs + the backfill + its
        # migration-003 re-invocation are open-time backfill work — skipped once the
        # fixups marker is stamped. Dead in the journal world (the ingest cycle writes
        # blocks with their snapshots), so it fires only on a pre-journal upgrade DB's
        # first open; after that the marker gates it out permanently.
        if not _fixups_current:
            existing = conn.execute(
                "SELECT 1 FROM five_hour_blocks LIMIT 1"
            ).fetchone()
            has_snapshots = conn.execute(
                "SELECT 1 FROM weekly_usage_snapshots "
                "WHERE five_hour_window_key IS NOT NULL "
                "  AND five_hour_percent     IS NOT NULL "
                "LIMIT 1"
            ).fetchone()
            if not existing and has_snapshots:
                inserted = _backfill_five_hour_blocks(conn)
                # Re-run the 5h dedup migration AFTER backfill creates parents.
                # The dispatcher above ran while five_hour_blocks was empty, so
                # the dedup handler no-op'd and stamped its marker. Snapshot
                # keys can carry jitter beyond the 600s canonical floor (the
                # 003_* migration handles up to 1800s grouping), so the
                # backfill's `DISTINCT five_hour_window_key` over those keys
                # can produce duplicate parent rows for one physical 5h
                # window. Without this re-invocation those duplicates persist
                # forever — the marker says it ran. Handler owns its own
                # BEGIN/COMMIT and is idempotent (no groups → no-op).
                #
                # Honor `db skip` here as well: if the operator marked 003 as
                # skipped (e.g., poison pill on their machine), we must NOT
                # back-door run the handler. Duplicates introduced by the
                # backfill will persist until they `db unskip` — which is the
                # explicit choice the skip records. Failure path mirrors the
                # dispatcher's contract: route through _log_migration_error so
                # the next interactive command renders the banner, and clear
                # the log entry on success so the banner auto-dismisses.
                if inserted > 0:
                    target_name = "003_merge_5h_block_duplicates_v1"
                    try:
                        skipped = {
                            row[0] for row in conn.execute(
                                "SELECT name FROM schema_migrations_skipped"
                            ).fetchall()
                        }
                    except sqlite3.OperationalError:
                        skipped = set()
                    if target_name not in skipped:
                        for _m in _STATS_MIGRATIONS:
                            if _m.name == target_name:
                                qualified = f"stats.db:{target_name}"
                                try:
                                    _m.handler(conn)
                                    _clear_migration_error_log_entries(qualified)
                                except Exception as exc:
                                    _log_migration_error(
                                        name=qualified,
                                        exc=exc,
                                        tb=traceback.format_exc(),
                                    )
                                    eprint(f"[migration {qualified}] failed: {exc}")
                                break

        # ── Append-only journal replay-identity columns + ingest cursor ──
        # (2026-07-22 DB journal redesign, spec §4.2 / §5.2). Every row a
        # journal fold materializes carries the originating line's stable `id`
        # in `journal_id`, with a partial UNIQUE index so the ingester's
        # INSERT OR IGNORE fold is idempotent under replay/re-ingest. Runs AFTER
        # the migration dispatcher so the columns land on the final (migrated)
        # table shape — migrations 005/006 recreate percent/5h milestone tables
        # and must not drop the column. All additive (add_column_if_missing /
        # CREATE ... IF NOT EXISTS), framework-untracked — same posture as
        # weekly_credit_floors / project_budget_milestones. Task 9 folds this
        # under the STATS_INDEX_EPOCH version gate; until then it is idempotent
        # per open (add_column_if_missing / IF NOT EXISTS no-op once present).
        for _jtable in (
            "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
            "five_hour_reset_events", "five_hour_blocks", "weekly_credit_floors",
            "percent_milestones", "five_hour_milestones", "budget_milestones",
            "projected_milestones", "project_budget_milestones",
        ):
            add_column_if_missing(conn, _jtable, "journal_id", "TEXT")
            conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_jtable}_journal_id "
                f"ON {_jtable}(journal_id) WHERE journal_id IS NOT NULL"
            )
        # Companion partial index (spec §5.3 harvest / Task 6 gate P2): the ingest
        # cycle's harvest scans every natural-keyed family for rows the pipeline
        # inserted this cycle (`WHERE journal_id IS NULL`). A partial index over
        # exactly those un-stamped rows keeps that scan O(this-cycle inserts), not
        # O(table) — at the 10x envelope the stamped rows are ~all of the table, so
        # a full scan would be pathological. Only the 8 HARVEST families need it
        # (the Model-A / op-fold tables — weekly_usage_snapshots, weekly_cost_
        # snapshots, weekly_credit_floors — are never harvest-scanned).
        for _htable in (
            "week_reset_events", "five_hour_reset_events", "five_hour_blocks",
            "percent_milestones", "five_hour_milestones", "budget_milestones",
            "projected_milestones", "project_budget_milestones",
        ):
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_htable}_journal_id_null "
                f"ON {_htable}(id) WHERE journal_id IS NULL"
            )
        # Single-row segment+offset consumption watermark (spec §5.2). The
        # applied_* pair is written atomically with the materialized rows and
        # acts as the trusted prefix when a cursor-only hand edit advances the
        # public pair without applying the skipped journal bytes (#410 Task B).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_cursor ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "segment TEXT NOT NULL, "
            "offset INTEGER NOT NULL, "
            "applied_segment TEXT, "
            "applied_offset INTEGER)"
        )
        add_column_if_missing(
            conn, "journal_cursor", "applied_segment", "TEXT"
        )
        add_column_if_missing(
            conn, "journal_cursor", "applied_offset", "INTEGER"
        )
        # Schema-apply compatibility for legacy/test-mode paths that reach this
        # DDL with a pre-pair cursor row. A released epoch-1003 index does NOT
        # use this as an upgrade shortcut: the epoch mismatch rebuilds it into
        # the complete current-epoch schema first.
        conn.execute(
            "UPDATE journal_cursor "
            "SET applied_segment = segment, applied_offset = offset "
            "WHERE applied_segment IS NULL AND applied_offset IS NULL"
        )
        # Disposable effective-event summary (#372 Task A). Durable truth remains
        # the append-only journal; rebuild repopulates this table from the shared
        # pure selector. The table lets live replay detect completed corrections
        # without inventing family-specific inverse operations.
        # `winning_sequence` and `conflict_hashes_json` (#496 S5b §3.2) complete
        # this table into the selector's whole per-event accumulator, which is
        # why S5b adds NO per-candidate table: same-revision containment needs
        # only the winning revision, the lowest-sequence winner and the set of
        # distinct content hashes observed at that revision, and this table is
        # already keyed one row per event id. The columns are declared HERE
        # rather than added by `add_column_if_missing`, because this is an epoch
        # bump: an epoch-current open returns before any schema work, so a
        # conditional column addition would never run on an upgraded install.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_effective_events ("
            "event_id TEXT PRIMARY KEY, "
            "rev INTEGER NOT NULL CHECK (rev >= 0), "
            "status TEXT NOT NULL CHECK (status IN ('active','tombstone')), "
            "content_hash TEXT NOT NULL, "
            "batch_id TEXT, "
            "event_json TEXT, "
            "winning_sequence INTEGER, "
            "conflict_hashes_json TEXT)"
        )
        # Disposable selector diagnostics (#402 Task A). The append-only journal
        # remains authoritative; rebuild/live preflight replace this bounded
        # summary after a complete correction-prefix selection.
        #
        # `available_after` (#496 S5b §3.2) is the selector's per-fingerprint
        # minimum sequence: a `journal_protocol_resolution` op that precedes it
        # is fatal, so an incremental pass that cannot re-derive it cannot
        # decide whether a resolution is legitimate.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_protocol_violations ("
            "fingerprint TEXT PRIMARY KEY, "
            "batch_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, "
            "violation_json TEXT NOT NULL, "
            "available_after INTEGER)"
        )
        # The primary key is the fingerprint, but every scoped selector read
        # filters `WHERE batch_id IN (...)` — on the status-line path, once per
        # merge tick. Without this index that is a full table scan, and the
        # rows-read guard in `tests/test_live_selection_496_s5b.py` cannot see
        # it: that proxy counts rows RETURNED, not rows scanned.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_protocol_violations_batch "
            "ON journal_protocol_violations(batch_id)"
        )
        # ── Durable selector state (#496 S5b §3.2) ──────────────────────────
        # `resolve_effective_events` accumulates six things over the record
        # stream and returns only a summary, so every live tick that meets a
        # correction record re-derives them from the whole journal prefix. These
        # three tables reproduce those six accumulators and nothing more, which
        # is what lets a validated generation seed incrementally instead.
        #
        # They are OPERATIONALLY AUTHORITATIVE only inside a validated current
        # generation. The journal remains ultimate truth: every rebuild, every
        # stale-generation fallback, Model-A versus harvest emission, bootstrap
        # handling and correction-batch application still derive from journal
        # records. Durable selector state may ACCELERATE a validated generation
        # and may never SUPERSEDE retained truth.
        #
        # One row, enforced structurally — unlike `stats_publication_stamp`,
        # whose duplicate row is a state that must resolve INDETERMINATE and
        # therefore may not be made impossible.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_selector_state ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "generation_record_path TEXT, "
            "generation_stamped_at_utc TEXT, "
            "covered_segment TEXT, "
            "covered_offset INTEGER, "
            "next_sequence INTEGER NOT NULL DEFAULT 0, "
            "selector_version INTEGER NOT NULL, "
            # `cutover_seen` distinguishes "no cutover op exists" from "the op
            # exists and recorded no account". A plain NULL cannot carry both
            # answers, and conflating them re-runs the whole-journal cutover
            # scan F20 exists to remove.
            "cutover_seen INTEGER NOT NULL DEFAULT 0, "
            "cutover_account_key TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_selector_batches ("
            "batch_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL "
            "  CHECK (status IN ('begin_only','completed','tainted')), "
            "action_count INTEGER, "
            "action_set_hash TEXT, "
            "begin_segment TEXT, begin_offset INTEGER, "
            "earliest_commit_segment TEXT, earliest_commit_offset INTEGER)"
        )
        # One row per marker and per action. A digest alone is not enough: when
        # a batch completes, the selector rebuilds every action's canonical core
        # to derive `actual_actions_hash`, and a split cycle — begin and actions
        # in an earlier generation, commit in this tick — decides completion NOW
        # from cores captured THEN. `action_core_json` is therefore retained
        # while the batch is `begin_only` OR `tainted`, and dropped only on
        # `completed`; an early taint does not end a batch's record stream.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS journal_selector_batch_records ("
            "batch_id TEXT NOT NULL, "
            "kind TEXT NOT NULL CHECK (kind IN ('marker','action')), "
            "key TEXT NOT NULL, "
            "record_digest TEXT NOT NULL, "
            "identity_digest TEXT, "
            "sequence INTEGER NOT NULL, "
            "action_core_json TEXT, "
            "PRIMARY KEY (batch_id, kind, key))"
        )
        # Set only by Stage 3, reserved here so no stage depends on a table a
        # later stage creates (#496 S5b §4.7). The stats quota projection is
        # materialized FROM cache.db, so a partial cache recovery publishes a
        # semantically partial projection inside the generation; the flag is the
        # per-transaction gate that keeps that projection from being served. The
        # target is VERSIONED, not a bare coordinate, so a target written by one
        # binary is never misread by another.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stats_quota_projection_state ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "incomplete INTEGER NOT NULL DEFAULT 0, "
            "target_version INTEGER NOT NULL DEFAULT 0, "
            "recovery_target_json TEXT)"
        )
        # In-place publication identity (#496 S3 §5). An in-place publish
        # attaches the scratch read-only and detaches it, so the scratch
        # survives commit and rollback identically and the publication marker's
        # `scratchPath` proxy inverts. This row is written INSIDE the
        # publication transaction, so it commits atomically with the content
        # and the `user_version` it describes and a crash before the commit
        # rolls it back. The opener compares it against the marker's
        # `recordPath` and knows without inference whether that publication
        # committed. Holds at most one row; single-row-ness is deliberately not
        # enforced structurally, because a duplicated row is one of the states
        # that must resolve INDETERMINATE rather than be made impossible.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stats_publication_stamp ("
            "record_path TEXT NOT NULL, "
            "started_at_utc TEXT NOT NULL, "
            "stamped_at_utc TEXT NOT NULL)"
        )

        # §6.2 backfill gate (Task 8): stamp the one-shot marker AFTER the three
        # open-time backfills ran, so the next open skips them (and their probes)
        # entirely. The marker table DDL runs ONLY on this "fixups ran" path, never
        # on the steady-state open. A crash before this commit leaves the marker
        # unset and everything re-runs idempotently next open (invariant).
        if not _fixups_current:
            _cctally_store.mark_stats_open_fixups_done(conn)

        conn.commit()

        # ── Epoch stamp / in-place cutover (DB journal redesign §7.1/§8) ──
        # The full schema is now applied (head 13 + journal_id columns + cursor). A
        # ``_target_path`` build (rebuild scratch) stamps the epoch DIRECTLY (no
        # export — the rebuild folds the journal itself). A real legacy install runs
        # the cutover: export history to a bootstrap segment, stamp ``journal_id`` on
        # every row, advance the cursor, and stamp the epoch — all atomic (spec §8).
        # Under test mode (epoch disabled) neither runs, so the DB stays at
        # len(registry) for the migration-framework harness.
        if _epoch_engaged:
            if _target_path is not None:
                conn.execute(f"PRAGMA user_version = {STATS_INDEX_EPOCH}")
                conn.commit()
            elif conn.execute("PRAGMA user_version").fetchone()[0] == LEGACY_STATS_HEAD:
                # Cut over ONLY once the legacy dispatcher reached the export baseline
                # (head 13). A DEFERRED migration (MigrationGateNotMet — e.g. the
                # 008/009/010 recompute gate) leaves user_version < 13; skip the
                # cutover so the next open retries the dispatcher first, then cuts over
                # (spec §8 step 1: "run any pending legacy stats migrations, reaching
                # the export baseline"). Never journal a pre-recompute stats shape.
                try:
                    importlib.import_module("_cctally_journal").run_cutover(conn)
                except BaseException:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise
        return conn


# === WeekRef cluster ================================================


def _canonicalize_optional_iso(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    s = value.strip()
    if s == "":
        return None
    normalized = _normalize_week_boundary_dt(parse_iso_datetime(s, label)).astimezone(dt.timezone.utc)
    return normalized.isoformat(timespec="seconds")


@dataclass(frozen=True)
class WeekRef:
    week_start: dt.date
    week_end: dt.date | None
    week_start_at: str | None
    week_end_at: str | None
    key: str


def make_week_ref(
    week_start_date: str,
    week_end_date: str | None,
    week_start_at: str | None = None,
    week_end_at: str | None = None,
) -> WeekRef:
    week_start = dt.date.fromisoformat(week_start_date)
    week_end = dt.date.fromisoformat(week_end_date) if week_end_date else None
    start_at = _canonicalize_optional_iso(week_start_at, "weekStartAt")
    end_at = _canonicalize_optional_iso(week_end_at, "weekEndAt")

    return WeekRef(
        week_start=week_start,
        week_end=week_end,
        week_start_at=start_at,
        week_end_at=end_at,
        key=week_start.isoformat(),
    )


# === Usage lookup ===================================================


def _get_latest_row_for_week(
    conn: sqlite3.Connection,
    table_name: str,
    week_ref: WeekRef,
    as_of_utc: str | None = None,
    *,
    account_key: str | None = None,
) -> sqlite3.Row | None:
    """Latest week row from a weekly snapshot table.

    ``account_key`` (#341): ``None`` = the account-blind merged read (today's
    behavior, byte-stable); a real key scopes to that account's rows — the
    ``--account`` render consumers and the write-path milestone-cost read (P2-1)
    pass it explicitly."""
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_params: tuple = () if account_key is None else (account_key,)
    if as_of_utc is None:
        return conn.execute(
            f"""
            SELECT *
            FROM {table_name}
            WHERE week_start_date = ?{acct_pred}
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """,
            (week_ref.week_start.isoformat(),) + acct_params,
        ).fetchone()
    return conn.execute(
        f"""
        SELECT *
        FROM {table_name}
        WHERE week_start_date = ?
          AND captured_at_utc <= ?{acct_pred}
        ORDER BY captured_at_utc DESC, id DESC
        LIMIT 1
        """,
        (week_ref.week_start.isoformat(), as_of_utc) + acct_params,
    ).fetchone()


def _reset_aware_floor(
    conn: sqlite3.Connection,
    week_start_date: str,
    week_start_at: str,
    week_end_at: str,
    *,
    account_key: str | None,
) -> str | None:
    """Return the latest in-week clamp floor (an ISO timestamp) across BOTH
    `week_reset_events` and `weekly_credit_floors`, or None when neither has a
    row for this week.

    ``account_key`` (#341, review finding 11): MANDATORY account context — no
    silent global fallback. A real key scopes both legs to that account's
    reset/credit rows (so one account's mid-week credit never clamps another's
    week); ``None`` is the explicit "all accounts" (merged) read used by the
    analytics floor path, byte-identical to today on a single-account install.

    This is the single chokepoint the four MAX-clamp sites consult to floor the
    current 7d % to the most-recent in-place credit / reset effective moment
    (record-credit M2, issue #209, spec §4a):
      - statusline `_hwm_clamp` 7d (bin/_cctally_statusline.py)
      - the record-usage write-site monotonic clamp (bin/_cctally_record.py)
      - `_resolve_reset_aware_hwm` (the --from default helper)
      - `project`'s `_load_week_snapshots` per-week MAX (bin/_cctally_project.py)

    A `week_reset_events` row counts iff its `effective_reset_at_utc` falls in
    `[week_start_at, week_end_at)`; a `weekly_credit_floors` row counts iff its
    `week_start_date` matches (record-credit always stamps `effective_at_utc`
    inside the week, validated at plan-build time).

    The latest floor wins via `ORDER BY unixepoch(floor_at) DESC LIMIT 1` —
    `unixepoch()`, NOT a textual `MAX(...)`: the two legs carry mixed offset
    spellings (`Z` / `+00:00`), and a lexical MAX would silently mis-order them
    on a non-UTC host (the same gotcha as the statusline clamp / 5h-block
    cross-reset flag; see the comment at bin/_cctally_statusline.py)."""
    acct_pred = "" if account_key is None else " AND account_key = ?"
    reset_params: tuple = (week_start_at, week_end_at) + (
        () if account_key is None else (account_key,))
    floor_params: tuple = (week_start_date,) + (
        () if account_key is None else (account_key,))
    row = conn.execute(
        f"""
        SELECT floor_at FROM (
            SELECT effective_reset_at_utc AS floor_at
              FROM week_reset_events
             WHERE unixepoch(effective_reset_at_utc) >= unixepoch(?)
               AND unixepoch(effective_reset_at_utc) <  unixepoch(?){acct_pred}
            UNION ALL
            SELECT effective_at_utc AS floor_at
              FROM weekly_credit_floors
             WHERE week_start_date = ?{acct_pred}
        )
        ORDER BY unixepoch(floor_at) DESC
        LIMIT 1
        """,
        reset_params + floor_params,
    ).fetchone()
    return row[0] if row and row[0] else None


def _floored_week_max(conn, rows, *, account_key=None):
    """Return {week_key -> per-week reset-aware-floored maximum weekly_percent}.

    ``rows`` is an iterable of
      (week_key, week_start_date, week_start_at, week_end_at,
       captured_at_utc, weekly_percent).

    Two-pass so floor resolution is independent of row order (#290): pass 1
    buckets rows per ``week_key`` and canonicalizes each week's first non-NULL
    (week_start_at, week_end_at) + its week_start_date; pass 2 resolves
    ``_reset_aware_floor`` once per week (keyed on week_start_date) and drops
    captures earlier than that floor before taking the week's maximum
    ``weekly_percent``. ``week_key`` is the caller's aggregation key (1:1 with a
    week); floor identity is week_start_date, so a NULL-bound legacy row cannot
    suppress the reset-event leg for a later anchored row of the same week.

    A NULL ``weekly_percent`` row is skipped. An unparseable ``captured_at_utc``
    under an active floor is RETAINED (epoch unknown), matching
    ``_cctally_project._load_week_snapshots``. All-NULL bounds resolve
    credit-floor-leg-only (the reset leg is inert: unixepoch(NULL) is NULL).
    A week whose every in-scope row is pre-floor is absent from the result.
    """
    # Pass 1: bucket + canonicalize bounds.
    buckets: dict = {}
    for wk, wsd, ws_at, we_at, cap, pct in rows:
        if pct is None:
            continue
        b = buckets.get(wk)
        if b is None:
            buckets[wk] = {
                "wsd": wsd, "ws_at": ws_at, "we_at": we_at,
                "rows": [(cap, float(pct))],
            }
            continue
        if b["wsd"] is None and wsd is not None:
            b["wsd"] = wsd
        if b["ws_at"] is None and ws_at is not None:
            b["ws_at"] = ws_at
        if b["we_at"] is None and we_at is not None:
            b["we_at"] = we_at
        b["rows"].append((cap, float(pct)))

    # Pass 2: resolve floor once per week, drop pre-floor captures, take max.
    result: dict = {}
    for wk, b in buckets.items():
        # Analytics floor path: ``account_key=None`` merges across accounts —
        # byte-identical to today on a single-account install (#341); a real key
        # scopes the floor to that account (the ``forecast --account`` dpp read).
        floor_iso = _reset_aware_floor(
            conn, b["wsd"], b["ws_at"], b["we_at"], account_key=account_key)
        floor_epoch = None
        if floor_iso:
            try:
                floor_epoch = int(
                    parse_iso_datetime(
                        floor_iso, "floored_week_max.floor"
                    ).timestamp()
                )
            except ValueError:
                floor_epoch = None
        best = None
        for cap, pct in b["rows"]:
            if floor_epoch is not None and cap is not None:
                try:
                    cap_epoch = int(
                        parse_iso_datetime(
                            str(cap), "floored_week_max.cap"
                        ).timestamp()
                    )
                except ValueError:
                    cap_epoch = None
                if cap_epoch is not None and cap_epoch < floor_epoch:
                    continue
            if best is None or pct > best:
                best = pct
        if best is not None:
            result[wk] = best
    return result


# --------------------------------------------------------------------------
# Active Claude account resolution (#341, spec §1 observe-and-stamp) — the
# identity source for record-usage / statusline / hook-tick obs stamping. A
# stat-read-stat over ``~/.claude.json`` (rewritten in place by Claude Code),
# mtime-cached so a hot status-line loop reads it at most once per file change.
# --------------------------------------------------------------------------

_ACTIVE_CLAUDE_ACCOUNT_CACHE: dict = {"sig": None, "identity": None}


def _resolve_active_claude_identity() -> dict:
    """Resolve the active Claude identity from ``~/.claude.json`` via the
    stable-read protocol, mtime-cached. Returns a dict
    ``{account_key, natural_id, email, plan_type}`` — ``account_key`` is the
    reserved ``unattributed`` sentinel and the rest ``None`` when stably-absent /
    api-key mode / torn mid-write. Never raises; never invents a guess."""
    import json as _json
    import _lib_accounts

    path = str(CLAUDE_JSON_PATH)
    try:
        st = os.stat(path)
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        sig = None
    cache = _ACTIVE_CLAUDE_ACCOUNT_CACHE
    if sig is not None and cache.get("sig") == sig:
        return cache["identity"]

    def _reader(data: bytes):
        try:
            obj = _json.loads(data)
        except (ValueError, TypeError):
            raise _lib_accounts.TornRead()
        if not isinstance(obj, dict):
            raise _lib_accounts.TornRead()
        oauth = obj.get("oauthAccount")
        nat = _lib_accounts.claude_natural_id(oauth)
        if nat is None:
            return None
        return {
            "natural_id": nat,
            "email": _lib_accounts.claude_email(oauth),
            "plan_type": (oauth.get("plan") if isinstance(oauth, dict) else None),
        }

    result = _lib_accounts.stable_read_identity(path, _reader)
    if result.status == "identified":
        info = result.value
        identity = {
            "account_key": _lib_accounts.account_key("claude", info["natural_id"]),
            "natural_id": info["natural_id"],
            "email": info.get("email"),
            "plan_type": info.get("plan_type"),
            # The three-valued read status (spec §1). ``account_key`` collapses
            # torn+stably_absent to the sentinel; consumers that must tell a
            # RESOLVED absence (single-account / api-key -> unattributed) from a
            # TRANSIENT torn read (defer / exit 2 for record-credit) read this.
            "status": "identified",
        }
    else:
        identity = {"account_key": _lib_accounts.UNATTRIBUTED,
                    "natural_id": None, "email": None, "plan_type": None,
                    "status": result.status}
    if sig is not None:
        cache["sig"] = sig
        cache["identity"] = identity
    return identity


def _resolve_active_claude_account() -> str:
    """The active Claude ``account_key`` (or ``unattributed``). Thin key-only
    accessor over :func:`_resolve_active_claude_identity` (mtime-cached)."""
    return _resolve_active_claude_identity()["account_key"]


def get_latest_usage_for_week(
    conn: sqlite3.Connection,
    week_ref: WeekRef,
    as_of_utc: str | None = None,
    *,
    account_key: str | None = None,
) -> sqlite3.Row | None:
    return _get_latest_row_for_week(
        conn, "weekly_usage_snapshots", week_ref, as_of_utc=as_of_utc,
        account_key=account_key,
    )
