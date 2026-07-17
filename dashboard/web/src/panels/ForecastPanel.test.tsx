// #248 Task 5 — Forecast calm tile + verdict glyph (C1 + the panel side of C2).
//
// The rebuilt panel is a calm-when-healthy tile: the projected % is the hero,
// and the verdict chip's glyph comes from `resolveVerdict(...).glyph`
// (✓ / ⚠ / ⛔) — NEVER the old hardcoded `icons.svg#warn-triangle`. Escalation:
// ok = calm (no accent edge); warn = amber accent edge + filled chip; over =
// red. The C2 NON-VACUITY guard is the `ok` test below: it asserts the OK chip
// shows ✓ and NOT ⚠, which fails the moment the glyph is hardcoded back.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ForecastPanel } from './ForecastPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { Envelope, ForecastEnvelope, Verdict } from '../types/envelope';

function forecast(verdict: Verdict, wkAvg = 88): ForecastEnvelope {
  return {
    verdict,
    week_avg_projection_pct: wkAvg,
    recent_24h_projection_pct: 92,
    budget_100_per_day_usd: 4.2,
    budget_90_per_day_usd: 3.1,
    confidence: 'high',
    confidence_score: 3,
    explain: {},
  };
}

function env(verdict: Verdict, wkAvg = 88): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: verdict,
      vs_last_week_delta: null,
    },
    current_week: null, forecast: forecast(verdict, wkAvg), trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function renderFor(verdict: Verdict, wkAvg = 88) {
  _resetForTests();
  updateSnapshot(env(verdict, wkAvg));
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
    expect(num?.textContent).toContain('88%');
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
    const { container } = renderFor('capped');
    const chip = container.querySelector('.fc-verdict-chip');
    expect(chip?.textContent).toContain('⛔');
    expect(chip?.className).toContain('is-over');
    expect(container.querySelector('.fc-accent-edge')).not.toBeNull();
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
    expect(foot?.textContent).toContain('92%');     // recent_24h_projection_pct
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

  it('Codex mode renders NO forecast values and no Claude forecast tile', () => {
    updateSnapshot(forecastLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<ForecastPanel />);
    expect(container.querySelector('[data-panel-kind="forecast"]')).toBeNull();
    expect(container.querySelector('.fc-num')).toBeNull();
  });

  it('All mode wraps the Claude forecast tile in a Claude-labeled provider section (no Codex section)', () => {
    updateSnapshot(forecastLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ForecastPanel />);
    const section = container.querySelector('.source-provider-section[data-source="claude"]');
    expect(section).not.toBeNull();
    expect(section!.querySelector('.source-chip')?.textContent).toBe('Claude');
    expect(section!.querySelector('[data-panel-kind="forecast"]')).not.toBeNull();
    expect(section!.querySelector('.fc-num')?.textContent).toContain('88%');
    expect(container.querySelector('.source-provider-section[data-source="codex"]')).toBeNull();
  });

  it('Claude mode still renders the forecast tile through the wrap (transparent)', () => {
    updateSnapshot(forecastLeakEnv());
    const { container } = render(<ForecastPanel />);
    expect(container.querySelector('[data-panel-kind="forecast"]')).not.toBeNull();
    expect(container.querySelector('.fc-num')?.textContent).toContain('88%');
  });
});
