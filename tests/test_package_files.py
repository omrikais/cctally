"""Guards over the cctally npm + brew distribution surfaces.

Three related invariants protect the runtime-sibling loader pattern
(`bin/cctally` uses `Path(__file__).parent / "<name>.py"` to import
`_lib_*.py` and `_cctally_*.py` modules, so any install layout missing
those files crashes at import or first-use):

  1. `bin/_lib_*.py` runtime siblings must be listed in `package.json`
     `files[]` — otherwise `npm pack` excludes them and the late-loader
     in `bin/cctally` hits a missing path. v1.6.1 closed this leak for
     `_lib_share_templates.py`.

  2. Every path in `files[]` must classify as `public` against
     `.mirror-allowlist`. The npm-publish GHA workflow runs from the
     public clone, so anything filtered out by the mirror never reaches
     the tarball — regardless of `files[]`. `_lib_share_templates.py`
     was in `files[]` from v1.6.1 onward but classified as `unmatched`
     in the allowlist, so the v1.6.1 tarball still shipped without it.
     v1.6.2 closes the allowlist gap.

  3. `homebrew/cctally.rb.template` must install every runtime sibling
     into `libexec/bin` next to `bin/cctally`. The formula historically
     listed only `USER_FACING_BINS`; once `_lib_semver` became an EAGER
     import (bin/cctally:213 — the bin-split refactor), `cctally --help`
     itself crashes on a brew layout that omits siblings. Asserting a
     glob pattern (rather than per-name enumeration) lets future
     `_lib_*.py` / `_cctally_*.py` additions land without touching the
     formula.

The three checks are kept side-by-side so any future runtime sibling
addition trips every gate if any layer is misconfigured.
"""
import importlib.util
import json
import pathlib


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_classifier():
    """Load .githooks/_match.classify without installing _match as a package.

    Mirrors how `bin/cctally-mirror-public` loads the classifier — via
    importlib.util.spec_from_file_location — to keep the test free of
    any sys.path mutation.
    """
    p = REPO_ROOT / ".githooks" / "_match.py"
    spec = importlib.util.spec_from_file_location("_match_for_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.classify


def test_package_files_includes_every_lib_sibling():
    pkg = json.loads((REPO_ROOT / "package.json").read_text())
    files = set(pkg.get("files", []))
    libs = sorted(p.name for p in (REPO_ROOT / "bin").glob("_lib_*.py"))
    cctally_libs = sorted(p.name for p in (REPO_ROOT / "bin").glob("_cctally_*.py"))
    libs = sorted(set(libs) | set(cctally_libs))
    assert libs, (
        "expected at least one bin/_lib_*.py or bin/_cctally_*.py runtime module"
    )
    missing = [f"bin/{lib}" for lib in libs if f"bin/{lib}" not in files]
    assert not missing, (
        f"bin/_lib_*.py and bin/_cctally_*.py runtime modules missing from "
        f"package.json files[]: {missing}. Add them, or npm-installed cctally "
        f"will fail when bin/cctally tries to late-load them via "
        f"Path(__file__).parent / '_lib_*.py' or '_cctally_*.py'."
    )


def test_brew_formula_installs_every_lib_sibling():
    """Parallel guard to test_package_files: the brew formula must install
    every `bin/_lib_*.py` / `bin/_cctally_*.py` runtime sibling into
    `libexec/bin` next to `bin/cctally`, or `_load_sibling` (which resolves
    `Path(__file__).parent / "<name>.py"`) hits `FileNotFoundError`.

    Once `_lib_semver` became an EAGER import at bin/cctally:213, that path
    is no longer "only on first use of doctor/share/release" — it fires
    during `cctally --help`. A formula install missing any sibling crashes
    every command immediately.

    We accept either a `Dir.glob("bin/_lib_*.py", "bin/_cctally_*.py")`
    pattern (preferred — future-proof) or explicit per-name install lines.
    """
    template = (REPO_ROOT / "homebrew" / "cctally.rb.template").read_text()
    libs = sorted(
        {p.name for p in (REPO_ROOT / "bin").glob("_lib_*.py")}
        | {p.name for p in (REPO_ROOT / "bin").glob("_cctally_*.py")}
    )
    assert libs, (
        "expected at least one bin/_lib_*.py or bin/_cctally_*.py runtime module"
    )
    has_glob = (
        "bin/_lib_*.py" in template and "bin/_cctally_*.py" in template
    )
    if has_glob:
        return
    missing = [name for name in libs if name not in template]
    assert not missing, (
        f"bin/_lib_*.py / bin/_cctally_*.py runtime modules absent from "
        f"homebrew/cctally.rb.template: {missing}. Add them to the install "
        f"block, or switch to a Dir.glob pattern covering both "
        f"`bin/_lib_*.py` and `bin/_cctally_*.py`. Without these, a "
        f"brew-installed cctally crashes on `cctally --help` because "
        f"`_load_sibling('_lib_semver')` (bin/cctally:213) runs at import."
    )


def test_package_files_paths_are_public_in_mirror_allowlist():
    classify = _load_classifier()
    pkg = json.loads((REPO_ROOT / "package.json").read_text())
    files = pkg.get("files", [])
    # `dashboard/static/**` is a glob — feed an exemplar concrete path
    # to the classifier (the allowlist matches the glob). Plain files
    # pass through unchanged.
    probes = [
        "dashboard/static/dashboard.html" if f == "dashboard/static/**" else f
        for f in files
    ]
    result = classify(probes, allowlist_path=str(REPO_ROOT / ".mirror-allowlist"))
    not_public = result["private"] + result["unmatched"]
    assert not not_public, (
        f"package.json files[] paths NOT classified as public by "
        f".mirror-allowlist: {not_public}. The npm-publish workflow runs "
        f"from the public clone, so any file the mirror filters out is "
        f"absent from the tarball even if files[] lists it. Promote the "
        f"path in .mirror-allowlist, or drop it from files[]."
    )
