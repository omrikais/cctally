import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, loadInitialForTests, getState } from '../src/store/store';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelRegistry';
import type { PanelId, GridPanelId } from '../src/lib/panelRegistry';

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

  it('reads a previously-persisted custom order (post-migration schema)', () => {
    // 10 entries — #248 removed 'current-week' from the grid, so a v3-cursor
    // saved order has no current-week and round-trips verbatim through the
    // reconciler (missing ids would otherwise be appended).
    const custom: PanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'projects', 'sessions', 'trend', 'forecast', 'alerts', 'cache-report'];
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      sortDefault: 'started desc',
      sessionsPerPage: 100,
      sessionsCollapsed: true,
      blocksCollapsed: true,
      dailyCollapsed: true,
      panelOrder: custom,
      onboardingToastSeen: true,
      // Bump cursor to CURRENT so the saved order round-trips verbatim.
      panelOrderSchemaVersion: 3,
    }));
    const initial = loadInitialForTests();
    expect(initial.prefs.panelOrder).toEqual(custom);
    expect(initial.prefs.onboardingToastSeen).toBe(true);
  });

  it('upgrades a v1 saved order: splices projects, drops current-week, appends cache-report', () => {
    // v1 = no panelOrderSchemaVersion key, no 'projects' in panelOrder, and a
    // 'current-week' the grid no longer carries (#248). The cumulative
    // migration splices projects at index 4 then drops current-week; the
    // reconciler appends cache-report (the v1 user never had it).
    const v1Custom: PanelId[] = [
      'daily', 'blocks', 'monthly', 'weekly',
      'sessions', 'trend', 'forecast', 'current-week', 'alerts',
    ];
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      panelOrder: v1Custom,
      onboardingToastSeen: true,
    }));
    const initial = loadInitialForTests();
    expect(initial.prefs.panelOrder).toEqual([
      'daily', 'blocks', 'monthly', 'weekly',
      'projects',  // spliced at canonical index 4
      'sessions', 'trend', 'forecast', 'alerts',  // current-week dropped (#248)
      'cache-report',  // appended by reconciler since not in v1 saved order
    ]);
    expect(initial.prefs.panelOrder).not.toContain('current-week');
    expect(initial.prefs.panelOrderSchemaVersion).toBe(3);
    // Migration is persisted immediately so a refresh doesn't re-fire it.
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(JSON.parse(raw!).panelOrderSchemaVersion).toBe(3);
    expect(JSON.parse(raw!).panelOrder).not.toContain('current-week');
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
  // Default grid order (#248): forecast, trend, sessions, projects, weekly, …
  it('moves panel from index 0 to index 3', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    const order = getState().prefs.panelOrder;
    // 'forecast' moves to index 3; 'trend' shifts up into index 0.
    expect(order[3]).toBe('forecast');
    expect(order[0]).toBe('trend');
  });

  it('persists to localStorage', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!) as { panelOrder: string[] };
    expect(parsed.panelOrder[0]).toBe('trend');
    expect(parsed.panelOrder[1]).toBe('forecast');
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

describe('SWAP_PANELS action (tier-aware — #248)', () => {
  // Default grid order + tiers:
  //   0 forecast(tile) 1 trend(wide) 2 sessions(wide) 3 projects(wide)
  //   4 weekly(tile)   5 monthly(tile) 6 blocks(tile)  7 daily(wide)
  //   8 alerts(tile)   9 cache-report(wide)
  // Shift+Arrow keeps dispatching SWAP_PANELS{index, direction}; the reducer
  // moves the card to the previous/next id sharing its CARD_TIER (skipping the
  // other tier), so a keyboard reorder can never cross tiers.
  const WIDES = ['trend', 'sessions', 'projects', 'daily', 'cache-report'];

  it('a tile swaps with the next TILE, skipping wides (forecast → weekly)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 0, direction: 1 });
    const order = getState().prefs.panelOrder;
    // forecast (index 0, tile) swaps with weekly (index 4, the next tile) —
    // the intervening wides (trend/sessions/projects) are skipped.
    expect(order[0]).toBe('weekly');
    expect(order[4]).toBe('forecast');
    // The wide cards keep their relative order.
    expect(order.filter((id) => WIDES.includes(id))).toEqual(WIDES);
  });

  it('a tile swaps with the previous TILE (weekly → forecast)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 4, direction: -1 });
    const order = getState().prefs.panelOrder;
    expect(order[0]).toBe('weekly');
    expect(order[4]).toBe('forecast');
    expect(order.filter((id) => WIDES.includes(id))).toEqual(WIDES);
  });

  it('a wide swaps with the next WIDE, skipping tiles (trend → sessions)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 1, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order[1]).toBe('sessions');
    expect(order[2]).toBe('trend');
    // The tiles keep their relative order.
    const TILES = ['forecast', 'weekly', 'monthly', 'blocks', 'alerts'];
    expect(order.filter((id) => TILES.includes(id))).toEqual(TILES);
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

  it('is a no-op for the last tile moving down (no further tile)', () => {
    // alerts is the last tile (index 8); the only ids after it are wides, so
    // a tier-aware +1 finds no target and is a no-op.
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'SWAP_PANELS', index: 8, direction: 1 });
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
    const preview: GridPanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'cache-report'];
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
    const preview: GridPanelId[] = ['forecast', 'cache-report', 'trend', 'sessions', 'weekly', 'monthly', 'blocks', 'daily'];
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
    const preview: GridPanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'cache-report'];
    dispatch({ type: 'SET_DRAG_PREVIEW', order: preview });
    dispatch({ type: 'CLEAR_DRAG_PREVIEW' });
    expect(getState().dragPreviewOrder).toBeNull();
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});
