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
import type { SharePanelId, ShareOptions, ShareTheme } from './types';
import type { DashboardSelection } from '../types/envelope';
import { ShareApiError } from './api';

export { ShareApiError };

export interface PresetRecord {
  template_id: string;
  options: ShareOptions;
  saved_at: string;
  // #294 S5 §7 — the source the preset was saved under. Presets are keyed by
  // (panel, name) server-side (unchanged), so saving under another source
  // OVERWRITES the record and updates this stored source; the dropdown shows the
  // stored-source label. Optional on read (legacy presets → treated as claude).
  source?: DashboardSelection;
}

export interface PresetsResponse {
  presets: Record<string, Record<string, PresetRecord>>;
}

export interface SavePresetArgs {
  panel: SharePanelId;
  name: string;
  template_id: string;
  options: ShareOptions;
  // #294 S5 §7 — stamp the share flow's source; the server records it against
  // the (panel, name) key (overwrite semantics preserved).
  source?: DashboardSelection;
  // #503 S3 §1 — sent ONLY after the user confirms a collision. Absent means
  // false server-side, so a save onto an existing name 409s by default.
  overwrite?: boolean;
}

export interface SavedPreset extends PresetRecord {
  panel: SharePanelId;
  name: string;
}

// #503 S3 §1 — rename is ONE server-side operation, not a save plus a
// delete. The old client-side pair rebuilt the record from the four fields
// it held, so it dropped `source` and reset `saved_at`; the endpoint moves
// the stored record whole. `overwrite` is only ever sent after the user
// confirms a collision — absent means false server-side.
export interface RenamePresetArgs {
  panel: SharePanelId;
  from_name: string;
  to_name: string;
  overwrite?: boolean;
}

// The `code` a collision answers with, on both mutations. The UI enters its
// confirmation on THIS discriminator rather than on its own name-list
// preflight, which can go stale between the GET and the write.
export const PRESET_NAME_CONFLICT = 'preset_name_conflict';

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let payload: { error?: string; field?: string; code?: string } = {};
    try {
      payload = await resp.json() as {
        error?: string; field?: string; code?: string;
      };
    } catch { /* ignore — non-JSON body */ }
    throw new ShareApiError(
      resp.status,
      payload.field,
      payload.error ?? `HTTP ${resp.status}`,
      // #503 S3 §1 — carry the machine-readable code so a caller can
      // branch on `preset_name_conflict` instead of matching prose.
      payload.code,
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

export async function renamePreset(
  args: RenamePresetArgs,
  init?: { signal?: AbortSignal },
): Promise<SavedPreset> {
  return jsonOrThrow<SavedPreset>(await fetch('/api/share/presets/rename', {
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

// #503 S3 §3 — a history row is a discriminated union on `kind`. `kind` is
// OPTIONAL on read: a row written before the discriminator existed carries
// none and means "panel". Read it through `historyRowKind` rather than
// comparing the raw field, so the legacy default lives in one place.
export interface HistoryFields {
  recipe_id: string;
  // `format` and `destination` are advisory display hints. The server
  // accepts string-or-null so a misconfigured client can't 400 itself
  // out of recording history; the dropdown row treats null/missing as
  // "(unknown)".
  format: string | null;
  destination: string | null;
  exported_at: string;
}

export interface PanelHistoryRecord extends HistoryFields {
  kind?: 'panel';
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  // #294 S5 §7 — the source the export was made under. Source participates in
  // history/digest identity server-side, so "same panel, different source" rows
  // are DISTINCT (no dedup/collapse). Optional on read (legacy → claude).
  source?: DashboardSelection;
  account?: string;
}

export interface ComposedHistorySection {
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  source?: DashboardSelection;
  account?: string;
}

export interface ComposedHistoryRecord extends HistoryFields {
  kind: 'composed';
  // Explicitly null, which is what makes an older client's
  // `h.panel === panel` filter hide the row rather than mis-render it.
  panel: null;
  sections: ComposedHistorySection[];
  composite: {
    title: string;
    theme: ShareTheme;
    reveal_projects: boolean;
    no_branding: boolean;
  };
}

export type HistoryRecord = PanelHistoryRecord | ComposedHistoryRecord;

export function historyRowKind(row: HistoryRecord): 'panel' | 'composed' {
  return row.kind ?? 'panel';
}

export function isComposedHistoryRow(
  row: HistoryRecord,
): row is ComposedHistoryRecord {
  return historyRowKind(row) === 'composed';
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
  // #294 S5 §7 — the share flow's captured source (stamped explicitly).
  source?: DashboardSelection;
  account?: string | null;
}

// POST body for a composed export (#503 S3 §3). One row per composed
// document, shown in every panel's Recent shares and display-only.
export interface AppendComposedHistoryArgs {
  sections: ComposedHistorySection[];
  composite: ComposedHistoryRecord['composite'];
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
  const { account, ...legacyArgs } = args;
  return jsonOrThrow<HistoryRecord>(await fetch('/api/share/history', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(account == null ? legacyArgs : { ...legacyArgs, account }),
    signal: init?.signal,
  }));
}

export async function appendComposedHistory(
  args: AppendComposedHistoryArgs,
  init?: { signal?: AbortSignal },
): Promise<ComposedHistoryRecord> {
  return jsonOrThrow<ComposedHistoryRecord>(
    await fetch('/api/share/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'composed', ...args }),
      signal: init?.signal,
    }),
  );
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
