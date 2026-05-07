# `cctally release`

Stamp `CHANGELOG.md`, cut a SemVer tag, propagate the commit + tag to the
public mirror, and create a GitHub Release. Four idempotent phases run in
sequence; any failure can be recovered via `--resume`. The same body
string flows through the stamp commit's public block, the tag annotation,
and the GitHub Release notes — single-source from the canonical
CHANGELOG section.

## Synopsis

```
cctally release <kind> [flags]
cctally release --resume [flags]
cctally release --dry-run <kind> [flags]
```

## When to use it

- You've finished a feature batch on `main`, all `[Unreleased]` entries
  are written in Keep-a-Changelog form, and you want one command to
  stamp + tag + publish.
- A previous release run failed mid-publish (network glitch, missing
  `gh auth`, dirty mirror clone) and you want to resume.
- You want to preview what the next cut will look like (`--dry-run`)
  without mutating the working tree.

## Bump kinds

Picks the next version by reading the latest `## [X.Y.Z] - YYYY-MM-DD`
header from `CHANGELOG.md`:

| Kind         | From            | To                                               |
|--------------|-----------------|--------------------------------------------------|
| `patch`      | `1.0.0`         | `1.0.1`                                          |
| `minor`      | `1.0.0`         | `1.1.0`                                          |
| `major`      | `1.0.0`         | `2.0.0`                                          |
| `prerelease` | `1.0.0`         | `1.1.0-rc.1` (with `--bump minor`; required)     |
| `prerelease` | `1.1.0-rc.1`    | `1.1.0-rc.2` (`--bump` REFUSED on prerelease)    |
| `finalize`   | `1.1.0-rc.2`    | `1.1.0` (drops the `-rc.N` suffix)               |
| (any kind)   | (no prior tag)  | `0.0.1` / `0.1.0` / `1.0.0` (kind-driven)        |

`--prerelease-id` overrides the default `rc` identifier (e.g. `--bump
minor --prerelease-id beta` produces `1.1.0-beta.1`).

## Flags

| Flag                 | Default     | Description                                                                                                          |
|----------------------|-------------|----------------------------------------------------------------------------------------------------------------------|
| `kind` (positional)  | required    | One of `patch`, `minor`, `major`, `prerelease`, `finalize`. Omit only with `--resume`.                               |
| `--resume`           | off         | Continue an in-progress release; infers `vX.Y.Z` from the latest CHANGELOG header. Mutually exclusive with `kind` / `--bump`. |
| `--dry-run`          | off         | Print the phase plan + unified CHANGELOG diff + tag annotation + mirror plan + gh plan; mutate nothing. Exits 0 on a clean dry-run, 2 if the stamp itself would refuse. |
| `--no-publish`       | off         | Run phases 1 + 2 only (stamp + tag + push); skip phase 3 (mirror) and phase 4 (gh release).                          |
| `--bump {patch,minor,major}` | none | REQUIRED with `prerelease` when current is stable; REFUSED when current is already a prerelease.                     |
| `--prerelease-id ID` | `rc`        | Override the default prerelease identifier (e.g. `beta`, `alpha`).                                                   |
| `--remote NAME`      | `origin`    | Private remote name (used for `git push`, tag push, fetch).                                                          |
| `--allow-branch NAME`| `main` only | Escape hatch — permit cutting from a non-`main` branch (e.g. `--allow-branch hotfix/1.0.x`).                         |
| `--public-clone PATH`| see below   | Override public-clone discovery (defaults to `git config release.publicClone` then the marker file).                 |

## Phases

Four ordered, individually idempotent phases:

### Phase 1 — Stamp CHANGELOG.md

Move every bullet under `## [Unreleased]` into a new `## [X.Y.Z] -
YYYY-MM-DD` section, leaving `[Unreleased]` empty. Stage `CHANGELOG.md`
(only — the staging guard refuses if anything else is staged), build the
commit message (private body + `--- public ---` block carrying the
canonical CHANGELOG body), and commit with `--cleanup=verbatim` so the
`### Added` / `### Fixed` headings survive.

Refuses with exit 2 if `[Unreleased]` is empty or `CHANGELOG.md` is
missing. Idempotent: the stamp-done signal is true when `HEAD`'s
`CHANGELOG.md` blob already carries the `## [version] - <today>` header
AND `HEAD`'s subject is exactly `chore(release): vX.Y.Z`.

**Resume safety:** if the header is on disk but `HEAD`'s subject is
different (an unrelated commit landed on top), the resume guard exits 2
with a diagnostic rather than tagging the wrong SHA.

**Skip-chain exemption:** `chore(release): vX.Y.Z` and `feat(release):
vX.Y.Z[-id.N]` subjects are unconditionally exempt from the public-mirror
skip-chain refuse gate (release-voice subjects are themselves the signal
of intentional bundling), so `--accept-skip-mismatch` is never needed
for normal release commits.

### Phase 2 — Annotated tag + push

Tag the stamp commit's SHA (threaded explicitly from Phase 1 — never
re-reads `HEAD`) with `vX.Y.Z`, annotation body re-parsed from
`CHANGELOG.md` and run through the same canonical-body helper Phase 1
used. Tag is signed (`-s`) iff `user.signingkey` is set AND
`tag.gpgsign` is `true`; otherwise annotated unsigned (`-a`).

Push uses `git push <remote> <branch> --follow-tags`, then a belt-and-
suspenders explicit `git push <remote> refs/tags/v<version>` because
`--follow-tags` skips tags whose target commit is already on the remote
(the resume-after-manual-push case).

### Phase 3 — Mirror push

Three sub-steps from the public clone (resolved via public-clone
discovery):

1. `bin/cctally-mirror-public --yes --public-clone <path>` — replay
   private commits onto the local public clone.
2. `git -C <public-clone> push origin <branch>` — branch is read
   dynamically (`git rev-parse --abbrev-ref HEAD`), NOT hardcoded `main`.
3. `git -C <public-clone> push origin refs/tags/v<version>` — push the
   new tag.

The mirror tool itself does NOT push the public clone — phase 3 owns
the push step explicitly. Skipped under `--no-publish`.

### Phase 4 — GitHub Release

`gh release create vX.Y.Z --repo omrikais/cctally --title vX.Y.Z
--notes-file <body>` (with `--prerelease` for `-id.N` versions). Body
is the canonical CHANGELOG section — byte-identical to the public block
of the stamp commit and the tag annotation.

**Auth fallback (returns 0):** if `gh auth status` or `gh api
repos/omrikais/cctally` fails, prints a copy-pasteable `gh release
create` command (notes go via a `/tmp/release-notes-vX.Y.Z.md` heredoc
with a randomized `CCTALLY_EOF_<pid>` terminator so a body that contains
a bare `EOF` line doesn't prematurely close the heredoc) and returns 0.
Phases 1–3 already succeeded; the release IS published from the public
mirror's perspective. Skipped under `--no-publish`.

## `--resume`

`--resume` infers the target version from the latest CHANGELOG header
and walks the four phases, short-circuiting any whose done-signal is
true:

- **Phase 1 done:** `CHANGELOG.md` has the header AND `HEAD`'s subject
  is `chore(release): vX.Y.Z`.
- **Phase 2 done:** `vX.Y.Z` exists locally AND on the configured
  remote.
- **Phase 3 done:** `vX.Y.Z` exists on the public clone's `origin`
  (read-only `git ls-remote --tags`).
- **Phase 4 done:** `gh release view vX.Y.Z --repo omrikais/cctally`
  returns 0.

If all four signals are true, exits 0 immediately with `release vX.Y.Z
already published`.

`--resume` is mutually exclusive with `kind` / `--bump`. The phase-done
checks are read-only network/git ops.

## Public-clone discovery

The public-clone path is resolved in this priority order:

1. `--public-clone <path>` flag.
2. `git config --get release.publicClone` (camelCase — git 2.46+
   rejects underscore-bearing keys at write time).
3. `~/.local/share/cctally/release-public-clone-path` plain-text marker
   file.

Refuses with exit 2 if all three are absent. No silent fallback to a
hard-coded path.

**One-time operator setup before the first cut:**

```bash
git config release.publicClone /path/to/public-clone
```

Or write the path to the marker file:

```bash
echo /path/to/public-clone > ~/.local/share/cctally/release-public-clone-path
```

## Examples

```bash
# Standard minor cut from a clean main, [Unreleased] populated.
cctally release minor

# Preview what the next minor would look like; no mutation.
cctally release minor --dry-run

# Start a release-candidate cycle off the current 1.0.0 release.
cctally release prerelease --bump minor          # → 1.1.0-rc.1
cctally release prerelease                       # → 1.1.0-rc.2 (later)
cctally release finalize                         # → 1.1.0

# Stamp + tag locally; defer mirror + gh release for later.
cctally release patch --no-publish

# Resume after the gh release create step failed (no `gh auth login`).
cctally release --resume

# Cut from a hotfix branch with explicit override.
cctally release patch --allow-branch hotfix/1.0.x

# Override public-clone discovery for a one-off cut.
cctally release minor --public-clone /tmp/cctally-mirror-clone
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (or auth-fallback path in phase 4 — phases 1-3 published, phase 4 awaits manual completion). |
| `1` | Unhandled internal exception (rare; report as a bug if it fires). |
| `2` | Refusal — wrong branch, dirty tree, behind remote, tag already exists, empty `[Unreleased]`, missing public-clone discovery, HEAD-not-stamp-commit on resume, mutually exclusive flags. |
| `3` | Mid-publish failure — staging guard tripped, mirror replay/push failed, hard `gh release create` failure after auth was confirmed OK. Re-run with `--resume`. |

## Authoring discipline

`[Unreleased]` entries follow [Keep-a-Changelog
1.1.0](https://keepachangelog.com/en/1.1.0/) conventions:

- `### Added` — new features.
- `### Changed` — changes to existing functionality.
- `### Deprecated` — soon-to-be-removed features.
- `### Removed` — removed features.
- `### Fixed` — bug fixes.
- `### Security` — security-relevant fixes.

Bullet style: `- ` or `* ` markers (not `+ `). Multi-line bullets are
preserved as a continuous block as long as continuation lines are
indented (`  ` or `\t`). Code-fenced blocks (`` ``` ``) suppress
heading detection inside the bullet.

The `changelog-updater` skill (in this repo's `.claude/skills/`)
appends entries automatically as features land — invoke it alongside
the implementing commit so `[Unreleased]` stays current and the next
`cctally release` has a non-empty section to stamp.

## Body-canonical-three-sources invariant

The body string carried by:

- the public block of the stamp commit (`--- public ---` section);
- the annotated tag's body;
- the GitHub Release notes body;

is **byte-identical** — produced by the canonical-body helper on the
same parsed `CHANGELOG.md` section. Phases 2 and 4 re-parse from disk
rather than threading the string through, so the canonical body is
re-derivable from `CHANGELOG.md` at any point.

For signed tags, the `-----BEGIN PGP SIGNATURE-----`-onward block is
stripped before comparison — the signature is appended after the
annotation body.

Manual edits to one surface (e.g. editing the GitHub Release notes via
the web UI) do NOT propagate back. To re-stamp from CHANGELOG truth
after a manual divergence, edit `CHANGELOG.md` and re-run with
`--resume` to regenerate the body from the parsed section.

## Notes

- **Stamping is byte-stable across runs** — the CHANGELOG stamper
  rstrip-trailing-newlines the preamble before re-serializing, so
  re-stamping the same input produces a no-op diff.

## See also

- [`bin/cctally-mirror-public`](../../bin/cctally-mirror-public) — the
  mirror tool invoked by phase 3.
- [`changelog-updater`](../../.claude/skills/changelog-updater/) — skill
  for appending `[Unreleased]` entries as features land.
- [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) — the
  bullet-format spec the parser implements.
