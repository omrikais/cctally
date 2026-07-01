// RecentAlertsModal empty-state teaching gauge (RA-1).
//
// The empty branch mirrors the already-reviewed RecentAlertsPanel empty tile:
// it reads the current weekly used% from the snapshot header and the CONFIGURED
// fire thresholds from `alertsConfig.weekly_thresholds` (fallback [90, 95]) —
// never hardcoding 90/95. The non-vacuous assertion feeds a NON-default
// [80, 95] config so a hardcoded-90/95 gauge would fail. When used% is unknown
// the gauge degrades to the one-liner.
import { beforeEach, describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { RecentAlertsModal } from './RecentAlertsModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import type { AlertsConfig } from '../store/store';
import type { Envelope } from '../types/envelope';

function config(weekly_thresholds: number[]): AlertsConfig {
  return {
    enabled: true,
    weekly_thresholds,
    five_hour_thresholds: [],
    budget_thresholds: [],
  };
}

function envWith(usedPct: number | null): Envelope {
  return { header: { used_pct: usedPct } } as unknown as Envelope;
}

function seed(usedPct: number | null, weekly: number[]): void {
  act(() => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: config(weekly),
      isFirstTick: true,
    });
    updateSnapshot(envWith(usedPct));
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('RecentAlertsModal empty teaching gauge (RA-1)', () => {
  it('renders a teaching gauge with ticks tracking the configured thresholds', () => {
    seed(11, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    const ticks = [...container.querySelectorAll('.ra-gauge-tick')].map((t) =>
      t.getAttribute('data-th'),
    );
    // Non-vacuous: a hardcoded 90/95 gauge would produce ['90','95'] and fail.
    expect(ticks).toEqual(['80', '95']);
    expect(container.textContent).toContain('11%');
  });

  it('positions each tick at its configured threshold percent', () => {
    seed(11, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    const ticks = [...container.querySelectorAll<HTMLElement>('.ra-gauge-tick')];
    expect(ticks[0].style.left).toBe('80%');
    expect(ticks[1].style.left).toBe('95%');
  });

  it('marks the lowest threshold amber and the higher one red', () => {
    seed(11, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    const ticks = [...container.querySelectorAll('.ra-gauge-tick')];
    expect(ticks[0].className).toContain('tick-amber'); // lowest (80)
    expect(ticks[1].className).toContain('tick-red'); // higher (95)
  });

  it('gives interior thresholds a distinct mid tone when 3+ are configured', () => {
    seed(11, [80, 90, 95]);
    const { container } = render(<RecentAlertsModal />);
    const ticks = [...container.querySelectorAll('.ra-gauge-tick')];
    expect(ticks[0].className).toContain('tick-amber'); // lowest (80)
    expect(ticks[1].className).toContain('tick-mid');   // middle (90)
    expect(ticks[2].className).toContain('tick-red');   // highest (95)
    expect(ticks[1].className).not.toContain('tick-amber');
    expect(ticks[1].className).not.toContain('tick-red');
  });

  it('shows the reassuring header when used% is below the lowest threshold', () => {
    seed(11, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.ra-gauge-head')).not.toBeNull();
    expect(container.textContent).toContain('well under the line');
  });

  it('omits the reassuring header when used% is at/above the lowest threshold', () => {
    seed(88, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    // still a teaching gauge (used% known), just without the "well under" header
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-head')).toBeNull();
  });

  it('falls back to the one-liner when used% is unknown', () => {
    seed(null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.ra-gauge')).toBeNull();
    expect(container.textContent).toContain('No alerts yet');
  });
});
