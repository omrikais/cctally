"""One config value must yield one threshold on every read path (#443 S3 F17).

Today `_cctally_tui.py` coerces with `int(raw)` and accepts the string
"20" as 20, while `resolve_dashboard_source_semantics` requires an int
instance and falls back to 15 — so one setting renders different verdicts
on the Claude and Codex panels.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import conftest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _cctally_loaded():
    """`resolve_dashboard_source_semantics` reaches sys.modules['cctally']."""
    conftest.load_script()


def _dashboard_path_threshold(raw):
    """The Codex-path resolution, via resolve_dashboard_source_semantics."""
    import _cctally_dashboard_sources as srcs
    semantics = srcs.resolve_dashboard_source_semantics(
        {"cache_report": {"anomaly_threshold_pp": raw}},
        display_tz_name=None,
    )
    return semantics.cache_report_anomaly_threshold_pp


def _claude_path_threshold(raw):
    """The Claude-path resolution, as _cctally_tui.py performs it."""
    import _lib_cache_report as crk
    return crk.resolve_cache_report_threshold(raw)


@pytest.mark.parametrize("raw", ["20", 15.0, True, None, 0, 101, 20, -1, "abc"])
def test_both_read_paths_agree(raw):
    assert _claude_path_threshold(raw) == _dashboard_path_threshold(raw)


@pytest.mark.parametrize("raw,expected", [
    (20, 20), (1, 1), (100, 100),
    ("20", 15), (15.0, 15), (True, 15), (False, 15),
    (None, 15), (0, 15), (101, 15), (-1, 15), ("abc", 15),
])
def test_resolver_semantics_are_strict_and_silent(raw, expected):
    import _lib_cache_report as crk
    assert crk.resolve_cache_report_threshold(raw) == expected


def test_the_tui_read_path_uses_the_resolver():
    """The Claude panel must not keep its own `int(raw)` coercion.

    Non-vacuity for the parity test above: `_claude_path_threshold` calls
    the kernel directly, so it would keep agreeing with the Codex path
    even if `_cctally_tui.py` never adopted the resolver. This asserts
    the TUI builder actually routes through it.
    """
    source = (_BIN / "_cctally_tui.py").read_text(encoding="utf-8")

    # Scope to the cache-report build block rather than the whole 6,500-line
    # module: a future unrelated local named `threshold_raw` anywhere in the
    # file would otherwise fail this test for the wrong reason.
    start = source.index('cfg_cr = load_config().get("cache_report")')
    end = source.index("build_cache_report_snapshot", start)
    block = source[start:end]

    assert "resolve_cache_report_threshold" in block, (
        "the TUI cache-report build must resolve the threshold through "
        "_lib_cache_report.resolve_cache_report_threshold"
    )
    assert "int(" not in block, (
        "the TUI's own int()/range coercion of anomaly_threshold_pp must "
        "be gone — it is the site that read '20' as 20"
    )
