"""#463 S1 — the pure segmentation kernel (spec sections 1 and 2).

`plan_segments` divides one canonical response item into bounded runs of fold
groups. The properties under test are the ones sessions S2 through S5 are told
they may rely on:

  * a fold group is atomic and is never divided across segments;
  * a group that exceeds the budget on its own becomes its own segment, which is
    what makes the budget a target with a stated ceiling rather than a hard cap;
  * a semantic boundary inside the last quarter of the budget closes the segment
    early, and the lookback floor keeps that from producing a run of tiny
    segments;
  * filling greedily from the start of the turn is what makes an append leave
    earlier segment anchors alone.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_segments as seg  # noqa: E402
import _lib_codex_conversation_query as q  # noqa: E402

plan_segments = seg.plan_segments


_row_counter = 0


def _group(*, blocks: int, is_title_boundary: bool = False,
           is_tool_transition: bool = False, source_bytes: int | None = None):
    """One fold group carrying `blocks` blocks over `blocks` synthetic rows."""
    global _row_counter
    rows = []
    for _ in range(max(1, blocks)):
        _row_counter += 1
        rows.append(f"row-{_row_counter}")
    return seg.FoldGroup(
        rows=rows, block_count=blocks,
        source_bytes=blocks * 100 if source_bytes is None else source_bytes,
        is_title_boundary=is_title_boundary,
        is_tool_transition=is_tool_transition)


def _all_groups(segments):
    return [group for segment in segments for group in segment.groups]


def test_a_fold_group_is_never_split_across_segments():
    groups = [_group(blocks=30), _group(blocks=25), _group(blocks=10)]
    segments = plan_segments(groups, block_budget=40)
    for segment in segments:
        for group in segment.groups:
            assert group in groups  # whole groups only, never partial


def test_every_group_appears_exactly_once_and_in_order():
    groups = [_group(blocks=7) for _ in range(23)]
    segments = plan_segments(groups, block_budget=40)
    assert _all_groups(segments) == groups


def test_a_group_larger_than_the_budget_becomes_its_own_segment():
    groups = [_group(blocks=10), _group(blocks=95), _group(blocks=10)]
    segments = plan_segments(groups, block_budget=40)
    oversized = [s for s in segments if s.block_count > 40]
    assert len(oversized) == 1
    assert len(oversized[0].groups) == 1


def test_the_snap_prefers_a_title_boundary_inside_the_lookback():
    groups = [_group(blocks=10) for _ in range(3)]
    groups.append(_group(blocks=5, is_title_boundary=True))
    groups.append(_group(blocks=5))
    segments = plan_segments(groups, block_budget=40)
    assert segments[0].block_count == 30, "closed before the title group"


def test_the_snap_never_shrinks_a_segment_below_75_percent_of_budget():
    groups = [_group(blocks=2, is_title_boundary=True) for _ in range(40)]
    segments = plan_segments(groups, block_budget=40)
    for segment in segments[:-1]:
        assert segment.block_count >= 30


def test_a_boundary_below_the_lookback_floor_is_ignored():
    """A title group early in the segment must not close it at 10 blocks."""
    groups = [_group(blocks=10),
              _group(blocks=10, is_title_boundary=True),
              _group(blocks=10),
              _group(blocks=10),
              _group(blocks=10)]
    segments = plan_segments(groups, block_budget=40)
    assert segments[0].block_count == 40


def test_a_tool_transition_closes_only_when_no_title_is_in_the_window():
    groups = [_group(blocks=10) for _ in range(3)]
    groups.append(_group(blocks=5, is_tool_transition=True))
    groups.append(_group(blocks=5))
    segments = plan_segments(groups, block_budget=40)
    assert segments[0].block_count == 30


def test_a_title_beats_a_tool_transition_earlier_in_the_window():
    groups = [_group(blocks=10) for _ in range(3)]
    groups.append(_group(blocks=5, is_tool_transition=True))
    groups.append(_group(blocks=5, is_title_boundary=True))
    segments = plan_segments(groups, block_budget=40)
    # The tool transition sits at 30 blocks and the title at 35; both are inside
    # the window, and the title wins on priority even though it is later.
    assert segments[0].block_count == 35


def test_appending_a_group_leaves_earlier_segment_anchors_unchanged():
    groups = [_group(blocks=10) for _ in range(9)]
    before = [s.anchor_row for s in plan_segments(groups, block_budget=40)]
    after = [s.anchor_row for s in plan_segments(groups + [_group(blocks=10)],
                                                 block_budget=40)]
    assert after[:len(before) - 1] == before[:len(before) - 1]


def test_segment_zero_has_ordinal_zero_and_anchors_on_the_first_row():
    groups = [_group(blocks=10) for _ in range(9)]
    segments = plan_segments(groups, block_budget=40)
    assert segments[0].ordinal == 0
    assert segments[0].anchor_row is groups[0].rows[0]
    assert [s.ordinal for s in segments] == list(range(len(segments)))


def test_source_bytes_accumulate_per_segment():
    groups = [_group(blocks=10, source_bytes=1000) for _ in range(4)]
    segments = plan_segments(groups, block_budget=40)
    assert len(segments) == 1
    assert segments[0].source_bytes == 4000


def test_no_groups_yields_no_segments():
    assert plan_segments([], block_budget=40) == []


def test_a_zero_or_negative_budget_is_rejected():
    with pytest.raises(ValueError):
        plan_segments([_group(blocks=1)], block_budget=0)


# ── the segment key shape (spec section 1, "Key derivation") ─────────────────


def test_a_segment_key_never_equals_the_row_key_for_the_same_row():
    row_key = q.codex_item_key(
        "conv-x", klass="prompt", turn_id=None,
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    segment_key = q.codex_item_key(
        "conv-x", klass="segment", turn_id=None,
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    assert segment_key != row_key


def test_a_segment_key_never_equals_the_block_key_for_the_same_row():
    block_key = q.codex_block_key(
        "conv-x", source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    segment_key = q.codex_item_key(
        "conv-x", klass="segment", turn_id=None,
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    assert segment_key != block_key


def test_a_segment_key_is_ordinal_free_and_stable_per_anchor_row():
    first = q.codex_item_key(
        "conv-x", klass="segment", turn_id="turn-a",
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    # The turn id is deliberately NOT an input: hashing the anchor row alone is
    # what keeps the key stable while the row's content stays at that offset.
    second = q.codex_item_key(
        "conv-x", klass="segment", turn_id="turn-b",
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    assert first == second
    moved = q.codex_item_key(
        "conv-x", klass="segment", turn_id="turn-a",
        source_path="/p/a.jsonl", line_offset=8, content_digest="d")
    replaced = q.codex_item_key(
        "conv-x", klass="segment", turn_id="turn-a",
        source_path="/p/a.jsonl", line_offset=7, content_digest="d2")
    assert moved != first and replaced != first


def test_a_segment_key_never_leaks_a_raw_path():
    key = q.codex_item_key(
        "conv-x", klass="segment", turn_id=None,
        source_path="/secret/dir/private.jsonl", line_offset=1,
        content_digest="d")
    assert "/secret/" not in key and "private.jsonl" not in key


def test_segment_zero_inherits_the_turn_key_unchanged():
    """The inheritance rule that makes every already-issued deep link resolve.

    Segment 0 is NOT given a "seg" key — it keeps the turn's "response" key,
    which is computed from the turn identifier alone and never referenced a row,
    so splitting a turn cannot move it.
    """
    turn_key = q.codex_item_key(
        "conv-x", klass="response", turn_id="turn-a",
        source_path=None, line_offset=None, content_digest=None)
    item = {"klass": "response", "turn_id": "turn-a", "anchor_row": None}
    assert q._item_key_for_item("conv-x", item) == turn_key
    later = q.codex_item_key(
        "conv-x", klass="segment", turn_id="turn-a",
        source_path="/p/a.jsonl", line_offset=7, content_digest="d")
    assert later != turn_key
