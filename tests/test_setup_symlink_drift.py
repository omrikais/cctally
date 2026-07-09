"""#279 S1 F2 — every user-facing thin wrapper must be in SETUP_SYMLINK_NAMES.

`bin/cctally-budget` existed, executable, since May 29 but was never added to
`SETUP_SYMLINK_NAMES`, so `cctally setup` / `repair-symlinks` never linked it.
The wrapper set and the tuple are hand-maintained with no cross-check; this
guard discovers thin wrappers on disk and asserts set-equality with the tuple.
"""
import re
import stat
import pathlib

# A thin wrapper `exec`s the sibling `cctally` binary with a fixed subcommand.
# Two on-disk forms exist and both count: the inline
# `exec "$(dirname "$0")/cctally" <sub>` and cctally-tui's two-line
# `DIR=...; exec "$DIR/cctally" <sub>`. `[^\n]*` spans the nested quotes of the
# inline form; maintainer tools (release/mirror-public/test-all/bench/preview)
# never exec `.../cctally <subcommand>`, so they don't match.
_THIN_WRAPPER = re.compile(r'exec\s+"[^\n]*/cctally"\s+[a-z][a-z-]+')


def test_every_thin_wrapper_is_in_setup_symlink_names(cctally_module):
    repo = pathlib.Path(__file__).resolve().parents[1]
    wrappers = set()
    for p in sorted((repo / "bin").glob("cctally-*")):
        if p.name.endswith("-test") or p.suffix == ".js" or not p.is_file():
            continue
        if not (p.stat().st_mode & stat.S_IXUSR):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _THIN_WRAPPER.search(text):
            wrappers.add(p.name)
    names = set(cctally_module.SETUP_SYMLINK_NAMES)
    assert "cctally" in names
    assert wrappers == names - {"cctally"}, (
        "bin/ thin wrappers and SETUP_SYMLINK_NAMES drifted: "
        f"missing from tuple={sorted(wrappers - names)}, "
        f"in tuple but not on disk={sorted((names - {'cctally'}) - wrappers)}"
    )
