import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TrendPanel } from '../src/panels/TrendPanel';
import { updateSnapshot, _resetForTests, getState } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<TrendPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the 8-week heading', () => {
    render(<TrendPanel />);
    expect(screen.getByText(/8 weeks/i)).toBeInTheDocument();
  });

  it('renders a bar-chart icon in the panel header', () => {
    render(<TrendPanel />);
    const header = document.querySelector('#panel-trend .panel-header');
    const use = header?.querySelector('use');
    expect(use?.getAttribute('href')).toBe('/static/icons.svg#bar-chart');
  });

  it('renders a table row for each week in env.trend.weeks with .current on is_current', () => {
    render(<TrendPanel />);
    const snap = fixture as unknown as Envelope;
    const expected = snap.trend?.weeks?.length ?? 0;
    const rows = document.querySelectorAll('#trend-rows tr');
    expect(rows.length).toBe(expected);
    // The fixture's W07 is flagged is_current=true
    const current = document.querySelectorAll('#trend-rows tr.current');
    expect(current.length).toBe(1);
    // Non-current $/1% cells get the `.dollar` modifier; the current one doesn't
    const dollarCells = document.querySelectorAll('#trend-rows td.num.dollar');
    expect(dollarCells.length).toBe(expected - 1);
  });

  it('renders spark bars as plain divs with inline height percent', () => {
    render(<TrendPanel />);
    const bars = document.querySelectorAll('.trend-spark .bar');
    const snap = fixture as unknown as Envelope;
    const expected = snap.trend?.weeks?.length ?? 0;
    expect(bars.length).toBe(expected);
    // Every bar has a non-empty height inline
    bars.forEach((b) => {
      const style = (b as HTMLElement).style;
      expect(style.height).not.toBe('');
    });
    // The final (newest) bar carries the color-mix purple-tinted background
    const last = bars[bars.length - 1] as HTMLElement;
    expect(last.style.background).toContain('color-mix');
    expect(last.style.background).toContain('var(--accent-purple)');
  });

  it('renders the trend-spark-title, trend-spark container and older→newer legend', () => {
    render(<TrendPanel />);
    expect(document.querySelector('.trend-spark-title')).not.toBeNull();
    expect(document.getElementById('trend-spark')).not.toBeNull();
    const legend = document.querySelector('.trend-spark-legend');
    expect(legend).not.toBeNull();
    expect(legend?.textContent).toMatch(/older/);
    expect(legend?.textContent).toMatch(/newer/);
  });

  it('applies delta-pos / delta-neg classes on the delta column', () => {
    render(<TrendPanel />);
    // Fixture has negative deltas on W01-W03, positive on W05-W07 (W07 is current so no class)
    const posCells = document.querySelectorAll('#trend-rows td.num.delta-pos');
    const negCells = document.querySelectorAll('#trend-rows td.num.delta-neg');
    expect(posCells.length).toBeGreaterThan(0);
    expect(negCells.length).toBeGreaterThan(0);
  });

  it('renders a <SortableHeader> with four sortable <th> elements', () => {
    render(<TrendPanel />);
    const ths = document.querySelectorAll(
      '#panel-trend table.trend-table thead th.th-sortable',
    );
    expect(ths.length).toBe(4);
  });

  it('clicking $/1% header reorders rows by dollar_per_pct desc', async () => {
    const user = userEvent.setup();
    render(<TrendPanel />);
    const dpTh = document.querySelector(
      '#panel-trend table.trend-table thead th[data-col="dollar_per_pct"]',
    ) as HTMLElement;
    await user.click(dpTh);
    expect(getState().prefs.trendSortOverride).toEqual({
      column: 'dollar_per_pct', direction: 'desc',
    });
    // Verify the table body is reordered: first <td> in each row is Week label,
    // and the order of $/1% values should be descending.
    const numCells = document.querySelectorAll(
      '#panel-trend table.trend-table tbody tr td.dollar',
    );
    const dollarVals = Array.from(numCells)
      .map((c) => parseFloat((c.textContent ?? '').replace(/[^0-9.]/g, '')))
      .filter((n) => !Number.isNaN(n));
    for (let i = 1; i < dollarVals.length; i++) {
      expect(dollarVals[i - 1]).toBeGreaterThanOrEqual(dollarVals[i]);
    }
  });

  it('header click does not open the trend modal', async () => {
    const user = userEvent.setup();
    render(<TrendPanel />);
    const weekTh = document.querySelector(
      '#panel-trend table.trend-table thead th[data-col="week"]',
    ) as HTMLElement;
    await user.click(weekTh);
    expect(getState().openModal).toBeNull();
  });

  it('current-week row keeps its .current class after sort', async () => {
    const user = userEvent.setup();
    render(<TrendPanel />);
    const usedTh = document.querySelector(
      '#panel-trend table.trend-table thead th[data-col="used_pct"]',
    ) as HTMLElement;
    await user.click(usedTh);
    // is_current row class must still be applied somewhere in the tbody.
    const currentRows = document.querySelectorAll(
      '#panel-trend table.trend-table tbody tr.current',
    );
    // Fixture has exactly one current week.
    expect(currentRows.length).toBe(1);
  });

  it('sparkline data stays chronological regardless of override', async () => {
    const user = userEvent.setup();
    render(<TrendPanel />);
    // Sparkline renders <div class="bar"> children (no <svg>); capture each bar's
    // inline style.height as the stable per-bar property reflecting the data
    // input. Ordering of `.bar` children across .trend-spark is the rendered
    // chronological sequence — must not change after a header click.
    const before = Array.from(
      document.querySelectorAll('#panel-trend .trend-spark .bar'),
    ).map((el) => (el as HTMLElement).style.height);
    const dpTh = document.querySelector(
      '#panel-trend table.trend-table thead th[data-col="dollar_per_pct"]',
    ) as HTMLElement;
    await user.click(dpTh);
    const after = Array.from(
      document.querySelectorAll('#panel-trend .trend-spark .bar'),
    ).map((el) => (el as HTMLElement).style.height);
    expect(after).toEqual(before);
  });
});
