// #247 S1 — card-chrome contract. The de-colored panels must let their
// header <h2> (and its leading icon) inherit the neutral `.panel-header`
// color instead of pinning a decorative accent inline. (The chrome goes
// neutral; accent is reserved for state/signal — see the design spec
// D1/D4/D5.) JSDOM can read inline `element.style.color`, so a surviving
// `style={{ color: 'var(--accent-*)' }}` shows up as a non-empty string.
import { type ComponentType } from 'react';
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { CurrentWeekPanel } from './CurrentWeekPanel';
import { TrendPanel } from './TrendPanel';
import { ForecastPanel } from './ForecastPanel';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
import { DailyPanel } from './DailyPanel';
import { BlocksPanel } from './BlocksPanel';
import { SessionsPanel } from './SessionsPanel';
import { ProjectsPanel } from './ProjectsPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

// Empty-but-valid envelope — the standard sibling-test mock (mirrors
// TrendPanel.test.tsx / ProjectsPanel.test.tsx). Every de-colored panel
// paints its header against this without bespoke row fixtures.
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

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(baseEnvelope());
});

// All 10 of the #247 S1 de-colored panels. Each renders cleanly against
// the empty-but-valid envelope above, so all 10 are asserted (not just a
// sample). CacheReport is deliberately excluded — it still pins an inline
// accent color on its error-state <h2> and is outside the S1 de-color set.
const PANELS: Array<[string, ComponentType]> = [
  ['Current Week', CurrentWeekPanel],
  ['Trend', TrendPanel],
  ['Forecast', ForecastPanel],
  ['Weekly', WeeklyPanel],
  ['Monthly', MonthlyPanel],
  ['Daily', DailyPanel],
  ['Blocks', BlocksPanel],
  ['Sessions', SessionsPanel],
  ['Recent Alerts', RecentAlertsPanel],
  ['Projects', ProjectsPanel],
];

describe('#247 S1 card-chrome contract', () => {
  it.each(PANELS)('header <h2> carries no inline decorative accent color (%s)', (_label, Panel) => {
    const { container } = render(<Panel />);
    const h2 = container.querySelector('.panel-header h2');
    expect(h2, 'expected a .panel-header h2').not.toBeNull();
    expect((h2 as HTMLElement).style.color).toBe('');
  });
  it.each(PANELS)('header icon carries no inline decorative accent color (%s)', (_label, Panel) => {
    const { container } = render(<Panel />);
    const icon = container.querySelector('.panel-header svg.icon');
    expect(icon, 'expected a .panel-header svg.icon').not.toBeNull();
    expect((icon as unknown as SVGElement & { style: CSSStyleDeclaration }).style.color).toBe('');
  });
});
