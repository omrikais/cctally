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
  // Optional so the many existing card tests that build a `{ sessionId }`
  // provider value keep compiling; consumers default a missing mode to 'all'.
  focusMode?: FocusMode;
  // Display-tz formatting context. Optional for the same back-compat reason;
  // consumers fall back to DEFAULT_FMT_CTX (Etc/UTC) when absent.
  fmtCtx?: FmtCtx;
}

export const TranscriptContext = createContext<TranscriptCtxValue>({
  sessionId: null,
  focusMode: 'all',
  fmtCtx: DEFAULT_FMT_CTX,
});

export const useSessionId = () => useContext(TranscriptContext).sessionId;
export const useFocusMode = (): FocusMode => useContext(TranscriptContext).focusMode ?? 'all';
export const useFmtCtx = (): FmtCtx => useContext(TranscriptContext).fmtCtx ?? DEFAULT_FMT_CTX;
