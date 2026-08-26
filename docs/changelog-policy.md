# CHANGELOG policy

This document is the normative policy for `CHANGELOG.md`. `bin/cctally-changelog-lint` enforces the mechanical half of it, and reviewers enforce the rest.

## Who the changelog is for

The changelog addresses one reader: somebody who installed cctally and wants to know what changed for them. It does not address a contributor, a reviewer, or a future maintainer looking for why a decision was made. Detailed internal prose belongs in commit bodies, issue threads and design records instead.

The audience rule is not a matter of taste. `CHANGELOG.md` is published byte-for-byte to the public repository, and the same `[X.Y.Z]` section is reused verbatim as the public snapshot commit message, as the tag message, and as the GitHub release body. One authored section therefore reaches four public surfaces, and every one of them is read by users rather than by maintainers.

## What earns an entry

An entry earns its place when a user could notice the change without reading the source. That covers new or changed subcommands, flags, output, exit codes, dashboard panels, config keys, install and upgrade behaviour, pricing data, and performance a person would actually feel.

Everything else receives no entry: tests, CI, fixtures, goldens, the release and mirror tooling, internal refactors, documentation restructuring, and maintainer-only scripts.

One edge case is worth naming explicitly. A migration or storage change is user-facing when the user must do something or will see something different, and it is not user-facing when it is invisible.

## The four authoring rules

1. One or two sentences, naming the surface the user touches in backticks: the command, the flag, the panel.
2. No issue references.
3. Every path cited must exist in the public tree. Write it inside backticks. The lint also treats an inline Markdown link target and an emphasis span as citations, so those are examined too, but a path written in bare prose is examined by nothing — backticking it is what puts it under the rule.
4. One entry per user-visible change per release, not one per session that touched it. Several sessions landing on one feature collapse into one line at release time.

## The five mechanical predicates

`bin/cctally-changelog-lint` applies these to the whole file. The preamble before the first `##` heading is parsed and preserved but is exempt from the entry predicates.

1. **One physical `- ` line per entry.** Continuation lines and alternate bullet markers are rejected. The stamper deliberately accepts multi-line bullets, so this rule is stated here rather than inherited from it.
2. **No `#\d+\b` anywhere in the entry.** Those numbers name issues in the private repository. GitHub does not autolink `#NNN` inside a `.md` file, but it does autolink in commit messages and release-note bodies, where the number resolves against the public repository and points at an unrelated contributor issue.
3. **At most 240 Unicode code points** after the `- ` marker, counting Markdown syntax. A two-sentence entry passes when the whole entry is within the cap.
4. **No path that the public tree does not carry.** A token in an entry is treated as a citation only when it sits inside one of three deliberate citation forms — a backtick code span, an inline Markdown link target `](…)`, or an emphasis span (`*…*`, `_…_`, `**…**`, `__…__`) — is not home-relative, and opens with a real top-level directory of this repository or carries a source-file extension. Bare prose is not a citation form, because flagging every slash-bearing token would report `dashboard/CLI` and `dashboard/conversation` as dead references. So ordinary prose that contains a slash, such as `-m/--mode`, `Etc/UTC` or `y/N`, is never examined, and neither is a path under the reader's own home directory such as `~/.claude/settings.json` — naming where a user's configuration lives is exactly what this file should do. The path axis then applies two necessary conditions, because they answer different questions and a reader can follow a citation only when both answer yes. The first asks whether the path would be published: where the tree carries the mirror's publication allowlist and the matcher that reads it, that classifier gives the answer, and any verdict other than `public` fails the condition. The second asks whether the tree the lint is running against actually carries the path; a path that is absent there is a dead reference whatever the allowlist would say about it. That question is answered from the repository index rather than the filesystem, so a symlink is never followed out of the tree and an untracked file such as `dashboard/web/node_modules` does not make one tree lint two ways on two machines. A tree with no index falls back to the filesystem, and the lint names which source it consulted. Where both are available the lint reports a path when either condition fails, and each condition contributes only its own clause to the message. Where only one is available — the public mirror carries neither the allowlist nor the matcher, so only the second runs there — that one decides and the lint states in its output which question went unasked. When neither is available, the lint says so rather than reporting a confident pass over a population it could not see.
5. **Markdown link-reference definitions are permitted only in the preamble.** The release stamper retains section headings, subsection headings, bullets and their continuations and nothing else, so a link definition placed after the first release heading would be dropped on the next stamp.

Predicates 2, 3 and 4 all read the **whole entry**: its first line and its continuation lines, joined with a space onto the one physical line predicate 1 requires. Predicate 1 is the exception, because a continuation is the thing it reports. So wrapping an entry does not put its tail out of reach of any rule.

### What is deliberately not a rule

There is no internal-vocabulary denylist. `db migration` and `db journal-repair` are documented user commands in `docs/commands/db.md`, and `alerts test` is documented in `docs/commands/alerts.md`. A denylist over this project's own grammar would refuse correct product language. Whether a topic is user-facing remains an author and reviewer judgment; the lint catches only the mechanical tells.

### Exit codes

`bin/cctally-changelog-lint` returns 0 when clean, 1 for policy findings, 2 for invalid invocation, and 3 for unreadable or malformed input.

### Where the gate fires

Three call sites, in the order a change meets them.

- `bin/cctally-preflight` checks the file before every authoritative test run. It calls the same pure kernel with the same two path predicates, so a finding there is the finding the lint reports. Findings, and an inability to complete the check, are preflight exit 3.
- `bin/cctally-doc-lint-test` runs the lint executable. That harness is what `doc-lint.yml` runs on the Markdown-only pushes the main workflow skips, which is exactly where a changelog edit lands. A finding increments the harness's failure count, so the harness and the workflow exit 1.
- The release tooling's stamp phase checks the file before it stamps anything. A finding is a validation refusal at exit 2 and leaves the tree exactly as it was — no stamp, no commit, no tag. An inability to run the check at all is exit 3, because "your changelog is wrong" and "I could not look" are different answers. `--dry-run` reports the same refusal without touching disk.

## A release with nothing user-facing

Keep the version heading and write one line under a `### Maintenance` subsection:

- Internal improvements only; no user-facing changes.

The operator writes this line before cutting the release. The release tooling never synthesizes it, because a gate that writes the content it is checking would report a pass on text nobody chose.

The subsection heading is required, not stylistic. The release tooling attaches a bullet to a section only when a subsection is open, so a bare bullet is discarded silently and the canonical body of that section comes back empty. Any later promotion whose stable span crosses that release then fails with `CHANGELOG section [X.Y.Z] is empty`.

## A worked example

As published, `v1.102.0` carried eight entries, five of which described the test estate, the mirror preview and an export sanitizer. Under this policy it reads:

```markdown
## [1.102.0] - 2026-08-22

### Added
- `cctally explain` reports which models, projects, sessions, 5-hour bursts, prompt-cache churn, and subagent fan-out account for a window's spend, and names a command to run for each.

### Fixed
- The README's Latest stable section now names the full upgrade range and links its release notes, instead of showing three highlights from one release.
```

## Structure

The Keep a Changelog `### Added` / `### Changed` / `### Fixed` grouping is unchanged, and so is every `## [X.Y.Z] - YYYY-MM-DD` heading. Only entry content is governed by this document.
