import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  combinedHeading,
  combinedPresentation,
  gateSessions,
  sourceDomainFreshness,
  warningForDomain,
  warningForSource,
} from './sourceGating';
import { resolveSourceView } from '../store/sourceView';
import {
  makeAllCombined,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeEmptyCombinedLeg,
  makeHydratingEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  AllSourceData,
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

describe('sourceDomainFreshness', () => {
  it('selects the requested domain independently from provider freshness', () => {
    const entry = makeCodexSourceEntry({
      freshness: 'stale',
      domain_freshness: {
        hero: 'stale',
        quota: 'stale',
        sessions: 'fresh',
      },
    });

    expect(sourceDomainFreshness(entry, 'hero')).toBe('stale');
    expect(sourceDomainFreshness(entry, 'quota')).toBe('stale');
    expect(sourceDomainFreshness(entry, 'sessions')).toBe('fresh');
  });

  it('falls back every domain to provider freshness for a legacy entry', () => {
    const entry = makeCodexSourceEntry({ freshness: 'stale' });
    delete entry.domain_freshness;

    expect(sourceDomainFreshness(entry, 'hero')).toBe('stale');
    expect(sourceDomainFreshness(entry, 'quota')).toBe('stale');
    expect(sourceDomainFreshness(entry, 'sessions')).toBe('stale');
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

  it('keeps Sessions rendered when only provider/other domains are stale', () => {
    const codex = makeCodexSourceEntry({
      availability: 'partial',
      freshness: 'stale',
      domain_freshness: {
        hero: 'stale',
        quota: 'stale',
        sessions: 'fresh',
      },
    });

    expect(gateSessions(viewFor('codex', makeClaudeSourceEntry(), codex))).toMatchObject({
      mode: 'render',
      noSuccessYet: false,
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


// ---- #556 S1 §4.4 — combinedPresentation -------------------------------

function allEntryWith(
  data: Partial<AllSourceData>,
  overrides: Partial<SourceEntry<AllSourceData>> = {},
): SourceEntry<AllSourceData> {
  const base = makeAllSourceEntry();
  return {
    ...base,
    ...overrides,
    data: { ...base.data!, ...data } as AllSourceData,
  };
}

describe('combinedPresentation', () => {
  it('publishes the figure, its legs and its contributors', () => {
    const combined = makeAllCombined();
    const presentation = combinedPresentation(allEntryWith({ combined }));

    expect(presentation.value).toEqual({
      costUsd: combined.cost_usd, totalTokens: combined.total_tokens,
    });
    expect(presentation.legs).toBe(combined.legs);
    expect(presentation.contributors).toEqual(['claude', 'codex']);
    expect(presentation.qualifications).toEqual([]);
    expect(presentation.unavailable).toBeNull();
  });

  it('counts a contributing leg that cannot name its own cycle', () => {
    // §3.7 rev5 — a `current` leg MAY omit `period`. It still counts toward the
    // sum and is still named in the heading; only its reset line is suppressed.
    const combined = makeAllCombined({
      legs: {
        claude: { state: 'current', cost_usd: 4, total_tokens: 40 },
        codex: makeAllCombined().legs.codex,
      },
    });
    const presentation = combinedPresentation(allEntryWith({ combined }));

    expect(presentation.contributors).toEqual(['claude', 'codex']);
    expect(presentation.legs?.claude.period).toBeUndefined();
  });

  it('excludes an EMPTY leg from the contributors', () => {
    const combined = makeAllCombined({
      legs: {
        claude: makeEmptyCombinedLeg(),
        codex: makeAllCombined().legs.codex,
      },
    });

    expect(combinedPresentation(allEntryWith({ combined })).contributors)
      .toEqual(['codex']);
  });

  it('reports both providers empty as no contributors at all', () => {
    const combined = makeAllCombined({
      legs: { claude: makeEmptyCombinedLeg(), codex: makeEmptyCombinedLeg() },
    });
    const presentation = combinedPresentation(allEntryWith({ combined }));

    expect(presentation.contributors).toEqual([]);
    // A published figure, not a withheld one: the value is real and it is zero.
    expect(presentation.value).toEqual({ costUsd: 0, totalTokens: 0 });
    expect(presentation.unavailable).toBeNull();
  });

  it('selects the typed reason when the figure is withheld', () => {
    const presentation = combinedPresentation(allEntryWith({
      combined: null,
      combined_unavailable: {
        code: 'multi_account_unsupported',
        message: 'Claude has 2 accounts on separate cycles.',
        causes: [{
          provider: 'claude',
          code: 'multi_account_unsupported',
          detail: { account_count: 2 },
        }],
      },
    }));

    expect(presentation.value).toBeNull();
    expect(presentation.legs).toBeNull();
    expect(presentation.unavailable).toEqual({
      code: 'multi_account_unsupported',
      message: 'Claude has 2 accounts on separate cycles.',
    });
  });

  it('prefers the typed reason over any hero warning on the same entry', () => {
    const presentation = combinedPresentation(allEntryWith(
      {
        combined: null,
        combined_unavailable: {
          code: 'claude_cycle_unresolved',
          message: "Claude's current subscription week could not be resolved.",
          causes: [{ provider: 'claude', code: 'claude_cycle_unresolved' }],
        },
      },
      { warnings: [{ code: 'other', message: 'a different reason', domain: 'hero' }] },
    ));

    expect(presentation.unavailable?.code).toBe('claude_cycle_unresolved');
  });

  it('falls back to the hero warning ONLY for a legacy envelope', () => {
    const legacy = allEntryWith(
      { combined: null },
      { warnings: [{ code: 'legacy_reason', message: 'legacy text', domain: 'hero' }] },
    );
    delete (legacy.data as Partial<AllSourceData>).combined_unavailable;

    expect(combinedPresentation(legacy).unavailable).toEqual({
      code: 'legacy_reason', message: 'legacy text',
    });
  });

  it('says nothing at all when there is no All entry', () => {
    expect(combinedPresentation(null)).toEqual({
      value: null, legs: null, contributors: [], qualifications: [],
      unavailable: null,
    });
  });

  it('never derives disclosure from hero freshness or source freshness', () => {
    // #556 B3 — the defect this seam exists to prevent. Both axes are stale and
    // the source is degraded, yet the figure is published and unqualified, so
    // nothing may say it is unavailable.
    const presentation = combinedPresentation(allEntryWith(
      { combined: makeAllCombined() },
      {
        freshness: 'stale',
        availability: 'partial',
        domain_freshness: { hero: 'stale', quota: 'stale', sessions: 'stale' },
        warnings: [{
          code: 'claude_week_unresolved',
          message: 'Claude data is not current.',
          domain: 'hero',
        }],
      },
    ));

    expect(presentation.value).not.toBeNull();
    expect(presentation.unavailable).toBeNull();
    expect(presentation.qualifications).toEqual([]);
  });

  // §4.3 — the two inverse backlog cases. The Codex ingest backlog reaches the
  // figure through `combined.qualifications`, which composition LIFTS from the
  // provider; the provider field stays published for the Codex tab's own use.
  it('follows combined.qualifications even when the provider field disagrees', () => {
    const codex = makeCodexSourceEntry();
    const entry = allEntryWith({
      combined: makeAllCombined({
        qualifications: [{
          code: 'codex_ingest_backlog',
          message: 'Codex has pending accounting to ingest.',
          provider: 'codex',
        }],
      }),
      providers: {
        claude: makeClaudeSourceEntry().data,
        // The provider field says there is NO backlog.
        codex: { ...codex.data!, ingest_backlog: undefined },
      },
    });

    expect(combinedPresentation(entry).qualifications.map((q) => q.code))
      .toEqual(['codex_ingest_backlog']);
  });

  it('renders no qualification carried only by the provider field', () => {
    const codex = makeCodexSourceEntry();
    const entry = allEntryWith({
      combined: makeAllCombined(),
      providers: {
        claude: makeClaudeSourceEntry().data,
        codex: {
          ...codex.data!,
          ingest_backlog: { files: 12, bytes: 4096, since: null },
        },
      },
    });

    expect(combinedPresentation(entry).qualifications).toEqual([]);
  });
});

describe('combinedHeading', () => {
  it.each([
    [['claude', 'codex'], 'COMBINED · CURRENT CYCLES'],
    [['claude'], 'CLAUDE · CURRENT CYCLE'],
    [['codex'], 'CODEX · CURRENT CYCLE'],
    [[], 'CURRENT CYCLES · NO DATA'],
  ] as const)('names its actual contributors: %s', (contributors, expected) => {
    expect(combinedHeading([...contributors])).toBe(expected);
  });
});


// #556 S1 acceptance 8 — a structural assertion, because the behavioural tests
// above can only show that today's inputs produce the right output. This one
// fails the moment a surface reaches for the aggregate freshness axis again,
// which is the specific mistake that produced B3 and would have survived every
// behavioural test written against a fixture where the axis happened to agree.
//
// It mechanises HALF of acceptance 8. The criterion forbids deriving combined
// disclosure from `sourceDomainFreshness(entry, 'hero')` OR from
// `entry.freshness`, and only the first half can be banned outright:
// `SourceStatusChip.tsx` reads `entry.freshness` legitimately, for the physical
// source's own fresh/stale label, so a blanket ban on the second would reject
// correct code. That half stays covered behaviourally — by the published-figure
// tests above and by the HeroStrip cases that pin a stale source beside a
// published number. A later reader should not take a green run here as proof
// that both halves are mechanised.
// Line comments and block comments, in that order: removing `/* ... */` first
// would let a `// ... /*` line open a block that swallows real code.
function stripComments(source: string): string {
  return source
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('//'))
    .join('\n')
    .replace(/\/\*[\s\S]*?\*\//g, '');
}

describe('combined disclosure has exactly one source', () => {
  const SRC = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)), '..',
  );
  const SURFACES = [
    'components/HeroStrip.tsx',
    'modals/CurrentWeekModal.tsx',
    'components/SourceStatusChip.tsx',
  ] as const;

  it.each(SURFACES)('%s derives it from combinedPresentation', (file) => {
    const source = readFileSync(path.join(SRC, file), 'utf8');

    expect(source).toContain('combinedPresentation');
  });

  it.each(SURFACES)('%s never calls sourceDomainFreshness', (file) => {
    // Comments may still NAME the function — that is how the reason it is
    // forbidden stays next to the code — so only a call is rejected. Both
    // comment forms are stripped: a `/* ... */` block naming the call would
    // otherwise redden this test for prose, which is the fastest way to teach
    // the next author to delete the explanation instead of the call.
    const code = stripComments(readFileSync(path.join(SRC, file), 'utf8'));

    expect(code).not.toMatch(/sourceDomainFreshness\s*\(/);
  });
});
