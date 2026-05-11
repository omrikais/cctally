// Typed wrappers around the /api/share/presets endpoints (spec §5.1,
// §11.3, plan §M2.3).
//
// Three endpoints — list / save / delete — keyed on (panel, name). The
// Python side persists to config.json so presets survive a browser
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
