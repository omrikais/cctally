import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationRail } from './ConversationRail';
import { _resetForTests, dispatch } from '../store/store';
import type { ConversationSummary, SearchHit } from '../types/conversation';

// Mirror the hook-test mocking convention (useConversations.test.tsx etc.):
// stub the data hooks + the display-tz hook so the rail render is driven by
// fixtures, not live fetches. The browse-vs-search branch is selected by the
// real store's conversationSearch field via dispatch.
let browseRows: ConversationSummary[] = [];
let searchHits: SearchHit[] = [];

vi.mock('../hooks/useConversations', () => ({
  useConversations: () => ({
    rows: browseRows, loading: false, error: null, hasMore: false,
    loadMore: () => Promise.resolve(),
  }),
}));
vi.mock('../hooks/useConversationSearch', () => ({
  useConversationSearch: () => ({
    hits: searchHits, mode: 'fts' as const, loading: false, error: null,
  }),
}));
vi.mock('../hooks/useDisplayTz', () => ({
  useDisplayTz: () => ({
    tz: 'utc', resolvedTz: 'Etc/UTC', offsetLabel: 'UTC', offsetSeconds: 0, pinned: false,
  }),
}));

function summary(over: Partial<ConversationSummary>): ConversationSummary {
  return {
    session_id: 's1',
    title: 'a conversation',
    project_label: 'proj',
    git_branch: 'main',
    started_utc: '2026-06-09T01:00:00Z',
    last_activity_utc: '2026-06-09T02:00:00Z',
    msg_count: 4,
    cost_usd: 1.25,
    models: ['claude-opus-4'],
    ...over,
  };
}

function hit(over: Partial<SearchHit>): SearchHit {
  return {
    session_id: 's1',
    uuid: 'u1',
    project_label: 'proj',
    title: 'how does the lock work',
    ts: '2026-06-09T01:10:00Z',
    snippet: 'the [flock] serializes writers',
    cost_usd: 0.05,
    ...over,
  };
}

beforeEach(() => {
  _resetForTests();
  browseRows = [];
  searchHits = [];
});
afterEach(() => {
  _resetForTests();
  vi.restoreAllMocks();
});

describe('ConversationRail', () => {
  it('browse rows lead with the title and show date dividers', () => {
    // Two rows in different date buckets (Today vs an older month), each titled.
    browseRows = [
      summary({ session_id: 's1', title: 'design the rail', started_utc: '2026-06-09T01:00:00Z' }),
      summary({ session_id: 's2', title: 'an older chat', started_utc: '2026-04-15T10:00:00Z' }),
    ];
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-row-title')).toBeTruthy();
    expect(screen.getByText('design the rail')).toBeInTheDocument();   // a row title
    expect(document.querySelector('.conv-rail-sec')).toBeTruthy();     // a divider
    // Two distinct buckets → two dividers.
    expect(document.querySelectorAll('.conv-rail-sec').length).toBeGreaterThanOrEqual(2);
  });

  it('search rows show the title header above the snippet', () => {
    searchHits = [hit({})];
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    expect(row).toBeTruthy();
    const title = row.querySelector('.conv-rail-row-title')!;
    const snippet = row.querySelector('.conv-rail-row-snippet')!;
    expect(title).toBeTruthy();
    expect(snippet).toBeTruthy();
    expect(title.textContent).toContain('how does the lock work');
    expect(title.compareDocumentPosition(snippet) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
