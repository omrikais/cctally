// CacheReportModal — anomaly watchdog detail view.
//
// Subscribes to ``state.cache_report`` via ``useSnapshot()`` and
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
import { useState } from 'react';
import { Modal } from './Modal';
import { useSnapshot } from '../hooks/useSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { CacheReportSpotlight } from './CacheReportSpotlight';
import { CacheSparkline } from './CacheSparkline';
import { CacheNetBars } from './CacheNetBars';
import { CacheBreakdownCard } from './CacheBreakdownCard';
import { CacheReportSettings } from './CacheReportSettings';
import { fmt } from '../lib/fmt';
import {
  CACHE_REPORT_BAND_PP,
  CACHE_REPORT_MIN_BASELINE_DAYS,
} from '../lib/cache-report-constants';
import type { CacheReportDailyRow } from '../types/envelope';

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

export function CacheReportModal() {
  const env = useSnapshot();
  const cr = env?.cache_report;
  const [showSettings, setShowSettings] = useState(false);
  // CR-2/CR-3 — the 8-column daily table reflows into an unlabeled run-on on
  // mobile, and the long header subtitle crowds the sticky title into "Cache
  // ⋯". A JS branch (JSDOM-testable, matches the Projects mobile-card
  // precedent) renders labeled cards + a short subtitle at ≤640w.
  const isMobile = useIsMobile();

  if (!cr) {
    return (
      <Modal title="Cache Report" accentClass="accent-teal">
        <div style={{ color: 'var(--text-dim)', padding: '20px 0' }}>
          Loading…
        </div>
      </Modal>
    );
  }

  // Empty state — no Claude activity in the window. The panel renders
  // its own short-circuit too; here we surface the same posture in
  // the modal body so the user understands the modal isn't broken.
  if (cr.is_empty) {
    return (
      <Modal title="Cache Report" accentClass="accent-teal">
        <div style={{ color: 'var(--text-dim)', padding: '20px 0' }}>
          No Claude activity in the last {cr.window_days} days.
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
          ? `${cr.window_days}d · Claude`
          : `Last ${cr.window_days} days · ${cr.anomaly_window_days}d baseline · Claude only`}
      </span>
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

  // Mirror the panel's chrome-amber gate: `anomaly_triggered` alone can
  // be true during the first 1–4 captured days (net_negative fires
  // without a baseline), so the panel deliberately stays teal until the
  // 5-day floor exists. The modal MUST follow suit, otherwise the
  // panel-to-modal handoff would be teal → amber on a baseline-building
  // day and contradict the panel's "Building baseline" copy.
  const insufficient =
    cr.today.baseline_daily_row_count < CACHE_REPORT_MIN_BASELINE_DAYS;
  const chromeAmber = cr.today.anomaly_triggered && !insufficient;

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
      <CacheNetBars days={cr.days} size="large" />

      {/* 4. Counterfactual callout */}
      <div className="crm-counterfactual">
        Without caching, you'd have paid{' '}
        <strong>
          +${cr.fourteen_day_counterfactual_usd.toFixed(2)} more
        </strong>{' '}
        over the last {cr.window_days} days · cache efficiency{' '}
        <span
          title={`saved / (saved + |wasted|) = ${efficiencyPct}%`}
        >
          {efficiencyPct}%
        </span>
      </div>

      {/* 5. Daily rows table */}
      <div className="crm-section">
        <div className="crm-section-head crm-sh-table">
          Daily rows · {cr.window_days} days
          <span className="meta">{cr.days.length} days observed</span>
        </div>
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
                  <span className={hitClass}>{fmt.pctFloor(d.cache_hit_percent)}%</span>,
                ],
                [
                  'Net',
                  <span className={isNetNeg ? 'net-neg' : 'net-pos'}>
                    {fmt.usdSigned(d.net_usd)}
                  </span>,
                ],
                ['Saved', <span>{fmt.usd2(d.saved_usd)}</span>],
                ['Wasted', <span>{fmt.usd2(d.wasted_usd)}</span>],
                ['Tok In', <span>{fmt.compact(d.input_tokens, { upper: true })}</span>],
                ['Tok Out', <span>{fmt.compact(d.output_tokens, { upper: true })}</span>],
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
                    <span
                      className={'cd-flag ' + (d.anomaly_triggered ? 'flag-warn' : 'flag-ok')}
                    >
                      {d.anomaly_triggered ? '⚠' : '✓'}
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
                        baselineKnown ? (isHitBad ? 'hit-bad' : 'hit-good') : ''
                      }`.trim()}
                    >
                      {fmt.pctFloor(d.cache_hit_percent)}%
                    </td>
                    <td className="num">{fmt.compact(d.input_tokens, { upper: true })}</td>
                    <td className="num">{fmt.compact(d.output_tokens, { upper: true })}</td>
                    <td className="num">{fmt.usd2(d.saved_usd)}</td>
                    <td className="num">{fmt.usd2(d.wasted_usd)}</td>
                    <td className={`num ${isNetNeg ? 'net-neg' : 'net-pos'}`}>
                      {fmt.usdSigned(d.net_usd)}
                    </td>
                    <td
                      className={`num ${
                        d.anomaly_triggered ? 'flag-warn' : 'flag-ok'
                      }`}
                    >
                      {d.anomaly_triggered ? '⚠' : '✓'}
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
