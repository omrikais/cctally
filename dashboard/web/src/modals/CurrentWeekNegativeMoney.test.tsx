// #886 S1 — a negative milestone amount uses the dashboard's accounting form
// (`−$71.21`, U+2212 before the currency sign), never `$-71.21`. Negative
// marginals are legitimate after a rederive (#875 spec §7.2–§7.3).
//
// Every path that fills the two milestone tables is covered (spec R1/R2): the
// live envelope stream, a fetched historic cycle, current-cycle block
// navigation out and back (where the active block must render the LIVE
// overlay, not its fetched rows), and the embedded All-providers variant.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { CurrentWeekModal } from './CurrentWeekModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { uninstallGlobalKeydown } from '../store/keymap';
import { clearMilestoneHistoryCacheForTests } from './milestoneHistory';
import { fmt } from '../lib/fmt';
import allFixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope, WeekIndexEntry } from '../types/envelope';

// The `$-` form this issue removes: a currency sign followed by a hyphen and a
// digit.
const DOLLAR_HYPHEN = /\$-\d/;

const CURRENT_ENTRY: WeekIndexEntry = {
  key: 'milestone_cycle:current', start_at_utc: null, end_at_utc: null,
  label: 'Sep 1–8', is_current: true, milestone_count: 2, block_count: 1,
  segment_count: 1, detail_stamp: 'st-current',
};

const HISTORIC_ENTRY: WeekIndexEntry = {
  key: 'milestone_cycle:historic', start_at_utc: '2026-08-25T00:00:00Z',
  end_at_utc: '2026-09-01T00:00:00Z', label: 'Aug 25–Sep 1',
  is_current: false, milestone_count: 2, block_count: 1, segment_count: 1,
  detail_stamp: 'st-historic',
};

function negativeEnv(): Envelope {
  return {
    generated_at: '2026-09-05T12:00:00Z',
    header: { week_label: 'Sep 1–8' },
    current_week: {
      used_pct: 20,
      five_hour_pct: null,
      five_hour_resets_in_sec: null,
      spent_usd: 328.79,
      dollar_per_pct: 16.44,
      reset_at_utc: '2026-09-08T00:00:00Z',
      reset_in_sec: null,
      last_snapshot_age_sec: null,
      milestones: [
        { percent: 1, crossed_at_utc: '2026-09-02T10:00:00Z', cumulative_usd: 400, marginal_usd: null, five_hour_pct_at_cross: null },
        { percent: 2, crossed_at_utc: '2026-09-03T10:00:00Z', cumulative_usd: 328.79, marginal_usd: -71.21, five_hour_pct_at_cross: null },
      ],
      freshness: null,
      five_hour_block: {
        block_start_at: '2026-09-05T01:00:00Z',
        five_hour_window_key: 901,
        seven_day_pct_at_block_start: 18,
        seven_day_pct_delta_pp: 2,
        crossed_seven_day_reset: false,
        credits: [],
      },
      five_hour_milestones: [
        { percent_threshold: 15, reset_event_id: 0, captured_at_utc: '2026-09-05T01:42:00Z', block_cost_usd: 395.05, marginal_cost_usd: 10, seven_day_pct_at_crossing: 18 },
        { percent_threshold: 16, reset_event_id: 0, captured_at_utc: '2026-09-05T02:01:00Z', block_cost_usd: 323.84, marginal_cost_usd: -81.71, seven_day_pct_at_crossing: 19 },
      ],
      week_index: [CURRENT_ENTRY],
    },
  } as unknown as Envelope;
}

// A five-hour milestone row as both the envelope and the week detail carry it.
function fhMilestone(threshold: number, blockCost: number, marginal: number | null, capturedAt: string) {
  return {
    percent_threshold: threshold, reset_event_id: 0, captured_at_utc: capturedAt,
    block_cost_usd: blockCost, marginal_cost_usd: marginal,
    seven_day_pct_at_crossing: 18, effective_seven_day_pct_at_crossing: 18,
  };
}

function detailBlock(windowKey: number, startAt: string, resetsAt: string, isClosed: boolean, milestones: unknown[]) {
  return {
    five_hour_window_key: windowKey, block_start_at: startAt, five_hour_resets_at: resetsAt,
    final_five_hour_percent: 20, total_cost_usd: 323.84, crossed_seven_day_reset: false,
    is_closed: isClosed, milestones, credits: [],
  };
}

// A fetched historic cycle: its weekly segment withholds the first marginal
// (observation gap) and then falls to 328.79, and its one block falls from
// 395.05 to 323.84.
const HISTORIC_PAYLOAD = {
  source: 'claude',
  key: HISTORIC_ENTRY.key,
  label: HISTORIC_ENTRY.label,
  start_at_utc: HISTORIC_ENTRY.start_at_utc,
  end_at_utc: HISTORIC_ENTRY.end_at_utc,
  is_current: false,
  detail_stamp: HISTORIC_ENTRY.detail_stamp,
  segments: [{
    key: 'milestone_segment:historic',
    milestones: [
      { percent: 1, crossed_at_utc: '2026-08-26T10:00:00Z', cumulative_usd: 400, marginal_usd: null, five_hour_pct_at_cross: null, marginal_usd_withheld_cause: 'observation_gap' },
      { percent: 2, crossed_at_utc: '2026-08-27T10:00:00Z', cumulative_usd: 328.79, marginal_usd: -71.21, five_hour_pct_at_cross: null },
    ],
  }],
  dividers: [],
  blocks: [
    detailBlock(800, '2026-08-27T01:00:00Z', '2026-08-27T06:00:00Z', true, [
      fhMilestone(15, 395.05, 10, '2026-08-27T01:42:00Z'),
      fhMilestone(16, 323.84, -81.71, '2026-08-27T02:01:00Z'),
    ]),
  ],
  observation_gap_runs: [],
};

function mockFetch(payload: unknown) {
  const spy = vi.fn(async () => ({ ok: true, json: async () => payload }));
  global.fetch = spy as unknown as typeof fetch;
  return spy;
}

function text(selector: string, root: ParentNode = document): string {
  const el = root.querySelector(selector);
  expect(el, selector).toBeTruthy();
  return el!.textContent ?? '';
}

// Exact cell texts, so a sign added to a non-negative amount (`+$395.05`,
// `−$395.05`) fails instead of passing a substring match (spec I2).
function cells(selector: string, root: ParentNode = document): string[] {
  const el = root.querySelector(selector);
  expect(el, selector).toBeTruthy();
  return [...el!.querySelectorAll('td')].map((td) => (td.textContent ?? '').trim());
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

describe('negative milestone money (#886)', () => {
  // R2(a) — the envelope path.
  it('renders a negative weekly marginal in the accounting form', () => {
    updateSnapshot(negativeEnv());
    render(<CurrentWeekModal />);
    const weekly = document.querySelector('#mcw-rows');
    expect(weekly).toBeTruthy();
    expect(weekly!.textContent).toContain('−$71.21');
    expect(cells('#mcw-rows')).toContain('$400.00');
    expect(weekly!.textContent).not.toMatch(DOLLAR_HYPHEN);
  });

  // R1(a) — the live envelope stream.
  it('renders a negative 5h marginal in the accounting form', () => {
    updateSnapshot(negativeEnv());
    render(<CurrentWeekModal />);
    const table = document.querySelector('#mcw-5h-table');
    expect(table).toBeTruthy();
    expect(table!.textContent).toContain('−$81.71');
    expect(cells('#mcw-5h-table')).toContain('$395.05');
    expect(table!.textContent).not.toMatch(DOLLAR_HYPHEN);
  });

  // R3 — on a population with a plain null marginal, so the subtitle shows.
  it('renders a negative average marginal in the accounting form', () => {
    updateSnapshot(negativeEnv());
    render(<CurrentWeekModal />);
    expect(document.body.textContent).toContain('avg marginal −$71.21');
    expect(document.body.textContent).not.toMatch(DOLLAR_HYPHEN);
  });

  // R1(b) — a historic cycle's block, from the fetched cycle detail.
  it('renders a historic block from the fetched detail in the accounting form', async () => {
    mockFetch(HISTORIC_PAYLOAD);
    const env = negativeEnv();
    env.current_week!.week_index = [CURRENT_ENTRY, HISTORIC_ENTRY];
    updateSnapshot(env);
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText(/Block 1 of 1/);
    const table = text('#mcw-5h-table');
    expect(table).toContain('−$81.71');
    expect(cells('#mcw-5h-table')).toContain('$395.05');
    expect(table).not.toMatch(DOLLAR_HYPHEN);
  });

  // R2(b) — a historic cycle's weekly table, with its withheld cell intact.
  it('renders a historic weekly table in the accounting form and keeps the observation gap', async () => {
    mockFetch(HISTORIC_PAYLOAD);
    const env = negativeEnv();
    env.current_week!.week_index = [CURRENT_ENTRY, HISTORIC_ENTRY];
    updateSnapshot(env);
    render(<CurrentWeekModal />);
    fireEvent.click(screen.getByLabelText('Older cycle'));
    await screen.findByText(/Block 1 of 1/);
    const weekly = text('#mcw-rows');
    expect(weekly).toContain('−$71.21');
    expect(cells('#mcw-rows')).toContain('$400.00');
    expect(weekly).toContain('observation gap');
    expect(weekly).not.toMatch(DOLLAR_HYPHEN);
  });

  // R1(c) — current-cycle navigation. Each population carries a DISTINCT
  // negative marginal, so a stale or wrong source shows the wrong number: the
  // live active block (−33.33, later −44.44), the fetched copy of that same
  // active block (−22.22), and an older fetched block (−11.11).
  it('renders the selected block, then the live active block, in the accounting form', async () => {
    const liveEnv = (marginal: number) => {
      const env = negativeEnv();
      env.current_week!.week_index = [{ ...CURRENT_ENTRY, block_count: 2 }];
      env.current_week!.five_hour_milestones = [
        fhMilestone(15, 395.05, 10, '2026-09-05T01:42:00Z'),
        fhMilestone(16, 323.84, marginal, '2026-09-05T02:01:00Z'),
      ] as unknown as NonNullable<Envelope['current_week']>['five_hour_milestones'];
      return env;
    };
    mockFetch({
      source: 'claude', key: CURRENT_ENTRY.key, label: 'Sep 1–8',
      start_at_utc: '2026-09-01T00:00:00Z', end_at_utc: '2026-09-08T00:00:00Z',
      is_current: true, detail_stamp: CURRENT_ENTRY.detail_stamp,
      segments: [{ key: 'milestone_segment:current', milestones: [] }],
      dividers: [],
      blocks: [
        detailBlock(900, '2026-09-04T20:00:00Z', '2026-09-05T01:00:00Z', true, [
          fhMilestone(15, 395.05, 10, '2026-09-04T20:42:00Z'),
          fhMilestone(16, 323.84, -11.11, '2026-09-04T21:01:00Z'),
        ]),
        detailBlock(901, '2026-09-05T01:00:00Z', '2026-09-05T06:00:00Z', false, [
          fhMilestone(15, 395.05, 10, '2026-09-05T01:42:00Z'),
          fhMilestone(16, 323.84, -22.22, '2026-09-05T02:01:00Z'),
        ]),
      ],
      observation_gap_runs: [],
    });
    updateSnapshot(liveEnv(-33.33));
    render(<CurrentWeekModal />);

    expect(screen.getByText(/Block 2 of 2/)).toBeTruthy();
    let table = text('#mcw-5h-table');
    expect(table).toContain('−$33.33');
    expect(table).not.toMatch(DOLLAR_HYPHEN);

    fireEvent.click(screen.getByLabelText('Older block'));
    await screen.findByText(/Block 1 of 2/);
    table = text('#mcw-5h-table');
    expect(table).toContain('−$11.11');
    expect(table).not.toContain('−$22.22');
    expect(table).not.toContain('−$33.33');
    expect(table).not.toMatch(DOLLAR_HYPHEN);

    act(() => updateSnapshot(liveEnv(-44.44)));
    fireEvent.click(screen.getByLabelText('Newer block'));
    await screen.findByText(/Block 2 of 2/);
    table = text('#mcw-5h-table');
    expect(table).toContain('−$44.44');
    expect(table).not.toContain('−$33.33');
    expect(table).not.toContain('−$22.22');
    expect(table).not.toContain('−$11.11');
    expect(table).not.toMatch(DOLLAR_HYPHEN);
  });

  // R1(d) + R2(c) — the embedded All-providers variant, which omits the
  // element IDs, so its Claude section is selected by provider-scoped
  // selectors.
  it('renders the embedded All-providers Claude section in the accounting form', () => {
    const composed = structuredClone(allFixture) as unknown as Envelope;
    composed.current_week = negativeEnv().current_week;
    act(() => {
      updateSnapshot(composed);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
      dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    });
    const { container } = render(<CurrentWeekModal />);
    const claude = container.querySelector('[data-provider-section="claude"]');
    expect(claude).toBeTruthy();
    expect(claude!.querySelector('#mcw-rows')).toBeNull();
    const weekly = text('.mcw-table', claude!);
    expect(weekly).toContain('−$71.21');
    expect(cells('.mcw-table', claude!)).toContain('$400.00');
    expect(weekly).not.toMatch(DOLLAR_HYPHEN);
    const fiveHour = text('.mcw-5h-table', claude!);
    expect(fiveHour).toContain('−$81.71');
    expect(cells('.mcw-5h-table', claude!)).toContain('$395.05');
    expect(fiveHour).not.toMatch(DOLLAR_HYPHEN);
  });

  // R8 dashboard leg (characterization) — the existing accounting formatter's
  // zero behaviour, which the cells above now inherit.
  it('keeps the accounting formatter zero behaviour', () => {
    expect(fmt.usd2Accounting(0)).toBe('$0.00');
    expect(fmt.usd2Accounting(-0)).toBe('$0.00');
    expect(fmt.usd2Accounting(-0.001)).toBe('\u2212$0.00');
  });
});
