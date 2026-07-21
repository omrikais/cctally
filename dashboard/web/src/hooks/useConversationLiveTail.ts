import { useEffect, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from './useSnapshot';
import { getState, selectLiveTailEnabled, subscribeStore } from '../store/store';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { conversationRefKey, normalizeConversationRef, type ConversationRefInput } from '../types/conversation';

// #278 Theme B — the single per-conversation live-tail signal, lifted out of
// useConversation so BOTH the reader (detail) and the outline can gate on it.
// `live` reflects real server liveness (set on the server's `ready` event, not
// mere socket-open), so passive/degraded streams leave `live` false and callers
// fall back to the memo-backed global tick (Codex F4). `growthNonce` bumps on
// `ready` (first-connect/reconnect catch-up) and every `tail` (genuine growth).
export function useConversationLiveTail(rawRef: ConversationRefInput | null) {
  const conversationRef = rawRef ? normalizeConversationRef(rawRef) : null;
  const identityKey = conversationRef ? conversationRefKey(conversationRef) : null;
  const [growthNonce, setGrowthNonce] = useState(0);
  const [live, setLive] = useState(false);
  const env = useSnapshot();
  const transcriptsEnabled = env?.transcriptsEnabled ?? false;
  const liveTailEnabled = useSyncExternalStore(
    subscribeStore, () => selectLiveTailEnabled(getState()));
  useEffect(() => {
    setLive(false);
    if (!conversationRef || !transcriptsEnabled || !liveTailEnabled) return;
    if (typeof EventSource === 'undefined') return;
    const es = new EventSource(conversationEntityUrl(conversationRef, 'events'));
    es.addEventListener('ready', () => { setLive(true); setGrowthNonce((n) => n + 1); });
    es.addEventListener('tail', () => { setGrowthNonce((n) => n + 1); });
    es.addEventListener('error', () => { setLive(false); });
    return () => { es.close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identityKey, transcriptsEnabled, liveTailEnabled]);
  return { growthNonce, live };
}
