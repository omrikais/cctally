import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, getState, dispatch, updateSnapshot } from '../src/store/store';
import { openPanelByPosition } from '../src/lib/openPanelByPosition';
import type { Envelope } from '../src/types/envelope';

// A snapshot with empty sessions — enough to clear the #207 B2/B3 no-data
// guard (openPanelByPosition is a no-op until a snapshot lands) while keeping
// the Sessions opener a safe no-op (no session id to resolve).
const SNAP = {
  header: {},
  sessions: { total: 0, rows: [], sort_key: 'started_desc' },
} as unknown as Envelope;

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(SNAP);
});

// Default grid order (#248 — current-week left the grid): forecast(1),
// trend(2), sessions(3), projects(4), weekly(5), monthly(6), blocks(7),
// daily(8), alerts(9), cache-report(10).
describe('openPanelByPosition', () => {
  it('opens the panel currently at position 1 (1-indexed)', () => {
    openPanelByPosition(1);
    expect(getState().openModal).toBe('forecast');
  });

  it('follows the saved order — after a reorder, position 1 opens the new occupant', () => {
    dispatch({ type: 'REORDER_PANELS', from: 1, to: 0 });   // trend → 0
    openPanelByPosition(1);
    expect(getState().openModal).toBe('trend');
  });

  it('uses the registered openAction (Sessions has a special opener that does NOT just OPEN_MODAL kind=session)', () => {
    // Sessions panel registry uses openMostRecentSessionModal which still
    // dispatches OPEN_MODAL kind=session, but only after resolving an id
    // from the current snapshot. The seeded snapshot has EMPTY sessions, so
    // there's no id to resolve and the opener is a safe no-op (openModal
    // stays null). The point is no crash. Sessions is position 3 now.
    openPanelByPosition(3);
    expect(getState().openModal).toBeNull();
  });

  it('opens the panel currently at position 8 (daily in the default order)', () => {
    openPanelByPosition(8);
    expect(getState().openModal).toBe('daily');
  });

  it('opens the panel currently at position 10 — "0" key (cache-report in default order)', () => {
    openPanelByPosition(10);
    expect(getState().openModal).toBe('cache-report');
  });

  it('opens the projects panel at position 4 in the default order', () => {
    openPanelByPosition(4);
    expect(getState().openModal).toBe('projects');
  });
});
