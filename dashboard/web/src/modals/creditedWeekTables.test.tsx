// #703 + #707 §6.3 — the `$/1%` TABLE column of a credited week.
//
// The cause reached the hero, the Current Week modal and the weekly detail
// card, and stopped there. Four lines to the right of a detail card reading
// `No climb since credit`, the Weekly modal's own table printed a bare
// em-dash, and so did the `$/1% Trend` panel. A real-browser pass measured the
// consequence: the credited week and an uncredited week with no usage rendered
// the IDENTICAL glyph in that column, so the table could not tell a withheld
// ratio from an absent one — which is exactly the reading `creditMarker.ts`
// says an em-dash there wrongly gives.
//
// The column is narrow and holds figures, so it carries the SHORT form of the
// cause rather than the full phrase, in the same vocabulary the terminal's
// `_DPP_WITHHELD_CELL` map uses for the same column.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { PeriodTable } from './PeriodTable';
import { TrendModal } from './TrendModal';
import { TrendPanel } from '../panels/TrendPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { useReducedMotion } from '../hooks/useReducedMotion';
import type { Envelope, ModelCostRow, PeriodRow, TrendRow } from '../types/envelope';

vi.mock('../hooks/useReducedMotion');

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 100 },
];

function weeklyRow(over: Partial<PeriodRow> = {}): PeriodRow {
  return {
    label: '04-13', cost_usd: 500, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 12, dollar_per_pct: 50, delta_cost_pct: 10, is_current: true,
    models, week_start_at: '2026-04-13T14:00:00+00:00',
    week_end_at: '2026-04-20T14:00:00+00:00', ...over,
  };
}

/** The `$/1%` cell of a weekly `PeriodTable` row — the fifth column. */
function weeklyDollarCells(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll('tbody tr'),
  ).map((tr) => tr.querySelectorAll('td')[4] as HTMLElement);
}

function trendRow(over: Partial<TrendRow> = {}): TrendRow {
  return {
    label: 'Apr 13', used_pct: 12, dollar_per_pct: 4.2, delta: null,
    is_current: false, cost_usd: 50, ...over,
  };
}

function trendEnvelope(rows: TrendRow[]): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-17T12:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'Apr 13–Apr 20', used_pct: 12, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null,
    trend: {
      weeks: rows,
      spark_heights: rows.map(() => 4),
      history: rows,
    },
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
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

describe('the weekly table distinguishes a withheld ratio from an absent one', () => {
  it('prints the short cause on the credited row and the em-dash on the control', () => {
    const { container } = render(
      <PeriodTable
        rows={[
          weeklyRow({
            label: '04-13', credited: true, dollar_per_pct: null,
            dollar_per_pct_withheld: 'no-climb-since-credit',
          }),
          // The control the browser pass measured against: no credit, no usage
          // snapshot, so the ratio is genuinely absent.
          weeklyRow({
            label: '03-16', is_current: false, used_pct: null,
            dollar_per_pct: null,
            week_start_at: '2026-03-16T14:00:00+00:00',
            week_end_at: '2026-03-23T14:00:00+00:00',
          }),
        ]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    const [credited, control] = weeklyDollarCells(container);
    expect(credited.textContent).toBe('No climb');
    expect(control.textContent).toBe('—');
    // The whole point of the finding: the two cells must not read alike.
    expect(credited.textContent).not.toBe(control.textContent);
    expect(credited.querySelector('.dpp-withheld')).not.toBeNull();
    expect(control.querySelector('.dpp-withheld')).toBeNull();
  });

  it('carries the full phrase in the cell title, which the short form abbreviates', () => {
    const { container } = render(
      <PeriodTable
        rows={[weeklyRow({
          credited: true, dollar_per_pct: null,
          dollar_per_pct_withheld: 'no-climb-since-credit',
        })]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    expect(container.querySelector('.dpp-withheld')?.getAttribute('title'))
      .toBe('No climb since credit');
  });

  it('renders a short generic word for an unrecognized cause, never an empty cell', () => {
    // The column is too narrow to paste a server-minted cause in verbatim, so
    // the cell says what is true — the ratio is withheld — and the exact cause
    // rides in the title.
    const { container } = render(
      <PeriodTable
        rows={[weeklyRow({
          credited: true, dollar_per_pct: null,
          dollar_per_pct_withheld: 'some-future-cause',
        })]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    const cell = weeklyDollarCells(container)[0];
    expect(cell.textContent).toBe('Withheld');
    expect(cell.querySelector('.dpp-withheld')?.getAttribute('title'))
      .toBe('some-future-cause');
  });

  it('keeps a published ratio, so a stale cause can never replace a figure', () => {
    const { container } = render(
      <PeriodTable
        rows={[weeklyRow({
          credited: true, dollar_per_pct: 50,
          dollar_per_pct_withheld: 'no-climb-since-credit',
        })]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    const cell = weeklyDollarCells(container)[0];
    expect(cell.textContent).toBe('$50.00');
    expect(cell.querySelector('.dpp-withheld')).toBeNull();
  });
});

describe('the $/1% trend panel distinguishes a withheld ratio from an absent one', () => {
  function dollarCells(): HTMLElement[] {
    return Array.from(
      document.querySelectorAll('#trend-rows tr'),
    ).map((tr) => tr.querySelectorAll('td')[2] as HTMLElement);
  }

  it('prints the short cause on the credited week and the em-dash on the control', () => {
    updateSnapshot(trendEnvelope([
      trendRow({
        label: 'Apr 13', dollar_per_pct: null,
        dollar_per_pct_withheld: 'no-climb-since-credit',
      }),
      trendRow({ label: 'Mar 16', used_pct: null, dollar_per_pct: null }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    render(<TrendPanel />);
    const [credited, control] = dollarCells();
    expect(credited.textContent).toBe('No climb');
    expect(control.textContent).toBe('—');
    expect(credited.textContent).not.toBe(control.textContent);
  });

  it('leaves the figure of an ordinary week alone', () => {
    updateSnapshot(trendEnvelope([trendRow({ dollar_per_pct: 4.2 })]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    render(<TrendPanel />);
    expect(dollarCells()[0].textContent).toBe('$4.20');
    expect(document.querySelector('.dpp-withheld')).toBeNull();
  });
});

describe('the trend modal names the cause in the same column', () => {
  it('replaces its `Unavailable` placeholder with the cause when one exists', () => {
    updateSnapshot(trendEnvelope([
      trendRow({
        label: 'Apr 13', dollar_per_pct: null,
        dollar_per_pct_withheld: 'no-climb-since-credit',
      }),
      trendRow({ label: 'Mar 16', used_pct: null, dollar_per_pct: null }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    render(<TrendModal />);
    const cells = Array.from(
      document.querySelectorAll('#mtr-rows tr'),
    ).map((tr) => tr.querySelectorAll('td')[3] as HTMLElement);
    expect(cells[0].textContent).toBe('No climb');
    // An absent ratio with no cause keeps the placeholder it already had.
    expect(cells[1].textContent).toBe('Unavailable');
  });
});
