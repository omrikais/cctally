import { describe, it, expect, beforeEach } from 'vitest';
import { render, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

// #293 S4 (A11Y-1 / ACTION-1): the bento card regions are DESCRIBE-only now —
// no tab stop, no region Enter/Space activation. The explicit Expand button is
// the sole keyboard path into each card's modal; a keydown that bubbles from the
// (now non-focusable) region body opens nothing. This file previously asserted
// the OPPOSITE (Enter/Space on the section opened the modal) — inverted for S4.
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

describe('#293 S4 — bento regions describe; Expand is the keyboard path', () => {
  it('ForecastPanel: region Enter/Space opens nothing; Expand opens forecast', async () => {
    const user = userEvent.setup();
    const { container } = render(<ForecastPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="forecast"]')!;
    firePanelKey(panel, ' ');
    firePanelKey(panel, 'Enter');
    expect(getState().openModal).toBeNull();
    (container.querySelector('.panel-expand') as HTMLButtonElement).focus();
    await user.keyboard('{Enter}');
    expect(getState().openModal).toBe('forecast');
  });

  it('TrendPanel: region Enter opens nothing; Expand opens trend', async () => {
    const user = userEvent.setup();
    const { container } = render(<TrendPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="trend"]')!;
    firePanelKey(panel, 'Enter');
    expect(getState().openModal).toBeNull();
    (container.querySelector('.panel-expand') as HTMLButtonElement).focus();
    await user.keyboard('{Enter}');
    expect(getState().openModal).toBe('trend');
  });

  it('SessionsPanel: region Enter opens nothing; Expand opens the session modal', async () => {
    const user = userEvent.setup();
    const { container } = render(<SessionsPanel />);
    const panel = document.querySelector<HTMLElement>('[data-panel-kind="sessions"]')!;
    firePanelKey(panel, 'Enter');
    expect(getState().openModal).toBeNull();
    (container.querySelector('.panel-expand') as HTMLButtonElement).focus();
    await user.keyboard('{Enter}');
    expect(getState().openModal).toBe('session');
  });
});
