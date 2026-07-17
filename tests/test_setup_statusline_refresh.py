"""Setup-managed `statusLine.refreshInterval` (#311 spec D3).

Covers the three new setup helpers:
  - `_is_cctally_statusline_command` — the anchored recognizer (direct +
    wrapper forms) that decides whether a settings.json `statusLine.command`
    (or a legacy wrapper script it points at) runs `cctally statusline`.
  - `_classify_statusline_refresh` — the five-state classifier
    (unavailable/absent/foreign/present/missing) shared with doctor.
  - `_settings_merge_statusline_refresh_interval` — add-when-absent /
    never-mutate / never-remove.

Every path constant is pinned under a per-test tmp HOME via `redirect_paths`,
so wrapper-form file scans never touch the developer's real ~/.claude.
"""
import sys

import pytest

from conftest import load_script, redirect_paths

NPM_SHIM = "cctally-npm-shim.js"


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


# ── constant ───────────────────────────────────────────────────────────


def test_default_interval_is_30(app):
    import _cctally_core

    assert _cctally_core.STATUSLINE_REFRESH_INTERVAL_DEFAULT == 30


# ── recognizer: direct forms ────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "cctally statusline",
        "cctally statusline --config /some/path",
        "/Users/me/.local/bin/cctally statusline",
        "'/Users/My Name/.local/bin/cctally' statusline",
        "cctally claude statusline",
        "/opt/homebrew/bin/cctally claude statusline",
        "cctally-statusline",
        "/usr/local/bin/cctally-statusline",
        f"/usr/local/lib/node_modules/cctally/bin/{NPM_SHIM} statusline",
        "FOO=1 cctally statusline",
        "TZ=Etc/UTC BAR=x cctally statusline",
    ],
)
def test_recognizer_direct_positive(app, cmd):
    assert app._is_cctally_statusline_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "",
        "   ",
        "cctally",                       # no subcommand
        "cctally forecast",              # wrong subcommand
        "cctally forecast --status-line",
        "ccusage statusline",            # foreign renderer
        "echo cctally statusline",       # anchored: echo is the command token
        "printf '%s' cctally statusline",
        "cat /home/u/.claude/statusline-command.sh",  # cat is not a shell
        'cctally statusline "',          # malformed shlex (unbalanced quote)
        "node other.js statusline",      # node + non-shim script
        123,                             # non-string
    ],
)
def test_recognizer_direct_negative(app, cmd):
    assert app._is_cctally_statusline_command(cmd) is False


# ── recognizer: wrapper forms (legacy-path script + correlated content) ──


def _seed_legacy_script(app, monkeypatch, tmp_path, content):
    """Write a legacy statusline wrapper at the pinned legacy path and point
    LEGACY_STATUSLINE_PATHS at it. HOME is already tmp_path (redirect_paths)
    so `~`/`$HOME` spellings expand to this same file."""
    path = tmp_path / ".claude" / "statusline-command.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    monkeypatch.setitem(sys.modules["cctally"].__dict__,
                        "LEGACY_STATUSLINE_PATHS", (path,))
    return path


def test_recognizer_wrapper_shell_prefixed(app, monkeypatch, tmp_path):
    path = _seed_legacy_script(app, monkeypatch, tmp_path,
                               '#!/bin/bash\nexec cctally statusline "$@"\n')
    assert app._is_cctally_statusline_command(f"bash {path}") is True
    assert app._is_cctally_statusline_command(f"sh {path}") is True


def test_recognizer_wrapper_bare_path(app, monkeypatch, tmp_path):
    path = _seed_legacy_script(app, monkeypatch, tmp_path,
                               '#!/bin/bash\nexec cctally statusline "$@"\n')
    assert app._is_cctally_statusline_command(str(path)) is True


def test_recognizer_wrapper_home_spellings(app, monkeypatch, tmp_path):
    _seed_legacy_script(app, monkeypatch, tmp_path,
                        '#!/bin/bash\nexec cctally statusline "$@"\n')
    rel = ".claude/statusline-command.sh"
    assert app._is_cctally_statusline_command(f"bash ~/{rel}") is True
    assert app._is_cctally_statusline_command(f"bash $HOME/{rel}") is True
    assert app._is_cctally_statusline_command(f"bash ${{HOME}}/{rel}") is True


def test_recognizer_wrapper_piped_invocation(app, monkeypatch, tmp_path):
    # Real wrappers commonly pipe input into cctally: `... | cctally statusline`.
    path = _seed_legacy_script(app, monkeypatch, tmp_path,
                               '#!/bin/bash\ninput=$(cat)\n'
                               'printf "%s" "$input" | cctally statusline\n')
    assert app._is_cctally_statusline_command(f"bash {path}") is True


def test_recognizer_wrapper_variable_indirection(app, monkeypatch, tmp_path):
    # The real wrapper's `command -v`-guarded `_cct_bin=cctally` + later
    # `"$_cct_bin" statusline` (Codex R2 F1 / R3 variable indirection).
    path = _seed_legacy_script(
        app, monkeypatch, tmp_path,
        '#!/bin/bash\n'
        'command -v cctally >/dev/null 2>&1 && _cct_bin=cctally\n'
        'exec "$_cct_bin" statusline "$@"\n',
    )
    assert app._is_cctally_statusline_command(f"bash {path}") is True


def test_recognizer_wrapper_self_contained_literal(app, monkeypatch, tmp_path):
    # bin/cctally-statusline itself dispatches to `cctally statusline`, so a
    # self-contained `cctally-statusline` token needs no following subcommand
    # (Codex R3 F1).
    path = _seed_legacy_script(app, monkeypatch, tmp_path,
                               '#!/bin/bash\nexec cctally-statusline "$@"\n')
    assert app._is_cctally_statusline_command(f"bash {path}") is True


def test_recognizer_wrapper_self_contained_indirection(app, monkeypatch, tmp_path):
    path = _seed_legacy_script(
        app, monkeypatch, tmp_path,
        '#!/bin/bash\n_cct_bin=cctally-statusline\nexec "$_cct_bin" "$@"\n',
    )
    assert app._is_cctally_statusline_command(f"bash {path}") is True


def test_recognizer_wrapper_comment_only_needle(app, monkeypatch, tmp_path):
    # The only cctally-statusline mention is inside a comment → NOT recognized.
    path = _seed_legacy_script(
        app, monkeypatch, tmp_path,
        '#!/bin/bash\n# used to be: exec cctally statusline "$@"\n'
        'exec ccusage statusline "$@"\n',
    )
    assert app._is_cctally_statusline_command(f"bash {path}") is False


def test_recognizer_wrapper_mixed_foreign_not_correlated(app, monkeypatch, tmp_path):
    # cctally appears (forecast --status-line) AND statusline appears (ccusage),
    # but never CORRELATED — must NOT be recognized (Codex R2 F1).
    path = _seed_legacy_script(
        app, monkeypatch, tmp_path,
        '#!/bin/bash\ncctally forecast --status-line >/dev/null\n'
        'exec ccusage statusline "$@"\n',
    )
    assert app._is_cctally_statusline_command(f"bash {path}") is False


def test_recognizer_wrapper_non_legacy_path(app, monkeypatch, tmp_path):
    # A script that DOES invoke cctally statusline but is NOT at a known
    # legacy path → not recognized (the path anchor bounds the scan).
    _seed_legacy_script(app, monkeypatch, tmp_path,
                        '#!/bin/bash\nexec cctally statusline "$@"\n')
    other = tmp_path / "elsewhere" / "custom.sh"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('#!/bin/bash\nexec cctally statusline "$@"\n', encoding="utf-8")
    assert app._is_cctally_statusline_command(f"bash {other}") is False


def test_recognizer_wrapper_missing_file(app, monkeypatch, tmp_path):
    # Legacy path is configured but the file does not exist → not recognized.
    path = tmp_path / ".claude" / "statusline-command.sh"
    monkeypatch.setitem(sys.modules["cctally"].__dict__,
                        "LEGACY_STATUSLINE_PATHS", (path,))
    assert app._is_cctally_statusline_command(f"bash {path}") is False


# ── five-state classifier ───────────────────────────────────────────────


def test_classify_unavailable_from_none_sentinel(app):
    # The SetupError sentinel must be preserved as None → `unavailable`,
    # NOT coerced to {} (which would falsely read as `absent`).
    assert app._classify_statusline_refresh(None) == ("unavailable", None)


@pytest.mark.parametrize("settings", [{}, {"otherKey": 42}, {"hooks": {}}])
def test_classify_absent(app, settings):
    assert app._classify_statusline_refresh(settings) == ("absent", None)


@pytest.mark.parametrize(
    "block",
    [
        "not-a-dict",
        123,
        {"type": "ansi"},                                   # not a command block
        {"type": "command", "command": "ccusage statusline"},
        {"type": "command", "command": "echo hi"},
        {"type": "command"},                                # no command key
    ],
)
def test_classify_foreign(app, block):
    assert app._classify_statusline_refresh({"statusLine": block}) == ("foreign", None)


def test_classify_missing(app):
    s = {"statusLine": {"type": "command", "command": "cctally statusline"}}
    assert app._classify_statusline_refresh(s) == ("missing", None)


@pytest.mark.parametrize("value", [30, 5, 0, "fast", True, [1, 2], {"k": "v"}])
def test_classify_present_echoes_value_verbatim(app, value):
    s = {"statusLine": {"type": "command", "command": "cctally statusline",
                        "refreshInterval": value}}
    assert app._classify_statusline_refresh(s) == ("present", value)


# ── merge: add-when-absent / never-mutate / never-remove ─────────────────


def test_merge_adds_when_missing(app):
    s = {"statusLine": {"type": "command", "command": "cctally statusline"}}
    assert app._settings_merge_statusline_refresh_interval(s) is True
    assert s["statusLine"]["refreshInterval"] == 30


@pytest.mark.parametrize("value", [30, 5, "fast", 0])
def test_merge_never_mutates_present(app, value):
    s = {"statusLine": {"type": "command", "command": "cctally statusline",
                        "refreshInterval": value}}
    assert app._settings_merge_statusline_refresh_interval(s) is False
    assert s["statusLine"]["refreshInterval"] == value


def test_merge_noop_when_absent(app):
    s = {"otherKey": 1}
    assert app._settings_merge_statusline_refresh_interval(s) is False
    assert "statusLine" not in s  # never CREATES a block


def test_merge_noop_when_foreign(app):
    s = {"statusLine": {"type": "command", "command": "ccusage statusline"}}
    assert app._settings_merge_statusline_refresh_interval(s) is False
    assert "refreshInterval" not in s["statusLine"]


def test_merge_idempotent_double_install(app):
    s = {"statusLine": {"type": "command", "command": "cctally statusline"}}
    assert app._settings_merge_statusline_refresh_interval(s) is True   # adds
    assert app._settings_merge_statusline_refresh_interval(s) is False  # now present
    assert s["statusLine"]["refreshInterval"] == 30


def test_uninstall_leaves_statusline_untouched(app):
    # _settings_merge_uninstall removes cctally hook entries but must NEVER
    # touch the statusLine block or its refreshInterval.
    s = {
        "statusLine": {"type": "command", "command": "cctally statusline",
                       "refreshInterval": 30},
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "/abs/cctally hook-tick"}]}
            ]
        },
    }
    out, removed = app._settings_merge_uninstall(s)
    assert removed == 1
    assert out["statusLine"] == {"type": "command", "command": "cctally statusline",
                                 "refreshInterval": 30}
