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
    expect(env.source_schema_version).toBe(2);
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
    expect(slice.source_schema_version).toBe(2);
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
      combined.legs.claude.total_tokens + combined.legs.codex.total_tokens);
  });

  it('omits qualifications and combined_unavailable when inapplicable', () => {
    const data = allSourceData('all-combined');

    expect('combined_unavailable' in data).toBe(false);
    expect(data.combined_unavailable).toBeUndefined();
    expect('qualifications' in (data.combined as object)).toBe(false);
  });

  it('a withheld combined is null BESIDE a typed, ordered cause list', () => {
    const data = allSourceData('all-combined-decorated');

    // `combined` stays PRESENT as null; only `combined_unavailable` is
    // omitted-when-inapplicable.
    expect('combined' in data).toBe(true);
    expect(data.combined).toBeNull();
    const unavailable = data.combined_unavailable;
    expect(unavailable?.code).toBe('multi_account_unsupported');
    expect(typeof unavailable?.message).toBe('string');
    // The list is precedence-ordered, so `causes[0]` always equals the winner.
    expect(unavailable?.causes[0].code).toBe(unavailable?.code);
    expect(unavailable?.causes[0].provider).toBe('claude');
    expect(unavailable?.causes[0].detail).toEqual({ account_count: 2 });
  });

  it('lists EVERY co-occurring cause, not just the winner', () => {
    const data = allSourceData('codex-cache-active');

    expect(data.combined).toBeNull();
    expect(data.combined_unavailable?.causes.map((cause) => cause.code)).toEqual([
      'codex_projection_incoherent', 'codex_cycle_unavailable',
    ]);
  });
});
