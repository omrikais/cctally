// SessionModal integration — the modal renders CacheRebuildsSection and a Jump
// drives OPEN_CONVERSATION cross-nav (parent-wiring guard; child unit lives in
// CacheRebuildsSection.test.tsx).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SessionModal } from './SessionModal';
import { _resetForTests, getState, dispatch } from '../store/store';

const SESSION_DETAIL = {
  session_id: 's1', started_utc: '2026-06-01T00:00:00Z',
  last_activity_utc: '2026-06-01T01:00:00Z', duration_min: 60,
  project_label: 'demo', project_path: '/demo', source_paths: [],
  models: [], input_tokens: 1, output_tokens: 1,
  cache_creation_tokens: 1, cache_read_tokens: 1, cache_hit_pct: 50,
  cost_per_model: [], cost_total_usd: 1.0,
};
const OUTLINE = {
  session_id: 's1',
  stats: {
    turns: { total: 0, human: 0, assistant: 0, tool_result: 0, meta: 0 },
    tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
    tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 },
    cost_usd: 0, cache_saved_usd: 1.0,
    cache_failures: { count: 1, tokens_recreated: 100000, est_wasted_usd: 0.1,
      rebuilds: [{ uuid: 'u0', subagent_key: null, ts: '2026-06-01T00:30:00Z',
                   tokens_recreated: 100000, est_wasted_usd: 0.1 }] },
  },
  turns: [],
};

beforeEach(() => {
  _resetForTests();
  dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: 's1' });
  global.fetch = vi.fn(async (url: string) => {
    const body = String(url).includes('/outline') ? OUTLINE : SESSION_DETAIL;
    return { ok: true, status: 200, json: async () => body } as Response;
  }) as never;
});
afterEach(() => { vi.restoreAllMocks(); });

describe('SessionModal cache-rebuilds wiring', () => {
  it('renders the section and jumps to the rebuild turn', async () => {
    render(<SessionModal />);
    await waitFor(() => expect(screen.getByText('Cache rebuilds')).toBeTruthy());
    fireEvent.click(await screen.findByRole('button', { name: /Jump/ }));
    expect(getState().view).toBe('conversations');
    expect(getState().selectedConversationId).toBe('s1');
    expect(getState().conversationJump?.uuid).toBe('u0');
  });
});

// SE-1 — STARTED / LAST ACTIVITY localize through fmt.datetimeShort instead of
// rendering the raw `*_utc` ISO. The store has no snapshot (useDisplayTz falls
// back to Etc/UTC), so the two cells render "Mon DD HH:MM UTC".
describe('SessionModal localizes timestamps (SE-1)', () => {
  it('renders STARTED / LAST ACTIVITY via fmt, not raw ISO', async () => {
    render(<SessionModal />);
    const started = await screen.findByText((t) => t.includes('Jun 01') && t.includes('00:00'));
    const last = await screen.findByText((t) => t.includes('Jun 01') && t.includes('01:00'));
    expect(started.textContent).not.toMatch(/T\d\d:\d\d:\d\dZ/);
    expect(last.textContent).not.toMatch(/T\d\d:\d\d:\d\dZ/);
    // no raw trailing Z anywhere in the two cells
    expect(started.textContent).not.toContain('Z');
    expect(last.textContent).not.toContain('Z');
  });
});

// SE-2 — a single-model session collapses "Models" + "Cost by model" into one
// caption. #260 — a multi-model session drops the standalone "Models" chip
// strip and renders the shared `ModelCostBars` under "Cost by model" (History
// parity); the bespoke segmented bar + legend are gone.
describe('SessionModal single-model collapse (SE-2)', () => {
  function mountWith(detail: Record<string, unknown>) {
    global.fetch = vi.fn(async (url: string) => {
      const body = String(url).includes('/outline') ? OUTLINE : detail;
      return { ok: true, status: 200, json: async () => body } as Response;
    }) as never;
    render(<SessionModal />);
  }

  it('collapses a single-model session into one caption (no Models / Cost-by-model sections)', async () => {
    mountWith({
      ...SESSION_DETAIL,
      models: [{ name: 'claude-opus-4' }],
      cost_per_model: [{ model: 'claude-opus-4', cost_usd: 1.0 }],
    });
    await waitFor(() =>
      expect(document.getElementById('msess-model-caption')).toBeTruthy(),
    );
    expect(document.querySelector('.sec-mod')).toBeNull();
    expect(document.querySelector('.sec-costm')).toBeNull();
    const caption = document.getElementById('msess-model-caption')!;
    expect(caption.textContent).toContain('claude-opus-4');
    expect(caption.textContent).toContain('$1.00');
  });

  it('drops the Models strip and renders ModelCostBars for a multi-model session (#260)', async () => {
    mountWith({
      ...SESSION_DETAIL,
      models: [
        { name: 'claude-opus-4-5-20251101' },
        { name: 'claude-haiku-4-5-20251001' },
      ],
      cost_per_model: [
        { model: 'claude-opus-4-5-20251101', cost_usd: 12.3 },
        { model: 'claude-haiku-4-5-20251001', cost_usd: 2.1 },
      ],
    });
    await waitFor(() => expect(document.querySelector('.sec-costm')).toBeTruthy());
    // No single-model caption (multi-model path).
    expect(document.getElementById('msess-model-caption')).toBeNull();
    // The standalone "Models" chip strip is gone — ModelCostBars carries the chips now.
    expect(document.querySelector('.sec-mod')).toBeNull();
    expect(document.getElementById('msess-models')).toBeNull();
    // The bespoke segmented bar + legend are gone.
    expect(document.getElementById('msess-cost-bar')).toBeNull();
    expect(document.getElementById('msess-cost-legend')).toBeNull();
    // ModelCostBars rendered one drill-bar row per model, cost-descending.
    const rows = document.querySelectorAll('.drill-bar-row');
    expect(rows).toHaveLength(2);
    // Chip labels are the SHORT abbreviateModel form (not the dated canonical id).
    expect(screen.getByText('opus-4-5')).toBeTruthy();
    expect(screen.getByText('haiku-4-5')).toBeTruthy();
    expect(screen.queryByText('claude-opus-4-5-20251101')).toBeNull();
    // Costs render via fmt.usd2 (2-dec), not the old 3-dec legend.
    expect(screen.getByText('$12.30')).toBeTruthy();
    expect(screen.getByText('$2.10')).toBeTruthy();
    // Top model bar = 100%, second = ~17% (2.1/12.3), relative to the TOP model.
    const bars = document.querySelectorAll('.drill-bar');
    expect((bars[0] as HTMLElement).style.getPropertyValue('--w')).toBe('100%');
  });
});
