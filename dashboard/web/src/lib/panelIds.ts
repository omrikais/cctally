// Canonical panel id type + default ordering.
//
// This module deliberately has zero imports: it is consumed by both
// `panelRegistry.ts` (which pulls in all panel components and therefore
// also pulls in the store) AND by `store/store.ts` itself. Putting the
// constants here breaks the otherwise-circular `panelRegistry → panels →
// store → panelRegistry` import cycle that would leave
// `DEFAULT_PANEL_ORDER` in the temporal dead zone during store module
// init.

export type PanelId =
  | 'current-week'
  | 'forecast'
  | 'trend'
  | 'sessions'
  | 'weekly'
  | 'monthly'
  | 'blocks'
  | 'daily'
  | 'alerts';

export const DEFAULT_PANEL_ORDER: PanelId[] = [
  'current-week', 'forecast', 'trend', 'sessions',
  'weekly', 'monthly', 'blocks', 'daily', 'alerts',
];
