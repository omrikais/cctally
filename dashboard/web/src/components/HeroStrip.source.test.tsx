import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
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
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR—');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });

  it('displays a restored 300-minute limit independently without changing cycle spend', () => {
    render(<HeroStrip />);
    expect(document.querySelector('.hero-usage')).toHaveTextContent('5-HOUR42%');
    expect(document.querySelector('.hero-spent')).toHaveTextContent('$12');
  });
});

describe('HeroStrip — All combined tiles (§6.1)', () => {
  it('shows the combined {cost_usd, total_tokens} when non-null', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const combined = screen.getByTestId('shared-hero-spent');
    // claude 8.4 + codex 12.3 = 20.7
    expect(combined).toHaveTextContent('$21');
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
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('Codex ingest is in progress.');
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
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('Codex native reset cycle is unavailable.');
  });
});

describe('HeroStrip — Claude unchanged (default source)', () => {
  it('keeps the subscription-week vocabulary under Claude', () => {
    updateSnapshot(envWith());
    render(<HeroStrip />);
    expect(screen.getByText(/SPENT THIS WEEK/)).toBeInTheDocument();
  });
});
