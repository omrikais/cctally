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
import { CacheReportPanel } from './CacheReportPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope, FreshnessLabel } from '../types/envelope';

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

// #247 S1 (C5) — the Current Week freshness reading must be a SINGLE surface:
// one header chip and NO duplicate "Last snapshot" foot line. `fresh` now
// renders a quiet neutral chip (it used to render nothing, leaving the foot as
// the only fresh signal); `stale` keeps the ⚠ glyph. A populated current_week
// fixture exercises the chip path (the base envelope's current_week is null).
function envelopeWithFreshness(label: FreshnessLabel): Envelope {
  const env = baseEnvelope();
  env.current_week = {
    used_pct: 42,
    five_hour_pct: null,
    five_hour_resets_in_sec: null,
    spent_usd: 12.5,
    dollar_per_pct: 0.3,
    reset_at_utc: '2026-05-20T00:00:00Z',
    reset_in_sec: 100000,
    last_snapshot_age_sec: 30,
    milestones: [],
    freshness: { label, captured_at: '2026-05-13T09:59:30Z', age_seconds: 30 },
    five_hour_block: null,
  };
  return env;
}

describe('#247 S1 freshness single-source (C5)', () => {
  it('shows exactly one freshness surface and no "Last snapshot" foot', () => {
    updateSnapshot(envelopeWithFreshness('fresh'));
    const { container } = render(<CurrentWeekPanel />);
    // Robust foot check: assert on the `.panel-foot.cw-foot` element and on the
    // subtree text directly. Both unambiguously detect the foot when it exists
    // (so they stay non-vacuous) and are strictly stronger than a queryByText
    // probe of the split "Last snapshot:" label.
    expect(container.querySelector('.panel-foot.cw-foot')).toBeNull();
    expect(container.textContent).not.toMatch(/Last snapshot:/);
    expect(container.querySelectorAll('[data-freshness]').length).toBe(1);
  });
  it('fresh state renders a quiet neutral chip (not hidden)', () => {
    updateSnapshot(envelopeWithFreshness('fresh'));
    const { container } = render(<CurrentWeekPanel />);
    const chip = container.querySelector('[data-freshness="fresh"]');
    expect(chip, 'fresh now renders a chip').not.toBeNull();
    expect(chip?.className).toContain('chip-fresh');
  });
  it('stale state carries the ⚠ glyph', () => {
    updateSnapshot(envelopeWithFreshness('stale'));
    const { container } = render(<CurrentWeekPanel />);
    const chip = container.querySelector('[data-freshness="stale"]');
    expect(chip, 'stale renders a chip').not.toBeNull();
    expect(chip?.textContent).toContain('⚠');
  });
});

// #247 S1 (spec acceptance #6) — uniform header affordance grammar across
// ALL 11 dashboard panels. The S1 facelift gave every panel header the same
// chrome shape; the two header affordances are NOT decorative — each is
// present iff the panel has the underlying capability:
//
//   • share icon (`.share-icon`, from components/ShareIcon.tsx) is present
//     IFF the panel id is in the share-capable set (`SharePanelId` in
//     share/types.ts, mirrored by `SHARE_CAPABLE_PANELS` in
//     bin/_lib_share_templates.py): current-week, trend, weekly, daily,
//     monthly, blocks, forecast, sessions, projects (9). NOT alerts/cache-report.
//   • collapse toggle (`.panel-collapse-toggle`) is present IFF the panel has
//     a `*Collapsed` pref in store/store.ts: sessions, blocks, daily, alerts (4).
//
// The expected booleans below are the ground truth DERIVED from those two
// sources — if a panel's rendered affordances stop matching this grammar,
// that is a real spec violation to surface, not a row to edit.
const AFFORDANCE_GRAMMAR: Array<[string, ComponentType, boolean, boolean]> = [
  // [panel id, Component, shareable, collapsible]
  ['current-week', CurrentWeekPanel, true, false],
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

describe('#247 S1 uniform header affordance grammar (acceptance #6 — 11 panels)', () => {
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

  // Guards the table itself: exactly 9 share-capable and 4 collapsible panels
  // (the audit's "share on 9/11, collapse on 4/11"). A drift in either count
  // means a panel silently gained/lost an affordance.
  it('covers all 11 panels with 9 share-capable and 4 collapsible', () => {
    expect(AFFORDANCE_GRAMMAR.length).toBe(11);
    expect(AFFORDANCE_GRAMMAR.filter(([, , s]) => s).length).toBe(9);
    expect(AFFORDANCE_GRAMMAR.filter(([, , , c]) => c).length).toBe(4);
  });
});
