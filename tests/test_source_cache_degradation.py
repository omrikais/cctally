"""Segmented LRU degradation instead of clearing every Codex source cache.

#769 S6 T2a, #716 Task B. Both source caps cleared everything when exceeded, so
a population permanently above a cap rebuilt and discarded on every generation.
That is not a hypothetical: on the sealed #786 v3 `current` corpus the 768 MiB
accelerator ceiling fired once during a single run and evicted 222,932 entries
at a stroke, and the background verifier that produced the verdict never
completed a generation, so the caches were admitted against a byte figure that
stayed at zero until the clear.

Eviction is now byte-weighted and least-recently-used over the entries the
caches already key by stable semantic units — account, parent, project, session
and physical row id. Cold buckets go until the total is within budget, and
everything else stays resident, so an above-cap steady state rebuilds only what
was evicted.

Eviction affects ACCELERATION ONLY. Every miss rebuilds from the current
immutable accounting generation, so privacy, attribution, ordering, envelope
bytes and latest-wins output are unaffected, and the last test here is what
holds that claim to a byte comparison rather than to an argument.
"""
from __future__ import annotations

import sys

import pytest

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)
from test_snapshot_bounded_work import (  # noqa: E402
    _broaden_corpus,
    _widen_corpus,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _widen_corpus(cache)
    shape = _broaden_corpus(cache)
    assert shape["rows"] >= 40 and shape["accounts"] > 1, shape
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


def _resident(module):
    return sum(len(cache) for cache in module._codex_source_caches())


# ── a cap evicts what does not fit, not everything ────────────────────────

def test_an_above_cap_population_keeps_everything_that_fits(env, monkeypatch):
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    for index in range(12):
        module._CODEX_PERIOD_VIEW_CACHE[f"bucket-{index}"] = ("payload",) * 8
    total = module.codex_source_retained_bytes()
    assert total > 0

    # A budget that fits roughly half of them.
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", total // 2)
    module.enforce_codex_source_accelerator_bounds()

    remaining = _resident(module)
    assert 0 < remaining < 12, (
        f"{remaining} of 12 buckets survived; a cap must evict what does not "
        "fit rather than discarding the whole cache")
    assert module.codex_source_retained_bytes() <= total // 2


def test_eviction_takes_the_least_recently_used_bucket_first(env, monkeypatch):
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    module._CODEX_PERIOD_VIEW_CACHE["oldest"] = ("payload",) * 8
    module._CODEX_PERIOD_VIEW_CACHE["newest"] = ("payload",) * 8
    # Reading the older bucket makes it the recently used one.
    module._CODEX_PERIOD_VIEW_CACHE.touch("oldest")

    total = module.codex_source_retained_bytes()
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", total // 2)
    module.enforce_codex_source_accelerator_bounds()

    assert "oldest" in module._CODEX_PERIOD_VIEW_CACHE
    assert "newest" not in module._CODEX_PERIOD_VIEW_CACHE


def test_an_entry_larger_than_the_whole_budget_is_never_admitted(
    env, monkeypatch,
):
    """NEVER admitted, not admitted and then evicted on the next pass.

    The distinction is the whole of Task 12 step 1's "an indivisible oversized
    result is computed without admission". Writing the entry and then running
    an admission pass proves admit-then-evict: between the two the cache is
    over the ceiling, and the pass that finds it works coldest-first, so it
    evicts other buckets before it reaches the one that caused the overflow.
    The cap is therefore set BEFORE the write here, and the assertion is that
    the write itself did not retain it.
    """
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    module._CODEX_PERIOD_VIEW_CACHE["cheap-neighbour"] = ()
    neighbour_bytes = module._CODEX_PERIOD_VIEW_CACHE.retained_owner_bytes
    assert neighbour_bytes > 0
    # A budget the neighbour fits inside and the oversized entry cannot.
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", neighbour_bytes * 2)
    refusals_before = module.codex_source_accelerator_memory_stats()[
        "admissionRefusalCount"]

    module._CODEX_PERIOD_VIEW_CACHE["huge"] = ("payload",) * 512

    assert "huge" not in module._CODEX_PERIOD_VIEW_CACHE, (
        "an entry larger than the whole budget must not be retained at all")
    assert module._CODEX_PERIOD_VIEW_CACHE.retained_owner_bytes == (
        neighbour_bytes), "a refused entry must leave no charge behind"
    assert module.codex_source_accelerator_memory_stats()[
        "admissionRefusalCount"] == refusals_before + 1
    assert "cheap-neighbour" in module._CODEX_PERIOD_VIEW_CACHE, (
        "refusing the oversized entry must not cost a neighbour that fits")


def test_a_resident_bucket_above_the_budget_is_still_evicted_and_counted(
    env, monkeypatch,
):
    """The pass-level backstop, for a bucket that was resident before the cap."""
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    module._CODEX_PERIOD_VIEW_CACHE["huge"] = ("payload",) * 64
    monkeypatch.setattr(module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", 8)
    before = module.codex_source_accelerator_memory_stats()["fallbackCount"]

    module.enforce_codex_source_accelerator_bounds()

    assert "huge" not in module._CODEX_PERIOD_VIEW_CACHE
    observed = module.codex_source_accelerator_memory_stats()
    assert observed["fallbackCount"] == before + 1
    assert observed["estimatedBytes"] <= 8


def test_a_self_reported_owner_carries_real_recency_and_real_bytes(env):
    """The defect: both self-reported owners always sorted as the coldest.

    `_charge` returned before stamping recency when an owner declared no charge
    function, and `eviction_candidates` reports the MIXIN's per-key bytes, which
    are zero for an owner keeping its ledger beside the mapping. So every key of
    the quota memo and of the visible population reported `recency=0, bytes=0`,
    and the two most expensive caches in the process sorted ahead of everything
    else however recently they had been used.
    """
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    quota = module._CODEX_QUOTA_OBSERVATION_CACHE
    quota["quota-key"] = ("observation",)
    module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["quota-key"] = 4096
    module._CODEX_QUOTA_OBSERVATION_CACHE_BYTES = 4096

    recency = dict(
        (key, value) for key, value in quota._cache_entry_recency.items())
    assert recency.get("quota-key", 0) > 0, (
        "a self-reported owner must stamp recency on its writes")

    owner = next(
        o for o in module._CODEX_SOURCE_CACHE_OWNERS
        if o.name == "quota_observations")
    assert owner.entry_bytes("quota-key") == 4096, (
        "a self-reported owner's per-key charge must come from its own ledger")


def test_the_coldest_bucket_is_evicted_first_across_owners(env, monkeypatch):
    """A charged owner and BOTH self-reported owners in one eviction order.

    The prior regression used only `_CODEX_PERIOD_VIEW_CACHE`, a charged owner,
    so it could not see the defect above at all. Here the genuinely coldest
    bucket is written first and never touched again, and the two self-reported
    owners are used afterwards; the coldest must go first and the visible
    population must not be thrown away ahead of an older, cheaper bucket.
    """
    _ns, cache, stats, module = env
    module.reset_codex_source_caches()

    # Coldest: written first, never read again.
    module._CODEX_PERIOD_VIEW_CACHE["stale-period"] = ("payload",) * 8
    # Then a real build, which populates the visible population and every
    # other accelerator, so the population is the most recently used thing.
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="eviction-order")
    # And a quota memo entry used most recently of all.
    quota = module._CODEX_QUOTA_OBSERVATION_CACHE
    quota["quota-hot"] = ("observation",)
    module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["quota-hot"] = 64
    module._CODEX_QUOTA_OBSERVATION_CACHE_BYTES = 64
    quota.move_to_end("quota-hot")

    assert module._CODEX_VISIBLE_POPULATION_CACHE.get("signature") is not None, (
        "non-vacuity: the build must have retained a visible population")

    order = _eviction_order(module)
    assert order, "non-vacuity: something must be evictable"
    first_owner, first_key = order[0]
    assert (first_owner, first_key) == ("period_views", "stale-period"), (
        f"the coldest bucket must be evicted first; order was {order[:4]}")

    positions: dict[str, int] = {}
    for index, (name, _key) in enumerate(order):
        positions.setdefault(name, index)
    assert positions["period_views"] < positions["visible_population"], (
        "the visible population was evicted ahead of an older, cheaper bucket")
    assert positions["visible_population"] < positions["quota_observations"], (
        "the most recently used quota entry must be the last thing to go")


def test_a_reused_population_is_aged_by_its_newest_field_not_its_oldest(env):
    """`max` over the indivisible owner's fields, and the case that tells them apart.

    Every field of the visible-population state is written by ONE `update` and
    then only the reuse path touches one of them, so aging the bucket by its
    COLDEST field reports the write time forever and sorts a population in
    constant use ahead of buckets nothing has read since.

    The test above cannot see that, and neither could the helper as it was
    first written: in a single build every field of the state is stamped within
    the same tick, so `min` and `max` return recencies that sort identically
    against anything written before or after the whole build. The one
    arrangement the two expressions disagree about is a REUSE that lands after
    a colder bucket was written, which is exactly the production shape — the
    clean-hit path calls `state.touch("entries")` on every tick that reuses the
    population.
    """
    _ns, cache, stats, module = env
    module.reset_codex_source_caches()
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="aging")
    state = module._CODEX_VISIBLE_POPULATION_CACHE
    assert state.get("signature") is not None, (
        "non-vacuity: the build must have retained a visible population")

    # One charged bucket, written AFTER the build, so it is newer than every
    # field the build stamped on the population.
    module._CODEX_PERIOD_VIEW_CACHE.clear()
    module._CODEX_PERIOD_VIEW_CACHE["written-after-the-build"] = (
        ("payload",) * 8)
    # ...and then the population is REUSED. `_cached_codex_visible_rows` reports
    # a clean hit exactly this way.
    state.touch("entries")

    order = _eviction_order(module)
    positions: dict[str, int] = {}
    for index, (name, _key) in enumerate(order):
        positions.setdefault(name, index)
    assert "visible_population" in positions, (
        f"non-vacuity: the population must be evictable; order was {order}")
    assert positions["period_views"] < positions["visible_population"], (
        "the reused population was evicted ahead of a bucket written after it "
        f"and never read, so the bucket is aged by its coldest field; order "
        f"was {order[:6]}")


def _eviction_order(module):
    """`(owner_name, key)` coldest-first, AS `_evict_codex_source_buckets` takes them.

    THE REAL EVICTOR DOES THE SORTING. An earlier version of this helper
    rebuilt the candidate list and re-stated the ordering rule here —
    `max(recency ...)` for the indivisible owner included — so it compared the
    product against a copy of itself: reverting `max` to `min` in
    `bin/_cctally_dashboard_sources.py` left every assertion built on it green.
    The bundle test in this same file already refuses that pattern, in the
    words "deliberately NOT the expression that constructs the field", and this
    helper broke the principle the other one states.

    It works by lowering the ENTRY cap one bucket at a time and watching what
    leaves. The byte budget is untouched, so each pass evicts exactly the
    coldest remaining bucket; the indivisible owner leaves whole and is
    reported once as `<WHOLE OWNER>`. It is destructive — it empties the caches
    it measures — so it is the last thing a test calls.
    """
    caches = module._codex_source_caches()
    owners = module._CODEX_SOURCE_CACHE_OWNERS

    def resident():
        return {(owner.name, key)
                for owner in owners for key in tuple(owner.cache)}

    saved_max_entries = module._CODEX_SOURCE_ACCELERATOR_MAX_ENTRIES
    order: list[tuple[str, object]] = []
    try:
        while True:
            before = resident()
            if not before:
                break
            module._CODEX_SOURCE_ACCELERATOR_MAX_ENTRIES = (
                sum(len(cache) for cache in caches) - 1)
            module._evict_codex_source_buckets(caches)
            removed = before - resident()
            if not removed:
                break
            names = {name for name, _key in removed}
            if len(removed) > 1 and len(names) == 1:
                order.append((next(iter(names)), "<WHOLE OWNER>"))
            else:
                order.extend(sorted(removed, key=lambda item: str(item)))
    finally:
        module._CODEX_SOURCE_ACCELERATOR_MAX_ENTRIES = saved_max_entries
    return order


def test_a_memo_hit_makes_its_entry_the_last_thing_evicted(env):
    """`move_to_end` is the quota memo's ONLY signal that an entry was used.

    The regression that was supposed to hold the recency stamp wrote its memo
    key immediately before calling `move_to_end` on it, so that key was the
    newest thing in the process whether or not the reordering stamped anything:
    deleting the stamp left the estate green. Here the entry that gets the hit
    is written FIRST and a second entry is written after it, so only the stamp
    can move it to the end of the eviction order.
    """
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    quota = module._CODEX_QUOTA_OBSERVATION_CACHE
    quota["hit-me"] = ("observation",)
    module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["hit-me"] = 64
    quota["written-later"] = ("observation",)
    module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["written-later"] = 64
    module._CODEX_QUOTA_OBSERVATION_CACHE_BYTES = 128

    quota.move_to_end("hit-me")

    order = _eviction_order(module)
    assert order == [
        ("quota_observations", "written-later"),
        ("quota_observations", "hit-me"),
    ], (f"a memo hit must be the last thing evicted; order was {order}. "
        "`_ObservedOrderedDict.move_to_end` stamps recency because a "
        "reordering IS the memo saying which entry it just used.")


def test_a_self_reported_owner_reports_its_oversize_to_the_evictor(
    env, monkeypatch,
):
    """`bucket_bytes > budget` has to be able to fire for a self-reported owner.

    The mixin reports zero bytes for every key of an owner that keeps its byte
    ledger beside the mapping, so the evictor asks the OWNER instead. Nothing
    drove that branch through the evictor before: only `_CodexCacheOwner`'s own
    method was asserted, and with the mixin's zero the oversize is invisible
    and the pass reports no fallback at all.
    """
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    quota = module._CODEX_QUOTA_OBSERVATION_CACHE
    quota["huge-window"] = ("observation",)
    module._CODEX_QUOTA_OBSERVATION_CACHE_SIZES["huge-window"] = 4096
    module._CODEX_QUOTA_OBSERVATION_CACHE_BYTES = 4096
    assert quota._cache_entry_bytes.get("huge-window", 0) == 0, (
        "non-vacuity: the mixin must report zero for a self-reported owner, "
        "which is the whole reason the evictor asks the owner")
    monkeypatch.setattr(module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", 8)
    before = module.codex_source_accelerator_memory_stats()["fallbackCount"]

    module.enforce_codex_source_accelerator_bounds()

    assert "huge-window" not in quota
    observed = module.codex_source_accelerator_memory_stats()
    assert observed["fallbackCount"] == before + 1, (
        "the evictor weighed a self-reported owner's bucket by the mixin's "
        "zero, so an oversized bucket could never be recognised there")


def test_a_large_population_is_evicted_past_the_budget_to_the_low_water_mark(
    env, monkeypatch,
):
    """The low-water margin, executed rather than only declared.

    `_CODEX_SOURCE_ACCELERATOR_LOW_WATER` applies only above
    `_CODEX_SOURCE_ACCELERATOR_LOW_WATER_MIN_ENTRIES` residents, and every
    other test in this file holds two orders of magnitude fewer entries than
    that, so the branch credited with the measured 98.5 ms to 64.1 ms
    improvement was executed by nothing in the estate. This crosses the gate.
    """
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    residents = int(module._CODEX_SOURCE_ACCELERATOR_LOW_WATER_MIN_ENTRIES) + 64
    for index in range(residents):
        module._CODEX_PERIOD_VIEW_CACHE[f"bucket-{index:05d}"] = ("payload",)
    assert _resident(module) >= int(
        module._CODEX_SOURCE_ACCELERATOR_LOW_WATER_MIN_ENTRIES), (
        "non-vacuity: the margin applies only above the entry floor")
    total = module.codex_source_retained_bytes()
    budget = int(total * 0.98)
    monkeypatch.setattr(module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", budget)

    module.enforce_codex_source_accelerator_bounds()

    after = module.codex_source_retained_bytes()
    low_water = int(budget * module._CODEX_SOURCE_ACCELERATOR_LOW_WATER)
    assert after <= low_water, (
        f"{after} bytes retained against a {budget}-byte budget with a "
        f"{low_water}-byte low-water mark; the pass stopped at the budget, so "
        "the next build is one entry over it again and pays a full candidate "
        "pass over every resident key")
    # A margin, not a clear: the point of evicting past the budget is that the
    # NEXT several passes take the early return, not that the cache empties.
    assert len(module._CODEX_PERIOD_VIEW_CACHE) > residents // 2, (
        "the low-water margin must free headroom, not the whole cache")


def test_an_entry_count_overflow_evicts_rather_than_clearing(env, monkeypatch):
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    for index in range(10):
        module._CODEX_PERIOD_VIEW_CACHE[f"bucket-{index}"] = ("payload",)
    monkeypatch.setattr(module, "_CODEX_SOURCE_ACCELERATOR_MAX_ENTRIES", 4)

    module.enforce_codex_source_accelerator_bounds()

    assert _resident(module) == 4, (
        "an entry-count overflow must evict down to the cap, not to zero")


def test_a_steady_state_above_the_cap_rebuilds_only_what_was_evicted(
    env, monkeypatch,
):
    """The defect: a permanently above-cap population rebuilt everything."""
    _ns, _cache, _stats, module = env
    module.reset_codex_source_caches()
    for index in range(10):
        module._CODEX_PERIOD_VIEW_CACHE[f"bucket-{index}"] = ("payload",) * 8
    total = module.codex_source_retained_bytes()
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", int(total * 0.6))

    module.enforce_codex_source_accelerator_bounds()
    survivors = set(module._CODEX_PERIOD_VIEW_CACHE)
    assert survivors

    # The next generation touches the survivors, as a build reading them does,
    # and admission leaves them alone because the total is inside the budget.
    for key in survivors:
        module._CODEX_PERIOD_VIEW_CACHE.touch(key)
    module.enforce_codex_source_accelerator_bounds()
    assert survivors <= set(module._CODEX_PERIOD_VIEW_CACHE), (
        "a second admission pass evicted buckets that were already within "
        "budget")


# ── eviction changes acceleration only ────────────────────────────────────

def _plain(value):
    """Mappings and sequences as plain containers, IN THE SAME ORDER.

    The source state carries `MappingProxyType`, which the outbound encoder
    does not descend into. Rebuilding it as `dict`/`list` preserves insertion
    order, which is precisely what the byte comparison below is for.
    """
    from types import MappingProxyType

    if isinstance(value, (dict, MappingProxyType)):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _describe_difference(left: bytes, right: bytes) -> str:
    """A readable window around the first differing byte."""
    if left == right:
        return "identical"
    index = next(
        (i for i, (a, b) in enumerate(zip(left, right)) if a != b),
        min(len(left), len(right)))
    lo = max(0, index - 90)
    return (
        f"first difference at byte {index}\n"
        f"  left : ...{left[lo:index + 90]!r}\n"
        f"  right: ...{right[lo:index + 90]!r}")


def _envelope_bytes(state):
    """The published envelope, encoded by the PRODUCTION encoder.

    `degraded.data == warm.data` was the wrong assertion. Mapping equality
    ignores insertion order and JSON serialisation does not, and Task 12 step 1
    names ordering explicitly — so a reordering that changed every published
    byte would have passed. This encodes what the dashboard actually sends.
    """
    from _lib_dashboard_json import encode_dashboard_json_bytes

    return encode_dashboard_json_bytes(_plain({
        "data": state.data,
        "availability": state.availability,
        "freshness": state.freshness,
        "warnings": [
            {"code": w.code, "domain": w.domain} for w in state.warnings],
        "capabilities": {
            name: {"status": cap.status, "semantics": cap.semantics}
            for name, cap in state.capabilities.items()
        },
    }), default=str)


def test_a_partially_evicted_cache_publishes_identical_envelope_bytes(
    env, monkeypatch,
):
    """The new steady state: some buckets gone, most still resident.

    Nothing previously built a published envelope from a PARTIALLY evicted
    cache. The old test monkeypatched the cap to 1 and enforced once after the
    fold, which is a warm build followed by a total wipe, so it proved
    `warm == cold` and never touched the mixed state eviction actually leaves.
    """
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)

    module.reset_codex_source_caches()
    warm = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))

    resident_before = _resident(module)
    assert resident_before > 2, resident_before
    real_budget = module._CODEX_SOURCE_ACCELERATOR_MAX_BYTES
    total = module.codex_source_retained_bytes()
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", int(total * 0.6))
    module.enforce_codex_source_accelerator_bounds()
    survivors = _resident(module)
    assert 0 < survivors < resident_before, (
        f"non-vacuity: {survivors} of {resident_before} survived, so the "
        "cache was not PARTIALLY evicted")

    # Restore the real budget by SETTING it, never `monkeypatch.undo()`: the
    # `env` fixture shares this monkeypatch instance, so undoing here would also
    # undo the data-directory redirection and the `CODEX_HOME` the corpus lives
    # under, and the next build would read a different store.
    monkeypatch.setattr(
        module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", real_budget)
    partial = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))
    assert partial == warm, (
        "a build over a partially evicted cache published different bytes\n"
        + _describe_difference(warm, partial))


def test_an_overlay_discard_publishes_identical_envelope_bytes(env):
    """A failed and rolled-back build must leave the next one byte-identical."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)

    module.reset_codex_source_caches()
    warm = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))

    with module.codex_path_scope() as path_scope:
        capture = module.capture_codex_source_state(
            context, path_scope=path_scope)
        original = module._build_codex_source_state

        def fail(*_args, **_kwargs):
            module._CODEX_PERIOD_VIEW_CACHE["poisoned"] = ("x",) * 8
            raise RuntimeError("synthetic build failure")

        module._build_codex_source_state = fail
        try:
            with pytest.raises(RuntimeError):
                module.build_codex_source_state_from_capture(
                    capture, data_version="discarded", path_scope=path_scope)
        finally:
            module._build_codex_source_state = original

    after = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))
    assert after == warm, (
        "a discarded build changed the bytes the next build published\n"
        + _describe_difference(warm, after))


def test_a_population_signature_miss_publishes_identical_envelope_bytes(env):
    """An unnecessary miss is acceptable; different bytes are not.

    The miss is forced the way production forces one — by regressing the
    accounting ledger head, which is what a rebuilt store looks like and what
    `build_cached_codex_accounting` answers with a cold reconstruction. That
    reconstruction reports the PRIOR population as `changed_old` and the new
    one as `changed_new`, so the derived caches subtract what they held and add
    what arrived. Resetting the accounting state outright instead would leave
    `changed_old` empty against caches still holding a complete generation, and
    that is not a signature miss — see the invariant test below.
    """
    _ns, cache, stats, module = env

    context = _context(module, cache, stats)
    module.reset_codex_source_caches()
    warm = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))
    signature = module._codex_project_population_signature()
    assert signature is not None, (
        "non-vacuity: the build must publish a population signature")

    cache.execute(
        "UPDATE cache_meta SET value='0' "
        "WHERE key='codex_accounting_mutation_seq'")
    cache.commit()
    missed = _envelope_bytes(
        module.build_codex_source_state(context, data_version="reference"))
    assert module._codex_project_population_signature() != signature, (
        "non-vacuity: the forced reconstruction must move the population name")
    assert missed == warm, (
        "a signature miss rebuilt a different population\n"
        + _describe_difference(warm, missed))


def test_resetting_the_accounting_state_alone_is_never_done_alone(env):
    """The coherence rule a partial cache clear breaks, stated as a test.

    Ten of the eleven accelerators are fed by the accounting carrier's DELTA
    lists, not by its population. A reset accounting state reports its next
    reconstruction as cold with `changed_old` EMPTY and `changed_new` the whole
    population, so any derived cache that survived the reset adds the entire
    population to a generation it already holds. Measured: an earlier
    remediation cleared only the caches one build had written to, and the
    published envelope carried `cost_usd` and every token count at twice their
    value.

    So `reset_codex_source_caches` has to do BOTH, and this is what holds it
    there.
    """
    _ns, cache, stats, module = env
    import _lib_snapshot_cache as snapshot

    context = _context(module, cache, stats)
    module.reset_codex_source_caches()
    module.build_codex_source_state(context, data_version="one")
    assert _resident(module) > 0
    assert snapshot._CODEX_ACCOUNTING_CACHE_STATE.get("entries"), (
        "non-vacuity: the accounting state must hold a population")

    module.reset_codex_source_caches()

    assert not snapshot._CODEX_ACCOUNTING_CACHE_STATE.get("entries"), (
        "clearing the accelerators must clear the accounting state they take "
        "their deltas from")
    assert _resident(module) == 0, (
        "and resetting the accounting state must clear every accelerator that "
        "consumes its deltas, in the same call")


def test_the_published_state_is_identical_with_and_without_eviction(
    env, monkeypatch,
):
    """The whole-wipe end of the range, kept beside the partial one above."""
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)

    module.reset_codex_source_caches()
    warm = module.build_codex_source_state(context, data_version="reference")
    warm_bytes = _envelope_bytes(warm)

    module.reset_codex_source_caches()
    monkeypatch.setattr(module, "_CODEX_SOURCE_ACCELERATOR_MAX_BYTES", 1)
    degraded = module.build_codex_source_state(context, data_version="reference")
    module.enforce_codex_source_accelerator_bounds()
    after_eviction = module.build_codex_source_state(
        context, data_version="reference")

    assert _envelope_bytes(degraded) == warm_bytes, (
        "a build under a budget too small to retain anything must publish the "
        "same bytes as one that retained everything\n"
        + _describe_difference(warm_bytes, _envelope_bytes(degraded)))
    assert _envelope_bytes(after_eviction) == warm_bytes


# ── the cap that is deliberately still all-or-nothing ─────────────────────

def test_the_visible_population_owner_is_declared_indivisible_and_evicted_whole(
    env,
):
    """Assert the BEHAVIOUR the reason describes, not the reason's length.

    Asserting `len(reason) > 40` proved that a string existed. What matters is
    that this owner is the only indivisible one, that the evictor really does
    take it as one bucket rather than key by key, and that its cap is still
    all-or-nothing — which is the deviation spec section 5.2 records.
    """
    _ns, cache, stats, module = env
    reason = module.CODEX_VISIBLE_POPULATION_INDIVISIBLE_REASON
    assert isinstance(reason, str) and len(reason) > 40

    indivisible = [
        owner.name for owner in module._CODEX_SOURCE_CACHE_OWNERS
        if owner.indivisible]
    assert indivisible == ["visible_population"], indivisible

    module.reset_codex_source_caches()
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="indivisible")
    state = module._CODEX_VISIBLE_POPULATION_CACHE
    assert state.get("signature") is not None, (
        "non-vacuity: the build must have retained a visible population")
    assert len(state) > 1, (
        "non-vacuity: the state must hold several fields for the whole-bucket "
        "claim to mean anything")

    owner = next(
        o for o in module._CODEX_SOURCE_CACHE_OWNERS
        if o.name == "visible_population")
    assert owner.evict_all() == len(state) or not state
    assert not state, (
        "an indivisible owner must be evicted whole, not key by key")


def test_the_visible_population_cap_is_still_all_or_nothing(env, monkeypatch):
    """The recorded deviation, held to the behaviour rather than to prose.

    Task 12 step 3 asked for the population to be partitioned along account.
    It is not, and spec section 5.2 records that with the measured cost of the
    rebuild. This test is what makes the record checkable: above the cap the
    fold clears the whole state and returns a complete cold fold, and it does
    NOT keep the accounts that would still have fitted.
    """
    _ns, cache, stats, module = env
    context = _context(module, cache, stats)
    module.reset_codex_source_caches()
    module.build_codex_source_state(context, data_version="one")
    assert module._CODEX_VISIBLE_POPULATION_CACHE.get("signature") is not None

    # A clean hit returns the prior fold without consulting the cap at all,
    # so the cap is only reachable from a cold or incremental fold. Moving the
    # accounting population's name is what forces one.
    import _lib_snapshot_cache as snapshot

    snapshot.reset_codex_accounting_cache_state()
    monkeypatch.setattr(module, "_CODEX_VISIBLE_POPULATION_MAX_BYTES", 1)
    module.build_codex_source_state(context, data_version="two")

    state = module._CODEX_VISIBLE_POPULATION_CACHE
    assert state.get("signature") is None, (
        "above its cap this fold retains nothing at all")
    assert int(state.get("fallback_count", 0)) > 0, (
        "the all-or-nothing fallback must be counted where the deviation "
        "record can cite it")
    assert not state.get("rows_by_account"), (
        "no per-account partition survives, which is exactly what makes this "
        "cap all-or-nothing")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
