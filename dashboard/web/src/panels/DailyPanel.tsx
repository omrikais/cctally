import { Fragment, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { fmt } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { DailyPanelRow } from '../types/envelope';

const MONTH_ABBR = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

function chunkRows<T>(rows: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < rows.length; i += size) out.push(rows.slice(i, i + size));
  return out;
}

// Date strings are local-tz-applied already on the Python side, so splitting
// avoids the JS Date-string-tz pitfall.
function fmtPeakDate(iso: string): string {
  const parts = iso.split('-').map(Number);
  return `${MONTH_ABBR[parts[1] - 1]} ${parts[2]}`;
}

function fmtChunkRange(firstIso: string, lastIso: string): string {
  const a = firstIso.split('-').map(Number);
  const b = lastIso.split('-').map(Number);
  const m1 = a[1], d1 = a[2], m2 = b[1], d2 = b[2];
  return m1 === m2
    ? `${MONTH_ABBR[m1 - 1]} ${d1} - ${d2}`
    : `${MONTH_ABBR[m1 - 1]} ${d1} - ${MONTH_ABBR[m2 - 1]} ${d2}`;
}

function Cell({
  r,
  staggerIndex,
  isMobile,
}: {
  r: DailyPanelRow;
  staggerIndex: number;
  isMobile: boolean;
}) {
  const dd = r.date.slice(8, 10);  // "YYYY-MM-DD" → "DD"
  // Mobile rounds to the ceiling integer so 6-char "$50.27" collapses
  // to 2-3 chars and fits the narrower 7-col grid (W-col hidden via CSS).
  // Desktop keeps the full "$NN.NN" precision. Tooltip stays usd2 — title
  // attributes don't fire on touch anyway, so desktop precision is the
  // only consumer.
  const cellCost = r.cost_usd > 0
    ? (isMobile ? `${Math.ceil(r.cost_usd)}` : fmt.usd2(r.cost_usd))
    : '—';
  const tooltip = [
    `${r.label} · ${r.cost_usd > 0 ? fmt.usd2(r.cost_usd) : '—'}`,
    ...r.models.map((m) => `${m.display} ${m.cost_pct.toFixed(0)}%`),
  ].join(' · ');
  return (
    <button
      type="button"
      data-cell-date={r.date}
      className={`daily-cell h${r.intensity_bucket}${r.is_today ? ' is-today' : ''} first-mount`}
      title={tooltip}
      style={{ ['--daily-stagger' as string]: `${staggerIndex * 30}ms` }}
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: r.date })}
    >
      <span className="d">{dd}</span>
      <span className="c">{cellCost}</span>
    </button>
  );
}

export function DailyPanel() {
  const env = useSnapshot();
  const isMobile = useIsMobile();
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.dailyCollapsed,
  );
  const rows = env?.daily?.rows ?? [];
  // Envelope rows are newest-first; the calendar grid renders oldest-first
  // (top-left → bottom-right).
  const orderedRows = [...rows].reverse();
  const chunks = chunkRows(orderedRows, 7);
  const total = rows.reduce((acc, r) => acc + r.cost_usd, 0);
  const peak = env?.daily?.peak ?? null;

  return (
    <section
      className={'panel accent-indigo' + (collapsed ? ' daily-collapsed' : '')}
      id="panel-daily"
      tabIndex={0}
      role="region"
      aria-label="Daily heatmap panel"
      data-panel-kind="daily"
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" style={{ color: 'var(--accent-indigo)' }}>
            <use href="/static/icons.svg#grid" />
          </svg>
          <h3 style={{ color: 'var(--accent-indigo)' }}>
            Daily <span className="sub">heatmap · 30 days</span>
          </h3>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <ShareIcon
            panel="daily"
            panelLabel="Daily"
            triggerId="daily-panel"
            onClick={() => dispatch(openShareModal('daily', 'daily-panel'))}
          />
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-daily-body"
            aria-label={collapsed ? 'Expand Daily' : 'Collapse Daily'}
            title={collapsed ? 'Expand' : 'Collapse'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({
                type: 'SAVE_PREFS',
                patch: { dailyCollapsed: !collapsed },
              });
            }}
          >
            <svg className="icon">
              <use href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`} />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      <div className="panel-body" id="panel-daily-body">
        {rows.length === 0 ? (
          <div className="panel-empty">No usage history yet.</div>
        ) : (
          <>
            <div className="daily-legend" aria-label="Color intensity legend">
              <span>Less</span>
              <span className="scale">
                <span className="h0" />
                <span className="h1" />
                <span className="h2" />
                <span className="h3" />
                <span className="h4" />
                <span className="h5" />
              </span>
              <span>More</span>
            </div>
            <div className="daily-cal-grid">
              {chunks.map((chunk, ci) => (
                <Fragment key={ci}>
                  <div className="daily-week-label">
                    <span className="wk">W{ci + 1}</span>
                    <span className="rng">
                      {fmtChunkRange(chunk[0].date, chunk[chunk.length - 1].date)}
                    </span>
                  </div>
                  {chunk.map((r, ri) => (
                    <Cell
                      key={r.date}
                      r={r}
                      staggerIndex={ci * 7 + ri}
                      isMobile={isMobile}
                    />
                  ))}
                  {chunk.length < 7 && (
                    <div
                      className="daily-month-continues"
                      style={{ gridColumn: `span ${7 - chunk.length}` }}
                      aria-hidden="true"
                    >
                      Month continues
                    </div>
                  )}
                </Fragment>
              ))}
            </div>
            <div className="daily-foot">
              <div className="daily-foot-col" data-total-cell>
                <svg className="daily-foot-icon icon-total" aria-hidden="true">
                  <use href="/static/icons.svg#pie-chart" />
                </svg>
                <div className="daily-foot-text">
                  <span className="lbl">Total ({rows.length} days)</span>
                  <span className="val">{fmt.usd2(total)}</span>
                </div>
              </div>
              {peak && (
                <button
                  type="button"
                  className="daily-foot-col daily-foot-peak"
                  data-peak-trigger
                  title="Click to open Daily modal at peak day"
                  onClick={() => dispatch({
                    type: 'OPEN_MODAL', kind: 'daily', dailyDate: peak.date,
                  })}
                >
                  <svg className="daily-foot-icon icon-peak" aria-hidden="true">
                    <use href="/static/icons.svg#trending-up" />
                  </svg>
                  <div className="daily-foot-text">
                    <span className="lbl">Peak day</span>
                    <span className="val">
                      <span className="d">{fmtPeakDate(peak.date)}</span>
                      <span className="a">{fmt.usd2(peak.cost_usd)}</span>
                    </span>
                  </div>
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
