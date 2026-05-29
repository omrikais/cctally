# `pricing-check`

Detect whether cctally's **embedded** model pricing
(`CLAUDE_MODEL_PRICING` / `CODEX_MODEL_PRICING`) has gone stale or is
missing a model. cctally prices every session locally from these embedded
tables; an unrecognized Claude model silently contributes **$0** and an
unrecognized Codex model is *approximated* via the `gpt-5` fallback. This
command surfaces those gaps proactively instead of waiting for someone to
reconcile by hand.

It is the network-aware counterpart to the offline
[`doctor`](doctor.md) `pricing.coverage` check, and the entry point for the
weekly `pricing-freshness` CI workflow.

## Synopsis

```
cctally pricing-check [--json] [--offline]
```

## Purpose

Three **independently-degrading** legs:

1. **Coverage (offline, all-history)** — models in your cached session data
   (`cache.db`) that cctally cannot price exactly: Claude models priced at
   `$0` (`unpriced`) or Codex models approximated via the `gpt-5` fallback
   (`fallback`). No network; a read-only scan over the cache.
2. **Drift (network, LiteLLM)** — embedded price *values* vs the
   [LiteLLM](https://github.com/BerriAI/litellm) pricing snapshot.
   Direction-aware (see below) and allowlist-suppressed.
3. **Existence (network, Anthropic `/v1/models`)** — vendor models the API
   offers that our table lacks. **Anthropic-only** and **maintainer-local**
   (needs a Claude OAuth bearer); see the asymmetry + caveat sections.

The tool *detects*; a human *applies*. It never auto-edits the pricing
tables — the verified-against-vendor discipline stays.

## Options

| Flag | Effect |
| --- | --- |
| `--json` | Emit the machine-readable payload (`schemaVersion: 1`) to stdout. CI consumes this. |
| `--offline` | Run the coverage leg only. The drift + existence legs are skipped (no network). |

## Exit codes

Exit code and `status` are **orthogonal**: the exit code reports whether you
must act; `status` reports whether the check was complete.

| Code | Meaning |
| --- | --- |
| `1` | **Any actionable finding** — a coverage gap, value drift, a missing-from-us model, **or** an existence gap — **even if a network leg degraded**. Findings always win over degradation. |
| `0` | **No actionable findings** — a fully clean run **or** a partially/fully network-degraded run that surfaced nothing actionable. The `--json` payload still carries `"status": "degraded"` so a caller can tell "clean" from "couldn't fully check." |
| `2` | Argument/usage error (argparse convention). |

`ahead_of_litellm` (a model we price that LiteLLM lacks) is **never**
actionable — we may legitimately lead the source. It is reported for context
only and does not affect the exit code.

## JSON schema (`schemaVersion: 1`)

```json
{
  "schemaVersion": 1,
  "status": "ok",
  "degraded_components": [],
  "snapshotDate": "2026-05-04",
  "litellmSource": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
  "coverage": [
    {
      "provider": "claude",
      "model": "claude-mystery",
      "kind": "unpriced",
      "entry_count": 3,
      "token_total": 5000
    }
  ],
  "drift": {
    "value_drift": [
      {
        "model": "claude-3-5-haiku-20241022",
        "field": "input_cost_per_token",
        "ours": 8e-07,
        "theirs": 9.99e-07
      }
    ],
    "missing_from_us": ["claude-future-x"],
    "ahead_of_litellm": ["claude-opus-4-8"]
  },
  "existence": {
    "status": "ok",
    "unpriced_vendor_models": ["claude-brand-new"]
  }
}
```

| Field | Shape | Notes |
| --- | --- | --- |
| `schemaVersion` | `1` | Bumped on a breaking change; adding optional keys does not bump. |
| `status` | `"ok"` \| `"degraded"` | `degraded` whenever ≥1 network leg failed. Orthogonal to the exit code. |
| `degraded_components` | `string[]` | Which legs degraded: `"litellm"` and/or `"models_api"`. Empty when complete (and always empty under `--offline`). |
| `snapshotDate` | ISO date | `PRICING_SNAPSHOT_DATE` — when the embedded tables were last verified against the vendor. |
| `litellmSource` | URL | The LiteLLM snapshot the drift leg compares against. |
| `coverage` | `CoverageGap[]` | `{provider, model, kind, entry_count, token_total}`. `kind` ∈ `{"unpriced", "fallback"}`. |
| `drift.value_drift` | `DriftRow[]` | `{model, field, ours, theirs}` — a shared model whose price field differs beyond a tiny epsilon. |
| `drift.missing_from_us` | `string[]` | Scoped LiteLLM models absent from our tables. Actionable. |
| `drift.ahead_of_litellm` | `string[]` | Models we price that scoped LiteLLM lacks. **Informational, never actionable.** |
| `existence` | object | `{status, unpriced_vendor_models}`. See below — the existence block is **Anthropic-only**. |

Consumers MUST tolerate unknown keys.

### The `existence` block (Anthropic-only — no Codex field)

`existence` describes the Anthropic `/v1/models` leg and **only** that leg.
There is **no** Codex sub-field: cctally has no OpenAI credentials, so Codex
new-model existence is out of scope (it relies on the coverage leg + LiteLLM
lag instead). The block is exactly:

```json
{ "status": "ok" | "degraded" | "skipped", "unpriced_vendor_models": [ ... ] }
```

- `"skipped"` — `--offline` was used (the leg never ran). `unpriced_vendor_models` is `[]`.
- `"degraded"` — the leg ran but the fetch failed (no OAuth token, `401`/`403`, network error, non-JSON). Not a finding; reflected in `status`/`degraded_components` as `models_api`.
- `"ok"` — the vendor list was obtained; `unpriced_vendor_models` lists the IDs the vendor offers that `_resolve_model_pricing` cannot price. A non-empty list is **actionable** (exit 1).

## LiteLLM lag + Codex-existence asymmetry

No source updates immediately except the provider page, which is HTML and
scrape-hostile — and we do not scrape. The two value/existence sources have
different lag profiles, and the asymmetry is deliberate:

- **LiteLLM (value drift)** *lags* Anthropic announcements. A LiteLLM-only
  check is blind during the day-0 window after a model ships. That is
  acceptable for *values* — price changes are rare and usually
  pre-announced.
- **Anthropic `/v1/models` (existence)** is *zero-lag* and clean JSON, but
  needs a Claude OAuth bearer, so it is **maintainer-local** — it runs in
  `doctor`/`pricing-check`/the release pre-flight on a machine with OAuth,
  but is **never** cron-automated (the weekly CI workflow has no OAuth and
  auto-degrades to LiteLLM drift only). The day-0 backstop for new-model
  *existence* is therefore the local coverage guard, not the cron.
- **Codex** has *no* existence leg at all (no OpenAI credentials). Codex
  new-model detection relies entirely on the coverage leg (the `gpt-5`
  fallback shows up as a `fallback` coverage gap once you actually use the
  model) plus LiteLLM lag for value drift.

## Allowlist (`PRICING_DRIFT_ALLOWLIST`)

A small allowlist beside the pricing tables suppresses *deliberate*
divergences from LiteLLM so they don't perpetually flag. Each entry is
`{model, field?, reason}`:

- `{model, field, reason}` — suppress a specific `value_drift` field.
- `{model, reason}` (no `field`) — suppress an intentionally-omitted in-scope
  model in `missing_from_us`.

The allowlist is guarded by a **non-vacuity test**: every entry must
correspond to a *real* divergence against the committed LiteLLM snapshot. If
upstream resolves a divergence, the now-stale entry fails the suite and must
be removed — stale ignores cannot accumulate.

## Caveats

- **`/v1/models` reachability via the OAuth bearer is UNVERIFIED.** The
  existence leg was authored without a live `/v1/models` call. It is unknown
  whether the Claude Code OAuth bearer actually authorizes
  `GET /v1/models`; if the endpoint returns `401`/`403`, the leg degrades
  gracefully (`existence.status: "degraded"`, `models_api` in
  `degraded_components`) and the feature still stands on LiteLLM drift + the
  local coverage guard. Run `cctally pricing-check` on a machine with a real
  OAuth token to confirm the leg reaches `"ok"`.
- **No test hits the network.** Snapshots are injected via hidden env hooks
  in the test suite; production fetches LiteLLM + `/v1/models` over HTTP.

## Examples

```bash
# Offline coverage only — fast, no network. Exit 0 if all observed models
# are priced; exit 1 if any unpriced/fallback model appears in your cache.
cctally pricing-check --offline

# Full check, machine-readable (what the weekly CI workflow runs):
cctally pricing-check --json | jq '{status, exit: .status, drift: .drift.value_drift}'

# Treat as a healthcheck: nonzero on an actionable finding.
cctally pricing-check --offline || echo "pricing needs attention"
```

## See also

- [`doctor`](doctor.md) — the offline `pricing.coverage` check (same coverage classification, surfaced in the dashboard chip/modal)
- [`cache-sync`](cache-sync.md) — rebuild the session-entry cache the coverage leg scans
