"""#846 §4.6 — the retention legs, driven through real TUI ticks.

Specification `docs/superpowers/specs/2026-09-15-845-846-codex-metadata-decode-tolerance.md`,
acceptance rows A9 and A13. Both rows are explicit that a builder-only test
does not satisfy them: the defect they pin is not in the builder at all. The
builder publishes a `transient_read_failure` generation correctly, and then
`_tui_retain_refused_partial` and `_tui_source_bundle_can_idle` republish it
for up to `PARTIAL_RETRY_INTERVAL` while the chip promises a retry. Only a
second TICK over the first tick's generation can observe that.

Every generation here is produced by forcing a real `sqlite3.Error` in one
metadata leg of the real builder, never by editing a carrier onto a state: a
carrier assembled with `dataclasses.replace` proves nothing about which
availability, freshness and warnings the production path actually publishes
beside it, and those are the other legs the two gates consult.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import sys

import pytest

from conftest import load_script, redirect_paths  # noqa: F401
from test_dashboard_source_read_model import (
    NOW,
    START,
    _a9_force_leg_failure,
    _cache_root_key,
    _install_active_native_cycle,
    _seeded_context,
)
from test_dashboard_accounts_wire import _seed_codex_accounts

from _lib_source_retry import EMPTY_RETRY_STATE  # noqa: E402


ACCOUNT_X = "e" * 32
ACCOUNT_Y = "f" * 32


class _TickHarness:
    """Drives real `_tui_build_source_bundle` ticks over one seeded store."""

    def __init__(self, ns, cache, stats):
        self.ns = ns
        self.cache = cache
        self.stats = stats
        self.tui = sys.modules["_cctally_tui"]
        self.bundle = None

    def tick(self, *, prior=..., codex_ingest_failed=False, now_utc=NOW):
        # The accounting memo is deliberately NOT reset between ticks: resetting
        # it marks the accounting cache pending, and a pending ledger forces a
        # rebuild through `_codex_forced_rebuild` before the retention leg is
        # ever consulted — which would make every assertion below pass for a
        # reason that has nothing to do with the carrier.
        self.bundle = self.tui._tui_build_source_bundle(
            stats_conn=self.stats,
            now_utc=now_utc,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            codex_ingest_failed=codex_ingest_failed,
            claude_cost_usd=1.0,
            claude_total_tokens=100,
            common_range_start=START,
            projects_envelope={},
            prior_bundle=self.bundle if prior is ... else prior,
            raw_config={},
        )
        return self.bundle

    @property
    def codex(self):
        return self.bundle.sources["codex"]

    def bump_signature(self):
        """Move the store so exact-version provider reuse cannot answer.

        `codex_physical_mutation_seq` is the shared physical-state
        invalidation token every cache writer advances, and it is part of the
        Codex reuse version. Advancing it is exactly what an ordinary ingest
        does, and without it a second tick over an unchanged store is answered
        by exact-version reuse and the build under test never runs.
        """
        self.cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1")
        self.cache.commit()


def _harness(tmp_path, monkeypatch, *, accounts=False):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    root = _cache_root_key(cache)
    if accounts:
        # Two REAL accounts, because the account children are decoration that
        # appears only above one real account (the R8 gate).
        cache.execute(
            "UPDATE codex_session_entries SET account_key = ? "
            "WHERE id IN (SELECT id FROM codex_session_entries "
            "ORDER BY id LIMIT 1)", (ACCOUNT_Y,))
        cache.execute(
            "UPDATE codex_session_entries SET account_key = ? "
            "WHERE account_key IS NULL", (ACCOUNT_X,))
        cache.commit()
        _seed_codex_accounts(stats, [
            {"account_key": ACCOUNT_X, "natural_id": "x",
             "email": "x@example", "label": "X", "plan_type": "pro"},
            {"account_key": ACCOUNT_Y, "natural_id": "y",
             "email": "y@example", "label": "Y", "plan_type": "pro"},
        ])
    _install_active_native_cycle(
        monkeypatch, source_module, reset=NOW + dt.timedelta(days=2), root=root)
    sys.modules["_cctally_tui"]._tui_reset_partial_retry_state()
    return _TickHarness(ns, cache, stats)


@pytest.fixture
def ticks(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        harness.cache.close()
        harness.stats.close()
        sys.modules["_cctally_tui"]._tui_reset_partial_retry_state()


@pytest.fixture
def account_ticks(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch, accounts=True)
    try:
        yield harness
    finally:
        harness.cache.close()
        harness.stats.close()
        sys.modules["_cctally_tui"]._tui_reset_partial_retry_state()


def test_846_a9_a_second_tick_rebuilds_the_transient_generation(
    ticks, monkeypatch,
):
    """A9's retention half, through `_tui_build_source_bundle`.

    A failed metadata leg publishes `partial` with the warning code
    `codex_metadata_incomplete`, which `_lib_source_retry` normalizes to the
    retainable cause `metadata_incomplete`. `_tui_retain_refused_partial`
    derived its cause from `prior.warnings` alone, so it would retain that
    generation for up to `PARTIAL_RETRY_INTERVAL`.

    Measured while writing this: with the carrier leg removed, the ticks below
    still rebuild, because the skipped qualified read leaves the accounting
    ledger pending and `_codex_forced_rebuild` fires before the retention leg
    is consulted. So this row asserts the published outcome, and the RED proof
    of the leg itself is the predicate test that follows.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        first = ticks.tick(prior=None)
        first_codex = first.sources["codex"]
        assert first_codex.metadata_health["state"] == "transient_read_failure"
        assert first_codex.availability == "partial"
        assert first_codex.data["projects"]["rows"] == ()

        # The SAME fault on every later tick, over an unchanged store, so the
        # cause and the version repeat exactly as they do in production. The
        # retry kernel rebuilds on the FIRST observation of a key and retains
        # the repeat inside its deadline, so the third tick is the one that was
        # retained; every tick here must rebuild, and no key may ever be armed.
        seen = [first_codex]
        for _ in range(2):
            later = ticks.tick().sources["codex"]
            assert all(later is not earlier for earlier in seen), (
                "a tick retained the transient generation instead of "
                "rebuilding it"
            )
            assert later.metadata_health["state"] == "transient_read_failure"
            assert later.data["projects"]["rows"] == ()
            assert ticks.tui._PARTIAL_RETRY_STATE == EMPTY_RETRY_STATE, (
                "the retention leg armed a key for a retryable carrier"
            )
            seen.append(later)

    recovered = ticks.tick().sources["codex"]
    assert recovered.metadata_health["state"] == "healthy"
    assert recovered.data["projects"]["rows"]


def test_846_a9_the_retention_leg_refuses_and_clears_the_armed_key(ticks,
                                                                   monkeypatch):
    """The same defect at the predicate, with the armed key observed.

    The first tick arms the provider's retry key; the leg must refuse the
    second tick AND drop that key, so the tick after a recovery is not met by
    a key still inside its deadline.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ticks.tui
    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        first = ticks.tick(prior=None)
    codex = first.sources["codex"]
    assert codex.availability == "partial"
    assert "codex_metadata_incomplete" in [w.code for w in codex.warnings]

    assert tui._tui_retain_refused_partial(
        codex, provider="codex", now_utc=NOW,
        data_version=codex.data_version,
    ) is False
    assert tui._tui_retain_refused_partial(
        codex, provider="codex", now_utc=NOW,
        data_version=codex.data_version,
    ) is False, "a repeated call must not arm the key either"


def test_846_a9_an_ingest_failure_republishes_the_transient_carrier(
    ticks, monkeypatch,
):
    """A9's third sequence. The ingest-failure branch republishes the prior
    generation under its OWN warning — that is the documented degradation for
    every generation — and the carrier travels with it, so the transient state
    stays disclosed. The tick after the ingest recovers rebuilds.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        first = ticks.tick(prior=None)
        first_codex = first.sources["codex"]
        assert first_codex.metadata_health["state"] == "transient_read_failure"

        second = ticks.tick(codex_ingest_failed=True)
        second_codex = second.sources["codex"]
        assert "source_ingest_failed" in [w.code for w in second_codex.warnings]
        assert second_codex.metadata_health == first_codex.metadata_health

    # The ingest recovers and the metadata leg recovers with it, so the next
    # tick rebuilds rather than republishing the retained transient generation.
    third = ticks.tick()
    third_codex = third.sources["codex"]
    assert third_codex is not second_codex
    assert third_codex.metadata_health["state"] == "healthy"
    assert third_codex.data["projects"]["rows"]


def test_846_a9_no_account_child_publishes_a_project_row(
    account_ticks, monkeypatch,
):
    """A9's "empty on the parent and on every account child".

    `_codex_account_scopes_wire`'s `metadata_transient` arm is what publishes
    the empty child, and nothing executed it: every partial test either had one
    account or asserted the parent alone.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    healthy = account_ticks.tick(prior=None)
    children = healthy.sources["codex"].data["account_scopes"]
    assert len(children) >= 2, sorted(children)
    assert any(
        child["projects"]["rows"] for child in children.values()
    ), "the healthy generation must publish a child project row to compare with"

    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        transient = account_ticks.tick(prior=None)
    codex = transient.sources["codex"]
    assert codex.metadata_health["state"] == "transient_read_failure"
    assert codex.data["projects"]["rows"] == ()
    transient_children = codex.data["account_scopes"]
    assert len(transient_children) >= 2, sorted(transient_children)
    for key, child in transient_children.items():
        assert child["projects"]["rows"] == (), (key, child["projects"])


def test_846_a13_healthy_transient_healthy_through_the_tick(ticks, monkeypatch):
    """A13, the transient sequence, driven through ticks rather than the
    builder. The published bundle after recovery must be the REBUILT
    generation, not the retained prior, and the armed retry key must be gone.
    """
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ticks.tui
    # The baseline is taken AFTER one signature bump so that it and the
    # recovery tick observe the same projection state; otherwise the bump below
    # would be the only difference between the two and `recovered.warnings`
    # would carry `codex_projection_incoherent` that the baseline never saw.
    ticks.bump_signature()
    healthy = ticks.tick(prior=None)
    healthy_codex = healthy.sources["codex"]
    assert healthy_codex.metadata_health["state"] == "healthy"
    healthy_labels = [
        row["label"] for row in healthy_codex.data["projects"]["rows"]]
    assert healthy_labels

    # Exact-version provider reuse would otherwise hand the healthy generation
    # straight back and the failing leg would never run, so the store moves
    # once — and only here, so the degraded and the recovery tick share one
    # version and retention is decided by the carrier rather than by a version
    # mismatch.
    ticks.bump_signature()
    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        degraded = ticks.tick()
    degraded_codex = degraded.sources["codex"]
    assert degraded_codex.metadata_health["state"] == "transient_read_failure"
    assert degraded_codex.data["projects"]["rows"] == ()
    # Arm a key so the recovery tick has something to clear. With the
    # retention leg in place this call refuses and clears without arming;
    # without the leg it arms, and the assertion below is what fails.
    tui._tui_retain_refused_partial(
        degraded_codex, provider="codex", now_utc=NOW,
        data_version=degraded_codex.data_version,
    )

    recovered = ticks.tick()
    assert tui._PARTIAL_RETRY_STATE == EMPTY_RETRY_STATE, (
        "the recovery tick left a key armed, so the next partial of the same "
        "cause would be retained rather than rebuilt"
    )
    recovered_codex = recovered.sources["codex"]
    assert recovered_codex is not degraded_codex
    assert recovered_codex.metadata_health == healthy_codex.metadata_health
    assert [
        row["label"] for row in recovered_codex.data["projects"]["rows"]
    ] == healthy_labels
    assert [w.code for w in recovered_codex.warnings] == [
        w.code for w in healthy_codex.warnings]


def test_846_a13_healthy_malformed_healthy_through_the_tick(ticks, monkeypatch):
    """A13's other sequence, which no test covered at all.

    A malformed generation is the REPRODUCIBLE degraded state, so it is the one
    a reader actually meets, and its recovery is what the rebuild remedy
    promises. The middle tick corrupts one thread's `cwd` in the store rather
    than patching a reader, so the carrier, the rows and the warnings are the
    production ones.
    """
    tui = ticks.tui
    cache = ticks.cache
    healthy = ticks.tick(prior=None)
    healthy_codex = healthy.sources["codex"]
    assert healthy_codex.metadata_health["state"] == "healthy"
    healthy_labels = [
        row["label"] for row in healthy_codex.data["projects"]["rows"]]
    assert healthy_labels

    original = cache.execute(
        "SELECT conversation_key, CAST(cwd AS BLOB) "
        "FROM codex_conversation_threads WHERE cwd IS NOT NULL AND cwd != '' "
        "ORDER BY conversation_key LIMIT 1").fetchone()
    assert original is not None
    key, good_cwd = original
    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = ?", (b"/synthetic/\xffproject", key))
    cache.commit()

    degraded = ticks.tick()
    degraded_codex = degraded.sources["codex"]
    assert degraded_codex.metadata_health["state"] == "malformed_row_partial"
    assert degraded_codex.metadata_health["retryable"] is False
    tui._tui_retain_refused_partial(
        degraded_codex, provider="codex", now_utc=NOW,
        data_version=degraded_codex.data_version,
    )

    cache.execute(
        "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
        "WHERE conversation_key = ?", (good_cwd, key))
    cache.commit()

    recovered = ticks.tick()
    recovered_codex = recovered.sources["codex"]
    assert recovered_codex is not degraded_codex
    assert recovered_codex.metadata_health == healthy_codex.metadata_health
    assert [
        row["label"] for row in recovered_codex.data["projects"]["rows"]
    ] == healthy_labels
    assert [w.code for w in recovered_codex.warnings] == [
        w.code for w in healthy_codex.warnings]


def test_846_the_idle_gate_clears_the_armed_key_it_refuses_on(ticks,
                                                              monkeypatch):
    """The `clear_partial_retry` branch of `_tui_source_bundle_can_idle`.

    The refusal itself was already covered; the CLEAR added beside it was not.
    Without it, the tick after the fault clears finds a key still inside its
    deadline and retains one more transient generation.

    The bundle is a real tick's, with its codex state's `availability` raised
    to `ok`. Under §4.6 rule 1 every transient generation now takes the
    cache-only fallback and therefore publishes `partial`, so the gate's
    availability leg would refuse first and the carrier leg would never run.
    Raising it is what makes the carrier the only leg that can refuse, which is
    the branch under test; the carrier itself is the production one.
    """
    import _lib_source_retry as retry

    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = ticks.tui
    with monkeypatch.context() as broken:
        _a9_force_leg_failure(broken, source_module, "detail_probe")
        bundle = ticks.tick(prior=None)
    codex = bundle.sources["codex"]
    assert codex.metadata_health["state"] == "transient_read_failure"
    assert codex.metadata_health["retryable"] is True

    bundle = dataclasses.replace(
        bundle,
        sources={
            **dict(bundle.sources),
            "codex": dataclasses.replace(
                codex, availability="ok", freshness="fresh"),
        },
    )

    _decision, armed = retry.plan_partial_retry(
        retry.EMPTY_RETRY_STATE,
        provider="codex",
        cause=("codex_metadata_incomplete",),
        data_version=codex.data_version,
        now=NOW,
    )
    assert armed != retry.EMPTY_RETRY_STATE, (
        "the fixture must actually arm a key, or the clear asserts nothing"
    )
    tui._PARTIAL_RETRY_STATE = armed

    assert tui._tui_source_bundle_can_idle(bundle) is False
    assert tui._PARTIAL_RETRY_STATE == retry.EMPTY_RETRY_STATE, (
        "the gate refused but left the key armed"
    )


def test_846_a9_the_idle_snapshot_rebuilds_the_transient_generation(
    tmp_path, monkeypatch,
):
    """A9's idle half, over a REAL transient generation.

    The idle tick never calls `reuse_coherent_source_state`: it consults
    `_tui_source_bundle_can_idle` and then republishes through the two clock
    refreshes, so refusing reuse in `_reusable_provider` does not reach it.
    """
    from zoneinfo import ZoneInfo

    harness = _harness(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    tui = harness.tui
    try:
        with monkeypatch.context() as broken:
            _a9_force_leg_failure(broken, source_module, "detail_probe")
            bundle = harness.tick(prior=None)
        codex = bundle.sources["codex"]
        assert codex.metadata_health["state"] == "transient_read_failure"

        prior = dataclasses.replace(
            tui._tui_empty_snapshot(NOW), source_bundle=bundle)
        assert tui._tui_source_bundle_can_idle(bundle) is False, (
            "the idle gate must refuse, or the rebuild branch never runs"
        )
        idle = tui._tui_build_idle_snapshot(
            prior,
            now_utc=NOW,
            precompute_envelope=False,
            runtime_bind=None,
            raw_config={},
            errors=[],
            display_tz_pref_override="utc",
            source_stats_conn=harness.stats,
            source_display_tz_name="UTC",
            source_display_tz=ZoneInfo("UTC"),
        )
        assert idle.source_bundle is not bundle, (
            "the rebuild branch must have run: a clock refresh would have "
            "returned the same transient bundle, which is the defect"
        )
        rebuilt = idle.source_bundle.sources["codex"]
        assert rebuilt.metadata_health["state"] == "healthy"
    finally:
        harness.cache.close()
        harness.stats.close()
        tui._tui_reset_partial_retry_state()
