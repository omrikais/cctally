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

export function CacheReportModal() {
  const env = useSnapshot();
  const cr = env?.cache_report;
  const [showSettings, setShowSettings] = useState(false);

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
        className="sub"
        style={{ marginRight: 12, color: 'var(--text-dim)' }}
      >
        Last {cr.window_days} days · {cr.anomaly_window_days}d baseline · Claude only
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
              // hit-bad rule: a row is bad iff its cache_hit_percent
              // sits more than CACHE_REPORT_BAND_PP below today's
              // baseline median — i.e. it falls below the SAME tinted
              // ±BAND_PP band the sparkline draws around the median.
              //
              // Earlier rounds tied hit-bad to `d.anomaly_reasons`
              // (cache_drop), but that uses the per-row anomaly
              // classifier with `anomaly_threshold_pp` (default 15)
              // instead of the modal's displayed ±5pp band. With the
              // defaults, days 6-14pp below baseline rendered green
              // even though they visibly sat outside the highlighted
              // band; raising the threshold widened the gap. Re-binding
              // to BAND_PP keeps the table cell color and the
              // sparkline band always in lock-step, regardless of how
              // the user configures `anomaly_threshold_pp`. The Flag
              // column (`flag-warn` / `flag-ok` below) remains tied to
              // each row's own `anomaly_triggered`, so the two signals
              // — display-band coloring vs. per-row anomaly classifier
              // — stay independent and each carries its own meaning.
              //
              // `baselineKnown` is still gated on today's median: it's
              // the window-wide "do we have any baseline at all" signal
              // (the rolling window is shared across rows), and the
              // neutral cell class — '' rather than 'hit-good' — only
              // makes sense when there's nothing to compare against.
              const baselineKnown = cr.today.baseline_median_percent !== null;
              const isHitBad =
                baselineKnown &&
                cr.today.baseline_median_percent !== null &&
                d.cache_hit_percent <
                  cr.today.baseline_median_percent - CACHE_REPORT_BAND_PP;
              const isNetNeg = d.net_usd < 0;
              return (
                <tr
                  key={d.date}
                  className={isToday ? 'cur' : ''}
                  data-testid="crm-daily-row"
                  data-date={d.date}
                >
                  <td>{d.date}</td>
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
