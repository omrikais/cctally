import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Header } from '../src/components/Header';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<Header />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the calendar icon + Week stat', () => {
    render(<Header />);
    expect(screen.getByText('Week')).toBeInTheDocument();
    // The calendar icon is an <svg><use href="...#calendar"/></svg>
    const uses = document.querySelectorAll('svg use');
    const hrefs = Array.from(uses).map((u) => u.getAttribute('href'));
    expect(hrefs).toContain('/static/icons.svg#calendar');
  });

  it('renders Used pct1 with hi-green class and 5h sub', () => {
    render(<Header />);
    expect(screen.getByText('Used')).toBeInTheDocument();
    // Fixture used_pct=17.4 → "17.4%"
    const used = screen.getByText('17.4%');
    expect(used.classList.contains('hi-green')).toBe(true);
    // 5h subtext — fmt.pct0 renders "42%"
    expect(screen.getByText(/\(5h/)).toBeInTheDocument();
    expect(screen.getByText('42%')).toBeInTheDocument();
  });

  it('renders $/1% usd2 with hi-cyan class', () => {
    render(<Header />);
    expect(screen.getByText('$/1%')).toBeInTheDocument();
    const dpp = screen.getByText('$1.23');
    expect(dpp.classList.contains('hi-cyan')).toBe(true);
  });

  it('renders Fcst pct0 with hi-amber class; no warn pill when verdict ok', () => {
    render(<Header />);
    expect(screen.getByText('Fcst')).toBeInTheDocument();
    // Fixture forecast_pct=68.5 → pct0 "69%"
    const fc = screen.getByText('69%');
    expect(fc.classList.contains('hi-amber')).toBe(true);
    // verdict is "ok" in the fixture so no WARN pill
    expect(document.querySelector('.pill-warn')).toBeNull();
  });

  it('renders trending-up icon + vs last week mute label', () => {
    render(<Header />);
    expect(screen.getByText('vs last week')).toBeInTheDocument();
    const uses = document.querySelectorAll('svg use');
    const hrefs = Array.from(uses).map((u) => u.getAttribute('href'));
    expect(hrefs).toContain('/static/icons.svg#trending-up');
  });

  it('renders a sync-chip span inside a topbar-sync button wrapper', () => {
    render(<Header />);
    const chip = document.getElementById('sync-chip');
    expect(chip).not.toBeNull();
    expect(chip?.tagName).toBe('SPAN');
    expect(chip?.classList.contains('sync-chip')).toBe(true);
    expect(chip?.classList.contains('mute')).toBe(true);
    const wrapper = chip?.closest('.topbar-sync') as HTMLElement | null;
    expect(wrapper).not.toBeNull();
    expect(wrapper?.tagName).toBe('BUTTON');
    expect(wrapper?.getAttribute('type')).toBe('button');
    expect(wrapper?.getAttribute('title')).toBe('Sync now (r)');
  });

  it('renders a refresh icon in the sync-chip stat container', () => {
    render(<Header />);
    const uses = document.querySelectorAll('svg use');
    const hrefs = Array.from(uses).map((u) => u.getAttribute('href'));
    expect(hrefs).toContain('/static/icons.svg#refresh');
  });
});
