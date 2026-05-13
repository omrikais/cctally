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
// All digit/letter globals are guarded against modals layered in their
// own root (`update.modalOpen`, `doctorModalOpen`) so a keystroke
// behind one of those modals doesn't dispatch a parallel panel modal
// into ModalRoot or fire `q`/`r` invisibly underneath. The Update and
// Doctor modals each manage their own `Escape` via modal-scope bindings,
// so closing them is unaffected by this guard. See
// `gotcha: project_global_hotkeys_modal_guard`.
const _globalKeyGuard = (): boolean => {
  const s = getState();
  return !s.update.modalOpen && !s.doctorModalOpen;
};

// Doctor key (`d`) uses a composite guard per spec §6.4 (Codex M5):
// - !openModal      — a panel modal isn't currently up.
// - !update.modalOpen — the update modal (own root) isn't up.
// - inputMode === null — search/filter input modes own the keyboard.
// The bare _globalKeyGuard pattern would let `d` fire through a
// panel modal or during text-input mode. The same triple guard the
// share/composer/basket keys use (see share/keyboardShare.ts).
const _doctorOpenGuard = (): boolean => {
  const s = getState();
  if (s.openModal !== null) return false;
  if (s.update.modalOpen) return false;
  if (s.inputMode !== null) return false;
  return true;
};

registerKeymap([
  { key: '1', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(1) },
  { key: '2', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(2) },
  { key: '3', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(3) },
  { key: '4', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(4) },
  { key: '5', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(5) },
  { key: '6', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(6) },
  { key: '7', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(7) },
  { key: '8', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(8) },
  { key: '9', scope: 'global', when: _globalKeyGuard, action: () => openPanelByPosition(9) },
  { key: 'r', scope: 'global', when: _globalKeyGuard, action: () => triggerSync() },
  { key: 'q', scope: 'global', when: _globalKeyGuard, action: tryQuit },
  { key: 'n', scope: 'global', when: _globalKeyGuard, action: () => stepMatch(1) },
  { key: 'N', scope: 'global', when: _globalKeyGuard, action: () => stepMatch(-1) },
  // Doctor modal — composite guard (see _doctorOpenGuard above).
  { key: 'd', scope: 'global', when: _doctorOpenGuard, action: () => dispatch({ type: 'OPEN_DOCTOR_MODAL' }) },
  // Share v2 (spec §12.1). Opens the share modal for the focused panel.
  // Guards (composer/share/panel modals empty, no input mode, focus on a
  // share-capable panel, not mobile) live inside buildShareKeyBinding so
  // tests can drive them through the same module main.tsx wires up.
  buildShareKeyBinding(),
  // Share v2 (spec §12.1). `B` opens the composer modal. Same guard
  // surface as `S` except the composer is global (no panel focus
  // resolution). Uppercase-only (mirrors the `S`-vs-`s` precedent;
  // lowercase `b` stays free for future per-panel use).
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
