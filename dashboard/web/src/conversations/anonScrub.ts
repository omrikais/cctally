// #281 S4 — the client-side anonymization applier. A DUMB executor of the wire
// plan the server ships at GET /api/conversation/<sid>/anon-map (plan_to_wire):
// NO pattern logic lives here — the single source of truth is the Python kernel
// bin/_lib_conversation_anon.py, and the generated parity fixture keeps this TS
// applier in lockstep (anonScrub.test.ts). Used only for per-card COPY; the
// Export menu fetches the server-scrubbed body directly.

import { conversationEntityUrl } from '../lib/conversationTransport';
import { conversationRefKey, normalizeConversationRef, type ConversationRefInput } from '../types/conversation';

export interface AnonWirePlan {
  tokens: { text: string; replacement: string; bounded: boolean }[];
  patterns: { name: string; source: string; ignoreCase: boolean; keepGroup1: boolean }[];
}

// Shared Python/JS boundary class for bounded tokens (deliberately NOT \b).
const B = 'A-Za-z0-9_.-';
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Identity single-pass alternation, then secrets — mirrors scrub_text. May throw
// if a wire pattern is not a valid JS RegExp; the caller (per-card copy) treats a
// throw as fail-closed (clipboard untouched, error state) — never a raw write.
export function scrubText(text: string, plan: AnonWirePlan): string {
  if (plan.tokens.length) {
    const alt = plan.tokens
      .map((t) => (t.bounded ? `(?<![${B}])${esc(t.text)}(?![${B}])` : esc(t.text)))
      .join('|');
    const map = new Map(plan.tokens.map((t) => [t.text, t.replacement]));
    text = text.replace(new RegExp(alt, 'g'), (m) => map.get(m) ?? '(unknown)');
  }
  for (const p of plan.patterns) {
    const re = new RegExp(p.source, p.ignoreCase ? 'gi' : 'g');
    text = text.replace(re, (_m, g1) => {
      const prefix = p.keepGroup1 && typeof g1 === 'string' ? g1 : '';
      return `${prefix}[REDACTED:${p.name}]`;
    });
  }
  return text;
}

// Validate the wire shape before trusting it (fail-closed on malformed data).
function assertWirePlan(w: unknown): AnonWirePlan {
  const o = w as AnonWirePlan;
  if (!o || !Array.isArray(o.tokens) || !Array.isArray(o.patterns)) {
    throw new Error('malformed anon-map');
  }
  return o;
}

// Per-session plan cache: one in-flight fetch shared by concurrent per-card
// copies of the same session; a REJECTED fetch is evicted so a later click can
// retry (never a permanently-cached failure). A session switch just fetches a
// different key — the awaiting caller re-checks the current session before it
// writes the clipboard (fail-closed against a stale response).
const planCache = new Map<string, Promise<AnonWirePlan>>();

export function fetchAnonPlan(rawRef: ConversationRefInput): Promise<AnonWirePlan> {
  const conversationRef = normalizeConversationRef(rawRef);
  const cacheKey = conversationRefKey(conversationRef);
  let p = planCache.get(cacheKey);
  if (!p) {
    p = fetch(conversationEntityUrl(conversationRef, 'anon-map')).then(
      async (res) => {
        if (!res.ok) throw new Error(`anon-map ${res.status}`);
        return assertWirePlan(await res.json());
      },
    );
    p.catch(() => {
      // Evict only if this exact rejected promise is still the cached one.
      if (planCache.get(cacheKey) === p) planCache.delete(cacheKey);
    });
    planCache.set(cacheKey, p);
  }
  return p;
}

// Test seam — clear the module-level cache between cases.
export function __clearAnonPlanCache(): void {
  planCache.clear();
}
