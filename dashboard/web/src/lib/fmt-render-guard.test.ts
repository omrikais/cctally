import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));       // dashboard/web/src/lib
const srcRoot = resolve(here, '..');                        // dashboard/web/src

// Explicit allowlist of the files S5 localizes. NOT a broad modals/+panels/
// sweep: Daily/Period surfaces still render raw dates until S8, and a broad
// sweep would false-positive on them. Whole-file scan (not JSX-children-only)
// so CacheNetBars' non-JSX `const label = … d.date.slice(5)` assignment is
// caught.
const GUARDED_FILES = [
  'modals/SessionModal.tsx',
  'modals/CacheReportModal.tsx',
  'modals/CacheReportSpotlight.tsx',
  'modals/CacheNetBars.tsx',
  'components/HeroStrip.tsx',
];

// A line is a non-render (key/attr/predicate/ISO-generation/already-fmt) and
// is skipped when it contains any of these. Two constructs are non-renders
// even though they carry an `_at`/`.date` token, so they are explicitly
// skipped (minimal per-construct widening — the file stays in the list):
//   - getState( — a store-accessor selector line (e.g. the `generated_at`
//     SSE-tick key in useGeneratedAt); snapshot metadata read, not a render.
//   - formatHHMMSS( — HeroStrip's visible "as of HH:MM:SS" body stamp, a
//     deliberately host-local CLOCK freshness reading (C5, out of SH-1 scope
//     per the spec), NOT a calendar-date / instant render.
//   - remainingSeconds( — HeroStrip's instant-to-DURATION helper. It consumes
//     an `*_at` bound and returns a number of seconds; what reaches the screen
//     is `fmt.ddhh` of that number, never the instant. These lines carried
//     `Date.parse` inline until the parse moved into the shared helper, which
//     is the only reason they were skipped before.
const SKIP = /(?:data-|key=|aria-|===|!==|Date\.parse|new Date\(|getState\(|formatHHMMSS\(|remainingSeconds\(|fmt\.)/;

// Raw datetime render/assignment: an *_utc / *_at member, or a `.date`
// member (incl. `.date.slice(...)`), used as a value.
const RAW_ISO = /\b[\w.]*(?:_utc|_at)\b/;
const RAW_DATE = /\.date\b/;

function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')      // block comments
    .split('\n')
    .map((l) => l.replace(/\/\/.*$/, ''))  // line comments
    .join('\n');
}

describe('fmt render guard (#251 SH-1)', () => {
  for (const rel of GUARDED_FILES) {
    it(`${rel} routes every datetime render through fmt.`, () => {
      const src = stripComments(readFileSync(resolve(srcRoot, rel), 'utf8'));
      const offenders: string[] = [];
      src.split('\n').forEach((line, i) => {
        if (SKIP.test(line)) return;
        if (RAW_ISO.test(line) || RAW_DATE.test(line)) {
          offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
        }
      });
      expect(offenders, offenders.join('\n')).toEqual([]);
    });
  }
});
