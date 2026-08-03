// CacheReportModal — anomaly watchdog detail view.
//
// Subscribes to ``state.cache_report`` via ``useScopedSnapshot()`` and
// renders six sections (spec §3.1):
//
//   1. Today's spotlight (CacheReportSpotlight).
//   2. Cache hit % — 14-day timeline (CacheSparkline, large variant).
//   3. Net $ per day (CacheNetBars).
//   4. Counterfactual savings callout.
//   5. Daily rows · 14 days table (per-column header accents via
//      ``.ch-table`` from C1).
//   6. Breakdowns row — by-project + by-model (CacheBreakdownCard ×2).
//
// Plus an inline settings popover (CacheReportSettings) anchored to
// the gear icon in the modal header.
//
// Live updates: SSE ticks re-render in place; the modal does NOT
// reset to null between ticks. Matches the SessionModal precedent
// (see ``docs/dashboard-gotchas.md`` for the warning).
//
// Spec 2026-05-21 §3.
import { useState, useSyncExternalStore } from 'react';
import { Modal } from './Modal';
import { useAccountScope, useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { CacheReportSpotlight } from './CacheReportSpotlight';
import { CacheSparkline } from './CacheSparkline';
import { CacheNetBars } from './CacheNetBars';
import { CacheBreakdownCard } from './CacheBreakdownCard';
import { CacheReportSettings } from './CacheReportSettings';
import { fmt } from '../lib/fmt';
import {
  presentationCacheReportComposition,
  presentationProviders,
  type ProviderPresentationSection,
} from '../lib/dashboardPresentation';
import {
  cacheReportVerdict,
  cacheRowFlagClass,
  cacheRowFlagGlyph,
  cacheRowFlagLabel,
  cacheRowVerdict,
} from '../lib/cacheReportVerdict';
import { getState, subscribeStore } from '../store/store';
import type { DashboardSelection } from '../types/envelope';
import { CACHE_REPORT_BAND_PP } from '../lib/cache-report-constants';
import {
  cachePercent, cachePercentText, cacheVocabulary, notApplicableReason,
  type CacheVocabulary,
} from '../lib/cacheReportVocabulary';
import { CacheNotApplicable } from './CacheNotApplicable';
import type {
  CacheAnomalyReason, CacheReportDailyRow, CacheReportEnvelope,
} from '../types/envelope';
import { SourceChip } from '../panels/sourcePanel';

// Shared per-row coloring rules for the daily section (desktop table + mobile
// cards render from the same derivation, so the two surfaces never diverge):
//   - hit-bad iff the row's percentage sits more than the displayed ±BAND_PP
//     band below today's baseline median (see the long note at the desktop
//     table for why this is band-bound, not the anomaly classifier);
//   - net-neg iff net_usd < 0;
//   - `comparable` gates the neutral (uncolored) hit cell. It needs BOTH ends
//     of the comparison: a baseline to compare against, and — #443 S2 §4.3 — a
//     percentage to compare. This rule reads the RAW number, not `pctFloor`, so
//     a row publishing neither percent key previously evaluated
//     `undefined < 62`, which is false, and the row silently rendered hit-good:
//     a colour asserting it sat inside the band.
function dailyRowFlags(
  d: CacheReportDailyRow,
  baselineMedian: number | null,
  source: string,
): { comparable: boolean; isHitBad: boolean; isNetNeg: boolean } {
  const pct = cachePercent(d, source);
  const comparable = baselineMedian !== null && pct !== null;
  const isHitBad =
    comparable && pct! < baselineMedian! - CACHE_REPORT_BAND_PP;
  return { comparable, isHitBad, isNetNeg: d.net_usd < 0 };
}

// One derivation for BOTH the desktop table and the mobile cards, so the two
// layouts cannot disagree about what a row's flag means (#443 F2). `partial`
// and `unevaluated` share the neutral glyph and differ only in the accessible
// name, which is what names the predicate that did not run. The state->class
// map lives in the chokepoint beside the state->glyph map, so adding a state
// is one edit rather than two.
//
// #443 S2 — both take the report's applicable predicate set and forward it.
// Deriving a row verdict here against the module default would leave every
// Codex row resolving against the Claude pair, which is the exact silent
// no-op spec §4.6 names.
function dailyFlagClass(
  d: CacheReportDailyRow,
  predicates?: readonly CacheAnomalyReason[],
): string {
  return cacheRowFlagClass(cacheRowVerdict(d, predicates).state);
}

function DailyFlagGlyph(
  { d, predicates, reasonLabel }: {
    d: CacheReportDailyRow;
    predicates?: readonly CacheAnomalyReason[];
    reasonLabel?: (predicate: CacheAnomalyReason) => string;
  },
) {
  const v = cacheRowVerdict(d, predicates);
  const label = cacheRowFlagLabel(v.state, v.unevaluated, v.observed, reasonLabel);
  return (
    // `crm-flag-glyph` is the hook the neutral-marker rule in index.css selects
    // on (`td.flag-none .crm-flag-glyph`). It is a styling contract, not
    // decoration: selecting the glyph by its position in the markup instead
    // would silently lose the styling the moment anything is wrapped around it.
    <span className="crm-flag-glyph" aria-label={label} title={label}>
      {cacheRowFlagGlyph(v.state)}
    </span>
  );
}

// The Cache % colour and the Flag glyph are driven by two DELIBERATELY
// independent signals — the displayed ±BAND_PP band and the configurable
// anomaly classifier. The epic's preserve list requires them explained rather
// than collapsed, which is also what resolves "red percent beside a green ✓".
function DailyLegend({ vocab }: { vocab: CacheVocabulary }) {
  return (
    <div className="crm-daily-legend" data-testid="crm-daily-legend">
      Flag: ✓ evaluated, no anomaly — ⚠ anomaly — · not evaluated
      (each flag's label names what was skipped). {vocab.percentColumnHeader} colour
      tracks the displayed ±{CACHE_REPORT_BAND_PP}pp band around the median,
      which is deliberately separate from the configurable anomaly
      threshold.
    </div>
  );
}

// A synthetic row has no token measurement either, so every measured cell
// renders an em dash rather than a zero.
function measured(d: CacheReportDailyRow, node: JSX.Element): JSX.Element {
  return d.observed === false ? <span className="m-unavailable">—</span> : node;
}

function observedDayCount(days: CacheReportDailyRow[]): number {
  return days.filter((d) => d.observed !== false).length;
}

function AllCacheReportSection({
  section,
}: {
  section: ProviderPresentationSection<CacheReportEnvelope>;
}) {
  const report = section.value;
  // Each section renders in its OWN provider's vocabulary — the merged `all`
  // selection has none of its own to pick (#443 S2 §4.4).
  const vocab = cacheVocabulary(section.source);
  return (
    <section
      className="provider-composition-section cache-provider-detail"
      data-provider-section={section.source}
      aria-label={`${section.label} cache report detail`}
    >
      <div className="source-provider-head provider-composition-head">
        <SourceChip source={section.source} />
        <strong>{section.label} cache report</strong>
        {section.status !== 'available' && (
          <span className="provider-section-status">{section.status}</span>
        )}
      </div>
      {report == null ? (
        <div className="provider-section-reason m-unavailable">{section.reason}</div>
      ) : (
        <>
          <div className="crm-section">
            <CacheReportSpotlight cr={report} source={section.source} />
          </div>
          <div className="crm-section">
            <div className="crm-section-head crm-sh-timeline">
              {vocab.timelineHeading} — {report.window_days}-day timeline
              <span className="meta">provider-local</span>
            </div>
            <div className="crm-chart-frame timeline">
              <CacheSparkline
                days={report.days}
                baseline_median_percent={report.today.baseline_median_percent}
                today_marker_color={report.today.anomaly_triggered ? 'var(--accent-amber)' : 'var(--accent-green)'}
                size="large"
                source={section.source}
              />
            </div>
          </div>
          <CacheNetBars days={report.days} size="large" source={section.source} />
          <div className="crm-counterfactual">
            Provider-local counterfactual:{' '}
            <strong>+${report.fourteen_day_counterfactual_usd.toFixed(2)}</strong>
          </div>
          <div className="crm-section">
            <div className="crm-section-head crm-sh-table">
              Daily rows · {report.window_days} days
              <span className="meta">{observedDayCount(report.days)} observed</span>
            </div>
            <div className="provider-daily-summary">
              {report.days.map((day) => (
                <div className="provider-daily-summary-row" key={day.date}>
                  <span>{fmt.calDate(day.date)}</span>
                  <span>{measured(day, <>{cachePercentText(day, section.source) ?? '—'}</>)}</span>
                  <span className={day.observed === false ? '' : day.net_usd < 0 ? 'net-neg' : 'net-pos'}>
                    {measured(day, <>{fmt.usdSigned(day.net_usd)}</>)}
                  </span>
                </div>
              ))}
            </div>
          </div>
          <div className="crm-section">
            <div className="crm-breakdowns">
              <CacheBreakdownCard kind="projects" rows={report.by_project} source={section.source} />
              <CacheBreakdownCard kind="models" rows={report.by_model} source={section.source} />
            </div>
          </div>
          {section.reason && <div className="provider-section-reason">{section.reason}</div>}
        </>
      )}
    </section>
  );
}

function AllCacheReportModal() {
  const env = useScopedSnapshot('all');
  const composition = presentationCacheReportComposition(env, 'all');
  return (
    <Modal title="Cache Report — by provider" accentClass="accent-teal">
      <div className="provider-composition provider-composition--modal" aria-label="Claude and Codex cache reports">
        {composition.sections.map((section) => (
          <AllCacheReportSection key={section.source} section={section} />
        ))}
      </div>
    </Modal>
  );
}

function CanonicalCacheReportModal({ source }: { source: DashboardSelection }) {
  // #416 — the expansion of a scoped panel stays scoped (see `useScopedSnapshot`).
  const env = useScopedSnapshot(source);
  // #443 S2 (F3/F4/F10) — the `sourceCr` compatibility fallback is DELETED. It
  // hard-coded `is_empty: false` so the empty short-circuit could never fire,
  // forced `baseline_daily_row_count` past the five-sample floor so "Building
  // baseline" was unreachable, took a one-sample median at two rows, and seeded
  // the settings popover with `anomaly_threshold_pp: CACHE_REPORT_BAND_PP` — so
  // Save on that path rewrote the GLOBAL threshold from 15 to 5 and changed
  // Claude's verdicts. With `cr` always the server's own report, none of those
  // values can be invented, which is what closes F10 (the finding dies with the
  // fallback, not with a re-added provider guard).
  //
  // #443 F5 — the modal never computed a composition either, so a stale or
  // degraded source showed a confident verdict here while the panel behind it
  // said "degraded". Same section, same chip, same reason as the panel.
  const section = presentationCacheReportComposition(env, source).sections[0];
  const cr = section?.value ?? null;
  // Pinned to the source this modal BOUND its data to, not the live
  // activeSource — otherwise the two disagree the moment the source
  // changes while the modal is open, and the guard below has to fall
  // back to "could not be built" for what is really just empty.
  const scope = useAccountScope(source);
  const vocab = cacheVocabulary(source);
  // The map is the authoritative user-facing reason for the null Codex values.
  const wastedNotApplicable = cr == null
    ? null : notApplicableReason(cr, 'wasted_usd');
  const efficiencyNotApplicable = cr == null
    ? null : notApplicableReason(cr, 'fourteen_day_efficiency_ratio');
  const statusChip = section && section.status !== 'available' ? (
    <span className="provider-section-status">{section.status}</span>
  ) : null;
  const statusReason = section?.reason ?? null;
  // The panel's three-way split, which the modal did not have: its null branch
  // was only hydrating-versus-failure and its empty branch keyed on
  // `cr.is_empty`, which needs a non-null report. Routing `cr` through the
  // composition without this would send an ordinary empty Codex source — and
  // every focused account with no Codex child — to "the snapshot could not be
  // built", a new false statement shipped by a truthfulness session. See the
  // panel for why the three routes differ.
  const accountEmpty = cr == null
    && scope.source === source  // belt-and-braces: the hook now pins it
    && scope.accountKey !== null
    && scope.isEmpty;
  const isEmpty = section?.status === 'empty' || cr?.is_empty === true || accountEmpty;
  const [showSettings, setShowSettings] = useState(false);
  // CR-2/CR-3 — the 8-column daily table reflows into an unlabeled run-on on
  // mobile, and the long header subtitle crowds the sticky title into "Cache
  // ⋯". A JS branch (JSDOM-testable, matches the Projects mobile-card
  // precedent) renders labeled cards + a short subtitle at ≤640w.
  const isMobile = useIsMobile();

  // Empty state — no activity in the window. The panel renders its own
  // short-circuit too; here we surface the same posture in the modal body so
  // the user understands the modal isn't broken. Checked BEFORE the null
  // branch: after S2 an available-and-empty source arrives with a null value.
  if (isEmpty) {
    const bare = section?.status === 'empty' || accountEmpty;
    return (
      <Modal
        title="Cache Report"
        accentClass="accent-teal"
        headerExtras={(bare ? null : statusChip) ?? undefined}
      >
        <div style={{ color: 'var(--text-dim)', padding: '20px 0' }}>
          {/*
            The day count is claimed only where a report survived to state it.
            `presentationCacheReportComposition` nulls `section.value` for an
            available-and-empty report but deliberately keeps it on `degraded`,
            so one of the two empty routes still knows its window and the other
            does not. Reading a count off the nulled value is impossible, and
            inventing one would be the same class of error this session removes.
          */}
          {cr == null
            ? `No ${section?.label ?? 'Claude'} activity yet.`
            : `No ${section?.label ?? 'Claude'} activity in the last ${cr.window_days} days.`}
        </div>
        {!bare && statusReason && (
          <div className="provider-section-reason">{statusReason}</div>
        )}
      </Modal>
    );
  }

  if (!cr) {
    // #443 F8 — the modal had no failure branch at all, printing an
    // unconditional "Loading…" over a snapshot that would never arrive.
    const hydrating = presentationProviders(env, source).hydrating;
    return (
      <Modal title="Cache Report" accentClass="accent-teal">
        <div style={{ color: 'var(--text-dim)', padding: '20px 0' }}>
          {hydrating
            ? 'Loading…'
            : 'Cache Report unavailable — the snapshot could not be built. Retrying on the next sync.'}
        </div>
      </Modal>
    );
  }

  const headerExtras = (
    <>
      <span
        className="sub crm-subtitle"
        style={{ marginRight: 12, color: 'var(--text-dim)' }}
      >
        {isMobile
          ? `${cr.window_days}d · ${source === 'claude' ? 'Claude' : source === 'codex' ? 'Codex' : 'All'}`
          : `Last ${cr.window_days} days · ${cr.anomaly_window_days}d baseline · ${source === 'claude' ? 'Claude only' : source === 'codex' ? 'Codex native cache' : 'All sources'}`}
      </span>
      {/*
        The chip stays in the header; the reason does NOT. `.modal-header` is
        flex/nowrap with no gap, so an unconstrained reason here shares a row
        with the <h2> and the subtitle and wraps both — at every reason length,
        including the 30-character default. The reason renders full-width at the
        top of the body instead, which is also what the panel does.
      */}
      {statusChip}
      <button
        type="button"
        aria-label="Cache Report settings"
        data-cr-settings-toggle
        onClick={(e) => {
          // stopPropagation so the surrounding modal's chrome (close,
          // backdrop) doesn't also process the click.
          e.stopPropagation();
          setShowSettings((v) => !v);
        }}
        style={{
          background: 'transparent',
          border: 0,
          color: 'var(--text-dim)',
          cursor: 'pointer',
          fontSize: 18,
          padding: '0 8px',
        }}
      >
        {/*
          ``data-cr-settings-toggle`` on the parent button is the carve-
          out the popover's outside-mousedown listener uses to skip
          closing when the user clicks the gear while the popover is
          open. ``closest(...)`` matches whether the user lands on the
          button itself or this inner glyph (H2 in /check-review).
        */}
        ⚙
      </button>
    </>
  );

  const chromeAmber = cacheReportVerdict(cr).chromeAmber;

  // Today's marker color for the timeline circle. Mirrors the panel's
  // todayMarker derivation so the modal and panel agree on the
  // semantic green/amber color.
  const todayMarker = chromeAmber
    ? 'var(--accent-amber)'
    : 'var(--accent-green)';

  // Mirror the panel's severity flip on the modal-card border so the
  // teal -> amber visual handoff between panel and modal stays
  // consistent on an anomalous day.
  const accentClass = chromeAmber ? 'accent-amber' : 'accent-teal';

  // Counterfactual efficiency ratio for the callout (already
  // computed server-side; we just format).
  const efficiencyPct = cr.fourteen_day_efficiency_ratio == null
    ? null : Math.round(cr.fourteen_day_efficiency_ratio * 100);

  return (
    <Modal title="Cache Report" accentClass={accentClass} headerExtras={headerExtras}>
      {showSettings && (
        <CacheReportSettings
          current_threshold_pp={cr.anomaly_threshold_pp}
          onClose={() => setShowSettings(false)}
        />
      )}

      {statusReason && (
        <div className="provider-section-reason crm-status-reason">{statusReason}</div>
      )}

      {/* 1. Spotlight */}
      <CacheReportSpotlight cr={cr} source={source} />

      {/* 2. Provider percent timeline */}
      <div className="crm-section">
        <div className="crm-section-head crm-sh-timeline">
          {vocab.timelineHeading} — {cr.window_days}-day timeline
          <span className="meta">
            band = {cr.anomaly_window_days}d median ±{CACHE_REPORT_BAND_PP}pp
          </span>
        </div>
        <div className="crm-chart-frame timeline">
          <CacheSparkline
            days={cr.days}
            baseline_median_percent={cr.today.baseline_median_percent}
            today_marker_color={todayMarker}
            size="large"
            source={source}
          />
        </div>
      </div>

      {/* 3. Net $ per day */}
      <CacheNetBars
        days={cr.days}
        size="large"
        source={source}
      />

      {/* 4. Counterfactual callout */}
      <div className="crm-counterfactual">
          {vocab.counterfactualLead}{' '}
          <strong>+${cr.fourteen_day_counterfactual_usd.toFixed(2)} more</strong>{' '}
          {/*
            The figure is a sum over `days`, and the synthetic today row
            contributes zero — so "over the last {window_days} days" claimed a
            day it does not cover. It names what it actually summed instead.
          */}
          across {observedDayCount(cr.days)} observed days · {vocab.efficiencyLabel}{' '}
          {efficiencyNotApplicable == null
            ? efficiencyPct == null
              ? <span className="m-unavailable">Unavailable</span>
              : <span title={`saved / (saved + |wasted|) = ${efficiencyPct}%`}>{efficiencyPct}%</span>
            : <CacheNotApplicable reason={efficiencyNotApplicable} />}
      </div>

      {/* 5. Daily rows table */}
      <div className="crm-section">
        <div className="crm-section-head crm-sh-table">
          Daily rows · {cr.window_days} days
          <span className="meta">{observedDayCount(cr.days)} days observed</span>
        </div>
        <DailyLegend vocab={vocab} />
        {/* hit-bad rule: a row is bad iff its percentage sits more than
            CACHE_REPORT_BAND_PP below today's baseline median — i.e. it falls
            below the SAME tinted ±BAND_PP band the sparkline draws around the
            median. Earlier rounds tied hit-bad to `d.anomaly_reasons`
            (cache_drop), but that uses the per-row anomaly classifier with
            `anomaly_threshold_pp` (default 15) instead of the modal's displayed
            ±5pp band; days 6-14pp below baseline then rendered green even
            though they visibly sat outside the highlighted band. Re-binding to
            BAND_PP (via `dailyRowFlags`) keeps the cell color and the sparkline
            band in lock-step. The Flag column (`flag-warn`/`flag-ok`) stays tied
            to each row's own `anomaly_triggered`, so display-band coloring and
            the per-row anomaly classifier remain independent. `comparable`
            gates the neutral cell class ('' rather than 'hit-good') when either
            end of the comparison is missing. CR-2: the desktop table reflows
            into an unlabeled run-on on mobile, so at ≤640w we render labeled
            cards from the same `dailyRowFlags` derivation instead. */}
        {isMobile ? (
          <div className="crm-daily-cards">
            {cr.days.map((d) => {
              const isToday = d.date === cr.today.date;
              const { comparable, isHitBad, isNetNeg } = dailyRowFlags(
                d,
                cr.today.baseline_median_percent,
                source,
              );
              const hitClass = comparable
                ? isHitBad
                  ? 'hit-bad'
                  : 'hit-good'
                : '';
              const cells: Array<[string, JSX.Element]> = [
                [
                  vocab.percentColumnHeader,
                  measured(d, <span className={hitClass}>{cachePercentText(d, source) ?? '—'}</span>),
                ],
                [
                  'Net',
                  measured(d, (
                    <span className={isNetNeg ? 'net-neg' : 'net-pos'}>
                      {fmt.usdSigned(d.net_usd)}
                    </span>
                  )),
                ],
                ['Saved', measured(d, <span>{fmt.usd2(d.saved_usd)}</span>)],
                [
                  'Wasted',
                  // Not applicable outranks unmeasured: an em dash here would
                  // read as "not captured today" for a figure that does not
                  // exist on this provider at all, and would carry none of the
                  // reason the wire supplies.
                  wastedNotApplicable == null
                    ? measured(d, <span>{d.wasted_usd == null ? '—' : fmt.usd2(d.wasted_usd)}</span>)
                    : <CacheNotApplicable reason={wastedNotApplicable} />,
                ],
                ['Tok In', measured(d, <span>{fmt.compact(d.input_tokens, { upper: true })}</span>)],
                ['Tok Out', measured(d, <span>{fmt.compact(d.output_tokens, { upper: true })}</span>)],
              ];
              return (
                <div
                  key={d.date}
                  className={'crm-daily-card' + (isToday ? ' cur' : '')}
                  data-testid="crm-daily-card"
                  data-date={d.date}
                >
                  <div className="crm-daily-card-head">
                    <span className="cd-date">{fmt.calDate(d.date)}</span>
                    <span className={`cd-flag ${dailyFlagClass(d, cr.anomaly_predicates)}`}>
                      <DailyFlagGlyph
                        d={d}
                        predicates={cr.anomaly_predicates}
                        reasonLabel={vocab.reasonLabel}
                      />
                    </span>
                  </div>
                  <div className="crm-daily-card-grid">
                    {cells.map(([label, value]) => (
                      <div key={label} className="cd-cell">
                        <span className="lbl">{label}</span>
                        <span className="val num">{value}</span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <table className="ch-table">
            <thead>
              <tr>
                <th className="c-date">Date</th>
                <th className="c-hit num">{vocab.percentColumnHeader}</th>
                <th className="c-tokens num">Tok In</th>
                <th className="c-tokens num">Tok Out</th>
                <th className="c-saved num">Saved</th>
                <th className="c-wasted num">Wasted</th>
                <th className="c-net num">Net</th>
                <th className="c-flag num">Flag</th>
              </tr>
            </thead>
            <tbody>
              {cr.days.map((d) => {
                const isToday = d.date === cr.today.date;
                const { comparable, isHitBad, isNetNeg } = dailyRowFlags(
                  d,
                  cr.today.baseline_median_percent,
                  source,
                );
                return (
                  <tr
                    key={d.date}
                    className={isToday ? 'cur' : ''}
                    data-testid="crm-daily-row"
                    data-date={d.date}
                  >
                    <td>{fmt.calDate(d.date)}</td>
                    <td
                      className={`num ${
                        d.observed === false
                          ? ''
                          : comparable ? (isHitBad ? 'hit-bad' : 'hit-good') : ''
                      }`.trim()}
                    >
                      {measured(d, <>{cachePercentText(d, source) ?? '—'}</>)}
                    </td>
                    <td className="num">
                      {measured(d, <>{fmt.compact(d.input_tokens, { upper: true })}</>)}
                    </td>
                    <td className="num">
                      {measured(d, <>{fmt.compact(d.output_tokens, { upper: true })}</>)}
                    </td>
                    <td className="num">{measured(d, <>{fmt.usd2(d.saved_usd)}</>)}</td>
                    <td className="num">
                      {wastedNotApplicable == null
                        ? measured(d, <>{d.wasted_usd == null ? '—' : fmt.usd2(d.wasted_usd)}</>)
                        : <CacheNotApplicable reason={wastedNotApplicable} />}
                    </td>
                    <td className={`num ${d.observed === false ? '' : isNetNeg ? 'net-neg' : 'net-pos'}`.trim()}>
                      {measured(d, <>{fmt.usdSigned(d.net_usd)}</>)}
                    </td>
                    <td className={`num ${dailyFlagClass(d, cr.anomaly_predicates)}`}>
                      <DailyFlagGlyph
                        d={d}
                        predicates={cr.anomaly_predicates}
                        reasonLabel={vocab.reasonLabel}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* 6. Breakdowns row */}
      <div className="crm-section">
        <div className="crm-breakdowns">
          <CacheBreakdownCard kind="projects" rows={cr.by_project} source={source} />
          <CacheBreakdownCard kind="models" rows={cr.by_model} source={source} />
        </div>
      </div>
    </Modal>
  );
}

export function CacheReportModal() {
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  return source === 'all'
    ? <AllCacheReportModal />
    : <CanonicalCacheReportModal source={source} />;
}
