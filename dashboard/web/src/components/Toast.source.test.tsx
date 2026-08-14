// #294 S5 Task 7 — alert toasts carry a source chip and render both providers.
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Toast } from './Toast';
import { _resetForTests, dispatch } from '../store/store';
import type { AlertsConfig } from '../store/store';
import type { SourceAlertRow } from '../types/envelope';
import { AXIS_CHIP_LABEL } from '../lib/alertAxis';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
  // #513 S2 §5.1 — the mirrored Claude weekly budget amount; null here
  // because these fixtures describe alert rendering, not the budget state.
  weekly_usd: null,
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

const codexRow: SourceAlertRow = {
  source: 'codex',
  key: 'alert:codex:codex_budget:calendar-month:100',
  axis: 'codex_budget',
  period: 'calendar-month',
  threshold: 100,
  value: 105,
  created_at: '2026-04-20T00:00:00Z',
};

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function surface(row: SourceAlertRow): void {
  act(() => dispatch({ type: 'INGEST_SOURCE_ALERTS', rows: [], alertsSettings: CONFIG, isFirstTick: true }));
  act(() => dispatch({ type: 'INGEST_SOURCE_ALERTS', rows: [row], alertsSettings: CONFIG, isFirstTick: false }));
}

describe('<Toast /> source-aware alert rendering', () => {
  it('a Claude alert toast carries a Claude source chip', () => {
    surface(claudeRow);
    const { container } = render(<Toast />);
    const chip = container.querySelector('.source-chip');
    expect(chip?.textContent).toBe('Claude');
    expect(container.querySelector('.toast--alert')).not.toBeNull();
  });

  it('a Codex alert toast carries a Codex source chip and renders codex_budget content', () => {
    surface(codexRow);
    const { container } = render(<Toast />);
    const chip = container.querySelector('.source-chip');
    expect(chip?.textContent).toBe('Codex');
    // The axis chip + threshold render for the lean Codex row. The chip is
    // selected by CLASS: after #556 S3 its text is shared with the Claude
    // budget axis, and the source chip is what says which provider it is.
    expect(container.querySelector('.chip--codex_budget')?.textContent).toBe('BUDGET');
    expect(container.textContent).toContain('100%');
  });

  // #556 S3 §4.1 — the ONE live path that already rendered through
  // AXIS_CHIP_LABEL rather than through the deleted hardcode. The Settings
  // overlay enumerates every axis including `codex_budget`, posts it to
  // /api/alerts/test, and dispatches the returned legacy AlertEntry;
  // `normalizeToastRow` stamps it `source: 'claude'`, so `alertDisplay` reads
  // the map. Covered here so the rename's effect on this surface is observed
  // rather than assumed.
  it('a Settings test-alert for codex_budget takes its chip from the axis map', () => {
    act(() => dispatch({
      type: 'SHOW_ALERT_TOAST',
      alert: {
        id: 'codex_budget:2026-04-01T00:00:00Z:calendar-month:100',
        axis: 'codex_budget',
        threshold: 100,
        crossed_at: '2026-04-20T00:00:00Z',
        alerted_at: '2026-04-20T00:00:00Z',
        context: {
          period: 'calendar-month',
          period_start_at: '2026-04-01T00:00:00Z',
          budget_usd: 100,
          spent_usd: 105,
          consumption_pct: 105,
        },
      } as never,
    }));
    const { container } = render(<Toast />);
    expect(container.querySelector('.chip--codex_budget')?.textContent)
      .toBe(AXIS_CHIP_LABEL.codex_budget);
    expect(AXIS_CHIP_LABEL.codex_budget).toBe('BUDGET');
  });
});
