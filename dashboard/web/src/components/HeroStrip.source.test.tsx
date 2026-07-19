import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

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

  it('uses the shared three-zone hero with Codex cycle, quota, forecast, and budget values', () => {
    render(<HeroStrip />);
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('$12');
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('552k');
    expect(screen.getByTestId('shared-hero-usage')).toHaveTextContent('7-DAY LIMIT');
    expect(screen.getByTestId('shared-hero-usage')).toHaveTextContent('5-HOUR');
    const support = screen.getByTestId('shared-hero-support');
    expect(support).toHaveTextContent('Forecast @ reset');
    expect(support).toHaveTextContent('Budget');
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
    const unavailable = screen.getByTestId('shared-hero-spent');
    expect(unavailable).toHaveTextContent('—');
    expect(unavailable).toHaveTextContent('Codex native reset cycle is unavailable.');
    expect(screen.getByTestId('shared-hero-usage')).toHaveTextContent('5-HOUR');
    expect(screen.getByTestId('shared-hero-support')).toHaveTextContent('Budget');
  });

  it('shows NO $/1% and NO subscription-week copy under Codex', () => {
    const { container } = render(<HeroStrip />);
    expect(container.textContent).not.toContain('/ 1% used');
    expect(container.textContent).not.toContain('WEEK USAGE');
    expect(container.textContent).not.toContain('SPENT THIS WEEK');
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
    expect(screen.getByTestId('shared-hero-usage')).toHaveTextContent('5-HOUR—');
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('$12');
    expect(screen.queryByText(/unavailable/i)).not.toBeInTheDocument();
  });

  it('displays a restored 300-minute limit independently without changing cycle spend', () => {
    render(<HeroStrip />);
    expect(screen.getByTestId('shared-hero-usage')).toHaveTextContent('5-HOUR42%');
    expect(screen.getByTestId('shared-hero-spent')).toHaveTextContent('$12');
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
