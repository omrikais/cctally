# Contributing to cctally

Thanks for your interest in cctally! This guide explains how to report
problems and how (and how *not*) to send changes.

## How this repo works

cctally is developed in a private repository and published here as a
**read-only mirror**. This public repo is the right place to **report bugs
and request features**, but the private repo is the source of truth — code
lands here automatically when the maintainer syncs a release.

That has one practical consequence for pull requests; see
[Pull requests](#pull-requests) below.

## Reporting bugs and requesting features

**[Open a GitHub issue](https://github.com/omrikais/cctally/issues)** — this
is the primary way to contribute. A good report includes:

- `cctally --version`
- Your OS / platform (e.g. macOS 15, Ubuntu 24.04)
- Relevant output from `cctally doctor` (a read-only health report)
- Steps to reproduce, and what you expected vs. what happened

**Please don't paste raw session data or credentials.** Your Claude session
JSONL can contain prompt/response content; `cctally doctor` output is safe to
share, but scrub anything sensitive before posting.

## Pull requests

Small, focused fixes (typos, clear bugs, doc corrections) are welcome — but
please read this first so your effort isn't wasted:

> Because this repo is a one-way mirror, **PRs opened here cannot be merged in
> the usual way.** Accepted changes are ported by hand into the private source
> and arrive here on the next sync. In practice that means your PR will be
> **closed with credit, not shown as "merged."** The change still ships — it
> just travels through the private repo to get here.

For anything beyond a small fix, **open an issue first** to agree on the
approach before you write code.

## Local development

cctally is a **stdlib-only Python 3** program — `bin/cctally` plus a family of sibling modules in `bin/` — with no install step and no build step for the CLI.

- Sanity-check a change: `python3 -m py_compile bin/cctally`
- Run the test suite: `bin/cctally-test-all`
- The web dashboard lives in `dashboard/web/` (Vite + React + TypeScript) and
  its built output is committed under `dashboard/static/`. If you touch the
  dashboard source, rebuild it (`npm run build` in `dashboard/web/`) and
  commit the regenerated bundle.

## Conventions

- **Keep the CLI zero-dependency** — stdlib only. The whole point of cctally is
  that it runs offline with no package manager. (`rich` is lazy-imported for the
  TUI; the dashboard island is the only build-time dependency surface.)
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
  messages (`feat:`, `fix:`, `docs:`, …).
- Match the style of the surrounding code.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache License 2.0](LICENSE).
