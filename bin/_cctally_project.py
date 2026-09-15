"""`cctally project` subcommand entry point.

Lazy I/O sibling: holds `cmd_project` + its 4 dedicated helpers
(`_load_week_snapshots`, `_accumulate_entry_into_bucket`,
`_project_json_output`, `_project_sort_key`). Aggregates session entries
by git-root project with per-project weekly usage attribution.

Honest imports are KERNEL-ONLY (`_cctally_core`). Every other symbol the
command calls is reached via the call-time `_cctally()` accessor so test
monkeypatches through `cctally`'s namespace are preserved — see the spec
§3.1 disposition table (the cache reads, the share builders/dispatch,
`_share_validate_args`, `_render_project_table`, `resolve_display_tz`,
and the `bin/cctally`-resident helpers all route through `c.`).

bin/cctally re-exports `cmd_project` (eager) so the parser's
`set_defaults(func=c.cmd_project)` resolves unchanged.

Spec: docs/superpowers/specs/2026-05-30-extract-project-cmd-design.md
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import sqlite3
import sys

from _cctally_core import (
    _command_as_of,
    eprint,
    latest_usage_by_segment,
    open_db,
    parse_iso_datetime,
)
from _lib_fmt import stable_sum

# #620 S1 D11: the one affordance shape every warning state renders.
import _lib_alert_scope


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


# ---------------------------------------------------------------------------
# Modelled quota attribution (#661 S2 spec §5)
#
# `Used %` used to be a project's share of the window's DOLLARS, scaled by the
# week's meter reading. Cache reads are far cheaper per token than output
# under the model's weights, so that proxy over-credits a cache-heavy project
# and under-credits an output-heavy Opus one: it reports a cost share under a
# column whose name says quota.
# ---------------------------------------------------------------------------

#: Why modelled quota is not published for a week or a whole run. A closed
#: set. `account-not-resolved` is the run-level one (§5.3); the rest are
#: per-week and are the reasons that week fell back to the cost share.
ATTRIBUTION_CAUSES = ("account-not-resolved", "calibration-absent",
                      "regime-boundary", "unsupported-composition",
                      "no-local-history")

#: The three bases a row or a run can carry.
ATTRIBUTION_BASES = ("modelled", "cost-share", "withheld")

#: The cause a residual states when it cannot be computed against a whole,
#: single-account, unfiltered population. A residual over a partial
#: population is not a residual.
RESIDUAL_MISALIGNED = "population-misaligned"

#: The other two reasons a residual is absent, which are NOT misalignment
#: (#661 S2 Stage C review). Spec §5.2 states the first outright — "an
#: absent observed side withholds on its own" — as a condition separate from
#: the account, window and filter alignment `RESIDUAL_MISALIGNED` names. All
#: three used to render as `population-misaligned`, and once the §5.2 footer
#: put that code in front of a terminal user as the sentence "the account,
#: the window or the population does not align", a run whose only defect was
#: a modelled week without a meter snapshot told the user something false
#: about their own request. This is the same defect class Stage C fixed for
#: `credit-baseline-absent` versus `reset-baseline-absent`: the withholding
#: is right and the named cause is wrong.
RESIDUAL_OBSERVED_ABSENT = "observed-absent"
RESIDUAL_NO_MODELLED_WEEKS = "no-modelled-weeks"

#: The closed set of residual causes, asserted by the tests exactly as
#: `MOVEMENT_WITHHELD_CAUSES` is. A member with no copy in
#: `_ATTRIBUTION_CAUSE_COPY` renders as its own code, which is readable but
#: is not the sentence the footer owes a terminal reader.
RESIDUAL_CAUSES = (RESIDUAL_MISALIGNED, RESIDUAL_OBSERVED_ABSENT,
                   RESIDUAL_NO_MODELLED_WEEKS)

#: Bumped from 1 because `attributedUsedPercent` and `costPerPercent` keep
#: their spellings and change their MEANING, from a share of the window's
#: dollars to modelled weekly quota. `docs/cli-contract.md` classifies a
#: changed value meaning as breaking. The additive `attribution` block and
#: the per-row `attributionBasis` would not on their own have required it.
PROJECT_JSON_SCHEMA_VERSION = 2


class WeekAttribution:
    """How ONE subscription week's `Used %` is measured.

    `units_per_point` is set only on the `modelled` basis; a cost-share week
    carries the typed cause of its fallback instead.
    """

    __slots__ = ("basis", "cause", "units_per_point")

    def __init__(self, basis, cause, units_per_point):
        self.basis = basis
        self.cause = cause
        self.units_per_point = units_per_point

    def __repr__(self):                                # pragma: no cover
        return (f"WeekAttribution(basis={self.basis!r}, cause={self.cause!r},"
                f" units_per_point={self.units_per_point!r})")


def _entry_quota_record(entry):
    """`(EntryRecord, weighted_units)` for one joined cache entry.

    `units` is None when the entry contributes nothing to the general weekly
    meter — a family that does not drain it, or a cache-write split the store
    cannot supply. Both are exactly what `population_units` skips, so the
    per-project parts keep summing to the population's own units rather than
    drifting above it.

    This runs on the RAW entry, before `_accumulate_entry_into_bucket`, which
    drops the one-hour cache-write split. Weighting an aggregate bucket would
    silently mis-price every cache-heavy project — the failure spec §5.1 names.
    """
    qm = _cctally()._load_sibling("_lib_quota_model")
    model = str(getattr(entry, "model", "") or "")
    if qm.family_participation(qm.normalize_family(model)) != "general":
        return None, None
    record = qm.EntryRecord(
        at=entry.timestamp,
        model=model,
        fresh=getattr(entry, "input_tokens", 0) or 0,
        output=getattr(entry, "output_tokens", 0) or 0,
        cache_create_total=getattr(entry, "cache_creation_tokens", 0) or 0,
        cache_1h=getattr(entry, "cache_1h_tokens", None),
        cache_read=getattr(entry, "cache_read_tokens", 0) or 0,
    )
    return record, qm.weighted_units(record)


def probe_provider_decoration(conn, provider: str) -> bool:
    """Whether `provider` renders account decoration, failing CLOSED.

    Spec §5.3 hangs on this answer: `False` lets a merged read publish a
    number under `Used %`, and on a genuinely decorated store that number is
    the cost share F1 exists to remove. So only ONE condition may relax the
    gate — the `accounts` table not being there at all, which really does
    mean the store cannot hold more than one real account, and which several
    hand-built fixtures are thin enough to hit.

    Catching `sqlite3.Error` around the decoration query itself was much
    wider than that: it also covers `database is locked`, `file is not a
    database`, `disk I/O error` and `no such column`, and a WAL-contended
    two-account store answering `False` is exactly the publication §5.3
    forbids. Every such failure therefore answers `True`, which withholds.
    The table probe is the `PRAGMA table_info` form `_snapshot_columns` uses.
    """
    c = _cctally()
    try:
        present = bool(conn.execute("PRAGMA table_info(accounts)").fetchall())
    except sqlite3.Error:
        return True
    if not present:
        return False
    try:
        return bool(c.provider_is_decorated(conn, provider))
    except sqlite3.Error:
        return True


def resolve_attribution_account_gate(*, account_key, decorated):
    """`(basis, cause)` when the run cannot model at all, else `(None, None)`.

    Spec §5.3. `project` without `--account` passes `account_key=None`, which
    means MERGED, and S1 publishes no valid merged calibration. On a decorated
    multi-account install, modelled quota is therefore withheld and
    `--account` is required: silently falling back to the cost share there
    would keep publishing the very number F1 exists to remove, under a column
    the acceptance criterion says reports the correct account.

    At a single real account the #341 R8 gate means nothing decorates and the
    merged path IS the account path, so this affects only genuinely
    multi-account installs.
    """
    if account_key is None and decorated:
        return "withheld", "account-not-resolved"
    return None, None


def resolve_week_attribution(regime, records, *, week_start, week_end):
    """Whether one subscription week can be modelled, and at what rate.

    A week whose entries do not all fall inside the regime's half-open
    interval falls back for the WHOLE week. The validated reader publishes
    only the OPEN regime, so the earlier segment has no rate at all, and
    spec §5.1 forbids producing a partial-week mixture — modelling half a
    week and cost-sharing the other half would publish a figure that is
    neither.

    Support is re-tested over the whole account-week population through
    §1.1's apply adapter rather than per project, and rather than inherited
    from the regime's stored status, which describes S1's fit population and
    not this one.
    """
    if regime is None:
        return WeekAttribution("cost-share", "calibration-absent", None)
    if week_start < regime.effective_from:
        return WeekAttribution("cost-share", "regime-boundary", None)
    if regime.effective_until is not None \
            and week_end > regime.effective_until:
        return WeekAttribution("cost-share", "regime-boundary", None)
    qcg = _cctally()._load_sibling("_cctally_quota_calibration")
    applied = qcg.apply_regime(regime, list(records))
    if isinstance(applied, qcg.ApplyRejection):
        return WeekAttribution("cost-share", applied.value, None)
    return WeekAttribution("modelled", None, regime.units_per_point)


def range_covers_whole_weeks(since, until, bounds, *, now) -> bool:
    """Whether `[since, until]` slices no subscription week it touches.

    `whole_weeks` used to be `not weeks_missing_snapshot`, which asks a
    different question — whether every week has a snapshot. A three-day
    `--since 2026-06-03 --until 2026-06-05` over one fully-snapshotted week
    passed it, and §5.2's residual was then published against a meter
    reading covering the whole week and a modelled population covering three
    days of it. That is exactly the misalignment §5.2 names.

    The snapshot condition is not lost by the replacement: the residual's
    observed side is already `None` unless every modelled week carries a
    snapshot, and a `None` observed side withholds on its own.

    The OPEN week is covered whole by a range running to `now`, because the
    meter reading and the local entries both stop there and neither side is
    clipped relative to the other. A closed week needs the range to reach
    its recorded end.
    """
    if not bounds:
        return False
    if since > bounds[0][0]:
        return False
    last_end = bounds[-1][1]
    if last_end > now:
        return until >= now
    # `until` is an INCLUSIVE instant and a week's end is EXCLUSIVE, so a
    # date-only `--until 2026-06-07` arrives as 23:59:59.999999 and covers a
    # week ending 2026-06-08T00:00:00 exactly. One microsecond is the
    # resolution `datetime` has, not a tolerance.
    return until >= last_end - dt.timedelta(microseconds=1)


def residual_withholding_cause(*, account_resolved, whole_weeks, filtered,
                               fallback_weeks):
    """None when the observed-minus-modelled residual may be stated.

    Spec §5.2: only when the account, the window and the population align —
    a single resolved account, whole subscription weeks, and no fallback or
    filter splitting the population.
    """
    if not account_resolved or not whole_weeks or filtered or fallback_weeks:
        return RESIDUAL_MISALIGNED
    return None


#: Human copy for the typed causes the footer renders. A cause with no entry
#: renders as its own code, which is readable and never blank.
_ATTRIBUTION_CAUSE_COPY: dict = {
    "account-not-resolved":
        "this install has more than one account and the read is merged; "
        "pass --account to model quota",
    "calibration-absent": "no usable quota calibration on this install",
    "regime-boundary": "the week crosses a metering-rate boundary",
    "unsupported-composition":
        "the week's model mix sits outside the calibration's support",
    "no-local-history": "no local entries to model",
    RESIDUAL_MISALIGNED:
        "the account, the window or the population does not align",
    RESIDUAL_OBSERVED_ABSENT:
        "at least one modelled week carries no meter snapshot, so there is "
        "no observed side to subtract from",
    RESIDUAL_NO_MODELLED_WEEKS:
        "this window models no subscription week",
}


def _cause_copy(cause) -> str:
    if cause is None:
        return "no cause stated"
    return _ATTRIBUTION_CAUSE_COPY.get(cause, str(cause))


def render_attribution_footer(totals, *, basis, cause) -> "list[str]":
    """Spec §5.2's footer lines for the terminal `project` table.

    The four quantities are named separately rather than conflated into "the
    rows do not add up", and the residual is stated with its SIGN AS
    MEASURED and never with a direction: §2.2 measured that difference with
    both signs, so a footer asserting one would be false half the time. The
    same sentence says outright that the difference does not identify or
    estimate off-machine usage, because §2.2's probe in that direction is
    structurally blind and §2.2 forbids the claim.

    Returns a list of lines so the caller appends without an inline guard.
    A withheld quantity states its cause and no number; it is absent rather
    than zero.
    """
    totals = totals or {}
    lines: list[str] = []
    if basis == "withheld":
        lines.append(
            f"Used %: modelled quota withheld \u2014 {_cause_copy(cause)}.")
    else:
        modelled = totals.get("modelledWeekPoints")
        visible = totals.get("visibleRowPoints")
        unmodelled = totals.get("filteredOrUnmodelledPoints")
        if modelled is None:
            lines.append(
                "Modelled quota: withheld \u2014 "
                f"{_cause_copy(cause)}. Used % is a cost share.")
        else:
            parts = [f"{modelled:,.2f} points across the modelled cycles"]
            if visible is not None:
                parts.append(f"{visible:,.2f} in the rows listed")
            if unmodelled is not None:
                parts.append(
                    f"{unmodelled:,.2f} filtered or unmodelled")
            lines.append("Modelled quota: " + "; ".join(parts) + ".")
    residual = totals.get("observedMinusModelledPoints")
    residual_cause = totals.get("residualCause")
    if residual is None:
        lines.append(
            "Observed meter minus modelled local quota: withheld \u2014 "
            f"{_cause_copy(residual_cause)}.")
    else:
        lines.append(
            "Observed meter minus modelled local quota: "
            f"{residual:+,.2f} points, as measured. This is a difference "
            "between two quantities, not an identification or an estimate "
            "of usage from another machine.")
    return lines


def residual_absence_cause(*, observed, modelled):
    """Why `observed_minus_modelled` returned None, or None when it did not.

    A pure classifier over the two operands, so the caller states which side
    is missing instead of reporting every absence as misalignment. The
    modelled side is checked first because when BOTH are absent the window
    modelled nothing at all, and naming the observed side there would blame
    the meter for a window that asked nothing of it.
    """
    if modelled is None:
        return RESIDUAL_NO_MODELLED_WEEKS
    if observed is None:
        return RESIDUAL_OBSERVED_ABSENT
    return None


def observed_minus_modelled(*, observed, modelled):
    """The meter's reading minus the modelled local points, or None.

    §2.2 measured this difference with BOTH signs, so no caller may assume it
    has one, and no surface may state it as off-machine usage. It is the
    difference itself, named as such.
    """
    if observed is None or modelled is None:
        return None
    return observed - modelled


def _load_week_snapshots(
    subweeks, *, account_key: "str | None" = None,
) -> dict[dt.datetime, float]:
    """Return ``{segment start (UTC) -> that cycle's weekly_percent}``.

    #750 S4 §2.1. This used to return ``{week_start_utc -> max(weekly_percent)}``
    keyed on `weekly_usage_snapshots.week_start_at`, while `cmd_project` looked
    each week up with a `SubWeek.start_ts` instant. On a credited week those are
    different quantities: the post-credit segment matched nothing and landed in
    `weeks_missing_snapshot`, and the pre-credit segment received the
    POST-credit reading. That is #731.

    It is now a thin wrapper over `latest_usage_by_segment`, the shared reducer
    the dashboard projects panel also calls, so the CLI and the panel resolve a
    cycle's percentage through one implementation instead of two. The keys are
    parsed back to datetimes because that is `cmd_project`'s bucket identity,
    and a segment with no observation inside its own interval is ABSENT rather
    than carrying a neighbour's reading — which is what lets
    `weeks_missing_snapshot` report one missing cycle per cut.

    This wrapper is the leg that opens its own connection; the reducer takes
    the caller's, because the dashboard already holds a transaction.
    """
    conn = open_db()
    try:
        by_segment = latest_usage_by_segment(
            conn, subweeks, account_key=account_key)
    finally:
        conn.close()
    out: dict[dt.datetime, float] = {}
    for segment_key, pct in by_segment.items():
        if pct is None:
            continue
        try:
            out[parse_iso_datetime(
                segment_key, "subweek.segment_key",
            ).astimezone(dt.timezone.utc)] = float(pct)
        except (TypeError, ValueError):
            continue
    return out


def _sum_cost_by_project(
    start: dt.datetime,
    now: dt.datetime,
    mode: str = "auto",
    skip_sync: bool = False,
) -> dict[str, float]:
    """Return ``{canonical_git_root: spent_usd}`` over ``[start, now]``.

    ONE scan over the joined session entries (the same iterator
    ``cmd_project`` walks), bucketed in Python by each entry's resolved
    git-root (``_resolve_project_key`` — a filesystem ``.git`` walk, NOT a
    SQL ``GROUP BY``), with per-entry cost computed via the same
    ``_calculate_entry_cost(model, usage, mode=...)`` path ``cmd_project``
    uses (so pricing edits flow through uniformly). Keys are the resolved
    ``ProjectKey.bucket_path`` (the canonical git-root when a ``.git`` is
    found, else the normalized path) — identical to how ``cmd_project``
    keys its rows, so configured ``budget.projects`` keys match by string
    equality.

    Synthetic entries (Claude Code internal markers) are skipped, mirroring
    ``cmd_project`` / the other ``_JoinedClaudeEntry`` aggregators. A
    configured project with no in-range entry simply never appears in the
    returned map (the caller renders it as a ``$0`` row — spec §7.2).

    Shared by the per-project budget display (§7.2, ``cmd_budget``) and the
    alert-firing path (§6.4); ``skip_sync`` threads through to
    ``get_claude_session_entries`` so the record-tick caller can reuse a
    cache already warmed earlier in the same tick.
    """
    c = _cctally()
    resolver_cache: dict[str, ProjectKey] = {}
    out: dict[str, float] = {}
    for entry in c.get_claude_session_entries(start, now, skip_sync=skip_sync):
        if entry.model == "<synthetic>":
            continue
        cost = c._calculate_entry_cost(
            entry.model,
            c.claude_usage_dict(   # #195 chokepoint
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
                speed=getattr(entry, "speed", None),
            ),
            mode=mode,
            cost_usd=entry.cost_usd,
        )
        key = c._resolve_project_key(entry.project_path, "git-root", resolver_cache)
        out[key.bucket_path] = out.get(key.bucket_path, 0.0) + cost
    return out


def _project_budget_labels(keys):
    """Collision-aware ``{project_key: label}`` for a set of budget project
    keys. Single source of the resolve+disambiguate primitive used by the
    budget table (`_build_project_budget_rows`), the alert payload
    (`maybe_record_project_budget_milestone`), and the dashboard SSE envelope
    (`_envelope_rows_project_budget`) — issue #130. Each caller passes its own
    key feed; the label for a key is identical across callers only when they
    feed the same key set (the dashboard intentionally feeds its alerted-row
    subset). Output is order-independent (disambiguation keys off basename
    collisions, not position)."""
    c = _cctally()
    keys = list(keys)
    resolver_cache: dict = {}
    pkeys = [c._resolve_project_key(k, "git-root", resolver_cache) for k in keys]
    disambig = c._project_disambiguate_labels([{"key": pk} for pk in pkeys])
    return {
        keys[i]: disambig.get(i, pkeys[i].display_key)
        for i in range(len(keys))
    }


def _accumulate_entry_into_bucket(
    b: dict,
    entry: "_JoinedClaudeEntry",
    pre_computed_cost: float | None = None,
) -> None:
    """Add one joined-Claude entry's tokens, cost, session-id, and timestamps
    into a project×week bucket dict.

    Cost is computed via the same `_calculate_entry_cost(model, usage_dict,
    mode="auto", cost_usd=...)` path used by `_aggregate_cache_by_session`
    (the other `_JoinedClaudeEntry` consumer) so pricing updates flow through
    uniformly. Per-model sub-buckets mirror the parent bucket's shape.

    `pre_computed_cost`: if callers have already invoked `_calculate_entry_cost`
    for this entry (e.g. to also feed the attribution denominator in
    `cmd_project`), pass it in to avoid double work.
    """
    c = _cctally()
    # Mirror `_aggregate_claude_sessions`: NULL session_id falls back to the
    # source-file basename so distinct files don't collapse into one bucket.
    if entry.session_id:
        sid = entry.session_id
    else:
        sid = os.path.splitext(os.path.basename(entry.source_path))[0]
    b["sessions"].add(sid)
    if entry.timestamp < b["first_seen"]:
        b["first_seen"] = entry.timestamp
    if entry.timestamp > b["last_seen"]:
        b["last_seen"] = entry.timestamp
    b["input"] += entry.input_tokens
    b["output"] += entry.output_tokens
    b["cache_write"] += entry.cache_creation_tokens
    b["cache_read"] += entry.cache_read_tokens
    if pre_computed_cost is not None:
        cost = pre_computed_cost
    else:
        cost = c._calculate_entry_cost(
            entry.model,
            c.claude_usage_dict(   # #195 chokepoint
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
                speed=getattr(entry, "speed", None),
            ),
            mode="auto",
            cost_usd=entry.cost_usd,
        )
    b["cost_usd"] += cost
    model = entry.model or "(unknown-model)"
    mb = b["models"].get(model)
    if mb is None:
        mb = {
            "cost_usd": 0.0,
            "input": 0, "output": 0,
            "cache_write": 0, "cache_read": 0,
            "first_seen": entry.timestamp, "last_seen": entry.timestamp,
        }
        b["models"][model] = mb
    if entry.timestamp < mb["first_seen"]:
        mb["first_seen"] = entry.timestamp
    if entry.timestamp > mb["last_seen"]:
        mb["last_seen"] = entry.timestamp
    mb["cost_usd"] += cost
    mb["input"] += entry.input_tokens
    mb["output"] += entry.output_tokens
    mb["cache_write"] += entry.cache_creation_tokens
    mb["cache_read"] += entry.cache_read_tokens


def _project_json_payload(
    *,
    since: dt.datetime,
    until: dt.datetime,
    weeks_in_range: int,
    group_mode: str,
    rows: list[dict],
    weeks_missing_snapshot: set[dt.datetime],
    warnings: list[str],
    include_breakdown: bool,
    week_snapshots: dict[dt.datetime, float],
    attribution_basis: str = "cost-share",
    attribution_cause: "str | None" = None,
    attribution_totals: "dict | None" = None,
) -> dict:
    """Build the project subcommand's --json payload per spec §4.

    `schemaVersion` is 2 from #661 S2. `attributedUsedPercent` and
    `costPerPercent` keep their spellings and change their MEANING, from a
    share of the window's dollars to modelled weekly quota, and
    `docs/cli-contract.md` classifies a changed value meaning as breaking.
    The `attribution` block and the per-row `attributionBasis` are the v2
    additions that say which measure a given payload actually carries.

    Accepts rows already sorted by the caller (so ordering flags apply
    uniformly to both terminal and JSON modes). Aggregates `totals.costUsd`
    from `rows` and `totals.usedPercent` from `week_snapshots` (sum over
    all weeks with snapshots in the range — matches the conservation-law
    denominator used by per-project attribution). `models[]` is included
    per-project only when `--breakdown` is requested to avoid payload bloat.
    """
    total_cost = stable_sum(r["cost_usd"] for r in rows)
    # Aggregate used % across all weeks with snapshots in the range.
    total_used_pct: float | None
    if week_snapshots:
        total_used_pct = stable_sum(week_snapshots.values())
    else:
        total_used_pct = None

    def _fmt_dt(ts: dt.datetime) -> str:
        return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    projects_json = []
    for row in rows:  # rows come already sorted by caller
        p = {
            "displayKey": row["key"].display_key,
            "projectPath": row["key"].bucket_path,
            "gitRoot": row["key"].git_root,
            "sessions": len(row["sessions"]),
            "firstSeen": _fmt_dt(row["first_seen"]),
            "lastSeen": _fmt_dt(row["last_seen"]),
            "inputTokens": row["input"],
            "outputTokens": row["output"],
            "cacheWriteTokens": row["cache_write"],
            "cacheReadTokens": row["cache_read"],
            "costUsd": round(row["cost_usd"], 4),
            "attributedUsedPercent": (
                round(row["attributed_pct"], 4)
                if row["attributed_pct"] is not None else None
            ),
            "costPerPercent": (
                round(row["cost_per_pct"], 4)
                if row["cost_per_pct"] is not None else None
            ),
            # A project can span weeks that resolved differently, so the
            # basis is a row field and not only a payload one. A row is
            # `modelled` only when EVERY contributing week was.
            "attributionBasis": row.get("attribution_basis", "cost-share"),
        }
        if include_breakdown:
            p["models"] = [
                {
                    "model": mname,
                    "firstSeen": _fmt_dt(mb["first_seen"]),
                    "lastSeen": _fmt_dt(mb["last_seen"]),
                    "inputTokens": mb["input"],
                    "outputTokens": mb["output"],
                    "cacheWriteTokens": mb["cache_write"],
                    "cacheReadTokens": mb["cache_read"],
                    "costUsd": round(mb["cost_usd"], 4),
                }
                for mname, mb in sorted(row["models"].items())
            ]
        projects_json.append(p)

    payload = {
        "rangeStart": since.date().isoformat(),
        "rangeEnd": until.date().isoformat(),
        "weeksInRange": weeks_in_range,
        "groupMode": group_mode,
        "totals": {
            "costUsd": round(total_cost, 4),
            "usedPercent": (
                round(total_used_pct, 4) if total_used_pct is not None else None
            ),
            "weeklyAttributionAvailable": len(weeks_missing_snapshot) == 0,
        },
        "attribution": {
            "basis": attribution_basis,
            "cause": attribution_cause,
            # The four quantities spec §5.2 names, which the earlier draft
            # conflated into "the rows do not add up". `residualCause` is
            # set whenever the residual could not be computed against a
            # whole, single-account, unfiltered population.
            "totals": dict(attribution_totals or {
                "modelledWeekPoints": None,
                "visibleRowPoints": None,
                "filteredOrUnmodelledPoints": None,
                "observedMinusModelledPoints": None,
                "residualCause": RESIDUAL_MISALIGNED,
            }),
        },
        "projects": projects_json,
        "warnings": warnings,
    }
    return _cctally().stamp_schema_version(
        payload, version=PROJECT_JSON_SCHEMA_VERSION)


def _project_json_output(**kwargs) -> str:
    """Serialize :func:`_project_json_payload` without changing CLI bytes."""
    return json.dumps(_project_json_payload(**kwargs), indent=2)


def _project_sort_key(row: dict, sort_by: str, order: str):
    """Return (primary, dname) where the primary is flipped to match
    ``order``. Tie-break on dname ascending regardless of direction.

    ``sort_by`` values align with argparse choices: cost|used|name|last-seen.
    """
    dname = row["key"].display_key.lower()
    sign = -1 if order == "desc" else 1
    if sort_by == "cost":
        return (sign * row["cost_usd"], dname)
    if sort_by == "used":
        v = row["attributed_pct"] if row["attributed_pct"] is not None else -1
        return (sign * v, dname)
    if sort_by == "last-seen":
        return (sign * row["last_seen"].timestamp(), dname)
    if sort_by == "name":
        # name is asc-natural; caller uses sorted(reverse=order=='desc').
        return (dname,)
    # Unreachable given argparse choices, but safe default.
    return (sign * row["cost_usd"], dname)


def cmd_project(args: argparse.Namespace) -> int:
    """Roll entries up by project (git-root) with per-project usage attribution."""
    c = _cctally()
    c._share_validate_args(args)
    config = c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz so the
    # existing resolve_display_tz precedence absorbs the new alias.
    c._bridge_z_into_tz(args, config)
    args._resolved_tz = c.resolve_display_tz(args, config)

    # Flag-combination validation (must run before any expensive work).
    if args.weeks is not None and args.weeks < 1:
        eprint("Error: --weeks must be >= 1")
        return 2
    if args.weeks is not None and (args.since or args.until):
        eprint("Error: --weeks cannot be combined with --since/--until")
        return 2
    if args.since and args.until:
        # Parse both as dates using the same multi-format helper shape used
        # elsewhere in the codebase so YYYY-MM-DD and YYYYMMDD both compare
        # correctly (string compare alone breaks across mixed formats).
        def _parse(raw: str) -> dt.date | None:
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    return dt.datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
            return None

        since_parsed = _parse(args.since)
        until_parsed = _parse(args.until)
        # Silent-skip if either date failed to parse: we only want to surface
        # a "range order" error here when both inputs are well-formed. Any
        # format error will be reported downstream by _parse_cli_date_range()
        # so the user sees the parse problem first (not a misleading order
        # complaint triggered by garbage input).
        if since_parsed is not None and until_parsed is not None and since_parsed > until_parsed:
            eprint("Error: --since must be <= --until")
            return 2

    now = _command_as_of()
    conn = open_db()

    # #341 --account: resolve the render filter (provider=claude; fail closed
    # with exit 3 when the entry cache is unavailable). None = merged.
    #
    # #620 S1 D1: resolved BEFORE the subscription-week walk below, not after
    # it. `_compute_subscription_weeks` takes an account context precisely so
    # one account's resets cannot re-anchor another's week walk, and passing
    # `None` while `--account` was in force bucketed one account's dollars
    # against the merged boundary set — which, when two accounts reset on
    # different weekdays, is neither account's. An invocation WITHOUT
    # `--account` still passes `None` and keeps today's merged boundaries.
    # Argument VALIDATION runs first. Task 4 moved `resolve_account_filter`
    # ahead of the range resolution because `_compute_subscription_weeks`
    # needs the account context to build the default window, and that
    # reordering also moved which error a user sees when both are wrong: a
    # malformed `--since` with an unavailable entry cache started exiting 3
    # instead of 2. Exit codes are contract (`docs/cli-contract.md`), and a
    # native-usage error outranks a staged environment failure because it is
    # the one the user can act on. Only the parse moves back in front; the
    # resolved account still precedes interval construction below.
    source_range = getattr(args, "_source_analytics_range", None)
    parsed_explicit_range = None
    if source_range is None and (args.since or args.until):
        parsed = c._parse_cli_date_range(args, now_utc=now)
        if isinstance(parsed, int):
            # Translate the ccusage-parity helper's exit 1 into project's own
            # native usage code 2 — project is cctally-native, not a ccusage
            # drop-in (docs/cli-contract.md; #279 S6 W2).
            return 2
        parsed_explicit_range = parsed

    acct_key, acct_exit = c.resolve_account_filter(
        args, "claude", needs_cache=True)
    if acct_exit is not None:
        return acct_exit

    # Resolve [since_dt, until_dt] in UTC.  All-provider project dispatch
    # resolves its calendar range once, then injects it here so Claude and
    # Codex describe precisely the same interval.  The normal Claude parser
    # remains unchanged for every user-facing legacy invocation.
    if source_range is not None:
        since_dt, until_dt = source_range
    elif parsed_explicit_range is not None:
        since_dt, until_dt = parsed_explicit_range
        since_dt = since_dt.astimezone(dt.timezone.utc)
        until_dt = until_dt.astimezone(dt.timezone.utc)
    else:
        # Default to the current subscription week; --weeks N extends
        # backwards over the REAL intervals.
        #
        # #620 S1. `since_dt = cw_start - 7 * (weeks - 1)` was short of the
        # truth by the accumulated shortfall of every drifted week in the
        # window — one day per six-day week, several days over `--weeks 12`.
        # `_compute_subscription_weeks` emits a leading extrapolated slice
        # covering whatever `since_dt` it is given, so that deficit was not
        # empty: every entry inside it was read and bucketed into a week the
        # panel does not have, `_load_week_snapshots` admitted the prior real
        # week (it selects every week OVERLAPPING the range) and summed its
        # whole percentage into `totals.usedPercent`, and the extra week with
        # no snapshot drove the rendered line "Used % unavailable for 1 week".
        #
        # The window is resolved the way the dashboard Projects panel
        # resolves it: one walk over one shared probe range, locate the
        # interval containing `now`, then take the `weeks` intervals ending
        # there. `rangeStart` and `weeksInRange` move as a result, which is
        # the point — a figure computed over the wrong population is what
        # this correction removes.
        weeks_back = args.weeks if args.weeks is not None else 1
        probe_start, _probe_end = c.subscription_window_probe_range(
            now, weeks_back,
        )
        # Widen by 1us so the emit loop fires when `now` is exactly at a reset
        # boundary (zero-width [now, now] makes Case A's `current < range_end`
        # false, which would otherwise wrongly fall through to the Monday
        # fallback for non-Monday-reset accounts).
        probe_weeks = c._compute_subscription_weeks(
            conn, probe_start, now + dt.timedelta(microseconds=1),
            config=config,
            # #341/#620 S1: the resolved `--account` scope, or None for the
            # merged (all-accounts) analytics read.
            account_key=acct_key,
        )
        probe_bounds: list[tuple[dt.datetime, dt.datetime]] = []
        for sw in probe_weeks:
            probe_bounds.append((
                parse_iso_datetime(sw.start_ts, "week.start_ts").astimezone(
                    dt.timezone.utc),
                parse_iso_datetime(sw.end_ts, "week.end_ts").astimezone(
                    dt.timezone.utc),
            ))
        cw_start = None
        for s_dt, e_dt in reversed(probe_bounds):
            if s_dt <= now < e_dt:
                cw_start = s_dt
        window = (
            c.subscription_window_ending_at(probe_bounds, cw_start, weeks_back)
            if cw_start is not None else []
        )
        if window:
            since_dt = window[0][0]
        else:
            # No interval covers `now` — the genuine no-anchor path. A
            # Monday-anchored week stepped back in seven-day multiples is
            # what that history actually looks like, and it is what the
            # panel falls back to as well.
            if cw_start is None:
                cw_start = (now - dt.timedelta(days=now.weekday())).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            since_dt = cw_start - dt.timedelta(days=7 * (weeks_back - 1))
        until_dt = now

    # Pre-compute subscription-week bounds for the query window so each entry
    # can be bucketed onto a canonical subscription-week start_ts. Mirrors
    # `_aggregate_weekly`'s bisect pattern (first-match-wins on overlap).
    subweeks = c._compute_subscription_weeks(
        conn, since_dt, until_dt, config=config,
        # #341/#620 S1: the resolved `--account` scope, or None for the merged
        # (all-accounts) analytics read.
        account_key=acct_key,
    )
    parsed_bounds: list[tuple[dt.datetime, dt.datetime]] = []
    for sw in subweeks:
        s_dt = parse_iso_datetime(sw.start_ts, "week.start_ts").astimezone(dt.timezone.utc)
        e_dt = parse_iso_datetime(sw.end_ts, "week.end_ts").astimezone(dt.timezone.utc)
        parsed_bounds.append((s_dt, e_dt))
    week_starts = [b[0] for b in parsed_bounds]

    def _week_start_for(ts: dt.datetime) -> dt.datetime | None:
        """Return the canonical subscription-week start_dt for `ts`, or None
        if `ts` falls outside every SubWeek interval (may happen near the
        boundaries of the requested [since_dt, until_dt] window)."""
        ts_utc = ts.astimezone(dt.timezone.utc)
        idx = bisect.bisect_right(week_starts, ts_utc) - 1
        if idx < 0:
            return None
        # First-match-wins on Anthropic reset-day-drift overlap (same
        # walk-back as `_aggregate_weekly`).
        while idx > 0:
            prev_start, prev_end = parsed_bounds[idx - 1]
            if prev_start <= ts_utc < prev_end:
                idx -= 1
            else:
                break
        s_dt, e_dt = parsed_bounds[idx]
        if s_dt <= ts_utc < e_dt:
            return s_dt
        return None

    # Pre-lower filter patterns (substring, OR semantics, repeatable).
    project_patterns = [p.lower() for p in (args.project or [])]
    model_patterns = [m.lower() for m in (args.model or [])]

    # Widen scan to full subscription-week bounds so the attribution
    # denominator includes ALL week cost, even entries outside the
    # user's [since_dt, until_dt] slice. Visible buckets are still
    # gated on the user slice below. Without this, a partial-week
    # --since/--until slice understates the denominator and inflates
    # every row's Used %.
    if parsed_bounds:
        scan_start = min(since_dt, parsed_bounds[0][0])
        scan_end = max(until_dt, parsed_bounds[-1][1])
    else:
        scan_start, scan_end = since_dt, until_dt

    resolver_cache: dict[str, ProjectKey] = {}
    buckets: dict[tuple[ProjectKey, dt.datetime], dict] = {}
    total_cost_by_week: dict[dt.datetime, float] = {}
    unknown_entry_count = 0
    missing_sid_count = 0

    # Issue #89: materialize the joined-entry iterator once so we can
    # (a) pre-compute the --debug report's scope (entries passing all
    # rendered-row filters — user slice + --model + --project) BEFORE
    # the aggregation loop runs and (b) preserve the existing
    # aggregation semantics (denominator widened to ALL entries; visible
    # rows only the post-filter subset). The list is small enough to
    # hold (entries already in memory via the cache row factory).
    joined_entries_all = list(c.get_claude_session_entries(
        scan_start, scan_end, account_key=acct_key))

    # Build the --debug report dataset: skip synthetic + out-of-window
    # entries, then apply --model and --project filters (mirroring the
    # exact predicate at the aggregation loop below). This must match
    # the rendered scope, NOT the denominator scope.
    if getattr(args, "debug", False):
        filtered_for_report = []
        for je in joined_entries_all:
            if je.model == "<synthetic>":
                continue
            if _week_start_for(je.timestamp) is None:
                continue
            if je.timestamp < since_dt or je.timestamp > until_dt:
                continue
            if model_patterns:
                mname = (je.model or "").lower()
                if not any(p in mname for p in model_patterns):
                    continue
            key_for_filter = c._resolve_project_key(
                je.project_path, args.group, resolver_cache,
            )
            if not c._project_filter_matches(key_for_filter, project_patterns):
                continue
            filtered_for_report.append(je)
        c._emit_debug_samples_if_set(
            args,
            [c._usage_entry_from_joined(je) for je in filtered_for_report],
            command_label="project",
        )

    # #661 S2 spec §5. The run-level gate first: on a decorated multi-account
    # install a merged read has no valid calibration to apply, so modelled
    # quota is withheld and `--account` is required rather than silently
    # falling back to the cost share.
    _decorated = probe_provider_decoration(conn, "claude")
    run_basis, run_cause = resolve_attribution_account_gate(
        account_key=acct_key, decorated=_decorated,
    )
    attribution_regime = None
    if run_basis is None:
        try:
            qcg = c._load_sibling("_cctally_quota_calibration")
            attribution_regime = qcg.read_calibration_file(
                account_key=acct_key).regime
        except Exception:                              # noqa: BLE001
            attribution_regime = None
    # Per-week weighted units, and the records the whole-week support test
    # runs over. Collected on EVERY entry — the denominator is the account's
    # whole week, not the user's slice.
    week_units_total: dict = {}
    week_records: dict = {}

    for entry in joined_entries_all:
        # Skip synthetic entries (Claude Code internal markers) to match
        # `_aggregate_cache_by_session` / `_aggregate_claude_sessions`.
        if entry.model == "<synthetic>":
            continue

        week_start = _week_start_for(entry.timestamp)
        if week_start is None:
            continue

        entry_cost = c._calculate_entry_cost(
            entry.model,
            c.claude_usage_dict(   # #195 chokepoint
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_creation_tokens=entry.cache_creation_tokens,
                cache_read_tokens=entry.cache_read_tokens,
                cache_1h_tokens=getattr(entry, "cache_1h_tokens", None),
                speed=getattr(entry, "speed", None),
            ),
            mode="auto",
            cost_usd=entry.cost_usd,
        )

        # Denominator: always contribute (whole-week attribution) so
        # `--model`/`--project`/partial-slice do NOT rescale it.
        total_cost_by_week[week_start] = (
            total_cost_by_week.get(week_start, 0.0) + entry_cost
        )

        # #661 S2 §5.1: weight BEFORE bucket aggregation, which drops the
        # one-hour cache-write split `weighted_units` needs. Same whole-week
        # scope as the cost denominator above, for the same reason.
        entry_record, entry_units = (
            _entry_quota_record(entry) if run_basis is None else (None, None))
        if entry_units is not None:
            week_units_total[week_start] = (
                week_units_total.get(week_start, 0.0) + entry_units)
            week_records.setdefault(week_start, []).append(entry_record)

        # User-slice gate: visible rows only include entries within
        # [since_dt, until_dt]. Entries outside the slice still
        # contributed to the denominator above.
        if entry.timestamp < since_dt or entry.timestamp > until_dt:
            continue

        if model_patterns:
            mname = (entry.model or "").lower()
            if not any(p in mname for p in model_patterns):
                continue

        key = c._resolve_project_key(entry.project_path, args.group, resolver_cache)
        if key.is_unknown:
            unknown_entry_count += 1

        # --project filter: match against display_key OR the underlying
        # path (git_root / bucket_path). Matching only display_key makes
        # basename-collision suffixes (e.g. `foo (repos)`) impossible to
        # select by their path segment.
        if project_patterns:
            dname = key.display_key.lower()
            pname = (key.git_root or key.bucket_path or "").lower()
            if not any((p in dname) or (p in pname) for p in project_patterns):
                continue

        if entry.session_id is None:
            missing_sid_count += 1

        bkey = (key, week_start)
        b = buckets.get(bkey)
        if b is None:
            b = {
                "key": key,
                "week_start": week_start,
                "sessions": set(),
                "first_seen": entry.timestamp,
                "last_seen": entry.timestamp,
                "input": 0, "output": 0,
                "cache_write": 0, "cache_read": 0,
                "cost_usd": 0.0,
                "units": 0.0,
                "models": {},
            }
            buckets[bkey] = b
        _accumulate_entry_into_bucket(b, entry, pre_computed_cost=entry_cost)
        if entry_units is not None:
            b["units"] += entry_units

    # The remediation moved OUT of these two sentences and into the shared
    # affordance line below (#620 S1 D11), so the terminal states the problem
    # once and the fix once. The `warnings` list the JSON payload carries is a
    # separate wire surface and keeps its own wording unchanged.
    if unknown_entry_count > 0:
        eprint(
            f"Warning: {unknown_entry_count} entries lacked project_path."
        )
    if missing_sid_count > 0:
        eprint(
            f"Warning: {missing_sid_count} entries lacked session_files "
            f"session_id."
        )
    # #620 S1 D11: state the remediation in the one shape every warning uses,
    # scoped to the provider whose cache is short and the range that was read.
    # The `warnings` list the JSON payload carries is deliberately untouched;
    # it is a wire surface, and this is the terminal affordance.
    resolved_source = getattr(args, "source", None) or "claude"
    if unknown_entry_count > 0 or missing_sid_count > 0:
        eprint(_lib_alert_scope.next_step_line(
            f"cctally cache-sync --source {resolved_source}",
            provider=resolved_source,
            window_start=since_dt,
            window_end=until_dt,
            tz=getattr(args, "_resolved_tz", None),
        ))

    # --- Attribution math (Task 5) -----------------------------------------
    # Load per-week `weekly_percent` (max within window) for every week that
    # intersects [since_dt, until_dt]. Missing snapshots are tracked so we
    # can surface `weeksMissingSnapshot` in the output — those weeks can't
    # contribute to attributed %.
    # One value per SEGMENT, resolved inside that cycle's own half-open
    # interval (#750 S4 §2.1 / #731). The emitted `SubWeek`s are handed over
    # rather than a date range, because the segment IS the identity
    # `cmd_project` buckets by and a date is what the two cycles of a
    # credited week share.
    week_snapshots: dict[dt.datetime, float] = _load_week_snapshots(
        subweeks, account_key=acct_key,
    )

    # Set of every week the user asked about (from the computed SubWeek
    # bounds), used to report `weeksInRange` and `weeksMissingSnapshot`
    # independent of whether that week had any project activity.
    weeks_in_range: set[dt.datetime] = {ws for ws in week_starts}
    weeks_missing_snapshot: set[dt.datetime] = {
        ws for ws in weeks_in_range if ws not in week_snapshots
    }

    # #661 S2 §5.1: one decision per subscription week, over that week's WHOLE
    # account population. A week with an unsupported or out-of-regime segment
    # falls back for the whole week; partial-week mixtures are not produced.
    # The segment's REAL emitted end, never `ws + 7d` (#750 S4 §2.5). Once
    # `ws` is a credit cut that synthetic end is not the cycle's end, and a
    # bounded regime can then classify a short segment as crossing a boundary
    # it never reaches.
    _end_by_start = {s_dt: e_dt for s_dt, e_dt in parsed_bounds}
    week_attribution: dict = {}
    for ws in sorted(set(week_starts) | set(week_records)):
        week_attribution[ws] = resolve_week_attribution(
            attribution_regime, week_records.get(ws, ()),
            week_start=ws,
            week_end=_end_by_start.get(ws, ws + dt.timedelta(days=7)))
    modelled_weeks = sorted(
        ws for ws, attr in week_attribution.items()
        if attr.basis == "modelled")
    fallback_weeks = sorted(
        ws for ws, attr in week_attribution.items()
        if attr.basis != "modelled")
    # The run's basis is the weakest any contributing week reached, because a
    # payload that claimed `modelled` while some of its rows came from a cost
    # share would be answering the same question two ways.
    if run_basis is not None:
        attribution_basis, attribution_cause = run_basis, run_cause
    elif modelled_weeks and not fallback_weeks:
        attribution_basis, attribution_cause = "modelled", None
    elif modelled_weeks:
        attribution_basis = "cost-share"
        attribution_cause = week_attribution[fallback_weeks[0]].cause
    else:
        attribution_basis = "cost-share"
        attribution_cause = (
            week_attribution[fallback_weeks[0]].cause if fallback_weeks
            else "calibration-absent")

    # Collapse (project_key, week) buckets into one row per project, summing
    # tokens / cost / sessions / first_seen / last_seen / models across the
    # weeks the project appears in.
    #
    # Attribution: for each (project P, week W) bucket,
    #     attributed_pct[P,W] = (cost[P,W] / total_cost[W]) * weekly_percent[W]
    # iff a snapshot exists for W. Weeks without a snapshot contribute None
    # (their weeks are already counted in `weeks_missing_snapshot`).
    project_rows: dict[str, dict] = {}
    for (key, wstart), b in buckets.items():
        row = project_rows.get(key.bucket_path)
        if row is None:
            row = {
                "key": key,
                "sessions": set(),
                "first_seen": b["first_seen"],
                "last_seen": b["last_seen"],
                "input": 0, "output": 0,
                "cache_write": 0, "cache_read": 0,
                "cost_usd": 0.0,
                # `None` until the first week with a snapshot contributes —
                # preserves the distinction between "every contributing week
                # lacked a snapshot" (→ None) and "genuine zero attribution"
                # (→ 0.0 after a real contribution). Spec §3.
                "attributed_pct": None,
                "units": 0.0,
                # `modelled` only when EVERY contributing week was; the first
                # fallback week degrades the row.
                "attribution_basis": (
                    "withheld" if run_basis == "withheld" else "modelled"),
                "models": {},
            }
            project_rows[key.bucket_path] = row
        row["sessions"] |= b["sessions"]
        if b["first_seen"] < row["first_seen"]:
            row["first_seen"] = b["first_seen"]
        if b["last_seen"] > row["last_seen"]:
            row["last_seen"] = b["last_seen"]
        row["input"] += b["input"]
        row["output"] += b["output"]
        row["cache_write"] += b["cache_write"]
        row["cache_read"] += b["cache_read"]
        row["cost_usd"] += b["cost_usd"]
        row["units"] += b["units"]

        # Merge per-model sub-buckets.
        for model, mb in b["models"].items():
            rm = row["models"].get(model)
            if rm is None:
                rm = {
                    "cost_usd": 0.0,
                    "input": 0, "output": 0,
                    "cache_write": 0, "cache_read": 0,
                    "first_seen": mb["first_seen"],
                    "last_seen": mb["last_seen"],
                }
                row["models"][model] = rm
            if mb["first_seen"] < rm["first_seen"]:
                rm["first_seen"] = mb["first_seen"]
            if mb["last_seen"] > rm["last_seen"]:
                rm["last_seen"] = mb["last_seen"]
            rm["cost_usd"] += mb["cost_usd"]
            rm["input"] += mb["input"]
            rm["output"] += mb["output"]
            rm["cache_write"] += mb["cache_write"]
            rm["cache_read"] += mb["cache_read"]

        # Attribution contribution. #661 S2 §5: MODELLED quota where this
        # week's population supports it — the bucket's own weighted units
        # over the regime's units-per-point, which needs no meter reading and
        # no cost denominator at all — and the #86 cost share otherwise.
        #
        # The cost share stays gated on a snapshot and a nonzero week total,
        # because a zero denominator would make the ratio meaningless.
        # `attributed_pct` stays `None` until the first real contribution;
        # subsequent contributions accumulate.
        attr = week_attribution.get(wstart)
        if run_basis == "withheld":
            # §5.3: no modelled quota and no cost-share stand-in either.
            pass
        elif attr is not None and attr.basis == "modelled":
            row["attributed_pct"] = (
                (row["attributed_pct"] or 0.0)
                + b["units"] / attr.units_per_point
            )
        else:
            row["attribution_basis"] = "cost-share"
            week_pct = week_snapshots.get(wstart)
            week_total = total_cost_by_week.get(wstart, 0.0)
            if week_pct is not None and week_total > 0:
                contribution = (b["cost_usd"] / week_total) * week_pct
                row["attributed_pct"] = (
                    (row["attributed_pct"] or 0.0) + contribution
                )

    # Compute $/1% per project: `cost_per_pct = cost_usd / attributed_pct`
    # when attribution is positive; None otherwise (e.g. every contributing
    # week lacked a snapshot — `attributed_pct` still None — or attribution
    # came out to zero).
    for row in project_rows.values():
        ap = row["attributed_pct"]
        if ap is not None and ap > 0:
            row["cost_per_pct"] = row["cost_usd"] / ap
        else:
            row["cost_per_pct"] = None

    # #661 S2 §5.2. Four distinct quantities, named rather than conflated
    # into "the rows do not add up":
    #   1. modelled-week points          — every local entry in the window's
    #                                      MODELLED weeks, weighted and
    #                                      converted
    #   2. visible-row points            — the subset the rendered rows carry
    #   3. filtered or unmodelled points — exactly 1 minus 2
    #   4. observed minus modelled       — the meter's reading minus 1
    #
    # The measured scope of 1 is the window's modelled weeks and NOT every
    # local entry in the window, which is why the key is spelled
    # `modelledWeekPoints`. A fallback week's points appear in NEITHER 1 nor
    # 3 — they are not comparable to a modelled quantity — and a run holding
    # one withholds the residual outright, so the two quantities that are
    # published are the ones this run can actually measure.
    if run_basis == "withheld" or not modelled_weeks:
        modelled_week_points = None
        visible_points = None
        unmodelled_points = None
    else:
        modelled_week_points = stable_sum(
            week_units_total.get(ws, 0.0)
            / week_attribution[ws].units_per_point
            for ws in modelled_weeks)
        visible_points = stable_sum(
            b["units"] / week_attribution[wstart].units_per_point
            for (_key, wstart), b in buckets.items()
            if week_attribution.get(wstart) is not None
            and week_attribution[wstart].basis == "modelled")
        unmodelled_points = modelled_week_points - visible_points
    observed_points = (
        stable_sum(week_snapshots[ws] for ws in modelled_weeks
                   if ws in week_snapshots)
        if modelled_weeks
        and all(ws in week_snapshots for ws in modelled_weeks)
        else None)
    residual_cause = residual_withholding_cause(
        account_resolved=(run_basis is None),
        whole_weeks=range_covers_whole_weeks(
            since_dt, until_dt, parsed_bounds, now=now),
        filtered=bool(project_patterns or model_patterns),
        fallback_weeks=len(fallback_weeks),
    )
    attribution_totals = {
        "modelledWeekPoints": (
            None if modelled_week_points is None
            else round(modelled_week_points, 4)),
        "visibleRowPoints": (
            None if visible_points is None else round(visible_points, 4)),
        "filteredOrUnmodelledPoints": (
            None if unmodelled_points is None
            else round(unmodelled_points, 4)),
        "observedMinusModelledPoints": None,
        "residualCause": residual_cause,
    }
    if residual_cause is None:
        residual = observed_minus_modelled(
            observed=observed_points, modelled=modelled_week_points)
        attribution_totals["observedMinusModelledPoints"] = (
            None if residual is None else round(residual, 4))
        if residual is None:
            # The population aligns, so the absence is one of the two the
            # alignment test cannot see: no modelled week at all, or a
            # modelled week with no meter snapshot. Naming either of them
            # `population-misaligned` states a false reason.
            attribution_totals["residualCause"] = residual_absence_cause(
                observed=observed_points, modelled=modelled_week_points)

    # Collect warnings to surface in the JSON payload (terminal path emits
    # them inline via eprint earlier, so this list stays JSON-specific).
    warnings: list[str] = []
    if unknown_entry_count > 0:
        warnings.append(
            f"{unknown_entry_count} entries lacked project_path — "
            f"run `cache-sync` to backfill."
        )
    if missing_sid_count > 0:
        warnings.append(
            f"{missing_sid_count} entries lacked session_files session_id — "
            f"run `cache-sync` to backfill."
        )

    # Honor --sort / --order. For numeric keys, `_project_sort_key` flips the
    # primary-key sign to match the requested direction so natural `sorted()`
    # ordering already produces the right answer; the dname tie-break stays
    # ascending in both directions (ties never invert alphabetically). For
    # `name`, the key is asc-natural (a-z) and `reverse=` is used for desc.
    if args.sort == "name":
        sorted_rows = sorted(
            project_rows.values(),
            key=lambda r: _project_sort_key(r, args.sort, args.order),
            reverse=(args.order == "desc"),
        )
    else:
        sorted_rows = sorted(
            project_rows.values(),
            key=lambda r: _project_sort_key(r, args.sort, args.order),
        )

    # Shareable-reports gate: --format short-circuits the JSON / table
    # dispatch via `_share_render_and_emit`. The mutex in
    # `_add_share_args` keeps `--format` and `--json` from coexisting.
    # Privacy invariant (Section 8.4 / 5.3): `_lib_share.render()` prepares
    # the RAW snapshot the wrapper hands it, so default output anonymizes
    # project labels to `project-1` / `project-2` / ...; `--reveal-projects`
    # opts back in. The builder populates `ProjectCell.label` /
    # `ChartPoint.project_label` / `ChartPoint.x_label` with REAL names;
    # `render()` is the chokepoint that rewrites them. (It is NOT `_scrub()`:
    # that function is retained for backward compatibility and no production
    # path calls it.)
    if getattr(args, "format", None):
        # Note: --breakdown is a no-op under --format (snapshot focuses on
        # the headline per-project usage table + HBar chart; per-model
        # sub-rows aren't in the share spec scope). Same convention as
        # cmd_daily / cmd_weekly / cmd_report.
        display_tz_str = c._share_display_tz_label(args._resolved_tz)
        snap = c._build_project_snapshot(
            list(sorted_rows),
            period_start=since_dt,
            period_end=until_dt,
            display_tz=display_tz_str,
            version=c._share_resolve_version(),
        )
        c._share_render_and_emit(snap, args)
        return 0

    if args.json:
        payload = _project_json_payload(
            since=since_dt,
            until=until_dt,
            weeks_in_range=len(weeks_in_range),
            group_mode=args.group,
            rows=sorted_rows,
            weeks_missing_snapshot=weeks_missing_snapshot,
            warnings=warnings,
            include_breakdown=args.breakdown,
            week_snapshots=week_snapshots,
            attribution_basis=attribution_basis,
            attribution_cause=attribution_cause,
            attribution_totals=attribution_totals,
        )
        payload.update(c.account_json_fields(acct_key))  # #341 R8 decoration
        sink = getattr(args, "_source_result_sink", None)
        if sink is not None:
            sink(payload)
        else:
            print(json.dumps(payload, indent=2))
        return 0

    # Terminal path
    range_label = f"{since_dt.date().isoformat()} \u2014 {until_dt.date().isoformat()}"
    title = f"Claude Token Usage Report - Projects ({range_label})"

    if not sorted_rows:
        eprint("No project usage found in range.")
        return 0

    # Session A (spec §7.3): the new --color flag overrides NO_COLOR
    # env; --no-color overrides FORCE_COLOR env; deny-wins on the
    # --color + --no-color clash. _resolve_color_enabled returns the
    # effective bool; pass it as ``color=`` so the renderer skips its
    # internal _supports_color_stdout() auto-detect (which would
    # re-consult NO_COLOR and incorrectly disable color when the user
    # passed --color under NO_COLOR=1).
    print(c._render_project_table(
        sorted_rows,
        title=title,
        breakdown=args.breakdown,
        weeks_missing_snapshot=len(weeks_missing_snapshot),
        weeks_in_range=len(weeks_in_range),
        color=c._resolve_color_enabled(args),
        compact=args.compact,
        # #661 S2 §5.2. The four quantities existed only in `project --json`,
        # so a terminal user got no reconciliation information at all.
        attribution_footer=render_attribution_footer(
            attribution_totals, basis=attribution_basis,
            cause=attribution_cause),
    ))
    return 0
