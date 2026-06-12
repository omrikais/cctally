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

export interface TranscriptCtxValue {
  sessionId: string | null;
  // Optional so the many existing card tests that build a `{ sessionId }`
  // provider value keep compiling; consumers default a missing mode to 'all'.
  focusMode?: FocusMode;
}

export const TranscriptContext = createContext<TranscriptCtxValue>({
  sessionId: null,
  focusMode: 'all',
});

export const useSessionId = () => useContext(TranscriptContext).sessionId;
export const useFocusMode = (): FocusMode => useContext(TranscriptContext).focusMode ?? 'all';
