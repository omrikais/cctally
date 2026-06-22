import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { _resetForTests, dispatch, getState } from '../store/store';
import { ComparisonView } from './ComparisonView';
import type { ConversationOutline } from '../types/conversation';

// A minimal outline whose turns include two main-thread human turns so the
// prompt spine is non-empty. The `sid` differentiates A/B so the two
// useConversationOutline instances render distinct spines.
function outlineFixture(sid: string): ConversationOutline {
  const human = (uuid: string, label: string): ConversationOutline['turns'][number] => ({
    uuid,
    kind: 'human',
    ts: '2026-06-22T10:00:00Z',
    label,
    member_uuids: [uuid],
    subagent_key: null,
    parent_uuid: null,
    is_sidechain: false,
  });
  const second = sid === 'A' ? 'fix mock' : 'use fixtures';
  return {
    session_id: sid,
    stats: {
      turns: { total: 4, human: 2, assistant: 2, tool_result: 0, meta: 0 },
      tool_counts: {},
      error_count: sid === 'A' ? 2 : 0,
      models: { 'claude-sonnet-4': 1 },
      duration_seconds: 600,
      tokens: { input: 10, output: 20, cache_creation: 30, cache_read: 40 },
      cost_usd: sid === 'A' ? 0.42 : 0.31,
      cache_saved_usd: 0,
    },
    files: [],
    turns: [human(`${sid}-h1`, 'shared'), human(`${sid}-h2`, second)],
  };
}

function mockFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = String(url);
    const sid = u.includes('/A/') ? 'A' : 'B';
    if (u.includes('/outline')) {
      return Promise.resolve(new Response(JSON.stringify(outlineFixture(sid)), { status: 200 }));
    }
    if (u.includes('/prompts')) {
      return Promise.resolve(
        new Response(JSON.stringify({ session_id: sid, prompts: [] }), { status: 200 }),
      );
    }
    return Promise.resolve(new Response('{}', { status: 200 }));
  });
}

describe('ComparisonView', () => {
  beforeEach(() => { _resetForTests(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('renders the header + metrics + aligned prompts for two sessions', async () => {
    mockFetch();
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    render(<ComparisonView a="A" b="B" />);
    await waitFor(() => expect(screen.getByText(/Comparing/i)).toBeInTheDocument());
    // the shared first prompt aligns once
    await waitFor(() => expect(screen.getAllByText('shared').length).toBeGreaterThan(0));
    // the divergent second prompts render on both sides
    expect(screen.getByText('fix mock')).toBeInTheDocument();
    expect(screen.getByText('use fixtures')).toBeInTheDocument();
  });

  it('header prefers a cached rail title, else falls back to the session slug (#227)', async () => {
    mockFetch();
    // Seed the shared rail title cache for A only; B has no cached title.
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['A', 'Refactor the store']] });
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    render(<ComparisonView a="A" b="B" />);
    await waitFor(() => expect(screen.getByText(/Comparing/i)).toBeInTheDocument());
    // A: real derived title from the cache.
    expect(screen.getByText('Refactor the store')).toBeInTheDocument();
    // B: no cached title → `Session <slug>` fallback (slug of 'B' is 'B').
    expect(screen.getByText('Session B')).toBeInTheDocument();
  });

  it('swap dispatches SWAP_COMPARE', async () => {
    mockFetch();
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    render(<ComparisonView a="A" b="B" />);
    await waitFor(() => expect(screen.getByText(/Comparing/i)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/swap/i));
    expect(getState().compare).toEqual({ a: 'B', b: 'A' });
  });

  it('close dispatches CLOSE_COMPARE', async () => {
    mockFetch();
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    render(<ComparisonView a="A" b="B" />);
    await waitFor(() => expect(screen.getByText(/Comparing/i)).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/close comparison/i));
    expect(getState().compare).toBeNull();
  });

  it('shows a not-found fallback when an outline errors', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('nope', { status: 404 }));
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    render(<ComparisonView a="A" b="B" />);
    await waitFor(() => expect(screen.getByText(/not (be )?found|couldn't load/i)).toBeInTheDocument());
  });
});
