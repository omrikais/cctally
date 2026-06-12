// Carries the currently-open transcript session id down to the tool cards (#177
// S3), so the load-full affordance (useFullPayload) can address the #178 route
// without threading `sessionId` through every intermediate component. The
// ConversationReader provides it; cards read it via `useSessionId`. Default is
// null (no open transcript) — useFullPayload no-ops when sessionId is null.

import { createContext, useContext } from 'react';

export const TranscriptContext = createContext<{ sessionId: string | null }>({ sessionId: null });

export const useSessionId = () => useContext(TranscriptContext).sessionId;
