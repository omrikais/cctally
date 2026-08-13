"""#496 S4 §8 — the two-tier rebuild benchmark.

Tier 1 gates in the ordinary suite. `bin/cctally-test-all` runs
`pytest tests/ -q -rfE` after the harness pool, so removing the old opt-in skip
puts these assertions in the everyday gate; that run applies `--timeout=120` per
test when `pytest-timeout` is importable, which is what bounds Tier 1's size to
tens of thousands of lines rather than a million.

Tier 1 deliberately does NOT assert wall time. At this size the fixed
schema-creation and publication costs dominate, so a per-line timing ratio
measures the wrong thing; the suite runs on two LAN runners under variable load,
and this repository treats a flaky test as a defect worth filing. It asserts the
properties F9 actually claims instead: per-pass traversal counters, output
equivalence against the pre-change canonical dumps, a peak-allocation slope
against doubled inert history, and the cache-leg flock hold.

Tier 2 stays opt-in behind ``CCTALLY_RUN_BENCHMARK=1`` at a million synthetic
lines. Every measured rebuild in both tiers runs in its own fresh subprocess
over a pre-built input, because ``ru_maxrss`` cannot be reset and building a
fixture in-process contaminates the resident peak.

    CCTALLY_RUN_BENCHMARK=1 bin/cctally-test-remote \\
        python3 -m pytest tests/test_rebuild_benchmark.py -x -q -s

``CCTALLY_BENCH_LINES`` overrides the Tier 2 target (default 1,000,000).
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import journal_fixture_496_s4 as F


#: `quotaLockHoldSeconds` is reported in the opt-in Tier 2 output and must not
#: become an assertion. `bench/README.md` states that rule; the flock property
#: it would be measuring is asserted structurally in `tests/test_quota_journal.py`.

#: Per-observation allocation that survives to the peak BEYOND the encoded byte
#: length of the retained line. Measured on this fixture at roughly 986 bytes,
#: of which about 41 is the `bytes` object header plus the list slot and the
#: rest is the cache leg's own per-observation anchor state — a pre-existing
#: property of `_apply_quota_records`, not of the retention this session added.
#: 2048 leaves headroom for run-to-run variation while still failing a
#: regression that retained decoded dictionaries, which cost several thousand
#: bytes per observation rather than one thousand.
_PER_RECORD_OVERHEAD_BYTES = 2048
#: The read path alone must retain NOTHING per dropped observation. Measured
#: delta with the cache leg disabled: 40 KB over 11,100 added observations.
_FLAT_RETENTION_ALLOWANCE_BYTES = 1_000_000


@pytest.fixture(scope="session")
def tier1_slope(tmp_path_factory):
    """One base fixture and one with the inert quota history doubled.

    The extra observations land in segments of their own, AFTER every record the
    stats fold consumes, so the retained decision population is identical and
    only the observation population grows. They are spread across several
    segments rather than piled into one because the streaming protocol-evidence
    accumulator buffers the segment it is reading (§5.2), and one oversized
    extra segment would raise peak allocation for a reason that has nothing to
    do with what this gate measures.
    """
    work = tmp_path_factory.mktemp("rebuild_slope")
    base = F.run_worker(
        work, "base", build=F.tier1_build(), trace=True)
    extra = int(base["shape"]["counts"]["obs_quota"])
    doubled = F.run_worker(
        work, "doubled",
        build=F.tier1_build(extra_quota_lines=extra), trace=True)
    return base, doubled


@pytest.fixture(scope="session")
def tier1_slope_without_the_leg(tmp_path_factory):
    """The same pair with `update_quota_cache=False`.

    Nothing is retained per observation then, so this isolates the READ PATH's
    retention from the cache leg's own per-observation state and turns the
    "observations are dropped" claim into a measurable one.
    """
    work = tmp_path_factory.mktemp("rebuild_slope_no_leg")
    base = F.run_worker(
        work, "base", build=F.tier1_build(), trace=True, no_quota_cache=True)
    extra = int(base["shape"]["counts"]["obs_quota"])
    doubled = F.run_worker(
        work, "doubled", build=F.tier1_build(extra_quota_lines=extra),
        trace=True, no_quota_cache=True)
    return base, doubled


def test_the_read_path_retains_nothing_per_dropped_observation(
    tier1_slope_without_the_leg
):
    """With the leg disabled the router drops every observation outright, so
    doubling inert history must not raise peak allocation at all. This is the
    sharp form of F9's claim; the bound below it is the softer one that has to
    absorb the leg's own state."""
    base, doubled = tier1_slope_without_the_leg
    growth = doubled["traced_peak_bytes"] - base["traced_peak_bytes"]
    assert growth <= _FLAT_RETENTION_ALLOWANCE_BYTES, growth


def test_the_fixture_models_the_production_record_mix(tier1_slope):
    """The builder asserts its own shape, so this is the guard on the guard:
    if the self-check were ever removed, the mix would still have to hold."""
    shape = tier1_slope[0]["shape"]
    modelled = shape["modelled"]
    assert 0.90 <= modelled["quota_share"] <= 0.95
    assert 700.0 <= modelled["mean_line_bytes"] <= 1100.0
    assert len([s for s in shape["segments"] if s.startswith("bootstrap-")]) >= 2
    assert len(
        [s for s in shape["segments"] if s.startswith("observations-")]) >= 2
    assert shape["resolution_op_id"]
    for family in ("obs_quota", "obs_claude", "evt", "op", "correction",
                   "correction_batch"):
        assert modelled["counts"][family] > 0, family


def test_each_journal_line_is_traversed_once_and_decoded_once(tier1_slope):
    base = tier1_slope[0]
    prefix = base["traversal"]["stats_prefix"]
    assert prefix["lines"] == base["shape"]["total_lines"]
    assert prefix["decodes"] == prefix["lines"] - base["malformed"]
    assert base["traversal"]["cutover_suffix"]["bytes"] == 0
    assert base["traversal"]["protocol_evidence"]["bytes"] > 0
    # No second traversal for the quota leg: it decodes bytes already read.
    replay = base["traversal"]["quota_replay"]
    assert replay["lines"] == replay["decodes"] > 0
    assert replay["bytes"] < prefix["bytes"]


def test_output_equivalence_against_the_pre_change_dump(tier1_slope):
    assert F.canonical_view(tier1_slope[0]) == F.load_golden(
        "tier1-full-prefix.json")


def test_doubling_inert_history_does_not_grow_the_retained_decisions(
    tier1_slope
):
    """The stats-side population is what F9's acceptance is about, and it must
    not move at all when only observation history grows."""
    base, doubled = tier1_slope
    assert (doubled["shape"]["retained_decision_lines"]
            == base["shape"]["retained_decision_lines"])
    assert doubled["lines_folded"] == base["lines_folded"]
    assert doubled["rows_by_table"] == base["rows_by_table"]


def test_peak_allocation_grows_only_by_the_added_observation_bytes(tier1_slope):
    """Retaining RAW BYTES keeps peak allocation linear in observation history,
    so "doubling inert history does not raise peak allocation" would be false
    and must not be asserted. The bound below still distinguishes this design
    from a regression that starts retaining decoded dictionaries again: a dict
    per observation is several times its encoded length, not a constant on top
    of it.
    """
    base, doubled = tier1_slope
    added_records = (doubled["traversal"]["quota_replay"]["lines"]
                     - base["traversal"]["quota_replay"]["lines"])
    added_bytes = (doubled["traversal"]["quota_replay"]["bytes"]
                   - base["traversal"]["quota_replay"]["bytes"])
    assert added_records > 0 and added_bytes > 0
    allowance = added_bytes + _PER_RECORD_OVERHEAD_BYTES * added_records
    growth = doubled["traced_peak_bytes"] - base["traced_peak_bytes"]
    assert growth <= allowance, (
        f"peak allocation grew {growth} bytes for {added_records} added "
        f"observations totalling {added_bytes} encoded bytes; allowance "
        f"{allowance}")


# ==========================================================================
# Tier 2 — opt-in, one million lines, reported rather than asserted
# ==========================================================================


@pytest.mark.skipif(
    not os.environ.get("CCTALLY_RUN_BENCHMARK"),
    reason="opt-in rebuild benchmark (set CCTALLY_RUN_BENCHMARK=1)",
)
def test_tier2_million_line_rebuild(tmp_path, capsys):
    target = int(os.environ.get("CCTALLY_BENCH_LINES", "1000000"))
    dump = F.run_worker(
        tmp_path, "tier2",
        build={"target_lines": target, "seed_cache": True},
        timeout=3600.0,
    )
    shape = dump["shape"]
    with capsys.disabled():
        print(
            f"\n[benchmark] {shape['total_lines']} lines / "
            f"{shape['total_bytes'] / 1e6:.1f} MB across "
            f"{len(shape['segments'])} segment(s); mean line "
            f"{shape['mean_line_bytes']:.1f} B; quota share "
            f"{shape['quota_share']:.4f}\n"
            f"[benchmark] resident peak "
            f"{dump['rss_peak_bytes'] / 1e9:.2f} GB; folded "
            f"{dump['lines_folded']} lines; effective events "
            f"{len(dump['journal_effective_events'])}\n"
            f"[benchmark] phases {dump['phase_seconds']}\n"
            f"[benchmark] traversal {dump['traversal']}\n"
            f"[benchmark] cache-leg flock hold "
            f"{dump['quota_lock_hold_seconds']:.2f}s")
    prefix = dump["traversal"]["stats_prefix"]
    assert prefix["lines"] == shape["total_lines"]
    assert dump["traversal"]["cutover_suffix"]["bytes"] == 0
