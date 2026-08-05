import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError, HttpError } from '../lib/fetchJson';
import { useDebouncedValue } from './useDebouncedValue';
import { conversationEntityUrl } from '../lib/conversationTransport';
import { adaptQualifiedFind } from '../lib/conversationAdapters';
import {
  conversationRefKey,
  isQualifiedConversationRef,
  normalizeConversationRef,
  type ConversationFindResult,
  type ConversationRefInput,
  type FindAnchor,
  type FindOccurrence,
  type LegacyConversationFindResult,
  type OccurrenceFindResult,
} from '../types/conversation';

export type FindTarget = FindAnchor | FindOccurrence;

export interface UseConversationFind {
  anchors: FindAnchor[];
  occurrences: FindOccurrence[];
  selected: FindTarget | null;
  selectedIndex: number;
  total: number;
  truncated: boolean;
  semantics: 'section' | 'occurrence';
  status: 'ready' | 'indexing';
  selectionStale: boolean;
  mode: 'fts' | 'like' | 'regex' | 'literal' | null;
  loading: boolean;
  error: string | null;
  step: (delta: number) => FindTarget | null | Promise<FindTarget | null>;
}

export interface UseConversationFindOptions {
  regex?: boolean;
  case?: boolean;
  tailRevision?: number;
}

const DEBOUNCE_MS = 200;

function isExact(result: ConversationFindResult | null): result is OccurrenceFindResult {
  return result != null && 'semantics' in result && result.semantics === 'occurrence';
}

export function useConversationFind(
  rawRef: ConversationRefInput,
  needle: string,
  opts: UseConversationFindOptions = {},
): UseConversationFind {
  const conversationRef = normalizeConversationRef(rawRef);
  const identityKey = conversationRefKey(conversationRef);
  const { regex = false, case: caseSensitive = false, tailRevision = 0 } = opts;
  const q = needle.trim();
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  const debouncedRev = useDebouncedValue(tailRevision, DEBOUNCE_MS, 0);
  const searchKey = JSON.stringify([identityKey, debouncedQ, regex, caseSensitive]);
  const [result, setResult] = useState<ConversationFindResult | null>(null);
  const [selectedOffset, setSelectedOffset] = useState(0);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ctlRef = useRef<AbortController | null>(null);
  const edgeRef = useRef<Promise<FindTarget | null> | null>(null);
  const selectedIdRef = useRef<string | null>(null);
  const committedSearchKeyRef = useRef<string | null>(null);

  const decode = useCallback((raw: unknown): ConversationFindResult => (
    isQualifiedConversationRef(conversationRef)
      ? adaptQualifiedFind(raw as Parameters<typeof adaptQualifiedFind>[0])
      : raw as LegacyConversationFindResult
  ), [identityKey]);

  const commit = useCallback((next: ConversationFindResult): FindTarget | null => {
    setResult(next);
    if (isExact(next)) {
      const offset = 0;
      setSelectedOffset(offset);
      const selected = next.page.occurrences[offset] ?? null;
      selectedIdRef.current = selected?.occurrence_id ?? null;
      return selected;
    }
    const previous = selectedIdRef.current;
    const offset = previous
      ? Math.max(0, next.anchors.findIndex((anchor) => anchor.uuid === previous))
      : 0;
    setSelectedOffset(offset);
    const selected = next.anchors[offset] ?? null;
    selectedIdRef.current = selected?.uuid ?? null;
    return selected;
  }, []);

  const request = useCallback(async ({
    cursor,
    direction,
    around,
    signal,
  }: {
    cursor?: string;
    direction?: 'next' | 'previous';
    around?: string;
    signal?: AbortSignal;
  } = {}): Promise<ConversationFindResult> => {
    const url = conversationEntityUrl(conversationRef, 'find', {
      q: debouncedQ,
      regex: regex || undefined,
      case: caseSensitive || undefined,
      ...(isQualifiedConversationRef(conversationRef) && conversationRef.source === 'codex'
        ? {
            limit: 100,
            cursor,
            direction: direction && (cursor || direction === 'previous') ? direction : undefined,
            around,
          }
        : {}),
    });
    return decode(await fetchJson<unknown>(url, signal));
  }, [identityKey, debouncedQ, regex, caseSensitive, decode]);

  useEffect(() => {
    if (!q) {
      ctlRef.current?.abort();
      setResult(null);
      setSelectedOffset(0);
      selectedIdRef.current = null;
      committedSearchKeyRef.current = null;
      setError(null);
      setFetching(false);
    }
    return () => { ctlRef.current?.abort(); };
  }, [q]);

  useEffect(() => {
    if (!debouncedQ) { setFetching(false); return; }
    const ctl = new AbortController();
    ctlRef.current?.abort();
    ctlRef.current = ctl;
    edgeRef.current = null;
    setFetching(true);
    const around = isExact(result) && committedSearchKeyRef.current === searchKey
      ? selectedIdRef.current ?? undefined
      : undefined;
    request({ around, signal: ctl.signal })
      .then((next) => {
        if (ctl.signal.aborted) return;
        commit(next);
        committedSearchKeyRef.current = searchKey;
        setError(null);
        setFetching(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setResult(null);
        selectedIdRef.current = null;
        committedSearchKeyRef.current = null;
        setError(e instanceof HttpError && e.status === 400 ? 'invalid regex' : 'find failed');
        setFetching(false);
      });
    return () => ctl.abort();
    // `result` is deliberately excluded: a committed page must not refetch itself.
    // `debouncedRev` is the live-tail reconciliation trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identityKey, debouncedQ, regex, caseSensitive, debouncedRev, searchKey, request, commit]);

  const step = useCallback((delta: number): FindTarget | null | Promise<FindTarget | null> => {
    if (!result || delta === 0) return null;
    if (!isExact(result)) {
      if (result.anchors.length === 0) return null;
      const offset = (
        (selectedOffset + delta) % result.anchors.length + result.anchors.length
      ) % result.anchors.length;
      setSelectedOffset(offset);
      const selected = result.anchors[offset];
      selectedIdRef.current = selected.uuid;
      return selected;
    }
    if (result.status !== 'ready' || result.page.occurrences.length === 0) return null;
    const within = selectedOffset + (delta > 0 ? 1 : -1);
    if (within >= 0 && within < result.page.occurrences.length) {
      setSelectedOffset(within);
      const selected = result.page.occurrences[within];
      selectedIdRef.current = selected.occurrence_id;
      return selected;
    }
    if (edgeRef.current) return edgeRef.current;
    const forward = delta > 0;
    const cursor = forward ? result.page.next_cursor : result.page.previous_cursor;
    const direction = forward ? 'next' as const : 'previous' as const;
    const pending = (async () => {
      try {
        let next: ConversationFindResult;
        try {
          next = await request({ cursor: cursor ?? undefined, direction });
        } catch (e) {
          if (!(e instanceof HttpError) || e.status !== 409) throw e;
          next = await request({ around: selectedIdRef.current ?? undefined });
          if (isExact(next) && !next.selection_stale) {
            const reconciledAt = next.page.occurrences.findIndex(
              (occurrence) => occurrence.occurrence_id === selectedIdRef.current,
            );
            const steppedAt = reconciledAt + (forward ? 1 : -1);
            if (reconciledAt >= 0 && steppedAt >= 0 && steppedAt < next.page.occurrences.length) {
              setResult(next);
              setSelectedOffset(steppedAt);
              const selected = next.page.occurrences[steppedAt];
              selectedIdRef.current = selected.occurrence_id;
              setError(null);
              return selected;
            }
            const reconciledCursor = forward
              ? next.page.next_cursor
              : next.page.previous_cursor;
            if (reconciledAt >= 0 && reconciledCursor) {
              next = await request({ cursor: reconciledCursor, direction });
            }
          }
        }
        if (!isExact(next)) return commit(next);
        setResult(next);
        const offset = forward ? 0 : Math.max(0, next.page.occurrences.length - 1);
        setSelectedOffset(offset);
        const selected = next.page.occurrences[offset] ?? null;
        selectedIdRef.current = selected?.occurrence_id ?? null;
        setError(null);
        return selected;
      } catch (e) {
        if (!isAbortError(e)) setError('find failed');
        return null;
      } finally {
        edgeRef.current = null;
      }
    })();
    edgeRef.current = pending;
    return pending;
  }, [result, selectedOffset, request, commit]);

  const exact = isExact(result);
  const anchors = exact || !result ? [] : result.anchors;
  const occurrences = exact ? result.page.occurrences : [];
  const targets: FindTarget[] = exact ? occurrences : anchors;
  const selected = targets[selectedOffset] ?? null;
  const selectedIndex = exact
    ? result.page.start_index + (selected ? selectedOffset : 0)
    : (selected ? selectedOffset : 0);
  const total = exact ? result.total ?? 0 : result?.total ?? 0;
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return {
    anchors,
    occurrences,
    selected,
    selectedIndex,
    total,
    truncated: !exact && result ? result.anchors_truncated : false,
    semantics: exact ? 'occurrence' : 'section',
    status: exact ? result.status : 'ready',
    selectionStale: exact ? result.selection_stale : false,
    mode: result?.mode ?? null,
    loading,
    error,
    step,
  };
}
