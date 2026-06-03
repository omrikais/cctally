"""Firing + reconcile tests for the PER-PROJECT budget alert path (Task 3,
spec §6 / §6.8 / §5.3).

Exercises ``maybe_record_project_budget_milestone`` (record-usage firing) and
``_reconcile_project_budget_milestones_on_write`` (forward-only-from-write)
against a redirected tmp stats.db + a seeded ``weekly_usage_snapshots`` window
anchor.

Per-project spend is injected via a monkeypatched ``_sum_cost_by_project`` so
the crossing arithmetic is deterministic and isolated from the cache-DB ingest
path (that path's correctness is locked by the F3/F-PROJ reconcile invariants in
``bin/cctally-reconcile-test``). Dispatch is captured via a fake
``_dispatch_alert_notification`` so no osascript is spawned — exactly the seam
``tests/test_budget_alerts.py`` uses for the global budget axis.

Covered cases (mirrors the global budget firing tests, scaled to a project
dimension):
  (a) project ``/a`` over thresholds 90 + 100 writes exactly {(/a,90),(/a,100)}
      with alerted_at set + dispatches both; a SECOND run is a no-op (pre-probe);
  (b) ``project_alerts_enabled=False`` / empty projects / empty thresholds →
      0 rows, no scan, no dispatch (gate-first);
  (c) a NON-configured project's spend never writes a row;
  (d) NON-VACUITY of the pre-probe skip: all pairs recorded → no scan;
  (e) forward-only reconcile-on-write records already-crossed pairs alerted_at
      WITHOUT dispatch; a later record-usage does not re-dispatch; a mid-week
      target change never re-stamps;
  (f) alert text + test-alert surface (project-specific, not generic fallback).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import pytest

from conftest import load_script, redirect_paths


# Subscription-week window the snapshot anchors. Tuesday 14:00 UTC, 7 days.
WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END = WEEK_START + dt.timedelta(days=7)
# now_utc placed mid-week (~4 days in) so elapsed/remaining are well-defined.
AS_OF = WEEK_START + dt.timedelta(hours=96)

PROJ_A = "/fake/repos/a"
PROJ_B = "/fake/repos/b"
PROJ_UNCONF = "/fake/repos/other"


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_week_key():
    """The exact ``week_start_at`` key the production code writes.

    ``_resolve_current_budget_window`` runs the seeded ISO timestamp through
    ``parse_iso_datetime`` (host-local datetime) then ``isoformat(timespec=
    "seconds")`` — so the stored key carries the host's UTC offset. Mirror that
    derivation here so the test is host-TZ-agnostic.
    """
    return dt.datetime.fromisoformat(
        _iso(WEEK_START).replace("Z", "+00:00")
    ).astimezone().isoformat(timespec="seconds")


WEEK_KEY = _expected_week_key()


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(AS_OF))
    return ns


def _seed_window(ns):
    """Seed one boundary-aware weekly_usage_snapshots row so
    ``_resolve_current_budget_window`` resolves the [WEEK_START, WEEK_END)
    window. The percent is irrelevant to budget spend."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " page_url, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(WEEK_START + dt.timedelta(hours=1)),
                WEEK_START.date().isoformat(),
                (WEEK_END - dt.timedelta(seconds=1)).date().isoformat(),
                _iso(WEEK_START),
                _iso(WEEK_END),
                40.0,
                None,
                "fixture",
                json.dumps({"fixture": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_config(ns, *, projects, project_alerts_enabled=True,
                  thresholds=(90, 100), weekly_usd=None):
    """Write a config.json carrying the budget block at the redirected
    CONFIG_PATH so ``load_config`` reads it. ``weekly_usd`` is left unset by
    default — per-project alerts are independent of a global budget."""
    import _cctally_core
    block = {
        "project_alerts_enabled": project_alerts_enabled,
        "alert_thresholds": list(thresholds),
        "projects": dict(projects),
    }
    if weekly_usd is not None:
        block["weekly_usd"] = weekly_usd
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": block}) + "\n"
    )


def _patch_spend(ns, monkeypatch, *, by_proj=None, spy=None):
    """Inject a deterministic ``_sum_cost_by_project`` on the cctally namespace
    (resolved at call time by the record-sibling shim). ``by_proj`` is the
    ``{git_root: usd}`` map returned; ``spy`` is an optional list recording each
    call's (start, end, mode, skip_sync) for non-vacuity proofs."""
    if by_proj is None:
        by_proj = {}

    def fake_sum(start, now, mode="auto", skip_sync=False):
        if spy is not None:
            spy.append((start, now, mode, skip_sync))
        return dict(by_proj)
    monkeypatch.setitem(ns, "_sum_cost_by_project", fake_sum)


def _patch_dispatch(ns, monkeypatch):
    """Capture dispatched payloads instead of spawning osascript."""
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append((payload, mode))
        return "queued"
    monkeypatch.setitem(ns, "_dispatch_alert_notification", fake_dispatch)
    return captured


def _rows(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT week_start_at, project_key, threshold, budget_usd, "
            "       spent_usd, consumption_pct, alerted_at "
            "FROM project_budget_milestones "
            "ORDER BY project_key, threshold"
        ).fetchall()
    finally:
        conn.close()


def _pairs(rows):
    return [(r["project_key"], r["threshold"]) for r in rows]


# ── (a) /a over 90+100 writes both pairs + dispatches; rerun is a no-op ──────


def test_crossing_records_rows_and_dispatches(ns, monkeypatch):
    _seed_window(ns)
    # /a budget $25, spent $26 → 104% → crosses 90 AND 100. /b under budget.
    _write_config(ns, projects={PROJ_A: 25.0, PROJ_B: 100.0},
                  thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0, PROJ_B: 10.0})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    rows = _rows(ns)
    assert _pairs(rows) == [(PROJ_A, 90), (PROJ_A, 100)]
    assert all(r["alerted_at"] is not None for r in rows)
    assert all(r["week_start_at"] == WEEK_KEY for r in rows)
    assert all(abs(r["budget_usd"] - 25.0) < 1e-9 for r in rows)
    assert all(abs(r["spent_usd"] - 26.0) < 1e-9 for r in rows)
    # Both crossings dispatched, mode=real, axis=project_budget.
    assert {(p["context"]["project_key"], p["threshold"]) for p, _ in captured} == {
        (PROJ_A, 90), (PROJ_A, 100),
    }
    assert all(p["axis"] == "project_budget" for p, _ in captured)
    assert all(mode == "real" for _, mode in captured)
    # Notification label is collision-aware, byte-matching the display
    # (`_project_disambiguate_labels`, spec §5.3): PROJ_A's basename `a` is
    # UNIQUE among the configured set ({a, b}), so the label is the bare
    # basename `a` — NOT over-qualified with the parent dir. The collision
    # path (which DOES append `(parent)`) is proved by
    # `test_crossing_label_disambiguates_same_basename_roots` below.
    assert all(
        p["context"]["project"] == "a" for p, _ in captured
    )


def test_crossing_label_disambiguates_same_basename_roots(ns, monkeypatch):
    """Two configured roots sharing a basename (`/fake/work/app` +
    `/fake/personal/app`) BOTH cross — the notifications must carry distinct,
    parent-dir-disambiguated labels (`app (work)` / `app (personal)`), proving
    the collision path still qualifies the label exactly as the display does
    (`_project_disambiguate_labels`, spec §5.3). The companion
    `test_crossing_records_rows_and_dispatches` proves a uniquely-named project
    keeps its BARE basename — together they pin both branches of the map."""
    _seed_window(ns)
    work_app = "/fake/work/app"
    personal_app = "/fake/personal/app"
    _write_config(ns, projects={work_app: 25.0, personal_app: 25.0},
                  thresholds=(100,))
    # Both over budget → both cross 100.
    _patch_spend(ns, monkeypatch,
                 by_proj={work_app: 26.0, personal_app: 30.0})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    # One crossing per root, each carrying its project_key + the disambiguated
    # label keyed off the PARENT dir (collision-only — the bare basename `app`
    # would be ambiguous across the two roots).
    label_by_key = {
        p["context"]["project_key"]: p["context"]["project"]
        for p, _ in captured
    }
    assert label_by_key == {
        work_app: "app (work)",
        personal_app: "app (personal)",
    }


def test_fire_once_second_run_is_noop(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})
    assert len(captured) == 2
    rows_after_first = _rows(ns)

    ns["maybe_record_project_budget_milestone"]({})
    assert len(captured) == 2  # no re-dispatch
    assert len(_rows(ns)) == len(rows_after_first) == 2  # no new rows


# ── (b) gating: disabled / empty projects / empty thresholds → nothing ──────


def test_project_alerts_disabled_does_nothing(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, project_alerts_enabled=False)
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _rows(ns) == []
    assert captured == []
    assert spy == []  # gate returns BEFORE the scan (zero overhead)


def test_empty_projects_does_nothing(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={}, project_alerts_enabled=True)
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _rows(ns) == []
    assert captured == []
    assert spy == []


def test_empty_thresholds_does_nothing(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=())
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _rows(ns) == []
    assert captured == []
    assert spy == []


# ── (c) a non-configured project's spend never writes a row ─────────────────


def test_nonconfigured_project_spend_never_writes(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    # PROJ_UNCONF is massively over but NOT in budget.projects; PROJ_A under.
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 5.0, PROJ_UNCONF: 9999.0})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    # PROJ_A at $5/$25 = 20% → crosses nothing; PROJ_UNCONF unconfigured → no row.
    assert _rows(ns) == []
    assert captured == []


def test_only_over_project_writes(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0, PROJ_B: 25.0}, thresholds=(90,))
    # /a over (104%), /b under (40%).
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0, PROJ_B: 10.0})
    _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _pairs(_rows(ns)) == [(PROJ_A, 90)]


# ── (d) NON-VACUITY: pre-probe skips the scan iff all pairs recorded ────────


def test_preprobe_skips_scan_when_all_recorded(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))

    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    _patch_dispatch(ns, monkeypatch)
    ns["maybe_record_project_budget_milestone"]({})
    assert len(spy) == 1  # scan ran once (work was owed)
    assert len(_rows(ns)) == 2

    spy.clear()
    ns["maybe_record_project_budget_milestone"]({})
    assert spy == []  # the scan was skipped (non-vacuous optimization)


def test_preprobe_does_not_skip_when_one_pair_pending(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))

    # Seed ONLY (/a, 90) (partial prior run / forward-only set).
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO project_budget_milestones "
            "(week_start_at, project_key, threshold, budget_usd, spent_usd, "
            " consumption_pct, crossed_at_utc, alerted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (WEEK_KEY, PROJ_A, 90, 25.0, 23.0, 92.0, _iso(AS_OF), _iso(AS_OF)),
        )
        conn.commit()
    finally:
        conn.close()

    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    # (/a,100) pending → scan ran, 100 recorded + dispatched; 90 untouched.
    assert len(spy) == 1
    assert _pairs(_rows(ns)) == [(PROJ_A, 90), (PROJ_A, 100)]
    assert [p["threshold"] for p, _ in captured] == [100]


# ── snap-up: a 89.9999999% consumption counts as crossing 90 ────────────────


def test_snap_up_crosses_threshold(ns, monkeypatch):
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 30.0}, thresholds=(90,))
    # 26.9999999999 / 30 * 100 == 89.99999... — +1e-9 must snap it to >= 90.
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.9999999999})
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _pairs(_rows(ns)) == [(PROJ_A, 90)]
    assert [p["threshold"] for p, _ in captured] == [90]


# ── lazy label resolution: only resolve when a crossing actually dispatches ──


def _spy_labels(ns, monkeypatch):
    """Count calls into the shared ``_project_budget_labels`` primitive — the
    firing path resolves it LAZILY (#130 follow-up), only on a genuine
    crossing. Delegates to the real impl so dispatched labels stay correct."""
    calls: list = []
    real = ns["_project_budget_labels"]

    def spy(keys):
        calls.append(list(keys))
        return real(keys)
    monkeypatch.setitem(ns, "_project_budget_labels", spy)
    return calls


def test_no_crossing_skips_label_resolution(ns, monkeypatch):
    # A pending-but-uncrossed tick (spend below every threshold) still SCANS,
    # but must NOT resolve labels — the per-key git-root work is wasted when no
    # crossing dispatches. Pins the lazy placement: the OLD eager call resolved
    # labels here unconditionally, so `label_calls == []` is non-vacuous.
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    scan_spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 10.0}, spy=scan_spy)  # 40%
    label_calls = _spy_labels(ns, monkeypatch)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _rows(ns) == []      # nothing crossed
    assert captured == []       # nothing dispatched
    assert scan_spy != []       # but the cost scan DID run (not an early bail)
    assert label_calls == []    # …and labels were NOT resolved


def test_label_resolved_once_across_multiple_crossings(ns, monkeypatch):
    # Two thresholds cross on the same tick (90 AND 100). Labels resolve lazily
    # on the FIRST dispatch and are reused for the second — exactly ONE
    # resolution (guards against re-resolving per-dispatch inside the loop).
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0})  # 104% → 90 + 100
    label_calls = _spy_labels(ns, monkeypatch)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})

    assert _pairs(_rows(ns)) == [(PROJ_A, 90), (PROJ_A, 100)]  # both crossed
    assert len(captured) == 2                                  # both dispatched
    assert len(label_calls) == 1                               # resolved once, reused


# ── malformed budget config is a quiet warn-once no-op (hot-path safety) ─────


def test_malformed_budget_config_is_quiet_noop(ns, monkeypatch, capsys):
    _seed_window(ns)
    import _cctally_core
    # A negative project budget fails _get_budget_config -> _BudgetConfigError.
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {
            "project_alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projects": {PROJ_A: -5.0},
        }}) + "\n"
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)
    captured = _patch_dispatch(ns, monkeypatch)

    ns["maybe_record_project_budget_milestone"]({})  # must not raise

    assert _rows(ns) == []
    assert captured == []
    assert spy == []  # returned at the config gate, before the scan
    assert "[budget] invalid config" in capsys.readouterr().err


# ── (e) forward-only reconcile-on-write: record without dispatch ────────────


def _reconcile(ns, *, projects, thresholds=(90, 100),
               project_alerts_enabled=True):
    """Run the reconcile via the validated-budget shape the write paths pass."""
    validated = {
        "projects": dict(projects),
        "project_alerts_enabled": project_alerts_enabled,
        "alert_thresholds": list(thresholds),
    }
    ns["_reconcile_project_budget_milestones_on_write"](validated)


def test_reconcile_records_without_dispatch_then_later_fires(ns, monkeypatch):
    _seed_window(ns)
    captured = _patch_dispatch(ns, monkeypatch)

    # `budget set 25 --project /a` reconcile at $24 (96%): 90 already crossed →
    # record with alerted_at SET but NO dispatch; 100 not crossed → no row.
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 24.0})
    _reconcile(ns, projects={PROJ_A: 25.0})

    rows = _rows(ns)
    assert _pairs(rows) == [(PROJ_A, 90)]
    assert rows[0]["alerted_at"] is not None  # recorded
    assert captured == []  # but NOT dispatched (no instant popup)

    # Later record-usage tick at $26 (104%): 90 already a row (skip), 100 pending
    # → fires ONLY (/a, 100).
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0})
    ns["maybe_record_project_budget_milestone"]({})

    rows = _rows(ns)
    assert _pairs(rows) == [(PROJ_A, 90), (PROJ_A, 100)]
    assert [p["threshold"] for p, _ in captured] == [100]


def test_reconcile_target_change_does_not_restamp(ns, monkeypatch):
    """A mid-week target change re-runs the reconcile; UNIQUE(week, project,
    threshold) + the `alerted_at IS NULL` UPDATE guard keep it idempotent —
    no duplicate row, no re-stamp, never a dispatch."""
    _seed_window(ns)
    captured = _patch_dispatch(ns, monkeypatch)
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 24.0})  # 96% of $25

    _reconcile(ns, projects={PROJ_A: 25.0})
    rows_first = _rows(ns)
    assert _pairs(rows_first) == [(PROJ_A, 90)]
    stamp_first = rows_first[0]["alerted_at"]

    # Mid-week target raise to $30 → $24/$30 = 80% (90 NOT crossed at new
    # target), but the already-alerted (/a,90) row must stay deduped + unchanged.
    _reconcile(ns, projects={PROJ_A: 30.0})
    rows_second = _rows(ns)
    assert _pairs(rows_second) == [(PROJ_A, 90)]  # no duplicate
    assert rows_second[0]["alerted_at"] == stamp_first  # not re-stamped
    assert captured == []  # reconcile never dispatches


def test_reconcile_gate_off_records_nothing(ns, monkeypatch):
    _seed_window(ns)
    captured = _patch_dispatch(ns, monkeypatch)
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0}, spy=spy)

    _reconcile(ns, projects={PROJ_A: 25.0}, project_alerts_enabled=False)

    assert _rows(ns) == []
    assert captured == []
    assert spy == []  # gate returns before the scan


def test_config_set_project_alerts_enabled_reconciles(ns, monkeypatch):
    """`config set budget.project_alerts_enabled true` while already over
    records the crossed (project, threshold) as already-alerted (no dispatch);
    the later record-usage tick does not re-fire it."""
    _seed_window(ns)
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {
            "alert_thresholds": [90, 100],
            "projects": {PROJ_A: 25.0},
        }}) + "\n"
    )
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 24.0})  # 96%
    captured = _patch_dispatch(ns, monkeypatch)

    args = argparse.Namespace(
        key="budget.project_alerts_enabled", value="true", emit_json=False,
    )
    rc = ns["_cmd_config_set"](args)
    assert rc == 0

    rows = _rows(ns)
    assert _pairs(rows) == [(PROJ_A, 90)]
    assert rows[0]["alerted_at"] is not None
    assert captured == []  # config set did NOT instant-popup

    # Later record-usage at 104% fires ONLY the still-pending 100.
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0})
    ns["maybe_record_project_budget_milestone"]({})
    rows = _rows(ns)
    assert _pairs(rows) == [(PROJ_A, 90), (PROJ_A, 100)]
    assert [p["threshold"] for p, _ in captured] == [100]


# ── (g) check-review P2 regressions: dedup self-heal · gating · malformed ────


def _snapshot_count(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()


def test_dedup_tick_self_heals_project_budget(ns, monkeypatch):
    """A record-usage tick that DEDUPS (weekly + 5h percent unchanged from the
    latest row) must STILL fire a per-project $ crossing — USD spend is
    decoupled from the Anthropic percent. Pre-fix the dedup branch returned
    before the project-budget axis, so the alert never fired until the next
    percent change. [Dedup mustn't gate side effects]"""
    # _seed_window writes one row at weekly_percent=40, five_hour_percent=NULL —
    # it is BOTH the budget-window anchor AND the row the dedup compares to.
    _seed_window(ns)
    _write_config(ns, projects={PROJ_A: 25.0}, thresholds=(90, 100))
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0})  # 104% → 90 + 100
    captured = _patch_dispatch(ns, monkeypatch)

    before = _snapshot_count(ns)
    # Same weekly_percent (40.0) + no 5h → should_insert is False (dedup).
    args = argparse.Namespace(
        percent=40.0,
        resets_at=int(WEEK_END.timestamp()),
        five_hour_percent=None,
        five_hour_resets_at=None,
    )
    rc = ns["cmd_record_usage"](args)
    assert rc == 0
    assert _snapshot_count(ns) == before  # dedup swallowed the snapshot
    # ...yet the project-budget axis still fired via the dedup self-heal loop.
    assert _pairs(_rows(ns)) == [(PROJ_A, 90), (PROJ_A, 100)]
    assert {p["threshold"] for p, _ in captured} == {90, 100}


def test_config_set_unrelated_budget_key_does_not_reconcile_projects(
    ns, monkeypatch
):
    """`config set budget.weekly_usd` (a GLOBAL-axis key) must NOT run the
    per-project reconcile. Otherwise a currently-over-but-not-yet-dispatched
    project threshold gets latched as already-alerted, permanently suppressing
    the next record-usage tick's dispatch. Pre-fix the reconcile ran on EVERY
    budget.* write."""
    _seed_window(ns)
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {
            "project_alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projects": {PROJ_A: 25.0},
        }}) + "\n"
    )
    spy: list = []
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 24.0}, spy=spy)  # 96%, > 90
    captured = _patch_dispatch(ns, monkeypatch)

    args = argparse.Namespace(
        key="budget.weekly_usd", value="50", emit_json=False,
    )
    assert ns["_cmd_config_set"](args) == 0
    assert spy == []        # project reconcile gated out → no scan
    assert _rows(ns) == []  # nothing latched in the per-project ledger
    assert captured == []

    # The crossing is still pending → a real record-usage tick fires (/a, 90).
    ns["maybe_record_project_budget_milestone"]({})
    assert _pairs(_rows(ns)) == [(PROJ_A, 90)]
    assert [p["threshold"] for p, _ in captured] == [90]


def test_budget_set_project_malformed_map_is_controlled_error(ns, capsys):
    """A hand-edited non-dict `budget.projects` yields a controlled exit 2 (not
    a `dict(...)` traceback) — the guard runs before _get_budget_config."""
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {"projects": "garbage"}}) + "\n"
    )
    args = argparse.Namespace(amount="25", project="/tmp/foo-repo", json=False)
    assert ns["_cmd_budget_set_project"](args) == 2
    assert "budget.projects must be an object" in capsys.readouterr().err


def test_budget_unset_project_malformed_map_is_controlled_error(ns, capsys):
    """The unset path carries the same guard (non-pair list → exit 2)."""
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": {"projects": ["not", "pairs"]}}) + "\n"
    )
    args = argparse.Namespace(project="/tmp/foo-repo", json=False)
    assert ns["_cmd_budget_unset_project"](args) == 2
    assert "budget.projects must be an object" in capsys.readouterr().err


def test_single_project_set_does_not_latch_sibling(ns, monkeypatch):
    """`budget set --project A` must reconcile ONLY A. A sibling B that is
    already over a threshold but whose dispatch tick has not run must NOT be
    latched as already-alerted — that would permanently suppress B's real
    alert. Regression for the [check-review P2] reconcile over-reach (the
    helper looped over EVERY configured project, not just the touched one)."""
    _seed_window(ns)
    import _cctally_core
    # Start with ONLY B configured + alerts on. B is over 100 (116%), pending
    # (no record-usage tick has dispatched it yet → no row).
    _cctally_core.CONFIG_PATH.write_text(json.dumps({"budget": {
        "project_alerts_enabled": True,
        "alert_thresholds": [100],
        "projects": {PROJ_B: 30.0},
    }}) + "\n")
    # A is over the $25 it's about to be set to; B is over its $30 (35/30=116%).
    _patch_spend(ns, monkeypatch, by_proj={PROJ_A: 26.0, PROJ_B: 35.0})
    captured = _patch_dispatch(ns, monkeypatch)

    # `budget set 25 --project=/fake/repos/a` touches ONLY A.
    args = argparse.Namespace(amount="25", project=PROJ_A, json=False)
    assert ns["_cmd_budget_set_project"](args) == 0

    pairs = _pairs(_rows(ns))
    assert (PROJ_A, 100) in pairs       # A latched (just set, already over)...
    assert (PROJ_B, 100) not in pairs   # ...but B NOT suppressed by A's write
    assert captured == []               # reconcile never dispatches

    # B's real crossing still fires on the next record-usage tick.
    ns["maybe_record_project_budget_milestone"]({})
    assert (PROJ_B, 100) in _pairs(_rows(ns))
    assert [p["context"]["project_key"] for p, _ in captured] == [PROJ_B]


# ── (f) alert text + test-alert surface ─────────────────────────────────────


def test_alert_text_is_project_specific(ns):
    """`_alert_text_project_budget` renders project-specific text containing the
    basename + $spent/$budget — NOT the generic axis=... fallback."""
    payload = ns["_build_alert_payload_project_budget"](
        threshold=100,
        crossed_at_utc=_iso(AS_OF),
        week_start_at=WEEK_KEY,
        project="example-project",
        project_key=PROJ_A,
        budget_usd=25.0,
        spent_usd=26.0,
        consumption_pct=104.0,
    )
    assert payload["axis"] == "project_budget"
    assert payload["context"]["project"] == "example-project"
    assert payload["context"]["project_key"] == PROJ_A
    title, subtitle, body = ns["_alert_text_project_budget"](payload, None)
    blob = " ".join([title, subtitle, body])
    assert "example-project" in blob
    assert "axis=" not in blob  # not the generic fallback
    assert "$26" in body and "$25" in body


def test_dispatch_routes_project_budget_to_text(ns, monkeypatch):
    """`_dispatch_alert_notification` selects `_alert_text_project_budget` for
    the project_budget axis (not the generic fallback). Proven by capturing the
    rendered command args via an injected popen_factory."""
    seen = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            seen["args"] = args

    payload = ns["_build_alert_payload_project_budget"](
        threshold=100,
        crossed_at_utc=_iso(AS_OF),
        week_start_at=WEEK_KEY,
        project="example-project",
        project_key=PROJ_A,
        budget_usd=25.0,
        spent_usd=26.0,
        consumption_pct=104.0,
    )
    # Force a notifier that builds a visible arg-list regardless of host OS.
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"alerts": {
            "notifier": "command",
            "command_template": ["echo", "{title}", "{subtitle}", "{body}"],
        }}) + "\n"
    )
    status = ns["_dispatch_alert_notification"](
        payload, popen_factory=_FakePopen, mode="test",
    )
    assert status == "queued"
    blob = " ".join(str(a) for a in seen["args"])
    assert "example-project" in blob
    assert "axis=" not in blob


def test_alerts_test_cli_project_budget(ns, monkeypatch, capsys):
    """`cctally alerts test --axis project-budget` exits 0 dispatching a
    synthetic example — no real budget.projects entry required.

    ``cmd_alerts_test`` lives in ``_cctally_alerts`` and calls the module-global
    ``_dispatch_alert_notification`` directly (not via the cctally-namespace
    shim the record path uses), so capture is wired at the module level."""
    import _cctally_alerts
    captured = []

    def fake_dispatch(payload, *, mode="real", **kwargs):
        captured.append((payload, mode))
        return "queued"
    monkeypatch.setattr(_cctally_alerts, "_dispatch_alert_notification", fake_dispatch)

    args = argparse.Namespace(
        axis="project-budget", threshold=100, metric="weekly_pct",
    )
    rc = ns["cmd_alerts_test"](args)
    assert rc == 0
    assert len(captured) == 1
    payload, mode = captured[0]
    assert payload["axis"] == "project_budget"
    assert mode == "test"
    # Synthetic example: project name present, threshold from --threshold.
    assert payload["context"]["project"]
    assert payload["threshold"] == 100
