import { getState } from '../store/store';
import { PANEL_REGISTRY } from './panelRegistry';

/** 1-indexed: position 1 = panelOrder[0]. Out-of-range → no-op. */
export function openPanelByPosition(position: number): void {
  const order = getState().prefs.panelOrder;
  const idx = position - 1;
  if (idx < 0 || idx >= order.length) return;
  const id = order[idx];
  const def = PANEL_REGISTRY[id];
  if (def) def.openAction();
}
