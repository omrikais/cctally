import { getState } from '../store/store';
import { PANEL_REGISTRY } from './panelRegistry';

/** 1-indexed: position 1 = panelOrder[0]. Out-of-range → no-op. */
export function openPanelByPosition(position: number): void {
  // B2/B3 (#207): during loading/error the live panels aren't mounted
  // (snapshot == null). Don't let a global digit binding pop a panel modal
  // over the skeleton/error screen (it would render env-null "—" data). This
  // gates ONLY the no-data window; a disconnected-but-ready dashboard (last
  // good data shown) keeps working.
  if (getState().snapshot == null) return;
  const order = getState().prefs.panelOrder;
  const idx = position - 1;
  if (idx < 0 || idx >= order.length) return;
  const id = order[idx];
  const def = PANEL_REGISTRY[id];
  if (def) def.openAction();
}
