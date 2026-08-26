"""The user-facing CHANGELOG policy kernel, and the tools over it.

`CHANGELOG.md` is published byte-for-byte to the public mirror, and the same
section text is reused as the public snapshot commit message, the tag message
and the GitHub release body. One authored section therefore reaches four public
surfaces, so the file has one audience: somebody who installed cctally and
wants to know what changed for them.

The kernel is deliberately structural only. There is no internal-vocabulary
denylist, because `db migration`, `db journal-repair` and `alerts test` are all
documented user commands in this repository's own grammar, and a denylist over
that grammar would refuse correct product language.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _lib_changelog_policy as policy  # noqa: E402


def _rules(text, **kw):
    return sorted({f.rule for f in policy.check_text(text, **kw).findings})


def format_note(report):
    return policy.format_report(report)


def test_clean_entry_produces_no_findings():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- `cctally explain` reports where a window's spend went.\n"
    assert policy.check_text(text).findings == ()


def test_issue_reference_is_refused():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- A thing landed (#630).\n"
    assert _rules(text) == ["issue-ref"]


def test_issue_reference_needs_a_word_boundary():
    # `#6a` is not an issue reference; a colour literal must not trip the rule.
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- The chip uses `#6ab0f3` now.\n"
    assert policy.check_text(text).findings == ()


def test_continuation_line_is_refused():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- One line\n  wrapped onto a second.\n"
    assert _rules(text) == ["multiline"]


def test_star_marker_is_refused():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n* A star bullet.\n"
    assert _rules(text) == ["multiline"]


def test_entry_over_the_cap_is_refused():
    long = "x" * 241
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- {long}\n"
    assert _rules(text) == ["too-long"]


def test_entry_at_the_cap_passes():
    exact = "x" * 240
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- {exact}\n"
    assert policy.check_text(text).findings == ()


def test_cap_counts_code_points_not_bytes():
    # 240 astral code points are 960 UTF-8 bytes; the rule must accept them.
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- " + ("\U0001F600" * 240) + "\n"
    assert policy.check_text(text).findings == ()


# --- the path axis: one injected decider, one injected fallback -------------
#
# Neither is imported. The kernel is published and pure, so `classify` (the
# mirror's own predicate) and `exists` (does this tree carry the path?) both
# arrive from the caller.


def _exists_none(_path):
    return False


def _exists_all(_path):
    return True


@pytest.mark.parametrize("path", ["docs/commands/explain.md", "docs/architecture.md"])
def test_a_path_the_tree_carries_is_not_a_finding(path):
    # `README.md` used to sit in this parametrization and asserted nothing: the
    # path regex requires a slash, so that token was never a candidate and the
    # case passed however the path axis behaved.
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- See `{path}` for detail.\n"
    assert policy.check_text(text, exists=_exists_all).findings == ()


def test_a_path_the_tree_does_not_carry_is_a_finding():
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    assert _rules(text, exists=_exists_none) == ["private-path"]


def test_the_existence_branch_never_claims_the_public_tree_lacks_the_path():
    """Each branch may assert only the fact it actually established.

    The retired family floor asserted "is not carried by the public tree" for
    everything it matched, and on the mirror that is the only message a run can
    produce, because the classifier cannot run there. At least one family was
    wrong about it, and no second layer existed to correct the message.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    detail = policy.check_text(text, exists=_exists_none).findings[0].detail
    assert "does not exist in this tree" in detail
    assert "public tree" not in detail


def test_the_classifier_branch_names_the_public_tree():
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/present-but-private.md` for detail.\n"
    )
    report = policy.check_text(
        text, classify=_classify_everything_private, exists=_exists_all
    )
    detail = report.findings[0].detail
    assert "is not carried by the public tree" in detail
    assert "does not exist in this tree" not in detail


def _classify_everything_public(paths):
    return {"public": list(paths), "private": [], "unmatched": []}


def test_a_path_that_classifies_public_but_is_absent_is_still_a_finding():
    """The two deciders answer different questions, and both must say yes.

    `classify` answers "would this path be published if it existed?".
    `exists` answers "does this tree actually carry it?". A reader can follow
    the citation only when both hold, so treating the classifier as a decider
    that ends the question passed a path that is published nowhere because it
    is nowhere at all. On the real file that hid seven dead references from the
    private tree that the public mirror's own run reports.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_public, exists=_exists_none
    ).findings
    assert [f.rule for f in findings] == ["private-path"]
    assert "does not exist in this tree" in findings[0].detail
    assert "public tree" not in findings[0].detail


def test_a_path_that_is_public_and_present_is_not_a_finding():
    # Non-vacuity for the case above: both conditions holding is still clean.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/present-thing.md` for detail.\n"
    )
    assert policy.check_text(
        text, classify=_classify_everything_public, exists=_exists_all
    ).findings == ()


def test_a_path_that_fails_both_conditions_says_both():
    """Neither branch may swallow the other's fact when both established one."""
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    detail = policy.check_text(
        text, classify=_classify_everything_private, exists=_exists_none
    ).findings[0].detail
    assert "is not carried by the public tree" in detail
    assert "does not exist in this tree" in detail


def test_one_finding_per_path_even_when_both_conditions_fail():
    # The two conditions are joined into one finding, not reported twice.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private, exists=_exists_none
    ).findings
    assert [f.rule for f in findings] == ["private-path"]


def test_a_classifier_with_no_existence_predicate_reports_what_it_could_not_see():
    """One decider supplied means the other's question went unasked.

    The run must say so, because the classifier alone cannot distinguish a
    published path from a path that would be published if anything were there.
    """
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Fine.\n"
    report = policy.check_text(text, classify=_classify_everything_public)
    assert report.classifier_available is True
    assert "existence" in report.classifier_note
    assert report.findings == ()


def test_both_deciders_supplied_withholds_nothing():
    # Non-vacuity for the case above: the complete check reports no degrade.
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Fine.\n"
    report = policy.check_text(
        text, classify=_classify_everything_public, exists=_exists_all
    )
    assert report.classifier_available is True
    assert report.classifier_note == ""


def test_with_neither_decider_the_path_rule_reports_its_own_absence():
    """A run that could not check paths says so instead of passing silently."""
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/absent-thing.md` for detail.\n"
    )
    report = policy.check_text(text)
    assert report.findings == ()
    assert report.classifier_available is False
    assert "cited-path rule did not run" in report.classifier_note


def test_the_existence_branch_reports_its_own_approximation():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Fine.\n"
    report = policy.check_text(text, exists=_exists_all)
    assert report.classifier_available is False
    assert report.classifier_note
    assert "classifier" in format_note(report)


# All SIX glob spellings the real file carries, not three. They resolve to no
# literal path, so the existence condition refuses every one of them. The
# classifier does NOT agree: `.mirror-allowlist` patterns match the literal `*`
# character, so the three `dashboard/`- and `tests/`-rooted spellings below
# classify `public` while the three `bin/_`-rooted ones classify `unmatched`.
# The two conditions therefore do not "agree" on a glob, which is what the
# kernel docstring claimed before this round; existence is what catches them.
_GLOB_SPELLINGS_CLASSIFYING_UNMATCHED = [
    "bin/_lib_*.py", "bin/_cctally_*.py", "bin/_*.py",
]
_GLOB_SPELLINGS_CLASSIFYING_PUBLIC = [
    "dashboard/static/assets/index-*.js",
    "dashboard/web/src/modals/*.tsx",
    "tests/fixtures/dashboard/*/stats.db",
]
_GLOB_SPELLINGS = (
    _GLOB_SPELLINGS_CLASSIFYING_UNMATCHED + _GLOB_SPELLINGS_CLASSIFYING_PUBLIC
)


@pytest.mark.parametrize("token", _GLOB_SPELLINGS)
def test_a_glob_spelling_is_a_finding_under_the_existence_condition(token):
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- See `{token}` for detail.\n"
    )
    assert _rules(text, exists=_exists_none) == ["private-path"]


@pytest.mark.parametrize("token", _GLOB_SPELLINGS_CLASSIFYING_PUBLIC)
def test_a_glob_the_classifier_calls_public_is_still_refused(token):
    """The three spellings a classifier-only run passed, and the mirror caught.

    An allowlist pattern matches the literal `*` in these tokens, so the
    classifier answers `public` for a spelling that names no file. Under the
    retired ranking, a private run with the classifier available reported
    nothing while the mirror's existence-only run reported all three.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- See `{token}` for detail.\n"
    )
    assert _rules(
        text, classify=_classify_everything_public, exists=_exists_none
    ) == ["private-path"]


@pytest.mark.parametrize("token", _GLOB_SPELLINGS_CLASSIFYING_UNMATCHED)
def test_a_glob_the_classifier_cannot_place_is_refused_by_the_classifier(token):
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- See `{token}` for detail.\n"
    )

    def classify_unmatched(paths):
        return {"public": [], "private": [], "unmatched": list(paths)}

    assert _rules(text, classify=classify_unmatched) == ["private-path"]


def test_the_real_allowlist_classifies_three_of_the_six_globs_public():
    """The claim the kernel docstring now makes, checked against real data.

    Skipped on the public mirror, which carries neither the allowlist nor the
    matcher. Asserting the split rather than skipping outright is what stops
    the docstring's "three of six" from being an unverified assertion on the
    one tree where it can be verified.
    """
    # The `.is_file()` gate is written on the path EXPRESSION rather than on a
    # name bound to it, matching `test_real_repo_classifier_layer_matches_this_tree`
    # above: `tests/test_public_test_dep_closure.py` resolves a gate against the
    # receiver it is called on, so a `matcher = ROOT / ...` intermediate reads as
    # an ungated reference to a private path from a published test.
    if not ((ROOT / ".githooks/_match.py").is_file()
            and (ROOT / ".mirror-allowlist").is_file()):
        pytest.skip("the mirror allowlist and matcher are unpublished")
    import importlib.util

    matcher = ROOT / ".githooks/_match.py"
    allowlist = ROOT / ".mirror-allowlist"
    spec = importlib.util.spec_from_file_location("_changelog_match_probe", matcher)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    verdict = module.classify(
        _GLOB_SPELLINGS, allowlist_text=allowlist.read_text(encoding="utf-8")
    )
    assert sorted(verdict["public"]) == sorted(_GLOB_SPELLINGS_CLASSIFYING_PUBLIC)
    assert sorted(verdict["unmatched"]) == sorted(
        _GLOB_SPELLINGS_CLASSIFYING_UNMATCHED
    )


# Ordinary prose that the first draft's path regex read as repository paths.
# `-m/--mode` is the exact form the policy's own authoring rule endorses, and
# linting three fully compliant entries reported it as a dead reference. On the
# real file this inflated private-path findings from 163 to 656.
_PROSE_WITH_SLASHES = [
    "-m/--mode", "Etc/UTC", "input/output", "and/or", "I/O", "A/B", "y/N",
    "npm/brew/public", "Enter/Space", "Weekly/Monthly", "4/4/4",
]


@pytest.mark.parametrize("token", _PROSE_WITH_SLASHES)
def test_prose_containing_a_slash_is_not_a_path_candidate(token):
    # The classifier is injected and answers `unmatched` for anything it is
    # asked about, so a token that is still treated as a path candidate is
    # reported. Without it this case would pass for the wrong reason: no floor
    # family matches these tokens either way.
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- The {token} selector is documented.\n"

    def classify(paths):
        return {"public": [], "private": [], "unmatched": list(paths)}

    assert policy.check_text(text, classify=classify).findings == ()


def test_prose_is_not_a_path_candidate_even_under_the_classifier():
    """The gate runs BEFORE classification, so prose never reaches `classify`.

    Otherwise every prose token would come back `unmatched` and be reported as
    a path the public tree does not carry.
    """
    line = "- The " + ", ".join(f"`{t}`" for t in _PROSE_WITH_SLASHES[:5]) + " forms are prose."
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n{line}\n"
    asked = []

    def classify(paths):
        asked.extend(paths)
        return {"public": [], "private": [], "unmatched": list(paths)}

    assert policy.check_text(text, classify=classify).findings == ()
    assert asked == []


# The other half of the candidate gate: a backticked repository path must still
# reach the path axis. These are inert fixture strings in a `- See \`…\``
# sentence; nothing here opens, imports or executes any of them.
_REAL_PRIVATE_PATHS = [
    # mirror-private-ok: the marker must sit INSIDE this assignment's own line
    # span, which is what scopes the waiver to it and nothing else (see
    # `tests/test_public_test_dep_closure.py::_waived_lines`).
    "bin/cctally-test-remote", "docs/remote-testing.md",
    ".claude/skills/release-cctally/SKILL.md",
]


@pytest.mark.parametrize("path", _REAL_PRIVATE_PATHS)
def test_a_backticked_repository_path_is_still_examined(path):
    """The Group A candidate rules must not swallow a genuine citation.

    None of these is home-relative and all three are backticked, so each one
    still produces exactly one finding.
    """
    text = f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- See `{path}` for detail.\n"
    findings = policy.check_text(text, classify=_classify_everything_private).findings
    assert [f.rule for f in findings] == ["private-path"]
    assert path in findings[0].detail


def test_the_classifier_catches_a_path_that_exists_here():
    """The one thing the existence fallback structurally cannot see.

    A private file is present in the private tree, so `exists` answers True and
    reports nothing. That false negative is acceptable because it can only
    happen where the classifier is absent, and `classifier_note` says so.
    """
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- See `docs/newly-private.md`.\n"
    assert policy.check_text(text, exists=_exists_all).findings == ()

    def classify(paths):
        return {"public": [], "private": list(paths), "unmatched": []}

    assert _rules(text, classify=classify, exists=_exists_all) == ["private-path"]


def test_report_states_when_the_classifier_did_not_run():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Fine.\n"
    report = policy.check_text(text)
    assert report.classifier_available is False
    assert report.classifier_note
    assert "classifier" in format_note(report)


def test_link_definition_in_the_preamble_is_allowed():
    text = "# Changelog\n\n[kac]: https://keepachangelog.com\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- Fine.\n"
    assert policy.check_text(text).findings == ()


def test_link_definition_after_the_first_heading_is_refused():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n[kac]: https://keepachangelog.com\n\n### Added\n- Fine.\n"
    assert _rules(text) == ["link-def"]


def test_findings_carry_one_based_line_numbers():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- A thing (#630).\n"
    assert [f.line for f in policy.check_text(text).findings] == [6]


# --- bin/cctally-changelog-lint, the thin executable over the kernel ---------

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINT = ROOT / "bin" / "cctally-changelog-lint"

CLEAN = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- `cctally explain` reports where spend went.\n"
DIRTY = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- A thing (#630).\n"


def _run(tmp_path, body, *extra):
    # `--repo-root` is pinned at the scratch directory, not defaulted. Without
    # it the executable resolves the root from its own location and picks up
    # THIS repository's `.mirror-allowlist`, so a case meaning to exercise the
    # floor-only path would silently run the classifier layer as well.
    f = tmp_path / "CHANGELOG.md"
    f.write_text(body, encoding="utf-8")
    return subprocess.run([sys.executable, str(LINT), "--repo-root", str(tmp_path),
                           "--file", str(f), *extra],
                          capture_output=True, text=True)


def _lint_json(root, *extra):
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(root),
         "--file", str(root / "CHANGELOG.md"), "--json", *extra],
        capture_output=True, text=True,
    )
    return json.loads(r.stdout)


def _flagged_paths(payload):
    return sorted(
        re.match(r"`([^`]+)`", f["detail"]).group(1)
        for f in payload["findings"] if f["rule"] == "private-path"
    )


def test_clean_file_exits_zero(tmp_path):
    assert _run(tmp_path, CLEAN).returncode == 0


def test_findings_exit_one_and_name_the_rule(tmp_path):
    r = _run(tmp_path, DIRTY)
    assert r.returncode == 1
    assert "issue-ref" in r.stdout


def test_bad_invocation_exits_two():
    # CPython itself exits 2 on a missing script, so without this the case
    # passes unchanged if the executable is deleted.
    assert LINT.is_file()
    r = subprocess.run([sys.executable, str(LINT), "--nonesuch"], capture_output=True, text=True)
    assert r.returncode == 2


def test_missing_file_exits_three(tmp_path):
    r = subprocess.run([sys.executable, str(LINT), "--file", str(tmp_path / "absent.md")],
                       capture_output=True, text=True)
    assert r.returncode == 3


def test_json_mode_is_machine_readable(tmp_path):
    r = _run(tmp_path, DIRTY, "--json")
    payload = json.loads(r.stdout)
    assert payload["schemaVersion"] == 1
    assert payload["findings"][0]["rule"] == "issue-ref"
    assert payload["classifierAvailable"] is False


def _module_docstrings(module):
    """Every docstring reachable from ``module``, as a set of exact strings.

    A docstring is exempt from the enumeration guard below, because it is prose
    a reader is meant to see rather than data the module decides with. This
    module's own prose deliberately names a handful of collaborator files, and
    flagging those would make the guard fire on every honest revision. The
    exemption is by exact string identity, so a floor cannot be smuggled in by
    sitting next to a docstring.
    """
    docs = {module.__doc__}
    for value in vars(module).values():
        if isinstance(value, types.ModuleType):
            continue
        doc = getattr(value, "__doc__", None)
        if isinstance(doc, str):
            docs.add(doc)
        if isinstance(value, type):
            for member in vars(value).values():
                member_doc = getattr(member, "__doc__", None)
                if isinstance(member_doc, str):
                    docs.add(member_doc)
    return docs


def _paths_in_text(text):
    """Path-shaped tokens in ``text``, by the kernel's OWN definition of one.

    `finditer` rather than a whitespace split, because the most likely
    regression is a single alternation regex — `^(?:bin/x|docs/y)` is one
    whitespace token and three path spellings.
    """
    tokens = [
        match.group(0)
        for match in policy._PATH_RE.finditer(text)
        if policy._looks_like_repo_path(match.group(0))
    ]
    tokens.extend(
        match.group(0)
        for match in _BARE_REPO_FILENAME_RE.finditer(text)
        if not _slash_adjacent(text, match)
    )
    return tokens


# A floor does not need a directory separator to be a floor. Writing
# `("cctally-release", "cctally-mirror-public")` discloses exactly what
# `("bin/cctally-release", ...)` discloses, and `policy._PATH_RE` requires a
# slash, so the slash-bearing walk above cannot see it. Of the shapes tried
# against this guard that one is the only one a person could reach by accident
# — every other miss needs the enumeration built at run time or hidden in an
# exotic container — so it is the one closed here.
#
# The rule is a NAMING CONVENTION, not a list: a token carrying a source
# extension, or one opening with a prefix this repository's own tooling uses.
# Both are published facts about how files are named, so the guard discloses
# nothing by holding them, which is the property the guard exists to protect.
_BARE_REPO_FILENAME_RE = re.compile(
    r"\b(?:"
    r"(?:cctally-|build-|_cctally_|_lib_|_lib-)[A-Za-z0-9_.-]+"
    r"|[A-Za-z0-9_-]+\.(?:py|sh|js|ts|tsx|json|toml|rb|md|tsv|sqlite)"
    r")\b"
)


def _slash_adjacent(text, match):
    """Is this bare name really the tail of a slash-bearing path?

    `bin/cctally-release` is already reported once by the slash-bearing walk,
    and reporting `cctally-release` again from inside it would double-count
    every hit and make the two rules impossible to tell apart in a failure.
    """
    before = text[match.start() - 1: match.start()]
    after = text[match.end(): match.end() + 1]
    return before == "/" or after == "/"


def _enumerated_paths(module):
    """Every path-shaped token reachable from ``module``'s own data.

    Reaches, in addition to the plain module-level containers the first version
    of this guard walked: dict keys and values, arbitrarily nested containers,
    the `.pattern` text of a compiled regex, bare module-level strings, class
    attributes, function default and keyword-default arguments, and the constants
    of a function's code object including nested code objects. The last of those
    is what reaches a list written inside a function body, which no inspection of
    the module namespace alone can see.

    Two documented exemptions. `_PATH_RE` itself is skipped by identity, because
    describing path syntax is its whole job; as written its pattern text happens
    to contain no path-shaped match, but the guard must not depend on that
    staying true across an edit to the regex. Docstrings are skipped, per
    `_module_docstrings`. Imported modules are skipped, because `import re` is
    not an enumeration of anything.
    """
    exempt_docstrings = _module_docstrings(module)
    found = []

    def scan(label, value, depth):
        if depth > 12:
            return
        if isinstance(value, str):
            if value in exempt_docstrings:
                return
            found.extend((label, token) for token in _paths_in_text(value))
        elif isinstance(value, re.Pattern):
            if value is policy._PATH_RE:
                return
            scan(f"{label}.pattern", value.pattern, depth + 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                scan(label, key, depth + 1)
                scan(label, item, depth + 1)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                scan(label, item, depth + 1)
        elif isinstance(value, types.CodeType):
            for const in value.co_consts:
                scan(label, const, depth + 1)
        elif isinstance(value, types.FunctionType):
            scan(label, value.__defaults__ or (), depth + 1)
            scan(label, tuple((value.__kwdefaults__ or {}).values()), depth + 1)
            scan(label, value.__code__, depth + 1)
        elif isinstance(value, type):
            for attr, item in vars(value).items():
                if not attr.startswith("__"):
                    scan(f"{label}.{attr}", item, depth + 1)

    for name, value in vars(module).items():
        if name.startswith("__") or isinstance(value, types.ModuleType):
            continue
        scan(name, value, 0)
    return found


def test_the_kernel_holds_no_enumeration_of_paths():
    """The kernel is PUBLISHED, so an enumeration inside it is a disclosure.

    Its predecessor was a floor of forty compiled families naming
    maintainer-only files. A module written to keep internal information out of
    the public repository was shipping forty internal filenames into it. The
    replacement injects both halves of the path axis, which needs no enumeration.

    This is the standing guard against that happening again, so it looks for the
    disclosure rather than for the one shape the deleted floor had. The earlier
    version tested `isinstance(value, (tuple, list, frozenset, set))` at module
    scope only, and missed a single alternation regex, a dict, a newline-joined
    string, a nested tuple, a default argument and a list inside a function body
    — six spellings of the same disclosure, the first of which is the natural way
    somebody reintroduces a floor. `_enumerated_paths` reaches all six, and
    `test_the_enumeration_guard_catches_every_shape_of_the_floor` proves it does.

    `_REPO_TOP_LEVEL` holds top-level directory names and `_SOURCE_EXTENSIONS`
    holds suffixes, so no entry in either contains a slash and neither identifies
    a file. Nine of the fifteen top-level names are absent from the public tree,
    which is not what makes them safe to publish — a directory name identifies no
    file, and that is.

    **What this guard cannot see, stated so nobody trusts it further than it
    deserves.** It reads the module's own data after import. It therefore cannot
    see an enumeration that does not exist as data at that moment, and a static
    guard never will: a determined author defeats it, and the point of writing
    the boundary down is that the next reader knows where it is rather than
    inferring completeness from a green run.

    Three classes are outside it. First, RUNTIME ASSEMBLY — a name built from
    fragments, an f-string evaluated inside a function, `os.path.join` at call
    time, a `dataclasses.field(default_factory=...)`, or anything else that
    produces the enumeration only when the code runs. Second, DYNAMIC
    CONTAINERS — a module's `__annotations__`, a class-level mapping under a
    dunder name, a value assigned in `__init__`, a bytes literal, or an
    attribute of a module-level object rather than of the module. Third,
    METHOD-HELD or DESCRIPTOR-HELD state — a `property` that returns the path, a
    `staticmethod` or `classmethod` holding it, or a `functools.partial` default.

    Twenty-five spellings were tried against it and it caught seventeen. The one
    miss a person could reach by accident, a bare filename with no directory
    separator, is closed by `_BARE_REPO_FILENAME_RE` above and exercised below.
    The rest each require deliberately constructing the enumeration somewhere
    the module's data does not reach, and chasing them would grow this guard
    without closing the class.

    Exactly one token is exempt, by exact match: `CHANGELOG.md`, which the
    kernel's own report label names because it is the file the kernel parses. A
    module about `CHANGELOG.md` naming `CHANGELOG.md` enumerates nothing, and
    matching it exactly rather than by pattern means no second filename can ride
    in beside it.
    """
    assert not hasattr(policy, "PRIVATE_PATH_FAMILIES")
    disclosed = [
        (name, token)
        for name, token in _enumerated_paths(policy)
        if token != _KERNEL_SELF_REFERENCE
    ]
    assert disclosed == []


# Six ways to write the retired floor, every one of which the first version of
# the guard above passed.
#
# The tokens are INVENTED repository-shaped names rather than real
# maintainer-only paths, and that costs the cases nothing: `_enumerated_paths`
# decides by `_looks_like_repo_path`, which is privacy-blind, so it flags an
# enumeration of repository paths whatever those paths are — the correct
# conservative rule for a module that gets published. Naming real private files
# here would have made this file, which is itself published, commit the very
# disclosure it is guarding against.
# The one path-shaped token the kernel legitimately holds: the name of the file
# it parses, published, and carried as the default label of its own report.
_KERNEL_SELF_REFERENCE = "CHANGELOG.md"

_DISCLOSED = "bin/cctally-example-tool"
_FLOOR_DEFEAT_SHAPES = {
    "a module-level alternation regex": (
        '_PRIVATE_RE = re.compile('
        'r"^(?:bin/cctally-example-tool|bin/cctally-example-|docs/example-runbook\\.md)")'
    ),
    "a dict of paths": (
        '_PRIVATE = {"tool": "bin/cctally-example-tool",'
        ' "runbook": "docs/example-runbook.md"}'
    ),
    "a newline-joined string constant": (
        '_PRIVATE = "bin/cctally-example-tool\\ndocs/example-runbook.md"'
    ),
    "a nested tuple of tuples": (
        '_PRIVATE = (("tool", ("bin/cctally-example-tool",)),'
        ' ("runbook", ("docs/example-runbook.md",)))'
    ),
    "a default argument": (
        "def is_private(path, families=("
        '"bin/cctally-example-tool", "docs/example-runbook.md")):\n'
        "    return path in families"
    ),
    "a list inside a function body": (
        "def is_private(path):\n"
        '    families = ["bin/cctally-example-tool", "docs/example-runbook.md"]\n'
        "    return path in families"
    ),
    # No directory separator. `policy._PATH_RE` requires a slash, so every
    # earlier version of this guard walked straight past this one, and it is
    # the only missed shape a person could write without meaning to.
    "a bare filename with no directory separator": (
        '_PRIVATE = ("cctally-example-tool", "example-runbook.md")'
    ),
}


@pytest.mark.parametrize("shape", sorted(_FLOOR_DEFEAT_SHAPES))
def test_the_enumeration_guard_catches_every_shape_of_the_floor(shape):
    """A guard nobody has tried to defeat is not a guard.

    Each source fragment is a working reintroduction of the floor in a different
    spelling. The guard must name the disclosed path in every one of them.
    """
    module = types.ModuleType(f"_floor_probe_{abs(hash(shape))}")
    exec("import re\n" + _FLOOR_DEFEAT_SHAPES[shape], vars(module))  # noqa: S102
    disclosed = [token for _name, token in _enumerated_paths(module)]
    # The bare-filename shape carries no directory, so it discloses the tail
    # rather than the whole path. Every other shape discloses `_DISCLOSED`.
    expected = (
        "cctally-example-tool" if "bare filename" in shape else _DISCLOSED
    )
    assert expected in disclosed, f"the guard does not see {shape}"


def test_the_enumeration_guard_passes_a_module_that_enumerates_nothing():
    # Non-vacuity for the six cases above: the guard is not simply reporting
    # every string it reaches. Prose with a slash, a suffix tuple and a
    # top-level-name set are all clean.
    module = types.ModuleType("_floor_probe_clean")
    exec(  # noqa: S102
        'TOP = frozenset({"bin", "docs"})\n'
        'SUFFIXES = (".py", ".md")\n'
        'MESSAGE = "the -m/--mode selector and the Etc/UTC zone are prose"\n'
        "def check(path, cap=240):\n"
        '    return len(path) <= cap and path != "y/N"\n',
        vars(module),
    )
    assert _enumerated_paths(module) == []


def test_the_lint_flags_a_path_this_tree_does_not_carry(tmp_path):
    """End to end, with no classifier: absence is the finding.

    The scratch tree carries neither `.mirror-allowlist` nor the matcher, so
    this is exactly the shape of a public-mirror run.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "present.md").write_text("x", encoding="utf-8")
    body = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/present.md` and `docs/absent.md`.\n"
    )
    r = _run(tmp_path, body, "--json")
    payload = json.loads(r.stdout)
    assert r.returncode == 1
    assert payload["classifierAvailable"] is False
    paths = [f_["detail"] for f_ in payload["findings"] if f_["rule"] == "private-path"]
    assert len(paths) == 1
    assert "`docs/absent.md` does not exist in this tree" in paths[0]


def test_the_lint_does_not_resolve_a_citation_outside_the_tree():
    """A token that climbs out of the tree is never resolved against the host.

    **The cited file is created on purpose.** Without it this case asserted
    nothing about the property it names: the review deleted the guard from a
    copy of the predicate and the case still passed identically, because the
    unguarded lookup found nothing there either. With `../outside/thing.md`
    actually present, an unguarded implementation reports zero findings and the
    shipped one reports a finding, so the case can now tell them apart.

    This is the only assertion covering the security-adjacent property in the
    change, and it is deliberately whole-behaviour rather than guard-specific:
    the shipped predicate refuses such a token twice over — the `..` check
    rejects the candidate, and the filesystem fallback's `resolve()` plus
    `is_relative_to(root)` would refuse it again — so the assertion is that the
    citation is not resolved, not that one named line of code did it.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        outside = pathlib.Path(tmp) / "outside"
        outside.mkdir()
        (outside / "thing.md").write_text("x", encoding="utf-8")
        root = pathlib.Path(tmp) / "repo"
        root.mkdir()
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
            "- See `../outside/thing.md` for detail.\n",
            encoding="utf-8",
        )
        # The fixture is the point of the case, so it is asserted rather than
        # assumed: a typo in the layout would silently restore the old vacuity.
        assert (root / ".." / "outside" / "thing.md").exists()
        assert _flagged_paths(_lint_json(root)) == ["../outside/thing.md"]


def test_real_repo_classifier_layer_matches_this_tree():
    """The classifier engages here, and reports its own absence elsewhere.

    THIS FILE IS PUBLISHED, and neither `.mirror-allowlist` nor
    `.githooks/_match.py` is, so on the public clone the classifier cannot run
    at all. An unconditional `classifierAvailable is True` therefore fails every
    public CI run. Both branches assert something real rather than skipping: a
    bare skip would make the case vacuous on the private tree, which is the one
    tree where the classifier layer actually does anything.

    The exit code is deliberately not asserted. The lint reports findings until
    `CHANGELOG.md` itself is rewritten.
    """
    classifier_data_present = (
        (ROOT / ".githooks/_match.py").is_file()
        and (ROOT / ".mirror-allowlist").is_file()
    )
    r = subprocess.run([sys.executable, str(LINT), "--repo-root", str(ROOT), "--json"],
                       capture_output=True, text=True)
    payload = json.loads(r.stdout)
    if classifier_data_present:
        assert payload["classifierAvailable"] is True
        assert payload["classifierNote"] == ""
    else:
        assert payload["classifierAvailable"] is False
        assert payload["classifierNote"]


# --- bin/cctally-changelog-audit, the metric recomputation ------------------

AUDIT = ROOT / "bin" / "cctally-changelog-audit"


def test_audit_reports_populations_separately(tmp_path):
    # Every figure quoted about this file states the population it was counted
    # over, because the first draft of the design conflated the `[Unreleased]`
    # heading with a released one and counted issue references over first lines
    # only. This command is what keeps that class of error out of the numbers.
    body = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- One (#1).\n\n"
        "## [1.1.0] - 2026-01-02\n\n### Added\n- Two\n  wrapped (#2).\n\n"
        "## [1.0.0] - 2026-01-01\n\n### Added\n- Three.\n"
    )
    f = tmp_path / "CHANGELOG.md"
    f.write_text(body, encoding="utf-8")
    r = subprocess.run([sys.executable, str(AUDIT), "--file", str(f), "--json"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    p = json.loads(r.stdout)
    assert p["schemaVersion"] == 1
    assert p["bytes"] == len(body.encode("utf-8"))
    assert p["releasedSections"] == 2
    assert p["entries"]["unreleased"] == 1
    assert p["entries"]["released"] == 2
    assert p["entries"]["total"] == 3
    # (#1) is on a first line; (#2) only on a continuation line.
    assert p["issueRefs"]["firstLineOnly"] == 1
    assert p["issueRefs"]["byEntry"] == 2
    assert p["singlePhysicalLineEntries"] == 2


def test_audit_exits_two_on_bad_invocation():
    # Same non-vacuity guard as the lint case above.
    assert AUDIT.is_file()
    r = subprocess.run([sys.executable, str(AUDIT), "--nonesuch"],
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_audit_exits_three_when_the_file_cannot_be_read(tmp_path):
    r = subprocess.run([sys.executable, str(AUDIT), "--file", str(tmp_path / "absent.md")],
                       capture_output=True, text=True)
    assert r.returncode == 3


def test_a_continuation_after_a_subsection_heading_starts_no_entry():
    """A heading closes the previous entry, so nothing continues across it.

    An indented line that follows `### Fixed` before that subsection's first
    bullet was appended to the PREVIOUS subsection's last entry, so the
    `multiline` finding named an entry on another line that is not the offender.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n"
        "### Added\n- A clean entry.\n\n"
        "### Fixed\n  an orphaned continuation line\n- Another clean entry.\n"
    )
    entries = policy.parse_entries(text)
    assert [e.content for e in entries] == ["A clean entry.", "Another clean entry."]
    assert all(e.continuations == () for e in entries)
    assert policy.check_text(text).findings == ()


def test_a_continuation_after_a_release_heading_starts_no_entry():
    text = (
        "# Changelog\n\n## [1.1.0] - 2026-02-02\n\n"
        "### Added\n- A clean entry.\n\n"
        "## [1.0.0] - 2026-01-01\n  an orphaned continuation line\n\n"
        "### Added\n- Another clean entry.\n"
    )
    assert policy.check_text(text).findings == ()


def test_a_genuine_continuation_is_still_attached_to_its_own_entry():
    # Non-vacuity for the two cases above: the rule still fires where it should,
    # and it names the bullet's own line.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n"
        "### Added\n- A wrapped entry\n  onto a second line.\n"
    )
    findings = policy.check_text(text).findings
    assert [(f.line, f.rule) for f in findings] == [(6, "multiline")]


def test_the_report_names_the_file_that_was_read():
    text = "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n- A thing (#630).\n"
    report = policy.check_text(text)
    assert policy.format_report(report, "probe.md").startswith("probe.md:6: ")
    assert policy.format_report(report).startswith("CHANGELOG.md:6: ")


def test_the_lint_names_the_file_it_was_pointed_at(tmp_path):
    f = tmp_path / "probe.md"
    f.write_text(DIRTY, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(tmp_path), "--file", str(f)],
        capture_output=True, text=True,
    )
    assert r.returncode == 1
    assert r.stdout.startswith("probe.md:6: ")


def test_the_repo_root_defaults_to_the_repository_holding_the_linted_file(tmp_path):
    """The allowlist that governs a file is the one in ITS repository.

    Without `--repo-root`, the executable resolved the classifier from its own
    checkout, so a file linted elsewhere was classified against a tree it has
    nothing to do with.
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "_match.py").write_text(
        "def classify(paths, allowlist_path='.mirror-allowlist', *, allowlist_text=None):\n"
        "    return {'public': [], 'private': list(paths), 'unmatched': []}\n",
        encoding="utf-8",
    )
    (tmp_path / ".mirror-allowlist").write_text("", encoding="utf-8")
    f = tmp_path / "CHANGELOG.md"
    f.write_text(
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/commands/explain.md` for detail.\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(LINT), "--file", str(f), "--json"],
        capture_output=True, text=True,
    )
    payload = json.loads(r.stdout)
    # THIS repository's allowlist calls that path public. The scratch repo's
    # classifier calls everything private, and it is the one that must run.
    assert payload["classifierAvailable"] is True
    assert [f_["rule"] for f_ in payload["findings"]] == ["private-path"]


def test_a_classifier_that_cannot_be_loaded_exits_three(tmp_path):
    """An inability to complete is exit 3, never exit 1.

    Exit 1 means "found policy violations". An unguarded `exec_module` produced
    a CPython traceback and exit 1, which is indistinguishable from it. This is
    distinct from the classifier being ABSENT, which is the reported-degrade
    path and stays 0/1.
    """
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "_match.py").write_text(
        "raise RuntimeError('the classifier is broken')\n", encoding="utf-8"
    )
    (tmp_path / ".mirror-allowlist").write_text("", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(CLEAN, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 3
    assert "cctally-changelog-lint:" in r.stderr
    assert "Traceback" not in r.stderr


def test_a_classifier_that_raises_when_called_exits_three(tmp_path):
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "_match.py").write_text(
        "def classify(*a, **kw):\n    raise RuntimeError('classify blew up')\n",
        encoding="utf-8",
    )
    (tmp_path / ".mirror-allowlist").write_text("", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/commands/explain.md`.\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 3
    assert "classify blew up" in r.stderr
    assert "Traceback" not in r.stderr


def test_an_absent_classifier_is_not_an_error(tmp_path):
    # Non-vacuity for the two cases above: absence stays 0/1 with a reported
    # degrade, so exit 3 really does mean "could not complete".
    (tmp_path / "CHANGELOG.md").write_text(CLEAN, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(tmp_path), "--json"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert json.loads(r.stdout)["classifierAvailable"] is False


# --- Group B: the existence condition reads the INDEX, not the filesystem ----
#
# `git ls-files` never follows a symlink, is identical on a bare checkout and on
# a developer's clone that has run `npm install`, and on the public mirror
# returns exactly the public file set — which is the right answer to "does this
# tree carry it". The filesystem check it replaced answered all three wrongly.


def _seed_repo(root, tracked, body):
    """A scratch repository whose INDEX holds ``tracked``. No commit is needed.

    `git ls-files` reads the index, so `git add` alone is the whole setup.
    """
    for rel in tracked:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                   capture_output=True)


def test_a_path_present_on_disk_but_untracked_is_absent_from_the_index():
    """Determinism: the same tree must lint the same way on two machines.

    `dashboard/web/node_modules` is absent on a bare checkout and present in a
    clone that has run `npm install`, so under the filesystem check one machine
    reported a finding and the other did not. The index is identical on both.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        body = (
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
            "- See `docs/tracked.md` and `docs/untracked.md`.\n"
        )
        _seed_repo(root, ["docs/tracked.md"], body)
        # Written AFTER `git add`, so it is on disk and not in the index.
        (root / "docs" / "untracked.md").write_text("x", encoding="utf-8")
        payload = _lint_json(root)
        assert payload["existenceSource"] == "index"
        assert _flagged_paths(payload) == ["docs/untracked.md"]


def test_a_directory_implied_by_a_tracked_file_counts_as_carried():
    # The index holds files; a citation naming a directory those files sit
    # under is still followable, so the implied directories are part of the set.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        body = (
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
            "- See `docs/commands` for detail.\n"
        )
        _seed_repo(root, ["docs/commands/explain.md"], body)
        assert _flagged_paths(_lint_json(root)) == []


def test_the_index_does_not_follow_a_symlink_out_of_the_tree():
    """A citation must never resolve through a link the index does not carry.

    A probe built `bin/escape -> <a directory outside the tree>` and `bin/etc ->
    /etc`; `Path.exists()` follows both, so `bin/escape/secret.md` and
    `bin/etc/passwd` were reported present. `git ls-files` records the symlink
    itself and nothing beneath it, so neither path is in the set.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        outside = pathlib.Path(tmp) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("x", encoding="utf-8")
        root = pathlib.Path(tmp) / "repo"
        (root / "bin").mkdir(parents=True)
        body = (
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
            "- See `bin/escape/secret.md` and `bin/etc/passwd`.\n"
        )
        (root / "bin" / "escape").symlink_to(outside, target_is_directory=True)
        (root / "bin" / "etc").symlink_to("/etc", target_is_directory=True)
        _seed_repo(root, ["bin/real.py"], body)
        payload = _lint_json(root)
        assert payload["existenceSource"] == "index"
        assert _flagged_paths(payload) == ["bin/escape/secret.md", "bin/etc/passwd"]


def test_the_filesystem_fallback_still_refuses_a_symlink_escape():
    """Where there is no index, the guard must hold on its own.

    A tarball export carries no `.git`, so the fallback runs. It resolves the
    candidate and refuses anything that lands outside the root, which is what
    stops a symlink from reaching the host.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        outside = pathlib.Path(tmp) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("x", encoding="utf-8")
        root = pathlib.Path(tmp) / "export"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "escape").symlink_to(outside, target_is_directory=True)
        (root / "bin" / "etc").symlink_to("/etc", target_is_directory=True)
        (root / "bin" / "real.py").write_text("x", encoding="utf-8")
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
            "- See `bin/escape/secret.md`, `bin/etc/passwd` and `bin/real.py`.\n",
            encoding="utf-8",
        )
        payload = _lint_json(root)
        assert payload["existenceSource"] == "filesystem"
        # `bin/real.py` is a real file inside the root and stays clean, so the
        # fallback is refusing the escapes rather than refusing everything.
        assert _flagged_paths(payload) == ["bin/escape/secret.md", "bin/etc/passwd"]


def test_the_text_report_names_the_filesystem_fallback(tmp_path):
    """A run is never ambiguous about which source it consulted.

    `--json` always carries `existenceSource`. Text mode names the source only
    when it is the fallback, because the index is the contract and the
    filesystem is the degrade — the same convention `classifier_note` follows.
    """
    r = _run(tmp_path, CLEAN)
    assert r.returncode == 0
    assert "the repository index is unavailable" in r.stdout


def test_the_text_report_is_silent_when_the_index_was_read():
    # Non-vacuity for the case above: the note is a degrade report, so a run
    # that read the index prints nothing extra.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        _seed_repo(root, ["docs/tracked.md"], CLEAN)
        r = subprocess.run(
            [sys.executable, str(LINT), "--repo-root", str(root)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        # The classifier note still prints — the scratch repo carries neither
        # the allowlist nor the matcher — so the assertion is scoped to the
        # existence-source note rather than to an empty stream.
        assert "the repository index is unavailable" not in r.stdout


def test_the_real_repository_run_reads_its_index():
    # The tree this suite runs against is a checkout, so the contract path is
    # the one exercised; a fallback here would mean the index read broke.
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(ROOT), "--json"],
        capture_output=True, text=True,
    )
    assert json.loads(r.stdout)["existenceSource"] == "index"


def test_the_kernel_file_carries_no_shebang():
    # It is committed 100644 and every `_lib_*.py` sibling carries none.
    source = (ROOT / "bin" / "_lib_changelog_policy.py").read_text(encoding="utf-8")
    assert not source.startswith("#!")


# --- Group A: two purely lexical rules inside the path-candidate step --------
#
# Both are lexical only — no injection, no git, no filesystem — so they behave
# identically on the private tree and on the public mirror.

_HOME_RELATIVE_TOKENS = [
    "~/.local/share/cctally/config.json",
    "~/.claude/settings.json",
    "~/.codex/config.toml",
    "~/.local/share/cctally/update-state.json",
    "$HOME/.claude/settings.json",
    "${HOME}/.codex/config.toml",
]


def _classify_everything_private(paths):
    return {"public": [], "private": list(paths), "unmatched": []}


@pytest.mark.parametrize("token", _HOME_RELATIVE_TOKENS)
def test_a_home_relative_path_is_not_a_repository_citation(token):
    """`~/.claude/settings.json` names the reader's own machine, not this tree.

    `~` is outside the path regex's character class, so the match began at
    `.claude` and the token reaching classification was the truncated
    `.claude/settings.json` — which classifies `unmatched` and was reported as a
    dead public reference. Telling a user where their own configuration lives is
    exactly what a user-facing changelog should do.

    The classifier here calls everything private, so a token still treated as a
    candidate is reported. Without it the case would pass for the wrong reason.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- The path is `{token}` now.\n"
    )
    report = policy.check_text(text, classify=_classify_everything_private)
    assert report.findings == ()


def test_the_three_home_paths_named_in_the_review_produce_nothing():
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- Settings move from `~/.local/share/cctally/config.json` to "
        "`~/.claude/settings.json` and `~/.codex/config.toml`.\n"
    )
    report = policy.check_text(text, classify=_classify_everything_private)
    assert [f.rule for f in report.findings] == []


# The two non-backticked candidates the real file carries. Both are prose.
_UNBACKTICKED_PROSE = ["dashboard/CLI", "dashboard/conversation"]


@pytest.mark.parametrize("token", _UNBACKTICKED_PROSE)
def test_a_token_outside_a_backtick_span_is_not_a_citation(token):
    """The policy's own authoring rule already requires backticking a surface.

    So an unbackticked `word/Word` token is prose, and reporting it as a dead
    public reference reports a citation nobody made.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- The {token} split is described elsewhere.\n"
    )
    report = policy.check_text(text, classify=_classify_everything_private)
    assert report.findings == ()


# --- Group D: the other two deliberate citation forms -----------------------
#
# A backtick code span is not the only way an author cites a path. A Markdown
# link target and an emphasis span are citations too, and neither was examined:
# `[docs/absent.md](docs/absent.md)` and `**docs/absent.md**` both yielded an
# empty cited-path list and no finding even under a classifier that reported
# everything private. There are zero live instances today, and Tranche 2
# rewrites 1,162 entries — one of them written as `[the release runbook](docs/RELEASE.md)`
# would publish a private path with the gate silent.
#
# The fix widens the citation CONTEXTS. It deliberately does not flag every
# unbackticked slash-bearing token, because that resurrects the `dashboard/CLI`
# and `dashboard/conversation` false positives the backtick gate exists to
# suppress.

_CITATION_FORMS = {
    "a Markdown link target": "See [the runbook](docs/absent-thing.md) for detail.",
    "a link whose text is the path": (
        "See [docs/absent-thing.md](docs/absent-thing.md) for detail."
    ),
    "single-asterisk emphasis": "See *docs/absent-thing.md* for detail.",
    "double-asterisk emphasis": "See **docs/absent-thing.md** for detail.",
    "single-underscore emphasis": "See _docs/absent-thing.md_ for detail.",
    "double-underscore emphasis": "See __docs/absent-thing.md__ for detail.",
    "a backtick code span": "See `docs/absent-thing.md` for detail.",
}


@pytest.mark.parametrize("form", sorted(_CITATION_FORMS))
def test_every_deliberate_citation_form_is_examined(form):
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- {_CITATION_FORMS[form]}\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private
    ).findings
    assert [f.rule for f in findings] == ["private-path"], (
        f"{form} was not examined"
    )
    assert "docs/absent-thing.md" in findings[0].detail


@pytest.mark.parametrize("path", _REAL_PRIVATE_PATHS)
def test_a_link_target_naming_a_real_private_path_is_caught(path):
    """The spelling a rewritten entry would most plausibly use.

    An author linking the release runbook by its path publishes a private path,
    and before this round the gate said nothing. The tokens come from
    `_REAL_PRIVATE_PATHS`, which already carries the mirror waiver, so this case
    adds no private path literal of its own to a published file.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- The promotion step is described in [the runbook]({path}).\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private
    ).findings
    assert [f.rule for f in findings] == ["private-path"]
    assert path in findings[0].detail


@pytest.mark.parametrize(
    "token", _UNBACKTICKED_PROSE + ["docs/absent-thing.md"] + _REAL_PRIVATE_PATHS
)
def test_bare_prose_containing_a_slash_is_still_not_a_citation(token):
    """Widening the contexts must not widen to bare prose.

    Flagging every slash-bearing token would report `dashboard/CLI` and
    `dashboard/conversation` again, which is the false-positive class the
    backtick gate was introduced to suppress. A path in running prose, in none
    of the three citation forms, is still not examined.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- The {token} split is described elsewhere.\n"
    )
    assert policy.check_text(
        text, classify=_classify_everything_private
    ).findings == ()


def test_an_underscore_inside_a_word_does_not_open_an_emphasis_span():
    """Intraword `_` opens no emphasis, which is CommonMark's own rule.

    Without the flanking rule, the underscores in two ordinary identifiers pair
    with each other and every slash-bearing token between them becomes a
    citation. That would report a path nobody cited.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- Both cache_db and stats_db mention docs/absent-thing.md in prose.\n"
    )
    assert policy.check_text(
        text, classify=_classify_everything_private
    ).findings == ()


def test_an_asterisk_inside_a_code_span_opens_no_emphasis_span():
    """Code spans take precedence over emphasis, so a glob's `*` pairs with nothing.

    Two glob spellings on one line carry two asterisks. If emphasis were scanned
    over the raw line they would pair, and the prose between them — including a
    token in no citation form at all — would be read as cited.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `bin/_lib_*.py` for docs/uncited-thing.md and `bin/_*.py` too.\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private
    ).findings
    assert sorted(
        re.match(r"`([^`]+)`", f.detail).group(1) for f in findings
    ) == ["bin/_*.py", "bin/_lib_*.py"]


def test_a_home_relative_path_inside_emphasis_is_still_not_a_citation():
    # The home-relative rule is applied to every citation form, not only to
    # backticked ones.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- Settings move to **~/.claude/settings.json** now.\n"
    )
    assert policy.check_text(
        text, classify=_classify_everything_private
    ).findings == ()


def test_an_unpaired_emphasis_delimiter_opens_no_span():
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A stray * and then docs/absent-thing.md in prose.\n"
    )
    assert policy.check_text(
        text, classify=_classify_everything_private
    ).findings == ()


def test_a_double_backtick_span_still_encloses_its_token():
    # A run of N backticks pairs with the next run of exactly N, so a span that
    # itself contains a backtick is still a span.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `` docs/absent-thing.md `` for detail.\n"
    )
    report = policy.check_text(text, classify=_classify_everything_private)
    assert [f.rule for f in report.findings] == ["private-path"]


def test_an_unpaired_backtick_does_not_open_a_span():
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A stray ` and then docs/absent-thing.md in prose.\n"
    )
    report = policy.check_text(text, classify=_classify_everything_private)
    assert report.findings == ()


# --- Group D: two rules that were reading only part of an entry -------------


def test_an_issue_reference_on_a_continuation_line_is_refused():
    """Two shipped tools must not report different populations for one rule.

    The lint searched `entry.content` and never `entry.continuations`, so it
    reported 831 issue-ref findings on the real file while
    `bin/cctally-changelog-audit` reported 835 for the same rule. Four entries
    carry a reference only on a continuation line.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A wrapped entry\n  that names (#630) only down here.\n"
    )
    findings = policy.check_text(text).findings
    assert sorted(f.rule for f in findings) == ["issue-ref", "multiline"]
    # The finding names the bullet's own line, not the continuation's.
    assert {f.line for f in findings} == {6}


def test_an_entry_with_no_reference_anywhere_is_still_clean():
    # Non-vacuity for the case above: widening the scan must not make every
    # wrapped entry an issue-ref finding.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A wrapped entry\n  with nothing to report.\n"
    )
    assert _rules(text) == ["multiline"]


# --- Group E: one scope for every content rule ------------------------------
#
# `issue-ref` read the whole entry while `private-path` and `too-long` read only
# the first line, so three rules over one entry read three populations. The
# scope is now the whole entry for all three.


def test_a_path_cited_only_on_a_continuation_line_is_examined():
    """A citation below the fold is still a citation.

    Seven path citations in the real file sit only on continuation lines. None
    of them is currently non-public, so there is no live under-report — but the
    scope must not depend on that staying true.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A wrapped entry\n  that cites `docs/absent-thing.md` only down here.\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private
    ).findings
    assert sorted(f.rule for f in findings) == ["multiline", "private-path"]
    # Both findings name the bullet's own line, not the continuation's.
    assert {f.line for f in findings} == {6}


def test_the_length_cap_measures_the_whole_entry():
    """A wrapped entry is not under the cap because its first line is.

    The cap asks how long the entry is. Measuring `entry.content` alone let a
    3,000-character entry pass by wrapping it.
    """
    first = "x" * 200
    second = "y" * 200
    text = (
        f"# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        f"- {first}\n  {second}\n"
    )
    rules = _rules(text)
    assert "too-long" in rules
    detail = [f for f in policy.check_text(text).findings if f.rule == "too-long"][0]
    # 200 + one joining space + 200.
    assert "401 code points" in detail.detail


def test_a_wrapped_entry_under_the_cap_is_not_too_long():
    # Non-vacuity for the case above: widening the scope must not make every
    # wrapped entry a length finding.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A short first line\n  and a short second one.\n"
    )
    assert _rules(text) == ["multiline"]


def test_the_entry_scope_joins_with_a_space_not_an_empty_string():
    """Two tokens either side of the line break must not glue into one.

    The policy requires the entry on one physical line, so the scope models the
    unwrapped entry: a space where the line break was. Joining with nothing
    would fuse the tail of one line to the head of the next and invent a token
    the author never wrote.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- See `docs/first-thing.md`\n  and `docs/second-thing.md` as well.\n"
    )
    findings = policy.check_text(
        text, classify=_classify_everything_private
    ).findings
    assert sorted(
        re.match(r"`([^`]+)`", f.detail).group(1)
        for f in findings if f.rule == "private-path"
    ) == ["docs/first-thing.md", "docs/second-thing.md"]


def test_the_two_tools_agree_on_the_issue_reference_population(tmp_path):
    """One definition, so the agreement cannot rest on two copies.

    `bin/cctally-changelog-audit` defined its own `_ISSUE_RE`. The literals were
    identical, which is the only reason both tools reported 835 for the real
    file; the agreement rested on nobody editing one of them.
    """
    body = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- One (#12).\n"
        "- Two, whose colour literal `#6ab0f3` is not a reference.\n"
        "- Three\n  naming (#34) only on this line.\n"
        "- Four, clean.\n"
    )
    f = tmp_path / "CHANGELOG.md"
    f.write_text(body, encoding="utf-8")
    audited = json.loads(subprocess.run(
        [sys.executable, str(AUDIT), "--file", str(f), "--json"],
        capture_output=True, text=True).stdout)
    linted = policy.check_text(body).findings
    issue_findings = [x for x in linted if x.rule == "issue-ref"]
    assert audited["issueRefs"]["byEntry"] == 2
    assert len(issue_findings) == audited["issueRefs"]["byEntry"]


def _load_audit_module():
    """`bin/cctally-changelog-audit` as a module, so its kernel use is testable.

    It carries no `.py` suffix, so it needs an explicit source loader. Importing
    it runs only its module scope; `main()` is behind the usual guard.
    """
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("_changelog_audit_probe", str(AUDIT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_audit_routes_issue_detection_through_the_kernel(monkeypatch):
    """Proves ONE definition, which comparing two counts cannot.

    Both tools reported 835 for the real file while each held its own copy of
    the literal, so an equal-counts assertion passes either way. Replacing the
    kernel's predicate is what tells them apart: the audit's answer must move
    with it, and would not if it still compiled its own pattern.
    """
    audit_module = _load_audit_module()
    body = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- One (#12).\n- Two\n  naming (#34) down here.\n"
    )
    assert audit_module.audit(body, body.encode())["issueRefs"]["byEntry"] == 2
    monkeypatch.setattr(policy, "cites_an_issue", lambda _text: False)
    assert audit_module.audit(body, body.encode())["issueRefs"]["byEntry"] == 0
    assert audit_module.audit(body, body.encode())["issueRefs"]["firstLineOnly"] == 0


def test_a_file_outside_the_repo_root_is_not_named_absolutely(tmp_path):
    """The `--json` fix did not cover the case it was written for.

    A `--file` outside `--repo-root` fell through `except ValueError` and
    printed the absolute path — the exact case whose comment says it was fixed
    because it published the runner's checkout location in every public CI run.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CHANGELOG.md").write_text(CLEAN, encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    probe = outside / "probe.md"
    probe.write_text(DIRTY, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(root),
         "--file", str(probe), "--json"],
        capture_output=True, text=True,
    )
    payload = json.loads(r.stdout)
    assert payload["file"] == "probe.md"
    assert str(tmp_path) not in r.stdout

    text_run = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(root), "--file", str(probe)],
        capture_output=True, text=True,
    )
    assert text_run.stdout.startswith("probe.md:6: ")
    assert str(tmp_path) not in text_run.stdout


def test_a_fenced_block_suppresses_heading_and_bullet_detection():
    """The lint must agree with the parser that actually stamps the file.

    `bin/_lib_changelog_stamp.py` suppresses heading detection inside a code
    fence. Without the same rule here, a fenced sample beginning at column 0
    with `## `, `### ` or `- ` is a heading or a bullet to the lint. The real
    file carries no fence today, but Tranche 2 runs this lint repeatedly
    against a partially rewritten file where one could appear.
    """
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- An entry showing a sample.\n"
        "```markdown\n"
        "## [9.9.9] - 1999-01-01\n"
        "### Added\n"
        "- not an entry\n"
        "```\n"
    )
    entries = policy.parse_entries(text)
    assert [e.content for e in entries] == ["An entry showing a sample."]
    # The fenced lines belong to the bullet, exactly as the stamper attaches
    # them, so the one-physical-line rule still refuses the entry.
    assert len(entries[0].continuations) == 5
    assert _rules(text) == ["multiline"]


def test_a_tilde_fence_is_not_a_fence():
    # ``` only, matching the stamper. Recognizing `~~~` here would make the two
    # parsers disagree in the other direction.
    text = (
        "# Changelog\n\n## [1.0.0] - 2026-01-01\n\n### Added\n"
        "- A real entry.\n\n"
        "~~~\n- not fenced away\n~~~\n"
    )
    assert [e.content for e in policy.parse_entries(text)] == [
        "A real entry.", "not fenced away",
    ]


def test_json_mode_names_the_file_relative_to_the_repository(tmp_path):
    """An absolute path here prints the runner's checkout location in public CI.

    `format_report` was already changed to prefer the repo-relative form; the
    `--json` `file` key had not been.
    """
    r = _run(tmp_path, DIRTY, "--json")
    assert json.loads(r.stdout)["file"] == "CHANGELOG.md"
