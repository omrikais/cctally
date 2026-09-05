# `cctally quota`

Reports how much of your weekly Claude quota your usage consumes, computed from your own token history against a budget fitted from that same history, and reports whether the provider has changed the metering rate.

```bash
cctally quota
cctally quota --json
cctally quota --since 2026-08-01
cctally quota --account work@example.com
cctally quota --reset-calibration
```

## What the fitted value is, and what it is not

The command fits one number: the **effective blended weighted units per general-weekly point**. Weighted units are token counts scaled by four fixed coefficients — fresh input at 1.0, output at 4.73, one-hour cache writes at 1.03 and cache reads at 0.0031 — and the fitted value is the total weighted units your history spent divided by the total meter movement it produced.

Four disclosures follow from how that value was obtained, and none of them is a caveat you can safely skip.

**It is not a provider budget.** It is one blended exchange rate, valid for the mixture of models and token classes your own retained history shows. It says nothing about any individual model family and it is not an Opus budget, a Sonnet budget or a plan allowance.

**It is the provider's budget minus an unmeasured, workload-dependent amount.** Your quota is charged for work this machine cannot see — usage from claude.ai, the desktop app, another machine, and the summarisation call a compaction makes, which carries no `usage` block and never reaches local session data. Every one of those raises the meter without raising the local token total, so the fitted units-per-point comes out lower than the provider's own figure by however much of that you do.

**No per-family multiplier ships.** Every family known to drain the general weekly meter contributes through the same four token-class coefficients. The research prototype carried a `MODEL_WEIGHT_DEFAULT` of 0.70 for anything it did not recognise; nothing like it ships, and an unrecognised family withholds the day instead.

**The coefficients were calibrated on Opus-5-dominated traffic.** The analysis start is therefore floored at 2026-07-25, the Opus 5 changeover, whatever `--since` you pass. Earlier history is a different metering era whose composition these coefficients have no validation for, and it is excluded rather than fitted. If your entire retained history predates that date the command reports `unvalidated-coefficient-era` and withholds.

**Fast mode is paid outside the subscription quota.** Anthropic's [Fast mode documentation](https://code.claude.com/docs/en/fast-mode) says subscription fast-mode requests draw directly from usage credits even while plan quota remains, so rows recorded with `speed = fast` are excluded from the weekly-plan fit and counted in `health.fastModeEntriesExcluded`. Standard and unmarked rows keep their existing treatment.

## Flags

| Flag | Effect |
| --- | --- |
| `--since DATE` | Ignore data before this ISO date or timestamp. It narrows the window and never widens it past the supported-composition floor. |
| `--watch-from DATE` | Evaluate this split date instead of scanning for one. It bypasses the scan and the multiplicity correction, never the health or fit gates. A run passing it persists nothing. |
| `--account REF` | Scope to one account. The reference resolves case-insensitively as a label, an email or a unique key prefix. |
| `--json` | Emit the machine-readable payload described below (`schemaVersion: 1`). |
| `--reset-calibration` | Discard the stored calibration for the selected account. Idempotent: resetting an absent calibration succeeds and says nothing was stored. |

`--since` and `--watch-from` both change what the command measures, so both can change the verdict. A narrower window means fewer days in the rank-sum population, a different Holm family and different interval widths. That is the point of the flags, and it is why a run passing `--watch-from` writes nothing durable.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | A trustworthy calibration and no rate change. |
| `1` | A rate change is confirmed. Following `pricing-check`, `1` here means an actionable finding rather than bad input. |
| `2` | An argument or validation error, and nothing else. |
| `3` | Judgment withheld: the data is unhealthy, a store is unavailable, or a reason blocks the verdict. |
| `4` | The evidence is healthy but too thin, too fragmented or too unstable to fit. |

**Detection and prediction are gated separately, so the status and the exit code are not the same axis.** Confirming a rate change needs at least fourteen baseline days at 10% interval width, at least three consecutive watch days at 15%, strictly disjoint intervals and a Holm-corrected p inside 0.01. Predicting from a calibration needs five eligible days at 15%. A change confirmed at three watch days therefore reports `insufficient-history` and exit `1` at the same time: the detector has independently cleared its own stricter gates while the successor regime is still two days short of being predictable from. In that window the fitted value, the consumption, the projection and the headroom are all withheld and the two regimes' fits are published as evidence instead.

**A withheld verdict is not a finding of no rate change.** When the command withholds, it says so in as many words, because "the evidence needed to decide is missing" and "your metering is unchanged" are different claims and conflating them is the failure this wording exists to prevent.

## What a successful run publishes

For the current subscription week: the modelled consumption in weekly points with its interval, the observed meter reading, the difference between them, the headroom to 100%, and the projection to the week's end. Each is either available with a value, an interval and its population, or withheld with a typed cause — never zeroed. Alongside them: the fitted units-per-point, the baseline and current family-share and token-class-share vectors, both support radii, and an explicit marker that Fable 5 and Mythos also drain dedicated pools this model does not cover.

A mid-week credit does not re-anchor the week when it comes from `record-credit`, so the week's units are counted from the post-credit slice rather than from the week anchor. Counting from the anchor would divide a whole week's tokens by a meter that was reset partway through.

### The two radii are effective, not observed

The command publishes two composition-support radii, one over the family shares and one over the raw token-class shares, and a day is supported only when it lies inside both. **Each published radius is the effective one: `max(observed spread, materiality floor)`.** The observed spread is published separately.

This matters because the two can differ by everything. A single-model user's observed family spread is exactly `0.0`, and the effective radius is then the `0.03` family floor. Reading `0.03` as a measurement of that user's variation would be wrong in both directions: the observed variation is zero, and the number that decides support is not a measurement at all. The floor is the smallest clean bound above the corrected supported maximum of `0.0274`; the corrected empirical radius is `0.0934`, so the measured store's own outlier decision is unchanged. The floor exists precisely so that a user who adds a small share of a second model is not locked out of their own calibration by a radius that collapsed to zero.

The terminal report prints both, labelled. The JSON publishes `composition.familyRadiusEffective` and `composition.familyRadiusEmpirical`, and the same pair for the token-class axis.

## The day a new Claude family ships

**Every day containing a model this build does not recognise is withheld as `unsupported-model-mix` at exit `3`, and it stays withheld until the family catalogue is updated.** A family's participation in the general weekly quota is not derivable from token counts, so an unrecognised name is treated as unknown rather than assumed. That is deliberate — assuming a new family drains the general meter at the shared exchange rate is exactly the kind of silent, wrong answer this command exists to avoid — but the consequence is worth stating plainly: on the day Anthropic ships a new family, this command stops answering for anyone using it, and stays stopped until a release adds the family to the catalogue.

Updating the catalogue changes the constants payload, so `QUOTA_MODEL_CONSTANTS_FINGERPRINT` must be re-pinned in the same change. **Re-pinning it marks every persisted calibration stale**, on every machine, because each stored regime records the fingerprint it was fitted under and a mismatch means the stored value was fitted under constants that no longer apply. The command does not act on a stale regime; it fits a fresh one, appends it, and leaves the old value untouched, because that value was true under its own constants. A user therefore sees one run reporting `stale` only if their fresh evidence is too thin to fit — otherwise the calibration self-heals on the first run under the new build.

## When the metering rate changes

A metering-rate transition is **recorded as a durable event when the detector confirms it**, whatever your alert configuration says. That is deliberate: the history exists from the first upgrade, so `cctally doctor`'s Quota category and this command both report the change with no configuration at all, and a user who later turns notification on finds the history already there rather than starting empty.

Recording keys off the **detector**, not off this command's verdict, for one class of withholding reason. A transition is recorded even when `quota` withholds its own verdict, provided the reason concerns predicting forward rather than the trustworthiness of the detector's own inputs — concretely, when the current week's composition sits outside the support of the calibration it is measured against. It is **not** recorded when a day inside the **published window the successor era was classified over** was itself withheld for a reason that removes it from that population — a day whose model family does not participate in the weekly pool, a decisive watch day whose own composition is unsupported, or a day whose token split could not be computed. That check reads the successor window and nothing earlier, so a withheld day falling **before** the detected split does not refuse recording, even though the detector drops it from its rank-sum population too. Every other blocking condition refuses, including a blocking reason that withholds the verdict while the status stays healthy.

So `doctor` reporting a rate change while `quota` withholds its verdict is not a contradiction. The two answer different questions: `doctor` reports durable history the detector qualified for, and `quota` refuses a **current predictive** verdict it cannot stand behind. A run in that state persists both regimes, stamps the successor with the withholding status, and keeps reporting the same status, verdict and exit code it reported before.

The desktop **notification** is opt-in and off by default, like every other alert toggle, so an upgrade never produces a surprise popup. It needs two booleans in `~/.local/share/cctally/config.json`:

```json
{"alerts": {"enabled": true, "rate_change_enabled": true}}
```

`alerts.rate_change_enabled` alone does nothing, and neither does `alerts.enabled` alone — the same two-switch rule the Codex quota axis uses. Both are validated on read, and a non-boolean is a configuration error rather than a silently truthy value.

The event is keyed on `(provider, account, effective instant)` and each key gets **one dispatch opportunity**. That promise is stated at exactly that precision: a revised detector can pick a different effective instant for the same underlying provider transition, and a store that grows a second account can turn a formerly merged identity into a real account key. Either produces a new key for a change you would call the same one.

"One dispatch opportunity" is weaker than "notifies exactly once", and the difference is worth stating because it is what you can actually rely on. A recording interrupted **before** its durable decision is marked — the ingest cycle rolling back after it appended, the process dying between the commit and the mark — is retried on the next `cctally quota` run, which is what a delivery record buys. An interruption **after** the mark is not retried: the mark is written before the notifier is reached, exactly as `alerted_at` is written before the notifier `Popen` for every other alert axis, so a notifier that fails or a process that dies during dispatch loses that one notification permanently. Two cases are not recovered at all: a notification owed before this feature shipped, because nothing in the store distinguishes a historical row that notified from one that did not, and a notification owed at the moment the delivery record is re-initialized (see below).

The retry covers the accounts the run analysed plus the unattributed bucket, which is always retried whatever `--account` you gave, because that bucket records the absence of an account rather than a second one.

You can rehearse this notification without waiting for a real transition: `cctally alerts test --axis meter-rate-change` builds a synthetic change and sends it through the same payload builder and dispatch path a real one uses. It takes no `--threshold` — this family has no percentage threshold — and supplying one exits 2. `cctally alerts test --axis quota --threshold 90` does the same for the Codex quota notification below.

On an install with more than one real account the notification's **title names the account** whose rate changed, using the same `[label]` prefix every other cctally alert carries. A transition recorded against the merged view — the bucket an install below the decoration threshold is analysed under — is named `Unattributed`, because that bucket records the absence of an account rather than a second one. On an install with one account, or none, the notification is exactly what it was before: no prefix, and every word unchanged.

Two things never fire it: the first regime a new install fits, which has no predecessor to have changed from, and a recalculation under new pricing constants, which closes the old regime as stale rather than recording a rate transition.

Set the toggle with `cctally config set alerts.rate_change_enabled true`, or from the dashboard's Settings overlay under Alerts. It needs `alerts.enabled` as well, because it decides only whether a recorded transition ALSO notifies.

## Multiple accounts

With one Claude account, or a lone unattributed bucket, the output is undecorated and the command analyses one population. Above one real account each account is analysed and reported **separately**, because two accounts have independent weekly meters and one budget fitted across both describes neither. The invocation's exit code is the most severe across the accounts, where a confirmed finding outranks a degraded or thin one.

`--reset-calibration` is selected-account-only. Without `--account` on an install with more than one Claude account it refuses with a usage error naming the candidates rather than clearing every regime.

## Where the calibration is stored

`~/.local/share/cctally/quota-calibrations.json`, serialized by a sibling lock file, both at mode `0600`. It lives in neither database on purpose: `cache.db` is declared fully re-derivable and `cache-sync --rebuild` clears keys there, and a `stats.db` table is re-materialized from the journal on `db rebuild --db stats`. A calibration in either would be silently discarded by an ordinary maintenance command.

The file is machine-owned internal state. It is not in `config get`/`config set` and never will be; you read it through this command and clear it with `--reset-calibration`.

A file this build cannot parse, or one stamped with a schema version from a newer build, is **renamed aside with a timestamped suffix and reported** rather than overwritten. It may be the only surviving record of budgets whose source rows have since been pruned.

### Where the notification-delivery record is stored

`~/.local/share/cctally/quota-rate-change-notification-decisions.json`, with its own sibling lock. It records which `(provider, account, effective instant)` keys have already had a dispatch opportunity claimed, and it is a separate file from the calibration for the same reason the calibration is separate from both databases: a `stats.db` rebuild replays the journal, so a delivery record derived from journal events would come back empty and replay your whole notification history.

It exists because a rate-change row can reach the store with no notification. An ingest cycle that appends its journal line and then rolls back leaves that line behind, and the next replay recreates the row with no way to notify from. `cctally quota` compares the recorded transitions against this record and delivers the ones that were never offered. On the first run after this feature ships the file is seeded with every transition already recorded, so upgrading does not announce your whole history at once.

Deleting it is safe: the next `cctally quota` re-seeds it from the recorded transitions, at the cost named earlier — a notification that was genuinely still owed at the moment you deleted the file is seeded as already decided and is not delivered.

Unlike the calibration, a delivery record this build cannot read is **left exactly as it is**, neither renamed aside nor overwritten, because the file is the only record of what has already been announced and both discarding it and reading it as empty would announce everything again. The run reports the file on stderr and skips the retry pass for that invocation. Rate changes are still recorded and still notified while it is unusable; only the retry of an earlier missed one waits until you repair or remove it.

## JSON

Stamped through the shared envelope, camelCase, `schemaVersion: 1`. Top-level keys, with one analysed population:

```
schemaVersion, generatedAt, status, verdict, exitCode,
scope       — accountKey, accountLabel, merged, analysisStart, invocation
calibration — recorded (the durable prior, or null), fitted
analysis    — baseline.fit, watch.fit
currentWeek — start, end, observedPercent, observedMinusModelled,
              consumption, projection, headroom
composition — baseline/current share vectors, both effective radii,
              both empirical radii, radiusRule
health      — entriesThrough, day counts, the eligibility fence,
              unattributed units and entries, the excluded in-progress day,
              excluded fast-mode entries,
              unrecognised snapshot sources, any quarantine path
method      — algorithmRevision, constantsFingerprint, verifiedAt,
              supportedCompositionFrom, coefficientSupport, coefficientEras, detector,
              dedicatedPoolScope
blocking    — the closed set of reasons that withheld the verdict
```

Above one real account the per-account payloads appear under `accounts`, and the top level carries the worst status, verdict and exit code across them.

Every calibration value takes the same evidence shape: `state` is `available` or `withheld`; an available value carries `value`, `interval`, `support` and `population`; a withheld one carries a typed `code` and no value at all. There is no confidence field on any of them, because the design never defined what a confidence would mean here.

Every JSON timestamp is RFC 3339 UTC with a trailing `Z`, including timestamps read back from an older calibration file that used an explicit `+00:00` offset.

`verdict` is the closed set `rate-change-detected | no-rate-change | withheld`. `status` is the closed set `ok`, `insufficient-history`, `fragmented-history`, `unstable-fit`, `local-history-incomplete`, `unsupported-model-mix`, `unvalidated-coefficient-era`, `token-split-unknown`, `stale`, `future`, `unavailable`.

**Stable:** the top-level keys, the enum meanings, the evidence shape, the interval semantics and the account-scoping semantics. **Additive:** per-day diagnostics, health counters and method metadata. Consumers must tolerate unknown keys.

### The detector block discloses its own bound

`method.detector` carries `inputEligibleDays`, `scannedEligibleDays`, `truncatedEligibleDays`, `scanStartDate`, `maxAutoScanDays` and `historyTruncated`. The automatic scan is bounded to the most recent 64 eligible days, and that retained set is the complete rank-sum population — the mid-ranks, every admissible split and the Holm correction are all computed inside it. The bound exists because the exact conditional test costs roughly the fifth power of the population size: measured on the maintainer's host, 30 days took 0.011 seconds and 201 days took 373 seconds.

A bounded exact scan is still valid evidence, so truncation is **not** a status and does not mark the calibration unhealthy. It is disclosed instead: when `historyTruncated` is true the terminal report states that only the most recent 64 of N eligible days were scanned and where the scan started.

The two date bounds compose in a fixed order. The 2026-07-25 coefficient-era floor first removes unsupported history; the detector then takes at most the most recent 64 eligible days from what remains. The eligibility fence, including the baseline side of a confirmed split, is computed from that same bounded population, so pre-horizon volume cannot change which scanned days qualify.

## What is out of scope

The five-hour meter. Fable 5's and Mythos's dedicated pools — all three families drain the general weekly meter as well, and only that draining is modelled. Any statement about an individual family's exchange rate. Named budget or promotional hypotheses, which are not reported at any support level.

## Related

- `docs/cli-contract.md` — the exit-code taxonomy and the JSON envelope convention.
- `docs/claude-cost-coverage.md` — why local cost is a lower bound, and why that refusal is scoped to cost surfaces rather than to this one.
- `docs/commands/account.md` — account references and the decoration gate.
