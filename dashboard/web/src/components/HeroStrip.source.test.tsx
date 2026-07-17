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

  it('shows Codex spend + the five token counters, quota windows, and budget verdict', () => {
    render(<HeroStrip />);
    expect(screen.getByTestId('codex-hero-spent')).toHaveTextContent('$12.30');
    const tokens = screen.getByTestId('codex-hero-tokens');
    expect(tokens).toHaveTextContent('input');
    expect(tokens).toHaveTextContent('cached input');
    expect(tokens).toHaveTextContent('reasoning');
    const support = screen.getByTestId('codex-hero-support');
    expect(support).toHaveTextContent('5-hour limit');
    expect(support).toHaveTextContent('Weekly limit');
    expect(support).toHaveTextContent('Budget');
  });

  it('shows NO $/1% and NO subscription-week copy under Codex', () => {
    const { container } = render(<HeroStrip />);
    expect(container.textContent).not.toContain('/ 1% used');
    expect(container.textContent).not.toContain('WEEK USAGE');
    expect(container.textContent).not.toContain('SPENT THIS WEEK');
  });
});

describe('HeroStrip — All combined tiles (§6.1)', () => {
  it('shows the combined {cost_usd, total_tokens} when non-null', () => {
    updateSnapshot(envWith());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const combined = screen.getByTestId('all-hero-combined');
    // claude 8.4 + codex 12.3 = 20.7
    expect(combined).toHaveTextContent('$20.70');
    expect(screen.queryByTestId('combined-unavailable')).toBeNull();
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
    expect(screen.getByTestId('combined-unavailable')).toHaveTextContent('Codex ingest is in progress.');
  });
});

describe('HeroStrip — Claude unchanged (default source)', () => {
  it('keeps the subscription-week vocabulary under Claude', () => {
    updateSnapshot(envWith());
    render(<HeroStrip />);
    expect(screen.getByText(/SPENT THIS WEEK/)).toBeInTheDocument();
  });
});
