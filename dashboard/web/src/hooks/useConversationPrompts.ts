import { useEffect, useRef, useState } from 'react';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { adaptQualifiedPrompts } from '../lib/conversationAdapters';
import { conversationRefKey, isQualifiedConversationRef, normalizeConversationRef, type ConversationRefInput } from '../types/conversation';

// #217 S7 F10 — lazy full-prompt-text fetch for the session-comparison view.
// The metrics strip + the alignment spine come from each session's /outline, so
// the comparison renders WITHOUT this route; it is only fetched once a user
// expands a row (the `active` flag flips true), and only once per session.
//
// Mirrors useConversationFind's abort/cache discipline: ONE AbortController
// (aborted on unmount or a session switch so a stale response can never commit),
// and a `loadedFor` ref so a re-render with the same active session never
// refetches. Returns a `uuid → full text` map the ExpandedPrompt panels read.
interface PromptEntry { uuid: string; text: string; }
export interface PromptsResult {
  byUuid: Record<string, string> | null;
  loading: boolean;
  error: string | null;
}

export function useConversationPrompts(rawRef: ConversationRefInput | null, active: boolean): PromptsResult {
  const conversationRef = rawRef ? normalizeConversationRef(rawRef) : null;
  const identityKey = conversationRef ? conversationRefKey(conversationRef) : null;
  const [byUuid, setByUuid] = useState<Record<string, string> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadedFor = useRef<string | null>(null);

  // A session switch invalidates the cache: drop the stale map + reset the
  // loaded marker so the new session's first `active` triggers a fresh fetch
  // (and the old session's map is never merged in).
  useEffect(() => {
    loadedFor.current = null;
    setByUuid(null);
    setError(null);
  }, [identityKey]);

  useEffect(() => {
    if (!active || !conversationRef || !identityKey || loadedFor.current === identityKey) return;
    const ctl = new AbortController();
    setLoading(true);
    setError(null);
    fetch(conversationEntityUrl(conversationRef, 'prompts'), { signal: ctl.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((raw: { prompts: PromptEntry[] } | Parameters<typeof adaptQualifiedPrompts>[0]) => {
        const data = isQualifiedConversationRef(conversationRef)
          ? adaptQualifiedPrompts(raw as Parameters<typeof adaptQualifiedPrompts>[0])
          : raw as { prompts: PromptEntry[] };
        const map: Record<string, string> = {};
        for (const p of data.prompts) map[p.uuid] = p.text;
        setByUuid(map);
        loadedFor.current = identityKey;
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === 'AbortError') return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
      })
      .finally(() => setLoading(false));
    return () => ctl.abort();
  }, [active, identityKey]);

  return { byUuid, loading, error };
}
