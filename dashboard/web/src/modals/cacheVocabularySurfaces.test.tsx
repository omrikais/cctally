// #443 S2 §4.4 — no Codex surface uses Claude cache vocabulary.
//
// The rule governs, not the enumeration: these tests exist per SURFACE, and a
// site the list misses is still in scope. They are paired by construction —
// every Codex assertion has a Claude counterpart over the same fixture, so a
// change that renamed the copy for BOTH providers fails here rather than
// passing as "Codex now says the right thing".
//
// jsdom cannot evaluate @media, so the mobile daily-card layout is exercised
// through the useIsMobile() React branch (stubMobileMedia), not through CSS.
// "Cached input" is a longer string than "Cache hit" in the same constrained
// header and the same 8-column table; that risk belongs to the real-browser QA
// gate, not here.
import { beforeEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { CacheReportModal } from './CacheReportModal';
import { CacheReportPanel } from '../panels/CacheReportPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import { stubMobileMedia } from '../test-utils/mobileMedia';
import envelopeFixture from '../../__tests__/fixtures/envelope.json';
import type {
  CacheReportEnvelope, DashboardSelection, Envelope,
} from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

function report(over: Partial<CacheReportEnvelope> = {}): CacheReportEnvelope {
  const days = Array.from({ length: 14 }, (_, i) => ({
    date: `2026-05-${String(i + 7).padStart(2, '0')}`,
    cache_hit_percent: 67 + (i % 5),
    cached_input_percent: 67 + (i % 5),
    input_tokens: 1_000, output_tokens: 200,
    cache_creation_tokens: 100, cache_read_tokens: 2_000,
    saved_usd: 1.2, wasted_usd: 0.15, net_usd: 1.05,
    anomaly_triggered: false, anomaly_reasons: [],
  }));
  return {
    window_days: 14,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: '2026-05-20',
      cache_hit_percent: 68, cached_input_percent: 68,
      baseline_median_percent: 67, delta_pp: -1,
      net_usd: 1.2, saved_usd: 1.35, wasted_usd: 0.15,
      anomaly_triggered: false, anomaly_reasons: [],
      baseline_daily_row_count: 13,
    },
    days,
    by_project: [{ key: 'cctally', cache_hit_percent: 52, net_usd: -0.18 }],
    by_model: [{ key: 'gpt-5', cache_hit_percent: 67, net_usd: 1.1 }],
    seven_day_net_usd: 5.94,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 28.4,
    fourteen_day_efficiency_ratio: 0.82,
    is_empty: false,
    ...over,
  };
}

/** The v4 Codex wire shape: one percent key plus null inapplicable figures. */
function codexReport(over: Partial<CacheReportEnvelope> = {}): CacheReportEnvelope {
  const base = report();
  const { cache_hit_percent: todayPercent, ...today } = base.today;
  return {
    ...base,
    today: {
      ...today,
      cached_input_percent: todayPercent,
      wasted_usd: null,
    },
    days: base.days.map(({ cache_hit_percent: percent, ...day }) => ({
      ...day,
      cached_input_percent: percent,
      wasted_usd: null,
    })),
    by_project: base.by_project.map(({ cache_hit_percent: percent, ...row }) => ({
      ...row,
      cached_input_percent: percent,
    })),
    by_model: base.by_model.map(({ cache_hit_percent: percent, ...row }) => ({
      ...row,
      cached_input_percent: percent,
    })),
    fourteen_day_efficiency_ratio: null,
    not_applicable: {
      wasted_usd: 'OpenAI charges no cache-write premium, so Codex has no wasted-cache figure.',
      fourteen_day_efficiency_ratio: 'Efficiency compares saved against wasted, and Codex has no wasted-cache figure.',
    },
    anomaly_predicates: ['cache_drop'],
    ...over,
  };
}

/** One envelope publishing BOTH providers' reports, so `all` composes two. */
function envFor(
  source: DashboardSelection,
  claude: CacheReportEnvelope = report(),
  codex: CacheReportEnvelope = codexReport(),
): Envelope {
  const e = {
    ...(structuredClone(envelopeFixture) as unknown as Envelope),
    ...makeSourceEnvelope(),
  };
  e.cache_report = claude;
  e.sources!.codex.data!.cache_report = codex;
  e.sources!.all.data!.providers.codex = e.sources!.codex.data;
  updateSnapshot(e);
  dispatch({ type: 'SET_ACTIVE_SOURCE', source });
  return e;
}

describe('#443 S2 vocabulary — panel', () => {
  it.each([
    ['claude', 'cache hit', 'cached input'],
    ['codex', 'cached input', 'cache hit'],
  ] as const)('%s headline names the provider figure', (source, present, absent) => {
    envFor(source);
    render(<CacheReportPanel />);
    const headline = document.querySelector('.cr-headline')!.textContent!;
    expect(headline.toLowerCase()).toContain(present);
    expect(headline.toLowerCase()).not.toContain(absent);
  });

  it('labels each provider summary in its own vocabulary under all sources', () => {
    envFor('all');
    render(<CacheReportPanel />);
    const claude = document.querySelector('[data-provider-section="claude"]')!;
    const codex = document.querySelector('[data-provider-section="codex"]')!;
    expect(claude.textContent).toContain('Cache hit');
    expect(claude.textContent).not.toContain('Cached input');
    expect(codex.textContent).toContain('Cached input');
    expect(codex.textContent).not.toContain('Cache hit');
  });
});

describe('#443 S2 vocabulary — modal', () => {
  it.each([
    ['claude', 'Cache hit % — 14-day timeline', 'Cached input %'],
    ['codex', 'Cached input % — 14-day timeline', 'Cache hit %'],
  ] as const)('%s timeline heading', (source, present, absent) => {
    envFor(source);
    render(<CacheReportModal />);
    expect(screen.getByText(new RegExp(present.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeInTheDocument();
    expect(document.querySelector('.crm-sh-timeline')!.textContent)
      .not.toContain(absent);
  });

  it.each([
    ['claude', 'Cache hit % timeline'],
    ['codex', 'Cached input % timeline'],
  ] as const)('%s sparkline accessible description', (source, label) => {
    envFor(source);
    render(<CacheReportModal />);
    expect(document.querySelector('.cr-spark')!.getAttribute('aria-label'))
      .toContain(label);
  });

  it.each([
    ['claude', 'Cache %', 'Reuse %'],
    ['codex', 'Reuse %', 'Cache %'],
  ] as const)('%s daily column header, desktop', (source, present, absent) => {
    stubMobileMedia(false);
    envFor(source);
    render(<CacheReportModal />);
    const headers = Array.from(document.querySelectorAll('.ch-table thead th'))
      .map((th) => th.textContent);
    expect(headers).toContain(present);
    expect(headers).not.toContain(absent);
  });

  it.each([
    ['claude', 'Cache %', 'Reuse %'],
    ['codex', 'Reuse %', 'Cache %'],
  ] as const)('%s daily column header, mobile cards', (source, present, absent) => {
    stubMobileMedia(true);
    envFor(source);
    render(<CacheReportModal />);
    const card = document.querySelectorAll('[data-testid="crm-daily-card"]')[0];
    expect(card.textContent).toContain(present);
    expect(card.textContent).not.toContain(absent);
  });

  it.each([
    ['claude', 'positive bars = caching helped', 'input reuse savings'],
    ['codex', 'bars = input reuse savings', 'caching helped'],
  ] as const)('%s net-bars caption', (source, present, absent) => {
    envFor(source);
    render(<CacheReportModal />);
    const head = document.querySelector('.crm-sh-net')!.textContent!;
    expect(head).toContain(present);
    expect(head).not.toContain(absent);
  });

  it.each([
    ['claude', 'Without caching', 'cache efficiency'],
    ['codex', 'Without input reuse', 'reuse efficiency'],
  ] as const)('%s counterfactual callout', (source, lead, efficiency) => {
    envFor(source);
    render(<CacheReportModal />);
    const callout = document.querySelector('.crm-counterfactual')!.textContent!;
    expect(callout).toContain(lead);
    expect(callout).toContain(efficiency);
  });

  it.each([
    ['claude', 'Cache hit', 'Cached input'],
    ['codex', 'Cached input', 'Cache hit'],
  ] as const)('%s spotlight stat key', (source, present, absent) => {
    envFor(source);
    render(<CacheReportModal />);
    const keys = Array.from(document.querySelectorAll('.crm-spotlight .k'))
      .map((k) => k.textContent);
    expect(keys).toContain(present);
    expect(keys).not.toContain(absent);
  });

  it('renders each provider section in its own vocabulary under all sources', () => {
    envFor('all');
    render(<CacheReportModal />);
    const claude = document.querySelector('[data-provider-section="claude"]')!;
    const codex = document.querySelector('[data-provider-section="codex"]')!;
    expect(claude.textContent).toContain('Cache hit % — 14-day timeline');
    expect(codex.textContent).toContain('Cached input % — 14-day timeline');
    expect(codex.textContent).not.toContain('Cache hit');
  });
});

// #443 S2 §3.3 / §4.4 + #465 — a Codex figure that does not exist must say so,
// and say why. The v4 wire publishes null values while the `not_applicable` map
// remains the authoritative source for the user-facing reason. Every assertion
// below therefore reads the map's own copy, not a client literal.
describe('#443 S2 not-applicable figures', () => {
  const MARKED = {
    wasted_usd: 'DISTINCT-WASTED-REASON',
    fourteen_day_efficiency_ratio: 'DISTINCT-EFFICIENCY-REASON',
  };

  it('marks the Wasted half of the spotlight pair not applicable under Codex', () => {
    envFor('codex', report(), codexReport({ not_applicable: MARKED }));
    render(<CacheReportModal />);
    const pair = Array.from(document.querySelectorAll('.crm-spotlight span'))
      .find((n) => n.querySelector('.k')?.textContent === 'Saved / Wasted')!;
    // Saved is a real Codex measurement and is kept.
    expect(pair.textContent).toContain('$1.35');
    expect(pair.textContent).not.toContain('$0.15');
    expect(pair.querySelector('.m-unavailable')).not.toBeNull();
    expect(screen.getAllByLabelText(MARKED.wasted_usd).length).toBeGreaterThan(0);
  });

  it('keeps both halves of the spotlight pair measured under Claude', () => {
    envFor('claude');
    render(<CacheReportModal />);
    const pair = Array.from(document.querySelectorAll('.crm-spotlight span'))
      .find((n) => n.querySelector('.k')?.textContent === 'Saved / Wasted')!;
    expect(pair.textContent).toContain('$1.35 / $0.15');
    expect(pair.querySelector('.m-unavailable')).toBeNull();
  });

  it('marks the efficiency callout not applicable under Codex', () => {
    envFor('codex', report(), codexReport({ not_applicable: MARKED }));
    render(<CacheReportModal />);
    const callout = document.querySelector('.crm-counterfactual')!;
    // A structural 1.0 ratio rendered as "100%" would be exactly the
    // fabricated measurement this session exists to remove.
    expect(callout.textContent).not.toMatch(/\d+%/);
    expect(callout.querySelector('.m-unavailable')).not.toBeNull();
    expect(callout.querySelector('.m-unavailable')!.getAttribute('aria-label'))
      .toBe(MARKED.fourteen_day_efficiency_ratio);
  });

  it('keeps the efficiency percentage under Claude', () => {
    envFor('claude');
    render(<CacheReportModal />);
    const callout = document.querySelector('.crm-counterfactual')!;
    expect(callout.textContent).toContain('82%');
    expect(callout.querySelector('.m-unavailable')).toBeNull();
  });

  it.each([false, true])('marks the daily Wasted cells not applicable under Codex (mobile=%s)', (mobile) => {
    stubMobileMedia(mobile);
    envFor('codex', report(), codexReport({ not_applicable: MARKED }));
    render(<CacheReportModal />);
    const row = document.querySelector(
      mobile ? '[data-testid="crm-daily-card"]' : '[data-testid="crm-daily-row"]',
    )!;
    // $0.15 is every fixture day's wasted figure; a bare dash would carry
    // none of the promised reason, so the reason itself is the assertion.
    expect(row.textContent).not.toContain('$0.15');
    expect(row.textContent).toContain('$1.20');   // Saved survives
    const na = row.querySelector('.m-unavailable')!;
    expect(na.getAttribute('aria-label')).toBe(MARKED.wasted_usd);
  });

  it.each([false, true])('keeps the daily Wasted cells measured under Claude (mobile=%s)', (mobile) => {
    stubMobileMedia(mobile);
    envFor('claude');
    render(<CacheReportModal />);
    const row = document.querySelector(
      mobile ? '[data-testid="crm-daily-card"]' : '[data-testid="crm-daily-row"]',
    )!;
    expect(row.textContent).toContain('$0.15');
    expect(row.querySelector('.m-unavailable')).toBeNull();
  });

  it('says nothing is inapplicable when the wire publishes no map', () => {
    // A pre-S2 Codex envelope carries no `not_applicable`, and absence must
    // carry the Claude meaning rather than defaulting to "not applicable".
    envFor('codex', report(), report());
    render(<CacheReportModal />);
    expect(document.querySelector('.crm-counterfactual .m-unavailable')).toBeNull();
    expect(document.querySelector('.crm-counterfactual')!.textContent)
      .toContain('82%');
  });
});

// #443 S2 §4.4 — the spotlight's reasons list prints the RAW predicate name
// beside an always-present `net < 0` threshold legend. Both are Claude-shaped:
// the raw name contradicts the "reuse drop" wording the contract fixes, and the
// `net < 0` legend describes a predicate that is not applicable to Codex at all.
describe('#443 S2 vocabulary — spotlight reasons list', () => {
  const anomalous = { anomaly_triggered: true, anomaly_reasons: ['cache_drop' as const] };

  it('keeps the raw predicate name and both thresholds under Claude', () => {
    const cr = report();
    envFor('claude', report({ today: { ...cr.today, ...anomalous } }));
    render(<CacheReportModal />);
    const reasons = document.querySelector('.crm-spotlight .reasons')!.textContent!;
    expect(reasons).toContain('cache_drop');
    expect(reasons).toContain('15pp drop, net < 0');
  });

  it('words the predicate as a reuse drop under Codex and drops the net legend', () => {
    const cr = codexReport();
    envFor('codex', report(), codexReport({ today: { ...cr.today, ...anomalous } }));
    render(<CacheReportModal />);
    const reasons = document.querySelector('.crm-spotlight .reasons')!.textContent!;
    expect(reasons).toContain('reuse drop');
    expect(reasons).not.toContain('cache_drop');
    // `net < 0` is not a Codex predicate that failed to run — it is not a
    // Codex predicate. Printing it as a live threshold is the same class of
    // false statement as labelling token reuse a cache hit.
    expect(reasons).not.toContain('net < 0');
    expect(reasons).toContain('15pp reuse drop');
  });
});
