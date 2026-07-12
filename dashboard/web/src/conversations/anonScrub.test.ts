import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { scrubText, type AnonWirePlan } from './anonScrub';

// spec §8.4 — the cross-language parity fixture is GENERATED from the production
// SECRET_PATTERNS + a fixed identity plan (bin/build-anon-parity-fixture.py) and
// golden-guarded on the Python side. Running the TS applier over the SAME
// inputs/expected executes every PRODUCTION secret pattern in the JS runtime, so
// a Python/JS drift is a test failure here — not a silent leak.
// Resolve from cwd (a real fs path, unlike import.meta.url under vitest —
// mirrors markerParity.test.ts). vitest runs with cwd = dashboard/web.
const fixture = JSON.parse(
  readFileSync(resolve(process.cwd(), '../../tests/fixtures/anon/parity.json'), 'utf8'),
) as { plan: AnonWirePlan; cases: { input: string; expected: string }[] };

describe('anonScrub parity fixture (TS applier == Python kernel)', () => {
  it('has cases', () => {
    expect(fixture.cases.length).toBeGreaterThan(10);
  });
  for (const c of fixture.cases) {
    it(`scrubs ${JSON.stringify(c.input).slice(0, 48)}`, () => {
      expect(scrubText(c.input, fixture.plan)).toBe(c.expected);
    });
  }
});
