"""Pure decomposition of a Codex reasoning aggregate into its authored headings.

#463 S2 §2.4. Codex writes reasoning as short bold headings. When an aggregate
holds several of them joined by newlines, ``_REASONING_TITLE_RE`` cannot
fullmatch the whole summary and the blob becomes one ``summary``, which the
reader renders as one clipped line. Measured over a real store: 5,081 of the
5,234 summary-only reasoning blocks are entirely bold-heading lines, holding
12,323 individual headings between them, and every one of those headings is
today invisible past the first thirty characters of the first one.

This module recovers the individual headings WITHOUT touching the stored
projection. That seam is load-bearing and is the trap most likely to be walked
into again: ``_row_is_reasoning_title`` reads the stored reasoning projection to
decide whether a row is a title boundary, and that is one of segmentation's two
semantic boundaries. Decomposing inside ``_reasoning_projection`` would change
which rows are boundaries and therefore move segment boundaries, which #463 S1's
contract forbids. Decomposition therefore runs at READ time, over the retained
payload's ``summary`` entries, and the stored ``title``, ``summary`` and ``body``
keep exactly today's values.

No I/O, no database, no imports beyond ``re``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# The same shape ``_lib_codex_conversation._REASONING_TITLE_RE`` recognises,
# applied per LINE rather than to the whole summary. Deliberately NOT carrying
# the stored projection's additional guard (which rejects a title containing
# ``**``): the projection is a frozen segmentation input, this is a read-time
# rendering concern, and §2.4 states the per-line rule without that guard.
_HEADING_LINE_RE = re.compile(r"\A\*\*([^\n]+)\*\*\Z")

# Codex interleaves this literal between headings in a minority of aggregates
# (123 of 5,234 measured). It is layout, not content, so a decomposed entry
# discards it.
_SEPARATOR_LINE = "<!-- -->"


def decompose_reasoning_headings(entries: Iterable[object]) -> list[str]:
    """Heading texts for one reasoning aggregate, in entry order.

    ``entries`` is the ordered ``text`` values of the retained payload's
    ``summary`` entries. The unit of decision is the ENTRY and the decision is
    all-or-nothing: an entry yields several headings only when, after discarding
    separator lines, every remaining line is a heading line and at least one
    remains. Otherwise it yields exactly one heading whose text is the entry
    VERBATIM, separator lines included — a fallback that partially cleaned its
    input would be a second transformation nobody reviewed.

    Coverage invariant (§2.4, and the test): every line of the source aggregate
    is either a discarded separator in a decomposed entry, or appears in exactly
    one heading, exactly once. No line is dropped, none is duplicated, and no
    path both extracts headings and re-renders the original text.

    Total over its input: a non-string entry is skipped rather than raised on, so
    no provider shape can fail detail assembly. The all-or-nothing rule for a
    malformed payload is enforced at the call site, which validates the whole
    entry list before calling.
    """
    out: list[str] = []
    for entry in entries or ():
        if not isinstance(entry, str) or not entry.strip():
            continue
        lines = [line.strip() for line in entry.split("\n") if line.strip()]
        kept = [line for line in lines if line != _SEPARATOR_LINE]
        matches = [_HEADING_LINE_RE.fullmatch(line) for line in kept]
        if kept and all(match is not None for match in matches):
            out.extend(match.group(1) for match in matches)
        else:
            out.append(entry)
    return out
