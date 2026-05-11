// Typed wrapper around POST /api/share/compose (spec §5.3).
//
// The compose endpoint is recipe-only: the server re-renders every
// section from its (panel, template_id, options) recipe. The client
// never submits rendered bodies. Per-section drift detection compares
// `data_digest_at_add` (recorded when the basket item was added) against
// the digest computed from the freshly-built panel_data; mismatches
// surface as `section_results[i].drift_detected: true` so the composer
// can render the "Outdated" badge.
//
// `buildComposeRequest` is the single helper the modal uses to assemble
// the POST body from its (basket, composite knobs) inputs — keeps the
// shape contract in one place so a future schema bump only edits one
// builder, not a half-dozen call sites.
import type { BasketItem } from '../store/basketSlice';
import { ShareApiError } from './api';

export interface ComposeRequest {
  title: string;
  theme: 'light' | 'dark';
  format: 'md' | 'html' | 'svg';
  no_branding: boolean;
  reveal_projects: boolean;
  sections: Array<{
    snapshot: {
      panel: BasketItem['panel'];
      template_id: string;
      options: BasketItem['options'];
      data_digest_at_add: string;
      kernel_version: number;
    };
  }>;
}

export interface ComposeSectionResult {
  snapshot_id: string;
  drift_detected: boolean;
  data_digest_at_add: string;
  data_digest_now: string;
}

export interface ComposeResponse {
  body: string;
  content_type: string;
  snapshot: {
    kernel_version: number;
    composed_at: string;
    section_results: ComposeSectionResult[];
  };
}

export function buildComposeRequest(
  basket: BasketItem[],
  opts: {
    title: string;
    theme: 'light' | 'dark';
    format: 'md' | 'html' | 'svg';
    no_branding: boolean;
    reveal_projects: boolean;
  },
): ComposeRequest {
  return {
    title: opts.title,
    theme: opts.theme,
    format: opts.format,
    no_branding: opts.no_branding,
    reveal_projects: opts.reveal_projects,
    sections: basket.map((it) => ({
      snapshot: {
        panel: it.panel,
        template_id: it.template_id,
        options: it.options,
        data_digest_at_add: it.data_digest_at_add,
        kernel_version: it.kernel_version,
      },
    })),
  };
}

export async function composeShare(
  req: ComposeRequest,
  init?: { signal?: AbortSignal },
): Promise<ComposeResponse> {
  const resp = await fetch('/api/share/compose', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
    signal: init?.signal,
  });
  if (!resp.ok) {
    let payload: { error?: string; field?: string; code?: string } = {};
    try {
      payload = await resp.json() as { error?: string; field?: string; code?: string };
    } catch { /* ignore */ }
    throw new ShareApiError(
      resp.status,
      payload.field,
      payload.error ?? `HTTP ${resp.status}`,
      payload.code,
    );
  }
  return resp.json() as Promise<ComposeResponse>;
}
