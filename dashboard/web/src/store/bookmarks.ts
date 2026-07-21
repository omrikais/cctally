// #217 S6 F4 — per-turn bookmarks/annotations: a bounded localStorage LRU keyed
// by qualified conversation identity, each value a map of bookmarked turn-uuid
// → { note, ts }. Mirrors
// readingPosition.ts (per-feature plain-function module, all localStorage calls
// in try/catch). Unlike readingPosition, loadBookmarks VALIDATES each stored
// value (note must be a string, ts finite) — notes are rendered, so a malformed
// blob must not reach the UI (Codex P2).

import {
  conversationRefKey,
  normalizeConversationRef,
  type ConversationRefInput,
} from '../types/conversation';

export const BOOKMARKS_KEY = 'cctally.conv.bookmarks';
export const BOOKMARKS_CAP = 50; // sessions kept; oldest (by max entry ts) evicted

export interface BookmarkEntry { note: string; ts: number; }
export type SessionBookmarks = Record<string, BookmarkEntry>;
type BookmarksMap = Record<string, SessionBookmarks>;

function coerceSession(raw: unknown): SessionBookmarks {
  const out: SessionBookmarks = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const [uuid, v] of Object.entries(raw as Record<string, unknown>)) {
    if (v && typeof v === 'object') {
      const note = (v as Record<string, unknown>).note;
      const ts = (v as Record<string, unknown>).ts;
      if (typeof note === 'string' && typeof ts === 'number' && Number.isFinite(ts)) {
        out[uuid] = { note, ts };
      }
    }
  }
  return out;
}

function readMap(): BookmarksMap {
  try {
    const raw = localStorage.getItem(BOOKMARKS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const out: BookmarksMap = {};
      for (const [sid, sess] of Object.entries(parsed as Record<string, unknown>)) {
        const coerced = coerceSession(sess);
        if (Object.keys(coerced).length) out[sid] = coerced;
      }
      return out;
    }
  } catch { /* corrupt/unavailable → empty */ }
  return {};
}

function writeMap(map: BookmarksMap): void {
  try { localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(map)); } catch { /* best effort */ }
}

// Recency for eviction = the session's most-recent entry ts.
function sessionRecency(s: SessionBookmarks): number {
  let m = 0;
  for (const e of Object.values(s)) m = Math.max(m, e.ts);
  return m;
}

function evictLru(map: BookmarksMap, cap: number): BookmarksMap {
  const keys = Object.keys(map);
  if (keys.length <= cap) return map;
  keys.sort((a, b) => sessionRecency(map[a]) - sessionRecency(map[b]));
  for (const k of keys.slice(0, keys.length - cap)) delete map[k];
  return map;
}

export function loadBookmarks(refInput: ConversationRefInput): SessionBookmarks {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key) return {};
  const map = readMap();
  return map[conversationRefKey(ref)] ?? (ref.source === 'claude' ? map[ref.key] : undefined) ?? {};
}

export function toggleBookmark(refInput: ConversationRefInput, uuid: string, ts: number = Date.now()): void {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key || !uuid) return;
  const map = readMap();
  const identityKey = conversationRefKey(ref);
  const sess = map[identityKey] ?? (ref.source === 'claude' ? map[ref.key] : undefined) ?? {};
  if (sess[uuid]) delete sess[uuid];
  else sess[uuid] = { note: '', ts };
  if (Object.keys(sess).length) map[identityKey] = sess; else delete map[identityKey];
  if (ref.source === 'claude') delete map[ref.key];
  writeMap(evictLru(map, BOOKMARKS_CAP));
}

export function setBookmarkNote(refInput: ConversationRefInput, uuid: string, note: string, ts: number = Date.now()): void {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key || !uuid) return;
  const map = readMap();
  const identityKey = conversationRefKey(ref);
  const sess = map[identityKey] ?? (ref.source === 'claude' ? map[ref.key] : undefined) ?? {};
  sess[uuid] = { note, ts };
  // A note always implies a populated session, so we unconditionally keep
  // map[sessionId] here. That's why this lacks toggleBookmark's empty-session
  // prune (`else delete map[sessionId]`): setting a note never empties the map.
  map[identityKey] = sess;
  if (ref.source === 'claude') delete map[ref.key];
  writeMap(evictLru(map, BOOKMARKS_CAP));
}

export function removeBookmark(refInput: ConversationRefInput, uuid: string): void {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key || !uuid) return;
  const map = readMap();
  const identityKey = conversationRefKey(ref);
  const sess = map[identityKey] ?? (ref.source === 'claude' ? map[ref.key] : undefined);
  if (!sess || !sess[uuid]) return;
  delete sess[uuid];
  if (Object.keys(sess).length) map[identityKey] = sess; else delete map[identityKey];
  if (ref.source === 'claude') delete map[ref.key];
  writeMap(map);
}

export function clearBookmarks(): void {
  try { localStorage.removeItem(BOOKMARKS_KEY); } catch { /* ignore */ }
}
