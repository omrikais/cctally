// #264 S2 / #265 — restored WeeklyPanel tile with S1 card chrome. Renders ALL
// weeks (the bento card scrolls internally — #265 uncap so the inner scroll is
// meaningful) + a whole-window footer total, opens its OWN weekly modal
// (whole-section click AND the ⤢ ExpandButton), and its ShareIcon dispatches
// openShareModal('weekly').
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WeeklyPanel } from './WeeklyPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import * as store from '../store/store';
import { BoardModeContext } from '../lib/boardModeContext';
import { useReducedMotion } from '../hooks/useReducedMotion';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

vi.mock('../hooks/useReducedMotion');

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 50 },
  { model: 'claude-sonnet-4-5', display: 'sonnet-4-5', chip: 'sonnet', cost_usd: 4, cost_pct: 33 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 2, cost_pct: 17 },
];

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W27', cost_usd: 50, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 9, dollar_per_pct: 5.5, delta_cost_pct: 10, is_current: false,
    models, ...over,
  };
}

// 4 rows → all render (scrollable inside the bento card); total_cost_usd is the
// whole window.
const WEEKLY: PeriodRow[] = [
  periodRow({ label: '2026-W27', cost_usd: 55, delta_cost_pct: 9, is_current: true }),
  periodRow({ label: '2026-W26', cost_usd: 40, delta_cost_pct: -5 }),
  periodRow({ label: '2026-W25', cost_usd: 30, delta_cost_pct: 2 }),
  periodRow({ label: '2026-W24', cost_usd: 20, delta_cost_pct: -1 }),
];

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-07-01T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jul 1', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: WEEKLY, total_cost_usd: 145 },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

beforeEach(() => {
  _resetForTests();
  updateSnapshot(baseEnvelope());
});
afterEach(() => {
  _resetForTests();
});

describe('<WeeklyPanel /> (#264 S2)', () => {
  it('renders the cyan panel card with the bar-chart icon and model-split subtitle', () => {
    render(<WeeklyPanel />);
    const section = document.getElementById('panel-weekly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-cyan')).toBe(true);
    expect(document.querySelector('#panel-weekly svg use')?.getAttribute('href'))
      .toBe('/static/icons.svg#bar-chart');
    expect(screen.getByText(/model split/i)).toBeInTheDocument();
  });

  it('bento (default, no provider): renders ALL rows so the inner scroll is meaningful, with a NOW pill', () => {
    render(<WeeklyPanel />);
    expect(document.querySelectorAll('#panel-weekly .period').length).toBe(4);
    expect(document.querySelectorAll('#panel-weekly .pill-current').length).toBe(1);
    expect(document.querySelector('#panel-weekly .model-stack')?.children.length).toBe(3);
  });

  it('renders the whole-window footer total (all 4 weeks)', () => {
    render(<WeeklyPanel />);
    const foot = document.querySelector('#panel-weekly .panel-foot');
    expect(foot?.textContent).toMatch(/4w total/);
    expect(foot?.textContent).toMatch(/\$145\.00/);
  });

  it('clicking the section opens the weekly modal', () => {
    const { container } = render(<WeeklyPanel />);
    (container.querySelector('#panel-weekly') as HTMLElement).click();
    expect(getState().openModal).toBe('weekly');
  });

  it('the ⤢ ExpandButton opens the weekly modal', () => {
    render(<WeeklyPanel />);
    dispatch({ type: 'CLOSE_MODAL' });
    fireEvent.click(screen.getByRole('button', { name: 'Open Weekly' }));
    expect(getState().openModal).toBe('weekly');
  });

  it('the ShareIcon dispatches openShareModal("weekly")', () => {
    render(<WeeklyPanel />);
    fireEvent.click(screen.getByRole('button', { name: /Share Weekly report/i }));
    expect(getState().shareModal?.panel).toBe('weekly');
  });
});

function renderAt(mode: 'stack' | 'bento') {
  return render(
    <BoardModeContext.Provider value={mode}>
      <WeeklyPanel />
    </BoardModeContext.Provider>,
  );
}

describe('#293 S3 — stacked summary window', () => {
  beforeEach(() => {
    vi.mocked(useReducedMotion).mockReturnValue(false);
  });

  it('stack: slices to 3 newest rows, keeps the NOW pill', () => {
    renderAt('stack');
    expect(document.querySelectorAll('#panel-weekly .period').length).toBe(3);
    expect(document.querySelectorAll('#panel-weekly .pill-current').length).toBe(1);
  });

  it('stack: shows a "+N more" button spelling the full N, and the whole-window total', () => {
    renderAt('stack');
    const more = document.querySelector('#panel-weekly .period-foot-more') as HTMLButtonElement;
    expect(more).toBeTruthy();
    expect(more.textContent).toContain('+1 more');
    expect(more.getAttribute('aria-label')).toBe('Show all 4 weeks');
    expect(document.querySelector('#panel-weekly .period-foot .total')?.textContent).toContain('145');
  });

  it('bento: renders ALL rows and NO "+N more" button', () => {
    renderAt('bento');
    expect(document.querySelectorAll('#panel-weekly .period').length).toBe(4);
    expect(document.querySelector('#panel-weekly .period-foot-more')).toBeNull();
  });

  it('"+N more" opens the weekly modal EXACTLY once (click)', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    spy.mockClear();
    await userEvent.click(document.querySelector('#panel-weekly .period-foot-more')!);
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'weekly',
    );
    expect(opens).toHaveLength(1);
  });

  it('"+N more" keydown Enter opens exactly once and does not double-fire the region', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    const more = document.querySelector('#panel-weekly .period-foot-more') as HTMLButtonElement;
    more.focus();
    spy.mockClear();
    await userEvent.keyboard('{Enter}');
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'weekly',
    );
    expect(opens).toHaveLength(1);
  });

  it('keydown guard is Enter/Space-scoped: a non-activation key bubbles', () => {
    const bubbled: string[] = [];
    render(
      <div onKeyDown={(e) => bubbled.push(e.key)}>
        <BoardModeContext.Provider value="stack">
          <WeeklyPanel />
        </BoardModeContext.Provider>
      </div>,
    );
    const more = document.querySelector('#panel-weekly .period-foot-more') as HTMLButtonElement;
    fireEvent.keyDown(more, { key: 'Enter' });
    fireEvent.keyDown(more, { key: 'ArrowDown' });
    expect(bubbled).not.toContain('Enter');   // stopped
    expect(bubbled).toContain('ArrowDown');    // allowed to bubble (Shift+Arrow reorder)
  });

  it('reduced motion: bars render at target width immediately (no width:0 frame)', () => {
    vi.mocked(useReducedMotion).mockReturnValue(true);
    renderAt('stack');
    const firstBar = document.querySelector('#panel-weekly .model-stack > span') as HTMLElement;
    expect(firstBar.style.width).not.toBe('0%');
  });

  it('reduced motion: rows revealed by a stack→bento transition do not animate (§4a)', () => {
    vi.mocked(useReducedMotion).mockReturnValue(true);
    const { rerender } = renderAt('stack');   // 3 rows visible
    rerender(
      <BoardModeContext.Provider value="bento">
        <WeeklyPanel />
      </BoardModeContext.Provider>,
    );
    // All 4 rows now render; the newly-revealed rows' bars must be at target
    // width, never width:0 — reduced motion suppresses the reveal animation too.
    const bars = document.querySelectorAll('#panel-weekly .model-stack > span');
    expect(bars.length).toBeGreaterThan(0);
    bars.forEach((b) => expect((b as HTMLElement).style.width).not.toBe('0%'));
  });

  it('"+N more" keydown Space opens exactly once', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    const more = document.querySelector('#panel-weekly .period-foot-more') as HTMLButtonElement;
    more.focus();
    spy.mockClear();
    await userEvent.keyboard(' ');
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'weekly',
    );
    expect(opens).toHaveLength(1);
  });
});
