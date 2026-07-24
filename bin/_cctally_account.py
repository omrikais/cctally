"""``cctally account`` subcommand + the account-decoration helpers (#341, spec §3).

Read surface over the ``accounts`` registry (derived from the journal by the
Task-1 rebuild appliers) plus the providers' own on-disk active-account state:

  * ``list [--json]``  — every observed account with label / provider / email /
    plan / first-last-seen and a live ``active`` marker per provider.
  * ``show <ref> [--json]`` — one account's identity + a small attribution
    summary (usage-snapshot / percent-milestone counts scoped to the key).
  * ``label <ref> <name>`` — append an ``account_label`` op (user provenance,
    top of the ``user > switcher > auto`` precedence) and run an authoritative
    ingest so the rename folds durably (survives ``db rebuild --db stats``).

Ref resolution is the pure ``_lib_accounts.resolve_account_ref`` (case-insensitive
label -> email -> unique key prefix; literal ``unattributed`` accepted); an
ambiguous/unknown ref raises ``AccountRefError`` -> exit 2 with candidates on
stderr (native-usage error per ``docs/cli-contract.md``).

This module is ALSO the single home for the R8 decoration gate + label helpers
consumed by the alerts log/label prefix, the ``--account`` render decoration,
the doctor ``accounts.*`` legs, and (Task 4) the dashboard read model:
``real_account_count`` / ``provider_is_decorated`` / ``account_label`` /
``resolve_active_account_keys``. Keeping them here means the ">1 real account"
trigger and the key->label map have exactly one definition.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import _cctally_core
import _lib_accounts


def _cctally():
    import sys
    return sys.modules["cctally"]


def eprint(*args, **kwargs):
    import sys
    print(*args, file=sys.stderr, **kwargs)


# --------------------------------------------------------------------------
# registry read helpers (shared decoration surface)
# --------------------------------------------------------------------------

_ACCOUNT_COLUMNS = (
    "account_key", "provider", "natural_id", "email", "label", "plan_type",
    "label_source", "first_seen_utc", "last_seen_utc",
)


def load_accounts(conn, provider: "str | None" = None) -> "list[dict]":
    """Every registry row (optionally one provider), deterministically ordered
    by ``(provider, first_seen_utc, account_key)`` so renders are stable."""
    sql = (
        "SELECT account_key, provider, natural_id, email, label, plan_type, "
        "label_source, first_seen_utc, last_seen_utc FROM accounts"
    )
    params: tuple = ()
    if provider is not None:
        sql += " WHERE provider = ?"
        params = (provider,)
    sql += " ORDER BY provider, first_seen_utc, account_key"
    return [
        {col: row[i] for i, col in enumerate(_ACCOUNT_COLUMNS)}
        for row in conn.execute(sql, params).fetchall()
    ]


def real_account_count(conn, provider: str) -> int:
    """Number of REAL accounts for one provider (excludes the ``unattributed``
    sentinel). The R8 decoration trigger: ``> 1`` means the provider decorates."""
    row = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE provider = ? AND account_key != ?",
        (provider, _lib_accounts.UNATTRIBUTED),
    ).fetchone()
    return int(row[0]) if row else 0


def provider_is_decorated(conn, provider: str) -> bool:
    """R8 gate: does this provider render account decoration? True iff it holds
    more than one REAL account (a lone ``unattributed`` bucket never triggers)."""
    return real_account_count(conn, provider) > 1


def account_label(conn, account_key: str) -> str:
    """A human label for ``account_key``: the registry label, else the email,
    else an 8-char key prefix. The reserved sentinels render as words so alert
    prefixes / renders read cleanly."""
    if account_key == _lib_accounts.UNATTRIBUTED:
        return "Unattributed"
    if account_key == _lib_accounts.VENDOR_WIDE:
        return "All accounts"
    row = conn.execute(
        "SELECT label, email FROM accounts WHERE account_key = ?", (account_key,)
    ).fetchone()
    if row is not None:
        if row[0]:
            return row[0]
        if row[1]:
            return row[1]
    return account_key[:8]


def resolve_account_filter(args, provider: str = "claude", *,
                           needs_cache: bool = False) -> "tuple[str | None, int | None]":
    """Resolve the ``--account <ref>`` render filter (#341, spec §3) to an
    ``account_key``.

    Returns ``(account_key | None, exit_code | None)``:
      * no ``--account`` flag        -> ``(None, None)`` (merged view, today's
        byte-identical output — R8);
      * resolved ref                 -> ``(key, None)``;
      * ambiguous/unknown ref        -> ``(None, 2)`` (candidates on stderr, a
        native-usage error per ``docs/cli-contract.md``);
      * ``needs_cache`` + cache down -> ``(None, 3)`` — the stamped-entry family
        (``daily``/``session``/…) fails closed when the entry cache is
        unavailable, because the direct-JSONL fallback carries NO account
        identity and must never be stamped with the current login at read time.

    ``provider`` scopes ref resolution to one provider's registry (``claude`` for
    the Claude usage/analytics family, ``codex`` for ``codex quota``)."""
    ref = getattr(args, "account", None)
    if ref is None:
        return (None, None)
    conn = _cctally_core.open_db()
    try:
        try:
            key = _lib_accounts.resolve_account_ref(conn, ref, provider)
        except _lib_accounts.AccountRefError as exc:
            eprint(f"account: --account {ref!r} is ambiguous or unknown")
            if exc.candidates:
                eprint("candidates:")
                for cand in exc.candidates:
                    eprint(f"  {cand}")
            return (None, 2)
    finally:
        conn.close()
    if needs_cache:
        try:
            import _cctally_cache
            _cctally_cache.open_cache_db().close()
        except Exception:
            eprint("account attribution unavailable (cache required)")
            return (None, 3)
    return (key, None)


def account_json_fields(account_key: "str | None") -> dict:
    """R8 JSON decoration for an account-aware invocation (#341, spec §3).

    Returns ``{"accountKey": <key>, "accountLabel": <label>}`` for a resolved
    ``--account`` key, else ``{}``. Emitted only under an explicitly account-aware
    invocation (``--account`` set), so a default (no-flag) render stays
    byte-identical (R8). camelCase + additive; no ``schemaVersion`` bump."""
    if account_key is None:
        return {}
    conn = _cctally_core.open_db()
    try:
        label = account_label(conn, account_key)
    finally:
        conn.close()
    return {"accountKey": account_key, "accountLabel": label}


def resolve_active_account_keys() -> "set[str]":
    """The set of account keys that are CURRENTLY active per the providers' own
    on-disk credential state (never guessed). Claude from ``~/.claude.json``;
    Codex from each provider root's ``auth.json``. Absent / api-key / torn reads
    contribute nothing. Read-only, best-effort — any failure yields an empty
    contribution rather than raising into a render path."""
    active: "set[str]" = set()
    try:
        claude = _cctally_core._resolve_active_claude_account()
        if claude and claude != _lib_accounts.UNATTRIBUTED:
            active.add(claude)
    except Exception:
        pass
    try:
        import _cctally_cache
        for root in _cctally_cache._codex_provider_roots():
            res = _cctally_cache._resolve_codex_account_for_root(root.provider_root)
            if getattr(res, "status", None) == "identified" and res.account_key:
                active.add(res.account_key)
    except Exception:
        pass
    return active


# --------------------------------------------------------------------------
# small deterministic table renderer (content-sized columns)
# --------------------------------------------------------------------------

def _render_table(headers: "list[str]", rows: "list[list[str]]") -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def _fmt(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    out = [_fmt(headers)]
    for row in rows:
        out.append(_fmt(row))
    return "\n".join(out)


def _dash(value: "str | None") -> str:
    return value if value else "-"


def _date_only(iso: "str | None") -> str:
    if not iso:
        return "-"
    return iso[:10]


# --------------------------------------------------------------------------
# command dispatch
# --------------------------------------------------------------------------

def cmd_account(args: argparse.Namespace) -> int:
    action = getattr(args, "account_action", None)
    if action == "list":
        return _cmd_account_list(args)
    if action == "show":
        return _cmd_account_show(args)
    if action == "label":
        return _cmd_account_label(args)
    eprint("account: unknown action")
    return 2


def _cmd_account_list(args: argparse.Namespace) -> int:
    conn = _cctally_core.open_db()
    try:
        accounts = load_accounts(conn)
    finally:
        conn.close()
    active = resolve_active_account_keys()
    if getattr(args, "emit_json", False):
        payload = {
            "accounts": [
                {
                    "accountKey": a["account_key"],
                    "provider": a["provider"],
                    "label": a["label"],
                    "email": a["email"],
                    "planType": a["plan_type"],
                    "labelSource": a["label_source"],
                    "firstSeenUtc": a["first_seen_utc"],
                    "lastSeenUtc": a["last_seen_utc"],
                    "active": a["account_key"] in active,
                }
                for a in accounts
            ]
        }
        print(json.dumps(_cctally().stamp_schema_version(payload)))
        return 0

    if not accounts:
        print("No accounts observed yet.")
        return 0
    headers = ["PROVIDER", "LABEL", "EMAIL", "PLAN", "FIRST SEEN",
               "LAST SEEN", "ACTIVE"]
    rows = []
    for a in accounts:
        rows.append([
            a["provider"] or "-",
            account_label_from_row(a),
            _dash(a["email"]),
            _dash(a["plan_type"]),
            _date_only(a["first_seen_utc"]),
            _date_only(a["last_seen_utc"]),
            "*" if a["account_key"] in active else "",
        ])
    print(_render_table(headers, rows))
    return 0


def account_label_from_row(a: dict) -> str:
    """Label for a loaded registry row without a second DB round-trip."""
    if a["label"]:
        return a["label"]
    if a["email"]:
        return a["email"]
    return (a["account_key"] or "")[:8] or "-"


def _resolve_ref_or_exit(conn, ref: str) -> "str | None":
    """Resolve a ref, printing candidates + returning None on error (exit 2)."""
    try:
        return _lib_accounts.resolve_account_ref(conn, ref)
    except _lib_accounts.AccountRefError as exc:
        eprint(f"account: ref {ref!r} is ambiguous or unknown")
        if exc.candidates:
            eprint("candidates:")
            for cand in exc.candidates:
                eprint(f"  {cand}")
        return None


def _cmd_account_show(args: argparse.Namespace) -> int:
    ref = getattr(args, "ref", None)
    conn = _cctally_core.open_db()
    try:
        key = _resolve_ref_or_exit(conn, ref)
        if key is None:
            return 2
        row = conn.execute(
            "SELECT account_key, provider, natural_id, email, label, plan_type, "
            "label_source, first_seen_utc, last_seen_utc FROM accounts "
            "WHERE account_key = ?", (key,)
        ).fetchone()
        a = ({col: row[i] for i, col in enumerate(_ACCOUNT_COLUMNS)}
             if row is not None else None)
        snap_count = _count_scoped(conn, "weekly_usage_snapshots", key)
        milestone_count = _count_scoped(conn, "percent_milestones", key)
    finally:
        conn.close()
    active = resolve_active_account_keys()
    is_active = key in active
    if getattr(args, "emit_json", False):
        payload = {
            "accountKey": key,
            "provider": (a["provider"] if a else None),
            "label": (a["label"] if a else None),
            "email": (a["email"] if a else None),
            "planType": (a["plan_type"] if a else None),
            "labelSource": (a["label_source"] if a else None),
            "firstSeenUtc": (a["first_seen_utc"] if a else None),
            "lastSeenUtc": (a["last_seen_utc"] if a else None),
            "active": is_active,
            "attribution": {
                "usageSnapshots": snap_count,
                "percentMilestones": milestone_count,
            },
        }
        print(json.dumps(_cctally().stamp_schema_version(payload)))
        return 0

    label = (account_label_from_row(a) if a else
             ("Unattributed" if key == _lib_accounts.UNATTRIBUTED else key[:8]))
    lines = [
        f"Account:    {label}",
        f"Key:        {key}",
        f"Provider:   {_dash(a['provider'] if a else None)}",
        f"Email:      {_dash(a['email'] if a else None)}",
        f"Plan:       {_dash(a['plan_type'] if a else None)}",
        f"Label from: {_dash(a['label_source'] if a else None)}",
        f"First seen: {_dash(a['first_seen_utc'] if a else None)}",
        f"Last seen:  {_dash(a['last_seen_utc'] if a else None)}",
        f"Active:     {'yes' if is_active else 'no'}",
        f"Attribution: {snap_count} usage snapshot(s), "
        f"{milestone_count} percent milestone(s)",
    ]
    print("\n".join(lines))
    return 0


def _count_scoped(conn, table: str, account_key: str) -> int:
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_key = ?", (account_key,)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return 0


def _cmd_account_label(args: argparse.Namespace) -> int:
    ref = getattr(args, "ref", None)
    label = getattr(args, "label", None)
    conn = _cctally_core.open_db()
    try:
        key = _resolve_ref_or_exit(conn, ref)
        if key is None:
            return 2
        provider_row = conn.execute(
            "SELECT provider FROM accounts WHERE account_key = ?", (key,)
        ).fetchone()
        provider = provider_row[0] if provider_row is not None else None
    finally:
        conn.close()

    import _cctally_journal as _jr
    import _lib_journal as _lj
    at = (_cctally_core._command_as_of()
          .isoformat(timespec="seconds").replace("+00:00", "Z"))
    _jr.append_record(_lj.make_account_label(
        at=at, account_key=key, label=label, provider=provider))
    _jr.run_stats_ingest(mode="authoritative")
    print(f"Labeled {key[:8]} -> {label}")
    return 0
