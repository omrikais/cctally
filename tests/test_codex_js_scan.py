"""#463 S3 — the bounded lexical scanner for Codex `js`/`exec` programs.

This module encodes the P0 the pre-plan review gate raised. 17,777 uncarded
`exec` calls, 51% of the uncarded set, are JavaScript programs rather than shell
commands, and the obvious way to card them is a textual scan for `tools.<name>(`.
That is not acceptable, and `tests/test_codex_conversation_normalization.py`
already says so: it asserts that a call written inside a string literal, inside a
`//` comment and inside a regex literal each decode to `None`, and a textual
scanner would decode all three — fabricating tool activity from a comment. A
`complete: false` flag communicates omission; it cannot make a false positive
truthful.

So the scanner walks the source once, skipping string literals, template
literals, regex literals and both comment forms, and recognizes an invocation
only at a genuine token position whose member path is exactly `tools.<name>`.
Lexing is not evaluation: nothing here executes, resolves an identifier, or
follows a reference.
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _lib_codex_js_scan import scan_tool_invocations  # noqa: E402


def test_scanner_ignores_calls_inside_literals_and_comments():
    for source in [
        'text("tools.exec_command({cmd: \\"inside string\\"})");',
        "// tools.exec_command({cmd: 'inside comment'})",
        '/* tools.exec_command({cmd: "inside block comment"}) */',
        'const pattern = /tools.exec_command\\(x\\)/;',
        'const t = `tools.exec_command(${x})`;',
        "const s = 'nested \\'quote\\' tools.exec_command(1)';",
        'const r = /a"b/; // tools.exec_command(2)',
    ]:
        assert scan_tool_invocations(source, limit=8) == [], source


def test_scanner_rejects_lookalike_member_paths():
    for source in [
        'const r = await evil.tools.exec_command({cmd: "nope"});',
        # JavaScript permits whitespace and newlines around `.`, so the guard
        # must key on the previous significant TOKEN and not on the character
        # immediately before `tools`. Each of these is a member access on some
        # other object, and decoding it would fabricate tool activity.
        'const r = await evil . tools.exec_command({cmd: "nope"});',
        'const r = await evil.\n  tools.exec_command({cmd: "nope"});',
        'const r = await this . tools.exec_command({cmd: "nope"});',
        'const r = await evil?.tools.exec_command({cmd: "nope"});',
        'const r = await evil\n  .\n  tools.exec_command({cmd: "nope"});',
    ]:
        assert scan_tool_invocations(source, limit=8) == [], source


def test_scanner_finds_real_invocations_around_other_statements():
    source = ('const names = ALL_TOOLS.filter(x => /gh/.test(x.name));\n'
              'const r = await tools.exec_command({cmd: "ls"});\n'
              'await Promise.all([tools.write_stdin({session_id: 7, chars: "y"})]);\n')
    found = [name for name, _ in scan_tool_invocations(source, limit=8)]
    assert found == ["exec_command", "write_stdin"]


def test_scanner_refuses_unlexable_source():
    assert scan_tool_invocations('const s = "unterminated', limit=8) is None
    assert scan_tool_invocations('/* unclosed', limit=8) is None


# ── the four adversarial cases spec section 3.3 adds ─────────────────────────


def test_scanner_handles_nested_quotes_regex_quotes_comment_quotes_and_templates():
    for source in [
        # A double-quoted string holding an escaped double quote AND an apostrophe.
        'const s = "he said \\"tools.exec_command(1)\\" and it\'s fine";',
        # A regex literal containing a quote character, which must not open a
        # string state that would then swallow the rest of the program.
        'const r = /"/; const t = "tools.exec_command(2)";',
        # A comment containing an unbalanced quote, same hazard.
        "// it's a comment about tools.exec_command(3)\nconst x = 1;",
        # A template literal whose substitution itself contains a string.
        'const t = `a${"tools.exec_command(4)"}b`;',
    ]:
        assert scan_tool_invocations(source, limit=8) == [], source


def test_scanner_returns_the_open_paren_index_of_each_invocation():
    source = 'const r = await tools.exec_command({cmd: "ls"});'
    found = scan_tool_invocations(source, limit=8)
    assert len(found) == 1
    name, index = found[0]
    assert name == "exec_command"
    assert source[index] == "("
    assert source[index:index + 2] == "({"


def test_scanner_finds_a_call_inside_another_calls_arguments():
    """Arguments are lexed too, so a nested invocation is not lost."""
    source = 'await Promise.all([tools.wait({cell_id: 1}), tools.wait({cell_id: 2})]);'
    assert [name for name, _ in scan_tool_invocations(source, limit=8)] == [
        "wait", "wait"]


def test_scanner_tolerates_whitespace_inside_the_member_path():
    source = 'const r = await tools\n  .exec_command\n  ({cmd: "ls"});'
    assert [name for name, _ in scan_tool_invocations(source, limit=8)] == [
        "exec_command"]


def test_scanner_distinguishes_division_from_a_regex_literal():
    # `a / b` is division; treating the first `/` as a regex would swallow the
    # real invocation that follows it on the next line.
    source = ('const ratio = total / count / 2;\n'
              'const r = await tools.exec_command({cmd: "ls"});\n')
    assert [name for name, _ in scan_tool_invocations(source, limit=8)] == [
        "exec_command"]


def test_scanner_stops_collecting_at_the_limit_but_still_lexes_to_the_end():
    source = "".join(f'tools.wait({{cell_id: {i}}});\n' for i in range(12))
    found = scan_tool_invocations(source, limit=3)
    assert len(found) == 3
    # An unterminated literal AFTER the limit is still detected, because the
    # scan does not stop early — a card must never be built from a source the
    # scanner could not lex cleanly.
    assert scan_tool_invocations(source + 'const s = "oops', limit=3) is None


def test_scanner_refuses_an_oversized_source():
    from _lib_codex_conversation import _CARD_HARNESS_PARSE_CAP
    assert scan_tool_invocations("x" * (_CARD_HARNESS_PARSE_CAP + 1), limit=8) is None


def test_scanner_never_evaluates_and_never_resolves_a_reference():
    """Lexing is permitted; evaluating source is not.

    A member path that only RESOLVES to `tools` at run time is not `tools`, and
    the scanner must not follow it. The argument is likewise never evaluated —
    that is `_HarnessLiteralParser`'s job, and it refuses non-literals.
    """
    for source in [
        'const alias = tools; const r = await alias.exec_command({cmd: "ls"});',
        'const r = await globalThis.tools.exec_command({cmd: "ls"});',
        'const r = await this.tools.exec_command({cmd: "ls"});',
        'const key = "exec_command"; const r = await tools[key]({cmd: "ls"});',
    ]:
        assert scan_tool_invocations(source, limit=8) == [], source
