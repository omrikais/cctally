import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { startSSE } from './store/sse';
import { installGlobalKeydown, registerKeymap } from './store/keymap';
import { installUrlRouting } from './store/urlRouting';
import { refreshUpdateState } from './store/update';
import { buildGlobalKeyBindings } from './store/globalBindings';
import {
  bootstrapDashboardAuth,
  renderDashboardAuthFailure,
} from './lib/dashboardAuth';
import '@fontsource/newsreader/400.css';
import '@fontsource/newsreader/500.css';
import '@fontsource/newsreader/600.css';
import '@fontsource/newsreader/400-italic.css';
import './index.css';

const root = document.getElementById('root');
if (!root) throw new Error('missing #root');

async function boot(target: HTMLElement): Promise<void> {
  try {
    await bootstrapDashboardAuth();
  } catch {
    renderDashboardAuthFailure(target);
    return;
  }

  // No API or EventSource consumer starts before the fragment-to-cookie
  // exchange above succeeds (or a cookie reload proves no exchange is needed).
  startSSE();
  refreshUpdateState();
  installGlobalKeydown();
  installUrlRouting();
  registerKeymap(buildGlobalKeyBindings());

  createRoot(target).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

void boot(root);
