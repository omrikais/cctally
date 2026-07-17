import { beforeEach, describe, expect, it } from 'vitest';
import { render, act } from '@testing-library/react';
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

describe('<HeroStrip /> (#264 S1 — 3 zones)', () => {
  it('usage zone: Used % is the big number (header.used_pct → 11.0%)', () => {
    const { container } = render(<HeroStrip />);
    const usage = container.querySelector('.hero-usage') as HTMLElement;
    expect(usage).not.toBeNull();
    expect(usage.textContent).toContain('WEEK USAGE');
    expect(usage.textContent).toContain('wk Jun 30');
    expect(usage.textContent).toContain('11.0%');
  });

  it('usage zone: 5-HOUR renders header.five_hour_pct (→ 8%)', () => {
    const { container } = render(<HeroStrip />);
    const usage = container.querySelector('.hero-usage') as HTMLElement;
    expect(usage.textContent).toContain('5-HOUR');
    expect(usage.textContent).toContain('8%');
  });

  it('usage zone: reset countdown from current_week.reset_in_sec (→ 2d 7h)', () => {
    const { container } = render(<HeroStrip />);
    const usage = container.querySelector('.hero-usage') as HTMLElement;
    expect(usage.textContent).toContain('2d 7h');
  });

  it('spent zone: whole-dollar hero from current_week.spent_usd (→ $14) + $/1% sub', () => {
    const { container } = render(<HeroStrip />);
    const spent = container.querySelector('.hero-spent') as HTMLElement;
    expect(spent).not.toBeNull();
    expect(spent.textContent).toContain('SPENT THIS WEEK');
    // usd0(14.2) → "$14" (whole dollars; NOT "$14.20").
    expect((container.querySelector('.hs-big') as HTMLElement).textContent).toBe('$14');
    // $/1% sub keeps 2dp.
    expect(spent.textContent).toContain('$23.40');
    expect(spent.textContent).toContain('/ 1% used');
  });

  it('support zone: Forecast @ reset tinted by verdict (ok → is-good)', () => {
    const { container } = render(<HeroStrip />);
    const support = container.querySelector('.hero-support') as HTMLElement;
    expect(support.textContent).toContain('Forecast @ reset');
    const val = support.querySelector('.sup-v.is-good');
    expect(val).not.toBeNull();
    expect(val?.textContent).toContain('31%');
  });

  it('support zone: renders the vs-last-week and Snapshot rows', () => {
    const { container } = render(<HeroStrip />);
    const support = container.querySelector('.hero-support') as HTMLElement;
    expect(support.textContent).toContain('$/1% vs last week');
    expect(support.textContent).toContain('Snapshot');
  });

  it('renders a freshness chip from current_week.freshness', () => {
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness="fresh"]');
    expect(chip).not.toBeNull();
    expect(chip?.className).toContain('chip-fresh');
  });

  // FRESH-1 non-vacuous guard: the server marks an 8-minute snapshot "stale"
  // (OAuth-tuned 30s/90s), but the hero re-derives its tint client-side and
  // must read it as CALM (fresh), never amber-⚠. Pin the exact attribute the
  // buggy (trust-server-label) vs fixed (client-derive) paths differ on.
  it('de-alarms an 8-minute snapshot to fresh even when the server says stale', () => {
    const env = heroEnvelope();
    env.current_week!.freshness = {
      label: 'stale', captured_at: '2026-06-30T09:52:00Z', age_seconds: 8 * 60,
    };
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('.sup-fresh') as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.getAttribute('data-freshness')).toBe('fresh');
    expect(chip.className).toContain('chip-fresh');
    expect(chip.className).not.toContain('chip-stale');
    expect(chip.textContent).not.toContain('⚠');
    // A stale-labeled query must find nothing — the tint is de-escalated.
    expect(container.querySelector('[data-freshness="stale"]')).toBeNull();
  });

  it('prefixes ⚠ on a genuinely stale (>60m) snapshot (HERO-4 escalation)', () => {
    const env = heroEnvelope();
    env.current_week!.freshness = {
      label: 'stale', captured_at: '2026-06-30T08:00:00Z', age_seconds: 3600 + 1,
    };
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness="stale"]');
    expect(chip).not.toBeNull();
    expect(chip?.className).toContain('chip-stale');
    expect(chip?.textContent).toContain('⚠');
  });

  it('humanizes the freshness age on the Snapshot row (#259)', () => {
    const env = heroEnvelope();
    // ~27h old — the reported case that previously rendered raw "97928s ago".
    env.current_week!.freshness = {
      label: 'stale', captured_at: '2026-06-29T06:47:52Z', age_seconds: 97928,
    };
    _resetForTests();
    updateSnapshot(env);
    const { container } = render(<HeroStrip />);
    const chip = container.querySelector('[data-freshness="stale"]') as HTMLElement;
    expect(chip.textContent).toContain('1d 3h ago');
    expect(chip.textContent).not.toContain('97928s ago');
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

  // #293 S4 — the hero keeps its own activation but adopts the region guards so
  // a bubbled activation from a nested descendant can NOT double-fire the modal.
  // Non-vacuous: without the `e.target !== e.currentTarget` keydown guard, an
  // Enter that bubbles from a child span would ALSO open current-week.
  it('does NOT open on an Enter that bubbles from a nested descendant', () => {
    const { container } = render(<HeroStrip />);
    const child = container.querySelector('.hs-big') as HTMLElement;
    expect(child).not.toBeNull();
    act(() => {
      child.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(getState().openModal).toBeNull();
  });
});

// The "vs last week" $/1% delta (#207 B1) — icon-only direction + color + aria,
// never a duplicated text arrow. Full direction coverage lives here; the row
// keeps its `data-metric="vs-last-week"` hook across the #264 3-zone rebuild.
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
