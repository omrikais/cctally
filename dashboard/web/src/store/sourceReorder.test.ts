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
  it('Codex: swapping uses the same full canonical positions as Claude', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // index 2 = projects; direction +1 stays within the tall row and clamps.
    dispatch({ type: 'SWAP_PANELS', index: 2, direction: 1 });
    const order = getState().prefs.panelOrder;
    expect(order).toEqual(FULL);
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
  it('Codex: reordering sessions→trend uses the same canonical slots', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // Move canonical[0] (sessions) to canonical[1] (trend's slot).
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 });
    const order = getState().prefs.panelOrder;
    expect(order.slice(0, 3)).toEqual(['trend', 'sessions', 'projects']);
    expect([...order].sort()).toEqual([...FULL].sort());
  });

  it('the persisted full order is never rewritten by a source switch alone', () => {
    const before = getState().prefs.panelOrder;
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    expect(getState().prefs.panelOrder).toBe(before);
  });
});
