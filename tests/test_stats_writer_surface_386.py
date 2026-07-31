"""Lexical freeze over the stats.db SQL write surface — issue #386, spec §6.3.

**This is explicitly NOT the AC1 proof.** §6.2's authorizer-backed runtime tests
(`tests/test_stats_writer_guard_386.py`) are, because only they can establish
caller lock state and only they can see dynamic SQL. This module is the
*supplementary* freeze: it detects a NEW SQL write site against a stats table
appearing in `bin/`, so an addition has to be a deliberate act that updates this
allowlist rather than a silent one.

Two things it deliberately cannot do, stated so nobody mistakes its green for
more than it is:

1. **It cannot resolve dynamic SQL.** 18 sites build the target from a variable
   (`UPDATE {table}` in the cutover stamp and eight journal folds, `INSERT INTO
   {table}` in `_insert_or_ignore`, migration 011's table rebuild). They are
   counted per file so a change in their number still trips the freeze, but the
   TABLE is invisible to any lexical scan.
2. **It cannot see physical mutation at all** — `os.replace`, `os.rename`,
   `unlink`, `VACUUM`, `PRAGMA user_version`. 12 of the 14 physical mutation
   sites are in that class; they are covered by the opener protocol and the lock
   corrections, not by any hook or scan.

**It MUST scan the extensionless `bin/cctally`.** A `bin/*.py` glob misses it,
and this repo has a recorded incident where exactly that hid a real undercount
into merge review. `test_the_scan_covers_the_extensionless_entry_point` pins it
directly, and `test_scan_would_catch_a_write_added_to_the_extensionless_entry`
proves the scanner reacts to a site placed there.
"""

from __future__ import annotations

import collections
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"


#: The stats.db table set: the tables `open_db` /
#: `_apply_quota_projection_schema` create, plus the three framework/marker
#: tables. Frozen deliberately — `test_every_frozen_table_is_really_created`
#: fails if one is renamed or dropped, so this list cannot silently rot.
STATS_TABLES = frozenset({
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "five_hour_blocks", "five_hour_block_models",
    "five_hour_block_projects", "five_hour_milestones", "percent_milestones",
    "budget_milestones", "projected_milestones", "project_budget_milestones",
    "weekly_credit_floors", "accounts", "journal_cursor",
    "journal_effective_events", "journal_protocol_violations",
    "stats_open_fixups", "schema_migrations",
    "schema_migrations_skipped", "quota_window_blocks",
    "quota_percent_milestones", "quota_threshold_events", "quota_alert_arming",
    "quota_projection_state", "quota_projection_ledger_state",
})

#: The SQL verb pattern. It matches the VERB alone and resolves the target from
#: a following window, because the plan's original `UPDATE [a-z_]+ +SET` form
#: requires the table and `SET` on one physical line and therefore MISSES three
#: of the four `UPDATE five_hour_blocks` statements in `bin/_cctally_record.py`.
_VERB = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK)\s+)?INTO"
    r"|REPLACE\s+INTO"
    r"|UPDATE(?:\s+OR\s+\w+)?"
    r"|DELETE\s+FROM"
    r"|CREATE\s+(?:UNIQUE\s+)?(?:TEMP\s+|TEMPORARY\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)"
    r"(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|DROP\s+(?:TABLE|INDEX|VIEW|TRIGGER)(?:\s+IF\s+EXISTS)?"
    r"|ALTER\s+TABLE)\b",
    re.IGNORECASE,
)
_NAME = re.compile(
    r"[\s\"']*([A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_.\[\]']*\})"
)

#: The frozen surface: {source file: {stats table: number of write sites}}.
#: A new write site changes a count; a write against a stats table from a NEW
#: module adds a key. Counts, not line numbers, so ordinary refactoring does not
#: churn this file.
FROZEN_WRITE_SITES = {
    "_cctally_core.py": {
        "accounts": 1,
        "budget_milestones": 1,
        "five_hour_block_models": 1,
        "five_hour_block_projects": 1,
        "five_hour_blocks": 1,
        "five_hour_milestones": 1,
        "five_hour_reset_events": 1,
        "journal_cursor": 2,
        "journal_effective_events": 1,
        "journal_protocol_violations": 1,
        "percent_milestones": 1,
        "project_budget_milestones": 1,
        "projected_milestones": 1,
        "quota_alert_arming": 1,
        "quota_percent_milestones": 1,
        # public #5: the `CREATE TABLE IF NOT EXISTS` for the incremental
        # projector's own state, inside `_apply_quota_projection_schema` — which
        # runs under `stats_open_time_guard` (maintenance EX +
        # `stats_write_scope("open-time")`), the sanctioned first-open scope.
        "quota_projection_ledger_state": 1,
        "quota_projection_state": 3,
        "quota_threshold_events": 1,
        "quota_window_blocks": 1,
        "week_reset_events": 1,
        "weekly_cost_snapshots": 1,
        "weekly_credit_floors": 1,
        "weekly_usage_snapshots": 2,
    },
    "_cctally_db.py": {
        "budget_milestones": 5,
        "five_hour_block_models": 4,
        "five_hour_block_projects": 4,
        "five_hour_blocks": 4,
        "five_hour_milestones": 7,
        "percent_milestones": 7,
        "projected_milestones": 1,
        "schema_migrations": 8,
        "schema_migrations_skipped": 4,
        "week_reset_events": 1,
        "weekly_cost_snapshots": 2,
        "weekly_usage_snapshots": 2,
    },
    "_cctally_five_hour.py": {
        "five_hour_block_models": 1,
        "five_hour_block_projects": 1,
        "five_hour_blocks": 1,
        "schema_migrations": 1,
    },
    "_cctally_journal.py": {
        "accounts": 8,
        "five_hour_blocks": 1,
        "journal_cursor": 1,
        "journal_effective_events": 4,
        "journal_protocol_violations": 2,
        "quota_alert_arming": 2,
        "quota_threshold_events": 1,
        "weekly_credit_floors": 1,
    },
    "_cctally_milestones.py": {
        "budget_milestones": 3,
        "percent_milestones": 1,
        "project_budget_milestones": 2,
        "projected_milestones": 1,
        "weekly_cost_snapshots": 1,
    },
    # public #5 raised four of these by one: the SCOPED sweep is a second
    # statement per child table alongside the whole-root one it did not replace,
    # and `quota_projection_state` gained the DELETE that retires an account
    # partition the projection no longer names.
    "_cctally_quota.py": {
        "quota_alert_arming": 2,
        "quota_percent_milestones": 3,
        "quota_projection_ledger_state": 1,
        "quota_projection_state": 2,
        "quota_threshold_events": 4,
        "quota_window_blocks": 3,
    },
    "_cctally_record.py": {
        "five_hour_block_models": 2,
        "five_hour_block_projects": 2,
        "five_hour_blocks": 5,
        "five_hour_milestones": 2,
        "five_hour_reset_events": 1,
        "percent_milestones": 1,
        "project_budget_milestones": 1,
        "projected_milestones": 1,
        "week_reset_events": 2,
        "weekly_credit_floors": 1,
        "weekly_usage_snapshots": 6,
    },
    "_cctally_store.py": {
        "stats_open_fixups": 2,
    },
    "_cctally_weekrefs.py": {
        "week_reset_events": 3,
    },
    # Fixture tooling. Spec §3.2 excludes it from the CALL-PATH enumeration on
    # target-path provenance (it only ever builds scratch DBs, never DB_PATH),
    # but it is kept in the LEXICAL freeze because keeping it costs nothing and
    # a new write here is still worth seeing.
    "_fixture_builders.py": {
        "accounts": 3,
        "budget_milestones": 1,
        "five_hour_block_models": 1,
        "five_hour_block_projects": 1,
        "five_hour_blocks": 1,
        "five_hour_milestones": 1,
        "five_hour_reset_events": 1,
        "percent_milestones": 1,
        "project_budget_milestones": 1,
        "projected_milestones": 1,
        "schema_migrations": 4,
        "schema_migrations_skipped": 2,
        "week_reset_events": 2,
        "weekly_cost_snapshots": 3,
        "weekly_credit_floors": 1,
        "weekly_usage_snapshots": 3,
    },
}

#: Dynamic-SQL sites per file — the target is a variable, so only the COUNT can
#: be frozen. `bin/cctally` carries none.
FROZEN_DYNAMIC_SITES = {
    "_cctally_db.py": 9,
    "_cctally_journal.py": 8,
    "_cctally_store.py": 1,
}


def _runtime_sources():
    """Every runtime source the freeze covers.

    `bin/cctally` is yielded FIRST and explicitly: it is extensionless, so a
    `bin/*.py` glob does not match it. `bin/build-*` fixture builders are
    excluded per spec §3.2.
    """
    yield BIN / "cctally"          # extensionless — a bin/*.py glob MISSES this
    for p in sorted(BIN.glob("*.py")):
        if not p.name.startswith("build-"):
            yield p


def _scan_text(text: str):
    """(Counter of stats-table write sites, count of dynamic-target sites)."""
    counts: collections.Counter = collections.Counter()
    dynamic = 0
    for m in _VERB.finditer(text):
        tail = text[m.end():m.end() + 240]
        # Collapse a Python implicit string concatenation across lines so
        # `"UPDATE five_hour_blocks"\n"   SET …"` still resolves its target.
        tail = re.sub(r"\s*\+?\s*\n\s*[\"']", " ", tail)
        nm = _NAME.match(tail)
        if nm is None:
            continue
        target = nm.group(1)
        if target.startswith("{"):
            dynamic += 1
        elif target in STATS_TABLES:
            counts[target] += 1
    return counts, dynamic


def _observed():
    sites: dict = {}
    dynamic: dict = {}
    for path in _runtime_sources():
        counts, dyn = _scan_text(
            path.read_text(encoding="utf-8", errors="replace"))
        if counts:
            sites[path.name] = dict(sorted(counts.items()))
        if dyn:
            dynamic[path.name] = dyn
    return sites, dynamic


def test_stats_write_surface_is_frozen():
    observed, _dynamic = _observed()
    if observed == FROZEN_WRITE_SITES:
        return

    lines = []
    for name in sorted(set(observed) | set(FROZEN_WRITE_SITES)):
        want = FROZEN_WRITE_SITES.get(name, {})
        got = observed.get(name, {})
        for table in sorted(set(want) | set(got)):
            if want.get(table, 0) != got.get(table, 0):
                lines.append(
                    f"  bin/{name}: {table}: frozen={want.get(table, 0)} "
                    f"found={got.get(table, 0)}"
                )
    raise AssertionError(
        "the stats.db SQL write surface changed (#386 spec §6.3):\n"
        + "\n".join(lines)
        + "\n\nRequired action: a NEW write site must run inside a sanctioned "
          "scope — `_cctally_store.stats_write_scope(...)` while holding "
          "`journal.ingest.lock` (steady state) or `stats.db.maintenance.lock` "
          "(first-open / legacy / epoch / administrative), per spec §3.1. "
          "Confirm that, add the coverage-table row in docs/journal-gotchas.md, "
          "then update FROZEN_WRITE_SITES here. Do NOT update this allowlist to "
          "make the test pass without doing the first two."
    )


def test_dynamic_sql_site_count_is_frozen():
    """The freeze cannot resolve a `{table}` target, but it can still notice a
    NEW one appearing — which is the case most likely to escape review."""
    _sites, dynamic = _observed()
    assert dynamic == FROZEN_DYNAMIC_SITES, (
        f"dynamic-SQL write sites changed: frozen={FROZEN_DYNAMIC_SITES} "
        f"found={dynamic}. A dynamic target is invisible to this scan AND to "
        "any lexical review — it must be covered by §6.2's authorizer test."
    )


def test_the_scan_covers_the_extensionless_entry_point():
    """`bin/cctally` has no extension; a `bin/*.py` glob silently misses it."""
    sources = list(_runtime_sources())
    entry = BIN / "cctally"
    assert entry.exists()
    assert entry in sources, "the freeze does not scan bin/cctally"
    assert entry not in set(BIN.glob("*.py")), (
        "bin/cctally gained a .py extension — this assertion exists to prove "
        "the glob genuinely misses it, so the explicit yield is load-bearing"
    )


def test_scan_would_catch_a_write_added_to_the_extensionless_entry():
    """Non-vacuity for the claim above: the scanner reacts to a site placed in
    `bin/cctally`, which currently contains ZERO stats write sites."""
    entry_text = (BIN / "cctally").read_text(encoding="utf-8", errors="replace")
    counts, _dyn = _scan_text(entry_text)
    assert counts == {}, (
        f"bin/cctally gained stats write sites: {dict(counts)} — update "
        "FROZEN_WRITE_SITES and this test's premise together"
    )
    injected = entry_text + (
        '\n_ = "INSERT INTO weekly_usage_snapshots (weekly_percent) VALUES (1)"\n'
    )
    counts, _dyn = _scan_text(injected)
    assert counts == {"weekly_usage_snapshots": 1}, (
        "the scanner did not see a write site injected into the extensionless "
        f"entry point: {dict(counts)}"
    )


def test_scan_detects_a_multiline_update():
    """The plan's `UPDATE [a-z_]+ +SET` form misses this shape, and three of the
    four `UPDATE five_hour_blocks` statements are written exactly this way."""
    counts, _dyn = _scan_text(
        'conn.execute(\n'
        '    "UPDATE five_hour_blocks\\n"\n'
        '    "   SET is_closed = 1"\n'
        ')\n'
    )
    assert counts == {"five_hour_blocks": 1}, dict(counts)


def test_temp_views_over_attached_dbs_are_not_counted():
    """The dashboard and TUI build `CREATE TEMP VIEW`s over an ATTACHed
    cache.db. They are not stats `main` writes and must not enter the freeze —
    the same false-positive class §6.1 scopes the authorizer to `main` to
    avoid."""
    counts, _dyn = _scan_text(
        'conn.execute("CREATE TEMP VIEW session_entries AS SELECT * FROM c.x")'
    )
    assert counts == {}, dict(counts)


def test_every_frozen_table_is_really_created():
    """`STATS_TABLES` cannot silently rot: every name must still appear in a
    `CREATE TABLE` in `bin/`, so a renamed or dropped table fails here rather
    than quietly shrinking the freeze's coverage."""
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in _runtime_sources()
    )
    missing = sorted(
        t for t in STATS_TABLES
        if not re.search(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?[\s\"']*" + t + r"\b",
            blob, re.IGNORECASE,
        )
    )
    assert missing == [], (
        f"frozen stats tables no longer created anywhere in bin/: {missing}"
    )
