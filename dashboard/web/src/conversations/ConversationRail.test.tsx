import { act, fireEvent, render, screen } from '@testing-library/react';
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
let filterDegraded = false;
const loadMoreSpy = vi.fn();
// #205 S3 (F8) — overridable error + retry spy so the error-branch Retry button
// can be exercised. Default error null preserves every existing browse test.
let browseError: string | null = null;
const retrySpy = vi.fn();
// #217 S3 E10#7 — overridable browse-list paging surface so the Load-more
// disabled/loading state can be exercised. Defaults preserve every existing
// browse test (no Load-more button).
let browseHasMore = false;
let browseLoadingMore = false;
const browseLoadMoreSpy = vi.fn();

vi.mock('../hooks/useConversations', () => ({
  useConversations: () => ({
    rows: browseRows, loading: false, error: browseError, hasMore: browseHasMore,
    loadMore: browseLoadMoreSpy, loadingMore: browseLoadingMore,
    filterDegraded, retry: retrySpy,
  }),
}));
// Stub the popover so the rail render doesn't reach useConversationFacets' live
// fetch — the rail-integration tests assert the popover MOUNTS (and the real
// store effects of the rail's own buttons/chips), not the popover internals.
vi.mock('./ConversationFiltersPopover', () => ({
  ConversationFiltersPopover: () => <div data-testid="filters-popover" />,
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
  filterDegraded = false;
  loadMoreSpy.mockReset();
  browseError = null;
  retrySpy.mockClear();
  browseHasMore = false;
  browseLoadingMore = false;
  browseLoadMoreSpy.mockReset();
});
afterEach(() => {
  _resetForTests();
  vi.restoreAllMocks();
});

describe('ConversationRail', () => {
  it('browse rows lead with the title and show date dividers', () => {
    // Two rows in different date buckets (recent vs an older month), each titled.
    // Buckets group on last_activity_utc (filters spec §4 last-activity-everywhere),
    // so the divider split is driven by last_activity, not started_utc.
    browseRows = [
      summary({ session_id: 's1', title: 'design the rail', last_activity_utc: '2026-06-09T01:00:00Z' }),
      summary({ session_id: 's2', title: 'an older chat', last_activity_utc: '2026-04-15T10:00:00Z' }),
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

  it('folds "· indexing…" into the count line when an active split-needing kind is prose-only', () => {
    // #177 S6 M1 — Tools/Thinking selected while the split index is still
    // backfilling (prose-only). The chip greys out but the hook still renders an
    // empty/degraded list; the inline note explains it (the disabled chip's
    // title="indexing…" is hover-only and keyboard-unreachable).
    searchHits = [];
    searchTotal = 0;
    searchDepth = 'prose-only';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'tools' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-count')!.textContent).toBe('No results · indexing…');
  });

  it('does NOT add "· indexing…" for a non-split kind under prose-only', () => {
    // 'All'/'Prompts'/'Assistant' don't need the split index, so prose-only is a
    // complete result for them — no indexing note.
    searchHits = [hit({})];
    searchTotal = 4;
    searchDepth = 'prose-only';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'prompts' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-count')!.textContent).toBe('4 results');
  });

  it('does NOT add "· indexing…" for a split kind once the index is full', () => {
    searchHits = [hit({})];
    searchTotal = 4;
    searchDepth = 'full';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'thinking' });
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-count')!.textContent).toBe('4 results');
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

  it('keeps badges OUTSIDE the clamped title-text element so long titles cannot clip them', () => {
    // Structural guard for the clamp-clipping fix: the title TEXT carries the
    // 2-line `-webkit-line-clamp` (.conv-rail-row-title-text); the badge group
    // must be a SIBLING of that clamped element, never a descendant — else a
    // long title fills both clamp lines and clips the badge row as a phantom
    // third line. JSDOM can't evaluate the clamp itself; we assert structure.
    searchHits = [hit({ uuid: 'tool-hit', match_kinds: ['tool', 'thinking'] })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    const clamped = row.querySelector('.conv-rail-row-title-text')!;
    const badges = row.querySelector('.conv-rail-kindbs')!;
    expect(clamped).toBeTruthy();
    expect(badges).toBeTruthy();
    // The badge group is NOT nested inside the clamped text element…
    expect(clamped.contains(badges)).toBe(false);
    // …and they share the title row as siblings.
    expect(badges.parentElement).toBe(clamped.parentElement);
    expect((badges.parentElement as HTMLElement).classList.contains('conv-rail-row-title')).toBe(true);
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

  // #217 S3 E10#7 — the BROWSE list's Load-more gets the same disabled/loading
  // affordance the search list already has (decision d — no sentinel-ization).
  it('browse Load-more is enabled and clickable while idle', () => {
    browseRows = [summary({ session_id: 'a' })];
    browseHasMore = true;
    render(<ConversationRail />);
    const more = document.querySelector('.conv-rail-more') as HTMLButtonElement;
    expect(more).toBeTruthy();
    expect(more.disabled).toBe(false);
    fireEvent.click(more);
    expect(browseLoadMoreSpy).toHaveBeenCalledTimes(1);
  });

  it('browse Load-more is disabled while a page is loading', () => {
    browseRows = [summary({ session_id: 'a' })];
    browseHasMore = true;
    browseLoadingMore = true;
    render(<ConversationRail />);
    expect((document.querySelector('.conv-rail-more') as HTMLButtonElement).disabled).toBe(true);
  });

  // ---- filters spec §4: Filters button, popover, active-filter chips ----

  it('shows a Filters button that toggles the popover', () => {
    browseRows = [summary({ session_id: 'a' })];
    render(<ConversationRail />);
    expect(screen.queryByTestId('filters-popover')).toBeNull();
    const btn = screen.getByRole('button', { name: /filters/i });
    fireEvent.click(btn);
    expect(getState().convFiltersOpen).toBe(true);
    // Popover mounts once open (parent-integration: real store + child mount).
    expect(screen.getByTestId('filters-popover')).toBeTruthy();
  });

  it('renders removable active-filter chips', () => {
    browseRows = [summary({ session_id: 'a' })];
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 1 } }));
    const chip = screen.getByText(/≥1/);
    fireEvent.click(chip.closest('button')!);   // chip's ✕ removes it
    expect(getState().conversationFilters.rebuildMin).toBeNull();
  });

  it('gives the chip-remove button a "Remove …" accessible name', () => {
    // FINDING 6: the ✕ is aria-hidden and the only "remove" cue was title=, which
    // screen readers don't reliably announce. The button must carry an aria-label
    // so its accessible name conveys the remove action — and it must remain the
    // single clickable control that removes the axis.
    browseRows = [summary({ session_id: 'a' })];
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 1 } }));
    const removeBtn = screen.getByRole('button', { name: /remove .*≥1/i });
    expect(removeBtn).toBeTruthy();
    fireEvent.click(removeBtn);
    expect(getState().conversationFilters.rebuildMin).toBeNull();
  });

  it('Clear all chip dispatches CLEAR_CONVERSATION_FILTERS', () => {
    browseRows = [summary({ session_id: 'a' })];
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 2, projects: ['proj'] } }));
    fireEvent.click(screen.getByRole('button', { name: /clear all/i }));
    expect(getState().conversationFilters.projects).toEqual([]);
    expect(getState().conversationFilters.rebuildMin).toBeNull();
  });

  it('renders a removable project chip per selected project', () => {
    browseRows = [summary({ session_id: 'a' })];
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { projects: ['projA', 'projB'] } }));
    const projChip = screen.getByText('projA').closest('button')!;
    fireEvent.click(projChip);   // removing projA leaves projB
    expect(getState().conversationFilters.projects).toEqual(['projB']);
  });

  it('disables Filters while searching and hides chips', () => {
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 1 } }));
    // A chip is visible while browsing.
    expect(screen.queryByText(/≥1/)).toBeTruthy();
    act(() => dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'hello' }));
    expect(screen.getByRole('button', { name: /filters/i })).toBeDisabled();
    // Chips hide while searching.
    expect(screen.queryByText(/≥1/)).toBeNull();
  });

  it('buckets browse rows by last activity, not start', () => {
    // started_utc is February but last_activity_utc is April — the section header
    // must reflect April (last activity), proving the grouping switched off
    // started_utc (filters spec §4 last-activity-everywhere / Codex P2 #6).
    browseRows = [summary({
      session_id: 'a',
      started_utc: '2026-02-01T00:00:00Z',
      last_activity_utc: '2026-04-15T00:00:00Z',
    })];
    render(<ConversationRail />);
    const sec = document.querySelector('.conv-rail-sec')!;
    expect(sec.textContent).toMatch(/Apr|April|2026-04/i);
    expect(sec.textContent).not.toMatch(/Feb|February/i);
  });

  it('surfaces a muted degraded note when filterDegraded is set', () => {
    browseRows = [summary({ session_id: 'a' })];
    filterDegraded = true;
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-filters-degraded')).toBeTruthy();
  });

  // ---- filters spec §4: distinct filtered-to-zero empty state ----

  it('shows the generic empty copy when there are no conversations and no filters', () => {
    // No rows, no active filters → the "nothing here at all" message, NOT the
    // filtered-to-zero copy, and no Clear-filters button.
    browseRows = [];
    render(<ConversationRail />);
    const empty = document.querySelector('.conv-rail-empty')!;
    expect(empty).toBeTruthy();
    expect(empty.textContent).toBe('No conversations.');
    expect(screen.queryByRole('button', { name: /clear filters/i })).toBeNull();
  });

  it('shows the distinct filtered-to-zero copy + a working Clear-filters button', () => {
    // No rows BUT a filter is active → the "no matches" copy plus a Clear-filters
    // button that dispatches CLEAR_CONVERSATION_FILTERS (spec §4 Empty state).
    browseRows = [];
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 3, projects: ['projA'] } }));
    const empty = document.querySelector('.conv-rail-empty')!;
    expect(empty.textContent).toContain('No conversations match these filters');
    // The generic copy must NOT show in the filtered case.
    expect(empty.textContent).not.toBe('No conversations.');
    const clear = screen.getByRole('button', { name: /clear filters/i });
    expect(clear).toBeTruthy();
    fireEvent.click(clear);
    expect(getState().conversationFilters.rebuildMin).toBeNull();
    expect(getState().conversationFilters.projects).toEqual([]);
  });
});

describe('ConversationRail browse-list error state (#205 S3 F8)', () => {
  it('shows a Retry button in the error state that calls retry()', () => {
    browseError = "Couldn't load conversations.";
    render(<ConversationRail />);
    const btn = screen.getByRole('button', { name: /retry/i });
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(retrySpy).toHaveBeenCalledTimes(1);
  });
});
