import {
  conversationRefKey,
  normalizeConversationRef,
  type ConversationRefInput,
  type ReadingPos,
  type ReadingPosMap,
} from '../types/conversation';

// #217 S3 E1 — anchor-based reading-position memory. A small persistence module
// over localStorage that records the current-turn uuid per qualified conversation as a bounded
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

// #217 S3 E1 (P2) — leading-edge throttle. The scroll-sync IntersectionObserver
// fires on every visible-turn change, and the reducer dispatches a
// SET_CONV_CURRENT_TURN per fire, so a fast scroll would hammer synchronous
// localStorage on every tick. We persist at most once per session per window:
// the FIRST write of a burst lands immediately (so the latest-position-before-a-
// switch invariant — Codex P2 — still holds; a single write per session always
// goes through), and subsequent same-session writes inside the window are
// dropped. The throttle clock is the call's own `ts` (defaulting to Date.now()),
// so deterministic tests passing explicit, well-separated `ts` values are never
// suppressed. Keyed PER SESSION so distinct conversations never throttle each
// other. `__resetReadingPosThrottle` clears the in-memory clock for tests.
export const READING_POS_THROTTLE_MS = 1000;
const _lastWriteTs = new Map<string, number>();
export function __resetReadingPosThrottle(): void { _lastWriteTs.clear(); }

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

// Record (or refresh) the reading position for one conversation. `ts` defaults to
// `Date.now()` (the LRU recency key); pass an explicit `ts` in tests for
// determinism. Re-writing an existing session refreshes its recency so it isn't
// evicted as "old". Caller throttles the call cadence (the scroll-sync observer
// fires often).
export function recordReadingPos(refInput: ConversationRefInput, uuid: string, ts: number = Date.now()): void {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key || !uuid) return;
  const identityKey = conversationRefKey(ref);
  // Leading-edge per-session throttle: drop a same-session write inside the
  // window (the first write of a burst already landed). The latest position
  // still survives a switch because a switch is preceded by at least one write
  // (Codex P2), and distinct sessions never throttle each other.
  const prev = _lastWriteTs.get(identityKey);
  if (prev !== undefined && ts - prev < READING_POS_THROTTLE_MS) return;
  _lastWriteTs.set(identityKey, ts);
  const map = readMap();
  map[identityKey] = { uuid, ts };
  // Legacy storage was keyed by the bare Claude session id. Production now
  // passes a ConversationRef object, so source—not input syntax—controls the
  // compatibility migration. Never probe/delete a bare key for Codex.
  if (ref.source === 'claude') delete map[ref.key];
  writeMap(evictLru(map, READING_POS_CAP));
}

// Read the saved reading position for one conversation (open-precedence slot 2), or
// null if none. Pure read — does NOT bump recency (a restore is a read, and the
// next scroll-sync write refreshes recency anyway).
export function loadReadingPos(refInput: ConversationRefInput): ReadingPos | null {
  const ref = normalizeConversationRef(refInput);
  if (!ref.key) return null;
  const map = readMap();
  return map[conversationRefKey(ref)] ?? (ref.source === 'claude' ? map[ref.key] : undefined) ?? null;
}

// Test/maintenance helper — clear the entire map (and the throttle clock so a
// later record in the same test/tick is not suppressed by a stale window).
export function clearReadingPositions(): void {
  try { localStorage.removeItem(READING_POS_KEY); } catch { /* ignore */ }
  __resetReadingPosThrottle();
}
