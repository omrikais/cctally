// #264 S2 / #265 — restored MonthlyPanel tile with S1 card chrome. Renders ALL
// months (the bento card scrolls internally — #265 uncap so the inner scroll is
// meaningful) + a whole-window footer total, opens its OWN monthly modal
// (whole-section click AND the ⤢ ExpandButton), and its ShareIcon dispatches
// openShareModal('monthly').
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MonthlyPanel } from './MonthlyPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import * as store from '../store/store';
import { BoardModeContext } from '../lib/boardModeContext';
import { useReducedMotion } from '../hooks/useReducedMotion';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';

vi.mock('../hooks/useReducedMotion');

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 50 },
  { model: 'claude-sonnet-4-5', display: 'sonnet-4-5', chip: 'sonnet', cost_usd: 4, cost_pct: 33 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 2, cost_pct: 17 },
];

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-07', cost_usd: 120, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: null, dollar_per_pct: null, delta_cost_pct: 20, is_current: false,
    models, ...over,
  };
}

// 4 rows → all render (scrollable inside the bento card); total_cost_usd is the
// whole window.
const MONTHLY: PeriodRow[] = [
  periodRow({ label: '2026-07', cost_usd: 120, delta_cost_pct: 20, is_current: true }),
  periodRow({ label: '2026-06', cost_usd: 200, delta_cost_pct: -10 }),
  periodRow({ label: '2026-05', cost_usd: 150, delta_cost_pct: 5 }),
  periodRow({ label: '2026-04', cost_usd: 90, delta_cost_pct: -3 }),
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
    weekly: { rows: [] },
    monthly: { rows: MONTHLY, total_cost_usd: 560 },
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

describe('<MonthlyPanel /> (#264 S2)', () => {
  it('renders the pink panel card with the calendar icon and model-split subtitle', () => {
    render(<MonthlyPanel />);
    const section = document.getElementById('panel-monthly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-pink')).toBe(true);
    expect(document.querySelector('#panel-monthly svg use')?.getAttribute('href'))
      .toBe('/static/icons.svg#calendar');
    expect(screen.getByText(/model split/i)).toBeInTheDocument();
  });

  it('bento (default, no provider): renders ALL rows so the inner scroll is meaningful, with a NOW pill', () => {
    render(<MonthlyPanel />);
    expect(document.querySelectorAll('#panel-monthly .period').length).toBe(4);
    expect(document.querySelectorAll('#panel-monthly .pill-current').length).toBe(1);
    expect(document.querySelector('#panel-monthly .model-stack')?.children.length).toBe(3);
  });

  it('renders the whole-window footer total (all 4 months)', () => {
    render(<MonthlyPanel />);
    const foot = document.querySelector('#panel-monthly .panel-foot');
    expect(foot?.textContent).toMatch(/4mo total/);
    expect(foot?.textContent).toMatch(/\$560\.00/);
  });

  it('clicking the section opens the monthly modal', () => {
    const { container } = render(<MonthlyPanel />);
    (container.querySelector('#panel-monthly') as HTMLElement).click();
    expect(getState().openModal).toBe('monthly');
  });

  it('the ⤢ ExpandButton opens the monthly modal', () => {
    render(<MonthlyPanel />);
    dispatch({ type: 'CLOSE_MODAL' });
    fireEvent.click(screen.getByRole('button', { name: 'Open Monthly' }));
    expect(getState().openModal).toBe('monthly');
  });

  it('the ShareIcon dispatches openShareModal("monthly")', () => {
    render(<MonthlyPanel />);
    fireEvent.click(screen.getByRole('button', { name: /Share Monthly report/i }));
    expect(getState().shareModal?.panel).toBe('monthly');
  });
});

function renderAt(mode: 'stack' | 'bento') {
  return render(
    <BoardModeContext.Provider value={mode}>
      <MonthlyPanel />
    </BoardModeContext.Provider>,
  );
}

describe('#293 S3 — stacked summary window', () => {
  beforeEach(() => {
    vi.mocked(useReducedMotion).mockReturnValue(false);
  });

  it('stack: slices to 3 newest rows, keeps the NOW pill', () => {
    renderAt('stack');
    expect(document.querySelectorAll('#panel-monthly .period').length).toBe(3);
    expect(document.querySelectorAll('#panel-monthly .pill-current').length).toBe(1);
  });

  it('stack: shows a "+N more" button spelling the full N, and the whole-window total', () => {
    renderAt('stack');
    const more = document.querySelector('#panel-monthly .period-foot-more') as HTMLButtonElement;
    expect(more).toBeTruthy();
    expect(more.textContent).toContain('+1 more');
    expect(more.getAttribute('aria-label')).toBe('Show all 4 months');
    expect(document.querySelector('#panel-monthly .period-foot .total')?.textContent).toContain('560');
  });

  it('bento: renders ALL rows and NO "+N more" button', () => {
    renderAt('bento');
    expect(document.querySelectorAll('#panel-monthly .period').length).toBe(4);
    expect(document.querySelector('#panel-monthly .period-foot-more')).toBeNull();
  });

  it('"+N more" opens the monthly modal EXACTLY once (click)', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    spy.mockClear();
    await userEvent.click(document.querySelector('#panel-monthly .period-foot-more')!);
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'monthly',
    );
    expect(opens).toHaveLength(1);
  });

  it('"+N more" keydown Enter opens exactly once and does not double-fire the region', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    const more = document.querySelector('#panel-monthly .period-foot-more') as HTMLButtonElement;
    more.focus();
    spy.mockClear();
    await userEvent.keyboard('{Enter}');
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'monthly',
    );
    expect(opens).toHaveLength(1);
  });

  it('keydown guard is Enter/Space-scoped: a non-activation key bubbles', () => {
    const bubbled: string[] = [];
    render(
      <div onKeyDown={(e) => bubbled.push(e.key)}>
        <BoardModeContext.Provider value="stack">
          <MonthlyPanel />
        </BoardModeContext.Provider>
      </div>,
    );
    const more = document.querySelector('#panel-monthly .period-foot-more') as HTMLButtonElement;
    fireEvent.keyDown(more, { key: 'Enter' });
    fireEvent.keyDown(more, { key: 'ArrowDown' });
    expect(bubbled).not.toContain('Enter');   // stopped
    expect(bubbled).toContain('ArrowDown');    // allowed to bubble (Shift+Arrow reorder)
  });

  it('reduced motion: bars render at target width immediately (no width:0 frame)', () => {
    vi.mocked(useReducedMotion).mockReturnValue(true);
    renderAt('stack');
    const firstBar = document.querySelector('#panel-monthly .model-stack > span') as HTMLElement;
    expect(firstBar.style.width).not.toBe('0%');
  });

  it('reduced motion: rows revealed by a stack→bento transition do not animate (§4a)', () => {
    vi.mocked(useReducedMotion).mockReturnValue(true);
    const { rerender } = renderAt('stack');   // 3 rows visible
    rerender(
      <BoardModeContext.Provider value="bento">
        <MonthlyPanel />
      </BoardModeContext.Provider>,
    );
    const bars = document.querySelectorAll('#panel-monthly .model-stack > span');
    expect(bars.length).toBeGreaterThan(0);
    bars.forEach((b) => expect((b as HTMLElement).style.width).not.toBe('0%'));
  });

  it('"+N more" keydown Space opens exactly once', async () => {
    const spy = vi.spyOn(store, 'dispatch');
    renderAt('stack');
    const more = document.querySelector('#panel-monthly .period-foot-more') as HTMLButtonElement;
    more.focus();
    spy.mockClear();
    await userEvent.keyboard(' ');
    const opens = spy.mock.calls.filter(
      ([a]) => (a as { type: string; kind?: string }).type === 'OPEN_MODAL'
            && (a as { kind?: string }).kind === 'monthly',
    );
    expect(opens).toHaveLength(1);
  });
});

// #556 S2 §5.1 — under All, Monthly renders provider SECTIONS, the treatment
// WeeklyPanel already implements. Locally, not extracted: the two panels' rows,
// columns and footers differ.
describe('#556 S2 — All-mode provider sections', () => {
  function allEnvelope(): Envelope {
    const env = baseEnvelope() as unknown as Record<string, unknown>;
    env.source_schema_version = 9;
    env.default_source = 'claude';
    env.source_order = ['claude', 'codex', 'all'];
    env.sources = {
      claude: {
        availability: 'ok', capabilities: {}, warnings: [],
        last_success_at: '2026-07-01T10:00:00Z',
        data: {
          hero: { cost_usd: 0, total_tokens: 0, header: null, current_week: null, forecast: null, trend: null },
          periods: {
            daily: { rows: [], quantile_thresholds: [], peak: null },
            monthly: { rows: MONTHLY.slice(0, 2) },
            weekly: { rows: [] },
          },
          sessions: { rows: [] },
          projects: { current_week: { rows: [] }, trend: { projects: [] }, rows: [] },
          quota: { current_week: {}, blocks: [], milestones: [], five_hour_milestones: [] },
          budget: { forecast: null, settings: null },
          alerts: { rows: [] },
        },
      },
      codex: {
        availability: 'ok', capabilities: {}, warnings: [],
        last_success_at: '2026-07-01T10:00:00Z',
        data: {
          hero: {}, periods: {
            daily: { rows: [] },
            monthly: {
              rows: [{
                label: '2026-06', cost_usd: 44, input_tokens: 3,
                cached_input_tokens: 1, output_tokens: 1,
                reasoning_output_tokens: 0, total_tokens: 5, models: ['gpt-5'],
              }],
            },
            weekly: { rows: [] },
          },
          projects: { rows: [] },
          quota: { histories: [], blocks: [], summary: { active: [] } },
          budget: { status: null }, alerts: { rows: [] },
          cache_report: null,
        },
      },
      all: {
        availability: 'ok', capabilities: {}, warnings: [],
        last_success_at: '2026-07-01T10:00:00Z',
        data: { combined: null, alerts: { rows: [] }, providers: { claude: null, codex: null } },
      },
    };
    return env as unknown as Envelope;
  }

  beforeEach(() => {
    updateSnapshot(allEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  // #556 S4 F8 — see WeeklyPanel.test.tsx for the rule this asserts: an h3
  // naming the section, the accessible name resolved through it, no competing
  // `aria-label`, and unique ids.
  it('names each provider section by its own heading (#556 S4)', () => {
    const { container } = render(<MonthlyPanel />);
    const sections = container.querySelectorAll('[data-provider-section]');
    expect(sections.length).toBe(2);
    sections.forEach((s) => {
      const labelledBy = s.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(s.getAttribute('aria-label')).toBeNull();
      const heading = container.querySelector(`[id="${labelledBy}"]`);
      expect(heading).not.toBeNull();
      expect(heading!.tagName).toBe('H3');
      expect(heading!.textContent).toMatch(/^(Claude|Codex) monthly history$/);
    });
    const ids = [...container.querySelectorAll('h3[id]')].map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('renders one labelled section per provider instead of one merged list', () => {
    const { container } = render(<MonthlyPanel />);
    const claude = container.querySelector('[data-provider-section="claude"]');
    const codex = container.querySelector('[data-provider-section="codex"]');
    expect(claude).not.toBeNull();
    expect(codex).not.toBeNull();
    expect(claude!.textContent).toContain('2026-07');
    expect(codex!.textContent).toContain('2026-06');
  });

  it('keeps each provider row in its own section rather than merging by label', () => {
    const { container } = render(<MonthlyPanel />);
    const codex = container.querySelector('[data-provider-section="codex"]');
    // $44.00 is the Codex row's own cost. A merged row would have added it to
    // the Claude row sharing its label.
    expect(codex!.textContent).toContain('$44.00');
    expect(container.querySelector('[data-provider-section="claude"]')!.textContent)
      .not.toContain('$44.00');
  });
});

// #556 S2 §5.2 — Monthly states a MONTH-LABEL span, deliberately not exact
// dates. Monthly rows carry no bounds, and the server's current-month read ends
// at `now_utc` rather than at the end of the labelled month, so month labels
// cannot distinguish a partial current month from a complete one. Claiming
// exact dates would assert a boundary the data does not carry.
describe('#556 S2 — the All monthly footer', () => {
  function allEnvelope(): Envelope {
    const slice = makeSourceEnvelope();
    slice.sources.claude.data!.periods.monthly.rows = [
      { label: '2026-04', cost_usd: 30, total_tokens: 1, input_tokens: 1,
        output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
        used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
        is_current: true, models: [] },
      { label: '2026-03', cost_usd: 20, total_tokens: 1, input_tokens: 1,
        output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
        used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
        is_current: false, models: [] },
    ];
    slice.sources.codex.data!.periods.monthly.rows = [{
      label: '2026-02', cost_usd: 12, input_tokens: 1, cached_input_tokens: 0,
      output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1,
      models: ['gpt-5'],
    }];
    return { ...baseEnvelope(), ...slice } as unknown as Envelope;
  }

  beforeEach(() => {
    updateSnapshot(allEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('states the combined cost, the month-label span and both provider legs', () => {
    const { container } = render(<MonthlyPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).toContain('$62.00');
    expect(foot!.textContent).toContain('2026-02 – 2026-04');
    expect(foot!.textContent).toContain('Claude $50.00');
    expect(foot!.textContent).toContain('Codex $12.00');
  });

  it('never claims exact dates, which monthly rows do not carry', () => {
    const { container } = render(<MonthlyPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).not.toMatch(/[A-Z][a-z]{2} \d{2}/);
  });
});


// #556 S2 QA P3 — the footer counts PROVIDER-months and must say so.
//
// "10mo total" beside a span of "2026-01 – 2026-08" invites the arithmetic
// 2026-01 through 2026-08 = 8 calendar months, and the 10 is Claude's 8 plus
// Codex's 2. Weekly already gets this right with "24 provider periods total".
describe('#556 S2 — the Monthly footer and modal name what they count', () => {
  function allEnvelope(): Envelope {
    const slice = makeSourceEnvelope();
    slice.sources.claude.data!.periods.monthly.rows = [
      { label: '2026-04', cost_usd: 30, total_tokens: 1, input_tokens: 1,
        output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
        used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
        is_current: true, models: [] },
    ];
    slice.sources.codex.data!.periods.monthly.rows = [{
      label: '2026-02', cost_usd: 12, input_tokens: 1, cached_input_tokens: 0,
      output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1,
      models: ['gpt-5'],
    }];
    return { ...baseEnvelope(), ...slice } as unknown as Envelope;
  }

  it('says "provider months", not "mo", under All', () => {
    updateSnapshot(allEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<MonthlyPanel />);
    const foot = container.querySelector('.panel-foot')!.textContent!;
    expect(foot).toContain('provider months total');
    expect(foot).not.toMatch(/\d+mo total/);
  });

  it('leaves the single-provider footer reading "Nmo total"', () => {
    updateSnapshot(allEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<MonthlyPanel />);
    const foot = container.querySelector('.panel-foot');
    if (foot) {
      expect(foot.textContent).toMatch(/\d+mo total/);
      expect(foot.textContent).not.toContain('provider months');
    }
  });
});


// #556 S2 QA — same defect and same remedy as Weekly. Measured at 390px before
// this change: the Monthly h2 had clientWidth 178 against scrollWidth 254, 70%
// visible, and `by provider` was in the hidden 30%.
describe('#556 S2 QA — the Monthly composition is off the h2', () => {
  beforeEach(() => {
    updateSnapshot({ ...baseEnvelope(), ...makeSourceEnvelope() } as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('states "by provider" on the sub-line, not inside the truncating h2', () => {
    const { container } = render(<MonthlyPanel />);
    const h2 = container.querySelector('.panel-header h2')!.textContent!;
    expect(h2).toContain('model split');
    expect(h2).not.toContain('by provider');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toContain('by provider');
  });

  it('renders the sub-line as a SIBLING of the header, which is what lets it wrap', () => {
    const { container } = render(<MonthlyPanel />);
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
    const note = container.querySelector('.panel-range-note')!;
    expect(note.parentElement!.querySelector(':scope > .panel-header')).not.toBeNull();
  });

  it('adds no sub-line on a single-provider tab, which composes nothing', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<MonthlyPanel />);
    expect(container.querySelector('.panel-range-note')).toBeNull();
  });
});
