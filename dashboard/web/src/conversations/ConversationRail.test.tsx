import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationRail, MATCH_KIND_LABELS } from './ConversationRail';
import { _resetForTests, dispatch, getState } from '../store/store';
import { clearRailPrefs } from '../store/conversationRailPrefs';
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
// #228 S1 (§6c) — overridable search loading/error so the role="status" /
// role="alert" branches can be exercised. Defaults preserve every existing
// search test (no loading, no error).
let searchLoading = false;
let searchError: string | null = null;
let filterDegraded = false;
// #217 S4 / I-2 — browse sort_degraded + search top-level filter_degraded.
let sortDegraded = false;
let searchFilterDegraded = false;
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
    filterDegraded, sortDegraded, retry: retrySpy,
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
    loading: searchLoading, loadingMore: searchLoadingMore, searchDepth,
    filterDegraded: searchFilterDegraded,
    error: searchError, loadMore: loadMoreSpy,
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
  // #217 S4 / I-2.2 — the railPrefs blob now persists filters+sort to
  // localStorage; clear it BEFORE _resetForTests so loadInitial re-seeds clean
  // (a prior test's SET_CONVERSATION_FILTERS would otherwise bleed across).
  clearRailPrefs();
  _resetForTests();
  browseRows = [];
  searchHits = [];
  searchTotal = 0;
  searchMode = 'fts';
  searchDepth = 'full';
  searchLoadingMore = false;
  searchLoading = false;
  searchError = null;
  filterDegraded = false;
  sortDegraded = false;
  searchFilterDegraded = false;
  loadMoreSpy.mockReset();
  browseError = null;
  retrySpy.mockClear();
  browseHasMore = false;
  browseLoadingMore = false;
  browseLoadMoreSpy.mockReset();
});
afterEach(() => {
  clearRailPrefs();
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

  it('renders the single-select kind chips while a needle is active', () => {
    // #217 S4 / I-3.2 — the row grew from 5 content kinds to 7 with the two
    // structural facets (Title/Files); a dedicated test below asserts the 7-count
    // + the two new chips. Here we only assert the radiogroup + default selection.
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const group = document.querySelector('[role="radiogroup"]')!;
    expect(group).toBeTruthy();
    expect(group.getAttribute('aria-label')).toBe('Search kind');
    const radios = group.querySelectorAll('[role="radio"]');
    expect(radios.length).toBe(7);
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

  // ---- #217 S4 / I-3.2: Title + Files facet chips (5 → 7) ----

  it('renders seven kind chips including Title and Files', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const group = document.querySelector('[role="radiogroup"]')!;
    const radios = group.querySelectorAll('[role="radio"]');
    expect(radios.length).toBe(7);
    expect(screen.getByRole('radio', { name: 'Title' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: 'Files' })).toBeTruthy();
  });

  it('selecting Title / Files dispatches the kind', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    fireEvent.click(screen.getByRole('radio', { name: 'Title' }));
    expect(getState().conversationSearchKind).toBe('title');
    fireEvent.click(screen.getByRole('radio', { name: 'Files' }));
    expect(getState().conversationSearchKind).toBe('files');
  });

  it('Title and Files are NOT disabled under prose-only (needsSplit:false)', () => {
    // title → conversation_title_fts/LIKE, files → LIKE — neither rides the split
    // index, so the prose-only interim must NOT grey them out (unlike Tools/Thinking).
    searchHits = [hit({})];
    searchTotal = 1;
    searchDepth = 'prose-only';
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    expect((screen.getByRole('radio', { name: 'Title' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('radio', { name: 'Files' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('groups Title/Files after a separator distinct from the content kinds', () => {
    // 5f — the two structural facets are visually grouped after a subtle
    // separator. We assert the separator element exists between the content kinds
    // and the structural facets within the chip row.
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    render(<ConversationRail />);
    const group = document.querySelector('[role="radiogroup"]')!;
    const sep = group.querySelector('.conv-rail-chips-sep');
    expect(sep).toBeTruthy();
    // The separator precedes the Title chip and follows the Thinking chip in DOM order.
    const title = screen.getByRole('radio', { name: 'Title' });
    const thinking = screen.getByRole('radio', { name: 'Thinking' });
    expect(thinking.compareDocumentPosition(sep!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(sep!.compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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

  // ---- #217 S4 / I-3.3: Title + File hit rendering ----

  it('renders a title hit with a "title" badge and the matched-title snippet', () => {
    // kind=title → session-level hit, snippet = the matched title (FTS [..]
    // markers), match_kinds:["title"]. The badge map renders "title".
    searchHits = [hit({
      uuid: 'first-turn',
      title: 'how does the lock work',
      snippet: 'how does the [lock] work',
      match_kinds: ['title'],
    })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'lock' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'title' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    const badges = [...row.querySelectorAll('.conv-rail-kindb')].map((b) => b.textContent);
    expect(badges).toEqual(['title']);
    // The snippet renders the matched title (with the [..] highlight stripped to a mark).
    const snippet = row.querySelector('.conv-rail-row-snippet')!;
    expect(snippet.textContent).toContain('how does the lock work');
  });

  it('renders a file hit with the path prominently + a "file" badge + secondary title', () => {
    // kind=files → file-path-prominent: the path is the primary line (file
    // styling), the session title is secondary, snippet = the plain path,
    // match_kinds:["file"], badge "file".
    searchHits = [hit({
      uuid: 'touch-anchor',
      title: 'the session about caching',
      snippet: 'bin/_lib_conversation_query.py',
      match_kinds: ['file'],
    })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'conversation_query' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'files' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    // The path is the prominent primary line, with file styling.
    const path = row.querySelector('.conv-rail-row-filepath')!;
    expect(path).toBeTruthy();
    expect(path.textContent).toContain('bin/_lib_conversation_query.py');
    // The session title is rendered secondary.
    const secondary = row.querySelector('.conv-rail-row-filetitle')!;
    expect(secondary).toBeTruthy();
    expect(secondary.textContent).toContain('the session about caching');
    // The "file" badge renders.
    const badges = [...row.querySelectorAll('.conv-rail-kindb')].map((b) => b.textContent);
    expect(badges).toEqual(['file']);
    // #217 S4 QA fix — the bottom snippet row is SUPPRESSED for a file hit: its
    // snippet IS the path, already shown on the filepath line, so it must not
    // render twice. Assert no .conv-rail-row-snippet AND the path appears once.
    expect(row.querySelector('.conv-rail-row-snippet')).toBeNull();
    const pathOccurrences =
      row.textContent!.split('bin/_lib_conversation_query.py').length - 1;
    expect(pathOccurrences).toBe(1);
  });

  it('MATCH_KIND_LABELS has an explicit entry for every badge kind', () => {
    // #223 item 3 — direct map coverage. The rendered-badge tests pass via
    // KindBadges' `?? b` fallback whether or not the entries exist; this reads
    // the map directly so a missing/stray entry fails loud. The four keys mirror
    // SearchHit['match_kinds'] ('tool' | 'thinking' | 'title' | 'file'); a new
    // match kind must add its label here too (the `satisfies` type enforces that
    // at build time, this catches stray/regressed entries at test time).
    expect(Object.keys(MATCH_KIND_LABELS).sort()).toEqual(['file', 'thinking', 'title', 'tool']);
    expect(MATCH_KIND_LABELS.title).toBe('title');
    expect(MATCH_KIND_LABELS.file).toBe('file');
  });

  it('a prose hit STILL renders its snippet row (the file-hit suppression is file-only)', () => {
    // A content (prose) hit keeps the #177 S6 layout: a title-prominent line PLUS
    // the matched-prose snippet row. The file-hit snippet suppression must NOT
    // touch it. Non-vacuity guard for the Finding-2 fix.
    searchHits = [hit({
      uuid: 'prose-hit',
      title: 'how does the lock work',
      snippet: 'the [flock] serializes writers',
      match_kinds: [],
    })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    // No file styling, and the snippet row IS present.
    expect(row.querySelector('.conv-rail-row-filepath')).toBeNull();
    const snippet = row.querySelector('.conv-rail-row-snippet');
    expect(snippet).not.toBeNull();
    expect(snippet!.textContent).toContain('flock serializes writers');
  });

  it('a title hit STILL renders its snippet row (file-hit suppression does not touch it)', () => {
    searchHits = [hit({
      uuid: 'first-turn',
      title: 'how does the lock work',
      snippet: 'how does the [lock] work',
      match_kinds: ['title'],
    })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'lock' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'title' });
    render(<ConversationRail />);
    const row = document.querySelector('.conv-rail-row--hit')!;
    expect(row.querySelector('.conv-rail-row-snippet')).not.toBeNull();
  });

  it('a title hit click navigates to the session first-turn anchor (hit.uuid)', () => {
    searchHits = [hit({ session_id: 's7', uuid: 'first-turn', match_kinds: ['title'] })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'lock' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'title' });
    render(<ConversationRail />);
    fireEvent.click(document.querySelector('.conv-rail-row--hit') as HTMLButtonElement);
    expect(getState().selectedConversationId).toBe('s7');
    expect(getState().conversationJump).toEqual({ session_id: 's7', uuid: 'first-turn' });
  });

  it('a file hit click navigates to the most-recent-touch anchor (hit.uuid)', () => {
    searchHits = [hit({ session_id: 's9', uuid: 'touch-anchor', match_kinds: ['file'] })];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'query' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'files' });
    render(<ConversationRail />);
    fireEvent.click(document.querySelector('.conv-rail-row--hit') as HTMLButtonElement);
    expect(getState().selectedConversationId).toBe('s9');
    expect(getState().conversationJump).toEqual({ session_id: 's9', uuid: 'touch-anchor' });
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

  it('keeps Filters enabled while searching and KEEPS chips visible (#217 S4 / I-2.5)', () => {
    // Filters now apply to BOTH browse and search (the shared-filter decision),
    // so the button stays enabled and the active chips stay rendered in search
    // mode — the prior browse-only behavior is REVERSED.
    render(<ConversationRail />);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 1 } }));
    expect(screen.queryByText(/≥1/)).toBeTruthy();         // chip visible while browsing
    act(() => dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'hello' }));
    expect((screen.getByRole('button', { name: /filters/i }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText(/≥1/)).toBeTruthy();         // chip STILL visible while searching
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

describe('ConversationRail sort control (#217 S4 / I-2.4)', () => {
  it('renders a sort <select> with the five rail sort options', () => {
    render(<ConversationRail />);
    const sel = screen.getByLabelText(/sort conversations/i) as HTMLSelectElement;
    expect(sel).toBeTruthy();
    const values = Array.from(sel.options).map((o) => o.value);
    expect(values).toEqual(['recent', 'oldest', 'cost', 'messages', 'project']);
  });

  it('changing the sort dispatches SET_CONVERSATION_RAIL_SORT', () => {
    render(<ConversationRail />);
    const sel = screen.getByLabelText(/sort conversations/i) as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: 'cost' } });
    expect(getState().conversationRailSort).toBe('cost');
  });

  it('reflects the current conversationRailSort in the select value', () => {
    act(() => dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'project' }));
    render(<ConversationRail />);
    const sel = screen.getByLabelText(/sort conversations/i) as HTMLSelectElement;
    expect(sel.value).toBe('project');
  });

  it('shows a "sort unavailable while indexing" hint when sortDegraded', () => {
    sortDegraded = true;
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-sort-degraded')).toBeTruthy();
    expect(document.querySelector('.conv-rail-sort-degraded')!.textContent)
      .toMatch(/indexing/i);
  });

  it('does NOT show the sort hint when not degraded', () => {
    sortDegraded = false;
    render(<ConversationRail />);
    expect(document.querySelector('.conv-rail-sort-degraded')).toBeNull();
  });
});

describe('ConversationRail filtered search (#217 S4 / I-2.5)', () => {
  it('keeps the Filters button enabled while a needle is active', () => {
    searchHits = [hit({})];
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const btn = screen.getByRole('button', { name: /filters/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.title).toBe('Filter conversations');
  });

  it('renders the Filters popover + active chips in search mode', () => {
    searchHits = [hit({})];
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { projects: ['projA'] } });
    render(<ConversationRail />);
    expect(screen.getByTestId('filters-popover')).toBeTruthy();
    // The active-filter chip row renders in search mode too.
    expect(document.querySelector('.conv-rail-filters-active')).toBeTruthy();
  });

  it('surfaces the search filter_degraded hint', () => {
    searchHits = [hit({})];
    searchFilterDegraded = true;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const note = document.querySelector('.conv-rail-search-filters-degraded');
    expect(note).toBeTruthy();
    expect(note!.textContent).toMatch(/filters/i);
  });
});

// #217 S7 F10 — comparison pick-mode.
describe('ConversationRail compare pick-mode (#217 S7 F10)', () => {
  it('shows the aria-live pick banner with the anchor when comparePick is set', () => {
    browseRows = [summary({ session_id: 's2', title: 'pick me' })];
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'anchorsid-1234' });
    render(<ConversationRail />);
    const banner = document.querySelector('.conv-rail-pickbanner')!;
    expect(banner).toBeTruthy();
    expect(banner.getAttribute('aria-live')).toBe('polite');
    expect(banner.textContent).toMatch(/pick a session/i);
    // the anchor is shown (short form)
    expect(banner.textContent).toContain('anchorsi');
  });

  it('Cancel in the banner dispatches CANCEL_COMPARE_PICK', () => {
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    render(<ConversationRail />);
    fireEvent.click(screen.getByRole('button', { name: /cancel comparison pick/i }));
    expect(getState().comparePick).toBeNull();
  });

  it('clicking a NON-anchor browse row in pick-mode dispatches OPEN_COMPARE, not SELECT_CONVERSATION', () => {
    browseRows = [summary({ session_id: 'B', title: 'pick me' })];
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    render(<ConversationRail />);
    fireEvent.click(screen.getByText('pick me'));
    expect(getState().compare).toEqual({ a: 'A', b: 'B' });
    // selection was NOT set by a SELECT_CONVERSATION
    expect(getState().selectedConversationId).toBe('A'); // OPEN_COMPARE sets the anchor
  });

  it('the anchor row is disabled (non-pickable) in pick-mode', () => {
    browseRows = [summary({ session_id: 'A', title: 'the anchor' })];
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    render(<ConversationRail />);
    const row = screen.getByText('the anchor').closest('button') as HTMLButtonElement;
    expect(row.disabled).toBe(true);
  });
});

// #228 S1 (§6c) — cross-session search states must be announced. The error
// branch is an alert (assertive); the "Searching…" branch is a status (polite).
// The hit-count div keeps its existing aria-live="polite"; the three branches
// are mutually exclusive, so there is no double-announce.
describe('ConversationRail search-state a11y (#228 S1 §6c)', () => {
  it('the search error branch carries role="alert"', () => {
    searchError = "Search failed.";
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const el = document.querySelector('.conv-rail-empty[role="alert"]');
    expect(el).toBeTruthy();
    expect(el!.textContent).toContain('Search failed.');
  });

  it('the "Searching…" loading branch carries role="status"', () => {
    searchLoading = true;
    searchHits = [];
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const el = document.querySelector('.conv-rail-empty[role="status"]');
    expect(el).toBeTruthy();
    expect(el!.textContent).toContain('Searching…');
  });

  it('the hit-count div keeps its aria-live="polite" (no role on the count)', () => {
    searchHits = [hit({})];
    searchTotal = 1;
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    render(<ConversationRail />);
    const count = document.querySelector('.conv-rail-count');
    expect(count).toBeTruthy();
    expect(count).toHaveAttribute('aria-live', 'polite');
  });
});
