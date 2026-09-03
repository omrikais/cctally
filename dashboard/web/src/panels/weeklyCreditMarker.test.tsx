// #703 + #707 §6.3/§6.4 — a credited week says so on the dashboard too.
//
// The envelope already carried `credited` and `dollar_per_pct_withheld`; the
// React client read neither, so the one surface a person actually looks at
// showed a credited week exactly like an uncredited one, and a withheld `$/1%`
// exactly like a week with no usage recorded.
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WeeklyPanel } from './WeeklyPanel';
import { PeriodDetailCard } from '../modals/PeriodDetailCard';
import { _resetForTests, updateSnapshot } from '../store/store';
import { useReducedMotion } from '../hooks/useReducedMotion';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

vi.mock('../hooks/useReducedMotion');

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 100 },
];

function periodRow(over: Partial<PeriodRow> = {}): PeriodRow {
  return {
    label: '08-29', cost_usd: 500, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 3, dollar_per_pct: 50, delta_cost_pct: 10, is_current: true,
    models, week_start_at: '2026-08-29T05:00:00+00:00',
    week_end_at: '2026-09-05T05:00:00+00:00', ...over,
  };
}

function envelopeWith(rows: PeriodRow[]): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-09-02T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Aug 29', used_pct: 3, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows, total_cost_usd: 500 },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
      budget_thresholds: [],
    },
  } as unknown as Envelope;
}

beforeEach(() => {
  _resetForTests();
  vi.mocked(useReducedMotion).mockReturnValue(true);
});
afterEach(() => {
  _resetForTests();
});

describe('the weekly panel marks a credited week', () => {
  it('renders the marker on the credited row only', () => {
    updateSnapshot(envelopeWith([
      periodRow({ label: '08-29', credited: true }),
      periodRow({
        label: '08-22', credited: false, is_current: false,
        week_start_at: '2026-08-22T05:00:00+00:00',
        week_end_at: '2026-08-29T05:00:00+00:00',
      }),
    ]));
    render(<WeeklyPanel />);
    const marked = document.querySelectorAll('.pill-credited');
    expect(marked).toHaveLength(1);
    const row = marked[0].closest('.period');
    expect(row?.querySelector('.label')?.textContent).toContain('08-29');
  });

  it('marks nothing when no week holds a credit', () => {
    updateSnapshot(envelopeWith([periodRow({ credited: false })]));
    render(<WeeklyPanel />);
    expect(document.querySelector('.pill-credited')).toBeNull();
  });

  it('marks nothing for a provider row that carries no credit field', () => {
    updateSnapshot(envelopeWith([periodRow()]));
    render(<WeeklyPanel />);
    expect(document.querySelector('.pill-credited')).toBeNull();
  });
});

describe('the weekly detail card states the credit and its withheld ratio', () => {
  it('marks the credited week', () => {
    render(
      <PeriodDetailCard
        row={periodRow({ credited: true })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    expect(document.querySelector('.pill-credited')).not.toBeNull();
  });

  it('renders the withheld cause in place of the ratio', () => {
    render(
      <PeriodDetailCard
        row={periodRow({
          credited: true,
          dollar_per_pct: null,
          dollar_per_pct_withheld: 'no-climb-since-credit',
        })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    const cell = document.querySelector('.s .v.withheld');
    expect(cell).not.toBeNull();
    expect(cell?.textContent).toBe('No climb since credit');
    // Never the em-dash, which reads as "no usage recorded", and never $0.00.
    expect(cell?.textContent).not.toBe('—');
    expect(cell?.textContent).not.toContain('$0.00');
  });

  it('renders an unrecognized cause verbatim rather than dropping it', () => {
    render(
      <PeriodDetailCard
        row={periodRow({
          credited: true,
          dollar_per_pct: null,
          dollar_per_pct_withheld: 'some-future-cause',
        })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    expect(document.querySelector('.s .v.withheld')?.textContent)
      .toBe('some-future-cause');
  });

  it('keeps the ordinary ratio when nothing is withheld', () => {
    render(
      <PeriodDetailCard
        row={periodRow({ credited: true, dollar_per_pct: 50 })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    expect(document.querySelector('.s .v.withheld')).toBeNull();
    expect(screen.getByText('$50.00')).toBeInTheDocument();
  });

  it('leaves an uncredited week with no usage on the em-dash', () => {
    render(
      <PeriodDetailCard
        row={periodRow({ credited: false, dollar_per_pct: null, used_pct: null })}
        variant="weekly"
        accentClass="accent-cyan"
      />,
    );
    expect(document.querySelector('.pill-credited')).toBeNull();
    expect(document.querySelector('.s .v.withheld')).toBeNull();
  });
});
