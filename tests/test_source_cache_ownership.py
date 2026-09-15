"""The retained-byte ownership graph for the Codex source caches.

#769 S6 T2a, #716 Task B. The retained-size oracle counts a shared object
identity ONCE across everything it walks. A sum of per-cache charges only
agrees with that if each shared payload has exactly one cache carrying its
charge, and the source caches share three payload classes: accounting rows,
adapted `CodexEntry` values, and the labelled row copies the project label
cache produces. The graph in `bin/_cctally_dashboard_sources.py` names the one
owner of each, and these tests are what hold it to that claim.

Both directions are asserted, because only one of them is bounded.

UPWARD. A payload charged twice inflates the running total, and the total is
what admission compares against its budget, so a double charge evicts a cache
that was never over budget. No owner's charge may exceed one walk of its own
cache, and the sum may not exceed one walk of the whole graph.

DOWNWARD. FOUR owners are charged in CLOSED FORM and deliberately understate,
and nothing in the product bounds by how much — so a budget compared against
the sum is a heuristic over declared charges and NOT a cap on memory. An
`incremental <= whole_graph` assertion alone permits an owner that charges
zero, which is why the fidelity test below holds every WALKED owner to a stated
lower bound as well, and pins the closed-form set to exactly the four named in
`CODEX_SOURCE_CHARGE_FIDELITY`. A fifth owner going closed-form without saying
so is what that test exists to catch.
"""
from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from _lib_retained_size import retained_size_bytes  # noqa: E402
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


@pytest.fixture
def built(tmp_path, monkeypatch):
    """One real Codex source build over a population with several of everything.

    `_widen_corpus` alone clones one template row twelve times, so every
    partition this graph reasons about has exactly one bucket and the
    double-charge residual is one string counted thirteen times — far inside
    any bound this file could state. `_broaden_corpus` adds several accounts,
    projects, sessions and models, and two projects that collide on their
    label, so the shared payloads really are shared between distinct owners.
    """
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _widen_corpus(cache)
    shape = _broaden_corpus(cache)
    assert shape["accounts"] > 1 and shape["conversations"] > 1, shape
    module = sys.modules["_cctally_dashboard_sources"]
    module.reset_codex_source_caches()
    context = module.DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START, now_utc=NOW,
        display_tz_name="UTC",
    )
    module.build_codex_source_state(context, data_version="ownership")
    try:
        yield module
    finally:
        module.reset_codex_source_caches()
        cache.close()
        stats.close()


# ── the graph describes the set the build checkpoints ─────────────────────

def test_the_graph_names_every_checkpointed_cache_in_order():
    """Drift here is a desynchronised total, not a cosmetic mismatch.

    `_codex_source_caches` is the one definition of what a build checkpoints
    and what a cold reference discards. An owner list that has drifted from it
    either charges a cache nothing or charges a cache that is not there.
    """
    caches = sources._codex_source_caches()
    owners = sources._CODEX_SOURCE_CACHE_OWNERS
    assert tuple(owner.cache for owner in owners) == caches
    assert len({owner.name for owner in owners}) == len(owners)
    for owner in owners:
        assert owner.owns and owner.borrows, owner.name
        assert (owner.charge is None) != (owner.reader is None), (
            f"{owner.name} must be charged per entry or self-reported, not "
            "both and not neither")


def test_every_declared_charge_is_bound_to_its_cache():
    for owner in sources._CODEX_SOURCE_CACHE_OWNERS:
        assert owner.cache._cache_owner_name == owner.name
        assert owner.cache._cache_owner_charge is owner.charge


# ── the borrowed and opaque seams the graph is built on ───────────────────

def test_a_borrowed_referent_is_charged_nothing_and_not_descended_into():
    payload = ["borrowed"] * 64
    owned = (1, 2, 3)
    container = {"own": owned, "shared": payload}
    lent = retained_size_bytes(container, borrowed=lambda obj: obj is payload)
    assert lent < retained_size_bytes(container)
    # Exactly the container, its two keys and the half it still owns.
    assert lent == (
        sys.getsizeof(container) + sys.getsizeof("own")
        + sys.getsizeof("shared") + retained_size_bytes(owned))


def test_the_borrowed_predicate_is_never_applied_to_the_value_itself():
    """Asking for the size of something you call borrowed is contradictory."""
    payload = ("a", "b")
    assert retained_size_bytes(payload, borrowed=lambda _obj: True) == (
        sys.getsizeof(payload))


def test_an_opaque_container_is_charged_its_shell_and_not_descended():
    rows = tuple(object() for _ in range(32))
    holder = {"rows": rows}
    shallow = retained_size_bytes(holder, opaque=lambda obj: obj is rows)
    assert shallow == (
        sys.getsizeof(holder) + sys.getsizeof("rows") + sys.getsizeof(rows))
    assert shallow < retained_size_bytes(holder)


# ── no double charging, against the existing oracle ───────────────────────

def test_no_payload_is_charged_twice_on_a_built_population(built):
    """The sum of declared charges never exceeds one walk of the whole graph.

    The comparison is deliberately against the UNRESTRICTED oracle over the
    same containers the removed background worker walked. That number counts
    every shared identity once and is therefore an upper bound on any honest
    partition of it. A sum above it is proof that some payload is being paid
    for by more than one owner.
    """
    module = built
    caches = module._codex_source_caches()
    whole_graph = retained_size_bytes(tuple(dict(cache) for cache in caches))
    incremental = module.codex_source_retained_bytes()
    assert incremental > 0, (
        "non-vacuity: a completed build must retain something to charge")
    breakdown = "\n".join(
        f"  {owner.name}: charged {owner.retained_bytes()} against "
        f"{retained_size_bytes(dict(owner.cache))} reachable"
        for owner in module._CODEX_SOURCE_CACHE_OWNERS
    )
    assert incremental <= whole_graph, (
        f"double charging: {incremental} charged against {whole_graph} "
        f"reachable bytes\n{breakdown}")
    for owner in module._CODEX_SOURCE_CACHE_OWNERS:
        reachable = retained_size_bytes(dict(owner.cache))
        assert owner.retained_bytes() <= reachable, (
            f"{owner.name} charges {owner.retained_bytes()} for {reachable} "
            "reachable bytes, so it is paying for one payload more than once")


#: How much of one walk of its own cache a WALKED owner must still charge.
#:
#: IT IS CALIBRATED TO THE OBSERVED POPULATION, NOT DERIVED, and saying so is
#: the point of this comment. An earlier version attributed the whole gap to
#: the shared-string residual described in the product's ownership graph, which
#: INFLATES a per-entry charge rather than deflating it — so on that reason
#: alone the principled floor would be 1.0 and 0.25 would admit a fourfold
#: under-charge as "walked".
#:
#: The real reason a walked owner charges below its own cache is that
#: `_charge_borrowing_payloads` passes `borrowed=_is_borrowed_codex_payload`,
#: so the charge stops at every accounting row and adapted entry, while the
#: reference walk below descends into them. How far below 1.0 that puts an
#: owner depends on how much of its cached value is borrowed payload rather
#: than its own structure, which is a property of the population and not a
#: number this file can derive. Measured on the fixture population this module
#: builds: `period_views` 0.287, `session_views` 0.323, `weekly_views` 0.808,
#: `cache_report_rows` 0.990. The floor sits under the smallest of those with
#: room for ordinary variation.
#:
#: What would justify tightening it is comparing each walked owner against a
#: walk that applies the SAME borrowed predicate the charge applies. The floor
#: would then be principled rather than calibrated, and near 1.0 — at the cost
#: of a reference number derived the same way as the charge it checks, which is
#: why this file has not made that trade.
_WALKED_CHARGE_FLOOR = 0.25


def test_the_charge_fidelity_declaration_covers_every_owner():
    """Every owner is either walked or a declared closed-form under-charge."""
    fidelity = sources.CODEX_SOURCE_CHARGE_FIDELITY
    names = tuple(owner.name for owner in sources._CODEX_SOURCE_CACHE_OWNERS)
    assert tuple(sorted(fidelity)) == tuple(sorted(names))
    closed = {
        name for name, value in fidelity.items() if value != "walked"}
    assert closed == {
        "project_wire", "entry_adapters", "visible_population",
        "project_labels"}, (
        "a fifth owner became a closed-form under-charge without the "
        "declaration saying so, and the budget is compared against a sum that "
        "includes it")
    for name in closed:
        assert fidelity[name].startswith("closed-form: "), name
        assert len(fidelity[name]) > 60, (
            f"{name} must state WHY it understates, not merely that it does")


def test_a_walked_owner_charges_within_a_two_sided_band(built):
    """The lower half of the band, which `incremental <= reachable` cannot see.

    An owner whose charge function silently stopped descending would keep
    passing the upper bound forever while contributing almost nothing to the
    total the budget reads. Holding the walked owners to a floor is what turns
    that into a failure.
    """
    module = built
    fidelity = module.CODEX_SOURCE_CHARGE_FIDELITY
    observed: dict[str, tuple[int, int]] = {}
    for owner in module._CODEX_SOURCE_CACHE_OWNERS:
        if fidelity[owner.name] != "walked" or not owner.cache:
            continue
        observed[owner.name] = (
            owner.retained_bytes(), retained_size_bytes(dict(owner.cache)))
    assert len(observed) >= 2, (
        f"non-vacuity: only {len(observed)} walked owners held anything, so "
        "the band was not exercised")
    # EVERY owner's ratio is reported, not just the first one to fail. The
    # floor is calibrated against this population, so the number a failure
    # prints is the evidence for re-calibrating it.
    report = ", ".join(
        f"{name} {charged}/{reachable} = {charged / reachable:.3f}"
        for name, (charged, reachable) in sorted(observed.items()))
    over = sorted(
        name for name, (charged, reachable) in observed.items()
        if charged > reachable)
    assert not over, f"double charging in {over}: {report}"
    under = sorted(
        name for name, (charged, reachable) in observed.items()
        if charged < reachable * _WALKED_CHARGE_FLOOR)
    assert not under, (
        f"{under} charge below the floor {_WALKED_CHARGE_FLOOR}, so a walked "
        f"charge stopped descending: {report}")


def test_the_closed_form_owners_gap_is_reported_rather_than_bounded(built):
    """Measure the gap the product declines to bound, and require it non-trivial.

    This is the number spec section 5 quotes. If a future change made one of
    these charges near-exact, the declaration above is wrong and should be
    changed to `walked` — which is what the upper assertion here catches.
    """
    module = built
    fidelity = module.CODEX_SOURCE_CHARGE_FIDELITY
    measured = {}
    for owner in module._CODEX_SOURCE_CACHE_OWNERS:
        if fidelity[owner.name] == "walked" or not owner.cache:
            continue
        reachable = retained_size_bytes(dict(owner.cache))
        measured[owner.name] = (owner.retained_bytes(), reachable)
    assert measured, "non-vacuity: no closed-form owner retained anything"
    for name, (charged, reachable) in measured.items():
        assert charged <= reachable, name
        assert charged < reachable, (
            f"{name} is declared a closed-form under-charge but charged "
            f"{charged} of {reachable} reachable bytes exactly; either the "
            "declaration or the charge is now wrong")


def test_the_accounting_rows_are_charged_by_their_own_carrier(built):
    """Rows are retained by the accounting cache and borrowed everywhere here.

    They are repeated in the visible-population index, in the project groups
    and in the label cache. Charging them at each of those would count one
    population four times over.
    """
    module = built
    rows = module._CODEX_VISIBLE_POPULATION_CACHE.get("rows_by_id") or {}
    assert rows, "non-vacuity: the fold must have indexed the population"
    sample = next(iter(rows.values()))
    assert module._is_codex_accounting_row(sample)
    assert module._is_borrowed_codex_payload(sample)


def test_the_adapter_cache_owns_the_adapted_entry_and_borrows_its_row(built):
    """`(row, CodexEntry)` — one half is charged here and one half is not."""
    module = built
    adapter = module._CODEX_ENTRY_ADAPTER_CACHE
    assert adapter, "non-vacuity: the build must have adapted rows"
    key, value = next(iter(adapter.items()))
    row, entry = value
    charged = module._charge_adapter_entry(key, value)
    assert 0 < charged < retained_size_bytes(value), (
        "the row half must not be charged to the adapter cache")
    assert charged == (
        sys.getsizeof(value) + sys.getsizeof(entry)
        + sys.getsizeof(entry.cost_usd)), (
        "the adapted entry shares every string with its row, so its charge is "
        "the two shells and the one float the adapter constructs")
    assert row is value[0]


def test_the_label_cache_owns_its_copies_without_recharging_their_rows():
    """A labelled copy shares every field but one with the row it replaces."""
    module = sources
    row = _sample_row()
    labelled = replace(row, display_label="alpha")
    value = {
        "pairs": frozenset({(str(row.project_key), str(row.project_label))}),
        "labels": {str(row.project_key): "alpha"},
        "entries": {int(row.cache_entry_id): (row, labelled)},
    }
    charged = module._charge_project_label_entry("key", value)
    assert charged > 0
    assert charged < retained_size_bytes(value), (
        "charging the raw row here would duplicate the accounting carrier")
    assert charged >= sys.getsizeof(labelled)


def test_the_project_wire_groups_cost_shells_not_their_populations():
    """A tick must not become proportional to the whole population again."""
    module = sources
    rows = tuple(_sample_row(index) for index in range(64))
    small = ("signature", {"rows": ()}, {("root", "p"): rows[:1]})
    large = ("signature", {"rows": ()}, {("root", "p"): rows})
    small_charge = module._charge_project_wire_entry("k", small)
    large_charge = module._charge_project_wire_entry("k", large)
    # The tuple shell grows by eight bytes a slot and nothing else does.
    assert large_charge - small_charge == (
        sys.getsizeof(rows) - sys.getsizeof(rows[:1]))


def _sample_row(index: int = 1):
    import datetime as dt

    from _lib_source_analytics import QualifiedCodexEntry

    return QualifiedCodexEntry(
        timestamp=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        session_id=f"session-{index}",
        model="gpt-5",
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_output_tokens=0,
        total_tokens=2,
        source_path=f"/rollouts/{index}.jsonl",
        cost_usd=0.5,
        project_key=f"project-{index}",
        project_label=f"label-{index}",
        display_label=f"label-{index}",
        source_root_key="root",
        conversation_key=f"conversation-{index}",
        cache_entry_id=index,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
