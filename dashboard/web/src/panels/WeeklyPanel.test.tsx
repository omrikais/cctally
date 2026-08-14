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
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';

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
  it('discloses every account behind a pooled Codex weekly row', () => {
    const env = baseEnvelope();
    env.weekly!.rows = [
      periodRow({
        label: '04-20',
        account_labels: ['work@example.com', 'personal@example.com'],
      }),
    ];
    updateSnapshot(env);
    render(<WeeklyPanel />);
    expect(
      Array.from(document.querySelectorAll('.period-account-chip')).map(
        (chip) => chip.textContent,
      ),
    ).toEqual(['work@example.com', 'personal@example.com']);
  });

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

// #556 S2 §5.2 — the combined footer names its span and its provider split.
//
// Before this, the footer stated one combined cost across twelve Claude
// subscription weeks plus twelve Codex native cycles with no range beside it
// and no attribution. The figure is a USD sum, which the anti-blend contract
// permits, so it stays — and it now says what it covers and what it is made of.
describe('#556 S2 — the All weekly footer', () => {
  function allEnvelope(): Envelope {
    const slice = makeSourceEnvelope();
    slice.sources.claude.data!.periods.weekly.rows = [
      {
        label: '04-13', cost_usd: 30, total_tokens: 1, input_tokens: 1,
        output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
        used_pct: 20, dollar_per_pct: 1.5, delta_cost_pct: null,
        is_current: true, models: [],
        week_start_at: '2026-04-13T14:00:00Z',
        week_end_at: '2026-04-20T14:00:00Z',
      },
      {
        label: '04-06', cost_usd: 20, total_tokens: 1, input_tokens: 1,
        output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
        used_pct: 10, dollar_per_pct: 2, delta_cost_pct: null,
        is_current: false, models: [],
        week_start_at: '2026-04-06T14:00:00Z',
        week_end_at: '2026-04-13T14:00:00Z',
      },
    ];
    slice.sources.codex.data!.periods.weekly.rows = [{
      label: '04-14 08:00', cost_usd: 12, input_tokens: 1,
      cached_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0,
      total_tokens: 1, models: ['gpt-5'],
      start_at: '2026-04-14T08:00:00Z', end_at: '2026-04-21T08:00:00Z',
    }];
    return { ...baseEnvelope(), ...slice } as unknown as Envelope;
  }

  beforeEach(() => {
    updateSnapshot(allEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  // #556 S4 F8 — every All provider section exposes a level-3 heading naming
  // that section, and the section's accessible name resolves THROUGH that
  // heading via `aria-labelledby` rather than through a separate `aria-label`
  // string, so the landmark and its heading carry one string and not two
  // competing ones. h3 because panel titles and modal titles are both h2, so a
  // provider section sits one level below either host. Nothing changes
  // visually: the heading is `sr-only`, and an absolutely-positioned flex child
  // is out of flow, so the head's `gap` does not grow. Ids are
  // surface-qualified so a panel and its modal can be mounted at once without
  // colliding.
  it('names each provider section by its own heading (#556 S4)', () => {
    const { container } = render(<WeeklyPanel />);
    const sections = container.querySelectorAll('[data-provider-section]');
    expect(sections.length).toBe(2);
    sections.forEach((s) => {
      const labelledBy = s.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(s.getAttribute('aria-label')).toBeNull();
      const heading = container.querySelector(`[id="${labelledBy}"]`);
      expect(heading).not.toBeNull();
      expect(heading!.tagName).toBe('H3');
      expect(heading!.textContent).toMatch(/^(Claude|Codex) weekly quota history$/);
    });
    const ids = [...container.querySelectorAll('h3[id]')].map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('states the combined cost, the resolved span and both provider legs', () => {
    const { container } = render(<WeeklyPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).toContain('$62.00');
    // The union of the two displayed provider sections: the earliest Claude
    // week start through the latest Codex cycle end.
    expect(foot!.textContent).toContain('Apr 06 – Apr 21');
    expect(foot!.textContent).toContain('Claude $50.00');
    expect(foot!.textContent).toContain('Codex $12.00');
  });

  it('leaves the single-provider footer alone', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<WeeklyPanel />);
    const foot = container.querySelector('.panel-foot');
    expect(foot!.textContent).not.toContain('Claude $');
    expect(foot!.textContent).not.toContain('–');
  });
});



// #556 S2 QA — the composition must SURVIVE 390px, not merely be present in
// the markup. The h2 carries `white-space: nowrap` + `text-overflow: ellipsis`
// on phones, so anything appended to it competes with the actions cluster and
// the composition — the last thing in the string — is what gets cut. Measured
// at 390px before this change: clientWidth 178 against scrollWidth 246, 72%
// visible, and `by provider` was in the hidden 28%. The composition therefore
// moves onto `.panel-range-note`, the full-width wrapping sub-line Projects
// already uses, which is a SIBLING of `.panel-header` rather than a child.
describe('#556 S2 QA — the Weekly composition is off the h2', () => {
  beforeEach(() => {
    updateSnapshot({ ...baseEnvelope(), ...makeSourceEnvelope() } as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('states "by provider" on the sub-line, not inside the truncating h2', () => {
    const { container } = render(<WeeklyPanel />);
    const h2 = container.querySelector('.panel-header h2')!.textContent!;
    expect(h2).toContain('model split');
    expect(h2).not.toContain('by provider');
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toContain('by provider');
  });

  it('renders the sub-line as a SIBLING of the header, which is what lets it wrap', () => {
    const { container } = render(<WeeklyPanel />);
    // Inside the header it would share the row with the actions cluster and
    // meet the same nowrap rule that truncated the h2.
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
    const note = container.querySelector('.panel-range-note')!;
    expect(note.parentElement!.querySelector(':scope > .panel-header')).not.toBeNull();
  });

  it('adds no sub-line on a single-provider tab, which composes nothing', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<WeeklyPanel />);
    expect(container.querySelector('.panel-range-note')).toBeNull();
    expect(container.querySelector('.panel-header h2')!.textContent)
      .toContain('model split');
  });
});
