// CacheReportPanel — anomaly-watchdog panel for the dashboard.
// Spec 2026-05-21 §2. State coverage: healthy, anomalous,
// insufficient-baseline, empty, click-to-open dispatch.
import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { CacheReportPanel } from './CacheReportPanel';
import envelopeFixture from '../../__tests__/fixtures/envelope.json';
import {
  ACCOUNT_A,
  ACCOUNT_EMPTY,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../store/store';
import type {
  CacheReportEnvelope,
  CodexSourceData,
  Envelope,
} from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-20T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk May 20', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null,
      forecast_verdict: 'ok', vs_last_week_delta: null,
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
      tz: 'local', resolved_tz: 'Etc/UTC',
      offset_label: 'UTC', offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [],
    },
  };
}

function healthyCacheReport(): CacheReportEnvelope {
  const days = Array.from({ length: 14 }).map((_, i) => ({
    date: `2026-05-${String(i + 7).padStart(2, '0')}`,
    cache_hit_percent: 67 + (i % 3),
    input_tokens: 500, output_tokens: 100,
    cache_creation_tokens: 200, cache_read_tokens: 2000,
    saved_usd: 0.8, wasted_usd: 0.1, net_usd: 0.7,
    anomaly_triggered: false, anomaly_reasons: [],
  }));
  return {
    window_days: 14,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: '2026-05-20',
      cache_hit_percent: 68,
      baseline_median_percent: 67,
      delta_pp: -1,  // slightly below; not anomalous
      net_usd: 1.20, saved_usd: 1.30, wasted_usd: 0.10,
      anomaly_triggered: false, anomaly_reasons: [],
      baseline_daily_row_count: 13,
    },
    days, by_project: [], by_model: [],
    seven_day_net_usd: 5.94,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 12.34,
    fourteen_day_efficiency_ratio: 0.92,
    is_empty: false,
  };
}

function envelopeWith(cr: CacheReportEnvelope): Envelope {
  const env = baseEnvelope();
  env.cache_report = cr;
  return env;
}

function anomalousCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      cache_hit_percent: 49,
      baseline_median_percent: 67,
      delta_pp: 18,         // 18 pp below median
      net_usd: -0.42,
      saved_usd: 0.36,
      wasted_usd: 0.78,
      anomaly_triggered: true,
      anomaly_reasons: ['cache_drop', 'net_negative'],
    },
    seven_day_anomaly_count: 2,
  };
}

function insufficientBaselineCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      baseline_median_percent: null,
      delta_pp: null,
      baseline_daily_row_count: 3,
    },
    days: base.days.slice(0, 3),
  };
}

function emptyCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: { ...base.today, cache_hit_percent: 0 },
    days: [],
    by_project: [],
    by_model: [],
    seven_day_net_usd: 0,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 0,
    fourteen_day_efficiency_ratio: 0,
    is_empty: true,
  };
}

describe('<CacheReportPanel /> healthy state', () => {
  it('renders healthy state with teal accent and the check glyph', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-teal');
    expect(screen.getByText('✓')).toBeInTheDocument();
    // Cache hit text — match the 68% number specifically.
    expect(screen.getByText(/68%/)).toBeInTheDocument();
  });

  it('renders 14 sparkline points when 14 days are present', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const polyline = document.querySelector('.cr-spark polyline');
    expect(polyline).toBeTruthy();
    const pts = polyline?.getAttribute('points') ?? '';
    expect(pts.split(' ').length).toBe(14);
  });

  it('panel click dispatches OPEN_MODAL with kind cache-report', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    fireEvent.click(panel);
    expect(getState().openModal).toBe('cache-report');
  });
});

describe('<CacheReportPanel /> anomalous state', () => {
  it('renders anomalous state with amber accent and the warning glyph', () => {
    updateSnapshot(envelopeWith(anomalousCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-amber');
    expect(screen.queryByText('✓')).toBeNull();
    expect(screen.getByText('⚠')).toBeInTheDocument();
    // cache_drop wins over net_negative when both fire: headline reads
    // "Today: cache hit ↓ 18pp" (delta floored, abs).
    expect(screen.getByText(/↓ 18pp/)).toBeInTheDocument();
    // Second subline is the 14d-net summary (issue #77 Round 2);
    // the prior "N ⚠ days" token now lives only in the modal's daily
    // table and spotlight.
    expect(screen.getByText(/14d net:/i)).toBeInTheDocument();
  });

  it('renders an amber today-marker on the sparkline when anomalous', () => {
    updateSnapshot(envelopeWith(anomalousCacheReport()));
    render(<CacheReportPanel />);
    const marker = screen.getByTestId('cr-spark-today-marker');
    // Color comes from the panel via the today_marker_color prop.
    expect(marker.getAttribute('fill')).toBe('var(--accent-amber)');
  });
});

describe('<CacheReportPanel /> insufficient-baseline state', () => {
  it('renders the ~ glyph and "Building baseline N/5 days" headline', () => {
    updateSnapshot(envelopeWith(insufficientBaselineCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    // Stays teal — insufficient baseline is not an anomaly.
    expect(panel).toHaveClass('accent-teal');
    expect(screen.getByText('~')).toBeInTheDocument();
    expect(screen.getByText(/Building baseline · 3\/5 days/i)).toBeInTheDocument();
    // Sparkline omitted in insufficient-baseline state.
    expect(document.querySelector('.cr-spark')).toBeNull();
  });

  it('keeps panel chrome teal even when today.anomaly_triggered is true while baseline samples are thin (round-2 finding)', () => {
    // First 1-4 captured days: the server-side classifier can fire
    // `net_negative` without a baseline, so `anomaly_triggered` arrives
    // true while baseline_daily_row_count is still below the 5-day
    // floor. The panel must keep accent-teal / "Building baseline" copy
    // and NOT flip the border, header text color, or the "⚠ Today"
    // badge — those would render a false warning before the watchdog is
    // actually live and contradict the body copy below.
    const cr = insufficientBaselineCacheReport();
    cr.today = {
      ...cr.today,
      anomaly_triggered: true,
      anomaly_reasons: ['net_negative'],
      net_usd: -0.42,
    };
    updateSnapshot(envelopeWith(cr));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-teal');
    expect(panel).not.toHaveClass('accent-amber');
    // Headline still reads "Building baseline", not the anomalous copy.
    expect(screen.getByText(/Building baseline · 3\/5 days/i)).toBeInTheDocument();
    // No "⚠ Today" header badge — that's part of the amber chrome.
    expect(screen.queryByText(/⚠ Today/i)).toBeNull();
  });
});

describe('<CacheReportPanel /> empty state', () => {
  it('renders the − glyph and "No Claude activity yet" headline when is_empty', () => {
    updateSnapshot(envelopeWith(emptyCacheReport()));
    render(<CacheReportPanel />);
    expect(screen.getByText('−')).toBeInTheDocument();
    expect(screen.getByText(/No Claude activity yet/i)).toBeInTheDocument();
    // Sparkline omitted in empty state.
    expect(document.querySelector('.cr-spark')).toBeNull();
  });

  it('uses quiet empty chrome that stays distinct from a failed build', () => {
    updateSnapshot(envelopeWith(emptyCacheReport()));
    const { container } = render(<CacheReportPanel />);
    const glyph = container.querySelector('.cr-glyph')!;
    expect(glyph.textContent).toBe('−');
    expect(glyph.classList.contains('empty')).toBe(true);
    expect(container.querySelector('.panel')?.getAttribute('aria-label'))
      .toBe('Cache Report · empty');
  });
});

describe('<CacheReportPanel /> loading state', () => {
  it('renders loading placeholder before first sync', () => {
    // No snapshot ingested — env?.cache_report is undefined. The panel
    // still mounts so panelOrder/drag-and-drop has a real DOM target,
    // and the click is wired so the modal can still open.
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    expect(panel).toHaveClass('accent-teal');
    expect(screen.getByText(/\(loading\)/i)).toBeInTheDocument();
    // Sparkline omitted before first sync.
    expect(document.querySelector('.cr-spark')).toBeNull();
    // Clicking the loading placeholder still opens the modal.
    fireEvent.click(panel);
    expect(getState().openModal).toBe('cache-report');
  });
});

// ---- Issue #77 Round 2 (P2-4): mini net-bars + 14d-net subline ----

describe('<CacheReportPanel /> mini net-bars (issue #77 P2-4)', () => {
  it('renders 14 mini net-bars under the sparkline on a healthy day', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const bars = document.querySelectorAll('[data-testid="crm-netbar-mini"]');
    expect(bars.length).toBe(14);
    // All-positive fixture days => all bars carry sign='pos'.
    bars.forEach((b) => expect(b.getAttribute('data-sign')).toBe('pos'));
  });

  it('renders mini net-bars on an anomalous day too (today is amber)', () => {
    updateSnapshot(envelopeWith(anomalousCacheReport()));
    render(<CacheReportPanel />);
    const bars = document.querySelectorAll('[data-testid="crm-netbar-mini"]');
    expect(bars.length).toBe(14);
    // The fixture still produces all-positive days[] (today.net_usd is
    // a separate top-level field that doesn't replace days[13]); we
    // assert presence here, not per-bar coloring.
  });

  it('omits mini net-bars in the insufficient-baseline state', () => {
    updateSnapshot(envelopeWith(insufficientBaselineCacheReport()));
    render(<CacheReportPanel />);
    expect(
      document.querySelectorAll('[data-testid="crm-netbar-mini"]').length,
    ).toBe(0);
  });

  it('omits mini net-bars in the empty state', () => {
    updateSnapshot(envelopeWith(emptyCacheReport()));
    render(<CacheReportPanel />);
    expect(
      document.querySelectorAll('[data-testid="crm-netbar-mini"]').length,
    ).toBe(0);
  });
});

// #293 S4 (A11Y-1/ACTION-1): the card region is now DESCRIBE-only — no tab
// stop, no region keydown. The M2 regression's intent (every state must be
// keyboard-openable) now rides the unconditional Expand button, which is a real
// <button> present in every branch (loading / empty / healthy). The region
// itself no longer activates on Enter/Space.
describe('<CacheReportPanel /> keyboard access via Expand in every state (M2 → #293 S4)', () => {
  function expandBtn() {
    return screen.getByRole('button', { name: /open cache report/i });
  }

  it('healthy state: Expand opens the modal on keyboard activation', async () => {
    const user = userEvent.setup();
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    expandBtn().focus();
    await user.keyboard('{Enter}');
    expect(getState().openModal).toBe('cache-report');
  });

  it('loading state: Expand opens the modal on keyboard activation', async () => {
    // No snapshot → env.cache_report undefined → no-data branch.
    const user = userEvent.setup();
    render(<CacheReportPanel />);
    expandBtn().focus();
    await user.keyboard(' ');
    expect(getState().openModal).toBe('cache-report');
  });

  it('empty state: Expand opens the modal on keyboard activation', async () => {
    const user = userEvent.setup();
    updateSnapshot(envelopeWith(emptyCacheReport()));
    render(<CacheReportPanel />);
    expandBtn().focus();
    await user.keyboard('{Enter}');
    expect(getState().openModal).toBe('cache-report');
  });

  it('the region is describe-only: Enter on the section does NOT open (no double-fire)', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    const panel = screen.getByRole('region', { name: /cache report/i });
    fireEvent.keyDown(panel, { key: 'Enter' });
    expect(getState().openModal).toBeNull();
  });
});

describe('<CacheReportPanel /> 14d-net subline (issue #77 P2-4)', () => {
  it('reads "14d net: +$9.80" on the healthy fixture (sum of 14 × 0.70)', () => {
    updateSnapshot(envelopeWith(healthyCacheReport()));
    render(<CacheReportPanel />);
    // Use within-subline scoping so we don't accidentally match a stray
    // "14d" in the headline.
    const subline = document.querySelector('.cr-subline.second');
    expect(subline).toBeTruthy();
    expect(subline?.textContent).toMatch(/14d net:\s*\+\$9\.80/);
    // Positive-net class is applied so the dollar amount renders green.
    const amount = subline?.querySelector('span');
    expect(amount?.className).toBe('ok');
  });

  it('keeps the "Watchdog activates at 5 days" copy in insufficient-baseline', () => {
    updateSnapshot(envelopeWith(insufficientBaselineCacheReport()));
    render(<CacheReportPanel />);
    expect(
      screen.getByText(/Watchdog activates at 5 days of history/i),
    ).toBeInTheDocument();
    // The 14d-net subline is suppressed in this state — only the
    // watchdog hint shows up in .cr-subline.second.
    const subline = document.querySelector('.cr-subline.second');
    expect(subline?.textContent).not.toMatch(/14d net:/);
  });
});

// ---------------------------------------------------------------------------
// #443 S1 — branch order, provider status, and the all-mode summary.
// ---------------------------------------------------------------------------

function unobservedTodayCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      cache_hit_percent: 0,
      net_usd: 0, saved_usd: 0, wasted_usd: 0,
      anomaly_triggered: false, anomaly_reasons: [],
      anomaly_unevaluated: ['net_negative', 'cache_drop'],
      observed: false,
    },
    days: [
      {
        ...base.days[0], date: '2026-05-21', cache_hit_percent: 0,
        input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0,
        cache_read_tokens: 0, saved_usd: 0, wasted_usd: 0, net_usd: 0,
        anomaly_unevaluated: ['net_negative', 'cache_drop'], observed: false,
      },
      ...base.days,
    ],
  };
}

function thinBaselineUnobservedCacheReport(): CacheReportEnvelope {
  const base = unobservedTodayCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      baseline_median_percent: null, delta_pp: null,
      baseline_daily_row_count: 3,
    },
  };
}

function thinBaselineNetNegativeCacheReport(): CacheReportEnvelope {
  const base = healthyCacheReport();
  return {
    ...base,
    today: {
      ...base.today,
      baseline_median_percent: null, delta_pp: null,
      baseline_daily_row_count: 3,
      net_usd: -0.42, saved_usd: 0.1, wasted_usd: 0.52,
      anomaly_triggered: true, anomaly_reasons: ['net_negative'],
      anomaly_unevaluated: ['cache_drop'],
    },
    days: base.days.slice(0, 3),
  };
}

function withCodexSection(cr: CacheReportEnvelope): Envelope {
  // Minimal source bundle so `all` mode composes two sections.
  const e = envelopeWith(cr);
  e.source_schema_version = 2;
  e.default_source = 'claude';
  e.source_order = ['claude', 'codex', 'all'];
  e.sources = {
    claude: {
      source: 'claude', availability: 'ok', freshness: 'fresh',
      warnings: [], data_version: 'v1', last_success_at: null,
      capabilities: {}, data: {},
      domain_freshness: { hero: 'fresh', quota: 'fresh', sessions: 'fresh' },
    },
    codex: {
      source: 'codex', availability: 'ok', freshness: 'fresh',
      warnings: [], data_version: 'v1', last_success_at: null,
      capabilities: {}, data: { cache_report: null },
      domain_freshness: { hero: 'fresh', quota: 'fresh', sessions: 'fresh' },
    },
    all: {
      source: 'all', availability: 'ok', freshness: 'fresh',
      warnings: [], data_version: 'v1', last_success_at: null,
      capabilities: {}, data: {},
      domain_freshness: { hero: 'fresh', quota: 'fresh', sessions: 'fresh' },
    },
  } as unknown as Envelope['sources'];
  return e;
}

function degradedClaudeEnvelope(cr = healthyCacheReport()): Envelope {
  const e = withCodexSection(cr);
  e.sources!.claude.freshness = 'stale';
  delete (e.sources!.claude as { domain_freshness?: unknown }).domain_freshness;
  return e;
}

describe('<CacheReportPanel /> #443 S1 hydrating vs failed', () => {
  it('keeps a populated report visible while hydrating', () => {
    // types/envelope.ts:104 — a populated-but-incomplete panel shows its data.
    const e = envelopeWith(healthyCacheReport());
    e.hydrating = true;
    updateSnapshot(e);
    render(<CacheReportPanel />);
    expect(screen.getByText(/cache hit/i)).toBeInTheDocument();
    expect(document.querySelector('.panel-skeleton')).toBeNull();
  });

  it('renders a skeleton only when hydrating with no value', () => {
    const e = baseEnvelope();
    e.hydrating = true;
    updateSnapshot(e);
    render(<CacheReportPanel />);
    expect(document.querySelector('.panel-skeleton')).not.toBeNull();
  });

  it('states the failure when a null report is not hydrating', () => {
    const e = baseEnvelope();
    e.hydrating = false;
    updateSnapshot(e);
    render(<CacheReportPanel />);
    expect(screen.getByText(/could not be built/i)).toBeInTheDocument();
    expect(screen.queryByText(/\(loading\)/)).toBeNull();
  });

  it('uses warning failure chrome and an accessible failed-state label', () => {
    const e = baseEnvelope();
    e.hydrating = false;
    updateSnapshot(e);
    const { container } = render(<CacheReportPanel />);
    const glyph = container.querySelector('.cr-glyph')!;
    expect(glyph.textContent).toBe('!');
    expect(glyph.classList.contains('fail')).toBe(true);
    expect(container.querySelector('.panel')?.classList.contains('accent-amber')).toBe(true);
    expect(container.querySelector('.panel')?.getAttribute('aria-label'))
      .toBe('Cache Report · failed');
  });
});

describe('<CacheReportPanel /> #443 S1 provider status in single-source view', () => {
  it('shows the provider status chip in single-source view when degraded', () => {
    updateSnapshot(degradedClaudeEnvelope());
    render(<CacheReportPanel />);
    expect(document.querySelector('.provider-section-status')!.textContent).toBe('degraded');
    expect(screen.getByText(/cache hit/i)).toBeInTheDocument();   // verdict retained
    expect(document.querySelector('.source-chip')).toBeNull();     // panel stays bare
  });

  it('renders the empty body with a chip for a degraded empty source', () => {
    updateSnapshot(degradedClaudeEnvelope(emptyCacheReport()));
    render(<CacheReportPanel />);
    expect(screen.getByText(/No Claude activity yet/)).toBeInTheDocument();
    expect(document.querySelector('.provider-section-status')).not.toBeNull();
    expect(screen.queryByText(/cache hit/i)).toBeNull();
  });
});

describe('<CacheReportPanel /> #443 S1 all-mode summary', () => {
  // #556 S4 F8 — see WeeklyPanel.test.tsx for the rule this asserts. This
  // surface additionally needs `role="region"`, because its section container
  // is a plain `<div>`: an `aria-label` or `aria-labelledby` on a generic
  // element names nothing, so without the role the heading would resolve to an
  // element with no landmark to name.
  it('names each provider section by its own heading and exposes a region (#556 S4)', () => {
    updateSnapshot(withCodexSection(thinBaselineNetNegativeCacheReport()));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<CacheReportPanel />);
    const sections = container.querySelectorAll('[data-provider-section]');
    expect(sections.length).toBe(2);
    sections.forEach((s) => {
      expect(s.getAttribute('role')).toBe('region');
      const labelledBy = s.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(s.getAttribute('aria-label')).toBeNull();
      const heading = container.querySelector(`[id="${labelledBy}"]`);
      expect(heading).not.toBeNull();
      expect(heading!.tagName).toBe('H3');
      expect(heading!.textContent).toMatch(/^(Claude|Codex) cache report$/);
    });
    const ids = [...container.querySelectorAll('h3[id]')].map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('applies the insufficient gate in the all-mode summary', () => {
    // Thin baseline + net_negative: the panel says Building baseline, so the
    // all-mode summary must not say anomaly for the same data (#443 F6).
    updateSnapshot(withCodexSection(thinBaselineNetNegativeCacheReport()));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    const claude = document.querySelector('[data-provider-section="claude"]')!;
    expect(claude.textContent).not.toContain('⚠ anomaly');
    expect(claude.textContent).toContain('Building baseline');
  });

  it('does not print a measured zero in the all-mode summary KPI', () => {
    updateSnapshot(withCodexSection(unobservedTodayCacheReport()));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    const claude = document.querySelector('[data-provider-section="claude"]')!;
    expect(claude.textContent).not.toContain('Cache hit0%');
    expect(claude.textContent).toContain('no activity today');
  });
});

describe('<CacheReportPanel /> #443 S2 the Codex compatibility fallback is gone', () => {
  // S1 could only suppress the chip here, because `cr` was the `adapted`
  // fabrication rather than the section's value and labelling a full verdict
  // "Codex cache report is unavailable." would have contradicted the screen.
  // S2 deletes the fabrication instead: the panel renders the section's own
  // value, so the chip describes exactly what is on screen and the
  // rendersSectionValue guard has nothing left to guard.
  it('states the unavailability instead of fabricating a verdict from daily rows', () => {
    const e = {
      ...(structuredClone(envelopeFixture) as unknown as Envelope),
      ...makeSourceEnvelope(),
    };
    e.sources!.codex.data!.cache_report = null;
    updateSnapshot(e);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<CacheReportPanel />);
    expect(document.querySelector('[data-source="codex"]')).not.toBeNull();
    // No invented report: no sparkline, no median, no verdict.
    expect(document.querySelector('.cr-spark')).toBeNull();
    expect(screen.getByText(/Cache Report unavailable/i)).toBeInTheDocument();
    // … and the chip and reason are now unconditional, because the rendered
    // object IS the section's value.
    expect(document.querySelector('.provider-section-status')!.textContent)
      .toBe('unavailable');
    expect(screen.getByText(/Codex cache report is unavailable/i))
      .toBeInTheDocument();
  });

  it('reads an available-but-empty Codex source as empty, never as a build failure', () => {
    // dashboardPresentation nulls section.value for an available-and-empty
    // report (S1's F7 fix), so emptiness reaches the panel as a STATUS on a
    // null value. Routing `cr` through the section without honouring that
    // would tell an idle user the snapshot could not be built.
    const e = {
      ...(structuredClone(envelopeFixture) as unknown as Envelope),
      ...makeSourceEnvelope(),
    };
    e.sources!.codex.data!.cache_report = {
      ...emptyCacheReport(), is_empty: true, days: [],
    };
    updateSnapshot(e);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<CacheReportPanel />);
    expect(screen.getByText(/No Codex activity yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be built/i)).toBeNull();
  });

  it('reads a focused account with no Codex child as empty, not as a build failure', () => {
    // accountScope.ts synthesizes the child with is_empty: true and
    // cache_report: null, and leaves the ENTRY's availability at 'ok' — so the
    // section reads `unavailable`, not `empty`. This is an ordinary path, not
    // a stale envelope, and it must not accuse the snapshot of failing.
    const slice = makeSourceEnvelope() as unknown as {
      sources: { codex: { data: CodexSourceData } };
    };
    slice.sources.codex.data = makeDecoratedCodexSourceData();
    updateSnapshot(slice as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_EMPTY });
    render(<CacheReportPanel />);
    expect(screen.getByText(/No Codex activity yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be built/i)).toBeNull();
  });

  it('still calls a populated account with a missing report a build failure', () => {
    // Non-vacuity for the case above: the account-empty escape must not
    // swallow a genuinely absent report for an account that HAS activity.
    const slice = makeSourceEnvelope() as unknown as {
      sources: { codex: { data: CodexSourceData } };
    };
    slice.sources.codex.data = makeDecoratedCodexSourceData();
    updateSnapshot(slice as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_A });
    render(<CacheReportPanel />);
    expect(screen.getByText(/could not be built/i)).toBeInTheDocument();
  });
});

describe('<CacheReportPanel /> #443 S1 chip never contradicts the rendered report', () => {
  it('keeps the chip on the failure branch, where the section value really is null', () => {
    const e = baseEnvelope();
    e.hydrating = false;
    e.source_schema_version = 2;
    e.default_source = 'claude';
    e.source_order = ['claude', 'codex', 'all'];
    e.sources = {
      claude: {
        source: 'claude', availability: 'unavailable', freshness: 'fresh',
        warnings: [], data_version: 'v1', last_success_at: null,
        capabilities: {}, data: null,
        domain_freshness: { hero: 'fresh', quota: 'fresh', sessions: 'fresh' },
      },
    } as unknown as Envelope['sources'];
    updateSnapshot(e);
    render(<CacheReportPanel />);
    expect(document.querySelector('.provider-section-status')!.textContent)
      .toBe('unavailable');
  });
});

describe('<CacheReportPanel /> #443 S1 empty card copy', () => {
  it('adds no chip or reason to the ordinary cold-start empty card', () => {
    // Spec §4.2 branch 2 — this card's copy is unchanged. An `empty` chip plus
    // "No Claude cache activity is available for this window." beneath
    // "No Claude activity yet / Run a session to start tracking" says the same
    // thing three times.
    updateSnapshot(withCodexSection(emptyCacheReport()));
    render(<CacheReportPanel />);
    expect(screen.getByText(/No Claude activity yet/)).toBeInTheDocument();
    expect(screen.getByText(/Run a session to start tracking/)).toBeInTheDocument();
    expect(document.querySelector('.provider-section-status')).toBeNull();
    expect(screen.queryByText(/is available for this window/i)).toBeNull();
  });
});

describe('<CacheReportPanel /> #443 S1 not-measured headline', () => {
  it('renders the not-measured headline in the healthy branch', () => {
    updateSnapshot(envelopeWith(unobservedTodayCacheReport()));
    render(<CacheReportPanel />);
    // Scoped to .cr-headline: the sparkline's unobserved guide carries an
    // accessible <title> with the same words, which is deliberate.
    expect(document.querySelector('.cr-headline')!.textContent)
      .toBe('No activity today');
    expect(screen.queryByText(/Today: cache hit 0%/)).toBeNull();
    expect(screen.queryByText('✓')).toBeNull();
    expect(document.querySelector('.cr-glyph')!.textContent).toBe('·');
  });

  it('keeps baseline progress in the headline when unobserved and thin', () => {
    updateSnapshot(envelopeWith(thinBaselineUnobservedCacheReport()));
    render(<CacheReportPanel />);
    expect(screen.getByText(/Building baseline · 3\/5 days/)).toBeInTheDocument();
    // insufficient still owns the headline; the subline carries the modifier.
    expect(document.querySelector('.cr-subline')!.textContent)
      .toBe('No activity today');
  });
});

// ---------------------------------------------------------------------------
// #443 S2 §4.5 (F26) — the panel's window labels are the report's own window,
// not a hard-coded 14. S1 already did this for the modal header and the
// all-mode summary; these were the four literals left behind.
// ---------------------------------------------------------------------------

describe('<CacheReportPanel /> #443 S2 window labels', () => {
  function withWindow(days: number, over: Partial<CacheReportEnvelope> = {}) {
    const base = healthyCacheReport();
    return { ...base, window_days: days, ...over };
  }

  it.each([
    ['healthy', {}],
    ['anomalous', {
      today: {
        ...healthyCacheReport().today,
        cache_hit_percent: 49, delta_pp: 18, net_usd: -0.42,
        anomaly_triggered: true,
        anomaly_reasons: ['cache_drop' as const, 'net_negative' as const],
      },
    }],
    ['unobserved-today', {
      today: {
        ...healthyCacheReport().today,
        cache_hit_percent: 0, net_usd: 0, saved_usd: 0, wasted_usd: 0,
        anomaly_unevaluated: ['net_negative' as const, 'cache_drop' as const],
        observed: false,
      },
    }],
  ])('the %s subline names the report window, not a hard-coded 14', (_name, over) => {
    updateSnapshot(envelopeWith(withWindow(7, over)));
    render(<CacheReportPanel />);
    const subline = document.querySelector('.cr-subline')!.textContent!;
    expect(subline).toContain('vs 7d median');
    expect(subline).not.toContain('vs 14d median');
  });

  it('the net subline names the report window too', () => {
    updateSnapshot(envelopeWith(withWindow(7)));
    render(<CacheReportPanel />);
    const second = document.querySelector('.cr-subline.second')!.textContent!;
    expect(second).toContain('7d net:');
    expect(second).not.toContain('14d net:');
  });
});

describe('<CacheReportPanel /> #443 S2 survives a source flip while mounted', () => {
  // REGRESSION (review P0-1). `useAccountScope` is three
  // `useSyncExternalStore` calls, and it was introduced BELOW the
  // `activeSource === 'all'` early return — so the panel rendered 3 hooks in
  // all-mode and 6 in single-source mode.
  //
  // Every other test in this file dispatches SET_ACTIVE_SOURCE *before*
  // render(), which pins the hook count for the component's whole life and
  // cannot observe this. The panel is mounted `key={id}` by panel id, NOT by
  // source (App.tsx), so the real instance survives the flip that the header
  // segmented control and the keyboard shortcut both dispatch. With no error
  // boundary anywhere in the app, the thrown hook-order error unmounted the
  // entire dashboard to a blank page.
  //
  // Flip AFTER mounting, in both directions, or this test is vacuous.
  function mountedEnvelope(): Envelope {
    return {
      ...(structuredClone(envelopeFixture) as unknown as Envelope),
      ...makeSourceEnvelope(),
    };
  }

  it('does not throw when the source flips from all to a single provider', () => {
    updateSnapshot(mountedEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    expect(document.querySelector('[data-source="all"]')).not.toBeNull();

    expect(() => {
      act(() => { dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' }); });
    }).not.toThrow();
    expect(document.querySelector('[data-source="all"]')).toBeNull();
    expect(document.querySelector('#panel-cache-report')).not.toBeNull();
  });

  it('does not throw when the source flips from a single provider to all', () => {
    updateSnapshot(mountedEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<CacheReportPanel />);
    expect(document.querySelector('[data-source="all"]')).toBeNull();

    expect(() => {
      act(() => { dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }); });
    }).not.toThrow();
    expect(document.querySelector('[data-source="all"]')).not.toBeNull();
  });

  it('survives a round trip through every source', () => {
    updateSnapshot(mountedEnvelope());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    expect(() => {
      for (const source of ['claude', 'codex', 'all', 'codex', 'claude', 'all'] as const) {
        act(() => { dispatch({ type: 'SET_ACTIVE_SOURCE', source }); });
      }
    }).not.toThrow();
    expect(document.querySelector('#panel-cache-report')).not.toBeNull();
  });
});
