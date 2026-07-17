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
import fixture from './fixtures/envelope.json';
import { makeSourceEnvelope } from '../src/test-utils/sourceEnvelope';

describe('S4 source envelope wire shape (guard)', () => {
  it('the JSON fixture spreads the source fields at the envelope TOP level', () => {
    const env = fixture as Record<string, unknown>;

    // The four bundle fields are TOP-LEVEL siblings, not nested under `sources`.
    expect(env.source_schema_version).toBe(1);
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
    const claude = sources.claude as { availability?: unknown } | undefined;
    const codex = sources.codex as { availability?: unknown } | undefined;
    const all = sources.all as { availability?: unknown } | undefined;
    expect(typeof claude?.availability).toBe('string');
    expect(typeof codex?.availability).toBe('string');
    expect(typeof all?.availability).toBe('string');
  });

  it('the test-utils builder convention encodes the SAME flat/top-level shape', () => {
    const slice = makeSourceEnvelope() as unknown as Record<string, unknown>;

    // Top-level siblings, mirroring the JSON fixture + the real serializer.
    expect(slice.source_schema_version).toBe(1);
    expect(slice.default_source).toBe('claude');
    expect(slice.source_order).toEqual(['claude', 'codex', 'all']);

    const sources = slice.sources as Record<string, unknown>;
    expect(Object.keys(sources).sort()).toEqual(['all', 'claude', 'codex']);
    // No phantom nesting in the builder either.
    expect('sources' in sources).toBe(false);
    expect((sources.claude as { availability?: unknown }).availability).toBe('ok');
  });
});
