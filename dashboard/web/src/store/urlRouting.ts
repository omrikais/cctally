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

import {
  getState as realGetState,
  subscribeStore as realSubscribeStore,
  dispatch as realDispatch,
} from './store';
import type { Action, UIState } from './store';

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

export interface UrlRoutingDeps {
  getState: () => UIState;
  subscribeStore: (fn: () => void) => () => void;
  dispatch: (action: Action) => void;
}

// Conversation-level hash WITHOUT a turn segment.
function baseHash(view: UIState['view'], sid: string | null): string {
  if (view === 'dashboard') return '';
  return formatHash(sid); // sid null -> '#/conversations'; sid -> '#/conversations/<sid>'
}

// The single write chokepoint. Idempotent (no-op when already there); always
// pushState/replaceState (never `location.hash =`, which would fire hashchange).
function writeUrl(hash: string, mode: 'push' | 'replace'): void {
  if (hash === window.location.hash) return;
  // Bare dashboard hash: drop the fragment, keep path + query.
  const url = hash === '' ? window.location.pathname + window.location.search : hash;
  if (mode === 'push') window.history.pushState(null, '', url);
  else window.history.replaceState(null, '', url);
}

// Used by the permalink button: reflect the address bar to a turn WITHOUT
// dispatching a jump (no scroll/flash on a turn already under the cursor).
export function reflectTurnUrl(sessionId: string, uuid: string): void {
  writeUrl(formatHash(sessionId, uuid), 'replace');
}

// Read path: parse the current hash and dispatch the matching action(s).
function applyHashToStore(deps: UrlRoutingDeps): void {
  const route = parseHash(window.location.hash);
  if (route === null) {
    deps.dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    return;
  }
  if (route.sessionId === null) {
    // No single action sets view=conversations AND clears selection, so do both:
    // SET_VIEW preserves selection; SELECT_CONVERSATION doesn't touch view.
    deps.dispatch({ type: 'SET_VIEW', view: 'conversations' });
    deps.dispatch({ type: 'SELECT_CONVERSATION', sessionId: null });
    return;
  }
  const jump = route.turnUuid
    ? { session_id: route.sessionId, uuid: route.turnUuid }
    : undefined;
  deps.dispatch({ type: 'OPEN_CONVERSATION', sessionId: route.sessionId, jump });
}

// Boot once, then wire the hashchange (URL->store) + subscribeStore (store->URL)
// listeners. Call at module scope in main.tsx. Returns a disposer (tests/prod-safe).
export function installUrlRouting(deps: UrlRoutingDeps = {
  getState: realGetState,
  subscribeStore: realSubscribeStore,
  dispatch: realDispatch,
}): () => void {
  // 1) Boot: reflect URL -> store BEFORE attaching listeners.
  applyHashToStore(deps);

  type Snap = { view: UIState['view']; sid: string | null; jumpUuid: string | null };
  const snap = (): Snap => {
    const s = deps.getState();
    return {
      view: s.view,
      sid: s.selectedConversationId,
      jumpUuid: s.conversationJump?.uuid ?? null,
    };
  };
  let prev: Snap = snap(); // initialize from post-boot state -> no echo write

  // 2) Read path: hashchange fires only on real user nav (our writes are silent).
  const onHashChange = () => applyHashToStore(deps);
  window.addEventListener('hashchange', onHashChange);

  // 3) Reflect path: transition-gated store -> URL.
  const onStoreChange = () => {
    const s = deps.getState();
    const curr = snap();
    const jumpTargetsSid = s.conversationJump?.session_id === curr.sid;
    if (curr.view !== prev.view || curr.sid !== prev.sid) {
      // conversation-level change -> push (carry the turn if a jump rides along)
      let desired = baseHash(curr.view, curr.sid);
      if (curr.view === 'conversations' && curr.sid && curr.jumpUuid && jumpTargetsSid) {
        desired = formatHash(curr.sid, curr.jumpUuid);
      }
      writeUrl(desired, 'push');
    } else if (
      curr.sid &&
      curr.sid === prev.sid &&
      curr.jumpUuid &&
      curr.jumpUuid !== prev.jumpUuid &&
      jumpTargetsSid
    ) {
      // jump within the same conversation -> replace (covers u1 -> u2)
      writeUrl(formatHash(curr.sid, curr.jumpUuid), 'replace');
    }
    // else (jump-clear, search edits, unrelated state): no write.
    prev = curr;
  };
  const unsubscribe = deps.subscribeStore(onStoreChange);

  return () => {
    window.removeEventListener('hashchange', onHashChange);
    unsubscribe();
  };
}
