// Hero-modal week/cycle history navigation — JSDOM-scoped behaviour
// (stepping, fetch policy, credit divider, embedded keymap suppression,
// Share visibility, vanish). Focus/real-keyboard/scroll are the browser gate.
import { Profiler } from 'react';
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

const HISTORIC_EMPTY_CYCLE_PAYLOAD = {
  ...HISTORIC_CYCLE_PAYLOAD,
  segments: [],
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
    const older = screen.getByLabelText('Older cycle') as HTMLButtonElement;
    const newer = screen.getByLabelText('Newer cycle') as HTMLButtonElement;
    expect(older.disabled).toBe(false);
    expect(newer.disabled).toBe(true);
  });
});

describe('fetch policy + reset-defined cycle wire', () => {
  it('fetches one opaque historic cycle and renders only its ledger', async () => {
    const spy = mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    expect(spy).toHaveBeenCalledWith('/api/milestones/claude/week/milestone_cycle%3Apost-reset');
    expect(screen.queryByText(/CREDIT/)).toBeNull();
  });

  it('uses cycle vocabulary for a historic cycle with no milestones', async () => {
    const spy = mockFetch(HISTORIC_EMPTY_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    expect(screen.getByText('No milestones recorded this cycle')).toBeTruthy();
    expect(spy).toHaveBeenCalledWith('/api/milestones/claude/week/milestone_cycle%3Apost-reset');
  });

  it('uses cycle vocabulary when a historic cycle cannot load', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch;
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    expect(await screen.findByText(/Couldn’t load this cycle/)).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load this week/)).toBeNull();
    // The history pill has moved, but the current envelope has not. No
    // percentage, spend, reset, progress or count belongs to the failed cycle.
    for (const id of ['mcw-bignum', 'mcw-spent', 'mcw-dpp', 'mcw-reset', 'mcw-pbar', 'mcw-ms-count']) {
      expect(document.querySelector(`#${id}`)).toBeNull();
    }
  });

  it('does not carry one fetched cycle into the next pending historic cycle', async () => {
    let rejectOlder: ((error: Error) => void) | undefined;
    global.fetch = vi.fn((url: string) => url.includes('pre-reset')
      ? new Promise((_resolve, reject) => { rejectOlder = reject; })
      : Promise.resolve({ ok: true, json: async () => HISTORIC_CYCLE_PAYLOAD })) as unknown as typeof fetch;
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    expect(document.querySelector('#mcw-bignum')).toHaveTextContent('1.0%');

    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Loading…')).toBeTruthy();
    expect(document.querySelector('#mcw-bignum')).toBeNull();
    expect(document.querySelector('#mcw-ms-count')).toBeNull();

    await act(async () => { rejectOlder?.(new Error('unavailable')); });
    expect(await screen.findByText(/Couldn’t load this cycle/)).toBeTruthy();
    expect(document.querySelector('#mcw-bignum')).toBeNull();
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
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(screen.queryByLabelText('Share Current week report')).toBeNull());
    expect(screen.queryByLabelText('Share Current week report')).toBeNull();
  });

  it('shows a vanished state with return-to-current when the selected key disappears', async () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    // A later snapshot drops the selected cycle from the index.
    const shrunk = [idxEntry('milestone_cycle:current', { is_current: true })];
    act(() => updateSnapshot(makeEnv(shrunk, '2026-05-18T12:05:00Z')));
    await screen.findByText(/no longer available/i);
    fireEvent.click(screen.getByText('Back to current'));
    await waitFor(() => expect(screen.queryByText(/no longer available/i)).toBeNull());
    expect(screen.getByLabelText('Newer cycle')).toBeTruthy();
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
    fireEvent.click(screen.getByLabelText('Older block'));
    // One action both hydrates the cycle detail and applies the requested
    // direction from the active/default last block.
    await screen.findByText(/Block 1 of 2/);
    // … while the herobar stays envelope-driven.
    expect(document.querySelector('#mcw-bignum .int')?.textContent).toBe('42');
    expect(document.querySelector('#mcw-spent')?.textContent).toContain('3.00');
  });
});

describe('current-period block navigator is immediately complete', () => {
  it('shows the active Claude block position and both boundary controls before fetching detail', () => {
    const spy = mockFetch({});
    const env = makeEnv([
      idxEntry('milestone_cycle:current', {
        is_current: true,
        segment_count: 1,
        block_count: 2,
      }),
      INDEX[1],
    ]);
    env.current_week!.five_hour_block = {
      block_start_at: '2026-05-16T05:00:00Z',
      five_hour_window_key: 901,
      seven_day_pct_at_block_start: 19,
      seven_day_pct_delta_pp: 1,
      crossed_seven_day_reset: false,
      credits: [],
    };
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    expect(screen.getByText('Block 2 of 2')).toBeTruthy();
    expect((screen.getByLabelText('Older block') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText('Newer block') as HTMLButtonElement).disabled).toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('Claude account-owned five-hour navigation (#839)', () => {
  const currentEntry = idxEntry('milestone_cycle:current', {
    is_current: true, segment_count: 2, block_count: 2,
  });
  const makeBlock = (account: 'alice' | 'bob') => ({
    five_hour_window_key: 901,
    account_key: account,
    account_label: account === 'alice' ? 'Alice' : 'Bob',
    block_start_at: '2026-05-16T05:00:00Z',
    five_hour_resets_at: '2026-05-16T10:00:00Z',
    final_five_hour_percent: account === 'alice' ? 7 : 19,
    total_cost_usd: 1,
    crossed_seven_day_reset: false,
    is_closed: false,
    milestones: [{
      percent_threshold: account === 'alice' ? 7 : 19,
      reset_event_id: 0,
      captured_at_utc: '2026-05-16T06:00:00Z',
      block_cost_usd: 1,
      marginal_cost_usd: 1,
      seven_day_pct_at_crossing: 23,
      effective_seven_day_pct_at_crossing: 23,
    }],
    credits: [{
      effective_reset_at_utc: '2026-05-16T06:30:00Z',
      prior_percent: 1,
      post_percent: account === 'alice' ? 5 : 18,
      delta_pp: account === 'alice' ? 4 : 17,
    }],
  });

  // #836 (U1): a scoped detail's `blocks[]` holds only the displayed
  // account's blocks — its own earlier block in the cycle, never another
  // account's block in the same window.
  const makeOlderBlock = (account: 'alice' | 'bob') => ({
    ...makeBlock(account),
    five_hour_window_key: 900,
    block_start_at: '2026-05-16T00:00:00Z',
    five_hour_resets_at: '2026-05-16T05:00:00Z',
    is_closed: true,
    milestones: [{
      percent_threshold: 11,
      reset_event_id: 0,
      captured_at_utc: '2026-05-16T01:00:00Z',
      block_cost_usd: 1,
      marginal_cost_usd: 1,
      seven_day_pct_at_crossing: 12,
      effective_seven_day_pct_at_crossing: 12,
    }],
    credits: [{
      effective_reset_at_utc: '2026-05-16T02:00:00Z',
      prior_percent: 1,
      post_percent: 7,
      delta_pp: 6,
    }],
  });

  it('retains the live block and stream while decorated history loads or fails', async () => {
    let rejectDetail!: (error: Error) => void;
    global.fetch = vi.fn(() => new Promise<Response>((_resolve, reject) => {
      rejectDetail = reject;
    })) as unknown as typeof fetch;
    const env = makeEnv([idxEntry('milestone_cycle:current', {
      is_current: true, segment_count: 1, block_count: 2,
    })]);
    env.current_week!.five_hour_block = {
      five_hour_window_key: 901,
      account_key: 'bob',
      block_start_at: '2026-05-16T05:00:00Z',
      seven_day_pct_at_block_start: 20,
      seven_day_pct_delta_pp: 3,
      crossed_seven_day_reset: false,
      credits: makeBlock('bob').credits,
    };
    env.current_week!.five_hour_milestones = makeBlock('bob').milestones;
    env.current_week!.account_key = 'bob';
    env.current_week!.account_label = 'Bob';
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    // #836 §5.8: the current envelope's weekly rows stay visible while the
    // decorated cycle detail loads; only the block order waits for it.
    expect(document.querySelector('#mcw-table')?.textContent).toContain('1.00');
    expect(screen.queryByText('Loading…')).toBeNull();
    expect(document.querySelector('#mcw-blocknav')?.textContent).toContain('Current block');
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('+17pp');
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('19');
    await act(async () => rejectDetail(new Error('detail unavailable')));
    // Review F4: the weekly rows come from the envelope, so a failed eager
    // fetch neither hides them nor reports a cycle that did not fail to load.
    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    expect(document.querySelector('#mcw-table')?.textContent).toContain('1.00');
    expect(screen.queryByText(/Couldn’t load this cycle/)).toBeNull();
    expect(document.querySelector('#mcw-blocknav')?.textContent).toContain('Current block');
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('+17pp');
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('19');
  });

  // Review F4. Every decorated current cycle fetches its detail eagerly. A
  // failed fetch (a network error, or a one-tick 404 when a reset takes effect
  // between the envelope and the request) must not hide weekly rows that come
  // from the envelope; when the rows come from the detail the error stands.
  it.each([
    ['a network error', 1, false],
    ['a one-tick 404', 1, false],
    ['a network error', 2, true],
    ['a one-tick 404', 2, true],
  ] as const)(
    'a failed eager decorated detail (%s, %i segments) shows the error only when the rows come from it',
    async (failure, segments, expectError) => {
      global.fetch = vi.fn(async () => {
        if (failure === 'a network error') throw new Error('detail unavailable');
        return { ok: false, status: 404, json: async () => ({ error: 'unknown_key' }) };
      }) as unknown as typeof fetch;
      const env = makeEnv([idxEntry('milestone_cycle:current', {
        is_current: true, segment_count: segments, block_count: 1,
      })]);
      env.current_week!.account_key = 'bob';
      env.current_week!.account_label = 'Bob';
      updateSnapshot(env);
      render(<CurrentWeekModal />);
      await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
      // Let the failed request settle into the navigator's state.
      await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
      if (expectError) {
        await waitFor(() => expect(screen.getByText(/Couldn’t load this cycle/)).toBeTruthy());
        expect(document.querySelector('#mcw-table')).toBeNull();
      } else {
        await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull());
        expect(screen.queryByText(/Couldn’t load this cycle/)).toBeNull();
        expect(document.querySelector('#mcw-table')?.textContent).toContain('1.00');
      }
    },
  );

  it('fetches a decorated single-segment cycle for the displayed account before assuming the live block is last', async () => {
    const entry = idxEntry('milestone_cycle:current', {
      is_current: true, segment_count: 1, block_count: 2,
    });
    const fetchSpy = mockFetch({
      source: 'claude', key: entry.key, label: 'May 15–22',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: entry.detail_stamp,
      segments: [], dividers: [], blocks: [makeOlderBlock('bob'), makeBlock('bob')],
    });
    const env = makeEnv([entry]);
    env.current_week!.account_key = 'bob';
    env.current_week!.account_label = 'Bob';
    env.current_week!.five_hour_block = {
      five_hour_window_key: 901,
      account_key: 'bob',
      block_start_at: '2026-05-16T05:00:00Z',
      seven_day_pct_at_block_start: 20,
      seven_day_pct_delta_pp: 3,
      crossed_seven_day_reset: false,
      credits: makeBlock('bob').credits,
    };
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    await waitFor(() => expect(screen.getByText('Block 2 of 2')).toBeTruthy());
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith('/api/milestones/claude/week/milestone_cycle%3Acurrent?account=bob');
    expect((screen.getByLabelText('Older block') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText('Newer block') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('Older block'));
    await waitFor(() => expect(screen.getByText('Block 1 of 2')).toBeTruthy());
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('+6pp');
  });

  // #836 spec §3 "Consequence": under U1 the scoped detail holds only the
  // displayed account's blocks, so the navigator no longer steps into another
  // account's block in the same window (#839 had exercised that step). It
  // walks the account's own blocks, borrows the live stream only for the live
  // block, and renders every other block from its own rows.
  it('steps only the displayed account’s blocks from the scoped detail', async () => {
    const fetchSpy = mockFetch({
      source: 'claude', key: currentEntry.key, label: 'May 15–22',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: currentEntry.detail_stamp,
      segments: [], dividers: [], blocks: [makeOlderBlock('bob'), makeBlock('bob')],
    });
    const env = makeEnv([currentEntry]);
    env.current_week!.account_key = 'bob';
    env.current_week!.account_label = 'Bob';
    env.current_week!.five_hour_block = {
      five_hour_window_key: 901,
      account_key: 'bob',
      block_start_at: '2026-05-16T05:00:00Z',
      seven_day_pct_at_block_start: 20,
      seven_day_pct_delta_pp: 3,
      crossed_seven_day_reset: false,
      credits: makeBlock('bob').credits,
    };
    env.current_week!.five_hour_milestones = makeBlock('bob').milestones;
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    await waitFor(() => expect(screen.getByText('Block 2 of 2')).toBeTruthy());
    expect(fetchSpy).toHaveBeenCalledWith('/api/milestones/claude/week/milestone_cycle%3Acurrent?account=bob');
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('+17pp');
    fireEvent.click(screen.getByLabelText('Older block'));
    await waitFor(() => expect(screen.getByText('Block 1 of 2')).toBeTruthy());
    const olderTable = document.querySelector('#mcw-5h-table')!;
    expect(olderTable.textContent).toContain('+6pp');
    expect(olderTable.textContent).toContain('11');
    expect(olderTable.textContent).not.toContain('+17pp');
    expect((screen.getByLabelText('Older block') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText('Alice')).toBeNull();
  });

  it('does not substitute an unqualified legacy live stream into a qualified block', async () => {
    mockFetch({
      source: 'claude', key: currentEntry.key, label: 'May 15–22',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: currentEntry.detail_stamp,
      segments: [], dividers: [], blocks: [makeBlock('bob')],
    });
    const env = makeEnv([currentEntry]);
    env.current_week!.five_hour_block = {
      five_hour_window_key: 901,
      block_start_at: '2026-05-16T05:00:00Z',
      seven_day_pct_at_block_start: 20,
      seven_day_pct_delta_pp: 3,
      crossed_seven_day_reset: false,
      credits: [{ ...makeBlock('bob').credits[0], delta_pp: 88 }],
    };
    updateSnapshot(env);
    render(<CurrentWeekModal />);
    await waitFor(() => expect(screen.getByText('Bob')).toBeTruthy());
    expect(document.querySelector('#mcw-5h-table')?.textContent).toContain('+17pp');
    expect(document.querySelector('#mcw-5h-table')?.textContent).not.toContain('88pp');
  });
});

// #836 — on a decorated store (two or more real Claude accounts) the envelope
// names the displayed account (`current_week.account_key/account_label`), and
// the modal's navigator, every cycle detail and the weekly section belong to
// that account alone (U1, U2). Cycle keys and stamps can be identical across
// accounts — shared subscription-week boundaries — so nothing keyed on the
// cycle alone may carry one account's detail into the other's view.
describe('#836 — the decorated Claude navigator is the displayed account’s', () => {
  const ALICE = 'a'.repeat(32);
  const BOB = 'b'.repeat(32);
  const qualified = (key: string, account: string) =>
    `/api/milestones/claude/week/${encodeURIComponent(key)}?account=${account}`;

  function accountEnv(
    account: string | null,
    label: string | null,
    opts: { index?: WeekIndexEntry[]; liveBlock?: boolean; weeklyUsd?: number } = {},
  ): Envelope {
    const env = makeEnv(opts.index ?? INDEX);
    const cw = env.current_week!;
    if (account != null) cw.account_key = account;
    if (label != null) cw.account_label = label;
    const usd = opts.weeklyUsd ?? 1;
    cw.milestones = [
      { percent: 1, crossed_at_utc: '2026-05-16T10:00:00Z', cumulative_usd: usd, marginal_usd: usd, five_hour_pct_at_cross: null },
    ];
    if (opts.liveBlock ?? true) {
      cw.five_hour_block = {
        five_hour_window_key: 901,
        ...(account != null ? { account_key: account, account_label: label ?? undefined } : {}),
        block_start_at: '2026-05-16T05:00:00Z',
        seven_day_pct_at_block_start: 20,
        seven_day_pct_delta_pp: 3,
        crossed_seven_day_reset: false,
        credits: [],
      };
    }
    return env;
  }

  function cyclePayload(
    key: string,
    account: string,
    label: string,
    opts: { usd?: number; stamp?: string; effective?: number | null } = {},
  ) {
    const usd = opts.usd ?? 5;
    return {
      source: 'claude', key, label: 'Jul 16–Jul 18',
      start_at_utc: '2026-07-16T05:00:00Z', end_at_utc: '2026-07-18T05:00:00Z',
      is_current: key === 'milestone_cycle:current',
      detail_stamp: opts.stamp ?? `st-${key}`,
      segments: [{ key: `seg-${key}`, milestones: [
        { percent: 9, crossed_at_utc: '2026-07-16T10:00:00Z', cumulative_usd: usd, marginal_usd: usd, five_hour_pct_at_cross: null },
      ] }],
      dividers: [],
      blocks: [{
        five_hour_window_key: 901,
        account_key: account,
        account_label: label,
        block_start_at: '2026-05-16T05:00:00Z',
        five_hour_resets_at: '2026-05-16T10:00:00Z',
        final_five_hour_percent: 30,
        total_cost_usd: 2,
        crossed_seven_day_reset: false,
        is_closed: false,
        milestones: [{
          percent_threshold: 30,
          reset_event_id: 0,
          captured_at_utc: '2026-05-16T06:00:00Z',
          block_cost_usd: 2,
          marginal_cost_usd: 1,
          seven_day_pct_at_crossing: 42,
          effective_seven_day_pct_at_crossing: opts.effective === undefined ? 31 : opts.effective,
        }],
        credits: [],
      }],
    };
  }

  // Requests that settle only when the test says so, so a response can be made
  // to arrive after the account it was asked for is no longer displayed.
  function deferredFetch() {
    const pending: Array<{
      url: string;
      succeed: (payload: unknown) => void;
      fail: (status: number) => void;
    }> = [];
    const spy = vi.fn((url: string) => new Promise((resolve) => {
      pending.push({
        url,
        succeed: (payload) => resolve({ ok: true, json: async () => payload }),
        fail: (status) => resolve({ ok: false, status, json: async () => ({ code: 'unknown_key' }) }),
      });
    }));
    global.fetch = spy as unknown as typeof fetch;
    return { spy, pending };
  }

  const badgeLabels = () => Array.from(
    document.querySelectorAll('#mcw-badges .mcw-ms-sub'),
  ).map((n) => n.textContent);

  // U2: the existing display label beside the cycle pill, in the block row's
  // quiet sub-text style — with or without a live block, and on the no-index
  // pill path; absent on an undecorated payload.
  for (const c of [
    { name: 'with a live block', liveBlock: true, index: INDEX },
    { name: 'without a live block', liveBlock: false, index: INDEX },
    { name: 'on the no-index pill path', liveBlock: false, index: [] as WeekIndexEntry[] },
  ]) {
    it(`names the displayed account beside the cycle pill ${c.name}`, () => {
      mockFetch(cyclePayload('milestone_cycle:current', ALICE, 'Alice'));
      updateSnapshot(accountEnv(ALICE, 'Alice', { liveBlock: c.liveBlock, index: c.index }));
      render(<CurrentWeekModal />);
      expect(document.querySelector('#mcw-week-pill')).not.toBeNull();
      expect(badgeLabels()).toEqual(['Alice']);
    });
  }

  it('adds no account label on an undecorated payload', () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(accountEnv(null, null));
    render(<CurrentWeekModal />);
    expect(badgeLabels()).toEqual([]);
  });

  it('walks older and newer through the displayed account’s cycles only', async () => {
    const spy = vi.fn(async (url: string) => {
      const key = decodeURIComponent(url.split('/week/')[1].split('?')[0]);
      return { ok: true, json: async () => cyclePayload(key, ALICE, 'Alice', { usd: key.includes('pre') ? 7.77 : 5.55 }) };
    });
    global.fetch = spy as unknown as typeof fetch;
    updateSnapshot(accountEnv(ALICE, 'Alice'));
    render(<CurrentWeekModal />);
    await waitFor(() => expect(spy).toHaveBeenCalledWith(qualified('milestone_cycle:current', ALICE)));

    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(document.querySelector('#mcw-table')?.textContent).toContain('5.55'));
    expect(spy).toHaveBeenLastCalledWith(qualified('milestone_cycle:post-reset', ALICE));
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(document.querySelector('#mcw-table')?.textContent).toContain('7.77'));
    expect(spy).toHaveBeenLastCalledWith(qualified('milestone_cycle:pre-reset', ALICE));
    fireEvent.click(screen.getByLabelText('Newer cycle'));
    await waitFor(() => expect(document.querySelector('#mcw-table')?.textContent).toContain('5.55'));
    for (const [url] of spy.mock.calls) expect(url).toMatch(new RegExp(`\\?account=${ALICE}$`));
    expect(badgeLabels()).toEqual(['Alice']);
  });

  it('renders a current observation-gap cycle from the displayed account’s detail', async () => {
    const spy = mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      key: 'milestone_cycle:current',
      is_current: true,
      detail_stamp: 'st-milestone_cycle:current',
    });
    updateSnapshot(accountEnv(BOB, 'Bob', {
      liveBlock: false,
      index: [
        idxEntry('milestone_cycle:current', { is_current: true, segment_count: 1, has_observation_gap: true }),
        INDEX[1],
      ],
    }));
    render(<CurrentWeekModal />);
    await waitFor(() => expect(document.getElementById('mcw-gap-notes')).not.toBeNull());
    expect(spy).toHaveBeenCalledWith(qualified('milestone_cycle:current', BOB));
    const cells = Array.from(document.querySelectorAll('#mcw-table .m-marginal'))
      .map((el) => (el.textContent ?? '').trim());
    expect(cells.filter((cell) => cell === 'observation gap')).toHaveLength(3);
  });

  it('shows the new account’s current cycle on the first render after an account flip', async () => {
    const { spy, pending } = deferredFetch();
    updateSnapshot(accountEnv(ALICE, 'Alice', { weeklyUsd: 1.11 }));
    const commits: string[] = [];
    render(
      <Profiler id="cw" onRender={() => { commits.push(document.body.textContent ?? ''); }}>
        <CurrentWeekModal />
      </Profiler>,
    );
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () => pending[0].succeed(cyclePayload('milestone_cycle:current', ALICE, 'Alice')));
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[1].url).toBe(qualified('milestone_cycle:post-reset', ALICE));
    await act(async () => pending[1].succeed(cyclePayload('milestone_cycle:post-reset', ALICE, 'Alice', { usd: 9.99 })));
    await waitFor(() => expect(document.querySelector('#mcw-table')?.textContent).toContain('9.99'));

    // Bob's index carries the very same keys and stamps as Alice's.
    commits.length = 0;
    act(() => { updateSnapshot(accountEnv(BOB, 'Bob', { weeklyUsd: 2.22 })); });
    expect(commits.length).toBeGreaterThan(0);
    for (const text of commits) {
      expect(text).toContain('Current Cycle — per-percent milestones');
      expect(text).not.toContain('9.99');
      expect(text).not.toContain('Alice');
      expect(text).not.toContain('Couldn’t load this cycle');
    }
    // Bob's envelope rows are visible while his own detail loads.
    expect(document.querySelector('#mcw-table')?.textContent).toContain('2.22');
    expect(badgeLabels()).toEqual(['Bob']);
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(qualified('milestone_cycle:current', BOB)));
  });

  it('keeps the selection when only the account label changes', async () => {
    const spy = vi.fn(async (url: string) => {
      const key = decodeURIComponent(url.split('/week/')[1].split('?')[0]);
      return { ok: true, json: async () => cyclePayload(key, ALICE, 'Alice', { usd: 5.55 }) };
    });
    global.fetch = spy as unknown as typeof fetch;
    updateSnapshot(accountEnv(ALICE, 'Alice'));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(document.querySelector('#mcw-table')?.textContent).toContain('5.55'));
    const calls = spy.mock.calls.length;
    act(() => { updateSnapshot(accountEnv(ALICE, 'Alice (work)')); });
    expect(document.querySelector('#mcw-table')?.textContent).toContain('5.55');
    expect(document.body.textContent).toContain('Cycle — per-percent milestones');
    expect(document.body.textContent).not.toContain('Current Cycle — per-percent milestones');
    expect(badgeLabels()).toEqual(['Alice (work)']);
    expect(spy.mock.calls.length).toBe(calls);
  });

  for (const outcome of ['success', 'error'] as const) {
    it(`ignores a late ${outcome} for the account that is no longer displayed`, async () => {
      const { pending } = deferredFetch();
      updateSnapshot(accountEnv(ALICE, 'Alice', { weeklyUsd: 1.11 }));
      render(<CurrentWeekModal />);
      await waitFor(() => expect(pending).toHaveLength(1));
      expect(pending[0].url).toBe(qualified('milestone_cycle:current', ALICE));

      act(() => { updateSnapshot(accountEnv(BOB, 'Bob', { weeklyUsd: 2.22 })); });
      await act(async () => {
        if (outcome === 'success') pending[0].succeed(cyclePayload('milestone_cycle:current', ALICE, 'Alice'));
        else pending[0].fail(404);
      });
      expect(screen.queryByText('Alice')).toBeNull();
      expect(screen.queryByText(/Couldn’t load this cycle/)).toBeNull();
      expect(document.querySelector('#mcw-blocknav')?.textContent).toContain('Current block');
      expect(document.querySelector('#mcw-table')?.textContent).toContain('2.22');

      await waitFor(() => expect(pending).toHaveLength(2));
      expect(pending[1].url).toBe(qualified('milestone_cycle:current', BOB));
      await act(async () => pending[1].succeed(cyclePayload('milestone_cycle:current', BOB, 'Bob')));
      await waitFor(() => expect(screen.getByText('Block 1 of 1')).toBeTruthy());
      expect(document.querySelector('#mcw-blocknav')?.textContent).toContain('Bob');
      expect(screen.queryByText('Alice')).toBeNull();
    });
  }

  // S1 (#834) stays effective-only on the scoped detail, and a stamp change
  // refetches: a crossing whose weekly join became unavailable renders `—`,
  // never the earlier effective value and never the raw one.
  it('refetches on a stamp change and renders — for a now-unavailable crossing', async () => {
    let effective: number | null = 31;
    const spy = vi.fn(async (url: string) => {
      const key = decodeURIComponent(url.split('/week/')[1].split('?')[0]);
      return { ok: true, json: async () => cyclePayload(key, ALICE, 'Alice', { effective }) };
    });
    global.fetch = spy as unknown as typeof fetch;
    updateSnapshot(accountEnv(ALICE, 'Alice'));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await waitFor(() => expect(document.querySelector('#mcw-5h-table')).not.toBeNull());
    const cellsOf = () => Array.from(document.querySelectorAll('#mcw-5h-table .m-fh')).map((n) => n.textContent);
    expect(cellsOf()).toEqual(['31%']);
    expect(document.querySelector('#mcw-5h-table')?.textContent).not.toContain('42%');

    effective = null;
    const restamped = INDEX.map((e) => e.key === 'milestone_cycle:post-reset' ? { ...e, detail_stamp: 'st-moved' } : e);
    act(() => { updateSnapshot(accountEnv(ALICE, 'Alice', { index: restamped })); });
    await waitFor(() => expect(cellsOf()).toEqual(['—']));
    expect(document.querySelector('#mcw-5h-table')?.textContent).not.toContain('42%');
    expect(spy).toHaveBeenLastCalledWith(qualified('milestone_cycle:post-reset', ALICE));
  });
});

// P1 (spec §4) — the 5h block navigator (heading + BlockNavHeader) must stay
// mounted whenever the selected week's effective block list has ≥1 block; only
// the milestone TABLE varies. A selected block whose stream is empty (e.g. a
// cross-reset straddler with no integer-percent crossing) renders a compact
// empty-state line in place of the table, and the ⚡ position marker stays
// visible. Previously the outer guard gated the whole section on
// `selectedBlockStream.length > 0 || currentHasBlocks`, which unmounted the
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
    fireEvent.click(screen.getByLabelText('Older cycle'));
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
    // The complete navigator is present from the compact current-week facts;
    // its first older step lazily fetches detail AND moves one position from
    // the active/default last block.
    fireEvent.click(screen.getByLabelText('Older block'));
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

  it('keeps the current block navigator mounted while lazy detail is loading', async () => {
    let resolveDetail!: (value: unknown) => void;
    const pendingDetail = new Promise<unknown>((resolve) => { resolveDetail = resolve; });
    global.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => pendingDetail,
    })) as unknown as typeof fetch;

    const env = makeEnv([
      idxEntry('milestone_cycle:current', {
        is_current: true,
        segment_count: 1,
        block_count: 2,
      }),
      INDEX[1],
    ]);
    env.current_week!.five_hour_block = {
      block_start_at: '2026-05-16T05:00:00Z',
      five_hour_window_key: 901,
      seven_day_pct_at_block_start: 40,
      seven_day_pct_delta_pp: 2,
      crossed_seven_day_reset: false,
      credits: [],
    };
    env.current_week!.five_hour_milestones = [{
      percent_threshold: 20,
      reset_event_id: 0,
      captured_at_utc: '2026-05-16T06:00:00Z',
      block_cost_usd: 1,
      marginal_cost_usd: 1,
      seven_day_pct_at_crossing: 42,
    }];
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    expect(screen.getByText(/Block 2 of 2/)).toBeTruthy();
    expect(document.querySelector('#mcw-5h-table')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Older block'));

    expect(screen.getByText(/Block 2 of 2/)).toBeTruthy();
    expect(screen.getByLabelText('Older block')).toBeTruthy();
    expect(screen.getByRole('status')).toHaveTextContent('Loading block detail…');
    expect(document.querySelector('#mcw-5h-table')).toBeNull();

    resolveDetail({
      source: 'claude', key: 'milestone_cycle:current', label: 'Jul 18–Jul 25',
      start_at_utc: '2026-05-15T00:00:00Z', end_at_utc: '2026-05-22T00:00:00Z',
      is_current: true, detail_stamp: 'st-milestone_cycle:current',
      segments: [{ key: 'milestone_segment:current', milestones: [] }],
      dividers: [],
      blocks: [
        { five_hour_window_key: 900, block_start_at: '2026-05-16T00:00:00Z', five_hour_resets_at: '2026-05-16T05:00:00Z', final_five_hour_percent: 10, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: true, milestones: [], credits: [] },
        { five_hour_window_key: 901, block_start_at: '2026-05-16T05:00:00Z', five_hour_resets_at: '2026-05-16T10:00:00Z', final_five_hour_percent: 20, total_cost_usd: 1, crossed_seven_day_reset: false, is_closed: false, milestones: [], credits: [] },
      ],
    });
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

  it('keeps the compact navigator mounted while lazy block detail is loading', async () => {
    let resolveDetail!: (value: unknown) => void;
    const pendingDetail = new Promise<unknown>((resolve) => { resolveDetail = resolve; });
    global.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => pendingDetail,
    })) as unknown as typeof fetch;
    openCodex();
    updateSnapshot(codexEnvWithIndex(CODEX_IDX));
    render(<CurrentWeekModal />);

    fireEvent.click(screen.getByLabelText('Older block'));

    expect(screen.getByText(/Block 3 of 3/)).toBeTruthy();
    expect(screen.getByLabelText('Older block')).toBeTruthy();
    expect(screen.getByRole('status')).toHaveTextContent('Loading block detail…');

    resolveDetail({
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
    });
    await screen.findByText(/Block 2 of 3/);
  });

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
    expect(screen.getByText('Block 3 of 3')).toBeTruthy();
    expect((screen.getByLabelText('Newer block') as HTMLButtonElement).disabled).toBe(true);
    expect(spy).not.toHaveBeenCalled();
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
    fireEvent.click(screen.getByLabelText('Older cycle'));
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

    fireEvent.click(within(claude).getByLabelText('Older cycle'));
    await within(claude).findByText('Jul 16–Jul 18');
    expect(within(codex).queryByText('Cyc prev')).toBeNull();

    fireEvent.click(within(codex).getByLabelText('Older cycle'));
    await within(codex).findByText('Apr 16–Apr 23');
    expect(within(claude).getByText('Jul 16–Jul 18')).toBeTruthy();
  });
});

describe('current-cycle range and reset semantics (#412 Task B)', () => {
  it('renders a complete Claude week label exactly once', () => {
    const env = makeEnv(INDEX);
    env.header.week_label = 'May 15–22';
    env.current_week!.reset_at_utc = '2026-05-22T00:00:00Z';
    updateSnapshot(env);

    render(<CurrentWeekModal />);

    expect(document.querySelector('#mcw-week-pill')).toHaveTextContent(/^May 15–22$/);
    expect(document.querySelector('#mcw-week-pill')).not.toHaveTextContent('→ May 22');
  });

  it('labels the reset-derived Claude fallback when the week label is absent', () => {
    const env = makeEnv(INDEX);
    env.header.week_label = null;
    env.current_week!.reset_at_utc = '2026-05-22T00:00:00Z';
    updateSnapshot(env);

    render(<CurrentWeekModal />);

    expect(document.querySelector('#mcw-week-pill')).toHaveTextContent(/^Resets May 22$/);
  });

  it('keeps the ordinary reset label when a Codex historic range ends at its nominal reset', async () => {
    const equal = {
      ...CODEX_IDX[1],
      start_at_utc: '2026-04-16T00:00:00Z',
      end_at_utc: '2026-04-23T00:00:00Z',
      resets_at_utc: '2026-04-23T00:00:00Z',
    };
    mockFetch({
      source: 'codex', ...equal, segments: [{ key: 'equal', milestones: [] }],
      dividers: [], blocks: [],
    });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(codexEnvWithIndex([CODEX_IDX[0], equal]));

    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));

    await screen.findByText('Apr 16–Apr 23');
    const mini = document.querySelector<HTMLElement>('#mcw-mini')!;
    expect(within(mini).getByText(/^reset$/i)).toBeTruthy();
    expect(within(mini).queryByText(/nominal reset/i)).toBeNull();
  });

  it('labels a later nominal reset without rewriting the effective clipped Codex range', async () => {
    const clipped = {
      ...CODEX_IDX[1],
      start_at_utc: '2026-07-13T00:00:00Z',
      end_at_utc: '2026-07-20T00:00:00Z',
      resets_at_utc: '2026-07-27T00:00:00Z',
      label: 'Jul 13–20',
    };
    mockFetch({
      source: 'codex', ...clipped, segments: [{ key: 'clipped', milestones: [] }],
      dividers: [], blocks: [],
    });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(codexEnvWithIndex([CODEX_IDX[0], clipped]));

    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));

    await screen.findByText('Jul 13–Jul 20');
    expect(document.querySelector('#mcw-week-pill')).toHaveTextContent(/^Jul 13–Jul 20$/);
    const mini = document.querySelector<HTMLElement>('#mcw-mini')!;
    expect(within(mini).getByText(/nominal reset/i)).toBeTruthy();
    expect(mini).toHaveTextContent('Jul 27');
  });
});

// #556 S4 F6, revised by #750 S4 — `WeekNavChip` is shared by both providers.
// It once hard-coded `Older week` / `Newer week`, which named a Claude concept
// on the Codex section. #556 S4 gave Codex its own noun; #750 S4 removes the
// remaining divergence, because Claude steps cycles too: its index comes from
// `build_claude_week_index`, one entry per effective reset-defined cycle, so
// on a credited week two adjacent steps stay inside one subscription week.
// Both sections must now name the same unit their visible labels do.
//
// This state is unreachable on a store with more than one Codex account: the
// Codex call site is gated behind `perAccountCycles === false`, and a decorated
// provider takes the "All accounts" shell, which contains no navigator at all.
// The fixture is therefore deliberately undecorated — it omits
// `sources.codex.data.accounts` rather than supplying an empty array, leaves
// `accountKey` unset, and carries two cycle-index entries so both directions
// and both enabled states exist. No realistic browser pass can reach this, so
// it is verified here and is deliberately absent from the acceptance criteria.
describe('provider-specific navigator vocabulary (#556 S4)', () => {
  it('names both providers\' navigators in cycle vocabulary', () => {
    const env = codexEnvWithIndex(CODEX_IDX);
    env.current_week = makeEnv(INDEX).current_week;
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(env);
    const { container } = render(<CurrentWeekModal />);

    const codex = container.querySelector<HTMLElement>('[data-provider-section="codex"]')!;
    // Non-vacuity: the undecorated branch really renders a navigator. If the
    // fixture were decorated this element would be absent and every assertion
    // below would pass or fail for the wrong reason.
    expect(codex.querySelector('.mcw-weeknav')).not.toBeNull();
    expect(within(codex).getByLabelText('Older cycle')).toBeInTheDocument();
    expect(within(codex).getByLabelText('Newer cycle')).toBeInTheDocument();
    expect(within(codex).queryByLabelText('Older week')).toBeNull();
    expect(within(codex).queryByLabelText('Newer week')).toBeNull();

    const claude = container.querySelector<HTMLElement>('[data-provider-section="claude"]')!;
    expect(claude.querySelector('.mcw-weeknav')).not.toBeNull();
    expect(within(claude).getByLabelText('Older cycle')).toBeInTheDocument();
    expect(within(claude).getByLabelText('Newer cycle')).toBeInTheDocument();
    expect(within(claude).queryByLabelText('Older week')).toBeNull();
    expect(within(claude).queryByLabelText('Newer week')).toBeNull();
  });
});

// #620 S1 — the boundary-less week row.
//
// An install carrying a `week_start_at IS NULL` milestone with no matching
// snapshot row produces an index entry whose `start_at_utc` and `end_at_utc`
// are both null. Block I2 fixed the ValueError that had been emptying the whole
// index for those installs, so the row is now REACHABLE — and its three client
// consumers (`formatCycleRange`, `hasDistinctNominalReset`, `fmtDateShort`) were
// null-tolerant only INCIDENTALLY, because `null` happens to be the default in
// this file's own `idxEntry` helper. These tests select the row and assert what
// it renders, which incidental coverage never did.
const BOUNDARYLESS_PAYLOAD = {
  source: 'claude',
  key: 'milestone_cycle:no-bounds',
  // `_week_label` falls back to `ref.week_start.strftime("%b %d")` for a
  // boundary-less ref, so the label is one bare date and not a range.
  label: 'Mar 01',
  start_at_utc: null,
  end_at_utc: null,
  is_current: false,
  detail_stamp: 'st-milestone_cycle:no-bounds',
  segments: [
    {
      key: 'milestone_segment:no-bounds',
      milestones: [{
        percent: 3,
        crossed_at_utc: '2026-03-01T10:00:00Z',
        cumulative_usd: 2,
        marginal_usd: 2,
        five_hour_pct_at_cross: null,
      }],
    },
  ],
  dividers: [],
  blocks: [],
};

const BOUNDARYLESS_INDEX: WeekIndexEntry[] = [
  idxEntry('milestone_cycle:current', {
    is_current: true,
    label: 'Jul 18–Jul 25',
    start_at_utc: '2026-07-18T05:00:00Z',
    end_at_utc: '2026-07-25T05:00:00Z',
  }),
  idxEntry('milestone_cycle:no-bounds', {
    start_at_utc: null,
    end_at_utc: null,
    resets_at_utc: null,
    label: 'Mar 01',
    block_count: 0,
    milestone_count: 1,
    segment_count: 1,
  }),
];

describe('#620 S1 — a week with no recorded boundaries', () => {
  it('is reachable in the index and selectable', async () => {
    mockFetch(BOUNDARYLESS_PAYLOAD);
    updateSnapshot(makeEnv(BOUNDARYLESS_INDEX));
    render(<CurrentWeekModal />);
    // Precondition asserted unconditionally: the current week DOES carry
    // bounds, so what follows is about this row and not about an index whose
    // every row is boundary-less.
    expect(BOUNDARYLESS_INDEX[0].start_at_utc).not.toBeNull();
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Mar 01');
  });

  it('renders its label rather than an empty range, and says the bounds are missing', async () => {
    mockFetch(BOUNDARYLESS_PAYLOAD);
    updateSnapshot(makeEnv(BOUNDARYLESS_INDEX));
    const { container } = render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Mar 01');

    // The pill falls back to the bare label — no `–` range, and certainly no
    // "Invalid Date".
    expect(container.textContent).not.toContain('Invalid');
    expect(container.textContent).not.toContain('NaN');

    // A dash where the reset should be states nothing. The row says why the
    // range and the reset are absent, in the same voice the rest of #620 uses.
    const note = container.querySelector('.mcw-bounds-missing');
    expect(note).not.toBeNull();
    expect(note).toHaveTextContent('This cycle’s exact start and reset were never recorded');
    expect((note?.textContent ?? '').toLowerCase()).toContain('reset');

    // `.mcw-mini` is a wrapping flex row of stat cells, so a paragraph placed
    // inside it becomes a flex ITEM: it sits beside the reset cell at wide
    // widths and takes `gap: 18px` on top of its own `margin-top`. The note is
    // a block sibling under the row instead, which is what the governing CSS
    // comment ("under the mini bar") describes.
    const mini = document.getElementById('mcw-mini');
    expect(mini).not.toBeNull();
    expect(mini!.contains(note!)).toBe(false);
  });

  it('still renders the milestones it does have', async () => {
    mockFetch(BOUNDARYLESS_PAYLOAD);
    updateSnapshot(makeEnv(BOUNDARYLESS_INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Mar 01');
    // The week has one 3% milestone; a missing boundary must not hide it.
    // `splitBigNum` renders the hero as two spans, so match the element rather
    // than a single text node.
    expect(document.getElementById('mcw-bignum')?.textContent).toBe('3.0%');
    expect(document.getElementById('mcw-ms-count')?.textContent).toContain('1 crossed');
  });

  it('places the note outside the stat row on the Codex leg too', async () => {
    const boundaryless = {
      ...CODEX_IDX[1],
      start_at_utc: null,
      end_at_utc: null,
      resets_at_utc: null,
      label: 'Cyc no-bounds',
    };
    mockFetch({
      source: 'codex',
      ...boundaryless,
      segments: [{ key: 'nb', milestones: [] }],
      dividers: [],
      blocks: [],
    });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    updateSnapshot(codexEnvWithIndex([CODEX_IDX[0], boundaryless]));

    const { container } = render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Cyc no-bounds');

    const note = container.querySelector('.mcw-bounds-missing');
    expect(note).not.toBeNull();
    // Same placement rule as the Claude leg above, and for the same reason:
    // `.mcw-mini` is a wrapping flex row, so a child paragraph is laid out as
    // a flex item beside the stat cells rather than as a line beneath them.
    const mini = document.getElementById('mcw-mini');
    expect(mini).not.toBeNull();
    expect(mini!.contains(note!)).toBe(false);
  });

  it('a boundary-carrying week shows no such note', async () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    const { container } = render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    expect(container.querySelector('.mcw-bounds-missing')).toBeNull();
  });
});

// #750 S4 §5. The milestone modal must name an observation gap rather than
// render a bare em dash for a marginal the CLI names, and state each run once
// above the table.
const GAP_RUN = {
  first_percent: 12,
  last_percent: 15,
  observed_at_utc: '2026-07-17T09:00:00Z',
  previous_crossed_at_utc: '2026-07-16T10:00:00Z',
};

function gapMilestones() {
  return [
    { percent: 11, crossed_at_utc: '2026-07-16T10:00:00Z', cumulative_usd: 1, marginal_usd: 1, five_hour_pct_at_cross: null },
    { percent: 12, crossed_at_utc: '2026-07-17T09:00:00Z', cumulative_usd: 4, marginal_usd: 3, five_hour_pct_at_cross: null },
    { percent: 13, crossed_at_utc: '2026-07-17T09:00:00Z', cumulative_usd: 4, marginal_usd: null, five_hour_pct_at_cross: null, marginal_usd_withheld_cause: 'observation_gap' },
    { percent: 14, crossed_at_utc: '2026-07-17T09:00:00Z', cumulative_usd: 4, marginal_usd: null, five_hour_pct_at_cross: null, marginal_usd_withheld_cause: 'observation_gap' },
    { percent: 15, crossed_at_utc: '2026-07-17T09:00:00Z', cumulative_usd: 4, marginal_usd: null, five_hour_pct_at_cross: null, marginal_usd_withheld_cause: 'observation_gap' },
  ];
}

const HISTORIC_GAP_PAYLOAD = {
  ...HISTORIC_CYCLE_PAYLOAD,
  segments: [{ key: 'milestone_segment:post-reset', milestones: gapMilestones() }],
  observation_gap_runs: [GAP_RUN],
};

describe('#750 S4 §5 — the observation-gap disclosure', () => {
  it('names the cause on the affected rows instead of a bare em dash', async () => {
    mockFetch(HISTORIC_GAP_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const cells = Array.from(document.querySelectorAll('#mcw-table .m-marginal'))
      .map((el) => (el.textContent ?? '').trim());
    // Rows 13/14/15 withhold; 11 and 12 carry a real figure.
    expect(cells.filter((c) => c === 'observation gap')).toHaveLength(3);
    expect(cells).not.toContain('—');
  });

  it('states each run once above the table', async () => {
    mockFetch(HISTORIC_GAP_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes).not.toBeNull();
    expect(notes?.querySelectorAll('li')).toHaveLength(1);
    expect(notes?.textContent).toMatch(/12%-15% were all recorded from one observation/);
    expect(notes?.textContent).toMatch(/not separable/);
    // The CLI's trailing `(observation_gap)` token is NOT repeated here: the
    // cell reads the cause in words, so the note does not need the code.
    expect(notes?.textContent).not.toContain('(observation_gap)');
  });

  it('names the span since the previous crossing, as the CLI does', async () => {
    // §5.2 requires the rendered sentence to match the CLI's except for the
    // trailing `(observation_gap)` token. `previous_crossed_at_utc` is what
    // that clause is composed from, and the field shipped unread.
    mockFetch(HISTORIC_GAP_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes?.textContent).toContain('23.0 hours after the previous crossing');
  });

  it('drops the clause for a run that opens the ladder', async () => {
    mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      observation_gap_runs: [{ ...GAP_RUN, previous_crossed_at_utc: null }],
    });
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes?.textContent).toMatch(/12%-15% were all recorded from one observation/);
    expect(notes?.textContent).not.toContain('after the previous crossing');
  });

  // The CLI renders this span with Python's `f"{x:.1f}"`, which breaks a tie to
  // EVEN. `Number.toFixed(1)` breaks it upward, so before the fix this exact
  // 4500-second gap read "1.3 hours" here and "1.2 hours" in the CLI. Whole
  // seconds are what `captured_at_utc` carries, so the tie is reachable.
  it('breaks a rounding tie the way Python does', async () => {
    mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      observation_gap_runs: [{
        ...GAP_RUN,
        // 4500 seconds = 1.25 hours exactly.
        previous_crossed_at_utc: '2026-07-17T07:45:00Z',
        observed_at_utc: '2026-07-17T09:00:00Z',
      }],
    });
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes?.textContent).toContain('1.2 hours after the previous crossing');
    expect(notes?.textContent).not.toContain('1.3 hours');
  });

  // The other side of the same tie: 6300 seconds is 1.75 hours, where rounding
  // to even and rounding upward agree. Without it a helper that always rounded
  // DOWN at a tie would pass the case above.
  it('rounds a tie upward when the even neighbour is the higher one', async () => {
    mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      observation_gap_runs: [{
        ...GAP_RUN,
        previous_crossed_at_utc: '2026-07-17T07:15:00Z',
        observed_at_utc: '2026-07-17T09:00:00Z',
      }],
    });
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes?.textContent).toContain('1.8 hours after the previous crossing');
  });

  // A near-miss must NOT be treated as a tie: 3780 seconds is 1.05 hours, whose
  // nearest double sits just above the midpoint, and Python renders it "1.1".
  // A tie test written on the decimal literal rather than on the double would
  // round it to "1.0" here.
  it('leaves a near-midpoint span to ordinary rounding', async () => {
    mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      observation_gap_runs: [{
        ...GAP_RUN,
        previous_crossed_at_utc: '2026-07-17T07:57:00Z',
        observed_at_utc: '2026-07-17T09:00:00Z',
      }],
    });
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    const notes = document.getElementById('mcw-gap-notes');
    expect(notes?.textContent).toContain('1.1 hours after the previous crossing');
  });

  it('renders nothing when the payload carries no runs', async () => {
    mockFetch(HISTORIC_CYCLE_PAYLOAD);
    updateSnapshot(makeEnv(INDEX));
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText('Jul 16–Jul 18');
    expect(document.getElementById('mcw-gap-notes')).toBeNull();
  });

  // §5.3's probe, written BEFORE deciding whether the `has_observation_gap`
  // hint is needed. A CURRENT single-segment cycle neither fetches its detail
  // (`shouldFetch`) nor uses one that arrives (`useDetail`), so a gap in the
  // live cycle would stay invisible. If this passes, the hint must not be
  // written.
  it('renders the disclosure for a CURRENT single-segment cycle', async () => {
    mockFetch({
      ...HISTORIC_GAP_PAYLOAD,
      key: 'milestone_cycle:current',
      is_current: true,
      detail_stamp: 'st-milestone_cycle:current',
    });
    updateSnapshot(makeEnv([
      idxEntry('milestone_cycle:current', {
        is_current: true, label: 'Jul 18–Jul 25', segment_count: 1,
        has_observation_gap: true,
      }),
      INDEX[1],
    ]));
    render(<CurrentWeekModal />);
    await waitFor(() => {
      expect(document.getElementById('mcw-gap-notes')).not.toBeNull();
    });
  });
  // ── #834 S1 (#836): render the EFFECTIVE weekly value at a crossing ────

  it('renders the effective weekly value at a crossing, and the unavailable marker when the joined snapshot row is gone', () => {
    // `seven_day_pct_at_crossing` is the RAW reading a weekly-clamped tick
    // stored, which no reader ever saw. The modal must render
    // `effective_seven_day_pct_at_crossing` instead, and an em-dash when that is
    // null — never a fallback to the raw value, which would put the wrong number
    // on screen exactly in the case nobody checks.
    const env = makeEnv([idxEntry('milestone_cycle:current', { is_current: true })]);
    env.current_week!.five_hour_block = {
      block_start_at: '2026-05-16T05:00:00Z',
      five_hour_window_key: 901,
      seven_day_pct_at_block_start: 40,
      seven_day_pct_delta_pp: 2,
      crossed_seven_day_reset: false,
      credits: [],
    };
    env.current_week!.five_hour_milestones = [
      {
        percent_threshold: 20,
        reset_event_id: 0,
        captured_at_utc: '2026-05-16T06:00:00Z',
        block_cost_usd: 1,
        marginal_cost_usd: 1,
        seven_day_pct_at_crossing: 50,
        effective_seven_day_pct_at_crossing: 63,
      },
      {
        percent_threshold: 30,
        reset_event_id: 0,
        captured_at_utc: '2026-05-16T07:00:00Z',
        block_cost_usd: 2,
        marginal_cost_usd: 1,
        seven_day_pct_at_crossing: 50,
        effective_seven_day_pct_at_crossing: null,
      },
    ];
    updateSnapshot(env);
    render(<CurrentWeekModal />);

    const table = document.querySelector('#mcw-5h-table');
    expect(table).toBeTruthy();
    const cells = Array.from(table!.querySelectorAll('.m-fh')).map((n) => n.textContent);
    expect(cells).toEqual(['63%', '\u2014']);
    expect(table!.textContent).not.toContain('50%');
  });
});
