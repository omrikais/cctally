// RecentAlertsModal empty-state teaching gauge (RA-1).
//
// The empty branch mirrors the already-reviewed RecentAlertsPanel empty tile:
// it reads the current weekly used% from the snapshot header and the CONFIGURED
// fire thresholds from `alertsConfig.weekly_thresholds` (fallback [90, 95]) —
// never hardcoding 90/95. The non-vacuous assertion feeds a NON-default
// [80, 95] config so a hardcoded-90/95 gauge would fail. Unknown usage keeps
// the same explanatory gauge hierarchy with an explicit unavailable hero.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { RecentAlertsModal } from './RecentAlertsModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { collectToastAlertRows } from '../lib/alertIdentity';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { AlertsConfig } from '../store/store';
import type { AlertEntry, Envelope } from '../types/envelope';

function config(weekly_thresholds: number[]): AlertsConfig {
  return {
    enabled: true,
    weekly_thresholds,
    five_hour_thresholds: [],
    budget_thresholds: [],
    // #513 S2 §5.1 — the mirrored Claude weekly budget amount; null here
    // because these fixtures describe alert rendering, not the budget state.
    weekly_usd: null,
  };
}

function envWith(
  usedPct: number | null,
  alerts: AlertEntry[],
  alertsSettings: AlertsConfig,
): Envelope {
  const sourceRows = alerts.map((alert) => ({
    ...alert,
    source: 'claude' as const,
    key: `alert:claude:${alert.id}`,
  }));
  const sourceSlice = makeSourceEnvelope();
  const claude = makeClaudeSourceEntry({
    data: {
      ...sourceSlice.sources.claude.data!,
      alerts: { rows: sourceRows },
    },
  });
  const codex = makeCodexSourceEntry({
    data: {
      ...sourceSlice.sources.codex.data!,
      alerts: { rows: [] },
    },
  });
  return {
    header: { used_pct: usedPct },
    alerts: [],
    alerts_settings: alertsSettings,
    ...makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    }),
  } as unknown as Envelope;
}

function seedAlerts(
  alerts: AlertEntry[],
  usedPct: number | null,
  weekly: number[],
): void {
  const snap = envWith(usedPct, alerts, config(weekly));
  act(() => {
    if (updateSnapshot(snap)) {
      dispatch({
        type: 'INGEST_SOURCE_ALERTS',
        rows: collectToastAlertRows(snap),
        alertsSettings: snap.alerts_settings ?? config(weekly),
        isFirstTick: true,
      });
    }
  });
}

function seed(usedPct: number | null, weekly: number[]): void {
  seedAlerts([], usedPct, weekly);
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

  it('replaces the reassuring header with a neutral empty-state header at/above the lowest threshold', () => {
    seed(88, [80, 95]);
    const { container } = render(<RecentAlertsModal />);
    // Still the canonical teaching gauge, but without the reassuring copy.
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-head')?.textContent).toContain('No alerts yet');
    expect(container.querySelector('.ra-gauge-head')?.textContent).not.toContain('well under');
  });

  it('keeps the canonical gauge and marks the hero unavailable when used% is unknown', () => {
    seed(null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-hero')?.textContent).toBe('—');
    expect(container.textContent).toContain('No alerts yet');
    expect(container.textContent).toContain('90% / 95%');
  });
});

// #574 — the modal's when-cell carries the same contract as the panel's. Both
// surfaces are asserted because they render the cell independently, and #556 S3
// showed that a change applied to one and not the other splits them silently.
describe('RecentAlertsModal firing instant (#574)', () => {
  function alert(partial: Partial<AlertEntry>): AlertEntry {
    return {
      id: 'weekly:2026-04-13:90:0',
      axis: 'weekly',
      threshold: 90,
      crossed_at: '2026-04-16T13:56:00Z',
      alerted_at: '2026-04-16T13:56:00Z',
      context: { week_start_date: '2026-04-13' },
      ...partial,
    };
  }

  // Only the "recent" fixture pins the clock; see the panel test for why that
  // pinning is mandatory rather than tidy.
  afterEach(() => {
    vi.useRealTimers();
  });

  const whenCells = (container: HTMLElement): HTMLElement[] =>
    [...container.querySelectorAll<HTMLElement>('.alert-cell-when')];

  it('titles an absolute-branch row with the instant, including its calendar year', () => {
    seedAlerts([alert({ id: 'when:abs' })], null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    expect(whenCells(container)[0].getAttribute('title')).toBe('2026-04-16 13:56 UTC');
  });

  it('renders different visible text for two alerts minutes apart on one calendar day', () => {
    seedAlerts([
      alert({ id: 'when:first', alerted_at: '2026-04-16T13:56:00Z' }),
      alert({ id: 'when:last', alerted_at: '2026-04-16T13:59:00Z' }),
    ], null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    expect(whenCells(container).map((c) => c.textContent))
      .toEqual(['Apr 16 13:56 UTC', 'Apr 16 13:59 UTC']);
  });

  it('titles a relative-branch row too, with the clock pinned so the row stays recent', () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-04-16T14:00:00Z'));
    seedAlerts([alert({ id: 'when:recent' })], null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('4m ago');
    expect(cell.getAttribute('title')).toBe('2026-04-16 13:56 UTC');
  });

  it('carries no title attribute when the instant is absent', () => {
    seedAlerts([alert({
      id: 'when:null',
      alerted_at: undefined as unknown as string,
    })], null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('—');
    expect(cell.hasAttribute('title')).toBe(false);
  });

  it('carries no title attribute when the instant is a non-empty string that does not parse', () => {
    seedAlerts([alert({ id: 'when:bad', alerted_at: 'not-a-date' })], null, [90, 95]);
    const { container } = render(<RecentAlertsModal />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('—');
    expect(cell.hasAttribute('title')).toBe(false);
  });
});
