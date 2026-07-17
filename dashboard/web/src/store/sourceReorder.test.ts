import { beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, getState, updateSnapshot } from './store';
import { DEFAULT_PANEL_ORDER } from '../lib/panelIds';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';

const FULL = [...DEFAULT_PANEL_ORDER];

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(fixture as unknown as Envelope);
});

// §6.11 — SWAP/REORDER index the VISIBLE list and write back into the full
// order, preserving hidden panels' positions. Under Codex: trend / cache-report
// / forecast are hidden (visible: sessions, projects, daily, weekly, monthly,
// blocks, alerts).
describe('SWAP_PANELS — visible-list indexing with full-order write-back (§6.11)', () => {
  it('Codex: swapping the 3rd visible (daily) forward lands on weekly and keeps hidden panels put', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // visible index 2 = daily (a medium-row card); direction +1 → next medium
    // visible = weekly (visible index 3).
    dispatch({ type: 'SWAP_PANELS', index: 2, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order).toEqual([
      'sessions', 'trend', 'projects', 'weekly', 'cache-report',
      'daily', 'monthly', 'blocks', 'forecast', 'alerts',
    ]);
    // Hidden panels held their absolute indices.
    expect(order[FULL.indexOf('trend')]).toBe('trend');
    expect(order[FULL.indexOf('cache-report')]).toBe('cache-report');
    expect(order[FULL.indexOf('forecast')]).toBe('forecast');
    expect([...order].sort()).toEqual([...FULL].sort());
  });

  it('Claude (all visible): SWAP is byte-identical to the legacy full-order swap', () => {
    // blocks is at full index 7 (medium); direction +1 → forecast at index 8.
    dispatch({ type: 'SWAP_PANELS', index: 7, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order[7]).toBe('forecast');
    expect(order[8]).toBe('blocks');
  });
});

describe('REORDER_PANELS — visible-list indexing with full-order write-back (§6.11)', () => {
  it('Codex: reordering visible sessions→projects slot preserves hidden trend', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // Move visible[0] (sessions) to visible[1] (projects' slot).
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    const order = getState().prefs.panelOrder;
    // trend (hidden) stays at index 1; the two visibles swap around it.
    expect(order[FULL.indexOf('trend')]).toBe('trend');
    expect(order.indexOf('projects')).toBeLessThan(order.indexOf('sessions'));
    expect([...order].sort()).toEqual([...FULL].sort());
  });

  it('the persisted full order is never rewritten by a source switch alone', () => {
    const before = getState().prefs.panelOrder;
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    expect(getState().prefs.panelOrder).toBe(before);
  });
});
