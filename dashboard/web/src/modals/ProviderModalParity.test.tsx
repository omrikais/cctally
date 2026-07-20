import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { DashboardSelection, Envelope } from '../types/envelope';
import { TrendModal } from './TrendModal';
import { ProjectsModal } from './ProjectsModal';
import { CacheReportModal } from './CacheReportModal';
import { ForecastModal } from './ForecastModal';
import { CurrentWeekModal } from './CurrentWeekModal';

const envelope = fixture as unknown as Envelope;

function renderFor(source: DashboardSelection, node: React.ReactElement) {
  act(() => {
    updateSnapshot(envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source });
  });
  return render(node);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

afterEach(() => cleanup());

describe.each(['claude', 'codex', 'all'] as const)(
  'provider-neutral destination composition — %s',
  (source) => {
    it('keeps the canonical $/1% Trend hierarchy for every source', () => {
      const { container } = renderFor(source, <TrendModal />);
      expect(container.textContent).toContain('Current $ / 1%');
      expect(container.querySelector('.modal-trend .m-chipstrip')).not.toBeNull();
      expect(container.querySelector('.modal-trend .m-hero')).not.toBeNull();
      expect(container.querySelector('.modal-trend .mtr-sparkhero')).not.toBeNull();
      expect(container.querySelector('.modal-trend .m-histable')).not.toBeNull();
    });

    it('keeps Projects controls, visualization, table, and footer', () => {
      const { container } = renderFor(source, <ProjectsModal />);
      expect(container.querySelector('.projects-controls')).not.toBeNull();
      expect(
        container.querySelector('.projects-trend, [data-testid="projects-ranked-bars"]'),
      ).not.toBeNull();
      expect(container.querySelector('.projects-table')).not.toBeNull();
      expect(container.querySelector('.projects-modal-footer-hint')).not.toBeNull();
    });

    it('keeps all Cache Report composition slots', () => {
      const { container } = renderFor(source, <CacheReportModal />);
      expect(container.textContent).toContain("Today's spotlight");
      expect(container.textContent).toContain('Cache hit %');
      expect(container.textContent).toContain('Net $ per day');
      expect(container.textContent).toContain('Daily rows');
      expect(container.querySelector('[data-bd-kind="projects"]')).not.toBeNull();
      expect(container.querySelector('[data-bd-kind="models"]')).not.toBeNull();
    });

    it('keeps Forecast verdict, hero, range, rates, and budget sections', () => {
      const { container } = renderFor(source, <ForecastModal />);
      expect(container.querySelector('.modal-forecast .m-chipstrip')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .m-hero')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .mfc-rangewrap')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .sec-rates')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .sec-bud')).not.toBeNull();
      if (source !== 'claude') {
        expect(container.querySelector('.modal-forecast .m-unavailable')).not.toBeNull();
      }
    });
  },
);

it.each(['codex', 'all'] as const)(
  'routes a %s project row through the shared source-detail path',
  (source) => {
    const { getAllByTestId } = renderFor(source, <ProjectsModal />);
    fireEvent.click(getAllByTestId('projects-table-row')[0]);
    expect(getState().openSourceDetail).toMatchObject({ resource: 'project' });
  },
);

it('renders the Codex hero destination with the canonical cycle hierarchy and native milestones', () => {
  const { container } = renderFor('codex', <CurrentWeekModal />);
  expect(container.textContent).toContain('Current Cycle — per-percent milestones');
  expect(container.querySelector('.modal-current-week .m-chipstrip')).not.toBeNull();
  expect(container.querySelector('.modal-current-week .mcw-herobar')).not.toBeNull();
  expect(container.querySelector('.modal-current-week .mcw-pbar')).not.toBeNull();
  expect(container.querySelector('.modal-current-week .m-histable')).not.toBeNull();
  expect(container.textContent).not.toContain('remain source-bound in the dashboard cards');
});

it('keeps Codex current-cycle milestones bound to one native quota identity and reset', () => {
  const populated = structuredClone(envelope);
  const data = populated.sources!.codex.data!;
  const history = data.quota.histories.find((row) => row.window_minutes === 10_080)!;
  data.hero.cycle = {
    window_minutes: 10_080,
    start_at: '2026-04-23T00:00:00Z',
    resets_at: '2026-04-30T00:00:00Z',
  };
  data.quota.milestones = [
    {
      key: 'matching', source: 'codex', block_key: 'block-a', quota_key: history.key,
      window_minutes: 10_080, resets_at: data.hero.cycle.resets_at,
      percent: 22, captured_at: '2026-04-24T10:00:00Z',
      cumulative_usd: 4, marginal_usd: 1,
    },
    {
      key: 'other-identity', source: 'codex', block_key: 'block-b', quota_key: 'quota:other',
      window_minutes: 10_080, resets_at: data.hero.cycle.resets_at,
      percent: 22, captured_at: '2026-04-24T10:05:00Z',
      cumulative_usd: 40, marginal_usd: 10,
    },
    {
      key: 'old-reset', source: 'codex', block_key: 'block-c', quota_key: history.key,
      window_minutes: 10_080, resets_at: '2026-04-29T00:00:00Z',
      percent: 23, captured_at: '2026-04-24T10:10:00Z',
      cumulative_usd: 50, marginal_usd: 10,
    },
  ];
  data.quota.histories.unshift({
    ...history,
    key: 'quota:stale-weekly',
    current_percent: 99,
    forecast: {
      ...history.forecast,
      current_percent: 99,
      resets_at: '2026-04-29T00:00:00Z',
    },
  });
  act(() => {
    updateSnapshot(populated);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });
  const { container } = render(<CurrentWeekModal />);
  const rows = container.querySelectorAll('#mcw-rows tr');
  expect(rows).toHaveLength(1);
  expect(rows[0].textContent).toContain('$4.00');
  expect(container.textContent).not.toContain('$40.00');
  expect(container.textContent).not.toContain('$50.00');
});

it('keeps every Codex percentage row while thinning overlapping progress ticks', () => {
  const populated = structuredClone(envelope);
  const data = populated.sources!.codex.data!;
  const history = data.quota.histories.find((row) => row.window_minutes === 10_080)!;
  data.quota.milestones = Array.from({ length: 25 }, (_, index) => ({
    key: `milestone-${index + 1}`,
    source: 'codex' as const,
    block_key: 'block-current',
    quota_key: history.key,
    window_minutes: 10_080,
    resets_at: data.hero.cycle!.resets_at,
    percent: index + 1,
    captured_at: `2026-04-24T10:${String(index).padStart(2, '0')}:00Z`,
    cumulative_usd: index + 1,
    marginal_usd: 1,
  }));
  act(() => {
    updateSnapshot(populated);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });
  const { container } = render(<CurrentWeekModal />);
  expect(container.querySelectorAll('#mcw-rows tr')).toHaveLength(25);
  expect(container.querySelectorAll('#mcw-ticks .tick')).toHaveLength(9);
});

it('keeps the canonical Codex Forecast composition when native forecast data is unavailable', () => {
  const unavailable = structuredClone(envelope);
  if (unavailable.sources?.codex?.data?.quota) {
    unavailable.sources.codex.data.quota.histories = [];
  }
  act(() => {
    updateSnapshot(unavailable);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });
  const { container } = render(<ForecastModal />);
  expect(container.textContent).toContain('Forecast unavailable');
  expect(container.querySelector('.modal-forecast .m-hero')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .mfc-rangewrap')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .sec-rates')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .sec-bud')).not.toBeNull();
});

it('renders native Codex $/1% and daily budgets without unavailable placeholders', () => {
  const populated = structuredClone(envelope);
  const history = populated.sources!.codex.data!.quota.histories[0];
  Object.assign(history, {
    current_percent: 25,
    window_minutes: 10_080,
    forecast: {
      status: 'ok',
      current_percent: 25,
      projected_percent: 50,
      remaining_seconds: 3 * 24 * 3600,
      rate_percent_per_hour: 0.25,
      confidence: 'high',
    },
  });
  Object.assign(populated.sources!.codex.data!.periods.weekly.rows[0], {
    used_pct: 25,
    dollar_per_pct: 0.4,
  });
  act(() => {
    updateSnapshot(populated);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  const { container } = render(<ForecastModal />);
  expect(container.textContent).toContain('$0.400');
  expect(container.textContent).toContain('$10.00 / day');
  expect(container.textContent).toContain('$8.67 / day');
  expect(container.textContent).not.toContain('Forecast unavailable');
});
