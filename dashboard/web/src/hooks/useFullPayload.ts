// Lazy "load full tool payload" hook for the #178 on-demand route (spec §4.4).
// The truncation affordance on a card calls `load()`; the hook fetches the full
// (un-capped, route-ceiling-bounded) result or input from
// /api/conversation/<sid>/payload, caches the result per hook instance
// (per-(toolUseId, which) since each card mounts its own), and surfaces a
// friendly message for the 410 source-gone case. No-ops when there's no open
// session id (sessionId === null).

import { useCallback, useState } from 'react';
import type { FullPayload } from '../types/conversation';

type State =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'done'; data: FullPayload }
  | { status: 'error'; error: string };

export function useFullPayload(
  sessionId: string | null,
  toolUseId: string,
  which: 'result' | 'input',
) {
  const [state, setState] = useState<State>({ status: 'idle' });
  const load = useCallback(async () => {
    // Already loaded / in flight, or nothing to address → nothing to do.
    if (state.status === 'done' || state.status === 'loading' || !sessionId) return;
    setState({ status: 'loading' });
    try {
      const url =
        `/api/conversation/${encodeURIComponent(sessionId)}/payload` +
        `?tool_use_id=${encodeURIComponent(toolUseId)}&which=${which}`;
      const r = await fetch(url);
      if (!r.ok) {
        // 410 = source JSONL rotated/deleted (the documented capped-cache
        // consequence); everything else (403 gate, 5xx, …) is generic.
        setState({
          status: 'error',
          error: r.status === 410 ? 'source no longer available' : 'unavailable',
        });
        return;
      }
      setState({ status: 'done', data: (await r.json()) as FullPayload });
    } catch {
      setState({ status: 'error', error: 'network error' });
    }
  }, [sessionId, toolUseId, which, state.status]);
  return { ...state, load };
}
