"""Dashboard envelope serialization (#279 S5 F2, wide scope).

Consumer-only sibling of ``bin/_cctally_dashboard.py`` — it re-imports every
name below, so ``bin/cctally``'s re-exports and the direct
``sys.modules["_cctally_dashboard"].X`` reaches (TUI accessor shims
``bin/_cctally_tui.py:373-378``, the pytest sites) keep resolving unchanged
(spec §2 re-export continuity).

What lives here (spec §4): the ``DataSnapshot`` → JSON-envelope serializer
call-tree — ``snapshot_to_envelope`` + ``_session_detail_to_envelope`` +
``_iso_z`` + ``_compute_intensity_buckets`` — plus the sync-thread-called
"envelope builders" satellites the dashboard docstring groups with it:
``_select_current_block_for_envelope``, ``_model_breakdowns_to_models``,
the five ``_envelope_rows_*`` + ``_ENVELOPE_AXIS_MAPPERS`` +
``_build_alerts_envelope_array``. The positional-``DataSnapshot``
``getattr(snap, …, None)`` / legacy-fallback branches inside
``snapshot_to_envelope`` are load-bearing (spec §1) — moved verbatim.

``DataSnapshot`` / ``TuiSessionDetail`` / ``DailyPanelRow`` stay in
``bin/cctally`` (the TUI vertical); this module only CONSUMES instances
(all references are in string annotations under ``from __future__ import
annotations``).

Cross-module reaches (spec §2 "fully-qualify cross-module refs"): the
cctally-forwarding accessor shims the moved code called by bare name
(``load_config``, ``_apply_display_tz_override``, ``_freshness_label``,
``_build_forecast_json_payload``, ``_warn_alerts_bad_config_once``,
``doctor_gather_state``, ``_load_update_state``, ``_load_update_suppress``,
and ``c = _cctally()``) are inlined to their ``sys.modules["cctally"].X``
call-time reach — identical behavior, and the ``ns["X"]`` cctally-namespace
patch surface is preserved (none is patched on the dashboard module object;
audited). ``_channel_env_fragment`` STAYS in the dashboard (grouped with the
port/startup wiring), so it is reached at call time via
``sys.modules["_cctally_dashboard"]``. ``_cache_report_snapshot_to_dict`` is
an honest import from ``_cctally_dashboard_cache_report`` (single source,
spec §4 / gate P2-2 — no duplicated function, just the bootstrap helper).
"""
from __future__ import annotations

import bisect
import datetime as dt
import importlib.util as _ilu
import os
import sys
from zoneinfo import ZoneInfo

from _cctally_core import (
    _AlertsConfigError,
    _BudgetConfigError,
    _budget_alerts_active,
    _get_alerts_config,
    _get_budget_config,
)
from _lib_display_tz import _compute_display_block, format_display_dt
from _lib_pricing import _chip_for_model, _short_model_name


def _ensure_sibling_loaded(name: str) -> None:
    """Register a NON-eager-loaded ``_cctally_dashboard_*`` sibling in ``sys.modules``.

    Bootstrap-only copy (spec §4 / gate P2-2 — the FUNCTION
    ``_cache_report_snapshot_to_dict`` stays single-source in the
    cache-report sibling; only this helper is duplicated). Mirrors
    ``bin/_cctally_record.py:_ensure_sibling_loaded`` so the honest import
    below resolves under the ``SourceFileLoader`` harness path too.
    """
    if name in sys.modules:
        return
    try:
        __import__(name)  # bin/ on sys.path: prod script / conftest / pytest
        return
    except ModuleNotFoundError:
        pass
    _p = os.path.join(os.path.dirname(__file__), f"{name}.py")
    _spec = _ilu.spec_from_file_location(name, _p)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules[name] = _mod
    _spec.loader.exec_module(_mod)


# Deliberate envelope → cache-report load edge (single source; no cycle,
# cache-report never references envelope). Bootstrapped, then honest-imported.
_ensure_sibling_loaded("_cctally_dashboard_cache_report")
from _cctally_dashboard_cache_report import _cache_report_snapshot_to_dict

# #279 S6 W4: the canonical None-safe UTC-Z serializer (its own former local
# copy was already exactly this — collapse to the single definition).
_ensure_sibling_loaded("_lib_json_envelope")
from _lib_json_envelope import _iso_z


def _model_breakdowns_to_models(model_breakdowns: list[dict[str, Any]],
                                period_cost: float) -> list[dict[str, Any]]:
    """Reshape a BucketUsage.model_breakdowns list (sorted desc by cost)
    into the dashboard envelope's `models` shape: short display name,
    chip key, and cost percentage of the period."""
    out: list[dict[str, Any]] = []
    for mb in model_breakdowns:
        canonical = mb["modelName"]
        cost = float(mb["cost"])
        pct = (cost / period_cost * 100.0) if period_cost > 0 else 0.0
        # Display: reuse the canonical short-name helper (strips "claude-"
        # prefix and any trailing "-YYYYMMDD" date suffix via regex).
        display = _short_model_name(canonical)
        out.append({
            "model": canonical,
            "display": display,
            "chip": _chip_for_model(canonical),
            "cost_usd": cost,
            "cost_pct": pct,
        })
    return out


def _compute_intensity_buckets(rows: "list[DailyPanelRow]") -> list[float]:
    """Mutates rows in place to set `intensity_bucket`. Returns the
    threshold cut points (length 5 when any non-zero days exist, [] otherwise).

    Differs from spec §5.1 pseudocode: the spec saturates the largest of
    5 distinct values at bucket 4; this variant correctly reaches bucket
    5. See `test_compute_intensity_buckets_quintile_distribution`.

    Bucket 0 → cost == 0 (always).
    Buckets 1..5 → quintile over non-zero days only, so a string of
    zero-cost days doesn't flatten the scale.

    The five raw cut points are taken from the sorted non-zero costs at
    relative positions 0/5, 1/5, 2/5, 3/5, 4/5 (treated as "lower bound
    of bucket 1..5"). When fewer than five distinct non-zero values
    exist (e.g. one non-zero day), duplicate cut points are kept in the
    returned threshold list for telemetry, but bucket assignment uses a
    deduplicated copy so a single distinct value collapses to bucket 1
    rather than saturating at bucket 5.
    """
    nonzero = sorted(r.cost_usd for r in rows if r.cost_usd > 0)
    if not nonzero:
        for r in rows:
            r.intensity_bucket = 0
        return []
    # Five cut points at relative quintiles of the sorted non-zero days.
    # Using i in 0..4 (rather than 1..5) makes each threshold the LOWER
    # bound of its bucket — so a value v lands in bucket `count of
    # thresholds <= v`, which is exactly `bisect_right(thresholds, v)`.
    thresholds = [
        nonzero[min(int(i / 5 * len(nonzero)), len(nonzero) - 1)]
        for i in (0, 1, 2, 3, 4)
    ]
    # Dedup for the bucket-assignment lookup: with one distinct non-zero
    # value the raw list is [v]*5, and bisect_right would return 5 →
    # bucket 5. Dedup → [v], bisect_right → 1 → bucket 1, matching the
    # "degenerate data lands at the lowest active bucket" intent.
    distinct = sorted(set(thresholds))
    for r in rows:
        if r.cost_usd == 0:
            r.intensity_bucket = 0
        else:
            r.intensity_bucket = min(bisect.bisect_right(distinct, r.cost_usd), 5)
    return thresholds


def _select_current_block_for_envelope(
    conn: sqlite3.Connection,
    *,
    current_used_pct: "float | None",
    now_utc: "dt.datetime",
) -> "dict | None":
    """Select the current 5h block for the dashboard's current_week envelope.

    Selection rule (spec §4.1): pick the row from ``five_hour_blocks`` whose
    ``five_hour_window_key`` matches the latest ``weekly_usage_snapshots``
    row's ``five_hour_window_key``. Returns None when no row matches —
    binding to the latest snapshot's key (rather than highest
    ``block_start_at``) prevents the panel's "current 5h session" copy from
    surfacing stale closed blocks.

    The latest-snapshot lookup is filtered to ``captured_at_utc <=
    now_utc`` so callers that pin the clock (CCTALLY_AS_OF / --as-of) get
    a block consistent with the same pinned moment used for ``used_pct``.
    Without this filter, a past ``now_utc`` would still pick the
    absolute-newest snapshot's window and surface a future block's deltas
    against past usage — mirrors ``_handle_get_session_detail``'s
    ``snap.generated_at`` plumbing for the same reason.
    ``captured_at_utc`` is stored canonical UTC-Z (see ``now_utc_iso``),
    so a lexicographic ``<=`` compare is chronological.

    Stale-block suppression: the block lookup additionally filters on
    ``is_closed = 0`` AND ``five_hour_resets_at > now_utc``. The two
    clauses are belt-and-suspenders — ``maybe_update_five_hour_block``'s
    natural-expiration sweep (``UPDATE … SET is_closed = 1``) only fires
    on a ``record-usage`` tick, but the dashboard read can race ahead of
    the next tick when the user is idle past the reset. A block that
    matches the latest snapshot's key but whose ``five_hour_resets_at``
    has already passed is no longer current — surfacing it would render
    a stale "current 5h session" delta against post-reset weekly usage.
    Returns ``None`` in that case; the React panel falls back to the
    legacy single-big-number layout (CLAUDE.md gotcha:
    ``Dashboard current_week.five_hour_block …``).

    Returned dict has snake_case keys to match the existing dashboard
    envelope convention (``current_week`` is snake_case; CLI ``--json`` is
    camelCase — separate conventions, see CLAUDE.md).

    Delta semantics:
      - Non-crossed block: ``current_used_pct - seven_day_pct_at_block_start``
        (the natural "how much 7d% has changed during this 5h block" read).
      - Crossed block (``crossed_seven_day_reset == 1``): the block straddles
        a weekly reset, so the natural delta would be dominated by the
        reset itself (e.g. −94pp) rather than the user's actual burn-rate.
        Compute the POST-RESET delta instead — ``current_used_pct -
        weekly_percent_at_first_post_reset_snapshot_in_block``. The React
        panel prefixes this delta with ``⚡`` to show the reset crossing
        without hiding the informative number.
      - ``None`` only when ``current_used_pct`` is unknown OR the
        block-start anchor is missing AND no post-reset anchor was found.
    """
    snap = conn.execute(
        """
        SELECT five_hour_window_key, week_start_at
          FROM weekly_usage_snapshots
         WHERE captured_at_utc <= ?
         ORDER BY captured_at_utc DESC, id DESC
         LIMIT 1
        """,
        (_iso_z(now_utc),),
    ).fetchone()
    if snap is None or snap["five_hour_window_key"] is None:
        return None

    block = conn.execute(
        """
        SELECT five_hour_window_key, block_start_at, last_observed_at_utc,
               seven_day_pct_at_block_start,
               crossed_seven_day_reset
          FROM five_hour_blocks
         WHERE five_hour_window_key = ?
           AND is_closed = 0
           AND five_hour_resets_at > ?
        """,
        (snap["five_hour_window_key"], _iso_z(now_utc)),
    ).fetchone()
    if block is None:
        return None

    crossed = bool(block["crossed_seven_day_reset"])
    p_start = block["seven_day_pct_at_block_start"]

    # When the block crossed a weekly reset, recompute the delta against
    # the first post-reset snapshot inside the block instead of the
    # pre-reset block-start anchor. Use ``unixepoch()`` because
    # ``block_start_at`` is host-local-tz (``+03:00``) while
    # ``captured_at_utc`` is canonical UTC-Z; lex compares mis-order
    # mixed-offset moments. ``snap.week_start_at`` is the latest
    # (post-reset) week's anchor, so equality on that column scopes
    # the lookup to the current weekly window.
    p_anchor = p_start
    if crossed and snap["week_start_at"] is not None:
        post = conn.execute(
            """
            SELECT weekly_percent
              FROM weekly_usage_snapshots
             WHERE week_start_at = ?
               AND unixepoch(captured_at_utc) >= unixepoch(?)
               AND unixepoch(captured_at_utc) <= unixepoch(?)
             ORDER BY captured_at_utc ASC, id ASC
             LIMIT 1
            """,
            (
                snap["week_start_at"],
                block["block_start_at"],
                _iso_z(now_utc),
            ),
        ).fetchone()
        if post is not None and post["weekly_percent"] is not None:
            p_anchor = float(post["weekly_percent"])

    delta = (
        None if (p_anchor is None or current_used_pct is None)
        else round(current_used_pct - p_anchor, 9)
    )

    # Spec §5.3 — in-place credit events for this 5h block's window,
    # ascending by ``effective_reset_at_utc``. Drives the
    # ``CurrentWeekPanel.tsx`` ``⚡ credited -Xpp`` chip and the
    # ``CurrentWeekModal.tsx`` merged-stream 5h milestones section.
    # Snake_case keys to match the envelope convention (see CLAUDE.md;
    # CLI ``--json`` uses camelCase, dashboard envelope is snake_case).
    cred_rows = conn.execute(
        """
        SELECT effective_reset_at_utc, prior_percent, post_percent
          FROM five_hour_reset_events
         WHERE five_hour_window_key = ?
         ORDER BY effective_reset_at_utc ASC
        """,
        (int(block["five_hour_window_key"]),),
    ).fetchall()
    credits = [
        {
            "effective_reset_at_utc": c["effective_reset_at_utc"],
            "prior_percent": float(c["prior_percent"]),
            "post_percent": float(c["post_percent"]),
            "delta_pp": round(
                float(c["post_percent"]) - float(c["prior_percent"]), 1
            ),
        }
        for c in cred_rows
    ]

    return {
        "block_start_at":               block["block_start_at"],
        "five_hour_window_key":         int(block["five_hour_window_key"]),
        "seven_day_pct_at_block_start": p_start,
        "seven_day_pct_delta_pp":       delta,
        "crossed_seven_day_reset":      crossed,
        "credits":                      credits,
    }


# === Alerts-envelope per-axis row-mappers (Task F) =========================
# Each mapper turns one axis's ``alerted_at IS NOT NULL`` milestone rows into
# the shared envelope-item dicts. The SQL is genuinely heterogeneous per axis
# (Codex P0-3: distinct columns, JOINs, id shapes), so the registry unifies the
# *set* of axes + their ``milestone_table`` + the shared ``severity_for``
# authority, not the query itself. ``descriptor.milestone_table`` drives each
# ``FROM`` clause so the table name lives in the registry, not inlined here.


def _envelope_rows_weekly(conn, descriptor, limit, severity_for) -> list[dict]:
    # ``reset_event_id`` (v1.7.2) segments the same (week, threshold)
    # across pre-credit (0) and post-credit (event.id) cohorts, both
    # of which can be alerted. The envelope id must include the
    # segment so React's <li key={a.id}> / <tr key={a.id}> doesn't
    # collide on the duplicate (week, threshold) pair. Older clients
    # tolerate longer ids — the id is opaque to them; only the React
    # key uniqueness invariant matters.
    rows = conn.execute(
        f"""
        SELECT week_start_date, percent_threshold, captured_at_utc,
               alerted_at, cumulative_cost_usd, reset_event_id
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["percent_threshold"])
        cumulative = float(r["cumulative_cost_usd"])
        dpp = (cumulative / threshold) if threshold else None
        out.append({
            "id": f"weekly:{r['week_start_date']}:{threshold}:{r['reset_event_id']}",
            "axis": descriptor.id,
            "threshold": threshold,
            "severity": severity_for(threshold),
            "crossed_at": r["captured_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_date":     r["week_start_date"],
                "cumulative_cost_usd": cumulative,
                "dollars_per_percent": dpp,
                # Round-3: parallel to the 5h context block below — both
                # axes now expose ``reset_event_id`` so downstream
                # clients (panel, modal, third-party consumers) can
                # discriminate pre- vs post-credit crossings of the
                # same (week, threshold) without scraping the
                # envelope ``id`` string. 0 = pre-credit / no-event;
                # event.id = post-credit segment.
                "reset_event_id":      int(r["reset_event_id"]),
            },
        })
    return out


def _envelope_rows_five_hour(conn, descriptor, limit, severity_for) -> list[dict]:
    # Site F (spec §3.2 bucket C / §3.3): widen the row identity to
    # include ``reset_event_id`` so post-credit (seg=event.id) crossings
    # of the same (window_key, threshold) don't collide with pre-credit
    # (seg=0) crossings on the React row key. Older clients tolerate
    # longer ids — the id is opaque to them; only the React key
    # uniqueness invariant matters. Mirrors the weekly precedent.
    rows = conn.execute(
        f"""
        SELECT m.five_hour_window_key, m.percent_threshold, m.captured_at_utc,
               m.alerted_at, m.block_cost_usd, m.reset_event_id,
               b.block_start_at
        FROM {descriptor.milestone_table} m
        LEFT JOIN five_hour_blocks b ON b.five_hour_window_key = m.five_hour_window_key
        WHERE m.alerted_at IS NOT NULL
        ORDER BY m.alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["percent_threshold"])
        out.append({
            "id":          (
                f"five_hour:{int(r['five_hour_window_key'])}:"
                f"{threshold}:{int(r['reset_event_id'])}"
            ),
            "axis":        descriptor.id,
            "threshold":   threshold,
            "severity":    severity_for(threshold),
            "crossed_at":  r["captured_at_utc"],
            "alerted_at":  r["alerted_at"],
            "context": {
                "five_hour_window_key": int(r["five_hour_window_key"]),
                "block_start_at":       r["block_start_at"] or "",
                "block_cost_usd":       float(r["block_cost_usd"] or 0.0),
                "reset_event_id":       int(r["reset_event_id"]),
            },
        })
    return out


def _envelope_rows_budget_family(conn, descriptor, limit, severity_for) -> list[dict]:
    # Unified vendor-tagged budget axis (#143). ONE mapper backs BOTH the
    # ``budget`` (``vendor='claude'``, issue #19) and ``codex_budget``
    # (``vendor='codex'``, calendar-period-codex-budgets spec §6) axes —
    # ``descriptor.vendor`` drives the ``WHERE vendor=?`` row filter + the
    # ``COALESCE(period, <vendor-default-noun>)`` default, ``descriptor.id`` the
    # envelope id prefix, ``descriptor.milestone_table`` (now ``budget_milestones``
    # for both) the source table.
    #
    # Budget alerts are keyed by ``period_start_at`` (the resolved period-window
    # start instant — a subscription-week start for claude OR a calendar
    # period-start for codex) + the write-once ``period`` discriminator (#137) +
    # the integer threshold. No ``reset_event_id`` segment: a mid-week reset
    # (claude) or a period rollover (codex) re-anchors ``period_start_at`` so the
    # new window naturally gets fresh rows under
    # ``UNIQUE(vendor, period_start_at, period, threshold)``.
    #
    # All numbers + the ``period`` noun are read FROM THE ROW (snapshotted at
    # crossing), never live config that may have changed since (the Codex P0-4
    # lesson; Symptom 1 fix) — a user who fires alerts then switches
    # ``budget.period`` keeps the historical row's noun. ``COALESCE(period, …)``
    # renders a pre-011 NULL-sentinel row with the vendor-default noun and keeps
    # the ``id`` non-``None``; the id's ``period`` segment gives a calendar-week ↔
    # calendar-month coinciding-instant collision (now distinct coexisting rows)
    # distinct React keys.
    #
    # Byte-stable identity: the envelope id ==
    # ``f"{descriptor.id}:{period_start_at}:{period}:{threshold}"`` matches the
    # pre-#143 ``budget:…`` / ``codex_budget:…`` strings exactly (same instant
    # value, renamed key column). The per-vendor context dict KEY ORDER matches
    # the pre-#143 envelopes verbatim: the claude/``budget`` axis still emits BOTH
    # the legacy ``week_start_at`` AND ``period_start_at`` (same instant value) so
    # no existing TS consumer of ``context.week_start_at`` breaks; the
    # codex/``codex_budget`` axis emits ``period_start_at`` only.
    vendor = descriptor.vendor
    default_noun = "subscription-week" if vendor == "claude" else "calendar-month"
    rows = conn.execute(
        f"""
        SELECT period_start_at,
               COALESCE(period, ?) AS period,
               threshold, crossed_at_utc, alerted_at,
               budget_usd, spent_usd, consumption_pct
        FROM {descriptor.milestone_table}
        WHERE vendor = ? AND alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (default_noun, vendor, limit),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        if vendor == "claude":
            ctx = {  # key order byte-stable with the pre-#143 budget envelope
                "week_start_at":   r["period_start_at"],
                "period":          r["period"],
                "period_start_at": r["period_start_at"],
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            }
        else:
            ctx = {  # key order byte-stable with the pre-#143 codex_budget envelope
                "period":          r["period"],
                "period_start_at": r["period_start_at"],
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            }
        out.append({
            "id": (
                f"{descriptor.id}:{r['period_start_at']}:{r['period']}:{threshold}"
            ),
            "axis":       descriptor.id,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context":    ctx,
        })
    return out


def _envelope_rows_projected(conn, descriptor, limit, severity_for) -> list[dict]:
    # Fourth axis (issue #121): projected-pace threshold crossings. Like
    # budget, projected alerts re-anchor ``week_start_at`` on a mid-week
    # reset, so there is NO ``reset_event_id`` segment — the new window gets
    # fresh rows under ``UNIQUE(week_start_at, period, metric, threshold)``. The
    # ``metric`` discriminator (``weekly_pct`` | ``budget_usd`` |
    # ``codex_budget_usd``) drives the frontend's metric-aware context renderer;
    # ``denominator`` + ``projected_value`` are rendered FROM THE ROW (the values
    # snapshotted at crossing), never live config that may have changed since
    # (Codex P0-4). The envelope id mirrors the dispatch payload's
    # ``projected:<week_start_at>:<period>:<metric>:<threshold>`` shape. The
    # write-once ``period`` discriminator (#137) carries no symptom-1 label here
    # — projected's ``context`` is metric-driven, never a live-config period
    # noun — so ``COALESCE(period, 'subscription-week')`` is purely for a stable
    # non-``None`` id segment on a pre-011 NULL-sentinel row (the calendar-week ↔
    # calendar-month within-metric collision otherwise shares a React key).
    rows = conn.execute(
        f"""
        SELECT week_start_at,
               COALESCE(period, 'subscription-week') AS period,
               metric, threshold, projected_value,
               denominator, crossed_at_utc, alerted_at
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        metric = str(r["metric"])
        out.append({
            "id": (
                f"projected:{r['week_start_at']}:{r['period']}"
                f":{metric}:{threshold}"
            ),
            "axis":       descriptor.id,
            "metric":     metric,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_at":   r["week_start_at"],
                "metric":          metric,
                "projected_value": float(r["projected_value"]),
                "denominator":     float(r["denominator"]),
            },
        })
    return out


def _envelope_rows_project_budget(conn, descriptor, limit, severity_for) -> list[dict]:
    # Fifth axis (issue #19 / #121): PER-PROJECT equiv-$ budget threshold
    # crossings. Like the global budget axis, project-budget alerts re-anchor
    # ``week_start_at`` on a mid-week reset, so there is NO ``reset_event_id``
    # segment — the new window gets fresh rows under
    # ``UNIQUE(week_start_at, project_key, threshold)``. ``project_key`` is the
    # canonical git-root (``ProjectKey.bucket_path``); the human-readable chip
    # context carries the project BASENAME, resolved through the production
    # ``_resolve_project_key`` (git-root mode) so a moved/deleted repo still
    # renders its basename from the snapshotted path (no FS dependency on a
    # live ``.git``). ``budget_usd`` / ``spent_usd`` / ``consumption_pct`` are
    # rendered FROM THE ROW (snapshotted at crossing), never live config that
    # may have changed since (Codex P0-4). The envelope id mirrors the dispatch
    # payload's ``project_budget:<week_start_at>:<project_key>:<threshold>``
    # shape (``_build_alert_payload_project_budget``).
    rows = conn.execute(
        f"""
        SELECT week_start_at, project_key, threshold, budget_usd, spent_usd,
               consumption_pct, crossed_at_utc, alerted_at
        FROM {descriptor.milestone_table}
        WHERE alerted_at IS NOT NULL
        ORDER BY alerted_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    c = sys.modules["cctally"]
    # Collision-aware labels via the shared primitive (#130), disambiguated
    # across the alerted ROWS (NOT live config) to preserve the
    # render-from-the-snapshotted-row invariant above — so a deleted/renamed
    # config key still renders. This is intentionally a different feed than the
    # table/notification (full config); see spec §1 Goals (Codex F1).
    label_by_key = c._project_budget_labels(
        sorted({r["project_key"] for r in rows})
    )
    out: list[dict] = []
    for r in rows:
        threshold = int(r["threshold"])
        project_key = r["project_key"]
        out.append({
            "id": (
                f"project_budget:{r['week_start_at']}:{project_key}:{threshold}"
            ),
            "axis":       descriptor.id,
            "threshold":  threshold,
            "severity":   severity_for(threshold),
            "crossed_at": r["crossed_at_utc"],
            "alerted_at": r["alerted_at"],
            "context": {
                "week_start_at":   r["week_start_at"],
                "project":         label_by_key.get(project_key, project_key),
                "project_key":     project_key,
                "budget_usd":      float(r["budget_usd"]),
                "spent_usd":       float(r["spent_usd"]),
                "consumption_pct": float(r["consumption_pct"]),
            },
        })
    return out


# Keyed by ``AlertAxisDescriptor.id`` — the registry decides which axes run,
# in what order; this table supplies the bespoke heterogeneous row-mapper.
# ``budget`` (vendor='claude') and ``codex_budget`` (vendor='codex') share the
# one ``_envelope_rows_budget_family`` mapper (#143) — it reads
# ``descriptor.vendor`` for the row filter + default noun and ``descriptor.id``
# for the envelope id prefix, so the two axes stay distinct on the wire.
_ENVELOPE_AXIS_MAPPERS = {
    "weekly": _envelope_rows_weekly,
    "five_hour": _envelope_rows_five_hour,
    "budget": _envelope_rows_budget_family,
    "projected": _envelope_rows_projected,
    "project_budget": _envelope_rows_project_budget,
    "codex_budget": _envelope_rows_budget_family,
}


def _build_alerts_envelope_array(
    conn: sqlite3.Connection,
    limit: int = 100,
) -> list[dict]:
    """Return the ``alerts`` array for the SSE snapshot envelope.

    Union of ``percent_milestones``, ``five_hour_milestones``,
    ``budget_milestones`` (vendor-tagged — backs BOTH the ``budget`` and
    ``codex_budget`` axes since #143), ``projected_milestones``, and
    ``project_budget_milestones`` rows with
    ``alerted_at IS NOT NULL``, ordered newest-first by ``alerted_at``, capped at
    ``limit`` (default 100). Single source of truth for both the dashboard panel
    (slices to 10 client-side) and the modal (renders all 100). Forward-only
    semantics: only rows the alert-dispatch path stamped get included; pre-deploy
    crossings stay NULL and are intentionally invisible (spec §4.3).

    All six axes share the same envelope schema; the ``axis`` field
    (``weekly`` / ``five_hour`` / ``budget`` / ``projected`` /
    ``project_budget`` / ``codex_budget``) discriminates. The ``projected`` axis
    additionally carries a top-level ``metric`` (``weekly_pct`` | ``budget_usd``
    | ``codex_budget_usd``) so the frontend can pick its metric-aware context
    renderer; ``project_budget``
    carries the project basename + ``$spent of $budget`` in its context;
    ``budget`` + ``codex_budget`` carry a ``period`` discriminator
    (subscription-week / calendar-week / calendar-month) so the frontend renders
    a period-aware "Month" / "Calendar week" / "Week" label.

    Per-axis ``LIMIT`` is applied at the SQL level (each query may yield
    up to ``limit``) and the union is re-sorted + sliced — important for
    the boundary case where one axis has ``limit`` rows and the other
    has more recent ones that would otherwise be dropped before the
    final sort.

    **Registry-driven (Task F).** The *set* of axes, their union *order*,
    and each axis's ``milestone_table`` come from
    ``_lib_alert_axes.AXIS_REGISTRY`` — adding a future axis is "register a
    descriptor + add a row-mapper", not "hand-roll a parallel branch". The
    SQL stays genuinely heterogeneous per axis (Codex P0-3: different
    columns, JOINs, id shapes), so each descriptor pairs with a bespoke
    row-mapper keyed by ``descriptor.id`` in ``_ENVELOPE_AXIS_MAPPERS``. The
    shared ``severity_for`` kernel stamps the additive ``severity`` field on
    every item (single severity authority, consumed by the frontend too).
    """
    c = sys.modules["cctally"]
    registry = c.AXIS_REGISTRY
    severity_for = c.severity_for
    out: list[dict] = []
    for descriptor in registry:
        mapper = _ENVELOPE_AXIS_MAPPERS.get(descriptor.id)
        if mapper is None:  # pragma: no cover - registry/mapper drift guard
            continue
        out.extend(mapper(conn, descriptor, limit, severity_for))

    # Python's list.sort is stable. When two alerts share the same
    # `alerted_at` ISO string (rare; multiple axes firing within the same
    # millisecond), the union order (weekly, then 5h, then budget, then
    # projected) determines the tiebreaker — no extra deterministic key is
    # added because the spec doesn't require one.
    out.sort(key=lambda a: a["alerted_at"], reverse=True)
    return out[:limit]


def snapshot_to_envelope(snap: "DataSnapshot", *,
                         now_utc: "dt.datetime",
                         monotonic_now: "float | None" = None,
                         oauth_usage_cfg: "dict | None" = None,
                         display_tz_pref_override: "str | None" = None,
                         runtime_bind: "str | None" = None,
                         transcripts_visible: bool = False) -> dict:
    """Serialize a DataSnapshot into the JSON envelope consumed by the
    browser (design spec §2.2).

    ``transcripts_visible`` gates the transcript-derived session ``title``
    (#264 S3): the key is emitted ONLY when the flag is True AND the row has
    a title, so False (the default) fails closed for any caller that forgets
    to pass it — no ``title`` key, no leaked prompt content. The two
    browser-serving emit sites (``GET /api/data`` + the SSE loop) pass the
    per-request ``_transcripts_visible_to_request()`` — the SAME predicate
    that drives ``transcriptsEnabled`` and the per-row "open conversation"
    button; every other caller (share builders, fixtures, tests) keeps the
    default. ``cache_hit_pct`` (sessions) and ``cost_usd`` (trend) are plain
    numbers and are never gated.

    Pure function — no I/O on the snapshot data path. Reads
    ``config.json`` once for ``display.tz`` and once for the
    ``alerts_settings`` mirror (cheap atomic-rename reads); the
    actual ``alerts`` array comes from the precomputed
    ``snap.alerts`` already populated by the sync thread, so
    rendering never touches the DB.

    ``now_utc`` is used for wall-clock age computations (``reset_in_sec``,
    ``last_snapshot_age_sec``, etc.). ``monotonic_now`` is the caller's
    ``time.monotonic()`` snapshot, used only to compute ``sync_age_s``
    against ``snap.last_sync_at`` (a monotonic-clock reading on the real
    DataSnapshot). Pass ``None`` when no sync has happened yet.

    ``oauth_usage_cfg`` is the resolved oauth_usage block (from
    ``_get_oauth_usage_config(load_config())``) used for freshness-label
    thresholds. Callers MUST pass a pre-resolved cfg to keep this
    function pure (no per-tick FS reads on the dashboard hot path);
    when ``None``, falls back to ``_OAUTH_USAGE_DEFAULTS`` directly so
    pure-construction tests don't need to plumb config.

    Panels that have no data serialize as None so the JS client can
    render placeholders without special-casing.

    Field mapping (envelope key → DataSnapshot attribute):
      * ``current_week.*``  ← ``TuiCurrentWeek`` (dollars_per_percent,
        five_hour_resets_at, latest_snapshot_at, spent_usd, used_pct,
        five_hour_pct). ``week_label`` and ``reset_at_utc`` are
        synthesized from ``week_start_at`` / ``week_end_at``.
      * ``forecast.*``      ← ``ForecastOutput`` (inputs.confidence,
        inputs.dollars_per_percent, r_avg / r_recent, inputs.p_now /
        remaining_hours, budgets[]). ``week_avg_projection_pct`` and
        ``recent_24h_projection_pct`` are computed per-method from their
        source rates so labels stay correct on decelerating weeks
        (r_recent < r_avg); ``recent_24h`` is omitted when ``r_recent``
        is None or the two projections match. ``budget_100_per_day_usd``
        / ``budget_90_per_day_usd`` pick the matching
        ``BudgetRow.dollars_per_day``.
      * ``trend.weeks[]``   ← ``snap.trend`` (the 8-week panel dataset,
        ``TuiTrendRow``: week_label, used_pct, dollars_per_percent,
        delta_dpp, is_current, spark_height). Preserves the envelope key
        names (``label``, ``dollar_per_pct``, ``delta``) that the JS
        consumes. Do NOT swap this for ``snap.weekly_history`` — that
        field holds up to 12 rows for the modal (``trend.history[]``
        below) and would overflow the panel's hard-coded "(8 weeks)"
        header if used for ``weeks[]``.
      * ``trend.history[]`` ← ``snap.weekly_history`` (up to 12 rows,
        same TuiTrendRow shape as ``weeks[]``). v2-only: consumed by
        the Trend detail modal. Sibling to ``weeks[]``, not a
        replacement.
      * ``sessions.rows[]`` ← ``TuiSessionRow`` (session_id, started_at,
        duration_minutes, model_primary, project_label, cost_usd).
    """
    cw = snap.current_week
    fc = snap.forecast
    # Issue #57 — prefer ``snap.forecast_view`` (precomputed by the
    # sync thread via ``build_forecast_view``) over re-deriving the
    # projection / verdict / header-routing / budget fields inline.
    # Falls back to the legacy inline routing below when
    # ``forecast_view`` is missing — fixture modules that construct
    # ``DataSnapshot`` positionally without the post-Bundle-1 fields
    # leave it at ``None``, and their goldens predate the View so
    # keeping the legacy path under that fallback preserves byte
    # stability.
    fc_view = getattr(snap, "forecast_view", None)

    # F1 fix: server-resolve the display tz to a CONCRETE IANA name and
    # surface it on the envelope so the browser never has to guess "local".
    # Reused below for week_lbl / blocks / monthly label rendering so the
    # whole envelope speaks one zone consistently.
    # F3 fix: when the dashboard was started with `--tz <X>`, the override
    # supersedes the persisted config.display.tz for the lifetime of the
    # process. The override flows in as a canonical tz token; we layer it
    # onto the config dict via _apply_display_tz_override before resolving
    # so every reader downstream sees one zone.
    # #268 M4: read the config + update-state + doctor from the sync-thread
    # precompute (spec §6) so this stays a PURE renderer — no config.json read,
    # no `security` fork, no update-state file read per SSE client per tick.
    # When the fields are absent (fixtures / the initial empty snapshot /
    # positionally-constructed DataSnapshots) fall back to the inline reads so
    # behavior is byte-identical for those callers.
    _precomp = getattr(snap, "envelope_precompute", None)
    _raw_config = _precomp["config"] if _precomp is not None else sys.modules["cctally"].load_config()
    config = sys.modules["cctally"]._apply_display_tz_override(
        _raw_config, display_tz_pref_override
    )
    display_block = _compute_display_block(config, snap.generated_at)
    resolved_tz_obj = ZoneInfo(display_block["resolved_tz"])

    if snap.last_sync_at is not None and monotonic_now is not None:
        sync_age_s = max(0, int(monotonic_now - snap.last_sync_at))
    else:
        sync_age_s = None

    # Header is a thin projection of the other panels — the JS can read
    # from current_week / forecast directly, but pre-composing it lets
    # tools like `curl` read a self-contained envelope.
    used_pct = getattr(cw, "used_pct", None) if cw is not None else None
    five_hr  = getattr(cw, "five_hour_pct", None) if cw is not None else None
    dollar_pp = getattr(cw, "dollars_per_percent", None) if cw is not None else None

    # Forecast field routing (issue #57). ``snap.forecast_view`` is the
    # single source of truth: ``build_forecast_view`` runs the per-
    # method projection / verdict / budget routing once and stashes
    # the surface fields on the View. The legacy inline derivation
    # remains as a fallback for fixture modules that construct
    # ``DataSnapshot`` positionally without populating ``forecast_view``
    # — their goldens predate the View, and the legacy block emits
    # the same numbers so byte stability is preserved.
    fcast_pct: "float | None" = None
    recent_24h_pct: "float | None" = None
    verdict: "str | None" = None
    confidence: "str | None" = None
    budget_100: "float | None" = None
    budget_90: "float | None" = None
    if fc_view is not None:
        fcast_pct = fc_view.week_avg_projection_pct
        recent_24h_pct = fc_view.recent_24h_projection_pct
        # ForecastView.dashboard_verdict / .confidence default to
        # ``"ok"`` / ``"unknown"`` even when ``output is None``; only
        # surface non-None envelope values when there's an actual
        # ForecastOutput backing them so the existing
        # ``verdict / confidence is None when fc is None`` shape stays.
        verdict = fc_view.dashboard_verdict if fc is not None else None
        confidence = fc_view.confidence if fc is not None else None
        budget_100 = fc_view.budget_100_per_day_usd
        budget_90 = fc_view.budget_90_per_day_usd
    elif fc is not None:
        # Legacy inline routing — kept verbatim for positionally-
        # constructed fixture snapshots that don't carry ``forecast_view``.
        inputs = getattr(fc, "inputs", None)
        if inputs is not None:
            confidence = getattr(inputs, "confidence", None)
        r_avg = getattr(fc, "r_avg", None)
        r_recent = getattr(fc, "r_recent", None)
        p_now = getattr(inputs, "p_now", None) if inputs is not None else None
        rem_hrs = getattr(inputs, "remaining_hours", None) if inputs is not None else None
        if p_now is not None and rem_hrs is not None and r_avg is not None:
            fcast_pct = p_now + r_avg * rem_hrs
        if (p_now is not None and rem_hrs is not None
                and r_recent is not None):
            p_final_recent = p_now + r_recent * rem_hrs
            if fcast_pct is None or p_final_recent != fcast_pct:
                recent_24h_pct = p_final_recent
        if getattr(fc, "already_capped", False):
            verdict = "capped"
        elif getattr(fc, "projected_cap", False):
            verdict = "cap"
        else:
            verdict = "ok"
        for b in getattr(fc, "budgets", []) or []:
            tp = getattr(b, "target_percent", None)
            dpd = getattr(b, "dollars_per_day", None)
            if tp == 100:
                budget_100 = dpd
            elif tp == 90:
                budget_90 = dpd

    # Freshness envelope for current_week — derived from
    # cw.latest_snapshot_at (a datetime, not an ISO string). None when
    # cw is absent or has no snapshot timestamp; the React client renders
    # a placeholder in that case. Refs spec §3.4.
    cw_freshness: "dict | None" = None
    if cw is not None and cw.latest_snapshot_at is not None:
        captured = cw.latest_snapshot_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=dt.timezone.utc)
        age_s = max(0.0, (now_utc - captured).total_seconds())
        _fresh_cfg = (
            oauth_usage_cfg if oauth_usage_cfg is not None
            else sys.modules["cctally"]._OAUTH_USAGE_DEFAULTS
        )
        cw_freshness = {
            "label":        sys.modules["cctally"]._freshness_label(age_s, _fresh_cfg),
            "captured_at":  _iso_z(captured),
            "age_seconds":  int(age_s),
        }

    week_lbl: "str | None" = None
    reset_at_utc: "dt.datetime | None" = None
    if cw is not None:
        ws = getattr(cw, "week_start_at", None)
        we = getattr(cw, "week_end_at", None)
        # Full window "Apr 13–20" so the dashboard matches the TUI/report
        # headers and doesn't hide month crossings (e.g. "Apr 27–May 03").
        # En-dash U+2013 matches the TUI format at cmd_tui.
        if ws is not None and we is not None:
            week_lbl = (
                f"{format_display_dt(ws, resolved_tz_obj, fmt='%b %d', suffix=False)}"
                f"–"
                f"{format_display_dt(we, resolved_tz_obj, fmt='%b %d', suffix=False)}"
            )
        elif ws is not None:
            week_lbl = format_display_dt(ws, resolved_tz_obj, fmt='%b %d', suffix=False)
        reset_at_utc = we

    # Header forecast_pct should match the projection that drove the
    # verdict pill next to it. The View (issue #57) carries the
    # already-routed ``header_projection_pct``; the fallback path
    # replays the legacy routing inline for fixture snapshots that
    # don't populate ``forecast_view``. The Forecast panel still
    # exposes both ``week_avg_projection_pct`` and
    # ``recent_24h_projection_pct`` unchanged.
    if fc_view is not None and fc is not None:
        header_fcast_pct = fc_view.header_projection_pct
    else:
        header_fcast_pct = fcast_pct
        if (
            verdict in ("cap", "capped")
            and recent_24h_pct is not None
            and fcast_pct is not None
            and recent_24h_pct > fcast_pct
        ):
            header_fcast_pct = recent_24h_pct

    # ---- weekly / monthly periods ---------------------------------
    def _weekly_row_to_dict(r: "WeeklyPeriodRow") -> dict:
        return {
            "label":                  r.label,
            "cost_usd":               r.cost_usd,
            "total_tokens":           r.total_tokens,
            "input_tokens":           r.input_tokens,
            "output_tokens":          r.output_tokens,
            "cache_creation_tokens":  r.cache_creation_tokens,
            "cache_read_tokens":      r.cache_read_tokens,
            "used_pct":               r.used_pct,
            "dollar_per_pct":         r.dollar_per_pct,
            "delta_cost_pct":         r.delta_cost_pct,
            "is_current":             r.is_current,
            "models":                 list(r.models),
            "week_start_at":          r.week_start_at,
            "week_end_at":            r.week_end_at,
        }

    def _monthly_row_to_dict(r: "MonthlyPeriodRow") -> dict:
        return {
            "label":                  r.label,
            "cost_usd":               r.cost_usd,
            "total_tokens":           r.total_tokens,
            "input_tokens":           r.input_tokens,
            "output_tokens":          r.output_tokens,
            "cache_creation_tokens":  r.cache_creation_tokens,
            "cache_read_tokens":      r.cache_read_tokens,
            "delta_cost_pct":         r.delta_cost_pct,
            "is_current":             r.is_current,
            "models":                 list(r.models),
        }

    def _blocks_row_to_dict(r: "BlocksPanelRow") -> dict:
        return {
            "start_at":  r.start_at,
            "end_at":    r.end_at,
            "anchor":    r.anchor,
            "is_active": r.is_active,
            "cost_usd":  r.cost_usd,
            "models":    list(r.models),
            "label":     r.label,
        }

    def _daily_row_to_dict(r: "DailyPanelRow") -> dict:
        return {
            "date":             r.date,
            "label":            r.label,
            "cost_usd":         r.cost_usd,
            "is_today":         r.is_today,
            "intensity_bucket": r.intensity_bucket,
            "models":           list(r.models),
            # ---- v2.3 additions ----
            "input_tokens":          r.input_tokens,
            "output_tokens":         r.output_tokens,
            "cache_creation_tokens": r.cache_creation_tokens,
            "cache_read_tokens":     r.cache_read_tokens,
            "total_tokens":          r.total_tokens,
            "cache_hit_pct":         r.cache_hit_pct,
        }

    # Spec §2.7: empty state is `weekly.rows === []`, not `weekly === null`.
    # Always emit a `{rows: [...]}` envelope (possibly empty) so the panel
    # can distinguish "loading / not synced yet" (null parent) from
    # "synced + no data" (empty rows). For Weekly/Monthly, sync always
    # builds the rows list (even if empty), so we always emit the object.
    weekly_env = {
        "rows": [_weekly_row_to_dict(r) for r in snap.weekly_periods],
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals. Sourced from sum-over-visible-rows in the sync thread
        # (``_tui_build_snapshot``) so the footer total is structurally
        # equal to the React panel's ``rows.reduce(...)``. The earlier
        # ``build_weekly_view``-sourced totals undercounted Bug-K
        # pre-credit synthesized rows on credit weeks; the regression is
        # captured by
        # ``test_weekly_envelope_total_matches_sum_of_visible_rows``.
        "total_cost_usd": snap.weekly_total_cost_usd,
        "total_tokens":   snap.weekly_total_tokens,
    }
    monthly_env = {
        "rows": [_monthly_row_to_dict(r) for r in snap.monthly_periods],
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals via sum-over-visible-rows so the React MonthlyPanel's
        # smoking-gun ``rows.reduce(...)`` collapses to a single envelope
        # read with the structural-equality invariant preserved.
        "total_cost_usd": snap.monthly_total_cost_usd,
        "total_tokens":   snap.monthly_total_tokens,
    }

    blocks_env = {
        "rows": [_blocks_row_to_dict(r) for r in snap.blocks_panel],
        # View-model unification follow-up (issue #56): additive scalars
        # so the React BlocksPanel can stop running `rows.reduce(...)`
        # in JS. Cost is summed-over-visible-rows in
        # `_dashboard_build_blocks_view` (same structural invariant as
        # daily/weekly/monthly footers); ``total_tokens`` is sourced
        # from the same view since ``BlocksPanelRow`` doesn't carry
        # token columns and we don't want to widen that shape.
        "total_cost_usd": snap.blocks_total_cost_usd,
        "total_tokens":   snap.blocks_total_tokens,
    }

    # Re-run helper to derive thresholds; mutates rows[*].intensity_bucket
    # (no-op for builder-constructed rows since values match cost_usd).
    # Single-source-of-truth with bucket assignment — do NOT re-derive
    # thresholds with an independent formula.
    daily_rows = list(snap.daily_panel)
    daily_thresholds = _compute_intensity_buckets(daily_rows)
    daily_peak = None
    if daily_rows:
        nonzero_rows = [r for r in daily_rows if r.cost_usd > 0]
        if nonzero_rows:
            peak_row = max(nonzero_rows, key=lambda r: r.cost_usd)
            daily_peak = {"date": peak_row.date, "cost_usd": peak_row.cost_usd}
    daily_env = {
        "rows":                [_daily_row_to_dict(r) for r in daily_rows],
        "quantile_thresholds": daily_thresholds,
        "peak":                daily_peak,
        # View-model unification (Bundle 1; spec §6.6): pre-computed
        # totals via sum-over-visible-rows in the sync thread. Gap days
        # carry ``cost_usd=0.0`` and ``total_tokens=0`` so summing the
        # materialized panel rows preserves the gap-free semantics. The
        # React panel's `rows.reduce(...)` collapses to a single
        # envelope read with the structural-equality invariant
        # preserved.
        "total_cost_usd":      snap.daily_total_cost_usd,
        "total_tokens":        snap.daily_total_tokens,
    }

    # ---- threshold-actions T5: alerts envelope + settings mirror ----
    # `alerts` is precomputed at sync-thread snapshot-build time (see
    # `_tui_build_snapshot`'s `_build_alerts_envelope_array(conn)` call)
    # so this remains a pure render path (no DB I/O on dashboard hot
    # path). Single source of truth for both the dashboard panel
    # (slices to 10 client-side) and the modal (renders all 100).
    #
    # `alerts_settings` mirrors the validated alerts config block into
    # the envelope so the dashboard's SettingsOverlay can seed its
    # fieldset without a separate GET /api/settings round-trip
    # (parallels how `display.tz` lives at envelope["display"]["tz"]).
    # Defensive: a corrupt alerts block in `config.json` must not 500
    # the entire snapshot — fall back to safe defaults and rely on
    # `_warn_alerts_bad_config_once` for the user-visible signal.
    alerts_array = list(getattr(snap, "alerts", []) or [])
    # #268 M4: reuse the precompute's config (or the fallback load_config()
    # resolved above) — the envelope used to call load_config() a SECOND time
    # here. Within one tick both reads returned the same file, so this is
    # byte-identical.
    _cfg_for_alerts = _raw_config
    try:
        _alerts_cfg = _get_alerts_config(_cfg_for_alerts)
    except _AlertsConfigError as exc:
        sys.modules["cctally"]._warn_alerts_bad_config_once(exc)
        _alerts_cfg = {
            "enabled": False,
            "weekly_thresholds": [],
            "five_hour_thresholds": [],
            "projected_enabled": False,
            # Mirror the dispatch keys so the new alerts_settings lines
            # (`notifier` / `command_configured`) don't KeyError on a
            # corrupt config. Safe defaults: no notifier override, no
            # configured command.
            "notifier": "auto",
            "command_template": None,
        }
    # Budget is its OWN config block (issue #19) — source budget fields
    # from ``_get_budget_config`` (the validated ``budget`` block), NOT
    # the ``alerts`` block. Defensive: a corrupt budget block must not
    # 500 the whole snapshot — fall back to "no budget / disabled".
    try:
        _budget_cfg = _get_budget_config(_cfg_for_alerts)
    except _BudgetConfigError:
        _budget_cfg = {"weekly_usd": None, "alerts_enabled": True,
                       "alert_thresholds": [], "projected_enabled": False,
                       "projects": {}, "project_alerts_enabled": False}
    alerts_settings = {
        "enabled":              _alerts_cfg["enabled"],
        "weekly_thresholds":    list(_alerts_cfg["weekly_thresholds"]),
        "five_hour_thresholds": list(_alerts_cfg["five_hour_thresholds"]),
        "budget_thresholds":    list(_budget_cfg["alert_thresholds"]),
        "budget_enabled":       _budget_alerts_active(_budget_cfg),
        # Projected-pace opt-in mirrors (#121). Two flags, one per parent
        # axis — the frontend SettingsOverlay seeds two toggles. Sourced
        # from the validated getters' ``projected_enabled`` (default False).
        "projected_weekly_enabled": bool(_alerts_cfg.get("projected_enabled")),
        "projected_budget_enabled": bool(_budget_cfg.get("projected_enabled")),
        # Per-project budget alerts opt-in mirror (issue #19 / #121). Gates the
        # ``project_budget`` axis dispatch only (the display section always
        # renders configured projects). Sourced from the validated budget
        # getter's ``project_alerts_enabled`` (default False) — the frontend
        # SettingsOverlay seeds a single on/off toggle from it.
        "project_alerts_enabled": bool(_budget_cfg.get("project_alerts_enabled")),
        # Codex budget toggle mirrors (#134). The frontend SettingsOverlay
        # seeds two toggles (alerts + projected) + a disabled-with-hint empty
        # state from these three flags. ``_budget_cfg["codex"]`` is ``None`` by
        # default (no Codex budget) → all three default false/absent safely;
        # the ``_BudgetConfigError`` fallback dict above lacks a ``codex`` key,
        # so ``.get("codex")`` → ``None`` is likewise safe.
        "codex_budget_configured":     _budget_cfg.get("codex") is not None,
        "codex_budget_alerts_enabled": bool((_budget_cfg.get("codex") or {}).get("alerts_enabled")),
        "codex_projected_enabled":     bool((_budget_cfg.get("codex") or {}).get("projected_enabled")),
        # Alert-dispatch notifier mirror (Phase B). `notifier` is the
        # validated backend selector ("auto"/"command"/etc.). The raw
        # `command_template` is NEVER mirrored — it routinely holds secrets
        # (webhook URLs, bearer tokens) and the SSE snapshot is broadcast to
        # every connected client. We expose only a boolean: is a custom
        # command configured? (the CLI/config remains the sole writer of the
        # template itself).
        "notifier":           _alerts_cfg.get("notifier", "auto"),
        "command_configured": _alerts_cfg.get("command_template") is not None,
    }
    # Dashboard render-prefs mirror (cache-failure-markers opt-out, spec §5).
    # Reuses the single `_cfg_for_alerts = load_config()` read above (no extra
    # FS hit on the hot path). Default true — absence is treated as ON (opt-out,
    # not opt-in); a hand-edited non-bool surfaces the default. The on/off toggle
    # is honored entirely at client render time; the kernel + API always emit the
    # marker data.
    _dash_cfg = _cfg_for_alerts.get("dashboard") if isinstance(
        _cfg_for_alerts.get("dashboard"), dict) else {}
    _cfm = _dash_cfg.get("cache_failure_markers", True)
    _lt = _dash_cfg.get("live_tail", True)
    dashboard_prefs = {
        "cache_failure_markers": _cfm if isinstance(_cfm, bool) else True,
        "live_tail": _lt if isinstance(_lt, bool) else True,
    }

    # Mirror update-state.json + update-suppress.json into the envelope
    # so the dashboard's amber "Update available" badge repaints from
    # the SSE channel rather than requiring a /api/update/status fetch
    # per check. Without this mirror, a long-open dashboard tab never
    # learns that the dashboard's `_DashboardUpdateCheckThread` wrote
    # a fresher latest_version (24h-TTL by default) — the badge stayed
    # hidden until manual reload. Same precedent as `alerts_settings`:
    # cheap atomic-rename reads on the snapshot hot path. Failures
    # produce a `null` block so a missing/corrupt state file doesn't
    # 500 the entire envelope; the client falls back to the defensive
    # null-state shape `coerceUpdateState` already understands.
    #
    # #268 M4: read from the sync-thread precompute (spec §6) when present so
    # this stays pure — no update-state file reads per SSE client. The inline
    # try/except is the fallback for fixtures / the initial snapshot, and keeps
    # the SAME error sentinels so the derived block is byte-identical.
    if _precomp is not None:
        _update_state_envelope = _precomp["update_state"]
        _update_suppress_envelope = _precomp["update_suppress"]
    else:
        try:
            _update_state_envelope = sys.modules["cctally"]._load_update_state()
        except sys.modules["cctally"].UpdateError:
            # _load_update_state() raises on truly malformed JSON. Surface
            # an _error sentinel so the client renders "no update info" the
            # same way it does for unreachable /api/update/status.
            _update_state_envelope = {"_error": "update-state.json invalid"}
        except Exception:
            _update_state_envelope = {"_error": "update-state.json read failed"}
        try:
            _update_suppress_envelope = sys.modules["cctally"]._load_update_suppress()
        except Exception:
            _update_suppress_envelope = {
                "skipped_versions": [], "remind_after": None,
            }
    update_envelope = {
        "state":    _update_state_envelope,
        "suppress": _update_suppress_envelope,
    }

    # Doctor aggregate block (spec §5.5). Only the small severity tree
    # rides on every SSE tick (~120 bytes); the full per-check payload
    # is fetched lazily via `GET /api/doctor`. `runtime_bind` is the
    # actual host the dashboard process is bound to (Codex H4) so
    # `safety.dashboard_bind` reflects the running state, not just
    # `config.json`. Defensive: never crash the snapshot pipeline on
    # a doctor failure — surface a synthetic FAIL block with `_error`.
    #
    # #268 M4: read the precomputed doctor block from the snapshot (spec §6)
    # so this render path never forks the `security` keychain subprocess. The
    # sync thread computes it ONCE per rebuild via `doctor_payload_memo`
    # (`_tui_precompute_doctor_payload`). The inline try/except below is the
    # fallback for fixtures / the initial snapshot / positionally-constructed
    # DataSnapshots (`doctor_payload=None`) and produces the SAME block, so
    # moving the computation is byte-identical. The lazy `GET /api/doctor`
    # endpoint keeps computing LIVE (an explicit user refresh must be fresh).
    _doctor_payload = getattr(snap, "doctor_payload", None)
    if _doctor_payload is not None:
        doctor_envelope: "dict" = _doctor_payload
    else:
        try:
            _ld = sys.modules["cctally"]._load_sibling("_lib_doctor")
            _doc_state = sys.modules["cctally"].doctor_gather_state(now_utc=now_utc, runtime_bind=runtime_bind)
            _doc_report = _ld.run_checks(_doc_state)
            doctor_envelope = {
                "severity":     _doc_report.overall_severity,
                "counts":       dict(_doc_report.counts),
                "generated_at": _ld._iso_z(_doc_report.generated_at),
                "fingerprint":  _ld.fingerprint(_doc_report),
            }
        except Exception as exc:  # noqa: BLE001 — never crash SSE on doctor failure
            doctor_envelope = {
                "severity":     "fail",
                "counts":       {"ok": 0, "warn": 0, "fail": 1},
                "generated_at": _iso_z(now_utc),
                "fingerprint":  "sha1:" + ("0" * 40),
                "_error":       f"{type(exc).__name__}: {exc}",
            }

    # B1 (#207): the "vs last week" header delta reuses the is_current trend
    # row's delta_dpp ($/1% vs the previous trend row — normally the prior
    # subscription week). Select by the is_current FLAG, not snap.trend[-1]:
    # snap.trend is oldest-first by week_start_date and reset/credit handling
    # can synthesize an intervening row, so position -1 is not guaranteed to
    # be the current week (Codex P1). reversed() picks the latest if ever
    # multiple are flagged. Null-safe: None when no row is current, or when
    # the current row's delta_dpp is itself None — both hide the stat.
    _current_trend = next(
        (r for r in reversed(snap.trend) if r.is_current), None
    ) if snap.trend else None

    return {
        "envelope_version": 2,
        # #278 Theme A: single additive first-paint hydration latch. True only
        # on the cheap seed + A2's partial republishes (data still being
        # assembled); False on every complete/stable snapshot. ``getattr``
        # default keeps positionally-constructed fixture snapshots serializing.
        "hydrating":        bool(getattr(snap, "hydrating", False)),
        "generated_at":     _iso_z(snap.generated_at),
        # last_sync_at in DataSnapshot is a monotonic float, not a wall
        # clock — the envelope's wall-clock moment is unknowable from
        # here, so expose it as None and rely on sync_age_s for the UI.
        "last_sync_at":    None,
        "sync_age_s":      sync_age_s,
        "last_sync_error": snap.last_sync_error,

        # F1 (server-resolves "local" → IANA): the browser never has to
        # guess. {tz, resolved_tz, offset_label, offset_seconds} computed
        # at the snapshot's `generated_at`. Always present, even when
        # cw / fc are None.
        "display":         display_block,

        "header": {
            "week_label":         week_lbl,
            "used_pct":           used_pct,
            "five_hour_pct":      five_hr,
            "dollar_per_pct":     dollar_pp,
            "forecast_pct":       header_fcast_pct,
            "forecast_verdict":   verdict,
            "vs_last_week_delta": (
                _current_trend.delta_dpp if _current_trend is not None else None
            ),
        },

        "current_week":
            None if cw is None else {
                "used_pct":                 cw.used_pct,
                "five_hour_pct":            cw.five_hour_pct,
                "five_hour_resets_in_sec":
                    None if cw.five_hour_resets_at is None
                    else max(0, int((cw.five_hour_resets_at - now_utc).total_seconds())),
                "spent_usd":                cw.spent_usd,
                "dollar_per_pct":           cw.dollars_per_percent,
                "reset_at_utc":             _iso_z(reset_at_utc),
                "reset_in_sec":
                    None if reset_at_utc is None
                    else max(0, int((reset_at_utc - now_utc).total_seconds())),
                "last_snapshot_age_sec":
                    None if cw.latest_snapshot_at is None
                    else max(0, int((now_utc - cw.latest_snapshot_at).total_seconds())),
                "freshness":                cw_freshness,
                # Current 5h block (spec §4.1) — populated upstream by
                # `_tui_build_current_week`. `getattr` with default keeps
                # legacy fixture modules that construct TuiCurrentWeek
                # directly (without the new field) compatible.
                "five_hour_block":          getattr(cw, "five_hour_block", None),
                "milestones": [
                    {
                        "percent":                m.percent,
                        "crossed_at_utc":         _iso_z(m.crossed_at),
                        "cumulative_usd":         round(m.cumulative_cost_usd, 4),
                        "marginal_usd":
                            None if m.marginal_cost_usd is None
                            else round(m.marginal_cost_usd, 4),
                        "five_hour_pct_at_cross": m.five_hour_pct_at_crossing,
                    }
                    for m in (snap.percent_milestones or [])
                ],
                # Spec §5.3 (Codex r1 finding 3) — NEW envelope key
                # parallel to ``milestones`` (which carries the WEEKLY
                # timeline). 5h-block milestones for the active block,
                # in capture-time order, both pre- and post-credit
                # segments included (bucket B per §3.2 — no
                # ``reset_event_id`` filter; the React layer renders
                # repeated thresholds as distinct rows keyed on
                # ``reset_event_id``). Empty list when no 5h block is
                # bound or the data source crashed during sync
                # (recorded on ``last_sync_error``).
                "five_hour_milestones":     getattr(snap, "five_hour_milestones", []) or [],
            },

        "forecast":
            None if fc is None else {
                "verdict":                     verdict,
                "week_avg_projection_pct":     fcast_pct,
                "recent_24h_projection_pct":   recent_24h_pct,
                "budget_100_per_day_usd":      budget_100,
                "budget_90_per_day_usd":       budget_90,
                "confidence":                  confidence or "unknown",
                # Map the binary ForecastOutput.confidence ("high" / "low") to
                # a 7-dot fill scale for the dashboard footer. ForecastOutput
                # has no numeric grade, so we pick a visually meaningful floor
                # for "low" (≈30% filled) and a full bar for "high".
                "confidence_score":            (7 if confidence == "high"
                                                else 2 if confidence == "low"
                                                else 0),
                "explain":                     sys.modules["cctally"]._build_forecast_json_payload(fc),
            },

        "trend":
            None if not snap.trend else {
                "weeks": [
                    {
                        "label":          w.week_label,
                        "used_pct":       w.used_pct,
                        "dollar_per_pct": w.dollars_per_percent,
                        "delta":          w.delta_dpp,
                        "is_current":     bool(w.is_current),
                        # #264 S3: additive weekly cost (already on the row via
                        # build_trend_view); rendered by the Trend modal's Cost
                        # column. ``getattr`` (like ``project_key`` /
                        # ``trend_history_median_dpp``) tolerates minimal trend
                        # shapes — SimpleNamespace test rows / older fixtures —
                        # that predate this nullable field. ``None`` when the
                        # week has no cost snapshot.
                        "cost_usd":       round(_wc, 4) if (_wc := getattr(w, "weekly_cost_usd", None)) is not None else None,
                    }
                    for w in snap.trend
                ],
                "spark_heights": [w.spark_height for w in snap.trend],
                "history": [
                    {
                        "label":          w.week_label,
                        "used_pct":       w.used_pct,
                        "dollar_per_pct": w.dollars_per_percent,
                        "delta":          w.delta_dpp,
                        "is_current":     bool(w.is_current),
                        # #264 S3: additive weekly cost (see weeks[] above; same
                        # getattr tolerance for minimal/older trend shapes).
                        "cost_usd":       round(_wc, 4) if (_wc := getattr(w, "weekly_cost_usd", None)) is not None else None,
                    }
                    for w in (snap.weekly_history or [])
                ],
                # View-model unification (Bundle 1; spec §6.6): the
                # pre-computed 3-sample $/% mean. TrendPanel reads this
                # instead of re-deriving the panel-average.
                "avg_dollars_per_pct": snap.trend_avg_dollars_per_pct,
                # Issue #59 follow-up: pre-computed 4-week-median of
                # non-current ``dollars_per_percent`` over the 12-row
                # history. TrendModal.tsx's ``median4NonCurrent`` helper
                # used to compute this client-side; pre-computing on
                # ``build_trend_view`` keeps the rule
                # (``sort(last 4 non-current dpps)``, midpoint
                # ``(s[1]+s[2])/2``) in one place. ``None`` when fewer
                # than 4 valid non-current samples — modal's client-side
                # fallback handles the ``null`` case.
                "history_median_dpp":
                    getattr(snap, "trend_history_median_dpp", None),
            },

        "weekly":  weekly_env,
        "monthly": monthly_env,
        "blocks":  blocks_env,
        "daily":   daily_env,

        "sessions": {
            "total":    len(snap.sessions),
            "sort_key": "started_desc",
            "rows": [
                {
                    "session_id":   s.session_id,
                    "started_utc":  _iso_z(s.started_at),
                    "duration_min": int(round(s.duration_minutes)) if s.duration_minutes else 0,
                    "model":        s.model_primary,
                    "project":      s.project_label,
                    # Projects-panel cross-nav identity (spec §4.1).
                    # Disambiguated display_key matching the projects
                    # envelope's `current_week.rows[].key`; ``None`` when
                    # the projects envelope sub-build failed, the row's
                    # project_path is missing, or the lookup didn't
                    # resolve (the React cell falls back to plain text).
                    "project_key":  getattr(s, "project_key", None),
                    "cost_usd":     round(s.cost_usd, 4) if s.cost_usd is not None else None,
                    # #264 S3: cache-hit % is a plain metric — always serialized
                    # (never gated). ``None`` when the row has no denominator.
                    "cache_hit_pct": round(s.cache_hit_pct, 1) if s.cache_hit_pct is not None else None,
                    # The session title is transcript-derived content: emit the
                    # `title` KEY only when the per-request transcript gate is
                    # open AND a title exists. Omitted (not null) otherwise, so
                    # a gated-off or untitled session renders the client's
                    # em-dash fallback and committed goldens (empty conversation
                    # fixtures -> no title) never carry a `title` key. Default
                    # transcripts_visible=False therefore fails fully closed.
                    **({"title": getattr(s, "title", None)}
                       if (transcripts_visible
                           and getattr(s, "title", None) is not None)
                       else {}),
                }
                for s in snap.sessions
            ],
        },

        # Projects panel + modal envelope block (spec §5.2).
        # Populated on the sync thread; the serializer reads it back
        # unchanged so it stays a pure renderer (no DB I/O). ``None``
        # on first tick before sync completes; the client renders the
        # panel-empty state in that case.
        "projects":         getattr(snap, "projects_envelope", None),

        # Cache-report panel + modal envelope block (spec
        # 2026-05-21-cache-report-panel-design.md §4.2). Snake_case
        # keys throughout — the envelope is intentionally snake_case
        # end-to-end (envelope.ts:189). ``None`` on first tick before
        # sync completes; the client renders the panel-empty state in
        # that case. envelope_version stays at 2 (additive optional
        # field, matches the update? / doctor? precedent).
        "cache_report":     _cache_report_snapshot_to_dict(
            getattr(snap, "cache_report", None)
        ),

        # threshold-actions T5: see prelude above for rationale.
        "alerts":           alerts_array,
        "alerts_settings":  alerts_settings,

        # Dashboard render-prefs mirror (cache-failure-markers opt-out, spec
        # §5). Additive optional, like alerts_settings — envelope_version
        # stays at 2. The client derives `markersEnabled` from this, defaulting
        # to true when the field is undefined (older server / first tick).
        "dashboard_prefs":  dashboard_prefs,

        # update-subcommand SSE mirror (see comment above the
        # `_load_update_state()` block). Shape matches GET
        # /api/update/status's payload (`{state, suppress}`) so the
        # dashboard client's existing coerceUpdateState/Suppress logic
        # consumes both surfaces uniformly.
        "update":           update_envelope,

        # Doctor aggregate-only block (spec §5.5). Full per-check
        # report fetched lazily via GET /api/doctor.
        "doctor":           doctor_envelope,

        # Preview channel (CCTALLY_CHANNEL=preview): additive-optional
        # `channel` key. Omitted when the marker is unset so prod payloads
        # (and the dashboard goldens) stay byte-identical; feeds the header
        # PREVIEW badge when set.
        **sys.modules["_cctally_dashboard"]._channel_env_fragment(),
    }


def _session_detail_to_envelope(detail: "TuiSessionDetail") -> dict:
    """Serialize TuiSessionDetail for GET /api/session/:id (spec §3.2, §4.6.4).

    Field names stay parallel to the live ``sessions.rows[]`` envelope
    (``session_id``, ``started_utc``, ``cost_usd``) so the JS client can
    treat this as an enrichment of a session row. ``models`` is flattened
    from ``list[tuple[str, str]]`` to ``list[{name, role}]`` for JSON
    palatability. ``cost_per_model`` is similarly flattened.

    Pure function — no I/O, no clocks. Dataclass fields mirror exactly
    (``started_at`` → ``started_utc``, ``last_activity_at`` →
    ``last_activity_utc``, ``duration_minutes`` → ``duration_min`` as an
    int for parity with sessions.rows[]). There is no per-entry array —
    the underlying aggregator is session-level (spec §4.6.4).
    """
    return {
        "session_id":            detail.session_id,
        "started_utc":           _iso_z(detail.started_at),
        "last_activity_utc":     _iso_z(detail.last_activity_at),
        "duration_min":          int(round(detail.duration_minutes)) if detail.duration_minutes else 0,
        "project_label":         detail.project_label,
        "project_path":          detail.project_path,
        "source_paths":          list(detail.source_paths),
        "models": [
            {"name": name, "role": role}
            for (name, role) in detail.models
        ],
        "input_tokens":          detail.input_tokens,
        "cache_creation_tokens": detail.cache_creation_tokens,
        "cache_read_tokens":     detail.cache_read_tokens,
        "output_tokens":         detail.output_tokens,
        "cache_hit_pct":         detail.cache_hit_pct,
        "cost_per_model": [
            {"model": name, "cost_usd": round(cost, 4)}
            for (name, cost) in detail.cost_per_model
        ],
        "cost_total_usd":        round(detail.cost_total_usd, 4),
    }
