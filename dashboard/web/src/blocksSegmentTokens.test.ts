/// <reference types="node" />
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// The Blocks-panel cost gauge (`.blocks-row .gauge-fill .seg-<fam>`) and the
// model legend rendered directly beneath it (`.ms-dot.<fam>`) both label the
// SAME model family, so they MUST paint from the SAME token — otherwise the
// bar segment and its legend dot show different colours for one model. This
// regressed for opus/sonnet/haiku/other: the gauge segments were wired to the
// generic `--accent-*` / `--text-dim` palette while the legend (and every
// other model surface — `.chip.<fam>`, `.model-stack > span.<fam>`,
// `.msess-model-caption .sw.<fam>`) uses the family `--chip-*` palette. For
// sonnet/haiku that is a full hue swap (bar blue vs chip green, and vice
// versa). This reads index.css at test time and pins the bar segment and the
// legend dot to the same token per family. (fable was already correct; this
// guard covers the whole family set so a future family can't drift again.)
// vitest runs with cwd at dashboard/web; resolve from cwd so a moved file
// fails loudly (import.meta.url carries a non-file scheme under vitest).
const cssPath = resolve(process.cwd(), 'src/index.css');

// Canonical per-family fill token — the one the legend dot uses. `other` is
// the grey track/surface fill (there is no --chip-other); every named family
// uses its own --chip-<fam>.
const FAMILY_TOKEN: Record<string, string> = {
  opus: '--chip-opus',
  sonnet: '--chip-sonnet',
  haiku: '--chip-haiku',
  fable: '--chip-fable',
  other: '--surface-faint',
};

describe('Blocks gauge segment / legend dot single-source tokens', () => {
  it('finds the stylesheet on disk', () => {
    expect(existsSync(cssPath), `expected stylesheet at ${cssPath}`).toBe(true);
  });

  const css = existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '';

  for (const [fam, token] of Object.entries(FAMILY_TOKEN)) {
    it(`family ${fam}: gauge segment + legend dot both reference var(${token})`, () => {
      // Legend dot — the reference surface every other model chip agrees with.
      expect(css).toMatch(
        new RegExp(`\\.ms-dot\\.${fam}\\s*\\{[^}]*background:\\s*var\\(${token}\\)`),
      );
      // Blocks-panel gauge segment — must agree with the legend dot above.
      expect(css).toMatch(
        new RegExp(
          `\\.blocks-row \\.gauge-fill \\.seg-${fam}\\s*\\{[^}]*background:\\s*var\\(${token}\\)`,
        ),
      );
    });
  }
});
