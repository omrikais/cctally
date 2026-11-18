"""#300 — the dashboard envelope surfaces an all-inputs ``data_version`` string
(derived from the DB dispatch signature) so the browser's lazy detail fetchers
revalidate on an actual data-change signal instead of the 5s ``generated_at``
heartbeat.
"""
import datetime as dt
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    # ``snapshot_to_envelope`` opens cache.db / stats.db for the freshness read
    # even on a hand-built snapshot; pin the kernel paths to tmp so it never
    # touches the real data dir (mirrors test_dashboard_envelope_freshness).
    _ns = load_script()
    redirect_paths(_ns, monkeypatch, tmp_path)
    return _ns


def _snap(ns, **over):
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    kwargs = dict(
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=now,
    )
    kwargs.update(over)
    return ns["DataSnapshot"](**kwargs)


def test_envelope_emits_data_version(ns):
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_snap(ns, data_version="7.42.3.1.0.0.5.9"), now_utc=now)
    # #1118 D7: the wire value extends the internal version (see below).
    assert env["data_version"].startswith("7.42.3.1.0.0.5.9")


def test_envelope_data_version_defaults_to_empty(ns):
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    # A snapshot built without the field (fixture/positional/TUI path) → "",
    # the client's revalToken "no signal" sentinel (falls back to generated_at).
    # The key is still always present.
    env = ns["snapshot_to_envelope"](_snap(ns), now_utc=now)
    assert env["data_version"] == ""


def test_data_version_helper_changes_only_on_signature_change(ns):
    # `_snapshot_data_version` must return a string that is stable when the
    # signature is unchanged and differs when any leg changes.
    make = ns["_snapshot_data_version"]
    Sig = ns["SnapshotSignature"]
    base = Sig(max_entry_id=10, max_wus_id=2, max_wcs_id=3, reset_sig=(1, 100),
               max_codex_id=4, generation=5, entry_mutation_seq=42)
    v0 = make(base)
    assert isinstance(v0, str) and v0
    # Same signature → same string.
    assert make(base) == v0
    # An id-stable in-place finalization (entry_mutation_seq advances, max id flat)
    # MUST change the version.
    assert make(base._replace(entry_mutation_seq=43)) != v0
    # A weekly-usage-only change MUST change the version (P1-a: under-invalidation).
    assert make(base._replace(max_wus_id=3)) != v0
    # A prune (generation bump, ids flat) MUST change the version (P1-c detection).
    assert make(base._replace(generation=6)) != v0
    # None → "" (non-precompute path).
    assert make(None) == ""


def _pricing_variant(lp, change):
    """The live pricing snapshot with exactly one served-cost input moved."""
    live = lp.current_pricing_snapshot()
    fields = {name: getattr(live, name) for name in lp.PricingSnapshot.__slots__}
    if change == "same":
        pass
    elif change == "claude_card":
        card = dict(live.claude_pricing["claude-fable-5-2"])
        card["input_cost_per_token"] = card["input_cost_per_token"] * 2
        fields["claude_pricing"] = dict(
            live.claude_pricing, **{"claude-fable-5-2": card})
    elif change == "claude_fast":
        fields["fast_multipliers"] = dict(
            live.fast_multipliers,
            claude=dict(live.fast_multipliers["claude"],
                        **{"claude-fable-5-2": 3.0}))
    elif change == "codex_card":
        card = dict(live.codex_pricing["gpt-6.2-sol"])
        card["output_cost_per_token"] = card["output_cost_per_token"] * 2
        fields["codex_pricing"] = dict(
            live.codex_pricing, **{"gpt-6.2-sol": card})
    elif change == "snapshot_date":
        fields["snapshot_date"] = "2099-01-01"
    else:
        raise AssertionError(change)
    return lp.PricingSnapshot(**fields)


def _wire_version_under(ns, monkeypatch, snapshot, internal):
    """Render one envelope as a process whose pricing is ``snapshot`` would.

    Pricing is fixed per process since #868, so the live identities are
    memoized; reset every memo so this render recomputes from ``snapshot``.
    """
    import _lib_pricing as lp
    monkeypatch.setattr(lp, "_LIVE_PRICING_SNAPSHOT", snapshot)
    monkeypatch.setattr(lp, "_LIVE_CODEX_PRICING_IDENTITY", None)
    monkeypatch.setattr(lp, "_LIVE_PRICING_REVISION_IDENTITY", None,
                        raising=False)
    now = dt.datetime(2026, 4, 30, 12, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](
        _snap(ns, data_version=internal), now_utc=now)
    return env["data_version"]


@pytest.mark.parametrize("change", (
    "claude_card", "claude_fast", "codex_card", "snapshot_date"))
def test_wire_data_version_moves_with_pricing(ns, monkeypatch, change):
    """#1118 D7. Detects: an open session/project/source detail or
    conversation keeping its pre-upgrade cost after a pricing upgrade. The
    upgrade path restarts the dashboard in place (#868) over the same store,
    so the internal DB-signature version is byte-identical across the two
    binaries; only a pricing-revision token in the wire ``data_version``
    makes the browser's ``revalToken`` refetch an open detail."""
    import _lib_pricing as lp
    internal = "7.42.3.1.0.0.5.9"
    live = lp.current_pricing_snapshot()
    before = _wire_version_under(ns, monkeypatch, live, internal)
    assert before == _wire_version_under(ns, monkeypatch, live, internal), (
        "the same snapshot under the same pricing must not revalidate")
    assert before == _wire_version_under(
        ns, monkeypatch, _pricing_variant(lp, "same"), internal), (
        "equal pricing semantics must yield the same token")
    after = _wire_version_under(
        ns, monkeypatch, _pricing_variant(lp, change), internal)
    assert after != before, f"{change} moved a served cost but not data_version"
    assert after.startswith(internal) and before.startswith(internal)


def test_wire_empty_data_version_stays_the_no_signal_sentinel(ns, monkeypatch):
    """#1118 D7. Detects: the pricing token turning the empty "no signal"
    sentinel into a value, which would stop ``revalToken`` falling back to
    ``generated_at`` on the non-precompute path."""
    import _lib_pricing as lp
    for change in ("same", "claude_card", "snapshot_date"):
        assert _wire_version_under(
            ns, monkeypatch, _pricing_variant(lp, change), "") == ""
