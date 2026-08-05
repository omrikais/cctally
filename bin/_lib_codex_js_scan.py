"""#463 S3 — a bounded lexical scanner for `tools.<name>(` in Codex programs.

Pure kernel: no I/O, nothing imported beyond ``re``.

Why this is a tokenizer and not a search. 17,777 uncarded `exec` calls, 51% of
the uncarded set, are JavaScript programs that declare constants, filter
`ALL_TOOLS`, await `Promise.all` and invoke `tools.exec_command`,
`tools.write_stdin` or other tools. The obvious way to card them is a textual
scan for `tools.<name>(`, and it is not acceptable:
`tests/test_codex_conversation_normalization.py` asserts that a call written
inside a string literal, inside a `//` comment and inside a regex literal each
decode to `None`, and a textual scanner would decode all three — fabricating
tool activity from a comment. A `complete: false` flag communicates omission; it
cannot make a false positive truthful.

So the source is walked ONCE through a small state machine that skips string
literals, template literals (including `${}` substitutions, which return to code
state), regex literals and both comment forms, and an invocation is recognized
only at a genuine token position whose member path is exactly `tools.<name>`.

It stays closed, bounded and non-executing. Lexing is not evaluation: nothing
here runs, resolves an identifier or follows a reference, so an aliased
`alias.exec_command(…)` and a computed `tools[key](…)` are both invisible by
construction, which is correct — they are not provably `tools.<name>`.

Anything that cannot be lexed cleanly returns ``None``, and the caller then
produces no card at all rather than a partial one.
"""
from __future__ import annotations

import re

# The same ceiling `decode_tool_call_card` already applies to a harness body.
# Duplicated as a literal rather than imported, because importing the decoder
# module here would be circular — that module imports this one.
_PARSE_CAP = 1_000_000

_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

# Anchored at a genuine `tools` token. Whitespace is tolerated around the dot and
# before the argument list because real programs wrap long chains across lines.
_MEMBER_CALL_RE = re.compile(
    r"tools\s*\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")

# A `/` opens a regex literal only when the previous significant token is one of
# these, or when it is the first token in the source. After a value — an
# identifier, a number, a string, `)` or `]` — a `/` is division. Getting this
# wrong in the permissive direction would swallow the rest of the program into a
# regex and silently lose every invocation after it.
_REGEX_ALLOWED_PUNCTUATION = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_ALLOWED_KEYWORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "case", "do", "else", "yield", "await", "throw",
})

# Sentinel `prev` value meaning "a completed value", after which `/` is division.
_VALUE = "\0value"

_DEFAULT = 0
_SINGLE = 1
_DOUBLE = 2
_TEMPLATE = 3
_LINE_COMMENT = 4
_BLOCK_COMMENT = 5
_REGEX = 6


def _regex_allowed(prev: str | None) -> bool:
    if prev is None:
        return True
    if prev == _VALUE:
        return False
    if len(prev) == 1:
        return prev in _REGEX_ALLOWED_PUNCTUATION
    return prev in _REGEX_ALLOWED_KEYWORDS


def scan_tool_invocations(
    source: str, *, limit: int,
) -> list[tuple[str, int]] | None:
    """``[(tool_name, arg_open_paren_index), …]``, or ``None``.

    ``None`` means the source could not be lexed cleanly — an unterminated
    string, template, regex or block comment, or a source past the parse cap —
    and the caller must then produce no card.

    An empty list means the source lexed but contains no `tools.<name>(` at a
    genuine token position. A call inside a literal or a comment is therefore
    indistinguishable from no call at all, which is the point.

    ``limit`` bounds how many invocations are COLLECTED, not how far the scan
    runs: the walk always continues to the end of the source, because an
    unterminated literal after the limit still means the source could not be
    lexed and no card may be built from it.
    """
    if not isinstance(source, str) or len(source) > _PARSE_CAP:
        return None
    found: list[tuple[str, int]] = []
    state = _DEFAULT
    prev: str | None = None
    brace_depth = 0
    template_returns: list[int] = []
    in_char_class = False
    index = 0
    size = len(source)
    while index < size:
        char = source[index]
        if state == _DEFAULT:
            if char in " \t\r\n":
                index += 1
                continue
            if char == "/" and index + 1 < size and source[index + 1] == "/":
                state = _LINE_COMMENT
                index += 2
                continue
            if char == "/" and index + 1 < size and source[index + 1] == "*":
                state = _BLOCK_COMMENT
                index += 2
                continue
            if char == "/":
                if _regex_allowed(prev):
                    state = _REGEX
                    in_char_class = False
                    index += 1
                    continue
                prev = "/"
                index += 1
                continue
            if char == "'":
                state = _SINGLE
                index += 1
                continue
            if char == '"':
                state = _DOUBLE
                index += 1
                continue
            if char == "`":
                state = _TEMPLATE
                index += 1
                continue
            if char == "{":
                brace_depth += 1
                prev = "{"
                index += 1
                continue
            if char == "}":
                if template_returns and brace_depth == template_returns[-1]:
                    template_returns.pop()
                    state = _TEMPLATE
                    prev = _VALUE
                    index += 1
                    continue
                brace_depth -= 1
                prev = "}"
                index += 1
                continue
            if _IDENT_START.match(char):
                # A `tools` token is only a member path when nothing binds to it
                # on the left. The test is the previous significant TOKEN, not
                # the character immediately before `tools`: JavaScript permits
                # whitespace and newlines around `.`, so `evil . tools.x` and
                # `evil.\n  tools.x` are member accesses on `evil` even though
                # the character before `tools` is a space. `prev` is unchanged by
                # the whitespace branch above, so it still holds the `.`.
                if prev != ".":
                    match = _MEMBER_CALL_RE.match(source, index)
                    if match is not None:
                        if len(found) < limit:
                            found.append((match.group(1), match.end() - 1))
                        prev = "("
                        index = match.end()
                        continue
                word = _IDENT_RE.match(source, index)
                prev = word.group(0)
                index = word.end()
                continue
            if char.isdigit():
                index += 1
                prev = _VALUE
                continue
            prev = char
            index += 1
            continue
        if state in (_SINGLE, _DOUBLE):
            if char == "\\":
                index += 2
                continue
            if (state == _SINGLE and char == "'") or (state == _DOUBLE and char == '"'):
                state = _DEFAULT
                prev = _VALUE
                index += 1
                continue
            if char == "\n":
                return None              # an unterminated ordinary string literal
            index += 1
            continue
        if state == _TEMPLATE:
            if char == "\\":
                index += 2
                continue
            if char == "`":
                state = _DEFAULT
                prev = _VALUE
                index += 1
                continue
            if char == "$" and index + 1 < size and source[index + 1] == "{":
                template_returns.append(brace_depth)
                state = _DEFAULT
                prev = "{"
                index += 2
                continue
            index += 1
            continue
        if state == _LINE_COMMENT:
            if char == "\n":
                state = _DEFAULT
            index += 1
            continue
        if state == _BLOCK_COMMENT:
            if char == "*" and index + 1 < size and source[index + 1] == "/":
                state = _DEFAULT
                index += 2
                continue
            index += 1
            continue
        # _REGEX
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_char_class = True
            index += 1
            continue
        if char == "]":
            in_char_class = False
            index += 1
            continue
        if char == "/" and not in_char_class:
            state = _DEFAULT
            prev = _VALUE
            index += 1
            continue
        if char == "\n":
            return None                  # an unterminated regex literal
        index += 1
    if state != _DEFAULT and state != _LINE_COMMENT:
        return None                      # unterminated literal or block comment
    if template_returns:
        return None                      # unterminated `${` substitution
    return found


__all__ = ["scan_tool_invocations"]
