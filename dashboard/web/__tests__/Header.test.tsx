import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Header } from '../src/components/Header';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<Header /> (slimmed to chrome — #248)', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the cctally brand', () => {
    render(<Header />);
    expect(screen.getByText('cctally')).toBeInTheDocument();
  });

  it('no longer renders the five dashboard stat blocks (moved to HeroStrip)', () => {
    render(<Header />);
    // The Week / Used / $/1% / Forecast / vs-last-week stats left the header
    // for the at-a-glance hero.
    for (const stat of ['week', 'used', 'dollar-per-pct', 'forecast', 'vs-last-week']) {
      expect(document.querySelector(`[data-stat="${stat}"]`)).toBeNull();
    }
    // And their labels are gone from the chrome bar.
    expect(screen.queryByText('Week')).toBeNull();
    expect(screen.queryByText('Used')).toBeNull();
    expect(screen.queryByText('$/1%')).toBeNull();
    expect(screen.queryByText('Fcst')).toBeNull();
    expect(document.querySelector('.pill-warn')).toBeNull();
  });

  it('keeps the settings + help action buttons', () => {
    render(<Header />);
    expect(document.querySelector('.topbar-settings')).not.toBeNull();
    expect(document.querySelector('.topbar-help')).not.toBeNull();
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
