# `transcript`

Export or search conversation transcripts from the local cache. `transcript export` produces an **anonymized-by-default** Markdown copy of a whole session — the same scrub the dashboard's Export ▾ menu applies — so the natural "share this session" action is safe by default. `transcript search` is a scriptable front end to the dashboard's cross-session search.

Both subcommands read the conversation store (`~/.local/share/cctally/conversations.db`, populated by the dashboard or `cctally cache-sync`). Conversation readers attach compact accounting metadata from `cache.db` read-only when needed; core accounting never attaches the transcript store. The commands do not re-ingest, so the transcript reflects the current cached state.

Both sources are addressable. `export` takes either a Claude `sessionId` **or** an opaque `v1.` conversation key (Codex conversations are only addressable by their `v1.` key); `search` picks the provider with `--source {claude,codex}` (default `claude`). Absent qualification, the Claude behavior is byte-identical to before.

The CLI exports one resolved conversation at a time. The dashboard's
mixed-source comparison copy action composes two such whole exports as separate
source-labelled Run A / Run B sections; it does not merge provider transcript
bodies or invent a combined CLI source.

## Synopsis

```
cctally transcript export <id> [--scope {all,prompts,chat,recipe}] [--raw]
                               [--speed {auto,standard,fast}] [-o PATH]
cctally transcript search <query> [--source {claude,codex}]
                                  [--kind {all,prompts,assistant,tools,thinking,title,files}]
                                  [--limit N] [--offset N] [--cursor TOKEN]
                                  [--project LABEL ...] [--model FAMILY ...]
                                  [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
                                  [--cost-min USD] [--cost-max USD] [--rebuild-min N]
                                  [--json]
```

## `transcript export`

Renders one whole conversation to Markdown. The output is byte-for-byte the same as the dashboard's `GET /api/conversation/<id>/export` download — the CLI is a thin wrapper over the same renderer, for both providers and in both anonymize and raw modes.

- `<id>` — a Claude `sessionId` **or** a `v1.` conversation key (positional, required). A `v1.` key routes through the provider-neutral dispatch; anything else is the legacy Claude path, byte-unchanged. A Codex conversation is only addressable by its `v1.` key.
- `--scope {all,prompts,chat,recipe}` — which slice to render (default `all`). The four scopes apply to Claude conversations; a **Codex** conversation supports only the default whole-conversation export, so any other `--scope` on a Codex `v1.` key is a usage error (exit 2).
  - `all` — full fidelity: prose, thinking, tool calls (Edit diffs, Bash commands + results), meta turns, and grouped subagent threads.
  - `prompts` — the main-thread human prompts only, each as `## Prompt N`.
  - `chat` — human + assistant prose only (no thinking, tools, or meta).
  - `recipe` — a `# Replay recipe` header plus a numbered list of the main-thread prompts.
- `--raw` — disable the whole scrub (identity **and** secrets). The result is byte-identical to the dashboard's raw export. Use this only for a transcript you intend to keep private. Qualified (`v1.`) exports anonymize by default via the provider-aware plan (which draws Codex roots and labels from the authoritative Codex tables); `--raw` escapes it.
- `--speed {auto,standard,fast}` — the Codex service tier used for per-turn cost in a Codex export (default `auto`, resolved once from your `$CODEX_HOME` config). Speed is Codex pricing behavior, so an **explicit** `--speed` (any value, including `auto`) on a bare Claude id or a `v1.` **Claude** key is a usage error (exit 2) — never a silent no-op. Omit it for Claude exports.
- `-o`, `--output PATH` — write to `PATH` instead of stdout. The file receives the exact same bytes stdout would; nothing else is printed on stdout.

**Emission is byte-exact.** The export is written as raw UTF-8 with no added trailing newline (the render already ends in exactly one). This is what lets `transcript export` byte-match the dashboard endpoint — a qualified default export byte-matches `GET /api/conversation/<v1key>/export?anonymize=1`, and `--raw` byte-matches the un-parameterized download — and lets you diff two exports meaningfully.

**Pending Codex normalization.** A Codex conversation whose cache predates migration `025_codex_conversation_normalization` cannot be exported yet: the command prints a `transcript: …` note to stderr (citing that the migration runs on the next cache open) and exits `1` — never a 0-exit empty export. Open the dashboard once, or run `cctally cache-sync --source codex`, to stamp it.

## `transcript search`

Cross-session search over the cached transcripts (FTS5 when available, otherwise a LIKE scan). It mirrors the dashboard's search + browse-filter surface.

**Search output is raw.** Search is a *navigation* surface — it helps you find the session to open — not a sharing artifact, so its output (snippets, project labels, session ids) is never anonymized. Anonymize at export time, not at search time.

- `<query>` — the search text (positional, required).
- `--source {claude,codex}` — which provider's conversations to search (default `claude`). The default-Claude table and `--json` envelope are byte-frozen; `--source codex` selects the Codex output described below.
- `--kind {all,prompts,assistant,tools,thinking,title,files}` — the search facet (default `all`; identical taxonomy for both providers).
- `--limit N` — max results (default `50`).
- `--offset N` — Claude-only result offset (default `0`). The Codex search kernel paginates by opaque cursor, so `--offset` with `--source codex` is a usage error (exit 2).
- `--cursor TOKEN` — Codex pagination cursor, taken from a prior response's `nextCursor` (the JSON footer, or the `next: --cursor …` line in the table). Codex only; using `--cursor` with `--source claude` is a usage error (exit 2).
- `--project LABEL` / `--model FAMILY` / `--date-from` / `--date-to` / `--cost-min` / `--cost-max` / `--rebuild-min` — Claude-only filter axes. The Codex search kernel has no filter axes, so any of these combined with `--source codex` is a usage error (exit 2) rather than a silently-ignored filter. Date-only values are resolved in your `display.tz`, using the same parser the dashboard filter uses.
- `--json` — emit a machine-readable envelope instead of the table (see below). The default output is a content-sized table.

### Codex search output

`--source codex` renders its own table — `Key / When / Project / Kinds / Snippet` — where `Key` is the **full, untruncated** `v1.` conversation key (so `search` → `export` pipes directly), `When` is the hit conversation's last activity rendered in your `display.tz`, `Project` is the project label (`—` when unknown), and `Kinds` are the match badges. When more results exist, a trailing `next: --cursor <token>` line prints the pagination cursor.

Its `--json` is a source-qualified, stamped-first camelCase envelope (distinct from the frozen Claude shape):

```
{
  "schemaVersion": 1,
  "source": "codex",
  "query": "<query>",
  "mode": "fts" | "like",
  "total": <int>,
  "hits": [
    {
      "conversationKey": "v1.…",
      "itemKey": "…",
      "title": "…",
      "snippet": "…",
      "badges": ["tool", ...],
      "lastActivityUtc": "…Z",
      "projectLabel": "…"
    },
    ...
  ],
  "nextCursor": "<token>" | null
}
```

A Codex cache that predates migration `025` returns an empty result set with a one-line stderr note and exit `0` — "nothing yet" is a truthful navigation answer, not an error.

### Claude search `--json` schema

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

- `0` — success (including a search with zero hits, and a Codex search whose normalization is still pending).
- `1` — unknown conversation on `transcript export` (either source), or a Codex export whose normalization is still pending (a `transcript: …` message is printed to stderr).
- `2` — usage / validation error: a bad flag, an unknown `--scope`/`--kind`, a malformed `--date-from`/`--date-to`, an explicit `--speed` on a non-Codex ref, a non-default `--scope` on a Codex export, or a Codex-incompatible `--offset` / `--cursor` / filter flag.

See [`docs/cli-contract.md`](../cli-contract.md) for the repo-wide exit-code taxonomy and JSON envelope conventions.
