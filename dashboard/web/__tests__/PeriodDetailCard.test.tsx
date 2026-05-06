import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PeriodDetailCard } from '../src/modals/PeriodDetailCard';
import type { PeriodRow } from '../src/types/envelope';

function makeRow(overrides: Partial<PeriodRow>): PeriodRow {
  return {
    label: '04-26',
    cost_usd: 4.0,
    total_tokens: 6150,
    input_tokens: 100,
    output_tokens: 50,
    cache_creation_tokens: 1000,
    cache_read_tokens: 5000,
    used_pct: null,
    dollar_per_pct: null,
    delta_cost_pct: 12,
    is_current: false,
    models: [],
    cache_hit_pct: null,
    ...overrides,
  };
}

describe('<PeriodDetailCard variant="daily" />', () => {
  it('renders the "Today" pill on is_current row (not "Now")', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ is_current: true })}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.queryByText('Now')).toBeNull();
  });

  it('does NOT render the "Today" pill when is_current is false', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ is_current: false })}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    expect(screen.queryByText('Today')).toBeNull();
  });

  it('renders the cache-hit tile when cache_hit_pct != null', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ cache_hit_pct: 87.3 })}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    const tile = document.querySelector('.tokens-row .t.cache');
    expect(tile).not.toBeNull();
    expect(tile?.textContent).toMatch(/87\.3%/);
  });

  it('does NOT render the cache-hit tile when cache_hit_pct is null', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ cache_hit_pct: null })}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    expect(document.querySelector('.tokens-row .t.cache')).toBeNull();
  });

  it('does NOT render the weekly subscription-window block', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ week_start_at: undefined, week_end_at: undefined })}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    expect(document.querySelector('.detail-card .window')).toBeNull();
  });

  it('does NOT render the weekly Used % / $/1% stats row', () => {
    render(
      <PeriodDetailCard
        row={makeRow({})}
        variant="daily"
        accentClass="accent-indigo"
      />,
    );
    expect(document.querySelector('.detail-card .stats2')).toBeNull();
  });
});

describe('<PeriodDetailCard /> (regression: weekly variant unchanged)', () => {
  it('still renders "Now" pill on weekly is_current row', () => {
    render(
      <PeriodDetailCard
        row={makeRow({ is_current: true, used_pct: 42, dollar_per_pct: 1.5,
                       week_start_at: '2026-04-23T00:00:00Z',
                       week_end_at: '2026-04-30T00:00:00Z' })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    expect(screen.getByText('Now')).toBeInTheDocument();
  });
});
