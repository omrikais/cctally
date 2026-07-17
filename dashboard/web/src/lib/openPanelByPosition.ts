import { getState } from '../store/store';
import { resolveSourceView } from '../store/sourceView';
import { deriveVisiblePanelOrder } from './visiblePanelOrder';
import { PANEL_REGISTRY } from './panelRegistry';
import type { GridPanelId } from './panelIds';

// #294 S5 — the legacy panel-family modals are Claude-shaped (they read the
// legacy top-level envelope), so under a non-Claude selection a digit must not
// open one: it would render Claude data under another source's label (ui-qa
// round-3 P2). Only modals that resolve their data per-source may open there.
// The visible Codex/All panels expose no click-to-modal affordance either, so
// this keeps keyboard and pointer behavior consistent.
const SOURCE_AWARE_MODAL_PANELS: ReadonlySet<GridPanelId> = new Set(['alerts']);

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
  if (s.activeSource !== 'claude' && !SOURCE_AWARE_MODAL_PANELS.has(id)) return;
  const def = PANEL_REGISTRY[id];
  if (def) def.openAction();
}
