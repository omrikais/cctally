import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { PeriodTable } from './PeriodTable';
import { _resetForTests } from '../store/store';
import type { PeriodRow } from '../types/envelope';

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W26', cost_usd: 10, total_tokens: 0, input_tokens: 0,
    output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
    used_pct: 5, dollar_per_pct: 2, delta_cost_pct: null, is_current: false,
    models: [], ...over,
  };
}

// Rows carry no week_start_at, so keyOf(row, 'week') falls back to label
// — the row key equals the label here.
const ROWS: PeriodRow[] = [
  periodRow({ label: '2026-W26' }),
  periodRow({ label: '2026-W27', cost_usd: 20, used_pct: 8, dollar_per_pct: 2.5, delta_cost_pct: 1 }),
];

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('PeriodTable keyboard row selection (SH-3, key-based)', () => {
  it('rows are focusable and Enter/Space selects by key like a click', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey="2026-W26" onSelect={onSelect} />,
    );
    const rows = container.querySelectorAll('tbody tr');
    expect((rows[1] as HTMLElement).tabIndex).toBe(0);
    fireEvent.keyDown(rows[1], { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith('2026-W27');
    fireEvent.keyDown(rows[0], { key: ' ' });
    expect(onSelect).toHaveBeenCalledWith('2026-W26');
  });

  it('clicking a row selects it by key', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey="2026-W26" onSelect={onSelect} />,
    );
    const rows = container.querySelectorAll('tbody tr');
    fireEvent.click(rows[1]);
    expect(onSelect).toHaveBeenCalledWith('2026-W27');
  });
});

describe('PeriodTable header (WM-1)', () => {
  it('labels the delta column "Δ cost" (not bare "Δ")', () => {
    render(<PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />);
    expect(screen.getByRole('columnheader', { name: 'Δ cost' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Δ' })).toBeNull();
  });

  it('renders the weekly-only Used % / $/1% headers; monthly omits them', () => {
    const { unmount } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />,
    );
    expect(screen.getByRole('columnheader', { name: 'Used %' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '$/1%' })).toBeInTheDocument();
    unmount();
    render(<PeriodTable rows={ROWS} variant="monthly" accentClass="accent-pink" selectedKey={null} onSelect={vi.fn()} />);
    expect(screen.queryByRole('columnheader', { name: 'Used %' })).toBeNull();
    expect(screen.queryByRole('columnheader', { name: '$/1%' })).toBeNull();
  });
});

describe('PeriodTable sortable headers', () => {
  const firstRowLabel = (container: HTMLElement) =>
    container.querySelector('tbody tr td')?.textContent ?? '';

  it('clicking "Cost (USD)" reorders rows (default envelope order → cost desc)', () => {
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />,
    );
    // Envelope order: W26 (10), W27 (20).
    expect(firstRowLabel(container)).toContain('2026-W26');
    fireEvent.click(screen.getByRole('columnheader', { name: 'Cost (USD)' }));
    // cost desc → W27 (20) leads.
    expect(firstRowLabel(container)).toContain('2026-W27');
  });
});
