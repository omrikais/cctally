// Client-only URL deep-linking for the conversation reader (#169, closes B3).
// Pure grammar here; the store<->URL glue is installUrlRouting below.
//
// Canonical hash grammar (path-style):
//   ''                                                        -> dashboard
//   '#/conversations'                                         -> no selection
//   '#/conversations/source/<source>/<key>[/<turn>]'           -> one conversation
//   '#/conversations/compare/<source>/<key>/<source>/<key>'    -> comparison
// Bare `/<sid>[/<turn>]` and `/compare/<A>/<B>` forms remain read-compatible
// Claude aliases, but every production write uses the qualified grammar.
// Segment values are encode/decode-wrapped so a future non-URL-safe id is safe;
// decode∘encode is identity on today's tokens, so a dispatched jump uuid still
// matches the raw data-uuid the reader scrolls to. The `compare` literal is the
// first segment, so `compare` is reserved as a session id in this grammar (no
// real session is named "compare" with two trailing segments).

import {
  getState as realGetState,
  subscribeStore as realSubscribeStore,
  dispatch as realDispatch,
} from './store';
import type { Action, UIState } from './store';
import {
  conversationJumpRef,
  conversationRefKey,
  isConversationRef,
  legacyClaudeConversationRef,
  normalizeConversationRef,
  sameConversationRef,
  type ConversationRef,
} from '../types/conversation';

export interface Route {
  sessionId: string | null;
  conversationRef?: ConversationRef | null;
  turnUuid: string | null;
  // #217 S7 F10 — set ONLY for the compare route; null for every single-session
  // / dashboard route. A route never carries both a sessionId and a compare.
  compare: { a: ConversationRef; b: ConversationRef } | null;
  qualified?: boolean;
}

const PREFIX = '#/conversations';

export function parseHash(hash: string): Route | null {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash; // strip one leading '#'
  // #228 S3 F4 — read-tolerance alias: the SINGULAR `/conversation/<id>` form the
  // issue literally writes is normalized to the canonical plural `/conversations/`
  // before any matching. Only the bare `/conversation` segment (end-of-string or
  // followed by `/`) is rewritten — `/conversations…` (already plural) is left
  // untouched, and `/conversationfoo` (not a full segment) does NOT match.
  const h =
    raw === '/conversation' || raw.startsWith('/conversation/')
      ? '/conversations' + raw.slice('/conversation'.length)
      : raw;
  if (h === '' || h === '/') return null; // dashboard
  if (h === '/conversations' || h === '/conversations/') {
    return { sessionId: null, turnUuid: null, compare: null }; // conversations, no selection
  }
  if (!h.startsWith('/conversations/')) return null; // unknown route -> dashboard (optimistic)
  const segs = h.slice('/conversations/'.length).split('/').filter((s) => s.length > 0);
  const source = (value: string | undefined): ConversationRef['source'] | null =>
    value === 'claude' || value === 'codex' ? value : null;
  // #321 Task A — canonical qualified single-conversation route. The key stays
  // an opaque segment; decoding is solely URL transport, never key parsing.
  if (segs[0] === 'source' && (segs.length === 3 || segs.length === 4)) {
    const qualifiedSource = source(segs[1]);
    if (!qualifiedSource || !segs[2]) return null;
    const conversationRef = { source: qualifiedSource, key: decodeURIComponent(segs[2]) };
    return {
      sessionId: qualifiedSource === 'claude' ? conversationRef.key : null,
      conversationRef,
      turnUuid: segs[3] ? decodeURIComponent(segs[3]) : null,
      compare: null,
      qualified: true,
    };
  }
  // Qualified comparison writer. Equal opaque keys remain distinct when their
  // sources differ; no delimiter is interpreted inside either decoded key.
  if (segs[0] === 'compare' && segs.length === 5) {
    const aSource = source(segs[1]);
    const bSource = source(segs[3]);
    if (!aSource || !bSource || !segs[2] || !segs[4]) return null;
    return {
      sessionId: null,
      conversationRef: null,
      turnUuid: null,
      compare: {
        a: { source: aSource, key: decodeURIComponent(segs[2]) },
        b: { source: bSource, key: decodeURIComponent(segs[4]) },
      },
      qualified: true,
    };
  }
  // #217 S7 F10 — compare route: `compare/<A>/<B>`. Matched BEFORE the
  // single-session arms so `compare` never reads as a session id.
  if (segs[0] === 'compare' && segs.length >= 3 && segs[1] && segs[2]) {
    return {
      sessionId: null, conversationRef: null, turnUuid: null,
      compare: {
        a: legacyClaudeConversationRef(decodeURIComponent(segs[1])),
        b: legacyClaudeConversationRef(decodeURIComponent(segs[2])),
      },
    };
  }
  if (segs.length === 1) return { sessionId: decodeURIComponent(segs[0]), turnUuid: null, compare: null };
  if (segs.length === 2) {
    return { sessionId: decodeURIComponent(segs[0]), turnUuid: decodeURIComponent(segs[1]), compare: null };
  }
  return null; // 3+ segments (non-compare) -> malformed -> dashboard
}

// Overloaded: accepts EITHER a Route object (the write-back path, which may carry
// a compare) OR the legacy positional `(sessionId, turnUuid?)` form (permalink /
// reflect / baseHash callers).
export function formatHash(route: Route): string;
export function formatHash(ref: ConversationRef, turnUuid?: string | null): string;
export function formatHash(sessionId: string | null, turnUuid?: string | null): string;
export function formatHash(arg: Route | ConversationRef | string | null, turnUuid?: string | null): string {
  if (isConversationRef(arg)) {
    const base = `${PREFIX}/source/${arg.source}/${encodeURIComponent(arg.key)}`;
    return turnUuid ? `${base}/${encodeURIComponent(turnUuid)}` : base;
  }
  if (arg !== null && typeof arg === 'object') {
    const route = arg;
    if (route.compare) {
      const a = normalizeConversationRef(route.compare.a);
      const b = normalizeConversationRef(route.compare.b);
      return `${PREFIX}/compare/${a.source}/${encodeURIComponent(a.key)}/${b.source}/${encodeURIComponent(b.key)}`;
    }
    return route.conversationRef
      ? formatHash(route.conversationRef, route.turnUuid)
      : formatHash(route.sessionId, route.turnUuid);
  }
  const sessionId = arg;
  if (sessionId === null) return PREFIX; // '#/conversations'
  const sid = encodeURIComponent(sessionId);
  if (turnUuid) return `${PREFIX}/${sid}/${encodeURIComponent(turnUuid)}`;
  return `${PREFIX}/${sid}`;
}

export function permalinkUrl(
  origin: string,
  pathname: string,
  conversation: string | ConversationRef,
  turnUuid: string,
): string {
  return `${origin}${pathname}${isConversationRef(conversation)
    ? formatHash(conversation, turnUuid)
    : formatHash(conversation, turnUuid)}`;
}

export interface UrlRoutingDeps {
  getState: () => UIState;
  subscribeStore: (fn: () => void) => () => void;
  dispatch: (action: Action) => void;
}

// Conversation-level hash WITHOUT a turn segment.
function baseHash(view: UIState['view'], ref: ConversationRef | null): string {
  if (view === 'dashboard') return '';
  return ref ? formatHash(ref) : formatHash(null);
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
export function reflectTurnUrl(conversation: string | ConversationRef, uuid: string): void {
  writeUrl(isConversationRef(conversation) ? formatHash(conversation, uuid) : formatHash(conversation, uuid), 'replace');
}

// Read path: parse the current hash and dispatch the matching action(s).
function applyHashToStore(deps: UrlRoutingDeps): void {
  const route = parseHash(window.location.hash);
  if (route === null) {
    deps.dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    return;
  }
  // #217 S7 F10 — compare route: enter the comparison (A===B degrades to a plain
  // single-session open, matching the OPEN_COMPARE store guard).
  if (route.compare) {
    if (sameConversationRef(route.compare.a, route.compare.b)) {
      deps.dispatch(route.qualified
        ? { type: 'OPEN_CONVERSATION', conversationRef: route.compare.a }
        : { type: 'OPEN_CONVERSATION', sessionId: route.compare.a.key });
    } else {
      deps.dispatch(route.qualified
        ? { type: 'OPEN_COMPARE', aRef: route.compare.a, bRef: route.compare.b }
        : { type: 'OPEN_COMPARE', a: route.compare.a.key, b: route.compare.b.key });
    }
    return;
  }
  const conversationRef = route.conversationRef
    ?? (route.sessionId ? legacyClaudeConversationRef(route.sessionId) : null);
  if (conversationRef === null) {
    // No single action sets view=conversations AND clears selection, so do both:
    // SET_VIEW preserves selection; SELECT_CONVERSATION doesn't touch view.
    deps.dispatch({ type: 'SET_VIEW', view: 'conversations' });
    deps.dispatch({ type: 'SELECT_CONVERSATION', sessionId: null });
    return;
  }
  const jump = route.turnUuid
    ? { ...(route.qualified ? { conversation_ref: conversationRef } : {}), session_id: conversationRef.key, uuid: route.turnUuid }
    : undefined;
  deps.dispatch(route.qualified
    ? { type: 'OPEN_CONVERSATION', conversationRef, jump }
    : { type: 'OPEN_CONVERSATION', sessionId: conversationRef.key, jump });
}

// Boot once, then wire the hashchange (URL->store) + subscribeStore (store->URL)
// listeners. Call at module scope in main.tsx. Returns a disposer (tests/prod-safe).
export function installUrlRouting(deps: UrlRoutingDeps = {
  getState: realGetState,
  subscribeStore: realSubscribeStore,
  dispatch: realDispatch,
}): () => void {
  // 0) #241 — opt out of the browser's native scroll restoration. The default
  // `'auto'` mode restores a session-history entry's saved scroll positions on a
  // reload, INCLUDING the conversation reader's inner `.conv-reader-body` overflow
  // scroller. That restore writes a STALE scrollTop (saved while the deep-linked
  // turn's owning subagent was force-OPEN; on reload it boots collapsed, so the
  // saved offset points at different content) and commonly lands AFTER the
  // deep-link jump pipeline's bounded convergence window — which never re-corrects
  // a post-settle external scroll, so the viewport sticks at the stale offset and
  // the target is lost (the subagent stays collapsed, never scrolled-to).
  // `'manual'` removes the ONLY production source of that late write, leaving the
  // app's own deep-link / restore / tail positioning as the sole driver of the
  // viewport. Assigned UNCONDITIONALLY (settable on every modern browser AND jsdom;
  // a bare assignment is harmless on the rare engine lacking the property — no `in`
  // guard, which would skip jsdom and break the regression test). The prior value
  // is restored by the disposer below so the install stays test-hermetic.
  const prevScrollRestoration = window.history.scrollRestoration;
  window.history.scrollRestoration = 'manual';

  // 1) Boot: reflect URL -> store BEFORE attaching listeners.
  applyHashToStore(deps);

  type Snap = {
    view: UIState['view'];
    ref: ConversationRef | null;
    refKey: string | null;
    jumpUuid: string | null;
    // Collision-safe serialized pair while a comparison is open, else null.
    cmp: string | null;
  };
  const snap = (): Snap => {
    const s = deps.getState();
    const ref = s.selectedConversationRef
      ?? (s.selectedConversationId ? legacyClaudeConversationRef(s.selectedConversationId) : null);
    return {
      view: s.view,
      ref,
      refKey: ref ? conversationRefKey(ref) : null,
      jumpUuid: s.conversationJump?.uuid ?? null,
      cmp: s.compare
        ? JSON.stringify([conversationRefKey(s.compare.a), conversationRefKey(s.compare.b)])
        : null,
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
    const jumpTargetsRef = !!s.conversationJump && sameConversationRef(conversationJumpRef(s.conversationJump), curr.ref);
    // #217 S7 F10 — comparison is the highest-priority URL state: while a
    // comparison is open, the hash is the compare route regardless of the
    // anchor sid OPEN_COMPARE also set. Push on entering/changing a comparison.
    if (curr.cmp && curr.cmp !== prev.cmp) {
      writeUrl(formatHash({ sessionId: null, turnUuid: null, compare: s.compare }), 'push');
      prev = curr;
      return;
    }
    if (curr.cmp) {
      // Comparison unchanged (a sibling state edit ticked the store) — never
      // overwrite the compare hash with the anchor's single-session hash.
      prev = curr;
      return;
    }
    // #217 S7 F10 — a comparison just closed/cleared (prev.cmp set, curr.cmp null).
    // CLOSE_COMPARE sets ONLY compare=null and leaves the anchor sid + view intact,
    // so the sid/view branch below would NOT fire and the URL would strand on the
    // stale compare route. Write the single-session/dashboard hash explicitly. The
    // reverse-clear actions (OPEN_CONVERSATION/SELECT_CONVERSATION/SET_VIEW) also
    // clear compare but move sid/view; routing them through here too keeps ONE
    // clear-write path — carry a jump if one rides along (e.g. an "open in reader"
    // that closes the comparison and lands on a specific turn).
    if (prev.cmp && !curr.cmp) {
      let desired = baseHash(curr.view, curr.ref);
      if (curr.view === 'conversations' && curr.ref && curr.jumpUuid && jumpTargetsRef) {
        desired = formatHash(curr.ref, curr.jumpUuid);
      }
      writeUrl(desired, 'push');
      prev = curr;
      return;
    }
    if (curr.view !== prev.view || curr.refKey !== prev.refKey) {
      // conversation-level change -> push (carry the turn if a jump rides along)
      let desired = baseHash(curr.view, curr.ref);
      if (curr.view === 'conversations' && curr.ref && curr.jumpUuid && jumpTargetsRef) {
        desired = formatHash(curr.ref, curr.jumpUuid);
      }
      writeUrl(desired, 'push');
    } else if (
      curr.ref &&
      curr.refKey === prev.refKey &&
      curr.jumpUuid &&
      curr.jumpUuid !== prev.jumpUuid &&
      jumpTargetsRef
    ) {
      // jump within the same conversation -> replace (covers u1 -> u2)
      writeUrl(formatHash(curr.ref, curr.jumpUuid), 'replace');
    }
    // else (jump-clear, search edits, unrelated state): no write.
    prev = curr;
  };
  const unsubscribe = deps.subscribeStore(onStoreChange);

  return () => {
    window.removeEventListener('hashchange', onHashChange);
    unsubscribe();
    // #241 — restore the pre-install scroll-restoration mode (test hermeticity;
    // prod never disposes, so 'manual' persists for the app's lifetime).
    window.history.scrollRestoration = prevScrollRestoration;
  };
}
