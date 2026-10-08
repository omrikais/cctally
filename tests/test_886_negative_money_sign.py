"""#886 S1 — a negative milestone amount puts its sign outside the `$`.

The CLI convention (`_lib_diff_kernel`, `_lib_share`, source analytics) is an
ASCII hyphen before the currency sign: `-$71.21`, never `$-71.21`. Negative
marginals are legitimate after a rederive (#875 spec §7.2–§7.3), so every
human milestone table must follow it.

Test kinds (spec §5):

* RED — an existing renderer printed the `$-` form on base `6bca5edc2`; the
  expectation is the captured base line with only that substring swapped, so
  the oracle is exact and also proves the column did not move.
* characterization — an output that must not change; the expectation is the
  byte string captured from the base code.
* unit — the new `_fmt_usd_accounting` helper, which does not exist on base.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import load_script, redirect_paths

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"

UTC = dt.timezone.utc

# One subscription week holding the weekly population, and one 5h block inside
# it holding the five-hour population.
WEEK_START_DATE = "2026-09-01"
WEEK_END_DATE = "2026-09-08"
WEEK_START_AT = "2026-09-01T00:00:00+00:00"
WEEK_END_AT = "2026-09-08T00:00:00+00:00"
BLOCK_START_ISO = "2026-09-05T01:00:00+00:00"
BLOCK_RESETS_ISO = "2026-09-05T06:00:00+00:00"
WEEKLY_FIRST_AT = "2026-09-02T10:00:00Z"
FIVE_HOUR_FIRST_AT = "2026-09-05T01:42:00Z"
NEGATIVE_AT = "2026-09-05T02:01:00Z"
# The dashboard envelope is built inside the block, so the block is current.
ENVELOPE_NOW = dt.datetime(2026, 9, 5, 3, 0, tzinfo=UTC)

NEG_6 = ("$-71.207516", "-$71.207516")   # five-hour-breakdown marginal
NEG_W6 = ("$-71.210000", "-$71.210000")  # weekly .6f marginal
NEG_2 = ("$-71.21", "-$71.21")           # TUI .2f marginal


def _expected_after(base_line: str, swap: "tuple[str, str]") -> str:
    assert base_line.count(swap[0]) == 1, base_line
    return base_line.replace(*swap)


# ── the shared negative store ─────────────────────────────────────────


def _seed_home_negative(ns) -> None:
    """Seed BOTH negative populations into the redirected stats.db.

    Weekly: percent 1 (cumulative 400.00, marginal 400.00), then percent 2
    (cumulative 328.79, marginal −71.21). Five-hour: Block $ 395.05 with no
    marginal, then 323.84 with marginal −71.207516. Every milestone joins its
    own usage snapshot, so the `7d at crossing` / `5h at crossing` columns are
    populated, and no two weekly rows share a snapshot, so no observation-gap
    run is classified.
    """
    conn = ns["open_db"]()
    try:
        key = ns["_canonical_5h_window_key"](
            int(dt.datetime.fromisoformat(BLOCK_RESETS_ISO).timestamp())
        )
        for snap_id, captured, weekly, five_hour, window_key in [
            (1, WEEKLY_FIRST_AT, 1.0, 12.0, None),
            (2, FIVE_HOUR_FIRST_AT, 1.5, 15.0, key),
            (3, NEGATIVE_AT, 2.0, 16.0, key),
        ]:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(id, captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json, five_hour_percent, five_hour_resets_at, "
                " five_hour_window_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'test', '{}', ?, ?, ?)",
                (snap_id, captured, WEEK_START_DATE, WEEK_END_DATE,
                 WEEK_START_AT, WEEK_END_AT, weekly, five_hour,
                 BLOCK_RESETS_ISO if window_key is not None else None,
                 window_key),
            )
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(id, captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, cost_usd, mode) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, 'auto')",
            (NEGATIVE_AT, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
             WEEK_END_AT, 328.79),
        )
        for threshold, captured, cumulative, marginal, snap_id, five_hour in [
            (1, WEEKLY_FIRST_AT, 400.0, 400.0, 1, 12.0),
            (2, NEGATIVE_AT, 328.79, -71.21, 3, 16.0),
        ]:
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, usage_snapshot_id, "
                " cost_snapshot_id, five_hour_percent_at_crossing, "
                " reset_event_id, account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 'unattributed')",
                (captured, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
                 WEEK_END_AT, threshold, cumulative, marginal, snap_id,
                 five_hour),
            )
        conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent, total_cost_usd,
                seven_day_pct_at_block_start, seven_day_pct_at_block_end,
                crossed_seven_day_reset, is_closed,
                created_at_utc, last_updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, BLOCK_RESETS_ISO, BLOCK_START_ISO, BLOCK_START_ISO,
             NEGATIVE_AT, 16.0, 323.84, 1.0, 2.0, 0, 0, BLOCK_START_ISO,
             NEGATIVE_AT),
        )
        block_id = conn.execute(
            "SELECT last_insert_rowid() AS id").fetchone()["id"]
        for threshold, cumulative, marginal, seven_day, captured, snap_id in [
            (15, 395.05, None, 1.5, FIVE_HOUR_FIRST_AT, 2),
            (16, 323.84, -71.207516, 2.0, NEGATIVE_AT, 3),
        ]:
            conn.execute(
                """
                INSERT INTO five_hour_milestones (
                    block_id, five_hour_window_key, percent_threshold,
                    captured_at_utc, usage_snapshot_id,
                    block_cost_usd, marginal_cost_usd, seven_day_pct_at_crossing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (block_id, key, threshold, captured, snap_id, cumulative,
                 marginal, seven_day),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def home_negative(tmp_path, monkeypatch):
    """The seeded store: ``.home`` for subprocesses, ``.ns`` for in-process
    reads (the same namespace the store was redirected through)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_home_negative(ns)
    return types.SimpleNamespace(home=tmp_path, ns=ns)


def _env(home: Path) -> dict:
    return {
        "HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1", "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _run(home: Path, *args: str) -> bytes:
    """The raw stdout bytes of one `cctally` invocation over the store."""
    res = subprocess.run(
        [sys.executable, str(BIN), *args], capture_output=True, env=_env(home),
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return res.stdout


def _table_lines(text: str) -> "tuple[str, str, str]":
    """The header, positive (row 1) and negative (row 2) lines of the FIRST
    boxed table in ``text``."""
    lines = text.splitlines()
    header = next(line for line in lines
                  if line.startswith("│") and "Threshold" in line)
    start = lines.index(header)
    positive = next(line for line in lines[start:] if line.startswith("│ 1 │"))
    negative = next(line for line in lines[start:] if line.startswith("│ 2 │"))
    return header, positive, negative


def _separator_offsets(line: str) -> "list[int]":
    return [i for i, ch in enumerate(line) if ch == "│"]


def _envelope_bytes(ns, monkeypatch):
    """The dashboard envelope over the store, built the way `/api/data` builds
    it, and its bytes through the dashboard's own encoder.

    Returns ``(env, whole, milestones, five_hour_milestones)``: the decoded
    envelope, the whole `/api/data` body, and each milestone array encoded by
    the same encoder with the same arguments.
    """
    monkeypatch.setitem(ns, "_load_update_state", lambda: None)
    monkeypatch.setitem(ns, "_load_update_suppress", lambda: {
        "skipped_versions": [], "remind_after": None,
    })
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})

    def _no_doctor(**_kw):
        raise RuntimeError("pinned: doctor disabled for envelope test")

    monkeypatch.setitem(ns, "doctor_gather_state", _no_doctor)
    snap = ns["_tui_build_snapshot"](now_utc=ENVELOPE_NOW, skip_sync=True)
    env = ns["snapshot_to_envelope"](
        snap, now_utc=ENVELOPE_NOW, monotonic_now=None)
    import _lib_dashboard_json

    encode = _lib_dashboard_json.encode_dashboard_json_bytes
    cw = env["current_week"]
    return (
        env,
        encode(env, ensure_ascii=False),
        encode(cw["milestones"], ensure_ascii=False),
        encode(cw["five_hour_milestones"], ensure_ascii=False),
    )


def _tui_lines(width: int, milestones) -> "list[str]":
    """The rendered lines of the TUI current-week per-percent modal."""
    load_script()
    import _cctally_tui as tui

    cw = tui.TuiCurrentWeek(
        week_start_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        week_end_at=dt.datetime(2026, 9, 8, tzinfo=UTC),
        used_pct=2.0, five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=328.79, dollars_per_percent=164.40,
        latest_snapshot_at=dt.datetime(2026, 9, 5, 2, 1, tzinfo=UTC),
    )
    snap = types.SimpleNamespace(
        current_week=cw,
        percent_milestones=[
            tui.TuiPercentMilestone(
                percent=percent,
                crossed_at=crossed,
                cumulative_cost_usd=cumulative,
                marginal_cost_usd=marginal,
                five_hour_pct_at_crossing=five_hour,
            )
            for percent, crossed, cumulative, marginal, five_hour in milestones
        ],
    )
    runtime = types.SimpleNamespace(
        display_tz=ZoneInfo("Etc/UTC"), focus_index=0,
        modal_snap_pending=False, modal_scroll=0,
    )
    _title, lines = tui._tui_modal_current_week(snap, runtime, width)
    return lines


TUI_MILESTONES = [
    (1, dt.datetime(2026, 9, 2, 10, 0, tzinfo=UTC), 400.0, 400.0, 12.0),
    (2, dt.datetime(2026, 9, 5, 2, 1, tzinfo=UTC), 328.79, -71.21, 16.0),
]
TUI_WIDE = 120
TUI_NARROW = 90


def _tui_row(lines: "list[str]", percent: int) -> str:
    marker = "{b}" + f"{percent:>3}" + "{/}"
    (row,) = [line for line in lines if marker in line]
    return row


# ── base captures (base `6bca5edc2`, captured on the remote runner) ───
#
# Each constant is the exact output of the unmodified code over the store
# above (or over `TUI_MILESTONES`). A RED test derives its expectation from
# one of these with `_expected_after`; a characterization test compares
# against it unchanged.

BASE_FHB_HEADER = (
    "│ # │ Threshold │ Cumulative Cost │ Marginal Cost │ 7d at crossing │")
BASE_FHB_POS_ROW = (
    "│ 1 │       15% │     $395.050000 │           n/a │             2% │")
BASE_FHB_NEG_ROW = (
    "│ 2 │       16% │     $323.840000 │   $-71.207516 │             2% │")

BASE_PB_HEADER = (
    "│ # │ Threshold │ Cumulative Cost │ Marginal Cost │ 5h at crossing │")
BASE_PB_POS_ROW = (
    "│ 1 │        1% │     $400.000000 │   $400.000000 │            12% │")
BASE_PB_NEG_ROW = (
    "│ 2 │        2% │     $328.790000 │   $-71.210000 │            16% │")

BASE_RD_HEADER = "│ # │ Threshold │ Cumulative Cost │ Marginal Cost │"
BASE_RD_POS_ROW = "│ 1 │        1% │     $400.000000 │   $400.000000 │"
BASE_RD_NEG_ROW = "│ 2 │        2% │     $328.790000 │   $-71.210000 │"

BASE_TUI_POS_WIDE = (
    "   {b}  1{/} {bright}Sep 02 10:00:00 UTC   {/} {b}$400.00 {/} "
    "{b}$400.00   {/} {dim}12%  {/}")
BASE_TUI_NEG_WIDE = (
    "   {b}  2{/} {bright}Sep 05 02:01:00 UTC   {/} {b}$328.79 {/} "
    "{b}$-71.21   {/} {dim}16%  {/}")
BASE_TUI_POS_NARROW = (
    "   {b}  1{/} {bright}Sep 02 10:00:00 UTC   {/} {b}$400.00 {/} "
    "{b}$400.00   {/}")
BASE_TUI_NEG_NARROW = (
    "   {b}  2{/} {bright}Sep 05 02:01:00 UTC   {/} {b}$328.79 {/} "
    "{b}$-71.21   {/}")

BASE_FHB_JSON = """\
{
  "schemaVersion": 1,
  "block": {
    "blockStartAt": "2026-09-05T01:00:00+00:00",
    "fiveHourWindowKey": 1788588000,
    "fiveHourResetsAt": "2026-09-05T06:00:00+00:00",
    "lastObservedAtUtc": "2026-09-05T02:01:00Z",
    "status": "closed",
    "finalFiveHourPercent": 16.0,
    "totalCost": 323.84,
    "dollarsPerPercent": 20.24,
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheCreationTokens": 0,
    "cacheReadTokens": 0,
    "sevenDayPctAtBlockStart": 1.0,
    "sevenDayPctAtBlockEnd": 2.0,
    "sevenDayPctDeltaPp": 1.0,
    "crossedSevenDayReset": false
  },
  "milestones": [
    {
      "percentThreshold": 15,
      "capturedAt": "2026-09-05T01:42:00Z",
      "blockCostUSD": 395.05,
      "marginalCostUSD": null,
      "sevenDayPctAtCrossing": 1.5,
      "effectiveSevenDayPctAtCrossing": 1.5,
      "resetEventId": 0
    },
    {
      "percentThreshold": 16,
      "capturedAt": "2026-09-05T02:01:00Z",
      "blockCostUSD": 323.84,
      "marginalCostUSD": -71.207516,
      "sevenDayPctAtCrossing": 2.0,
      "effectiveSevenDayPctAtCrossing": 2.0,
      "resetEventId": 0
    }
  ],
  "credits": []
}
"""

# `generatedAt` is the one wall-clock field; the captured value was replaced by
# the placeholder `_GENERATED_AT_RE` substitutes into the live output.
BASE_PB_JSON = """\
{
  "schemaVersion": 1,
  "weekStartDate": "2026-09-01",
  "weekEndDate": "2026-09-08",
  "weekStartAt": "2026-09-01T00:00:00+00:00",
  "weekEndAt": "2026-09-08T00:00:00+00:00",
  "milestones": [
    {
      "percentThreshold": 1,
      "cumulativeCostUSD": 400.0,
      "marginalCostUSD": 400.0,
      "capturedAt": "2026-09-02T10:00:00Z",
      "fiveHourPercentAtCrossing": 12.0
    },
    {
      "percentThreshold": 2,
      "cumulativeCostUSD": 328.79,
      "marginalCostUSD": -71.21,
      "capturedAt": "2026-09-05T02:01:00Z",
      "fiveHourPercentAtCrossing": 16.0
    }
  ],
  "generatedAt": "<generated>"
}
"""
_GENERATED_AT_RE = re.compile(rb'^(  "generatedAt": )"[^"]*"$', re.M)

BASE_ENV_MILESTONES = (
    '[{"percent": 1, "crossed_at_utc": "2026-09-02T10:00:00Z", '
    '"cumulative_usd": 400.0, "marginal_usd": 400.0, '
    '"five_hour_pct_at_cross": 12.0}, '
    '{"percent": 2, "crossed_at_utc": "2026-09-05T02:01:00Z", '
    '"cumulative_usd": 328.79, "marginal_usd": -71.21, '
    '"five_hour_pct_at_cross": 16.0}]'
)
BASE_ENV_FIVE_HOUR_MILESTONES = (
    '[{"percent_threshold": 15, "captured_at_utc": "2026-09-05T01:42:00Z", '
    '"block_cost_usd": 395.05, "marginal_cost_usd": null, '
    '"seven_day_pct_at_crossing": 1.5, '
    '"effective_seven_day_pct_at_crossing": 1.5, "reset_event_id": 0}, '
    '{"percent_threshold": 16, "captured_at_utc": "2026-09-05T02:01:00Z", '
    '"block_cost_usd": 323.84, "marginal_cost_usd": -71.207516, '
    '"seven_day_pct_at_crossing": 2.0, '
    '"effective_seven_day_pct_at_crossing": 2.0, "reset_event_id": 0}]'
)


# ── R4: `five-hour-breakdown` ─────────────────────────────────────────


def test_five_hour_breakdown_negative_row_exact(home_negative):
    """RED (R4). The negative crossing's whole row, in an unchanged column."""
    out = _run(home_negative.home, "five-hour-breakdown").decode("utf-8")
    header, _positive, negative = _table_lines(out)
    assert negative == _expected_after(BASE_FHB_NEG_ROW, NEG_6)
    assert _separator_offsets(negative) == _separator_offsets(header)
    assert "$-" not in out


def test_five_hour_breakdown_header_and_positive_rows_unchanged(home_negative):
    """Characterization (R4)."""
    out = _run(home_negative.home, "five-hour-breakdown").decode("utf-8")
    header, positive, _negative = _table_lines(out)
    assert header == BASE_FHB_HEADER
    assert positive == BASE_FHB_POS_ROW


# ── R5: `percent-breakdown` ───────────────────────────────────────────


def test_percent_breakdown_negative_row_exact(home_negative):
    """RED (R5)."""
    out = _run(home_negative.home, "percent-breakdown").decode("utf-8")
    header, _positive, negative = _table_lines(out)
    assert negative == _expected_after(BASE_PB_NEG_ROW, NEG_W6)
    assert _separator_offsets(negative) == _separator_offsets(header)
    assert "$-" not in out


def test_percent_breakdown_header_and_positive_rows_unchanged(home_negative):
    """Characterization (R5)."""
    out = _run(home_negative.home, "percent-breakdown").decode("utf-8")
    header, positive, _negative = _table_lines(out)
    assert header == BASE_PB_HEADER
    assert positive == BASE_PB_POS_ROW


# ── R6: `report --detail` ─────────────────────────────────────────────


def _report_detail_table(home: Path) -> str:
    out = _run(home, "report", "--detail").decode("utf-8")
    return out[out.index("Percent breakdown (current week):"):]


def test_report_detail_negative_row_exact(home_negative):
    """RED (R6). The table `report --detail` builds for itself."""
    detail = _report_detail_table(home_negative.home)
    header, _positive, negative = _table_lines(detail)
    assert negative == _expected_after(BASE_RD_NEG_ROW, NEG_W6)
    assert _separator_offsets(negative) == _separator_offsets(header)
    assert "$-" not in detail


def test_report_detail_header_and_positive_rows_unchanged(home_negative):
    """Characterization (R6)."""
    detail = _report_detail_table(home_negative.home)
    header, positive, _negative = _table_lines(detail)
    assert header == BASE_RD_HEADER
    assert positive == BASE_RD_POS_ROW


# ── R7: the TUI current-week modal ────────────────────────────────────


def test_tui_negative_line_exact_wide():
    """RED (R7). The 5-hr column is present and populated, and starts at the
    same offset as on the positive row."""
    load_script()
    import _cctally_tui as tui

    assert tui._tui_width_bucket(TUI_WIDE) != "narrow"
    lines = _tui_lines(TUI_WIDE, TUI_MILESTONES)
    negative = _tui_row(lines, 2)
    assert negative == _expected_after(BASE_TUI_NEG_WIDE, NEG_2)
    assert negative.endswith("{dim}16%  {/}")
    assert negative.index("{dim}") == _tui_row(lines, 1).index("{dim}")


def test_tui_negative_line_exact_narrow():
    """RED (R7). The 5-hr column is absent at a narrow width."""
    load_script()
    import _cctally_tui as tui

    assert tui._tui_width_bucket(TUI_NARROW) == "narrow"
    negative = _tui_row(_tui_lines(TUI_NARROW, TUI_MILESTONES), 2)
    assert negative == _expected_after(BASE_TUI_NEG_NARROW, NEG_2)
    assert "{dim}" not in negative


def test_tui_positive_lines_unchanged():
    """Characterization (R7)."""
    assert _tui_row(_tui_lines(TUI_WIDE, TUI_MILESTONES), 1) == BASE_TUI_POS_WIDE
    assert (_tui_row(_tui_lines(TUI_NARROW, TUI_MILESTONES), 1)
            == BASE_TUI_POS_NARROW)


# ── R8: the sign rule and zero behaviour, through existing renderers ──

SIGN_RULE_CASES = [
    # (value, `.6f` cell, `.2f` cell) — the first four are RED (base printed
    # the `$-` form, `$-0.000000` / `$-0.00` for the zero cases); the last two
    # are characterization.
    (-71.207516, "-$71.207516", "-$71.21"),
    (-0.0, "$0.000000", "$0.00"),
    (-1e-7, "-$0.000000", "-$0.00"),
    (-0.004, "-$0.004000", "-$0.00"),
    (0.0, "$0.000000", "$0.00"),
    (6.63, "$6.630000", "$6.63"),
]


@pytest.mark.parametrize(
    "value, expected",
    [(value, six) for value, six, _two in SIGN_RULE_CASES],
)
def test_marginal_cell_sign_rule(value, expected):
    """R8 through `observation_gap_marginal_cell` (`.6f`)."""
    ns = load_script()
    assert ns["observation_gap_marginal_cell"](value, withheld=False) == expected


def test_marginal_cell_markers_unchanged():
    """Characterization (R5, I3): the absent and withheld markers."""
    ns = load_script()
    cell = ns["observation_gap_marginal_cell"]
    assert cell(None, withheld=True) == "observation_gap"
    assert cell(None, withheld=False) == "n/a"


@pytest.mark.parametrize(
    "value, expected",
    [(value, two) for value, _six, two in SIGN_RULE_CASES],
)
def test_tui_marginal_sign_rule(value, expected):
    """R8 through the TUI Marginal field (`.2f`, padded to 10)."""
    lines = _tui_lines(TUI_WIDE, [
        (1, dt.datetime(2026, 9, 2, 10, 0, tzinfo=UTC), 400.0, value, 12.0),
    ])
    fields = re.findall(r"\{b\}(.*?)\{/\}", _tui_row(lines, 1))
    assert len(fields) == 3, fields  # percent, Cumul, Marginal
    assert fields[2] == expected.ljust(10)


# ── R11: machine-readable outputs are byte-identical ──────────────────


def test_json_outputs_byte_identical(home_negative):
    """Characterization (R11a). Raw stdout bytes, not decoded values."""
    home = home_negative.home
    fhb = _run(home, "five-hour-breakdown", "--json")
    assert fhb == BASE_FHB_JSON.encode("utf-8")

    pb, substituted = _GENERATED_AT_RE.subn(
        rb'\1"<generated>"', _run(home, "percent-breakdown", "--json"),
        count=1)
    assert substituted == 1
    assert pb == BASE_PB_JSON.encode("utf-8")

    fhb_ms = json.loads(fhb)["milestones"]
    assert fhb_ms[0]["marginalCostUSD"] is None
    negative = fhb_ms[1]["marginalCostUSD"]
    assert isinstance(negative, (int, float)) and negative < 0
    pb_negative = json.loads(pb)["milestones"][1]["marginalCostUSD"]
    assert isinstance(pb_negative, (int, float)) and pb_negative < 0


def test_envelope_milestone_bytes_identical(home_negative, monkeypatch):
    """Characterization (R11b). The envelope's milestone arrays, serialized by
    the dashboard's own encoder, are the base bytes, and they are the exact
    bytes the whole `/api/data` body carries."""
    env, whole, milestones, five_hour = _envelope_bytes(
        home_negative.ns, monkeypatch)
    assert milestones == BASE_ENV_MILESTONES.encode("utf-8")
    assert five_hour == BASE_ENV_FIVE_HOUR_MILESTONES.encode("utf-8")
    assert b'"milestones": ' + milestones in whole
    assert b'"five_hour_milestones": ' + five_hour in whole

    cw = env["current_week"]
    weekly_negative = cw["milestones"][1]["marginal_usd"]
    assert isinstance(weekly_negative, (int, float)) and weekly_negative < 0
    assert cw["five_hour_milestones"][0]["marginal_cost_usd"] is None
    five_hour_negative = cw["five_hour_milestones"][1]["marginal_cost_usd"]
    assert isinstance(five_hour_negative, (int, float))
    assert five_hour_negative < 0


# ── unit: the shared CLI/TUI helper (absent on base) ──────────────────


@pytest.mark.parametrize("value, places, expected", [
    (value, 6, six) for value, six, _two in SIGN_RULE_CASES
] + [
    (value, 2, two) for value, _six, two in SIGN_RULE_CASES
])
def test_fmt_usd_accounting_unit(value, places, expected):
    """Unit (R8). `_fmt_usd_accounting` over the six sign-rule inputs at both
    precisions, reached on the `cctally` namespace the renderers use."""
    ns = load_script()
    assert ns["_fmt_usd_accounting"](value, places) == expected
    import _lib_fmt

    assert _lib_fmt._fmt_usd_accounting(value, places) == expected
