// Typed contracts for the /api/share/* endpoints.
//
// `SharePanelId` is the explicit subset of `PanelId` (from
// lib/panelIds.ts) for which the kernel can render a shareable
// snapshot. Source of truth on the Python side is
// `bin/_lib_share_templates.SHARE_CAPABLE_PANELS` (9 panels). We
// deliberately spell it as a literal union here (not
// `Exclude<PanelId, 'alerts'>`) so a future PanelId addition that
// happens to be non-share-capable doesn't silently widen this type.
import type { DashboardSelection } from '../types/envelope';

export type ShareFormat = 'md' | 'html' | 'svg';
export type ShareTheme = 'light' | 'dark';
export type SharePanelId =
  | 'current-week' | 'trend' | 'weekly' | 'daily'
  | 'monthly' | 'blocks' | 'forecast' | 'sessions'
  | 'projects';

// The per-source share matrix mirrors the canonical board. All three modes use
// the same nine share-capable panels; cache-report remains the one canonical
// card without a share kernel. This gates the picker and keyboard `S` binding.
export const SHARE_PANEL_MATRIX: Record<DashboardSelection, ReadonlySet<SharePanelId>> = {
  claude: new Set<SharePanelId>([
    'current-week', 'trend', 'weekly', 'daily', 'monthly', 'blocks', 'forecast', 'sessions', 'projects',
  ]),
  codex: new Set<SharePanelId>([
    'current-week', 'trend', 'weekly', 'daily', 'monthly', 'blocks', 'forecast', 'sessions', 'projects',
  ]),
  all: new Set<SharePanelId>([
    'current-week', 'trend', 'weekly', 'daily', 'monthly', 'blocks', 'forecast', 'sessions', 'projects',
  ]),
};

// True when `panel` is shareable under the given source selection.
export function isSharePanelAllowed(source: DashboardSelection, panel: SharePanelId): boolean {
  return SHARE_PANEL_MATRIX[source].has(panel);
}

// #294 S5 §7 — human label for a source selection (share chrome, chips, preset/
// history rows). Covers 'all' too, unlike the alert SOURCE_LABEL.
export const SELECTION_LABEL: Record<DashboardSelection, string> = {
  claude: 'Claude',
  codex: 'Codex',
  all: 'All',
};

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
  // Per-panel scalar overrides — currently only the Projects panel
  // emits this, sourced from the ProjectsModal's 1w / 4w / 8w / 12w
  // pill state via `shareModal.params.windowWeeks`. The server reads
  // it at `bin/_cctally_dashboard.py:1581` (`options.get("windowWeeks", 1)`).
  // Optional + narrowly-typed on purpose so other panels keep the
  // shape unchanged. Empty/missing → server default (`1`).
  windowWeeks?: 1 | 4 | 8 | 12;
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
  // #503 S3 §4 — which digest DEFINITION `data_digest` was minted under.
  // Stored beside the digest on a basket item and replayed at compose time;
  // optional because an older server does not send it.
  data_digest_version?: number;
  // Additive account metadata. The server omits both for agnostic shares and
  // anonymizes the label unless reveal_projects was explicitly enabled.
  account?: string;
  account_label?: string;
}

export interface ShareRenderResponse {
  body: string;
  content_type: string;
  // #503 S1 B1 — whether this export carries any project name at all. The
  // server derives it from the same snapshot it rendered, through the
  // kernel's single enumeration of typed project display sites. Optional
  // because an older server does not send it, and because the additive
  // envelope rule in `docs/cli-contract.md` means a consumer must tolerate
  // its absence rather than assume a value. `undefined` keeps the two
  // original states, which is the safe fallback: never claim "no project
  // names" without evidence.
  has_project_names?: boolean;
  snapshot: ShareSnapshot;
}

export interface ShareTemplatesResponse {
  panel: SharePanelId;
  templates: ShareTemplate[];
}
