// #294 S5 — WIRE-SHAPE GUARD for the S4 source contract.
//
// The client historically modeled a PHANTOM nested shape
// (`env.sources.sources[<source>]`) that the server never emits. The real S4
// serializer (`bin/_cctally_dashboard_envelope.py::_source_bundle_to_envelope`,
// spread into the envelope at its call site via `envelope.update(...)`) puts the
// four source fields at the envelope TOP LEVEL and makes `env.sources` the FLAT
// per-source map `{claude, codex, all}` of `SourceEntry` objects.
//
// This guard transcribes that serializer: it fails loudly if either client
// fixture convention drifts back to the nested shape. Both fixtures
// (`__tests__/fixtures/envelope.json` and the `test-utils/sourceEnvelope.ts`
// builders) must encode the flat/top-level shape so unit tests can never again
// validate a wire shape the server does not produce.
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fixture from './fixtures/envelope.json';
import { makeSourceEnvelope } from '../src/test-utils/sourceEnvelope';
import type { AllSourceData } from '../src/types/envelope';

describe('S4 source envelope wire shape (guard)', () => {
  it('the JSON fixture spreads the source fields at the envelope TOP level', () => {
    const env = fixture as Record<string, unknown>;

    // The four bundle fields are TOP-LEVEL siblings, not nested under `sources`.
    // #556 S2 §3.9 — the fixture tracks the CURRENT server version (9 -> 10 for
    // #583 S3's nulled All provider mirror; 8 -> 9 was #564's decorated Codex
    // fallback totals). The assertion is about placement, not about the
    // number: no production client branches on it.
    expect(env.source_schema_version).toBe(11);
    expect(env.default_source).toBe('claude');
    expect(env.source_order).toEqual(['claude', 'codex', 'all']);
  });

  it('`env.sources` is the FLAT per-source map, with NO phantom `sources` nesting', () => {
    const env = fixture as { sources?: Record<string, unknown> };
    const sources = env.sources ?? {};

    // Flat map keyed by the three selections.
    expect(Object.keys(sources).sort()).toEqual(['all', 'claude', 'codex']);

    // The phantom nested key must NOT exist.
    expect('sources' in sources).toBe(false);
    expect('source_schema_version' in sources).toBe(false);

    // Each value is a SourceEntry — assert the discriminating field directly on
    // the flat path the runtime reads (`env.sources.claude.availability`).
    const claude = sources.claude as {
      availability?: unknown;
      domain_freshness?: unknown;
    } | undefined;
    const codex = sources.codex as {
      availability?: unknown;
      domain_freshness?: unknown;
    } | undefined;
    const all = sources.all as {
      availability?: unknown;
      domain_freshness?: unknown;
    } | undefined;
    expect(typeof claude?.availability).toBe('string');
    expect(typeof codex?.availability).toBe('string');
    expect(typeof all?.availability).toBe('string');
    expect(claude?.domain_freshness).toEqual({
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    });
    expect(codex?.domain_freshness).toEqual({
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    });
    expect(all?.domain_freshness).toEqual({
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    });
  });

  it('the test-utils builder convention encodes the SAME flat/top-level shape', () => {
    const slice = makeSourceEnvelope() as unknown as Record<string, unknown>;

    // Top-level siblings, mirroring the JSON fixture + the real serializer.
    // #556 S2 §3.9 — tracks the server's current version, exactly as the JSON
    // fixture above does. Keeping the two in step is the invariant; the
    // number is not.
    expect(slice.source_schema_version).toBe(11);
    expect(slice.default_source).toBe('claude');
    expect(slice.source_order).toEqual(['claude', 'codex', 'all']);

    const sources = slice.sources as Record<string, unknown>;
    expect(Object.keys(sources).sort()).toEqual(['all', 'claude', 'codex']);
    // No phantom nesting in the builder either.
    expect('sources' in sources).toBe(false);
    expect((sources.claude as { availability?: unknown }).availability).toBe('ok');
    expect((sources.codex as { domain_freshness?: unknown }).domain_freshness).toEqual({
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    });
  });
});

// #556 S1 Unit 2 — the v5 combined contract, checked against a REAL captured
// envelope rather than against a client fixture. `tests/fixtures/dashboard/
// <scenario>/golden-data.json` is the byte-stable `/api/data` body the Python
// harness diffs, so it is the one artifact in this repository that cannot drift
// from what the server emits. Transcribing the TypeScript interfaces from a
// client fixture is how the #294 S5 phantom nested bundle happened.
//
// The assertions below read the golden THROUGH `AllSourceData`, so a missing or
// wrong interface field is a `tsc --noEmit` failure in the frontend harness's
// typecheck leg, not merely an untyped runtime read that would pass either way.
//
// The path is assembled at RUNTIME. `new URL('<literal>', import.meta.url)` is
// statically analysed by Vite and rewritten into an asset import, which its
// `server.fs` allowlist then denies for a file outside `dashboard/web`.
const GOLDEN_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..', '..', '..', 'tests', 'fixtures', 'dashboard',
);

function allSourceData(scenario: string): AllSourceData {
  const golden = JSON.parse(readFileSync(
    path.join(GOLDEN_ROOT, scenario, 'golden-data.json'), 'utf8',
  )) as { sources: { all: { data: AllSourceData } } };
  return golden.sources.all.data;
}

describe('v5 combined wire shape (guard)', () => {
  it('a published combined carries legs, each naming its own cycle', () => {
    const combined = allSourceData('all-combined').combined;
    if (combined == null) throw new Error('all-combined must publish a figure');

    expect(typeof combined.cost_usd).toBe('number');
    expect(typeof combined.total_tokens).toBe('number');
    expect(Object.keys(combined.legs).sort()).toEqual(['claude', 'codex']);
    for (const [provider, kind] of [
      ['claude', 'subscription_week'],
      ['codex', 'native_7_day_cycle'],
    ] as const) {
      const leg = combined.legs[provider];
      expect(leg.state).toBe('current');
      expect(typeof leg.cost_usd).toBe('number');
      expect(typeof leg.total_tokens).toBe('number');
      expect(leg.period?.kind).toBe(kind);
      expect(typeof leg.period?.label).toBe('string');
      // ONE parser suffices: both bounds are always `...Z`, on both providers.
      expect(leg.period?.start_at).toMatch(/Z$/);
      expect(leg.period?.end_at).toMatch(/Z$/);
    }
    expect(combined.cost_usd).toBeCloseTo(
      combined.legs.claude.cost_usd + combined.legs.codex.cost_usd, 9);
    expect(combined.total_tokens).toBe(
      combined.legs.claude.total_tokens! + combined.legs.codex.total_tokens!);
  });

  it('omits qualifications and combined_unavailable when inapplicable', () => {
    const data = allSourceData('all-combined');

    expect('combined_unavailable' in data).toBe(false);
    expect(data.combined_unavailable).toBeUndefined();
    expect('qualifications' in (data.combined as object)).toBe(false);
  });

  it('a decorated provider publishes certified account-cycle contributions', () => {
    const data = allSourceData('all-combined-decorated');

    expect(data.combined?.total_tokens).toBeNull();
    const claude = data.combined?.legs.claude;
    expect(claude?.scope).toBe('account_cycles');
    expect(claude?.total_tokens).toBeNull();
    expect(claude?.accounts?.map((row) => row.account_key)).toEqual([
      'a'.repeat(32), 'b'.repeat(32), 'unattributed',
    ]);
    expect(claude?.cost_usd).toBeCloseTo(
      claude!.accounts!.reduce((sum, row) => sum + row.cost_usd, 0), 9,
    );
    expect(data.combined_unavailable).toBeUndefined();
  });

  it('withholds a decorated provider when one account cost is unresolved', () => {
    const data = allSourceData('all-combined-account-unresolved');

    expect(data.combined).toBeNull();
    expect(data.combined_unavailable?.code).toBe('account_cost_unresolved');
    expect(data.combined_unavailable?.causes).toEqual([{
      provider: 'claude', code: 'account_cost_unresolved',
    }]);
  });

  it('lists EVERY co-occurring cause, not just the winner', () => {
    const data = allSourceData('codex-cache-active');

    expect(data.combined).toBeNull();
    expect(data.combined_unavailable?.causes.map((cause) => cause.code)).toEqual([
      'codex_projection_incoherent', 'codex_cycle_unavailable',
    ]);
  });
});

// #583 S3 §9 — the DECODED half of the payload reduction. Compression cannot
// touch what the browser allocates after parsing; only removing the duplicated
// subtree can, which is why this is measured apart from the wire-byte gate.
describe('#583 S3 — each provider domain is published exactly once', () => {
  const nodes = (v: unknown): number => (
    v === null || typeof v !== 'object'
      ? 1
      : 1 + Object.values(v as object).reduce<number>((n, x) => n + nodes(x), 0)
  );

  it('publishes each provider domain exactly once and nulls the mirror', () => {
    const env = structuredClone(fixture) as unknown as {
      sources: {
        all: { data: Record<string, unknown> & { providers: unknown } };
        claude: { data: unknown };
        codex: { data: unknown };
      };
    };
    expect(env.sources.all.data.providers).toEqual({ claude: null, codex: null });
    // Acceptance criterion 6 has two halves. The node counts below carry the
    // "the duplicated subtree was removed" half; this carries the "and nothing
    // else was" half, which no count can express. A mirror reintroduced under
    // ANY other name — `provider_data`, `claude`, a debug copy — adds a key
    // here and fails, where a count-only guard would simply read a larger
    // envelope and go on passing.
    expect(Object.keys(env.sources.all.data).sort()).toEqual(
      ['aggregates', 'alerts', 'combined', 'providers'],
    );
    // Non-vacuous: the physical entries must actually carry the domains, or
    // the node-count comparison below would compare two empty subtrees.
    expect(env.sources.claude.data).not.toBeNull();
    expect(env.sources.codex.data).not.toBeNull();

    const legacy = structuredClone(env);
    legacy.sources.all.data.providers = {
      claude: legacy.sources.claude.data,
      codex: legacy.sources.codex.data,
    };

    const removed = nodes(legacy) - nodes(env);
    const duplicated = nodes(legacy.sources.claude.data)
                     + nodes(legacy.sources.codex.data) - 2;

    // DO NOT restore `expect(removed).toBe(duplicated)`. That equality held BY
    // CONSTRUCTION and could not fail: `nodes` traverses references, so
    // replacing a `null` leaf (1 node) with a subtree of N nodes changes the
    // total by exactly N - 1, twice over — which is the definition of
    // `duplicated`. It asserted arithmetic, not the envelope.
    //
    // What discriminates is MATERIALITY: the duplication is measured against
    // the size of the envelope that carries it. Reconstructing the v9 shape
    // costs 34.0% more nodes than the whole v10 envelope on the committed
    // fixture (453 duplicated against 1331 total), so the pinned floor is 25%
    // — comfortably below the measurement and far above anything a fixture
    // that had stopped exercising the removal could reach.
    //
    // BOTH forms are present because they fail for different reasons. The ratio
    // states the claim the criterion actually makes — the duplication is large
    // relative to the envelope carrying it — but it couples two quantities that
    // move independently: `removed` is fixed by the provider subtree, while
    // `nodes(env)` grows with every unrelated addition to the fixture, so about
    // 36% of ordinary fixture growth would trip the ratio with nothing
    // regressed. The absolute floor carries the same materiality claim with no
    // such coupling, so it survives fixture growth and is what still fails if a
    // future edit relaxes the ratio to accommodate a larger fixture.
    expect(removed).toBeGreaterThan(nodes(env) * 0.25);
    expect(removed).toBeGreaterThan(300);
    expect(duplicated).toBeGreaterThan(100);
  });
});
