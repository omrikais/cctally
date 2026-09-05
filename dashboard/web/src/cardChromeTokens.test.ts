/// <reference types="node" />
import { existsSync, readdirSync, readFileSync } from 'node:fs';
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

  it('keeps both quota values separated and the support slots in one shared hero grid', () => {
    // #590 — plain `1fr` tracks keep their automatic min-content floor. On a
    // decorated Codex hero that let the usage zone force a 200px/102px split
    // at 320px, leaving only 78px of content width for a 102px spend figure.
    // Explicit zero minima let the two authored equal tracks stay equal.
    expect(mobile).toMatch(
      /\.hero-strip\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\)/,
    );
    expect(mobile).toMatch(
      /\.hero-usage\s*\{(?=[^}]*grid-template-columns:\s*fit-content\(110px\)\s+minmax\(0,\s*1fr\))(?=[^}]*column-gap:\s*var\(--space-3\))/,
    );
    // #556 S1 QA P1 — the support track count FOLLOWS the row count instead of
    // being the canonical hero's three written into the template. The All
    // selection supplies two rows, and a fixed three-track grid squeezed each
    // of its longer labels into a third of the zone.
    expect(mobile).toMatch(
      /\.hero-support\s*\{(?=[^}]*grid-auto-flow:\s*column)(?=[^}]*grid-auto-columns:\s*minmax\(0,\s*1fr\))/,
    );
    expect(mobile).not.toMatch(/\.hero-support\s*\{[^}]*grid-template-columns:/);
    // A label that outgrows its cell wraps. It must never keep one line and
    // paint itself over the next cell's label, which is what `nowrap` with
    // visible overflow did.
    expect(mobile).toMatch(/\.hero-support \.sup-l\s*\{[^}]*white-space:\s*normal/);
    expect(mobile).toMatch(/\.hero-support \.sup-v\s*\{[^}]*white-space:\s*nowrap/);
  });

  it('stacks the All hero\'s two full-size provider blocks one per row', () => {
    // #556 S1 QA P0 — the canonical template above sizes ONE primary block
    // beside one small secondary metric. Two full-size peers do not fit side by
    // side at phone widths at any credible KPI size, so the second one was
    // crushed to a fraction of its numeral's width and painted across the zone
    // border.
    expect(mobile).toMatch(
      /\.hero-strip\[data-source="all"\] \.hero-usage\s*\{(?=[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s*;)/,
    );
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

// #661 S2 remediation, finding C1. `ForecastModal` shipped a heading with
// `className="m-sec sec-quota"` and `index.css` had no `.m-sec.sec-quota`
// rule at all, so the heading rendered in the inherited colour while every
// sibling heading was tinted — and nothing errored, because an unmatched
// modifier class is silent. This scans the client for every `sec-*` modifier
// actually used beside `m-sec` and requires a rule for each, so the next one
// fails here rather than shipping untinted.
describe('#661 S2 — every m-sec accent modifier has a rule', () => {
  const srcDir = resolve(process.cwd(), 'src');

  function walk(dir: string): string[] {
    const out: string[] = [];
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = resolve(dir, entry.name);
      if (entry.isDirectory()) out.push(...walk(full));
      else if (/\.tsx?$/.test(entry.name)) out.push(full);
    }
    return out;
  }

  // NOT `ruleBody`: that helper matches `selector + " {"` literally, and the
  // accent block aligns its opening braces with runs of spaces, so seven of
  // the thirteen rules would read as absent. This matches the selector with
  // whitespace tolerance instead.
  function accentBody(name: string): string | null {
    const re = new RegExp(`\\.m-sec\\.sec-${name}\\s*\\{([^}]*)\\}`);
    const match = css.match(re);
    return match ? match[1] : null;
  }

  const used = new Set<string>();
  for (const file of walk(srcDir)) {
    const text = readFileSync(file, 'utf8');
    for (const m of text.matchAll(/m-sec\s+sec-([a-z0-9-]+)/g)) used.add(m[1]);
  }

  it('finds the modifiers the client actually uses', () => {
    // Non-vacuity: an empty scan would satisfy every assertion below.
    expect(used.size).toBeGreaterThanOrEqual(10);
    expect(used.has('quota')).toBe(true);
  });

  it('resolves a rule that exists and refuses one that does not', () => {
    // Non-vacuity for the matcher itself: a regex that matched nothing would
    // report every modifier as missing, and one that matched anything would
    // report none.
    expect(accentBody('quota')).toMatch(/color:/);
    expect(accentBody('no-such-accent')).toBeNull();
  });

  it.each([...used].sort())('.m-sec.sec-%s is defined and sets a colour',
    (name) => {
      const body = accentBody(name);
      expect(body, `no .m-sec.sec-${name} rule in index.css`).not.toBeNull();
      expect(body).toMatch(/color:/);
    });
});

// #730 / #750 S2 — the header actions cluster shrinks only where a child of it
// can actually give, which today means only where it holds `.sessions-ctrls`.
//
// Shrinking a cluster whose children are all incompressible cannot prevent
// overflow; it only moves the overflow outside the cluster's own box. Every
// panel except Sessions holds three or four icon buttons and nothing else, and
// an unconditional floor on the bare selector was measured doing exactly that:
// at 320px the Projects header does not wrap, the flex algorithm shrank the
// cluster to 77.7px while its children stayed at 44 + 44 + 16.7px, and
// `.panel-grip` rendered at x=324 against a 320px viewport — 4px of document
// horizontal scroll that the initial `auto` floor does not produce.
//
// #750 S2 review — the `:has()` scoping was necessary and not sufficient, and
// the floor now lives INSIDE `@media (max-width: 640px)` beside the
// `.sessions-ctrls` floor it needs. Above that breakpoint the two never both
// applied: the `SessionsControls` hoist means `.sessions-ctrls` is a child of
// the cluster only above 640px, exactly where its own floor is switched off,
// so the cluster could shrink while its one compressible child could not give.
// Measured on the Sessions header with the search input open: the cluster
// shrank to 320.2px at 1200px against children wanting 424.3px, and the strip
// and the three icon affordances rendered 96.8px to the right of the cluster's
// box and 73.8px past the panel's own right border. Scoping the floor into the
// query returns that to zero at every width from 900 to 1440, because the
// header's `h2` gives instead.
//
// The counterfactual the pair exists for still closes. Re-parenting
// `.sessions-ctrls` as the first child of the cluster at 390px — the exact
// `!isMobile` DOM — measured document `scrollWidth` 571 against `clientWidth`
// 390 with no floor at all, 563 with the cluster floor alone, and 390 with
// both.
//
// These are the guards on that defense. Deleting the scoped declaration must
// fail here, so must putting a floor back on the bare selector, and so must
// hoisting the scoped one back out of the query.

// `ruleBody` matches its needle as a substring, so `.panel-header-actions {`
// also finds `#panel-alerts > .panel-header > .panel-header-actions {`. These
// assertions must speak about the BARE selector specifically, so anchor the
// match at a rule boundary and collect every such body.
function bareClusterRuleBodies(): string[] {
  const bodies: string[] = [];
  const re = /(?:^|[\n,{}])\s*\.panel-header-actions\s*\{/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    const open = css.indexOf('{', m.index);
    bodies.push(css.slice(open + 1, css.indexOf('}', open)));
  }
  return bodies;
}

describe('#730 / #750 S2 — .panel-header-actions shrinks only where a child gives', () => {
  it('declares min-width: 0 on the :has()-scoped cluster', () => {
    expect(ruleBody('.panel-header-actions:has(.sessions-ctrls)')).toMatch(/min-width:\s*0/);
  });

  it('keeps that floor inside the mobile query, beside the child floor it needs', () => {
    expect(mobileSessionsBlock()).toMatch(
      /\n\s*\.panel-header-actions:has\(\.sessions-ctrls\)\s*\{[^}]*min-width:\s*0/,
    );
  });

  it('leaves the cluster at its automatic minimum above 640px', () => {
    const block = mobileSessionsBlock();
    const outside = css.slice(0, css.indexOf(block)) + css.slice(css.indexOf(block) + block.length);
    expect(outside).not.toMatch(
      /\.panel-header-actions:has\(\.sessions-ctrls\)\s*\{[^}]*min-width:\s*0/,
    );
  });

  it('is not vacuous: a bare cluster rule really is the right-aligned flex group', () => {
    const bodies = bareClusterRuleBodies();
    expect(bodies.length, 'no bare .panel-header-actions rule in index.css').toBeGreaterThan(0);
    const chrome = bodies.find((b) => /margin-left:\s*auto/.test(b) && /display:\s*flex/.test(b));
    expect(chrome, 'no bare .panel-header-actions rule sets margin-left: auto + display: flex').toBeDefined();
  });

  it('leaves every bare cluster rule at its automatic minimum', () => {
    for (const body of bareClusterRuleBodies()) expect(body).not.toMatch(/min-width:/);
  });
});

// #750 S2 review — the second half of the #730 defense, scoped to mobile.
//
// `min-width: 0` on `.panel-header-actions:has(.sessions-ctrls)` lets the
// CLUSTER box shrink, but `.sessions-ctrls` keeps `min-width: auto`, so in the
// counterfactual DOM its 180px search input held the cluster's children out
// past the viewport whatever the cluster's own box did. Flooring the controls
// strip too lets it give, which pulls the incompressible 44px affordances back
// inside the line.
//
// The rule is deliberately confined to the mobile query. Above 640px
// `.sessions-ctrls` is a real child of the cluster and its width is the search
// strip a person uses; at or below 640px the `SessionsControls` hoist makes it
// a block-level sibling of `.panel-header`, where a min-width floor changes no
// rendered box at all. So the defense costs nothing visible on either side.
//
// `ruleBody` above cannot express this: it takes the LONGEST body for a
// selector across the whole file and knows nothing about at-rules, so a
// declaration inside a media query would be answered by the top-level rule of
// the same name. The block is sliced by brace matching instead.
function mobileSessionsBlock(): string {
  const anchorSelector = '#panel-sessions > .sessions-ctrls {';
  const anchor = css.indexOf(anchorSelector);
  expect(anchor, 'no #panel-sessions > .sessions-ctrls rule in index.css').toBeGreaterThan(-1);
  const open = css.lastIndexOf('@media (max-width: 640px) {', anchor);
  expect(open, 'the Sessions mobile hoist left @media (max-width: 640px)').toBeGreaterThan(-1);
  let depth = 0;
  for (let i = css.indexOf('{', open); i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    else if (css[i] === '}') {
      depth -= 1;
      if (depth === 0) return css.slice(open, i + 1);
    }
  }
  throw new Error('unterminated @media (max-width: 640px) block in index.css');
}

describe('#750 S2 — .sessions-ctrls can give inside the actions cluster', () => {
  it('floors the controls strip inside the mobile query', () => {
    expect(mobileSessionsBlock()).toMatch(/\n\s*\.sessions-ctrls\s*\{[^}]*min-width:\s*0/);
  });

  it('is not vacuous: the slice really is the block holding the mobile hoist', () => {
    const block = mobileSessionsBlock();
    expect(block).toContain('#panel-sessions > .sessions-ctrls {');
    expect(block.startsWith('@media (max-width: 640px) {')).toBe(true);
  });

  it('leaves the desktop strip alone: no bare floor outside the query', () => {
    const block = mobileSessionsBlock();
    const outside = css.slice(0, css.indexOf(block)) + css.slice(css.indexOf(block) + block.length);
    expect(outside).not.toMatch(/\n\s*\.sessions-ctrls\s*\{[^}]*min-width:\s*0/);
  });
});

// #750 S2 — the Daily foot stacks rather than clipping its own figures.
//
// Diagnosed in a real browser at 320x844: `.daily-foot-col` declares
// `min-width: 0`, which replaces a grid item's automatic min-content minimum,
// so `1fr 1fr` resolved to two 117px tracks inside a 258px `.panel-body` even
// though the peak column's min-content is 158.7px and the total column's is
// 136.8px. Both overflowed — the peak by 42px, the total by 20px — and
// `.panel-body`'s `overflow-x: hidden` clipped them instead of scrolling, so
// the figures were silently wrong rather than visibly cut. It was never a
// 320px-only defect: the peak still overflowed by 22px at 360px and by 7px at
// 390px.
describe('#750 S2 — the Daily foot does not clip its dollar figures', () => {
  it('floors each foot track so the columns stack instead of squeezing', () => {
    const body = ruleBody('.daily-foot');
    expect(body).toMatch(/grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(160px,\s*1fr\)\)/);
  });

  it('lets the value line wrap, which is the backstop behind that floor', () => {
    expect(ruleBody('.daily-foot-text .val')).toMatch(/flex-wrap:\s*wrap/);
  });

  it('is not vacuous: both rules are the ones that size the foot', () => {
    expect(ruleBody('.daily-foot')).toMatch(/display:\s*grid/);
    expect(ruleBody('.daily-foot-text .val')).toMatch(/display:\s*flex/);
  });
});
