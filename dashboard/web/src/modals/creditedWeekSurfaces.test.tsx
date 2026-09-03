// #703 + #707 §6.1/§6.3 — the credited-week surfaces a real-browser QA pass
// found still wrong after the feature shipped.
//
// The milestone ladder rendered two segments as one table whose percent column
// ran 1 through 40 and then restarted at 22, with cumulative cost falling at
// that row and no separator between them. The client's divider row was already
// written; the server published an empty list, so the branch was unreachable.
//
// The withheld `$/1%` cause reached the Weekly panel and the weekly detail card
// but not the Current Week hero or its modal, which rendered `$0.000` beside
// `spent $0.00` — the misleading figure §6.3 exists to replace.
//
// And the weekly detail card's cause was confined to one 85px track of a
// five-track grid, wrapping to three lines with a word orphaned on the label
// line while most of the row sat empty.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CurrentWeekModal } from './CurrentWeekModal';
import { PeriodDetailCard } from './PeriodDetailCard';
import { HeroStrip } from '../components/HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { uninstallGlobalKeydown } from '../store/keymap';
import { clearMilestoneHistoryCacheForTests } from './milestoneHistory';
import type { Envelope, ModelCostRow, PeriodRow, WeekIndexEntry } from '../types/envelope';

// ── shared fixtures ────────────────────────────────────────────────────

const CREDITED_ENTRY: WeekIndexEntry = {
  key: 'milestone_cycle:credited',
  start_at_utc: '2026-04-13T14:00:00Z',
  end_at_utc: '2026-04-20T14:00:00Z',
  label: 'Apr 13–Apr 20',
  is_current: true,
  milestone_count: 4,
  block_count: 0,
  segment_count: 2,
  detail_stamp: 'st-credited',
};

function ms(percent: number, at: string, cumulative: number) {
  return {
    percent,
    crossed_at_utc: at,
    cumulative_usd: cumulative,
    marginal_usd: 1,
    five_hour_pct_at_cross: null,
  };
}

function creditedPayload(dividers: unknown[]) {
  return {
    source: 'claude',
    key: 'milestone_cycle:credited',
    label: 'Apr 13–Apr 20',
    start_at_utc: '2026-04-13T14:00:00Z',
    end_at_utc: '2026-04-20T14:00:00Z',
    is_current: true,
    detail_stamp: 'st-credited',
    segments: [
      {
        key: 'milestone_segment:pre',
        milestones: [
          ms(1, '2026-04-13T20:00:00Z', 1),
          ms(40, '2026-04-16T08:00:00Z', 35.64),
        ],
      },
      {
        key: 'milestone_segment:post',
        milestones: [
          ms(22, '2026-04-16T10:00:00Z', 13.95),
          ms(24, '2026-04-17T11:00:00Z', 16.1),
        ],
      },
    ],
    dividers,
    blocks: [],
  };
}

function creditedEnv(
  over: Record<string, unknown> = {},
  entry: WeekIndexEntry = CREDITED_ENTRY,
): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-17T12:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'Apr 13–Apr 20',
      used_pct: 22,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: {
      used_pct: 22,
      five_hour_pct: null,
      five_hour_resets_in_sec: null,
      spent_usd: 0,
      dollar_per_pct: null,
      dollar_per_pct_withheld: 'no-climb-since-credit',
      // The ACCOUNTING anchor, deliberately later than the week's own start.
      week_start_at: '2026-04-16T09:41:00Z',
      reset_at_utc: '2026-04-20T14:00:00Z',
      reset_in_sec: 265000,
      last_snapshot_age_sec: 30,
      milestones: [],
      freshness: { label: 'fresh', captured_at: '2026-04-17T11:59:30Z', age_seconds: 30 },
      five_hour_block: null,
      five_hour_milestones: [],
      week_index: [entry],
      ...over,
    },
    forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
      budget_thresholds: [],
    },
  } as unknown as Envelope;
}

// The tests that exercise the hero / mini-stats never open a ladder, so they
// use an entry the modal will not fetch a detail payload for: an unmocked
// `fetch` would reject and put the modal into its error state, which is a
// different screen from the one under test.
const SINGLE_SEGMENT_ENTRY: WeekIndexEntry = {
  ...CREDITED_ENTRY, segment_count: 1, milestone_count: 0,
};

function mockFetch(payload: unknown) {
  const spy = vi.fn(async () => ({ ok: true, json: async () => payload }));
  global.fetch = spy as unknown as typeof fetch;
  return spy;
}

beforeEach(() => {
  _resetForTests();
  clearMilestoneHistoryCacheForTests();
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
  _resetForTests();
});

// ── P1-A: the ladder's credit divider ──────────────────────────────────

describe('the milestone ladder separates a credited week\'s two segments', () => {
  async function renderLadder(dividers: unknown[]) {
    mockFetch(creditedPayload(dividers));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(creditedEnv());
    const view = render(<CurrentWeekModal />);
    await screen.findByText('$16.10');
    return view;
  }

  it('draws the credit row between the two ladders', async () => {
    const { container } = await renderLadder([
      { effective_at_utc: '2026-04-16T09:41:00Z', prior_percent: 40 },
    ]);
    const rows = Array.from(
      container.querySelectorAll('#mcw-rows tr'),
    ) as HTMLElement[];
    const creditIndex = rows.findIndex(
      (r) => r.className === 'mcw-5h-credit-row',
    );
    // Exactly one, and it sits between the last pre-credit row and the first
    // post-credit one — not appended at either end.
    expect(creditIndex).toBe(2);
    expect(rows).toHaveLength(5);
    expect(rows[1].textContent).toContain('40');
    expect(rows[3].textContent).toContain('22');
  });

  it('names the credit and the level it dropped from', async () => {
    const { container } = await renderLadder([
      { effective_at_utc: '2026-04-16T09:41:00Z', prior_percent: 40 },
    ]);
    const cell = container.querySelector('.mcw-5h-credit-cell') as HTMLElement;
    expect(cell.textContent).toContain('CREDIT');
    expect(cell.textContent).toContain('from 40%');
    // Full width, so nothing lands in the percent column and reads as a rung.
    expect(cell.getAttribute('colspan')).toBe('5');
  });

  it('draws nothing for an unresolvable credit but keeps every rung', async () => {
    // A `null` slot preserves index alignment; dropping it would pair the next
    // credit with the wrong ladder.
    const { container } = await renderLadder([null]);
    expect(container.querySelector('.mcw-5h-credit-row')).toBeNull();
    expect(container.querySelectorAll('#mcw-rows tr')).toHaveLength(4);
  });

  it('omits the level clause when the credit recorded no prior reading', async () => {
    const { container } = await renderLadder([
      { effective_at_utc: '2026-04-16T09:41:00Z', prior_percent: null },
    ]);
    const cell = container.querySelector('.mcw-5h-credit-cell') as HTMLElement;
    expect(cell.textContent).toContain('CREDIT');
    expect(cell.textContent).not.toContain('from');
  });
});

// ── P1-B: the withheld `$/1%` on the current-week surfaces ─────────────

describe('the current-week hero states why the ratio is withheld', () => {
  it('prints the cause instead of $0.000', () => {
    updateSnapshot(creditedEnv({}, SINGLE_SEGMENT_ENTRY));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    const spent = container.querySelector('.hero-spent') as HTMLElement;
    expect(spent.textContent).toContain('No climb since credit');
    expect(spent.textContent).not.toContain('$0.000');
    expect(container.querySelector('.hero-dpp-withheld')).not.toBeNull();
  });

  it('keeps a published rate when one exists', () => {
    const env = creditedEnv({}, SINGLE_SEGMENT_ENTRY);
    (env.header as unknown as Record<string, unknown>).dollar_per_pct = 25;
    (env.current_week as unknown as Record<string, unknown>).dollar_per_pct = 25;
    (env.current_week as unknown as Record<string, unknown>)
      .dollar_per_pct_withheld = null;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('.hero-dpp-withheld')).toBeNull();
    expect((container.querySelector('.hero-spent') as HTMLElement).textContent)
      .toContain('$25.00 / 1% used');
  });

  it('renders an unrecognized cause verbatim rather than falling back to a figure', () => {
    const env = creditedEnv({}, SINGLE_SEGMENT_ENTRY);
    (env.current_week as unknown as Record<string, unknown>)
      .dollar_per_pct_withheld = 'some-future-cause';
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('.hero-dpp-withheld')?.textContent)
      .toBe('some-future-cause');
  });
});

describe('the current-week modal states why the ratio is withheld', () => {
  function renderModal(env: Envelope) {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(env);
    return render(<CurrentWeekModal />);
  }

  it('prints the cause instead of $0.000', () => {
    const { container } = renderModal(creditedEnv({}, SINGLE_SEGMENT_ENTRY));
    const cell = container.querySelector('#mcw-dpp') as HTMLElement;
    expect(cell.textContent).toBe('No climb since credit');
    expect(cell.className).toContain('withheld');
  });

  it('keeps a published rate when one exists', () => {
    const env = creditedEnv({}, SINGLE_SEGMENT_ENTRY);
    (env.current_week as unknown as Record<string, unknown>).dollar_per_pct = 25;
    (env.current_week as unknown as Record<string, unknown>)
      .dollar_per_pct_withheld = null;
    const { container } = renderModal(env);
    const cell = container.querySelector('#mcw-dpp') as HTMLElement;
    expect(cell.textContent).toBe('$25.000');
    expect(cell.className).not.toContain('withheld');
  });
});

// ── P2-B: the window label names the week, not the credit ──────────────

describe('the displayed window is the week, not the accounting anchor', () => {
  it('labels the hero from `header.week_label`, never from the accounting anchor', () => {
    // The envelope's `current_week.week_start_at` is the credit instant
    // (Apr 16) because that is the range `spent_usd` covers. The window a
    // person is shown is the week's own, which the server publishes as
    // `header.week_label`.
    updateSnapshot(creditedEnv({}, SINGLE_SEGMENT_ENTRY));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    const usage = container.querySelector('.hero-usage') as HTMLElement;
    expect(usage.textContent).toContain('Apr 13–Apr 20');
    expect(usage.textContent).not.toContain('Apr 16');
  });

  it('labels the modal pill from the same label', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(creditedEnv({}, SINGLE_SEGMENT_ENTRY));
    const { container } = render(<CurrentWeekModal />);
    const pill = container.querySelector('#mcw-week-pill')!;
    expect(pill.textContent).toContain('Apr 13–Apr 20');
  });
});

// ── P2-A: the cause gets a track it fits in ────────────────────────────

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 100 },
];

function weeklyRow(over: Partial<PeriodRow> = {}): PeriodRow {
  return {
    label: '04-13', cost_usd: 500, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 22, dollar_per_pct: 50, delta_cost_pct: 10, is_current: true,
    models, week_start_at: '2026-04-13T14:00:00+00:00',
    week_end_at: '2026-04-20T14:00:00+00:00', ...over,
  };
}

describe('the weekly detail card gives the cause room', () => {
  it('widens only the cell that holds prose', () => {
    render(
      <PeriodDetailCard
        row={weeklyRow({
          credited: true,
          dollar_per_pct: null,
          dollar_per_pct_withheld: 'no-climb-since-credit',
        })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    const cells = Array.from(document.querySelectorAll('.stats2 .s'));
    expect(cells).toHaveLength(2);
    // `Used %` keeps its own track; only the prose cell spans the rest.
    expect(cells[0].className).not.toContain('s-wide');
    expect(cells[1].className).toContain('s-wide');
  });

  it('leaves a rendered figure in its ordinary track', () => {
    render(
      <PeriodDetailCard
        row={weeklyRow({ credited: true, dollar_per_pct: 50 })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    const cells = Array.from(document.querySelectorAll('.stats2 .s'));
    expect(cells.some((c) => c.className.includes('s-wide'))).toBe(false);
  });
});
