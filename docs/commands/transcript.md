# `transcript`

Export or search conversation transcripts from the local cache. `transcript export` produces an **anonymized-by-default** Markdown copy of a whole session — the same scrub the dashboard's Export ▾ menu applies — so the natural "share this session" action is safe by default. `transcript search` is a scriptable front end to the dashboard's cross-session search.

Both subcommands read the conversation cache (`~/.local/share/cctally/cache.db`, populated by the dashboard / status-line sync or `cctally cache-sync`); they do not re-ingest, so the transcript reflects the current cached state.

## Synopsis

```
cctally transcript export <session-id> [--scope {all,prompts,chat,recipe}] [--raw] [-o PATH]
cctally transcript search <query> [--kind {all,prompts,assistant,tools,thinking,title,files}]
                                  [--limit N] [--offset N]
                                  [--project LABEL ...] [--model FAMILY ...]
                                  [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
                                  [--cost-min USD] [--cost-max USD] [--rebuild-min N]
                                  [--json]
```

## `transcript export`

Renders one whole session to Markdown. The output is byte-for-byte the same as the dashboard's `GET /api/conversation/<id>/export` download — the CLI is a thin wrapper over the same renderer.

- `<session-id>` — the Claude `sessionId` to export (positional, required).
- `--scope {all,prompts,chat,recipe}` — which slice to render (default `all`):
  - `all` — full fidelity: prose, thinking, tool calls (Edit diffs, Bash commands + results), meta turns, and grouped subagent threads.
  - `prompts` — the main-thread human prompts only, each as `## Prompt N`.
  - `chat` — human + assistant prose only (no thinking, tools, or meta).
  - `recipe` — a `# Replay recipe` header plus a numbered list of the main-thread prompts.
- `--raw` — disable the whole scrub (identity **and** secrets). The result is byte-identical to the dashboard's raw export. Use this only for a transcript you intend to keep private.
- `-o`, `--output PATH` — write to `PATH` instead of stdout. The file receives the exact same bytes stdout would; nothing else is printed on stdout.

**Emission is byte-exact.** The export is written as raw UTF-8 with no added trailing newline (the render already ends in exactly one). This is what lets `transcript export` byte-match the dashboard endpoint and lets you diff two exports meaningfully.

## `transcript search`

Cross-session search over the cached transcripts (FTS5 when available, otherwise a LIKE scan). It mirrors the dashboard's search + browse-filter surface.

**Search output is raw.** Search is a *navigation* surface — it helps you find the session to open — not a sharing artifact, so its output (snippets, project labels, session ids) is never anonymized. Anonymize at export time, not at search time.

- `<query>` — the search text (positional, required).
- `--kind {all,prompts,assistant,tools,thinking,title,files}` — the search facet (default `all`).
- `--limit N` / `--offset N` — pagination (default `50` / `0`).
- `--project LABEL` — restrict to sessions with this project label (repeatable, or comma-joined).
- `--model FAMILY` — restrict to sessions using this model family (repeatable).
- `--date-from` / `--date-to YYYY-MM-DD` — restrict to sessions in a date range. Date-only values are resolved in your `display.tz`, using the same parser the dashboard filter uses.
- `--cost-min` / `--cost-max USD`, `--rebuild-min N` — restrict by session cost / cache-rebuild count.
- `--json` — emit a machine-readable envelope instead of the table (see below). The default output is a content-sized table (session, timestamp, project, match kinds, snippet).

### Search `--json` schema

`schemaVersion: 1`. The envelope is stamped first, then an explicit camelCase mapping of the search result:

```
{
  "schemaVersion": 1,
  "query": "<query>",
  "mode": "fts" | "like",
  "hits": [
    {
      "sessionId": "...",
      "uuid": "...",
      "projectLabel": "...",
      "title": "...",
      "ts": "...Z",
      "snippet": "...",
      "matchKinds": ["tool", ...],
      "costUsd": 0.0
    },
    ...
  ],
  "total": <int>,
  "kind": "<facet>",
  "searchDepth": "full" | "prose-only",
  "filterDegraded": true    // present only when a rollup-only filter axis was dropped
}
```

`searchDepth` is `prose-only` during the one-time cache index split (the tools/thinking facets return empty until it finishes); it becomes `full` afterward. Consumers must tolerate unknown keys (additive evolution).

## What anonymized export covers — and what it does not

Anonymization is **best-effort over known tokens; review before sharing.** The tested guarantee is that zero *known* identity tokens survive an anonymized export of a fixture session — nothing broader is claimed.

**Covered (replaced with stable placeholders):**

- Observed project roots → `project-1`, `project-2`, … (numbered deterministically by path), including each root's dash-encoded directory variant (as it appears under `~/.claude/projects/<encoded>`).
- Project labels (basenames) → their root's `project-N`.
- Home directories → `~`; usernames (home-dir basenames) → `user`.
- A small documented set of high-precision secret patterns, redacted to `[REDACTED:<name>]`: Anthropic keys (`sk-ant-…`), generic `sk-…` keys, GitHub tokens (`ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_…`), AWS access keys (`AKIA…`), Slack tokens (`xox[baprs]-…`), `Bearer <token>` values, `Authorization:` header values, and `api_key`/`token`/`secret`/`password` assignments (the key + separator are kept, the value redacted).

**NOT covered (not comprehensively detected or guaranteed removed in v1):** emails, IP addresses, hostnames, remote URLs (e.g. git remotes), session ids, and any project identity that is not present in cctally's cache. A known identity substring may still be replaced *inside* one of these (e.g. a username inside an email), but the classes themselves carry no guarantee. Common-word project labels (a repo literally named `test`) will also replace that word in prose — identity beats readability in anonymized mode, and `--raw` is one flag away. One narrow under-redaction edge in the other direction: a bare project label or username that sits immediately against an underscore (e.g. as the first or last word inside markdown `_emphasis_`, or embedded in a snake_case identifier) is not replaced, because `_` is part of the token-boundary class that protects code identifiers; full paths, home directories, encoded dirnames, and secrets are unaffected by this edge.

**Point-in-time numbering (not persisted).** The `project-N` numbers are derived fresh from the cache at export time and are not stored — the same project can map to a different number in a later export as your observed projects change. Do not treat `project-3` as a stable identifier across exports.

## Exit codes

- `0` — success (including a search with zero hits).
- `1` — unknown session on `transcript export` (a `transcript: …` message is printed to stderr).
- `2` — usage / validation error (a bad flag, an unknown `--scope`/`--kind`, or a malformed `--date-from`/`--date-to`).

See [`docs/cli-contract.md`](../cli-contract.md) for the repo-wide exit-code taxonomy and JSON envelope conventions.
