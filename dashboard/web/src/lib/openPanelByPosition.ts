import { dispatch, getState } from '../store/store';
import { resolveSourceView } from '../store/sourceView';
import { collectSourceSessionRows } from './sourceRows';
import { presentationBlocks } from './dashboardPresentation';
import { deriveVisiblePanelOrder } from './visiblePanelOrder';
import { PANEL_REGISTRY } from './panelRegistry';

/** 1-indexed: position 1 = the FIRST VISIBLE panel for the active source.
 *  Out-of-range (incl. addressing a hidden panel's slot) → no-op. */
export function openPanelByPosition(position: number): void {
  // B2/B3 (#207): during loading/error the live panels aren't mounted
  // (snapshot == null). Don't let a global digit binding pop a panel modal
  // over the skeleton/error screen (it would render env-null "—" data). This
  // gates ONLY the no-data window; a disconnected-but-ready dashboard (last
  // good data shown) keeps working.
  const s = getState();
  if (s.snapshot == null) return;
  // #294 S5 §6.11 — digits address VISIBLE positions only, so a source-hidden
  // panel is unreachable by digit.
  const order = deriveVisiblePanelOrder(
    s.prefs.panelOrder,
    resolveSourceView(s.snapshot, s.activeSource),
  );
  const idx = position - 1;
  if (idx < 0 || idx >= order.length) return;
  const id = order[idx];
  if (s.activeSource !== 'claude' && id === 'sessions') {
    const row = collectSourceSessionRows(resolveSourceView(s.snapshot, s.activeSource))[0];
    if (row) dispatch({ type: 'OPEN_SOURCE_DETAIL', source: row.source, resource: 'session', key: row.key });
    return;
  }
  if (s.activeSource !== 'claude' && id === 'blocks') {
    const row = presentationBlocks(s.snapshot, s.activeSource).find((item) => item.is_active)
      ?? presentationBlocks(s.snapshot, s.activeSource)[0];
    if (row?.source === 'claude') {
      dispatch({ type: 'OPEN_MODAL', kind: 'block', blockStartAt: row.start_at });
    } else if (row) {
      dispatch({ type: 'OPEN_SOURCE_DETAIL', source: row.source, resource: 'block', key: row.key });
    }
    return;
  }
  const def = PANEL_REGISTRY[id];
  if (def) def.openAction();
}
