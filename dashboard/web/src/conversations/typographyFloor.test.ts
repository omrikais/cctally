import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// #304 S3 — conversation-viewer typography floor guard (spec §3). Static scan:
// no viewer text below --fs-eyebrow (11px). Parser is an offset-preserving
// comment-stripping balanced-brace scanner (Codex F8), NOT a brace-pair regex.

const CSS_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', 'index.css');

interface CssRule { selector: string; body: string; line: number; }

// Replace comment characters with spaces so offsets/line numbers survive.
export function stripCssComments(css: string): string {
  let out = '';
  let i = 0;
  while (i < css.length) {
    if (css[i] === '/' && css[i + 1] === '*') {
      const end = css.indexOf('*/', i + 2);
      const stop = end === -1 ? css.length : end + 2;
      for (; i < stop; i++) out += css[i] === '\n' ? '\n' : ' ';
    } else { out += css[i]; i++; }
  }
  return out;
}

export function extractRules(cssRaw: string): CssRule[] {
  const css = stripCssComments(cssRaw);
  const rules: CssRule[] = [];
  const lineAt = (idx: number) => cssRaw.slice(0, idx).split('\n').length;
  function walk(start: number, end: number): void {
    let i = start;
    let headStart = start;
    while (i < end) {
      const ch = css[i];
      if (ch === '{') {
        // find matching close brace
        let depth = 1; let j = i + 1;
        while (j < end && depth > 0) { if (css[j] === '{') depth++; else if (css[j] === '}') depth--; j++; }
        const head = css.slice(headStart, i).trim();
        const bodyStart = i + 1; const bodyEnd = j - 1;
        if (head.startsWith('@')) {
          if (/^@(media|supports|layer|container)\b/.test(head)) walk(bodyStart, bodyEnd); // descend
          // @keyframes / @font-face etc: skip (no selector rules inside)
        } else if (head) {
          rules.push({ selector: head, body: css.slice(bodyStart, bodyEnd), line: lineAt(headStart + (head.length - head.trimStart().length)) });
        }
        headStart = j; i = j;
      } else if (ch === '}') { headStart = i + 1; i++; }
      else i++;
    }
  }
  walk(0, css.length);
  return rules;
}

const FLOOR_PX = 11;
// Floor-safe tokens (spec §3): any OTHER var() in a conv font-size fails loud.
const SAFE_TOKENS = new Set(['--fs-eyebrow', '--fs-meta', '--fs-body', '--fs-data', '--fs-strong', '--fs-title', '--fs-kpi', '--fs-hero', '--fs-display']);
// Ships EMPTY (spec §3 / Q4). Entries need a spec revision.
const ALLOWLIST: ReadonlyArray<{ selector: string; value: string }> = [];

function violations(cssRaw: string): string[] {
  const out: string[] = [];
  for (const rule of extractRules(cssRaw)) {
    const selectors = rule.selector.split(',').map((s) => s.trim());
    if (!selectors.some((s) => s.includes('.conv-'))) continue;
    const decls = [...rule.body.matchAll(/(?:^|;)\s*(font-size|font)\s*:\s*([^;]+)/g)];
    for (const [, prop, valueRaw] of decls) {
      const value = valueRaw.trim();
      if (ALLOWLIST.some((a) => a.selector === rule.selector && a.value === value)) continue;
      for (const [, num] of value.matchAll(/([\d.]+)px/g)) {
        if (parseFloat(num) < FLOOR_PX) out.push(`${rule.selector} (line ${rule.line}): ${prop}: ${value}`);
      }
      for (const [, token] of value.matchAll(/var\((--[\w-]+)/g)) {
        if (prop === 'font-size' && !SAFE_TOKENS.has(token)) out.push(`${rule.selector} (line ${rule.line}): unknown font token ${token}`);
      }
    }
  }
  return out;
}

describe('conversation-viewer typography floor (#304 S3)', () => {
  const css = readFileSync(CSS_PATH, 'utf8');

  it('has no .conv- rule with a font size below 11px', () => {
    expect(violations(css)).toEqual([]);
  });

  // Anti-vacuity anchors (Codex F7): the one known cross-scope defense must
  // exist and the floor token must be 11px — deleting either turns this RED
  // even though bare `.chip` sits outside the .conv- scan scope.
  it('pins --fs-eyebrow at 11px', () => {
    expect(css).toMatch(/--fs-eyebrow:\s*11px/);
  });
  it('requires the .conv-view .chip floor override', () => {
    const rule = extractRules(css).find((r) => r.selector === '.conv-view .chip');
    expect(rule, '.conv-view .chip override missing').toBeTruthy();
    expect(rule!.body).toMatch(/font-size:\s*var\(--fs-eyebrow\)/);
  });

  // Parser fixtures (Codex F8) — the scanner itself is under test.
  it('parser: nested media, comments, multi-selectors, shorthand, clamp, line numbers', () => {
    const fixture = [
      '/* font-size: 5px in a comment is ignored */',
      '@media (max-width: 100px) { @media (min-width: 10px) { .conv-a, .other { font-size: 9px; } } }',
      '@keyframes conv-spin { from { font-size: 8px; } }',
      '.conv-b { font: 700 10px/1.4 monospace; }',
      '.conv-c { font-size: clamp(10.5px, 2vw, 14px); }',
      '.conv-d { font-size: var(--fs-eyebrow); }',
      '.conv-e { font-size: var(--mystery-token); }',
    ].join('\n');
    const v = violations(fixture);
    expect(v.some((x) => x.startsWith('.conv-a, .other (line 2)'))).toBe(true); // nested media + multi-selector + line
    expect(v.some((x) => x.includes('conv-spin'))).toBe(false);                  // keyframes skipped
    expect(v.some((x) => x.startsWith('.conv-b'))).toBe(true);                   // font shorthand
    expect(v.some((x) => x.startsWith('.conv-c'))).toBe(true);                   // clamp px literal
    expect(v.some((x) => x.includes('--mystery-token'))).toBe(true);             // unknown token fails loud
    expect(v.some((x) => x.startsWith('.conv-d'))).toBe(false);                  // safe token passes
    expect(v).toHaveLength(4);
  });
});
