"""Thin bash-wrapper conformance (#279 S6 W4, spec gate F13).

Every `bin/cctally-<cmd>` thin wrapper must be the canonical shape: a bash
shebang on line 1, `set -euo pipefail`, and a final
`exec "$(dirname "$0")/cctally" <argv> "$@"` line. The wrapper->argv map is
EXPLICIT — it records that `cctally-dollar-per-percent` deliberately execs
`report --sync-current` (not a same-named subcommand); a same-name assumption
would reject valid behavior or tempt removal of `--sync-current`.

Deliberate non-conformers are excluded from the census: the `*-test`
harnesses, `cctally-test-all` (`set -uo pipefail` intentionally), the
non-bash entry points (`cctally-bench`/`cctally-mirror-public`/`cctally-release`
are python; the `*.js` shims are node), and `cctally-preview` (not a thin
wrapper).
"""
import pathlib

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"

WRAPPER_ARGV = {
    "cctally-alerts": "alerts",
    "cctally-budget": "budget",
    "cctally-dashboard": "dashboard",
    "cctally-dollar-per-percent": "report --sync-current",
    "cctally-five-hour-blocks": "five-hour-blocks",
    "cctally-five-hour-breakdown": "five-hour-breakdown",
    "cctally-forecast": "forecast",
    "cctally-project": "project",
    "cctally-refresh-usage": "refresh-usage",
    "cctally-statusline": "statusline",
    "cctally-sync-week": "sync-week",
    "cctally-tui": "tui",
    "cctally-update": "update",
}


def test_every_thin_wrapper_conforms():
    for name, argv in WRAPPER_ARGV.items():
        path = BIN / name
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        assert lines[0] == "#!/usr/bin/env bash", name
        assert "set -euo pipefail" in text, f"{name} missing set -euo pipefail"
        assert lines[-1] == f'exec "$(dirname "$0")/cctally" {argv} "$@"', name
        assert path.stat().st_mode & 0o111, f"{name} not executable"


def test_map_is_complete():
    thin = set()
    for p in BIN.glob("cctally-*"):
        if p.name.endswith("-test") or p.name in (
            "cctally-test-all", "cctally-preview", "cctally-bench",
            "cctally-mirror-public", "cctally-release",
        ):
            continue
        head = p.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if "bash" in head:
            thin.add(p.name)
    assert thin == set(WRAPPER_ARGV), thin.symmetric_difference(set(WRAPPER_ARGV))
