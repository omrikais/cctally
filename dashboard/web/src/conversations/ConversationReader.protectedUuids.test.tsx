import { act, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// #228 S3 B3 (2c) — verify the reader FEEDS the windowed-cap trim its protected
// uuids (the turn the user is on / navigating to must never be dropped). The hook
// itself is unit-tested in useConversation.test.tsx; here we mock it to CAPTURE
// the `opts.protectedUuids` the reader passes in (modal-level wiring — a child
// callback unit can't prove the parent actually wires it; [[feedback_modal_level_integration_test]]).
const optsSeen: { protectedUuids?: Set<string> }[] = [];
vi.mock('../hooks/useConversation', () => ({
  useConversation: (_sessionId: string | null, opts: { protectedUuids?: Set<string> } = {}) => {
    optsSeen.push(opts);
    return {
      detail: {
        session_id: 's', project_label: 'p', git_branch: null, title: null,
        started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
        cost_usd: 0, models: [], subagent_meta: {},
        items: [
          { kind: 'human', anchor: { session_id: 's', uuid: 'h1', id: 1 }, member_uuids: ['h1'], ts: 't', text: 'hi', blocks: [], is_sidechain: false },
          { kind: 'human', anchor: { session_id: 's', uuid: 'h2', id: 2 }, member_uuids: ['h2'], ts: 't', text: 'yo', blocks: [], is_sidechain: false },
        ],
        page: { next_after: null, has_more: false, prev_before: null, has_prev: false },
        last_anchor: null,
      },
      loading: false, error: null, hasMore: false, hasPrev: false, prevBefore: null,
      openScrollIntent: null, lastOp: null,
      loadMore: async () => null, loadPrev: async () => null,
      loadToTarget: async () => {}, jumpToLatest: async () => {}, tailRevision: 0,
    };
  },
}));

import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch } from '../store/store';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';

beforeEach(() => {
  optsSeen.length = 0;
  _resetForTests();
  installIntersectionObserverStub();
});
afterEach(() => { _resetForTests(); vi.restoreAllMocks(); });

const latest = () => optsSeen[optsSeen.length - 1];

describe('ConversationReader feeds protectedUuids into the hook (#228 S3 B3 2c)', () => {
  it('includes the keyboard current-turn uuid', async () => {
    const { rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(optsSeen.length).toBeGreaterThan(0));
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h2' }); });
    rerender(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(latest().protectedUuids?.has('h2')).toBe(true));
  });

  it('includes the explicit pin uuid', async () => {
    const { rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(optsSeen.length).toBeGreaterThan(0));
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h1' }); });
    rerender(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(latest().protectedUuids?.has('h1')).toBe(true));
  });

  it('includes an in-flight jump target for THIS session (covers the find match)', async () => {
    const { rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(optsSeen.length).toBeGreaterThan(0));
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'h2' } }); });
    rerender(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(latest().protectedUuids?.has('h2')).toBe(true));
  });

  it('does NOT include a jump target for a DIFFERENT session', async () => {
    const { rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(optsSeen.length).toBeGreaterThan(0));
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'other', jump: { session_id: 'other', uuid: 'zzz' } }); });
    rerender(<ConversationReader sessionId="s" />);
    // The set never carries another session's jump uuid (it would protect the
    // wrong window — the reader filters on jump.session_id === sessionId).
    await waitFor(() => expect(latest().protectedUuids?.has('zzz')).toBe(false));
  });

  it('is empty when no turn is current / pinned / jumped', async () => {
    render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(optsSeen.length).toBeGreaterThan(0));
    expect(latest().protectedUuids?.size).toBe(0);
  });
});
