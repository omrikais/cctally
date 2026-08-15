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
    if action == "attribute":
        return _cmd_account_attribute(args)
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


# --------------------------------------------------------------------------
# #500 — `cctally account attribute`: operator attribution of recorded Codex
# quota windows and spend.
#
# Spec: docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md
#
# The command is a PLANNER plus an apply sequence. Planning decides what to
# record and refuses loudly; the fold-time overlay (`_lib_codex_window_
# attribution`, applied inside `load_codex_quota_observations`) re-decides what
# to APPLY on every load and suppresses quietly. The two evaluation points are
# deliberately separate: ingest keeps running, and a group that was cleanly
# unattributed at assertion time may later acquire native evidence.
#
# Nothing here writes `quota_window_snapshots`. The journal holds the truth, a
# derived cache table indexes it, and both axes follow from one insertion point.
# --------------------------------------------------------------------------

#: Refusal codes, sorted into this order wherever more than one applies, so the
#: envelope is stable and an operator reads the same first cause every run.
#:
#: `not_weekly` and `model_scoped` share a DISPOSITION and a remedy, and they
#: are still two codes here, because this surface names a CAUSE per window
#: rather than a decision. Measured on the maintainer's store, one whole-era
#: range reaches 513 five-hour windows against 26 separate model pools, and an
#: operator reading `refusalCodes: ["model_scoped"]` beside a 5-hour reset
#: concludes the `_lib_codex_pools` classifier is wrong about it. The fold-time
#: outcome deliberately stays ONE value (`_lib_codex_window_attribution
#: .SUPPRESSED_MODEL_SCOPED`): that surface names a suppression decision, and
#: nothing acts on the two differently there.
_REFUSAL_ORDER = (
    "partial_group",
    "not_weekly",
    "model_scoped",
    "native_account_conflict",
    "assertion_conflict",
    "spend_account_conflict",
)

#: The refusals that BLOCK an all-or-nothing apply, which is not every refusal.
#:
#: Each of these names a real disagreement about a window the operator could
#: legitimately have meant, so applying the rest would leave the era they asked
#: for half-attributed. `not_weekly` and `model_scoped` are deliberately NOT
#: among them: a 5-hour window or a separate model pool can NEVER be account
#: weekly quota, so it was never a candidate, and the operator did not ask for
#: it — a time range selected it. Measured read-only against the maintainer's
#: store on 2026-08-15, a single `--since 2025-12-01 --until 2026-07-29` selects
#: 605 groups of which 539 are out of scope (513 five-hour windows and 26 Spark
#: pools) and 66 are attributable; letting the 539 block would refuse every run
#: an operator could ever make, which is the literal reading of the spec's
#: precedence table and is dead on arrival. Such a group is still REPORTED as
#: refused with its code, which is what AC4's "refused at plan time" asks for
#: and what keeps the exclusion visible rather than silent.
_BLOCKING_REFUSALS = frozenset({
    "partial_group",
    "native_account_conflict",
    "assertion_conflict",
    "spend_account_conflict",
})


class _AttributeUsage(ValueError):
    """A native-usage error: printed on stderr, exit 2."""


def _attribute_parse_instant(text: object, flag: str) -> "dt.datetime":
    """A timezone-aware ISO instant, normalized to UTC.

    Naive input is refused rather than assumed to be UTC or local. The selector
    is a half-open `[since, until)` range and partial-group refusal depends on
    exactly which side of it a boundary falls, so an ambiguous instant would
    silently change which groups are whole.
    """
    import datetime as dt

    raw = str(text or "").strip()
    if not raw:
        raise _AttributeUsage(f"{flag} requires an ISO instant")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _AttributeUsage(
            f"{flag}: {raw!r} is not an ISO-8601 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _AttributeUsage(
            f"{flag}: {raw!r} is naive; a timezone-aware instant is required "
            "(for example 2026-01-01T00:00:00Z)")
    return parsed.astimezone(dt.timezone.utc)


def _attribute_resolve_account(conn, ref: str) -> str:
    """Resolve `<ref>` to a REAL Codex account key, or raise `_AttributeUsage`.

    The literal `unattributed` resolves everywhere else a ref is accepted and is
    rejected here, because attributing data TO the sentinel is not a fact an
    operator can assert — the sentinel means "nobody could determine this".
    """
    try:
        key = _lib_accounts.resolve_account_ref(conn, ref)
    except _lib_accounts.AccountRefError as exc:
        eprint(f"account attribute: ref {ref!r} is ambiguous or unknown")
        print_ref_candidates(conn, exc.candidates)
        raise _AttributeUsage("unresolvable ref") from None
    if key == _lib_accounts.UNATTRIBUTED:
        raise _AttributeUsage(
            "account attribute: the literal 'unattributed' names the "
            "\"account could not be determined\" bucket, so it cannot be the "
            "SUBJECT of an attribution")
    row = conn.execute(
        "SELECT provider FROM accounts WHERE account_key = ?", (key,)
    ).fetchone()
    provider = (row[0] if row is not None else None) or ""
    if provider != "codex":
        raise _AttributeUsage(
            f"account attribute: {ref!r} resolves to a {provider or 'unknown'} "
            "account; this command attributes codex data only (Claude legacy "
            "history keeps its accounts_cutover answer)")
    return key


def _attribute_witness(value) -> str:
    """One spelling for one reset instant, applied to BOTH sides of the binding.

    The same normalizer the overlay applies to a group's stored resets and to an
    assertion's stored witnesses, so a `Z` witness and a `+00:00` group member
    intersect. Taken from the pure ledger leaf rather than respelled.
    """
    import _lib_quota_ledger

    return _lib_quota_ledger.normalize_reset(value)


class _AttributeGroup:
    """One planned physical window group, or one planned assertion record."""

    __slots__ = (
        "group_key", "source_root_key", "logical_limit_key", "observed_slot",
        "window_minutes", "canonical_resets_at", "raw_resets", "disposition",
        "native_accounts", "assertion_accounts", "observation_count",
        "spend_candidate_count", "refusal_codes", "op_ids",
    )

    def __init__(self, **kwargs):
        for name in self.__slots__:
            setattr(self, name, kwargs.get(name))

    @property
    def blocks(self) -> bool:
        """Whether this group's refusal stops the whole apply."""
        return bool(set(self.refusal_codes) & _BLOCKING_REFUSALS)

    @property
    def fingerprint(self) -> tuple:
        """Everything the apply revalidation compares.

        Spec §8: the preview was computed without locks, so the plan is
        re-derived under them and compared before anything is written. This is
        the comparison — the group's witnesses, the accounts present, the
        active assertions and the spend counts.
        """
        return (
            tuple(self.group_key), tuple(self.raw_resets),
            tuple(self.native_accounts), tuple(self.assertion_accounts),
            int(self.observation_count), int(self.spend_candidate_count),
            str(self.disposition), tuple(self.refusal_codes),
            tuple(self.op_ids or ()),
        )

    def to_json(self) -> dict:
        return {
            "group": {
                "sourceRootKey": self.source_root_key,
                "logicalLimitKey": self.logical_limit_key,
                "observedSlot": self.observed_slot,
                "windowMinutes": int(self.window_minutes),
                "canonicalResetsAtUtc": self.canonical_resets_at,
                "rawResetsAtUtc": list(self.raw_resets),
            },
            "disposition": self.disposition,
            "nativeAccountKeys": list(self.native_accounts),
            "assertionAccountKeys": list(self.assertion_accounts),
            "observationCount": int(self.observation_count),
            "spendCandidateCount": int(self.spend_candidate_count),
            "refusalCodes": list(self.refusal_codes),
            "assertionOpIds": list(self.op_ids or ()),
        }


def _attribute_spend_index(conn, roots) -> dict:
    """Per root, the accounting rows sorted by instant, for O(log n) lookups.

    One query per root rather than one per group: a whole-history plan can name
    dozens of groups, and the alternative is a full scan each time.
    """
    index: dict = {}
    for root in sorted(roots):
        rows = []
        try:
            cursor = conn.execute(
                "SELECT timestamp_utc, account_key FROM codex_session_entries "
                " WHERE source_root_key = ? ORDER BY timestamp_utc", (root,))
        except sqlite3.DatabaseError:
            index[root] = ([], [])
            continue
        for timestamp, account in cursor:
            parsed = _attribute_parse_stored_instant(timestamp)
            if parsed is None:
                continue
            rows.append((parsed, account))
        rows.sort(key=lambda item: item[0])
        index[root] = ([item[0] for item in rows], [item[1] for item in rows])
    return index


def _attribute_parse_stored_instant(value):
    import datetime as dt

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _attribute_spend_slice(index, root, start, end):
    """The `[start, end)` accounting rows of one root, as account keys."""
    import bisect

    instants, accounts = index.get(root, ([], []))
    low = bisect.bisect_left(instants, start)
    high = bisect.bisect_left(instants, end)
    return accounts[low:high]


def _attribute_plan_groups(conn, *, account_key, since, until):
    """Plan the ATTRIBUTE mode: which physical window groups the range names.

    The range selects OBSERVATIONS. A group every one of whose observations
    falls inside it is whole and may be attributed; a group the range covers
    only partially is refused rather than split, and its whole extent is
    reported so the operator can widen the range instead of guessing at it.
    """
    import datetime as dt

    import _cctally_quota as _quota
    import _lib_codex_pools as _pools
    import _lib_codex_window_attribution as _wa

    observations = _quota.load_codex_quota_observations(cache_conn=conn)
    _resolutions, ownership = _quota.resolve_codex_window_attributions(conn)

    buckets: dict = {}
    for observation in observations:
        identity = observation.identity
        anchor = observation.canonical_resets_at
        key = (identity.source, identity.source_root_key,
               identity.logical_limit_key, identity.observed_slot,
               identity.window_minutes, anchor)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {
                "members": [], "accounts": set(), "witnesses": set(),
                "model_scoped": False,
            }
        bucket["members"].append(observation)
        if identity.account_key != _wa.UNATTRIBUTED_SENTINEL:
            bucket["accounts"].add(identity.account_key)
        bucket["witnesses"].add(_attribute_witness(
            observation.resets_at.astimezone(dt.timezone.utc).isoformat()))
        # `limit_name` sits outside identity equality, so one group's members
        # can disagree about it; ANY Spark evidence demotes the whole group out
        # of account weekly quota (#373), matching the fold and the spend pass.
        if _pools.is_model_scoped_codex_quota(
                identity.logical_limit_key, identity.limit_name):
            bucket["model_scoped"] = True

    selected_keys = [
        key for key, bucket in buckets.items()
        if any(since <= member.captured_at < until
               for member in bucket["members"])
    ]
    roots = {key[1] for key in selected_keys}
    spend_index = _attribute_spend_index(conn, roots)

    planned = []
    for key in sorted(selected_keys, key=lambda item: (item[5], str(item))):
        bucket = buckets[key]
        members = bucket["members"]
        owner = ownership.get(key)
        # The overlay stamps an unattributed observation only where the group
        # carries NO native real account, so subtracting the resolved owner
        # recovers the pre-overlay native population exactly.
        native = sorted(bucket["accounts"] - ({owner} if owner else set()))
        asserted = sorted({owner} if owner else set())
        anchor = key[5]
        window_minutes = int(key[4])
        start = anchor - dt.timedelta(minutes=window_minutes)
        spend = _attribute_spend_slice(spend_index, key[1], start, anchor)
        spend_candidates = sum(
            1 for account in spend
            if not account or account == _wa.UNATTRIBUTED_SENTINEL)
        spend_conflicts = sorted({
            account for account in spend
            if account and account != _wa.UNATTRIBUTED_SENTINEL
            and account != account_key
        })

        codes = []
        if not all(since <= member.captured_at < until for member in members):
            codes.append("partial_group")
        if window_minutes != _wa.ACCOUNT_WEEKLY_WINDOW_MINUTES:
            codes.append("not_weekly")
        if bucket["model_scoped"]:
            codes.append("model_scoped")
        if native and native != [account_key]:
            codes.append("native_account_conflict")
        if asserted and asserted != [account_key]:
            codes.append("assertion_conflict")
        if spend_conflicts:
            codes.append("spend_account_conflict")

        if codes:
            disposition = "refused"
        elif native == [account_key] or asserted == [account_key]:
            # Already true, by native evidence or by an assertion already on
            # record. Idempotency falls out of this branch rather than needing a
            # dedup check: a second identical apply plans every group here.
            disposition = "noop"
        else:
            disposition = "eligible"

        planned.append(_AttributeGroup(
            group_key=key, source_root_key=key[1], logical_limit_key=key[2],
            observed_slot=key[3], window_minutes=window_minutes,
            canonical_resets_at=anchor.isoformat(),
            raw_resets=tuple(sorted(bucket["witnesses"])),
            disposition=disposition, native_accounts=tuple(native),
            assertion_accounts=tuple(asserted),
            observation_count=len(members),
            spend_candidate_count=spend_candidates,
            refusal_codes=tuple(
                code for code in _REFUSAL_ORDER if code in codes),
            op_ids=(),
        ))
    return planned


def _attribute_plan_retractions(conn, *, account_key, since, until):
    """Plan the RETRACT mode, over the durable assertion records (spec §4.1).

    Retraction cannot use the attribute selector. An assertion is allowed to be
    DORMANT — its witnesses match no current group — and the operator is told to
    clear a dormant or split assertion by retracting it. A dormant assertion has
    no current observation for an observation-range selector to find, so an
    observation-based `--retract` could never reach the one case it most needs
    to reach.
    """
    import datetime as dt

    import _cctally_cache as _cache_mod
    import _cctally_quota as _quota

    records = _cache_mod.load_active_window_attributions(conn)
    resolutions, _ownership = _quota.resolve_codex_window_attributions(conn)
    outcome = {str(r.op_id): str(r.outcome) for r in resolutions}

    planned = []
    for record in records:
        if str(record["account_key"]) != account_key:
            continue
        anchor = _attribute_parse_stored_instant(
            record.get("canonical_resets_at_utc"))
        # The selector is the record's stored assertion-time WINDOW — the
        # nominal `[anchor - window, anchor)` interval — matched by OVERLAP, not
        # its reset instant. Two reasons, and the second is the one that would
        # bite an operator. Attribute mode selects OBSERVATIONS, which sit up to
        # a week before the reset they witness, so an instant match would make
        # the very range that recorded an assertion fail to retract it. And a
        # retraction is a corrective action the operator previews before
        # applying, so reaching one record too many is cheap where reaching one
        # too few is the failure §4.1 exists to prevent.
        #
        # The assertion timestamp is the tiebreak for a record whose window is
        # no longer resolvable at all, which is exactly the record a retraction
        # most needs to be able to reach.
        if anchor is not None:
            start = anchor - dt.timedelta(minutes=int(record["window_minutes"]))
            if not (start < until and since < anchor):
                continue
        else:
            asserted = _attribute_parse_stored_instant(
                record.get("asserted_at_utc"))
            if asserted is None or not (since <= asserted < until):
                continue
        op_id = str(record["op_id"])
        planned.append(_AttributeGroup(
            group_key=("codex", str(record["source_root_key"]),
                       str(record["logical_limit_key"]),
                       str(record["observed_slot"]),
                       int(record["window_minutes"]),
                       record.get("canonical_resets_at_utc")),
            source_root_key=str(record["source_root_key"]),
            logical_limit_key=str(record["logical_limit_key"]),
            observed_slot=str(record["observed_slot"]),
            window_minutes=int(record["window_minutes"]),
            canonical_resets_at=record.get("canonical_resets_at_utc"),
            raw_resets=tuple(str(v) for v in record["raw_resets_at_utc"]),
            disposition=outcome.get(op_id, "dormant"),
            native_accounts=(), assertion_accounts=(account_key,),
            observation_count=0, spend_candidate_count=0,
            refusal_codes=(), op_ids=(op_id,),
        ))
    planned.sort(key=lambda group: (str(group.canonical_resets_at or ""),
                                    group.op_ids[0]))
    return planned


def _attribute_records(planned, *, account_key, mode, at):
    """The journal ops one apply appends — ONE PER GROUP, never a whole range.

    The journal enforces a 65,536-byte line limit, and per-group lines also make
    a partial append recoverable as a complete prefix.
    """
    import _lib_journal as _lj

    records = []
    for group in planned:
        # `codex_window_attributions.canonical_resets_at_utc` is nullable per
        # its DDL, and the journal is append-only, so `str(None)` would write
        # the literal "None" into a segment nothing can ever rewrite. The field
        # is audit-only and never matched on, so the record's first raw witness
        # is the honest stand-in; when there is not even one, the builder's
        # non-empty guard rejects the record rather than accepting a placeholder.
        canonical = group.canonical_resets_at
        if not isinstance(canonical, str) or not canonical.strip():
            canonical = next(iter(group.raw_resets or ()), None)
        common = dict(
            account_key=account_key,
            source_root_key=group.source_root_key,
            logical_limit_key=group.logical_limit_key,
            observed_slot=group.observed_slot,
            window_minutes=int(group.window_minutes),
            raw_resets_at_utc=list(group.raw_resets),
            canonical_resets_at_utc=canonical,
        )
        if mode == "retract":
            records.append(_lj.make_codex_window_attribution_retract(
                at=at, retracted_assertion_ids=list(group.op_ids), **common))
        else:
            records.append(_lj.make_codex_window_attribution(at=at, **common))
    return records


def _attribute_plan(conn, *, account_key, mode, since, until):
    if mode == "retract":
        return _attribute_plan_retractions(
            conn, account_key=account_key, since=since, until=until)
    return _attribute_plan_groups(
        conn, account_key=account_key, since=since, until=until)


def _attribute_status(planned, *, mode, applied: bool) -> str:
    if not planned:
        return "empty"
    if any(group.blocks for group in planned):
        return "refused"
    if applied:
        return "applied"
    if mode != "retract" and not any(
            group.disposition == "eligible" for group in planned):
        # Nothing left to record. That covers both a second identical apply
        # (every group already asserted) and a range that reached only
        # out-of-scope windows — neither is a refusal, and reporting the second
        # as one would tell an operator to fix something no operator can fix.
        # The summary tells the two apart.
        return "noop"
    return "preview"


def _attribute_payload(*, status, mode, account_key, label, since, until,
                       planned, actions, errors, until_specified=True) -> dict:
    selected = len(planned)
    eligible = sum(1 for g in planned if g.disposition == "eligible")
    noop = sum(1 for g in planned if g.disposition == "noop")
    refused = sum(1 for g in planned if g.disposition == "refused")
    blocking = sum(1 for g in planned if g.blocks)
    if mode == "retract":
        # Every matched record is retractable; the disposition column carries
        # the record's CURRENT resolution state instead (resolved / dormant /
        # split / suppressed_*), which is what §4.1 asks the preview to show.
        eligible, noop = selected, 0
    return {
        "status": status,
        "mode": mode,
        "source": "codex",
        "account": ({"accountKey": account_key, "accountLabel": label}
                    if account_key else None),
        # `until` is the RESOLVED exclusive end, never null on a run that got
        # far enough to resolve one, so a consumer can reproduce the selection
        # the command actually made. `untilSpecified` is what preserves the
        # other fact — whether the operator named that instant or the command
        # defaulted it to the run's own "now".
        "selector": {"since": since, "until": until,
                     "untilSpecified": bool(until_specified)},
        "summary": {
            "selectedGroups": selected, "eligibleGroups": eligible,
            "noOpGroups": noop, "refusedGroups": refused,
            "blockingRefusedGroups": blocking,
        },
        "groups": [group.to_json() for group in planned],
        "actions": dict(actions),
        "errors": list(errors),
    }


_ATTRIBUTE_NO_ACTIONS = {
    "journalOpsAppended": 0, "quotaGroupsUpdated": 0, "spendRowsUpdated": 0,
}


#: Refused rows the human render prints before it summarizes the rest — the cap
#: `five-hour-blocks` already applies to an unfiltered listing, for the same
#: reason. A whole-era range selects 605 groups on the maintainer's store and
#: refuses 539 of them as out of scope, and one row each buries the handful
#: that actually need a decision.
_ATTRIBUTE_REFUSED_ROW_CAP = 50


def _attribute_render(payload, *, requested_apply: bool = False) -> None:
    mode = payload["mode"]
    account = payload["account"] or {}
    selector = payload["selector"]
    print(
        f"account attribute ({mode}) — codex / "
        f"{account.get('accountLabel') or account.get('accountKey') or '-'}")
    # The resolved instant, annotated when the command supplied it, so this
    # render and the `--json` envelope describe the same range.
    until_note = "" if selector.get("untilSpecified", True) else "  (now)"
    print(f"range: [{selector['since']}, {selector['until']}){until_note}")
    if not payload["groups"]:
        print("No window group matches the selector; nothing to do.")
        return
    headers = (["WINDOW RESET", "SLOT", "OBS", "SPEND", "STATE", "DETAIL"]
               if mode != "retract" else
               ["WINDOW RESET", "SLOT", "ASSERTION", "STATE", "DETAIL"])
    rows = []
    shown_refused = hidden_refused = 0
    for group in payload["groups"]:
        if group["disposition"] == "refused":
            if shown_refused >= _ATTRIBUTE_REFUSED_ROW_CAP:
                hidden_refused += 1
                continue
            shown_refused += 1
        window = group["group"]
        detail = ", ".join(group["refusalCodes"]) or ", ".join(
            group["nativeAccountKeys"] or group["assertionAccountKeys"])[:16]
        if mode == "retract":
            rows.append([
                str(window["canonicalResetsAtUtc"] or "-"),
                window["observedSlot"],
                (group["assertionOpIds"] or ["-"])[0][:16],
                group["disposition"], detail or "-",
            ])
        else:
            rows.append([
                str(window["canonicalResetsAtUtc"]), window["observedSlot"],
                str(group["observationCount"]), str(group["spendCandidateCount"]),
                group["disposition"], detail or "-",
            ])
    print(_render_table(headers, rows))
    if hidden_refused:
        print(f"… and {hidden_refused} more refused window(s) not shown "
              f"(--json lists every one).")
    summary = payload["summary"]
    line = (
        f"{summary['selectedGroups']} selected, "
        f"{summary['eligibleGroups']} eligible, {summary['noOpGroups']} no-op, "
        f"{summary['refusedGroups']} refused")
    blocking = int(summary.get("blockingRefusedGroups") or 0)
    if blocking:
        # Without this an operator cannot tell a refusal that stops the run from
        # an out-of-scope window that was merely skipped, except by reading
        # every row's codes.
        line += f" ({blocking} blocking)"
    print(line)
    actions = payload["actions"]
    if payload["status"] in ("applied", "recovered"):
        print(
            f"applied: {actions['journalOpsAppended']} journal op(s), "
            f"{actions['quotaGroupsUpdated']} window group(s), "
            f"{actions['spendRowsUpdated']} spend row(s)")
        print(
            "The percentage axis converges on this run; a projection reached "
            "from a Codex hook tick instead defers a whole-history pass to the "
            "detached verifier and lands on a following tick.")
    elif payload["status"] == "refused":
        if requested_apply:
            print("Nothing was written: apply is all-or-nothing and a group "
                  "refused.")
        else:
            # A preview was never going to write anything, so reporting that it
            # did not is no news; what the operator needs is what an apply WOULD
            # do with this range.
            print("Nothing would be written: apply is all-or-nothing and a "
                  "group refused. Resolve or exclude it, then re-run with "
                  "--yes.")
    elif payload["status"] != "empty":
        print("Preview only — nothing was written. Re-run with --yes to apply.")
    for error in payload["errors"]:
        eprint(f"account attribute: {error['message']}")


def _attribute_recovered(previous, current, account_key) -> bool:
    """Whether the difference between two plans is a `recordedPending` recovery.

    §8.5: if the append succeeds and a later step fails, the journal holds the
    truth and the derived state does not. On the rerun the tail replay lands the
    records, so every group this plan wanted to assert now reads as a no-op for
    exactly this account. That is a RECOVERY, not drift, and it must be
    recognised before the generic drift check refuses it.
    """
    before = {tuple(g.group_key): g for g in previous
              if g.disposition == "eligible"}
    if not before:
        return False
    after = {tuple(g.group_key): g for g in current}
    for key, _group in before.items():
        landed = after.get(key)
        if landed is None or landed.disposition != "noop":
            return False
        if landed.assertion_accounts != (account_key,):
            return False
    return True


def _attribute_tail_pending(conn) -> bool:
    """Whether an earlier run recorded attribution the stats side never got.

    §8.5 promises that a rerun after a failure anywhere past the append
    "completes the cache and stats steps, reports `recovered`, and exits 0". The
    window this predicate exists for is the one where the journal append AND the
    cache transaction both succeeded and the stats step did not: the derived
    table already carries the assertions, so every group plans as a no-op, and
    the `--yes` short-circuit would exit 0 reporting `noop` while stats.db still
    lacked the attribution. Retract mode reaches the same state through `empty`.

    The predicate is the projection certificate's attribution revision against
    the derived table's live one — the same comparison
    `load_codex_quota_projection_certificate` already fails closed on (§8.3).
    A store that has never asserted anything reads 0 on both sides, so an
    ordinarily idempotent second apply still short-circuits and still never
    takes the lock set, which is what AC9 pins.
    """
    import _cctally_cache as _cache_mod
    import _cctally_quota as _quota

    try:
        live = int(_cache_mod.codex_window_attribution_revision(conn))
        stamped = int(_quota._certificate_attribution_revision(
            _quota._codex_quota_projection_certificate_payload(conn)))
    except (sqlite3.DatabaseError, TypeError, ValueError):
        # A cache too old to carry either value has no attribution to finish.
        return False
    return stamped != live


def _attribute_apply(planned, *, account_key, mode, since, until, at,
                     completion=False):
    """Record the plan, move both axes, and re-project — spec §8.

    Locks, in the one order that preserves the repository lock-order law:
    stats maintenance exclusive, cache maintenance shared, the journal ingest
    lock exclusive, the global cache writer flock exclusive, the Codex provider
    flock exclusive. `release_cache_flocks()` then drops the last two so every
    cache write is committed and unlocked before the stats transaction opens.

    Returns `(status, actions, errors, planned)`.
    """
    import _cctally_cache as _cache_mod
    import _cctally_journal as _jr
    import _cctally_quota as _quota
    import _cctally_rederive as _rd

    actions = dict(_ATTRIBUTE_NO_ACTIONS)
    appended = False
    try:
        with _rd.codex_attribution_apply_locks() as owner:
            # Opened INSIDE the lock set, which is only safe because the schema
            # and every pending migration are already applied: `_cmd_account_
            # attribute` opens the cache for the preview before this runs, and
            # `apply_db_rederive` does the same before taking the same locks. A
            # future caller reaching this without that guarantee would run a
            # migration under five held flocks.
            conn = _cache_mod.open_cache_db()
            try:
                # Reconcile the derived table's tail from the journal FIRST, so
                # a record this command appended on a previous run and never
                # materialized is visible to the revalidation below.
                conn.execute("BEGIN IMMEDIATE")
                _landed, skipped = _cache_mod.rehydrate_codex_window_attributions(
                    conn)
                conn.commit()
                if skipped:
                    _jr._report_window_attribution_skips(skipped)

                fresh = _attribute_plan(
                    conn, account_key=account_key, mode=mode,
                    since=since, until=until)
                recovered = (
                    mode != "retract"
                    and _attribute_recovered(planned, fresh, account_key))
                if not recovered:
                    if ([g.fingerprint for g in fresh]
                            != [g.fingerprint for g in planned]):
                        return ("conflict", actions, [{
                            "code": "plan_drift",
                            "message": (
                                "the store changed before the apply lock was "
                                "acquired; rerun `cctally account attribute` "
                                "for a fresh preview"),
                        }], fresh)
                    if any(g.blocks for g in fresh):
                        return ("refused", actions, [], fresh)

                targets = [g for g in fresh if g.disposition == "eligible"] \
                    if mode != "retract" else list(fresh)
                records = _attribute_records(
                    targets, account_key=account_key, mode=mode, at=at)
                if records:
                    _jr.append_records(
                        records, expected_high_water=_jr.journal_high_water())
                    appended = True
                    actions["journalOpsAppended"] = len(records)

                conn.execute("BEGIN IMMEDIATE")
                _landed, skipped = _cache_mod.rehydrate_codex_window_attributions(
                    conn)
                restored, adopted = (
                    _cache_mod.reconcile_codex_window_attribution_spend(
                        conn, strict=True))
                # Verify the applied record set against the plan we locked,
                # BEFORE the commit: a silent partial application must abort the
                # transaction rather than be reported as success (spec §8.2).
                active = {
                    str(row["op_id"])
                    for row in _cache_mod.load_active_window_attributions(conn)
                    if str(row["account_key"]) == account_key
                }
                expected = {str(rec["id"]) for rec in records}
                if mode == "retract":
                    # The TARGETED assertion ids, never the retraction op ids.
                    # A retraction inserts no row of its own — it only stamps
                    # `retracted_by_op_id` on the assertions it names — so
                    # intersecting the retraction ids with the active set is
                    # empty by construction and proves nothing, which is not
                    # what §8.2 asks for.
                    targeted = {
                        op_id for group in targets
                        for op_id in (group.op_ids or ())}
                    if targeted & active:
                        raise sqlite3.DatabaseError(
                            "retraction did not tombstone every named assertion")
                elif not expected <= active:
                    raise sqlite3.DatabaseError(
                        "the attribution records did not all materialize")
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
            owner.release_cache_flocks()
            if skipped:
                _jr._report_window_attribution_skips(skipped)
            actions["spendRowsUpdated"] = int(restored) + int(adopted)
            actions["quotaGroupsUpdated"] = len(targets)
            # Still holding stats maintenance and the ingest lock: consume the
            # appended prefix into stats.db without reacquiring either.
            _jr.run_stats_ingest(mode="authoritative", locks_held=True)
    except _rd.RederiveBusy as exc:
        return ("busy", actions, [{"code": "busy", "message": str(exc)}], planned)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        if appended:
            return ("recordedPending", actions, [{
                "code": "recordedPending",
                "message": (
                    f"the journal records were appended but a later step "
                    f"failed ({exc}); rerun the same command to finish"),
            }], planned)
        return ("error", actions, [
            {"code": "apply_failed", "message": str(exc)}], planned)

    # Outside the lock set, because this takes its own. No alert-eligible roots
    # are passed, which is what suppresses historical milestone dispatch while
    # still re-anchoring terminal state (spec §8.4).
    #
    # It opens its own stats transaction over the whole history and can fail on
    # its own, and by this point the journal append, the cache transaction and
    # the stats ingest have all succeeded. Uncaught, that failure would reach
    # the operator as a traceback with no envelope at all, when the state is
    # exactly the one `recordedPending` describes: recorded, not finished. The
    # rerun completes it, because the certificate's attribution revision is
    # still behind the derived table's (`_attribute_tail_pending`).
    try:
        _quota.reconcile_codex_quota_projection()
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return ("recordedPending", actions, [{
            "code": "recordedPending",
            "message": (
                f"the attribution was recorded and applied but the quota "
                f"projection did not finish ({exc}); rerun the same command "
                f"to complete it"),
        }], fresh)
    status = ("recovered" if ((recovered or completion) and not appended)
              else "applied")
    return (status, actions, [], fresh)


def _cmd_account_attribute(args: argparse.Namespace) -> int:
    """`cctally account attribute` — preview by default, `--yes` applies.

    Exit codes follow the staged-command convention of `db rederive` and
    `db journal-repair`: 0 for a preview, an empty selection, a no-op and a
    successful apply; 2 for every validation failure and every refusal; 3 for
    operational failure, including `recordedPending`.
    """
    import _cctally_cache as _cache_mod

    emit_json = bool(getattr(args, "emit_json", False))
    mode = "retract" if getattr(args, "retract", False) else "attribute"
    now = _cctally_core._command_as_of()
    since_text = str(getattr(args, "since", "") or "")
    until_text = getattr(args, "until", None)

    try:
        since = _attribute_parse_instant(since_text, "--since")
        until = (_attribute_parse_instant(until_text, "--until")
                 if until_text else now)
        if until <= since:
            raise _AttributeUsage(
                "account attribute: --until must be strictly after --since; "
                "the range is half-open [since, until)")
        conn = _cctally_core.open_db()
        try:
            account_key = _attribute_resolve_account(
                conn, getattr(args, "ref", None))
            label = display_account_label(conn, account_key)
        finally:
            conn.close()
    except _AttributeUsage as exc:
        message = str(exc)
        if message != "unresolvable ref":
            eprint(message)
        if emit_json:
            print(json.dumps(_cctally().stamp_schema_version(
                _attribute_payload(
                    status="error", mode=mode, account_key=None, label=None,
                    since=since_text,
                    until=(str(until_text) if until_text else None),
                    until_specified=bool(until_text),
                    planned=[], actions=_ATTRIBUTE_NO_ACTIONS,
                    errors=[{"code": "usage", "message": message}]))))
        return 2

    since_iso = since.isoformat().replace("+00:00", "Z")
    # Always the RESOLVED end, so a `--json` consumer can reproduce the exact
    # selection; `untilSpecified` carries whether the operator named it.
    until_iso = until.isoformat().replace("+00:00", "Z")

    requested_apply = bool(getattr(args, "yes", False))
    cache = _cache_mod.open_cache_db()
    try:
        planned = _attribute_plan(
            cache, account_key=account_key, mode=mode, since=since, until=until)
        completion = requested_apply and _attribute_tail_pending(cache)
    finally:
        cache.close()

    actions = dict(_ATTRIBUTE_NO_ACTIONS)
    errors: list = []
    status = _attribute_status(planned, mode=mode, applied=False)
    # An apply with nothing to record short-circuits BEFORE the lock set. That
    # is what makes a second identical apply append zero journal lines and
    # leave the high-water unchanged (spec §8.5) rather than taking five flocks
    # to discover the same thing.
    #
    # `completion` is the one exception, and it is what keeps §8.5's recovery
    # promise honest: a run whose append and cache transaction landed and whose
    # stats step did not plans every group as a no-op, so the short-circuit
    # alone would report `noop`, exit 0, and leave the attribution missing from
    # stats.db forever.
    if requested_apply and status != "refused" and (
            status == "preview" or completion):
        at = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        status, actions, errors, planned = _attribute_apply(
            planned, account_key=account_key, mode=mode,
            since=since, until=until, at=at, completion=completion)

    payload = _attribute_payload(
        status=status, mode=mode, account_key=account_key, label=label,
        since=since_iso, until=until_iso, until_specified=bool(until_text),
        planned=planned, actions=actions, errors=errors)
    if emit_json:
        print(json.dumps(_cctally().stamp_schema_version(payload)))
    else:
        _attribute_render(payload, requested_apply=requested_apply)
    if status in ("refused", "conflict"):
        return 2
    if status in ("recordedPending", "error", "busy"):
        return 3
    return 0
