import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { CurrentWeekModal } from '../src/modals/CurrentWeekModal';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<CurrentWeekModal />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the week-pill chip strip with formatted week window', () => {
    render(<CurrentWeekModal />);
    const pill = document.getElementById('mcw-week-pill');
    expect(pill).not.toBeNull();
    expect(pill?.classList.contains('m-pill')).toBe(true);
    expect(pill?.classList.contains('accent-green')).toBe(true);
    // Fixture: week_label "Apr 21–28", reset "2026-04-28T00:00:00Z" → "Apr 28"
    // Post-F1: literal " UTC" is gone — the offset suffix is rendered by
    // datetime formatters (e.g. fmt.datetimeShortZ on the reset cell);
    // the week-window pill is a pure date range with no clock time.
    expect(pill?.textContent).toMatch(/Apr 21–28 → Apr 28/);
  });

  it('splits the big numeral into .int + .unit', () => {
    render(<CurrentWeekModal />);
    const wrap = document.getElementById('mcw-bignum');
    expect(wrap).not.toBeNull();
    const intEl = wrap?.querySelector('.int');
    const unit = wrap?.querySelector('.unit');
    // Fixture used_pct=17.4 → int "17", unit ".4%"
    expect(intEl?.textContent).toBe('17');
    expect(unit?.textContent).toBe('.4%');
  });

  it('renders the progress-bar fill + marker + scale ticks', () => {
    render(<CurrentWeekModal />);
    expect(document.querySelector('.mcw-pbar')).not.toBeNull();
    const fill = document.getElementById('mcw-fill');
    expect(fill?.style.width).toBe('17.4%');
    const marker = document.getElementById('mcw-marker');
    expect(marker?.style.left).toBe('17.4%');
    const scale = document.querySelector('.mcw-pscale');
    expect(scale?.children.length).toBe(5);
  });

  it('renders the three mcw-mini stats (spent, $ / 1%, reset)', () => {
    render(<CurrentWeekModal />);
    const mini = document.getElementById('mcw-mini');
    expect(mini?.querySelectorAll('.s').length).toBe(3);
    expect(document.getElementById('mcw-spent')?.classList.contains('v-magenta')).toBe(true);
    expect(document.getElementById('mcw-dpp')?.classList.contains('v-cyan')).toBe(true);
    // Values
    expect(document.getElementById('mcw-spent')?.textContent).toBe('$20.95');
    expect(document.getElementById('mcw-dpp')?.textContent).toBe('$1.230');
  });

  it('renders the Milestones section header with hash icon + count pill', () => {
    render(<CurrentWeekModal />);
    const sec = document.querySelector('.m-sec.sec-ms');
    expect(sec).not.toBeNull();
    const useEl = sec?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#hash');
    const cnt = document.getElementById('mcw-ms-count');
    expect(cnt?.classList.contains('m-pill')).toBe(true);
    expect(cnt?.classList.contains('accent-purple')).toBe(true);
    const snap = fixture as unknown as Envelope;
    const expected = (snap.current_week?.milestones?.length ?? 0) + ' crossed';
    expect(cnt?.textContent).toBe(expected);
  });

  it('renders one m-histable row per milestone with pct-cell pill', () => {
    render(<CurrentWeekModal />);
    const snap = fixture as unknown as Envelope;
    const expected = snap.current_week?.milestones?.length ?? 0;
    const rows = document.querySelectorAll('#mcw-rows tr');
    expect(rows.length).toBe(expected);
    const pctCells = document.querySelectorAll('#mcw-rows .pct-cell');
    expect(pctCells.length).toBe(expected);
    pctCells.forEach((cell) => {
      expect(cell.classList.contains('m-pill')).toBe(true);
      expect(cell.classList.contains('accent-purple')).toBe(true);
    });
  });

  it('renders the empty state when no milestones', () => {
    _resetForTests();
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      current_week: {
        ...(fixture as unknown as Envelope).current_week!,
        milestones: [],
      },
    });
    render(<CurrentWeekModal />);
    const empty = document.getElementById('mcw-empty');
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toMatch(/No milestones yet/);
    expect(document.getElementById('mcw-table')).toBeNull();
  });

  it('renders a sub text with avg marginal + latest at when ≥2 milestones', () => {
    render(<CurrentWeekModal />);
    const sub = document.getElementById('mcw-ms-sub');
    expect(sub).not.toBeNull();
    expect(sub?.textContent).toMatch(/avg marginal/);
    expect(sub?.textContent).toMatch(/latest at/);
  });
});
