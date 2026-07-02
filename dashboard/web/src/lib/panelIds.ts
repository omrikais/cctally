// Canonical panel id type + default ordering.
//
// This module deliberately has zero imports: it is consumed by both
// `panelRegistry.ts` (which pulls in all panel components and therefore
// also pulls in the store) AND by `store/store.ts` itself. Putting the
// constants here breaks the otherwise-circular `panelRegistry → panels →
// store → panelRegistry` import cycle that would leave
// `DEFAULT_PANEL_ORDER` in the temporal dead zone during store module
// init.

// S8 (#254): the weekly/monthly/daily grid tiles collapsed into one
// `history` card (the relabeled heatmap). `history` is a grid PanelId; the
// share backend still knows daily/weekly/monthly as SharePanelId (see
// share/types.ts), reached from the History modal's period toggle.
export type PanelId =
  | 'current-week'
  | 'forecast'
  | 'trend'
  | 'sessions'
  | 'projects'
  | 'history'
  | 'blocks'
  | 'alerts'
  | 'cache-report';

// Grid-only ids (#248): `current-week` left the grid — it is now the hero
// strip (HeroStrip), opened via click/Enter, not a digit. It STAYS a valid
// `PanelId` so `ModalKind` / `SharePanelId` / the Current Week modal + share
// path continue to compile. Everything that owns a GRID card — the default
// order, the registry, the tier map, `openPanelByPosition`, `prefs.panelOrder`
// — is typed to `GridPanelId` so the grid can never address `current-week`.
export type GridPanelId = Exclude<PanelId, 'current-week'>;

// #264 S1: bento left-to-right order. Fresh-state / reset installs render
// tall(sessions·trend·projects) → medium(history·cache-report) →
// short(forecast·blocks·alerts). Digit-key mapping follows this order, so
// `1` opens Sessions. Existing users keep their persisted order — reconcile
// preserves saved positions, and the bento is order-independent (each card's
// row + span is static per-id in CARD_LAYOUT), so no schema bump is needed.
export const DEFAULT_PANEL_ORDER: GridPanelId[] = [
  'sessions', 'trend', 'projects',
  'history', 'cache-report',
  'forecast', 'blocks', 'alerts',
];

// #264 S1: the #248 tile/wide two-tier map is replaced by a height-matched
// 12-col bento. Each card has a fixed row-class + column span (per-row spans
// sum to 12). App partitions prefs.panelOrder into three DndContext rows by
// `.row`; the grid places cards by `.span`; SWAP_PANELS skips within a `.row`.
export const CARD_LAYOUT: Record<GridPanelId, { row: 'tall' | 'medium' | 'short'; span: number }> = {
  sessions: { row: 'tall', span: 6 },
  trend:    { row: 'tall', span: 3 },
  projects: { row: 'tall', span: 3 },
  history:        { row: 'medium', span: 8 },
  'cache-report': { row: 'medium', span: 4 },
  forecast: { row: 'short', span: 4 },
  blocks:   { row: 'short', span: 4 },
  alerts:   { row: 'short', span: 4 },
};

// Panels for which a share affordance is rendered (spec §6.1, plan §M1.9).
//
// Two id families live here, so it is keyed by `string` (a DOM-derived
// `data-panel-kind` membership gate), NOT `PanelId`:
//   • the Python share ids (`bin/_lib_share_templates.SHARE_CAPABLE_PANELS`,
//     the source of truth — 9 hyphenated entries) which the modal ShareIcons
//     and the composer still key off (SharePanelId, share/types.ts). This
//     keeps daily/weekly/monthly here even though they left the grid.
//   • S8 (#254) `history` — the grid card whose id is a PanelId but NOT a
//     SharePanelId; global `S` on it routes through `gridPanelToSharePanel`
//     (history → daily) in share/keyboardShare.ts before the cast.
// Alerts is excluded by design: it's a notification stream, not a report.
export const SHARE_CAPABLE_PANELS: ReadonlySet<string> = new Set<string>([
  'current-week', 'forecast', 'trend', 'sessions', 'projects',
  'weekly', 'monthly', 'blocks', 'daily', 'history',
]);  // intentionally excludes 'alerts'
