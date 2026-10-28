"""#997 D19 / T19: the rebuild's open-block key query ranks each account once.

``_backfill_five_hour_blocks(conn, only_missing=True)`` selects, per account,
the window of its latest qualifying snapshot that has no block yet. It found
that snapshot with a correlated scalar subquery, which SQLite evaluates once
per qualifying row with a full scan and a temp B-tree each time: 68.4 s on a
production-shaped store (spec section 21). The rewrite computes each
account's latest qualifying snapshot once with ``ROW_NUMBER()``.

Every test reads the statement the function actually executes, captured with
``set_trace_callback``, never a copy of it. The equivalence tests keep the old
query text verbatim as their oracle and run both over a permissive scratch
schema, because the store's ``account_key`` is ``NOT NULL`` and the NULL
partition the old ``IS`` comparison handled can only be exercised outside it.
The fixture cases are spec T19's; competing rows always sit in different
windows, so whichever row a query picks shows up in the selected keys.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from conftest import load_script, redirect_paths


# The pre-#997-D19 `only_missing` key query, verbatim (HEAD 0bfc75475).
OLD_KEYS_SQL = (
    """
                SELECT DISTINCT snapshots.five_hour_window_key,
                                snapshots.account_key
                  FROM weekly_usage_snapshots AS snapshots
                 WHERE snapshots.five_hour_window_key IS NOT NULL
                   AND snapshots.five_hour_percent     IS NOT NULL
        """
    "   AND snapshots.id = ("
    "       SELECT latest.id"
    "         FROM weekly_usage_snapshots AS latest"
    "        WHERE latest.account_key IS snapshots.account_key"
    "          AND latest.five_hour_window_key IS NOT NULL"
    "          AND latest.five_hour_percent IS NOT NULL"
    "        ORDER BY unixepoch(latest.captured_at_utc) DESC,"
    "                 latest.id DESC"
    "        LIMIT 1"
    "   )"
    "   AND NOT EXISTS ("
    "       SELECT 1 FROM five_hour_blocks AS blocks"
    "        WHERE blocks.five_hour_window_key ="
    "              snapshots.five_hour_window_key"
    "          AND blocks.account_key IS snapshots.account_key"
    "   )"
)

_KEY_SQL_MARKER = "SELECT DISTINCT snapshots.five_hour_window_key"


def _wrong(old: str, new: str) -> str:
    assert OLD_KEYS_SQL.count(old) == 1, old
    return OLD_KEYS_SQL.replace(old, new)


#: Spec T19's three wrong rewrites, each one edit away from the oracle.
WRONG_REWRITES = {
    "text_ordering": _wrong(
        "ORDER BY unixepoch(latest.captured_at_utc) DESC,",
        "ORDER BY latest.captured_at_utc DESC,",
    ),
    "equals_account_match": _wrong(
        "WHERE latest.account_key IS snapshots.account_key",
        "WHERE latest.account_key = snapshots.account_key",
    ),
    "lowest_id_ties": _wrong(
        "latest.id DESC",
        "latest.id ASC",
    ),
}


def _open_store(ns, tmp_path, scratch_policy: bool):
    import _cctally_store

    conn = ns["open_db"](_target_path=str(tmp_path / "stats.db"))
    if scratch_policy:
        # The rebuild runs the projection on a replay scratch, whose extra
        # indexes (D6) could change the plan.
        _cctally_store.apply_disposable_scratch_policy(conn)
    return conn


def _captured_key_sql(conn, ns) -> str:
    """Run the real projection and return the key query it executed."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        ns["_backfill_five_hour_blocks"](conn, only_missing=True)
    finally:
        conn.set_trace_callback(None)
    key_sql = [s for s in statements if _KEY_SQL_MARKER in s]
    assert len(key_sql) == 1, statements
    return key_sql[0]


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def executed_key_sql(ns, tmp_path):
    conn = _open_store(ns, tmp_path, scratch_policy=False)
    try:
        return _captured_key_sql(conn, ns)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The plan check.
# --------------------------------------------------------------------------

_READ_RE = re.compile(
    r"^(?:SCAN|SEARCH)\s+(?:TABLE\s+)?(\S+)(?:\s+AS\s+(\S+))?")
_ALLOWED_CORRELATED_READS = {"blocks", "five_hour_blocks"}
_SNAPSHOT_NAMES = ("weekly_usage_snapshots", "snapshots", "latest")


def _plan_tree(conn, sql):
    rows = sqlite3.Connection.execute(
        conn, "EXPLAIN QUERY PLAN " + sql).fetchall()
    nodes = {int(r[0]): (int(r[1]), str(r[3])) for r in rows}
    children: dict[int, list[int]] = {}
    for node_id, (parent, _detail) in nodes.items():
        children.setdefault(parent, []).append(node_id)
    return nodes, children


def _subtree(children, root):
    out, stack = [], list(children.get(root, []))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(children.get(node, []))
    return out


@pytest.mark.parametrize("scratch_policy", [False, True],
                         ids=["store", "replay_scratch"])
def test_open_block_keys_plan_has_no_correlated_snapshot_scan(
    ns, tmp_path, scratch_policy,
):
    conn = _open_store(ns, tmp_path, scratch_policy)
    try:
        sql = _captured_key_sql(conn, ns)
        nodes, children = _plan_tree(conn, sql)
    finally:
        conn.close()
    rendered = "\n".join(
        f"{i} <- {p}: {d}" for i, (p, d) in sorted(nodes.items()))

    # Non-vacuity: the plan was read, it reads the snapshots at all, and it
    # has the kept NOT EXISTS, which is itself a correlated subquery.
    assert any(
        _READ_RE.match(d) and any(n in d for n in _SNAPSHOT_NAMES)
        for _p, d in nodes.values()
    ), rendered
    correlated = [i for i, (_p, d) in nodes.items()
                  if d.startswith("CORRELATED ")]
    assert correlated, rendered

    for root in correlated:
        for node in _subtree(children, root):
            detail = nodes[node][1]
            assert "TEMP B-TREE" not in detail, (
                f"correlated subquery {root} sorts per outer row:\n"
                f"{rendered}")
            assert not any(n in detail for n in _SNAPSHOT_NAMES), (
                f"correlated subquery {root} reads the snapshots per outer "
                f"row:\n{rendered}")
            read = _READ_RE.match(detail)
            if read:
                assert read.group(1) in _ALLOWED_CORRELATED_READS, rendered


# --------------------------------------------------------------------------
# Equivalence with the old query.
# --------------------------------------------------------------------------

A, B = "acct-a", "acct-b"

# rows: (id, captured_at_utc, five_hour_window_key, five_hour_percent,
#        account_key); blocks: (five_hour_window_key, account_key).
CASES = {
    # Two real accounts plus several NULL-account snapshots, each in its own
    # window. acct-b's latest capture has the lower id.
    "accounts_and_null_partition": dict(
        rows=[
            (1, "2026-01-01T00:00:00Z", 100, 1.0, A),
            (2, "2026-01-01T01:00:00Z", 101, 1.0, A),
            (3, "2026-01-01T02:00:00Z", 200, 1.0, B),
            (4, "2026-01-01T00:30:00Z", 201, 1.0, B),
            (5, "2026-01-01T03:00:00Z", 300, 1.0, None),
            (6, "2026-01-01T01:00:00Z", 301, 1.0, None),
            (7, "2026-01-01T02:00:00Z", 302, 1.0, None),
        ],
        blocks=[],
        expected={(101, A), (200, B), (300, None)},
    ),
    # One account, two captures in the same second, in different windows.
    # unixepoch() truncates the fraction, so they tie and the higher id (the
    # +00:00 row) wins. As text, `.` sorts after `+`, so the text-order
    # winner is the lower id.
    "same_second_text_winner_is_not_highest_id": dict(
        rows=[
            (1, "2026-01-01T10:00:00.500Z", 110, 1.0, A),
            (2, "2026-01-01T10:00:00+00:00", 111, 1.0, A),
        ],
        blocks=[],
        expected={(111, A)},
    ),
    # A -02:00 capture at 09:00 is 11:00Z, later than 10:00Z, though it
    # sorts earlier as text.
    "offset_text_order_differs_from_instant": dict(
        rows=[
            (1, "2026-01-02T09:00:00-02:00", 120, 1.0, A),
            (2, "2026-01-02T10:00:00Z", 121, 1.0, A),
        ],
        blocks=[],
        expected={(120, A)},
    ),
    # acct-a's newest-id row is unparseable beside an older valid row: a
    # NULL unixepoch sorts last under DESC. acct-b's rows are all
    # unparseable, so the id alone decides.
    "unparseable_captures": dict(
        rows=[
            (1, "2026-01-03T00:00:00Z", 131, 1.0, A),
            (2, "garbage", 130, 1.0, A),
            (3, "not-a-time", 230, 1.0, B),
            (4, "also bad", 231, 1.0, B),
        ],
        blocks=[],
        expected={(131, A), (231, B)},
    ),
    # The latest qualifying window already has a block while an older window
    # of the same account has none: that account yields no key (no fallback
    # to the older window). The NULL partition behaves the same way.
    "blocked_latest_window_with_older_unblocked": dict(
        rows=[
            (1, "2026-01-05T00:00:00Z", 140, 1.0, A),
            (2, "2026-01-05T01:00:00Z", 141, 1.0, A),
            (3, "2026-01-05T01:00:00Z", 240, 1.0, B),
            (4, "2026-01-05T00:00:00Z", 340, 1.0, None),
            (5, "2026-01-05T02:00:00Z", 341, 1.0, None),
        ],
        blocks=[(141, A), (341, None)],
        expected={(240, B)},
    ),
    # Rows with a NULL window key or a NULL percent are the newest; they
    # never become the latest qualifying snapshot.
    "non_qualifying_rows_newest": dict(
        rows=[
            (1, "2026-01-04T00:00:00Z", 150, 1.0, A),
            (2, "2026-01-04T00:30:00Z", 151, 1.0, A),
            (3, "2026-01-04T01:00:00Z", None, 1.0, A),
            (4, "2026-01-04T02:00:00Z", 152, None, A),
            (5, "2026-01-04T00:00:00Z", 250, 1.0, B),
            (6, "2026-01-04T03:00:00Z", 251, None, B),
            (7, "2026-01-04T04:00:00Z", None, 1.0, B),
        ],
        blocks=[],
        expected={(151, A), (250, B)},
    ),
}


def _scratch(case) -> sqlite3.Connection:
    """A permissive schema with the columns the key query reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " id INTEGER PRIMARY KEY, captured_at_utc TEXT,"
        " five_hour_window_key INTEGER, five_hour_percent REAL,"
        " account_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE five_hour_blocks ("
        " five_hour_window_key INTEGER, account_key TEXT)"
    )
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots VALUES (?, ?, ?, ?, ?)",
        case["rows"],
    )
    conn.executemany(
        "INSERT INTO five_hour_blocks VALUES (?, ?)", case["blocks"],
    )
    return conn


def _keys(sql, case):
    conn = _scratch(case)
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    assert len(rows) == len(set(rows)), rows
    return set(rows)


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_open_block_keys_equal_old_query(executed_key_sql, case_name):
    case = CASES[case_name]
    oracle = _keys(OLD_KEYS_SQL, case)
    # The fixture states its intent, so a case cannot silently degenerate.
    assert oracle == case["expected"]
    assert _keys(executed_key_sql, case) == oracle


#: Where each wrong rewrite must part from the oracle. Pinned exactly, so a
#: fixture edit that stops a case from discriminating fails here.
EXPECTED_DISAGREEMENTS = {
    "text_ordering": {
        "same_second_text_winner_is_not_highest_id",
        "offset_text_order_differs_from_instant",
        "unparseable_captures",
    },
    "equals_account_match": {"accounts_and_null_partition"},
    "lowest_id_ties": {
        "same_second_text_winner_is_not_highest_id",
        "unparseable_captures",
    },
}


@pytest.mark.parametrize("rewrite", sorted(WRONG_REWRITES))
def test_wrong_rewrites_disagree_with_oracle(rewrite):
    disagreements = {
        name for name, case in CASES.items()
        if _keys(WRONG_REWRITES[rewrite], case)
        != _keys(OLD_KEYS_SQL, case)
    }
    assert disagreements, rewrite
    assert disagreements == EXPECTED_DISAGREEMENTS[rewrite]
