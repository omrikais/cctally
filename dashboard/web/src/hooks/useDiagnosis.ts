import { useCallback, useEffect, useState } from 'react';
import { diagnosisUrl, type DiagnosisReport, type DiagnosisRequest } from '../lib/diagnosis';

// #620 S2 — the on-demand fetch behind the explain modal.
//
// Deliberately not an envelope key and not an SSE frame: the diagnosis is a
// surface most ticks never display, and paying for it per frame would put
// #607's client cost on every tab whether or not anyone opened it.
//
// The one thing this hook must get right is the 503 body. A report whose
// requested provider's store could not be READ is published with 503 AND with
// the whole withheld report as its body, because the typed cause is what the
// reader needs. Treating a 503 as a transport failure would replace that cause
// with "HTTP 503", which says less than the server already said.

export interface UseDiagnosisResult {
  report: DiagnosisReport | null;
  loading: boolean;
  /** A transport or request failure — NOT a withheld report, which is a
   *  successful answer and arrives as `report`. */
  error: string | null;
  /** #620 S3 spec 4.7 / 5.5. `generation_incoherent` means a component moved
   *  twice while it was read, which is a store under active write rather than
   *  a broken one: the same request a moment later normally succeeds. The
   *  modal presents it as a retryable state instead of a failure, so the code
   *  the server took the trouble to name is surfaced here rather than
   *  flattened into "HTTP 503". */
  retryable: boolean;
  retry: () => void;
}

function isReport(payload: unknown): payload is DiagnosisReport {
  return (
    typeof payload === 'object' && payload !== null
    && Array.isArray((payload as { results?: unknown }).results)
  );
}

export function useDiagnosis(request: DiagnosisRequest): UseDiagnosisResult {
  const [report, setReport] = useState<DiagnosisReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  // Bumping this re-runs the effect without changing the URL, which is what a
  // retry of the SAME request needs. Without it the effect's dependency list
  // would have to carry a mutable object and would re-fetch on every render.
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => { setAttempt((n) => n + 1); }, []);
  const url = diagnosisUrl(request);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setRetryable(false);
    fetch(url, { signal: controller.signal })
      .then(async (response) => {
        const payload: unknown = await response.json().catch(() => null);
        if (cancelled) return;
        if (response.ok || (response.status === 503 && isReport(payload))) {
          if (isReport(payload)) {
            setReport(payload);
            setError(null);
          } else {
            setReport(null);
            setError('The diagnosis response could not be read.');
          }
          return;
        }
        setReport(null);
        const body = payload as { error?: string; code?: string } | null;
        setRetryable(body?.code === 'generation_incoherent');
        setError(body?.error ?? `HTTP ${response.status}`);
      })
      .catch((err: unknown) => {
        if (cancelled || (err as DOMException | undefined)?.name === 'AbortError') return;
        setReport(null);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [url, attempt]);

  return { report, loading, error, retryable, retry };
}
