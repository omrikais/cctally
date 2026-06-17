import { useEffect, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import type { ConversationFacets } from '../types/conversation';

// Project facet for the browse-filter popover's multi-select (filters spec §4).
// Fetched ONCE from GET /api/conversations/facets — deriving the project options
// from the loaded rail rows would be incomplete under pagination. Fails closed to
// an empty list (the popover then shows no project options rather than crashing);
// the AbortController cancels the in-flight fetch on unmount so a late resolve
// can't set state on a torn-down component.
export function useConversationFacets(): ConversationFacets {
  const [facets, setFacets] = useState<ConversationFacets>({ projects: [] });
  useEffect(() => {
    const ctl = new AbortController();
    fetchJson<ConversationFacets>('/api/conversations/facets', ctl.signal)
      .then(setFacets)
      .catch((e) => { if (!isAbortError(e)) setFacets({ projects: [] }); });
    return () => ctl.abort();
  }, []);
  return facets;
}
