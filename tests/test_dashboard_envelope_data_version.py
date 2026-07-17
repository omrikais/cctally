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
    assert env["data_version"] == "7.42.3.1.0.0.5.9"


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
