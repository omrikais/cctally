import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PeriodDetailCard } from './PeriodDetailCard';
import type { PeriodRow, ModelCostRow } from '../types/envelope';

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 10, cost_pct: 66.7 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 5, cost_pct: 33.3 },
];

function periodRow(over: Partial<PeriodRow> = {}): PeriodRow {
  return {
    label: '2026-W26', cost_usd: 15, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 7, dollar_per_pct: 2.1, delta_cost_pct: 5, is_current: false,
    models, ...over,
  };
}

describe('PeriodDetailCard', () => {
  it('renders per-model cost bars via ModelCostBars (.drill-bar-row rows)', () => {
    render(<PeriodDetailCard row={periodRow()} variant="weekly" accentClass="accent-cyan" />);
    const rows = document.querySelectorAll('.drill-bar-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument();
    expect(screen.getByText('claude-haiku-4-5')).toBeInTheDocument();
    // The former thin .model-stack bar is gone (swapped for ModelCostBars).
    expect(document.querySelector('.model-stack')).toBeNull();
  });

  it('renders the weekly-only Used % / $/1% stats for the weekly variant', () => {
    render(<PeriodDetailCard row={periodRow()} variant="weekly" accentClass="accent-cyan" />);
    expect(screen.getByText('Used %')).toBeInTheDocument();
    expect(screen.getByText('$/1%')).toBeInTheDocument();
  });

  it('omits the Used % / $/1% stats for monthly', () => {
    render(<PeriodDetailCard row={periodRow({ label: '2026-04' })} variant="monthly" accentClass="accent-pink" />);
    expect(document.querySelectorAll('.drill-bar-row')).toHaveLength(2);
    expect(screen.queryByText('Used %')).toBeNull();
    expect(screen.queryByText('$/1%')).toBeNull();
  });

  it('omits the Used % / $/1% stats for daily', () => {
    render(<PeriodDetailCard row={periodRow({ label: '04-26' })} variant="daily" accentClass="accent-indigo" />);
    expect(document.querySelectorAll('.drill-bar-row')).toHaveLength(2);
    expect(screen.queryByText('Used %')).toBeNull();
  });
});
