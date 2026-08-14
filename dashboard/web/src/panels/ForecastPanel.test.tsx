// #248 Task 5 — Forecast calm tile + verdict glyph (C1 + the panel side of C2).
//
// The rebuilt panel is a calm-when-healthy tile: the projected % is the hero,
// and the verdict chip's glyph comes from `resolveVerdict(...).glyph`
// (✓ / ⚠ / ⛔) — NEVER the old hardcoded `icons.svg#warn-triangle`. Escalation:
// ok = calm (no accent edge); warn = amber accent edge + filled chip; over =
// red. The C2 NON-VACUITY guard is the `ok` test below: it asserts the OK chip
// shows ✓ and NOT ⚠, which fails the moment the glyph is hardcoded back.
import { render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ForecastPanel } from './ForecastPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  CodexSourceData,
  Envelope,
  ForecastEnvelope,
  SourcesMap,
  Verdict,
} from '../types/envelope';

function forecast(verdict: Verdict, wkAvg = 88, recent = 92): ForecastEnvelope {
  return {
    verdict,
    week_avg_projection_pct: wkAvg,
    recent_24h_projection_pct: recent,
    budget_100_per_day_usd: 4.2,
    budget_90_per_day_usd: 3.1,
    confidence: 'high',
    confidence_score: 3,
    explain: {},
  };
}

function env(verdict: Verdict, wkAvg = 88, recent = 92): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: verdict,
      vs_last_week_delta: null,
    },
    current_week: null, forecast: forecast(verdict, wkAvg, recent), trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function renderFor(verdict: Verdict, wkAvg = 88, recent = 92) {
  _resetForTests();
  updateSnapshot(env(verdict, wkAvg, recent));
  return render(<ForecastPanel />);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#248 Task 5 — Forecast calm tile', () => {
  it('the projected % (week_avg_projection_pct) is the dominant number', () => {
    const { container } = renderFor('ok', 88);
    const num = container.querySelector('.fc-num');
    expect(num).not.toBeNull();
    expect(num?.textContent).toContain('88.0%');
  });

  it('keeps one decimal for the projection and current/recent percentages', () => {
    const { container } = renderFor('ok', 19.5, 27.5);
    expect(container.querySelector('.fc-num')?.textContent).toBe('19.5%');
    expect(container.querySelector('.fc-budget-foot')?.textContent).toContain('27.5%');
  });

  // C2 NON-VACUITY: the OK verdict must render the ✓ glyph, NOT ⚠. This is the
  // regression that the hardcoded `#warn-triangle` broke — proven RED by
  // reverting `{v.glyph}` to a hardcoded ⚠.
  it('OK verdict renders the ✓ glyph (not ⚠) and stays calm — C2 regression', () => {
    const { container } = renderFor('ok');
    const chip = container.querySelector('.fc-verdict-chip');
    expect(chip).not.toBeNull();
    expect(chip?.textContent).toContain('✓');
    expect(chip?.textContent).not.toContain('⚠');
    // Calm: no escalation accent edge on a healthy forecast.
    expect(container.querySelector('.fc-accent-edge')).toBeNull();
    expect(chip?.className).toContain('is-good');
  });

  it('WARN (cap) renders ⚠ + an amber accent edge + filled chip', () => {
    const { container } = renderFor('cap');
    const chip = container.querySelector('.fc-verdict-chip');
    expect(chip?.textContent).toContain('⚠');
    expect(chip?.textContent).not.toContain('✓');
    expect(chip?.className).toContain('is-warn');
    expect(container.querySelector('.fc-accent-edge')).not.toBeNull();
  });

  it('OVER (capped) renders ⛔ + red escalation', () => {
    const { container } = renderFor('capped', 100);
    const chip = container.querySelector('.fc-verdict-chip');
    expect(chip?.textContent).toContain('⛔');
    expect(chip?.className).toContain('is-over');
    expect(container.querySelector('.fc-accent-edge')).not.toBeNull();
    expect(container.querySelector('.fc-num')?.textContent).toContain('≥100%');
    const section = container.querySelector('[data-panel-kind="forecast"]');
    expect(section?.className).toContain('fc-esc-over');
  });

  // #264 S1 (VOID-1) — the pace bar fills the matched short-row height with a
  // fill sized to week_avg_projection_pct (clamped 0..100) and tinted by the
  // resolved verdict, so the sparse tile is no longer a void.
  it('#264 S1 — renders a verdict-tinted pace bar filled to the week-avg projection', () => {
    const { container } = renderFor('ok', 88);
    const pace = container.querySelector('.fc-pace');
    expect(pace).not.toBeNull();
    expect(pace?.className).toContain('is-good');
    const fill = container.querySelector('.fc-pace-fill') as HTMLElement;
    expect(fill).not.toBeNull();
    expect(fill.style.width).toBe('88%');
  });

  it('#264 S1 — clamps the pace fill to 100% when the projection exceeds the cap', () => {
    const { container } = renderFor('capped', 140);
    const fill = container.querySelector('.fc-pace-fill') as HTMLElement;
    expect(fill.style.width).toBe('100%');
    expect(container.querySelector('.fc-pace')?.className).toContain('is-over');
  });

  it('renders the muted budget foot (recent-24h + per-day budgets)', () => {
    const { container } = renderFor('ok');
    const foot = container.querySelector('.fc-budget-foot');
    expect(foot).not.toBeNull();
    expect(foot?.textContent).toContain('$4.20');  // budget_100_per_day_usd
    expect(foot?.textContent).toContain('$3.10');  // budget_90_per_day_usd
    expect(foot?.textContent).toContain('92.0%');   // recent_24h_projection_pct
  });

  it('drops the old hardcoded warn-banner / warn-triangle', () => {
    const { container } = renderFor('cap');
    expect(container.querySelector('.warn-banner')).toBeNull();
    expect(container.querySelector('#fc-banner')).toBeNull();
    expect(container.querySelector('use[href*="warn-triangle"]')).toBeNull();
  });
});

// #294 S5 — the source seam: ForecastPanel must not leak the legacy top-level
// `env.forecast` (Claude forecast) under a Codex selection. Wrapped in
// SourcePanelShell — Claude renders unchanged, Codex renders nothing, All
// renders the Claude-labeled provider section.
function forecastLeakEnv(): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry();
  const slice = makeSourceEnvelope({
    sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
  });
  // A populated legacy top-level forecast (the structural leak surface).
  return { ...env('ok', 88), ...slice } as unknown as Envelope;
}

describe('ForecastPanel source seam — no Claude leak under Codex (#294 S5)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('Codex mode renders the shared forecast tile from native quota forecast', () => {
    updateSnapshot(forecastLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<ForecastPanel />);
    expect(container.querySelector('[data-panel-kind="forecast"][data-source="codex"]')).not.toBeNull();
    expect(container.querySelector('.fc-num')).not.toBeNull();
  });

  it('All mode renders labelled Claude and Codex projections without one combined verdict', () => {
    updateSnapshot(forecastLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ForecastPanel />);
    expect(container.querySelectorAll('[data-panel-kind="forecast"]')).toHaveLength(1);
    expect(container.querySelector('[data-source="all"]')).not.toBeNull();
    const claude = container.querySelector('[data-provider-section="claude"]');
    const codex = container.querySelector('[data-provider-section="codex"]');
    expect(claude?.textContent).toContain('Claude');
    expect(claude?.textContent).toContain('88.0%');
    expect(claude?.textContent).toContain('OK');
    expect(codex?.textContent).toContain('Codex');
    expect(codex?.textContent).toContain('80.0%');
    expect(container.querySelector('[data-provider-section="all"]')).toBeNull();
  });

  it('Claude mode still renders the forecast tile through the wrap (transparent)', () => {
    updateSnapshot(forecastLeakEnv());
    const { container } = render(<ForecastPanel />);
    expect(container.querySelector('[data-panel-kind="forecast"]')).not.toBeNull();
    expect(container.querySelector('.fc-num')?.textContent).toContain('88.0%');
  });
});

// #556 S4 F2 — the All Forecast panel rendered no `foot` array at all, though
// `presentationForecastComposition` already placed the complete presentation
// into each section's `value`. Claude lost both per-day budget rows and Codex
// lost Confidence and Budget pace, so the panel showed strictly less than
// either provider tab. Under decoration the parity is deliberately partial:
// `presentationForecast` still derives Confidence from the FIRST eligible
// weekly history — one arbitrarily chosen account — so publishing it as the
// provider's would reconcile against no account card and no tab.
//
// The panel also collapsed a provider with no forecast to a bare reason line
// under a literal `unavailable` status chip, where that provider's own tab
// renders the labelled dash structure. It now renders that structure, drops
// the chip (per docs/dashboard-gotchas.md:730 — a chip describes
// `section.value`, and this surface renders a substitute) and KEEPS the
// reason, which can carry a specific warning or capability explanation.
function allEnv(opts: { claudeForecast: boolean; codexData?: CodexSourceData }): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = opts.codexData == null
    ? makeCodexSourceEntry()
    : makeCodexSourceEntry({ data: opts.codexData });
  const slice = makeSourceEnvelope({
    sources: { claude, codex, all: makeAllSourceEntry(claude, codex) } as unknown as SourcesMap,
  });
  const base = env('ok', 88);
  // `makeClaudeSourceData().hero.forecast` is null, so the top-level legacy
  // forecast is the only Claude forecast in this fixture: dropping it is
  // exactly the "no Claude forecast" state, with no other field disturbed.
  return {
    ...base,
    ...(opts.claudeForecast ? {} : { forecast: null }),
    ...slice,
  } as unknown as Envelope;
}

// The wire type declares `forecast` non-nullable on a quota history row, but
// the read path guards `row.forecast != null` because the server omits it, so
// the fixture reproduces the emitted shape rather than the declared one.
function codexDataWithoutForecast(): CodexSourceData {
  const base = makeCodexSourceData();
  return {
    ...base,
    quota: {
      ...base.quota,
      histories: base.quota.histories.map((row) => ({ ...row, forecast: null })),
    },
  } as unknown as CodexSourceData;
}

function renderAllForecast(opts: { claudeForecast: boolean; codexData?: CodexSourceData }) {
  _resetForTests();
  updateSnapshot(allEnv(opts));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  return render(<ForecastPanel />);
}

describe('the All Forecast panel carries each provider\'s own detail (#556 S4)', () => {
  it('renders each provider footer line under All', () => {
    renderAllForecast({ claudeForecast: true });
    expect(screen.getByText('Budget ≤100%')).toBeInTheDocument();
    expect(screen.getByText('Budget ≤90%')).toBeInTheDocument();
    expect(screen.getByText('Confidence')).toBeInTheDocument();
    expect(screen.getByText('Budget pace')).toBeInTheDocument();
  });

  it('omits Confidence for a decorated Codex under All', () => {
    const { container } = renderAllForecast({
      claudeForecast: true,
      codexData: makeDecoratedCodexSourceData(),
    });
    // Non-vacuity: the decorated branch must really be the one under test, so
    // the Codex section must be in per-account mode. Without this the absence
    // of Confidence would prove nothing.
    const codex = container.querySelector('[data-provider-section="codex"]') as HTMLElement;
    expect(within(codex).getAllByTestId('forecast-per-account').length).toBeGreaterThan(0);
    expect(screen.queryByText('Confidence')).not.toBeInTheDocument();
    expect(within(codex).getByText('Budget pace')).toBeInTheDocument();
  });

  // #556 S4 F8 — see WeeklyPanel.test.tsx for the rule this asserts. Like the
  // Cache Report panel summary, this section container is a plain `<div>`, so
  // it additionally needs `role="region"` for the name to resolve to anything.
  it('names each provider section by its own heading and exposes a region', () => {
    const { container } = renderAllForecast({ claudeForecast: true });
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
      expect(heading!.textContent).toMatch(/^(Claude|Codex) forecast$/);
    });
    const ids = [...container.querySelectorAll('h3[id]')].map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('renders the dashed structure and the reason, and no status chip, when Claude has no forecast', () => {
    const { container } = renderAllForecast({ claudeForecast: false });
    const claudeSection = container.querySelector('[data-provider-section="claude"]') as HTMLElement;
    // The labelled structure the Claude tab renders, not a bare reason line.
    expect(within(claudeSection).getByText('Projected @ reset')).toBeInTheDocument();
    expect(within(claudeSection).getByText('Recent-24h')).toBeInTheDocument();
    expect(within(claudeSection).getByText('Budget ≤100%')).toBeInTheDocument();
    const kpis = claudeSection.querySelector('.provider-summary-kpis')!;
    expect(kpis.textContent).toContain('—');
    // The VALUE structure never says "unavailable" — that word belongs to the
    // status chip, which this surface must not render. The reason beneath it
    // is a separate element and deliberately survives.
    expect(kpis.textContent).not.toMatch(/unavailable/i);
    expect(claudeSection.querySelector('.provider-section-status')).toBeNull();
    expect(claudeSection.querySelector('.fc-verdict-chip')).toBeNull();
    const reason = claudeSection.querySelector('.provider-section-reason');
    expect(reason).not.toBeNull();
    expect(reason!.textContent!.trim().length).toBeGreaterThan(0);
  });

  // The substitution is provider-neutral (`section.value == null`), but §5
  // argued it for Claude only, so the Codex half shipped uncovered and a
  // Codex-specific regression would have been invisible. Codex availability is
  // `accountHistories.some((row) => row.forecast != null)`
  // (`dashboardPresentation.ts:866`), so clearing the per-history forecast is
  // exactly the "no Codex forecast" state with no other field disturbed —
  // the same construction the Claude case above uses.
  it('renders the dashed structure and the reason, and no status chip, when Codex has no forecast', () => {
    const { container } = renderAllForecast({
      claudeForecast: true,
      codexData: codexDataWithoutForecast(),
    });
    const codexSection = container.querySelector('[data-provider-section="codex"]') as HTMLElement;
    const kpis = codexSection.querySelector('.provider-summary-kpis')!;
    expect(kpis.textContent).toContain('—');
    expect(kpis.textContent).not.toMatch(/unavailable/i);
    expect(codexSection.querySelector('.provider-section-status')).toBeNull();
    expect(codexSection.querySelector('.fc-verdict-chip')).toBeNull();
    const reason = codexSection.querySelector('.provider-section-reason');
    expect(reason).not.toBeNull();
    expect(reason!.textContent!.trim().length).toBeGreaterThan(0);
    // Non-vacuity: Claude must still be the available provider in this render.
    // Without it, a panel that rendered no sections at all would pass.
    const claudeSection = container.querySelector('[data-provider-section="claude"]') as HTMLElement;
    expect(within(claudeSection).getByText('Budget ≤100%')).toBeInTheDocument();
  });
});
