"""Every CLI warning state names the command that explains it (#620 S1
Task 14, spec D11).

These assertions read the committed golden files, which are the recorded
stdout+stderr of a real `bin/cctally` subprocess run by the fixture harness
against a seeded store. That is the rendered output itself, so a builder that
returns the right string but is never wired into a command cannot pass here —
and a golden re-taken with the wrong window fails, because each case pins the
command exactly and cross-checks the window it names against the window the
line states.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# The one affordance shape, shared with the alert bodies: a `cctally` command
# in backticks, an em dash, the provider, then both bounds of the window it
# addresses in the display zone.
NEXT_STEP = re.compile(
    r"→ Run `(?P<command>cctally [^`]+)` — (?P<provider>claude|codex) · "
    r"(?P<start>\d{4}-\d{2}-\d{2}) (?P<start_time>\d{2}:\d{2}) → "
    r"(?P<end>\d{4}-\d{2}-\d{2}) (?P<end_time>\d{2}:\d{2}) [A-Z]+"
)


def _golden(relative: str) -> str:
    path = _FIXTURES / relative
    assert path.is_file(), f"missing golden: {path}"
    return path.read_text()


def _selector_minute(value: str) -> str:
    """A `range-cost` selector read back as the clock reading it names.

    `parse_iso_datetime` treats a naive value as HOST-LOCAL and never extends
    a bare date to end-of-day, so a selector that is not a full UTC instant is
    a different window from the one the line states. Reject the naive form
    here rather than silently normalizing it.
    """
    assert value.endswith("Z"), (
        f"range-cost selector {value!r} is not a UTC instant; "
        "`parse_iso_datetime` reads a naive value as host-local"
    )
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%d %H:%M")


def _sole_next_step(text: str) -> re.Match:
    matches = list(NEXT_STEP.finditer(text))
    assert matches, (
        "no next-step line in the rendered output:\n"
        + "\n".join(text.splitlines()[-12:])
    )
    return matches[0]


# relative golden -> the exact command that warning state must offer.
# `None` means the command carries no window selector of its own, so only the
# fixed prefix is pinned and the window lives in the stated scope.
WARNING_CASES = {
    "forecast/already-capped/golden-terminal.txt": (
        "cctally percent-breakdown --week-start 2026-04-13",
        "claude",
    ),
    "budget/over/golden-terminal.txt": (
        "cctally project --since 2026-05-26 --until 2026-06-02",
        "claude",
    ),
    "project/missing-session-id-fallback/golden-terminal.txt": (
        "cctally cache-sync --source claude",
        "claude",
    ),
}


@pytest.mark.parametrize("relative", sorted(WARNING_CASES))
def test_warning_states_name_the_explaining_command(relative):
    expected_command, expected_provider = WARNING_CASES[relative]
    match = _sole_next_step(_golden(relative))
    assert match.group("command") == expected_command
    assert match.group("provider") == expected_provider


@pytest.mark.parametrize(
    "relative",
    [
        "cache-report/net-negative-anomaly/golden-terminal.txt",
        "diff/mismatched-allowed/golden-terminal.txt",
    ],
)
def test_range_scoped_warnings_offer_the_window_they_state(relative):
    """`cache-report` and `diff` resolve their own window from relative
    selectors, so the fixture — not this test — decides the dates. Pin the
    command shape and require the instant it selects to be the instant the
    line states, at BOTH ends.

    `range-cost -s/-e` are ISO-8601 INSTANTS, not inclusive days: a bare
    `-e 2026-04-15` selects that day's midnight and drops every hour the
    window still covers. Both bounds are therefore compared for equality —
    an upper bound on the end is what let a twelve-hour truncation pass. The
    goldens are recorded under `TZ=Etc/UTC`, so the UTC selector and the
    display-zone statement name the same clock reading.
    """
    match = _sole_next_step(_golden(relative))
    command = match.group("command")
    assert match.group("provider") == "claude"
    inner = re.fullmatch(
        r"cctally range-cost -s (?P<since>\S+) -e (?P<until>\S+) "
        r"-b --source claude",
        command,
    )
    assert inner, f"unexpected command: {command}"
    assert _selector_minute(inner.group("since")) == (
        f"{match.group('start')} {match.group('start_time')}"
    )
    assert _selector_minute(inner.group("until")) == (
        f"{match.group('end')} {match.group('end_time')}"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "forecast/midweek-safe/golden-terminal.txt",
        "budget/under/golden-terminal.txt",
        "cache-report/healthy-cache-hit/golden-terminal.txt",
        "project/two-projects-current-week/golden-terminal.txt",
        "diff/same-length-week/golden-terminal.txt",
    ],
)
def test_healthy_states_offer_nothing(relative):
    """The idiom must not leak into output with nothing to explain."""
    text = _golden(relative)
    assert "→ Run" not in text
    assert "→ No scoped explanation" not in text


def test_every_warning_case_uses_one_shared_shape():
    """All five commands render the same line, so a reader learns it once."""
    seen = set()
    for relative in list(WARNING_CASES) + [
        "cache-report/net-negative-anomaly/golden-terminal.txt",
        "diff/mismatched-allowed/golden-terminal.txt",
    ]:
        match = _sole_next_step(_golden(relative))
        seen.add(match.group(0).split("`")[0])
    assert seen == {"→ Run "}


def test_the_codex_budget_block_names_codex(tmp_path, monkeypatch):
    """A vendor-scoped warning must name its own vendor, not the default.

    No committed budget fixture puts the Codex block into warn or over, so
    this drives the real block renderer over the real status kernel rather
    than asserting a branch no golden reaches.
    """
    import datetime as dt

    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_budget
    import _cctally_forecast as cc

    inputs = _lib_budget.BudgetInputs(
        target_usd=200.0,
        spent_usd=240.0,
        recent_24h_usd=20.0,
        week_start_at=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 5, 26, tzinfo=dt.timezone.utc),
        alert_thresholds=(90, 100),
    )
    status = _lib_budget.compute_budget_status(inputs)
    assert status.verdict == "over", "the fixture must reach the warning state"

    lines = cc._budget_block_lines(
        inputs,
        status,
        header_label="Codex budget: $200.00   (calendar month 2026-05)",
        alerts_line="  Alerts: off",
        color=False,
        provider="codex",
        tz=dt.timezone.utc,
    )
    match = _sole_next_step("\n".join(lines))
    assert match.group("provider") == "codex"
    assert match.group("command") == (
        "cctally project --source codex --since 2026-05-01 --until 2026-05-31"
    )


def test_the_claude_budget_block_defaults_to_claude(tmp_path, monkeypatch):
    import datetime as dt

    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_budget
    import _cctally_forecast as cc

    inputs = _lib_budget.BudgetInputs(
        target_usd=300.0,
        spent_usd=333.0,
        recent_24h_usd=60.0,
        week_start_at=dt.datetime(2026, 5, 26, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 5, 31, 14, tzinfo=dt.timezone.utc),
        alert_thresholds=(90, 100),
    )
    status = _lib_budget.compute_budget_status(inputs)
    assert status.verdict == "over"

    lines = cc._budget_block_lines(
        inputs, status, header_label="Weekly budget: $300.00",
        alerts_line="  Alerts: off", color=False, tz=dt.timezone.utc,
    )
    match = _sole_next_step("\n".join(lines))
    assert match.group("provider") == "claude"
    assert match.group("command") == (
        "cctally project --since 2026-05-26 --until 2026-06-01"
    )


def test_a_healthy_budget_block_offers_nothing(tmp_path, monkeypatch):
    import datetime as dt

    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_budget
    import _cctally_forecast as cc

    inputs = _lib_budget.BudgetInputs(
        target_usd=300.0,
        spent_usd=72.0,
        recent_24h_usd=18.0,
        week_start_at=dt.datetime(2026, 5, 26, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        alert_thresholds=(90, 100),
    )
    status = _lib_budget.compute_budget_status(inputs)
    assert status.verdict == "ok", "this case must NOT be a warning state"

    lines = cc._budget_block_lines(
        inputs, status, header_label="Weekly budget: $300.00",
        alerts_line="  Alerts: off", color=False, tz=dt.timezone.utc,
    )
    assert "→ Run" not in "\n".join(lines)


def test_the_budget_scope_statement_renders_in_the_display_zone(
    tmp_path, monkeypatch, capsys
):
    """The block's header renders its period from the display-tz civil
    boundary, and `docs/commands/alerts.md` says the scope statement renders
    in `display.tz` too. A hardcoded UTC in the next-step line makes the
    header and the line directly below it name two different zones.

    The golden harnesses pin `TZ=Etc/UTC`, so they can never observe this;
    the zone is passed explicitly here instead of inherited from the host.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from conftest import load_isolated_cctally_module

    load_isolated_cctally_module(tmp_path, monkeypatch)
    import _lib_budget
    import _cctally_forecast as cc

    tz = ZoneInfo("Asia/Jerusalem")
    inputs = _lib_budget.BudgetInputs(
        target_usd=300.0,
        spent_usd=333.0,
        recent_24h_usd=60.0,
        week_start_at=dt.datetime(2026, 5, 26, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        now=dt.datetime(2026, 5, 31, 14, tzinfo=dt.timezone.utc),
        alert_thresholds=(90, 100),
    )
    status = _lib_budget.compute_budget_status(inputs)
    assert status.verdict == "over", "the fixture must reach the warning state"

    cc._budget_render_terminal(
        None,
        {"alerts_enabled": False, "alert_thresholds": (90, 100)},
        inputs,
        status,
        period="subscription-week",
        coexists=True,
        tz=tz,
    )
    rendered = capsys.readouterr().out
    match = _sole_next_step(rendered)

    expected_label = cc._cctally().display_tz_label(
        inputs.week_end_at.astimezone(tz)
    )
    assert expected_label != "UTC", "the test zone must differ from UTC"
    assert f"{expected_label}" in match.group(0), (
        f"the scope statement is not in the display zone:\n{match.group(0)}"
    )
    assert match.group("start_time") == inputs.week_start_at.astimezone(
        tz
    ).strftime("%H:%M")
    assert match.group("end_time") == inputs.week_end_at.astimezone(
        tz
    ).strftime("%H:%M")


def test_the_cache_report_selector_is_a_utc_instant_in_a_non_utc_zone(
    tmp_path, monkeypatch, capsys
):
    """`cache-report` resolves its window in the DISPLAY zone, so formatting
    those bounds with a literal `Z` labels a local wall clock as UTC.

    `_resolve_cache_report_window` returns datetimes aware in the resolved
    display zone — or, when `display.tz` is `local` (the default), in the host
    zone. `strftime` formats the datetime's own fields and appends the `Z` as
    a literal character, so `-s`/`-e` name an instant seven hours away from
    the window the line above them states.

    The committed goldens are recorded under `TZ=Etc/UTC`, where the UTC
    selector and the display-zone statement are the same clock reading, so
    they structurally cannot observe this. The zone is passed explicitly here
    instead, the way `test_the_budget_scope_statement_renders_in_the_display_zone`
    does for the budget block.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo

    from conftest import load_isolated_cctally_module

    app = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_cache_report as crm
    import _lib_pricing

    zone = ZoneInfo("America/Los_Angeles")
    as_of = dt.datetime(2026, 4, 15, 12, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-04-15T12:00:00Z")

    def _entry(hours_back, cache_create, cache_read):
        import types

        return types.SimpleNamespace(
            timestamp=as_of - dt.timedelta(hours=hours_back),
            model="claude-opus-4-7",
            usage=_lib_pricing.claude_usage_dict(
                input_tokens=20_000,
                output_tokens=2_000,
                cache_creation_tokens=cache_create,
                cache_read_tokens=cache_read,
                cache_1h_tokens=None,
                speed=None,
            ),
            cost_usd=8.98,
            source_path="/fake/jsonl/nna-session.jsonl",
        )

    # Heavy cache creation against a trivial read: the write premium dominates
    # so `net_usd < 0` and the `net_negative` predicate fires, which is what
    # puts the next-step line on screen at all.
    entries = [_entry(10, 800_000, 5_000), _entry(4, 600_000, 2_000)]
    monkeypatch.setattr(app, "get_entries", lambda *a, **k: entries)

    args = app.build_parser().parse_args(
        ["cache-report", "--tz", "America/Los_Angeles", "--days", "7"]
    )
    assert crm.cmd_cache_report(args) == 0
    rendered = capsys.readouterr().out
    match = _sole_next_step(rendered)

    inner = re.fullmatch(
        r"cctally range-cost -s (?P<since>\S+) -e (?P<until>\S+) "
        r"-b --source claude",
        match.group("command"),
    )
    assert inner, f"unexpected command: {match.group('command')}"

    # The true window: `--days 7` anchors on the display zone's calendar days,
    # so it opens at local midnight six days back and closes at the pinned now.
    expected_since = dt.datetime(2026, 4, 9, 0, 0, tzinfo=zone)
    expected_until = as_of
    assert dt.datetime.fromisoformat(
        inner.group("since").replace("Z", "+00:00")
    ) == expected_since, "the -s selector is not the instant the window opens"
    assert dt.datetime.fromisoformat(
        inner.group("until").replace("Z", "+00:00")
    ) == expected_until, "the -e selector is not the instant the window closes"

    # And the statement beside it stays in the display zone, so the two agree
    # about the same two instants rather than about the same digits.
    assert match.group("start_time") == expected_since.astimezone(
        zone
    ).strftime("%H:%M")
    assert match.group("end_time") == expected_until.astimezone(
        zone
    ).strftime("%H:%M")
