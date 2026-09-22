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

// #850 §4.9 — the HTTP failure of an anonymized request, carrying its status
// so a caller can tell the server's typed refusal from any other failure. Both
// failure paths threw a bare message before, which left the 409 and a 500
// indistinguishable and made the refusal disclosure impossible to render.
export class AnonRequestError extends Error {
  readonly status: number;
  readonly undecodableCwdRows: number | null;
  readonly ambiguousCwdRows: number | null;

  constructor(status: number, undecodableCwdRows: number | null, ambiguousCwdRows: number | null) {
    super(`anon request failed: ${status}`);
    this.name = 'AnonRequestError';
    this.status = status;
    this.undecodableCwdRows = undecodableCwdRows;
    this.ambiguousCwdRows = ambiguousCwdRows;
  }
}

export const ANON_UNAVAILABLE_STATUS = 409;

// The M5 viewer sentence, verbatim. The remedy is the rebuild that re-derives
// every Codex thread's `cwd` from JSON, which is why it is the only remedy
// named here.
export function anonUnavailableMessage(error: AnonRequestError): string {
  if (error.ambiguousCwdRows !== null) {
    return (
      `Anonymized copy is unavailable: ${error.ambiguousCwdRows} Codex project path(s) `
      + 'cannot be safely attributed to this account. Use a raw export or omit the account scope.'
    );
  }
  return (
    `Anonymized copy is unavailable: ${error.undecodableCwdRows ?? 0} Codex project path(s) `
    + 'could not be read. Run cctally cache-sync --source codex --rebuild.'
  );
}

// Read the typed refusal body of a 409. A body that is missing or malformed
// still refuses — the status alone is the decision — and reports no count.
export async function anonRequestErrorFor(res: {
  status: number; json?: () => Promise<unknown>;
}): Promise<AnonRequestError> {
  let rows: number | null = null;
  let ambiguousRows: number | null = null;
  if (res.status === ANON_UNAVAILABLE_STATUS && typeof res.json === 'function') {
    try {
      const body = (await res.json()) as {
        undecodable_cwd_rows?: unknown;
        ambiguous_cwd_rows?: unknown;
        reason?: unknown;
      };
      if (typeof body?.undecodable_cwd_rows === 'number') {
        rows = body.undecodable_cwd_rows;
      }
      if (body?.reason === 'ambiguous_account_provenance'
          && typeof body.ambiguous_cwd_rows === 'number') {
        ambiguousRows = body.ambiguous_cwd_rows;
      }
    } catch {
      /* a malformed refusal body is still a refusal */
    }
  }
  return new AnonRequestError(res.status, rows, ambiguousRows);
}

// Validate the wire shape before trusting it (fail-closed on malformed data).
function assertWirePlan(w: unknown): AnonWirePlan {
  const o = w as AnonWirePlan;
  if (!o || !Array.isArray(o.tokens) || !Array.isArray(o.patterns)) {
    throw new Error('malformed anon-map');
  }
  return o;
}

// Per-session plan cache: one IN-FLIGHT fetch shared by concurrent per-card
// copies of the same session. A session switch just fetches a different key —
// the awaiting caller re-checks the current session before it writes the
// clipboard (fail-closed against a stale response).
//
// #850 §4.9 — the cached promise is evicted when it SETTLES, fulfilled or
// rejected, not only when it rejects. A plan fetched while the store was
// healthy used to serve every later anonymized copy of the same conversation
// for the life of the page, so a thread corrupted after one successful copy was
// never observed and M5 was false for that page. Concurrent clicks during one
// flight still share one fetch, and every later copy issues a new request and
// observes the server's current decision, at the cost of one small GET per
// copy click. No client state stands in for the server's decision.
const planCache = new Map<string, Promise<AnonWirePlan>>();

export function fetchAnonPlan(rawRef: ConversationRefInput): Promise<AnonWirePlan> {
  const conversationRef = normalizeConversationRef(rawRef);
  const cacheKey = conversationRefKey(conversationRef);
  let p = planCache.get(cacheKey);
  if (!p) {
    p = fetch(conversationEntityUrl(conversationRef, 'anon-map')).then(
      async (res) => {
        if (!res.ok) throw await anonRequestErrorFor(res);
        return assertWirePlan(await res.json());
      },
    );
    const evict = () => {
      // Evict only if this exact promise is still the cached one.
      if (planCache.get(cacheKey) === p) planCache.delete(cacheKey);
    };
    p.then(evict, evict);
    planCache.set(cacheKey, p);
  }
  return p;
}

// Test seam — clear the module-level cache between cases.
export function __clearAnonPlanCache(): void {
  planCache.clear();
}
