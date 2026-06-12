import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationRail } from './ConversationRail';
import { _resetForTests, dispatch, getState } from '../store/store';
import type { ConversationSummary, SearchHit } from '../types/conversation';

// Mirror the hook-test mocking convention (useConversations.test.tsx etc.):
// stub the data hooks + the display-tz hook so the rail render is driven by
// fixtures, not live fetches. The browse-vs-search branch is selected by the
// real store's conversationSearch field via dispatch.
let browseRows: ConversationSummary[] = [];
let searchHits: SearchHit[] = [];
// #177 S6 — per-test overrides for the search-hook surface (total, mode,
// searchDepth, loadMore spy). Reset in beforeEach.
let searchTotal = 0;
let searchMode: 'fts' | 'like' = 'fts';
let searchDepth: 'prose-only' | 'full' = 'full';
let searchLoadingMore = false;
const loadMoreSpy = vi.fn();

vi.mock('../hooks/useConversations', () => ({
  useConversations: () => ({
    rows: browseRows, loading: false, error: null, hasMore: false,
    loadMore: () => Promise.resolve(),
  }),
}));
vi.mock('../hooks/useConversationSearch', () => ({
  useConversationSearch: () => ({
    hits: searchHits, mode: searchMode, total: searchTotal,
    loading: false, loadingMore: searchLoadingMore, searchDepth,
    error: null, loadMore: loadMoreSpy,
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
  searchTotal = 0;
  searchMode = 'fts';
  searchDepth = 'full';
  searchLoadingMore = false;
  loadMoreSpy.mockReset();
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

  // ---- #177 S6: kind chips ----

  it('renders the kind chips as a radiogroup only while searching', () => {
    // No needle → no chips.
    render(<ConversationRail />);
    expect(document.querySelector('[role="radiogroup"]')).toBeNull();
  });

  it('renders five single-select kind chips while a needle is active', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const group = document.querySelector('[role="radiogroup"]')!;
    expect(group).toBeTruthy();
    expect(group.getAttribute('aria-label')).toBe('Search kind');
    const radios = group.querySelectorAll('[role="radio"]');
    expect(radios.length).toBe(5);
    // 'All' is selected by default.
    const all = screen.getByRole('radio', { name: 'All' });
    expect(all.getAttribute('aria-checked')).toBe('true');
  });

  it('clicking a chip dispatches SET_CONVERSATION_SEARCH_KIND', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    fireEvent.click(screen.getByRole('radio', { name: 'Tools' }));
    expect(getState().conversationSearchKind).toBe('tools');
  });

  it('disables Tools/Thinking chips while indexing (prose-only depth)', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    searchDepth = 'prose-only';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    const tools = screen.getByRole('radio', { name: 'Tools' }) as HTMLButtonElement;
    const thinking = screen.getByRole('radio', { name: 'Thinking' }) as HTMLButtonElement;
    expect(tools.disabled).toBe(true);
    expect(thinking.disabled).toBe(true);
    expect(tools.getAttribute('title')).toBe('indexing…');
    // All/Prompts/Assistant stay enabled.
    expect((screen.getByRole('radio', { name: 'All' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('radio', { name: 'Prompts' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('radio', { name: 'Assistant' }) as HTMLButtonElement).disabled).toBe(false);
  });

  // ---- #177 S6: count line ----

  it('count line shows "{total} results"', () => {
    searchHits = [hit({}), hit({ uuid: 'u2' })];
    searchTotal = 87;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const count = document.querySelector('.conv-rail-count')!;
    expect(count).toBeTruthy();
    expect(count.getAttribute('aria-live')).toBe('polite');
    expect(count.textContent).toBe('87 results');
  });

  it('count line folds in "· basic search" on LIKE mode', () => {
    searchHits = [hit({})];
    searchTotal = 3;
    searchMode = 'like';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-count')!.textContent).toBe('3 results · basic search');
    // The old standalone hint is gone.
    expect(document.querySelector('.conv-rail-hint')).toBeNull();
  });

  it('count line shows "No results" when total is 0', () => {
    searchHits = [];
    searchTotal = 0;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'zzz' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-count')!.textContent).toBe('No results');
  });

  // ---- #177 S6: match-kind badges ----

  it('renders match-kind badges on a hit and none on a prose hit', () => {
    searchHits = [
      hit({ uuid: 'tool-hit', match_kinds: ['tool', 'thinking'] }),
      hit({ uuid: 'prose-hit', match_kinds: [] }),
    ];
    searchTotal = 2;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    const rows = document.querySelectorAll('.conv-rail-row--hit');
    const badges0 = rows[0].querySelectorAll('.conv-rail-kindb');
    expect([...badges0].map((b) => b.textContent)).toEqual(['tool', 'thinking']);
    expect(rows[1].querySelectorAll('.conv-rail-kindb').length).toBe(0);
  });

  // ---- #177 S6: load-more button ----

  it('shows a Load-more button with remaining math when hits < total', () => {
    searchHits = [hit({})];
    searchTotal = 120;   // 119 remaining → next page caps at 50
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    const more = document.querySelector('.conv-rail-more') as HTMLButtonElement;
    expect(more).toBeTruthy();
    expect(more.textContent).toBe('Load 50 more (119 remaining)');
    fireEvent.click(more);
    expect(loadMoreSpy).toHaveBeenCalledTimes(1);
  });

  it('load-more shows the exact remaining when fewer than 50 are left', () => {
    searchHits = [hit({}), hit({ uuid: 'u2' })];
    searchTotal = 5;   // 3 remaining
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    expect((document.querySelector('.conv-rail-more') as HTMLButtonElement).textContent)
      .toBe('Load 3 more (3 remaining)');
  });

  it('load-more is hidden once all hits are loaded', () => {
    searchHits = [hit({}), hit({ uuid: 'u2' })];
    searchTotal = 2;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-more')).toBeNull();
  });

  it('load-more is disabled while a page is loading', () => {
    searchHits = [hit({})];
    searchTotal = 10;
    searchLoadingMore = true;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    expect((document.querySelector('.conv-rail-more') as HTMLButtonElement).disabled).toBe(true);
  });
});
