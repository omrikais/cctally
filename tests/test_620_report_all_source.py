"""#620 S1 D7 / D9 — `report --source all` renders the real report, and a
short week says so.

Two defects, one renderer.

D7: `_render_claude_terminal` reduced every Claude section to
`_legacy_claude_totals`, which reads a `totals` object the report payload
does not have. `report --source all` therefore printed two lines and the
literal `Data available.` — no current-week table, no trend table, no
`$ / 1%` column — while `report` on its own printed the whole thing. The
renderer is extracted as a pure function over the payload and called from
both places.

D9: a trend row whose span is short of the nominal subscription week — an
early reset ended the cycle sooner — was rendered indistinguishably from a
full one, so a `$ / 1%` computed over five days sat beside one computed over
seven with nothing to say they were not comparable.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest


_REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"
_FIXTURES = _REPO / "tests" / "fixtures" / "dashboard"
_REPORT_FIXTURES = _REPO / "tests" / "fixtures" / "report"


def _store(tmp_path, scenario: str):
    app = tmp_path / ".local" / "share" / "cctally"
    app.mkdir(parents=True, exist_ok=True)
    src = _FIXTURES / scenario / ".local" / "share" / "cctally"
    for name in ("stats.db", "cache.db"):
        shutil.copy2(src / name, app / name)
    return tmp_path


def _harness_env(home, *, as_of: str) -> dict:
    """The environment `bin/_lib-fixture-harness.sh` gives a golden run.

    Reproduced rather than approximated, because
    `test_legacy_report_is_byte_identical_on_full_weeks` compares against the
    golden that harness wrote. `COLUMNS` is removed so the renderer takes its
    120-column TTY fallback, which is what the harness leaves it to do.
    """
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    for name in ("CODEX_HOME", "DO_NOT_TRACK", "CCTALLY_DISABLE_TELEMETRY"):
        env.pop(name, None)
    env.update({
        "HOME": str(home),
        "NO_COLOR": "1",
        "TZ": "Etc/UTC",
        "CCTALLY_AS_OF": as_of,
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_DISABLE_RETENTION_SWEEP": "1",
    })
    return env


def _run(home, *args, as_of: str):
    env = _harness_env(home, as_of=as_of)
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, str(_BIN), *args],
        env=env, capture_output=True, text=True, timeout=100,
    )


@pytest.fixture()
def ok_home(tmp_path):
    """The `ok` dashboard fixture: eight prior weeks with both usage and cost
    snapshots, so `report` has a real trend to render."""
    return _store(tmp_path, "ok")


@pytest.fixture()
def short_week_home(tmp_path):
    """`non-monday-anchor` carries a six-day week produced by a reset-day
    shift, which is what the partial-window marker exists to mark."""
    return _store(tmp_path, "non-monday-anchor")


# --- D7 -------------------------------------------------------------------

def test_the_legacy_report_really_renders_a_trend_table(ok_home):
    """Guards the guard: if the single-source report were empty on this
    fixture, the all-source assertion below could not distinguish a renderer
    that works from one that has nothing to render."""
    proc = _run(ok_home, "report", as_of="2026-04-16T14:00:00Z")
    assert proc.returncode == 0, proc.stderr
    assert "$ / 1%" in proc.stdout, proc.stdout
    assert "Trend:" in proc.stdout, proc.stdout


def test_all_source_renders_the_trend_table(ok_home):
    """A7 — the all-source Claude section renders the real report."""
    proc = _run(ok_home, "report", "--source", "all",
                as_of="2026-04-16T14:00:00Z")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "Data available." not in out, out
    assert "$ / 1%" in out, (
        "the all-source Claude section must carry the trend table's "
        f"$ / 1% column:\n{out}"
    )
    assert "Trend:" in out, out


def test_all_source_claude_section_matches_the_single_source_report(ok_home):
    """A7 — the two paths call one renderer, so the all-source Claude section
    contains the single-source report verbatim.

    Substring containment rather than equality, because the all-source form
    adds its own `Claude …` heading and the Codex section after it. What is
    asserted exactly is that every byte the single-source report emits is
    present, in order, inside the Claude section.
    """
    single = _run(ok_home, "report", as_of="2026-04-16T14:00:00Z")
    combined = _run(ok_home, "report", "--source", "all",
                    as_of="2026-04-16T14:00:00Z")
    assert single.returncode == 0, single.stderr
    assert combined.returncode == 0, combined.stderr
    body = single.stdout.strip("\n")
    assert body, single.stdout
    assert body in combined.stdout, (
        "the all-source Claude section diverged from the single-source "
        f"report.\n--- single ---\n{single.stdout}\n--- all ---\n"
        f"{combined.stdout}"
    )


def test_legacy_report_is_byte_identical_on_full_weeks(tmp_path):
    """A7 — `report` without `--source` is unchanged on a full-week fixture.

    The renderer extraction must move code without moving a byte, and the
    partial-window marker must not widen the `#` column of a table that has
    no partial row. The committed `report` golden is a full-week fixture, so
    it is the exact witness: this rebuilds that fixture and compares the
    captured output with the golden byte for byte, the same way
    `bin/cctally-report-test` does.
    """
    out_root = tmp_path / "fixtures"
    build = subprocess.run(
        [sys.executable, str(_REPO / "bin" / "build-report-fixtures.py"),
         "--out", str(out_root)],
        capture_output=True, text=True, timeout=45,
    )
    assert build.returncode == 0, build.stderr

    home = out_root / "base"
    app = home / ".local" / "share" / "cctally"
    # The harness seeds this before every golden run; without it the week
    # windows render without their `UTC` suffix and the compare is not the
    # one the golden was taken under.
    (app / "config.json").write_text(json.dumps({
        "collector": {"host": "127.0.0.1", "port": 17321,
                      "token": "harness", "week_start": "monday"},
        "display": {"tz": "utc"},
    }, indent=2) + "\n")

    input_env = (_REPORT_FIXTURES / "base" / "input.env").read_text()
    as_of = ""
    flags: list[str] = []
    for line in input_env.splitlines():
        key, _, raw = line.partition("=")
        value = raw.strip().strip('"')
        if key == "AS_OF":
            as_of = value
        elif key == "FLAGS":
            flags = value.split()
    assert as_of, input_env

    proc = subprocess.run(
        [sys.executable, str(_BIN), "report", *flags],
        env=_harness_env(home, as_of=as_of),
        capture_output=True, text=True, timeout=45,
    )
    # The harness captures `2>&1` through a command substitution, which
    # strips trailing newlines, and writes the golden with `echo`.
    captured = (proc.stdout + proc.stderr).rstrip("\n") + "\n"
    golden = (_REPORT_FIXTURES / "base" / "golden-terminal.txt").read_text()
    assert captured == golden, (
        "the extracted renderer changed `report`'s bytes on a full-week "
        f"fixture.\n--- captured ---\n{captured}\n--- golden ---\n{golden}"
    )


def _populated_zero_store(tmp_path):
    """A store whose Claude side has ROWS but whose compatible cost and token
    totals are both exactly zero — the state that reaches the fallback.

    An empty store does not reach it: `_claude_result_status` classifies that
    as `empty` and the section renders `No data.` The fallback is guarded by a
    truthiness check on the totals, so it needs a result that is `ok` and
    sums to zero, which is one entry carrying no tokens at all.
    """
    import sqlite3
    home = _store(tmp_path, "no-data")
    cache = home / ".local" / "share" / "cctally" / "cache.db"
    conn = sqlite3.connect(cache)
    try:
        path = "/fake/repos/zero/zero.jsonl"
        conn.execute(
            "INSERT INTO session_files(path, size_bytes, mtime_ns, "
            " last_byte_offset, last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-04-20T00:00:00Z",
             "zerozero-0000-0000-0000-000000000000", "/fake/repos/zero"),
        )
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
            " input_tokens, output_tokens, cache_create_tokens, "
            " cache_read_tokens, cost_usd_raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (path, 0, "2026-04-17T12:00:00+00:00", "claude-sonnet-4-6",
             "msg-zero", "req-zero", 0, 0, 0, 0, 0.0),
        )
        conn.commit()
    finally:
        conn.close()
    return home


def test_populated_zero_result_states_what_is_absent(tmp_path):
    """A7 — `project`, `range-cost` and `cache-report` state what is absent
    and name the command that shows it, instead of `Data available.`

    `diff` is deliberately NOT in this list: Claude `diff` is dispatched
    before `_legacy_claude_totals` is consulted, so even a populated
    exact-zero diff calls `_append_diff_terminal` and can never reach the
    fallback.
    """
    home = _populated_zero_store(tmp_path)
    # A7 enumerates all three, so all three are asserted. The previous form
    # required only that ONE of them reach the replacement statement, which
    # two silently-unchanged commands would have satisfied.
    reached = []
    for argv, as_of in (
        # An explicit range, because `project`'s default window is the
        # current subscription week and this store has no snapshot to anchor
        # one — the Monday fallback lands on 2026-04-20 and excludes the
        # seeded 2026-04-17 entry, so the section reports `empty` and the
        # populated-exact-zero branch is never reached.
        (["project", "--source", "all",
          "--since", "2026-04-13", "--until", "2026-04-21"],
         "2026-04-20T12:00:00Z"),
        (["range-cost", "--source", "all",
          "--start", "2026-04-13", "--end", "2026-04-21"],
         "2026-04-20T12:00:00Z"),
        (["cache-report", "--source", "all"], "2026-04-20T12:00:00Z"),
    ):
        proc = _run(home, *argv, as_of=as_of)
        assert proc.returncode in (0, 3), (argv, proc.stderr, proc.stdout)
        out = proc.stdout
        assert "Data available." not in out, (
            f"{argv} still prints the bare fallback:\n{out}"
        )
        assert "No compatible cost or token total" in out, (
            f"{argv[0]} did not reach the populated-exact-zero branch, so it "
            f"never states what is absent:\n{out}"
        )
        assert f"cctally {argv[0]} --source claude" in out, (
            f"{argv} must name the command that shows the detail:\n{out}"
        )
        reached.append(argv[0])
    assert reached == ["project", "range-cost", "cache-report"], reached


# --- D9 -------------------------------------------------------------------

def _partial_predicate():
    """`load_script()` returns the script's globals dict, and `bin/cctally`
    binds the forecast sibling into it at module level."""
    from conftest import load_script
    return load_script()["_cctally_forecast"]._report_row_is_partial_week


def _span_row(seconds_short: int) -> dict:
    start = dt.datetime(2026, 3, 27, 9, 0, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=7) - dt.timedelta(seconds=seconds_short)
    return {
        "weekStartAt": start.isoformat().replace("+00:00", "Z"),
        "weekEndAt": end.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.parametrize(
    "seconds_short, marked",
    [
        (0, False),        # a full week is never marked
        (3600, False),     # A8: short by EXACTLY one hour is not marked
        (3601, True),      # A8: short by more than one hour is marked
        (86400, True),     # a six-day week
    ],
)
def test_the_partial_week_boundary_is_more_than_one_hour(seconds_short, marked):
    """A8 — the tolerance absorbs the hour-boundary normalisation applied to
    reset jitter, so the predicate turns over strictly beyond one hour.

    The exactly-one-hour case is the whole reason the tolerance exists; a test
    that only checked a six-day week would pass against a predicate with no
    tolerance at all.
    """
    assert _partial_predicate()(_span_row(seconds_short)) is marked


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"weekStartAt": "2026-03-27T09:00:00Z", "weekEndAt": None},
        {"weekStartAt": None, "weekEndAt": "2026-04-02T09:00:00Z"},
        {"weekStartAt": "not-a-timestamp", "weekEndAt": "2026-04-02T09:00:00Z"},
    ],
)
def test_a_row_without_both_bounds_is_not_marked(row):
    """A row whose span cannot be computed asserts nothing, so it is not
    marked. Marking on a guess would claim a fact the data does not carry."""
    assert _partial_predicate()(row) is False


def test_a_short_week_is_marked_and_a_legend_explains_it(short_week_home):
    """A8 — the short row is marked, the full rows are not, and the legend
    renders because at least one row is marked.

    Row identity is asserted exactly: the marker must land on the six-day
    2026-03-27 -> 2026-04-02 row and on no other.
    """
    proc = _run(short_week_home, "report", as_of="2026-04-22T12:00:00Z")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "│ ~4 │ 2026-03-27 09:00 -> 2026-04-02 09:00 │" in out, (
        "the six-day week 2026-03-27 -> 2026-04-02 must carry the marker in "
        f"its `#` cell:\n{out}"
    )
    for index, window in (
        (1, "2026-04-16 09:00 -> 2026-04-23 09:00"),
        (2, "2026-04-09 09:00 -> 2026-04-16 09:00"),
        (3, "2026-04-02 09:00 -> 2026-04-09 09:00"),
    ):
        assert f"│  {index} │ {window} │" in out, (
            f"the full week {window} must render unmarked:\n{out}"
        )
    # The legend is a sentence, not a bare glyph, and it only appears with a
    # marked row present.
    assert "shorter than" in out, out


def test_a_full_week_table_carries_no_legend(ok_home):
    """A8 — a table of full weeks keeps its bytes apart from the widened `#`
    column: no marker, and no legend line."""
    proc = _run(ok_home, "report", as_of="2026-04-16T14:00:00Z")
    assert proc.returncode == 0, proc.stderr
    assert "shorter than" not in proc.stdout, proc.stdout
