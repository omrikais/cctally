from __future__ import annotations

import json
import pathlib
import re
import sys

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from _lib_codex_find_projection import (  # noqa: E402
    FindRange,
    ProjectedLeaf,
    RenderLeaf,
    literal_ranges,
    project_context,
    project_markdown,
    project_plain,
    regex_ranges,
    slice_range_to_leaves,
)


CASES = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "codex-find-projection" / "cases.json").read_text()
)


def _projection_value(projected):
    text, leaves = projected
    return {
        "text": text,
        "leaves": [
            {"key": leaf.key, "start": leaf.start, "end": leaf.end}
            for leaf in leaves
        ],
    }


def test_shared_projection_cases_match_hand_checked_visible_text():
    for case in CASES["projectionCases"]:
        if case["kind"] == "markdown":
            actual = project_markdown(case["source"])
        elif case["kind"] == "context":
            actual = project_context(case["source"])
        else:
            actual = project_plain(tuple(RenderLeaf(**leaf) for leaf in case["leaves"]))
        assert _projection_value(actual) == case["expected"], case["name"]


def test_shared_literal_cases_are_leftmost_non_overlapping_scalar_ranges():
    for case in CASES["literalCases"]:
        actual = literal_ranges(
            case["text"], case["query"], case_sensitive=case["caseSensitive"]
        )
        assert [vars(match) for match in actual] == case["expected"], case["name"]


def test_shared_regex_cases_use_python_and_omit_zero_width_matches():
    for case in CASES["regexCases"]:
        flags = 0 if case["caseSensitive"] else re.IGNORECASE
        actual = regex_ranges(case["text"], re.compile(case["pattern"], flags))
        assert [vars(match) for match in actual] == case["expected"], case["name"]


def test_shared_slice_cases_address_every_leaf_fragment():
    for case in CASES["sliceCases"]:
        actual = slice_range_to_leaves(
            FindRange(**case["match"]),
            tuple(ProjectedLeaf(**leaf) for leaf in case["leaves"]),
        )
        assert [vars(fragment) for fragment in actual] == case["expected"], case["name"]
