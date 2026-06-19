import { describe, it, expect, beforeEach } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';

describe('chromeOverlayOpen counter', () => {
  beforeEach(() => { localStorage.clear(); _resetForTests(); });

  it('defaults to 0', () => {
    expect(getState().chromeOverlayOpen).toBe(0);
  });
  it('increments and decrements', () => {
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    dispatch({ type: 'INCREMENT_CHROME_OVERLAY' });
    expect(getState().chromeOverlayOpen).toBe(2);
    dispatch({ type: 'DECREMENT_CHROME_OVERLAY' });
    expect(getState().chromeOverlayOpen).toBe(1);
  });
  it('never goes below 0', () => {
    dispatch({ type: 'DECREMENT_CHROME_OVERLAY' });
    expect(getState().chromeOverlayOpen).toBe(0);
  });
});
