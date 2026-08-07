"""#496 S4 — the streaming rebuild read path.

Covers the four things that could change behaviour rather than only cost:

  * output equivalence against the canonical dumps captured from the PRE-CHANGE
    implementation, in fresh subprocesses over cloned inputs with the target
    quota rows initially absent;
  * the cutover answer, inline and through the pinned-prefix suffix fallback;
  * `accounts.last_seen_utc`, whose contribution set is exactly three classes;
  * the durable protocol-evidence hash and the violation fingerprints that
    depend on the selector's sequence numbering.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import journal_fixture_496_s4 as F
from conftest import load_script, redirect_paths


UTC_AT = "2026-01-01T00:00:00Z"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    return ns, jr, jl


def _write_segment(core, name: str, records, jl) -> None:
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(core.JOURNAL_DIR / name, "ab") as handle:
        for record in records:
            handle.write(jl.encode_line(record))


def _claude_obs(jl, at: str):
    return jl.make_obs(at=at, src="record-usage", provider="claude",
                       payload={"weekly_percent": 1.0, "source": "statusline"})


def _cutover_op(jr, jl, at: str, account: str):
    record = jl.make_op(at=at, src="accounts-cutover", payload={
        "kind": "accounts_cutover", "claude_legacy_account": account})
    record["id"] = jr.CUTOVER_OP_ID
    return record


# ==========================================================================
# Output equivalence against the pre-change canonical dumps (spec §8.5)
# ==========================================================================


@pytest.fixture(scope="session")
def tier1_dumps(tmp_path_factory):
    """One unpinned and one pinned rebuild, each in its own fresh subprocess.

    Session-scoped because each run costs a couple of seconds and every
    assertion below reads the same two dumps; per-test rebuilds would spend the
    suite's 120 s per-test budget on repetition rather than coverage.
    """
    work = tmp_path_factory.mktemp("rebuild_read_path")
    return {
        "full": F.run_worker(work, "full", build=F.tier1_build()),
        "pinned": F.run_worker(
            work, "pinned", build=F.tier1_build(), pin="before-cutover"),
    }


def test_full_prefix_rebuild_reproduces_the_pre_change_dump(tier1_dumps):
    assert F.canonical_view(tier1_dumps["full"]) == F.load_golden(
        "tier1-full-prefix.json")


def test_pinned_prefix_rebuild_reproduces_the_pre_change_dump(tier1_dumps):
    """The pinned answer is what the suffix fallback exists to preserve: a
    rebuild that resolved the cutover from the prefix alone would restamp every
    legacy Claude observation to `unattributed` and move `last_seen_utc`."""
    assert F.canonical_view(tier1_dumps["pinned"]) == F.load_golden(
        "tier1-pinned-before-cutover.json")


def test_the_two_scenarios_are_distinguishable(tier1_dumps):
    """Guards the pair above against passing for the wrong reason: if pinning
    made no difference, both comparisons would be the same assertion twice."""
    assert F.canonical_view(tier1_dumps["full"]) != F.canonical_view(
        tier1_dumps["pinned"])


def test_the_quota_leg_actually_ran(tier1_dumps):
    """`_rebuild_quota_cache_leg` returns immediately when cache.db is absent,
    which is how the previous benchmark never exercised 93% of a real journal."""
    replay = tier1_dumps["full"]["traversal"]["quota_replay"]
    assert replay["decodes"] > 0
    assert tier1_dumps["full"]["cache"]["quota_window_snapshots"]


# ==========================================================================
# Per-pass counters (spec §10.2)
# ==========================================================================


def test_every_journal_line_is_traversed_once_and_decoded_once(tier1_dumps):
    dump = tier1_dumps["full"]
    prefix = dump["traversal"]["stats_prefix"]
    shape = dump["shape"]
    assert prefix["lines"] == shape["total_lines"]
    assert prefix["decodes"] == prefix["lines"] - dump["malformed"]
    # Acceptance criterion 2 is about BYTES, not lines: counting each line once
    # would still pass if a pass re-read part of a line. The counter accumulates
    # `len(raw) + 1` per line and the fixture's `total_bytes` is the sum of the
    # encoded lines including their newline, so equality here is the exact
    # "each byte at most once, and every byte once" statement.
    assert prefix["bytes"] == shape["total_bytes"]


def test_a_full_prefix_rebuild_captures_the_cutover_inline(tier1_dumps):
    """The common path: the op is inside the streamed prefix, so no suffix scan
    runs and `cutover_suffix` reports zero bytes."""
    assert tier1_dumps["full"]["traversal"]["cutover_suffix"] == {
        "lines": 0, "bytes": 0, "decodes": 0}


def test_a_pinned_prefix_before_the_cutover_reads_only_the_suffix(tier1_dumps):
    """Placement alone never reaches the fallback — an unpinned rebuild always
    uses the current full high-water — and the suffix stops at the first match
    rather than reading to the end of the journal."""
    dump = tier1_dumps["pinned"]
    suffix = dump["traversal"]["cutover_suffix"]
    assert suffix["bytes"] > 0
    assert suffix["lines"] == 1
    prefix = dump["traversal"]["stats_prefix"]
    assert prefix["bytes"] + suffix["bytes"] <= dump["shape"]["total_bytes"]


def test_the_protocol_evidence_pass_is_reported_separately(tier1_dumps):
    """The fixture carries a resolution op, so the evidence pass is non-zero and
    is visible as its own named pass rather than folded into a total."""
    assert tier1_dumps["full"]["traversal"]["protocol_evidence"]["bytes"] > 0
    assert tier1_dumps["full"]["traversal"]["protocol_evidence"]["lines"] == 1


def test_the_quota_replay_decodes_each_retained_observation_once(tier1_dumps):
    replay = tier1_dumps["full"]["traversal"]["quota_replay"]
    assert replay["lines"] == replay["decodes"]
    assert replay["bytes"] < tier1_dumps["full"]["traversal"][
        "stats_prefix"]["bytes"]


def test_the_additive_metrics_carry_every_named_phase(tier1_dumps):
    phases = tier1_dumps["full"]["phase_seconds"]
    # `structural_fold` and `open_block_projection` split `stats_fold`'s first
    # two spans (#496 S5b). They exist because together they are how long the
    # retained cache read snapshot pins the cache.db WAL, and a rebuild record
    # is where that window has to stay measurable.
    assert set(phases) == {
        "journal_read_decode", "cutover_suffix", "protocol_evidence",
        "effective_selection", "quota_cache_leg", "structural_fold",
        "open_block_projection", "stats_fold", "scratch_validate",
        "publication"}
    assert all(value >= 0 for value in phases.values())


# ==========================================================================
# The cutover resolver (spec §5.1)
# ==========================================================================


def test_an_inline_capture_short_circuits_the_suffix_scan(tmp_path, monkeypatch):
    _ns, jr, _jl = _load(tmp_path, monkeypatch)
    counters = {"lines": 0, "bytes": 0, "decodes": 0}
    assert jr._resolve_cutover_for_rebuild(
        "acct-x", ("observations-2026-01.jsonl", 10), [], counters) == "acct-x"
    assert counters == {"lines": 0, "bytes": 0, "decodes": 0}


def test_a_captured_null_account_resolves_to_unattributed(tmp_path, monkeypatch):
    """`find_accounts_cutover_op` returns at the first matching RECORD, so an op
    that recorded no account means `unattributed`, not "keep looking"."""
    _ns, jr, _jl = _load(tmp_path, monkeypatch)
    import _lib_accounts
    assert jr._resolve_cutover_for_rebuild(
        None, ("observations-2026-01.jsonl", 10), []
    ) == _lib_accounts.UNATTRIBUTED


def test_a_pinned_prefix_finds_the_cutover_in_the_suffix(tmp_path, monkeypatch):
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    records = [_claude_obs(jl, UTC_AT) for _ in range(4)]
    records.append(_cutover_op(jr, jl, UTC_AT, "acct-legacy-claude"))
    records.extend(_claude_obs(jl, UTC_AT) for _ in range(2))
    _write_segment(core, "observations-2026-01.jsonl", records, jl)

    lines = list(jr.iter_range(None, jr.journal_high_water()))
    pinned = (lines[2][0], lines[2][1] + len(lines[2][2]) + 1)
    counters = {"lines": 0, "bytes": 0, "decodes": 0}
    resolved = jr._resolve_cutover_for_rebuild(
        jr._CUTOVER_UNSEEN, pinned, jr.list_segments(), counters)
    assert resolved == "acct-legacy-claude"
    # Only the unvisited suffix — lines 3 and 4, the second of which is the op —
    # and it stopped at the first match rather than reading the two lines after.
    assert counters["lines"] == 2
    assert counters["bytes"] < sum(len(raw) + 1 for _s, _o, raw in lines)


def test_an_absent_cutover_resolves_to_unattributed(tmp_path, monkeypatch):
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    import _lib_accounts
    _write_segment(core, "observations-2026-01.jsonl",
                   [_claude_obs(jl, UTC_AT) for _ in range(3)], jl)
    hw = jr.journal_high_water()
    lines = list(jr.iter_range(None, hw))
    pinned = (lines[0][0], lines[0][1] + len(lines[0][2]) + 1)
    assert jr._resolve_cutover_for_rebuild(
        jr._CUTOVER_UNSEEN, pinned, jr.list_segments()
    ) == _lib_accounts.UNATTRIBUTED


def test_find_accounts_cutover_op_still_returns_none_when_absent(
    tmp_path, monkeypatch
):
    """The cache and conversations migrations defer their backfill on None; the
    streaming rewrite must not turn absence into a sentinel string."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _write_segment(core, "observations-2026-01.jsonl",
                   [_claude_obs(jl, UTC_AT)], jl)
    assert jr.find_accounts_cutover_op() is None
    _write_segment(core, "observations-2026-02.jsonl",
                   [_cutover_op(jr, jl, UTC_AT, "acct-legacy")], jl)
    assert jr.find_accounts_cutover_op() == "acct-legacy"


# ==========================================================================
# The streaming protocol-evidence hash (spec §5.2)
# ==========================================================================


def _streamed_digest(jr, high_water, at_prefix):
    """Drive the accumulator over `high_water` and read `at_prefix` from it."""
    import _lib_journal_router as R

    hasher = R.PrefixHashAccumulator()
    state = {"prior": None}
    for segment, offset, raw in jr._iter_range_with_segments(
        None, high_water, jr.list_segments(),
        on_segment=lambda name: hasher.begin_segment(name, state["prior"]),
        on_bytes=hasher.extend,
    ):
        state["prior"] = (segment, offset + len(raw) + 1)
        if state["prior"] == at_prefix:
            return hasher.digest_at(at_prefix)
    return hasher.digest_at(at_prefix)


def test_streaming_prefix_hash_matches_journal_prefix_hash(tmp_path, monkeypatch):
    """The durable hash value is unchanged, including a prefix ending
    mid-segment — the case the framing makes awkward, because a segment's length
    is written before its bytes and is only known at the evidence point."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _write_segment(core, "bootstrap-20260101T000000_000001.jsonl",
                   [_claude_obs(jl, UTC_AT) for _ in range(3)], jl)
    _write_segment(core, "observations-2026-01.jsonl",
                   [_claude_obs(jl, UTC_AT) for _ in range(5)], jl)
    hw = jr.journal_high_water()
    lines = list(jr.iter_range(None, hw))

    boundaries = [
        # a prefix ending exactly at a segment boundary
        (lines[2][0], lines[2][1] + len(lines[2][2]) + 1),
        # a prefix ending mid-segment
        (lines[5][0], lines[5][1] + len(lines[5][2]) + 1),
        # the whole prefix
        hw,
    ]
    for prefix in boundaries:
        assert _streamed_digest(jr, hw, prefix) == jr.journal_prefix_hash(
            prefix), prefix


def test_streaming_prefix_hash_frames_an_empty_segment(tmp_path, monkeypatch):
    """`iter_range` skips a zero-byte segment; `journal_prefix_hash` frames it."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    core.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (core.JOURNAL_DIR / "bootstrap-20260101T000000_000001.jsonl").write_bytes(b"")
    _write_segment(core, "observations-2026-01.jsonl",
                   [_claude_obs(jl, UTC_AT) for _ in range(3)], jl)
    hw = jr.journal_high_water()
    assert _streamed_digest(jr, hw, hw) == jr.journal_prefix_hash(hw)


# ==========================================================================
# Sequence preservation (corrects spec §4.7)
# ==========================================================================


def test_violation_fingerprints_survive_the_filtered_retention(tier1_dumps):
    """Three of the seven structural violation kinds put the selector's
    `enumerate` sequence inside `ProtocolViolation.evidence`, and the
    fingerprint hashes that evidence. The fingerprint is DURABLE: it lands in
    `journal_protocol_violations` and is named by a `journal_protocol_resolution`
    op that `_cctally_journal_repair` mints from the UNFILTERED record list. A
    rebuild that renumbered would make an acknowledged violation unresolvable
    and raise on every later rebuild.
    """
    dump = tier1_dumps["full"]
    rows = [json.loads(row) for row in dump["journal_protocol_violations"]]
    kinds = {row["kind"] for row in rows}
    assert "commit_without_begin" in kinds, kinds
    # The sequence-bearing evidence is present, so the fingerprint depends on it.
    assert any("commitSequence" in row["evidence"] for row in rows)
    # And an acknowledgement minted against the unfiltered numbering still
    # resolves — the whole reason placeholders exist.
    assert dump["acknowledged_protocol_violations"]


def test_the_acknowledged_violation_is_bound_to_a_real_prefix_hash(tier1_dumps):
    acknowledged = tier1_dumps["full"]["acknowledged_protocol_violations"]
    assert len(acknowledged) == 1
    assert acknowledged[0]["journalPrefixHash"].startswith("sha256:")


# ==========================================================================
# `accounts.last_seen_utc` (spec §4.6 / §10.6)
# ==========================================================================


def _rebuild_last_seen(jr, tmp_path):
    dest = tmp_path / "rebuilt.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest), update_quota_cache=False)
    import sqlite3
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        return dict(conn.execute(
            "SELECT account_key, last_seen_utc FROM accounts"))
    finally:
        conn.close()


def _last_seen_journal(jr, jl, core, *, tail):
    """An observe op, one stamped observation, then whatever `tail` adds."""
    records = [
        jl.make_account_observe("2026-01-01T00:00:00Z", "acct-a", "claude"),
        jl.make_obs(at="2026-02-01T00:00:00Z", src="record-usage",
                    provider="claude", payload={"weekly_percent": 1.0},
                    account="acct-a"),
        _cutover_op(jr, jl, "2026-01-01T00:00:00Z", "acct-a"),
        *tail,
    ]
    _write_segment(core, "observations-2026-01.jsonl", records, jl)


def test_a_later_legacy_event_does_not_move_last_seen(tmp_path, monkeypatch):
    """A legacy evt normalizes into `payload.account_key`, which `_account_of`
    does not read, so it never advanced `last_seen_utc` and must not start."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _last_seen_journal(jr, jl, core, tail=[
        jl.make_evt("snapshot_accept", "sa:late", "2099-01-01T00:00:00Z", {
            "captured_at_utc": "2099-01-01T00:00:00Z",
            "week_start_date": "2099-01-01", "week_end_date": "2099-01-08",
            "week_start_at": "2099-01-01T00:00:00+00:00",
            "week_end_at": "2099-01-08T00:00:00+00:00",
            "weekly_percent": 5.0, "source": "statusline",
            "payload_json": "{}", "page_url": None,
        }),
    ])
    assert _rebuild_last_seen(jr, tmp_path)["acct-a"] == "2026-02-01T00:00:00Z"


def test_a_later_vendor_tagged_budget_event_does_not_move_last_seen(
    tmp_path, monkeypatch
):
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _last_seen_journal(jr, jl, core, tail=[
        jl.make_evt("budget", "budget:late", "2099-01-01T00:00:00Z", {
            "vendor": "claude", "period": "month",
            "period_start_at": "2099-01-01T00:00:00+00:00",
            "threshold": 50, "budget_usd": 100.0, "spent_usd": 50.0,
            "consumption_pct": 50.0,
            "crossed_at_utc": "2099-01-01T00:00:00Z", "alerted_at": None,
        }),
    ])
    assert _rebuild_last_seen(jr, tmp_path)["acct-a"] == "2026-02-01T00:00:00Z"


def test_a_later_legacy_claude_observation_moves_last_seen(tmp_path, monkeypatch):
    """The one legacy class that DOES contribute: normalization stamps a
    top-level `account`, so the cutover account's last-seen advances."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    _last_seen_journal(jr, jl, core, tail=[
        jl.make_obs(at="2099-01-01T00:00:00Z", src="record-usage",
                    provider="claude", payload={"weekly_percent": 9.0}),
    ])
    assert _rebuild_last_seen(jr, tmp_path)["acct-a"] == "2099-01-01T00:00:00Z"


def test_a_later_legacy_codex_observation_moves_unattributed(
    tmp_path, monkeypatch
):
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    import _lib_accounts
    _last_seen_journal(jr, jl, core, tail=[
        jl.make_account_observe(
            "2026-01-01T00:00:00Z", _lib_accounts.UNATTRIBUTED, "codex"),
        jl.make_obs(at="2099-02-02T00:00:00Z", src="codex-quota",
                    provider="codex",
                    payload={"kind": "quota_window_snapshot",
                             "source": "codex", "source_root_key": "r",
                             "source_path": "/r/x.jsonl", "line_offset": 0,
                             "captured_at_utc": "2099-02-02T00:00:00Z",
                             "observed_slot": "primary",
                             "logical_limit_key": "limit-primary",
                             "limit_id": "native-primary",
                             "limit_name": "Primary", "window_minutes": 300,
                             "used_percent": 1.0,
                             "resets_at_utc": "2099-02-03T00:00:00Z",
                             "plan_type": "pro",
                             "individual_limit_json": None,
                             "reached_type": None,
                             "observed_model": "gpt-5.3-codex"}),
    ])
    last_seen = _rebuild_last_seen(jr, tmp_path)
    assert last_seen[_lib_accounts.UNATTRIBUTED] == "2099-02-02T00:00:00Z"
    assert last_seen["acct-a"] == "2026-02-01T00:00:00Z"


# ==========================================================================
# In-leg normalization of retained observation bytes (spec §6.3)
# ==========================================================================


def _quota_cache_rows(core):
    import sqlite3
    conn = sqlite3.connect(f"file:{core.CACHE_DB_PATH}?mode=ro", uri=True)
    try:
        return list(conn.execute(
            "SELECT source_path, account_key FROM quota_window_snapshots "
            "ORDER BY source_path"))
    finally:
        conn.close()


def _quota_obs(jl, *, path: str, account=None):
    return jl.make_obs(
        at="2026-03-01T00:00:00Z", src="codex-quota", provider="codex",
        account=account,
        payload={"kind": "quota_window_snapshot", "source": "codex",
                 "source_root_key": "root-a", "source_path": path,
                 "line_offset": 0, "captured_at_utc": "2026-03-01T00:00:00Z",
                 "observed_slot": "primary",
                 "logical_limit_key": "limit-primary",
                 "limit_id": "native-primary", "limit_name": "Primary",
                 "window_minutes": 300, "used_percent": 12.0,
                 "resets_at_utc": "2026-03-02T00:00:00Z", "plan_type": "pro",
                 "individual_limit_json": None, "reached_type": None,
                 "observed_model": "gpt-5.3-codex"})


def test_retained_observation_bytes_are_normalized_inside_the_leg(
    tmp_path, monkeypatch
):
    """Normalizing a transient during the router pass has no effect on the
    record decoded later from its retained bytes, so the leg must normalize. An
    unstamped legacy Codex observation lands as `unattributed`, never NULL."""
    ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    import _lib_accounts
    ns["open_cache_db"]().close()
    _write_segment(core, "observations-2026-03.jsonl", [
        _quota_obs(jl, path="/codex/root-a/legacy.jsonl"),
        _quota_obs(jl, path="/codex/root-a/stamped.jsonl",
                   account="codex:acct-b"),
        _cutover_op(jr, jl, "2026-01-01T00:00:00Z", "acct-legacy-claude"),
    ], jl)
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(tmp_path / "rebuilt.db"))
    rows = dict(_quota_cache_rows(core))
    assert rows["/codex/root-a/legacy.jsonl"] == _lib_accounts.UNATTRIBUTED
    assert rows["/codex/root-a/stamped.jsonl"] == "codex:acct-b"


# ==========================================================================
# The other converted whole-prefix readers (spec §7)
# ==========================================================================


def test_the_prefix_matcher_agrees_with_what_the_rebuild_published(
    tmp_path, monkeypatch
):
    """Its only output is a selection compared against
    `journal_effective_events`, so it must reach the same verdict from the same
    filtered retention — including the same violation fingerprints."""
    ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    records = [
        jl.make_account_observe("2026-01-01T00:00:00Z", "acct-a", "claude"),
        _cutover_op(jr, jl, "2026-01-01T00:00:00Z", "acct-a"),
    ]
    for index in range(50):
        records.append(_quota_obs(jl, path=f"/codex/root-a/r{index}.jsonl"))
        records.append(jl.make_evt(
            "weekly_cost_snapshot", f"wcs:{index}", "2026-02-01T00:00:00Z", {
                "captured_at_utc": "2026-02-01T00:00:00Z",
                "week_start_date": "2026-02-01", "week_end_date": "2026-02-08",
                "week_start_at": "2026-02-01T00:00:00+00:00",
                "week_end_at": "2026-02-08T00:00:00+00:00",
                "range_start_iso": "2026-02-01T00:00:00+00:00",
                "range_end_iso": "2026-02-08T00:00:00+00:00",
                "cost_usd": 1.0, "mode": "auto", "project": None,
                "account_key": "acct-a"}))
    _write_segment(core, "observations-2026-02.jsonl", records, jl)

    dest = tmp_path / "rebuilt.db"
    hw = jr.journal_high_water()
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest), high_water=hw)
    assert jr.stats_index_matches_journal_prefix(dest, hw) is True


def test_the_prefix_matcher_rejects_a_stale_index(tmp_path, monkeypatch):
    """Non-vacuity: the matcher must still say no when the index is behind."""
    ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    ns["open_cache_db"]().close()
    _write_segment(core, "observations-2026-02.jsonl", [
        jl.make_evt("weekly_cost_snapshot", "wcs:1", "2026-02-01T00:00:00Z", {
            "captured_at_utc": "2026-02-01T00:00:00Z",
            "week_start_date": "2026-02-01", "week_end_date": "2026-02-08",
            "week_start_at": "2026-02-01T00:00:00+00:00",
            "week_end_at": "2026-02-08T00:00:00+00:00",
            "range_start_iso": "2026-02-01T00:00:00+00:00",
            "range_end_iso": "2026-02-08T00:00:00+00:00",
            "cost_usd": 1.0, "mode": "auto", "project": None}),
    ], jl)
    dest = tmp_path / "rebuilt.db"
    stale_hw = jr.journal_high_water()
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest), high_water=stale_hw)
    _write_segment(core, "observations-2026-02.jsonl", [
        jl.make_evt("weekly_cost_snapshot", "wcs:2", "2026-03-01T00:00:00Z", {
            "captured_at_utc": "2026-03-01T00:00:00Z",
            "week_start_date": "2026-03-01", "week_end_date": "2026-03-08",
            "week_start_at": "2026-03-01T00:00:00+00:00",
            "week_end_at": "2026-03-08T00:00:00+00:00",
            "range_start_iso": "2026-03-01T00:00:00+00:00",
            "range_end_iso": "2026-03-08T00:00:00+00:00",
            "cost_usd": 2.0, "mode": "auto", "project": None}),
    ], jl)
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is False


def test_correction_commit_high_water_stops_at_the_first_match(
    tmp_path, monkeypatch
):
    """The early return existed before, but arrived after `_read_range` had
    already materialized the whole prefix. Count the bytes the streaming form
    reads and prove it stops near the marker."""
    _ns, jr, jl = _load(tmp_path, monkeypatch)
    core = importlib.import_module("_cctally_core")
    batch = jl.make_correction_batch(
        batch_id="fix-1", family="claude_usage", at="2026-02-01T00:00:00Z",
        actions=[{"action": "tombstone", "id": "wcs:1", "rev": 1,
                  "at": "2026-02-01T00:00:00Z", "payload": None}])
    records = [*batch] + [_claude_obs(jl, UTC_AT) for _ in range(200)]
    _write_segment(core, "observations-2026-02.jsonl", records, jl)

    hw = jr.journal_high_water()
    read = {"bytes": 0}
    original = jr._iter_segment_lines

    def counting(seg_path, lo, hi, *, on_bytes=None):
        for item in original(seg_path, lo, hi, on_bytes=on_bytes):
            read["bytes"] += len(item[2]) + 1
            yield item

    monkeypatch.setattr(jr, "_iter_segment_lines", counting)
    found = jr._correction_commit_high_water("fix-1", hw)
    assert found is not None
    total = sum(len(jl.encode_line(record)) for record in records)
    assert read["bytes"] < total / 4
