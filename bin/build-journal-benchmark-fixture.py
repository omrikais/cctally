#!/usr/bin/env python3
"""Generate a PRODUCTION-SHAPED journal for the rebuild benchmark (#496 S4).

The previous builder wrote **one** segment containing six represented families
and **zero** Codex quota observations, and `tests/conftest.py::redirect_paths`
points `CACHE_DB_PATH` at a `share/cache.db` it never creates — so
`_rebuild_quota_cache_leg` returned immediately and the benchmark never
exercised the leg that consumes 92.86% of a real journal.

A read-only census of the maintainer's journal on 2026-08-06 measured the mix
this builder reproduces:

    Codex quota observations   1,814,466   92.86%   1,641.6 MB
    correction                    64,248    3.29%      39.2 MB
    Claude record-usage obs        40,262    2.06%      13.1 MB
    evt                            34,807    1.78%      26.8 MB
    op                                230    0.01%       0.1 MB
    correction_batch                    4    0.00%       0.0 MB
    total                      1,954,007            1,720.8 MB   (880.6 B/line)

`build()` asserts these properties about ITSELF and raises `FixtureShapeError`
when they drift, so a later edit fails the build rather than silently restoring
the hole.

The artifact is written into the APP_DIR resolved from the ambient env
(``CCTALLY_DATA_DIR`` / ``HOME``) — a SCRATCH dir; it MUST NOT enter the git
tree. Driven by tests/test_rebuild_benchmark.py and
tests/test_rebuild_read_path_496_s4.py, or standalone:

    CCTALLY_DATA_DIR=/tmp/bench HOME=/tmp/bench-home \\
        python3 bin/build-journal-benchmark-fixture.py 1000000
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _fixture_builders import fixture_source_timestamp_z  # noqa: E402


def _load_cctally():
    """Load the cctally script when executed standalone. In a pytest context the
    harness has already loaded it, so this re-uses the existing module."""
    bin_dir = str(pathlib.Path(__file__).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    if "cctally" not in sys.modules:
        from importlib.machinery import SourceFileLoader
        import importlib.util
        loader = SourceFileLoader("cctally", os.path.join(bin_dir, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cctally"] = mod
        loader.exec_module(mod)


class FixtureShapeError(AssertionError):
    """The generated journal does not model production."""


# -- measured production shape, and the bands the builder holds itself to ----

QUOTA_SHARE = 0.929
QUOTA_SHARE_BAND = (0.90, 0.95)
MEAN_LINE_BYTES_BAND = (700.0, 1100.0)
CUTOVER_FRACTION = 0.929
CUTOVER_FRACTION_BAND = (0.85, 0.98)

#: Bootstrap segments sort BEFORE observation segments (`segment_sort_key`), so
#: the layout also exercises the two-class canonical order rather than one file.
_BOOTSTRAP_SEGMENTS = (
    ("bootstrap-20240101T000000_000001.jsonl", 0.14),
    ("bootstrap-20240102T000000_000002.jsonl", 0.13),
    ("bootstrap-20240103T000000_000003.jsonl", 0.13),
)
_OBSERVATION_SEGMENTS = (
    ("observations-2024-01.jsonl", 0.20),
    ("observations-2024-02.jsonl", 0.20),
    ("observations-2024-03.jsonl", 0.20),
)
#: Extra inert Codex quota history for the §8.3 slope gate lands in segments
#: named from here on, AFTER everything the stats fold consumes, so the
#: stats-side population is identical between the two fixtures.
#:
#: It is spread across SEVERAL segments rather than piled into one, and no
#: extra segment is allowed to be larger than the base layout's per-segment
#: budget. Real segments are cut by UTC month, so doubling retained history
#: genuinely adds segments rather than growing one — and the streaming
#: protocol-evidence accumulator buffers the segment it is reading (§5.2), so a
#: single oversized extra segment would raise peak allocation for a reason that
#: has nothing to do with what the slope gate is measuring.
_EXTRA_QUOTA_SEGMENTS = tuple(
    f"observations-2024-{month:02d}.jsonl" for month in range(4, 13)
)

_BASE = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
_ROOT_SCOPE = "codex-root-a"
_CLAUDE_ACCOUNT = "claude:acct-benchmark-primary"
_CODEX_ACCOUNT = "codex:acct-benchmark-primary"

#: Production Codex quota observations average 904 bytes encoded, and the whole
#: journal averages 880.6. The real payload carries the provider's verbatim
#: per-limit JSON, which is what makes the line that large; reproducing the SIZE
#: is what matters, because the read path's cost and the retained-bytes
#: residency are both linear in it. This shape encodes to 925 bytes.
_LIMIT_JSON = json.dumps({
    "primary": {
        "used_percent": 41.2, "window_minutes": 300,
        "resets_in_seconds": 9182, "limit_name": "Primary",
        "plan": "pro", "reached": None,
    },
    "secondary": {
        "used_percent": 8.7, "window_minutes": 10080,
        "resets_in_seconds": 402913, "limit_name": "Weekly",
        "plan": "pro", "reached": None,
    },
}, separators=(",", ":"))

#: One emission round reproduces the census mix: 74 Codex quota observations
#: against 2 Claude observations, the tick's evts, and — every third round — a
#: five-line correction batch. That lands quota at 92.9% of the journal and the
#: mean encoded line near the measured 880.6 bytes.
_QUOTA_PER_TICK = 74
_CORRECTION_EVERY_TICKS = 3


def _iso(minutes: int) -> str:
    return fixture_source_timestamp_z(_BASE + dt.timedelta(minutes=minutes))


class _SegmentWriter:
    """Append encoded lines across a fixed segment layout.

    Rolls to the next segment when the current one's line budget is spent. A
    record group may straddle a boundary — that is what a real journal does, and
    the effective selector's ordering rules are defined over the record STREAM,
    not over one file.
    """

    def __init__(self, journal_dir, layout, total_lines):
        self._dir = journal_dir
        self._budgets = [
            (name, max(1, int(round(total_lines * share))))
            for name, share in layout
        ]
        self._index = 0
        self._written_in_segment = 0
        self._handle = None
        self.lines = 0
        self.bytes = 0
        self.per_segment: dict = {}
        self._open()

    def _open(self):
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
        name = self._budgets[self._index][0]
        self._handle = open(self._dir / name, "ab", buffering=1024 * 1024)
        self._written_in_segment = 0
        self.per_segment.setdefault(name, 0)

    def write(self, encoded: bytes) -> None:
        budget = self._budgets[self._index][1]
        if (self._written_in_segment >= budget
                and self._index < len(self._budgets) - 1):
            self._index += 1
            self._open()
        self._handle.write(encoded)
        self._written_in_segment += 1
        self.per_segment[self._budgets[self._index][0]] += 1
        self.lines += 1
        self.bytes += len(encoded)

    def close(self):
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None


def _quota_obs(J, *, index: int, at: str, inert: bool = False):
    """One real-shaped `quota_window_snapshot` observation.

    `inert=True` produces the same SIZE and the same record family but reuses
    one file identity and one quota window, varying only the natural key's byte
    offset. That form exists for the §8.3 slope gate: the cache leg keeps
    per-file and per-window state of its own, so history that introduced fresh
    windows would grow peak allocation for a reason that has nothing to do with
    what the retained observation bytes cost. Realism is the base fixture's job.
    """
    if inert:
        slot, limit, window, name = "primary", "limit-primary", 300, "Primary"
        path = f"/codex/{_ROOT_SCOPE}/rollout-inert.jsonl"
        resets = _iso(300)
        offset = index * 512
    else:
        even = index % 2 == 0
        slot = "primary" if even else "secondary"
        limit = "limit-primary" if even else "limit-weekly"
        window = 300 if even else 10080
        name = "Primary" if even else "Weekly"
        path = f"/codex/{_ROOT_SCOPE}/rollout-{index // 4096:04d}.jsonl"
        resets = _iso(300 + (index % 997) * 5)
        offset = (index % 4096) * 512
    return J.make_obs(
        at=at, src="codex-quota", provider="codex",
        payload={
            "kind": "quota_window_snapshot",
            "source": "codex",
            "source_root_key": _ROOT_SCOPE,
            "source_path": path,
            "line_offset": offset,
            "captured_at_utc": at,
            "observed_slot": slot,
            "logical_limit_key": limit,
            "limit_id": "native-primary",
            "limit_name": name,
            "window_minutes": window,
            "used_percent": round((index % 1000) / 10.0, 1),
            "resets_at_utc": resets,
            "plan_type": "pro",
            "individual_limit_json": _LIMIT_JSON,
            "reached_type": None,
            "observed_model": "gpt-5.3-codex",
        },
    )


def _claude_obs(J, *, tick: int, at: str, week_key: int):
    return J.make_obs(
        at=at, src="record-usage", provider="claude",
        payload={
            "weekly_percent": float((tick % 100) + 1),
            "resets_at": week_key,
            "source": "statusline",
            "captured_at": at,
            "five_hour_percent": float((tick % 12) * 8),
        },
    )


def _tick_evts(J, *, tick: int, at: str):
    """The evt families a real tick produces.

    Returns `(evts, wcs_id)`. Every tick emits the two Model-A generic folds;
    the harvest families (`percent_milestone`, `five_hour_milestone`,
    `five_hour_block_close`) ride the same cadence they do in production, so the
    canonical dumps cover the derived-FK seam rather than only the flat folds.
    """
    week = tick // 60
    wsd_date = (_BASE + dt.timedelta(days=7 * week)).date()
    wsd = wsd_date.isoformat()
    wed = (wsd_date + dt.timedelta(days=7)).isoformat()
    wsa = f"{wsd}T00:00:00+00:00"
    wea = f"{wed}T00:00:00+00:00"
    pct = float((tick % 60) + 1)
    slot = tick // 12
    block_start = _BASE + dt.timedelta(hours=5 * slot)
    fh_key = int(block_start.timestamp())
    fh_resets = (block_start + dt.timedelta(hours=5)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    fh_pct = float((tick % 12 + 1) * 8)
    sa_id = f"sa:t{tick:08d}"
    wcs_id = f"wcs:t{tick:08d}"
    sa = J.make_evt("snapshot_accept", sa_id, at, {
        "captured_at_utc": at, "week_start_date": wsd, "week_end_date": wed,
        "week_start_at": wsa, "week_end_at": wea, "weekly_percent": pct,
        "source": "statusline", "payload_json": "{}", "page_url": None,
        "five_hour_percent": fh_pct,
        "five_hour_resets_at": fh_resets,
        "five_hour_window_key": fh_key,
        "account_key": _CLAUDE_ACCOUNT,
    })
    wcs = J.make_evt("weekly_cost_snapshot", wcs_id, at, {
        "captured_at_utc": at, "week_start_date": wsd, "week_end_date": wed,
        "week_start_at": wsa, "week_end_at": wea,
        "range_start_iso": wsa, "range_end_iso": wea,
        "cost_usd": round(tick * 0.001, 4), "mode": "auto", "project": None,
        "account_key": _CLAUDE_ACCOUNT,
    })
    evts = [sa, wcs]
    if tick % 12 == 0:
        evts.append(J.make_evt(
            "five_hour_block_close", f"fhbc:{fh_key}", at, {
                "five_hour_window_key": fh_key,
                "five_hour_resets_at": fh_resets,
                "block_start_at": block_start.isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
                "first_observed_at_utc": at, "last_observed_at_utc": at,
                "final_five_hour_percent": 96.0,
                "seven_day_pct_at_block_start": pct,
                "seven_day_pct_at_block_end": pct,
                "crossed_seven_day_reset": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cache_create_tokens": 0, "total_cache_read_tokens": 0,
                "total_cost_usd": 0.0, "is_closed": 1,
                "created_at_utc": at, "last_updated_at_utc": at,
                # Non-empty child sets, so the canonical dumps cover the
                # `_replace_block_children` seam rather than an empty table.
                "_models": [{
                    "five_hour_window_key": fh_key,
                    "model": "claude-sonnet-4-5-20250929",
                    "input_tokens": 10, "output_tokens": 5,
                    "cache_create_tokens": 0, "cache_read_tokens": 0,
                    "cost_usd": 0.0, "entry_count": 1,
                }],
                "_projects": [{
                    "five_hour_window_key": fh_key,
                    "project_path": "/benchmark/project",
                    "input_tokens": 10, "output_tokens": 5,
                    "cache_create_tokens": 0, "cache_read_tokens": 0,
                    "cost_usd": 0.0, "entry_count": 1,
                }],
                "account_key": _CLAUDE_ACCOUNT,
            }))
    if tick % 4 == 0:
        evts.append(J.make_evt(
            "percent_milestone", f"pm:{wsd}:{tick:08d}", at, {
                "captured_at_utc": at, "week_start_date": wsd,
                "week_end_date": wed, "week_start_at": wsa, "week_end_at": wea,
                "percent_threshold": int(pct),
                "cumulative_cost_usd": round(tick * 0.01, 4),
                "marginal_cost_usd": None,
                "five_hour_percent_at_crossing": fh_pct,
                "alerted_at": None, "usage_snapshot_ref": sa_id,
                "cost_snapshot_ref": wcs_id, "reset_event_ref": "0",
                "account_key": _CLAUDE_ACCOUNT,
            }))
    if tick % 6 == 0:
        evts.append(J.make_evt(
            "five_hour_milestone", f"fhm:{fh_key}:{tick:08d}", at, {
                "captured_at_utc": at, "five_hour_window_key": fh_key,
                "percent_threshold": int(fh_pct),
                "block_input_tokens": 0, "block_output_tokens": 0,
                "block_cache_create_tokens": 0, "block_cache_read_tokens": 0,
                "block_cost_usd": 0.0, "marginal_cost_usd": None,
                "seven_day_pct_at_crossing": pct, "alerted_at": None,
                "usage_snapshot_ref": sa_id, "reset_event_ref": "0",
                "account_key": _CLAUDE_ACCOUNT,
            }))
    return evts, wcs_id


def _correction_actions(J, corrected):
    """Replace each named `weekly_cost_snapshot` with a rev-1 restatement."""
    actions = []
    for wcs_id, at, cost in corrected:
        actions.append({
            "action": "replace", "id": wcs_id, "rev": 1, "at": at,
            "payload": {
                "kind": "weekly_cost_snapshot",
                "captured_at_utc": at,
                "week_start_date": "2024-01-01", "week_end_date": "2024-01-08",
                "week_start_at": "2024-01-01T00:00:00+00:00",
                "week_end_at": "2024-01-08T00:00:00+00:00",
                "range_start_iso": "2024-01-01T00:00:00+00:00",
                "range_end_iso": "2024-01-08T00:00:00+00:00",
                "cost_usd": cost, "mode": "calculate", "project": None,
                "account_key": _CLAUDE_ACCOUNT,
            },
        })
    return actions


def _orphan_commit_marker(J, batch_id: str, at: str):
    """A commit marker with no begin — the `commit_without_begin` violation.

    Built through `make_correction_batch` so the manifest hash is genuine; only
    the commit half is appended, which is exactly the crash shape the selector
    taints.
    """
    records = J.make_correction_batch(
        batch_id=batch_id, family="claude_usage", at=at,
        actions=_correction_actions(J, [(f"wcs:orphan:{batch_id}", at, 1.5)]),
    )
    return records[-1]


def build(
    target_lines: int = 1_000_000,
    *,
    seed_cache: bool = True,
    extra_quota_lines: int = 0,
    with_resolution: bool = True,
    verify_shape: bool = True,
) -> dict:
    """Direct-write a production-shaped journal. Deterministic; O(n).

    Returns the measured shape. `extra_quota_lines` appends inert Codex quota
    history AFTER every record the stats fold consumes, which is what the §8.3
    slope gate needs: the retained decision population is identical between the
    two fixtures and only the observation population grows.
    """
    _load_cctally()
    import _cctally_core
    import _cctally_journal as jr
    import _lib_journal as J

    journal_dir = _cctally_core.JOURNAL_DIR
    journal_dir.mkdir(parents=True, exist_ok=True)

    layout = _BOOTSTRAP_SEGMENTS + _OBSERVATION_SEGMENTS
    writer = _SegmentWriter(journal_dir, layout, target_lines)

    counts = {
        "obs_quota": 0, "obs_claude": 0, "evt": 0,
        "correction": 0, "correction_batch": 0, "op": 0,
    }
    records: list = []

    def emit(record):
        writer.write(J.encode_line(record))
        t = record.get("t")
        if t == "obs":
            key = ("obs_quota" if jr._is_codex_quota_obs(record)
                   else "obs_claude")
        elif t == "op":
            key = "op"
        elif t == "correction":
            key = "correction"
        elif t == "correction_batch":
            key = "correction_batch"
        else:
            key = "evt"
        counts[key] += 1
        records.append(record)

    # Accounts machinery first, so the observe ops create the registry rows the
    # fold-time `last_seen_utc` derivation is allowed to touch.
    emit(J.make_account_observe(
        _iso(0), _CLAUDE_ACCOUNT, "claude",
        natural_id="benchmark-primary", email="primary@example.test",
        plan_type="max"))
    emit(J.make_account_observe(
        _iso(0), _CODEX_ACCOUNT, "codex",
        natural_id="benchmark-codex", email="codex@example.test",
        plan_type="pro"))
    emit(J.make_codex_file_account(
        _iso(1), root_scope=_ROOT_SCOPE,
        file_identity=f"{_ROOT_SCOPE}:rollout-0000",
        incarnation=1, from_offset=0, account_key=_CODEX_ACCOUNT))

    cutover_at_line = int(target_lines * CUTOVER_FRACTION)
    cutover_written = False
    quota_index = 0
    tick = 0
    pending_corrections: list = []
    batch_seq = 0

    while writer.lines < target_lines:
        if not cutover_written and writer.lines >= cutover_at_line:
            op = J.make_op(
                at=_iso(tick), src="accounts-cutover",
                payload={"kind": "accounts_cutover",
                         "claude_legacy_account": _CLAUDE_ACCOUNT})
            op["id"] = jr.CUTOVER_OP_ID
            emit(op)
            cutover_written = True

        at = _iso(tick)
        week_key = int((_BASE + dt.timedelta(days=7 * (tick // 60))).timestamp())
        for _ in range(2):
            emit(_claude_obs(J, tick=tick, at=at, week_key=week_key))
        evts, wcs_id = _tick_evts(J, tick=tick, at=at)
        for evt in evts:
            emit(evt)
        pending_corrections.append((wcs_id, at, round(tick * 0.002, 4)))
        for _ in range(_QUOTA_PER_TICK):
            emit(_quota_obs(J, index=quota_index, at=at))
            quota_index += 1
        tick += 1

        if len(pending_corrections) >= _CORRECTION_EVERY_TICKS:
            batch_seq += 1
            for record in J.make_correction_batch(
                batch_id=f"bench-batch-{batch_seq:05d}",
                family="claude_usage", at=at,
                actions=_correction_actions(J, pending_corrections),
            ):
                emit(record)
            pending_corrections = []

        if tick % 8 == 0:
            emit(J.make_op(
                at=at, src="weekly-credit",
                payload={"kind": "weekly_credit_floor",
                         "week_start_date": (
                             _BASE + dt.timedelta(days=7 * (tick // 60))
                         ).date().isoformat(),
                         "effective_at_utc": at,
                         "observed_pre_credit_pct": float((tick % 60) + 1),
                         "applied_at_utc": at,
                         "account_key": _CLAUDE_ACCOUNT}))

    if not cutover_written:
        op = J.make_op(
            at=_iso(tick), src="accounts-cutover",
            payload={"kind": "accounts_cutover",
                     "claude_legacy_account": _CLAUDE_ACCOUNT})
        op["id"] = jr.CUTOVER_OP_ID
        emit(op)
        cutover_written = True

    # -- the four correction shapes -----------------------------------------
    # 1. complete and valid — already emitted, once per ten ticks.
    # 2. incomplete: begin + actions, no commit. The selector ignores it and
    #    reports NO violation, which is the crash-before-commit contract.
    incomplete = J.make_correction_batch(
        batch_id="bench-incomplete", family="claude_usage", at=_iso(tick),
        actions=_correction_actions(
            J, [(f"wcs:t{0:08d}", _iso(0), 9.75)]),
    )
    for record in incomplete[:-1]:
        emit(record)
    # 3. tainted: a commit marker with no begin.
    emit(_orphan_commit_marker(J, "bench-tainted", _iso(tick)))
    # 4. acknowledged: a second orphan commit, resolved by the audit op below.
    emit(_orphan_commit_marker(J, "bench-acknowledged", _iso(tick)))

    writer.close()

    resolution_id = None
    if with_resolution:
        selection = J.resolve_effective_events(records)
        target = next(
            (violation for violation in selection.protocol_violations
             if violation.batch_id == "bench-acknowledged"),
            None,
        )
        if target is None:
            raise FixtureShapeError(
                "the acknowledged-shape batch produced no protocol violation")
        segments = jr.list_segments()
        last = segments[-1]
        high_water = (last, os.path.getsize(journal_dir / last))
        audit = J.make_protocol_resolution(
            at=_iso(tick + 1), violations=[target],
            journal_high_water=high_water,
            journal_prefix_hash=jr.journal_prefix_hash(high_water),
        )
        with open(journal_dir / last, "ab") as handle:
            encoded = J.encode_line(audit)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        writer.lines += 1
        writer.bytes += len(encoded)
        writer.per_segment[last] = writer.per_segment.get(last, 0) + 1
        counts["op"] += 1
        records.append(audit)
        resolution_id = audit["id"]

    # The production-shape verdict is about the MODELLED journal, before any
    # inert history the slope gate bolts on: that extra history is a deliberate
    # perturbation of the mix, not a drift in it.
    base_shape = {
        "lines": writer.lines,
        "bytes": writer.bytes,
        "counts": dict(counts),
    }

    if extra_quota_lines > 0:
        per_segment = max(
            1, int(round(target_lines * max(share for _n, share in layout))))
        remaining = extra_quota_lines
        for name in _EXTRA_QUOTA_SEGMENTS:
            if remaining <= 0:
                break
            take = min(per_segment, remaining)
            with open(journal_dir / name, "ab", buffering=1024 * 1024) as handle:
                for _ in range(take):
                    encoded = J.encode_line(_quota_obs(
                        J, index=quota_index, at=_iso(tick + 2), inert=True))
                    handle.write(encoded)
                    quota_index += 1
                    writer.lines += 1
                    writer.bytes += len(encoded)
                    counts["obs_quota"] += 1
                handle.flush()
                os.fsync(handle.fileno())
            writer.per_segment[name] = take
            remaining -= take
        if remaining > 0:
            raise FixtureShapeError(
                f"{extra_quota_lines} extra quota lines do not fit in "
                f"{len(_EXTRA_QUOTA_SEGMENTS)} segments of {per_segment} lines")

    if seed_cache:
        _seed_cache()

    shape = {
        "total_lines": writer.lines,
        "total_bytes": writer.bytes,
        "mean_line_bytes": writer.bytes / max(1, writer.lines),
        "counts": dict(counts),
        "quota_share": counts["obs_quota"] / max(1, writer.lines),
        "segments": jr.list_segments(),
        "lines_per_segment": dict(writer.per_segment),
        "cutover_line_fraction": cutover_at_line / max(1, writer.lines),
        "resolution_op_id": resolution_id,
        "extra_quota_lines": extra_quota_lines,
        "retained_decision_lines": (
            counts["evt"] + counts["op"] + counts["correction"]
            + counts["correction_batch"]),
        # The modelled journal on its own — what `_verify_shape` judges.
        "modelled": {
            "total_lines": base_shape["lines"],
            "total_bytes": base_shape["bytes"],
            "mean_line_bytes": (
                base_shape["bytes"] / max(1, base_shape["lines"])),
            "counts": base_shape["counts"],
            "quota_share": (
                base_shape["counts"]["obs_quota"]
                / max(1, base_shape["lines"])),
            "cutover_line_fraction": (
                cutover_at_line / max(1, base_shape["lines"])),
        },
    }
    if verify_shape:
        _verify_shape(shape, with_resolution=with_resolution)
    return shape


def _seed_cache() -> None:
    """Create cache.db WITH its schema and WITHOUT the target quota rows.

    `_rebuild_quota_cache_leg` returns immediately when cache.db is absent, so a
    fixture that skips this never runs the leg that consumes 93% of the journal
    — the exact hole the previous benchmark had. The quota rows are left absent
    so a replay that writes nothing cannot pass by finding them already there.
    """
    import _cctally_cache

    conn = _cctally_cache.open_cache_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, "
            " last_seen_utc) VALUES (?,?,?,?)",
            (_ROOT_SCOPE, f"/codex/{_ROOT_SCOPE}", _iso(0), _iso(0)),
        )
        conn.commit()
        present = conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0]
        if int(present) != 0:
            raise FixtureShapeError(
                "seeded cache.db already carries quota rows; a replay that "
                "wrote nothing would pass")
    finally:
        conn.close()


def _verify_shape(shape: dict, *, with_resolution: bool) -> None:
    """Fail the BUILD when the generated journal stops modelling production.

    Judged over the MODELLED journal, excluding any inert history the §8.3
    slope gate appended: that history is a deliberate perturbation of the mix,
    and folding it in here would make the self-check refuse the very fixture it
    exists to support.
    """
    problems = []
    modelled = shape["modelled"]
    low, high = QUOTA_SHARE_BAND
    if not low <= modelled["quota_share"] <= high:
        problems.append(
            f"quota observation share {modelled['quota_share']:.4f} outside "
            f"[{low}, {high}]")
    low, high = MEAN_LINE_BYTES_BAND
    if not low <= modelled["mean_line_bytes"] <= high:
        problems.append(
            f"mean encoded line {modelled['mean_line_bytes']:.1f} B outside "
            f"[{low}, {high}] (production measured 880.6 B)")
    bootstrap = [s for s in shape["segments"] if s.startswith("bootstrap-")]
    observations = [s for s in shape["segments"] if s.startswith("observations-")]
    if len(bootstrap) < 2:
        problems.append(f"expected several bootstrap segments, got {bootstrap}")
    if len(observations) < 2:
        problems.append(
            f"expected several observation segments, got {observations}")
    low, high = CUTOVER_FRACTION_BAND
    if not low <= modelled["cutover_line_fraction"] <= high:
        problems.append(
            f"cutover op at {modelled['cutover_line_fraction']:.3f} of the "
            f"journal, outside [{low}, {high}]")
    for family in ("obs_quota", "obs_claude", "evt", "op", "correction",
                   "correction_batch"):
        if modelled["counts"][family] <= 0:
            problems.append(f"no {family} records were emitted")
    if with_resolution and not shape["resolution_op_id"]:
        problems.append("no journal_protocol_resolution op was appended")
    if problems:
        raise FixtureShapeError(
            "generated journal does not model production:\n  - "
            + "\n  - ".join(problems))


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    result = build(target)
    import _cctally_journal
    print(json.dumps(result, indent=2))
    print(f"journal built: {result['total_lines']} lines across "
          f"{len(_cctally_journal.list_segments())} segment(s)")
