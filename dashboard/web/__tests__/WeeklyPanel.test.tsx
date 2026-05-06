import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WeeklyPanel } from '../src/panels/WeeklyPanel';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<WeeklyPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel card with cyan accent', () => {
    render(<WeeklyPanel />);
    const section = document.getElementById('panel-weekly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-cyan')).toBe(true);
  });

  it('renders the bar-chart icon and the 8-weeks subtitle', () => {
    render(<WeeklyPanel />);
    const useEl = document.querySelector('#panel-weekly svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#bar-chart');
    expect(screen.getByText(/8 weeks/i)).toBeInTheDocument();
  });

  it('renders one .period block per fixture row (2 rows)', () => {
    render(<WeeklyPanel />);
    const periods = document.querySelectorAll('#panel-weekly .period');
    expect(periods.length).toBe(2);
  });

  it('renders a [NOW] pill on the current row only', () => {
    render(<WeeklyPanel />);
    const pills = document.querySelectorAll('#panel-weekly .pill-current');
    expect(pills.length).toBe(1);
  });

  it('renders a stacked bar with one segment per model', () => {
    render(<WeeklyPanel />);
    const firstStack = document.querySelector('#panel-weekly .model-stack');
    // First fixture row has 3 models.
    expect(firstStack?.children.length).toBe(3);
  });

  it('renders a Δ chip with .up class on the +9% row', () => {
    render(<WeeklyPanel />);
    const deltas = document.querySelectorAll('#panel-weekly .delta');
    expect(deltas.length).toBeGreaterThanOrEqual(1);
    expect(Array.from(deltas).some((d) => d.classList.contains('up'))).toBe(true);
  });

  it('renders the foot total (sum of visible-row costs)', () => {
    render(<WeeklyPanel />);
    const foot = document.querySelector('#panel-weekly .panel-foot');
    expect(foot).not.toBeNull();
    // 48.21 + 44.10 = 92.31
    expect(foot?.textContent).toMatch(/\$92\.31/);
  });

  it('opens the weekly modal on click', async () => {
    const { container } = render(<WeeklyPanel />);
    (container.querySelector('#panel-weekly') as HTMLElement).click();
    const { getState } = await import('../src/store/store');
    expect(getState().openModal).toBe('weekly');
  });
});
