# `cctally account`

Inspect the per-provider account registry. cctally observes which Claude or Codex account each usage sample was written under — reading the provider's own on-disk credential state, never any third-party switcher tool — and records milestones, quota, and alerts **per account**. This subcommand lists the observed accounts, shows one account's identity and attribution summary, and sets a durable friendly label.

Multi-account support is byte-stable: with at most one real account per provider (a legacy `unattributed` bucket does not count), every default render is identical to a single-account install. Account decoration — labels, extra columns, JSON keys — appears only once a provider has more than one real account, or when you explicitly ask for it (`--account`, `cctally account …`).

## Usage

```
cctally account list [--json]
cctally account show <ref> [--json]
cctally account label <ref> <name>
```

### `list`

Lists every observed account with its provider, label, email, plan, first/last-seen timestamps, and a live `active` marker (the account currently logged in per the provider's credential state). `--json` emits the stamped-first camelCase envelope (`schemaVersion: 1`) with an `accounts` array.

### `show <ref>`

Shows one account's identity plus a short attribution summary (how many usage snapshots and percent milestones are stamped to it). `--json` emits the envelope.

### `label <ref> <name>`

Sets a durable, user-provided label for the account. User labels win over any auto-derived or switcher-imported label (`user > switcher > auto`) and survive `cctally db rebuild --db stats` because the rename is journaled.

## Account refs

Everywhere a `<ref>` is accepted (`show`, `label`, and the `--account` filter below), it is resolved **case-insensitively** in this order:

1. label (exact, case-insensitive),
2. email (exact, case-insensitive),
3. a unique `account_key` prefix (the 32-hex opaque key).

The literal `unattributed` is accepted for the pre-feature / unresolved bucket. An ambiguous or unknown ref exits **2** with the candidate keys printed on stderr.

## The `--account <ref>` filter

`--account <ref>` scopes a command's output to a single account. It is wired onto the Claude usage/analytics family — `report`, `forecast`, `weekly`, `percent-breakdown`, `five-hour-blocks`, `five-hour-breakdown`, `daily`, `monthly`, `session`, `project`, `diff`, `range-cost`, `cache-report` (provider `claude`) — and the five `codex quota` views `history` / `statusline` / `forecast` / `blocks` / `breakdown` (provider `codex`). Under `--json`, a selected account adds the `accountKey` / `accountLabel` keys; without the flag the render is byte-identical to the pre-feature output (R8).

On the source-aware analytics commands (`project`, `diff`, `range-cost`, `cache-report`, `report`), `--account` scopes the Claude analytics and is only valid with `--source claude` (the default); combining it with `--source codex` or `--source all` is a usage error (exit 2), since the account dimension is provider-scoped and Codex account filtering lives on `codex quota`.

If `--account` is requested but the entry cache is unavailable (the direct-JSONL fallback path), the command exits **3** with an attribution-unavailable diagnostic — historical JSONL lines carry no account identity and must never be stamped with the current login at read time.

## Per-account budgets

Two config keys hold optional per-account weekly budgets, keyed by account:

```
cctally config set budget.accounts '{"<ref-or-key>": 50}'          # Claude
cctally config set budget.codex.accounts '{"<ref-or-key>": 30}'    # Codex
```

`config set` accepts a ref (label / email / key prefix) but **normalizes it to the immutable account key at write time**, so a later `cctally account label` rename never retargets a configured budget. A raw 32-hex account key is stored verbatim. The reserved `unattributed` / `*` buckets are rejected — per-account budgets target real accounts only. `budget.codex.accounts` is valid **without** a vendor-wide `budget.codex.amount_usd`.

Vendor-wide budgets (`budget.weekly_usd`, `budget.codex.amount_usd`) keep today's semantics and count **all** accounts including unattributed spend; unattributed spend can never trip a per-account budget alert.

## Alerts

Alert notification text gains a `[<label>]` prefix only when the vendor has more than one real account. The `alerts.log` file carries the account key as its trailing (8th) tab-delimited field on every line (`*` for vendor-wide rows).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. |
| 2 | Ambiguous or unknown account ref (candidates on stderr). |
| 3 | `--account` requested but the entry cache is unavailable. |

## See also

- `docs/commands/budget.md` — the budget subcommand and vendor-wide budgets.
- `docs/commands/doctor.md` — the `accounts.*` health legs.
