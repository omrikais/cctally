import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DailyMiniBars } from '../src/modals/DailyMiniBars';
import type { DailyPanelRow } from '../src/types/envelope';

function makeRow(overrides: Partial<DailyPanelRow>): DailyPanelRow {
  return {
    date: '2026-04-26',
    label: '04-26',
    cost_usd: 4.0,
    is_today: false,
    intensity_bucket: 3,
    models: [],
    input_tokens: 100,
    output_tokens: 50,
    cache_creation_tokens: 1000,
    cache_read_tokens: 5000,
    total_tokens: 6150,
    cache_hit_pct: 80.6,
    ...overrides,
  };
}

const TODAY = '2026-04-26';

const FIXTURE: DailyPanelRow[] = [
  makeRow({ date: '2026-04-26', label: '04-26', cost_usd: 4.0, is_today: true }),
  makeRow({ date: '2026-04-25', label: '04-25', cost_usd: 8.0 }),
  makeRow({ date: '2026-04-24', label: '04-24', cost_usd: 0.0, cache_hit_pct: null }),
  makeRow({ date: '2026-04-23', label: '04-23', cost_usd: 2.0 }),
];

describe('<DailyMiniBars />', () => {
  it('renders one button per row', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={() => {}} />);
    const bars = document.querySelectorAll('.daily-modal-bars-grid .bar');
    expect(bars.length).toBe(FIXTURE.length);
  });

  it('renders bars in oldest-left → newest-right order', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={() => {}} />);
    const bars = Array.from(document.querySelectorAll('.daily-modal-bars-grid .bar')) as HTMLButtonElement[];
    expect(bars[0].getAttribute('data-date')).toBe('2026-04-23');
    expect(bars[bars.length - 1].getAttribute('data-date')).toBe('2026-04-26');
  });

  it('marks the selected bar with .sel and aria-pressed=true', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate="2026-04-25" onSelect={() => {}} />);
    const sel = document.querySelector('.daily-modal-bars-grid .bar.sel') as HTMLButtonElement;
    expect(sel).not.toBeNull();
    expect(sel.getAttribute('data-date')).toBe('2026-04-25');
    expect(sel.getAttribute('aria-pressed')).toBe('true');
  });

  it('marks the today bar with .today regardless of selection', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate="2026-04-23" onSelect={() => {}} />);
    const todayBar = document.querySelector(`[data-date="${TODAY}"]`) as HTMLButtonElement;
    expect(todayBar.classList.contains('today')).toBe(true);
    expect(todayBar.classList.contains('sel')).toBe(false);
  });

  it('marks zero-cost bars with .zero and disables them', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={() => {}} />);
    const zero = document.querySelector('[data-date="2026-04-24"]') as HTMLButtonElement;
    expect(zero.classList.contains('zero')).toBe(true);
    expect(zero.disabled).toBe(true);
  });

  it('clicking a non-zero bar fires onSelect with that row date', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={onSelect} />);
    const bar = document.querySelector('[data-date="2026-04-25"]') as HTMLButtonElement;
    await user.click(bar);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith('2026-04-25');
  });

  it('clicking a zero-cost bar does NOT fire onSelect', async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={onSelect} />);
    const zero = document.querySelector('[data-date="2026-04-24"]') as HTMLButtonElement;
    await user.click(zero);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('renders empty when rows is empty (no crash)', () => {
    render(<DailyMiniBars rows={[]} selectedDate={null} onSelect={() => {}} />);
    const bars = document.querySelectorAll('.daily-modal-bars-grid .bar');
    expect(bars.length).toBe(0);
  });

  it('bar height ∝ cost / max(cost)', () => {
    render(<DailyMiniBars rows={FIXTURE} selectedDate={TODAY} onSelect={() => {}} />);
    const apr26 = document.querySelector(`[data-date="${TODAY}"]`) as HTMLButtonElement;
    expect(apr26.style.height).toMatch(/^50(\.0+)?%$/);
  });
});
