"""#620 S1 D10 / D13 (CLI half) — confidence statements name their real cause,
and the CLI `project` cost share names its denominator.

D10 has two halves. `budget` hardcoded `(LOW CONF — early in week)` on a
predicate that is a disjunction: the period is barely elapsed OR nothing has
been spent. On a fully elapsed period with zero spend the second disjunct
fires and the string makes a false claim about the first. It becomes
cause-neutral rather than gaining a cause enum, because a cause enum is new
vocabulary and belongs to S2.

`forecast` printed its four machine reason codes verbatim —
`(elapsed_hours<24, percent<2)` — into a line a person reads. Each code maps
to human wording, with a fallback so an unrecognised code is surfaced rather
than dropped. The wire codes are unchanged, so `--json` consumers see no
difference.

D13's CLI half: `project` renders a cost-share column, and the terminal has no
hover, so the denominator is stated as visible text.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from conftest import load_script


_REPO = pathlib.Path(__file__).resolve().parent.parent
_BIN = _REPO / "bin" / "cctally"


def _forecast_module():
    return load_script()["_cctally_forecast"]


# --- D10, budget --------------------------------------------------------

def _budget_lines(*, spent_usd: float, elapsed_fraction: float):
    """Render one budget block for a period at the given elapsed fraction.

    Built through `compute_budget_status` rather than a hand-made
    `BudgetStatus`, so the rendered string is tied to the real predicate — a
    hand-made status could assert `low_confidence=True` on a period the
    predicate would not call low-confidence at all.
    """
    ns = load_script()
    fc = ns["_cctally_forecast"]
    budget = ns["_lib_budget"]
    start = dt.datetime(2026, 4, 13, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=7)
    now = start + dt.timedelta(days=7 * elapsed_fraction)
    inputs = budget.BudgetInputs(
        target_usd=200.0,
        spent_usd=spent_usd,
        recent_24h_usd=spent_usd / 7.0,
        week_start_at=start,
        week_end_at=end,
        now=now,
        alert_thresholds=(80, 100),
    )
    status = budget.compute_budget_status(inputs)
    assert status.low_confidence, (
        "this fixture must be low-confidence or the test asserts nothing "
        f"about the confidence string (spent={spent_usd}, "
        f"elapsed={elapsed_fraction})"
    )
    return "\n".join(fc._budget_block_lines(
        inputs, status,
        header_label="Claude Budget", alerts_line="", color=False,
    ))


def test_budget_confidence_is_cause_neutral_on_a_fully_elapsed_period():
    """A9 — the period is fully elapsed and nothing was spent, so the second
    disjunct of the predicate fired. The string must not claim the first."""
    rendered = _budget_lines(spent_usd=0.0, elapsed_fraction=1.0)
    assert "early in week" not in rendered, rendered
    assert "(LOW CONF — limited evidence)" in rendered, rendered


def test_budget_confidence_is_cause_neutral_on_an_early_period():
    """A9 — the same neutral string renders on the branch the old wording did
    describe correctly. Asserted because a test on one branch alone cannot
    tell a cause-neutral string from a still-wrong one."""
    rendered = _budget_lines(spent_usd=40.0, elapsed_fraction=0.05)
    assert "early in week" not in rendered, rendered
    assert "(LOW CONF — limited evidence)" in rendered, rendered


def test_the_budget_share_note_carries_the_same_neutral_string():
    """The shared artifact and the terminal describe one status, so they must
    not disagree about why confidence is low."""
    source = (_REPO / "bin" / "_cctally_forecast.py").read_text()
    assert "LOW CONF — early in week" not in source, (
        "a budget surface still hardcodes the cause-specific claim"
    )
    assert source.count("LOW CONF — limited evidence") == 2, (
        "both budget surfaces — the terminal projection line and the share "
        "snapshot note — must carry the neutral string"
    )


# --- D10, forecast ------------------------------------------------------

@pytest.mark.parametrize(
    "code, expected",
    [
        ("elapsed_hours<24", "less than 24 hours into the week"),
        ("percent<2", "under 2% of quota used so far"),
        ("snapshots<3", "fewer than 3 usage snapshots"),
        ("no_sample_ge_24h", "no snapshot at least 24 hours old"),
    ],
)
def test_forecast_confidence_is_human(code, expected):
    """A9 — each of the four codes maps to its exact expected text."""
    fc = _forecast_module()
    assert fc._forecast_confidence_wording([code]) == expected


def test_an_unrecognised_forecast_code_takes_the_fallback():
    """A9 — an unrecognised code is surfaced, not dropped. Dropping it would
    render `LOW CONF — insufficient data ()`, which states no cause at all."""
    fc = _forecast_module()
    rendered = fc._forecast_confidence_wording(["percent<2", "brand_new_code"])
    assert "brand_new_code" in rendered, rendered
    assert "under 2% of quota used so far" in rendered, rendered


def test_the_forecast_reason_codes_are_unchanged_on_the_wire():
    """The mapping is a render step only, so `--json` consumers are
    unaffected. The four codes the predicate emits are asserted here so a
    rename of a code cannot pass by moving both sides at once."""
    fc = _forecast_module()
    _, reasons = fc._assess_forecast_confidence(1.0, 0.0, 0)
    assert reasons == ["elapsed_hours<24", "percent<2", "snapshots<3"]


def test_forecast_doc_lists_every_trigger():
    """A9 — all four trigger names appear in the forecast reference page."""
    doc = (_REPO / "docs" / "commands" / "forecast.md").read_text()
    for code in ("elapsed_hours<24", "percent<2", "snapshots<3",
                 "no_sample_ge_24h"):
        assert code in doc, f"{code} is not documented in forecast.md"


# --- D13, the CLI project cost-share column -----------------------------

_SHARE_NOTE = re.compile(
    r"Cost Share: each project's share of \$([0-9,]+\.[0-9]{2}) — the total "
    r"cost of the (\d+) projects? listed\."
)


def _project_output(tmp_path, scenario: str, *extra_flags: str):
    out_root = tmp_path / "fixtures"
    if not out_root.exists():
        build = subprocess.run(
            [sys.executable, str(_REPO / "bin" / "build-project-fixtures.py"),
             "--out", str(out_root)],
            capture_output=True, text=True, timeout=45,
        )
        assert build.returncode == 0, build.stderr
    home = out_root / scenario
    as_of = ""
    for line in (
        _REPO / "tests" / "fixtures" / "project" / scenario / "input.env"
    ).read_text().splitlines():
        key, _, raw = line.partition("=")
        if key == "AS_OF":
            as_of = raw.strip().strip('"')
    assert as_of, scenario
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
    proc = subprocess.run(
        [sys.executable, str(_BIN), "project", *extra_flags],
        env=env, capture_output=True, text=True, timeout=45,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_project_states_its_denominator(tmp_path):
    """A12 — the CLI cost-share column names its denominator as visible text.

    The terminal has no hover, so the disclosure the dashboard makes in a
    tooltip has to be a rendered line here. The note's figure is checked
    against the command's own per-project costs, so a note naming the wrong
    denominator fails rather than reading plausibly.
    """
    scenario = "two-projects-current-week"
    terminal = _project_output(tmp_path, scenario)
    payload = json.loads(_project_output(tmp_path, scenario, "--json"))

    assert "Cost Share" in terminal, terminal
    match = _SHARE_NOTE.search(terminal)
    assert match, (
        "the cost-share column must name its denominator on the surface:\n"
        f"{terminal}"
    )

    projects = payload["projects"]
    total = sum(float(p["costUsd"]) for p in projects)
    assert float(match.group(1).replace(",", "")) == pytest.approx(total, abs=0.005)
    assert int(match.group(2)) == len(projects)

    for project in projects:
        share = 100.0 * float(project["costUsd"]) / total
        assert f"{share:.1f}%" in terminal, (
            f"{project['displayKey']} should render a {share:.1f}% cost "
            f"share:\n{terminal}"
        )


def test_project_cost_share_is_not_the_quota_percentage(tmp_path):
    """The two percentages have different denominators, so a fixture where
    they coincide would let a column that simply repeats `Used %` pass."""
    payload = json.loads(
        _project_output(tmp_path, "two-projects-current-week", "--json")
    )
    projects = payload["projects"]
    total = sum(float(p["costUsd"]) for p in projects)
    for project in projects:
        share = 100.0 * float(project["costUsd"]) / total
        used = float(project["attributedUsedPercent"])
        assert abs(share - used) > 1.0, (
            f"{project['displayKey']}: cost share {share:.1f}% and used "
            f"{used:.1f}% are too close for this fixture to discriminate"
        )
