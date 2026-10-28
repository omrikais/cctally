// #247 S1 — card-chrome contract: every grid panel header carries the
// affordances its capabilities call for (share, collapse, expand).
import { type ComponentType } from 'react';
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { TrendPanel } from './TrendPanel';
import { ForecastPanel } from './ForecastPanel';
import { DailyPanel } from './DailyPanel';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
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

describe('#452 Cache Report header icon contract', () => {
  it('hides the decorative header icon from assistive technology', () => {
    // The heading text names the card; the icon beside it is decoration, so a
    // screen reader must not announce it as an unlabeled graphic.
    const { container } = render(<CacheReportPanel />);
    const icon = container.querySelector('.panel-header svg.icon');
    expect(icon).not.toBeNull();
    expect(icon?.getAttribute('aria-hidden')).toBe('true');
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
//     bin/_lib_share_templates.py): trend, daily, weekly, monthly, blocks,
//     forecast, sessions, projects (8 grid panels post-S2). NOT
//     alerts/cache-report. (current-week is share-capable too but #248 removed
//     it from the grid — it shares via the HeroStrip / Current Week modal.
//     #264 S2 split the S8 "History" heatmap back into Daily/Weekly/Monthly.)
//   • collapse toggle (`.panel-collapse-toggle`) is present IFF the panel has
//     a `*Collapsed` pref in store/store.ts: sessions, blocks, daily
//     (dailyCollapsed), alerts (4). Weekly/Monthly have no collapse pref.
//   • expand button (`.panel-expand`, from components/ExpandButton.tsx) is
//     present on ALL 10 grid panels (#264 S1 AFFORD-1) — the consistent
//     "open this card's detail modal" affordance, unconditional.
//
// The expected booleans below are the ground truth DERIVED from those
// sources — if a panel's rendered affordances stop matching this grammar,
// that is a real spec violation to surface, not a row to edit.
const AFFORDANCE_GRAMMAR: Array<[string, ComponentType, boolean, boolean, boolean]> = [
  // [panel id, Component, shareable, collapsible, expandable]
  ['trend', TrendPanel, true, false, true],
  ['daily', DailyPanel, true, true, true],
  ['weekly', WeeklyPanel, true, false, true],
  ['monthly', MonthlyPanel, true, false, true],
  ['blocks', BlocksPanel, true, true, true],
  ['forecast', ForecastPanel, true, false, true],
  ['sessions', SessionsPanel, true, true, true],
  ['projects', ProjectsPanel, true, false, true],
  ['alerts', RecentAlertsPanel, false, true, true],
  ['cache-report', CacheReportPanel, false, false, true],
];

describe('#247 S1 uniform header affordance grammar (acceptance #6 — 10 grid panels)', () => {
  // The base envelope (no cache_report field) renders CacheReportPanel in its
  // loading branch, which — like every cache-report state — carries neither a
  // share icon nor a collapse toggle, so [false, false] holds without a
  // bespoke fixture. The #264 expand button is present in every branch.
  it.each(AFFORDANCE_GRAMMAR)(
    '%s: share icon iff share-capable, collapse toggle iff collapsible, expand button always',
    (_panelId, Panel, shareable, collapsible, expandable) => {
      const { container } = render(<Panel />);
      expect(
        container.querySelector('.share-icon') !== null,
        `expected share icon presence === ${shareable}`,
      ).toBe(shareable);
      expect(
        container.querySelector('.panel-collapse-toggle') !== null,
        `expected collapse toggle presence === ${collapsible}`,
      ).toBe(collapsible);
      expect(
        container.querySelector('.panel-expand') !== null,
        `expected expand button presence === ${expandable}`,
      ).toBe(expandable);
    },
  );
});
