"""#929 S1 A9: Claude Haiku 5.5 is priced end to end through the real CLI.

A production-shaped Claude JSONL with three Haiku 5.5 requests is ingested by
`cache-sync`, then read back through `daily --json`, `session --json`,
`cache-report --json` and `cache-report --by-session --json` as subprocesses
against a temporary data dir. Expected values are independent vendor
arithmetic:

  short   50,000 in / 1,000 out                       -> base   $0.0055
  long    40,001 in / 40,000 write (1h 20,000) /
          20,000 read / 1,000 out (prompt 100,001)    -> higher $0.0560005
  edge    60,000 in / 40,000 5m write (prompt 100,000) -> base   $0.011

On the pre-#929 tree every one of them is an unknown model priced at $0.

`daily` and `session` read every retained entry when given no window, so they
do not depend on the wall clock. `cache-report` defaults to a trailing
`--days 7` window anchored at "now", so its calls pin an explicit full-ISO
window around the fixed fixture day; without it the test silently stops
seeing the fixture a week after 2026-10-07.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"
HAIKU = "claude-haiku-5-5"
SESSION_ID = "92900000-0000-4000-8000-000000000001"

SHORT = 50_000 * 1e-07 + 1_000 * 5e-07
LONG = (40_001 * 5e-07 + 1_000 * 2.5e-06 + 20_000 * 6.25e-07
        + 20_000 * (5e-07 * 2.0) + 20_000 * 5e-08)
EDGE = 60_000 * 1e-07 + 40_000 * 1.25e-07
TOTAL = SHORT + LONG + EDGE

# The fixture day as an explicit full-ISO window (equal date-only bounds would
# be an empty window by design).
REPORT_WINDOW = ["--since", "2026-10-07T00:00:00Z",
                 "--until", "2026-10-08T00:00:00Z"]


def _assistant(msg, req, minute, *, inp, out, cc, h, cr):
    return {
        "type": "assistant",
        "timestamp": f"2026-10-07T10:{minute:02d}:00Z",
        "requestId": req,
        "sessionId": SESSION_ID,
        "cwd": "/fixture",
        "message": {
            "id": msg,
            "model": HAIKU,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cc - h,
                    "ephemeral_1h_input_tokens": h,
                },
            },
        },
    }


def _write_corpus(home):
    projects = home / ".claude" / "projects" / "-fixture"
    projects.mkdir(parents=True)
    rows = [
        _assistant("m-short", "r-short", 0, inp=50_000, out=1_000, cc=0,
                   h=0, cr=0),
        _assistant("m-long", "r-long", 1, inp=40_001, out=1_000, cc=40_000,
                   h=20_000, cr=20_000),
        _assistant("m-edge", "r-edge", 2, inp=60_000, out=0, cc=40_000, h=0,
                   cr=0),
    ]
    (projects / f"{SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")


def _run(argv, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env["TZ"] = "Etc/UTC"
    env.pop("CLAUDE_CONFIG_DIR", None)
    return subprocess.run([sys.executable, str(_BIN), *argv],
                          capture_output=True, text=True, env=env)


def test_haiku_55_is_priced_through_ingest_and_reports(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_corpus(home)

    sync = _run(["cache-sync"], home)
    assert sync.returncode == 0, sync.stderr
    stderr = sync.stderr

    daily = _run(["daily", "--json"], home)
    assert daily.returncode == 0, daily.stderr
    stderr += daily.stderr
    payload = json.loads(daily.stdout)
    assert abs(payload["totals"]["totalCost"] - TOTAL) < 1e-9, payload["totals"]
    assert abs(TOTAL - 0.0725005) < 1e-12

    session = _run(["session", "--json"], home)
    assert session.returncode == 0, session.stderr
    stderr += session.stderr
    sessions = json.loads(session.stdout)["sessions"]
    assert [s["sessionId"] for s in sessions] == [SESSION_ID]
    assert abs(sessions[0]["totalCost"] - TOTAL) < 1e-9, sessions[0]

    # cache-report selects the card from the full prompt too: only the long
    # request reads (saved) and its writes carry the higher premium; the edge
    # request's 5m write stays on the base card.
    report = _run(["cache-report", "--json", *REPORT_WINDOW], home)
    assert report.returncode == 0, report.stderr
    stderr += report.stderr
    totals = json.loads(report.stdout)["totals"]
    saved = 20_000 * (5e-07 - 5e-08)
    wasted = (20_000 * (1e-06 - 5e-07) + 20_000 * (6.25e-07 - 5e-07)
              + 40_000 * (1.25e-07 - 1e-07))
    assert totals is not None, report.stdout
    assert abs(totals["savedUsd"] - saved) < 1e-9, totals
    assert abs(totals["wastedUsd"] - wasted) < 1e-9, totals
    assert abs(totals["netUsd"] - (saved - wasted)) < 1e-9, totals

    # The by-session path reads the same store through its own aggregator and
    # must select the same long/short cards per request.
    by_session = _run(["cache-report", "--by-session", "--json",
                       *REPORT_WINDOW], home)
    assert by_session.returncode == 0, by_session.stderr
    stderr += by_session.stderr
    rows = json.loads(by_session.stdout)["sessions"]
    assert [r["sessionId"] for r in rows] == [SESSION_ID], rows
    row = rows[0]
    assert abs(row["savedUsd"] - saved) < 1e-9, row
    assert abs(row["wastedUsd"] - wasted) < 1e-9, row
    assert abs(row["netUsd"] - (saved - wasted)) < 1e-9, row

    assert "unknown model" not in stderr
