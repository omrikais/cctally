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

describe('#247 S1 mobile form-control & data floor', () => {
  // css is already comment-stripped at module load. This regex greedily
  // captures from the first 640px @media to EOF, so `mobile` also includes any
  // later non-media CSS — it's a TEXT tripwire, not a precise block extractor
  // (JSDOM can't evaluate @media; real verification is the ui-qa gate). Don't
  // upgrade this to a fragile balanced-brace regex.
  const m = css.match(/@media \(max-width:\s*640px\)\s*\{([\s\S]*)$/);
  const mobile = m ? m[1] : '';
  it('mobile block re-asserts inputs at id-strength (#root .class … 16px)', () => {
    expect(mobile).toMatch(/#root \.ctrl-input\b[^}]*font-size:\s*16px/);
    expect(mobile).toMatch(/#root \.settings-fs input\b[^}]*font-size:\s*16px/);
  });
  it('mobile block re-asserts the Settings select and Cache Report popover input', () => {
    expect(mobile).toMatch(/#root \.settings-(select|btn)\b[^}]*font-size:\s*16px/);
    expect(mobile).toMatch(/#root \.crm-settings-popover input\b[^}]*font-size:\s*16px/);
  });
  it('mobile primary-data floor uses --fs-data (14px)', () => {
    expect(mobile).toMatch(/var\(--fs-data\)/);
  });
});

// #312 P1 — JSDOM does not resolve the dashboard's responsive stylesheet, so
// these are intentionally focused cascade contracts. Real Chromium verifies
// pixels; these pin the two mobile layout boundaries that prevent a future
// source-aware header/table change from reviving document overflow or stacked
// native token cells.
describe('#312 mobile source-layout containment', () => {
  const m = css.match(/@media \(max-width:\s*640px\)\s*\{([\s\S]*)$/);
  const mobile = m ? m[1] : '';

  it('makes the topbar action band a shrinkable full mobile row', () => {
    expect(mobile).toMatch(
      /\.topbar \.topbar-actions\s*\{(?=[^}]*width:\s*100%)(?=[^}]*flex:\s*1\s+1\s+100%)(?=[^}]*min-width:\s*0)/,
    );
  });

  it('uses the canonical compact mobile session records for every source', () => {
    expect(mobile).toMatch(/\.sess-table tr\.session-row\s*\{[^}]*display:\s*grid/);
    expect(mobile).not.toMatch(/source-sess-table[^}]*min-width:\s*760px/);
    expect(mobile).not.toMatch(/source-session-row[^}]*display:\s*table-row/);
  });

  it('bounds shared Trend and collapsed Blocks cards independent of provider row count', () => {
    expect(mobile).toMatch(/#panel-trend \.trend-table-wrap\s*\{[^}]*max-height:\s*132px/);
    expect(mobile).toMatch(/#panel-blocks\.blocks-collapsed\s*\{(?=[^}]*height:\s*184px)(?=[^}]*max-height:\s*184px)/);
    expect(mobile).toMatch(/#panel-blocks\.blocks-collapsed:has\(\.blocks-row\)\s*\{(?=[^}]*height:\s*216px)(?=[^}]*max-height:\s*216px)/);
  });

  it('keeps both quota values separated and the three support slots in one shared hero grid', () => {
    expect(mobile).toMatch(
      /\.hero-usage\s*\{(?=[^}]*grid-template-columns:\s*fit-content\(110px\)\s+minmax\(0,\s*1fr\))(?=[^}]*column-gap:\s*var\(--space-3\))/,
    );
    expect(mobile).toMatch(/\.hero-support\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/);
  });

  it('bounds native quota and period bodies with their own mobile vertical scroller', () => {
    expect(mobile).toMatch(
      /\.panel-body--source-native,\s*\.source-provider-body\s*\{(?=[^}]*max-height:\s*360px)(?=[^}]*overflow-y:\s*auto)/,
    );
    expect(ruleBody('#panel-sessions .panel-body')).toMatch(/overflow-x:\s*auto/);
  });

  it('keeps shared period tables inside their pane and converts mobile rows to wrapping records', () => {
    expect(css).toMatch(/\.period-table-pane\s*\{(?=[^}]*min-width:\s*0)(?=[^}]*overflow-x:\s*auto)/);
    expect(mobile).toMatch(/\.history-table tr\s*\{(?=[^}]*display:\s*flex)(?=[^}]*flex-wrap:\s*wrap)/);
  });
});

// ============================================================================
// #255 — GLOBAL no-orphan-literal lint (border-radius / box-shadow / accent).
// Promotes the S1 scoped lint to a whole-dashboard ban. Operates on a
// newline-preserving MASKED copy of index.css (so an index maps 1:1 to a raw
// line — allow-markers are read from the raw text) and excludes the
// conversation-viewer (#228 owns its token story) by BOTH the section banner
// position AND a selector namespace — the two are complementary (see isConvScope).
// `rawCss` / `root` are reused from the S1 block above.
// ============================================================================
const rawCss255 = existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '';
// Mask block comments but keep their newlines, so masked indices share line
// numbers with rawCss255. (S1's `css` collapses lines — do NOT reuse it here.)
const masked = rawCss255.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ''));
const rawLines255 = rawCss255.split('\n');

// A rule is conversation-viewer scope (excluded) iff its selector is namespaced.
const CONV_SELECTOR = /\.(conv-|comparison|view-switcher|view-seg|sess-open-conv)\b/;
// The conversation-viewer region begins at this banner; everything at/after it
// is #228's token story, excluded by POSITION. This complements the selector
// predicate: the suffix has conv rules whose selector carries NO conv- prefix
// (`.codeblock`, `.md code/pre`, a `@keyframes conv-jump-flash` step `0%`), which
// the selector check alone would miss; the selector check in turn catches conv
// rules that interleave BEFORE the banner. A missing banner fails loudly (below).
const convBannerLine = rawLines255.findIndex((l) => l.includes('Conversation viewer (spec §4)')) + 1;
// A literal at `idx` is conversation-viewer scope (excluded) if it sits at/after
// the banner OR inside a conv-namespaced rule.
function isConvScope(idx: number): boolean {
  return (convBannerLine > 0 && lineAt(idx) >= convBannerLine) || CONV_SELECTOR.test(enclosingSelector(idx));
}

// Accent colour set, derived from the :root --accent-* definitions themselves —
// adding a future accent auto-extends the ban (no hand-maintained list).
const accentHexes = new Set<string>();
const accentTriples = new Set<string>();
for (const mm of root.matchAll(/--accent-[a-z]+\s*:\s*#([0-9a-fA-F]{6})\b/g)) {
  const hex = mm[1].toLowerCase();
  accentHexes.add(hex);
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  accentTriples.add(`${r},${g},${b}`);
}

function lineAt(index: number): number {
  let n = 1;
  for (let k = 0; k < index; k++) if (masked.charCodeAt(k) === 10) n++;
  return n;
}
// Selector of the rule whose declaration block contains `index` (nearest
// unmatched `{` going back; @media/@supports wrappers are one level further out).
function enclosingSelector(index: number): string {
  let depth = 0, open = -1;
  for (let j = index - 1; j >= 0; j--) {
    const c = masked[j];
    if (c === '}') depth++;
    else if (c === '{') { if (depth === 0) { open = j; break; } depth--; }
  }
  if (open < 0) return '';
  let start = 0;
  for (let j = open - 1; j >= 0; j--) if (masked[j] === '{' || masked[j] === '}') { start = j + 1; break; }
  return masked.slice(start, open).trim();
}
// Property name of the declaration containing `index` (text before its first `:`).
function enclosingProperty(index: number): string {
  let start = 0;
  for (let j = index - 1; j >= 0; j--) { const c = masked[j]; if (c === '{' || c === '}' || c === ';') { start = j + 1; break; } }
  const seg = masked.slice(start, index);
  const colon = seg.indexOf(':');
  return (colon >= 0 ? seg.slice(0, colon) : seg).trim();
}
function allowMarked(line: number, prop: string): boolean {
  const m = (rawLines255[line - 1] ?? '').match(/\/\*\s*lint-allow:\s*([a-z-]+)/i);
  return !!m && m[1].toLowerCase() === prop.toLowerCase();
}

type V = { line: number; selector: string; detail: string };
const fmt = (vs: V[]) => '\n' + vs.map((x) => `  ${x.line}: ${x.selector} — ${x.detail}`).join('\n');

function radiusViolations(): V[] {
  const out: V[] = [];
  for (const m of masked.matchAll(/border-radius\s*:\s*([^;}]*)/g)) {
    const idx = m.index ?? 0;
    if (isConvScope(idx)) continue;
    // Strip token refs + explicit 0 + inherit; any remaining number/% = raw literal.
    const stripped = m[1].replace(/var\([^)]*\)/g, '').replace(/\binherit\b/g, '').replace(/\b0\b/g, '').trim();
    if (!/\d|%/.test(stripped)) continue;
    const line = lineAt(idx);
    if (!allowMarked(line, 'border-radius')) out.push({ line, selector: enclosingSelector(idx), detail: `border-radius: ${m[1].trim()}` });
  }
  return out;
}
function shadowViolations(): V[] {
  const out: V[] = [];
  for (const m of masked.matchAll(/box-shadow\s*:\s*([^;}]*)/g)) {
    const idx = m.index ?? 0;
    if (isConvScope(idx)) continue;
    // Strip var() FIRST (so nested color-mix parens collapse), then color-mix();
    // any remaining rgba()/hex = raw colour term → fail.
    const stripped = m[1].replace(/var\([^)]*\)/g, '').replace(/color-mix\([^)]*\)/g, '');
    if (!/rgba?\(|#[0-9a-fA-F]{3,8}/.test(stripped)) continue;
    const line = lineAt(idx);
    if (!allowMarked(line, 'box-shadow')) out.push({ line, selector: enclosingSelector(idx), detail: `box-shadow: ${m[1].trim()}` });
  }
  return out;
}
function accentViolations(): V[] {
  const out: V[] = [];
  for (const m of masked.matchAll(/rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/g)) {
    if (!accentTriples.has(`${+m[1]},${+m[2]},${+m[3]}`)) continue;
    const idx = m.index ?? 0;
    if (isConvScope(idx)) continue;
    if (enclosingProperty(idx).startsWith('--')) continue; // token definition, not a consumer
    const line = lineAt(idx);
    if (!allowMarked(line, 'accent')) out.push({ line, selector: enclosingSelector(idx), detail: `accent rgba(${+m[1]},${+m[2]},${+m[3]})` });
  }
  for (const m of masked.matchAll(/#([0-9a-fA-F]{6})\b/g)) {
    if (!accentHexes.has(m[1].toLowerCase())) continue;
    const idx = m.index ?? 0;
    if (isConvScope(idx)) continue;
    if (enclosingProperty(idx).startsWith('--')) continue; // the :root --accent-* definition
    const line = lineAt(idx);
    if (!allowMarked(line, 'accent')) out.push({ line, selector: enclosingSelector(idx), detail: `accent #${m[1].toLowerCase()}` });
  }
  return out;
}

describe('#255 global no-orphan-literal lint', () => {
  it('boundary predicate pins both ends (.panel dashboard, .conv-view conv)', () => {
    expect(CONV_SELECTOR.test('.panel')).toBe(false);
    expect(CONV_SELECTOR.test('.conv-view')).toBe(true);
    expect(CONV_SELECTOR.test('.sess-open-conv')).toBe(true);
  });
  it('derives the accent set from :root (≥12 families incl. amber)', () => {
    expect(accentHexes.size).toBeGreaterThanOrEqual(12);
    expect(accentTriples.has('251,191,36')).toBe(true);
  });
  it('no raw border-radius literal in dashboard-scope rules', () => {
    const v = radiusViolations();
    expect(v, fmt(v)).toEqual([]);
  });
  it('no raw box-shadow literal in dashboard-scope rules', () => {
    const v = shadowViolations();
    expect(v, fmt(v)).toEqual([]);
  });
  it('no surviving accent literal (rgba triple or hex) in dashboard-scope rules', () => {
    const v = accentViolations();
    expect(v, fmt(v)).toEqual([]);
  });
  it('boundary anchor: the conversation-viewer banner is present', () => {
    expect(convBannerLine, 'conv-viewer banner not found — the region anchor vanished').toBeGreaterThan(0);
  });
  it('non-vacuity: the conversion breadth is actually present', () => {
    expect((masked.match(/var\(--radius-/g) ?? []).length).toBeGreaterThanOrEqual(60);
    // Only 4 box-shadows are tokenizable per the spec table (--shadow-sm, the
    // --shadow-md blur snap, --shadow-xl, + 1 pre-existing --shadow-md); every
    // other shadow is correctly an allow-marked bespoke drop or a color-mix accent.
    expect((masked.match(/var\(--shadow-/g) ?? []).length).toBeGreaterThanOrEqual(3);
    expect(/color-mix\(in srgb, var\(--accent-/.test(masked)).toBe(true);
    expect(/lint-allow:/.test(rawCss255)).toBe(true);
  });
});
