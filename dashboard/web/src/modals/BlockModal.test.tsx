// BlockModal — BL-1 (#251): the projected KV label spells out "min" so that,
// under the KV label's text-transform:uppercase, "191 MIN LEFT" reads
// unambiguously (not "191M LEFT" which looked like mega/million directly under
// the total-tokens count). Mirrors SessionModal.test.tsx's fetch-stub +
// OPEN_MODAL pattern.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BlockModal } from './BlockModal';
import { _resetForTests, dispatch } from '../store/store';
import type { BlockDetail } from '../types/envelope';

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
