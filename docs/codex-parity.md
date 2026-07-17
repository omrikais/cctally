# Claude and Codex product-parity contract

Issue #294 defines parity as equivalent user outcomes with truthful provider
semantics. It does not mean relabeling one provider's data as the other's, or
inventing a zero value for an unavailable capability. S0 fixed the contract;
S1 supplies the physical ingest foundation, S2 ships native quota
interpretation and lifecycle, and S4 now ships the dashboard's source-aware
backend read model. S5 still owns the visible React source selector and its
end-to-end interaction design.

## S3 provider-aware CLI analytics

S3 supports provider-aware CLI and share analytics for `project`, `diff`,
`range-cost`, `cache-report`, and `report`. Flat leaves accept `--source
{claude,codex,all}` and default to the byte-compatible Claude path. Fixed
`cctally claude|codex <leaf>` forms share the parser/handler but pin their
source and omit a contradictory source flag. `all` is flat-only and preserves
ordered Claude/Codex sections.

| Area | Claude | Codex | `--source all` |
|---|---|---|---|
| `--speed` | Non-`auto` rejected | Applies | Codex leg only |
| `project --weeks` | Subscription weeks | Configured calendar weeks | One resolved calendar interval for both |
| `project --sort used` | Applies | Rejected | Rejected |
| `diff --only cache` / `token-reuse` | Cache / token-reuse rejected | Cache rejected / token reuse applies | Separate Claude cache and Codex reuse sections |
| `range-cost --mode` | Applies | Non-default rejected | Claude leg only |
| `cache-report --anomaly-*`, `--no-anomaly` | Applies | Non-default rejected | Claude leg only |
| `cache-report --sort reuse` | Rejected | Cached-input-percent descending | Codex leg only |
| `report` | Subscription-week trend | Native logical-limit/reset series | Separate sections; no combined report |

Codex project identity is root-qualified internally but exposes only an opaque
`projectKey` and privacy-safe label. An exact opaque key wins; an exact label
must be unique in the selected Codex set; ambiguous labels are usage errors.
Codex cached input is token reuse, never a Claude cache hit. Its input is
inclusive of cached input and output is inclusive of reasoning output.

Direct source JSON carries source/status/data/warnings (`schema_version` stays
frozen for diff). Status is `ok`, `empty`, `partial`, or `unavailable`; a
source block in `all` is never omitted. Physical USD/tokens may be combined for
project/diff/range/cache output, but report has no combined field because
logical-limit series can overlap the same physical accounting rows.

The S3 acceptance rows `source-derived-project-attribution`,
`source-aware-cli-share-identity`, `report-per-source-never-blended`, and
`codex-token-reuse-forensics` are supported. `codex-cache-hit-rate-not-applicable`
remains deliberately not applicable. S4's dashboard backend contract is
supported; S5 source-selection controls, native conversations, and later
certification remain deferred, so issue #294 is still open.

## Capability states

- `supported` means the provider exposes the semantics and cctally ships the
  outcome at this revision.
- `derived` means cctally truthfully derives the named equivalent from native
  provider data.
- `unavailable` means the required stable provider integration does not exist.
- `deferred` means the outcome is planned for its owner session, not shipped.
- `not applicable` means a provider-specific outcome has no meaningful
  counterpart.

Every capability below identifies its Claude and Codex state, owner session,
and the semantic boundary that applies. The matrix complements the executable
[manifest](../tests/fixtures/codex-parity/v1/manifest.json) and
[acceptance matrix](../tests/fixtures/codex-parity/v1/acceptance-matrix.json).

## Existing Codex support to preserve

Codex accounting ingest already powers `codex-daily`, `codex-monthly`,
`codex-weekly`, and `codex-session`, including their subgroup aliases,
source-native token accounting, embedded pricing, speed-tier pricing, budget
calculation, and `cache-sync --source codex`. These shipped outcomes are
`supported`; S1 must retain their terminal and JSON bytes while adding only
source-derived fields. The deliberate duplicate `token_count` dedup behavior
also remains supported.

## S1 physical ingest foundation

S1 now parses each complete Codex rollout record once in binary mode and
retains the existing accounting row together with its complete canonical
physical event, source-derived thread facts, and native quota observations in
the local protected cache. This is a storage foundation, not a dashboard panel
or native conversation reader. S2 interprets the retained observations through
the native `cctally codex quota` commands and setup-managed lifecycle hooks;
S4 still owns dashboard reconciliation, and S6–S8 still own normalization,
routes, search, export, sharing, and browser storage.

The cache migration `024_codex_fused_ingest_rebuild` deliberately clears old
Codex-derived rows so the next local rollout sync can rederive provider-root
and thread facts without fabricating them from an older accounting-only cache.

## Capability matrix

### Accounting

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| accounting ingest | supported | supported | S1 | Preserve existing Codex accounting while retaining source-qualified metadata. |
| daily, monthly, calendar-weekly, session | supported | supported | S1 | Keep the four Codex reports and aliases byte-compatible. |
| speed-tier pricing | supported | supported | S1 | Price each source under its native model semantics. |
| budget calculation and actual/projected alerts | supported | supported | S1 | Existing Codex budget semantics remain separately addressable; autonomous pure-Codex triggering is deferred to S2. |
| pricing coverage and drift | supported | supported | S1 | Existing Codex coverage/drift semantics remain supported. |
| cache sync | supported | supported | S1 | Source selection remains explicit. |

### Quota and provider analytics

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| physical rollout and quota retention | supported | supported | S1 | Retain complete local records and native quota observations only; interpretation remains S2 work. |
| quota history | supported | supported | S2 | `codex quota history` selects S1-retained local observations with their observed slot and actual duration. |
| statusline | supported | supported | S2 | `codex quota statusline` renders provider-native windows; it never fakes a Claude window. |
| forecast | supported | supported | S2 | `codex quota forecast` fits each provider-native reset window independently. |
| blocks | supported | supported | S2 | `codex quota blocks` preserves native reset windows; it does not translate `primary` or `secondary` to a fixed duration. |
| thresholds and alerts | supported | supported | S2 | Opt-in alerts are source-root and logical-limit qualified, with one terminal lifecycle per threshold/reset. |
| `report` and `$ / 1%` | supported | deferred | S3 | Consume S2 quota kernels but compute per source and quota limit. |
| `percent-breakdown` / native quota breakdown | supported | supported | S2 | `codex quota breakdown` keeps milestone and query-time cost correlation per source root and logical limit. |
| `five-hour-breakdown` | supported | deferred | S2 | It is not a substitute for an arbitrary Codex slot. |

### Analytics, sharing, and composition

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| project attribution | supported | deferred | S3 | Group only after provider-qualified identity. |
| `diff` | supported | deferred | S3 | Compare compatible, source-qualified measures. |
| `range-cost` | supported | deferred | S3 | USD is source-native before optional combination. |
| `cache-report` cache hit rate | supported | not applicable | S3 | Codex has no Claude cache hit/miss/create/read analogue. |
| Codex token-reuse forensics | not applicable | deferred | S3 | `cached_input_tokens` may be a truthful provider-native outcome, never a hit rate. |
| share formats | supported | deferred | S3 | Share artifacts retain source-qualified identity. |
| JSON/config behavior | supported | deferred | S3 | New JSON fields are additive and use `schemaVersion` conventions. |
| source-aware composition | deferred | deferred | S3 | All-source views retain source labels and native detail. |

### Dashboard

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| source-aware envelope and hero | supported | supported | S4 | `/api/data` adds immutable Claude, Codex, and presentation-only `all` states without changing the legacy Claude subtree. |
| periods, sessions, projects, qualified details | supported | supported | S4 | Source-qualified routes own their opaque keys; same labels or native IDs never fall back across providers. |
| quota blocks and alerts | supported | supported | S4 | Native quota windows and provider-specific alerts stay side by side, never merged. |
| share backend | supported | supported | S4 | Render, compose, presets, and history carry `source`; `all` renders labelled provider sections rather than a blended quota. |
| visible source selector and sharing controls | deferred | deferred | S5 | S5 owns the React controls and browser UX; S4 deliberately ships no frontend selector. |
| debug diagnostics | supported | supported | S4 | Loopback-only `/api/debug/backend` reports source-aware aggregate counts and opaque versions. |

## S4 dashboard backend contract

`GET /api/data` now appends `source_schema_version`, `default_source`,
`source_order`, and `sources` to the existing Claude-compatible envelope. The
published states are `claude`, `codex`, and presentation-only `all`; an
unavailable source fails closed rather than triggering request-time ingest.

`GET /api/source/<source>/<resource>/<opaque-key>` serves a published
`session`, `project`, or `block` only for its physical `claude` or `codex`
owner. Codex details re-query the bounded relational cache with `sync=False`
and reuse the native session, project, quota, milestone, and forecast kernels;
they do not walk rollout files or fall back to published summary rows. `all`,
malformed pairs, and unavailable domains return the generic
`source_capability_unavailable` error; an unknown valid key returns generic
`source_resource_not_found`. Neither response includes roots, paths, native
keys, logical limits, or exception text.

The dashboard share backend accepts an optional `source` of `claude`, `codex`,
or `all` on render, composer recipes, presets, and history. Omitted source
keeps the legacy Claude request/response shape. Source-aware `all` output is
two labelled provider sections, never a fabricated combined quota. The browser
source picker is intentionally deferred to S5. Explicit Codex reports pass
through the canonical Codex share kernel, use the configured calendar-week
boundary for current-week output, and derive drift digests from Codex state.

### Conversations

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| browse, search/facets, reader | supported | deferred | S6–S8 | New routes use opaque qualified conversation keys. |
| title and outline | supported | deferred | S6 | First meaningful user prompt is the initial Codex title fallback. |
| tools, reasoning, events | supported | deferred | S6 | Preserve observed provider record distinctions. |
| thread nesting | supported | deferred | S6 | Use `thread_source`/parent metadata, never an `agent-` filename convention. |
| find, comparison, navigation | supported | deferred | S7–S8 | Session IDs alone are insufficient identities. |
| reading position | supported | deferred | S8 | `readingPosition.ts` must store an opaque qualified key. |
| transcript CLI and anonymized/raw export | supported | deferred | S7 | `build_anon_plan_for_db` must include Codex roots and labels. |
| payload/media readback and live-tail | supported | deferred | S7–S8 | Feature detection degrades only affected capabilities. |

### Lifecycle and governance

| Outcome | Claude | Codex | Owner | Truthful contract |
| --- | --- | --- | --- | --- |
| setup-managed autonomous sync and alerts | supported | supported | S2 | Native Codex `Stop`/`SubagentStop` hooks run a local all-root sync, reporting reconciliation, and due-root-only alert evaluation without provider crossover. |
| doctor, setup, and uninstall | supported | supported | S2 | Report source-specific local freshness, hook configuration, and lifecycle activity; uninstall removes only cctally-owned handlers. |
| `refresh-usage` / provider-live OAuth refresh | supported | unavailable | S2 | Codex has no provider-live/OAuth analogue. |
| local rollout quota freshness/reread | supported | supported | S2 | S2 surfaces freshness of locally retained rollout captures only; it is not a provider-live refresh. |
| record-credit/reset reanchoring | supported | not applicable | S3 | Claude Code credit/reset semantics are provider-specific. |
| Claude Code hooks | supported | not applicable | S3 | No fake Codex hook counterpart is introduced. |
| TUI presentation | supported | not applicable | S9 | TUI stays under the approved bugfix-only freeze. |

## Provider-qualified identity

Every new cross-source resource uses `IdentityV1`: version, source,
resource kind, non-reversible source-root fingerprint, native key, and parent
key. Its public form is an opaque, versioned URL-safe encoding; consumers
compare or round-trip it and do not split it. Absolute roots never enter URLs,
browser storage, share artifacts, or public fixture expectations.

The corpus deliberately makes one Claude session and one Codex thread share a
UUID, and makes two Codex roots share the same inner UUID. Their qualified
identities differ. It also supplies a parent/child relationship through
`thread_source`, proving that provider metadata rather than a filename names
Codex nesting.

## Generic quota and schema tolerance

A quota observation records source, capture time, observed slot, optional limit
ID/name, positive `windowMinutes`, percent, reset time, and optional provider
metadata. Its stable logical per-window key is the root-qualified composite
`{source, sourceRootKey, limitId, observedSlot, windowMinutes}`. `limitId` is
an input, not a globally unique winner: the provider root, observed slot, and
actual duration keep colliding envelopes distinct. `primary` and `secondary`
remain observed labels, never global aliases for 5h or 7d.

The fixture snapshot is version-tolerant feature detection, not a closed JSON
schema. Binary parsing preserves physical byte offsets; unknown records/fields
are retained or ignored without aborting accounting. Missing `rate_limits`, a
malformed individual window, a partial final line, and legacy envelopes degrade
quota only when safe accounting or metadata remains. The synthetic corpus
includes each case and no live rollout content.

## Combined arithmetic

| Measure | Across sources | Rule |
| --- | --- | --- |
| USD cost | additive | Sum values computed under each provider's pricing semantics. |
| Input/output/total tokens | additive only with source-native labels retained | A combined total may be shown, but detail must not relabel cached input or reasoning as Claude cache-create/cache-read. |
| Quota used percent | never additive or averaged | Render independent source/limit windows side by side. |
| Quota reset/window | never merged | Keep the source, limit identity, duration, and reset together. |
| `$ / 1%` | never additive or averaged | Compute and display per source and per logical limit only. |
| Percent milestones/marginal cost | never merged | Keep per source and quota limit. |
| Budget | additive only for an explicitly combined USD budget | Provider budgets and alerts remain separately addressable. |
| Projects/sessions | aggregate only after qualified grouping | Same labels or UUIDs do not imply shared identity. |

In short: never sum or average quota percentages, reset/window values,
`$ / 1%`, or percent milestones.

## Explicit exceptions and delivery order

Codex local-rollout reread is not a live OAuth pull. Codex token reuse is not
Claude cache hit/miss/create/read reporting. The first meaningful user prompt
is a fallback title, not an assumed provider AI title. There is no fake Codex
record-credit/reset reanchoring or Claude Code hooks. The TUI freeze remains
explicit.

The delivery graph is `S0 → S1`; `S1 → S2, S3, S4, S6`; `S4 → S5`; and
`S6 → S7 → S8`, with S2/S3/S5/S8 feeding S9. #293 S4 is a hard predecessor
for S5 absent a maintainer-approved ownership split. S6–S8 are independently
deferrable, but the epic cannot claim completion while their matrix rows remain
`deferred`.

## Evidence

The approved [S0 design](superpowers/specs/2026-07-14-294-s0-parity-contract-design.md),
generated [manifest](../tests/fixtures/codex-parity/v1/manifest.json), generated
[acceptance matrix](../tests/fixtures/codex-parity/v1/acceptance-matrix.json),
and issue #294 define this contract. The executable guards are in
`tests/test_codex_parity_contract.py`.
