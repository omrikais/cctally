import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, loadInitialForTests, getState } from '../src/store/store';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelRegistry';
import type { PanelId } from '../src/lib/panelRegistry';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('Prefs.panelOrder', () => {
  it('defaults to DEFAULT_PANEL_ORDER on first load', () => {
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
  });

  it('defaults onboardingToastSeen to false', () => {
    expect(getState().prefs.onboardingToastSeen).toBe(false);
  });

  it('reads a previously-persisted custom order', () => {
    const custom = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'current-week', 'alerts'];
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      sortDefault: 'started desc',
      sessionsPerPage: 100,
      sessionsCollapsed: true,
      blocksCollapsed: true,
      dailyCollapsed: true,
      panelOrder: custom,
      onboardingToastSeen: true,
    }));
    const initial = loadInitialForTests();
    expect(initial.prefs.panelOrder).toEqual(custom);
    expect(initial.prefs.onboardingToastSeen).toBe(true);
  });

  it('reconciles a stale saved order (drops unknown, appends missing)', () => {
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      panelOrder: ['forecast', 'unknown-id', 'trend'],
    }));
    const initial = loadInitialForTests();
    expect(initial.prefs.panelOrder).toHaveLength(DEFAULT_PANEL_ORDER.length);
    expect(initial.prefs.panelOrder.slice(0, 2)).toEqual(['forecast', 'trend']);
    expect([...initial.prefs.panelOrder].sort()).toEqual([...DEFAULT_PANEL_ORDER].sort());
  });
});

import { dispatch } from '../src/store/store';

describe('REORDER_PANELS action', () => {
  it('moves panel from index 0 to index 3', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    const order = getState().prefs.panelOrder;
    expect(order[3]).toBe('current-week');
    expect(order[0]).toBe('forecast');
  });

  it('persists to localStorage', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!) as { panelOrder: string[] };
    expect(parsed.panelOrder[0]).toBe('forecast');
    expect(parsed.panelOrder[1]).toBe('current-week');
  });

  it('is a no-op when from === to', () => {
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'REORDER_PANELS', from: 2, to: 2 });
    expect(getState().prefs.panelOrder).toEqual(before);
  });

  it('is a no-op for out-of-bounds indices', () => {
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'REORDER_PANELS', from: -1, to: 3 });
    expect(getState().prefs.panelOrder).toEqual(before);
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 99 });
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});

describe('SWAP_PANELS action', () => {
  it('swaps with next neighbor (direction=+1)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 0, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order[0]).toBe('forecast');
    expect(order[1]).toBe('current-week');
  });

  it('swaps with previous neighbor (direction=-1)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 1, direction: -1 });
    const order = getState().prefs.panelOrder;
    expect(order[0]).toBe('forecast');
    expect(order[1]).toBe('current-week');
  });

  it('is a no-op at the start (index=0, direction=-1)', () => {
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'SWAP_PANELS', index: 0, direction: -1 });
    expect(getState().prefs.panelOrder).toEqual(before);
  });

  it('is a no-op at the end (index=last, direction=+1)', () => {
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'SWAP_PANELS', index: before.length - 1, direction: 1 });
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});

describe('RESET_PANEL_ORDER action', () => {
  it('resets only panelOrder; preserves other prefs and onboardingToastSeen', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsPerPage: 250 } });
    dispatch({ type: 'RESET_PANEL_ORDER' });
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
    expect(getState().prefs.onboardingToastSeen).toBe(true);
    expect(getState().prefs.sessionsPerPage).toBe(250);
  });
});

describe('MARK_ONBOARDING_TOAST_SEEN action', () => {
  it('flips the flag and persists', () => {
    expect(getState().prefs.onboardingToastSeen).toBe(false);
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    expect(getState().prefs.onboardingToastSeen).toBe(true);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(JSON.parse(raw!).onboardingToastSeen).toBe(true);
  });

  it('is idempotent', () => {
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    expect(getState().prefs.onboardingToastSeen).toBe(true);
  });
});

describe('RESET_PREFS preserves onboardingToastSeen, resets panelOrder', () => {
  it('panelOrder goes back to default', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
  });

  it('onboardingToastSeen is preserved', () => {
    dispatch({ type: 'MARK_ONBOARDING_TOAST_SEEN' });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.onboardingToastSeen).toBe(true);
  });
});

describe('drag preview lifecycle', () => {
  it('SET_DRAG_PREVIEW stores a preview order without touching prefs.panelOrder or localStorage', () => {
    const before = [...getState().prefs.panelOrder];
    const preview: PanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'current-week'];
    dispatch({ type: 'SET_DRAG_PREVIEW', order: preview });
    expect(getState().dragPreviewOrder).toEqual(preview);
    expect(getState().prefs.panelOrder).toEqual(before);
    // localStorage should still hold the prior prefs (default), no preview-derived overwrite:
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    if (raw) {
      const parsed = JSON.parse(raw) as { panelOrder: string[] };
      expect(parsed.panelOrder).toEqual(before);
    }
  });

  it('COMMIT_DRAG_PREVIEW promotes preview to prefs.panelOrder and persists', () => {
    const preview: PanelId[] = ['forecast', 'current-week', 'trend', 'sessions', 'weekly', 'monthly', 'blocks', 'daily'];
    dispatch({ type: 'SET_DRAG_PREVIEW', order: preview });
    dispatch({ type: 'COMMIT_DRAG_PREVIEW' });
    expect(getState().dragPreviewOrder).toBeNull();
    expect(getState().prefs.panelOrder).toEqual(preview);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).panelOrder).toEqual(preview);
  });

  it('COMMIT_DRAG_PREVIEW with no preview is a no-op', () => {
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'COMMIT_DRAG_PREVIEW' });
    expect(getState().prefs.panelOrder).toEqual(before);
    expect(getState().dragPreviewOrder).toBeNull();
  });

  it('CLEAR_DRAG_PREVIEW discards the preview without changing prefs', () => {
    const before = [...getState().prefs.panelOrder];
    const preview: PanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'current-week'];
    dispatch({ type: 'SET_DRAG_PREVIEW', order: preview });
    dispatch({ type: 'CLEAR_DRAG_PREVIEW' });
    expect(getState().dragPreviewOrder).toBeNull();
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});
