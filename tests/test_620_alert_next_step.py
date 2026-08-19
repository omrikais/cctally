"""Every alert axis offers a next step (#620 S1 Task 13, spec D11).

Each of the seven axes renders a `→ Run \\`cctally …\\`` line naming the
command that explains it, scoped to that alert's provider and window. The
line is part of the message body, so `alerts.log` keeps exactly eight tab
fields and the R8 `[label]` title gating is untouched.

Every assertion here reads the bytes the notifier is actually spawned with,
not the return value of a builder — a body that never reaches a renderer
proves nothing about what a user sees.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module

UTC = ZoneInfo("UTC")

# The budget family's next step is the bare `cctally budget`, which reports
# whichever period is LIVE — so a fixture pinned to a fixed past date would
# exercise the closed-period branch instead of the one under test. The anchor
# is taken once, and both the payload and the expected line are derived from
# it, so the two cannot drift apart.
_NOW = dt.datetime.now(dt.timezone.utc)
_LIVE_WEEK_START = (_NOW - dt.timedelta(days=1)).replace(
    minute=0, second=0, microsecond=0
)
_LIVE_WEEK_END = _LIVE_WEEK_START + dt.timedelta(days=7)
_LIVE_MONTH_START = _NOW.replace(
    day=1, hour=0, minute=0, second=0, microsecond=0
)
_LIVE_MONTH_END = (
    _LIVE_MONTH_START.replace(year=_LIVE_MONTH_START.year + 1, month=1)
    if _LIVE_MONTH_START.month == 12
    else _LIVE_MONTH_START.replace(month=_LIVE_MONTH_START.month + 1)
)


def _window(start: dt.datetime, end: dt.datetime) -> str:
    return f"{start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC"


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _dispatch(payload, sink, *, tz=UTC):
    import _cctally_alerts

    return _cctally_alerts._dispatch_alert_notification(
        payload,
        popen_factory=(lambda args, **k: sink.append(list(args))),
        mode="real",
        platform="linux",
        which_on_path=lambda n: n == "notify-send",
        tz=tz,
    )


def _spawned_text(sink):
    """The exact bytes the notifier was handed, joined for substring reads.

    `notify-send` receives the folded `subtitle\\nbody`, so the next-step line
    is present here only if it survived every render step.
    """
    assert sink, "the notifier was never spawned"
    return "\n".join(sink[0])


def _log_fields():
    raw = (_cctally_core.LOG_DIR / "alerts.log").read_bytes()
    assert raw.endswith(b"\n")
    return [
        line.split("\t")
        for line in raw.decode("utf-8").rstrip("\n").split("\n")
    ]


def _payloads(cc):
    """One real payload per axis, built through the production builders."""
    return {
        "weekly": cc._build_alert_payload_weekly(
            threshold=90,
            crossed_at_utc="2026-07-03T12:00:00Z",
            week_start_date="2026-07-01",
            cumulative_cost_usd=47.32,
            dollars_per_percent=0.53,
            account_key="deadbeefdeadbeef",
        ),
        "five_hour": cc._build_alert_payload_five_hour(
            threshold=95,
            crossed_at_utc="2026-07-01T18:00:00Z",
            five_hour_window_key=1782923400,
            block_start_at="2026-07-01T14:30:00Z",
            block_cost_usd=3.87,
            primary_model="claude-sonnet-4-6",
            account_key="deadbeefdeadbeef",
        ),
        "budget": cc._build_alert_payload_budget(
            threshold=90,
            crossed_at_utc="2026-07-03T12:00:00Z",
            week_start_at=_LIVE_WEEK_START.isoformat().replace("+00:00", "Z"),
            budget_usd=300.0,
            spent_usd=275.0,
            consumption_pct=91.6,
        ),
        "codex_budget": cc._build_alert_payload_codex_budget(
            threshold=90,
            crossed_at_utc="2026-07-03T12:00:00Z",
            period_start_at=_LIVE_MONTH_START.isoformat(),
            period="calendar-month",
            budget_usd=120.0,
            spent_usd=118.0,
            consumption_pct=98.3,
        ),
        "project_budget": cc._build_alert_payload_project_budget(
            threshold=100,
            crossed_at_utc="2026-07-03T12:00:00Z",
            week_start_at="2026-07-01T00:00:00Z",
            project="foo",
            project_key="/repos/foo",
            budget_usd=25.0,
            spent_usd=26.0,
            consumption_pct=104.0,
        ),
        "projected": cc._build_alert_payload_projected(
            metric="weekly_pct",
            threshold=90,
            projected_value=95.0,
            denominator=100.0,
            week_start_at=_LIVE_WEEK_START.isoformat().replace("+00:00", "Z"),
        ),
        "quota": cc._build_alert_payload_quota(
            source="codex",
            source_root_key="root-a",
            logical_limit_key="primary",
            observed_slot="primary",
            window_minutes=300,
            resets_at_utc="2026-07-15T15:00:00+00:00",
            threshold=95,
            kind="actual",
            crossed_at_utc="2026-07-15T12:00:00Z",
            qualifying_percent=95.0,
            projected_percent=None,
            account_key="codexkey1234",
        ),
    }


# axis -> (expected command, expected provider, expected window detail)
EXPECTED = {
    # This payload retains only `week_start_date`, which is a calendar day.
    # The window therefore renders at DAY granularity — no clock reading and
    # no zone label, because the row carries neither.
    "weekly": (
        "cctally percent-breakdown --week-start 2026-07-01",
        "claude",
        "2026-07-01 → 2026-07-08",
    ),
    "five_hour": (
        "cctally five-hour-breakdown --block-start 2026-07-01T14:30",
        "claude",
        "2026-07-01 14:30 → 2026-07-01 19:30 UTC",
    ),
    # No `--vendor` / `--period`: `cmd_budget` computes both on the bare
    # status path and then reads the period from config instead, so the two
    # flags name a scope the command cannot honour.
    "budget": (
        "cctally budget",
        "claude",
        _window(_LIVE_WEEK_START, _LIVE_WEEK_END),
    ),
    "codex_budget": (
        "cctally budget",
        "codex",
        _window(_LIVE_MONTH_START, _LIVE_MONTH_END),
    ),
    "project_budget": (
        "cctally project --since 2026-07-01 --until 2026-07-07 --project foo",
        "claude",
        "2026-07-01 00:00 → 2026-07-08 00:00 UTC",
    ),
    # `cctally forecast` has no historical selector either — its only time
    # knob is the `argparse.SUPPRESS`-registered `--as-of` test hook — so the
    # projected week must be LIVE for this axis to offer a command at all.
    "projected": (
        "cctally forecast --explain",
        "claude",
        _window(_LIVE_WEEK_START, _LIVE_WEEK_END),
    ),
    "quota": (
        "cctally codex quota breakdown --reset-at 2026-07-15T15:00:00Z "
        "--root-key root-a --limit-key primary",
        "codex",
        "2026-07-15 10:00 → 2026-07-15 15:00 UTC",
    ),
}

ALL_AXES = tuple(EXPECTED)


@pytest.mark.parametrize("axis", ALL_AXES)
def test_every_axis_carries_a_next_step(cc, axis):
    cc.open_db().close()
    payload = _payloads(cc)[axis]
    command, provider, window = EXPECTED[axis]

    sink = []
    assert _dispatch(payload, sink) == "queued"
    text = _spawned_text(sink)

    expected_line = f"→ Run `{command}` — {provider} · {window}"
    assert expected_line in text, (
        f"{axis}: expected next-step line missing.\nrendered:\n{text}"
    )
    # The window and the provider must be present, not merely implied.
    assert provider in text
    assert window in text


@pytest.mark.parametrize("axis", ALL_AXES)
def test_next_step_is_a_body_line_not_a_log_field(cc, axis):
    """The eighth `alerts.log` field stays unconditional and the log keeps
    exactly one line of exactly eight fields per dispatch attempt."""
    cc.open_db().close()
    payload = _payloads(cc)[axis]
    sink = []
    _dispatch(payload, sink)

    lines = _log_fields()
    assert len(lines) == 1
    fields = lines[0]
    assert len(fields) == 8, f"{axis}: alerts.log field count changed"
    assert fields[1] == axis
    assert fields[7] == payload["account_key"]
    assert "→ Run" not in "\t".join(fields)


def test_r8_label_gating_is_unchanged_by_the_next_step(cc):
    """One real account keeps the title bare; the next step appears anyway."""
    import _lib_accounts
    import _cctally_journal as jr
    import _lib_journal as lj

    solo = _lib_accounts.account_key("claude", "uuid-solo")
    for kw in (
        dict(at="2026-07-01T00:00:00Z", account_key=solo, provider="claude",
             email="solo@x.com", label="solo", label_source="auto"),
        dict(at="2026-07-01T00:00:00Z", account_key="unattributed",
             provider="claude", label_source="auto"),
    ):
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    payload = cc._build_alert_payload_weekly(
        threshold=90, crossed_at_utc="2026-07-03T12:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=47.32,
        dollars_per_percent=0.53, account_key=solo,
    )
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert "[solo]" not in text
    assert "→ Run `cctally percent-breakdown --week-start 2026-07-01`" in text
    # The command must not leak an account selector: a pure text builder
    # cannot evaluate the R8 gate, and `unattributed` / `*` are sentinels.
    assert "--account" not in text


def test_r8_label_prefix_still_applies_above_one_real_account(cc):
    import _lib_accounts
    import _cctally_journal as jr
    import _lib_journal as lj

    ka = _lib_accounts.account_key("claude", "uuid-a")
    kb = _lib_accounts.account_key("claude", "uuid-b")
    for kw in (
        dict(at="2026-07-01T00:00:00Z", account_key=ka, provider="claude",
             email="a@x.com", label="alice", label_source="auto"),
        dict(at="2026-07-02T00:00:00Z", account_key=kb, provider="claude",
             email="b@x.com", label="bob", label_source="auto"),
    ):
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    payload = cc._build_alert_payload_weekly(
        threshold=90, crossed_at_utc="2026-07-03T12:00:00Z",
        week_start_date="2026-07-01", cumulative_cost_usd=47.32,
        dollars_per_percent=0.53, account_key=ka,
    )
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert "[alice]" in text
    assert "→ Run `cctally percent-breakdown --week-start 2026-07-01`" in text


def test_withheld_scope_states_why_instead_of_inventing_a_window(cc):
    """A projected budget alert with no retained period cannot name a window
    length. The line offers the live-window command and states the gap rather
    than substituting a window the alert never described."""
    cc.open_db().close()
    payload = cc._build_alert_payload_projected(
        metric="budget_usd", threshold=100, projected_value=310.0,
        denominator=300.0, week_start_at="2026-07-01T00:00:00Z",
    )
    assert "period" not in payload["context"]
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert "→ Run `cctally budget`" in text
    assert "retains no period" in text
    assert "2026-07-08" not in text, "a window length was invented"


def test_an_axis_with_no_derivable_window_offers_nothing(cc):
    """A five-hour alert whose block start and window key are both missing has
    no addressable window, so the line states that and offers no command."""
    cc.open_db().close()
    payload = {
        "id": "five_hour:0:95",
        "axis": "five_hour",
        "threshold": 95,
        "crossed_at": "2026-07-01T18:00:00Z",
        "alerted_at": "2026-07-01T18:00:00Z",
        "account_key": "deadbeefdeadbeef",
        "context": {"block_cost_usd": 3.87, "primary_model": None},
    }
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert "→ No scoped explanation:" in text
    assert "retains no block start" in text
    assert "→ Run" not in text


@pytest.mark.parametrize("axis", ALL_AXES)
def test_the_existing_body_is_kept_ahead_of_the_next_step(cc, axis):
    """The next step is appended, never a replacement: the axis's own numbers
    still render, and the affordance sits on its own line after them."""
    cc.open_db().close()
    payload = _payloads(cc)[axis]
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    body_lines = text.split("\n")
    step_index = next(
        i for i, line in enumerate(body_lines) if line.startswith("→ Run")
    )
    assert step_index > 0
    assert body_lines[step_index - 1].strip(), "the next step swallowed the body"


def test_a_closed_budget_period_offers_no_command(cc):
    """`cctally budget` has no historical selector — `cmd_budget`'s bare
    status reads the period from config and its `--period` flag is consumed
    only by `set`/`unset`. Following it from an alert whose period has closed
    would report the LIVE period while the line names the closed one, which is
    exactly the silent substitution the as-of contract forbids. The line
    therefore states why and offers nothing, as the purged five-hour block
    already does."""
    cc.open_db().close()
    payload = cc._build_alert_payload_budget(
        threshold=90,
        crossed_at_utc="2026-07-03T12:00:00Z",
        week_start_at="2026-07-01T00:00:00Z",
        budget_usd=300.0,
        spent_usd=275.0,
        consumption_pct=91.6,
    )
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert "→ No scoped explanation:" in text
    assert "has already closed" in text
    assert "→ Run" not in text
    # Only the navigation is withheld: the axis's own numbers still render.
    assert "$275.00 of $300.00" in text


def test_a_closed_projected_budget_period_offers_no_command(cc):
    """The projected budget metrics reuse `cctally budget`, so they inherit
    the same limit.

    Neither `_build_alert_payload_projected` nor the dashboard's projected
    envelope mapper retains a `period` today, so a projected budget metric's
    window is withheld for a missing period before liveness is ever reached.
    The context is therefore written out here rather than built, and the
    assertion is on the rule the caller applies once a period IS present.
    """
    cc.open_db().close()
    import _lib_alerts_payload as ap

    payload = {
        "id": "projected:2026-07-01T00:00:00+00:00:codex_budget_usd:100",
        "axis": "projected",
        "metric": "codex_budget_usd",
        "threshold": 100,
        "account_key": "*",
        "context": {
            "week_start_at": "2026-07-01T00:00:00+00:00",
            "period": "calendar-month",
            "metric": "codex_budget_usd",
            "projected_value": 140.0,
            "denominator": 120.0,
        },
    }
    line = ap.alert_next_step_line(
        payload, UTC, now=dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc)
    )
    assert line.startswith("→ No scoped explanation:")
    assert "has already closed" in line

    live = ap.alert_next_step_line(
        payload, UTC, now=dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc)
    )
    assert live == (
        "→ Run `cctally budget` — codex · "
        "2026-07-01 00:00 → 2026-08-01 00:00 UTC"
    ), live


def test_a_closed_forecast_week_offers_no_command(cc):
    """`cctally forecast` addresses the LIVE week only, exactly as `cctally
    budget` addresses the live period only.

    Its one time knob is `--as-of`, registered `argparse.SUPPRESS` as a test
    hook rather than a user-facing selector, so the command has no way to
    report a week that has already ended. The `weekly_pct` projected metric
    needs no retained `period` — `_scope_projected` supplies
    `subscription-week` for it — so that metric always derives a full window
    and always reached `available=True` with no liveness test at all.

    The alert is reachable on a backlogged ingest: `maybe_record_milestone` is
    called with the record's own capture time while the resulting alerts
    dispatch post-commit at the current instant, so a backlog spanning a week
    boundary dispatches an alert whose window has closed.
    """
    cc.open_db().close()
    import _lib_alerts_payload as ap

    payload = cc._build_alert_payload_projected(
        metric="weekly_pct",
        threshold=90,
        projected_value=95.0,
        denominator=100.0,
        week_start_at="2026-07-01T00:00:00Z",
    )
    assert "period" not in payload["context"], (
        "the weekly_pct metric derives its period from the metric, not the row"
    )

    closed = ap.alert_next_step_line(
        payload, UTC, now=dt.datetime(2026, 8, 15, tzinfo=dt.timezone.utc)
    )
    assert closed.startswith("→ No scoped explanation:"), closed
    assert "has already closed" in closed, closed
    assert "→ Run" not in closed

    live = ap.alert_next_step_line(
        payload, UTC, now=dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)
    )
    assert live == (
        "→ Run `cctally forecast --explain` — claude · "
        "2026-07-01 00:00 → 2026-07-08 00:00 UTC"
    ), live


@pytest.mark.parametrize("axis", ("budget", "codex_budget"))
def test_the_budget_command_names_no_flag_the_read_path_ignores(cc, axis):
    """`cmd_budget` resolves `--vendor` and `--period` and then consumes them
    only in the `set` / `unset` branches; bare status reads
    `budget_cfg["period"]` from config. A suggested command must not carry a
    selector the command it names cannot honour."""
    cc.open_db().close()
    sink = []
    _dispatch(_payloads(cc)[axis], sink)
    text = _spawned_text(sink)
    step = next(
        line for line in text.split("\n") if line.startswith("→ Run")
    )
    assert "--vendor" not in step
    assert "--period" not in step


def test_the_cli_test_alert_previews_a_real_crossing_not_the_degraded_form(
    cc, monkeypatch
):
    """`cctally alerts test --axis weekly` exists to show what a real weekly
    alert looks like, so it must carry the field a real one carries.

    `_build_alert_payload_weekly` was called without `week_start_at`, which
    defaults to the empty string, so the preview rendered the day-granularity
    fallback — two bare dates, no reset hour, no zone — while an actual
    crossing renders the instant form. No golden asserts the body, so nothing
    caught it.
    """
    import argparse

    import _cctally_alerts

    captured = []
    monkeypatch.setattr(
        _cctally_alerts, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: (
            captured.append((payload, mode)) or "queued"
        ),
    )
    cc.open_db().close()
    rc = _cctally_alerts.cmd_alerts_test(
        argparse.Namespace(axis="weekly", threshold=90, metric="weekly_pct")
    )
    assert rc == 0
    payload, mode = captured[0]
    assert mode == "test"
    assert payload["axis"] == "weekly"

    start_iso = payload["context"]["week_start_at"]
    assert start_iso, (
        "the preview must carry the reset instant a real crossing carries"
    )
    start = dt.datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
    assert start.tzinfo is not None
    assert payload["context"]["week_start_date"] == start.date().isoformat(), (
        "the previewed date and instant must name the same week"
    )

    import _lib_alerts_payload as ap

    line = ap.alert_next_step_line(payload, UTC)
    assert line == (
        "→ Run `cctally percent-breakdown --week-start "
        f"{start:%Y-%m-%d}` — claude · {_window(start, start + dt.timedelta(days=7))}"
    ), line


def test_the_dashboard_test_alert_previews_the_same_instant(cc, monkeypatch):
    """The dashboard's test-alert button mirrors the CLI branch, so it carried
    the same gap. Asserted through the real route, because the payload is built
    inside the handler."""
    import http.client
    import json
    import threading

    monkeypatch.setitem(
        cc.__dict__, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: "queued",
    )
    ns = cc.__dict__
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].no_sync = False
    ns["DashboardHTTPHandler"].display_tz_pref_override = None

    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        raw = json.dumps({"axis": "weekly", "threshold": 90}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest(
            "POST", "/api/alerts/test", skip_host=True,
            skip_accept_encoding=True,
        )
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(raw)))
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Origin", f"http://127.0.0.1:{port}")
        conn.endheaders()
        conn.send(raw)
        resp = conn.getresponse()
        status = resp.status
        body = json.loads(resp.read().decode())
        conn.close()
    finally:
        srv.shutdown()

    assert status == 200, body
    ctx = body["alert"]["context"]
    assert ctx["week_start_at"], (
        "the dashboard preview must carry the reset instant too"
    )
    start = dt.datetime.fromisoformat(
        str(ctx["week_start_at"]).replace("Z", "+00:00")
    )
    assert start.tzinfo is not None
    assert ctx["week_start_date"] == start.date().isoformat()


def test_the_weekly_window_states_the_reset_hour_it_fired_against(cc):
    """A subscription week runs reset-to-reset, not UTC-midnight to
    UTC-midnight. `percent_milestones.week_start_at` retains that instant, so
    the dispatched payload carries it and the line states the real bounds.
    Deriving from `week_start_date` alone shifts both bounds by the reset-hour
    offset while still printing a time — the headline axis stating a window it
    never fired against."""
    cc.open_db().close()
    payload = cc._build_alert_payload_weekly(
        threshold=90,
        crossed_at_utc="2026-04-16T13:00:00Z",
        week_start_date="2026-04-13",
        week_start_at="2026-04-13T14:00:00Z",
        cumulative_cost_usd=38.25,
        dollars_per_percent=0.425,
        account_key="deadbeefdeadbeef",
    )
    assert payload["context"]["week_start_at"] == "2026-04-13T14:00:00Z"
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    assert (
        "→ Run `cctally percent-breakdown --week-start 2026-04-13` — claude · "
        "2026-04-13 14:00 → 2026-04-20 14:00 UTC"
    ) in text, text
    assert "2026-04-13 00:00" not in text


def test_a_date_only_weekly_row_states_no_hour_it_does_not_have(cc):
    """`week_start_at` is nullable and pre-dates the current write path, so a
    historical row carries only the calendar day. The window is still the
    right week, but stating `00:00 → 00:00 UTC` would claim a reset hour the
    row never recorded, so it renders as bare dates."""
    cc.open_db().close()
    payload = cc._build_alert_payload_weekly(
        threshold=90,
        crossed_at_utc="2026-04-16T13:00:00Z",
        week_start_date="2026-04-13",
        cumulative_cost_usd=38.25,
        dollars_per_percent=0.425,
        account_key="deadbeefdeadbeef",
    )
    assert not payload["context"].get("week_start_at")
    sink = []
    _dispatch(payload, sink)
    text = _spawned_text(sink)
    step = next(line for line in text.split("\n") if line.startswith("→ Run"))
    assert step == (
        "→ Run `cctally percent-breakdown --week-start 2026-04-13` — claude · "
        "2026-04-13 → 2026-04-20"
    ), step
    assert "00:00" not in step
    assert "UTC" not in step
