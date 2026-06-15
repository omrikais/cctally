import { describe, it, expect, beforeEach } from 'vitest';
import { dispatch, selectLiveTailEnabled, _resetForTests } from './store';

describe('selectLiveTailEnabled', () => {
  beforeEach(() => { _resetForTests?.(); });

  it('defaults ON when the field is absent', () => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: {} });
    expect(selectLiveTailEnabled()).toBe(true);
  });

  it('is OFF only when explicitly false', () => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { live_tail: false } });
    expect(selectLiveTailEnabled()).toBe(false);
  });

  it('is ON when true', () => {
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { live_tail: true } });
    expect(selectLiveTailEnabled()).toBe(true);
  });
});
