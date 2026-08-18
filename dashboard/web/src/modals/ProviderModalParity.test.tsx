import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { CacheReportEnvelope, DashboardSelection, Envelope } from '../types/envelope';
import { TrendModal } from './TrendModal';
import { ProjectsModal } from './ProjectsModal';
import { CacheReportModal } from './CacheReportModal';
import { ForecastModal } from './ForecastModal';
import { CurrentWeekModal } from './CurrentWeekModal';
import { WeeklyModal } from './WeeklyModal';
import { MonthlyModal } from './MonthlyModal';

const envelope = fixture as unknown as Envelope;

function asCodexCacheReport(report: CacheReportEnvelope): CacheReportEnvelope {
  const codex = structuredClone(report);
  for (const row of [codex.today, ...codex.days, ...codex.by_project, ...codex.by_model]) {
    row.cached_input_percent = row.cache_hit_percent;
    delete row.cache_hit_percent;
  }
  codex.today.wasted_usd = null;
  for (const day of codex.days) day.wasted_usd = null;
  codex.fourteen_day_efficiency_ratio = null;
  codex.not_applicable = {
    wasted_usd: 'OpenAI charges no cache-write premium, so Codex has no wasted-cache figure.',
    fourteen_day_efficiency_ratio: 'Efficiency compares saved against wasted, and Codex has no wasted-cache figure.',
  };
  codex.anomaly_predicates = ['cache_drop'];
  return codex;
}

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
      // #443 S2 — the shipped fixture publishes no Codex `cache_report`, and
      // the composition slots used to appear there only because the client
      // fabricated a report from raw daily rows. That fallback is deleted, so
      // the parity claim is now stated over a provider that actually HAS a
      // report: seed Codex's from Claude's rather than assert slots over an
      // absent one, which would only pin the unavailable body.
      const composed = structuredClone(envelope);
      // #583 S3 §4: physical entry only — the All mirror publishes null.
      composed.sources!.codex.data!.cache_report =
        asCodexCacheReport(composed.cache_report!);
      act(() => {
        updateSnapshot(composed);
        dispatch({ type: 'SET_ACTIVE_SOURCE', source });
      });
      const { container } = render(<CacheReportModal />);
      expect(container.textContent).toContain("Today's spotlight");
      // Parity is about the SLOTS, not the wording: #443 S2 gives Codex its own
      // cache vocabulary, so the timeline heading reads "Cached input %" there
      // and "Cache hit %" everywhere else. Asserting the Claude literal for
      // every source would pin the very copy the vocabulary contract fixes.
      expect(container.querySelector('.crm-sh-timeline')?.textContent).toContain(
        source === 'codex' ? 'Cached input %' : 'Cache hit %',
      );
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
      if (source === 'codex') {
        expect(container.querySelector('.modal-forecast .m-unavailable')).not.toBeNull();
      }
    });

    // #556 S5 §4.5 — the two are DIFFERENT quantities and must be pinned
    // separately. `.sec-bud` is the quota-ceiling "Daily budgets to stay under"
    // heading derived from the projection; `[data-budget-section]` is the
    // CONFIGURED budget the user set. Letting one check stand in for the other
    // would let the block disappear while this test stayed green.
    it('carries the configured-budget block beside the quota-ceiling rows', () => {
      const { container } = renderFor(source, <ForecastModal />);
      const blocks = container.querySelectorAll('[data-budget-section]');
      expect(blocks.length).toBe(source === 'all' ? 2 : 1);
      blocks.forEach((block) => {
        expect(block.getAttribute('data-surface')).toBe('modal');
        expect(block.getAttribute('role')).toBe('region');
      });
      // The quota-ceiling heading is a different element and still present.
      expect(container.querySelector('.modal-forecast .sec-bud')).not.toBeNull();
    });
  },
);

it('All Forecast modal labels and preserves both provider-native projections', () => {
  const composed = structuredClone(envelope);
  composed.forecast!.week_avg_projection_pct = 68.5;
  composed.sources!.codex.data!.quota.histories.find(
    (row) => row.window_minutes === 10_080,
  )!.forecast.projected_percent = 74;
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<ForecastModal />);

  // #578 — the visual range is a named composition, not an anonymous generic
  // div.  `getByRole` deliberately pins the accessible role/name pair rather
  // than merely proving that an aria-label attribute exists in the DOM.
  expect(screen.getByRole('group', {
    name: 'Claude projected usage against caps',
  })).toBeInTheDocument();
  expect(screen.getByRole('group', {
    name: 'Codex projected usage against caps',
  })).toBeInTheDocument();

  const claude = container.querySelector('[data-provider-section="claude"]');
  const codex = container.querySelector('[data-provider-section="codex"]');
  expect(claude?.textContent).toContain('Claude');
  expect(claude?.textContent).toContain('68.5%');
  expect(claude?.textContent).toContain('OK');
  expect(codex?.textContent).toContain('Codex');
  expect(codex?.textContent).toContain('74.0%');
  expect(container.textContent).not.toContain('Codex quota + spend');
});

it('All Cache modal labels and preserves both provider-native reports', () => {
  const composed = structuredClone(envelope);
  composed.cache_report!.days = Array.from({ length: 6 }, (_, index) => ({
    ...composed.cache_report!.days[0],
    date: `2026-04-${String(index + 1).padStart(2, '0')}`,
  }));
  const codexReport = asCodexCacheReport(composed.cache_report!);
  codexReport.today.cached_input_percent = 42;
  codexReport.today.net_usd = 12.5;
  codexReport.fourteen_day_counterfactual_usd = 99;
  // #583 S3 §4: physical entry only — the All mirror publishes null.
  composed.sources!.codex.data!.cache_report = codexReport;
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<CacheReportModal />);

  // #578 — the All-provider composition needs a real grouping role for its
  // accessible name; an aria-label on a plain div is not the contract.
  expect(screen.getByRole('group', {
    name: 'Claude and Codex cache reports',
  })).toBeInTheDocument();

  const claude = container.querySelector('[data-provider-section="claude"]');
  const codex = container.querySelector('[data-provider-section="codex"]');
  expect(claude?.textContent).toContain('Claude');
  expect(claude?.textContent?.match(/Today's spotlight/g)).toHaveLength(1);
  expect(claude?.querySelectorAll('.provider-daily-summary-row')).toHaveLength(
    composed.cache_report!.days.length,
  );
  expect(claude?.textContent).toContain('87%');
  expect(claude?.textContent).toContain('+$3.10');
  expect(codex?.textContent).toContain('Codex');
  expect(codex?.textContent?.match(/Today's spotlight/g)).toHaveLength(1);
  expect(codex?.querySelectorAll('.provider-daily-summary-row')).toHaveLength(
    codexReport.days.length,
  );
  expect(codex?.textContent).toContain('42%');
  expect(codex?.textContent).toContain('+$12.50');
  expect(container.textContent).not.toContain('All sources');
});

it('All Weekly modal keeps provider ownership on every independent quota row', () => {
  const composed = structuredClone(envelope);
  composed.sources!.claude.data!.periods.weekly.rows = composed.weekly.rows;
  composed.sources!.all.data = null;
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<WeeklyModal />);

  expect(container.querySelector('.history-table--weekly [data-col="source"]')).not.toBeNull();
  const sources = Array.from(container.querySelectorAll('.history-table--weekly tbody .source-chip'))
    .map((chip) => chip.textContent);
  expect(sources).toContain('Claude');
  expect(sources).toContain('Codex');
  expect(container.querySelector('.detail-card [data-period-source]')).not.toBeNull();
});

it('Codex Weekly uses native cycle vocabulary throughout the shared shell', () => {
  const composed = structuredClone(envelope);
  const newest = composed.sources!.codex.data!.periods.weekly.rows[0];
  composed.sources!.codex.data!.periods.weekly.rows = [
    {
      ...newest, label: 'Cycle A', cost_usd: 20,
      start_at: '2026-07-13T00:00:00Z', end_at: '2026-07-20T00:00:00Z',
    },
    {
      ...newest, label: 'Cycle B', cost_usd: 10,
      start_at: '2026-07-06T00:00:00Z', end_at: '2026-07-13T00:00:00Z',
    },
  ];
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });
  const { container } = render(<WeeklyModal />);

  expect(container.querySelector('.modal-header h2')?.textContent).toMatch(/Weekly · last \d+ cycles/);
  expect(container.querySelector('[aria-label="Cost by cycle"]')).not.toBeNull();
  expect(container.querySelector('[data-col="label"]')?.textContent).toContain('Cycle');
  expect(container.textContent).toContain('vs prior cycle');
  expect(container.textContent).toContain('Reset cycle:');
  expect(container.textContent).not.toContain('Subscription window:');
});

it('All Weekly uses neutral provider-period vocabulary while retaining source ownership', () => {
  const composed = structuredClone(envelope);
  composed.sources!.claude.data!.periods.weekly.rows = composed.weekly.rows;
  composed.sources!.all.data = null;
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<WeeklyModal />);

  expect(container.querySelector('.modal-header h2')?.textContent).toMatch(/provider periods/);
  expect(container.querySelector('[aria-label="Cost by provider period"]')).not.toBeNull();
  expect(container.querySelector('[data-col="label"]')?.textContent).toContain('Provider period');
  expect(container.textContent).toContain('vs prior provider period');
});

it('All Trend modal renders two provider-owned histories and no All-sources quota verdict', () => {
  const composed = structuredClone(envelope);
  act(() => {
    updateSnapshot(composed);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<TrendModal />);

  const claude = container.querySelector('[data-provider-section="claude"]');
  const codex = container.querySelector('[data-provider-section="codex"]');
  expect(claude?.textContent).toContain('Claude');
  expect(claude?.querySelector('.mtr-sparkhero')).not.toBeNull();
  expect(codex?.textContent).toContain('Codex');
  expect(codex?.querySelector('.mtr-sparkhero')).not.toBeNull();
  expect(container.textContent).not.toContain('All sources');
});

it('All Trend modal uses cycle vocabulary only in the Codex history (#576)', () => {
  const { container } = renderFor('all', <TrendModal />);
  const claude = container.querySelector('[data-provider-section="claude"]')!;
  const codex = container.querySelector('[data-provider-section="codex"]')!;

  expect(claude.querySelector('#mtr-weeks-pill-claude')?.textContent).toMatch(/weeks?/);
  expect(claude.querySelector('.sec-spark')?.textContent).toMatch(/week history/i);
  expect(claude.querySelector('.sec-tbl')?.textContent).toContain('Weekly detail');
  expect(claude.querySelector('[data-col="week"]')?.textContent).toContain('Week');
  expect(claude.querySelector('.mtr-tbl-sub')?.textContent).toContain('prior week');
  expect(claude.querySelector('#mtr-svg-claude')?.getAttribute('aria-label')).toMatch(/weeks?/);

  expect(codex.querySelector('#mtr-weeks-pill-codex')?.textContent).toMatch(/cycles?/);
  expect(codex.querySelector('.sec-spark')?.textContent).toMatch(/-cycle history/i);
  expect(codex.querySelector('.sec-tbl')?.textContent).toContain('Cycle detail');
  expect(codex.querySelector('[data-col="week"]')?.textContent).toContain('Cycle');
  expect(codex.querySelector('.mtr-tbl-sub')?.textContent).toContain('prior cycle');
  expect(codex.querySelector('#mtr-svg-codex')?.getAttribute('aria-label')).toMatch(/cycles?/);
  expect(codex.textContent).not.toMatch(/\bweek/i);
  expect(codex.textContent).not.toContain('W−');
});

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

it('keeps Current Week bound to the source captured when it opened', () => {
  act(() => {
    updateSnapshot(envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  const { container } = render(<CurrentWeekModal />);

  expect(container.textContent).toContain('Current Week — per-percent milestones');
  expect(container.querySelector('.modal-current-week')?.getAttribute('data-source')).toBe('claude');
  expect(container.textContent).not.toContain('Current Cycle — per-percent milestones');
});

it('keeps Current Week sharing bound to the source captured when it opened', () => {
  act(() => {
    updateSnapshot(envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  const { getByRole } = render(<CurrentWeekModal />);
  fireEvent.click(getByRole('button', { name: /Share Current week report/i }));

  expect(getState().shareModal).toMatchObject({
    panel: 'current-week',
    source: 'claude',
  });
});

it('renders provider-owned Claude and Codex destinations when Current Week opens under All', () => {
  act(() => {
    updateSnapshot(envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
  });

  const { container } = render(<CurrentWeekModal />);
  const claude = container.querySelector('[data-provider-section="claude"]');
  const codex = container.querySelector('[data-provider-section="codex"]');

  expect(container.textContent).toContain('Current Usage — provider cycles');
  expect(claude?.textContent).toContain('Claude');
  expect(claude?.querySelector('.mcw-herobar')).not.toBeNull();
  expect(codex?.textContent).toContain('Codex');
  expect(codex?.querySelector('.mcw-herobar')).not.toBeNull();

  const ids = Array.from(container.querySelectorAll('[id]'), (node) => node.id);
  expect(new Set(ids).size).toBe(ids.length);
});

it('explains provider-specific Current Week degradation under All', () => {
  const degraded = structuredClone(envelope);
  degraded.sources!.codex = {
    ...degraded.sources!.codex,
    availability: 'partial',
    warnings: [{
      code: 'codex_cycle_unavailable',
      message: 'Codex native reset evidence is unavailable.',
      domain: 'hero',
    }],
    capabilities: {
      ...degraded.sources!.codex.capabilities,
      hero: { status: 'unavailable' },
    },
  };
  act(() => {
    updateSnapshot(degraded);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
  });

  const { container } = render(<CurrentWeekModal />);
  const codex = container.querySelector('[data-provider-section="codex"]');
  expect(codex?.querySelector('.provider-section-reason')).toHaveTextContent(
    'Codex native reset evidence is unavailable.',
  );
  expect(container.querySelector('[data-provider-section="claude"] .provider-section-reason')).toBeNull();
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

// #556 S2 §5.4 — the MONTHLY variant of PeriodModal / PeriodTable under All,
// which had no coverage at all: the parity suite exercised only the weekly one.
describe('#556 S2 — the All monthly destination', () => {
  function withMonths(): Envelope {
    const env = structuredClone(envelope) as Envelope;
    env.sources!.claude.data!.periods.monthly.rows = [{
      label: '2026-04', cost_usd: 30, total_tokens: 1, input_tokens: 1,
      output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
      used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
      is_current: true, models: [],
    }];
    env.sources!.codex.data!.periods.monthly.rows = [{
      label: '2026-04', cost_usd: 12, input_tokens: 1, cached_input_tokens: 0,
      output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1,
      models: ['gpt-5'],
    }];
    return env;
  }

  it('keeps two same-labelled provider rows distinct instead of merging them', () => {
    act(() => {
      updateSnapshot(withMonths());
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });
    const { container } = render(<MonthlyModal />);

    // A Source column, and one row per provider under the shared label.
    const chips = [...container.querySelectorAll('tbody .source-chip')]
      .map((chip) => chip.textContent);
    expect(chips).toEqual(['Claude', 'Codex']);
    // Neither row is labelled "Combined", which is what a merged row rendered.
    expect(chips).not.toContain('Combined');
  });

  it('qualifies the navigator label so two April rows are tellable apart', () => {
    act(() => {
      updateSnapshot(withMonths());
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });
    const { container } = render(<MonthlyModal />);
    const text = container.textContent ?? '';
    expect(text).toContain('Claude · 2026-04');
    expect(text).toContain('Codex · 2026-04');
  });
});


// #556 S2 QA P3 — the Monthly modal title repeated the panel footer's
// calendar-month reading of a provider-month count.
it('titles the All monthly modal in provider months', () => {
  const env = structuredClone(envelope) as unknown as Envelope;
  env.sources!.claude.data!.periods.monthly.rows = [{
    label: '2026-04', cost_usd: 30, total_tokens: 1, input_tokens: 1,
    output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
    used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
    is_current: true, models: [],
  }];
  env.sources!.codex.data!.periods.monthly.rows = [{
    label: '2026-02', cost_usd: 12, input_tokens: 1, cached_input_tokens: 0,
    output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1,
    models: ['gpt-5'],
  }];
  act(() => {
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });
  const { container } = render(<MonthlyModal />);
  const title = container.querySelector('.modal-title, h2')!.textContent!;
  expect(title).toContain('2 provider months');
  expect(title).not.toContain('last 2 months');
});

// #556 S4 F1 — `wide` is width alone; the >=1025px internal-pane scroll
// contract is the separate `paneScroll` opt-in, emitted as
// `.modal-pane-scroll`. Only a modal that really renders `.period-two-pane`
// may claim it. The two All modals below ask for width and must NOT inherit
// the pane contract: before this split they got an `overflow: hidden` body
// with no pane to scroll, which left their content clipped at 1440x900 with
// no scroller anywhere in the card.
describe('the pane-scroll contract is opt-in, not implied by width (#556 S4)', () => {
  it('gives the single-provider Trend modal the pane contract', () => {
    const { container } = renderFor('claude', <TrendModal />);
    const card = container.querySelector('.modal-card')!;
    expect(card.className).toContain('modal-wide');
    expect(card.className).toContain('modal-pane-scroll');
  });

  it('withholds the pane contract from the All Trend modal', () => {
    const { container } = renderFor('all', <TrendModal />);
    const card = container.querySelector('.modal-card')!;
    expect(card.className).toContain('modal-wide');
    expect(card.className).not.toContain('modal-pane-scroll');
    // The All branch really has no `.period-two-pane` of its own at the body
    // level — it nests one per embedded provider — which is why inheriting the
    // contract clipped it.
    expect(container.querySelector('.modal-body > .period-two-pane')).toBeNull();
  });

  it('withholds the pane contract from the All Current Usage modal', () => {
    const { container } = renderFor('all', <CurrentWeekModal />);
    const card = container.querySelector('.modal-card')!;
    expect(card.className).toContain('modal-wide');
    expect(card.className).not.toContain('modal-pane-scroll');
  });

  // The All branch is the one that names its provider on the card; the
  // single-provider branch is already scoped by the board and never passed a
  // value. #556 S4 F7 deleted the attribute outright as unread, which turned
  // `e2e/period-native-vocabulary.spec.ts` red on main.
  it('publishes data-source only on the All Trend card', () => {
    for (const source of ['claude', 'codex'] as const) {
      const { container } = renderFor(source, <TrendModal />);
      expect(container.querySelector('.modal-card')!.hasAttribute('data-source')).toBe(false);
      cleanup();
    }
    const { container } = renderFor('all', <TrendModal />);
    expect(container.querySelector('.modal-card')).toHaveAttribute('data-source', 'all');
  });
});

// #556 S4 F8 — the modal half of the same rule the five panel tests assert:
// every All provider section exposes a level-3 heading naming that section,
// and the section's accessible name resolves THROUGH that heading via
// `aria-labelledby` rather than through a competing `aria-label` string. h3
// because a modal title is an h2, so a provider section sits one level below
// it; ids are surface-qualified so a panel and its modal can be mounted at
// once without colliding.
//
// Subsection headings inside these modals drop to h4 when embedded. Left at h3
// they would become apparent PEERS of the provider section that contains them,
// which is a worse hierarchy than no heading at all.
describe('All provider sections are named by their own heading (#556 S4)', () => {
  function assertHeadings(container: HTMLElement, pattern: RegExp, expectRegion: boolean) {
    const sections = container.querySelectorAll('[data-provider-section]');
    expect(sections.length).toBe(2);
    sections.forEach((s) => {
      if (expectRegion) expect(s.getAttribute('role')).toBe('region');
      const labelledBy = s.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(s.getAttribute('aria-label')).toBeNull();
      const heading = container.querySelector(`[id="${labelledBy}"]`);
      expect(heading).not.toBeNull();
      expect(heading!.tagName).toBe('H3');
      expect(heading!.textContent).toMatch(pattern);
    });
    const ids = [...container.querySelectorAll('h3[id]')].map((h) => h.id);
    expect(new Set(ids).size).toBe(ids.length);
  }

  it('names both Cache Report modal sections', () => {
    const composed = structuredClone(envelope);
    // #583 S3 §4: physical entry only — the All mirror publishes null.
    composed.sources!.codex.data!.cache_report = asCodexCacheReport(composed.cache_report!);
    act(() => {
      updateSnapshot(composed);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });
    const { container } = render(<CacheReportModal />);
    assertHeadings(container, /^(Claude|Codex) cache report detail$/, false);
  });

  it('names both Forecast modal sections', () => {
    const { container } = renderFor('all', <ForecastModal />);
    assertHeadings(container, /^(Claude|Codex) forecast detail$/, false);
  });

  // #556 S5 §4.3 — the same rule for the budget sections, on their own
  // selector so a budget card is never counted as a forecast section.
  it('names both Forecast modal budget sections', () => {
    const { container } = renderFor('all', <ForecastModal />);
    const sections = container.querySelectorAll('[data-budget-section]');
    expect(sections.length).toBe(2);
    sections.forEach((s) => {
      expect(s.getAttribute('role')).toBe('region');
      const labelledBy = s.getAttribute('aria-labelledby');
      expect(labelledBy).toMatch(/^budget-modal-(claude|codex)-heading$/);
      const heading = container.querySelector(`[id="${labelledBy}"]`);
      expect(heading).not.toBeNull();
      expect(heading!.tagName).toBe('H3');
      expect(heading!.textContent).toMatch(/^(Claude|Codex) budget$/);
    });
  });

  it('names both Trend modal sections', () => {
    const { container } = renderFor('all', <TrendModal />);
    assertHeadings(container, /^(Claude|Codex) \$ per 1% trend history$/, false);
  });

  it('names both Current Usage modal sections', () => {
    const { container } = renderFor('all', <CurrentWeekModal />);
    assertHeadings(
      container,
      /^(Claude subscription week|Codex native 7-day quota)$/,
      false,
    );
  });

  it('drops embedded subsection headings to h4 so they are not peers of their provider', () => {
    const { container } = renderFor('all', <TrendModal />);
    container.querySelectorAll('[data-provider-section]').forEach((s) => {
      // The section's OWN heading is the only h3 inside it; every heading the
      // embedded modal body renders is one level deeper.
      expect(s.querySelectorAll('h3').length).toBe(1);
    });
  });
});
