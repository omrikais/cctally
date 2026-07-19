import { describe, expect, it } from 'vitest';
import {
  PANEL_GATING,
  gatePanel,
  isPanelVisible,
  providerSections,
  resolvePanelData,
} from './sourceGating';
import { resolveSourceView, type SourceView } from '../store/sourceView';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeHydratingEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  DashboardSelection,
  Envelope,
  SourceEntry,
} from '../types/envelope';

interface ViewOpts {
  claude?: SourceEntry<unknown>;
  codex?: SourceEntry<unknown>;
  cacheReport?: unknown;
}

function viewFor(selection: DashboardSelection, opts: ViewOpts = {}): SourceView {
  const claude = (opts.claude ?? makeClaudeSourceEntry()) as ReturnType<typeof makeClaudeSourceEntry>;
  const codex = (opts.codex ?? makeCodexSourceEntry()) as ReturnType<typeof makeCodexSourceEntry>;
  const slice = makeSourceEnvelope({
    sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
  });
  const env = {
    ...slice,
    cache_report: opts.cacheReport ?? { is_empty: false },
  } as unknown as Envelope;
  return resolveSourceView(env, selection);
}

describe('PANEL_GATING table shape (§5.5)', () => {
  it('maps each panel to its capability key', () => {
    expect(PANEL_GATING['current-week'].capability).toBe('hero');
    expect(PANEL_GATING.forecast.capability).toBe('hero');
    expect(PANEL_GATING.trend.capability).toBe('hero');
    expect(PANEL_GATING.daily.capability).toBe('daily');
    expect(PANEL_GATING.weekly.capability).toBe('weekly');
    expect(PANEL_GATING.monthly.capability).toBe('monthly');
    expect(PANEL_GATING.sessions.capability).toBe('sessions');
    expect(PANEL_GATING.projects.capability).toBe('projects');
    expect(PANEL_GATING.blocks.capability).toBe('quota');
    expect(PANEL_GATING['cache-report'].capability).toBe('forensics');
    expect(PANEL_GATING.alerts.capability).toBe('alerts');
  });

  it('only cache-report carries a legacy fallback', () => {
    for (const [panel, spec] of Object.entries(PANEL_GATING)) {
      if (panel === 'cache-report') expect(spec.legacyFallback).toBeTypeOf('function');
      else expect(spec.legacyFallback).toBeUndefined();
    }
  });
});

describe('gatePanel — Claude single-source', () => {
  it('renders supported panels whose path resolves', () => {
    const v = viewFor('claude');
    for (const p of ['sessions', 'daily', 'weekly', 'monthly', 'projects', 'blocks', 'forecast', 'trend'] as const) {
      expect(gatePanel(v, p).mode).toBe('render');
    }
  });

  it('renders cache-report via the legacy fallback when cache_report exists', () => {
    expect(gatePanel(viewFor('claude'), 'cache-report').mode).toBe('render');
  });

  it('keeps cache-report as a SKELETON (not hidden) when a healthy Claude entry has a null legacy cache_report', () => {
    // Cold start / transient sub-build failure: the Claude entry is otherwise
    // healthy but the top-level cache_report hasn't (re)built yet. The panel
    // must keep a skeleton placeholder so it does not un-mount and reflow the
    // digit-shortcut map (§6.11). `null` cache_report resolves to `undefined`
    // via the `?? undefined` fallback.
    const env = { ...makeSourceEnvelope(), cache_report: null } as unknown as Envelope;
    expect(gatePanel(resolveSourceView(env, 'claude'), 'cache-report').mode).toBe('skeleton');
    // And it is still VISIBLE (skeleton !== hidden) so the digit map is stable.
    expect(isPanelVisible(resolveSourceView(env, 'claude'), 'cache-report')).toBe(true);
  });

  it('keeps cache-report as a SKELETON on a pre-S4 legacy Claude envelope with no cache_report', () => {
    // The entry==null legacy-compatible Claude path: no source bundle at all,
    // no cache_report — still a skeleton placeholder, never hidden.
    const env = { cache_report: null } as unknown as Envelope;
    expect(gatePanel(resolveSourceView(env, 'claude'), 'cache-report').mode).toBe('skeleton');
  });

  it('still HIDES cache-report when Claude forensics is deferred/not_applicable (skeleton is scoped to healthy entries)', () => {
    for (const status of ['deferred', 'not_applicable'] as const) {
      const claude = makeClaudeSourceEntry({
        capabilities: { ...makeClaudeSourceEntry().capabilities, forensics: { status } },
      });
      const env = { ...makeSourceEnvelope({
        sources: { claude, codex: makeCodexSourceEntry(), all: makeAllSourceEntry(claude, makeCodexSourceEntry()) },
      }), cache_report: null } as unknown as Envelope;
      expect(gatePanel(resolveSourceView(env, 'claude'), 'cache-report').mode).toBe('hidden');
    }
  });
});

describe('gatePanel — Codex single-source (ghost-panel prevention, §5.5/§6.6)', () => {
  it('hides forecast/trend under Codex (supported hero capability, absent path)', () => {
    const v = viewFor('codex');
    expect(gatePanel(v, 'forecast').mode).toBe('hidden');
    expect(gatePanel(v, 'trend').mode).toBe('hidden');
  });

  it('hides cache-report under Codex (forensics supported, no path, no fallback)', () => {
    expect(gatePanel(viewFor('codex'), 'cache-report').mode).toBe('hidden');
  });

  it('renders Codex daily/weekly/monthly/sessions/projects/blocks/alerts', () => {
    const v = viewFor('codex');
    for (const p of ['daily', 'weekly', 'monthly', 'sessions', 'projects', 'blocks', 'alerts'] as const) {
      expect(gatePanel(v, p).mode).toBe('render');
    }
  });
});

describe('gatePanel — capability status precedence (§5.5 Layer 1/3)', () => {
  it('deferred / not_applicable → hidden', () => {
    for (const status of ['deferred', 'not_applicable'] as const) {
      const codex = makeCodexSourceEntry({
        capabilities: { ...makeCodexSourceEntry().capabilities, daily: { status } },
      });
      expect(gatePanel(viewFor('codex', { codex }), 'daily').mode).toBe('hidden');
    }
  });

  it('runtime-unavailable capability → degraded with warning', () => {
    const codex = makeCodexSourceEntry({
      capabilities: { ...makeCodexSourceEntry().capabilities, daily: { status: 'unavailable' } },
      warnings: [{ code: 'daily_unavailable', message: 'Daily is temporarily unavailable.' }],
    });
    const gate = gatePanel(viewFor('codex', { codex }), 'daily');
    expect(gate.mode).toBe('degraded');
    expect(gate.warning?.code).toBe('daily_unavailable');
  });

  it('unknown capability status → degrade generically (never throw)', () => {
    const codex = makeCodexSourceEntry({
      capabilities: {
        ...makeCodexSourceEntry().capabilities,
        daily: { status: 'weird-future-status' as never },
      },
    });
    const gate = gatePanel(viewFor('codex', { codex }), 'daily');
    expect(gate.mode).toBe('degraded');
    expect(gate.warning).toBeNull();
  });
});

describe('gatePanel — availability precedence (§5.5 Layer 3)', () => {
  it('entry-level unavailable → degraded with warning, noSuccessYet from last_success_at', () => {
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'source_ingest_failed', message: 'Source ingest failed.' }],
      last_success_at: null,
    });
    const gate = gatePanel(viewFor('codex', { codex }), 'daily');
    expect(gate.mode).toBe('degraded');
    expect(gate.warning?.code).toBe('source_ingest_failed');
    expect(gate.noSuccessYet).toBe(true);
  });

  it('unavailable with last_success_at null → noSuccessYet true regardless of warning presence', () => {
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [],
      last_success_at: null,
    });
    // warnings empty + last_success null + caps {} + data null would be
    // hydrating; add a warning to make it a true unavailable state.
    const codex2 = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'w', message: 'm' }],
      last_success_at: null,
    });
    expect(gatePanel(viewFor('codex', { codex: codex2 }), 'daily').noSuccessYet).toBe(true);
    void codex;
  });

  it('partial/stale → degraded but retains data (still visible)', () => {
    const codex = makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'stale',
      warnings: [{ code: 'source_ingest_contended', message: 'Source ingest is in progress.' }],
      last_success_at: '2026-04-24T13:00:00Z',
    });
    const gate = gatePanel(viewFor('codex', { codex }), 'daily');
    expect(gate.mode).toBe('degraded');
    expect(gate.warning?.code).toBe('source_ingest_contended');
    expect(gate.noSuccessYet).toBe(false);
    expect(isPanelVisible(viewFor('codex', { codex }), 'daily')).toBe(true);
    // The data path still resolves (retained generation).
    expect(resolvePanelData(viewFor('codex', { codex }), 'daily')).not.toBeNull();
  });

  it('hydrating entry → skeleton', () => {
    const codex = makeHydratingEntry() as unknown as SourceEntry<unknown>;
    expect(gatePanel(viewFor('codex', { codex }), 'daily').mode).toBe('skeleton');
  });
});

describe('gateSingleSource — wholly-unavailable Codex: absent-path hidden wins (§5.5)', () => {
  // The QA P0 shape: a Codex source that wholly failed to build — availability
  // 'unavailable', capabilities {}, data null, a non-empty warning (so it is NOT
  // hydrating), last_success_at null.
  function unavailCodex() {
    return makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'source_build_failed', message: 'Source data could not be built.' }],
      last_success_at: null,
    });
  }

  it('hides forecast/trend/cache-report (no Codex path) even though the entry is unavailable', () => {
    const v = viewFor('codex', { codex: unavailCodex() });
    for (const p of ['forecast', 'trend', 'cache-report'] as const) {
      expect(gatePanel(v, p).mode).toBe('hidden');
      expect(isPanelVisible(v, p)).toBe(false);
    }
  });

  it('degrades (not hides) the panels Codex CAN have — daily/weekly/monthly/sessions/projects/blocks/hero/alerts', () => {
    const v = viewFor('codex', { codex: unavailCodex() });
    for (const p of ['daily', 'weekly', 'monthly', 'sessions', 'projects', 'blocks', 'current-week', 'alerts'] as const) {
      const gate = gatePanel(v, p);
      expect(gate.mode).toBe('degraded');
      expect(gate.warning?.code).toBe('source_build_failed');
      expect(gate.noSuccessYet).toBe(true);
    }
  });

  it('same absent-path distinction under partial availability with retained data', () => {
    const codex = makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'stale',
      warnings: [{ code: 'source_ingest_contended', message: 'Source ingest is in progress.' }],
      last_success_at: '2026-04-24T13:00:00Z',
    });
    const v = viewFor('codex', { codex });
    // Absent-path panels hide regardless of availability/freshness.
    for (const p of ['forecast', 'trend', 'cache-report'] as const) {
      expect(gatePanel(v, p).mode).toBe('hidden');
    }
    // Panels Codex can have keep retained data with a degraded chip.
    expect(gatePanel(v, 'daily').mode).toBe('degraded');
  });

  it('the All-mode Codex provider-child obeys the same distinction (unavailable Codex)', () => {
    const codex = unavailCodex();
    const claude = makeClaudeSourceEntry();
    const slice = makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    });
    const env = { ...slice, cache_report: { is_empty: false } } as unknown as Envelope;
    const view = resolveSourceView(env, 'all');
    const forecast = Object.fromEntries(
      providerSections(view, 'forecast').map((s) => [s.source, s.gate.mode]),
    );
    // Codex forecast child hidden (absent path); Claude child renders.
    expect(forecast.codex).toBe('hidden');
    expect(forecast.claude).toBe('render');
    const daily = Object.fromEntries(
      providerSections(view, 'daily').map((s) => [s.source, s.gate.mode]),
    );
    // Codex daily child degraded (path exists, source unavailable); Claude renders.
    expect(daily.codex).toBe('degraded');
    expect(daily.claude).toBe('render');
  });
});

describe('providerSections / gatePanel — All mode (§5.5 Layer 2)', () => {
  it('gates each provider child through the table (Codex forecast hidden, Claude render)', () => {
    const sections = providerSections(viewFor('all'), 'forecast');
    const bySource = Object.fromEntries(sections.map((s) => [s.source, s.gate.mode]));
    expect(bySource.claude).toBe('render');
    expect(bySource.codex).toBe('hidden');
    // The aggregate panel is visible (at least one provider renders).
    expect(gatePanel(viewFor('all'), 'forecast').mode).toBe('render');
  });

  it('the legacy fallback applies only to the Claude child (cache-report)', () => {
    const sections = providerSections(viewFor('all'), 'cache-report');
    const bySource = Object.fromEntries(sections.map((s) => [s.source, s.gate.mode]));
    expect(bySource.claude).toBe('render'); // via env.cache_report fallback
    expect(bySource.codex).toBe('hidden'); // no Codex forensics domain
  });

  it("the `all` entry's own capabilities gate only combined-hero + alerts", () => {
    // current-week (combined hero) and alerts render from the `all` entry.
    expect(gatePanel(viewFor('all'), 'current-week').mode).toBe('render');
    expect(gatePanel(viewFor('all'), 'alerts').mode).toBe('render');
  });

  it('providerSections returns [] for a single-source view', () => {
    expect(providerSections(viewFor('claude'), 'daily')).toEqual([]);
    expect(providerSections(viewFor('codex'), 'daily')).toEqual([]);
  });
});

describe('gatePanel — warning-domain routing (#312)', () => {
  function partialCodex(warnings: SourceEntry<unknown>['warnings']) {
    return makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'fresh',
      warnings,
    });
  }

  it('degrades only Projects for a projects-domain metadata warning', () => {
    const codex = partialCodex([
      {
        code: 'codex_metadata_incomplete',
        message: 'Project metadata is incomplete.',
        domain: 'projects',
      },
    ]);
    const view = viewFor('codex', { codex });

    expect(gatePanel(view, 'projects').mode).toBe('degraded');
    expect(gatePanel(view, 'projects').warning?.code).toBe('codex_metadata_incomplete');
    expect(gatePanel(view, 'daily').mode).toBe('render');
    expect(gatePanel(view, 'blocks').mode).toBe('render');
  });

  it('treats ingest, read_model, absent, and unknown warning domains as source-wide', () => {
    for (const domain of ['ingest', 'read_model', undefined, 'future-domain']) {
      const codex = partialCodex([
        { code: 'source_problem', message: 'Source problem.', ...(domain === undefined ? {} : { domain }) },
      ]);
      const view = viewFor('codex', { codex });
      expect(gatePanel(view, 'daily').mode).toBe('degraded');
      expect(gatePanel(view, 'projects').mode).toBe('degraded');
    }
  });

  it('prefers a source-wide warning over an earlier scoped warning', () => {
    const codex = partialCodex([
      { code: 'projects_first', message: 'Projects only.', domain: 'projects' },
      { code: 'ingest_second', message: 'Source-wide.', domain: 'ingest' },
    ]);
    const daily = gatePanel(viewFor('codex', { codex }), 'daily');
    expect(daily.mode).toBe('degraded');
    expect(daily.warning?.code).toBe('ingest_second');
  });
});
