import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import { revalToken } from '../lib/revalToken';
import { conversationDegradedNotice, conversationDegradedReason, conversationEntityUrl, type ConversationDegradedNotice } from '../lib/conversationTransport';
import { adaptQualifiedOutline, ConversationNormalizationPending } from '../lib/conversationAdapters';
import { sha256Hex } from '../lib/sha256';
import {
  conversationRefKey,
  isQualifiedConversationRef,
  normalizeConversationRef,
  type ConversationOutline,
  type ConversationRef,
  type ConversationRefInput,
} from '../types/conversation';

type ProgressiveTransfer = {
  token: string;
  total?: number;
  sha256?: string;
  chunk_size: number;
};

type ProgressiveOutline<T> = {
  progressive: 1;
  summary?: T;
  transfer: ProgressiveTransfer;
};

type TransferChunk = {
  offset: number;
  next_offset: number;
  total: number;
  sha256: string;
  done: boolean;
  chunk: string;
};

class ConversationOutlineDegraded extends Error {
  constructor(readonly reason: string) { super('Conversation outline store degraded'); }
}

function assertOutlineAvailable(body: unknown): void {
  const reason = conversationDegradedReason(body);
  if (reason) throw new ConversationOutlineDegraded(reason);
}

function isProgressiveOutline<T>(body: T | ProgressiveOutline<T>): body is ProgressiveOutline<T> {
  return typeof body === 'object' && body !== null
    && (body as { progressive?: unknown }).progressive === 1;
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function throwIfAborted(signal: AbortSignal) {
  if (signal.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
}

async function cancelOutlineTransfer(token: string): Promise<void> {
  try {
    await fetch(
      `/api/conversation/outline-transfer/${encodeURIComponent(token)}`,
      { method: 'DELETE', keepalive: true },
    );
  } catch {
    // Best effort only: the server's TTL remains the fallback cleanup path.
  }
}

async function hydrateOutlineTransfer<T>(
  transfer: ProgressiveTransfer,
  signal: AbortSignal,
): Promise<T> {
  const parts: Uint8Array[] = [];
  let received = 0;
  let total = transfer.total;
  let sha256 = transfer.sha256;
  try {
    while (total === undefined || received < total) {
      throwIfAborted(signal);
      const chunk = await fetchJson<TransferChunk>(
        `/api/conversation/outline-transfer/${encodeURIComponent(transfer.token)}?offset=${received}`,
        signal,
      );
      throwIfAborted(signal);
      if (total === undefined) total = chunk.total;
      if (sha256 === undefined) sha256 = chunk.sha256;
      if (chunk.offset !== received || chunk.total !== total
          || chunk.sha256 !== sha256 || chunk.next_offset <= received) {
        throw new Error('Invalid outline transfer');
      }
      const bytes = decodeBase64(chunk.chunk);
      if (received + bytes.length !== chunk.next_offset) throw new Error('Invalid outline chunk');
      parts.push(bytes);
      received = chunk.next_offset;
      if (chunk.done !== (received === total)) throw new Error('Invalid outline completion');
    }
    const all = new Uint8Array(received);
    let offset = 0;
    for (const part of parts) { all.set(part, offset); offset += part.length; }
    if (sha256 === undefined || !/^[0-9a-f]{64}$/.test(sha256)
        || await sha256Hex(all, signal) !== sha256) {
      throw new Error('Invalid outline digest');
    }
    return JSON.parse(new TextDecoder().decode(all)) as T;
  } catch (error) {
    void cancelOutlineTransfer(transfer.token);
    throw error;
  }
}

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
  const [degraded, setDegraded] = useState<ConversationDegradedNotice | null>(null);
  const [notFound, setNotFound] = useState(false);
  const identityRef = useRef(identityKey);
  const conversationRefRef = useRef<ConversationRef | null>(conversationRef);
  const outlineRef = useRef<ConversationOutline | null>(null);
  const fetchingRef = useRef(false);
  const pendingRef = useRef(false);
  const requestGenerationRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);

  const refetch = useCallback(async () => {
    // Coalesce a tick that lands mid-fetch into ONE trailing replay (the
    // `finally` re-invokes once pendingRef is set). Never concurrent requests.
    if (fetchingRef.current) { pendingRef.current = true; return; }
    const key = identityRef.current;
    const ref = conversationRefRef.current;
    if (!key || !ref) return;
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    const controller = new AbortController();
    controllerRef.current = controller;
    const ownsRequest = () => requestGenerationRef.current === generation
      && identityRef.current === key
      && !controller.signal.aborted;
    fetchingRef.current = true;
    try {
      const outlineUrl = conversationEntityUrl(ref, 'outline', { progressive: 1 });
      if (isQualifiedConversationRef(ref)) {
        type RawQualified = Parameters<typeof adaptQualifiedOutline>[1];
        const raw = await fetchJson<RawQualified | ProgressiveOutline<RawQualified>>(
          outlineUrl,
          controller.signal,
        );
        if (!ownsRequest()) return;
        assertOutlineAvailable(raw);
        const progressive = isProgressiveOutline(raw);
        const summaryRaw = progressive ? raw.summary : raw;
        let body: ConversationOutline;
        if (summaryRaw !== undefined) {
          body = adaptQualifiedOutline(ref, summaryRaw);
          if (!ownsRequest()) return;
          outlineRef.current = body;
          setOutline(body); setError(null); setDegraded(null); setNotFound(false); setLoading(false);
        }

        const fullRaw = progressive
          ? await hydrateOutlineTransfer<RawQualified>(raw.transfer, controller.signal)
          : summaryRaw as RawQualified;
        assertOutlineAvailable(fullRaw);
        const rawPrompts = ref.source === 'claude'
          ? await fetchJson<{ prompts?: { item_key: string; text: string }[] }>(
              conversationEntityUrl(ref, 'prompts'),
              controller.signal,
            )
          : null;
        assertOutlineAvailable(rawPrompts);
        body = adaptQualifiedOutline(
          ref,
          fullRaw,
          {},
          rawPrompts ? new Set((rawPrompts.prompts ?? []).map((prompt) => prompt.item_key)) : undefined,
        );
        if (!ownsRequest()) return;
        outlineRef.current = body;
        setOutline(body); setError(null); setDegraded(null); setNotFound(false); setLoading(false);
      } else {
        const raw = await fetchJson<ConversationOutline | ProgressiveOutline<ConversationOutline> | { status: 'not_found' }>(
          outlineUrl,
          controller.signal,
        );
        if (!ownsRequest()) return;
        assertOutlineAvailable(raw);
        if ('status' in raw && raw.status === 'not_found') {
          outlineRef.current = null;
          setOutline(null); setError(null); setDegraded(null); setNotFound(true); setLoading(false);
          return;
        }
        const progressive = isProgressiveOutline(raw);
        let body: ConversationOutline;
        if (progressive) {
          if (raw.summary !== undefined) {
            body = raw.summary;
            if (!ownsRequest()) return;
            outlineRef.current = body;
            setOutline(body); setError(null); setDegraded(null); setNotFound(false); setLoading(false);
          }
          body = await hydrateOutlineTransfer<ConversationOutline>(raw.transfer, controller.signal);
          assertOutlineAvailable(body);
        } else {
          body = raw as ConversationOutline;
        }
        if (!ownsRequest()) return;
        outlineRef.current = body;
        setOutline(body); setError(null); setDegraded(null); setNotFound(false); setLoading(false);
      }
    } catch (e) {
      // A session switch aborts the obsolete progressive transfer. Only a
      // genuine fetch failure for the CURRENT session reaches the inline error
      // banner; aborts and stale generations are silent.
      if (isAbortError(e)) return;
      if (!ownsRequest()) return;
      if (e instanceof ConversationOutlineDegraded) {
        outlineRef.current = null;
        setOutline(null); setError(null); setDegraded(conversationDegradedNotice(e.reason)); setNotFound(false); setLoading(false);
        return;
      }
      if (!isQualifiedConversationRef(ref) && e instanceof HttpError && e.status === 404) {
        // Older servers still use a 404 for an absent progressive preflight.
        // A non-404 failure remains visible as an actual outline error.
        outlineRef.current = null;
        setOutline(null); setError(null); setDegraded(null); setNotFound(true); setLoading(false);
        return;
      }
      setDegraded(null);
      setError(e instanceof ConversationNormalizationPending
        ? 'Conversation indexing is still finishing.'
        : "Couldn't load the outline."); setLoading(false);
    } finally {
      if (requestGenerationRef.current === generation) {
        controllerRef.current = null;
        fetchingRef.current = false;
        if (pendingRef.current) { pendingRef.current = false; void refetch(); }
      }
    }
  }, []);

  useEffect(() => {
    controllerRef.current?.abort();
    requestGenerationRef.current += 1;
    identityRef.current = identityKey;
    conversationRefRef.current = conversationRef;
    outlineRef.current = null;
    // Clear the in-flight/coalesce guards on a session switch. The generation
    // invalidation above prevents the old request's `finally` from clearing the
    // new request's flags, while aborting stops obsolete progressive chunks.
    fetchingRef.current = false;
    pendingRef.current = false;
    setOutline(null); setError(null); setDegraded(null); setNotFound(false);
    if (!conversationRef) { setLoading(false); return; }
    setLoading(true);
    void refetch();
    return () => {
      controllerRef.current?.abort();
      requestGenerationRef.current += 1;
      fetchingRef.current = false;
      pendingRef.current = false;
    };
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

  return { outline, loading, error, degraded, notFound };
}
