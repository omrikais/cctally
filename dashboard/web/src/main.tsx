import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { startSSE } from './store/sse';
import { installGlobalKeydown, registerKeymap } from './store/keymap';
import { dispatch, getState } from './store/store';
import { triggerSync } from './store/sync';
import { stepMatch, tryQuit } from './store/actions';
import { refreshUpdateState } from './store/update';
import { openPanelByPosition } from './lib/openPanelByPosition';
import { buildShareKeyBinding } from './share/keyboardShare';
import { buildBasketKeyBindings } from './share/keyboardBasket';
import './index.css';

// Boot SSE (module-scoped; StrictMode's double-mount cannot double-boot it).
startSSE();

// Update-subcommand bootstrap (spec §6). One-shot fetch of
// /api/update/status to seed `state.update.{state,suppress}` so the
// header badge can render on first paint when an update is available.
// Errors swallowed — a failing endpoint just leaves the slice null and
// the badge hidden until the next refresh.
//
// Steady-state refresh is SSE-driven: each envelope tick mirrors
// `update-state.json` / `update-suppress.json` (see `ingestUpdate` in
// store/sse.ts), so background dashboard update-checks repaint the
// badge live without polling. This boot call still exists as a
// belt-and-suspenders for the SSE-not-yet-connected window so first
// paint isn't bottlenecked on the first tick.
refreshUpdateState();

// Install the global keydown listener and the always-on bindings.
installGlobalKeydown();
// All digit/letter globals are guarded against `update.modalOpen` so a
// keystroke while the update modal is showing doesn't dispatch a
// parallel panel modal into ModalRoot (UpdateModal mounts in its own
// root). `q`/`r`/`n`/`N` all skip while the update modal is open, but
// Esc still routes to UpdateModal's modal-scope binding via the
// scope-priority sort. See `gotcha: project_global_hotkeys_modal_guard`.
const _updateOpenGuard = () => !getState().update.modalOpen;

registerKeymap([
  { key: '1', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(1) },
  { key: '2', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(2) },
  { key: '3', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(3) },
  { key: '4', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(4) },
  { key: '5', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(5) },
  { key: '6', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(6) },
  { key: '7', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(7) },
  { key: '8', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(8) },
  { key: '9', scope: 'global', when: _updateOpenGuard, action: () => openPanelByPosition(9) },
  { key: 'r', scope: 'global', when: _updateOpenGuard, action: () => triggerSync() },
  { key: 'q', scope: 'global', when: _updateOpenGuard, action: tryQuit },
  { key: 'n', scope: 'global', when: _updateOpenGuard, action: () => stepMatch(1) },
  { key: 'N', scope: 'global', when: _updateOpenGuard, action: () => stepMatch(-1) },
  // Share v2 (spec §12.1). Opens the share modal for the focused panel.
  // Guards (composer/share/panel modals empty, no input mode, focus on a
  // share-capable panel, not mobile) live inside buildShareKeyBinding so
  // tests can drive them through the same module main.tsx wires up.
  buildShareKeyBinding(),
  // Share v2 (spec §12.1). `B` opens the composer modal. Same guard
  // surface as `S` except the composer is global (no panel focus
  // resolution) and case-insensitive (both `b` and `B` fire).
  ...buildBasketKeyBindings(),
  {
    key: 'c',
    scope: 'sessions',
    when: () => !getState().openModal,
    action: () => {
      const cur = getState().prefs.sessionsCollapsed;
      dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: !cur } });
    },
  },
]);

const root = document.getElementById('root');
if (!root) throw new Error('missing #root');
createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
