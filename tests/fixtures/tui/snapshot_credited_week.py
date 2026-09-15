"""#734 — a credited subscription week, rendered from a real seeded store.

Every other snapshot module here hands the renderer objects it constructed
itself. That is fine for a presentation fixture, but it is the wrong shape for
this one: the thing under test is whether the TUI shows BOTH billing cycles of
an in-place-credited week, and a hand-written pair of `TuiTrendRow` objects
asserts only that the renderer prints two rows it was given. The existing
coverage in `tests/test_weekly_credit_segments.py` and
`tests/test_project_in_place_credit_window.py` already seeds credits in memory
and exercises kernels; reproducing that here would add a third file with the
same blind spot.

So this module seeds an isolated store and assembles the snapshot through
`_tui_build_snapshot`, the same builder the live TUI and the dashboard use. If
`build_trend_view` ever stopped splitting a credited week into its two cycles,
this fixture's golden would change, which is the property #734 asks for.

The store is seeded in a fresh temporary directory, never in the caller's data
directory. `CCTALLY_DATA_DIR` is set before the paths are re-bound because it
outranks `HOME`, and since #769 S4 the generator `bin/build-tui-fixtures.py`
exports a `CCTALLY_DATA_DIR` of its own pointing at one scratch directory
shared by every combination. Setting `HOME` alone here would therefore leave
that inherited value in force and seed this store into the generator's shared
scratch tree rather than into this module's own.
"""
import atexit
import dataclasses
import datetime as dt
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

_UTC = dt.timezone.utc

#: One subscription week, credited once at +72h. `_NOW` sits inside the second
#: cycle so the current-week panel reports that cycle's own reading.
_WEEK_START = dt.datetime(2026, 4, 13, 0, 0, tzinfo=_UTC)
_WEEK_END = _WEEK_START + dt.timedelta(days=7)
_CUT = _WEEK_START + dt.timedelta(hours=72)
_NOW = _WEEK_START + dt.timedelta(hours=100)
_ACCOUNT = "unattributed"
#: Distinct nonzero readings on both sides of the credit: the pre-credit cycle
#: peaks at 41.0 and the post-credit cycle at 30.0.
_LADDER = ((20, 24.0), (50, 41.0), (72, 19.0), (92, 30.0))

_TMP = tempfile.mkdtemp(prefix="cctally-tui-credited-week-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_SHARE = pathlib.Path(_TMP) / ".local" / "share" / "cctally"
_SHARE.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = _TMP
os.environ["CCTALLY_DATA_DIR"] = str(_SHARE)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["TZ"] = "Etc/UTC"

_REPO = pathlib.Path(__file__).resolve().parents[3]
_PATH = _REPO / "bin" / "cctally"
_LOADER = importlib.machinery.SourceFileLoader("_cctally_tui_credited", str(_PATH))
_SPEC = importlib.util.spec_from_loader("_cctally_tui_credited", _LOADER)
m = importlib.util.module_from_spec(_SPEC)
sys.modules["_cctally_tui_credited"] = m
_SPEC.loader.exec_module(m)

# The siblings are shared through `sys.modules`, so the path constants this
# rebinds are the ones the running `cctally tui` process reads.
m._cctally_core._init_paths_from_env()
assert m._cctally_core.APP_DIR == _SHARE, m._cctally_core.APP_DIR

sys.path.insert(0, str(_REPO / "bin"))
import _fixture_builders  # noqa: E402


def _iso(value: dt.datetime) -> str:
    return value.astimezone(_UTC).isoformat(timespec="seconds")


def _seed_stats() -> None:
    conn = m.open_db()
    try:
        for hours_in, percent in _LADDER:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (_iso(_WEEK_START + dt.timedelta(hours=hours_in)),
                 _WEEK_START.date().isoformat(), _WEEK_END.date().isoformat(),
                 _iso(_WEEK_START), _iso(_WEEK_END), percent, "statusline",
                 "{}", _ACCOUNT),
            )
        # Exactly one credit. `old_week_end_at` equals `effective_reset_at_utc`,
        # which is what marks an IN-PLACE credit rather than a re-anchoring
        # reset, and `new_week_end_at` is the week's own unchanged boundary.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key) "
            "VALUES (?,?,?,?,?,?)",
            (_iso(_CUT), _iso(_CUT), _iso(_WEEK_END), _iso(_CUT), 41.0,
             _ACCOUNT),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_cache() -> None:
    cache_path = m._cctally_core.CACHE_DB_PATH
    _fixture_builders.create_cache_db(cache_path)
    conn = sqlite3.connect(cache_path)
    try:
        # One session per cycle, in different projects and with different token
        # counts, so the two cycles carry distinct nonzero costs. A credited
        # week's cost is live-computed from `session_entries` rather than read
        # from `weekly_cost_snapshots`, so this is where the dollars come from.
        for index, (offset_hours, tokens, project) in enumerate((
            (24, 150_000, "/fake/repos/alpha"),
            (80, 240_000, "/fake/repos/beta"),
        )):
            path = f"/fake/jsonl/credited-week-{index}.jsonl"
            at = _WEEK_START + dt.timedelta(hours=offset_hours)
            conn.execute(
                "INSERT OR IGNORE INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?, 0, 0, 0, ?, ?, ?)",
                (path, _iso(_NOW), f"credited-week-s{index}", project),
            )
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens) VALUES (?, 0, ?, ?, ?, ?, 0, 0)",
                (path, _iso(at), "claude-opus-4-7", tokens, 20_000),
            )
        conn.commit()
    finally:
        conn.close()


_seed_stats()
_seed_cache()

_SNAP = m._cctally_tui._tui_build_snapshot(now_utc=_NOW, skip_sync=True)

#: The property the fixture exists for, asserted BEFORE anything is rendered.
#: A golden that happened to show one row would otherwise be accepted as the
#: expected output of a broken builder.
_CYCLE_STARTS = [row.week_start_at for row in _SNAP.trend]
assert len(_CYCLE_STARTS) == 2, _CYCLE_STARTS
assert len(set(_CYCLE_STARTS)) == 2, _CYCLE_STARTS
assert _CYCLE_STARTS == [_WEEK_START, _CUT], _CYCLE_STARTS
assert [row.used_pct for row in _SNAP.trend] == [41.0, 30.0], (
    [row.used_pct for row in _SNAP.trend])
assert all(row.dollars_per_percent for row in _SNAP.trend), (
    "both cycles must carry a nonzero cost, or the rendered $/1% says nothing")

# `last_sync_at=None` gives the header a deterministic "synced -", and
# `generated_at` is pinned for the same reason.
SNAPSHOT = dataclasses.replace(_SNAP, last_sync_at=None, generated_at=_NOW)
