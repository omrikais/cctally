import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { SourceStatusChip } from './SourceStatusChip';
import { resolveSourceView } from '../store/sourceView';
import { gateSessions } from '../lib/sourceGating';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import {
  ACCOUNT_B,
  makeAllSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
  withSharedRootWeeklyWindows,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

function canonicalStructure(el: Element): unknown {
  return {
    tag: el.tagName,
    className: el.getAttribute('class'),
    metric: el.getAttribute('data-metric'),
    children: Array.from(el.children, canonicalStructure),
  };
}

function parityEnv(): Envelope {
  const env = envWith();
  env.header.vs_last_week_delta = (12.3 / 61) - 0.25;
  env.current_week = {
    used_pct: 17.4,
    five_hour_pct: 9,
    five_hour_resets_in_sec: null,
    spent_usd: 8.4,
    dollar_per_pct: 0.48,
    reset_at_utc: '2026-04-30T00:00:00Z',
    reset_in_sec: 216000,
    last_snapshot_age_sec: 420,
    milestones: [],
    freshness: {
      label: 'fresh',
      captured_at: '2026-04-24T13:00:00Z',
      age_seconds: 420,
    },
    five_hour_block: null,
  };
  const codex = env.sources!.codex.data!;
  const current = codex.periods.weekly.rows[0];
  codex.periods.weekly.rows = [
    {
      ...current,
      label: '04-23 00:00',
      cost_usd: 12.3,
      start_at: '2026-04-23T00:00:00Z',
      end_at: '2026-04-30T00:00:00Z',
      used_pct: 61,
      dollar_per_pct: 12.3 / 61,
    },
    {
      ...current,
      label: '04-16 00:00',
      cost_usd: 16.25,
      start_at: '2026-04-16T00:00:00Z',
      end_at: '2026-04-23T00:00:00Z',
      used_pct: 65,
      dollar_per_pct: 0.25,
    },
  ];
  return env;
}

function envWith(mut?: (b: ReturnType<typeof makeSourceEnvelope>) => void): Envelope {
  const slice = makeSourceEnvelope();
  mut?.(slice);
  return {
    header: { used_pct: 17.4, week_label: 'wk', five_hour_pct: null, dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok', vs_last_week_delta: null },
    current_week: null,
    ...slice,
  } as unknown as Envelope;
}

describe('HeroStrip — Codex tiles (§6.1)', () => {
  beforeEach(() => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('does not evaluate an account-blind parent forecast under decorated All', () => {
    const slice = makeSourceEnvelope();
    const data = withSharedRootWeeklyWindows(makeDecoratedCodexSourceData());
    for (const row of data.quota.histories.filter(
      (history) => history.window_minutes === 10_080,
    )) {
      Object.defineProperty(row, 'forecast', {
        get() {
          throw new Error(
            `account-blind parent forecast read for ${row.account_key ?? ACCOUNT_B}`,
          );
        },
      });
    }
    const codex = { ...slice.sources.codex, data };
    updateSnapshot({
      header: {
        used_pct: 17.4,
        week_label: 'wk',
        five_hour_pct: null,
        dollar_per_pct: 1.2,
        forecast_pct: 60,
        forecast_verdict: 'ok',
        vs_last_week_delta: null,
      },
      current_week: null,
      ...slice,
      sources: {
        ...slice.sources,
        codex,
        all: makeAllSourceEntry(slice.sources.claude, codex),
      },
    } as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });

    expect(() => render(<HeroStrip />)).not.toThrow();
    expect(screen.getAllByTestId('hero-per-account-value').length)
      .toBeGreaterThan(0);
  });

  it('uses Claude\'s exact hero structure and metric slots with Codex cycle data', () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-24T13:07:00Z'));
    const env = parityEnv();
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);
    const claudeStructure = canonicalStructure(container.querySelector('.hero-strip')!);

    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const hero = container.querySelector('.hero-strip')!;
    expect(canonicalStructure(hero)).toEqual(claudeStructure);
    expect(hero).toHaveTextContent('WEEK USAGE · Apr 23–Apr 30');
    expect(hero).toHaveTextContent('61.0%');
    expect(hero).toHaveTextContent('5-HOUR42%');
    expect(hero).toHaveTextContent('SPENT THIS WEEK$12');
    expect(hero).toHaveTextContent('$0.20 / 1% used');
    expect(hero).toHaveTextContent('Forecast @ reset80%');
    expect(hero).toHaveTextContent('$/1% vs last week$0.05');
    expect(hero).toHaveTextContent('Snapshot7m ago');
    expect(hero).not.toHaveTextContent('total tokens');
    expect(hero).not.toHaveTextContent('Budget');
  });

  it('opens the source-aware current-cycle modal instead of a status toast', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip') as HTMLElement;
    act(() => { hero.click(); });
    expect(getState().openModal).toBe('current-week');
    expect(getState().toast).toBeNull();
  });

  it('makes an unavailable native reset cycle explicit without rendering zero spend', () => {
    updateSnapshot(
      envWith((b) => {
        const codex = b.sources.codex;
        b.sources.codex = {
          ...codex,
          availability: 'partial',
          freshness: 'fresh',
          warnings: [{
            code: 'codex_cycle_unavailable',
            message: 'Codex native reset cycle is unavailable.',
            domain: 'hero',
          }],
          capabilities: {
            ...codex.capabilities,
            hero: {
              status: 'unavailable',
              semantics: 'missing-or-conflicting-native-cycle',
            },
          },
          data: {
            ...codex.data!,
            hero: {
              ...codex.data!.hero,
              cost_usd: null,
              input_tokens: null,
              cached_input_tokens: null,
              output_tokens: null,
              reasoning_output_tokens: null,
              total_tokens: null,
              cycle: null,
            },
          } as unknown as typeof codex.data,
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<HeroStrip />);
    const unavailable = document.querySelector('.hero-spent')!;
    expect(unavailable).toHaveTextContent('—');
    expect(unavailable).toHaveAttribute('title', 'Codex native reset cycle is unavailable.');
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR');
    expect(document.querySelector('.hero-support')).toHaveTextContent('$/1% vs last week');
  });

  it('uses the canonical week and $/1% vocabulary under Codex', () => {
    const { container } = render(<HeroStrip />);
    expect(container.textContent).toContain('/ 1% used');
    expect(container.textContent).toContain('WEEK USAGE');
    expect(container.textContent).toContain('SPENT THIS WEEK');
  });

  it('treats a missing 300-minute limit as healthy and keeps weekly cycle spend', () => {
    updateSnapshot(envWith((b) => {
      const data = b.sources.codex.data!;
      data.quota.histories = data.quota.histories.filter((row) => row.window_minutes !== 300);
      data.quota.blocks = data.quota.blocks.filter((row) => !row.label.includes('5-hour'));
      data.hero.quota.active = data.hero.quota.active.filter((row) => row.key !== 'quota:codex-5h');
    }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<HeroStrip />);
    expect(document.querySelector('.hero-usage')).not.toHaveTextContent('5-HOUR');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });

  it('displays a restored 300-minute limit independently without changing cycle spend', () => {
    render(<HeroStrip />);
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR42%');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
  });
});

// =========================================================================
// #350 — a stale-but-valid Codex cycle keeps its ACTUALS and blanks only the
// projection. Spec §3.0: "Projections blank. Actuals stay."
// =========================================================================

function staleCycleEnv(): Envelope {
  const env = parityEnv();
  const codex = env.sources!.codex.data!;
  // Build time stamped the additive hero-local marker; the envelope metadata
  // (availability / freshness / warnings / capabilities) is deliberately
  // untouched, because five separate gates read it as one meaning.
  (codex.hero as unknown as { cycle_freshness?: string }).cycle_freshness = 'stale';
  const weekly = codex.quota.histories.find((row) => row.window_minutes === 10_080)!;
  weekly.freshness = 'stale';
  weekly.forecast.status = 'stale';
  // The clock PRESERVES projected_percent alongside a stale status — the hero
  // must gate on the status, not on the value being null.
  weekly.forecast.projected_percent = 80.0;
  const active = codex.hero.quota.active.find((row) => row.key === weekly.key)!;
  active.freshness = 'stale';
  // Two hours old, so the client-derived Snapshot chip reads `stale`.
  active.captured_at = '2026-04-24T11:00:00Z';
  return env;
}

describe('HeroStrip — stale Codex cycle disclosure (#350)', () => {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-04-24T13:07:00Z'));
    updateSnapshot(staleCycleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('keeps every backward-looking actual and blanks only the forecast', () => {
    const { container } = render(<HeroStrip />);
    const hero = container.querySelector('.hero-strip')!;

    expect(hero).toHaveTextContent('SPENT THIS WEEK$12');
    expect(hero).toHaveTextContent('$0.20 / 1% used');
    expect(hero).toHaveTextContent('$/1% vs last week$0.05');
    expect(hero).toHaveTextContent('WEEK USAGE · Apr 23–Apr 30');
    expect(hero).toHaveTextContent('61.0%');
    // The projection pauses through its OWN forecast.status gate.
    expect(hero).toHaveTextContent('Forecast @ reset—');
    // Token totals are retained on the wire but never displayed for Codex.
    expect(hero).not.toHaveTextContent('total tokens');
  });

  it('reads the existing Snapshot stale marker rather than a new visual', () => {
    render(<HeroStrip />);
    const chip = document.querySelector('.sup-fresh')!;

    expect(chip).toHaveAttribute('data-freshness', 'stale');
    expect(chip).toHaveTextContent('⚠');
    // The state must be NAMED, not only tinted: colour plus a glyph is
    // unreadable to a colourblind user and carries no meaning on its own.
    expect(chip).toHaveTextContent(/stale/i);
    expect(chip.getAttribute('title')).toMatch(/stale/i);
  });

  it('discloses the stale cycle on the hero-spent zone without hiding the spend', () => {
    render(<HeroStrip />);
    const spent = document.querySelector('.hero-spent')!;

    expect(spent).toHaveTextContent('$12');
    expect(spent.getAttribute('title')).toMatch(/stale/i);
    // `title` alone is hover-only — unreachable on touch and unreliable for
    // screen readers, so the reason must also ride an aria-label.
    expect(spent.getAttribute('aria-label')).toMatch(/stale/i);
  });

  it('leaves the source status chip reading fresh, never "Hero unavailable"', () => {
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');

    expect(chip).toHaveTextContent('fresh');
    expect(chip.textContent).not.toContain('Hero unavailable');
    expect(chip.className).not.toContain('is-degraded');
  });

  it('leaves Sessions gating untouched', () => {
    const view = resolveSourceView(staleCycleEnv(), 'codex');
    expect(gateSessions(view).mode).toBe('render');
  });
});

describe('HeroStrip — All combined tiles (§6.1)', () => {
  it('opens the provider-composed Current Week modal', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<HeroStrip />);

    act(() => { (container.querySelector('.hero-strip') as HTMLElement).click(); });

    expect(getState().openModal).toBe('current-week');
    expect(getState().openModalSource).toBe('all');
    expect(getState().toast).toBeNull();
  });

  it('shows the combined {cost_usd, total_tokens} when non-null', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const combined = screen.getByTestId('shared-hero-spent');
    // claude 8.4 + codex 12.3 = 20.7
    expect(combined).toHaveTextContent('$20.70');
    expect(combined).not.toHaveTextContent('unavailable');
  });

  it('shows an explicit combined-unavailable state when combined is null', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          warnings: [{ code: 'source_ingest_contended', message: 'Codex ingest is in progress.' }],
          data: { ...b.sources.all.data!, combined: null },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const warning = screen.getByTestId('shared-hero-warning');
    expect(warning).toHaveTextContent('Combined unavailable');
    expect(warning).toHaveAttribute('title', 'Codex ingest is in progress.');
    expect(warning).toHaveAttribute('aria-label', 'Combined totals unavailable: Codex ingest is in progress.');
  });

  it('uses the hero warning for an unavailable combined hero instead of an earlier panel warning', () => {
    updateSnapshot(
      envWith((b) => {
        b.sources.all = {
          ...b.sources.all,
          warnings: [
            { code: 'projects', message: 'Projects metadata is incomplete.', domain: 'projects' },
            { code: 'codex_cycle_unavailable', message: 'Codex native reset cycle is unavailable.', domain: 'hero' },
          ],
          data: { ...b.sources.all.data!, combined: null },
        };
      }),
    );
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const warning = screen.getByTestId('shared-hero-warning');
    expect(warning).toHaveTextContent('Combined unavailable');
    expect(warning).toHaveAttribute('title', 'Codex native reset cycle is unavailable.');
    expect(warning).toHaveAttribute('aria-label', 'Combined totals unavailable: Codex native reset cycle is unavailable.');
  });
});

describe('HeroStrip — Claude unchanged (default source)', () => {
  it('keeps the subscription-week vocabulary under Claude', () => {
    updateSnapshot(envWith());
    render(<HeroStrip />);
    expect(screen.getByText(/SPENT THIS WEEK/)).toBeInTheDocument();
  });
});
