import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, act } from '@testing-library/react';
import { App } from '../src/App';
import { updateSnapshot, _resetForTests, dispatch } from '../src/store/store';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelRegistry';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<App />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(fixture) }),
    );
    updateSnapshot(fixture as unknown as Envelope);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders all panels in default order', () => {
    render(<App />);
    const hosts = Array.from(document.querySelectorAll('[data-panel-host]')) as HTMLElement[];
    expect(hosts.map((h) => h.dataset.panelHost)).toEqual(DEFAULT_PANEL_ORDER);
  });

  it('re-renders panels in the new order after REORDER_PANELS', () => {
    render(<App />);
    act(() => {
      dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    });
    const hosts = Array.from(document.querySelectorAll('[data-panel-host]')) as HTMLElement[];
    const order = hosts.map((h) => h.dataset.panelHost);
    // Full expected order: 'current-week' moves from index 0 to index 3 via splice;
    // assert the entire array to catch off-by-one or direction regressions.
    expect(order).toEqual([
      'forecast', 'trend', 'sessions', 'current-week',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
  });
});
