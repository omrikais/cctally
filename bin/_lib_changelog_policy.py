"""Pure kernel: does a CHANGELOG entry meet the user-facing policy?

Five structural predicates over `CHANGELOG.md`. Deliberately structural
only: whether a topic is user-facing stays an author and reviewer
judgment, because a vocabulary denylist over this project's own grammar
would refuse `db migration`, `db journal-repair` and `alerts test`, all
of which are documented user commands.

Both halves of the path axis are INJECTED rather than imported, because
this module is published and must stay pure: no filesystem, no git, no
import outside the stdlib.

The two are NECESSARY CONDITIONS, not a decider and a fallback beneath
it, because they answer different questions. `classify` is the mirror's
own predicate and answers "would this path be published if it existed?".
`exists` answers "does the tree this lint runs against actually carry
it?". A reader can follow a citation only when both answers are yes, so
a run that has both reports a finding when either says no.

Treating the classifier as a decider that ends the question passed every
path that would be published but is present in no tree at all. On the
real file that hid seven dead references from the private run which the
public mirror's own run reports, and the public mirror is where the gate
would then have failed.

Where only one is supplied, that one decides and `Report.classifier_note`
states which question went unasked. `.githooks/_match.py` and
`.mirror-allowlist` are unpublished, and `bin/cctally-doc-lint-test` also
runs on the public mirror, so that is the arrangement there. A run with
neither reports the degrade rather than a confident pass over a
population it never saw.
"""

from __future__ import annotations

import dataclasses
import re

DEFAULT_MAX_CODE_POINTS = 240

_ISSUE_RE = re.compile(r"#\d+\b")
_BULLET_RE = re.compile(r"^([-*]) (.*)$")
_SECTION_RE = re.compile(r"^## ")
_SUBSECTION_RE = re.compile(r"^### ")
_LINK_DEF_RE = re.compile(r"^\[[^\]]+\]:\s")
_PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*-]+)+(?:\.[A-Za-z0-9]+)?")

# A `word/word` token is not a path. `-m/--mode`, `Etc/UTC`, `input/output`,
# `I/O`, `A/B`, `y/N` and `npm/brew/public` all match the regex above, and
# treating them as paths reported the `-m/--mode` form the policy's own
# authoring rule endorses as a dead public reference. A token becomes a path
# CANDIDATE only when it opens with a real top-level segment of this tree or
# carries a source-file extension; everything else is prose and is never
# examined, never classified, and never reported.
#
# Both sets are literals rather than derivations, because this kernel must stay
# pure and importable with no git and no filesystem access. The segments were
# taken from `git ls-files | cut -d/ -f1 | sort -u` on 2026-08-25.
#
# Publishing this set discloses nothing, and the reason is NOT that both trees
# carry every entry — nine of the fifteen do not: `.agent-workflows`, `.agentmem`,
# `.agents`, `.claude`, `.claude-memory`, `.codex`, `.githooks`, `homebrew` and
# `telemetry` are absent from the public tree. It is that a top-level directory
# name identifies no file. The disclosure this module must avoid is a list of
# maintainer-only FILES, which is what its predecessor shipped; a name such as
# `.agentmem` tells a reader that some private directory exists and nothing about
# what is in it. `_SOURCE_EXTENSIONS` holds suffixes and identifies nothing at all.
_REPO_TOP_LEVEL = frozenset({
    ".agent-workflows", ".agentmem", ".agents", ".claude", ".claude-memory",
    ".codex", ".githooks", ".github", "bench", "bin", "dashboard", "docs",
    "homebrew", "telemetry", "tests",
})
_SOURCE_EXTENSIONS = (
    ".py", ".sh", ".ts", ".tsx", ".md", ".json", ".yml", ".yaml", ".sql", ".toml",
)


@dataclasses.dataclass(frozen=True)
class Entry:
    line: int
    marker: str
    content: str
    continuations: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    detail: str


@dataclasses.dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...]
    classifier_available: bool
    classifier_note: str


def parse_entries(text: str) -> list[Entry]:
    """Every bullet at or after the first `## ` heading, with its continuations.

    Bullets in the preamble are not entries and are not returned.

    Code fences suppress heading and bullet detection, and a fenced line
    continues an open bullet — the same rules `bin/_lib_changelog_stamp.py`
    applies, including recognizing ``` and not `~~~`. The lint has to agree
    with the parser that actually stamps the file: without this, a fenced
    sample beginning at column 0 with `## `, `### ` or `- ` is a heading or a
    bullet to one of them and sample text to the other.
    """
    entries: list[Entry] = []
    in_sections = False
    in_fence = False
    # The index of the entry a continuation line belongs to, or None. A HEADING
    # clears it: an indented line that appears after `### Fixed` but before that
    # subsection's first bullet continues nothing, and appending it to the
    # previous subsection's last entry made the `multiline` finding name an
    # entry that is not on the same line and is not the offender.
    open_entry: "int | None" = None

    def _continue(index: int, line: str) -> None:
        last = entries[index]
        entries[index] = dataclasses.replace(
            last, continuations=last.continuations + (line,)
        )

    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if open_entry is not None:
                _continue(open_entry, line)
            continue
        if in_fence:
            if open_entry is not None:
                _continue(open_entry, line)
            continue
        if _SECTION_RE.match(line):
            in_sections = True
            open_entry = None
            continue
        if not in_sections:
            continue
        if _SUBSECTION_RE.match(line):
            open_entry = None
            continue
        match = _BULLET_RE.match(line)
        if match:
            entries.append(Entry(number, match.group(1), match.group(2), ()))
            open_entry = len(entries) - 1
            continue
        if (
            open_entry is not None
            and line.strip()
            and (line.startswith("  ") or line.startswith("\t"))
        ):
            _continue(open_entry, line)
    return entries


def cites_an_issue(text: str) -> bool:
    """Does ``text`` carry an issue reference? THE definition, for every tool.

    `bin/cctally-changelog-audit` compiled its own copy of the same literal. The
    two agreed only because nobody had edited one of them, and both tools
    reporting 835 for the real file rested on that.
    """
    return bool(_ISSUE_RE.search(text))


def entry_text(entry: Entry) -> str:
    """The whole entry, as the one physical line the policy requires it to be.

    ONE scope for every content rule. `issue-ref` read the first line and the
    continuations, while `too-long` and `private-path` read only the first line,
    so three rules over one entry measured three different populations: a
    wrapped entry was under the length cap because its first line was, and seven
    path citations in the real file sit only on continuation lines.

    Joined with a SPACE, not with nothing and not with a newline. The policy
    requires the entry on one physical line, so this models the entry an author
    must produce; joining with nothing would fuse the tail of one line to the
    head of the next and invent a token nobody wrote.
    """
    return " ".join(
        (entry.content, *(line.strip() for line in entry.continuations))
    )


def _looks_like_repo_path(token: str) -> bool:
    """Is this token a repository path, or is it ordinary prose with a slash?"""
    return (
        token.split("/", 1)[0] in _REPO_TOP_LEVEL
        or token.endswith(_SOURCE_EXTENSIONS)
    )


def _delimiter_runs(text: str, char: str) -> list[tuple[int, int]]:
    """Every maximal run of ``char``, as half-open (start, end) offsets."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] == char:
            start = index
            while index < len(text) and text[index] == char:
                index += 1
            runs.append((start, index))
        else:
            index += 1
    return runs


def _backtick_spans(text: str) -> list[tuple[int, int, int, int]]:
    """Every backtick code span, as (open_start, content_start, content_end, close_end).

    A run of N backticks pairs with the next run of exactly N, which is
    CommonMark's rule, so a `` ``…`` `` span still encloses its content and an
    unpaired backtick opens nothing.
    """
    runs = _delimiter_runs(text, "`")
    spans: list[tuple[int, int, int, int]] = []
    position = 0
    while position < len(runs):
        open_start, open_end = runs[position]
        width = open_end - open_start
        for other in range(position + 1, len(runs)):
            close_start, close_end = runs[other]
            if close_end - close_start == width:
                spans.append((open_start, open_end, close_start, close_end))
                position = other + 1
                break
        else:
            position += 1
    return spans


def _code_span_ranges(text: str) -> list[tuple[int, int]]:
    """The content range of every backtick code span in ``text``."""
    return [(content_start, content_end)
            for _open, content_start, content_end, _close in _backtick_spans(text)]


_LINK_TARGET_RE = re.compile(r"\]\(([^)]*)\)")


def _link_target_ranges(text: str) -> list[tuple[int, int]]:
    """The target range of every inline Markdown link, the `](…)` part."""
    return [match.span(1) for match in _LINK_TARGET_RE.finditer(text)]


def _is_punctuation(char: str) -> bool:
    return not char.isalnum() and not char.isspace()


def _flanking(text: str, start: int, end: int) -> tuple[bool, bool]:
    """Is the delimiter run at ``start``:``end`` left-flanking, right-flanking?

    CommonMark's definition. A run outside the text is treated as if surrounded
    by whitespace, which is what the two defaults encode.
    """
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    left = not after.isspace() and (
        not _is_punctuation(after) or before.isspace() or _is_punctuation(before)
    )
    right = not before.isspace() and (
        not _is_punctuation(before) or after.isspace() or _is_punctuation(after)
    )
    return left, right


def _emphasis_ranges(text: str) -> list[tuple[int, int]]:
    """The content range of every `*…*`, `**…**`, `_…_` and `__…__` span.

    The flanking rule is not decoration. Without it the underscores inside two
    ordinary identifiers — `cache_db` and `stats_db` in one sentence — pair with
    each other, and every slash-bearing token between them becomes a citation
    nobody made. CommonMark's rule is that an intraword `_` neither opens nor
    closes emphasis, and that is exactly the case this must reject.

    Pairing is deliberately conservative: a run pairs with the next run of the
    same character and the same width that can close. CommonMark's full
    rule-of-three is not reproduced, because the only question here is whether a
    token sits inside a span an author wrote as emphasis.
    """
    spans: list[tuple[int, int]] = []
    for char in ("*", "_"):
        runs = _delimiter_runs(text, char)
        position = 0
        while position < len(runs):
            start, end = runs[position]
            left, right = _flanking(text, start, end)
            can_open = left and (char == "*" or not right
                                 or _is_punctuation(text[start - 1] if start else " "))
            if not can_open:
                position += 1
                continue
            for other in range(position + 1, len(runs)):
                close_start, close_end = runs[other]
                if close_end - close_start != end - start:
                    continue
                close_left, close_right = _flanking(text, close_start, close_end)
                can_close = close_right and (
                    char == "*" or not close_left
                    or _is_punctuation(text[close_end] if close_end < len(text) else " ")
                )
                if can_close:
                    spans.append((end, close_start))
                    position = other + 1
                    break
            else:
                position += 1
    return spans


def _citation_ranges(text: str) -> list[tuple[int, int]]:
    """Every range in ``text`` whose content counts as a deliberate citation.

    Three forms, all of them ways an author points a reader at a file: a
    backtick code span, an inline Markdown link target, and an emphasis span.
    Bare prose is not one of them, and widening to it would report
    `dashboard/CLI` and `dashboard/conversation` again — the false-positive
    class the backtick gate was introduced to suppress.

    Code spans are found first and then MASKED OUT before the other two are
    scanned, because a code span takes precedence over emphasis in CommonMark
    and because a glob spelling carries a literal `*`. Two globs on one line
    would otherwise pair their asterisks and swallow the prose between them.
    """
    code = _backtick_spans(text)
    masked = list(text)
    for open_start, _content_start, _content_end, close_end in code:
        for index in range(open_start, close_end):
            masked[index] = " "
    outside_code = "".join(masked)
    return (
        [(content_start, content_end)
         for _open, content_start, content_end, _close in code]
        + _link_target_ranges(outside_code)
        + _emphasis_ranges(outside_code)
    )


def _is_home_relative(content: str, start: int, token: str) -> bool:
    """Does the match at ``start`` name the reader's home directory?

    `~/.claude/settings.json` is where a user's own configuration lives, and
    naming it is exactly what a user-facing changelog should do. `~` is outside
    the path regex's character class, so the match opens at `.claude` and the
    truncated `.claude/settings.json` was reaching classification and being
    reported as a dead public reference.
    """
    before = content[:start]
    if before.endswith("~/") or before.endswith("${HOME}/"):
        return True
    # `$HOME/x` matches from the `H`, because `$` is outside the character class
    # while `HOME` is inside it, so the token itself carries the prefix.
    return before.endswith("$") and token.startswith("HOME/")


def _cited_paths(entry: Entry) -> list[str]:
    """The repository paths this entry cites, as opposed to mentions in prose.

    Read over the WHOLE entry, per `entry_text`, so a citation on a continuation
    line is examined like any other.

    Three purely lexical gates, in order, and none of them consults git, the
    filesystem or the classifier — so a candidate set is identical on the
    private tree and on the public mirror.

    1. The token must sit inside a deliberate citation form: a backtick code
       span, an inline Markdown link target, or an emphasis span. A token in
       running prose is not a citation. Requiring a backtick alone was a hard
       rule nothing enforced — `[docs/x.md](docs/x.md)` and `**docs/x.md**`
       both produced an empty candidate list, so an entry rewritten as
       `[the release runbook](docs/RELEASE.md)` would publish a private path
       with the gate silent.
    2. A home-relative token names the reader's machine, not this tree.
    3. The token must open with a real top-level segment of this tree or carry
       a source-file extension; everything else is prose with a slash in it.
    """
    cited: list[str] = []
    seen: set = set()
    # Scanned INSIDE each range rather than over the whole line and then tested
    # for containment. `*` and `_` are both inside the path regex's character
    # class, so a scan over the raw line absorbs the emphasis delimiters:
    # `*docs/a.md*` matched the token `docs/a.md*`, which then failed the
    # containment test, and `_docs/a.md_` matched `_docs/a.md_`, which is not a
    # repository path at all. Scanning the range's content puts the delimiters
    # outside the text being matched, and makes the containment test unnecessary.
    text = entry_text(entry)
    for low, high in _citation_ranges(text):
        for match in _PATH_RE.finditer(text[low:high]):
            token = match.group(0).rstrip(".,;:")
            start = low + match.start()
            if (start, token) in seen:
                continue
            seen.add((start, token))
            if _is_home_relative(text, start, token):
                continue
            if _looks_like_repo_path(token):
                cited.append(token)
    return cited


def _dead_reference_detail(path: str, not_public: bool, absent: bool) -> str:
    """The finding text for one cited path, or "" when it is followable.

    Each condition contributes only the fact it established. A path that fails
    both carries both clauses, because a message naming one of two established
    facts understates what the run found.
    """
    if not_public and absent:
        return (
            f"`{path}` is not carried by the public tree and does not exist in "
            "this tree, so the reference is dead either way"
        )
    if not_public:
        return (
            f"`{path}` is not carried by the public tree, so the reference is "
            "dead there"
        )
    if absent:
        return f"`{path}` does not exist in this tree, so the reference is dead"
    return ""


def check_text(
    text: str,
    *,
    classify=None,
    exists=None,
    max_code_points: int = DEFAULT_MAX_CODE_POINTS,
) -> Report:
    """Check ``text`` against the five structural predicates.

    ``classify(paths) -> {"public": [...], "private": [...], "unmatched": [...]}``
    is the mirror's own predicate and answers whether a path WOULD be published.
    ``exists(path) -> bool`` answers whether the tree being linted actually
    carries it. Both are injected; this module opens nothing and imports nothing
    outside the stdlib.

    **Scope.** Every content rule — `issue-ref`, `too-long` and `private-path` —
    reads `entry_text`, the whole entry with its continuations joined onto one
    line. `multiline` is the exception by definition, because the presence of a
    continuation is the thing it reports.

    The two are necessary conditions, so where both are supplied a path is
    reported when either answers no. Where one is supplied it decides alone and
    ``Report.classifier_note`` names the question that went unasked.

    Three deliberate consequences of that arrangement, none of which is a
    defect to be fixed by widening the kernel:

    * **A false negative with no classifier.** A maintainer-only file is
      present in the private tree, so ``exists`` answers True and a run with no
      classifier reports nothing about it. ``Report.classifier_note`` states
      that the classifier did not run — that note is the surface telling a
      reader the answer is approximate.
    * **Every glob spelling is a finding, but the two conditions do NOT agree
      about why.** A glob resolves to no literal path, so ``exists`` always
      answers False. The classifier's answer varies with the allowlist: of the
      six spellings the real file carries, three answer ``unmatched`` and three
      answer ``public``, because an allowlist pattern can match the literal
      ``*`` character in the token. So the existence condition is what refuses
      a glob in every case, and the joined message says only what each
      condition established. The fix is to cite a real file or drop the
      citation.
    * **A token the classifier accounts for nowhere is treated as not public.**
      A verdict that omits a path it was asked about establishes nothing about
      it, and the privacy axis fails closed rather than open.
    """
    findings: list[Finding] = []
    entries = parse_entries(text)

    for entry in entries:
        if entry.marker != "-" or entry.continuations:
            findings.append(Finding(
                entry.line, "multiline",
                "an entry is one physical `- ` line; found "
                + ("a `*` marker" if entry.marker != "-" else
                   f"{len(entry.continuations)} continuation line(s)"),
            ))
        # Every content rule reads `entry_text` — the whole entry, first line
        # and continuations, joined as the one physical line the policy
        # requires. Reading `content` alone made this lint and
        # `bin/cctally-changelog-audit` report different populations for the
        # issue rule, 831 against 835 on the real file; widening only that rule
        # then left three rules over one entry reading two different scopes.
        text_of_entry = entry_text(entry)
        if cites_an_issue(text_of_entry):
            findings.append(Finding(
                entry.line, "issue-ref",
                "issue references name private issues and resolve against the "
                "public repository in commit messages and release notes",
            ))
        length = len(text_of_entry)
        if length > max_code_points:
            findings.append(Finding(
                entry.line, "too-long",
                f"{length} code points; the cap is {max_code_points}",
            ))

    # TWO NECESSARY CONDITIONS, never a decider with a fallback beneath it.
    # `classify` answers "would this path be published if it existed?" and
    # `exists` answers "does this tree carry it?"; a public reader can follow
    # the citation only when both answer yes. Ranking the classifier above the
    # existence check silently passed every path that classifies `public` and
    # is present in no tree at all — seven dead references on the real file,
    # invisible to the private run and reported by the mirror's own run, which
    # is the tree where the gate would then have failed.
    #
    # This replaced an enumeration of forty private file-name families. THIS
    # FILE IS PUBLISHED, so a list built to keep internal information out of
    # the public repository was shipping forty internal filenames into it. The
    # enumeration was also failing on its own terms: its message asserted "is
    # not carried by the public tree" for a family whose real path is public,
    # on the one tree where no second layer can correct the message, and 622 of
    # 2,372 non-public tracked files matched no family at all. The injected
    # conditions need no enumeration, so they disclose nothing; they have no
    # blind spots, because every absent path is caught; and each states only
    # what it established.
    #
    # Each condition therefore contributes its OWN clause to the finding text.
    # Neither may claim the other's fact, and a path that fails both says so.
    classifier_available = classify is not None
    cited = [(entry.line, path) for entry in entries for path in _cited_paths(entry)]
    wanted = {path for _line, path in cited}

    not_public: set = set()
    if classify is not None and wanted:
        verdict = classify(sorted(wanted))
        # Subtracting `public` rather than unioning `private` and `unmatched`
        # is what makes this fail CLOSED. A token the verdict accounts for in
        # no list establishes nothing, and the earlier form treated exactly
        # that case as published.
        not_public = wanted - set(verdict.get("public", ()))

    absent: set = set()
    if exists is not None:
        absent = {path for path in wanted if not exists(path)}

    # A dict rather than a set, so a path cited twice on one entry line is one
    # finding carrying one message, exactly as it was under the single-branch
    # form.
    flagged: dict = {}
    for line, path in cited:
        detail = _dead_reference_detail(path, path in not_public, path in absent)
        if detail:
            flagged[(line, path)] = detail
    for (line, path), detail in sorted(flagged.items()):
        findings.append(Finding(line, "private-path", detail))

    # Link-reference definitions are dropped by the stamper once they follow a
    # release heading, so they are permitted only in the preamble.
    in_sections = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _SECTION_RE.match(line):
            in_sections = True
            continue
        if in_sections and _LINK_DEF_RE.match(line):
            findings.append(Finding(
                number, "link-def",
                "a link-reference definition after the first release heading is "
                "discarded by the next stamp; put it in the preamble",
            ))

    if classifier_available and exists is not None:
        note = ""
    elif classifier_available:
        note = (
            "a cited path was checked against the mirror classifier only, "
            "because no tree-existence predicate was supplied; the classifier "
            "answers whether a path would be published, so a path that would "
            "be published and is carried by no tree was not caught"
        )
    elif exists is not None:
        note = (
            "the mirror classifier did not run, because the allowlist and the "
            "matcher it needs are unpublished; a cited path was checked for "
            "existence in this tree instead, which cannot see a path that is "
            "present here and unpublished"
        )
    else:
        note = (
            "the cited-path rule did not run: this check was given neither the "
            "mirror classifier nor a tree-existence predicate, so no cited path "
            "was checked"
        )
    return Report(
        tuple(sorted(findings, key=lambda f: (f.line, f.rule))),
        classifier_available,
        note,
    )


def format_report(report: Report, path: str = "CHANGELOG.md") -> str:
    """Render the findings, each prefixed with the file they were found in.

    ``path`` is the file the caller actually read. Hard-coding the literal
    `CHANGELOG.md` here printed `CHANGELOG.md:6:` for a `--file probe.md` run,
    which names a file the run never opened.
    """
    lines = [f"{path}:{f.line}: {f.rule}: {f.detail}" for f in report.findings]
    if report.classifier_note:
        lines.append(f"note: {report.classifier_note}")
    return "\n".join(lines)
