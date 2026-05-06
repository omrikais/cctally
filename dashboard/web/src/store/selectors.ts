import type { Envelope, ForecastEnvelope, SessionRow } from '../types/envelope';
import type { SessionSortKey } from './store';
import { fmt, type FmtCtx } from '../lib/fmt';

// Default fallback used when a snapshot has no `display` block (legacy
// envelope from an older server, or pre-first-tick before the store's
// snapshot is populated). Mirrors `useDisplayTz`'s DEFAULT_DISPLAY so
// the haystack matches what the table would render in that state.
// keep in sync with DEFAULT_DISPLAY in src/hooks/useDisplayTz.ts
const FALLBACK_FMT_CTX: FmtCtx = { tz: 'Etc/UTC', offsetLabel: 'UTC' };

// Resolve the FmtCtx from an envelope's `display` block. Used by the
// store reducer so search-haystack formatting matches what the user
// actually sees in the rendered Started column under their chosen tz.
export function ctxFromEnvelope(env: Envelope | null | undefined): FmtCtx {
  if (!env?.display) return FALLBACK_FMT_CTX;
  return { tz: env.display.resolved_tz, offsetLabel: env.display.offset_label };
}

// ----- Sessions sort -----
export function sessionComparator(key: SessionSortKey | string): (a: SessionRow, b: SessionRow) => number {
  const desc = (f: (r: SessionRow) => number) =>
    (a: SessionRow, b: SessionRow) => (f(b) ?? 0) - (f(a) ?? 0);
  const asc = (f: (r: SessionRow) => string | null | undefined) =>
    (a: SessionRow, b: SessionRow) => {
      const xa = (f(a) || '').toString();
      const xb = (f(b) || '').toString();
      return xa < xb ? -1 : xa > xb ? 1 : 0;
    };
  switch (key) {
    case 'cost desc':
      return desc((r) => r.cost_usd ?? 0);
    case 'duration desc':
      return desc((r) => r.duration_min);
    case 'model asc':
      return asc((r) => r.model);
    case 'project asc':
      return asc((r) => r.project);
    case 'started desc':
    default:
      return desc((r) => (r.started_utc ? Date.parse(r.started_utc) : 0));
  }
}

// Single case-insensitive substring match against project OR model.
// Whitespace inside the needle is a literal char (so "node runner" works),
// not a token separator. Empty needle returns rows untouched.
export function applySessionFilter(rows: SessionRow[], text: string): SessionRow[] {
  const t = (text || '').toLowerCase();
  if (!t) return rows;
  return rows.filter(
    (r) =>
      (r.project || '').toLowerCase().includes(t) ||
      (r.model || '').toLowerCase().includes(t),
  );
}

// Search haystack covers every column the user can see (Started, Dur,
// Model, Project, Cost), which is broader than the filter's
// project-OR-model domain. Callers pass the already-filtered+sorted+
// sliced row list so indices point at the rendered DOM positions.
//
// `ctx` matches the per-row Started column rendering (F4 of the
// localize-datetime-display spec): with `{ noSuffix: true }` the body is
// formatted but the offset suffix is omitted, since the column header
// owns that label. Defaults to FALLBACK_FMT_CTX so direct callers
// (vitest unit tests, ad-hoc tooling) keep working without threading a
// snapshot through.
export function formatRowHaystack(r: SessionRow, ctx: FmtCtx = FALLBACK_FMT_CTX): string {
  const started = r.started_utc
    ? fmt.startedShort(r.started_utc, ctx, { noSuffix: true })
    : '';
  const dur = r.duration_min != null ? `${r.duration_min}m` : '';
  const cost = r.cost_usd != null ? `$${r.cost_usd.toFixed(2)}` : '';
  return [started === '—' ? '' : started, dur, r.model || '', r.project || '', cost]
    .join(' ')
    .toLowerCase();
}

export function computeSearchMatches(
  rows: SessionRow[],
  searchText: string,
  ctx: FmtCtx = FALLBACK_FMT_CTX,
): number[] {
  const q = (searchText || '').toLowerCase();
  if (!q) return [];
  const out: number[] = [];
  for (let i = 0; i < rows.length; i++) {
    if (formatRowHaystack(rows[i], ctx).includes(q)) out.push(i);
  }
  return out;
}

// ----- Trend data for Recharts -----
// CLAUDE.md gotcha: trend.weeks[] is 8 rows (panel sparkline);
// trend.history[] is 12 rows (modal). Do not merge them.
export interface TrendChartDatum {
  label: string;
  used_pct: number | null;
  dollar_per_pct: number | null;
  delta: number | null;
  is_current: boolean;
  spark_height?: number;
}

export function buildTrendSparkData(env: Envelope | null): TrendChartDatum[] {
  const trend = env?.trend;
  if (!trend) return [];
  if (trend.spark_heights && trend.spark_heights.length !== trend.weeks.length) {
    console.warn(
      `cctally dashboard: spark_heights length ${trend.spark_heights.length} != weeks ${trend.weeks.length}`,
    );
  }
  return trend.weeks.map((w, i) => ({
    label: w.label,
    used_pct: w.used_pct,
    dollar_per_pct: w.dollar_per_pct,
    delta: w.delta,
    is_current: w.is_current,
    spark_height: trend.spark_heights?.[i],
  }));
}

export function buildTrendHistoryData(env: Envelope | null): TrendChartDatum[] {
  const trend = env?.trend;
  if (!trend) return [];
  return trend.history.map((w) => ({
    label: w.label,
    used_pct: w.used_pct,
    dollar_per_pct: w.dollar_per_pct,
    delta: w.delta,
    is_current: w.is_current,
  }));
}

// ----- Range-bar layout -----
// Ported in spirit from dashboard/static/modals.js#renderRangeBar /
// resolvePillPositions. The legacy DOM renderer computed pill widths at paint
// time and ran a measurement-dependent collision pass; this pure selector
// uses a simple pixel-distance threshold instead so the React component can
// decide visibility from the model without touching the DOM.
//
// Priority (highest wins when two callouts collide):
//   week-avg > recent-24h > now > cap
// The lower-priority callout is marked visible:false.

export type RangeBarZoneKind = 'now' | 'week-avg' | 'recent-24h' | 'cap';

export interface RangeBarZone {
  kind: RangeBarZoneKind;
  x: number;
  w: number;
  label: string;
}

export interface RangeBarCallout {
  kind: RangeBarZoneKind;
  x: number; // center-x in svg coords
  label: string;
  visible: boolean;
}

export interface RangeBarLayout {
  zones: RangeBarZone[];
  callouts: RangeBarCallout[];
}

export const OVERLAP_PX_THRESHOLD = 48;

export function buildRangeBarLayout(fc: ForecastEnvelope | null, width: number): RangeBarLayout {
  if (fc == null || !Number.isFinite(width) || width <= 0) return { zones: [], callouts: [] };

  const wa = fc.week_avg_projection_pct;
  const r24 = fc.recent_24h_projection_pct;

  if (wa == null && r24 == null) return { zones: [], callouts: [] };

  // Scale 0..max(projection, 100) onto the svg width.
  const maxPct = Math.max(100, wa ?? 0, r24 ?? 0);
  const xFor = (pct: number) =>
    Math.max(0, Math.min(width, (pct / maxPct) * width));

  const zones: RangeBarZone[] = [
    { kind: 'cap', x: xFor(0), w: xFor(100) - xFor(0), label: '0–100%' },
  ];

  const callouts: RangeBarCallout[] = [];
  if (wa != null) {
    callouts.push({
      kind: 'week-avg',
      x: xFor(wa),
      label: `wk avg ${wa.toFixed(0)}%`,
      visible: true,
    });
  }
  if (r24 != null) {
    callouts.push({
      kind: 'recent-24h',
      x: xFor(r24),
      label: `24h ${r24.toFixed(0)}%`,
      visible: true,
    });
  }

  // Overlap pass: sort by x; if two callouts are within OVERLAP_PX_THRESHOLD,
  // hide the lower-priority one. week-avg beats recent-24h because the
  // week-average drives the primary verdict.
  const priority: Record<RangeBarZoneKind, number> = {
    'week-avg': 0,
    'recent-24h': 1,
    now: 2,
    cap: 3,
  };
  const sorted = [...callouts].sort((a, b) => a.x - b.x);
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i];
    const b = sorted[i + 1];
    if (!a.visible || !b.visible) continue;
    if (Math.abs(b.x - a.x) < OVERLAP_PX_THRESHOLD) {
      // Hide the lower-priority one (larger priority number = lower).
      if (priority[a.kind] <= priority[b.kind]) {
        b.visible = false;
      } else {
        a.visible = false;
      }
    }
  }

  return { zones, callouts };
}
