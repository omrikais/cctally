"""#901 Amendment 1 items 2-9 (binding Codex answer ``c901-design-2``, G1/G2).

Task A1's plan guard found eight recurring stats.db reads that sorted, grouped
or de-duplicated a history-sized input through a temp b-tree. Stats epoch 1017
gives each one an index in its own order; items 8 and 9 also rewrite their
statements. Every test here asserts two things per item: the statement builds
no temp structure (G1, on the real code path), and it returns exactly what it
returned before (G2).

"Before" means two different things. Items 3-7 change no SQL text, so the
"before" leg is the same call on a backup of the store with every #901 read
index dropped (the pre-#901 schema). Items 2, 8 and 9 change their statements:
item 2 gains an explicit tie order and an ``INDEXED BY`` pin (Amendment 1b),
items 8 and 9 are rewritten. Their "before" leg is the statement frozen from
``56e66f07a`` (``LEGACY_*``), patched in for the final-output comparison; for
item 2 it also runs on the pre-#901 schema, because that schema's plan decided
the legacy order of rows tied on one reset second.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import types

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
R1, R2, R3, R4, R5 = ("1" * 32, "2" * 32, "3" * 32, "4" * 32, "5" * 32)
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
UNATTRIBUTED = "unattributed"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_db"]().close()
    return ns


#: Every #901 stats read index (epoch 1017): the "before" leg drops them all.
READ_INDEXES = (
    "idx_quota_projection_state_digest", "idx_quota_blocks_digest",
    "idx_quota_milestones_digest", "idx_quota_threshold_events_digest",
    "idx_budget_milestones_codex_digest",
    "idx_projected_milestones_codex_digest",
    "idx_percent_milestones_alert_digest",
    "idx_five_hour_milestones_alert_digest",
    "idx_budget_milestones_alert_digest",
    "idx_projected_milestones_alert_digest",
    "idx_project_budget_milestones_alert_digest",
    "idx_usage_week_boundary_group", "idx_usage_week_date_group",
    "idx_quota_blocks_weekly_reset_order",
    "idx_quota_blocks_weekly_single_root_order",
    "idx_percent_milestones_week_date",
    "idx_quota_milestones_root", "idx_quota_threshold_events_root",
    "idx_quota_blocks_root_group_pairs", "idx_week_reset_events_effective_order",
    "idx_usage_subscription_anchor_order", "idx_usage_subscription_anchor_pick",
)


def _read_index_names() -> "tuple[str, ...]":
    return READ_INDEXES


def _open(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_and_split(tmp_path, seed):
    """Seed the live store, then back it up without any #901 read index."""
    import _cctally_core

    live = _cctally_core.DB_PATH
    conn = sqlite3.connect(live)
    try:
        seed(conn)
        conn.commit()
    finally:
        conn.close()
    before = tmp_path / "before-901.db"
    source = sqlite3.connect(live)
    target = sqlite3.connect(before)
    try:
        source.backup(target)
        for name in _read_index_names():
            target.execute(f"DROP INDEX IF EXISTS {name}")
        target.commit()
        assert not ({row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
            & set(_read_index_names()))
    finally:
        source.close()
        target.close()
    return live, before


def _plans(call, path, site):
    """Every statement ``call(conn)`` issues from ``site``, with its plan."""
    with guard.capture_sql_plans() as recorder:
        conn = _open(path)
        try:
            call(conn)
        finally:
            conn.close()
    return [statement for statement in recorder.statements
            if site in statement.call_sites]


def _temp_free(statements) -> list:
    return [(statement.sql, statement.plan) for statement in statements
            if any(marker in detail for detail in statement.plan
                   for marker in guard.TEMP_MARKERS)]


def _uses(statements, index) -> bool:
    return any(index in detail for statement in statements
               for detail in statement.plan)


def _plus(text: str, **delta) -> str:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (value + dt.timedelta(**delta)).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ── Item 2: Codex weekly cycles (`_load_codex_cycles`) ───────────────────────
#
# Amendment 1b (binding follow-up to ``c901-design-2``): the oracle is the
# FROZEN legacy statement run on the pre-#901 schema, not the amended function
# on a store without the index. The legacy statement ordered by the reset
# second alone, and its plan decided the order of rows tied on that second.
# ``_canonicalize_codex_cluster`` keeps the first maximal member, so that tie
# order is observable in the selected cycles. The rows are compared IN ORDER.
#
# Amendment 1c (``c901-design-3``): the legacy plan is not one plan. A
# multi-root read scans ``idx_quota_blocks_active`` (ties: root, account,
# orphaned_at, limit, slot, reset spelling); a single-root read seeks the
# UNIQUE autoindex (the same without orphaned_at). Production therefore picks
# its index and explicit order by the NORMALIZED root count, and G2 preserves
# each legacy result separately, including the case where they disagree.

LEGACY_CYCLES_SQL = (
    "SELECT source_root_key, logical_limit_key, observed_slot, "
    "       window_minutes, limit_id, limit_name, account_key, "
    "       resets_at_utc, nominal_start_at_utc, current_percent "
    "FROM quota_window_blocks "
    "WHERE source='codex' AND window_minutes=10080 "
    "{orphan}{account}AND source_root_key IN ({roots}) "
    "ORDER BY unixepoch(resets_at_utc) DESC"
)
#: The production read's SELECT list, frozen. The cycle-detail route issues
#: the same list for its 5-hour blocks, so ``_CycleReads`` also requires the
#: weekly predicate and the reset ordering before it intercepts a statement.
CYCLES_SELECT = (
    "SELECT source_root_key, logical_limit_key, observed_slot, "
    "window_minutes, limit_id, limit_name, account_key, resets_at_utc, "
    "nominal_start_at_utc, current_percent FROM quota_window_blocks "
)
WEEKLY_RESET_ORDER_DDL = (
    "CREATE INDEX idx_quota_blocks_weekly_reset_order "
    "ON quota_window_blocks(unixepoch(resets_at_utc) DESC, "
    "source_root_key ASC, account_key ASC, orphaned_at ASC, "
    "logical_limit_key ASC, observed_slot ASC, resets_at_utc ASC) "
    "WHERE source='codex' AND window_minutes=10080"
)
WEEKLY_SINGLE_ROOT_ORDER_DDL = (
    "CREATE INDEX idx_quota_blocks_weekly_single_root_order "
    "ON quota_window_blocks(unixepoch(resets_at_utc) DESC, "
    "source_root_key ASC, account_key ASC, "
    "logical_limit_key ASC, observed_slot ASC, resets_at_utc ASC) "
    "WHERE source='codex' AND window_minutes=10080"
)
MULTI_ROOT_INDEX = "idx_quota_blocks_weekly_reset_order"
SINGLE_ROOT_INDEX = "idx_quota_blocks_weekly_single_root_order"
MULTI_ROOT_ORDER = (
    "ORDER BY unixepoch(resets_at_utc) DESC, source_root_key ASC, "
    "account_key ASC, orphaned_at ASC, logical_limit_key ASC, "
    "observed_slot ASC, resets_at_utc ASC")
SINGLE_ROOT_ORDER = (
    "ORDER BY unixepoch(resets_at_utc) DESC, source_root_key ASC, "
    "account_key ASC, logical_limit_key ASC, observed_slot ASC, "
    "resets_at_utc ASC")


def _normalized_roots(roots) -> tuple:
    """The production normalization: strings only, non-empty, de-duplicated."""
    return tuple(sorted({r for r in roots if isinstance(r, str) and r}))


def _designated(roots) -> "tuple[str, str]":
    """(index, ORDER BY) Amendment 1c designates for this root input."""
    if len(_normalized_roots(roots)) == 1:
        return SINGLE_ROOT_INDEX, SINGLE_ROOT_ORDER
    return MULTI_ROOT_INDEX, MULTI_ROOT_ORDER


def _block(conn, *, root, resets, percent, account=UNATTRIBUTED,
           limit="codex", slot="secondary", window=10080, limit_name=None,
           orphaned=None, source="codex", group=None, digest=None):
    start = _plus(resets, minutes=-window)
    conn.execute(
        "INSERT INTO quota_window_blocks (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, limit_id,"
        " limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent,"
        " current_percent, last_source_path, last_line_offset, generation,"
        " orphaned_at, account_key, physical_group_key, physical_group_digest)"
        " VALUES (?,?,?,?,?,'codex',?,?,?,?,?,1.0,?,'/p.jsonl',1,'g',?,?,?,?)",
        (source, root, limit, slot, window, limit_name, resets, start, start,
         start, percent, orphaned, account, group, digest))


CYCLE_BLOCKS = (
    dict(root=R1, resets="2026-07-14T12:00:00Z", percent=90.0,
         account=ACCOUNT_A),
    dict(root=R1, resets="2026-07-21T12:00:00Z", percent=42.0,
         account=ACCOUNT_A),
    # The same instant on the same identity: another account, the same
    # account spelled with an offset, and a fractional spelling inside the
    # same second. A same-second tie in every sense.
    dict(root=R1, resets="2026-07-21T12:00:00Z", percent=40.0,
         account=ACCOUNT_B),
    dict(root=R1, resets="2026-07-21T14:00:00+02:00", percent=43.0,
         account=ACCOUNT_A),
    dict(root=R1, resets="2026-07-21T12:00:00.250Z", percent=39.0,
         account=ACCOUNT_B),
    dict(root=R1, resets="2026-07-21T12:00:00Z", percent=38.0,
         account=UNATTRIBUTED),
    # A later jitter sibling, 40 seconds on, inside the 600-second floor.
    dict(root=R1, resets="2026-07-21T12:00:40Z", percent=44.0,
         account=ACCOUNT_A),
    # Same second as the 07-14 reset, sub-second spelling, other slot.
    dict(root=R1, slot="primary", resets="2026-07-14T12:00:00.400Z",
         percent=91.0, account=ACCOUNT_A),
    dict(root=R1, resets="2026-07-07T11:59:30Z", percent=70.0),
    dict(root=R2, resets="2026-07-20T09:00:00Z", percent=30.0),
    # A model pool: filtered after the read.
    dict(root=R2, limit="codex_bengalfox", resets="2026-07-20T09:00:00Z",
         percent=99.0, limit_name="GPT-5.3-Codex-Spark"),
    dict(root=R1, resets="2026-06-30T12:00:00Z", percent=50.0,
         account=ACCOUNT_A, orphaned="2026-07-01T00:00:00Z"),
    # Ties across roots and orphan states at the 07-21 instant.
    dict(root=R2, resets="2026-07-21T12:00:00Z", percent=41.0,
         account=ACCOUNT_B, orphaned="2026-07-15T00:00:00Z"),
    dict(root=R2, resets="2026-07-21T12:00:00Z", percent=37.0,
         account=ACCOUNT_A),
    dict(root=R3, resets="2026-07-21T12:00:00Z", percent=10.0,
         account=ACCOUNT_A),
    dict(root=R3, resets="2026-07-21T13:00:00+01:00", percent=11.0),
    # The orphan-state edge: one identity and account at one instant, one row
    # orphaned and one live, their raw spellings ordering opposite to
    # orphaned_at.
    dict(root=R4, resets="2026-07-21T12:00:00Z", percent=60.0,
         account=ACCOUNT_A, orphaned="2026-07-15T00:00:00Z"),
    dict(root=R4, resets="2026-07-21T14:00:00+02:00", percent=61.0,
         account=ACCOUNT_A),
    # A competing identity on the edge's root and instant whose percent lies
    # between the edge's two rows (Amendment 1c): the cross-identity choice
    # takes it over a 60.0 representative and loses it to a 61.0 one, so the
    # tie order decides the selected cycle KEY (and the detail route's
    # pruned/unknown reason), not only a percent.
    dict(root=R4, slot="primary", resets="2026-07-21T12:00:00Z", percent=60.5,
         account=ACCOUNT_A),
    # Excluded by the statement's own predicates.
    dict(root=R1, resets="2026-07-20T15:00:00Z", percent=5.0,
         account=ACCOUNT_A, window=300),
    dict(root=R1, resets="2026-07-21T12:00:00Z", percent=6.0,
         source="claude"),
)


def _seed_cycles_in(order):
    def seed(conn):
        blocks = list(CYCLE_BLOCKS)
        if order == "reversed":
            blocks.reverse()
        for block in blocks:
            _block(conn, **block)
    return seed


#: Root inputs: one root, several, reordered and duplicated (the function
#: de-duplicates and sorts them; the oracle sees exactly what it binds).
CYCLE_ROOTS = {"one": (R1,), "two": (R1, R2), "three": (R1, R2, R3),
               "reordered": (R3, R1, R2), "duplicated": (R2, R1, R2, R1, ""),
               "edge": (R4,), "edge-multi": (R4, R1),
               # Duplicates normalize to ONE root: the single-root branch.
               "edge-duplicated": (R4, "", R4)}
CYCLE_ACCOUNTS = {"merged": None, "a": ACCOUNT_A, "b": ACCOUNT_B,
                  "unattributed": UNATTRIBUTED}
LIVE_BOUNDARY = types.SimpleNamespace(
    resets_at=dt.datetime(2026, 7, 21, 12, tzinfo=UTC))


def _legacy_cycles_sql_for(production_sql: str) -> str:
    """The frozen pre-#901 statement with the clauses ``production_sql``
    carries: the same orphan and account predicates and as many roots, so the
    same parameters bind it."""
    import re

    roots = re.search(r"source_root_key IN \(([?,]+)\)", production_sql)
    assert roots is not None, production_sql
    return LEGACY_CYCLES_SQL.format(
        orphan=("AND orphaned_at IS NULL "
                if "orphaned_at IS NULL" in production_sql else ""),
        account=("AND account_key=? "
                 if "account_key=?" in production_sql else ""),
        roots=roots.group(1))


class _Fetched:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _CycleReads(sqlite3.Connection):
    """Records the weekly-block read's rows in order; with ``legacy`` set it
    runs the frozen pre-#901 statement in place of the production one."""

    legacy = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def execute(self, sql, params=()):
        text = " ".join(str(sql).split())
        if not (text.startswith(CYCLES_SELECT)
                and "WHERE source='codex' AND window_minutes=10080 " in text
                and "ORDER BY unixepoch(resets_at_utc) DESC" in text):
            return super().execute(sql, params)
        if self.legacy:
            text = _legacy_cycles_sql_for(text)
        rows = super().execute(text, params).fetchall()
        plan = tuple(str(row[3]) for row in super().execute(
            f"EXPLAIN QUERY PLAN {text}", params))
        self.reads.append((text, tuple(params), plan,
                           [tuple(row) for row in rows]))
        return _Fetched(rows)


def _cycle_view(cycles) -> list:
    return [
        (c.root, c.limit, c.slot, c.window, c.account_key, c.limit_id,
         c.limit_name, c.start, c.reset, c.end, c.current_percent,
         c.is_current, tuple(c.member_reset_isos),
         tuple(m.account_key for m in c.cluster_members),
         tuple(m.current_percent for m in c.cluster_members))
        for c in cycles
    ]


def _cycles(path, *, legacy):
    import _cctally_milestone_history as mh

    observed = {}
    for roots in CYCLE_ROOTS:
        for account in CYCLE_ACCOUNTS:
            for orphaned in (False, True):
                for boundary in (False, True):
                    conn = sqlite3.connect(path, factory=_CycleReads)
                    conn.row_factory = sqlite3.Row
                    conn.legacy = legacy
                    try:
                        view = _cycle_view(mh._load_codex_cycles(
                            conn, CYCLE_ROOTS[roots],
                            include_orphaned=orphaned,
                            current_boundary=(
                                LIVE_BOUNDARY if boundary else None),
                            now_utc=NOW,
                            account_key=CYCLE_ACCOUNTS[account]))
                        assert len(conn.reads) == 1, conn.reads
                        observed[(roots, account, orphaned, boundary)] = (
                            conn.reads[0], view)
                    finally:
                        conn.close()
    return observed


EDGE_RESET = dt.datetime(2026, 7, 21, 12, tzinfo=UTC)


def _edge_choice(view) -> list:
    """The edge root's selected cycle at the edge instant: (slot, percent)."""
    return [(cycle[2], cycle[10]) for cycle in view
            if cycle[0] == R4 and cycle[8] == EDGE_RESET]


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_codex_cycles_are_identical_and_stream(ns, tmp_path, order):
    import _cctally_core
    import _cctally_milestone_history as mh

    live, before = _seed_and_split(tmp_path, _seed_cycles_in(order))
    old = _cycles(before, legacy=True)
    new = _cycles(live, legacy=False)
    # The production statement on the pre-#901 schema: the unpinned
    # compatibility path an older index takes.
    compat = _cycles(before, legacy=False)
    assert old.keys() == new.keys() == compat.keys()

    mismatched = []
    branch_problems = []
    for key in old:
        (old_sql, old_params, old_plan, old_rows), old_view = old[key]
        (new_sql, new_params, new_plan, new_rows), new_view = new[key]
        (compat_sql, compat_params, _plan, compat_rows), compat_view = (
            compat[key])
        index, order_by = _designated(CYCLE_ROOTS[key[0]])
        # The oracle really is the frozen statement on the pre-#901 schema.
        # Its plan decides the tie order: a multi-root read is the forward
        # active-index scan; a single-root read (measured, SQLite 3.53.4)
        # seeks the UNIQUE autoindex instead, except account-scoped live-only
        # reads, whose constant orphaned_at makes the two orders agree.
        assert old_sql.endswith("ORDER BY unixepoch(resets_at_utc) DESC")
        assert "INDEXED BY" not in old_sql
        if index == MULTI_ROOT_INDEX:
            assert any("idx_quota_blocks_active" in detail
                       for detail in old_plan), (key, old_plan)
        else:
            assert any(name in detail for detail in old_plan for name in (
                "idx_quota_blocks_active",
                "sqlite_autoindex_quota_window_blocks_1")), (key, old_plan)
        assert new_params == old_params == compat_params, key
        for label, rows, view in (("pinned", new_rows, new_view),
                                  ("unpinned", compat_rows, compat_view)):
            if rows != old_rows or view != old_view:
                mismatched.append((
                    key, label,
                    "rows differ" if rows != old_rows else "rows equal",
                    "cycles differ" if view != old_view else "cycles equal",
                    old_plan))
        # The production read takes the branch its NORMALIZED root count
        # designates: that branch's index and explicit order, with no sorter
        # of any kind (a "LAST n TERMS" tie-run sorter included).
        if not (new_sql.endswith(order_by)
                and f"INDEXED BY {index} " in new_sql
                and any(index in detail for detail in new_plan)
                and not any(marker in detail for detail in new_plan
                            for marker in guard.TEMP_MARKERS)):
            branch_problems.append((key, "pinned", new_sql, new_plan))
        # The unpinned compatibility path keeps ordinary predicates.
        if not (compat_sql.endswith(order_by)
                and "INDEXED BY" not in compat_sql
                and "+" not in compat_sql):
            branch_problems.append((key, "unpinned", compat_sql))

    assert mismatched == [], "\n".join(map(repr, mismatched))
    assert branch_problems == [], "\n".join(map(repr, branch_problems))

    # Non-vacuity: multi-root ties, cross-account exact-instant ties and
    # merged clusters whose member order is observable all occur.
    def second(text):
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            microsecond=0)

    multi_root = old[("three", "merged", True, True)][0][3]
    tied = [row for row in multi_root
            if second(row[7]) == dt.datetime(2026, 7, 21, 12, tzinfo=UTC)]
    assert len({row[0] for row in tied}) == 3, tied
    assert len({row[6] for row in tied if row[0] == R1}) == 3, tied
    assert any(len(set(cycle[13])) > 1
               for cycle in old[("one", "merged", False, True)][1]), (
        "a merged cluster whose members span accounts")

    # Amendment 1c's edge: the two legacy orders DISAGREE on it, and each
    # branch preserves its own legacy result (asserted above against the
    # oracle). A single-root read meets the orphaned 60.0 row first, so the
    # competing 60.5 identity wins the instant; a multi-root read meets the
    # live 61.0 row first and keeps the edge identity.
    for account in ("merged", "a"):
        for boundary in (False, True):
            single = old[("edge", account, True, boundary)][1]
            multi = old[("edge-multi", account, True, boundary)][1]
            assert _edge_choice(single) == [("primary", 60.5)], single
            assert _edge_choice(multi) == [("secondary", 61.0)], multi
            # Duplicate root arguments are the single-root read.
            assert old[("edge-duplicated", account, True, boundary)][1] == (
                single)
            # Live-only, both branches agree: the orphaned row is filtered.
            assert _edge_choice(
                old[("edge", account, False, boundary)][1]) == [
                ("secondary", 61.0)]

    for roots in ((R1,), (R1, R1, ""), (R1, R2), (R2, R1, R3)):
        index, _order_by = _designated(roots)
        for orphaned in (False, True):
            for account in (None, ACCOUNT_A, ACCOUNT_B, UNATTRIBUTED):
                statements = _plans(
                    lambda conn: mh._load_codex_cycles(
                        conn, roots, include_orphaned=orphaned,
                        account_key=account),
                    _cctally_core.DB_PATH,
                    "_cctally_milestone_history._load_codex_cycles")
                reads = [s for s in statements
                         if "FROM quota_window_blocks" in s.sql]
                assert len(reads) == 1, [s.sql for s in reads]
                assert _temp_free(reads) == []
                assert _uses(reads, index)


class _EdgeIdentity:
    quota_identity = None
    resets_at = LIVE_BOUNDARY.resets_at

    def __init__(self, roots):
        self.source_root_keys = roots


def _edge_key(root, slot) -> str:
    import _cctally_milestone_history as mh

    return mh.dashboard_resource_key(
        "milestone_cycle", "codex", root, "codex", slot, 10080,
        EDGE_RESET.isoformat())


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_codex_cycle_detail_keeps_each_branchs_reason(ns, tmp_path, order):
    """Amendment 1c: the include_orphaned=True read decides the detail
    route's 404 reason. On the edge, a single-root route finds the competing
    identity's key among the orphan-inclusive cycles ("pruned"); a multi-root
    route does not ("unknown"). Both must survive the rewrite."""
    import _cctally_milestone_history as mh

    live, before = _seed_and_split(tmp_path, _seed_cycles_in(order))
    keys = {"primary": _edge_key(R4, "primary"),
            "secondary": _edge_key(R4, "secondary"),
            "absent": _edge_key(R5, "secondary")}
    results = {}
    for leg, path in (("legacy", before), ("new", live)):
        for roots in ("edge", "edge-duplicated", "edge-multi"):
            for account in ("merged", "a"):
                for name, key in keys.items():
                    stats = sqlite3.connect(path, factory=_CycleReads)
                    stats.row_factory = sqlite3.Row
                    stats.legacy = leg == "legacy"
                    cache = ns["open_cache_db"]()
                    try:
                        results[(leg, roots, account, name)] = (
                            mh.build_codex_cycle_detail(
                                stats, cache,
                                identity=_EdgeIdentity(CYCLE_ROOTS[roots]),
                                key=key, speed="standard", now_utc=NOW,
                                account_key=CYCLE_ACCOUNTS[account]))
                    finally:
                        stats.close()
                        cache.close()
    differing = [key[1:] for key in results if key[0] == "legacy"
                 and results[key] != results[("new",) + key[1:]]]
    assert differing == [], [
        (key, results[("legacy",) + key], results[("new",) + key])
        for key in differing]
    # Non-vacuity: the pruned/unknown distinction is exercised, and it is
    # exactly where the two legacy orders disagree.
    for account in ("merged", "a"):
        assert results[("legacy", "edge", account, "primary")] == (
            None, "pruned")
        assert results[("legacy", "edge-duplicated", account, "primary")] == (
            None, "pruned")
        assert results[("legacy", "edge-multi", account, "primary")] == (
            None, "unknown")
        assert results[("legacy", "edge", account, "absent")] == (
            None, "unknown")
        found = results[("legacy", "edge", account, "secondary")]
        assert not isinstance(found, tuple), found


#: Numeric-looking TEXT keys: a comparison that applied numeric affinity
#: would equate some of these; TEXT comparison keeps every one distinct.
NUMERIC_ROOTS = ("123", "0123", "1e3", "123.0")
NUMERIC_ACCOUNTS = ("7", "07", "7.0", UNATTRIBUTED)


def _seed_numeric_keys(conn) -> None:
    week = 0
    for root in NUMERIC_ROOTS:
        for account in NUMERIC_ACCOUNTS:
            for orphaned in (None, "2026-07-15T00:00:00Z"):
                week += 1
                _block(conn, root=root, account=account,
                       resets=_plus("2026-07-21T12:00:00Z", days=-7 * week),
                       percent=float(week), orphaned=orphaned)
    # Python integers stored through TEXT affinity become the TEXT '123' / '7'.
    _block(conn, root=123, account=7, slot="primary",
           resets="2026-07-21T12:00:00Z", percent=99.0)
    _block(conn, root=123, account=7, slot="primary",
           resets="2026-07-28T12:00:00Z", percent=98.0,
           orphaned="2026-07-22T00:00:00Z")


def test_the_pinned_unary_plus_filters_the_same_population(ns, tmp_path):
    """Amendment 1c Q2: under the pin, ``+source_root_key IN (...)``,
    ``+account_key = ?`` and ``+orphaned_at IS NULL`` drop the column's
    affinity. That is equivalent only under the TEXT-binding contract (TEXT
    keys, TEXT parameters); this compares the filtered populations with and
    without the operators over numeric-looking keys and both orphan states."""
    import _cctally_milestone_history as mh

    live, _before = _seed_and_split(tmp_path, _seed_numeric_keys)
    root_inputs = (("123",), ("0123",), ("1e3", "123"), ("123.0", "0123"),
                   ("123", "123"), NUMERIC_ROOTS)
    compared = 0
    for roots in root_inputs:
        index, _order_by = _designated(roots)
        for account in (None,) + NUMERIC_ACCOUNTS + ("7.00",):
            for orphaned in (False, True):
                conn = sqlite3.connect(live, factory=_CycleReads)
                conn.row_factory = sqlite3.Row
                try:
                    mh._load_codex_cycles(conn, roots,
                                          include_orphaned=orphaned,
                                          account_key=account)
                    ((text, params, _plan, rows),) = conn.reads
                    assert f"INDEXED BY {index} " in text, text
                    assert "+source_root_key IN" in text, text
                    assert ("+account_key=?" in text) == (account is not None)
                    assert ("+orphaned_at IS NULL" in text) == (not orphaned)
                    plain = (text.replace("+source_root_key", "source_root_key")
                             .replace("+account_key", "account_key")
                             .replace("+orphaned_at", "orphaned_at"))
                    assert "+" not in plain, plain
                    plain_rows = [tuple(row) for row in sqlite3.Connection
                                  .execute(conn, plain, params).fetchall()]
                finally:
                    conn.close()
                assert sorted(rows) == sorted(plain_rows), (roots, account,
                                                            orphaned)
                assert rows == plain_rows, (roots, account, orphaned)
                assert {row[0] for row in rows} <= set(
                    _normalized_roots(roots))
                if account is not None:
                    assert {row[6] for row in rows} <= {account}
                compared += 1
    assert compared == len(root_inputs) * 6 * 2
    # Non-vacuity: TEXT comparison really separates the numeric-looking keys.
    conn = sqlite3.connect(live, factory=_CycleReads)
    conn.row_factory = sqlite3.Row
    try:
        mh._load_codex_cycles(conn, ("123",), include_orphaned=True,
                              account_key="7")
        ((_text, _params, _plan, rows),) = conn.reads
    finally:
        conn.close()
    assert {(row[0], row[6]) for row in rows} == {("123", "7")}
    assert len(rows) == 4, rows


def test_the_weekly_reset_order_indexes_are_the_amended_definitions(ns):
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        for name, ddl in ((MULTI_ROOT_INDEX, WEEKLY_RESET_ORDER_DDL),
                          (SINGLE_ROOT_INDEX, WEEKLY_SINGLE_ROOT_ORDER_DDL)):
            (sql,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name=?", (name,)).fetchone()
            assert " ".join(sql.split()) == ddl
            (entry,) = [statement for _table, _required, statement
                        in _cctally_core._STATS_READ_INDEX_DDL
                        if f"INDEX IF NOT EXISTS {name} " in statement]
            assert " ".join(entry.replace(" IF NOT EXISTS", "").split()) == (
                ddl)
    finally:
        conn.close()


# ── Item 3: Claude milestone week keys (`_navigable_claude_refs`) ────────────


def _usage(conn, *, captured, start_date, end_date, start_at=None,
           end_at=None, account=UNATTRIBUTED, percent=10.0):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent,"
        " payload_json, account_key) VALUES (?,?,?,?,?,?,'{}',?)",
        (captured, start_date, end_date, start_at, end_at, percent, account))


def _milestone(conn, *, start_date, end_date, threshold, account=UNATTRIBUTED,
               alerted=None, start_at=None, end_at=None):
    conn.execute(
        "INSERT INTO percent_milestones (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, percent_threshold,"
        " cumulative_cost_usd, marginal_cost_usd, usage_snapshot_id,"
        " cost_snapshot_id, reset_event_id, account_key, alerted_at)"
        " VALUES (?,?,?,?,?,?,1.0,0.1,1,1,0,?,?)",
        (f"{start_date}T13:00:00Z", start_date, end_date, start_at, end_at,
         threshold, account, alerted))


def _seed_claude_weeks(conn):
    _usage(conn, captured="2026-07-14T10:00:00Z", start_date="2026-07-13",
           end_date="2026-07-20", start_at="2026-07-13T12:00:00+00:00",
           end_at="2026-07-20T12:00:00+00:00")
    _usage(conn, captured="2026-07-08T10:00:00Z", start_date="2026-07-06",
           end_date="2026-07-13", start_at="2026-07-06T12:00:00+00:00",
           end_at="2026-07-13T12:00:00+00:00", account=ACCOUNT_A)
    for threshold in (1, 2, 3):
        _milestone(conn, start_date="2026-07-13", end_date="2026-07-20",
                   threshold=threshold, account=ACCOUNT_A,
                   alerted="2026-07-14T00:00:00Z" if threshold == 1 else None)
        _milestone(conn, start_date="2026-07-13", end_date="2026-07-20",
                   threshold=threshold, account=ACCOUNT_B)
    # A milestone-only week, never alerted.
    _milestone(conn, start_date="2026-06-22", end_date="2026-06-29",
               threshold=5, start_at="2026-06-22T12:00:00+00:00",
               end_at="2026-06-29T12:00:00+00:00")
    _milestone(conn, start_date="2026-06-15", end_date="2026-06-22",
               threshold=7, account=ACCOUNT_B)


def test_claude_week_refs_are_identical_and_stream(ns, tmp_path):
    import _cctally_core
    import _cctally_milestone_history as mh

    live, before = _seed_and_split(tmp_path, _seed_claude_weeks)
    observed = []
    for path in (before, live):
        conn = _open(path)
        try:
            observed.append((
                repr(mh._navigable_claude_refs(conn)),
                sorted(row[0] for row in conn.execute(
                    "SELECT DISTINCT week_start_date FROM percent_milestones")),
            ))
        finally:
            conn.close()
    assert observed[1] == observed[0]
    assert observed[1][1] == ["2026-06-15", "2026-06-22", "2026-07-13"]
    statements = [
        s for s in _plans(mh._navigable_claude_refs, _cctally_core.DB_PATH,
                          "_cctally_milestone_history._navigable_claude_refs")
        if s.sql == "SELECT DISTINCT week_start_date FROM percent_milestones"]
    assert len(statements) == 1
    assert _temp_free(statements) == []
    assert _uses(statements, "idx_percent_milestones_week_date")


# ── Items 4 and 5: historic root keys and root group pairs ───────────────────


def _quota_milestone(conn, *, root, threshold=5, orphaned=None,
                     source="codex"):
    conn.execute(
        "INSERT INTO quota_percent_milestones (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, resets_at_utc,"
        " percent_threshold, captured_at_utc, source_path, line_offset,"
        " high_water_percent, generation, orphaned_at, account_key)"
        " VALUES (?,?,'codex','secondary',10080,'2026-07-21T12:00:00Z',?,"
        " '2026-07-15T00:00:00Z','/p.jsonl',1,?,'g',?,'unattributed')",
        (source, root, threshold, threshold, orphaned))


def _threshold_event(conn, *, root, orphaned=None):
    conn.execute(
        "INSERT INTO quota_threshold_events (source, source_root_key,"
        " logical_limit_key, observed_slot, window_minutes, resets_at_utc,"
        " threshold, qualifying_kind, qualifying_percent, projected_percent,"
        " severity, created_at_utc, disposition, alerted_at, suppressed_at,"
        " orphaned_at, account_key) VALUES ('codex',?,'codex','secondary',"
        " 10080,'2026-07-21T12:00:00Z',90,'actual',91.0,NULL,'warn',"
        " '2026-07-15T00:00:00Z','alerted','2026-07-15T00:00:00Z',NULL,?,"
        " 'unattributed')",
        (root, orphaned))


def _seed_roots(conn):
    # R1: only orphaned blocks. R2: milestones only. R3: threshold events only.
    # R4: projection state only. R5: a non-Codex block.
    _block(conn, root=R1, resets="2026-07-21T12:00:00Z", percent=1.0,
           orphaned="2026-07-15T00:00:00Z", group="g1", digest="d1")
    _quota_milestone(conn, root=R2)
    _quota_milestone(conn, root=R2, threshold=6,
                     orphaned="2026-07-15T00:00:00Z")
    _threshold_event(conn, root=R3, orphaned="2026-07-15T00:00:00Z")
    conn.execute(
        "INSERT INTO quota_projection_state (source_root_key, account_key,"
        " generation, physical_signature, completed_at_utc)"
        " VALUES (?, 'unattributed', 'g', 's', '2026-07-15T00:00:00Z')", (R4,))
    _block(conn, root=R5, resets="2026-07-21T12:00:00Z", percent=1.0,
           source="claude")
    # Group pairs for R2/R3: duplicates across accounts, two digests for one
    # group key, NULL group/digest rows, orphaned rows, other sources.
    for account in (UNATTRIBUTED, ACCOUNT_A, ACCOUNT_B):
        _block(conn, root=R2, resets="2026-07-21T12:00:00Z", percent=2.0,
               account=account, group="g-week", digest="d-week")
    _block(conn, root=R2, resets="2026-07-20T15:00:00Z", percent=3.0,
           window=300, group="g-5h", digest="d-5h-old")
    _block(conn, root=R2, resets="2026-07-20T15:00:30Z", percent=3.5,
           window=300, group="g-5h", digest="d-5h-new")
    _block(conn, root=R2, resets="2026-07-14T12:00:00Z", percent=4.0,
           group=None, digest="d-null-group")
    _block(conn, root=R2, resets="2026-07-07T12:00:00Z", percent=5.0,
           group="g-null-digest", digest=None)
    _block(conn, root=R2, resets="2026-06-30T12:00:00Z", percent=6.0,
           group="g-orphan", digest="d-orphan", orphaned="2026-07-01T00:00:00Z")
    _block(conn, root=R2, resets="2026-06-23T12:00:00Z", percent=7.0,
           group="g-claude", digest="d-claude", source="claude")
    _block(conn, root=R3, resets="2026-07-21T12:00:00Z", percent=8.0,
           group="g-other-root", digest="d-other-root")


def test_historic_roots_and_group_pairs_are_identical_and_stream(ns, tmp_path):
    import _cctally_core
    import _cctally_quota as quota
    import _lib_quota_ledger as ledger

    live, before = _seed_and_split(tmp_path, _seed_roots)
    observed = []
    for path in (before, live):
        conn = _open(path)
        try:
            pairs = {root: quota._root_group_pairs(conn, root)
                     for root in (R1, R2, R3, R4, R5)}
            observed.append((
                quota._historic_root_keys(conn),
                {root: sorted(value) for root, value in pairs.items()},
                {root: ledger.compose_root_signature(value)
                 for root, value in pairs.items()},
            ))
        finally:
            conn.close()
    assert observed[1] == observed[0]
    assert observed[1][0] == {R1, R2, R3, R4, R5}
    assert observed[1][1][R2] == [
        ("g-5h", "d-5h-new"), ("g-5h", "d-5h-old"), ("g-week", "d-week")]
    roots = _plans(quota._historic_root_keys, _cctally_core.DB_PATH,
                   "_cctally_quota._historic_root_keys")
    assert len(roots) == 4
    assert _temp_free(roots) == []
    for index in ("idx_quota_blocks_root_group_pairs", "idx_quota_milestones_root",
                  "idx_quota_threshold_events_root"):
        assert _uses(roots, index), index
    pairs = _plans(lambda conn: quota._root_group_pairs(conn, R2),
                   _cctally_core.DB_PATH, "_cctally_quota._root_group_pairs")
    assert len(pairs) == 1
    assert _temp_free(pairs) == []
    assert _uses(pairs, "idx_quota_blocks_root_group_pairs")


# ── Items 6 and 7: reset events (`_apply_reset_events_to_weekrefs`,
#    `in_place_cut_instants`) ──────────────────────────────────────────────

LEGACY_RESET_EVENTS_SQL = (
    "SELECT id, old_week_end_at, new_week_end_at, effective_reset_at_utc "
    "FROM week_reset_events{account} "
    "ORDER BY unixepoch(effective_reset_at_utc) DESC, id DESC"
)
WEEK_END = "2026-07-20T12:00:00Z"


def _event(conn, *, old, new, effective, account=ACCOUNT_A, origin):
    conn.execute(
        "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at,"
        " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct,"
        " account_key, origin_observation_id) VALUES (?,?,?,?,NULL,?,?)",
        ("2026-07-19T00:00:00Z", old, new, effective, account, origin))


def _seed_reset_events(conn):
    # Boundary shifts of one (old, new) pair: equal effective seconds spelled
    # two ways (id DESC decides), and an earlier one.
    _event(conn, old="2026-07-14T00:00:00Z", new=WEEK_END,
           effective="2026-07-13T12:00:00Z", origin="o1")
    _event(conn, old="2026-07-14T00:00:00Z", new=WEEK_END,
           effective="2026-07-13T14:00:00+02:00", origin="o2")
    _event(conn, old="2026-07-14T00:00:00Z", new=WEEK_END,
           effective="2026-07-13T10:00:00Z", origin="o3")
    # In-place credits (old == effective, raw): one instant spelled twice,
    # an offset spelling, and a cut after the week's end.
    _event(conn, old="2026-07-17T06:00:00Z", new=WEEK_END,
           effective="2026-07-17T06:00:00Z", origin="o4")
    _event(conn, old="2026-07-17T08:00:00+02:00", new=WEEK_END,
           effective="2026-07-17T08:00:00+02:00", origin="o5")
    _event(conn, old="2026-07-18T08:00:00+02:00", new=WEEK_END,
           effective="2026-07-18T08:00:00+02:00", origin="o6")
    _event(conn, old="2026-07-25T00:00:00Z", new=WEEK_END,
           effective="2026-07-25T00:00:00Z", origin="o7")
    # Another account's credit in the same week, and an older shift.
    _event(conn, old="2026-07-16T00:00:00Z", new=WEEK_END,
           effective="2026-07-16T00:00:00Z", account=ACCOUNT_B, origin="o8")
    _event(conn, old="2026-07-07T00:00:00Z", new="2026-07-13T12:00:00Z",
           effective="2026-07-06T18:00:00Z", account=UNATTRIBUTED, origin="o9")


def _week_refs():
    import _cctally_core

    return [
        _cctally_core.make_week_ref(
            week_start_date="2026-07-13", week_end_date="2026-07-20",
            week_start_at="2026-07-13T12:00:00+00:00",
            week_end_at="2026-07-20T12:00:00+00:00"),
        _cctally_core.make_week_ref(
            week_start_date="2026-07-07", week_end_date="2026-07-14",
            week_start_at="2026-07-07T00:00:00+00:00",
            week_end_at="2026-07-14T00:00:00+00:00"),
        _cctally_core.make_week_ref(
            week_start_date="2026-07-06", week_end_date="2026-07-13",
            week_start_at="2026-07-06T12:00:00+00:00",
            week_end_at="2026-07-13T12:00:00+00:00"),
    ]


def test_reset_events_are_identical_and_stream(ns, tmp_path):
    import _cctally_core
    import _cctally_weekrefs as weekrefs

    live, before = _seed_and_split(tmp_path, _seed_reset_events)
    accounts = (None, ACCOUNT_A, ACCOUNT_B, UNATTRIBUTED)
    observed = []
    for path in (before, live):
        conn = _open(path)
        try:
            observed.append({
                account: (
                    [tuple(row) for row in conn.execute(
                        LEGACY_RESET_EVENTS_SQL.format(
                            account="" if account is None
                            else " WHERE account_key = ?"),
                        () if account is None else (account,))],
                    weekrefs._apply_reset_events_to_weekrefs(
                        conn, _week_refs(), account_key=account),
                    _cctally_core.in_place_cut_instants(
                        conn, account_key=account),
                )
                for account in accounts
            })
        finally:
            conn.close()
    assert observed[1] == observed[0]
    assert len(observed[1][ACCOUNT_A][2]) == 2, "o4/o5 dedupe, o7 rejected"
    assert len(observed[1][ACCOUNT_A][1]) > len(_week_refs()), "split weeks"
    for account in (None, ACCOUNT_A):
        for site, call in (
            ("_cctally_weekrefs._apply_reset_events_to_weekrefs",
             lambda conn: weekrefs._apply_reset_events_to_weekrefs(
                 conn, _week_refs(), account_key=account)),
            ("_cctally_core.in_place_cut_instants",
             lambda conn: _cctally_core.in_place_cut_instants(
                 conn, account_key=account)),
        ):
            reads = [s for s in _plans(call, _cctally_core.DB_PATH, site)
                     if "FROM week_reset_events" in s.sql]
            assert len(reads) == 1, (site, [s.sql for s in reads])
            assert _temp_free(reads) == []
            assert _uses(reads, "idx_week_reset_events_effective_order")


# ── Item 8: recent weeks across usage and cost (`get_recent_weeks`) ──────────

LEGACY_RECENT_WEEKS_SQL = """
        SELECT week_start_date, MAX(week_end_date) AS week_end_date
        FROM (
          SELECT week_start_date, week_end_date FROM weekly_usage_snapshots{acct}
          UNION ALL
          SELECT week_start_date, week_end_date FROM weekly_cost_snapshots{acct}
        )
        GROUP BY week_start_date
        ORDER BY week_start_date DESC
        LIMIT ?
        """


def _legacy_recent_week_rows(conn, limit, *, account_key=None):
    limit_sql = -1 if limit is None else int(limit)
    acct = "" if account_key is None else " WHERE account_key = ?"
    params = () if account_key is None else (account_key,)
    return [(row[0], row[1]) for row in conn.execute(
        LEGACY_RECENT_WEEKS_SQL.format(acct=acct),
        params + params + (limit_sql,))]


def _cost(conn, *, captured, start_date, end_date, account=UNATTRIBUTED):
    conn.execute(
        "INSERT INTO weekly_cost_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, cost_usd, account_key) VALUES (?,?,?,1.0,?)",
        (captured, start_date, end_date, account))


def _seed_recent_weeks(conn):
    _usage(conn, captured="2026-07-14T10:00:00Z", start_date="2026-07-13",
           end_date="2026-07-20", start_at="2026-07-13T12:00:00+00:00",
           end_at="2026-07-20T12:00:00+00:00", account=ACCOUNT_A)
    _usage(conn, captured="2026-07-08T10:00:00Z", start_date="2026-07-06",
           end_date="2026-07-13")
    # A conflicting end date for the same week, on the other leg too.
    _usage(conn, captured="2026-07-09T10:00:00Z", start_date="2026-07-06",
           end_date="2026-07-12", account=ACCOUNT_B)
    _cost(conn, captured="2026-07-15T10:00:00Z", start_date="2026-07-13",
          end_date="2026-07-21", account=ACCOUNT_A)
    _cost(conn, captured="2026-07-15T11:00:00Z", start_date="2026-07-06",
          end_date="2026-07-14", account=ACCOUNT_B)
    _usage(conn, captured="2026-07-01T10:00:00Z", start_date="2026-06-29",
           end_date="2026-07-06", account=ACCOUNT_B)
    # Cost-only weeks, one of which is not a date at all (an invalid ref that
    # sorts first and still counts against the limit).
    _cost(conn, captured="2026-06-23T10:00:00Z", start_date="2026-06-22",
          end_date="2026-06-29")
    _cost(conn, captured="2026-06-23T11:00:00Z", start_date="not-a-date",
          end_date="2026-06-29")
    _cost(conn, captured="2026-06-16T10:00:00Z", start_date="2026-06-15",
          end_date="2026-06-22", account=ACCOUNT_A)


RECENT_LIMITS = (None, -1, 0, 1, 2, 3, 4, 5, 100)
RECENT_ACCOUNTS = (None, ACCOUNT_A, ACCOUNT_B, UNATTRIBUTED, "nobody")


def test_recent_week_rows_match_the_legacy_grouping(ns, tmp_path):
    import _cctally_core
    import _cctally_weekrefs as weekrefs

    live, _before = _seed_and_split(tmp_path, _seed_recent_weeks)
    conn = _open(live)
    try:
        for account in RECENT_ACCOUNTS:
            for limit in RECENT_LIMITS:
                assert weekrefs._recent_week_rows(
                    conn, limit, account_key=account) == (
                    _legacy_recent_week_rows(conn, limit, account_key=account)
                ), (account, limit)
        assert [row[0] for row in weekrefs._recent_week_rows(conn, None)] == [
            "not-a-date", "2026-07-13", "2026-07-06", "2026-06-29",
            "2026-06-22", "2026-06-15"]
        assert ("2026-07-13", "2026-07-21") in weekrefs._recent_week_rows(
            conn, None)
    finally:
        conn.close()
    for account in (None, ACCOUNT_A):
        reads = [s for s in _plans(
            lambda c: weekrefs._recent_week_rows(c, None, account_key=account),
            _cctally_core.DB_PATH, "_cctally_weekrefs._recent_week_rows")
            if "GROUP BY week_start_date" in s.sql]
        assert len(reads) == 2, [s.sql for s in reads]
        assert _temp_free(reads) == []
        # Amendment 1b: the usage leg is pinned to the covering
        # idx_usage_week_date_group; the cost leg keeps idx_cost_week_time.
        assert _uses(reads, "USING COVERING INDEX idx_usage_week_date_group")
        assert _uses(reads, "idx_cost_week_time")


def test_recent_week_rows_fall_back_to_the_week_time_index(ns, tmp_path):
    """Without the epoch-1017 usage index the usage leg keeps its
    compatibility pin to idx_usage_week_time and returns the same rows."""
    import _cctally_core
    import _cctally_weekrefs as weekrefs

    live, _before = _seed_and_split(tmp_path, _seed_recent_weeks)
    conn = _open(live)
    try:
        expected = {account: weekrefs._recent_week_rows(
            conn, None, account_key=account) for account in RECENT_ACCOUNTS}
        conn.execute("DROP INDEX idx_usage_week_date_group")
        conn.commit()
        assert {account: weekrefs._recent_week_rows(
            conn, None, account_key=account)
            for account in RECENT_ACCOUNTS} == expected
    finally:
        conn.close()
    for account in (None, ACCOUNT_A):
        reads = [s for s in _plans(
            lambda c: weekrefs._recent_week_rows(c, None, account_key=account),
            _cctally_core.DB_PATH, "_cctally_weekrefs._recent_week_rows")
            if "GROUP BY week_start_date" in s.sql]
        assert len(reads) == 2, [s.sql for s in reads]
        assert _temp_free(reads) == []
        assert _uses(reads, "idx_usage_week_time")
        assert not _uses(reads, "idx_usage_week_date_group")


def test_recent_weeks_are_identical_with_the_legacy_grouping(
    ns, tmp_path, monkeypatch,
):
    import _cctally_weekrefs as weekrefs

    live, _before = _seed_and_split(tmp_path, _seed_recent_weeks)

    def observe():
        conn = _open(live)
        try:
            return {(account, limit): weekrefs.get_recent_weeks(
                conn, limit, account_key=account)
                for account in RECENT_ACCOUNTS for limit in RECENT_LIMITS}
        finally:
            conn.close()

    new = observe()
    with monkeypatch.context() as patch:
        patch.setattr(weekrefs, "_recent_week_rows", _legacy_recent_week_rows)
        old = observe()
    assert new == old
    assert len(new[(None, None)]) == 5, "the invalid ref is filtered"
    assert new[(None, 1)] == [], "the limit counts the invalid ref"


def test_recent_week_rows_read_one_snapshot_and_keep_the_callers_transaction(
    ns, tmp_path,
):
    import _cctally_weekrefs as weekrefs

    live, _before = _seed_and_split(tmp_path, _seed_recent_weeks)
    conn = _open(live)
    try:
        assert not conn.in_transaction
        weekrefs._recent_week_rows(conn, None)
        assert not conn.in_transaction, "an own transaction must be closed"
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()
        weekrefs._recent_week_rows(conn, None)
        assert conn.in_transaction, "the caller's transaction must survive"
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    statements = []
    real = sqlite3.Connection.execute

    class Recording(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append((" ".join(str(sql).split()), self.in_transaction))
            return real(self, sql, *args)

    conn = sqlite3.connect(live, factory=Recording)
    try:
        weekrefs._recent_week_rows(conn, 3)
    finally:
        conn.close()
    reads = [in_tx for sql, in_tx in statements if "GROUP BY" in sql]
    assert reads == [True, True], statements


# ── Item 9: subscription-week anchors (`_compute_subscription_weeks`) ────────

LEGACY_SUBSCRIPTION_ANCHOR_SQL = (
    "SELECT "
    "    MIN(week_start_at) AS week_start_at, "
    "    MIN(week_end_at)   AS week_end_at, "
    "    week_start_date, "
    "    MIN(week_end_date) AS week_end_date "
    "FROM weekly_usage_snapshots "
    "WHERE week_start_at IS NOT NULL "
    "  AND week_end_at   IS NOT NULL "
    "  AND week_start_date IS NOT NULL "
    "  {acct} "
    "GROUP BY week_start_date "
    "ORDER BY MIN(week_start_at) ASC"
)


def _legacy_subscription_anchor_rows(conn, account_key):
    acct = "" if account_key is None else " AND account_key = ?"
    params = () if account_key is None else (account_key,)
    return conn.execute(
        LEGACY_SUBSCRIPTION_ANCHOR_SQL.format(acct=acct), params).fetchall()


def _anchor(conn, *, date, start, end_at, end_date, account=UNATTRIBUTED):
    _usage(conn, captured="2026-07-20T00:00:00Z", start_date=date,
           end_date=end_date, start_at=start, end_at=end_at, account=account)


def _seed_subscription_weeks(order):
    def seed(conn):
        rows = []
        # Three minima on three different rows of one date.
        rows += [
            dict(date="2026-07-13", start="2026-07-13T12:00:00Z",
                 end_at="2026-07-20T12:00:00Z", end_date="2026-07-20"),
            dict(date="2026-07-13", start="2026-07-13T12:00:00+00:00",
                 end_at="2026-07-20T12:30:00Z", end_date="2026-07-20"),
            dict(date="2026-07-13", start="2026-07-13T13:00:00Z",
                 end_at="2026-07-20T11:00:00Z", end_date="2026-07-20"),
            dict(date="2026-07-13", start="2026-07-13T14:00:00Z",
                 end_at="2026-07-20T13:00:00Z", end_date="2026-07-19"),
        ]
        # Duplicate minima.
        rows += [dict(date="2026-07-06", start="2026-07-06T12:00:00Z",
                      end_at="2026-07-13T12:00:00Z", end_date="2026-07-13")] * 2
        # Equal minimum starts on two dates.
        rows += [
            dict(date="2026-06-30", start="2026-06-29T12:00:00Z",
                 end_at="2026-07-06T12:00:00Z", end_date="2026-07-06"),
            dict(date="2026-06-29", start="2026-06-29T12:00:00Z",
                 end_at="2026-07-06T12:00:00Z", end_date="2026-07-05"),
        ]
        # Date order and start order disagree.
        rows += [
            dict(date="2026-06-15", start="2026-06-22T12:00:00Z",
                 end_at="2026-06-29T12:00:00Z", end_date="2026-06-29"),
            dict(date="2026-06-22", start="2026-06-15T12:00:00Z",
                 end_at="2026-06-22T12:00:00Z", end_date="2026-06-22"),
        ]
        # A data gap longer than the re-anchor window, then an early anchor.
        rows += [dict(date="2026-05-11", start="2026-05-11T12:00:00Z",
                      end_at="2026-05-18T12:00:00Z", end_date="2026-05-18")]
        accounts = [(UNATTRIBUTED, row) for row in rows]
        accounts += [(ACCOUNT_A, dict(row, start=_plus(row["start"], hours=3)
                                      .replace("Z", "+00:00")))
                     for row in rows[::2]]
        # Ineligible rows (NULL boundary) and an empty boundary string, which
        # MIN() prefers and the consumer cannot parse.
        accounts += [
            (ACCOUNT_B, dict(date="2026-07-13", start="2026-07-13T09:00:00Z",
                             end_at=None, end_date="2026-07-20")),
            (ACCOUNT_B, dict(date="2026-07-13", start=None,
                             end_at="2026-07-20T09:00:00Z",
                             end_date="2026-07-20")),
            (ACCOUNT_B, dict(date="2026-07-06", start="2026-07-06T09:00:00Z",
                             end_at="", end_date="2026-07-13")),
        ]
        if order == "reversed":
            accounts.reverse()
        for account, row in accounts:
            _anchor(conn, account=account, **row)
    return seed


SUBSCRIPTION_ACCOUNTS = (None, UNATTRIBUTED, ACCOUNT_A, ACCOUNT_B)
SUBSCRIPTION_RANGES = (
    (dt.datetime(2026, 5, 1, tzinfo=UTC), dt.datetime(2026, 8, 15, tzinfo=UTC)),
    (dt.datetime(2026, 6, 20, tzinfo=UTC), dt.datetime(2026, 7, 16, tzinfo=UTC)),
)


def _outcome(call):
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - the outcome itself is compared
        return ("raises", type(exc).__name__, str(exc))


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_subscription_weeks_are_identical_and_stream(
    ns, tmp_path, monkeypatch, order,
):
    import _cctally_core
    import _lib_subscription_weeks as sw

    live, _before = _seed_and_split(tmp_path, _seed_subscription_weeks(order))

    def observe():
        conn = _open(live)
        try:
            return {
                (account, start, end): (
                    [tuple(row) for row in sw._subscription_anchor_rows(
                        conn, account)],
                    _outcome(lambda: sw._compute_subscription_weeks(
                        conn, start, end, {}, account_key=account)),
                )
                for account in SUBSCRIPTION_ACCOUNTS
                for start, end in SUBSCRIPTION_RANGES
            }
        finally:
            conn.close()

    new = observe()
    with monkeypatch.context() as patch:
        patch.setattr(sw, "_subscription_anchor_rows",
                      _legacy_subscription_anchor_rows)
        old = observe()
    assert new == old
    merged = new[(None, *SUBSCRIPTION_RANGES[0])]
    assert merged[0][:2] == [
        ("2026-05-11T12:00:00Z", "2026-05-18T12:00:00Z", "2026-05-11",
         "2026-05-18"),
        ("2026-06-15T12:00:00Z", "2026-06-22T12:00:00Z", "2026-06-22",
         "2026-06-22"),
    ]
    assert ("2026-07-13T12:00:00+00:00", "2026-07-20T11:00:00Z", "2026-07-13",
            "2026-07-19") in merged[0], "three minima from three rows"
    unattributed = new[(UNATTRIBUTED, *SUBSCRIPTION_RANGES[0])]
    assert unattributed[1][0] == "ok" and unattributed[1][1], "non-vacuity"
    assert new[(ACCOUNT_B, *SUBSCRIPTION_RANGES[0])][1][0] == "raises"

    for account in (None, ACCOUNT_A):
        reads = [s for s in _plans(
            lambda c: sw._subscription_anchor_rows(c, account),
            _cctally_core.DB_PATH, "_lib_subscription_weeks._subscription_anchor_rows")
            if "FROM weekly_usage_snapshots" in s.sql]
        assert len(reads) == 1, [s.sql for s in reads]
        assert _temp_free(reads) == []
        assert _uses(reads, "idx_usage_subscription_anchor_order")
        assert _uses(reads, "idx_usage_subscription_anchor_pick")


def test_the_subscription_anchor_indexes_match_amendment_1b(ns):
    """Amendment 1b Q2.1: the order index omits ``week_start_date IS NOT
    NULL`` because the column is declared NOT NULL (membership unchanged);
    the pick index keeps the answer's full predicate."""
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        notnull = {row[1]: row[3] for row in conn.execute(
            "PRAGMA table_info(weekly_usage_snapshots)")}
        definitions = {name: " ".join(sql.split()) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_usage_subscription_anchor_order', "
            "'idx_usage_subscription_anchor_pick')")}
    finally:
        conn.close()
    assert notnull["week_start_date"] == 1
    assert definitions == {
        "idx_usage_subscription_anchor_order": (
            "CREATE INDEX idx_usage_subscription_anchor_order "
            "ON weekly_usage_snapshots(week_start_at ASC, week_start_date ASC) "
            "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL"),
        "idx_usage_subscription_anchor_pick": (
            "CREATE INDEX idx_usage_subscription_anchor_pick "
            "ON weekly_usage_snapshots(week_start_date ASC, week_start_at ASC, "
            "id ASC) WHERE week_start_at IS NOT NULL AND week_end_at IS NOT "
            "NULL AND week_start_date IS NOT NULL"),
    }
