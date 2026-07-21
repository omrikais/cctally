import { useEffect, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import type { ConversationFacets } from '../types/conversation';
import type { ConversationSource } from '../types/conversation';
import { qualifiedFacetsUrl, type QualifiedFacetsEnvelope } from '../lib/conversationTransport';

// Backoff before the single facets-fetch retry. The likeliest transient cause is
// a startup race (server not yet serving) or a momentary hiccup during a heavy
// sync; a short pause lets it recover before we give up.
const FACETS_RETRY_MS = 700;

// Project facet for the browse-filter popover's multi-select (filters spec §4).
// Fetched ONCE from GET /api/conversations/facets — deriving the project options
// from the loaded rail rows would be incomplete under pagination. Fails closed to
// an empty list (the popover then shows no project options rather than crashing);
// the AbortController cancels the in-flight fetch on unmount so a late resolve
// can't set state on a torn-down component.
//
// #278 Theme C P3 follow-up — a transient failure previously failed closed to
// empty immediately, leaving the popover option-less until it was reopened. It
// now retries ONCE after a short backoff before settling empty (bounded so a
// persistently-down endpoint can't spin). Helps the Project axis identically.
export function useConversationFacets(source: ConversationSource = 'claude'): ConversationFacets {
  const [facets, setFacets] = useState<ConversationFacets>({ projects: [], models: [] });
  useEffect(() => {
    const ctl = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const load = (attempt: number): void => {
      fetchJson<ConversationFacets | QualifiedFacetsEnvelope>(
        source === 'codex' ? qualifiedFacetsUrl('codex') : '/api/conversations/facets', ctl.signal)
        // #278 Theme C — normalize on the SUCCESS path, not just initial/error
        // state: an older or mocked response carrying only `{ projects }` would
        // otherwise set `models: undefined` and crash the popover's `.map`.
        .then((raw) => {
          if (source === 'codex') {
            const r = raw as QualifiedFacetsEnvelope;
            setFacets({
              projects: r.facets.projects.map((p) => ({
                project_label: p.project_label ?? 'Unnamed project', count: p.count, filter_value: p.project_key,
              })),
              models: r.facets.models.map((m) => ({ family: m.model, count: m.count, filter_value: m.model })),
            });
          } else {
            const r = raw as ConversationFacets;
            setFacets({ projects: r.projects ?? [], models: r.models ?? [] });
          }
        })
        .catch((e) => {
          if (isAbortError(e)) return;
          if (attempt === 0) { retryTimer = setTimeout(() => load(1), FACETS_RETRY_MS); return; }
          setFacets({ projects: [], models: [] });
        });
    };
    load(0);
    return () => { ctl.abort(); if (retryTimer) clearTimeout(retryTimer); };
  }, [source]);
  return facets;
}
