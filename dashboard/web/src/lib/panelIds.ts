// Canonical panel id type + default ordering.
//
// This module deliberately has zero imports: it is consumed by both
// `panelRegistry.ts` (which pulls in all panel components and therefore
// also pulls in the store) AND by `store/store.ts` itself. Putting the
// constants here breaks the otherwise-circular `panelRegistry → panels →
// store → panelRegistry` import cycle that would leave
// `DEFAULT_PANEL_ORDER` in the temporal dead zone during store module
// init.

// S2 (#264): the S8 `history` card is un-collapsed back into three
// independent Daily / Weekly / Monthly peer grid tiles. `history` is gone as
// a grid id; `daily`/`weekly`/`monthly` are grid PanelIds again, each opening
// its own modal at its own period (no Day·Week·Month toggle). They already
// were SharePanelIds (see share/types.ts), so the keyboardShare history→daily
// shim is removed.
export type PanelId =
  | 'current-week'
  | 'forecast'
  | 'trend'
  | 'sessions'
  | 'projects'
  | 'daily'
  | 'weekly'
  | 'monthly'
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

// #264 S2: bento order — tall(sessions·trend·projects) → medium 2×2
// (daily·cache-report / weekly·monthly) → short(forecast·blocks·alerts).
// Digit keys follow this order (1=sessions … 0=alerts, the 10th). Existing
// users keep their persisted order — reconcile preserves saved positions, and
// the bento is order-independent (each card's row + span is static per-id in
// CARD_LAYOUT). The v4→v5 panel-order migration reinstates weekly/monthly.
export const DEFAULT_PANEL_ORDER: GridPanelId[] = [
  'sessions', 'trend', 'projects',
  'daily', 'cache-report', 'weekly', 'monthly',
  'forecast', 'blocks', 'alerts',
];

// #264 S2: the medium row holds FOUR span-6 cards → it wraps into two implicit
// grid rows (Daily|Cache, then Weekly|Monthly), both at the class row height.
// App partitions prefs.panelOrder into three DndContext rows by `.row`; the
// grid places cards by `.span`; SWAP_PANELS skips within a `.row`.
export const CARD_LAYOUT: Record<GridPanelId, { row: 'tall' | 'medium' | 'short'; span: number }> = {
  sessions: { row: 'tall', span: 6 },
  trend:    { row: 'tall', span: 3 },
  projects: { row: 'tall', span: 3 },
  daily:          { row: 'medium', span: 6 },
  'cache-report': { row: 'medium', span: 6 },
  weekly:         { row: 'medium', span: 6 },
  monthly:        { row: 'medium', span: 6 },
  forecast: { row: 'short', span: 4 },
  blocks:   { row: 'short', span: 4 },
  alerts:   { row: 'short', span: 4 },
};

// Panels for which a share affordance is rendered (spec §6.1, plan §M1.9).
//
// Keyed by `string` (a DOM-derived `data-panel-kind` membership gate), NOT
// `PanelId`, to also cover `current-week` (the hero, off-grid). The entries
// mirror the Python share ids (`bin/_lib_share_templates.SHARE_CAPABLE_PANELS`,
// the source of truth) that the modal ShareIcons + composer key off
// (SharePanelId, share/types.ts).
//
// #264 S2: 'history' is removed; daily/weekly/monthly ARE grid SharePanelIds
// now, so the keyboardShare history→daily shim is gone (see M4). Alerts is
// excluded by design: it's a notification stream, not a report.
export const SHARE_CAPABLE_PANELS: ReadonlySet<string> = new Set<string>([
  'current-week', 'forecast', 'trend', 'sessions', 'projects',
  'weekly', 'monthly', 'blocks', 'daily',
]);  // intentionally excludes 'alerts'
