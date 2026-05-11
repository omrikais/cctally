// Typed contracts for the /api/share/* endpoints.
//
// `SharePanelId` is the explicit subset of `PanelId` (from
// lib/panelIds.ts) for which the kernel can render a shareable
// snapshot. Source of truth on the Python side is
// `bin/_lib_share_templates.SHARE_CAPABLE_PANELS` (8 panels). We
// deliberately spell it as a literal union here (not
// `Exclude<PanelId, 'alerts'>`) so a future PanelId addition that
// happens to be non-share-capable doesn't silently widen this type.
export type ShareFormat = 'md' | 'html' | 'svg';
export type ShareTheme = 'light' | 'dark';
export type SharePanelId =
  | 'current-week' | 'trend' | 'weekly' | 'daily'
  | 'monthly' | 'blocks' | 'forecast' | 'sessions';

export interface SharePeriod {
  kind: 'current' | 'previous' | 'custom';
  start?: string;  // ISO; required when kind=custom
  end?: string;
}

export interface ShareOptions {
  format: ShareFormat;
  theme: ShareTheme;
  reveal_projects: boolean;
  no_branding: boolean;
  top_n: number | null;
  period: SharePeriod;
  project_allowlist: string[] | null;
  show_chart: boolean;
  show_table: boolean;
}

export interface ShareTemplate {
  id: string;
  label: string;
  description: string;
  default_options: Partial<ShareOptions>;
}

export interface ShareSnapshot {
  kernel_version: number;
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  generated_at: string;
  data_digest: string;
}

export interface ShareRenderResponse {
  body: string;
  content_type: string;
  snapshot: ShareSnapshot;
}

export interface ShareTemplatesResponse {
  panel: SharePanelId;
  templates: ShareTemplate[];
}
