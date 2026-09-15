"""#834 S2 (#837): every week-boundary read resolves inside ONE account.

`_get_canonical_boundary_for_date` took no account parameter and its query
carried no account predicate, so on a store holding two accounts in one week
the account that captured FIRST owned every other account's boundary. The
query is::

    ORDER BY captured_at_utc ASC, id ASC LIMIT 1

so it returns the EARLIEST established boundary. Every fixture in this module
therefore gives the INACTIVE account the earliest capture: a fixture built on a
"newer" competing boundary proves nothing, because a newer row can never win
that query and the test would pass against the defect.

The write path (`record-usage`) is the most serious of the call sites, because
it stores the contaminated boundary rather than merely rendering it.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import pathlib

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

AS_OF = "2026-06-19T14:37:00Z"

WEEK_START_DATE = "2026-06-13"
WEEK_END_DATE = "2026-06-20"

# Account A is the ACTIVE account in every test here.
A_UUID = "acct-uuid-active-A"
A_WS_AT = "2026-06-13T05:00:00+00:00"
A_WE_AT = "2026-06-20T05:00:00+00:00"

# Account B is INACTIVE and captured EARLIEST, so it owns the account-blind
# boundary the defect returns.
B_UUID = "acct-uuid-inactive-B"
B_WS_AT = "2026-06-13T01:00:00+00:00"
B_WE_AT = "2026-06-20T01:00:00+00:00"

B_CAPTURED = "2026-06-14T01:05:00Z"   # earliest
A_CAPTURED = "2026-06-15T05:05:00Z"   # later

# The observation under test resets at 06:00, so the boundary the payload
# DERIVES differs from every seeded boundary. A stored 06:00 row would mean the
# canonical override stopped running; a stored 01:00 row is account bleed.
RESETS_AT_EPOCH = 1781935200          # 2026-06-20T06:00:00Z
DERIVED_WS_AT = "2026-06-13T06:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _account_key(uuid: str) -> str:
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _activate(uuid: str) -> str:
    """Make ``uuid`` the active Claude identity and return its account key."""
    _cctally_core.CLAUDE_JSON_PATH.write_text(json.dumps({
        "oauthAccount": {
            "accountUuid": uuid,
            "emailAddress": f"{uuid}@example.com",
            "plan": "max",
        }
    }))
    _cctally_core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)
    return _account_key(uuid)


def _stamp_journal_id(conn, table: str, rowid: int) -> None:
    """Stamp the cutover-scheme ``journal_id`` on a directly-seeded row so the
    ingest harvest never reverse-refs to a NULL-``journal_id`` row."""
    conn.execute(
        f"UPDATE {table} SET journal_id = ? WHERE id = ?",
        (f"b:{table}:{rowid}", rowid),
    )


def _seed_snapshot(conn, *, account_key, captured, ws_at, we_at, pct,
                   source="userscript", held=0):
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " account_key, weekly_observation_held) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (captured, WEEK_START_DATE, WEEK_END_DATE, ws_at, we_at, pct,
         None, source, "{}", account_key, held),
    )
    rowid = int(cur.lastrowid)
    _stamp_journal_id(conn, "weekly_usage_snapshots", rowid)
    conn.commit()
    return rowid


def _seed_two_account_week(ns, *, a_key, b_key, a_pct=46.0, b_pct=20.0,
                           inactive_first=True):
    """Seed one week held by two accounts, the INACTIVE one capturing earliest.

    ``inactive_first`` flips only the INSERT order, never the capture stamps:
    the boundary query orders by ``captured_at_utc ASC, id ASC``, so insertion
    order is the tie-break the fixture must vary independently.
    """
    conn = ns["open_db"]()
    try:
        rows = [
            dict(account_key=b_key, captured=B_CAPTURED, ws_at=B_WS_AT,
                 we_at=B_WE_AT, pct=b_pct),
            dict(account_key=a_key, captured=A_CAPTURED, ws_at=A_WS_AT,
                 we_at=A_WE_AT, pct=a_pct),
        ]
        if not inactive_first:
            rows.reverse()
        for row in rows:
            _seed_snapshot(conn, **row)
    finally:
        conn.close()


def _record_usage_args(*, percent, resets_at=RESETS_AT_EPOCH):
    return argparse.Namespace(
        percent=percent,
        resets_at=resets_at,
        five_hour_percent=None,
        five_hour_resets_at=None,
    )


# ── Task 1: the record-usage write path ────────────────────────────────


@pytest.mark.parametrize("inactive_first", [True, False],
                         ids=["inactive-inserted-first", "active-inserted-first"])
def test_837_record_usage_writes_its_own_accounts_boundary(
        ns, monkeypatch, inactive_first):
    """A recorded observation carries the boundary ITS OWN account established.

    Account B captured earliest, so the account-blind query returns B's
    01:00 boundary for every account in the week. The stored row for active
    account A must carry A's 05:00 boundary instead — and must NOT fall back to
    the payload-derived 06:00 boundary, which would mean the canonical override
    stopped running rather than became account-scoped.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    a_key = _activate(A_UUID)
    b_key = _account_key(B_UUID)
    _seed_two_account_week(ns, a_key=a_key, b_key=b_key,
                           inactive_first=inactive_first)

    assert ns["cmd_record_usage"](_record_usage_args(percent=61.0)) == 0

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT id, account_key, week_start_at, week_end_at, weekly_percent "
            "FROM weekly_usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        inactive_rows = conn.execute(
            "SELECT week_start_at, week_end_at FROM weekly_usage_snapshots "
            "WHERE account_key = ?", (b_key,)).fetchall()
    finally:
        conn.close()

    assert row is not None, "record-usage wrote no snapshot row"
    assert row["account_key"] == a_key, (
        f"the new row is stamped {row['account_key']!r}, not the active "
        f"account {a_key!r}")
    assert (row["week_start_at"], row["week_end_at"]) == (A_WS_AT, A_WE_AT), (
        "record-usage stored the wrong week boundary for the active account.\n"
        f"  stored:            {row['week_start_at']} .. {row['week_end_at']}\n"
        f"  active account A:  {A_WS_AT} .. {A_WE_AT}   (expected)\n"
        f"  inactive acct B:   {B_WS_AT} .. {B_WE_AT}   (earliest capture — "
        "what the account-blind query returns)\n"
        f"  payload-derived:   {DERIVED_WS_AT} ..       (no canonical override)"
    )
    # The inactive account's own rows are untouched.
    assert [(r["week_start_at"], r["week_end_at"]) for r in inactive_rows] == [
        (B_WS_AT, B_WE_AT)]


def test_837_record_usage_establishes_its_own_boundary_when_it_has_none(
        ns, monkeypatch):
    """An account with no prior boundary for the week establishes its own.

    It must not inherit the other account's, which is what the account-blind
    read did.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    a_key = _activate(A_UUID)
    b_key = _account_key(B_UUID)
    conn = ns["open_db"]()
    try:
        _seed_snapshot(conn, account_key=b_key, captured=B_CAPTURED,
                       ws_at=B_WS_AT, we_at=B_WE_AT, pct=20.0)
    finally:
        conn.close()

    assert ns["cmd_record_usage"](_record_usage_args(percent=61.0)) == 0

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT account_key, week_start_at, week_end_at "
            "FROM weekly_usage_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["account_key"] == a_key
    assert row["week_start_at"] == DERIVED_WS_AT, (
        "an account with no established boundary must derive its own from "
        f"resets_at, not inherit {B_WS_AT!r} from the other account; got "
        f"{row['week_start_at']!r}")


# ── Task 2: the four remaining account-aware callers ───────────────────


def _seed_account(conn, account_key, *, email, label, provider="claude"):
    conn.execute(
        "INSERT OR REPLACE INTO accounts "
        "(account_key, provider, natural_id, email, label, plan_type, "
        " label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (account_key, provider, account_key, email, label, "max", "manual",
         B_CAPTURED, A_CAPTURED),
    )
    conn.commit()


A_EMAIL = "active-a@example.com"
B_EMAIL = "inactive-b@example.com"


def _seed_two_account_registry(ns, *, a_key, b_key):
    conn = ns["open_db"]()
    try:
        _seed_account(conn, a_key, email=A_EMAIL, label="Active-A")
        _seed_account(conn, b_key, email=B_EMAIL, label="Inactive-B")
    finally:
        conn.close()


@pytest.fixture
def two_account_week(ns, monkeypatch):
    """One week, two accounts, the INACTIVE one holding the earliest capture."""
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    a_key = _account_key(A_UUID)
    b_key = _account_key(B_UUID)
    _seed_two_account_week(ns, a_key=a_key, b_key=b_key)
    _seed_two_account_registry(ns, a_key=a_key, b_key=b_key)
    return a_key, b_key


def _boundary_failure(actual):
    return (
        f"  resolved:          {actual}\n"
        f"  active account A:  {A_WS_AT}   (expected)\n"
        f"  inactive acct B:   {B_WS_AT}   (earliest capture — what the "
        "account-blind query returns)")


@pytest.mark.parametrize("inactive_first", [True, False],
                         ids=["inactive-inserted-first", "active-inserted-first"])
def test_837_get_recent_weeks_forwards_its_account(ns, monkeypatch,
                                                   inactive_first):
    """`get_recent_weeks` accepts an `account_key` and did not forward it."""
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    a_key = _account_key(A_UUID)
    b_key = _account_key(B_UUID)
    _seed_two_account_week(ns, a_key=a_key, b_key=b_key,
                           inactive_first=inactive_first)

    conn = ns["open_db"]()
    try:
        refs = ns["get_recent_weeks"](conn, 8, account_key=a_key)
    finally:
        conn.close()

    assert [r.key for r in refs] == [WEEK_START_DATE]
    assert refs[0].week_start_at == A_WS_AT, (
        "get_recent_weeks resolved the week boundary across accounts.\n"
        + _boundary_failure(refs[0].week_start_at))
    assert refs[0].week_end_at == A_WE_AT


def test_837_report_resolves_its_scoped_accounts_boundary(
        ns, two_account_week, capsys):
    """`report --account` resolves the scoped account's current-week boundary.

    `cmd_report` selects an account-scoped latest usage row and then resolved
    that row's boundary account-blind, so the JSON `currentWeek` reported the
    earliest-capturing account's window.
    """
    a_key, _b_key = two_account_week
    args = ns["build_parser"]().parse_args(
        ["report", "--json", "--account", A_EMAIL])
    assert ns["cmd_report"](args) == 0
    payload = json.loads(capsys.readouterr().out)
    current = payload["currentWeek"]
    assert current["weekStartAt"] == A_WS_AT, (
        "report resolved the current week's boundary across accounts.\n"
        + _boundary_failure(current["weekStartAt"]))
    assert current["weekEndAt"] == A_WE_AT


def test_837_percent_breakdown_resolves_its_scoped_accounts_boundary(
        ns, two_account_week, capsys):
    """`percent-breakdown --account` renders the scoped account's window."""
    a_key, _b_key = two_account_week
    args = ns["build_parser"]().parse_args(
        ["percent-breakdown", "--json", "--account", A_EMAIL])
    assert ns["cmd_percent_breakdown"](args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["weekStartAt"] == A_WS_AT, (
        "percent-breakdown resolved the display window across accounts.\n"
        + _boundary_failure(payload["weekStartAt"]))
    assert payload["weekEndAt"] == A_WE_AT


def test_837_trend_view_resolves_its_scoped_accounts_boundary(
        ns, two_account_week):
    """The account-scoped trend view resolves BOTH of its boundary reads.

    Two assertions, because the site carries two omissions that fail at
    different times. The rendered row boundary fails today, because
    `_get_canonical_boundary_for_date` was account-blind. `is_current` fails
    against a partial fix that scopes that read and leaves the
    `_apply_reset_events_to_weekrefs` call beside it merged: the inactive
    account's in-place credit is seeded at the ACTIVE account's week end, so a
    merged applier splits the active account's current ref and the row stops
    matching. A scoped boundary followed by merged reset events is still a
    merged answer.
    """
    a_key, b_key = two_account_week
    conn = ns["open_db"]()
    try:
        # An in-place credit owned by the INACTIVE account, shaped
        # `old == effective` and ending at the ACTIVE account's week end.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, account_key) VALUES (?,?,?,?,?)",
            ("2026-06-17T00:00:00+00:00", "2026-06-17T00:00:00+00:00",
             A_WE_AT, "2026-06-17T00:00:00+00:00", b_key),
        )
        conn.commit()
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        view = ns["build_trend_view"](
            conn, now_utc=ns["_command_as_of"](), n=8, account_key=a_key)
    finally:
        conn.close()

    rows = [r for r in view.rows
            if r.week_start_date is not None
            and r.week_start_date.isoformat() == WEEK_START_DATE]
    assert rows, "the scoped trend view rendered no row for the seeded week"
    resolved = rows[-1].week_start_at.astimezone(dt.timezone.utc).isoformat()
    assert resolved == A_WS_AT, (
        "the trend view resolved the week boundary across accounts.\n"
        + _boundary_failure(resolved))
    assert rows[-1].is_current, (
        "the trend view's current-week probe applied ANOTHER account's reset "
        "events, so the scoped row no longer matched the resolved current "
        "week")


# ── Task 3: the call-site inventory, as a gate rather than an attestation ──

BOUNDARY_HELPER = "_get_canonical_boundary_for_date"

#: Call sites that hold an account and MUST forward it. `"<path>:<enclosing
#: function>"`. A site here whose caller has an account available but does not
#: pass it is a bug, not a classification.
ACCOUNT_FORWARDING_CALL_SITES = frozenset({
    "bin/_cctally_weekrefs.py:get_recent_weeks",
    "bin/_cctally_record.py:_usage_snapshot_columns",
    "bin/_cctally_record.py:_revalidate_credit_plan",
    "bin/_cctally_record.py:cmd_record_credit",
    "bin/_cctally_forecast.py:cmd_report",
    "bin/_cctally_percent_breakdown.py:cmd_percent_breakdown",
    "bin/_lib_view_models.py:build_trend_view",
})

#: Call sites that are DELIBERATELY merged because no account is in scope
#: there. Empty today: every call site in the tree reaches an account. A new
#: entry here needs a reason stating what account context the site lacks.
DELIBERATELY_MERGED_CALL_SITES: frozenset = frozenset()


class _BoundaryCallCollector(ast.NodeVisitor):
    """Record `"<path>:<enclosing function>"` for every call of the helper.

    Both call shapes count: the bare name (`_get_canonical_boundary_for_date(...)`,
    inside the defining module and through `record.py`'s `def`-shims) and the
    attribute form (`c._get_canonical_boundary_for_date(...)`, the cctally-namespace
    accessor every renderer uses).
    """

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.stack: list[str] = []
        self.sites: set[str] = set()
        self.shapes: set[str] = set()

    def _visit_scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_Call(self, node):
        func = node.func
        shape = None
        if isinstance(func, ast.Name) and func.id == BOUNDARY_HELPER:
            shape = "name"
        elif isinstance(func, ast.Attribute) and func.attr == BOUNDARY_HELPER:
            shape = "attribute"
        if shape is not None:
            enclosing = self.stack[-1] if self.stack else "<module>"
            # A `def _get_canonical_boundary_for_date(...)` whose body forwards
            # to the cctally namespace is a re-export shim, not a consumer that
            # resolves a boundary, so it has no account to classify.
            if enclosing != BOUNDARY_HELPER:
                self.sites.add(f"{self.rel_path}:{enclosing}")
                self.shapes.add(shape)
        self.generic_visit(node)


def _discover_boundary_call_sites():
    root = pathlib.Path(__file__).resolve().parents[1]
    bin_dir = root / "bin"
    sources = sorted(bin_dir.glob("*.py")) + [bin_dir / "cctally"]
    sites: set[str] = set()
    shapes: set[str] = set()
    for path in sources:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _BoundaryCallCollector(str(path.relative_to(root)))
        collector.visit(tree)
        sites |= collector.sites
        shapes |= collector.shapes
    return sites, shapes


def test_837_every_boundary_call_site_is_classified():
    """Every call of the boundary helper is declared, so the inventory cannot drift.

    #837's issue body named seven sites and the review found an eighth, which is
    why completeness is a test rather than an implementor's attestation. A new
    call site fails this test until it is declared — and a site that has an
    account available belongs in the forwarding set and is a bug to fix, never a
    classification to make.
    """
    discovered, shapes = _discover_boundary_call_sites()
    declared = ACCOUNT_FORWARDING_CALL_SITES | DELIBERATELY_MERGED_CALL_SITES

    # Non-vacuity: a collector that matched nothing, or that recognized only one
    # of the two call shapes, would pass an equality against a declaration it
    # also failed to populate.
    assert discovered, "the AST walk found no call site at all"
    assert shapes == {"name", "attribute"}, (
        f"the walk recognized only {sorted(shapes)}; both the bare-name and the "
        "cctally-namespace attribute form are live in this tree")

    undeclared = sorted(discovered - declared)
    stale = sorted(declared - discovered)
    assert not undeclared, (
        "unclassified call site(s) of "
        f"{BOUNDARY_HELPER}: {undeclared}. Read each one: if an account is in "
        "scope there, forward it and add it to ACCOUNT_FORWARDING_CALL_SITES; "
        "widening DELIBERATELY_MERGED_CALL_SITES to make this pass is how the "
        "inventory drifted in the first place.")
    assert not stale, (
        f"declared call site(s) that no longer exist: {stale}. Remove them, or "
        "the declaration stops describing the tree.")


# ── Task 5: every record-credit planning read, one mechanism per test ──
#
# One combined fixture cannot prove this, and the review showed why. The
# boundary query takes the EARLIEST row, so a newer competing boundary can
# never win it. An existing floor is preferred BEFORE the high-water mark is
# resolved, so seeding a competing floor suppresses the default-high-water
# branch entirely. And a completed floor can stop the command before it applies,
# while `--force` bypasses the completion classification. A single test
# asserting all of that would fail today for some reasons and not others, and a
# partial implementation could pass it.

A_WS_Z = "2026-06-13T05:00:00Z"
A_WE_Z = "2026-06-20T05:00:00Z"
B_WS_Z = "2026-06-13T01:00:00Z"

#: `--at`, and the instant every "current week" resolution is asked about.
CREDIT_AT = "2026-06-19T14:37:00Z"
#: A five-hour window still live at CREDIT_AT, so `_resolve_prior_5h` answers.
FIVE_HOUR_RESETS_AT = "2026-06-19T18:00:00+00:00"

#: B's EARLIEST capture owns the account-blind canonical boundary; B's LATEST
#: owns the account-blind current-week resolution and the account-blind
#: high-water mark. One fixture serves both because the two reads order
#: oppositely, so a single B row could only ever discriminate one of them.
B_EARLIEST = "2026-06-14T01:05:00Z"
A_OBSERVED = "2026-06-15T05:05:00Z"
B_LATEST = "2026-06-19T13:00:00Z"


def _rows_for(ns, account_key, table="weekly_usage_snapshots"):
    conn = ns["open_db"]()
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        select = ", ".join(cols)
        return conn.execute(
            f"SELECT {select} FROM {table} WHERE account_key = ? ORDER BY id",
            (account_key,)).fetchall()
    finally:
        conn.close()


def _seed_credit_week(ns, *, a_key, b_key, a_pct=46.0, b_pct=80.0,
                      insertion="inactive-first"):
    rows = [
        dict(account_key=b_key, captured=B_EARLIEST, ws_at=B_WS_AT,
             we_at=B_WE_AT, pct=12.0),
        dict(account_key=a_key, captured=A_OBSERVED, ws_at=A_WS_AT,
             we_at=A_WE_AT, pct=a_pct),
        dict(account_key=b_key, captured=B_LATEST, ws_at=B_WS_AT,
             we_at=B_WE_AT, pct=b_pct),
    ]
    if insertion != "inactive-first":
        rows.reverse()
    conn = ns["open_db"]()
    try:
        for row in rows:
            _seed_snapshot(conn, **row)
    finally:
        conn.close()


def _seed_floor(ns, account_key, *, effective, pre_credit,
                applied="2026-06-19T13:00:00Z"):
    conn = ns["open_db"]()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
            " applied_at_utc, account_key) VALUES (?,?,?,?,?)",
            (WEEK_START_DATE, effective, pre_credit, applied, account_key))
        _stamp_journal_id(conn, "weekly_credit_floors", int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()


def _seed_command_owned_synthetic(ns, account_key, *, captured, pct):
    conn = ns["open_db"]()
    try:
        _seed_snapshot(conn, account_key=account_key, captured=captured,
                       ws_at=A_WS_AT, we_at=A_WE_AT, pct=pct,
                       source="record-credit")
    finally:
        conn.close()


def _credit_args(**over):
    args = dict(to=31.0, from_pct=None, at=CREDIT_AT, week=WEEK_START_DATE,
                dry_run=True, yes=False, json=True, force=False)
    args.update(over)
    return argparse.Namespace(**args)


def _credit_preview(ns, capsys, **over):
    assert ns["cmd_record_credit"](_credit_args(**over)) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def credit_accounts(ns, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", CREDIT_AT)
    a_key = _activate(A_UUID)
    b_key = _account_key(B_UUID)
    _seed_two_account_registry(ns, a_key=a_key, b_key=b_key)
    return a_key, b_key


INSERTION_ORDERS = pytest.mark.parametrize(
    "insertion", ["inactive-first", "active-first"])


@INSERTION_ORDERS
@pytest.mark.parametrize("week", [WEEK_START_DATE, None],
                         ids=["explicit-week", "resolved-current-week"])
def test_837_boundary_selection_is_account_scoped(
        ns, credit_accounts, capsys, insertion, week):
    """The week the plan describes is the ACTIVE account's week.

    `--week` resolves through `_get_canonical_boundary_for_date`, which takes
    the EARLIEST established boundary, so B's earliest capture owned it. Without
    `--week` the resolver is `_fetch_current_week_snapshots`, which takes the
    LATEST capture in the containing window, so B's latest capture owned that
    one. The two order oppositely, which is why B holds a row at each end.
    """
    a_key, b_key = credit_accounts
    _seed_credit_week(ns, a_key=a_key, b_key=b_key, insertion=insertion)
    before = _rows_for(ns, b_key)

    payload = _credit_preview(ns, capsys, week=week, from_pct=46.0)
    assert payload["week"]["weekStartAt"] == A_WS_Z, (
        "record-credit planned against another account's week.\n"
        f"  planned:           {payload['week']['weekStartAt']}\n"
        f"  active account A:  {A_WS_Z}   (expected)\n"
        f"  inactive acct B:   {B_WS_Z}")
    assert payload["week"]["weekEndAt"] == A_WE_Z
    assert _rows_for(ns, b_key) == before


@INSERTION_ORDERS
def test_837_hwm_default_is_account_scoped(ns, credit_accounts, capsys,
                                           insertion):
    """With NO floor present, the `--from` default is the ACTIVE account's HWM.

    No floor is seeded on purpose: an existing floor is preferred before the
    high-water mark is resolved, so a fixture carrying one suppresses this
    branch entirely and the test would prove nothing about it.
    """
    a_key, b_key = credit_accounts
    _seed_credit_week(ns, a_key=a_key, b_key=b_key, a_pct=46.0, b_pct=80.0,
                      insertion=insertion)
    before = _rows_for(ns, b_key)

    payload = _credit_preview(ns, capsys)
    assert payload["credit"]["fromSource"] == "hwm", (
        "the default-high-water branch was not reached, so this test says "
        f"nothing about it: {payload['credit']!r}")
    assert payload["credit"]["fromPct"] == 46.0, (
        "the `--from` default came from the merged high-water mark.\n"
        "  active account A's max:    46.0   (expected)\n"
        "  inactive account B's max:  80.0\n"
        f"  resolved:                  {payload['credit']['fromPct']}")
    assert _rows_for(ns, b_key) == before


@INSERTION_ORDERS
def test_837_floor_selection_is_account_scoped(ns, credit_accounts, capsys,
                                               insertion):
    """The prior-credit baseline is the ACTIVE account's floor, not the newest.

    B's floor is deliberately NEWER, so the merged `ORDER BY
    unixepoch(effective_at_utc) DESC, id DESC LIMIT 1` prefers it.
    """
    a_key, b_key = credit_accounts
    _seed_credit_week(ns, a_key=a_key, b_key=b_key, insertion=insertion)
    _seed_floor(ns, a_key, effective="2026-06-19T10:00:00+00:00",
                pre_credit=52.0)
    _seed_floor(ns, b_key, effective="2026-06-19T12:00:00+00:00",
                pre_credit=77.0)
    before = _rows_for(ns, b_key, table="weekly_credit_floors")

    payload = _credit_preview(ns, capsys)
    assert payload["credit"]["fromSource"] == "prior_credit"
    assert payload["credit"]["fromPct"] == 52.0, (
        "the prior-credit baseline came from another account's floor.\n"
        "  active account A's floor:    52.0   (expected)\n"
        "  inactive account B's floor:  77.0   (newer — what the merged "
        "read prefers)\n"
        f"  resolved:                    {payload['credit']['fromPct']}")
    assert _rows_for(ns, b_key, table="weekly_credit_floors") == before


def _seed_completion_state(ns, a_key, b_key):
    """A's floor is half-applied; B's week already carries a command-owned
    synthetic AFTER A's effective.

    A's floor is the NEWER of the two, so the merged latest-floor read picks
    A's and the only thing left to differ is the completion probe. Merged, that
    probe finds B's synthetic and classifies A's floor as fully applied, which
    refuses the command; scoped, A owns no synthetic and the credit completes.
    """
    _seed_credit_week(ns, a_key=a_key, b_key=b_key)
    _seed_floor(ns, b_key, effective="2026-06-19T10:00:00+00:00",
                pre_credit=77.0)
    _seed_floor(ns, a_key, effective="2026-06-19T12:00:00+00:00",
                pre_credit=46.0)
    _seed_command_owned_synthetic(ns, b_key, captured="2026-06-19T13:30:00Z",
                                 pct=9.0)


def test_837_completion_classification_is_account_scoped(ns, credit_accounts):
    """Another account's completed credit must not refuse this account's."""
    a_key, b_key = credit_accounts
    _seed_completion_state(ns, a_key, b_key)
    before_snapshots = _rows_for(ns, b_key)
    before_floors = _rows_for(ns, b_key, table="weekly_credit_floors")

    rc = ns["cmd_record_credit"](
        _credit_args(dry_run=False, yes=True, json=False))
    assert rc == 0, (
        "the merged completion probe found the INACTIVE account's "
        "command-owned synthetic and refused the active account's half-applied "
        f"credit (exit {rc})")

    conn = ns["open_db"]()
    try:
        synthetic = conn.execute(
            "SELECT account_key, weekly_percent FROM weekly_usage_snapshots "
            "WHERE source='record-credit' AND account_key = ?",
            (a_key,)).fetchone()
    finally:
        conn.close()
    assert synthetic is not None and float(synthetic[1]) == 31.0
    assert _rows_for(ns, b_key) == before_snapshots
    assert _rows_for(ns, b_key, table="weekly_credit_floors") == before_floors


def test_837_force_bypasses_completion_not_account_scope(ns, credit_accounts):
    """`--force` bypasses the completion classification, never the account.

    The forced path clears this week's command-owned synthetics and floors
    before re-recording. Scoped, it clears only the ACTIVE account's; every
    inactive-account row must still be byte-identical afterwards.
    """
    a_key, b_key = credit_accounts
    _seed_completion_state(ns, a_key, b_key)
    _seed_command_owned_synthetic(ns, a_key, captured="2026-06-19T13:40:00Z",
                                 pct=28.0)
    before_snapshots = _rows_for(ns, b_key)
    before_floors = _rows_for(ns, b_key, table="weekly_credit_floors")

    rc = ns["cmd_record_credit"](
        _credit_args(dry_run=False, yes=True, json=False, force=True))
    assert rc == 0, f"--force did not re-record the credit (exit {rc})"

    assert _rows_for(ns, b_key) == before_snapshots, (
        "the forced clear removed rows belonging to the inactive account")
    assert _rows_for(ns, b_key, table="weekly_credit_floors") == before_floors


def test_837_prior_five_hour_is_account_scoped(ns, credit_accounts):
    """The synthetic carries the ACTIVE account's five-hour evidence.

    `_resolve_prior_5h` takes the most recent snapshot carrying a live
    five-hour window, so the inactive account's newer reading owned it.
    """
    a_key, b_key = credit_accounts
    _seed_credit_week(ns, a_key=a_key, b_key=b_key)
    conn = ns["open_db"]()
    try:
        for account_key, captured, fhp in (
                (a_key, "2026-06-19T13:30:00Z", 22.0),
                (b_key, "2026-06-19T14:00:00Z", 88.0)):
            cur = conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, page_url, "
                " source, payload_json, five_hour_percent, "
                " five_hour_resets_at, five_hour_window_key, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (captured, WEEK_START_DATE, WEEK_END_DATE,
                 A_WS_AT if account_key == a_key else B_WS_AT,
                 A_WE_AT if account_key == a_key else B_WE_AT,
                 20.0, None, "userscript", "{}", fhp,
                 FIVE_HOUR_RESETS_AT, 1781884800, account_key),
            )
            _stamp_journal_id(conn, "weekly_usage_snapshots",
                              int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()
    before = _rows_for(ns, b_key)

    rc = ns["cmd_record_credit"](
        _credit_args(dry_run=False, yes=True, json=False, from_pct=46.0))
    assert rc == 0

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT five_hour_percent FROM weekly_usage_snapshots "
            "WHERE source='record-credit' AND account_key = ?",
            (a_key,)).fetchone()
    finally:
        conn.close()
    assert row is not None, "no command-owned synthetic was written"
    assert row[0] == 22.0, (
        "the synthetic carried another account's five-hour evidence.\n"
        "  active account A's reading:    22.0   (expected)\n"
        "  inactive account B's reading:  88.0   (newer — what the merged "
        "read takes)\n"
        f"  resolved:                      {row[0]}")
    assert _rows_for(ns, b_key) == before


# ── Task 6: the preview and the application describe ONE population ────

STALE_REPLICA_AT = "2026-06-19T14:10:00Z"
HELD_REPLICA_AT = "2026-06-19T14:20:00Z"
#: `--from`, and the centre of the 1.0pp stale-replay band.
CREDIT_FROM_PCT = 46.0


def _all_snapshot_ids(ns):
    conn = ns["open_db"]()
    try:
        return {int(r[0]) for r in conn.execute(
            "SELECT id FROM weekly_usage_snapshots")}
    finally:
        conn.close()


def test_837_preview_and_apply_delete_the_same_rows(ns, credit_accounts,
                                                    capsys):
    """The previewed stale population and the deleted rows are the same rows.

    Asserted on row IDENTIFIERS, never on counts: equal counts over different
    rows is exactly the failure this test exists to catch, and it is reachable
    because `_count_stale_replays` counted across every account while the
    DELETE has always been account-scoped. Both accounts hold a replica inside
    the band and a held row, so a count alone would read 2 while the DELETE
    removed 1.
    """
    a_key, b_key = credit_accounts
    _seed_credit_week(ns, a_key=a_key, b_key=b_key)
    conn = ns["open_db"]()
    try:
        for account_key, ws_at, we_at in ((a_key, A_WS_AT, A_WE_AT),
                                          (b_key, B_WS_AT, B_WE_AT)):
            _seed_snapshot(conn, account_key=account_key,
                           captured=STALE_REPLICA_AT, ws_at=ws_at, we_at=we_at,
                           pct=CREDIT_FROM_PCT)
            _seed_snapshot(conn, account_key=account_key,
                           captured=HELD_REPLICA_AT, ws_at=ws_at, we_at=we_at,
                           pct=CREDIT_FROM_PCT, held=1)
    finally:
        conn.close()

    # The count the preview REPORTS, which is the number a person reads before
    # authorizing the mutation. It counted across every account while the
    # DELETE has always been account-scoped, so it over-reported by exactly the
    # inactive account's in-band rows.
    preview = _credit_preview(ns, capsys, from_pct=CREDIT_FROM_PCT)
    assert preview["actions"]["staleReplaysDeleted"] == 1, (
        "the preview reported a stale population the DELETE will not remove: "
        f"{preview['actions']['staleReplaysDeleted']} rows, but only the "
        "active account's single in-band row is eligible")

    # The population the preview describes, enumerated through the same entry
    # point the command uses.
    conn = ns["open_db"]()
    try:
        plan = ns["_build_credit_plan"](
            week_start_date=WEEK_START_DATE, week_start_at=A_WS_AT,
            week_end_at=A_WE_AT, from_pct=CREDIT_FROM_PCT, from_source="explicit",
            to_pct=31.0,
            at_dt=dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc),
            now=dt.datetime(2026, 6, 19, 14, 37, tzinfo=dt.timezone.utc),
        )
        previewed = ns["_stale_replay_candidates"](
            conn, plan, account_key=a_key)
        held_ids = {int(r[0]) for r in conn.execute(
            "SELECT id FROM weekly_usage_snapshots "
            "WHERE weekly_observation_held = 1")}
        inactive_ids = {int(r[0]) for r in conn.execute(
            "SELECT id FROM weekly_usage_snapshots WHERE account_key = ?",
            (b_key,))}
    finally:
        conn.close()

    assert len(previewed) == 1, (
        "the fixture must place exactly ONE active-account row in the band, or "
        f"the identity comparison below proves nothing: {previewed!r}")
    before = _all_snapshot_ids(ns)

    rc = ns["cmd_record_credit"](_credit_args(
        dry_run=False, yes=True, json=False, from_pct=CREDIT_FROM_PCT))
    assert rc == 0

    after = _all_snapshot_ids(ns)
    deleted = before - after
    assert deleted == set(previewed), (
        "the applied DELETE removed a different set of rows from the one the "
        f"preview enumerated.\n  previewed: {sorted(previewed)}\n"
        f"  deleted:   {sorted(deleted)}")
    assert not (deleted & held_ids), (
        "a held row was deleted; the held-row exclusion must be shared by the "
        "count, the enumeration and the deletion")
    assert not (deleted & inactive_ids), (
        "a row belonging to the inactive account was deleted")


def test_837_refusal_names_the_account_that_holds_the_week(
        ns, credit_accounts, capsys):
    """A week held only by another account refuses with THAT fact.

    Scoping the boundary read to the resolved account made an empty result
    ambiguous, because the week may hold no rows at all or may hold rows this
    account does not own, and those need different actions from the reader. The
    state is reachable: an identity that had no `~/.claude.json` when the
    journal cutover ran holds sentinel history and then logs in.
    """
    a_key, b_key = credit_accounts
    conn = ns["open_db"]()
    try:
        _seed_snapshot(conn, account_key=b_key, captured=B_EARLIEST,
                       ws_at=B_WS_AT, we_at=B_WE_AT, pct=12.0)
        _seed_snapshot(conn, account_key=b_key, captured=B_LATEST,
                       ws_at=B_WS_AT, we_at=B_WE_AT, pct=80.0)
    finally:
        conn.close()

    assert ns["cmd_record_credit"](_credit_args()) == 2
    err = capsys.readouterr().err
    assert b_key in err, (
        "the refusal did not name the account that actually holds the week, so "
        "the reader is told it is empty when it is not.\n"
        f"  stderr: {err!r}")
    assert "another account's rows" in err, (
        f"the refusal did not say why the rows are not usable: {err!r}")


def test_837_refusal_over_a_genuinely_empty_week_stays_bare(
        ns, credit_accounts, capsys):
    """No row anywhere in the week keeps the original message.

    This is the counterexample for the test above. Without it, a diagnostic
    that fired unconditionally would pass that test while telling every reader
    of an empty week that some other account holds it.
    """
    a_key, b_key = credit_accounts

    assert ns["cmd_record_credit"](_credit_args()) == 2
    err = capsys.readouterr().err
    assert err.strip() == (
        f"record-credit: no snapshot for --week {WEEK_START_DATE}"), (
        f"an empty week no longer reports a plain absence: {err!r}")
    assert b_key not in err
