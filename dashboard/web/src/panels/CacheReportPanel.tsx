// CacheReportPanel — anomaly watchdog for the dashboard.
// Spec 2026-05-21 §2.
//
// Visual states (calm-when-healthy / loud-when-anomalous):
//
//   Healthy:               accent-teal border, ✓ glyph, today's % +
//                          14d median compare, sparkline + mini
//                          net-bars rendered, "14d net: +$X.XX" subline.
//   Anomalous:             accent-amber border, ⚠ glyph, worst-trigger
//                          headline, sparkline rendered with amber
//                          today-marker, mini net-bars rendered.
//   Insufficient baseline: accent-teal border, ~ glyph, "Building
//                          baseline N/5 days" — sparkline + bars omitted.
//   Empty (no activity):   accent-teal border, − glyph, "No Claude
//                          activity yet".
//
// Mini net-bars sit under the sparkline to fill the panel body.
// Single-direction version of CacheNetBars (green=positive net day,
// amber=negative), scaled to max |net| so trends are visible at 28 px
// tall. The 14d-net subline below them is the panel's headline
// summary number; the ⚠-days count is surfaced in the modal table
// rather than competing for panel space.
//
// Click anywhere on the panel body dispatches OPEN_MODAL kind:
// 'cache-report'. The PanelGrip touch handle inside the header is
// drag-only (managed by dnd-kit at the panel-host level), so it does
// not steal the click; the panel is wrapped by PanelHost upstream which
// installs the dnd-kit pointer listeners on the surrounding element.
//
// No ShareIcon in v1 — cache-report is not in SHARE_CAPABLE_PANELS
// (spec §2.6).
import { useSnapshot } from '../hooks/useSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { dispatch } from '../store/store';
import { PanelGrip } from '../components/PanelGrip';
import { ExpandButton } from '../components/ExpandButton';
import { CacheSparkline } from '../modals/CacheSparkline';
import { CacheNetBars } from '../modals/CacheNetBars';
import { fmt } from '../lib/fmt';
import { CACHE_REPORT_MIN_BASELINE_DAYS } from '../lib/cache-report-constants';

const TEAL = 'var(--accent-teal)';
const AMBER = 'var(--accent-amber)';
const GREEN = 'var(--accent-green)';

export function CacheReportPanel() {
  const env = useSnapshot();
  const cr = env?.cache_report;
  // Mobile-driven collapse (< 720 px). The `.cache-report-collapsed`
  // modifier class hides the sparkline + secondary subline via the
  // existing @media rule at index.css:4186 so the panel reads as a
  // single-line summary on phones. Mirrors the daily-collapsed /
  // sessions-collapsed convention but is viewport-driven (no user pref).
  const isMobile = useIsMobile();
  const collapseClass = isMobile ? ' cache-report-collapsed' : '';
  const openModal = () => dispatch({ type: 'OPEN_MODAL', kind: 'cache-report' });

  // Shared keyboard handler attached to all three render branches
  // (loading / empty / healthy) so a focused panel opens the modal on
  // Enter / Space in every state — M2 in /check-review round 4. The
  // section-focus-only guard mirrors SessionsPanel / ProjectsPanel so
  // a key press inside a child element doesn't double-fire.
  const handlePanelKeyDown = (e: React.KeyboardEvent) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openModal();
    }
  };

  // No data yet — minimal placeholder (envelope cold-start). The panel
  // still renders so panelOrder / drag-and-drop / keymap routing have
  // a real DOM target; click is wired so the modal can open even before
  // the first sync tick.
  if (!cr) {
    return (
      <section
        className={`panel accent-teal${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        role="region"
        aria-label="Cache Report"
        tabIndex={0}
        onClick={openModal}
        onKeyDown={handlePanelKeyDown}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <h2 style={{ color: TEAL }}>
              Cache Report <span className="sub">(loading)</span>
            </h2>
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
      </section>
    );
  }

  // Empty-state — no Claude activity in the window.
  if (cr.is_empty) {
    return (
      <section
        className={`panel accent-teal${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        role="region"
        aria-label="Cache Report"
        tabIndex={0}
        onClick={openModal}
        onKeyDown={handlePanelKeyDown}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <h2 style={{ color: TEAL }}>Cache Report</h2>
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="cr-status-row">
          <span className="cr-glyph thin">−</span>
          <div>
            <div className="cr-headline">No Claude activity yet</div>
            <div className="cr-subline">Run a session to start tracking</div>
          </div>
        </div>
      </section>
    );
  }

  const anomalous = cr.today.anomaly_triggered;
  const insufficient =
    cr.today.baseline_daily_row_count < CACHE_REPORT_MIN_BASELINE_DAYS;

  // Accent class flip (anomalous => amber). The header color follows the
  // same flip so the title text reads correctly against the bordered
  // panel.
  //
  // Gate the chrome flip on `!insufficient`: during the first 1–4
  // captured days a `net_negative` today already sets
  // `cr.today.anomaly_triggered = true` (the server-side classifier
  // skips only `cache_drop` when samples are thin), but the watchdog is
  // supposed to read as neutral "Building baseline" until the 5-day
  // floor exists. Flipping the border / header / sparkline-marker to
  // amber here would render a false warning before the baseline is
  // established and contradict the headline copy below
  // (CacheReportModal mirrors the same gate on .modal-card).
  const chromeAmber = anomalous && !insufficient;
  const accentClass = chromeAmber ? 'accent-amber' : 'accent-teal';
  const headerColor = chromeAmber ? AMBER : TEAL;
  const todayMarker = chromeAmber ? AMBER : GREEN;

  let glyph: { icon: string; cls: string };
  let headline: React.ReactNode;
  let sublineFirst: React.ReactNode = null;

  if (insufficient) {
    glyph = { icon: '~', cls: 'thin' };
    const n = cr.today.baseline_daily_row_count;
    headline = <>Building baseline · {n}/{CACHE_REPORT_MIN_BASELINE_DAYS} days</>;
    sublineFirst = (
      <>
        Today: cache hit {fmt.pctFloor(cr.today.cache_hit_percent)}% · net{' '}
        {fmt.usdSigned(cr.today.net_usd)}
      </>
    );
  } else if (anomalous) {
    glyph = { icon: '⚠', cls: 'warn' };
    // Worst trigger picks the headline; cache_drop wins when both fire.
    const reasons = cr.today.anomaly_reasons;
    if (reasons.includes('cache_drop') && cr.today.delta_pp !== null) {
      // Snap-up-floor on the absolute value (matches Spotlight at
      // CacheReportSpotlight.tsx:48). Floor-then-abs would round a
      // negative delta away from zero (-16.7 -> floor=-17 -> abs=17),
      // disagreeing with the modal by 1.
      const drop = fmt.pctFloor(Math.abs(cr.today.delta_pp));
      headline = (
        <>
          Today: cache hit <span className="delta-bad">↓ {drop}pp</span>
        </>
      );
    } else {
      headline = (
        <>
          Today: net{' '}
          <span className="delta-bad">{fmt.usdSigned(cr.today.net_usd)}</span>
        </>
      );
    }
    sublineFirst = (
      <>
        vs 14d median{' '}
        {cr.today.baseline_median_percent !== null
          ? fmt.pctFloor(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="warn">{fmt.usdSigned(cr.today.net_usd)}</span>
      </>
    );
  } else {
    glyph = { icon: '✓', cls: 'ok' };
    headline = (
      <>
        Today: cache hit{' '}
        <span className="delta-good">{fmt.pctFloor(cr.today.cache_hit_percent)}%</span>
      </>
    );
    sublineFirst = (
      <>
        vs 14d median{' '}
        {cr.today.baseline_median_percent !== null
          ? fmt.pctFloor(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="ok">{fmt.usdSigned(cr.today.net_usd)}</span>
      </>
    );
  }

  // 14-day net = sum of per-day net (positive = caching paid off net
  // of waste; negative = caching cost more than it saved). Reduce
  // computes from the same array the mini bars render, so the headline
  // number and the bar magnitudes can never disagree.
  const fourteenDayNet = cr.days.reduce((acc, d) => acc + d.net_usd, 0);
  const fourteenDayNetClass = fourteenDayNet >= 0 ? 'ok' : 'warn';

  const sublineSecond = insufficient ? (
    <>Watchdog activates at {CACHE_REPORT_MIN_BASELINE_DAYS} days of history</>
  ) : (
    <>
      14d net:{' '}
      <span className={fourteenDayNetClass}>{fmt.usdSigned(fourteenDayNet)}</span>
    </>
  );

  return (
    <section
      className={`panel ${accentClass}${collapseClass}`}
      id="panel-cache-report"
      data-panel-kind="cache-report"
      role="region"
      aria-label="Cache Report"
      tabIndex={0}
      onClick={openModal}
      onKeyDown={handlePanelKeyDown}
      style={{ cursor: 'pointer' }}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div className="cr-panel-header-inner">
          <h2 style={{ color: headerColor }}>
            Cache Report
            {chromeAmber && <span className="sub">⚠ Today</span>}
          </h2>
        </div>
        <div className="panel-header-actions">
          <ExpandButton label="Cache Report" onOpen={openModal} />
          <PanelGrip />
        </div>
      </div>

      <div className="cr-status-row">
        <span className={`cr-glyph ${glyph.cls}`}>{glyph.icon}</span>
        <div>
          <div className="cr-headline">{headline}</div>
          <div className="cr-subline">{sublineFirst}</div>
        </div>
      </div>

      {!insufficient && cr.days.length > 0 && (
        <>
          <CacheSparkline
            days={cr.days}
            baseline_median_percent={cr.today.baseline_median_percent}
            today_marker_color={todayMarker}
            size="mini"
          />
          {/* flex: 1 wrapper — the bars edge-to-edge fill whatever
              vertical room is left in the panel between the sparkline
              and the 14d-net subline. */}
          <div className="cr-netbars-mini-wrap">
            <CacheNetBars days={cr.days} size="mini" />
          </div>
        </>
      )}

      <div className="cr-subline second">{sublineSecond}</div>
    </section>
  );
}
