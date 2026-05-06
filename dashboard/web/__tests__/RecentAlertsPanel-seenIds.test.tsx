import { describe, it, expect, beforeEach } from 'vitest';
import { dispatch, getState, _resetForTests } from '../src/store/store';

const DEFAULT_ALERTS_SETTINGS = {
  enabled: false,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
};

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('RecentAlertsPanel — seenAlertIds cold-start rule', () => {
  it('first SSE tick after mount populates seenAlertIds without surfacing toast', () => {
    const alerts = [
      {
        id: 'weekly:2026-04-27:90',
        axis: 'weekly' as const,
        threshold: 90,
        crossed_at: '2026-04-29T14:32:11Z',
        alerted_at: '2026-04-29T14:32:11Z',
        context: { week_start_date: '2026-04-27' },
      },
    ];
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts,
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);
    expect(getState().toast).toBeNull();
  });

  it('subsequent tick with new alert surfaces toast', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    const fresh = {
      id: 'weekly:2026-04-27:95',
      axis: 'weekly' as const,
      threshold: 95,
      crossed_at: '2026-04-29T15:32:11Z',
      alerted_at: '2026-04-29T15:32:11Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [fresh],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast?.kind).toBe('alert');
  });

  it('reconnect (isFirstTick=true) does not re-surface known alerts as toasts', () => {
    const alert = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T14:32:11Z',
      alerted_at: '2026-04-29T14:32:11Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [alert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast?.kind).toBe('alert');
    dispatch({ type: 'HIDE_TOAST' });
    // simulate reconnect — same alert, isFirstTick=true again
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [alert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().toast).toBeNull();
  });
});
