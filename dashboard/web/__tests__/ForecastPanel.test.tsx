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

  it('renders 7 confidence dots inside .dots#fc-dots', () => {
    render(<ForecastPanel />);
    const host = document.getElementById('fc-dots');
    expect(host).not.toBeNull();
    expect(host?.classList.contains('dots')).toBe(true);
    expect(document.querySelectorAll('#fc-dots .d').length).toBe(7);
  });

  it('paints the verdict banner class from VERDICT_MAP (fixture verdict=ok → .good)', () => {
    render(<ForecastPanel />);
    const banner = document.getElementById('fc-banner');
    expect(banner).not.toBeNull();
    expect(banner?.classList.contains('warn-banner')).toBe(true);
    // Fixture has verdict="ok" → cls "good"
    expect(banner?.classList.contains('good')).toBe(true);
    // Verdict banner carries a warn-triangle svg
    const use = banner?.querySelector('use');
    expect(use?.getAttribute('href')).toBe('/static/icons.svg#warn-triangle');
  });

  it('renders 4 fc-row elements with their expected icon refs and labels', () => {
    render(<ForecastPanel />);
    const rows = document.querySelectorAll('.fc-row');
    expect(rows.length).toBe(4);
    // Each row has an .icon-box holding a <svg><use/></svg>
    const boxes = document.querySelectorAll('.icon-box');
    expect(boxes.length).toBe(4);
    // Icon refs: trending-up, flame, dollar, dollar
    const hrefs = Array.from(boxes).map((b) => b.querySelector('use')?.getAttribute('href'));
    expect(hrefs).toEqual([
      '/static/icons.svg#trending-up',
      '/static/icons.svg#flame',
      '/static/icons.svg#dollar',
      '/static/icons.svg#dollar',
    ]);
    // Labels
    expect(screen.getByText('Week-avg projection')).toBeInTheDocument();
    expect(screen.getByText('Recent-24h projection')).toBeInTheDocument();
    expect(screen.getByText('Budget to stay ≤100%')).toBeInTheDocument();
    expect(screen.getByText('Budget to stay ≤90%')).toBeInTheDocument();
  });

  it('renders the fc-divider between projections and budgets', () => {
    render(<ForecastPanel />);
    expect(document.querySelector('.fc-divider')).not.toBeNull();
  });

  it('renders the panel-foot fc-conf with Confidence label + value', () => {
    render(<ForecastPanel />);
    const foot = document.querySelector('.panel-foot.fc-conf');
    expect(foot).not.toBeNull();
    expect(foot?.textContent).toMatch(/Confidence:/);
    const clockUse = foot?.querySelector('svg use');
    expect(clockUse?.getAttribute('href')).toBe('/static/icons.svg#clock');
    const val = foot?.querySelector('.val');
    expect(val?.textContent).toBe('high'); // fixture confidence
  });

  it('renders the crystal-ball icon in the header', () => {
    render(<ForecastPanel />);
    const header = document.querySelector('#panel-forecast .panel-header');
    const use = header?.querySelector('use');
    expect(use?.getAttribute('href')).toBe('/static/icons.svg#crystal-ball');
  });

  it('dots on-count matches confidence_score', () => {
    render(<ForecastPanel />);
    // Fixture confidence_score=6
    const onDots = document.querySelectorAll('#fc-dots .d.on');
    expect(onDots.length).toBe(6);
  });
});
