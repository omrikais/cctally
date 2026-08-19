"""#620 S1 D6 / A6 — the Python half of the Cache Report verdict parity.

`dashboard/web/src/lib/cacheReportVerdict.ts` has shipped a four-state
verdict since #443 S2 (`anomalous | clean | partial | unevaluated`), while
the terminal marked only triggered rows and left the other three states
indistinguishable — an unmarked tick meant "evaluated and clean", "one
predicate skipped" and "nothing evaluated at all" alike.

The classification rule now lives in a pure Python kernel beside the
predicate classifier, and both languages are driven over ONE shared
truth-table file. `dashboard/web/__tests__/cacheReportVerdictParity.test.ts`
reads the same file; a duplicated vector list would be two truths rather
than a parity test.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _lib_cache_report as kernel  # noqa: E402


_VECTORS_PATH = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "cache-report-verdict-vectors.json"
)


def _vectors():
    data = json.loads(_VECTORS_PATH.read_text())
    return data["vectors"]


def test_the_truth_table_covers_every_required_case():
    """A6 — the vector file itself is asserted, so a future edit that drops a
    case fails here rather than quietly shrinking both suites at once."""
    vectors = _vectors()
    names = {v["name"] for v in vectors}
    assert len(names) == len(vectors), "vector names must be unique"

    expected_states = {v["expected"] for v in vectors}
    assert expected_states == {"anomalous", "clean", "partial", "unevaluated"}, (
        f"every one of the four states must appear; got {sorted(expected_states)}"
    )

    triggered = [v for v in vectors if v["input"]["anomaly_triggered"]]
    assert any(v["input"].get("anomaly_unevaluated") for v in triggered), (
        "triggered precedence needs at least one vector where a predicate was "
        "ALSO unevaluated, or precedence is never exercised"
    )
    assert any(
        len(v["input"].get("anomaly_unevaluated") or []) >= len(v["predicates"])
        for v in triggered
    ), "triggered must be shown to win over the fully-unevaluated state too"

    predicate_sets = {tuple(v["predicates"]) for v in vectors}
    assert ("net_negative", "cache_drop") in predicate_sets, "Claude's pair"
    assert ("cache_drop",) in predicate_sets, "Codex's single predicate"

    sizes = {len(v["input"].get("anomaly_unevaluated") or []) for v in vectors}
    assert {0} <= sizes, "a zero-unevaluated vector is required"
    assert any(s not in (0,) for s in sizes), "a some-unevaluated vector is required"

    assert any("observed" in v["input"] for v in vectors), (
        "the observed state must be exercised"
    )


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_kernel_matches_vectors(vector):
    """A6 — the Python kernel produces the expected state for every vector."""
    got = kernel.cache_row_verdict(
        triggered=vector["input"]["anomaly_triggered"],
        reasons=vector["input"].get("anomaly_reasons") or [],
        unevaluated=vector["input"].get("anomaly_unevaluated"),
        predicates=vector["predicates"],
        observed=vector["input"].get("observed"),
    )
    assert got.state == vector["expected"], (
        f"{vector['name']}: {vector['why']} — expected {vector['expected']}, "
        f"got {got.state}"
    )
    if "expected_observed" in vector:
        assert got.observed is vector["expected_observed"]


def test_the_kernel_default_predicate_set_is_claudes_pair():
    """Omitting `predicates` must mean the Claude pair, matching the
    TypeScript default, so a pre-#443-S2 envelope resolves identically on
    both sides."""
    got = kernel.cache_row_verdict(
        triggered=False, reasons=[], unevaluated=["cache_drop"],
    )
    assert got.state == "partial"
    assert kernel.CACHE_ANOMALY_PREDICATES == ("net_negative", "cache_drop")


# `_layout_cache_table` gives a left-aligned, non-expandable column a compact
# floor of 8 characters, and at the shipped 120-column default that floor is
# also the CEILING for the twelfth column: 120 minus 34 of border, 10 for
# Date, 12 for Models and 56 for the eight numeric columns leaves exactly 8.
_MAX_LABEL_WIDTH = 8


def test_every_state_has_a_worded_terminal_label():
    """D6 — the terminal renders words, not a bare glyph. Every state the
    kernel can return must have one, or a row would render blank."""
    seen = set()
    for state in ("anomalous", "clean", "partial", "unevaluated"):
        label = kernel.cache_row_verdict_label(state)
        assert label and label.strip(), state
        seen.add(label)
    assert len(seen) == 4, f"the four labels must be distinct; got {seen}"


def test_no_label_can_overflow_the_compact_column():
    """The labels must fit the width the layout can actually give them.

    A label longer than this truncates to an ellipsis at the shipped
    120-column default — `not eval` would become `not ev…` — and two states
    that truncate to the same prefix stop being distinguishable, which is the
    defect this column exists to remove. Asserting the width here means a
    future wording change fails on the arithmetic rather than on a golden
    nobody reads closely.
    """
    for state in ("anomalous", "clean", "partial", "unevaluated"):
        label = kernel.cache_row_verdict_label(state)
        assert len(label) <= _MAX_LABEL_WIDTH, (
            f"{state} label {label!r} is {len(label)} chars; the compact "
            f"column can render at most {_MAX_LABEL_WIDTH}"
        )
        assert label.isascii(), (
            f"{state} label {label!r} is not ASCII — the labels have no "
            "unicode/ASCII axis, so a non-ASCII one would render as-is on a "
            "terminal that cannot show it"
        )
    # Distinctness must survive truncation to the same width, not merely hold
    # at full length.
    truncated = {
        kernel.cache_row_verdict_label(s)[:_MAX_LABEL_WIDTH]
        for s in ("anomalous", "clean", "partial", "unevaluated")
    }
    assert len(truncated) == 4, truncated
