# Telemetry

cctally sends an anonymous, opt-out **install-count beat** so the project can answer one honest question: roughly how many real people actually use it. GitHub stars and npm download counts are drowned in bots and mirrors; a tiny once-a-day beat from the running client is the only signal that reflects real usage. This page documents exactly what is sent, what is never sent, how the privacy properties work, and every way to turn it off.

It is on by default and designed for the smallest possible data collection. If you would rather not participate, opt out with one command (see [Opting out](#opting-out)) — no data leaves your machine for at least 24 hours after first run, so you always have a window to opt out before the first beat.

## What is sent

At most once per day, the client POSTs a single JSON object with exactly three fields to `https://cctally-telemetry.cctally.workers.dev/beat`:

| Field | Meaning | Example |
| --- | --- | --- |
| `t` | The rotating monthly token — a 32-character (128-bit) hex string. See [The token](#the-token). | `9f2a…c1` |
| `v` | The cctally version, or the literal `"unknown"` when the client is running from an unstamped tree. | `1.63.0` |
| `os` | The coarse operating-system family, one of `macos`, `linux`, `windows`, `other`. Never the OS version. | `macos` |

That is the whole payload. Version adoption and the platform split are the two dimensions a maintainer almost always wants, and both are extremely coarse. Nothing else is transmitted.

## What is never collected

The beat never contains, and the server never stores, any of the following:

- **No identity** — no username, no email, no machine name, no account, no GitHub handle.
- **No file paths** — no project names, no working directory, no home directory.
- **No prompts or conversation content** — none of your Claude Code or Codex session data.
- **No usage data** — no command names, no costs, no token counts, no percentages, no timing beyond the coarse "beat happened this month".
- **No stored IP** — Cloudflare's edge necessarily sees the source IP to route any HTTPS request (the same as visiting any website), but the Worker never reads `CF-Connecting-IP`, never logs it, and never stores it. Request logging and analytics retention are disabled. This is an operational promise, stated as such — see [The privacy claim, precisely](#the-privacy-claim-precisely).

## The token

The only durable identifier is a random `install_id` — a `uuid4()` generated on first run and written to `~/.local/share/cctally/install_id` with `0600` permissions. It is derived from nothing about your machine or account, so it is not personally identifying, and it is user-resettable: delete the file (or run `cctally telemetry reset`) and a fresh identity is minted. **The `install_id` never leaves your machine in raw form.**

What actually leaves is a one-way, month-rotating token:

```
period = current calendar month in UTC            e.g. "2026-07"
token  = sha256(f"{install_id}:{period}:{PEPPER}").hexdigest()[:32]
```

`PEPPER` is a fixed, public constant baked into the client (`cctally-install-count-v1`) purely for domain separation — it is not a secret, and disclosing it reveals nothing about any install. Because SHA-256 is one-way and the only linking input (`install_id`) is a local secret, the server receives the token but can never recover the `install_id` from it. Each calendar month uses a different `period`, so July's token and August's token are unrelated hashes: the server holds nothing that links one month's token to the next.

### The privacy claim, precisely

The precise, defensible claim is narrow on purpose: **the token alone is cryptographically unlinkable across months.** It is *not* a claim that no correlation is possible in principle. Residual correlation via source IP plus timing plus the coarse version/OS fields is possible in principle, and it is disclosed here rather than denied. Cloudflare is acknowledged as a **transport processor** — it routes the HTTPS request and unavoidably sees the connecting IP, exactly as it would for any website you visit — and the "no IP stored" guarantee above is an operational promise about how the Worker is configured, not a cryptographic property. We state it plainly so you can judge it for what it is.

## Retention

Retention is by construction, not by a cleanup job:

- Each token is stored with an expiry TTL of about **40 days** (one month plus slack), so every month's tokens auto-evaporate. There is no growing store and no trailing-window counting — counting is always scoped to a single calendar month.
- A small monthly job snapshots just the **integer active-install count** (e.g. "July = 512") into a durable record, then lets the individual tokens expire. That single number is all the maintainer keeps long-term — **zero per-install data**. The version/OS breakdown is not part of the durable record: it is computed on demand by `/stats` only while that month's tokens are still alive (within the ~40-day window) and is gone once they expire.
- The maintainer-only `/stats` read endpoint (behind a bearer secret) returns **only aggregate counts** — it never returns raw tokens, so even the authenticated read surface cannot enumerate the membership set.

## Opting out

Telemetry is on by default (there is already precedent — cctally phones home for update checks). Any one of the following disables it, and the first three take effect immediately with no beat ever sent:

- **`cctally telemetry off`** — the one obvious command. Flips the `telemetry.enabled` config key to `false`. Re-enable with `cctally telemetry on`. See [`commands/telemetry.md`](commands/telemetry.md).
- **`cctally config set telemetry.enabled false`** — the same config key, set directly. Absence of the key means ON (opt-out semantics).
- **`CCTALLY_DISABLE_TELEMETRY=1`** — a cctally-specific environment kill switch, matching the existing `CCTALLY_DISABLE_UPDATE_CHECK` convention. Set it before any command runs (for packagers, CI, and managed environments).
- **`DO_NOT_TRACK=1`** — the cross-tool community standard ([consoledonottrack.com](https://consoledonottrack.com)). If it is set, cctally honors it.

Two more conditions disable telemetry automatically:

- **First-beat grace.** No data leaves on the first eligible run. The client records a first-seen marker and mints the `install_id`, but the first beat is held until that marker is at least **24 hours** old. Every install — interactive, headless, or statusline-only — therefore has a real window to opt out before the first byte leaves.
- **Dev-checkout exclusion.** When cctally is run from a git checkout (a maintainer's or contributor's development tree), telemetry is hard-disabled, so development never inflates the count.

You can see the resolved state and the precedence reason at any time:

```bash
cctally telemetry        # human-readable status + this month's token preview
cctally telemetry --json # machine-readable
cctally doctor           # includes a read-only, always-OK telemetry line
```

The status view is strictly read-only — it resolves the state and previews the current month's token from an existing `install_id` without ever minting one.

## Threat model

Because cctally is open source, the token algorithm is public, so a determined person could forge beats to skew the number. This is stated honestly rather than papered over. What the design cleanly defeats is the thing that actually breaks npm's numbers: **bots and mirrors do not run the client.** So the claim is "a good-faith count of real installs, resistant to the automated inflation that makes download stats useless" — not "a tamper-proof, audited metric." If you need the latter, this is not it, and it does not pretend to be.

## Where the code lives

The client half is `bin/_cctally_telemetry.py` (state resolution, token derivation, the beat, and the `cctally telemetry` command) — stdlib-only, and open in this repository for you to read. The server half is a Cloudflare Worker plus KV; the endpoint is `https://cctally-telemetry.cctally.workers.dev/beat`. There is no third-party analytics processor beyond Cloudflare acting as transport.
