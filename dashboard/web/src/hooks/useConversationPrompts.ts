import { useEffect, useRef, useState } from 'react';

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

export function useConversationPrompts(sessionId: string | null, active: boolean): PromptsResult {
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
  }, [sessionId]);

  useEffect(() => {
    if (!active || !sessionId || loadedFor.current === sessionId) return;
    const ctl = new AbortController();
    setLoading(true);
    setError(null);
    fetch(`/api/conversation/${encodeURIComponent(sessionId)}/prompts`, { signal: ctl.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { prompts: PromptEntry[] }) => {
        const map: Record<string, string> = {};
        for (const p of data.prompts) map[p.uuid] = p.text;
        setByUuid(map);
        loadedFor.current = sessionId;
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === 'AbortError') return;
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
      })
      .finally(() => setLoading(false));
    return () => ctl.abort();
  }, [active, sessionId]);

  return { byUuid, loading, error };
}
