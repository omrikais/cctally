import { useEffect, useRef } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Modal } from './Modal';
import { ShareIcon } from '../components/ShareIcon';
import { resolveVerdict } from '../lib/verdict';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { ForecastEnvelope } from '../types/envelope';

// The range bar (pills + leaders + 3-zone track + bounds) is built via
// DOM-mutating layout in a ref effect; React manages only the container
// tree above that. Each pin measures its rendered width to resolve
// overlap so the two projection pills never visually collide.

interface ExplainWeek {
  elapsed_hours?: number | null;
  remaining_hours?: number | null;
}

function fmtWeekDone(wk: ExplainWeek | null | undefined): string {
  if (!wk) return '—';
  const el = wk.elapsed_hours;
  const rm = wk.remaining_hours;
  if (el == null || rm == null) return '—';
  const total = el + rm;
  if (total <= 0) return '—';
  const pct = (el / total) * 100;
  return Math.min(100, pct).toFixed(1) + '%';
}

function clamp0_110(v: number | null | undefined): number | null {
  if (v == null || !isFinite(v)) return null;
  if (v < 0) return 0;
  if (v > 110) return 110;
  return v;
}

function pillTextFor(raw: number | null | undefined): string {
  if (raw == null || !isFinite(raw)) return '—';
  if (raw < 0) return '~' + (+raw).toFixed(1) + '%';
  return (+raw).toFixed(1) + '%';
}

interface RangePin {
  kind: 'wa' | 'r24';
  pos: number;           // clamped 0..110
  raw: number;           // raw projection for pill text
  trueXPct: number;
  resolvedXPct: number;
  pillWidthPx: number;
  pillEl?: HTMLElement;
}

function resolvePillPositions(pins: RangePin[], wrapWidthPx: number): void {
  const MIN_GAP_PX = 8;
  for (const p of pins) p.trueXPct = (p.pos / 110) * 100;
  if (pins.length < 2 || wrapWidthPx <= 0) {
    for (const p of pins) p.resolvedXPct = p.trueXPct;
    return;
  }
  const sorted = pins.slice().sort((a, b) => a.trueXPct - b.trueXPct);
  const a = sorted[0];
  const b = sorted[1];
  const aHalfWPct = (a.pillWidthPx / 2 / wrapWidthPx) * 100;
  const bHalfWPct = (b.pillWidthPx / 2 / wrapWidthPx) * 100;
  const minGapPct = (MIN_GAP_PX / wrapWidthPx) * 100;
  const overlapThresholdPct = aHalfWPct + bHalfWPct + minGapPct;
  const resolvedGapPct = 2 * overlapThresholdPct;
  if (b.trueXPct - a.trueXPct < overlapThresholdPct) {
    const midpoint = (a.trueXPct + b.trueXPct) / 2;
    a.resolvedXPct = midpoint - resolvedGapPct / 2;
    b.resolvedXPct = midpoint + resolvedGapPct / 2;
    const aMinX = aHalfWPct;
    const bMaxX = 100 - bHalfWPct;
    if (a.resolvedXPct < aMinX) {
      a.resolvedXPct = aMinX;
      if (b.resolvedXPct - a.resolvedXPct < resolvedGapPct) {
        b.resolvedXPct = Math.min(bMaxX, a.resolvedXPct + resolvedGapPct);
      }
    }
    if (b.resolvedXPct > bMaxX) {
      b.resolvedXPct = bMaxX;
      if (b.resolvedXPct - a.resolvedXPct < resolvedGapPct) {
        a.resolvedXPct = Math.max(aMinX, b.resolvedXPct - resolvedGapPct);
      }
    }
  } else {
    a.resolvedXPct = a.trueXPct;
    b.resolvedXPct = b.trueXPct;
  }
}

function useRangeBar(
  wrapRef: React.RefObject<HTMLDivElement>,
  trackRef: React.RefObject<HTMLDivElement>,
  fc: ForecastEnvelope | null,
): void {
  useEffect(() => {
    const wrapEl = wrapRef.current;
    const trackEl = trackRef.current;
    if (!wrapEl || !trackEl || !fc) return;

    const pillsEl = wrapEl.querySelector<HTMLDivElement>('.mfc-pills');
    const leadersEl = wrapEl.querySelector<SVGSVGElement>('.mfc-leaders');
    const rangeBandEl = trackEl.querySelector<HTMLDivElement>('.mfc-rangeband');
    if (!pillsEl || !leadersEl) return;

    const waProj = fc.week_avg_projection_pct;
    const r24Proj = fc.recent_24h_projection_pct;
    const wa = clamp0_110(waProj);
    const r24 = clamp0_110(r24Proj);
    const rawPins: { kind: 'wa' | 'r24'; pos: number; raw: number }[] = [];
    if (wa != null && waProj != null) rawPins.push({ kind: 'wa', pos: wa, raw: waProj });
    if (r24 != null && r24Proj != null) rawPins.push({ kind: 'r24', pos: r24, raw: r24Proj });

    // Remove chevs/pills whose kind is no longer present.
    const wantedKinds = new Set(rawPins.map((p) => p.kind));
    trackEl.querySelectorAll<HTMLElement>('.mfc-chev').forEach((el) => {
      const k = el.dataset.kind;
      if (!k || !wantedKinds.has(k as 'wa' | 'r24')) el.remove();
    });
    pillsEl.querySelectorAll<HTMLElement>('.mfc-pill').forEach((el) => {
      const k = el.dataset.kind;
      if (!k || !wantedKinds.has(k as 'wa' | 'r24')) el.remove();
    });

    const pins: RangePin[] = rawPins.map((p) => ({
      ...p,
      trueXPct: 0,
      resolvedXPct: 0,
      pillWidthPx: 0,
    }));

    for (const p of pins) {
      const x = (p.pos / 110) * 100;
      let chev = trackEl.querySelector<HTMLElement>(`.mfc-chev[data-kind="${p.kind}"]`);
      if (!chev) {
        chev = document.createElement('div');
        chev.className = 'mfc-chev ' + p.kind;
        chev.dataset.kind = p.kind;
        trackEl.appendChild(chev);
      }
      chev.style.left = x + '%';

      let pill = pillsEl.querySelector<HTMLElement>(`.mfc-pill[data-kind="${p.kind}"]`);
      const newText = pillTextFor(p.raw);
      if (!pill) {
        pill = document.createElement('div');
        pill.className = 'mfc-pill ' + p.kind;
        pill.dataset.kind = p.kind;
        pill.textContent = newText;
        pillsEl.appendChild(pill);
      } else if (pill.textContent !== newText) {
        pill.textContent = newText;
      }
      p.pillEl = pill;
    }

    if (rangeBandEl) {
      let show = false;
      if (pins.length === 2) {
        const xa = (pins[0].pos / 110) * 100;
        const xb = (pins[1].pos / 110) * 100;
        const lo = Math.min(xa, xb);
        const hi = Math.max(xa, xb);
        const width = hi - lo;
        if (width >= 0.5) {
          rangeBandEl.style.left = lo + '%';
          rangeBandEl.style.width = width + '%';
          show = true;
        }
      }
      rangeBandEl.hidden = !show;
    }

    const applyLayout = (): void => {
      for (const p of pins) {
        if (!p.pillEl || !p.pillEl.isConnected) return;
      }
      const wrapWidthPx = wrapEl.clientWidth;
      if (wrapWidthPx <= 0) {
        requestAnimationFrame(applyLayout);
        return;
      }
      for (const p of pins) {
        const r = p.pillEl!.getBoundingClientRect();
        p.pillWidthPx = r.width;
      }
      resolvePillPositions(pins, wrapWidthPx);
      for (const p of pins) {
        p.pillEl!.style.left = p.resolvedXPct + '%';
      }
      // Rebuild SVG leaders each tick
      while (leadersEl.firstChild) leadersEl.removeChild(leadersEl.firstChild);
      const SVG_NS = 'http://www.w3.org/2000/svg';
      for (const p of pins) {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('class', p.kind);
        path.setAttribute('vector-effect', 'non-scaling-stroke');
        path.setAttribute(
          'd',
          `M ${p.resolvedXPct} 0 C ${p.resolvedXPct} 10, ${p.trueXPct} 8, ${p.trueXPct} 18`,
        );
        leadersEl.appendChild(path);
      }
    };
    applyLayout();
  }, [wrapRef, trackRef, fc]);
}

export function ForecastModal() {
  const env = useSnapshot();
  const fc = env?.forecast ?? null;
  const explain = (fc?.explain ?? null) as
    | { rates?: { dollars_per_percent?: number | null; week_average_pct_per_hour?: number | null; recent_24h_pct_per_hour?: number | null }; week?: ExplainWeek }
    | null;
  const wrapRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  useRangeBar(wrapRef, trackRef, fc);

  const headerExtras = (
    <ShareIcon
      panel="forecast"
      panelLabel="Forecast"
      triggerId="forecast-modal"
      onClick={() => dispatch(openShareModal('forecast', 'forecast-modal'))}
    />
  );

  if (!fc) {
    return (
      <Modal
        title="Forecast — explain"
        accentClass="accent-purple"
        headerExtras={headerExtras}
      >
        <section className="modal-forecast">
          <p className="empty-state" id="mfc-empty">
            No forecast data yet.
          </p>
        </section>
      </Modal>
    );
  }

  const vinfo = resolveVerdict(fc.verdict);
  const verdictCls = vinfo ? `m-pill ${vinfo.accent}` : 'm-pill';
  const verdictText = vinfo ? `${vinfo.glyph} ${vinfo.label}` : '—';

  let confCls = 'm-pill';
  let confText = '—';
  let confHidden = true;
  if (fc.confidence === 'high') {
    confCls = 'm-pill accent-blue';
    confText = 'high confidence';
    confHidden = false;
  } else if (fc.confidence === 'low') {
    confCls = 'm-pill accent-red';
    confText = 'low confidence';
    confHidden = false;
  }

  const rates = explain?.rates;
  const week = explain?.week;

  return (
    <Modal
      title="Forecast — explain"
      accentClass="accent-purple"
      headerExtras={headerExtras}
    >
      <section className="modal-forecast">
        <div className="m-chipstrip" id="mfc-chips">
          <span className={verdictCls} id="mfc-verdict">
            {verdictText}
          </span>
          <span className={confCls} id="mfc-confidence" hidden={confHidden}>
            {confText}
          </span>
        </div>

        <div className="m-hero cols-2">
          <div className="m-kv kv-wa">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#gauge" />
            </svg>
            <div>
              <div className="v" id="mfc-wa-pct">{fmt.pct1(fc.week_avg_projection_pct)}</div>
              <div className="lbl">Week-avg projection</div>
              <div className="sub" id="mfc-wa-sub">
                {fmt.ratePctPerHour(rates?.week_average_pct_per_hour)}
              </div>
            </div>
          </div>
          <div className="m-kv kv-r24">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#zap" />
            </svg>
            <div>
              <div className="v" id="mfc-r24-pct">{fmt.pct1(fc.recent_24h_projection_pct)}</div>
              <div className="lbl">Recent-24h projection</div>
              <div className="sub" id="mfc-r24-sub">
                {fmt.ratePctPerHour(rates?.recent_24h_pct_per_hour)}
              </div>
            </div>
          </div>
        </div>

        <h3 className="m-sec sec-range">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#bar-chart" />
          </svg>
          Range vs. caps
        </h3>
        <div className="mfc-rangewrap" id="mfc-rangewrap" ref={wrapRef}>
          <div className="mfc-pills" id="mfc-pills" />
          <svg
            className="mfc-leaders"
            id="mfc-leaders"
            viewBox="0 0 100 18"
            preserveAspectRatio="none"
            aria-hidden="true"
          />
          <div className="mfc-rangetrack" id="mfc-rangetrack" ref={trackRef}>
            <div className="mfc-zone safe" />
            <div className="mfc-zone warn" />
            <div className="mfc-zone over" />
            <div className="mfc-rangeband" id="mfc-rangeband" hidden />
            <div className="mfc-bound b90">
              <div className="lbl">90%</div>
            </div>
            <div className="mfc-bound b100">
              <div className="lbl">100%</div>
            </div>
          </div>
        </div>

        <h3 className="m-sec sec-rates">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#activity" />
          </svg>
          Rates
        </h3>
        <div className="mfc-kvgrid">
          <div className="mfc-krow">
            <span className="l">$ / 1%</span>
            <span className="v v-cyan" id="mfc-dpp">{fmt.usd3(rates?.dollars_per_percent)}</span>
          </div>
          <div className="mfc-krow">
            <span className="l">week done</span>
            <span className="v v-green" id="mfc-wkdone">{fmtWeekDone(week)}</span>
          </div>
          <div className="mfc-krow">
            <span className="l">elapsed</span>
            <span className="v v-green" id="mfc-elapsed">{fmt.hours1(week?.elapsed_hours)}</span>
          </div>
          <div className="mfc-krow">
            <span className="l">remaining</span>
            <span className="v" id="mfc-remain">{fmt.hours1(week?.remaining_hours)}</span>
          </div>
        </div>

        <h3 className="m-sec sec-bud">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#dollar" />
          </svg>
          Daily budgets to stay under
        </h3>
        <div className="mfc-kvgrid mfc-kvgrid-single">
          <div className="mfc-krow">
            <span className="l">@ 100% cap</span>
            <span className="v v-magenta" id="mfc-bud100">{fmt.usd2PerDay(fc.budget_100_per_day_usd)}</span>
          </div>
          <div className="mfc-krow">
            <span className="l">@ 90% cap</span>
            <span className="v v-amber" id="mfc-bud90">{fmt.usd2PerDay(fc.budget_90_per_day_usd)}</span>
          </div>
        </div>
      </section>
    </Modal>
  );
}
