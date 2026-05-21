// CacheReportPanel — anomaly-watchdog panel for the dashboard.
// Spec 2026-05-21 §2. State coverage: healthy, anomalous,
// insufficient-baseline, empty, click-to-open dispatch.
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { CacheReportPanel } from './CacheReportPanel';
import {
  _resetForTests,
  getState,
  updateSnapshot,
} from '../store/store';
import type {
  CacheReportEnvelope,
  Envelope,
} from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-20T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk May 20', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null,
      forecast_verdict: 'ok', vs_last_week_delta: null,
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
      tz: 'local', resolved_tz: 'Etc/UTC',
      offset_label: 'UTC', offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
    },
  };
}

function healthyCacheReport(): CacheReportEnvelope {
  const days = Array.from({ length: 14 }).map((_, i) => ({
    date: `2026-05-${String(i + 7).padStart(2, '0')}`,
    cache_hit_percent: 67 + (i % 3),
    input_tokens: 500, output_tokens: 100,
    cache_creation_tokens: 200, cache_read_tokens: 2000,
    saved_usd: 0.8, wasted_usd: 0.1, net_usd: 0.7,
    anomaly_triggered: false, anomaly_reasons: [],
  }));
  return {
    window_days: 14,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: '2026-05-20',
      cache_hit_percent: 68,
      baseline_median_percent: 67,
      delta_pp: -1,  // slightly below; not anomalous
      net_usd: 1.20, saved_usd: 1.30, wasted_usd: 0.10,
      anomaly_triggered: false, anomaly_reasons: [],
      baseline_daily_row_count: 13,
    },
    days, by_project: [], by_model: [],
    seven_day_net_usd: 5.94,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 12.34,
    fourteen_day_efficiency_ratio: 0.92,
    is_empty: false,
  };
}

function envelopeWith(cr: CacheReportEnvelope): Envelope {
  const env = baseEnvelope();
  env.cache_report = cr;
  return env;
}

function anomalousCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      cache_hit_percent: 49,
      baseline_median_percent: 67,
      delta_pp: 18,         // 18 pp below median
      net_usd: -0.42,
      saved_usd: 0.36,
      wasted_usd: 0.78,
      anomaly_triggered: true,
      anomaly_reasons: ['cache_drop', 'net_negative'],
    },
    seven_day_anomaly_count: 2,
  };
}

function insufficientBaselineCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      baseline_median_percent: null,
      delta_pp: null,
      baseline_daily_row_count: 3,
    },
    days: base.days.slice(0, 3),
  };
}

function emptyCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: { ...base.today, cache_hit_percent: 0 },
    days: [],
    by_project: [],
    by_model: [],
    seven_day_net_usd: 0,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 0,
    fourteen_day_efficiency_ratio: 0,
    is_empty: true,
  };
}

describe('<CacheReportPanel /> healthy state', () => {
  it('renders healthy state with teal accent and the check glyph', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-teal');
    expect(screen.getByText('✓')).toBeInTheDocument();
    // Cache hit text — match the 68% number specifically.
    expect(screen.getByText(/68%/)).toBeInTheDocument();
  });

  it('renders 14 sparkline points when 14 days are present', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const polyline = document.querySelector('.cr-spark polyline');
    expect(polyline).toBeTruthy();
    const pts = polyline?.getAttribute('points') ?? '';
    expect(pts.split(' ').length).toBe(14);
  });

  it('panel click dispatches OPEN_MODAL with kind cache-report', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    fireEvent.click(panel);
    expect(getState().openModal).toBe('cache-report');
  });
});

describe('<CacheReportPanel /> anomalous state', () => {
  it('renders anomalous state with amber accent and the warning glyph', () => {
    updateSnapshot(envelopeWith(anomalousCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-amber');
    expect(screen.queryByText('✓')).toBeNull();
    expect(screen.getByText('⚠')).toBeInTheDocument();
    // cache_drop wins over net_negative when both fire: headline reads
    // "Today: cache hit ↓ 18pp" (delta floored, abs).
    expect(screen.getByText(/↓ 18pp/)).toBeInTheDocument();
    // Second subline surfaces the anomaly count.
    expect(screen.getByText(/2 ⚠ days/)).toBeInTheDocument();
  });

  it('renders an amber today-marker on the sparkline when anomalous', () => {
    updateSnapshot(envelopeWith(anomalousCacheReport()));
    render(<CacheReportPanel />);
    const marker = screen.getByTestId('cr-spark-today-marker');
    // Color comes from the panel via the today_marker_color prop.
    expect(marker.getAttribute('fill')).toBe('var(--accent-amber)');
  });
});

describe('<CacheReportPanel /> insufficient-baseline state', () => {
  it('renders the ~ glyph and "Building baseline N/5 days" headline', () => {
    updateSnapshot(envelopeWith(insufficientBaselineCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    // Stays teal — insufficient baseline is not an anomaly.
    expect(panel).toHaveClass('accent-teal');
    expect(screen.getByText('~')).toBeInTheDocument();
    expect(screen.getByText(/Building baseline · 3\/5 days/i)).toBeInTheDocument();
    // Sparkline omitted in insufficient-baseline state.
    expect(document.querySelector('.cr-spark')).toBeNull();
  });
});

describe('<CacheReportPanel /> empty state', () => {
  it('renders the − glyph and "No Claude activity yet" headline when is_empty', () => {
    updateSnapshot(envelopeWith(emptyCacheReport()));
    render(<CacheReportPanel />);
    expect(screen.getByText('−')).toBeInTheDocument();
    expect(screen.getByText(/No Claude activity yet/i)).toBeInTheDocument();
    // Sparkline omitted in empty state.
    expect(document.querySelector('.cr-spark')).toBeNull();
  });
});
