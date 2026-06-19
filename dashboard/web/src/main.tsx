import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { startSSE } from './store/sse';
import { installGlobalKeydown, registerKeymap } from './store/keymap';
import { installUrlRouting } from './store/urlRouting';
import { refreshUpdateState } from './store/update';
import { buildGlobalKeyBindings } from './store/globalBindings';
import '@fontsource/newsreader/400.css';
import '@fontsource/newsreader/500.css';
import '@fontsource/newsreader/600.css';
import '@fontsource/newsreader/400-italic.css';
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
// Install conversation URL deep-linking (#169): boot from the hash, then keep
// the hash and the store in sync. Module-scoped like installGlobalKeydown, so
// StrictMode's double-mount cannot double-install.
installUrlRouting();
// The always-on dashboard key bindings + their guards now live in a
// side-effect-free builder (`store/globalBindings.ts`, #207 D1) so the
// keymap can be populated in tests without booting SSE / createRoot.
registerKeymap(buildGlobalKeyBindings());

const root = document.getElementById('root');
if (!root) throw new Error('missing #root');
createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
