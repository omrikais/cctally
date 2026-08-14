import { dispatch, getScopedSnapshot, getState } from '../store/store';
import { resolveSourceView } from '../store/sourceView';
import { collectSourceSessionRows } from './sourceRows';
import { presentationBlocks } from './dashboardPresentation';
import { deriveVisiblePanelOrder } from './visiblePanelOrder';
import { PANEL_REGISTRY } from './panelRegistry';

/** 1-indexed over the persisted ten-card order, which every source shares.
 *  Out-of-range → no-op. */
export function openPanelByPosition(position: number): void {
  // B2/B3 (#207): during loading/error the live panels aren't mounted
  // (snapshot == null). Don't let a global digit binding pop a panel modal
  // over the skeleton/error screen (it would render env-null "—" data). This
  // gates ONLY the no-data window; a disconnected-but-ready dashboard (last
  // good data shown) keeps working.
  const s = getState();
  if (s.snapshot == null) return;
  const env = getScopedSnapshot(s);
  if (env == null) return;
  // #556 S4 — no panel is source-hidden. deriveVisiblePanelOrder returns the
  // persisted order unchanged for every source, so a digit addresses the same
  // card on every tab. The seam is kept for a source that filters in future.
  const order = deriveVisiblePanelOrder(
    s.prefs.panelOrder,
    resolveSourceView(env, s.activeSource),
  );
  const idx = position - 1;
  if (idx < 0 || idx >= order.length) return;
  const id = order[idx];
  if (s.activeSource !== 'claude' && id === 'sessions') {
    const row = collectSourceSessionRows(resolveSourceView(env, s.activeSource))[0];
    if (row) dispatch({ type: 'OPEN_SOURCE_DETAIL', source: row.source, resource: 'session', key: row.key });
    return;
  }
  if (s.activeSource !== 'claude' && id === 'blocks') {
    const blocks = presentationBlocks(env, s.activeSource);
    const row = blocks.find((item) => item.is_active) ?? blocks[0];
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
