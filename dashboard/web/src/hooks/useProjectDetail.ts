// useProjectDetail — stale-while-revalidate lazy fetcher for the
// per-project drill (spec §5.3, plan Task 5 Step 3).
//
// Mirrors SessionModal's SWR pattern: a fetch fires on
// (projectKey, windowWeeks) change and refires on every SSE tick (via
// the snapshot's `generated_at`). The previously-rendered `data` stays
// mounted across ticks ("stale-while-revalidate"); only an initial
// fetch (new key, new window, or no prior data) flips the `loading`
// flag. Non-404 refetch failures are silently swallowed so a transient
// blip does not yank good content from under the user.
//
// 404 grace policy: the dashboard envelope updates the project list
// every tick, but the drill endpoint reads from the cache directly. A
// one-tick 404 is therefore treated as transient (a single race between
// envelope and cache); a second consecutive 404 evicts to the "no
// longer in cache" error. Successful refetches (or non-404 failures)
// clear the arm. The arm is also cleared on (key, window) change so a
// new drill does not inherit a prior project's armed state.
//
// `data == null` in the `isInitial` predicate covers the abort-and-
// retry case: if a tick aborts an in-flight initial fetch before it
// resolves, lastKeyRef is already set but no content has rendered —
// without this guard the retry would be classified as a refetch (a 404
// would skip the eviction path and a network error would be silently
// swallowed), leaving the modal stuck on the spinner.
//
// `inFlightRef` prevents the endless-loading bug surfaced in Playwright
// e2e testing: when /api/project/<heavy-key>?weeks=12 takes longer than
// the SSE tick interval (e.g. ~10s fetch vs ~5s ticks for a 500+ session
// project over 12 weeks), the bare effect would cleanup-abort every
// in-flight initial fetch on each generatedAt refire. Because `data`
// stays null and `isInitial` keeps re-firing setLoading(true), the
// drill rendered "Loading…" indefinitely. Guarding the effect body on
// `inFlightRef.current && isInitial` lets the original fetch resolve
// naturally; subsequent SSE-tick revalidations only fire AFTER data is
// loaded (where the SWR pattern is correct and welcome).
import { useEffect, useRef, useState } from 'react';
import { useSnapshot } from './useSnapshot';
import type { ProjectDetail } from '../types/envelope';

export interface ProjectDetailState {
  data: ProjectDetail | null;
  loading: boolean;
  error: string | null;
}

export function useProjectDetail(
  projectKey: string | null,
  windowWeeks: number,
): ProjectDetailState {
  const [data, setData] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  const consec404Ref = useRef(0);
  const lastKeyRef = useRef<string | null>(null);
  const lastWindowRef = useRef<number | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!projectKey) {
      setData(null);
      setLoading(false);
      setError(null);
      consec404Ref.current = 0;
      lastKeyRef.current = null;
      lastWindowRef.current = null;
      inFlightRef.current = false;
      return;
    }
    const keyOrWindowChanged =
      lastKeyRef.current !== projectKey ||
      lastWindowRef.current !== windowWeeks;
    const isInitial = keyOrWindowChanged || data == null;
    // SSE-tick race guard: if an initial fetch is already in flight for
    // the same (key, window), let it resolve. Don't abort + restart on
    // every generatedAt change — that's the bug.
    if (isInitial && inFlightRef.current && !keyOrWindowChanged) {
      return;
    }
    if (isInitial) {
      setLoading(true);
      setError(null);
    }
    // Reset the 404 arm on (key, window) change so a new drill does not
    // inherit a prior project's armed state.
    if (keyOrWindowChanged) {
      consec404Ref.current = 0;
    }
    lastKeyRef.current = projectKey;
    lastWindowRef.current = windowWeeks;
    inFlightRef.current = true;
    const ctl = new AbortController();
    const url = `/api/project/${encodeURIComponent(projectKey)}?weeks=${windowWeeks}`;
    fetch(url, { signal: ctl.signal })
      .then(async (r) => {
        if (r.status === 404) {
          consec404Ref.current += 1;
          if (consec404Ref.current >= 2) {
            setData(null);
            setError('Project no longer in cache — close this drill.');
            setLoading(false);
          } else if (isInitial) {
            // First 404 on an initial fetch: surface the error but
            // arm for a retry on next tick (which might recover if the
            // cache catches up). We don't evict on the first strike.
            setError("Couldn't load project detail — will retry on next tick.");
            setLoading(false);
          }
          return;
        }
        if (!r.ok) {
          if (isInitial) {
            setError("Couldn't load project detail — will retry on next tick.");
            setLoading(false);
          }
          return;
        }
        consec404Ref.current = 0;
        const body = (await r.json()) as ProjectDetail;
        setData(body);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if ((err as DOMException)?.name === 'AbortError') return;
        if (isInitial) {
          setError("Couldn't load project detail — will retry on next tick.");
          setLoading(false);
        }
      })
      .finally(() => {
        inFlightRef.current = false;
      });
    return () => ctl.abort();
    // We intentionally exclude `data` from the dep array: including it
    // would refire the effect after every successful fetch (setData →
    // re-run → fetch again), causing an infinite loop. The `data == null`
    // check inside the effect uses the closure's value, which is fine
    // because the only branch that depends on it is the initial-fetch
    // classification at the start (and the effect runs again on
    // generatedAt/key/window changes anyway).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey, windowWeeks, generatedAt]);

  return { data, loading, error };
}
