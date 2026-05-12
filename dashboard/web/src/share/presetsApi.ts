// Typed wrappers around the /api/share/presets endpoints (spec §5.1,
// §11.3, plan §M2.3) and the /api/share/history ring buffer (spec §5.1,
// §11.4, plan §M4.3).
//
// Three preset endpoints — list / save / delete — keyed on (panel, name).
// Three history endpoints — list / append / clear — server-side trim to
// the last 20 records. The Python side persists both under
// `share.{presets,history}` in config.json so presets survive a browser
// reload AND a future CLI consumer can read the same shape (designed
// for, not shipped — out of scope per spec §15).
//
// Re-exports `ShareApiError` from `./api` so callers can do
//   `catch (err) { if (err instanceof ShareApiError) ... }`
// without importing two files.
import type { SharePanelId, ShareOptions } from './types';
import { ShareApiError } from './api';

export { ShareApiError };

export interface PresetRecord {
  template_id: string;
  options: ShareOptions;
  saved_at: string;
}

export interface PresetsResponse {
  presets: Record<string, Record<string, PresetRecord>>;
}

export interface SavePresetArgs {
  panel: SharePanelId;
  name: string;
  template_id: string;
  options: ShareOptions;
}

export interface SavedPreset extends PresetRecord {
  panel: SharePanelId;
  name: string;
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let payload: { error?: string; field?: string } = {};
    try {
      payload = await resp.json() as { error?: string; field?: string };
    } catch { /* ignore — non-JSON body */ }
    throw new ShareApiError(
      resp.status,
      payload.field,
      payload.error ?? `HTTP ${resp.status}`,
    );
  }
  return resp.json() as Promise<T>;
}

export async function listPresets(
  init?: { signal?: AbortSignal },
): Promise<PresetsResponse> {
  return jsonOrThrow<PresetsResponse>(
    await fetch('/api/share/presets', { signal: init?.signal }),
  );
}

export async function savePreset(
  args: SavePresetArgs,
  init?: { signal?: AbortSignal },
): Promise<SavedPreset> {
  return jsonOrThrow<SavedPreset>(await fetch('/api/share/presets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
    signal: init?.signal,
  }));
}

export async function deletePreset(
  panel: SharePanelId,
  name: string,
  init?: { signal?: AbortSignal },
): Promise<void> {
  const resp = await fetch(
    `/api/share/presets/${encodeURIComponent(panel)}/${encodeURIComponent(name)}`,
    { method: 'DELETE', signal: init?.signal },
  );
  // The server emits 204 on success — `Response.ok` covers 200-299, so
  // this branch only fires on 4xx/5xx. We still guard for a body so the
  // toast/snackbar gets a meaningful message rather than `HTTP 404`.
  if (!resp.ok) {
    let payload: { error?: string } = {};
    try {
      payload = await resp.json() as { error?: string };
    } catch { /* ignore — non-JSON body */ }
    throw new ShareApiError(
      resp.status,
      undefined,
      payload.error ?? `HTTP ${resp.status}`,
    );
  }
}

// ---- /api/share/history — spec §11.4, plan §M4.3 ----------------------
//
// A 20-deep ring buffer of export recipes. Recorded after every
// successful Copy/Download/Open/PNG/Print export from the share modal.
// The PresetDropdown shows the last 20 entries for the current panel
// under a "Recent shares" group. Clicking a row re-applies the recipe;
// it does NOT auto-export (the user must re-confirm).
//
// Server fields stamped on POST:
//   - `recipe_id`: random hex; opaque (we order by insertion, not by id).
//   - `exported_at`: ISO-8601 UTC.

export interface HistoryRecord {
  recipe_id: string;
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  // `format` and `destination` are advisory display hints. The server
  // accepts string-or-null so a misconfigured client can't 400 itself
  // out of recording history; the dropdown row treats null/missing as
  // "(unknown)".
  format: string | null;
  destination: string | null;
  exported_at: string;
}

export interface HistoryResponse {
  history: HistoryRecord[];
}

// POST body — `recipe_id` and `exported_at` are stamped by the server.
export interface AppendHistoryArgs {
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  format: string;
  destination: string;
}

export async function listHistory(
  init?: { signal?: AbortSignal },
): Promise<HistoryResponse> {
  return jsonOrThrow<HistoryResponse>(
    await fetch('/api/share/history', { signal: init?.signal }),
  );
}

export async function appendHistory(
  args: AppendHistoryArgs,
  init?: { signal?: AbortSignal },
): Promise<HistoryRecord> {
  return jsonOrThrow<HistoryRecord>(await fetch('/api/share/history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
    signal: init?.signal,
  }));
}

export async function clearHistory(
  init?: { signal?: AbortSignal },
): Promise<void> {
  const resp = await fetch('/api/share/history', {
    method: 'DELETE',
    signal: init?.signal,
  });
  // The server emits 204 on success — `Response.ok` covers 200-299, so
  // this branch only fires on 4xx/5xx.
  if (!resp.ok) {
    let payload: { error?: string } = {};
    try {
      payload = await resp.json() as { error?: string };
    } catch { /* ignore — non-JSON body */ }
    throw new ShareApiError(
      resp.status,
      undefined,
      payload.error ?? `HTTP ${resp.status}`,
    );
  }
}
