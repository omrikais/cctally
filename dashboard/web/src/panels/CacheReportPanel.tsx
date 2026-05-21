// CacheReportPanel — anomaly watchdog for the dashboard.
// Spec 2026-05-21 §2.
//
// Visual states (calm-when-healthy / loud-when-anomalous):
//
//   Healthy:               accent-teal border, ✓ glyph, today's % +
//                          14d median compare, sparkline rendered.
//   Anomalous:             accent-amber border, ⚠ glyph, worst-trigger
//                          headline, sparkline rendered with amber
//                          today-marker.
//   Insufficient baseline: accent-teal border, ~ glyph, "Building
//                          baseline N/5 days" — sparkline omitted.
//   Empty (no activity):   accent-teal border, − glyph, "No Claude
//                          activity yet".
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
import { dispatch } from '../store/store';
import { PanelGrip } from '../components/PanelGrip';
import { CacheSparkline } from '../modals/CacheSparkline';

const TEAL = 'var(--accent-teal)';
const AMBER = 'var(--accent-amber)';
const GREEN = 'var(--accent-green)';

function fmtSignedUsd(n: number): string {
  // Match the spec's "+$1.20" / "−$0.42" rendering (Unicode minus sign).
  const sign = n >= 0 ? '+' : '−';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

// Always snap up by 1e-9 before Math.floor on a percent-like float —
// the same idiom enforced in CLAUDE.md to defend against ULP drift on
// `fraction * 100` boundaries.
function floorPct(p: number): number {
  return Math.floor(p + 1e-9);
}

export function CacheReportPanel() {
  const env = useSnapshot();
  const cr = env?.cache_report;
  const openModal = () => dispatch({ type: 'OPEN_MODAL', kind: 'cache-report' });

  // No data yet — minimal placeholder (envelope cold-start). The panel
  // still renders so panelOrder / drag-and-drop / keymap routing have
  // a real DOM target; click is wired so the modal can open even before
  // the first sync tick.
  if (!cr) {
    return (
      <section
        className="panel accent-teal"
        id="panel-cache-report"
        data-panel-kind="cache-report"
        role="region"
        aria-label="Cache Report"
        tabIndex={0}
        onClick={openModal}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h3 style={{ color: TEAL }}>
              Cache Report <span className="sub">(loading)</span>
            </h3>
          </div>
          <PanelGrip />
        </div>
      </section>
    );
  }

  // Empty-state — no Claude activity in the window.
  if (cr.is_empty) {
    return (
      <section
        className="panel accent-teal"
        id="panel-cache-report"
        data-panel-kind="cache-report"
        role="region"
        aria-label="Cache Report"
        tabIndex={0}
        onClick={openModal}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h3 style={{ color: TEAL }}>Cache Report</h3>
          </div>
          <PanelGrip />
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

  // Decide state.
  const anomalous = cr.today.anomaly_triggered;
  const insufficient = cr.today.baseline_daily_row_count < 5;

  // Accent class flip (anomalous => amber). The header color follows the
  // same flip so the title text reads correctly against the bordered
  // panel.
  const accentClass = anomalous ? 'accent-amber' : 'accent-teal';
  const headerColor = anomalous ? AMBER : TEAL;
  const todayMarker = anomalous ? AMBER : GREEN;

  // Glyph + headline + subline content varies by state.
  let glyph: { icon: string; cls: string };
  let headline: React.ReactNode;
  let sublineFirst: React.ReactNode = null;

  if (insufficient) {
    glyph = { icon: '~', cls: 'thin' };
    const n = cr.today.baseline_daily_row_count;
    headline = <>Building baseline · {n}/5 days</>;
    sublineFirst = (
      <>
        Today: cache hit {floorPct(cr.today.cache_hit_percent)}% · net{' '}
        {fmtSignedUsd(cr.today.net_usd)}
      </>
    );
  } else if (anomalous) {
    glyph = { icon: '⚠', cls: 'warn' };
    // Worst trigger picks the headline; cache_drop wins when both fire.
    const reasons = cr.today.anomaly_reasons;
    if (reasons.includes('cache_drop') && cr.today.delta_pp !== null) {
      const drop = Math.abs(floorPct(cr.today.delta_pp));
      headline = (
        <>
          Today: cache hit <span className="delta-bad">↓ {drop}pp</span>
        </>
      );
    } else {
      headline = (
        <>
          Today: net{' '}
          <span className="delta-bad">{fmtSignedUsd(cr.today.net_usd)}</span>
        </>
      );
    }
    sublineFirst = (
      <>
        vs 14d median{' '}
        {cr.today.baseline_median_percent !== null
          ? floorPct(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="warn">{fmtSignedUsd(cr.today.net_usd)}</span>
      </>
    );
  } else {
    // Healthy.
    glyph = { icon: '✓', cls: 'ok' };
    headline = (
      <>
        Today: cache hit{' '}
        <span className="delta-good">{floorPct(cr.today.cache_hit_percent)}%</span>
      </>
    );
    sublineFirst = (
      <>
        vs 14d median{' '}
        {cr.today.baseline_median_percent !== null
          ? floorPct(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="ok">{fmtSignedUsd(cr.today.net_usd)}</span>
      </>
    );
  }

  const sublineSecond = insufficient ? (
    <>Watchdog activates at 5 days of history</>
  ) : (
    <>
      7d:{' '}
      <span className="ok">{fmtSignedUsd(cr.seven_day_net_usd)} saved</span>
      {' · '}
      {cr.seven_day_anomaly_count > 0 ? (
        <span className="warn">{cr.seven_day_anomaly_count} ⚠ days</span>
      ) : (
        <>0 ⚠ days</>
      )}
    </>
  );

  return (
    <section
      className={`panel ${accentClass}`}
      id="panel-cache-report"
      data-panel-kind="cache-report"
      role="region"
      aria-label="Cache Report"
      tabIndex={0}
      onClick={openModal}
      onKeyDown={(e) => {
        // Mirror SessionsPanel / ProjectsPanel "section-focus-only" guard
        // so a key press inside a child element doesn't double-fire.
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openModal();
        }
      }}
      style={{ cursor: 'pointer' }}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h3 style={{ color: headerColor }}>
            Cache Report
            {anomalous && <span className="sub">⚠ Today</span>}
          </h3>
        </div>
        <PanelGrip />
      </div>

      <div className="cr-status-row">
        <span className={`cr-glyph ${glyph.cls}`}>{glyph.icon}</span>
        <div>
          <div className="cr-headline">{headline}</div>
          <div className="cr-subline">{sublineFirst}</div>
        </div>
      </div>

      {!insufficient && cr.days.length > 0 && (
        <CacheSparkline
          days={cr.days}
          baseline_median_percent={cr.today.baseline_median_percent}
          today_marker_color={todayMarker}
          size="mini"
        />
      )}

      <div className="cr-subline second">{sublineSecond}</div>
    </section>
  );
}
