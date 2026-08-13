"""#556 S1 Unit 1 — the server contract behind the All combined headline.

Spec: ``docs/superpowers/specs/2026-08-13-556-s1-combined-headline.md``.

This module owns the DB-backed halves of Unit 1: Claude's current-cycle
accumulation (§3.3), the redefined hero counters (§3.4), and the period
identity that makes a nominal week rollover invalidate the Claude source
generation (§3.6). The pure-kernel halves (the typed combined outcome, the
account-scope metadata) live in ``tests/test_dashboard_source_kernel.py``.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from types import SimpleNamespace

from _lib_dashboard_sources import CapabilityRecord, SourceDashboardState
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc

WEEK_START = dt.datetime(2026, 4, 13, 14, 0, 0, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)
NOW = WEEK_START + dt.timedelta(hours=72)

_SOURCE_PATH = "/fake/home/.claude/projects/-fake-repos-demo/session.jsonl"
_SESSION_ID = "556s1cur-0000-0000-0000-000000000001"
_MODEL = "claude-sonnet-4-6"

# (label, timestamp, input, output, cache_create, cache_read, cache_create_1h)
#
# The two out-of-range rows are the oracle: an accumulator bounded by anything
# other than ``[week_start, now]`` picks one of them up, and both carry token
# counts far larger than the in-range rows so the assertion moves loudly.
# ``cache_create_1h`` is a SUBDIVISION of ``cache_create`` (#195), never an
# extra quantity, so the in-range row that carries one proves the #104 total
# does not double-count it.
_ENTRIES: tuple[tuple[str, dt.datetime, int, int, int, int, int], ...] = (
    ("before-week-start", WEEK_START - dt.timedelta(hours=1),
     500_000, 90_000, 0, 0, 0),
    ("in-week-early", WEEK_START + dt.timedelta(hours=1),
     20_000, 3_000, 7_000, 5_000, 4_000),
    ("in-week-late", WEEK_START + dt.timedelta(hours=50),
     13_000, 900, 0, 2_500, 0),
    ("after-now", NOW + dt.timedelta(hours=1),
     700_000, 80_000, 0, 0, 0),
)
_IN_RANGE_LABELS = frozenset({"in-week-early", "in-week-late"})


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _seed_current_week(ns, *, entries=_ENTRIES) -> sqlite3.Connection:
    """Seed one resolvable subscription week plus ``entries`` of Claude spend."""
    from _fixture_builders import (
        seed_session_entry,
        seed_session_file,
        seed_weekly_usage_snapshot,
    )

    stats = ns["open_db"]()
    for hours, pct in ((6, 8.0), (30, 22.0), (72, 40.0)):
        seed_weekly_usage_snapshot(
            stats,
            captured_at_utc=_iso(WEEK_START + dt.timedelta(hours=hours)),
            week_start_date=WEEK_START.date().isoformat(),
            week_end_date=(WEEK_END - dt.timedelta(days=1)).date().isoformat(),
            week_start_at=_iso(WEEK_START),
            week_end_at=_iso(WEEK_END),
            weekly_percent=pct,
        )
    stats.commit()

    cache = ns["open_cache_db"]()
    try:
        seed_session_file(
            cache,
            path=_SOURCE_PATH,
            size_bytes=4096,
            last_byte_offset=len(entries),
            session_id=_SESSION_ID,
            project_path="/fake/repos/demo",
        )
        for offset, (_label, ts, inp, out, cc, cr, cc1h) in enumerate(entries):
            seed_session_entry(
                cache,
                source_path=_SOURCE_PATH,
                line_offset=offset,
                timestamp_utc=_iso(ts),
                model=_MODEL,
                input_tokens=inp,
                output_tokens=out,
                cache_create=cc,
                cache_read=cr,
            )
            if cc1h:
                cache.execute(
                    "UPDATE session_entries SET cache_create_1h_tokens = ? "
                    "WHERE source_path = ? AND line_offset = ?",
                    (cc1h, _SOURCE_PATH, offset),
                )
        cache.commit()
    finally:
        cache.close()
    return stats


def _expected_cost_and_tokens(ns, entries=_ENTRIES) -> tuple[float, int]:
    """The independently-derived oracle for the in-range rows.

    Cost is priced through the production per-entry pricer over a usage dict
    built by ``claude_usage_dict``, so this asserts the accumulated figure
    against the same contract the cache read publishes — without reusing the
    accumulator under test.
    """
    claude_usage_dict = ns["claude_usage_dict"]
    calculate = ns["_calculate_entry_cost"]
    cost = 0.0
    tokens = 0
    for label, _ts, inp, out, cc, cr, cc1h in entries:
        if label not in _IN_RANGE_LABELS:
            continue
        usage = claude_usage_dict(
            input_tokens=inp,
            output_tokens=out,
            cache_creation_tokens=cc,
            cache_read_tokens=cr,
            cache_1h_tokens=cc1h or None,
            speed=None,
        )
        cost += calculate(_MODEL, usage, mode="auto", cost_usd=None)
        tokens += inp + out + cc + cr
    return cost, tokens


def test_current_week_tokens_cover_exactly_the_entries_priced_into_spent_usd(
    tmp_path, monkeypatch,
):
    """§3.3 / acceptance 12 — one accumulation pass, one entry set.

    The published ``total_tokens`` must describe the SAME rows the published
    ``spent_usd`` prices: the two rows inside ``[week_start, now]`` and neither
    of the two outside it.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = _seed_current_week(ns)
    try:
        current_week = ns["_cctally_tui"]._tui_build_current_week(
            stats, NOW, skip_sync=True,
        )
    finally:
        stats.close()

    assert current_week is not None
    expected_cost, expected_tokens = _expected_cost_and_tokens(ns)
    assert current_week.spent_usd == expected_cost
    assert current_week.total_tokens == expected_tokens
    # Non-vacuity: the out-of-range rows are large enough that any wider or
    # narrower bound moves the figure well past this margin.
    assert 0 < current_week.total_tokens < 100_000


def test_current_week_tokens_are_zero_when_the_week_holds_no_entries(
    tmp_path, monkeypatch,
):
    """A resolved week with no accounting publishes a zero counter, not None."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    outside_only = tuple(
        row for row in _ENTRIES if row[0] not in _IN_RANGE_LABELS
    )
    stats = _seed_current_week(ns, entries=outside_only)
    try:
        current_week = ns["_cctally_tui"]._tui_build_current_week(
            stats, NOW, skip_sync=True,
        )
    finally:
        stats.close()

    assert current_week is not None
    assert current_week.spent_usd == 0.0
    assert current_week.total_tokens == 0


def test_legacy_envelope_current_week_carries_the_token_total(
    tmp_path, monkeypatch,
):
    """§3.3 — the counter must reach ``_tui_project_claude_source_data``.

    That projector reads the legacy envelope, never the dataclass, so a field
    that stops at ``TuiCurrentWeek`` never reaches composition.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = _seed_current_week(ns)
    try:
        current_week = ns["_cctally_tui"]._tui_build_current_week(
            stats, NOW, skip_sync=True,
        )
        snapshot = ns["_tui_build_snapshot"](
            now_utc=NOW, skip_sync=True, display_tz_pref_override="utc",
        )
    finally:
        stats.close()

    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=NOW)
    _expected_cost, expected_tokens = _expected_cost_and_tokens(ns)
    assert current_week.total_tokens == expected_tokens
    assert envelope["current_week"]["total_tokens"] == expected_tokens
    # §3.5 / §3.6 — the effective cycle START, beside the already-published
    # end. Composition labels the Claude leg's period from the pair, and the
    # source version detects a rollover from it.
    assert envelope["current_week"]["week_start_at"] == "2026-04-13T14:00:00Z"
    assert envelope["current_week"]["reset_at_utc"] == "2026-04-20T14:00:00Z"


# --- Task 2 — the Claude hero counters are current-cycle (§3.4) -------------


def _legacy_envelope(*, current_week: dict | None) -> dict:
    """A minimal legacy envelope whose daily rollup differs from its cycle."""
    return {
        "daily": {"total_cost_usd": 1704.25, "total_tokens": 987_654_321},
        "monthly": {},
        "weekly": {},
        "sessions": {"rows": ()},
        "projects": {},
        "blocks": {"rows": ()},
        "current_week": current_week,
        "alerts": (),
        "header": {},
        "forecast": None,
        "trend": None,
    }


def test_claude_hero_counters_are_the_current_cycle_not_the_thirty_day_rollup(
    tmp_path, monkeypatch,
):
    """§3.4 — `hero.cost_usd` / `hero.total_tokens` mean current-cycle actuals.

    The daily rollup and the current week are seeded to unequal values, so a
    projector still reading `daily` fails loudly.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    envelope = _legacy_envelope(current_week={
        "spent_usd": 42.5,
        "total_tokens": 1_234_567,
        "used_pct": 40.0,
        "freshness": {"label": "fresh", "age_seconds": 1, "captured_at": _iso(NOW)},
        "milestones": [],
        "five_hour_milestones": [],
    })

    data = ns["_cctally_tui"]._tui_project_claude_source_data(envelope)

    assert data["hero"]["cost_usd"] == 42.5
    assert data["hero"]["total_tokens"] == 1_234_567
    # The thirty-day figure keeps its own home, unchanged.
    assert data["periods"]["daily"]["total_cost_usd"] == 1704.25
    assert data["periods"]["daily"]["total_tokens"] == 987_654_321
    # Everything else on the hero is untouched by this change.
    assert data["hero"]["current_week"]["spent_usd"] == 42.5
    assert set(data["hero"]) == {
        "cost_usd", "total_tokens", "header", "current_week", "forecast", "trend",
    }


def test_claude_hero_counters_are_null_when_no_current_week_resolves(
    tmp_path, monkeypatch,
):
    """§3.7 — an unresolved cycle publishes no counter, never the rollup.

    Composition distinguishes `empty` from `claude_cycle_unresolved` using the
    provider's own availability; a zero here would make both look like spend.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")

    data = ns["_cctally_tui"]._tui_project_claude_source_data(
        _legacy_envelope(current_week=None),
    )

    assert data["hero"]["cost_usd"] is None
    assert data["hero"]["total_tokens"] is None
    assert data["periods"]["daily"]["total_cost_usd"] == 1704.25


# --- Task 6 — the freshness axes are repointed (§4.1) -----------------------


def _claude_source_data(*, week_end: str | None, freshness_label: str) -> dict:
    return {
        "hero": {
            "cost_usd": 12.5,
            "total_tokens": 1_000,
            "current_week": {
                **({"week_start_at": "2026-04-13T14:00:00Z",
                    "reset_at_utc": week_end} if week_end else {}),
                "freshness": {
                    "label": freshness_label, "age_seconds": 900,
                    "captured_at": "2026-04-16T13:45:00Z",
                },
            },
        },
    }


def test_a_stale_claude_percent_observation_stales_quota_and_not_hero(
    tmp_path, monkeypatch,
):
    """§4.1 — `hero` means current-cycle accounting resolvability.

    A percent observation older than the 90-second `stale_after_seconds` bound
    is what `quota` has always described. It says nothing about whether the
    backward-looking counters inside an unexpired week are publishable, and
    staling `hero` on it is what kept All's caveat permanently on.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")

    domains = ns["_cctally_tui"]._tui_claude_domain_freshness(
        _claude_source_data(week_end="2026-04-20T14:00:00Z", freshness_label="stale"),
        now_utc=NOW,
    )

    assert domains == {"hero": "fresh", "quota": "stale", "sessions": "fresh"}


def test_an_expired_claude_week_stales_the_hero_axis(tmp_path, monkeypatch):
    """§4.1 — an expired boundary cannot bound current accounting."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")

    domains = ns["_cctally_tui"]._tui_claude_domain_freshness(
        _claude_source_data(week_end="2026-04-14T14:00:00Z", freshness_label="fresh"),
        now_utc=NOW,
    )

    assert domains == {"hero": "stale", "quota": "fresh", "sessions": "fresh"}


def test_an_unresolvable_claude_week_stales_the_hero_axis(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")

    domains = ns["_cctally_tui"]._tui_claude_domain_freshness(
        _claude_source_data(week_end=None, freshness_label="fresh"),
        now_utc=NOW,
    )

    assert domains["hero"] == "stale"
    assert domains["quota"] == "fresh"


def test_the_claude_idle_clock_advances_quota_but_not_hero(tmp_path, monkeypatch):
    """The idle clock wrote the percent-derived label to BOTH axes (`:2561`)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui = ns["_cctally_tui"]
    state = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-v1",
        last_success_at=NOW,
        capabilities={"hero": CapabilityRecord("supported", "subscription-week")},
        data=_claude_source_data(
            week_end="2026-04-20T14:00:00Z", freshness_label="fresh"),
        domain_freshness={"hero": "fresh", "quota": "fresh", "sessions": "fresh"},
    )
    current_week = SimpleNamespace(
        latest_snapshot_at=NOW - dt.timedelta(seconds=600),
        week_start_at=WEEK_START,
        week_end_at=WEEK_END,
    )

    refreshed = tui._refresh_claude_source_clock(
        state, current_week=current_week, now_utc=NOW, raw_config={},
    )

    assert dict(refreshed.domain_freshness) == {
        "hero": "fresh", "quota": "stale", "sessions": "fresh",
    }


def test_the_claude_idle_clock_stales_the_hero_axis_past_the_week_end(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui = ns["_cctally_tui"]
    state = SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="claude-v1",
        last_success_at=NOW,
        capabilities={"hero": CapabilityRecord("supported", "subscription-week")},
        data=_claude_source_data(
            week_end="2026-04-20T14:00:00Z", freshness_label="fresh"),
        domain_freshness={"hero": "fresh", "quota": "fresh", "sessions": "fresh"},
    )
    current_week = SimpleNamespace(
        latest_snapshot_at=WEEK_END,
        week_start_at=WEEK_START,
        week_end_at=WEEK_END,
    )

    refreshed = tui._refresh_claude_source_clock(
        state, current_week=current_week,
        now_utc=WEEK_END + dt.timedelta(hours=1), raw_config={},
    )

    assert dict(refreshed.domain_freshness)["hero"] == "stale"


# --- Task 7 — the fixture's non-vacuity oracle (§6.2) -----------------------

_FIXTURE_ROOT = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "all-combined"
)
_FIXTURE_AS_OF = "2026-04-16T14:00:00+00:00"


def _fixture_golden() -> dict:
    return json.loads((_FIXTURE_ROOT / "golden-data.json").read_text())


def _fixture_cache() -> sqlite3.Connection:
    path = _FIXTURE_ROOT / ".local" / "share" / "cctally" / "cache.db"
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _claude_tokens_between(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_create_tokens "
        "+ cache_read_tokens), 0) FROM session_entries "
        "WHERE timestamp_utc >= ? AND timestamp_utc <= ?",
        (start, end),
    ).fetchone()
    return int(row[0])


def _codex_tokens_between(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM codex_session_entries "
        "WHERE timestamp_utc >= ? AND timestamp_utc < ?",
        (start, end),
    ).fetchone()
    return int(row[0])


def _iso_utc(value: str) -> str:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).isoformat()


def test_all_combined_fixture_legs_are_bounded_by_their_own_cycles():
    """§6.2 — each directional wrong-bound mutation moves ITS OWN leg.

    The oracle is the prefix window ``[claude.start, codex.start)``, which is
    inside the earlier provider's cycle and outside the later one's. It carries
    TWO provider-specific rows, because Claude and Codex accounting live in
    separate tables read by separate helpers: a Claude row could never be
    picked up by applying Claude's bounds to the Codex leg, so one row catches
    exactly one direction.
    """
    golden = _fixture_golden()
    combined = golden["sources"]["all"]["data"]["combined"]
    claude_period = combined["legs"]["claude"]["period"]
    codex_period = combined["legs"]["codex"]["period"]
    claude_start = _iso_utc(claude_period["start_at"])
    codex_start = _iso_utc(codex_period["start_at"])
    codex_end = _iso_utc(codex_period["end_at"])
    assert claude_start < codex_start, "the prefix window must be non-empty"

    conn = _fixture_cache()
    try:
        claude_own = _claude_tokens_between(conn, claude_start, _FIXTURE_AS_OF)
        claude_wrong = _claude_tokens_between(conn, codex_start, _FIXTURE_AS_OF)
        codex_own = _codex_tokens_between(
            conn, codex_start, min(codex_end, _FIXTURE_AS_OF))
        codex_wrong = _codex_tokens_between(
            conn, claude_start, min(codex_end, _FIXTURE_AS_OF))
    finally:
        conn.close()

    # Each leg equals its OWN bounds and nothing else.
    assert claude_own == combined["legs"]["claude"]["total_tokens"]
    assert codex_own == combined["legs"]["codex"]["total_tokens"]
    # Applying the LATER provider's start to the earlier leg drops the Claude
    # prefix row.
    assert claude_wrong < claude_own
    # Applying the EARLIER provider's start to the later leg picks up the Codex
    # out-of-cycle sentinel.
    assert codex_wrong > codex_own
    # A swapped leg, a sign error or a unit change also moves the golden.
    assert claude_own != codex_own
    assert combined["legs"]["claude"]["cost_usd"] != combined["legs"]["codex"]["cost_usd"]
    assert combined["total_tokens"] == claude_own + codex_own


def test_all_combined_fixture_pins_the_state_that_kept_the_caveat_on():
    """§6.1 / acceptance 5 — both percent clocks stale, both cycles resolvable.

    The Codex weekly observation is 3601 seconds old against a 3600-second
    bound and the Claude percent capture is 600 seconds old against a
    90-second one, yet both cycles resolve — so the figure publishes with no
    staleness marker anywhere.
    """
    golden = _fixture_golden()
    all_source = golden["sources"]["all"]

    assert all_source["domain_freshness"] == {
        "hero": "fresh", "quota": "stale", "sessions": "fresh",
    }
    assert all_source["warnings"] == []
    assert "qualifications" not in all_source["data"]["combined"]
    assert golden["sources"]["codex"]["data"]["hero"]["cycle_freshness"] == "stale"
    # The harness sentinelizes the LEGACY top-level `current_week.freshness`,
    # not the projected copy under the source bundle, so this is the real,
    # `CCTALLY_AS_OF`-pinned value.
    claude_freshness = golden["sources"]["claude"]["data"]["hero"][
        "current_week"]["freshness"]
    assert claude_freshness["label"] == "stale"
    assert claude_freshness["age_seconds"] == 600
    assert golden["sources"]["claude"]["domain_freshness"] == {
        "hero": "fresh", "quota": "stale", "sessions": "fresh",
    }
    assert golden["sources"]["codex"]["domain_freshness"] == {
        "hero": "fresh", "quota": "stale", "sessions": "fresh",
    }


def test_all_combined_decorated_fixture_withholds_with_a_named_reason():
    """§3.2 / acceptance 3 — decoration names the provider and its count."""
    golden = json.loads(
        (_FIXTURE_ROOT.parent / "all-combined-decorated" / "golden-data.json")
        .read_text()
    )
    data = golden["sources"]["all"]["data"]

    assert data["combined"] is None
    assert data["combined_unavailable"]["code"] == "multi_account_unsupported"
    assert data["combined_unavailable"]["causes"] == [{
        "provider": "claude",
        "code": "multi_account_unsupported",
        "detail": {"account_count": 2},
    }]
    # No account CARDINALITY leaks into the provider envelopes themselves.
    assert "account_scope" not in golden["sources"]["claude"]
    assert "account_scope" not in golden["sources"]["codex"]


def test_a_decorated_provider_whose_account_read_failed_still_withholds(
    tmp_path, monkeypatch,
):
    """§3.8 / acceptance 13 — a FAILED read is unresolved, never undecorated.

    The other half of acceptance 13 (metadata simply absent) is the kernel
    matrix row in ``tests/test_dashboard_source_kernel.py``. This half exercises
    the real builder against a genuinely DECORATED install whose count read
    raises, which is the state §3.8 exists for: both physical builders swallow a
    decoration-read failure and fall back to the undecorated wire, so a fallback
    here would publish exactly the figure decoration forbids, on exactly the
    install where it is wrong.

    Both providers' reads fail, so both causes are listed and Claude — first at
    equal precedence — is the winner.
    """
    from _fixture_builders import seed_account

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    # AFTER `load_script`, never before: it drops cached `_cctally_*` siblings
    # from `sys.modules`, so a module imported earlier is a stale instance the
    # builder's own deferred import would not resolve to.
    import _cctally_account

    stats = ns["open_db"]()
    try:
        for key, email, label in (
            ("a" * 32, "work@example.com", "work"),
            ("b" * 32, "home@example.com", "home"),
        ):
            seed_account(
                stats,
                account_key=key,
                provider="claude",
                natural_id=f"uuid-{label}",
                email=email,
                label=label,
                plan_type="max",
                label_source="user",
                first_seen_utc=_iso(WEEK_START),
                last_seen_utc=_iso(NOW),
            )
        stats.commit()
        # Sanity: without the failure this install really is decorated, so the
        # assertion below cannot be satisfied by an accidentally-empty registry.
        assert _cctally_account.real_account_count(stats, "claude") == 2

        def _raise(*_args, **_kwargs):
            raise sqlite3.OperationalError("no such table: accounts")

        monkeypatch.setattr(_cctally_account, "real_account_count", _raise)
        bundle = ns["_cctally_tui"]._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=NOW,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=0.0,
            claude_total_tokens=0,
        )
    finally:
        stats.close()

    for provider in ("claude", "codex"):
        assert bundle.sources[provider].account_scope is None, provider
    data = bundle.sources["all"].data
    assert data["combined"] is None
    unavailable = data["combined_unavailable"]
    assert unavailable["code"] == "account_scope_unresolved"
    assert [cause["code"] for cause in unavailable["causes"]] == [
        "account_scope_unresolved", "account_scope_unresolved",
    ]
    assert [cause["provider"] for cause in unavailable["causes"]] == [
        "claude", "codex",
    ]


def test_an_unimportable_account_module_withholds_rather_than_crashing(
    monkeypatch,
):
    """§3.8 — the deferred import is a different failure class from the query.

    ``ImportError`` must reach the same fail-closed ``None``. Letting it
    propagate would fail the whole dashboard tick instead of withholding one
    figure. ``None`` in ``sys.modules`` is the documented way to make an
    ``import`` statement raise ``ImportError`` without touching ``builtins``.
    """
    import sys

    import _cctally_tui

    monkeypatch.setitem(sys.modules, "_cctally_account", None)

    assert _cctally_tui._tui_resolve_account_scope(None, "claude") is None
