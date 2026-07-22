// Hero-modal week/cycle history navigation — JSDOM-scoped behaviour
// (stepping, fetch policy, credit divider, embedded keymap suppression,
// Share visibility, vanish). Focus/real-keyboard/scroll are the browser gate.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { CurrentWeekModal } from './CurrentWeekModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { installGlobalKeydown, registeredBindings, uninstallGlobalKeydown } from '../store/keymap';
import { clearMilestoneHistoryCacheForTests } from './milestoneHistory';
import codexFixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope, WeekIndexEntry } from '../types/envelope';

function idxEntry(key: string, opts: Partial<WeekIndexEntry> = {}): WeekIndexEntry {
  return {
    key,
    start_at_utc: null,
    end_at_utc: null,
    label: `Wk ${key}`,
    is_current: false,
    milestone_count: 1,
    block_count: 1,
    segment_count: 1,
    detail_stamp: `st-${key}`,
    ...opts,
  };
}

const INDEX: WeekIndexEntry[] = [
  idxEntry('milestone_cycle:current', { is_current: true, label: 'Jul 18–Jul 25' }),
  idxEntry('milestone_cycle:post-reset', {
    start_at_utc: '2026-07-16T05:00:00Z', end_at_utc: '2026-07-18T05:00:00Z',
    label: 'Jul 15–Jul 17',
  }),
  idxEntry('milestone_cycle:pre-reset', {
    start_at_utc: '2026-07-11T05:00:00Z', end_at_utc: '2026-07-16T05:00:00Z',
    label: 'Jul 10–Jul 15',
  }),
];

function makeEnv(weekIndex: WeekIndexEntry[], generatedAt = '2026-05-18T12:00:00Z'): Envelope {
  return {
    generated_at: generatedAt,
    header: { week_label: 'May 15–22' },
    current_week: {
      used_pct: 20,
      five_hour_pct: null,
      five_hour_resets_in_sec: null,
      spent_usd: 3,
      dollar_per_pct: 0.15,
      reset_at_utc: '2026-05-22T00:00:00Z',
      reset_in_sec: null,
      last_snapshot_age_sec: null,
      milestones: [
        { percent: 1, crossed_at_utc: '2026-05-16T10:00:00Z', cumulative_usd: 1, marginal_usd: 1, five_hour_pct_at_cross: null },
      ],
      freshness: null,
      five_hour_block: null,
      five_hour_milestones: [],
      week_index: weekIndex,
    },
  } as unknown as Envelope;
}

const HISTORIC_CYCLE_PAYLOAD = {
  source: 'claude',
  key: 'milestone_cycle:post-reset',
  label: 'Jul 16–Jul 18',
  start_at_utc: '2026-07-16T05:00:00Z',
  end_at_utc: '2026-07-18T05:00:00Z',
  is_current: false,
  detail_stamp: 'st-milestone_cycle:post-reset',
  segments: [
    { key: 'milestone_segment:post-reset', milestones: [{ percent: 1, crossed_at_utc: '2026-07-16T10:00:00Z', cumulative_usd: 1, marginal_usd: 1, five_hour_pct_at_cross: null }] },
  ],
  dividers: [],
  blocks: [],
};

function mockFetch(payload: unknown) {
  const spy = vi.fn(async () => ({ ok: true, json: async () => payload }));
  global.fetch = spy as unknown as typeof fetch;
  return spy;
}

beforeEach(() => {
  _resetForTests();
  clearMilestoneHistoryCacheForTests();
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
  dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
});

afterEach(() => {
  uninstallGlobalKeydown();
  vi.restoreAllMocks();
});

describe('week nav chip', () => {
  it('renders ‹/› and disables the newer step on the current week', () => {
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    const older = screen.getByLabelText('Older week') as HTMLButtonElement;
    const newer = screen.getByLabelText('Newer week') as HTMLButtonElement;
    expect(older.disabled).toBe(false);
    expect(newer.disabled).toBe(true);
  });
});

describe('fetch policy + reset-defined cycle wire', () => {
  it('fetches one opaque historic cycle and renders only its ledger', async () => {
    const spy = mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older week'));
    await screen.findByText('Jul 16–Jul 18');
    expect(spy).toHaveBeenCalledWith('/api/milestones/claude/week/milestone_cycle%3Apost-reset');
    expect(screen.queryByText(/CREDIT/)).toBeNull();
  });

  it('does NOT fetch on mount for a single-segment current week', () => {
    const spy = mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('keyboard registration (embedded suppression)', () => {
  it('registers arrow bindings in the single-provider variant', () => {
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    const keys = registeredBindings().map((b) => b.key);
    expect(keys).toContain('ArrowUp');
    expect(keys).toContain('ArrowDown');
    expect(keys).toContain('ArrowLeft');
    expect(keys).toContain('ArrowRight');
  });

  it('registers NO arrow bindings in the embedded All variant', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    const keys = registeredBindings().map((b) => b.key);
    expect(keys).not.toContain('ArrowLeft');
    expect(keys).not.toContain('ArrowRight');
  });
});

describe('Share visibility + vanish', () => {
  it('hides the Share icon on a historic week', async () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    expect(screen.queryByLabelText('Share Current week report')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Older week'));
    await waitFor(() => expect(screen.queryByLabelText('Share Current week report')).toBeNull());
    expect(screen.queryByLabelText('Share Current week report')).toBeNull();
  });

  it('shows a vanished state with return-to-current when the selected key disappears', async () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older week'));
    await screen.findByText('Jul 16–Jul 18');
    // A later snapshot drops the selected cycle from the index.
    const shrunk = [idxEntry('milestone_cycle:current', { is_current: true })];
    act(() => updateSnapshot(makeEnv(shrunk, '2026-05-18T12:05:00Z')));
    await screen.findByText(/no longer available/i);
    fireEvent.click(screen.getByText('Back to current'));
    await waitFor(() => expect(screen.queryByText(/no longer available/i)).toBeNull());
    expect(screen.getByLabelText('Newer week')).toBeTruthy();
  });
});

// P1-A — the herobar override (big number / spent / $-per-% / reset) is
// spec §4 tied to HISTORIC weeks only; the CURRENT week's herobar must stay
// envelope-driven (live fractional `used_pct` / `spent_usd`) even while its
// tables render from a fetched detail payload after a block-step.
describe('current-week herobar stays envelope-driven (P1-A)', () => {
  function withHero(env: Envelope, usedPct: number, spentUsd: number): Envelope {
    const cw = env.current_week as unknown as { used_pct: number; spent_usd: number };
    cw.used_pct = usedPct;
    cw.spent_usd = spentUsd;
    return env;
  }

  it('keeps the herobar envelope-driven after a first block-step on a single-segment current week', async () => {
    const payload = {
      source: 'claude', key: 'milestone_cycle:current', label: 'Jul 18–Jul 25',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: 'st-milestone_cycle:current',
      segments: [{ key: 'milestone_segment:current', milestones: [
        { percent: 5, crossed_at_utc: '2026-05-16T10:00:00Z', cumulative_usd: 2, marginal_usd: 1, five_hour_pct_at_cross: null },
      ] }],
      dividers: [],
      blocks: [
        { five_hour_window_key: 900, block_start_at: '2026-05-16T00:00:00Z', five_hour_resets_at: '2026-05-16T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [], credits: [] },
        { five_hour_window_key: 901, block_start_at: '2026-05-16T05:00:00Z', five_hour_resets_at: '2026-05-16T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [
          { percent_threshold: 15, reset_event_id: 0, captured_at_utc: '2026-05-16T06:00:00Z', block_cost_usd: 0.5, marginal_cost_usd: 0.5, seven_day_pct_at_crossing: 42 },
        ], credits: [] },
      ],
    };
    mockFetch(payload);
    updateSnapshot(withHero(
      makeEnv([idxEntry('milestone_cycle:current', { is_current: true, segment_count: 1, block_count: 2 }), INDEX[1]]),
      42.7, 3,
    ));
    render(<CurrentWeekModal />);
    fireEvent.click(await screen.findByText(/‹ blocks/));
    // One action both hydrates the cycle detail and applies the requested
    // direction from the active/default last block.
    await screen.findByText(/Block 1 of 2/);
    // … while the herobar stays envelope-driven.
    expect(document.querySelector('#mcw-bignum .int')?.textContent).toBe('42');
    expect(document.querySelector('#mcw-spent')?.textContent).toContain('3.00');
  });
});

// P1 (spec §4) — the 5h block navigator (heading + BlockNavHeader) must stay
// mounted whenever the selected week's effective block list has ≥1 block; only
// the milestone TABLE varies. A selected block whose stream is empty (e.g. a
// cross-reset straddler with no integer-percent crossing) renders a compact
// empty-state line in place of the table, and the ⚡ position marker stays
// visible. Previously the outer guard gated the whole section on
// `selectedBlockStream.length > 0 || currentHasMoreBlocks`, which unmounted the
// navigator on empty-stream blocks (trapping mouse users) and dropped the
// section entirely on a historic week whose only block had no milestones.
describe('empty-stream block keeps the navigator mounted (P1)', () => {
  const HISTORIC_EMPTY_BLOCK = {
    source: 'claude', key: 'milestone_cycle:post-reset', label: 'Jul 16–Jul 18',
    start_at_utc: '2026-07-16T05:00:00Z', end_at_utc: '2026-07-18T05:00:00Z',
    is_current: false, detail_stamp: 'st-milestone_cycle:post-reset',
    segments: [{ key: 'milestone_segment:historic-empty', milestones: [
      { percent: 1, crossed_at_utc: '2026-05-09T10:00:00Z', cumulative_usd: 1, marginal_usd: 1, five_hour_pct_at_cross: null },
    ] }],
    dividers: [],
    blocks: [
      { five_hour_window_key: 700, block_start_at: '2026-05-09T00:00:00Z', five_hour_resets_at: '2026-05-09T05:00:00Z', final_five_hour_percent: 5, total_cost_usd: 1, crossed_seven_day_reset: true, is_closed: true, milestones: [], credits: [] },
    ],
  };

  it('renders the block-nav header (with ⚡) and an empty-state line for a historic week whose only block has no milestones', async () => {
    mockFetch(HISTORIC_EMPTY_BLOCK);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older week'));
    // The navigator stays mounted even though the block's stream is empty …
    const label = await screen.findByText(/Block 1 of 1/);
    expect(label.textContent).toContain('⚡'); // cross-reset straddler marker
    // … the milestone table is replaced by the compact empty-state line …
    expect(screen.getByText('No integer-percent crossings in this block.')).toBeTruthy();
    // … and no 5h table is rendered.
    expect(document.querySelector('#mcw-5h-table')).toBeNull();
  });

  it('keeps the navigator mounted after stepping onto an empty-stream block on the current week, and can step back', async () => {
    const payload = {
      source: 'claude', key: 'milestone_cycle:current', label: 'Jul 18–Jul 25',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: 'st-milestone_cycle:current',
      segments: [{ key: 'milestone_segment:current', milestones: [
        { percent: 5, crossed_at_utc: '2026-05-16T10:00:00Z', cumulative_usd: 2, marginal_usd: 1, five_hour_pct_at_cross: null },
      ] }],
      dividers: [],
      blocks: [
        { five_hour_window_key: 900, block_start_at: '2026-05-16T00:00:00Z', five_hour_resets_at: '2026-05-16T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [
          { percent_threshold: 15, reset_event_id: 0, captured_at_utc: '2026-05-16T02:00:00Z', block_cost_usd: 0.5, marginal_cost_usd: 0.5, seven_day_pct_at_crossing: 42 },
        ], credits: [] },
        { five_hour_window_key: 901, block_start_at: '2026-05-16T05:00:00Z', five_hour_resets_at: '2026-05-16T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: true, is_closed: true, milestones: [], credits: [] },
      ],
    };
    mockFetch(payload);
    updateSnapshot(makeEnv([idxEntry('milestone_cycle:current', { is_current: true, segment_count: 1, block_count: 2 }), INDEX[1]]));
    render(<CurrentWeekModal />);
    // First block-step lazily fetches the current week AND moves one position
    // older from the active/default last block.
    fireEvent.click(await screen.findByText(/‹ blocks/));
    await screen.findByText(/Block 1 of 2/);
    expect(document.querySelector('#mcw-5h-table')).toBeTruthy();
    expect(screen.queryByText('No integer-percent crossings in this block.')).toBeNull();
    // Stepping newer reaches the empty straddler without unmounting nav.
    fireEvent.click(screen.getByLabelText('Newer block'));
    const label = await screen.findByText(/Block 2 of 2/);
    expect(label.textContent).toContain('⚡');
    expect(screen.getByText('No integer-percent crossings in this block.')).toBeTruthy();
    expect(document.querySelector('#mcw-5h-table')).toBeNull();
    fireEvent.click(screen.getByLabelText('Older block'));
    await screen.findByText(/Block 1 of 2/);
  });
});

// P2-A (spec Q2-A) — every week view, current included, gets a block
// navigator whenever the selected entry has `block_count > 0`. The Codex
// current cycle carries no per-block envelope stream, so its block view is
// fully detail-driven: the first block-step lazily fetches the cycle detail.
const CODEX_IDX: WeekIndexEntry[] = [
  { key: 'milestone_cycle:codex-current', start_at_utc: '2026-04-23T00:00:00Z', end_at_utc: '2026-04-30T00:00:00Z', resets_at_utc: '2026-04-30T00:00:00Z', label: 'Cyc current', is_current: true, milestone_count: 1, block_count: 3, detail_stamp: 'st-cur' },
  { key: 'milestone_cycle:codex-prev', start_at_utc: '2026-04-16T00:00:00Z', end_at_utc: '2026-04-23T00:00:00Z', resets_at_utc: '2026-04-23T00:00:00Z', label: 'Cyc prev', is_current: false, milestone_count: 1, block_count: 3, detail_stamp: 'st-prev' },
];

function codexEnvWithIndex(cycleIndex: WeekIndexEntry[]): Envelope {
  const env = structuredClone(codexFixture) as unknown as Envelope;
  (env.sources!.codex!.data as unknown as { quota: { cycle_index: WeekIndexEntry[] } })
    .quota.cycle_index = cycleIndex;
  return env;
}

describe('Codex current-cycle block navigator (P2-A)', () => {
  function openCodex() {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
  }

  it('shows the block-nav affordance and one click fetches then lands on the immediately older block', async () => {
    const payload = {
      source: 'codex', key: 'milestone_cycle:codex-current', label: 'Cyc current',
      start_at_utc: '2026-04-23T00:00:00Z', end_at_utc: '2026-04-30T00:00:00Z',
      resets_at_utc: '2026-04-30T00:00:00Z', is_current: true, detail_stamp: 'st-cur',
      segments: [{ key: 'milestone_segment:codex-current', milestones: [] }],
      dividers: [],
      blocks: [
        { key: 'blk-1', block_start_at: '2026-04-24T00:00:00Z', five_hour_resets_at: '2026-04-24T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [] },
        { key: 'blk-2', block_start_at: '2026-04-24T05:00:00Z', five_hour_resets_at: '2026-04-24T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [] },
        { key: 'blk-3', block_start_at: '2026-04-24T10:00:00Z', five_hour_resets_at: '2026-04-24T15:00:00Z', final_five_hour_percent: 30, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: false, milestones: [] },
      ],
    };
    const spy = mockFetch(payload);
    openCodex();
    updateSnapshot(codexEnvWithIndex(CODEX_IDX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older block'));
    await waitFor(() => expect(spy).toHaveBeenCalledWith('/api/milestones/codex/week/milestone_cycle%3Acodex-current'));
    await screen.findByText(/Block 2 of 3/);
    fireEvent.click(screen.getByLabelText('Older block'));
    await screen.findByText(/Block 1 of 3/);
    expect((screen.getByLabelText('Older block') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('Newer block'));
    await screen.findByText(/Block 2 of 3/);
    fireEvent.click(screen.getByLabelText('Newer block'));
    await screen.findByText(/Block 3 of 3/);
    expect((screen.getByLabelText('Newer block') as HTMLButtonElement).disabled).toBe(true);
  });

  it('preserves an ArrowLeft step across lazy current-cycle detail fetch', async () => {
    const payload = {
      source: 'codex', key: 'milestone_cycle:codex-current', label: 'Cyc current',
      start_at_utc: '2026-04-23T00:00:00Z', end_at_utc: '2026-04-30T00:00:00Z',
      resets_at_utc: '2026-04-30T00:00:00Z', is_current: true, detail_stamp: 'st-cur',
      segments: [{ key: 'milestone_segment:codex-current', milestones: [] }],
      dividers: [],
      blocks: [
        { key: 'blk-1', block_start_at: '2026-04-24T00:00:00Z', five_hour_resets_at: '2026-04-24T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [] },
        { key: 'blk-2', block_start_at: '2026-04-24T05:00:00Z', five_hour_resets_at: '2026-04-24T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: false, milestones: [] },
        { key: 'blk-3', block_start_at: '2026-04-24T10:00:00Z', five_hour_resets_at: '2026-04-24T15:00:00Z', final_five_hour_percent: 30, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: false, milestones: [] },
      ],
    };
    const spy = mockFetch(payload);
    openCodex();
    updateSnapshot(codexEnvWithIndex(CODEX_IDX));
    render(<CurrentWeekModal />);
    installGlobalKeydown();
    fireEvent.keyDown(document, { key: 'ArrowLeft' });
    await waitFor(() => expect(spy).toHaveBeenCalledWith('/api/milestones/codex/week/milestone_cycle%3Acodex-current'));
    await screen.findByText(/Block 2 of 3/);
  });

  it('defaults a historic retained-5h cycle to its last block and traverses the complete list', async () => {
    const payload = {
      source: 'codex', key: 'milestone_cycle:codex-prev', label: 'Cyc prev',
      start_at_utc: '2026-04-16T00:00:00Z', end_at_utc: '2026-04-23T00:00:00Z',
      resets_at_utc: '2026-04-23T00:00:00Z', is_current: false, detail_stamp: 'st-prev',
      segments: [{ key: 'milestone_segment:codex-prev', milestones: [] }],
      dividers: [],
      blocks: [
        { key: 'prev-1', block_start_at: '2026-04-17T00:00:00Z', five_hour_resets_at: '2026-04-17T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: true, is_closed: true, milestones: [] },
        { key: 'prev-2', block_start_at: '2026-04-17T05:00:00Z', five_hour_resets_at: '2026-04-17T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [] },
        { key: 'prev-3', block_start_at: '2026-04-17T10:00:00Z', five_hour_resets_at: '2026-04-17T15:00:00Z', final_five_hour_percent: 30, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [] },
      ],
    };
    const spy = mockFetch(payload);
    openCodex();
    updateSnapshot(codexEnvWithIndex(CODEX_IDX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older week'));
    await waitFor(() => expect(spy).toHaveBeenCalledWith('/api/milestones/codex/week/milestone_cycle%3Acodex-prev'));
    await screen.findByText(/Block 3 of 3/);
    fireEvent.click(screen.getByLabelText('Older block'));
    await screen.findByText(/Block 2 of 3/);
    fireEvent.click(screen.getByLabelText('Older block'));
    await screen.findByText(/Block 1 of 3/);
    expect((screen.getByLabelText('Older block') as HTMLButtonElement).disabled).toBe(true);
  });

  it('hides the block navigator on the current cycle when block_count === 0', () => {
    mockFetch({});
    openCodex();
    updateSnapshot(codexEnvWithIndex([{ ...CODEX_IDX[0], block_count: 0 }, CODEX_IDX[1]]));
    render(<CurrentWeekModal />);
    expect(screen.queryByLabelText('Older block')).toBeNull();
    expect(screen.getByText('No 5h data retained for this cycle.')).toBeTruthy();
  });

  it('keeps Claude and Codex cycle selection independent in All', async () => {
    const codexHistoric = {
      source: 'codex', key: 'milestone_cycle:codex-prev', label: 'Cyc prev',
      start_at_utc: '2026-04-16T00:00:00Z', end_at_utc: '2026-04-23T00:00:00Z',
      resets_at_utc: '2026-04-23T00:00:00Z', is_current: false, detail_stamp: 'st-prev',
      segments: [{ key: 'milestone_segment:codex-prev', milestones: [] }],
      dividers: [], blocks: [],
    };
    global.fetch = vi.fn(async (input: RequestInfo | URL) => ({
      ok: true,
      json: async () => String(input).includes('/claude/')
        ? HISTORIC_CYCLE_PAYLOAD
        : codexHistoric,
    })) as unknown as typeof fetch;
    const env = codexEnvWithIndex(CODEX_IDX);
    env.current_week = makeEnv(INDEX).current_week;
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(env);
    const { container } = render(<CurrentWeekModal />);
    const claude = container.querySelector<HTMLElement>('[data-provider-section="claude"]')!;
    const codex = container.querySelector<HTMLElement>('[data-provider-section="codex"]')!;

    fireEvent.click(within(claude).getByLabelText('Older week'));
    await within(claude).findByText('Jul 16–Jul 18');
    expect(within(codex).queryByText('Cyc prev')).toBeNull();

    fireEvent.click(within(codex).getByLabelText('Older week'));
    await within(codex).findByText('Apr 16–Apr 23');
    expect(within(claude).getByText('Jul 16–Jul 18')).toBeTruthy();
  });
});
