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
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useIsMobile } from '../hooks/useIsMobile';
import { CacheReportSpotlight } from './CacheReportSpotlight';
import { CacheSparkline } from './CacheSparkline';
import { CacheNetBars } from './CacheNetBars';
import { CacheBreakdownCard } from './CacheBreakdownCard';
import { CacheReportSettings } from './CacheReportSettings';
import { fmt } from '../lib/fmt';
import {
  presentationCacheDays,
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
import {
  CACHE_REPORT_BAND_PP,
  CACHE_REPORT_MIN_BASELINE_DAYS,
} from '../lib/cache-report-constants';
import type { CacheReportDailyRow, CacheReportEnvelope } from '../types/envelope';
import { SourceChip } from '../panels/sourcePanel';

// Shared per-row coloring rules for the daily section (desktop table + mobile
// cards render from the same derivation, so the two surfaces never diverge):
//   - hit-bad iff the row's cache_hit_percent sits more than the displayed
//     ±BAND_PP band below today's baseline median (see the long note at the
//     desktop table for why this is band-bound, not the anomaly classifier);
//   - net-neg iff net_usd < 0;
//   - baselineKnown gates the neutral (uncolored) hit cell when no baseline
//     exists yet.
function dailyRowFlags(
  d: CacheReportDailyRow,
  baselineMedian: number | null,
): { baselineKnown: boolean; isHitBad: boolean; isNetNeg: boolean } {
  const baselineKnown = baselineMedian !== null;
  const isHitBad =
    baselineKnown &&
    baselineMedian !== null &&
    d.cache_hit_percent < baselineMedian - CACHE_REPORT_BAND_PP;
  return { baselineKnown, isHitBad, isNetNeg: d.net_usd < 0 };
}

// One derivation for BOTH the desktop table and the mobile cards, so the two
// layouts cannot disagree about what a row's flag means (#443 F2). `partial`
// and `unevaluated` share the neutral glyph and differ only in the accessible
// name, which is what names the predicate that did not run. The state->class
// map lives in the chokepoint beside the state->glyph map, so adding a state
// is one edit rather than two.
function dailyFlagClass(d: CacheReportDailyRow): string {
  return cacheRowFlagClass(cacheRowVerdict(d).state);
}

function DailyFlagGlyph({ d }: { d: CacheReportDailyRow }) {
  const v = cacheRowVerdict(d);
  const label = cacheRowFlagLabel(v.state, v.unevaluated, v.observed);
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
function DailyLegend() {
  return (
    <div className="crm-daily-legend" data-testid="crm-daily-legend">
      Flag: ✓ evaluated, no anomaly — ⚠ anomaly — · not evaluated
      (each flag's label names what was skipped). Cache % colour tracks
      the displayed ±{CACHE_REPORT_BAND_PP}pp band around the median,
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

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function AllCacheReportSection({
  section,
}: {
  section: ProviderPresentationSection<CacheReportEnvelope>;
}) {
  const report = section.value;
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
            <CacheReportSpotlight cr={report} />
          </div>
          <div className="crm-section">
            <div className="crm-section-head crm-sh-timeline">
              Cache hit % — {report.window_days}-day timeline
              <span className="meta">provider-local</span>
            </div>
            <div className="crm-chart-frame timeline">
              <CacheSparkline
                days={report.days}
                baseline_median_percent={report.today.baseline_median_percent}
                today_marker_color={report.today.anomaly_triggered ? 'var(--accent-amber)' : 'var(--accent-green)'}
                size="large"
              />
            </div>
          </div>
          <CacheNetBars days={report.days} size="large" />
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
                  <span>{measured(day, <>{fmt.pctFloor(day.cache_hit_percent)}%</>)}</span>
                  <span className={day.observed === false ? '' : day.net_usd < 0 ? 'net-neg' : 'net-pos'}>
                    {measured(day, <>{fmt.usdSigned(day.net_usd)}</>)}
                  </span>
                </div>
              ))}
            </div>
          </div>
          <div className="crm-section">
            <div className="crm-breakdowns">
              <CacheBreakdownCard kind="projects" rows={report.by_project} />
              <CacheBreakdownCard kind="models" rows={report.by_model} />
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
  const display = useDisplayTz();
  const isClaude = source === 'claude';
  const nativeReport = env?.sources?.codex?.data?.cache_report ?? null;
  const sourceRows = isClaude
    ? null
    : (presentationCacheDays(env, source) ?? [])
        .filter((row) => row.input_tokens + row.cache_read_tokens + row.output_tokens > 0)
        .slice(0, 14);
  const baseline = sourceRows == null ? null : median(sourceRows.slice(1).map((row) => row.cache_hit_percent));
  const first = sourceRows?.[0];
  const sourceCr: CacheReportEnvelope | null = nativeReport ?? (sourceRows == null ? null : {
    window_days: 14,
    anomaly_threshold_pp: CACHE_REPORT_BAND_PP,
    anomaly_window_days: 14,
    today: {
      date: first?.date ?? fmt.calendarDateKey(env?.generated_at, {
        tz: display.resolvedTz,
        offsetLabel: display.offsetLabel,
      }) ?? '1970-01-01',
      cache_hit_percent: first?.cache_hit_percent ?? 0,
      baseline_median_percent: baseline,
      delta_pp: first == null || baseline == null ? null : first.cache_hit_percent - baseline,
      net_usd: 0,
      saved_usd: 0,
      wasted_usd: 0,
      anomaly_triggered: false,
      anomaly_reasons: [],
      baseline_daily_row_count: Math.max(CACHE_REPORT_MIN_BASELINE_DAYS, sourceRows.length),
    },
    days: sourceRows,
    by_project: [],
    by_model: [],
    seven_day_net_usd: 0,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 0,
    fourteen_day_efficiency_ratio: 0,
    is_empty: false,
  });
  const cr = isClaude ? env?.cache_report : sourceCr;
  // #443 F5 — the modal never computed a composition, so a stale or degraded
  // source showed a confident verdict here while the panel behind it said
  // "degraded". Same chip and reason as the panel, from the same helper.
  const section = presentationCacheReportComposition(env, source).sections[0];
  // As in the panel: the chip describes the SECTION's report. On the Codex
  // compatibility-fallback path `sourceCr` is not `section.value`, and pairing
  // a full verdict with a chip denying the report exists would contradict the
  // screen. S2 (F3/F4) owns that fallback's own presentation.
  const rendersSectionValue = (cr ?? null) === (section?.value ?? null);
  const statusChip = section && rendersSectionValue && section.status !== 'available' ? (
    <span className="provider-section-status">{section.status}</span>
  ) : null;
  const statusReason = section && rendersSectionValue ? section.reason : null;
  const [showSettings, setShowSettings] = useState(false);
  // CR-2/CR-3 — the 8-column daily table reflows into an unlabeled run-on on
  // mobile, and the long header subtitle crowds the sticky title into "Cache
  // ⋯". A JS branch (JSDOM-testable, matches the Projects mobile-card
  // precedent) renders labeled cards + a short subtitle at ≤640w.
  const isMobile = useIsMobile();

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

  // Empty state — no Claude activity in the window. The panel renders
  // its own short-circuit too; here we surface the same posture in
  // the modal body so the user understands the modal isn't broken.
  if (cr.is_empty) {
    return (
      <Modal
        title="Cache Report"
        accentClass="accent-teal"
        headerExtras={statusChip ?? undefined}
      >
        <div style={{ color: 'var(--text-dim)', padding: '20px 0' }}>
          No {source === 'codex' ? 'Codex' : source === 'all' ? 'provider' : 'Claude'} activity in the last {cr.window_days} days.
        </div>
        {statusReason && (
          <div className="provider-section-reason">{statusReason}</div>
        )}
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
  const efficiencyPct = Math.round(cr.fourteen_day_efficiency_ratio * 100);

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
      <CacheReportSpotlight cr={cr} />

      {/* 2. Cache hit % timeline */}
      <div className="crm-section">
        <div className="crm-section-head crm-sh-timeline">
          Cache hit % — {cr.window_days}-day timeline
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
          />
        </div>
      </div>

      {/* 3. Net $ per day */}
      <CacheNetBars
        days={cr.days}
        size="large"
      />

      {/* 4. Counterfactual callout */}
      <div className="crm-counterfactual">
          Without caching, you'd have paid{' '}
          <strong>+${cr.fourteen_day_counterfactual_usd.toFixed(2)} more</strong>{' '}
          over the last {cr.window_days} days · cache efficiency{' '}
          <span title={`saved / (saved + |wasted|) = ${efficiencyPct}%`}>{efficiencyPct}%</span>
      </div>

      {/* 5. Daily rows table */}
      <div className="crm-section">
        <div className="crm-section-head crm-sh-table">
          Daily rows · {cr.window_days} days
          <span className="meta">{observedDayCount(cr.days)} days observed</span>
        </div>
        <DailyLegend />
        {/* hit-bad rule: a row is bad iff its cache_hit_percent sits more than
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
            the per-row anomaly classifier remain independent. `baselineKnown`
            gates the neutral cell class ('' rather than 'hit-good') when there
            is nothing to compare against yet. CR-2: the desktop table reflows
            into an unlabeled run-on on mobile, so at ≤640w we render labeled
            cards from the same `dailyRowFlags` derivation instead. */}
        {isMobile ? (
          <div className="crm-daily-cards">
            {cr.days.map((d) => {
              const isToday = d.date === cr.today.date;
              const { baselineKnown, isHitBad, isNetNeg } = dailyRowFlags(
                d,
                cr.today.baseline_median_percent,
              );
              const hitClass = baselineKnown
                ? isHitBad
                  ? 'hit-bad'
                  : 'hit-good'
                : '';
              const cells: Array<[string, JSX.Element]> = [
                [
                  'Cache %',
                  measured(d, <span className={hitClass}>{fmt.pctFloor(d.cache_hit_percent)}%</span>),
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
                ['Wasted', measured(d, <span>{fmt.usd2(d.wasted_usd)}</span>)],
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
                    <span className={`cd-flag ${dailyFlagClass(d)}`}>
                      <DailyFlagGlyph d={d} />
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
                <th className="c-hit num">Cache %</th>
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
                const { baselineKnown, isHitBad, isNetNeg } = dailyRowFlags(
                  d,
                  cr.today.baseline_median_percent,
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
                          : baselineKnown ? (isHitBad ? 'hit-bad' : 'hit-good') : ''
                      }`.trim()}
                    >
                      {measured(d, <>{fmt.pctFloor(d.cache_hit_percent)}%</>)}
                    </td>
                    <td className="num">
                      {measured(d, <>{fmt.compact(d.input_tokens, { upper: true })}</>)}
                    </td>
                    <td className="num">
                      {measured(d, <>{fmt.compact(d.output_tokens, { upper: true })}</>)}
                    </td>
                    <td className="num">{measured(d, <>{fmt.usd2(d.saved_usd)}</>)}</td>
                    <td className="num">{measured(d, <>{fmt.usd2(d.wasted_usd)}</>)}</td>
                    <td className={`num ${d.observed === false ? '' : isNetNeg ? 'net-neg' : 'net-pos'}`.trim()}>
                      {measured(d, <>{fmt.usdSigned(d.net_usd)}</>)}
                    </td>
                    <td className={`num ${dailyFlagClass(d)}`}>
                      <DailyFlagGlyph d={d} />
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
          <CacheBreakdownCard kind="projects" rows={cr.by_project} />
          <CacheBreakdownCard kind="models" rows={cr.by_model} />
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
