import { describe, expect, it } from 'vitest';
import { computeSequenceDiff, normalizeLabel, type SpinePrompt } from './sessionAlign';

const p = (uuid: string, label: string): SpinePrompt => ({ uuid, label });

describe('normalizeLabel', () => {
  it('trims, collapses whitespace, lowercases', () => {
    expect(normalizeLabel('  Refactor   AUTH  ')).toBe('refactor auth');
  });
});

describe('computeSequenceDiff', () => {
  it('all-match when sequences are identical', () => {
    const a = [p('a1', 'one'), p('a2', 'two')];
    const b = [p('b1', 'ONE'), p('b2', ' two ')];
    const rows = computeSequenceDiff(a, b);
    expect(rows.map(r => r.kind)).toEqual(['match', 'match']);
    expect(rows.every(r => !r.divergence)).toBe(true);
  });

  it('marks a replaced region as divergence with paired rows', () => {
    const a = [p('a1', 'shared'), p('a2', 'fix mock'), p('a3', 'run suite')];
    const b = [p('b1', 'shared'), p('b2', 'use fixtures'), p('b3', 'run suite')];
    const rows = computeSequenceDiff(a, b);
    expect(rows.map(r => r.kind)).toEqual(['match', 'replace', 'match']);
    const rep = rows[1];
    expect(rep.divergence).toBe(true);
    expect(rep.a?.uuid).toBe('a2');
    expect(rep.b?.uuid).toBe('b2');
  });

  it('one-sided insertion is aOnly/bOnly with a gap, no divergence', () => {
    const a = [p('a1', 'shared'), p('a2', 'extra A')];
    const b = [p('b1', 'shared')];
    const rows = computeSequenceDiff(a, b);
    expect(rows.map(r => r.kind)).toEqual(['match', 'aOnly']);
    expect(rows[1].b).toBeNull();
    expect(rows[1].divergence).toBe(false);
  });

  it('unequal replaced run: pairs to shorter length, remainder one-sided, all in the divergence region', () => {
    const a = [p('a1', 'x1'), p('a2', 'x2')];
    const b = [p('b1', 'y1')];
    const rows = computeSequenceDiff(a, b);
    // a1/b1 replace, a2 aOnly — both flagged divergence (adjacent del+add region)
    expect(rows.map(r => r.kind)).toEqual(['replace', 'aOnly']);
    expect(rows.every(r => r.divergence)).toBe(true);
  });

  it('handles an empty session on either side', () => {
    expect(computeSequenceDiff([], []).length).toBe(0);
    expect(computeSequenceDiff([p('a1', 'x')], []).map(r => r.kind)).toEqual(['aOnly']);
    expect(computeSequenceDiff([], [p('b1', 'y')]).map(r => r.kind)).toEqual(['bOnly']);
  });
});
