// #294 S5 §5.6 — the qualified source-detail fetch hook.
//
// Fetches `/api/source/<source>/<resource>/<key>`, unwraps the
// `{source, resource, data}` success envelope, and hands back the body (a
// `detail_kind`-discriminated adapter shape). Follows the useProjectDetail SWR
// conventions: revalidate on `revalToken(env)` (the #300 all-inputs
// data_version, falling back to generated_at), a monotonic `reqIdRef`
// stale-guard (never abort an in-flight initial fetch), and a 404-grace of two.
// The two stable error envelopes (`source_capability_unavailable` HTTP 400,
// `source_resource_not_found` HTTP 404) surface as friendly non-fatal variants.
//
// `all`-selection rows fetch under the OWNING provider's source (never `all`):
// the caller passes `source: 'claude' | 'codex'` from the row itself.
import { useEffect, useRef, useState } from 'react';
import { useSnapshot } from './useSnapshot';
import { revalToken } from '../lib/revalToken';
import type { QualifiedDetailEnvelope, SourceName } from '../types/envelope';

export type SourceResource = 'session' | 'project' | 'block';

export type SourceDetailError =
  | { kind: 'capability'; code: 'source_capability_unavailable' } // HTTP 400
  | { kind: 'not-found'; code: 'source_resource_not_found' } //      HTTP 404
  | { kind: 'network' };

export interface SourceDetailState<T> {
  data: T | null;
  error: SourceDetailError | null;
  loading: boolean;
}

export interface SourceDetailOptions {
  windowWeeks?: 1 | 4 | 8 | 12;
}

export function useSourceDetail<T extends { detail_kind: string }>(
  source: SourceName,
  resource: SourceResource,
  key: string | null,
  options: SourceDetailOptions = {},
): SourceDetailState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<SourceDetailError | null>(null);
  const [loading, setLoading] = useState(false);
  const env = useSnapshot();
  const token = revalToken(env);

  const consec404Ref = useRef(0);
  const lastKeyRef = useRef<string | null>(null);
  const inFlightRef = useRef(false);
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (!key) {
      setData(null);
      setError(null);
      setLoading(false);
      consec404Ref.current = 0;
      lastKeyRef.current = null;
      inFlightRef.current = false;
      reqIdRef.current += 1;
      return;
    }
    const windowWeeks = resource === 'project' ? options.windowWeeks : undefined;
    const identity = `${source}/${resource}/${key}/${windowWeeks ?? ''}`;
    const keyChanged = lastKeyRef.current !== identity;
    const isInitial = keyChanged || data == null;
    // Let an in-flight initial fetch for the same identity resolve; don't
    // restart on a token change.
    if (isInitial && inFlightRef.current && !keyChanged) return;
    if (isInitial) {
      setLoading(true);
      setError(null);
    }
    if (keyChanged) consec404Ref.current = 0;
    lastKeyRef.current = identity;
    inFlightRef.current = true;
    const myReqId = ++reqIdRef.current;
    const query = windowWeeks == null ? '' : `?weeks=${windowWeeks}`;
    const url = `/api/source/${source}/${resource}/${encodeURIComponent(key)}${query}`;
    fetch(url)
      .then(async (r) => {
        if (reqIdRef.current !== myReqId) return; // superseded
        if (r.status === 404) {
          consec404Ref.current += 1;
          if (consec404Ref.current >= 2) {
            setData(null);
            setError({ kind: 'not-found', code: 'source_resource_not_found' });
            setLoading(false);
          } else if (isInitial) {
            setError({ kind: 'not-found', code: 'source_resource_not_found' });
            setLoading(false);
          }
          return;
        }
        if (r.status === 400) {
          if (isInitial) {
            setError({ kind: 'capability', code: 'source_capability_unavailable' });
            setLoading(false);
          }
          return;
        }
        if (!r.ok) {
          if (isInitial) {
            setError({ kind: 'network' });
            setLoading(false);
          }
          return;
        }
        consec404Ref.current = 0;
        const body = (await r.json()) as QualifiedDetailEnvelope<T>;
        if (reqIdRef.current !== myReqId) return; // superseded during json()
        setData(body.data);
        setError(null);
        setLoading(false);
      })
      .catch(() => {
        if (reqIdRef.current !== myReqId) return;
        if (isInitial) {
          setError({ kind: 'network' });
          setLoading(false);
        }
      })
      .finally(() => {
        if (reqIdRef.current === myReqId) inFlightRef.current = false;
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, resource, key, options.windowWeeks, token]);

  return { data, error, loading };
}
