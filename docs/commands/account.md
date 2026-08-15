# `cctally account`

Inspect the per-provider account registry. cctally observes which Claude or Codex account each usage sample was written under — reading the provider's own on-disk credential state, never any third-party switcher tool — and records milestones, quota, and alerts **per account**. This subcommand lists the observed accounts, shows one account's identity and attribution summary, and sets a durable friendly label.

Multi-account support is byte-stable: with at most one real account per provider (a legacy `unattributed` bucket does not count), every default render is identical to a single-account install. Account decoration — labels, extra columns, JSON keys — appears only once a provider has more than one real account, or when you explicitly ask for it (`--account`, `cctally account …`).

## Usage

```
cctally account list [--json]
cctally account show <ref> [--json]
cctally account label <ref> <name>
cctally account attribute <ref> --since <iso> [--until <iso>] [--retract] [--yes] [--json]
```

### `list`

Lists every observed account with its provider, label, email, plan, first/last-seen timestamps, and a live `active` marker (the account currently logged in per the provider's credential state). `--json` emits the stamped-first camelCase envelope (`schemaVersion: 1`) with an `accounts` array.

### `show <ref>`

Shows one account's identity plus a short attribution summary (how many usage snapshots and percent milestones are stamped to it). `--json` emits the envelope.

### `label <ref> <name>`

Sets a durable, user-provided label for the account. User labels win over any auto-derived or switcher-imported label (`user > switcher > auto`) and survive `cctally db rebuild --db stats` because the rename is journaled.

### `attribute <ref> --since <iso> [--until <iso>]`

Attributes already-recorded **Codex** quota windows and spend to a known Codex account.

cctally learned to record which account each sample belongs to partway through its life. Everything recorded before that carries the `unattributed` bucket, and nothing else can move it — so on a multi-account Codex install the whole earlier history stays unattributed permanently. This command lets you state the fact you already know: that a stretch of recorded Codex history belongs to a particular account.

It is Codex-only. Claude's earlier history has its own answer and a Claude `<ref>` is rejected.

**Preview is the default.** Running it without `--yes` writes nothing and prints every window group the range selects, with the observation and spend-row counts that would move and a disposition of `eligible`, `noop` or `refused`. Add `--yes` to apply.

**The time range.** `--since` is required and `--until` is optional (it defaults to now). Both must be timezone-aware ISO instants — `2026-01-01T00:00:00Z` or `2026-01-01T00:00:00+02:00`, never a bare `2026-01-01T00:00:00` — and the range is half-open, `[since, until)`, so a boundary falls on exactly one side of it. A naive value is a usage error.

**Whole windows only.** Attribution is asserted per whole physical quota window, never per observation. A window the range covers only partially is refused rather than split; the preview shows that window's full extent so you can widen the range and rerun.

**All or nothing, over the windows you could have meant.** A `partial_group`, `native_account_conflict`, `assertion_conflict` or `spend_account_conflict` refusal stops the whole run, and nothing at all is written. Each of those names a real disagreement about a window your range could legitimately have meant, so applying the rest would leave the stretch of history you asked for half attributed.

A window that is not account-level weekly quota is the exception. It is reported as refused, left alone, and does not stop the rest of the range from applying — because you never asked for it: a time range selected it, and no assertion can file a five-hour window or a separate model pool as account weekly quota under any circumstances. This is the majority outcome on a real range. One whole-era range measured on the author's own store selects 605 windows, of which 513 are five-hour windows and 26 belong to a separate model pool; only 66 are attributable at all.

**What refuses, and why**

| Reason | What it means | Stops the whole run? |
|---|---|---|
| `partial_group` | The range covers only part of that window. Widen it. | Yes |
| `not_weekly` | The window is not an account-level weekly quota window — a five-hour window, for instance. | No — reported and skipped |
| `model_scoped` | The window belongs to a separate model pool rather than to account-level weekly quota. | No — reported and skipped |
| `native_account_conflict` | The provider already identified a different account for that window. Recorded evidence always wins over an assertion. | Yes |
| `assertion_conflict` | Another assertion already names a different account for that window. Retract one first. | Yes |
| `spend_account_conflict` | Spend inside that window already carries a different real account. | Yes |

**Both axes move together.** Applying updates the quota percentage attribution and the spend attribution in one step, and the result survives `cctally db rebuild --db stats` and `cctally cache-sync --rebuild --source codex`, because the assertion is recorded in the append-only journal rather than in either database.

Applying re-derives the quota projection on the spot, so the change is visible immediately. If the projection is instead reached from a Codex hook tick — because the command could not finish it — a whole-history pass is handed to a background verifier and the attribution appears on a following tick rather than instantly.

Recorded evidence keeps winning afterwards. If the provider later identifies a different account for a window you attributed, the assertion stops applying and **both** axes move to the account the provider named; you do not have to retract anything for that to happen.

**Withdrawing an assertion.** `--retract` removes matching assertions and recomputes as if they had never been recorded — a surviving provider-identified account or a second valid assertion still stands, so retracting does not force a window back to `unattributed`. Retract mode selects over the recorded assertions rather than over observations, so it can still reach an assertion whose window has since disappeared from the store.

**Running it twice is safe.** A second identical apply finds every window already asserted, records nothing, changes nothing, and exits 0.

**Applying briefly blocks other cctally writes.** `--yes` holds the same database locks `cctally cache-sync --rebuild` and `cctally db rederive` take, for as long as it takes to re-check the plan and move both axes — measured at roughly half a minute on a store with years of history. Codex hook ticks that arrive during that window fall back to a read rather than failing. A preview takes no locks at all.

```bash
# See what would happen.
cctally account attribute work --since 2025-12-01T00:00:00Z

# Apply it.
cctally account attribute work --since 2025-12-01T00:00:00Z --yes

# Change your mind.
cctally account attribute work --since 2025-12-01T00:00:00Z --retract --yes
```

`--json` emits the stamped-first camelCase envelope (`schemaVersion: 1`) with `status`, `mode`, `selector`, a `summary`, a `groups` array, an `actions` object counting what was written, and an `errors` array. `selector.until` is always the resolved exclusive end, and `selector.untilSpecified` says whether you named that instant or the command defaulted it to the moment it ran. The `summary` counts the selected / eligible / no-op / refused windows, and `summary.blockingRefusedGroups` is the subset of the refused ones that stop the run.

`cctally doctor` reports the health of recorded assertions under `accounts.codex_window_attribution` — an assertion that matches no current window, matches more than one, or conflicts with recorded evidence is reported at WARN with the retraction remedy.

## Account refs

Everywhere a `<ref>` is accepted (`show`, `label`, and the `--account` filter below), it is resolved **case-insensitively** in this order:

1. label (exact, case-insensitive),
2. email (exact, case-insensitive),
3. a unique `account_key` prefix (the 32-hex opaque key).

The literal `unattributed` is accepted for the pre-feature / unresolved bucket by `show`, `label` and the `--account` filter. It is **not** accepted by `attribute`, which requires a real Codex account: `unattributed` means "the account could not be determined", so it is never something an operator can assert data belongs to. An ambiguous or unknown ref exits **2** with the candidate keys printed on stderr.

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

An account with no recorded usage renders an explicit "no Codex activity recorded" note with its percentage and reset left blank, rather than continuing to show whichever account you were looking at before. An account that has spend but no live cycle keeps its spend and leaves only the percentage and reset blank; that spend is a **trailing summary**, covering the last seven days rather than everything on record, and the card says so. The reserved `unattributed` bucket stays selectable and shows totals only — it holds Codex history recorded before per-file attribution existed, which is deliberately never guessed at — and its card total is the same trailing summary, so a bucket whose spend is all older than seven days shows `$0.00` for the window while keeping its card. In both cases the account's own period, session and project views still cover the wider range under their own labels; only the card, whose figure is summed into the week-labelled headline, is narrowed. Alerts that belong to the whole vendor rather than to one account, such as a vendor-wide budget crossing, stay visible under a focused account and are labelled `vendor-wide`.

Under **All accounts** the hero headline is the merged spend and token total across every account, including the unattributed bucket — the same figures the cards below it add up to. The headline percentage, reset, `$/1%`, forecast and week range are deliberately blank instead, because independent quota allowances are never blended into a single number and no single account's cycle window describes the whole; each blank carries a `per account` pointer to the cards, where each account's own percentage, reset and spend appear. Opening the cycle modal from that hero shows the same per-account table rather than any one account's milestone ladder — pick an account chip first to see a ladder. A single-account install is unchanged throughout.

**Claude — the chip is a hero decoration only.** Claude usage is stamped per account and reported per account by `--account` on the CLI, but the dashboard does not yet split its panels by account. Sessions and Recent alerts therefore keep an explicit `all accounts (unfiltered)` caption while a Claude account is selected, so the panel never implies a filter it is not applying.

## Alerts

Alert notification text gains a `[<label>]` prefix only when the vendor has more than one real account. The `alerts.log` file carries the account key as its trailing (8th) tab-delimited field on every line (`*` for vendor-wide rows).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success — including an `attribute` preview, an empty selection, and a no-op apply. |
| 2 | Ambiguous or unknown account ref (candidates on stderr); an `attribute` validation failure or refusal. |
| 3 | `--account` requested but the entry cache is unavailable; an `attribute` operational failure, including a run whose assertions were recorded but whose later steps did not finish (rerun the same command to complete it). |

## See also

- `docs/commands/budget.md` — the budget subcommand and vendor-wide budgets.
- `docs/commands/doctor.md` — the `accounts.*` health legs.
