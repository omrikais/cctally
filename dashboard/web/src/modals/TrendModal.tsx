import { Fragment } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Modal } from './Modal';
import { buildTrendHistoryData, type TrendChartDatum } from '../store/selectors';

function formatWeeksPill(n: number): string {
  const months = Math.max(1, Math.round(n / 4));
  return `${n} week${n === 1 ? '' : 's'} · ${months} month${months === 1 ? '' : 's'}`;
}

function findCurrentIndex(rows: TrendChartDatum[]): number {
  const i = rows.findIndex((r) => r && r.is_current);
  return i >= 0 ? i : rows.length - 1;
}

function median4NonCurrent(rows: TrendChartDatum[], curIdx: number): number | null {
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

function buildSparklinePrimitives(rows: TrendChartDatum[], curIdx: number): SvgPrimitive[] {
  const out: SvgPrimitive[] = [];
  if (!rows.length) return out;
  const W = 600;
  const H = 140;
  const PAD = { left: 44, right: 14, top: 12, bottom: 22 };
  const INNER_W = W - PAD.left - PAD.right;
  const INNER_H = H - PAD.top - PAD.bottom;
  const N = rows.length;
  const xi = (i: number) => PAD.left + (N === 1 ? INNER_W : (i / (N - 1)) * INNER_W);

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
        text: 'median $' + med.toFixed(2),
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

  // 6) Y labels (primary)
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

export function TrendModal() {
  const env = useSnapshot();
  const rows = buildTrendHistoryData(env);

  if (!rows.length) {
    return (
      <Modal title="Trend — 12-week history" accentClass="accent-amber">
        <section className="modal-trend">
          <p className="empty-state" id="mtr-empty">
            No history data yet.
          </p>
        </section>
      </Modal>
    );
  }

  const curIdx = findCurrentIndex(rows);
  const cur = rows[curIdx];
  const med = median4NonCurrent(rows, curIdx);
  const delta =
    cur && cur.dollar_per_pct != null && med != null ? cur.dollar_per_pct - med : null;
  const dkv = deltaKv(delta);

  const svgPrims = buildSparklinePrimitives(rows, curIdx);
  const hasUsed = rows.some((r) => r.used_pct != null && isFinite(r.used_pct));
  const N = rows.length;

  return (
    <Modal title="Trend — 12-week history" accentClass="accent-amber">
      <section className="modal-trend">
        <div className="m-chipstrip">
          <span className="m-pill accent-amber" id="mtr-weeks-pill">
            {formatWeeksPill(N)}
          </span>
        </div>

        <div className="m-hero cols-3">
          <div className="m-kv kv-cur">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#dollar" />
            </svg>
            <div>
              <div className="v" id="mtr-cur">
                {cur && cur.dollar_per_pct != null
                  ? '$' + cur.dollar_per_pct.toFixed(3)
                  : '—'}
              </div>
              <div className="lbl">Current $ / 1%</div>
            </div>
          </div>
          <div className="m-kv kv-med">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#minus" />
            </svg>
            <div>
              <div className="v" id="mtr-med">
                {med != null ? '$' + med.toFixed(3) : '—'}
              </div>
              <div className="lbl">4-week median</div>
            </div>
          </div>
          <div className={`m-kv kv-delta ${dkv.cls}`} id="mtr-delta-kv">
            <svg className="icon" aria-hidden="true">
              <use href={dkv.iconHref} />
            </svg>
            <div>
              <div className="v" id="mtr-delta">{dkv.text}</div>
              <div className="lbl">vs median</div>
            </div>
          </div>
        </div>

        <h3 className="m-sec sec-spark">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#trending-down" />
          </svg>
          12-week history · $/1%
          <span className={'mtr-legend' + (hasUsed ? '' : ' legend-no-used')} aria-hidden="true">
            <span className="sw sw-dpp" />
            <span className="sw-lbl">$/1%</span>
            <span className="sw sw-used" />
            <span className="sw-lbl">used %</span>
          </span>
        </h3>
        <div className="mtr-sparkhero">
          <svg
            id="mtr-svg"
            viewBox="0 0 600 140"
            preserveAspectRatio="none"
            aria-hidden="true"
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
          </svg>
          <div className="mtr-sparkaxis" id="mtr-sparkaxis">
            {N > 0 ? (
              <Fragment>
                <span>W−{N - 1}</span>
                <span>W−{Math.floor((N - 1) / 2)}</span>
                <span>Now</span>
              </Fragment>
            ) : null}
          </div>
        </div>

        <h3 className="m-sec sec-tbl">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#hash" />
          </svg>
          Weekly detail
        </h3>
        <div className="mtr-tbl-head">
          <span className="m-pill accent-purple" id="mtr-tbl-count">
            {N} weeks
          </span>
          <span className="mtr-tbl-sub">
            current row highlighted · negative Δ = $/1% down vs prior week
          </span>
        </div>
        <table className="m-histable" id="mtr-table">
          <thead>
            <tr>
              <th>Week</th>
              <th className="num">Used %</th>
              <th className="num">$ / 1%</th>
              <th className="num">Δ $/1%</th>
            </tr>
          </thead>
          <tbody id="mtr-rows">
            {rows.map((r, i) => {
              const k = N - 1 - i;
              const wlab = historyWlab(r, k);
              const usedTxt = r.used_pct != null ? Math.round(r.used_pct) + '%' : '—';
              const dppTxt =
                r.dollar_per_pct != null ? '$' + r.dollar_per_pct.toFixed(2) : '—';
              const d = renderDelta(r.delta);
              return (
                <tr key={r.label + '|' + i} className={r.is_current ? 'cur' : undefined}>
                  <td>
                    <span className="wlab">{wlab}</span>
                  </td>
                  <td className="num usedpct">{usedTxt}</td>
                  <td className="num dpp">{dppTxt}</td>
                  <td className={`num delta ${d.cls}`}>{d.text}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </Modal>
  );
}
