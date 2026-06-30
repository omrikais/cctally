import { describe, it, expect, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';
import { ForecastPanel } from '../src/panels/ForecastPanel';
import { TrendPanel } from '../src/panels/TrendPanel';
import { SessionsPanel } from '../src/panels/SessionsPanel';
import {
  getState,
  updateSnapshot,
  _resetForTests,
} from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

// Enter / Space on a focused panel must open the matching modal.
// <section> does NOT synthesize click from Enter the way <button> does,
// so we add explicit onKeyDown handlers. Legacy parity: focus.js did
// the same via a document-level delegated keydown.
function firePanelKey(el: HTMLElement, key: 'Enter' | ' ') {
  el.focus();
  act(() => {
    el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  });
}

beforeEach(() => {
  _resetForTests();
  updateSnapshot(fixture as unknown as Envelope);
});

describe('Enter / Space on focused panel opens the matching modal', () => {
  // #248 — the Current Week grid card was removed (it is now the HeroStrip,
  // covered by HeroStrip.test.tsx). The remaining grid panels keep the
  // Enter/Space → open-modal contract.
  it('ForecastPanel → forecast (Space also works)', () => {
    render(<ForecastPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="forecast"]')!;
    firePanelKey(panel, ' ');
    expect(getState().openModal).toBe('forecast');
  });
  it('TrendPanel → trend', () => {
    render(<TrendPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="trend"]')!;
    firePanelKey(panel, 'Enter');
    expect(getState().openModal).toBe('trend');
  });
  it('SessionsPanel → session', () => {
    render(<SessionsPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="sessions"]')!;
    firePanelKey(panel, 'Enter');
    expect(getState().openModal).toBe('session');
  });
});
