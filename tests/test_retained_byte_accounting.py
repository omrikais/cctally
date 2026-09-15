"""Incremental retained-byte accounting for the Codex source caches.

#769 S6 T2a, #716 Task B. Retained bytes used to be discovered after the fact
by a background worker that walked two full container copies of the whole
cache set once per generation. On the sealed 222,888-row measurement corpus
that walk visited over eleven million objects across one run and never
completed a single generation, so the caches were admitted against a byte
figure that stayed at zero until the walk finally returned the over-budget
sentinel, which cleared all eleven caches at once.

The bytes are now known when the mutation that changed them returns. Every
entry is charged once, at admission, by its owner's declared charge function,
and the charge is retained beside the entry so a removal subtracts exactly what
the admission added and a replacement subtracts before it adds.

That only holds if EVERY mutation surface is accounted for. A surface that
slips through does not raise and does not move a golden: it silently
desynchronises the running total from the graph it is supposed to describe, and
the total then drifts for the life of the process. The completeness test below
is what makes an unaccounted surface a test failure instead.
"""
from __future__ import annotations

import random
import sys
from collections import OrderedDict

import pytest

import _cctally_dashboard_sources as sources  # noqa: E402  (bin/ on sys.path)


@pytest.fixture
def cache():
    """One declared owner, charged by the ordinary borrowing charge function."""
    subject = sources._ObservedDict()
    subject._cache_owner_name = "test-owner"
    subject._cache_owner_charge = sources._charge_borrowing_payloads
    return subject


def _recompute(subject) -> int:
    """The full recomputation the running total must equal."""
    return sum(
        subject._cache_owner_charge(key, value)
        for key, value in subject.items()
    )


# ── every accounted mutator moves the total ────────────────────────────────

def test_setitem_charges_the_admitted_entry(cache):
    assert cache.retained_owner_bytes == 0
    cache["a"] = ("payload", 1, 2, 3)
    assert cache.retained_owner_bytes == _recompute(cache) > 0


def test_a_replacement_subtracts_before_it_adds(cache):
    """A same-key overwrite must not leave the superseded charge standing."""
    cache["a"] = ("x",) * 40
    big = cache.retained_owner_bytes
    cache["a"] = ("x",)
    assert cache.retained_owner_bytes < big
    assert cache.retained_owner_bytes == _recompute(cache)


def test_delitem_subtracts_exactly_what_was_charged(cache):
    cache["a"] = ("x",) * 8
    cache["b"] = ("y",) * 8
    del cache["a"]
    assert cache.retained_owner_bytes == _recompute(cache)
    del cache["b"]
    assert cache.retained_owner_bytes == 0


def test_pop_subtracts_and_a_missing_pop_does_not(cache):
    cache["a"] = ("x",) * 8
    charged = cache.retained_owner_bytes
    assert cache.pop("absent", None) is None
    assert cache.retained_owner_bytes == charged
    cache.pop("a")
    assert cache.retained_owner_bytes == 0


def test_popitem_subtracts_the_item_it_removed(cache):
    cache["a"] = ("x",) * 32
    cache["b"] = ("y",)
    cache.popitem()
    assert cache.retained_owner_bytes == _recompute(cache)


def test_setdefault_charges_only_when_it_inserts(cache):
    cache.setdefault("a", ("x",) * 8)
    charged = cache.retained_owner_bytes
    assert charged == _recompute(cache) > 0
    cache.setdefault("a", ("y",) * 900)
    assert cache.retained_owner_bytes == charged, (
        "setdefault over an existing key inserts nothing and must charge "
        "nothing")


def test_bulk_update_charges_every_key_it_wrote(cache):
    cache.update({"a": ("x",) * 4, "b": ("y",) * 4})
    assert cache.retained_owner_bytes == _recompute(cache) > 0


def test_the_restore_path_update_replaces_rather_than_accumulates(cache):
    """`cache.clear(); cache.update(prior)` is what a failed build runs."""
    cache["a"] = ("x",) * 64
    prior = dict(cache)
    cache["a"] = ("x",) * 4
    cache["b"] = ("y",) * 4
    cache.clear()
    cache.update(prior)
    assert cache.retained_owner_bytes == _recompute(cache)


def test_update_over_an_existing_key_subtracts_before_it_adds(cache):
    cache["a"] = ("x",) * 64
    big = cache.retained_owner_bytes
    cache.update({"a": ("x",)})
    assert cache.retained_owner_bytes < big
    assert cache.retained_owner_bytes == _recompute(cache)


def test_clear_discharges_everything(cache):
    cache["a"] = ("x",) * 8
    cache["b"] = ("y",) * 8
    cache.clear()
    assert cache.retained_owner_bytes == 0
    assert not cache._cache_entry_bytes


def test_move_to_end_reorders_without_moving_the_total():
    """Reordering is eviction order, not retention, and a HIT performs it."""
    subject = sources._ObservedOrderedDict()
    subject._cache_owner_name = "test-owner"
    subject._cache_owner_charge = sources._charge_borrowing_payloads
    subject["a"] = ("x",) * 8
    subject["b"] = ("y",) * 8
    charged = subject.retained_owner_bytes
    before_generation = sources._CODEX_SOURCE_ACCELERATOR_GENERATION
    subject.move_to_end("a")
    assert list(subject) == ["b", "a"]
    assert subject.retained_owner_bytes == charged
    assert sources._CODEX_SOURCE_ACCELERATOR_GENERATION == before_generation, (
        "a memo hit calls move_to_end; dirtying the accelerators on a read "
        "would make every read a generation")


# ── the surface is complete, and what it does not cover is refused ─────────

def test_the_frozen_mapping_surface_still_describes_this_interpreter():
    """DERIVED from `dir()`, so a name a future interpreter adds is a failure.

    The prior version of this test intersected a HAND-WRITTEN literal with
    `hasattr`. The filter could only ever REMOVE names, so a mutator added to a
    future `dict` never entered the set being checked and the test could not
    fail — while the product docstring claimed exactly that it would. This
    compares the running interpreter's own `dir(dict) | dir(OrderedDict)`
    against the frozen snapshot the classification was written over.
    """
    live = set(dir(dict)) | set(dir(OrderedDict))
    known = sources._OBSERVED_CACHE_KNOWN_MAPPING_NAMES
    added = sorted(live - known)
    assert not added, (
        f"this interpreter's mappings expose {added}, which the retained-byte "
        "classification has never seen. Classify each one as accounted, "
        "reordering, construction or prohibited before the estate goes green.")
    removed = sorted(known - live)
    assert not removed, (
        f"the frozen snapshot names {removed}, which this interpreter's "
        "mappings no longer expose; the snapshot is stale")


def test_every_mutating_name_is_accounted_reordered_or_prohibited():
    """No mutation surface may be left implicit.

    An unaccounted mutator does not raise and does not move a golden. It moves
    the retained graph while the running total stands still, and the total then
    stays wrong for the life of the process.
    """
    classified = (
        sources._OBSERVED_CACHE_ACCOUNTED_MUTATORS
        | sources._OBSERVED_CACHE_REORDERING_MUTATORS
        | sources._OBSERVED_CACHE_PROHIBITED_MUTATORS
        | sources._OBSERVED_CACHE_CONSTRUCTION_SURFACES
    )
    mutating = {
        "__setitem__", "__delitem__", "__ior__", "clear", "pop", "popitem",
        "setdefault", "update", "move_to_end", "__init__",
    }
    present = {
        name for name in mutating
        if hasattr(dict, name) or hasattr(OrderedDict, name)
    }
    assert present <= classified, sorted(present - classified)
    assert len(classified & present) == len(present)
    # No name may be classified twice; a mutator that is both "accounted" and
    # "reordering" means nobody decided which.
    buckets = (
        sources._OBSERVED_CACHE_ACCOUNTED_MUTATORS,
        sources._OBSERVED_CACHE_REORDERING_MUTATORS,
        sources._OBSERVED_CACHE_PROHIBITED_MUTATORS,
        sources._OBSERVED_CACHE_CONSTRUCTION_SURFACES,
    )
    assert sum(len(bucket) for bucket in buckets) == len(classified)
    for name in sources._OBSERVED_CACHE_ACCOUNTED_MUTATORS:
        assert name in vars(sources._ObservedCacheMixin), (
            f"{name} is declared accounted but the mixin does not override it")
    assert "move_to_end" in vars(sources._ObservedOrderedDict), (
        "the reordering surface must be overridden where it exists, or the "
        "quota memo's hits stamp no recency")


def test_bulk_update_accepts_a_one_shot_iterator_of_pairs(cache):
    """`dict.update` takes any iterable of pairs, and an iterator is one.

    The keys were enumerated OUT of the iterator and the exhausted iterator was
    then handed to `super().update()`, so nothing was inserted and the charge
    loop raised `KeyError` on the first key. No production caller passes an
    iterator today, which is why it was latent rather than broken — but the
    surface the spec required to be complete has to keep the convention it is
    completing.
    """
    cache.update(iter([("a", ("x",)), ("b", ("y",))]))
    assert dict(cache) == {"a": ("x",), "b": ("y",)}
    assert cache.retained_owner_bytes > 0
    assert set(cache._cache_entry_bytes) == {"a", "b"}


def test_a_key_in_both_the_positional_source_and_kwargs_is_charged_once(cache):
    """`dict.update` allows one key in both arguments; the ledger allowed two charges.

    `changed` collected the key from the mapping AND from `kwargs`, so the
    discharge loop subtracted it once and then subtracted nothing, while the
    charge loop ADDED it twice against a `_cache_entry_bytes` that holds it
    once. The running total is then permanently above the graph it describes,
    which evicts early for the life of the process, and nothing raises. No
    in-tree caller passes both today; the surface this mixin exists to make
    complete has to keep `dict.update`'s convention anyway.
    """
    cache.update({"a": ("x",) * 8}, a=("y",) * 8)

    assert cache["a"] == ("y",) * 8, "kwargs must still win, as `dict` does"
    assert cache.retained_owner_bytes == _recompute(cache), (
        "the key was charged twice against one stored per-key figure")
    assert cache.retained_owner_bytes == cache._cache_entry_bytes["a"]


def test_bulk_update_from_a_generator_charges_every_key(cache):
    cache.update((key, (key,)) for key in ("p", "q", "r"))
    assert sorted(cache) == ["p", "q", "r"]
    assert cache.retained_owner_bytes == sum(
        cache._cache_entry_bytes[key] for key in cache)


def test_reinvoking_init_on_a_live_cache_is_refused(cache):
    """`__init__` populates at C level, so it charges nothing.

    It is sound exactly once, because the ledgers are created in the same call
    immediately before the C constructor runs. Re-invoking it on a live cache
    would leave a zeroed ledger beside a populated mapping — the exact silent
    desynchronisation the surface classification exists to prevent — and it
    appeared in none of the classification sets at all.
    """
    cache["a"] = ("x",) * 8
    charged = cache.retained_owner_bytes
    assert charged > 0

    with pytest.raises(TypeError, match="refuses a second __init__"):
        cache.__init__({"b": ("y",)})

    assert cache.retained_owner_bytes == charged
    assert dict(cache) == {"a": ("x",) * 8}


def test_a_fresh_cache_constructed_with_contents_is_classified_not_charged():
    """The one sound use, stated: a NEW object, charged nothing, holding rows.

    This is why `__init__` is a construction surface rather than an accounted
    mutator. The declared owners are all constructed empty, so no live cache
    reaches this state; the classification records that rather than leaving it
    implicit.
    """
    subject = sources._ObservedDict({"seeded": ("x",) * 8})
    assert dict(subject) == {"seeded": ("x",) * 8}
    assert subject.retained_owner_bytes == 0, (
        "population through the C constructor charges nothing, which is what "
        "makes it a construction surface rather than a mutation")
    for owner in sources._CODEX_SOURCE_CACHE_OWNERS:
        assert owner.cache._cache_entry_bytes is not None


def test_the_prohibited_inplace_union_is_refused(cache):
    """`|=` mutates in place without routing through `update`."""
    cache["a"] = ("x",)
    with pytest.raises(TypeError, match="refuses"):
        cache |= {"b": ("y",)}


def test_an_undeclared_cache_accounts_nothing_rather_than_guessing():
    subject = sources._ObservedDict()
    subject["a"] = ("x",) * 64
    assert subject.retained_owner_bytes == 0


def test_a_charge_that_raises_is_counted_and_does_not_take_the_write():
    """A broken charge must deflate the total visibly, never lose the entry."""
    subject = sources._ObservedDict()
    subject._cache_owner_name = "raising"

    def explode(_key, _value):
        raise RuntimeError("charge failed")

    subject._cache_owner_charge = explode
    before = sources.codex_source_accelerator_memory_stats()[
        "measurementErrorCount"]
    subject["a"] = ("x",)
    assert subject["a"] == ("x",)
    assert subject.retained_owner_bytes == 0
    after = sources.codex_source_accelerator_memory_stats()[
        "measurementErrorCount"]
    assert after == before + 1


# ── exact reconciliation after a randomized sequence ──────────────────────

def test_the_running_total_equals_a_full_recomputation(cache):
    """The property that matters: no drift over an arbitrary mutation order."""
    rng = random.Random(20260910)
    keys = [f"k{index}" for index in range(24)]
    for step in range(600):
        key = rng.choice(keys)
        action = rng.random()
        if action < 0.42:
            cache[key] = ("v",) * rng.randrange(1, 24)
        elif action < 0.56:
            cache.pop(key, None)
        elif action < 0.66:
            cache.setdefault(key, ("d",) * rng.randrange(1, 8))
        elif action < 0.76:
            cache.update({
                rng.choice(keys): ("u",) * rng.randrange(1, 12)
                for _ in range(rng.randrange(1, 4))
            })
        elif action < 0.84 and cache:
            cache.popitem()
        elif action < 0.88:
            cache.clear()
        assert cache.retained_owner_bytes == _recompute(cache), (
            f"drifted at step {step} after {action:.2f}")
    assert cache.retained_owner_bytes == _recompute(cache)


# ── admission reads the total instead of scheduling a traversal ───────────

def test_admission_publishes_the_incremental_total_without_a_worker():
    sources.reset_codex_source_caches()
    sources._CODEX_PERIOD_VIEW_CACHE["resident"] = ("payload",) * 12
    sources.enforce_codex_source_accelerator_bounds()
    observed = sources.codex_source_accelerator_memory_stats()
    assert observed["estimatedBytes"] == sources.codex_source_retained_bytes()
    assert observed["estimatedBytes"] > 0
    assert observed["measurementPending"] == 0, (
        "a measurement that is complete when the mutation returns is never "
        "pending")
    assert observed["workerAlive"] == 0
    sources.reset_codex_source_caches()


def test_admission_evicts_when_the_incremental_total_exceeds_the_ceiling(
    monkeypatch,
):
    sources.reset_codex_source_caches()
    sources._CODEX_PERIOD_VIEW_CACHE["resident"] = ("payload",) * 12
    monkeypatch.setattr(sources, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", 1)
    before = sources.codex_source_accelerator_memory_stats()["fallbackCount"]
    sources.enforce_codex_source_accelerator_bounds()
    assert not sources._CODEX_PERIOD_VIEW_CACHE
    observed = sources.codex_source_accelerator_memory_stats()
    assert observed["fallbackCount"] == before + 1
    assert observed["estimatedBytes"] == 0
    sources.reset_codex_source_caches()


def test_the_retained_size_worker_shims_are_inert():
    """The dashboard still binds and unbinds them; neither does anything."""
    assert sources.set_codex_source_memory_completion_lock(object()) is None
    assert sources.shutdown_codex_source_memory_worker() is None
    assert sources.set_codex_source_memory_completion_lock(None) is None
    assert not hasattr(sources, "_CodexSourceMemoryWorker")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
