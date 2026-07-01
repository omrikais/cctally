import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { PeriodTable } from './PeriodTable';
import type { PeriodRow } from '../types/envelope';

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W26', cost_usd: 10, total_tokens: 0, input_tokens: 0,
    output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
    used_pct: 5, dollar_per_pct: 2, delta_cost_pct: null, is_current: false,
    models: [], ...over,
  };
}

const ROWS: PeriodRow[] = [
  periodRow({ label: '2026-W26' }),
  periodRow({ label: '2026-W27', cost_usd: 20, used_pct: 8, dollar_per_pct: 2.5, delta_cost_pct: 1 }),
];

describe('PeriodTable keyboard row selection (SH-3)', () => {
  it('rows are focusable and Enter/Space selects like a click', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedIndex={0} onSelect={onSelect} />,
    );
    const rows = container.querySelectorAll('tbody tr');
    expect((rows[1] as HTMLElement).tabIndex).toBe(0);
    fireEvent.keyDown(rows[1], { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith(1);
    fireEvent.keyDown(rows[0], { key: ' ' });
    expect(onSelect).toHaveBeenCalledWith(0);
  });
});
