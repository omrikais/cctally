"""Guard: every bin/_lib_*.py runtime sibling must be in package.json files[].

Regression for v1.6.0 dashboard share GUI silently failing on npm
installs. The dashboard's /api/share/templates handler late-loads
bin/_lib_share_templates.py via Path(__file__).parent / "_lib_...".py;
when files[] omits a sibling, the npm tarball ships without it and the
lazy-loader's spec_from_file_location returns None on the missing path,
crashing the handler. Safari surfaces the killed connection as the
generic "Load failed" error message.

Latent since v1.4.0 (which added _lib_share for CLI --format) — the
fewer-trafficked CLI surface never tripped the gap. v1.6.0's dashboard
share button made it the default user path.
"""
import json
import pathlib


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


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
