// #620 S1 D6 / A6 — the TypeScript half of the Cache Report verdict parity.
//
// `cacheRowVerdict` here and `cache_row_verdict` in bin/_lib_cache_report.py
// are two implementations of one rule. One literal classifier is not possible
// across the language boundary without code generation, so what is asserted is
// that they AGREE on every vector — and both suites read the SAME file. A
// duplicated vector list would be two truths, not a parity test: each side
// would keep passing against its own copy while the algorithms drifted.
//
// This deliberately does not follow the AXIS_CHIP_LABEL precedent, which
// regex-parses static maps and compares literals. That shape cannot
// demonstrate behavioural agreement between two algorithms; driving both over
// the same inputs can.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  CACHE_ANOMALY_PREDICATES, cacheRowVerdict,
} from '../src/lib/cacheReportVerdict';
import type { CacheAnomalyReason } from '../src/types/envelope';

const VECTORS_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..', '..', '..', 'tests', 'fixtures', 'cache-report-verdict-vectors.json',
);

interface Vector {
  name: string;
  why: string;
  predicates: CacheAnomalyReason[];
  input: {
    anomaly_triggered: boolean;
    anomaly_reasons?: CacheAnomalyReason[];
    anomaly_unevaluated?: CacheAnomalyReason[];
    observed?: boolean;
  };
  expected: 'anomalous' | 'clean' | 'partial' | 'unevaluated';
  expected_observed?: boolean;
}

const vectors: Vector[] = (
  JSON.parse(readFileSync(VECTORS_PATH, 'utf8')) as { vectors: Vector[] }
).vectors;

describe('cache report verdict — shared truth table', () => {
  it('reads the same file the Python kernel test reads', () => {
    expect(vectors.length).toBeGreaterThan(0);
    // If this ever needs a local copy to pass, the parity claim is gone.
    expect(VECTORS_PATH).toContain(
      path.join('tests', 'fixtures', 'cache-report-verdict-vectors.json'),
    );
  });

  it('covers all four states', () => {
    expect(new Set(vectors.map((v) => v.expected))).toEqual(
      new Set(['anomalous', 'clean', 'partial', 'unevaluated']),
    );
  });

  for (const v of vectors) {
    it(`${v.name}: ${v.why}`, () => {
      const got = cacheRowVerdict(
        {
          anomaly_triggered: v.input.anomaly_triggered,
          anomaly_reasons: v.input.anomaly_reasons ?? [],
          anomaly_unevaluated: v.input.anomaly_unevaluated,
          observed: v.input.observed,
        },
        v.predicates,
      );
      expect(got.state).toBe(v.expected);
      if (v.expected_observed !== undefined) {
        expect(got.observed).toBe(v.expected_observed);
      }
    });
  }

  it('the default predicate set is Claude\'s pair, matching the kernel', () => {
    expect([...CACHE_ANOMALY_PREDICATES]).toEqual(['net_negative', 'cache_drop']);
    expect(
      cacheRowVerdict({
        anomaly_triggered: false,
        anomaly_reasons: [],
        anomaly_unevaluated: ['cache_drop'],
      }).state,
    ).toBe('partial');
  });
});
