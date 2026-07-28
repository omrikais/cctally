// #373 — every CLIENT selector that decides "is this window account-level
// standard quota" must skip the `model_scoped` rows the envelope stamps.
//
// The envelope keeps a foreign pool LISTED (§7.2: identity-oriented views
// preserve it) and marks it with the additive `model_scoped: true`, so the
// exclusion is entirely the reader's job. A selector that forgets it hands the
// account's card, forecast or 5h window to a separate model pool — the exact
// user-visible defect this issue exists to fix.
//
// This is live-reachable today, not hypothetical: the retained
// `codex_bengalfox` 5h rows sit on the PRIMARY slot and Codex stopped emitting
// standard 300-minute windows on 2026-07-12, so `find(w === 300)` resolves to
// the foreign pool whenever the standard 5h history is absent.
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import { BlocksPanel } from '../panels/BlocksPanel';
import { CurrentWeekModal } from '../modals/CurrentWeekModal';
import type {
  CodexQuotaHistoryRow, DashboardSelection, Envelope, WeekIndexEntry,
} from '../types/envelope';
import { presentationForecastComposition } from './dashboardPresentation';

function codexEnv(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return {
    header: {
      used_pct: 17.4, week_label: 'wk', five_hour_pct: null,
      dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    ...slice,
  } as unknown as Envelope;
}

/** A foreign pool's retained history, shaped like the real Spark rows. */
function foreignHistory(
  windowMinutes: number, overrides: Partial<CodexQuotaHistoryRow> = {},
): CodexQuotaHistoryRow {
  return {
    key: `quota:codex-spark-${windowMinutes}`,
    source: 'codex',
    model_scoped: true,
    label: 'GPT-5.3-Codex-Spark',
    observed_slot: 0,
    window_minutes: windowMinutes,
    current_percent: 0,
    captured_at: '2026-04-24T13:00:00Z',
    freshness: 'fresh',
    stale_after_seconds: 3600,
    forecast: {
      status: 'ok',
      current_percent: 0,
      rate_percent_per_hour: 0,
      projected_percent: 0,
      resets_at: '2026-05-01T08:58:36Z',
      remaining_seconds: 600000,
      sample_count: 4,
      sample_span_seconds: 3600,
      confidence: 'low',
    },
    ...overrides,
  } as CodexQuotaHistoryRow;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(cleanup);

describe('presentationForecastComposition ignores foreign quota pools', () => {
  it('reads the account weekly row, not a model-scoped row at index 0', () => {
    const env = codexEnv((b) => {
      const codex = b.sources.codex.data!;
      codex.quota.histories.unshift(foreignHistory(10_080, {
        forecast: {
          ...foreignHistory(10_080).forecast, status: 'stale',
        },
      }) as never);
    });
    // Positive precondition: the foreign row really is what an unfiltered
    // `.find(window_minutes === 10_080)` would resolve to.
    const codexHistories = env.sources!.codex.data!.quota.histories;
    expect(codexHistories.find((r) => r.window_minutes === 10_080)!.model_scoped).toBe(true);
    expect(
      codexHistories.some((r) => r.window_minutes === 10_080 && !r.model_scoped),
    ).toBe(true);

    const section = presentationForecastComposition(env, 'codex').sections[0];
    expect(section.source).toBe('codex');
    // The ACCOUNT weekly forecast is `ok`, so the section must not be degraded
    // by the foreign pool's stale one.
    expect(section.status).toBe('available');
    expect(section.value).toMatchObject({ projected: 80 });
  });

  it('does not count a model-scoped forecast as provider forecast capability', () => {
    const env = codexEnv((b) => {
      const codex = b.sources.codex.data!;
      for (const row of codex.quota.histories) {
        (row as { forecast: unknown }).forecast = null;
      }
      codex.quota.histories.push(foreignHistory(10_080) as never);
    });
    // Positive precondition: the ONLY row carrying a forecast is foreign.
    const withForecast = env.sources!.codex.data!.quota.histories
      .filter((r) => r.forecast != null);
    expect(withForecast).toHaveLength(1);
    expect(withForecast[0].model_scoped).toBe(true);

    const section = presentationForecastComposition(env, 'codex').sections[0];
    expect(section.status).toBe('unavailable');
    expect(section.value).toBeNull();
  });
});

describe('client 5h selectors ignore foreign quota pools', () => {
  it('BlocksPanel does not treat a foreign 5h pool as the account 5h window', () => {
    const env = codexEnv((b) => {
      const codex = b.sources.codex.data!;
      // Codex stopped emitting standard 300-minute windows on 2026-07-12, so
      // the ONLY retained 300-minute history is the foreign pool's.
      codex.quota.histories = codex.quota.histories
        .filter((row) => row.window_minutes !== 300) as never;
      codex.quota.histories.push(foreignHistory(300) as never);
    });
    // Positive precondition: an unfiltered `.some(w === 300)` is still true.
    expect(
      env.sources!.codex.data!.quota.histories.some((r) => r.window_minutes === 300),
    ).toBe(true);

    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' as DashboardSelection });
    });
    render(<BlocksPanel />);
    expect(screen.getByText('(optional 5h · current cycle)')).toBeInTheDocument();
  });

  it('CurrentWeekModal does not anchor the current 5h block on a foreign pool', () => {
    const env = codexEnv((b) => {
      const codex = b.sources.codex.data!;
      // A foreign 300-minute history FIRST, resetting at a different instant
      // than the account's retained 5h block.
      codex.quota.histories.unshift(foreignHistory(300) as never);
      const entry: WeekIndexEntry = {
        key: 'milestone_cycle:current',
        start_at_utc: '2026-04-23T00:00:00Z',
        end_at_utc: '2026-04-30T00:00:00Z',
        resets_at_utc: '2026-04-30T00:00:00Z',
        label: 'Apr 23–Apr 30',
        is_current: true,
        milestone_count: 1,
        block_count: 1,
        detail_stamp: 'stamp-1',
      };
      (codex.quota as { cycle_index?: WeekIndexEntry[] }).cycle_index = [entry];
    });
    // Positive precondition: the foreign row is what an unfiltered
    // `.find(window_minutes === 300)` resolves to, and it resets elsewhere.
    const histories = env.sources!.codex.data!.quota.histories;
    const unfiltered = histories.find((r) => r.window_minutes === 300)!;
    expect(unfiltered.model_scoped).toBe(true);
    expect(unfiltered.forecast.resets_at).not.toBe(
      env.sources!.codex.data!.quota.blocks[0].resets_at,
    );

    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' as DashboardSelection });
      dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    });
    const { container } = render(<CurrentWeekModal />);
    const nav = container.querySelector('.mcw-blocknav');
    expect(nav).not.toBeNull();
    // The account's own retained 5h block window is what the nav shows.
    expect(nav).toHaveTextContent('13:00');
    expect(nav).toHaveTextContent('18:00');
  });
});
