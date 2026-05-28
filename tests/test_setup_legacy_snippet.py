"""Pytest module for `_setup_detect_legacy_snippet` (issue #115).

The detector scans `LEGACY_STATUSLINE_PATHS` for the `cctally record-usage`
needle. A line that merely *references* the legacy command inside a shell
comment (e.g. a NOTE documenting the removal shipped by #86 Session G) must
NOT be treated as an executing snippet — only lines that actually run it.
"""
import pathlib

import pytest
from conftest import load_script


@pytest.fixture
def ns():
    """Fresh per-test cctally globals dict.

    Returned as the actual exec'd globals so monkeypatch.setitem(ns, ...)
    propagates to `_setup_detect_legacy_snippet`, which reads
    `LEGACY_STATUSLINE_PATHS` / `LEGACY_STATUSLINE_NEEDLE` via the
    call-time `_cctally()` accessor (== sys.modules["cctally"]).
    """
    return load_script()


def _write_statusline(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "statusline-command.sh"
    p.write_text(body, encoding="utf-8")
    return p


def _pin_paths(ns, monkeypatch, *paths):
    """Restrict the detector to exactly `paths` so the maintainer's real
    ~/.claude/statusline-command.sh can't bleed into the test."""
    monkeypatch.setitem(ns, "LEGACY_STATUSLINE_PATHS", tuple(paths))


def test_real_execution_line_detected(ns, monkeypatch, tmp_path):
    # No-regression guard: an actually-executing legacy line still fires.
    p = _write_statusline(
        tmp_path,
        "#!/bin/bash\n"
        '# Legacy status-line snippet (pre-hooks)\n'
        'exec cctally record-usage "$@"\n',
    )
    _pin_paths(ns, monkeypatch, p)
    result = ns["_setup_detect_legacy_snippet"]()
    assert result is not None
    path, lines = result
    assert path == p
    assert lines == [3]


def test_comment_only_line_not_detected(ns, monkeypatch, tmp_path):
    # The exact symptom from #115: the needle lives only inside a NOTE comment.
    p = _write_statusline(
        tmp_path,
        "#!/bin/bash\n"
        "# NOTE: the prior `cctally record-usage` background invocation was removed.\n"
        "# Persistence now flows via `cctally hook-tick` from the PostToolBatch hook.\n"
        'exec cctally statusline "$@"\n',
    )
    _pin_paths(ns, monkeypatch, p)
    assert ns["_setup_detect_legacy_snippet"]() is None


def test_indented_comment_not_detected(ns, monkeypatch, tmp_path):
    # First non-whitespace char is `#` even with leading indentation.
    p = _write_statusline(
        tmp_path,
        "#!/bin/bash\n"
        "if true; then\n"
        "    # cctally record-usage was here once\n"
        "    :\n"
        "fi\n",
    )
    _pin_paths(ns, monkeypatch, p)
    assert ns["_setup_detect_legacy_snippet"]() is None


def test_comment_and_real_line_reports_only_real(ns, monkeypatch, tmp_path):
    # A NOTE comment on line 2 PLUS a real execution line on line 3:
    # only the real line's number is reported.
    p = _write_statusline(
        tmp_path,
        "#!/bin/bash\n"
        "# NOTE: replaced the prior `cctally record-usage` background call.\n"
        'exec cctally record-usage "$@"\n',
    )
    _pin_paths(ns, monkeypatch, p)
    result = ns["_setup_detect_legacy_snippet"]()
    assert result is not None
    path, lines = result
    assert path == p
    assert lines == [3]
