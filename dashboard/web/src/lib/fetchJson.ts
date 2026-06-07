// dashboard/web/src/lib/fetchJson.ts
// Shared fetch primitive for the dashboard's JSON hooks. The single edit-point
// for a future CSRF header / retry / uniform error text across the migrated
// conversation hooks. HttpError carries the status so callers keep their
// status-specific branches (e.g. 404 -> "not found").
export class HttpError extends Error {
  constructor(public readonly status: number) {
    super(`HTTP ${status}`);
    this.name = 'HttpError';
  }
}

export function isAbortError(e: unknown): boolean {
  return (e as DOMException | undefined)?.name === 'AbortError';
}

export async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, signal ? { signal } : undefined);
  if (!r.ok) throw new HttpError(r.status);
  return (await r.json()) as T;
}
