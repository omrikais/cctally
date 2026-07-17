// #294 S5 Task 7 — the source-aware toast pipeline (INGEST_SOURCE_ALERTS).
import { beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';
import type { AlertsConfig } from './store';
import type { SourceAlertRow } from '../types/envelope';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
};

const claudeRow: SourceAlertRow = {
  source: 'claude',
  key: 'alert:claude:0:weekly:90',
  id: 'weekly:2026-04-13:90:0',
  axis: 'weekly',
  threshold: 90,
  crossed_at: '2026-04-16T12:00:00Z',
  alerted_at: '2026-04-16T12:00:00Z',
  context: { week_start_date: '2026-04-13' },
};

const codexBudgetRow: SourceAlertRow = {
  source: 'codex',
  key: 'alert:codex:codex_budget:calendar-month:90',
  axis: 'codex_budget',
  period: 'calendar-month',
  threshold: 90,
  value: 90.5,
  created_at: '2026-04-20T00:00:00Z',
};

function ingest(rows: SourceAlertRow[], isFirstTick: boolean): void {
  dispatch({ type: 'INGEST_SOURCE_ALERTS', rows, alertsSettings: CONFIG, isFirstTick });
}

beforeEach(() => {
  _resetForTests();
});

describe('INGEST_SOURCE_ALERTS toast pipeline', () => {
  it('first tick seeds rows as seen without surfacing a toast', () => {
    ingest([claudeRow, codexBudgetRow], true);
    expect(getState().toast).toBeNull();
    expect(getState().alertToastQueue).toEqual([]);
    // Both normalized identities are marked seen.
    expect(getState().seenAlertIds.has('claude:weekly:2026-04-13:90:0')).toBe(true);
    expect(getState().seenAlertIds.has(`codex:${codexBudgetRow.key}`)).toBe(true);
  });

  it('first tick seeds BOTH the normalized and the legacy bare forms (continuity)', () => {
    ingest([claudeRow], true);
    // normalized …
    expect(getState().seenAlertIds.has('claude:weekly:2026-04-13:90:0')).toBe(true);
    // … AND the bare legacy id.
    expect(getState().seenAlertIds.has('weekly:2026-04-13:90:0')).toBe(true);
  });

  it('a fresh Codex alert surfaces exactly one toast (no double consumption)', () => {
    ingest([], true); // cold-start seeds nothing
    ingest([codexBudgetRow], false);
    const toast = getState().toast;
    expect(toast?.kind).toBe('alert');
    expect(toast?.kind === 'alert' && 'source' in toast.payload && toast.payload.source).toBe('codex');
    // Exactly one — no shadow copy from a legacy feed.
    expect(getState().alertToastQueue).toEqual([]);
  });

  it('fires toasts regardless of the active source', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    ingest([], true);
    ingest([codexBudgetRow], false); // a Codex alert while Claude is active
    const toast = getState().toast;
    expect(toast?.kind === 'alert' && 'source' in toast.payload && toast.payload.source).toBe('codex');
  });

  it('a source switch neither replays nor clears seen state', () => {
    ingest([claudeRow, codexBudgetRow], true); // seed both as seen
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // No replay: still no toast, seen state intact.
    expect(getState().toast).toBeNull();
    expect(getState().seenAlertIds.has(`codex:${codexBudgetRow.key}`)).toBe(true);
    // A re-ingest of the same rows does not re-toast (still seen after the switch).
    ingest([claudeRow, codexBudgetRow], false);
    expect(getState().toast).toBeNull();
  });

  it('multiple fresh alerts queue; HIDE_TOAST drains the queue head-first', () => {
    ingest([], true);
    ingest([claudeRow, codexBudgetRow], false);
    // Head surfaces, the rest queues.
    expect(getState().toast?.kind).toBe('alert');
    expect(getState().alertToastQueue.length).toBe(1);
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast?.kind).toBe('alert');
    expect(getState().alertToastQueue.length).toBe(0);
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();
  });

  it('reconnect (isFirstTick again) re-seeds without replaying', () => {
    ingest([], true);
    ingest([claudeRow], false); // toast fires
    dispatch({ type: 'HIDE_TOAST' });
    // Reconnect: a fresh first-tick with the same rows must not re-toast.
    ingest([claudeRow, codexBudgetRow], true);
    expect(getState().toast).toBeNull();
    expect(getState().seenAlertIds.has(`codex:${codexBudgetRow.key}`)).toBe(true);
  });
});
