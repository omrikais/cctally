"""#529 S5 §4.5-§4.6 — the declared harness-ownership map and the query over it.

`bin/cctally-test-owners` answers "which harnesses own this change?" from two
declared files (`tests/harness-ownership.json` plus the mirror-private overlay
`tests/harness-ownership.private.json`) and a scraper that re-derives the
fixture edges from each harness's own text.

Two rules make the answer safe to act on, and both are tested here rather than
assumed:

  * Only mechanically verified evidence narrows. A changed fixture directory, a
    changed builder and a change to a harness file itself are the whole list.
    Every other changed path — including one attributed through the advisory
    hand-maintained source map — widens the answer to the entire shell estate.
  * A fixture edge the scraper cannot prove marks its harness **opaque**, which
    also widens to the entire estate. An unrecognised construction is never
    silently treated as "this harness owns no fixtures".

The overlay is discovered by existence: a public clone carries only the public
half, and the tooling must operate on public rows alone there rather than
erroring. That is exercised against synthetic trees below, so the assertion
does not depend on which tree the suite happens to run in.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import pty
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "cctally-test-owners"
MANIFEST = ROOT / "tests" / "authoritative-test-manifest.json"
PUBLIC_OWNERSHIP = ROOT / "tests" / "harness-ownership.json"


def _load_owners():
    # SourceFileLoader by hand: the script is EXTENSIONLESS, and
    # spec_from_file_location returns None for a suffix it has no loader for.
    loader = importlib.machinery.SourceFileLoader("_cctally_test_owners", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


owners = _load_owners()


# --- the scraper ------------------------------------------------------------


def test_scraper_finds_a_literal_fixture_reference() -> None:
    """Form 1: a literal ``tests/fixtures/<name>`` path anywhere in the text."""
    scrape = owners.scrape_fixture_edges(
        '#!/usr/bin/env bash\ncp "$REPO_ROOT/tests/fixtures/foo/case-a/db.sqlite" "$dest"\n'
    )
    assert scrape.edges == {"tests/fixtures/foo"}
    assert scrape.opaque_reasons == []


def test_scraper_finds_a_literal_builder_reference() -> None:
    """Form 1, other half: the builder is an edge in its own right."""
    scrape = owners.scrape_fixture_edges(
        '#!/usr/bin/env bash\npython3 "$REPO_ROOT/bin/build-foo-fixtures.py" --out "$d"\n'
    )
    assert scrape.edges == {"bin/build-foo-fixtures.py"}
    assert scrape.opaque_reasons == []


def test_scraper_reads_a_harness_fixtures_dir_assignment() -> None:
    """Form 2: the ``HARNESS_FIXTURES_DIR=`` assignment the shared fixture
    wrapper consumes. A value carrying no resolvable fixture literal is opaque,
    which is the half a literal scan cannot see."""
    resolved = owners.scrape_fixture_edges(
        'HARNESS_FIXTURES_DIR="${REPO_ROOT}/tests/fixtures/foo"\n'
    )
    assert resolved.edges == {"tests/fixtures/foo"}
    assert resolved.opaque_reasons == []

    unresolved = owners.scrape_fixture_edges('HARNESS_FIXTURES_DIR="$SOME_ROOT"\n')
    assert unresolved.edges == set()
    assert unresolved.opaque_reasons, "an unresolvable assignment must be opaque"


def test_scraper_expands_the_staging_helper_into_both_edges() -> None:
    """Form 3: ``bin/_lib-harness-env.sh`` builds BOTH a fixture directory and a
    builder path out of the helper's single argument, so a per-harness literal
    scan sees neither."""
    scrape = owners.scrape_fixture_edges(
        'stage_fixtures_out_of_tree foo "$FIXTURES_ROOT" || exit 1\n'
    )
    assert scrape.edges == {"tests/fixtures/foo", "bin/build-foo-fixtures.py"}
    assert scrape.opaque_reasons == []


def test_scraper_marks_a_constructed_staging_argument_opaque() -> None:
    """The rule that keeps the safe set honest: a construction the scraper
    cannot prove is opaque, never absent. Absent would mean "this harness owns
    no fixtures", which narrows; opaque widens to the whole estate."""
    scrape = owners.scrape_fixture_edges(
        'stage_fixtures_out_of_tree "$SOME_VAR" "$FIXTURES_ROOT"\n'
    )
    assert scrape.edges == set()
    assert scrape.opaque_reasons, "a variable argument must mark the harness opaque"


def test_scraper_normalises_a_nested_fixture_path_to_its_directory() -> None:
    """Ownership is declared at the fixture-directory granularity, so a deep
    literal and a shallow one are the same edge."""
    scrape = owners.scrape_fixture_edges(
        "tests/fixtures/migrations/per-migration/001_x/pre.sqlite\n"
    )
    assert scrape.edges == {"tests/fixtures/migrations"}


def test_scraper_marks_a_component_wise_fixture_build_opaque() -> None:
    """The catch-all probe, half one. ``repo_root / "tests" / "fixtures" / …``
    builds the same directory out of separate string constants, so the literal
    scan sees nothing and — before the probe — the harness declared no edges at
    all. Silence is the dangerous direction: a harness whose real edge is
    invisible narrows every query that another harness declares the same
    directory in."""
    scrape = owners.scrape_fixture_edges(
        'FIXTURE_DIR = repo_root / "tests" / "fixtures" / "projects"\n'
    )
    assert scrape.edges == set()
    assert scrape.opaque_reasons, "a component-wise build must mark the harness opaque"


def test_scraper_marks_a_shell_interpolated_fixture_path_opaque() -> None:
    """The catch-all probe, half two: ``tests/fixtures/`` whose next character
    cannot be part of a directory name names a directory this scraper cannot
    read."""
    scrape = owners.scrape_fixture_edges(
        'cp "$REPO_ROOT/tests/fixtures/$CMD/case" "$dest"\n'
    )
    assert scrape.edges == set()
    assert scrape.opaque_reasons, "an interpolated fixture path must be opaque"


def test_the_catch_all_probe_leaves_a_provable_literal_alone() -> None:
    """The probe must be a tightening, not a blanket. A harness carrying only
    readable forms stays provable even when both forms appear together, and an
    unprovable sibling in the SAME text still marks it opaque rather than
    letting the provable half stand for the whole."""
    provable = owners.scrape_fixture_edges(
        'cp "$REPO_ROOT/tests/fixtures/foo/a" .\n'
        'python3 "$REPO_ROOT/bin/build-foo-fixtures.py"\n'
        'stage_fixtures_out_of_tree bar "$d"\n'
    )
    assert provable.opaque_reasons == []
    assert provable.edges == {
        "tests/fixtures/foo",
        "bin/build-foo-fixtures.py",
        "tests/fixtures/bar",
        "bin/build-bar-fixtures.py",
    }

    mixed = owners.scrape_fixture_edges(
        'cp "$REPO_ROOT/tests/fixtures/foo/a" .\n'
        'cp "$REPO_ROOT/tests/fixtures/$CMD/a" .\n'
    )
    assert mixed.edges == {"tests/fixtures/foo"}
    assert mixed.opaque_reasons


def test_scraper_ignores_a_mention_that_only_appears_in_a_comment() -> None:
    """A comment is prose about the harness, not a path the harness reaches.

    Four committed declarations were scraped out of ``#`` lines, three of them
    naming builders no harness runs. A false edge is only an over-selection, but
    it pollutes the one artifact this feature's answer rests on, and the fourth
    hid a REAL edge behind a comment rather than behind code.
    """
    scrape = owners.scrape_fixture_edges(
        "#!/usr/bin/env bash\n"
        "# Fixture: tests/fixtures/mode/mixed-cost — described, never read.\n"
        "#   (parallels build-forecast-fixtures.py, which this harness never runs)\n"
        "   # an indented comment is a comment too\n"
        "echo no fixtures\n"
    )
    assert scrape.edges == set()
    assert scrape.opaque_reasons == []


def test_a_comment_does_not_mark_a_harness_opaque_either() -> None:
    """The catch-all probe reads code for the same reason the edge scan does. A
    prose mention of an interpolated path would otherwise widen every query
    forever, and nothing in the tree could clear it."""
    scrape = owners.scrape_fixture_edges(
        "#!/usr/bin/env bash\n"
        '# each case resolves "$REPO_ROOT/tests/fixtures/$CMD" for itself\n'
        '# and the projects leg uses repo_root / "tests" / "fixtures" / "projects"\n'
        "echo no fixtures\n"
    )
    assert scrape.edges == set()
    assert scrape.opaque_reasons == []


# --- helpers for the synthetic-tree tests -----------------------------------


def _run(*args: str, cwd: pathlib.Path | None = None, stdin: str | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        input=stdin,
    )


def _make_tree(
    root: pathlib.Path,
    harnesses: dict[str, str],
    *,
    private: dict[str, str] | None = None,
    ownership: dict | None = None,
    overlay: dict | None = None,
    manifest_rows: list[dict] | None = None,
) -> None:
    """A minimal repository: a manifest, harness files, and ownership rows.

    `harnesses` maps a harness NAME to the body of ``bin/cctally-<name>-test``.
    `private` does the same for rows that belong in the overlay. Passing
    `ownership`/`overlay`/`manifest_rows` overrides what would otherwise be
    derived from those bodies, which is how each mutation below is introduced.
    """
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    private = private or {}
    rows = []
    derived_public: dict[str, dict] = {}
    derived_private: dict[str, dict] = {}
    for name, body in sorted({**harnesses, **private}.items()):
        path = root / "bin" / f"cctally-{name}-test"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        visibility = "private" if name in private else "public"
        rows.append(
            {
                "name": name,
                "visibility": visibility,
                "minCases": 1,
                "countPolicy": "fixed",
            }
        )
        scrape = owners.scrape_fixture_edges(body)
        row = {"fixturePaths": sorted(scrape.edges), "sourcePaths": []}
        (derived_private if name in private else derived_public)[name] = row
        for edge in scrape.edges:
            target = root / edge
            if edge.startswith("tests/fixtures/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "seededAt": "2026-08-11",
        "minHarnessRows": 0,
        "harnesses": manifest_rows if manifest_rows is not None else rows,
    }
    MANIFEST_PATH = root / "tests" / "authoritative-test-manifest.json"
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    public_doc = {
        "schemaVersion": 1,
        "harnesses": derived_public if ownership is None else ownership,
    }
    (root / "tests" / "harness-ownership.json").write_text(
        json.dumps(public_doc, indent=2) + "\n", encoding="utf-8"
    )
    if private or overlay is not None:
        overlay_doc = {
            "schemaVersion": 1,
            "harnesses": derived_private if overlay is None else overlay,
        }
        (root / "tests" / "harness-ownership.private.json").write_text(
            json.dumps(overlay_doc, indent=2) + "\n", encoding="utf-8"
        )


def _private_profile(root: pathlib.Path) -> None:
    """`.mirror-allowlist` present is exactly the private discriminator, the
    same one `bin/cctally-test-all` uses."""
    (root / ".mirror-allowlist").write_text("bin/cctally-*\ntests/**\n", encoding="utf-8")


_HARNESS = '#!/usr/bin/env bash\ncp "$REPO_ROOT/tests/fixtures/{name}/a" .\n'
_BARE = "#!/usr/bin/env bash\necho no fixtures\n"


# --- --verify ---------------------------------------------------------------


def test_verify_accepts_a_consistent_tree(tmp_path) -> None:
    """The green control. Every mutation below is measured against this."""
    _make_tree(tmp_path, {"alpha": _HARNESS.format(name="alpha"), "beta": _BARE})
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr


def test_verify_rejects_a_removed_declaration(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _HARNESS.format(name="alpha")},
        ownership={"alpha": {"fixturePaths": [], "sourcePaths": []}},
    )
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "tests/fixtures/alpha" in res.stdout + res.stderr


def test_verify_rejects_a_stale_declaration(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _HARNESS.format(name="alpha")},
        ownership={
            "alpha": {
                "fixturePaths": ["tests/fixtures/alpha", "tests/fixtures/gone"],
                "sourcePaths": [],
            }
        },
    )
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "tests/fixtures/gone" in res.stdout + res.stderr


def test_verify_rejects_a_declaration_the_harness_no_longer_reaches(tmp_path) -> None:
    """The other direction, isolated. The stale-declaration case above is also
    caught by the exists-on-disk check, so on its own it would not prove that
    `declared - discovered` is checked at all: dropping that comparison left it
    green. Here the declared directory EXISTS and only the harness's text has
    stopped naming it."""
    _make_tree(
        tmp_path,
        {"alpha": _HARNESS.format(name="alpha")},
        ownership={
            "alpha": {
                "fixturePaths": ["tests/fixtures/alpha", "tests/fixtures/retired"],
                "sourcePaths": [],
            }
        },
    )
    (tmp_path / "tests" / "fixtures" / "retired").mkdir(parents=True)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "tests/fixtures/retired" in res.stdout + res.stderr


def test_verify_rejects_a_declaration_whose_directory_is_gone(tmp_path) -> None:
    """The exists-on-disk check, isolated.

    Every other case that removes a fixture directory also removes the harness
    text that named it, so `declared - discovered` fires first and the shared
    path substring appears either way — deleting the exists check left the whole
    suite green. Here the harness still NAMES the directory, so the scraper
    still discovers it and only the disk is missing. The wording is asserted
    rather than the path, because the path alone cannot tell the two apart.
    """
    _make_tree(tmp_path, {"alpha": _HARNESS.format(name="alpha")})
    (tmp_path / "tests" / "fixtures" / "alpha").rmdir()
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "does not exist on disk" in out, out
    assert "reaches no such edge" not in out, out


def test_verify_rejects_an_undeclared_edge_on_a_harness_declaring_none(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _HARNESS.format(name="alpha")},
        ownership={"alpha": {"fixturePaths": [], "sourcePaths": []}},
    )
    (tmp_path / "tests" / "fixtures" / "alpha").mkdir(parents=True, exist_ok=True)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr


def test_verify_rejects_a_row_present_in_both_files(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _BARE},
        private={"secret": _BARE},
        overlay={
            "secret": {"fixturePaths": [], "sourcePaths": []},
            "alpha": {"fixturePaths": [], "sourcePaths": []},
        },
    )
    _private_profile(tmp_path)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "alpha" in res.stdout + res.stderr


def test_a_duplicated_row_is_reported_as_the_duplication_and_nothing_else(
    tmp_path,
) -> None:
    """A row in both files necessarily sits in the wrong one of them too, so the
    placement loops fired as well and the duplicate check could never change the
    exit code — its own case passed with the check deleted. The placement loops
    now step over a name the duplicate check has already claimed, which makes
    the duplicate message the one that has to be produced.
    """
    _make_tree(
        tmp_path,
        {"alpha": _BARE},
        private={"secret": _BARE},
        overlay={
            "secret": {"fixturePaths": [], "sourcePaths": []},
            "alpha": {"fixturePaths": [], "sourcePaths": []},
        },
    )
    _private_profile(tmp_path)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "appears in BOTH" in out, out
    assert "sits in the private overlay" not in out, out


def test_verify_rejects_a_private_harness_row_kept_in_the_public_file(tmp_path) -> None:
    """Placement, the direction the overlay half does not cover. A private
    harness whose row sits in the PUBLIC file publishes that harness's fixture
    layout to the mirror, and the row is then also missing from the overlay a
    private checkout reads."""
    _make_tree(
        tmp_path,
        {"alpha": _BARE},
        private={"secret": _BARE},
        ownership={
            "alpha": {"fixturePaths": [], "sourcePaths": []},
            "secret": {"fixturePaths": [], "sourcePaths": []},
        },
        overlay={},
    )
    _private_profile(tmp_path)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "PUBLIC ownership file" in out, out
    assert "secret" in out, out


def test_verify_refuses_to_be_combined_with_a_query(tmp_path) -> None:
    """The explicit guard, not argparse.

    The existing invalid-invocation case passes ``--nonsense``, which argparse
    rejects before the guard is reached, so exit 2 there proves nothing about
    the guard. Each combination below is a VALID argparse parse that only this
    check refuses, and the tool's own wording is asserted so the two paths
    cannot be confused.
    """
    _make_tree(tmp_path, {"alpha": _BARE})
    for extra in (
        ["tests/fixtures/alpha"],
        ["--names-only"],
        ["--from-diff", "HEAD..HEAD"],
    ):
        res = _run("--verify", "--repo-root", str(tmp_path), *extra)
        assert res.returncode == 2, (extra, res.stdout, res.stderr)
        assert "--verify takes no" in res.stdout + res.stderr, (extra, res.stderr)


def test_verify_rejects_an_ownership_row_with_no_manifest_row(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _BARE},
        ownership={
            "alpha": {"fixturePaths": [], "sourcePaths": []},
            "ghost": {"fixturePaths": [], "sourcePaths": []},
        },
    )
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "ghost" in res.stdout + res.stderr


def test_verify_rejects_a_manifest_harness_with_no_ownership_row(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {"alpha": _BARE, "beta": _BARE},
        ownership={"alpha": {"fixturePaths": [], "sourcePaths": []}},
    )
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "beta" in res.stdout + res.stderr


def test_verify_rejects_a_manifest_row_with_no_harness_on_disk(tmp_path) -> None:
    """Harness-discovery drift, direction one: the estate the manifest declares
    is not the estate on disk."""
    _make_tree(tmp_path, {"alpha": _BARE})
    (tmp_path / "bin" / "cctally-alpha-test").unlink()
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr


def test_verify_rejects_a_harness_on_disk_with_no_manifest_row(tmp_path) -> None:
    """Direction two. The remote wrapper materialises UNCOMMITTED files, so an
    unstaged harness is a routine cause."""
    _make_tree(tmp_path, {"alpha": _BARE})
    stray = tmp_path / "bin" / "cctally-stray-test"
    stray.write_text(_BARE, encoding="utf-8")
    stray.chmod(0o755)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "stray" in res.stdout + res.stderr


def test_verify_rejects_a_harness_that_lost_its_executable_bit(tmp_path) -> None:
    _make_tree(tmp_path, {"alpha": _BARE})
    (tmp_path / "bin" / "cctally-alpha-test").chmod(0o644)
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr
    assert "alpha" in res.stdout + res.stderr


def test_verify_without_the_overlay_is_normal_on_a_public_tree(tmp_path) -> None:
    """A public clone carries the public half only. The overlay's absence is
    the expected state there and must not be an error."""
    _make_tree(tmp_path, {"alpha": _BARE})
    assert not (tmp_path / "tests" / "harness-ownership.private.json").exists()
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "public" in (res.stdout + res.stderr).lower()


def test_verify_requires_the_overlay_under_the_private_profile(tmp_path) -> None:
    """In the private checkout its absence is a silent narrowing, not a clone."""
    _make_tree(tmp_path, {"alpha": _BARE}, private={"secret": _BARE})
    _private_profile(tmp_path)
    (tmp_path / "tests" / "harness-ownership.private.json").unlink()
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr


def test_verify_reports_an_unreadable_ownership_file_as_a_failure(tmp_path) -> None:
    """Inability to verify fails; it never passes."""
    _make_tree(tmp_path, {"alpha": _BARE})
    (tmp_path / "tests" / "harness-ownership.json").write_text("{ not json", encoding="utf-8")
    res = _run("--verify", "--repo-root", str(tmp_path))
    assert res.returncode == 3, res.stdout + res.stderr


def test_verify_rejects_an_invalid_invocation(tmp_path) -> None:
    res = _run("--verify", "--repo-root", str(tmp_path), "--nonsense")
    assert res.returncode == 2, res.stdout + res.stderr


def test_verify_passes_against_the_real_tree() -> None:
    """The committed ownership files agree with the committed estate."""
    res = _run("--verify", "--repo-root", str(ROOT))
    assert res.returncode == 0, res.stdout + res.stderr


# --- the query --------------------------------------------------------------


def _query_tree(root: pathlib.Path) -> None:
    _make_tree(
        root,
        {
            "alpha": _HARNESS.format(name="alpha")
            + 'python3 "$REPO_ROOT/bin/build-alpha-fixtures.py"\n',
            "beta": _HARNESS.format(name="beta"),
            "gamma": _BARE,
        },
    )


def _safe_set(res) -> set[str]:
    return {n for n in res.stdout.split("\0") if n}


def test_query_narrows_to_the_harness_owning_a_changed_fixture(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run(
        "--names-only", "--repo-root", str(tmp_path), "tests/fixtures/alpha/case/db.sqlite"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha"}


def test_query_narrows_to_the_harness_owning_a_changed_builder(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), "bin/build-alpha-fixtures.py")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha"}


def test_query_narrows_to_a_changed_harness_file(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), "bin/cctally-beta-test")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"beta"}


def test_query_widens_to_the_whole_estate_for_a_changed_module(tmp_path) -> None:
    """Source attribution is advisory and may never narrow anything."""
    _query_tree(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), "bin/_lib_share.py")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha", "beta", "gamma"}


def test_an_advisory_source_edge_never_narrows(tmp_path) -> None:
    """The tempting mistake: a plausible hand-maintained map deciding what to
    skip. Declaring the changed module as alpha's source path must still yield
    the whole estate."""
    _query_tree(tmp_path)
    doc = json.loads((tmp_path / "tests" / "harness-ownership.json").read_text())
    doc["harnesses"]["alpha"]["sourcePaths"] = ["bin/_lib_share.py"]
    (tmp_path / "tests" / "harness-ownership.json").write_text(
        json.dumps(doc, indent=2) + "\n", encoding="utf-8"
    )
    res = _run("--names-only", "--repo-root", str(tmp_path), "bin/_lib_share.py")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha", "beta", "gamma"}

    # The advisory attribution must be REPORTED as such. Without this the test
    # would also pass against an implementation that ignored sourcePaths
    # entirely, and would then prove nothing about the rule it exists for.
    human = _run("--repo-root", str(tmp_path), "bin/_lib_share.py")
    assert "advisory" in human.stdout.lower(), human.stdout
    assert "alpha" in human.stdout


def test_an_opaque_harness_widens_the_whole_answer(tmp_path) -> None:
    _make_tree(
        tmp_path,
        {
            "alpha": _HARNESS.format(name="alpha"),
            "murky": '#!/usr/bin/env bash\nstage_fixtures_out_of_tree "$WHICH" "$d"\n',
        },
    )
    res = _run(
        "--names-only", "--repo-root", str(tmp_path), "tests/fixtures/alpha/case/db.sqlite"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha", "murky"}


def test_one_unattributable_path_widens_the_union(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run(
        "--names-only",
        "--repo-root",
        str(tmp_path),
        "tests/fixtures/alpha/case/db.sqlite",
        "docs/commands/diff.md",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha", "beta", "gamma"}


def test_query_reads_paths_from_stdin(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run(
        "--names-only",
        "--repo-root",
        str(tmp_path),
        stdin="tests/fixtures/alpha/case/db.sqlite\n",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha"}


def test_query_normalises_an_absolute_path_inside_the_repository(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run(
        "--names-only",
        "--repo-root",
        str(tmp_path),
        str(tmp_path / "tests" / "fixtures" / "alpha" / "case" / "db.sqlite"),
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha"}


def test_query_rejects_a_path_outside_the_repository(tmp_path) -> None:
    _query_tree(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), "/etc/hosts")
    assert res.returncode == 2, res.stdout + res.stderr


def test_query_rejects_an_invocation_carrying_no_paths(tmp_path) -> None:
    """Nothing to attribute is a mistake in the invocation, not an answer."""
    _query_tree(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), stdin="")
    assert res.returncode == 2, res.stdout + res.stderr


def _git_seed(root: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=str(root),
        check=True,
    )


def test_an_empty_safe_set_emits_nothing_and_says_so(tmp_path) -> None:
    """The contract Tranche 3 codes against: empty stdout means RUN NO HARNESS.

    A consumer that turned an empty emission into a bare `bin/cctally-test-all`
    would run the whole estate as an authoritative full run, so the emptiness
    is announced on stderr rather than left to be inferred from silence.
    """
    _query_tree(tmp_path)
    _git_seed(tmp_path)
    res = _run("--names-only", "--repo-root", str(tmp_path), "--from-diff", "HEAD..HEAD")
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout == ""
    assert "no harness" in res.stderr.lower()


def test_from_diff_treats_a_rename_as_both_paths(tmp_path) -> None:
    """A rename changes two paths, and the old one owns a harness the new one
    does not. Counting only the new path would silently drop `alpha`."""
    _query_tree(tmp_path)
    src = tmp_path / "tests" / "fixtures" / "alpha" / "case.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("a fixture body long enough for rename detection\n" * 4, encoding="utf-8")
    _git_seed(tmp_path)
    dest = tmp_path / "tests" / "fixtures" / "beta" / "case.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "mv", str(src), str(dest)], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "move"],
        cwd=str(tmp_path),
        check=True,
    )
    res = _run(
        "--names-only", "--repo-root", str(tmp_path), "--from-diff", "HEAD~1..HEAD"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _safe_set(res) == {"alpha", "beta"}


def test_the_diff_parser_treats_a_copy_as_both_paths() -> None:
    """A ``C`` record carries TWO path tokens, exactly as ``R`` does.

    Reached at the parser rather than through ``--from-diff``, because
    ``_from_diff`` passes ``-M``, and ``-M`` on the command line selects
    rename-only detection and overrides ``diff.renames=copies`` — measured: git
    reports the copy as a plain ``A`` under every combination the current
    invocation can produce. The branch is still the correct parse and still
    worth holding: a status that consumes two tokens read as consuming one
    desynchronises the REST of the stream, so every later path is misread, not
    just this one.
    """
    tokens = [
        "C100", "tests/fixtures/alpha/a", "tests/fixtures/beta/a",
        "R100", "tests/fixtures/gamma/a", "tests/fixtures/delta/a",
        "M", "bin/cctally",
    ]
    assert owners._parse_name_status(tokens) == [
        "tests/fixtures/alpha/a", "tests/fixtures/beta/a",
        "tests/fixtures/gamma/a", "tests/fixtures/delta/a",
        "bin/cctally",
    ]


def test_a_bare_invocation_on_a_terminal_refuses_instead_of_blocking(tmp_path) -> None:
    """A person who types the command with no arguments gets the usage message.

    Reading stdin to EOF is right for a pipe and wrong for a terminal, where
    there is no EOF coming: the tool simply sat there. Driven through a real pty
    because ``isatty`` is the whole subject, and a pipe cannot exercise it.
    """
    _query_tree(tmp_path)
    master, slave = pty.openpty()
    try:
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(tmp_path)],
            stdin=slave, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the tool blocked reading stdin from a terminal instead of refusing")
    finally:
        os.close(slave)
        os.close(master)
    assert res.returncode == 2, res.stdout + res.stderr
    assert "no changed paths" in res.stderr.lower(), res.stderr


def test_a_defect_is_not_downgraded_by_the_words_in_its_message(tmp_path) -> None:
    """Exit 2 against exit 3 is decided by WHICH error was raised.

    The code was chosen by testing whether the message contained "outside the
    repository", so a repository whose own path carries that phrase reported an
    inability to check as an invocation mistake — and a caller reading 2 as "my
    command was wrong" would never look at the defect. A path with spaces in it
    is ordinary; the phrase in it is what makes this a discriminating case.
    """
    broken = tmp_path / "outside the repository"
    broken.mkdir()
    _query_tree(broken)
    (broken / "tests" / "harness-ownership.json").write_text("{ not json", encoding="utf-8")
    res = _run(
        "--names-only", "--repo-root", str(broken), "tests/fixtures/alpha/case/db.sqlite"
    )
    assert res.returncode == 3, res.stdout + res.stderr

    # The genuine usage error still exits 2, so the fix did not simply collapse
    # both paths onto the defect code. Measured on a HEALTHY tree, because the
    # ownership files are loaded before any path is normalised and the defect
    # above would otherwise be the error that surfaces.
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    _query_tree(healthy)
    outside = _run("--names-only", "--repo-root", str(healthy), "/etc/hosts")
    assert outside.returncode == 2, outside.stdout + outside.stderr


def test_an_empty_answer_never_also_claims_it_widened(tmp_path) -> None:
    """The two statements contradict each other, so only one may be printed.

    Opacity is recorded before the empty-paths branch, so a query that names no
    path at all against a tree carrying an opaque harness printed "(empty — no
    harness needs to run)" and "Widened to the whole estate because…" one under
    the other. An operator cannot act on that, and the safe reading of the pair
    is the wrong one.
    """
    _make_tree(
        tmp_path,
        {
            "alpha": _HARNESS.format(name="alpha"),
            "murky": '#!/usr/bin/env bash\nstage_fixtures_out_of_tree "$WHICH" "$d"\n',
        },
    )
    _git_seed(tmp_path)
    res = _run("--repo-root", str(tmp_path), "--from-diff", "HEAD..HEAD")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "(empty" in res.stdout, res.stdout
    assert "Widened to the whole estate" not in res.stdout, res.stdout

    # The same tree WITH a changed path still says why it widened, so the gate
    # suppresses the contradiction rather than the explanation.
    named = _run(
        "--repo-root", str(tmp_path), "tests/fixtures/alpha/case/db.sqlite"
    )
    assert "Widened to the whole estate" in named.stdout, named.stdout


def test_the_human_report_separates_owners_unattributed_and_the_safe_set(
    tmp_path,
) -> None:
    _query_tree(tmp_path)
    res = _run(
        "--repo-root",
        str(tmp_path),
        "tests/fixtures/alpha/case/db.sqlite",
        "docs/commands/diff.md",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout
    assert "alpha" in out
    assert "docs/commands/diff.md" in out
    assert "beta" in out and "gamma" in out


def test_every_answer_states_the_pytest_estate_stays_full(tmp_path) -> None:
    """With counts computed live, so the statement cannot go stale."""
    _query_tree(tmp_path)
    (tmp_path / "tests" / "test_one.py").write_text("load_script\n", encoding="utf-8")
    (tmp_path / "tests" / "test_two.py").write_text("nothing\n", encoding="utf-8")
    res = _run("--repo-root", str(tmp_path), "tests/fixtures/alpha/case/db.sqlite")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "2" in res.stdout and "1" in res.stdout
    assert "pytest" in res.stdout.lower()


def test_the_query_launches_no_tests(tmp_path) -> None:
    """A sentinel harness that would leave a trace if it were ever executed."""
    marker = tmp_path / "executed"
    _make_tree(
        tmp_path,
        {"alpha": f'#!/usr/bin/env bash\ntouch "{marker}"\n'},
    )
    _run("--repo-root", str(tmp_path), "bin/cctally-alpha-test")
    assert not marker.exists()


# --- the committed files ----------------------------------------------------


def test_the_public_ownership_file_covers_every_public_manifest_harness() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    public_names = {
        row["name"] for row in manifest["harnesses"] if row.get("visibility") == "public"
    }
    doc = json.loads(PUBLIC_OWNERSHIP.read_text(encoding="utf-8"))
    assert doc["schemaVersion"] == 1
    assert set(doc["harnesses"]) == public_names


def test_every_committed_row_declares_both_lists() -> None:
    doc = json.loads(PUBLIC_OWNERSHIP.read_text(encoding="utf-8"))
    for name, row in doc["harnesses"].items():
        assert set(row) == {"fixturePaths", "sourcePaths"}, name
        assert isinstance(row["fixturePaths"], list), name
        assert isinstance(row["sourcePaths"], list), name
