/// <reference types="node" />
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #247 S1 — scoped CSS-text lint for the dashboard card-chrome facelift.
// Reads index.css at test time. SCOPED to enumerated chrome rules — never a
// global no-literal ban (that is the deferred exhaustive issue). Non-vacuity:
// each assertion pins a concrete selector/token so an empty match fails loudly.
const cssPath = resolve(process.cwd(), 'src/index.css');
// Strip block comments once at load so the rule-body slicer never trips on a
// `}` embedded in a CSS comment (e.g. the `.panel` rule's min-width note). With
// comments gone, production declaration order is no longer hostage to this test.
const css = (existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '').replace(/\/\*[\s\S]*?\*\//g, '');

// Extract the body { ... } of a CSS rule by exact selector. A selector can carry
// several rules (e.g. the trivial `.panel { scroll-margin-top }` helper plus the
// substantive `.panel { ... }` chrome rule); return the LONGEST body — the
// canonical declaration block — so an assertion targets the real rule, not an
// incidental one-liner. Block comments are stripped from `css` at module load,
// so a rule body holds no stray braces (CSS declaration blocks don't nest and
// comments were the only brace source here), making the first-`{`→first-`}`
// slice per match exact.
function ruleBody(selector: string): string {
  let best: string | null = null;
  for (const needle of [selector + ' {', selector + '{']) {
    for (let at = css.indexOf(needle); at >= 0; at = css.indexOf(needle, at + 1)) {
      const open = css.indexOf('{', at);
      const body = css.slice(open + 1, css.indexOf('}', open));
      if (best === null || body.length > best.length) best = body;
    }
  }
  expect(best, `selector not found: ${selector}`).not.toBeNull();
  return best ?? '';
}

// The :root foundation block carries the token definitions; build it once.
const root = ruleBody(':root');

describe('#247 S1 token scales defined in :root', () => {
  it('finds the stylesheet on disk', () => {
    expect(existsSync(cssPath), `expected stylesheet at ${cssPath}`).toBe(true);
  });
  for (const t of ['--radius-xs', '--radius-sm', '--radius-md', '--radius-lg', '--radius-pill', '--radius-circle',
                   '--shadow-sm', '--shadow-md', '--shadow-lg', '--shadow-xl',
                   '--fs-data', '--fs-strong', '--fs-title', '--fs-kpi', '--fs-hero', '--fs-display']) {
    it(`defines ${t}`, () => { expect(root).toMatch(new RegExp(`${t}\\s*:`)); });
  }
  // The conversation-viewer tokens must be untouched (exact values).
  for (const [t, v] of [['--fs-eyebrow', '11px'], ['--fs-meta', '12px'], ['--fs-body', '13.5px']] as const) {
    it(`keeps ${t}: ${v} unchanged`, () => { expect(root).toMatch(new RegExp(`${t}\\s*:\\s*${v.replaceAll('.', '\\.')}`)); });
  }
  // Locks the one behavior Task 1 actually ships: panel chrome adopts the radius token.
  it('panel chrome adopts --radius-md', () => {
    expect(ruleBody('.panel')).toMatch(/border-radius:\s*var\(--radius-md\)/);
  });
});

describe('#247 S1 neutral panel chrome', () => {
  it('base .panel defines --panel-accent and derives --pill-bg via color-mix', () => {
    const body = ruleBody('.panel');
    expect(body).toMatch(/--panel-accent:\s*var\(--accent-/);
    expect(body).toMatch(/--pill-bg:\s*color-mix\(in srgb, var\(--panel-accent\)/);
  });
  it('no .panel.accent-* rule sets a decorative border-color or --accent-glow', () => {
    const accentRules = css.match(/\.panel\.accent-[a-z]+\s*\{[^}]*\}/g) ?? [];
    expect(accentRules.length, 'expected the .panel.accent-* family to exist').toBeGreaterThanOrEqual(10);
    for (const r of accentRules) {
      expect(r, `decorative border-color survived: ${r}`).not.toMatch(/border-color:/);
      expect(r, `--accent-glow survived: ${r}`).not.toMatch(/--accent-glow:/);
      expect(r).toMatch(/--panel-accent:/);
    }
  });
  it('one neutral .panel:focus-visible ring, and no per-accent focus outlines', () => {
    expect(ruleBody('.panel:focus-visible')).toMatch(/outline:\s*2px solid var\(--accent-blue\)/);
    expect(css).not.toMatch(/\.panel\.accent-[a-z]+:focus-visible/);
  });
  it('--pill-bg is still resolvable (Now/Active pill consumers intact)', () => {
    expect(css).toMatch(/background:\s*var\(--pill-bg\)/);
  });
  it('.panel-body--scroll caps height and scrolls internally', () => {
    const body = ruleBody('.panel-body--scroll');
    expect(body).toMatch(/max-height:\s*420px/);
    expect(body).toMatch(/overflow-y:\s*auto/);
  });
  it('Sessions scrolled body keeps its column headers pinned (sticky thead)', () => {
    expect(css).toMatch(/\.panel-body--scroll \.sess-table thead th[^}]*position:\s*sticky/);
  });
});
