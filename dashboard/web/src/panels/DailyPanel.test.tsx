import { render, screen } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import { DailyPanel, formatDailyCell } from './DailyPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
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

// #556 S2 Task 16 — Daily states its composition and renders a withheld
// outcome rather than "No usage history yet".
describe('#556 S2 — All composition and the withheld state', () => {
  function allEnvelope(outcome?: Record<string, unknown>): Envelope {
    const env = structuredClone(fixture) as unknown as Envelope;
    if (outcome) env.sources!.all.data!.aggregates!.daily = outcome as never;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    return env;
  }

  it('states that the heatmap sums both providers', () => {
    allEnvelope();
    const { container } = render(<DailyPanel />);
    // On the sub-line, not in the h2: see the '#556 S2 QA' describe below.
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toContain('both providers');
  });

  it('renders a withheld outcome instead of an empty-history message', () => {
    // The empty array was the ONLY way this adapter could report a failure, so
    // a range problem read as "No usage history yet" — honest emptiness over a
    // real fault.
    allEnvelope({ state: 'withheld', code: 'claude_fold_failed', provider: 'claude' });
    const { container } = render(<DailyPanel />);
    expect(container.querySelector('.panel-withheld')!.textContent)
      .toContain("Claude's totals");
    expect(container.textContent).not.toContain('No usage history yet');
    expect(container.querySelector('.daily-cal-grid')).toBeNull();
  });
});


// #556 S2 QA P1-1 — the same cold-load defect on Daily. `presentationDailyRows`
// synthesizes `rows_absent` for a null envelope, and the withheld branch was
// tested BEFORE the hydrating one, so the first paint of a persisted All
// selection told the user the server was wrong.
describe('#556 S2 — a cold load of the All tab', () => {
  it('shows the hydrating skeleton, not the wrong-server copy', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<DailyPanel />);

    expect(container.querySelector('.panel-skeleton')).not.toBeNull();
    expect(container.querySelector('.panel-withheld')).toBeNull();
    expect(container.textContent).not.toContain('does not publish');
    expect(container.textContent).not.toContain('Reload to pick up');
  });
});

// #556 S2 QA P2-6 / P3 — the header must not assert a range the body says is
// unresolved, and its sub-span takes the same parenthesised form as its three
// siblings.
describe('#556 S2 — the Daily title', () => {
  function allEnv(outcome?: Record<string, unknown>): void {
    const env = structuredClone(fixture) as unknown as Envelope;
    if (outcome) env.sources!.all.data!.aggregates!.daily = outcome as never;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  }

  it('degrades to "withheld" instead of still claiming 30 days', () => {
    allEnv({ state: 'withheld', code: 'range_unresolved' });
    const { container } = render(<DailyPanel />);
    const title = container.querySelector('.panel-header h2')!.textContent!
      + ' ' + container.querySelector('.panel-range-note')!.textContent!;
    expect(title).toContain('withheld');
    expect(title).not.toContain('30 days');
    expect(title).not.toContain('both providers');
  });

  it('states the window on the sub-line, like Projects, Weekly, Monthly and Blocks', () => {
    allEnv();
    const { container } = render(<DailyPanel />);
    expect(container.querySelector('.panel-header h2')!.textContent)
      .toContain('heatmap');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toBe('30 days · both providers');
  });
});


// #556 S2 QA — Daily's composition was not truncated at 390px; it was ABSENT.
// `#panel-daily .panel-header h2 .sub { display: none }` (#264 S4) hides the
// whole sub-span on phones, so the h2 measured 100% visible reading only
// "Daily" and the words "30 days · both providers" rendered nowhere. Moving
// them onto `.panel-range-note` states the composition on every viewport; the
// #264 rule keeps hiding the redundant descriptor word, which is all it ever
// claimed to be about.
describe('#556 S2 QA — the Daily composition survives the mobile sub-span rule', () => {
  it('puts the window and the composition outside the hidden .sub', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<DailyPanel />);

    expect(container.querySelector('.panel-header h2 .sub')!.textContent)
      .toBe('heatmap');
    const note = container.querySelector('.panel-range-note')!;
    expect(note.textContent).toBe('30 days · both providers');
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
    expect(note.parentElement!.querySelector(':scope > .panel-header')).not.toBeNull();
  });

  it('says only the window on a single-provider tab', () => {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<DailyPanel />);
    expect(container.querySelector('.panel-range-note')!.textContent).toBe('30 days');
  });
});
