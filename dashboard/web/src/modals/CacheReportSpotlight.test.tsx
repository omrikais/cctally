// CacheReportSpotlight — #443 S1: the spotlight must not render a verdict, or
// a measurement, for a day that was never measured.
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CacheReportSpotlight } from './CacheReportSpotlight';
import type { CacheReportEnvelope } from '../types/envelope';

function makeReport(
  overrides: Partial<CacheReportEnvelope['today']> = {},
  { unobservedToday = false } = {},
): CacheReportEnvelope {
  const days = Array.from({ length: 14 }, (_, i) => ({
    date: `2026-05-${String(i + 7).padStart(2, '0')}`,
    cache_hit_percent: 67,
    input_tokens: 1_200_000,
    output_tokens: 180_000,
    cache_creation_tokens: 200_000,
    cache_read_tokens: 2_000_000,
    saved_usd: 1.2,
    wasted_usd: 0.15,
    net_usd: 1.05,
    anomaly_triggered: false,
    anomaly_reasons: [] as never[],
  }));
  if (unobservedToday) {
    // The builder inserts the synthetic today row at index 0 (newest-first).
    days[0] = {
      ...days[0], date: '2026-05-20', cache_hit_percent: 0,
      input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0,
      cache_read_tokens: 0, saved_usd: 0, wasted_usd: 0, net_usd: 0,
      ...({ observed: false, anomaly_unevaluated: ['net_negative', 'cache_drop'] } as object),
    };
  }
  return {
    window_days: 14,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: '2026-05-20',
      cache_hit_percent: 0,
      baseline_median_percent: 67,
      delta_pp: -67,
      net_usd: 0,
      saved_usd: 0,
      wasted_usd: 0,
      anomaly_triggered: false,
      anomaly_reasons: [],
      baseline_daily_row_count: 13,
      ...(unobservedToday
        ? { observed: false, anomaly_unevaluated: ['net_negative', 'cache_drop'] as never }
        : {}),
      ...overrides,
    },
    days,
    by_project: [],
    by_model: [],
    seven_day_net_usd: 5.94,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 28.4,
    fourteen_day_efficiency_ratio: 0.82,
    is_empty: false,
  };
}

const reportWithUnobservedToday = (
  over: Partial<CacheReportEnvelope['today']> = {},
) => makeReport(over, { unobservedToday: true });

describe('<CacheReportSpotlight /> unobserved today (#443 S1)', () => {
  it('does not read Healthy when today was never measured', () => {
    render(<CacheReportSpotlight cr={reportWithUnobservedToday()} />);
    expect(screen.queryByText(/Healthy/)).toBeNull();
    expect(screen.getByText(/No activity today/)).toBeInTheDocument();
  });

  it('renders unmeasured ratio and dollar fields as em dashes', () => {
    render(<CacheReportSpotlight cr={reportWithUnobservedToday()} />);
    const hit = screen.getByText('Cache hit').parentElement!;
    expect(hit.textContent).toContain('—');
    const delta = screen.getByText('Δ').parentElement!;
    expect(delta.textContent).toContain('—');
    const net = screen.getByText('Net').parentElement!;
    expect(net.textContent).toContain('—');
    const savedWasted = screen.getByText('Saved / Wasted').parentElement!;
    expect(savedWasted.textContent).toContain('—');
  });

  it('keeps the 14d median, which is known even when today is not', () => {
    render(
      <CacheReportSpotlight
        cr={reportWithUnobservedToday({ baseline_median_percent: 67 })}
      />,
    );
    expect(screen.getByText('14d median').parentElement!.textContent).toContain('67%');
  });

  it('excludes the synthetic row from the observed-day count', () => {
    // 14 rows, one of them synthetic.
    render(<CacheReportSpotlight cr={reportWithUnobservedToday()} />);
    expect(screen.getByText(/13 days observed/)).toBeInTheDocument();
  });
});

describe('<CacheReportSpotlight /> observed today keeps existing behaviour', () => {
  it('still reads Healthy and renders measured values', () => {
    render(<CacheReportSpotlight cr={makeReport({ cache_hit_percent: 68, net_usd: 1.2 })} />);
    expect(screen.getByText(/Healthy/)).toBeInTheDocument();
    expect(screen.getByText('Cache hit').parentElement!.textContent).toContain('68%');
    expect(screen.getByText(/14 days observed/)).toBeInTheDocument();
  });

  it('keeps the Building baseline pill and the measured subline when thin', () => {
    render(
      <CacheReportSpotlight
        cr={makeReport({
          baseline_daily_row_count: 2, cache_hit_percent: 3,
          net_usd: -0.18, anomaly_triggered: true,
          anomaly_reasons: ['net_negative'],
        })}
      />,
    );
    expect(screen.getByText(/Building baseline · 2\/5 days/)).toBeInTheDocument();
    expect(screen.getByText('Net').parentElement!.textContent).toContain('$0.18');
  });
});
