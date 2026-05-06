import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MonthlyPanel } from '../src/panels/MonthlyPanel';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<MonthlyPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel card with pink accent', () => {
    render(<MonthlyPanel />);
    const section = document.getElementById('panel-monthly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-pink')).toBe(true);
  });

  it('renders the calendar icon and the 6-months subtitle', () => {
    render(<MonthlyPanel />);
    const useEl = document.querySelector('#panel-monthly svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#calendar');
    expect(screen.getByText(/6 months/i)).toBeInTheDocument();
  });

  it('renders one .period block per fixture row (2 rows)', () => {
    render(<MonthlyPanel />);
    const periods = document.querySelectorAll('#panel-monthly .period');
    expect(periods.length).toBe(2);
  });

  it('opens the monthly modal on click', async () => {
    const { container } = render(<MonthlyPanel />);
    (container.querySelector('#panel-monthly') as HTMLElement).click();
    const { getState } = await import('../src/store/store');
    expect(getState().openModal).toBe('monthly');
  });

  it('renders the foot total (361.71 = 182.50 + 179.21)', () => {
    render(<MonthlyPanel />);
    const foot = document.querySelector('#panel-monthly .panel-foot');
    expect(foot?.textContent).toMatch(/\$361\.71/);
  });
});
