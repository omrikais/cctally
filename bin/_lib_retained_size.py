"""Bounded retained-object sizing for long-lived dashboard memos.

This is an admission meter, not a heap profiler.  It charges each distinct
object reachable through the containers and record shapes cctally retains,
counts shared references once, and can stop as soon as a caller's byte budget
is exceeded.  The early-stop result is the unambiguous ``budget + 1`` sentinel
so callers fail closed without traversing the rest of a large conversation or
quota population.
"""
from __future__ import annotations

import sys
import threading
import types
from collections.abc import Mapping
from typing import Callable


_ATOMIC = (str, bytes, bytearray, memoryview, int, float, bool, type(None))

# One process-wide traversal at a time. The source and snapshot-cache owners
# are independent latest-wins queues, but overlapping their CPU and temporary
# visited indexes would violate the dashboard's combined background ceiling.
RETAINED_SIZE_WORK_LOCK = threading.Lock()
RETAINED_SIZE_WORKER_DUTY = 0.25
_MIN_VISIT_INDEX_BUDGET_BYTES = 256 * 1024


class _CompactObjectIdSet:
    """Exact visited-object index without one Python integer per object.

    CPython object addresses are pointer-aligned.  Pack those addresses into
    sparse bitmap pages while retaining an exact set fallback for any runtime
    that produces an unaligned ``id``.  One bitmap page covers 32,768 aligned
    object addresses (256 KiB of address space) in 4 KiB.
    """

    _ALIGNMENT_SHIFT = 3
    _PAGE_SHIFT = 15
    _PAGE_MASK = (1 << _PAGE_SHIFT) - 1
    _PAGE_BYTES = 1 << (_PAGE_SHIFT - 3)

    __slots__ = ("_pages", "_unaligned", "_size", "_allocation_bytes")

    def __init__(self) -> None:
        self._pages: dict[int, bytearray] = {}
        self._unaligned: set[int] = set()
        self._size = 0
        self._allocation_bytes = (
            sys.getsizeof(self._pages) + sys.getsizeof(self._unaligned)
        )

    def __len__(self) -> int:
        return self._size

    def __contains__(self, object_id: int) -> bool:
        if object_id & ((1 << self._ALIGNMENT_SHIFT) - 1):
            return object_id in self._unaligned
        packed = object_id >> self._ALIGNMENT_SHIFT
        page = self._pages.get(packed >> self._PAGE_SHIFT)
        if page is None:
            return False
        bit = packed & self._PAGE_MASK
        return bool(page[bit >> 3] & (1 << (bit & 7)))

    @property
    def allocation_bytes(self) -> int:
        """Measured storage retained by the visit index itself."""
        return self._allocation_bytes

    def add(self, object_id: int) -> None:
        if object_id & ((1 << self._ALIGNMENT_SHIFT) - 1):
            before = len(self._unaligned)
            container_before = sys.getsizeof(self._unaligned)
            self._unaligned.add(object_id)
            added = len(self._unaligned) - before
            self._size += added
            if added:
                self._allocation_bytes += (
                    sys.getsizeof(self._unaligned) - container_before
                    + sys.getsizeof(object_id)
                )
            return
        packed = object_id >> self._ALIGNMENT_SHIFT
        page_key = packed >> self._PAGE_SHIFT
        page = self._pages.get(page_key)
        if page is None:
            container_before = sys.getsizeof(self._pages)
            page = bytearray(self._PAGE_BYTES)
            self._pages[page_key] = page
            self._allocation_bytes += (
                sys.getsizeof(self._pages) - container_before
                + sys.getsizeof(page_key)
                + sys.getsizeof(page)
            )
        bit = packed & self._PAGE_MASK
        byte_index = bit >> 3
        mask = 1 << (bit & 7)
        if not page[byte_index] & mask:
            page[byte_index] |= mask
            self._size += 1


class RetainedSizeCancelled(RuntimeError):
    """A cooperative background retained-size pass was superseded or stopped."""


def retained_size_bytes(
    value,
    *,
    stop_after: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    _object_id: Callable[[object], int] = id,
) -> int:
    """Return owned reachable bytes, or ``stop_after + 1`` once over budget.

    ``sys.getsizeof`` is intentionally the accounting primitive: the result is
    a stable process-local admission estimate for comparing against another
    process-local ceiling.  Referents outside the supported retained shapes are
    charged for their object shell and, when present, ``__dict__``/``__slots__``.
    Modules, functions and classes therefore do not cause an unbounded walk of
    interpreter-global state.
    """
    if stop_after is not None and stop_after < 0:
        raise ValueError("stop_after must be non-negative or None")

    seen = _CompactObjectIdSet()
    total = 0
    sentinel = None if stop_after is None else int(stop_after) + 1
    visit_index_budget = (
        None if stop_after is None
        else max(_MIN_VISIT_INDEX_BUDGET_BYTES, int(stop_after) // 4)
    )

    def add(obj, object_id: int) -> bool:
        nonlocal total
        seen.add(object_id)
        total += sys.getsizeof(obj)
        if cancelled is not None and len(seen) % 1024 == 0 and cancelled():
            raise RetainedSizeCancelled()
        return (
            stop_after is not None
            and (
                total > stop_after
                or seen.allocation_bytes > visit_index_budget
            )
        )

    def walk(obj) -> bool:
        object_id = _object_id(obj)
        if object_id in seen:
            return False
        if add(obj, object_id):
            return True
        if isinstance(obj, _ATOMIC):
            return False
        if isinstance(obj, (types.ModuleType, type)) or callable(obj):
            return False
        if isinstance(obj, (dict, types.MappingProxyType, Mapping)):
            for key, item in obj.items():
                if walk(key) or walk(item):
                    return True
            return False
        if isinstance(obj, (tuple, list, set, frozenset)):
            for item in obj:
                if walk(item):
                    return True
            return False
        namespace = getattr(obj, "__dict__", None)
        if isinstance(namespace, dict) and walk(namespace):
            return True
        slots = getattr(type(obj), "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for field in slots:
            if field in {"__dict__", "__weakref__"}:
                continue
            try:
                item = getattr(obj, field)
            except (AttributeError, TypeError):
                continue
            if walk(item):
                return True
        return False

    exceeded = walk(value)
    return sentinel if exceeded and sentinel is not None else total
