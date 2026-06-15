import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  _resetForTests,
  dispatch,
  getState,
  selectMarkersEnabled,
} from './store';

// cache-failure-markers spec §5 — the dashboard_prefs slice + the
// `markersEnabled` selector. Mirrors the alertsConfig pattern: the SSE ingest
// replaces the slice wholesale each tick (INGEST_DASHBOARD_PREFS), and the
// selector derives a boolean defaulting to TRUE when the field is undefined
// (opt-out, not opt-in — an older server / first tick reads as ON).
describe('dashboardPrefs slice + markersEnabled selector', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });
  afterEach(() => {
    _resetForTests();
  });

  it('defaults markersEnabled to true before any tick', () => {
    expect(selectMarkersEnabled(getState())).toBe(true);
  });

  it('markersEnabled === false when the server reports cache_failure_markers=false', () => {
    dispatch({
      type: 'INGEST_DASHBOARD_PREFS',
      prefs: { cache_failure_markers: false },
    });
    expect(selectMarkersEnabled(getState())).toBe(false);
  });

  it('markersEnabled === true when the server reports cache_failure_markers=true', () => {
    dispatch({
      type: 'INGEST_DASHBOARD_PREFS',
      prefs: { cache_failure_markers: true },
    });
    expect(selectMarkersEnabled(getState())).toBe(true);
  });

  it('markersEnabled defaults to true when the field is absent on the wire', () => {
    // An older Python omits the field entirely; absence is treated as ON.
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: {} });
    expect(selectMarkersEnabled(getState())).toBe(true);
  });

  it('a later tick replaces the slice wholesale (server is the source of truth)', () => {
    dispatch({
      type: 'INGEST_DASHBOARD_PREFS',
      prefs: { cache_failure_markers: false },
    });
    expect(selectMarkersEnabled(getState())).toBe(false);
    dispatch({
      type: 'INGEST_DASHBOARD_PREFS',
      prefs: { cache_failure_markers: true },
    });
    expect(selectMarkersEnabled(getState())).toBe(true);
  });
});
