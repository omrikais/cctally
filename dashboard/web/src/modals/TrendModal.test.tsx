// TrendModal — TR-3 right second axis (labeled with the UN-padded actual
// used% min/max) + TR-4 per-week hover/focus tooltips. JSDOM can dispatch
// focus and read the rendered label text; the visual positioning + real
// hover are ui-qa checks (#250 S4 · plan Task 7).
import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TrendModal } from './TrendModal';
import { _resetForTests, getState, updateSnapshot } from '../store/store';
import type { Envelope, TrendRow } from '../types/envelope';

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-01T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk Jun 01',
      used_pct: 0,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: {
      tz: 'local',
      resolved_tz: 'Etc/UTC',
      offset_label: 'UTC',
      offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true,
      weekly_thresholds: [],
      five_hour_thresholds: [],
      budget_thresholds: [],
    },
  };
}

// used% spans 10..45; $/1% varies so the primary + secondary scales differ.
function historyFixture(): TrendRow[] {
  return [
    { label: 'W-4', used_pct: 45, dollar_per_pct: 1.2, delta: null, is_current: false },
    { label: 'W-3', used_pct: 30, dollar_per_pct: 1.1, delta: -0.1, is_current: false },
    { label: 'W-2', used_pct: 20, dollar_per_pct: 1.3, delta: 0.2, is_current: false },
    { label: 'W-1', used_pct: 15, dollar_per_pct: 1.05, delta: -0.25, is_current: false },
    { label: 'Now', used_pct: 10, dollar_per_pct: 1.4, delta: 0.35, is_current: true },
  ];
}

function renderTrend(history: TrendRow[]) {
  const env = baseEnvelope();
  env.trend = { weeks: [], spark_heights: [], history };
  updateSnapshot(env);
  return render(<TrendModal />);
}

// 10 rows with varied $/1% so the median label + count assertions are sharp
// and N=10 differs from any hardcoded "12" / "8".
function history10(): TrendRow[] {
  const dpp = [1.0, 1.4, 1.1, 1.6, 1.2, 1.8, 1.3, 1.5, 1.7, 1.9];
  return dpp.map((v, i) => ({
    label: i === dpp.length - 1 ? 'Now' : `W-${dpp.length - 1 - i}`,
    used_pct: 10 + i * 3,
    dollar_per_pct: v,
    delta: i === 0 ? null : v - dpp[i - 1],
    is_current: i === dpp.length - 1,
  }));
}

// TR-1 — every week count (title, section head) derives from rows.length; the
// hardcoded "12-week" contradiction is gone.
describe('<TrendModal /> derives the week count from N (TR-1)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('states the real N in the title + section head, never "12-week"', () => {
    const { container } = renderTrend(history10());
    expect(container.textContent).not.toContain('12-week');
    expect(container.textContent).toContain('10-week history');
    expect(container.textContent).toContain('Trend — last 10 weeks');
  });

  it('renders the empty-state title as bare "Trend" (no misleading count)', () => {
    renderTrend([]);
    expect(document.getElementById('mtr-empty')).not.toBeNull();
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog.textContent).toContain('Trend');
    expect(dialog.textContent).not.toContain('12-week');
    expect(dialog.textContent).not.toContain('0-week');
  });
});

// TR-2 — the chart median reference line states its basis ("10-wk median $X"),
// disambiguating it from the hero KV's "4-week median".
describe('<TrendModal /> median label states its basis (TR-2)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('labels the chart median with its window, not a bare "median"', () => {
    const { container } = renderTrend(history10());
    const med = container.querySelector('.mtr-medlabel') as SVGTextElement;
    expect(med).not.toBeNull();
    expect(med.textContent).toMatch(/^10-wk median \$/);
    expect(med.textContent).not.toMatch(/^median \$/);
  });
});

describe('<TrendModal /> second axis + tooltips (TR-3/TR-4)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('labels the right axis with the actual (un-padded) used% min/max', () => {
    const { container } = renderTrend(historyFixture()); // used% spans 10..45
    const right = [...container.querySelectorAll('.mtr-ylabel-right')].map(
      (e) => e.textContent,
    );
    // The used% domain is padded 0.96/1.04 internally; the right-axis labels
    // undo that padding, so they read the true extremes (45 / 10), NOT the
    // padded domain bounds (46.8 / 9.6).
    expect(right).toContain('45%');
    expect(right).toContain('10%');
    expect(right).not.toContain('47%');
  });

  it('shows a tooltip on focusing a week hit-target', () => {
    const { container } = renderTrend(historyFixture());
    const hit = container.querySelector('.mtr-hit') as HTMLElement;
    expect(hit).not.toBeNull();
    fireEvent.focus(hit);
    expect(container.querySelector('.mtr-tip')).not.toBeNull();
  });

  it('clears the tooltip on blur', () => {
    const { container } = renderTrend(historyFixture());
    const hit = container.querySelector('.mtr-hit') as HTMLElement;
    fireEvent.focus(hit);
    expect(container.querySelector('.mtr-tip')).not.toBeNull();
    fireEvent.blur(hit);
    expect(container.querySelector('.mtr-tip')).toBeNull();
  });

  it('the chart SVG is no longer aria-hidden (keyboard-reachable)', () => {
    const { container } = renderTrend(historyFixture());
    const svg = container.querySelector('#mtr-svg');
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute('aria-hidden')).toBeNull();
  });
});

// S3 (#264) TREND-KPI — fewer than 4 valid non-current weeks → no median →
// collapse the 3-tile hero to a 2-tile [Current $/1%] + informational hint.
function historyFew(): TrendRow[] {
  return [
    { label: 'W-2', used_pct: 20, dollar_per_pct: 1.2, delta: null, is_current: false },
    { label: 'W-1', used_pct: 15, dollar_per_pct: 1.1, delta: -0.1, is_current: false },
    { label: 'Now', used_pct: 10, dollar_per_pct: 1.5, delta: 0.4, is_current: true },
  ];
}

describe('<TrendModal /> median-KPI collapse (TREND-KPI · decision 8)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('collapses to two tiles with an informational hint when <4 non-current weeks', () => {
    const { container } = renderTrend(historyFew()); // 2 non-current → med == null
    expect(container.querySelector('.m-hero.cols-2')).not.toBeNull();
    expect(container.querySelector('.m-hero.cols-3')).toBeNull();
    const tiles = container.querySelectorAll('.m-hero .m-kv');
    expect(tiles.length).toBe(2);
    // Acceptance criterion 4: NO empty "—" KPI tile under 4 weeks — every KV
    // value reads as real content, not a missing dash.
    const values = [...container.querySelectorAll('.m-hero .m-kv .v')].map(
      (v) => (v.textContent ?? '').trim(),
    );
    expect(values).not.toContain('—');
    // The hint reads as informational, not a bare median value.
    expect(container.textContent).toContain('needs 4 weeks');
    // The Current $/1% tile still shows a real number.
    expect(container.querySelector('#mtr-cur')?.textContent).toBe('$1.500');
  });

  it('keeps the three-tile hero when >=4 non-current weeks (median present)', () => {
    const { container } = renderTrend(history10()); // 9 non-current → med != null
    expect(container.querySelectorAll('.m-hero.cols-3 .m-kv').length).toBe(3);
    expect(container.querySelector('.m-hero.cols-2')).toBeNull();
    expect(container.textContent).not.toContain('needs 4 weeks');
  });
});

// S3 (#264) TREND-COLS — sortable Cost column via TREND_COLUMNS +
// SortableHeader, with an EPHEMERAL modal-local sort that never reorders the
// on-board panel; chrono-keyed week labels survive a sort (finding 2).
// costs (10,50,20,40,30) are non-monotonic so a cost-desc sort visibly
// reorders and the current row (30) lands mid-table — proving the label + sort.
function historyWithCost(): TrendRow[] {
  return [
    { label: 'W-4', used_pct: 45, dollar_per_pct: 1.2, delta: null, is_current: false, cost_usd: 10 },
    { label: 'W-3', used_pct: 30, dollar_per_pct: 1.1, delta: -0.1, is_current: false, cost_usd: 50 },
    { label: 'W-2', used_pct: 20, dollar_per_pct: 1.3, delta: 0.2, is_current: false, cost_usd: 20 },
    { label: 'W-1', used_pct: 15, dollar_per_pct: 1.05, delta: -0.25, is_current: false, cost_usd: 40 },
    { label: 'Now', used_pct: 10, dollar_per_pct: 1.4, delta: 0.35, is_current: true, cost_usd: 30 },
  ];
}

describe('<TrendModal /> sortable Cost column (TREND-COLS · decisions 6/7)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('renders a sortable Cost header + a Cost cell per row (fmt.usd2)', () => {
    const { container } = renderTrend(historyWithCost());
    // SortableHeader → five sortable <th> (Week · Cost · Used% · $/1% · Δ).
    const ths = container.querySelectorAll('#mtr-table thead th.th-sortable');
    expect(ths.length).toBe(5);
    const costTh = container.querySelector('#mtr-table thead th[data-col="cost_usd"]');
    expect(costTh).not.toBeNull();
    expect(costTh?.textContent).toContain('Cost');
    // One Cost cell per row, formatted with fmt.usd2; default order is
    // chronological (W-4 first, cost 10).
    const costCells = container.querySelectorAll('#mtr-rows td.c-cost');
    expect(costCells.length).toBe(5);
    expect(costCells[0].textContent).toBe('$10.00');
  });

  it('sorting the modal by Cost does NOT write prefs.trendSortOverride', async () => {
    const user = userEvent.setup();
    const { container } = renderTrend(historyWithCost());
    const before = getState().prefs.trendSortOverride; // panel pref baseline
    const costTh = container.querySelector(
      '#mtr-table thead th[data-col="cost_usd"]',
    ) as HTMLElement;
    await user.click(costTh);
    // Non-vacuous: the click DID sort the modal-local table (max cost first).
    const firstCost = container.querySelector('#mtr-rows tr:first-child td.c-cost');
    expect(firstCost?.textContent).toBe('$50.00');
    // …but the panel's PERSISTED sort is untouched (modal uses local state).
    expect(getState().prefs.trendSortOverride).toEqual(before);
  });

  it('keeps the current row labelled "Now" after sorting by Cost (finding 2)', async () => {
    const user = userEvent.setup();
    const { container } = renderTrend(historyWithCost());
    const costTh = container.querySelector(
      '#mtr-table thead th[data-col="cost_usd"]',
    ) as HTMLElement;
    await user.click(costTh); // cost-desc: 50,40,30,20,10 → current (30) is 3rd
    const curRow = container.querySelector('#mtr-rows tr.cur');
    expect(curRow).not.toBeNull();
    // Chrono-keyed: the current row still reads "Now …", NOT "W−2 …" (which is
    // what the sorted map index would mislabel it as).
    expect(curRow?.querySelector('.wlab')?.textContent ?? '').toMatch(/^Now/);
  });
});
