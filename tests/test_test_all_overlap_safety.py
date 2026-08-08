"""Forward-looking drift guard for a potential pytest/shell-pool overlap (#296).

#296 investigated overlapping the post-pool `pytest` phase with the shell
harness pool in `bin/cctally-test-all`. The overlap was proven SAFE and
regression-free but NOT enabled — empirically it does not pay off, because the
shell pool is CPU-saturated (bulk pytest competing with it inflates the pool's
critical path by ~as much as it saves; best case was ~4% on a 16-core box, far
short of the target). See the "Empirical result & decision" section of
docs/superpowers/specs/2026-07-13-296-pytest-shell-overlap-design.md.

This test survives as cheap insurance: it encodes the audit that made the
overlap safe, so that IF the overlap is ever revisited, the deselect set it
would need is already known and drift is caught. It has no runtime dependency
on the runner — it is pure static analysis of the harnesses and tests.

The invariant: every pytest file that reads an in-place-rebuilt
tests/fixtures/<cmd> dir must be in KNOWN_SAFE_DESELECT (the set an overlap
would have to run serially after the pool). If a new such reader appears, this
fails — reminding whoever adds it that it would race an in-place fixture
rebuild under overlap, and must be added to the deselect set (see the spec).

Two independent checks:
  1. Auto-derive the in-place-rebuilt dir set from the harnesses and assert it
     equals the hardcoded EXPECTED set filtered to harnesses present in THIS
     tree (a brittle-parser miss fails loudly here; the tree filter keeps it
     correct on the public mirror, where some harnesses are private/absent).
  2. Assert every pytest reader of any dir in that set is in KNOWN_SAFE_DESELECT.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

# The audit's durable output (#296 spec audit table). Update — and re-audit
# the readers — only when a harness starts/stops rebuilding a fixtures/<x> in
# place. build-codex-fixtures.py is the one builder whose roots are NOT
# fixtures/<name>; its suite calls are all --out-redirected, so it never lands
# here (mapping handled below for correctness).
EXPECTED_INPLACE_DIRS = {
    "dashboard", "doctor", "pricing-check", "conversation", "share", "share-v2",
}
# The pytest files an overlap would have to deselect (run serially after the
# pool).
KNOWN_SAFE_DESELECT = {
    # The three #503 S1 share guards below are mirror-private, so a public
    # clone does not carry them. Naming them here is safe anyway, and is
    # waived for tests/test_public_test_dep_closure.py Scope A2 on that basis:
    # mirror-private-ok — this list is only ever read as a superset of the
    # readers `_pytest_readers_of` DISCOVERS by globbing, so an entry whose
    # file is absent is never matched and never asserted about.
    "tests/test_dashboard_responsive_startup.py",
    # #503 S1: the share privacy detector's negative corpus reads every
    # committed golden under both share fixture roots, which
    # bin/cctally-share-test and bin/cctally-share-v2-test rebuild in place.
    # (Spelling the two directory names out here would make this file itself
    # match the reader scan below.)
    "tests/test_share_privacy_detector.py",
    # #503 S1 B1/B3: reads the panel fixtures and the harness's `--plan` case
    # list under the share-v2 root, which bin/cctally-share-v2-test rebuilds in
    # place on every start.
    "tests/test_share_golden_discipline.py",
    # #503 S1 B4: the registry/fixture/golden bijection enumerates both share
    # fixture roots, and both harnesses rebuild theirs in place.
    "tests/test_share_registry_completeness.py",
    # #503 S2: the boundary-kind classification, the chart bounding-box
    # sweep and the empty-table cases build every registered template from
    # the share-v2 panel fixtures, which bin/cctally-share-v2-test rebuilds
    # in place on every start. Unlike the four entries above, these two
    # files are PUBLIC — their share-v2 reads carry a module-level
    # `.is_dir()` skip gate so a public clone skips them rather than
    # failing, which is a separate concern from the overlap race this list
    # records.
    "tests/test_lib_share.py",
    "tests/test_lib_share_v2.py",
    # #496 S6 §7.6: the tripwire that asserts each of the four new doctor
    # scenarios still reaches the WARN or FAIL branch it exists to cover reads
    # their committed `expected.txt` and `expected.exit`, under a root
    # bin/cctally-doctor-test rebuilds in place. The tripwire exists because a
    # gather defect once degraded all four to "retention scan unavailable" —
    # which reads exactly like a healthy install — with the harness still green
    # because the goldens had been regenerated from the broken output.
    "tests/test_doctor_journal_legs.py",
}
# builder stem -> output roots when NOT tests/fixtures/<stem>.
BUILDER_ROOT_EXCEPTIONS = {
    "codex": ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"],
}
OUTDIR_MARKERS = ("--out", "--out-dir", "build_fixtures_cached", "SCRATCH")


def _logical_lines(text):
    """Join backslash line-continuations into single logical lines."""
    out, buf = [], ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append(buf + raw)
        buf = ""
    if buf:
        out.append(buf)
    return out


# A builder invocation in COMMAND position: optional leading whitespace, an
# optional `python3 ` / `env VAR=val ` prefix, then the builder path token.
# Excludes `#`-comments, `FAIL: build-… crashed` echoes (builder mid-string),
# and `B=…`/`SB=…` assignments (token not in command position).
_CMD = re.compile(
    r'^\s*(?:python3\s+|env(?:\s+\w+=\S+)*\s+)*'
    r'"?\$(?:REPO_ROOT|REPO)"?/bin/build-([a-z0-9-]+)-fixtures\.py\b(.*)$'
)


def _derive_inplace_dirs():
    dirs = set()
    for harness in sorted((REPO / "bin").glob("cctally-*-test")):
        for line in _logical_lines(harness.read_text()):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            m = _CMD.match(line)
            if not m:
                continue
            stem = m.group(1)
            if any(mark in line for mark in OUTDIR_MARKERS):
                continue  # redirected to scratch -> not in-place
            dirs.update(BUILDER_ROOT_EXCEPTIONS.get(stem, [stem]))
    return dirs


def _expected_dirs_present():
    """EXPECTED filtered to harnesses actually present in this tree.

    Every EXPECTED dir maps 1:1 to bin/cctally-<dir>-test. The public mirror
    excludes some harnesses (bin/cctally-share-v2-test is mirror-private), so
    their fixture dir cannot be auto-derived there; requiring exact equality
    against the full EXPECTED would red public CI (#296). Filtering keeps the
    check strong on every tree: a parser miss still drops a *present* dir
    (RED), and a new in-place rebuilder still adds a dir not in EXPECTED (RED).
    """
    return {
        d for d in EXPECTED_INPLACE_DIRS
        if (REPO / "bin" / ("cctally-%s-test" % d)).exists()
    }


def _pytest_readers_of(dirs):
    """tests/*.py files that reference tests/fixtures/<d> for any d in dirs."""
    readers = {}
    for test in sorted((REPO / "tests").glob("test_*.py")):
        text = test.read_text()
        for d in dirs:
            # literal "fixtures/<d>" OR pathlib "fixtures", "<d>" / "fixtures" / "<d>"
            pat = (
                r'fixtures/%s\b' % re.escape(d)
                + r'|"fixtures"\s*[,/]\s*"%s"' % re.escape(d)
            )
            if re.search(pat, text):
                readers.setdefault("tests/" + test.name, set()).add(d)
    return readers


def test_derived_inplace_dirs_match_expected():
    derived = _derive_inplace_dirs()
    expected = _expected_dirs_present()
    assert derived == expected, (
        "auto-derived in-place-rebuilt dirs %s != expected-present %s "
        "(new/removed in-place rebuilder — re-audit its pytest readers, or "
        "update EXPECTED_INPLACE_DIRS)" % (sorted(derived), sorted(expected))
    )


def test_every_inplace_reader_is_deselected():
    readers = _pytest_readers_of(EXPECTED_INPLACE_DIRS)
    missing = {f: sorted(ds) for f, ds in readers.items()
               if f not in KNOWN_SAFE_DESELECT}
    assert not missing, (
        "pytest files read an in-place-rebuilt fixture dir but are NOT in "
        "KNOWN_SAFE_DESELECT: %s. Under a pytest/shell-pool overlap (#296) they "
        "would race the rebuild; add them to KNOWN_SAFE_DESELECT and, if the "
        "overlap is (re-)enabled, to the runner's deselect list." % missing
    )


def test_guard_is_non_vacuous():
    # There is at least one real reader being guarded (else the test is vacuous).
    assert _pytest_readers_of(EXPECTED_INPLACE_DIRS), \
        "no in-place fixture readers found — guard would be vacuous"
