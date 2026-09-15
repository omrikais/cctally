// BlockModal — BL-1 (#251): the projected KV label spells out "min" so that,
// under the KV label's text-transform:uppercase, "191 MIN LEFT" reads
// unambiguously (not "191M LEFT" which looked like mega/million directly under
// the total-tokens count). Mirrors SessionModal.test.tsx's fetch-stub +
// OPEN_MODAL pattern.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BlockModal } from './BlockModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import type { BlockDetail, Envelope } from '../types/envelope';

const BLOCK_DETAIL: BlockDetail = {
  start_at: '2026-06-01T00:00:00Z',
  end_at: '2026-06-01T05:00:00Z',
  actual_end_at: null,
  anchor: 'recorded',
  is_active: true,
  label: 'Jun 01 00:00 → 05:00',
  entries_count: 12,
  cost_usd: 12.34,
  total_tokens: 30_777_045,
  input_tokens: 1_000_000,
  output_tokens: 2_000_000,
  cache_creation_tokens: 500_000,
  cache_read_tokens: 300_000,
  cache_hit_pct: 50,
  facts_source: 'computed',
  models: [],
  burn_rate: { tokens_per_minute: 100, cost_per_hour: 5 },
  projection: { total_tokens: 40_000_000, total_cost_usd: 20, remaining_minutes: 191 },
  samples: [],
};

beforeEach(() => {
  _resetForTests();
  dispatch({ type: 'OPEN_MODAL', kind: 'block', blockStartAt: BLOCK_DETAIL.start_at });
  global.fetch = vi.fn(async () => (
    { ok: true, status: 200, json: async () => BLOCK_DETAIL } as Response
  )) as never;
});
afterEach(() => { vi.restoreAllMocks(); });

describe('BlockModal projection unit suffix (BL-1)', () => {
  it('spells out the projection remaining minutes as "191 min left"', async () => {
    render(<BlockModal />);
    const kv = await screen.findByText(/min left/);
    expect(kv.textContent).toContain('191 min left');
    expect(kv.textContent).not.toContain('191m left');
  });

  it('names the resolved IANA zone in both the title and the window bounds', async () => {
    updateSnapshot({
      generated_at: '2026-06-01T00:30:00Z',
      display: {
        tz: 'Asia/Jerusalem', resolved_tz: 'Asia/Jerusalem',
        offset_label: 'IDT', offset_seconds: 10800,
      },
    } as Envelope);
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ...BLOCK_DETAIL, label: '03:00 Jun 01 IDT' }),
    } as Response)) as never;

    render(<BlockModal />);

    await screen.findByText(/03:00 Jun 01 IDT/);
    expect(screen.getAllByText('[Asia/Jerusalem]')).toHaveLength(2);
    expect(screen.getByRole('dialog')).toHaveAccessibleName(
      'Block · 03:00 Jun 01 IDT [Asia/Jerusalem]',
    );
  });
});

// #260 — the block detail "Cost by model" section reuses the shared
// `ModelCostBars` (History / Session / Projects parity), replacing the former
// bespoke segmented bar + legend (`.msess-costm`).
describe('BlockModal cost-by-model uses shared ModelCostBars (#260)', () => {
  it('renders ModelCostBars rows (not the bespoke segmented bar) for a multi-model block', async () => {
    global.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        ...BLOCK_DETAIL,
        models: [
          { model: 'claude-opus-4-5-20251101', display: 'opus-4-5', chip: 'opus', cost_usd: 12.3, cost_pct: 85 },
          { model: 'claude-haiku-4-5-20251001', display: 'haiku-4-5', chip: 'haiku', cost_usd: 2.1, cost_pct: 15 },
        ],
      }),
    } as Response)) as never;
    render(<BlockModal />);
    await screen.findByText('Cost by model');
    // The bespoke segmented bar + legend are gone.
    expect(document.querySelector('.msess-costm')).toBeNull();
    // ModelCostBars rendered one drill-bar row per model with the server
    // `display` label + fmt.usd2 cost, relative to the top model.
    const rows = document.querySelectorAll('.drill-bar-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('opus-4-5')).toBeTruthy();
    expect(screen.getByText('haiku-4-5')).toBeTruthy();
    expect(screen.getByText('$12.30')).toBeTruthy();
    expect(screen.getByText('$2.10')).toBeTruthy();
    const bars = document.querySelectorAll('.drill-bar');
    expect((bars[0] as HTMLElement).style.getPropertyValue('--w')).toBe('100%');
  });
});

// #769 S2 P2-5: a frozen five-hour block serves its headline from the facts
// retained when it closed, while the trajectory below it is drawn from whatever
// entries the local cache holds now. Measured on a seeded store, one retained
// block served a $41.50 headline over seven entries above a four-sample
// trajectory ending at $18.60, and nothing on the page said the two figures
// came from different evidence. That divergence is what these cases pin.
//
// The two empty-`samples` cases pin the component against a payload shape the
// SERVER DOES NOT CURRENTLY PRODUCE: a block exists only where entries grouped
// into it, so a window with no cached entries yields no block and `/api/block`
// answers 404. They are here so the arm cannot start lying if that changes.
describe('BlockModal frozen-block evidence disclosure (#769 S2)', () => {
  const FROZEN: BlockDetail = {
    ...BLOCK_DETAIL,
    is_active: false,
    burn_rate: null,
    projection: null,
    facts_source: 'retained',
    samples: [],
  };

  function serve(detail: BlockDetail) {
    global.fetch = vi.fn(async () => (
      { ok: true, status: 200, json: async () => detail } as Response
    )) as never;
  }

  it('would not claim a retained block with an empty cache recorded no spend', async () => {
    serve(FROZEN);
    render(<BlockModal />);
    await screen.findByText(/retained when this block closed/);
    expect(screen.queryByText(/No spend recorded yet in this block/)).toBeNull();
  });

  it('would say why a retained block has no trajectory to draw', async () => {
    serve(FROZEN);
    render(<BlockModal />);
    const note = await screen.findByText(/retained when this block closed/);
    expect(note.textContent).toContain('no longer in the local cache');
    expect(note.textContent).toContain('no trajectory to draw');
  });

  it('warns that a retained block’s trajectory can differ from its headline', async () => {
    serve({
      ...FROZEN,
      samples: [
        { t: '2026-06-01T01:00:00Z', cum: 1.5 },
        { t: '2026-06-01T02:00:00Z', cum: 3.0 },
      ],
    });
    render(<BlockModal />);
    const note = await screen.findByText(/retained when this block closed/);
    expect(note.textContent).toContain('drawn from the entries currently in the local cache');
    expect(note.textContent).toContain('can differ');
    // The gap is the point: the headline is $12.34 and the trajectory ends at
    // $3.00, so the note names the second figure rather than leaving the reader
    // to infer it from the chart's vertical scale.
    expect(note.textContent).toContain('totals $3.00');
  });

  it('keeps the computed empty state, and adds no note, when nothing is retained', async () => {
    serve({ ...FROZEN, facts_source: 'computed', cost_usd: 0, entries_count: 0 });
    render(<BlockModal />);
    await screen.findByText(/No spend recorded yet in this block/);
    expect(screen.queryByText(/retained when this block closed/)).toBeNull();
  });
});
