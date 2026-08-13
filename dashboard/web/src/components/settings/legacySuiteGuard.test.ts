// #513 S2 §6.3 — the superseded settings suites cannot come back.
//
// Task 2 moved every case out of `__tests__/SettingsOverlay*.test.tsx` into the
// three canonical suites under `src/components/`, and Task 11 deleted the
// originals. Deletion alone is not a guarantee: a later change that restores a
// file at the old path would be collected again, and the two copies would drift
// silently until one of them started failing for a reason nobody could explain.
//
// The vitest `exclude` in `vite.config.ts` is deliberately narrow — the file
// glob, never the whole `__tests__` directory, which holds `setup.ts` (loaded
// via `setupFiles`) and a dozen live suites. That narrowness is what makes this
// guard necessary: an excluded-but-present file would be invisible rather than
// loud.
import { existsSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const LEGACY_DIR = resolve(HERE, '../../../__tests__');

describe('the superseded settings suites stay retired', () => {
  it('finds the legacy directory, so the check below is not vacuous', () => {
    expect(existsSync(LEGACY_DIR)).toBe(true);
    const entries = readdirSync(LEGACY_DIR);
    expect(entries).toContain('setup.ts');
    // Other live suites still live here; this guard is about one file family.
    expect(entries.filter((name) => name.endsWith('.test.tsx')).length).toBeGreaterThan(5);
  });

  it('no SettingsOverlay suite exists under __tests__/', () => {
    const strays = readdirSync(LEGACY_DIR).filter((name) =>
      /^SettingsOverlay.*\.test\.tsx$/.test(name),
    );
    expect(
      strays,
      'move the cases into src/components/SettingsOverlay{,.model,.a11y}.test.tsx instead',
    ).toEqual([]);
  });
});
