# `cctally hook-tick`

**Internal — invoked automatically by Claude Code hooks.**
Users normally don't run this directly. Hidden from `cctally --help`.

## What it does

On every assistant-message boundary (`PostToolBatch`, `Stop`, `SubagentStop`):

1. Reads the CC hook payload from stdin (JSON: `hook_event_name`, `session_id`,
   `transcript_path`, `cwd`).
2. Detaches stdout/stderr to `~/.local/share/cctally/logs/hook-tick.log`.
3. Tail-ingests new JSONL entries into the local SQLite cache.
4. If `~/.local/share/cctally/hook-tick.last-fetch` is older than the throttle
   threshold (default 30s), refreshes 7-day / 5-hour rate limits from the
   Anthropic OAuth usage API and writes via the same `record-usage` path.
5. Writes one log line. Returns 0 unconditionally.

## Useful flags (manual / debugging)

- `--explain` — synchronous, prints decision tree to stdout, exits informative code.
- `--no-oauth` — skip OAuth refresh entirely (local sync only).
- `--throttle-seconds N` — override the 30s default.
- `--event NAME` — override the event name written to the log line (rare).

## Log format

```
<UTC ISO ts> event=<E> session=<sid8> ingested=<N> oauth=<status> dur_ms=<D>
```

`oauth=` values:

- `ok(7d=N,5h=M)` / `ok(7d=N)` — fetched successfully (5h optional)
- `throttled(age=Ns)` — last fetch was less than the throttle threshold ago
- `skipped-no-oauth` — `--no-oauth` flag set
- `skipped-no-token` — no Claude OAuth token found in keychain or credentials file
- `err(network)` / `err(parse)` — fetch or parse failed
- `err(record-usage=K)` / `err(record-usage=exc)` — `record-usage` returned exit code K or raised
- `err(internal:<TypeName>)` — uncaught exception class within the hook (rare; signals a bug)

Rotation: when the log file exceeds 1 MB, it's atomic-renamed to
`hook-tick.log.1` (single-generation; older `.1` is overwritten).

## See also

- [`setup`](setup.md) — installs the hook entries that fire `hook-tick`
- [`refresh-usage`](refresh-usage.md) — manual variant that busts the status-line cache
- [`record-usage`](record-usage.md) — direct status-line integration (alternative to hooks)
