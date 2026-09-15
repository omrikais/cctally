"""The per-build generation overlay that replaced the whole-estate checkpoint.

#769 S6 T2a, #716 Task B. `capture_codex_source_state` took
`tuple(dict(cache) for cache in caches)` — a complete container copy of every
retained Codex source cache — so that a build that failed could be rolled back
by `clear()` followed by `update(prior)`. On the sealed #786 v3 `current`
corpus that cost a full copy of 222,896 adapter entries on every build,
whatever the build actually changed.

The rollback is now a per-build journal of the keys the build touched. A
failed build restores exactly those keys and leaves everything else alone,
which is what makes an invalidation of one physical path cost the one path.

PUBLICATION IS COMPARE-AND-SWAP, and the reason is concurrency rather than
tidiness. `bin/_cctally_dashboard_share.py` calls `build_codex_source_state`
from an HTTP request thread for custom-period share rendering, reaching the
same module-global caches as the synchronized dashboard builder without taking
the dashboard's sync lock. Each build records the publish epoch it started
from; a build whose epoch has moved refuses to publish, discards its own
journal and leaves the caches cold, so the next build reconstructs rather than
serving a generation two builds interleaved into.
"""
from __future__ import annotations

import sqlite3
import sys
import threading

import pytest

import _cctally_dashboard_sources as sources  # noqa: E402

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)
from test_snapshot_bounded_work import (  # noqa: E402
    _broaden_corpus,
    _widen_corpus,
)


class _AnyDict(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, dict)

    def __subclasscheck__(cls, other):
        return issubclass(other, dict)


def _count_full_container_copies(module, monkeypatch):
    """Count `dict(cache)` over an observed cache, at the expression itself.

    Python resolves a bare `dict(...)` inside the module through the module's
    globals before the builtins, so shadowing the name observes the copy where
    it happens rather than inferring it from a wrapper's assumption about what
    a call site still does. The metaclass answers the builtin's `isinstance`
    question, because the module runs `isinstance(value, dict)` and a bare
    subclass would answer False for every ordinary dict.
    """
    observed = {id(cache) for cache in module._codex_source_caches()}
    counted: list[int] = []

    class _CountingDict(dict, metaclass=_AnyDict):
        def __init__(self, *args, **kwargs):
            if len(args) == 1 and not kwargs and isinstance(args[0], dict):
                if id(args[0]) in observed:
                    counted.append(len(args[0]))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "dict", _CountingDict, raising=False)
    return counted


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _widen_corpus(cache)
    shape = _broaden_corpus(cache)
    assert shape["rows"] >= 40, shape
    module = sys.modules["_cctally_dashboard_sources"]
    module.reset_codex_source_caches()
    try:
        yield ns, cache, stats, module
    finally:
        module.reset_codex_source_caches()
        cache.close()
        stats.close()


def _context(module, cache, stats):
    return module.DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START, now_utc=NOW,
        display_tz_name="UTC",
    )


def _main_path(conn):
    return next(
        str(row[2]) for row in conn.execute("PRAGMA database_list")
        if str(row[1]) == "main"
    )


def _snapshot(module):
    """A plain reference copy of every cache, for a bit-for-bit comparison."""
    return tuple({key: value for key, value in cache.items()}
                 for cache in module._codex_source_caches())


# ── the bundle the overlay covers is enumerated, not assumed ──────────────

def test_the_overlay_bundle_names_every_piece_of_transactional_state():
    """A discard must restore exactly what the old restore restored.

    The `mappings` assertion is deliberately NOT `bundle["mappings"] == tuple(
    owner.name for owner in owners)`, which is the expression that constructs
    the field and therefore cannot fail. It is compared against
    `_codex_source_caches`, the independent definition of what a build
    checkpoints, through each cache's own declared owner name.
    """
    module = sys.modules["_cctally_dashboard_sources"]
    bundle = module.CODEX_SOURCE_OVERLAY_BUNDLE
    checkpointed = tuple(
        cache._cache_owner_name for cache in module._codex_source_caches())
    assert all(checkpointed), (
        "every checkpointed cache must carry a declared owner name")
    assert bundle["mappings"] == checkpointed
    assert len(bundle["mappings"]) == 11
    for name in (
        "quota_observation_cache_stats",
        "account_card_totals_fallbacks",
        "snapshot_accounting_cache_state",
    ):
        assert name in bundle["extra_state"], name
    assert bundle["ordering"] == ("quota_observation_lru_order",), (
        "the ordering the bundle claims to cover must be named; nothing "
        "asserted this field, which is why its claim went unchecked")
    # Diagnostics that deliberately survive a discard, named rather than
    # silently omitted: a rolled-back build still HAPPENED, and a counter that
    # forgot it would under-report the work the process actually did.
    assert bundle["not_rolled_back"], (
        "the counters a discard deliberately leaves standing must be named")


def test_a_discard_restores_the_quota_memo_lru_order(env):
    """The claim the bundle used to make about the journal, asserted.

    Reverse journal replay reinstates the prior order only when the build's
    removals were last-in-first-out, and this memo evicts with
    `popitem(last=False)`, which takes the oldest key. Demonstrated in the
    interpreter:

        start     ['a', 'b', 'c']
        in-build  ['b', 'c', 'e']
        replayed  ['b', 'c', 'a']

    And `move_to_end` is not journaled at all, so a build that only READ the
    memo left it permanently reordered. The order is checkpointed explicitly
    now, and this is what holds it there.
    """
    module = sys.modules["_cctally_dashboard_sources"]
    module.reset_codex_quota_observation_cache()
    memo = module._CODEX_QUOTA_OBSERVATION_CACHE
    for key in ("a", "b", "c"):
        memo[key] = (key,)
        module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES[key] = 8
    module._CODEX_QUOTA_OBSERVATION_CACHE_BYTES = 24
    before = list(memo)
    assert before == ["a", "b", "c"]

    checkpoint = module._quota_observation_cache_checkpoint()
    overlay = module._open_codex_source_overlay()
    try:
        # Exactly the two mutations the journal cannot reconstruct: an
        # oldest-first eviction, and a hit that reorders without writing.
        memo.popitem(last=False)
        memo["e"] = ("e",)
        module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["e"] = 8
        memo.move_to_end("b")
        assert list(memo) != before
    finally:
        overlay.discard()
        module._restore_quota_observation_cache_checkpoint(checkpoint)

    assert list(memo) == before, (
        "a discard must reinstate the eviction order the build started from, "
        "and reverse journal replay does not")
    module.reset_codex_quota_observation_cache()


# ── a failed build leaves the previous generation intact ──────────────────

def test_a_failed_build_restores_the_previous_generation_exactly(env):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    before = _snapshot(module)
    assert any(before), "non-vacuity: the first build must have retained state"

    boom = RuntimeError("synthetic build failure")
    with pytest.raises(RuntimeError, match="synthetic build failure"):
        with module.codex_path_scope() as path_scope:
            capture = module.capture_codex_source_state(
                context, path_scope=path_scope)
            original = module._build_codex_source_state

            def fail(*args, **kwargs):
                # Mutate first, so the rollback has something to undo.
                module._CODEX_PERIOD_VIEW_CACHE["poisoned"] = ("x",) * 8
                raise boom

            module._build_codex_source_state = fail
            try:
                module.build_codex_source_state_from_capture(
                    capture, data_version="two", path_scope=path_scope)
            finally:
                module._build_codex_source_state = original

    after = _snapshot(module)
    assert after == before, "a failed build must leave the caches untouched"


def test_a_failed_build_leaves_the_retained_byte_totals_untouched(env):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    before = module.codex_source_retained_bytes()

    with module.codex_path_scope() as path_scope:
        capture = module.capture_codex_source_state(
            context, path_scope=path_scope)
        original = module._build_codex_source_state

        def fail(*args, **kwargs):
            module._CODEX_PERIOD_VIEW_CACHE["poisoned"] = ("x",) * 64
            raise RuntimeError("synthetic build failure")

        module._build_codex_source_state = fail
        try:
            with pytest.raises(RuntimeError):
                module.build_codex_source_state_from_capture(
                    capture, data_version="two", path_scope=path_scope)
        finally:
            module._build_codex_source_state = original

    assert module.codex_source_retained_bytes() == before


# ── an invalidation costs what it changed ─────────────────────────────────

def test_a_warm_build_performs_zero_full_container_copies(env, monkeypatch):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")

    counted = _count_full_container_copies(module, monkeypatch)
    module.build_codex_source_state(context, data_version="two")
    assert counted == [], (
        f"a warm build copied {len(counted)} whole containers holding "
        f"{sum(counted)} entries")


def test_the_journal_records_only_the_keys_a_build_touched(env):
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    resident = sum(len(c) for c in module._codex_source_caches())
    assert resident > 0

    with module.codex_path_scope() as path_scope:
        capture = module.capture_codex_source_state(
            context, path_scope=path_scope)
        journalled = capture.overlay.journal_size()
        module.build_codex_source_state_from_capture(
            capture, data_version="two", path_scope=path_scope)
    assert journalled < resident, (
        f"a clean second build journalled {journalled} of {resident} resident "
        "entries, which is the whole-estate copy under another name")


# ── two concurrent builds cannot publish or discard each other ────────────

def test_a_build_whose_base_generation_moved_refuses_to_publish(env):
    """The share render and the dashboard tick reach the same caches."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")

    with module.codex_path_scope() as path_scope:
        # The dashboard tick opens its overlay.
        tick = module.capture_codex_source_state(context, path_scope=path_scope)

        # A share render on another thread completes a whole build meanwhile.
        # It opens its OWN connections, because sqlite3 refuses a handle
        # across threads — which is exactly the shape of the real share render
        # on an HTTP request thread.
        errors: list[BaseException] = []

        cache_path = _main_path(cache)
        stats_path = _main_path(stats)

        def render():
            own_cache = sqlite3.connect(cache_path)
            own_stats = sqlite3.connect(stats_path)
            try:
                module.build_codex_source_state(
                    module.DashboardReadContext(
                        cache_conn=own_cache, stats_conn=own_stats,
                        range_start=START, now_utc=NOW, display_tz_name="UTC",
                    ),
                    data_version="share",
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                own_cache.close()
                own_stats.close()

        thread = threading.Thread(target=render, name="share-render")
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive()
        assert not errors, errors

        module.build_codex_source_state_from_capture(
            tick, data_version="two", path_scope=path_scope)

    assert tick.overlay.published is False, (
        "a build whose base generation moved must refuse to publish")
    assert not any(module._codex_source_caches()), (
        "a refused publication must leave the caches cold, not holding a "
        "generation two builds interleaved into")


def test_two_genuinely_interleaved_builds_cannot_publish_into_each_other(env):
    """Both folds are inside the product AT THE SAME TIME, on a barrier.

    The prior version of this test called `thread.join(timeout=60)` before the
    tick's own build, so only the tick's CAPTURE overlapped the share render.
    That proves the compare-and-swap refuses a moved epoch; it does not prove
    plan Task 10 step 2's requirement, which is about two builds running at
    once. A two-party barrier inside `_build_codex_source_state` holds both
    builds in the fold until both have arrived.
    """
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")

    barrier = threading.Barrier(2, timeout=55)
    original = module._build_codex_source_state
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def interleaved(*args, **kwargs):
        # Both builds block here until the other has also reached the fold.
        barrier.wait()
        return original(*args, **kwargs)

    cache_path = _main_path(cache)
    stats_path = _main_path(stats)

    def render():
        own_cache = sqlite3.connect(cache_path)
        own_stats = sqlite3.connect(stats_path)
        try:
            results["share"] = module.build_codex_source_state(
                module.DashboardReadContext(
                    cache_conn=own_cache, stats_conn=own_stats,
                    range_start=START, now_utc=NOW, display_tz_name="UTC",
                ),
                data_version="share",
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
            try:
                barrier.abort()
            except Exception:  # noqa: BLE001 - best effort unblock
                pass
        finally:
            own_cache.close()
            own_stats.close()

    module._build_codex_source_state = interleaved
    thread = threading.Thread(target=render, name="share-render")
    try:
        thread.start()
        results["tick"] = module.build_codex_source_state(
            context, data_version="tick")
        thread.join(timeout=55)
    finally:
        module._build_codex_source_state = original
    assert not thread.is_alive()
    assert not errors, errors

    # Neither build may serve a generation the other interleaved into: both
    # results were folded from their own captured evidence, so they agree.
    assert results["tick"].data == results["share"].data, (
        "two interleaved builds published different data from one store")

    # And the caches are either one coherent generation or cold — never a
    # mixture. A rebuild from whatever is left must reproduce the same bytes.
    from test_source_cache_degradation import _envelope_bytes

    after = module.build_codex_source_state(context, data_version="after")
    module.reset_codex_source_caches()
    cold = module.build_codex_source_state(context, data_version="after")
    assert _envelope_bytes(after) == _envelope_bytes(cold), (
        "the generation left behind by two interleaved builds does not "
        "reproduce a cold build")


def test_a_refused_publication_takes_every_accelerator_cold_together(env):
    """All eleven go, and that is correctness rather than caution.

    Clearing only the caches the losing build wrote to looks like the obvious
    saving, and it is wrong. The accounting state has to be reset on this path,
    because the checkpoint this build captured predates the winning build; a
    reset accounting state reports its next reconstruction as cold with
    `changed_old` empty and `changed_new` the whole population; and ten of the
    eleven accelerators apply that as a DELTA to what they already hold. A
    survivor therefore publishes the population twice. That was measured, not
    argued: an earlier remediation cleared the subset and
    `tests/test_source_cache_degradation.py` caught `cost_usd` and every token
    count at twice their value.

    The thrash this costs under concurrent share rendering is recorded in spec
    section 5.2 rather than reduced.
    """
    _ns, cache, stats, module = env
    import _lib_snapshot_cache as snapshot

    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    assert sum(len(c) for c in module._codex_source_caches()) > 0

    with module.codex_path_scope() as path_scope:
        tick = module.capture_codex_source_state(context, path_scope=path_scope)
        with module._CODEX_SOURCE_PUBLISH_LOCK:
            module._CODEX_SOURCE_PUBLISH_EPOCH += 1
        module.build_codex_source_state_from_capture(
            tick, data_version="two", path_scope=path_scope)

    assert tick.overlay.published is False
    assert not any(module._codex_source_caches()), (
        "a refused publication must leave every accelerator cold")
    assert not snapshot._CODEX_ACCOUNTING_CACHE_STATE.get("entries"), (
        "and the accounting state those accelerators take their deltas from "
        "must go with them, or the next reconstruction double-counts")


def test_a_refused_publication_restores_the_account_card_fallback_counter(env):
    """Enumerated `extra_state` is restored, not zeroed by a blanket reset."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    module._CODEX_ACCOUNT_CARD_TOTALS_FALLBACKS = 7

    with module.codex_path_scope() as path_scope:
        tick = module.capture_codex_source_state(context, path_scope=path_scope)
        assert tick.account_card_fallbacks_checkpoint == 7
        module._CODEX_ACCOUNT_CARD_TOTALS_FALLBACKS = 9
        with module._CODEX_SOURCE_PUBLISH_LOCK:
            module._CODEX_SOURCE_PUBLISH_EPOCH += 1
        module.build_codex_source_state_from_capture(
            tick, data_version="two", path_scope=path_scope)

    assert module._CODEX_ACCOUNT_CARD_TOTALS_FALLBACKS == 7, (
        "an enumerated piece of transactional state must be restored from the "
        "checkpoint, not zeroed by `reset_codex_source_caches`")
    module._CODEX_ACCOUNT_CARD_TOTALS_FALLBACKS = 0


def test_a_failed_build_whose_base_moved_never_writes_its_journal_back(env):
    """The exception path is now as safe as the refused-publication path.

    `_restore_codex_source_capture` replayed the journal unconditionally, and
    every value in a journal predates the build that wrote it. When another
    build has published in between, replaying writes a value older than BOTH
    builds over a key the other one legitimately republished.
    """
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    period = module._CODEX_PERIOD_VIEW_CACHE
    period["contested"] = ("old",)

    with module.codex_path_scope() as path_scope:
        capture = module.capture_codex_source_state(
            context, path_scope=path_scope)
        # This build touches the key, so it is in its journal with prior "old".
        period["contested"] = ("mine",)
        # Meanwhile another build publishes a NEWER value for the same key.
        with module._CODEX_SOURCE_PUBLISH_LOCK:
            module._CODEX_SOURCE_PUBLISH_EPOCH += 1
        period["contested"] = ("newer",)

        original = module._build_codex_source_state

        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic build failure")

        module._build_codex_source_state = fail
        try:
            with pytest.raises(RuntimeError):
                module.build_codex_source_state_from_capture(
                    capture, data_version="two", path_scope=path_scope)
        finally:
            module._build_codex_source_state = original

    # NOT merely `!= ("old",)`. The drop takes every cache cold, so the key is
    # absent afterwards and a `get` returning `None` satisfies an inequality
    # against the stale value without pinning what actually happened. The
    # outcome is that the generation went cold, and that is what is asserted.
    assert "contested" not in period, (
        "the discard wrote back a value that predates both builds; the key "
        f"holds {period.get('contested')!r}")
    assert not any(module._codex_source_caches()), (
        "a refused base takes all eleven caches cold together; see "
        "`_drop_codex_source_generation`")


def test_a_stale_overlay_left_on_a_thread_is_discarded_before_the_next_one(env):
    """Capture and build are two calls, so a caller can abandon one."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    with module.codex_path_scope() as path_scope:
        abandoned = module.capture_codex_source_state(
            context, path_scope=path_scope)
        assert abandoned.overlay.open is True
        second = module.capture_codex_source_state(
            context, path_scope=path_scope)
        assert abandoned.overlay.open is False, (
            "the abandoned overlay must be discarded, not left journalling")
        assert second.overlay.open is True
        module.build_codex_source_state_from_capture(
            second, data_version="two", path_scope=path_scope)


def test_a_stale_overlay_whose_base_moved_is_dropped_rather_than_replayed(env):
    """The fifth discard path has to take the epoch decision as well.

    Four of the five reachable discard paths compare the publish epoch before
    replaying a journal; opening the next overlay did not. A thread captures
    and never builds, another build publishes, and the same thread captures
    again: replaying the abandoned journal writes prior values that predate
    BOTH builds over keys the winner republished, which is a stale hit rather
    than a rollback. Only `bin/_cctally_tui.py:3868`/`:4030` splits capture
    from build today and nothing else publishes into these module globals in
    that process, so it is not reachable in production — it is the same defect
    the other four paths fixed, on the path that was missed.
    """
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="one")
    period = module._CODEX_PERIOD_VIEW_CACHE
    period["contested"] = ("old",)

    with module.codex_path_scope() as path_scope:
        abandoned = module.capture_codex_source_state(
            context, path_scope=path_scope)
        # The abandoned build touched the key, so its journal holds ("old",).
        period["contested"] = ("mine",)
        # Meanwhile another build publishes a NEWER value for the same key.
        with module._CODEX_SOURCE_PUBLISH_LOCK:
            module._CODEX_SOURCE_PUBLISH_EPOCH += 1
        period["contested"] = ("newer",)

        # Opening the next overlay is what closes the abandoned one.
        second = module.capture_codex_source_state(
            context, path_scope=path_scope)
        assert abandoned.overlay.open is False, (
            "non-vacuity: the abandoned overlay must have been closed here")
        module.build_codex_source_state_from_capture(
            second, data_version="two", path_scope=path_scope)

    assert period.get("contested") != ("old",), (
        "opening the next overlay replayed a journal that predates both "
        "builds over a key the winning build republished")
    assert "contested" not in period, (
        "a stale overlay whose base moved must take the generation cold; the "
        f"key holds {period.get('contested')!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
