import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, getState, dispatch } from '../src/store/store';
import { openPanelByPosition } from '../src/lib/openPanelByPosition';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('openPanelByPosition', () => {
  it('opens the panel currently at position 1 (1-indexed)', () => {
    openPanelByPosition(1);
    expect(getState().openModal).toBe('current-week');
  });

  it('follows the saved order — after a reorder, position 1 opens the new occupant', () => {
    dispatch({ type: 'REORDER_PANELS', from: 1, to: 0 });   // forecast → 0
    openPanelByPosition(1);
    expect(getState().openModal).toBe('forecast');
  });

  it('uses the registered openAction (Sessions has a special opener that does NOT just OPEN_MODAL kind=session)', () => {
    // Sessions panel registry uses openMostRecentSessionModal which still
    // dispatches OPEN_MODAL kind=session, but only after resolving an id
    // from the current snapshot. Without a snapshot, it should be a no-op
    // and openModal stays null.
    openPanelByPosition(4);
    // No snapshot loaded in this test → openMostRecentSessionModal is a
    // safe no-op; openModal stays null. The point is no crash.
    expect(getState().openModal).toBeNull();
  });

  it('opens the panel currently at position 9 (alerts in default order)', () => {
    openPanelByPosition(9);
    expect(getState().openModal).toBe('alerts');
  });
});
