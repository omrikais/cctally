/// <reference types="node" />
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #207 C5 — the Daily heatmap legend swatches MUST match the cell-bucket
// fills bucket-for-bucket. Both now route through one `var(--daily-hN)`
// token set, so they structurally cannot diverge. This reads index.css at
// test time and asserts each bucket's token is defined and that both the
// cell fill (background-color) and legend swatch (background) reference it.
// vitest runs with cwd at dashboard/web; resolve the stylesheet from cwd (a
// real fs path, unlike import.meta.url which carries a non-file scheme under
// vitest's transform) and assert it exists so a moved file fails loudly.
const cssPath = resolve(process.cwd(), 'src/index.css');

describe('Daily heatmap legend/cell single-source tokens (#207 C5)', () => {
  it('finds the stylesheet on disk', () => {
    expect(existsSync(cssPath), `expected stylesheet at ${cssPath}`).toBe(true);
  });

  const css = existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '';

  for (let n = 0; n <= 5; n++) {
    it(`bucket h${n}: token defined, cell + legend both reference var(--daily-h${n})`, () => {
      expect(css).toMatch(new RegExp(`--daily-h${n}\\s*:`));
      expect(css).toMatch(
        new RegExp(`\\.daily-cell\\.h${n}\\s*\\{[^}]*background-color:\\s*var\\(--daily-h${n}\\)`),
      );
      expect(css).toMatch(
        new RegExp(`\\.daily-legend \\.scale \\.h${n}\\s*\\{[^}]*background:\\s*var\\(--daily-h${n}\\)`),
      );
    });
  }
});
