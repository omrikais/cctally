// Backstop only. The primary enforcement that no component re-derives the
// predicate is the component tests, which assert each surface renders what
// the helper returns. A text scan cannot catch `<= 4`, a reversed
// comparison, or a re-derived `anomaly_triggered && !insufficient` — so this
// exists to catch a literal copy-paste, nothing more.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// process.cwd(), not import.meta.url — vitest rewrites the latter to a
// root-relative URL, so `new URL('..', import.meta.url).pathname` resolves to
// a bare "/src" and the scan dies on ENOENT before reading a single file.
// A scan that cannot open the tree is not a passing scan, it is a vacuous one.
const SRC = resolve(process.cwd(), 'src');
const NEEDLE = 'baseline_daily_row_count' + ' <';

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) { sourceFiles(full, out); continue; }
    if (!/\.tsx?$/.test(name)) continue;
    if (/\.test\.tsx?$/.test(name)) continue;      // exclude test sources
    if (name === 'cacheReportVerdict.drift.test.ts') continue;  // never self-match
    out.push(full);
  }
  return out;
}

describe('insufficient predicate has exactly one definition', () => {
  it('scans a source tree that actually exists', () => {
    // Non-vacuity guard: without this a broken SRC would make the scan
    // throw (or, worse, find zero files) and read as a spelling problem.
    expect(existsSync(SRC)).toBe(true);
    expect(sourceFiles(SRC).length).toBeGreaterThan(50);
  });

  it('appears only in cacheReportVerdict.ts', () => {
    const hits = sourceFiles(SRC)
      .filter((f) => readFileSync(f, 'utf8').includes(NEEDLE));
    // Assert the location, not only the count: a rename that makes the scan
    // find zero matches must fail rather than silently pass.
    expect(hits.map((f) => f.split('/').pop())).toEqual(['cacheReportVerdict.ts']);
  });
});
