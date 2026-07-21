import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import { revalToken } from '../lib/revalToken';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { adaptQualifiedOutline, ConversationNormalizationPending } from '../lib/conversationAdapters';
import type { NativeTokens } from '../lib/conversationAdapters';
import {
  conversationRefKey,
  isQualifiedConversationRef,
  normalizeConversationRef,
  type ConversationOutline,
  type ConversationRef,
  type ConversationRefInput,
} from '../types/conversation';

// #177 S5 — full-session outline + stats. Owns its OWN SSE tick subscription
// (Codex F3: useConversation only tail-polls once fully paged), with the same
// coalescing discipline as pollTail: one in-flight fetch, a tick that lands
// mid-fetch replays exactly once after it settles. A fetch error degrades
// gracefully ({outline: null, error}); the reader itself is unaffected. A
// stale-session response (session switched mid-fetch) is dropped, never exposed.
// #227 — `revalidateOnTick` (default true) gates the per-SSE-tick refetch. The
// reader/OutlinePanel keep the default (a live session's outline must track
// growth); ComparisonView passes false so its two finished-run snapshots open
// once and don't re-fetch on every dashboard tick (the comparison never
// live-tails by design).
// #278 Theme B — `growthNonce`/`live` are the shared live-tail signal (from
// useConversationLiveTail via ConversationsView). When `live` (server actively
// live-tailing), the per-tick revalidation is skipped and the outline refetches
// only on a genuine growth push (`growthNonce`); when live-tail is off/passive,
// the change signal stays as the fallback. Defaults keep pre-#278 behavior.
// #300 — that non-live fallback now keys on the change signal `revalToken(env)`
// (the all-inputs `data_version`, falling back to `generated_at`) rather than
// the raw 5s `generated_at` heartbeat, so a finished/static conversation
// fetches its outline once instead of re-GET every tick while open.
export function useConversationOutline(
  rawRef: ConversationRefInput | null,
  opts?: { revalidateOnTick?: boolean; growthNonce?: number; live?: boolean },
) {
  const conversationRef = rawRef ? normalizeConversationRef(rawRef) : null;
  const identityKey = conversationRef ? conversationRefKey(conversationRef) : null;
  const revalidateOnTick = opts?.revalidateOnTick ?? true;
  const growthNonce = opts?.growthNonce ?? 0;
  const live = opts?.live ?? false;
  const [outline, setOutline] = useState<ConversationOutline | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const identityRef = useRef(identityKey);
  const conversationRefRef = useRef<ConversationRef | null>(conversationRef);
  const outlineRef = useRef<ConversationOutline | null>(null);
  const fetchingRef = useRef(false);
  const pendingRef = useRef(false);

  const refetch = useCallback(async () => {
    // Coalesce a tick that lands mid-fetch into ONE trailing replay (the
    // `finally` re-invokes once pendingRef is set). Never concurrent requests.
    if (fetchingRef.current) { pendingRef.current = true; return; }
    const key = identityRef.current;
    const ref = conversationRefRef.current;
    if (!key || !ref) return;
    fetchingRef.current = true;
    try {
      const body = isQualifiedConversationRef(ref)
        ? await Promise.all([
            fetchJson<Parameters<typeof adaptQualifiedOutline>[1]>(conversationEntityUrl(ref, 'outline')),
            fetchJson<{ total_cost_usd?: number; tokens?: NativeTokens }>(
              conversationEntityUrl(ref, 'detail', { limit: 1 })),
            ref.source === 'claude'
              ? fetchJson<{ prompts?: { item_key: string; text: string }[] }>(conversationEntityUrl(ref, 'prompts'))
              : Promise.resolve(null),
          ]).then(([rawOutline, rawDetail, rawPrompts]) => adaptQualifiedOutline(
            ref,
            rawOutline,
            rawDetail,
            rawPrompts ? new Set((rawPrompts.prompts ?? []).map((prompt) => prompt.item_key)) : undefined,
          ))
        : await fetchJson<ConversationOutline>(conversationEntityUrl(ref, 'outline'));
      if (identityRef.current !== key) return;   // session switched mid-fetch — drop
      outlineRef.current = body;
      setOutline(body); setError(null); setLoading(false);
    } catch (e) {
      // Deliberate no-AbortController choice (#184): the single-in-flight guard
      // (`fetchingRef`) already prevents overlapping requests, and the
      // `sessionRef.current !== sid` check below drops any stale response a
      // session switch left in flight — so there is no fetch to abort and no
      // AbortError to special-case. A genuine fetch failure for the CURRENT
      // session degrades to the inline error banner.
      if (identityRef.current !== key) return;
      setError(e instanceof ConversationNormalizationPending
        ? 'Conversation indexing is still finishing.'
        : "Couldn't load the outline."); setLoading(false);
    } finally {
      fetchingRef.current = false;
      if (pendingRef.current) { pendingRef.current = false; void refetch(); }
    }
  }, []);

  useEffect(() => {
    identityRef.current = identityKey;
    conversationRefRef.current = conversationRef;
    outlineRef.current = null;
    // Clear the in-flight/coalesce guards on a session switch: a fetch still in
    // flight for the OLD session must not block the NEW session's fetch (its
    // late response is dropped by the sessionRef guard, and the pending-replay
    // would otherwise re-issue against the new session anyway). Without this the
    // new session would stall behind a never-resolving stale fetch.
    fetchingRef.current = false;
    pendingRef.current = false;
    setOutline(null); setError(null);
    if (!conversationRef) { setLoading(false); return; }
    setLoading(true);
    void refetch();
  }, [identityKey, refetch]);

  const env = useSnapshot();
  // #300 — gate the non-live fallback on the change signal (`data_version`), not
  // the 5s `generated_at` heartbeat, so a finished/static conversation open in
  // the reader fetches its outline once instead of re-GET every tick. Falls back
  // to `generated_at` when data_version is absent/empty. See `lib/revalToken.ts`.
  const token = revalToken(env);
  useEffect(() => {
    // #227 — skip the SSE-tick revalidation entirely when the caller opted out
    // (ComparisonView's static two-run snapshot). The initial-load effect above
    // is unaffected, so a non-revalidating caller still gets its first fetch.
    // #278 — also skip the global tick while the server is actively live-tailing
    // (`live`); growth arrives via growthNonce below. The tick stays a fallback
    // when live-tail is off/passive.
    if (revalidateOnTick && !live && outlineRef.current) void refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, revalidateOnTick, live]);

  // #278 — genuine per-conversation growth push → refetch the whole-session
  // outline. (ComparisonView passes revalidateOnTick:false and no live-tail, so
  // both effects stay inert for it.)
  useEffect(() => {
    if (growthNonce === 0) return;
    if (revalidateOnTick && outlineRef.current) void refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [growthNonce]);

  return { outline, loading, error };
}
