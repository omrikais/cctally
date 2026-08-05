"""Read-time cleaning of harness markup out of a Codex title (#463 S4 §5).

Pure kernel: one entry point, ``clean_codex_title(text) -> str``, and a CLOSED
allowlist of the grammars a census of the real store actually found.

**Read time, not ingest (D5).** The title is stored —
``codex_conversation_rollups.title`` is written at ingest and ``_rollup_fields``
returns it on its fast path — so repairing ``derive_title`` would heal nothing
for the conversations that are already wrong. Cleaning on the read path heals
all history with no migration and no reingest flag.

**The allowlist is a measurement, not a guess (§5.4).** Over the 438 stored
Codex rollup titles in the production store on 2026-08-04:

===========================================  =====  ===========
grammar                                      count  disposition
===========================================  =====  ===========
``[$name](<abs path>/SKILL.md) <rest>``        165  unwrap
``<command-name>…</command-name> …``            40  see below
``<recommended_plugins> …``                      6  strip
``<command-message>…</command-message> …``       1  see below
===========================================  =====  ===========

Nothing else occurred. No title carried a tag anywhere but at its head (0 of
438), and the two ``CODEX_TITLE_SKIP_PREFIXES`` wrappers
(``<environment_context>``, ``<user_instructions>``) appeared 0 times, because
they are skipped at ingest by a different mechanism that stays where it is
(§5.3).

Within the command wrapper the three tags are dispositioned separately, from
what their content actually looks like: ``command-name`` is the slash command
and is STRIPPED, while ``command-message`` and ``command-args`` carry the human
text and are UNWRAPPED. On the corpus that turns
``<command-name>/model</command-name> <command-message>model</command-message>
<command-args>fable</command-args>`` into ``model fable``.

``recommended_plugins`` never closes in the data: titles are capped at 120
characters, so the stored value is the head of a plugin catalogue. Stripping it
leaves nothing, and ``_display_chain`` falls through to the project label and
then to a short native thread id, which the chain already does.

**Closed, and deliberately so.** A general tag stripper would eat user-authored
angle brackets in a title. An unrecognized construct passes through BYTE for
byte — the function returns its input unchanged when no rule fires, so the 226
titles the census found clean, and every prose label this is applied to, cannot
move.
"""
from __future__ import annotations

import re

# `[$skill-name](/abs/path/to/SKILL.md)` — the Codex skill invocation. The
# prompt text after it is real, and the link target is a private filesystem path
# that leaks into every title surface. Byte-identical to the client's
# `cleanQualifiedTitle` regex, so the two agree on the same input and applying
# both is a no-op.
#
# NO trailing lookahead. The first version required whitespace or end of string
# after the closing paren, so `…/SKILL.md)Task B of issue #450.` — prompt text
# written straight against the paren — did not match and the whole link,
# absolute path included, reached the reader header and the outline rail. Two of
# 300 served titles in the test store carry that form. Nothing is lost by
# dropping the lookahead: the pattern is head-anchored (`pattern.match`) and its
# target is the literal `/SKILL.md)`, so it cannot start matching mid-title or
# consume any other Markdown link.
_SKILL_LINK_RE = re.compile(
    r"\[((?:\$)[^\]\r\n]+)\]\([^)\r\n]*/SKILL\.md\)")

_STRIP, _UNWRAP = "strip", "unwrap"

# Head-anchored, in match order. Each entry is (pattern, disposition, group) —
# `group` names the capture an `unwrap` keeps.
_GRAMMARS: tuple[tuple[re.Pattern, str, int], ...] = (
    (_SKILL_LINK_RE, _UNWRAP, 1),
    (re.compile(r"<command-name>(.*?)</command-name>", re.S), _STRIP, 1),
    (re.compile(r"<command-message>(.*?)</command-message>", re.S), _UNWRAP, 1),
    (re.compile(r"<command-args>(.*?)</command-args>", re.S), _UNWRAP, 1),
    # The closing form first: alternation is ordered, and the open-ended arm
    # would otherwise swallow a closed construct's tail.
    (re.compile(r"<recommended_plugins>.*?</recommended_plugins>"
                r"|<recommended_plugins>.*", re.S), _STRIP, 0),
)


def clean_codex_title(text) -> str:
    """The title with recognized leading harness markup removed or unwrapped.

    Returns the input unchanged when no grammar in the allowlist matches its
    head, including for a non-string or empty input, which keeps every
    untouched title and every prose label byte-stable.

    A construct that strips to nothing yields ``""``, and the caller's fallback
    chain takes over (§5.3).
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    kept: list[str] = []
    rest = text
    matched = False
    while rest:
        head = rest.lstrip()
        for pattern, disposition, group in _GRAMMARS:
            found = pattern.match(head)
            if found is None:
                continue
            matched = True
            if disposition == _UNWRAP:
                kept.append(found.group(group))
            rest = head[found.end():]
            break
        else:
            kept.append(head)
            break
    if not matched:
        return text
    return " ".join(" ".join(part.split()) for part in kept if part.strip()).strip()
