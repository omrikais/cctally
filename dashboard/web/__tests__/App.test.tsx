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

  // #248 — the grid is partitioned into a TILE strip + a WIDE strip (two dnd
  // contexts). DOM order is tiles-then-wides; within each strip the relative
  // order follows prefs.panelOrder.
  function hostsIn(container: HTMLElement, sel: string): (string | undefined)[] {
    return Array.from(container.querySelectorAll(`${sel} [data-panel-host]`))
      .map((h) => (h as HTMLElement).dataset.panelHost);
  }

  it('renders the default order partitioned into tile + wide strips', () => {
    const { container } = render(<App />);
    // S8 #254 — weekly/monthly left the grid; the daily heatmap became the
    // wide "history" card. Tiles: forecast, blocks, alerts.
    expect(hostsIn(container, '.tile-strip'))
      .toEqual(['forecast', 'blocks', 'alerts']);
    expect(hostsIn(container, '.wide-strip'))
      .toEqual(['trend', 'sessions', 'projects', 'history', 'cache-report']);
    // The two slices together cover the full default order exactly once.
    const all = Array.from(document.querySelectorAll('[data-panel-host]'))
      .map((h) => (h as HTMLElement).dataset.panelHost);
    expect([...all].sort()).toEqual([...DEFAULT_PANEL_ORDER].sort());
  });

  it('a within-tier REORDER reflects in that strip and leaves the other tier intact', () => {
    const { container } = render(<App />);
    act(() => {
      // Move forecast (panelOrder index 0, a tile) to blocks' slot (index 4):
      // within the tile strip it now sits AFTER blocks.
      dispatch({ type: 'REORDER_PANELS', from: 0, to: 4 });
    });
    expect(hostsIn(container, '.tile-strip'))
      .toEqual(['blocks', 'forecast', 'alerts']);
    // The wide strip's relative order is preserved.
    expect(hostsIn(container, '.wide-strip'))
      .toEqual(['trend', 'sessions', 'projects', 'history', 'cache-report']);
  });
});
