// #661 S2 Task D4 step 3b — the non-threshold family finally RENDERS.
//
// Stage C published the `meter_rate_changes` wire array and the
// `MeterRateChangeEntry` TypeScript variant and rendered neither, because the
// alerts table is threshold-shaped (`%` / `Axis` / `Cost`) and the toast
// pipeline consumes `AlertEntry` rows.
//
// What this module owns is that the family reaches a screen WITHOUT being
// forced into `AlertEntry`. Spec section 6.1 exists to prevent that widening:
// `AlertEntry` requires a numeric `threshold`, its `id` is threshold-shaped,
// and `alert_row_owner` raises on a seventh axis so that adding one without
// deciding its ownership fails a test rather than shipping an invisible row.
import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RecentAlertsModal } from './RecentAlertsModal';
import { Toast } from './Toast';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { collectToastAlertRows } from '../lib/alertIdentity';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { AlertsConfig } from '../store/store';
import type { Envelope, MeterRateChangeEntry } from '../types/envelope';

function config(): AlertsConfig {
  return {
    enabled: true,
    weekly_thresholds: [90, 95],
    five_hour_thresholds: [],
    budget_thresholds: [],
    weekly_usd: null,
  };
}

function rateChange(over: Partial<MeterRateChangeEntry> = {}): MeterRateChangeEntry {
  return {
    id: 'meter_rate_change:claude:unattributed:2026-08-25T00:00:00+00:00',
    family: 'meter_rate_change',
    provider: 'claude',
    owner: 'claude',
    severity: 'alarm',
    effective_from: '2026-08-25T00:00:00+00:00',
    detected_at: '2026-08-29T12:00:00+00:00',
    recorded_at: '2026-08-29T12:00:00+00:00',
    previous_units_per_point: 2_442_620,
    new_units_per_point: 1_685_000,
    ...over,
  };
}

function envWith(changes: MeterRateChangeEntry[]): Envelope {
  const sourceSlice = makeSourceEnvelope();
  const claude = makeClaudeSourceEntry({
    data: { ...sourceSlice.sources.claude.data!, alerts: { rows: [] } },
  });
  const codex = makeCodexSourceEntry({
    data: { ...sourceSlice.sources.codex.data!, alerts: { rows: [] } },
  });
  return {
    header: { used_pct: 11 },
    alerts: [],
    alerts_settings: config(),
    meter_rate_changes: changes,
    ...makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    }),
  } as unknown as Envelope;
}

function seed(changes: MeterRateChangeEntry[], { firstTick = true } = {}): void {
  const snap = envWith(changes);
  act(() => {
    if (updateSnapshot(snap)) {
      dispatch({
        type: 'INGEST_SOURCE_ALERTS',
        rows: collectToastAlertRows(snap),
        rateChanges: changes,
        alertsSettings: snap.alerts_settings ?? config(),
        isFirstTick: firstTick,
      });
    }
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('the alerts modal renders a separate rate-transition section', () => {
  it('renders the section with columns that suit a rate transition', () => {
    seed([rateChange()]);
    const { container } = render(<RecentAlertsModal />);
    const section = container.querySelector('[data-testid="alerts-rate-change"]');
    expect(section).not.toBeNull();
    const headers = [...section!.querySelectorAll('th')].map((h) => h.textContent);
    expect(headers).toEqual(['Provider', 'Change', 'Units / point', 'Effective']);
    // The columns the THRESHOLD table has and this one must not: a rate
    // transition has no percentage and no cost.
    expect(headers).not.toContain('%');
    expect(headers).not.toContain('Cost');
    expect(headers).not.toContain('Axis');
  });

  it('states the direction and size of the change', () => {
    seed([rateChange()]);
    const { container } = render(<RecentAlertsModal />);
    expect(
      container.querySelector('[data-testid="alerts-rate-change"]')?.textContent,
    ).toContain('31% less usage');
  });

  it('renders NO section when nothing was recorded', () => {
    seed([]);
    const { container } = render(<RecentAlertsModal />);
    expect(
      container.querySelector('[data-testid="alerts-rate-change"]'),
    ).toBeNull();
  });

  it('renders the section beside the empty gauge when only a rate change exists', () => {
    // A store with a recorded transition and no threshold crossing is a real
    // state. The gauge alone would hide the one thing there IS to report.
    seed([rateChange()]);
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.ra-gauge, [class*="gauge"]')).not.toBeNull();
    expect(
      container.querySelector('[data-testid="alerts-rate-change"]'),
    ).not.toBeNull();
  });

  it('tolerates a server that omits the array entirely', () => {
    // An older server sends no `meter_rate_changes` key at all, which a tab
    // surviving an `execvp` restart really does meet.
    const snap = envWith([]);
    delete (snap as { meter_rate_changes?: unknown }).meter_rate_changes;
    act(() => { updateSnapshot(snap); });
    const { container } = render(<RecentAlertsModal />);
    expect(
      container.querySelector('[data-testid="alerts-rate-change"]'),
    ).toBeNull();
  });
});

describe('the toast branches on the variant tag', () => {
  it('renders the rate-change toast, and not the threshold body', () => {
    seed([], { firstTick: true });
    seed([rateChange()], { firstTick: false });
    const { container } = render(<Toast />);
    const toast = container.querySelector('[data-testid="toast-meter-rate-change"]');
    expect(toast).not.toBeNull();
    expect(toast?.textContent).toContain('metering rate changed');
    expect(toast?.textContent).toContain('31% less usage');
    expect(toast?.textContent).toContain('cctally quota');
    // A threshold toast would print a percentage in its head. This family has
    // no threshold, so there is none to print.
    expect(toast?.querySelector('.toast--alert-threshold')).toBeNull();
  });

  it('carries the family’s EXPLICIT severity, never a threshold-derived one', () => {
    seed([], { firstTick: true });
    seed([rateChange({ severity: 'info' })], { firstTick: false });
    const { container } = render(<Toast />);
    expect(
      container.querySelector('[data-testid="toast-meter-rate-change"]')?.className,
    ).toContain('toast--severity-info');
  });

  it('does NOT toast on a cold start', () => {
    // Section 6.2: events are recorded from the first upgrade, so a cold
    // connect finds a history the user has already seen in `cctally quota`.
    // Toasting each of them is exactly the surprise the default-off
    // convention exists to prevent.
    seed([rateChange()], { firstTick: true });
    const { container } = render(<Toast />);
    expect(
      container.querySelector('[data-testid="toast-meter-rate-change"]'),
    ).toBeNull();
  });

  it('does not re-toast a transition it has already shown', () => {
    seed([], { firstTick: true });
    seed([rateChange()], { firstTick: false });
    act(() => { dispatch({ type: 'HIDE_TOAST' }); });
    seed([rateChange()], { firstTick: false });
    const { container } = render(<Toast />);
    expect(
      container.querySelector('[data-testid="toast-meter-rate-change"]'),
    ).toBeNull();
  });
});
