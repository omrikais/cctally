"""#463 S1 (F4) — Codex reverse-paging parity with the Claude kernel.

A named parity port of ``tests/test_conversation_pagination.py`` written against
``_lib_codex_conversation_query._paginate_items``. No test anywhere exercised
Codex ``before`` before this file, which is how the defect stayed latent: the
kernel computed ``lo``/``hi`` and then sliced, so a ``before`` request returned
``items[0:hi][:limit]`` — the conversation's OPENING items — and reported
``has_before`` as False.

``fetchPrev`` can only fire when ``has_before`` is true, which on Codex had
never happened, because the corpus maximum of 495 items always fit inside one
500-item page. Segmentation makes ``has_before`` true routinely, which is why
this is fixed in the session that introduces segmentation rather than later.

Two Codex-only divergences from the Claude kernel are pinned here as tests
rather than left to a comment, because a later literal re-port would silently
break both:

  * ``limit == 0`` means UNBOUNDED. ``get_codex_conversation_export`` passes it.
    Claude's default branch computes ``end = min(limit, N)``, which for zero
    yields an empty page — every Codex export would lose its conversation items.
  * cursor resolution covers ``member_item_keys`` aliases as well as primary
    item keys, so a cursor naming an item folded by a later contract version
    still resolves. Claude's ``_idx`` checks only its primary anchor id.
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_conversation_query as q  # noqa: E402
import _lib_codex_segments as segkern  # noqa: E402


def _items(n: int) -> list[dict]:
    return [{"item_key": f"i{i:02d}", "member_item_keys": []} for i in range(n)]


def _keys(window: list[dict]) -> list[str]:
    return [item["item_key"] for item in window]


def test_before_returns_preceding_page():
    window, page = q._paginate_items(
        _items(10), after=None, before="i08", tail=False, limit=2)
    assert _keys(window) == ["i06", "i07"]
    assert page["has_before"] is True
    assert page["has_after"] is True


def test_before_short_head_page_still_reports_no_previous():
    window, page = q._paginate_items(
        _items(10), after=None, before="i01", tail=False, limit=5)
    assert _keys(window) == ["i00"]
    assert page["has_before"] is False
    # Truncating at the head does not remove the newer items the reader can
    # still page DOWN to (the Claude kernel's P2-8 contract).
    assert page["has_after"] is True


def test_page_up_then_down_reconstructs_full_list():
    items = _items(10)
    seen: list[str] = []
    cursor = None
    while True:
        window, page = q._paginate_items(
            items, after=None, before=cursor, tail=(cursor is None), limit=3)
        seen = _keys(window) + seen
        if not page["has_before"]:
            break
        cursor = page["before"]
    assert seen == _keys(items)


def test_stale_before_cursor_returns_empty_page():
    window, page = q._paginate_items(
        _items(10), after=None, before="nope", tail=False, limit=3)
    assert window == []
    assert page["returned"] == 0
    assert page["has_before"] is False
    assert page["has_after"] is False


def test_stale_after_cursor_returns_empty_page():
    window, page = q._paginate_items(
        _items(10), after="nope", before=None, tail=False, limit=3)
    assert window == []
    assert page["returned"] == 0


def test_after_returns_following_page():
    window, page = q._paginate_items(
        _items(10), after="i02", before=None, tail=False, limit=3)
    assert _keys(window) == ["i03", "i04", "i05"]
    assert page["has_before"] is True
    assert page["has_after"] is True


def test_tail_returns_the_last_page():
    window, page = q._paginate_items(
        _items(10), after=None, before=None, tail=True, limit=4)
    assert _keys(window) == ["i06", "i07", "i08", "i09"]
    assert page["has_after"] is False
    assert page["has_before"] is True


def test_head_page_is_the_default():
    window, page = q._paginate_items(
        _items(10), after=None, before=None, tail=False, limit=4)
    assert _keys(window) == ["i00", "i01", "i02", "i03"]
    assert page["has_before"] is False
    assert page["has_after"] is True


def test_limit_zero_is_unbounded_for_export():
    """Guard, not a bug reproduction — this passes before the fix too.

    ``get_codex_conversation_export`` passes ``limit=0``. A literal port of the
    Claude default branch would make every Codex export empty.
    """
    window, page = q._paginate_items(
        _items(10), after=None, before=None, tail=False, limit=0)
    assert len(window) == 10
    assert page["has_before"] is False and page["has_after"] is False
    assert page["total"] == 10 and page["returned"] == 10


def test_before_resolves_a_member_item_key_alias():
    items = _items(10)
    items[8]["member_item_keys"] = ["folded-into-i08"]
    window, _page = q._paginate_items(
        items, after=None, before="folded-into-i08", tail=False, limit=2)
    assert _keys(window) == ["i06", "i07"]


def test_after_resolves_a_member_item_key_alias():
    items = _items(10)
    items[2]["member_item_keys"] = ["folded-into-i02"]
    window, _page = q._paginate_items(
        items, after="folded-into-i02", before=None, tail=False, limit=2)
    assert _keys(window) == ["i03", "i04"]


# ── the per-page block budget (#463 S1, spec section 2) ─────────────────────


def _sized(n: int, blocks: int) -> list[dict]:
    return [{"item_key": f"i{i:02d}", "member_item_keys": [], "block_count": blocks}
            for i in range(n)]


def test_the_page_budget_bounds_a_page_the_item_count_does_not():
    """The profiled shape, reduced to its arithmetic.

    The profiled response was ``total: 78, returned: 78, has_after: false`` —
    13.3 MB in one page, because 78 items is fewer than the requested 500 and
    the largest of them held 827 blocks. A change that capped items alone would
    not have bounded that conversation at all.
    """
    items = _sized(78, 40)  # 3,120 blocks, well over the 2,000-block budget
    window, page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=500, block_budget=2000)
    assert len(window) < 78
    assert sum(item["block_count"] for item in window) <= 2000
    assert page["has_after"] is True
    assert page["after"] == window[-1]["item_key"]


def test_the_page_budget_trims_a_tail_page_from_the_front():
    items = _sized(78, 40)
    window, page = q._paginate_items(
        items, after=None, before=None, tail=True, limit=500, block_budget=2000)
    # A tail page stays anchored at the END of the conversation, so the trim
    # comes off the front.
    assert window[-1]["item_key"] == "i77"
    assert sum(item["block_count"] for item in window) <= 2000
    assert page["has_after"] is False and page["has_before"] is True


def test_the_page_budget_trims_a_reverse_page_from_the_front():
    items = _sized(78, 40)
    window, page = q._paginate_items(
        items, after=None, before="i60", tail=False, limit=500, block_budget=2000)
    assert window[-1]["item_key"] == "i59"  # still ends at the cursor
    assert sum(item["block_count"] for item in window) <= 2000
    assert page["has_before"] is True


def test_a_single_oversized_item_is_still_served():
    """The budget must never return an empty page for one huge item."""
    items = [{"item_key": "big", "member_item_keys": [], "block_count": 9000}]
    window, page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=500, block_budget=2000)
    assert [item["item_key"] for item in window] == ["big"]
    assert page["returned"] == 1


def test_the_page_budget_never_applies_to_the_unbounded_export():
    items = _sized(78, 40)
    window, _page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=0, block_budget=2000)
    assert len(window) == 78


def test_items_without_a_block_count_are_unaffected_by_the_budget():
    window, _page = q._paginate_items(
        _items(10), after=None, before=None, tail=False, limit=5, block_budget=2000)
    assert _keys(window) == ["i00", "i01", "i02", "i03", "i04"]


# ── the per-page SOURCE-BYTE budget (#463 S1, spec section 2) ───────────────


def _sized_bytes(n: int, blocks: int, source_bytes: int) -> list[dict]:
    return [{"item_key": f"i{i:02d}", "member_item_keys": [],
             "block_count": blocks, "source_bytes": source_bytes}
            for i in range(n)]


def test_the_byte_budget_closes_a_page_the_block_budget_does_not():
    """The profiled conversation, at the REAL production constants.

    Measured on 2026-08-02 against a read-only copy of the production store:
    after segmentation the profiled conversation is 128 segments carrying 1,906
    blocks and 91.5 MB of source bytes, and it served 1,713 blocks and 13.24 MB
    on the wire in ONE page. 1,713 is below `PAGE_BLOCK_BUDGET`, so the block
    bound never fired — a per-page budget expressed only in blocks does not
    bound that conversation, which is what the source-byte budget exists to fix.

    This test deliberately uses the module constants rather than literals, so
    removing the byte bound, or raising it past the profiled figure, fails here.
    """
    per_item = 91_520_000 // 128
    items = _sized_bytes(128, 1906 // 128, per_item)
    total_blocks = sum(item["block_count"] for item in items)
    assert total_blocks < segkern.PAGE_BLOCK_BUDGET, (
        "the fixture must reproduce the profile: the block budget does not fire")
    window, page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=500,
        block_budget=segkern.PAGE_BLOCK_BUDGET,
        byte_budget=segkern.PAGE_SOURCE_BYTE_BUDGET)
    assert len(window) < 128, "the byte budget must close the page"
    assert page["has_after"] is True
    assert sum(item["source_bytes"] for item in window) <= (
        segkern.PAGE_SOURCE_BYTE_BUDGET)


def test_the_byte_budget_trims_a_reverse_page_from_the_front():
    items = _sized_bytes(200, 1, 1_000_000)
    window, page = q._paginate_items(
        items, after=None, before="i150", tail=False, limit=500,
        block_budget=None, byte_budget=4_000_000)
    assert window[-1]["item_key"] == "i149"  # still ends at the cursor
    assert sum(item["source_bytes"] for item in window) <= 4_000_000
    assert page["has_before"] is True


def test_a_single_oversized_item_survives_the_byte_budget():
    items = [{"item_key": "big", "member_item_keys": [], "block_count": 1,
              "source_bytes": 500_000_000}]
    window, page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=500,
        block_budget=None, byte_budget=4_000_000)
    assert [item["item_key"] for item in window] == ["big"]
    assert page["returned"] == 1


def test_the_byte_budget_never_applies_to_the_unbounded_export():
    items = _sized_bytes(78, 1, 10_000_000)
    window, _page = q._paginate_items(
        items, after=None, before=None, tail=False, limit=0,
        block_budget=segkern.PAGE_BLOCK_BUDGET,
        byte_budget=segkern.PAGE_SOURCE_BYTE_BUDGET)
    assert len(window) == 78


def test_whichever_budget_is_reached_first_closes_the_page():
    """Two bounds, and the tighter one wins in each direction."""
    heavy_blocks = _sized_bytes(100, 100, 1_000)     # 10,000 blocks, 100 KB
    window, _page = q._paginate_items(
        heavy_blocks, after=None, before=None, tail=False, limit=500,
        block_budget=2000, byte_budget=4_000_000)
    assert sum(item["block_count"] for item in window) <= 2000
    heavy_bytes = _sized_bytes(100, 1, 1_000_000)    # 100 blocks, 100 MB
    window, _page = q._paginate_items(
        heavy_bytes, after=None, before=None, tail=False, limit=500,
        block_budget=2000, byte_budget=4_000_000)
    assert sum(item["source_bytes"] for item in window) <= 4_000_000


def test_items_without_source_bytes_are_unaffected_by_the_byte_budget():
    window, _page = q._paginate_items(
        _items(10), after=None, before=None, tail=False, limit=5,
        block_budget=None, byte_budget=1)
    assert _keys(window) == ["i00", "i01", "i02", "i03", "i04"]


def test_empty_item_list_pages_cleanly():
    window, page = q._paginate_items(
        [], after=None, before=None, tail=True, limit=5)
    assert window == []
    assert page == {"total": 0, "returned": 0, "before": None, "after": None,
                    "has_before": False, "has_after": False}
