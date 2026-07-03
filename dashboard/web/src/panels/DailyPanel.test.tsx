import { render, screen } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { DailyPanel, formatDailyCell } from './DailyPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import { fmt } from '../lib/fmt';
import type { DailyPanelRow, Envelope } from '../types/envelope';

// #264 S4 (A4): the Daily card's compact-cost mode is `isMobile || isDesktopBento`.
// Mock both hooks with hoisted mutable flags (default false = the tablet band's
// full-precision path, matching pre-#264 behavior) so existing tests are unchanged;
// the bento test flips desktopBento true.
const mocks = vi.hoisted(() => ({ desktopBento: false, mobile: false }));
vi.mock('../hooks/useIsDesktopBento', () => ({ useIsDesktopBento: () => mocks.desktopBento }));
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mocks.mobile }));

describe('formatDailyCell (#214 M3-3 / #264 S4 A4)', () => {
  it('compact mode: $-prefixed ceil integer', () => {
    expect(formatDailyCell(527.3, true)).toBe('$528');
    expect(formatDailyCell(50.27, true)).toBe('$51');
    expect(formatDailyCell(1, true)).toBe('$1');
    expect(formatDailyCell(518.54, true)).toBe('$519');
  });
  it('non-compact: routes to full usd2 precision', () => {
    expect(formatDailyCell(527.3, false)).toBe(fmt.usd2(527.3));
    expect(formatDailyCell(518.54, false)).toBe('$518.54');
  });
  it('zero or non-positive renders the em dash', () => {
    expect(formatDailyCell(0, true)).toBe('—');
    expect(formatDailyCell(0, false)).toBe('—');
  });
});

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  mocks.desktopBento = false;
  mocks.mobile = false;
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function dailyRow(over: Partial<DailyPanelRow>): DailyPanelRow {
  return {
    date: '2026-05-13', label: '05-13', cost_usd: 1.0, is_today: false,
    intensity_bucket: 2, models: [],
    input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0,
    cache_read_tokens: 0, total_tokens: 0, cache_hit_pct: null, ...over,
  };
}

describe('DailyPanel cost-cell auto-fit hint (#208)', () => {
  // The cost cell sizes its font to the cell width via a container-query
  // formula keyed on `--c-len` (the rendered string's char count). JSDOM
  // can't evaluate the container-query font itself, but this guards the
  // wiring: every `.c` must carry `--c-len` === its text length, so a
  // 3-digit "$212.83" never clips at narrow 2-col widths.
  it('sets --c-len on every cost cell equal to the rendered string length', () => {
    const env = baseEnvelope();
    env.daily = {
      rows: [
        dailyRow({ date: '2026-05-11', cost_usd: 212.83 }), // "$212.83" → 7
        dailyRow({ date: '2026-05-12', cost_usd: 9.99 }),   // "$9.99"   → 5
        dailyRow({ date: '2026-05-13', cost_usd: 0 }),      // "—"       → 1
      ],
      quantile_thresholds: [], peak: null, total_cost_usd: 222.82,
    };
    updateSnapshot(env);
    const { container } = render(<DailyPanel />);

    const cells = [...container.querySelectorAll('#panel-daily .daily-cell .c')];
    expect(cells.length).toBe(3);
    for (const c of cells) {
      const el = c as HTMLElement;
      expect(el.style.getPropertyValue('--c-len')).toBe(String(el.textContent!.length));
    }
    // Spot-check the 3-digit value that motivated the fix.
    const threeDigit = cells.find((c) => c.textContent === fmt.usd2(212.83)) as HTMLElement;
    expect(threeDigit).toBeTruthy();
    expect(threeDigit.style.getPropertyValue('--c-len')).toBe('7');
  });
});

describe('DailyPanel compact cost on the desktop bento card (#264 S4 A4)', () => {
  it('shows the ceil-int cost inline (compact), not the full-precision form', () => {
    mocks.desktopBento = true; // isDesktopBento → compact === true
    const env = baseEnvelope();
    env.daily = {
      rows: [dailyRow({ date: '2026-07-01', cost_usd: 518.54, intensity_bucket: 3 })],
      quantile_thresholds: [], peak: null, total_cost_usd: 518.54,
    };
    updateSnapshot(env);
    const { container } = render(<DailyPanel />);
    // The heatmap cell's cost (.c) is the compact ceil form ($519), NOT the
    // full-precision $518.54 the 640–900 tablet band uses. (The footer Total
    // legitimately keeps $518.54 full precision — scope the check to the cell.)
    const cellCost = container.querySelector('#panel-daily .daily-cell .c');
    expect(cellCost?.textContent).toBe('$519');
    expect(screen.getByText('$519')).toBeInTheDocument();
  });
});
