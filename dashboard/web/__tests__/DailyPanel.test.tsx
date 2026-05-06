import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DailyPanel } from '../src/panels/DailyPanel';
import { updateSnapshot, _resetForTests, dispatch, getState } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<DailyPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel card with indigo accent', () => {
    render(<DailyPanel />);
    const section = document.getElementById('panel-daily');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-indigo')).toBe(true);
  });

  it('renders the grid icon and the heatmap subtitle', () => {
    render(<DailyPanel />);
    const useEl = document.querySelector('#panel-daily svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#grid');
    expect(screen.getByText(/heatmap/i)).toBeInTheDocument();
  });

  it('renders one .daily-cell per fixture row (6 rows)', () => {
    render(<DailyPanel />);
    const cells = document.querySelectorAll('#panel-daily .daily-cell');
    expect(cells.length).toBe(6);
  });

  it('assigns the correct h{intensity_bucket} class per cell', () => {
    render(<DailyPanel />);
    const cells = Array.from(
      document.querySelectorAll('#panel-daily .daily-cell'),
    ) as HTMLElement[];
    // Cells render in oldest→newest order (rows reversed). So the order is:
    // [04-21 h0, 04-22 h5, 04-23 h3, 04-24 h5, 04-25 h4, 04-26 h5]
    expect(cells[0].classList.contains('h0')).toBe(true);
    expect(cells[1].classList.contains('h5')).toBe(true);
    expect(cells[2].classList.contains('h3')).toBe(true);
  });

  it("outlines today's cell with .is-today", () => {
    render(<DailyPanel />);
    const todayCells = document.querySelectorAll('#panel-daily .daily-cell.is-today');
    expect(todayCells.length).toBe(1);
    expect(todayCells[0].textContent).toMatch(/26/);
  });

  it('renders zero-cost cell with em-dash placeholder', () => {
    render(<DailyPanel />);
    const cells = Array.from(document.querySelectorAll('#panel-daily .daily-cell'));
    const zeroCell = cells.find((c) => c.textContent?.includes('21'));
    expect(zeroCell?.textContent).toContain('—');
    expect(zeroCell?.textContent).not.toContain('$0.00');
  });

  it('renders peak in totals strip when non-null', () => {
    render(<DailyPanel />);
    const totals = document.querySelector('#panel-daily .daily-foot');
    expect(totals?.textContent).toMatch(/Peak day/);
    expect(totals?.textContent).toMatch(/\$8\.40/);
  });

  it('hides peak when daily.peak is null', () => {
    const env = fixture as unknown as Envelope;
    updateSnapshot({
      ...env,
      daily: { ...env.daily, peak: null },
    });
    render(<DailyPanel />);
    const totals = document.querySelector('#panel-daily .daily-foot');
    expect(totals?.textContent ?? '').not.toMatch(/Peak day/);
  });

  it('applies daily-collapsed class when prefs.dailyCollapsed is true', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { dailyCollapsed: true } });
    render(<DailyPanel />);
    const section = document.getElementById('panel-daily');
    expect(section?.classList.contains('daily-collapsed')).toBe(true);
  });

  it('renders the totals strip with the row count + $TOTAL', () => {
    render(<DailyPanel />);
    const totals = document.querySelector('#panel-daily .daily-foot');
    expect(totals?.textContent).toMatch(/6 days/);
    expect(totals?.textContent).toMatch(/\$31\.69/);
  });

  it('applies first-mount class with staggered animation-delay on initial render', async () => {
    render(<DailyPanel />);
    await waitFor(() => {
      const cells = document.querySelectorAll('#panel-daily .daily-cell.first-mount');
      expect(cells.length).toBe(6);
    });
    // Stagger: cells render in oldest→newest order with 30ms × index delay.
    const cells = Array.from(
      document.querySelectorAll('#panel-daily .daily-cell.first-mount'),
    ) as HTMLElement[];
    expect(cells[0].style.getPropertyValue('--daily-stagger')).toBe('0ms');
    expect(cells[1].style.getPropertyValue('--daily-stagger')).toBe('30ms');
    expect(cells[5].style.getPropertyValue('--daily-stagger')).toBe('150ms');
  });
});

describe('<DailyPanel /> click handlers (Daily modal entry points)', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('clicking a day cell dispatches OPEN_MODAL with that date', async () => {
    const user = userEvent.setup();
    render(<DailyPanel />);
    const cell = document.querySelector('[data-cell-date="2026-04-26"]') as HTMLElement;
    expect(cell).not.toBeNull();
    await user.click(cell);
    const s = getState();
    expect(s.openModal).toBe('daily');
    expect(s.openDailyDate).toBe('2026-04-26');
  });

  it('clicking the peak-day footer dispatches OPEN_MODAL with peak date', async () => {
    const user = userEvent.setup();
    render(<DailyPanel />);
    const peakBtn = document.querySelector('[data-peak-trigger]') as HTMLElement;
    expect(peakBtn).not.toBeNull();
    await user.click(peakBtn);
    const s = getState();
    expect(s.openModal).toBe('daily');
    expect(s.openDailyDate).toBe('2026-04-26');
  });

  it('the Total footer cell is NOT clickable (no button, no onClick)', () => {
    render(<DailyPanel />);
    const total = document.querySelector('[data-total-cell]');
    expect(total).not.toBeNull();
    expect(total?.tagName).not.toBe('BUTTON');
    expect((total as HTMLElement).onclick).toBeNull();
  });
});
