import { describe, expect, it } from 'vitest';
import { gateSessions, warningForDomain, warningForSource } from './sourceGating';
import { resolveSourceView } from '../store/sourceView';
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
  SourceWarning,
} from '../types/envelope';

function warnings(...domains: Array<string | undefined>): SourceWarning[] {
  return domains.map((domain, index) => ({
    code: `warning-${index}`,
    message: `warning ${domain ?? 'missing'}`,
    ...(domain === undefined ? {} : { domain }),
  }));
}

describe('source warning selection', () => {
  it.each([undefined, 'ingest', 'read_model', 'future-domain'])(
    'treats %s as source-wide for every panel',
    (domain) => {
      const sourceWide = warnings(domain)[0];
      expect(warningForDomain([sourceWide], 'daily')).toBe(sourceWide);
      expect(warningForDomain([sourceWide], 'projects')).toBe(sourceWide);
    },
  );

  it('prioritizes a source-wide warning even when a scoped warning appears first', () => {
    const [scoped, sourceWide] = warnings('projects', 'read_model');
    expect(warningForSource([scoped, sourceWide])).toBe(sourceWide);
    expect(warningForDomain([scoped, sourceWide], 'projects')).toBe(sourceWide);
  });

  it('keeps known capability warnings scoped to their own panel', () => {
    const projects = warnings('projects')[0];
    expect(warningForDomain([projects], 'daily')).toBeNull();
    expect(warningForDomain([projects], 'projects')).toBe(projects);
    expect(warningForSource([projects])).toBe(projects);
  });
});

function viewFor(
  selection: DashboardSelection,
  claude: SourceEntry<unknown> = makeClaudeSourceEntry(),
  codex: SourceEntry<unknown> = makeCodexSourceEntry(),
) {
  const slice = makeSourceEnvelope({
    sources: {
      claude: claude as ReturnType<typeof makeClaudeSourceEntry>,
      codex: codex as ReturnType<typeof makeCodexSourceEntry>,
      all: makeAllSourceEntry(
        claude as ReturnType<typeof makeClaudeSourceEntry>,
        codex as ReturnType<typeof makeCodexSourceEntry>,
      ),
    },
  });
  return resolveSourceView(slice as unknown as Envelope, selection);
}

describe('gateSessions', () => {
  it('renders healthy provider sessions', () => {
    expect(gateSessions(viewFor('claude')).mode).toBe('render');
    expect(gateSessions(viewFor('codex')).mode).toBe('render');
  });

  it('preserves the loading skeleton before a provider has ingested', () => {
    const hydrating = makeHydratingEntry() as SourceEntry<unknown>;
    expect(gateSessions(viewFor('codex', makeClaudeSourceEntry(), hydrating)).mode).toBe('skeleton');
  });

  it('preserves a truthful degraded state for unavailable sessions', () => {
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      last_success_at: null,
      warnings: warnings('ingest'),
    });
    expect(gateSessions(viewFor('codex', makeClaudeSourceEntry(), codex))).toMatchObject({
      mode: 'degraded',
      noSuccessYet: true,
      warning: { message: 'warning ingest' },
    });
  });

  it('retains partial stale session data while surfacing its warning', () => {
    const codex = makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'stale',
      warnings: warnings('read_model'),
    });
    expect(gateSessions(viewFor('codex', makeClaudeSourceEntry(), codex))).toMatchObject({
      mode: 'degraded',
      noSuccessYet: false,
      warning: { message: 'warning read_model' },
    });
  });

  it('hides explicitly deferred sessions', () => {
    const codex = makeCodexSourceEntry({
      capabilities: {
        ...makeCodexSourceEntry().capabilities,
        sessions: { status: 'deferred' },
      },
    });
    expect(gateSessions(viewFor('codex', makeClaudeSourceEntry(), codex)).mode).toBe('hidden');
  });

  it('keeps All visible when either provider has sessions and skeletonizes only when both hydrate', () => {
    const unavailable = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      last_success_at: null,
      warnings: warnings('ingest'),
    });
    expect(gateSessions(viewFor('all', makeClaudeSourceEntry(), unavailable)).mode).toBe('render');

    const hydrating = makeHydratingEntry() as SourceEntry<unknown>;
    expect(gateSessions(viewFor('all', hydrating, hydrating)).mode).toBe('skeleton');
  });
});
