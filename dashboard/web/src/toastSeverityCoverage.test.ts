/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  RATE_CHANGE_SEVERITIES,
  THRESHOLD_SEVERITIES,
} from './types/envelope';

// #693 — every severity a toast can be given must have a toast rule.
//
// The dashboard runs TWO severity vocabularies. The threshold family bands a
// percentage (`info` / `warn` / `critical`); the metering-rate family states
// its severity outright (`info` / `warn` / `alarm`). Both interpolate their
// token into `toast--severity-${severity}`, so a member with no rule keeps the
// base `.toast` amber border and reads as a warning whatever it actually is.
// That is exactly what shipped: `alarm` had no rule at all.
//
// The test iterates the two exported tuples rather than a list written here,
// and those tuples ARE the unions (`types/envelope.ts` derives both types from
// them), so a fourth member cannot be added to either type without appearing
// in the array this file walks.
//
// Three ways a weaker version of this test would pass while the defect
// survived, and what is done about each:
//
//   1. A COMMENT satisfying the search. The block comment above the severity
//      rules names the tokens in prose. So the stylesheet is comment-stripped
//      before it is parsed — which is required for correctness anyway, since
//      comments in this file contain braces that would otherwise break the
//      rule slicer (the same reason `cardChromeTokens.test.ts` strips them).
//      The count of such comments is deliberately not stated here: it changes
//      whenever the stylesheet is edited, and the first test below asserts
//      that at least one exists rather than trusting a number.
//   2. A DESCENDANT selector standing in for the rule.
//      `.toast--severity-info .toast--alert-threshold` contains the string
//      `.toast--severity-info`, so a substring check passes for a member whose
//      own border rule was deleted. The lookup below compares against parsed
//      selector-list entries, so it matches the whole compound only.
//   3. BRITTLENESS the other way. Pinning an exact literal would fail on a
//      grouped selector, a reordered declaration or a whitespace change, all
//      of which are harmless. The assertion reads a parsed selector list and
//      the declared border colour, not a formatting of the rule.
//   4. A CONDITIONAL rule standing in for an unconditional one. A first draft
//      of this file skipped an at-rule PRELUDE and let the rules nested inside
//      it match on their own, so `@media (min-width: 99999px) { … }` around a
//      severity rule satisfied every assertion here while the toast kept the
//      base amber border at every real viewport. Nesting depth is therefore
//      tracked, and only a top-level rule counts as reachable.
//   5. THE CASCADE. Taking the first matching rule reports a colour the
//      browser may not paint, because a later rule with the same selector
//      wins. The lookup takes the last one that declares a border.
const cssPath = resolve(process.cwd(), 'src', 'index.css');
const rawCss = readFileSync(cssPath, 'utf8');
const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, '');

interface CssRule {
  selectors: string[];
  body: string;
  // How many at-rule blocks (`@media`, `@supports`, …) enclose this rule. A
  // rule at depth 0 applies unconditionally; anything deeper applies only when
  // its condition holds, which is not what "reachable" means here.
  atRuleDepth: number;
}

// A single left-to-right scan. Comments are already gone, so a declaration
// block contains no nested braces and ends at the next `}`; an at-rule prelude
// opens a block instead, which is what the depth counter tracks.
function parseRules(source: string): CssRule[] {
  const out: CssRule[] = [];
  let atRuleDepth = 0;
  let headStart = 0;
  let i = 0;
  while (i < source.length) {
    const ch = source[i];
    if (ch === '{') {
      const prelude = source.slice(headStart, i).trim();
      if (prelude.startsWith('@')) {
        atRuleDepth += 1;
        i += 1;
        headStart = i;
        continue;
      }
      const end = source.indexOf('}', i + 1);
      const stop = end === -1 ? source.length : end;
      out.push({
        selectors: prelude.split(',').map((s) => s.trim()).filter(Boolean),
        body: source.slice(i + 1, stop),
        atRuleDepth,
      });
      i = stop + 1;
      headStart = i;
      continue;
    }
    if (ch === '}') {
      if (atRuleDepth > 0) atRuleDepth -= 1;
      i += 1;
      headStart = i;
      continue;
    }
    i += 1;
  }
  return out;
}

const rules = parseRules(css);

// The `border` shorthand the browser would actually paint for
// `.toast--severity-<member>`: the LAST unconditional rule whose selector list
// contains that selector as a whole compound, or null when there is none.
function severityBorder(member: string, pool: CssRule[] = rules): string | null {
  const target = `.toast--severity-${member}`;
  let painted: string | null = null;
  for (const rule of pool) {
    if (rule.atRuleDepth !== 0) continue;
    if (!rule.selectors.includes(target)) continue;
    const declared = rule.body.match(/(?:^|;)\s*border\s*:\s*([^;]+)/);
    if (declared) painted = declared[1].trim();
  }
  return painted;
}

const ALL_SEVERITIES = [
  ...new Set<string>([...THRESHOLD_SEVERITIES, ...RATE_CHANGE_SEVERITIES]),
];

describe('toast severity rule coverage (#693)', () => {
  it('parses the stylesheet with comments removed and finds real rules', () => {
    // Non-vacuity for the parse itself: comments exist, carry braces, and are
    // gone from what is searched; and the parse produced a populated rule set.
    expect(rawCss).toContain('/*');
    expect(rawCss).toMatch(/\/\*[^*]*[{}][\s\S]*?\*\//);
    expect(css).not.toContain('/*');
    expect(rules.length).toBeGreaterThan(500);
    expect(ALL_SEVERITIES.length).toBeGreaterThanOrEqual(4);
  });

  it('gives every member of both severity vocabularies its own toast border rule', () => {
    for (const member of ALL_SEVERITIES) {
      const border = severityBorder(member);
      expect(
        border,
        `.toast--severity-${member} has no standalone border rule — a toast ` +
          `given this severity would keep the base .toast border`,
      ).not.toBeNull();
      // A border with no colour token is the base amber by another name.
      expect(border).toMatch(/var\(--[a-z0-9-]+\)/);
    }
  });

  it('will not accept a descendant selector in place of the rule', () => {
    // The descendant rule this guard exists to reject is really in the
    // stylesheet, so the negative case below is live rather than hypothetical.
    const descendants = rules.filter((rule) =>
      rule.selectors.some((s) => /^\.toast--severity-\S+\s+\S/.test(s)),
    );
    expect(descendants.length).toBeGreaterThan(0);
    // And a token that has only prose behind it resolves to nothing, which is
    // what makes the assertions above capable of failing.
    expect(severityBorder('nonexistent')).toBeNull();
  });

  it('will not accept a rule that only applies inside a conditional at-rule', () => {
    // The exact shape a reachability test has to reject: the selector is
    // declared, and an alarm toast still keeps the base amber border at every
    // viewport a person will ever use.
    const buried = parseRules(`
      @media (min-width: 99999px) {
        .toast--severity-alarm { border: 1px solid var(--accent-red); }
      }
    `);
    expect(buried).toHaveLength(1);
    expect(buried[0].atRuleDepth).toBe(1);
    expect(severityBorder('alarm', buried)).toBeNull();
    // The same rule at top level IS accepted, so the guard rejects the nesting
    // rather than the input.
    const plain = parseRules('.toast--severity-alarm { border: 1px solid var(--accent-red); }');
    expect(severityBorder('alarm', plain)).toBe('1px solid var(--accent-red)');
  });

  it('reports the border the cascade paints, not the first one declared', () => {
    const twice = parseRules(`
      .toast--severity-alarm { border: 1px solid var(--accent-red); }
      .toast--severity-alarm { border: 1px solid var(--accent-amber); }
    `);
    expect(severityBorder('alarm', twice)).toContain('var(--accent-amber)');
  });

  it('paints both vocabularies from the shared accent palette', () => {
    // D1: red is already this family's alarm colour on its other two surfaces
    // (`.chip-rate-change.severity-alarm`, `.fc-rate-chip.severity-alarm`), so
    // the alarm toast joins critical on `--accent-red` rather than inventing a
    // fourth accent. warn and info keep the colours they already had.
    expect(severityBorder('info')).toContain('var(--accent-indigo)');
    expect(severityBorder('warn')).toContain('var(--accent-amber)');
    expect(severityBorder('critical')).toContain('var(--accent-red)');
    expect(severityBorder('alarm')).toContain('var(--accent-red)');
  });
});
