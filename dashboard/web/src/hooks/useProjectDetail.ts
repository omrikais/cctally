// useProjectDetail — stale-while-revalidate lazy fetcher for the
// per-project drill (spec §5.3, plan Task 5 Step 3).
//
// Mirrors SessionModal's SWR pattern: a fetch fires on
// (projectKey, windowWeeks) change and refires on the change signal
// `revalToken(env)` (#300 — the all-inputs `data_version`, falling back
// to the snapshot's `generated_at`), NOT on every 5s heartbeat tick. The
// previously-rendered `data` stays mounted across ticks
// ("stale-while-revalidate"); only an initial fetch (new key, new
// window, or no prior data) flips the `loading` flag. Non-404 refetch
// failures are silently swallowed so a transient blip does not yank good
// content from under the user.
//
// 404 grace policy: the dashboard envelope updates the project list
// every tick, but the drill endpoint reads from the cache directly. A
// one-tick 404 is therefore treated as transient (a single race between
// envelope and cache); a second consecutive 404 evicts to the "no
// longer in cache" error. Successful refetches (or non-404 failures)
// clear the arm. The arm is also cleared on (key, window) change so a
// new drill does not inherit a prior project's armed state. Under
// `data_version` gating (#300) the second tap arrives on the next data
// change (a `cache-sync --rebuild` churns `data_version` repeatedly);
// the rare quiescent targeted-prune case is a documented residual.
//
// `data == null` in the `isInitial` predicate covers the interrupt-and-
// retry case: if an initial fetch is superseded before it resolves,
// lastKeyRef is already set but no content has rendered — without this
// guard the retry would be classified as a refetch (a 404 would skip the
// eviction path and a network error would be silently swallowed),
// leaving the modal stuck on the spinner.
//
// #300 — the fetch is NEVER aborted (previously a per-effect
// AbortController aborted the in-flight initial fetch on every token
// change, and the `inFlightRef` guard then declined to restart it —
// which, once the 5s heartbeat no longer re-fired the effect, stranded
// the drill on "Loading…" forever). Instead a monotonic `reqIdRef`
// stale-guard drops any response whose request is no longer current (a
// key/window change bumped the id). A token change during an in-flight
// initial fetch now lets that fetch complete naturally (the `inFlightRef`
// guard declines to restart; no supersession bumps its id), so there is
// no strand and no abort-thrash for a fetch slower than the tick.
import { useEffect, useRef, useState } from 'react';
import { useSnapshot } from './useSnapshot';
import { revalToken } from '../lib/revalToken';
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
  // #300 — revalidate on the change signal (`data_version`), not the 5s
  // `generated_at` heartbeat, so a finished/static drill fetches once instead of
  // re-GET every tick. Falls back to `generated_at`. See `lib/revalToken.ts`.
  const token = revalToken(env);
  const consec404Ref = useRef(0);
  const lastKeyRef = useRef<string | null>(null);
  const lastWindowRef = useRef<number | null>(null);
  const inFlightRef = useRef(false);
  // Monotonic request id: bumped each time a NEW fetch starts. A response
  // whose captured id is no longer current (a later fetch superseded it — a
  // key/window change) is dropped without committing. Replaces the prior
  // AbortController so an in-flight INITIAL fetch is never cancelled (#300).
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (!projectKey) {
      setData(null);
      setLoading(false);
      setError(null);
      consec404Ref.current = 0;
      lastKeyRef.current = null;
      lastWindowRef.current = null;
      inFlightRef.current = false;
      reqIdRef.current += 1; // invalidate any in-flight response
      return;
    }
    const keyOrWindowChanged =
      lastKeyRef.current !== projectKey ||
      lastWindowRef.current !== windowWeeks;
    const isInitial = keyOrWindowChanged || data == null;
    // Race guard: if an initial fetch is already in flight for the same
    // (key, window), let it resolve — do NOT start a second one on a token
    // change. Its response is not superseded (no reqId bump here), so it
    // commits normally.
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
    const myReqId = ++reqIdRef.current;
    const url = `/api/project/${encodeURIComponent(projectKey)}?weeks=${windowWeeks}`;
    fetch(url)
      .then(async (r) => {
        if (reqIdRef.current !== myReqId) return; // superseded — drop
        if (r.status === 404) {
          consec404Ref.current += 1;
          if (consec404Ref.current >= 2) {
            setData(null);
            setError('Project no longer in cache — close this drill.');
            setLoading(false);
          } else if (isInitial) {
            // First 404 on an initial fetch: surface the error but
            // arm for a retry on the next data change (which might recover
            // if the cache catches up). We don't evict on the first strike.
            setError("Couldn't load project detail — will retry on next update.");
            setLoading(false);
          }
          return;
        }
        if (!r.ok) {
          if (isInitial) {
            setError("Couldn't load project detail — will retry on next update.");
            setLoading(false);
          }
          return;
        }
        consec404Ref.current = 0;
        const body = (await r.json()) as ProjectDetail;
        if (reqIdRef.current !== myReqId) return; // superseded during json() — drop
        setData(body);
        setError(null);
        setLoading(false);
      })
      .catch(() => {
        if (reqIdRef.current !== myReqId) return; // superseded — drop
        if (isInitial) {
          setError("Couldn't load project detail — will retry on next update.");
          setLoading(false);
        }
        // Refetch failures (non-initial) → silently keep stale data.
      })
      .finally(() => {
        // Only the current request clears the in-flight flag; a superseded
        // request finishing must not clear it out from under the live one.
        if (reqIdRef.current === myReqId) {
          inFlightRef.current = false;
        }
      });
    // NO cleanup-abort (#300): the reqId stale-guard supersedes a stale-key
    // response instead. `data` is intentionally excluded from the deps —
    // including it would refire after every successful fetch (setData →
    // re-run → fetch again). The effect re-runs on token/key/window changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey, windowWeeks, token]);

  return { data, loading, error };
}
