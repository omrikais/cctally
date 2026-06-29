/// <reference types="node" />
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #247 S1 — scoped CSS-text lint for the dashboard card-chrome facelift.
// Reads index.css at test time. SCOPED to enumerated chrome rules — never a
// global no-literal ban (that is the deferred exhaustive issue). Non-vacuity:
// each assertion pins a concrete selector/token so an empty match fails loudly.
const cssPath = resolve(process.cwd(), 'src/index.css');
const css = existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '';

// Extract the body { ... } of a single CSS rule by exact selector (first match).
// Exported so the build's `tsc --noEmit` (noUnusedLocals) accepts it while Task 1's
// own assertions don't call it yet — Tasks 2 & 3 extend this file and reuse it.
export function ruleBody(selector: string): string {
  const i = css.indexOf(selector + ' {');
  const j = css.indexOf(selector + '{');
  const start = i >= 0 ? i : j;
  expect(start, `selector not found: ${selector}`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  return css.slice(open + 1, close);
}

describe('#247 S1 token scales defined in :root', () => {
  it('finds the stylesheet on disk', () => {
    expect(existsSync(cssPath), `expected stylesheet at ${cssPath}`).toBe(true);
  });
  for (const t of ['--radius-xs', '--radius-sm', '--radius-md', '--radius-lg', '--radius-pill', '--radius-circle',
                   '--shadow-sm', '--shadow-md', '--shadow-lg', '--shadow-xl',
                   '--fs-data', '--fs-strong', '--fs-title', '--fs-kpi', '--fs-hero', '--fs-display']) {
    it(`defines ${t}`, () => { expect(css).toMatch(new RegExp(`${t}\\s*:`)); });
  }
  // The conversation-viewer tokens must be untouched (exact values).
  for (const [t, v] of [['--fs-eyebrow', '11px'], ['--fs-meta', '12px'], ['--fs-body', '13.5px']] as const) {
    it(`keeps ${t}: ${v} unchanged`, () => { expect(css).toMatch(new RegExp(`${t}\\s*:\\s*${v.replace('.', '\\.')}`)); });
  }
});
