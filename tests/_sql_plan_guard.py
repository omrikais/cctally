"""Capture every SQL statement with its plan and classify temp structures (#901 G1).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` I1: no
statement on a recurring path may sort, group or materialize an input whose
cardinality grows with retained history through a temp structure. This module is
the mechanism; ``tests/test_901_sql_plan_guard.py`` drives the recurring
lifecycles through it.

Capture hands every ``sqlite3.connect`` a ``PlanConnection`` factory for the
duration of ``capture_sql_plans()``, so managed connections (the store opener)
and raw ones (``_pricing_observed_models``, the doctor's read-only probes) are
covered alike. Each statement is explained on its own connection, with its own
bound parameters, immediately before it runs, so attached databases, temp tables
and the transaction state are exactly what the statement itself sees. Trigger
bodies never reach Python; ``tests/test_901_partition_latest_maintenance.py``
plans their statements directly.

``ALLOWLIST`` names temp structures whose input is bounded independently of
retained history, each with the bound in ``reason``. ``PENDING`` is Part A's work
list: findings a later Part A task removes. It must be empty when Part A is
complete, and Owner B (§5.4) never adds to it.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import pathlib
import re
import sqlite3
import sys
import threading

BIN_DIR = (pathlib.Path(__file__).resolve().parent.parent / "bin").resolve()

#: A plan line carrying any of these builds a transient structure from the
#: statement's input: a sorter/temp b-tree (GROUP BY, ORDER BY, DISTINCT, window
#: partitioning, partial ORDER BY), an automatic index, or a materialized
#: subquery/CTE. I1 forbids all three on a history-sized input ("sorts, groups or
#: materializes").
TEMP_MARKERS = ("USE TEMP B-TREE", "AUTOMATIC", "MATERIALIZE")

_EXPLAINABLE = frozenset(
    {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE", "VALUES"})


@dataclasses.dataclass(frozen=True)
class CapturedStatement:
    phase: str
    sql: str
    plan: tuple
    call_sites: tuple
    #: ``PRAGMA temp_store`` on the statement's connection when it ran. Plans
    #: are judged independently of it (spec I1, Q11); it is recorded so a test
    #: can prove it judged both the file and the in-memory temp store.
    temp_store: "int | None" = None


@dataclasses.dataclass(frozen=True)
class TempStructureRule:
    rule_id: str
    call_site: str
    sql_pattern: str
    detail_pattern: str
    reason: str
    sql_exclude: "str | None" = None

    def matches(self, statement: CapturedStatement, detail: str) -> bool:
        if self.call_site not in statement.call_sites:
            return False
        if re.search(self.sql_pattern, statement.sql) is None:
            return False
        if (self.sql_exclude is not None
                and re.search(self.sql_exclude, statement.sql) is not None):
            return False
        return re.search(self.detail_pattern, detail) is not None


@dataclasses.dataclass(frozen=True)
class Finding:
    statement: CapturedStatement
    detail: str


def normalize_sql(sql) -> str:
    return " ".join(str(sql).split())


def _first_keyword(sql: str) -> str:
    stripped = sql.lstrip(" \t\r\n(")
    return stripped.split(None, 1)[0].upper() if stripped else ""


@functools.lru_cache(maxsize=None)
def _resolved_parent(filename: str) -> "pathlib.Path | None":
    try:
        return pathlib.Path(filename).resolve().parent
    except OSError:
        return None


def _call_sites(frame, roots) -> tuple:
    sites = []
    while frame is not None:
        code = frame.f_code
        if _resolved_parent(code.co_filename) in roots:
            sites.append(
                f"{pathlib.Path(code.co_filename).stem}.{code.co_qualname}")
        frame = frame.f_back
    return tuple(sites)


class _Recorder:
    def __init__(self, roots) -> None:
        self.roots = frozenset(pathlib.Path(root).resolve() for root in roots)
        self.phase = "setup"
        self.statements: "list[CapturedStatement]" = []
        self._lock = threading.Lock()

    def record(self, conn, sql, params, frame) -> None:
        text = normalize_sql(sql)
        if _first_keyword(text) not in _EXPLAINABLE:
            return
        sites = _call_sites(frame, self.roots)
        if not sites:
            return
        try:
            cursor = sqlite3.Connection.cursor(conn)
            cursor.row_factory = None
            rows = sqlite3.Cursor.execute(
                cursor, "EXPLAIN QUERY PLAN " + str(sql), params).fetchall()
            plan = tuple(str(row[3]) for row in rows)
        except sqlite3.Error as exc:
            plan = (f"<explain failed: {exc}>",)
        try:
            temp_store = int(sqlite3.Cursor.execute(
                sqlite3.Connection.cursor(conn),
                "PRAGMA temp_store").fetchone()[0])
        except sqlite3.Error:
            temp_store = None
        with self._lock:
            self.statements.append(
                CapturedStatement(self.phase, text, plan, sites, temp_store))


_RECORDER: "_Recorder | None" = None


class PlanCursor(sqlite3.Cursor):
    def execute(self, sql, *args):
        recorder = _RECORDER
        if recorder is not None:
            recorder.record(
                self.connection, sql, args[0] if args else (),
                sys._getframe(1))
        return super().execute(sql, *args)

    def executemany(self, sql, seq_of_parameters):
        params = list(seq_of_parameters)
        recorder = _RECORDER
        if recorder is not None and params:
            recorder.record(self.connection, sql, params[0], sys._getframe(1))
        return super().executemany(sql, params)


class PlanConnection(sqlite3.Connection):
    def cursor(self, factory=PlanCursor):
        return super().cursor(factory)

    def execute(self, sql, *args):
        recorder = _RECORDER
        if recorder is not None:
            recorder.record(self, sql, args[0] if args else (), sys._getframe(1))
        return super().execute(sql, *args)

    def executemany(self, sql, seq_of_parameters):
        params = list(seq_of_parameters)
        recorder = _RECORDER
        if recorder is not None and params:
            recorder.record(self, sql, params[0], sys._getframe(1))
        return super().executemany(sql, params)


@contextlib.contextmanager
def capture_sql_plans(*, roots=(BIN_DIR,), temp_store=None):
    """Record every explainable statement whose stack reaches ``roots``.

    ``temp_store`` ("FILE" or "MEMORY"), when given, is selected on every
    connection opened during the capture, read-only ones included, so a test
    can judge the same statements under both temp stores (spec I1, Q11). A
    writer opener re-applies ``_cctally_store.WRITER_TEMP_STORE`` at open, so a
    caller also pins that to the same value.
    """
    global _RECORDER
    if _RECORDER is not None:
        raise AssertionError("capture_sql_plans() does not nest")
    real_connect = sqlite3.connect
    recorder = _Recorder(roots)

    def connect(*args, **kwargs):
        if "factory" in kwargs or len(args) > 5:
            raise AssertionError(
                "the #901 plan guard cannot see a connection opened with a "
                "custom factory; open it with plain sqlite3.connect")
        kwargs["factory"] = PlanConnection
        conn = real_connect(*args, **kwargs)
        if temp_store is not None:
            sqlite3.Connection.execute(conn, f"PRAGMA temp_store = {temp_store}")
        return conn

    sqlite3.connect = connect
    _RECORDER = recorder
    try:
        yield recorder
    finally:
        _RECORDER = None
        sqlite3.connect = real_connect


def temp_findings(statements):
    for statement in statements:
        for detail in statement.plan:
            if any(marker in detail for marker in TEMP_MARKERS):
                yield Finding(statement, detail)


def classify(statements, *, allowlist, pending):
    """Return ``(unexplained findings, stale pending rule ids)``."""
    hits = {rule.rule_id: 0 for rule in pending}
    unexplained = []
    for finding in temp_findings(statements):
        if any(rule.matches(finding.statement, finding.detail)
               for rule in allowlist):
            continue
        matched = [rule for rule in pending
                   if rule.matches(finding.statement, finding.detail)]
        if matched:
            for rule in matched:
                hits[rule.rule_id] += 1
            continue
        unexplained.append(finding)
    stale = sorted(rule_id for rule_id, count in hits.items() if count == 0)
    return unexplained, stale


def format_findings(findings) -> str:
    seen = set()
    lines = []
    for finding in findings:
        key = (finding.statement.sql, finding.detail)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"[{finding.statement.phase}] {finding.detail}\n"
            f"    at {' <- '.join(finding.statement.call_sites[:5])}\n"
            f"    sql: {finding.statement.sql[:700]}")
    return "\n".join(lines)


#: #901 Q11 / Amendment 19 T1 audited every rule below for an INDEPENDENT
#: cardinality bound (spec I1): a true top-N (``LIMIT``), or a bound the schema
#: enforces on what the structure can hold. Grouping scope is never a bound: one
#: session's or conversation's whole history, one rollout file, one attribution
#: axis shard, one subscription week, one trailing week or one batch can each
#: grow without limit, and ``temp_store = MEMORY`` discharges nothing. The
#: rules the audit could not bound were rewritten so their statements build no
#: temp structure at all (each module's docstring names its rewrite), and were
#: removed from this list:
#:
#: * ``claude-session-batch-{_session_cost_map,_session_models_map}`` and
#:   ``quota-attribution-anchor-distinct`` and ``codex-turn-repair-one-source``:
#:   the DISTINCT moved into Python over a streamed read (a result-sized set);
#: * ``claude-session-batch-_session_first_prompt_titles_map``: one
#:   index-served ``LIMIT 12`` lookup per session;
#: * ``codex-{normalized,index}-rows-one-conversation`` and
#:   ``codex-find-projection-one-conversation``: read in ``(timestamp_utc, id)``
#:   index order, each timestamp tie run put in physical order in Python;
#: * ``current-week-sample-rows``, ``codex-cycle-five-hour-rows``,
#:   ``doctor-journal-violations-per-batch`` and ``qualified-codex-dirty-paths``:
#:   the rows the reader already returns whole are ordered in Python (a stable
#:   sort over the same scan, so ties keep the engine's order);
#: * ``doctor-accounts-trailing-week`` and ``retention-null-identity-paths``:
#:   the GROUP BY moved into Python over a streamed read (one entry per group).
ALLOWLIST: "tuple[TempStructureRule, ...]" = (
    TempStructureRule(
        rule_id="quota-recent-rows-limit",
        call_site="_cctally_quota.load_codex_quota_observations",
        sql_pattern=r"FROM quota_window_snapshots\b.* ORDER BY .* LIMIT \?$",
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason=("top-N: LIMIT max_rows, which every caller passes as the "
                "constant DASHBOARD_QUOTA_OBSERVATION_LIMIT; the sorter keeps "
                "at most that many rows"),
    ),
    TempStructureRule(
        rule_id="quota-first-block-physical-tuple",
        call_site="_cctally_quota._first_block_physical_tuple",
        sql_pattern=(r"FROM quota_window_snapshots WHERE source='codex' AND "
                     r"source_root_key=\? AND logical_limit_key=\? AND "
                     r"observed_slot=\? AND window_minutes=\?"),
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason="top-1: LIMIT 1, so the sorter keeps one row",
    ),
    TempStructureRule(
        rule_id="reset-anchor-line-tiebreak",
        call_site="_cctally_cache.CodexResetAnchorResolver._components_for",
        sql_pattern=r"ORDER BY source_path, line_offset, id$",
        detail_pattern=r"USE TEMP B-TREE FOR LAST TERM OF ORDER BY",
        reason=("schema bound: the partial sorter holds one run of rows tied "
                "on (source_path, line_offset), and UNIQUE(source, "
                "source_path, line_offset, logical_limit_key) with "
                "logical_limit_key IN (the snap-equivalent spellings) admits "
                "at most one row per spelling, a fixed handful"),
    ),
    TempStructureRule(
        rule_id="current-week-date-fallback-pick",
        call_site="_cctally_forecast._fetch_current_week_snapshots",
        sql_pattern=(r"GROUP BY week_start_date, week_end_date ORDER BY "
                     r"MAX\(captured_at_utc\) DESC LIMIT 1$"),
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason=("top-1: the GROUP BY streams from idx_usage_week_date_group "
                "and only the ORDER BY over its groups sorts, under LIMIT 1, "
                "so the sorter keeps one group"),
    ),
    TempStructureRule(
        rule_id="week-latest-row-limit-1",
        call_site="_cctally_core._get_latest_row_for_week",
        sql_pattern=(r"FROM weekly_usage_snapshots WHERE week_start_date = \?"
                     r" .* LIMIT 1$"),
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason="top-1: LIMIT 1, so the sorter keeps one row",
    ),
    TempStructureRule(
        rule_id="weekly-periods-latest-limit-1",
        call_site="_cctally_dashboard._dashboard_build_weekly_periods",
        sql_pattern=(r"^SELECT week_start_date FROM weekly_usage_snapshots "
                     r"ORDER BY captured_at_utc DESC, id DESC LIMIT 1$"),
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason="top-1: LIMIT 1, so the sorter keeps one row",
    ),
    TempStructureRule(
        rule_id="claude-week-key-latest-limit-1",
        call_site="_cctally_milestone_history._current_claude_week_key",
        sql_pattern=(r"^SELECT week_start_date FROM weekly_usage_snapshots "
                     r"ORDER BY captured_at_utc DESC, id DESC LIMIT 1$"),
        detail_pattern=r"USE TEMP B-TREE FOR ORDER BY",
        reason="top-1: LIMIT 1, so the sorter keeps one row",
    ),
    TempStructureRule(
        rule_id="codex-cycle-milestone-count",
        call_site="_cctally_milestone_history._codex_milestone_count",
        sql_pattern=(r"^SELECT COUNT\(DISTINCT percent_threshold\), "
                     r"MAX\(captured_at_utc\) FROM quota_percent_milestones "
                     r"WHERE source='codex' AND source_root_key=\?"),
        detail_pattern=r"USE TEMP B-TREE FOR count\(DISTINCT\)",
        reason=("schema bound: a count(DISTINCT) b-tree holds distinct keys "
                "only, and percent_threshold is an INTEGER the writer derives "
                "from an integer crossing under CHECK(percent_threshold "
                "BETWEEN 1 AND 100): at most 100 entries"),
    ),
    *(
        TempStructureRule(
            rule_id=f"panel-row-cap-{call_site.rsplit('.', 1)[1].strip('_')}",
            call_site=call_site,
            sql_pattern=r" LIMIT \?$",
            # A full sorter, or (once an epoch-1017 digest index supplies a
            # prefix of the order, as idx_budget_milestones_codex_digest does
            # for `_budget_wire`) a partial "LAST n TERMS" sorter. SQLite's
            # pushOntoSorter applies the same LIMIT cap to both: it never
            # holds more than LIMIT+OFFSET rows.
            detail_pattern=(r"USE TEMP B-TREE FOR "
                            r"(LAST (TERM|\d+ TERMS) OF )?ORDER BY"),
            reason=("top-N: LIMIT, the panel's constant row cap; the sorter "
                    "retains at most that many rows"),
        )
        for call_site in (
            "_cctally_dashboard_envelope._build_meter_rate_change_array",
            "_cctally_dashboard_envelope._envelope_rows_budget_family",
            "_cctally_dashboard_envelope._envelope_rows_five_hour",
            "_cctally_dashboard_envelope._envelope_rows_project_budget",
            "_cctally_dashboard_envelope._envelope_rows_projected",
            "_cctally_dashboard_envelope._envelope_rows_weekly",
            "_cctally_dashboard_sources._alerts_wire",
            "_cctally_dashboard_sources._budget_wire",
            "_cctally_dashboard_sources._codex_weekly_periods",
            "_cctally_dashboard_sources._projected_budget_wire",
            "_cctally_dashboard_sources._quota_wire",
        )
    ),
)

#: The rule ids Amendment 19 T1 removed. ``tests/test_901_allowlist_audit.py``
#: asserts none of them is back.
REMOVED_BY_AUDIT = frozenset({
    "claude-session-batch-_session_cost_map",
    "claude-session-batch-_session_models_map",
    "claude-session-batch-_session_first_prompt_titles_map",
    "codex-turn-repair-one-source",
    "quota-attribution-anchor-distinct",
    "current-week-sample-rows",
    "retention-null-identity-paths",
    "codex-normalized-rows-one-conversation",
    "codex-index-rows-one-conversation",
    "codex-find-projection-one-conversation",
    "doctor-accounts-trailing-week",
    "doctor-journal-violations-per-batch",
    "codex-cycle-five-hour-rows",
    "qualified-codex-dirty-paths",
    "projects-week-window",
})

PENDING: "tuple[TempStructureRule, ...]" = ()
