// ForecastModal — FC-1 pill resolver (collapse-to-range on narrow wraps,
// true pixel-space min-gap on wide wraps) + FC-2 range-bar legibility
// (now-marker sourced from the current weekly used %, 0/110 scale bounds,
// legend). The pure `resolvePillLayout` is unit-tested directly (JSDOM-
// blind chart math); the DOM-mutating range-bar effect is exercised via a
// full modal render (#250 S4 · plan Tasks 5 & 6).
import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { ForecastModal, resolvePillLayout } from './ForecastModal';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope, ForecastEnvelope } from '../types/envelope';

const pins = () => [
  { kind: 'wa', pos: 19.8, raw: 19.8, pillWidthPx: 44 },
  { kind: 'r24', pos: 30.6, raw: 30.6, pillWidthPx: 44 },
];

describe('resolvePillLayout', () => {
  it('collapses to a range pill when both cannot fit (narrow wrap)', () => {
    const r = resolvePillLayout(pins() as never, /*wrapPx*/ 90, 8);
    expect(r.collapsed).toBe(true);
    expect(r.rangeText).toBe('19.8–30.6%');
  });
  it('keeps both with an edge gap >= minGap on a wide wrap', () => {
    const r = resolvePillLayout(pins() as never, /*wrapPx*/ 600, 8);
    expect(r.collapsed).toBe(false);
    const [a, b] = r.pins!;
    const ax = (a.resolvedXPct / 100) * 600,
      bx = (b.resolvedXPct / 100) * 600;
    const edgeGap =
      Math.abs(bx - ax) - (a.pillWidthPx / 2 + b.pillWidthPx / 2);
    expect(edgeGap).toBeGreaterThanOrEqual(8 - 0.01);
  });
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-01T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk Jun 01',
      used_pct: 0,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: {
      tz: 'local',
      resolved_tz: 'Etc/UTC',
      offset_label: 'UTC',
      offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true,
      weekly_thresholds: [],
      five_hour_thresholds: [],
      budget_thresholds: [],
    },
  };
}

function fcFixture(): ForecastEnvelope {
  return {
    verdict: 'ok',
    week_avg_projection_pct: 19.8,
    recent_24h_projection_pct: 30.6,
    budget_100_per_day_usd: 12.5,
    budget_90_per_day_usd: 10.0,
    confidence: 'high',
    confidence_score: 0.9,
    explain: {
      rates: {
        dollars_per_percent: 1.2,
        week_average_pct_per_hour: 0.11,
        recent_24h_pct_per_hour: 0.18,
      },
      week: { elapsed_hours: 40, remaining_hours: 128 },
    },
  };
}

function renderForecast(opts: {
  usedPct: number | null;
  forecast: ForecastEnvelope;
}) {
  const env = baseEnvelope();
  env.header.used_pct = opts.usedPct;
  env.forecast = opts.forecast;
  updateSnapshot(env);
  return render(<ForecastModal />);
}

describe('<ForecastModal /> range bar (FC-2)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('renders a now-marker positioned from the current weekly used %', () => {
    const { container } = renderForecast({ usedPct: 11, forecast: fcFixture() });
    const now = container.querySelector('.mfc-now') as HTMLElement | null;
    expect(now).not.toBeNull();
    // 11 / 110 * 100 = 10%
    expect(now!.style.left).toBe('10%');
  });

  it('renders a legend and 0%/110% scale bounds', () => {
    const { container } = renderForecast({ usedPct: 11, forecast: fcFixture() });
    expect(container.querySelector('.mfc-legend')).not.toBeNull();
    expect(container.textContent).toContain('0%');
    expect(container.textContent).toContain('110%');
  });

  it('omits the now-marker when the current used % is unknown', () => {
    const { container } = renderForecast({ usedPct: null, forecast: fcFixture() });
    expect(container.querySelector('.mfc-now')).toBeNull();
  });
});
