import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CurrentWeekPanel } from '../src/panels/CurrentWeekPanel';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope, FreshnessLabel } from '../src/types/envelope';

// Build an envelope variant with a custom freshness label, leaving every
// other field identical to the canonical fixture. Used by the freshness-chip
// tests below — see spec §3.4 (cctally OAuth `/usage` UA bypass design).
function withFreshness(label: FreshnessLabel | null): Envelope {
  const base = JSON.parse(JSON.stringify(fixture)) as Envelope;
  if (base.current_week) {
    base.current_week.freshness =
      label === null
        ? null
        : { label, captured_at: '2026-04-24T13:05:00Z', age_seconds: 120 };
  }
  return base;
}

describe('<CurrentWeekPanel />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the panel card', () => {
    render(<CurrentWeekPanel />);
    expect(screen.getByText(/current week/i)).toBeInTheDocument();
  });

  it('renders a progress bar with 30 cells and class="progress"', () => {
    render(<CurrentWeekPanel />);
    const prog = document.getElementById('cw-progress');
    expect(prog?.children.length).toBe(30);
    expect(prog?.classList.contains('progress')).toBe(true);
  });

  it('renders a percent string for used_pct', () => {
    render(<CurrentWeekPanel />);
    const pctTexts = screen.getAllByText(/%$/);
    expect(pctTexts.length).toBeGreaterThan(0);
  });

  it('renders all three cw-kv rows with correct icon refs and value colors', () => {
    render(<CurrentWeekPanel />);
    const kvs = document.querySelectorAll('.cw-kv');
    expect(kvs.length).toBe(3);
    const uses = document.querySelectorAll('#panel-current-week svg use');
    const hrefs = Array.from(uses).map((u) => u.getAttribute('href'));
    expect(hrefs).toContain('/static/icons.svg#trending-up');
    expect(hrefs).toContain('/static/icons.svg#clock');
    expect(hrefs).toContain('/static/icons.svg#dollar');
    expect(hrefs).toContain('/static/icons.svg#refresh');
    expect(document.querySelector('.cw-kv .v.cyan')).not.toBeNull();
    expect(document.querySelector('.cw-kv.kv-spent .v.magenta')).not.toBeNull();
    expect(document.querySelector('.cw-kv.kv-reset .v.amber')).not.toBeNull();
  });

  it('renders the progress-scale ticks', () => {
    render(<CurrentWeekPanel />);
    const scale = document.querySelector('.progress-scale');
    expect(scale).not.toBeNull();
    expect(scale?.children.length).toBe(3);
  });

  it('renders the cw-foot with a clock icon and Last snapshot label', () => {
    render(<CurrentWeekPanel />);
    const foot = document.querySelector('.panel-foot.cw-foot');
    expect(foot).not.toBeNull();
    expect(foot?.textContent).toMatch(/Last snapshot:/);
    const useEl = foot?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#clock');
  });

  it('applies accent-green inline color on the h3', () => {
    render(<CurrentWeekPanel />);
    const h3 = screen.getByText('Current Week');
    expect((h3 as HTMLElement).style.color).toBe('var(--accent-green)');
  });

  // ---- Freshness chip (spec §3.4 / OAuth /usage UA bypass plan, Task C5) ----

  describe('freshness chip', () => {
    it('renders no chip when freshness.label === "fresh"', () => {
      _resetForTests();
      updateSnapshot(withFreshness('fresh'));
      render(<CurrentWeekPanel />);
      // No element should carry the data-freshness attribute when fresh.
      expect(document.querySelector('[data-freshness]')).toBeNull();
    });

    it('renders no chip when freshness is null', () => {
      _resetForTests();
      updateSnapshot(withFreshness(null));
      render(<CurrentWeekPanel />);
      expect(document.querySelector('[data-freshness]')).toBeNull();
    });

    it('renders a dim "aging" chip when freshness.label === "aging"', () => {
      _resetForTests();
      updateSnapshot(withFreshness('aging'));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('[data-freshness="aging"]');
      expect(chip).not.toBeNull();
      expect(chip?.classList.contains('chip-aging')).toBe(true);
      expect(chip?.classList.contains('chip-stale')).toBe(false);
      expect(chip?.textContent).toMatch(/as of \d\d:\d\d:\d\d · 120s ago/);
    });

    it('renders an amber "stale" chip when freshness.label === "stale"', () => {
      _resetForTests();
      updateSnapshot(withFreshness('stale'));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('[data-freshness="stale"]');
      expect(chip).not.toBeNull();
      expect(chip?.classList.contains('chip-stale')).toBe(true);
      expect(chip?.classList.contains('chip-aging')).toBe(false);
      expect(chip?.textContent).toMatch(/as of \d\d:\d\d:\d\d · 120s ago/);
    });

    it('exposes captured_at via title attribute for hover detail', () => {
      _resetForTests();
      updateSnapshot(withFreshness('stale'));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('[data-freshness="stale"]');
      expect(chip?.getAttribute('title')).toBe(
        'Captured 2026-04-24T13:05:00Z',
      );
    });

    it('places the chip inside the panel-header (not the body)', () => {
      _resetForTests();
      updateSnapshot(withFreshness('aging'));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('[data-freshness="aging"]');
      expect(chip).not.toBeNull();
      // Walk up to the nearest .panel-header / .panel-body ancestor.
      const header = chip?.closest('.panel-header');
      const body = chip?.closest('.panel-body');
      expect(header).not.toBeNull();
      expect(body).toBeNull();
    });
  });

  // ---- 5h credit chip (Round-3 Item 4a / spec §5.3) -----------------

  // Build an envelope with one or more in-place 5h credits attached to
  // the current week's five_hour_block. Used by the credit-chip tests
  // below. Leaves everything else identical to the canonical fixture.
  function withCredits(
    deltas: number[],
  ): Envelope {
    const base = JSON.parse(JSON.stringify(fixture)) as Envelope;
    const cw = base.current_week;
    if (cw) {
      // Canonical fixture lacks five_hour_block; build a minimal one
      // so the credit-chip render path lights up.
      cw.five_hour_block = {
        block_start_at: '2026-04-24T10:00:00+00:00',
        five_hour_window_key: 1745493600,
        seven_day_pct_at_block_start: 14.0,
        seven_day_pct_delta_pp: 3.4,
        crossed_seven_day_reset: false,
        credits: deltas.map((delta_pp, i) => ({
          effective_reset_at_utc: `2026-04-24T1${i}:00:00+00:00`,
          prior_percent: 28.0,
          post_percent: 28.0 + delta_pp,
          delta_pp,
        })),
      };
    }
    return base;
  }

  describe('credit chip (Round-3)', () => {
    it('renders no chip when credits[] is absent or empty', () => {
      // Canonical fixture has no credits → no chip.
      render(<CurrentWeekPanel />);
      expect(document.querySelector('.credit-chip')).toBeNull();
    });

    it('renders an inline credit chip when credits[] is non-empty', () => {
      _resetForTests();
      updateSnapshot(withCredits([-20]));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('.credit-chip');
      expect(chip).not.toBeNull();
      // Chip carries the ⚡ glyph + "credited" verb + delta value.
      expect(chip?.textContent).toMatch(/⚡/);
      expect(chip?.textContent).toMatch(/credited/);
      expect(chip?.textContent).toMatch(/-20pp/);
    });

    it('exposes credit count via data-credit-count for analytics', () => {
      _resetForTests();
      updateSnapshot(withCredits([-20, -8]));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('.credit-chip');
      expect(chip?.getAttribute('data-credit-count')).toBe('2');
    });

    it('concatenates multiple credits in the chip text', () => {
      _resetForTests();
      updateSnapshot(withCredits([-20, -8]));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('.credit-chip');
      expect(chip).not.toBeNull();
      // Both deltas surface (spec §5.3 — "⚡ credited −Xpp, −Ypp").
      expect(chip?.textContent).toMatch(/-20pp/);
      expect(chip?.textContent).toMatch(/-8pp/);
    });

    it('places the chip inside the 5-hour row next to the percent reading', () => {
      _resetForTests();
      updateSnapshot(withCredits([-20]));
      render(<CurrentWeekPanel />);
      const chip = document.querySelector('.credit-chip');
      expect(chip).not.toBeNull();
      // Chip must live within the 5-hour cw-kv row so it visually
      // suffixes the percent number (spec §5.3 layout).
      const row = chip?.closest('.kv-five-hour');
      expect(row).not.toBeNull();
    });
  });
});
