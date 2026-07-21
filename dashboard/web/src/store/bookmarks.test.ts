import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  BOOKMARKS_KEY, BOOKMARKS_CAP, clearBookmarks, loadBookmarks,
  toggleBookmark, setBookmarkNote, removeBookmark,
} from './bookmarks';

beforeEach(() => clearBookmarks());
afterEach(() => clearBookmarks());

describe('bookmarks persistence (#217 S6 F4)', () => {
  it('keeps same-native-id provider/root references independent', () => {
    const claude = { source: 'claude', key: 'same' } as const;
    const codexA = { source: 'codex', key: 'v1.root-a-same' } as const;
    const codexB = { source: 'codex', key: 'v1.root-b-same' } as const;
    toggleBookmark(claude as never, 'u', 1000);
    setBookmarkNote(codexA as never, 'u', 'root A', 2000);
    setBookmarkNote(codexB as never, 'u', 'root B', 3000);
    expect(loadBookmarks(claude as never).u.note).toBe('');
    expect(loadBookmarks(codexA as never).u.note).toBe('root A');
    expect(loadBookmarks(codexB as never).u.note).toBe('root B');
  });

  it('toggles a bookmark on and off, persisting per session', () => {
    expect(loadBookmarks('s1')).toEqual({});
    toggleBookmark('s1', 'u1', 1000);
    expect(loadBookmarks('s1')).toEqual({ u1: { note: '', ts: 1000 } });
    toggleBookmark('s1', 'u1', 2000);
    expect(loadBookmarks('s1')).toEqual({});
  });
  it('sets and updates a note (implies bookmarked)', () => {
    setBookmarkNote('s1', 'u1', 'check this', 1000);
    expect(loadBookmarks('s1').u1.note).toBe('check this');
    setBookmarkNote('s1', 'u1', 'revised', 2000);
    expect(loadBookmarks('s1').u1.note).toBe('revised');
  });
  it('removeBookmark clears one entry', () => {
    toggleBookmark('s1', 'u1', 1000); toggleBookmark('s1', 'u2', 1001);
    removeBookmark('s1', 'u1');
    expect(loadBookmarks('s1')).toEqual({ u2: { note: '', ts: 1001 } });
  });
  it('keeps sessions independent and caps sessions at BOOKMARKS_CAP (oldest by max ts evicted)', () => {
    for (let i = 0; i < BOOKMARKS_CAP; i++) toggleBookmark(`s${i}`, 'u', 1000 + i);
    toggleBookmark('sNew', 'u', 9_999_999);
    expect(loadBookmarks('s0')).toEqual({}); // evicted
    expect(loadBookmarks('sNew').u).toBeTruthy();
  });
  it('drops malformed stored values on load (non-string note / non-finite ts)', () => {
    localStorage.setItem(BOOKMARKS_KEY, JSON.stringify({ s1: { u1: { note: 42, ts: 'x' }, u2: { note: 'ok', ts: 5 } } }));
    expect(loadBookmarks('s1')).toEqual({ u2: { note: 'ok', ts: 5 } });
  });
  it('tolerates corrupt localStorage (bad JSON) by reading empty', () => {
    localStorage.setItem(BOOKMARKS_KEY, '{not json');
    expect(loadBookmarks('s1')).toEqual({});
  });
  it('reads a legacy bare-Claude entry and rewrites it under the qualified key', () => {
    localStorage.setItem(BOOKMARKS_KEY, JSON.stringify({ legacy: { u: { note: 'old', ts: 1 } } }));
    const ref = { source: 'claude', key: 'legacy' } as const;
    expect(loadBookmarks(ref).u.note).toBe('old');
    setBookmarkNote(ref, 'u', 'new', 2);
    const raw = JSON.parse(localStorage.getItem(BOOKMARKS_KEY)!);
    expect(raw.legacy).toBeUndefined();
    expect(raw['["claude","legacy"]'].u.note).toBe('new');
  });
});
