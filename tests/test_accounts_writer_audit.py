"""Structural writer-audit for the account dimension (#341 Task 1, Step 8 item 9).

rev 4.1 makes the real-account families carry ``account_key TEXT NOT NULL
DEFAULT 'unattributed'`` as a **defensive backstop only** — every production
writer MUST pass the key explicitly. This test enforces that: it source-scans
the live-ingest attribution write chain and fails if any ``INSERT``/upsert into a
real-account family omits ``account_key`` from its column list (i.e. silently
relies on the schema DEFAULT). A future writer that forgets the stamp trips it.

Scope (the observe-and-stamp origination writers):
  bin/_cctally_record.py, bin/_cctally_milestones.py, bin/_cctally_five_hour.py,
  bin/_cctally_weekrefs.py, bin/_cctally_quota.py (the Codex quota subsystem —
  #341 Task 2/3: every quota real-account INSERT stamps account_key).

Deliberately EXCLUDED:
  * bin/_cctally_db.py — frozen migration / rebuild-copy handlers that run on a
    PRE-epoch stats.db; their output is superseded by the epoch-1001 rebuild,
    which re-derives account attribution from the journal (spec §2). Adding
    account_key to a frozen migration handler risks its per-migration golden.
  * bin/_cctally_journal.py — the generic fold writers use
    ``_insert_or_ignore(conn, spec.table, cols)`` where ``account_key`` rides in
    ``cols`` from the evt payload (two-shaped stamp), so there is no literal
    per-table INSERT to audit; the ``_apply_op_weekly_credit_floor`` dict-insert
    is covered by tests/test_accounts_journal.py.
  * build-*-fixtures.py / cctally-*-test — fixture/test-side (out of scope).
"""
from __future__ import annotations

import pathlib
import re

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

REAL_ACCOUNT_TABLES = frozenset({
    "weekly_usage_snapshots", "weekly_cost_snapshots", "percent_milestones",
    "week_reset_events", "five_hour_reset_events", "five_hour_blocks",
    "five_hour_milestones", "five_hour_block_models", "five_hour_block_projects",
    "weekly_credit_floors",
    # Codex quota real-account families (#341 Task 2/3, spec §2): each carries
    # account_key in its (source_root_key, account_key, …)-qualified UNIQUE.
    "quota_window_blocks", "quota_percent_milestones", "quota_threshold_events",
    "quota_alert_arming", "quota_projection_state",
})

AUDITED_MODULES = (
    "_cctally_record.py", "_cctally_milestones.py", "_cctally_five_hour.py",
    "_cctally_weekrefs.py", "_cctally_quota.py",
)

# `INSERT [OR IGNORE|OR REPLACE|...] INTO <table> (` — the start of a
# column-list INSERT. The paren may sit on the next line, and Python string
# concatenation puts quote/whitespace seams between the table name and the '('
# (e.g. ``"INSERT OR IGNORE INTO week_reset_events " "(detected_at_utc, ...)"``),
# so tolerate whitespace AND quote chars there.
_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)[\s\"']*\(", re.IGNORECASE)

_ACCOUNT_KEY_TOKEN = re.compile(r"\baccount_key\b")


def _balanced_paren_body(src: str, open_idx: int) -> str:
    """Return the text between the '(' at ``open_idx`` and its matching ')'."""
    depth = 0
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i]
    return src[open_idx + 1:]


def test_every_real_account_insert_stamps_account_key():
    findings: list[str] = []
    covered: set[str] = set()
    for modname in AUDITED_MODULES:
        src = (BIN / modname).read_text()
        for m in _INSERT_RE.finditer(src):
            table = m.group(1)
            if table not in REAL_ACCOUNT_TABLES:
                continue
            covered.add(table)
            open_idx = m.end() - 1  # index of the '(' the regex consumed
            collist = _balanced_paren_body(src, open_idx)
            line = src.count("\n", 0, m.start()) + 1
            if "{" in collist:
                # Dynamic ``INSERT INTO t ({colnames})`` form: the column list is
                # built from a Python ``columns``/``cols`` dict. Require that dict
                # (within the enclosing function, scanned as a window before the
                # INSERT) to carry an explicit "account_key" entry.
                window = src[max(0, m.start() - 2500):m.start()]
                ok = ('"account_key"' in window) or ("'account_key'" in window)
            else:
                ok = _ACCOUNT_KEY_TOKEN.search(collist) is not None
            if not ok:
                findings.append(
                    f"{modname}:{line} INSERT INTO {table} omits account_key")
    assert not findings, (
        "real-account-family INSERT(s) rely on the schema DEFAULT instead of "
        "stamping account_key explicitly (rev 4.1 forbids this):\n  "
        + "\n  ".join(findings))
    # Non-vacuity: the audit must have actually SEEN a writer for every
    # real-account table — else a drifted module/table set passes vacuously.
    missing = REAL_ACCOUNT_TABLES - covered
    assert not missing, (
        "writer-audit found no INSERT for real-account table(s) "
        f"{sorted(missing)} in the audited modules — the audit scope drifted "
        "(a writer moved, or the table set is stale)")


def test_audit_is_non_vacuous_when_account_key_removed(tmp_path):
    """Prove the scanner is non-vacuous: a synthetic INSERT that omits
    account_key must be flagged (guards against a regex that never matches)."""
    good = "INSERT INTO percent_milestones (captured_at_utc, account_key) VALUES (?, ?)"
    bad = "INSERT OR IGNORE INTO five_hour_blocks (five_hour_window_key) VALUES (?)"
    for m in _INSERT_RE.finditer(good):
        body = _balanced_paren_body(good, m.end() - 1)
        assert _ACCOUNT_KEY_TOKEN.search(body) is not None
    hits = list(_INSERT_RE.finditer(bad))
    assert hits, "regex must match a real INSERT statement"
    body = _balanced_paren_body(bad, hits[0].end() - 1)
    assert _ACCOUNT_KEY_TOKEN.search(body) is None, (
        "scanner must detect a missing account_key column")
