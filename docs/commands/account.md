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

## The dashboard account selector

When a provider has more than one real account, the dashboard shows a row of account chips under the hero and the `a` key cycles through them (All accounts → each account → back to All). The selection is per source and is remembered across reloads; an account that disappears from the envelope falls back to All without losing the stored choice, so it re-engages if the account comes back.

**Codex — the selection re-scopes the whole view.** Picking an account switches the daily, monthly and weekly period tables, Sessions, Projects, the trend chart, cache diagnostics, the forecast, budget, quota blocks, alerts and the milestone history to that account alone — including each panel's expanded view, so clicking a panel open never widens it back to every account. Its weekly percentage, reset, spend and tokens are its own, and opening a past cycle from the hero shows that account's own cycle rather than the merged one. Nothing is added together in the browser: each account's figures are computed server-side, because model breakdowns cannot be reassembled from totals and quota percentages are not additive.

An account with no recorded usage renders an explicit "no Codex activity recorded" note with its percentage and reset left blank, rather than continuing to show whichever account you were looking at before. An account that has spend but no live cycle keeps its spend and leaves only the percentage and reset blank. The reserved `unattributed` bucket stays selectable and shows totals only — it holds Codex history recorded before per-file attribution existed, which is deliberately never guessed at. Alerts that belong to the whole vendor rather than to one account, such as a vendor-wide budget crossing, stay visible under a focused account and are labelled `vendor-wide`.

Under **All accounts** the hero headline is the merged spend and token total across every account, including the unattributed bucket — the same figures the cards below it add up to. The headline percentage, reset, `$/1%`, forecast and week range are deliberately blank instead, because independent quota allowances are never blended into a single number and no single account's cycle window describes the whole; each blank carries a `per account` pointer to the cards, where each account's own percentage, reset and spend appear. Opening the cycle modal from that hero shows the same per-account table rather than any one account's milestone ladder — pick an account chip first to see a ladder. A single-account install is unchanged throughout.

**Claude — the chip is a hero decoration only.** Claude usage is stamped per account and reported per account by `--account` on the CLI, but the dashboard does not yet split its panels by account. Sessions and Recent alerts therefore keep an explicit `all accounts (unfiltered)` caption while a Claude account is selected, so the panel never implies a filter it is not applying.

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
