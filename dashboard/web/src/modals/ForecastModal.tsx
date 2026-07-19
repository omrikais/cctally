import { useEffect, useRef, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Modal } from './Modal';
import { ShareIcon } from '../components/ShareIcon';
import { resolveVerdict } from '../lib/verdict';
import { fmt } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { presentationForecast } from '../lib/dashboardPresentation';
import type { DashboardSelection, ForecastEnvelope } from '../types/envelope';

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

// FC-1 pill-layout model. Pure (px in, layout decision out) so the
// overlap/collapse math is unit-tested without a browser. Input pins carry
// the measured pill width; the resolver either returns per-pin resolved
// x-positions (a true hard min-gap in PIXEL space — the prior percent-space
// resolver under-corrected and let the two pills still touch) OR a collapse
// signal when the two pills plus the min-gap physically cannot fit the wrap,
// in which case the effect renders one centered range pill instead.
export interface PillPin {
  kind: 'wa' | 'r24';
  pos: number;           // clamped 0..110
  raw: number;           // raw projection for pill text
  pillWidthPx: number;
}

export interface ResolvedPillPin extends PillPin {
  trueXPct: number;      // un-adjusted position (leader anchor on the track)
  resolvedXPct: number;  // overlap-resolved pill center
}

// Flat (non-discriminated) shape so callers read `collapsed`, `pins`, and
// `rangeText` without narrowing: `pins` is set iff not collapsed,
// `rangeText` iff collapsed.
export interface PillLayout {
  collapsed: boolean;
  pins?: ResolvedPillPin[];
  rangeText?: string;
}

export function resolvePillLayout(
  pins: PillPin[],
  wrapWidthPx: number,
  minGapPx = 8,
): PillLayout {
  const resolved: ResolvedPillPin[] = pins.map((p) => ({
    ...p,
    trueXPct: (p.pos / 110) * 100,
    resolvedXPct: (p.pos / 110) * 100,
  }));

  if (resolved.length < 2 || wrapWidthPx <= 0) {
    return { collapsed: false, pins: resolved };
  }

  // Collapse when the two pills + the min-gap cannot physically fit.
  const p0 = pins[0];
  const p1 = pins[1];
  const needPx = p0.pillWidthPx + minGapPx + p1.pillWidthPx;
  if (needPx > wrapWidthPx) {
    const lo = Math.min(p0.raw, p1.raw);
    const hi = Math.max(p0.raw, p1.raw);
    return { collapsed: true, rangeText: `${lo.toFixed(1)}–${hi.toFixed(1)}%` };
  }

  // Resolve in pixel space with a true hard min-gap (edge-to-edge).
  const sorted = resolved.slice().sort((a, b) => a.trueXPct - b.trueXPct);
  const a = sorted[0];
  const b = sorted[1];
  const aHalf = a.pillWidthPx / 2;
  const bHalf = b.pillWidthPx / 2;
  const minCenterDist = aHalf + bHalf + minGapPx;
  let ax = (a.trueXPct / 100) * wrapWidthPx;
  let bx = (b.trueXPct / 100) * wrapWidthPx;
  if (bx - ax < minCenterDist) {
    const mid = (ax + bx) / 2;
    ax = mid - minCenterDist / 2;
    bx = mid + minCenterDist / 2;
    // Clamp within [aHalf, wrap - bHalf], preserving the min gap. `needPx`
    // <= wrap guarantees the pair fits inside these bounds.
    const aMin = aHalf;
    const bMax = wrapWidthPx - bHalf;
    if (ax < aMin) {
      ax = aMin;
      bx = Math.max(bx, ax + minCenterDist);
    }
    if (bx > bMax) {
      bx = bMax;
      ax = Math.min(ax, bx - minCenterDist);
    }
    if (ax < aMin) ax = aMin;
  }
  a.resolvedXPct = (ax / wrapWidthPx) * 100;
  b.resolvedXPct = (bx / wrapWidthPx) * 100;
  return { collapsed: false, pins: resolved };
}

interface WorkPin {
  kind: 'wa' | 'r24';
  pos: number;   // clamped 0..110
  raw: number;   // raw projection for pill text
  pillEl?: HTMLElement;
}

function useRangeBar(
  wrapRef: React.RefObject<HTMLDivElement>,
  trackRef: React.RefObject<HTMLDivElement>,
  fc: ForecastEnvelope | null,
  nowPct: number | null,
): void {
  // Whether the last layout collapsed to a single range pill. Read at the
  // NEXT effect's kind-cleanup so the synthetic `.mfc-pill.range` (whose
  // `data-kind="range"` is not a live projection kind) is preserved rather
  // than swept away before applyLayout re-decides.
  const collapsedRef = useRef(false);
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

    // Remove chevs/pills whose kind is no longer present. The collapsed
    // range pill (data-kind="range") is preserved while collapsed.
    const wantedKinds = new Set<string>(rawPins.map((p) => p.kind));
    if (collapsedRef.current) wantedKinds.add('range');
    trackEl.querySelectorAll<HTMLElement>('.mfc-chev').forEach((el) => {
      const k = el.dataset.kind;
      if (!k || !wantedKinds.has(k)) el.remove();
    });
    pillsEl.querySelectorAll<HTMLElement>('.mfc-pill').forEach((el) => {
      const k = el.dataset.kind;
      if (!k || !wantedKinds.has(k)) el.remove();
    });

    const pins: WorkPin[] = rawPins.map((p) => ({ ...p }));

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

    // "now" marker — the current weekly used %, on the same 0–110 scale as
    // the projections. Positioned independently of the pill overlap pass
    // (it never collides with the pills, which sit above the track).
    {
      let nowEl = trackEl.querySelector<HTMLElement>('.mfc-now');
      const nowClamped = clamp0_110(nowPct);
      if (nowClamped == null) {
        if (nowEl) nowEl.remove();
      } else {
        if (!nowEl) {
          nowEl = document.createElement('div');
          nowEl.className = 'mfc-now';
          const glyph = document.createElement('span');
          glyph.className = 'mfc-now-glyph';
          glyph.textContent = '▸';
          nowEl.appendChild(glyph);
          trackEl.appendChild(nowEl);
        }
        nowEl.style.left = (nowClamped / 110) * 100 + '%';
      }
    }

    let rafHandle: number | null = null;
    const applyLayout = (): void => {
      // Bail (and stop rescheduling) if the wrap has detached — e.g. the
      // modal unmounted or the effect re-ran while a 0-width rAF was still
      // pending. Without this the rAF below would self-perpetuate on stale
      // refs (a detached node keeps reporting clientWidth === 0).
      if (!wrapEl.isConnected) return;
      for (const p of pins) {
        if (!p.pillEl || !p.pillEl.isConnected) return;
      }
      const wrapWidthPx = wrapEl.clientWidth;
      if (wrapWidthPx <= 0) {
        schedule();
        return;
      }
      const pinInputs: PillPin[] = pins.map((p) => ({
        kind: p.kind,
        pos: p.pos,
        raw: p.raw,
        pillWidthPx: p.pillEl!.getBoundingClientRect().width,
      }));
      const layout = resolvePillLayout(pinInputs, wrapWidthPx);
      collapsedRef.current = layout.collapsed;

      const chevs = trackEl.querySelectorAll<HTMLElement>('.mfc-chev');

      if (layout.collapsed) {
        // One centered range pill; per-kind pills, chevrons + leaders hide.
        for (const p of pins) p.pillEl!.style.display = 'none';
        chevs.forEach((c) => {
          c.style.display = 'none';
        });
        while (leadersEl.firstChild) leadersEl.removeChild(leadersEl.firstChild);
        let range = pillsEl.querySelector<HTMLElement>('.mfc-pill.range');
        if (!range) {
          range = document.createElement('div');
          range.className = 'mfc-pill range';
          range.dataset.kind = 'range';
          pillsEl.appendChild(range);
        }
        const rangeText = layout.rangeText ?? '';
        if (range.textContent !== rangeText) range.textContent = rangeText;
        range.style.left = '50%';
        return;
      }

      // Not collapsed — drop any range pill, restore per-kind pills + chevs.
      pillsEl
        .querySelectorAll<HTMLElement>('.mfc-pill.range')
        .forEach((el) => el.remove());
      for (const p of pins) p.pillEl!.style.display = '';
      chevs.forEach((c) => {
        c.style.display = '';
      });

      const resolved = layout.pins ?? [];
      const byKind = new Map(resolved.map((r) => [r.kind, r]));
      for (const p of pins) {
        const r = byKind.get(p.kind);
        if (r) p.pillEl!.style.left = r.resolvedXPct + '%';
      }
      // Rebuild SVG leaders each tick.
      while (leadersEl.firstChild) leadersEl.removeChild(leadersEl.firstChild);
      const SVG_NS = 'http://www.w3.org/2000/svg';
      for (const r of resolved) {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('class', r.kind);
        path.setAttribute('vector-effect', 'non-scaling-stroke');
        path.setAttribute(
          'd',
          `M ${r.resolvedXPct} 0 C ${r.resolvedXPct} 10, ${r.trueXPct} 8, ${r.trueXPct} 18`,
        );
        leadersEl.appendChild(path);
      }
    };
    function schedule(): void {
      if (rafHandle != null) cancelAnimationFrame(rafHandle);
      rafHandle = requestAnimationFrame(() => {
        rafHandle = null;
        applyLayout();
      });
    }

    applyLayout();

    // Reflow immediately on an in-place resize of the open modal, instead of
    // waiting for the next snapshot tick to re-run the effect (#257). A
    // ResizeObserver on the wrap supersets a window-resize listener (it fires
    // on any width change of the wrap). Guarded for runtimes without RO; the
    // vitest harness polyfills it (__tests__/setup.ts).
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => schedule());
      ro.observe(wrapEl);
    }

    return () => {
      if (rafHandle != null) cancelAnimationFrame(rafHandle);
      if (ro) ro.disconnect();
    };
  }, [wrapRef, trackRef, fc, nowPct]);
}

function CanonicalForecastModal({ source }: { source: DashboardSelection }) {
  const env = useSnapshot();
  const isClaude = source === 'claude';
  const presented = presentationForecast(env, source);
  const nativeHistory = env?.sources?.codex?.data?.quota.histories.find(
    (row) => row.window_minutes === 10_080,
  ) ?? env?.sources?.codex?.data?.quota.histories[0];
  const nativeForecast = nativeHistory?.forecast;
  const nativeRemainingHours = nativeForecast?.remaining_seconds == null
    ? null
    : nativeForecast.remaining_seconds / 3600;
  const nativeElapsedHours = nativeHistory?.window_minutes == null || nativeRemainingHours == null
    ? null
    : Math.max(0, nativeHistory.window_minutes / 60 - nativeRemainingHours);
  const nativeBudgetPace = env?.sources?.codex?.data?.budget.status?.pace.daily_usd ?? null;
  const fc: ForecastEnvelope | null = isClaude
    ? env?.forecast ?? null
    : {
        verdict: presented.verdict ?? 'ok',
        week_avg_projection_pct: presented.projected,
        recent_24h_projection_pct: presented.recent,
        budget_100_per_day_usd: null,
        budget_90_per_day_usd: null,
        confidence: nativeForecast?.confidence === 'high' ? 'high' : 'unknown',
        confidence_score: 0,
        explain: {
          rates: {
            dollars_per_percent: null,
            week_average_pct_per_hour: nativeForecast?.rate_percent_per_hour ?? null,
            recent_24h_pct_per_hour: null,
          },
          week: { elapsed_hours: nativeElapsedHours, remaining_hours: nativeRemainingHours },
        },
      };
  const explain = (fc?.explain ?? null) as
    | { rates?: { dollars_per_percent?: number | null; week_average_pct_per_hour?: number | null; recent_24h_pct_per_hour?: number | null }; week?: ExplainWeek }
    | null;
  const wrapRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  // "now" anchor for the range bar — the current weekly used %. Prefer the
  // header value, fall back to current_week; null hides the marker.
  const nowPct = isClaude
    ? env?.header?.used_pct ?? env?.current_week?.used_pct ?? null
    : presented.recent;
  useRangeBar(wrapRef, trackRef, fc, nowPct);

  const headerExtras = (
    <ShareIcon
      panel="forecast"
      panelLabel="Forecast"
      triggerId="forecast-modal"
      onClick={() => dispatch(openShareModal('forecast', 'forecast-modal'))}
    />
  );

  const vinfo = resolveVerdict(isClaude ? fc?.verdict : presented.verdict);
  const verdictCls = vinfo ? `m-pill ${vinfo.accent}` : 'm-pill';
  const verdictText = vinfo ? `${vinfo.glyph} ${vinfo.label}` : '—';

  let confCls = 'm-pill';
  let confText = '—';
  let confHidden = true;
  if (fc?.confidence === 'high') {
    confCls = 'm-pill accent-blue';
    confText = 'high confidence';
    confHidden = false;
  } else if (fc?.confidence === 'low') {
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
      <section className="modal-forecast" data-source={source}>
        <div className="m-chipstrip" id="mfc-chips">
          <span className={verdictCls} id="mfc-verdict">
            {verdictText}
          </span>
          <span className={confCls} id="mfc-confidence" hidden={confHidden}>
            {confText}
          </span>
          {!isClaude && (
            <span className="m-pill accent-blue">
              {source === 'all' ? 'Codex quota · combined spend' : 'Codex native quota'}
            </span>
          )}
          {((isClaude && !fc) || (!isClaude && nativeForecast == null)) && (
            <span className="m-pill m-unavailable">Forecast unavailable</span>
          )}
        </div>

        <div className="m-hero cols-2">
          <div className="m-kv kv-wa">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#gauge" />
            </svg>
            <div>
              <div className={`v${fc?.week_avg_projection_pct == null && !isClaude ? ' m-unavailable' : ''}`} id="mfc-wa-pct">{fmt.pct1(fc?.week_avg_projection_pct)}</div>
              <div className="lbl">{isClaude ? 'Week-avg projection' : presented.primaryLabel}</div>
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
              <div className={`v${fc?.recent_24h_projection_pct == null && !isClaude ? ' m-unavailable' : ''}`} id="mfc-r24-pct">{fmt.pct1(fc?.recent_24h_projection_pct)}</div>
              <div className="lbl">{isClaude ? 'Recent-24h projection' : presented.recentLabel}</div>
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
        {/* Legend — swatch colors MUST match the rendered pill/chev colors
            (week-avg is blue, recent-24h is amber throughout the modal). */}
        <div className="mfc-legend" id="mfc-legend">
          <span className="mfc-leg-item">
            <span className="mfc-leg-sw wa" />
            {isClaude ? 'week-avg' : 'projected'}
          </span>
          <span className="mfc-leg-item">
            <span className="mfc-leg-sw r24" />
            {isClaude ? 'recent-24h' : 'current'}
          </span>
          <span className="mfc-leg-item">
            <span className="mfc-leg-now">▸</span>
            now
          </span>
        </div>
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
            <div className="mfc-bound b0">
              <div className="lbl">0%</div>
            </div>
            <div className="mfc-bound b90">
              <div className="lbl">90%</div>
            </div>
            <div className="mfc-bound b100">
              <div className="lbl">100%</div>
            </div>
            <div className="mfc-bound b110">
              <div className="lbl">110%</div>
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
            <span className={`v v-cyan${!isClaude ? ' m-unavailable' : ''}`} id="mfc-dpp">
              {isClaude ? fmt.usd3(rates?.dollars_per_percent) : 'Unavailable'}
            </span>
          </div>
          {!isClaude && <div className="mfc-krow">
            <span className="l">Quota rate</span>
            <span className={`v v-cyan${rates?.week_average_pct_per_hour == null ? ' m-unavailable' : ''}`}>
              {fmt.ratePctPerHour(rates?.week_average_pct_per_hour)}
            </span>
          </div>}
          <div className="mfc-krow">
            <span className="l">{isClaude ? 'week done' : 'cycle done'}</span>
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
          {isClaude ? 'Daily budgets to stay under' : 'Provider budget context'}
        </h3>
        <div className="mfc-kvgrid mfc-kvgrid-single">
          <div className="mfc-krow">
            <span className="l">{isClaude ? '@ 100% cap' : 'Budget pace'}</span>
            <span className={`v v-magenta${!isClaude && nativeBudgetPace == null ? ' m-unavailable' : ''}`} id="mfc-bud100">
              {isClaude ? fmt.usd2PerDay(fc?.budget_100_per_day_usd) : fmt.usd2PerDay(nativeBudgetPace)}
            </span>
          </div>
          <div className="mfc-krow">
            <span className="l">{isClaude ? '@ 90% cap' : 'Confidence'}</span>
            <span className={`v v-amber${!isClaude && nativeForecast?.confidence == null ? ' m-unavailable' : ''}`} id="mfc-bud90">
              {isClaude ? fmt.usd2PerDay(fc?.budget_90_per_day_usd) : nativeForecast?.confidence ?? 'Unavailable'}
            </span>
          </div>
        </div>
      </section>
    </Modal>
  );
}

export function ForecastModal() {
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  return <CanonicalForecastModal source={source} />;
}
