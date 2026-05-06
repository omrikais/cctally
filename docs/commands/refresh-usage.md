# `cctally refresh-usage`

Force-fetch 7-day and 5-hour rate-limits from the Anthropic OAuth usage API and
persist them via the same path `record-usage` uses.

**Normally not needed** because `cctally hook-tick` calls the same OAuth-fetch
path automatically. Manual invocation is mostly for force-refresh / debugging.

## Difference from `hook-tick`

`refresh-usage` busts the status-line cache file
(`/tmp/claude-statusline-usage-cache.json`) on success, so the next status-line
tick re-fetches. `hook-tick` calls a non-busting variant of the same fetch path
to avoid interfering with the status-line cache.

## User-Agent

`cctally refresh-usage` sends `User-Agent: claude-code/<discovered-version>` by
default. Anthropic gates the OAuth `/usage` endpoint behind a per-User-Agent
rate-limit; presenting the official Claude Code UA bypasses that gate so the
fetch returns 200 instead of 429 during active sessions.

The discovery chain (first hit wins):

1. **Active executable.** `claude --version 2>/dev/null` (5 s timeout), parsed
   against `\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?`.
2. **Versions directory glob.** `~/.local/share/claude/versions/` filtered to
   semver-shaped entries; highest semver wins.
3. **Frozen sentinel.** A pinned fallback version baked into the script.

The default can be overridden via the `oauth_usage.user_agent` config field —
see [Honesty mode](#honesty-mode) below.

## Flags

- `--quiet` — suppress text output
- `--json` — emit structured envelope
- `--color {auto,always,never}` — color discipline
- `--timeout N` — HTTP timeout (default 5s)

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success **or** rate-limited (graceful fallback emitted) |
| 2 | No OAuth token (run `claude` once to authenticate) |
| 3 | Network / HTTP failure (non-429) |
| 4 | Malformed response |
| 5 | `record-usage` failure |

A 429 response from the OAuth usage API is **not** an error from the user's
perspective — there is no actionable retry without changing UA. The command
serves last-known data from `weekly_usage_snapshots`, prints a freshness
indicator, and exits 0. See the next section for the exact output shape.

## Output: rate-limited mode

When the upstream API returns 429, `refresh-usage` falls back to the most
recent row in `weekly_usage_snapshots` (ordered by `captured_at_utc DESC,
id DESC`) and exits 0 in all 429 cases.

**Text mode (stderr).** One line:

- With prior snapshot:

  ```
  refresh-usage: rate-limited; using last-known (captured Ns ago)
  ```

  Followed on stdout by the standard one-liner rendered from the fallback
  payload (with `src:db-fallback cache:absent`). When the snapshot's
  `resets_at_epoch` is `null` (rows captured before the `week_end_at`
  migration), the renderer drops the `(in Xd Yh)` TTL segment for that
  block.

- With no prior snapshot:

  ```
  refresh-usage: rate-limited; no last-known data; status-line will populate on next CC tick
  ```

  Nothing is printed on stdout.

**JSON mode (`--json`).** A rate-limited envelope replaces the success
payload:

```json
{
  "status": "rate_limited",
  "fallback": {
    "schema_version": 1,
    "fetched_at": "<iso8601>",
    "seven_day": {
      "used_percent": 47.0,
      "resets_at": "<iso8601>",
      "resets_at_epoch": 1735689600
    },
    "five_hour": {
      "used_percent": 12.0,
      "resets_at": "<iso8601>",
      "resets_at_epoch": 1735603200
    },
    "source": "db-fallback",
    "statusline_cache": "absent"
  },
  "freshness": {
    "label": "fresh",
    "captured_at": "<iso8601>",
    "age_seconds": 12
  },
  "reason": "user-agent rate-limit gate"
}
```

Notes:

- `freshness.label` is one of `fresh` / `aging` / `stale`, derived from
  `oauth_usage.fresh_threshold_seconds` (default 30) and
  `oauth_usage.stale_after_seconds` (default 90).
- `fallback.source` is `"db-fallback"` (not `"oauth"`) so consumers can
  distinguish fallback data from a fresh fetch.
- `fallback.five_hour` is omitted (set to `null`) when the latest snapshot
  has no recorded 5-hour block.
- `fallback.seven_day.resets_at_epoch` may be `null` for snapshots predating
  the `week_end_at` migration; the same applies to
  `fallback.five_hour.resets_at_epoch`. The text renderer drops the
  `(in Xd Yh)` segment whenever a block's `resets_at_epoch` is `null`.

When no prior snapshot exists, `fallback` is `null`, `freshness` is `null`,
and `reason` is `"no prior snapshot"`. Exit is still 0.

## Honesty mode

Users who do not want `cctally` to impersonate Claude Code can set an
explicit User-Agent in `~/.local/share/cctally/config.json`:

```json
{
  "oauth_usage": {
    "user_agent": "cctally/0.1",
    "fresh_threshold_seconds": 30,
    "stale_after_seconds": 90
  }
}
```

When `oauth_usage.user_agent` is a non-empty string, that exact value is
sent in the `User-Agent` header — the `claude-code/<discovered>`
impersonation default is disabled.

**Consequence.** Anthropic's per-UA rate-limit gate will respond `429 Too
Many Requests` to any UA other than `claude-code/*` during active sessions.
`refresh-usage` then takes the rate-limited path described above: last-known
data from the DB, a freshness label derived from
`fresh_threshold_seconds` / `stale_after_seconds`, and exit 0. The
status-line and dashboard chips will reflect the staleness instead of
showing a hard error.

This is the recommended posture for users with stricter compliance
requirements who would rather see slightly stale data than send the
official Claude Code UA from a non-Claude-Code process.

To revert to the impersonation default, set `user_agent` back to `null` or
remove the field.

## See also

- [`hook-tick`](hook-tick.md) — automatic per-fire variant (no cache-bust)
- [`setup`](setup.md) — installs the hooks that drive `hook-tick`
- [`record-usage`](record-usage.md) — receiver for both fetch paths and the opt-in status-line integration
