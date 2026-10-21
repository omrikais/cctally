"""The A5s non-vacuity patches apply and revert cleanly (#940 spec 6.7, 7j).

`tests/fixtures/a5s-nonvacuity/` holds seven reversible patches, each a
mutation the direct write accounting and its consumers must detect: the
product without `temp_store=MEMORY`, a fallback charged below its writes, a
default control that does not spill, accounting blind to every write,
accounting that cannot be registered, a measured connection that bypasses
the accounting VFS, and an accumulator that counts only file extents. The
directory's README names, per patch, each target node with its designated
outcome and failure text, and `tests/_a5s_mutation_runner.py` runs them on
every lane. This module only proves, hermetically, that each one still
applies to the current tree and reverts to it byte for byte, and that the
README's per-node table covers exactly these patches, so a later edit to the
consumers cannot leave a patch silently stale.
"""

import pathlib
import shutil
import subprocess

import pytest

import _a5s_mutation_runner as runner

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_DIR = ROOT / "tests" / "fixtures" / "a5s-nonvacuity"
PATCHES = (
    "01-product-temp-store-file.patch",
    "02-fallback-charge-below-its-writes.patch",
    "03-default-control-does-not-spill.patch",
    "04-blind-accounting.patch",
    "05-accounting-unavailable.patch",
    "06-measured-connection-bypasses.patch",
    "07-extent-only-accumulator.patch",
)
MARKER = "A5s non-vacuity"


def _touched(patch_text):
    return sorted({line[len("+++ b/"):].strip()
                   for line in patch_text.splitlines() if line.startswith("+++ b/")})


def _git(tree, *args):
    return subprocess.run(["git", *args], cwd=str(tree), capture_output=True,
                          text=True, timeout=60)


def test_the_directory_holds_exactly_the_four_patches():
    # The node id predates revision 7j's seven patches and is kept stable for
    # the recorded test estate; the body checks the seven PATCHES.
    assert sorted(p.name for p in PATCH_DIR.glob("*.patch")) == list(PATCHES)
    readme = (PATCH_DIR / "README.md").read_text(encoding="utf-8")
    plan = runner.parse_readme(readme)
    assert list(plan) == list(PATCHES), \
        "the README's per-node table must name exactly the seven patches"
    for name, targets in plan.items():
        assert targets, "the README names no target node for " + name
        for target in targets:
            module = ROOT / target.node.split("::", 1)[0]
            assert module.is_file(), (name, target.node)


@pytest.mark.parametrize("name", [pytest.param(n, id=n[:2]) for n in PATCHES])
def test_a_patch_applies_to_the_tree_and_reverts_byte_for_byte(tmp_path, name):
    patch = PATCH_DIR / name
    text = patch.read_text(encoding="utf-8")
    touched = _touched(text)
    assert touched, "the patch names no file"
    tree = tmp_path / "tree"
    tree.mkdir()
    assert _git(tree, "init", "-q").returncode == 0
    originals = {}
    for rel in touched:
        source = ROOT / rel
        assert source.is_file(), rel
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))
        originals[rel] = source.read_bytes()

    check = _git(tree, "apply", "--check", str(patch))
    assert check.returncode == 0, "the patch no longer applies:\n" + check.stderr
    assert _git(tree, "apply", str(patch)).returncode == 0
    for rel in touched:
        mutated = (tree / rel).read_bytes()
        assert mutated != originals[rel], rel
        assert MARKER.encode() in mutated, rel

    reverse = _git(tree, "apply", "-R", str(patch))
    assert reverse.returncode == 0, reverse.stderr
    for rel in touched:
        assert (tree / rel).read_bytes() == originals[rel], rel
