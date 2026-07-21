// Lazy "load full tool payload" hook for the #178 on-demand route (spec §4.4).
// The truncation affordance on a card calls `load()`; the hook fetches the full
// (un-capped, route-ceiling-bounded) result or input from
// /api/conversation/<sid>/payload, caches the result per hook instance
// (per-(toolUseId, which) since each card mounts its own), and surfaces a
// friendly message for the 410 source-gone case. No-ops when there's no open
// session id (sessionId === null).

import { useCallback, useEffect, useRef, useState } from 'react';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { adaptQualifiedPayload } from '../lib/conversationAdapters';
import { conversationRefKey, normalizeConversationRef, type ConversationRefInput, type FullPayload } from '../types/conversation';

type State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'done'; data: FullPayload }
  | { status: 'error'; error: string };

export function useFullPayload(
  rawRef: ConversationRefInput | null,
  toolUseId: string,
  which: 'result' | 'input',
) {
  const conversationRef = rawRef ? normalizeConversationRef(rawRef) : null;
  const identityKey = conversationRef ? conversationRefKey(conversationRef) : null;
  const [state, setState] = useState<State>({ status: 'idle' });
  // Synchronous in-flight guard (mirrors useConversation's loadingMoreRef /
  // pollingRef). `state.status` is async React state, so two SYNCHRONOUS load()
  // calls — a user double-clicking the load-full affordance — would both still
  // read status:'idle' and fire two fetches. The ref flips true BEFORE the
  // setState({status:'loading'}) so the second synchronous call short-circuits.
  // `doneRef` mirrors the once-done cache so a repeat load() after success is a
  // no-op even before its setState commits.
  const inFlightRef = useRef(false);
  const doneRef = useRef(false);
  const requestKey = `${identityKey ?? ''}\u0000${toolUseId}\u0000${which}`;
  const requestKeyRef = useRef(requestKey);
  useEffect(() => {
    requestKeyRef.current = requestKey;
    inFlightRef.current = false;
    doneRef.current = false;
    setState({ status: 'idle' });
  }, [requestKey]);
  const load = useCallback(async () => {
    // Already loaded / in flight (sync ref), or nothing to address → nothing to do.
    if (inFlightRef.current || doneRef.current || !conversationRef) return;
    const startedFor = requestKey;
    inFlightRef.current = true;
    setState({ status: 'loading' });
    try {
      const url = conversationEntityUrl(conversationRef, 'payload', conversationRef.source === 'codex'
        ? { block_key: toolUseId, which: which === 'input' ? 'call' : 'output' }
        : { tool_use_id: toolUseId, which });
      const r = await fetch(url);
      if (requestKeyRef.current !== startedFor) return;
      if (!r.ok) {
        // 410 = source JSONL rotated/deleted (the documented capped-cache
        // consequence); everything else (403 gate, 5xx, …) is generic.
        setState({
          status: 'error',
          error: r.status === 410 ? 'source no longer available' : 'unavailable',
        });
        return;
      }
      const raw = await r.json() as FullPayload | Parameters<typeof adaptQualifiedPayload>[2];
      const data = conversationRef.source === 'codex'
        ? adaptQualifiedPayload(toolUseId, which, raw as Parameters<typeof adaptQualifiedPayload>[2])
        : raw as FullPayload;
      if (requestKeyRef.current !== startedFor) return;
      doneRef.current = true;
      setState({ status: 'done', data });
    } catch {
      if (requestKeyRef.current === startedFor) {
        setState({ status: 'error', error: 'network error' });
      }
    } finally {
      if (requestKeyRef.current === startedFor) inFlightRef.current = false;
    }
  }, [conversationRef, requestKey, toolUseId, which]);
  return { ...state, load };
}
