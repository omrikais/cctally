# `telemetry`

Inspect and control cctally's anonymous, opt-out **install-count telemetry**. cctally sends, at most once a day, a minimal beat — a one-way month-rotating token (never your install id), the client version, and a coarse OS family (`macos`/`linux`/`windows`/`other`). No IP, username, path, or session content ever leaves the machine.

This page is the command reference. For the full privacy story — what is sent, what is never collected, how the token's cross-month unlinkability works, retention, and the honest threat model — see [`../telemetry.md`](../telemetry.md).

## Synopsis

```
cctally telemetry [on | off | reset] [--json]
```

## Actions

| Action | Effect |
| --- | --- |
| *(none)* | Show the current state: enabled/disabled + the resolved precedence reason, what gets sent (with the resolved version and OS family), the token that would be used this month, and the opt-out surfaces. **Read-only** — it previews the current month's token from an existing install id without ever minting one, and shows `(not yet armed)` when no id exists yet. |
| `on` | Enable telemetry (sets `telemetry.enabled = true`). Thin wrapper over `cctally config set telemetry.enabled true`. |
| `off` | Disable telemetry (sets `telemetry.enabled = false`). Thin wrapper over `cctally config set telemetry.enabled false`. |
| `reset` | Discard the local `install_id` and mint a fresh one, rotating your identity for future months. |

## Options

| Flag | Effect |
| --- | --- |
| `--json` | Emit the status as JSON (status output only — has no effect with `on`/`off`/`reset`). |

## Resolved state and precedence

The bare-status view reports whether telemetry is enabled and *why*, resolved by a side-effect-free predicate. First match wins:

1. `CCTALLY_DISABLE_TELEMETRY` set (truthy) → disabled, reason `env-disabled`.
2. `DO_NOT_TRACK` set (truthy) → disabled, reason `do-not-track`.
3. Running from a dev checkout → disabled, reason `dev-checkout`.
4. `telemetry.enabled = false` in config → disabled, reason `config-disabled`.
5. Otherwise → enabled, reason `enabled`.

Resolving the state never mints an `install_id`, writes config, or sends a beat.

## Examples

```bash
cctally telemetry              # human-readable status + this month's token preview
cctally telemetry --json       # machine-readable status
cctally telemetry off          # opt out
cctally telemetry on           # opt back in
cctally telemetry reset        # rotate the local install id
```

Bare status (text) looks like:

```
telemetry: enabled (enabled)
  sends: rotating monthly token + version (1.63.0) + os (macos)
  token this month: 9f2a…c1
  opt out: cctally telemetry off  |  CCTALLY_DISABLE_TELEMETRY=1  |  DO_NOT_TRACK=1
```

The `--json` payload carries `enabled`, `reason`, `version`, `os`, `period`, `token_preview` (`null` when not yet armed), and `fields` (`["token", "version", "os"]`).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (status shown, or the action applied). |
| `2` | Argument/usage error (argparse convention), or a config-validation error on `on`/`off`. |

## See also

- [`../telemetry.md`](../telemetry.md) — the full transparency page (privacy properties, retention, threat model).
- [`config.md`](config.md) — the `telemetry.enabled` config key.
- [`doctor.md`](doctor.md) — surfaces a read-only, always-OK telemetry line.
