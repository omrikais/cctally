import type { ReadingPos, ReadingPosMap } from '../types/conversation';

// #217 S3 E1 — anchor-based reading-position memory. A small persistence module
// over localStorage that records the current-turn uuid per session as a bounded
// LRU map so it cannot grow unbounded, and restores it on the next open. The
// store wires `recordReadingPos` to the THROTTLED `SET_CONV_CURRENT_TURN`
// (persist-before-reset, Codex P2 — a genuine switch resets convCurrentTurnUuid
// in the reducer, so persisting "on switch" would save a just-cleared value);
// the reader reads `loadReadingPos` as open-precedence slot 2.

// New surface → the `cctally.*` namespace (NOT the legacy `ccusage.*` blob).
export const READING_POS_KEY = 'cctally.conv.readingPos';

// ~50 sessions kept; the oldest by `ts` is evicted past the cap so the entry can
// never grow unbounded. A round, generous bound — a reader rarely revisits more
// than a handful of conversations, and 50 anchors are a few KB at most.
export const READING_POS_CAP = 50;

function readMap(): ReadingPosMap {
  try {
    const raw = localStorage.getItem(READING_POS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as ReadingPosMap;
    }
  } catch {
    // Corrupt / unavailable localStorage (private mode, quota, bad JSON) →
    // start clean rather than crashing the reader.
  }
  return {};
}

function writeMap(map: ReadingPosMap): void {
  try {
    localStorage.setItem(READING_POS_KEY, JSON.stringify(map));
  } catch {
    // localStorage unavailable → the position just won't survive a reload.
  }
}

// Evict the oldest (smallest `ts`) entries until at most `cap` remain. Pure over
// the passed map (mutates a shallow copy is not needed — callers pass a fresh
// object), returns the trimmed map.
function evictLru(map: ReadingPosMap, cap: number): ReadingPosMap {
  const keys = Object.keys(map);
  if (keys.length <= cap) return map;
  // Sort by recency ascending (oldest first) and drop the head past the cap.
  keys.sort((a, b) => (map[a]?.ts ?? 0) - (map[b]?.ts ?? 0));
  const drop = keys.slice(0, keys.length - cap);
  for (const k of drop) delete map[k];
  return map;
}

// Record (or refresh) the reading position for one session. `ts` defaults to
// `Date.now()` (the LRU recency key); pass an explicit `ts` in tests for
// determinism. Re-writing an existing session refreshes its recency so it isn't
// evicted as "old". Caller throttles the call cadence (the scroll-sync observer
// fires often).
export function recordReadingPos(sessionId: string, uuid: string, ts: number = Date.now()): void {
  if (!sessionId || !uuid) return;
  const map = readMap();
  map[sessionId] = { uuid, ts };
  writeMap(evictLru(map, READING_POS_CAP));
}

// Read the saved reading position for one session (open-precedence slot 2), or
// null if none. Pure read — does NOT bump recency (a restore is a read, and the
// next scroll-sync write refreshes recency anyway).
export function loadReadingPos(sessionId: string): ReadingPos | null {
  if (!sessionId) return null;
  const map = readMap();
  return map[sessionId] ?? null;
}

// Test/maintenance helper — clear the entire map.
export function clearReadingPositions(): void {
  try { localStorage.removeItem(READING_POS_KEY); } catch { /* ignore */ }
}
