"""Leaf-of-the-graph kernel for cctally.

Contains primitives that every sibling and bin/cctally itself depend on:
logging (eprint), datetime helpers, week-name/bounds, time-of-day,
alerts-config validation, open_db, WeekRef + make_week_ref,
get_latest_usage_for_week.

Path constants (APP_DIR, DB_PATH, LOG_DIR) intentionally live in
bin/cctally and are read here via a call-time _cctally() accessor —
this is the ONLY accessor use inside core. See
docs/superpowers/specs/2026-05-17-cctally-core-kernel-extraction.md §2.
"""
from __future__ import annotations
import datetime as dt
import os
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from typing import Any


def _cctally():
    return sys.modules["cctally"]


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
    c = _cctally()
    c.APP_DIR.mkdir(parents=True, exist_ok=True)
    c.LOG_DIR.mkdir(parents=True, exist_ok=True)


# === Alerts validation cluster ======================================


class _AlertsConfigError(ValueError):
    """Raised by _get_alerts_config on invalid alerts block."""


_ALERTS_CONFIG_VALID_KEYS = {"enabled", "weekly_thresholds", "five_hour_thresholds"}


def _validate_threshold_list(name: str, value: object) -> "list[int]":
    """Validate one of the alerts threshold lists.

    Rules: non-empty list of plain ints (NOT bools — `bool` is an `int`
    subclass), each in [1, 100], strictly increasing (no duplicates).
    Error messages mention `alerts.<name>` so users can locate the
    offending key in their config.json.
    """
    if not isinstance(value, list):
        raise _AlertsConfigError(f"alerts.{name} must be a list of integers")
    if len(value) == 0:
        raise _AlertsConfigError(
            f"alerts.{name} must not be empty (disable alerts via alerts.enabled=false)"
        )
    out: "list[int]" = []
    prev = -1
    seen: "set[int]" = set()
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise _AlertsConfigError(
                f"alerts.{name} items must be integers, got {type(item).__name__}: {item!r}"
            )
        if item < 1 or item > 100:
            raise _AlertsConfigError(
                f"alerts.{name} items must be in [1, 100], got {item}"
            )
        if item in seen:
            raise _AlertsConfigError(
                f"alerts.{name} contains duplicate value {item}"
            )
        if item <= prev:
            raise _AlertsConfigError(
                f"alerts.{name} must be strictly increasing, got {prev} then {item}"
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
        raise _AlertsConfigError("alerts must be an object")
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
            f"alerts.enabled must be a JSON boolean, got {type(enabled).__name__}: {enabled!r}"
        )
    weekly = _validate_threshold_list(
        "weekly_thresholds", block.get("weekly_thresholds", [90, 95])
    )
    five_hour = _validate_threshold_list(
        "five_hour_thresholds", block.get("five_hour_thresholds", [90, 95])
    )
    return {
        "enabled": enabled,
        "weekly_thresholds": weekly,
        "five_hour_thresholds": five_hour,
    }


# === DB primitive ===================================================


def open_db() -> sqlite3.Connection:
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

    ensure_dirs()
    conn = sqlite3.connect(c.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
            payload_json TEXT NOT NULL
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
            project TEXT
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
    if not needs_5h_key_backfill and conn.execute(
        "SELECT 1 FROM weekly_usage_snapshots "
        "WHERE five_hour_resets_at IS NOT NULL "
        "  AND five_hour_window_key IS NULL "
        "LIMIT 1"
    ).fetchone() is not None:
        needs_5h_key_backfill = True

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
            UNIQUE(week_start_date, percent_threshold, reset_event_id)
        )
        """
    )

    add_column_if_missing(conn, "percent_milestones", "five_hour_percent_at_crossing", "REAL")
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
            UNIQUE(old_week_end_at, new_week_end_at)
        )
        """
    )
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
            UNIQUE(five_hour_window_key, effective_reset_at_utc)
        )
        """
    )

    # ── five_hour_blocks (rollup, one row per API-anchored 5h block) ──
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS five_hour_blocks (
            id                            INTEGER PRIMARY KEY AUTOINCREMENT,
            five_hour_window_key          INTEGER NOT NULL UNIQUE,
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
            last_updated_at_utc           TEXT    NOT NULL
        )
        """
    )
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
            UNIQUE(five_hour_window_key, percent_threshold, reset_event_id),
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
            UNIQUE(five_hour_window_key, model),
            FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
        )
        """
    )
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
            UNIQUE(five_hour_window_key, project_path),
            FOREIGN KEY (block_id) REFERENCES five_hour_blocks(id)
        )
        """
    )
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

    conn.commit()
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
) -> sqlite3.Row | None:
    if as_of_utc is None:
        return conn.execute(
            f"""
            SELECT *
            FROM {table_name}
            WHERE week_start_date = ?
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """,
            (week_ref.week_start.isoformat(),),
        ).fetchone()
    return conn.execute(
        f"""
        SELECT *
        FROM {table_name}
        WHERE week_start_date = ?
          AND captured_at_utc <= ?
        ORDER BY captured_at_utc DESC, id DESC
        LIMIT 1
        """,
        (week_ref.week_start.isoformat(), as_of_utc),
    ).fetchone()


def get_latest_usage_for_week(
    conn: sqlite3.Connection,
    week_ref: WeekRef,
    as_of_utc: str | None = None,
) -> sqlite3.Row | None:
    return _get_latest_row_for_week(
        conn, "weekly_usage_snapshots", week_ref, as_of_utc=as_of_utc,
    )
