import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { ForecastModal } from '../src/modals/ForecastModal';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope, ForecastEnvelope } from '../src/types/envelope';

describe('<ForecastModal />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the verdict + confidence chipstrip', () => {
    render(<ForecastModal />);
    const verdict = document.getElementById('mfc-verdict');
    expect(verdict).not.toBeNull();
    expect(verdict?.classList.contains('m-pill')).toBe(true);
    // Fixture verdict=ok → accent-green
    expect(verdict?.classList.contains('accent-green')).toBe(true);
    // Fixture confidence=high → accent-blue pill visible
    const conf = document.getElementById('mfc-confidence');
    expect(conf?.hidden).toBe(false);
    expect(conf?.classList.contains('accent-blue')).toBe(true);
    expect(conf?.textContent).toBe('high confidence');
  });

  it('renders the two m-kv hero cards with gauge + zap icons', () => {
    render(<ForecastModal />);
    const hero = document.querySelector('.m-hero.cols-2');
    expect(hero).not.toBeNull();
    const kvs = hero?.querySelectorAll('.m-kv');
    expect(kvs?.length).toBe(2);
    expect(document.querySelector('.m-kv.kv-wa svg use')?.getAttribute('href')).toBe('/static/icons.svg#gauge');
    expect(document.querySelector('.m-kv.kv-r24 svg use')?.getAttribute('href')).toBe('/static/icons.svg#zap');
    expect(document.getElementById('mfc-wa-pct')?.textContent).toBe('68.5%');
    expect(document.getElementById('mfc-r24-pct')?.textContent).toBe('72.0%');
  });

  it('renders the range-bar wrap with 3 zones, rangeband, and 2 bounds', () => {
    render(<ForecastModal />);
    const wrap = document.getElementById('mfc-rangewrap');
    expect(wrap).not.toBeNull();
    expect(wrap?.querySelector('.mfc-pills')).not.toBeNull();
    expect(wrap?.querySelector('svg.mfc-leaders')).not.toBeNull();
    const track = document.getElementById('mfc-rangetrack');
    expect(track).not.toBeNull();
    const zones = track?.querySelectorAll('.mfc-zone');
    expect(zones?.length).toBe(3);
    expect(zones?.[0].classList.contains('safe')).toBe(true);
    expect(zones?.[1].classList.contains('warn')).toBe(true);
    expect(zones?.[2].classList.contains('over')).toBe(true);
    expect(track?.querySelector('.mfc-rangeband')).not.toBeNull();
    expect(track?.querySelector('.mfc-bound.b90')).not.toBeNull();
    expect(track?.querySelector('.mfc-bound.b100')).not.toBeNull();
  });

  it('renders the Rates section with 4 krow entries and correct value classes', () => {
    render(<ForecastModal />);
    const sec = document.querySelector('.m-sec.sec-rates');
    expect(sec).not.toBeNull();
    const useEl = sec?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#activity');
    // 4 rows in the first kvgrid (rates)
    const grid = document.querySelector('.mfc-kvgrid:not(.mfc-kvgrid-single)');
    expect(grid?.querySelectorAll('.mfc-krow').length).toBe(4);
    expect(document.getElementById('mfc-dpp')?.classList.contains('v-cyan')).toBe(true);
    expect(document.getElementById('mfc-wkdone')?.classList.contains('v-green')).toBe(true);
    expect(document.getElementById('mfc-elapsed')?.classList.contains('v-green')).toBe(true);
  });

  it('renders the Daily budgets section with 2 krow entries and magenta/amber', () => {
    render(<ForecastModal />);
    const sec = document.querySelector('.m-sec.sec-bud');
    expect(sec).not.toBeNull();
    expect(sec?.querySelector('svg use')?.getAttribute('href')).toBe('/static/icons.svg#dollar');
    const grid = document.querySelector('.mfc-kvgrid.mfc-kvgrid-single');
    expect(grid?.querySelectorAll('.mfc-krow').length).toBe(2);
    expect(document.getElementById('mfc-bud100')?.classList.contains('v-magenta')).toBe(true);
    expect(document.getElementById('mfc-bud90')?.classList.contains('v-amber')).toBe(true);
    expect(document.getElementById('mfc-bud100')?.textContent).toBe('$24.50 / day');
    expect(document.getElementById('mfc-bud90')?.textContent).toBe('$21.00 / day');
  });

  it('renders the empty state when forecast is null', () => {
    _resetForTests();
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      forecast: null,
    });
    render(<ForecastModal />);
    const empty = document.getElementById('mfc-empty');
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toMatch(/No forecast data yet/);
  });

  it('verdict pill gets amber class on WARN / red on OVER', () => {
    _resetForTests();
    const warnFc: ForecastEnvelope = {
      verdict: 'cap',
      week_avg_projection_pct: 110,
      recent_24h_projection_pct: 120,
      budget_100_per_day_usd: 10,
      budget_90_per_day_usd: 5,
      confidence: 'low',
      confidence_score: 2,
      explain: null,
    };
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      forecast: warnFc,
    });
    render(<ForecastModal />);
    const verdict = document.getElementById('mfc-verdict');
    expect(verdict?.classList.contains('accent-amber')).toBe(true);
    const conf = document.getElementById('mfc-confidence');
    expect(conf?.classList.contains('accent-red')).toBe(true);
  });

  it('section header for Range has the bar-chart icon', () => {
    render(<ForecastModal />);
    const sec = document.querySelector('.m-sec.sec-range');
    expect(sec).not.toBeNull();
    const useEl = sec?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#bar-chart');
  });
});
