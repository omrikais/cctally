// #293 S4 A11Y-1 / ACTION-1 — the Hybrid card-region contract, data-driven over
// all TEN grid panels. The regions must DESCRIBE (role=region + aria-label) but
// no longer IMPERSONATE a button: no region tab-stop, no region onKeyDown. The
// explicit Expand button stays the sole keyboard/SR path. A guarded pointer
// body-click survives ONLY where one exists today, and a click on a nested
// control OR the aria-hidden drag grip never double-fires the panel modal.
//
// Non-vacuity (proven on the remote runner by stashing the Task-2 edits):
//   - "region is not a tab stop"  → RED on main (every region has tabIndex={0}).
//   - "grip click opens nothing"  → RED on main (unguarded region onClick + no
//     data-card-region-ignore lets the bubbled grip click fire OPEN_MODAL).
// The bare-body-click-opens + row-preserves-sessionId cases are preservation /
// regression guards (green before and after) — emission is asserted via the
// store's post-click openModal/openSessionId, per store.ts:988 (a 2nd generic
// OPEN_MODAL would overwrite openSessionId to null, so the Sessions guard is a
// real state divergence, not a vacuous same-value overwrite).
import { type ComponentType } from 'react';
import { render, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ForecastPanel } from './ForecastPanel';
import { TrendPanel } from './TrendPanel';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
import { ProjectsPanel } from './ProjectsPanel';
import { CacheReportPanel } from './CacheReportPanel';
import { SessionsPanel } from './SessionsPanel';
import { DailyPanel } from './DailyPanel';
import { BlocksPanel } from './BlocksPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { _resetForTests, getState, updateSnapshot } from '../store/store';
import type { Envelope, SessionRow } from '../types/envelope';

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

// Projects populated → the MAIN (body-click) branch, not the unavailable one.
function projectsEnvelope(): Envelope {
  const env = baseEnvelope();
  env.projects = {
    current_week: {
      week_label: 'wk May 13', week_start_date: '2026-05-13', week_start_at: null,
      total_cost_usd: 2,
      rows: [{ key: 'p', bucket_path: 'p', cost_usd: 2, attributed_pct: 100, sessions_count: 1 }],
    },
    trend: { window_weeks: 12, weeks: [], projects: [] },
  };
  return env;
}

function sessRow(over: Partial<SessionRow>): SessionRow {
  return {
    session_id: 's1', started_utc: '2026-05-13T09:00:00Z', duration_min: 12,
    model: 'claude-opus-4-8', project: 'p', project_key: 'p', cost_usd: 1.0, ...over,
  };
}

function sessionsEnvelope(): Envelope {
  const env = baseEnvelope();
  env.sessions = { total: 1, sort_key: 'started_desc', rows: [sessRow({ session_id: 's1' })] };
  return env;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

// [data-panel-kind, Component, envelope builder]
const ALL_TEN: Array<[string, ComponentType, () => Envelope]> = [
  ['forecast', ForecastPanel, baseEnvelope],
  ['trend', TrendPanel, baseEnvelope],
  ['weekly', WeeklyPanel, baseEnvelope],
  ['monthly', MonthlyPanel, baseEnvelope],
  ['projects', ProjectsPanel, projectsEnvelope],
  ['cache-report', CacheReportPanel, baseEnvelope],
  ['alerts', RecentAlertsPanel, baseEnvelope],
  ['sessions', SessionsPanel, sessionsEnvelope],
  ['daily', DailyPanel, baseEnvelope],
  ['blocks', BlocksPanel, baseEnvelope],
];

describe('#293 S4 — all ten grid regions describe, do not impersonate (ACTION-1)', () => {
  it('covers exactly the ten grid panels', () => {
    expect(ALL_TEN.length).toBe(10);
  });

  it.each(ALL_TEN)(
    '%s: region is NOT a tab stop (no tabindex, no region onkeydown) and Expand is a real button',
    (kind, Panel, env) => {
      updateSnapshot(env());
      const { container } = render(<Panel />);
      const section = container.querySelector(`[data-panel-kind="${kind}"]`) as HTMLElement;
      expect(section, `expected a [data-panel-kind="${kind}"] section`).not.toBeNull();
      expect(section.getAttribute('tabindex')).toBeNull();
      const expand = container.querySelector('.panel-expand');
      expect(expand).toBeInstanceOf(HTMLButtonElement);
    },
  );
});

// The panels that keep a guarded region body-click today.
const BODY_CLICK: Array<[string, ComponentType, () => Envelope, string]> = [
  ['forecast', ForecastPanel, baseEnvelope, 'forecast'],
  ['trend', TrendPanel, baseEnvelope, 'trend'],
  ['weekly', WeeklyPanel, baseEnvelope, 'weekly'],
  ['monthly', MonthlyPanel, baseEnvelope, 'monthly'],
  ['projects', ProjectsPanel, projectsEnvelope, 'projects'],
  ['cache-report', CacheReportPanel, baseEnvelope, 'cache-report'],
  ['alerts', RecentAlertsPanel, baseEnvelope, 'alerts'],
];

describe('#293 S4 — body-click panels: bare body opens, grip does not (A11Y-1 guard)', () => {
  it.each(BODY_CLICK)(
    '%s: a bare region-body click still opens the panel modal',
    (kind, Panel, env, modalKind) => {
      updateSnapshot(env());
      const { container } = render(<Panel />);
      const section = container.querySelector(`[data-panel-kind="${kind}"]`) as HTMLElement;
      fireEvent.click(section);
      expect(getState().openModal).toBe(modalKind);
    },
  );

  it.each(BODY_CLICK)(
    '%s: a click on the aria-hidden drag grip does NOT open the panel modal',
    (_kind, Panel, env) => {
      updateSnapshot(env());
      const { container } = render(<Panel />);
      const grip = container.querySelector('.panel-grip') as HTMLElement;
      expect(grip, 'expected a .panel-grip').not.toBeNull();
      fireEvent.click(grip);
      expect(getState().openModal).toBeNull();
    },
  );

  it.each(BODY_CLICK.filter(([kind]) => kind !== 'cache-report' && kind !== 'alerts'))(
    '%s: clicking the ShareIcon opens Share, never the panel modal',
    (_kind, Panel, env) => {
      updateSnapshot(env());
      const { container } = render(<Panel />);
      const share = container.querySelector('.share-icon') as HTMLElement;
      expect(share, 'expected a .share-icon').not.toBeNull();
      fireEvent.click(share);
      expect(getState().shareModal).not.toBeNull();
      expect(getState().openModal).toBeNull();
    },
  );
});

// Panels that are focusable-but-no-body-click today: lose only the tab stop;
// NO region onClick is added (a Sessions region click would clear openSessionId).
describe('#293 S4 — no-body-click panels: not a tab stop, region body opens nothing', () => {
  it.each([
    ['daily', DailyPanel, baseEnvelope],
    ['blocks', BlocksPanel, baseEnvelope],
  ] as Array<[string, ComponentType, () => Envelope]>)(
    '%s: region body click opens nothing',
    (kind, Panel, env) => {
      updateSnapshot(env());
      const { container } = render(<Panel />);
      const section = container.querySelector(`[data-panel-kind="${kind}"]`) as HTMLElement;
      expect(section.getAttribute('tabindex')).toBeNull();
      fireEvent.click(section);
      expect(getState().openModal).toBeNull();
    },
  );

  it('projects UNAVAILABLE branch: not a tab stop, no region onClick', () => {
    updateSnapshot(baseEnvelope()); // projects null → unavailable branch
    const { container } = render(<ProjectsPanel />);
    const section = container.querySelector('[data-panel-kind="projects"]') as HTMLElement;
    expect(section.getAttribute('tabindex')).toBeNull();
    fireEvent.click(section);
    expect(getState().openModal).toBeNull();
  });
});

describe('#293 S4 — Sessions rows keep their sessionId (no region double-dispatch)', () => {
  it('Sessions region is not a tab stop and a bare region click opens nothing', () => {
    updateSnapshot(sessionsEnvelope());
    const { container } = render(<SessionsPanel />);
    const section = container.querySelector('[data-panel-kind="sessions"]') as HTMLElement;
    expect(section.getAttribute('tabindex')).toBeNull();
    fireEvent.click(section);
    expect(getState().openModal).toBeNull();
  });

  it('a row click opens the row sessionId and it is NOT cleared by a second dispatch', () => {
    updateSnapshot(sessionsEnvelope());
    const { container } = render(<SessionsPanel />);
    const tr = container.querySelector('tr.session-row') as HTMLElement;
    expect(tr, 'expected a session row').not.toBeNull();
    fireEvent.click(tr);
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('s1');
  });
});
