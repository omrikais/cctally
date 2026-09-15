"""#769 S6 / #753 — the retained Codex hero cohort.

The Codex hook path commits cache ingestion with
``sync_codex_cache(..., quota_reconcile="defer")``, closes that handle, and
then runs ``reconcile_codex_quota_projection``, whose stats projection commits
before the cache-side certificate is stamped. A dashboard read landing in that
window sees new cache facts against an old stats projection, and
``codex_projection_coherence`` correctly rejects it. The source then published
that rejection destructively: a null aggregate hero spend over still-populated
account cards.

Both gaps are reached through existing orchestration seams — the real
``sync_codex_cache(..., quota_reconcile="defer")`` with the following
``reconcile_codex_quota_projection`` withheld, and the existing
``_after_stats_commit`` seam, which ``run_stats_ingest`` fires after the stats
commit and before the projection certificate is stamped. No production test
hook is added.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sqlite3
import sys

import pytest

from _cctally_dashboard_sources import DashboardReadContext
from _lib_quota import QuotaObservation, QuotaWindowIdentity
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
START = dt.datetime(2026, 7, 1, tzinfo=UTC)
NOW = dt.datetime(2026, 7, 20, tzinfo=UTC)


# ── seams ──────────────────────────────────────────────────────────────────

def _seeded_context(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    # The ordinary ingest, with reconciliation run — a coherent generation.
    ns["sync_codex_cache"](cache)
    return ns, cache, stats


def _open_real_gap(ns, cache, stats, tmp_path):
    """Gap one, through the real orchestration and no patched predicate.

    The Codex hook commits cache ingestion with `quota_reconcile="defer"`,
    closes that handle, and only then runs `reconcile_codex_quota_projection`.
    Appending a second rollout and ingesting it with reconciliation withheld
    leaves exactly the state a dashboard read lands in during that window: new
    cache facts against the previous stats projection. The predicate is asked
    rather than told, so this stays a real gap if its rule changes.
    """
    import _cctally_dashboard_sources as sources

    second = (tmp_path / "provider" / "sessions" / "2026" / "07" / "17"
              / "rollout.jsonl")
    second.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", second)
    ns["sync_codex_cache"](cache, quota_reconcile="defer")
    verdict = sources.codex_projection_coherence(_context(cache, stats))
    assert not verdict.coherent, (
        "the deferred ingest did not open the reconciliation gap: "
        f"{verdict.reason!r}"
    )


def _seed_task_titles(cache, tmp_path):
    """Give the corpus the persisted Codex task names the labels come from.

    ``_codex_conversation_metadata`` reads titles from the provider root's
    ``state_5.sqlite`` ``threads`` table. The parity corpus ships no such file,
    so without this seed the build publishes an EMPTY
    ``private_session_labels`` map and any check that iterates it proves
    nothing.
    """
    native_ids = [
        str(row[0]) for row in cache.execute(
            "SELECT DISTINCT native_thread_id FROM codex_conversation_threads "
            "WHERE native_thread_id IS NOT NULL AND native_thread_id <> ''"
        )
    ]
    assert native_ids, "the corpus minted no Codex conversation thread row"
    state_path = tmp_path / "provider" / "state_5.sqlite"
    state = sqlite3.connect(state_path)
    try:
        state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
        state.executemany(
            "INSERT INTO threads (id, title) VALUES (?, ?)",
            [(native_id, f"Rework the frontier ledger {native_id[:8]}")
             for native_id in native_ids],
        )
        state.commit()
    finally:
        state.close()
    return native_ids


def _root_key(cache):
    row = cache.execute(
        "SELECT source_root_key FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _observation(*, root, window_minutes, resets_at, captured_at):
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex",
            source_root_key=root,
            logical_limit_key="limit",
            observed_slot="primary",
            window_minutes=window_minutes,
        ),
        captured_at=captured_at,
        used_percent=25.0,
        resets_at=resets_at,
        source_path=f"/private/{root}.jsonl",
        line_offset=1,
    )


def _install_live_cycle(monkeypatch, source_module, *, root, reset=None):
    reset = reset or (NOW + dt.timedelta(days=2))
    monkeypatch.setattr(
        source_module,
        "load_codex_quota_observations",
        lambda **_kwargs: (
            _observation(
                root=root, window_minutes=300,
                resets_at=NOW + dt.timedelta(hours=4),
                captured_at=NOW - dt.timedelta(minutes=10),
            ),
            _observation(
                root=root, window_minutes=10_080,
                resets_at=reset,
                captured_at=NOW - dt.timedelta(minutes=10),
            ),
        ),
    )


def _context(cache, stats):
    return DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START,
        now_utc=NOW, display_tz_name="UTC",
    )


def _build(source_module, cache, stats, *, version, prior=None):
    return source_module.build_codex_source_state(
        _context(cache, stats),
        data_version=version,
        prior_hero_cohort=prior,
    )





# ── the cases ──────────────────────────────────────────────────────────────

def test_a_coherent_build_publishes_a_hero_cohort(tmp_path, monkeypatch):
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        state = _build(source_module, cache, stats, version="coherent-v1")
        assert state.capabilities["hero"].status == "supported"
        cohort = state.hero_cohort
        assert cohort is not None
        assert cohort["hero"]["cost_usd"] == state.data["hero"]["cost_usd"]
        assert set(cohort) == {"hero", "accounts", "validity"}
        # A healthy generation's payload stays byte-identical.
        assert "update_state" not in state.data["hero"]
    finally:
        cache.close()
        stats.close()


def test_an_incoherent_build_keeps_the_prior_coherent_hero_with_its_warning(
    tmp_path, monkeypatch,
):
    """D4. The publication is one coherent set, never a mixture."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        coherent = _build(source_module, cache, stats, version="coherent-v1")
        spent = coherent.data["hero"]["cost_usd"]
        assert spent is not None

        _open_real_gap(_ns, cache, stats, tmp_path)
        gapped = _build(
            source_module, cache, stats, version="gap-v2",
            prior=coherent.hero_cohort,
        )

        hero = gapped.data["hero"]
        # `is not None` is the discriminator: before this change the gapped
        # build nulled every hero operand, and the corpus's own cycle spend
        # happens to be 0.0, so an equality alone would read as a pass under a
        # future regression that published a bare zero.
        assert hero["cost_usd"] is not None, "the known spend was erased"
        assert hero["cost_usd"] == spent
        assert hero["input_tokens"] == coherent.data["hero"]["input_tokens"]
        assert hero["total_tokens"] == coherent.data["hero"]["total_tokens"]
        assert hero["update_state"] == "updating"
        assert hero["cycle"] == coherent.data["hero"]["cycle"]
        # The disclosure stays exactly where it was.
        assert any(
            warning.code == "codex_projection_incoherent"
            for warning in gapped.warnings
        )
        assert gapped.domain_freshness["hero"] == "stale"
        assert gapped.availability == "partial"
    finally:
        cache.close()
        stats.close()


def test_with_no_coherent_cohort_ever_published_the_hero_is_pending(
    tmp_path, monkeypatch,
):
    """D5. Not a zero, not a dash — an explicit Pending state.

    This is the state after a restart whose FIRST build lands in the gap.
    """
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        _open_real_gap(_ns, cache, stats, tmp_path)
        state = _build(source_module, cache, stats, version="cold-gap-v1")

        hero = state.data["hero"]
        assert hero["update_state"] == "pending"
        assert hero["cost_usd"] is None
        assert hero["total_tokens"] is None
        assert hero["cycle"] is None
        assert state.hero_cohort is None
    finally:
        cache.close()
        stats.close()


def test_a_cycle_failure_renders_pending_rather_than_the_retained_cohort(
    tmp_path, monkeypatch,
):
    """The retention is scoped to the PROJECTION gap, not to every hero failure.

    An install with no Codex accounting yet and no resolvable weekly cycle is
    COHERENT: `cycle_failure` requires both a missing cycle and existing
    accounting, so the build publishes a cohort whose spend is ``0.0`` over an
    empty cycle vector. The user then runs their first Codex session.
    Accounting now exists, the cycle still does not resolve, and
    `cycle_failure` becomes true — while the validity key has not moved,
    because the accounts digest, the empty cycle vector and both store
    identities are unchanged.

    Gating retention on the composed ``projection_incoherent or
    cycle_failure`` therefore republishes that ``0.0`` as a retained figure
    with ``update_state="updating"`` beside a `SPENT THIS WEEK` label, and the
    client never surfaces the correct `codex_cycle_unavailable` reason because
    it reads `unavailableReason` only when `cost_usd` is null. A zero beside
    that label is a claim, which is exactly what D5's Pending state exists to
    prevent.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root = tmp_path / "provider"
    rollout = root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(root))
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        # No cycle resolves in either build: no quota observation is installed
        # and none is retained, so `_resolve_codex_weekly_cycle` yields none.
        empty = _build(source_module, cache, stats, version="no-accounting-v1")
        assert empty.data["hero"]["cost_usd"] == 0.0, (
            "the no-accounting build is not the coherent zero this case needs"
        )
        assert "update_state" not in empty.data["hero"]
        assert empty.hero_cohort is not None

        # The user's first Codex session.
        ns["sync_codex_cache"](cache)

        # The reachability claim, proved rather than assumed: the prior cohort
        # still CERTIFIES against this generation, so Pending below can only
        # come from the failure classification, never from a moved key.
        # The vector is built by the production constructor over the empty
        # cycle list this case has already established, rather than written out
        # as a literal `()`. The list stays local because reproducing how
        # production reaches it would mean re-running the whole quota
        # observation capture, which is more duplication rather than less; the
        # vector's SHAPE now follows `_codex_cycle_vector`.
        current = source_module._codex_hero_cohort_validity(
            _context(cache, stats),
            cycle_vector=source_module._codex_cycle_vector(()),
        )
        assert source_module._retained_hero_cohort(
            empty.hero_cohort, current) is not None, (
            "the validity key moved, so this case no longer reaches the branch"
        )

        after = _build(
            source_module, cache, stats, version="first-session-v2",
            prior=empty.hero_cohort,
        )
        hero = after.data["hero"]
        assert any(
            warning.code == "codex_cycle_unavailable"
            for warning in after.warnings
        ), "this case must be a cycle failure, not a projection gap"
        assert not any(
            warning.code == "codex_projection_incoherent"
            for warning in after.warnings
        ), "the projection must stay coherent for this case to be the one filed"
        assert hero["update_state"] == "pending", (
            "a cycle failure retained a cohort the projection gap owns"
        )
        assert hero["cost_usd"] is None, (
            "a stale $0.00 was republished beside a SPENT THIS WEEK label"
        )
        assert hero["total_tokens"] is None
        assert hero["cycle"] is None
        assert after.hero_cohort is None
    finally:
        cache.close()
        stats.close()


@pytest.mark.parametrize("mutate", ["accounts_digest", "cycle_vector",
                                    "cache_identity", "stats_identity"])
def test_the_cohort_is_dropped_when_any_validity_element_changes(
    tmp_path, monkeypatch, mutate,
):
    """A weak key would show an account, a cycle or a store the current
    generation should not. Each element is exercised on its own."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        coherent = _build(source_module, cache, stats, version="coherent-v1")
        assert coherent.hero_cohort is not None
        stale = dict(coherent.hero_cohort)
        validity = dict(stale["validity"])
        validity[mutate] = ("mutated", mutate)
        stale["validity"] = validity

        _open_real_gap(_ns, cache, stats, tmp_path)
        gapped = _build(
            source_module, cache, stats, version="gap-v2", prior=stale,
        )
        assert gapped.data["hero"]["update_state"] == "pending"
        assert gapped.data["hero"]["cost_usd"] is None
        assert gapped.hero_cohort is None
    finally:
        cache.close()
        stats.close()


def test_an_unresolvable_accounts_digest_fails_closed_to_pending(
    tmp_path, monkeypatch,
):
    """The retention fails closed rather than retaining across an unknown."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        coherent = _build(source_module, cache, stats, version="coherent-v1")
        assert coherent.hero_cohort is not None

        _open_real_gap(_ns, cache, stats, tmp_path)
        def refusing_digest(_stats_conn):
            raise sqlite3.OperationalError("no such table: accounts")

        monkeypatch.setattr(
            source_module, "accounts_identity_digest", refusing_digest)
        gapped = _build(
            source_module, cache, stats, version="gap-v2",
            prior=coherent.hero_cohort,
        )
        assert gapped.data["hero"]["update_state"] == "pending"
        assert gapped.hero_cohort is None
    finally:
        cache.close()
        stats.close()


def test_the_cohort_carries_no_private_labels_and_no_account_scopes(
    tmp_path, monkeypatch,
):
    """Request privacy is applied AFTER state publication, so a retained
    private map would outlive the request that was allowed to see it."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        _seed_task_titles(cache, tmp_path)
        state = _build(source_module, cache, stats, version="coherent-v1")
        cohort = state.hero_cohort
        assert cohort is not None
        assert "private_session_labels" not in cohort
        assert "account_scopes" not in cohort
        # Nothing anywhere inside it, at any depth, may be a session title.
        # The population is asserted first: with an empty label map the loop
        # body never runs, and the strongest check in this file would pass
        # while proving nothing.
        assert state.private_session_labels, (
            "the corpus published no session labels, so the disclosure check "
            "below has nothing to look for"
        )
        rendered = repr(cohort)
        for title in (state.private_session_labels or {}).values():
            assert title not in rendered, title
        # Only the named hero operands travel from a card.
        for operands in cohort["accounts"].values():
            assert set(operands) <= {
                "spendUsd", "inputTokens", "cachedInputTokens",
                "outputTokens", "reasoningOutputTokens", "totalTokens",
            }, operands
    finally:
        cache.close()
        stats.close()


def test_the_state_refuses_a_cohort_carrying_private_labels():
    """Refused rather than silently stripped, so a wrong construction is loud.

    The guard is a positive whitelist of the three members the cohort has, so
    a key nobody anticipated is refused too — a two-name denylist would admit
    every future one.
    """
    from _lib_dashboard_sources import SourceDashboardState

    def _state(cohort):
        return SourceDashboardState(
            source="codex",
            availability="ok",
            freshness="fresh",
            warnings=(),
            data_version="v1",
            last_success_at=None,
            capabilities={},
            data={},
            hero_cohort=cohort,
        )

    for forbidden in ("private_session_labels", "account_scopes",
                      "sessions", "projects"):
        with pytest.raises(ValueError):
            _state({"hero": {}, forbidden: {"a": "b"}})
    # The three named members are accepted, so the whitelist did not narrow
    # the shape the builder actually publishes.
    accepted = _state({"hero": {}, "accounts": {}, "validity": {}})
    assert set(accepted.hero_cohort) == {"hero", "accounts", "validity"}


def test_the_cohort_survives_the_idle_clock_and_a_degrade(
    tmp_path, monkeypatch,
):
    """A field omitted from either constructor is dropped without error."""
    from _lib_dashboard_sources import (
        SourceDashboardWarning, degrade_source_state,
    )

    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        coherent = _build(source_module, cache, stats, version="coherent-v1")
        assert coherent.hero_cohort is not None

        degraded = degrade_source_state(
            coherent,
            SourceDashboardWarning(
                "source_ingest_contended", "Source ingest is in progress.",
                "ingest",
            ),
        )
        assert degraded.hero_cohort == coherent.hero_cohort

        clocked = source_module.refresh_codex_source_clock(
            coherent, now_utc=NOW + dt.timedelta(hours=3),
        )
        assert clocked.hero_cohort == coherent.hero_cohort
    finally:
        cache.close()
        stats.close()


def test_a_gap_publishes_a_hero_and_cards_that_agree(tmp_path, monkeypatch):
    """The contradiction the issue reports: the client reconstructed the
    focused hero's cost from a still-populated card while the aggregate hero
    read null. Server-side the two must now come from one generation."""
    _ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    source_module = sys.modules["_cctally_dashboard_sources"]
    try:
        _install_live_cycle(monkeypatch, source_module, root=_root_key(cache))
        coherent = _build(source_module, cache, stats, version="coherent-v1")
        _open_real_gap(_ns, cache, stats, tmp_path)
        gapped = _build(
            source_module, cache, stats, version="gap-v2",
            prior=coherent.hero_cohort,
        )
        # An undecorated install publishes no cards at all, and the retained
        # cohort's card map is then empty too. That is the agreement, stated as
        # an equality rather than skipped, so this case still fails if one side
        # starts publishing cards the other does not.
        cards = gapped.data.get("accounts") or ()
        cohort_cards = coherent.hero_cohort["accounts"]
        assert len(cohort_cards) == len(cards)
        for card in cards:
            retained = cohort_cards.get(str(card["accountKey"]))
            assert retained is not None
            assert card["spendUsd"] == retained["spendUsd"]
    finally:
        cache.close()
        stats.close()
