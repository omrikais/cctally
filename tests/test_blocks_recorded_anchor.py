"""Tests for the blocks subcommand's real-window anchoring."""
from __future__ import annotations

import datetime as dt
import pytest

from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def _entry(ns, ts: dt.datetime, model: str = "claude-sonnet-4-6"):
    """Build a UsageEntry matching production's dataclass shape."""
    UsageEntry = ns["UsageEntry"]
    return UsageEntry(
        timestamp=ts,
        model=model,
        usage={"input_tokens": 100, "output_tokens": 200,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        cost_usd=None,
    )


def test_group_accepts_explicit_now_and_uses_it_for_is_active(ns):
    """_group_entries_into_blocks honors an explicit `now` param.

    Proves the clock-through: an entry whose floor+5h lies BEFORE
    `now` yields is_active=False; same entries with `now` BEFORE
    that boundary yield is_active=True.
    """
    group = ns["_group_entries_into_blocks"]
    base = dt.datetime(2026, 4, 23, 8, 0, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, base + dt.timedelta(minutes=10))]

    before_end = base + dt.timedelta(hours=2)   # inside the 5h window
    after_end = base + dt.timedelta(hours=10)  # well past reset

    blocks_active = group(entries, mode="auto", now=before_end)
    blocks_closed = group(entries, mode="auto", now=after_end)

    assert len(blocks_active) == 1 and blocks_active[0].is_active is True
    assert len(blocks_closed) == 1 and blocks_closed[0].is_active is False


def test_block_anchor_default_is_heuristic(ns):
    """Block instantiated without `anchor` defaults to 'heuristic'."""
    Block = ns["Block"]
    import datetime as dt
    b = Block(
        start_time=dt.datetime(2026, 4, 23, tzinfo=dt.timezone.utc),
        end_time=dt.datetime(2026, 4, 23, 5, tzinfo=dt.timezone.utc),
        actual_end_time=None,
        is_active=False,
        is_gap=False,
        entries_count=0,
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        total_tokens=0, cost_usd=0.0,
        models=[], burn_rate=None, projection=None,
    )
    assert b.anchor == "heuristic"


def test_block_anchor_explicit_recorded(ns):
    """Anchor can be set explicitly to 'recorded'."""
    Block = ns["Block"]
    import datetime as dt
    b = Block(
        start_time=dt.datetime(2026, 4, 23, tzinfo=dt.timezone.utc),
        end_time=dt.datetime(2026, 4, 23, 5, tzinfo=dt.timezone.utc),
        actual_end_time=None,
        is_active=False, is_gap=False,
        entries_count=0,
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        total_tokens=0, cost_usd=0.0,
        models=[], burn_rate=None, projection=None,
        anchor="recorded",
    )
    assert b.anchor == "recorded"


def test_group_accepts_recorded_windows_param_without_effect_when_empty(ns):
    """When recorded_windows is None or [], behavior matches legacy."""
    group = ns["_group_entries_into_blocks"]
    base = dt.datetime(2026, 4, 23, 8, 15, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, base)]
    now = base + dt.timedelta(hours=2)

    baseline = group(entries, mode="auto", now=now)
    with_none = group(entries, mode="auto", recorded_windows=None, now=now)
    with_empty = group(entries, mode="auto", recorded_windows=[], now=now)

    # Same anchor, same start/end — partition didn't alter anything
    assert baseline[0].anchor == with_none[0].anchor == with_empty[0].anchor == "heuristic"
    assert baseline[0].start_time == with_none[0].start_time == with_empty[0].start_time


def test_group_partitions_entry_into_recorded_bucket(ns):
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entry_ts = R - dt.timedelta(hours=1)
    entries = [_entry(ns, entry_ts)]
    now = R - dt.timedelta(minutes=30)

    blocks = group(entries, mode="auto", recorded_windows=[R], now=now)

    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1
    b = non_gap[0]
    assert b.anchor == "recorded"
    assert b.start_time == R - BLOCK_DURATION
    assert b.end_time == R
    assert b.is_active is True
    assert b.entries_count == 1


def test_group_partition_left_closed_interval(ns):
    """entry.timestamp == R - 5h belongs to window R (left-closed)."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, R - BLOCK_DURATION)]
    blocks = group(entries, mode="auto", recorded_windows=[R], now=R)

    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1 and non_gap[0].anchor == "recorded"


def test_group_partition_right_open_interval(ns):
    """entry.timestamp == R belongs to the NEXT window (right-open)."""
    group = ns["_group_entries_into_blocks"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, R)]
    now = R + dt.timedelta(hours=1)
    blocks = group(entries, mode="auto", recorded_windows=[R], now=now)

    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1
    # No window covers R exactly (R is the open end) — entry falls to heuristic
    assert non_gap[0].anchor == "heuristic"


def test_group_entry_outside_all_windows_is_heuristic(ns):
    group = ns["_group_entries_into_blocks"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, R + dt.timedelta(hours=3))]  # way past R
    now = R + dt.timedelta(hours=4)
    blocks = group(entries, mode="auto", recorded_windows=[R], now=now)

    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1 and non_gap[0].anchor == "heuristic"


def test_group_empty_recorded_window_is_skipped(ns):
    """Recorded R with no entries in [R-5h, R) → no block emitted."""
    group = ns["_group_entries_into_blocks"]
    R_empty = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    R_used = dt.datetime(2026, 4, 23, 20, 0, tzinfo=dt.timezone.utc)
    entry_ts = R_used - dt.timedelta(hours=1)
    entries = [_entry(ns, entry_ts)]
    now = R_used - dt.timedelta(minutes=15)
    blocks = group(entries, mode="auto",
                   recorded_windows=[R_empty, R_used], now=now)
    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1
    assert non_gap[0].end_time == R_used  # only the non-empty window made a block


def test_group_cc_span_across_two_adjacent_real_windows(ns):
    """Entries spanning R1 and R2 split into two recorded blocks."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R1 = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    R2 = R1 + BLOCK_DURATION  # abutting
    e1 = _entry(ns, R1 - dt.timedelta(minutes=30))  # in [R1-5h, R1)
    e2 = _entry(ns, R2 - dt.timedelta(minutes=30))  # in [R1, R2)
    now = R2 - dt.timedelta(minutes=15)
    blocks = group(entries=[e1, e2], mode="auto",
                   recorded_windows=[R1, R2], now=now)

    non_gap = sorted([b for b in blocks if not b.is_gap], key=lambda b: b.start_time)
    assert len(non_gap) == 2
    assert non_gap[0].end_time == R1 and non_gap[0].anchor == "recorded"
    assert non_gap[1].end_time == R2 and non_gap[1].anchor == "recorded"


def test_group_active_recorded_block_uses_real_window_for_elapsed(ns):
    """Active recorded block: burn_rate derived from (now - (R - 5h))."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entries = [_entry(ns, R - dt.timedelta(hours=2))]  # 3h into a 5h window
    now = R - dt.timedelta(hours=2)
    blocks = group(entries, mode="auto", recorded_windows=[R], now=now)
    b = [x for x in blocks if not x.is_gap][0]

    assert b.is_active is True
    # elapsed should equal now - (R - 5h) = 3h = 180 min (NOT 0 min from first entry)
    expected_elapsed_minutes = (now - (R - BLOCK_DURATION)).total_seconds() / 60
    assert expected_elapsed_minutes == pytest.approx(180.0)
    # tokens_per_minute = total_tokens / 180
    total_tokens = entries[0].usage["input_tokens"] + entries[0].usage["output_tokens"]
    assert b.burn_rate is not None
    assert b.burn_rate["tokensPerMinute"] == pytest.approx(total_tokens / 180.0)


def test_aggregate_block_active(ns):
    """_aggregate_block returns burn_rate + projection for active window."""
    agg = ns["_aggregate_block"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    start = dt.datetime(2026, 4, 23, 7, 0, tzinfo=dt.timezone.utc)
    end = start + BLOCK_DURATION
    now = start + dt.timedelta(hours=3)
    entries = [_entry(ns, start + dt.timedelta(minutes=30))]
    result = agg(entries, start, end, now, "auto")
    assert result["total_tokens"] == 300  # 100 input + 200 output
    assert result["burn_rate"] is not None
    assert result["burn_rate"]["tokensPerMinute"] == pytest.approx(300 / 180.0)
    assert result["projection"] is not None
    assert result["projection"]["totalTokens"] == int(300 / 180.0 * 300)


def test_aggregate_block_inactive_has_no_burn_rate(ns):
    agg = ns["_aggregate_block"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    start = dt.datetime(2026, 4, 23, 7, 0, tzinfo=dt.timezone.utc)
    end = start + BLOCK_DURATION
    now = end + dt.timedelta(hours=2)  # past window
    entries = [_entry(ns, start + dt.timedelta(minutes=30))]
    result = agg(entries, start, end, now, "auto")
    assert result["burn_rate"] is None
    assert result["projection"] is None


def test_cmd_blocks_reads_recorded_windows_from_db(ns, tmp_path, monkeypatch):
    """cmd_blocks SELECT DISTINCT five_hour_resets_at -> passes to grouper."""
    import io
    import sqlite3
    import contextlib
    import json
    import pathlib

    # Redirect module-level path constants (captured at load time from
    # pathlib.Path.home()) into our tmp share dir. Also set HOME so the
    # runtime Path.home() lookup used by _get_claude_data_dirs() resolves
    # to an empty .claude/projects tree — preventing sync_cache from
    # ingesting the host's real session files.
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setitem(ns, "APP_DIR", share)
    monkeypatch.setitem(ns, "DB_PATH", share / "stats.db")
    monkeypatch.setitem(ns, "CACHE_DB_PATH", share / "cache.db")
    monkeypatch.setitem(ns, "CACHE_LOCK_PATH", share / "cache.db.lock")
    monkeypatch.setitem(ns, "CACHE_LOCK_CODEX_PATH", share / "cache.db.codex.lock")
    monkeypatch.setitem(ns, "CONFIG_PATH", share / "config.json")
    monkeypatch.setitem(ns, "CONFIG_LOCK_PATH", share / "config.json.lock")
    monkeypatch.setitem(ns, "LOG_DIR", share / "logs")

    # Recorded reset timestamp, and deterministic "now".
    R = "2026-04-23T12:00:00+00:00"
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-04-23T11:30:00Z")

    # Empty ~/.claude/projects tree: sync_cache walks it but finds no JSONL
    # to ingest, leaving our seeded session_entries row intact.
    claude_projects = tmp_path / ".claude" / "projects"
    claude_projects.mkdir(parents=True)

    # Open via production open_db() to build the schema, then seed one
    # weekly_usage_snapshots row whose five_hour_resets_at == R.
    open_db = ns["open_db"]
    with open_db() as conn:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
            " source, payload_json, five_hour_percent, five_hour_resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-04-23T11:00:00Z", "2026-04-22", "2026-04-29",
             42.0, "test", "{}", 30.0, R),
        )
        conn.commit()

    # Seed one session entry inside [R - 5h, R) directly into cache.db
    # via the shared fixture helpers.
    import sys
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
    )
    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as cconn:
        seed_session_file(
            cconn,
            path="/fake/sess.jsonl",
            session_id="s1",
            project_path="/p",
        )
        seed_session_entry(
            cconn,
            source_path="/fake/sess.jsonl",
            line_offset=0,
            timestamp_utc="2026-04-23T10:30:00Z",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=200,
        )
        cconn.commit()

    # Invoke cmd_blocks --json.
    import argparse
    args = argparse.Namespace(since=None, until=None, breakdown=False, json=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ns["cmd_blocks"](args)
    assert rc == 0

    data = json.loads(buf.getvalue())
    blocks = [b for b in data["blocks"] if not b["isGap"]]
    assert len(blocks) == 1, (
        f"Expected exactly one non-gap block; got {len(blocks)}. "
        f"Payload: {data}"
    )

    # Block's startTime should be R - 5h (the recorded anchor), not
    # floor-to-hour(entry.ts) from the heuristic path.
    from datetime import datetime, timedelta, timezone
    expected_start = (
        datetime.fromisoformat(R) - timedelta(hours=5)
    ).astimezone(timezone.utc)
    assert blocks[0]["startTime"].startswith(
        expected_start.strftime("%Y-%m-%dT%H:%M")
    ), (
        f"Expected startTime to begin with {expected_start.isoformat()}, "
        f"got {blocks[0]['startTime']}. Block: {blocks[0]}"
    )


def _build_dummy_block(ns, *, anchor="heuristic", is_active=False, is_gap=False):
    Block = ns["Block"]
    import datetime as dt
    return Block(
        start_time=dt.datetime(2026, 4, 23, 8, 0, tzinfo=dt.timezone.utc),
        end_time=dt.datetime(2026, 4, 23, 13, 0, tzinfo=dt.timezone.utc),
        actual_end_time=dt.datetime(2026, 4, 23, 10, 0, tzinfo=dt.timezone.utc),
        is_active=is_active, is_gap=is_gap,
        entries_count=1, input_tokens=10, output_tokens=20,
        cache_creation_tokens=0, cache_read_tokens=0,
        total_tokens=30, cost_usd=0.01,
        models=["claude-sonnet-4-6"],
        burn_rate=None, projection=None,
        anchor=anchor,
    )


def test_render_table_prefixes_tilde_on_heuristic_row(ns):
    render = ns["_render_blocks_table"]
    b = _build_dummy_block(ns, anchor="heuristic")
    out = render([b])
    assert "~2026" in out


def test_render_table_no_tilde_on_recorded_row(ns):
    render = ns["_render_blocks_table"]
    b = _build_dummy_block(ns, anchor="recorded")
    out = render([b])
    assert "~2026" not in out


def test_render_table_footer_legend_when_heuristic_present(ns):
    render = ns["_render_blocks_table"]
    b = _build_dummy_block(ns, anchor="heuristic")
    out = render([b])
    assert "~ = approximate start" in out


def test_render_table_no_legend_when_all_recorded(ns):
    render = ns["_render_blocks_table"]
    b = _build_dummy_block(ns, anchor="recorded")
    out = render([b])
    assert "~ = approximate start" not in out


def test_render_table_gap_rows_unaffected_by_anchor(ns):
    """Gap rows must not receive the tilde marker."""
    render = ns["_render_blocks_table"]
    b_gap = _build_dummy_block(ns, is_gap=True)
    b_rec = _build_dummy_block(ns, anchor="recorded")
    out = render([b_gap, b_rec])
    # No tilde anywhere (gap has default 'heuristic' anchor but we skip it)
    assert "~2026" not in out


def test_blocks_to_json_includes_anchor_on_non_gap(ns):
    import json as _json
    to_json = ns["_blocks_to_json"]
    b = _build_dummy_block(ns, anchor="recorded")
    data = _json.loads(to_json([b]))
    assert data["blocks"][0]["anchor"] == "recorded"


def test_blocks_to_json_omits_anchor_on_gap(ns):
    import json as _json
    to_json = ns["_blocks_to_json"]
    b_gap = _build_dummy_block(ns, is_gap=True)
    data = _json.loads(to_json([b_gap]))
    assert "anchor" not in data["blocks"][0]


def test_blocks_to_json_heuristic_anchor(ns):
    import json as _json
    to_json = ns["_blocks_to_json"]
    b = _build_dummy_block(ns, anchor="heuristic")
    data = _json.loads(to_json([b]))
    assert data["blocks"][0]["anchor"] == "heuristic"


def test_group_clamps_heuristic_end_to_next_recorded_window(ns):
    """Regression: heuristic block end must not overlap the next recorded window.

    Entries earlier than the next `R - 5h` boundary land in `leftover`.
    Their heuristic +5h span can extend past that boundary unless clamped,
    producing two simultaneously-ACTIVE rows (one heuristic, one recorded).
    """
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    e1 = _entry(ns, dt.datetime(2026, 4, 23, 6, 20, tzinfo=dt.timezone.utc))
    e2 = _entry(ns, dt.datetime(2026, 4, 23, 8, 30, tzinfo=dt.timezone.utc))
    now = dt.datetime(2026, 4, 23, 9, 0, tzinfo=dt.timezone.utc)

    blocks = group([e1, e2], mode="auto", recorded_windows=[R], now=now)

    non_gap = sorted(
        [b for b in blocks if not b.is_gap], key=lambda b: b.start_time
    )
    actives = [b for b in non_gap if b.is_active]
    assert len(actives) == 1, (
        f"Expected exactly one active non-gap block, got {len(actives)}: "
        f"{[(b.start_time, b.end_time, b.anchor, b.is_active) for b in non_gap]}"
    )

    # Heuristic block must be clamped to R - BLOCK_DURATION = 07:00.
    heuristic = [b for b in non_gap if b.anchor == "heuristic"]
    recorded = [b for b in non_gap if b.anchor == "recorded"]
    assert len(heuristic) == 1 and len(recorded) == 1
    assert heuristic[0].end_time == R - BLOCK_DURATION
    assert recorded[0].end_time == R
    assert recorded[0].is_active is True

    # No overlap between the two non-gap blocks.
    assert non_gap[0].end_time <= non_gap[1].start_time


def test_group_no_clamp_when_recorded_windows_empty(ns):
    """No recorded windows → heuristic span stays at start + 5h."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    entry_ts = dt.datetime(2026, 4, 23, 5, 45, tzinfo=dt.timezone.utc)
    now = dt.datetime(2026, 4, 23, 6, 0, tzinfo=dt.timezone.utc)
    blocks = group([_entry(ns, entry_ts)], mode="auto",
                   recorded_windows=None, now=now)
    non_gap = [b for b in blocks if not b.is_gap]
    assert len(non_gap) == 1
    b = non_gap[0]
    assert b.end_time == b.start_time + BLOCK_DURATION


def test_group_clamp_uses_earliest_following_recorded_window(ns):
    """Clamp must use the EARLIEST R whose R-5h follows the block start."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R1 = dt.datetime(2026, 4, 23, 11, 0, tzinfo=dt.timezone.utc)
    R2 = dt.datetime(2026, 4, 23, 16, 0, tzinfo=dt.timezone.utc)
    entry_ts = dt.datetime(2026, 4, 23, 5, 45, tzinfo=dt.timezone.utc)
    now = dt.datetime(2026, 4, 23, 6, 0, tzinfo=dt.timezone.utc)
    blocks = group([_entry(ns, entry_ts)], mode="auto",
                   recorded_windows=[R1, R2], now=now)
    non_gap = [b for b in blocks if not b.is_gap and b.anchor == "heuristic"]
    assert len(non_gap) == 1
    # Heuristic block start = floor(05:45) = 05:00; natural end = 10:00.
    # R1 - 5h = 06:00 is earlier than 10:00, so clamp kicks in → end = 06:00.
    # R2 - 5h = 11:00 is LATER; must not be chosen.
    assert non_gap[0].end_time == R1 - BLOCK_DURATION


def test_group_no_clamp_when_heuristic_end_precedes_next_recorded_start(ns):
    """Heuristic end already <= next R-5h → clamp is a no-op."""
    group = ns["_group_entries_into_blocks"]
    BLOCK_DURATION = ns["BLOCK_DURATION"]
    R = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    entry_ts = dt.datetime(2026, 4, 23, 1, 0, tzinfo=dt.timezone.utc)
    now = dt.datetime(2026, 4, 23, 6, 30, tzinfo=dt.timezone.utc)
    blocks = group([_entry(ns, entry_ts)], mode="auto",
                   recorded_windows=[R], now=now)
    non_gap = [b for b in blocks if not b.is_gap and b.anchor == "heuristic"]
    assert len(non_gap) == 1
    b = non_gap[0]
    # floor(01:00) = 01:00 → natural end 06:00. R - 5h = 07:00 > 06:00, no clamp.
    assert b.end_time == b.start_time + BLOCK_DURATION


def test_floor_to_ten_minutes(ns):
    """_floor_to_ten_minutes drops sub-10-minute precision (floor, not round).

    Anthropic ``rate_limits.5h.resets_at`` arrives with capture-time
    jitter and occasional larger glitches; flooring to a 10-minute grid
    collapses fine-grained noise into shared buckets while leaving
    truly distinct values separable.
    """
    floor = ns["_floor_to_ten_minutes"]
    base = dt.datetime(2026, 4, 23, 8, 10, 0, tzinfo=dt.timezone.utc)

    # On-boundary stays put.
    assert floor(base) == base

    # User-confirmed example: 08:15 → 08:10 (floor, not nearest).
    assert floor(base.replace(minute=15)) == base
    # 08:19:59 still floors down to 08:10.
    assert floor(base.replace(minute=19, second=59)) == base
    # 08:20 advances to the next bucket.
    assert floor(base.replace(minute=20)) == base.replace(minute=20)

    # 09:59:59 floors DOWN to 09:50 (deliberate — overlap dedup handles
    # the case where the real reset is the adjacent 10:00 R).
    probe = dt.datetime(2026, 4, 23, 9, 59, 59, tzinfo=dt.timezone.utc)
    assert floor(probe) == dt.datetime(
        2026, 4, 23, 9, 50, 0, tzinfo=dt.timezone.utc
    )

    # Sub-second / microsecond components are dropped.
    probe = base.replace(second=5, microsecond=123_456)
    assert floor(probe) == base


def test_select_non_overlapping_recorded_windows_drops_low_support_phantom(ns):
    """Phantom R inside an existing 5h window is dropped on row count.

    Reproduces the production scenario from 2026-04-25: the user had
    three real 5h sessions ending at 04:10Z, 10:00Z, 15:00Z. A bogus R
    at 08:30Z (2 supporting rows) was recorded mid-session 2 alongside
    the real 10:00Z (78 rows). The dedup must drop 08:30Z so cmd_blocks
    renders 3 non-overlapping blocks, not 4.
    """
    select = ns["_select_non_overlapping_recorded_windows"]
    items = [
        (dt.datetime(2026, 4, 25, 4, 10, tzinfo=dt.timezone.utc), 31),
        (dt.datetime(2026, 4, 25, 8, 30, tzinfo=dt.timezone.utc), 2),
        (dt.datetime(2026, 4, 25, 10, 0, tzinfo=dt.timezone.utc), 78),
        (dt.datetime(2026, 4, 25, 15, 0, tzinfo=dt.timezone.utc), 18),
    ]
    assert select(items) == [
        dt.datetime(2026, 4, 25, 4, 10, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 25, 10, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 25, 15, 0, tzinfo=dt.timezone.utc),
    ]


def test_select_non_overlapping_recorded_windows_keeps_all_when_clean(ns):
    """No-op when every R is already >= 5h from its neighbors."""
    select = ns["_select_non_overlapping_recorded_windows"]
    items = [
        (dt.datetime(2026, 4, 25, 4, 0, tzinfo=dt.timezone.utc), 5),
        (dt.datetime(2026, 4, 25, 10, 0, tzinfo=dt.timezone.utc), 1),
        (dt.datetime(2026, 4, 25, 15, 0, tzinfo=dt.timezone.utc), 1),
    ]
    assert select(items) == [r for r, _ in items]


def test_select_non_overlapping_recorded_windows_handles_empty(ns):
    select = ns["_select_non_overlapping_recorded_windows"]
    assert select([]) == []


def test_load_recorded_five_hour_windows_collapses_jittery_pairs(
    ns, tmp_path, monkeypatch,
):
    """Two R values 1s apart collapse into a single floored value.

    Reproduces the original production bug where the DB accumulates
    pairs like 23:00:00 and 23:00:01 (Anthropic capture-time jitter),
    causing ``cmd_blocks`` to emit two adjacent blocks instead of one.
    Both raw values floor to 23:00 under the 10-minute grid; the loader
    returns the single deduped datetime.
    """
    # Redirect paths into tmp_path (same pattern as Task 6).
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setitem(ns, "APP_DIR", share)
    monkeypatch.setitem(ns, "DB_PATH", share / "stats.db")
    monkeypatch.setitem(ns, "CACHE_DB_PATH", share / "cache.db")
    monkeypatch.setitem(ns, "CACHE_LOCK_PATH", share / "cache.db.lock")
    monkeypatch.setitem(ns, "CACHE_LOCK_CODEX_PATH", share / "cache.db.codex.lock")
    monkeypatch.setitem(ns, "CONFIG_PATH", share / "config.json")
    monkeypatch.setitem(ns, "LOG_DIR", share / "logs")

    # Seed two jittery R values 1s apart, both inside the query range.
    R_even = "2026-04-23T23:00:00+00:00"
    R_jitter = "2026-04-23T23:00:01+00:00"
    open_db = ns["open_db"]
    with open_db() as conn:
        for captured_at, resets_at in (
            ("2026-04-23T22:30:00Z", R_even),
            ("2026-04-23T22:30:05Z", R_jitter),
        ):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
                " source, payload_json, five_hour_percent, five_hour_resets_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (captured_at, "2026-04-22", "2026-04-29",
                 42.0, "test", "{}", 30.0, resets_at),
            )
        conn.commit()

    load = ns["_load_recorded_five_hour_windows"]
    range_start = dt.datetime(2026, 4, 22, 0, 0, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 4, 24, 0, 0, tzinfo=dt.timezone.utc)
    result = load(range_start, range_end)

    expected = dt.datetime(2026, 4, 23, 23, 0, 0, tzinfo=dt.timezone.utc)
    assert result == [expected], (
        f"Expected single floored value [{expected!r}]; got {result!r}"
    )


def test_load_recorded_five_hour_windows_drops_phantom_in_real_window(
    ns, tmp_path, monkeypatch,
):
    """End-to-end regression for the 2026-04-25 phantom-block bug.

    Seeds the exact production shape: three real R values (each with
    many supporting rows) plus one phantom R captured 1.5h before the
    second real reset (only 2 supporting rows, both within 1s). The
    loader must drop the phantom so cmd_blocks renders 3 blocks for
    the user's 3 actual sessions instead of 4.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setitem(ns, "APP_DIR", share)
    monkeypatch.setitem(ns, "DB_PATH", share / "stats.db")
    monkeypatch.setitem(ns, "CACHE_DB_PATH", share / "cache.db")
    monkeypatch.setitem(ns, "CACHE_LOCK_PATH", share / "cache.db.lock")
    monkeypatch.setitem(ns, "CACHE_LOCK_CODEX_PATH", share / "cache.db.codex.lock")
    monkeypatch.setitem(ns, "CONFIG_PATH", share / "config.json")
    monkeypatch.setitem(ns, "LOG_DIR", share / "logs")

    real_resets = [
        ("2026-04-25T04:10:00+00:00", 31),  # session 1: 02:10-07:10 local
        ("2026-04-25T10:00:00+00:00", 78),  # session 2: 08:00-13:00 local
        ("2026-04-25T15:00:00+00:00", 18),  # session 3: 13:00-now  local
    ]
    phantom_R = "2026-04-25T08:28:41+00:00"  # only 2 supporting rows

    open_db = ns["open_db"]
    with open_db() as conn:
        for resets_at, n_rows in real_resets:
            for i in range(n_rows):
                # Vary captured_at so we don't violate any UNIQUE index;
                # actual values don't matter for the loader logic.
                conn.execute(
                    "INSERT INTO weekly_usage_snapshots "
                    "(captured_at_utc, week_start_date, week_end_date, "
                    " weekly_percent, source, payload_json, "
                    " five_hour_percent, five_hour_resets_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"2026-04-25T00:00:{i:02d}Z",
                        "2026-04-22", "2026-04-29", 50.0, "test",
                        "{}", 25.0, resets_at,
                    ),
                )
        for i in range(2):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " weekly_percent, source, payload_json, "
                " five_hour_percent, five_hour_resets_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"2026-04-25T07:28:4{1+i}Z",
                    "2026-04-22", "2026-04-29", 50.0, "test",
                    "{}", 80.0, phantom_R,
                ),
            )
        conn.commit()

    load = ns["_load_recorded_five_hour_windows"]
    range_start = dt.datetime(2026, 4, 24, 0, 0, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 4, 26, 0, 0, tzinfo=dt.timezone.utc)
    result = load(range_start, range_end)

    expected = [
        dt.datetime(2026, 4, 25, 4, 10, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 25, 10, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 25, 15, 0, tzinfo=dt.timezone.utc),
    ]
    assert result == expected, (
        f"Phantom 08:28 R should be dropped; got {result!r}"
    )
