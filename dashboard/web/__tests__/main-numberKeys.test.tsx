import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, getState, dispatch, updateSnapshot } from '../src/store/store';
import { openPanelByPosition } from '../src/lib/openPanelByPosition';
import type { Envelope } from '../src/types/envelope';

// A snapshot with empty sessions — enough to clear the #207 B2/B3 no-data
// guard (openPanelByPosition is a no-op until a snapshot lands) while keeping
// the Sessions opener a safe no-op (no session id to resolve).
// #294 S5 §6.11 — digits address the VISIBLE panel order; the Claude cache-report
// panel is visible only when the legacy `cache_report` object exists, so include
// a (non-null) one here to keep all 10 grid panels visible and the default
// full-order digit mapping intact.
const SNAP = {
  header: {},
  sessions: { total: 0, rows: [], sort_key: 'started_desc' },
  cache_report: { is_empty: false },
} as unknown as Envelope;

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(SNAP);
});

// Default grid order (#264 S2 bento): sessions(1), trend(2), projects(3),
// daily(4), cache-report(5), weekly(6), monthly(7), forecast(8), blocks(9),
// alerts(10 → key '0').
describe('openPanelByPosition', () => {
  it('uses the registered openAction (Sessions at position 1 has a special opener that does NOT just OPEN_MODAL kind=session)', () => {
    // Sessions (position 1) uses openMostRecentSessionModal, which resolves a
    // session id from the snapshot before dispatching. The seeded snapshot has
    // EMPTY sessions, so there's no id to resolve and the opener is a safe
    // no-op (openModal stays null). The point is no crash — and that the
    // registered openAction is used, not a bare OPEN_MODAL kind=session.
    openPanelByPosition(1);
    expect(getState().openModal).toBeNull();
  });

  it('opens the panel currently at position 2 (trend)', () => {
    openPanelByPosition(2);
    expect(getState().openModal).toBe('trend');
  });

  it('follows the saved order — after a reorder, position 1 opens the new occupant', () => {
    dispatch({ type: 'REORDER_PANELS', from: 1, to: 0 });   // trend → 0
    openPanelByPosition(1);
    expect(getState().openModal).toBe('trend');
  });

  it('opens the projects panel at position 3 in the default order', () => {
    openPanelByPosition(3);
    expect(getState().openModal).toBe('projects');
  });

  it('opens the panel currently at position 4 (daily in the default order)', () => {
    openPanelByPosition(4);
    expect(getState().openModal).toBe('daily');
  });

  it('opens the panel currently at position 5 (cache-report in the default order)', () => {
    openPanelByPosition(5);
    expect(getState().openModal).toBe('cache-report');
  });

  it('opens the panel currently at position 6 (weekly in the default order)', () => {
    openPanelByPosition(6);
    expect(getState().openModal).toBe('weekly');
  });

  it('opens the panel currently at position 7 (monthly in the default order)', () => {
    openPanelByPosition(7);
    expect(getState().openModal).toBe('monthly');
  });
});
