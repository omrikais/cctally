// #513 S2 §5.1 — the Claude weekly budget amount is mirrored into the
// dashboard envelope so Settings can distinguish "no budget configured" from
// "budget configured, alerts off" without POSTing an empty `{"budget":{}}`
// (which would reach `save_config` and trigger the synchronous rebuild).
//
// The compatibility hazard this file exists for: the SSE seam passes a PRESENT
// `alerts_settings` block through wholesale and only defaults when the whole
// block is absent. An older server that sends the block without the new leaf
// therefore yields `undefined`, not a canonical `null`, and every consumer
// would have to re-derive the same defaulting. `normalizeAlertsSettings` is
// the single chokepoint that turns the optional wire leaf into the required
// store leaf, and the store reducer is the only writer of `alertsConfig`.
import { beforeEach, describe, expect, it } from 'vitest';
import type { AlertsSettingsEnvelope } from '../types/envelope';
import {
  _resetForTests,
  dispatch,
  getState,
  normalizeAlertsSettings,
} from './store';

function wire(patch: Partial<AlertsSettingsEnvelope> = {}): AlertsSettingsEnvelope {
  return {
    enabled: false,
    weekly_thresholds: [90, 95],
    five_hour_thresholds: [90, 95],
    budget_thresholds: [90, 100],
    budget_enabled: false,
    ...patch,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('normalizeAlertsSettings — the weekly-budget mirror (#513 S2 §5.1)', () => {
  it('normalizes a missing weekly_usd to null when the block is present', () => {
    expect(normalizeAlertsSettings(wire()).weekly_usd).toBeNull();
  });

  it('carries an explicit null through unchanged', () => {
    expect(normalizeAlertsSettings(wire({ weekly_usd: null })).weekly_usd).toBeNull();
  });

  it('carries a number through unchanged', () => {
    expect(normalizeAlertsSettings(wire({ weekly_usd: 250 })).weekly_usd).toBe(250);
  });

  it('leaves every other mirrored leaf untouched', () => {
    const normalized = normalizeAlertsSettings(
      wire({ notifier: 'osascript', codex_budget_configured: true }),
    );
    expect(normalized.notifier).toBe('osascript');
    expect(normalized.codex_budget_configured).toBe(true);
    expect(normalized.weekly_thresholds).toEqual([90, 95]);
  });
});

describe('the store never exposes an undefined weekly_usd', () => {
  it('defaults to null before the first tick', () => {
    expect(getState().alertsConfig.weekly_usd).toBeNull();
  });

  it('is null after a tick whose block omits the leaf', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: wire(),
      isFirstTick: true,
    });
    expect(getState().alertsConfig.weekly_usd).toBeNull();
  });

  it('carries the amount through the source-alerts tick', () => {
    dispatch({
      type: 'INGEST_SOURCE_ALERTS',
      rows: [],
      alertsSettings: wire({ weekly_usd: 120.5 }),
      isFirstTick: true,
    });
    expect(getState().alertsConfig.weekly_usd).toBe(120.5);
  });

  it('a later tick that omits the leaf resets it to null rather than retaining the old amount', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: wire({ weekly_usd: 200 }),
      isFirstTick: true,
    });
    expect(getState().alertsConfig.weekly_usd).toBe(200);
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: wire(),
      isFirstTick: false,
    });
    // Wholesale replacement is the contract for this slice: the server is the
    // source of truth, so a budget cleared on the server must clear here too.
    expect(getState().alertsConfig.weekly_usd).toBeNull();
  });
});
