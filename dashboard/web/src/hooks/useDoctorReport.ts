// Doctor full-report fetch hook (spec §6 / §5.6).
//
// The SSE envelope only carries the aggregate `doctor` block (severity
// + counts + generated_at + fingerprint, ~120 bytes). The full
// per-check tree is fetched lazily from `GET /api/doctor` so the SSE
// channel stays small. `useDoctorReport` is the React-side cache for
// that fetch — the DoctorModal calls `refresh()` once on open and
// whenever the user clicks the refresh button.
//
// The kernel's `fingerprint` is hashed over an identity slice (check
// ids + severities + overall counts; spec §5.5), so two ticks with
// identical findings collapse to one fetch even when the per-check
// `details` blocks ripple (e.g. last-fire age ticking up second by
// second). A future iteration may auto-refresh on fingerprint change
// — for v1, refresh is manual (per spec §6.3 "reading-friendly").
import { useState, useCallback } from 'react';

export type DoctorSeverity = 'ok' | 'warn' | 'fail';

export interface DoctorCheck {
  id: string;
  title: string;
  severity: DoctorSeverity;
  summary: string;
  remediation?: string;
  details: Record<string, unknown>;
}

export interface DoctorCategory {
  id: string;
  title: string;
  severity: DoctorSeverity;
  checks: DoctorCheck[];
}

export interface DoctorReport {
  schema_version: number;
  generated_at: string;
  cctally_version: string;
  overall: { severity: DoctorSeverity; counts: { ok: number; warn: number; fail: number } };
  categories: DoctorCategory[];
}

export interface UseDoctorReportResult {
  report: DoctorReport | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useDoctorReport(): UseDoctorReportResult {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/doctor');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as DoctorReport;
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  return { report, loading, error, refresh };
}
