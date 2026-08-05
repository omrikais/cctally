// Carries the currently-open transcript session id down to the tool cards (#177
// S3), so the load-full affordance (useFullPayload) can address the #178 route
// without threading `sessionId` through every intermediate component. The
// ConversationReader provides it; cards read it via `useSessionId`. Default is
// null (no open transcript) — useFullPayload no-ops when sessionId is null.
//
// #177 S5 — the context also carries the active focus mode so the block walker
// (MessageBlocks) can suppress tool/orphan-result chips under chat mode without
// threading the mode through every render site. `useSessionId` keeps its
// string-or-null shape for the existing card consumers.

import { createContext, useContext } from 'react';
import type { FocusMode } from './applyFocusMode';
import type { FmtCtx } from '../lib/fmt';
import { legacyClaudeConversationRef, type ConversationRef, type ConversationSessionIndex } from '../types/conversation';

// #184 — the display-tz FmtCtx rides on the context too. Memo economics: the
// reader memoizes its MessageItems precisely so an SSE tick doesn't re-render
// every mounted item. If each item called useDisplayTz() it would re-subscribe
// to the snapshot store and re-render on EVERY tick, defeating the memo. The
// reader computes `fmtCtx` ONCE and provides it here; items read it from context
// (no per-item store subscription), so a tick that doesn't change the provider
// value re-renders nothing. The default (Etc/UTC) lets isolated component tests
// render without a provider.
const DEFAULT_FMT_CTX: FmtCtx = { tz: 'Etc/UTC', offsetLabel: 'UTC' };

export interface TranscriptCtxValue {
  sessionId: string | null;
  conversationRef?: ConversationRef | null;
  // Optional so the many existing card tests that build a `{ sessionId }`
  // provider value keep compiling; consumers default a missing mode to 'all'.
  focusMode?: FocusMode;
  // Display-tz formatting context. Optional for the same back-compat reason;
  // consumers fall back to DEFAULT_FMT_CTX (Etc/UTC) when absent.
  fmtCtx?: FmtCtx;
  // cache-failure-markers spec §3 — the conversation-viewer cache-rebuild
  // marker opt-out, provided ONCE by the reader from selectMarkersEnabled (so
  // the memoized MessageItems don't each subscribe to the store and re-render
  // every tick — same memo economics as fmtCtx). Optional + default true
  // (opt-out): an absent provider value renders the chip.
  markersEnabled?: boolean;
  // #217 S6 F3 — the session's heaviest LOADED per-turn cost, provided ONCE by
  // the reader (same memo economics as fmtCtx/markersEnabled) so the per-turn
  // cost bar can size itself without each memoized MessageItem subscribing to
  // the store. Optional + default 0 (→ no bar) for the many provider-less tests.
  maxTurnCost?: number;
  // #281 S4 — the "Anonymize" mode, provided ONCE by the reader (single source
  // for the Export menu + every per-card CopyButton). Optional + default FALSE
  // for provider-less card tests (which assert today's raw-copy behavior).
  anonMode?: boolean;
  // #463 S2 §2.6 — reasoning heading keys the reader must not render, because an
  // earlier block of the SAME TURN already rendered that text (Codex writes
  // cumulative reasoning summaries). Computed once by the reader over the loaded
  // window, for the same memo economics as fmtCtx: the decision needs the whole
  // turn, which one block cannot see, and per-item recomputation would defeat
  // the MessageItem memo. Optional + default empty, so a provider-less card test
  // renders every heading exactly as before.
  suppressedHeadingKeys?: ReadonlySet<string>;
  // #463 S3 §3.2 — the conversation-scoped shell-session index. Provided ONCE
  // by the reader for the same memo economics as fmtCtx: the cards need a
  // whole-conversation fact (which call opened a session, whether the index is
  // complete) that no single block can see. Optional + absent by default, so a
  // provider-less card test and a pre-S3 server both render no session note at
  // all rather than a wrong one.
  sessionIndex?: ConversationSessionIndex;
}

export const TranscriptContext = createContext<TranscriptCtxValue>({
  sessionId: null,
  focusMode: 'all',
  fmtCtx: DEFAULT_FMT_CTX,
  markersEnabled: true,
  maxTurnCost: 0,
});

export const useSessionId = () => useContext(TranscriptContext).sessionId;
export const useConversationRef = (): ConversationRef | null => {
  const value = useContext(TranscriptContext);
  return value.conversationRef ?? (value.sessionId ? legacyClaudeConversationRef(value.sessionId) : null);
};
export const useFocusMode = (): FocusMode => useContext(TranscriptContext).focusMode ?? 'all';
export const useFmtCtx = (): FmtCtx => useContext(TranscriptContext).fmtCtx ?? DEFAULT_FMT_CTX;
// Default true (opt-out): a missing provider value reads as markers-on.
export const useMarkersEnabled = (): boolean =>
  useContext(TranscriptContext).markersEnabled ?? true;
// #217 S6 F3 — the session max-turn-cost denominator for the per-turn cost bar.
// Default 0 (no provider value) → costIntensity returns 0 → no bar.
export const useMaxTurnCost = (): number =>
  useContext(TranscriptContext).maxTurnCost ?? 0;
// #281 S4 — the per-card copy anon mode. Default FALSE (no provider value) so
// isolated card tests copy raw exactly as before.
export const useAnonMode = (): boolean =>
  useContext(TranscriptContext).anonMode ?? false;
// #463 S3 §3.2 — undefined by default: with no index the cards say nothing
// about openers, which is correct rather than merely safe.
export const useSessionIndex = (): ConversationSessionIndex | undefined =>
  useContext(TranscriptContext).sessionIndex;
// #463 S2 §2.6 — empty by default: no provider value means suppress nothing.
const NO_SUPPRESSED_HEADINGS: ReadonlySet<string> = new Set<string>();
export const useSuppressedHeadingKeys = (): ReadonlySet<string> =>
  useContext(TranscriptContext).suppressedHeadingKeys ?? NO_SUPPRESSED_HEADINGS;
