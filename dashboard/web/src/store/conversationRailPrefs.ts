import type { ConversationFilters, RailSortKey } from '../types/conversation';
import { EMPTY_FILTERS } from '../types/conversation';

// #217 S4 / I-2.2 — rail discovery-prefs persistence. A single localStorage blob
// `cctally.conv.railPrefs = { filters, sort }` (following the readingPosition.ts
// JSON pattern) that flips the previous explicit session-only behavior: the
// browse filters AND the rail sort key now survive a reload. Validated on load
// against the EMPTY_FILTERS shape + the known RailSortKey set, with a fail-safe
// to `{ EMPTY_FILTERS, 'recent' }` on corruption / absence so a hand-edited or
// stale blob can never crash init or smuggle an unknown sort to the server.

// New surface → the `cctally.*` namespace (NOT the legacy `ccusage.*` blob).
export const RAIL_PREFS_KEY = 'cctally.conv.railPrefs';

export interface RailPrefs {
  filters: ConversationFilters;
  sort: RailSortKey;
}

const RAIL_SORT_KEYS: ReadonlySet<RailSortKey> = new Set<RailSortKey>([
  'recent', 'oldest', 'cost', 'messages', 'project',
]);

export function defaultRailPrefs(): RailPrefs {
  return { filters: EMPTY_FILTERS, sort: 'recent' };
}

function isRailSortKey(v: unknown): v is RailSortKey {
  return typeof v === 'string' && RAIL_SORT_KEYS.has(v as RailSortKey);
}

// Coerce an arbitrary parsed object to a valid ConversationFilters, taking ONLY
// the recognized keys with the right primitive shapes and falling back to the
// EMPTY_FILTERS default per axis. A corrupt/partial blob therefore yields a
// well-formed filter set rather than leaking unexpected fields into the request.
function coerceFilters(raw: unknown): ConversationFilters {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return EMPTY_FILTERS;
  const r = raw as Record<string, unknown>;
  const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
  const num = (v: unknown): number | null =>
    (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const strArr = (v: unknown): string[] =>
    (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
  return {
    dateFrom: str(r.dateFrom),
    dateTo: str(r.dateTo),
    datePreset: str(r.datePreset),
    projects: strArr(r.projects),
    costMin: num(r.costMin),
    costMax: num(r.costMax),
    rebuildMin: num(r.rebuildMin),
    // #278 Theme C — a blob persisted before the model axis existed has no
    // `models` key; strArr defaults it to [] so filterParams / the popover
    // never crash on `.map`/`.length` of undefined.
    models: strArr(r.models),
  };
}

export function loadRailPrefs(): RailPrefs {
  try {
    const raw = localStorage.getItem(RAIL_PREFS_KEY);
    if (!raw) return defaultRailPrefs();
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return defaultRailPrefs();
    }
    const p = parsed as Record<string, unknown>;
    return {
      filters: coerceFilters(p.filters),
      sort: isRailSortKey(p.sort) ? p.sort : 'recent',
    };
  } catch {
    // Corrupt / unavailable localStorage (private mode, quota, bad JSON) →
    // start clean rather than crashing the rail.
    return defaultRailPrefs();
  }
}

export function saveRailPrefs(prefs: RailPrefs): void {
  try {
    localStorage.setItem(RAIL_PREFS_KEY, JSON.stringify({
      filters: prefs.filters,
      sort: prefs.sort,
    }));
  } catch {
    // localStorage unavailable → the prefs just won't survive a reload.
  }
}

// Test/maintenance helper — clear the persisted blob.
export function clearRailPrefs(): void {
  try { localStorage.removeItem(RAIL_PREFS_KEY); } catch { /* ignore */ }
}
