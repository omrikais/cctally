// Typed wrappers around the /api/share/* endpoints.
//
// `ShareApiError` surfaces both `status` (HTTP code) and the optional
// `field` from the typed error envelope returned by the Python
// handlers (e.g. {error: "...", field: "template_id"}). The composer
// UI (M1.11+) reads `field` to highlight the offending form input
// instead of just blasting a top-level toast.
import type {
  SharePanelId, ShareOptions, ShareRenderResponse, ShareTemplatesResponse,
} from './types';

export class ShareApiError extends Error {
  // `code` is a forward-looking slot for the typed-error envelope: the
  // Python handlers currently emit {error, field} but the spec leaves
  // room for a `code` discriminator string for future structured-error
  // paths. Declared here so it's always reflected on the instance (the
  // plan's test asserts `toMatchObject({code: undefined})` which in
  // vitest 4 requires the property to exist on the actual).
  public code: string | undefined;
  constructor(public status: number, public field?: string, message?: string, code?: string) {
    super(message);
    this.code = code;
  }
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let payload: { error?: string; field?: string; code?: string } = {};
    try { payload = await resp.json() as { error?: string; field?: string; code?: string }; } catch { /* ignore */ }
    throw new ShareApiError(
      resp.status,
      payload.field,
      payload.error ?? `HTTP ${resp.status}`,
      payload.code,
    );
  }
  return resp.json() as Promise<T>;
}

export async function fetchTemplates(
  panel: SharePanelId,
  init?: { signal?: AbortSignal },
): Promise<ShareTemplatesResponse> {
  return jsonOrThrow(await fetch(
    `/api/share/templates?panel=${encodeURIComponent(panel)}`,
    { signal: init?.signal },
  ));
}

export async function renderShare(
  args: {
    panel: SharePanelId;
    template_id: string;
    options: ShareOptions;
  },
  init?: { signal?: AbortSignal },
): Promise<ShareRenderResponse> {
  return jsonOrThrow(await fetch('/api/share/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
    signal: init?.signal,
  }));
}
