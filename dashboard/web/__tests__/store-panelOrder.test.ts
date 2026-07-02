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
    // 8 entries — S8 #254 collapsed weekly/monthly/daily into 'history', so a
    // v4-cursor saved order round-trips verbatim through the reconciler
    // (missing ids would otherwise be appended).
    const custom: PanelId[] = ['history', 'blocks', 'projects', 'sessions', 'trend', 'forecast', 'alerts', 'cache-report'];
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      sortDefault: 'started desc',
      sessionsPerPage: 100,
      sessionsCollapsed: true,
      blocksCollapsed: true,
      dailyCollapsed: true,
      panelOrder: custom,
      onboardingToastSeen: true,
      // Bump cursor to CURRENT so the saved order round-trips verbatim.
      panelOrderSchemaVersion: 4,
    }));
    const initial = loadInitialForTests();
    expect(initial.prefs.panelOrder).toEqual(custom);
    expect(initial.prefs.onboardingToastSeen).toBe(true);
  });

  it('upgrades a v1 saved order: splices projects, drops current-week, collapses period tiles to history, appends cache-report', () => {
    // v1 = no panelOrderSchemaVersion key, no 'projects', a 'current-week' the
    // grid no longer carries (#248), and the weekly/monthly/daily tiles S8
    // (#254) collapses into one 'history' card. The cumulative migration runs
    // all steps; the reconciler appends cache-report (never in the v1 order).
    const v1Custom = [
      'daily', 'blocks', 'monthly', 'weekly',
      'sessions', 'trend', 'forecast', 'current-week', 'alerts',
    ] as unknown as PanelId[];
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({
      panelOrder: v1Custom,
      onboardingToastSeen: true,
    }));
    const initial = loadInitialForTests();
    const order = initial.prefs.panelOrder as unknown as string[];
    // Legacy ids gone; every modern grid id present exactly once.
    for (const gone of ['current-week', 'weekly', 'monthly', 'daily']) {
      expect(order).not.toContain(gone);
    }
    for (const present of ['projects', 'history', 'cache-report']) {
      expect(order).toContain(present);
    }
    expect(new Set(order)).toEqual(new Set(DEFAULT_PANEL_ORDER as unknown as string[]));
    expect(order).toHaveLength(DEFAULT_PANEL_ORDER.length);
    expect(initial.prefs.panelOrderSchemaVersion).toBe(4);
    // Migration is persisted immediately so a refresh doesn't re-fire it.
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(JSON.parse(raw!).panelOrderSchemaVersion).toBe(4);
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
  // Default bento order (#264 S1): sessions, trend, projects, history,
  // cache-report, forecast, blocks, alerts.
  it('moves panel from index 0 to index 3', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    const order = getState().prefs.panelOrder;
    // 'sessions' moves to index 3; 'trend' shifts up into index 0.
    expect(order[3]).toBe('sessions');
    expect(order[0]).toBe('trend');
  });

  it('persists to localStorage', () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!) as { panelOrder: string[] };
    expect(parsed.panelOrder[0]).toBe('trend');
    expect(parsed.panelOrder[1]).toBe('sessions');
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

describe('SWAP_PANELS action (height-class-aware — #264 S1)', () => {
  // Default bento order + rows:
  //   0 sessions(tall)  1 trend(tall)  2 projects(tall)
  //   3 history(medium) 4 cache-report(medium)
  //   5 forecast(short) 6 blocks(short) 7 alerts(short)
  // Shift+Arrow keeps dispatching SWAP_PANELS{index, direction}; the reducer
  // moves the card to the previous/next id sharing its CARD_LAYOUT row
  // (skipping other classes), so a keyboard reorder can never cross classes.
  const TALL = ['sessions', 'trend', 'projects'];
  const MEDIUM = ['history', 'cache-report'];
  const SHORT = ['forecast', 'blocks', 'alerts'];

  it('a tall card swaps with the next tall card (sessions → trend)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 0, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order[0]).toBe('trend');
    expect(order[1]).toBe('sessions');
    // The medium + short rows keep their relative order.
    expect(order.filter((id) => MEDIUM.includes(id))).toEqual(MEDIUM);
    expect(order.filter((id) => SHORT.includes(id))).toEqual(SHORT);
  });

  it('a short card swaps with the previous short card (blocks → forecast)', () => {
    dispatch({ type: 'SWAP_PANELS', index: 6, direction: -1 });
    const order = getState().prefs.panelOrder;
    expect(order[5]).toBe('blocks');
    expect(order[6]).toBe('forecast');
    // The tall row keeps its relative order.
    expect(order.filter((id) => TALL.includes(id))).toEqual(TALL);
  });

  it('skips an interleaved other-class card to reach the next same-class id', () => {
    // Interleave a medium card between two tall cards: move history (index 3,
    // medium) up to index 1 → [sessions, history, trend, projects, …].
    dispatch({ type: 'REORDER_PANELS', from: 3, to: 1 });
    expect(getState().prefs.panelOrder.slice(0, 4))
      .toEqual(['sessions', 'history', 'trend', 'projects']);
    // sessions (index 0, tall) + Shift-down: the next TALL is trend at index 2,
    // skipping the intervening medium 'history' at index 1.
    dispatch({ type: 'SWAP_PANELS', index: 0, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order[0]).toBe('trend');
    expect(order[2]).toBe('sessions');
    // The medium card stayed put (only sessions↔trend swapped).
    expect(order[1]).toBe('history');
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

  it('is a no-op at a class boundary with no further same-class card', () => {
    // projects is the last tall card (index 2); every id after it is
    // medium/short, so a class-aware +1 finds no target and is a no-op.
    const before = [...getState().prefs.panelOrder];
    dispatch({ type: 'SWAP_PANELS', index: 2, direction: 1 });
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
    const preview: GridPanelId[] = ['history', 'blocks', 'projects', 'sessions', 'trend', 'forecast', 'alerts', 'cache-report'];
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
    const preview: GridPanelId[] = ['forecast', 'cache-report', 'trend', 'sessions', 'projects', 'blocks', 'history', 'alerts'];
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
    const preview: GridPanelId[] = ['history', 'blocks', 'projects', 'sessions', 'trend', 'forecast', 'alerts', 'cache-report'];
    dispatch({ type: 'SET_DRAG_PREVIEW', order: preview });
    dispatch({ type: 'CLEAR_DRAG_PREVIEW' });
    expect(getState().dragPreviewOrder).toBeNull();
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});
