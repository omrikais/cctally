// #923 S2 — the Projects label classifier (spec §3.1). Detects a split that
// drops or reorders label text, protects too little of a generated qualifier,
// or truncates a label whose parentheses are unbalanced.
import { describe, expect, it } from 'vitest';
import { classifyProjectLabel } from './projectLabel';

describe('classifyProjectLabel', () => {
  it.each([
    // The spec §3.1 example table.
    ['repo (1)', 'repo', ' (1)'],
    ['app (old) (1)', 'app', ' (old) (1)'],
    ['app (a/b)', 'app', ' (a/b)'],
    ['foo (bar (baz))', 'foo', ' (bar (baz))'],
    ['app (x) b (y)', 'app (x) b', ' (y)'],
    // Codex: an ordinal appended to a raw label, with and without parentheses.
    ['customer-analytics-pipeline-service (2)', 'customer-analytics-pipeline-service', ' (2)'],
    ['tool (beta) (12)', 'tool', ' (beta) (12)'],
    // Claude: parent, deep and last-resort ordinal qualifiers.
    ['api (client-engagements-archive-2026)', 'api', ' (client-engagements-archive-2026)'],
    ['api (work/acme)', 'api', ' (work/acme)'],
    ['api (work/acme) (3)', 'api', ' (work/acme) (3)'],
    // Whitespace inside the base is kept verbatim, never trimmed.
    ['  my  repo (1)', '  my  repo', ' (1)'],
  ])('splits %j into %j + %j', (label, base, suffix) => {
    const result = classifyProjectLabel(label);
    expect(result).toEqual({ kind: 'split', base, suffix });
    if (result.kind === 'split') expect(result.base + result.suffix).toBe(label);
  });

  it.each([
    '(unknown)',
    'foo',
    'foo(1)',
    ' (1)',
    'foo (1) bar',
    '',
  ])('leaves %j plain', (label) => {
    expect(classifyProjectLabel(label)).toEqual({ kind: 'plain' });
  });

  it.each([
    'foo)',
    // Directory text with an unmatched parenthesis inside a generated qualifier.
    'app (customer-alpha (b)',
    'app (old (1)',
    'app (customer-alpha (b) (1)',
    'a) b (1)',
    'x (',
  ])('marks %j ambiguous so it is never truncated', (label) => {
    expect(classifyProjectLabel(label)).toEqual({ kind: 'ambiguous' });
  });
});
