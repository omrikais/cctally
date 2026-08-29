"""Forecast decision kernel for cctally.

Pure-fn layer (no I/O at import time): the forecast inputs/output/budget
dataclasses plus `_compute_forecast` — the projection + budget-headroom
math. Values in (`ForecastInputs`), a `ForecastOutput` out. No DB reads,
no config loads, no `_cctally()` accessor: the projection routes through
`project_linear` (the shared, pure primitive whose real home is
`_lib_budget`).

Imported by `_cctally_forecast.py` (and `_cctally_tui.py`) and re-exported
on the `cctally` namespace via `bin/cctally`, so the existing
`cctally.ForecastInputs` / `mod.ForecastOutput` / `inspect.getsource(
cctally._compute_forecast)` read paths resolve unchanged (re-export
continuity, spec §2). Single definition of each dataclass lives here;
everything else imports it, so class identity stays unique.

Spec: docs/superpowers/specs/2026-07-09-279-s4-record-kernelization-design.md
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from enum import Enum

import _lib_quota_model as _qm
from _lib_budget import project_linear


# --- The ceiling correction (#661 S2, spec section 3.1) --------------------
#
# Both meters DISPLAY A CEILING, so a shown reading of `k` means true
# consumption fell in `[k-1, k)`. The rule itself has one home — the quota
# kernel's `true_percent_point` / `true_percent_interval` — and these two
# helpers only adapt a possibly-fractional stored reading to it through the
# canonical integer-percent floor. A second spelling of the rule here would
# be a second place to get `[k-1, k)` wrong.
def corrected_percent_point(shown) -> "float | None":
    """Unbiased point estimate of the consumption behind a shown reading.

    `None` when the reading is right-censored — a displayed 100 denotes
    `[99, +inf)` and has no finite point estimate, so returning 99.5 would
    invent a value the observation does not supply.
    """
    if shown is None:
        return None
    return _qm.true_percent_point(_qm.integer_percent(float(shown)))


def corrected_percent_interval(shown) -> "tuple | None":
    """The half-open interval a shown reading denotes; `hi` is None at 100."""
    if shown is None:
        return None
    return _qm.true_percent_interval(_qm.integer_percent(float(shown)))


def projection_base(inputs) -> "float | None":
    """The reading every projection is measured FROM.

    The corrected point where one exists; the raw displayed reading
    otherwise. `otherwise` covers two cases: a right-censored reading, which
    has no corrected point at all, and a duck-typed inputs object from an
    older fixture that carries no corrected field. Several renderers
    re-derive `p_now + rate * remaining` for their own labels rather than
    reading `final_percent_*` (which are min/max aggregates and swap labels),
    and each of those must project from the SAME operand `_compute_forecast`
    used, or the rendered projection mixes a corrected rate with a raw base.
    """
    corrected = getattr(inputs, "p_now_corrected", None)
    if corrected is not None:
        return corrected
    return getattr(inputs, "p_now", None)


class ProjectionBasis(str, Enum):
    """Which measurement a published end-of-week projection came from.

    The three values are a wire contract every S2 surface renders, so they
    are the exact strings the selector emits.
    """

    CALIBRATED = "calibrated"
    CORRECTED_METER = "corrected-meter"
    WITHHELD = "withheld"


@dataclass(frozen=True)
class SelectedProjection:
    basis: ProjectionBasis
    value: "float | None"
    #: A member of the quota kernel's closed `EVIDENCE_CODES` union when the
    #: basis is WITHHELD; None otherwise.
    code: "str | None" = None


#: The cause a withheld projection states when the meter is right-censored.
#: A member of `_lib_quota_model.EVIDENCE_CODES`, pinned by a test rather
#: than by this comment.
_RIGHT_CENSORED_CODE = "right-censored"
_UNAVAILABLE_CODE = "unavailable"


def select_projection_basis(inputs) -> SelectedProjection:
    """The one projection every S2 consumer publishes, and where it came from.

    The calibrated model when the section 1.1 adapters said it was
    trustworthy for THIS week's population, the corrected meter otherwise,
    and a typed withholding when neither is available. Pure: the caller
    supplies the calibrated value on the inputs, because reaching it needs a
    store read and this function must stay callable from the kernel.

    Selection lives here and in `_compute_forecast` rather than in
    `build_forecast_view`, because the CLI calls `_compute_forecast` directly
    and the view is only one of its consumers.
    """
    calibrated = getattr(inputs, "calibrated_projection_pct", None)
    if calibrated is not None:
        try:
            usable = math.isfinite(calibrated)
        except (TypeError, OverflowError):
            usable = False
        if usable:
            return SelectedProjection(ProjectionBasis.CALIBRATED,
                                      float(calibrated), None)
        # A non-finite calibrated value is not a projection, so the meter is
        # selected below. This is a BACKSTOP, not the reporting site: the
        # producer `_calibrated_projection` refuses a non-finite result and
        # states `unavailable` on the wire's `calibration_code`, so a value
        # reaching here unusable means a caller built the inputs by hand.

    # The meter's own projection. A right-censored reading has no point
    # estimate, so there is nothing to project from and none is invented.
    # Right-censoring applies at 100 and ONLY at 100, because that reading
    # alone denotes an interval unbounded above (spec §3.6). Every other
    # reading, zero included, denotes a bounded interval whose midpoint is
    # the corrected point, so a displayed 0 projects from 0.25 exactly as a
    # displayed 40 projects from 39.5. An earlier revision withheld the
    # zero-reading week and gave it the cause `no-local-history`; both halves
    # were wrong. It singled out the one reading where the correction is most
    # visible and refused to apply it, and the cause was false independently,
    # because S1 defines `no-local-history` as the ingest tail failing to
    # cover the interval or the interval holding no entries, while the
    # fixture that produced it carries $9.12 of spend across seven snapshots.
    if getattr(inputs, "right_censored", False):
        return SelectedProjection(ProjectionBasis.WITHHELD, None,
                                  _RIGHT_CENSORED_CODE)
    base = projection_base(inputs)
    remaining = getattr(inputs, "remaining_hours", None)
    elapsed = getattr(inputs, "elapsed_hours", None)
    if base is None or remaining is None or elapsed is None:
        return SelectedProjection(ProjectionBasis.WITHHELD, None,
                                  _UNAVAILABLE_CODE)
    r_avg = base / elapsed if elapsed > 0 else 0.0
    return SelectedProjection(ProjectionBasis.CORRECTED_METER,
                              base + r_avg * remaining, None)


class ForecastConfidenceCause(str, Enum):
    """The closed set of reasons a forecast is low-confidence.

    The four values are a wire contract `--json` consumers read, so they are
    the exact strings `_assess_forecast_confidence` has always emitted.
    """

    ELAPSED_HOURS = "elapsed_hours<24"
    PERCENT = "percent<2"
    SNAPSHOTS = "snapshots<3"
    NO_SAMPLE_GE_24H = "no_sample_ge_24h"


@dataclass(frozen=True)
class ForecastConfidenceAssessment:
    confidence: str                 # "high" | "low"
    reasons: tuple[str, ...]


def assess_forecast_confidence(
    elapsed_hours: float,
    percent: float,
    snapshot_count: int,
    *,
    has_sample_ge_24h: bool,
) -> ForecastConfidenceAssessment:
    """The complete confidence predicate, including the fourth trigger.

    `no_sample_ge_24h` used to be appended by `_load_forecast_inputs` after
    it called the three-trigger predicate, which meant glue could add reasons
    the predicate did not know about (#620 S2 E3, closes F19). Reason order
    is `elapsed_hours<24`, `percent<2`, `snapshots<3`, `no_sample_ge_24h` —
    the order the old call-plus-append produced.
    """
    reasons: list[str] = []
    if elapsed_hours < 24:
        reasons.append(ForecastConfidenceCause.ELAPSED_HOURS.value)
    if percent < 2:
        reasons.append(ForecastConfidenceCause.PERCENT.value)
    if snapshot_count < 3:
        reasons.append(ForecastConfidenceCause.SNAPSHOTS.value)
    if not has_sample_ge_24h:
        reasons.append(ForecastConfidenceCause.NO_SAMPLE_GE_24H.value)
    return ForecastConfidenceAssessment(
        confidence="low" if reasons else "high",
        reasons=tuple(reasons),
    )


#: Every value `_select_dollars_per_percent` can return, with its human
#: copy. The union used to live in a trailing comment on the field and had
#: already fallen behind the code by one member.
#:
#: The three trailing-median members share a denominator and differ only in
#: what the §4.2 comparability test was able to say about the candidate
#: population: it passed, it rejected at least one week for model-mix drift,
#: or it could not run at all because the local store was unreadable. A bare
#: `replace("_", " ")` rendered the qualification as raw text with no
#: explanation, which is what this table replaces.
DOLLARS_PER_PERCENT_SOURCES: dict = {
    "this_week": "this week",
    "this_week_sparse": "this week sparse",
    "no_usage_observed": "no usage observed",
    "trailing_4wk_median": "trailing 4wk median",
    "trailing_4wk_median_drifted":
        "trailing 4wk median (drift-reduced confidence)",
    "trailing_4wk_median_unverified":
        "trailing 4wk median (comparability unverified)",
}


def dollars_per_percent_source_label(source: str) -> str:
    """Human copy for a `dollars_per_percent_source` code.

    An unknown code degrades to the old `replace("_", " ")` rendering rather
    than raising or printing an empty cell, so a code added by a later
    session is readable before its copy lands.
    """
    return DOLLARS_PER_PERCENT_SOURCES.get(source, str(source).replace("_", " "))


@dataclass
class ForecastInputs:
    now_utc: dt.datetime
    week_start_at: dt.datetime
    week_end_at: dt.datetime
    elapsed_hours: float
    elapsed_fraction: float
    remaining_hours: float
    remaining_days: float
    # Current state
    p_now: float
    five_hour_percent: float | None
    spent_usd: float
    snapshot_count: int
    latest_snapshot_at: dt.datetime
    # Rate inputs
    p_24h_ago: float | None
    t_24h_actual_hours: float | None
    # $/1% selection. `None` when no usage has been observed (#620 S1 D5) —
    # every dollar-derived output is then unavailable rather than zero, and
    # `dollars_per_percent_source` carries the cause.
    dollars_per_percent: "float | None"
    #: One of `DOLLARS_PER_PERCENT_SOURCES`. It is a MACHINE code — every
    #: `--json` surface emits it verbatim — and human copy comes from
    #: `dollars_per_percent_source_label`, never from a `replace("_", " ")`
    #: at a render site.
    dollars_per_percent_source: str
    # Confidence
    confidence: str  # "high" | "low"
    low_confidence_reasons: list[str]
    # --- The ceiling-corrected pair (#661 S2, spec section 3.1) ------------
    #
    # `p_now` and `p_24h_ago` above stay the RAW displayed readings, which is
    # what every surface prints. These four carry the corrected reading the
    # arithmetic runs on. They are derived in `__post_init__` rather than
    # required of the caller, so every construction site — the loader, the
    # TUI's demo builder, a test — gets the correction; a field the loader
    # alone filled in would leave every other site silently uncorrected.
    p_now_corrected: "float | None" = None
    p_now_interval: "tuple | None" = None
    p_24h_ago_corrected: "float | None" = None
    right_censored: "bool | None" = None
    # --- The calibrated basis (#661 S2, spec section 3.3) -----------------
    #
    # The model-backed projected end-of-week percent, present only when the
    # section 1.1 adapters said the persisted regime was trustworthy for THIS
    # week's population. `calibrated_withheld_code` states why it is absent,
    # as a member of the quota kernel's closed `EVIDENCE_CODES` union, so a
    # renderer states a cause rather than a blank.
    calibrated_projection_pct: "float | None" = None
    calibrated_withheld_code: "str | None" = None
    # The rest of what the same week-scan produced (#661 S2 spec §10). The
    # dashboard publishes consumption and headroom beside the projection,
    # and reading them from a second call would run a second unbounded week
    # scan per refresh for numbers the first scan already computed. All
    # three are None whenever `calibrated_projection_pct` is.
    calibrated_consumption_pct: "float | None" = None
    calibrated_consumption_interval: "tuple | None" = None
    calibrated_headroom_pct: "float | None" = None

    def __post_init__(self) -> None:
        if self.p_now_corrected is None:
            self.p_now_corrected = corrected_percent_point(self.p_now)
        if self.p_now_interval is None:
            self.p_now_interval = corrected_percent_interval(self.p_now)
        if self.p_24h_ago_corrected is None:
            self.p_24h_ago_corrected = corrected_percent_point(self.p_24h_ago)
        if self.right_censored is None:
            self.right_censored = self.p_now_corrected is None


@dataclass
class BudgetRow:
    target_percent: int
    pct_headroom: float | None     # None when already past target
    dollars_per_day: float | None
    percent_per_day: float | None


@dataclass
class ForecastOutput:
    inputs: ForecastInputs
    # pct per hour, week-avg. `None` at a right-censored reading: a displayed
    # 100 denotes `[99, +inf)`, so the observation supplies no rate either.
    r_avg: float | None
    r_recent: float | None         # pct per hour, 24h recent; None if no prior sample
    # The three projections below are `None` in the right-censored state
    # (#661 S2, spec section 3.2): a displayed 100 denotes `[99, +inf)`, so
    # there is no point estimate to project from and none is fabricated.
    final_percent_low: float | None
    final_percent_high: float | None
    week_avg_projection_pct: float | None
    projected_cap: bool
    already_capped: bool
    cap_at: dt.datetime | None
    budgets: list[BudgetRow]
    #: True when the current reading is right-censored. `already_capped`
    #: says the meter SHOWS its cap; this says the observation carries no
    #: upper bound, which is why the projections above are withheld.
    right_censored: bool = False
    #: `ProjectionBasis` value naming where `week_avg_projection_pct` came
    #: from, and the typed cause when it is withheld.
    projection_basis: str = ProjectionBasis.CORRECTED_METER.value
    projection_code: "str | None" = None


def _compute_forecast(inputs: ForecastInputs, targets: list[int]) -> ForecastOutput:
    """Implements spec §2. targets are sorted desc for stable output (100, 90, …).

    Every quantity below is computed from the CEILING-CORRECTED reading
    (#661 S2, spec section 3.1). `inputs.p_now` remains the raw displayed
    value and is what the renderers print; `inputs.p_now_corrected` is the
    unbiased estimate of what was actually consumed, and the two differ by
    half a point at every reading above 1.
    """
    censored = bool(inputs.right_censored)
    # In the censored state there is no corrected point, so there is no
    # week-average rate either and none is invented. A displayed 103 denotes
    # `[99, +inf)`; 103/120 is arithmetic over a reading the model declares
    # censored, not a lower bound on the rate and not an estimate of it. An
    # earlier revision fell back to the displayed value here and claimed
    # nothing derived from it was published, which was false — it reached the
    # wire as `rates.week_average_pct_per_hour`.
    r_avg: float | None = None
    if not censored:
        r_avg = (inputs.p_now_corrected / inputs.elapsed_hours
                 if inputs.elapsed_hours > 0 else 0.0)
    if (not censored and inputs.p_24h_ago_corrected is not None
            and inputs.t_24h_actual_hours):
        r_recent: float | None = max(
            0.0,
            (inputs.p_now_corrected - inputs.p_24h_ago_corrected)
            / inputs.t_24h_actual_hours
        )
    else:
        r_recent = None

    # The ONE selector both this kernel and the projected-alert twin call
    # (#661 S2 spec section 3.3), so the two cannot drift apart.
    selection = select_projection_basis(inputs)

    if censored:
        # No point estimate, no ETA, no headroom, no budget amount is
        # fabricated from a censored METER. The cap itself is a FACT here,
        # not a projection, so both cap flags stay true. A trustworthy
        # calibration still supplies `week_avg_projection_pct`, because it
        # reads tokens rather than the censored meter; the band, the ETA and
        # the budgets stay withheld either way, because those are derived
        # from the meter and this session does not model them.
        return ForecastOutput(
            inputs=inputs, r_avg=r_avg, r_recent=None,
            final_percent_low=None, final_percent_high=None,
            week_avg_projection_pct=selection.value, projected_cap=True,
            already_capped=True, cap_at=None, budgets=[],
            right_censored=True,
            projection_basis=selection.basis.value,
            projection_code=selection.code,
        )
    p_now = inputs.p_now_corrected

    if selection.basis is ProjectionBasis.WITHHELD:
        # An uncensored week reaches this branch on exactly one cause,
        # `unavailable`: the corrected point, the elapsed span or the
        # remaining span is missing, so there is no pace to project along and
        # no ceiling distance to price. The percent budget below does not
        # depend on a rate and is still published, which is the split
        # #620 S1 D5 already decided for the dollar budget.
        return ForecastOutput(
            inputs=inputs, r_avg=r_avg, r_recent=r_recent,
            final_percent_low=None, final_percent_high=None,
            week_avg_projection_pct=None, projected_cap=False,
            already_capped=False, cap_at=None,
            budgets=_budget_rows(inputs, targets, p_now),
            right_censored=False,
            projection_basis=selection.basis.value,
            projection_code=selection.code,
        )

    # Projected final % — routed through the shared project_linear primitive
    # (spec F1). r_recent is None ⇒ collapse to the average projection.
    if r_recent is None:
        final_low, final_high = project_linear(
            p_now, inputs.remaining_hours, r_avg, r_avg
        )
    else:
        a, b = project_linear(
            p_now, inputs.remaining_hours, r_avg, r_recent
        )
        final_low, final_high = min(a, b), max(a, b)

    # Smooth week-average projection (additive surface field). Distinct from
    # the displayed band (which keys off final_high): this is the conservative
    # week-average value the projected-pace alert axis fires on.
    # p_now + r_avg*remaining (== project_linear collapsed to the single rate).
    # The selector's value, not a second derivation of it. When no
    # calibration is available this is exactly `p_now + r_avg * remaining`,
    # which is what keeps the meter-basis behaviour unchanged.
    week_avg_projection_pct = selection.value

    # `already_capped` is UNREACHABLE here: `corrected_percent_point` returns
    # None at every displayed reading of 100 or more, which is the censored
    # branch above, so the corrected point tops out at 98.5. The flag is
    # therefore False on this path by construction and is not re-tested.
    projected_cap = final_high >= 100.0

    cap_at: dt.datetime | None = None
    if projected_cap:
        r_pessimistic = max(r_avg, r_recent or 0.0)
        if r_pessimistic > 0:
            hours_to_cap = (100.0 - p_now) / r_pessimistic
            if hours_to_cap < inputs.remaining_hours:
                cap_at = inputs.now_utc + dt.timedelta(hours=hours_to_cap)

    return ForecastOutput(
        inputs=inputs,
        r_avg=r_avg,
        r_recent=r_recent,
        final_percent_low=final_low,
        final_percent_high=final_high,
        week_avg_projection_pct=week_avg_projection_pct,
        projected_cap=projected_cap,
        already_capped=False,
        cap_at=cap_at,
        budgets=_budget_rows(inputs, targets, p_now),
        right_censored=False,
        projection_basis=selection.basis.value,
        projection_code=selection.code,
    )


def _budget_rows(inputs, targets, p_now) -> "list[BudgetRow]":
    """One `BudgetRow` per target, measured from the corrected reading.

    Shared by the ordinary path and the no-observed-usage path, so the two
    cannot drift: the percent budget never depended on a projection, and #620
    S1 D5 already decided that it is published where the dollar budget is not.
    """
    rows: list[BudgetRow] = []
    for t in sorted(targets, reverse=True):
        headroom = t - p_now
        if headroom <= 0 or inputs.remaining_days <= 0:
            rows.append(BudgetRow(target_percent=t, pct_headroom=None,
                                  dollars_per_day=None, percent_per_day=None))
            continue
        # #620 S1 D5: with no observed usage there is no rate, so the dollar
        # budget is unavailable. The percent budget does not depend on the
        # rate and is still published.
        dollars_day = (
            None if inputs.dollars_per_percent is None
            else (headroom * inputs.dollars_per_percent) / inputs.remaining_days
        )
        rows.append(BudgetRow(
            target_percent=t,
            pct_headroom=headroom,
            dollars_per_day=dollars_day,
            percent_per_day=headroom / inputs.remaining_days,
        ))
    return rows
