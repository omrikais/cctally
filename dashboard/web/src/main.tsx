import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { startSSE } from './store/sse';
import { installGlobalKeydown, registerKeymap } from './store/keymap';
import { dispatch, getState } from './store/store';
import { triggerSync } from './store/sync';
import { stepMatch, tryQuit } from './store/actions';
import { openPanelByPosition } from './lib/openPanelByPosition';
import './index.css';

// Boot SSE (module-scoped; StrictMode's double-mount cannot double-boot it).
startSSE();

// Install the global keydown listener and the always-on bindings.
installGlobalKeydown();
registerKeymap([
  { key: '1', scope: 'global', action: () => openPanelByPosition(1) },
  { key: '2', scope: 'global', action: () => openPanelByPosition(2) },
  { key: '3', scope: 'global', action: () => openPanelByPosition(3) },
  { key: '4', scope: 'global', action: () => openPanelByPosition(4) },
  { key: '5', scope: 'global', action: () => openPanelByPosition(5) },
  { key: '6', scope: 'global', action: () => openPanelByPosition(6) },
  { key: '7', scope: 'global', action: () => openPanelByPosition(7) },
  { key: '8', scope: 'global', action: () => openPanelByPosition(8) },
  { key: '9', scope: 'global', action: () => openPanelByPosition(9) },
  { key: 'r', scope: 'global', action: () => triggerSync() },
  { key: 'q', scope: 'global', action: tryQuit },
  { key: 'n', scope: 'global', action: () => stepMatch(1) },
  { key: 'N', scope: 'global', action: () => stepMatch(-1) },
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
