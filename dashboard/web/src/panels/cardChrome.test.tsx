// #247 S1 — card-chrome contract. The de-colored panels must let their
// header <h2> (and its leading icon) inherit the neutral `.panel-header`
// color instead of pinning a decorative accent inline. (The chrome goes
// neutral; accent is reserved for state/signal — see the design spec
// D1/D4/D5.) JSDOM can read inline `element.style.color`, so a surviving
// `style={{ color: 'var(--accent-*)' }}` shows up as a non-empty string.
import { type ComponentType } from 'react';
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { TrendPanel } from './TrendPanel';
import { ForecastPanel } from './ForecastPanel';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
import { DailyPanel } from './DailyPanel';
import { BlocksPanel } from './BlocksPanel';
import { SessionsPanel } from './SessionsPanel';
import { ProjectsPanel } from './ProjectsPanel';
import { CacheReportPanel } from './CacheReportPanel';
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

// The #247 S1 de-colored grid panels. Each renders cleanly against the
// empty-but-valid envelope above, so all are asserted (not just a sample).
// CacheReport is deliberately excluded — it still pins an inline accent color
// on its error-state <h2> and is outside the S1 de-color set. #248 — Current
// Week left the grid (it is the HeroStrip now), so it is no longer in this set.
const PANELS: Array<[string, ComponentType]> = [
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

// #247 S1 (spec acceptance #6) — uniform header affordance grammar across
// the dashboard GRID panels. The S1 facelift gave every panel header the same
// chrome shape; the two header affordances are NOT decorative — each is
// present iff the panel has the underlying capability:
//
//   • share icon (`.share-icon`, from components/ShareIcon.tsx) is present
//     IFF the panel id is in the share-capable set (`SharePanelId` in
//     share/types.ts, mirrored by `SHARE_CAPABLE_PANELS` in
//     bin/_lib_share_templates.py): trend, weekly, daily, monthly, blocks,
//     forecast, sessions, projects (8 grid panels). NOT alerts/cache-report.
//     (current-week is share-capable too but #248 removed it from the grid —
//     it shares via the HeroStrip / Current Week modal.)
//   • collapse toggle (`.panel-collapse-toggle`) is present IFF the panel has
//     a `*Collapsed` pref in store/store.ts: sessions, blocks, daily, alerts (4).
//
// The expected booleans below are the ground truth DERIVED from those two
// sources — if a panel's rendered affordances stop matching this grammar,
// that is a real spec violation to surface, not a row to edit.
const AFFORDANCE_GRAMMAR: Array<[string, ComponentType, boolean, boolean]> = [
  // [panel id, Component, shareable, collapsible]
  ['trend', TrendPanel, true, false],
  ['weekly', WeeklyPanel, true, false],
  ['daily', DailyPanel, true, true],
  ['monthly', MonthlyPanel, true, false],
  ['blocks', BlocksPanel, true, true],
  ['forecast', ForecastPanel, true, false],
  ['sessions', SessionsPanel, true, true],
  ['projects', ProjectsPanel, true, false],
  ['alerts', RecentAlertsPanel, false, true],
  ['cache-report', CacheReportPanel, false, false],
];

describe('#247 S1 uniform header affordance grammar (acceptance #6 — 10 grid panels)', () => {
  // The base envelope (no cache_report field) renders CacheReportPanel in its
  // loading branch, which — like every cache-report state — carries neither a
  // share icon nor a collapse toggle, so [false, false] holds without a
  // bespoke fixture.
  it.each(AFFORDANCE_GRAMMAR)(
    '%s: share icon iff share-capable, collapse toggle iff collapsible',
    (_panelId, Panel, shareable, collapsible) => {
      const { container } = render(<Panel />);
      expect(
        container.querySelector('.share-icon') !== null,
        `expected share icon presence === ${shareable}`,
      ).toBe(shareable);
      expect(
        container.querySelector('.panel-collapse-toggle') !== null,
        `expected collapse toggle presence === ${collapsible}`,
      ).toBe(collapsible);
    },
  );

  // Guards the table itself: exactly 8 share-capable and 4 collapsible GRID
  // panels (#248 removed current-week from the grid: share-capable went 9→8,
  // total 11→10). A drift in either count means a panel silently gained/lost
  // an affordance.
  it('covers all 10 grid panels with 8 share-capable and 4 collapsible', () => {
    expect(AFFORDANCE_GRAMMAR.length).toBe(10);
    expect(AFFORDANCE_GRAMMAR.filter(([, , s]) => s).length).toBe(8);
    expect(AFFORDANCE_GRAMMAR.filter(([, , , c]) => c).length).toBe(4);
  });
});
