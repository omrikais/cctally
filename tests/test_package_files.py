"""Guards over the cctally npm distribution surface.

Two related invariants kept the dashboard share GUI broken on npm
installs across v1.6.0 and v1.6.1:

  1. `bin/_lib_*.py` runtime siblings must be listed in `package.json`
     `files[]` — otherwise `npm pack` excludes them and the late-loader
     in `bin/cctally` (`Path(__file__).parent / "_lib_..."`) hits a
     missing path. v1.6.1 closed this leak for `_lib_share_templates.py`.

  2. Every path in `files[]` must classify as `public` against
     `.mirror-allowlist`. The npm-publish GHA workflow runs from the
     public clone, so anything filtered out by the mirror never reaches
     the tarball — regardless of `files[]`. `_lib_share_templates.py`
     was in `files[]` from v1.6.1 onward but classified as `unmatched`
     in the allowlist, so the v1.6.1 tarball still shipped without it.
     v1.6.2 closes the allowlist gap.

The two checks are kept side-by-side so any future runtime sibling
addition trips BOTH gates if either layer is misconfigured.
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
    assert libs, "expected at least one bin/_lib_*.py runtime module"
    missing = [f"bin/{lib}" for lib in libs if f"bin/{lib}" not in files]
    assert not missing, (
        f"bin/_lib_*.py runtime modules missing from package.json files[]: "
        f"{missing}. Add them, or npm-installed cctally will fail when "
        f"bin/cctally tries to late-load them via "
        f"Path(__file__).parent / '_lib_*.py'."
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
