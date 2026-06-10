// Client-only URL deep-linking for the conversation reader (#169, closes B3).
// Pure grammar here; the store<->URL glue is installUrlRouting below.
//
// Hash grammar (path-style, four states):
//   ''                              -> dashboard            (parseHash -> null)
//   '#/conversations'               -> conversations, no selection ({sessionId:null})
//   '#/conversations/<sid>'         -> a selected conversation
//   '#/conversations/<sid>/<turn>'  -> a specific turn
// Segment values are encode/decode-wrapped so a future non-URL-safe id is safe;
// decode∘encode is identity on today's tokens, so a dispatched jump uuid still
// matches the raw data-uuid the reader scrolls to.

export interface Route {
  sessionId: string | null;
  turnUuid: string | null;
}

const PREFIX = '#/conversations';

export function parseHash(hash: string): Route | null {
  const h = hash.startsWith('#') ? hash.slice(1) : hash; // strip one leading '#'
  if (h === '' || h === '/') return null; // dashboard
  if (h === '/conversations' || h === '/conversations/') {
    return { sessionId: null, turnUuid: null }; // conversations, no selection
  }
  if (!h.startsWith('/conversations/')) return null; // unknown route -> dashboard (optimistic)
  const segs = h.slice('/conversations/'.length).split('/').filter((s) => s.length > 0);
  if (segs.length === 1) return { sessionId: decodeURIComponent(segs[0]), turnUuid: null };
  if (segs.length === 2) {
    return { sessionId: decodeURIComponent(segs[0]), turnUuid: decodeURIComponent(segs[1]) };
  }
  return null; // 3+ segments -> malformed -> dashboard
}

export function formatHash(sessionId: string | null, turnUuid?: string | null): string {
  if (sessionId === null) return PREFIX; // '#/conversations'
  const sid = encodeURIComponent(sessionId);
  if (turnUuid) return `${PREFIX}/${sid}/${encodeURIComponent(turnUuid)}`;
  return `${PREFIX}/${sid}`;
}

export function permalinkUrl(
  origin: string,
  pathname: string,
  sessionId: string,
  turnUuid: string,
): string {
  return `${origin}${pathname}${formatHash(sessionId, turnUuid)}`;
}
