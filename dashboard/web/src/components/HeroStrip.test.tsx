import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, updateSnapshot, getState } from '../store/store';
import type { Envelope } from '../types/envelope';

// Minimal-but-valid envelope with the header + current_week fields the hero
// reads. Mirrors the sibling-test mock shape (cardChrome.test.tsx).
function heroEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30',
      used_pct: 11,
      five_hour_pct: 8,
      dollar_per_pct: 23.4,
      forecast_pct: 31,
      forecast_verdict: 'ok',
      vs_last_week_delta: -0.12,
    },
    current_week: {
      used_pct: 11,
      five_hour_pct: 8,
      five_hour_resets_in_sec: null,
      spent_usd: 14.2,
      dollar_per_pct: 23.4,
      reset_at_utc: '2026-07-03T00:00:00Z',
      reset_in_sec: 200000,
      last_snapshot_age_sec: 30,
      milestones: [],
      freshness: { label: 'fresh', captured_at: '2026-06-30T09:59:30Z', age_seconds: 30 },
      five_hour_block: null,
    },
    forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

beforeEach(() => {
  _resetForTests();
  updateSnapshot(heroEnvelope());
});

describe('<HeroStrip /> (#248)', () => {
  it('renders the Used % as the hero number (header.used_pct → 11.0%)', () => {
    render(<HeroStrip />);
    expect(screen.getByText('11.0%')).toBeInTheDocument();
  });

  it('renders the week eyebrow with the week label', () => {
    const { container } = render(<HeroStrip />);
    expect(container.textContent).toContain('WEEK USAGE');
    expect(container.textContent).toContain('wk Jun 30');
  });

  it('renders the resets/spent sub-line from current_week', () => {
    const { container } = render(<HeroStrip />);
    // reset_in_sec 200000 → ddhh "2d 7h"; spent_usd 14.2 → "$14.20".
    expect(container.textContent).toContain('2d 7h');
    expect(container.textContent).toContain('$14.20');
  });

  it('renders the four spelled-out metric labels (H3)', () => {
    render(<HeroStrip />);
    expect(screen.getByText('cost / 1%')).toBeInTheDocument();
    expect(screen.getByText('Forecast')).toBeInTheDocument();
    expect(screen.getByText('5-hour')).toBeInTheDocument();
    expect(screen.getByText('vs last week')).toBeInTheDocument();
  });

  it('renders the metric values (cost/1% and forecast)', () => {
    render(<HeroStrip />);
    expect(screen.getByText('$23.40')).toBeInTheDocument();
    expect(screen.getByText('31%')).toBeInTheDocument();
  });

  it('renders a freshness chip from current_week.freshness', () => {
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness="fresh"]');
    expect(chip).not.toBeNull();
    expect(chip?.className).toContain('chip-fresh');
  });

  it('prefixes the ⚠ glyph on a STALE freshness chip (ported C5 coverage)', () => {
    const env = heroEnvelope();
    env.current_week!.freshness = {
      label: 'stale', captured_at: '2026-06-30T09:00:00Z', age_seconds: 3600,
    };
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness="stale"]');
    expect(chip).not.toBeNull();
    expect(chip?.className).toContain('chip-stale');
    expect(chip?.textContent).toContain('⚠');
  });

  it('localizes the freshness tooltip (SH-1)', () => {
    const env = heroEnvelope();
    env.current_week!.freshness = {
      label: 'aging', captured_at: '2026-06-29T17:12:25Z', age_seconds: 120,
    };
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness]') as HTMLElement;
    expect(chip.title).toContain('Captured Jun 29');
    expect(chip.title).not.toContain('Z');
    expect(chip.title).not.toMatch(/T\d\d:\d\d:\d\dZ/);
  });

  it('tints the Forecast metric value by verdict (ok → is-good)', () => {
    const { container } = render(<HeroStrip />);
    const val = container.querySelector('[data-metric="forecast"] .hero-metric-val');
    expect(val?.className).toContain('is-good');
  });

  it('opens the Current Week modal on click', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip') as HTMLElement;
    act(() => { hero.click(); });
    expect(getState().openModal).toBe('current-week');
  });

  it('opens the Current Week modal on Enter / Space', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip') as HTMLElement;
    hero.focus();
    act(() => {
      hero.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(getState().openModal).toBe('current-week');
  });
});

// The "vs last week" $/1% delta (#207 B1) moved out of Header into the hero
// metric grid verbatim (icon-only direction + color + aria; never a duplicated
// text arrow). Full direction coverage lives here now.
describe('<HeroStrip /> vs last week metric (#207 B1)', () => {
  function metricFor(d: number | null): HTMLElement | null {
    const env = heroEnvelope();
    env.header.vs_last_week_delta = d;
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    return container.querySelector('[data-metric="vs-last-week"]') as HTMLElement | null;
  }

  it('renders an em-dash value (no icon) when the delta is null', () => {
    const cell = metricFor(null)!;
    expect(cell).not.toBeNull();
    expect(cell.querySelector('use')).toBeNull();
    expect(cell.textContent).toContain('—');
  });

  it('cheaper (negative) → green + trending-down', () => {
    const cell = metricFor(-0.12)!;
    expect(cell.querySelector('use')?.getAttribute('href')).toContain('#trending-down');
    expect(cell.querySelector('svg')?.getAttribute('style')).toContain('--accent-green');
    expect(cell.getAttribute('aria-label')?.toLowerCase()).toContain('down');
    expect(cell.textContent).toContain('$0.12');
  });

  it('costlier (positive) → red + trending-up', () => {
    const cell = metricFor(0.34)!;
    expect(cell.querySelector('use')?.getAttribute('href')).toContain('#trending-up');
    expect(cell.querySelector('svg')?.getAttribute('style')).toContain('--accent-red');
    expect(cell.getAttribute('aria-label')?.toLowerCase()).toContain('up');
  });

  it('flat (|Δ| < 0.02) → dim + minus', () => {
    const cell = metricFor(0.01)!;
    expect(cell.querySelector('use')?.getAttribute('href')).toContain('#minus');
    expect(cell.querySelector('svg')?.getAttribute('style')).toContain('--text-dim');
    expect(cell.getAttribute('aria-label')?.toLowerCase()).toContain('flat');
  });
});
