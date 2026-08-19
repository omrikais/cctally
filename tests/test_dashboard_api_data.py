"""Tests for /api/data JSON envelope."""
import datetime as dt
import http.client
import json
import threading
import types

import pytest

from conftest import load_script
from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)


@pytest.fixture(autouse=True)
def _isolate_prod_dbs(monkeypatch, tmp_path):
    """Issue #144: ``snapshot_to_envelope`` / the ``/api/data`` + ``/api/events``
    handlers open ``cache.db`` + ``stats.db`` (for ``last_sync_at`` / freshness)
    even when fed a hand-built snapshot. Without HOME isolation those resolve to
    the developer's REAL ``~/.local/share/cctally`` (conftest sets
    ``CCTALLY_DISABLE_DEV_AUTODETECT=1`` process-wide, so ``_init_paths_from_env``
    falls to the prod layout under ``$HOME``). That leaks test reads onto the
    real machine and — from a dev checkout whose prod DB lags the migration
    registry — trips the #142 prod-migration guard.

    Every test here calls ``load_script()`` IN-BODY, and ``load_script`` re-runs
    ``_cctally_core._init_paths_from_env()`` against the current ``$HOME`` (the
    conftest-blessed ``setenv("HOME", tmp) + load_script()`` ordering). So
    setting ``HOME`` to a fresh tmp dir BEFORE the body runs is sufficient to
    redirect every path constant to ``tmp`` — no per-test signature change. The
    pwd-resolved guard (``_real_prod_data_dir``) still points at real prod, so a
    tmp-dir open can never be mistaken for prod.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_envelope_has_all_top_level_keys():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert set(env.keys()) >= {
        "generated_at", "last_sync_at", "sync_age_s", "last_sync_error",
        "header", "current_week", "forecast", "trend", "sessions",
    }


def test_envelope_appends_frozen_source_bundle_without_changing_legacy_values():
    """S4 appends its read-model contract; the legacy Claude envelope is intact."""
    ns = load_script()
    now = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    snap = ns["_empty_dashboard_snapshot"]()
    legacy = {
        key: value
        for key, value in ns["snapshot_to_envelope"](snap, now_utc=now).items()
        if key not in {"source_schema_version", "default_source", "source_order", "sources"}
    }
    claude = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-v1",
        last_success_at=now,
        capabilities={"hero": CapabilityRecord("supported", "subscription-week")},
        data={"hero": {"cost_usd": 1.25, "total_tokens": 42}},
        # #556 S1 §3.8: composition fails closed without an authoritative
        # count, so an undecorated single account has to be stated.
        account_scope={"real_account_count": 1},
    )
    codex = SourceDashboardState(
        source="codex",
        availability="empty",
        freshness="fresh",
        warnings=(),
        data_version="codex-v1",
        last_success_at=now,
        capabilities={"hero": CapabilityRecord("supported", "calendar-week")},
        data={"hero": {"cost_usd": 0.0, "total_tokens": 0}},
        account_scope={"real_account_count": 1},
    )
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": compose_all_state(claude, codex)},
    )

    envelope = ns["snapshot_to_envelope"](snap, now_utc=now)

    assert {key: envelope[key] for key in legacy} == legacy
    assert envelope["source_schema_version"] == SOURCE_SCHEMA_VERSION
    assert envelope["default_source"] == "claude"
    assert envelope["source_order"] == ["claude", "codex", "all"]
    assert envelope["sources"]["claude"]["data"] == {
        "hero": {"cost_usd": 1.25, "total_tokens": 42},
    }
    # #556 S1 §3.5: the Codex provider is `empty`, so its leg is explicit and
    # the published figure is the Claude leg alone. Neither leg carries a
    # period here — this snapshot publishes no cycle bounds.
    assert envelope["sources"]["all"]["data"]["combined"] == {
        "cost_usd": 1.25,
        "total_tokens": 42,
        "legs": {
            "claude": {
                "state": "current", "scope": "provider_cycle",
                "cost_usd": 1.25, "total_tokens": 42,
            },
            "codex": {
                "state": "empty", "scope": "provider_cycle",
                "cost_usd": 0.0, "total_tokens": 0,
            },
        },
        "qualifications": [
            {
                "code": "provider_empty",
                "message": "Codex has no accounting in its current cycle.",
                "provider": "codex",
            },
        ],
    }
    assert "combined_unavailable" not in envelope["sources"]["all"]["data"]
    json.dumps(envelope)


def test_codex_labels_are_injected_per_request_into_parent_and_account_rows():
    """One frozen source bundle serves clients with different transcript gates.

    Codex labels are transcript-derived content: they stay in a server-private
    key map and are injected into fresh request-local wire rows only when the
    same gate that controls ``transcriptsEnabled`` is open.
    """
    ns = load_script()
    now = dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc)
    key = "session:codex-private"
    label = "Private first prompt"
    claude = SourceDashboardState(
        source="claude", availability="empty", freshness="fresh",
        warnings=(), data_version="claude-v1", last_success_at=now,
        capabilities={}, data={"sessions": {"rows": ()}},
    )
    codex = SourceDashboardState(
        source="codex", availability="ok", freshness="fresh",
        warnings=(), data_version="codex-v1", last_success_at=now,
        capabilities={},
        data={
            "sessions": {"rows": ({"key": key, "source": "codex"},)},
            "account_scopes": {
                "account-a": {
                    "sessions": {
                        "rows": ({"key": key, "source": "codex"},),
                    },
                },
            },
        },
    )
    # The server-private map stays outside the published data tree and is read
    # only by the request-local overlay.
    object.__setattr__(codex, "private_session_labels", {key: label})
    snap = ns["_empty_dashboard_snapshot"]()
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION, default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": claude,
            "codex": codex,
            "all": compose_all_state(claude, codex),
        },
    )

    open_first = ns["snapshot_to_envelope"](
        snap, now_utc=now, transcripts_visible=True,
    )
    closed = ns["snapshot_to_envelope"](
        snap, now_utc=now, transcripts_visible=False,
    )
    open_again = ns["snapshot_to_envelope"](
        snap, now_utc=now, transcripts_visible=True,
    )

    def codex_rows(env):
        # #583 S3 §4: `sources.all.data.providers` publishes null for both
        # members, so the physical `sources.codex` entry is the ONE place the
        # All tab reads these rows from. The mirrored pair this used to check
        # was the SAME list object, so dropping it removes a duplicate
        # assertion rather than coverage — the mirror's own nulling is
        # asserted immediately below.
        sources = env["sources"]
        direct = sources["codex"]["data"]
        assert sources["all"]["data"]["providers"] == {
            "claude": None, "codex": None,
        }
        return (
            direct["sessions"]["rows"],
            direct["account_scopes"]["account-a"]["sessions"]["rows"],
        )

    for env in (open_first, open_again):
        assert all(rows[0]["label"] == label for rows in codex_rows(env))
    assert all("label" not in rows[0] for rows in codex_rows(closed))
    # The shared frozen publication remains content-free after open requests.
    assert "label" not in codex.data["sessions"]["rows"][0]


def test_envelope_without_source_bundle_fails_closed_with_unavailable_sources():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()

    envelope = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc),
    )

    assert envelope["sources"]["claude"]["availability"] == "unavailable"
    assert envelope["sources"]["codex"]["data"] is None


def test_envelope_null_panels_on_empty_snapshot():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    # Panels that have no data serialize as None so the JS can render "—".
    assert env["current_week"] is None
    assert env["forecast"] is None
    assert env["trend"] is None
    assert env["sessions"]["total"] == 0
    assert env["sessions"]["rows"] == []


def test_envelope_generated_at_is_iso_z():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["generated_at"].endswith("Z")


def test_envelope_is_json_serializable():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    json.dumps(env)  # must not raise


def test_api_data_returns_json_200():
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    # #583 S3 §7: `/api/data` serves the most recently PUBLISHED state, so a
    # bare reference is not enough — seed the hub exactly as `cmd_dashboard`
    # does before the HTTP server binds.
    ns["DashboardHTTPHandler"].hub.publish(
        ns["DashboardHTTPHandler"].snapshot_ref.get()
    )
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=2)
        c.request("GET", "/api/data")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("application/json")
        env = json.loads(body)
        assert "header" in env
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_envelope_has_weekly_and_monthly_keys():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert "weekly" in env
    assert "monthly" in env


def test_envelope_weekly_monthly_empty_rows_on_empty_snapshot():
    """Empty snapshot -> `{rows: []}` panel keys, NOT null. Spec §2.7
    says the empty state is `weekly.rows === []`, not `weekly === null`,
    so the panel can distinguish "synced + no data" from "loading".

    View-model unification (Bundle 1, spec §6.6) added optional
    `total_cost_usd` / `total_tokens` scalars on the monthly block;
    empty snapshots emit them as 0.0 / 0 (additive identity, not None
    — see ``DataSnapshot.monthly_total_*`` defaults). Weekly's totals
    land in Task 9.
    """
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["weekly"] == {
        "rows": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    assert env["monthly"] == {
        "rows": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }


def test_envelope_weekly_emits_rows_when_snapshot_populated():
    """Hand-build a snapshot with one WeeklyPeriodRow; envelope emits it."""
    ns = load_script()
    row = ns["WeeklyPeriodRow"](
        label="04-23",
        cost_usd=48.21,
        total_tokens=346_000_000,
        input_tokens=414_000,
        output_tokens=240_000,
        cache_creation_tokens=21_300_000,
        cache_read_tokens=324_000_000,
        used_pct=41.0,
        dollar_per_pct=1.18,
        delta_cost_pct=0.09,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 26.51, "cost_pct": 55.0}],
        week_start_at="2026-04-23T09:59:00+02:00",
        week_end_at="2026-04-30T09:59:00+02:00",
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.weekly_periods = [row]
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 25,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["weekly"] is not None
    assert len(env["weekly"]["rows"]) == 1
    r = env["weekly"]["rows"][0]
    assert r["label"] == "04-23"
    assert r["cost_usd"] == 48.21
    assert r["used_pct"] == 41.0
    assert r["dollar_per_pct"] == 1.18
    assert r["is_current"] is True
    assert r["models"][0]["chip"] == "opus"
    assert r["week_start_at"].startswith("2026-04-23")


def _make_weekly_row(ns, *, label, cost_usd, total_tokens,
                     week_start_at, week_end_at, is_current=False):
    """Helper for the structural-invariant tests below.

    Keeps the cross-test row construction in one place so adding a
    field to ``WeeklyPeriodRow`` only requires editing one spot.
    """
    return ns["WeeklyPeriodRow"](
        label=label,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        used_pct=None,
        dollar_per_pct=None,
        delta_cost_pct=None,
        is_current=is_current,
        models=[],
        week_start_at=week_start_at,
        week_end_at=week_end_at,
    )


def _make_monthly_row(ns, *, label, cost_usd, total_tokens):
    return ns["MonthlyPeriodRow"](
        label=label,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        delta_cost_pct=None,
        is_current=False,
        models=[],
    )


def _make_daily_row(ns, *, date, cost_usd, total_tokens, is_today=False):
    return ns["DailyPanelRow"](
        date=date,
        label=date[5:],
        cost_usd=cost_usd,
        is_today=is_today,
        intensity_bucket=0,
        models=[],
        total_tokens=total_tokens,
    )


def test_weekly_envelope_total_matches_sum_of_visible_rows():
    """Structural invariant (spec §6.6, Critical #1 regression):
    the weekly envelope's ``total_cost_usd`` / ``total_tokens`` MUST
    equal the sum over the rendered ``rows[]`` — never undercount
    synthesized rows.

    Pre-fix, ``snap.weekly_total_cost_usd`` was sourced from a
    parallel ``build_weekly_view`` call that iterated only over
    ``_aggregate_weekly`` buckets — but the dashboard's
    ``_dashboard_build_weekly_periods`` synthesizes Bug-K pre-credit
    segment rows on top of those buckets. On credit weeks the
    builder-sourced total undercounted the rendered footer by
    hundreds of dollars (~$372 in the v1.7.2 round-5 case). Coupling
    the sync-thread totals to ``sum(r.cost_usd for r in rows)`` makes
    the invariant structural.
    """
    ns = load_script()
    # Hand-build a snapshot that mimics the Bug-K synthesized layout:
    # a single subscription week containing a pre-credit synthesized
    # row PLUS the post-credit row that ``_aggregate_weekly`` produced.
    pre_row = _make_weekly_row(
        ns, label="04-18", cost_usd=372.50, total_tokens=900_000_000,
        week_start_at="2026-04-18T00:00:00Z",
        week_end_at="2026-04-21T12:30:00Z",
    )
    post_row = _make_weekly_row(
        ns, label="04-21", cost_usd=134.00, total_tokens=300_000_000,
        week_start_at="2026-04-21T12:30:00Z",
        week_end_at="2026-04-25T00:00:00Z",
        is_current=True,
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.weekly_periods = [post_row, pre_row]  # newest-first
    # Mimic what the sync thread now does (Critical #1 fix):
    # sum-over-visible-rows.
    snap.weekly_total_cost_usd = sum(r.cost_usd for r in snap.weekly_periods)
    snap.weekly_total_tokens = sum(r.total_tokens for r in snap.weekly_periods)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["weekly"]["rows"], "test setup must produce visible rows"
    expected_cost = sum(r["cost_usd"] for r in env["weekly"]["rows"])
    expected_tokens = sum(r["total_tokens"] for r in env["weekly"]["rows"])
    assert env["weekly"]["total_cost_usd"] == pytest.approx(
        expected_cost, abs=1e-9,
    )
    assert env["weekly"]["total_tokens"] == expected_tokens
    # And materially: the pre-credit row's $372 IS in the total.
    assert env["weekly"]["total_cost_usd"] == pytest.approx(506.50, abs=1e-9)


def test_monthly_envelope_total_matches_sum_of_visible_rows():
    """Same structural invariant for the monthly envelope (spec §6.6).
    Mirrors the weekly assertion so the symmetric fix-shape stays
    pinned (no parallel ``build_monthly_view`` totals drift).
    """
    ns = load_script()
    rows = [
        _make_monthly_row(ns, label="2026-04", cost_usd=182.50,
                          total_tokens=1_000_000_000),
        _make_monthly_row(ns, label="2026-03", cost_usd=140.25,
                          total_tokens=800_000_000),
    ]
    snap = ns["_empty_dashboard_snapshot"]()
    snap.monthly_periods = rows
    snap.monthly_total_cost_usd = sum(r.cost_usd for r in rows)
    snap.monthly_total_tokens = sum(r.total_tokens for r in rows)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["monthly"]["rows"], "test setup must produce visible rows"
    assert env["monthly"]["total_cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in env["monthly"]["rows"]), abs=1e-9,
    )
    assert env["monthly"]["total_tokens"] == sum(
        r["total_tokens"] for r in env["monthly"]["rows"]
    )


def test_daily_envelope_total_matches_sum_of_visible_rows():
    """Same structural invariant for the daily envelope (spec §6.6).
    Daily's materialized panel includes zero-cost gap rows (the
    contiguous N-day calendar window); those contribute 0 to the sum
    so the invariant holds whether or not gap days are present.
    """
    ns = load_script()
    rows = [
        _make_daily_row(ns, date="2026-04-25", cost_usd=12.34,
                        total_tokens=10_000_000, is_today=True),
        # zero-cost gap day — must NOT shift the invariant.
        _make_daily_row(ns, date="2026-04-24", cost_usd=0.0, total_tokens=0),
        _make_daily_row(ns, date="2026-04-23", cost_usd=8.50,
                        total_tokens=7_000_000),
    ]
    snap = ns["_empty_dashboard_snapshot"]()
    snap.daily_panel = rows
    snap.daily_total_cost_usd = sum(r.cost_usd for r in rows)
    snap.daily_total_tokens = sum(r.total_tokens for r in rows)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["daily"]["rows"], "test setup must produce visible rows"
    assert env["daily"]["total_cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in env["daily"]["rows"]), abs=1e-9,
    )
    assert env["daily"]["total_tokens"] == sum(
        r["total_tokens"] for r in env["daily"]["rows"]
    )


def test_envelope_monthly_emits_rows_when_snapshot_populated():
    ns = load_script()
    row = ns["MonthlyPeriodRow"](
        label="2026-04",
        cost_usd=182.50,
        total_tokens=1_000_000_000,
        input_tokens=2_000_000,
        output_tokens=500_000,
        cache_creation_tokens=92_000_000,
        cache_read_tokens=900_000_000,
        delta_cost_pct=0.02,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 110.0, "cost_pct": 60.0}],
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.monthly_periods = [row]
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 25,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["monthly"] is not None
    assert env["monthly"]["rows"][0]["label"] == "2026-04"
    # Monthly rows have no used_pct or week boundaries.
    assert "used_pct" not in env["monthly"]["rows"][0]
    assert "week_start_at" not in env["monthly"]["rows"][0]


def test_header_vs_last_week_delta_null_on_empty_trend():
    """#207 B1: with no trend rows (and so no ``is_current`` row), the
    header's vs-last-week delta is None so the client hides the stat."""
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](
        snap, now_utc=dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc))
    assert env["header"]["vs_last_week_delta"] is None


def test_header_vs_last_week_delta_uses_is_current_row():
    """#207 B1: the vs-last-week delta is the ``is_current`` trend row's
    ``delta_dpp`` — selected by the flag, NOT by position [-1]. A trailing
    non-current row proves the selector picks by ``is_current``."""
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()

    def mk(label, dpp, delta, cur):
        return types.SimpleNamespace(
            week_label=label, used_pct=10.0, dollars_per_percent=dpp,
            delta_dpp=delta, is_current=cur, spark_height=1,
        )

    # Oldest-first; the current week is the flagged row, NOT positionally
    # guaranteed last — a trailing non-current row proves the selector
    # picks by is_current, not by [-1].
    snap.trend = [
        mk("W1", 1.30, None, False),
        mk("W2", 1.23, -0.07, True),
        mk("W3-stale", 9.99, 8.76, False),
    ]
    env = ns["snapshot_to_envelope"](
        snap, now_utc=dt.datetime(2026, 4, 20, 12, 0, tzinfo=dt.timezone.utc))
    assert env["header"]["vs_last_week_delta"] == -0.07


# --- #583 S3 §6/§7: /api/data serves the last PUBLISHED state, compressed ----


def _boot(ns, hub, ref):
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _fetch(port, *, accept_encoding=None, path="/api/data"):
    """GET `path`, controlling `Accept-Encoding` exactly.

    `http.client` sends `Accept-Encoding: identity` unless told not to, so the
    gzip case has to be requested deliberately.
    """
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path, skip_accept_encoding=True)
    c.putheader("Host", f"127.0.0.1:{port}")
    if accept_encoding is not None:
        c.putheader("Accept-Encoding", accept_encoding)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, headers, body


def _hydrating(snap, value):
    import dataclasses
    return dataclasses.replace(snap, hydrating=value)


def test_api_data_agrees_with_the_stream_on_hydrating_during_a_progressive_fill():
    """#600. The A2 callback sets the reference to a snapshot whose `hydrating`
    flag differs from the one it publishes, so reading the REFERENCE labelled
    partial data complete — measured at +2.6s while the build finished at
    +105.6s. `/api/data` now reports the last PUBLISHED state, which is by
    construction what the stream last sent.
    """
    ns = load_script()
    base = ns["_empty_dashboard_snapshot"]()
    hub = ns["SSEHub"]()
    ref = ns["_SnapshotRef"](_hydrating(base, True))
    hub.publish(ref.get())
    # The builder assembles a NON-hydrating snapshot into the reference while
    # the fill is still running, and has not published it.
    ref.set(_hydrating(base, False))
    srv, t = _boot(ns, hub, ref)
    try:
        status, _headers, body = _fetch(srv.server_address[1])
        assert status == 200
        env = json.loads(body)
        assert env["hydrating"] is True, (
            "/api/data reported a snapshot as complete that no client was sent")
        assert ref.get().hydrating is False, "precondition: the two disagree"
    finally:
        srv.shutdown()
        t.join(timeout=2)


@pytest.mark.parametrize(
    "mutator", ["set", "capture_batch", "settle", "mark_rebuilding"])
def test_api_data_serves_the_last_published_state_for_every_mutator(mutator):
    """The reference is mutated by FOUR operations that publish separately, so
    `/api/data` is DEFINED as the last published state rather than as the
    reference. Exercising only `set` would miss three of the four windows in
    which the two can disagree."""
    ns = load_script()
    base = ns["_empty_dashboard_snapshot"]()
    hub = ns["SSEHub"]()
    ref = ns["_SnapshotRef"](_hydrating(base, True))
    hub.publish(ref.get())

    if mutator == "set":
        ref.set(_hydrating(base, False))
    elif mutator == "capture_batch":
        ref.set(_hydrating(base, False))
        ref.capture_batch()
    elif mutator == "settle":
        ref.set(_hydrating(base, False))
        batch_id, _refresh = ref.capture_batch()
        ref.settle(batch_id, "ok")
    else:
        ref.set(_hydrating(base, False))
        ref.mark_rebuilding(True)

    srv, t = _boot(ns, hub, ref)
    try:
        status, _headers, body = _fetch(srv.server_address[1])
        assert status == 200
        assert json.loads(body)["hydrating"] is True, mutator
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_api_data_503s_before_the_first_publication():
    """No silent fallback to the reference: that is exactly the disagreement
    this endpoint's contract removes."""
    ns = load_script()
    hub = ns["SSEHub"]()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    srv, t = _boot(ns, hub, ref)
    try:
        status, _headers, body = _fetch(srv.server_address[1])
        assert status == 503
        assert "error" in json.loads(body)
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_api_data_is_gzip_when_negotiated_and_decodes_to_the_identity_body():
    import gzip as _gzip
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    hub.publish(snap)
    srv, t = _boot(ns, hub, ref)
    port = srv.server_address[1]
    try:
        st_id, h_id, body_id = _fetch(port, accept_encoding="identity")
        st_gz, h_gz, body_gz = _fetch(port, accept_encoding="gzip")
        assert st_id == st_gz == 200
        assert h_id.get("content-encoding") is None
        assert h_gz.get("content-encoding") == "gzip"
        assert h_id.get("vary") == h_gz.get("vary") == "Accept-Encoding"
        # Content-Length describes the bytes actually on the wire.
        assert int(h_gz["content-length"]) == len(body_gz)
        assert int(h_id["content-length"]) == len(body_id)
        decoded = _gzip.decompress(body_gz)
        # Byte-for-byte the identity serialization, modulo the wall-clock
        # fields that legitimately advance between two requests.
        assert json.loads(decoded)["header"] == json.loads(body_id)["header"]
        assert len(body_gz) < len(body_id)
        # And the strict JSON contract holds through compression.
        assert json.loads(decoded)["source_schema_version"] == SOURCE_SCHEMA_VERSION
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_api_data_never_appends_a_second_response_after_committing():
    """#583 S3 §6. The handler used to wrap the header commit AND the body
    write in one `try` whose `except` called `_respond_json(500, ...)`, which
    writes a SECOND HTTP response onto an already-committed stream. Fail the
    body write and assert no JSON error body follows the partial body.
    """
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    hub.publish(snap)
    handler = ns["DashboardHTTPHandler"]
    responded = []
    real_respond = handler._respond_json

    def recording(self, status, payload):
        responded.append(status)
        return real_respond(self, status, payload)

    handler._respond_json = recording
    real_data = handler._serve_api_data

    fired = []

    def failing_write(self):
        real_write = self.wfile.write

        def write(data):
            # The BODY, not the header block: `BaseHTTPRequestHandler` flushes
            # its buffered headers through this same `write`, and failing on
            # those would exercise the PRE-commit path this test is not about.
            if len(data) > 1000:
                fired.append(len(data))
                raise BrokenPipeError("simulated peer gone mid-body")
            return real_write(data)

        self.wfile.write = write
        try:
            return real_data(self)
        finally:
            self.wfile.write = real_write

    handler._serve_api_data = failing_write
    srv, t = _boot(ns, hub, ref)
    try:
        try:
            _fetch(srv.server_address[1])
        except Exception:
            pass    # a truncated response is the expected client-side outcome
        assert fired, "the injected write failure never fired — test is vacuous"
        assert responded == [], (
            f"a second HTTP response was appended after commit: {responded}")
    finally:
        handler._respond_json = real_respond
        handler._serve_api_data = real_data
        srv.shutdown()
        t.join(timeout=2)


def _raw_get(port, path="/api/data"):
    """Every byte the server wrote for one request, unparsed.

    `http.client` stops reading at the end of the FIRST response, so it cannot
    observe a second HTTP response appended onto an already-committed stream.
    Reading the socket to EOF can, and counting `HTTP/1.` status lines is the
    discriminating assertion.
    """
    import socket
    chunks = []
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Accept-Encoding: identity\r\nConnection: close\r\n\r\n".encode())
        while True:
            block = s.recv(65536)
            if not block:
                break
            chunks.append(block)
    except OSError:
        pass
    finally:
        s.close()
    return b"".join(chunks)


def test_api_data_never_appends_a_second_response_when_the_503_write_fails():
    """#583 S3 §6, on the one path the prepare/commit split did not enumerate.

    The not-yet-published 503 was written INSIDE the preparation `try` whose
    `except` answers a JSON 500. So a failure part-way through writing the 503
    was caught by that `except`, which appended a SECOND HTTP response onto a
    stream the handler had already committed — exactly the defect the split
    exists to remove. The CHANGELOG already tells users this cannot happen, so
    it must actually not happen.
    """
    ns = load_script()
    # Nothing published, so the handler takes the 503 path.
    hub = ns["SSEHub"]()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    handler = ns["DashboardHTTPHandler"]
    real_data = handler._serve_api_data
    fired = []

    def failing_body_write(self):
        real_write = self.wfile.write

        def write(data):
            # The JSON body, not the header block: `BaseHTTPRequestHandler`
            # flushes its buffered headers through this same `write` and those
            # start with `HTTP/1.`. Failing the body leaves the response
            # COMMITTED, which is the state the handler must not answer twice.
            if data[:1] == b"{":
                fired.append(bytes(data))
                raise BrokenPipeError("simulated peer gone mid-body")
            return real_write(data)

        self.wfile.write = write
        try:
            return real_data(self)
        finally:
            self.wfile.write = real_write

    handler._serve_api_data = failing_body_write
    srv, t = _boot(ns, hub, ref)
    try:
        raw = _raw_get(srv.server_address[1])
        assert fired, "the injected write failure never fired — test is vacuous"
        assert raw.split(b"\r\n", 1)[0].split()[1:2] == [b"503"], raw[:200]
        assert raw.count(b"HTTP/1.") == 1, (
            "a second HTTP response was appended after commit: %r" % (raw[:400],))
    finally:
        handler._serve_api_data = real_data
        srv.shutdown()
        t.join(timeout=2)


def test_api_data_projects_with_a_freshly_sampled_clock_not_the_publication_pin():
    """#583 S3 §5, acceptance criterion 8, second half.

    A delivery pins its clock at publication so every SSE client served from one
    tick agrees on the age fields. `/api/data` answers NOW, so reusing that pin
    would report ages frozen at the last publication: a poll long after the last
    tick would claim the data had just been synced.

    Two OBSERVED events are compared — the pin the hub stored at publication and
    the clock the handler passed into the projection — never a wall-clock
    ceiling.

    The comparison is made on `time.monotonic()` alone. The handler samples its
    UTC through `_now_utc()`, which honours `CCTALLY_AS_OF`, while the hub pins
    `dt.datetime.now(dt.timezone.utc)`, which does not. A worker that leaked
    `CCTALLY_AS_OF` into `os.environ` would therefore fail a UTC comparison for
    a reason that has nothing to do with the mechanism under test, and this
    repository has already lost a session to exactly that leak. The monotonic
    pair carries the whole claim: `mono1 > pinned.pinned_monotonic` proves the
    handler sampled rather than reused the pin, and `mono2 > mono1` proves it
    samples per request. `utc2 >= utc1` is kept because both sides come from
    `_now_utc()`, so a pin makes them equal instead of making them disagree.
    """
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    hub.publish(snap)
    pinned = hub.latest()
    dashboard = ns["_cctally_dashboard"]
    real = dashboard.snapshot_to_envelope
    seen = []

    def recording(snapshot, **kw):
        seen.append((kw.get("now_utc"), kw.get("monotonic_now")))
        return real(snapshot, **kw)

    dashboard.snapshot_to_envelope = recording
    srv, t = _boot(ns, hub, ref)
    try:
        assert _fetch(srv.server_address[1])[0] == 200
        assert _fetch(srv.server_address[1])[0] == 200
    finally:
        dashboard.snapshot_to_envelope = real
        srv.shutdown()
        t.join(timeout=2)

    assert len(seen) == 2, seen
    (utc1, mono1), (utc2, mono2) = seen
    assert mono1 > pinned.pinned_monotonic, (
        "/api/data reused the publication pin instead of sampling now")
    # And each request samples again rather than caching the first sample.
    assert mono2 > mono1
    assert utc2 >= utc1


def test_api_data_still_500s_when_projection_fails_before_commit():
    """Preparation-phase failures must still answer a JSON 500 — nothing has
    been sent yet, so a status code is still available."""
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    hub.publish(snap)
    dashboard = ns["_cctally_dashboard"]
    real = dashboard.snapshot_to_envelope

    def boom(*_a, **_kw):
        raise RuntimeError("projection exploded")

    dashboard.snapshot_to_envelope = boom
    srv, t = _boot(ns, hub, ref)
    try:
        status, headers, body = _fetch(srv.server_address[1])
        assert status == 500
        assert headers.get("content-type", "").startswith("application/json")
        assert json.loads(body) == {"error": "internal error"}
    finally:
        dashboard.snapshot_to_envelope = real
        srv.shutdown()
        t.join(timeout=2)
