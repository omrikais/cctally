"""#834 S2 (#830) — the cause-keyed partial-generation retry state machine.

Plan: ``docs/superpowers/plans/2026-09-13-834-s2-source-recovery-read-model.md``.

The kernel under test is pure and clock-injected: every test supplies its own
instants, so nothing here reads a wall clock and no test is time-dependent.
"""
from __future__ import annotations

import datetime as dt

import pytest

import _lib_dashboard_sources as lds
import _lib_source_retry as retry


UTC = dt.timezone.utc
T0 = dt.datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _tick(state, *, cause, now, provider="codex", data_version="v1"):
    """Run one tick through the kernel and return ``(decision, next_state)``."""
    return retry.plan_partial_retry(
        state,
        provider=provider,
        cause=cause,
        data_version=data_version,
        now=now,
    )


def test_830_the_first_partial_for_a_key_is_always_refused():
    """A newly observed structural cause rebuilds; it is never retained.

    This is the whole point of the change: the defect was a degraded
    generation handed back for the life of the process, so the FIRST tick
    after a partial must construct a replacement.
    """
    decision, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    assert decision.action == "rebuild"
    assert decision.throttled is False
    assert decision.cause == "metadata_incomplete"
    assert state["codex"].first_seen == T0


def test_830_a_repeated_tick_inside_the_deadline_retains():
    """A repeated structural key is throttled, not rebuilt on every tick."""
    _, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    decision, state = _tick(
        state,
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=1),
    )
    assert decision.action == "retain"
    assert decision.throttled is True
    assert decision.deadline == T0 + retry.PARTIAL_RETRY_INTERVAL


def test_830_a_repeated_tick_does_not_extend_the_deadline():
    """The deadline derives from ``first_seen``, which a retain never moves.

    A deadline re-derived from ``now`` on every tick never expires while the
    cause persists, which is the same permanent freeze in a new costume. The
    assertion is on the arming instant itself, not only on the decision: a
    kernel that moved ``first_seen`` forward by less than one interval would
    still answer ``retain`` here and pass a decision-only test.
    """
    _, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    first_seen = state["codex"].first_seen
    for offset in (1, 5, 30, 90):
        decision, state = _tick(
            state,
            cause="codex_metadata_incomplete",
            now=T0 + dt.timedelta(seconds=offset),
        )
        assert decision.action == "retain"
        assert state["codex"].first_seen == first_seen
        assert decision.deadline == first_seen + retry.PARTIAL_RETRY_INTERVAL


def test_830_expiry_is_evaluated_on_both_sides_of_the_boundary():
    """``now >= deadline`` expires; one microsecond earlier does not."""
    _, armed = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    deadline = T0 + retry.PARTIAL_RETRY_INTERVAL

    before, _ = _tick(
        armed,
        cause="codex_metadata_incomplete",
        now=deadline - dt.timedelta(microseconds=1),
    )
    assert before.action == "retain"

    at, rearmed = _tick(armed, cause="codex_metadata_incomplete", now=deadline)
    assert at.action == "rebuild"
    assert rearmed["codex"].first_seen == deadline

    after, _ = _tick(
        armed,
        cause="codex_metadata_incomplete",
        now=deadline + dt.timedelta(seconds=1),
    )
    assert after.action == "rebuild"


def test_830_a_changed_cause_is_a_different_key_that_retries_at_once():
    """A different cause must not inherit the previous key's throttle.

    The armed key is dropped rather than left running beside the new cause: a
    deadline that kept ticking while its own cause was absent would retain the
    first partial after that cause returned, which is the permanent freeze
    this kernel exists to end, reached by a different route.
    """
    _, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    later = T0 + dt.timedelta(seconds=1)
    decision, state = _tick(state, cause="codex_projection_incoherent", now=later)
    assert decision.action == "rebuild"
    assert decision.cause == "projection_incoherent"
    assert "codex" not in state

    returned = later + dt.timedelta(seconds=1)
    decision, state = _tick(
        state, cause="codex_metadata_incomplete", now=returned,
    )
    assert decision.action == "rebuild"
    assert state["codex"].first_seen == returned
    assert state["codex"].cause == "metadata_incomplete"


def test_830_a_changed_data_version_is_a_different_key_that_retries_at_once():
    """The same cause over new evidence is a new situation, not a repeat."""
    _, state = _tick(
        retry.EMPTY_RETRY_STATE,
        cause="codex_metadata_incomplete",
        now=T0,
        data_version="v1",
    )
    later = T0 + dt.timedelta(seconds=1)
    decision, state = _tick(
        state, cause="codex_metadata_incomplete", now=later, data_version="v2",
    )
    assert decision.action == "rebuild"
    assert state["codex"].first_seen == later
    assert state["codex"].data_version == "v2"


def test_830_a_healthy_publish_clears_the_providers_state_entirely():
    """Recovery must not leave a key that throttles the next real partial."""
    _, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    _, state = _tick(
        state,
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=1),
    )
    assert "codex" in state

    state = retry.clear_partial_retry(state, provider="codex")
    assert "codex" not in state

    # And the next partial for the SAME cause is refused rather than retained.
    decision, _ = _tick(
        state,
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=2),
    )
    assert decision.action == "rebuild"


def test_830_a_transient_read_failure_is_never_throttled():
    """A transient cause rebuilds on every tick and arms no deadline."""
    state = retry.EMPTY_RETRY_STATE
    for offset in (0, 1, 2, 3):
        decision, state = _tick(
            state,
            cause="source_ingest_failed",
            now=T0 + dt.timedelta(seconds=offset),
        )
        assert decision.action == "rebuild"
        assert decision.throttled is False
        assert decision.deadline is None
        assert "codex" not in state


def test_830_a_transient_failure_clears_a_structural_key():
    """It must not leave a stale structural key behind to throttle later.

    A transient cause that merely returned ``rebuild`` while preserving the
    prior structural entry would let the structural key's original deadline
    keep running, so the structural cause would be retained the moment it
    returned even though it had not been seen for a whole interval.
    """
    _, state = _tick(
        retry.EMPTY_RETRY_STATE, cause="codex_metadata_incomplete", now=T0,
    )
    _, state = _tick(
        state,
        cause="source_ingest_failed",
        now=T0 + dt.timedelta(seconds=1),
    )
    assert "codex" not in state
    decision, _ = _tick(
        state,
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=2),
    )
    assert decision.action == "rebuild"


def test_830_each_provider_keys_independently():
    """Claude's throttle must not silence Codex's first partial."""
    _, state = _tick(
        retry.EMPTY_RETRY_STATE,
        provider="claude",
        cause="codex_metadata_incomplete",
        now=T0,
    )
    decision, state = _tick(
        state,
        provider="codex",
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=1),
    )
    assert decision.action == "rebuild"
    assert set(state) == {"claude", "codex"}


def test_830_the_cause_normalizer_collapses_a_warning_set_to_one_stable_key():
    """Several warnings give one key, and order must not change it."""
    forward = retry.normalize_partial_cause(
        ("codex_metadata_incomplete", "codex_projection_incoherent"),
    )
    reverse = retry.normalize_partial_cause(
        ("codex_projection_incoherent", "codex_metadata_incomplete"),
    )
    assert forward == reverse
    assert forward != retry.normalize_partial_cause("codex_metadata_incomplete")


def test_830_an_unrecognized_warning_code_keeps_its_own_key():
    """An unknown code is throttled like a structural cause, not merged."""
    first = retry.normalize_partial_cause("something_new")
    second = retry.normalize_partial_cause("something_else")
    assert first != second
    assert first not in retry.TRANSIENT_PARTIAL_CAUSES
    assert second not in retry.TRANSIENT_PARTIAL_CAUSES


def test_830_a_partial_with_no_stated_cause_is_never_retained():
    """The hydrating seed publishes `partial` with no warnings at all.

    A generation that states no cause cannot be shown to be safe to retain, so
    the positive allowlist refuses it and it rebuilds on every tick.
    """
    assert retry.is_retainable_partial_cause(retry.NO_STATED_CAUSE) is False
    decision, state = _tick(retry.EMPTY_RETRY_STATE, cause=(), now=T0)
    assert decision.action == "rebuild"
    assert decision.cause == retry.NO_STATED_CAUSE
    assert "codex" not in state


def test_830_the_kernel_accepts_warning_objects_as_well_as_codes():
    """The tick holds `SourceDashboardWarning`s, not bare strings."""

    class _Warning:
        def __init__(self, code: str) -> None:
            self.code = code

    assert retry.normalize_partial_cause(
        (_Warning("codex_metadata_incomplete"),)
    ) == retry.normalize_partial_cause("codex_metadata_incomplete")


def test_830_the_kernel_refuses_a_naive_clock():
    """Every instant in this repository is timezone-aware."""
    with pytest.raises(ValueError):
        _tick(
            retry.EMPTY_RETRY_STATE,
            cause="codex_metadata_incomplete",
            now=dt.datetime(2026, 9, 13, 12, 0, 0),
        )


def test_830_the_kernel_refuses_an_unknown_provider():
    with pytest.raises(ValueError):
        _tick(
            retry.EMPTY_RETRY_STATE,
            provider="all",
            cause="codex_metadata_incomplete",
            now=T0,
        )


def test_830_the_returned_state_is_immutable_and_the_input_is_untouched():
    """Pure: the caller's state object is never mutated in place."""
    _, first = _tick(
        retry.EMPTY_RETRY_STATE,
        cause="codex_metadata_incomplete",
        now=T0,
        data_version="v1",
    )
    _, second = _tick(
        first,
        cause="codex_metadata_incomplete",
        now=T0 + dt.timedelta(seconds=1),
        data_version="v2",
    )
    assert first["codex"].data_version == "v1"
    assert second["codex"].data_version == "v2"
    with pytest.raises(TypeError):
        first["codex2"] = second["codex"]  # type: ignore[index]


# === Task 10 — the reuse predicate keeps its own promise ====================


def _reuse_state(availability, freshness, *, source="codex", version="v1"):
    """One provider state in an arbitrary availability/freshness combination."""
    return lds.SourceDashboardState(
        source=source,
        availability=availability,
        freshness=freshness,
        warnings=(),
        data_version="" if availability == "unavailable" else version,
        last_success_at=None,
        capabilities={},
        data=None if availability == "unavailable" else {"hero": {}},
    )


@pytest.mark.parametrize("freshness", ("fresh", "stale"))
def test_830_no_partial_provider_passes_the_coherence_predicate(freshness):
    """A degraded generation is never handed back, whatever its freshness.

    ``reuse_coherent_source_state``'s docstring has always said a stale or
    partial object does not qualify. It did not hold: the predicate admitted
    ``partial``, every fresh Codex build sets ``freshness="fresh"``, and the
    reuse version is an identity digest that a read failure does not move — so
    the degraded object was handed back for the life of the process.
    """
    prior = _reuse_state("partial", freshness)
    assert lds.reuse_coherent_source_state(prior, data_version="v1") is None


@pytest.mark.parametrize(
    "availability,freshness,reusable",
    (
        ("ok", "fresh", True),
        ("empty", "fresh", True),
        ("partial", "fresh", False),
        ("unavailable", "stale", False),
        ("ok", "stale", False),
        ("empty", "stale", False),
        ("partial", "stale", False),
    ),
)
def test_830_reuse_admits_exactly_the_intact_fresh_generations(
    availability, freshness, reusable,
):
    """Every availability and freshness combination the tree can produce."""
    prior = _reuse_state(availability, freshness)
    reused = lds.reuse_coherent_source_state(prior, data_version="v1")
    assert (reused is prior) is reusable


def test_830_composition_still_admits_a_fresh_partial_provider():
    """The two predicates are deliberately different, and must stay so.

    ``_coherent_provider`` answers a question about COMPOSITION: whether a
    generation's data may contribute to the All source's combined figure and
    its aggregates. A fresh partial provider's data is real, and #556 S2 §3.7
    settled that withholding a whole cross-provider ranking over incomplete
    Codex project metadata would discard it. Narrowing that predicate instead
    of the reuse one would turn `account_scope_unresolved` into the blunter
    `provider_incoherent` and withhold both aggregates on every install whose
    Codex project metadata is incomplete.
    """
    prior = _reuse_state("partial", "fresh")
    assert lds._coherent_provider(prior) is True
    assert lds._reusable_provider(prior) is False


def test_830_a_non_retainable_cause_is_never_throttled():
    """Retention is a positive allowlist, and an unknown cause fails closed."""
    for cause in (
        "codex_account_scope_unresolved",
        "codex_projection_incoherent",
        "codex_cycle_unavailable",
        "something_nobody_classified",
    ):
        state = retry.EMPTY_RETRY_STATE
        for offset in (0, 1, 2):
            decision, state = _tick(
                state, cause=cause, now=T0 + dt.timedelta(seconds=offset),
            )
            assert decision.action == "rebuild", cause
            assert decision.throttled is False, cause
            assert "codex" not in state, cause


def test_830_a_composite_key_is_retainable_only_if_every_member_is():
    """One non-retainable member disqualifies the whole generation."""
    mixed = retry.normalize_partial_cause(
        ("codex_metadata_incomplete", "codex_account_scope_unresolved"),
    )
    assert retry.is_retainable_partial_cause(mixed) is False
    decision, state = _tick(retry.EMPTY_RETRY_STATE, cause=mixed, now=T0)
    assert decision.action == "rebuild"
    assert "codex" not in state


# === The browser gate's P0 — a retryable carrier is not reusable either =====


def _health_state(health, *, availability="ok", version="v1"):
    """A coherent generation carrying one metadata-health result."""
    return lds.SourceDashboardState(
        source="codex",
        availability=availability,
        freshness="fresh",
        warnings=(),
        data_version=version,
        last_success_at=None,
        capabilities={},
        data={"hero": {}},
        metadata_health=health,
    )


def test_830_a_transient_health_generation_is_not_reusable():
    """`availability` does not carry a detail-probe read failure, so reuse must.

    Found by the real-browser gate, not by this module's earlier tests. A
    transient metadata-health failure leaves `availability` at `ok`, because
    that field turns `partial` only on incomplete accounting rows, a failed
    hero projection or an unreadable account registry. The generation was
    therefore admitted for reuse and republished unexamined: measured in the
    browser, the state survived 4 minutes 47 seconds across roughly twenty
    published generations with zero authoritative rebuilds, while the note on
    screen promised a retry on the next refresh and the payload said
    `retryable: true`.
    """
    prior = _health_state(lds.build_metadata_health("transient_read_failure"))
    assert prior.availability == "ok", (
        "the fixture no longer reproduces the defect: a transient probe "
        "failure must leave availability at ok, or this test would pass "
        "against the partial clause instead of the health clause")
    assert lds._reusable_provider(prior) is False
    assert lds.reuse_coherent_source_state(prior, data_version="v1") is None


def test_830_a_healthy_generation_stays_reusable():
    """The counterexample: the new clause must not refuse every carrier.

    Without this, a clause that refused any non-null `metadata_health` would
    pass the test above while rebuilding the Codex source on every tick of a
    completely healthy install.
    """
    prior = _health_state(lds.build_metadata_health("healthy"))
    assert lds._reusable_provider(prior) is True
    assert lds.reuse_coherent_source_state(prior, data_version="v1") is prior


def test_830_a_malformed_row_generation_stays_reusable_on_its_own_axis():
    """`malformed_row_partial` is refused for being partial, never for retrying.

    It is deliberately NOT retryable: rebuilding the cache clears it and
    refreshing the dashboard does not, so rebuilding the source on every tick
    would buy nothing. The generation that carries it is refused anyway,
    because the accounting capture that counted those rows also sets
    `availability` to `partial`. This pins WHICH clause does the refusing, so
    a later change to either one cannot silently rely on the other.
    """
    health = lds.build_metadata_health("malformed_row_partial", incomplete_rows=3)
    assert health["retryable"] is False

    on_ok = _health_state(health)
    assert lds._reusable_provider(on_ok) is True, (
        "a malformed-row carrier must not be refused by the retryable clause")

    as_published = _health_state(health, availability="partial")
    assert lds._reusable_provider(as_published) is False


# === The idle path's copy of the same refusal ===============================
#
# Refusing reuse in `_reusable_provider` is not sufficient, and the browser
# gate is what established that. The idle tick never calls
# `reuse_coherent_source_state`: it decides eligibility in
# `_cctally_tui._tui_source_bundle_can_idle` and then republishes the prior
# generation through the two clock refreshes. So a retryable carrier has to be
# refused in both places or the defect survives on the path that produced it.


def _idle_bundle(codex_health):
    """A bundle whose providers are both idle-eligible but for the carrier."""
    import _cctally_tui as tui

    def provider(source, health=None):
        return lds.SourceDashboardState(
            source=source,
            availability="ok",
            freshness="fresh",
            warnings=(),
            data_version="v1",
            last_success_at=None,
            capabilities={},
            data={"hero": {}},
            metadata_health=health,
        )

    claude = provider("claude")
    codex = provider("codex", codex_health)
    bundle = lds.SourceDashboardBundle(
        source_schema_version=lds.SOURCE_SCHEMA_VERSION,
        default_source=lds.DEFAULT_SOURCE,
        source_order=lds.SOURCE_ORDER,
        sources={
            "claude": claude,
            "codex": codex,
            # The bundle requires all three selections, and the idle gate reads
            # only the two physical ones.
            "all": lds.compose_all_state(claude, codex),
        },
    )
    return tui, bundle


def test_830_idle_reuse_refuses_a_retryable_carrier():
    """The idle tick must fall through to a rebuild, not refresh the clock.

    Found by the browser gate reading the path the first fix did not reach.
    `availability` is `ok` here on purpose: a detail-probe read failure never
    sets `partial`, so every other leg of the idle gate passes and this is the
    only one that can refuse. Without it the transient generation is
    republished for the life of the process, which is exactly what was
    measured before the fix.
    """
    tui, bundle = _idle_bundle(lds.build_metadata_health("transient_read_failure"))
    assert bundle.sources["codex"].availability == "ok", (
        "the fixture no longer reproduces the defect: the carrier must be the "
        "only disqualifying leg, or this test says nothing about it")
    assert tui._tui_source_bundle_can_idle(bundle) is False


def test_830_idle_reuse_still_admits_a_healthy_bundle():
    """The counterexample: a healthy install must still take the idle path.

    A leg that refused any carrier would pass the test above while forcing a
    full source rebuild on every idle tick of a completely healthy install,
    which is a worse regression than the defect it set out to fix.
    """
    tui, bundle = _idle_bundle(lds.build_metadata_health("healthy"))
    assert tui._tui_source_bundle_can_idle(bundle) is True


def test_830_idle_reuse_admits_a_bundle_with_no_carrier_at_all():
    """A provider describing no Codex metadata is not a degraded provider.

    `metadata_health` is None on a Claude-only generation and on any provider
    published before the carrier existed. Treating absent as degraded would
    make every such install rebuild on every tick.
    """
    tui, bundle = _idle_bundle(None)
    assert tui._tui_source_bundle_can_idle(bundle) is True
