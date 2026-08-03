import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import fixture from '../../__tests__/fixtures/envelope.json';
import { CacheReportPanel } from './CacheReportPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

// A snapshot carrying both the source fields AND the legacy `cache_report` object
// (the Claude forensics legacy fallback, §5.2).
function env(): Envelope {
  const result = {
    ...(structuredClone(fixture) as unknown as Envelope),
    ...makeSourceEnvelope(),
  };
  const codexReport = structuredClone(result.cache_report!);
  for (const row of [
    codexReport.today,
    ...codexReport.days,
    ...codexReport.by_project,
    ...codexReport.by_model,
  ]) {
    row.cached_input_percent = row.cache_hit_percent;
    delete row.cache_hit_percent;
  }
  codexReport.today.cached_input_percent = 42;
  codexReport.today.net_usd = 12.5;
  codexReport.today.wasted_usd = null;
  codexReport.fourteen_day_counterfactual_usd = 99;
  codexReport.fourteen_day_efficiency_ratio = null;
  for (const day of codexReport.days) day.wasted_usd = null;
  codexReport.not_applicable = {
    wasted_usd: 'OpenAI charges no cache-write premium, so Codex has no wasted-cache figure.',
    fourteen_day_efficiency_ratio: 'Efficiency compares saved against wasted, and Codex has no wasted-cache figure.',
  };
  codexReport.anomaly_predicates = ['cache_drop'];
  codexReport.days[0].cached_input_percent = 42;
  codexReport.days[0].net_usd = 12.5;
  result.sources!.codex.data!.cache_report = codexReport;
  result.sources!.all.data!.providers.codex = result.sources!.codex.data;
  return result;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('CacheReportPanel source labeling (§5.5 layer 2 / §6.6)', () => {
  it('Claude mode renders the panel bare (no provider source chip)', () => {
    updateSnapshot(env());
    render(<CacheReportPanel />);
    expect(screen.getByText('Cache Report', { selector: 'h2' })).toBeInTheDocument();
    expect(screen.queryByText('Claude', { selector: '.source-chip' })).toBeNull();
  });

  it('All mode keeps one shell with distinct labelled provider summaries', () => {
    updateSnapshot(env());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<CacheReportPanel />);
    expect(screen.getByText('Cache Report', { selector: 'h2' })).toBeInTheDocument();
    expect(document.querySelectorAll('#panel-cache-report')).toHaveLength(1);
    const claude = document.querySelector('[data-provider-section="claude"]');
    const codex = document.querySelector('[data-provider-section="codex"]');
    expect(claude?.textContent).toContain('Claude');
    expect(claude?.textContent).toContain('87%');
    expect(claude?.textContent).toContain('7d net');
    expect(claude?.textContent).toContain('+$3.10');
    expect(codex?.textContent).toContain('Codex');
    expect(codex?.textContent).toContain('42%');
    expect(codex?.textContent).toContain('7d net');
    expect(codex?.textContent).toContain('+$12.50');
    expect(document.querySelector('[data-provider-section="all"]')).toBeNull();
  });
});
