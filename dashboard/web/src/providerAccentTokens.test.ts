/// <reference types="node" />
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// #463 S5 (F24b) — exactly one token names each provider's identity, everywhere.
// Before this, --accent-blue named Claude on the dashboard chips and Codex on the
// conversation-viewer badges, so one colour meant two different providers.
const cssPath = resolve(process.cwd(), 'src/index.css');
const css = (existsSync(cssPath) ? readFileSync(cssPath, 'utf8') : '').replace(/\/\*[\s\S]*?\*\//g, '');

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

const IDENTITY: Array<[string, string]> = [
  ['.source-chip--codex', '--accent-codex'],
  ['.source-chip--claude', '--accent-purple'],
  ['.conv-source-badge--codex', '--accent-codex'],
  ['.conv-source-badge--claude', '--accent-purple'],
  ['.accent-provider-codex', '--accent-codex'],
  ['.accent-provider-claude', '--accent-purple'],
];

describe('#463 S5 — one identity token per provider', () => {
  it('finds the stylesheet', () => {
    expect(existsSync(cssPath)).toBe(true);
  });

  for (const [selector, token] of IDENTITY) {
    it(`${selector} uses ${token}`, () => {
      const body = ruleBody(selector);
      expect(body).toContain(`var(${token})`);
    });

    it(`${selector} names no other accent token`, () => {
      const body = ruleBody(selector);
      const others = ['--accent-blue', '--accent-green', '--accent-codex', '--accent-purple'].filter((t) => t !== token);
      for (const other of others) expect(body, `${selector} also uses ${other}`).not.toContain(other);
    });
  }

  it('no identity selector carries a raw hex fallback', () => {
    for (const [selector] of IDENTITY) {
      expect(ruleBody(selector)).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    }
  });
});

// #463 S5 (F24b, spec §3.4) — the selector scan above is only half the guard. A
// selector scan cannot see a RENDER SITE that hardcodes a colour, which is
// precisely the false-negative class that let five provider pills ship through a
// generic accent class and stay invisible to the audit. Scan the sources too, in
// the metaLabelConstruction.test.ts idiom.
const SRC = resolve(process.cwd(), 'src');
const ACCENT_OWNER = join(SRC, 'lib', 'providerAccent.ts');

function productionSources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) { productionSources(path, out); continue; }
    if (!/\.tsx?$/.test(name)) continue;
    if (/\.test\.tsx?$/.test(name)) continue;
    out.push(path);
  }
  return out;
}

// An element's opening tag, plus the immediate text child that follows it. The
// alternation lists EVERY accent class index.css defines, because the rule this
// scan states is about generic accents in general, not about the four the first
// draft happened to name — `.accent-orange` was defined and unscanned.
//
// Two limits the scan does not close, stated so the guard is not read as
// stronger than it is. (1) `[^<>]` means an opening tag containing a `>` never
// matches at all, so any element carrying an arrow function (`onClick={() =>
// …}`) is invisible here. (2) The capture is `[^<]*`, so a tag whose first child
// is an ELEMENT rather than text captures the empty string, and a provider named
// inside that nested element is not seen. Both are false negatives; the browser
// gate and review remain the backstop for them.
const ACCENT_TAG =
  /<[a-zA-Z][^<>]*?accent-(?:amber|blue|codex|cyan|green|indigo|magenta|orange|pink|purple|red|teal)[^<>]*>([^<]*)</g;
const NAMES_A_PROVIDER = /\b(Codex|Claude)\b/;

// #463 S5 / #498 — the two sites the widened alternation flags, deferred with a
// recorded reason rather than left to a regex that happened not to look. Both
// are `<span className="m-pill accent-orange">Codex · native 7-day quota</span>`
// in the Codex arm of the current-week modal, whose whole section is painted in
// a per-section orange palette. Recolouring just these two provider-naming pills
// would leave them disagreeing with four non-naming siblings in the same rows,
// so #498 owns restyling the section as a unit.
//
// The count is the tripwire in both directions: a third such pill fails this
// test, and so does #498 landing and leaving a stale exemption behind.
const DEFERRED_GENERIC_ACCENT_SITES: Array<{ file: string; text: string; count: number }> = [
  { file: join('modals', 'CurrentWeekModal.tsx'), text: 'Codex · native 7-day quota', count: 2 },
];

describe('#463 S5 — provider identity is rendered through one owner', () => {
  const files = productionSources(SRC);

  it('scans a non-trivial number of production sources', () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it('the owner really does construct both identity class literals', () => {
    const owner = readFileSync(ACCENT_OWNER, 'utf8');
    expect(owner).toContain('accent-provider-codex');
    expect(owner).toContain('accent-provider-claude');
  });

  it('no production source outside lib/providerAccent.ts writes an accent-provider-* literal', () => {
    const offenders = files
      .filter((f) => f !== ACCENT_OWNER)
      .filter((f) => /accent-provider-/.test(readFileSync(f, 'utf8')));
    expect(offenders, 'hardcode an identity class instead of calling providerAccentClass()').toEqual([]);
  });

  it('no pill naming a provider carries a generic accent class', () => {
    // The complement of the rule above: a render site can also name a provider
    // in its own text while painting itself with a NON-identity accent, which is
    // how `accent-blue` came to mean Codex here and Claude on the source chips.
    // A site whose className routes through providerAccentClass() is compliant
    // even when a sibling branch keeps a generic accent (TrendModal's "All
    // sources" arm names no provider and correctly stays generic).
    const offenders: string[] = [];
    const deferredSeen = DEFERRED_GENERIC_ACCENT_SITES.map(() => 0);
    for (const file of files) {
      const source = readFileSync(file, 'utf8');
      for (const match of source.matchAll(ACCENT_TAG)) {
        const tag = source.slice(match.index, source.indexOf('>', match.index) + 1);
        if (tag.includes('providerAccentClass')) continue;
        if (!NAMES_A_PROVIDER.test(match[1])) continue;
        const text = match[1].trim();
        const deferred = DEFERRED_GENERIC_ACCENT_SITES.findIndex(
          (site) => file.endsWith(site.file) && text === site.text,
        );
        if (deferred >= 0) { deferredSeen[deferred] += 1; continue; }
        offenders.push(`${file}:${source.slice(0, match.index).split('\n').length} ${text}`);
      }
    }
    expect(offenders, 'name a provider but paint themselves with a generic accent').toEqual([]);
    expect(
      deferredSeen,
      'a #498 exemption no longer matches its site — close the exemption or widen it deliberately',
    ).toEqual(DEFERRED_GENERIC_ACCENT_SITES.map((site) => site.count));
  });

  it('the pill scan is non-vacuous — it does find accent-classed elements to judge', () => {
    let seen = 0;
    for (const file of files) {
      seen += Array.from(readFileSync(file, 'utf8').matchAll(ACCENT_TAG)).length;
    }
    expect(seen).toBeGreaterThan(5);
  });
});
