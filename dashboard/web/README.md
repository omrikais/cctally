# cctally dashboard — web frontend

React + TypeScript source for the `cctally dashboard` web UI.

## Setup (one-time per clone, or after dependency bump)

```bash
cd dashboard/web
nvm use            # picks up .nvmrc → Node 24.11.1
npm ci
```

Requires **Node 24.11.x** (pinned via `.nvmrc`; enforced by `package.json` `engines`).
Other Node majors will refuse to install. The pin exists because Vite/Rolldown
output isn't byte-stable across Node majors — building on a different version
would dirty `dashboard/static/` even when source is unchanged. Use `nvm`, `fnm`,
or `asdf` so `cd dashboard/web` auto-switches.

After upgrading dependencies (lockfile bump), always `npm ci` BEFORE `npm run build`
so node_modules matches the lockfile. A stale `node_modules` (e.g. left over from
a prior major version of Vite) silently produces non-canonical bundle bytes.

## Dev loop

```bash
# Terminal 1 — Python backend
cctally dashboard --no-open

# Terminal 2 — frontend with HMR
cd dashboard/web
npm run dev
# → open http://localhost:5173
```

Vite's dev server at `:5173` proxies `/api/*` and `/static/*` to the Python server at `:8789`.
The Origin header is rewritten on proxied requests so the Python CSRF check accepts them.

## Tests

```bash
npm run test          # one-shot
npm run test:watch    # watch mode
```

## Build

```bash
nvm use            # ensure Node 24.11.x active
npm run build
```

This runs `tsc --noEmit` first, then `vite build`. Output goes to `../static/` (i.e. `dashboard/static/`),
which is committed to the repo. Users never need to run npm; the built bundle ships with the code.

The bundle is byte-stable on **Node 24.11.x + the lockfile-declared deps**. If your
rebuild produces a different `index-*.{js,css}` hash than what's committed, the
likely cause is one of:
- Wrong Node major active (check `node --version`)
- Stale `node_modules` not matching the lockfile (run `npm ci`)

## Commit convention

**Every frontend change is one commit that includes BOTH the `dashboard/web/src/` diff AND the resulting `dashboard/static/` diff.**
This keeps `main` always consistent: every checkout is a working dashboard.

```bash
npm run build
cd ../..
# use /commit skill — never subject-only; include a detailed body
```

## Fixture HOMEs for design work

Run the dashboard against fixture data to exercise extreme states:
```bash
HOME=$(pwd)/tests/fixtures/dashboard/forecast-low-conf \
  cctally dashboard --no-open
# then npm run dev in another terminal to see the React UI against that data
```

Useful fixtures:
- `tests/fixtures/dashboard/forecast-low-conf/` — LOW CONF banner
- `tests/fixtures/dashboard/reset-week/` — mid-week reset current-week milestones
- `tests/fixtures/dashboard/capped/` — OVER verdict

## Threshold alerts (v1)

The dashboard surfaces threshold alerts (off by default; see
`docs/commands/alerts.md`):

- New ninth panel **"Recent alerts"** — press `9` to open the modal with
  the full alert history for the current envelope. Collapsible from the
  panel header chevron.
- Settings overlay (`s`) has a **"Threshold alerts"** fieldset:
  enable/disable toggle, read-only display of weekly + 5h threshold
  lists, and a **Send test alert** button that round-trips through
  `POST /api/alerts/test` and renders an alert toast.
- Toasts have two variants — alert toasts (amber `<95%`, red `>=95%`)
  vs. status toasts (neutral). Both are click-to-dismiss; alert toasts
  also live persistently in the panel.

## Pointers

- Architecture + gotchas: repo root `CLAUDE.md`
- Python handler source of truth for envelope shape: `bin/cctally#snapshot_to_envelope`
