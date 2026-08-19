// #620 S1 D4 (F9) / A14 — the single-source forecast tile discloses staleness.
//
// The panel already computed `presentationForecastComposition(...)` and then
// used it only on the All tab, so a Claude or Codex tab rendered a projection
// from a stale, degraded or capability-limited source with no disclosure at
// all, while the All tab's card for that very same source stated it. These
// tests compare the two surfaces FIELD FOR FIELD — the section status token and
// the section reason sentence — rather than looking for a similar-looking chip,
// because a chip that merely resembles the other one is what A14 rules out.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ForecastPanel } from './ForecastPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  ClaudeSourceData,
  Envelope,
  ForecastEnvelope,
  SourceEntry,
} from '../types/envelope';

const FORECAST: ForecastEnvelope = {
  verdict: 'ok',
  week_avg_projection_pct: 61,
  recent_24h_projection_pct: 64,
  budget_100_per_day_usd: 4.2,
  budget_90_per_day_usd: 3.1,
  confidence: 'high',
  confidence_score: 3,
  explain: {},
};

function envWithClaude(claude: SourceEntry<ClaudeSourceData>): Envelope {
  const slice = makeSourceEnvelope();
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: FORECAST,
    trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [],
    },
    ...slice,
    sources: { ...slice.sources, claude, codex: makeCodexSourceEntry() },
  } as unknown as Envelope;
}

// The four inputs D4 names, each pinned to one provider entry. Every case must
// make `providerSection` leave `available`, and each does it through a
// different leg of that function, so a fix that only handles warnings cannot
// pass.
const CASES: Array<{ name: string; entry: SourceEntry<ClaudeSourceData> }> = [
  {
    name: 'stale hero freshness',
    entry: makeClaudeSourceEntry({
      domain_freshness: { hero: 'stale', quota: 'fresh', sessions: 'fresh' },
    }),
  },
  {
    name: 'stale quota freshness',
    entry: makeClaudeSourceEntry({
      domain_freshness: { hero: 'fresh', quota: 'stale', sessions: 'fresh' },
    }),
  },
  {
    name: 'ingest warning',
    entry: makeClaudeSourceEntry({
      warnings: [{ code: 'ingest_behind', message: 'Claude ingest is behind.', domain: 'ingest' }],
    }),
  },
  {
    name: 'deferred budget capability',
    entry: makeClaudeSourceEntry({
      capabilities: {
        ...makeClaudeSourceEntry().capabilities,
        budget: { status: 'deferred', semantics: 'Budget status is still being computed.' },
      },
    }),
  },
];

function renderOn(source: 'claude' | 'all', claude: SourceEntry<ClaudeSourceData>) {
  _resetForTests();
  updateSnapshot(envWithClaude(claude));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source });
  return render(<ForecastPanel />);
}

/** The two fields A14 pins. Read from the Claude region on either surface. */
function claudeSectionFields(container: HTMLElement): {
  status: string | null;
  reason: string | null;
} {
  const scope = container.querySelector('[data-provider-section="claude"]') ?? container;
  return {
    status: scope.querySelector('.provider-section-status')?.textContent ?? null,
    reason: scope.querySelector('.provider-section-reason')?.textContent ?? null,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#620 S1 D4 — the single-source tile discloses staleness', () => {
  it.each(CASES)('$name renders the same fields on the tab as on the All card', ({ entry }) => {
    const all = renderOn('all', entry);
    const allFields = claudeSectionFields(all.container);
    all.unmount();

    const tab = renderOn('claude', entry);
    const tabFields = claudeSectionFields(tab.container);

    // Unconditional precondition: the All card must actually be disclosing
    // something, or the equality below would be satisfied by two blanks.
    expect(allFields.status).toBe('degraded');
    expect(allFields.reason).not.toBeNull();
    expect(allFields.reason).not.toBe('');

    expect(tabFields.status).toBe(allFields.status);
    expect(tabFields.reason).toBe(allFields.reason);
  });

  it('a healthy source discloses nothing on either surface', () => {
    const healthy = makeClaudeSourceEntry();
    const all = renderOn('all', healthy);
    expect(claudeSectionFields(all.container)).toEqual({ status: null, reason: null });
    all.unmount();

    const tab = renderOn('claude', healthy);
    expect(claudeSectionFields(tab.container)).toEqual({ status: null, reason: null });
  });
});
