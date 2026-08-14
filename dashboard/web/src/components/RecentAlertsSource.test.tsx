// #294 S5 Task 7 — the source-aware Recent Alerts panel (§6.7 Panel).
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeClaudeSourceData,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeAllSourceEntry,
  type SourceEnvelopeSlice,
} from '../test-utils/sourceEnvelope';
import type { CodexAlertRow, Envelope } from '../types/envelope';

const claudeAlert = {
  source: 'claude' as const,
  key: 'alert:claude:0:weekly:90',
  id: 'weekly:2026-04-13:90:0',
  axis: 'weekly' as const,
  threshold: 90,
  crossed_at: '2026-04-16T12:00:00Z',
  alerted_at: '2026-04-16T12:00:00Z',
  context: { week_start_date: '2026-04-13' },
};

const codexQuotaAlert: CodexAlertRow = {
  source: 'codex',
  key: 'alert:codex:quota:root:limit:0:300:reset:95:t',
  axis: 'quota',
  threshold: 95,
  severity: 'critical',
  created_at: '2026-04-21T00:00:00Z',
};

function makeBundle(over?: { claudeAlerts?: unknown[]; codexAlerts?: CodexAlertRow[] }): SourceEnvelopeSlice {
  const claude = makeClaudeSourceEntry({
    data: {
      ...makeClaudeSourceData(),
      alerts: { rows: (over?.claudeAlerts ?? []) as Record<string, unknown>[] },
    },
  });
  const codexData = makeCodexSourceData();
  const codex = makeCodexSourceEntry({
    data: { ...codexData, alerts: { rows: over?.codexAlerts ?? codexData.alerts.rows } },
  });
  return {
    source_schema_version: 2,
    default_source: 'claude',
    source_order: ['claude', 'codex', 'all'],
    sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
  };
}

function env(bundle: SourceEnvelopeSlice): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk',
      used_pct: 42,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [90, 95], five_hour_thresholds: [90, 95], budget_thresholds: [90, 95] },
    ...bundle,
  } as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('RecentAlertsPanel — source-aware rows (§6.7)', () => {
  it('Claude mode shows Claude-owned rows, not Codex rows', () => {
    act(() => updateSnapshot(env(makeBundle({ claudeAlerts: [claudeAlert] }))));
    const { container } = render(<RecentAlertsPanel />);
    expect(screen.getByText('WEEKLY')).toBeInTheDocument();
    expect(screen.getByText('90%')).toBeInTheDocument();
    // #556 S3 §4.4 — this asserted that no element read 'CODEX'. After the
    // rename no 'CODEX' text exists anywhere in the app, so that assertion
    // would pass for a reason unrelated to what it checks. Assert the absence
    // of the Codex BUDGET row itself, by its axis class, which stays
    // discriminating whatever the chip is called.
    expect(container.querySelector('.chip--codex_budget')).toBeNull();
    expect(screen.queryByText('QUOTA')).toBeNull();
  });

  it('Codex mode shows Codex rows incl. the quota axis with a native label', () => {
    act(() => updateSnapshot(env(makeBundle({ codexAlerts: [codexQuotaAlert] }))));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<RecentAlertsPanel />);
    expect(screen.getByText('QUOTA')).toBeInTheDocument();
    expect(screen.getByText('95%')).toBeInTheDocument();
    // No Claude weekly row leaked in.
    expect(screen.queryByText('WEEKLY')).toBeNull();
  });

  it('All mode shows the union with a per-row source chip and no cross-source merge', () => {
    act(() =>
      updateSnapshot(
        env(makeBundle({ claudeAlerts: [claudeAlert], codexAlerts: [codexQuotaAlert] })),
      ),
    );
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    const { container } = render(<RecentAlertsPanel />);
    // Both rows present, each labeled by source.
    expect(screen.getByText('WEEKLY')).toBeInTheDocument();
    expect(screen.getByText('QUOTA')).toBeInTheDocument();
    const chips = container.querySelectorAll('.source-chip');
    const labels = Array.from(chips).map((c) => c.textContent);
    expect(labels).toContain('Claude');
    expect(labels).toContain('Codex');
  });

  it('All mode carries the sub-line, as a sibling of the header', () => {
    // #556 S3 §4.6. The sibling relationship is the assertion, not decoration:
    // inside `.panel-header` the element loses full-card width and stops
    // wrapping, which is the defect S2 shipped at 390px.
    act(() =>
      updateSnapshot(
        env(makeBundle({ claudeAlerts: [claudeAlert], codexAlerts: [codexQuotaAlert] })),
      ),
    );
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    const { container } = render(<RecentAlertsPanel />);
    const note = container.querySelector('.panel-range-note');
    expect(note?.textContent).toBe('Both providers, newest first');
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
  });

  it('gates the sub-line on All explicitly — Claude and Codex get none', () => {
    act(() =>
      updateSnapshot(
        env(makeBundle({ claudeAlerts: [claudeAlert], codexAlerts: [codexQuotaAlert] })),
      ),
    );
    const { container: claudeView } = render(<RecentAlertsPanel />);
    expect(claudeView.querySelector('.panel-range-note')).toBeNull();
    cleanup();
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const { container: codexView } = render(<RecentAlertsPanel />);
    expect(codexView.querySelector('.panel-range-note')).toBeNull();
  });

  it('All mode renders the rows in firing order, interleaved across providers', () => {
    // #556 S3 §5.2 — the test above renders All and checks only MEMBERSHIP, so
    // it passed over E1 for as long as E1 existed. These four rows interleave
    // by firing instant, so any source-grouped ordering fails here.
    const claudeOld = {
      ...claudeAlert,
      id: 'weekly:2026-04-13:50:0',
      key: 'alert:claude:1:weekly:50',
      threshold: 50,
      alerted_at: '2026-04-16T13:57:00Z',
      crossed_at: '2026-04-16T13:50:00Z',
    };
    const claudeNew = {
      ...claudeAlert,
      threshold: 90,
      alerted_at: '2026-04-16T13:59:00Z',
      crossed_at: '2026-04-16T13:40:00Z',
    };
    const codexNew: CodexAlertRow = {
      ...codexQuotaAlert,
      threshold: 95,
      alerted_at: '2026-04-16T13:58:00Z',
      created_at: '2026-04-16T13:58:00Z',
    };
    const codexOld: CodexAlertRow = {
      ...codexQuotaAlert,
      key: 'alert:codex:quota:root:limit:0:300:reset:60:t',
      threshold: 60,
      alerted_at: '2026-04-16T13:56:00Z',
      created_at: '2026-04-16T13:56:00Z',
    };
    act(() =>
      updateSnapshot(
        env(makeBundle({
          claudeAlerts: [claudeNew, claudeOld],
          codexAlerts: [codexNew, codexOld],
        })),
      ),
    );
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    const { container } = render(<RecentAlertsPanel />);
    const rendered = Array.from(container.querySelectorAll('.alert-row')).map(
      (row) => [
        row.querySelector('.source-chip')?.textContent,
        row.textContent?.match(/\d+%/)?.[0],
      ].join(' '),
    );
    expect(rendered).toEqual([
      'Claude 90%',   // 13:59Z
      'Codex 95%',    // 13:58Z
      'Claude 50%',   // 13:57Z
      'Codex 60%',    // 13:56Z
    ]);
  });

  it('renders per-source empty copy under Codex when there are no Codex alerts', () => {
    act(() => updateSnapshot(env(makeBundle({ codexAlerts: [] }))));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const { container } = render(<RecentAlertsPanel />);
    expect(container.textContent).toContain('No Codex alerts');
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-bar')).not.toBeNull();
    // The Claude weekly-gauge copy must not appear under Codex.
    expect(container.textContent).not.toContain('weekly usage crosses');
  });

  it('Claude-mode row rendering is value-identical to the legacy path', () => {
    // Legacy path (no sources bundle) via INGEST_SNAPSHOT_ALERTS.
    act(() =>
      dispatch({
        type: 'INGEST_SNAPSHOT_ALERTS',
        alerts: [{ ...claudeAlert }],
        alertsSettings: { enabled: true, weekly_thresholds: [90, 95], five_hour_thresholds: [90, 95], budget_thresholds: [90, 95] },
        isFirstTick: true,
      }),
    );
    const legacy = render(<RecentAlertsPanel />);
    const legacyRow = legacy.container.querySelector('.alert-row')!.textContent;
    legacy.unmount();
    _resetForTests();
    // Source path (bundle present) with the same underlying alert.
    act(() => updateSnapshot(env(makeBundle({ claudeAlerts: [claudeAlert] }))));
    const src = render(<RecentAlertsPanel />);
    const srcRow = src.container.querySelector('.alert-row')!.textContent;
    expect(srcRow).toBe(legacyRow);
  });
});

describe('RecentAlertsPanel — All empty union copy', () => {
  it('shows an all-sources empty copy when neither provider has alerts', () => {
    act(() => updateSnapshot(env(makeBundle({ claudeAlerts: [], codexAlerts: [] }))));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    const { container } = render(<RecentAlertsPanel />);
    const body = within(container.querySelector('#panel-alerts-body') as HTMLElement);
    expect(body.getByText(/No alerts yet/i)).toBeInTheDocument();
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-bar')).not.toBeNull();
    expect(container.textContent).toContain('90% / 95%');
  });
});
