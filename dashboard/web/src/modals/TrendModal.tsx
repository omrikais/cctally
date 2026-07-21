import { Fragment, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Modal } from './Modal';
import { ShareIcon } from '../components/ShareIcon';
import { SortableHeader } from '../components/SortableHeader';
import { fmt } from '../lib/fmt';
import { applyTableSort, type SortOverride } from '../lib/tableSort';
import { TREND_COLUMNS, type TrendTableRow } from '../lib/trendColumns';
import { buildTrendHistoryData, type TrendChartDatum } from '../store/selectors';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { presentationTrend } from '../lib/dashboardPresentation';
import type { DashboardSelection } from '../types/envelope';

function formatWeeksPill(n: number): string {
  const months = Math.max(1, Math.round(n / 4));
  return `${n} week${n === 1 ? '' : 's'} · ${months} month${months === 1 ? '' : 's'}`;
}

function findCurrentIndex(rows: TrendChartDatum[]): number {
  const i = rows.findIndex((r) => r && r.is_current);
  return i >= 0 ? i : rows.length - 1;
}

// Issue #59 client-side fallback for envelopes that omit
// `trend.history_median_dpp` (pre-v1.9 envelopes / fixture snapshots).
// Rule mirrors `build_trend_view`'s pre-computed
// `median_dpp_non_current_4w`: drop the current row, keep
// non-null/finite dpp values, sort the last 4 ascending, take the
// midpoint `(s[1]+s[2])/2`. Returns null when fewer than 4 valid
// samples remain.
function median4NonCurrentFallback(
  rows: TrendChartDatum[],
  curIdx: number,
): number | null {
  const nonCur = rows
    .filter((_, i) => i !== curIdx)
    .map((r) => r.dollar_per_pct)
    .filter((v): v is number => v != null && isFinite(v));
  if (nonCur.length < 4) return null;
  const last4 = nonCur.slice(-4).sort((a, b) => a - b);
  return (last4[1] + last4[2]) / 2;
}

function medianAll(vals: number[]): number | null {
  if (!vals.length) return null;
  const s = [...vals].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

interface SvgPrimitive {
  el: 'line' | 'polyline' | 'circle' | 'text';
  attrs: Record<string, string | number>;
  text?: string;
  key: string;
}

// Sparkline geometry — hoisted so the interactive hit-targets + tooltip
// anchors (TR-4) share the exact scale the primitives are drawn on.
// `PAD.right` reserves room for the right-hand used% second axis (TR-3).
const SPARK_W = 600;
const SPARK_H = 140;
const SPARK_PAD = { left: 44, right: 30, top: 12, bottom: 22 };
const SPARK_INNER_W = SPARK_W - SPARK_PAD.left - SPARK_PAD.right;
const SPARK_INNER_H = SPARK_H - SPARK_PAD.top - SPARK_PAD.bottom;
function sparkXi(i: number, n: number): number {
  return SPARK_PAD.left + (n === 1 ? SPARK_INNER_W : (i / (n - 1)) * SPARK_INNER_W);
}

function buildSparklinePrimitives(rows: TrendChartDatum[], curIdx: number): SvgPrimitive[] {
  const out: SvgPrimitive[] = [];
  if (!rows.length) return out;
  const W = SPARK_W;
  const H = SPARK_H;
  const PAD = SPARK_PAD;
  const INNER_H = SPARK_INNER_H;
  const N = rows.length;
  const xi = (i: number) => sparkXi(i, N);

  // Primary scale — $/1%
  const yvalsDpp = rows.map((r) => r.dollar_per_pct).filter((v): v is number => v != null && isFinite(v));
  let ymin = yvalsDpp.length ? Math.min(...yvalsDpp) * 0.96 : 0;
  let ymax = yvalsDpp.length ? Math.max(...yvalsDpp) * 1.04 : 1;
  if (ymax - ymin < 1e-6) {
    ymin -= 0.01;
    ymax += 0.01;
  }
  const yDpp = (v: number) => PAD.top + INNER_H * (1 - (v - ymin) / (ymax - ymin));

  const yvalsUsed = rows.map((r) => r.used_pct).filter((v): v is number => v != null && isFinite(v));
  let umin = yvalsUsed.length ? Math.min(...yvalsUsed) * 0.96 : 0;
  let umax = yvalsUsed.length ? Math.max(...yvalsUsed) * 1.04 : 100;
  if (umax - umin < 1e-6) {
    umin -= 1;
    umax += 1;
  }
  const yUsed = (v: number) => PAD.top + INNER_H * (1 - (v - umin) / (umax - umin));

  // 1) Baseline axis
  out.push({
    el: 'line',
    attrs: {
      class: 'mtr-axis',
      x1: PAD.left,
      y1: PAD.top + INNER_H,
      x2: W - PAD.right,
      y2: PAD.top + INNER_H,
    },
    key: 'axis',
  });

  // 2) Median reference
  if (yvalsDpp.length) {
    const med = medianAll(yvalsDpp);
    if (med != null) {
      out.push({
        el: 'line',
        attrs: {
          class: 'mtr-hl',
          x1: PAD.left,
          y1: yDpp(med),
          x2: W - PAD.right,
          y2: yDpp(med),
        },
        key: 'med-line',
      });
      out.push({
        el: 'text',
        attrs: { class: 'mtr-medlabel', x: PAD.left + 2, y: yDpp(med) - 3 },
        text: `${N}-wk median $` + med.toFixed(2),
        key: 'med-label',
      });
    }
  }

  // 3) Secondary polyline (used_pct)
  {
    const pts: string[] = [];
    rows.forEach((r, i) => {
      if (r.used_pct != null && isFinite(r.used_pct)) pts.push(`${xi(i)},${yUsed(r.used_pct)}`);
    });
    if (pts.length >= 2) {
      out.push({
        el: 'polyline',
        attrs: { class: 'mtr-trendline-dim', points: pts.join(' ') },
        key: 'poly-used',
      });
    }
  }

  // 4) Primary polyline ($/1%)
  {
    const pts: string[] = [];
    rows.forEach((r, i) => {
      if (r.dollar_per_pct != null && isFinite(r.dollar_per_pct))
        pts.push(`${xi(i)},${yDpp(r.dollar_per_pct)}`);
    });
    if (pts.length >= 2) {
      out.push({
        el: 'polyline',
        attrs: { class: 'mtr-trendline', points: pts.join(' ') },
        key: 'poly-dpp',
      });
    }
  }

  // 5) Primary dots
  rows.forEach((r, i) => {
    if (r.dollar_per_pct == null || !isFinite(r.dollar_per_pct)) return;
    const isCur = i === curIdx;
    out.push({
      el: 'circle',
      attrs: {
        class: 'mtr-trenddot' + (isCur ? ' cur' : ''),
        cx: xi(i),
        cy: yDpp(r.dollar_per_pct),
        r: isCur ? 5 : 3,
      },
      key: `dot-${i}`,
    });
  });

  // 6) Y labels (primary $/1%)
  if (yvalsDpp.length) {
    out.push({
      el: 'text',
      attrs: { class: 'mtr-ylabel', x: 3, y: PAD.top + 8 },
      text: '$' + (ymax / 1.04).toFixed(2),
      key: 'ymax',
    });
    out.push({
      el: 'text',
      attrs: { class: 'mtr-ylabel', x: 3, y: PAD.top + INNER_H },
      text: '$' + (ymin / 0.96).toFixed(2),
      key: 'ymin',
    });
  }

  // 6b) Right second axis (used %). TR-3: label the ACTUAL extremes by
  // undoing the 0.96/1.04 domain padding — the same way the primary axis
  // above reports `ymax/1.04` / `ymin/0.96` — NOT the padded domain bounds.
  if (yvalsUsed.length) {
    out.push({
      el: 'text',
      attrs: { class: 'mtr-ylabel-right', x: W - PAD.right + 2, y: PAD.top + 8 },
      text: Math.round(umax / 1.04) + '%',
      key: 'umax',
    });
    out.push({
      el: 'text',
      attrs: { class: 'mtr-ylabel-right', x: W - PAD.right + 2, y: PAD.top + INNER_H },
      text: Math.round(umin / 0.96) + '%',
      key: 'umin',
    });
  }

  // 7) Current-value label next to the cyan dot
  const cur = rows[curIdx];
  if (cur && cur.dollar_per_pct != null && isFinite(cur.dollar_per_pct)) {
    out.push({
      el: 'text',
      attrs: {
        class: 'mtr-curlabel',
        x: Math.max(PAD.left + 4, xi(curIdx) - 22),
        y: Math.min(H - 2, yDpp(cur.dollar_per_pct) + 16),
      },
      text: '$' + cur.dollar_per_pct.toFixed(2),
      key: 'curlabel',
    });
  }

  return out;
}

function historyWlab(r: TrendChartDatum, k: number): string {
  return k === 0
    ? 'Now' + (r.label ? ' · ' + r.label : '')
    : 'W−' + k + (r.label ? ' · ' + r.label : '');
}

function renderDelta(delta: number | null): { cls: string; text: string } {
  if (delta == null || !isFinite(delta)) return { cls: 'flat', text: '—' };
  if (delta > 0.0005) return { cls: 'up', text: '+' + delta.toFixed(2) };
  if (delta < -0.0005) return { cls: 'down', text: delta.toFixed(2) };
  return { cls: 'flat', text: '0.00' };
}

interface DeltaKv {
  cls: 'delta-up' | 'delta-down' | 'delta-flat';
  iconHref: string;
  text: string;
}

function deltaKv(delta: number | null): DeltaKv {
  if (delta == null || !isFinite(delta)) {
    return { cls: 'delta-flat', iconHref: '/static/icons.svg#minus', text: '—' };
  }
  if (delta < -0.0005) {
    return {
      cls: 'delta-down',
      iconHref: '/static/icons.svg#trending-down',
      text: '−$' + Math.abs(delta).toFixed(3),
    };
  }
  if (delta > 0.0005) {
    return {
      cls: 'delta-up',
      iconHref: '/static/icons.svg#trending-up',
      text: '+$' + delta.toFixed(3),
    };
  }
  return { cls: 'delta-flat', iconHref: '/static/icons.svg#minus', text: '$0.000' };
}

function CanonicalTrendModal({
  source,
  embedded = false,
}: {
  source: DashboardSelection;
  embedded?: boolean;
}) {
  const env = useSnapshot();
  const isClaude = source === 'claude';
  const presentation = presentationTrend(env, source);
  const rows: TrendChartDatum[] = isClaude
    ? buildTrendHistoryData(env)
    : presentation.rows.map((row) => ({ ...row, spark_height: row.dollar_per_pct ?? 0 }));
  // TR-4: index of the week whose tooltip is showing (hover OR keyboard
  // focus). Declared before the empty-state early return to keep hook order
  // stable.
  const [hovered, setHovered] = useState<number | null>(null);
  // EPHEMERAL, modal-local sort override (decision 7 / finding 3). Reset to
  // null on each mount, so opening the modal never inherits or writes the
  // panel's persisted `prefs.trendSortOverride` — sorting this table by Cost
  // must NOT reorder the always-visible Trend panel underneath. Declared
  // before the empty-state early return to keep hook order stable.
  const [localSort, setLocalSort] = useState<SortOverride | null>(null);

  // Shared header extras (share-affordance) reused across the early
  // empty-state return and the main return below.
  const headerExtras = (
    <ShareIcon
      panel="trend"
      panelLabel="Trend"
      triggerId="trend-modal"
      onClick={() => dispatch(openShareModal('trend', 'trend-modal'))}
    />
  );

  const curIdx = findCurrentIndex(rows);
  const cur = rows[curIdx];
  // Issue #59: prefer the envelope's pre-computed median
  // (`trend.history_median_dpp`) so the rule lives in one place
  // (Python's `build_trend_view`). Falls back to the client-side
  // implementation when the field is missing — fixture snapshots and
  // pre-v1.9 envelopes don't carry it.
  const envMedian = isClaude ? env?.trend?.history_median_dpp : null;
  const med =
    envMedian != null && isFinite(envMedian)
      ? envMedian
      : median4NonCurrentFallback(rows, curIdx);
  const delta =
    cur && cur.dollar_per_pct != null && med != null ? cur.dollar_per_pct - med : null;
  const dkv = deltaKv(delta);

  const svgPrims = buildSparklinePrimitives(rows, curIdx);
  const hasUsed = rows.some((r) => r.used_pct != null && isFinite(r.used_pct));
  const N = rows.length;
  const idFor = (base: string): string => embedded ? `${base}-${source}` : base;

  // Decorate rows with their chronological index (finding 2) BEFORE sorting,
  // so the "Now / W−1 / W−2" label and the `.cur` highlight follow the row's
  // identity through any sort — the map index over `tableRows` is the SORTED
  // position and would relabel rows when sorting by Cost/Used%. `applyTableSort`
  // returns the input untouched when `localSort` is null, so the default view
  // stays chronological.
  const decorated: TrendTableRow[] = rows.map((r, i) => ({ ...r, _chronoIdx: i }));
  const tableRows = applyTableSort(decorated, TREND_COLUMNS, localSort);

  // Current $/1% hero tile — shared by the 3-tile (median present) and the
  // collapsed 2-tile (median unavailable) hero layouts.
  const heroCur = (
    <div className="m-kv kv-cur">
      <svg className="icon" aria-hidden="true">
        <use href="/static/icons.svg#dollar" />
      </svg>
      <div>
        <div className="v" id={idFor('mtr-cur')}>
          {cur && cur.dollar_per_pct != null
            ? '$' + cur.dollar_per_pct.toFixed(3)
            : <span className="m-unavailable">—</span>}
        </div>
                <div className="lbl">Current $ / 1%</div>
      </div>
    </div>
  );

  const content = (
      <section className="modal-trend" data-source={source}>
        <div className="m-chipstrip">
          <span className={`m-pill accent-amber${N === 0 ? ' m-unavailable' : ''}`} id={idFor('mtr-weeks-pill')}>
            {N === 0 ? 'History unavailable' : formatWeeksPill(N)}
          </span>
          {!isClaude && <span className="m-pill accent-blue">{source === 'all' ? 'All sources' : 'Codex'}</span>}
        </div>

        <div className="period-two-pane">
          <div className="period-detail-pane">
        {med != null ? (
          <div className="m-hero cols-3">
            {heroCur}
            <div className="m-kv kv-med">
              <svg className="icon" aria-hidden="true">
                <use href="/static/icons.svg#minus" />
              </svg>
              <div>
                <div className="v" id={idFor('mtr-med')}>
                  {'$' + med.toFixed(3)}
                </div>
                <div className="lbl">4-week median</div>
              </div>
            </div>
            <div className={`m-kv kv-delta ${dkv.cls}`} id={idFor('mtr-delta-kv')}>
              <svg className="icon" aria-hidden="true">
                <use href={dkv.iconHref} />
              </svg>
              <div>
                <div className="v" id={idFor('mtr-delta')}>{dkv.text}</div>
                <div className="lbl">vs median</div>
              </div>
            </div>
          </div>
        ) : (
          /* TREND-KPI (decision 8): fewer than 4 valid non-current weeks →
             no median. Collapse cols-3 → cols-2 = [Current $/1%] + one muted
             hint tile that READS as informational ("needs 4 weeks" as the
             value, "Median" as the label) rather than an empty "—" KPI. */
          <div className="m-hero cols-2">
            {heroCur}
            <div className="m-kv kv-med kv-hint" id={idFor('mtr-med-hint')}>
              <svg className="icon" aria-hidden="true">
                <use href="/static/icons.svg#minus" />
              </svg>
              <div>
                <div className="v">needs 4 weeks</div>
                <div className="lbl">Median</div>
              </div>
            </div>
          </div>
        )}

        <h3 className="m-sec sec-spark">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#trending-down" />
          </svg>
          {N > 0 ? `${N}-week` : 'Weekly'} history · $/1%
          <span className={'mtr-legend' + (hasUsed ? '' : ' legend-no-used')} aria-hidden="true">
            <span className="sw sw-dpp" />
            <span className="sw-lbl">$/1%</span>
            <><span className="sw sw-used" /><span className="sw-lbl">used %</span></>
          </span>
        </h3>
        <div className="mtr-sparkhero">
          <svg
            id={idFor('mtr-svg')}
            viewBox="0 0 600 140"
            preserveAspectRatio="none"
            role="group"
            aria-label={`$ per 1% over the last ${N} week${N === 1 ? '' : 's'}, with the used % line overlaid`}
          >
            {svgPrims.map((p) => {
              const { el, attrs, text, key } = p;
              // Convert "class" to "className" for JSX
              const jsxAttrs: Record<string, string | number> = {};
              for (const k in attrs) {
                jsxAttrs[k === 'class' ? 'className' : k] = attrs[k];
              }
              if (el === 'line')
                return <line key={key} {...(jsxAttrs as React.SVGProps<SVGLineElement>)} />;
              if (el === 'polyline')
                return (
                  <polyline
                    key={key}
                    {...(jsxAttrs as React.SVGProps<SVGPolylineElement>)}
                  />
                );
              if (el === 'circle')
                return <circle key={key} {...(jsxAttrs as React.SVGProps<SVGCircleElement>)} />;
              return (
                <text key={key} {...(jsxAttrs as React.SVGProps<SVGTextElement>)}>
                  {text}
                </text>
              );
            })}
            {/* TR-4: one transparent per-week hit-target. Hover OR keyboard
                focus reveals that week's exact $/1% + used %. */}
            {rows.map((r, i) => {
              const hitW = N > 1 ? SPARK_INNER_W / (N - 1) : SPARK_INNER_W;
              const k = N - 1 - i;
              const usedTxt =
                r.used_pct != null && isFinite(r.used_pct)
                  ? Math.round(r.used_pct) + '% used'
                  : 'used —';
              const dppTxt =
                r.dollar_per_pct != null && isFinite(r.dollar_per_pct)
                  ? '$' + r.dollar_per_pct.toFixed(2) + ' / 1%'
                  : '$— / 1%';
              return (
                <rect
                  key={`hit-${i}`}
                  className="mtr-hit"
                  data-testid="mtr-hit"
                  x={sparkXi(i, N) - hitW / 2}
                  y={SPARK_PAD.top}
                  width={hitW}
                  height={SPARK_INNER_H}
                  tabIndex={0}
                  role="button"
                  aria-label={`${historyWlab(r, k)}: ${dppTxt}, ${usedTxt}`}
                  onMouseEnter={() => setHovered(i)}
                  onFocus={() => setHovered(i)}
                  onMouseLeave={() => setHovered(null)}
                  onBlur={() => setHovered(null)}
                />
              );
            })}
          </svg>
          {N === 0 && <div className="empty-state m-unavailable" id={idFor('mtr-empty')}>No history data is available for this source yet.</div>}
          {hovered != null && rows[hovered]
            ? (() => {
                const r = rows[hovered];
                const k = N - 1 - hovered;
                const leftPct = Math.min(
                  92,
                  Math.max(8, (sparkXi(hovered, N) / SPARK_W) * 100),
                );
                const dppTxt =
                  r.dollar_per_pct != null && isFinite(r.dollar_per_pct)
                    ? '$' + r.dollar_per_pct.toFixed(2) + ' / 1%'
                    : '$— / 1%';
                const usedTxt =
                  r.used_pct != null && isFinite(r.used_pct)
                    ? Math.round(r.used_pct) + '% used'
                    : 'used —';
                return (
                  <div
                    className="mtr-tip"
                    role="status"
                    style={{ left: leftPct + '%' }}
                  >
                    <span className="mtr-tip-wk">{historyWlab(r, k)}</span>
                    <span className="mtr-tip-dpp">{dppTxt}</span>
                    <span className="mtr-tip-used">{usedTxt}</span>
                  </div>
                );
              })()
            : null}
          <div className="mtr-sparkaxis" id={idFor('mtr-sparkaxis')}>
            {N > 0 ? (
              <Fragment>
                <span>W−{N - 1}</span>
                <span>W−{Math.floor((N - 1) / 2)}</span>
                <span>Now</span>
              </Fragment>
            ) : null}
          </div>
        </div>
          </div>{/* /period-detail-pane */}

          <div className="period-table-pane">
        <h3 className="m-sec sec-tbl">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#hash" />
          </svg>
          Weekly detail
        </h3>
        <div className="mtr-tbl-head">
          <span className="m-pill accent-purple" id={idFor('mtr-tbl-count')}>
            {N} weeks
          </span>
          <span className="mtr-tbl-sub">
            current row highlighted · negative Δ = $/1% down vs prior week
          </span>
        </div>
        <table className="m-histable" id={idFor('mtr-table')}>
          {/* Sortable header off the shared TREND_COLUMNS (Week · Cost · Used%
              · $/1% · Δ). The override is the modal-local, ephemeral `localSort`
              — NOT the panel's persisted `prefs.trendSortOverride` (decision 7).
              The <tbody> stays hand-rendered so the modal keeps its week labels,
              `.cur` highlight, and delta formatting the panel lacks. */}
          <SortableHeader
            columns={TREND_COLUMNS}
            override={localSort}
            onChange={setLocalSort}
            accentVar="--accent-amber"
          />
          <tbody id={idFor('mtr-rows')}>
            {tableRows.map((r) => {
              // finding 2: the "Now / W−1 / W−2" label keys off the row's
              // chronological identity (`_chronoIdx`), NOT its (possibly
              // sorted) render position — so sorting by Cost/Used% never
              // relabels rows.
              const k = N - 1 - r._chronoIdx;
              const wlab = historyWlab(r, k);
              const usedTxt = r.used_pct != null ? Math.round(r.used_pct) + '%' : 'Unavailable';
              const dppTxt =
                r.dollar_per_pct != null ? '$' + r.dollar_per_pct.toFixed(2) : 'Unavailable';
              const d = renderDelta(r.delta);
              return (
                <tr
                  key={r.label + '|' + r._chronoIdx}
                  className={r.is_current ? 'cur' : undefined}
                >
                  <td>
                    <span className="wlab">{wlab}</span>
                  </td>
                  <td className="num c-cost">{fmt.usd2(r.cost_usd)}</td>
                  <td className={`num usedpct${r.used_pct == null ? ' m-unavailable' : ''}`}>{usedTxt}</td>
                  <td className={`num dpp${r.dollar_per_pct == null ? ' m-unavailable' : ''}`}>{dppTxt}</td>
                  <td className={`num delta ${d.cls}`}>{d.text}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
          </div>{/* /period-table-pane */}
        </div>{/* /period-two-pane */}
      </section>
  );
  if (embedded) return content;
  return (
    <Modal
      title={N > 0 ? `Trend — last ${N} weeks` : 'Trend'}
      accentClass="accent-amber"
      headerExtras={headerExtras}
      wide
    >
      {content}
    </Modal>
  );
}

export function TrendModal() {
  const env = useSnapshot();
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  if (source !== 'all') return <CanonicalTrendModal source={source} />;
  const presentation = presentationTrend(env, 'all');
  const headerExtras = (
    <ShareIcon
      panel="trend"
      panelLabel="Trend"
      triggerId="trend-modal"
      onClick={() => dispatch(openShareModal('trend', 'trend-modal'))}
    />
  );
  const claudeN = presentation.sections.find((section) => section.source === 'claude')?.historyRows.length ?? 0;
  const codexN = presentation.sections.find((section) => section.source === 'codex')?.historyRows.length ?? 0;
  return (
    <Modal
      title={`Trend · Claude ${claudeN} weeks · Codex ${codexN} cycles`}
      accentClass="accent-amber"
      headerExtras={headerExtras}
      wide
      dataSource="all"
    >
      <div className="provider-composition provider-composition--modal trend-modal-composition">
        {presentation.sections.map((section) => (
          <section
            key={section.source}
            className="provider-composition-section"
            data-provider-section={section.source}
            aria-label={`${section.label} $ per 1% trend history`}
          >
            <div className="source-provider-head provider-composition-head">
              <span className={`source-chip source-chip--${section.source}`}>{section.label}</span>
              <span className="provider-summary-label">
                {section.historyRows.length} {section.source === 'claude' ? 'weeks' : 'cycles'}
              </span>
            </div>
            <CanonicalTrendModal source={section.source} embedded />
          </section>
        ))}
      </div>
    </Modal>
  );
}
