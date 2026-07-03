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

  // #264 S1 — the grid is partitioned into three height-class rows (tall /
  // medium / short), each its own dnd context. DOM order is tall→medium→short;
  // within each row the relative order follows prefs.panelOrder.
  function hostsIn(container: HTMLElement, sel: string): (string | undefined)[] {
    return Array.from(container.querySelectorAll(`${sel} [data-panel-host]`))
      .map((h) => (h as HTMLElement).dataset.panelHost);
  }

  it('renders the default order partitioned into the three bento rows', () => {
    const { container } = render(<App />);
    expect(hostsIn(container, '.bento-row.row-tall'))
      .toEqual(['sessions', 'trend', 'projects']);
    // #266 — medium is a 6-card 3×2; short holds only Alerts (full width).
    expect(hostsIn(container, '.bento-row.row-medium'))
      .toEqual(['daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast']);
    expect(hostsIn(container, '.bento-row.row-short'))
      .toEqual(['alerts']);
    // The three slices together cover the full default order exactly once.
    const all = Array.from(document.querySelectorAll('[data-panel-host]'))
      .map((h) => (h as HTMLElement).dataset.panelHost);
    expect([...all].sort()).toEqual([...DEFAULT_PANEL_ORDER].sort());
  });

  it('a within-class REORDER reflects in that row and leaves the other rows intact', () => {
    const { container } = render(<App />);
    act(() => {
      // Move sessions (panelOrder index 0, a tall card) to projects' slot
      // (index 2): within the tall row it now sits AFTER trend and projects.
      dispatch({ type: 'REORDER_PANELS', from: 0, to: 2 });
    });
    expect(hostsIn(container, '.bento-row.row-tall'))
      .toEqual(['trend', 'projects', 'sessions']);
    // The medium and short rows keep their relative order.
    expect(hostsIn(container, '.bento-row.row-medium'))
      .toEqual(['daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast']);
    expect(hostsIn(container, '.bento-row.row-short'))
      .toEqual(['alerts']);
  });
});
