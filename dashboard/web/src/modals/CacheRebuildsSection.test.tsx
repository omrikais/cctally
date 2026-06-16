// CacheRebuildsSection — tiles + worst-first list + cap/more + zero-state +
// saved-line gating + markers-off suppression + jump dispatch (2026-06-16 spec).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CacheRebuildsSection } from './CacheRebuildsSection';
import {
  _resetForTests,
  getState,
  dispatch,
} from '../store/store';
import type { ConversationOutline } from '../types/conversation';

// Controllable mock outline returned by the hook.
let MOCK_OUTLINE: ConversationOutline | null = null;
vi.mock('../hooks/useConversationOutline', () => ({
  useConversationOutline: () => ({ outline: MOCK_OUTLINE, loading: false, error: null }),
}));

function outlineWith(rebuilds: number, saved: number): ConversationOutline {
  const list = Array.from({ length: rebuilds }, (_, i) => ({
    uuid: `u${i}`,
    subagent_key: i === 0 ? 'sub-abc' : null,
    ts: `2026-06-01T0${i}:00:00Z`,
    tokens_recreated: (rebuilds - i) * 100_000,        // already worst-first
    est_wasted_usd: (rebuilds - i) * 0.1,
  }));
  return {
    session_id: 's1',
    stats: {
      turns: { total: 0, human: 0, assistant: 0, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 },
      cost_usd: 0,
      cache_saved_usd: saved,
      ...(rebuilds > 0
        ? { cache_failures: {
              count: rebuilds,
              tokens_recreated: list.reduce((a, r) => a + r.tokens_recreated, 0),
              est_wasted_usd: list.reduce((a, r) => a + r.est_wasted_usd, 0),
              rebuilds: list } }
        : {}),
    },
    turns: [],
  };
}

beforeEach(() => { _resetForTests(); MOCK_OUTLINE = null; });
afterEach(() => { vi.clearAllMocks(); });

describe('CacheRebuildsSection', () => {
  it('renders count/wasted/re-created tiles and a worst-first capped list', () => {
    MOCK_OUTLINE = outlineWith(5, 3.18);
    render(<CacheRebuildsSection sessionId="s1" />);
    expect(screen.getByText('Cache rebuilds')).toBeTruthy();
    expect(screen.getByText('5')).toBeTruthy();                 // Rebuilds tile
    // Only the first 3 (worst) rows render before expanding.
    expect(screen.getAllByRole('button', { name: /Jump/ }).length).toBe(3);
    expect(screen.getByText('+2 more')).toBeTruthy();
  });

  it('expands the full list on "+N more"', () => {
    MOCK_OUTLINE = outlineWith(5, 0);
    render(<CacheRebuildsSection sessionId="s1" />);
    fireEvent.click(screen.getByText('+2 more'));
    expect(screen.getAllByRole('button', { name: /Jump/ }).length).toBe(5);
  });

  it('shows the cache-saved line only when > 0', () => {
    MOCK_OUTLINE = outlineWith(0, 0);
    const { rerender } = render(<CacheRebuildsSection sessionId="s1" />);
    expect(screen.queryByText(/Cache saved this session/)).toBeNull();
    MOCK_OUTLINE = outlineWith(0, 2.5);
    rerender(<CacheRebuildsSection sessionId="s1" />);
    expect(screen.getByText(/Cache saved this session/)).toBeTruthy();
  });

  it('renders the healthy zero-state when there are no rebuilds', () => {
    MOCK_OUTLINE = outlineWith(0, 1.0);
    render(<CacheRebuildsSection sessionId="s1" />);
    expect(screen.getByText('No cache rebuilds ✓')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Jump/ })).toBeNull();
  });

  it('is suppressed entirely when markers are disabled', () => {
    // selectMarkersEnabled reads state.dashboardPrefs (seeded by
    // INGEST_DASHBOARD_PREFS), NOT the snapshot — so flip it via that action,
    // not updateSnapshot.
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { cache_failure_markers: false } });
    MOCK_OUTLINE = outlineWith(3, 1.0);
    const { container } = render(<CacheRebuildsSection sessionId="s1" />);
    expect(container.querySelector('.m-sec')).toBeNull();
  });

  it('dispatches OPEN_CONVERSATION with the rebuild uuid on Jump', () => {
    MOCK_OUTLINE = outlineWith(1, 0);
    render(<CacheRebuildsSection sessionId="s1" />);
    fireEvent.click(screen.getByRole('button', { name: /Jump/ }));
    expect(getState().view).toBe('conversations');
    expect(getState().selectedConversationId).toBe('s1');
    expect(getState().conversationJump?.uuid).toBe('u0');
  });
});
