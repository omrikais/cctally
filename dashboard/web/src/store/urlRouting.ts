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
import { ALL_ACCOUNTS } from './accountFocus';
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

// IdentityV1 is opaque for storage and transport, but its source discriminator
// is the only authority available to the synchronous hash parser. Decode just
// enough of the versioned envelope to preserve provider identity on legacy
// one-segment deep links; malformed or non-conversation keys retain the legacy
// Claude-alias path and are rejected later by the existing entity endpoint.
function identityV1Source(key: string): ConversationRef['source'] | null {
  if (!key.startsWith('v1.')) return null;
  try {
    const raw = key.slice(3).replace(/-/g, '+').replace(/_/g, '/');
    const padded = raw + '='.repeat((4 - (raw.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
    const payload = JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
    if (payload.version !== 1 || payload.resourceKind !== 'conversation') return null;
    return payload.source === 'claude' || payload.source === 'codex' ? payload.source : null;
  } catch {
    return null;
  }
}

export function parseHash(hash: string): Route | null {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash; // strip one leading '#'
  const [rawPath, rawQuery = ''] = raw.split('?', 2);
  const accountParams = new URLSearchParams(rawQuery);
  // #228 S3 F4 — read-tolerance alias: the SINGULAR `/conversation/<id>` form the
  // issue literally writes is normalized to the canonical plural `/conversations/`
  // before any matching. Only the bare `/conversation` segment (end-of-string or
  // followed by `/`) is rewritten — `/conversations…` (already plural) is left
  // untouched, and `/conversationfoo` (not a full segment) does NOT match.
  const h =
    rawPath === '/conversation' || rawPath.startsWith('/conversation/')
      ? '/conversations' + rawPath.slice('/conversation'.length)
      : rawPath;
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
    const accountKey = accountParams.get('account') || undefined;
    const conversationRef = accountKey
      ? { source: qualifiedSource, key: decodeURIComponent(segs[2]), account_key: accountKey }
      : { source: qualifiedSource, key: decodeURIComponent(segs[2]) };
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
        a: accountParams.get('a_account')
          ? { source: aSource, key: decodeURIComponent(segs[2]), account_key: accountParams.get('a_account')! }
          : { source: aSource, key: decodeURIComponent(segs[2]) },
        b: accountParams.get('b_account')
          ? { source: bSource, key: decodeURIComponent(segs[4]), account_key: accountParams.get('b_account')! }
          : { source: bSource, key: decodeURIComponent(segs[4]) },
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
  if (segs.length === 1 || segs.length === 2) {
    const key = decodeURIComponent(segs[0]);
    const qualifiedSource = identityV1Source(key);
    if (qualifiedSource) {
      return {
        sessionId: qualifiedSource === 'claude' ? key : null,
        conversationRef: { source: qualifiedSource, key },
        turnUuid: segs[1] ? decodeURIComponent(segs[1]) : null,
        compare: null,
        qualified: true,
      };
    }
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
    const path = turnUuid ? `${base}/${encodeURIComponent(turnUuid)}` : base;
    return arg.account_key
      ? `${path}?account=${encodeURIComponent(arg.account_key)}`
      : path;
  }
  if (arg !== null && typeof arg === 'object') {
    const route = arg;
    if (route.compare) {
      const a = normalizeConversationRef(route.compare.a);
      const b = normalizeConversationRef(route.compare.b);
      const base = `${PREFIX}/compare/${a.source}/${encodeURIComponent(a.key)}/${b.source}/${encodeURIComponent(b.key)}`;
      const params = new URLSearchParams();
      if (a.account_key) params.set('a_account', a.account_key);
      if (b.account_key) params.set('b_account', b.account_key);
      return params.size ? `${base}?${params.toString()}` : base;
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

// #463 S5 (review F5) — every history entry this module writes carries this
// stamp, which is what lets a Back/Forward be told apart from a fresh fragment
// navigation. See the historyTraversalPending comment below for the measurement
// that made a stamp necessary.
const HISTORY_STAMP = 'cctallyRoute';
function isStampedEntry(state: unknown): boolean {
  return typeof state === 'object' && state !== null && HISTORY_STAMP in state;
}

// The single write chokepoint. Idempotent (no-op when already there); always
// pushState/replaceState (never `location.hash =`, which would fire hashchange).
function writeUrl(hash: string, mode: 'push' | 'replace'): void {
  if (hash === window.location.hash) return;
  // Bare dashboard hash: drop the fragment, keep path + query.
  const url = hash === '' ? window.location.pathname + window.location.search : hash;
  const state = { [HISTORY_STAMP]: true };
  if (mode === 'push') window.history.pushState(state, '', url);
  else window.history.replaceState(state, '', url);
}

// Used by the permalink button: reflect the address bar to a turn WITHOUT
// dispatching a jump (no scroll/flash on a turn already under the cursor).
export function reflectTurnUrl(conversation: string | ConversationRef, uuid: string): void {
  writeUrl(isConversationRef(conversation) ? formatHash(conversation, uuid) : formatHash(conversation, uuid), 'replace');
}

// #463 S5 (F24d) — the board state a conversation entry must establish BEFORE
// the conversation opens.
//
// Order is the correctness argument, twice over. SET_ACCOUNT_FOCUS clears
// selectedConversationId/Ref/conversationJump/compare/comparePick as a #347 side
// effect, so dispatching it after the open would erase the navigation target.
// And SET_ACTIVE_SOURCE alone is not enough: it recomputes only the dashboard
// grid search, so an active conversation search or filter can still exclude the
// target from the rail and leave no row marked current. The manual switch in
// ConversationRail.selectSource clears both for exactly that reason — this path
// copies that clearing, but NOT its null SELECT_CONVERSATION.
//
// The ALL_ACCOUNTS default is decided behavior, not a fallback of convenience: a
// permalink naming no account makes a claim about a CONVERSATION rather than
// about an account view, and ALL_ACCOUNTS is the only focus guaranteed to
// contain the target. It cannot violate #347's invariant, because a chip reading
// "all accounts" makes no claim the reader's content can contradict.
//
// Both dispatches persist — SET_ACTIVE_SOURCE through saveActiveSource and
// SET_ACCOUNT_FOCUS through saveAccountFocus — so following a cross-source,
// accountless permalink DURABLY changes the board's source and that source's
// account focus. That cost is accepted, not overlooked.
//
// NOT exported. `runConversationEntry` below is the only way to reach this
// batch, so a second entry point cannot dispatch it raw and reproduce the
// history defect the suppression exists to prevent (#463 S5 review F4:
// ComparisonView did exactly that).
function conversationEntryActions(
  ref: ConversationRef,
  options: ConversationEntryOptions = {},
): Action[] {
  const actions: Action[] = [
    { type: 'SET_ACTIVE_SOURCE', source: ref.source },
    // #556 S5 §5.1 — the batch above sets the ACTIVE SOURCE to `ref.source`, a
    // physical provider, so the view this entry establishes is that provider's
    // own tab and the focus belongs in its `provider` slot. A permalink never
    // lands on All, so it never writes an All slot.
    { type: 'SET_ACCOUNT_FOCUS', source: ref.source, slot: 'provider', account: ref.account_key ?? ALL_ACCOUNTS },
  ];
  // #463 S5 (review F5) — a Back/Forward traversal keeps the rail's own state.
  // `applyHashToStore` is the `hashchange` handler, and the reflector's pushes
  // make `hashchange` reachable by the Back button, so an unconditional clear
  // would discard a search and a filter set by the user moments earlier. Source
  // and account focus are still resolved, because the tab must be right.
  //
  // The accepted trade-off: on Back, a target excluded by the preserved filter
  // may have no current row in the rail. Preserving the user's own filter state
  // is judged the better behaviour, because the filter is something the user
  // typed and the current-row marker is not.
  if (!options.preserveRailState) {
    actions.push({ type: 'CLEAR_CONVERSATION_FILTERS' });
    actions.push({ type: 'SET_CONVERSATION_SEARCH', text: '' });
  }
  return actions;
}

// #463 S5 — true while a conversation entry is being applied. The entry batch
// above clears the selected ref whenever SET_ACCOUNT_FOCUS actually CHANGES the
// focus (the reducer no-ops on an unchanged value before it clears anything,
// store.ts), and the reflector is transition-gated, so without this it observes
// that transition and pushes `#/conversations` mid-batch — leaving a history
// entry the back button lands on and the user never visited. Measured before the
// fix: following an account-changing cross-source permalink pushed BOTH
// `#/conversations` and the target.
let applyingHash = false;

// #463 S5 (review F4) — registered by installUrlRouting. A suppressed batch ends
// with the reflector's snapshot stale and, for a caller that is NOT the hash
// reader, with the address bar still on the previous route. Calling the reflector
// once here settles both: it writes the settled hash (idempotently, so the hash
// reader writes nothing) and re-synchronizes the snapshot. Without it,
// ComparisonView's "open in reader" would strand the URL on the comparison route
// until some unrelated store change happened to tick the reflector.
//
// Caveat: this is a module global bound to the deps of the most recent install,
// whereas runConversationEntry receives its own `dispatch`. The two describe the
// same store only because the app installs routing exactly once over one store
// (main.tsx). A second concurrent install, or a caller dispatching into a
// different store, would settle the other one. Tests install and dispose one at
// a time for that reason.
let settleAfterEntry: (() => void) | null = null;

// #463 S5 (review F5) — set by the popstate listener, consumed by the next
// applyHashToStore.
//
// The naive form of this test — "a popstate arrived, therefore this is a Back" —
// is WRONG, and measuring the browser is the only way to find that out. Chromium
// (probed 2026-08-05 through the e2e harness) fires popstate BEFORE hashchange
// for a plain `location.hash = …` assignment, which is a FRESH navigation, and
// fires exactly the same pair for a Back. `pushState`/`replaceState` fire
// neither, so the app's own writes are invisible here either way.
//
// What does separate the two is the history STATE. A fresh fragment navigation
// appends a brand-new entry whose state is null; a Back/Forward restores an entry
// this module wrote earlier, which carries HISTORY_STAMP. Setting the flag from
// the stamp on EVERY popstate is also self-correcting, because a fresh navigation
// clears it rather than leaving a stale value behind.
//
// Known limit: an entry this module neither wrote nor adopted — a fragment the
// user typed into the address bar — is unstamped, so returning to it reads as a
// fresh navigation and clears the rail state. That is the conservative direction.
// The entry the app BOOTS on is no longer in that set: installUrlRouting stamps
// it in place (step 0b below), which is what makes the FIRST Back in a freshly
// opened tab behave like every later one.
//
// Two further caveats, neither reachable today. First, the flag is armed by
// popstate and consumed by applyHashToStore, so a popstate NOT followed by a
// hashchange would leave it armed and let the next navigation read as a
// traversal. Every entry this module writes differs from its neighbours in the
// FRAGMENT, so a traversal between two of them always produces a hashchange; an
// entry differing only in the path or query would break that assumption.
//
// Second, the discriminator assumes popstate is delivered BEFORE hashchange on a
// traversal, which is the ordering WHATWG specifies and Chromium implements. An
// engine ordering them the other way round would leave the flag unset when
// applyHashToStore reads it, which degrades to the pre-fix behaviour — Back
// clears the rail state — rather than misreading a fresh navigation as a Back.
let historyTraversalPending = false;

export interface ConversationEntryOptions {
  // Skip the rail's filter/search clearing (Back/Forward traversal only).
  preserveRailState?: boolean;
}

// #463 S5 (review F4) — THE single entry point for opening a conversation with
// its board resolved. Wraps the entry batch AND the open action in the
// suppression above, so no caller can leave the intermediate cleared-selection
// state visible to the reflector. Re-entrant: applyHashToStore already holds the
// suppression, and only the outermost call settles the reflector.
export function runConversationEntry(
  dispatch: (action: Action) => void,
  ref: ConversationRef,
  openAction: Action,
  options: ConversationEntryOptions = {},
): void {
  const wasApplying = applyingHash;
  applyingHash = true;
  try {
    for (const action of conversationEntryActions(ref, options)) dispatch(action);
    dispatch(openAction);
  } finally {
    applyingHash = wasApplying;
  }
  if (!wasApplying) settleAfterEntry?.();
}

// Read path: parse the current hash and dispatch the matching action(s).
function applyHashToStore(deps: UrlRoutingDeps): void {
  const options: ConversationEntryOptions = { preserveRailState: historyTraversalPending };
  historyTraversalPending = false;
  applyingHash = true;
  try {
    applyHashToStoreInner(deps, options);
  } finally {
    applyingHash = false;
  }
}

function applyHashToStoreInner(deps: UrlRoutingDeps, options: ConversationEntryOptions): void {
  const route = parseHash(window.location.hash);
  if (route === null) {
    deps.dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    return;
  }
  // #217 S7 F10 — compare route: enter the comparison (A===B degrades to a plain
  // single-session open, matching the OPEN_COMPARE store guard).
  if (route.compare) {
    // #463 S5 — resolve source and account focus from side A. OPEN_COMPARE
    // canonically anchors on A, and CLOSE_COMPARE preserves that selection, so A
    // supplies a definite rail source both during the comparison and after it
    // closes. Leaving a cross-source comparison's board unchanged reproduces the
    // desync at close.
    const openCompare: Action = sameConversationRef(route.compare.a, route.compare.b)
      ? (route.qualified
        ? { type: 'OPEN_CONVERSATION', conversationRef: route.compare.a }
        : { type: 'OPEN_CONVERSATION', sessionId: route.compare.a.key })
      : (route.qualified
        ? { type: 'OPEN_COMPARE', aRef: route.compare.a, bRef: route.compare.b }
        : { type: 'OPEN_COMPARE', a: route.compare.a.key, b: route.compare.b.key });
    runConversationEntry(deps.dispatch, route.compare.a, openCompare, options);
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
  runConversationEntry(deps.dispatch, conversationRef, route.qualified
    ? { type: 'OPEN_CONVERSATION', conversationRef, jump }
    : { type: 'OPEN_CONVERSATION', sessionId: conversationRef.key, jump }, options);
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

  // 0b) #463 S5 — adopt the entry we booted on. A pasted permalink opened in a
  // NEW TAB lands on a history entry the browser created, which this module never
  // stamps: `writeUrl` early-returns when the hash already matches, so nothing
  // ever writes over it. The first Back therefore read as a fresh navigation and
  // cleared the rail search and filters, while every later Back preserved them —
  // and the CHANGELOG claims Back preserves them unconditionally. Stamping the
  // entry in place makes the boot entry indistinguishable from one we wrote.
  //
  // The two-argument form leaves the URL untouched by definition. Do NOT pass
  // `''` as the third argument instead: an empty url is resolved against the
  // document URL, which DROPS the fragment and would silently discard the very
  // permalink being booted.
  if (!isStampedEntry(window.history.state)) {
    const carried = typeof window.history.state === 'object' && window.history.state !== null
      ? window.history.state as Record<string, unknown>
      : {};
    window.history.replaceState({ ...carried, [HISTORY_STAMP]: true }, '');
  }

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
  // #463 S5 — reflection is suppressed for the duration of the batch, so `prev`
  // is re-synchronized once it completes. Without that, the next genuine store
  // change would be compared against the PRE-batch snapshot: a jump inside the
  // newly opened conversation would see a differing refKey, take the push branch
  // instead of the replace branch, and write the base hash — dropping the turn.
  const onHashChange = () => {
    applyHashToStore(deps);
    prev = snap();
  };
  window.addEventListener('hashchange', onHashChange);

  // #463 S5 (review F5) — a same-document navigation fires popstate before the
  // hashchange that applies it, for a Back/Forward AND for a fresh
  // `location.hash` assignment. Only the entry's stamp separates them.
  const onPopState = (event: PopStateEvent) => {
    historyTraversalPending = isStampedEntry(event.state);
  };
  window.addEventListener('popstate', onPopState);

  // 3) Reflect path: transition-gated store -> URL.
  const onStoreChange = () => {
    if (applyingHash) return;
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

  // #463 S5 (review F4) — see settleAfterEntry. Assigned AFTER `prev` and
  // `onStoreChange` exist, so the boot apply above (which is suppressed as a
  // whole and is therefore never the outermost entry) cannot reach it.
  const settle = () => { onStoreChange(); };
  settleAfterEntry = settle;

  return () => {
    window.removeEventListener('hashchange', onHashChange);
    window.removeEventListener('popstate', onPopState);
    historyTraversalPending = false;
    if (settleAfterEntry === settle) settleAfterEntry = null;
    unsubscribe();
    // #241 — restore the pre-install scroll-restoration mode (test hermeticity;
    // prod never disposes, so 'manual' persists for the app's lifetime).
    window.history.scrollRestoration = prevScrollRestoration;
  };
}
