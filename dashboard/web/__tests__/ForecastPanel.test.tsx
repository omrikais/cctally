import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ForecastPanel } from '../src/panels/ForecastPanel';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<ForecastPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel', () => {
    render(<ForecastPanel />);
    expect(screen.getByText(/forecast/i)).toBeInTheDocument();
  });

  // #248 §4 — the panel is now a calm tile. The hero is the projected % at
  // reset (fixture week_avg_projection_pct=68.5 → 69%); the verdict glyph is
  // data-driven (fixture verdict="ok" → ✓ / is-good, no accent edge).
  it('shows the projected % at reset as the hero number', () => {
    render(<ForecastPanel />);
    const num = document.querySelector('#panel-forecast .fc-num');
    expect(num).not.toBeNull();
    expect(num?.textContent).toContain('69%');
  });

  it('renders the data-driven OK verdict chip (✓ / is-good, calm)', () => {
    render(<ForecastPanel />);
    const chip = document.querySelector('#panel-forecast .fc-verdict-chip');
    expect(chip).not.toBeNull();
    expect(chip?.textContent).toContain('✓');
    expect(chip?.className).toContain('is-good');
    // Calm: no escalation accent edge and none of the retired chrome.
    expect(document.querySelector('.fc-accent-edge')).toBeNull();
    expect(document.querySelector('.warn-banner')).toBeNull();
    expect(document.querySelector('#fc-banner')).toBeNull();
    expect(document.querySelector('.fc-row')).toBeNull();
    expect(document.querySelector('.fc-divider')).toBeNull();
    expect(document.querySelector('.panel-foot.fc-conf')).toBeNull();
  });

  it('renders the muted budget foot (recent-24h + per-day budgets)', () => {
    render(<ForecastPanel />);
    const foot = document.querySelector('#panel-forecast .fc-budget-foot');
    expect(foot).not.toBeNull();
    expect(foot?.textContent).toContain('72%');     // recent_24h_projection_pct
    expect(foot?.textContent).toContain('$24.50');  // budget_100_per_day_usd
    expect(foot?.textContent).toContain('$21.00');  // budget_90_per_day_usd
  });

  it('renders the crystal-ball icon in the header', () => {
    render(<ForecastPanel />);
    const header = document.querySelector('#panel-forecast .panel-header');
    const use = header?.querySelector('use');
    expect(use?.getAttribute('href')).toBe('/static/icons.svg#crystal-ball');
  });
});
