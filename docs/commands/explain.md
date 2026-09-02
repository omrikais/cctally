# `cctally explain`

Explain where locally retained usage went, over seven contributor classes — four accounting-native and three conversation-derived — ranked by observed cost and backed by evidence you can navigate to.

`explain` answers one question: which subjects account for the money this window spent. It never gives behavioural advice, never projects a quota percentage onto a slice of spend, and never reports a number without stating the population it was measured over.

> **Cost coverage:** Claude dollar and token totals are [transcript-derived lower bounds](../claude-cost-coverage.md), not exact `/usage` billing totals. `explain` says so on both surfaces: every report states that it explains locally retained cost and tokens.

## Usage

```bash
cctally explain                                    # the current subscription week, Claude
cctally explain --source all                       # each provider in its own section
cctally explain --window last-week --json          # the stamped JSON envelope
cctally explain --window 2026-08-10..2026-08-16    # an explicit date range
cctally explain --start-at 2026-08-10T00:00:00Z \
                --end-at 2026-08-17T00:00:00Z      # exact instants, half-open
cctally explain --account work --reveal-projects   # one account, project labels shown
```

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--source {claude,codex,all}` | `claude` | Which provider to diagnose. `all` renders one section per provider, each with its own denominator and verdict. |
| `--account REF` | none | Scope to one account. The ref resolves case-insensitively by label, email or unique key prefix. Rejected with `--source all`. |
| `--window TOKEN` | `this-week` | The window to diagnose. Same grammar as `cctally diff`: `this-week`, `last-week`, `Nw-ago`, `this-month`, `last-month`, `Nm-ago`, `last-Nd`, `prev-Nd`, or `YYYY-MM-DD..YYYY-MM-DD`. |
| `--start-at ISO` | none | Exact window start instant, **inclusive**. Requires `--end-at`, and cannot be combined with `--window`. |
| `--end-at ISO` | none | Exact window end instant, **exclusive**. Requires `--start-at`, and cannot be combined with `--window`. |
| `--speed {auto,standard,fast}` | `auto` | The Codex pricing tier applied to Codex accounting. |
| `--tz TZ` | config `display.tz` | Display timezone: `local`, `utc`, or an IANA name. |
| `--json` | off | Emit the stamped JSON envelope instead of the terminal report. |
| `--reveal-projects` | off | Show derived project display labels instead of the default `project-1`, `project-2`, … aliases. |

**`--start-at` / `--end-at` exist because date grammar cannot name the window a warning fired against.** A five-hour block starts at an instant, not on a calendar day, so the pair the dashboard route has always accepted is now on the CLI too — and it is what every conversation-derived class's next step carries, so following one reproduces the population the row measured rather than a day that contains it. A **date-only** value is refused rather than read as midnight in some zone, which is the rule `five-hour-breakdown --block-start` already applies; a naive datetime is read as UTC, and a full-ISO form carries its own offset and is timezone-independent. An inverted or malformed pair is exit 2, the code a malformed `--window` token gets.

`explain` does not take the rest of the shareable-output surface (`--format`, `--theme`, `--output`, `--copy`, `--open`). That surface is a separate concern with its own render kernel.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A valid report. This includes a healthy report in which no class found a contributor, and a report withheld as `retained_range_mismatch`, `stale_evidence`, `pricing_unavailable` or `insufficient_population`: those are correct answers about what the store holds, not failures. |
| 2 | A selector or usage error: `--account` combined with `--source all`, an unparseable window token, an ambiguous or unknown account ref. An `--account` ref is unresolvable — and so exit 2 — whatever the reason it could not be resolved, including a machine that holds no account registry at all. |
| 3 | A report-establishment failure, and the unreadable-store case below. **Every** establishment error is exit 3 — `range_unresolved` and `account_unresolved` as much as `generation_incoherent`. |

Exit 1 is not used by `explain`.

The split is by *when* the failure is decided, not by which code it carries. Argument-shaped validation fails before any store is opened and is exit 2. Once the report is being established, any failure to establish it is exit 3, because at that point the command has accepted its arguments and cannot answer.

**A store that could not be read prints its report AND exits 3.** When a requested provider's store cannot be read — `provider_unavailable`, whether the accounting table would not resolve or the store would not open at all — and no other requested provider produced a measurement, `explain` renders the withheld report and exits 3. A person then sees the typed cause and a script sees the failure; exiting without a report gave the person a number and no statement of why. Under `--source all`, one provider withheld while the other reports is a report, and exits 0.

That rule is deliberately narrow. An unreadable store is an infrastructure failure; every other withheld cause is a legitimate answer about data availability, and reporting those as failures would train you to ignore the one that is not.

## The seven contributor classes

Every class is evaluated on every run. A class that finds a contributor is ranked with the others; a class that finds none is listed as such; a class that could not be measured is listed with its typed cause.

| Class | Subject | Support minimum | Healthy inverse |
|---|---|---|---|
| Expensive model mix | model | at least 2 distinct models and at least 20 priced entries | no model reaches the floor |
| One project dominating | resolved project key | at least 2 distinct projects and at least 20 priced entries | no project reaches the floor |
| One session dominating | session or conversation identity | at least 2 distinct sessions and at least 20 priced entries | no session reaches the floor |
| Concentrated 5-hour bursts | provider-native block | at least 2 native blocks and at least 20 priced entries | no block reaches the floor |
| Prompt-cache churn | the qualifying set | at least 20 evaluated priced entries | no turn rebuilt its cache |
| Short conversations carrying large context | the qualifying set | at least 20 evaluated priced entries | no conversation qualified |
| Subagent fan-out | the qualifying set | at least 20 evaluated priced entries | nothing was delegated |

The support unit for every class is the count of priced accounting entries attributed to that class's subjects. For the four accounting-native classes that is the attributed subset; for the three conversation-derived ones it is the **evaluated** population — see *The three populations* below.

**The three conversation-derived classes each contribute ONE aggregate subject**, the qualifying set, whose observed cost is that set's in-window retained cost. Members never appear as rows; they appear as evidence fields beneath the row, and the subject key, kind and label are constants of the class rather than anything derived from a member. That is why their distinct-subject minimum is 1 rather than 2.

### The published rule

Each conversation-derived class publishes the predicate that produced its verdict, as a human sentence and as machine-readable parameters, in `constants.registry[].rule`. Both surfaces render the sentence, so a verdict can be reproduced rather than trusted.

| Class | Rule |
|---|---|
| Prompt-cache churn | Turns whose prompt cache was rebuilt: the cached prefix collapsed and was re-created in the same turn. Claude only — Codex retains a cached-input ratio and no loss predicate, so the class is `not_applicable` there. |
| Short conversations carrying large context | Conversations with at most `shortConversationMaxHumanTurns` (3) human turns where at least one request used at least `largeContextMinWindowFraction` (0.80) of **that request's own** model context window. With several replies the largest single request fraction is used, never their sum. |
| Subagent fan-out | Cost attributable to delegated subagent work, grouped by parent, where a parent has at least `minSubagentBuckets` (2) distinct buckets. On Codex this is an identifiable subset of unknown completeness. |

**The turn predicate is published too**, as `constants.turnDefinition`, and it is not the physical message count: `conversation_sessions.msg_count` groups `conversation_messages` with no sidechain predicate, while subagent files carry the parent's session id. A human turn is a main-thread human message — non-sidechain, non-subagent, after command and meta normalisation — and assistant replies associate with the nearest preceding human.

**Turn counts span the whole retained conversation while cost stays window-clipped.** "Short" is a property of the conversation, and a window-clipped count would call a long conversation short whenever the window caught only its tail. A qualifying conversation still contributes only its in-window retained cost.

### The three populations

The conversation-derived classes distinguish three populations, and every coverage figure names its own in `coverage.dimensions`:

| Population | Meaning |
|---|---|
| candidate | every in-window priced entry eligible for an attempted evaluation |
| evaluated | the candidates whose predicate could actually be decided |
| qualifying | the evaluated entries the predicate matched |

`evaluabilityCoverage` is `evaluated / candidate`, and it is the **only** dimension that falls when a row could not be decided — an unresolvable subagent identity, a parent matching zero or several threads, an ambiguous Codex origin, an unknown context window, an exhausted scan budget. `identityCoverage` cannot also fall for those rows, because a population that excludes them has perfect identity coverage by construction. `supportUnits` and `usdCoverage` are measured over the **evaluated** population and never over the qualifying set: counting qualifiers would make confidence rise precisely as the problem worsens.

### Which classes read the transcript store

| Class | Claude | Codex |
|---|---|---|
| the four accounting-native classes | `cache.db` and `stats.db` | `cache.db` and `stats.db` |
| Prompt-cache churn | `conversations.db` | not applicable |
| Short conversations carrying large context | `conversations.db` | `conversations.db` |
| Subagent fan-out | `conversations.db` | `cache.db` only |

**Codex subagent fan-out needs no transcript access at all**, because `codex_conversation_threads` and `codex_session_entries` both live in `cache.db`. On a dashboard that is not authorized to read transcripts, that class is therefore measured while Claude's is withheld — the same class name, two verdicts, in one report. Both surfaces state that in words on the withheld row.

A class that needs a store this run may not read reports `transcripts_not_visible`; a class whose authorized read found the store absent, unreadable, or predating a column these statements select reports `signal_unavailable`. Authorization is decided before availability, so a denied class reports the denial even where the store is also missing.

**The classification floor is 0.20.** A class reports `contributor` when its top subject's share of the class denominator is at least 0.20 **and** its support minimum is met. The boundary is inclusive: a share of exactly 0.20 is a contributor. It reports `no_contributor` when support is met and the top share falls below the floor, which is a measured answer rather than a withheld one. It reports `withheld` with `insufficient_population` when support is not met, and names which minimum it missed — see *The support shortfall* below.

**The exact comparison is `share >= 0.20 - shareFloorEpsilon`, and both halves are published.** A share is a ratio of two independently accumulated float sums, so a subject genuinely at one fifth of the denominator computes as `0.19999999999999998`. The JSON therefore publishes `constants.shareFloorEpsilon` (`1e-9`) beside `constants.contributorShareFloor`, because a client applying a bare `share >= floor` to the published share would compute `no_contributor` on a row the server published as `contributor` — the constants exist so a reader can apply the rule that produced the verdict rather than infer it.

### The support shortfall

`insufficient_population` is one cause for four different shortfalls, and the support count printed beside it belongs to only one of them: a class with sixty priced entries under a single model has ample entries and still fails the distinct-subject minimum. Each withheld class therefore states which minimum it missed, as `supportShortfall` on the wire and as a phrase on the terminal line.

| `supportShortfall` | Meaning |
|---|---|
| `min_distinct_subjects` | Fewer distinct subjects than the class requires. |
| `min_priced_entries` | Fewer priced entries than the class requires. |
| `min_usd_coverage` | Dollar coverage below 0.50. |
| `no_priced_dollars` | There is a population and its retained cost is zero, so no share can be divided. |

It is `null` for every other verdict, and for a class a provider-wide cause preempted: that class never shaped its subjects, so it has no minimum to have failed.

**A withheld class states no confidence.** Confidence is a statement about a measurement, and a withheld class made none.

Ties are resolved by reporting every subject within `1e-9` USD of the top, ordered by subject key.

**The classes are not mutually exclusive, so shares do not sum to 100 percent.** One session is also a project and a model. Both surfaces state this.

Ranking is `(-observedUsd, registryOrder, subject.key)`. Registry order is the fixed order of the table above.

### The overall verdict

**The two verdicts are deliberately asymmetric, because they claim different things.**

`no_contributor_detected` is a claim about the whole population: nothing in this window reached the floor. That claim is only true if every applicable class was measured, so a single withheld applicable class withholds the overall verdict and its cause is reported.

`contributor_detected` claims only that a contributor was found. That stays true beside a withheld sibling class, so a measured contributor is never erased by an unmeasurable one. Because it is therefore *not* a complete account of the window, both surfaces state how many applicable classes were withheld alongside it: the terminal appends `[N of M applicable classes withheld]` to the provider and overall verdict lines, and the JSON publishes `withheldClassCount` and `applicableClassCount` at the report level and on every result.

A `not_applicable` class is excluded from the completeness test: it means the provider structurally cannot support the class, which is not a failed measurement. The terminal gives it its own heading — `Classes this provider does not support:` — rather than filing it beside the withheld ones, because those two states are exactly what the asymmetry above rests on. No S2 class is `not_applicable` for either provider; the state exists because the conversation-derived classes that follow include one that is.

## The three cause layers

These layers are distinct and never mixed, because a report-establishment failure is not an evidence field.

**Report-establishment errors** end the request and produce exit 3 (or, on the dashboard, a 4xx or 5xx status): `range_unresolved`, `account_unresolved`, `store_unavailable`, `generation_incoherent`.

A relative `--window` token needs the subscription-week anchor, which is a read of `weekly_usage_snapshots` in stats.db. Every way that read can fail is `store_unavailable` at exit 3, including a stored timestamp that is not text: stats.db is a non-STRICT SQLite database, so a BLOB survives in a column declared `TEXT` and the parse raises a type error. That is a statement about the store, not about the `--window` token the user wrote.

**Evidence-withheld reasons** are field-level and closed on the server. They are published in this precedence order, and the overall verdict reports the highest-precedence cause among its withheld classes:

| Cause | When it fires |
|---|---|
| `provider_unavailable` | This provider's store could not be read at all — an older `cache.db` carries no Codex tables, for example, or the store is absent. Under `--source all` the other provider still reports; the unreadable one withholds every class and its denominator, and the withheld denominator carries the failure's own message when it has one. A Codex quota projection that is merely incomplete names its remedy there: `run \`cctally cache-sync\` to reconcile it`. This is the one withheld cause that also sets the exit code — see *Exit codes*. |
| `retained_range_mismatch` | The store retains no part of the requested window. |
| `pricing_unavailable` | There is a population and no price for any of it. |
| `unattributed_evidence` | Every subject's identity is unresolved, so no subject can be named. |
| `stale_evidence` | The store retains less than half of the part of the requested window it could have answered for. See *Retention* below — a young install is not a pruned one. |
| `insufficient_population` | Support is not met, or dollar coverage is below 0.50. |
| `calculation_failed` | A loader or evaluator raised. An exception is never rendered as a healthy result. |

Two of those causes are conversation-derived and sit at their own precedence positions. `transcripts_not_visible` goes directly after `provider_unavailable`, because it is an authorization denial decided before any store opens. `signal_unavailable` goes after `stale_evidence` and before `insufficient_population`: a signal that could not be established at all is more fundamental than one established with too little support, and a pruned store is the more informative root cause when both apply.

| Cause | When it fires |
|---|---|
| `transcripts_not_visible` | This surface is not authorized to read transcripts. Only the dashboard can produce it — the CLI reads this machine's own stores directly. The class is withheld; the accounting classes still measure, and the response is 200 rather than 403, because a whole-route denial would discard evidence the request IS authorized to see. |
| `signal_unavailable` | An AUTHORIZED read could not establish the signal: the store is absent, unreadable, or predates a column these statements select. It is never `provider_unavailable`, so it never turns an answered report into exit 3. |

**Baseline outcomes** are their own axis: `baseline_insufficient`. A thin baseline withholds only the baseline field; the observed-cost result still renders. A subject missing from an otherwise fully measured baseline is the value zero, not a cause — a model, project or session that appears this week and did not exist last week is the most informative case the baseline has.

The comparators are class-specific, and two of them are deliberately not per-key lookups:

| Class | Comparator |
|---|---|
| Expensive model mix | the same model's share of the preceding window's retained cost |
| One project dominating | the same project key's share |
| One session dominating | the maximum single-session share |
| Concentrated 5-hour bursts | the maximum provider-native block share within the subject's own Codex pool |

A session key is minted per session and a 5-hour block key embeds its start instant, so neither can appear in both windows. A per-key lookup for those two classes would miss every single time and read as a thin baseline when the baseline was complete.

## Coverage and confidence

Every measured quantity carries its population rather than standing alone. `PopulationCoverage` names its dimensions separately: `countCoverage` over priced entries, `usdCoverage` over repriced USD, `identityCoverage` over subjects that resolved to a stable key, `retentionCoverage` over the requested window actually retained, and `pricingCoverage` over entries priced without a fallback. An unmeasurable dimension is absent from the JSON rather than zero, because zero is a measurement and absence is not; `gapCodes` names why.

`countCoverage` and `usdCoverage` compare the class's attributed subset with the whole provider population, which is what they mean. `identityCoverage` and `pricingCoverage` are properties **of that attributed subset** and are computed over it, so a class never publishes a figure derived from a population other than its own. `retentionCoverage` is the one **provider-scoped** dimension: it compares the store's retained range with the requested window and has no per-class meaning.

**Confidence is computed on `usdCoverage`**, because a diagnosis ranked by dollars must gate on dollar coverage rather than row counts. Every class line that reports a measurement states the same population a contributor row states — support units first, then each coverage dimension that was measured — and closes with its confidence, so the two surfaces state the same facts in the same order. A `no_contributor` verdict admitted at 0.50 dollar coverage is a low-confidence answer and says so. A withheld class states its support and its shortfall instead, and no confidence: the JSON publishes `confidence: null` there, which is not the same as `low`.

| Level | Requires |
|---|---|
| `high` | complete coverage and at least 20 support units |
| `medium` | at least 0.80 coverage and at least 5 support units |
| `low` | anything else above the withholding floor |

Below 0.50 coverage, or fewer than 2 support units, the class verdict is withheld as `insufficient_population`. A low-confidence result above that floor stays visible and labelled. Staleness and unsupported signals are availability causes, not confidence levels.

A baseline comparison requires at least `medium` confidence. Baselines are the immediately preceding equal-duration window under the same provider, account, timezone interpretation, pricing snapshot and effective Codex speed, and never affect the current row's rank or verdict.

Every reported contributor row states the same population beneath its figures, on both surfaces and in the same words: support units first, then each coverage dimension that was measured. A dimension that could not be measured is omitted from that line rather than printed as `0%`.

**A class a provider-wide cause preempted publishes no figure it never measured.** That cause is decided before any class shapes its subjects, so the class attributed nothing: `countCoverage`, `usdCoverage`, `identityCoverage`, `pricingCoverage` and `supportUnits` are all absent (`supportUnits` is `null`), and `gapCodes` names the cause that preempted them. Publishing `usdCoverage: 0.0` there would state that this class covers none of the window's dollars, which is a measurement it never made. `retentionCoverage` and the observed bounds stay, because they are provider-scoped and were measured before any class existed.

### Retention

`retentionCoverage` answers one question: how much of the requested window could this store have answered for. The earliest retained accounting row alone cannot answer it, because a fresh install two days into the subscription week has the same shape as a store pruned five days back.

The discriminator is the install's own observation horizon — the earliest provider-native block `stats.db` holds, independent of the accounting rows. An install that was already observing before the window and holds no accounting rows for the earlier part has been pruned, and reports `stale_evidence`. An install whose horizon begins inside the window never covered the earlier part at all, so that interval is excluded from the denominator rather than counted against the store.

**Without that signal there is no figure, and the consequence is accepted.** Neither table carrying the horizon is written by the accounting path: `five_hour_blocks` comes from `record-usage` on the status-line hook, and `quota_window_blocks` needs the optional Codex hooks or a rollout ingest. An install that never wired either has no horizon at all, so `retentionCoverage` is **absent** rather than derived from the window start — deriving it would restore exactly the rule the horizon replaced and report a young store as a pruned one. A store with no provider-native blocks therefore never reports `stale_evidence`, because pruning and youth genuinely cannot be told apart without that signal.

### Coverage gap codes

`gapCodes` names what a coverage figure does not cover, and a contributor row carries its own qualifications:

| Code | Meaning |
|---|---|
| `unmatched_pool_window` | Some spend matched no compatible provider-native quota window, so it appears in no block. |
| `unresolved_project_identity` | Some entries have no resolvable project. |
| `block_precedes_window` | This block started before the requested window. Its dollars are still window-scoped; only entries inside the window are ever assigned to it. |
| `block_exceeds_window` | This block ends after the requested window, with the same scoping. |
| `unknown_context_window` | Some conversations had no published context window for any reply, so the fraction could not be computed. Never guessed at and never treated as small. |
| `scan_budget_exhausted` | Some subjects held more history than their allocated scan share, so the predicate could not be decided. Never classified short, long or mis-attributed. |
| `unresolved_subagent_attribution` | Some subagent-shaped spend could not be joined to exactly one parent. Its dollars are published separately as `unallocatedUsd` rather than folded in or discarded. |
| `ambiguous_origin_category` | Some Codex threads carried an origin category this build cannot read, or no thread row at all. That is spend this tree can say nothing about, so it contributes to no published figure — filing it as unallocated would overstate `unallocatedUsd`. |

The whole closed set is published as `constants.gapCodes`, so a client can tell a code from a newer server apart from one it should have handled.

Both rendered surfaces state the gap codes as one line led by the word `undecided`, once over the whole list — `undecided: unknown_context_window` on the terminal, and the same lead-in over the plain-English phrases in the dashboard modal. The line sits directly under the coverage line, and the lead-in is what separates the two.

### The evidence fields

Each conversation-derived row publishes its own evidence beneath it. Every member is a full evidence field, so one figure can be withheld while the row still reports its cost.

| Class | Evidence |
|---|---|
| Prompt-cache churn | `flaggedTurnCount`, `affectedConversationCount`, `estWastedUsd` |
| Short conversations carrying large context | `conversationCount`, `medianHumanTurns`, `maxContextWindowFraction` |
| Subagent fan-out | `identifiedSubagentCount`, `largestSubagentShare`, `unallocatedUsd` |

The names in that table are the **wire** vocabulary — what `--json` publishes and what a client keys on. The terminal and the dashboard modal both render them under human labels instead (`turns that rebuilt their cache`, `largest request as a share of its context window`), and the two surfaces use one label per figure. A figure a newer server publishes under a name this build does not know renders under that raw name rather than vanishing from the row.

`estWastedUsd` is a counterfactual and never the sort key: every class ranks on the retained cost of what it flagged, against the one denominator. `largestSubagentShare` divides by the **class's own** qualifying cost rather than by the report denominator, because it states how concentrated the fan-out is. `identifiedSubagentCount` carries `identifiable_subset_unknown_completeness` on Codex, and no completeness figure is published there.

On the wire each field carries its own `qualifications` list, so a consumer reads them per figure. Both rendered surfaces state a qualification **once per row**: a mark the row already states beside its observed cost is not repeated under a figure that also carries it, because the same words printed twice in one row read as two separate facts about two different figures.

### The three capped read paths

Four read paths are capped rather than structurally bounded — the per-conversation normalize budget governs the Claude and the Codex candidate statements alike. Each cap is allocated per subject so no session, conversation or source file can starve another and the result never depends on read order:

| Constant | Bounds |
|---|---|
| `seedScanBudgetRows` (50,000) | the compaction seed scan, split across the sessions in the window, with the current and the baseline window receiving separate budgets |
| `conversationNormalizeBudgetRows` (2,000) | the human-candidate rows normalized per conversation |
| `codexEventScanBudgetRows` (1,048,576) | the global pool for Codex turn-inference rows, split across source files |
| `codexEventScanPerFileRows` (4,096) | the maximum inference-essential rows read from one Codex source file; unrelated payload rows do not spend this budget |

A subject that exhausts its share is **unevaluable**: it leaves the evaluated population with a `scan_budget_exhausted` gap and lowers `evaluabilityCoverage`. It is never classified short, never long, and never silently mis-attributed.

## Next steps

Each reported contributor carries exactly one next step, always evidence navigation and never behavioural advice. The terminal renders it as `-> Run cctally …`; the JSON carries it as `nextStep`.

Each next step is generated in the provider-pinned subgroup form (`cctally claude daily …`, `cctally codex session …`) rather than with a `--source` flag, because `daily` and `session` register no `--source` of their own.

**No next step interpolates a subject key.** A subject key is store data — a model name, a git root, a conversation key, a composite block key — and none of it is guaranteed free of characters a shell interprets. `five_hour_bursts` is the one class whose next step names its subject, so it publishes no generic template at all: each block builder attaches the command for the block it just built, which is the only place the safe form is known. Claude blocks navigate to `cctally five-hour-breakdown --block-start <instant>` and Codex blocks to `cctally codex quota blocks`.

**Every conversation-derived next step is `cctally explain` over the same scope, with exact bounds.** It is transcript-free, valid in every configuration, and points at exactly the population the row measured: `cctally explain --source <provider> --start-at <instant> --end-at <instant> --json`. `cache-report` measures cache hit ratio and cost and `session` groups by session rather than by subagent bucket, so neither measures these populations.

**A next step reproduces this command's own accounting where it can.** `explain` reprices every Claude accounting entry at read time, while `cctally daily` and `cctally session` default to `-m auto`, which returns a stored `session_entries.cost_usd_raw` verbatim when one exists. Those next steps therefore carry `-m calculate`, so the dollars they show are the dollars that sent you there. Two targets cannot take that flag: `cctally project` and `cctally five-hour-breakdown` register no `-m/--mode` at all, so on a store holding stored Claude costs their totals may differ from the figure above them. The Codex subcommands register none either, and need none — Codex cost is always computed at read time.

## Privacy

Projects are anonymized by default using deterministic response-local aliases (`project-1`, `project-2`, … ordered by descending cost). `--reveal-projects` exposes the derived project display label only, never a filesystem path, and never widens the published key.

Sessions stay opaque stable keys in every mode. A session identifier is not a name a person chose, so revealing it buys nothing.

## Generation

Every report publishes a `generation` object: a **version vector with component-local consistency**, not a claim of an instantaneous cross-file cut. SQLite provides no such thing across separate files, and a content digest describes the bytes read rather than proving that independently opened snapshots coexisted.

Three components are always published — `stats`, `cache` and `configuration` — and a fourth, `conversations`, appears only when the plan actually read conversation bytes. Each component digest is computed over exactly the facts read from that store. Each is probed before and after its read; a component whose probe differs is re-read once, and a second divergence ends the request with `generation_incoherent`. On the dashboard that is a **retryable** state rather than a failure: a component that moved twice while it was read is a store under active write, and the same request a moment later normally succeeds.

The `conversations` component is digested over exactly the derived facts the report itself publishes or aggregates — the flagged counts, the qualifying booleans, the fractions and the aggregate USD. Per-row topology, opaque provider keys, raw text, blocks payloads, content digests, filesystem paths and identities reach neither that digest nor the wire, so it is an equality oracle only for facts the response body already discloses. Its probe covers `cache.db` as well, because prompt-cache churn reads `session_entries` off the cache connection after that component's own probe pair has closed, and a digest that cannot detect a change to the fact it binds is not a digest of that fact.

`generationId` is a SHA-256 over the contract version, the normalized scope, the whole execution plan and the component vector. The plan is part of the identity because a seven-class report with three withheld results is a different report from a four-class one and must not share an identifier with it — so a request denied transcript access publishes a different id from an authorized one over the same window, and the CLI-versus-route byte comparison is scoped to the same plan.

Two surfaces reading the same facts under the same plan publish the same id, which is what makes the CLI and the dashboard route comparable byte for byte.

The diagnosis opens every store **read-only**. The ordinary opener performs schema work, migration, legacy import, contract repair and replay, any of which would mutate a store the diagnosis is only reading.

## JSON envelope

`--json` emits a stamped-first camelCase envelope at `schemaVersion` 1. Its top level carries `contractVersion`, `measuredAt`, the normalized half-open `window` with its IANA zone, the classification `constants` including the registry, the two header `notes`, the `overallVerdict` and `overallCode`, and a `results[]` array with one entry per provider.

Each result states its `source`, `accountKey`, `effectiveSpeed`, `generation`, `denominator`, `coverage`, `verdict`, `code`, `applicableClassCount`, `withheldClassCount`, the ranked `contributors[]` and the per-class `classes[]`. Every class states its `verdict`, its `code`, its `supportShortfall`, its `confidence` (`null` when it measured nothing) and its `population`. Every share divides by the named denominator object, whose `identity` is always `totalExplainedRetainedCost`.

A subject's `qualifications` are attached once, to `observedUsd`. They are one statement about the subject, not a separate qualification of each figure derived from it.

**The denominator's `usd` is an `EvidenceField`, not a bare number.** It is withheld under the same provider-wide cause ladder every class is withheld under, and a withheld denominator publishes `{"state": "withheld", "value": null, "code": …}` rather than a figure. The terminal prints the cause in its place. A definite `$0.00 of locally retained cost` above four classes withheld as `pricing_unavailable` states something the data does not support: the true retained cost there is unknown, not zero.

Consumers must tolerate unknown keys. Adding an optional key does not bump `schemaVersion`.

## The same report on the dashboard

The dashboard serves this report from `GET /api/diagnosis`, through the same `build_diagnosis` and the same `diagnosis_to_wire` adapter the CLI uses, so the two surfaces cannot answer differently. Their canonical projections are asserted byte-identical for the same `generationId`.

Three ways in. A persistent **Explain** button sits in the top area beside the source and account selectors; `e` opens the same modal; and a warning whose own window no existing modal can render — a closed week, a five-hour crossing that recorded no block start, a budget on a calendar week — now offers **Explain this window** beside the sentence saying why the week or the block is not opened.

The entry points differ in account scope, which matters because `--account` changes what the denominator counts. The button and `e` ask about **all accounts** — the same as running `cctally explain` with no `--account` — since neither names one; the account chip beside the button scopes the panels, not this question. Following a warning instead scopes to **that alert's own account**, which is the population the warning was raised over. The modal header states the scope it used.

Following a warning re-measures its window against live data rather than reconstructing the dashboard as it looked when the alert fired, so the modal states **both** instants: when the alert fired and when the diagnosis measured.

The route is read-only and is deliberately not an envelope key: it is a surface most ticks never display, and an envelope key would pay its cost on every tick. Status codes and selectors are in [`dashboard`](dashboard.md#endpoints).

### Latency and boundedness

The maintained large-corpus budgets are a 1.0-second warm median and 2.0-second pooled p95 for Claude, and a 2.0-second warm median and 4.0-second pooled p95 for `--source all`. A fresh dashboard process's first complete `GET /api/diagnosis?source=all` response has a separate **3.0-second ceiling**, measured after the HTTP server has bound; dashboard startup is not hidden inside that request figure.

These are on-demand budgets, not permission to serve an older answer. Diagnosis has no report cache or periodic precomputation, reads the current and preceding half-open windows under the generation probe protocol above, and does not change the dashboard's refresh interval. The reproducible maintainer receipt is `bin/cctally-test-remote python3 bench/explain-benchmark.py`; it fails when a target, canonical CLI/dashboard parity, current/baseline non-vacuity, cold-route status, row/query cap or aggregate parent-and-worker RSS ceiling is missed.

## Implementation

The diagnosis is a pure kernel, `bin/_lib_diagnosis.py`, and every store read goes through a single adapter, `bin/_cctally_diagnosis_sources.py`. That adapter opens each database read-only (SQLite `mode=ro`), reports a `{stats, cache, configuration}` generation vector so an incoherent set of sources is refused rather than blended, and owns the Codex pool-compatible block join. The CLI and the one `diagnosis_to_wire` adapter that renders the JSON envelope live in `bin/_cctally_diagnosis.py`. A report-establishment failure is raised as an `EstablishmentFailure`, which is always exit 3.

## Related

- [`cctally diff`](diff.md) — compare two windows directly.
- [`cctally project`](project.md) — the per-project roll-up a project contributor's next step points at.
- [`cctally five-hour-breakdown`](five-hour-breakdown.md) — the per-percent milestones inside one 5-hour block.
- [`cctally forecast`](forecast.md) — where the week is heading, rather than where it has been.
