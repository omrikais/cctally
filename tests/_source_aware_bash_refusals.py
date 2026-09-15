"""The four `bin/cctally-source-aware-test` refusals, once, for both shells.

#769 S4 #784 asked for every shell refusal in that harness to hold under both
interpreters this repository runs bash from: `/bin/bash`, the 3.2.57 floor
`bin/cctally-preflight`'s legacy leg parses under, and the Homebrew 5.x build.
#776 is open precisely because the two are not interchangeable.

The first encoding put both interpreters in one parametrization inside the
PUBLIC `tests/test_source_aware_share.py`. That module publishes to
`omrikais/cctally`, whose `test-pr` job runs `bin/cctally-test-all` on
`ubuntu-latest`, where `/opt/homebrew/bin/bash` does not exist — so ten nodes
failed there with a message addressed to a maintainer workstation, and even
had the file existed, Ubuntu's `/bin/bash` is 5.x too, so the parametrization
would have been 5.x twice and the stated purpose would have quietly vanished.

The bodies therefore live here, called once per interpreter by two modules:
the public one runs them under `/bin/bash`, and the mirror-private
`tests/test_source_aware_share_homebrew_bash.py` runs them under the Homebrew
build. Node ids stay static in both, no `skipif` is introduced, and the public
lane runs only what a public clone can run.

This module is public. It loads no `bin/` module, names no private path, and
reads only what it is handed — the same reasoning that keeps
`tests/_harness_emission_contract.py` public.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


#: The interpreter every clone has at a fixed path, which is the reason the
#: public module runs the bodies below under it. It is the 3.2.57 floor ON
#: macOS ONLY. On the `ubuntu-latest` lane the public repository's `test-pr`
#: job uses, `/bin/bash` is 5.x, so a green public run exercises 5.x twice and
#: certifies NOTHING about the floor — which is the very failure the module
#: docstring above criticises in the first encoding. Floor coverage is a
#: maintainer-workstation property; read a green public run accordingly.
FLOOR_BASH = "/bin/bash"

#: The Homebrew 5.x build. Named here rather than in the private twin so both
#: interpreters stay visible together, which is the point #784 makes.
MODERN_BASH = "/opt/homebrew/bin/bash"


def require_interpreter(interpreter: str) -> str:
    assert Path(interpreter).is_file(), (
        f"{interpreter} is absent, so this refusal was checked under one shell "
        "rather than both. Install it rather than narrowing the case: the two "
        "builds are what #776 is open about."
    )
    return interpreter


def builder_shim_farm(
    tmp_path,
    root: Path,
    *,
    canaries_body: str | None = None,
    manifest_body: str | None = None,
) -> Path:
    """A symlink farm whose builder answers one flag however we say.

    `$REPO_ROOT` resolves inside `tmp_path`, so the harness's default retention
    directory lands there too, and every invocation this farm does not intercept
    is delegated to the real builder, so the run is complete and passing in every
    other respect. `tests/fixtures` is symlinked at the PARENT rather than at
    `source-aware`, because `find` resolves a symlink that is a path component
    and refuses to follow one that is its argument.
    """
    farm = tmp_path / "farm"
    (farm / "bin").mkdir(parents=True)
    for name in ("cctally-source-aware-test", "_lib-harness-env.sh",
                 "_lib-golden-diff.sh"):
        (farm / "bin" / name).symlink_to(root / "bin" / name)
    (farm / "tests").mkdir()
    (farm / "tests" / "fixtures").symlink_to(root / "tests" / "fixtures")
    real_builder = root / "bin" / "build-source-aware-fixtures.py"
    intercepts = ""
    if canaries_body is not None:
        intercepts += "if '--canaries' in sys.argv:\n" + canaries_body + "\n"
    if manifest_body is not None:
        intercepts += "if '--manifest' in sys.argv:\n" + manifest_body + "\n"
    assert intercepts, "a shim farm that intercepts nothing is the real builder"
    shim = farm / "bin" / "build-source-aware-fixtures.py"
    shim.write_text(
        "import subprocess\n"
        "import sys\n"
        + intercepts
        + "raise SystemExit(subprocess.call(\n"
        f"    [sys.executable, {str(real_builder)!r}] + sys.argv[1:]))\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return farm


def run_harness(interpreter: str, harness: Path, tmp_path, **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CCTALLY_SOURCE_AWARE_RAW_LOG_DIR"] = str(tmp_path / "retained")
    env.update(extra)
    return subprocess.run(
        [require_interpreter(interpreter), str(harness)],
        text=True, capture_output=True, check=False, env=env,
    )


def check_a_crashed_manifest_builder_is_reported_as_a_builder_failure(
    tmp_path, interpreter
):
    """The manifest leg's status was consumed by a process substitution.

    `while read … done < <(python3 … --manifest)` publishes no status, so a
    builder that died while printing nothing produced an EMPTY expected-path
    list and the harness then announced `fixture count expected 0, actual 190`.
    That names a cause the harness never observed — the count did not disagree,
    the builder never answered — and it is #760's class on the one leg that
    still carried it. The `--canaries` leg was given the scratch-file treatment
    in #769 S1; this is its twin.
    """
    root = Path(__file__).resolve().parents[1]
    farm = builder_shim_farm(
        tmp_path, root, manifest_body="    raise SystemExit(4)"
    )
    result = run_harness(
        interpreter, farm / "bin" / "cctally-source-aware-test", tmp_path
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "FAIL source-aware: fixture manifest builder exit status 4"
    ) in result.stdout, result.stdout
    assert "fixture count expected 0" not in result.stdout, (
        "a crashed manifest builder was reported as a count mismatch, which "
        "names a disagreement the harness never observed:\n" + result.stdout)


def check_the_planted_hook_refuses_a_traversal_out_of_the_temporary_root(
    tmp_path, interpreter
):
    """A textual `case` cannot see `..`.

    `$TMPDIR/../../<repo>/tests/fixtures/source-aware` matches `"$tmp_root"/*`
    character for character and resolves to the committed corpus, which is the
    exact substitution the hook's bound exists to prevent: the generated side
    and the golden side become the same file and the run reports 190 passes
    having certified nothing.
    """
    root = Path(__file__).resolve().parents[1]
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    committed = root / "tests" / "fixtures" / "source-aware"
    traversal = str(tmp_root / os.path.relpath(committed, tmp_root))
    assert traversal.startswith(str(tmp_root) + os.sep), traversal
    assert os.path.realpath(traversal) == os.path.realpath(committed)

    result = run_harness(
        interpreter,
        root / "bin" / "cctally-source-aware-test",
        tmp_path,
        TMPDIR=str(tmp_root),
        CCTALLY_TESTING="1",
        CCTALLY_SOURCE_AWARE_PLANTED_ARTIFACTS=traversal,
    )

    assert result.returncode != 0, (
        "the harness accepted a traversal path out of the temporary root:\n"
        + result.stdout + result.stderr)
    assert (
        "FAIL source-aware: artifact path not under tmpdir "
    ) in result.stdout, result.stdout


def check_the_planted_hook_refuses_a_symlink_out_of_the_temporary_root(
    tmp_path, interpreter
):
    """And it cannot see a symlink either. The link itself lives under the
    temporary root, so every arm of the textual `case` accepts it, and the tree
    it names is the committed corpus."""
    root = Path(__file__).resolve().parents[1]
    tmp_root = tmp_path / "tmproot"
    tmp_root.mkdir()
    committed = root / "tests" / "fixtures" / "source-aware"
    link = tmp_root / "corpus"
    link.symlink_to(committed)
    assert os.path.realpath(link) == os.path.realpath(committed)

    result = run_harness(
        interpreter,
        root / "bin" / "cctally-source-aware-test",
        tmp_path,
        TMPDIR=str(tmp_root),
        CCTALLY_TESTING="1",
        CCTALLY_SOURCE_AWARE_PLANTED_ARTIFACTS=str(link),
    )

    assert result.returncode != 0, (
        "the harness accepted a symlink out of the temporary root:\n"
        + result.stdout + result.stderr)
    assert (
        "FAIL source-aware: artifact path not under tmpdir "
    ) in result.stdout, result.stdout


#: The two malformed canary lists, as (id, builder body).
BLANK_CANARY_BODIES = (
    ("blank-in-the-middle",
     "    sys.stdout.write('alpha\\n\\nomega\\n')\n    raise SystemExit(0)"),
    ("blank-only",
     "    sys.stdout.write('\\n')\n    raise SystemExit(0)"),
    # Whitespace-only, which `[ -z "$canary" ]` accepted (#769 S4). A canary
    # of one space is not empty by that test and `grep -F -- " "` matches
    # nearly every artifact, so the whole defect survived one space.
    ("whitespace-only-in-the-middle",
     "    sys.stdout.write('alpha\\n \\nomega\\n')\n    raise SystemExit(0)"),
    ("whitespace-only",
     "    sys.stdout.write('   \\n')\n    raise SystemExit(0)"),
)


def check_a_blank_canary_is_refused_and_one_nonblank_is_required(
    tmp_path, interpreter, canaries_body
):
    """`grep -F -- ""` matches every artifact, so a blank canary turns the
    privacy leg into an unconditional FAIL on a clean corpus — and the
    blank-only list also means the harness scanned for no real canary at all
    while its count check saw a non-zero total. Both shapes are refused before
    the scan loop begins, which is what makes "at least one non-blank canary"
    a property of the run rather than an assumption about the builder."""
    root = Path(__file__).resolve().parents[1]
    farm = builder_shim_farm(tmp_path, root, canaries_body=canaries_body)
    result = run_harness(
        interpreter, farm / "bin" / "cctally-source-aware-test", tmp_path
    )

    assert result.returncode != 0, (
        "the harness accepted a canary list carrying a blank entry:\n"
        + result.stdout + result.stderr)
    assert "FAIL source-aware: privacy canary empty canary=" in result.stdout, (
        result.stdout)
    assert "privacy canary match" not in result.stdout, (
        "the blank canary reached the scan and matched every artifact:\n"
        + result.stdout)
    assert "source-aware: scanner evidence retained at " in result.stdout, result.stdout
