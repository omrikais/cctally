// RecentAlertsPanel — severity consumption (Task F).
//
// The panel must render the color class from `alert.severity` (the kernel's
// single authority) and only fall back to threshold derivation when the
// field is absent. The smoking-gun case feeds a severity that DISAGREES with
// what the threshold would derive, proving the panel consumes rather than
// recomputes.
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { collectToastAlertRows } from '../lib/alertIdentity';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { AlertEntry, Envelope } from '../types/envelope';
import type { AlertsConfig } from '../store/store';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
  // #513 S2 §5.1 — the mirrored Claude weekly budget amount; null here
  // because these fixtures describe alert rendering, not the budget state.
  weekly_usd: null,
};

function ingest(
  alerts: AlertEntry[],
  options: {
    usedPct?: number | null;
    legacyAlerts?: AlertEntry[];
    alertsSettings?: AlertsConfig;
  } = {},
) {
  const snap = alertEnv(
    options.usedPct ?? null,
    alerts,
    options.legacyAlerts ?? [],
    options.alertsSettings ?? CONFIG,
  );
  act(() => {
    if (updateSnapshot(snap)) {
      dispatch({
        type: 'INGEST_SOURCE_ALERTS',
        rows: collectToastAlertRows(snap),
        alertsSettings: snap.alerts_settings ?? CONFIG,
        isFirstTick: true, // cold-start: no toast side effects
      });
    }
  });
}

function entry(partial: Partial<AlertEntry>): AlertEntry {
  return {
    id: 'weekly:2026-04-13:90:0',
    axis: 'weekly',
    threshold: 90,
    crossed_at: '2026-04-16T12:00:00Z',
    alerted_at: '2026-04-16T12:00:00Z',
    context: { week_start_date: '2026-04-13' },
    ...partial,
  };
}

function alertEnv(
  usedPct: number | null,
  alerts: AlertEntry[],
  legacyAlerts: AlertEntry[],
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
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: usedPct, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: legacyAlerts,
    alerts_settings: alertsSettings,
    ...makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    }),
  };
}

beforeEach(() => {
  _resetForTests();
});

describe('RecentAlertsPanel severity', () => {
  it('renders the class from alert.severity, not recomputed from threshold', () => {
    // severity:'critical' but threshold 50 ⇒ if the panel recomputed it would
    // be 'info'. Consuming severity ⇒ critical class present, info absent.
    ingest([entry({ id: 'x:1', severity: 'critical', threshold: 50 })]);
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('50%');
    expect(cell.className).toContain('severity-critical');
    expect(cell.className).not.toContain('severity-info');
  });

  it('falls back to threshold bands when severity is absent', () => {
    // threshold 90 ⇒ warn (no severity field).
    ingest([entry({ id: 'y:1', threshold: 90 })]);
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('90%');
    expect(cell.className).toContain('severity-warn');
    // threshold 100 ⇒ critical.
    _resetForTests();
    ingest([entry({ id: 'z:1', threshold: 100 })]);
    render(<RecentAlertsPanel />);
    const critCell = screen.getByText('100%');
    expect(critCell.className).toContain('severity-critical');
  });
});

// #264 S1 (VOID-1) — the empty Alerts tile gains a teaching gauge below the
// one-liner (mirrors the RecentAlertsModal empty-state gauge vocabulary): a
// fill at the current used%, with a tick per configured weekly fire threshold,
// so "you're at 42%, alerts fire at 90/95" is SHOWN, not just told.
describe('RecentAlertsPanel empty-state teaching gauge (#264 S1)', () => {
  function seed(usedPct: number | null) {
    ingest([], { usedPct });
  }

  it('renders a gauge filled to used_pct with one tick per weekly threshold', () => {
    seed(42);
    const { container } = render(<RecentAlertsPanel />);
    const fill = container.querySelector('.ra-gauge-fill') as HTMLElement;
    expect(fill).not.toBeNull();
    expect(fill.style.width).toBe('42%');
    const ticks = container.querySelectorAll('.ra-gauge-tick');
    expect(Array.from(ticks).map((t) => (t as HTMLElement).style.left)).toEqual([
      '90%',
      '95%',
    ]);
  });

  it('keeps the canonical empty-state gauge when used_pct is unknown', () => {
    seed(null);
    const { container } = render(<RecentAlertsPanel />);
    expect((container.querySelector('.ra-gauge-fill') as HTMLElement).style.width).toBe('0%');
    expect(container.querySelector('.ra-gauge-hero')?.textContent).toBe('—');
    expect(container.querySelector('.panel-empty')).toBeNull();
    expect(container.textContent).toContain('No alerts yet');
  });
});

// #574 — the when-cell must disclose the firing instant. The visible text is
// what AC1/AC3 are written against; the `title` is the desktop convenience AC4
// specifies, and it must be ABSENT rather than "—" when the instant does not
// parse.
describe('RecentAlertsPanel firing instant (#574)', () => {
  // The render sites omit `nowMs`, so the component reads `Date.now()`. Only
  // the "recent" fixture below pins the clock; the absolute-branch fixtures use
  // instants far enough in the past that they can only age further into that
  // branch.
  afterEach(() => {
    vi.useRealTimers();
  });

  const whenCells = (container: HTMLElement): HTMLElement[] =>
    [...container.querySelectorAll<HTMLElement>('.alert-when')];

  it('renders the source-qualified bundle when the legacy top-level row disagrees', () => {
    ingest(
      [entry({ id: 'source-row', threshold: 73 })],
      { legacyAlerts: [entry({ id: 'legacy-row', threshold: 99 })] },
    );
    render(<RecentAlertsPanel />);
    expect(screen.getByText('73%')).toBeInTheDocument();
    expect(screen.queryByText('99%')).toBeNull();
  });

  it('titles an absolute-branch row with the instant, including its calendar year', () => {
    ingest([entry({ id: 'when:abs', alerted_at: '2026-04-16T13:56:00Z' })]);
    const { container } = render(<RecentAlertsPanel />);
    expect(whenCells(container)[0].getAttribute('title')).toBe('2026-04-16 13:56 UTC');
  });

  it('renders different visible text for two alerts minutes apart on one calendar day', () => {
    ingest([
      entry({ id: 'when:first', alerted_at: '2026-04-16T13:56:00Z' }),
      entry({ id: 'when:last', alerted_at: '2026-04-16T13:59:00Z' }),
    ]);
    const { container } = render(<RecentAlertsPanel />);
    expect(whenCells(container).map((c) => c.textContent))
      .toEqual(['Apr 16 13:56 UTC', 'Apr 16 13:59 UTC']);
  });

  it('titles a relative-branch row too, with the clock pinned so the row stays recent', () => {
    // Pinning is mandatory: a hard-coded "recent" instant would eventually age
    // into the absolute branch and this test would stop testing the relative
    // branch without ever failing.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-04-16T14:00:00Z'));
    ingest([entry({ id: 'when:recent', alerted_at: '2026-04-16T13:56:00Z' })]);
    const { container } = render(<RecentAlertsPanel />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('4m ago');
    expect(cell.getAttribute('title')).toBe('2026-04-16 13:56 UTC');
  });

  it('carries no title attribute when the instant is absent', () => {
    ingest([entry({
      id: 'when:null',
      alerted_at: undefined as unknown as string,
    })]);
    const { container } = render(<RecentAlertsPanel />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('—');
    expect(cell.hasAttribute('title')).toBe(false);
  });

  it('carries no title attribute when the instant is a non-empty string that does not parse', () => {
    // The case that separates a parse-aware guard from a truthiness check: a
    // truthy-but-unparseable instant formats to the "—" sentinel, so
    // `title={fmt.startedShort(...)}` would emit title="—" here.
    ingest([entry({ id: 'when:bad', alerted_at: 'not-a-date' })]);
    const { container } = render(<RecentAlertsPanel />);
    const cell = whenCells(container)[0];
    expect(cell.textContent).toBe('—');
    expect(cell.hasAttribute('title')).toBe(false);
  });
});
