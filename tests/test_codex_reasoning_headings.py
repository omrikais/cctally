"""#463 S2 §2.4 — the pure Codex reasoning-heading decomposition kernel.

The kernel takes the ordered ``text`` values of a reasoning aggregate's retained
``summary`` entries and returns the individual authored headings. It performs no
I/O, reads no database, and is deliberately separate from the stored reasoning
projection: that projection feeds ``_row_is_reasoning_title``, which is a
segmentation-boundary input, so changing it would move segment boundaries.
"""
from __future__ import annotations

import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from _lib_codex_reasoning_headings import (  # noqa: E402
    decompose_reasoning_headings,
)


def test_a_single_heading_entry_yields_one_heading():
    assert decompose_reasoning_headings(["**Inspecting git worktree usage**"]) == [
        "Inspecting git worktree usage"]


def test_a_multi_heading_entry_decomposes_in_order():
    entry = "**Planning concurrency test**\n**Designing monkeypatch**"
    assert decompose_reasoning_headings([entry]) == [
        "Planning concurrency test", "Designing monkeypatch"]


def test_separator_lines_are_discarded():
    entry = "**First**\n<!-- -->\n**Second**"
    assert decompose_reasoning_headings([entry]) == ["First", "Second"]


def test_a_mixed_entry_falls_back_to_the_entry_verbatim_including_separators():
    """§2.4: the fallback does not partially clean its input. A fallback that
    edits text is a second transformation nobody reviewed."""
    entry = "**First**\n<!-- -->\nplain prose line"
    assert decompose_reasoning_headings([entry]) == [entry]


def test_a_truncated_fragment_falls_back_verbatim():
    assert decompose_reasoning_headings(["**Planning"]) == ["**Planning"]


def test_entries_are_concatenated_in_order():
    assert decompose_reasoning_headings(["**A**", "**B**\n**C**"]) == ["A", "B", "C"]


def test_an_entry_of_only_separators_falls_back_verbatim():
    """At least one heading line must remain after discarding separators."""
    assert decompose_reasoning_headings(["<!-- -->"]) == ["<!-- -->"]


def test_empty_and_blank_entries_yield_nothing():
    assert decompose_reasoning_headings([]) == []
    assert decompose_reasoning_headings(["", "   "]) == []


def test_surrounding_whitespace_on_a_heading_line_is_stripped():
    assert decompose_reasoning_headings(["  **Indented heading**  "]) == [
        "Indented heading"]


def test_a_line_with_inner_markers_strips_only_the_outer_pair():
    """§2.4 defines a heading line as ``\\A\\*\\*([^\\n]+)\\*\\*\\Z`` — the same
    shape ``_REASONING_TITLE_RE`` recognises — and nothing more.

    ``**a** and **b**`` fullmatches that with greedy inner text ``a** and **b``,
    so it decomposes. This DIVERGES from ``_reasoning_projection``, which adds a
    guard rejecting a title containing ``**``. The divergence is deliberate and
    is why this test exists: the projection is a segmentation-boundary input and
    is frozen by S1's contract, while the decomposition is a read-time rendering
    concern, and §2.4 states the per-line rule without that guard. The coverage
    invariant still holds — re-wrapping the heading reproduces the source line
    exactly.
    """
    assert decompose_reasoning_headings(["**a** and **b**"]) == ["a** and **b"]


def test_a_non_string_entry_is_ignored_rather_than_raising():
    """The retained payload is provider data, so an entry may be anything. The
    kernel is TOTAL over its input so that no provider shape can raise inside
    detail assembly. The all-or-nothing rule of §2.3 is enforced one layer up,
    at the call site, which validates the whole entry list before calling."""
    assert decompose_reasoning_headings([None, 7, {"text": "x"}, "**A**"]) == ["A"]


_CORPUS_SHAPED_ENTRIES = [
    "**Inspecting git worktree usage**",
    "**Planning concurrency test**\n**Designing monkeypatch**",
    "**First**\n<!-- -->\n**Second**",
    "**One**\n**Two**\n**Three**\n**Four**",
    "**Planning",
    "**First**\n<!-- -->\nplain prose line",
    "<!-- -->",
    "**Reviewing the segment budget**\n<!-- -->\n**Deciding the boundary**\n"
    "<!-- -->\n**Recording the measurement**",
    "A plain paragraph with no heading at all.",
    "**a** and **b**",
]


def test_coverage_invariant_every_source_line_is_used_exactly_once():
    """§2.4's governing invariant: every line of the source aggregate is either a
    discarded separator in a decomposed entry, or appears in exactly one heading,
    exactly once. No line is dropped, none is duplicated, and no path both
    extracts headings and re-renders the original text."""
    decomposed = 0
    fallbacks = 0
    for entry in _CORPUS_SHAPED_ENTRIES:
        headings = decompose_reasoning_headings([entry])
        source_lines = [ln.strip() for ln in entry.split("\n") if ln.strip()]
        if headings == [entry]:
            fallbacks += 1
            continue
        decomposed += 1
        rebuilt = [f"**{h}**" for h in headings]
        non_separator = [ln for ln in source_lines if ln != "<!-- -->"]
        assert rebuilt == non_separator, entry
    # Non-vacuity: the corpus must exercise BOTH arms, or the loop above could
    # pass by taking one branch every time.
    assert decomposed >= 4 and fallbacks >= 4, (decomposed, fallbacks)
