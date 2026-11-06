"""The trigger-maintained content revision of stats.db's Claude domain (#1050).

An in-place rewrite of a Claude-published stats row (a Model-A convergence, a
reviewed rederive, the open-block projection, a probe ``UPDATE``) moves none of
the append-shaped legs the dashboard compares (`MAX(id)`, `_reset_sig`, the
armed-row digests). This kernel gives that domain one identity
(spec `docs/superpowers/specs/2026-11-04-1050-correction-signal-design.md`
§4.1–§4.4):

* ``stats_content_revision`` — a singleton row carrying the kernel
  ``format_version``, the ``domain_generation`` an installation repair bumps,
  and ``seq``, the global sequence every tracked mutation advances;
* ``stats_content_revision_scopes`` — per ``(kind, account_key, scope_key)``
  the ``seq`` of the latest tracked mutation that dirtied that scope, with the
  epoch range a ``block`` scope spans.

Triggers on the §4.2 tables do the bookkeeping inside the writer's own
statement: the singleton is advanced FIRST, then each dirtied scope is
upserted with ``revision`` = the new ``seq``. A rolled-back write rolls its
trigger effects back with it, and an assignment of identical values fires
nothing (every ``WHEN`` compares null-safely with ``IS NOT``). The revision
tables carry no triggers and ``recursive_triggers`` stays off.

Installation never bumps the stats epoch. ``classify`` reads only (one bounded
singleton read plus one ``sqlite_schema`` read), so a ``current`` open writes
and locks nothing; ``ensure_stats_content_revision`` installs an ``absent``
store and repairs a ``partial`` one under the open-time maintenance guard with
a zero wait, and leaves the store untouched on contention.

``read_claude_content_revision`` returns the opaque equality token of the
Claude content domain within stats.db, including its publication generation
(the #790 contract): the token changes on every tracked mutation, on every
repair and on every publication, and is ``""`` only for a never-mutated,
never-repaired, never-published store, so a fresh store stays byte-neutral.

Stdlib only, and no sibling import statement (core imports this module, so it
must stay a proven leaf). The guard and its contention error are reached at
call time, and only when an install is needed.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import secrets
import sqlite3
import types
from typing import Mapping, NamedTuple, Sequence

#: The kernel's object format. A store whose singleton names a higher format is
#: ``unsupported`` (left alone); a lower one is ``partial`` (repaired in place).
KERNEL_FORMAT_VERSION: int = 1

STATE_CURRENT = "current"
STATE_ABSENT = "absent"
STATE_PARTIAL = "partial"
STATE_UNSUPPORTED = "unsupported"
STATE_MALFORMED = "malformed"
STATES = frozenset({
    STATE_CURRENT, STATE_ABSENT, STATE_PARTIAL, STATE_UNSUPPORTED,
    STATE_MALFORMED,
})

REVISION_TABLE = "stats_content_revision"
SCOPE_TABLE = "stats_content_revision_scopes"
USAGE_SNAPSHOT_INDEX = "idx_five_hour_milestones_usage_snapshot_id"
TRIGGER_PREFIX = "trg_content_revision_"

SCOPE_KINDS = ("cohort", "bucket", "block", "reset_point")

REVISION_TABLE_SQL = (
    "CREATE TABLE stats_content_revision ("
    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
    "format_version INTEGER NOT NULL CHECK (format_version >= 1), "
    "domain_generation INTEGER NOT NULL DEFAULT 0 "
    "CHECK (domain_generation >= 0), "
    "seq INTEGER NOT NULL DEFAULT 0 CHECK (seq >= 0))"
)
SCOPE_TABLE_SQL = (
    "CREATE TABLE stats_content_revision_scopes ("
    "kind TEXT NOT NULL "
    "CHECK (kind IN ('cohort', 'bucket', 'block', 'reset_point')), "
    "account_key TEXT NOT NULL, "
    "scope_key TEXT NOT NULL, "
    "start_epoch INTEGER, "
    "end_epoch INTEGER, "
    "revision INTEGER NOT NULL, "
    "PRIMARY KEY (kind, account_key, scope_key))"
)
#: §4.2: every trigger lookup is a primary-key or indexed equality, and the
#: snapshot → five-hour milestone reference had no index of its own.
USAGE_SNAPSHOT_INDEX_SQL = (
    "CREATE INDEX idx_five_hour_milestones_usage_snapshot_id "
    "ON five_hour_milestones(usage_snapshot_id)"
)


class ContentRevision(NamedTuple):
    """One read of the identity. ``token``/``cycle_basis`` are ``None`` in
    every untrusted state (``absent``, ``partial``, ``unsupported``)."""

    state: str
    token: "str | None"
    cycle_basis: "str | None"


class StatsContentRevisionUnreadable(Exception):
    """The identity cannot be read: a ``malformed`` store, or more than one
    ``stats_publication_stamp`` row. Callers treat it like any other failed
    dispatch read (a nonce leg), never as an empty identity."""


class TableClassification(NamedTuple):
    """One §4.2 row. ``compared`` columns fire the UPDATE trigger when they
    differ null-safely; ``excluded`` columns are bookkeeping and never do.
    ``insert_rule``: ``"tracked"`` (every INSERT), ``"below_max"`` (only an
    INSERT below the table's ``MAX(id)``), ``"exempt"`` (another leg already
    covers INSERT) or ``"untracked"``. ``scopes``: the scope kinds dirtied
    from OLD and NEW (``()`` = the global ``seq`` only). ``owner_predicate``:
    a SQL predicate over ``{R}`` (the OLD/NEW row) restricting tracking to the
    Claude-owned rows, or ``None``."""

    compared: "tuple[str, ...]"
    excluded: "tuple[str, ...]"
    insert_rule: str
    scopes: "tuple[str, ...]"
    owner_predicate: "str | None" = None


class ScopeRow(NamedTuple):
    kind: str
    account_key: str
    scope_key: str
    start_epoch: "int | None"
    end_epoch: "int | None"
    revision: int


# ── §4.2 the classification table ─────────────────────────────────────────

TRACKED_TABLES: "Mapping[str, TableClassification]" = types.MappingProxyType({
    "weekly_usage_snapshots": TableClassification(
        compared=(
            "id", "captured_at_utc", "week_start_date", "week_end_date",
            "week_start_at", "week_end_at", "weekly_percent", "page_url",
            "source", "payload_json", "account_key",
            "weekly_observation_held", "five_hour_percent",
            "five_hour_resets_at", "five_hour_window_key", "journal_id",
        ),
        excluded=(), insert_rule="below_max", scopes=("bucket", "block"),
    ),
    "weekly_cost_snapshots": TableClassification(
        compared=(
            "id", "captured_at_utc", "week_start_date", "week_end_date",
            "week_start_at", "week_end_at", "range_start_iso",
            "range_end_iso", "cost_usd", "source", "mode", "project",
            "account_key", "journal_id",
        ),
        excluded=(), insert_rule="below_max", scopes=("bucket",),
    ),
    "percent_milestones": TableClassification(
        compared=(
            "id", "captured_at_utc", "week_start_date", "week_end_date",
            "week_start_at", "week_end_at", "percent_threshold",
            "cumulative_cost_usd", "marginal_cost_usd", "usage_snapshot_id",
            "cost_snapshot_id", "reset_event_id", "account_key",
            "five_hour_percent_at_crossing", "alerted_at", "journal_id",
        ),
        excluded=(), insert_rule="tracked", scopes=("cohort", "bucket"),
    ),
    "five_hour_milestones": TableClassification(
        compared=(
            "id", "block_id", "five_hour_window_key", "percent_threshold",
            "captured_at_utc", "usage_snapshot_id", "block_input_tokens",
            "block_output_tokens", "block_cache_create_tokens",
            "block_cache_read_tokens", "block_cost_usd", "marginal_cost_usd",
            "seven_day_pct_at_crossing", "reset_event_id", "account_key",
            "alerted_at", "journal_id",
        ),
        excluded=(), insert_rule="tracked", scopes=("block",),
    ),
    "five_hour_blocks": TableClassification(
        compared=(
            "id", "five_hour_window_key", "five_hour_resets_at",
            "block_start_at", "first_observed_at_utc", "last_observed_at_utc",
            "final_five_hour_percent", "seven_day_pct_at_block_start",
            "seven_day_pct_at_block_end", "crossed_seven_day_reset",
            "total_input_tokens", "total_output_tokens",
            "total_cache_create_tokens", "total_cache_read_tokens",
            "total_cost_usd", "pricing_provenance_json", "is_closed",
            "account_key", "journal_id",
        ),
        excluded=("created_at_utc", "last_updated_at_utc"),
        insert_rule="tracked", scopes=("block",),
    ),
    "five_hour_block_models": TableClassification(
        compared=(
            "id", "block_id", "five_hour_window_key", "model",
            "input_tokens", "output_tokens", "cache_create_tokens",
            "cache_read_tokens", "cost_usd", "entry_count", "account_key",
        ),
        excluded=(), insert_rule="tracked", scopes=(),
    ),
    "five_hour_block_projects": TableClassification(
        compared=(
            "id", "block_id", "five_hour_window_key", "project_path",
            "input_tokens", "output_tokens", "cache_create_tokens",
            "cache_read_tokens", "cost_usd", "entry_count", "account_key",
        ),
        excluded=(), insert_rule="tracked", scopes=(),
    ),
    "five_hour_reset_events": TableClassification(
        compared=(
            "id", "detected_at_utc", "five_hour_window_key", "prior_percent",
            "post_percent", "effective_reset_at_utc", "account_key",
            "journal_id",
        ),
        excluded=(), insert_rule="tracked", scopes=("block",),
    ),
    "week_reset_events": TableClassification(
        compared=(
            "id", "detected_at_utc", "old_week_end_at", "new_week_end_at",
            "effective_reset_at_utc", "observed_pre_credit_pct",
            "account_key", "origin_observation_id", "journal_id",
        ),
        excluded=(), insert_rule="exempt", scopes=("reset_point",),
    ),
    "weekly_credit_floors": TableClassification(
        compared=(
            "id", "week_start_date", "effective_at_utc",
            "observed_pre_credit_pct", "account_key", "journal_id",
        ),
        excluded=("applied_at_utc",), insert_rule="exempt", scopes=(),
    ),
    "claude_weekly_observation_index": TableClassification(
        compared=(
            "observation_id", "account_key", "captured_at_utc",
            "weekly_percent", "weekly_resets_at_utc",
            "weekly_reading_genuine", "weekly_freshness_eligible",
        ),
        excluded=(), insert_rule="exempt", scopes=(),
    ),
    # The Claude ownership predicates of `_CLAUDE_STATS_DIGEST_RELATIONS`
    # (`bin/_lib_dashboard_sources.py`): Codex budget rows are not Claude
    # content, and every project budget row is (that relation has no
    # predicate, so neither does its row here).
    "budget_milestones": TableClassification(
        compared=(
            "id", "vendor", "period_start_at", "period", "threshold",
            "budget_usd", "spent_usd", "consumption_pct", "crossed_at_utc",
            "alerted_at", "account_key", "journal_id",
        ),
        excluded=(), insert_rule="untracked", scopes=(),
        owner_predicate="{R}.vendor <> 'codex'",
    ),
    "projected_milestones": TableClassification(
        compared=(
            "id", "week_start_at", "period", "metric", "threshold",
            "projected_value", "denominator", "crossed_at_utc", "alerted_at",
            "account_key", "journal_id",
        ),
        excluded=(), insert_rule="untracked", scopes=(),
        owner_predicate="{R}.metric <> 'codex_budget_usd'",
    ),
    "project_budget_milestones": TableClassification(
        compared=(
            "id", "week_start_at", "project_key", "threshold", "budget_usd",
            "spent_usd", "consumption_pct", "crossed_at_utc", "alerted_at",
            "account_key", "journal_id",
        ),
        excluded=(), insert_rule="untracked", scopes=(),
    ),
})


# ── trigger SQL ───────────────────────────────────────────────────────────

_SEQ_ADVANCE = (
    "UPDATE stats_content_revision SET seq = seq + 1 WHERE singleton = 1;"
)
_SEQ_VALUE = "(SELECT seq FROM stats_content_revision WHERE singleton = 1)"
# `MIN(COALESCE(start_epoch, x), x)` widening, with the incoming side also
# coalesced so a NULL epoch (an unparseable timestamp, or a scope kind that
# has no range) never erases a recorded one: SQLite's multi-argument MIN/MAX
# return NULL when any argument is NULL.
_UPSERT_TAIL = (
    " ON CONFLICT (kind, account_key, scope_key) DO UPDATE SET "
    "start_epoch = MIN(COALESCE(start_epoch, excluded.start_epoch), "
    "COALESCE(excluded.start_epoch, start_epoch)), "
    "end_epoch = MAX(COALESCE(end_epoch, excluded.end_epoch), "
    "COALESCE(excluded.end_epoch, end_epoch)), "
    "revision = excluded.revision;"
)
_UPSERT_HEAD = (
    "INSERT INTO stats_content_revision_scopes "
    "(kind, account_key, scope_key, start_epoch, end_epoch, revision) "
)


class _ScopeSelect(NamedTuple):
    """One scope lookup over the row alias ``{R}``: the select list (kind,
    account, key, start, end), an optional FROM and the WHERE. The WHERE is
    never empty: a FROM-carrying SELECT followed by ``ON CONFLICT`` is the
    documented UPSERT parsing ambiguity, so even a FROM-less one ends with
    ``WHERE true``."""

    columns: str
    source: str
    where: str


_BLOCK_COLUMNS = (
    "'block', b.account_key, CAST(b.five_hour_window_key AS TEXT), "
    "unixepoch(b.block_start_at), unixepoch(b.five_hour_resets_at)"
)


def _scope_selects(table: str) -> "tuple[_ScopeSelect, ...]":
    """The scope lookups one row of ``table`` dirties, over alias ``{R}``."""
    if table in ("weekly_usage_snapshots",):
        return (
            _ScopeSelect(
                "'bucket', {R}.account_key, {R}.week_start_date, NULL, NULL",
                "", "true"),
            # Every block whose five-hour milestone references this snapshot
            # (the milestone publishes the snapshot's weekly value).
            _ScopeSelect(
                _BLOCK_COLUMNS,
                "FROM five_hour_milestones AS m, five_hour_blocks AS b",
                "m.usage_snapshot_id = {R}.id AND b.id = m.block_id"),
        )
    if table == "weekly_cost_snapshots":
        return (
            _ScopeSelect(
                "'bucket', {R}.account_key, {R}.week_start_date, NULL, NULL",
                "", "true"),
        )
    if table == "percent_milestones":
        return (
            _ScopeSelect(
                "'cohort', {R}.account_key, "
                "{R}.week_start_date || '|' || {R}.reset_event_id, NULL, NULL",
                "", "true"),
            _ScopeSelect(
                "'bucket', {R}.account_key, {R}.week_start_date, NULL, NULL",
                "", "true"),
        )
    if table == "five_hour_milestones":
        # The parent block by `block_id`, else by its natural key.
        return (
            _ScopeSelect(
                _BLOCK_COLUMNS, "FROM five_hour_blocks AS b",
                "b.id = {R}.block_id"),
            _ScopeSelect(
                _BLOCK_COLUMNS, "FROM five_hour_blocks AS b",
                "b.account_key = {R}.account_key "
                "AND b.five_hour_window_key = {R}.five_hour_window_key "
                "AND NOT EXISTS (SELECT 1 FROM five_hour_blocks AS p "
                "WHERE p.id = {R}.block_id)"),
        )
    if table == "five_hour_blocks":
        return (
            _ScopeSelect(
                "'block', {R}.account_key, "
                "CAST({R}.five_hour_window_key AS TEXT), "
                "unixepoch({R}.block_start_at), "
                "unixepoch({R}.five_hour_resets_at)",
                "", "true"),
        )
    if table == "five_hour_reset_events":
        return (
            _ScopeSelect(
                _BLOCK_COLUMNS, "FROM five_hour_blocks AS b",
                "b.account_key = {R}.account_key "
                "AND b.five_hour_window_key = {R}.five_hour_window_key"),
        )
    if table == "week_reset_events":
        return tuple(
            _ScopeSelect(
                f"'reset_point', {{R}}.account_key, "
                f"CAST(unixepoch({{R}}.{column}) AS TEXT), "
                f"unixepoch({{R}}.{column}), unixepoch({{R}}.{column})",
                "", f"unixepoch({{R}}.{column}) IS NOT NULL")
            for column in (
                "effective_reset_at_utc", "old_week_end_at",
                "new_week_end_at")
        )
    return ()


def _render(template: str, row: str) -> str:
    return template.replace("{R}", row)


def _upsert(select: _ScopeSelect, row: str) -> str:
    source = f" {_render(select.source, row)}" if select.source else ""
    return (
        f"{_UPSERT_HEAD}SELECT {_render(select.columns, row)}, {_SEQ_VALUE}"
        f"{source} WHERE {_render(select.where, row)}{_UPSERT_TAIL}"
    )


def _body(table: str, rows: "Sequence[str]") -> str:
    statements = [_SEQ_ADVANCE]
    for row in rows:
        statements.extend(_upsert(s, row) for s in _scope_selects(table))
    return " BEGIN " + " ".join(statements) + " END"


def _owner(cls: TableClassification, row: str) -> "str | None":
    if cls.owner_predicate is None:
        return None
    return f"({_render(cls.owner_predicate, row)})"


def _trigger_sql() -> "dict[str, str]":
    """Every kernel trigger, name → its exact `sqlite_schema.sql`."""
    out: "dict[str, str]" = {}
    for table, cls in TRACKED_TABLES.items():
        diff = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in cls.compared)
        update_when = f"({diff})"
        if cls.owner_predicate is not None:
            update_when += f" AND ({_owner(cls, 'OLD')} OR {_owner(cls, 'NEW')})"
        out[f"{TRIGGER_PREFIX}{table}_update"] = (
            f"CREATE TRIGGER {TRIGGER_PREFIX}{table}_update "
            f"AFTER UPDATE ON {table} WHEN {update_when}"
            f"{_body(table, ('OLD', 'NEW'))}"
        )
        delete_when = (
            f" WHEN {_owner(cls, 'OLD')}"
            if cls.owner_predicate is not None else ""
        )
        out[f"{TRIGGER_PREFIX}{table}_delete"] = (
            f"CREATE TRIGGER {TRIGGER_PREFIX}{table}_delete "
            f"AFTER DELETE ON {table}{delete_when}{_body(table, ('OLD',))}"
        )
        if cls.insert_rule in ("tracked", "below_max"):
            insert_when = (
                f" WHEN EXISTS (SELECT 1 FROM {table} WHERE id > NEW.id)"
                if cls.insert_rule == "below_max" else ""
            )
            out[f"{TRIGGER_PREFIX}{table}_insert"] = (
                f"CREATE TRIGGER {TRIGGER_PREFIX}{table}_insert "
                f"AFTER INSERT ON {table}{insert_when}"
                f"{_body(table, ('NEW',))}"
            )
    return out


TRIGGER_SQL: "Mapping[str, str]" = types.MappingProxyType(_trigger_sql())
#: Every kernel-owned object, name → SQL: what the rebuild contract requires.
KERNEL_TABLES = (REVISION_TABLE, SCOPE_TABLE)
KERNEL_INDEXES = (USAGE_SNAPSHOT_INDEX,)
KERNEL_TRIGGERS = tuple(sorted(TRIGGER_SQL))
_TABLE_SQL = {REVISION_TABLE: REVISION_TABLE_SQL, SCOPE_TABLE: SCOPE_TABLE_SQL}


def trigger_lookup_queries() -> "tuple[tuple[str, str], ...]":
    """Every trigger lookup as a standalone query over named parameters, for
    `EXPLAIN QUERY PLAN` (§8.4): each scope SELECT with ``{R}.col`` bound as
    ``:col``, and the below-max INSERT predicate."""
    out = []
    for table, cls in TRACKED_TABLES.items():
        for index, select in enumerate(_scope_selects(table)):
            source = f" {select.source}" if select.source else ""
            sql = re.sub(
                r"\{R\}\.(\w+)", r":\1",
                f"SELECT {select.columns}{source} WHERE {select.where}")
            out.append((f"{table}#{index}", sql))
        if cls.insert_rule == "below_max":
            out.append((f"{table}#below_max",
                        f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id > :id)"))
    out.append(("seq#advance",
                "UPDATE stats_content_revision SET seq = seq + 1 "
                "WHERE singleton = 1"))
    out.append(("scope#upsert",
                "SELECT revision FROM stats_content_revision_scopes "
                "WHERE kind = :kind AND account_key = :account_key "
                "AND scope_key = :scope_key"))
    return tuple(out)


# ── classification (reads only) ───────────────────────────────────────────

_SCHEMA_READ = (
    "SELECT type, name, sql FROM sqlite_schema "
    "WHERE name IN (?, ?, ?) "
    "OR (type = 'trigger' AND substr(name, 1, ?) = ?)"
)


def _inspect(conn) -> "tuple[str, tuple | None]":
    """The §4.3 state and, when the singleton is valid, its
    ``(format_version, domain_generation, seq)``."""
    objects = conn.execute(
        _SCHEMA_READ,
        (REVISION_TABLE, SCOPE_TABLE, USAGE_SNAPSHOT_INDEX,
         len(TRIGGER_PREFIX), TRIGGER_PREFIX),
    ).fetchall()
    tables = {str(r[1]): r[2] for r in objects if r[0] == "table"}
    if not tables:
        return STATE_ABSENT, None
    if len(tables) != 2 or any(
            tables[name] != sql for name, sql in _TABLE_SQL.items()):
        return STATE_MALFORMED, None
    try:
        rows = conn.execute(
            "SELECT singleton, format_version, domain_generation, seq "
            "FROM stats_content_revision LIMIT 2"
        ).fetchall()
    except sqlite3.Error:
        return STATE_MALFORMED, None
    if len(rows) != 1:
        return STATE_MALFORMED, None
    values = tuple(rows[0])
    if any(type(v) is not int for v in values):
        return STATE_MALFORMED, None
    singleton, format_version, generation, seq = values
    if singleton != 1 or format_version < 1 or generation < 0 or seq < 0:
        return STATE_MALFORMED, None
    singleton_values = (format_version, generation, seq)
    if format_version > KERNEL_FORMAT_VERSION:
        return STATE_UNSUPPORTED, singleton_values
    if format_version < KERNEL_FORMAT_VERSION:
        return STATE_PARTIAL, singleton_values
    present = {
        str(r[1]): r[2] for r in objects if r[0] in ("trigger", "index")
    }
    expected = dict(TRIGGER_SQL)
    expected[USAGE_SNAPSHOT_INDEX] = USAGE_SNAPSHOT_INDEX_SQL
    if present != expected:
        return STATE_PARTIAL, singleton_values
    return STATE_CURRENT, singleton_values


def classify(conn) -> str:
    """The store's §4.3 installation state. Reads only."""
    return _inspect(conn)[0]


# ── installation ──────────────────────────────────────────────────────────

def _default_guard(live: bool):
    store = importlib.import_module("_cctally_store")
    return store.stats_open_time_guard(live=live, wait_seconds=0.0)


def _contention_errors() -> "tuple[type, ...]":
    db = importlib.import_module("_cctally_db")
    return (db.StatsDbMaintenanceError,)


def _is_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc) or "busy" in str(exc))


def _install(conn, state: str, singleton) -> None:
    """Create or repair every kernel object; the caller owns the transaction.
    Triggers are created last, so seeding the singleton fires nothing."""
    for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' "
            "AND substr(name, 1, ?) = ?",
            (len(TRIGGER_PREFIX), TRIGGER_PREFIX)).fetchall():
        conn.execute(f'DROP TRIGGER IF EXISTS "{row[0]}"')
    conn.execute(f"DROP INDEX IF EXISTS {USAGE_SNAPSHOT_INDEX}")
    if state == STATE_ABSENT:
        conn.execute(REVISION_TABLE_SQL)
        conn.execute(SCOPE_TABLE_SQL)
        conn.execute(
            "INSERT INTO stats_content_revision "
            "(singleton, format_version, domain_generation, seq) "
            "VALUES (1, ?, 0, 0)", (KERNEL_FORMAT_VERSION,))
    else:
        # A repair is a new tracking domain over existing content: rows may
        # have moved while tracking was incomplete, so the identity moves
        # once. `seq` only ever grows.
        _format, generation, seq = singleton
        conn.execute(
            "UPDATE stats_content_revision SET format_version = ?, "
            "domain_generation = ?, seq = ? WHERE singleton = 1",
            (KERNEL_FORMAT_VERSION, generation + 1, seq + 1))
    conn.execute(USAGE_SNAPSHOT_INDEX_SQL)
    for name in KERNEL_TRIGGERS:
        conn.execute(TRIGGER_SQL[name])


def ensure_stats_content_revision(conn, *, live: bool, guard=None) -> str:
    """Install or repair the kernel's objects when needed; return the state
    after the call (§4.3).

    ``current``, ``unsupported`` and ``malformed`` stores are returned as they
    are, with nothing written and no lock taken. An ``absent`` or ``partial``
    store is reclassified and acted on under the open-time maintenance guard
    (zero wait) in one transaction. Contention — on the guard or on SQLite's
    own write lock — leaves the store unchanged and returns its state; any
    other failure propagates. ``guard`` overrides the default
    ``stats_open_time_guard(live=live, wait_seconds=0.0)`` (a test seam).
    """
    state = classify(conn)
    if state not in (STATE_ABSENT, STATE_PARTIAL):
        return state
    contention = _contention_errors()
    previous_timeout = None
    try:
        with (guard(live) if guard is not None else _default_guard(live)):
            state, singleton = _inspect(conn)
            if state not in (STATE_ABSENT, STATE_PARTIAL):
                return state
            # SQLite's own write lock is contention too: an opener must not
            # wait out another writer's transaction (busy_timeout 15 s) for
            # optional work the next open retries.
            previous_timeout = int(
                conn.execute("PRAGMA busy_timeout").fetchone()[0])
            conn.execute("PRAGMA busy_timeout = 0")
            own = not conn.in_transaction
            conn.execute("BEGIN IMMEDIATE" if own else "SAVEPOINT scr_install")
            try:
                state, singleton = _inspect(conn)
                if state in (STATE_ABSENT, STATE_PARTIAL):
                    _install(conn, state, singleton)
                if own:
                    conn.commit()
                else:
                    conn.execute("RELEASE scr_install")
            except BaseException:
                if own:
                    conn.rollback()
                else:
                    conn.execute("ROLLBACK TO scr_install")
                    conn.execute("RELEASE scr_install")
                raise
    except contention:
        pass
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
    finally:
        if previous_timeout is not None:
            conn.execute(f"PRAGMA busy_timeout = {previous_timeout:d}")
    return classify(conn)


# ── the identity ──────────────────────────────────────────────────────────

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _stamp_rows(conn) -> list:
    try:
        rows = conn.execute(
            "SELECT record_path, started_at_utc, stamped_at_utc "
            "FROM stats_publication_stamp").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    # `record_path` names a private file; only its digest takes part.
    return sorted(
        [hashlib.sha256(str(r[0]).encode("utf-8")).hexdigest(),
         None if r[1] is None else str(r[1]),
         None if r[2] is None else str(r[2])]
        for r in rows)


def read_claude_content_revision(stats_conn) -> ContentRevision:
    """Read the identity (§4.4). Joins an open transaction without ending it;
    otherwise owns one short ``BEGIN`` … ``ROLLBACK``. Raises
    `StatsContentRevisionUnreadable` on a ``malformed`` store or more than one
    publication stamp row."""
    own = not stats_conn.in_transaction
    if own:
        stats_conn.execute("BEGIN")
    try:
        state, singleton = _inspect(stats_conn)
        if state == STATE_MALFORMED:
            raise StatsContentRevisionUnreadable(
                "stats content revision objects are malformed")
        stamps = _stamp_rows(stats_conn)
        if len(stamps) > 1:
            raise StatsContentRevisionUnreadable(
                f"stats_publication_stamp holds {len(stamps)} rows")
        if state != STATE_CURRENT:
            return ContentRevision(state, None, None)
        format_version, generation, seq = singleton
        stamp_value = stamps or None
        if seq == 0 and generation == 0 and stamp_value is None:
            token = ""
        else:
            user_version = int(
                stats_conn.execute("PRAGMA user_version").fetchone()[0])
            token = hashlib.sha256(_canonical(
                [format_version, generation, user_version, stamp_value, seq]
            ).encode("utf-8")).hexdigest()[:16]
        cycle_basis = (
            None if generation == 0 and stamp_value is None
            else _canonical([format_version, generation, stamp_value])
        )
        return ContentRevision(state, token, cycle_basis)
    finally:
        if own:
            stats_conn.rollback()


def revision_leg(rev: ContentRevision) -> str:
    """The published leg: the token (``""`` included), else a fresh nonce,
    so an untrusted store never compares equal across reads."""
    if isinstance(rev.token, str):
        return rev.token
    return "?" + secrets.token_hex(8)


def comparable_identity(rev: ContentRevision) -> tuple:
    """What a build bracket compares: stable across reads of an unchanged
    store in every state, different on any state change."""
    if rev.state == STATE_CURRENT:
        return (rev.state, rev.token)
    return (rev.state,)


def read_scope_rows(stats_conn, *, account_key: "str | None") -> "list[ScopeRow]":
    """Every scope row, in primary-key order. Strict to ``account_key`` when
    it is given; merged across every account (``unattributed`` included) when
    it is ``None``. An absent store has none."""
    sql = (
        "SELECT kind, account_key, scope_key, start_epoch, end_epoch, revision "
        "FROM stats_content_revision_scopes"
    )
    params: tuple = ()
    if account_key is not None:
        sql += " WHERE account_key = ?"
        params = (account_key,)
    sql += " ORDER BY kind, account_key, scope_key"
    try:
        rows = stats_conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    return [ScopeRow(*tuple(r)) for r in rows]


# ── identical child replacement (§4.2) ────────────────────────────────────
#
# The open-block projection and a block-close fold or convergence replace a
# block's model and project rows wholesale. Deleting and re-inserting an
# identical set would fire the tracked DELETE and INSERT triggers and move
# `seq` with nothing changed, so the writers first ask whether the stored set
# already equals the replacement, compared as multisets over every persisted
# column except `id`. A set that is missing, or differs anywhere, is replaced
# as before (and so still advances `seq`); inequality is the safe answer to
# anything this cannot decide.

CHILD_TABLES = ("five_hour_block_models", "five_hour_block_projects")
_CHILD_SELECTS = types.MappingProxyType({
    (table, scoped): (
        f"SELECT * FROM {table} WHERE "
        + ("account_key = ? AND " if scoped else "")
        + "five_hour_window_key = ?"
    )
    for table in CHILD_TABLES
    for scoped in (True, False)
})
#: No ORDER BY: the column order only has to agree between the stored and the
#: wanted tuples, which both take it from this one read, and sorting the
#: pragma's rows costs a temp B-tree on the live record path.
_CHILD_COLUMNS = "SELECT name, dflt_value FROM pragma_table_info(?)"
_UNDECIDABLE = object()


def _default_value(text):
    """The value a plain-literal column DEFAULT stores, ``None`` for no
    default or ``NULL``, and ``_UNDECIDABLE`` for any expression."""
    if text is None:
        return None
    value = text.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if value.upper() == "NULL":
        return None
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    for parse in (int, float):
        try:
            return parse(value)
        except ValueError:
            pass
    return _UNDECIDABLE


def child_set_unchanged(conn, table: str, *, five_hour_window_key: int,
                        account_key: "str | None",
                        replacement: "Sequence[Mapping]") -> bool:
    """True iff ``table``'s rows for the window (and ``account_key`` when it
    is not ``None``, the writer's own DELETE predicate) equal ``replacement``
    as a multiset over every column except ``id``.

    ``replacement`` holds the column → value mappings the writer would
    INSERT; a column a mapping omits takes the table's literal DEFAULT. A
    mapping naming ``id`` or an unknown column, or an omitted column whose
    default is an expression, is never equal. Values compare as Python does,
    so an INSERT that SQLite's affinity would coerce (``'3'`` into an
    INTEGER column) compares unequal and the set is replaced."""
    if table not in CHILD_TABLES:
        raise ValueError(f"not a five-hour child table: {table}")
    from collections import Counter
    declared = conn.execute(_CHILD_COLUMNS, (table,)).fetchall()
    defaults = {str(name): _default_value(dflt) for name, dflt in declared}
    columns = [name for name in defaults if name != "id"]
    wanted: Counter = Counter()
    for row in replacement:
        if "id" in row or any(name not in defaults for name in row):
            return False
        values = tuple(
            row[name] if name in row else defaults[name] for name in columns)
        if any(value is _UNDECIDABLE for value in values):
            return False
        wanted[values] += 1
    scoped = account_key is not None
    params = ((account_key, int(five_hour_window_key)) if scoped
              else (int(five_hour_window_key),))
    cursor = conn.execute(_CHILD_SELECTS[(table, scoped)], params)
    names = [d[0] for d in cursor.description]
    stored: Counter = Counter(
        tuple(dict(zip(names, tuple(r)))[name] for name in columns)
        for r in cursor.fetchall()
    )
    return stored == wanted
