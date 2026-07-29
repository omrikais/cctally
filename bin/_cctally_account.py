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


def _base_label_for_row(row: dict) -> str:
    """The undecorated label: manual label, else email, else key prefix.

    `cctally account label` sits at the top of the `user > switcher > auto`
    precedence, so a manual label is the BASE the collision pass decorates — it
    is never overridden by the email.
    """
    if row.get("label"):
        return str(row["label"])
    if row.get("email"):
        return str(row["email"])
    return str(row.get("account_key") or "")[:8] or "-"


def display_label_map_from_rows(rows: "list[dict]") -> "dict[str, str]":
    """Collision-only display labels for one provider's population (#416 §6).

    Collision handling CANNOT live in `account_label` / `account_label_from_row`:
    both are scalar — one row, no population and no plan context — so neither can
    know that its label is shared. This map sees the whole provider population,
    which is the only place the question is answerable.

    Decision D5: auto-disambiguate ONLY on collision. A label nobody else shares
    comes back untouched, so a single-account install and every existing golden
    are unaffected. Two Codex accounts really do auto-label as one email (the
    `pro` and one `team` account share it; `account_key` correctly differs
    because it derives from `chatgpt_account_id + email`), and that is the case
    this exists for.

    The discriminator is the PLAN where the plan separates the tied accounts, and
    a key prefix where it does not — a group of two `team` accounts on one email
    is exactly the case D1 says no heuristic can separate, so the fallback is the
    one thing that IS unique. Disambiguation is per sub-group, not
    all-or-nothing: the `pro` account in such a group still gets the readable
    plan discriminator while its two `team` siblings get prefixes.

    Collision detection is CASE-INSENSITIVE because `resolve_account_ref`
    resolves labels and emails case-insensitively — two labels differing only in
    case are one ambiguous ref, so they are one collision here too.

    The result is injective by construction: `account_key` prefixes are the
    terminal discriminator and keys are distinct.
    """
    by_base: "dict[str, list[dict]]" = {}
    for row in rows:
        key = str(row.get("account_key") or "")
        if not key or key in (_lib_accounts.UNATTRIBUTED, _lib_accounts.VENDOR_WIDE):
            continue
        by_base.setdefault(_base_label_for_row(row).lower(), []).append(row)

    labels: "dict[str, str]" = {}
    for group in by_base.values():
        if len(group) == 1:
            row = group[0]
            labels[str(row["account_key"])] = _base_label_for_row(row)
            continue
        by_plan: "dict[str, list[dict]]" = {}
        for row in group:
            plan = str(row.get("plan_type") or "").strip()
            by_plan.setdefault(plan.lower(), []).append(row)
        for plan_group in by_plan.values():
            for row in plan_group:
                base = _base_label_for_row(row)
                plan = str(row.get("plan_type") or "").strip()
                key = str(row["account_key"])
                discriminator = (
                    plan if plan and len(plan_group) == 1 else key[:8]
                )
                labels[key] = f"{base} ({discriminator})"
    return labels


def display_label_map(conn, provider: str) -> "dict[str, str]":
    """`display_label_map_from_rows` over one provider's registry."""
    return display_label_map_from_rows(load_accounts(conn, provider))


def display_account_label(conn, account_key: str) -> str:
    """The population-aware label for ONE key — the scalar entry point every
    consumer (alert prefix, share label, `--account` JSON, dashboard card) uses.

    Returns exactly what `display_label_map` would for the same key, so the
    surfaces cannot disagree about what an account is called. Sentinels and keys
    the registry does not know degrade to the scalar `account_label`, never to a
    guess.
    """
    if account_key in (_lib_accounts.UNATTRIBUTED, _lib_accounts.VENDOR_WIDE):
        return account_label(conn, account_key)
    row = conn.execute(
        "SELECT provider FROM accounts WHERE account_key = ?", (account_key,)
    ).fetchone()
    if row is None or not row[0]:
        return account_label(conn, account_key)
    return display_label_map(conn, str(row[0])).get(
        account_key, account_label(conn, account_key))


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
            print_ref_candidates(conn, exc.candidates)
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
        label = display_account_label(conn, account_key)
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
    # #416 §6: the rendered label is population-aware, so two accounts that
    # auto-label to one email no longer print identically in the list they are
    # meant to be distinguished by. Built per provider, since a Claude account
    # sharing a Codex account's email is not a collision (they never appear in
    # one list).
    display: "dict[str, str]" = {}
    for prov in sorted({str(a["provider"]) for a in accounts if a["provider"]}):
        display.update(display_label_map_from_rows(
            [a for a in accounts if a["provider"] == prov]))
    for a in accounts:
        rows.append([
            a["provider"] or "-",
            display.get(a["account_key"]) or account_label_from_row(a),
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


def print_ref_candidates(conn, candidates) -> None:
    """Print an ambiguity candidate list a user can actually act on (#416 §6).

    Each line carries the population-aware DISPLAY label plus a key PREFIX.
    Both are needed: `resolve_account_ref` accepts only STORED labels, emails and
    key prefixes (`bin/_lib_accounts.py`), so a generated collision label such as
    `omrikais@me.com (pro)` is NOT a resolvable ref — printing it alone would
    hand the user a string that cannot be typed back. The prefix is the
    resolvable half; the label is the half that says which account it is.

    Best-effort: an unreadable registry falls back to the bare keys, which is
    exactly today's output.
    """
    if not candidates:
        return
    eprint("candidates:")
    try:
        labels = {
            key: display_account_label(conn, key) for key in candidates
        }
    except sqlite3.Error:
        labels = {}
    for cand in candidates:
        label = labels.get(cand)
        eprint(f"  {cand[:8]}  {label}" if label else f"  {cand}")


def _resolve_ref_or_exit(conn, ref: str) -> "str | None":
    """Resolve a ref, printing candidates + returning None on error (exit 2)."""
    try:
        return _lib_accounts.resolve_account_ref(conn, ref)
    except _lib_accounts.AccountRefError as exc:
        eprint(f"account: ref {ref!r} is ambiguous or unknown")
        print_ref_candidates(conn, exc.candidates)
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
        # Resolved while the connection is still open — the map is
        # population-aware and therefore needs the registry, unlike the scalar
        # `account_label_from_row` it replaces.
        display = display_account_label(conn, key) if a is not None else None
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

    # #416 §6: population-aware, so `account show` names the account exactly the
    # way `account list`, the chip, the alert prefix and the share label do.
    label = (display or
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
